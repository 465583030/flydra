#emacs, this is -*-Python-*- mode
# $Id$

import threading
import time
import socket
import sys
import Pyro.core, Pyro.errors
import FlyMovieFormat
import struct
import numarray as nx
import pyx_cam_iface as cam_iface
try:
    import realtime_image_analysis
except ImportError, x:
    if str(x).startswith('libippcore.so: cannot open shared object file'):
        print 'WARNING: IPP not loaded, proceeding without it'
        import realtime_image_analysis_noipp as realtime_image_analysis
    else:
        raise x

if sys.platform == 'win32':
    time_func = time.clock
else:
    time_func = time.time
    
Pyro.config.PYRO_MULTITHREADED = 0 # We do the multithreading around here!

Pyro.config.PYRO_TRACELEVEL = 3
Pyro.config.PYRO_USER_TRACELEVEL = 3
Pyro.config.PYRO_DETAILED_TRACEBACK = 1
Pyro.config.PYRO_PRINT_REMOTE_TRACEBACK = 1

CAM_CONTROLS = {'shutter':cam_iface.SHUTTER,
                'gain':cam_iface.GAIN,
                'brightness':cam_iface.BRIGHTNESS}

# where is the "main brain" server?
try:
    main_brain_hostname = socket.gethostbyname('mainbrain')
except:
    # try localhost
    main_brain_hostname = socket.gethostbyname(socket.gethostname())

class GrabClass(object):
    def __init__(self, cam, coord_port):
        self.cam = cam
        self.coord_port = coord_port

        # get coordinates for region of interest
        height = self.cam.get_max_height()
        width = self.cam.get_max_width()
        self.realtime_analyzer = realtime_image_analysis.RealtimeAnalyzer(width, height)

    def get_clear_threshold(self):
        return self.realtime_analyzer.clear_threshold
    def set_clear_threshold(self, value):
        self.realtime_analyzer.clear_threshold = value
    clear_threshold = property( get_clear_threshold, set_clear_threshold )
    
    def get_diff_threshold(self):
        return self.realtime_analyzer.diff_threshold
    def set_diff_threshold(self, value):
        self.realtime_analyzer.diff_threshold = value
    diff_threshold = property( get_diff_threshold, set_diff_threshold )

    def get_use_arena(self):
        return self.realtime_analyzer.use_arena
    def set_use_arena(self, value):
        self.realtime_analyzer.use_arena = value
    use_arena = property( get_use_arena, set_use_arena )

    def set_roi(self, *args):
        self.realtime_analyzer.set_roi(*args)
    
    def grab_func(self,globals):
        n_bg_samples = 100
        
        # questionable optimization: speed up by eliminating namespace lookups
        cam_quit_event_isSet = globals['cam_quit_event'].isSet
        acquire_lock = globals['incoming_frames_lock'].acquire
        release_lock = globals['incoming_frames_lock'].release
        sleep = time.sleep
        bg_frame_number = -1
        rot_frame_number = -1
        collect_background_start_isSet = globals['collect_background_start'].isSet
        collect_background_start_clear = globals['collect_background_start'].clear
        clear_background_start_isSet = globals['clear_background_start'].isSet
        clear_background_start_clear = globals['clear_background_start'].clear
        find_rotation_center_start_isSet = globals['find_rotation_center_start'].isSet
        find_rotation_center_start_clear = globals['find_rotation_center_start'].clear
        debug_isSet = globals['debug'].isSet
        height = self.cam.get_max_height()
        width = self.cam.get_max_width()
        buf_ptr_step = width

        buf = nx.zeros( (self.cam.max_height,self.cam.max_width), nx.UInt8 ) # allocate buffer
        coord_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            while not cam_quit_event_isSet():
                self.cam.grab_next_frame_blocking(buf) # grab frame and stick in buf
                sys.stdout.write('.')
                sys.stdout.flush()
                
                # get best guess as to when image was taken
                timestamp=self.cam.get_last_timestamp()
                framenumber=self.cam.get_last_framenumber()
                globals['last_frame_timestamp']=timestamp
                
                points = self.realtime_analyzer.do_work( buf )
                buf2 = self.realtime_analyzer.get_working_image()
                
                # make appropriate references to our copy of the data
                globals['most_recent_frame'] = buf2
                globals['most_recent_frame_and_points'] = buf2, points
                acquire_lock()
                globals['incoming_frames'].append(
                    (buf2,timestamp,framenumber) ) # save it
                release_lock()

                if clear_background_start_isSet():
                    clear_background_start_clear()
                    self.realtime_analyzer.clear_background_image()
                    
                if collect_background_start_isSet():
                    bg_frame_number=0
                    collect_background_start_clear()
                    self.realtime_analyzer.clear_accumulator_image()
                    
                if bg_frame_number>=0:
                    self.realtime_analyzer.accumulate_last_image()
                    bg_frame_number += 1
                    if bg_frame_number>=n_bg_samples:
                        bg_frame_number=-1 # stop averaging frames
                        self.realtime_analyzer.convert_accumulator_to_bg_image(n_bg_samples)
                                
                if find_rotation_center_start_isSet():
                    find_rotation_center_start_clear()
                    rot_frame_number=0
                    self.realtime_analyzer.rotation_calculation_init()
                    
                if rot_frame_number>=0:
                    self.realtime_analyzer.rotation_update()
                    rot_frame_number += 1
                    if rot_frame_number>=n_rot_samples:
                        self.realtime_analyzer.rotation_end()
                        rot_frame_number=-1 # stop averaging frames
              
                n_pts = len(points)
                data = struct.pack('<dli',timestamp,framenumber,n_pts)
                for i in range(n_pts):
                    data = data + struct.pack('<fff',*points[i])
                coord_socket.sendto(data,
                                    (main_brain_hostname,self.coord_port))
                sleep(1e-6) # yield processor
        finally:

            globals['cam_quit_event'].set()
            globals['grab_thread_done'].set()

class FromMainBrainAPI( Pyro.core.ObjBase ):
    # "camera server"
    
    # ----------------------------------------------------------------
    #
    # Methods called locally
    #
    # ----------------------------------------------------------------
    
    def post_init(self, cam_id, main_brain, main_brain_lock, globals):
        if type(cam_id) != type(''):
            raise TypeError('cam_id must be a string')
        self.cam_id = cam_id
        self.main_brain = main_brain
        self.main_brain_lock = main_brain_lock
        self.globals = globals
        self.quit_listening_now = False

    def listen(self,daemon):
        """thread mainloop"""
        self_cam_quit_event_isSet = self.globals['cam_quit_event'].isSet
        hr = daemon.handleRequests
        try:
            while not self_cam_quit_event_isSet():
                hr(5.0) # block on select for n seconds
                sys.stdout.write('^')
                sys.stdout.flush()
                if self.quit_listening_now:
                    break
                
        finally:
            self.globals['listen_thread_done'].set()

    # ----------------------------------------------------------------
    #
    # Methods called remotely from main_brain
    #
    # These all get called in their own thread.  Don't call across
    # the thread boundary without using locks.
    #
    # ----------------------------------------------------------------

    def send_most_recent_frame(self):
        """Trigger asynchronous send of image"""
        self.main_brain_lock.acquire()
        self.main_brain.set_image(self.cam_id, self.globals['most_recent_frame'])
        self.main_brain_lock.release()

    def get_most_recent_frame(self):
        """Return (synchronous) image"""
        return self.globals['most_recent_frame_and_points']

    def get_roi(self):
        """Return region of interest"""
        return self.globals['lbrt']

    def get_widthheight(self):
        """Return width and height of camera"""
        return self.globals['width'], self.globals['height']

    def is_ipp_enabled(self):
        result = False
        return result

    def start_debug(self):
        self.globals['debug'].set()
        print '-='*20,'ENTERING DEBUG MODE'

    def stop_debug(self):
        self.globals['debug'].clear()
        print '-='*20,'LEAVING DEBUG MODE'

    def start_recording(self,filename):
        self.quit_listening_now = True
        
        fly_movie_lock = threading.Lock()
        self.globals['record_status_lock'].acquire()
        try:
            fly_movie = FlyMovieFormat.FlyMovieSaver(filename,version=1)
            self.globals['record_status'] = ('save',fly_movie,fly_movie_lock)
            print "starting to record to %s"%filename
        finally:
            self.globals['record_status_lock'].release()        

    def stop_recording(self):
        cmd=None
        self.globals['record_status_lock'].acquire()
        try:
##            if self.globals['record_status']:
##                cmd,fly_movie,fly_movie_lock = self.globals['record_status']
            self.globals['record_status'] = None
        finally:
            self.globals['record_status_lock'].release()
            
        if cmd == 'save':
            fly_movie_lock.acquire()
            fly_movie.close()
            fly_movie_lock.release()
            print "stopping recording"
        else:
            # still saving data...
            #print "got stop recording command, but not recording!"
            pass

    def no_op(self):
        """used to test connection"""
        return None

    def quit(self):
        self.globals['cam_quit_event'].set()

    def collect_background(self):
        print 'collect 1'
        self.globals['collect_background_start'].set()

    def clear_background(self):
        print 'clear 1'
        self.globals['clear_background_start'].set()

    def get_diff_threshold(self):
        return self.globals['diff_threshold']

    def get_clear_threshold(self):
        return self.globals['clear_threshold']

    def find_r_center(self):
        self.globals['find_rotation_center_start'].set()
    
class App:
##    cdef object globals
##    cdef object cam_id
##    cdef object from_main_brain_api
    
##    cdef object main_brain
##    cdef object main_brain_lock
##    cdef int num_cams
    
##    # MAX_GRABBERS = 3
##    cdef Camera cam0
##    cdef Camera cam1
##    cdef Camera cam2
    
##    cdef GrabClass grabber0
##    cdef GrabClass grabber1
##    cdef GrabClass grabber2
    
    def __init__(self):
##        cdef Camera cam
##        cdef GrabClass grabber

        MAX_GRABBERS = 3
        # ----------------------------------------------------------------
        #
        # Setup cameras
        #
        # ----------------------------------------------------------------

        self.num_cams = cam_iface.get_num_cameras()
        print 'Number of cameras detected:', self.num_cams
        assert self.num_cams <= MAX_GRABBERS
        if self.num_cams == 0:
            return

        # ----------------------------------------------------------------
        #
        # Initialize network connections
        #
        # ----------------------------------------------------------------

        Pyro.core.initServer(banner=0,storageCheck=1)
        port = 9833
        name = 'main_brain'

        main_brain_URI = "PYROLOC://%s:%d/%s" % (main_brain_hostname,port,name)
        print 'connecting to',main_brain_URI
        self.main_brain = Pyro.core.getProxyForURI(main_brain_URI)
        self.main_brain._setOneway(['set_image','set_fps','close'])
        self.main_brain_lock = threading.Lock()

        # ----------------------------------------------------------------
        #
        # Initialize each camera
        #
        # ----------------------------------------------------------------

        self.globals = []
        self.cam_id = []
        self.from_main_brain_api = []
        
        for cam_no in range(self.num_cams):
            cam = cam_iface.CamContext(cam_no,30)

            height = cam.get_max_height()
            width = cam.get_max_width()

            if cam_no == 0:
                self.cam0=cam
            elif cam_no == 1:
                self.cam1=cam
            elif cam_no == 2:
                self.cam2=cam
            # add more if MAX_GRABBERS increases
                
            # ----------------------------------------------------------------
            #
            # Initialize "global" variables
            #
            # ----------------------------------------------------------------

            self.globals.append({})
            globals = self.globals[cam_no] # shorthand

            globals['incoming_frames']=[]
            globals['record_status']=None
            globals['most_recent_frame']=None
            globals['most_recent_frame_and_points']=None

            # control flow events for threading model
            globals['cam_quit_event'] = threading.Event()
            globals['listen_thread_done'] = threading.Event()
            globals['grab_thread_done'] = threading.Event()
            globals['incoming_frames_lock'] = threading.Lock()
            globals['collect_background_start'] = threading.Event()
            globals['clear_background_start'] = threading.Event()
            globals['find_rotation_center_start'] = threading.Event()
            globals['debug'] = threading.Event()
            globals['record_status_lock'] = threading.Lock()

            globals['lbrt'] = 0,0,width-1,height-1
            globals['width'] = width
            globals['height'] = height
            globals['last_frame_timestamp']=None

            # set defaults
            cam.set_camera_property(cam_iface.SHUTTER,300,0,0)
            cam.set_camera_property(cam_iface.GAIN,72,0,0)
            cam.set_camera_property(cam_iface.BRIGHTNESS,783,0,0)

            # get settings
            scalar_control_info = {}
            for name, enum_val in CAM_CONTROLS.items():
                current_value = cam.get_camera_property(enum_val)[0]
                tmp = cam.get_camera_property_range(enum_val)
                min_value = tmp[1]
                max_value = tmp[2]
                scalar_control_info[name] = (current_value, min_value, max_value)
            diff_threshold = 8.1
            scalar_control_info['initial_diff_threshold'] = diff_threshold
            clear_threshold = 0.0
            scalar_control_info['initial_clear_threshold'] = clear_threshold

            # register self with remote server
            port = 9834 + cam_no # for local Pyro server
            self.main_brain_lock.acquire()
            self.cam_id.append(
                self.main_brain.register_new_camera(cam_no,
                                                    scalar_control_info,
                                                    port))
            coord_port = self.main_brain.get_coord_port(self.cam_id[cam_no])
            self.main_brain_lock.release()
            
            # ---------------------------------------------------------------
            #
            # start local Pyro server
            #
            # ---------------------------------------------------------------

            hostname = socket.gethostname()
            if hostname == 'flygate':
                hostname = 'mainbrain' # serve on internal network
            print 'hostname',hostname
            host = socket.gethostbyname(hostname)
            daemon = Pyro.core.Daemon(host=host,port=port)
            self.from_main_brain_api.append( FromMainBrainAPI() )
            self.from_main_brain_api[cam_no].post_init(
                                                       self.cam_id[cam_no],
                                                       self.main_brain,
                                                       self.main_brain_lock,
                                                       globals)
            URI=daemon.connect(self.from_main_brain_api[cam_no],'camera_server')
            print 'listening locally at',URI

##            fly_movie = FlyMovieFormat.FlyMovieSaver('/tmp/cam.fmf',version=1)
##            fly_movie_lock = threading.Lock()
##            globals['record_status'] = ('save',fly_movie,fly_movie_lock)
            
            # create and start listen thread
            listen_thread=threading.Thread(target=self.from_main_brain_api[cam_no].listen,
                                           args=(daemon,))
            listen_thread.start()

            # ----------------------------------------------------------------
            #
            # start camera thread
            #
            # ----------------------------------------------------------------

            grabber = GrabClass(cam,coord_port)
            
            grabber.diff_threshold = diff_threshold
            # shadow grabber value
            globals['diff_threshold'] = grabber.diff_threshold
            
            grabber.clear_threshold = clear_threshold
            # shadow grabber value
            globals['clear_threshold'] = grabber.clear_threshold
            
            grabber.use_arena = False
            globals['use_arena'] = grabber.use_arena
            
            grab_thread=threading.Thread(target=grabber.grab_func,
                                         args=(globals,))
            cam.start_camera()  # start camera
            grab_thread.start() # start grabbing frames from camera

            print 'grab thread started'
            if cam_no == 0:
                self.grabber0=grabber
            elif cam_no == 1:
                self.grabber1=grabber
            elif cam_no == 2:
                self.grabber2=grabber
            print 'set grabber'
            # add more if MAX_GRABBERS increases

    def mainloop(self):
##        cdef Camera cam
##        cdef GrabClass grabber
        # per camera variables
        grabbed_frames = []

        last_measurement_time = []
        last_return_info_check = []
        n_frames = []
        
        if self.num_cams == 0:
            return

        for cam_no in range(self.num_cams):
            grabbed_frames.append( [] )

            last_measurement_time.append( time_func() )
            last_return_info_check.append( 0.0 ) # never
            n_frames.append( 0 )
            
        try:
            try:
                cams_in_operation = self.num_cams
                while cams_in_operation>0:
                    cams_in_operation = 0
                    for cam_no in range(self.num_cams):
                        globals = self.globals[cam_no] # shorthand

                        # check if camera running
                        if globals['cam_quit_event'].isSet():
                            continue

                        cams_in_operation = cams_in_operation + 1

                        if cam_no == 0:
                            cam=self.cam0
                        elif cam_no == 1:
                            cam=self.cam1
                        elif cam_no == 2:
                            cam=self.cam2

                        cam_id = self.cam_id[cam_no]
                        
                        now = time_func()
                        lft = globals['last_frame_timestamp']
                        if lft is not None:
                            if (now-lft) > 1.0:
                                print 'WARNING: last frame was %f seconds ago'%(now-lft,)
                                print '(Is the grab thread dead?)'
                                globals['last_frame_timestamp'] = None

                        # calculate and send FPS
                        elapsed = now-last_measurement_time[cam_no]
                        if elapsed > 5.0:
                            fps = n_frames[cam_no]/elapsed
                            self.main_brain_lock.acquire()
                            self.main_brain.set_fps(cam_id,fps)
                            self.main_brain_lock.release()
                            last_measurement_time[cam_no] = now
                            n_frames[cam_no] = 0

                        # get new frames from grab thread
                        lock = globals['incoming_frames_lock']
                        lock.acquire()
                        t1=time_func()
                        gif = globals['incoming_frames']
                        len_if = len(gif)
                        if len_if:
                            n_frames[cam_no] = n_frames[cam_no]+len_if
                            grabbed_frames[cam_no].extend( gif )
                            globals['incoming_frames'] = []
                        lock.release()
                        t2=time_func()
                        diff = t2-t1
                        if diff > 0.005:
                            print '                        Held lock for %f msec'%(diff*1000.0,)

                        # process asynchronous commands
                        self.main_brain_lock.acquire()
                        cmds=self.main_brain.get_and_clear_commands(cam_id)
                        self.main_brain_lock.release()
                        for key in cmds.keys():
                            if key == 'set':
                                for property_name,value in cmds['set'].iteritems():
                                    enum = CAM_CONTROLS[property_name]
                                    cam.set_camera_property(enum,value,0,0)
                            elif key == 'get_im': # low priority get image (for streaming)
                                self.from_main_brain_api[cam_no].send_most_recent_frame() # mimic call
                            else:
                                if cam_no == 0:
                                    grabber=self.grabber0
                                elif cam_no == 1:
                                    grabber=self.grabber1
                                elif cam_no == 2:
                                    grabber=self.grabber2
                                # add more if MAX_GRABBERS increases
                                if key == 'roi':
                                    l,b,r,t = cmds[key]
                                    grabber.set_roi( l,b,r,t )
                                    # shadow grabber value
                                    globals['lbrt']=l,b,r,t
                                elif key == 'diff_threshold':
                                    grabber.diff_threshold = cmds[key]
                                    # shadow grabber value
                                    globals['diff_threshold'] = grabber.diff_threshold
                                elif key == 'clear_threshold':
                                    grabber.clear_threshold = cmds[key]
                                    # shadow grabber value
                                    globals['clear_threshold'] = grabber.clear_threshold
                                elif key == 'use_arena':
                                    grabber.use_arena = cmds[key]
                                    globals['use_arena'] = grabber.use_arena
                                
                        # handle saving movie if needed
                        cmd=None
                        globals['record_status_lock'].acquire()
                        try:
                            if globals['record_status']:
                                cmd,fly_movie,fly_movie_lock = globals['record_status']
                        finally:
                            globals['record_status_lock'].release()

                        gfcn = grabbed_frames[cam_no]
                        len_gfcn = len(gfcn)
                        if len_gfcn:
                            if cmd=='save':
                                #print 'saving %d frames'%(len(grabbed_frames[cam_no]),)
                                sys.stdout.write('<%d'%len(gfcn))
                                sys.stdout.flush()
                                t1=time_func()
                                fly_movie_lock.acquire()
                                try:
                                    if 1:
                                        for frame,timestamp,framenumber in gfcn:
                                            fly_movie.add_frame(frame,timestamp)
                                        sz= frame.shape[1]*frame.shape[0]
                                    else:
                                        frames, timestamps, framenumbers = zip(*gfcn)
                                        fly_movie.add_frames(frames,timestamps)
                                        sz= frames[0].shape[1]*frames[0].shape[0]
                                finally:
                                    fly_movie_lock.release()
                                t2=time_func()
                                tdiff = t2-t1
                                mb_per_sec = len_gfcn*sz/(1024*1024)/tdiff
                                sys.stdout.write('> %d frames, %d MB/sec\n'%(len_gfcn,mb_per_sec))
                                sys.stdout.flush()

                            grabbed_frames[cam_no] = []

                    time.sleep(0.05)
                    sys.stdout.write('M')
                    sys.stdout.flush()

            finally:
##                self.globals[cam_no]['cam_quit_event'].set() # make sure other threads close
                self.main_brain_lock.acquire()
                for cam_id in self.cam_id:
                    self.main_brain.close(cam_id)
                self.main_brain_lock.release()
                for cam_no in range(self.num_cams):
                    self.globals[cam_no]['cam_quit_event'].set()                    
##                    self.globals[cam_no]['grab_thread_done'].wait() # block until thread is done...
##                    self.globals[cam_no]['listen_thread_done'].wait() # block until thread is done...
        except Pyro.errors.ConnectionClosedError:
            print 'unexpected connection closure...'

def main():
    app=App()
    app.mainloop()
    print

if __name__=='__main__':
    main()
        
