#!/usr/bin/env python
import numarray as na
from numarray import *
from numarray.linear_algebra import *
import sys

# Extract (linear) camera parameters.

def cross(a,b):
    cross = []
    cross.append( a[1]*b[2]-a[2]*b[1] )
    cross.append( a[2]*b[0]-a[0]*b[2] )
    cross.append( a[0]*b[1]-a[1]*b[0] )
    return asarray(cross)

def norm(a):
    return sqrt(sum(a**2))

def rq(X):
    Qt, Rt = qr_decomposition(transpose(X))
    Rt = transpose(Rt)
    Qt = transpose(Qt)

    Qu = []

    Qu.append( cross(Rt[1,:], Rt[2,:] ) )
    Qu[0] = Qu[0]/norm(Qu[0])

    Qu.append( cross(Qu[0], Rt[2,:] ) )
    Qu[1] = Qu[1]/norm(Qu[1])

    Qu.append( cross(Qu[0], Qu[1] ) )

    R = matrixmultiply( Rt, transpose(Qu))
    Q = matrixmultiply( Qu, Qt )

    return R, Q

####################################################################

# sample data from "Multiple View Geometry in Computer Vision" Hartley
# and Zisserman, example 6.2, p. 163

if len( sys.argv ) < 2:
    P = array( [[ 3.53553e2,   3.39645e2,  2.77744e2,  -1.44946e6 ],
                [-1.03528e2,   2.33212e1,  4.59607e2,  -6.32525e5 ],
                [ 7.07107e-1, -3.53553e-1, 6.12372e-1, -9.18559e2 ]] )
else:
    def load_ascii_matrix(filename):
        fd=open(filename,mode='rb')
        buf = fd.read()
        lines = buf.split('\n')[:-1]
        return na.array([map(float,line.split()) for line in lines])
    P=load_ascii_matrix( sys.argv[1] )

# camera center
X = determinant( [ P[:,1], P[:,2], P[:,3] ] )
Y = -determinant( [ P[:,0], P[:,2], P[:,3] ] )
Z = determinant( [ P[:,0], P[:,1], P[:,3] ] )
T = -determinant( [ P[:,0], P[:,1], P[:,2] ] )

C_ = transpose(array( [[ X/T, Y/T, Z/T ]] ))

M = P[:,:3]

# separate internal parameters K from rotation matrix R
K,R = rq(M)

t = matrixmultiply( -R, C_ )

# reconstruct P via eqn 6.8 (p. 156)
P_ = matrixmultiply( K, concatenate( (R, t), axis=1 ) )

show_results = True
if show_results:
    print 'P (original):'
    print P
    print

    print 'C~ (center):'
    print C_
    print

    print 'K (calibration):'
    print K
    print

    print 'R (orientation):'
    print R
    print

    print 't:'
    print t
    print

    print 'P (reconstructed):'
    print P_
    print

#############################################

print '-='*20
print 'world to camera, with 3x4 P matrix:'
X = transpose(array([
    [1,2,3,1],
    [4,5,6,1],
    [7,8,9,1],
    [4,5,6,1],
    [7,8,9,1],
    ]))

X_ = X[:3,:]

print 'X'
print X
print 
x = matrixmultiply(P,X)

print 'x'
print x
print

print '-='*20
print 'world to camera, with 3x3 R matrix (and t):'
# camera center to origin of coordinate system
Xcam = matrixmultiply(R,X_)+t
print 'Xcam (world coordinates translated and rotated to camera origin):'
print Xcam
print

# now camera parameters are 3x3 matrix
# which for which the inverse may be found
x = matrixmultiply(K,Xcam)
print 'x'
print x
print

print '-='*20
print 'camera to world, with 3x3 R matrix (and t):'
Ki = inverse(K)
Xcam2 = matrixmultiply(Ki,x)
print 'Xcam2'
print Xcam2
print

print 'Xcam2-t'
print Xcam2-t
print

X2 = matrixmultiply(inverse(R),Xcam2-t)
print 'X2'
print X2
print
