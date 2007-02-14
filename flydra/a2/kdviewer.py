import sets
from enthought.tvtk.api import tvtk
import numpy
import tables as PT
import sys
from optparse import OptionParser
import detect_saccades
import stimulus_positions

#IVTK= True
IVTK= False
RIBEXPORT=False
if IVTK:
    from enthought.tvtk.tools import ivtk

def print_cam_props(camera):
    print 'camera.parallel_projection = ',camera.parallel_projection
    print 'camera.focal_point = ',camera.focal_point
    print 'camera.position = ',camera.position
    print 'camera.view_angle = ',camera.view_angle
    print 'camera.view_up = ',camera.view_up
    print 'camera.clipping_range = ',camera.clipping_range
    print 'camera.parallel_scale = ',camera.parallel_scale

def doit(filename,
         show_obj_ids=False,
         obj_start=None,
         obj_end=None,
         obj_only=None,
         show_n_longest=None,
         radius=0.002, # in meters
         min_length=10,
         show_saccades = True,
         show_observations = False,
         show_saccade_times = False,
         stim = None,
         quick_kalman=False,
         fps=100.0,
         ):
    
    kresults = PT.openFile(filename,mode="r")
    obs_obj_ids = kresults.root.kalman_observations.read(field='obj_id',flavor='numpy')
    use_obj_ids = numpy.unique(obs_obj_ids)
    
    if show_n_longest is not None:
        if ((obj_start is not None) or
            (obj_end is not None) or
            (obj_only is not None)):
            raise ValueError("show_n_longest incompatible with other limiters")
        frames = kresults.root.kalman_observations.read(field='frame',flavor='numpy')
        obj_ids_by_n_frames = {}
        for i,obj_id in enumerate(use_obj_ids):
            if i%100==0:
                print 'doing %d of %d'%(i,len(use_obj_ids))
            obs_cond = obs_obj_ids==obj_id
            obj_frames = frames[obs_cond]
            n_frames = obj_frames[-1]-obj_frames[0]
            obj_ids_by_n_frames.setdefault( n_frames, [] ).append( obj_id )
        n_frames_list = obj_ids_by_n_frames.keys()
        n_frames_list.sort()

        obj_only = []
        while len(n_frames_list):
            n_frames = n_frames_list.pop()
            obj_ids = obj_ids_by_n_frames[n_frames]
            obj_only.extend( obj_ids)
            if len(obj_only) > show_n_longest:
                break

        print 'longest traces = ',obj_only
        use_obj_ids = numpy.array(obj_only)

    if obj_start is not None:
        use_obj_ids = use_obj_ids[use_obj_ids >= obj_start]
    if obj_end is not None:
        use_obj_ids = use_obj_ids[use_obj_ids <= obj_end]

    if obj_only is not None:
        use_obj_ids = numpy.array(obj_only)
##        obs_idx = []
##        for obj_id in use_obj_ids:
##            obs_idx.append( numpy.nonzero(obs_obj_ids==obj_id)[0] )
##        obs_idx.append
##        obs_rows = kresults.root.kalman_observations.readCoordinates(obs_idx,flavor='numpy')

    #################
    rw = tvtk.RenderWindow(size=(600, 600))
    ren = tvtk.Renderer(background=(1.0,1.0,1.0))

    if 1:
        camera = ren.active_camera
        camera.parallel_projection =  0
        camera.focal_point =  (0.52719625417063776, 0.15695605837665305, 0.10876143712478874)
        camera.position =  (0.39743071773877131, -0.4114652255728779, 0.097431169175252269)
        camera.view_angle =  30.0
        camera.view_up =  (-0.072067516965519787, -0.0034285481144054573, 0.99739386305323308)
        camera.clipping_range =  (0.25210456649736646, 1.0012868084455435)
        camera.parallel_scale =  0.294595461395
 
    rw.add_renderer(ren)
    rwi = tvtk.RenderWindowInteractor(render_window=rw)
    
    lut = tvtk.LookupTable(hue_range = (0.667, 0.0))
    max_vel = 0.4
    actors = []
    actor2obj_id = {}
    #################
    
    ca = detect_saccades.CachingAnalyzer()
    for obj_id in use_obj_ids:
        if show_observations:
            obs_idx = numpy.nonzero(obs_obj_ids==obj_id)[0]
            obs_rows = kresults.root.kalman_observations.readCoordinates(obs_idx,flavor='numpy')
            obs_x = obs_rows.field('x')
            obs_y = obs_rows.field('y')
            obs_z = obs_rows.field('z')
            obs_X = numpy.vstack((obs_x,obs_y,obs_z)).T

            pd = tvtk.PolyData()
            pd.points = obs_X

            g = tvtk.Glyph3D(scale_mode='data_scaling_off',
                             vector_mode = 'use_vector',
                             input=pd)
            ss = tvtk.SphereSource(radius = radius/3)
            g.source = ss.output
            vel_mapper = tvtk.PolyDataMapper(input=g.output)
            a = tvtk.Actor(mapper=vel_mapper)
            a.property.color = 1.0, 0.0, 0.0
            actors.append(a)
            actor2obj_id[a] = obj_id

        
        n_observations = numpy.sum(obs_obj_ids == obj_id)
        if int(n_observations) < int(min_length):
            continue
        results = ca.calculate_trajectory_metrics(obj_id,
                                                  kresults,
                                                  quick_kalman=quick_kalman,
                                                  frames_per_second=fps,
                                                  method='position based',
                                                  method_params={'downsample':1,
                                                                 })
        verts = results['X_raw']
        speeds = results['speed_raw']
        
        if show_saccades:
            saccades = ca.detect_saccades(obj_id,
                                          kresults,
                                          quick_kalman=quick_kalman,
                                          frames_per_second=fps,
                                          method='position based',
                                          method_params={'downsample':1,
                                                         'horizontal only':False,
                                                         #'horizontal only':True,
                                                         })
            saccade_verts = saccades['X']
            saccade_times = saccades['times']
        
        #################

        pd = tvtk.PolyData()
        pd.points = verts
        pd.point_data.scalars = speeds
        if numpy.any(speeds>max_vel):
            print 'WARNING: maximum speed (%.3f m/s) exceeds color map max'%(speeds.max(),)

        g = tvtk.Glyph3D(scale_mode='data_scaling_off',
                         vector_mode = 'use_vector',
                         input=pd)
        ss = tvtk.SphereSource(radius = radius)
        g.source = ss.output
        vel_mapper = tvtk.PolyDataMapper(input=g.output)
        vel_mapper.lookup_table = lut
        vel_mapper.scalar_range = 0.0, max_vel
        a = tvtk.Actor(mapper=vel_mapper)
        if show_observations:
            a.property.opacity = 0.3
        actors.append(a)
        actor2obj_id[a] = obj_id

        if 0:
            # show time of each saccade
            for X,showtime in zip(verts,results['time_raw']):
                ta = tvtk.TextActor(input=str( showtime ))
                ta.property.color = 0.0, 0.0, 0.0 # black
                ta.position_coordinate.coordinate_system = 'world'
                ta.position_coordinate.value = tuple(X)
                actors.append(ta)
                actor2obj_id[a] = obj_id

        ##################
    
        if show_saccades:
            pd = tvtk.PolyData()
            pd.points = saccade_verts

            g = tvtk.Glyph3D(scale_mode='data_scaling_off',
                             vector_mode = 'use_vector',
                             input=pd)
            ss = tvtk.SphereSource(radius = 0.005,
                                   theta_resolution=20,
                                   phi_resolution=20,
                                   )
            g.source = ss.output
            mapper = tvtk.PolyDataMapper(input=g.output)
            a = tvtk.Actor(mapper=mapper)
            #a.property.color = (0,1,0) # green
            a.property.color = (0,0,0) # black
            a.property.opacity = 0.3
            actors.append(a)
            actor2obj_id[a] = obj_id

        if show_saccade_times:
            # show time of each saccade
            for X,showtime in zip(saccade_verts,saccade_times):
                ta = tvtk.TextActor(input=str( showtime ))
                ta.property.color = 0.0, 0.0, 0.0 # black
                ta.position_coordinate.coordinate_system = 'world'
                ta.position_coordinate.value = tuple(X)
                actors.append(ta)
                actor2obj_id[a] = obj_id

    kresults.close()
    
    ################################
      
    if stim is not None:
        all_verts = stimulus_positions.stim_positions[stim]

        for verts in all_verts:

            verts = numpy.asarray(verts)

            pd = tvtk.PolyData()

            np = len(verts) - 1
            lines = numpy.zeros((np, 2), 'l')
            lines[:,0] = numpy.arange(0, np-0.5, 1, 'l')
            lines[:,1] = numpy.arange(1, np+0.5, 1, 'l')

            pd.points = verts
            pd.lines = lines

            pt = tvtk.TubeFilter(radius=0.006,input=pd,
                                 number_of_sides=20,
                                 vary_radius='vary_radius_off',
                                 )
            m = tvtk.PolyDataMapper(input=pt.output)
            a = tvtk.Actor(mapper=m)
            a.property.color = 0,0,0
            a.property.specular = 0.3
            actors.append(a)
            
    for a in actors:
        ren.add_actor(a)
        
    if 1:
        # Create a scalar bar
 	scalar_bar = tvtk.ScalarBarActor(title="Speed (m/s)",
 	                                 orientation='horizontal',
                                         width=0.4, height=0.08,
#                                         width=0.8, height=0.17,
 	                                 lookup_table = vel_mapper.lookup_table)
        
        scalar_bar.property.color = 0.0, 0.0, 0.0 # black

        scalar_bar.title_text_property.color = 0.0, 0.0, 0.0
        scalar_bar.title_text_property.shadow = False
        
        scalar_bar.label_text_property.color = 0.0, 0.0, 0.0
        scalar_bar.label_text_property.shadow = False
        
 	scalar_bar.position_coordinate.coordinate_system = 'normalized_viewport'
 	scalar_bar.position_coordinate.value = 0.1, 0.01, 0.0
        
        if 1:
            # Use the ScalarBarWidget so we can drag the scalar bar around.
            sc_bar_widget = tvtk.ScalarBarWidget(interactor=rwi,
                                                 scalar_bar_actor=scalar_bar)


            rwi.initialize()
            sc_bar_widget.enabled = True

            #rwi.interactor_style = tvtk.InteractorStyleSwitch() # doesn't work??
            if 1:
                picker = tvtk.CellPicker(tolerance=1e-9)
                #print 'dir(picker)',dir(picker)
                def annotatePick(object, event):
                    if not picker.cell_id < 0:
                        found = sets.Set([])
                        for actor in picker.actors:
                            objid = actor2obj_id[actor]
                            found.add(objid)
                        found = list(found)
                        found.sort()
                        print ' '.join(map(str,found))
                
                picker.add_observer('EndPickEvent', annotatePick)
                rwi.picker = picker
            
            rwi.start()
            print_cam_props( ren.active_camera )
            
def main():
    usage = '%prog FILE [options]'
    
    parser = OptionParser(usage)
    
    parser.add_option("-f", "--file", dest="filename", type='string',
                      help="hdf5 file with data to display FILE",
                      metavar="FILE")

##    parser.add_option("--debug", type="int",
##                      help="debug level",
##                      metavar="DEBUG")
        
    parser.add_option("--start", type="int",
                      help="first object ID to plot",
                      metavar="START")
        
    parser.add_option("--stop", type="int",
                      help="last object ID to plot",
                      metavar="STOP")

    parser.add_option("--obj-only", type="string",
                      dest="obj_only")
    
    parser.add_option("--stim", type="string",
                      dest="stim")
    
    parser.add_option("--n-top-traces", type="int",
                      help="show N longest traces")
    
    parser.add_option("--min-length", dest="min_length", type="int",
                      help="minimum number of tracked points (not observations!) required to plot",
                      default=10,)
    
    parser.add_option("--radius", type="float",
                      help="radius of line (in meters)",
                      default=0.002,
                      metavar="RADIUS")
    
    parser.add_option("--show-obj-ids", action='store_true',dest='show_obj_ids',
                      help="show object ID numbers at start of trajectory")

    parser.add_option("--show-saccades", action='store_true',dest='show_saccades',
                      help="show saccades")

    parser.add_option("--show-observations", action='store_true',dest='show_observations',
                      help="show observations")

    parser.add_option("--show-saccade-times", action='store_true',dest='show_saccade_times',
                      help="show saccade times")

    parser.add_option("--quick-kalman", action='store_true',dest='quick_kalman',
                      help="show original, causal Kalman filtered data (rather than Kalman smoothed observations)")

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

    if options.obj_only is not None:
        seq = map(int,options.obj_only.split())
        options.obj_only = seq

        if options.start is not None or options.stop is not None:
            raise ValueError("cannot specify start and stop with --obj-only option")

    doit(filename=h5_filename,
         obj_start=options.start,
         obj_end=options.stop,
         obj_only=options.obj_only,
         quick_kalman=options.quick_kalman,
         show_n_longest=options.n_top_traces,
         show_obj_ids = options.show_obj_ids,
         radius = options.radius,
         min_length = options.min_length,
         show_saccades = options.show_saccades,
         show_observations = options.show_observations,
         show_saccade_times = options.show_saccade_times,
         stim = options.stim,
         fps = 100.0,
         )
    
if __name__=='__main__':
    main()

