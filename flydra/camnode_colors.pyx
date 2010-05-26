cimport c_python
cimport motmot.FastImage.FastImage as FastImage
import motmot.FastImage.FastImage as FastImage
cimport ipp

cdef extern from "colors.h":
    int mono8_bggr_to_red_color(ipp.Ipp8u* im, int step, ipp.IppiSize, int color_range_1, int color_range_2, int color_range_3, int sat_thresh) nogil

cdef int iRED_CHANNEL
cdef int iRED_COLOR

RED_CHANNEL = 0
RED_COLOR = 1

iRED_CHANNEL = 0
iRED_COLOR = 1

def replace_with_red_image( object arr, object coding, int chan, int color_range_1, int color_range_2, int color_range_3, int sat_thresh):
    assert coding=='MONO8:BGGR'
    cdef FastImage.FastImage8u fiarr
    cdef int err

    fiarr = FastImage.asfastimage(arr)

    err = 2
    with nogil:
        # if chan==iRED_CHANNEL:
        #     err = mono8_bggr_to_red_channel( <unsigned char*>inter.data, inter.strides[0], inter.shape[0], inter.shape[1] )
        if chan==iRED_COLOR:
            err = mono8_bggr_to_red_color( <ipp.Ipp8u*>fiarr.im, fiarr.step, fiarr.imsiz.sz, color_range_1, color_range_2, color_range_3, sat_thresh)
    if err:
        raise RuntimeError("to_red() returned error %d"%err)
