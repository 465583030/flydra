import time, StringIO, sets, sys
import math, os, struct, glob
import numpy
import numpy as nx
import numpy as mlab
import numarray
import tables as PT
import matplotlib
import pylab
import matplotlib.ticker
from pylab import figure, plot, clf, imshow, cm, setp, figtext
from pylab import gca, title, axes, ion, ioff, gcf, savefig
from matplotlib.ticker import LinearLocator
import PQmath
import cgtypes
import threading

import flydra.undistort as undistort
import FlyMovieFormat

from numpy import nan,inf
import numarray # needed for pytables

# flydra analysis tools:
import caching_movie_opener 
from result_utils import get_camn, get_camn_and_frame, \
     get_camn_and_remote_timestamp, get_cam_ids, \
     get_caminfo_dicts, get_results, get_f_xyz_L_err, \
     get_reconstructor, get_resolution, status

##try:
##    import Pyro.core, Pyro.errors
##except ImportError,x:
##    print 'skipping Pyro import:',x
##else:
##    Pyro.core.initClient(banner=0)

##PROXY_PYRO = False

class ExactROIFrameMovieInfo(PT.IsDescription):
    cam_id             = PT.StringCol(16,pos=0)
    camn               = PT.Int32Col(pos=1)#,indexed=True)
    filename           = PT.StringCol(255,pos=2)
    frame              = PT.Int32Col(pos=3,indexed=True)
    fmf_frame          = PT.Int32Col(pos=4)
    timestamp          = PT.FloatCol(pos=5,indexed=True)
    left               = PT.Int32Col(pos=6)
    bottom             = PT.Int32Col(pos=7)

class SmallFMFSummary(PT.IsDescription):
    cam_id             = PT.StringCol(16,pos=0)
    camn               = PT.Int32Col(pos=1,indexed=True)
    start_timestamp    = PT.FloatCol(pos=2)
    stop_timestamp     = PT.FloatCol(pos=3)  
    basename           = PT.StringCol(255,pos=4)

class IgnoreFrames(PT.IsDescription):
    start_frame        = PT.Int32Col(pos=0)
    stop_frame         = PT.Int32Col(pos=1)

class SmoothData(PT.IsDescription):
    frame = PT.Int32Col(pos=0)
    x     = PT.FloatCol(pos=1)
    y     = PT.FloatCol(pos=2)
    z     = PT.FloatCol(pos=3)
    qw    = PT.FloatCol(pos=4)
    qx    = PT.FloatCol(pos=5)
    qy    = PT.FloatCol(pos=6)
    qz    = PT.FloatCol(pos=7)

class TimedVectors(PT.IsDescription):
    frame = PT.Int32Col(pos=0)
    fx    = PT.FloatCol(pos=1)
    fy    = PT.FloatCol(pos=2)
    fz    = PT.FloatCol(pos=3)

class LinearInterpolator:
    def __init__(self,x,y):
        x = numpy.asarray(x)
        self.minx = x.min()
        self.maxx = x.max()
        self.meanx = numpy.mean(x) # minimize numerical errors
        a1 = x[:,numpy.newaxis] - self.meanx
        a2 = numpy.ones( (len(x),1))
        A = numpy.hstack((a1, a2))
        b = numpy.asarray(y)[:,numpy.newaxis]
        
        solution,resids,rank,s = numpy.linalg.lstsq(A,b)
        self.gain = solution[0,0]
        self.offset = solution[1,0]
        
    def __call__(self,newx):
        if numpy.any(newx > self.maxx) or numpy.any(newx < self.minx):
            raise ValueError('will not extrapolate data ~(min(x) <= newx <= max(x))')
        return (newx-self.meanx)*self.gain + self.offset
        
def make_new_fmt(results):
    """convert all 2D data into camera-by-camera tables"""
    Info2D = results.root.data2d.description # 2D data format for PyTables
    if hasattr(results.root, 'cam_by_cam_2d'):
        return # already made
    # (can delete with results.root.cam_by_cam_2d._f_remove() )
    # or results.removeNode( results.root.cam_by_cam_2d, recursive=True)
    status("making new cam_by_cam_2d tables...")
    cam_by_cam_2d = results.createGroup( results.root, 'cam_by_cam_2d' )
    
    tables_by_camn = {}
    for oldrow in results.root.data2d:
        camn = oldrow['camn']
        if camn not in tables_by_camn:
            table_name = 'camn'+str(camn)
            tables_by_camn[camn] = results.createTable( cam_by_cam_2d, table_name,
                                                        Info2D, "2d data" )
        table = tables_by_camn[camn]
        newrow = table.row
        for attr in Info2D._v_names:
            newrow[attr] = oldrow[attr] #copy all attributes
        newrow.append()
        
    for camn,table in tables_by_camn.iteritems():
        table.flush()
    status("done")

def save_ascii_matrix(thefile,m):
    if hasattr(thefile,'write'):
        fd=thefile
    else:
        fd=open(thefile,mode='wb')
    for row in m:
        fd.write( ' '.join(map(str,row)) )
        fd.write( '\n' )

def my_subplot(n):
    x_space = 0.05
    y_space = 0.15
    
    left = n*0.2 + x_space
    bottom = 0 + + y_space
    w = 0.2 - (x_space*1.5)
    h = 1.0 - (y_space*2)
    return axes([left,bottom,w,h])

def dougs_subplot(n,n_rows=2,n_cols=3):
    # 2 rows and n_cols
    
    rrow = n // n_cols # reverse row
    row = n_rows-rrow-1 # row number
    col = n % n_cols
    
    x_space = (0.02/n_cols)
    #y_space = 0.0125
    y_space = 0.07

    y_size = 0.42
    
    left = col*(1.0/n_cols) + x_space
    bottom = row*y_size + y_space
    w = (1.0/n_cols) - x_space
    h = y_size - 2*y_space
    return axes([left,bottom,w,h])

##proxy_spawner = None
##def get_server(cam_id,port=9888):
##    if PROXY_PYRO:
##        import urlparse, socket
##        global proxy_spawner

##        if proxy_spawner is None:
##            hostname = 'localhost' # requires tunnelling (e.g. over ssh)

##            proxy_URI = "PYROLOC://%s:%d/%s" % (hostname,port,'proxy_spawner')
##            print 'connecting to',proxy_URI,'...',
##            proxy_spawner = Pyro.core.getProxyForURI(proxy_URI)

##        # make sure URI is local (so we can forward through tunnel)
##        URI = str(proxy_spawner.spawn_proxy(cam_id))
##        URI=URI.replace('PYRO','http') # urlparse chokes on PYRO://
##        URIlist = list( urlparse.urlsplit(str(URI)) )
##        network_location = URIlist[1]
##        localhost = socket.gethostbyname(socket.gethostname())
##        port = network_location.split(':')[1]
##        URIlist[1] = '%s:%s'%(localhost,port)
##        URI = urlparse.urlunsplit(URIlist)
##        URI=URI.replace('http','PYRO')
##    else:
##        hostname = cam_id.split(':')[0]

##        URI = "PYROLOC://%s:%d/%s" % (hostname,port,'frame_server')

##    frame_server = Pyro.core.getProxyForURI(URI)
##    frame_server.noop()
        
##    return frame_server    
        
def flip_line_direction(results,frame,typ='best'):
    if typ=='best':
        data3d = results.root.data3d_best
    elif typ=='fastest':
        data3d = results.root.data3d_fastest

    nrow = None
    for row in data3d.where( data3d.cols.frame == frame ):
        #nrow = row.nrow()
        nrow = row.nrow
        p2, p4, p5 = row['p2'], row['p4'], row['p5']
    if nrow is None:
        raise ValueError('could not find frame')
    nrow = int(nrow) # hmm weird pytables bug?
    data3d.cols.p2[nrow] = -p2
    data3d.cols.p4[nrow] = -p4
    data3d.cols.p5[nrow] = -p5

def my_normalize(V):
    v = nx.asarray(V)
    assert len(v.shape)==1
    u = v/ math.sqrt( nx.sum( v**2) )
    return u

def sort_on_col0( a, b ):
    a0 = a[0]
    b0 = b[0]
    if a0 < b0: return -1
    elif a0 > b0: return 1
    else: return 0

def auto_flip_line_direction(results,start_frame,stop_frame,typ='best',
                             skip_allowance = 5,                             
                             ):
    if typ=='best':
        data3d = results.root.data3d_best
    elif typ=='fastest':
        data3d = results.root.data3d_fastest

    assert stop_frame-start_frame > 1
    
    frame_and_dir_list = [ (row['frame'], -row['p2'],row['p4'],-row['p5']) for row in data3d.where( start_frame <= data3d.cols.frame <= stop_frame ) ]
    frame_and_dir_list.sort(sort_on_col0)
    frame_and_dir_list = nx.array( frame_and_dir_list )
    bad_idx=list(getnan(frame_and_dir_list[:,1])[0])
    good_idx = [i for i in range(len(frame_and_dir_list[:,1])) if i not in bad_idx]
    frame_and_dir_list = [ frame_and_dir_list[i] for i in good_idx]
    
    prev_frame = frame_and_dir_list[0][0]
    prev_dir = my_normalize(frame_and_dir_list[0][1:4])
    
    cos_90 = math.cos(math.pi/4)
    frames_flipped = []
    
    for frame_and_dir in frame_and_dir_list[1:]:
        this_frame = frame_and_dir[0]
        this_dir = my_normalize(frame_and_dir[1:4])

        try:
            cos_theta = nx.dot(this_dir, prev_dir)
        except Exception,x:
            print 'exception this_dir prev_dir',this_dir,prev_dir
            raise
        except:
            print 'hmm'
            raise
        theta_deg = math.acos(cos_theta)/math.pi*180
        
        if this_frame in [188305, 188306, 188307]:
            print '*'*10,theta_deg
        
#        print this_frame, this_dir, cos_theta, math.acos(cos_theta)/math.pi*180
#        print
        
        dt_frames = this_frame - prev_frame
        
        prev_frame = this_frame
        prev_dir = this_dir

        
        if dt_frames > skip_allowance:
            print 'frame %d skipped because previous %d frames not found'%(this_frame,skip_allowance)
            continue
        if theta_deg > 90:
            flip_line_direction(results,this_frame,typ=typ)
            prev_dir = -prev_dir
            frames_flipped.append(this_frame)
    return frames_flipped

def auto_flip_line_direction_hang(results,start_frame,stop_frame,typ='best',
                              ):
    if typ=='best':
        data3d = results.root.data3d_best
    elif typ=='fastest':
        data3d = results.root.data3d_fastest

    assert stop_frame-start_frame > 1
    
    frame_and_dir_list = [ (row['frame'], -row['p2'],row['p4'],-row['p5']) for row in data3d.where( start_frame <= data3d.cols.frame <= stop_frame ) ]
    frame_and_dir_list.sort(sort_on_col0)
    frame_and_dir_list = nx.array( frame_and_dir_list )
    bad_idx=list(getnan(frame_and_dir_list[:,1])[0])
    good_idx = [i for i in range(len(frame_and_dir_list[:,1])) if i not in bad_idx]
    frame_and_dir_list = [ frame_and_dir_list[i] for i in good_idx]
    
    frames_flipped = []
    
    for frame_and_dir in frame_and_dir_list:
        this_frame = int(frame_and_dir[0])
        this_dir = my_normalize(frame_and_dir[1:4])
        if this_dir[2] < -0.1:
            flip_line_direction(results,this_frame,typ=typ)
            frames_flipped.append(this_frame)
    return frames_flipped

def time2frame(results,time_double,typ='best'):
    assert isinstance(time_double,float)

    if typ=='best':
        table = results.root.data3d_best
    elif typ=='fastest':
        table = results.root.data3d_fastest

    status('copying column to Python')
    find3d_time = nx.array( table.cols.find3d_time )
    status('searching for closest time')
    idx = nx.argmin(nx.abs(find3d_time-time_double))
    status('found index %d'%idx)
    return table.cols.frame[idx]

def from_table_by_frame(table,frame,colnames=None):
    if colnames is None:
        colnames='x','y'

    def values_for_keys(dikt,keys):
        return [dikt[key] for key in keys]
    
    rows = [values_for_keys(x,colnames) for x in table if x['frame']==frame]
    #rows = [values_for_keys(x,colnames) for x in table.where( table.cols.frame==frame)]
    return rows

def get_pmat(results,cam_id):
    return nx.asarray(results.root.calibration.pmat.__getattr__(cam_id))

def get_intlin(results,cam_id):
    return nx.asarray(results.root.calibration.intrinsic_linear.__getattr__(cam_id))

def get_intnonlin(results,cam_id):
    return nx.asarray(results.root.calibration.intrinsic_nonlinear.__getattr__(cam_id))

def get_frames_with_3d(results):
    table = results.root.data3d_best
    return [x['frame'] for x in table]

def redo_3d_calc(results,frame,reconstructor=None,verify=True,overwrite=False):
    """redo 3D computations for a single frame

    See also recompute_3d_from_2d()

    last used a long time ago... probably doesn't work

    There's a new function, find_best_3d. This one doesn't use it.

    """
    
    import flydra.reconstruct
    
    data3d = results.root.data3d_best
    data2d = results.root.data2d
    cam_info = results.root.cam_info
    
    Xorig, camns_used, nrow = [ ((x['x'],x['y'],x['z']),x['camns_used'],x.nrow())
##                                for x in data3d.where( data3d.cols.frame==frame )][0]
                                for x in data3d if x['frame']==frame ][0]
    nrowi=int(nrow)
    assert nrowi==nrow
    camns_used = map(int,camns_used.split())

    status('testing frame %d with cameras %s'%(frame,str(camns_used)))

    if reconstructor is None:
        reconstructor = flydra.reconstruct.Reconstructor(results)

    cam_ids_and_points2d = []
    for camn in camns_used:
        cam_id = [row['cam_id'] for row in cam_info if row['camn']==camn][0]
        value_tuple_list = [(row['x'],row['y'],row['area'],
                             row['slope'],row['eccentricity'],
                             row['p1'],row['p2'],row['p3'],row['p4'])
                            for row in data2d if row['frame']==frame and row['camn']==camn]
        if len(value_tuple_list) == 0:
            raise RuntimeError('no 2D data for camn %d frame %d'%(camn,frame))
        assert len(value_tuple_list) == 1
        value_tuple = value_tuple_list[0]
##        value_tuple = [(row['x'],row['y'],row['area'],
##                        row['slope'],row['eccentricity'],
##                        row['p1'],row['p2'],row['p3'],row['p4'])
##                       ##                       for row in data2d.where( data2d.cols.frame==frame )
####                       if row['camn']==camn][0]
##                       for row in data2d if row['frame']==frame and row['camn']==camn][0]
        x,y,area,slope,eccentricity,p1,p2,p3,p4 = value_tuple
        cam_ids_and_points2d.append( (cam_id, value_tuple) )
        
    Xnew, Lcoords = reconstructor.find3d(cam_ids_and_points2d)
    if verify:
        assert nx.allclose(Xnew,Xorig)
    if overwrite:
        raise NotImplementedError("not done yet")

def get_3d_frame_range_with_2d_info(results):
    data3d = results.root.data3d_best
    data2d = results.root.data2d

    frames_3d = [ x['frame'] for x in data3d ]
    frames_3d.sort()
    frame_min = frames_3d[0]
    frame_max = frames_3d[-1]
    return frame_min, frame_max

def summarize(results):
    res = StringIO.StringIO()

    camn2cam_id, cam_id2camns = get_caminfo_dicts(results)
##    # camera info
##    cam_info = results.root.cam_info
##    cam_id2camns = {}
##    camn2cam_id = {}
    
##    for row in cam_info:
##        cam_id, camn = row['cam_id'], row['camn']
##        cam_id2camns.setdefault(cam_id,[]).append(camn)
##        camn2cam_id[camn]=cam_id

    # 2d data
    data2d = results.root.data2d

    n_2d_rows = {}
    for camn in camn2cam_id.keys():
##        n_2d_rows[camn] = len( [ x for x in data2d.where( data2d.cols.camn == camn ) ])
        n_2d_rows[camn] = len( [ x for x in data2d if x['camn'] == camn ])

    #print >> res, 'camn cam_id n_2d_rows'
    for camn in camn2cam_id.keys():
        print >> res, "camn %d ('%s') %d frames of 2D info"%(camn, camn2cam_id[camn], n_2d_rows[camn])

    print >> res

    # 3d data
    data3d = results.root.data3d_best
    print >> res, len(data3d),'frames of 3d information'
    frames = list(data3d.cols.frame)
    frames.sort()
    print >> res, '  (start: %d, stop: %d)'%(frames[0],frames[-1])
    

    frame2camns_used = {}
    for row in data3d:
        camns_used = map(int,row['camns_used'].split())
        if len(getnan(row['p0'])[0]):
            orient_info = False
        else:
            orient_info = True
        frame2camns_used[row['frame']]=camns_used, orient_info
    
    nframes_by_n_camns_used = {}
    nframes2_by_n_camns_used = {} # with orient_info
    for f, (camns_used, orient_info) in frame2camns_used.iteritems():
        nframes_by_n_camns_used[len(camns_used)]=nframes_by_n_camns_used.get(len(camns_used),0) + 1
        if orient_info:
            nframes2_by_n_camns_used[len(camns_used)]=nframes2_by_n_camns_used.get(len(camns_used),0) + 1
        else:
            nframes2_by_n_camns_used.setdefault(len(camns_used),0)
        
        #orig_value = nframes_by_n_camns_used.setdefault( len(camns_used), 0).add(1)
        
    for n_camns_used,n_frames in nframes_by_n_camns_used.iteritems():
        print >> res, ' with %d camns: %d frames (%d with orientation)'%(n_camns_used, n_frames, nframes2_by_n_camns_used[n_camns_used] )
    
    res.seek(0)
    return res.read()

def plot_movie_2d(results,
                  start_idx=0,
                  stop_idx=-1,
                  fstart=None,
                  fstop=None,
                  typ='best',
                  show_err=False,
                  max_err=10,
                  onefigure=True,
                  show_cam_usage=False):
    """

    last used 2006-05-08
    """
    if not hasattr(results.root, 'cam_by_cam_2d'):
        make_new_fmt(results)
    ioff()
    try:
        import flydra.reconstruct
        reconstructor = flydra.reconstruct.Reconstructor(results)
        
        data3d = results.root.data3d_best
        
        # get 3D data
        f,X,L,err = get_f_xyz_L_err(results,max_err=max_err,typ=typ)
        if fstart is not None:
            fstart = max(f[start_idx],fstart)
            if fstart != f[start_idx]:
                start_idx = numarray.nonzero(f>=fstart)[0][0]
        else:
            fstart = f[start_idx]
        if fstop is not None:
            fstop = min(f[stop_idx],fstop)
            if fstop != f[stop_idx]:
                stop_idx = numarray.nonzero(f<=fstop)[0][-1]
        else:
            fstop = f[stop_idx]
        f=f[start_idx:stop_idx]
        X=X[start_idx:stop_idx]
        X = nx.concatenate( (X, nx.ones( (X.shape[0],1) )), axis=1 )
        X.transpose()

        # get camera information
        camns = [ row['camn'] for row in results.root.cam_info]
        camn2cam_id = {}
        cam_id2camn = {}
        cam_ids_unique = True
        for row in results.root.cam_info:
            cam_id = row['cam_id']
            camn = row['camn']
            camn2cam_id[ camn] = cam_id
            if cam_id in cam_id2camn:
                cam_ids_unique = False
            cam_id2camn[ cam_id ] = camn
        cam_ids = [ camn2cam_id[camn] for camn in camns ]
        cam_ids.sort()
        if onefigure:
            # plots from the bottom up
            cam_ids.reverse()
        if cam_ids_unique:
            # can order by cam_id
            camns = [ cam_id2camn[cam_id] for cam_id in cam_ids ]

        if onefigure:
            ncams = len(camns)
            height = 0.8/ncams
        ax = None
        useful_axes = {}
        for i, camn in enumerate(camns):
            cam_id = camn2cam_id[camn]

            # project 3D data through camera calibration to get 2D reprojection
            xy=reconstructor.find2d(cam_id,X)

            # get original 2D points
            f2 = []
            x2 = []
            y2 = []
            p1 = []
            camn_used = []

            table_name = 'camn'+str(camn)
            table = getattr(results.root.cam_by_cam_2d,table_name)
            if PT.__version__ <= '1.3.2':
                fstart = int(fstart)
                fstop = int(fstop)
            for row in table.where( fstart <= table.cols.frame <= fstop ):
                frame = row['frame']
                f2.append(frame)
                x2.append(row['x'])
                y2.append(row['y'])
                p1.append(row['p1'])
                if show_cam_usage:
                    cu = nan
                    for row3d in data3d.where( frame == data3d.cols.frame ):
                        if str(camn) in row3d['camns_used']:
                            cu = 1
                    camn_used.append( cu )
                    
            f2 = nx.array(f2)
            x2 = nx.array(x2)
            y2 = nx.array(y2)
            p1 = nx.array(p1)
            p1[~nx.isnan(p1)]=1 # nans and 1s
            if show_cam_usage:
                camn_used = nx.array(camn_used)

            if len(f2)!=0:
                if onefigure:
                    ax = pylab.axes([0.1, height*i+0.05,  0.8, height],sharex=ax)
                    useful_axes[cam_id] = ax
                    ax.xaxis.set_major_formatter(matplotlib.ticker.OldScalarFormatter())
                    xline3d,yline3d = ax.plot( f,  xy[0,:],'rx', # projected from 3D reconstruction
                                               f,  xy[1,:],'gx',
                                               ms=6.0,
                                               )

                    xline2d,yline2d = ax.plot( f2, x2,'ko', # real data
                                               f2, y2,'bo', mfc='None', ms=2.0 )
                    pylab.setp(xline2d,'markeredgecolor',(0.5,0.0,0.0))#'red')
                    pylab.setp(yline2d,'markeredgecolor',(0.0,0.3,0.0))#'green')

                    oriline, = ax.plot( f2, p1,'ko') # orientation data present
                    if show_cam_usage:
                        camusedline, = ax.plot( f2, 700*camn_used,'yo') # orientation data present
                        pylab.setp(camusedline,'markeredgecolor',(0.0,0.3,0.0,0.0))
                        pylab.setp(camusedline,'markeredgewidth',0)

                    pylab.ylabel( cam_id )
                    pylab.setp(ax, 'ylim',[-10,710])
                else:
                    pylab.figure() # new figure
                    lines = pylab.plot( x2, y2, 'ko',
                                        xy[0,:], xy[1,:], 'rx' ) # projected from 3D reconstruction
                    pylab.legend(lines,['original 2D data','reprojections'])
                    pylab.xlabel( 'X (pixels)' )
                    pylab.ylabel( 'Y (pixels)' )
                    pylab.title( cam_id )
                    pylab.setp(pylab.gca(), 'xlim',[-10,660])
                    pylab.setp(pylab.gca(), 'ylim',[-10,500])
        if onefigure:
            useful_cam_ids = useful_axes.keys()
            useful_cam_ids.sort()
            useful_cam_ids.reverse()
            height = 0.9/len(useful_cam_ids)
            fbycid = {}
            if hasattr(results.root,'exact_movie_info'):
                for row in results.root.exact_movie_info:
                    cam_id = row['cam_id']
                    start_frame = row['start_frame']
                    stop_frame = row['stop_frame']
                    fbycid.setdefault(cam_id,[]).append( (start_frame,stop_frame))
            for i, cam_id in enumerate(useful_cam_ids):
                ax=useful_axes[cam_id]
                ax.set_position([0.1, height*i+0.05,  0.8, height])
                if cam_id in fbycid:
                    for start_frame,stop_frame in fbycid[cam_id]:
                        ax.axvspan( start_frame, stop_frame, fc=0.5, alpha=0.5 )
    finally:
        ion()

def plot_whole_movie_3d(results, typ='best', show_err=False, max_err=10,
                        fstart=None, fstop=None):
    import flydra.reconstruct
    ioff()
    
    if typ == 'fast':
        data3d = results.root.data3d_fast
    elif typ == 'best':
        data3d = results.root.data3d_best

    f,xyz,L,err = get_f_xyz_L_err(results,max_err=max_err,typ=typ)

    if fstart is not None:
        idx = nx.where( f >= fstart )[0]
        f = nx.take(f,idx,axis=0)
        xyz = nx.take(xyz,idx,axis=0)
        L = nx.take(L,idx,axis=0)
        err = nx.take(err,idx,axis=0)
    if fstop is not None:
        idx = nx.where( f <= fstop )[0]
        f = nx.take(f,idx,axis=0)
        xyz = nx.take(xyz,idxx,axis=0)
        L = nx.take(L,idxx,axis=0)
        err = nx.take(err,idxx,axis=0)

    x = xyz[:,0]
    y = xyz[:,1]
    z = xyz[:,2]

    clf()
    yinc = .65/3
    ax_x = pylab.axes([0.1,  0.25+2*yinc,  0.8, yinc])
    ax_y = pylab.axes([0.1,  0.25+yinc,  0.8, yinc],sharex=ax_x)
    ax_z = pylab.axes([0.1,  0.25,  0.8, yinc],sharex=ax_x)
    ax_xlen = pylab.axes([0.1, 0.15,  0.8, 0.1],sharex=ax_x)
    ax_err = pylab.axes([0.1, 0.05,  0.8, 0.1],sharex=ax_x)
    
    # plot it!
    xl=ax_x.plot(f,x,'r.')
    ax_x.axhline(y=730)
    ax_x.set_ylim([0,1500])
    yl=ax_y.plot(f,y,'g.')
    ax_y.axhline(y=152.5)
    ax_y.set_ylim([0,450])
    zl=ax_z.plot(f,z,'b.')
    ax_y.set_ylabel('position (mm)')
    ax_z.set_ylim([-100,400])
##    if show_err:
##        ax.plot(f,err,'k.')
##    setp(ax,'ylim',[-10,600])

    U = flydra.reconstruct.line_direction(L)
#    ax_xlen.plot( f, U[:,0], 'r.')
    ax_xlen.plot( f, nx.arctan2(U[:,1],U[:,0]), 'r.')
    ax_xlen.set_ylabel('angle')
    ax_xlen.set_ylim([-4,4])
        
    ax_err.plot(f,err,'k.')
    ax_err.set_xlabel('frame no.')
    ax_err.xaxis.set_major_formatter(matplotlib.ticker.OldScalarFormatter())
    ax_err.set_ylabel('err\n(pixels)')
    pylab.setp(ax_err,'ylim',[0,10])
    ##ax.title(typ+' data')
    ##ax.xlabel('frame number')

    if hasattr(results.root,'exact_movie_info'):
        fbycid = {}
        for row in results.root.exact_movie_info:
            cam_id = row['cam_id']
            start_frame = row['start_frame']
            stop_frame = row['stop_frame']
            fbycid.setdefault(cam_id,[]).append( (start_frame,stop_frame))

        cam_ids = fbycid.keys()
        cam_ids.sort()
        yticks = []
        yticklabels = []
        for y, cam_id in enumerate( cam_ids ):
            pairs = fbycid[cam_id]
            for start, stop in pairs:
                plot( [start, stop], [y, y] )
            yticks.append( y )
            yticklabels.append( cam_id )
        setp( gca(), 'yticks', yticks )
        setp( gca(), 'yticklabels', yticklabels )
        if len(yticks):
            setp( gca(), 'ylim', [-1, max(yticks)+1])

    ion()

def plot_whole_range(results, start_frame, stop_frame, typ='best', show_err=False):
    ioff()
    if typ == 'fast':
        data3d = results.root.data3d_fast
    elif typ == 'best':
        data3d = results.root.data3d_best

    f=[]
    x=[]
    y=[]
    z=[]
    for row in data3d:
        if start_frame<=row['frame']<=stop_frame:
            f.append(row['frame'])
            x.append(row['x'])
            y.append(row['y'])
            z.append(row['z'])
    f = nx.array(f)
    x = nx.array(x)
    y = nx.array(y)
    z = nx.array(z)
    if show_err:
        err = nx.array(data3d.cols.mean_dist)
    
    # plot it!
    ax = pylab.axes()
    ax.plot(f,x,'r.')
    ax.plot(f,y,'g.')
    ax.plot(f,z,'b.')
    if show_err:
        ax.plot(f,err,'k.')
    ##ax.title(typ+' data')
    ##ax.xlabel('frame number')
    ion()

def save_smooth_data(results,frames,Psmooth,Qsmooth,table_name='smooth_data'):
    assert len(frames)==len(Psmooth)==len(Qsmooth)
    if not hasattr(results.root,table_name):
        smooth_data = results.createTable(results.root,table_name,SmoothData,'')
    else:
        smooth_data = results.root.smooth_data
        
    for i in range(len(frames)):        
        frame = frames[i]
        P = Psmooth[i]
        Q = Qsmooth[i]

        old_nrow = None
        for row in smooth_data.where(smooth_data.cols.frame==frame):
            print row.nrow
            if old_nrow is not None:
                raise RuntimeError('more than row with frame number %d in smooth_data'%frame)
            old_nrow = row.nrow
            old_nrow = int(old_nrow) # XXX pytables bug ?
        
        new_row = []
        new_row_dict = {}
        for colname in smooth_data.colnames:
            if colname == 'frame': new_row.append( frame )
            elif colname == 'x': new_row.append( P[0] )
            elif colname == 'y': new_row.append( P[1] )
            elif colname == 'z': new_row.append( P[2] )
            elif colname == 'qw': new_row.append( Q.w )
            elif colname == 'qx': new_row.append( Q.x )
            elif colname == 'qy': new_row.append( Q.y )
            elif colname == 'qz': new_row.append( Q.z )
            else: raise KeyError("don't know column name '%s'"%colname)
            new_row_dict[colname] = new_row[-1]
        if old_nrow is None:
            for k,v in new_row_dict.iteritems():
                if k=='frame':
                    v = int(v)
                else:
                    v = float(v)
                smooth_data.row[k] = v
            smooth_data.row.append()
        else:
            smooth_data[old_nrow] = new_row
    smooth_data.flush()

def save_resultant(results,frames,resultant):
    return save_timed_forces('resultant_forces',results,frames,resultant)

def save_timed_forces(table_name,results,frames,resultant):
    assert len(frames)==len(resultant)
    if not hasattr(results.root,table_name):
        resultant_forces = results.createTable(results.root,table_name,TimedVectors,'')
    else:
        resultant_forces = results.root.resultant_forces
        
    for i in range(len(frames)):        
        frame = frames[i]
        fxyz = resultant[i]

        old_nrow = None
        for row in resultant_forces:
            if row['frame'] != frame:
                    continue
            if old_nrow is not None:
                raise RuntimeError('more than row with frame number %d in smooth_data'%frame)
            old_nrow = row.nrow()
        
        new_row = []
        new_row_dict = {}
        for colname in resultant_forces.colnames:
            if colname == 'frame': new_row.append( frame )
            elif colname == 'fx': new_row.append( fxyz[0] )
            elif colname == 'fy': new_row.append( fxyz[1] )
            elif colname == 'fz': new_row.append( fxyz[2] )
            else: raise KeyError("don't know column name '%s'"%colname)
            new_row_dict[colname] = new_row[-1]
        if old_nrow is None:
            for k,v in new_row_dict.iteritems():
                resultant_forces.row[k] = v
            resultant_forces.row.append()
        else:
            resultant_forces[old_nrow] = new_row
    resultant_forces.flush()

def set_ignore_frames(results,start_frame,stop_frame):
    if not hasattr(results.root,'ignore_frames'):
        ignore_frames = results.createTable(results.root,'ignore_frames',IgnoreFrames,'')
    else:
        ignore_frames = results.root.ignore_frames

    ignore_frames.row['start_frame']=start_frame
    ignore_frames.row['stop_frame']=stop_frame
    ignore_frames.row.append()
    ignore_frames.flush()

def update_exact_roi_movie_info(results,cam_id,roi_movie_basename):
    """indexes .fmf/.smd files by timestamp and saves to HDF5 file

    WARNING: This is pretty heavy-handed for giant files. On these it
    would be better to use update_small_fmf_summary().
    
    last used extensively 2006-05-16
    """
    status('making exact ROI movie info for %s'%cam_id)
    
    data2d = results.root.data2d

    if hasattr(results.root,'exact_roi_movie_info'):
        exact_roi_movie_info = results.root.exact_roi_movie_info
    else:
        exact_roi_movie_info = results.createTable(results.root,'exact_roi_movie_info',ExactROIFrameMovieInfo,'')

    frame_col = exact_roi_movie_info.cols.frame
    if frame_col.index is None:
        print 'creating index on exact_roi_movie_info.cols.frame ...'
        frame_col.createIndex()
        print 'done'

    timestamp_col = exact_roi_movie_info.cols.timestamp
    if timestamp_col.index is None:
        print 'creating index on exact_roi_movie_info.cols.timestamp ...'
        timestamp_col.createIndex()
        print 'done'

    if 0:
        camn2cam_id, cam_id2camns = get_caminfo_dicts(results)
        possible_camns = cam_id2camns[cam_id]
        ssdict = {}
        for possible_camn in possible_camns:
            print 'creating timestamp->frame mapping for camn %d, cam_id %s'%(possible_camn,cam_id)
            data2d = results.root.data2d
            ss = []
            print ' getting coordinates'
            coords = data2d.getWhereList(data2d.cols.camn == possible_camn)
            print 'type(coords)',type(coords)
            print 'coords.shape',coords.shape
            sys.exit(0)
            print ' reading timestamps'
            timestamps = data2d.readCoordinates(coords, 'timestamp' )
            print ' reading frames'
            frames = data2d.readCoordinates(coords, 'frame' )
            print ' done'
            timestamps = nx.asarray(timestamps)
            frames = nx.asarray(frames)
            if 1: # check if sorted
                tdiff = timestamps[1:]-timestamps[-1]
                if nx.amin(tdiff) < 0:
                    raise NotImplementedError('support for non-monotonically increasing timestamps not yet implemented')
            ssdict[possible_camn] = (timestamps,frames)

    fmf_filename = roi_movie_basename + '.fmf'
    smd_filename = roi_movie_basename + '.smd'

    fmf = FlyMovieFormat.FlyMovie(fmf_filename,check_integrity=True)
    # XXX should update to use smdfile.SMDFile
    smd_fd = open(smd_filename,mode='r')
    smd = smd_fd.read()
    smd_fd.close()
    small_datafile_fmt = '<dII'
    row_sz = struct.calcsize(small_datafile_fmt)
    all_timestamps = fmf.get_all_timestamps()
    idx = 0
    n_frames = len(all_timestamps)
    try:
        for fmf_frame, timestamp in enumerate(all_timestamps):
            if fmf_frame%100==0:
                print '  frame % 9d of % 9d'%(fmf_frame,n_frames)
            buf = smd[idx:idx+row_sz]
            idx += row_sz
            cmp_ts, left, bottom = struct.unpack(small_datafile_fmt,buf)
            if timestamp != cmp_ts:
                raise RuntimeError("timestamps not equal in .fmf and .smd files")

            if 1:
                try:
                    camn,frame = get_camn_and_frame(results,cam_id,timestamp)
                except ValueError:
                    #no data found
                    continue
            else:
                camn = get_camn(results,cam_id,timestamp=timestamp)
                (all_timestamps, all_frames) = ssdict[camn]
                frame_index = all_timestamps.searchsorted( [timestamp] )[0]
                frame = all_frames[frame_index]

            skip_im = False
            for row in exact_roi_movie_info.where( exact_roi_movie_info.cols.frame==frame ):
                if row['camn']==camn:
                    #raise RuntimeError('data already in table for cam_id %s, timestamp %s, camn %d, frame %d'%(cam_id,str(timestamp),camn,frame))
                    print 'WARNING: data already in table for cam_id %s, timestamp %s, camn %d, frame %d'%(cam_id,str(timestamp),camn,frame)
                    skip_im = True
                    break
            if skip_im:
                continue # skip this image
            newrow = exact_roi_movie_info.row
            newrow['cam_id'] = cam_id
            newrow['camn'] = camn
            newrow['filename'] = fmf_filename
            newrow['frame'] = frame
            newrow['fmf_frame'] = fmf_frame
            newrow['timestamp'] = timestamp
            newrow['left'] = left
            newrow['bottom'] = bottom
            newrow.append()
    finally:
        exact_roi_movie_info.flush()
    print 'done'
    
_caching_movie_opener = caching_movie_opener.CachingMovieOpener()
get_movie_frame2 = _caching_movie_opener.get_movie_frame
get_background_image = _caching_movie_opener.get_background_image
get_undistorted_background_image = _caching_movie_opener.get_undistorted_background_image

def get_movie_frame2_orig(results, cam_timestamp_or_frame, cam, suffix=None):
    # OLD, delete me
    if suffix is None:
        suffix = ''
        
    if isinstance(cam_timestamp_or_frame,float):
        cam_timestamp = cam_timestamp_or_frame
    else:
        frame = cam_timestamp_or_frame
        camn, cam_timestamp = get_camn_and_remote_timestamp(results,cam,frame)
    
    cam_info = results.root.cam_info
    movie_info = results.root.movie_info
    data2d = results.root.data2d
    data3d = results.root.data3d_best

    if isinstance(cam,int): # camn
        camn = cam
        cam_id = [x['cam_id'] for x in cam_info.where( cam_info.cols.camn == camn) ][0]
        cam_id = [x['cam_id'] for x in cam_info if x['camn'] == camn ][0]
    elif isinstance(cam,str): # cam_id
        cam_id = cam
        
    if not hasattr(results.root,'exact_movie_info'):
        make_exact_movie_info2(results)
    exact_movie_info = results.root.exact_movie_info

    # find normal (non background movie filename)
    found = False
    for row in exact_movie_info:
        if row['cam_id'] == cam_id:
            if row['start_timestamp'] < cam_timestamp < row['stop_timestamp']:
                filename = row['filename']
                found = True
                break
    if not found:
        raise ValueError('movie not found for %s'%(cam_id,))

    filename = os.path.splitext(filename)[0] + '%s.fmf'%(suffix,) # alter to be background image

    fly_movie = FlyMovieFormat.FlyMovie(filename)
    frame, movie_timestamp = fly_movie.get_frame_at_or_before_timestamp( cam_timestamp )
    return frame, movie_timestamp

def recompute_3d_from_2d(results,
                         overwrite=False,
                         hypothesis_test=True, # discards camns_used
                         typ='best',
                         ):
    """
    recompute 3D from 2D over many frames

    See also redo_3d_calc() for single frames.
    
    Last used 2006-07-23.
    """
    import flydra.reconstruct
    import flydra.reconstruct_utils as ru
    import flydra.MainBrain as mb
    
    # allow rapid building of numarray.records.RecArray:
    Info3DColNames = PT.Description(mb.Info3D().columns)._v_names
    Info3DColFormats = PT.Description(mb.Info3D().columns)._v_nestedFormats
    reconstructor = flydra.reconstruct.Reconstructor(results)

    data2d = results.root.data2d_distorted
    cam_info = results.root.cam_info

    camn2cam_id = {}
    for row in cam_info:
        cam_id, camn = row['cam_id'], row['camn']
        camn2cam_id[camn]=cam_id
        
    max_n_cameras = len(reconstructor.cam_combinations[0])
    
    if not hypothesis_test:
        if typ == 'fast':
            data3d = results.root.data3d_fast
        elif typ == 'best':
            data3d = results.root.data3d_best

        print len(data3d),'rows to be processed'
        stride_len = 100
        count = 0
        for stride_start in range(0,len(data3d),stride_len):
            seq = (stride_start,min(stride_start+stride_len,len(data3d)),1)
            new_rows = []
            for row_idx in range(seq[0],seq[1],seq[2]):
                row = data3d[row_idx]
                frame_no = row['frame']
                if count%10==0:
                    print 'processing row %d (frame %d)'%(count,frame_no)
                count += 1
                camns_used = map(int,row['camns_used'].split())
                if not len(camns_used):
                    raise ValueError('no camns used frame %d'%frame_no)
                    print 'continuing'
                    continue
                #nrow = row_idx#row.nrow
                d2 = {}
                did_camns = []
                for x in data2d.where( data2d.cols.frame == frame_no ): # this call take most time
                    camn = x['camn']
                    if camn in camns_used:
                        if camn in did_camns:
                            continue # only use first found point
                        cam_id = camn2cam_id[camn]
                        d2[cam_id] = (x['x'], x['y'], x['area'], x['slope'],
                                      x['eccentricity'], x['p1'], x['p2'],
                                      x['p3'], x['p4'])
                        did_camns.append( camn )

                X, line3d = reconstructor.find3d(d2.items())
                if overwrite:
                    new_row = []
                    for colname in data3d.colnames:
                        if colname == 'x': value = X[0]
                        elif colname == 'y': value = X[1]
                        elif colname == 'z': value = X[2]
                        elif colname[0]=='p' and colname[1:] in ['0','1','2','3','4','5']:
                            if line3d is not None:
                                if   colname == 'p0': value = line3d[0]
                                elif colname == 'p1': value = line3d[1]
                                elif colname == 'p2': value = line3d[2]
                                elif colname == 'p3': value = line3d[3]
                                elif colname == 'p4': value = line3d[4]
                                elif colname == 'p5': value = line3d[5]
                            else:
                                value = nan
                        else: value = row[colname]
                        new_row.append( value )
                    new_rows.append( new_row )
            if overwrite:
                recarray = numarray.records.array(
                    buffer=new_rows,
                    formats=Info3DColFormats,
                    names=Info3DColNames)
                data3d.modifyRows(start=seq[0],
                                  stop=seq[1],
                                  step=seq[2],
                                  rows=recarray)
                data3d.flush()
    else: # do hypothesis testing
        if not overwrite:
            raise RuntimeError('hypothesis testing requires overwriting')

        # remove any old data3d structures
        if hasattr(results.root,'data3d_best'):
            print 'deleting old data3d_best'
            results.removeNode( results.root.data3d_best )
        if hasattr(results.root,'data3d_fast'):
            print 'deleting old data3d_fast'
            results.removeNode( results.root.data3d_fast )

        ############################################
        def do_3d_stuff(do_d2,
                        do_frame_no,
                        do_idxs_in_comp,
                        do_cam_id2camn,
                        list_of_row_tup3d,
                        absolutely_save=False
                        ):
            n_saved = 0            
            if 0:
                print 'computing 3d for frame %d'%do_frame_no
                print 'do_idxs_in_comp',do_idxs_in_comp
                print 'do_d2',do_d2
                print
            if len(do_d2)>=2:

                (X, line3d, cam_ids_used,
                 mean_dist) = ru.find_best_3d(reconstructor,do_d2)

                camns_used = [ do_cam_id2camn[cam_id] for cam_id in cam_ids_used ]
                camns_used_str = ' '.join(map(str,camns_used))
                if line3d is None:
                    line3d = (numpy.nan,numpy.nan,numpy.nan,
                              numpy.nan,numpy.nan,numpy.nan)
                if 0:
                    print 'repr(camns_used)',repr(camns_used)
                    print 'camns_used_str',camns_used_str
                    print 'X',X
                    print 'line3d',line3d
                    print 'mean_dist',mean_dist
                row_tup3d = (do_frame_no,X[0],X[1],X[2],
                             line3d[0],line3d[1],line3d[2],
                             line3d[3],line3d[4],line3d[5],
                             time.time(),
                             camns_used_str, mean_dist)
                    
                list_of_row_tup3d.append( row_tup3d )
                # save to disk every 100 rows
                if ((absolutely_save and len(list_of_row_tup3d)) or
                    len(list_of_row_tup3d) >= 100):
                    n_saved = len(list_of_row_tup3d)
                    recarray = numarray.records.array(
                        buffer=list_of_row_tup3d,
                        formats=Info3DColFormats,
                        names=Info3DColNames)
                    data3d.append(recarray)
                    data3d.flush()
                    del list_of_row_tup3d[:]
            return n_saved
        ############################################

            
        # create new data3d
        data3d = results.createTable(results.root,
                                     'data3d_best', mb.Info3D,
                                     "3d data (best)")
        print 'loading all frame numbers from 2d data'
        all_frames = numpy.asarray(data2d.col('frame'))
        print 'sorting frame numbers'
        stably_sorted_idxs = all_frames.argsort(kind='mergesort') # sort stably
        print 'chunking and processing data'
        n_2d_rows = len(all_frames)
        approx_chunksize = 10000
        next_chunk_start = 0
        total_n_saved = 0
        while 1:
            # ----- indexing calculations
            chunk_start = next_chunk_start
            if chunk_start>=n_2d_rows:
                break
            #        chunk_stop is index x[y] (not slice x[:y])
            chunk_stop = chunk_start+approx_chunksize 
            chunk_stop = min(chunk_stop,n_2d_rows-1) # make sure we're not off end
            next_chunk_stop = chunk_stop+1
            chunk_stop_idx = stably_sorted_idxs[chunk_stop]
            stop_frame = all_frames[chunk_stop_idx]
            # ----- find contiguous chunk of frames
            while 1:
                if next_chunk_stop>=n_2d_rows:
                    break # at end of list
                
                # make sure to get all rows with same frame number
                next_chunk_stop_idx = stably_sorted_idxs[next_chunk_stop]
                test_stop_frame = all_frames[next_chunk_stop_idx]
                if test_stop_frame != stop_frame:
                    break # next frame number is different, stop here

                chunk_stop = next_chunk_stop
                chunk_stop_idx = next_chunk_stop_idx
                next_chunk_stop = chunk_stop+1
            next_chunk_start = chunk_stop+1

            if 0:
                print
                print "chunk_start",  chunk_start,  stably_sorted_idxs[chunk_start],  all_frames[stably_sorted_idxs[chunk_start]]
                print "chunk_stop-1", chunk_stop-1, stably_sorted_idxs[chunk_stop-1], all_frames[stably_sorted_idxs[chunk_stop-1]]
                print "chunk_stop",   chunk_stop,   stably_sorted_idxs[chunk_stop], all_frames[stably_sorted_idxs[chunk_stop]]
                if chunk_stop+1 < n_2d_rows:
                    print "chunk_stop+1", chunk_stop+1, stably_sorted_idxs[chunk_stop+1], all_frames[stably_sorted_idxs[chunk_stop+1]]
                print "stop_frame",stop_frame
                print

            if 1:
                #print
                print 'chunk %d-%d (of %d total) approx frame %d'%(
                    chunk_start, chunk_stop,n_2d_rows,stop_frame)

            chunk_idxs=stably_sorted_idxs[chunk_start:chunk_stop+1]
            
            # get contiguous block of 2D table
            first_chunk_idx = chunk_idxs.min()
            last_chunk_idx  = chunk_idxs.max()
            if 0:
                print 'first_chunk_idx,last_chunk_idx+1',first_chunk_idx,last_chunk_idx+1
                print 'last_chunk_idx+1-first_chunk_idx',last_chunk_idx+1-first_chunk_idx
            chunk = data2d[first_chunk_idx:last_chunk_idx+1]
            chunk_relative_idxs = chunk_idxs-first_chunk_idx

            # intialize state variables for loop
            # chunks are sorted by frames, so we don't have to carry
            # over into next chunk
            last_frame_no = None
            d2 = {}
            cam_id2camn = {}
            did_camns = []
            list_of_row_tup3d = []
            idxs_in_comp = []

            for idx in chunk_relative_idxs:
                row = chunk[idx]
                
                frame_no = row['frame']
                if 0:
                    print
                    print 'idx',idx
                    print 'row',row
                    print 'frame_no',     type(frame_no),      frame_no
                    print 'last_frame_no',type(last_frame_no), last_frame_no
                if frame_no != last_frame_no:
                    # previous frame data collection over -- compute 3D
                    # clear data collection apparatus
                    
                    #print 'frame_no',frame_no,'(% 5d of %d)'%(count,n_rows)
                    do_d2 = d2 # save for 3D calc
                    do_frame_no = last_frame_no
                    do_idxs_in_comp = idxs_in_comp
                    do_cam_id2camn = cam_id2camn
                    
                    last_frame_no = frame_no
                    d2 = {}
                    cam_id2camn = {} # must be recomputed each frame
                    did_camns = []
                    idxs_in_comp = []

                    if do_frame_no is not None:
                        total_n_saved += do_3d_stuff(do_d2,
                                                     do_frame_no,
                                                     do_idxs_in_comp,
                                                     do_cam_id2camn,
                                                     list_of_row_tup3d)
                    
                # collect all data from data2d row for this frame
                # load all 2D data
                idxs_in_comp.append(int(idx))
                
                camn = row['camn']
                if camn in did_camns:
                    # only take 1st found point... XXX should fix (with Kalman?)
                    continue

                did_camns.append(camn)
                cam_id = camn2cam_id[camn]
                cam_id2camn[cam_id] = camn
                rx = row['x']
                if not numpy.isnan(rx):
                    ry = row['y']
                    rx,ry=reconstructor.undistort(cam_id,(rx,ry))
                    d2[cam_id] = (rx, ry,
                                  row['area'], row['slope'],
                                  row['eccentricity'],
                                  row['p1'], row['p2'],
                                  row['p3'], row['p4'])
                    
            # repeat 3d calcs on last idx in chunk:
            #print 'frame_no',frame_no,'(% 5d of %d)'%(count,n_rows)
            do_d2 = d2 # save for 3D calc
            do_frame_no = last_frame_no
            do_idxs_in_comp = idxs_in_comp
            do_cam_id2camn = cam_id2camn
            total_n_saved += do_3d_stuff(do_d2,
                                         do_frame_no,
                                         do_idxs_in_comp,
                                         do_cam_id2camn,
                                         list_of_row_tup3d,
                                         absolutely_save=True)
            print 'running total: %d 3d frames found'%(total_n_saved,)
        print     '        total: %d 3d frames found'%(total_n_saved,)
      
def switch_data2d_to_data2d_distorted(results):
    """

    This is only valid if the reconstructor has the original
    undistortion parameters. (In other words, this only produces valid
    data if the calibration data in the results file is that which was
    used to construct data2d.)
    
    """
    import flydra.MainBrain as MainBrain
    
    reconstructor = get_reconstructor(results)
    camn2cam_id, cam_id2camns = get_caminfo_dicts(results)

    data2d = results.root.data2d
    
    # remove any old data2d_distorted structures
    if hasattr(results.root,'data2d_distorted'):
        print 'deleting old data2d_distorted'
        results.removeNode( results.root.data2d_distorted )
        
    # create new data3d
    data2d_distorted = results.createTable(
        results.root,
        'data2d_distorted', MainBrain.Info2D,
        "2D data (re-distorted from undistorted points)")

    chunk_len = 1000
    for chunk_start in range(0,len(data2d),1000):
        chunk_stop = chunk_start+chunk_len
        chunk_stop = min(chunk_stop,len(data2d))
        print '%d (of %d)'%(chunk_start,len(data2d))

        nra = data2d[chunk_start:chunk_stop] # pytables' NestedRecArray
        chunk = nra.asRecArray() # numarray RecArray

        chunk_x = chunk.field('x')
        chunk_y = chunk.field('y')
        chunk_camn = chunk.field('camn')

        chunk_cam_id = [camn2cam_id[camn] for camn in chunk_camn]
        for i,(cam_id,x_undistorted,y_undistorted) in enumerate(
            zip(chunk_cam_id,chunk_x,chunk_y)):
            x_distorted, y_distorted = reconstructor.distort(cam_id,
                                                             (x_undistorted,
                                                              y_undistorted))
            chunk_x[i] = x_distorted
            chunk_y[i] = y_distorted
        data2d_distorted.append(chunk)
        data2d_distorted.flush()
        
    print 'should now delete results.root.data2d'

def plot_all_images(results,
                    frame_no,
                    show_raw_image=True,
                    zoomed=True,
                    typ='best',
                    PLOT_GREEN=True,# raw 2d data
                    PLOT_RED=True,  # raw 3d data
                    PLOT_BLUE=True, # smoothed 3d data
                    ##recompute_3d=False,
                    do_undistort=True,
                    frame_type='full_frame_fmf',
                    fixed_im_centers=None,
                    fixed_im_lims=None,
                    plot_orientation=True,
                    plot_true_3d_line=False, # show real line3d info (don't adjust to intersect 3d point)
                    plot_3d_unit_vector=True,
                    origin='upper', # upper is right-side up, lower works better in mpl (no y conversions)
#                    origin='lower',
                    display_labels=True,
                    display_titles=True,
                    start_frame_offset=188191, # used to calculate time display
                    max_err=None,
                    plot_red_ori_fixed=False,
                    no_2d_data_mode=None,
                    plt_all_images_locals_cache=None,
                    colormap='jet'):
    """plots images saved from cameras with various 2D/3D data overlaid

    last used significantly: 2006-05-06
    """

    if plt_all_images_locals_cache is None:
        plt_all_images_locals_cache = {}

    if no_2d_data_mode==None:
        no_2d_data_mode='fullframe_plot_reproj'

    if no_2d_data_mode not in ['fullframe_plot_reproj',
                               'blank',
                               'plot_reproj_with_bg',
                               ]:
        raise ValueError('invalid no_2d_data_mode')

    #if origin == 'upper' and zoomed==True: raise NotImplementedError('')
    if plot_true_3d_line and plot_3d_unit_vector:
        raise ValueError('options plot_true_3d_line and plot_3d_unit_vector are mutually exclusive')
    
    if fixed_im_centers is None:
        fixed_im_centers = {}
    if fixed_im_lims is None:
        fixed_im_lims = {}
    ioff()
    
    import flydra.reconstruct

    if 'reconstructor' not in plt_all_images_locals_cache:
        plt_all_images_locals_cache['reconstructor'] = (
            flydra.reconstruct.Reconstructor(results))
    reconstructor = plt_all_images_locals_cache['reconstructor']
    assert reconstructor.cal_source == results # make sure cache is valid
    
    if typ == 'fast':
        data3d = results.root.data3d_fast
    elif typ == 'best':
        data3d = results.root.data3d_best

    data2d = results.root.data2d_distorted
    cam_info = results.root.cam_info

    if colormap == 'jet':
        cmap = cm.jet
    elif colormap == 'pink':
        cmap = cm.pink
    elif colormap.startswith('gray'):
        cmap = cm.gray
    else:
        raise ValueError("unknown colormap '%s'"%colormap)

    camn2cam_id = {}
    for row in cam_info:
        cam_id, camn = row['cam_id'], row['camn']
        camn2cam_id[camn]=cam_id
    
    # find total number of cameras plugged in:
    cam_ids=list(sets.Set(cam_info.cols.cam_id))
    cam_ids.sort()

    tmp = [((tmpx['x'],tmpx['y'],tmpx['z']),
            (tmpx['p0'],tmpx['p1'],tmpx['p2'],tmpx['p3'],tmpx['p4'],tmpx['p5']),
            tmpx['camns_used'], tmpx['mean_dist']
            ) for tmpx in data3d.where( data3d.cols.frame == frame_no) ]
    if len(tmp) == 0:
        X, line3d = None, None
        camns_used = ()
    else:
        assert len(tmp)==1
        X, line3d, camns_used, err = tmp[0]
        camns_used = map(int,camns_used.split())
        if max_err is not None:
            if err > max_err:
                X, line3d = None, None
                camns_used = ()
                
    undistorted_bgs = {}
        
    clf()
    figtext( 0.5, 0.99, '% 5.2f sec'%( (frame_no-start_frame_offset)/100.0,),
             horizontalalignment='center',
             verticalalignment='top',
             )
    for subplot_number,cam_id in enumerate(cam_ids):
        width, height = reconstructor.get_resolution(cam_id)
        
        i = cam_ids.index(cam_id)
        ax=dougs_subplot(subplot_number)
        setp(ax,'frame_on',display_labels)

        xs=[]
        ys=[]
        slopes=[]
        eccentricities=[]
        remote_timestamps=[]
        
        for row in data2d.where(data2d.cols.frame==frame_no):
            test_camn = row['camn']
            if camn2cam_id[test_camn] == cam_id:
                camn=test_camn
                rx = row['x']
                if nx.isnan(rx):
                    continue
                ry = row['y']
                rx,ry=reconstructor.undistort(cam_id,(rx,ry))
                xs.append(rx)
                ys.append(ry)
                slopes.append(row['slope'])
                eccentricities.append(row['eccentricity'])
                remote_timestamps.append(row['timestamp'])
                    
        # check to make sure they're from the same time
        if len(sets.Set(remote_timestamps))>1:
            raise RuntimeError("somehow more than one timestamp from same frame!?")
        if len(remote_timestamps):
            remote_timestamp = remote_timestamps[0]
            
        have_2d_data = bool(len(xs))
        if no_2d_data_mode=='blank' and not have_2d_data:
            setp(ax,'frame_on',False)
            setp(ax,'xticks',[])
            setp(ax,'yticks',[])
            continue

        title_str = cam_id
        
        im, movie_timestamp = None, None # make sure these aren't falsely set
        del im, movie_timestamp          # make sure these aren't falsely set
        undistorted_im = None

        if X is not None:
            if line3d is None:
                reproj_x,reproj_y=reconstructor.find2d(cam_id,X)
                reproj_l3=None
            else:
                if plot_true_3d_line:
                    reproj_xy,reproj_l3=reconstructor.find2d(cam_id,X,line3d)
                else:
                    U = flydra.reconstruct.line_direction(line3d)
                    if plot_3d_unit_vector:
                        reproj_xy=reconstructor.find2d(cam_id,X)
                        unit_x1, unit_y1=reconstructor.find2d(cam_id,X-5*U)
                        unit_x2, unit_y2=reconstructor.find2d(cam_id,X+5*U)
                    else:
                        line3d_fake = flydra.reconstruct.pluecker_from_verts(X,X+U)
                        reproj_xy,reproj_l3=reconstructor.find2d(cam_id,X,line3d_fake)
                reproj_x,reproj_y=reproj_xy
            if not do_undistort:
                reproj_x,reproj_y = reconstructor.distort(cam_id,(reproj_x,reproj_y))

        have_limit_data = False
        if show_raw_image:
            have_raw_image = False
            if have_2d_data:
                try:
                    im, movie_timestamp = get_movie_frame2(results, remote_timestamp, cam_id, frame_type=frame_type, width=width, height=height)
                    have_raw_image = True
##                except ValueError,exc:
##                    print 'WARNING: skipping frame for %s because ValueError: %s'%(cam_id,str(exc))
                except KeyError,exc:
                    print 'WARNING: skipping frame for %s because KeyError: %s'%(cam_id,str(exc))
                except caching_movie_opener.NoFrameRecordedHere,exc:
                    # probably small FMF file not recorded for this camera, which is not really worth printing error
                    pass
                except PT.exceptions.NoSuchNodeError:
                    print
                    print '*'*20
                    print 'ERROR: no such node returned from pytables.'
                    print 'This is usually because results.root.backgrounds'
                    print 'does not exist. Either create it or use'
                    print 'frame_type="small_frame_only" in kwargs to plot_all_images'
                    print 'You can create it using "simple_add_backgrounds(results)".'
                    print '*'*20
                    raise
            elif no_2d_data_mode=='plot_reproj_with_bg':
                try:
                    im = get_background_image(results, cam_id)
                except PT.exceptions.NoSuchNodeError:
                    print
                    print '*'*20
                    print 'ERROR: no such node returned from pytables.'
                    print 'This is usually because results.root.backgrounds'
                    print 'does not exist. Either create it or use'
                    print 'no_2d_data_mode="blank" in kwargs to plot_all_images'
                    print '*'*20
                    raise
                have_raw_image = True
                if do_undistort:
                    undistorted_im = get_undistorted_background_image(results, reconstructor, cam_id)
            if have_2d_data and have_raw_image:
                if remote_timestamp != movie_timestamp:
                    print 'Whoa! timestamps are not equal!',cam_id
                    print ' XXX may be able to fix display, but not displaying wrong image for now'
            if have_raw_image:
#                print cam_id,'have_raw_image True'
                if do_undistort:
                    if undistorted_im is not None:
                        im = undistorted_im # use cached image
                    else:
                        intrin = reconstructor.get_intrinsic_linear(cam_id)
                        k = reconstructor.get_intrinsic_nonlinear(cam_id)
                        f = intrin[0,0], intrin[1,1] # focal length
                        c = intrin[0,2], intrin[1,2] # camera center
                        im = undistort.rect(im, f=f, c=c, k=k)

                code_path_0 = False
                
                if have_2d_data and zoomed:
                    code_path_0 = True
                    xcenter, ycenter = xs[0],ys[0]
                    if not do_undistort:
                        xcenter,ycenter = reconstructor.distort(cam_id,(xcenter,ycenter))

                if fixed_im_lims.has_key(cam_id):
                    code_path_0 = True
                if no_2d_data_mode=='plot_reproj_with_bg':
                    if 0<=reproj_x<=width:
                        if 0<=reproj_y<=height:
                            xcenter,ycenter = reproj_x,reproj_y
                            code_path_0 = True
                    
                if code_path_0:
                    #print 'XXX code path 0',cam_id
                
                    w = 13
                    h = 20
                    xcenter, ycenter = fixed_im_centers.get(
                        cam_id, (xcenter,ycenter))
                    xmin = int(xcenter-w)
                    xmax = int(xcenter+w)
                    ymin = int(ycenter-h)
                    ymax = int(ycenter+h)

                    # workaround ipython -pylab mode:
                    max = sys.modules['__builtin__'].max
                    min = sys.modules['__builtin__'].min
                    
                    xmin = max(0,xmin)
                    xmax = min(xmax,width-1)
                    ymin = max(0,ymin)
                    ymax = min(ymax,height-1)

                    (xmin, xmax), (ymin, ymax) = fixed_im_lims.get(
                        cam_id, ((xmin,xmax),(ymin,ymax)) )
                    
                    if xmax>xmin and ymax>ymin: # distortion can cause this not to be true

                        if origin == 'upper':
                            show_ymin = height-ymax
                            show_ymax = height-ymin
                        else:
                            show_ymin = ymin
                            show_ymax = ymax

                        have_limit_data = True

                        if origin == 'upper':
                            extent = (xmin,xmax,height-ymin,height-ymax)
                        else:
                            extent = (xmin,xmax,ymin,ymax)
                        im = im.copy() # make contiguous
                        im_small = im[ymin:ymax,xmin:xmax]
                        
                        if origin == 'upper':
                            im_small = im_small[::-1] # flip-upside down
                        im_small = im_small.copy()
                        ax.imshow(im_small,
                                  origin=origin,
                                  interpolation='nearest',
                                  cmap=cmap,
                                  extent = extent,
                                  vmin=0,vmax=255,
                                  )
                    
                else:
                    if no_2d_data_mode=='plot_reproj_with_bg' and zoomed:
                        # plot nothing and continue...
                        setp(ax,'frame_on',False)
                        setp(ax,'xticks',[])
                        setp(ax,'yticks',[])
                        continue
                    
                    #print 'XXX code path 1',cam_id
                    xmin=0
                    xmax=width-1
                    ymin=0
                    ymax=height-1

##                    (xmin, xmax), (ymin, ymax) = fixed_im_lims.get(
##                        cam_id, ((xmin,xmax),(ymin,ymax)) )
                    
                    show_ymin = ymin
                    show_ymax = ymax
                    
                    if origin == 'upper':
                        extent = (xmin,xmax,height-ymax,height-ymin)
                    else:
                        extent = (xmin,xmax,ymin,ymax)

                    ax.imshow(im,
                              origin=origin,
                              interpolation='nearest',
                              extent=extent,
                              cmap=cmap,
                              vmin=0,vmax=255,
                              )

            else:
                #print 'XXX code path 2',cam_id
                    
                xmin=0
                xmax=width-1
                ymin=0
                ymax=height-1
                
                (xmin, xmax), (ymin, ymax) = fixed_im_lims.get(
                    cam_id, ((xmin,xmax),(ymin,ymax)) )
                
                show_ymin = ymin
                show_ymax = ymax

        if have_2d_data and camn is not None:
            for point_number,(x,y,eccentricity,slope) in enumerate(
                zip(xs,ys,eccentricities,slopes)):

                if not do_undistort:
                    x,y = reconstructor.distort(cam_id,(x,y))
                
                # raw 2D
                if PLOT_GREEN:
                    if origin == 'upper':
                        lines=ax.plot([x],[height-y],'o')
                    else:
                        lines=ax.plot([x],[y],'o')

                    if show_raw_image:
                        green = (0,1,0)
                        dark_green = (0, 0.2, 0)
                        dark_blue = (0, 0, 0.3)

                        used_camera_used_point_color = green
                        unused_camera_point_color = dark_green
                        used_camera_unused_point_color = dark_blue

                        setp(lines,'markerfacecolor',None)
                        if camn in camns_used:
                            if point_number==0:
                                setp(lines,'markeredgecolor',
                                     used_camera_used_point_color)
                            else:
                                setp(lines,'markeredgecolor',
                                     used_camera_unused_point_color)
                        elif camn not in camns_used:
                            setp(lines,'markeredgecolor',
                                 unused_camera_point_color)
                        setp(lines,'markeredgewidth',2.0)

                #if not len(numarray.ieeespecial.getnan(slope)[0]):
                if eccentricity > flydra.reconstruct.MINIMUM_ECCENTRICITY:
                    #title_str = cam_id + ' %.1f'%eccentricity

                    #eccentricity = min(eccentricity,100.0) # bound it

                    # ax+by+c=0
                    a=slope
                    b=-1
                    c=y-a*x

                    x1=xmin
                    y1=-(c+a*x1)/b
                    if y1 < ymin:
                        y1 = ymin
                        x1 = -(c+b*y1)/a
                    elif y1 > ymax:
                        y1 = ymax
                        x1 = -(c+b*y1)/a

                    x2=xmax
                    y2=-(c+a*x2)/b
                    if y2 < ymin:
                        y2 = ymin
                        x2 = -(c+b*y2)/a
                    elif y2 > ymax:
                        y2 = ymax
                        x2 = -(c+b*y2)/a                

                    if plot_orientation:
                        if origin == 'upper':
                            lines=ax.plot([x1,x2],[height-y1,height-y2],':',linewidth=1.5)
                        else:
                            lines=ax.plot([x1,x2],[y1,y2],':',linewidth=1.5)
                        if show_raw_image:
                            green = (0,1,0)
                            if camn in camns_used:
                                setp(lines,'color',green)
                            elif camn not in camns_used:
                                setp(lines,'color',(0, 0.2, 0))
                            #setp(lines,'color',green)
                            #setp(lines[0],'linewidth',0.8)
            del x,y,eccentricity,slope # remove loop variables
                            
        if X is not None:
##            if line3d is None:
##                reproj_x,reproj_y=reconstructor.find2d(cam_id,X)
##                reproj_l3=None
##            else:
##                if plot_true_3d_line:
##                    reproj_xy,reproj_l3=reconstructor.find2d(cam_id,X,line3d)
##                else:
##                    U = flydra.reconstruct.line_direction(line3d)
##                    if plot_3d_unit_vector:
##                        reproj_xy=reconstructor.find2d(cam_id,X)
##                        unit_x1, unit_y1=reconstructor.find2d(cam_id,X-5*U)
##                        unit_x2, unit_y2=reconstructor.find2d(cam_id,X+5*U)
##                    else:
##                        line3d_fake = flydra.reconstruct.pluecker_from_verts(X,X+U)
##                        reproj_xy,reproj_l3=reconstructor.find2d(cam_id,X,line3d_fake)
##                reproj_x,reproj_y=reproj_xy
            #near = 10
            if PLOT_RED:
                if origin=='upper':
                    lines=ax.plot([reproj_x],[height-reproj_y],'o')
                else:
                    lines=ax.plot([reproj_x],[reproj_y],'o')
                setp(lines,'markerfacecolor',(1,0,0))
                setp(lines,'markeredgewidth',0.0)
                setp(lines,'markersize',4.0)
                
            if PLOT_RED and line3d is not None:
                if plot_orientation:
                    if plot_3d_unit_vector:
                        if origin == 'upper':
                            if plot_red_ori_fixed:
                                lines=ax.plot([reproj_x,unit_x1],[height-reproj_y,height-unit_y1],'r-',linewidth=1.5)
                            else:
                                lines=ax.plot([unit_x1,unit_x2],[height-unit_y1,height-unit_y2],'r-',linewidth=1.5)
                        else:
                            if plot_red_ori_fixed:
                                lines=ax.plot([reproj_x,unit_x1],[reproj_y,unit_y1],'r-',linewidth=1.5)
                            else:
                                lines=ax.plot([unit_x1,unit_x2],[unit_y1,unit_y2],'r-',linewidth=1.5)
                    else:
                        a,b,c=reproj_l3
                        # ax+by+c=0

                        # y = -(c+ax)/b
                        # x = -(c+by)/a


                        x1=xmin
                        y1=-(c+a*x1)/b
                        if y1 < ymin:
                            y1 = ymin
                            x1 = -(c+b*y1)/a
                        elif y1 > ymax:
                            y1 = ymax
                            x1 = -(c+b*y1)/a

                        x2=xmax
                        y2=-(c+a*x2)/b
                        if y2 < ymin:
                            y2 = ymin
                            x2 = -(c+b*y2)/a
                        elif y2 > ymax:
                            y2 = ymax
                            x2 = -(c+b*y2)/a

                        if origin == 'upper':
                            lines=ax.plot([x1,x2],[height-y1,height-y2],'r--',linewidth=1.5)
                        else:
                            lines=ax.plot([x1,x2],[y1,y2],'r--',linewidth=1.5)
                        del a,b,c # keep namespace clean(er)
        if PLOT_BLUE:
            smooth_data = results.root.smooth_data
            have_smooth_data = False
            for row in smooth_data:
                if row['frame'] == frame_no:
                    Psmooth = nx.array( (row['x'], row['y'], row['z']) )
                    Qsmooth = cgtypes.quat( row['qw'], row['qx'], row['qy'], row['qz'] )
                    have_smooth_data = True
                    break
            if not have_smooth_data:
                # make sure we don't use old data
                Psmooth = None
                Qsmooth = None
            else:
                #x,l3=reconstructor.find2d(cam_id,Psmooth,line3d)
                U=nx.array(PQmath.quat_to_orient(Qsmooth))
                x,y            = reconstructor.find2d(cam_id,Psmooth)
                unit_x, unit_y = reconstructor.find2d(cam_id,(Psmooth-5.0*U))

                if origin=='upper':
                    lines=ax.plot([x],[height-y],'o')
                else:
                    lines=ax.plot([x],[y],'o')
                setp(lines,'markerfacecolor',(0,0,1))

                if origin == 'upper':
                    lines=ax.plot([x,unit_x],[height-y,height-unit_y],'b-',linewidth=1.5)
                else:
                    lines=ax.plot([x,unit_x],[y,unit_y],'b-',linewidth=1.5)
                
        if display_titles:
            title(title_str)
        if have_limit_data:
            ax.xaxis.set_major_locator( LinearLocator(numticks=2) )
            ax.yaxis.set_major_locator( LinearLocator(numticks=2) )
            setp(ax,'xlim',[xmin, xmax])
            setp(ax,'ylim',[show_ymin, show_ymax])
        elif fixed_im_lims.has_key(cam_id):
            (xmin, xmax), (ymin, ymax) = fixed_im_lims[cam_id]
            ax.xaxis.set_major_locator( LinearLocator(numticks=2) )
            ax.yaxis.set_major_locator( LinearLocator(numticks=2) )
            setp(ax,'xlim',[xmin, xmax])
            setp(ax,'ylim',[show_ymin, show_ymax])
        else:
            margin_pixels = 20
            setp(ax,'xlim',[-margin_pixels, width+margin_pixels])
            setp(ax,'ylim',[-margin_pixels, height+margin_pixels])
        if not display_labels:
            setp(ax,'xticks',[])
            setp(ax,'yticks',[])
        labels=ax.get_xticklabels()
        setp(labels, rotation=90)
    ion()

def test():
    import flydra.reconstruct
    frames = get_frames_with_3d(results)
    reconstructor = flydra.reconstruct.Reconstructor(results)
    for frame in frames:
        try:
            redo_3d_calc(results,frame,reconstructor=reconstructor,
                         verify=True,overwrite=False)
        except Exception, x:
            status('ERROR (frame %d): %s'%(frame,str(x)))

def update_small_fmf_summary(results,cam_id,roi_movie_basename):
    """creates summary for .fmf/.smd files and saves to HDF5 file

    last used extensively 2006-05-17
    """
    status('making summary ROI movie info for %s'%cam_id)
    
    cam_summary = results.root.data2d_camera_summary
    
    if hasattr(results.root,'small_fmf_summary'):
        small_fmf_summary = results.root.small_fmf_summary
    else:
        small_fmf_summary = results.createTable(results.root,'small_fmf_summary',SmallFMFSummary,'')
    table = small_fmf_summary
    
    fmf_filename = roi_movie_basename + '.fmf'
    #smd_filename = roi_movie_basename + '.smd'

    fmf = FlyMovieFormat.FlyMovie(fmf_filename,check_integrity=True)

    # first frame
    if 0:
        fmf_timestamps = fmf.get_all_timestamps()
        fmf_start_timestamp = fmf_timestamps[0]
        fmf_stop_timestamp = fmf_timestamps[-1]
    else:
        fmf.seek(0)
        fmf_start_timestamp = fmf.get_next_timestamp()
        
        fmf.seek(-1)
        fmf_stop_timestamp = fmf.get_next_timestamp()
    fmf.close()
    
    for row in cam_summary.where( cam_summary.cols.cam_id == cam_id ):
        start_timestamp = max(fmf_start_timestamp,row['start_timestamp'])
        stop_timestamp = min(fmf_stop_timestamp,row['stop_timestamp'])
        # check that there was some overlap between movie and data file:
        if start_timestamp > row['stop_timestamp']:
            continue
        if stop_timestamp < row['start_timestamp']:
            continue
        
        # keep in loop because cam_id can have multiple camns
        newrow = table.row
        newrow['cam_id'] = cam_id
        newrow['camn'] = row['camn']
        newrow['start_timestamp'] = start_timestamp
        newrow['stop_timestamp'] = stop_timestamp
        newrow['basename'] = roi_movie_basename
        newrow.append()
    table.flush()
        
def plot_simple_phase_plots(results,form='xy',max_err=10,typ='best',ori_180_ambig=True):
    from matplotlib.collections import LineCollection
    f,xyz,L,err = get_f_xyz_L_err(results,max_err=max_err,typ=typ)
    import flydra.reconstruct
    U = flydra.reconstruct.line_direction(L) # unit vector
    if form == 'xy':
        hidx = 0
        hname = 'X (mm)'
        vidx = 1
        vname = 'Y (mm)'
    elif form == 'xz':
        hidx = 0
        hname = 'X (mm)'
        vidx = 2
        vname = 'Z (mm)'
    plot(xyz[:,hidx],xyz[:,vidx],'o',mec=(0,0,0),mfc=None,ms=2.0)
    segments = []
    for i in range(len(U)):
        pi = xyz[i]
        Pqi = U[i]
        if len(getnan(pi)[0]) or len(getnan(Pqi)[0]):
            continue
        if ori_180_ambig:
            segment = ( (pi[hidx]+Pqi[hidx]*1,   # x1
                         pi[vidx]+Pqi[vidx]*1),   # y1
                        (pi[hidx]-Pqi[hidx]*1,   # x2
                         pi[vidx]-Pqi[vidx]*1) ) # y2
        else:
            segment = ( (pi[hidx],  # x1
                         pi[vidx]), # y1
                        (pi[hidx]-Pqi[hidx]*2,   # x2
                         pi[vidx]-Pqi[vidx]*2) ) # y2
        #print segment
        segments.append( segment )
    collection = LineCollection(segments)#,colors=[ (0,0,1) ] *len(segments))
    gca().add_collection(collection)
    xlabel(hname)
    ylabel(vname)

def switch_calibration_data(results,new_caldir):
    import flydra.reconstruct
    new_reconstructor = flydra.reconstruct.Reconstructor(new_caldir)
    new_reconstructor.save_to_h5file( results, OK_to_delete_old_calibration=True )

def emit_recalibration_data(results, calib_dir,
                            ignore_camns_used=False,
                            use_frames_with_multiple_tracked_points=False,
                            max_err=10.0,
                            debug=False,
                            force_cam_ids=None
                            ):
    """
    take found 2D points and generate calibration data

    last used 2006-05-18
    """
    import flydra.reconstruct

    if not os.path.exists(calib_dir):
        os.makedirs(calib_dir)

    if force_cam_ids is None:
        force_cam_ids = []

    seq = (int(5e5), int(2e6), 100)
    print 'Using start %f, stop %f, inc %f'%seq

    #seq = (0, int(1.3e6), 100)
    reconstructor = flydra.reconstruct.Reconstructor(results)

    data2d = results.root.data2d_distorted

    if hasattr(results.root,'data2d'):
        # this is a precaution against screwing up later...
        raise RuntimeError("will not continue unless undistorted data2d removed")

    if 1:
        cam_centers = nx.asarray([reconstructor.get_camera_center(cam_id)[:,0]
                                  for cam_id in reconstructor.get_cam_ids()])
        print 'OLD camera centers (may be useful for recalibration):'
        save_ascii_matrix(sys.stdout,cam_centers)
        print
    
    data3d = results.root.data3d_best
    coords = data3d.getWhereList(data3d.cols.mean_dist <= max_err)
    coords_frames = nx.asarray(data3d.readCoordinates( coords, 'frame' ))
    if not ignore_camns_used:
        coords_camns_used = nx.asarray(data3d.readCoordinates( coords, 'camns_used' ))

    # make sure it's ascending
    cfdiff = coords_frames[1:]-coords_frames[:-1]
    if nx.amin(cfdiff) < 0:
        raise RuntimeError('not ascending frames in data3d (needed for searchsorted)')

    camn2cam_id, cam_id2camns = get_caminfo_dicts(results)
    if debug:
        print 'cam_id2camns',cam_id2camns
    to_output = []
    if 1:
        framelist = nx.arange(seq[0],seq[1],seq[2],dtype=int)
        frame_idxs = coords_frames.searchsorted(framelist)
        for i in range(len(framelist)):
            if i%100==0:
                print '%d of %d'%(i,len(framelist))
            frame_idx = frame_idxs[i]
            desired_frame = framelist[i]
            if frame_idx >= len(coords_frames):
                # desired frame isn't in data3d
                continue
            frame = coords_frames[ frame_idx ]
            if frame != desired_frame:
                # sanity check
                continue

            if not ignore_camns_used:
                camns_used = map(int,coords_camns_used[frame_idx].split())

            if ignore_camns_used or len(camns_used) >= 3:
                #print '\nframe',frame
                #print 'row:',data3d[coords[frame_idx]]
                row_dict = {}
                if PT.__version__ <= '1.3.3':
                    oldframe=frame
                    frame=int(frame)
                    assert frame==oldframe # check for rounding error
                if debug:
                    print 'frame',repr(frame),type(frame)
                for row in data2d.where( data2d.cols.frame == frame ):
                    camn = row['camn']
                    if ignore_camns_used or (camn in camns_used):
                        cam_id = camn2cam_id[camn]
                        x=row['x']
                        y=row['y']
                        if nx.isnan(x):
                            continue
                        #print camn2cam_id[row['camn']],x,y
                        if debug:
                            print '  %s: % 5.1f, % 5.1f (camn %d) -- '%(cam_id,x,y,camn),
                        if cam_id in row_dict:
                            if use_frames_with_multiple_tracked_points:
                                if debug:
                                    print 'not used'
                                continue
                            else:
                                print 'multiple tracked points, skipping frame %d'%frame
                                row_dict = {}
                                break
                        else:
                            if debug:
                                print 'used'
                        row_dict[cam_id] = (x,y)
                forced_ignore = False
                for cam_id in force_cam_ids:
                    if cam_id not in row_dict:
                        forced_ignore = True
                        break
                if forced_ignore:
                    print 'forcing ignore because cam_id is missing'
                    if debug:
                        print
                    continue
                if debug:
                    if len(row_dict)<3:
                        print 'not saving -- less than 3 points'
                        print
                        continue
                    else:
                        cam_ids = row_dict.keys()
                        cam_ids.sort()
                        print '  saving %d cameras:'%len(cam_ids),' '.join(cam_ids)
                        print
                to_output.append(row_dict)
                
    all_cam_ids = cam_id2camns.keys()
    all_cam_ids.sort()
    print '%d calibration points found'%len(to_output)

    # make output format matrices
    IdMat = []
    points = []
    useful_points = 0

    count_by_cam_id = {}
    for cam_id in all_cam_ids:
        count_by_cam_id[cam_id]=0
        
    for row_dict in to_output:
        ids = []
        save_points = []
        n_cams = 0
        this_cam_ids = []
        for cam_id in all_cam_ids:
            if cam_id in row_dict:
                pt = row_dict[cam_id]
                save_pt = pt[0],pt[1],1.0
                id = 1
                n_cams += 1
                this_cam_ids.append(cam_id)
            else:
                save_pt = nan,nan,nan
                id = 0
            ids.append(id)
            save_points.extend(save_pt)
        if n_cams>=3:
            useful_points += 1
            for cam_id in this_cam_ids:
                count_by_cam_id[cam_id]+=1

        IdMat.append(ids)
        points.append( save_points )

    print '  %d points total with 3 or more cameras'%useful_points
    for cam_id in all_cam_ids:
        print '    %s: %d points with 3 or more cameras'%(cam_id,count_by_cam_id[cam_id])
    print 'saving to disk...'

    IdMat = nx.transpose(nx.array(IdMat))
    points = nx.transpose(nx.array(points))

    # resolution
    Res = []
    for cam_id in all_cam_ids:
        Res.append( reconstructor.get_resolution(cam_id) )
    Res = nx.array( Res )

    # save the data
    
    save_ascii_matrix(os.path.join(calib_dir,'IdMat.dat'),IdMat)
    save_ascii_matrix(os.path.join(calib_dir,'points.dat'),points)
    save_ascii_matrix(os.path.join(calib_dir,'Res.dat'),Res)
    
    fd = open(os.path.join(calib_dir,'camera_order.txt'),'w')
    for cam_id in all_cam_ids:
        fd.write('%s\n'%cam_id)
    fd.close()
            
def save_movie(results):
    """convenience function for calling plot_all_images

    last used significantly: 2006-05-06
    """
##    fixed_im_centers = {'cam1:0':(260,304),
##                    'cam2:0':(402,226),
##                    'cam3:0':(236,435),
##                    'cam4:0':(261,432),
##                    'cam5:0':(196,370)}
    full_frame = ((0,655),(0,490))
    fixed_im_lims = {
        'cam1:0':((70,  170), (200, 270)),
        'cam2:0':((350, 430), ( 80, 220)),
        'cam3:0':((350, 450), (  0, 160)),
        'cam4:0':((290, 400), ( 20, 180)),
        'cam5:0':((355, 395), (130, 200)),
        }

    cam_ids = get_cam_ids(results,)

    #start_frame = 374420
    #stop_frame = 374720

    if 0:
        start_frame = 1019640
        stop_frame = 1019700
    elif 0: # near landing
        start_frame = 873910
        stop_frame =  874020
    elif 0: # take off
        start_frame = 295160
        stop_frame =  295180
    elif 1: #jerky
        start_frame = 1021340
        stop_frame =  1021350
        
    
    plt_all_images_locals_cache = {}
    for frame in range(start_frame, stop_frame, 1):
        clf()
        try:
            fname = 'jerky/frame_%07d.png'%frame
            try: os.unlink(fname)
            except OSError, err: pass
            print ' plotting',fname,'...',
            sys.stdout.flush()
            plot_all_images(results, frame,
                            #fixed_im_centers=fixed_im_centers,
                            #fixed_im_lims=fixed_im_lims,
                            #colormap='grayscale',
                            colormap='jet',
                            #zoomed=True,
                            
                            #frame_type='small_frame_only',
                            frame_type='small_frame_and_bg',

                            #do_undistort=False, # leave images untouched, but drawn lines are undistorted, while everything else is distorted
                            do_undistort=True, # for alignment with points
                            
                            plot_true_3d_line=False,
                            plot_3d_unit_vector=False,
                            
                            #plot_orientation=True,
                            plot_orientation=False,
                            
                            origin='lower',
                            display_labels=False,
                            display_titles=False,
                            #display_titles=True,
                            start_frame_offset=start_frame,
                            #PLOT_GREEN=False,
                            PLOT_RED=True,
                            #PLOT_RED=False,
                            PLOT_BLUE=False,
                            max_err=10,
                            plot_red_ori_fixed=False,
                            no_2d_data_mode='blank',
                            plt_all_images_locals_cache=plt_all_images_locals_cache,
                            )
            
            print ' saving...',
            sys.stdout.flush()
            savefig(fname)
            print 'done'
        except Exception, x:
            #print x, str(x)
            raise

def plot_camera_view(results,camn):
    ioff()
    try:
        start_frame = 68942+330
        stop_frame = 68942+360
        
        f1 = start_frame
        f2 = stop_frame

        for row in results.root.cam_info:
            if camn == row['camn']:
                cam_id = row['cam_id']

        f = []
        x = []
        y = []
        cam_timestamps = []
        for row in results.root.data2d:
            if row['camn'] != camn:
                continue
            if f1<=row['frame']<=f2:
                if len( getnan(row['x'])[0] ) == 0:
                    f.append( row['frame'] )
                    x.append( row['x'] )
                    y.append( row['y'] )
                    cam_timestamps.append( row['timestamp'] )
        plot(x,y,'o-',mfc=None,mec='k',ms=2.0)
        for i,frame in enumerate(f):
            t = (frame-start_frame) / 100.0
            #if (t%0.1) < 1e-5 or (t%0.1)>(0.1-1e-5):
            if 1:
                text( x[i], y[i], str(t) )
        title(cam_id)
    finally:
        ion()
    return f, cam_timestamps
    
def get_data_array(results):
##    import flydra.reconstruct
##    save_ascii_matrix
    
    data3d = results.root.data3d_best

    M = []
    for row in data3d.where( 132700 <= data3d.cols.frame <= 132800 ):
        M.append( (row['frame'], row['x'], row['y'], row['z'] ) )
    M = nx.array(M)
    return M

def get_start_stop_times( results ):
    data3d = results.root.data3d_best
    
    # XXX Assume all rows in table are in chronological
    # order. (Messing with tables during analysis could have screwed
    # this up.)

    try:
        results = data3d.cols.timestamp[0], data3d.cols.timestamp[-1]
    except IndexError:
        results = None
        
    return results

##    all_times = data3d.cols.timestamp
##    timin = nx.argmin(all_times)
##    timax = nx.argmax(all_times)
##    return all_times[timin], all_times[timax]
    
def get_timestamp( results, frame, cam):
    camn2cam_id = {}
    for row in results.root.cam_info:
        cam_id, camn = row['cam_id'], row['camn']
        camn2cam_id[camn]=cam_id

    found = False
    if isinstance(cam,int):
        camn = cam
        for row in results.root.data2d:
            if row['frame'] == frame and row['camn'] == camn:
                timestamp = row['timestamp']
                found = True

    else:
        cam_id = cam
        for row in results.root.data2d:
            if row['frame'] == frame:
                camn = row['camn']
                if camn2cam_id[camn] == cam_id:
                    if found:
                        print 'WARNING: multiple frames found with same'
                        print 'timestamp and cam_id. (Use camn instead.)'
                    timestamp = row['timestamp']
                    found = True
    if found:
        return timestamp
    else:
        return None

class RT_Analyzer_State:
    def __init__(self, results, camn, diff_threshold, clear_threshold, start_frame):
        cam_info = results.root.cam_info
        cam_id = [x['cam_id'] for x in cam_info if x['camn'] == camn ][0]

        frame, timestamp, self.rt_state = get_frame_ts_and_realtime_analyzer_state( results,
                                                                                    frame = start_frame,
                                                                                    camn = camn,
                                                                                    diff_threshold=diff_threshold,
                                                                                    clear_threshold=clear_threshold,
                                                                         )
        self.cur_frame_no = start_frame
        self.cur_image = frame

        fg_frame_server = get_server(cam_id)
        bg_frame_server = get_server(cam_id,port=9899) # port 9889 for bg images
        self.frame_server_dict_fg = { cam_id:fg_frame_server }
        self.frame_server_dict_bg = { cam_id:bg_frame_server }
        

def get_frame_ts_and_realtime_analyzer_state( results,
                                              frame = 6804,
                                              camn = 15,
                                              diff_threshold=None,
                                              clear_threshold=None,
                                              ):
    timestamp = None
    data2d = results.root.data2d_distorted
    for row in data2d.where(data2d.cols.frame==frame):
        if row['camn'] == camn:
            timestamp = row['timestamp']
            break
    if timestamp is None:
        return None,None,None
    print 'timestamp',timestamp
            
    cam_info = results.root.cam_info      
    cam_id = [x['cam_id'] for x in cam_info if x['camn'] == camn ][0]

    import realtime_image_analysis
    import flydra.reconstruct
    import FastImage
    
    frame,timestamp2=get_movie_frame2(results, timestamp, camn, suffix='')
    assert timestamp2-timestamp < 1e-15
    bg_frame,bg_timestamp=get_movie_frame2(results, timestamp, camn, suffix='_bg')
    bg_frame_fi = FastImage.asfastimage(bg_frame)
    std_frame,std_timestamp=get_movie_frame2(results, timestamp, camn, suffix='_std')
    std_frame_fi = FastImage.asfastimage(std_frame)
    
    if 0:
        diff = frame.astype(numarray.Int32) - bg_frame.astype(numarray.Int32)
        imshow(diff)
        colorbar()
    
    reconstructor = flydra.reconstruct.Reconstructor(results)
    
    ALPHA = 0.1
    rt = realtime_image_analysis.RealtimeAnalyzer(frame.shape[1],frame.shape[0])
    
    mean_im = rt.get_image_view('mean')
    bg_frame_fi.get_8u_copy_put(mean_im,bg_frame_fi.size)

    cmp_im = rt.get_image_view('cmp')
    std_frame_fi.get_8u_copy_put(cmp_im,std_frame_fi.size)
    
    rt.set_reconstruct_helper( reconstructor.get_recontruct_helper_dict()[cam_id] )
    rt.pmat = reconstructor.get_pmat(cam_id)
    if clear_threshold is None:
        clear_threshold = 0.9 # XXX should save to fmf file??
        print 'WARNING: set clear_threshold to',clear_threshold
    if diff_threshold is None:
        diff_threshold = 15 # XXX should save to fmf file??
        print 'WARNING: set diff_threshold to',diff_threshold
    rt.clear_threshold = clear_threshold
    rt.diff_threshold = diff_threshold
    return frame, timestamp, rt

def show_working_image(results,cam,fno,
                       diff_threshold=15.0,
                       clear_threshold=0.2):
    if isinstance(cam,int):
        camn = cam
    else:
        orig_cam_id = cam
        
        cam_id2camns = {}
        for row in results.root.cam_info:
            add_cam_id, add_camn = row['cam_id'], row['camn']
            cam_id2camns.setdefault(add_cam_id,[]).append(add_camn)

        found = False
        for row in results.root.data2d.where(results.root.data2d.cols.frame==fno):
            test_camn = row['camn']
            if test_camn in cam_id2camns[orig_cam_id]:
                camn = test_camn
                found = True
                break

        if not found:
            raise ValueError("could not find data for cam")
                    
    use_roi2 = True
    frame, ts, rt = get_frame_ts_and_realtime_analyzer_state( results,
                                                              fno, camn,
                                                              diff_threshold,
                                                              clear_threshold)
    points, found, orientation = rt.do_work(frame,0,fno,use_roi2)
    wi = rt.get_working_image()
    imshow(wi,interpolation='nearest',origin='lower')
    colorbar()
    return points[0]

def recompute_2d_data(results,
                      diff_threshold=15.0,
                      clear_threshold=0.2,
                      max_num_points = 3,
                      roi2_radius = 20,
                      use_roi2 = True,
                      use_cmp = True,
                      movie_dir=None,
                      ):
    """

    last used 2007 01 04

    """
    import flydra_ipp.realtime_image_analysis4 as realtime_image_analysis
    import flydra.reconstruct
    import FastImage
    import Image
    
    camn2cam_id, cam_id2camns = get_caminfo_dicts(results)
    
    data2d = results.root.data2d_distorted
    
    # step 0: find all camns
    camns = data2d.read(field='camn',flavor='numpy')
    camns = numpy.unique(camns)

    if 0:
        print 'WARNING: limiting cams'
        camns = [14]

    timestamp2frame = {}

    # step 1: get movie timestamps/ flydra main brain frame number correlation
    for camn in camns:

        this_camn_idxs = data2d.getWhereList( data2d.cols.camn==int(camn), flavor='numpy' )
        this_camn_timestamps = data2d.readCoordinates( this_camn_idxs,
                                                       field='timestamp', flavor='numpy')
        this_camn_frames = data2d.readCoordinates( this_camn_idxs,
                                                   field='frame', flavor='numpy')
        timestamp2frame[camn] = LinearInterpolator( this_camn_timestamps, this_camn_frames )
        
        # check interpolation
        frame_guesses = timestamp2frame[camn](this_camn_timestamps)
        if numpy.any( abs(frame_guesses-this_camn_frames) > 0.5 ):
                raise ValueError('frame interpolation not working!')
        else:
            print 'interpolation working (camn %d)!'%(camn,)

    # step 2: delete old table and create new one
    Info2D = results.root.data2d_distorted.description # 2D data format for PyTables
    Info2DColNames = Info2D._v_names
    Info2DColFormats = Info2D._v_nestedFormats
    results.removeNode( results.root.data2d_distorted, recursive=True)
    results.createTable( results.root, 'data2d_distorted',
                         Info2D, "2d data" )
    data2d = results.root.data2d_distorted

    # step 3: open each movie file

    for camn in camns:
        # loop over every camera
        cam_id = camn2cam_id[camn]
        
        for row in results.root.movie_info.where(results.root.movie_info.cols.cam_id==cam_id):
            # loop over every .fmf file
            
            filename = row['filename']
            
            if movie_dir is not None:
                filename = os.path.join(movie_dir,os.path.split(filename)[-1])
            
            bg_filename = os.path.splitext(filename)[0]+'_bg.fmf'
            std_filename = os.path.splitext(filename)[0]+'_std.fmf'

            print 'camn %d, cam_id %s, file %s'%(camn,cam_id,filename)

            fmf = FlyMovieFormat.FlyMovie(filename,check_integrity=True)
            fmf_bg = FlyMovieFormat.FlyMovie(bg_filename,check_integrity=True)
            fmf_std = FlyMovieFormat.FlyMovie(std_filename,check_integrity=True)

            w = fmf.get_width()
            h = fmf.get_height()
            lbrt = (0,0,w-1,h-1)
            
            rt = realtime_image_analysis.RealtimeAnalyzer(lbrt,w,h,
                                                          max_num_points,roi2_radius)

            # set initial state
            reconstructor = flydra.reconstruct.Reconstructor(results)
            rt.scale_factor = reconstructor.get_scale_factor()
            helper = reconstructor.get_reconstruct_helper_dict()[cam_id]
            rt.set_reconstruct_helper( helper )
            rt.set_pmat( reconstructor.get_pmat(cam_id) )
            rt.clear_threshold = clear_threshold
            rt.diff_threshold = diff_threshold
            
            mean_im = rt.get_image_view('mean')
            cmp_im = rt.get_image_view('cmp')
            next_bg_frame, next_bg_timestamp = fmf_bg.get_next_frame()
            next_std_frame, next_std_timestamp = fmf_std.get_next_frame()

            return_first_xy = False
            max_time_sec = 1.0

            # loop over every frame
            count = 0
            while 1:
                try:
                    current_frame, current_timestamp = fmf.get_next_frame()
                except FlyMovieFormat.NoMoreFramesException:
                    break
                count += 1

                if count%100==0:
                    print '%d frames analyzed'%(count,)
                    
                if current_timestamp >= next_bg_timestamp:
                    current_bg_frame = next_bg_frame
                    current_bg_timestamp = next_bg_timestamp
                    try:
                        next_bg_frame, next_bg_timestamp = fmf_bg.get_next_frame()

                        bg_frame_fi = FastImage.asfastimage(current_bg_frame)
                        bg_frame_fi.get_8u_copy_put(mean_im,bg_frame_fi.size)
                    except FlyMovieFormat.NoMoreFramesException:
                        print 'bg fmf ended (count %d)'%(count,)
                        next_bg_frame, next_bg_timestamp = None, numpy.inf
                    
                if current_timestamp >= next_std_timestamp:
                    current_std_frame = next_std_frame
                    current_std_timestamp = next_std_timestamp
                    try:
                        next_std_frame, next_std_timestamp = fmf_std.get_next_frame()

                        std_frame_fi = FastImage.asfastimage(current_std_frame)
                        std_frame_fi.get_8u_copy_put(cmp_im,std_frame_fi.size)
                    except FlyMovieFormat.NoMoreFramesException:
                        print 'std fmf ended (count %d)'%(count,)
                        next_std_frame, next_std_timestamp = None, numpy.inf
                        
                current_framenumber = int(round(timestamp2frame[camn](current_timestamp) ))
                if 0:
                    if current_framenumber < 11820:
                        continue
                    if current_framenumber > 11850:
                        continue
                hw_roi_frame = FastImage.asfastimage(current_frame)

                if False and 105 <= count <= 110:
                    print '%d timestamp: %s'%(count,repr(current_timestamp))
                    im=Image.fromstring('L',(w,h),current_frame.tostring())
                    im.save('frame%d.bmp'%count)
                    
                    im=Image.fromstring('L',(w,h),current_bg_frame.tostring())
                    im.save('frame_bg_%d.bmp'%count)
                    print 'current_bg_timestamp',current_bg_timestamp
                    
                    im=Image.fromstring('L',(w,h),current_std_frame.tostring())
                    im.save('frame_std_%d.bmp'%count)
                    print 'current_std_timestamp',current_std_timestamp
                
                points = rt.do_work(hw_roi_frame, current_timestamp, current_framenumber,
                                    use_roi2, use_cmp, return_first_xy, max_time_sec)

                save_camn = int(camn) # prevent weird numarray/pytables/numpy interaction bug
                list_of_rows_of_data2d = []
                if len(points):
                    for frame_pt_idx, pt in enumerate(points):
                        list_of_rows_of_data2d.append((save_camn, # defer saving to later
                                                 current_framenumber,
                                                 current_timestamp)
                                                +pt[:9]
                                                +(frame_pt_idx,))
                else:
                    frame_pt_idx = 0
                    nine_nans = (numpy.nan,numpy.nan,numpy.nan,numpy.nan,numpy.nan,
                                 numpy.nan,numpy.nan,numpy.nan,numpy.nan)
                    list_of_rows_of_data2d.append((save_camn, # defer saving to later
                                                   current_framenumber,
                                                   current_timestamp)
                                                  +nine_nans[:9]
                                                  +(frame_pt_idx,))

                recarray = numarray.records.array(
                    list_of_rows_of_data2d,
                    formats=Info2DColFormats,
                    names=Info2DColNames)
                data2d.append( recarray )

                if False and len(points):
                    print current_framenumber, len(points)
    data2d.flush()

def cam_usage(results,typ='best'):
    
    start_frame = 68942+330
    stop_frame = 68942+360
    
    if typ=='best':
        data3d = results.root.data3d_best
    elif typ=='fastest':
        data3d = results.root.data3d_fastest
        
    data2d = results.root.data2d
    cam_info = results.root.cam_info
    
    camn2cam_id = {}
    
    for row in cam_info:
        cam_id, camn = row['cam_id'], row['camn']
        camn2cam_id[camn]=cam_id
        
    for frame in range(start_frame, stop_frame+1):
        tmp_res = [ (row['camns_used'],row['mean_dist']) for row in data3d.where(data3d.cols.frame == frame) ]
        if len(tmp_res) == 0:
            continue
        assert len(tmp_res) == 1
        camns_used = map(int,tmp_res[0][0].split(' '))
        err = tmp_res[0][1]
        
#        print 'camns_used',camns_used
        used = [int(camn2cam_id[camn][3]) for camn in camns_used]
#        print 'used',used

        camns_found = []
        for row in data2d.where(data2d.cols.frame == frame):
            if not len(getnan([row['x']])[0]):
                camn = row['camn']
                camns_found.append( row['camn'] )
                cam_id = camn2cam_id[camn]
                num = int(cam_id[3])
                
        found_but_not_used = []
        for camn in camns_found:
            if camn not in camns_used:
                cam_id = camn2cam_id[camn]
                num = int(cam_id[3])
                found_but_not_used.append(num)
#        print 'found_but_not_used',found_but_not_used
        if 1:
        #if len(found_but_not_used):
            print 'frame %d:'%(frame,),
            
            found_but_not_used.sort()
            for i in range( 6 ):
                if i in found_but_not_used:
                    print 'X',
                elif i in used:
                    print '.',
                else:
                    print ' ',
                print '  ',
            print '% 3.1f'%err


##def calculate_3d_point(results, frame_server_dict=None):
##    by_cam_id = {}
##    for row in results.root.exact_movie_info:
##        print row
##        cam_id = row['cam_id']
##        if by_cam_id.has_key( cam_id ):
##            continue # already did this camera

##        if frame_server_dict is None:
##            frame_server = get_server(cam_id)
##        else:
##            frame_server = frame_server_dict[cam_id]

##        frame_server.load( row['filename'] )
##        frame, timestamp = frame_server.get_frame(0)
##        by_cam_id[cam_id] = frame

##    clf()
##    i = 0
##    for cam_id, frame in by_cam_id.iteritems():
##        i += 1
##        subplot(2,3,i)

##        cla()
##        imshow(frame)
##        title(cam_id)

##    return

##def plot_3d_point(results, X=None, frame_server_dict=None):
##    if X is not None:
##        import flydra.reconstruct
##        reconstructor = flydra.reconstruct.Reconstructor(results)
        
##    by_cam_id = {}
##    for row in results.root.exact_movie_info:
##        print row
##        cam_id = row['cam_id']
##        if by_cam_id.has_key( cam_id ):
##            continue # already did this camera

##        if frame_server_dict is None:
##            frame_server = get_server(cam_id)
##        else:
##            frame_server = frame_server_dict[cam_id]

##        frame_server.load( row['filename'] )
##        frame, timestamp = frame_server.get_frame(0)
##        by_cam_id[cam_id] = frame

##    clf()
##    i = 0
##    for cam_id, frame in by_cam_id.iteritems():
##        i += 1
##        subplot(2,3,i)

##        cla()
##        undistorted = flydra.undistort.undistort(reconstructor,frame,cam_id)
##        imshow(undistorted)
##        title(cam_id)

##        if X is not None:
##            xy=reconstructor.find2d(cam_id,X)
##            print xy
##            x,y=xy[:2]
##            plot( [x], [y], 'wo')
##        #ion()
##    return

##if 0:
##        class MyPickClass:
##            def __init__(self, d, cam_id):
##                self.d = d
##                self.cam_id = cam_id
##            def pick(self,event):
##                if event.key=='p' and event.inaxes is not None:
##                    ax = event.inaxes
##                    print self.cam_id, (event.x, event.y)
##                    self.d[self.cam_id] = (event.x, event.y)

##        picker = MyPickClass(by_cam_id,cam_id)
##        mpl_id = pylab.connect('key_press_event',picker.pick)
                         
##        try:
##            print 'connected MPL event',mpl_id
##            pylab.show()
##            print 'Press "p" to display cursor coordinates, press enter for next frame',mpl_id
##            raw_input()
##        finally:
##            pylab.disconnect( mpl_id )
##            print 'disconnected MPL event',mpl_id
##        print by_cam_id

def get_usable_startstop(results,min_len=100,max_break=5,max_err=10,typ='best'):
    f,xyz,L,err = get_f_xyz_L_err(results,max_err=max_err,typ=typ)
    del xyz
    del L
    
    sort_order = nx.argsort( f )
    f = f[sort_order]
    err = err[sort_order]
    
    good_frames = nx.take(f,nx.where( err < 10.0 ),axis=0)
    good_frames = good_frames[0] # make 1D array

    f_diff = good_frames[1:] - good_frames[:-1]

    break_idxs = nx.where(f_diff > max_break)
    break_idxs = break_idxs[0] # hmm, why must I do this?

    start_frame = good_frames[0]
    results = []
    for break_idx in break_idxs:
        stop_frame = good_frames[break_idx]
        
        if (stop_frame - start_frame + 1) >= min_len:
            results.append( (start_frame, stop_frame) )

        # for next loop
        start_frame = good_frames[break_idx+1]
    return results

def print_clock_diffs(results):
    table = results.root.host_clock_info
    hostnames = list(sets.Set(table.cols.remote_hostname))
    for hostname in hostnames:
        print '--',hostname,'--'
        for row in table.where( table.cols.remote_hostname == hostname ):
            diff = row['stop_timestamp'] - row['remote_timestamp']
            dur = row['stop_timestamp'] - row['start_timestamp']
            print '  %.1f msec (within %.2f msec)'%(diff*1e3,dur*1e3)
        print

def add_backgrounds_to_results(results,cam_id,bg_fmf_filename,frame=-1):
    fmf = FlyMovieFormat.FlyMovie(bg_fmf_filename,check_integrity=True)
    fmf.seek(frame)
    frame,timestamp = fmf.get_next_frame()
    
    if not hasattr(results.root,'backgrounds'):
        backgrounds_group = results.createGroup(results.root,'backgrounds')
    else:
        backgrounds_group = results.root.backgrounds
        
    pytables_filt = numpy.asarray
    results.createArray(backgrounds_group, cam_id,
                        pytables_filt(frame))

def simple_add_backgrounds(results):
    """automatically find and add background images

    last used significantly: 2006-05-16
    """
    camn2cam_id, cam_id2camns = get_caminfo_dicts(results)
    full_names = glob.glob('full*.fmf')
    for cam_id in cam_id2camns:
        print 'searching for background for',cam_id
        found_file = False
        worked_filename = None
        for full_name in full_names:
            if (full_name.endswith( cam_id + '_bg.fmf' ) or
                full_name.endswith( cam_id + '.fmf' )):
                print 'candidate file',full_name
                try:
                    fmf = FlyMovieFormat.FlyMovie(full_name,
                                                  check_integrity=True)
                    fmf.seek(-1)
                    frame,timestamp = fmf.get_next_frame()
                except Exception, err:
                    print '  failed'
                    print str(err)
                else:
                    print '  success'
                    found_file = True
                    worked_filename = full_name
                    if full_name.endswith( cam_id + '_bg.fmf' ):
                        # don't seek further
                        break
        if found_file:
            add_backgrounds_to_results(results,cam_id,worked_filename)
                    
if __name__=='__main__':
    # for running in ipython:
    try:
        results
    except NameError,err:
        pass
    else:
        results.close()
        del results
        
    results = get_results('DATA20061219_190812.h5',mode='r+')
    #results = get_results('DATA20060315_170142.h5',mode='r+')

    #del results.root.exact_movie_info
    #results.close()
    #make_exact_movie_info2(results)
    if 0:
        import FastImage
        for camn in [14]:
            for framenumber in range(1231000,1231800):
                print
                print 'camn',camn
                print 'framenumber',framenumber
                frame,timestamp,rt=get_frame_ts_and_realtime_analyzer_state(results,
                                                                            frame=framenumber,
                                                                            camn=camn,
                                                                            clear_threshold=0.2,
                                                                            diff_threshold=11,
                                                                            )
                print 'timestamp',timestamp
                #print 'frame',frame
                if frame is  None:
                    print 'no reconstructor'
                    continue
                raw_im = FastImage.asfastimage(frame)

                use_roi2=1
                use_cmp=1
                return_first_xy=0
                points = rt.do_work(raw_im, timestamp,
                                    framenumber,
                                    use_roi2,
                                    use_cmp,
                                    return_first_xy)
                for (x, y, area, slope, eccentricity, p1, p2, p3, p4, line_found, slope_found) in points:
                    print 're: x,y,p1,eccentricity',x,y,p1,eccentricity

                print

                data2d = results.root.data2d
                for r in data2d.where( data2d.cols.camn == camn ):
                    if r['frame']==framenumber:
                        print 'orig: x,y,p1,eccentricity',r['x'],r['y'],r['p1'],r['eccentricity']
                
##    if 1:
##        del results.root.exact_movie_info
##        results.close()
##        results = get_results('DATA20051122_224900.h5',mode='r+')
##        make_exact_movie_info2(results)
    if len(sys.argv) > 1:
        save_movie(results)
