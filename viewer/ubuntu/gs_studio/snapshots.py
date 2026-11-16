"""Offline PNG export from completed viewport pixels or native GTK content."""
from __future__ import annotations

import os
from concurrent.futures import CancelledError
from pathlib import Path
import struct
import tempfile
import zlib

import numpy as np


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class SnapshotPendingError(RuntimeError):
    """The widget has not completed the requested frame yet; retry after paint."""


def _chunk(kind, data):
    checksum = zlib.crc32(data, zlib.crc32(kind)) & 0xffffffff
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def _rgba_png(pixels):
    """Encode top-left RGBA rows using the lossless PNG format, without Pillow."""
    pixels = np.asarray(pixels)
    if pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 4 or min(pixels.shape[:2]) <= 0:
        raise ValueError("Snapshot requires nonempty uint8 RGBA pixels")
    height, width = pixels.shape[:2]
    if width >= 2**31 or height >= 2**31:
        raise ValueError("Snapshot dimensions exceed the PNG format limit")
    compressor = zlib.compressobj()
    blocks = []
    for row in pixels:
        blocks.append(compressor.compress(b"\0" + row.tobytes()))
    blocks.append(compressor.flush())
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", b"".join(blocks)) + _chunk(b"IEND", b"")


def snapshot_png(widget) -> bytes:
    """Capture a visible widget on GTK's UI thread, returning encoded PNG bytes.

    GLArea exports its completed private framebuffer to avoid capturing an
    unpopulated GTK paintable between GL frames. A workspace/comparison widget
    uses GTK's renderer so native text, controls and images retain their layout.
    """
    capture = getattr(widget, "snapshot_rgba", None)
    if callable(capture):
        return _rgba_png(capture())
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gsk", "4.0")
    # Import Gsk before to_node(): PyGObject needs the registered render-node
    # subclasses to convert the native snapshot into a Python wrapper.
    from gi.repository import Gdk, Gsk, Gtk
    del Gdk, Gsk
    width, height = widget.get_width(), widget.get_height()
    if width <= 0 or height <= 0 or not widget.get_mapped():
        raise ValueError("Show the workspace before exporting a snapshot")
    native = widget.get_native()
    renderer = native.get_renderer() if native is not None else None
    if renderer is None:
        raise ValueError("The workspace does not have a realized GTK renderer")
    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, float(width), float(height))
    node = snapshot.to_node()
    if node is None:
        raise SnapshotPendingError("The workspace has not produced a completed frame yet")
    texture = renderer.render_texture(node, None)
    data = texture.save_to_png_bytes()
    return bytes(data.get_data())


class _PendingCapture:
    """One main-thread capture request with bounded, disconnected completion."""
    def __init__(self, widget, complete, cancelled, ready):
        import gi
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, GLib
        self._Gdk, self._GLib = Gdk, GLib
        self.pending = True
        self._widget, self._complete = widget, complete
        self._cancelled, self._ready = cancelled, ready
        self._clock, self._clock_handler = None, 0
        self._widget_handlers = []
        self._timeout = 0
        self._retry = 0
        self._ever_mapped = widget.get_mapped()
        self._ever_rooted = widget.get_root() is not None
        self._widget_handlers = [widget.connect("map", self._mapped),
                                 widget.connect("unmap", self._unmapped),
                                 widget.connect("realize", self._mapped),
                                 widget.connect("unrealize", lambda *_: self.cancel()),
                                 widget.connect("notify::root", self._root_changed)]
        self._timeout = GLib.timeout_add(2000, self._expired)
        self._queue_frame()

    def _mapped(self, *_):
        if not self.pending:
            return
        self._ever_mapped = self._ever_mapped or self._widget.get_mapped()
        self._queue_frame()

    def _unmapped(self, *_):
        if self.pending and self._ever_mapped:
            self.cancel()

    def _root_changed(self, *_):
        if not self.pending:
            return
        rooted = self._widget.get_root() is not None
        if self._ever_rooted and not rooted:
            self.cancel()
        else:
            self._ever_rooted = self._ever_rooted or rooted
            self._queue_frame()

    def _queue_frame(self):
        if not self.pending:
            return
        try:
            if self._cancelled():
                self.cancel()
                return
            clock = self._widget.get_frame_clock()
            if clock is not self._clock:
                if self._clock_handler:
                    self._clock.disconnect(self._clock_handler)
                self._clock, self._clock_handler = clock, 0
                if clock is not None:
                    self._clock_handler = clock.connect("after-paint", self._after_paint)
            self._widget.queue_draw()
            if clock is not None:
                clock.request_phase(self._Gdk.FrameClockPhase.AFTER_PAINT)
        except Exception as error:
            self._finish(None, error)

    def _after_paint(self, *_):
        if not self.pending:
            return
        try:
            if self._cancelled():
                self.cancel()
                return
            if (not self._widget.get_mapped() or self._widget.get_width() <= 0 or
                    self._widget.get_height() <= 0 or not self._ready()):
                self._schedule_retry()
                return
            png = snapshot_png(self._widget)
        except SnapshotPendingError:
            self._schedule_retry()
        except Exception as error:
            self._finish(None, error)
        else:
            self._finish(png, None)

    def _schedule_retry(self):
        # A no-vsync frame clock can repaint continuously at a higher priority
        # than idle work. Give the rest of GTK a turn between readiness checks.
        if self.pending and not self._retry:
            self._retry = self._GLib.timeout_add(16, self._retry_frame)

    def _retry_frame(self):
        self._retry = 0
        self._queue_frame()
        return False

    def _expired(self):
        self._timeout = 0
        try:
            error = CancelledError() if self._cancelled() else TimeoutError("The workspace did not finish a snapshot within two seconds")
        except Exception as failure:
            error = failure
        self._finish(None, error)
        return False

    def _finish(self, png, error):
        if not self.pending:
            return
        self.pending = False
        if self._timeout:
            self._GLib.source_remove(self._timeout)
            self._timeout = 0
        if self._retry:
            self._GLib.source_remove(self._retry)
            self._retry = 0
        if self._clock_handler:
            self._clock.disconnect(self._clock_handler)
            self._clock_handler = 0
        self._clock = None
        for handler in self._widget_handlers:
            self._widget.disconnect(handler)
        self._widget_handlers = []
        complete = self._complete
        self._widget = self._complete = self._cancelled = self._ready = None
        complete(png, error)

    def cancel(self):
        """Cancel on GTK's main thread; completion is delivered exactly once."""
        self._finish(None, CancelledError())


def capture_png_when_ready(widget, complete, *, cancelled=lambda: False, ready=lambda: True):
    """Capture after a completed GTK paint, without blocking its event loop.

    Call on the UI thread. complete(bytes, None) or complete(None, error) runs
    there exactly once. The returned handle supports cancel() and pending.
    SnapshotPendingError retries after paint for up to two seconds; other
    errors are reported immediately. Unmapping/unrealizing or cancellation
    disconnects all handlers. Callers can open the native save dialog from
    complete: the PNG has already been captured before that dialog dims it.
    """
    return _PendingCapture(widget, complete, cancelled, ready)


def write_png(target, png: bytes) -> None:
    """Atomically replace a local PNG after flushing a sibling temporary file.

    A failed encode/write leaves the previous target intact. Symbolic-link
    targets are rejected so export cannot unexpectedly follow another file.
    The application separately prevents replacing its source reconstruction.
    """
    png = bytes(png)
    if not png.startswith(PNG_SIGNATURE):
        raise ValueError("Snapshot data is not a PNG image")
    path = Path(target).expanduser()
    if path.is_symlink():
        raise ValueError("Choose a regular file for snapshot export, not a symbolic link")
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(png)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
