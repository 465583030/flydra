# $Id$
import os, glob, sys, math
opj=os.path.join
import numpy as nx
import numpy
import sets
import flydra.reconstruct_utils as reconstruct_utils # in pyrex/C for speed
import time
from flydra.common_variables import MINIMUM_ECCENTRICITY
import scipy.linalg
import traceback

L_i = nx.array([0,0,0,1,3,2])
L_j = nx.array([1,2,3,2,1,3])

def my_rq(M):
    """RQ decomposition, ensures diagonal of R is positive"""
    R,K = scipy.linalg.rq(M)
    n = R.shape[0]
    for i in range(n):
        if R[i,i]<0:
            # I checked this with Mathematica. Works if R is upper-triangular.
            R[:,i] = -R[:,i]
            K[i,:] = -K[i,:]
##    if R[0,0]<0:
##        # I checked this with Mathematica. Works if R is upper-triangular.
##        R[0,0] = -R[0,0]
##        K[0,:] = -K[0,:]
    return R,K

def load_ascii_matrix(filename):
    fd=open(filename,mode='rb')
    buf = fd.read()
    fd.close()
    lines = buf.split('\n')[:-1]
    return nx.array([map(float,line.split()) for line in lines])

def save_ascii_matrix(M,fd):
    def fmt(f):
        return '% 8e'%f
    A = nx.asarray(M)
    if len(A.shape) == 1:
        A=nx.reshape(A, (1,A.shape[0]) )

    close_file = False
    if type(fd) == str:
        fd = open(fd,mode='wb')
        close_file = True

    for i in range(A.shape[0]):
        buf = ' '.join( map( fmt, A[i,:] ) )
        fd.write( buf )
        fd.write( '\n' )
    if close_file:
        fd.close()

def as_column(x):
    x = nx.asarray(x)
    if len(x.shape) == 1:
        x = nx.reshape(x, (x.shape[0],1) )
    return x

def as_vec(x):
    x = nx.asarray(x)
    if len(x.shape) == 1:
        return x
    elif len(x.shape) == 2:
        long_dim = x.shape[0]+x.shape[1]-1
        if (x.shape[0]*x.shape[1]) != long_dim:
            # more than 1 rows or columns
            raise ValueError("cannot convert to vector")
    else:
        raise ValueError("cannot convert to vector")
    return nx.reshape(x,(longdim,))

def Lcoords2Lmatrix(Lcoords):
    Lcoords = nx.asarray(Lcoords)
    Lmatrix = nx.zeros((4,4),nx.float64)
    Lmatrix[L_i,L_j]=Lcoords
    Lmatrix[L_j,L_i]=-Lcoords
    return Lmatrix

def Lmatrix2Lcoords(Lmatrix):
    return Lmatrix[L_i,L_j]

def pts2Lmatrix(A,B):
    A = as_column(A)
    B = as_column(B)
    L = nx.dot(A,nx.transpose(B)) - nx.dot(B,nx.transpose(A))
    return L

def pts2Lcoords(A,B):
    return Lmatrix2Lcoords(pts2Lmatrix(A,B))

def norm_vec(V):
    Va = nx.asarray(V)
    if len(Va.shape)==1:
        # vector
        U = Va/math.sqrt(Va[0]**2 + Va[1]**2 + Va[2]**2) # normalize
    else:
        assert Va.shape[1] == 3
        Vamags = nx.sqrt(Va[:,0]**2 + Va[:,1]**2 + Va[:,2]**2)
        U = Va/Vamags[:,nx.newaxis]
    return U

def line_direction(Lcoords):
    """convert from Pluecker coordinates to a direction"""
    L = nx.asarray(Lcoords)
    if len(L.shape)==1:
        # single line coord
        U = nx.array((-L[2], L[4], -L[5]))
    else:
        assert L.shape[1] == 6
        # XXX could speed up with concatenate:
        U = nx.transpose(nx.array((-L[:,2], L[:,4], -L[:,5])))
    return norm_vec(U)

def pluecker_from_verts(A,B):
    """

    See Hartley & Zisserman (2003) p. 70
    """
    if len(A)==3:
        A = A[0], A[1], A[2], 1.0
    if len(B)==3:
        B = B[0], B[1], B[2], 1.0
    A=nx.reshape(A,(4,1))
    B=nx.reshape(B,(4,1))
    L = nx.dot(A,nx.transpose(B)) - nx.dot(B,nx.transpose(A))
    return Lmatrix2Lcoords(L)

def pmat2cam_center(P):
    """

    See Hartley & Zisserman (2003) p. 163
    """
    assert P.shape == (3,4)
    determinant = numpy.linalg.det

    # camera center
    X = determinant( [ P[:,1], P[:,2], P[:,3] ] )
    Y = -determinant( [ P[:,0], P[:,2], P[:,3] ] )
    Z = determinant( [ P[:,0], P[:,1], P[:,3] ] )
    T = -determinant( [ P[:,0], P[:,1], P[:,2] ] )

    C_ = nx.transpose(nx.array( [[ X/T, Y/T, Z/T ]] ))
    return C_

def setOfSubsets(L):
    """find all subsets of L

    from Alex Martelli:
    http://mail.python.org/pipermail/python-list/2001-January/067815.html

    This is also called the power set:
    http://en.wikipedia.org/wiki/Power_set
    """
    N = len(L)
    return [ [ L[i] for i in range(N)
                if X & (1L<<i) ]
        for X in range(2**N) ]

def normalize_pmat(pmat):
    pmat_orig = pmat
    M = pmat[:,:3]
    t = pmat[:,3,numpy.newaxis]
    K,R = my_rq(M)
    eps = 1e-6
    if abs(K[2,2]-1.0)>eps:
        pmat = pmat/K[2,2]
    assert numpy.allclose(pmat2cam_center(pmat_orig),pmat2cam_center(pmat))
    return pmat

class SingleCameraCalibration:
    def __init__(self,
                 cam_id=None, # non-optional
                 Pmat=None,   # non-optional
                 res=None,    # non-optional
                 pp=None,
                 helper=None,
                 scale_factor=None # scale_factor is for conversion to meters (e.g. should be 1e-3 if your units are mm)
                 ):
        """

        Required arguments
        ------------------
        cam_id - string identifying camera
        Pmat - camera calibration matrix (3 x 4)
        res - resolution (width,height)
        pp - pricipal point (point on image plane on optical axis) (x,y)

        Optional arguments
        ------------------
        helper - has camera distortion parameters
        """
        if type(cam_id) != str:
            raise TypeError('cam_id must be string')
        pm = numpy.asarray(Pmat)
        if pm.shape != (3,4):
            raise ValueError('Pmat must have shape (3,4)')
        if len(res) != 2:
            raise ValueError('len(res) must be 2')

        self.cam_id=cam_id
        self.Pmat=Pmat
        self.res=res

        pp_guess = False
        if pp is None:
            pp = self.res[0]/2.0,self.res[1]/2.0
            pp_guess = True
        if len(pp) != 2:
            raise ValueError('len(pp) must be 2')
        self.pp = pp

        if 1:
            center = self.get_image_center()
            if ((pp[0]-center[0])**2 + (pp[1]-center[1])**2 ) > 5:
                print 'WARNING: principal point and image center seriously misaligned'
                print '  pp: %s, center: %s'%(str(pp),str(center))
                if pp_guess:

                    print '  (note: one of these parameters was guessed ' \
                          'as the midpoint of the specified image resolution, ' \
                          'and could be wrong)'

        if helper is None:
            M = numpy.asarray(Pmat)
            cam_center = pmat2cam_center(M)

            intrinsic_parameters, cam_rotation = my_rq(M[:,:3])
            #intrinsic_parameters = intrinsic_parameters/intrinsic_parameters[2,2] # normalize
            eps = 1e-6
            if abs(intrinsic_parameters[2,2]-1.0)>eps:
                print 'WARNING: expected last row/col of intrinsic parameter matrix to be unity'
                print 'intrinsic_parameters[2,2]',intrinsic_parameters[2,2]
                raise ValueError('expected last row/col of intrinsic parameter matrix to be unity')

            fc1 = intrinsic_parameters[0,0]
            cc1 = intrinsic_parameters[0,2]
            fc2 = intrinsic_parameters[1,1]
            cc2 = intrinsic_parameters[1,2]

            helper = reconstruct_utils.ReconstructHelper(fc1,fc2, # focal length
                                                         cc1,cc2, # image center
                                                         0,0, # radial distortion
                                                         0,0) # tangential distortion
        if not isinstance(helper,reconstruct_utils.ReconstructHelper):
            raise TypeError('helper must be reconstruct_utils.ReconstructHelper instance')
        self.helper = helper

        self.pmat_inv = numpy.linalg.pinv(self.Pmat)
        self.scale_factor = scale_factor

    def __ne__(self,other):
        return not (self==other)

    def __eq__(self,other):
        return (numpy.allclose(self.pp,other.pp) and
                (self.cam_id == other.cam_id) and
                numpy.allclose(self.Pmat,other.Pmat) and
                numpy.allclose(self.res,other.res) and
                self.helper == other.helper)

    def get_pmat(self):
        return self.Pmat

    def get_scaled(self,scale_factor):
        """change units (e.g. from mm to meters)

        Note: some of the data structures are shared with the unscaled original
        """
        scale_array = numpy.ones((3,4))
        scale_array[:,3] = scale_factor # mulitply last column by scale_factor
        scaled_Pmat = scale_array*self.Pmat # element-wise multiplication

        if self.scale_factor is not None:
            new_scale_factor = self.scale_factor/scale_factor
        else:
            new_scale_factor = None

        scaled = SingleCameraCalibration(self.cam_id,
                                         scaled_Pmat,
                                         self.res,
                                         self.pp,
                                         self.helper,
                                         scale_factor=new_scale_factor)
        return scaled

    def get_cam_center(self):
        """get the 3D location of the camera center in world coordinates"""
        # should be called get_camera_center?
        return pmat2cam_center(self.Pmat)
    def get_M(self):
        """return parameters except extrinsic translation params"""
        return self.Pmat[:,:3]
    def get_t(self):
        """return extrinsic translation parameters"""
        return self.Pmat[:,3,numpy.newaxis]
    def get_KR(self):
        """return intrinsic params (K) and extrinsic rotation/scale params (R)"""
        M = self.get_M()
        K,R = my_rq(M)
##        if K[2,2] != 0.0:
##            # normalize K
##            K = K/K[2,2]
        return K,R
    def get_mean_focal_length(self):
        K,R = self.get_KR()
        return (K[0,0]+K[1,1])/2.0
    def get_image_center(self):
        K,R = self.get_KR()
        return K[0,2], K[1,2]
    def get_extrinsic_parameter_matrix(self):
        """contains rotation and translation information"""
        C_ = self.get_cam_center()
        K,R = self.get_KR()
        t = numpy.dot( -R, C_ )
        ext = numpy.concatenate( (R, t), axis=1 )
        return ext

    def get_example_3d_point_creating_image_point(self,image_point,w_val=1.0):
        # project back through principal point to get 3D line
        c1 = self.get_cam_center()[:,0]

        x2d = (image_point[0],image_point[1],1.0)
        c2 = numpy.dot(self.pmat_inv, as_column(x2d))[:,0]
        c2 = c2[:3]/c2[3]

        direction = c2-c1
        direction = direction/numpy.sqrt(numpy.sum(direction**2))
        c3 = c1+direction*w_val
        return c3

    def get_optical_axis(self):
        # project back through principal point to get 3D line
        #import flydra.geom as geom
        import flydra.fastgeom as geom
        c1 = self.get_cam_center()[:,0]

        x2d = (self.pp[0],self.pp[1],1.0)
        c2 = numpy.dot(self.pmat_inv, as_column(x2d))[:,0]
        c2 = c2[:3]/c2[3]
        c1 = geom.ThreeTuple(c1)
        c2 = geom.ThreeTuple(c2)
        return geom.line_from_points( c1, c2 )

    def get_up_vector(self):
        # create up vector from image plane
        x2d_a = (self.pp[0],self.pp[1],1.0)
        c2_a = numpy.dot(self.pmat_inv, as_column(x2d_a))[:,0]
        c2_a = c2_a[:3]/c2_a[3]

        x2d_b = (self.pp[0],self.pp[1]+1,1.0)
        c2_b = numpy.dot(self.pmat_inv, as_column(x2d_b))[:,0]
        c2_b = c2_b[:3]/c2_b[3]

        up_dir = c2_b-c2_a
        return norm_vec(up_dir)

    def to_file(self,filename):
        fd = open(filename,'wb')
        fd.write(    'cam_id = "%s"\n'%self.cam_id)

        fd.write(    'pmat = [\n')
        for row in self.Pmat:
            fd.write('        [%s, %s, %s, %s],\n'%tuple([repr(x) for x in row]))
        fd.write(    '       ]\n')

        fd.write(    'res = (%d,%d)\n'%(self.res[0],self.res[1]))
        fd.write(    'pp = (%s,%s)\n'%(repr(self.pp[0]),repr(self.pp[1])))

        fd.write(    'K = [\n')
        for row in self.helper.get_K():
            fd.write('     [%s, %s, %s],\n'%tuple([repr(x) for x in row]))
        fd.write(    '    ]\n')

        k1,k2,p1,p2 = self.helper.get_nlparams()
        fd.write(    'radial_params = %s, %s\n'%(repr(k1),repr(k2)))
        fd.write(    'tangential_params = %s, %s\n'%(repr(p1),repr(p2)))

def SingleCameraCalibration_fromfile(filename):
    params={}
    execfile(filename,params)
    pmat = numpy.asarray(params['pmat']) # XXX redundant information in pmat and K
    K = numpy.asarray(params['K'])
    cam_id = params['cam_id']
    res = params['res']
    pp = params['pp']
    k1,k2 = params['radial_params']
    p1,p2 = params['tangential_params']

    fc1 = K[0,0]
    cc1 = K[0,2]
    fc2 = K[1,1]
    cc2 = K[1,2]

    helper = reconstruct_utils.ReconstructHelper(fc1,fc2, # focal length
                                                 cc1,cc2, # image center
                                                 k1, k2, # radial distortion
                                                 p1, p2) # tangential distortion
    return SingleCameraCalibration(cam_id=cam_id,
                                   Pmat=pmat,
                                   res=res,
                                   pp=pp,
                                   helper=helper)

def SingleCameraCalibration_from_basic_pmat(pmat,**kw):
    M = numpy.asarray(pmat)
    cam_center = pmat2cam_center(M)

    intrinsic_parameters, cam_rotation = my_rq(M[:,:3])
    #intrinsic_parameters = intrinsic_parameters/intrinsic_parameters[2,2] # normalize
    if intrinsic_parameters[2,2]!=1.0:
        print 'WARNING: expected last row/col of intrinsic parameter matrix to be unity'
        raise ValueError('expected last row/col of intrinsic parameter matrix to be unity')

    # (K = intrinsic parameters)

    #cam_translation = numpy.dot( -cam_rotation, cam_center )
    #extrinsic_parameters = numpy.concatenate( (cam_rotation, cam_translation), axis=1 )

    #mean_focal_length = (intrinsic_parameters[0,0]+intrinsic_parameters[1,1])/2.0
    #center = intrinsic_parameters[0,2], intrinsic_parameters[1,2]

    #focalLength, center = compute_stuff_from_cal_matrix(cal)

    fc1 = intrinsic_parameters[0,0]
    cc1 = intrinsic_parameters[0,2]
    fc2 = intrinsic_parameters[1,1]
    cc2 = intrinsic_parameters[1,2]

    helper = reconstruct_utils.ReconstructHelper(fc1,fc2, # focal length
                                                 cc1,cc2, # image center
                                                 0,0, # radial distortion
                                                 0,0) # tangential distortion
    return SingleCameraCalibration(Pmat=M,
                                   helper=helper,
                                   **kw)

class Reconstructor:
    def __init__(self,
                 cal_source = None,
                 do_normalize_pmat=True,
                 ):
        """
        inputs
        ======
        cal_source - the source of the calibration. can be the output of MultiCamSelfCal, a pytables file, etc.
        do_normalize_pmat - whether the pmat is normalized such that the intrinsic parameters are in the expected form

        """
        self.cal_source = cal_source

        if isinstance(self.cal_source,str) or isinstance(self.cal_source,unicode):
            if not self.cal_source.endswith('h5'):
                self.cal_source_type = 'normal files'
            else:
                self.cal_source_type = 'pytables filename'
        elif hasattr(self.cal_source,'__len__'): # is sequence
            for i in range(len(self.cal_source)):
                if not isinstance(self.cal_source[i],SingleCameraCalibration):
                    raise TypeError('If calsource is a sequence, it must '
                                    'be a string specifying calibration '
                                    'directory or a sequence of '
                                    'SingleCameraCalibration instances.')
            self.cal_source_type = 'SingleCameraCalibration instances'
        else:
            self.cal_source_type = 'pytables'

        if self.cal_source_type == 'pytables filename':
            import tables as PT # PyTables
            use_cal_source = PT.openFile(self.cal_source,mode='r')
            self.cal_source_type = 'pytables'
        else:
            use_cal_source = self.cal_source


        if self.cal_source_type == 'normal files':
            fd = open(os.path.join(use_cal_source,'camera_order.txt'),'r')
            cam_ids = fd.read().split('\n')
            fd.close()
            if cam_ids[-1] == '': del cam_ids[-1] # remove blank line
        elif self.cal_source_type == 'pytables':
            import tables as PT # PyTables
            assert type(use_cal_source)==PT.File
            results = use_cal_source
            nodes = results.root.calibration.pmat._f_listNodes()
            cam_ids = []
            for node in nodes:
                cam_ids.append( node.name )
        elif self.cal_source_type=='SingleCameraCalibration instances':
            cam_ids = [scci.cam_id for scci in use_cal_source]

        N = len(cam_ids)
        # load calibration matrices
        self.Pmat = {}
        self.Res = {}
        self._helper = {}

        # values for converting to meters
        self._known_units2scale_factor = {
            'millimeters':1e-3,
            }

        self._scale_factor2known_units = {}
        for tmp_unit, tmp_scale_factor in self._known_units2scale_factor.iteritems():
            self._scale_factor2known_units[tmp_scale_factor] = tmp_unit

        if self.cal_source_type == 'normal files':
            res_fd = open(os.path.join(use_cal_source,'Res.dat'),'r')
            for i, cam_id in enumerate(cam_ids):
                fname = 'camera%d.Pmat.cal'%(i+1)
                pmat = load_ascii_matrix(opj(use_cal_source,fname)) # 3 rows x 4 columns
                if do_normalize_pmat:
                    pmat_orig = pmat
                    pmat = normalize_pmat(pmat)
##                    if not numpy.allclose(pmat_orig,pmat):
##                        assert numpy.allclose(pmat2cam_center(pmat_orig),pmat2cam_center(pmat))
##                        #print 'normalized pmat, but camera center should  changed for %s'%cam_id
                self.Pmat[cam_id] = pmat
                self.Res[cam_id] = map(int,res_fd.readline().split())
            res_fd.close()

            # load non linear parameters
            rad_files = glob.glob(os.path.join(use_cal_source,'*.rad'))
            assert len(rad_files) < 10
            rad_files.sort() # cheap trick to associate camera number with file
            for cam_id, filename in map( None, cam_ids, rad_files ):
                if filename is None:
                    print 'WARNING: no non-linear data (e.g. radial distortion) in calibration for %s'%cam_id
                    self._helper[cam_id] = SingleCameraCalibration_from_basic_pmat(
                        self.Pmat[cam_id],
                        cam_id=cam_id,
                        res=self.Res[cam_id]).helper
                    continue

                params = {}
                execfile(filename,params)
                self._helper[cam_id] = reconstruct_utils.ReconstructHelper(
                    params['K11'], params['K22'], params['K13'], params['K23'],
                    params['kc1'], params['kc2'], params['kc3'], params['kc4'])

                if 1:
                    ## check consistency of .rad file with calibration matrix
                    pmat = self.Pmat[cam_id]
                    intrinsic_parameters, cam_rotation = my_rq(pmat[:,:3])
                    eps = 1e-6
                    if abs(intrinsic_parameters[2,2]-1.0)>eps:
                        print 'WARNING: expected last row/col of intrinsic parameter matrix to be unity'
                        print 'intrinsic_parameters[2,2]',intrinsic_parameters[2,2]
##                        if do_normalize_pmat:
##                            print 'WARNING: normalizing the intrinsic parameters'
##                            intrinsic_parameters = intrinsic_parameters/intrinsic_parameters[2,2] # normalize
##                        else:
                        raise ValueError('expected last row/col of intrinsic parameter matrix to be unity')

                    fc1 = intrinsic_parameters[0,0]
                    cc1 = intrinsic_parameters[0,2]
                    fc2 = intrinsic_parameters[1,1]
                    cc2 = intrinsic_parameters[1,2]
                    if ((fc1 != params['K11']) or
                        (fc2 != params['K22']) or
                        (cc1 != params['K13']) or
                        (cc2 != params['K23'])):
                        print 'WARNING: *.rad file and *.Pmat.cal files differ for',cam_id
                        print "                  .Pmat.cal    .rad"
                        print "focal length X:",fc1,params['K11']
                        print "focal length Y:",fc2,params['K22']
                        print "principal point X:",cc1,params['K13']
                        print "principal point Y:",cc2,params['K23']
                        print

            filename = os.path.join(use_cal_source,'calibration_units.txt')
            if os.path.exists(filename):
                fd = file(filename,'r')
                value = fd.read()
                fd.close()
                value = value.strip()
            else:
                print 'Assuming scale_factor units are millimeters in %s: file %s does not exist'%(
                    __file__,filename)
                value = 'millimeters'

            if value in self._known_units2scale_factor:
                self.scale_factor = self._known_units2scale_factor[value]
            else:
                raise ValueError('Unknown unit "%s"'%value)


        elif self.cal_source_type == 'pytables':
            scale_factors = []
            for cam_id in cam_ids:
                pmat = nx.array(results.root.calibration.pmat.__getattr__(cam_id))
                res = tuple(results.root.calibration.resolution.__getattr__(cam_id))
                K = nx.array(results.root.calibration.intrinsic_linear.__getattr__(cam_id))
                nlparams = tuple(results.root.calibration.intrinsic_nonlinear.__getattr__(cam_id))
                if hasattr(results.root.calibration,'scale_factor2meters'):
                    sf_array = numpy.array(results.root.calibration.scale_factor2meters.__getattr__(cam_id))
                    scale_factors.append(sf_array[0])
                self.Pmat[cam_id] = pmat
                self.Res[cam_id] = res
                self._helper[cam_id] = reconstruct_utils.ReconstructHelper(
                    K[0,0], K[1,1], K[0,2], K[1,2],
                    nlparams[0], nlparams[1], nlparams[2], nlparams[3])

            unique_scale_factors = list(sets.Set(scale_factors))
            if len(unique_scale_factors)==0:
                print 'Assuming scale_factor units are millimeters in pytables',__file__
                self.scale_factor = self._known_units2scale_factor['millimeters']
            elif len(unique_scale_factors)==1:
                self.scale_factor = unique_scale_factors[0]
            else:
                raise NotImplementedError('cannot handle case where each camera has a different scale factor')

        elif self.cal_source_type=='SingleCameraCalibration instances':
            # find instance
            scale_factors = []
            for cam_id in cam_ids:
                for scci in use_cal_source:
                    if scci.cam_id==cam_id:
                        break
                self.Pmat[cam_id] = scci.Pmat
                self.Res[cam_id] = scci.res
                self._helper[cam_id] = scci.helper
                if scci.scale_factor is not None:
                    scale_factors.append( scci.scale_factor )
            unique_scale_factors = list(sets.Set(scale_factors))
            if len(unique_scale_factors)==0:
                print 'Assuming scale_factor units are millimeters in SingleCameraCalibration instances (%s)'%(
                    __file__,)
                self.scale_factor = self._known_units2scale_factor['millimeters']
            elif len(unique_scale_factors)==1:
                self.scale_factor = unique_scale_factors[0]
            else:
                raise NotImplementedError('cannot handle case where each camera has a different scale factor')

        self.pmat_inv = {}
        for cam_id in cam_ids:
            # For speed reasons, make sure self.Pmat has only numpy arrays.
            self.Pmat[cam_id] = numpy.array(self.Pmat[cam_id])

            self.pmat_inv[cam_id] = numpy.linalg.pinv(self.Pmat[cam_id])

        self.cam_combinations = [s for s in setOfSubsets(cam_ids) if len(s) >=2]
        def cmpfunc(a,b):
            if len(a) > len(b):
                return -1
            else:
                return 0
        # order camera combinations from most cameras to least
        self.cam_combinations.sort(cmpfunc)
        self.cam_combinations_by_size = {}
        for cc in self.cam_combinations:
            self.cam_combinations_by_size.setdefault(len(cc),[]).append(cc)
        self.cam_ids = cam_ids
        # fill self._cam_centers_cache
        self._cam_centers_cache = {}
        for cam_id in self.cam_ids:
            self._cam_centers_cache[cam_id] = self.get_camera_center(cam_id)[:,0] # make rank-1

    def __ne__(self,other):
        return not (self==other)

    def __eq__(self,other):
        orig_sccs = [self.get_SingleCameraCalibration(cam_id) for cam_id in self.cam_ids]
        other_sccs = [other.get_SingleCameraCalibration(cam_id) for cam_id in other.cam_ids]
        eq = True
        for my_scc,other_scc in zip(orig_sccs,other_sccs):
            if my_scc != other_scc:
                eq = False
                break
        return eq

    def get_extrinsic_parameter_matrix(self,cam_id):
        scc = self.get_SingleCameraCalibration(cam_id)
        return scc.get_extrinsic_parameter_matrix()

    def get_scaled(self,scale_factor):
        """change units (e.g. from mm to meters)

        Note: some of the data structures are shared with the unscaled original
        """
        # get original calibration
        orig_sccs = [self.get_SingleCameraCalibration(cam_id) for cam_id in self.cam_ids]
        scaled_sccs = [scc.get_scaled(scale_factor) for scc in orig_sccs]
        return Reconstructor(scaled_sccs)

    def get_cam_ids(self):
        return self.cam_ids

    def save_to_files_in_new_directory(self, new_dirname):
        if os.path.exists(new_dirname):
            raise RuntimeError('directory "%s" already exists'%new_dirname)
        os.mkdir(new_dirname)

        fd = open(os.path.join(new_dirname,'camera_order.txt'),'w')
        for cam_id in self.cam_ids:
            fd.write(cam_id+'\n')
        fd.close()

        res_fd = open(os.path.join(new_dirname,'Res.dat'),'w')
        for cam_id in self.cam_ids:
            res_fd.write( ' '.join(map(str,self.Res[cam_id])) + '\n' )
        res_fd.close()

        for i, cam_id in enumerate(self.cam_ids):
            fname = 'camera%d.Pmat.cal'%(i+1)
            pmat_fd = open(os.path.join(new_dirname,fname),'w')
            save_ascii_matrix(self.Pmat[cam_id],pmat_fd)
            pmat_fd.close()

        # non linear parameters
        for i, cam_id in enumerate(self.cam_ids):
            fname = 'basename%d.rad'%(i+1)
            rad_fd = open(os.path.join(new_dirname,fname),'w')
            K = self._helper[cam_id].get_K()
            nlparams = self._helper[cam_id].get_nlparams()
            k1, k2, p1, p2 = nlparams
            rad_fd.write('K11 = %s\n'%repr(K[0,0]))
            rad_fd.write('K12 = %s\n'%repr(K[0,1]))
            rad_fd.write('K13 = %s\n'%repr(K[0,2]))
            rad_fd.write('K21 = %s\n'%repr(K[1,0]))
            rad_fd.write('K22 = %s\n'%repr(K[1,1]))
            rad_fd.write('K23 = %s\n'%repr(K[1,2]))
            rad_fd.write('K31 = %s\n'%repr(K[2,0]))
            rad_fd.write('K32 = %s\n'%repr(K[2,1]))
            rad_fd.write('K33 = %s\n'%repr(K[2,2]))
            rad_fd.write('\n')
            rad_fd.write('kc1 = %s\n'%repr(k1))
            rad_fd.write('kc2 = %s\n'%repr(k2))
            rad_fd.write('kc3 = %s\n'%repr(p1))
            rad_fd.write('kc4 = %s\n'%repr(p2))
            rad_fd.close()

        fd = open(os.path.join(new_dirname,'calibration_units.txt'),mode='w')
        fd.write(self.get_calibration_unit()+'\n')
        fd.close()

    def save_to_h5file(self, h5file, OK_to_delete_old_calibration=False):
        """create groups with calibration information"""

        import tables as PT # pytables
        class AdditionalInfo(PT.IsDescription):
            cal_source_type      = PT.StringCol(20)
            cal_source           = PT.StringCol(80)
            minimum_eccentricity = PT.Float32Col() # record what parameter was used during reconstruction
        pytables_filt = numpy.asarray
        ct = h5file.createTable # shorthand
        root = h5file.root # shorthand

        cam_ids = self.Pmat.keys()
        cam_ids.sort()

        if hasattr(root,'calibration'):
            if OK_to_delete_old_calibration:
                h5file.removeNode( root.calibration, recursive=True )
            else:
                raise RuntimeError('not deleting old calibration.')

        cal_group = h5file.createGroup(root,'calibration')

        pmat_group = h5file.createGroup(cal_group,'pmat')
        for cam_id in cam_ids:
            h5file.createArray(pmat_group, cam_id,
                               pytables_filt(self.get_pmat(cam_id)))
        res_group = h5file.createGroup(cal_group,'resolution')
        for cam_id in cam_ids:
            res = self.get_resolution(cam_id)
            h5file.createArray(res_group, cam_id, pytables_filt(res))

        intlin_group = h5file.createGroup(cal_group,'intrinsic_linear')
        for cam_id in cam_ids:
            intlin = self.get_intrinsic_linear(cam_id)
            h5file.createArray(intlin_group, cam_id, pytables_filt(intlin))

        intnonlin_group = h5file.createGroup(cal_group,'intrinsic_nonlinear')
        for cam_id in cam_ids:
            h5file.createArray(intnonlin_group, cam_id,
                               pytables_filt(self.get_intrinsic_nonlinear(cam_id)))

        scale_group = h5file.createGroup(cal_group,'scale_factor2meters')
        for cam_id in cam_ids:
            h5file.createArray(scale_group, cam_id,
                               pytables_filt([self.get_scale_factor()]))

        h5additional_info = ct(cal_group,'additional_info', AdditionalInfo,
                               '')
        row = h5additional_info.row
        row['cal_source_type'] = self.cal_source_type
        if isinstance(self.cal_source,list):
            row['cal_source'] = '(originally was list - not saved here)'
        else:
            if not isinstance( self.cal_source,PT.File):
                row['cal_source'] = self.cal_source
            else:
                row['cal_source'] = self.cal_source.filename
        row['minimum_eccentricity'] = MINIMUM_ECCENTRICITY
        row.append()
        h5additional_info.flush()

    def get_resolution(self, cam_id):
        return self.Res[cam_id]

    def get_pmat(self, cam_id):
        return self.Pmat[cam_id]

    def get_camera_center(self, cam_id):
        # should be called get_cam_center?
        return pmat2cam_center(self.Pmat[cam_id])

    def get_intrinsic_linear(self, cam_id):
        return self._helper[cam_id].get_K()

    def get_intrinsic_nonlinear(self, cam_id):
        return self._helper[cam_id].get_nlparams()

    def undistort(self, cam_id, x_kk):
        return self._helper[cam_id].undistort(x_kk[0],x_kk[1])

    def distort(self, cam_id, xl):
        return self._helper[cam_id].distort(xl[0],xl[1])

    def get_reconstruct_helper_dict(self):
        return self._helper

    def find3d(self, cam_ids_and_points2d, return_X_coords = True, return_line_coords = True ):
        """Find 3D coordinate using all data given

        Implements a linear triangulation method to find a 3D
        point. For example, see Hartley & Zisserman section 12.2
        (p.312).

        Note: For a function which does hyptothesis testing to
        selectively choose 2D to incorporate, see
        hypothesis_testing_algorithm__find_best_3d() in
        reconstruct_utils.

        """
        svd = scipy.linalg.svd
        # for info on SVD, see Hartley & Zisserman (2003) p. 593 (see
        # also p. 587)

        # Construct matrices
        A=[]
        P=[]
        for m, (cam_id,value_tuple) in enumerate(cam_ids_and_points2d):
            if len(value_tuple)==2:
                # only point information ( no line )
                x,y = value_tuple
                have_line_coords = False
            else:
                # get shape information from each view of a blob:
                x,y,area,slope,eccentricity, p1,p2,p3,p4 = value_tuple
                have_line_coords = True
            if return_X_coords:
                Pmat = self.Pmat[cam_id] # Pmat is 3 rows x 4 columns
                row2 = Pmat[2,:]
                A.append( x*row2 - Pmat[0,:] )
                A.append( y*row2 - Pmat[1,:] )

            if return_line_coords and have_line_coords:
                if eccentricity > MINIMUM_ECCENTRICITY: # require a bit of elongation
                    P.append( (p1,p2,p3,p4) )

        # Calculate best point
        if return_X_coords:
            A=nx.array(A)
            u,d,vt=svd(A)
            X = vt[-1,0:3]/vt[-1,3] # normalize
            if not return_line_coords:
                return X

        if not return_line_coords or len(P) < 2:
            Lcoords = None
        else:
            P = nx.asarray(P)
            # Calculate best line
            try:
                u,d,vt=svd(P,full_matrices=True)
                # "two columns of V corresponding to the two largest singular
                # values span the best rank 2 approximation to A and may be
                # used to define the line of intersection of the planes"
                # (Hartley & Zisserman, p. 323)
                P = vt[0,:] # P,Q are planes (take row because this is transpose(V))
                Q = vt[1,:]

                # directly to Pluecker line coordinates
                Lcoords = ( -(P[3]*Q[2]) + P[2]*Q[3],
                              P[3]*Q[1]  - P[1]*Q[3],
                            -(P[2]*Q[1]) + P[1]*Q[2],
                            -(P[3]*Q[0]) + P[0]*Q[3],
                            -(P[2]*Q[0]) + P[0]*Q[2],
                            -(P[1]*Q[0]) + P[0]*Q[1] )
            except Exception, exc:
                print 'WARNING svd exception:',str(exc)
                Lcoords = None
            except:
                print 'WARNING: unknown error in reconstruct.py'
                print '(you probably have an old version of numarray'
                print 'and SVD did not converge)'
                Lcoords = None
        if return_line_coords:
            if return_X_coords:
                return X, Lcoords
            else:
                return Lcoords

    def find2d(self,cam_id,X,Lcoords=None,distorted=False):
        # see Hartley & Zisserman (2003) p. 449
        if type(X)==tuple or type(X)==list:
            rank1=True
        else:
            rank1 = len(X.shape)==1 # assuming array type
        if rank1:
            # make homogenous coords, rank2
            if len(X) == 3:
                X = nx.array([[X[0]], [X[1]], [X[2]], [1.0]])
            else:
                X = X[:,nx.newaxis] # 4 rows, 1 column
        else:
            X = nx.transpose(X) # 4 rows, N columns
        Pmat = self.Pmat[cam_id]
        x=nx.dot(Pmat,X)

        x = x[0:2,:]/x[2,:] # normalize

        if distorted:
            if rank1:
                xd, yd = self.distort(cam_id, x)
                x[0] = xd
                x[1] = yd
            else:
                N_pts = x.shape[1]
                for i in range(N_pts):
                    xpt = x[:,i]
                    xd, yd = self.distort(cam_id, xpt)
                    x[0,i]=xd
                    x[1,i]=yd

        # XXX The rest of this function hasn't been (recently) checked
        # for >1 points. (i.e. not rank1)

        if Lcoords is not None:
            if distorted:

                # Impossible to distort Lcoords. The image of the line
                # could be distorted downstream.

                raise RuntimeError('cannot (easily) distort line')

            if not rank1:
                raise NotImplementedError('Line reconstruction not yet implemented for rank-2 data')

            # see Hartley & Zisserman (2003) p. 198, eqn 8.2
            L = Lcoords2Lmatrix(Lcoords)
            # XXX could be made faster by pre-computing line projection matrix
            lx = nx.dot(Pmat, nx.dot(L,nx.transpose(Pmat)))
            l3 = lx[2,1], lx[0,2], lx[1,0] #(p. 581)
            return x, l3
        else:
            if rank1:
                # convert back to rank1
                return x[:,0]
            return x

    def find3d_single_cam(self,cam_id,x):
        "see also SingleCameraCalibration.get_example_3d_point_creating_image_point()"""
        return nx.dot(self.pmat_inv[cam_id], as_column(x))

    def get_projected_line_from_2d(self,cam_id,xy):
        "see also SingleCameraCalibration.get_example_3d_point_creating_image_point()"""
        # XXX Could use nullspace method?
        # image of 2d point in 3d space (on optical ray)
        XY = self.find3d_single_cam(cam_id,(xy[0],xy[1],1.0))
        XY = XY[:3,0]/XY[3] # convert to rank1
        C = self._cam_centers_cache[cam_id]
        return pluecker_from_verts(XY,C)

    def get_scale_factor(self):
        return self.scale_factor

    def get_calibration_unit(self):
        return self._scale_factor2known_units[self.scale_factor]

    def get_SingleCameraCalibration(self,cam_id):
        return SingleCameraCalibration(cam_id=cam_id,
                                       Pmat=self.Pmat[cam_id],
                                       res=self.Res[cam_id],
                                       helper=self._helper[cam_id],
                                       scale_factor=self.scale_factor,
                                       )

def test():
    import flydra.generate_fake_calibration as gfc
    recon = gfc.generate_calibration()
    Xs = [[0,0,0],
         [1,2,3],
         [0.5,-.2,.0004]]
    for X in Xs:
        print 'X',X
        Xh = numpy.concatenate((X,[1]))
        print 'Xh',Xh
        for cam_id in recon.cam_ids:
            pmat = recon.get_pmat(cam_id)
            print 'numpy.dot(pmat,Xh)',numpy.dot(pmat,Xh)
            x = recon.find2d(cam_id,X)
            print cam_id,'pmat ========='
            print pmat
            Rt = recon.get_extrinsic_parameter_matrix(cam_id)
            print 'Rt'
            print Rt
            print 'numpy.dot(Rt,Xh)'
            print numpy.dot(Rt,Xh)
            K,R = recon.get_SingleCameraCalibration(cam_id).get_KR()
            print 'K'
            print K
            print 'numpy.dot(K,numpy.dot(Rt,Xh))'
            print numpy.dot(K,numpy.dot(Rt,Xh))
            print 'numpy.dot(pmat,Xh)'
            print numpy.dot(pmat,Xh)
            print 'x',x
        print
        break
    print '-='*30
    for cam_id in recon.cam_ids:
        line = recon.get_projected_line_from_2d( cam_id, (800,600) )
        print cam_id,'line',line
        scc = recon.get_SingleCameraCalibration(cam_id)
        print 'center',scc.get_cam_center()
        oax = scc.get_optical_axis()
        print 'oax',oax
        print 'oax HZ',oax.to_hz()
        print 'closest',oax.closest()
        print

if __name__=='__main__':
    test()
