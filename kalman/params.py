import numpy

# distance units are in m
# time units are in sec
# thus, velocity is (m/sec)

ss = 9 # length of state vector (state size)
os = 3 # length of observation vector (observation size)

dt = 0.01 # sec
dt2 = dt**2

# process update matrix (time evolution update matrix)
A = numpy.array([[   1. ,    0. ,    0. ,   dt  ,    0. ,    0. ,  dt2  ,    0. ,    0. ],
                 [   0. ,    1. ,    0. ,    0. ,   dt  ,    0. ,    0. ,  dt2  ,    0. ],
                 [   0. ,    0. ,    1. ,    0. ,    0. ,   dt  ,    0. ,    0. ,  dt2  ],
                 [   0. ,    0. ,    0. ,    1. ,    0. ,    0. ,   dt  ,    0. ,    0. ],
                 [   0. ,    0. ,    0. ,    0. ,    1. ,    0. ,    0. ,   dt  ,    0. ],
                 [   0. ,    0. ,    0. ,    0. ,    0. ,    1. ,    0. ,    0. ,   dt  ],
                 [   0. ,    0. ,    0. ,    0. ,    0. ,    0. ,    0.1,    0. ,    0. ],
                 [   0. ,    0. ,    0. ,    0. ,    0. ,    0. ,    0. ,    0.1,    0. ],
                 [   0. ,    0. ,    0. ,    0. ,    0. ,    0. ,    0. ,    0. ,    0.1]])

# measurement prediction matrix
C = numpy.zeros((os,ss))
C[:os,:os] = numpy.eye(os) # directly measure x,y,z positions

# process covariance
Q = numpy.zeros((ss,ss))
for i in range(6,9):
    Q[i,i] = 10.0 # acceleration noise (near (3.16m*sec**-2)**2)

# measurement noise covariance matrix
R = 1e-6*numpy.eye(os) # (1mm)**2 = (0.001m)**2
