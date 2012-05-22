#!/usr/bin/env python

"""core runtime code for online, realtime tracking"""
# TODO:
# 1. make variable eccentricity threshold dependent on area (bigger area = lower threshold)
from __future__ import with_statement, division
import threading, time, socket, select, sys, os, struct, pickle, copy
import collections
import traceback
import Pyro.core
import flydra.reconstruct
import flydra.reconstruct_utils as ru
import numpy
from numpy import nan, inf
near_inf = 9.999999e20
import Queue
import tables
pytables_filt = numpy.asarray
import atexit

import motmot.utils.config
import flydra.version
import flydra.kalman.flydra_kalman_utils as flydra_kalman_utils
import flydra.kalman.flydra_tracker
import flydra.fastgeom as geom
#import flydra.geom as geom
import flydra.data_descriptions
import flydra.debuglock
DebugLock = flydra.debuglock.DebugLock

import warnings
warnings.filterwarnings('ignore', category=tables.NaturalNameWarning)

# ensure that pytables uses numpy:
import tables.flavor
tables.flavor.restrict_flavors(keep=['numpy'])

import roslib; roslib.load_manifest('mainbrain')
import rospy
from geometry_msgs.msg import Point, Vector3

#LOGLEVEL = rospy.DEBUG
#LOGLEVEL = rospy.INFO
LOGLEVEL = rospy.WARN
#LOGLEVEL = rospy.ERROR
#LOGLEVEL = rospy.FATAL

from mainbrain.msg import *
from mainbrain.srv import *


DO_KALMAN= True # Enables/disables Kalman filter based tracking
MIN_KALMAN_OBSERVATIONS_TO_SAVE = 10 # how many data points are required before saving trajectory?
SHOW_3D_PROCESSING_LATENCY = False
NETWORK_PROTOCOL = rospy.get_param('mainbrain/network_protocol', 'udp')
ATTEMPT_DATA_RECOVERY = True
#ATTEMPT_DATA_RECOVERY = False
BENCHMARK_GATHER=False


if os.name == 'posix':
    try:
        import posix_sched
    except ImportError, err:
        warnings.warn('Could not open posix_sched module')

Pyro.config.PYRO_MULTITHREADED = 0 # We do the multithreading around here...

Pyro.config.PYRO_TRACELEVEL = 3
Pyro.config.PYRO_USER_TRACELEVEL = 3
Pyro.config.PYRO_DETAILED_TRACEBACK = 1
Pyro.config.PYRO_PRINT_REMOTE_TRACEBACK = 1

IMPOSSIBLE_TIMESTAMP = -10.0

PT_TUPLE_IDX_X = flydra.data_descriptions.PT_TUPLE_IDX_X
PT_TUPLE_IDX_Y = flydra.data_descriptions.PT_TUPLE_IDX_Y
PT_TUPLE_IDX_FRAME_PT_IDX = flydra.data_descriptions.PT_TUPLE_IDX_FRAME_PT_IDX
PT_TUPLE_IDX_CUR_VAL_IDX = flydra.data_descriptions.PT_TUPLE_IDX_CUR_VAL_IDX
PT_TUPLE_IDX_MEAN_VAL_IDX = flydra.data_descriptions.PT_TUPLE_IDX_MEAN_VAL_IDX
PT_TUPLE_IDX_SUMSQF_VAL_IDX = flydra.data_descriptions.PT_TUPLE_IDX_SUMSQF_VAL_IDX

USE_ROS_INTERFACE = False # temporary.
USE_ONE_TIMEPORT_PER_CAMERA = False # True=OnePerCamera, False=OnePerCamnode.  Keep in sync with camnode.py

########
# persistent configuration data ( implementation in motmot.utils.config )
def get_rc_params():
    defaultParams = {
        'frames_per_second'  : 100.0,
        'hypothesis_test_max_acceptable_error' : 50.0,
        'kalman_model' :'EKF mamarama, units: mm',
        'max_reconstruction_latency_sec':0.06, # 60 msec
        'max_N_hypothesis_test':3,
        }
    fviewrc_fname = motmot.utils.config.rc_fname(filename='mainbrainrc',
                                                 dirname='.flydra')
    rc_params = motmot.utils.config.get_rc_params(fviewrc_fname,
                                                  defaultParams)
    return rc_params
def save_rc_params():
    save_fname = motmot.utils.config.rc_fname(must_already_exist=False,
                                              filename='mainbrainrc',
                                              dirname='.flydra')
    motmot.utils.config.save_rc_params(save_fname,rc_params)
    
rc_params = get_rc_params()
max_reconstruction_latency_sec = rc_params['max_reconstruction_latency_sec']
max_N_hypothesis_test =  rc_params['max_N_hypothesis_test']
########

g_XXX_framenumber = 0

# MainbrainKeeper()
# Keeps track of all mainbrain instances, and cleans up gracefully when exiting.
class MainbrainKeeper:
    def __init__(self):
        self.kept = []
        atexit.register(self.atexit)
    def register(self, mainbrain_instance ):
        self.kept.append( mainbrain_instance )
    def atexit(self):
        for k in self.kept:
            k.quit() # closes hdf5 file and closes cameras

g_mainbrain_keeper = MainbrainKeeper() # global to close Mainbrain instances upon exit

class LockedValue:
    def __init__(self,initial_value=None):
        self.lock = threading.Lock()
        self._val = initial_value
        self._q = Queue.Queue()
    def set(self,value):
        self._q.put( value )
    def get(self):
        try:
            while 1:
                self._val = self._q.get_nowait()
        except Queue.Empty:
            pass
        return self._val

g_best_realtime_data=None

try:
    g_hostname = socket.gethostbyname(socket.gethostname())
except:
    g_hostname = socket.gethostbyname('mainbrain')

g_downstream_hosts = []

if 0:
    g_downstream_hosts.append( ('192.168.1.199',28931) ) # projector
if 0:
    g_downstream_hosts.append( ('127.0.0.1',28931) ) # self
if 0:
    g_downstream_hosts.append( ('192.168.1.151',28931) ) # brain1

g_downstream_kalman_hosts = []
if 1:
    g_downstream_kalman_hosts.append( ('127.0.0.1',28931) ) # localhost
    g_downstream_kalman_hosts.append( ('192.168.10.41',28931) ) # wtstim
#    g_downstream_kalman_hosts.append( ('255.255.255.255',28931) ) # broadcast to every device on subnet
if 0:
    g_downstream_kalman_hosts.append( ('astraw-office.kicks-ass.net',28931) ) # send off subnet

g_socket_outgoing_UDP = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# 2D data format for PyTables:
Info2D = flydra.data_descriptions.Info2D

class CamSyncInfo(tables.IsDescription):
    guid = tables.StringCol(256,pos=0)
    camn   = tables.UInt16Col(pos=1)
    frame0 = tables.FloatCol(pos=2)

class HostClockInfo(tables.IsDescription):
    remote_hostname  = tables.StringCol(255,pos=0)
    start_timestamp  = tables.FloatCol(pos=1)
    remote_timestamp = tables.FloatCol(pos=2)
    stop_timestamp   = tables.FloatCol(pos=3)

class TriggerClockInfo(tables.IsDescription):
    start_timestamp  = tables.FloatCol(pos=0)
    framecount       = tables.Int64Col(pos=1)
    tcnt             = tables.UInt16Col(pos=2)
    stop_timestamp   = tables.FloatCol(pos=3)

class MovieInfo(tables.IsDescription):
    guid             = tables.StringCol(16,pos=0)
    filename           = tables.StringCol(255,pos=1)
    approx_start_frame = tables.Int64Col(pos=2)
    approx_stop_frame  = tables.Int64Col(pos=3)

class Info3D(tables.IsDescription):
    # This is for the non-Kalman version.
    frame      = tables.Int64Col(pos=0)

    x          = tables.Float32Col(pos=1)
    y          = tables.Float32Col(pos=2)
    z          = tables.Float32Col(pos=3)

    p0         = tables.Float32Col(pos=4)
    p1         = tables.Float32Col(pos=5)
    p2         = tables.Float32Col(pos=6)
    p3         = tables.Float32Col(pos=7)
    p4         = tables.Float32Col(pos=8)
    p5         = tables.Float32Col(pos=9)

    timestamp  = tables.FloatCol(pos=10)

    camns_used = tables.StringCol(32,pos=11)
    mean_dist  = tables.Float32Col(pos=12) # mean 2D reconstruction error

class TextLogDescription(tables.IsDescription):
    mainbrain_timestamp = tables.FloatCol(pos=0)
    guid = tables.StringCol(255,pos=1)
    host_timestamp = tables.FloatCol(pos=2)
    message = tables.StringCol(255,pos=3)

FilteredObservations = flydra_kalman_utils.FilteredObservations
kalman_observations_2d_idxs_type = flydra_kalman_utils.kalman_observations_2d_idxs_type

h5_obs_names = tables.Description(FilteredObservations().columns)._v_names

# allow rapid building of numpy.rec.array:
Info2DCol_description = tables.Description(Info2D().columns)._v_nestedDescr

def encode_data_packet( fn_corrected,
                        line3d_valid,
                        outgoing_data,
                        min_mean_dist):

    # This is for the non-Kalman data. See
    # kalman.flydra_tracker.Tracker.encode_data_packet and
    # kalman.data_packets.encode_data_packet() for Kalman version.

    fmt = '<iBfffffffffdf' # XXX I guess this no longer works -- what's the B for? 20071011
    packable_data = list(outgoing_data)
    if not line3d_valid:
        packable_data[3:9] = 0,0,0,0,0,0
    packable_data.append( min_mean_dist )
    try:
        data_packet = struct.pack(fmt,
                                  fn_corrected,
                                  line3d_valid,
                                  *packable_data)
    except SystemError, x:
        rospy.logwarn( 'fmt: %s' % fmt)
        rospy.logwarn( 'corrected_framenumber: %d' % fn_corrected)
        rospy.logwarn( 'line3d_valid: %s' % line3d_valid)
        rospy.logwarn( 'packable_data: %s' % packable_data)
        raise
    return data_packet

def save_ascii_matrix(filename,m):
    fd=open(filename,mode='wb')
    for row in m:
        fd.write( ' '.join(map(str,row)) )
        fd.write( '\n' )

def get_best_realtime_data():
    global g_best_realtime_data
    data = g_best_realtime_data
    g_best_realtime_data = None
    return data

##def DEBUG(msg=''):
##    rospy.logwarn( msg,'line',sys._getframe().f_back.f_lineno,', thread', threading.currentThread()
##    #for t in threading.enumerate():
##    #    rospy.logwarn( '   ',t

def DEBUG(msg=''):
    return


class ThreadEchoTimestamp(threading.Thread):
    def __init__(self):
        self.echotimestamp_byguid = {}
        
    def register_camera(self, guid):
        if guid not in self.echotimestamp_byguid:
            st_service = 'guid_%s' % guid 
            rospy.wait_for_service(st_service)
            self.echotimestamp_byguid[guid] = {}
            self.echotimestamp_byguid[guid]['service'] = rospy.ServiceProxy(st_service, SrvEchoTimestamp)
    
    def deregister_camera(self, guid):
        if guid in self.echotimestamp_byguid:
            del self.echotimestamp_byguid[guid]
    
    def measure_durations(self):
        # Measure the times.
        for guid in self.echotimestamp_byguid:
            timestampMainbrainPre = rospy.Time.now().to_sec()
            rv = self.echotimestamp_byguid[guid]['service'](timestampMainbrain)
            timestampCamera = rv.time
            timestampMainbrainPost = rospy.Time.now().to_sec()
            
            self.echotimestamp_byguid[guid]['durationM2C'] = timestampCamera - timestampMainbrainPre
            self.echotimestamp_byguid[guid]['durationC2M'] = timestampMainbrainPost - timestampCamera
            
        # Report the times.
        for guid in self.echotimestamp_byguid:
            durationTotal = self.echotimestamp_byguid[guid]['durationM2C'] + self.echotimestamp_byguid[guid]['durationC2M']
            rospy.logwarn('duration[%s] M2C:%0.3f, C2M:%0.3f, total:%0.3f' % (guid,
                                                                              self.echotimestamp_byguid[guid]['durationM2C'],
                                                                              self.echotimestamp_byguid[guid]['durationC2M'], 
                                                                              durationTotal))
            
            
            

class TimestampEchoReceiver(threading.Thread):
    def __init__(self,mainbrain):
        self.mainbrain = mainbrain

        name = 'TimestampEchoReceiver thread'
        threading.Thread.__init__(self,name=name)
        
        

    def run(self):
        rospy.sleep(1)  # Haven't investigated why, but we get a raise ResponseNotReady() from rospy if we don't do it.

        ip2hostname = {}
        fmt_timestamp_echo_2 = rospy.get_param('mainbrain/timestamp_echo_fmt2', '&lt;dd')

        port = rospy.get_param('mainbrain/port_timestamp_mainbrain', 28993)
        socket_echo_timestamp_mainbrain = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        socket_echo_timestamp_mainbrain.bind((g_hostname, port))

        diff_last_clock = collections.defaultdict(list)

        while 1:
            size_echotimestamp = struct.calcsize(fmt_timestamp_echo_2) 
            timestamp_echo_buf = ''
            try:
                while len(timestamp_echo_buf)<size_echotimestamp:
                    chunk,(timestamp_echo_remote_ip,cam_port) = socket_echo_timestamp_mainbrain.recvfrom(size_echotimestamp-len(timestamp_echo_buf))
                    if chunk == '':
                        raise RuntimeError("socket connection broken")
                    timestamp_echo_buf += chunk
                    
            except Exception, err:
                rospy.logwarn( 'Exception receiving timestamp echo data: %s' % str(err))
                continue

            stop_timestamp = rospy.Time.now().to_sec()

            start_timestamp,remote_timestamp = struct.unpack(fmt_timestamp_echo_2,timestamp_echo_buf)

            tlist = diff_last_clock[timestamp_echo_remote_ip]
            tlist.append( (start_timestamp, remote_timestamp, stop_timestamp) )
            if len(tlist)==100:
                if timestamp_echo_remote_ip not in ip2hostname:
                    ip2hostname[timestamp_echo_remote_ip]=socket.getfqdn(timestamp_echo_remote_ip)
                remote_hostname = ip2hostname[timestamp_echo_remote_ip]
                tarray = numpy.array(tlist)

                del tlist[:] # clear list
                start_timestamps = tarray[:,0]
                stop_timestamps = tarray[:,2]
                durationsRoundtrip = stop_timestamps-start_timestamps
                # find best measurement (that with shortest durationsRoundtrip)
                iMinDuration = numpy.argmin(durationsRoundtrip)
                srs = tarray[iMinDuration,:] # Get the (start,remote,stop) times of the quickest entry.
                start_timestamp, remote_timestamp, stop_timestamp = srs
                durationToRemote = remote_timestamp - start_timestamp
                durationFromRemote = stop_timestamp - remote_timestamp
                if True: #durationToRemote > 1:
                    rospy.logwarn('%s : durationToRemote=%.2fms durationFromRemote=%.2fms, durationTotal=%0.2fms' % (remote_hostname, 
                                                                                                                     durationToRemote*1e3, 
                                                                                                                     durationFromRemote*1e3, 
                                                                                                                     durationsRoundtrip[iMinDuration]*1e3,))

                self.mainbrain.queue_host_clock_info.put(  (remote_hostname,
                                                             start_timestamp,
                                                             remote_timestamp,
                                                             stop_timestamp) )
                if False:
                    duration_measurement = durationsRoundtrip[iMinDuration]
                    clock_diff = stop_timestamp-remote_timestamp

                    rospy.logwarn( '%s: remote diff is %.1f msec (within 0-%.1f msec accuracy)' % (remote_hostname, 
                                                                                                   clock_diff*1000, 
                                                                                                   duration_measurement*1000))

class TrigReceiver(threading.Thread):
    def __init__(self,mainbrain):
        self.mainbrain = mainbrain

        name = 'TrigReceiver thread'
        threading.Thread.__init__(self,name=name)

    def run(self):
        global g_hostname

        rospy.sleep(2)
        port = rospy.get_param('mainbrain/port_trigger_network', 28994)
        socket_trigger_network = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        socket_trigger_network.bind((g_hostname, port))

        while 1: # XXX enable quit
            try:
                trig_buf, (remote_ip,cam_port) = socket_trigger_network.recvfrom(4096) # BUG: Need to wrap this in a while loop.
            except Exception, err:
                rospy.logwarn( 'Exception receiving trigger data: %s' % str(err))
                continue

            if trig_buf=='1':
                with self.mainbrain.lock_trigger_device:
                    pre_timestamp = rospy.Time.now().to_sec()
                    self.mainbrain.trigger_device.ext_trig1 = True
                    # hmm, calling log_message is normally what the cameras do..
                    self.mainbrain.remote_api.log_message('<mainbrain>',pre_timestamp,'EXTTRIG1')

            elif trig_buf=='2':
                with self.mainbrain.lock_trigger_device:
                    pre_timestamp = rospy.Time.now().to_sec()
                    self.mainbrain.trigger_device.ext_trig2 = True
                    # hmm, calling log_message is normally what the cameras do..
                    self.mainbrain.remote_api.log_message('<mainbrain>',pre_timestamp,'EXTTRIG2')

            elif trig_buf=='3':
                with self.mainbrain.lock_trigger_device:
                    pre_timestamp = rospy.Time.now().to_sec()
                    self.mainbrain.trigger_device.ext_trig3 = True
                    # hmm, calling log_message is normally what the cameras do..
                    self.mainbrain.remote_api.log_message('<mainbrain>',pre_timestamp,'EXTTRIG3')

class CoordRealReceiver(threading.Thread):
    # called from CoordinateProcessor thread
    def __init__(self,quit_event):
        global g_hostname

        self.quit_event = quit_event
        self.lock_socket = threading.Lock()

        self.coordinatesframe_queue = Queue.Queue()
        with self.lock_socket:
            self.guid_from_socket_listen = {}
            self.guid_from_socket_server = {}

        name = 'CoordRealReceiver thread'
        threading.Thread.__init__(self,name=name)


    def add_socket(self, port_coordinates, guid):
        global g_hostname

        if NETWORK_PROTOCOL == 'udp':
            socket_coordinates = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            socket_coordinates.bind((g_hostname, port_coordinates))
            socket_coordinates.setblocking(False)
            with self.lock_socket:
                self.guid_from_socket_listen[socket_coordinates] = guid
                
        elif NETWORK_PROTOCOL == 'tcp':
            socket_coordinates = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            socket_coordinates.bind((g_hostname, port_coordinates))
            socket_coordinates.listen(1)
            socket_coordinates.setblocking(False)
            with self.lock_socket:
                self.guid_from_socket_server[socket_coordinates] = guid
        else:
            raise ValueError('unknown NETWORK_PROTOCOL')
        
        
    def remove_socket(self, guid):
        with self.lock_socket:
            for socket_coordinates, test_guid in self.guid_from_socket_listen.iteritems():
                if guid == test_guid:
                    socket_coordinates.close()
                    del self.guid_from_socket_listen[socket_coordinates]
                    break # XXX naughty to delete item inside iteration
            for socket_coordinates, test_guid in self.guid_from_socket_server.iteritems():
                if guid == test_guid:
                    socket_coordinates.close()
                    del self.guid_from_socket_server[socket_coordinates]
                    break # XXX naughty to delete item inside iteration

    def get_coordinatesframes(self):
        # One item in list looks like:
        #header = struct.pack('<ddliI',
        #                   timestamp_image, 
        #                   timestamp_received,
        #                   framenumber,
        #                   len(points),
        #                   n_frames_skipped)
        # coordinatesframe = header + multiple copies of struct.pack(pt_fmt, *point_tuple)
        # item = (guid, coordinatesframe)
        coordinatesframe_list = []

        try:
            coordinatesframe_list.append( self.coordinatesframe_queue.get(True, 0.1) ) # block for 0.1 second timeout for the first item
            while 1:
                # Don't wait for next items, but collect them if they're there
                coordinatesframe_list.append( self.coordinatesframe_queue.get_nowait() )
        except Queue.Empty:
            pass
        
        return coordinatesframe_list

    # Called from CoordRealReceiver thread
    def run(self):
        timeout=0.1
        BENCHMARK_2D_GATHER = False
        if BENCHMARK_2D_GATHER:
            fmt_header = '<ddliI'
            size_header = struct.calcsize(fmt_header)
        while not self.quit_event.isSet():
            if NETWORK_PROTOCOL == 'tcp':
                with self.lock_socket:
                    sockobjs = self.guid_from_socket_server.keys()
                try:
                    sock_in_ready, sock_out_ready, sock_exc_ready = select.select(sockobjs, [],  [],  0.0)
                except (select.error, socket.error), exc:
                    rospy.logwarn('Exception in server socket: %s' % exc)
                else:
                    for sockobj in sock_in_ready:
                        with self.lock_socket:
                            guid = self.guid_from_socket_server[sockobj]
                        client_sockobj, addr = sockobj.accept()
                        client_sockobj.setblocking(False)
                        rospy.logwarn('Camera %s connected from %s' % (guid, addr))
                        with self.lock_socket:
                            self.guid_from_socket_listen[client_sockobj]=guid

            with self.lock_socket:
                sockobjs = self.guid_from_socket_listen.keys()
            try:
                sock_in_ready, sock_out_ready, sock_exc_ready = select.select(sockobjs, [], [],  timeout)
            except (select.error, socket.error), exc:
                rospy.logwarn('Exception in listen socket: %s' % exc)
            else:
                if not len(sock_in_ready):
                    continue

                # now gather all data waiting on the sockets

                for sockobj in sock_in_ready:
                    try:
                        with self.lock_socket:
                            guid = self.guid_from_socket_listen[sockobj]
                    except KeyError,ValueError:
                        rospy.logwarn( 'Strange - what is in my listen sockets list?  %s' % sockobj)
                        # XXX camera was dropped?
                        continue

                    if NETWORK_PROTOCOL == 'udp':
                        try:
                            data, addr = sockobj.recvfrom(4096)  # BUG: Need to wrap this in a while loop.
                        except Exception, err:
                            rospy.logwarn( 'Exception receiving UDP data: %s' % str(err))
                            continue
                    elif NETWORK_PROTOCOL == 'tcp':
                        data = sockobj.recv(4096)
                    else:
                        raise ValueError('unknown NETWORK_PROTOCOL')

                    if BENCHMARK_2D_GATHER:
                        header = data[:size_header]
                        if len(header) != size_header:
                            # incomplete header buffer
                            break
                        # this timestamp is the remote camera's timestamp
                        (timestamp, timestamp_received, framenumber,
                         n_points,n_frames_skipped) = struct.unpack(fmt_header,header)
                        recv_latency_msec = (rospy.Time.now().to_sec()-timestamp_received)*1e3
                        #rospy.logwarn('recv_latency_msec: %3.1f' % recv_latency_msec)
                        #rospy.logwarn ('guid=%s: now-timestamp=%0.4f' % (guid, rospy.Time.now().to_sec()-timestamp)) # Terrible.
                        #rospy.logwarn ('guid=%s: now-received=%0.4f' % (guid, rospy.Time.now().to_sec()-timestamp_received))  # Better.
                    self.coordinatesframe_queue.put((guid, data ))


class CoordinateSender(threading.Thread):
      """a class to send realtime coordinate data from a separate thread"""
      def __init__(self,my_queue,my_ros_queue,quit_event):
          self.my_queue = my_queue
          self.my_ros_queue = my_ros_queue
          self.quit_event = quit_event
          name = 'CoordinateSender thread'
          threading.Thread.__init__(self,name=name)
          
      def run(self):
          global g_downstream_kalman_hosts
          
          out_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
          block = 1
          timeout = 0.1
          encode_super_packet = flydra.kalman.data_packets.encode_super_packet

          
          pub = rospy.Publisher('flydra_mainbrain_super_packets', flydra_mainbrain_super_packet)

          while not self.quit_event.isSet():
              packets = []
              packets.append( self.my_queue.get() )
              while 1:
                  try:
                      packets.append( self.my_queue.get_nowait() )
                  except Queue.Empty:
                      break
              # Now packets is a list of all recent data
              super_packet = encode_super_packet( packets )
              for downstream_host in g_downstream_kalman_hosts:
                nBytesTotal = len(super_packet)
                nBytesSent = 0
                while nBytesSent < nBytesTotal:
                    nBytes = g_socket_outgoing_UDP.sendto(super_packet[nBytesSent:],downstream_host)
                    nBytesSent += nBytes



              ros_packets = []
              ros_packets.append( self.my_ros_queue.get() )
              while 1:
                  try:
                      ros_packets.append( self.my_ros_queue.get_nowait() )
                  except Queue.Empty:
                      break
              ros_super_packet = flydra_mainbrain_super_packet(packets=ros_packets)
              pub.publish(ros_super_packet)

class CoordinateProcessor(threading.Thread):
    def __init__(self,
                 mainbrain,
                 save_profiling_data=False,
                 debug_level=None,
                 show_sync_errors=True,
                 show_overall_latency=None):
        
        global g_hostname
        
        self.mainbrain = mainbrain
        self.debug_level = debug_level
        self.show_overall_latency = show_overall_latency

        self.save_profiling_data = save_profiling_data
        if self.save_profiling_data:
            self.data_dict_queue = []

        self.realtime_kalman_data_queue = Queue.Queue()
        self.realtime_ros_packets_queue = Queue.Queue()

        self.guid_list = []
        self.port_coordinates_byguid = {}
        self.timestamp_prev_byguid = {}
        self.offset_trigger_prev_byguid = {}
        self.offset_trigger_accumulator_byguid = {}
        self.resync_byguid = {}
        self.fn_skip_prev_byguid = {}
        self.general_save_info_byguid = {}

        if ATTEMPT_DATA_RECOVERY:
            #self.lock_request_data = DebugLock('lock_request_data',True) # protect request_data
            self.lock_request_data = threading.Lock() # protect request_data
            self.request_data = {}
            
        self.camn_from_guid = {}
        self.guid_from_camn = {}
        self.guid_from_guid = {} # Identity mapping to use guid instead of camn for flydra.kalman functions.
        self.reconstructor = None
        self.reconstructor_meters = None
        self.tracker = None
        self.show_sync_errors = show_sync_errors
        self.b_dosync = False

        self.ip2hostname = {}

        self.lock_tracker = threading.Lock()
        #self.lock_tracker = DebugLock('lock_tracker',verbose=True)
        self.lock_alldata = threading.RLock()
        #self.lock_alldata = DebugLock('lock_alldata',verbose=False)
        self.quit_event = threading.Event()

        #self.max_camns = -1

        self.realreceiver = CoordRealReceiver(self.quit_event)
        self.realreceiver.setDaemon(True)
        self.realreceiver.start()

        cs = CoordinateSender(self.realtime_kalman_data_queue,self.realtime_ros_packets_queue,self.quit_event)
        cs.setDaemon(True)
        cs.start()

        name = 'CoordinateProcessor thread'
        threading.Thread.__init__(self,name=name)

    def port_coordinates_from_guid(self, guid):
        with self.lock_alldata:
            port_coordinates = self.port_coordinates_byguid[guid]
        return port_coordinates

    def get_general_cam_info(self):
        with self.lock_alldata:
            result = self.general_save_info_byguid.copy()
        return result

    def get_missing_data_dict(self):
        # called from main thread, must lock data in realtime coord thread
        result_by_guid = {}
        with self.lock_request_data:
            # Go through all the cameras.
            for guid,queue_tmp in self.request_data.iteritems():
                fn_missing_list = []
                #guid = None
                offset_fn = None
                try:
                    # Get all the queued framenumbers for camera N.
                    while 1:
                        offset_fn_this, fn_list_this = queue_tmp.get_nowait()
                        #if guid is None:
                        #    guid = guid_this
                        if offset_fn is None:
                            offset_fn = offset_fn_this

                        #assert guid == guid_this # make sure given camn comes from single guid
                        assert offset_fn == offset_fn_this

                        fn_missing_list.extend( fn_list_this )
                except Queue.Empty:
                    pass
                
                if len(fn_missing_list):
                    result_by_guid[guid] = offset_fn, fn_missing_list
                    
        return result_by_guid

    def set_reconstructor(self,r):
        # called from main thread, must lock to send to realtime coord thread
        with self.lock_alldata:
            self.reconstructor = r

        if r is None:
            self.reconstructor_meters = None
            return

        # get version that operates in meters
        scale_factor = self.reconstructor.get_scale_factor()
        self.reconstructor_meters = self.reconstructor.get_scaled(scale_factor)

    def set_new_tracker(self,kalman_model=None):
        # called from main thread, must lock to send to realtime coord thread
        scale_factor = self.reconstructor.get_scale_factor()
        tracker = flydra.kalman.flydra_tracker.Tracker(self.reconstructor_meters,
                                                       scale_factor=scale_factor,
                                                       kalman_model=kalman_model)
        tracker.set_killed_tracker_callback( self.enqueue_finished_tracked_object )
        with self.lock_tracker:
            if self.tracker is not None:
                self.tracker.kill_all_trackers() # save (if necessary) all old data
            self.tracker = tracker # bind to name, replacing old tracker
            if self.save_profiling_data:
                tracker = copy.copy(self.tracker)
                tracker.kill_tracker_callbacks = []
                if 1:
                    raise NotImplementedError('')
                else:
                    # this is the old call, it needs to be fixed...
                    tracker.live_tracked_objects = []
                tracker.dead_tracked_objects = []
                self.data_dict_queue.append( ('tracker',tracker))

    def enqueue_finished_tracked_object(self, tracked_object ):
        # this is from called within the realtime coords thread
        if self.mainbrain.is_saving_data():
            if self.debug_level.isSet():
                rospy.logdebug('Saving finished kalman object with %d frames' % len(tracked_object.frames))
            self.mainbrain.queue_data3d_kalman_estimates.put(
                (tracked_object.obj_id,
                 tracked_object.frames, tracked_object.xhats, tracked_object.Ps,
                 tracked_object.timestamps,
                 tracked_object.observations_frames, tracked_object.observations_data,
                 tracked_object.observations_2d, tracked_object.observations_Lcoords,
                 ) )

    # Find the lowest available value in the dict, starting at 0.
    def find_first_free_value(self, index_dict):
        #index = 0
        #values = index_dict.values()
        #while index in values:
        #    index += 1
        # 
        # return index
        global g_camn_max
        try:
            g_camn_max += 1
        except NameError:
            g_camn_max = 0
            
        return g_camn_max
        
        
    def connect(self, guid):
        # called from Remote-API thread on camera connect
        global g_hostname

        assert not self.mainbrain.is_saving_data()

        with self.lock_alldata:
            self.guid_list.append(guid)

            # Allocate a port for coordinates.
            if len(self.port_coordinates_byguid)>0:
                port_coordinates = max(self.port_coordinates_byguid.values())+1
            else:
                port_coordinates = rospy.get_param('mainbrain/port_coordinate_base', 34813)
            self.port_coordinates_byguid[guid] = port_coordinates

            # Allocate a cam index.
            camn = self.find_first_free_value(self.camn_from_guid)

            # Map guids to indices.
            self.guid_from_camn[camn] = guid
            self.camn_from_guid[guid] = camn
            self.guid_from_guid[guid] = guid

            # create and bind socket to listen to
            self.realreceiver.add_socket(port_coordinates,guid)
            self.timestamp_prev_byguid[guid] = IMPOSSIBLE_TIMESTAMP # arbitrary impossible number
            self.offset_trigger_prev_byguid[guid] = IMPOSSIBLE_TIMESTAMP
            self.offset_trigger_accumulator_byguid[guid] = 0.0
            self.fn_skip_prev_byguid[guid] = -1 # arbitrary impossible number
            self.general_save_info_byguid[guid] = {'camn':camn,
                                              'frame0':IMPOSSIBLE_TIMESTAMP}
            self.mainbrain.queue_cam_info.put(  (guid, camn, IMPOSSIBLE_TIMESTAMP) )
        return port_coordinates

    def disconnect(self, guid):
        with self.lock_alldata:
            camn = self.camn_from_guid[guid]
            try:
                del self.guid_from_camn[camn]
            except KeyError:
                pass
            
            del self.camn_from_guid[guid]
            del self.guid_from_guid[guid]
            del self.port_coordinates_byguid[guid]
            self.realreceiver.remove_socket(guid)
            del self.timestamp_prev_byguid[guid]
            del self.fn_skip_prev_byguid[guid]
            del self.general_save_info_byguid[guid]

    def quit(self):
        # called from outside of thread to quit the thread
        if self.save_profiling_data:
            fname = "data_for_kalman_profiling.pkl"
            fullpath = os.path.abspath(fname)
            rospy.logwarn("Saving data for profiling to %s" % fullpath)
            to_save = self.data_dict_queue
            save_fd = open(fullpath,mode="wb")
            pickle.dump( to_save, save_fd )
            save_fd.close()
            rospy.logwarn("Done saving")
        self.quit_event.set()
        self.join() # wait until CoordReceiver thread quits


    # Discard all the frames in each dict in the list.
    def discard_all_frames(self, byfn_list):
        with self.lock_alldata:
            for byfn in byfn_list:
                for fn in byfn:
                    del byfn[fn]


    def OnSynchronize(self, 
                      guid, 
                      #timestamp_trigger,
                      points_undistorted_byguid_byfn, 
                      timestamp_trigger_byguid_byfn,
                      points_pluecker_bycamn_byfn,
                      timestamp_frame_byfn,
                      fn_corrected_set):

        rospy.logwarn ('SYNCHRONIZE')
        if self.mainbrain.is_saving_data():
            rospy.logerror('Cannot resynchronize while saving data!')
            return

        if self.timestamp_prev_byguid[guid] != IMPOSSIBLE_TIMESTAMP:
            rospy.logwarn('(Re)synchronized camera %s' % guid)
            
            # Delete this guid in all frames, and if the last guid in the frame, then delete the frame too.
            todelete = []
            for fn in points_undistorted_byguid_byfn:
                if guid in points_undistorted_byguid_byfn[fn]:
                    del points_undistorted_byguid_byfn[fn][guid]
                if len(points_undistorted_byguid_byfn[fn])==0:
                    todelete.append(fn)
            for fn in todelete:
                del points_undistorted_byguid_byfn[fn]

            todelete = []
            camn = self.camn_from_guid[guid]                    
            for fn in points_pluecker_bycamn_byfn:
                if camn in points_pluecker_bycamn_byfn[fn]:
                    del points_pluecker_bycamn_byfn[fn][camn]
                if len(points_pluecker_bycamn_byfn[fn])==0:
                    todelete.append(fn)
            for fn in todelete:
                del camn_from_guid[fn]

            todelete = []
            for fn in timestamp_trigger_byguid_byfn:
                if guid in timestamp_trigger_byguid_byfn[fn]:
                    del timestamp_trigger_byguid_byfn[fn][guid]
                if len(timestamp_trigger_byguid_byfn[fn])==0:
                    todelete.append(fn)
            for fn in todelete:
                del timestamp_trigger_byguid_byfn[fn]

            todelete = []
            for fn in timestamp_frame_byfn:
                if fn not in timestamp_trigger_byguid_byfn:
                    todelete.append(fn)
            for fn in todelete:
                del timestamp_frame_byfn[fn]

            fn_corrected_set.clear()
            
        #self.mainbrain.queue_cam_info.put((guid, self.camn_from_guid[guid], timestamp_trigger))
        
        
    # Give the camera a new number.
    def reassign_camn(self, guid):
        with self.lock_alldata:
            camn_old = self.camn_from_guid[guid]
            camn_new = self.find_first_free_value(self.camn_from_guid)
            del self.guid_from_camn[camn_old]
            
            self.guid_from_camn[camn_new] = guid
            self.camn_from_guid[guid] = camn_new

            self.general_save_info_byguid[guid]['camn'] = camn_new
            #self.general_save_info_byguid[guid]['frame0'] = timestamp_trigger

        rospy.logwarn ('Changing cam number for %s: %d->%d' % (guid, camn_old, camn_new))


    # get_points_from_coordinatesframe()
    # Get three lists of points from the given coordinatesframe.
    #
    def get_points_from_coordinatesframe(self, guid, coordinatesframe):                    
        fmt_header = '<ddliI'
        size_header = struct.calcsize(fmt_header)
        pt_fmt = '<dddddddddBBddBddddddddd' # keep in sync with camnode.py
        size_point = struct.calcsize(pt_fmt)

        header = coordinatesframe[:size_header]
        pointarray = coordinatesframe[size_header:]
        
        # Check for valid header.
        if len(header) != size_header:
            # incomplete header buffer
            raise RuntimeError('Incomplete header buffer in coordinatesframe.')
        
        # Unpack the header.
        # timestamp_raw is the remote camera's timestamp (?? from the driver, not the host clock??)
        (timestamp_raw, timestamp_received, fn_raw, n_points, n_frames_skipped) = struct.unpack(fmt_header, header)

        # Write some debugging info.
        DEBUG_DROP = self.mainbrain.remote_api.caminfo_byguid[guid]['scalar_control_info']['debug_drop']
        if DEBUG_DROP:
            if debug_drop_fd is None:
                debug_drop_fd = open('debug_framedrop.txt',mode='w')
            debug_drop_fd.write('%d,%d\n'%(fn_raw,n_points))
            
        # Check for valid points array.
        if len(pointarray) < n_points * size_point:
            raise RuntimeError('Incomplete point info in coordinatesframe.')
        
        # Check that frame numbers make sense.
        fn_predicted = n_frames_skipped + self.fn_skip_prev_byguid[guid] + 1
        if fn_raw < fn_predicted:
            rospy.logwarn('fn_raw: %d' % fn_raw)
            rospy.logwarn('n_frames_skipped: %d' % n_frames_skipped)
            rospy.logwarn('fn_predicted: %d' % fn_predicted)
            rospy.logwarn('self.fn_skip_prev_byguid[%s]: %s' % (guid, self.fn_skip_prev_byguid[guid]))
            raise RuntimeError('Got framenumber already received or skipped!')

        elif fn_raw > fn_predicted:
            if not (self.fn_skip_prev_byguid[guid] == -1):
                # This is not the first frame.
                # Probably because network buffer filled up before we emptied it.
                rospy.logwarn('Frame data loss %s' % guid)

                if ATTEMPT_DATA_RECOVERY:
                    # this is not the first frame
                    fn_missing_list_tmp = range(self.fn_skip_prev_byguid[guid]+1, fn_raw)

                    with self.lock_request_data:
                        queue_tmp = self.request_data.setdefault(guid, Queue.Queue())

                        try:
                            offset_fn_tmp = self.mainbrain.timestamp_modeler.get_frame_offset(guid)#self.camn_from_guid[guid])
                        except KeyError:
                            pass
                        else:
                            queue_tmp.put( (offset_fn_tmp, fn_missing_list_tmp) )
                            del offset_fn_tmp
                            
                    del queue_tmp # drop reference to queue
                    del fn_missing_list_tmp

        self.fn_skip_prev_byguid[guid] = fn_raw

        # Set up lists of points.                        
        points_pluecker = []
        points_undistorted = []
        points_distorted = []
        
        if n_points:
            start = 0
            for i_point in range(n_points):
                (x_distorted, y_distorted,
                 area, slope, eccentricity,
                 p1, p2, p3, p4,
                 line_found,
                 slope_found,
                 x_undistorted, y_undistorted,
                 ray_valid,
                 ray0, ray1, ray2, ray3, ray4, ray5, # pluecker coords from cam center to detected point
                 cur_val, mean_val, sumsqf_val,
                 ) = struct.unpack(pt_fmt, pointarray[start:(start+size_point)])
                 
                # nan cannot get sent across network in platform-independent way
                if not line_found:
                    p1,p2,p3,p4 = nan,nan,nan,nan
                if slope == near_inf:
                    slope = inf
                if eccentricity == near_inf:
                    eccentricity = inf
                if not slope_found:
                    slope = nan

                # Keep in sync with kalmanize.py and data_descriptions.py
                pt_undistorted = (x_undistorted,y_undistorted,
                                  area,slope,eccentricity,
                                  p1,p2,p3,p4, line_found, i_point,
                                  cur_val, mean_val, sumsqf_val)
                pt_distorted = (x_distorted,y_distorted,
                                area,slope,eccentricity,
                                p1,p2,p3,p4, line_found, i_point,
                                cur_val, mean_val, sumsqf_val)
                if ray_valid:
                    points_pluecker.append( (pt_undistorted,
                                              geom.line_from_HZline((ray0,ray1,
                                                                     ray2,ray3,
                                                                     ray4,ray5))
                                              ))
                points_undistorted.append( pt_undistorted )
                points_distorted.append( pt_distorted )
                start += size_point
        else:
            # Append a non-point to allow correlation of
            # timestamps with frame number
            no_point_tuple = (nan,nan,nan,nan,nan,nan,nan,nan,nan,False,0,0,0,0)
            points_distorted.append( no_point_tuple )
            points_undistorted.append( no_point_tuple )
            
            
        return (points_pluecker, points_undistorted, points_distorted)


    # The frame time is that of the first camera to trigger in that frame.
    # Store the trigger timestamp, but don't overwrite older timestamps.
    # Output parameter:  timestamp_frame_byfn
    def update_timestamp_frame(self, timestamp_frame_byfn, fn, timestamp_new, inc_val):
        # If no frame yet, then enter the timestamp_new.
        if fn not in timestamp_frame_byfn:
            timestamp_frame_byfn[fn] = timestamp_new, inc_val
        
        else: # Otherwise take the oldest of existing & new times.
            timestamp_existing,n = timestamp_frame_byfn[fn]
            if timestamp_existing is None:
                timestamp_oldest = timestamp_new # This may be None, but eventually won't be.
            else:
                timestamp_oldest = min(timestamp_new, timestamp_existing)
                
            timestamp_frame_byfn[fn] = (timestamp_oldest, n+inc_val)
                                

    # Append the given data to the datarowlist that will be saved (elsewhere) at regular intervals.
    # Output parameter:  datarowlist_to_save
    def append_points_for_saving (self, datarowlist_to_save, camn, fn, timestamp_trigger, timestamp_received, points):
        if self.mainbrain.is_saving_data():
            for point_tuple in points:
                # Save 2D data (even when no point found) to allow
                # temporal correlation of movie frames to 2D data.
                i_point = point_tuple[PT_TUPLE_IDX_FRAME_PT_IDX]
                cur_val = point_tuple[PT_TUPLE_IDX_CUR_VAL_IDX]
                mean_val = point_tuple[PT_TUPLE_IDX_MEAN_VAL_IDX]
                sumsqf_val = point_tuple[PT_TUPLE_IDX_SUMSQF_VAL_IDX]

                # Don't bother saving if we don't know when it was from
                if fn is None:
                    continue
                
                # Append all to the given list.
                datarowlist_to_save.append((camn, fn, timestamp_trigger, timestamp_received)
                                           +point_tuple[:5]
                                           +(i_point, cur_val, mean_val, sumsqf_val))


    # Using the given coordinatesframes_list, update the various points and timestamps dicts.
    # Output parameters are:
    #   points_undistorted_byguid_byfn 
    #   timestamp_trigger_byguid_byfn 
    #   points_pluecker_bycamn_byfn 
    #   timestamp_frame_byfn 
    #   fn_corrected_set
    def collect_framedata_from_coordinatesframes_list (self, 
                                                      points_undistorted_byguid_byfn, 
                                                      points_pluecker_bycamn_byfn, 
                                                      timestamp_trigger_byguid_byfn, 
                                                      timestamp_frame_byfn, 
                                                      fn_corrected_set,
                                                      coordinatesframes_list,
                                                      timestamp_benchmark_list=None):
        with self.lock_alldata:

            datarowlist_to_save = []
            fn_corrected_set.clear()
            fmt_header = '<ddliI'
            size_header = struct.calcsize(fmt_header)
            for guid, coordinatesframe in coordinatesframes_list:
                try:
                    # Get the camera number.
                    camn = self.camn_from_guid[guid]
                except KeyError:
                    rospy.logwarn ('Camera %s no longer exists.' % guid)
                    pass # Camera no longer exists.
                else:
                    # Unpack the coordinatesframe.
                    header = coordinatesframe[:size_header]
                    (timestamp_raw, timestamp_received, fn_raw, n_points, n_frames_skipped) = struct.unpack(fmt_header, header)
                    (points_pluecker, points_undistorted, points_distorted) = self.get_points_from_coordinatesframe(guid, coordinatesframe)

                    if timestamp_benchmark_list is not None:
                        timestamp_benchmark_list.append(timestamp_received)


                    # Use timestamp_received to determine sync info. This avoids 2 potential problems:
                    #  * using timestamp_raw can fail if the camera drivers don't provide useful data
                    #  * using rospy.Time.now().to_sec() can fail if the network latency jitter is on 
                    #    the order of the inter frame interval.

                    # Register the frame.  To use guid, must indicate a resync with a flag self.resync_byguid[guid], and add a function to reset things in timestamp_modeler.
                    (timestamp_trigger, fn_corrected, did_frame_offset_change) = self.mainbrain.timestamp_modeler.register_frame(guid, fn_raw, timestamp_received, full_output=True)
                    if did_frame_offset_change:
                        self.resync_byguid[guid] = True

                    if fn_corrected is None:
                        continue


                    self.timestamp_prev_byguid[guid] = timestamp_trigger
                    #rospy.logwarn ('guid=%s: now-timestamp_received=%0.4f' % (guid, rospy.Time.now().to_sec()-timestamp_received))
                    #rospy.logwarn ('guid=%s: now-timestamp_trigger=%0.4f' % (guid, rospy.Time.now().to_sec()-timestamp_trigger))


                    # Store the timestamp & framenumber.
                    g_XXX_framenumber = fn_corrected
                    fn_corrected_set.add(fn_corrected)

                    
                    # Append the points for saving.
                    self.append_points_for_saving (datarowlist_to_save, camn, fn_corrected, timestamp_trigger, timestamp_received, points_distorted)


                    # Initialize a new entry for the frame.
                    if fn_corrected not in points_undistorted_byguid_byfn:
                        points_undistorted_byguid_byfn[fn_corrected] = {}
                        timestamp_trigger_byguid_byfn[fn_corrected] = {}

                    # Attempt 3D reconstruction of 1st point from each 2D view.  For hypothesis testing.
                    points_undistorted_byguid_byfn[fn_corrected][guid]= points_undistorted[0]
                    timestamp_trigger_byguid_byfn[fn_corrected][guid]= timestamp_trigger #timestamp_received

                    # Save all 3D Pluecker coordinates for Kalman filtering
                    if len(points_pluecker):
                        points_pluecker_bycamn_byfn[fn_corrected][camn] = points_pluecker

                    if n_points:
                        inc_val = 1
                    else:
                        inc_val = 0

                    # Update the timestamp_frame entry with the timestamp of the oldest trigger.
                    self.update_timestamp_frame(timestamp_frame_byfn, fn_corrected, timestamp_trigger, inc_val)
                    #rospy.logwarn ('time: %0.4f' % (rospy.Time.now().to_sec()-timestamp_frame_byfn[fn_corrected][0]))


                    # XXX hack? make data available via caminfo_byguid
                    caminfo = self.mainbrain.remote_api.caminfo_byguid[guid]
                    with caminfo['lock']:
                        caminfo['points_distorted'] = points_distorted

                # end try block
            # end for (coordinatesframe in list)

            
            if len(datarowlist_to_save):
                self.mainbrain.queue_data2d.put( datarowlist_to_save )

        # end lock


    def enqueue_3d_from_2d_points(self, 
                              fn,
                              points_pluecker_bycamn_byfn, 
                              timestamp_frame_byfn):
        
        #rospy.logwarn ('All camera data arrived.')
        if self.debug_level.isSet():
            rospy.logdebug('Frame: %d' % fn)

        if SHOW_3D_PROCESSING_LATENCY:
            start_3d_proc = rospy.Time.now().to_sec()

        if self.reconstructor is None:
            # can't do any 3D math without calibration information
            g_best_realtime_data = None
            return

        if DO_KALMAN:
            with self.lock_tracker:
                if self.tracker is None: # tracker isn't instantiated yet...
                    g_best_realtime_data = None
                    return

                points_pluecker_bycamn = points_pluecker_bycamn_byfn[fn]

                if self.save_profiling_data:
                    points_pluecker_bycamn_pickled = pickle.dumps(points_pluecker_bycamn)
                    self.data_dict_queue.append(('gob',(fn,
                                                        points_pluecker_bycamn_pickled,
                                                        self.guid_from_camn)))
                    
                points_pluecker_bycamn = self.tracker.calculate_a_posteriori_estimates(
                                                        fn,
                                                        points_pluecker_bycamn,
                                                        self.guid_from_camn)

                if self.debug_level.isSet():
                    rospy.logdebug('%d live objects' % self.tracker.live_tracked_objects.how_many_are_living())
                    results = self.tracker.live_tracked_objects.rmap( 'get_most_recent_data' ) # reverse map
                    Xs = []
                    for result in results:
                        if result is None:
                            return
                        obj_id,last_xhat,P = result
                        rospy.logdebug(last_xhat[:3])

                if self.save_profiling_data:
                    self.data_dict_queue.append(('ntrack',self.tracker.live_tracked_objects.how_many_are_living()))

                now = rospy.Time.now().to_sec()
                if SHOW_3D_PROCESSING_LATENCY:
                    start_3d_proc_a = now
                if self.show_overall_latency.isSet():
                    timestamp_oldest, n = timestamp_frame_byfn[ fn ]
                    if n>0:
                        if 0:
                            rospy.logwarn('Overall latency %d: %.1f msec (oldest: %s now: %s)' % (n,
                                                                                                  (now-timestamp_oldest)*1e3,
                                                                                                  repr(timestamp_oldest),
                                                                                                  repr(now)))
                        else:
                            rospy.logwarn( 'Overall latency (%d camera detected 2d points): %.1f msec (note: may exclude camera->camera computer latency)'%(n,
                                                                                                                                (now-timestamp_oldest)*1e3))

                if 1:
                    # The above calls
                    # self.enqueue_finished_tracked_object()
                    # when a tracked object is no longer
                    # tracked.

                    # Now, tracked objects have been updated (and their 2D data points
                    # removed from consideration), so we can use old flydra
                    # "hypothesis testing" algorithm on remaining data to see if there
                    # are new objects.

                    scale_factor = self.tracker.scale_factor
                    results = self.tracker.live_tracked_objects.rmap( 'get_most_recent_data' ) # reverse map
                    Xs = []
                    for result in results:
                        if result is None:
                            return
                        obj_id,last_xhat,P = result
                        X = last_xhat[0]/scale_factor, last_xhat[1]/scale_factor, last_xhat[2]/scale_factor
                        Xs.append(X)
                    if len(Xs):
                        g_best_realtime_data = Xs, 0.0
                    else:
                        g_best_realtime_data = None

                if SHOW_3D_PROCESSING_LATENCY:
                    start_3d_proc_b = rospy.Time.now().to_sec()

                # Convert to format accepted by find_best_3d()
                (found_data_dict, first_idx_byguid) = flydra_kalman_utils.convert_format(
                                                                        points_pluecker_bycamn,
                                                                        self.guid_from_camn,
                                                                        area_threshold=0.0,
                                                                        only_likely=True)

                if SHOW_3D_PROCESSING_LATENCY:
                    if len(found_data_dict) < 2:
                        rospy.logwarn(' ')
                    else:
                        rospy.logwarn('*')

                if len(found_data_dict) >= 2:
                    # Can't do any 3D math without at least 2 cameras giving good
                    # data.
                    try:
                        (this_observation_orig_units, 
                         this_observation_Lcoords_orig_units, 
                         guids_used,
                         min_mean_dist) = ru.hypothesis_testing_algorithm__find_best_3d(
                                                        self.reconstructor,
                                                        found_data_dict,
                                                        max_error,
                                                        max_n_cams=max_N_hypothesis_test,
                                                        )
                    except ru.NoAcceptablePointFound, err:
                        pass
                    else:
                        this_observation_camns = [self.camn_from_guid[guid] for guid in guids_used]
                        this_observation_idxs = [first_idx_byguid[camn] for camn in this_observation_camns] # zero idx
                        ####################################
                        #  Now join found point into Tracker
                        if self.save_profiling_data:
                            self.data_dict_queue.append(('join',(fn,
                                                                 this_observation_orig_units,
                                                                 this_observation_Lcoords_orig_units,
                                                                 this_observation_camns,
                                                                 this_observation_idxs
                                                                 )))
                        # test for novelty
                        believably_new = self.tracker.is_believably_new( this_observation_orig_units)
                        if believably_new:
                            self.tracker.join_new_obj( fn,
                                                       this_observation_orig_units,
                                                       this_observation_Lcoords_orig_units,
                                                       this_observation_camns,
                                                       this_observation_idxs
                                                       )
                if 1:
                    if self.tracker.live_tracked_objects.how_many_are_living():
                        data_packet = self.tracker.encode_data_packet(
                            fn,
                            timestamp_oldest,now)
                        if data_packet is not None:
                            self.realtime_kalman_data_queue.put(data_packet)
                            
                        results = self.tracker.live_tracked_objects.rmap( 'get_most_recent_data' )
                        ros_objects = []
                        for result in results:
                            if result is None:
                                return
                            obj_id,xhat,P = result
                            this_ros_object = flydra_object(obj_id=obj_id,
                                                            position=Point(*xhat[:3]),
                                                            velocity=Vector3(*xhat[3:6]),
                                                            posvel_covariance_diagonal=numpy.diag(P)[:6].tolist())
                            ros_objects.append( this_ros_object )
                        ros_packet = flydra_mainbrain_packet(framenumber=fn,
                                                             reconstruction_stamp=rospy.Time.from_sec(now),
                                                             acquire_stamp=rospy.Time.from_sec(timestamp_oldest),
                                                             objects = ros_objects)
                        self.realtime_ros_packets_queue.put( ros_packet )

                if SHOW_3D_PROCESSING_LATENCY:
                    start_3d_proc_c = rospy.Time.now().to_sec()

        else: # not DO_KALMAN:

            found_data_dict = {} # old "good" points will go in here
            for guid, this_point in data_dict.iteritems():
                if not numpy.isnan(this_point[0]): # only use if point was found
                    found_data_dict[guid] = this_point[:9]

            if len(found_data_dict) < 2:
                # Can't do any 3D math without at least 2
                # cameras giving good data.
                return

            try:
                # hypothesis testing algorithm
                (X, line3d, guids_used,min_mean_dist) = ru.hypothesis_testing_algorithm__find_best_3d(
                                                                        self.reconstructor,
                                                                        found_data_dict,
                                                                        max_n_cams=max_N_hypothesis_test,
                                                                        )
            except:
                # This prevents us from bombing this thread...
                traceback.print_exc()
                rospy.logwarn('SKIPPED 3d calculation for this frame.')
                return
            
            cam_nos_used = [self.camn_from_guid[guid] for guid in guids_used]

            if line3d is None:
                line3d = nan, nan, nan, nan, nan, nan
                line3d_valid = False
            else:
                line3d_valid = True

            find3d_time = rospy.Time.now().to_sec()

            x,y,z=X
            outgoing_data = [x,y,z]
            outgoing_data.extend( line3d ) # 6 component vector
            outgoing_data.append( find3d_time )

            if len(g_downstream_hosts):
                # This is for the non-Kalman data. See
                # tracker.encode_data_packet() for
                # Kalman version.
                data_packet = encode_data_packet(
                    fn,
                    line3d_valid,
                    outgoing_data,
                    min_mean_dist,
                    )

            # realtime 3d data
            g_best_realtime_data = [X], min_mean_dist
            try:
                for downstream_host in g_downstream_hosts:
                    nBytesTotal = len(data_packet)
                    nBytesSent = 0
                    while nBytesSent < nBytesTotal:
                        nBytes = g_socket_outgoing_UDP.sendto(data_packet[nBytesSent:],downstream_host)
                        nBytesSent += nBytes
            except:
                rospy.logwarn( 'Could not send 3d point data over UDP')
            if self.mainbrain.is_saving_data():
                self.mainbrain.queue_data3d_best.put( (fn,
                                                        outgoing_data,
                                                        cam_nos_used,
                                                        min_mean_dist) )
        if SHOW_3D_PROCESSING_LATENCY:
            stop_3d_proc = rospy.Time.now().to_sec()
            dur_3d_proc_msec = (stop_3d_proc - start_3d_proc)*1e3
            dur_3d_proc_msec_a = (start_3d_proc_a - start_3d_proc)*1e3
            dur_3d_proc_msec_b = (start_3d_proc_b - start_3d_proc)*1e3
            dur_3d_proc_msec_c = (start_3d_proc_c - start_3d_proc)*1e3

            rospy.logwarn('dur_3d_proc_msec % 3.1f % 3.1f % 3.1f % 3.1f'%(dur_3d_proc_msec,
                                                                          dur_3d_proc_msec_a,
                                                                          dur_3d_proc_msec_b,
                                                                          dur_3d_proc_msec_c))
                            

    def run(self):
        """Main loop of CoordinateProcessor"""
        global g_downstream_hosts
        global g_best_realtime_data
        global g_socket_outgoing_UDP
        global g_XXX_framenumber


        # Improve our thread priority.
        if os.name == 'posix':
            try:
                #priority = posix_sched.get_priority_max( posix_sched.FIFO )
                priority = 1 #41 #posix_sched.get_priority_min( posix_sched.FIFO )  # Faster than user procs, slower than kernel procs: rtprio=41.
                sched_params = posix_sched.SchedParam(priority)
                rv = posix_sched.setscheduler(0, posix_sched.FIFO, sched_params)
                rospy.logwarn('Excellent (%d), 3D reconstruction thread running in maximum priority mode' % rv)
            except Exception, x:
                import ctypes
                # The 186 comes from the command:  grep -r _gettid /usr/include/*
                # and may vary on linux flavor, 32/64 bitness, etc.
                rospy.logwarn('WARNING: Could not change to FIFO priority=%d, <threadID>=%d: %s' % (priority, ctypes.CDLL('libc.so.6').syscall(186), str(x))) 
                rospy.logwarn('You can set this manually via:')
                rospy.logwarn('sudo chrt -f -p 1 %d' % ctypes.CDLL('libc.so.6').syscall(186))


        points_undistorted_byguid_byfn = {}
        timestamp_trigger_byguid_byfn = {}
        points_pluecker_bycamn_byfn = collections.defaultdict(dict)
        timestamp_frame_byfn = {}

        fn_corrected_set = set()

        max_error = self.mainbrain.get_hypothesis_test_max_error()


        debug_drop_fd = None

        while not self.quit_event.isSet():
            
            ########################################################################
            # Get the coordinatesframes from the queue, and put them in their appropriate dicts.
            ########################################################################

            coordinatesframes_list = self.realreceiver.get_coordinatesframes()
            if not len(coordinatesframes_list):
                continue


            if BENCHMARK_GATHER:
                timestamp_benchmark_list = []
            else:
                timestamp_benchmark_list = None
                
            # Put the coordinatesframes data into the various points & timestamps dicts.                
            self.collect_framedata_from_coordinatesframes_list (points_undistorted_byguid_byfn, 
                                                                points_pluecker_bycamn_byfn, 
                                                                timestamp_trigger_byguid_byfn, 
                                                                timestamp_frame_byfn, 
                                                                fn_corrected_set,
                                                                coordinatesframes_list,
                                                                timestamp_benchmark_list)
            
            for guid in self.resync_byguid:            
                if self.resync_byguid[guid]:
                    # Delete all for this guid, so we can start over.
                    self.OnSynchronize( guid, 
                                        #timestamp_trigger,
                                        points_undistorted_byguid_byfn,
                                        timestamp_trigger_byguid_byfn,
                                        points_pluecker_bycamn_byfn,
                                        timestamp_frame_byfn,
                                        fn_corrected_set)
                    
                    self.mainbrain.timestamp_modeler.reset_id(guid)#self.camn_from_guid[guid])
                    self.resync_byguid[guid] = False

            # NOTE: The above resync_byguid[] loop requires the following to be changed in motmot/fview_ext_trig.live_timestamp_model.py:
            #
            #    def reset_id(self, id_string):
            #        if id_string in self.last_frame:
            #            del self.last_frame[id_string]
            #        
            #    def register_frame(self, id_string, framenumber, frame_timestamp, full_output=False):
            #        frame_timestamp = time.time()
            #
            #        if frame_timestamp is not None:
            #            last_frame_timestamp = self.last_frame.get(id_string, frame_timestamp)#-np.inf) # frame_timestamp)#
            #            this_interval = abs(frame_timestamp-last_frame_timestamp)
            #
            #            did_frame_offset_change = False
            #            if this_interval > self.sync_interval:
            #                if self.block_activity:
            #                    print('changing frame offset is disallowed, but you attempted to do it. ignoring.')
            #                else:
            #                    # re-synchronize camera
            #
            #                    # XXX need to figure out where frame offset of two comes from:
            #                    # The 2 seems to mean that upon a resync, the corrected_framenumber = framenumber-(framenumber-2) = 2
            #                    self.frame_offsets[id_string] = framenumber-2
            #                    did_frame_offset_change = True
            #
            #            self.last_frame[id_string] = frame_timestamp
            #
            #            if did_frame_offset_change:
            #                self.frame_offset_changed = True # fire any listeners
            #
            #        # Get the output results.
            #        result = self.gain_offset_residuals
            #        if result is None: # Not enough data
            #            trigger_timestamp = None
            #            corrected_framenumber = None
            #        else:
            #            gain,offset,residuals = result
            #            try:
            #                corrected_framenumber = framenumber-self.frame_offsets[id_string]
            #            except KeyError:
            #                corrected_framenumber = framenumber
            #                
            #            trigger_timestamp = corrected_framenumber*gain + offset
            #    
            #        # Format the return value.
            #        if full_output:
            #            rv = trigger_timestamp, corrected_framenumber, did_frame_offset_change
            #        else:
            #            rv = trigger_timestamp
            #                
            #        return rv




            if BENCHMARK_GATHER:
                timestamp_finish_frame_sorting = rospy.Time.now().to_sec()
                min_packet_gather_dur = timestamp_finish_frame_sorting - max(timestamp_benchmark_list)
                max_packet_gather_dur = timestamp_finish_frame_sorting - min(timestamp_benchmark_list)
                rospy.logwarn('proc dur: % 3.1f % 3.1f' % (min_packet_gather_dur*1e3,
                                                           max_packet_gather_dur*1e3))


            ########################################################################
            # We've grabbed the coordinates from all the waiting frames. 
            # Now calculate 3D info.
            ########################################################################
            
            with self.lock_alldata:
                fn_finished_list = [] # for quick deletion

                #rospy.logwarn('fn_corrected_set=%s' %list(fn_corrected_set))
                for fn in fn_corrected_set:
                    (timestamp_frame, n) = timestamp_frame_byfn[fn]
                    if timestamp_frame is None:
                        #rospy.logwarn( 'No latency estimate available -- skipping 3D reconstruction'
                        continue
                    
                    # Print latency per frame.
                    #rospy.logwarn ('frame %d has %s, latency %0.4f, oldest=%0.4f' % (fn,
                    #                                                   points_undistorted_byguid_byfn[fn].keys(),
                    #                                                   (rospy.Time.now().to_sec() - timestamp_frame),
                    #                                                   timestamp_frame))
                    
                    # Print latency per guid.
                    #for guid in points_undistorted_byguid_byfn[fn].keys():
                    #    rospy.logwarn('guid=%s has latency %0.4f' % (guid,
                    #                                                 rospy.Time.now().to_sec() - timestamp_trigger_byguid_byfn[fn][guid]))
                        
                    # Skip processing if latency is too long.
                    if (rospy.Time.now().to_sec() - timestamp_frame) > max_reconstruction_latency_sec:
                        rospy.logwarn( 'Maximum reconstruction latency exceeded (%0.4f>%0.4f) -- skipping 3D reconstruction' \
                                        % ((rospy.Time.now().to_sec() - timestamp_frame), max_reconstruction_latency_sec))
                        continue
                    
                    # Print count of unprocessed frames.
                    #rospy.logwarn( 'latency %0.4f>%0.4f, #frames of: undistorted)=%d/%d, pluecker=%d, timestamp_oldest=%d' % ((rospy.Time.now().to_sec() - timestamp_frame), 
                    #                                                    max_reconstruction_latency_sec,
                    #                                                    len(points_undistorted_byguid_byfn),
                    #                                                    len(points_undistorted_byguid_byfn[fn]),
                    #                                                    len(points_pluecker_bycamn_byfn),
                    #                                                    len(timestamp_frame_byfn)))
                        
                    points_undistorted_byguid = points_undistorted_byguid_byfn[fn]

                    #rospy.logwarn ('len(points_undistorted_byguid)==len(self.guid_list): %d==%d' % (len(points_undistorted_byguid),len(self.guid_list)))
                    if (len(points_undistorted_byguid)==len(self.guid_list)): # all camera data arrived
                        self.enqueue_3d_from_2d_points(fn, 
                                                       points_pluecker_bycamn_byfn, 
                                                       timestamp_frame_byfn)
                        # Mark for deletion.
                        fn_finished_list.append(fn)

                # end for (fn in fn_corrected_set)
                
                    
                # For each frame where all cameras have delivered, check that timestamps are in reasonable agreement (low priority)
                for fn in fn_finished_list:
                    timestamp_frame_list = []
                    for guid, timestamp_trigger_tmp in timestamp_trigger_byguid_byfn[fn].iteritems():
                        timestamp_frame_list.append(timestamp_trigger_tmp)

                    if self.show_sync_errors:
                        if len(timestamp_frame_list):
                            diff = abs(max(timestamp_frame_list)-min(timestamp_frame_list))
                            if diff > 0.005:
                                rospy.logwarn('Timestamps off by %0.3f (more than 0.005 sec) -- synchronization error' % diff)
                # end for fn in fn_finished_list

                    
                # Clean up finished frame records.
                for fn in fn_finished_list:
                    #rospy.logwarn('Deleting %d' % fn)
                    del points_undistorted_byguid_byfn[fn]
                    del timestamp_trigger_byguid_byfn[fn]
                    del timestamp_frame_byfn[fn]
                    try:
                        del points_pluecker_bycamn_byfn[fn]
                    except KeyError:
                        pass


                # If too many points, only keep the last 50.
                # This is only needed when multiple cameras are not
                # synchronized, (When camera-camera frame
                # correspondences are unknown.)
                if len(points_undistorted_byguid_byfn)>100:
                    rospy.logwarn('Cameras not synchronized or network dropping packets -- unmatched 2D data accumulating: len=%d' % len(points_undistorted_byguid_byfn))
                    fn_list = points_undistorted_byguid_byfn.keys()
                    fn_list.sort()

                    if 1:
                        # get one sample
                        fn_corrected = fn_list[0]
                        data_dict = points_undistorted_byguid_byfn[fn_corrected]
                        this_guid_list = data_dict.keys()
                        missing_guid_guess = list(set(self.guid_list) - set( this_guid_list ))
                        if len(missing_guid_guess):
                            rospy.logwarn('A guess at missing guid(s): %s' % list(set(self.guid_list)-set(this_guid_list)))

                    for fn in fn_list[:-50]:
                        del points_undistorted_byguid_byfn[fn]
                        del timestamp_trigger_byguid_byfn[fn]


                # If too many points, only keep the last 50.
                if len(points_pluecker_bycamn_byfn)>100:
                    rospy.logwarn('Deleting unused 3D data (this should be a rare occurrance)')
                    fn_list = points_pluecker_bycamn_byfn.keys()
                    fn_list.sort()
                    for fn in fn_list[:-50]:
                        del points_pluecker_bycamn_byfn[fn]

                # If too many points, only keep the last 50.
                if len(timestamp_frame_byfn)>100:
                    fn_list = timestamp_frame_byfn.keys()
                    fn_list.sort()
                    for fn in fn_list[:-50]:
                        del timestamp_frame_byfn[fn]


        if DO_KALMAN:
            with self.lock_tracker:
                if self.tracker is not None:
                    self.tracker.kill_all_trackers() # save (if necessary) all old data

class Mainbrain(object):
    """Handle all camera network stuff and interact with application"""

    class RemoteAPI(Pyro.core.ObjBase):

        # ================================================================
        #
        # Methods called locally
        #
        # ================================================================

        def get_version(self):
            return flydra.version.__version__

        def post_init(self, mainbrain):
            """call after __init__"""
            # let Pyro handle __init__
            self.caminfo_byguid = {}
            self.lock_caminfo = threading.Lock()
            self.lock_changed_cam = threading.Lock()
            self.event_no_cams = threading.Event()
            self.event_no_cams.set()
            with self.lock_changed_cam:
                self.new_guids = []
                self.old_guids = []
            self.mainbrain = mainbrain

            # threading control locks
            self.quit_now = threading.Event()
            self.thread_done = threading.Event()
            self.message_queue = Queue.Queue()

        def external_get_and_clear_pending_cams(self):
            with self.lock_changed_cam:
                new_guids = self.new_guids
                self.new_guids = []
                old_guids = self.old_guids
                self.old_guids = []
            return new_guids, old_guids

        def external_get_guids(self):
            with self.lock_caminfo:
                guids = self.caminfo_byguid.keys()
            guids.sort()
            return guids

        def external_get_info(self, guid):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    scalar_control_info = copy.deepcopy(caminfo['scalar_control_info'])
                    fqdn = caminfo['fqdn']
                    port = caminfo['port']
            return scalar_control_info, fqdn, port

        def external_get_image_fps_points(self, guid):
            ### XXX should extend to include lines
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    coord_and_image = caminfo['image']
                    fps = caminfo['fps']
                    points_distorted = caminfo['points_distorted'][:]
            # NB: points are distorted (and therefore align
            # with distorted image)
            if coord_and_image is not None:
                image_coords, image = coord_and_image
            else:
                image_coords, image = None, None
            return image, fps, points_distorted, image_coords

        def external_send_set_camera_property( self, guid, property_name, value):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['commands'].setdefault('set',{})[property_name]=value
                    old_value = caminfo['scalar_control_info'][property_name]
                    if type(old_value) == tuple and type(value) == int:
                        # i.e. (current, min, max)
                        caminfo['scalar_control_info'][property_name] = (value, old_value[1], old_value[2])
                    else:
                        caminfo['scalar_control_info'][property_name] = value

        def external_request_image_async(self, guid):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['commands']['get_im']=None

        def external_start_recording( self, guid, raw_file_basename):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['commands']['start_recording']=raw_file_basename

        def external_stop_recording( self, guid):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['commands']['stop_recording']=None

        def external_start_small_recording( self, guid,
                                            small_filebasename):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['commands']['start_small_recording']=small_filebasename

        def external_stop_small_recording( self, guid):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['commands']['stop_small_recording']=None

        def external_quit( self, guid):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['commands']['quit']=True

        def external_take_background( self, guid):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['commands']['take_bg']=None

        def external_request_missing_data(self, guid, camn, offset_fn, fn_missing_list):
            with self.lock_caminfo:
                if guid not in self.caminfo_byguid:
                    # the camera was dropped, ignore this request
                    return
                caminfo = self.caminfo_byguid[guid]

                camn_and_list = [camn, offset_fn]
                camn_and_list.extend( fn_missing_list )
                cmd_str = ' '.join(map(repr, camn_and_list))
                with caminfo['lock']:
                    caminfo['commands']['request_missing']=cmd_str

        def external_clear_background( self, guid):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['commands']['clear_bg']=None

        def external_set_cal( self, guid, pmat, intlin, intnonlin, scale_factor):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['commands']['cal']= pmat, intlin, intnonlin, scale_factor
                    caminfo['is_calibrated'] = True
        # === thread boundary =========================================

        def listen(self, daemon):
            """thread mainloop"""
            while not self.quit_now.isSet():
                try:
                    daemon.handleRequests(0.1) # block on select for n seconds
                except select.error, err:
                    rospy.logwarn('Exception in RemoteAPI.listen(): %s' % err)
                    continue

                with self.lock_caminfo:
                    guids = self.caminfo_byguid.keys()
                    
                for guid in guids:
                    with self.caminfo_byguid[guid]['lock']:
                        connected = self.caminfo_byguid[guid]['caller'].connected
                    if not connected:
                        rospy.logwarn( 'mainbrain lost camera %s at %s'%(guid, time.asctime()))
                        self.close_camera(guid)
            self.thread_done.set()

        # ================================================================
        #
        # Methods called remotely from listeners
        #
        # These all get called in their own thread.  Don't call across
        # the thread boundary without using locks, especially to GUI
        # or OpenGL.
        #
        # ================================================================

        def register_downstream_kalman_host(self,host,port):
            global g_downstream_kalman_hosts
            addr = (host,port)
            if addr not in g_downstream_kalman_hosts:
                rospy.logwarn('Appending to kalman host list: %s' % addr)
                g_downstream_kalman_hosts.append( (host,port) )
            else:
                rospy.logwarn('Already in kalman host list: %s' % addr)

        def remove_downstream_kalman_host(self,host,port):
            global g_downstream_kalman_hosts
            host_tuple = (host,port)
            try:
                i = g_downstream_kalman_hosts.index( host_tuple )
            except ValueError:
                return # could not find entry
            del g_downstream_kalman_hosts[i]

        # ================================================================
        #
        # Methods called remotely from cameras
        #
        # These all get called in their own thread.  Don't call across
        # the thread boundary without using locks, especially to GUI
        # or OpenGL.
        #
        # ================================================================

        def register_camera(self, cam_no, scalar_control_info, port, guid=None):
            """register new camera, return guid (caller: remote camera)"""

            caller= self.daemon.getLocalStorage().caller # XXX Pyro hack??
            caller_addr= caller.addr
            caller_ip, caller_port = caller_addr
            fqdn = socket.getfqdn(caller_ip)

            if guid is None:
                guid = '%s_%d'%(fqdn,cam_no)

            port_coordinates = self.mainbrain.coord_processor.connect(guid)
            with self.lock_caminfo:
                self.caminfo_byguid[guid] = {'commands':{}, # command queue for cam
                                         'lock':threading.Lock(), # prevent concurrent access
                                         'image':None,  # most recent image from cam
                                         'fps':None,    # most recept fps from cam
                                         'points_distorted':[], # 2D image points
                                         'caller':caller,
                                         'scalar_control_info':scalar_control_info,
                                         'fqdn':fqdn,
                                         'port':port,
                                         'is_calibrated':False, # has 3D calibration been sent yet?
                                         }
            self.event_no_cams.clear()
            with self.lock_changed_cam:
                self.new_guids.append(guid)
            return guid

        def set_image(self, guid, coord_and_image):
            """set most recent image (caller: remote camera)"""
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['image'] = coord_and_image

        def receive_missing_data(self, guid, offset_fn, missing_data ):
            #rospy.logwarn('Received missing data from camera %s (offset %d):' % (guid, offset_fn))
            if len(missing_data)==0:
                # no missing data
                return

            datarowlist_to_save = []
            for (camn, framenumber, remote_timestamp, timestamp_received,
                 points_distorted) in missing_data:

                fn_corrected = framenumber-offset_fn
                if len(points_distorted)==0:
                    # no point was tracked that frame
                    points_distorted = [(nan,nan,nan,nan,nan,nan,nan,nan,nan,False,0,0,0,0)] # same as no_point_tuple
                for point_tuple in points_distorted:
                    # Save 2D data (even when no point found) to allow
                    # temporal correlation of movie frames to 2D data.
                    try:
                        i_point = point_tuple[PT_TUPLE_IDX_FRAME_PT_IDX]
                        cur_val = point_tuple[PT_TUPLE_IDX_CUR_VAL_IDX]
                        mean_val = point_tuple[PT_TUPLE_IDX_MEAN_VAL_IDX]
                        sumsqf_val = point_tuple[PT_TUPLE_IDX_SUMSQF_VAL_IDX]
                    except:
                        rospy.logerror('While appending point_tuple %s' % point_tuple)
                        raise
                    if fn_corrected is None:
                        # don't bother saving if we don't know when it was from
                        continue
                    datarowlist_to_save.append((camn, # defer saving to later
                                             fn_corrected,
                                             remote_timestamp, timestamp_received)
                                            +point_tuple[:5]
                                            +(i_point,cur_val,mean_val,sumsqf_val))
            self.mainbrain.queue_data2d.put(datarowlist_to_save)

        def set_fps(self, guid, fps):
            """set most recent fps (caller: remote camera)"""
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    caminfo['fps'] = fps

        def get_and_clear_commands(self, guid):
            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                with caminfo['lock']:
                    cmds = caminfo['commands']
                    caminfo['commands'] = {}
            return cmds

        def get_coordinates_port(self, guid):
            """Send port number to which camera should send realtime data"""
            port_coordinates = self.mainbrain.coord_processor.port_coordinates_from_guid(guid)
            return port_coordinates

        def log_message(self, guid, timestamp_host, message):
            timestamp_mainbrain = rospy.Time.now().to_sec()
            rospy.logwarn('Received log message from %s: %s' % (guid, message))
            self.message_queue.put( (timestamp_mainbrain, guid, timestamp_host, message) )

        def close_camera(self, guid):
            """gracefully say goodbye (caller: remote camera)"""
            with self.lock_caminfo:
                self.mainbrain.coord_processor.disconnect(guid)
                del self.caminfo_byguid[guid]
                if not len(self.caminfo_byguid):
                    self.event_no_cams.set()
                with self.lock_changed_cam:
                    self.old_guids.append(guid)

    ######## end of RemoteAPI class
    
    
    # Not finished implementing the ProvideRosInterface()...
    class ProvideRosInterface():
        def post_init(self, mainbrain):
            """call after __init__"""
            # let Pyro handle __init__
            self.caminfo_byguid = {}
            self.lock_caminfo = threading.Lock()
            self.lock_changed_cam = threading.Lock()
            self.event_no_cams = threading.Event()
            self.event_no_cams.set()
            with self.lock_changed_cam:
                self.new_guids = []
                self.old_guids = []
            self.mainbrain = mainbrain

            # threading control locks
            self.quit_now = threading.Event()
            self.thread_done = threading.Event()
            self.message_queue = Queue.Queue()


            # Expose the flydra_mainbrain API functions as ROS services.  Same as the Pyro function call, except with a few parameters pickled.
            rospy.Service ('mainbrain/get_version',              SrvGetVersion, self.callback_get_version)
            rospy.Service ('mainbrain/register_camera',          SrvRegisterCamera, self.callback_register_camera)
            rospy.Service ('mainbrain/get_coordinates_port',     SrvGetCoordinatesPort, self.callback_get_coordinates_port)
            rospy.Service ('mainbrain/get_and_clear_commands',   SrvGetAndClearCommands, self.callback_get_and_clear_commands)
            rospy.Service ('mainbrain/set_fps',                  SrvSetFps, self.callback_set_fps)
            rospy.Service ('mainbrain/set_image',                SrvSetImage, self.callback_set_image)
            rospy.Service ('mainbrain/log_message',              SrvLogMessage, self.callback_log_message)
            rospy.Service ('mainbrain/receive_missing_data',     SrvReceiveMissingData, self.callback_receive_missing_data)
            rospy.Service ('mainbrain/close_camera',             SrvClose, self.callback_close_camera)
            rospy.Service ('mainbrain/get_recording_status',     SrvGetRecordingStatus, self.callback_get_recording_status)

        
        ###########################################################################
        # Callbacks    
        ###########################################################################
    
        def callback_get_version (self, srvreqGetVersion):
            #srvrespGetVersion = SrvGetVersionResponse()
            return {'version': flydra.version.__version__}
    
            
        def callback_register_camera (self, srvreqRegisterCamera):
            """Register camera, return guid (caller: remote camera)"""

            # Unpack the parameters.
            cam_no = srvreqRegisterCamera.cam_no
            scalar_control_info = pickle.loads(srvreqRegisterCamera.pickled_scalar_control_info)
            port = srvreqRegisterCamera.port
            guid = srvreqRegisterCamera.guid
    
            if guid is None:
                guid = 'camera_%d' % cam_no

            self.mainbrain.echotimestamp.register_camera(guid)
            
            port_coordinates = self.mainbrain.coord_processor.connect(guid)
            with self.lock_caminfo:
                self.caminfo_byguid[guid] = {'commands':{}, # command queue for cam
                                         'lock':threading.Lock(), # prevent concurrent access
                                         'image':None,  # most recent image from cam
                                         'fps':None,    # most recept fps from cam
                                         'points_distorted':[], # 2D image points
                                         'scalar_control_info':scalar_control_info,
                                         'port':port,
                                         'is_calibrated':False, # has 3D calibration been sent yet?
                                         }
            self.event_no_cams.clear()
            with self.lock_changed_cam:
                self.new_guids.append(guid)

            return {'guid': guidNew}
    
    
    
        def callback_get_coordinates_port (self, srvreqGetCoordinatesPort):
            """Send port number to which camera should send realtime data"""
            guid = srvreqGetCoordinatesPort.guid

            port_coordinates = self.mainbrain.coord_processor.port_coordinates_from_guid(guid)
            return {'port': port_coordinates}

    
    
        def callback_get_and_clear_commands (self, srvreqGetAndClearCommands):
            guid = srvreqGetAndClearCommands.guid

            with self.lock_caminfo:
                caminfo = self.caminfo_byguid[guid]
                
                with caminfo['lock']:
                    cmds = caminfo['commands']
                    caminfo['commands'] = {}

            return {'pickled_cmds': pickle.dumps(cmds)}

        
        
        def callback_set_fps (self, srvreqSetFps):
            """set most recent fps (caller: remote camera)"""
            guid = srvreqSetFps.guid
            fps = srvreqSetFps.fps
            self.mainbrain.remote_api.set_fps(guid, fps)

            return {}

    
        def callback_set_image (self, srvreqSetImage):
            """set most recent image (caller: remote camera)"""
            guid = srvreqSetImage.guid
            coord_and_image = pickle.loads(srvreqSetImage.pickled_coord_and_image)
            self.mainbrain.remote_api.set_image(guid, coord_and_image)

            return {}
    
    
        def callback_log_message (self, srvreqLogMessage):
            guid = srvreqLogMessage.guid
            timestamp_host = srvreqLogMessage.host_timestamp
            message = srvreqLogMessage.message
            self.mainbrain.remote_api.log_message (guid, timestamp_host, message)

            return {}

    
        def callback_receive_missing_data (self, srvreqReceiveMissingData):
            guid = srvreqReceiveMissingData.guid
            offset_fn = srvreqReceiveMissingData.framenumber_offset
            missing_data = pickle.loads(srvreqReceiveMissingData.pickled_missing_data)
            self.mainbrain.remote_api.receive_missing_data (guid, offset_fn, missing_data)

            return {}    
    
    
        def callback_close_camera (self, srvreqClose):
            """gracefully say goodbye (caller: remote camera)"""
            guid = srvreqClose.guid
            self.mainbrain.echotimestamp.deregister_camera(guid)
            self.mainbrain.remote_api.close_camera(guid)

            return {}

    
        
    
        # Not sure if mainbrain uses this anymore.
        def callback_get_recording_status (self, srvreqGetRecordingStatus):        
#            msg = None
#            if self.socketTriggerRecording is not None:
#                try:
#                    msg, addr = self.socketTriggerRecording.recvfrom(4096) # Call mainbrain to get any trigger recording commands.
#                except socket.error, err:
#                    if err.args[0] == 11: #Resource temporarily unavailable
#                        pass
#    
#            #rospy.logwarn( ">>> %s <<< %s" % (msg, self.isRecording)) 
#            
#            if msg=='record_ufmf':
#                if self.isRecording==False:
#                    self.isRecording = True
#                    rospy.logwarn('Start saving video.')
#            
#            elif msg==None:
#                if (self.isRecording==True) and (rospy.Time.now().to_sec() - self.timeRecord >= 4): # Record at least 4 secs of video.
#                    self.isRecording = False
#                    rospy.logwarn('Stop saving video.')
#    
#            self.timeRecord = rospy.Time.now().to_sec()
                    
    
            return {'status': False}#self.isRecording}
    
    
            
        def callback_coordinates(self, srvreqCoordinates):
            guid = srvreqCoordinates.guid
            data = srvreqCoordinates.data
            self.mainbrain.coord_processor.realreceiver.coordinatesframe_queue.put((guid, data))
    
            return {}
        
    
    ######## end of ProvideRosInterface class



    ######## Continue with the Mainbrain class...

    def __init__(self,
                 server=None,
                 save_profiling_data=False, 
                 show_sync_errors=True):
        
        global g_mainbrain_keeper, g_hostname

        import motmot.fview_ext_trig.ttrigger
        import motmot.fview_ext_trig.live_timestamp_modeler

        rospy.init_node('flydra_mainbrain', log_level=LOGLEVEL)

        if server is not None:
            g_hostname = server
        rospy.logwarn('Running mainbrain at hostname: %s' % g_hostname)

        assert tables.__version__ >= '1.3.1' # bug was fixed in pytables 1.3.1 where HDF5 file kept in inconsistent state

        self.debug_level = threading.Event()
        self.show_overall_latency = threading.Event()

        self.lock_trigger_device = threading.Lock()
        with self.lock_trigger_device:
            self.trigger_device = motmot.fview_ext_trig.ttrigger.DeviceModel()
            self.trigger_device.frames_per_second = rc_params['frames_per_second']
            self.timestamp_modeler = motmot.fview_ext_trig.live_timestamp_modeler.LiveTimestampModeler()
            self.timestamp_modeler.set_trigger_device( self.trigger_device )

        Pyro.core.initServer(banner=0)

        port = rospy.get_param('mainbrain/port_mainbrain', 9833)

        # start Pyro server
        daemon = Pyro.core.Daemon(host=g_hostname,port=port)
        remote_api = Mainbrain.RemoteAPI(); remote_api.post_init(self)
        URI=daemon.connect(remote_api, 'mainbrain')

        # create (but don't start) listen thread
        self.listen_thread=threading.Thread(target=remote_api.listen,
                                            name='RemoteAPI-Thread',
                                            args=(daemon,))
        #self.listen_thread.setDaemon(True) # don't let this thread keep app alive
        self.remote_api = remote_api

        self.rosinterface = self.ProvideRosInterface()
        self.rosinterface.post_init(self)
        
        
        self._new_camera_functions = []
        self._old_camera_functions = []

        self.last_requested_image = {}
        self.pending_requests = {}
        self.last_set_param_time = {}

        self.socket_outgoing_latency_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self.num_cams = 0
        self.Mainbrain_guids_copy = [] # Keep a copy of all guids connected
        self._fqdns_by_guid = {}
        self.set_new_camera_callback(self.IncreaseCamCounter)
        self.set_new_camera_callback(self.SendExpectedFPS)
        self.set_new_camera_callback(self.SendCalibration)
        self.set_old_camera_callback(self.DecreaseCamCounter)

        self.last_saved_data_time = 0.0
        self.last_trigger_framecount_check_time = 0.0

        self._currently_recording_movies = {}

        self.reconstructor = None

        # Attributes which come in use when saving data occurs
        self.h5file = None
        self.h5data2d = None
        self.h5cam_info = None
        self.h5host_clock_info = None
        self.h5trigger_clock_info = None
        self.h5movie_info = None
        self.h5textlog = None
        if DO_KALMAN:
            self.h5data3d_kalman_estimates = None
            self.h5data3d_kalman_observations = None
            self.h5_2d_obs = None
        else:
            self.h5data3d_best = None

        # Queues of information to save
        self.queue_data2d          = Queue.Queue()
        self.queue_cam_info        = Queue.Queue()
        self.queue_host_clock_info = Queue.Queue()
        self.queue_trigger_clock_info = Queue.Queue()
        self.queue_data3d_best     = Queue.Queue()

        self.queue_data3d_kalman_estimates = Queue.Queue()

        self.hypothesis_test_max_error = LockedValue(
            rc_params['hypothesis_test_max_acceptable_error']) # maximum reprojection error

        self.coord_processor = CoordinateProcessor(self,
                                                   save_profiling_data=save_profiling_data,
                                                   debug_level=self.debug_level,
                                                   show_overall_latency=self.show_overall_latency,
                                                   show_sync_errors=show_sync_errors,
                                                   )
        #self.coord_processor.setDaemon(True)
        self.coord_processor.start()

        self.trig_receiver = TrigReceiver(self)
        self.trig_receiver.setDaemon(True)
        self.trig_receiver.start()


        if USE_ROS_INTERFACE:
            self.echotimestamp = ThreadEchoTimestamp()
        else:
            self.timestamp_echo_receiver = TimestampEchoReceiver(self)
            self.timestamp_echo_receiver.setDaemon(True)
            self.timestamp_echo_receiver.start()
        

        g_mainbrain_keeper.register(self)


    def get_fps(self):
        return self.trigger_device.frames_per_second_actual

    def set_fps(self,fps):
        self.do_synchronization(fps_new=fps)

    def do_synchronization(self, fps_new=None):
        rospy.logwarn('DO_SYNC====================================================')
        if self.is_saving_data():
            raise RuntimeError('Will not (re)synchronize while saving data')

        if fps_new is not None:
            self.trigger_device.frames_per_second = fps_new
            fps_actual = self.trigger_device.frames_per_second_actual

        self.timestamp_modeler.synchronize = True # fire event handler
        
        if fps_new is not None:
            guids = self.remote_api.external_get_guids()
            for guid in guids:
                try:
                    self.send_set_camera_property(guid, 'expected_trigger_framerate', fps_actual )
                except Exception,err:
                    rospy.logwarn('Exception with send_set_camera_property(): %s' % err)
            rc_params['frames_per_second'] = fps_actual
            save_rc_params()
        
        self.coord_processor.b_dosync = True
        

    def get_hypothesis_test_max_error(self):
        return self.hypothesis_test_max_error.get()

    def set_hypothesis_test_max_error(self,val):
        self.hypothesis_test_max_error.set(val)
        rc_params['hypothesis_test_max_acceptable_error'] = val
        save_rc_params()

    def IncreaseCamCounter(self,guid,scalar_control_info,fqdn_and_port):
        self.num_cams += 1
        self.Mainbrain_guids_copy.append( guid )

    def SendExpectedFPS(self,guid,scalar_control_info,fqdn_and_port):
        self.send_set_camera_property( guid, 'expected_trigger_framerate', self.trigger_device.frames_per_second_actual )

    def SendCalibration(self,guid,scalar_control_info,fqdn_and_port):
        if self.reconstructor is not None and guid in self.reconstructor.get_guids():
            pmat = self.reconstructor.get_pmat(guid)
            intlin = self.reconstructor.get_intrinsic_linear(guid)
            intnonlin = self.reconstructor.get_intrinsic_nonlinear(guid)
            scale_factor = self.reconstructor.get_scale_factor()
            self.remote_api.external_set_cal( guid, pmat, intlin, intnonlin, scale_factor)

    def DecreaseCamCounter(self,guid):
        try:
            idx = self.Mainbrain_guids_copy.index( guid )
        except ValueError, err:
            rospy.logwarn('DecreaseCamCounter() called with non-existant guid: %s' % err)
            return
        self.num_cams -= 1
        del self.Mainbrain_guids_copy[idx]

    def get_num_cams(self):
        return self.num_cams

    def get_scalarcontrolinfo(self, guid):
        sci, fqdn, port = self.remote_api.external_get_info(guid)
        return sci

    def get_widthheight(self, guid):
        sci, fqdn, port = self.remote_api.external_get_info(guid)
        w = sci['width']
        h = sci['height']
        return w,h

    def get_roi(self, guid):
        sci, fqdn, port = self.remote_api.external_get_info(guid)
        lbrt = sci['roi']
        return lbrt

    def get_all_params(self):
        guids = self.remote_api.external_get_guids()
        all = {}
        for guid in guids:
            sci, fqdn, port = self.remote_api.external_get_info(guid)
            all[guid] = sci
        return all

    def start_listening(self):
        # start listen thread
        #self.listen_thread.setDaemon(True)
        self.listen_thread.start()

    def set_new_camera_callback(self,handler):
        self._new_camera_functions.append(handler)

    def set_old_camera_callback(self,handler):
        self._old_camera_functions.append(handler)

    def service_pending(self):
        """the Mainbrain application calls this fairly frequently (e.g. every 100 msec)"""
        new_guids, old_guids = self.remote_api.external_get_and_clear_pending_cams()
        for guid in new_guids:
            if guid in old_guids:
                continue # inserted and then removed
            if self.is_saving_data():
                raise RuntimeError("Cannot add new camera while saving data")
            scalar_control_info, fqdn, port = self.remote_api.external_get_info(guid)
            for new_cam_func in self._new_camera_functions:
                new_cam_func(guid,scalar_control_info,(fqdn,port))

        for guid in old_guids:
            for old_cam_func in self._old_camera_functions:
                old_cam_func(guid)

        now = rospy.Time.now().to_sec()
        diff = now - self.last_saved_data_time
        if diff >= 5.0: # request missing data and save data every 5 seconds
            self._request_missing_data()
            self._service_save_data()
            self.last_saved_data_time = now

        diff = now - self.last_trigger_framecount_check_time
        if diff >= 5.0:
            self._trigger_framecount_check()
            self.last_trigger_framecount_check_time = now

        self._check_latencies()

    def _trigger_framecount_check(self):
        import motmot.fview_ext_trig.live_timestamp_modeler
        try:
            tmp = self.timestamp_modeler.update(return_last_measurement_info=True)
            start_timestamp, stop_timestamp, framecount, tcnt = tmp
            self.queue_trigger_clock_info.put((start_timestamp, framecount, tcnt, stop_timestamp))
        except motmot.fview_ext_trig.live_timestamp_modeler.ImpreciseMeasurementError, err:
            pass

    # PERCAMNODE VERSION - Uses one ip:port combination per camnode (multiple cameras per camnode) to do the timestamp echo procedure.  port = 28992.
    # PERCAMERA VERSION - Uses one ip:port combination per camera to do the timestamp echo procedure.  port = 28995+iCamera
    def _check_latencies(self):
        if USE_ONE_TIMEPORT_PER_CAMERA:
            timestamp_echo_fmt1 = rospy.get_param('mainbrain/timestamp_echo_fmt1', '&lt;d')
            port_echo_timestamp_camera_base = rospy.get_param('mainbrain/port_timestamp_camera_base', 28995)
    
            for iCamera,guid in enumerate(self.Mainbrain_guids_copy):
                port_echo_timestamp_camera = port_echo_timestamp_camera_base + iCamera 
                if guid not in self._fqdns_by_guid:
                    sci, fqdn, coordinates_port = self.remote_api.external_get_info(guid)
                    self._fqdns_by_guid[guid] = fqdn
                else:
                    fqdn = self._fqdns_by_guid[guid]
                buf = struct.pack( timestamp_echo_fmt1, rospy.Time.now().to_sec() )
                nBytesTotal = len(buf)
                nBytesSent = 0
                while nBytesSent < nBytesTotal:
                    nBytes = self.socket_outgoing_latency_udp.sendto(buf[nBytesSent:],(fqdn,port_echo_timestamp_camera))
                    nBytesSent += nBytes
        else:
            timestamp_echo_fmt1 = rospy.get_param('mainbrain/timestamp_echo_fmt1', '&lt;d')
            port_echo_timestamp_camnode = rospy.get_param('mainbrain/port_timestamp_camnode', 28993)
            
            for guid in self.Mainbrain_guids_copy:
                if guid not in self._fqdns_by_guid:
                    sci, fqdn, coordinates_port = self.remote_api.external_get_info(guid)
                    self._fqdns_by_guid[guid] = fqdn
                else:
                    fqdn = self._fqdns_by_guid[guid]
                buf = struct.pack( timestamp_echo_fmt1, rospy.Time.now().to_sec() )
                nBytesTotal = len(buf)
                nBytesSent = 0
                while nBytesSent < nBytesTotal:
                    nBytes = self.socket_outgoing_latency_udp.sendto(buf[nBytesSent:],(fqdn,port_echo_timestamp_camnode))
                    nBytesSent += nBytes
                
        


    def get_last_image_fps(self, guid):
        # XXX should extend to include lines

        # Points are originally distorted (and align with distorted
        # image).
        (image, fps, points_distorted,
         image_coords) = self.remote_api.external_get_image_fps_points(guid)

        return image, fps, points_distorted, image_coords

    def close_camera(self,guid):
        sys.stdout.flush()
        self.remote_api.external_quit( guid )
        sys.stdout.flush()

    def set_collecting_background(self, guid, value):
        self.remote_api.external_send_set_camera_property( guid, 'collecting_background', value)

    def set_color_filter(self, guid, value):
        self.remote_api.external_send_set_camera_property( guid, 'color_filter', value)

    def take_background(self,guid):
        self.remote_api.external_take_background(guid)

    def clear_background(self,guid):
        self.remote_api.external_clear_background(guid)

    def send_set_camera_property(self, guid, property_name, value):
        self.remote_api.external_send_set_camera_property( guid, property_name, value)

    def request_image_async(self, guid):
        self.remote_api.external_request_image_async(guid)

    def get_debug_level(self):
        return self.debug_level.isSet()

    def set_debug_level(self,value):
        if value:
            self.debug_level.set()
        else:
            self.debug_level.clear()

    def get_show_overall_latency(self):
        return self.show_overall_latency.isSet()

    def set_show_overall_latency(self,value):
        if value:
            self.show_overall_latency.set()
        else:
            self.show_overall_latency.clear()

    def start_recording(self, guid, raw_file_basename):
        global g_XXX_framenumber

        self.remote_api.external_start_recording( guid, raw_file_basename)
        approx_start_frame = g_XXX_framenumber
        self._currently_recording_movies[ guid ] = (raw_file_basename, approx_start_frame)
        if self.is_saving_data():
            self.h5movie_info.row['guid'] = guid
            self.h5movie_info.row['filename'] = raw_file_basename+'.fmf'
            self.h5movie_info.row['approx_start_frame'] = approx_start_frame
            self.h5movie_info.row.append()
            self.h5movie_info.flush()

    def stop_recording(self, guid):
        global g_XXX_framenumber
        self.remote_api.external_stop_recording(guid)
        approx_stop_frame = g_XXX_framenumber
        raw_file_basename, approx_start_frame = self._currently_recording_movies[ guid ]
        del self._currently_recording_movies[ guid ]
        # modify save file to include approximate movie stop time
        if self.is_saving_data():
            nrow = None
            for r in self.h5movie_info:
                # get row in table
                if (r['guid'] == guid and r['filename'] == raw_file_basename+'.fmf' and
                    r['approx_start_frame']==approx_start_frame):
                    nrow =r.nrow
                    break
            if nrow is not None:
                nrowi = int(nrow) # pytables bug workaround...
                assert nrowi == nrow # pytables bug workaround...
                approx_stop_framei = int(approx_stop_frame)
                assert approx_stop_framei == approx_stop_frame

                new_columns = numpy.rec.fromarrays([[approx_stop_framei]], formats='i8')
                self.h5movie_info.modifyColumns(start=nrowi, columns=new_columns, names=['approx_stop_frame'])
            else:
                raise RuntimeError("could not find row to save movie stop frame.")

    def start_small_recording(self, guid, small_filename):
        self.remote_api.external_start_small_recording( guid,
                                                        small_filename)

    def stop_small_recording(self, guid):
        self.remote_api.external_stop_small_recording(guid)

    def quit(self):
        """closes any files being saved and closes camera connections"""
        # XXX ====== non-isolated calls to remote_api being done ======
        # this may be called twice: once explicitly and once by __del__
        with self.remote_api.lock_caminfo:
            guids = self.remote_api.caminfo_byguid.keys()

        for guid in guids:
            try:
                self.close_camera(guid)
            except Pyro.errors.ProtocolError:
                # disconnection results in error
                rospy.logwarn('Exception on camera %s' % guid)
                pass
        self.remote_api.event_no_cams.wait(2.0)
        self.remote_api.quit_now.set() # tell thread to finish
        self.remote_api.thread_done.wait(0.5) # wait for thread to finish
        if not self.remote_api.event_no_cams.isSet():
            guids = self.remote_api.caminfo_byguid.keys()
            rospy.logwarn('Cameras failed to quit cleanly: %s' % str(guids))
            #raise RuntimeError('cameras failed to quit cleanly: %s'%str(guids))

        self.stop_saving_data()
        self.coord_processor.quit()

    def load_calibration(self,dirname):
        if self.is_saving_data():
            raise RuntimeError("Cannot (re)load calibration while saving data")
        connected_guids = self.remote_api.external_get_guids()
        self.reconstructor = flydra.reconstruct.Reconstructor(dirname)
        calib_guids = self.reconstructor.get_guids()

        calib_guids = calib_guids

        self.coord_processor.set_reconstructor(self.reconstructor)

        for guid in calib_guids:
            pmat = self.reconstructor.get_pmat(guid)
            intlin = self.reconstructor.get_intrinsic_linear(guid)
            intnonlin = self.reconstructor.get_intrinsic_nonlinear(guid)
            scale_factor = self.reconstructor.get_scale_factor()
            if guid in connected_guids:
                self.remote_api.external_set_cal( guid, pmat, intlin, intnonlin, scale_factor )

    def clear_calibration(self):
        if self.is_saving_data():
            raise RuntimeError("Cannot unload calibration while saving data")
        guids = self.remote_api.external_get_guids()
        self.reconstructor = None

        self.coord_processor.set_reconstructor(self.reconstructor)

        for guid in guids:
            self.remote_api.external_set_cal( guid, None, None, None, None )

    def set_new_tracker(self,kalman_model_name=None):
        if self.is_saving_data():
            raise RuntimeError('will not set Kalman parameters while saving data')

        fps = self.get_fps()
        dt = 1.0/fps
        dynamic_model = flydra.kalman.dynamic_models.get_kalman_model(name=kalman_model_name,dt=dt)

        self.kalman_saver_info_instance = flydra_kalman_utils.KalmanSaveInfo(name=kalman_model_name)
        self.KalmanEstimatesDescription = self.kalman_saver_info_instance.get_description()
        self.dynamic_model=dynamic_model
        self.dynamic_model_name=kalman_model_name

        self.h5_xhat_names = tables.Description(self.KalmanEstimatesDescription().columns)._v_names

        # send params over to realtime coords thread
        self.coord_processor.set_new_tracker(kalman_model=dynamic_model)

    def __del__(self):
        self.quit()

    def is_saving_data(self):
        return self.h5file is not None

    def start_saving_data(self, filename):
        if os.path.exists(filename):
            raise RuntimeError("will not overwrite data file")

        self.timestamp_modeler.block_activity = True
        self.h5file = tables.openFile(filename, mode="w", title="Flydra data file")
        expected_rows = int(1e6)
        ct = self.h5file.createTable # shorthand
        root = self.h5file.root # shorthand
        self.h5data2d               = ct(root,'data2d_distorted', Info2D, "2d data", expectedrows=expected_rows*5)
        self.h5cam_info             = ct(root,'cam_info', CamSyncInfo, "Cam Sync Info", expectedrows=500)
        self.h5host_clock_info      = ct(root,'host_clock_info', HostClockInfo, "Host Clock Info",
                                         expectedrows=6*60*24) # 24 hours at 10 sec sample intervals
        self.h5trigger_clock_info   = ct(root,'trigger_clock_info', TriggerClockInfo, "Trigger Clock Info",
                                         expectedrows=6*60*24) # 24 hours at 10 sec sample intervals
        self.h5movie_info           = ct(root,'movie_info', MovieInfo, "Movie Info", expectedrows=500)
        self.h5textlog              = ct(root,'textlog', TextLogDescription, "text log")
        
        self._startup_message()
        if self.reconstructor is not None:
            self.reconstructor.save_to_h5file(self.h5file)
            if DO_KALMAN:
                self.h5data3d_kalman_estimates = ct(root,'kalman_estimates', self.KalmanEstimatesDescription,
                                                    "3d data (from Kalman filter)",
                                                    expectedrows=expected_rows)
                self.h5data3d_kalman_estimates.attrs.dynamic_model_name = self.dynamic_model_name
                self.h5data3d_kalman_estimates.attrs.dynamic_model = self.dynamic_model

                self.h5data3d_kalman_observations = ct(root,'kalman_observations', FilteredObservations,
                                                       "3d data (input to Kalman filter)",
                                                       expectedrows=expected_rows)
                self.h5_2d_obs = self.h5file.createVLArray(self.h5file.root,
                                                           'kalman_observations_2d_idxs',
                                                           kalman_observations_2d_idxs_type(), # dtype should match with tro.observations_2d
                                                           "camns and idxs")
                self.h5_2d_obs_next_idx = 0
            else:
                self.h5data3d_best = ct(root,'data3d_best', Info3D,
                                        "3d data (best)",
                                        expectedrows=expected_rows)

        general_save_info_byguid=self.coord_processor.get_general_cam_info()
        for guid,dd in general_save_info_byguid.iteritems():
            self.h5cam_info.row['guid'] = guid
            self.h5cam_info.row['camn']   = dd['camn']
            self.h5cam_info.row['frame0'] = dd['frame0']
            self.h5cam_info.row.append()
        self.h5cam_info.flush()

        # save raw image from each camera
        img = self.h5file.createGroup(root,'images','sample images')
        guids = self.remote_api.external_get_guids()
        for guid in guids:
            image, fps, points_distorted, image_coords = self.get_last_image_fps(guid)
            if image is None:
                raise ValueError('image cannot be None')
            self.h5file.createArray( img, guid, image, 'sample image from %s'%guid )

    def stop_saving_data(self):
        self._service_save_data()
        if self.is_saving_data():
            self.h5file.close()
            self.h5file = None
            self.timestamp_modeler.block_activity = False
        else:
            DEBUG('saving already stopped, cannot stop again')
        self.h5data2d = None
        self.h5cam_info = None
        self.h5host_clock_info = None
        self.h5trigger_clock_info = None
        self.h5movie_info = None
        self.h5textlog = None
        if DO_KALMAN:
            self.h5data3d_kalman_estimates = None
            self.h5data3d_kalman_observations = None
            self.h5_2d_obs = None
        else:
            self.h5data3d_best = None

    def _startup_message(self):
        textlog_row = self.h5textlog.row
        guid = 'mainbrain'
        timestamp = rospy.Time.now().to_sec()

        # This line is important (including the formatting). It is
        # read by flydra.a2.check_atmel_clock.

        list_of_textlog_data = [
            (timestamp,guid,timestamp,
             ('Mainbrain running at %s fps, (top %s, '
              'hypothesis_test_max_error %s, trigger_CS3 %s, FOSC %s, flydra_version %s)'%(
            str(self.trigger_device.frames_per_second_actual),
            str(self.trigger_device._t3_state.timer3_top),
            str(self.get_hypothesis_test_max_error()),
            str(self.trigger_device._t3_state.timer3_CS),
            str(self.trigger_device.FOSC),
            flydra.version.__version__,
            ))),
            (timestamp,guid,timestamp, 'using flydra version %s'%(
             flydra.version.__version__,)),
            ]
        for textlog_data in list_of_textlog_data:
            (mainbrain_timestamp,guid,host_timestamp,message) = textlog_data
            textlog_row['mainbrain_timestamp'] = mainbrain_timestamp
            textlog_row['guid'] = guid
            textlog_row['host_timestamp'] = host_timestamp
            textlog_row['message'] = message
            textlog_row.append()

        self.h5textlog.flush()

    def _request_missing_data(self):
        if ATTEMPT_DATA_RECOVERY:
            # request from camera computers any data that we're missing
            missing_data_dict = self.coord_processor.get_missing_data_dict()
            for guid, (offset_fn, fn_missing_list) in missing_data_dict.iteritems():
                #rospy.logwarn('Requesting from camn %d: %d frames %s' % (camn,len(fn_missing_list), numpy.array(fn_missing_list) ))
                camn = self.coord_processor.camn_from_guid[guid]
                self.remote_api.external_request_missing_data(guid, camn, offset_fn, fn_missing_list)

    def _service_save_data(self):
        # ** 2d data **
        #   clear queue
        datarowlist_to_save = []
        try:
            while True:
                datarowlist_tmp = self.queue_data2d.get(False)
                datarowlist_to_save.extend( datarowlist_tmp )
        except Queue.Empty:
            pass
        #   save
        if self.h5data2d is not None and len(datarowlist_to_save):
            # it's much faster to convert to numpy first:
            recarray = numpy.rec.array(
                datarowlist_to_save,
                dtype=Info2DCol_description)
            self.h5data2d.append( recarray )
            self.h5data2d.flush()

        # ** textlog **
        # clear queue
        list_of_textlog_data = []
        try:
            while True:
                tmp = self.remote_api.message_queue.get(False)
                list_of_textlog_data.append( tmp )
        except Queue.Empty:
            pass
        if 1:
            for textlog_data in list_of_textlog_data:
                (mainbrain_timestamp,guid,host_timestamp,message) = textlog_data
                rospy.logwarn('MESSAGE: %s %s "%s"' % (guid, time.asctime(time.localtime(host_timestamp)), message))
        #   save
        if self.h5textlog is not None and len(list_of_textlog_data):
            textlog_row = self.h5textlog.row
            for textlog_data in list_of_textlog_data:
                (mainbrain_timestamp,guid,host_timestamp,message) = textlog_data
                textlog_row['mainbrain_timestamp'] = mainbrain_timestamp
                textlog_row['guid'] = guid
                textlog_row['host_timestamp'] = host_timestamp
                textlog_row['message'] = message
                textlog_row.append()

            self.h5textlog.flush()

        # ** camera info **
        #   clear queue
        list_of_cam_info = []
        try:
            while True:
                list_of_cam_info.append( self.queue_cam_info.get(False) )
        except Queue.Empty:
            pass
        #   save
        if self.h5cam_info is not None:
            cam_info_row = self.h5cam_info.row
            for cam_info in list_of_cam_info:
                guid, camn, frame0 = cam_info
                cam_info_row['guid'] = guid
                cam_info_row['camn']   = camn
                cam_info_row['frame0'] = frame0
                cam_info_row.append()

            self.h5cam_info.flush()

        if DO_KALMAN:
            # ** 3d data - kalman **
            q = self.queue_data3d_kalman_estimates

            #   clear queue
            list_of_3d_data = []
            try:
                while True:
                    list_of_3d_data.append( q.get(False) )
            except Queue.Empty:
                pass
            if self.h5data3d_kalman_estimates is not None:
##                rospy.logwarn('Saving kalman data (%d objects)' % len(list_of_3d_data))
                for (obj_id, tro_frames, tro_xhats, tro_Ps, tro_timestamps,
                     obs_frames, obs_data,
                     observations_2d, obs_Lcoords) in list_of_3d_data:


                    if len(obs_frames)<MIN_KALMAN_OBSERVATIONS_TO_SAVE:
                        # only save data with at least 10 observations
                        if self.debug_level.isSet():
                            rospy.logwarn('Not saving kalman object -- too few observations to save')
                        continue

                    if self.debug_level.isSet():
                        rospy.logwarn('Saving kalman object %d' % obj_id)

                    # save observation 2d data indexes
                    this_idxs = []
                    for camns_and_idxs in observations_2d:
                        this_idxs.append( self.h5_2d_obs_next_idx )
                        self.h5_2d_obs.append( camns_and_idxs )
                        self.h5_2d_obs_next_idx += 1
                    self.h5_2d_obs.flush()

                    this_idxs = numpy.array( this_idxs, dtype=numpy.uint64 ) # becomes obs_2d_idx (index into 'kalman_observations_2d_idxs')

                    # save observations
                    observations_frames = numpy.array(obs_frames, dtype=numpy.uint64)
                    obj_id_array = numpy.empty(observations_frames.shape, dtype=numpy.uint32)
                    obj_id_array.fill(obj_id)
                    observations_data = numpy.array(obs_data, dtype=numpy.float32)
                    observations_Lcoords = numpy.array(obs_Lcoords, dtype=numpy.float32)
                    list_of_obs = [observations_data[:,i] for i in range(observations_data.shape[1])]
                    list_of_lines = [observations_Lcoords[:,i] for i in range(observations_Lcoords.shape[1])]
                    array_list = [obj_id_array,observations_frames]+list_of_obs+[this_idxs]+list_of_lines
                    obs_recarray = numpy.rec.fromarrays(array_list, names = h5_obs_names)

                    self.h5data3d_kalman_observations.append(obs_recarray)
                    self.h5data3d_kalman_observations.flush()

                    # save xhat info (kalman estimates)
                    frames = numpy.array(tro_frames, dtype=numpy.uint64)
                    timestamps = numpy.array(tro_timestamps, dtype=numpy.float64)
                    xhat_data = numpy.array(tro_xhats, dtype=numpy.float32)
                    P_data_full = numpy.array(tro_Ps, dtype=numpy.float32)
                    obj_id_array = numpy.empty(frames.shape, dtype=numpy.uint32)
                    obj_id_array.fill(obj_id)
                    list_of_xhats = [xhat_data[:,i] for i in range(xhat_data.shape[1])]
                    ksii = self.kalman_saver_info_instance
                    list_of_Ps = ksii.covar_mats_to_covar_entries(P_data_full)
                    xhats_recarray = numpy.rec.fromarrays(
                        [obj_id_array,frames,timestamps]+list_of_xhats+list_of_Ps,
                        names = self.h5_xhat_names)

                    self.h5data3d_kalman_estimates.append( xhats_recarray )
                    self.h5data3d_kalman_estimates.flush()

        else:
            # ** 3d data - hypothesis testing **
            q = self.queue_data3d_best
            h5table = self.h5data3d_best

            #   clear queue
            list_of_3d_data = []
            try:
                while True:
                    list_of_3d_data.append( q.get(False) )
            except Queue.Empty:
                pass
            #   save
            if h5table is not None:
                row = h5table.row
                for data3d in list_of_3d_data:
                    fn_corrected, outgoing_data, cam_nos_used, mean_dist = data3d
                    cam_nos_used.sort()
                    cam_nos_used_str = ' '.join( map(str, cam_nos_used) )

                    row['frame']=fn_corrected

                    row['x']=outgoing_data[0]
                    row['y']=outgoing_data[1]
                    row['z']=outgoing_data[2]

                    row['p0']=outgoing_data[3]
                    row['p1']=outgoing_data[4]
                    row['p2']=outgoing_data[5]
                    row['p3']=outgoing_data[6]
                    row['p4']=outgoing_data[7]
                    row['p5']=outgoing_data[8]

                    row['timestamp']=outgoing_data[9]

                    row['camns_used']=cam_nos_used_str
                    row['mean_dist']=mean_dist
                    row.append()

                h5table.flush()

        # ** camera info **
        #   clear queue
        list_of_host_clock_info = []
        try:
            while True:
                list_of_host_clock_info.append( self.queue_host_clock_info.get(False) )
        except Queue.Empty:
            pass
        #   save
        if self.h5host_clock_info is not None:
            host_clock_info_row = self.h5host_clock_info.row
            for host_clock_info in list_of_host_clock_info:
                remote_hostname, start_timestamp, remote_timestamp, stop_timestamp = host_clock_info
                host_clock_info_row['remote_hostname'] = remote_hostname
                host_clock_info_row['start_timestamp'] = start_timestamp
                host_clock_info_row['remote_timestamp'] = remote_timestamp
                host_clock_info_row['stop_timestamp'] = stop_timestamp
                host_clock_info_row.append()

            self.h5host_clock_info.flush()

        #   clear queue
        list_of_trigger_clock_info = []
        try:
            while True:
                list_of_trigger_clock_info.append( self.queue_trigger_clock_info.get(False) )
        except Queue.Empty:
            pass
        #   save
        if self.h5trigger_clock_info is not None:
            row = self.h5trigger_clock_info.row
            for trigger_clock_info in list_of_trigger_clock_info:
                start_timestamp, framecount, tcnt, stop_timestamp = trigger_clock_info
                row['start_timestamp'] = start_timestamp
                row['framecount'] = framecount
                row['tcnt'] = tcnt
                row['stop_timestamp'] = stop_timestamp
                row.append()

            self.h5trigger_clock_info.flush()


