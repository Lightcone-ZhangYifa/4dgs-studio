"""Generate deterministic empty, camera-only and animated GSV test scenes."""

import argparse
from pathlib import Path

import numpy as np
from gsv import Writer, add_camera, add_persistent_gaussians


def write_fixtures(output):
    output.mkdir(parents=True, exist_ok=True)
    with Writer(output / "empty.gsv", overwrite=True):
        pass
    K = np.array([[500.0, 0, 320], [0, 500, 240], [0, 0, 1]])
    poses = np.tile(np.eye(4), (3, 1, 1, 1))
    poses[:, 0, 2, 3] = -4
    poses[:, 0, 0, 3] = [0, 1, 2]
    with Writer(output / "camera_only.gsv", overwrite=True) as out:
        add_camera(
            out,
            "camera",
            [0.0, 0.5, 2.0],
            np.tile(K, (3, 1, 1, 1)),
            poses,
            [[640, 480]],
            metadata={"role": "ground_truth"},
        )

    def frames():
        for t in range(10):
            f = np.zeros((200, 16), dtype="float32")
            angle = np.arange(200) * 2 * np.pi / 200
            f[:, 0] = np.cos(angle)
            f[:, 1] = np.sin(angle)
            f[:, 2] = np.sin(angle * 3 + t * 0.1) * 0.3
            f[:, 3] = 0.9
            f[:, 4:7] = 0.025
            f[:, 7:9] = 1
            f[:, 12:15] = [0.9, 0.4, 0.1]
            f[:, 15] = -1
            yield f

    with Writer(
        output / "persistent_identity_with_cameras.gsv",
        overwrite=True,
        metadata={"purpose": "engineering_fixture"},
    ) as out:
        add_persistent_gaussians(
            out, "ring", np.arange(200, dtype="int64"), np.arange(10) / 5, frames()
        )
        add_camera(
            out,
            "camera",
            [0.0, 0.5, 2.0],
            np.tile(K, (3, 1, 1, 1)),
            poses,
            [[640, 480]],
            metadata={"role": "ground_truth"},
        )
        out.add_object(
            "unknown",
            "motion",
            "org.example.future",
            {"description": "Inspectable extension, no evaluator"},
            label="Unrecognized method data",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    write_fixtures(parser.parse_args().output)
