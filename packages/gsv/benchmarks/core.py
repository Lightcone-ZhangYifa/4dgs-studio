"""Bounded CPU microbenchmarks; these do not measure rendering or disk throughput.

Run from the repository root with the GSV package installed. The temporary
scene contains many small buffers to expose metadata traversal cost. Repeated
reads may use the OS cache. Results are observations, never test thresholds.
"""

import argparse
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np
from gsv import Scene, Writer
from gsv.evaluate import matrix_quat, quat_matrix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buffers", type=int, default=3000)
    parser.add_argument("--rotations", type=int, default=100000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if min(args.buffers, args.rotations, args.repeats) <= 0:
        parser.error("All counts must be positive")
    result = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "platform": platform.platform(),
        "seed": 20261002,
        "scope": "CPU microbenchmark; not renderer FPS or cold-storage bandwidth",
    }
    with tempfile.TemporaryDirectory(prefix="gsv-benchmark-") as directory:
        path = Path(directory) / "buffers.gsv"
        with Writer(path) as out:
            for index in range(args.buffers):
                out.add_array(str(index), np.array([index], dtype=np.int64))
        elapsed = []
        for _ in range(args.repeats):
            with Scene(path) as scene:
                start = time.perf_counter()
                scene.validate()
                elapsed.append(time.perf_counter() - start)
        result["integrity_audit"] = {
            "buffers": args.buffers,
            "bytes": path.stat().st_size,
            "seconds": elapsed,
            "median_seconds": statistics.median(elapsed),
        }
    quaternion = np.random.default_rng(result["seed"]).normal(size=(args.rotations, 4))
    quaternion /= np.linalg.norm(quaternion, axis=1, keepdims=True)
    matrices = quat_matrix(quaternion)
    elapsed = []
    for _ in range(args.repeats):
        start = time.perf_counter()
        restored = matrix_quat(matrices)
        elapsed.append(time.perf_counter() - start)
    result["matrix_to_quaternion"] = {
        "rotations": args.rotations,
        "seconds": elapsed,
        "median_seconds": statistics.median(elapsed),
        "matrix_roundtrip_max_abs": float(
            np.max(np.abs(quat_matrix(restored) - matrices))
        ),
    }
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
