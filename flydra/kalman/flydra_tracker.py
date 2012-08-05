import numpy
import numpy as np
import flydra.kalman.ekf as kalman_ekf
#import flydra.geom as geom
import flydra.fastgeom as geom
import flydra.geom
import os, math, struct
import flydra.data_descriptions as data_descriptions
import warnings, collections
from pprint import pprint

import flydra_tracked_object
from flydra_tracked_object import TrackedObject

__all__ = ['TrackedObject','Tracker']

class AsyncApplier(object):
    def __init__(self,mylist,name,args=None,kwargs=None,targets=None):
        self.mylist = mylist
        self.name = name
        self.args = args
        self.kwargs = kwargs
        self.targets = targets
    def get(self):
        """wait for and return asynchronous results"""
        if self.targets is None:
            targets = range(len(self.mylist))
        else:
            targets = self.targets

        if self.args is not None:
            if self.kwargs is not None:
                results = [getattr(self.mylist[i],self.name)(*self.args,**self.kwargs) for i in targets]
            else:
                results = [getattr(self.mylist[i],self.name)(*self.args) for i in targets]
        else:
            if self.kwargs is not None:
                results = [getattr(self.mylist[i],self.name)(**self.kwargs) for i in targets]
            else:
                results = [getattr(self.mylist[i],self.name)() for i in targets]

        return results

class RemoteProxy(object):
    def __init__(self,obj):
        self._obj=obj
    def __getattr__(self,name):
        return getattr(self._obj,name)

class TrackedObjectKeeper(object):
    """proxy to keep all tracked objects, possibly in other processes

    Load balancing, as such, is acheived by equalizing number of live
    objects across processes.

    """
    def __init__(self,klass):
        self._tros = []
        self._klass = klass
    def how_many_are_living(self):
        return len(self._tros)
    def remove_from_remote(self,targets=None):
        """remove from remote server and return as local object"""
        if targets is None:
            targets = range(len(self._tros))
        else:
            targets = targets[:]
            targets.sort()
        targets.reverse()
        results = [RemoteProxy(self._tros[i]) for i in targets]
        for i in targets:
            del self._tros[i]
        return results
    def make_new(self,*args,**kwargs):
        instance = self._klass(*args,**kwargs)
        self._tros.append( instance )
    def get_async_applier(self,name,args=None,kwargs=None,targets=None):
        return AsyncApplier(self._tros,name,
                            args=args,
                            kwargs=kwargs,
                            targets=targets)
    def rmap_async(self, name, *args, **kwargs):
        """asynchronous reverse map function

        Applies the same set of args and kwargs to every element.

        """
        return AsyncApplier(self._tros, name, args, kwargs)
    def rmap(self, name, *args, **kwargs):
        """reverse map function

        Applies the same set of args and kwargs to every element.

        """
        return self.rmap_async(name,*args,**kwargs).get()

class AsyncApplierIPythonParallel(object):
    def __init__(self,mec,mylist):
        self._mylist = mylist
        self._mec = mec
    def get(self):
        pr_list = [entry[2] for entry in self._mylist]
        self._mec.barrier(pr_list) # wait until all pending are done
        results = []
        for (ip_target, result_name, pr) in self._mylist:
            this_result = pr.get_result(block=True)
            if 0:
                print 'got result',this_result
                print 'got result len',len(this_result)
                print 'got result',type(this_result)
                print 'got result',dir(this_result)
                results.append( this_result[0] )
            else:
                results.extend( self._mec.pull( result_name, targets=[ip_target] ) )
        return results

class RemoteProxyIPythonParallel(object):
    def __init__(self,mec,ip_target,remote_name):
        self._mec = mec
        self._ip_target = ip_target
        self._remote_name = remote_name
    def __del__(self):
        self._mec.execute('del %s'%self._remote_name,targets=[self._ip_target])
    def __getattr__(self,attr_name):
        fullname = '%s.%s'%(self._remote_name,attr_name)
        self._mec.execute('tmp=%s'%fullname,targets=[self._ip_target])
        return self._mec.pull('tmp',targets=[self._ip_target])

class TrackedObjectKeeperIPythonParallel(object):
    """proxy to keep all tracked objects, possibly in other processes

    Load balancing, as such, is acheived by equalizing number of live
    objects across processes.

    """
    def __init__(self,klass):
        from IPython.kernel import client
        self._mec = client.MultiEngineClient()
        assert klass == TrackedObject

        if 1:
            fname = flydra_tracked_object.__file__
            base,ext = os.path.splitext(fname)
            if ext=='.pyc':
                ext = '.py'
            fname = base+ext
            fd = open(fname,mode='r')
            objstr = fd.read()
            self._mec.execute(objstr)
        else:
            self._mec.execute('from flydra.kalman.flydra_tracked_object import TrackedObject')
        print 'executed remote code OK',self._mec.get_ids()
        self._tro_handles = []
        self._count = 0
        self._uniq = 0
    def how_many_are_living(self):
        return len(self._tro_handles)
    def _make_unique(self):
        self._uniq += 1
        return 'uniq%d'%self._uniq
    def remove_from_remote(self,targets=None):
        """remove from remote server and return as local object"""
        if targets is None:
            targets = range(len(self._tro_handles))
        else:
            targets = targets[:]
            targets.sort()

        results = []
        targets.reverse()
        for t in targets:
            (ip_target,remote_name) =self._tro_handles[t]
            this_result = RemoteProxyIPythonParallel( self._mec, ip_target,remote_name)
            #this_result = self._mec.pull( remote_name, targets=[ip_target] )
            results.append(this_result)

            del self._tro_handles[t]
            self._count -= 1
        #print 'count',self._count
        return results
    def make_new(self,*args,**kwargs):
        remote_name = self._make_unique()

        N_remote = len(self._mec.get_ids())
        N_per_target = np.zeros( (N_remote,) )
        for ip_target,tmp in self._tro_handles:
            N_per_target[ip_target] += 1
        ip_target = int(np.argmin(N_per_target))
        self._mec.push( dict(args=args,kwargs=kwargs), targets=[ip_target] )
        self._mec.execute('%s = TrackedObject(*args,**kwargs)'%remote_name,targets=[ip_target])

        self._tro_handles.append( (ip_target,remote_name) )
        self._count += 1
        #print 'count',self._count
    def get_async_applier(self,attr_name,args=None,kwargs=None,targets=None):
        if targets is None:
            targets = range(self._count)
        tmp = []
        for t in targets:
            ip_target,instance_name = self._tro_handles[t]
            ip_targets = [ip_target]
            result_name = 'result' # XXX, fixme?, should make unique
            pushdict = {}
            callstr = []
            if args is not None:
                pushdict['args']=args
                callstr.append('*args')
            if kwargs is not None:
                pushdict['kwargs']=kwargs
                callstr.append('**kwargs')
            self._mec.push( pushdict, targets=ip_targets )
            execstr = '%s = %s.%s(%s)'%(result_name,instance_name,attr_name,','.join(callstr))
            pr = self._mec.execute(execstr,
                                   targets=ip_targets,
                                   block=False)
            tmp.append( (ip_target, result_name, pr) )
        return AsyncApplierIPythonParallel( self._mec, tmp )
    def rmap_async(self, attr_name, *args, **kwargs):
        """asynchronous reverse map function

        Applies the same set of args and kwargs to every element.

        """
        targets = range(self._count)
        tmp = []
        for t in targets:
            ip_target,instance_name = self._tro_handles[t]
            ip_targets = [ip_target]
            result_name = 'result' # XXX, fixme?, should make unique
            pushdict = {}
            callstr = []
            if args is not None:
                pushdict['args']=args
                callstr.append('*args')
            if kwargs is not None:
                pushdict['kwargs']=kwargs
                callstr.append('**kwargs')
            self._mec.push( pushdict, targets=ip_targets )
            execstr = '%s = %s.%s(%s)'%(result_name,instance_name,attr_name,','.join(callstr))
            pr = self._mec.execute(execstr,
                                   targets=ip_targets,
                                   block=False)
            tmp.append( (ip_target, result_name, pr) )
        return AsyncApplierIPythonParallel(  self._mec, tmp )
    def rmap(self, name, *args, **kwargs):
        """reverse map function

        Applies the same set of args and kwargs to every element.

        """
        return self.rmap_async(name,*args,**kwargs).get()

class Tracker:
    """
    Handle multiple tracked objects using TrackedObject instances.

    This class keeps a list of objects currently being tracked. It
    also keeps a couple other lists for dealing with cases when the
    tracked objects are no longer 'live'.

    """
    def __init__(self,
                 reconstructor_meters,
                 kalman_model=None,
                 max_frames_skipped=25,
                 save_all_data=False,
                 area_threshold=0,
                 area_threshold_for_orientation=0.0,
                 disable_image_stat_gating=False,
                 orientation_consensus=0,
                 fake_timestamp=None,
                 ):
        """

        arguments
        =========
        reconstructor_meters - reconstructor instance with internal units of meters
        kalman_model - dictionary of Kalman filter parameters
        area_threshold - minimum area to consider for tracking use

        """
        self.area_threshold = area_threshold
        self.area_threshold_for_orientation=area_threshold_for_orientation
        self.save_all_data = save_all_data
        self.reconstructor_meters=reconstructor_meters
        #self.live_tracked_objects = TrackedObjectKeeperIPythonParallel( TrackedObject )
        self.live_tracked_objects = TrackedObjectKeeper( TrackedObject )
        self.dead_tracked_objects = [] # save for getting data out
        self.kill_tracker_callbacks = []
        self.disable_image_stat_gating = disable_image_stat_gating
        self.orientation_consensus = orientation_consensus
        self.fake_timestamp = fake_timestamp
        self.cur_obj_id = 0

        # set values for passing to TrackedObject
        self.max_frames_skipped = max_frames_skipped

        if kalman_model is None:
            raise ValueError('must specify kalman_model')
        self.kalman_model = kalman_model

    def is_believably_new( self, Xmm, debug=0 ):

        """Sometimes the Kalman tracker will not gobble all the points
        it should. This still prevents spawning a new Kalman
        tracker."""

        believably_new = True
        X = Xmm
        min_dist_to_believe_new_meters = self.kalman_model['min_dist_to_believe_new_meters']
        min_dist_to_believe_new_nsigma = self.kalman_model['min_dist_to_believe_new_sigma']
        results = self.live_tracked_objects.rmap( 'distance_in_meters_and_nsigma', X ) # reverse map
        for (dist_meters, dist_nsigma) in results:
            if debug>5:
                print 'distance in meters, nsigma:',dist_meters, dist_nsigma
            if ((dist_nsigma < min_dist_to_believe_new_nsigma) or
                (dist_meters < min_dist_to_believe_new_meters)):
                believably_new = False
                break
        return believably_new

    def remove_duplicate_detections(self,frame,input_data_dict):
        """remove points that are close to current objects being tracked"""

        PT_TUPLE_IDX_FRAME_PT_IDX = data_descriptions.PT_TUPLE_IDX_FRAME_PT_IDX

        (test_frame, bad_camn_pts) = self.last_close_camn_pt_idxs
        assert test_frame==frame

        all_bad_pts = collections.defaultdict(set)
        for camn, ptnum in bad_camn_pts:
            all_bad_pts[camn].add(ptnum)

        output_data_dict = collections.defaultdict(list)
        for camn,camn_list in input_data_dict.iteritems():
            bad_pts = all_bad_pts[camn]
            for element in camn_list:
                pt = element[0]
                ptnum = pt[PT_TUPLE_IDX_FRAME_PT_IDX]
                if ptnum not in bad_pts:
                    output_data_dict[camn].append( element )

        return output_data_dict

    def calculate_a_posteriori_estimates(self,frame,data_dict,camn2cam_id,debug2=0):
        # Allow earlier tracked objects to take all the data they
        # want.

        if debug2>1:
            print self,'gobbling all data for frame %d'%(frame,)

        kill_idxs = []
        all_to_gobble= []
        best_by_hash = {}
        to_rewind = []
        # I could parallelize this========================================
        # this is map:
        results = self.live_tracked_objects.rmap(
            'calculate_a_posteriori_estimate',
            frame,
            data_dict,
            camn2cam_id,
            debug1=debug2,
            )

        # this is reduce:
        all_close_camn_pt_idxs = []
        for idx,result in enumerate(results):
            (used_camns_and_idxs, kill_me, obs2d_hash,
             Pmean, close_camn_pt_idxs) = result

            # Two similar lists -- lists of points that will be
            # removed from further consideration. "Gobbling" prevents
            # another object from using it if all the data were in
            # common. Removal of "close", probable duplicate
            # detections, does not remove consideration from
            # pre-existing objects, but will prevent birth of new
            # targets.

            all_to_gobble.extend( used_camns_and_idxs )
            all_close_camn_pt_idxs.extend( close_camn_pt_idxs )

            if kill_me:
                kill_idxs.append( idx )
            if obs2d_hash is not None:
                if obs2d_hash in best_by_hash:
                    (best_idx, best_Pmean) = best_by_hash[ obs2d_hash ]
                    if Pmean < best_Pmean:
                        # new value is better than previous best
                        best_by_hash[ obs2d_hash ] = ( idx, Pmean )
                        to_rewind.append( best_idx )
                    else:
                        # old value is still best
                        to_rewind.append( idx )
                else:
                    best_by_hash[obs2d_hash] = ( idx, Pmean )
        self.last_close_camn_pt_idxs = (frame, all_close_camn_pt_idxs)

        # End  ================================================================

        if len(all_to_gobble):

            # We deferred gobbling until now - fuse all points to be
            # gobbled and remove them from further consideration.

            # fuse dictionaries
            fused_to_gobble = collections.defaultdict(set)
            for (camn, frame_pt_idx, dd_idx) in all_to_gobble:
                fused_to_gobble[camn].add(dd_idx)

            # remove data to gobble
            for camn, dd_idx_set in fused_to_gobble.iteritems():
                old_list = data_dict[camn]
                data_dict[camn] = [ item for (idx,item) in enumerate(old_list) if idx not in dd_idx_set ]

        if len(to_rewind):

            # Take-back previous observations - starve this Kalman
            # object (which has higher error) so that 2 Kalman objects
            # don't start sharing all observations.

            self.live_tracked_objects.get_async_applier(
                'remove_previous_observation', kwargs=dict(debug1=debug2),
                targets=to_rewind).get()

        # remove tracked objects from live list (when their error grows too large)
        self.live_tracked_objects.get_async_applier('kill', targets=kill_idxs).get()
        self.dead_tracked_objects.extend(self.live_tracked_objects.remove_from_remote(targets=kill_idxs))
        self._flush_dead_queue()
        return data_dict

    def join_new_obj(self,
                     frame,
                     first_observation_orig_units,
                     first_observation_Lcoords_orig_units,
                     first_observation_camns,
                     first_observation_idxs,
                     debug=0):
        obj_id = self.cur_obj_id
        self.cur_obj_id+=1

        self.live_tracked_objects.make_new(
            self.reconstructor_meters,
            obj_id,
            frame,
            first_observation_orig_units,
            first_observation_Lcoords_orig_units,
            first_observation_camns,
            first_observation_idxs,
            kalman_model=self.kalman_model,
            save_all_data=self.save_all_data,
            area_threshold=self.area_threshold,
            area_threshold_for_orientation=self.area_threshold_for_orientation,
            disable_image_stat_gating=self.disable_image_stat_gating,
            orientation_consensus = self.orientation_consensus,
            fake_timestamp = self.fake_timestamp,
            )
    def kill_all_trackers(self):
        self.live_tracked_objects.get_async_applier('kill').get()
        self.dead_tracked_objects.extend(
            self.live_tracked_objects.remove_from_remote()
            )
        self._flush_dead_queue()
    def set_killed_tracker_callback(self,callback):
        self.kill_tracker_callbacks.append( callback )

    def _flush_dead_queue(self):
        while len(self.dead_tracked_objects):
            tro = self.dead_tracked_objects.pop(0)
            for callback in self.kill_tracker_callbacks:
                callback(tro)
