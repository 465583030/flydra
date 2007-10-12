import threading, sys, time, traceback
from contextlib import contextmanager

class DebugLock(object):
    def __init__(self,name,verbose=False):
        self.name = name
        self._lock = threading.Lock()
        self.verbose = verbose

    def acquire(self, latency_warn_msec = None):
        if self.verbose:
            print '-='*20
        print '*****',self.name,'request acquire by',threading.currentThread()
        if self.verbose:
            frame = sys._getframe()
            traceback.print_stack(frame)
            print '-='*20
        tstart = time.time()
        self._lock.acquire()
        tstop = time.time()
        print '*****',self.name,'acquired by',threading.currentThread()
        if latency_warn_msec is not None:
            lat = (tstop-tstart)*1000.0
            if lat > latency_warn_msec:
                print '          **** WARNING acquisition time %.1f msec'%lat
        
        if self.verbose:
            frame = sys._getframe()
            traceback.print_stack(frame)
            print '-='*20

    def release(self):
        print '*****',self.name,'released by',threading.currentThread()
        if self.verbose:
            frame = sys._getframe()
            traceback.print_stack(frame)
            print '-='*20
        self._lock.release()

    def __enter__(self):
        print '__enter__',
        self.acquire()

    def __exit__(self,etype,eval,etb):
        print '__exit__',
        self.release()
        if etype:
            print '*****',self.name,'error on __exit__',threading.currentThread()
            raise 
