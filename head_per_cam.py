#!/usr/bin/env python

DUMMY=0

import threading
import time
import socket
import sys
import numarray as na
import Pyro.core, Pyro.errors

Pyro.config.PYRO_MULTITHREADED = 0 # No multithreading!
Pyro.config.PYRO_PRINT_REMOTE_TRACEBACK = 1

if not DUMMY:
    import cam_iface
else:
    import cam_iface_dummy
    cam_iface = cam_iface_dummy

CAM_CONTROLS = {'shutter':cam_iface.SHUTTER,
            'gain':cam_iface.GAIN,
            'brightness':cam_iface.BRIGHTNESS}

incoming_frames = []

def grab_func(cam,quit_now,thread_done,incoming_frames_lock):
    # transfer data from camera
    # (this could be in C)
    global incoming_frames
    buf = na.zeros( (cam.max_height,cam.max_width), na.UInt8 ) # allocate buffer
    try:
        while not quit_now.isSet():
            cam.grab_next_frame_blocking(buf) # grab frame and stick in buf
            incoming_frames_lock.acquire()
            incoming_frames.append( buf.copy() ) # save a copy of the buffer
            incoming_frames_lock.release()
            time.sleep(0.00001) # yield processor
    finally:
        thread_done.set()

class FromMainBrainAPI( Pyro.core.ObjBase ):

    # ----------------------------------------------------------------
    #
    # Methods called locally
    #
    # ----------------------------------------------------------------
    
    def post_init(self):
        # threading control locks
        self.quit_now = threading.Event()
        self.thread_done = threading.Event()

    def listen(self,daemon):
        """thread mainloop"""
        quit_now_isSet = self.quit_now.isSet
        hr = daemon.handleRequests
        while not quit_now_isSet():
            hr(0.1) # block on select for n seconds
        self.thread_done.set()

    def quit_listening(self):
        self.quit_now.set()
        self.thread_done.wait()
        
    # ----------------------------------------------------------------
    #
    # Methods called remotely from cameras
    #
    # These all get called in their own thread.  Don't call across
    # the thread boundary without using locks.
    #
    # ----------------------------------------------------------------

    def ouch(self):
        print 'ouch'
           
def main():
    global incoming_frames

    start = time.time()
    now = start
    num_buffers = 30
    
    grabbed_frames = []

    # open network stuff ###########################
    # myself as a server
    Pyro.core.initServer(banner=0)
    hostname = socket.gethostbyname(socket.gethostname())
    fqdn = socket.getfqdn(hostname)
    port = 9834
    
    # start Pyro server
    daemon = Pyro.core.Daemon(host=hostname,port=port)
    from_main_brain_api = FromMainBrainAPI(); from_main_brain_api.post_init()
    URI=daemon.connect(from_main_brain_api,'camera_server')
    print 'serving',URI,'at',time.time(),'(camera_server)'
    
    # create and start listen thread
    listen_thread=threading.Thread(target=from_main_brain_api.listen,
                                   args=(daemon,))
    listen_thread.start()
        
    # myself as a client
    #Pyro.core.initClient(banner=0)
    # where is the "main brain" server?
    try:
        main_brain_hostname = socket.gethostbyname('flydra-server')
    except:
        # try localhost
        main_brain_hostname = socket.gethostbyname(socket.gethostname())
    port = 9833
    name = 'main_brain'
    
    main_brain_URI = "PYROLOC://%s:%d/%s" % (main_brain_hostname,port,name)
    print 'searching for',main_brain_URI
    main_brain = Pyro.core.getProxyForURI(main_brain_URI)
    print 'found'

    # ----------------------------------------------------------------
    #
    # Setup cameras
    #
    # ----------------------------------------------------------------

    for device_number in range(cam_iface.cam_iface_get_num_cameras()):
        try:
            cam = cam_iface.CamContext(device_number,num_buffers)
            break # found a camera
        except Exception, x:
            if not x.args[0].startswith('The requested resource is in use.'):
                raise

    cam.set_camera_property(cam_iface.SHUTTER,300,0,0)
    cam.set_camera_property(cam_iface.GAIN,72,0,0)
    cam.set_camera_property(cam_iface.BRIGHTNESS,783,0,0)
    
    cam.start_camera()

    # ----------------------------------------------------------------
    #
    # inform brain that we're connected before starting camera thread
    #
    # ----------------------------------------------------------------

    scalar_control_info = {}
    for name, enum_val in CAM_CONTROLS.items():
        current_value = cam.get_camera_property(enum_val)[0]
        tmp = cam.get_camera_property_range(enum_val)
        min_value = tmp[1]
        max_value = tmp[2]
        scalar_control_info[name] = (current_value, min_value, max_value)

    driver = cam_iface.cam_iface_get_driver_name()

    cam_id = main_brain.register_new_camera(scalar_control_info)
    main_brain._setOneway(['set_image','set_fps','close'])

    # ----------------------------------------------------------------
    #
    # start camera thread
    #
    # ----------------------------------------------------------------
    
    thread_done = threading.Event()
    quit_now = threading.Event()
    incoming_frames_lock = threading.Lock()
    grab_thread=threading.Thread(target=grab_func,
                                 args=(cam,
                                       quit_now,
                                       thread_done,
                                       incoming_frames_lock))
    grab_thread.start()

    last_measurement_time = time.time()
    last_return_info_check = 0.0 # never
    n_frames = 0
    quit = False
    try:
        try:
            while not quit:
                now = time.time()

                # calculate and send FPS
                elapsed = now-last_measurement_time
                if elapsed > 5.0:
                    fps = n_frames/elapsed
                    main_brain.set_fps(cam_id,fps)
                    last_measurement_time = now
                    n_frames = 0

                # get new frames from grab thread
                if len(incoming_frames):
                    n_frames += len(incoming_frames)
                    incoming_frames_lock.acquire()
                    grabbed_frames.extend( incoming_frames )
                    incoming_frames = []
                    incoming_frames_lock.release()

                # send most recent image
                if len(grabbed_frames):
                    main_brain.set_image(cam_id,grabbed_frames[-1])
                    grabbed_frames = []

                # poll for commands
                if now - last_return_info_check > 1.0:
                    updates = main_brain.get_commands(cam_id)
                    last_return_info_check = now

                    for key,value in updates:
                        if key in CAM_CONTROLS:
                            enum = CAM_CONTROLS[key]
                            cam.set_camera_property(enum,value,0,0)
                        # more commands here
                        elif key == 'quit':
                            quit = value
                        else:
                            raise RuntimeError ('Unknown command: %s'%repr(ud))
                time.sleep(0.01)
                if thread_done.isSet():
                    quit = True

        finally:
            print 'telling grab thread to quit'
            quit_now.set()
            print 'waiting for grab thread to quit'
            thread_done.wait() # block until thread is done...
            print 'telling main_brain to close cam_id'
            main_brain.close(cam_id)
            print 'closed connection to main_brain server'
            print 'closing camera_server'
            from_main_brain_api.quit_listening()
            print 'quitting'
    except Pyro.errors.ConnectionClosedError:
        print 'unexpected connection closure...'
    
if __name__=='__main__':
    main()
