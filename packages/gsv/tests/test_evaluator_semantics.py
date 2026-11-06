"""Adversarial numerical contracts that must not silently change geometry."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
from gsv import GSVError, Scene, Writer, add_camera, add_persistent_gaussians
from gsv.evaluate import evaluate, matrix_quat, quat_matrix


class EvaluatorSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "scene.gsv"

    def write_static(self, *, scale=1.0, position=0.0):
        with Writer(self.path) as out:
            out.manifest["spaces"] = {
                "world": {
                    "unit": "unitless",
                    "handedness": "right",
                    "axes": ["x", "y", "z"],
                }
            }
            arrays = {
                "ids": np.array([0], np.int64),
                "means": np.full((1, 3), position, np.float64),
                "scales": np.full((1, 3), scale, np.float64),
                "quaternions": np.array([[1.0, 0, 0, 0]]),
                "opacity": np.ones(1),
                "colors": np.ones((1, 3)),
            }
            data = {key: out.add_array(key, value) for key, value in arrays.items()}
            data["color_space"] = "srgb"
            out.add_object(
                "field", "gaussian", "gsv.gaussian.static", data, space="world"
            )

    def test_se3_basis_cannot_omit_coordinate_or_time_domain(self):
        for missing in ["space", "clock"]:
            with self.subTest(missing=missing), self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    out.manifest["spaces"] = {
                        "world": {
                            "unit": "unitless",
                            "handedness": "right",
                            "axes": ["x", "y", "z"],
                        }
                    }
                    out.manifest["clocks"] = {"scene": {"unit": "frame"}}
                    data = {
                        key: out.add_array(key, value)
                        for key, value in {
                            "times": np.array([0.0]),
                            "rotations6d": np.array([[[1.0, 0, 0, 0, 1, 0]]]),
                            "translations": np.zeros((1, 1, 3)),
                        }.items()
                    }
                    fields = {"space": "world", "clock": "scene"}
                    fields.pop(missing)
                    out.add_object(
                        "motion", "motion", "gsv.motion.se3-basis", data, **fields
                    )

    def test_f64_scale_underflow_is_not_returned_as_zero_sized_gaussian(self):
        self.write_static(scale=1e-60)
        with Scene(self.path) as scene:
            with self.assertRaisesRegex(GSVError, "Scale"):
                evaluate(scene, "field", 0)

    def test_f64_position_overflow_is_not_returned_as_infinity(self):
        self.write_static(position=1e60)
        with Scene(self.path) as scene:
            with self.assertRaisesRegex(GSVError, "Nonfinite"):
                evaluate(scene, "field", 0)

    def test_nearest_persistent_state_extreme_clock_and_earlier_ties(self):
        frames = np.zeros((3, 1, 16), np.float32)
        frames[:, :, 3:7] = 0.1
        frames[:, :, 7:9] = 1
        frames[:, :, 15] = -1
        frames[:, 0, 0] = [1, 2, 3]
        maximum = np.finfo(np.float64).max
        large = float.fromhex("0x1p+1023")
        with np.errstate(over="raise"), Writer(self.path) as out:
            add_persistent_gaussians(
                out,
                "field",
                np.array([0], np.int64),
                [-large, -large / 2, large],
                frames,
            )
        with Scene(self.path) as scene, np.errstate(over="raise"):
            self.assertEqual(evaluate(scene, "field", maximum)[0, 0], 3)
            self.assertEqual(evaluate(scene, "field", maximum / 2)[0, 0], 3)
            self.assertEqual(evaluate(scene, "field", -large * 0.75)[0, 0], 1)
        # An endpoint query on the opposite end of the finite f64 range made
        # both distances infinite in the previous all-frame subtraction.
        with Writer(self.path, overwrite=True) as out:
            add_persistent_gaussians(
                out,
                "field",
                np.array([0], np.int64),
                [-maximum, -maximum * 0.9],
                frames[:2],
            )
        with Scene(self.path) as scene, np.errstate(over="raise"):
            self.assertEqual(evaluate(scene, "field", maximum)[0, 0], 2)

    def test_batched_matrix_quaternion_roundtrip_at_half_turns(self):
        rng = np.random.default_rng(20261002)
        q = np.concatenate([np.eye(4), rng.normal(size=(96, 4))])
        q /= np.linalg.norm(q, axis=1, keepdims=True)
        matrix = quat_matrix(q).reshape(5, 20, 3, 3)
        np.testing.assert_allclose(quat_matrix(matrix_quat(matrix)), matrix, atol=2e-15)
        np.testing.assert_allclose(
            quat_matrix(matrix_quat(matrix[0, 0])), matrix[0, 0], atol=2e-15
        )
        self.assertEqual(matrix_quat(np.empty((0, 3, 3))).shape, (0, 4))

    def test_camera_homogeneous_row_is_affine_not_approximately_projective(self):
        pose = np.eye(4)[None, None]
        pose[0, 0, 3, 3] = 1.000001
        with self.assertRaisesRegex(GSVError, "homogeneous"):
            with Writer(self.path) as out:
                add_camera(out, "camera", [0], np.eye(3)[None, None], pose, [[10, 10]])

    def test_different_motion_space_cannot_be_silently_applied(self):
        # Both clocks agree, but a translation in centimeters is not a meter.
        with self.assertRaisesRegex(GSVError, "coordinate space"):
            with Writer(self.path) as out:
                out.manifest["spaces"] = {
                    key: {"unit": unit, "handedness": "right", "axes": ["x", "y", "z"]}
                    for key, unit in [("world", "meter"), ("control", "centimeter")]
                }
                out.manifest["clocks"] = {"frame": {"unit": "frame"}}
                basis = {
                    key: out.add_array("basis/" + key, value)
                    for key, value in {
                        "times": np.array([0.0], np.float64),
                        "rotations6d": np.array([[[1.0, 0, 0, 0, 1, 0]]]),
                        "translations": np.array([[[100.0, 0, 0]]]),
                    }.items()
                }
                out.add_object(
                    "motion",
                    "motion",
                    "gsv.motion.se3-basis",
                    basis,
                    space="control",
                    clock="frame",
                )
                arrays = {
                    "ids": np.array([0], np.int64),
                    "canonical_means": np.zeros((1, 3)),
                    "scales": np.ones((1, 3)),
                    "quaternions": np.array([[1.0, 0, 0, 0]]),
                    "opacity": np.ones(1),
                    "colors": np.ones((1, 3)),
                    "coefficients": np.ones((1, 1)),
                }
                data = {
                    key: out.add_array("field/" + key, value)
                    for key, value in arrays.items()
                }
                data["color_space"] = "srgb"
                out.add_object(
                    "field",
                    "gaussian",
                    "gsv.gaussian.se3-basis",
                    data,
                    links={"motion": "motion"},
                    space="world",
                    clock="frame",
                )


if __name__ == "__main__":
    unittest.main()
