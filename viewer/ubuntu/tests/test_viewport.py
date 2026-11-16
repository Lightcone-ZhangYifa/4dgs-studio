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

    def test_clipped_segments_and_calibrated_integer_center(self):
        from gs_studio.viewport import Viewport, project_segments
        k = np.array([[50., 0., 30.], [0., 50., 20.], [0., 0., 1.]])
        points = np.array([[[0., 0., -1.], [0., 0., 2.]], [[0., 0., -2.], [0., 0., -1.]]])
        projected = project_segments(points, np.ones((2,2,4)), k, np.eye(4), 64, 48)
        self.assertEqual(projected.shape, (2,8))
        self.assertTrue(np.isfinite(projected).all())
        viewport = Viewport()
        viewport.set_camera(SimpleNamespace(K=k,c2w=np.eye(4),width=64,height=48,pixel_center='integer',distortion='none'))
        actual, _, width, height = viewport.get_camera()
        scale = min(width/64,height/48)
        self.assertAlmostEqual(actual[0,2], (30+.5)*scale+(width-64*scale)/2)
        self.assertAlmostEqual(actual[1,2], (20+.5)*scale+(height-48*scale)/2)
