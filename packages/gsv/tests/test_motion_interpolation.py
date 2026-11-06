"""Public evaluator checks for finite clocks across the f64 range."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
from gsv import Scene, Writer
from gsv.evaluate import evaluate, quat_matrix


class MotionInterpolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "motion.gsv"

    def write_motion(self, times, *, rotations=None, translations=None, mapping=None):
        times = np.asarray(times, dtype=np.float64)
        if rotations is None:
            rotations = np.tile([1.0, 0, 0, 0, 1, 0], (1, len(times), 1))
        if translations is None:
            translations = np.zeros((1, len(times), 3))
            translations[0, :, 0] = np.linspace(0, 2, len(times))
        with Writer(self.path, overwrite=True) as out:
            out.manifest["spaces"] = {
                "world": {
                    "unit": "meter",
                    "handedness": "right",
                    "axes": ["x", "y", "z"],
                }
            }
            out.manifest["clocks"] = {"basis": {"unit": "second"}}
            field_clock = "basis"
            if mapping is not None:
                field_clock = "field"
                out.manifest["clocks"][field_clock] = {
                    "unit": "normalized",
                    "mapping": mapping,
                }
            basis = {
                key: out.add_array("basis/" + key, value)
                for key, value in {
                    "times": times,
                    "rotations6d": rotations,
                    "translations": translations,
                }.items()
            }
            out.add_object(
                "motion",
                "motion",
                "gsv.motion.se3-basis",
                basis,
                space="world",
                clock="basis",
            )
            arrays = {
                "ids": np.array([7], np.int64),
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
                clock=field_clock,
            )

    def test_wide_finite_clock_interpolates_and_clamps_without_overflow(self):
        maximum = np.finfo(np.float64).max
        cases = [
            (
                [-1e308, 1e308],
                [-maximum, -1e308, -5e307, 0.0, 5e307, 1e308, maximum],
                [0.0, 0.0, 0.5, 1.0, 1.5, 2.0, 2.0],
            ),
            (
                [-maximum, maximum],
                [-maximum, -maximum / 2, 0.0, maximum / 2, maximum],
                [0.0, 0.5, 1.0, 1.5, 2.0],
            ),
            ([-1.6e308, 8e307], [-4e307, 0.0], [1.0, 4 / 3]),
            ([-maximum, -maximum * 0.9], [maximum], [2.0]),
        ]
        for times, queries, expected in cases:
            with (
                self.subTest(times=times),
                np.errstate(over="raise", invalid="raise", divide="raise"),
            ):
                self.write_motion(times)
                with Scene(self.path) as scene:
                    self.assertTrue(
                        all(row["status"] == "verified" for row in scene.validate())
                    )
                    actual = [evaluate(scene, "field", time)[0, 0] for time in queries]
                np.testing.assert_allclose(actual, expected, rtol=1e-7, atol=0)

    def test_small_and_large_origin_intervals_keep_their_resolvable_midpoint(self):
        smallest = float.fromhex("0x0.0000000000001p-1022")
        large = 1e308
        next_large = np.nextafter(large, np.inf)
        cases = [
            ([0.0, 2 * smallest], smallest),
            ([0.0, 2e-300], 1e-300),
            ([large, np.nextafter(next_large, np.inf)], next_large),
        ]
        for times, query in cases:
            with self.subTest(times=times), np.errstate(all="raise"):
                self.write_motion(times)
                with Scene(self.path) as scene:
                    self.assertEqual(evaluate(scene, "field", query)[0, 0], 1.0)

    def test_rotation_parameters_use_the_same_wide_clock_weight(self):
        rotations = np.array([[[1.0, 0, 0, 0, 1, 0], [0.0, 1, 0, -1, 0, 0]]])
        self.write_motion([-1e308, 1e308], rotations=rotations)
        with Scene(self.path) as scene, np.errstate(over="raise", invalid="raise"):
            frame = evaluate(scene, "field", 0.0)
        c = np.sqrt(0.5)
        np.testing.assert_allclose(
            quat_matrix(frame[:, 8:12]), [[[c, -c, 0], [c, c, 0], [0, 0, 1]]], atol=2e-7
        )
        self.assertEqual(frame[0, 0], 1.0)

    def test_interpolation_uses_declared_basis_clock_after_conversion(self):
        self.write_motion(
            [-1e308, 1e308], mapping={"target": "basis", "scale": 1e308, "offset": 0.0}
        )
        with Scene(self.path) as scene, np.errstate(over="raise", invalid="raise"):
            self.assertEqual(evaluate(scene, "field", 0.5)[0, 0], 1.5)

    def test_f32_parameters_keep_f64_clock_precision_during_interpolation(self):
        # Rounding the weight to f32 first would cancel these translations to
        # zero, although the f64 query has a resolvable two-meter displacement.
        self.write_motion(
            [0.0, 1.0], translations=np.array([[[1e8, 0, 0], [-1e8, 0, 0]]], np.float32)
        )
        with Scene(self.path) as scene:
            self.assertEqual(evaluate(scene, "field", 0.50000001)[0, 0], -2.0)

    def test_single_sample_is_constant_at_every_finite_query(self):
        self.write_motion([1e308], translations=np.array([[[2.0, 0.0, 0.0]]]))
        maximum = np.finfo(np.float64).max
        with Scene(self.path) as scene, np.errstate(all="raise"):
            for time in [-maximum, 0.0, 1e308, maximum]:
                self.assertEqual(evaluate(scene, "field", time)[0, 0], 2.0)


if __name__ == "__main__":
    unittest.main()
