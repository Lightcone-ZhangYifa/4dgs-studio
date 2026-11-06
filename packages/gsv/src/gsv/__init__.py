"""Typed scene containers. No executable model deserialization occurs on read."""

from .camera_render import add_render_profile, add_render_view
from .codecs import (
    CODECS,
    add_camera,
    add_camera_affine,
    add_camera_rgb,
    add_persistent_gaussians,
    validate_object,
)
from .container import GSVError, Scene, Writer, open_scene
from .metrics import compare_rgb, convert_rgb

__all__ = [
    "Scene",
    "Writer",
    "GSVError",
    "open_scene",
    "add_persistent_gaussians",
    "add_camera",
    "add_camera_rgb",
    "add_camera_affine",
    "add_render_profile",
    "add_render_view",
    "compare_rgb",
    "convert_rgb",
    "validate_object",
    "CODECS",
]
