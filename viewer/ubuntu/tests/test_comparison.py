"""Analytical image and cross-platform render-contract acceptance checks."""
from concurrent.futures import CancelledError
from pathlib import Path
import tempfile
import unittest

import numpy as np
from gsv import (Scene, Writer, GSVError, add_camera, add_camera_affine,
                 add_camera_rgb, add_persistent_gaussians, add_render_profile,
                 add_render_view)
from gs_studio.comparison import compare_view, render_view, render_ewa, _metrics
from gsv.metrics import compare_rgb


def profile():
    return dict(
        algorithm="ewa-splat",
        visibility={"center_depth": {"near": 0, "far": None, "near_inclusive": False, "far_inclusive": True}, "camera_volume": "ignore"},
        projection={"divide_epsilon": 0, "jacobian": {"mode": "unclamped", "factor": 1}, "pixel_offset": [0, 0]},
        filter={"covariance_dilation": 0, "radius_clip": 0, "antialias_compensation": False},
        support={"bounds": "tile-major-radius", "sigma": 3, "tile_size": [4, 4], "mahalanobis_cutoff": None},
        alpha={"minimum": 0, "maximum": 1},
        compositing={"direction": "front-to-back", "depth_sort": "global-stable", "tie_break": "source-order", "transmittance": {"threshold": 0, "inclusive": False, "stage": "before-contribution"}})


def gaussian(*, mean=(0, 0, 2), color=(1, 0, 0), opacity=.5, scale=(.2, .2, .2)):
    a = np.zeros((1, 16), np.float32)
    a[0, :3] = mean
    a[0, 3] = opacity
    a[0, 4:7] = scale
    a[0, 7:9] = 1
    a[0, 12:15] = color
    a[0, 15] = -1
    return a


class AnalyticalTests(unittest.TestCase):
    def render(self, a=None, p=None, **kwargs):
        return render_ewa(gaussian() if a is None else a,
                          np.array([[10., 0, 5], [0, 10, 5], [0, 0, 1]]),
                          np.eye(4), 11, 11, profile() if p is None else p,
                          pixel_center="integer", **kwargs)

    def test_isotropic_center_and_one_sigma(self):
        image = self.render()
        self.assertEqual(image.dtype, np.float32)
        np.testing.assert_allclose(image[5, 5], [.5, 0, 0], atol=1e-7)
        np.testing.assert_allclose(image[5, 6], [.5 * np.exp(-.5), 0, 0], atol=1e-7)
        # Tile support has no implicit ellipse cutoff when the profile omits it.
        self.assertLess(image[0, 0, 0], 1e-10)

    def test_anisotropic_covariance(self):
        image = self.render(gaussian(scale=(.4, .2, .1)))
        self.assertAlmostEqual(image[5, 7, 0], image[6, 5, 0], places=6)

    def test_front_to_back_depth_sort_and_stable_tie(self):
        red, blue = gaussian(), gaussian(color=(0, 0, 1), mean=(0, 0, 3))
        a = np.concatenate([blue, red])
        np.testing.assert_allclose(self.render(a)[5, 5], [.5, 0, .25], atol=1e-7)
        blue[:, 2] = 2
        np.testing.assert_allclose(self.render(np.concatenate([blue, red]))[5, 5], [.25, 0, .5], atol=1e-7)

    def test_background_after_transmission(self):
        np.testing.assert_allclose(self.render(background=(.2, .4, .6))[5, 5], [.6, .2, .3], atol=1e-7)

    def test_near_far_inclusion_and_camera_volume(self):
        p = profile()
        p["visibility"]["center_depth"].update(far=2., far_inclusive=False)
        self.assertEqual(self.render(p=p).max(), 0)
        p["visibility"]["center_depth"]["far_inclusive"] = True
        self.assertEqual(self.render(p=p)[5, 5, 0], .5)
        self.assertEqual(self.render(p=p, volume=(2.1, None)).max(), 0)
        p["visibility"]["center_depth"].update(near=2., far=None, near_inclusive=False)
        self.assertEqual(self.render(p=p).max(), 0)
        p["visibility"]["center_depth"]["near_inclusive"] = True
        self.assertEqual(self.render(p=p)[5, 5, 0], .5)

    def test_alpha_cap_minimum_and_mahalanobis(self):
        p = profile()
        p["alpha"].update(maximum=.2, minimum=.15)
        image = self.render(p=p)
        self.assertAlmostEqual(image[5, 5, 0], .2, places=7)
        self.assertEqual(image[5, 7, 0], 0)
        p["support"]["mahalanobis_cutoff"] = .9
        self.assertEqual(self.render(p=p)[5, 6, 0], 0)

    def test_radius_clip_and_antialias_compensation(self):
        p = profile()
        p["filter"]["radius_clip"] = 4
        self.assertEqual(self.render(p=p).max(), 0)
        p["filter"].update(radius_clip=0, covariance_dilation=1, antialias_compensation=True)
        self.assertAlmostEqual(self.render(p=p)[5, 5, 0], .25, places=6)

    def test_transmittance_termination_precedes_contribution(self):
        p = profile()
        p["compositing"]["transmittance"].update(threshold=.5, inclusive=True)
        self.assertEqual(self.render(p=p)[5, 5, 0], 0)
        p["compositing"]["transmittance"]["inclusive"] = False
        self.assertEqual(self.render(p=p)[5, 5, 0], .5)

    def test_pixel_center_and_offset(self):
        p = profile()
        p["projection"]["pixel_offset"] = [.5, .5]
        K = np.array([[10., 0, 5], [0, 10, 5], [0, 0, 1]])
        half = render_ewa(gaussian(), K, np.eye(4), 11, 11, p, pixel_center="half")
        np.testing.assert_allclose(half, self.render(), atol=1e-7)

    def test_opacity_bounds_support_zero_minimum(self):
        p = profile()
        p["support"]["bounds"] = "opacity-aware"
        self.assertEqual(self.render(p=p)[5, 5, 0], .5)
        p["alpha"]["minimum"] = .3
        self.assertEqual(self.render(p=p)[5, 7, 0], 0)

    def test_cancel_and_allocation_limit(self):
        with self.assertRaises(CancelledError):
            self.render(cancelled=lambda: True)
        with self.assertRaises(GSVError):
            render_ewa(gaussian(), np.eye(3), np.eye(4), 20000, 1, profile())

    def test_strip_metrics_match_sdk_without_double_counting_halos(self):
        rng = np.random.default_rng(178)
        a, b = [rng.random((287, 17, 3), dtype=np.float32) for _ in range(2)]
        mask = rng.random((287, 17)) > .002
        for selection in (mask, np.zeros_like(mask), None):
            actual = _metrics(a, b, "srgb", selection, lambda: False)
            expected = compare_rgb(a, b, color_space="srgb", mask=selection)
            for key in expected:
                if isinstance(expected[key], float):
                    self.assertAlmostEqual(actual[key], expected[key], places=12, msg=key)
                else:
                    self.assertEqual(actual[key], expected[key], key)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "scene.gsv"

    def write(self, *, observed=True, times=(0, 1), camera_times=(0,), affine=False,
              distorted=False, transformed=False, integrated=False, mask=None, static=False,
              second_observation=False):
        with Writer(self.path, overwrite=True) as out:
            count = len(camera_times)
            K = np.tile(np.array([[10., 0, 5], [0, 10, 5], [0, 0, 1]]), (count, 1, 1, 1))
            distortion = {"model": "opencv-brown", "coefficients": np.zeros((count, 1, 5))} if distorted else None
            add_camera(out, "camera", camera_times, K, np.tile(np.eye(4), (count, 1, 1, 1)), [[11, 11]], pixel_center="integer", distortion=distortion)
            if static:
                arrays = {"ids": np.array([7], np.int64), "means": np.array([[0, 0, 2]], np.float32), "scales": np.array([[.2, .2, .2]], np.float32), "quaternions": np.array([[1, 0, 0, 0]], np.float32), "colors": np.array([[1, 0, 0]], np.float32), "opacity": np.array([.5], np.float32)}
                d = {k: out.add_array("field/"+k, v) for k, v in arrays.items()}
                d["color_space"] = "srgb"
                add = out.add_object("field", "gaussian", "gsv.gaussian.static", d, space="world")
            else:
                add_persistent_gaussians(out, "field", np.array([7], np.int64), times, [gaussian()] * len(times))
            add_render_profile(out, "profile", **profile())
            extras = {}
            if observed:
                samples = [0] * len(times) if count == 1 else list(range(len(times)))
                add_camera_rgb(out, "obs", "camera", samples, [0] * len(times), [np.zeros((11, 11, 3), np.uint8)] * len(times), times=times, masks=None if mask is None else [mask] * len(times), color_space="srgb", role="reference")
                extras["observation_ids"] = ["obs"]
                if second_observation:
                    add_camera_rgb(out, "other-obs", "camera", samples, [0]*len(times), [np.zeros((11,11,3),np.uint8)]*len(times), times=times,color_space="srgb")
                    extras["observation_ids"].append("other-obs")
            if affine:
                add_camera_affine(out, "response", "camera", np.ones((count, 1, 3), np.float32), np.full((count, 1, 3), .1, np.float32), color_space="srgb")
                extras["radiometry_id"] = "response"
            if transformed:
                extras.update(image_size=[6, 6], camera_to_output=[[.5, 0, 0], [0, .5, 0], [0, 0, 1]])
            if integrated:
                extras["temporal"] = {"mode": "integrated", "shutter_open": -.1, "shutter_close": .1, "samples": 4, "sampling": "uniform-midpoint"}
            add_render_view(out, "view", "camera", ["field"], "profile", **extras)

    def test_static_camera_dynamic_field_exact_time(self):
        self.write()
        with Scene(self.path) as scene:
            for time in (0, 1):
                result = compare_view(scene, "view", time)
                self.assertEqual(result.render.shape, (11, 11, 3))
                self.assertEqual(result.metrics["valid_pixels"], 121)
                self.assertEqual(result.metrics["ssim_windows"], 1)
            with self.assertRaisesRegex(GSVError, "No exact observation"):
                compare_view(scene, "view", .5)
            with self.assertRaisesRegex(GSVError, "No exact geometry"):
                render_view(scene, "view", .5)

    def test_dynamic_camera_exact_calibration(self):
        self.write(observed=False, camera_times=(0, 1))
        with Scene(self.path) as scene:
            with self.assertRaisesRegex(GSVError, "No exact camera"):
                render_view(scene, "view", .5)

    def test_static_geometry_at_arbitrary_time(self):
        self.write(static=True)
        with Scene(self.path) as scene:
            result = render_view(scene, "view", 12345)
            self.assertEqual(result.render[5, 5, 0], .5)

    def test_affine_only_changes_prediction_and_applies_before_clamp(self):
        self.write(affine=True)
        with Scene(self.path) as scene:
            result = compare_view(scene, "view", 0)
            np.testing.assert_allclose(result.render[5, 5], [.6, .1, .1], atol=1e-6)
            self.assertEqual(result.reference.max(), 0)

    def test_masked_metrics_have_no_false_perfect_score(self):
        self.write(mask=np.zeros((11, 11), bool))
        with Scene(self.path) as scene:
            result = compare_view(scene, "view", 0)
            self.assertEqual(result.metrics["valid_pixels"], 0)
            self.assertIsNone(result.metrics["psnr"])

    def test_render_only_without_observations(self):
        self.write(observed=False)
        with Scene(self.path) as scene:
            result = compare_view(scene, "view", 0)
            self.assertIsNone(result.reference)
            self.assertIsNone(result.metrics)

    def test_transformed_output_can_preview_but_cannot_score(self):
        self.write(transformed=True)
        with Scene(self.path) as scene:
            self.assertEqual(render_view(scene, "view", 0).render.shape, (6, 6, 3))
            with self.assertRaisesRegex(GSVError, "native camera raster"):
                compare_view(scene, "view", 0)

    def test_unsupported_optics_and_time_integration(self):
        for option, message in (("distorted", "distortion"), ("integrated", "integration")):
            self.write(**{option: True})
            with Scene(self.path) as scene:
                with self.assertRaisesRegex(GSVError, message):
                    compare_view(scene, "view", 0)

    def test_observation_membership_and_ambiguous_images(self):
        self.write(second_observation=True)
        with Scene(self.path) as scene:
            with self.assertRaisesRegex(GSVError, "Multiple linked"):
                compare_view(scene, "view", 0)
            self.assertIsNotNone(compare_view(scene, "view", 0, observation_id="obs").metrics)
            with self.assertRaisesRegex(GSVError, "not explicitly linked"):
                compare_view(scene, "view", 0, observation_id="foreign")

    def test_mac_native_parser_fixtures_share_comparison_capabilities(self):
        # Reuse macOS contract fixtures rather than invent a Linux-only format.
        import importlib.util
        root = Path(__file__).resolve().parents[2]
        source = root / "native" / "tests" / "camera_render_fixtures.py"
        spec = importlib.util.spec_from_file_location("camera_render_fixtures", source)
        fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixtures)
        kinds = ["full_camera_tracks", "static_camera_observation_times", "static_gaussian", "affine",
                 "symmetric_factor_half", "image_bounds", "finite_inclusive_far", "finite_exclusive_far",
                 "distorted_unsupported", "integrated_unsupported", "cropped_unsupported",
                 "metadata_cannot_extend_time", "typed_domain_over_metadata"]
        unsupported = {"distorted_unsupported", "integrated_unsupported", "cropped_unsupported", "metadata_cannot_extend_time"}
        for kind in kinds:
            with self.subTest(kind=kind):
                with Writer(self.path, overwrite=True) as out:
                    fixtures.build(out, kind)
                time = .5 if kind == "metadata_cannot_extend_time" else 0
                with Scene(self.path) as scene:
                    scene.validate()
                    if kind in unsupported:
                        with self.assertRaises(GSVError):
                            compare_view(scene, "view", time)
                    else:
                        result = compare_view(scene, "view", time)
                        self.assertEqual(result.metrics["valid_pixels"], 240)


if __name__ == "__main__":
    unittest.main()
