import unittest
import reconstruct
import pkg_resources
import flydra.reconstruct_utils as reconstruct_utils
import flydra.geom
import flydra.fastgeom
import flydra.undistort
import numpy
import scipy.optimize

try:
    import numpy.testing.parametric as parametric
except ImportError,err:
    # old numpy (without module), use local copy
    import numpy_testing_parametric as parametric

##class TestGeom(parametric.ParametricTestCase):
##    _indepParTestPrefix = 'test_geom'
##    def test_geom(self):
##        for mod in [flydra.geom,
##                    flydra.fastgeom]:
##                yield (test,mod)

class TestGeomParametric(parametric.ParametricTestCase):
    #: Prefix for tests with independent state.  These methods will be run with
    #: a separate setUp/tearDown call for each test in the group.
    _indepParTestPrefix = 'test_geom'

    def test_geom(self):
        for mod in [flydra.geom,
                    flydra.fastgeom]:
            for x1 in [1,100,10000]:
                for y1 in [5,50,500]:
                    for z1 in [-10,234,0]:
                        for x2 in [3,50]:
                            yield (self.tstXX,mod,x1,y1,z1,x2)
            for test in [self.tst_tuple_neg,
                         self.tst_tuple_multiply1,
                         self.tst_tuple_multiply2,
                         self.tst_line_closest1,
                         self.tst_line_translate,
                         self.tst_line_from_points,
                         self.tst_init]:
                yield (test, mod)


    def tstXX(self,geom,x1,y1,z1,x2):
        pt_a = [x1,y1,z1,1]
        pt_b = [x2,5,6,1]
        hz_p = reconstruct.pluecker_from_verts(pt_a,pt_b)

        a=geom.ThreeTuple(pt_a[:3])
        b=geom.ThreeTuple(pt_b[:3])
        L = geom.line_from_points(a,b)

        hzL = geom.line_from_HZline(hz_p)

        strL = str(L)
        strhzL = str(hzL)
        assert strL==strhzL
        if 0:
            print 'hz_p',hz_p
            print 'correct',L
            print 'test   ',hzL
            print

    def tst_line_from_points(self,geom):
        line=geom.line_from_points(geom.ThreeTuple((2,1,0)),
                                   geom.ThreeTuple((2,0,0)))
        line.closest()
        line.dist2()

    def tst_line_closest1(self,geom):
        if geom is flydra.fastgeom:
            return # not implemented
        xaxis=geom.line_from_points(geom.ThreeTuple((0,0,0)),
                                    geom.ThreeTuple((1,0,0)))
        zline=geom.line_from_points(geom.ThreeTuple((.5,0,0)),
                                    geom.ThreeTuple((.5,0,1)))
        result = xaxis.get_my_point_closest_to_line( zline )
        eps = 1e-10
        assert result.dist_from( geom.ThreeTuple( (0.5, 0, 0) )) < eps

    def tst_init(self,geom):
        a = geom.ThreeTuple((1,2,3))
        b = geom.ThreeTuple(a)
        assert a==b

    def tst_tuple_neg(self,geom):
        a = geom.ThreeTuple((1,2,3))
        b = -a
        c = geom.ThreeTuple((-1,-2,-3))
        assert b == c

    def tst_tuple_multiply1(self,geom):
        x = 2.0
        a = geom.ThreeTuple((1,2,3))
        b = x*a
        c = a*x
        assert b == c

    def tst_tuple_multiply2(self,geom):
        x = -1.0
        a = geom.ThreeTuple((1,2,3))
        b = x*a
        c = -a
        assert b == c

    def tst_line_translate(self,geom):
        a = geom.ThreeTuple((0,0,1))
        b = geom.ThreeTuple((0,1,0))
        c = geom.ThreeTuple((1,0,0))
        ln = geom.PlueckerLine(a,b)
        lnx = ln.translate(c)
        assert lnx == geom.PlueckerLine(geom.ThreeTuple((0,0,-1)),
                                        geom.ThreeTuple((0,-2,0)))

class TestReconstructor(unittest.TestCase):
    def test_from_sample_directory(self):
        caldir = pkg_resources.resource_filename(__name__,"sample_calibration")
        reconstruct.Reconstructor(caldir)
    def test_pickle(self):
        caldir = pkg_resources.resource_filename(__name__,"sample_calibration")
        x=reconstruct.Reconstructor(caldir)
        import pickle
        pickle.dumps(x)

class TestNonlinearDistortion(parametric.ParametricTestCase):
    _indepParTestPrefix = 'test_coord_undistort'

    def test_coord_undistort(self):
        xys = [ ( 10, 20 ),
                ( 600, 20 ),
                ( 320, 240 ),
                ( 10, 490 ),
                ]

        all_helper_args = []
        # sample data from real lens - mild radial distortion
        fc1, fc2, cc1, cc2, k1, k2, p1, p2, alpha_c = (1149.1142578125, 1144.7752685546875, 327.5, 245.0, -0.47600057721138, 0.34306392073631287, 0.0, 0.0, 0.0)
        helper_args = (fc1, fc2, cc1, cc2, k1, k2, p1, p2)
        all_helper_args.append( helper_args )

        # same as above, with a little tangential distortion
        fc1, fc2, cc1, cc2, k1, k2, p1, p2, alpha_c = (1149.1142578125, 1144.7752685546875, 327.5, 245.0, -0.47600057721138, 0.34306392073631287, 0.1, 0.05, 0.0)
        helper_args = (fc1, fc2, cc1, cc2, k1, k2, p1, p2)
        all_helper_args.append( helper_args )

        # sample data from real lens - major radial distortion
        fc1, fc2, cc1, cc2, k1, k2, p1, p2, alpha_c = (1000, 1000, 317.64022687190129, 253.60089300842131, -1.5773930368340232, 1.9308294843687406, 0.0, 0.0, 0.0)
        helper_args = (fc1, fc2, cc1, cc2, k1, k2, p1, p2)
        all_helper_args.append( helper_args )

        for helper_args in all_helper_args:
            yield (self.tst_distort,        xys, helper_args)
##            yield (self.tst_undistort_mesh, xys, helper_args)
            yield (self.tst_undistort_orig, xys, helper_args)
            yield (self.tst_roundtrip,      xys, helper_args)

    def _distort( self, helper_args, xy ):
        fc1, fc2, cc1, cc2, k1, k2, p1, p2 = helper_args

        # this is the distortion model we're using:
        xl, yl = xy
        x = (xl-cc1)/fc1
        y = (yl-cc2)/fc2
        r2 = x**2 + y**2
        r4 = r2**2
        term1 = k1*r2 + k2*r4

        # Tangential distortion: see
        # http://www.vision.caltech.edu/bouguetj/calib_doc/htmls/parameters.html

        xd = x + x*term1 + ( 2*p1*x*y       + p2*(r2+2*x**2) )
        yd = y + y*term1 + ( p1*(r2+2*y**2) + 2*p2*x*y       )

        xd = fc1*xd + cc1
        yd = fc2*yd + cc2
        return xd, yd

    ## def _undistort( self, helper_args, xy ):
    ##     helper = reconstruct_utils.ReconstructHelper( *helper_args )
    ##     lbrt = ( -100,-100,800,700 )
    ##     dm = flydra.undistort.DistortionMesh( helper, lbrt )
    ##     x,y= xy
    ##     undistorted_xs, undistorted_ys = dm.undistort_points( [x],[y] )
    ##     assert len(undistorted_xs)==1
    ##     assert len(undistorted_ys)==1
    ##     return undistorted_xs[0], undistorted_ys[0]

    def tst_distort(self, pinhole_xys, helper_args ):
        helper = reconstruct_utils.ReconstructHelper( *helper_args )
        for xy in pinhole_xys:
            xd, yd = self._distort( helper_args, xy )
            test_x, test_y = helper.distort( *xy )

            rtol=1e-4 # float32 not very accurate
            assert numpy.allclose([xd,yd], [test_x, test_y], rtol=rtol)

    def tst_roundtrip(self, xys_orig_distorted, helper_args ):
        helper = reconstruct_utils.ReconstructHelper( *helper_args )
        for xy_orig_distorted in xys_orig_distorted:
            xy_undistorted = helper.undistort( xy_orig_distorted[0], xy_orig_distorted[1] )
            xy_distorted = helper.distort( xy_undistorted[0], xy_undistorted[1] )
            assert numpy.allclose( xy_distorted, xy_orig_distorted )

    ## def tst_undistort_mesh(self, distorted_xys, helper_args ):
    ##     helper = reconstruct_utils.ReconstructHelper( *helper_args )
    ##     for xy in distorted_xys:
    ##         undistorted_xy = self._undistort( helper_args, xy)
    ##         redistorted_xy = helper.distort( *undistorted_xy )

    ##         rtol = 1e-3 # distortion mesh not very accurate
    ##         assert numpy.allclose( xy, redistorted_xy, rtol=rtol)

    def tst_undistort_orig(self, distorted_xys, helper_args ):
        helper = reconstruct_utils.ReconstructHelper( *helper_args )
        for xy in distorted_xys:
            undistorted_xy = helper.undistort(*xy)
            redistorted_xy = helper.distort( *undistorted_xy )

            assert numpy.allclose( xy, redistorted_xy)

def get_test_suite():
    ts=unittest.TestSuite([unittest.makeSuite(TestGeomParametric),
                           unittest.makeSuite(TestReconstructor),
                           unittest.makeSuite(TestNonlinearDistortion),
                           ])
    return ts

if __name__=='__main__':
    if 0:
        ts = get_test_suite()
        ts.debug()
    else:
        unittest.main()
