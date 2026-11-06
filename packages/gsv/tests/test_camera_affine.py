"""Radiometry is optional camera-bound appearance, never geometry or GT data."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
from gsv import (
    GSVError,
    Scene,
    Writer,
    add_camera,
    add_camera_affine,
    add_persistent_gaussians,
)


class CameraAffineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "scene.gsv"

    def camera(self, out, id="camera", clock="capture"):
        return add_camera(
            out,
            id,
            [0, 1],
            np.tile(np.eye(3), (2, 2, 1, 1)),
            np.tile(np.eye(4), (2, 2, 1, 1)),
            [[20, 30], [40, 50]],
            clock=clock,
        )

    def profile(self, out, **changes):
        kwargs = dict(
            camera_id="camera",
            gain=np.ones((2, 2, 3), np.float32),
            bias=np.zeros((2, 2, 3), np.float32),
            color_space="srgb",
        )
        kwargs.update(changes)
        return add_camera_affine(out, "radiometry", **kwargs)

    def field(self, out, **changes):
        frame = np.zeros((1, 16), np.float32)
        frame[:, 3:7] = 0.1
        frame[:, 7:9] = 1
        frame[:, 15] = -1
        kwargs = dict(clock="capture", metadata={"color_space": "srgb"}, links={})
        kwargs.update(changes)
        return add_persistent_gaussians(
            out, "field", np.array([5], np.int64), [0, 1], [frame, frame], **kwargs
        )

    def test_optional_profile_without_gt_and_lossless_shared_camera_axes(self):
        gain = np.array(
            [[[0.9, 1.2, 1.0], [1, 1, 1]], [[1.1, 1.1, 1.1], [0.8, 0.9, 1.0]]],
            np.float32,
        )
        bias = np.full((2, 2, 3), -0.02, np.float32)
        with Writer(self.path) as out:
            self.camera(out)
            self.profile(out, gain=gain, bias=bias)
            self.field(out)
        with Scene(self.path) as scene:
            self.assertEqual(len(scene.validate()), 3)
            profile = scene.object("radiometry")
            self.assertEqual(profile["type"], "appearance")
            self.assertEqual(profile["links"]["camera"], "camera")
            np.testing.assert_array_equal(scene.array(profile["data"]["gain"]), gain)
            np.testing.assert_array_equal(scene.array(profile["data"]["bias"]), bias)
            self.assertFalse(any(obj["type"] == "image" for obj in scene.objects))

    def test_invalid_profile_shape_values_domain_or_camera_abort(self):
        for change in (
            {"gain": np.ones((2, 3), np.float32)},
            {"bias": np.zeros((2, 1, 3), np.float32)},
            {"gain": np.full((2, 2, 3), -0.01, np.float32)},
            {"bias": np.full((2, 2, 3), np.inf, np.float32)},
            {"gain": np.full((2, 2, 3), np.nan, np.float32)},
            {"color_space": "linear"},
            {"camera_id": "missing"},
        ):
            with self.subTest(change=list(change)), self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    self.camera(out)
                    self.profile(out, **change)
            self.assertFalse(self.path.exists())

    def test_geometry_cannot_own_camera_radiometry(self):
        with self.assertRaisesRegex(GSVError, "render view"):
            with Writer(self.path) as out:
                self.camera(out)
                self.profile(out)
                self.field(out, links={"radiometry": "radiometry"})

    def test_profile_must_share_exact_camera_space_and_clock(self):
        for field in ("clock", "space"):
            with self.subTest(field=field), self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    self.camera(out)
                    profile = self.profile(out)
                    out.manifest[field + "s"]["different"] = dict(
                        out.manifest[field + "s"][profile[field]]
                    )
                    profile[field] = "different"

    def test_low_level_profile_f64_storage_rejected(self):
        with self.assertRaisesRegex(GSVError, "f32"):
            with Writer(self.path) as out:
                self.camera(out)
                profile = self.profile(out)
                profile["data"]["gain"] = out.add_array(
                    "double-gain", np.ones((2, 2, 3), np.float64)
                )


if __name__ == "__main__":
    unittest.main()
