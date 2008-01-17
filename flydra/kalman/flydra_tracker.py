import numpy
import time
import adskalman as kalman
#import flydra.geom as geom
import flydra.fastgeom as geom
import math, struct
import flydra.data_descriptions
import collections

__all__ = ['TrackedObject','Tracker','decode_data_packet']

PT_TUPLE_IDX_X = flydra.data_descriptions.PT_TUPLE_IDX_X
PT_TUPLE_IDX_Y = flydra.data_descriptions.PT_TUPLE_IDX_Y
PT_TUPLE_IDX_AREA = flydra.data_descriptions.PT_TUPLE_IDX_AREA
PT_TUPLE_IDX_FRAME_PT_IDX = flydra.data_descriptions.PT_TUPLE_IDX_FRAME_PT_IDX

packet_header_fmt = '<idB' # XXX check format
packet_header_fmtsize = struct.calcsize(packet_header_fmt)

state_size = 9
err_size = 1
#per_tracked_object_fmt = 'I' + 'f'*(state_size+err_size) # obj_id + state vector
per_tracked_object_fmt = 'f'*(state_size+err_size)
per_tracked_object_fmtsize = struct.calcsize(per_tracked_object_fmt)

class FakeThreadingEvent:
    def isSet(self):
        return False

class TrackedObject:
    """
    Track one object using a Kalman filter.

    TrackedObject handles all internal units in meters, but external interfaces are original units

    """

    def __init__(self,
                 reconstructor_meters, # the Reconstructor instance
                 frame, # frame number of first data
                 first_observation_orig_units, # first data
                 first_observation_camns,
                 first_observation_idxs,
                 scale_factor=None,
                 kalman_model=None,
                 save_calibration_data=None,
                 max_frames_skipped=25,
                 save_all_data=False,
                 area_threshold=0,
                 ):
        """

        arguments
        =========
        reconstructor_meters - reconstructor instance with internal units of meters
        frame - frame number of first observation data
        first_observation_orig_units - first observation (in arbitrary units)
        scale_factor - how to convert from arbitrary units (of observations) into meters (e.g. 1e-3 for mm)
        kalman_model - Kalman parameters
        area_threshold - minimum area to consider for tracking use
        """
        self.area_threshold = area_threshold
        self.save_all_data = save_all_data
        self.kill_me = False
        self.reconstructor_meters = reconstructor_meters
        self.current_frameno = frame
        if scale_factor is None:
            print 'WARNING: no scale_factor given in flydra_tracker, assuming 1e-3'
            self.scale_factor = 1e-3
        else:
            self.scale_factor = scale_factor
        first_observation_meters = first_observation_orig_units*self.scale_factor
        initial_x = numpy.hstack((first_observation_meters, # convert to mm from meters
                                  (0,0,0, 0,0,0))) # zero velocity and acceleration
        ss = kalman_model['ss']
        P_k1=numpy.eye(ss) # initial state error covariance guess
        for i in range(0,3):
            P_k1[i,i]=kalman_model['initial_position_covariance_estimate']
        for i in range(3,6):
            P_k1[i,i]=kalman_model.get('initial_velocity_covariance_estimate',0)
        for i in range(6,9):
            P_k1[i,i]=kalman_model['initial_acceleration_covariance_estimate']

        self.n_sigma_accept = kalman_model['n_sigma_accept']
        self.max_variance = kalman_model['max_variance_dist_meters']**2 # square so that it is in variance units

        self.my_kalman = kalman.KalmanFilter(kalman_model['A'],
                                             kalman_model['C'],
                                             kalman_model['Q'],
                                             kalman_model['R'],
                                             initial_x,
                                             P_k1)
        self.frames = [frame]
        self.xhats = [initial_x]
        self.timestamps = [time.time()]
        self.Ps = [P_k1]

        self.observations_frames = [frame]
        self.observations_data = [first_observation_meters]

        first_observations_2d_pre = [[camn,idx] for camn,idx in zip(first_observation_camns,first_observation_idxs)]
        first_observations_2d = []
        for obs in first_observations_2d_pre:
            first_observations_2d.extend( obs )
        first_observations_2d = numpy.array(first_observations_2d,dtype=numpy.uint16) # if saved as VLArray, should match with atom type

        self.observations_2d = [first_observations_2d]

        if save_calibration_data is None:
            self.save_calibration_data = FakeThreadingEvent()
        else:
            self.save_calibration_data = save_calibration_data
        self.saved_calibration_data = []

        self.max_frames_skipped=max_frames_skipped

        # Don't run kalman filter with initial data, as this would
        # cause error estimates to drop too low.

    def distance_in_sigma( self, testx ):
        xhat = self.xhats[-1][:3]

        dist2 = numpy.sum((testx-xhat)**2) # distance squared
        dist = numpy.sqrt(dist2)

        P = self.Ps[-1]
        Pmean = numpy.sqrt(numpy.sum([P[i,i]**2 for i in range(3)])) # sigma squared
        sigma = numpy.sqrt(Pmean)
        return dist/sigma

    def kill(self):
        # called when killed
        if self.save_all_data:
            return

        # find last data
        last_observation_frame = self.observations_frames[-1]

        # eliminate estimates past last observation
        while 1:
            if self.frames[-1] > last_observation_frame:
                self.frames.pop()
                self.xhats.pop()
                self.timestamps.pop()
                self.Ps.pop()
            else:
                break

    def calculate_a_posteri_estimate(self,frame,
                                     data_dict,
                                     camn2cam_id,
                                     debug1=0):
        # Step 1. Update Kalman state to a priori estimates for this frame.
        # Step 1.A. Update Kalman state for each skipped frame.
        if self.current_frameno is not None:
            # Make sure we have xhat_k1 (previous frames' a posteri)

            # For each frame that was skipped, step the Kalman filter.
            # Since we have no observation, the estimated error will
            # rise.
            frames_skipped = frame-self.current_frameno-1

            if debug1>2:
                print 'doing',self,'============--'

            if frames_skipped > self.max_frames_skipped:
                self.kill_me = True # don't run Kalman filter, just quit
                if debug1>2:
                    print 'killed because too many frames skipped'
            else:
                if debug1>2:
                    print 'updating for %d frames skipped'%(frames_skipped,)
                for i in range(frames_skipped):
                    xhat, P = self.my_kalman.step()
                    ############ save outputs ###############
                    self.frames.append( self.current_frameno + i + 1 )
                    self.xhats.append( xhat )
                    self.timestamps.append( 0.0 )
                    self.Ps.append( P )
        else:
            raise RuntimeError("why did we get here?")

        if not self.kill_me:
            self.current_frameno = frame
            # Step 1.B. Update Kalman to provide a priori estimates for this frame
            xhatminus, Pminus = self.my_kalman.step1__calculate_a_priori()
            if debug1>2:
                print 'xhatminus'
                print xhatminus
                print 'Pminus'
                print Pminus
                print

            # Step 2. Filter incoming 2D data to use informative points
            observation_meters, used_camns_and_idxs = self._filter_data(xhatminus, Pminus,
                                                                        data_dict,
                                                                        camn2cam_id,
                                                                        debug=debug1)
            if debug1>2:
                print 'observation_meters, used_camns_and_idxs',observation_meters,used_camns_and_idxs

            # Step 3. Incorporate observation to estimate a posteri
            try:
                xhat, P = self.my_kalman.step2__calculate_a_posteri(xhatminus, Pminus,
                                                                    observation_meters)
            except OverflowError,err:
                print 'OVERFLOW ERROR:'
                print 'self.kill_me',self.kill_me
                print 'frames_skipped',type(frames_skipped),frames_skipped
                raise err

            # calculate mean variance of x y z position (assumes first three components of state vector are position)
            Pmean = numpy.sqrt(numpy.sum([P[i,i]**2 for i in range(3)]))
            if debug1>2:
                print 'xhat'
                print xhat
                print 'P'
                print P
                print 'Pmean',Pmean

            # XXX Need to test if error for this object has grown beyond a
            # threshold at which it should be terminated.
            if Pmean > self.max_variance:
                self.kill_me = True
                if debug1>2:
                    print 'will kill next time because Pmean too large'

            ############ save outputs ###############
            self.frames.append( frame )
            self.xhats.append( xhat )
            self.timestamps.append(time.time())
            self.Ps.append( P )

            if observation_meters is not None:
                self.observations_frames.append( frame )
                self.observations_data.append( observation_meters )
                tmp = []
                for (camn, frame_pt_idx, dd_idx) in used_camns_and_idxs:
                    tmp.extend( [camn,frame_pt_idx] )
                tmp = numpy.array( tmp, dtype=numpy.uint8 ) # convert to numpy
                self.observations_2d.append( tmp )
            if debug1>2:
                print
        return used_camns_and_idxs, self.kill_me

    def _filter_data(self, xhatminus, Pminus, data_dict, camn2cam_id,
                     debug=0):
        """given state estimate, select useful incoming data and make new observation"""
        # 1. For each camera, predict 2D image location and error distance

        a_priori_observation_prediction = xhatminus[:3] # equiv. to "dot(self.my_kalman.C,xhatminus)"

        variance_estimate = numpy.array([Pminus[i,i] for i in range(3)]) # maybe equiv. to "dot(self.my_kalman.C,Pminus[i,i])"
        mean_variance_estimate = numpy.sqrt(numpy.sum(variance_estimate**2)) # put in variance units (meters^2)
        dist2cmp = self.n_sigma_accept*mean_variance_estimate
        neg_predicted_3d = -geom.ThreeTuple( a_priori_observation_prediction )
        cam_ids_and_points2d = []

        used_camns_and_idxs = []
        if debug>2:
            print '_filter_data():'
            print '  dist2cmp %f = self.n_sigma_accept * %f'%(dist2cmp,mean_variance_estimate)
        for camn,candidate_point_list in data_dict.iteritems():
            cam_id = camn2cam_id[camn]

            if debug>2:
                predicted_2d = self.reconstructor_meters.find2d(cam_id,a_priori_observation_prediction)
                print 'camn',camn,'cam_id',cam_id
                print 'predicted_2d',predicted_2d

            # For large numbers of 2d points in data_dict, probably
            # faster to compute 2d image of error ellipsoid and see if
            # data_dict points fall inside that. For now, however,
            # compute distance individually

            # Use the first acceptable 2d point match as it's probably
            # best from distance-from-mean-image-background
            # perspective, but remove from further consideration all
            # 2d points that meet consideration critereon.

            closest_dist2 = numpy.inf
            closest_idx = None
            for idx,(pt_undistorted,projected_line_meters) in enumerate(candidate_point_list):
                # find closest distance between projected_line and predicted position for each 2d point
                dist2=projected_line_meters.translate(neg_predicted_3d).dist2()
                pt_area = pt_undistorted[PT_TUPLE_IDX_AREA]

                if debug>2:
                    frame_pt_idx = pt_undistorted[PT_TUPLE_IDX_FRAME_PT_IDX]
                    print '->', dist2, pt_undistorted[:2], '(idx %d, area %f)'%(frame_pt_idx,pt_area)

                if dist2<dist2cmp and pt_area > self.area_threshold:
                    if debug>2:
                        print '       (acceptable)'
                    if dist2<closest_dist2:
                        if debug>2:
                            print '       (so far the best -- taking)'
                        closest_dist2 = dist2
                        closest_idx = idx

            if closest_idx is not None:
                pt_undistorted, projected_line_meters = candidate_point_list[closest_idx]
                cam_ids_and_points2d.append( (cam_id,(pt_undistorted[PT_TUPLE_IDX_X],
                                                      pt_undistorted[PT_TUPLE_IDX_Y])))
                frame_pt_idx = pt_undistorted[PT_TUPLE_IDX_FRAME_PT_IDX]
                used_camns_and_idxs.append( (camn, frame_pt_idx, closest_idx) )

                if debug>2:
                    print 'best match idx %d (%s)'%(closest_idx, str(pt_undistorted[:2]))
        # Now cam_ids_and_points2d has just the 2d points we'll use for this reconstruction
        if len(cam_ids_and_points2d)>=2:
            if debug>=1:
                print len(cam_ids_and_points2d) # XXX ADS
            observation_meters = self.reconstructor_meters.find3d( cam_ids_and_points2d, return_line_coords = False)
            if len(cam_ids_and_points2d)>=3:
                if self.save_calibration_data.isSet():
                    self.saved_calibration_data.append( cam_ids_and_points2d )
        else:
            observation_meters = None
        return observation_meters, used_camns_and_idxs

class Tracker:
    """
    Handle multiple tracked objects using TrackedObject instances.

    This class keeps a list of objects currently being tracked. It
    also keeps a couple other lists for dealing with cases when the
    tracked objects are no longer 'live'.

    """
    def __init__(self,
                 reconstructor_meters,
                 scale_factor=None,
                 kalman_model=None,
                 save_calibration_data=None,
                 max_frames_skipped=25,
                 save_all_data=False,
                 area_threshold=0,
                 ):
        """

        arguments
        =========
        reconstructor_meters - reconstructor instance with internal units of meters
        scale_factor - how to convert from arbitrary units (of observations) into meters (e.g. 1e-3 for mm)
        kalman_model - dictionary of Kalman filter parameters
        area_threshold - minimum area to consider for tracking use

        """
        self.area_threshold = area_threshold
        self.save_all_data = save_all_data
        self.reconstructor_meters=reconstructor_meters
        self.live_tracked_objects = []
        self.dead_tracked_objects = [] # save for getting data out
        self.kill_tracker_callbacks = []

        # set values for passing to TrackedObject
        if scale_factor is None:
            print 'WARNING: scale_factor set to 1e-3 (because no value was specified)',__file__
            self.scale_factor = 1e-3
        else:
            self.scale_factor = scale_factor
        self.max_frames_skipped = max_frames_skipped

        if kalman_model is None:
            raise ValueError('must specify kalman_model')
        self.kalman_model = kalman_model
        self.save_calibration_data=save_calibration_data

    def is_believably_new( self, Xmm, n_sigma = 2.0, debug=0 ):

        """Sometimes the Kalman tracker will not gobble all the points
        it should. This still prevents spawning a new Kalman
        tracker."""

        believably_new = True
        X = Xmm*self.scale_factor
        for idx,tro in enumerate(self.live_tracked_objects):
            if debug>5:
                nsigma = tro.distance_in_sigma( X )
                print 'distance in sigma:',nsigma, tro
            if tro.distance_in_sigma( X ) < n_sigma:
                believably_new = False
                break
        return believably_new

    def calculate_a_posteri_estimates(self,frame,data_dict,camn2cam_id,debug2=0):
        # Allow earlier tracked objects to be greedy and take all the
        # data they want.
        kill_idxs = []

        if debug2>1:
            print self,'gobbling all data for frame %d'%(frame,)

        all_to_gobble= []
        # I could easily parallelize this========================================
        for idx,tro in enumerate(self.live_tracked_objects):
            try:
                this_to_gobble, kill_me = tro.calculate_a_posteri_estimate(frame,
                                                                           data_dict,
                                                                           camn2cam_id,
                                                                           debug1=debug2,
                                                                           )
            except OverflowError, err:
                print 'WARNING: OverflowError in tro.gobble_2d_data_and_calculate_a_posteri_estimate'
                print 'Killing tracked object and continuing...'
                print 'XXX Note to Andrew: should really fix this within the Kalman object.'
                tro.kill_me = True
                kill_idxs.append( idx )
            else:
                for item in this_to_gobble:
                    all_to_gobble.append( item )
                if kill_me:
                    kill_idxs.append( idx )
        # End  ================================================================

        if 1:

            # We deferred gobbling until now - fuse all points to be
            # gobbled and remove them from further consideration.

            # fuse dictionaries
            fused_to_gobble = collections.defaultdict(set)
            for (camn, frame_pt_idx, dd_idx) in all_to_gobble:
                fused_to_gobble[camn].add(dd_idx)

            for camn, dd_idx_set in fused_to_gobble.iteritems():
                dd_idx = list(dd_idx_set)
                dd_idx.sort()
                dd_idx.reverse()
                for idx in dd_idx:
                    del data_dict[camn][idx]

        # remove tracked objects from live list (when their error grows too large)
        kill_idxs.reverse()
        for kill_idx in kill_idxs:
            tro = self.live_tracked_objects.pop( kill_idx )
            tro.kill()
            if self.save_all_data or len(tro.observations_frames)>1:
                # require more than single observation to save
                self.dead_tracked_objects.append( tro )
        self._flush_dead_queue()
        return data_dict

    def join_new_obj(self,
                     frame,
                     first_observation_orig_units,
                     first_observation_camns,
                     first_observation_idxs,
                     debug=0):
        tro = TrackedObject(self.reconstructor_meters,
                            frame,
                            first_observation_orig_units,
                            first_observation_camns,
                            first_observation_idxs,
                            scale_factor=self.scale_factor,
                            kalman_model=self.kalman_model,
                            save_calibration_data=self.save_calibration_data,
                            max_frames_skipped=self.max_frames_skipped,
                            save_all_data=self.save_all_data,
                            area_threshold=self.area_threshold,
                            )
        self.live_tracked_objects.append(tro)
    def kill_all_trackers(self):
        while len(self.live_tracked_objects):
            tro = self.live_tracked_objects.pop()
            tro.kill()
            self.dead_tracked_objects.append( tro )
        self._flush_dead_queue()
    def set_killed_tracker_callback(self,callback):
        self.kill_tracker_callbacks.append( callback )

    def _flush_dead_queue(self):
        while len(self.dead_tracked_objects):
            tro = self.dead_tracked_objects.pop(0)
            for callback in self.kill_tracker_callbacks:
                callback(tro)

    def encode_data_packet(self,corrected_framenumber,timestamp):
        N = len(self.live_tracked_objects)
        data_packet = struct.pack(packet_header_fmt,
                                  corrected_framenumber,
                                  timestamp,
                                  N)
        for idx,tro in enumerate(self.live_tracked_objects):
            if not len(tro.xhats):
                continue
            xhat = tro.xhats[-1]
            P = tro.Ps[-1]
            meanP = math.sqrt(numpy.sum(numpy.array([P[i,i]**2 for i in range(3)])))
            data_values = [xhat[i] for i in range(state_size)]+[meanP]
            data_packet += struct.pack(per_tracked_object_fmt,*data_values)
        return data_packet

def decode_data_packet(buf):
    header = buf[:packet_header_fmtsize]
    rest = buf[packet_header_fmtsize:]

    (corrected_framenumber,timestamp,N) = struct.unpack(
        packet_header_fmt,header)
    state_vecs = []
    for i in range(N):
        this_tro = rest[:per_tracked_object_fmtsize]
        rest = rest[per_tracked_object_fmtsize:]

        results = struct.unpack(per_tracked_object_fmt,this_tro)
        state_vec = results[:state_size]
        meanP = results[state_size]
        state_vecs.append( state_vec )
    return corrected_framenumber, timestamp, state_vecs, meanP



