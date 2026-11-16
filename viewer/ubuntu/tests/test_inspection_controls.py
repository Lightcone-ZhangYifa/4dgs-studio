"""Mapped GTK controls: timeline editing and linked image inspection semantics.

Controller callbacks receive explicit modifier/gesture state; no physical mouse
or touchpad input is synthesized. Widgets use real GTK allocations and drawing.
"""
import os
import threading
import time
from types import SimpleNamespace
import unittest

import numpy as np


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"), "GTK controls need a display; use xvfb-run")
class InspectionControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_foreign("cairo")
        from gi.repository import Gdk, GLib, Gtk
        from gs_studio.timeline import Timeline
        from gs_studio.image_view import ComparisonCanvas
        cls.Gdk, cls.GLib, cls.Gtk = Gdk, GLib, Gtk
        cls.Timeline, cls.ComparisonCanvas = Timeline, ComparisonCanvas
        Gtk.init()

    def setUp(self):
        self.window = None

    def tearDown(self):
        if self.window:
            self.window.destroy()

    def mapped(self, widget, width=768, height=360):
        self.window = self.Gtk.Window(child=widget, default_width=width, default_height=height)
        self.window.present()
        context, deadline = self.GLib.MainContext.default(), time.monotonic()+5
        while time.monotonic() < deadline:
            for _ in range(30):
                if not context.pending():
                    break
                context.iteration(False)
            if widget.get_mapped() and widget.get_width() > 0 and widget.get_height() > 0:
                return widget
            threading.Event().wait(.005)
        self.fail("GTK inspection control did not receive an allocation")

    def modifiers(self, alt=False):
        return SimpleNamespace(get_current_event_state=lambda: self.Gdk.ModifierType.ALT_MASK if alt else self.Gdk.ModifierType(0))

    def timeline(self):
        timeline = self.mapped(self.Timeline(), height=108)
        timeline.times = np.array([0., 20., 50., 100.])
        timeline.fit()
        return timeline

    def image_canvas(self):
        canvas = self.mapped(self.ComparisonCanvas(), width=800, height=400)
        rng = np.random.default_rng(812)
        first, second = [rng.random((96, 128, 3), dtype=np.float32) for _ in range(2)]
        canvas.set_images(first, second, "srgb")
        return canvas, first, second

    def key_controller(self, widget):
        controllers = widget.observe_controllers()
        return next(controllers.get_item(i) for i in range(controllers.get_n_items())
                    if isinstance(controllers.get_item(i), self.Gtk.EventControllerKey))

    def test_timeline_scroll_and_pinch_keep_pointer_time_fixed(self):
        timeline = self.timeline()
        timeline.snap = False
        pointer = (timeline.get_width()*.73, 30)
        timeline._pointer = pointer
        before = timeline._time(pointer[0])
        span = timeline.upper-timeline.lower
        timeline._scroll(self.modifiers(), 0, -2)
        self.assertAlmostEqual(timeline._time(pointer[0]), before, places=10)
        self.assertLess(timeline.upper-timeline.lower, span)
        initial_span = timeline.upper-timeline.lower
        timeline._pinch(None, 1.5)
        timeline._pinch(None, 2.)
        self.assertAlmostEqual(timeline._time(pointer[0]), before, places=10)
        self.assertAlmostEqual(timeline.upper-timeline.lower, initial_span/2, places=10)
        np.testing.assert_array_equal(timeline.times, [0,20,50,100])

    def test_alt_scrub_temporarily_bypasses_snap_without_changing_preference(self):
        timeline = self.timeline()
        sought = []
        timeline.on_seek = sought.append
        x = timeline._x(50)+3
        timeline._begin(self.modifiers(), x, 35)
        timeline._end(self.modifiers(), 0, 0)
        self.assertEqual(sought[-1], 50)
        timeline._begin(self.modifiers(alt=True), x, 35)
        self.assertNotEqual(sought[-1], 50)
        self.assertLess(abs(sought[-1]-50), 1)
        timeline._end(self.modifiers(alt=True), 0, 0)
        self.assertTrue(timeline.snap)
        timeline._begin(self.modifiers(), x, 35)
        self.assertEqual(sought[-1], 50)

    def test_escape_cancels_drag_without_committing_key_retime(self):
        timeline = self.timeline()
        timeline.keys = [20,50]
        selected, moved = [], []
        timeline.on_key_select = selected.append
        timeline.on_key_move = lambda index,value: moved.append((index,value))
        x = timeline._x(20)
        timeline._begin(self.modifiers(), x, 78)
        timeline._update(self.modifiers(), 60, 0)
        self.assertEqual(selected, [0])
        self.assertNotEqual(timeline._ghost, 20)
        controller = self.key_controller(timeline)
        self.assertTrue(controller.emit("key-pressed", self.Gdk.KEY_Escape, 0, self.Gdk.ModifierType(0)))
        timeline._update(self.modifiers(), 80, 0)
        timeline._end(self.modifiers(), 80, 0)
        self.assertEqual(moved, [])
        self.assertEqual(timeline.keys, [20,50])
        timeline._begin(self.modifiers(), x, 78)
        timeline._update(self.modifiers(), 60, 0)
        timeline._end(self.modifiers(), 60, 0)
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0][0], 0)
        self.assertGreater(moved[0][1], 20)

    def test_timeline_keyboard_steps_native_samples_home_fits_and_space_plays(self):
        timeline = self.timeline()
        timeline.time = 50
        sought, playing = [], []
        timeline.on_seek = sought.append
        timeline.on_play = lambda: playing.append(True)
        controller = self.key_controller(timeline)
        for key in (self.Gdk.KEY_Left,self.Gdk.KEY_Right,self.Gdk.KEY_space):
            self.assertTrue(controller.emit("key-pressed", key, 0, self.Gdk.ModifierType(0)))
        self.assertEqual(sought, [20,100])
        self.assertEqual(playing, [True])
        timeline.lower, timeline.upper = 45,55
        controller.emit("key-pressed", self.Gdk.KEY_Home, 0, self.Gdk.ModifierType(0))
        self.assertLessEqual(timeline.lower, 0)
        self.assertGreaterEqual(timeline.upper, 100)
        self.assertFalse(controller.emit("key-pressed", self.Gdk.KEY_a, 0, self.Gdk.ModifierType(0)))

    def test_preview_range_shades_only_its_native_time_interval(self):
        import cairo
        timeline = self.timeline()
        width, height = timeline.get_width(), timeline.get_height()
        def image():
            surface = cairo.ImageSurface(cairo.FORMAT_RGB24,width,height)
            timeline._draw(timeline,cairo.Context(surface),width,height)
            return np.frombuffer(surface.get_data(),np.uint8).reshape(height,surface.get_stride()).copy()
        unshaded = image()
        timeline.preview_range = (20,60)
        shaded = image()
        inside = slice(int(timeline._x(31))*4,int(timeline._x(34))*4)
        outside = slice(int(timeline._x(81))*4,int(timeline._x(84))*4)
        self.assertGreater(np.abs(shaded[85:100,inside].astype(int)-unshaded[85:100,inside].astype(int)).max(), 10)
        np.testing.assert_array_equal(shaded[85:100,outside],unshaded[85:100,outside])

    @staticmethod
    def source_point(canvas, pane, pointer):
        """Inverse the visible image rectangle at a widget coordinate."""
        surface = canvas.images[pane][0]
        width, height = surface.get_width(),surface.get_height()
        pane_width = canvas.get_width()/len(canvas.images)
        scale = min((pane_width-24)/width,(canvas.get_height()-40)/height)*canvas.zoom
        left = pane*pane_width+(pane_width-width*scale)/2+canvas.pan[0]
        top = 28+(canvas.get_height()-28-height*scale)/2+canvas.pan[1]
        return np.array([(pointer[0]-left)/scale,(pointer[1]-top)/scale])

    def test_plain_image_scroll_pans_and_alt_zoom_preserves_both_pane_anchors(self):
        canvas, first, second = self.image_canvas()
        images = [pixels.copy() for _,pixels in canvas.images]
        inputs = [first.copy(),second.copy()]
        canvas._scroll(self.modifiers(), 1.25,-2)
        self.assertEqual(canvas.zoom, 1)
        self.assertLess(canvas.pan[0],0)
        self.assertGreater(canvas.pan[1],0)
        pane_width = canvas.get_width()/2
        canvas._pointer = (pane_width+pane_width*.63,canvas.get_height()*.57)
        left_pointer = (canvas._pointer[0]-pane_width,canvas._pointer[1])
        before = [self.source_point(canvas,0,left_pointer),self.source_point(canvas,1,canvas._pointer)]
        canvas._scroll(self.modifiers(alt=True),0,-2)
        self.assertGreater(canvas.zoom,1)
        np.testing.assert_allclose(self.source_point(canvas,0,left_pointer),before[0],atol=1e-10)
        np.testing.assert_allclose(self.source_point(canvas,1,canvas._pointer),before[1],atol=1e-10)
        for (_,pixels), original in zip(canvas.images,images):
            np.testing.assert_array_equal(pixels,original)
        np.testing.assert_array_equal(first,inputs[0])
        np.testing.assert_array_equal(second,inputs[1])

    def test_image_pinch_is_cumulative_fit_and_actual_pixels_keep_raw_pair(self):
        canvas, _, _ = self.image_canvas()
        images = [pixels.copy() for _,pixels in canvas.images]
        canvas._pointer = (canvas.get_width()*.3,canvas.get_height()*.4)
        anchor = self.source_point(canvas,0,canvas._pointer)
        canvas._pinch(None,1.5)
        canvas._pinch(None,2.)
        self.assertAlmostEqual(canvas.zoom,2)
        np.testing.assert_allclose(self.source_point(canvas,0,canvas._pointer),anchor,atol=1e-10)
        canvas.actual_pixels()
        sample0 = self.source_point(canvas,0,(100,100))
        sample1 = self.source_point(canvas,0,(100+1/canvas.get_scale_factor(),100))
        self.assertAlmostEqual(sample1[0]-sample0[0],1,places=10)
        canvas.reset()
        self.assertEqual(canvas.zoom,1)
        self.assertEqual(canvas.pan,(0,0))
        pair = canvas.snapshot_surface()
        self.assertEqual((pair.get_width(),pair.get_height()),(256,96))
        for (_,pixels),original in zip(canvas.images,images):
            np.testing.assert_array_equal(pixels,original)


if __name__ == "__main__":
    unittest.main()
