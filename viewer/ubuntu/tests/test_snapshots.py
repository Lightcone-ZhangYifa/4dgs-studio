"""PNG encoding/atomic writes and actual GTK workspace/GL capture checks."""
import os
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
from gs_studio.snapshots import snapshot_png, write_png, PNG_SIGNATURE


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
        self.assertEqual(pixels.shape[:2], (canvas.get_height(), canvas.get_width()))
        np.testing.assert_array_equal(pixels[10, 10, :3], [255, 0, 0])
        np.testing.assert_array_equal(pixels[-10, 10, :3], [0, 0, 255])

    def test_gl_snapshot_has_preview_pixels_and_preserves_bindings(self):
        from gs_studio.viewport import Viewport
        from OpenGL import GL
        from gs_studio.camera import Calibration
        viewport = Viewport()
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


if __name__ == "__main__":
    unittest.main()
