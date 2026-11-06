"""Calibration, visibility and image output remain explicit independent peers."""

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
    add_camera_rgb,
    add_persistent_gaussians,
    add_render_profile,
    add_render_view,
)


def profile_parameters():
    return {
        "visibility": {
            "center_depth": {
                "near": 0.2,
                "far": None,
                "near_inclusive": False,
                "far_inclusive": True,
            },
            "camera_volume": "ignore",
        },
        "projection": {
            "divide_epsilon": 1e-7,
            "jacobian": {"mode": "symmetric-fov", "factor": 1.3},
            "pixel_offset": [0.0, 0.0],
        },
        "filter": {
            "covariance_dilation": 0.3,
            "radius_clip": 0.0,
            "antialias_compensation": False,
        },
        "support": {
            "bounds": "tile-major-radius",
            "sigma": 3.0,
            "tile_size": [16, 16],
            "mahalanobis_cutoff": None,
        },
        "alpha": {"maximum": 0.99, "minimum": 1 / 255},
        "compositing": {
            "direction": "front-to-back",
            "depth_sort": "tile-stable",
            "tie_break": "source-order",
            "transmittance": {
                "threshold": 1e-4,
                "inclusive": False,
                "stage": "before-contribution",
            },
        },
    }


class CameraRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "scene.gsv"

    def camera(self, out, id="camera", **kwargs):
        K = np.array([[25.0, 0.3, 10.0], [0.0, 24.0, 6.0], [0.0, 0.0, 1.0]])[None, None]
        return add_camera(
            out,
            id,
            [0.0],
            K,
            np.eye(4)[None, None],
            [[20, 12]],
            pixel_center="half",
            **kwargs,
        )

    def field(self, out, id="field", **kwargs):
        frame = np.zeros((1, 16), np.float32)
        frame[:, 2] = 2.0
        frame[:, 3:7] = 0.1
        frame[:, 7:9] = 1
        frame[:, 15] = -1
        return add_persistent_gaussians(
            out, id, np.array([5], np.int64), [0.0, 0.5, 1.0], [frame] * 3, **kwargs
        )

    def profile(self, out, **kwargs):
        parameters = profile_parameters()
        parameters.update(kwargs)
        return add_render_profile(out, "profile", **parameters)

    def test_empty_and_camera_and_profile_are_independently_optional(self):
        for build, count in (
            (lambda out: None, 0),
            (self.camera, 1),
            (self.profile, 1),
            (self.field, 1),
        ):
            with self.subTest(count=count):
                with Writer(self.path, overwrite=True) as out:
                    build(out)
                with Scene(self.path) as scene:
                    self.assertEqual(len(scene.validate()), count)

    def test_static_camera_supports_many_field_times_without_gt(self):
        with Writer(self.path) as out:
            self.camera(out, view_ids=["acquisition-camera-17"])
            self.field(out)
            self.profile(out)
            obj = add_render_view(out, "view", "camera", ["field"], "profile")
            self.assertEqual(obj["data"]["output"]["image_size"], [20, 12])
        with Scene(self.path) as scene:
            self.assertEqual(len(scene.validate()), 4)
            self.assertEqual(
                scene.object("camera")["data"]["view_ids"], ["acquisition-camera-17"]
            )
            self.assertEqual(
                scene.array(scene.object("camera")["data"]["times"]).shape, (1,)
            )
            self.assertEqual(
                scene.array(scene.object("field")["data"]["times"]).shape, (3,)
            )
            self.assertFalse(any(obj["type"] == "image" for obj in scene.objects))

    def test_authored_volume_projection_mapping_and_center_filter_independent_roundtrip(
        self,
    ):
        with Writer(self.path) as out:
            camera = self.camera(
                out,
                depth_mapping={
                    "near": 0.0001,
                    "far": 1.0,
                    "ndc_range": "zero-to-one",
                    "direction": "forward",
                },
                view_volume={
                    "near": [[0.0]],
                    "far": [[0.0]],
                    "far_unbounded": [[True]],
                },
            )
            self.field(out)
            self.profile(
                out,
                visibility={
                    "center_depth": {
                        "near": 0.02,
                        "far": None,
                        "near_inclusive": False,
                        "far_inclusive": False,
                    },
                    "camera_volume": "gaussian-center",
                },
            )
            add_render_view(out, "view", "camera", ["field"], "profile")
        with Scene(self.path) as scene:
            scene.validate()
            camera = scene.object("camera")["data"]
            self.assertEqual(camera["projection"]["depth_mapping"]["far"], 1.0)
            self.assertTrue(scene.array(camera["view_volume"]["far_unbounded"])[0, 0])
            self.assertIsNone(
                scene.object("profile")["data"]["visibility"]["center_depth"]["far"]
            )
            clone = self.path.with_name("copy.gsv")
            scene.copy(clone)
        with Scene(clone) as scene:
            self.assertEqual(len(scene.validate()), 4)

    def test_capture_tracks_and_distortion_store_without_claiming_renderer_support(
        self,
    ):
        for model, dimension in (("opencv-brown", 5), ("opencv-fisheye", 4)):
            with self.subTest(model=model):
                with Writer(self.path, overwrite=True) as out:
                    self.camera(
                        out,
                        distortion={
                            "model": model,
                            "coefficients": np.zeros((1, 1, dimension)),
                        },
                        optics={
                            "focal_length_mm": [[35]],
                            "sensor_width_mm": [[36]],
                            "sensor_height_mm": [[24]],
                            "f_number": [[2.8]],
                            "focus_distance": [[2.5]],
                        },
                        acquisition={
                            "exposure_seconds": [[0.01]],
                            "iso": [[200]],
                            "shutter_open": [[-0.005]],
                            "shutter_close": [[0.005]],
                            "rolling_readout": [[0.02]],
                            "rolling_direction": "top-to-bottom",
                        },
                    )
                    self.field(out)
                    self.profile(out)
                    add_render_view(
                        out,
                        "view",
                        "camera",
                        ["field"],
                        "profile",
                        temporal={
                            "mode": "integrated",
                            "shutter_open": -0.005,
                            "shutter_close": 0.005,
                            "samples": 8,
                            "sampling": "uniform-midpoint",
                        },
                    )
                with Scene(self.path) as scene:
                    self.assertEqual(len(scene.validate()), 4)
                    lens = scene.object("camera")["data"]["projection"]["distortion"]
                    self.assertEqual(
                        scene.array(lens["coefficients"]).shape, (1, 1, dimension)
                    )
                    self.assertEqual(
                        scene.object("view")["data"]["temporal"]["mode"], "integrated"
                    )

    def test_invalid_camera_tracks_distortion_and_flat_layout_fail(self):
        cases = [
            {"view_ids": ["duplicate", "duplicate"]},
            {"view_ids": [""]},
            {"view_ids": "camera"},
            {
                "view_volume": {
                    "near": [[0.2]],
                    "far": [[0.1]],
                    "far_unbounded": [[False]],
                }
            },
            {
                "view_volume": {
                    "near": [[0.0]],
                    "far": [[10.0]],
                    "far_unbounded": [[True]],
                }
            },
            {"view_volume": {"near": [[0.0]], "far": [[10.0]], "far_unbounded": [[1]]}},
            {
                "view_volume": {
                    "near": [[0.0]],
                    "far": [[np.inf]],
                    "far_unbounded": [[True]],
                }
            },
            {
                "depth_mapping": {
                    "near": 0.0,
                    "far": None,
                    "ndc_range": "zero-to-one",
                    "direction": "forward",
                }
            },
            {
                "depth_mapping": {
                    "near": 1.0,
                    "far": 1.0,
                    "ndc_range": "zero-to-one",
                    "direction": "forward",
                }
            },
            {"optics": {"f_number": [[0.0]]}},
            {"optics": {"focus_distance": [1.0]}},
            {"acquisition": {"shutter_open": [[0.0]]}},
            {"acquisition": {"shutter_open": [[1.0]], "shutter_close": [[0.0]]}},
            {"acquisition": {"rolling_readout": [[0.1]]}},
            {"acquisition": {"exposure_seconds": [[0.0]]}},
            {
                "distortion": {
                    "model": "opencv-brown",
                    "coefficients": np.zeros((1, 1, 4)),
                }
            },
            {"distortion": {"model": "none", "coefficients": np.zeros((1, 1, 4))}},
            {"distortion": {"model": "unknown", "coefficients": np.zeros((1, 1, 4))}},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    self.camera(out, **kwargs)
            self.assertFalse(self.path.exists())
        with self.assertRaises(GSVError):
            with Writer(self.path) as out:
                camera = self.camera(out)
                camera["data"]["intrinsics"] = camera["data"]["projection"][
                    "intrinsics"
                ]

    def test_finite_volume_and_unbounded_mapping_are_valid(self):
        with Writer(self.path) as out:
            self.camera(
                out,
                view_volume={
                    "near": [[0.1]],
                    "far": [[10.0]],
                    "far_unbounded": [[False]],
                },
                depth_mapping={
                    "near": 0.01,
                    "far": None,
                    "ndc_range": "minus-one-to-one",
                    "direction": "reversed",
                },
            )
        with Scene(self.path) as scene:
            self.assertEqual(len(scene.validate()), 1)

    def test_missing_profile_fields_and_invalid_numerics_abort(self):
        changes = [
            ("alpha", "maximum", 1.1),
            ("alpha", "minimum", 0.999),
            ("support", "sigma", 0),
            ("support", "tile_size", [True, 16]),
            ("support", "mahalanobis_cutoff", -1),
            ("projection", "divide_epsilon", float("nan")),
            ("projection", "pixel_offset", [0]),
            ("filter", "radius_clip", -1),
            ("filter", "antialias_compensation", 1),
        ]
        for group, name, value in changes:
            parameters = profile_parameters()
            parameters[group][name] = value
            with self.subTest(name=name), self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    add_render_profile(out, "profile", **parameters)
        with self.assertRaises(GSVError):
            with Writer(self.path) as out:
                profile = self.profile(out)
                del profile["data"]["support"]["sigma"]
        with self.assertRaises(GSVError):
            with Writer(self.path) as out:
                profile = self.profile(out)
                profile["data"]["visibility"]["center_depth"]["near_inclusive"] = 1

    def test_profile_owns_immutable_copy_of_input_dicts(self):
        parameters = profile_parameters()
        with Writer(self.path) as out:
            add_render_profile(out, "profile", **parameters)
            parameters["alpha"]["maximum"] = 100
        with Scene(self.path) as scene:
            self.assertEqual(scene.object("profile")["data"]["alpha"]["maximum"], 0.99)

    def test_render_view_affine_and_observation_membership_explicit(self):
        with Writer(self.path) as out:
            self.camera(out)
            self.field(out)
            self.profile(out)
            add_camera_rgb(
                out,
                "rgb",
                "camera",
                [0],
                [0],
                [np.zeros((12, 20, 3), np.uint8)],
                color_space="srgb",
            )
            add_camera_affine(
                out,
                "affine",
                "camera",
                np.ones((1, 1, 3), np.float32),
                np.zeros((1, 1, 3), np.float32),
                color_space="srgb",
            )
            add_render_view(
                out,
                "view",
                "camera",
                ["field"],
                "profile",
                radiometry_id="affine",
                observation_ids=["rgb"],
            )
        with Scene(self.path) as scene:
            self.assertEqual(len(scene.validate()), 6)
            links = scene.object("view")["links"]
            self.assertEqual(links["radiometry"], "affine")
            self.assertEqual(links["observations"], ["rgb"])
            self.assertNotIn("radiometry", scene.object("field").get("links", {}))

    def test_bad_render_view_associations_fail(self):
        for mutation in (
            "clock",
            "profile",
            "field",
            "duplicate",
            "empty",
            "camera-view",
            "volume",
            "radiometry-camera",
            "radiometry-domain",
            "observations-camera",
        ):
            with self.subTest(mutation=mutation), self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    self.camera(out)
                    self.camera(out, id="camera2")
                    self.field(out)
                    profile = self.profile(out)
                    kwargs = {}
                    field_ids = ["field"]
                    profile_id = "profile"
                    if mutation == "clock":
                        out.manifest["objects"][2]["clock"] = "other"
                        out.manifest["clocks"]["other"] = {"unit": "second"}
                    elif mutation == "profile":
                        profile_id = "camera"
                    elif mutation == "field":
                        field_ids = ["camera"]
                    elif mutation == "duplicate":
                        field_ids = ["field", "field"]
                    elif mutation == "empty":
                        field_ids = []
                    elif mutation == "camera-view":
                        kwargs["camera_view"] = 1
                    elif mutation == "volume":
                        profile["data"]["visibility"]["camera_volume"] = (
                            "gaussian-center"
                        )
                    elif mutation.startswith("radiometry"):
                        add_camera_affine(
                            out,
                            "affine",
                            "camera2" if mutation == "radiometry-camera" else "camera",
                            np.ones((1, 1, 3), np.float32),
                            np.zeros((1, 1, 3), np.float32),
                            color_space="linear-srgb"
                            if mutation == "radiometry-domain"
                            else "srgb",
                        )
                        kwargs["radiometry_id"] = "affine"
                    elif mutation == "observations-camera":
                        add_camera_rgb(
                            out,
                            "rgb",
                            "camera2",
                            [0],
                            [0],
                            [np.zeros((12, 20, 3), np.uint8)],
                            color_space="srgb",
                        )
                        kwargs["observation_ids"] = ["rgb"]
                    add_render_view(
                        out, "view", "camera", field_ids, profile_id, **kwargs
                    )

    def test_invalid_output_and_temporal_policies_fail(self):
        cases = [
            {"image_size": [20, 0]},
            {"camera_to_output": [[1, 0.2, 0], [0, 1, 0], [0, 0, 1]]},
            {"camera_to_output": [[-1, 0, 0], [0, 1, 0], [0, 0, 1]]},
            {"background": [0, 0, float("nan")]},
            {"clamp": "implicit"},
            {"temporal": {"mode": "instantaneous", "samples": 1}},
            {
                "temporal": {
                    "mode": "integrated",
                    "shutter_open": 1,
                    "shutter_close": 0,
                    "samples": 2,
                    "sampling": "uniform-midpoint",
                }
            },
            {
                "temporal": {
                    "mode": "integrated",
                    "shutter_open": 0,
                    "shutter_close": 1,
                    "samples": 0,
                    "sampling": "uniform-midpoint",
                }
            },
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(GSVError):
                with Writer(self.path) as out:
                    self.camera(out)
                    self.field(out)
                    self.profile(out)
                    add_render_view(
                        out, "view", "camera", ["field"], "profile", **kwargs
                    )

    def test_static_gaussian_without_clock_can_share_dynamic_view(self):
        with Writer(self.path) as out:
            self.camera(out)
            self.profile(out)
            data = {
                name: out.add_array("static/" + name, a)
                for name, a in {
                    "ids": np.array([1], np.int64),
                    "means": np.array([[0.0, 0.0, 2.0]]),
                    "scales": np.full((1, 3), 0.1),
                    "quaternions": np.array([[1.0, 0, 0, 0]]),
                    "opacity": np.array([0.5]),
                    "colors": np.ones((1, 3)),
                }.items()
            }
            data["color_space"] = "srgb"
            out.add_object(
                "static", "gaussian", "gsv.gaussian.static", data, space="world"
            )
            add_render_view(out, "view", "camera", ["static"], "profile")
        with Scene(self.path) as scene:
            self.assertEqual(len(scene.validate()), 4)

    def test_nested_observation_required_and_metadata_does_not_supply_semantics(self):
        with self.assertRaises(GSVError):
            with Writer(self.path) as out:
                self.camera(out)
                obs = add_camera_rgb(
                    out,
                    "rgb",
                    "camera",
                    [0],
                    [0],
                    [np.zeros((12, 20, 3), np.uint8)],
                    color_space="srgb",
                )
                obs["data"] = {
                    **obs["data"]["samples"],
                    **obs["data"]["pixels"],
                    "role": "ground_truth",
                }
        with self.assertRaises(GSVError):
            with Writer(self.path) as out:
                profile = self.profile(out)
                profile["metadata"] = {"visibility": profile["data"].pop("visibility")}

    def test_static_calibration_has_independent_observation_times(self):
        with Writer(self.path) as out:
            self.camera(out)
            self.field(out)
            self.profile(out)
            add_camera_rgb(
                out,
                "rgb",
                "camera",
                [0, 0, 0],
                [0, 0, 0],
                [np.zeros((12, 20, 3), np.uint8)] * 3,
                times=[0.0, 0.5, 1.0],
                color_space="srgb",
            )
            add_render_view(
                out, "view", "camera", ["field"], "profile", observation_ids=["rgb"]
            )
        with Scene(self.path) as scene:
            scene.validate()
            data = scene.object("rgb")["data"]["samples"]
            np.testing.assert_array_equal(scene.array(data["times"]), [0.0, 0.5, 1.0])
            np.testing.assert_array_equal(
                scene.array(data["camera_samples"]), [0, 0, 0]
            )
        for times in ([0.0, 0.0], [0.0, float("nan")], [0.0], [[0.0, 1.0]]):
            with self.subTest(times=times), self.assertRaises(GSVError):
                with Writer(self.path, overwrite=True) as out:
                    self.camera(out)
                    add_camera_rgb(
                        out,
                        "rgb",
                        "camera",
                        [0, 0],
                        [0, 0],
                        [np.zeros((12, 20, 3), np.uint8)] * 2,
                        times=times,
                        color_space="srgb",
                    )

    def test_dynamic_calibration_observations_require_exact_sample_times(self):
        for times in ([0.0, 0.9], [0.0, 1.0]):

            def write():
                with Writer(self.path, overwrite=True) as out:
                    add_camera(
                        out,
                        "camera",
                        [0.0, 1.0],
                        np.tile(np.eye(3), (2, 1, 1, 1)),
                        np.tile(np.eye(4), (2, 1, 1, 1)),
                        [[20, 12]],
                    )
                    add_camera_rgb(
                        out,
                        "rgb",
                        "camera",
                        [0, 1],
                        [0, 0],
                        [np.zeros((12, 20, 3), np.uint8)] * 2,
                        times=times,
                        color_space="srgb",
                    )

            if times[1] != 1.0:
                with self.assertRaisesRegex(GSVError, "exact calibration"):
                    write()
            else:
                write()
                with Scene(self.path) as scene:
                    self.assertEqual(len(scene.validate()), 2)

    def test_gaussian_rgb_domain_is_typed_and_cannot_come_from_metadata(self):
        with self.assertRaisesRegex(GSVError, "color domains"):
            with Writer(self.path) as out:
                self.camera(out)
                self.field(
                    out, color_space="linear-srgb", metadata={"color_space": "srgb"}
                )
                self.profile(out)
                add_render_view(
                    out, "view", "camera", ["field"], "profile", color_space="srgb"
                )
        with self.assertRaisesRegex(GSVError, "data.color_space"):
            with Writer(self.path) as out:
                field = self.field(out, metadata={"color_space": "srgb"})
                del field["data"]["color_space"]
        with Writer(self.path) as out:
            self.camera(out)
            self.field(out, color_space="linear-srgb", metadata={"color_space": "srgb"})
            self.profile(out)
            add_render_view(
                out, "view", "camera", ["field"], "profile", color_space="linear-srgb"
            )
        with Scene(self.path) as scene:
            self.assertEqual(len(scene.validate()), 4)


if __name__ == "__main__":
    unittest.main()
