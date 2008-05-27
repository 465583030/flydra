# see a2/save_movies_overlay.py
from __future__ import division
import numpy
from numpy import nan, pi
import tables as PT
import tables.flavor
tables.flavor.restrict_flavors(keep=['numpy']) # ensure pytables 2.x
import pytz # from http://pytz.sourceforge.net/
import datetime
import sets
import sys
from optparse import OptionParser
import pylab
import flydra.reconstruct
import flydra.analysis.result_utils as result_utils
import matplotlib.cm as cm

def auto_subplot(fig,n,n_rows=2,n_cols=3):
    # 2 rows and n_cols

    rrow = n // n_cols # reverse row
    row = n_rows-rrow-1 # row number
    col = n % n_cols

    x_space = (0.02/n_cols)
    #y_space = 0.0125
    y_space = 0.03
    y_size = (1.0/n_rows)-(2*y_space)

    left = col*(1.0/n_cols) + x_space
    bottom = row*y_size + y_space
    w = (1.0/n_cols) - x_space
    h = y_size - 2*y_space
    return fig.add_axes([left,bottom,w,h])


class ShowIt(object):
    def __init__(self):
        self.subplot_by_cam_id = {}
        self.reconstructor = None
        self.cam_ids_and_points2d = []

    def find_cam_id(self,ax):
        found = False
        for cam_id, axtest in self.subplot_by_cam_id.iteritems():
            if ax is axtest:
                found = True
                break
        if not found:
            raise RuntimeError("event in unknown axes")
        return cam_id

    def on_key_press(self,event):

        if event.key=='c':
            del self.cam_ids_and_points2d[:]

        elif event.key=='i':
            if self.reconstructor is None:
                return

            X = self.reconstructor.find3d(self.cam_ids_and_points2d,return_X_coords = True, return_line_coords=False)
            print 'maximum liklihood intersection:'
            print repr(X)
            if 1:
                print 'reprojection errors:'
                for (cam_id, value_tuple) in self.cam_ids_and_points2d:
                    newx,newy=self.reconstructor.find2d(cam_id,X,distorted=True)
                    origx,origy=self.reconstructor.undistort(cam_id, value_tuple[:2] )
                    reproj_error = numpy.sqrt((newx-origx)**2+(newy-origy)**2)
                    print '  %s: %.1f'%(cam_id,reproj_error)
                print

            for cam_id, ax in self.subplot_by_cam_id.iteritems():
                newx,newy=self.reconstructor.find2d(cam_id,X,distorted=True)
                xlim = ax.get_xlim()
                ylim = ax.get_ylim()
                ax.plot([newx],[newy],'co',ms=5)
                ax.set_xlim(xlim)
                ax.set_ylim(ylim)
            pylab.draw()

        elif event.key=='p':
            # new point -- project onto other images

            if not event.inaxes:
                print 'not in axes -- nothing to do'
                return

            ax = event.inaxes  # the axes instance
            cam_id = self.find_cam_id(ax)

            xlim = ax.get_xlim()
            ylim = ax.get_ylim()
            ax.plot([event.xdata],[event.ydata],'bx')
            ax.set_xlim(xlim)
            ax.set_ylim(ylim)

            if self.reconstructor is None:
                return

            x,y = self.reconstructor.undistort(cam_id, [event.xdata, event.ydata])
            self.cam_ids_and_points2d.append(  (cam_id, (x,y)) )
            line3d = self.reconstructor.get_projected_line_from_2d(cam_id,[x,y])

            cam_ids = self.subplot_by_cam_id.keys()
            cam_ids.sort()

            for other_cam_id in cam_ids:
                if other_cam_id == cam_id:
                    continue
                xs,ys = self.reconstructor.get_distorted_line_segments(other_cam_id, line3d) # these are distorted
                ax = self.subplot_by_cam_id[other_cam_id]
                #print xs
                #print ys
                xlim = ax.get_xlim()
                ylim = ax.get_ylim()
                ax.plot(xs,ys,'b-')
                ax.set_xlim(xlim)
                ax.set_ylim(ylim)
            pylab.draw()

    def show_it(self,
                fig,
                filename,
                kalman_filename = None,
                frame_start = None,
                frame_stop = None,
                show_nth_frame = None,
                reconstructor_filename=None,
                ):

        if show_nth_frame == 0:
            show_nth_frame = None

        results = result_utils.get_results(filename,mode='r')
        if hasattr(results.root,'images'):
            img_table = results.root.images
        else:
            img_table = None

        if 0:
            if hasattr(results.root,'calibration'):
                self.reconstructor = flydra.reconstruct.Reconstructor(results)
        else:
            reconstructor_source = None
            if reconstructor_filename is None:
                if kalman_filename is not None:
                    reconstructor_source = kalman_filename
                elif hasattr(results.root,'calibration'):
                    reconstructor_source = results
            else:
                reconstructor_source = reconstructor_filename

            if reconstructor_source is not None:
                self.reconstructor = flydra.reconstruct.Reconstructor(reconstructor_source)

        if 1:
            self.reconstructor = self.reconstructor.get_scaled()

        camn2cam_id, cam_id2camns = result_utils.get_caminfo_dicts(results)

        data2d = results.root.data2d_distorted # make sure we have 2d data table

        debugADS = False
        if debugADS:
            for row in data2d.where(data2d.cols.frame==11900):
                print '%d: %s'%(row.nrow,str(row))

        print 'reading frames...'
        frames = data2d.read(field='frame')
        print 'OK'

        if frame_start is not None:
            print 'selecting frames after start'
            #after_start = data2d.getWhereList( 'frame>=frame_start')
            after_start = numpy.nonzero(frames>=frame_start)[0]
        else:
            after_start = None

        if frame_stop is not None:
            print 'selecting frames before stop'
            #before_stop = data2d.getWhereList( 'frame<=frame_stop')
            before_stop = numpy.nonzero(frames<=frame_stop)[0]
        else:
            before_stop = None

        print 'finding all frames'
        if after_start is not None and before_stop is not None:
            use_idxs = numpy.intersect1d(after_start,before_stop)
        elif after_start is not None:
            use_idxs = after_start
        elif before_stop is not None:
            use_idxs = before_stop
        else:
            use_idxs = numpy.arange(data2d.nrows)

        # OK, we have data coords, plot

        print 'reading cameras'
        frames = frames[use_idxs]#data2d.readCoordinates( use_idxs, field='frame')
        print 'frame range: %d - %d'%( frames[0], frames[-1] )
        camns = data2d.read(field='camn')
        camns = camns[use_idxs]
        #camns = data2d.readCoordinates( use_idxs, field='camn')
        unique_camns = numpy.unique1d(camns)
        unique_cam_ids = list(sets.Set([camn2cam_id[camn] for camn in unique_camns]))
        unique_cam_ids.sort()
        print '%d cameras with data'%(len(unique_cam_ids),)

        if len(unique_cam_ids)==1:
            n_rows=1
            n_cols=1
        elif len(unique_cam_ids)<=6:
            n_rows=2
            n_cols=3
        elif len(unique_cam_ids)<=12:
            n_rows=3
            n_cols=4
        else:
            n_rows=4
            n_cols=int( math.ceil(len(unique_cam_ids)/n_rows))

        for i,cam_id in enumerate(unique_cam_ids):
            ax = auto_subplot(fig,i,n_rows=n_rows,n_cols=n_cols)
            ax.set_title( '%s: %s'%(cam_id,str(cam_id2camns[cam_id])) )
    ##        ax.set_xticks([])
    ##        ax.set_yticks([])
            self.subplot_by_cam_id[cam_id] = ax

        for camn in unique_camns:
            cam_id = camn2cam_id[camn]
            ax = self.subplot_by_cam_id[cam_id]
            this_camn_idxs = use_idxs[camns == camn]

            xs = data2d.readCoordinates( this_camn_idxs, field='x')
            ys = data2d.readCoordinates( this_camn_idxs, field='y')

            if img_table is not None:
                bg_arr_h5 = getattr(img_table,cam_id)
                bg_arr = bg_arr_h5.read()
                ax.imshow( bg_arr, origin='lower',cmap=cm.pink )

            valid_idx = numpy.nonzero( ~numpy.isnan(xs) )[0]
            if not len(valid_idx):
                continue
            idx_first_valid = valid_idx[0]
            idx_last_valid = valid_idx[-1]
            tmp_frames = data2d.readCoordinates( this_camn_idxs, field='frame')

            ax.plot([xs[idx_first_valid]],[ys[idx_first_valid]],
                    'ro',label='first point')

            ax.plot(xs,ys,'g.',label='all points')

            ax.plot([xs[idx_last_valid]],[ys[idx_last_valid]],
                    'bo',label='first point')

            if show_nth_frame is not None:
                for i,f in enumerate(tmp_frames):
                    if f%show_nth_frame==0:
                        ax.text(xs[i],ys[i],'%d'%(f,))

            if 0:
                for x,y,frame in zip(xs[::5],ys[::5],tmp_frames[::5]):
                    ax.text(x,y,'%d'%(frame,))

            if self.reconstructor is not None:
                res = self.reconstructor.get_resolution(cam_id)
                ax.set_xlim([0,res[0]])
                #ax.set_ylim([0,res[1]])
                ax.set_ylim([res[1],0])
            elif bg_arr is not None:
                ax.set_xlim([0,bg_arr.shape[1]])
                #ax.set_ylim([0,res[1]])
                ax.set_ylim([bg_arr.shape[0],0])

        binding_id = fig.canvas.mpl_connect('key_press_event', self.on_key_press)

        if kalman_filename is None:
            return

        # Do same as above for Kalman-filtered data

        kresults = PT.openFile(kalman_filename,mode='r')
        kobs = kresults.root.kalman_observations
        kframes = kobs.read(field='frame')
        if frame_start is not None:
            k_after_start = numpy.nonzero( kframes>=frame_start )[0]
            #k_after_start = kobs.readCoordinates(idxs)
            #k_after_start = kobs.getWhereList(
            #    'frame>=frame_start')
        else:
            k_after_start = None
        if frame_stop is not None:
            k_before_stop = numpy.nonzero( kframes<=frame_stop )[0]
            #k_before_stop = kobs.readCoordinates(idxs)
            #k_before_stop = kobs.getWhereList(
            #    'frame<=frame_stop')
        else:
            k_before_stop = None

        if k_after_start is not None and k_before_stop is not None:
            k_use_idxs = numpy.intersect1d(k_after_start,k_before_stop)
        elif k_after_start is not None:
            k_use_idxs = k_after_start
        elif k_before_stop is not None:
            k_use_idxs = k_before_stop
        else:
            k_use_idxs = numpy.arange(kobs.nrows)

        obj_ids = kobs.read(field='obj_id')[k_use_idxs]
        #obj_ids = kobs.readCoordinates( k_use_idxs,
        #                                field='obj_id')
        obs_2d_idxs = kobs.read(field='obs_2d_idx')[k_use_idxs]
        #obs_2d_idxs = kobs.readCoordinates( k_use_idxs,
        #                                    field='obs_2d_idx')
        kframes = kframes[k_use_idxs]#kobs.readCoordinates( k_use_idxs,
                                      # field='frame')

        kobs_2d = kresults.root.kalman_observations_2d_idxs
        xys_by_obj_id = {}
        for obj_id,kframe,obs_2d_idx in zip(obj_ids,kframes,obs_2d_idxs):
            obs_2d_idx_find = int(obs_2d_idx) # XXX grr, why can't pytables do this?
            obj_id_save = int(obj_id) # convert from possible numpy scalar
            xys_by_cam_id = xys_by_obj_id.setdefault( obj_id_save, {})
            kobs_2d_data = kobs_2d.read( start=obs_2d_idx_find,
                                         stop=obs_2d_idx_find+1 )
            assert len(kobs_2d_data)==1
            kobs_2d_data = kobs_2d_data[0]
            this_camns = kobs_2d_data[0::2]
            this_camn_idxs = kobs_2d_data[1::2]

            this_use_idxs = use_idxs[frames==kframe]
            if debugADS:
                print
                print kframe,'==============='
                print 'this_use_idxs', this_use_idxs

            try:
                d2d = data2d.readCoordinates( this_use_idxs )
            except:
                print repr(this_use_idxs)
                print type(this_use_idxs)
                print this_use_idxs.dtype
                raise
            if debugADS:
                print 'd2d -=--=--=--=--=-'
                for row in d2d:
                    print row
            for this_camn,this_camn_idx in zip(this_camns,this_camn_idxs):
                this_idxs_tmp = numpy.nonzero(d2d['camn'] == this_camn)[0]
                this_camn_d2d = d2d[d2d['camn'] == this_camn]
                found = False
                for this_row in this_camn_d2d: # XXX could be sped up
                    if this_row['frame_pt_idx'] == this_camn_idx:
                        found = True
                        break
                if not found:
                    if 1:
                        print 'WARNING:point not found in data -- 3D data starts before 2D I guess.'
                        continue
                    else:
                        raise RuntimeError('point not found in data!?')
                #this_row = this_camn_d2d[this_camn_idx]
                this_cam_id = camn2cam_id[this_camn]
                xys = xys_by_cam_id.setdefault( this_cam_id, ([],[]) )
                xys[0].append( this_row['x'] )
                xys[1].append( this_row['y'] )

        for obj_id in xys_by_obj_id:
            xys_by_cam_id = xys_by_obj_id[obj_id]
            for cam_id, (xs,ys) in xys_by_cam_id.iteritems():
                ax = self.subplot_by_cam_id[cam_id]
                if 0:
                    ax.plot(xs,ys,label='obs: %d'%obj_id)
                else:
                    ax.plot(xs,ys,'x-',label='obs: %d'%obj_id)
                ax.text(xs[0],ys[0],'%d:'%(obj_id,))
                ax.text(xs[-1],ys[-1],':%d'%(obj_id,))

        if 0:
            for cam_id in self.subplot_by_cam_id.keys():
                ax = self.subplot_by_cam_id[cam_id]
                ax.legend()
        print 'note: could/should also plot re-projection of Kalman filtered/smoothed data'

        results.close()
        kresults.close()

def main():
    usage = '%prog FILE [options]'

    parser = OptionParser(usage)

    parser.add_option("-f", "--file", dest="filename", type='string',
                      help="hdf5 file with data to display FILE",
                      metavar="FILE")

    parser.add_option('-k', "--kalman-file", dest="kalman_filename", type='string',
                      help="hdf5 file with kalman data to display KALMANFILE",
                      metavar="KALMANFILE")

    parser.add_option("-r", "--reconstructor", dest="reconstructor_path", type='string',
                      help="calibration/reconstructor path (if not specified, defaults to FILE)",
                      metavar="RECONSTRUCTOR")

    parser.add_option("--start", type="int",
                      help="first frame to plot",
                      metavar="START")

    parser.add_option("--stop", type="int",
                      help="last frame to plot",
                      metavar="STOP")

    parser.add_option("--show-nth-frame", type="int",
                      dest='show_nth_frame',
                      help='show Nth frame number (0=none)')

    (options, args) = parser.parse_args()

    if options.filename is not None:
        args.append(options.filename)

    if len(args)>1:
        print >> sys.stderr,  "arguments interpreted as FILE supplied more than once"
        parser.print_help()
        return

    if len(args)<1:
        parser.print_help()
        return

    h5_filename=args[0]

    fig = pylab.figure()
    showit = ShowIt()
    showit.show_it(fig,
                   h5_filename,
                   kalman_filename = options.kalman_filename,
                   frame_start = options.start,
                   frame_stop = options.stop,
                   show_nth_frame = options.show_nth_frame,
                   reconstructor_filename=options.reconstructor_path,
                   )
    pylab.show()

if __name__=='__main__':
    main()
