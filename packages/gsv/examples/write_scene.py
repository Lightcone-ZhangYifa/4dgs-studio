"""Small synthetic I/O example, not a fitted reconstruction or benchmark.

Run: python packages/gsv/examples/write_scene.py /tmp/example.gsv
"""

import argparse

import numpy as np
from gsv import (
    Scene,
    Writer,
    add_camera,
    add_camera_rgb,
    add_persistent_gaussians,
    add_render_profile,
    add_render_view,
)


def write_scene(path):
    # IDs are arbitrary sequence-local integers. Never cast them through float.
    ids = np.array([7, 2**60 + 3], dtype=np.int64)
    times = np.array([0.0, 0.5, 1.0], dtype=np.float64)

    def frames():
        for time in times:
            frame = np.zeros((len(ids), 16), dtype=np.float32)
            frame[:, :3] = [[-0.2 + 0.2 * time, 0, 2], [0.2, 0.1 * time, 2]]
            frame[:, 3] = 0.8  # Activated opacity
            frame[:, 4:7] = [0.05, 0.03, 0.02]  # Axis standard deviations
            frame[:, 7] = 1  # Valid, distinct from opacity
            frame[:, 8] = 1  # Unit quaternion wxyz
            frame[:, 12:15] = [[1, 0.1, 0.1], [0.1, 0.5, 1]]
            frame[:, 15] = -1  # Confidence unavailable
            yield frame

    with Writer(path, metadata={"evidence": "synthetic I/O example"}) as out:
        out.manifest["spaces"]["world"] = {
            "unit": "meter",
            "handedness": "right",
            "axes": ["right", "down", "forward"],
        }
        # Seconds are known here because this example defines its own clock.
        # For capture data with unknown FPS use unit='frame' and source indices.
        out.manifest["clocks"]["scene"] = {"unit": "second"}
        add_persistent_gaussians(
            out,
            "moving-points",
            ids,
            times,
            frames(),
            color_space="srgb",
            metadata={"color_space": "srgb", "appearance": "constant RGB"},
        )
        K = np.array([[500, 0, 320], [0, 500, 240], [0, 0, 1]], dtype=np.float64)
        intrinsics = np.repeat(K[None, None], 2, axis=0)
        intrinsics[1, 0, 0, 0] = 520  # Time-varying focal length.
        camera_poses = np.repeat(np.eye(4)[None, None], 2, axis=0)
        camera_poses[1, 0, 0, 3] = 0.1
        add_camera(
            out,
            "reference-camera",
            [0.0, 1.0],
            intrinsics,
            camera_poses,
            [[640, 480]],
            pixel_center="half",
            view_ids=["front"],
            depth_mapping={
                "near": 0.01,
                "far": 100.0,
                "ndc_range": "zero-to-one",
                "direction": "forward",
            },
            view_volume={
                "near": [[0.01], [0.01]],
                "far": [[100.0], [100.0]],
                "far_unbounded": [[False], [False]],
            },
            metadata={"role": "reference"},
        )
        # Synthetic API images are explicitly references, not measured GT of
        # this Gaussian scene. Real input photographs use role='ground_truth'
        # and retain acquisition/rectification provenance.
        rows, columns = np.indices((480, 640))
        image = np.stack(
            (columns % 256, rows % 256, (rows + columns) % 256), axis=-1
        ).astype(np.uint8)
        add_camera_rgb(
            out,
            "reference-images",
            "reference-camera",
            np.array([0, 1], np.int64),
            np.array([0, 0], np.int32),
            iter([image, np.roll(image, 5, axis=1)]),
            color_space="srgb",
            role="reference",
            metadata={"source": "synthetic API image; not reconstruction ground truth"},
        )
        # Every policy is explicit. Projection depth mapping and this Gaussian
        # center filter have different meanings, even in a synthetic example.
        add_render_profile(
            out,
            "ewa-profile",
            visibility={
                "center_depth": {
                    "near": 0.01,
                    "far": None,
                    "near_inclusive": False,
                    "far_inclusive": True,
                },
                "camera_volume": "ignore",
            },
            projection={
                "divide_epsilon": 1e-7,
                "jacobian": {"mode": "symmetric-fov", "factor": 1.3},
                "pixel_offset": [0.0, 0.0],
            },
            filter={
                "covariance_dilation": 0.3,
                "radius_clip": 0.0,
                "antialias_compensation": False,
            },
            support={
                "bounds": "tile-major-radius",
                "sigma": 3.0,
                "tile_size": [16, 16],
                "mahalanobis_cutoff": None,
            },
            alpha={"minimum": 1 / 255, "maximum": 0.99},
            compositing={
                "direction": "front-to-back",
                "depth_sort": "tile-stable",
                "tie_break": "source-order",
                "transmittance": {
                    "threshold": 1e-4,
                    "inclusive": False,
                    "stage": "before-contribution",
                },
            },
        )
        add_render_view(
            out,
            "reference-view",
            "reference-camera",
            ["moving-points"],
            "ewa-profile",
            color_space="srgb",
            background=(0, 0, 0),
            observation_ids=["reference-images"],
        )

    with Scene(path) as scene:
        scene.validate()
        obj = scene.object("moving-points")
        print("Stored IDs:", scene.array(obj["data"]["ids"]))
        print("Objects:", [(o["id"], o["encoding"]) for o in scene.objects])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    write_scene(parser.parse_args().output)
