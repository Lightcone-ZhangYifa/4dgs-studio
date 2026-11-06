"""Analytical, independent direct-window and cross-language metric contracts."""

import json
import math
import unittest
from pathlib import Path

import numpy as np
from gsv import GSVError, compare_rgb, convert_rgb


class RGBMetricTests(unittest.TestCase):
    def test_constant_analytical_and_exact_match(self):
        black = np.zeros((13, 14, 3), np.float64)
        result = compare_rgb(black + 0.25, black, color_space="srgb")
        self.assertEqual(result["mse"], 0.0625)
        self.assertEqual(result["mae"], 0.25)
        self.assertAlmostEqual(result["psnr"], 12.041199826559248, places=12)
        self.assertAlmostEqual(result["ssim"], 0.0001 / (0.0625 + 0.0001), places=12)
        self.assertEqual(result["ssim_windows"], 12)
        exact = compare_rgb(black, black, color_space="linear-srgb")
        self.assertEqual(exact["ssim"], 1)
        self.assertEqual(exact["psnr"], math.inf)

    def test_ssim_matches_independent_direct_2d_windows(self):
        rng = np.random.default_rng(7)
        a = rng.random((13, 14, 3))
        b = rng.random((13, 14, 3))
        coordinate = np.arange(-5, 6)
        kernel = np.exp(-(coordinate[:, None] ** 2 + coordinate[None, :] ** 2) / 4.5)
        kernel /= kernel.sum()
        scores = []
        for y in range(3):
            for x in range(4):
                p = a[y : y + 11, x : x + 11]
                r = b[y : y + 11, x : x + 11]
                mp = np.sum(p * kernel[:, :, None], axis=(0, 1))
                mr = np.sum(r * kernel[:, :, None], axis=(0, 1))
                vp = np.sum((p - mp) ** 2 * kernel[:, :, None], axis=(0, 1))
                vr = np.sum((r - mr) ** 2 * kernel[:, :, None], axis=(0, 1))
                cov = np.sum((p - mp) * (r - mr) * kernel[:, :, None], axis=(0, 1))
                scores.append(
                    ((2 * mp * mr + 0.0001) * (2 * cov + 0.0009))
                    / ((mp**2 + mr**2 + 0.0001) * (vp + vr + 0.0009))
                )
        self.assertAlmostEqual(
            compare_rgb(a, b, color_space="srgb")["ssim"], np.mean(scores), places=12
        )

    def test_mask_full_support_and_empty_pixels_are_not_perfect(self):
        a = np.zeros((13, 14, 3), np.float32)
        b = a.copy()
        b[0] = 1
        mask = np.ones((13, 14), bool)
        mask[0] = False
        result = compare_rgb(a, b, color_space="srgb", mask=mask)
        self.assertEqual(result["mse"], 0)
        self.assertEqual(result["ssim"], 1)
        self.assertEqual(result["ssim_windows"], 8)
        mask[6, 6] = False
        result = compare_rgb(a, b, color_space="srgb", mask=mask)
        self.assertEqual(result["ssim_status"], "no_full_valid_window")
        self.assertIsNone(result["ssim"])
        result = compare_rgb(a, b, color_space="srgb", mask=np.zeros((13, 14), bool))
        self.assertEqual(result["status"], "no_valid_pixels")
        self.assertIsNone(result["psnr"])

    def test_uint8_normalization_and_small_image(self):
        a = np.zeros((4, 5, 3), np.uint8)
        b = np.full_like(a, 255)
        result = compare_rgb(a, b, color_space="srgb")
        self.assertEqual(result["mse"], 1)
        self.assertEqual(result["psnr"], 0)
        self.assertEqual(result["ssim_status"], "image_smaller_than_window")

    def test_no_guessing_clipping_resizing_or_mask_casting(self):
        a = np.zeros((13, 14, 3), np.float32)
        for b, extra in [
            (a, {"color_space": "linear"}),
            (a[:12], {}),
            (a + 2, {}),
            (a + np.nan, {}),
            (a.astype(np.int32), {}),
            (a, {"mask": np.ones((13, 14), np.uint8)}),
        ]:
            with self.assertRaises(GSVError):
                compare_rgb(a, b, **{"color_space": "srgb", **extra})

    def test_explicit_color_conversion_endpoints_and_roundtrip(self):
        rgb = np.array([[[0, 0.04045, 1], [0.0031308, 0.5, 0.8]]], np.float64)
        linear = convert_rgb(rgb, source="srgb", target="linear-srgb")
        self.assertAlmostEqual(linear[0, 0, 1], 0.04045 / 12.92)
        np.testing.assert_allclose(
            convert_rgb(linear, source="linear-srgb", target="srgb"), rgb, atol=3e-8
        )

    def test_shared_swift_fixtures(self):
        path = Path(__file__).parent / "fixtures" / "rgb_metrics.json"
        fixtures = json.loads(path.read_text())
        for case in fixtures["cases"]:
            with self.subTest(case=case["name"]):
                shape = case["shape"]
                dtype = case["dtype"]
                prediction = np.array(case["prediction"], dtype=dtype).reshape(shape)
                reference = np.array(case["reference"], dtype=dtype).reshape(shape)
                mask = (
                    None
                    if case["mask"] is None
                    else np.array(case["mask"], bool).reshape(shape[:2])
                )
                result = compare_rgb(
                    prediction, reference, color_space=case["color_space"], mask=mask
                )
                for key, wanted in case["expected"].items():
                    if wanted == "Infinity":
                        self.assertEqual(result[key], math.inf)
                    elif isinstance(wanted, float):
                        self.assertAlmostEqual(result[key], wanted, places=10)
                    else:
                        self.assertEqual(result[key], wanted)


if __name__ == "__main__":
    unittest.main()
