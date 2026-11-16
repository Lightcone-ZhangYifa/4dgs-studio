"""PNG encoding/atomic writes and actual GTK workspace/GL capture checks."""
import os
import shutil
import subprocess
import sys
from concurrent.futures import CancelledError
from pathlib import Path
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

import numpy as np
from gs_studio.snapshots import (snapshot_png, write_png, PNG_SIGNATURE,
                                 capture_png_when_ready, SnapshotPendingError,
                                 _workspace_pixel_size,MAX_WORKSPACE_PIXELS,MAX_WORKSPACE_SIDE)


def decode_png(data):
    """Small test-only RGB/RGBA decoder, including all PNG scanline filters."""
    if data[:8] != PNG_SIGNATURE:
        raise AssertionError("Invalid PNG signature")
    compressed, offset, header = [], 8, None
    while offset < len(data):
        n = struct.unpack_from(">I", data, offset)[0]
        kind, payload = data[offset+4:offset+8], data[offset+8:offset+8+n]
        expected = struct.unpack_from(">I", data, offset+8+n)[0]
        assert zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff == expected
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.append(payload)
        offset += n + 12
    width, height, depth, color, _, _, interlace = header
    assert depth == 8 and color in (2, 6) and interlace == 0
    channels = 3 if color == 2 else 4
    raw = zlib.decompress(b"".join(compressed))
    stride = width * channels
    result = np.zeros((height, stride), np.uint8)
    def paeth(a, b, c):
        p = a + b - c
        d = [abs(p-a), abs(p-b), abs(p-c)]
        return (a, b, c)[d.index(min(d))]
    for y in range(height):
        pos = y * (stride + 1)
        mode = raw[pos]
        scan = raw[pos+1:pos+1+stride]
        for x, value in enumerate(scan):
            left = int(result[y, x-channels]) if x >= channels else 0
            above = int(result[y-1, x]) if y else 0
            corner = int(result[y-1, x-channels]) if y and x >= channels else 0
            prediction = (0, left, above, (left+above)//2, paeth(left, above, corner))[mode]
            result[y, x] = (value + prediction) & 255
    return result.reshape(height, width, channels)


class EncodingTests(unittest.TestCase):
    def test_workspace_backing_extent_limits_are_checked_before_allocation(self):
        self.assertEqual(_workspace_pixel_size(8192,2048,2),(MAX_WORKSPACE_SIDE,4096))
        self.assertEqual(16384*4096,MAX_WORKSPACE_PIXELS)
        for width,height,scale in ((8193,1,2),(8192,2049,2),(0,10,2),(10,10,0)):
            with self.subTest(size=(width,height,scale)):
                widget=SimpleNamespace(get_width=lambda:width,get_height=lambda:height,
                    get_scale_factor=lambda:scale,get_mapped=lambda:True,
                    get_native=mock.Mock(side_effect=AssertionError('allocation attempted')))
                with self.assertRaises(ValueError):
                    snapshot_png(widget)
                widget.get_native.assert_not_called()

    def test_rgba_rows_colors_alpha_and_noncontiguous_input(self):
        pixels = np.array([[[255, 0, 0, 255], [0, 0, 255, 128]],
                           [[0, 255, 0, 1], [250, 130, 30, 0]]], np.uint8)
        for source in (pixels, pixels[::-1, ::-1]):
            result = snapshot_png(SimpleNamespace(snapshot_rgba=lambda: source))
            np.testing.assert_array_equal(decode_png(result), source)

    def test_invalid_pixels_refused(self):
        for pixels in (np.zeros((0, 1, 4), np.uint8), np.zeros((1, 1, 3), np.uint8), np.zeros((1, 1, 4), float)):
            with self.assertRaises(ValueError):
                snapshot_png(SimpleNamespace(snapshot_rgba=lambda: pixels))

    def test_atomic_write_unicode_path_and_failure_preserves_previous(self):
        data = snapshot_png(SimpleNamespace(snapshot_rgba=lambda: np.zeros((1, 2, 4), np.uint8)))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "场景 screenshot.png"
            write_png(target, data)
            self.assertEqual(target.read_bytes(), data)
            with mock.patch("gs_studio.snapshots.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    write_png(target, data + b"new")
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(list(Path(directory).iterdir()), [target])
            with self.assertRaises(ValueError):
                write_png(target, b"not an image")
            self.assertEqual(target.read_bytes(), data)

    def test_symlink_export_cannot_modify_referenced_source(self):
        data = snapshot_png(SimpleNamespace(snapshot_rgba=lambda: np.zeros((1, 1, 4), np.uint8)))
        with tempfile.TemporaryDirectory() as directory:
            source, link = Path(directory) / "source.gsv", Path(directory) / "linked.png"
            source.write_bytes(b"private reconstruction")
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                write_png(link, data)
            self.assertEqual(source.read_bytes(), b"private reconstruction")


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"), "Native snapshots require a display; use xvfb-run")
class NativeSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_foreign("cairo")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk, GLib
        # Import the GL frontend before GSK can leave an ES context current;
        # this is also the application's startup/import order on Ubuntu.
        from gs_studio.viewport import Viewport
        cls.Viewport = Viewport
        cls.Gtk, cls.GLib = Gtk, GLib
        Gtk.init()

    def pump(self, condition, timeout=5):
        context = self.GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for _ in range(30):
                if not context.pending():
                    break
                context.iteration(False)
            if condition():
                return
            threading.Event().wait(.005)
        self.fail("GTK snapshot did not complete")

    def test_native_widget_snapshot_preserves_raster_and_color(self):
        canvas = self.Gtk.DrawingArea(content_width=160, content_height=100)
        drawn = []
        def draw(widget, cr, width, height):
            cr.set_source_rgb(1, 0, 0)
            cr.paint()
            cr.set_source_rgb(0, 0, 1)
            cr.rectangle(0, height/2, width, height/2)
            cr.fill()
            drawn.append(True)
        canvas.set_draw_func(draw)
        window = self.Gtk.Window(child=canvas)
        self.addCleanup(window.destroy)
        window.present()
        self.pump(lambda: drawn and canvas.get_mapped())
        pixels = decode_png(snapshot_png(canvas))
        scale=canvas.get_scale_factor()
        self.assertEqual(pixels.shape[:2], (canvas.get_height()*scale, canvas.get_width()*scale))
        expected_scale=os.environ.get('GSV_EXPECT_SNAPSHOT_SCALE')
        if expected_scale:
            self.assertEqual(scale,int(expected_scale))
        np.testing.assert_array_equal(pixels[10, 10, :3], [255, 0, 0])
        np.testing.assert_array_equal(pixels[-10, 10, :3], [0, 0, 255])

    def test_hidpi_widget_framebuffer_and_comparison_exports_in_independent_display(self):
        launcher=shutil.which('xvfb-run')
        if not launcher:
            self.skipTest('Independent HiDPI GTK verification requires xvfb-run')
        env=os.environ.copy()
        env.update(GDK_BACKEND='x11',GDK_SCALE='2',GDK_DPI_SCALE='1',GDK_DEBUG='no-vsync',
                   GTK_A11Y='none',GIO_USE_VFS='local',GSV_EXPECT_SNAPSHOT_SCALE='2')
        env.pop('WAYLAND_DISPLAY',None)
        tests=Path(__file__).resolve().parent
        env['PYTHONPATH']=str(tests)+os.pathsep+env.get('PYTHONPATH','')
        methods=('test_native_widget_snapshot_preserves_raster_and_color',
                 'test_gl_snapshot_has_preview_pixels_and_preserves_bindings',
                 'test_queued_gl_frame_cannot_export_previous_resolution',
                 'test_comparison_export_retains_source_raster_at_display_scale')
        result=subprocess.run([launcher,'-a','-s','-screen 0 1920x1080x24',sys.executable,
            '-m','unittest',*[f'test_snapshots.NativeSnapshotTests.{method}' for method in methods],'-v'],
            env=env,text=True,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_comparison_export_retains_source_raster_at_display_scale(self):
        from gs_studio.image_view import ComparisonCanvas
        canvas=ComparisonCanvas()
        render=np.zeros((12,20,3),np.float32);render[:,:,0]=1
        reference=np.zeros((12,20,3),np.float32);reference[:,:,2]=1
        canvas.set_images(render,reference,'srgb')
        window=self.Gtk.Window(child=canvas,default_width=320,default_height=180)
        self.addCleanup(window.destroy)
        window.present()
        self.pump(lambda:canvas.get_mapped() and canvas.get_width()>0)
        surface=canvas.snapshot_surface()
        self.assertEqual((surface.get_width(),surface.get_height()),(40,12))
        pixels=np.frombuffer(surface.get_data(),np.uint8).reshape(12,surface.get_stride())[:,:160].reshape(12,40,4)
        np.testing.assert_array_equal(pixels[5,5,:3],[0,0,255])
        np.testing.assert_array_equal(pixels[5,25,:3],[255,0,0])
        np.testing.assert_array_equal(render[:,:,0],1)
        np.testing.assert_array_equal(reference[:,:,2],1)

    def test_gl_snapshot_has_preview_pixels_and_preserves_bindings(self):
        from OpenGL import GL
        from gs_studio.camera import Calibration
        viewport = self.Viewport()
        viewport.show_grid = False
        viewport.set_frame(SimpleNamespace(means=np.array([[0, 0, 2]], np.float32),
            covariance=np.array([np.eye(3)*.01], np.float32), colors=np.array([[1, 0, 0]], np.float32),
            opacity=np.array([1], np.float32), ids=np.array([7], np.int64), object_ids=np.array(["field"], object)))
        window = self.Gtk.Window(child=viewport, default_width=200, default_height=160)
        self.addCleanup(window.destroy)
        window.present()
        self.pump(lambda: viewport._renderer is not None or viewport.render_error is not None)
        self.assertIsNone(viewport.render_error)
        self.pump(lambda: viewport._renderer.fbo != 0)
        viewport.set_camera(Calibration(np.array([[100, 0, 100], [0, 100, 80], [0, 0, 1]], float), np.eye(4), 200, 160))
        viewport.queue_render()
        # Wait for the submitted calibrated camera to become the uploaded pose.
        self.pump(lambda: viewport._last_upload_pose is not None and np.array_equal(viewport._last_upload_pose, np.eye(4)))
        viewport.make_current()
        before = [int(GL.glGetIntegerv(x)) for x in (GL.GL_READ_FRAMEBUFFER_BINDING, GL.GL_DRAW_FRAMEBUFFER_BINDING, GL.GL_PACK_ALIGNMENT)]
        pixels = decode_png(snapshot_png(viewport))
        after = [int(GL.glGetIntegerv(x)) for x in (GL.GL_READ_FRAMEBUFFER_BINDING, GL.GL_DRAW_FRAMEBUFFER_BINDING, GL.GL_PACK_ALIGNMENT)]
        self.assertEqual(before, after)
        self.assertEqual(pixels.shape[:2], viewport._renderer.size[::-1])
        self.assertGreater(pixels[:, :, 0].max(), 200)
        self.assertTrue(np.any(pixels[:, :, 0].astype(int) - pixels[:, :, 1].astype(int) > 150))

    def assert_disconnected(self, handle):
        self.assertFalse(handle.pending)
        self.assertIsNone(handle._clock)
        self.assertEqual(handle._clock_handler,0)
        self.assertEqual(handle._timeout,0)
        self.assertEqual(handle._retry,0)
        self.assertEqual(handle._widget_handlers,[])

    def painted_area(self, color, width=180, height=90):
        area = self.Gtk.DrawingArea(content_width=width,content_height=height)
        painted = []
        def draw(widget,cr,w,h):
            cr.set_source_rgb(*color)
            cr.paint()
            painted.append((w,h))
        area.set_draw_func(draw)
        return area,painted

    def test_async_capture_waits_for_first_real_paint_and_finishes_once(self):
        canvas,painted = self.painted_area((.1,.8,.2))
        window = self.Gtk.Window(child=canvas)
        self.addCleanup(window.destroy)
        completed = []
        handle = capture_png_when_ready(window,lambda data,error: completed.append((data,error)))
        self.assertTrue(handle.pending)
        window.present()
        self.pump(lambda: bool(completed))
        png,error = completed[0]
        self.assertIsNone(error)
        self.assertTrue(painted)
        pixels = decode_png(png)
        self.assertGreater(pixels[pixels.shape[0]//2,pixels.shape[1]//2,1],180)
        self.assert_disconnected(handle)
        previous = len(painted)
        canvas.queue_draw()
        self.pump(lambda: len(painted)>previous)
        handle.cancel()
        self.assertEqual(len(completed),1)

    def test_async_capture_includes_new_layout_and_real_pixels(self):
        first,painted = self.painted_area((.1,.8,.2),height=80)
        content = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL)
        caption = self.Gtk.Label(label="Initial layout")
        content.append(caption)
        content.append(first)
        window = self.Gtk.Window(child=content)
        self.addCleanup(window.destroy)
        window.present()
        self.pump(lambda: bool(painted))
        blue,blue_painted = self.painted_area((.1,.2,.9),height=50)
        caption.set_label("Updated layout\nWith another line")
        content.append(blue)
        completed = []
        handle = capture_png_when_ready(window,lambda data,error: completed.append((data,error)),
                                        ready=lambda: blue.get_height()>0)
        self.pump(lambda: bool(completed))
        png,error = completed[0]
        self.assertIsNone(error)
        self.assertTrue(blue_painted)
        pixels = decode_png(png)
        self.assertGreater(pixels[-10,pixels.shape[1]//2,2],200)
        self.assertLess(pixels[-10,pixels.shape[1]//2,0],50)
        self.assert_disconnected(handle)

    def test_async_cancel_and_unrealize_disconnect_without_late_completion(self):
        for mode in ("cancel","unrealize","token"):
            with self.subTest(mode=mode):
                canvas,painted = self.painted_area((.7,.2,.1))
                window = self.Gtk.Window(child=canvas)
                window.present()
                self.pump(lambda: bool(painted))
                completed,cancelled = [],[False]
                handle = capture_png_when_ready(window,lambda data,error: completed.append((data,error)),
                                                ready=lambda:False,cancelled=lambda:cancelled[0])
                if mode == "cancel":
                    handle.cancel()
                elif mode == "unrealize":
                    window.destroy()
                else:
                    cancelled[0] = True
                    canvas.queue_draw()
                self.pump(lambda: bool(completed))
                self.assertIsNone(completed[0][0])
                self.assertIsInstance(completed[0][1],CancelledError)
                self.assert_disconnected(handle)
                handle.cancel()
                self.assertEqual(len(completed),1)
                window.destroy()

    def test_async_readiness_timeout_is_bounded_and_event_loop_runs(self):
        canvas,painted = self.painted_area((.7,.2,.1))
        window = self.Gtk.Window(child=canvas)
        self.addCleanup(window.destroy)
        window.present()
        self.pump(lambda: bool(painted))
        completed,heartbeat = [],[]
        start = time.monotonic()
        handle = capture_png_when_ready(window,lambda data,error: completed.append((data,error)),ready=lambda:False)
        self.GLib.idle_add(lambda: heartbeat.append(True) or False)
        self.pump(lambda: bool(completed),timeout=4)
        self.assertTrue(heartbeat)
        self.assertLess(time.monotonic()-start,3.5)
        self.assertIsNone(completed[0][0])
        self.assertIsInstance(completed[0][1],TimeoutError)
        self.assert_disconnected(handle)

    def test_queued_gl_frame_cannot_export_previous_resolution(self):
        viewport = self.Viewport()
        window = self.Gtk.Window(child=viewport,default_width=200,default_height=160)
        self.addCleanup(window.destroy)
        window.present()
        self.pump(lambda: viewport._renderer is not None and viewport._renderer.fbo and not viewport._render_pending)
        viewport.resolution_scale = .5
        viewport.queue_render()
        with self.assertRaises(SnapshotPendingError):
            snapshot_png(viewport)
        completed = []
        handle = capture_png_when_ready(viewport,lambda data,error: completed.append((data,error)))
        self.pump(lambda: bool(completed))
        png,error = completed[0]
        self.assertIsNone(error)
        pixels = decode_png(png)
        expected = tuple(max(1,round(value*viewport.get_scale_factor()*.5))
                         for value in (viewport.get_width(),viewport.get_height()))
        self.assertEqual(pixels.shape[:2],expected[::-1])
        self.assertFalse(viewport._render_pending)
        self.assert_disconnected(handle)


if __name__ == "__main__":
    unittest.main()
