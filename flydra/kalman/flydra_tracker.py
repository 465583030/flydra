import numpy
import time
import adskalman as kalman
import params
import flydra.geom as geom
import math, struct
import flydra.data_descriptions

__all__ = ['TrackedObject','Tracker']

PT_TUPLE_IDX_X = flydra.data_descriptions.PT_TUPLE_IDX_X
PT_TUPLE_IDX_Y = flydra.data_descriptions.PT_TUPLE_IDX_Y
PT_TUPLE_IDX_FRAME_PT_IDX = flydra.data_descriptions.PT_TUPLE_IDX_FRAME_PT_IDX

state_size = params.A.shape[0]
per_tracked_object_fmt = 'f'*state_size

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
                 n_sigma_accept = 3.0, # default: arbitrarily set to 3
                 max_variance_dist_meters = 0.010, # default: allow error to grow to 10 mm before dropping
                 initial_position_covariance_estimate = 1e-6, # default: initial guess 1mm ( (1e-3)**2 meters)
                 initial_acceleration_covariance_estimate = 15, # default: arbitrary initial guess, rather large
                 Q = None,
                 R = None,
                 save_calibration_data=None,
                 max_frames_skipped=25,
                 ):
        """

        arguments
        ---------
        reconstructor_meters - reconstructor instance with internal units of meters
        frame - frame number of first observation data
        first_observation_orig_units - first observation (in arbitrary units)
        scale_factor - how to convert from arbitrary units (of observations) into meters (e.g. 1e-3 for mm)
        n_sigma_accept - gobble 2D data points that are within this distance from predicted 2D location
        max_variance_dist_meters - estimated error (in meters) to allow growth to before killing tracked object
        initial_position_covariance_estimate -
        initial_acceleration_covariance_estimate -
        Q - process covariance matrix
        R - measurement noise covariance matrix
        """
        self.kill_me = False
        self.reconstructor_meters = reconstructor_meters
        self.current_frameno = frame
        self.n_sigma_accept = n_sigma_accept # arbitrary
        self.max_variance_dist_meters = max_variance_dist_meters
        if scale_factor is None:
            print 'WARNING: no scale_factor given in flydra_tracker, assuming 1e-3'
            self.scale_factor = 1e-3
        else:
            self.scale_factor = scale_factor
        first_observation_meters = first_observation_orig_units*self.scale_factor
        initial_x = numpy.hstack((first_observation_meters, # convert to mm from meters
                                  (0,0,0, 0,0,0))) # zero velocity and acceleration
        ss = params.A.shape[0]
        P_k1=numpy.eye(ss) # initial state error covariance guess
        for i in range(0,3):
            P_k1[i,i]=initial_position_covariance_estimate
        for i in range(6,9):
            P_k1[i,i]=initial_acceleration_covariance_estimate

        if Q is None:
            Q = params.Q
        if R is None:
            R = params.R
        self.my_kalman = kalman.KalmanFilter(params.A,
                                             params.C,
                                             Q,
                                             R,
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
    def kill(self):
        # called when killed

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
        
    def gobble_2d_data_and_calculate_a_posteri_estimate(self,frame,data_dict,camn2cam_id,debug1=False):
        # Step 1. Update Kalman state to a priori estimates for this frame.
        # Step 1.A. Update Kalman state for each skipped frame.
        if self.current_frameno is not None:
            # Make sure we have xhat_k1 (previous frames' a posteri)

            # For each frame that was skipped, step the Kalman filter.
            # Since we have no observation, the estimated error will
            # rise.
            frames_skipped = frame-self.current_frameno-1
            
            if debug1:
                print 'doing',self,'--------------'
            
            if frames_skipped > self.max_frames_skipped:
                self.kill_me = True # don't run Kalman filter, just quit
                if debug1:
                    print 'killed because too many frames skipped'
            else:
                if debug1:
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
            if debug1:
                print 'xhatminus, Pminus',xhatminus,Pminus

            # Step 2. Filter incoming 2D data to use informative points
            observation_meters, used_camns_and_idxs = self._filter_data(xhatminus, Pminus,
                                                                        data_dict,
                                                                        camn2cam_id,
                                                                        debug=debug1)
            if debug1:
                print 'observation_meters, used_camns_and_idxs',observation_meters,used_camns_and_idxs

            # Step 3. Incorporate observation to estimate a posteri
            try:
                xhat, P = self.my_kalman.step2__calculate_a_posteri(xhatminus, Pminus,
                                                                    observation_meters)
            except OverflowError,err:
                print 'OVERFLOW ERROR:'
                print 'self.kill_me',self.kill_me
                print 'frames_skipped',type(frames_skipped),frames_skipped
                print 'self.my_kalman.n_skipped',type(self.my_kalman.n_skipped),self.my_kalman.n_skipped
                raise err
                
            Pmean = numpy.sqrt(numpy.sum([P[i,i] for i in range(3)]))
            if debug1:
                print 'xhat,P,Pmean',xhat,P,Pmean

            # XXX Need to test if error for this object has grown beyond a
            # threshold at which it should be terminated.
            if Pmean > self.max_variance_dist_meters:
                self.kill_me = True
                if debug1:
                    print 'will kill next time because Pmean too large'

            ############ save outputs ###############
            self.frames.append( frame )
            self.xhats.append( xhat )
            self.timestamps.append(time.time())
            self.Ps.append( P )
        
            if observation_meters is not None:
                self.observations_frames.append( frame )
                self.observations_data.append( observation_meters )
                self.observations_2d.append( used_camns_and_idxs )
            if debug1:
                print
        
    def _filter_data(self, xhatminus, Pminus, data_dict, camn2cam_id, debug=False):
        """given state estimate, select useful incoming data and make new observation"""
        # 1. For each camera, predict 2D image location and error distance
        
        a_priori_observation_prediction = xhatminus[:3] # equiv. to "dot(self.my_kalman.C,xhatminus)"
        
        variance_estimate = [Pminus[i,i] for i in range(3)] # maybe equiv. to "dot(self.my_kalman.C,Pminus[i,i])"
        variance_estimate_scalar = numpy.sqrt(numpy.sum(variance_estimate)) # put in distance units (meters)
        neg_predicted_3d = -geom.ThreeTuple( a_priori_observation_prediction )
        cam_ids_and_points2d = []

        used_camns_and_idxs = []
        if debug:
            print '_filter_data():'
            print '  variance_estimate_scalar',variance_estimate_scalar
        for camn in data_dict:
            cam_id = camn2cam_id[camn]

            predicted_2d = self.reconstructor_meters.find2d(cam_id,a_priori_observation_prediction)
            if debug:
                print 'camn',camn,'cam_id',cam_id
                print 'predicted_2d',predicted_2d
            # For large numbers of 2d points in data_dict, probably
            # faster to compute 2d image of error ellipsoid and see if
            # data_dict points fall inside that. For now, however,
            # compute distance individually

            candidate_point_list = data_dict[camn]
            found_idxs = []
            
            # Use the first acceptable 2d point match as it's probably
            # best from distance-from-mean-image-backgroudn
            # perspective, but remove from further consideration all
            # 2d points that meet consideration critereon.

            match_dist_and_idx = []
            for idx,(pt_undistorted,projected_line_meters) in enumerate(candidate_point_list):
                # find closest distance between projected_line and predicted position for each 2d point
                dist2=projected_line_meters.translate(neg_predicted_3d).dist2()
                dist = numpy.sqrt(dist2)
                
                if debug:
                    frame_pt_idx = pt_undistorted[PT_TUPLE_IDX_FRAME_PT_IDX]
                    print '->', dist, pt_undistorted[:2], '(idx %d)'%(frame_pt_idx,),
                
                if dist<(self.n_sigma_accept*variance_estimate_scalar):
                    # accept point
                    match_dist_and_idx.append( (dist,idx) )
                    found_idxs.append( idx )
                    if debug:
                        frame_pt_idx = pt_undistorted[PT_TUPLE_IDX_FRAME_PT_IDX]
                        print '(accepted)'
                else:
                    if debug:
                        print
                    
            match_dist_and_idx.sort() # sort by distance
            if len(match_dist_and_idx):
                closest_idx = match_dist_and_idx[0][1] # take idx of closest point
                pt_undistorted, projected_line_meters = candidate_point_list[closest_idx]
                cam_ids_and_points2d.append( (cam_id,(pt_undistorted[PT_TUPLE_IDX_X],
                                                      pt_undistorted[PT_TUPLE_IDX_Y])))
                frame_pt_idx = pt_undistorted[PT_TUPLE_IDX_FRAME_PT_IDX]
                used_camns_and_idxs.extend( [camn, frame_pt_idx] )
                if debug:
                    print 'best match idx %d (%s)'%(frame_pt_idx, str(pt_undistorted[:2]))
            found_idxs.reverse() # keep indexes OK as we delete them
            for idx in found_idxs:
                del candidate_point_list[idx]
        # Now new_data_dict has just the 2d points we'll use for this reconstruction
        if len(cam_ids_and_points2d)>=2:
            observation_meters = self.reconstructor_meters.find3d( cam_ids_and_points2d, return_line_coords = False)
            if len(cam_ids_and_points2d)>=3:
                if self.save_calibration_data.isSet():
                    self.saved_calibration_data.append( cam_ids_and_points2d )
        else:
            observation_meters = None
        used_camns_and_idxs = numpy.array( used_camns_and_idxs, dtype=numpy.uint8 ) # convert to numpy
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
                 n_sigma_accept = 3.0, # default: arbitrarily set to 3
                 max_variance_dist_meters = 0.010, # default: allow error to grow to 10 mm before dropping
                 initial_position_covariance_estimate = 1e-6, # default: initial guess 1mm ( (1e-3)**2 meters)
                 initial_acceleration_covariance_estimate = 15, # default: arbitrary initial guess, rather large
                 Q = None,
                 R = None,
                 save_calibration_data=None,
                 ):
        """
        
        arguments
        ---------
        reconstructor_meters - reconstructor instance with internal units of meters
        scale_factor - how to convert from arbitrary units (of observations) into meters (e.g. 1e-3 for mm)
        n_sigma_accept - gobble 2D data points that are within this distance from predicted 2D location
        max_variance_dist_meters - estimated error (in meters) to allow growth to before killing tracked object
        initial_position_covariance_estimate -
        initial_acceleration_covariance_estimate -
        Q - process covariance matrix
        R - measurement noise covariance matrix
        
        """
        
        self.reconstructor_meters=reconstructor_meters
        self.live_tracked_objects = []
        self.dead_tracked_objects = [] # save for getting data out
        self.kill_tracker_callbacks = []

        # set values for passing to TrackedObject
        if scale_factor is None:
            print 'WARNING: scale_factor set to 1e-3',__file__
            self.scale_factor = 1e-3
        else:
            self.scale_factor = scale_factor
        self.n_sigma_accept = n_sigma_accept
        self.max_variance_dist_meters = max_variance_dist_meters
        self.initial_position_covariance_estimate = initial_position_covariance_estimate
        self.initial_acceleration_covariance_estimate = initial_acceleration_covariance_estimate
        self.Q = Q
        self.R = R
        self.save_calibration_data=save_calibration_data
            
    def gobble_2d_data_and_calculate_a_posteri_estimates(self,frame,data_dict,camn2cam_id,debug2=False):
        # Allow earlier tracked objects to be greedy and take all the
        # data they want.
        kill_idxs = []
        
        if debug2:
            print self,'gobbling all data for frame %d'%(frame,)
            
        for idx,tro in enumerate(self.live_tracked_objects):
            try:
                tro.gobble_2d_data_and_calculate_a_posteri_estimate(frame,
                                                                    data_dict,
                                                                    camn2cam_id,
                                                                    debug1=debug2)
            except OverflowError, err:
                print 'WARNING: OverflowError in tro.gobble_2d_data_and_calculate_a_posteri_estimate'
                print 'Killing tracked object and continuing...'
                print 'XXX Note to Andrew: should really fix this within the Kalman object.'
                tro.kill_me = True
            if tro.kill_me:
                kill_idxs.append( idx )

        # remove tracked objects from live list (when their error grows too large)
        kill_idxs.reverse()
        for kill_idx in kill_idxs:
            tro = self.live_tracked_objects.pop( kill_idx )
            tro.kill()
            if len(tro.observations_frames)>1:
                # require more than single observation to save
                self.dead_tracked_objects.append( tro )
        self._flush_dead_queue()
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
                            n_sigma_accept = self.n_sigma_accept,
                            max_variance_dist_meters = self.max_variance_dist_meters,
                            initial_position_covariance_estimate = self.initial_position_covariance_estimate,
                            initial_acceleration_covariance_estimate = self.initial_acceleration_covariance_estimate,
                            Q = self.Q,
                            R = self.R,
                            save_calibration_data=self.save_calibration_data,
                            )
        self.live_tracked_objects.append(tro)
        if debug>0:
            print 'new:',tro
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
        fmt = '<idB' # XXX check format
        data_packet = struct.pack(fmt,
                                  corrected_framenumber,
                                  timestamp,
                                  N)
        for idx,tro in enumerate(self.live_tracked_objects):
            if not len(tro.xhats):
                continue
            xhat = tro.xhats[-1]
            data_packet += struct.pack(per_tracked_object_fmt,
                                       *xhat)
        return data_packet
    
