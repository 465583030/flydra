import numarray as na
import math
#import thread

from wxPython.wx import *
from wxPython.glcanvas import *
from OpenGL.GL import *
#from VisionEgg.GLTrace import *

class DynamicImageCanvas(wxGLCanvas):
    def __init__(self, *args, **kw):
        wxGLCanvas.__init__(*(self,)+args, **kw)
        self.init = False
        EVT_ERASE_BACKGROUND(self, self.OnEraseBackground)
        EVT_SIZE(self, self.OnSize)
        EVT_PAINT(self, self.OnPaint)
        EVT_IDLE(self, self.OnDraw)
        self._gl_tex_info_dict = {}
#        self._gl_lock = thread.allocate_lock()

    def delete_image(self,id_val):
        tex_id, gl_tex_xy_alloc, gl_tex_xyfrac = self._gl_tex_info_dict[id_val]
        glDeleteTextures( tex_id )
        del self._gl_tex_info_dict[id_val]        

    def __del__(self, *args, **kwargs):
        for id_val in self._gl_tex_info_dict.keys():
            self.delete_image(id_val)
            
    def OnEraseBackground(self, event):
        pass # Do nothing, to avoid flashing on MSW.

    def OnSize(self, event):
        size = self.GetClientSize()
        if self.GetContext():
            self.SetCurrent()
            glViewport(0, 0, size.width, size.height)

    def OnPaint(self, event):
        dc = wxPaintDC(self)
        self.SetCurrent()
        if not self.init:
            self.InitGL()
            self.init = True
        self.OnDraw()
        
    def InitGL(self):
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0,1,0,1,-1,1)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        glEnable( GL_BLEND )
        glClearColor(0.0, 0.0, 1.0, 0.0) # blue
        glBlendFunc( GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA )
        glDisable(GL_DEPTH_TEST);
        glColor4f(1.0,1.0,1.0,1.0)
        
    def create_texture_object(self,id_val,image):
        
        def next_power_of_2(f):
            return int(math.pow(2.0,math.ceil(math.log(f)/math.log(2.0))))

        height, width = image.shape
        
        width_pow2  = next_power_of_2(width)
        height_pow2  = next_power_of_2(height)
        
        buffer = na.zeros( (height_pow2,width_pow2,2), image.typecode() )+128
        buffer[0:height,0:width,0] = image
        
        clipped = na.greater(image,254) + na.less(image,1)
        mask = na.choose(clipped, (255, 0) )
        buffer[0:height,0:width,1] = mask
        
        raw_data = buffer.tostring()

        tex_id = glGenTextures(1)

        gl_tex_xy_alloc = width_pow2, height_pow2
        gl_tex_xyfrac = width/float(width_pow2),  height/float(height_pow2)

        self._gl_tex_info_dict[id_val] = tex_id, gl_tex_xy_alloc, gl_tex_xyfrac            
        
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glEnable(GL_TEXTURE_2D)
        glTexEnvi(GL_TEXTURE_ENV, GL_TEXTURE_ENV_MODE, GL_MODULATE)
        glPixelStorei(GL_UNPACK_ALIGNMENT,1)
##        glTexParameterf(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
##        glTexParameterf(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameterf(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        glTexParameterf(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexImage2D(GL_TEXTURE_2D, # target
                     0, #mipmap_level
                     GL_LUMINANCE_ALPHA, #internal_format,
                     width_pow2,
                     height_pow2,
                     0, #border,
                     GL_LUMINANCE_ALPHA, #data_format,
                     GL_UNSIGNED_BYTE, #data_type,
                     raw_data);

    def update_image(self,id_val,image):
#        self._gl_lock.acquire() # serialize OpenGL access

        if id_val not in self._gl_tex_info_dict:
            self.create_texture_object(id_val,image)
            return
        height, width = image.shape
        tex_id, gl_tex_xy_alloc, gl_tex_xyfrac = self._gl_tex_info_dict[id_val]
        
        max_x, max_y = gl_tex_xy_alloc 
        if width > max_x or height > max_y: 
            self.delete_image(id_val) 
            self.create_texture_object(id_val,image)
        else:
            buffer = na.zeros( (height,width,2), image.typecode() )+128
            buffer[:,:,0] = image
            clipped = na.greater(image,254) + na.less(image,1)
            mask = na.choose(clipped, (255, 200) ) # alpha for transparency
            buffer[:,:,1] = mask
            self._gl_tex_xyfrac = width/float(max_x),  height/float(max_y)
            glBindTexture(GL_TEXTURE_2D,tex_id)
            glTexSubImage2D(GL_TEXTURE_2D, #target,
                            0, #mipmap_level,
                            0, #x_offset,
                            0, #y_offset,
                            width,
                            height,
                            GL_LUMINANCE_ALPHA, #data_format,
                            GL_UNSIGNED_BYTE, #data_type,
                            buffer.tostring())
#        self._gl_lock.release() # serialize OpenGL access

    def OnDraw(self,*dummy_arg):
        # clear color and depth buffers
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT )

        N = float(len(self._gl_tex_info_dict))
        ids = self._gl_tex_info_dict.keys()
        ids.sort()
        x_border_pixels = 1
        y_border_pixels = 1
        size = self.GetClientSize()

        x_border = x_border_pixels/float(size[0])
        y_border = y_border_pixels/float(size[1])
        hx = x_border*0.5
        hy = y_border*0.5
        x_borders = x_border*(N+1)
        y_borders = y_border*(N+1)
        for i in range(N):
            bottom = y_border
            top = 1.0-y_border
            left = (1.0-2*hx)*i/N+hx+hx
            right = (1.0-2*hx)*(i+1)/N-hx+hx
            
            tex_id, gl_tex_xy_alloc, gl_tex_xyfrac = self._gl_tex_info_dict[ids[i]]

            xx,yy = gl_tex_xyfrac

            glBindTexture(GL_TEXTURE_2D,tex_id)
            glBegin(GL_QUADS)
            glTexCoord2f( 0, yy) # texture is flipped upside down to fix OpenGL<->na
            glVertex2f( left, bottom)
        
            glTexCoord2f( xx, yy)
            glVertex2f( right, bottom)
        
            glTexCoord2f( xx, 0)
            glVertex2f( right, top)
        
            glTexCoord2f( 0, 0)
            glVertex2f( left,top)
            glEnd()
        
        self.SwapBuffers()
