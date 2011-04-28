if 1:
    # deal with old files, forcing to numpy
    import tables.flavor
    tables.flavor.restrict_flavors(keep=['numpy'])

import numpy
import sys, os, time
import flydra.a2.core_analysis as core_analysis
from optparse import OptionParser
import flydra.analysis.flydra_analysis_convert_to_mat
import tables
import flydra.analysis.result_utils as result_utils
import flydra.a2.utils as utils
import warnings

def cam_id2hostname(cam_id):
    hostname = '_'.join(   cam_id.split('_')[:-1] )
    return hostname

def convert(infilename,
            outfilename,
            frames_per_second=None,
            save_timestamps=True,
            file_time_data=None,
            do_nothing=False, # set to true to test for file existance
            start_obj_id=None,
            stop_obj_id=None,
            dynamic_model_name=None,
            min_ori_quality_required=None,
            ori_quality_smooth_len=10,
            velocity_weight_gain=None,
            max_velocity_weight=None,
            elevation_up_bias_degrees=None,
            ):
    if start_obj_id is None:
        start_obj_id=-numpy.inf
    if stop_obj_id is None:
        stop_obj_id=numpy.inf

    if save_timestamps:
        print 'STAGE 1: finding timestamps'
        print 'opening file %s...'%infilename

        h5file_raw = tables.openFile(infilename,mode='r')
        table_kobs   = h5file_raw.root.kalman_observations # table to get framenumbers from
        kobs_2d = h5file_raw.root.kalman_observations_2d_idxs # VLArray linking two

        if file_time_data is None:
            h52d = h5file_raw
            close_h52d = False
        else:
            h52d = tables.openFile(file_time_data,mode='r')
            close_h52d = True

        try:
            table_data2d = h52d.root.data2d_distorted # Table to get timestamps from. (If you don't have timestamps, use the '--no-timestamps' option.)
        except tables.exceptions.NoSuchNodeError, err:
            print >> sys.stderr, "No timestamps in file. Either specify not to save timestamps ('--no-timestamps') or specify the original .h5 file with the timestamps ('--time-data=FILE2D')"
            sys.exit(1)

        print 'caching raw 2D data...',
        sys.stdout.flush()
        table_data2d_frames = table_data2d.read(field='frame').astype(numpy.uint64) # cast to uint64 for fast searching
        #table_data2d_frames_find = fastsearch.binarysearch.BinarySearcher( table_data2d_frames )
        table_data2d_frames_find = utils.FastFinder( table_data2d_frames )
        table_data2d_camns = table_data2d.read(field='camn')
        table_data2d_timestamps = table_data2d.read(field='timestamp')
        print 'done'
        print '(cached index of %d frame values of dtype %s)'%(len(table_data2d_frames),str(table_data2d_frames.dtype))

        drift_estimates = result_utils.drift_estimates( h52d )
        camn2cam_id, cam_id2camns = result_utils.get_caminfo_dicts(h52d)

        hostnames = drift_estimates['hostnames']
        gain = {}; offset = {};
        print 'hostname time_gain time_offset'
        print '-------- --------- -----------'
        for i,hostname in enumerate(hostnames):
            tgain, toffset = result_utils.model_remote_to_local(
                drift_estimates['remote_timestamp'][hostname][::10],
                drift_estimates['local_timestamp'][hostname][::10])
            gain[hostname]=tgain
            offset[hostname]=toffset
            print '  ',repr(hostname),tgain,toffset
        print

        if do_nothing:
            h5file_raw.close()
            if close_h52d:
                h52d.close()
            return

        print 'caching Kalman obj_ids...'
        obs_obj_ids = table_kobs.read(field='obj_id')
        print 'finding unique obj_ids...'
        unique_obj_ids = numpy.unique(obs_obj_ids)
        print '(found %d)'%(len(unique_obj_ids),)
        unique_obj_ids = unique_obj_ids[ unique_obj_ids >= start_obj_id ]
        unique_obj_ids = unique_obj_ids[ unique_obj_ids <= stop_obj_id ]
        print '(will export %d)'%(len(unique_obj_ids),)
        print 'finding 2d data for each obj_id...'
        timestamp_time = numpy.zeros( unique_obj_ids.shape, dtype=numpy.float64)
        table_kobs_frame = table_kobs.read(field='frame')
        assert table_kobs_frame.dtype == table_data2d_frames.dtype # otherwise very slow

        for obj_id_enum,obj_id in enumerate(unique_obj_ids):
            if obj_id_enum%100==0:
                print '%d of %d'%(obj_id_enum,len(unique_obj_ids))
            valid_cond = obs_obj_ids == obj_id
            idxs = numpy.nonzero(valid_cond)[0]
            idx0 = numpy.min(idxs)

            framenumber = table_kobs_frame[idx0]
            remote_timestamp = numpy.nan

            this_camn = None
            frame_idxs = table_data2d_frames_find.get_idxs_of_equal(framenumber)
            if len(frame_idxs):
                frame_idx = frame_idxs[0]
                this_camn = table_data2d_camns[frame_idx]
                remote_timestamp = table_data2d_timestamps[frame_idx]

            if this_camn is None:
                print 'skipping frame %d (obj %d): no data2d_distorted data'%(framenumber,obj_id)
                continue

            cam_id = camn2cam_id[this_camn]
            remote_hostname = cam_id2hostname(cam_id)
            mainbrain_timestamp = remote_timestamp*gain[remote_hostname] + offset[remote_hostname] # find mainbrain timestamp

            timestamp_time[obj_id_enum] = mainbrain_timestamp
            if obj_id_enum%100==0:
                try:
                    print time.asctime(time.localtime(mainbrain_timestamp))
                except:
                    print '** no timestamp **'
                print

        h5file_raw.close()
        if close_h52d:
            h52d.close()

        extra_vars = {'obj_ids':unique_obj_ids,
                      'timestamps':timestamp_time,
                      }
        print 'STAGE 2: running Kalman smoothing operation'
    else:
        extra_vars = None

    ca = core_analysis.get_global_CachingAnalyzer()
    all_obj_ids, obj_ids, is_mat_file, data_file, extra = ca.initial_file_load(infilename)
    obj_ids = obj_ids[ obj_ids >= start_obj_id ]
    obj_ids = obj_ids[ obj_ids <= stop_obj_id ]
    if frames_per_second is None:
        frames_per_second = extra['frames_per_second']
    if dynamic_model_name is None:
        dynamic_model_name = extra.get('dynamic_model_name',None)
        if dynamic_model_name is None:
            dynamic_model_name = 'fly dynamics, high precision calibration, units: mm'
            warnings.warn('no dynamic model specified, using "%s"'%dynamic_model_name)
        else:
            print 'detected file loaded with dynamic model "%s"'%dynamic_model_name
        if dynamic_model_name.startswith('EKF '):
            dynamic_model_name = dynamic_model_name[4:]
        print '  for smoothing, will use dynamic model "%s"'%dynamic_model_name

    allrows = []
    for i,obj_id in enumerate(obj_ids):
        if obj_id > stop_obj_id:
            break
        if i%100 == 0:
            print '%d of %d'%(i,len(obj_ids))
        try:
            rows = ca.load_data(obj_id,
                                infilename,
                                dynamic_model_name=dynamic_model_name,
                                frames_per_second=frames_per_second,
                                min_ori_quality_required=min_ori_quality_required,
                                ori_quality_smooth_len=ori_quality_smooth_len,
                                velocity_weight_gain=velocity_weight_gain,
                                max_velocity_weight=max_velocity_weight,
                                elevation_up_bias_degrees=elevation_up_bias_degrees,
                                )
        except core_analysis.NotEnoughDataToSmoothError:
            warnings.warn('not enough data to smooth obj_id %d, skipping.'%(obj_id,))
            continue
        allrows.append(rows)

    allrows = numpy.concatenate( allrows )
    recarray = numpy.rec.array(allrows)

    flydra.analysis.flydra_analysis_convert_to_mat.do_it(
        rows=recarray,
        ignore_observations=True,
        newfilename=outfilename,
        extra_vars=extra_vars,
        )
    ca.close()

def main():
    usage = '%prog FILE [options]'
    parser = OptionParser(usage)
    parser.add_option("--dest-file", type='string', default=None,
                      help="save to mat file")
    parser.add_option("--time-data", dest="file2d", type='string',
                      help="hdf5 file with 2d data FILE2D used to calculate timestamp information",
                      metavar="FILE2D")
    parser.add_option("--no-timestamps",action='store_true',dest='no_timestamps',default=False)
    parser.add_option("--start-obj-id",default=None,type='int',help='last obj_id to save')
    parser.add_option("--stop-obj-id",default=None,type='int',help='last obj_id to save')
    parser.add_option("--stop",default=None,type='int',help='last obj_id to save (DEPRECATED)')
    parser.add_option("--profile",action='store_true',dest='profile',default=False)
    parser.add_option("--dynamic-model",
                      type="string",
                      dest="dynamic_model",
                      default=None,
                      )
    parser.add_option("--velocity-weight-gain",default=None,type='float')
    parser.add_option("--max-velocity-weight",default=None,type='float')
    parser.add_option("--elevation-up-bias-degrees",default=None,type='float')

    parser.add_option("--min-ori-quality-required",default=None,type='float',
                      help='minimum orientation quality required to emit 3D orientation info')
    parser.add_option("--ori-quality-smooth-len",default=10,type='int',
                      help='smoothing length of trajectory')

    (options, args) = parser.parse_args()

    if len(args)>1:
        print >> sys.stderr,  "arguments interpreted as FILE supplied more than once"
        parser.print_help()
        return

    if len(args)<1:
        parser.print_help()
        return

    if options.stop_obj_id is not None and options.stop is not None:
        raise ValueError('--stop and --stop-obj-id cannot both be set')

    if options.stop is not None:
        warnings.warn('DeprecationWarning: --stop will be phased out in favor of --stop-obj-id')
        options.stop_obj_id = options.stop

    infilename = args[0]
    if options.dest_file is None:
        outfilename = os.path.splitext(infilename)[0] + '_smoothed.mat'
    else:
        outfilename = options.dest_file
    cmd_str = """convert(infilename,outfilename,
                       file_time_data=options.file2d,
                       save_timestamps = not options.no_timestamps,
                       start_obj_id=options.start_obj_id,
                       stop_obj_id=options.stop_obj_id,
                       dynamic_model_name=options.dynamic_model,
                       min_ori_quality_required=options.min_ori_quality_required,
                       ori_quality_smooth_len=options.ori_quality_smooth_len,
                       velocity_weight_gain=options.velocity_weight_gain,
                       max_velocity_weight=options.max_velocity_weight,
                       elevation_up_bias_degrees=options.elevation_up_bias_degrees,
                       )"""
    if options.profile:
        import cProfile
        import lsprofcalltree
        p = cProfile.Profile()

        print 'PROFILING'
        p.runctx(cmd_str,globals(),locals())
        k = lsprofcalltree.KCacheGrind(p)
        data = open('data2smoothed.prof',mode='wb')
        k.output(data)
        data.close()
    else:
        exec(cmd_str)

if __name__=='__main__':
    main()
