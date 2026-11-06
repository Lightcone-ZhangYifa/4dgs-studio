"""Camera correspondence must be explicit, exact and independent of geometry."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from gsv import (
    GSVError,
    Scene,
    Writer,
    add_camera,
    add_camera_rgb,
    add_persistent_gaussians,
)
from gsv.container import HEADER, MAGIC


class CameraRGBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "observations.gsv"

    def camera(self, out):
        out.manifest["clocks"]["capture"] = {"unit": "frame"}
        K = np.broadcast_to(np.eye(3), (3, 2, 3, 3)).copy()
        K[2, 1, 0, 0] = 3.0
        poses = np.broadcast_to(np.eye(4), (3, 2, 4, 4)).copy()
        poses[2, 1, 0, 3] = 2.0
        return add_camera(
            out,
            "camera",
            [0, 10, 20],
            K,
            poses,
            [[14, 12], [16, 13]],
            clock="capture",
            pixel_center="half",
        )

    def observations(self, out, **changes):
        kwargs = dict(
            camera_id="camera",
            camera_samples=np.array([0, 2], np.int64),
            view_indices=np.array([0, 1], np.int32),
            images=[np.zeros((12, 14, 3), np.uint8), np.ones((13, 16, 3), np.float32)],
            masks=[np.ones((12, 14), bool), np.ones((13, 16), bool)],
            color_space="srgb",
        )
        kwargs.update(changes)
        return add_camera_rgb(out, "observations", **kwargs)

    def test_camera_only_and_optional_gt_and_empty_remain_valid(self):
        with Writer(self.path) as out:
            self.camera(out)
        with Scene(self.path) as scene:
            self.assertEqual(len(scene.validate()), 1)
        with Writer(self.path, overwrite=True):
            pass
        with Scene(self.path) as scene:
            self.assertEqual(scene.validate(), [])
        with Writer(self.path, overwrite=True) as out:
            add_persistent_gaussians(out, "field", np.empty(0, np.int64), [], [])
        with Scene(self.path) as scene:
            self.assertEqual(len(scene.validate()), 1)

    def test_independent_observation_times_views_sizes_streaming_roundtrip(self):
        with Writer(self.path) as out:
            self.camera(out)
            obj = self.observations(
                out,
                images=iter(
                    [np.zeros((12, 14, 3), np.uint8), np.ones((13, 16, 3), np.float32)]
                ),
            )
            self.assertEqual(obj["clock"], "capture")
        with Scene(self.path) as scene:
            self.assertTrue(all(x["status"] == "verified" for x in scene.validate()))
            obj = scene.object("observations")
            self.assertEqual(obj["links"]["camera"], "camera")
            self.assertEqual(obj["data"]["role"], "ground_truth")
            self.assertEqual(
                scene.array(obj["data"]["pixels"]["images"][1]).shape, (13, 16, 3)
            )
            self.assertFalse(
                scene.array(obj["data"]["pixels"]["images"][0]).flags.writeable
            )
            copy_path = self.path.with_name("copy.gsv")
            scene.copy(copy_path)
        with Scene(copy_path) as copied:
            self.assertEqual(len(copied.validate()), 2)

    def test_empty_observation_collection_allowed(self):
        with Writer(self.path) as out:
            self.camera(out)
            self.observations(
                out,
                camera_samples=np.empty(0, np.int64),
                view_indices=np.empty(0, np.int32),
                images=[],
                masks=[],
            )
        with Scene(self.path) as scene:
            self.assertEqual(len(scene.validate()), 2)

    def test_exact_integer_lists_and_unsigned_arrays_convert_without_rounding(self):
        with Writer(self.path) as out:
            self.camera(out)
            self.observations(out, camera_samples=[0, 2], view_indices=[0, 1])
        with Scene(self.path) as scene:
            data = scene.object("observations")["data"]
            self.assertEqual(
                scene.array(data["samples"]["camera_samples"]).dtype, np.int64
            )
            self.assertEqual(
                scene.array(data["samples"]["view_indices"]).dtype, np.int32
            )
        self.path.unlink()
        for indices in ([True, False], np.array([0, 2**63], np.uint64)):
            with self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    self.camera(out)
                    self.observations(out, camera_samples=indices)

    def test_boolean_payload_is_canonical_in_writes_and_strict_on_reads(self):
        noncanonical = np.array([0, 255], np.uint8).view(np.bool_)
        with Writer(self.path) as out:
            out.add_array("mask", noncanonical)
        with Scene(self.path) as scene:
            np.testing.assert_array_equal(scene.array("mask").view(np.uint8), [0, 1])
        with Writer(self.path, overwrite=True, validate=False) as out:
            out._buffer("invalid", [b"\x02"])
            out.manifest["arrays"]["invalid"] = {
                "buffer": "invalid",
                "dtype": "bool",
                "shape": [1],
            }
        with Scene(self.path, verify=False) as scene:
            with self.assertRaisesRegex(GSVError, "canonical"):
                scene.array("invalid")
            with self.assertRaisesRegex(GSVError, "canonical"):
                scene.validate(semantic=False)

    def test_bad_shapes_domains_masks_and_correspondence_fail_atomically(self):
        cases = [
            {"camera_samples": np.array([0.0, 2.0])},
            {"camera_samples": np.array([0, 3], np.int64)},
            {"view_indices": np.array([0, 2], np.int32)},
            {"view_indices": np.array([0, 2**32], np.int64)},
            {"view_indices": np.array([False, True], bool)},
            {
                "camera_samples": np.array([0, 0], np.int64),
                "view_indices": np.array([0, 0], np.int32),
            },
            {"images": [np.zeros((12, 14, 3), np.uint8)]},
            {"images": [np.zeros((12, 14, 3), np.uint8)] * 3},
            {
                "images": [
                    np.zeros((12, 14, 3), np.uint8),
                    np.zeros((12, 16, 3), np.uint8),
                ]
            },
            {
                "images": [
                    np.zeros((12, 14, 3), np.uint8),
                    np.full((13, 16, 3), np.nan, np.float32),
                ]
            },
            {
                "images": [
                    np.zeros((12, 14, 3), np.uint8),
                    np.full((13, 16, 3), 1.1, np.float32),
                ]
            },
            {
                "images": [
                    np.zeros((12, 14, 3), np.uint8),
                    np.zeros((13, 16, 3), np.float64),
                ]
            },
            {"masks": [np.ones((12, 14), np.uint8), np.ones((13, 16), bool)]},
            {"masks": []},
            {"masks": [np.ones((12, 14), bool)] * 3},
            {"role": "assumed_gt"},
            {"color_space": "linear"},
            {"camera_id": "missing"},
        ]
        for changes in cases:
            with self.subTest(changes=list(changes)), self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    self.camera(out)
                    self.observations(out, **changes)
            self.assertFalse(self.path.exists())

    def test_low_level_writer_cannot_bypass_clock_space_and_shape_checks(self):
        for mutation in ("clock", "space", "images", "mask", "camera"):
            with self.subTest(mutation=mutation), self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    self.camera(out)
                    obj = self.observations(out)
                    if mutation == "clock":
                        out.manifest["clocks"]["unrelated"] = {"unit": "frame"}
                        obj["clock"] = "unrelated"
                    elif mutation == "space":
                        out.manifest["spaces"]["unrelated"] = out.manifest["spaces"][
                            "world"
                        ].copy()
                        obj["space"] = "unrelated"
                    elif mutation == "images":
                        obj["data"]["pixels"]["images"].pop()
                    elif mutation == "mask":
                        obj["data"]["pixels"]["masks"][0] = obj["data"]["pixels"][
                            "images"
                        ][0]
                    else:
                        obj["links"]["camera"] = "observations"

    def test_unregistered_core_codec_rejected_even_without_semantic_validation(self):
        with self.assertRaises(GSVError):
            with Writer(self.path, validate=False) as out:
                out.add_object(
                    "extension", "gaussian", "gsv.gaussian.not-registered", {}
                )
        manifest = {
            "format": "gsv.scene",
            "objects": [
                {
                    "id": "x",
                    "type": "gaussian",
                    "encoding": "gsv.gaussian.not-registered",
                    "data": {},
                }
            ],
            "arrays": {},
            "buffers": {},
        }
        raw = json.dumps(manifest).encode()
        self.path.write_bytes(
            HEADER.pack(
                MAGIC, 64 + len(raw), 64, len(raw), hashlib.sha256(raw).digest()
            )
            + raw
        )
        with self.assertRaisesRegex(GSVError, "Unregistered core"):
            Scene(self.path, verify=False)

    def test_recipe_and_provenance_contracts(self):
        bad_recipes = [
            {},
            {"source_commit": "abc", "architecture": "MLP", "entrypoint": "module:run"},
            {
                "source_commit": "",
                "architecture": "MLP",
                "entrypoint": "module:run",
                "configuration": {},
            },
        ]
        for recipe in bad_recipes:
            with self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    out.add_object(
                        "motion",
                        "motion",
                        "gsv.motion.neural",
                        {"tensors": {}, "recipe": recipe},
                    )
        with Writer(self.path) as out:
            out.add_object(
                "motion",
                "motion",
                "gsv.motion.neural",
                {
                    "tensors": {},
                    "recipe": {
                        "source_commit": "snapshot:abc",
                        "architecture": "MLP",
                        "entrypoint": "module:run",
                        "configuration": {},
                    },
                },
            )
            out.add_object(
                "evidence",
                "provenance",
                "gsv.provenance.record",
                {
                    "method": "fixture",
                    "sources": [{"path": "input.png", "sha256": "a" * 64}],
                },
            )
        with Scene(self.path) as scene:
            self.assertEqual(len(scene.validate()), 2)
        for evidence in (
            {},
            {"method": "fixture", "sources": [{}]},
            {"method": "fixture", "sources": [{"uri": "x", "sha256": "bad"}]},
        ):
            with self.assertRaises(GSVError):
                with Writer(self.path, overwrite=True) as out:
                    out.add_object(
                        "evidence", "provenance", "gsv.provenance.record", evidence
                    )


if __name__ == "__main__":
    unittest.main()
