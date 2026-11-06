import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from gsv import GSVError, Scene, Writer, add_camera, add_persistent_gaussians
from gsv.container import HEADER
from gsv.evaluate import evaluate, matrix_quat, quat_matrix


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "test.gsv"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty(self):
        with Writer(self.p):
            pass
        with Scene(self.p) as s:
            self.assertEqual(s.validate(), [])

    def test_unknown_roundtrip_and_exact_large_ids(self):
        with Writer(self.p) as w:
            a = w.add_array("ids", np.array([2**60 + 3], dtype="int64"))
            w.add_object(
                "future",
                "motion",
                "org.example.new-motion",
                {"values": a},
                custom={"retained": True},
            )
        q = self.p.with_name("copy.gsv")
        with Scene(self.p) as s:
            self.assertEqual(s.array("ids")[0], 2**60 + 3)
            self.assertEqual(s.validate()[0]["status"], "preserved_unknown_encoding")
            s.copy(q)
        with Scene(q) as s:
            self.assertTrue(s.object("future")["custom"]["retained"])

    def test_samples_and_camera_independent_clock(self):
        f = np.zeros((2, 16), dtype="float32")
        f[:, 3:7] = 0.1
        f[:, 7:9] = 1
        f[:, 12:15] = 0.5
        f[:, 15] = -1
        with Writer(self.p) as w:
            add_persistent_gaussians(
                w, "geometry", np.array([3, 8], dtype="int64"), [0.0, 2.0], iter([f, f])
            )
            add_camera(
                w,
                "camera",
                [0.0, 1.0, 3.0],
                np.tile(np.eye(3), (3, 1, 1, 1)),
                np.tile(np.eye(4), (3, 1, 1, 1)),
                [[640, 480]],
            )
        with Scene(self.p) as s:
            self.assertEqual(len(s.validate()), 2)
            np.testing.assert_array_equal(evaluate(s, "geometry", 1), f)

    def test_payload_corruption(self):
        with Writer(self.p) as w:
            w.add_array("a", np.arange(5, dtype="float32"))
        with self.p.open("r+b") as f:
            f.seek(16384)
            f.write(b"\xff")
        with Scene(self.p) as s:
            with self.assertRaises(GSVError):
                s.array("a")

    def test_manifest_checksum_and_truncation(self):
        with Writer(self.p):
            pass
        raw = self.p.read_bytes()
        self.p.write_bytes(raw[:-1])
        with self.assertRaises(GSVError):
            Scene(self.p)

    def test_atomic_failure_preserves_destination(self):
        with Writer(self.p):
            pass
        before = self.p.read_bytes()
        with self.assertRaises(RuntimeError):
            with Writer(self.p, overwrite=True) as w:
                w.add_array("x", np.arange(2))
                raise RuntimeError("interrupted")
        self.assertEqual(self.p.read_bytes(), before)

    def test_closed_writer_rejects_unsaved_mutations(self):
        with Writer(self.p) as writer:
            pass
        before = self.p.read_bytes()
        for operation in (
            lambda: writer.add_array("late", np.array([1])),
            lambda: writer.add_object("late", "tensor", "org.example.tensor", {}),
        ):
            with self.assertRaisesRegex(GSVError, "closed"):
                operation()
        self.assertEqual(self.p.read_bytes(), before)
        self.assertEqual(writer.manifest["objects"], [])

    def test_boolean_aliases_checked_even_without_object_references(self):
        def write(validate):
            with Writer(self.p, validate=validate) as writer:
                writer.add_array("bytes", np.array([0, 2, 1], np.uint8))
                writer.manifest["arrays"]["mask"] = {
                    "buffer": "bytes",
                    "dtype": "bool",
                    "shape": [1],
                    "byte_offset": 1,
                }

        with self.assertRaisesRegex(GSVError, "canonical"):
            write(True)
        self.assertFalse(self.p.exists())
        write(False)
        for verify in (True, False):
            with Scene(self.p, verify=verify) as scene:
                with self.assertRaisesRegex(GSVError, "canonical"):
                    scene.array("mask")
        with Scene(self.p) as scene:
            with self.assertRaisesRegex(GSVError, "canonical"):
                scene.validate(semantic=False)

    def test_boolean_values_write_canonical_bytes(self):
        source_bytes = np.tile(np.array([0, 1, 2, 255], dtype=np.uint8), 64)
        with Writer(self.p) as writer:
            writer.add_array("mask", source_bytes.view(np.bool_))
        with Scene(self.p) as scene:
            mask_bytes = scene.array("mask").view(np.uint8)
            np.testing.assert_array_equal(mask_bytes, np.tile([0, 1, 1, 1], 64))

    def test_boolean_alias_and_numeric_views_roundtrip(self):
        with Writer(self.p) as writer:
            writer.add_array("bytes", np.array([7, 0, 1, 1], np.uint8))
            writer.manifest["arrays"]["mask"] = {
                "buffer": "bytes",
                "dtype": "bool",
                "shape": [3],
                "byte_offset": 1,
            }
        with Scene(self.p) as scene:
            np.testing.assert_array_equal(scene.array("mask"), [False, True, True])
            np.testing.assert_array_equal(scene.array("bytes"), [7, 0, 1, 1])
            scene.copy(self.p.with_name("copy.gsv"))
        with Scene(self.p.with_name("copy.gsv")) as scene:
            np.testing.assert_array_equal(scene.array("mask"), [False, True, True])

    def test_negative_extents_rejected(self):
        with self.assertRaises(GSVError):
            with Writer(self.p) as w:
                w.add_array("x", np.arange(5))
                w.manifest["arrays"]["x"]["byte_offset"] = -1
        self.assertFalse(self.p.exists())

    def test_overlapping_payload_rejected(self):
        with self.assertRaises(GSVError):
            with Writer(self.p) as w:
                w.add_array("a", np.arange(2))
                w.add_array("b", np.arange(2))
                w.manifest["buffers"]["b"]["offset"] = w.manifest["buffers"]["a"][
                    "offset"
                ]

    def test_pickle_and_bad_camera_rejected(self):
        with self.assertRaises(GSVError):
            with Writer(self.p) as w:
                w.add_array("unsafe", np.array([{}], dtype=object))
        with self.assertRaises(GSVError):
            with Writer(self.p) as w:
                add_camera(
                    w,
                    "bad",
                    [0],
                    np.eye(3)[None, None],
                    np.zeros((1, 1, 4, 4)),
                    [[10, 10]],
                )

    def test_file_modified_after_open(self):
        with Writer(self.p) as w:
            w.add_array("x", np.arange(3))
        with Scene(self.p) as s:
            with self.p.open("ab") as f:
                f.write(b"x")
            with self.assertRaises(GSVError):
                s.array("x")

    def test_source_changes_between_copy_verification_and_read(self):
        with Writer(self.p) as w:
            w.add_array("x", np.array([1], dtype="int64"))
        output = self.p.with_name("copy.gsv")
        original = Writer._buffer

        def mutate_then_copy(writer, key, chunks, **kwargs):
            with self.p.open("r+b") as f:
                f.seek(16384)
                f.write(struct.pack("<q", 2))
            original(writer, key, chunks, **kwargs)

        with Scene(self.p) as s, patch.object(Writer, "_buffer", mutate_then_copy):
            with self.assertRaises(GSVError):
                s.copy(output)
        self.assertFalse(output.exists())

    def test_scalar_and_empty_arrays(self):
        with Writer(self.p) as w:
            w.add_array("scalar", np.array(3.5))
            w.add_array("empty", np.empty((0, 3), dtype="float32"))
        with Scene(self.p) as s:
            self.assertEqual(s.array("scalar").shape, ())
            self.assertEqual(s.array("empty").shape, (0, 3))
            self.assertFalse(s.array("empty").flags.writeable)

    def test_clock_mapping_chain_inverse_and_unrelated(self):
        with Writer(self.p) as w:
            w.manifest["clocks"] = {
                "seconds": {"unit": "second"},
                "frames": {
                    "unit": "frame",
                    "mapping": {"target": "seconds", "scale": 0.04, "offset": 2},
                },
                "normalized": {
                    "unit": "normalized",
                    "mapping": {"target": "frames", "scale": 100, "offset": 10},
                },
                "other": {"unit": "second"},
            }
        with Scene(self.p) as s:
            self.assertAlmostEqual(s.convert_time(0.5, "normalized", "seconds"), 4.4)
            self.assertAlmostEqual(s.convert_time(4.4, "seconds", "normalized"), 0.5)
            with self.assertRaises(GSVError):
                s.convert_time(0, "seconds", "other")

    def test_clock_shared_large_origin_does_not_erase_local_time(self):
        with Writer(self.p) as w:
            w.manifest["clocks"] = {
                "root": {"unit": "second"},
                "a": {
                    "unit": "second",
                    "mapping": {"target": "root", "scale": 1, "offset": 1e16},
                },
                "b": {
                    "unit": "second",
                    "mapping": {"target": "root", "scale": 2, "offset": 1e16},
                },
            }
        with Scene(self.p) as s:
            self.assertEqual(s.convert_time(1, "a", "a"), 1)
            self.assertEqual(s.convert_time(1, "a", "b"), 0.5)

    def test_cycles_and_boolean_scales_rejected(self):
        for mapping in (
            {"target": "a", "scale": 1, "offset": 0},
            {"target": "b", "scale": True, "offset": 0},
        ):
            with self.assertRaises(GSVError):
                with Writer(self.p) as w:
                    w.manifest["clocks"] = {
                        "a": {"unit": "frame", "mapping": mapping},
                        "b": {"unit": "second"},
                    }

    def test_json_overflow_and_duplicate_keys_rejected(self):
        for raw in (
            b'{"format":"gsv.scene","objects":[],"buffers":{},"arrays":{},"metadata":{"x":1e999}}',
            b'{"format":"gsv.scene","objects":[],"buffers":{},"arrays":{},"arrays":{}}',
        ):
            self.p.write_bytes(
                HEADER.pack(
                    b"GSV\x00\r\n\x1a\n",
                    64 + len(raw),
                    64,
                    len(raw),
                    hashlib.sha256(raw).digest(),
                )
                + raw
            )
            with self.assertRaises(GSVError):
                Scene(self.p)

    def test_manifest_requires_utf8(self):
        data = json.dumps(
            {"format": "gsv.scene", "objects": [], "arrays": {}, "buffers": {}}
        )
        for encoding in ["utf-16", "utf-32"]:
            with self.subTest(encoding=encoding):
                raw = data.encode(encoding)
                header = HEADER.pack(
                    b"GSV\x00\r\n\x1a\n",
                    64 + len(raw),
                    64,
                    len(raw),
                    hashlib.sha256(raw).digest(),
                )
                self.p.write_bytes(header + raw)
                with self.assertRaises(GSVError):
                    Scene(self.p)

    def test_invalid_json_cannot_replace_destination(self):
        with Writer(self.p):
            pass
        previous = self.p.read_bytes()
        cyclic = {}
        cyclic["self"] = cyclic
        for metadata in [cyclic, {1: "one", "1": "also one"}]:
            with self.assertRaises(GSVError):
                with Writer(self.p, overwrite=True, validate=False) as out:
                    out.manifest["metadata"] = metadata
            self.assertEqual(self.p.read_bytes(), previous)
            self.assertEqual(list(self.p.parent.glob("*.partial")), [])

    def test_nonzero_padding_rejected(self):
        with Writer(self.p) as w:
            w.add_array("x", np.array([1], dtype="uint8"))
        with self.p.open("r+b") as f:
            f.seek(16385)
            f.write(b"\x01")
        with Scene(self.p) as s:
            with self.assertRaises(GSVError):
                s.validate()

    def test_semantic_type_mismatch_rejected(self):
        with self.assertRaises(GSVError):
            with Writer(self.p) as w:
                w.add_object("wrong", "camera", "gsv.provenance.record", {})

    def test_binding_rows_are_not_numeric_identity_values(self):
        def write(index):
            with Writer(self.p) as w:
                ids = w.add_array("ids", np.array([2**60 + 1], dtype="int64"))
                positions = w.add_array("positions", np.zeros((1, 3), dtype="float32"))
                for name in ("source", "target"):
                    w.add_object(
                        name,
                        "pointcloud",
                        "gsv.geometry.points",
                        {"ids": ids, "positions": positions},
                    )
                w.add_object(
                    "weights",
                    "binding",
                    "gsv.binding.weights",
                    {
                        "source_ids": ids,
                        "target_rows": w.add_array(
                            "rows", np.array([[index]], dtype="int64")
                        ),
                        "weights": w.add_array(
                            "weights", np.ones((1, 1), dtype="float32")
                        ),
                        "normalization": "partition-of-unity",
                    },
                    links={"source": "source", "target": "target"},
                )

        with self.assertRaises(GSVError):
            write(2**60 + 1)
        write(0)
        with Scene(self.p) as s:
            self.assertEqual(len(s.validate()), 3)

    def test_false_scene_bounds_rejected(self):
        frame = np.zeros((1, 16), dtype="float32")
        frame[0, 0] = 100
        frame[:, 3:7] = 0.1
        frame[:, 7:9] = 1
        frame[:, 15] = -1
        with self.assertRaises(GSVError):
            with Writer(self.p) as w:
                obj = add_persistent_gaussians(
                    w, "geometry", np.array([1], dtype="int64"), [0], [frame]
                )
                obj["data"]["bounds"] = [[-1, -1, -1], [1, 1, 1]]


class EvaluatorTests(unittest.TestCase):
    def test_half_turn_quaternion(self):
        q = np.array([[0, 1, 0, 0], [0.5, 0.5, 0.5, 0.5]])
        np.testing.assert_allclose(
            quat_matrix(matrix_quat(quat_matrix(q))), quat_matrix(q), atol=1e-12
        )

    def test_joint_gaussian_conditional(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.gsv"
            cov = np.eye(4)[None]
            cov[0, 0, 3] = cov[0, 3, 0] = 0.5
            with Writer(p) as w:
                w.manifest["spaces"] = {
                    "world": {
                        "unit": "unitless",
                        "handedness": "right",
                        "axes": ["x", "y", "z"],
                    }
                }
                w.manifest["clocks"] = {"scene": {"unit": "second"}}
                d = {
                    k: w.add_array(k, v)
                    for k, v in {
                        "ids": np.array([1], dtype="int64"),
                        "means": np.zeros((1, 4)),
                        "covariance": cov,
                        "opacity": np.array([0.8]),
                        "colors": np.ones((1, 3)) * 0.5,
                    }.items()
                }
                d["color_space"] = "srgb"
                w.add_object(
                    "g",
                    "gaussian",
                    "gsv.gaussian.covariance4d",
                    d,
                    space="world",
                    clock="scene",
                )
            with Scene(p) as s:
                f = evaluate(s, "g", 2)
                np.testing.assert_allclose(f[0, :3], [1, 0, 0])
                self.assertAlmostEqual(f[0, 3], 0.8 * np.exp(-2), places=6)
                R = quat_matrix(f[:, 8:12])
                c = R @ np.eye(3)[None] * 0
                c = R @ np.array([np.diag(f[0, 4:7] ** 2)]) @ R.swapaxes(-1, -2)
                np.testing.assert_allclose(c[0], np.diag([0.75, 1, 1]), atol=1e-6)


if __name__ == "__main__":
    unittest.main()
