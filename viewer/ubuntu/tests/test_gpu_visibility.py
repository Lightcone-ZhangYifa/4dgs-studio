"""Nonblocking query lifecycle and real GL3.3 frustum-count correctness."""
import os
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np
from gs_studio.gpu_visibility import GpuVisibility


class VisibilityQueryTests(unittest.TestCase):
    def tracker(self):
        tracker=GpuVisibility(interval=0,maximum_queries=2)
        tracker.supported=True
        tracker.objects={11}
        tracker.pending=[{'query':11,'epoch':0,'sequence':42,'submitted_at':time.monotonic(),
                          'metadata':{'source_time':.25,'pose':np.eye(4).tolist()}}]
        return tracker

    def test_pending_query_is_never_read_before_ready(self):
        tracker=self.tracker()
        with mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectiv',return_value=False), \
             mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectuiv',side_effect=AssertionError('blocking read')):
            self.assertFalse(tracker.poll())
        self.assertIsNone(tracker.summary()['visible_count'])
        self.assertTrue(tracker.summary()['visibility_pending'])
        with mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectiv',return_value=True), \
             mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectuiv',return_value=17):
            self.assertTrue(tracker.poll())
        summary=tracker.summary()
        self.assertEqual(summary['visible_count'],17)
        self.assertEqual(summary['visibility_sequence'],42)
        self.assertEqual(summary['visibility_source_time'],.25)
        self.assertGreaterEqual(summary['visibility_age_ms'],0)

    def test_source_reset_discards_completed_old_epoch_count(self):
        tracker=self.tracker()
        tracker.reset()
        with mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectiv',return_value=True), \
             mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectuiv',side_effect=AssertionError('old result read')):
            self.assertFalse(tracker.poll())
        self.assertIsNone(tracker.summary()['visible_count'])
        self.assertEqual(tracker.free,[11])

    def test_bounded_queries_skip_without_waiting_and_fail_to_unknown(self):
        tracker=self.tracker()
        tracker.objects.add(12)
        with mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectiv',return_value=False), \
             mock.patch('gs_studio.gpu_visibility.gl.glGetQueryiv',return_value=0), \
             mock.patch('gs_studio.gpu_visibility.gl.glGenQueries',side_effect=AssertionError('unbounded allocation')):
            self.assertFalse(tracker.submit(lambda program:None,43))
        self.assertEqual(tracker.samples_skipped,1)
        with mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectiv',side_effect=RuntimeError('query failure')):
            tracker.poll()
        self.assertFalse(tracker.supported)
        self.assertIsNone(tracker.summary()['visible_count'])
        self.assertIn('rendering continues',tracker.reason)

    def test_sampling_throttle_keeps_recent_completed_result_during_playback(self):
        tracker=self.tracker()
        with mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectiv',return_value=True), \
             mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectuiv',return_value=5):
            tracker.poll()
        tracker.next_sample_at=time.monotonic()+10
        for sequence in range(43,103):
            self.assertFalse(tracker.submit(lambda program:self.fail('throttled GPU draw'),sequence))
            self.assertEqual(tracker.summary()['visible_count'],5)

    def test_empty_geometry_is_exact_zero_without_a_gpu_sample_or_old_query_leak(self):
        tracker=self.tracker()
        tracker.mark_empty()
        with mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectiv',return_value=True), \
             mock.patch('gs_studio.gpu_visibility.gl.glGetQueryObjectuiv',side_effect=AssertionError('old result read')):
            tracker.poll()
        summary=tracker.summary()
        self.assertEqual(summary['visible_count'],0)
        self.assertEqual(summary['visibility_method'],'empty')
        self.assertIn('no GPU sample',summary['visibility_reason'])
        self.assertEqual(tracker.samples_submitted,0)


@unittest.skipUnless(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'),'Real GL checks need a display; use xvfb-run')
class VisibilityGLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version('Gtk','4.0')
        from gi.repository import Gtk,GLib
        cls.Gtk,cls.GLib=Gtk,GLib
        Gtk.init()

    def pump(self):
        context=self.GLib.MainContext.default()
        for _ in range(30):
            if not context.pending():break
            context.iteration(False)

    def setUp(self):
        from OpenGL import GL as gl
        from gs_studio.gl_renderer import Renderer
        self.gl=gl
        self.area=self.Gtk.GLArea()
        self.area.set_required_version(3,3)
        self.window=self.Gtk.Window(child=self.area,default_width=128,default_height=96)
        self.window.present()
        deadline=time.monotonic()+5
        while not self.area.get_realized() and time.monotonic()<deadline:
            self.pump();time.sleep(.005)
        self.area.make_current()
        self.assertIsNone(self.area.get_error())
        self.renderer=Renderer(sort_mode='cpu');self.renderer.initialize()
        self.assertTrue(self.renderer.gpu_visibility.supported,self.renderer.gpu_visibility.reason)
        self.renderer.gpu_visibility.interval=0
        self.k=np.array([[100.,17.,63.],[0.,100.,43.],[0.,0.,1.]])
        self.pose=np.eye(4)
        # The probe owns an offscreen destination; it does not rely on GTK
        # supplying its render-signal framebuffer outside that signal.
        self.destination=int(gl.glGenFramebuffers(1));self.destination_texture=int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D,self.destination_texture)
        gl.glTexImage2D(gl.GL_TEXTURE_2D,0,gl.GL_RGBA8,128,96,0,gl.GL_RGBA,gl.GL_UNSIGNED_BYTE,None)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,self.destination)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER,gl.GL_COLOR_ATTACHMENT0,gl.GL_TEXTURE_2D,self.destination_texture,0)
        self.assertEqual(gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER),gl.GL_FRAMEBUFFER_COMPLETE)

    def tearDown(self):
        self.area.make_current()
        self.renderer.close()
        self.gl.glDeleteFramebuffers(1,[self.destination]);self.gl.glDeleteTextures([self.destination_texture])
        self.window.destroy();self.pump()

    def fixture(self):
        means=np.array([[0,0,2],[0,0,.01],[0,0,-1],[20,0,2],[1.4,0,2],
                        [0,0,2],[0,0,2],[0,0,2],[0,20,2],[0,0,2]],np.float32)
        covariance=np.tile(np.eye(3,dtype=np.float32)*.01,(len(means),1,1))
        covariance[7]=np.nan
        return SimpleNamespace(means=means,covariance=covariance,colors=np.ones_like(means),
            opacity=np.array([.8,.8,.8,.8,.8,0,.003,.8,.8,1/255],np.float32),
            ids=np.arange(len(means),dtype=np.int64),object_ids=np.full(len(means),'field'))

    def draw(self,frame,**controls):
        gl=self.gl;r=self.renderer
        self.area.make_current();r.upload(frame,self.pose,set())
        destination=self.destination
        r.target(128,96);r.begin(self.k,self.pose,128,96,near_depth=.01,**controls)
        r.splats();r.splats(depth_only=True);r.finish(destination,128,96);gl.glFlush()

    def completed_count(self):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            self.area.make_current();self.renderer.poll_metrics()
            if not self.renderer.gpu_visibility.pending:
                return self.renderer.performance_metrics()['visible_count']
            self.pump();time.sleep(.002)
        self.fail('visibility query did not complete')

    def test_real_frustum_count_matches_display_culling_and_scale_opacity_controls(self):
        self.draw(self.fixture())
        self.assertEqual(self.completed_count(),3)  # Inside, bbox touches, threshold alpha.
        self.draw(self.fixture(),gaussian_scale=.01)
        self.assertEqual(self.completed_count(),2)
        self.draw(self.fixture(),opacity_scale=0)
        self.assertEqual(self.completed_count(),0)
        self.assertEqual(self.gl.glGetError(),self.gl.GL_NO_ERROR)

    def test_prepared_validity_and_fractional_interpolation_count_source_identities_once(self):
        count=3;first=np.zeros((count,16),np.float32)
        first[:,:3]=[[0,0,2],[.1,0,2],[20,0,2]];first[:,3]=.8
        first[:,4:7]=.1;first[:,7:9]=1;first[:,12:15]=1;first[:,15]=-1
        second=first.copy();second[0,7]=0;second[2,0]=0
        first.flags.writeable=second.flags.writeable=False
        args=dict(is_prepared=True,first=first,second=second,weights=None,ids=np.arange(count,dtype=np.int64),
                  object_ids=np.full(count,'field'),endpoint_key=('visibility-test',),time=.5)
        self.draw(SimpleNamespace(**args,weight=.5))
        self.assertEqual(self.completed_count(),1)
        self.assertEqual(self.renderer.performance_metrics()['visibility_source_time'],.5)
        self.draw(SimpleNamespace(**args,weight=1.))
        self.assertEqual(self.completed_count(),2)
        self.renderer.reset_visibility()
        self.assertIsNone(self.renderer.performance_metrics()['visible_count'])

    def test_query_failure_preserves_display_and_returns_unknown(self):
        with mock.patch('gs_studio.gpu_visibility.gl.glBeginQuery',side_effect=RuntimeError('test query unavailable')):
            self.draw(self.fixture())
        self.assertFalse(self.renderer.gpu_visibility.supported)
        self.assertIsNone(self.renderer.performance_metrics()['visible_count'])
        self.draw(self.fixture())
        self.assertEqual(self.gl.glGetError(),self.gl.GL_NO_ERROR)

    def test_multiple_endpoint_segments_keep_tiny_interior_validity_and_exact_endpoints(self):
        first=np.zeros((3,16),np.float32)
        first[:,:3]=[[0,0,2],[.1,0,2],[20,0,2]];first[:,3]=.8
        first[:,4:7]=.1;first[:,7:9]=1;first[:,12:15]=1
        second=first.copy();second[0,7]=0;second[2,0]=0
        first.flags.writeable=second.flags.writeable=False
        state=SimpleNamespace(is_prepared=True,first=first,second=second,
            weights=np.array([0.,.5,1.],np.float32),weight=None,
            segments=(('a',0,1,1e-300),('b',1,2,.5),('c',2,3,1.)),
            ids=np.arange(3,dtype=np.int64),object_ids=np.array(['a','b','c']),
            endpoint_key=('mixed-visibility',),time=.5)
        self.draw(state)
        self.assertEqual(self.completed_count(),2)

    def test_hide_all_and_all_inactive_keep_exact_zero_with_original_source_denominator(self):
        source=self.fixture()
        self.draw(source)
        self.assertEqual(self.completed_count(),3)
        self.renderer.upload(None,self.pose,set())
        stats=self.renderer.performance_metrics()
        self.assertEqual((stats['visible_count'],stats['source_count']),(0,0))
        inactive=SimpleNamespace(ids=np.empty(0,np.int64),object_ids=np.empty(0,object),
                                 source_count=10,source_layout=('doc','field',10))
        self.renderer.upload(inactive,self.pose,set())
        stats=self.renderer.performance_metrics()
        self.assertEqual((stats['visible_count'],stats['source_count'],stats['active_count']),(0,10,0))

    def test_source_cohort_switch_resets_count_but_time_and_validity_layout_do_not(self):
        source=self.fixture();source.source_count=10;source.source_layout=('doc','field',10)
        self.draw(source)
        self.assertEqual(self.completed_count(),3)
        changed_time=self.fixture();changed_time.source_count=10;changed_time.source_layout=source.source_layout
        self.renderer.upload(changed_time,self.pose,set())
        self.assertEqual(self.renderer.performance_metrics()['visible_count'],3)
        subset=SimpleNamespace(means=source.means[:1],covariance=source.covariance[:1],
            colors=source.colors[:1],opacity=source.opacity[:1],ids=source.ids[:1],object_ids=source.object_ids[:1],
            source_count=1,source_layout=('doc','other-field',1))
        self.renderer.upload(subset,self.pose,set())
        self.assertIsNone(self.renderer.performance_metrics()['visible_count'])
        self.assertEqual(self.renderer.performance_metrics()['source_count'],1)
        self.draw(subset)
        self.assertEqual(self.completed_count(),1)


if __name__=='__main__':unittest.main()
