from __future__ import with_statement
import motmot.ufmf.ufmf as ufmf_mod
import sys, os, tempfile, re, contextlib, warnings
from optparse import OptionParser
import flydra.a2.auto_discover_ufmfs as auto_discover_ufmfs
import numpy as np
import tables
import flydra.a2.utils as utils
import flydra.analysis.result_utils as result_utils
import scipy.misc
import subprocess
import flydra.a2.ufmf_tools as ufmf_tools
import scipy.ndimage
import flydra.data_descriptions
from tables_tools import clear_col, openFileSafe
import motmot.FastImage.FastImage as FastImage
import motmot.realtime_image_analysis.realtime_image_analysis \
       as realtime_image_analysis

def get_tile(N):
    rows = int(np.ceil(np.sqrt(float(N))))
    cols = rows
    return '%dx%d'%(rows,cols)

def retrack_movies( h5_filename,
                    output_h5_filename=None,
                    max_n_frames = None,
                    start = None,
                    stop = None,
                    ufmf_dir = None,
                    ):

    save_debug_images = False
    area_minimum_threshold = 10
    min_absdiff = 5
    absdiff_max_frac_thresh = 0.5

    # 2D data format for PyTables:
    Info2D = flydra.data_descriptions.Info2D
    # allow rapid building of numpy.rec.array:
    Info2DCol_description = tables.Description(Info2D().columns)._v_nestedDescr

    ufmf_fnames = auto_discover_ufmfs.find_ufmfs( h5_filename,
                                                  ufmf_dir=ufmf_dir,
                                                  careful=True )

    if os.path.exists( output_h5_filename ):
        raise RuntimeError(
            "will not overwrite old file '%s'"%output_h5_filename)

    # get name of data

    datetime_str = os.path.splitext(os.path.split(h5_filename)[-1])[0]
    datetime_str = datetime_str[4:19]

    retrack_cam_ids = [ufmf_tools.get_cam_id_from_ufmf_fname(f)
                       for f in ufmf_fnames]

    with openFileSafe( h5_filename, mode='r' ) as h5:

        # Find camns in original data
        camn2cam_id, cam_id2camns = result_utils.get_caminfo_dicts(h5)
        retrack_camns = []
        for cam_id in retrack_cam_ids:
            retrack_camns.extend( cam_id2camns[cam_id] )
        all_camns = camn2cam_id.keys()

        with openFileSafe( output_h5_filename, mode='w') as output_h5:

            out_data2d = output_h5.createTable(
                output_h5.root,
                'data2d_distorted',
                Info2D, "2d data",
                expectedrows=h5.root.data2d_distorted.nrows)

            # Are there any camns in original h5 that are not being retracked?
            if len(set(all_camns)-set(retrack_camns)):
                # Yes.

                # OK, exclude all camns to be retracked...
                orig_data2d = h5.root.data2d_distorted[:] # read all data
                for camn in retrack_camns:
                    delete_cond = orig_data2d['camn']==camn
                    save_cond = ~delete_cond
                    orig_data2d = orig_data2d[save_cond]

                # And save original data for untouched camns
                out_data2d.append( orig_data2d )

            for input_node in h5.root._f_iterNodes():
                if input_node._v_name not in ['data2d_distorted',
                                              'kalman_estimates',
                                              'kalman_observations',
                                              'kalman_observations_2d_idxs',
                                              ]:
                    print 'copying',input_node._v_name
                    # copy everything from source to dest
                    input_node._f_copy(output_h5.root,recursive=True)

            fpc = realtime_image_analysis.FitParamsClass() # allocate FitParamsClass

            iterate_frames = ufmf_tools.iterate_frames # shorten notation
            for frame_enum,(frame_dict,frame) in enumerate(iterate_frames(
                h5_filename, ufmf_fnames,
                max_n_frames = max_n_frames,
                start = start,
                stop = stop,
                )):

                if (frame_enum%100)==0:
                    print '%s: frame %d'%(datetime_str,frame)

                for ufmf_fname in ufmf_fnames:
                    try:
                        frame_data = frame_dict[ufmf_fname]
                    except KeyError:
                        # no data saved (frame skip on Prosilica camera?)
                        continue
                    camn = frame_data['camn']
                    cam_id = frame_data['cam_id']
                    image = frame_data['image']
                    cam_received_timestamp=frame_data['cam_received_timestamp']
                    timestamp=frame_data['timestamp']
                    detected_points = True
                    obj_slices = None
                    if len(frame_data['regions'])==0:
                        # no data this frame -- go to next camera or frame
                        detected_points = False
                    if detected_points:
                        #print frame,cam_id,len(frame_data['regions'])
                        absdiff_im = abs(frame_data['mean'].astype(np.float32) -
                                         image)
                        thresh_val = np.max(absdiff_im)*absdiff_max_frac_thresh
                        thresh_val = max(min_absdiff,thresh_val)
                        thresh_im = absdiff_im > thresh_val
                        labeled_im,n_labels = scipy.ndimage.label(thresh_im)
                        if not n_labels:
                            detected_points = False
                        else:
                            obj_slices = scipy.ndimage.find_objects(labeled_im)
                    detection = out_data2d.row
                    if detected_points:
                        height,width = image.shape
                        if save_debug_images:
                            xarr = []
                            yarr = []
                        frame_pt_idx = 0
                        detected_points = False # possible not to find any below

                        for i in range(n_labels):
                            y_slice, x_slice = obj_slices[i]
                            # limit pixel operations to covering rectangle
                            this_labeled_im = labeled_im[y_slice,x_slice]
                            this_label_im = this_labeled_im==(i+1)

                            # calculate area (number of binarized pixels)
                            xsum = np.sum(this_label_im,axis=0)
                            pixel_area = np.sum(xsum)
                            if pixel_area < area_minimum_threshold:
                                continue

                            # calculate center
                            xpos = np.arange(x_slice.start,x_slice.stop,
                                             x_slice.step)
                            ypos = np.arange(y_slice.start,y_slice.stop,
                                             y_slice.step)

                            xmean = np.sum((xsum*xpos))/np.sum(xsum)
                            ysum = np.sum(this_label_im,axis=1)
                            ymean = np.sum((ysum*ypos))/np.sum(ysum)

                            if 1:
                                # This is not yet finished
                                this_absdiff_im = absdiff_im[y_slice,x_slice]
                                fast_foreground = FastImage.asfastimage(
                                    this_absdiff_im.astype(np.uint8) )
                                fail_fit = False
                                try:
                                    (x0_roi, y0_roi, weighted_area, slope,
                                     eccentricity) = fpc.fit(
                                        fast_foreground )
                                except realtime_image_analysis.FitParamsError,err:
                                    fail_fit = True
                                    print "frame %d, ufmf %s: fit failed"%(frame,
                                                                           ufmf_fname)
                                    print err
                            else:
                                fail_fit = True

                            if fail_fit:
                                slope = np.nan
                                eccentricity = np.nan
                                weighted_area = pixel_area

                            detection['camn']=camn
                            detection['frame']=frame
                            detection['timestamp']=timestamp
                            detection['cam_received_timestamp']=cam_received_timestamp
                            detection['x']=xmean
                            detection['y']=ymean
                            detection['area']=weighted_area
                            detection['slope']=slope
                            detection['eccentricity']=eccentricity
                            detection['frame_pt_idx']=frame_pt_idx
                            frame_pt_idx += 1
                            if save_debug_images:
                                xarr.append(xmean)
                                yarr.append(ymean)
                            detection.append()
                            detected_points = True

                        if save_debug_images:
                            save_fname_path = 'debug/debug_%s_%d.png'%(cam_id,
                                                                       frame)
                            print 'saving',save_fname_path
                            import benu
                            canv=benu.Canvas(save_fname_path,width,height)
                            maxlabel = np.max(labeled_im)
                            fact = int(np.floor(255.0/maxlabel))
                            canv.imshow((labeled_im*fact).astype(np.uint8),0,0)
                            canv.scatter(xarr, yarr,
                                         color_rgba=(0,1,0,1),
                                         radius=10,
                                         )
                            canv.save()

                    if not detected_points:
                        # If no point was tracked for this frame,
                        # still save timestamp.
                        detection['camn']=camn
                        detection['frame']=frame
                        detection['timestamp']=timestamp
                        detection['cam_received_timestamp']=cam_received_timestamp
                        detection['x']=np.nan
                        detection['y']=np.nan
                        detection.append()

def main():
    usage = '%prog DATAFILE2D.h5 [options]'

    parser = OptionParser(usage)

    parser.add_option("--ufmf-dir", type='string',
                      help="directory with .ufmf files")

    parser.add_option("--max-n-frames", type='int', default=None,
                      help="maximum number of frames to save")

    parser.add_option("--start", type='int', default=None,
                      help="start frame")

    parser.add_option("--stop", type='int', default=None,
                      help="stop frame")

    parser.add_option("--output-h5", type='string',
                      help="filename for output .h5 file with data2d_distorted")

    (options, args) = parser.parse_args()

    if len(args)<1:
        parser.print_help()
        return

    if options.output_h5 is None:
        raise ValueError('--output-h5 option must be specified')

    h5_filename = args[0]
    retrack_movies( h5_filename,
                    ufmf_dir = options.ufmf_dir,
                    max_n_frames = options.max_n_frames,
                    start = options.start,
                    stop = options.stop,
                    output_h5_filename=options.output_h5,
                    )