import tables as PT
print 'using pytables',PT.__version__
print '  from',PT.__file__
import numpy as nx
import sys, os
import FlyMovieFormat

import datetime
import pytz # from http://pytz.sourceforge.net/

# should avoid using any matplotlib here -- we want to keep this
# module lean and mean

def status(status_string):
    print " status:",status_string
    sys.stdout.flush()
    
def get_camn(results, cam, remote_timestamp=None, frame=None):
    """helper function to get camn given timestamp or frame number

    last used 2006-05-17
    
    """
    if not isinstance(cam,str):
        camn = cam
        return camn

    cam_id = cam
    possible_camns = []
    for row in results.root.cam_info:
        if row['cam_id'] == cam_id:
                possible_camns.append( row['camn'] )

    table = results.root.data2d_camera_summary
    for row in table.where(table.cols.cam_id==cam_id):
        camn = None
        if row['camn'] in possible_camns:
            if remote_timestamp is not None:
                if row['start_timestamp'] <= remote_timestamp <= row['stop_timestamp']:
                    if camn is not None:
                        if camn != row['camn']:
                            raise RuntimeError('Found camn already! (Is frame from different run than timestamp?)')
                    camn=row['camn']
            if frame is not None:
                if row['start_frame'] <= frame <= row['stop_frame']:
                    if camn is not None:
                        if camn != row['camn']:
                            raise RuntimeError('Found camn already! (Is frame from different run than timestamp?)')
                    camn=row['camn']
    if camn is None:
        raise RuntimeError("could not find frame or timestamp")
    return camn

def get_frame_from_camn_and_timestamp(results, camn, remote_timestamp):
    """helper function

    last used 2006-06-06
    """
    found = False
    data2d = results.root.data2d_distorted
    if PT.__version__ <= '1.3.2':
        #if type(remote_timestamp)==numpy.float64scalar:
        remote_timestamp=float(remote_timestamp)
    for row in data2d.where( data2d.cols.timestamp==remote_timestamp ):
        test_camn = row['camn']
        if test_camn==camn:
            frame = row['frame']
            found = True
            break
    if not found:
        raise ValueError("No data found for cam and remote_timestamp")
    return frame

def get_camn_and_frame(results, cam, remote_timestamp):
    """helper function

    last used 2006-06-06
    """
    camn = get_camn(results, cam, remote_timestamp=remote_timestamp)
    frame = get_frame_from_camn_and_timestamp(results, camn, remote_timestamp)        
    return camn, frame

def get_camn_and_remote_timestamp(results, cam, frame):
    """helper function

    last used 2006-05-08
    """
    camn = get_camn(results, cam, frame=frame)
    found = False
    data2d = results.root.data2d_distorted
    try:
        for row in data2d.where( data2d.cols.frame==frame ):
            test_camn = row['camn']
            if test_camn==camn:
                timestamp = row['timestamp']
                found = True
                break
    except TypeError:
        print 'frame',frame
        print 'repr(frame)',repr(frame)
        print 'type(frame)',type(frame)
        raise
    if not found:
        raise ValueError("No data found for cam and frame")
    return camn, timestamp

def get_cam_ids(results):
    cam_info = results.root.cam_info
    cam_ids=list(sets.Set(cam_info.cols.cam_id))
    cam_ids.sort()
    return cam_ids

def get_caminfo_dicts(results):
    # camera info
    cam_info = results.root.cam_info
    cam_id2camns = {}
    camn2cam_id = {}
    
    for row in cam_info:
        cam_id, camn = row['cam_id'], row['camn']
        cam_id2camns.setdefault(cam_id,[]).append(camn)
        camn2cam_id[camn]=cam_id
    return camn2cam_id, cam_id2camns

def get_results(filename,mode='r+'):
    h5file = PT.openFile(filename,mode=mode)
    if hasattr(h5file.root,'data3d_best'):
        frame_col = h5file.root.data3d_best.cols.frame
        if frame_col.index is None:
            print 'creating index on data3d_best.cols.frame ...'
            frame_col.createIndex()
            print 'done'
        
    if False and hasattr(h5file.root,'data2d'):
        frame_col = h5file.root.data2d.cols.frame
        if frame_col.index is None:
            print 'creating index on data2d.cols.frame ...'
            frame_col.createIndex()
            print 'done'

##        timestamp_col = h5file.root.data2d.cols.timestamp
##        if timestamp_col.index is None:
##            print 'creating index on data2d.cols.timestamp ...'
##            timestamp_col.createIndex()
##            print 'done'

    if hasattr(h5file.root,'data2d_distorted'):
        frame_col = h5file.root.data2d_distorted.cols.frame
        if frame_col.index is None:
            if h5file._isWritable():
                print 'creating index on data2d_distorted.cols.frame ...'
                frame_col.createIndex()
                print 'done'
            else:
                print 'WARNING: file is not writable and cannot create index - some operations may be very slow'

##        timestamp_col = h5file.root.data2d_distorted.cols.timestamp
##        if timestamp_col.index is None:
##            print 'creating index on data2d_distorted.cols.timestamp ...'
##            timestamp_col.createIndex()
##            print 'done'

        if not hasattr(h5file.root,'data2d_camera_summary') and h5file._isWritable():
            print 'creating data2d camera summary ...'
            create_data2d_camera_summary(h5file)
            print 'done'
    return h5file

def get_f_xyz_L_err( results, max_err = 10, typ = 'best', include_timestamps=False):
    """workhorse function to get 3D data from file

    returns:
    (f,X,L,err)
    if include_timestamps is True:
    (f,X,L,err,timestamps)
    
    where:
    f is frame numbers
    X is 3D position coordinates
    L is Pluecker line coordinates
    err is mean reprojection distance
    timestamps are the timestamps on the 3D reconstruction computer

    last used 2006-05-16
    """
    if typ == 'fast':
        data3d = results.root.data3d_fast
    elif typ == 'best':
        data3d = results.root.data3d_best

    if max_err is not None:
        f = []
        x = []
        y = []
        z = []
        xyz=[]
        L = []
        err = []
        timestamps = []
        for row in data3d.where( data3d.cols.mean_dist <= max_err ):
            f.append( row['frame'] )
            xyz.append( (row['x'],row['y'],row['z']) )
            L.append( (row['p0'],row['p1'],
                       row['p2'],row['p3'],
                       row['p4'],row['p5']))
            err.append( row['mean_dist'] )
            timestamps.append( row['timestamp'] )
        f = nx.array(f)
        xyz = nx.array(xyz)
        L = nx.array(L)
        err = nx.array(err)
        timestamps = nx.array(timestamps)
    else:
        frame_col = data3d.cols.frame
        if not len(frame_col):
            print 'no 3D data'
            return
        f = nx.array(frame_col)
        timestamps = nx.array(timestamps)
        x = nx.array(data3d.cols.x)
        y = nx.array(data3d.cols.y)
        z = nx.array(data3d.cols.z)
        xyz = nx.concatenate( (x[:,nx.NewAxis],
                               y[:,nx.NewAxis],
                               z[:,nx.NewAxis]),
                              axis = 1)
        p0 = nx.array(data3d.cols.p0)[:,nx.NewAxis]
        p1 = nx.array(data3d.cols.p1)[:,nx.NewAxis]
        p2 = nx.array(data3d.cols.p2)[:,nx.NewAxis]
        p3 = nx.array(data3d.cols.p3)[:,nx.NewAxis]
        p4 = nx.array(data3d.cols.p4)[:,nx.NewAxis]
        p5 = nx.array(data3d.cols.p5)[:,nx.NewAxis]
        L = nx.concatenate( (p0,p1,p2,p3,p4,p5), axis=1 )
        err = nx.array(data3d.cols.mean_dist)

    if hasattr(results.root,'ignore_frames'):
        good = nx.argsort(f)
        good_set = sets.Set( nx.argsort( f ) )
        for row in results.root.ignore_frames:
            start_frame, stop_frame = row['start_frame'], row['stop_frame']
            head = nx.where( f < start_frame )
            tail = nx.where( f > stop_frame )
            head_set = sets.Set(head[0])
            tail_set = sets.Set(tail[0])

            good_set = (good_set & head_set) | (good_set & tail_set)
        good_idx = list( good_set )
        good_idx.sort()
    else:
        good_idx = nx.argsort(f)
        
    f = nx.take(f,good_idx,axis=0)
    xyz = nx.take(xyz,good_idx,axis=0)
    L = nx.take(L,good_idx,axis=0)
    err = nx.take(err,good_idx,axis=0)
    timestamps = nx.take(timestamps,good_idx,axis=0)

    rval = [f,xyz,L,err]
    if include_timestamps:
        rval.append( timestamps )
    return tuple(rval)

def get_reconstructor(results):
    import flydra.reconstruct
    return flydra.reconstruct.Reconstructor(results)

def get_resolution(results,cam_id):
    return tuple(results.root.calibration.resolution.__getattr__(cam_id))

def create_data2d_camera_summary(results):

    class Data2DCameraSummary(PT.IsDescription):
        cam_id             = PT.StringCol(16,pos=0)
        camn               = PT.Int32Col(pos=1)
        start_frame        = PT.Int32Col(pos=2)
        stop_frame         = PT.Int32Col(pos=3)
        start_timestamp    = PT.FloatCol(pos=4)
        stop_timestamp     = PT.FloatCol(pos=5)
    
    data2d = results.root.data2d_distorted # make sure we have 2d data table
    camn2cam_id, cam_id2camns = get_caminfo_dicts(results)
    table = results.createTable( results.root, 'data2d_camera_summary',
                                 Data2DCameraSummary, 'data2d camera summary' )
    for camn in camn2cam_id:
        cam_id = camn2cam_id[camn]
        print 'creating 2d camera index for camn %d, cam_id %s'%(camn,cam_id)

        first_row = True
        for row_data2d in data2d.where( data2d.cols.camn == camn ):
            ts = row_data2d['timestamp']
            f = row_data2d['frame']
            if first_row:
                start_timestamp = ts; stop_timestamp = ts
                start_frame = f;      stop_frame = f
                first_row = False
            start_timestamp = min(start_timestamp,ts)
            stop_timestamp = max(stop_timestamp,ts)
            start_frame = min(start_frame,f)
            stop_frame = max(stop_frame,f)
        newrow = table.row
        newrow['cam_id'] = cam_id
        newrow['camn'] = camn
        newrow['start_frame']=start_frame
        newrow['stop_frame']=stop_frame
        newrow['start_timestamp']=start_timestamp
        newrow['stop_timestamp']=stop_timestamp
        newrow.append()
    table.flush()

def timestamp2string(ts_float,timezone='US/Pacific'):
    pacific = pytz.timezone(timezone)
    dt_ts = datetime.datetime.fromtimestamp(ts_float,pacific)
    # dt_ts.ctime()
    return dt_ts.isoformat()

def make_exact_movie_info2(results,movie_dir=None):
    
    class ExactMovieInfo(PT.IsDescription):
        cam_id             = PT.StringCol(16,pos=0)
        filename           = PT.StringCol(255,pos=1)
        start_frame        = PT.Int32Col(pos=2)
        stop_frame         = PT.Int32Col(pos=3)
        start_timestamp    = PT.FloatCol(pos=4)
        stop_timestamp     = PT.FloatCol(pos=5)
    
    status('making exact movie info')
    
    movie_info = results.root.movie_info
    data2d = results.root.data2d_distorted
    cam_info = results.root.cam_info

    camn2cam_id = {}
    for row in cam_info:
        cam_id, camn = row['cam_id'], row['camn']
        camn2cam_id[camn]=cam_id

    exact_movie_info = results.createTable(results.root,'exact_movie_info',ExactMovieInfo,'')
    
    for row in movie_info:
        cam_id = row['cam_id']
        filename = row['filename']
        print 'filename1:',filename
        if movie_dir is None:
            computer_name = cam_id.split(':')[0]
            filename = filename.replace('local',computer_name)
        else:
            filename = os.path.join(movie_dir,os.path.split(filename)[-1])
        print 'filename2:',filename
        frame_server = FlyMovieFormat.FlyMovie(filename,check_integrity=True)
        status(' for %s %s:'%(cam_id,filename))
        tmp_frame, timestamp_movie_start = frame_server.get_frame( 0 )
        tmp_frame, timestamp_movie_stop = frame_server.get_frame( -1 )
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
            
        exact_movie_info.row['cam_id']=cam_id
        exact_movie_info.row['filename']=filename
        exact_movie_info.row['start_frame']=start_frame
        exact_movie_info.row['stop_frame']=stop_frame
        exact_movie_info.row['start_timestamp']=timestamp_movie_start
        exact_movie_info.row['stop_timestamp']=timestamp_movie_stop
        exact_movie_info.row.append()
        exact_movie_info.flush()
