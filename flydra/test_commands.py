"""test flydra installed system commands"""
import pkg_resources
import os, subprocess, tempfile, shutil, sys, warnings
import numpy as np
import scipy.misc
from optparse import OptionParser
import nose

AUTOGEN_DIR = os.path.join(os.path.split(__file__)[0],'autogenerated')
GALLERY_PATH = os.path.join(os.path.split(__file__)[0],'..',
                            'flydra-sphinx-docs','gallery.rst')

DATAFILE2D = pkg_resources.resource_filename('flydra.a2','sample_datafile.h5')
DATAFILE3D = pkg_resources.resource_filename('flydra.a2','sample_datafile.h5')
CALIB = pkg_resources.resource_filename('flydra.a2','sample_calibration.xml')

def _get_names_dict(data2d,data3d,calib):
    DATAFILE2D_NOEXT = os.path.splitext(data2d)[0]
    DATAFILE3D_NOEXT = os.path.splitext(data3d)[0]
    names = dict(DATAFILE2D=data2d,
                 DATAFILE3D=data3d,
                 DATAFILE2D_NOEXT=DATAFILE2D_NOEXT,
                 DATAFILE3D_NOEXT=DATAFILE3D_NOEXT,
                 CALIB=calib,
                 )
    return names

def _my_call(cmd):
    """grab stdout and stderr, only show them if error"""
    tmp_stdout = tempfile.TemporaryFile()
    tmp_stderr = tempfile.TemporaryFile()
    try:
        subprocess.check_call(cmd, shell=True,
                              stdout=tmp_stdout,
                              stderr=tmp_stderr,
                              )
    except:
        tmp_stdout.seek(0)
        buf=tmp_stdout.read()
        sys.stdout.write(buf)

        tmp_stderr.seek(0)
        buf=tmp_stderr.read()
        sys.stderr.write(buf)
        raise

# image based commands
image_info = [
    {'cmd':('flydra_analysis_plot_kalman_2d %(DATAFILE2D)s '
            '--save-fig=%(target)s'),
     'suffix':'.png',
     'result':'plot_kalman_2d.png',
     'title':'Camera view of 2D data',
     },

    {'cmd':('flydra_analysis_plot_timeseries_2d_3d %(DATAFILE2D)s '
            '--save-fig=%(target)s --hide-source-name'),
     'suffix':'.png',
     'result':'plot_timeseries_2d.png',
     'title':'Timeseries of 2D data',
     },

    {'cmd':('flydra_analysis_plot_timeseries_2d_3d %(DATAFILE2D)s '
            '--kalman-file=%(DATAFILE3D)s --disable-kalman-smoothing '
            '--save-fig=%(target)s --likely-only --hide-source-name'),
     'suffix':'.png',
     'result':'plot_timeseries_2d_3d.png',
     'title':'Timeseries of 2D and 3D data',
     'rst_comments':"""The ``--likely-only`` argument limits
the 2D data plotted."""

     },

    ]

# non-image based commands
command_info =  [
    {'cmd':('flydra_kalmanize %(DATAFILE2D)s --reconstructor=%(CALIB)s '
            '--max-err=10.0 --min-observations-to-save=10 '
            '--dest-file=%(target)s'),
     'noshow_cmd':' --fake-timestamp=123456.7',
     'outfile':'%(DATAFILE2D_NOEXT)s.kalmanized.h5',
     'result':'kalmanized.h5',
     'suffix':'.h5',
     'rst_comments':"""This re-runs the data association algorithm. It
is useful to do this because the original realtime run may have
skipped some processing to meet realtime constraints or because a
better calibration is known. The new data are saved to an .h5 file
named ``%(outfile)s``.
"""
     },
    {'cmd':('flydra_analysis_data2smoothed %(DATAFILE3D)s '
            '--time-data=%(DATAFILE2D)s --dest-file=%(target)s'),
     'outfile':'%(DATAFILE3D_NOEXT)s_smoothed.mat',
     'suffix':'.mat',
     'result':'data2smoothed.mat',
     'rst_comments':"""This produces a .mat file named
``%(outfile)s``. This file contains smoothed tracking data in addition
to (unsmoothed) maximum likelihood position estimates."""
     },
    ]


gallery_rst_src = """
Gallery
*******

This page shows images that were automatically generated by the
command line tools installed with flydra. The command line used to
generate each figure is shown. These figures also serve as unit tests
for flydra -- the stored versions are compared with newly generated
versions whenever nosetests_ is run.

.. _nosetests: http://somethingaboutorange.com/mrl/projects/nose/

.. This file generated by flydra_test_commands --generate. EDITS WILL BE LOST.

Image gallery
=============

%(image_gallery)s

Command gallery
===============

%(command_gallery)s

"""

def test_image_generating_commands():
    for info in image_info:
        yield check_command, 'check', info

def test_commands():
    for info in command_info:
        yield check_command, 'check', info

def break_long_lines(cmd):
    components = cmd.split()
    outputs = [components.pop(0)]
    for component in components:
        test_line = outputs[-1] + ' ' + component
        if len(test_line) < 79:
            # Test line is short. Take it.
            outputs[-1] = test_line
        else:
            # Break line
            outputs[-1] = outputs[-1]+' \\'
            outputs.append('       '+component)
    result = '\n'.join(outputs)
    return result

def test_break_long_lines():
    assert break_long_lines('short')=='short'
    assert (break_long_lines('short but multiple words') ==
            'short but multiple words')

def _get_cmd_show_and_comments(info):
    names = _get_names_dict('DATAFILE2D.h5',
                            'DATAFILE3D.h5',
                            'CALIBRATION.xml')

    names['target']='image.png'
    if 'outfile' in info:
        names['outfile'] = info['outfile']%names
        names['target'] = names['outfile']

    cmd_show = info['cmd']%names
    cmd_show = break_long_lines(cmd_show)

    rst_comments = None
    if 'rst_comments' in info:
        rst_comments = info['rst_comments']%names
    return cmd_show, rst_comments

def generate_inner_loop(info):
    command_gallery = ''
    check_command( 'generate', info )
    cmd_show, rst_comments = _get_cmd_show_and_comments(info)

    if 'title' in info:
        title = info['title']
    else:
        title = info['cmd'].split()[0]
    command_gallery += title+'\n'
    command_gallery += '.'*len(title)+'\n'
    command_gallery += '\n'
    command_gallery += '::\n\n'
    command_gallery += '  '+cmd_show+'\n\n'
    if rst_comments is not None:
        command_gallery += rst_comments + '\n'
    command_gallery += '\n'
    return command_gallery

def generate_commands(info):
    command_gallery = []
    for this_info in info:
        command_gallery.append(generate_inner_loop(this_info))
    return command_gallery

def filter_image_gallery( image_gallery_list, result_names, widths ):
    result = []
    for ig,result_name,width in zip(image_gallery_list,result_names,widths):
        igs = ig.split('\n')
        new_igs = []
        for igi in igs:
            if igi=='::':
                igi='The following command generated this image::'
            new_igs.append(igi)
        ig = '\n'.join(new_igs)
        ig += """
.. image:: ../flydra/autogenerated/%(result)s
  :width: %(width)d
"""%{'result':result_name,'width':width}

        result.append(ig)
    return result

def generate():
    image_gallery = generate_commands(image_info)
    image_gallery=filter_image_gallery( image_gallery,
                                        [i['result'] for i in image_info],
                                        [600]*len(image_gallery),
                                        )
    image_gallery = '\n'.join(image_gallery)

    command_gallery = generate_commands(command_info)
    command_gallery = '\n'.join(command_gallery)

    gallery_rst = gallery_rst_src%{'image_gallery':image_gallery,
                                   'command_gallery':command_gallery}
    fd = open(GALLERY_PATH,mode='w')
    fd.write(gallery_rst)
    fd.close()

def check_command(mode,info):
    checker_function_dict = {'.png':are_images_close,
                             '.h5':are_pytables_close,
                             }

    assert mode in ['check','generate']
    result_fullpath = os.path.join( AUTOGEN_DIR, info['result'] )
    names = _get_names_dict(DATAFILE2D,DATAFILE3D,CALIB)

    suffix = info.get('suffix','')
    handle, target = tempfile.mkstemp(suffix)
    try:
        os.unlink(target) # erase first temporary file
        names['target']=target
        cmd = info['cmd']%names
        cmd += info.get('noshow_cmd','')
        _my_call(cmd)

        checker_function = checker_function_dict.get(suffix,are_files_close)

        if mode=='check':
            if info.get('compare_results',True):
                are_close = checker_function( target, result_fullpath )
                assert are_close == True, '%s returned False'%checker_function
        elif mode=='generate':
            if info.get('compare_results',True):
                shutil.move( target, result_fullpath )
    finally:
        # cleanup after ourselves
        if os.path.exists(target):
            os.unlink(target)

def are_files_close(filename1, filename2):
    fd1 = open(filename1)
    fd2 = open(filename2)
    are_close = True
    while 1:
        buf1 = fd1.read(1024*1024*8)
        buf2 = fd2.read(1024*1024*8)
        if not buf1 == buf2:
            are_close = False
            break
        if len(buf1)==0:
            break
    return are_close

def are_images_close( im1_filename, im2_filename,
                      ok_fraction_threshold=0.99):
    """return True if two image files are very similar"""
    im1 = scipy.misc.pilutil.imread(im1_filename)
    im2 = scipy.misc.pilutil.imread(im2_filename)
    if im1.ndim != im2.ndim:
        raise ValueError('images have different ndim')
    if im1.shape != im2.shape:
        raise ValueError('images have different shape')
    if np.allclose( im1, im2 ):
        # identical -- no more testing needed
        return True

    # maybe-3D difference image
    di = abs(im1.astype(np.float)-im2.astype(np.float) )
    if di.ndim==3:
        # flatten
        di2d = np.mean(di,axis=2)
    else:
        di2d = di
    n_diff = np.sum(di2d > 0.1)
    n_total = di2d.shape[0]* di2d.shape[1]
    fraction_different = n_diff/float(n_total)
    fraction_same = 1.0-fraction_different
    result = fraction_same>=ok_fraction_threshold
    if result == False:
        print 'fraction_same=%s'%fraction_same
    return result

def are_pytables_close(filename1, filename2):
    import tables

    def are_pytables_groups_close(g1,g2):
        result = True

        for f1_node in g1._f_iterNodes():
            f2_node = g2._f_getChild(f1_node._v_name)

            if isinstance(f1_node,tables.Group):
                is_close = are_pytables_groups_close(f1_node,f2_node)
            elif (isinstance(f1_node,tables.Table) or
                  isinstance(f1_node,tables.VLArray)):
                if len(f1_node) != len(f2_node):
                    is_close = False
                else:
                    for row1,row2 in zip(f1_node,f2_node):
                        # read rows
                        r1 = row1[:]
                        r2 = row2[:]
                        if isinstance(r1,tuple):
                            is_close=r1==r2
                        else:
                            # assume numpy
                            is_close=np.allclose(r1,r2)
                        if not is_close:
                            break
            elif isinstance(f1_node,tables.Array):
                a1=np.array(f1_node)
                a2=np.array(f2_node)
                if a1.ndim != a2.ndim:
                    is_close = False
                elif a1.shape != a2.shape:
                    is_close = False
                else:
                    is_close = np.allclose(a1,a2)
            else:
                warnings.warn('not checking leaf %s'%f1_node)
                continue
            if not is_close:
                result = False
                break
        return result

    f1 = tables.openFile(filename1,mode='r')
    f2 = tables.openFile(filename2,mode='r')
    result = are_pytables_groups_close( f1.root, f2.root )
    f1.close()
    f2.close()
    return result

def main():
    usage = '%prog FILE [options]'

    parser = OptionParser(usage)
    parser.add_option("--generate", action='store_true',
                      default=False)
    (options, args) = parser.parse_args()
    if options.generate:
        generate()
    else:
        nose.main() # how to limit to just this module?

if __name__=='__main__':
    main()
