from __future__ import division
import result_browser
import pylab
import matplotlib.axes
import numpy as nx
import math, glob, time
import circstats
from math import pi

try:
    import rpy1
    have_rpy = True
except ImportError:
    have_rpy = False

if have_rpy:
    R = rpy.r
    R.library("circular")

R2D = 180/pi
D2R = pi/180

# find segments to use
analysis_file = open('strict_data.txt','r')
f_segments = [line.strip().split() for line in analysis_file.readlines()]

h5files = {}
parsed = {}

heading_early = {}
heading_late = {}
turn_angle = {}
early_xvel = {}
early_yvel = {}

late_xvel = {}
late_yvel = {}

for line in f_segments:
    upwind, fstart, trig_fno, fend, h5filename, tf_hz = line
    upwind = bool(upwind)
    fstart = int(fstart)
    trig_fno = int(trig_fno)
    fend = int(fend)
    tf_hz = float(tf_hz)
    if h5filename not in h5files:
        h5files[h5filename] = result_browser.get_results(h5filename)
        f,xyz,L,err,ts = result_browser.get_f_xyz_L_err( h5files[h5filename],include_timestamps=True )
        parsed[h5filename] = f,xyz,L,err,ts

    results = h5files[h5filename]
    f,xyz,L,err,timestamps = parsed[h5filename]

    early_start = trig_fno-10
    early_end = trig_fno

    late_start = trig_fno+15
    late_end = trig_fno+25

    early_cond = (early_start <= f) & (f<=early_end)
    late_cond = (late_start <= f) & (f<=late_end)
    
    early_times = timestamps[early_cond]
    late_times = timestamps[late_cond]

    early_dur = early_times[-1]-early_times[0]
    late_dur = late_times[-1]-late_times[0]
    
    early_xyz = xyz[early_cond]
    late_xyz = xyz[late_cond]
    
    xyz_dist_early = (early_xyz[-1]-early_xyz[0])/1000.0
    xyz_dist_late = (late_xyz[-1]-late_xyz[0])/1000.0

    heading_early.setdefault(tf_hz,[]).append( math.atan2( xyz_dist_early[1], xyz_dist_early[0] ) )
    heading_late.setdefault(tf_hz,[]).append( math.atan2( xyz_dist_late[1], xyz_dist_late[0] ) )

    turn_angle.setdefault(tf_hz,[]).append(  (heading_late[tf_hz][-1] -
                                              heading_early[tf_hz][-1])%(2*pi) )
    
    early_xvel.setdefault(tf_hz,[]).append( xyz_dist_early[0]/early_dur )
    early_yvel.setdefault(tf_hz,[]).append( xyz_dist_early[1]/early_dur )
    late_xvel.setdefault(tf_hz,[]).append( xyz_dist_late[0]/late_dur )
    late_yvel.setdefault(tf_hz,[]).append( xyz_dist_late[1]/late_dur )
    
# convert data to numpy
for d in [heading_early,
          heading_late,
          turn_angle,
          early_xvel,
          early_yvel,
          late_xvel,
          late_yvel]:
    for tf_hz in d.keys():
        d[tf_hz] = nx.asarray(d[tf_hz])
        
if 1:
    pylab.figure()#figsize=(8,8/(2/3)))
    for tf_hz, col, title in [(0.0,0,'no FOE'),
                              (5.0,1,'FOE'),
                              ]:
        downwind_cond = early_xvel[tf_hz] < -0.2 # m/sec
        slow_cond = (-0.2 <= early_xvel[tf_hz]) & (early_xvel[tf_hz] < 0.2) # m/sec
        upwind_cond = (0.2 <= early_xvel[tf_hz])

        downwind_headling_late = heading_late[tf_hz][downwind_cond]
        slow_headling_late = heading_late[tf_hz][slow_cond]
        upwind_headling_late = heading_late[tf_hz][upwind_cond]

        ax = pylab.subplot(3,2,col+1,frameon=False)#,polar=True)
        ax.set_title(title)
        circstats.raw_data_plot(ax,downwind_headling_late,marker='.',linestyle='None')
        mu = circstats.mle_vonmises(downwind_headling_late)['mu']
        circstats.raw_data_plot(ax,[mu],marker='*',linestyle='None',r=0.8)

        ax = pylab.subplot(3,2,col+3,frameon=False)#,polar=True)
        circstats.raw_data_plot(ax,slow_headling_late,marker='.',linestyle='None')
        mu = circstats.mle_vonmises(slow_headling_late)['mu']
        circstats.raw_data_plot(ax,[mu],marker='*',linestyle='None',r=0.8)

        ax = pylab.subplot(3,2,col+5,frameon=False)#,polar=True)
        circstats.raw_data_plot(ax,upwind_headling_late,marker='.',linestyle='None')
        mu = circstats.mle_vonmises(upwind_headling_late)['mu']
        circstats.raw_data_plot(ax,[mu],marker='*',linestyle='None',r=0.8)
    
if 0:
    class ToR:
        def __init__(self):
            self.toR_fds = {}
        def __call__(self,name,arr,rfile,circular=None):
            # remember if we did manipulations to R data file
            if rfile not in self.toR_fds:
                self.toR_fds[rfile] = {}

            valstr = 'c(%s)'%(', '.join(map(repr,arr)),)
            if circular is not None:
                if not self.toR_fds[rfile].get('loaded circular',False):
                    print >>rfile,'library("circular")'
                    self.toR_fds[rfile]['loaded circular'] = True
                if circular.lower().startswith('deg'):
                    valstr = 'circular(%s,units="degrees")'%valstr
                elif circular.lower().startswith('rad'):
                    valstr = 'circular(%s,units="radians")'%valstr
                else:
                    raise ValueError("unknown circular format")
            print >>rfile,'%s <- %s'%(name,valstr)
            
    toR = ToR()
    tf_hz = 0.0
    name = {0.0:'still',
            5.0:'fast',
            }
    rfile = open('data.r','w')
    for tf_hz in [0.0,5.0]:
        toR('heading_early_%s'%name[tf_hz],heading_early[tf_hz],rfile,circular='rad')
        toR('heading_late_%s'%name[tf_hz],heading_late[tf_hz],rfile,circular='rad')
        toR('xvel_early_%s'%name[tf_hz],early_xvel[tf_hz],rfile)
    rfile.close()

if 0:
    pylab.figure()
    for tf_hz, fmt, fmt_fit in [(0.0,'r.','r-'),
                                (5.0,'b.','b-'),
                                ]:
        pylab.subplot(4,2,1)
        pylab.plot( early_xvel[tf_hz], turn_angle[tf_hz], fmt )
        pylab.xlabel('initial x vel (m/sec)')
        pylab.axvline(0.0,color='k')
        pylab.ylabel('turn angle (deg)')
        pylab.axhline(0.0,color='k')
        pylab.axhline(360.0,color='k')

        pylab.subplot(4,2,2)
        pylab.plot( heading_early[tf_hz], heading_late[tf_hz], fmt )
        pylab.xlabel('initial heading (rad)')
        pylab.axvline(-180.0,color='k')
        pylab.axvline(0.0,color='k')
        pylab.axvline(180.0,color='k')
        pylab.ylabel('late heading (rad)')
        pylab.axhline(-180.0,color='k')
        pylab.axhline(0.0,color='k')
        pylab.axhline(180.0,color='k')

        pylab.subplot(4,2,3)
        pylab.plot( early_xvel[tf_hz], heading_early[tf_hz], fmt )
        pylab.xlabel('initial x vel (m/sec)')
        pylab.axvline(0.0,color='k')
        pylab.ylabel('initial heading (rad)')
        pylab.axhline(-180.0,color='k')
        pylab.axhline(0.0,color='k')
        pylab.axhline(180.0,color='k')

        pylab.subplot(4,2,4)
        pylab.plot( early_xvel[tf_hz], heading_late[tf_hz], fmt )
        pylab.xlabel('initial x vel (m/sec)')
        pylab.axvline(0.0,color='k')
        pylab.ylabel('late heading (rad)')
        pylab.axhline(-180.0,color='k')
        pylab.axhline(0.0,color='k')
        pylab.axhline(180.0,color='k')
        if have_rpy:
            # do regression
            regr = R.lm_circular( nx.array(heading_late[tf_hz])*D2R,
                                  early_xvel[tf_hz], [1.0],
                                  type="c-l", verbose=True )
            mu = regr['mu']
            beta = regr['coefficients']
            
            # fit
            x = pylab.linspace(-1,.6,100)
            yfit = mu + 2*nx.arctan( x*beta )
            pylab.plot( x,yfit*R2D,fmt_fit)

        pylab.subplot(4,2,5)
        pylab.plot( early_xvel[tf_hz], early_yvel[tf_hz], fmt )
        pylab.xlabel('initial x vel (m/sec)')
        pylab.axvline(0.0,color='k')
        pylab.ylabel('initial y vel (m/sec)')
        pylab.axhline(0.0,color='k')

        pylab.subplot(4,2,6)
        pylab.plot( early_xvel[tf_hz], late_yvel[tf_hz], fmt )
        pylab.xlabel('initial x vel (m/sec)')
        pylab.axvline(0.0,color='k')
        pylab.ylabel('late y vel (m/sec)')
        pylab.axhline(0.0,color='k')

        pylab.subplot(4,2,7)
        pylab.plot( early_yvel[tf_hz], late_yvel[tf_hz], fmt )
        pylab.xlabel('initial y vel (m/sec)')
        pylab.axvline(0.0,color='k')
        pylab.ylabel('late y vel (m/sec)')
        pylab.axhline(0.0,color='k')

        pylab.subplot(4,2,8)
        pylab.plot( [0],[0],fmt,label='TF %.0f Hz'%tf_hz)
    pylab.legend()

if 0:
    pylab.figure()
    for tf_hz, fmt, fmt2 in [(0.0,'r-','r.'),
                             (5.0,'b-','b.'),
                             ]:
        pylab.subplot(1,1,1)
        for ex, lx, ey, ly in zip(early_xvel[tf_hz],late_xvel[tf_hz],
                                  early_yvel[tf_hz],late_yvel[tf_hz]):
            pylab.plot( [ex,lx],[ey,ly],fmt)
            pylab.plot( [lx],[ly],fmt2)
pylab.show()
