#!/usr/bin/env python
import time, StringIO, sets, sys
import math
import numarray as nx
import tables as PT
import matplotlib
import matplotlib.pylab as pylab
from pylab import figure, plot, clf, imshow, cm, set
from pylab import gca, title, axes, ion, ioff, gcf, savefig
from matplotlib.ticker import LinearLocator

import flydra.undistort as undistort

from numarray.ieeespecial import getnan, nan, inf
import numarray.linear_algebra

import Pyro.core, Pyro.errors
Pyro.core.initClient(banner=0)

PROXY_PYRO = False
REFINE_THRESH = 10.0 # used in "hypothesis testing"

class ExactMovieInfo(PT.IsDescription):
    cam_id             = PT.StringCol(16,pos=0)
    filename           = PT.StringCol(255,pos=1)
    start_frame        = PT.Int32Col(pos=2)
    stop_frame         = PT.Int32Col(pos=3)

def status(status_string):
    print " status:",status_string

def my_subplot(n):
    x_space = 0.05
    y_space = 0.15
    
    left = n*0.2 + x_space
    bottom = 0 + + y_space
    w = 0.2 - (x_space*1.5)
    h = 1.0 - (y_space*2)
    return axes([left,bottom,w,h])

def dougs_subplot(n):
    
    row = n / 2
    col = n % 2
    
    x_space = 0.01
    y_space = 0.025
    
    left = col*0.5 + x_space
    bottom = (1.0-row*0.5)-0.5 + y_space
    w = 0.5 - x_space
    h = 0.5 - y_space
    return axes([left,bottom,w,h])

proxy_spawner = None
def get_server(cam_id):
    if PROXY_PYRO:
        import urlparse, socket
        global proxy_spawner

        if proxy_spawner is None:
            port = 9888
            hostname = 'localhost' # requires tunnelling (e.g. over ssh)

            proxy_URI = "PYROLOC://%s:%d/%s" % (hostname,port,'proxy_spawner')
            print 'connecting to',proxy_URI,'...',
            proxy_spawner = Pyro.core.getProxyForURI(proxy_URI)

        # make sure URI is local (so we can forward through tunnel)
        URI = str(proxy_spawner.spawn_proxy(cam_id))
        URI=URI.replace('PYRO','http') # urlparse chokes on PYRO://
        URIlist = list( urlparse.urlsplit(str(URI)) )
        network_location = URIlist[1]
        localhost = socket.gethostbyname(socket.gethostname())
        port = network_location.split(':')[1]
        URIlist[1] = '%s:%s'%(localhost,port)
        URI = urlparse.urlunsplit(URIlist)
        URI=URI.replace('http','PYRO')
    else:
        port = 9888
        hostname = cam_id.split(':')[0]

        URI = "PYROLOC://%s:%d/%s" % (hostname,port,'frame_server')

    frame_server = Pyro.core.getProxyForURI(URI)
    frame_server.noop()
        
    return frame_server

def flip_line_direction(results,frame,typ='best'):
    if typ=='best':
        data3d = results.root.data3d_best
    elif typ=='fastest':
        data3d = results.root.data3d_fastest

    for row in data3d.where( data3d.cols.frame == frame ):
        nrow = row.nrow()
        p2, p4, p5 = row['p2'], row['p4'], row['p5']
    data3d.cols.p2[nrow] = -p2
    data3d.cols.p4[nrow] = -p4
    data3d.cols.p5[nrow] = -p5

def normalize(V):
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
    print frame_and_dir_list
    bad_idx=list(getnan(frame_and_dir_list[:,1])[0])
    good_idx = [i for i in range(len(frame_and_dir_list[:,1])) if i not in bad_idx]
    frame_and_dir_list = [ frame_and_dir_list[i] for i in good_idx]
    
    prev_frame = frame_and_dir_list[0][0]
    prev_dir = normalize(frame_and_dir_list[0][1:4])
    
    cos_90 = math.cos(math.pi/4)
    frames_flipped = []
    
    for frame_and_dir in frame_and_dir_list[1:]:
        this_frame = frame_and_dir[0]
        this_dir = normalize(frame_and_dir[1:4])

        try:
            cos_theta = nx.dot(this_dir, prev_dir)
        except Exception,x:
            print 'x'
            raise
        except:
            print 'hmm'
            raise
        theta_deg = math.acos(cos_theta)/math.pi*180
        
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

def time2frame(results,time_double,typ='best'):
    assert type(time_double)==float

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
    return nx.array(results.root.calibration.pmat.__getattr__(cam_id))

def get_resolution(results,cam_id):
    return tuple(results.root.calibration.resolution.__getattr__(cam_id))

def get_frames_with_3d(results):
    table = results.root.data3d_best
    return [x['frame'] for x in table]

def redo_3d_calc(results,frame,reconstructor=None,verify=True,overwrite=False):
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

    # camera info
    cam_info = results.root.cam_info
    cam_id2camns = {}
    camn2cam_id = {}
    
    for row in cam_info:
        cam_id, camn = row['cam_id'], row['camn']
        cam_id2camns.setdefault(cam_id,[]).append(camn)
        camn2cam_id[camn]=cam_id

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

def plot_whole_movie_3d(results, typ='best', show_err=False):
    ioff()
    
    if typ == 'fast':
        data3d = results.root.data3d_fast
    elif typ == 'best':
        data3d = results.root.data3d_best

    f = nx.array(data3d.cols.frame)
    x = nx.array(data3d.cols.x)
    y = nx.array(data3d.cols.y)
    z = nx.array(data3d.cols.z)
    if show_err:
        err = nx.array(data3d.cols.mean_dist)
    
    # plot it!
    ax = pylab.axes()
    ax.plot(f,x,'r-')
    ax.plot(f,y,'g-')
    ax.plot(f,z,'b-')
    if show_err:
        ax.plot(f,err,'k.')
    set(ax,'ylim',[-1000,1000])
    ##ax.title(typ+' data')
    ##ax.xlabel('frame number')
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

def make_exact_movie_info(results):
    status('making exact movie info')
    exact_movie_info = None # defer making table until we have a connection
    
    movie_info = results.root.movie_info
    data2d = results.root.data2d
    cam_info = results.root.cam_info

    camn2cam_id = {}
    for row in cam_info:
        cam_id, camn = row['cam_id'], row['camn']
        camn2cam_id[camn]=cam_id
    
    for row in movie_info:
        cam_id = row['cam_id']
        filename = row['filename']
        print 'filename',filename
        frame_server = get_server(cam_id)
        try:
            frame_server.load( filename )
        except IOError,x:
            status('WARNING: IOerror %s, skipping %s %s'%(str(x),cam_id,filename))
            continue

        timestamp_movie_start = frame_server.get_timestamp( 0 )
        timestamp_movie_stop = frame_server.get_timestamp( -1 )
        status(' for %s %s:'%(cam_id,filename))
        status('  %s %s'%(repr(timestamp_movie_start),repr(timestamp_movie_stop)))
        camn_start_frame_list = [(x['camn'],x['frame']) for x in data2d
                                 if x['timestamp'] == timestamp_movie_start ]
##        camn_start_frame_list = [(x['camn'],x['frame']) for x in data2d.where(
##            data2d.cols.timestamp == timestamp_movie_start )]
        if len(camn_start_frame_list) == 0:
            status('WARNING: movie for %s %s : start data not found'%(cam_id,filename))
            #ts = nx.array(data2d.cols.timestamp)
            #print 'min(ts),timestamp_movie_start,max(ts)',min(ts),timestamp_movie_start,max(ts)
            continue
        else:
            if len(camn_start_frame_list) > 1:
                for camn, start_frame in camn_start_frame_list:
                    if camn2cam_id[camn] == cam_id:
                        break
            else:
                camn, start_frame = camn_start_frame_list[0]
            assert camn2cam_id[camn] == cam_id
        camn_stop_frame_list = [x['frame'] for x in data2d
                                if x['timestamp'] == timestamp_movie_stop ]
##        camn_stop_frame_list = [x['frame'] for x in data2d.where(
##            data2d.cols.timestamp == timestamp_movie_stop )]
        if len(camn_stop_frame_list) == 0:
            status('WARNING: movie for %s %s : stop data not found in data2d, using last data2d as stop point'%(cam_id,filename))
            camn_frame_list = [x['frame'] for x in data2d
                               if x['timestamp'] >= timestamp_movie_start ]
            stop_frame = max(camn_frame_list)
        else:
            stop_frame = camn_stop_frame_list[0]
            
        if exact_movie_info is None:
            exact_movie_info = results.createTable(results.root,'exact_movie_info',ExactMovieInfo,'')
            
        exact_movie_info.row['cam_id']=cam_id
        exact_movie_info.row['filename']=filename
        exact_movie_info.row['start_frame']=start_frame
        exact_movie_info.row['stop_frame']=stop_frame
        exact_movie_info.row.append()
        exact_movie_info.flush()
    
def get_movie_frame(results, frame_no, cam, frame_server_dict=None):
    cam_info = results.root.cam_info
    movie_info = results.root.movie_info
    data2d = results.root.data2d
    data3d = results.root.data3d_best

    if not hasattr(results.root,'exact_movie_info'):
        make_exact_movie_info(results)

    exact_movie_info = results.root.exact_movie_info

    if type(cam) == int: # camn
        camn = cam
##        cam_id = [x['cam_id'] for x in cam_info.where( cam_info.cols.camn == camn) ][0]
        cam_id = [x['cam_id'] for x in cam_info if x['camn'] == camn ][0]
    elif type(cam) == str: # cam_id
        cam_id = cam
        
    if frame_server_dict is None:
        frame_server = get_server(cam_id)
    else:
        frame_server = frame_server_dict[cam_id]

    found = False
    for row in exact_movie_info:
        if row['cam_id'] == cam_id:
            if row['start_frame'] < frame_no < row['stop_frame']:
                filename = row['filename']
                frame_offset = row['start_frame']
                found = True
                break
    if not found:
        raise ValueError('movie not found for %s'%(cam_id,))
        
    frame_server.load( filename )

    movie_frame = frame_no - frame_offset
    frame, movie_timestamp = frame_server.get_frame( movie_frame )
    return frame, movie_timestamp

def get_cam_ids(results):
    cam_info = results.root.cam_info
    cam_ids=list(sets.Set(cam_info.cols.cam_id))
    cam_ids.sort()
    return cam_ids

def recompute_3d_from_2d(results,
                         overwrite=False,
                         hypothesis_test=True, # discards camns_used
                         typ='best',
                         start_stop=None, # used for hypothesis_test
                         ):
    import flydra.reconstruct
    reconstructor = flydra.reconstruct.Reconstructor(results)

    if typ == 'fast':
        data3d = results.root.data3d_fast
    elif typ == 'best':
        data3d = results.root.data3d_best

    data2d = results.root.data2d
    cam_info = results.root.cam_info

    camn2cam_id = {}
    for row in cam_info:
        cam_id, camn = row['cam_id'], row['camn']
        camn2cam_id[camn]=cam_id
        
    max_n_cameras = len(reconstructor.cam_combinations[0])
    
    if not hypothesis_test:
        print len(data3d),'rows to be processed'
        count = 0
        for row in data3d:
            if count%100==0:
                print 'processing row',count
            count += 1
            camns_used = map(int,row['camns_used'].split())
            if not len(camns_used):
                continue
            nrow = row.nrow()
            frame_no = row['frame']
            d2 = {}
            for x in data2d.where( data2d.cols.frame == frame_no ):
                camn = x['camn']
                if camn in camns_used:
                    cam_id = camn2cam_id[camn]
                    d2[cam_id] = (x['x'], x['y'], x['area'], x['slope'],
                                  x['eccentricity'], x['p1'], x['p2'],
                                  x['p3'], x['p4'])

            X, line3d = reconstructor.find3d(d2.items())
            if overwrite:
                new_row = []
                for colname in data3d.colnames:
                    if colname == 'x': value = X[0]
                    elif colname == 'y': value = X[1]
                    elif colname == 'z': value = X[2]
                    else: value = row[colname]
                    if line3d is not None:
                        if   colname == 'p0': value = line3d[0]
                        elif colname == 'p1': value = line3d[1]
                        elif colname == 'p2': value = line3d[2]
                        elif colname == 'p3': value = line3d[3]
                        elif colname == 'p4': value = line3d[4]
                        elif colname == 'p5': value = line3d[5]
                    new_row.append( value )
                data3d[nrow] = new_row

    else: # do hypothesis testing
        start_frame, stop_frame = start_stop

        
        print stop_frame-start_frame+1,'rows to be processed'
        for frame_no in range(start_frame,stop_frame+1):
            print 'frame_no',frame_no
            # load all 2D data
            d2 = {}
            cam_id2camn = {} # must be recomputed each frame
            for x in data2d:
                if x['frame'] == frame_no :
                    camn = x['camn']
                    cam_id = camn2cam_id[camn]
                    cam_id2camn[cam_id] = camn
                    d2[cam_id] = (x['x'], x['y'], x['area'], x['slope'],
                                  x['eccentricity'], x['p1'], x['p2'],
                                  x['p3'], x['p4'])

            # do hypothestis testing
            least_err_by_n_cameras = {}
            for cam_ids_used in reconstructor.cam_combinations:
                # make sure we have all data to test this combination:
                if len(sets.Set(cam_ids_used) - sets.Set(d2.keys())) > 0:
                    continue
                
                d3 = {}
                missing_cam_data = False
                for cam_id in cam_ids_used:
                    values = d2[cam_id]
                    if len( getnan(values[:2])[0] ):
                        missing_cam_data = True
                        break
                    d3[cam_id] = values
                if missing_cam_data:
                    continue
                
                camns_used = [cam_id2camn[cam_id] for cam_id in cam_ids_used]

                n_cams = len(d3)
                
                # do 3d math
                X, line3d = reconstructor.find3d(d3.items())

                mean_dist = 0.0
                alpha = 1.0/n_cams
                for cam_id in d3.iterkeys():
                    orig_x, orig_y = d3[cam_id][:2]
                    recon_x, recon_y = reconstructor.find2d(cam_id,X)

                    dist = math.sqrt((orig_x-recon_x)**2 + (orig_y-recon_y)**2)
                    mean_dist += dist*alpha

                #print '  ',cam_ids_used, mean_dist

                least_err, tmp = least_err_by_n_cameras.get(n_cams,(inf,None))
                if mean_dist < least_err:
                    least_err_by_n_cameras[n_cams] = mean_dist, (X, line3d, camns_used)

            #for n_cams, (dist, tmp) in least_err_by_n_cameras.iteritems():
            #    print '     ',n_cams, dist

            n_cams_list = least_err_by_n_cameras.keys()
            n_cams_list.sort()

            X, line3d, camns_used = None, None, []
            for n_cams in n_cams_list:
                least_err, tmp = least_err_by_n_cameras[n_cams]
                if least_err < REFINE_THRESH:
                    mean_dist = least_err
                    X, line3d, camns_used = tmp

            #print '        chose mean_dist',mean_dist
            # Now we have the best possible data
            
            if overwrite:
                # find old row
                old_nrow = None
                for row in data3d.where( data3d.cols.frame == frame_no ):
                    if old_nrow is not None:
                        raise RuntimeError('more than row with frame number %d in data3d'%frame_no)
                    old_nrow = row.nrow()
                    
                # delete old row
                if old_nrow is not None:
                    data3d.removeRows(start=old_nrow,stop=None)
                
                # insert new row if it meets threshold distance
                if line3d is None:
                    line3d = [nan]*6 # fill with nans
                cam_nos_used_str = ' '.join( map(str, camns_used) )
                new_row = data3d.row
                new_row['frame'] = frame_no
                new_row['x'] = X[0]
                new_row['y'] = X[1]
                new_row['z'] = X[2]
                new_row['p0'] = line3d[0]
                new_row['p1'] = line3d[1]
                new_row['p2'] = line3d[2]
                new_row['p3'] = line3d[3]
                new_row['p4'] = line3d[4]
                new_row['p5'] = line3d[5]
                new_row['timestamp']=0.0
                new_row['camns_used']=cam_nos_used_str
                new_row['mean_dist']=mean_dist
                new_row.append()
                data3d.flush()

def plot_all_images(results,
                    frame_no,
                    show_raw_image=True,
                    zoomed=True,
                    typ='best',
                    PLOT_RED=True,
                    ##recompute_3d=False,
                    frame_server_dict=None,
                    do_undistort=True,
                    fixed_im_centers=None,
                    plot_orientation=True,
                    plot_true_3d_line=False, # show real line3d info (don't adjust to intersect 3d point)
                    plot_3d_unit_vector=True,
                    origin='lower',
                    display_labels=True,
                    colormap='jet'):
    
    if fixed_im_centers is None:
        fixed_im_centers = {}
    ioff()
    import flydra.reconstruct
    reconstructor = flydra.reconstruct.Reconstructor(results)

    if typ == 'fast':
        data3d = results.root.data3d_fast
    elif typ == 'best':
        data3d = results.root.data3d_best

    data2d = results.root.data2d
    cam_info = results.root.cam_info

    if colormap == 'jet':
        cmap = cm.jet
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

    print 'WARNING: not plotting cam2:0'
    cam_ids.remove('cam2:0')

    tmp = [((x['x'],x['y'],x['z']),
            (x['p0'],x['p1'],x['p2'],x['p3'],x['p4'],x['p5']),
            x['camns_used']
            ) for x in data3d.where( data3d.cols.frame == frame_no) ]
    if len(tmp) == 0:
        X, line3d = None, None
        camns_used = ()
    else:
        assert len(tmp)==1
        X, line3d, camns_used = tmp[0]
        camns_used = map(int,camns_used.split())
        
    clf()
    for subplot_number,cam_id in enumerate(cam_ids):
        width, height = reconstructor.get_resolution(cam_id)
        
        i = cam_ids.index(cam_id)
        ax=dougs_subplot(subplot_number)
        set(ax,'frame_on',display_labels)

        have_2d_data = False
        for row in data2d:
            if row['frame'] != frame_no:
                # XXX ARGH!!! some weird bug in my code or pytables??
                # it means I can't do "in kernel", e.g.:
                #        for row in data2d.where( data2d.cols.frame == frame_no ):
                continue
            camn = row['camn']
            if camn2cam_id[camn] == cam_id:
                have_2d_data = True
                x=row['x']
                y=row['y']
                slope=row['slope']
                eccentricity=row['eccentricity']
                remote_timestamp = row['timestamp']
                if len(getnan([x])[0]):
                    have_2d_data = False
                break
        if not have_2d_data:
            camn=None
            x=None
            y=None
            slope=None
            eccentricity=None
            remote_timestamp = None
            
        title_str = cam_id

        have_limit_data = False
        if show_raw_image:
            try:
                im, movie_timestamp = get_movie_frame(results, frame_no, cam_id, frame_server_dict=frame_server_dict)
                have_raw_image = True
            except ValueError,exc:
                print 'WARNING: skipping frame for %s because %s'%(cam_id,str(exc))
                have_raw_image = False
            if have_2d_data and have_raw_image:
                if remote_timestamp != movie_timestamp:
                    print 'Whoa! timestamps are not equal!'
                    print ' XXX may be able to fix display, but not displaying wrong image for now'
                    have_raw_image = False
            if have_raw_image:
                if do_undistort:
                    intrin = reconstructor.get_intrinsic_linear(cam_id)
                    k = reconstructor.get_intrinsic_nonlinear(cam_id)
                    f = intrin[0,0], intrin[1,1] # focal length
                    c = intrin[0,2], intrin[1,2] # camera center
                    im = undistort.rect(im, f=f, c=c, k=k)
                    im = im.astype(nx.UInt8)
                if have_2d_data and zoomed and x>=0:
                    w = 40
                    h = 60
                    xcenter, ycenter = fixed_im_centers.get(
                        cam_id, (x,y))
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
                    have_limit_data = True

                    extent = (xmin,xmax,ymin,ymax)
                    im = im.copy()
                    im_small = im[ymin:ymax,xmin:xmax]
                    im_small = im_small.copy()
                    ax.imshow(im_small,
                              origin=origin,
                              interpolation='nearest',
                              cmap=cmap,
                              extent = extent,
                              )
                    
                else:
                    xmin=0
                    xmax=width-1
                    ymin=0
                    ymax=height-1
                    ax.imshow(im,origin=origin,interpolation='nearest',cmap=cmap)
            else:
                xmin=0
                xmax=width-1
                ymin=0
                ymax=height-1

        if have_2d_data and camn is not None:
                
            # raw 2D
            try:
                if origin == 'upper':
                    lines=ax.plot([x],[height-y],'o')
                else:
                    lines=ax.plot([x],[y],'o')
            except:
                print 'x, y',x, y
                sys.stdout.flush()
                raise
            
            if show_raw_image:
                green = (0,1,0)
                set(lines,'markerfacecolor',None)
                if camn in camns_used:
                    set(lines,'markeredgecolor',green)
                elif camn not in camns_used:
                    set(lines,'markeredgecolor',(0, 0.2, 0))
                    #print 'setting alpha in',cam_id
                    #set(lines,'alpha',0.2)
                set(lines,'markeredgewidth',2.0)

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
                            set(lines,'color',green)
                        elif camn not in camns_used:
                            set(lines,'color',(0, 0.2, 0))
                        #set(lines,'color',green)
                        #set(lines[0],'linewidth',0.8)

        if X is not None:
            if line3d is None:
                x,y=reconstructor.find2d(cam_id,X)
                l3=None
            else:
                if plot_true_3d_line:
                    x,l3=reconstructor.find2d(cam_id,X,line3d)
                else:
                    U = flydra.reconstruct.line_direction(line3d)
                    if plot_3d_unit_vector:
                        x=reconstructor.find2d(cam_id,X)
                        unit_x1, unit_y1=reconstructor.find2d(cam_id,X-5*U)
                        unit_x2, unit_y2=reconstructor.find2d(cam_id,X+5*U)
                    else:
                        line3d_fake = flydra.reconstruct.pluecker_from_verts(X,X+U)
                        x,l3=reconstructor.find2d(cam_id,X,line3d_fake)
                    x,y=x
                
            #near = 10
            if PLOT_RED:
                if origin=='upper':
                    lines=ax.plot([x],[height-y],'o')
                else:
                    lines=ax.plot([x],[y],'o')
                if 1:
                    set(lines,'markerfacecolor',(1,0,0))
                else:
                    set(lines,'markerfacecolor',None)
                    set(lines,'markeredgecolor',(1,0,0))
                    set(lines,'markeredgewidth',2.0)
                
            if PLOT_RED and line3d is not None:
                if plot_orientation:
                    if plot_3d_unit_vector:
                        if origin == 'upper':
                            lines=ax.plot([unit_x1,unit_x2],[height-unit_y1,height-unit_y2],'r-',linewidth=1.5)
                        else:
                            lines=ax.plot([unit_x1,unit_x2],[unit_y1,unit_y2],'r-',linewidth=1.5)
                    else:
                        a,b,c=l3
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
        labels=ax.get_xticklabels()
        set(labels, rotation=90)
        if display_labels:
            title(title_str)
        if have_limit_data:
            ax.xaxis.set_major_locator( LinearLocator(numticks=5) )
            ax.yaxis.set_major_locator( LinearLocator(numticks=5) )
            set(ax,'xlim',[xmin, xmax])
            set(ax,'ylim',[ymin, ymax])
        else:
            margin_pixels = 20
            set(ax,'xlim',[-margin_pixels, width+margin_pixels])
            set(ax,'ylim',[-margin_pixels, height+margin_pixels])
        if not display_labels:
            set(ax,'xticks',[])
            set(ax,'yticks',[])
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

##def recompute_3d_data():
##    import flydra.reconstruct
##    frames = get_frames_with_3d(results)
##    reconstructor = flydra.reconstruct.Reconstructor(results)
##    for frame in frames:
##        verify_3d_calc(results,frame,reconstructor=reconstructor,
##                       verify=False,overwrite=True)

def get_results(filename):
    return PT.openFile(filename,mode='r+')

def save_movie(results):
##    fixed_im_centers = {'cam1:0':(260,304),
##                    'cam2:0':(402,226),
##                    'cam3:0':(236,435),
##                    'cam4:0':(261,432),
##                    'cam5:0':(196,370)}

    cam_ids = get_cam_ids(results,)
    frame_server_dict = {}
    for cam_id in cam_ids:
        print 'getting frame server for',cam_id
        frame_server_dict[cam_id] = get_server(cam_id)
#    for frame in xrange(5813, 5945, 1):
    for frame in xrange(5783, 5945, 1):
#    for frame in xrange(5896,5898):
        clf()
        try:
            plot_all_images(results, frame,
                            frame_server_dict=frame_server_dict,
                            #fixed_im_centers=fixed_im_centers,
                            colormap='grayscale',
                            zoomed=False,
                            plot_orientation=True,
                            origin='upper',
                            display_labels=False,
                            )
            
            fname = 'ori_raw_frame%04d.png'%frame
            #fname = 'raw_frame%04d.png'%frame
            print ' saving',fname
            savefig(fname)
        except Exception, x:
            #print x, str(x)
            raise

def get_data_array(results):
##    import flydra.reconstruct
##    save_ascii_matrix
    
    data3d = results.root.data3d_best

    M = []
    for row in data3d.where( 132700 <= data3d.cols.frame <= 132800 ):
        M.append( (row['frame'], row['x'], row['y'], row['z'] ) )
    M = nx.array(M)
    return M
   
if __name__=='__main__':
    results = get_results('hb124j.h5')
#    recompute_3d_from_2d(results, overwrite=True, start_stop=(5784,5945) )
#    recompute_3d_from_2d(results, start_stop=(5944,5944) )
#    recompute_3d_from_2d(results, start_stop=(5809,5809) )
    if 1:
        save_movie(results)
    if 0:
        import pylab
        plot_whole_movie_3d(results)
        #pylab.figure(2)
        #plot_all_images(results,19783)
        pylab.show()
        raw_input()
