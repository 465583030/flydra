#!/usr/bin/env python
import sys
import threading
import time
import socket
import os
import copy
import Pyro.core
import DynamicImageCanvas

from UserDict import UserDict

Pyro.config.PYRO_MULTITHREADED = 0 # No multithreading!

Pyro.config.PYRO_TRACELEVEL = 3
Pyro.config.PYRO_USER_TRACELEVEL = 3
Pyro.config.PYRO_DETAILED_TRACEBACK = 1
Pyro.config.PYRO_PRINT_REMOTE_TRACEBACK = 1

from wxPython.wx import *
from wxPython.xrc import *

RESDIR = os.path.split(os.path.abspath(sys.argv[0]))[0]
RESFILE = os.path.join(RESDIR,'flydra_server.xrc')
hydra_image_file = os.path.join(RESDIR,'hydra.gif')
RES = wxXmlResource(RESFILE)

class OrderedDict(UserDict):
    # XXX Taken from:
    # Twisted, Copyright (C) 2001 Matthew W. Lefkowitz
    """A UserDict that preserves insert order whenever possible."""
    def __init__(self, d=None):
        # UserDict.__init__ calls self.update(d).
        self._order = []
        UserDict.__init__(self, d)

    def __repr__(self):
        return '{'+', '.join([('%r: %r' % item) for item in self.items()])+'}'

    def __setitem__(self, key, value):
        if not self.has_key(key):
            self._order.append(key)
        UserDict.__setitem__(self, key, value)

    def copy(self):
        return self.__class__(self)

    def __delitem__(self, key):
        UserDict.__delitem__(self, key)
        self._order.remove(key)

    def iteritems(self):
        for item in self._order:
            yield (item, self[item])

    def items(self):
        return list(self.iteritems())

    def itervalues(self):
        for item in self._order:
            yield self[item]

    def values(self):
        return list(self.itervalues())

    def iterkeys(self):
        return iter(self._order)

    def keys(self):
        return list(self._order)

    def popitem(self):
        key = self._order[-1]
        value = self[key]
        del self[key]
        return (key, value)

    def setdefault(self, item, default):
        if self.has_key(item):
            return self[item]
        self[item] = default
        return default

    def update(self, d):
        for k, v in d.items():
            self[k] = v

class MainBrain:
    """Handle all camera network stuff and interact with application"""

    class RemoteAPI(Pyro.core.ObjBase):

        # ----------------------------------------------------------------
        #
        # Methods called locally
        #
        # ----------------------------------------------------------------

        def post_init(self):
            """call after __init__"""
            # let Pyro handle __init__
            self.cam_info = {}
            #self.cam_info_lock = threading.Lock() # XXX probably not needed: listen thread is only writer
            self.changed_cam_lock = threading.Lock()
            self.no_cams_connected = threading.Event()
            self.no_cams_connected.set()
            self.changed_cam_lock.acquire()
            self.new_cam_ids = []
            self.old_cam_ids = []
            self.changed_cam_lock.release()
            
            # threading control locks
            self.quit_now = threading.Event()
            self.thread_done = threading.Event()

        def listen(self,daemon):
            """thread mainloop"""
            quit_now_isSet = self.quit_now.isSet
            hr = daemon.handleRequests
            while not quit_now_isSet():
                hr(0.1) # block on select for n seconds
                cam_ids = self.cam_info.keys()
                for cam_id in cam_ids:
                    if not self.cam_info[cam_id]['caller'].connected:
                        print 'WARNING: lost camera',cam_id
                        self.close(cam_id)
##                sys.stdout.write('.')
##                sys.stdout.flush()
            self.thread_done.set()
                                             
        # ----------------------------------------------------------------
        #
        # Methods called remotely from cameras
        #
        # These all get called in their own thread.  Don't call across
        # the thread boundary without using locks, especially to GUI
        # or OpenGL.
        #
        # ----------------------------------------------------------------

        def register_new_camera(self,scalar_control_info):
            """register new camera, return cam_id (caller: remote camera)"""
            
            caller= self.daemon.getLocalStorage().caller # XXX Pyro hack??
            caller_addr= caller.addr
            caller_ip, caller_port = caller_addr
            fqdn = socket.getfqdn(caller_ip)
        
            cam_id = '%s:%d'%(fqdn,caller_port)
            print 'listening to',cam_id

            self.cam_info[cam_id] = {'commands':{}, # command queue for cam
                                     'lock':threading.Lock(), # prevent concurrent access
                                     'image':None,  # most recent image from cam
                                     'fps':None,    # most recept fps from cam
                                     'caller':caller,    # most recept fps from cam
                                     'scalar_control_info':scalar_control_info,
                                     }
            self.no_cams_connected.clear()
            
            self.changed_cam_lock.acquire()
            self.new_cam_ids.append(cam_id)
            self.changed_cam_lock.release()
            
            return cam_id

        def get_commands(self,cam_id):
            """get command queue from server (caller: remote camera)"""
            cam = self.cam_info[cam_id]
            cam_lock = cam['lock']
            cam_lock.acquire()
            commands = cam['commands']
            cam['commands'] = {}
            cam_lock.release()
            return commands.items()

        def set_image(self,cam_id,image):
            """set most recent image (caller: remote camera)"""
            cam = self.cam_info[cam_id]
            cam_lock = cam['lock']
            cam_lock.acquire()
            self.cam_info[cam_id]['image'] = image
            cam_lock.release()
##            sys.stdout.write('Y')
##            sys.stdout.flush()

        def set_fps(self,cam_id,fps):
            """set most recent fps (caller: remote camera)"""
            cam = self.cam_info[cam_id]
            cam_lock = cam['lock']
            cam_lock.acquire()
            self.cam_info[cam_id]['fps'] = fps
            cam_lock.release()
##            sys.stdout.write('F')
##            sys.stdout.flush()

        def close(self,cam_id):
            """gracefully say goodbye (caller: remote camera)"""
            del self.cam_info[cam_id]
            if not len(self.cam_info):
                self.no_cams_connected.set()
            
            self.changed_cam_lock.acquire()
            self.old_cam_ids.append(cam_id)
            self.changed_cam_lock.release()
            
            print 'bye to',cam_id

    def __init__(self):
        Pyro.core.initServer(banner=0)
        try:
            hostname = socket.gethostbyname('flydra-server')
        except:
            hostname = socket.gethostbyname(socket.gethostname())
        fqdn = socket.getfqdn(hostname)
        port = 9833

        # start Pyro server
        daemon = Pyro.core.Daemon(host=hostname,port=port)
        remote_api = MainBrain.RemoteAPI(); remote_api.post_init()
        URI=daemon.connect(remote_api,'main_brain')

        # create (but don't start) listen thread
        self.listen_thread=threading.Thread(target=remote_api.listen,
                                            args=(daemon,))

        self.remote_api = remote_api

        self._new_camera_functions = []
        self._old_camera_functions = []

    def start_listening(self):
        # start listen thread
        self.listen_thread.start()

    def set_new_camera_callback(self,handler):
        self._new_camera_functions.append(handler)

    def set_old_camera_callback(self,handler):
        self._old_camera_functions.append(handler)

    def service_pending(self):
        self.remote_api.changed_cam_lock.acquire()
        # release lock as quickly as possible
        new_cam_ids = self.remote_api.new_cam_ids
        self.remote_api.new_cam_ids = []
        old_cam_ids = self.remote_api.old_cam_ids
        self.remote_api.old_cam_ids = []
        self.remote_api.changed_cam_lock.release()

        for cam_id in new_cam_ids:
            if cam_id in old_cam_ids:
                continue # inserted and removed
            for new_cam_func in self._new_camera_functions:
                # get scalar_control_info
                cam = self.remote_api.cam_info[cam_id]
                cam_lock = cam['lock']
                cam_lock.acquire()
                scalar_control_info = copy.deepcopy(cam['scalar_control_info'])
                cam_lock.release()
                new_cam_func(cam_id,scalar_control_info)
                
        for cam_id in old_cam_ids:
            for old_cam_func in self._old_camera_functions:
                old_cam_func(cam_id)

    def get_last_image_fps(self,cam_id):
        cam = self.remote_api.cam_info[cam_id]
        cam_lock = cam['lock']
        cam_lock.acquire()
        image = cam['image']
        cam['image'] = None
        fps = cam['fps']
        cam['fps'] = None
        cam_lock.release()
        return image, fps

    def send_commands(self,cam_id,commands):
        cam = self.remote_api.cam_info[cam_id]
        cam_lock = cam['lock']
        cam_lock.acquire()
        cam['commands'].update(commands)
        cam_lock.release()

    def close_camera(self,cam_id):
        self.send_commands(cam_id,{'quit':True})

    def quit(self):
        # this may be called twice: once explicitly and once by __del__
        print 'sending quit signal to cameras'
        cam_ids = self.remote_api.cam_info.keys()
        for cam_id in cam_ids:
            self.close_camera(cam_id)
        print 'waiting for cameras to quit'
        self.remote_api.no_cams_connected.wait(2.0)
        print 'sending quit signal to listen_thread...'
        self.remote_api.quit_now.set() # tell thread to finish
        print 'waiting for listen_thread to quit...'
        self.remote_api.thread_done.wait(0.5) # wait for thread to finish
        if not self.remote_api.no_cams_connected.isSet():
            cam_ids = self.remote_api.cam_info.keys()
            raise RuntimeError('cameras failed to quit cleanly: %s'%str(cam_ids))
    
    def __del__(self):
        self.quit()
        
class App(wxApp):
    def OnInit(self,*args,**kw):
    
        wxInitAllImageHandlers()
        frame = wxFrame(None, -1, "Flydra Main Brain",size=(800,600))
        
        self.statusbar = frame.CreateStatusBar()
        self.statusbar.SetFieldsCount(3)
        menuBar = wxMenuBar()
        filemenu = wxMenu()
        ID_quit = wxNewId()
        filemenu.Append(ID_quit, "Quit\tCtrl-Q", "Quit application")
        EVT_MENU(self, ID_quit, self.OnQuit)
        menuBar.Append(filemenu, "&File")
        frame.SetMenuBar(menuBar)

        self.main_panel = RES.LoadPanel(frame,"FLYDRA_PANEL") # make frame main panel
        self.main_panel.SetFocus()

        frame_box = wxBoxSizer(wxVERTICAL)
        frame_box.Add(self.main_panel,1,wxEXPAND)
        frame.SetSizer(frame_box)
        frame.Layout()

        nb = XRCCTRL(self.main_panel,"MAIN_NOTEBOOK")
        self.cam_preview_panel = RES.LoadPanel(nb,"CAM_PREVIEW_PANEL") # make camera preview panel
        nb.AddPage(self.cam_preview_panel,"Camera Preview/Settings")
        
        temp_panel = RES.LoadPanel(nb,"UNDER_CONSTRUCTION_PANEL") # make camera preview panel
        nb.AddPage(temp_panel,"3D Calibration")

        temp_panel = RES.LoadPanel(nb,"UNDER_CONSTRUCTION_PANEL") # make camera preview panel
        nb.AddPage(temp_panel,"Record raw video")

        temp_panel = RES.LoadPanel(nb,"UNDER_CONSTRUCTION_PANEL") # make camera preview panel
        nb.AddPage(temp_panel,"Realtime 3D tracking")

        #####################################

        self.all_cam_panel = XRCCTRL(self.cam_preview_panel,"AllCamPanel")

        acp_box = wxBoxSizer(wxHORIZONTAL) # all camera panel (for camera controls, e.g. gain)
        self.all_cam_panel.SetSizer(acp_box)
        
        #acp_box.Add(wxStaticText(self.all_cam_panel,-1,"This is the main panel"),0,wxEXPAND)
        self.all_cam_panel.Layout()

        #########################################

        frame.SetAutoLayout(true)

        frame.Show()
        self.SetTopWindow(frame)
        self.frame = frame

        dynamic_image_panel = XRCCTRL(self.main_panel,"DynamicImagePanel") # get container
        self.cam_image_canvas = DynamicImageCanvas.DynamicImageCanvas(dynamic_image_panel,-1) # put GL window in container
        #self.cam_image_canvas = wxButton(dynamic_image_panel,-1,"Button") # put GL window in container

        box = wxBoxSizer(wxVERTICAL)
        #box.Add(self.cam_image_canvas,1,wxEXPAND|wxSHAPED) # keep aspect ratio
        box.Add(self.cam_image_canvas,1,wxEXPAND)
        dynamic_image_panel.SetSizer(box)
        dynamic_image_panel.Layout()

        ID_Timer  = wxNewId()
        self.timer = wxTimer(self,      # object to send the event to
                             ID_Timer)  # event id to use
        EVT_TIMER(self,  ID_Timer, self.OnTimer)
        self.timer.Start(20)

        self.cameras = OrderedDict()
        self.wx_id_2_cam_id = {}

        self.update_wx()

        print 'initial setup'
        
        return True

    def attach_and_start_main_brain(self,main_brain):
        self.main_brain = main_brain
        self.main_brain.set_new_camera_callback(self.OnNewCamera)
        self.main_brain.set_old_camera_callback(self.OnOldCamera)
        self.main_brain.start_listening()

    def update_wx(self):
        self.statusbar.SetStatusText('%d camera servlet(s)'%len(self.cameras),2)
        
    def OnQuit(self, event):
        print 'wxApp quit'
        self.timer.Stop()
        del self.main_brain
        self.frame.Close(True)

    def OnTimer(self, event):
        self.main_brain.service_pending() # may call OnNewCamera, OnOldCamera, etc
        for cam_id in self.cameras.keys():
            cam = self.cameras[cam_id]
            camPanel = cam['camPanel']
            image = None
            acquired_fps = None
            try:
                image, acquired_fps = self.main_brain.get_last_image_fps(cam_id) # returns None if no new image
            except KeyError:
                # unexpected disconnect
                pass # may have lost camera since call to service_pending
            if image is not None:
                self.cam_image_canvas.update_image(cam_id,image)
            if acquired_fps is not None:
                acquired_fps_label = XRCCTRL(camPanel,'acquired_fps_label') # get container
                acquired_fps_label.SetLabel('Frames per second (acquired): %.1f'%acquired_fps)
        self.cam_image_canvas.OnDraw()

    def OnNewCamera(self, cam_id, scalar_control_info):
        print 'a new camera was registered:',cam_id

        # add self to WX
        camPanel = RES.LoadPanel(self.all_cam_panel,"PerCameraPanel")
        acp_box = self.all_cam_panel.GetSizer()
        acp_box.Add(camPanel,1,wxEXPAND | wxALL,border=10)

        if 0:
            box = camPanel.GetSizer()
            static_box = box.GetStaticBox()
            static_box.SetLabel( 'Camera ID: %s'%cam_id )

        XRCCTRL(camPanel,'cam_info_label').SetLabel('camera %s'%(cam_id))
        
        quit_camera = XRCCTRL(camPanel,"quit_camera") # get container
        EVT_BUTTON(quit_camera, quit_camera.GetId(), self.OnCloseCamera)
        self.wx_id_2_cam_id.update( {quit_camera.GetId():cam_id} )
        
        per_cam_controls_panel = XRCCTRL(camPanel,"PerCameraControlsContainer") # get container
        box = wxBoxSizer(wxVERTICAL)

        for param in scalar_control_info:
            current_value, min_value, max_value = scalar_control_info[param]
            scalarPanel = RES.LoadPanel(per_cam_controls_panel,"ScalarControlPanel") # frame main panel
            box.Add(scalarPanel,1,wxEXPAND)
            
            label = XRCCTRL(scalarPanel,'scalar_control_label')
            label.SetLabel( param )
            
            slider = XRCCTRL(scalarPanel,'scalar_control_slider')
            #slider.SetToolTip(wxToolTip('adjust %s'%param))
            slider.SetRange( min_value, max_value )
            slider.SetValue( current_value )
            
            class ParamSliderHelper:
                def __init__(self, name, cam_id, slider, main_brain):
                    self.name=name
                    self.cam_id=cam_id
                    self.slider=slider
                    self.main_brain=main_brain
                def onScroll(self, event):
                    self.main_brain.send_commands(self.cam_id,
                                                  {self.name:
                                                   self.slider.GetValue()})
            
            psh = ParamSliderHelper(param,cam_id,slider,self.main_brain)
            EVT_COMMAND_SCROLL(slider, slider.GetId(), psh.onScroll)
      
        per_cam_controls_panel.SetSizer(box)
        self.all_cam_panel.Layout()

        # bookkeeping
        self.cameras[cam_id] = {'scalar_control_info':scalar_control_info,
                                'camPanel':camPanel,
                                }
        # XXX should tell self.cam_image_canvas
        self.update_wx()

    def OnCloseCamera(self, event):
        cam_id = self.wx_id_2_cam_id[event.GetId()]
        self.main_brain.close_camera(cam_id) # eventually calls OnOldCamera
    
    def OnOldCamera(self, cam_id):
        print 'a camera was unregistered:',cam_id
        self.cam_image_canvas.delete_image(cam_id)
        
        camPanel=self.cameras[cam_id]['camPanel']
        camPanel.DestroyChildren()
        camPanel.Destroy()
        
        del self.cameras[cam_id]
        
        self.update_wx()
    
def main():
    # initialize GUI
    #app = App(redirect=1,filename='flydra_log.txt')
    app = App() 
    
    # create main_brain server (not started yet)
    main_brain = MainBrain()

    try:
        # connect server to GUI
        app.attach_and_start_main_brain(main_brain)
        print 'between init and ml'

        # hand control to GUI
        app.MainLoop()
        print 'MainLoop done'
        del app
        print 'app deleted'

    finally:
        # stop main_brain server
        main_brain.quit()
        print 'done'
    
if __name__ == '__main__':
    main()
