"""Offline PNG export from completed viewport pixels or native GTK content."""
from __future__ import annotations

import os
from pathlib import Path
import struct
import tempfile
import zlib

import numpy as np


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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
        raise ValueError("The workspace has not produced a completed frame yet")
    texture = renderer.render_texture(node, None)
    data = texture.save_to_png_bytes()
    return bytes(data.get_data())


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
