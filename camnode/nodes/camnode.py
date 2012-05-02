#!/usr/bin/env python

"""

There are several ways we want to acquire data:

A) From live cameras (for indefinite periods).
B) From full-frame .fmf files (of known length).
C) From small-frame .ufmf files (of unknown length).
D) From a live image generator (for indefinite periods).
E) From a point generator (for indefinite periods).

The processing chain normally consists of:

0) Grab images from ImageSource. (This is not actually part of the chain).
1) Processing the images in ProcessCamData
2) Save images in SaveFMF.
3) Save small .ufmf images in SaveUFMF.
4) Display images in DisplayCamData.

In cases B-E, some form of image/data control (play, stop, set fps)
must be settable. Ideally, this would be possible from a Python API
(for automated testing) and from a GUI (for visual debugging).

"""

from __future__ import division
from __future__ import with_statement
 
PACKAGE='camnode'
import roslib; roslib.load_manifest(PACKAGE)
import rospy

from sensor_msgs.msg import Image, CameraInfo
from dynamic_reconfigure.server import Server
from camnode.cfg import CamnodeConfig

import os
BENCHMARK = int(os.environ.get('FLYDRA_BENCHMARK',0))
FLYDRA_BT = int(os.environ.get('FLYDRA_BT',0)) # threaded benchmark

NAUGHTY_BUT_FAST = False
#DISABLE_ALL_PROCESSING = True
DISABLE_ALL_PROCESSING = False
near_inf = 9.999999e20
bright_non_gaussian_cutoff = 255
bright_non_gaussian_replacement = 5

import contextlib
from contextlib import contextmanager
import errno
import numpy as np
import numpy.dual
import pickle
import Queue
import scipy.misc.pilutil
import socket
import string
import threading, time, sys, struct
import traceback
from optparse import OptionParser

import motmot.ufmf.ufmf as ufmf
import motmot.FlyMovieFormat.FlyMovieFormat as FlyMovieFormat
cam_iface = None # global variable, value set in main()
import motmot.cam_iface.choose as cam_iface_choose
import motmot.FastImage.FastImage as FastImage
import motmot.realtime_image_analysis.realtime_image_analysis as realtime_image_analysis

import flydra.camnode_colors as camnode_colors
import flydra.camnode_utils as camnode_utils

import flydra.reconstruct_utils as reconstruct_utils
import flydra.version
from flydra.reconstruct import do_3d_operations_on_2d_point
import flydra.debuglock
DebugLock = flydra.debuglock.DebugLock

from mainbrain.srv import *
gLockParams = threading.Lock()

#FastImage.set_debug(3)

if sys.platform == 'win32':
    time_func = time.clock
#else:
#    time_func = rospy.Time.now().to_sec #time.time

pt_fmt = '<dddddddddBBddBddddddddd' # Keep this in sync with MainBrain.py
small_datafile_fmt = '<dII'


#LOGLEVEL = rospy.DEBUG
#LOGLEVEL = rospy.INFO
LOGLEVEL = rospy.WARN
#LOGLEVEL = rospy.ERROR
#LOGLEVEL = rospy.FATAL

USE_ROS = False # False=UseTheSocketsInterfaceToMainbrain,  True=UseTheROSServicesInterfaceToMainbrain
USE_ONE_TIMEPORT_PER_CAMERA = USE_ROS # True=OnePerCamera, False=OnePerCamnode.  Keep MainBrain.py in sync with this.

if not BENCHMARK:
    import Pyro.core, Pyro.errors, Pyro.util
    Pyro.config.PYRO_MULTITHREADED = 0 # We do the multithreading around here!
    Pyro.config.PYRO_TRACELEVEL = 3
    Pyro.config.PYRO_USER_TRACELEVEL = 3
    Pyro.config.PYRO_DETAILED_TRACEBACK = 1
    Pyro.config.PYRO_PRINT_REMOTE_TRACEBACK = 1
    ConnectionClosedError = Pyro.errors.ConnectionClosedError
else:
    class NonExistantError(Exception):
        pass
    ConnectionClosedError = NonExistantError



###############################################################################
# Class & Function defs
###############################################################################

@contextmanager
def monkeypatch_camera_method(self):
    with self._monkeypatched_lock:
        # get the lock

        # hack the THREAD_DEBUG stuff in cam_iface_ctypes
        self.mythread = threading.currentThread()

        yield # run what we need to run

    # release the lock


class NullClass:
    pass

class SharedValue1(object):
    # in trackem
    def __init__(self, initial_value):
        self._val = initial_value
        self.lock = threading.Lock()
    def get(self):
        self.lock.acquire()
        try:
            val = self._val
        finally:
            self.lock.release()
        return val
    def set(self,new_value):
        self.lock.acquire()
        try:
            self._val = new_value
        finally:
            self.lock.release()


class DummyMainBrain:
    def __init__(self,*args,**kw):
        self.set_image = self.noop
        self.set_fps = self.noop
        self.log_message = self.noop
        self.close_camera = self.noop
        self.camno = 0
    def noop(self,*args,**kw):
        return
    def get_cam2mainbrain_port(self,*args,**kw):
        return 12345
    def register_new_camera(self,*args,**kw):
        result = 'camdummy_%d'%self.camno
        self.camno += 1
        return result
    def get_and_clear_commands(self,*args,**kw):
        return {}



def stdout_write(x):
    while 1:
        try:
            sys.stdout.write(x)
            break
        except IOError, err:
            if err.args[0] == errno.EINTR: # interrupted system call
                continue
    while 1:
        try:
            sys.stdout.flush()
            break
        except IOError, err:
            if err.args[0] == errno.EINTR: # interrupted system call
                continue

L_i = np.array([0,0,0,1,3,2])
L_j = np.array([1,2,3,2,1,3])

def Lmatrix2Lcoords(Lmatrix):
    return Lmatrix[L_i,L_j]

def pluecker_from_verts(A,B):
    """
    See Hartley & Zisserman (2003) p. 70
    """
    if len(A)==3:
        A = A[0], A[1], A[2], 1.0
    if len(B)==3:
        B = B[0], B[1], B[2], 1.0
    A=np.reshape(A,(4,1))
    B=np.reshape(B,(4,1))
    L = np.dot(A,np.transpose(B)) - np.dot(B,np.transpose(A))
    return Lmatrix2Lcoords(L)

class PreallocatedBuffer(object):
    def __init__(self,size,pool):
        self._size = size
        self._buf = FastImage.FastImage8u(size)
        self._pool = pool
    def get_size(self):
        return self._size
    def get_buf(self):
        return self._buf
    def get_pool(self):
        return self._pool

class PreallocatedBufferPool(object):
    """One instance of this class for each camera. Threadsafe."""
    def __init__(self,size):
        self._lock = threading.Lock()
        # start: vars access controlled by self._lock
        self._allocated_pool = []
        #   end: vars access controlled by self._lock

        self.set_size(size)
        self._buffers_handed_out = 0 # self._zero_buffer_lock is set when this is 0
        self._zero_buffer_lock = threading.Event()
        self._zero_buffer_lock.set()

    def set_size(self,size):
        """size is FastImage.Size() instance"""
        assert isinstance(size,FastImage.Size)
        with self._lock:
            self._size = size
            del self._allocated_pool[:]

    def get_free_buffer(self):
        with self._lock:
            if len(self._allocated_pool):
                buf = self._allocated_pool.pop()
            else:
                buf = PreallocatedBuffer(self._size,self)
            self._buffers_handed_out += 1
            self._zero_buffer_lock.clear()
            return buf

    def return_buffer(self,buf):
        assert isinstance(buf, PreallocatedBuffer)
        with self._lock:
            self._buffers_handed_out -= 1
            if buf.get_size() == self._size:
                self._allocated_pool.append( buf )

            if self._buffers_handed_out == 0:
                self._zero_buffer_lock.set()

    def get_num_outstanding_buffers(self):
        return self._buffers_handed_out

    def wait_for_0_outstanding_buffers(self,*args):
        self._zero_buffer_lock.wait(*args)

@contextlib.contextmanager
def get_free_buffer_from_pool(pool):
    """manage access to buffers from the pool"""
    buf = pool.get_free_buffer()
    buf._i_promise_to_return_buffer_to_the_pool = False
    try:
        yield buf
    finally:
        if not buf._i_promise_to_return_buffer_to_the_pool:
            pool.return_buffer(buf)



###############################################################################
# Processors: ProcessCamData, FakeProcessCamData
###############################################################################

class ProcessCamData(object):
    def __init__(self,
                 iCamera=None,
                 idCamera=None,
                 log_message_queue=None,
                 max_num_points=None,
                 roi2_radius=None,
                 bg_frame_interval=None,
                 bg_frame_alpha=None,
                 mask_image=None,
                 framerate = None,
                 lbrt=None,
                 max_height=None,
                 max_width=None,
                 camerainfo = None,
                 options = None,
                 initial_image_dict = None,
                 benchmark = False,
                 mainbrain = None,
                 ):

        

        self.benchmark = benchmark
        self.options = options
        self.camerainfo = camerainfo
        self.mainbrain = mainbrain
        
        if framerate is not None:
            self.shortest_IFI = 1.0/framerate  # "Inter-Frame Interval"?
        else:
            self.shortest_IFI = numpy.inf
        self.idCamera = idCamera
        self.log_message_queue = log_message_queue
        self.rosrate = float(self.options.rosrate)
        self.lasttime = rospy.Time.now().to_sec()


        self.bg_frame_alpha = bg_frame_alpha
        self.bg_frame_interval = bg_frame_interval
        
        self.lockParameters = gLockParams #threading.Lock()
        self.LoadParameters()

        self.new_roi = threading.Event()
        self.new_roi_data = None
        self.new_roi_data_lock = threading.Lock()

        self.max_height = max_height
        self.max_width = max_width

        if mask_image is None:
            mask_image = numpy.zeros((self.max_height, self.max_width), dtype=numpy.bool)
            # mask is currently an array of bool
            mask_image = mask_image.astype(numpy.uint8)*255
            
        self.mask_image = mask_image
        self.max_num_points=max_num_points

        self.realtime_analyzer = realtime_image_analysis.RealtimeAnalyzer(lbrt,
                                                                          self.max_width,
                                                                          self.max_height,
                                                                          self.max_num_points,
                                                                          roi2_radius,
                                                                          )
        
        with self.lockParameters:
            self.realtime_analyzer.diff_threshold = self.parameters['threshold_diff']
            self.realtime_analyzer.clear_threshold = self.parameters['threshold_clear']

        self._hlper = None
        self._pmat = None
        self._scale_factor = None # for 3D calibration stuff
        self.iCamera = iCamera

        self._chain = camnode_utils.ChainLink()
        self._initial_image_dict = initial_image_dict

        rospy.logwarn('Using %s/image_raw' % idCamera )
        self.pubImageRaw = rospy.Publisher('%s/image_raw'%idCamera, Image, tcp_nodelay=True)
        self.pubCameraInfo = rospy.Publisher('%s/camera_info'%idCamera, CameraInfo, tcp_nodelay=True)


    
    def LoadParameters(self):
        with self.lockParameters:
            self.parameters = {}
            self.parameters['threshold_diff'] = rospy.get_param(rospy.get_name()+'/threshold_diff', 5.0)
            self.parameters['threshold_clear'] = rospy.get_param(rospy.get_name()+'/threshold_clear', 0.3)
            self.parameters['color_filter_sat'] = rospy.get_param(rospy.get_name()+'/color_filter_sat', 100)
            self.parameters['n_sigma'] = rospy.get_param(rospy.get_name()+'/n_sigma', 7.0)
            self.parameters['n_erode'] = rospy.get_param(rospy.get_name()+'/n_erode', 0)
            self.parameters['use_color_filter'] = rospy.get_param(rospy.get_name()+'/use_color_filter', False)
            self.parameters['color_filter_1'] = rospy.get_param(rospy.get_name()+'/color_filter_1', 0)
            self.parameters['color_filter_2'] = rospy.get_param(rospy.get_name()+'/color_filter_2', 150)
            self.parameters['color_filter_3'] = rospy.get_param(rospy.get_name()+'/color_filter_3', 255)
            self.parameters['dynamic_background'] = rospy.get_param(rospy.get_name()+'/dynamic_background', True)
            self.parameters['framerate_trigger'] = rospy.get_param(rospy.get_name()+'/framerate_trigger', 20.0)
            self.parameters['framerate_max'] = rospy.get_param(rospy.get_name()+'/framerate_max', 20.0)
            self.parameters['roi/left'] = rospy.get_param(rospy.get_name()+'/roi/left', 0)
            self.parameters['roi/top'] = rospy.get_param(rospy.get_name()+'/roi/top', 0)
            self.parameters['roi/right'] = rospy.get_param(rospy.get_name()+'/roi/right', 1023)
            self.parameters['roi/bottom'] = rospy.get_param(rospy.get_name()+'/roi/bottom', 767)
            self.parameters['trigger_mode'] = rospy.get_param(rospy.get_name()+'/trigger_mode', 0)
            self.parameters['use_cmp'] = rospy.get_param(rospy.get_name()+'/use_cmp', False)



    # SetParameters()
    # Set the parameter values from the argument, but only those that exist in both dicts.
    def SetParameters(self, config):
        with self.lockParameters:
            for key in self.parameters:
                if key in config:
                    self.parameters[key] = config[key]
        



    def get_chain(self):
        return self._chain

    def get_scale_factor(self):
        return self._scale_factor
    def set_scale_factor(self,value):
        self._scale_factor = value

    def get_roi(self):
        return self.realtime_analyzer.roi
    def set_roi(self, lbrt):
        with self.new_roi_data_lock:
            self.new_roi_data = lbrt
            self.new_roi.set()
    roi = property( get_roi, set_roi )

    def get_pmat(self):
        return self._pmat
    def set_pmat(self,value):
        if value is None:
            self._pmat = None
            self._camera_center = None
            self._pmat_inv = None
            self._scale_factor = None
            self._pmat_meters = None
            self._pmat_meters_inv = None
            self._camera_center_meters = None
            return

        self._pmat = numpy.asarray(value)

        # find camera center in 3D world coordinates
        P = self._pmat
        col0_asrow = P[np.newaxis,:,0]
        col1_asrow = P[np.newaxis,:,1]
        col2_asrow = P[np.newaxis,:,2]
        col3_asrow = P[np.newaxis,:,3]
        X = numpy.dual.det(  numpy.r_[ col1_asrow, col2_asrow, col3_asrow ] )
        Y = -numpy.dual.det( numpy.r_[ col0_asrow, col2_asrow, col3_asrow ] )
        Z = numpy.dual.det(  numpy.r_[ col0_asrow, col1_asrow, col3_asrow ] )
        T = -numpy.dual.det( numpy.r_[ col0_asrow, col1_asrow, col2_asrow ] )

        self._camera_center = np.array( [ X/T, Y/T, Z/T, 1.0 ] )
        self._pmat_inv = numpy.dual.pinv(self._pmat)

        scale_array = numpy.ones((3,4))
        scale_array[:,3] = self._scale_factor # mulitply last column by scale_factor
        self._pmat_meters = scale_array*self._pmat # element-wise multiplication
        self._pmat_meters_inv = numpy.dual.pinv(self._pmat_meters)

        # find camera center in 3D world coordinates
        P = self._pmat_meters
        col0_asrow = P[np.newaxis,:,0]
        col1_asrow = P[np.newaxis,:,1]
        col2_asrow = P[np.newaxis,:,2]
        col3_asrow = P[np.newaxis,:,3]
        X = numpy.dual.det(  numpy.r_[ col1_asrow, col2_asrow, col3_asrow ] )
        Y = -numpy.dual.det( numpy.r_[ col0_asrow, col2_asrow, col3_asrow ] )
        Z = numpy.dual.det(  numpy.r_[ col0_asrow, col1_asrow, col3_asrow ] )
        T = -numpy.dual.det( numpy.r_[ col0_asrow, col1_asrow, col2_asrow ] )
        self._camera_center_meters = np.array( [ X/T, Y/T, Z/T, 1.0 ] )

    def make_reconstruct_helper(self, intlin, intnonlin):
        if intlin is None and intnonlin is None:
            self._hlper = None
            return

        fc1 = intlin[0,0]
        fc2 = intlin[1,1]
        cc1 = intlin[0,2]
        cc2 = intlin[1,2]
        k1, k2, p1, p2 = intnonlin

        self._hlper = reconstruct_utils.ReconstructHelper(
            fc1, fc2, cc1, cc2, k1, k2, p1, p2 )

    def _convert_to_wire_order(self, xpoints, imgROI, imgRunningMean, imgSumSq ):
        """the images passed in are already in roi coords, as are index_x and index_y.
        convert to values for sending.
        """
        points = []
        imgROI = numpy.asarray( imgROI )
        for xpt in xpoints:
            try:
                (x0_abs, y0_abs, area, slope, eccentricity, index_x, index_y) = xpt
            except:
                rospy.logwarn('xpt %s'%xpt)
                raise

            # Find values at location in image that triggered
            # point. Cast to Python int and floats.
            valCur = int(imgROI[index_y,index_x])
            valMean = float(imgRunningMean[index_y, index_x])
            valSumSq = float(imgSumSq[index_y, index_x])

            if numpy.isnan(slope):
                run = numpy.nan
                line_found = False
            else:
                line_found = True
                if numpy.isinf(slope):
                    run = 0
                    rise = 1
                else:
                    run = 1
                    #slope = rise/run
                    rise = slope

            ray_valid = False
            if self._hlper is not None:
                x0u, y0u = self._hlper.undistort( x0_abs, y0_abs )
                if line_found:

                    # (If we have self._hlper _pmat_inv, we can assume we have
                    # self._pmat_inv and sef._pmat_meters.)
                    (p1, p2, p3, p4, ray0, ray1, ray2, ray3, ray4,
                     ray5) = do_3d_operations_on_2d_point(self._hlper,x0u,y0u,
                                                          self._pmat_inv, self._pmat_meters_inv,
                                                          self._camera_center, self._camera_center_meters,
                                                          x0_abs, y0_abs,
                                                          rise, run)
                    ray_valid = True
            else:
                x0u = x0_abs # fake undistorted data
                y0u = y0_abs

            if not ray_valid:
                p1,p2,p3,p4 = -1, -1, -1, -1 # sentinel value (will be converted to nan)
                (ray0, ray1, ray2, ray3, ray4, ray5) = (0,0,0, 0,0,0)

            slope_found = True
            if numpy.isnan(slope):
                # prevent nan going across network
                slope_found = False
                slope = 0.0

            if numpy.isinf(eccentricity):
                eccentricity = near_inf

            if numpy.isinf(slope):
                slope = near_inf

            # see pt_fmt struct definition:
            pt = (x0_abs, y0_abs, area, slope, eccentricity,
                  p1, p2, p3, p4, line_found, slope_found,
                  x0u, y0u,
                  ray_valid,
                  ray0, ray1, ray2, ray3, ray4, ray5,
                  valCur, valMean, valSumSq)
            points.append( pt )
        return points

    def mainloop(self):

        disable_ifi_warning = self.options.disable_ifi_warning
        DEBUG_DROP = self.camerainfo['debug_drop']
        if DEBUG_DROP:
            debug_fd = open('debug_framedrop_cam.txt',mode='w')

        # questionable optimization: speed up by eliminating namespace lookups
        cam_quit_event_isSet = self.camerainfo['cam_quit_event'].isSet
        bg_frame_number = -1
        clear_background_isSet = self.camerainfo['clear_background'].isSet
        clear_background_clear = self.camerainfo['clear_background'].clear
        take_background_isSet = self.camerainfo['take_background'].isSet
        take_background_clear = self.camerainfo['take_background'].clear
        #dynamic_background_isSet = self.camerainfo['dynamic_background'].isSet


        max_frame_size = FastImage.Size(self.max_width, self.max_height)

        lbrt = self.realtime_analyzer.roi
        left,bottom,right,top=lbrt
        hw_roi_w = right-left+1
        hw_roi_h = top-bottom+1
        cur_roi_l = left
        cur_roi_b = bottom
        #cur_roi_l, cur_roi_b,hw_roi_w, hw_roi_h  = self.camera.get_frame_roi()
        cur_fisize = FastImage.Size(hw_roi_w, hw_roi_h)

        bg_changed = True
        use_roi2 = True
        fi8ufactory = FastImage.FastImage8u

#        imgROI = fi8ufactory( cur_fisize )
#        self._imgROI = imgROI # make accessible to other code
        old_ts = rospy.Time.now().to_sec()
        old_fn = None
        points = []

        #FastImage.set_debug(3) # let us see any images malloced, should only happen on hardware ROI size change


        #################### initialize images ############

        running_mean8u_im_full = self.realtime_analyzer.get_image_view('mean') # this is a view we write into
        absdiff8u_im_full = self.realtime_analyzer.get_image_view('absdiff') # this is a view we write into

        mask_im = self.realtime_analyzer.get_image_view('mask') # this is a view we write into
        newmask_fi = FastImage.asfastimage( self.mask_image )
        newmask_fi.get_8u_copy_put(mask_im, max_frame_size)

        # allocate images and initialize if necessary

        imgRunningMean_full = FastImage.FastImage32f(max_frame_size)
        self._imgRunningMean_full = imgRunningMean_full # make accessible to other code

        fastframef32_tmp_full = FastImage.FastImage32f(max_frame_size)

        mean2_full = FastImage.FastImage32f(max_frame_size)
        self._mean2_full = mean2_full # make accessible to other code
        std2_full = FastImage.FastImage32f(max_frame_size)
        self._std2_full = std2_full # make accessible to other code
        running_stdframe_full = FastImage.FastImage32f(max_frame_size)
        self._running_stdframe_full = running_stdframe_full # make accessible to other code
        compareframe_full = FastImage.FastImage32f(max_frame_size)
        compareframe8u_full = self.realtime_analyzer.get_image_view('cmp') # this is a view we write into
        self._compareframe8u_full = compareframe8u_full

        running_sumsqf_full = FastImage.FastImage32f(max_frame_size)
        running_sumsqf_full.set_val(0,max_frame_size)
        self._running_sumsqf_full = running_sumsqf_full # make accessible to other code

        noisy_pixels_mask_full = FastImage.FastImage8u(max_frame_size)
        mean_duration_no_bg = 0.0053 # starting value
        mean_duration_bg = 0.020 # starting value

        # set ROI views of full-frame images
        running_mean8u_im = running_mean8u_im_full.roi(cur_roi_l, cur_roi_b, cur_fisize) # set ROI view
        imgRunningMean = imgRunningMean_full.roi(cur_roi_l, cur_roi_b, cur_fisize)  # set ROI view
        fastframef32_tmp = fastframef32_tmp_full.roi(cur_roi_l, cur_roi_b, cur_fisize)  # set ROI view
        mean2 = mean2_full.roi(cur_roi_l, cur_roi_b, cur_fisize)  # set ROI view
        std2 = std2_full.roi(cur_roi_l, cur_roi_b, cur_fisize)  # set ROI view
        running_stdframe = running_stdframe_full.roi(cur_roi_l, cur_roi_b, cur_fisize)  # set ROI view
        compareframe = compareframe_full.roi(cur_roi_l, cur_roi_b, cur_fisize)  # set ROI view
        compareframe8u = compareframe8u_full.roi(cur_roi_l, cur_roi_b, cur_fisize)  # set ROI view
        running_sumsqf = running_sumsqf_full.roi(cur_roi_l, cur_roi_b, cur_fisize)  # set ROI view
        noisy_pixels_mask = noisy_pixels_mask_full.roi(cur_roi_l, cur_roi_b, cur_fisize)  # set ROI view

        if self._initial_image_dict is not None:
            # If we have initial values, load them.

            # implicit conversion to float32
            numpy.asarray(imgRunningMean_full)[:,:] = self._initial_image_dict['mean']
            numpy.asarray(running_sumsqf)[:,:] = self._initial_image_dict['sumsqf']

            if 1:
                rospy.logwarn('WARNING: ignoring initial images and taking new background.')
                self.camerainfo['take_background'].set()

        else:
            self.camerainfo['take_background'].set()

        imgRunningMean.get_8u_copy_put( running_mean8u_im, cur_fisize )

        #################### done initializing images ############

        incoming_raw_frames_queue = self.camerainfo['incoming_raw_frames']
        incoming_raw_frames_queue_put = incoming_raw_frames_queue.put

        initial_take_bg_state = None

        while 1:

            with camnode_utils.use_buffer_from_chain(self._chain) as chainbuf:
                if chainbuf.quit_now:
                    break
                chainbuf.updated_running_mean_image = None
                chainbuf.updated_running_sumsqf_image = None

                imgROI = chainbuf.get_buf()
                cam_received_time = chainbuf.cam_received_time

                with self.lockParameters:
                    if self.parameters['use_color_filter']:
                        if self.parameters['color_filter_1'] < self.parameters['color_filter_2']:
    
                            camnode_colors.replace_with_red_image( imgROI,
                                                                   chainbuf.image_coding,
                                                                   #camnode_colors.RED_CHANNEL)
                                                                   camnode_colors.RED_COLOR,
                                                                   self.parameters['color_filter_1'],
                                                                   self.parameters['color_filter_2'],
                                                                   self.parameters['color_filter_3'],
                                                                   self.parameters['color_filter_sat'])
                        else:
                            rospy.logwarn('ERROR: color_filter_2 >= color_filter_1 -- skipping')

                # Get best guess as to when image was taken
                timestamp=chainbuf.timestamp
                framenumber=chainbuf.framenumber

                # Publish raw image on ROS network
                now = rospy.Time.now().to_sec()
                if now-self.lasttime+0.005 > 1./(self.rosrate):
                    # Create and publish an image_raw message.
                    imageRaw = Image()
                    imageRaw.header.seq=framenumber
                    imageRaw.header.stamp=rospy.Time.now() # XXX TODO: once camera trigger is ROS node, get accurate timestamp
                    imageRaw.header.frame_id = "Camera%d" % self.iCamera

                    npimgROI = np.array(imgROI)
                    (height,width) = npimgROI.shape

                    imageRaw.height = height
                    imageRaw.width = width
                    imageRaw.encoding = chainbuf.image_coding
                    pixel_format = chainbuf.image_coding
                    if pixel_format == 'MONO8':
                        imageRaw.encoding = 'mono8'
                    elif pixel_format in ('RAW8:RGGB','MONO8:RGGB'):
                        imageRaw.encoding = 'bayer_rggb8'
                    elif pixel_format in ('RAW8:BGGR','MONO8:BGGR'):
                        imageRaw.encoding = 'bayer_bggr8'
                    elif pixel_format in ('RAW8:GBRG','MONO8:GBRG'):
                        imageRaw.encoding = 'bayer_gbrg8'
                    elif pixel_format in ('RAW8:GRBG','MONO8:GRBG'):
                        imageRaw.encoding = 'bayer_grbg8'
                    else:
                        raise ValueError('unknown pixel format "%s"'%pixel_format)

                    imageRaw.step = width
                    imageRaw.data = npimgROI.tostring() # let numpy convert to string

                    self.pubImageRaw.publish(imageRaw)
                    
                    # Create and publish a camera_info message.
                    camera_info = CameraInfo()
                    camera_info.header = imageRaw.header
                    camera_info.height = imageRaw.height
                    camera_info.width = imageRaw.width
                    camera_info.distortion_model = 'plumb_bob'
                    camera_info.D = [0, 0, 0, 0, 0]
                    camera_info.K = [1,0,0, 0,1,0, 0,0,1]
                    camera_info.R = [1,0,0, 0,1,0, 0,0,1]
                    camera_info.P = [1,0,0,0, 0,1,0,0, 0,0,1,0]
                    self.pubCameraInfo.publish(camera_info)
                    
                    self.lasttime = now

                if 1:
                    if old_fn is None:
                        # no old frame
                        old_fn = framenumber-1
                    if framenumber-old_fn > 1:
                        n_frames_skipped = framenumber-old_fn-1
                        msg = '  frames apparently skipped: %d'%(n_frames_skipped,)
                        self.log_message_queue.put((self.idCamera,rospy.Time.now().to_sec(),msg))
                        rospy.logerr(msg)
                    else:
                        n_frames_skipped = 0

                    diff = timestamp-old_ts
                    time_per_frame = diff/(n_frames_skipped+1)
                    if not disable_ifi_warning:
                        if time_per_frame > 2*self.shortest_IFI:
                            msg = 'Warning: IFI is %f on %s at %s (frame skipped?)'%(time_per_frame, self.idCamera, time.asctime())
                            self.log_message_queue.put((self.idCamera,rospy.Time.now().to_sec(),msg))
                            rospy.logerr(msg)

                old_ts = timestamp
                old_fn = framenumber

                #rospy.logwarn('erode value=%d'% self.parameters['n_erode'])
                with self.lockParameters:
                    xpoints = self.realtime_analyzer.do_work(imgROI,
                                                             timestamp, 
                                                             framenumber, 
                                                             use_roi2,
                                                             self.parameters['use_cmp'],
                                                             max_duration_sec=self.shortest_IFI-0.0005, # give .5 msec for other processing
                                                             return_debug_values=1,
                                                             n_erode_absdiff=int(self.parameters['n_erode']))
                    
                ## if len(xpoints)>=self.max_num_points:
                ##     msg = 'Warning: cannot save acquire points this frame because maximum number already acheived'
                ##     rospy.logerr(msg)
                chainbuf.processed_points = xpoints
                if NAUGHTY_BUT_FAST:
                    chainbuf.absdiff8u_im_full = absdiff8u_im_full
                    chainbuf.mean8u_im_full = running_mean8u_im_full
                    chainbuf.compareframe8u_full = compareframe8u_full
                else:
                    chainbuf.absdiff8u_im_full = numpy.array(absdiff8u_im_full,copy=True)
                    chainbuf.mean8u_im_full = numpy.array(running_mean8u_im_full,copy=True)
                    chainbuf.compareframe8u_full = numpy.array(compareframe8u_full,copy=True)
                points = self._convert_to_wire_order( xpoints, imgROI, imgRunningMean, running_sumsqf)

                # allow other thread to see images
                imname = self.camerainfo['export_image_name'] # figure out what is wanted # XXX theoretically could have threading issue
                if imname == 'raw':
                    export_image = imgROI
                else:
                    export_image = self.realtime_analyzer.get_image_view(imname) # get image
                self.camerainfo['most_recent_frame_potentially_corrupt'] = (0,0), export_image # give view of image, receiver must be careful

                if 1:
                    # allow other thread to see raw image always (for saving)
                    if incoming_raw_frames_queue.qsize() >1000:
                        # chop off some old frames to prevent memory explosion
                        rospy.logwarn('ERROR: Deleting old frames to make room for new ones! (and sleeping)')
                        for i in range(100):
                            incoming_raw_frames_queue.get_nowait()
                    incoming_raw_frames_queue_put(
                        (imgROI.get_8u_copy(imgROI.size), # save a copy
                         timestamp,
                         framenumber,
                         points,
                         self.realtime_analyzer.roi,
                         cam_received_time,
                         ) )
                    #rospy.logwarn(' '*20+'put frame')

                do_bg_maint = False

                if initial_take_bg_state is not None:
                    assert initial_take_bg_state == 'gather'
                    n_initial_take = 5
                    if 1:
                        initial_take_frames.append( numpy.array(imgROI,copy=True) )
                        if len( initial_take_frames ) >= n_initial_take:

                            initial_take_frames = numpy.array( initial_take_frames, dtype=numpy.float32 )
                            mean_frame = numpy.mean( initial_take_frames, axis=0)
                            sumsqf_frame = numpy.sum(initial_take_frames**2, axis=0)/len( initial_take_frames )

                            numpy.asarray(imgRunningMean)[:,:] = mean_frame
                            numpy.asarray(running_sumsqf)[:,:] = sumsqf_frame
                            rospy.logwarn('Using slow method, calculated mean and sumsqf frames from first %d frames'%(n_initial_take,))

                            # we're done with initial transient, set stuff
                            do_bg_maint = True
                            initial_take_bg_state = None
                            del initial_take_frames
                    elif 0:
                        # faster approach (currently seems broken)

                        # accummulate sum

                        # I could re-write this to use IPP instead of
                        # numpy, but would that really matter much?
                        npy_view =  numpy.asarray(imgROI)
                        numpy.asarray(imgRunningMean)[:,:] = numpy.asarray(imgRunningMean) +  npy_view
                        numpy.asarray(running_sumsqf)[:,:]  = numpy.asarray(running_sumsqf)  +  npy_view.astype(numpy.float32)**2
                        initial_take_frames_done += 1
                        del npy_view

                        if initial_take_frames_done >= n_initial_take:

                            # now divide to take average
                            numpy.asarray(imgRunningMean)[:,:] = numpy.asarray(imgRunningMean) / initial_take_frames_done
                            numpy.asarray(running_sumsqf)[:,:]  = numpy.asarray(running_sumsqf) / initial_take_frames_done

                            # we're done with initial transient, set stuff
                            do_bg_maint = True
                            initial_take_bg_state = None
                            del initial_take_frames_done

                if take_background_isSet():
                    rospy.logwarn('Taking new bg')
                    # reset background image with current frame as mean and 0 STD
                    if cur_fisize != max_frame_size:
                        rospy.logwarn(cur_fisize)
                        rospy.logwarn(max_frame_size)
                        rospy.logwarn('ERROR: Can only take background image if not using ROI')
                    else:
                        if 0:
                            # old way
                            imgROI.get_32f_copy_put(running_sumsqf,max_frame_size)
                            running_sumsqf.toself_square(max_frame_size)

                            imgROI.get_32f_copy_put(imgRunningMean,cur_fisize)
                            imgRunningMean.get_8u_copy_put( running_mean8u_im, max_frame_size )
                            do_bg_maint = True
                        else:
                            initial_take_bg_state = 'gather'
                            if 1:
                                initial_take_frames = [ numpy.array(imgROI,copy=True) ] # for slow approach
                            elif 0:

                                initial_take_frames_done = 1 # for faster approach

                                # set imgRunningMean
                                imgROI.get_32f_copy_put(imgRunningMean,cur_fisize)
                                imgRunningMean.get_8u_copy_put( running_mean8u_im, max_frame_size )

                                # set running_sumsqf
                                imgROI.get_32f_copy_put(running_sumsqf,max_frame_size)
                                running_sumsqf.toself_square(max_frame_size)

                    take_background_clear()

                if self.parameters['dynamic_background']:#dynamic_background_isSet():
                    bg_frame_number += 1
                    if (bg_frame_number % self.bg_frame_interval == 0):
                        do_bg_maint = True

                if do_bg_maint:
                    with self.lockParameters:
                        realtime_image_analysis.do_bg_maint(
                        #rospy.logwarn('Doing slow bg maint, frame %d' % chainbuf.framenumber)
                        #tmpresult = motmot.realtime_image_analysis.slow.do_bg_maint(
                            imgRunningMean,#in
                            imgROI,#in
                            cur_fisize,#in
                            self.bg_frame_alpha, #in
                            running_mean8u_im,
                            fastframef32_tmp,
                            running_sumsqf, #in
                            mean2,
                            std2,
                            running_stdframe,
                            self.parameters['n_sigma'],#in
                            compareframe8u,
                            bright_non_gaussian_cutoff,#in
                            noisy_pixels_mask,#in
                            bright_non_gaussian_replacement,#in
                            bench=0 )
                            #debug=0)
                    #chainbuf.real_std_est= tmpresult
                    bg_changed = True
                    bg_frame_number = 0

                if self.options.debug_std:
                    if framenumber % 200 == 0:
                        mean_std = numpy.mean( numpy.mean( numpy.array(running_stdframe,dtype=numpy.float32 )))
                        rospy.logwarn('%s mean STD %.2f'%(self.idCamera, mean_std))

                if clear_background_isSet():
                    # reset background image with 0 mean and 0 STD
                    imgRunningMean.set_val( 0, max_frame_size )
                    running_mean8u_im.set_val(0, max_frame_size )
                    running_sumsqf.set_val( 0, max_frame_size )
                    compareframe8u.set_val(0, max_frame_size )
                    bg_changed = True
                    clear_background_clear()

                if bg_changed:
                    chainbuf.updated_running_mean_image = numpy.array( imgRunningMean, copy=True )
                    chainbuf.updated_running_sumsqf_image = numpy.array( running_sumsqf, copy=True )
                    bg_changed = False

                with self.lockParameters:
                    self.realtime_analyzer.diff_threshold = self.parameters['threshold_diff']
                    self.realtime_analyzer.clear_threshold = self.parameters['threshold_clear']

                # XXX could speed this with a join operation I think
                data = struct.pack('<ddliI',
                                   timestamp, 
                                   cam_received_time,
                                   framenumber,
                                   len(points),
                                   n_frames_skipped)
                
                for point_tuple in points:
                    try:
                        data = data + struct.pack(pt_fmt, *point_tuple)
                    except:
                        rospy.logwarn('Error-causing data: %s'%point_tuple)
                        raise
                if 0:
                    local_processing_time = (rospy.Time.now().to_sec() - cam_received_time)*1e3
                    rospy.logwarn('local_processing_time %3.1fms' % local_processing_time)
                    
                self.mainbrain.send_coordinates(self.idCamera, data)

                if DEBUG_DROP:
                    debug_fd.write('%d,%d\n'%(framenumber,len(points)))
                #rospy.logwarn('Sent data...')

                if 0 and self.new_roi.isSet():
                    with self.new_roi_data_lock:
                        lbrt = self.new_roi_data
                        self.new_roi_data = None
                        self.new_roi.clear()
                    left,bottom,right,top=lbrt
                    width = right-left+1
                    height = top-bottom+1
                    self.realtime_analyzer.roi = lbrt
                    rospy.logwarn('Desired left,bottom,width,height=%s'%[left,bottom,width,height])

                    l2,b2,w2,h2 = self.camera.get_frame_roi()
                    if ((left==l2) and (bottom==b2) and (width==w2) and (height==h2)):
                        rospy.logwarn('Current ROI matches desired ROI - not changing')
                    else:
                        self.camera.set_frame_roi(left,bottom,width,height)
                        left,bottom,width,height = self.camera.get_frame_roi()
                        rospy.logwarn('Actual left,bottom,width,height=%s'%[left,bottom,width,height])
                    right = left+width-1
                    top = bottom+height-1
                    cur_fisize = FastImage.Size(width, height)
                    imgROI = fi8ufactory( cur_fisize )
                    self.realtime_analyzer.roi = (left,bottom,right,top)

                    # set ROI views of full-frame images
                    running_mean8u_im = running_mean8u_im_full.roi(left, bottom, cur_fisize) # set ROI view
                    imgRunningMean = imgRunningMean_full.roi(left, bottom, cur_fisize)  # set ROI view
                    fastframef32_tmp = fastframef32_tmp_full.roi(left, bottom, cur_fisize)  # set ROI view
                    mean2 = mean2_full.roi(left, bottom, cur_fisize)  # set ROI view
                    std2 = std2_full.roi(left, bottom, cur_fisize)  # set ROI view
                    running_stdframe = running_stdframe_full.roi(left, bottom, cur_fisize)  # set ROI view
                    compareframe = compareframe_full.roi(left, bottom, cur_fisize)  # set ROI view
                    compareframe8u = compareframe8u_full.roi(left, bottom, cur_fisize)
                    running_sumsqf = running_sumsqf_full.roi(left, bottom, cur_fisize)  # set ROI view
                    noisy_pixels_mask = noisy_pixels_mask_full.roi(left, bottom, cur_fisize)  # set ROI view


class FakeProcessCamData(object):
    def __init__(self, idCamera=None):
        self._chain = camnode_utils.ChainLink()
        self._idCamera = idCamera
    def get_chain(self):
        return self._chain
    def mainloop(self):
        while 1:
            with camnode_utils.use_buffer_from_chain(self._chain) as buf:
                #stdout_write('P')
                buf.processed_points = [ (10,20) ]


###############################################################################
# Savers: SaveFMF, SaveUFMF 
###############################################################################

class SaveFMF(object):
    def __init__(self, 
                 idCamera=None, 
                 quit_event=None):
        self._chain = camnode_utils.ChainLink()
        self._idCamera = idCamera
        self.cmd = Queue.Queue()
        
    def get_chain(self):
        return self._chain
    
    def start_recording(self, filenamebaseFMF = None):
        """threadsafe"""
        self.cmd.put( ('save', filenamebaseFMF) )

    def stop_recording(self, *args, **kw):
        """threadsafe"""
        self.cmd.put( ('stop',) )

    def mainloop(self):
        # Note: need to accummulate frames into queue and add with .add_frames() for speed
        # Also: old version uses fmf version 1. Not sure why.

        raw = []
        meancmp = []

        state = 'pass'

        last_bgcmp_image_timestamp = None
        last_running_mean_image = None
        last_running_sumsqf_image = None

        image_coding = None

        while True:

            # 1: process commands
            while True:
                if self.cmd.empty():
                    break
                cmd = self.cmd.get()
                
                if cmd[0] == 'save':
                    rospy.logwarn('Saving .fmf'+'-'*50)

                    filenamebaseFMF = cmd[1]
                    full_raw = filenamebaseFMF + '.fmf'
                    full_bg = filenamebaseFMF + '_mean.fmf'
                    full_std = filenamebaseFMF + '_sumsqf.fmf'
                    movieFmfRaw = FlyMovieFormat.FlyMovieSaver(full_raw,
                                                             format=image_coding,
                                                             bits_per_pixel=8,
                                                             version=3)
                    if image_coding.startswith('MONO8:'):
                        tmp_coding = 'MONO32f:' + image_coding[6:]
                    else:
                        if image_coding != 'MONO8':
                            print >> sys.stderr, ('WARNING: Unknown image '
                                                  'coding %s for .fmf files'%(
                                image_coding,))
                        tmp_coding = 'MONO32f'
                    movieFmfBg = FlyMovieFormat.FlyMovieSaver(full_bg,
                                                            format=tmp_coding,
                                                            bits_per_pixel=32,
                                                            version=3)
                    movieFmfStd = FlyMovieFormat.FlyMovieSaver(full_std,
                                                             format='MONO32f', # std is monochrome
                                                             bits_per_pixel=32,
                                                             version=3)
                    del tmp_coding
                    state = 'saving'

                    if last_bgcmp_image_timestamp is not None:
                        movieFmfBg.add_frame(FastImage.asfastimage(last_running_mean_image),
                                           last_bgcmp_image_timestamp,
                                           error_if_not_fast=True)
                        movieFmfStd.add_frame(FastImage.asfastimage(last_running_sumsqf_image),
                                            last_bgcmp_image_timestamp,
                                            error_if_not_fast=True)
                    else:
                        print 'WARNING: Could not save initial bg and std frames'

                elif cmd[0] == 'stop':
                    print '-'*20,'Done saving .fmf','-'*30
                    movieFmfRaw.close()
                    movieFmfBg.close()
                    movieFmfStd.close()
                    state = 'pass'

            # 2: block for image data
            with camnode_utils.use_buffer_from_chain(self._chain) as chainbuf: # must do on every frame
                if chainbuf.quit_now:
                    break

                if image_coding is None:
                    image_coding = chainbuf.image_coding

                if chainbuf.updated_running_mean_image is not None:
                    # Always keep the current bg and std images so
                    # that we can save them when starting a new .fmf
                    # movie save sequence.
                    last_bgcmp_image_timestamp = chainbuf.cam_received_time
                    # Keeping references to these images should be OK,
                    # not need to copy - the Process thread already
                    # made a copy of the realtime analyzer's internal
                    # copy.
                    last_running_mean_image = chainbuf.updated_running_mean_image
                    last_running_sumsqf_image = chainbuf.updated_running_sumsqf_image

                if state == 'saving':
                    raw.append( (numpy.array(chainbuf.get_buf(), copy=True),
                                 chainbuf.cam_received_time) )
                    if chainbuf.updated_running_mean_image is not None:
                        meancmp.append( (chainbuf.updated_running_mean_image,
                                         chainbuf.updated_running_sumsqf_image,
                                         chainbuf.cam_received_time)) # these were copied in process thread

            # 3: grab any more that are here
            try:
                with camnode_utils.use_buffer_from_chain(self._chain,blocking=False) as chainbuf:
                    if chainbuf.quit_now:
                        break

                    if chainbuf.updated_running_mean_image is not None:
                        # Always keep the current bg and std images so
                        # that we can save them when starting a new .fmf
                        # movie save sequence.
                        last_bgcmp_image_timestamp = chainbuf.cam_received_time
                        # Keeping references to these images should be OK,
                        # not need to copy - the Process thread already
                        # made a copy of the realtime analyzer's internal
                        # copy.
                        last_running_mean_image = chainbuf.updated_running_mean_image
                        last_running_sumsqf_image = chainbuf.updated_running_sumsqf_image

                    if state == 'saving':
                        raw.append( (numpy.array(chainbuf.get_buf(), copy=True),
                                     chainbuf.cam_received_time) )
                        if chainbuf.updated_running_mean_image is not None:
                            meancmp.append( (chainbuf.updated_running_mean_image,
                                             chainbuf.updated_running_sumsqf_image,
                                             chainbuf.cam_received_time)) # these were copied in process thread
            except Queue.Empty:
                pass

            # 4: actually save the data
            #   TODO: switch to add_frames() method which doesn't acquire GIL after each frame.
            if state == 'saving':
                for frame,timestamp in raw:
                    print "add_frame Raw..."
                    movieFmfRaw.add_frame(FastImage.asfastimage(frame),timestamp,error_if_not_fast=True)
                for running_mean,running_sumsqf,timestamp in meancmp:
                    print "add_frame Bg/Std..."
                    movieFmfBg.add_frame(FastImage.asfastimage(running_mean),timestamp,error_if_not_fast=True)
                    movieFmfStd.add_frame(FastImage.asfastimage(running_sumsqf),timestamp,error_if_not_fast=True)
            del raw[:]
            del meancmp[:]


class SaveUFMF(object):
    def __init__(self,
                 idCamera=None,
                 options = None,
                 mkdir_lock = None):
        
        self.options = options
        self._chain = camnode_utils.ChainLink()
        self._idCamera = idCamera
        self.cmd = Queue.Queue()
        self.movieUfmf = None
        if mkdir_lock is not None:
            self._mkdir_lock = mkdir_lock
        else:
            self._mkdir_lock = threading.Lock()

    def get_chain(self):
        return self._chain
    
    def start_recording(self, filenamebaseUFMF=None):
        """threadsafe"""
        self.cmd.put( ('save',filenamebaseUFMF))

    def stop_recording(self,*args,**kw):
        """threadsafe"""
        self.cmd.put( ('stop',) )

    def mainloop(self):
        # Note: need to accummulate frames into queue and add with .add_frames() for speed
        # Also: old version uses fmf version 1. Not sure why.

        meancmp = []

        state = 'pass'

        last_bgcmp_image_timestamp = None
        last_running_mean_image = None
        last_running_sumsqf_image = None

        while True:

            while True:
                if self.cmd.empty():
                    break
                cmd = self.cmd.get()
                
                if cmd[0] == 'save':
                    print 'Saving .ufmf','-'*50
                    filename_base = cmd[1]
                    filenamebaseFMF = os.path.expanduser(filename_base)
                    state = 'saving'
                elif cmd[0] == 'stop':
                    print '-'*20,'Done saving .ufmf','-'*30
                    if self.movieUfmf is not None:
                        self.movieUfmf.close()
                        self.movieUfmf = None
                    state = 'pass'

            # block for images
            with camnode_utils.use_buffer_from_chain(self._chain) as chainbuf:
                if chainbuf.quit_now:
                    break

                if chainbuf.updated_running_mean_image is not None:
                    # Always keep the current bg and std images so
                    # that we can save them when starting a new .fmf
                    # movie save sequence.
                    last_bgcmp_image_timestamp = chainbuf.cam_received_time
                    # Keeping references to these images should be OK,
                    # not need to copy - the Process thread already
                    # made a copy of the realtime analyzer's internal
                    # copy.
                    last_running_mean_image = chainbuf.updated_running_mean_image
                    last_running_sumsqf_image = chainbuf.updated_running_sumsqf_image

                if state == 'saving':
                    if chainbuf.updated_running_mean_image is not None:
                        meancmp.append( (chainbuf.updated_running_mean_image,
                                         chainbuf.updated_running_sumsqf_image,
                                         chainbuf.cam_received_time)) # these were copied in process thread
                    if self.movieUfmf is None:
                        filename_base = os.path.abspath(os.path.expanduser(filename_base))
                        dirname = os.path.split(filename_base)[0]

                        with self._mkdir_lock:
                            # Because this is a multi-threaded
                            # program, sometimes another thread will
                            # try to create this directory.
                            if not os.path.exists(dirname):
                                os.makedirs(dirname)
                        filename = filename_base + '.ufmf'
                        print 'saving to',filename
                        if chainbuf.image_coding.startswith('MONO8'):
                            height,width=numpy.array(chainbuf.get_buf(), copy=False).shape
                        else:
                            raise NotImplementedError(
                                'unable to determine shape from image with '
                                'coding %s'%(chainbuf.image_coding,))
                        self.movieUfmf = ufmf.AutoShrinkUfmfSaverV3( filename,
                                                                 coding = chainbuf.image_coding,
                                                                 max_width=width,
                                                                 max_height=height,
                                                                 )
                        del height,width


                        if last_running_mean_image is not None:
                            print "movieUfmf.add_keyframe..."
                            self.movieUfmf.add_keyframe('mean',
                                                    last_running_mean_image,
                                                    last_bgcmp_image_timestamp)
                            self.movieUfmf.add_keyframe('sumsq',
                                                    last_running_sumsqf_image,
                                                    last_bgcmp_image_timestamp)

                    self._tobuf( chainbuf )

            # grab any more that are here
            try:
                with camnode_utils.use_buffer_from_chain(self._chain,blocking=False) as chainbuf:
                    if chainbuf.quit_now:
                        break

                    if chainbuf.updated_running_mean_image is not None:
                        # Always keep the current bg and std images so
                        # that we can save them when starting a new .fmf
                        # movie save sequence.
                        last_bgcmp_image_timestamp = chainbuf.cam_received_time
                        # Keeping references to these images should be OK,
                        # not need to copy - the Process thread already
                        # made a copy of the realtime analyzer's internal
                        # copy.
                        last_running_mean_image = chainbuf.updated_running_mean_image
                        last_running_sumsqf_image = chainbuf.updated_running_sumsqf_image

                    if state == 'saving':
                        self._tobuf( chainbuf ) # actually save the .ufmf data
                        if chainbuf.updated_running_mean_image is not None:
                            meancmp.append( (chainbuf.updated_running_mean_image,
                                             chainbuf.updated_running_sumsqf_image,
                                             chainbuf.cam_received_time)) # these were copied in process thread
            except Queue.Empty:
                pass

            # actually save the data
            #   TODO: switch to add_frames() method which doesn't acquire GIL after each frame.
            if state == 'saving':
                for running_mean,running_sumsqf,timestamp in meancmp:
                    self.movieUfmf.add_keyframe('mean',running_mean,timestamp)
                    self.movieUfmf.add_keyframe('sumsq',running_sumsqf,timestamp)
            del meancmp[:]

    def _tobuf( self, chainbuf ):
        frame = chainbuf.get_buf()
        if 0:
            print 'saving %d points'%(len(chainbuf.processed_points ),)
        pts = []
        wh = self.options.small_save_radius*2
        for pt in chainbuf.processed_points:
            pts.append( (pt[0],pt[1],wh,wh) )
        self.movieUfmf.add_frame( frame, chainbuf.cam_received_time, pts )



###############################################################################
# ImageSources
###############################################################################

class ImageSource(threading.Thread):
    """One instance of this class for each camera. Do nothing but get
    new frames, copy them, and pass to listener chain."""
    def __init__(self,
                 chain=None,
                 camera=None,
                 buffer_pool=None,
                 debug_acquire = False,
                 iCamera = None,
                 quit_event = None,
                 ):

        threading.Thread.__init__(self,name='ImageSource')
        self._chain = chain
        self.camera = camera
        with self.camera._hack_acquire_lock():
            self.image_coding = self.camera.get_pixel_coding()
        self.buffer_pool = buffer_pool
        self.debug_acquire = debug_acquire
        self.iCamera_str = str(iCamera)
        self.quit_event = quit_event
        self.idCamera = '<unassigned>'
        
        
    def assign_idCamera(self,idCamera):
        self.idCamera = idCamera
        
        
    def set_chain(self,new_chain):
        # XXX TODO FIXME: put self._chain behind lock
        if self._chain is not None:
            raise NotImplementedError('replacing a processing chain not implemented')
        self._chain = new_chain
        
        
    def get_buffer_pool(self):
        return self.buffer_pool
    
    
    def run(self):
        print 'ImageSource running in process',os.getpid()
        buffer_pool = self.buffer_pool
        cam_quit_event_isSet = self.quit_event.isSet
        while not cam_quit_event_isSet():
            self._block_until_ready() # no-op for realtime camera processing
            if buffer_pool.get_num_outstanding_buffers() > 100:
                # Grab some frames (wait) until the number of
                # outstanding buffers decreases -- give processing
                # threads time to catch up.            camera._monkeypatched_lock = threading.Lock()

                print ('*'*80+'\n')*5
                print 'ERROR: We seem to be leaking buffers - will not acquire more images for a while!'
                print ('*'*80+'\n')*5
                while 1:
                    self._grab_buffer_quick()
                    if buffer_pool.get_num_outstanding_buffers() < 10:
                        print 'Resuming normal image acquisition'
                        break

            # this gets a new (unused) buffer from the preallocated pool
            with get_free_buffer_from_pool( buffer_pool ) as chainbuf:
                chainbuf.quit_now = False

                _bufim = chainbuf.get_buf()

                try_again_condition, timestamp, framenumber = self._grab_into_buffer( _bufim )
                if try_again_condition:
                    continue

                if self.debug_acquire:
                    stdout_write(self.iCamera_str)

                cam_received_time = rospy.Time.now().to_sec()

                chainbuf.cam_received_time = cam_received_time
                chainbuf.timestamp = timestamp
                chainbuf.framenumber = framenumber
                chainbuf.image_coding = self.image_coding

                # Now we get rid of the frame from this thread by passing
                # it to processing threads. The last one of these will
                # return the buffer to buffer_pool when done.
                if self._chain is not None:

                    # Setting this gives responsibility to the last
                    # chain to call
                    # "buffer_pool.return_buffer(chainbuf)" when
                    # done. This is acheived automatically by the
                    # context manager in use_buffer_from_chain() and
                    # the ChainLink.end_buf() method which returns the
                    # buffer when the last link in the chain is done.
                    chainbuf._i_promise_to_return_buffer_to_the_pool = True

                    self._chain.fire( chainbuf ) # the end of the chain will call return_buffer()
        # now, we are quitting, so fire one last event through the chain to signal quit
        with get_free_buffer_from_pool( buffer_pool ) as chainbuf:
            chainbuf.quit_now = True

            # see above for this stuff
            if self._chain is not None:
                chainbuf._i_promise_to_return_buffer_to_the_pool = True
                self._chain.fire( chainbuf )



class ImageSourceControllerBase(object):
    pass

class ImageSourceFromCamera(ImageSource):
    def __init__(self,*args,**kwargs):
        ImageSource.__init__(self,*args,**kwargs)
        self._prosilica_hack_last_framenumber = None
        self._prosilica_hack_framenumber_offset = 0

    def _block_until_ready(self):
        # no-op for realtime camera processing
        pass

    def spawn_controller(self):
        controller = ImageSourceControllerBase()
        return controller

    def _grab_buffer_quick(self):
        try:
            with self.camera._hack_acquire_lock():
                trash = self.camera.grab_next_frame_blocking()
        except cam_iface.BuffersOverflowed:
            msg = 'ERROR: Buffers overflowed on %s at %s'%(self.iCamera_str,time.asctime(time.localtime(now)))
            self.log_message_queue.put((self.iCamera_str,now,msg))
            print >> sys.stderr, msg
        except cam_iface.FrameDataMissing:
            pass
        except cam_iface.FrameDataCorrupt:
            pass
        except cam_iface.FrameSystemCallInterruption:
            pass

    def _grab_into_buffer(self, _bufim ):
        try_again_condition= False

        with self.camera._hack_acquire_lock():
            # transfer thread ownership into this thread. (This is a
            # semi-evil hack into camera class... Should call a method
            # like self.camera.acquire_thread())
            # self.camera.mythread=threading.currentThread()

            try:
                self.camera.grab_next_frame_into_buf_blocking(_bufim)
            except cam_iface.BuffersOverflowed:
                if self.debug_acquire:
                    stdout_write('(O%s)'%self.iCamera_str)
                now = rospy.Time.now().to_sec()
                msg = 'ERROR: Buffers overflowed on %s at %s'%(self.iCamera_str,time.asctime(time.localtime(now)))
                self.log_message_queue.put((self.iCamera_str,now,msg))
                print >> sys.stderr, msg
                try_again_condition = True
            except cam_iface.FrameDataMissing:
                if self.debug_acquire:
                    stdout_write('(M%s)'%self.iCamera_str)
                now = rospy.Time.now().to_sec()
                msg = 'Warning: frame data missing on %s at %s'%(self.iCamera_str,time.asctime(time.localtime(now)))
                #self.log_message_queue.put((self.iCamera_str,now,msg))
                print >> sys.stderr, msg
                try_again_condition = True
            except cam_iface.FrameDataCorrupt:
                if self.debug_acquire:
                    stdout_write('(C%s)'%self.iCamera_str)
                now = rospy.Time.now().to_sec()
                msg = 'Warning: frame data corrupt on %s at %s'%(self.iCamera_str,time.asctime(time.localtime(now)))
                #self.log_message_queue.put((self.iCamera_str,now,msg))
                print >> sys.stderr, msg
                try_again_condition = True
            except (cam_iface.FrameSystemCallInterruption, cam_iface.NoFrameReturned):
                if self.debug_acquire:
                    stdout_write('(S%s)'%self.iCamera_str)
                try_again_condition = True

            if not try_again_condition:
                # get best guess as to when image was taken
                timestamp=self.camera.get_last_timestamp()
                framenumber=self.camera.get_last_framenumber()

                # Hack to deal with Prosilica framenumber resetting at
                # 65535 (even though it's an unsigned long).

                _prosilica_hack_max_skipped_frames = 100
                if ((framenumber<=_prosilica_hack_max_skipped_frames) and
                    (self._prosilica_hack_last_framenumber >= 65536-_prosilica_hack_max_skipped_frames) and
                    (self._prosilica_hack_last_framenumber < 65536)):
                    # We're dealing with a Prosilica camera which just
                    # rolled over.
                    self._prosilica_hack_framenumber_offset += 65636
                self._prosilica_hack_last_framenumber = framenumber
                framenumber += self._prosilica_hack_framenumber_offset
            else:
                timestamp = framenumber = None
        return try_again_condition, timestamp, framenumber

class ImageSourceFakeCamera(ImageSource):

    # XXX TODO: I should actually just incorporate all the fake cam
    # stuff in this class. There doesn't seem to be much point in
    # having a separate fake cam class. On the other hand, the fake
    # cam gets called by another thread, so the separation would be
    # less clear about what is running in which thread.

    def __init__(self,*args,**kw):
        self._do_step = threading.Event()
        self._fake_cam = kw['camera']
        self._buffer_pool = None
        self._count = 0
        super( ImageSourceFakeCamera, self).__init__(*args,**kw)

    def _block_until_ready(self):
        while 1:
            if self.quit_event.isSet():
                return

            if self._count==0:
                self._tstart = rospy.Time.now().to_sec()
            elif self._count>=1000:
                tstop = rospy.Time.now().to_sec()
                dur = tstop-self._tstart
                fps = self._count/dur
                print 'fps: %.1f'%(fps,)

                # prepare for next
                self._tstart = tstop
                self._count = 0
            self._count += 1

            # This lock ping-pongs execution back and forth between
            # "acquire" and process.

            self._do_step.wait(0.01) # timeout
            if self._do_step.isSet():
                self._do_step.clear()
                return
            if self._buffer_pool is not None:
                r=self._buffer_pool.get_num_outstanding_buffers()
                self._do_step.set()

    def register_buffer_pool( self, buffer_pool ):
        assert self._buffer_pool is None,'buffer pool may only be set once'
        self._buffer_pool = buffer_pool

    def spawn_controller(self):
        class ImageSourceFakeCameraController(ImageSourceControllerBase):
            def __init__(self, do_step=None, fake_cam=None, quit_event=None):
                self._do_step = do_step
                self._fake_cam = fake_cam
                self._quit_event = quit_event
            def trigger_single_frame_start(self):
                self._do_step.set()
            def set_to_frame_0(self):
                self._fake_cam.set_to_frame_0()
            def is_finished(self):
                #print 'self._fake_cam.is_finished()',self._fake_cam.is_finished()
                return self._fake_cam.is_finished()
            def quit_now(self):
                self._quit_event.set()
            def get_n_frames(self):
                return self._fake_cam.get_n_frames()
        controller = ImageSourceFakeCameraController(self._do_step,
                                                     self._fake_cam,
                                                     self.quit_event)
        return controller

    def _grab_buffer_quick(self):
        time.sleep(0.05)

    def _grab_into_buffer(self, _bufim ):
        with self.camera._hack_acquire_lock():
            self.camera.grab_next_frame_into_buf_blocking(_bufim, self.quit_event)

            try_again_condition = False
            timestamp=self.camera.get_last_timestamp()
            framenumber=self.camera.get_last_framenumber()
        return try_again_condition, timestamp, framenumber


###############################################################################
# Fake Cameras
###############################################################################

class FakeCamera(object):
    def start_camera(self):
        # no-op
        pass

    def get_framerate(self):
        return 123456

    def get_num_camera_properties(self):
        return 0

    def get_trigger_mode_number(self):
        return 0

    def get_max_height(self):
        left,bottom,width,height = self.get_frame_roi()
        return height

    def get_max_width(self):
        left,bottom,width,height = self.get_frame_roi()
        return width

    def get_pixel_coding(self):
        return 'UNKNOWN'
    
    def close(self):
        return

    def get_num_trigger_modes(self):
        return 1

    def get_trigger_mode_string(self,i):
        return 'fake camera trigger'

class FakeCameraFromNetwork(FakeCamera):
    def __init__(self,id,frame_size):
        self.id = id
        self.frame_size = frame_size
        self.proxyRemote = None
        Pyro.core.initClient(banner=0)
        self._hack_acquire_lock = threading.Lock


    def get_frame_roi(self):
        width,height = self.frame_size
        return 0,0,width,height

    def _ensure_remote(self):
        if self.proxyRemote is None:
            hostname = 'localhost'
            port = rospy.get_param(rospy.get_name()+'/port_camnode_emulated_camera_control', 9645)
            name = 'remote_camera_source'
            uriRemote = "PYROLOC://%s:%d/%s" % (hostname, port, name)
            self.proxyRemote = Pyro.core.getProxyForURI(uriRemote)

    def grab_next_frame_into_buf_blocking(self,buf, quit_event):
        # XXX TODO: implement quit_event checking
        self._ensure_remote()

        pt_list = self.proxyRemote.get_point_list(self.id) # this will block...
        width,height = self.frame_size
        new_raw = np.asarray( buf )
        assert new_raw.shape == (height,width)
        for pt in pt_list:
            x,y = pt
            xi = int(round(x))
            yi = int(round(y))
            new_raw[yi,xi] = 10
        return new_raw

    def get_last_timestamp(self):
        self._ensure_remote()
        return self.proxyRemote.get_last_timestamp(self.id) # this will block...

    def get_last_framenumber(self):
        self._ensure_remote()
        return self.proxyRemote.get_last_framenumber(self.id) # this will block...

class FakeCameraFromRNG(FakeCamera):
    def __init__(self,id,frame_size):
        self.id = id
        self.frame_size = frame_size
        self.proxyRemote = None
        self.last_timestamp = 0.0
        self.last_count = -1
        self._hack_acquire_lock = threading.Lock

    def get_pixel_coding(self):
        return 'MONO8'

    def get_frame_roi(self):
        width,height=self.frame_size
        return 0,0,width,height

    def grab_next_frame_into_buf_blocking(self,buf, quit_event):
        # XXX TODO: implement quit_event checking
        width,height = self.frame_size
        new_raw = np.asarray( buf )
        assert new_raw.shape == (height,width)
        self.last_timestamp = rospy.Time.now().to_sec()
        self.last_count += 1
        for pt_num in range( np.random.randint(5) ):
            x,y = np.random.uniform(0.0,1.0,size=(2,))
            xi = int(round(x*(width-1)))
            yi = int(round(y*(height-1)))
            new_raw[yi,xi] = 10
        return new_raw

    def get_last_timestamp(self):
        return self.last_timestamp

    def get_last_framenumber(self):
        return self.last_count

class FakeCameraFromFMF(FakeCamera):

    def __init__(self,filename):
        self.fmf_recarray = FlyMovieFormat.mmap_flymovie( filename )
        if 0:
            print 'short!'
            self.fmf_recarray = self.fmf_recarray[:600]

        self._n_frames = len(self.fmf_recarray)
        self._curframe = SharedValue1(0)
        self._frame_offset = 0
        self._hack_acquire_lock = threading.Lock


    def get_n_frames(self):
        return self._n_frames

    def get_frame_roi(self):
        height,width = self.fmf_recarray['frame'][0].shape
        return 0,0,width,height

    def grab_next_frame_into_buf_blocking(self, buf, quit_event):
        buf = numpy.asarray( buf )
        curframe = self._curframe.get()
        while self.is_finished():
            if quit_event.isSet():
                return
            # We're being asked to go off the end here...
            # wait until we get told to return to beginning.
            time.sleep(0.05)
            curframe = self._curframe.get()
        buf[:,:] = self.fmf_recarray['frame'][ curframe ]
        self._last_timestamp = self.fmf_recarray['timestamp'][ curframe ]
        self._last_framenumber = curframe + self._frame_offset
        self._curframe.set( curframe + 1 )

    def get_last_timestamp(self):
        return self._last_timestamp

    def get_last_framenumber(self):
        return self._last_framenumber

    def set_to_frame_0(self):
        self._frame_offset += self._curframe.get()
        self._curframe.set( 0 )

    def is_finished(self):
        # this can is called by any thread
        #print "len( self.fmf_recarray['frame'] )",len( self.fmf_recarray['frame'] )
        #print "self._curframe.get()",self._curframe.get()
        result = self._curframe.get() >= len( self.fmf_recarray['frame'] )
        #print result
        #print
        return result

def create_cam_for_emulation_image_source( filename_or_pseudofilename ):
    """factory function to create fake camera and ImageSourceModel"""
    fname = filename_or_pseudofilename
    if fname.endswith('.fmf'):
        camera = FakeCameraFromFMF(fname)
        ImageSourceModel = ImageSourceFakeCamera

        mean_filename = os.path.splitext(fname)[0] + '_mean' + '.fmf'
        sumsqf_filename = os.path.splitext(fname)[0] + '_sumsqf' + '.fmf'

        fmf_ra = FlyMovieFormat.mmap_flymovie( fname )
        mean_ra =  FlyMovieFormat.mmap_flymovie( mean_filename )
        sumsqf_ra = FlyMovieFormat.mmap_flymovie( sumsqf_filename ) # not really mean2 (actually running_sumsqf)

        t0 = fmf_ra['timestamp'][0]
        mean_t0 = mean_ra['timestamp'][0]
        sumsqf_t0 = sumsqf_ra['timestamp'][0]

        if not ((t0 >= mean_t0) and (t0 >= sumsqf_t0)):
            print '*'*80
            print 'WARNING timestamps of first image frame is not before mean image timestamps. they are'
            print ' raw .fmf: %s'%repr(t0)
            print ' mean .fmf:  %s'%repr(mean_t0)
            print ' sumsqf .fmf: %s'%repr(sumsqf_t0)
            print '*'*80

        initial_image_dict = {'mean':mean_ra['frame'][0],
                              'sumsqf':sumsqf_ra['frame'][0],  # not really mean2 (actually running_sumsqf)
                              'raw':fmf_ra['frame'][0]}
        if 0 and len( mean_ra['frame'] ) > 1:
            print ("No current support for reading back multi-frame "
                   "background/cmp. (But this should not be necessary, "
                   "as you can reconstruct them, anyway.)")

    elif fname.endswith('.ufmf'):
        raise NotImplementedError('Patience, young grasshopper')
    elif fname.startswith('<net') and fname.endswith('>'):
        args = fname[4:-1].strip()
        args = args.split()
        port, width, height = map(int, args)
        camera = FakeCameraFromNetwork(port,(width,height))
        ImageSourceModel = ImageSourceFakeCamera
        with camera._hack_acquire_lock():
            left,bottom,width,height = camera.get_frame_roi()
            del left,bottom

        imgMean = np.ones( (height,width), dtype=np.uint8 )
        imgSumSq = np.ones( (height,width), dtype=np.uint8 )
        imgRaw = np.ones( (height,width), dtype=np.uint8 )

        initial_image_dict = {'mean':imgMean,
                              'sumsqf':imgSumSq,
                              'raw':imgRaw}
    elif fname == '<rng>':
        width, height = 640, 480
        camera = FakeCameraFromRNG('fakecam1',(width,height))
        ImageSourceModel = ImageSourceFakeCamera
        with camera._hack_acquire_lock():
            left,bottom,width,height = camera.get_frame_roi()

        imgMean = np.ones( (height,width), dtype=np.uint8 )
        imgSumSq = np.ones( (height,width), dtype=np.uint8 )
        imgRaw = np.ones( (height,width), dtype=np.uint8 )

        initial_image_dict = {'mean':imgMean,
                              'sumsqf':imgSumSq,
                              'raw':imgRaw}
    else:
        raise ValueError('Could not create emulation image source')
    return camera, ImageSourceModel, initial_image_dict


###############################################################################
# App classes: ConsoleApp, AppState, 
###############################################################################

class ConsoleApp(object):
    def __init__(self, call_often=None):
        self.call_often = call_often
        self.exit_value = 0
        self.quit_now = False
    def MainLoop(self):
        while not self.quit_now:
            time.sleep(0.05)
            self.call_often()
        if self.exit_value != 0:
            sys.exit(self.exit_value)
    def OnQuit(self, exit_value=0):
        self.quit_now = True
        self.exit_value = exit_value

    def generate_view(self, model, controller ):
        if hasattr(controller, 'trigger_single_frame_start' ):
            rospy.logwarn('No control in ConsoleApp for %s'%controller)
            controller.trigger_single_frame_start()

class AppState(object):
    """This class handles all camera states, properties, etc."""
    def __init__(self,
                 benchmark = False,
                 options = None):
        global cam_iface

        self.benchmark = benchmark
        self.options = options
        self._real_quit_function = None
        
        self.mainbrain = MainbrainInterface(use_ros=USE_ROS)
        self.log_message_queue = Queue.Queue()


        # Get the source of the images, i.e. from files, from simulation, or from the cameras. 
        self.filespecImageSources = self.options.emulation_image_sources
        if self.filespecImageSources is not None:                                       # Command-line specified image sources, i.e. emulation.
            self.sourceImages = 'Emulation'
            self.filespecImageSources = self.filespecImageSources.split( os.pathsep )
            nCameras = len( self.filespecImageSources )
        elif self.options.simulate_point_extraction is not None:                        # Command-line specified simulation. 
            self.sourceImages = 'Simulation'
            self.filespecImageSources = self.options.simulate_point_extraction.split( os.pathsep )
            nCameras = len( self.filespecImageSources )
        elif self.benchmark:                                                            # Command-line specified to benchmark. 
            self.sourceImages = 'Benchmark'
            nCameras = 1
        else:                                                                           # None of the above.  Use the cameras.
            self.sourceImages = 'Cameras'
            cam_iface = cam_iface_choose.import_backend( self.options.backend, self.options.wrapper )
            self.cam_order = self.GetCameraOrder()
            nCameras = len(self.cam_order)

        if nCameras == 0:
            raise RuntimeError('No cameras detected')

        self.cameras = [None]*nCameras
        self.statusCamera = [None]*nCameras
        self.chains = [None]*nCameras
        self.processors = [None]*nCameras
        self.saversFMF = [None]*nCameras
        self.saversUFMF = [None]*nCameras
        self.camerainfolist = [None]*nCameras
        self.idCameras = [None]*nCameras
        self._image_sources = [None]*nCameras
        self._image_controllers = [None]*nCameras
        self.initial_images = [None]*nCameras
        self.critical_threads = []
        self.lockEchoTimestamp = threading.Lock()
        self.statusRecordingPrev = False

        for iCamera in range(nCameras):
            self.InitializeCameraVars(iCamera)


        # Check version of Mainbrain.
        if not self.options.ignore_version:
            versionMainbrain = self.mainbrain.get_version()
            assert versionMainbrain == flydra.version.__version__



        # Get the filenames of the mask images.
        filespecMaskImages = self.options.mask_images
        if filespecMaskImages is not None:
            filespecMaskImages = filespecMaskImages.split( os.pathsep )


        lock_save_ufmf_data_mkdir = threading.Lock()

        if self.options.force_cam_ids is not None:
            idCameras_list = self.options.force_cam_ids.split(',')
        else:
            idCameras_list = None


        for iCamera in range(nCameras):
            camera = self.cameras[iCamera]
            with camera._hack_acquire_lock():
                # Get the image mask.
                if filespecMaskImages is not None:
                    filespecMaskImage = filespecMaskImages[iCamera]
                    mask = self.MaskFromFile(filespecMaskImage)
                else:
                    left,top,width,height = camera.get_frame_roi()
                    mask = numpy.zeros((height,width), dtype=numpy.uint8)

                
                # Get various camera parameters.
                self.LoadParameters()
                scalar_control_info = self.GetScalarControlInfo(camera, self.camerainfolist[iCamera], self.options)
                if idCameras_list is None:
                    idCamera = socket.getfqdn() + str(iCamera)
                else:
                    idCamera = idCameras_list[iCamera]

                idCamera = string.replace(idCamera,'/','_')  # Make a valid ROS name.
                
                
                # Offer the EchoTimestamp service.
                if ((not self.benchmark) or (not FLYDRA_BT)) and (USE_ROS):
                    rospy.Service (idCamera+'/echo_timestamp', SrvEchoTimestamp, self.callback_echo_timestamp)

                # Register cameras with Mainbrain
                print "Registering camera %d, %s" % (iCamera, idCamera)
                idCamera = self.mainbrain.register_camera(iCamera=iCamera,
                                                          scalar_control_info=scalar_control_info,
                                                          idCamera=idCamera)
                self.idCameras[iCamera] = idCamera
                self._image_sources[iCamera].assign_idCamera(idCamera)


                ##################################################################
                # Spawn threads for ProcessCam(), SaveFMF(), and SaveUFMF().
                ##################################################################
                if not DISABLE_ALL_PROCESSING:
                    if 0:
                        self.processors[iCamera] = FakeProcessCamData()
                    else:
                        camera.get_max_height()
                        left,bottom,width,height = camera.get_frame_roi()
                        right = left+width-1
                        top = bottom+height-1
                        lbrt = left,bottom,right,top
                        
                        self.processors[iCamera] = ProcessCamData(
                            idCamera = idCamera,
                            log_message_queue = self.log_message_queue,
                            max_num_points = self.options.num_points,
                            roi2_radius = self.options.software_roi_radius,
                            bg_frame_interval = self.options.background_frame_interval,
                            bg_frame_alpha = self.options.background_frame_alpha,
                            iCamera = iCamera,
                            mask_image = mask,
                            framerate = None,
                            lbrt = lbrt,
                            max_height = camera.get_max_height(),
                            max_width = camera.get_max_width(),
                            camerainfo = self.camerainfolist[iCamera],
                            options = self.options,
                            initial_image_dict = self.initial_images[iCamera],
                            benchmark = self.benchmark,
                            mainbrain = self.mainbrain,
                            )

                    # Spawn a thread for the camera processor.
                    chainCamera = self.processors[iCamera].get_chain()
                    self.chains[iCamera] = chainCamera
                    thread = threading.Thread(target=self.processors[iCamera].mainloop,
                                              name='processor_%s'%idCamera)
                    thread.setDaemon(True)
                    thread.start()
                    self.critical_threads.append(thread)

                    
                    # Spawn a thread for full video frames.
                    if 1:
                        self.saversFMF[iCamera]= SaveFMF()
                        chainCamera.append_link(self.saversFMF[iCamera].get_chain())
                        thread = threading.Thread(target = self.saversFMF[iCamera].mainloop,
                                                  name = 'save_fmf_%s'%idCamera)
                        thread.setDaemon(True)
                        thread.start()
                        self.critical_threads.append(thread)
                    else:
                        print 'Not starting .fmf thread'


                    # Spawn a thread for small video frames.
                    if 1:
                        self.saversUFMF[iCamera] = SaveUFMF(options=self.options, mkdir_lock=lock_save_ufmf_data_mkdir)
                        chainCamera.append_link(self.saversUFMF[iCamera].get_chain())
                        thread = threading.Thread(target = self.saversUFMF[iCamera].mainloop,
                                                  name = 'save_ufmf_%s'%idCamera)
                        thread.setDaemon(True)
                        thread.start()
                        self.critical_threads.append( thread )
                    else:
                        print 'Not starting .ufmf thread'

                else:
                    chainCamera = None

                self._image_sources[iCamera].set_chain(chainCamera)


                ##################################################################
                # Log a message.
                ##################################################################
                if cam_iface is not None:
                    driver_string = 'Using cam_iface driver: %s (wrapper: %s)'%(cam_iface.get_driver_name(),
                                                                                cam_iface.get_wrapper_name())
                    self.log_message_queue.put((idCamera,
                                                rospy.Time.now().to_sec(),
                                                driver_string))
        # end, for iCamera in range(nCameras)
        
        self.srvDynReconf = Server(CamnodeConfig, self.callback_dynamic_reconfigure)
        
        self.last_frames_by_cam = [ [] for c in range(nCameras) ]
        self.last_points_by_cam = [ [] for c in range(nCameras) ]
        self.last_points_framenumbers_by_cam = [ [] for c in range(nCameras) ]
        self.n_raw_frames = [0 for i in range(nCameras)]
        self.last_measurement_time = [rospy.Time.now().to_sec() for i in range(nCameras)]
        self.last_return_info_check = [ 0.0 for i in range(nCameras)]


    # callback_dynamic_reconfigure()
    # Receive notifications of parameter changes from ROS dynamic_reconfigure.
    # Pass them along to the camera node and to the image processor. 
    # dynamic_reconfigure takes care of rospy.set_param, etc.
    #
    def callback_dynamic_reconfigure(self, config, level):
        #rospy.logwarn('config=%s'%config)

        # Until each camera has a node of its own, the parameters apply to all cameras.
        for iCamera in range(len(self.cameras)):
            CAM_CONTROLS = self.camerainfolist[iCamera]['cam_controls']
    
            for param,value in config.iteritems():
                # Set all the parameters into ROS.
                #rospy.set_param (rospy.get_name()+'/'+param, value)
    
    
                # Set parameters into camera.            
                if param in CAM_CONTROLS: # i.e. gain, shutter, brightness
                    #rospy.logwarn ('Setting camera %s=%s' % (param,value))
                    enum = CAM_CONTROLS[param]
                    with self.cameras[iCamera]._hack_acquire_lock():
                        self.cameras[iCamera].set_camera_property(enum,value,0)
                    
                # Set other parameters.            
                elif param == 'roi/left':
                    roi = self.processors[iCamera].roi
                    self.processors[iCamera].roi = (value, roi[1], roi[2], roi[3])
                    
                elif param == 'roi/top':
                    roi = self.processors[iCamera].roi
                    self.processors[iCamera].roi = (roi[0], value, roi[2], roi[3])
                    
                elif param == 'roi/right':
                    roi = self.processors[iCamera].roi
                    self.processors[iCamera].roi = (roi[0], roi[1], value, roi[3])
                    
                elif param == 'roi/bottom':
                    roi = self.processors[iCamera].roi
                    self.processors[iCamera].roi = (roi[0], roi[1], roi[2], value)
                    
                elif param == 'trigger_mode':
                    with self.cameras[iCamera]._hack_acquire_lock():
                        self.cameras[iCamera].set_trigger_mode_number(value)
                        
                elif param == 'use_cmp':
                    if value:
                        self.camerainfolist[iCamera]['use_cmp'].set()
                    else: 
                        self.camerainfolist[iCamera]['use_cmp'].clear()
                    
                elif param == 'framerate_trigger':
                    if value==0.0:
                        rospy.logwarn ('WARNING: framerate_trigger is set '
                               'to 0, but setting shortest IFI to 10 msec '
                               'anyway')
                        self.processors[iCamera].shortest_IFI = 0.01 # XXX TODO: FIXME: thread crossing bug
                    else:
                        self.processors[iCamera].shortest_IFI = 1.0/value # XXX TODO: FIXME: thread crossing bug
                        
                elif param == 'framerate_max':
                    if 0:
                        #print 'Ignoring request to set max_framerate'
                        pass
                    else:
                        with self.cameras[iCamera]._hack_acquire_lock():
                            self.cameras[iCamera].set_framerate(value)
                            
                elif param == 'dynamic_background':
                    if value: 
                        self.camerainfolist[iCamera]['dynamic_background'].set()
                    else: 
                        self.camerainfolist[iCamera]['dynamic_background'].clear()
                        
                elif param == 'use_color_filter':
                    self.processors[iCamera].use_color_filter = value
                    
                elif param == 'visible_image_view':
                    self.camerainfolist[iCamera]['export_image_name'] = value
    

        # Set the parameter values into the appstate, and into the image processors.
        self.SetParameters(config)
        for processorCamera in self.processors:
            processorCamera.SetParameters(config)

        
        return config


    def callback_echo_timestamp(self, srvreqEchoTimestamp):
        with self.lockEchoTimestamp:
            rv = {'time': rospy.Time.now().to_sec()}
             
        return rv 
    
    

    def InitializeCameraVars(self, iCamera):
        ##################################################################
        # Initialize "camerainfo" variables.
        ##################################################################
        rospy.logwarn ('Initializing camera %d' % iCamera)
        self.camerainfolist[iCamera] = {} # intialize

        self.camerainfolist[iCamera]['debug_drop']=self.options.debug_drop
        self.camerainfolist[iCamera]['debug_acquire']=self.options.debug_acquire
        self.camerainfolist[iCamera]['incoming_raw_frames']=Queue.Queue()
        self.camerainfolist[iCamera]['raw_fmf_and_bg_fmf']=None
        self.camerainfolist[iCamera]['most_recent_frame_potentially_corrupt']=None
        self.camerainfolist[iCamera]['saved_bg_frame']=False

        # control flow events for threading model
        self.camerainfolist[iCamera]['cam_quit_event'] = threading.Event()
        self.camerainfolist[iCamera]['listen_thread_done'] = threading.Event()
        self.camerainfolist[iCamera]['take_background'] = threading.Event()
        self.camerainfolist[iCamera]['clear_background'] = threading.Event()
        self.camerainfolist[iCamera]['dynamic_background'] = threading.Event()
        self.camerainfolist[iCamera]['dynamic_background'].set()
        self.camerainfolist[iCamera]['export_image_name'] = 'raw'
        self.camerainfolist[iCamera]['use_cmp'] = threading.Event()

        #rospy.logwarn('Not using ongoing variance estimate')
        self.camerainfolist[iCamera]['use_cmp'].set()

        if self.sourceImages == 'Cameras': # cam_iface is not None:
            #backend = cam_iface.get_driver_name()
            nModes = cam_iface.get_num_modes(self.cam_order[iCamera])
            mode = None
            if self.options.mode_num is not None:
                self.options.show_cam_details = True
                
            if self.options.show_cam_details:
                rospy.logwarn('Camera info: %s' % cam_iface.get_camera_info(self.cam_order[iCamera]))
                rospy.logwarn('%d available modes:' % nModes)
                
            for i in range(nModes):
                mode_string = cam_iface.get_mode_string(self.cam_order[iCamera],i)
                if self.options.show_cam_details:
                    rospy.logwarn('  mode %d: %s'%(i,mode_string))
                    
                if 'format7_0' in mode_string.lower():
                    # prefer format7_0
                    if mode is None:
                        mode = i
                        
            if mode is None:
                mode = 0
                
            if self.options.mode_num is not None:
                mode = self.options.mode_num

            cam_iface.Camera._hack_acquire_lock = monkeypatch_camera_method # add our monkeypatch
            camera = cam_iface.Camera(self.cam_order[iCamera], self.options.num_buffers, mode)
            camera._monkeypatched_lock = threading.Lock()

            if self.options.show_cam_details:
                rospy.logwarn('Using mode %d: %s'%(mode, cam_iface.get_mode_string(self.cam_order[iCamera],mode)))
            ImageSourceModel = ImageSourceFromCamera
            initial_image_dict = None

        elif self.sourceImages=='Simulation': #self.options.simulate_point_extraction:
            (camera, ImageSourceModel, initial_image_dict)  = create_cam_for_emulation_image_source(self.filespecImageSources[iCamera])
        
        elif self.sourceImages=='Benchmark': #self.benchmark: # emulate full images with random number generator
            (camera, ImageSourceModel, initial_image_dict) = create_cam_for_emulation_image_source('<rng>')
        
        else: #self.sourceImages=='Emulation': # emulate full images
            (camera, ImageSourceModel, initial_image_dict)  = create_cam_for_emulation_image_source(self.filespecImageSources[iCamera])


        if initial_image_dict is None:
            self.camerainfolist[iCamera]['take_background'].set()
        else:
            self.camerainfolist[iCamera]['take_background'].clear()

        self.initial_images[iCamera] = initial_image_dict

        self.cameras[iCamera] = camera
        if camera is not None:
            with camera._hack_acquire_lock():
                camera.start_camera()  # start camera
        self.statusCamera[iCamera]= 'started'
        if ImageSourceModel is not None:
            with camera._hack_acquire_lock():
                left,bottom,width,height = camera.get_frame_roi()
            buffer_pool = PreallocatedBufferPool(FastImage.Size(width,height))
            del left,bottom,width,height
            image_source = ImageSourceModel(chain = None,
                                            camera = camera,
                                            buffer_pool = buffer_pool,
                                            debug_acquire = self.options.debug_acquire,
                                            iCamera = iCamera,
                                            quit_event = self.camerainfolist[iCamera]['cam_quit_event'],
                                            )
            if self.benchmark: # should maybe be for any simulated camera in non-GUI mode?
                image_source.register_buffer_pool( buffer_pool )

            controller = image_source.spawn_controller()

            image_source.setDaemon(True)
            image_source.start()
            self._image_sources[iCamera] = image_source
            self._image_controllers[iCamera]= controller
        else:
            self._image_sources[iCamera] = None
            self._image_controllers[iCamera]= None


    def GetCameraOrder(self):
        all_cam_info_list = []
        for i in range(cam_iface.get_num_cameras()):
            try:
                this_info1 =  cam_iface.get_camera_info(i)
            except cam_iface.CameraNotAvailable:
                this_info2 =  ('(not available)', i)
            else:
                this_info2 =  ('\0'.join(this_info1), i)
            all_cam_info_list.append(this_info2)

        all_cam_info_list.sort() # make sure list is always in same order for given cameras
        all_cam_info_list.reverse() # any ordering will do, but reverse for historical reasons
        cam_order = [ x[1] for x in all_cam_info_list]
        del all_cam_info_list
        rospy.logwarn('Camera order'%cam_order)
        for i,iCamera in enumerate(cam_order):
            try:
                avail_string = cam_iface.get_camera_info(iCamera)
            except cam_iface.CameraNotAvailable:
                avail_string = '(not available)'
            rospy.logwarn('Order %d: %s'%(i, avail_string))

        cams_only = self.options.cams_only
        if cams_only is not None:
            cams_only = map(int,cams_only.split(','))

            new_cam_order = [ cam_order[i] for i in cams_only ]
            cam_order = new_cam_order
        
        return cam_order

    
    def MaskFromFile(self, filespecMaskImage):
        im = scipy.misc.pilutil.imread( filespecMaskImage )
        if len(im.shape) != 3:
            raise ValueError('mask image must have color channels')
        if im.shape[2] != 4:
            raise ValueError('mask image must have an alpha (4th) channel')
        alpha = im[:,:,3]
        if numpy.any((alpha > 0) & (alpha < 255)):
            rospy.logwarn('WARNING: some alpha values between 0 and '
                   '255 detected. Only zero and non-zero values are '
                   'considered.')
        #mask = alpha.astype(numpy.bool)
        mask = alpha.astype(numpy.uint8)*255
        
        return mask


    # LoadParameters()
    # Set the parameter values from the parameter server.
    def LoadParameters(self):    
        self.parameters = {}
        self.parameters['brightness'] = rospy.get_param(rospy.get_name()+'/brightness', 0)
        self.parameters['gain'] = rospy.get_param(rospy.get_name()+'/gain', 100)
        self.parameters['shutter'] = rospy.get_param(rospy.get_name()+'/shutter', 100)

    # SetParameters()
    # Set the parameter values from the argument, but only those that exist in both dicts.
    def SetParameters(self, config):
        for key in self.parameters:
            if key in config:
                self.parameters[key] = config[key]
        
    
    # GetScalarControlInfo()
    # Specifies the default values for the user controls in mainbrain.
    # The scalar_control_info stuff should be moved entirely out of mainbrain (and handled via the ROS parameter server).
    def GetScalarControlInfo(self, camera, camerainfo, options):
        scalar_control_info = {}    # This could really be called mainbrain_user_control_info

        # Get trigger modes.
        if 1:
            # trigger modes
            N_trigger_modes = camera.get_num_trigger_modes()
            if options.show_cam_details:
                rospy.logwarn('  %d available trigger modes:'%N_trigger_modes)
                for i in range(N_trigger_modes):
                    mode_string = camera.get_trigger_mode_string(i)
                    rospy.logwarn('  mode %d: %s'%(i,mode_string))
            scalar_control_info['N_trigger_modes'] = N_trigger_modes
            # XXX TODO: scalar_control_info['trigger_mode'] # current value

        camerainfo['cam_controls'] = {}
        

        # Get the list of properties, and set each to it's (current,min,max) values.
        nProperties = camera.get_num_camera_properties()
        listPropertyNames = []
        for iProperty in range(nProperties):
            # Get min/current/max property values
            propertyinfo = camera.get_camera_property_info(iProperty)  # propertyinfo contains 'name', 'min_value', 'max_value', 'has_manual_mode'
            current_value,auto = camera.get_camera_property( iProperty )
            new_value = current_value
            min_value = propertyinfo['min_value']
            max_value = propertyinfo['max_value']
            
            # If property is settable, then set it.
            force_manual = True
            if propertyinfo['has_manual_mode']:
                if force_manual or min_value <= new_value <= max_value:
                    try:
                        if options.show_cam_details:
                            rospy.logwarn('Setting camera property "%s" to manual mode'%(propertyinfo['name'],))
                        camera.set_camera_property( iProperty, new_value, 0 )
                    except:
                        rospy.logwarn('Error while setting property %s to %d (from %d)'%(propertyinfo['name'],new_value,current_value))
                        raise
                else:
                    if options.show_cam_details:
                        rospy.logwarn('Not setting property %s to %d (from %d) because out of range (%d<=value<=%d)'%(propertyinfo['name'],new_value,current_value,min_value,max_value))

                # Save the property number.
                camerainfo['cam_controls'][propertyinfo['name']]=iProperty
                
            # Put the property values into structure for mainbrain.                             
            scalar_control_info[propertyinfo['name']] = (current_value, min_value, max_value)
            # XXX FIXME: should transmit is_scaled_quantity info (scaled_unit_name, scale_gain, scale_offset)
            listPropertyNames.append( propertyinfo['name'] )

        scalar_control_info['camprops'] = listPropertyNames
        scalar_control_info['diff_threshold']  = rospy.get_param(rospy.get_name()+'/threshold_diff', 5)
        scalar_control_info['clear_threshold'] = rospy.get_param(rospy.get_name()+'/threshold_clear', 0.3)
        scalar_control_info['max_framerate']   = rospy.get_param(rospy.get_name()+'/framerate_max', 20)#camera.get_framerate()
        scalar_control_info['expected_trigger_framerate'] = rospy.get_param(rospy.get_name()+'/framerate_trigger', 20)#0.0
        scalar_control_info['visible_image_view'] = 'raw'
        scalar_control_info['collecting_background'] = rospy.get_param(rospy.get_name()+'/dynamic_background', True)
        scalar_control_info['n_sigma']         = rospy.get_param(rospy.get_name()+'/n_sigma', 7)
        scalar_control_info['n_erode_absdiff'] = rospy.get_param(rospy.get_name()+'/n_erode', 0)
        scalar_control_info['color_filter']    = rospy.get_param(rospy.get_name()+'/use_color_filter', False)
        scalar_control_info['color_range_1']   = rospy.get_param(rospy.get_name()+'/color_filter_1', 0)
        scalar_control_info['color_range_2']   = rospy.get_param(rospy.get_name()+'/color_filter_2', 150)
        scalar_control_info['color_range_3']   = rospy.get_param(rospy.get_name()+'/color_filter_3', 255)
        scalar_control_info['sat_thresh']      = rospy.get_param(rospy.get_name()+'/color_filter_sat', 100)
        #scalar_control_info['collecting_background']=camerainfo['dynamic_background'].isSet()
        
        #try:
        #    scalar_control_info['trigger_mode'] = camera.get_trigger_mode_number()
        #except cam_iface.CamIFaceError:
        #    scalar_control_info['trigger_mode'] = 0
        scalar_control_info['trigger_mode'] = rospy.get_param(rospy.get_name()+'/trigger_mode', 0)
        scalar_control_info['cmp'] = rospy.get_param(rospy.get_name()+'/use_cmp', False)#camerainfo['use_cmp'].isSet()
        
        
        #left,top,width,height = camera.get_frame_roi()
        left   = rospy.get_param(rospy.get_name()+'/roi/left', 0)
        top    = rospy.get_param(rospy.get_name()+'/roi/top', 0)
        right  = rospy.get_param(rospy.get_name()+'/roi/right', 1023)
        bottom = rospy.get_param(rospy.get_name()+'/roi/bottom', 767)
        #scalar_control_info['width'] = width
        #scalar_control_info['height'] = height
        scalar_control_info['roi'] = (left,top,right,bottom)
        
        scalar_control_info['debug_drop']=camerainfo['debug_drop']
        
        return scalar_control_info
            

    def get_image_sources(self):
        return self._image_sources

    def get_image_controllers(self):
        return self._image_controllers

    def quit_function(self,exit_value):
        for camerainfo in self.camerainfolist:
            camerainfo['cam_quit_event'].set()

        for thread in self.critical_threads:
            if thread.isAlive():
                thread.join(0.01)

        if self._real_quit_function is not None:
            self._real_quit_function(exit_value)

    def set_quit_function(self, quit_function=None):
        self._real_quit_function = quit_function

    def append_chain(self,
                     klass=None,
                     args=None,
                     basename=None,
                     kwargs=None,
                     kwargs_per_instance=None,
                     ):
        if basename is None:
            basename = 'appended thread'
        targets = {}
        for iCamera, (idCamera, chain) in enumerate(zip(self.idCameras,
                                                     self.chains)):
            base_kwargs = dict(idCamera=idCamera)

            if kwargs is not None:
                base_kwargs.update( kwargs )

            if kwargs_per_instance is not None:
                base_kwargs.update( kwargs_per_instance[ iCamera ] )

            if args is None:
                thread_instance = klass(**base_kwargs)
            else:
                thread_instance = klass(*args,**base_kwargs)

            chain.append_link( thread_instance.get_chain() )
            name = basename + ' ' + idCamera
            thread = threading.Thread( target = thread_instance.mainloop,
                                       name = name )
            thread.setDaemon(True)
            thread.start()
            self.critical_threads.append( thread )
            targets[idCamera] = thread_instance
        return targets

    def main_thread_task(self):
        """gets called often in mainloop of app"""
        try:
            # handle pyro function calls
            for iCamera, idCamera in enumerate(self.idCameras):
                if self.statusCamera[iCamera] == 'destroyed':
                    # ignore commands for closed cameras
                    continue
                try:
                    cmds = self.mainbrain.get_and_clear_commands(idCamera)
                except KeyError:
                    rospy.logwarn('Mainbrain appears to have lost cam_id %d' % idCamera)
                except Exception, x:
                    rospy.logerr('Remote traceback:'+'*'*30)
                    rospy.logerr(''.join(Pyro.util.getPyroTraceback(x)))
                    raise
                else:
                    self.handle_commands(iCamera,cmds)


            # Video recording.
            statusRecording = self.mainbrain.get_recording_status() # Not sure this ever gets set.
            if statusRecording==True:
                for iCamera, idCamera in enumerate(self.idCameras):
                    if self.saversUFMF[iCamera] is None:
                        rospy.logwarn('No .ufmf save thread for camera %d. Cannot save small movies' % iCamera)
                        continue

                    filenamebaseUFMF = time.strftime( 'CAM_NODE_MOV_%Y%m%d_%H%M%S_camid_' + repr(iCamera) + '.ufmf')
                    self.saversUFMF[iCamera].start_recording(filenamebaseUFMF=filenamebaseUFMF)
            elif self.statusRecordingPrev: # Only on transition True->False
                for iCamera, idCamera in enumerate(self.idCameras):
                    self.saversUFMF[iCamera].stop_recording()
            self.statusRecordingPrev = statusRecording
                


            # test if all closed
            all_closed = True
            for iCamera, idCamera in enumerate(self.idCameras):
                if self.statusCamera[iCamera] != 'destroyed':
                    all_closed = False
                    break

            # quit if no more cameras
            if all_closed:
                if self.quit_function is None:
                    raise RuntimeError('all cameras closed, but no quit_function set')
                self.quit_function(0)

            # if any threads have died, quit
            for thread in self.critical_threads:
                if not thread.isAlive():
                    rospy.logwarn('ERROR: Thread %s died unexpectedly. Quitting'%(thread.getName()))
                    self.quit_function(1)

            if not DISABLE_ALL_PROCESSING:
                for iCamera, idCamera in enumerate(self.idCameras):
                    last_frames = self.last_frames_by_cam[iCamera]
                    last_points = self.last_points_by_cam[iCamera]
                    last_points_framenumbers = self.last_points_framenumbers_by_cam[iCamera]

                    now = rospy.Time.now().to_sec() # roughly flydra_camera_node.py line 1504

                    # calculate and send FPS every 5 sec
                    elapsed = now-self.last_measurement_time[iCamera]
                    if elapsed > 5.0:
                        fps = self.n_raw_frames[iCamera]/elapsed
                        self.mainbrain.set_fps(idCamera,fps)
                        self.last_measurement_time[iCamera] = now
                        self.n_raw_frames[iCamera] = 0

                    # Get new raw frames from grab thread.
                    get_raw_frame = self.camerainfolist[iCamera]['incoming_raw_frames'].get_nowait
                    try:
                        while 1:
                            (frame,timestamp,framenumber,points,lbrt,cam_received_time) = get_raw_frame() # this may raise Queue.Empty
                            last_frames.append( (frame,timestamp,framenumber,points) ) # save for post-triggering
                            while len(last_frames)>200:
                                del last_frames[0]

                            last_points_framenumbers.append( framenumber ) # save for dropped packet recovery
                            last_points.append( (timestamp,points,cam_received_time) ) # save for dropped packet recovery
                            while len(last_points)>10000:
                                del last_points[:100]
                                del last_points_framenumbers[:100]

                            self.n_raw_frames[iCamera] += 1
                    except Queue.Empty:
                        pass

        except:
            traceback.print_exc()
            self.quit_function(1)

        
    # handle_commands()
    # Commands coming from mainbrain are handled here.        
    def handle_commands(self, iCamera, cmds):
        if cmds:
            #rospy.logwarn ('handle_commands %s: %s'%(self.idCameras[iCamera], cmds))
            CAM_CONTROLS = self.camerainfolist[iCamera]['cam_controls']

        paramRemap = {'diff_threshold': 'threshold_diff',
                      'clear_threshold': 'threshold_clear',
                      'n_erode_absdiff': 'n_erode',
                      'color_filter': 'use_color_filter',
                      'color_range_1': 'color_filter_1',
                      'color_range_2': 'color_filter_2',
                      'color_range_3': 'color_filter_3',
                      'sat_thresh': 'color_filter_sat',
                      'collecting_background': 'dynamic_background',
                      'max_framerate': 'framerate_max',
                      'expected_trigger_framerate': 'framerate_trigger',
                      'cmp': 'use_cmp'
                      }

        for cmd in cmds.keys():
            if cmd == 'set':    # Change a parameter value.
                # Remap old parameter names to new parameter names.
                params2 = {}
                for param,val in cmds['set'].iteritems():
                    if param in paramRemap:
                        p2 = paramRemap[param]
                    else:
                        p2 = param
                    if type(val)==numpy.float64: 
                        val=float(val)
                    params2[p2] = val

                # Send the params to ROS.
                self.srvDynReconf.update_configuration(params2) 
                    

            elif cmd == 'get_im':   # Send the image from the camprocessor to mainbrain. 
                val = self.camerainfolist[iCamera]['most_recent_frame_potentially_corrupt']
                if val is not None: # prevent race condition
                    leftbottom, im = val
                    #npim = np.array(im) # copy to native np form, not view of __array_struct__ form
                    npim = np.asarray(im) # view of __array_struct__ form
                    self.mainbrain.set_image(self.idCameras[iCamera], 
                                             (leftbottom, npim,))


            elif cmd == 'request_missing':
                camn_and_list = map(int,cmds[cmd].split())
                camn, framenumber_offset = camn_and_list[:2]
                missing_framenumbers = camn_and_list[2:]
                rospy.logwarn('Mainbrain wants %d frames (camn %d) at %s:'%(len(missing_framenumbers), camn, time.asctime()))
                if len(missing_framenumbers) > 200:
                    rospy.logwarn(str(missing_framenumbers[:25]) + ' + ... + ' + str(missing_framenumbers[-25:]))
                else:
                    rospy.logwarn(str(missing_framenumbers))

                last_points_framenumbers = self.last_points_framenumbers_by_cam[iCamera]
                last_points = self.last_points_by_cam[iCamera]

                # convert to numpy arrays for quick indexing
                last_points_framenumbers = numpy.array( last_points_framenumbers, dtype=numpy.int64 )
                missing_framenumbers = numpy.array( missing_framenumbers, dtype=numpy.int64 )

                # now find missing_framenumbers in last_points_framenumbers
                idxs = last_points_framenumbers.searchsorted( missing_framenumbers )

                missing_data = []
                still_missing = []
                for ii,(idx,missing_framenumber) in enumerate(zip(idxs,missing_framenumbers)):
                    if idx == 0:
                        # search sorted will sometimes return 0 when value not in range
                        found_framenumber = last_points_framenumbers[idx]
                        if found_framenumber != missing_framenumber:
                            still_missing.append( missing_framenumber )
                            continue
                    elif idx == len(last_points_framenumbers):
                        still_missing.append( missing_framenumber )
                        continue

                    timestamp, points, camn_received_time = last_points[idx]
                    # make sure data is pure python, (not numpy)
                    missing_data.append( (int(camn), int(missing_framenumber), float(timestamp),
                                          float(camn_received_time), points) )
                if len(missing_data):
                    self.mainbrain.receive_missing_data(cam_id=self.idCameras[iCamera], 
                                                        framenumber_offset=framenumber_offset, 
                                                        missing_data=missing_data)

                if len(still_missing):
                    rospy.logwarn('  Unable to find %d frames (camn %d):'%(len(still_missing), camn))
                    if len(still_missing) > 200:
                        rospy.logwarn(str(still_missing[:25]) + ' + ... + ' + str(still_missing[-25:]))
                    else:
                        rospy.logwarn(str(still_missing))

            elif cmd == 'quit':
                self._image_sources[iCamera].join(0.1)
                # XXX TODO: quit and join chain threads
                with self.cameras[iCamera]._hack_acquire_lock():
                    self.cameras[iCamera].close()
                self.statusCamera[iCamera] = 'destroyed'
                self.mainbrain.close_camera(self.idCameras[iCamera])
                
            elif cmd == 'take_bg':
                self.camerainfolist[iCamera]['take_background'].set()
                
            elif cmd == 'clear_bg':
                self.camerainfolist[iCamera]['clear_background'].set()

            elif cmd == 'start_recording':
                if self.saversFMF[iCamera] is None:
                    rospy.logwarn('No .fmf save thread -- cannot save movies')
                    continue

                filenamebaseFMF = cmds[cmd]
                filenamebaseFMF = os.path.expanduser(filenamebaseFMF)
                save_dir = os.path.split(filenamebaseFMF)[0]
                if not os.path.exists(save_dir):
                    rospy.logwarn('Making %s'%save_dir)
                    os.makedirs(save_dir)

                self.saversFMF[iCamera].start_recording(filenamebaseFMF = filenamebaseFMF)

            elif cmd == 'stop_recording':
                self.saversFMF[iCamera].stop_recording()

            elif cmd == 'start_small_recording':
                if self.saversUFMF[iCamera] is None:
                    rospy.logwarn('No .ufmf save thread -- cannot save small movies')
                    continue

                filenamebaseUFMF = cmds[cmd]
                filenamebaseUFMF = os.path.expanduser(filenamebaseUFMF)
                save_dir = os.path.split(filenamebaseUFMF)[0]
                if not os.path.exists(save_dir):
                    rospy.logwarn('Making %s'%save_dir)
                    os.makedirs(save_dir)

                self.saversUFMF[iCamera].start_recording(filenamebaseUFMF=filenamebaseUFMF)
                
            elif cmd == 'stop_small_recording':
                self.saversUFMF[iCamera].stop_recording()
                
            elif cmd == 'cal':
                rospy.logwarn('Setting calibration')
                pmat, intlin, intnonlin, scale_factor = cmds[cmd]

                # XXX TODO: FIXME: thread crossing bug
                # these three should always be done together in this order:
                self.processors[iCamera].set_scale_factor( scale_factor )
                self.processors[iCamera].set_pmat( pmat )
                self.processors[iCamera].make_reconstruct_helper(intlin, intnonlin) # let grab thread make one
            else:
                raise ValueError('Unknown cmd "%s"'%cmd)





###############################################################################
# Mainbrain's ROS interface.   
###############################################################################

# Thread to echo timestamps from mainbrain, to camera, back to mainbrain.
def ThreadEchoTimestamp(iCamera, camera):
    # Create timestamp sending socket.
    socketSendTimestamp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with gLockParams:
        portSendTimestamp = rospy.get_param('mainbrain/port_timestamp', 28993)

    # Offer a receiving socket for echo_timestamp from mainbrain:  localhost:28995,6,7,8,...
    socketReceiveTimestamp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    hostname = ''
    with gLockParams:
        if iCamera is None:
            portReceiveTimestamp = rospy.get_param('mainbrain/port_timestamp_camera', 28992) # One port per camnode.
        else:
            portReceiveTimestampBase = rospy.get_param('mainbrain/port_timestamp_camera_base', 28995) # One port per camera.
            portReceiveTimestamp = portReceiveTimestampBase + iCamera
    try:
        socketReceiveTimestamp.bind(( hostname, portReceiveTimestamp))
        rospy.logwarn('Created udp server (to receive timestamps) on port %s:%d' % (hostname, portReceiveTimestamp))
    except socket.error, err:
        if err.args[0]==98:
            with gLockParams:
                rospy.logwarn('EchoTimestamp for camera %d not available because port %d in use' % (iCamera, portReceiveTimestamp))


    
    with gLockParams:
        fmt = rospy.get_param('mainbrain/timestamp_echo_fmt1', '<d') #flydra.common_variables.timestamp_echo_fmt_diff
    
    while True:
        # Receive timestamp from mainbrain.
        try:
            packTimestamp, (orig_host,orig_port) = socketReceiveTimestamp.recvfrom(4096)
        except socket.error, err:
            if err.args[0] == errno.EINTR: # interrupted system call
                continue
            raise

        if struct is None: # this line prevents bizarre interpreter shutdown errors
            return


        # Send timestamp to camera & back.
        timeMainbrain = struct.unpack(fmt,packTimestamp)[0]
        timeCamera = camera['echo_timestamp'](time=timeMainbrain)
        
        # Send both times back to mainbrain.
        packTimestamp2 = packTimestamp + struct.pack( fmt, timeCamera)
        socketSendTimestamp.sendto(packTimestamp2, (orig_host, portSendTimestamp))
        
    

###############################################################################
# MainbrainInterface provides a class to abstract the camnode/mainbrain interface.
###############################################################################
class MainbrainInterface(object):
    def __init__(self, use_ros=False):
        self.use_ros = use_ros
        self.lockParameters = gLockParams
        
        if use_ros:
            self.AttachMainbrainRosInterface()
        else:
            self.AttachMainbrainSocketInterface()
            
            
    # Get the index of a camera (into self.cameras) from its idCamera.
    def ICameraFromId (self, idCamera):
        return self.idCameras_list.index(idCamera)
        
        
    def get_echo_time(self, time):
        return rospy.Time.now().to_sec()
        
    
        
    ###########################################################################
    # The ..._ros() functions wrap the service calls.

    # AttachMainbrainRosInterface()
    #   Note that the MainbrainRosInterface node must be running.
    #
    def AttachMainbrainRosInterface(self):
        self.send_coordinates_service_list = []
        self.idCameras_list = []
        
        stSrv = 'mainbrain/get_version'
        rospy.wait_for_service(stSrv)
        self.get_version_service = rospy.ServiceProxy(stSrv, SrvGetVersion)

        stSrv = 'mainbrain/register_camera'
        rospy.wait_for_service(stSrv)
        self.register_new_camera_service = rospy.ServiceProxy(stSrv, SrvRegisterCamera)

        stSrv = 'mainbrain/get_and_clear_commands'
        rospy.wait_for_service(stSrv)
        self.get_and_clear_commands_service = rospy.ServiceProxy(stSrv, SrvGetAndClearCommands)

        stSrv = 'mainbrain/set_fps'
        rospy.wait_for_service(stSrv)
        self.set_fps_service = rospy.ServiceProxy(stSrv, SrvSetFps)

        stSrv = 'mainbrain/set_image'
        rospy.wait_for_service(stSrv)
        self.set_image_service = rospy.ServiceProxy(stSrv, SrvSetImage)

        stSrv = 'mainbrain/log_message'
        rospy.wait_for_service(stSrv)
        self.log_message_service = rospy.ServiceProxy(stSrv, SrvLogMessage)

        stSrv = 'mainbrain/receive_missing_data'
        rospy.wait_for_service(stSrv)
        self.receive_missing_data_service = rospy.ServiceProxy(stSrv, SrvReceiveMissingData)

        stSrv = 'mainbrain/close_camera'
        rospy.wait_for_service(stSrv)
        self.close_camera_service = rospy.ServiceProxy(stSrv, SrvClose)

        stSrv = 'mainbrain/get_recording_status'
        rospy.wait_for_service(stSrv)
        self.get_recording_status_service = rospy.ServiceProxy(stSrv, SrvGetRecordingStatus)

        # Point to the appropriate functions.
        self.get_version            = self.get_version_ros
        self.register_camera        = self.register_camera_ros
        self.get_and_clear_commands = self.get_and_clear_commands_ros
        self.set_fps                = self.set_fps_ros
        self.set_image              = self.set_image_ros
        self.log_message            = self.log_message_ros
        self.receive_missing_data   = self.receive_missing_data_ros
        self.close_camera           = self.close_camera_ros
        self.get_recording_status   = self.get_recording_status_ros
        self.send_coordinates       = self.send_coordinates_ros
            

    def get_version_ros (self):
        response = self.get_version_service()
        return response.version

        
    def register_camera_ros (self, iCamera, scalar_control_info, idCamera):
        # Register the camera with mainbrain.
        response = self.register_new_camera_service(cam_no=iCamera,
                                                    pickled_scalar_control_info=pickle.dumps(scalar_control_info),
                                                    force_cam_id=idCamera)
        idCamera = response.cam_id
        self.idCameras_list.append(idCamera)
        assert(iCamera==len(self.idCameras_list)-1)
        

        # Each camera also needs to have a place to send its coordinates.
        stSrv = 'mainbrain/coordinates/'+idCamera
        rospy.wait_for_service(stSrv)
        rospy.logwarn('Camnode connected to service %s...' % stSrv)
        self.send_coordinates_service_list.append(rospy.ServiceProxy(stSrv, SrvCoordinates))
        
        return idCamera


    #def get_mainbrain_port_ros (self, idCamera):
    #    response = self.get_mainbrain_port_service(idCamera)
    #    return response.port}


    def get_and_clear_commands_ros (self, idCamera):
        response = self.get_and_clear_commands_service(idCamera)
        cmds = pickle.loads(response.pickled_cmds)
        return cmds
    
    
    def set_fps_ros (self, idCamera, fps):
        self.set_fps_service(idCamera, fps)
        return


    def set_image_ros (self, idCamera, (leftbottom, npim)):
        self.set_image_service(cam_id=idCamera, 
                                         pickled_coord_and_image=pickle.dumps((leftbottom, npim,)))
        return 


    def log_message_ros (self, idCamera, timestamp, message):
        self.log_message_service(idCamera, timestamp, message)
        return

    def receive_missing_data_ros (self, idCamera, framenumber_offset, missing_data):
        self.receive_missing_data_service(cam_id=idCamera, framenumber_offset=framenumber_offset, missing_data=missing_data)
        return


    def close_camera_ros (self, idCamera):
        self.close_camera_service(idCamera)
        return
    

    # Not sure that mainbrain uses this anymore.
    def get_recording_status_ros (self):
        response = self.get_recording_status_service()        
        return response.status

        
    def send_coordinates_ros(self, idCamera, data):
        iCamera = self.ICameraFromId(idCamera)
        response = self.send_coordinates_service_list[iCamera](idCamera, data)
        return



    ###########################################################################
    # The ..._socket() functions wrap the socket calls.
    # Most of these are just the self.proxyMainbrain versions, but a few need
    # special attention.

    def AttachMainbrainSocketInterface(self):
        Pyro.core.initClient(banner=0)

        ##################################################################
        # Connect to Mainbrain.

        self.idCameras_list = []
        self.portMainbrainCoordinates_list = []
        self.socket_coordinates_list = []
        self.threadEchoTime_list = []
        
        with self.lockParameters:
            self.protocol = rospy.get_param('mainbrain/network_protocol','udp')
            self.portMainbrain = rospy.get_param('mainbrain/port_mainbrain', 9833)
            self.nameMainbrain = rospy.get_param('mainbrain/hostname', 'main_brain')
            
        # Construct a URI to mainbrain.
        try:
            self.hostnameMainbrain = socket.gethostbyname(self.nameMainbrain)
        except:
            try:
                self.hostnameMainbrain = socket.gethostbyname(socket.gethostname()) # try localhost
            except: #socket.gaierror?
                self.hostnameMainbrain = ''
        uriMainbrain = "PYROLOC://%s:%d/%s" % (self.hostnameMainbrain, self.portMainbrain, self.nameMainbrain)


        # Connect to mainbrain.
        try:
            self.proxyMainbrain = Pyro.core.getProxyForURI(uriMainbrain)
        except:
            logerr('ERROR connecting to %s' % uriMainbrain)
            raise
        rospy.logwarn('Connected to %s' % uriMainbrain)
        self.proxyMainbrain._setOneway(['set_image','set_fps','close','log_message','receive_missing_data'])



        ##################################################################
        # Incoming connections to Camnode.

        # TriggerRecording service.
        self.timeRecord = rospy.Time.now().to_sec()
        self.isRecording = False
        hostnameLocal = ''
        portTriggerRecording = 30043 # arbitrary number
        self.socketTriggerRecording = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        try:
            self.socketTriggerRecording.bind((hostnameLocal, portTriggerRecording))
            rospy.logwarn('Created udp server (to receive recording triggers) on port %s:%d' % (hostnameLocal, portTriggerRecording))
        except socket.error, err:
            if err.args[0]==98: # port in use
                rospy.logwarn('Port %s:%d in use.  Cannot toggle recording state.' % (hostnameLocal, portTriggerRecording))
                self.socketTriggerRecording = None

        if self.socketTriggerRecording is not None:
            self.socketTriggerRecording.setblocking(0)

        if not USE_ONE_TIMEPORT_PER_CAMERA:
            # Launch only one thread to handle echo_timestamp.
            rospy.logwarn('Starting: %s...' % ('thread_timestamp'))
            self.threadEchoTime_list.append (threading.Thread(target=ThreadEchoTimestamp, name='thread_timestamp', args=(None, None,)))
            self.threadEchoTime_list[0].setDaemon(True) # quit that thread if it's the only one left...
            self.threadEchoTime_list[0].start()
            rospy.logwarn('Started thread %s' % ('thread_timestamp'))


        # Point to the socket-based versions of the API.
        self.get_version            = self.proxyMainbrain.get_version
        self.register_camera        = self.register_camera_socket
        self.get_and_clear_commands = self.proxyMainbrain.get_and_clear_commands
        self.set_fps                = self.proxyMainbrain.set_fps
        self.set_image              = self.proxyMainbrain.set_image
        self.log_message            = self.proxyMainbrain.log_message
        self.receive_missing_data   = self.proxyMainbrain.receive_missing_data
        self.close_camera           = self.proxyMainbrain.close
        self.get_recording_status   = self.get_recording_status_socket
        self.send_coordinates       = self.send_coordinates_socket


    def register_camera_socket (self, iCamera, scalar_control_info, idCamera):
        # Register the camera with mainbrain.
        with self.lockParameters:
            port = rospy.get_param('mainbrain/port_camera_base', 9834) + iCamera
            
        idCamera = self.proxyMainbrain.register_new_camera(cam_no=iCamera,
                                                           scalar_control_info=scalar_control_info,
                                                           port=port,
                                                           force_cam_id=idCamera)
        self.idCameras_list.append(idCamera)
        assert(iCamera==len(self.idCameras_list)-1)
        

        # Get mainbrain's coordinates port, once per each camera.
        self.portMainbrainCoordinates_list.append(self.proxyMainbrain.get_cam2mainbrain_port(idCamera))

        # Each camera also needs to have a place to send its coordinates.
        if self.protocol == 'udp':
            socketCoordinates = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        elif self.protocol == 'tcp':
            socketCoordinates = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            socketCoordinates.connect((self.hostnameMainbrain, self.portMainbrainCoordinates_list[iCamera]))
        else:
            raise ValueError('Unknown network_protocol %s' % self.protocol)

        self.socket_coordinates_list.append(socketCoordinates)


        if USE_ONE_TIMEPORT_PER_CAMERA:
            # Launch a thread to handle echo_timestamp, once per each camera.
            rospy.logwarn('Starting: %s...' % ('thread_timestamp_'+idCamera))
            camera = {'echo_timestamp': self.get_echo_time}
            self.threadEchoTime_list.append (threading.Thread(target=ThreadEchoTimestamp, name='thread_timestamp_'+idCamera, args=(iCamera, camera,)))
            self.threadEchoTime_list[iCamera].setDaemon(True) # quit that thread if it's the only one left...
            self.threadEchoTime_list[iCamera].start()
            rospy.logwarn('Started thread %s' % ('thread_timestamp_'+idCamera))
        
        return idCamera


    def send_coordinates_socket(self, idCamera, data):
        iCamera = self.ICameraFromId(idCamera)
        
            
        if self.protocol == 'udp':
            try:
                self.socket_coordinates_list[iCamera].sendto(data, (self.hostnameMainbrain, self.portMainbrainCoordinates_list[iCamera]))
            except socket.error, err:
                rospy.logwarn('WARNING: Ignoring error: %s' % err)
                
        elif self.protocol == 'tcp':
            self.socket_coordinates_list[iCamera].send(data)
        else:
            raise ValueError('Unknown network_protocol %s' % self.protocol)

        return
    
    
    def get_recording_status_socket (self):        
        msg = None
        if self.socketTriggerRecording is not None:
            try:
                msg, addr = self.socketTriggerRecording.recvfrom(4096) # Call mainbrain to get any trigger recording commands.
            except socket.error, err:
                if err.args[0] == 11: #Resource temporarily unavailable
                    pass
            finally:
                #rospy.logwarn(">>> %s <<< %s" % (msg, self.isRecording)) 
                if msg=='record_ufmf':
                    if self.isRecording==False:
                        self.isRecording = True
                        rospy.logwarn('Start saving video .ufmf')
                
                elif msg==None:
                    if (self.isRecording==True) and (rospy.Time.now().to_sec() - self.timeRecord >= 4): # Record at least 4 secs of video.
                        self.isRecording = False
                        rospy.logwarn('Stop saving video .ufmf')

        self.timeRecord = rospy.Time.now().to_sec()
                

        return self.isRecording






###############################################################################
def get_app_defaults():
    defaults = dict(wrapper='ctypes',
                    backend='mega',

                    debug_drop=False,
                    wx=False,
                    sdl=False,
                    debug_acquire=False,
                    disable_ifi_warning=False,
                    num_points=20,
                    software_roi_radius=10,
                    num_buffers=50,
                    small_save_radius=10,
                    background_frame_interval=50,
                    background_frame_alpha=1.0/50.0,
                    mask_images = None,
                    )
    return defaults

###########################################################################
# Main
###########################################################################
def Main():
    rospy.init_node('camnode',
                    anonymous=True, # allow multiple instances to run
                    disable_signals=True, # let WX intercept them
                    log_level=LOGLEVEL)
    Parse_args_and_run()

def Benchmark():
    Parse_args_and_run(benchmark=True)

def Parse_args_and_run(benchmark=False):
    usage_lines = ['%prog [options]',
                   '',
                   '  available wrappers and backends:']

    for wrapper,backends in cam_iface_choose.wrappers_and_backends.iteritems():
        for backend in backends:
            usage_lines.append('    --wrapper %s --backend %s'%(wrapper,backend))
    del wrapper, backend # delete temporary variables
    usage = '\n'.join(usage_lines)

    parser = OptionParser(usage=usage,
                          version="%prog "+flydra.version.__version__)

    defaults = get_app_defaults()
    parser.set_defaults(**defaults)

    parser.add_option("--wrapper", type='string',
                      help="cam_iface WRAPPER to use [default: %default]",
                      metavar="WRAPPER")

    parser.add_option("--backend", type='string',
                      help="cam_iface BACKEND to use [default: %default]",
                      metavar="BACKEND")

    parser.add_option("--debug-drop", action='store_true',
                      help="save debugging information regarding dropped network packets")

    parser.add_option("--debug-std", action='store_true',
                      help="show mean pixel STD every 200 frames")

    parser.add_option("--sdl", action='store_true',
                      help="SDL-based display of raw images")

    parser.add_option("--wx", action='store_true',
                      help="wx-based GUI to display raw images")

    parser.add_option("--wx-full", action='store_true',
                      help="wx-based GUI to display raw and processed images")

    parser.add_option("--debug-acquire", action='store_true',
                      help="print to the console information on each frame")

    parser.add_option("--disable-ifi-warning", action='store_true',
                      help=("do not print a warning if the inter-frame-interval "
                            "(IFI) is longer than expected"))

    parser.add_option("--ignore-version", action='store_true',
                      help=("do not care if version is mismatched with mainbrain"))

    parser.add_option("--num-points", type="int",
                      help="number of points to track per cameras [default: %default]")

    parser.add_option("--software-roi-radius", type="int",
                      help="radius of software region of interest [default: %default]")

    parser.add_option("--background-frame-interval", type="int",
                      help="every N frames, add a new BG image to the accumulator [default: %default]")

    parser.add_option("--background-frame-alpha", type="float",
                      help="weight for each BG frame added to accumulator [default: %default]")

    parser.add_option("--mode-num", type="int", default=None,
                      help="force a camera mode")

    parser.add_option("--num-buffers", type="int",
                      help="force number of buffers [default: %default]")

    parser.add_option("--mask-images", type="string",
                      help="list of masks for each camera (uses OS-specific path separator, ':' for POSIX, ';' for Windows)")

    parser.add_option("--emulation-image-sources", type="string",
                      help=("list of image sources for each camera (uses OS-specific "
                            "path separator, ':' for POSIX, ';' for Windows) ends with '.fmf', "
                            "'.ufmf', or is '<random:params=x>'"))

    parser.add_option("--simulate-point-extraction", type="string",
                      help="list of image sources for each camera")

    parser.add_option("--force-cam-ids", type="string",
                      help="list of names for each camera (comma separated)")

    parser.add_option("--cams-only", type="string",
                      help="list cameras to use (comma separated)")

    parser.add_option("--show-cam-details", action='store_true', default=False)

    parser.add_option("--small-save-radius", type="int",
                      help='half the edge length of .ufmf movies [default: %default]')
    parser.add_option("--rosrate", type="float", dest='rosrate', default=30.,
                      help='desired framerate for the ROS raw image emitter (if ROS enabled)')

    (options, args) = parser.parse_args()
    #rospy.logwarn(dir(options))

    if not options.wrapper:
        rospy.logwarn('WRAPPER must be set')
        parser.print_help()
        return

    if not options.backend:
        rospy.logwarn('BACKEND must be set')
        parser.print_help()
        return

    app_state=AppState(options = options,
                       benchmark=benchmark,
                       )

    if options.wx or options.wx_full:
        assert options.sdl == False, 'cannot have wx and sdl simultaneously enabled!'
        full = bool(options.wx_full)
        import camnodewx
        app=camnodewx.WxApp()
        if not DISABLE_ALL_PROCESSING:
            app_state.append_chain( klass = camnodewx.DisplayCamData, args=(app,),
                                    kwargs = dict(full=full),
                                    basename = 'camnodewx.DisplayCamData' )
        app.post_init(call_often=app_state.main_thread_task, full=full)
        app_state.set_quit_function( app.OnQuit )
    elif options.sdl:
        import camnodesdl
        app=camnodesdl.SdlApp(
                              call_often = app_state.main_thread_task)
        if not DISABLE_ALL_PROCESSING:
            app_state.append_chain( klass = camnodesdl.DisplayCamData, args=(app,),
                                    basename = 'camnodesdl.DisplayCamData' )
        app_state.set_quit_function( app.OnQuit )
    else:
        app=ConsoleApp(call_often=app_state.main_thread_task)
        app_state.set_quit_function( app.OnQuit )

    for (model, controller) in zip(app_state.get_image_sources(),
                                   app_state.get_image_controllers()):
        app.generate_view( model, controller )
    app.MainLoop()

if __name__=='__main__':
    Main()

