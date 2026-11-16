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

    def test_presets_are_proper_world_up_rotations_and_preserve_orbit(self):
        from gs_studio.navigation import world_basis
        for up in [np.array(x,dtype=float)*sign for x in np.eye(3) for sign in (-1,1)]:
            for name in ('front','back','left','right','top','bottom','isometric'):
                nav=Navigation();nav.up=up;nav.pivot=np.array([2.,1.,3.])
                distance=np.linalg.norm(nav.c2w[:3,3]-nav.pivot)
                nav.preset(name)
                np.testing.assert_allclose(nav.c2w[:3,:3].T@nav.c2w[:3,:3],np.eye(3),atol=1e-12)
                self.assertAlmostEqual(np.linalg.det(nav.c2w[:3,:3]),1.,places=12)
                self.assertAlmostEqual(np.linalg.norm(nav.c2w[:3,3]-nav.pivot),distance,places=10)
                np.testing.assert_array_equal(nav.pivot,[2,1,3])
                if name=='front':np.testing.assert_allclose(nav.c2w[:3,2],-world_basis(up)[:,2],atol=1e-12)

    def test_nonfinite_pan_is_ignored(self):
        nav=Navigation();pose=nav.c2w.copy();pivot=nav.pivot.copy()
        nav.pan(float('nan'),0,nav.intrinsics(800,600))
        np.testing.assert_array_equal(nav.c2w,pose);np.testing.assert_array_equal(nav.pivot,pivot)

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
        viewport.navigation.c2w=np.diag([1.,-1.,-1.,1.]);viewport.navigation.c2w[2,3]=3
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
        self.assertEqual(mask.tolist(),[0],"Whole-object selection does not mark individual Gaussian rows in macOS")
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
        initial = viewport._renderer.submitted_frame_count
        self.assertGreater(initial,0)
        caption.set_text('Frame time');caption.queue_draw();root.queue_draw();self.pump(10)
        self.assertEqual(viewport._renderer.submitted_frame_count,initial)
        viewport.queue_render();viewport.queue_render();self.pump(10)
        self.assertEqual(viewport._renderer.submitted_frame_count,initial+1)
        win.set_default_size(320,280);self.pump(10)
        self.assertGreater(viewport._renderer.submitted_frame_count,initial+1)
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

    def test_source_interval_rotation_uploads_only_new_endpoint_both_directions(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from gsv import Writer, add_persistent_gaussians
        from gs_studio.model import SceneDocument
        from gs_studio.viewport import Viewport
        from gs_studio.gl_renderer import Renderer
        from OpenGL import GL as gl
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'stream.gsv'
            frames=[]
            for step in range(4):
                packed=np.zeros((2,16),np.float32)
                packed[:,:3]=[[-.3+step*.04,0,2],[.3,step*.02,2.5]]
                packed[:,3]=.8;packed[:,4:7]=[.08,.04,.03];packed[:,7]=1
                packed[:,8]=math.cos(step*.2);packed[:,11]=math.sin(step*.2)
                packed[:,12:15]=[[1,.1,.3],[.2,.3,1]];packed[:,15]=-1
                frames.append(packed)
            with Writer(path) as out:
                add_persistent_gaussians(out,'field',np.array([-7,2**60+3],np.int64),[0.,1.,2.,3.],frames)
            with SceneDocument(path) as doc:
                states=[doc.prepare_frame(t) for t in (.25,1.25,2.25)]
                self.assertIs(states[0].second,states[1].first)
                self.assertIs(states[1].second,states[2].first)
                win=self.Gtk.Window(default_width=128,default_height=128)
                viewport=Viewport();win.set_child(viewport);win.present();self.pump()
                viewport.make_current();renderer=Renderer();renderer.initialize()
                pose=np.eye(4);k=np.array([[80.,0,64],[0,80.,64],[0,0,1.]])
                def image(active):
                    renderer.upload(active,pose,set());renderer.target(128,128)
                    renderer.begin(k,pose,128,128);renderer.splats()
                    return bytes(gl.glReadPixels(0,0,128,128,gl.GL_RGBA,gl.GL_UNSIGNED_BYTE))
                try:
                    with mock.patch.object(renderer,'_upload_texture_buffer',wraps=renderer._upload_texture_buffer) as uploads:
                        image(states[0]);self.assertEqual(uploads.call_count,2)
                        old_pair=(renderer.buffer,renderer.second_buffer)
                        for active in [states[1],states[2],states[1],states[0]]:
                            before=uploads.call_count
                            actual=image(active)
                            self.assertEqual(uploads.call_count-before,1)
                            self.assertTrue(renderer.last_endpoint_swapped)
                            self.assertEqual(renderer.last_endpoint_upload_bytes,active.first.nbytes)
                            self.assertEqual((renderer.buffer,renderer.second_buffer),old_pair[::-1])
                            old_pair=(renderer.buffer,renderer.second_buffer)
                            # Independent non-reusing renderer proves pixels after rotation.
                            reference=Renderer(reuse_endpoints=False);reference.initialize()
                            try:
                                reference.upload(active.materialize(),pose,set());reference.target(128,128)
                                reference.begin(k,pose,128,128);reference.splats()
                                expected=bytes(gl.glReadPixels(0,0,128,128,gl.GL_RGBA,gl.GL_UNSIGNED_BYTE))
                                np.testing.assert_allclose(np.frombuffer(actual,np.uint8).astype(int),np.frombuffer(expected,np.uint8).astype(int),atol=1)
                            finally:
                                reference.close()
                        before=uploads.call_count
                        image(doc.prepare_frame(.6))
                        self.assertEqual(uploads.call_count,before)
                        self.assertEqual(renderer.last_endpoint_uploads,0)
                finally:
                    renderer.close();win.destroy();self.pump(2)

    def test_camera_pick_resolves_depth_occlusion_empty_scene_and_toggle(self):
        from unittest import mock
        from gs_studio.viewport import Viewport
        win=self.Gtk.Window(default_width=128,default_height=128)
        viewport=Viewport();viewport.navigation.c2w=np.eye(4)
        viewport.show_grid=viewport.show_pivot=False
        win.set_child(viewport);win.present();self.pump()
        k,_,width,height=viewport.get_camera();x,y=width/2+.5,height/2+.5
        def line(z,px=None):
            px=x if px is None else px
            pixels=np.array([[px,y-12,1],[px,y+12,1]])
            return (pixels@np.linalg.inv(k).T*z).reshape(1,2,3)
        front={'object_id':'front','view':0,'segments':line(2)}
        back={'object_id':'back','view':0,'segments':line(4)}
        callbacks=[];gaussians=[]
        viewport.on_camera_pick=lambda *args:callbacks.append(args)
        viewport.on_pick=lambda *args:gaussians.append(args)
        f=frame([[0,0,3]])
        with mock.patch.object(viewport,'_camera_segments',side_effect=lambda entry:entry['segments']):
            viewport.set_frame(f);viewport.set_camera_wireframes([front]);self.pump(5)
            viewport.pick(x,y,extend=True)
            self.assertEqual(callbacks.pop(),('front',0,True))
            self.assertEqual(gaussians,[])
            viewport.set_camera_wireframes([back]);self.pump(5)
            self.assertEqual(viewport.pick(x,y),('field',-(2**63)+1))
            self.assertEqual(callbacks,[])
            viewport.set_frame(None);viewport.set_camera_wireframes([back,front]);self.pump(5)
            viewport.pick(x,y)
            self.assertEqual(callbacks.pop(),('front',0,False))
            # Cursor misses a narrow Gaussian; the closest camera edge is still
            # hidden by its alpha>=.1 footprint several pixels away from cursor.
            mean=np.linalg.inv(k)@np.array([x+3,y,1.])*3
            narrow=frame([mean]);narrow.covariance[:]=np.eye(3)*1e-6
            viewport.set_frame(narrow);viewport.set_camera_wireframes([dict(back,segments=line(4,x+3))]);self.pump(5)
            viewport.pick(x,y)
            self.assertEqual(callbacks,[])
            self.assertEqual(gaussians[-1],(None,None,False))
        win.destroy();self.pump(2)

    def test_visible_camera_edge_clips_screen_span_and_interpolates_inverse_depth(self):
        from gs_studio.viewport import camera_edge_hits
        projected=np.array([[-10.,-1.,0,1,1,1,1,2],[10.,3.,0,1,1,1,1,6]])
        hits=camera_edge_hits(projected,[0,0],100,100,4)
        self.assertEqual(hits.shape,(1,3))
        np.testing.assert_allclose(hits[0],[0,1,3],atol=1e-6)

    def test_minimum_selection_ring_marks_only_individual_elements(self):
        from gs_studio.viewport import Viewport
        win=self.Gtk.Window(default_width=128,default_height=128)
        viewport=Viewport();viewport.navigation.c2w=np.eye(4)
        viewport.show_grid=viewport.show_pivot=viewport.show_cameras=False
        tiny=frame([[0,0,3]]);tiny.covariance[:]=np.eye(3)*1e-8;tiny.opacity[:]=.01
        viewport.set_frame(tiny);viewport.set_selection({('field',-(2**63)+1)})
        win.set_child(viewport);win.present();self.pump()
        self.assertIsNone(viewport.render_error)
        rgba=viewport.snapshot_rgba();k,_,_,_=viewport.get_camera()
        amber=(rgba[:,:,0]>200)&(rgba[:,:,1]>120)&(rgba[:,:,2]<80)
        ys,xs=np.nonzero(amber)
        self.assertGreater(len(xs),0)
        radii=np.hypot(xs+.5-k[0,2],ys+.5-k[1,2])
        self.assertGreater(radii.min(),4.8);self.assertLess(radii.max(),6.1)
        viewport.set_selection({('field',None)});self.pump(5)
        rgba=viewport.snapshot_rgba()
        self.assertFalse(((rgba[:,:,0]>200)&(rgba[:,:,1]>120)&(rgba[:,:,2]<80)).any())
        win.destroy();self.pump(2)

    def test_center_selection_preset_and_free_view_preserve_required_state(self):
        from gs_studio.viewport import Viewport
        viewport=Viewport();viewport.set_frame(frame([[2,1,3]]))
        viewport.selected={('field',-(2**63)+1)}
        k=np.array([[55.,7.,30.],[0.,60.,24.],[0,0,1.]])
        viewport.set_camera(SimpleNamespace(K=k,c2w=np.eye(4),width=64,height=48,pixel_center='half',distortion='none'))
        pose=viewport.navigation.c2w.copy();effective=viewport.get_camera()[0]
        viewport.center_on_selection()
        np.testing.assert_array_equal(viewport.navigation.c2w,pose)
        np.testing.assert_allclose(viewport.navigation.pivot,[2,1,3])
        viewport.free_camera();np.testing.assert_array_equal(viewport.get_camera()[0],effective)
        viewport.frame_selection();np.testing.assert_array_equal(viewport.get_camera()[0],effective)
        np.testing.assert_array_equal(viewport.navigation.c2w[:3,:3],pose[:3,:3])
        pivot=viewport.navigation.pivot.copy();distance=np.linalg.norm(viewport.navigation.c2w[:3,3]-pivot)
        viewport.preset('right')
        self.assertAlmostEqual(np.linalg.det(viewport.navigation.c2w[:3,:3]),1)
        np.testing.assert_array_equal(viewport.navigation.pivot,pivot)
        self.assertAlmostEqual(np.linalg.norm(viewport.navigation.c2w[:3,3]-pivot),distance)
        viewport.frame_scene()
        self.assertIsNone(viewport._calibration)
        self.assertAlmostEqual(viewport.navigation.fov,360/7)

    def test_scroll_latches_trackpad_operation_and_obeys_discrete_wheel_policy(self):
        from unittest import mock
        from gs_studio.viewport import Viewport
        from gi.repository import Gdk
        class Controller:
            state=Gdk.ModifierType.ALT_MASK;unit=Gdk.ScrollUnit.SURFACE
            def get_current_event_state(self):return self.state
            def get_unit(self):return self.unit
            def get_current_event(self):return None
        viewport=Viewport();controller=Controller()
        with mock.patch.object(viewport.navigation,'orbit') as orbit,mock.patch.object(viewport.navigation,'pan') as pan,mock.patch.object(viewport.navigation,'dolly') as dolly:
            viewport._scroll(controller,1,2);controller.state=Gdk.ModifierType(0)
            viewport._scroll(controller,2,3)
            self.assertEqual(orbit.call_count,2);pan.assert_not_called()
            viewport.cancel_navigation();controller.unit=Gdk.ScrollUnit.WHEEL;controller.state=Gdk.ModifierType.ALT_MASK
            viewport._scroll(controller,0,1);self.assertEqual(dolly.call_count,1)
            viewport.cancel_navigation()
        viewport.tool='pan';self.assertEqual(viewport.get_cursor().get_name(),'grab')

    def test_history_restores_up_axis_and_ignores_inactive_focus_loss(self):
        from gs_studio.viewport import Viewport
        viewport=Viewport();viewport.clear_navigation_history()
        initial=len(viewport._history)
        viewport.cancel_navigation();self.assertEqual(len(viewport._history),initial)
        viewport._begin_navigation();viewport.set_up_axis('z');viewport.navigation.orbit(10,0);viewport._end_navigation()
        viewport.previous_view();np.testing.assert_array_equal(viewport.navigation.up,[0,1,0])
        viewport.next_view();np.testing.assert_array_equal(viewport.navigation.up,[0,0,1])

    def test_grid_frustum_size_and_zero_interval_trails_match_native(self):
        from gs_studio.viewport import Viewport
        viewport=Viewport();viewport.set_frame(frame([[0,0,3]]))
        grid,_=viewport._grid_segments();viewport.navigation.dolly(30);same,_=viewport._grid_segments()
        np.testing.assert_array_equal(grid,same)
        self.assertEqual(np.max(np.abs(grid)),10)
        calibration=SimpleNamespace(K=np.eye(3),c2w=np.eye(4),width=1,height=1,pixel_center='half')
        first=viewport._camera_segments(calibration);viewport.navigation.dolly(30)
        np.testing.assert_array_equal(first,viewport._camera_segments(calibration))
        viewport.paths=[SimpleNamespace(positions=np.array([[0,0,0],[1,0,0]]),times=np.array([0.,1.]),identity=7,object_id='field',colors=np.ones((2,3)))]
        viewport.source_times=np.array([0.,1.]);viewport.time=0;viewport.show_paths=False
        points,_=viewport._path_segments();self.assertEqual(len(points),0)

    def test_framebuffer_and_transparency_allocation_failures_are_transactional(self):
        from unittest import mock
        from gs_studio.viewport import Viewport
        from gs_studio.gl_renderer import Renderer,RenderAllocationError
        from OpenGL import GL as gl
        win=self.Gtk.Window(default_width=128,default_height=128);viewport=Viewport()
        win.set_child(viewport);win.present();self.pump();viewport.make_current()
        renderer=Renderer();renderer.initialize()
        try:
            k=np.array([[40.,0,32],[0,40.,24],[0,0,1.]])
            renderer.target(64,48);renderer.begin(k,np.eye(4),64,48,background=(.2,.3,.4))
            old=(renderer.fbo,renderer.texture,renderer.depth,renderer.size)
            reference=bytes(gl.glReadPixels(0,0,64,48,gl.GL_RGBA,gl.GL_UNSIGNED_BYTE))
            with mock.patch.object(gl,'glTexImage2D',side_effect=RuntimeError('Injected target OOM')):
                with self.assertRaises(RenderAllocationError):renderer.target(128,96)
            self.assertEqual((renderer.fbo,renderer.texture,renderer.depth,renderer.size),old)
            self.assertTrue(gl.glIsFramebuffer(old[0]));self.assertTrue(gl.glIsTexture(old[1]))
            self.assertEqual(bytes(gl.glReadPixels(0,0,64,48,gl.GL_RGBA,gl.GL_UNSIGNED_BYTE)),reference)
            original=gl.glTexImage2D
            def fail_transparency(target,level,internal,*args):
                if internal==gl.GL_RGBA32F:raise RuntimeError('Injected transparency OOM')
                return original(target,level,internal,*args)
            with mock.patch.object(gl,'glTexImage2D',side_effect=fail_transparency):
                with self.assertRaises(RenderAllocationError):renderer.target(128,96,transparency=True)
                with self.assertRaises(RenderAllocationError):renderer.target(64,48,transparency=True)
            self.assertEqual((renderer.fbo,renderer.texture,renderer.depth,renderer.size),old)
            self.assertEqual(renderer.oit_fbo,0)
            renderer.target(128,96,transparency=True)
            self.assertEqual(renderer.size,(128,96));self.assertGreater(renderer.oit_fbo,0)
            self.assertFalse(gl.glIsFramebuffer(old[0]))
            self.assertEqual(gl.glGetError(),gl.GL_NO_ERROR)
        finally:renderer.close();win.destroy();self.pump(2)

    def test_allocation_failure_retains_completed_preview_and_quality_retry_recovers(self):
        from unittest import mock
        from gs_studio.viewport import Viewport
        from gs_studio.snapshots import SnapshotPendingError
        from OpenGL import GL as gl
        win=self.Gtk.Window(default_width=128,default_height=128);viewport=Viewport()
        viewport.show_grid=viewport.show_cameras=viewport.show_pivot=False
        viewport.set_frame(frame());warnings=[];viewport.on_error=warnings.append
        win.set_child(viewport);win.present();self.pump()
        previous=viewport.snapshot_rgba().copy();old=viewport._renderer.fbo
        with mock.patch.object(gl,'glTexImage2D',side_effect=RuntimeError('Injected recoverable OOM')):
            viewport.resolution_scale=.5;viewport.queue_render();self.pump(8)
        self.assertIsNone(viewport.render_error);self.assertEqual(viewport._renderer.fbo,old)
        self.assertTrue(viewport._render_pending);self.assertEqual(len(warnings),1)
        with self.assertRaises(SnapshotPendingError):viewport.snapshot_rgba()
        viewport.make_current();gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER,old)
        retained=np.frombuffer(gl.glReadPixels(0,0,previous.shape[1],previous.shape[0],gl.GL_RGBA,gl.GL_UNSIGNED_BYTE),np.uint8).reshape(previous.shape)[::-1]
        np.testing.assert_array_equal(retained,previous)
        viewport.queue_render();self.pump(8)
        self.assertIsNone(viewport.last_render_warning);self.assertFalse(viewport._render_pending)
        self.assertEqual(viewport.snapshot_rgba().shape[:2],(round(viewport.get_height()*.5),round(viewport.get_width()*.5)))
        win.destroy();self.pump(2)

    def test_gpu_metrics_wait_for_ready_queries_and_count_true_completions(self):
        from unittest import mock
        from gs_studio.viewport import Viewport
        from gs_studio.gl_renderer import Renderer
        from OpenGL import GL as gl
        win=self.Gtk.Window(default_width=128,default_height=128);viewport=Viewport()
        win.set_child(viewport);win.present();self.pump();viewport.make_current();viewport.attach_buffers()
        destination=int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        renderer=Renderer();renderer.initialize()
        try:
            metrics=renderer.gpu_metrics
            if not metrics.supported:self.skipTest(metrics.reason)
            k=np.array([[50.,0,32],[0,50.,24],[0,0,1.]])
            renderer.target(64,48);renderer.begin(k,np.eye(4),64,48);renderer.finish(destination,64,48)
            self.assertEqual(renderer.submitted_frame_count,1)
            self.assertEqual(metrics.completed_frame_count,0)
            self.assertIsNone(metrics.summary()['gpu_ms'])
            self.assertEqual(metrics.summary()['gpu_completed_fps'],0)
            with mock.patch.object(gl,'glGetQueryObjectiv',return_value=0),mock.patch.object(metrics,'_read_u64',wraps=metrics._read_u64) as read:
                self.assertFalse(metrics.poll());read.assert_not_called()
            gl.glFinish();self.assertTrue(metrics.poll())
            summary=renderer.performance_metrics()
            self.assertEqual(summary['completed_frame_count'],1)
            self.assertGreater(summary['gpu_render_ms'],0)
            self.assertEqual(summary['gpu_sort_ms'],0)
            self.assertGreater(summary['resident_bytes'],0)
            self.assertTrue(summary['resident_bytes_estimated'])
            renderer.begin(k,np.eye(4),64,48);renderer.finish(destination,64,48);gl.glFinish();metrics.poll()
            self.assertEqual(metrics.completed_frame_count,2)
            self.assertGreater(metrics.timestamps[-1],metrics.timestamps[0])
            self.assertGreater(metrics.summary()['gpu_completed_fps'],0)
            metrics.last_result_at-=1
            self.assertEqual(metrics.summary()['gpu_completed_fps'],0)
            queries=list(metrics.objects)
        finally:
            renderer.close();win.destroy();self.pump(2)
        self.assertTrue(all(not gl.glIsQuery(query) for query in queries))

    def test_paused_viewport_publishes_completed_gpu_metrics_without_redrawing(self):
        from gs_studio.viewport import Viewport
        win=self.Gtk.Window(default_width=128,default_height=128);viewport=Viewport()
        stats=[];viewport.on_stats=stats.append
        win.set_child(viewport);win.present();self.pump(70)
        self.assertTrue(stats)
        if not stats[-1]['gpu_timing_available']:
            win.destroy();self.skipTest(stats[-1]['gpu_timing_reason'])
        self.assertGreaterEqual(stats[-1]['completed_frame_count'],1)
        self.assertIsNotNone(stats[-1]['gpu_ms'])
        submitted=stats[-1]['submitted_frame_count'];self.pump(10)
        self.assertEqual(viewport._renderer.submitted_frame_count,submitted)
        self.assertEqual(stats[-1]['gpu_completed_fps'],0)
        win.destroy();self.pump(2)

    def test_gpu_metrics_skip_external_query_and_disable_on_failure_without_losing_draw(self):
        from unittest import mock
        from gs_studio.viewport import Viewport
        from gs_studio.gl_renderer import Renderer
        from OpenGL import GL as gl
        win=self.Gtk.Window(default_width=128,default_height=128);viewport=Viewport()
        win.set_child(viewport);win.present();self.pump();viewport.make_current();viewport.attach_buffers()
        destination=int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        renderer=Renderer();renderer.initialize()
        try:
            metrics=renderer.gpu_metrics
            if not metrics.supported:self.skipTest(metrics.reason)
            k=np.array([[50.,0,32],[0,50.,24],[0,0,1.]])
            renderer.target(64,48)
            external=int(np.asarray(gl.glGenQueries(1)).reshape(-1)[0])
            gl.glBeginQuery(gl.GL_TIME_ELAPSED,external)
            renderer.begin(k,np.eye(4),64,48);renderer.finish(destination,64,48)
            self.assertEqual(int(gl.glGetQueryiv(gl.GL_TIME_ELAPSED,gl.GL_CURRENT_QUERY)),external)
            self.assertEqual(renderer.submitted_frame_count,1)
            self.assertEqual(metrics.timing_samples_skipped,1)
            gl.glEndQuery(gl.GL_TIME_ELAPSED);gl.glDeleteQueries(1,[external])
            with mock.patch.object(gl,'glBeginQuery',side_effect=RuntimeError('Injected instrumentation failure')):
                renderer.target(64,48);renderer.begin(k,np.eye(4),64,48,background=(.2,.3,.4));renderer.finish(destination,64,48)
            self.assertFalse(metrics.supported);self.assertIn('Injected instrumentation failure',metrics.reason)
            self.assertEqual(renderer.submitted_frame_count,2)
            self.assertIsNone(renderer.performance_metrics()['gpu_ms'])
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER,renderer.fbo)
            pixel=np.frombuffer(gl.glReadPixels(10,10,1,1,gl.GL_RGBA,gl.GL_UNSIGNED_BYTE),np.uint8)
            np.testing.assert_allclose(pixel[:3],[51,77,102],atol=1)
            self.assertEqual(gl.glGetError(),gl.GL_NO_ERROR)
        finally:renderer.close();win.destroy();self.pump(2)

    def test_camera_rig_child_selection_frames_hidden_view_without_gaussian_lookup(self):
        from unittest import mock
        from gs_studio.viewport import Viewport
        viewport=Viewport()
        lookup=mock.Mock(side_effect=AssertionError('Camera view index treated as Gaussian ID'))
        viewport.set_frame(SimpleNamespace(is_prepared=True,ids=np.array([7]),bounds=(np.full(3,-1.),np.ones(3)),object_bounds={},element=lookup))
        entries=[]
        for view,x in [(0,-2.),(1,2.)]:
            pose=np.eye(4);pose[:3,3]=[x,0,3]
            entries.append({'object_id':'rig','view':view,'visible':view==0,'calibration':SimpleNamespace(c2w=pose,K=np.eye(3),width=1,height=1)})
        viewport.set_camera_wireframes(entries)
        viewport.selected={('rig',1)}
        initial=viewport.navigation.c2w.copy()
        viewport.center_on_selection()
        np.testing.assert_array_equal(viewport.navigation.pivot,[2,0,3])
        np.testing.assert_array_equal(viewport.navigation.c2w,initial)
        viewport.frame_selection();np.testing.assert_array_equal(viewport.navigation.pivot,[2,0,3])
        self.assertFalse(viewport._camera_entry_selected(entries[0]))
        self.assertTrue(viewport._camera_entry_selected(entries[1]))
        viewport.selected={('rig',0)};viewport.center_on_selection()
        np.testing.assert_array_equal(viewport.navigation.pivot,[-2,0,3])
        viewport.selected={('rig',None)};viewport.center_on_selection()
        np.testing.assert_array_equal(viewport.navigation.pivot,[0,0,3])
        self.assertTrue(all(viewport._camera_entry_selected(entry) for entry in entries))
        lookup.assert_not_called()

    def test_hidden_camera_entries_do_not_draw_pick_or_expose_paths(self):
        from unittest import mock
        from gs_studio.viewport import Viewport
        win=self.Gtk.Window(default_width=128,default_height=128);viewport=Viewport()
        viewport.navigation.c2w=np.eye(4);viewport.show_grid=viewport.show_pivot=False
        viewport.show_trails=False
        hidden={'object_id':'rig','view':1,'visible':False}
        viewport.set_camera_wireframes([hidden])
        viewport.set_paths([SimpleNamespace(object_id='rig',identity=None,positions=np.array([[0,0,1],[1,0,1]]),times=np.array([0.,1.]),colors=np.ones((2,3)))])
        calls=[];viewport.on_camera_pick=lambda *args:calls.append(args)
        with mock.patch.object(viewport,'_camera_segments',side_effect=AssertionError('Hidden camera projected')) as segments:
            win.set_child(viewport);win.present();self.pump()
            self.assertIsNone(viewport.render_error)
            viewport.pick(viewport.get_width()/2,viewport.get_height()/2)
            self.assertEqual(calls,[]);segments.assert_not_called()
            points,_=viewport._path_segments();self.assertEqual(len(points),0)
        k,_,w,h=viewport.get_camera()
        pixel=np.array([[w/2+.5,h/2-10,1],[w/2+.5,h/2+10,1]])
        visible={'object_id':'rig','view':0,'visible':True,'segments':(pixel@np.linalg.inv(k).T*2).reshape(1,2,3)}
        hidden=dict(hidden,segments=(pixel@np.linalg.inv(k).T).reshape(1,2,3))
        with mock.patch.object(viewport,'_camera_segments',side_effect=lambda entry:entry['segments']) as segments:
            viewport.set_paths([]);viewport.set_camera_wireframes([hidden,visible]);self.pump(5)
            viewport.pick(w/2,h/2)
            self.assertEqual(calls[-1],('rig',0,False))
            self.assertTrue(all(call.args[0] is visible for call in segments.call_args_list))
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
