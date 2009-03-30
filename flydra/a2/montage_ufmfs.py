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

def get_tile(N):
    rows = int(np.ceil(np.sqrt(float(N))))
    cols = rows
    return '%dx%d'%(rows,cols)

def make_montage( h5_filename,
                  ufmf_dir=None,
                  dest_dir = None,
                  white_background=False,
                  save_ogv_movie = False,
                  no_remove = False,
                  max_n_frames = None,
                  start = None,
                  stop = None,
                  ):
    ufmf_fnames = auto_discover_ufmfs.find_ufmfs( h5_filename,
                                                  ufmf_dir=ufmf_dir,
                                                  careful=True )

    if dest_dir is None:
        dest_dir = os.curdir
    else:
        if not os.path.exists( dest_dir ):
            os.makedirs(dest_dir)

    # get name of data

    datetime_str = os.path.splitext(os.path.split(h5_filename)[-1])[0]
    datetime_str = datetime_str[4:19]

    all_frame_montages = []
    for frame_enum,(frame_dict,frame) in enumerate(ufmf_tools.iterate_frames(
        h5_filename, ufmf_fnames,
        white_background=white_background,
        max_n_frames = max_n_frames,
        start = start,
        stop = stop,
        )):

        if (frame_enum%100)==0:
            print '%s: frame %d'%(datetime_str,frame)

        saved_fnames = []
        for cam_id, frame_data in frame_dict.iteritems():
            save_fname = 'tmp_frame%07d_%s.bmp'%(frame,cam_id)
            save_fname_path = os.path.join(dest_dir, save_fname)
            image = frame_data['image']
            scipy.misc.pilutil.imsave(save_fname_path, image)
            saved_fnames.append( save_fname_path )

        target = os.path.join(dest_dir, 'movie%s_frame%07d.jpg'%(
            datetime_str,frame_enum+1 ))
        tile = get_tile( len(saved_fnames) )
        imnames = ' '.join(saved_fnames)
        # All cameras saved for this frame, make montage
        CMD=("montage %s -mode Concatenate -tile %s -bordercolor white "
             "-title '%s frame %d' "
             "-border 2 %s"%(imnames, tile, datetime_str, frame, target))
        #print CMD
        subprocess.check_call(CMD,shell=True)
        all_frame_montages.append( target )
        if not no_remove:
            for fname in saved_fnames:
                os.unlink(fname)

    if save_ogv_movie:
        orig_dir = os.path.abspath(os.curdir)
        os.chdir(dest_dir)
        try:
            CMD = 'ffmpeg2theora -v 10 movie%s_frame%%07d.jpg -o movie%s.ogv'%(
                datetime_str,datetime_str)
            subprocess.check_call(CMD,shell=True)
        finally:
            os.chdir(orig_dir)

        if not no_remove:
            for fname in all_frame_montages:
                os.unlink(fname)

def main():
    usage = '%prog DATAFILE2D.h5 [options]'

    parser = OptionParser(usage)

    parser.add_option("--dest-dir", type='string',
                      help="destination directory to save resulting files")

    parser.add_option("--ufmf-dir", type='string',
                      help="directory with .ufmf files")

    parser.add_option("--max-n-frames", type='int', default=None,
                      help="maximum number of frames to save")

    parser.add_option("--start", type='int', default=None,
                      help="start frame")

    parser.add_option("--stop", type='int', default=None,
                      help="stop frame")

    parser.add_option("--ogv", action='store_true', default=False,
                      help="export .ogv video")

    parser.add_option('-n', "--no-remove", action='store_true', default=False,
                      help="don't remove intermediate images")

    parser.add_option("--white-background", action='store_true', default=False,
                      help="don't display background information")

    (options, args) = parser.parse_args()

    if len(args)<1:
        parser.print_help()
        return

    h5_filename = args[0]
    make_montage( h5_filename,
                  ufmf_dir = options.ufmf_dir,
                  dest_dir = options.dest_dir,
                  save_ogv_movie = options.ogv,
                  no_remove = options.no_remove,
                  white_background = options.white_background,
                  max_n_frames = options.max_n_frames,
                  start = options.start,
                  stop = options.stop,
                  )
