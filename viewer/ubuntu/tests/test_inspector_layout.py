"""Mapped camera-editor layout: divider resizing must not clip native controls."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest


@unittest.skipUnless(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'),
                     'GTK layout requires a display; use xvfb-run')
class InspectorLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_foreign('cairo')
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Adw, Gio, GLib, Gtk
        from gs_studio.application import StudioWindow
        cls.GLib, cls.Gtk, cls.StudioWindow = GLib, Gtk, StudioWindow
        cls.app = Adw.Application(application_id='io.github.LightconeZhangYifa.GSVStudio.LayoutTests',
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def wait(self, predicate):
        context = self.GLib.MainContext.default()
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            for _ in range(40):
                if not context.pending():
                    break
                context.iteration(False)
            if predicate():
                return
            threading.Event().wait(.005)
        self.fail('GTK layout did not settle')

    def bounds(self, widget, parent):
        valid, bounds = widget.compute_bounds(parent)
        self.assertTrue(valid)
        return bounds

    def descendants(self, widget):
        yield widget
        child = widget.get_first_child()
        while child is not None:
            yield from self.descendants(child)
            child = child.get_next_sibling()

    def test_camera_controls_stay_inside_window_when_divider_requests_a_narrow_inspector(self):
        from test_application import write_scene

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'scene.gsv'
            write_scene(source)
            window = self.StudioWindow(self.app)
            errors = []
            window.error = lambda error: errors.append(str(error))
            try:
                window.present()
                window.open_path(source)
                self.wait(lambda: window.document is not None and window.frame is not None
                          and not window.frames.running)
                window.create_camera()
                window.track_name.set_text('A long authored camera name · '+('selected view ' * 5))
                window.track_name.emit('activate')
                window.key_drop.set_selected(0)
                window.key_time.set_text('0.12345678901234567')
                window.key_time.emit('activate')
                self.wait(lambda: window.camera_editor.get_mapped() and not window.frames.running)
                minimum = window.inspector.measure(self.Gtk.Orientation.HORIZONTAL, -1)[0]
                self.assertGreater(minimum, 80)

                # Exercise the same resize request a divider drag can produce.
                # The child must receive its measured minimum rather than draw
                # beyond a smaller slot into the window's clipped right edge.
                window.inner_paned.set_position(window.inner_paned.get_width()-80)
                self.wait(lambda: window.inspector.get_width() >= minimum and
                          self.bounds(window.inspector, window).get_x()+
                          self.bounds(window.inspector, window).get_width() <= window.get_width())
                for widget in self.descendants(window.camera_editor):
                    if not widget.get_mapped():
                        continue
                    bounds = self.bounds(widget, window)
                    self.assertGreaterEqual(bounds.get_x(), 0, type(widget).__name__)
                    self.assertLessEqual(bounds.get_x()+bounds.get_width(), window.get_width(),
                                         type(widget).__name__)
                key = self.bounds(window.key_drop, window.inspector)
                self.assertGreaterEqual(key.get_x(), 0)
                self.assertLessEqual(key.get_x()+key.get_width(), window.inspector.get_width())
                self.assertEqual(window.key_drop.get_selected_item().get_string(), '0.12345678901234566')
                self.assertEqual(errors, [])
            finally:
                if not window.closed:
                    window._finish_close()
                self.wait(lambda: not any(worker.running for worker in
                          (window.loader, window.frames, window.comparisons, window.file_jobs)))


if __name__ == '__main__':
    unittest.main()
