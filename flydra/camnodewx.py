#emacs, this is -*-Python-*- mode
from __future__ import division
from __future__ import with_statement

import wx
import wx.lib.newevent
import camnode
import camnode_utils
import numpy
from motmot.wxglvideo.simple_overlay import PointDisplayCanvas
from pygarrayimage.arrayimage import ArrayInterfaceImage

DisplayImageEvent, EVT_DISPLAYIMAGE = wx.lib.newevent.NewEvent()

class WxApp(wx.App):
    def OnInit(self):
        self.call_often = None
        wx.InitAllImageHandlers()
        self.frame = wx.Frame(None, -1, "camnode wx",size=(640,480))

        # menubar ------------------------------------
        menuBar = wx.MenuBar()
        #   File menu
        filemenu = wx.Menu()

        ID_quit = wx.NewId()
        filemenu.Append(ID_quit, "Quit\tCtrl-Q", "Quit application")
        wx.EVT_MENU(self, ID_quit, self.OnQuit)
        #wx.EVT_CLOSE(self, ID_quit, self.OnQuit)
        # JAB thinks this will allow use of the window-close ('x') button
        # instead of forcing users to file->quit

        menuBar.Append(filemenu, "&File")

        # finish menubar -----------------------------
        self.frame.SetMenuBar(menuBar)

        self.frame_box = wx.BoxSizer(wx.HORIZONTAL)
        self.cic_box = wx.BoxSizer(wx.VERTICAL)
        self.step_button_box = wx.BoxSizer(wx.HORIZONTAL)
        self.cic_box.Add(self.step_button_box,0,wx.EXPAND)

        self.cam_image_canvases = {}

        self.frame_box.Add(self.cic_box,1,wx.EXPAND)

        self.frame.SetSizer(self.frame_box)
        self.frame.Layout()

        self.frame.SetAutoLayout(True)

        self.frame.Show()
        self.SetTopWindow(self.frame)

        wx.EVT_CLOSE(self.frame, self.OnWindowClose)
        wx.EVT_KEY_DOWN(self, self.OnKeyDown)

        ID_Timer  = wx.NewId()
        self.timer = wx.Timer(self,      # object to send the event to
                              ID_Timer)  # event id to use
        #wx.EVT_TIMER(self,  ID_Timer, wrap_loud(self.frame,self.OnDoSingleFrame))
        wx.EVT_TIMER(self,  ID_Timer, self.OnDoSingleFrame)

        ID_Timer2 = wx.NewId()
        self.timer2 = wx.Timer(self, ID_Timer2)
        wx.EVT_TIMER(self, ID_Timer2, self.OnTimer2)
        self.update_interval2=50
        self.timer2.Start(self.update_interval2)

        EVT_DISPLAYIMAGE(self, self.OnDisplayImageEvent )
        self.controllers = []
        self._built_playback_GUI = False

        return True
    def post_init(self, call_often = None, full=False):
        self.call_often = call_often
        self._full_debug_images=full

    def OnWindowClose(self, event):
        event.Skip() # propagate event up the chain...
    def OnKeyDown(self, event):
        keycode = event.GetKeyCode()
        if keycode==wx.WXK_F11:
            if self.frame.IsFullScreen():
                self.frame.ShowFullScreen(False)
            else:
                self.frame.ShowFullScreen(True)
        else:
            event.Skip()
            return

    def OnQuit(self, dummy_event=None):
        self.frame.Close() # results in call to OnWindowClose()
        self.timer.Stop()
        self.timer2.Stop()

    def OnTimer2(self,event):
        if self.call_often is not None:
            self.call_often()

    def quit_now(self, exit_value):
        # called from callback thread
        if exit_value != 0:
            # don't know how to make wx exit with exit value otherwise
            sys.exit(exit_value)
        else:
            # send event to app
            event = wx.CloseEvent()
            event.SetEventObject(self)
            wx.PostEvent(self, event)

    def OnDisplayImageEvent(self, event):
        if event.cam_id not in self.cam_image_canvases:
            # first frame for this cam_id

            parent = self.frame

            cam_row_box = wx.StaticBoxSizer(wx.StaticBox(parent,-1,event.cam_id),wx.HORIZONTAL)

            # realtime image
            im_box = wx.BoxSizer(wx.VERTICAL)

            raw_canvas = PointDisplayCanvas(parent,-1)
            raw_canvas.set_fullcanvas(True)

            pygim = ArrayInterfaceImage(event.buf,allow_copy=False)
            raw_canvas.new_image(pygim)

            height,width = numpy.asarray(event.buf).shape

            im_box.Add(raw_canvas,proportion=1,
                       flag=wx.EXPAND|wx.ALL,border=2)
            im_box.Add(wx.StaticText(parent,-1,"raw image (%d x %d)"%(width,height)),
                       proportion=0,flag=wx.ALIGN_CENTRE|wx.ALL,
                       border=2)

            cam_row_box.Add(im_box,proportion=1,
                            flag=wx.EXPAND|wx.ALL,border=2)

            if self._full_debug_images:
                # absdiff image
                im_box = wx.BoxSizer(wx.VERTICAL)

                absdiff_canvas = PointDisplayCanvas(parent,-1)
                absdiff_canvas.set_fullcanvas(True)

                # event.absdiff_buf is (naughtily) not locked or copied between threads
                pygim = ArrayInterfaceImage(event.absdiff_buf,allow_copy=False)
                absdiff_canvas.new_image(pygim)

                im_box.Add(absdiff_canvas,proportion=1,
                           flag=wx.EXPAND|wx.ALL,border=2)
                im_box.Add(wx.StaticText(parent,-1,"modified absdiff image"),
                           proportion=0,flag=wx.ALIGN_CENTRE|wx.ALL,
                           border=2)
                cam_row_box.Add(im_box,proportion=1,
                                flag=wx.EXPAND|wx.ALL,border=2)

                # mean image
                im_box = wx.BoxSizer(wx.VERTICAL)

                mean_canvas = PointDisplayCanvas(parent,-1)
                mean_canvas.set_fullcanvas(True)

                # event.mean_buf is (naughtily) not locked or copied between threads
                pygim = ArrayInterfaceImage(event.mean_buf,allow_copy=False)
                mean_canvas.new_image(pygim)

                im_box.Add(mean_canvas,proportion=1,
                           flag=wx.EXPAND|wx.ALL,border=2)
                im_box.Add(wx.StaticText(parent,-1,"mean image"),
                           proportion=0,flag=wx.ALIGN_CENTRE|wx.ALL,
                           border=2)

                cam_row_box.Add(im_box,proportion=1,
                                flag=wx.EXPAND|wx.ALL,border=2)


                # cmp image
                im_box = wx.BoxSizer(wx.VERTICAL)

                cmp_canvas = PointDisplayCanvas(parent,-1)
                cmp_canvas.set_fullcanvas(True)

                # event.cmp_buf is (naughtily) not locked or copied between threads
                pygim = ArrayInterfaceImage(event.cmp_buf,allow_copy=False)
                cmp_canvas.new_image(pygim)

                im_box.Add(cmp_canvas,proportion=1,
                           flag=wx.EXPAND|wx.ALL,border=2)
                im_box.Add(wx.StaticText(parent,-1,"cmp image"),
                           proportion=0,flag=wx.ALIGN_CENTRE|wx.ALL,
                           border=2)

                cam_row_box.Add(im_box,proportion=1,
                                flag=wx.EXPAND|wx.ALL,border=2)


            self.cic_box.Add(cam_row_box,proportion=1,flag=wx.EXPAND)

            parent.Layout()
            if self._full_debug_images:
                self.cam_image_canvases[event.cam_id] = (raw_canvas, absdiff_canvas, mean_canvas, cmp_canvas)
            else:
                self.cam_image_canvases[event.cam_id] = (raw_canvas,)
        else:
            # this is not the first frame for this cam_id - just display it
            if self._full_debug_images:
                (raw_canvas, absdiff_canvas, mean_canvas, cmp_canvas) = self.cam_image_canvases[event.cam_id]
            else:
                (raw_canvas,) = self.cam_image_canvases[event.cam_id]

            points = event.pts
            point_colors, linesegs,lineseg_colors = None,None,None
            raw_canvas.extra_points_linesegs = (
                points,point_colors, linesegs,lineseg_colors)
            raw_canvas.update_image( event.buf )
            if self._full_debug_images:
                absdiff_canvas.update_image( event.absdiff_buf )
                mean_canvas.update_image( event.mean_buf )
                cmp_canvas.update_image( event.cmp_buf )

    def generate_view(self, model, controller ):
        self.controllers.append( controller )
        if hasattr(controller, 'trigger_single_frame_start' ):
            if not self._built_playback_GUI:

                ctrl = wx.Button(self.frame,-1,"step")
                wx.EVT_BUTTON(ctrl, ctrl.GetId(), self.OnDoSingleFrame)
                self.step_button_box.Add(ctrl,proportion=1,flag=wx.EXPAND)

                ctrl = wx.Button(self.frame,-1,"play 100 fps")
                wx.EVT_BUTTON(ctrl, ctrl.GetId(), self.OnPlay100Fps)
                self.step_button_box.Add(ctrl,proportion=1,flag=wx.EXPAND)

                ctrl = wx.Button(self.frame,-1,"stop")
                wx.EVT_BUTTON(ctrl, ctrl.GetId(), self.OnStopPlaying)
                self.step_button_box.Add(ctrl,proportion=1,flag=wx.EXPAND)

                ctrl = wx.Button(self.frame,-1,"frame 0")
                wx.EVT_BUTTON(ctrl, ctrl.GetId(), self.OnFrame0)
                self.step_button_box.Add(ctrl,proportion=1,flag=wx.EXPAND)

                self.frame.Layout()
                self._built_playback_GUI = True

    def OnPlay100Fps(self, event):
        self.timer.Start(10) # call every n msec

    def OnStopPlaying(self, event):
        self.timer.Stop() # call every n msec

    def OnDoSingleFrame(self, event):
        for controller in self.controllers:
            controller.trigger_single_frame_start()

    def OnFrame0(self, event):
        for controller in self.controllers:
            controller.set_to_frame_0()

class DisplayCamData(object):
    def __init__(self, wxapp,
                 cam_id=None,
                 full=False,
                 ):
        self._chain = camnode_utils.ChainLink()
        self._wxapp = wxapp
        self._cam_id = cam_id
        self._full_debug_images=full
    def get_chain(self):
        return self._chain
    def mainloop(self):
        NAUGHTY_BUT_FAST = False
        while 1:
            with camnode_utils.use_buffer_from_chain(self._chain) as chainbuf:
                if chainbuf.quit_now:
                    # XXX TODO: Send done event to GUI.
                    print 'TODO: send quit event to GUI for cam_id %s'%(self._cam_id)
                    break
                # post images and processed points to wx
                if hasattr(chainbuf,'processed_points'):
                    pts = chainbuf.processed_points
                else:
                    pts = None
                if NAUGHTY_BUT_FAST:
                    buf_copy = chainbuf.get_buf() # not a copy at all!
                    if self._full_debug_images:
                        absdiff = chainbuf.absdiff8u_im_full
                        mean = chainbuf.mean8u_im_full
                        cmp = chainbuf.compareframe8u_full
                else:
                    buf_copy = numpy.array( chainbuf.get_buf(), copy=True )
                    if self._full_debug_images:
                        absdiff = numpy.array( chainbuf.absdiff8u_im_full, copy=True )
                        mean = numpy.array( chainbuf.mean8u_im_full, copy=True )
                        cmp = numpy.array( chainbuf.compareframe8u_full, copy=True )
                image_coding = chainbuf.image_coding

            kwargs = {}
            if self._full_debug_images:
                kwargs.update( dict(
                    absdiff_buf=absdiff,
                    mean_buf=mean,
                    cmp_buf=cmp,
                    ))

            wx.PostEvent(self._wxapp, DisplayImageEvent(buf=buf_copy,
                                                        pts=pts,
                                                        cam_id=self._cam_id,
                                                        image_coding = image_coding,
                                                        **kwargs
                                                        ))
