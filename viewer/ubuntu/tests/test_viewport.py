"""Navigation invariants and real Mesa shader/picking integration checks."""
import math
import unittest
from types import SimpleNamespace
import numpy as np
from gs_studio.navigation import Navigation, operation, trail_start
from gs_studio.gl_renderer import projected_ellipses


def frame(points=None):
    points = [[0., 0., 0.], [0., 0., -.5]] if points is None else points
    return SimpleNamespace(means=np.array(points), covariance=np.tile(np.diag([.01, .04, .01]), (len(points), 1, 1)), colors=np.array([[1., 0., 0.], [0., 0., 1.]])[:len(points)], opacity=np.full(len(points), .8), ids=np.array([-(2**63)+1, 2**63-1], np.int64)[:len(points)], object_ids=np.array(['field'] * len(points)))


class NavigationTests(unittest.TestCase):
    def test_input_policy(self):
        self.assertEqual(operation(2), 'orbit')
        self.assertEqual(operation(2, shift=True), 'pan')
        self.assertEqual(operation(1, alt=True), 'orbit')
        self.assertEqual(operation(3, alt=True), 'dolly')
        self.assertEqual(operation(1, shift=True), 'pan')
        self.assertEqual(operation(1, control=True), 'dolly')

    def test_orbit_preserves_rigid_camera_and_pivot_distance(self):
        nav = Navigation()
        distance = np.linalg.norm(nav.c2w[:3, 3] - nav.pivot)
        for _ in range(100):
            nav.orbit(13, 7)
        np.testing.assert_allclose(nav.c2w[:3, :3].T @ nav.c2w[:3, :3], np.eye(3), atol=1e-12)
        self.assertAlmostEqual(np.linalg.det(nav.c2w[:3, :3]), 1)
        self.assertAlmostEqual(np.linalg.norm(nav.c2w[:3, 3] - nav.pivot), distance)
        self.assertTrue(np.isfinite(nav.c2w).all())

    def test_pan_moves_camera_and_pivot_equally_with_skew(self):
        nav = Navigation()
        old_camera, old_pivot = nav.c2w[:3, 3].copy(), nav.pivot.copy()
        k = nav.intrinsics(800, 600); k[0, 1] = 15
        nav.pan(20, 12, k)
        np.testing.assert_allclose(nav.c2w[:3, 3] - old_camera, nav.pivot - old_pivot)
        camera_shift = nav.c2w[:3, :3].T @ (nav.pivot - old_pivot)
        self.assertAlmostEqual(camera_shift[2], 0)

    def test_trackball_and_extreme_dolly_are_finite(self):
        nav = Navigation(mode='trackball')
        nav.orbit(20, 10)
        nav.dolly(1e12); nav.dolly(-1e12)
        self.assertTrue(np.isfinite(nav.c2w).all())
        self.assertGreater(np.linalg.norm(nav.c2w[:3, 3] - nav.pivot), 0)

    def test_dolly_preserves_skewed_cursor_anchor_and_scales_depth(self):
        nav = Navigation()
        nav.orbit(37, -15)
        k = nav.intrinsics(800,600); k[0,1] = 70; k[0,2] = 291; k[1,2] = 240
        pixel = [637.,117.]
        anchor = nav.point_on_pivot_plane(pixel,k)
        distance = np.linalg.norm(nav.c2w[:3,3]-nav.pivot)
        nav.dolly(-9,pixel=pixel,k=k)
        camera = nav.c2w[:3,:3].T @ (anchor-nav.c2w[:3,3])
        projected = k @ camera
        np.testing.assert_allclose(projected[:2]/projected[2],pixel,atol=1e-10)
        self.assertAlmostEqual(np.linalg.norm(nav.c2w[:3,3]-nav.pivot)/distance, math.exp(-9*.025))

    def test_trackball_matches_native_composed_camera_rotations(self):
        from gs_studio.navigation import rotation
        nav = Navigation(mode='trackball')
        pose = nav.c2w.copy()
        expected = rotation(pose[:3,0], .12) @ rotation(pose[:3,1], .06)
        nav.orbit(10,20)
        np.testing.assert_allclose(nav.c2w[:3,:3], expected @ pose[:3,:3],atol=1e-12)
        np.testing.assert_allclose(nav.c2w[:3,3],nav.pivot+expected@(pose[:3,3]-nav.pivot),atol=1e-12)

    def test_roll_preserves_optical_center_and_scene_pivot(self):
        nav = Navigation()
        nav.pivot = np.array([1.,.3,-.2])
        origin,pivot,forward = nav.c2w[:3,3].copy(),nav.pivot.copy(),nav.c2w[:3,2].copy()
        nav.roll(.3)
        np.testing.assert_allclose(nav.c2w[:3,3],origin,atol=1e-12)
        np.testing.assert_array_equal(nav.pivot,pivot)
        np.testing.assert_allclose(nav.c2w[:3,2],forward,atol=1e-12)

    def test_trail_uses_source_intervals(self):
        times = [0., 1., 2., 5., 11.]
        self.assertEqual(trail_start(.5, times, 2), 0)
        self.assertEqual(trail_start(8, times, 2), 1.5)
        self.assertEqual(trail_start(11, times, 2), 2)
        self.assertEqual(trail_start(8, times, 0), 8)
        self.assertTrue(math.isfinite(trail_start(0, [-1e308, -5e307, 5e307, 1e308], 1)))

    def test_projected_covariance_matches_analytic_and_offcenter_skew(self):
        f = frame([[0., 0., 2.]])
        f.covariance[:] = np.diag([.01, .04, .01])
        k = np.array([[100., 10., 63.], [0., 120., 45.], [0., 0., 1.]])
        centers, covariance, depth = projected_ellipses(f, k, np.eye(4), width=128, height=96)
        j = np.array([[50., 5., 0.], [0., 60., 0.]])
        expected = j @ f.covariance[0] @ j.T + np.eye(2) * .3
        np.testing.assert_allclose(centers, [[63., 45.]])
        np.testing.assert_allclose(covariance[0], expected)
        np.testing.assert_allclose(depth, [2.])


class GLViewportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk, Gdk, GLib
        cls.Gtk, cls.GLib = Gtk, GLib
        Gtk.init()
        if Gdk.Display.get_default() is None:
            raise unittest.SkipTest('Real GL viewport checks need a display; run under xvfb-run')

    def pump(self, count=30):
        import time
        for _ in range(count):
            while self.GLib.MainContext.default().pending():
                self.GLib.MainContext.default().iteration(False)
            time.sleep(.01)

    def test_real_shader_blending_picking_resolution_and_disposal(self):
        from gs_studio.viewport import Viewport
        from OpenGL import GL as gl
        win = self.Gtk.Window(default_width=128, default_height=128)
        viewport = Viewport()
        viewport.show_grid = viewport.show_cameras = False
        viewport.on_error = lambda text: self.fail(text)
        viewport.set_frame(frame())
        win.set_child(viewport); win.present(); self.pump()
        self.assertIsNone(viewport.render_error)
        viewport.make_current()
        renderer = viewport._renderer
        previous_fbo = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, renderer.fbo)
        pixels = np.frombuffer(gl.glReadPixels(0, 0, *renderer.size, gl.GL_RGB, gl.GL_UNSIGNED_BYTE), np.uint8).reshape(renderer.size[1], renderer.size[0], 3)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, previous_fbo)
        center = pixels[renderer.size[1]//2, renderer.size[0]//2] / 255
        self.assertGreater(center[0], .7)
        self.assertGreater(center[2], .1)
        self.assertLess(center[2], .25)
        selected = viewport.pick(viewport.get_width()/2, viewport.get_height()/2)
        self.assertEqual(selected, ('field', -(2**63)+1))
        self.assertEqual(viewport.selected, {selected})
        old_size = renderer.size
        viewport.resolution_scale = .5; viewport.queue_render(); self.pump()
        self.assertIsNone(viewport.render_error)
        self.assertEqual(renderer.size, (round(viewport.get_width()*.5), round(viewport.get_height()*.5)))
        self.assertEqual(gl.glGetError(), gl.GL_NO_ERROR)
        win.destroy(); self.pump(2)
        self.assertEqual(renderer.program, 0)
        self.assertEqual(renderer.buffer, 0)
        self.assertEqual(renderer.fbo, 0)

    def test_preview_controls_affect_shader_and_pick_consistently(self):
        from gs_studio.viewport import Viewport
        from OpenGL import GL as gl
        win = self.Gtk.Window(default_width=160, default_height=160)
        viewport = Viewport()
        viewport.show_grid = viewport.show_cameras = viewport.show_pivot = False
        viewport.set_frame(frame([[0., 0., 0.]]))
        win.set_child(viewport); win.present(); self.pump()
        def pixels():
            viewport.queue_render(); self.pump(5); viewport.make_current()
            self.assertIsNone(viewport.render_error)
            renderer = viewport._renderer
            previous = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, renderer.fbo)
            result = np.frombuffer(gl.glReadPixels(0, 0, *renderer.size, gl.GL_RGB, gl.GL_UNSIGNED_BYTE), np.uint8).reshape(renderer.size[1], renderer.size[0], 3).copy()
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, previous)
            return result
        baseline = pixels()
        y, x = baseline.shape[0]//2, baseline.shape[1]//2
        viewport.gaussian_scale = 2
        enlarged = pixels()
        self.assertGreater(int(enlarged[y, x+10, 0]), int(baseline[y, x+10, 0]) + 20)
        self.assertEqual(viewport.pick(x+10, viewport.get_height()/2), ('field', -(2**63)+1))
        viewport.set_selection({('field', None)})
        pixels()
        gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, viewport._renderer.mask_buffer)
        mask = np.frombuffer(gl.glGetBufferSubData(gl.GL_TEXTURE_BUFFER,0,1),np.uint8)
        self.assertEqual(mask.tolist(),[1])
        viewport.set_selection(set())
        viewport.opacity_multiplier = .25
        dimmed = pixels()
        self.assertLess(int(dimmed[y,x,0]), int(enlarged[y,x,0])*.5)
        viewport.opacity_multiplier = 0
        viewport.background = 'white'
        blank = pixels()
        self.assertTrue((blank == 255).all())
        self.assertIsNone(viewport.pick(viewport.get_width()/2,viewport.get_height()/2))
        viewport.opacity_multiplier = 1
        viewport.background = 'black'
        viewport.near_clip = 4
        clipped = pixels()
        self.assertFalse(clipped.any())
        self.assertIsNone(viewport.pick(viewport.get_width()/2,viewport.get_height()/2))
        viewport.near_clip = .001
        viewport.exposure_multiplier = .25
        quarter_exposure = pixels()
        viewport.exposure_multiplier = 1
        normal_exposure = pixels()
        self.assertAlmostEqual(float(quarter_exposure[y,x,0])/normal_exposure[y,x,0],.25,delta=.02)
        win.destroy(); self.pump(2)

    def test_weighted_translucent_paths_are_order_independent(self):
        from gs_studio.viewport import Viewport
        from OpenGL import GL as gl
        win = self.Gtk.Window(default_width=128, default_height=128)
        viewport = Viewport()
        viewport.show_grid = viewport.show_pivot = False
        win.set_child(viewport); win.present(); self.pump()
        viewport.make_current()
        renderer = viewport._renderer
        width, height = renderer.size
        x, y = width//2+.5, height//2+.5
        # Two differently colored paths cross at the same pixel and depth.
        red = [[10,y,.8,1,0,0,.6,.005], [width-10,y,.8,1,0,0,.6,.005]]
        blue = [[x,10,.8,0,0,1,.6,.005], [x,height-10,.8,0,0,1,.6,.005]]
        k, pose, _, _ = viewport.get_camera()
        results = []
        for order in [red+blue,blue+red]:
            renderer.target(width,height)
            renderer.begin(k,pose,width,height)
            renderer.paths(np.array(order,np.float32),width,height)
            results.append(viewport.snapshot_rgba())
        np.testing.assert_allclose(results[0],results[1],atol=1)
        crossing = results[0][height//2,width//2,:3]/255
        self.assertGreater(crossing[0],.35)
        self.assertGreater(crossing[2],.35)
        self.assertLess(abs(crossing[0]-crossing[2]),.02)
        self.assertGreater(renderer.oit_fbo,0)
        # Resize must dispose the transparency targets along with shared depth.
        renderer.target(width//2,height//2)
        self.assertEqual(renderer.oit_fbo,0)
        win.destroy();self.pump(2)

    def test_translation_and_selection_reuse_exact_depth_order(self):
        from gs_studio.viewport import Viewport
        from OpenGL import GL as gl
        win = self.Gtk.Window(default_width=128,default_height=128)
        viewport = Viewport(); viewport.set_frame(frame())
        win.set_child(viewport);win.present();self.pump()
        viewport.make_current(); renderer = viewport._renderer
        _,pose,_,_ = viewport.get_camera()
        initial = renderer.sort_count
        translated = pose.copy();translated[:3,3] += [10,-4,2]
        renderer.upload(viewport.frame,translated,{('field',None)})
        self.assertTrue(renderer.last_sort_reused)
        self.assertEqual(renderer.sort_count,initial)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER,renderer.order_buffer)
        actual = np.frombuffer(gl.glGetBufferSubData(gl.GL_ARRAY_BUFFER,0,8),np.uint32)
        expected = np.argsort(-(viewport.frame.means@pose[:3,2]),kind='stable')
        np.testing.assert_array_equal(actual,expected)
        viewport.navigation.orbit(5,0)
        renderer.upload(viewport.frame,viewport.navigation.c2w,set())
        self.assertFalse(renderer.last_sort_reused)
        self.assertEqual(renderer.sort_count,initial+1)
        win.destroy();self.pump(2)

    def test_escape_stops_drag_without_picking_or_reverting_pose(self):
        from gs_studio.viewport import Viewport
        from gi.repository import Gdk
        viewport = Viewport();viewport._drag_operation = 'orbit';viewport._extend = False
        viewport._drag_origin = np.array([10.,20.]);viewport._drag_cancelled = False
        viewport._begin_navigation();viewport._drag_update(None,20,12)
        pose = viewport.navigation.c2w.copy()
        picked=[];viewport.on_pick = lambda *args:picked.append(args)
        self.assertTrue(viewport._key(None,Gdk.KEY_Escape,0,0))
        viewport._drag_update(None,100,100);viewport._drag_end(None,100,100)
        np.testing.assert_array_equal(viewport.navigation.c2w,pose)
        self.assertEqual(picked,[])
        self.assertFalse(viewport._navigation_active)

    def test_static_parent_redraw_reuses_texture_and_resize_renders(self):
        from gs_studio.viewport import Viewport
        win = self.Gtk.Window(default_width=240, default_height=240)
        root = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL)
        caption = self.Gtk.Label(label='Frame latency')
        viewport = Viewport();viewport.set_frame(frame())
        submitted=[];viewport.on_stats = lambda stats: submitted.append(stats)
        root.append(caption);root.append(viewport);win.set_child(root);win.present();self.pump()
        initial = len(submitted)
        self.assertGreater(initial,0)
        caption.set_text('Frame time');caption.queue_draw();root.queue_draw();self.pump(10)
        self.assertEqual(len(submitted),initial)
        viewport.queue_render();viewport.queue_render();self.pump(10)
        self.assertEqual(len(submitted),initial+1)
        win.set_default_size(320,280);self.pump(10)
        self.assertGreater(len(submitted),initial+1)
        self.assertEqual(viewport._renderer.size,(viewport.get_width()*viewport.get_scale_factor(),viewport.get_height()*viewport.get_scale_factor()))
        win.destroy();self.pump(2)

    def test_prepared_shader_interpolation_residency_and_exact_identity_pick(self):
        import tempfile
        from pathlib import Path
        from gsv import Writer, add_persistent_gaussians
        from gs_studio.model import SceneDocument
        from gs_studio.viewport import Viewport
        from OpenGL import GL as gl
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'resident.gsv'
            ids = np.array([-2**60-3,7,2**60+5],np.int64)
            a = np.zeros((3,16),np.float32)
            a[:,:3] = [[-.25,0,2],[.2,.1,2.5],[.4,-.1,2]]
            a[:,3] = .8;a[:,4:7] = [.08,.03,.05];a[:,7:9] = 1
            a[:,12:15] = [[1,.1,.2],[.1,.3,1],[.3,1,.2]];a[:,15] = -1
            b = a.copy();b[:,0] += .1;b[:,4:7] *= [1.2,.8,1]
            b[:,8:12] = [math.cos(.6),0,0,math.sin(.6)]
            b[1,8:12] *= -1
            b[2,7] = 0
            with Writer(path) as out:
                add_persistent_gaussians(out,'field',ids,[0.,1.],[a,b])
            with SceneDocument(path) as doc:
                prepared = doc.prepare_frame(.35)
                win = self.Gtk.Window(default_width=240,default_height=180)
                viewport = Viewport();viewport.show_grid = viewport.show_pivot = False
                viewport.set_frame(prepared);win.set_child(viewport);win.present();self.pump()
                self.assertIsNone(viewport.render_error)
                viewport.make_current();renderer = viewport._renderer
                key,capacity = renderer._geometry_key,renderer.capacity
                actual = viewport.snapshot_rgba()
                # Same dimensions, pose and overlays; only evaluation backend changes.
                viewport.set_frame(prepared.materialize());self.pump(10)
                reference = viewport.snapshot_rgba()
                np.testing.assert_allclose(actual.astype(int),reference.astype(int),atol=1)
                viewport.set_frame(prepared);self.pump(5)
                element = prepared.element('field',int(ids[0]))
                k,pose,width,height = viewport.get_camera()
                camera = pose[:3,:3].T@(element.position-pose[:3,3]);pixel = k@camera;pixel=pixel[:2]/pixel[2]
                result = viewport.pick(*pixel)
                self.assertEqual(result,('field',int(ids[0])))
                np.testing.assert_allclose(viewport.navigation.pivot,element.position)
                # Seeking within this source interval must retain both GPU endpoints.
                second = doc.prepare_frame(.7)
                before_a = renderer.buffer;before_b = renderer.second_buffer
                viewport.set_frame(second);self.pump(5)
                self.assertEqual(renderer._geometry_key,key)
                self.assertEqual(renderer.capacity,capacity)
                self.assertEqual((renderer.buffer,renderer.second_buffer),(before_a,before_b))
                # An interior Float64 time may round to Float32 zero: gap rows stay hidden.
                tiny = doc.prepare_frame(np.nextafter(0.,1.))
                viewport.set_frame(tiny);self.pump(5)
                missing = tiny.element('field',int(ids[2]))
                camera = pose[:3,:3].T@(missing.position-pose[:3,3]);pixel=k@camera;pixel=pixel[:2]/pixel[2]
                self.assertIsNone(viewport.pick(*pixel))
                self.assertEqual(gl.glGetError(),gl.GL_NO_ERROR)
                win.destroy();self.pump(2)

    def test_prepared_multiple_cohort_weights_match_cpu_render(self):
        import tempfile
        from pathlib import Path
        from gsv import Writer, add_persistent_gaussians
        from gs_studio.model import SceneDocument
        from gs_studio.viewport import Viewport
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'cohorts.gsv'
            a=np.zeros((1,16),np.float32);a[0,:3]=[-.2,0,2]
            a[:,3]=.8;a[:,4:7]=[.08,.03,.02];a[:,7:9]=1;a[:,12:15]=[1,.1,.2];a[:,15]=-1
            b=a.copy();b[:,0]=0;b[:,12:15]=[.1,.8,.3]
            c=a.copy();c[:,0]=.3;c[:,12:15]=[.1,.2,1]
            d=c.copy();d[:,1]=.1;d[:,4:7]=[.03,.08,.02]
            with Writer(path) as out:
                add_persistent_gaussians(out,'a',np.array([7],np.int64),[0.,1.],[a,b])
                add_persistent_gaussians(out,'b',np.array([7],np.int64),[0.,2.],[c,d])
            with SceneDocument(path) as doc:
                prepared=doc.prepare_frame(.5)
                self.assertIsNotNone(prepared.weights)
                win=self.Gtk.Window(default_width=200,default_height=160)
                viewport=Viewport();viewport.show_grid=viewport.show_pivot=False
                viewport.set_frame(prepared);win.set_child(viewport);win.present();self.pump()
                self.assertIsNone(viewport.render_error)
                actual=viewport.snapshot_rgba()
                viewport.set_frame(prepared.materialize());self.pump(10)
                reference=viewport.snapshot_rgba()
                np.testing.assert_allclose(actual.astype(int),reference.astype(int),atol=1)
                viewport.set_frame(prepared);self.pump(5)
                for owner in ('a','b'):
                    element=prepared.element(owner,7)
                    k,pose,_,_=viewport.get_camera()
                    camera=pose[:3,:3].T@(element.position-pose[:3,3]);pixel=k@camera;pixel=pixel[:2]/pixel[2]
                    self.assertEqual(viewport.pick(*pixel),(owner,7))
                win.destroy();self.pump(2)

    def test_gpu_pick_preserves_adjacent_float_depth_and_source_ties(self):
        from gs_studio.viewport import Viewport
        from OpenGL import GL as gl
        win=self.Gtk.Window(default_width=128,default_height=128)
        viewport=Viewport();win.set_child(viewport);win.present();self.pump()
        viewport.make_current();renderer=viewport._renderer
        f=frame([[0,0,np.nextafter(np.float32(2),np.float32(3))],[0,0,2]])
        pose=np.eye(4);k=np.array([[60.,0,64],[0,60.,64],[0,0,1.]])
        renderer.upload(f,pose,set())
        self.assertEqual(renderer.pick(k,pose,128,128,[64,64]),1)
        tied=frame([[0,0,2],[0,0,2]])
        renderer.upload(tied,pose,set())
        self.assertEqual(renderer.pick(k,pose,128,128,[64,64]),0)
        self.assertEqual(gl.glGetError(),gl.GL_NO_ERROR)
        win.destroy();self.pump(2)

    def test_compute_failure_rebuilds_current_source_order_on_cpu(self):
        from unittest import mock
        from gs_studio.viewport import Viewport
        from gs_studio.gpu_sort import depth_order_cpu
        from OpenGL import GL as gl
        win=self.Gtk.Window(default_width=128,default_height=128)
        viewport=Viewport();win.set_child(viewport);win.present();self.pump()
        viewport.make_current();renderer=viewport._renderer
        renderer.sort_mode='gpu';renderer.gpu_sorter.supported=True
        f=frame([[0,0,2],[0,0,3]])
        with mock.patch.object(renderer.gpu_sorter,'sort',side_effect=RuntimeError('Synthetic compute allocation failure')):
            renderer.upload(f,np.eye(4),set())
        self.assertEqual(renderer.sort_backend,'cpu')
        self.assertIn('Synthetic compute allocation failure',renderer.sort_reason)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER,renderer.order_buffer)
        order=np.frombuffer(gl.glGetBufferSubData(gl.GL_ARRAY_BUFFER,0,8),np.uint32)
        np.testing.assert_array_equal(order,depth_order_cpu(f.means,np.eye(4)))
        self.assertEqual(gl.glGetError(),gl.GL_NO_ERROR)
        win.destroy();self.pump(2)

    def test_clipped_segments_and_calibrated_integer_center(self):
        from gs_studio.viewport import Viewport, project_segments
        k = np.array([[50., 0., 30.], [0., 50., 20.], [0., 0., 1.]])
        points = np.array([[[0., 0., -1.], [0., 0., 2.]], [[0., 0., -2.], [0., 0., -1.]]])
        projected = project_segments(points, np.ones((2,2,4)), k, np.eye(4), 64, 48)
        self.assertEqual(projected.shape, (2,8))
        self.assertTrue(np.isfinite(projected).all())
        viewport = Viewport()
        viewport.navigation.pivot=np.array([1.,.5,3.])
        viewport.set_camera(SimpleNamespace(K=k,c2w=np.eye(4),width=64,height=48,pixel_center='integer',distortion='none'))
        np.testing.assert_array_equal(viewport.navigation.pivot,[1.,.5,3.])
        actual, _, width, height = viewport.get_camera()
        scale = min(width/64,height/48)
        self.assertAlmostEqual(actual[0,2], (30+.5)*scale+(width-64*scale)/2)
        self.assertAlmostEqual(actual[1,2], (20+.5)*scale+(height-48*scale)/2)
