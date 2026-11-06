"""Generate deterministic color, identity, visibility-gap and clock fixtures."""
from pathlib import Path
import argparse
import numpy as np
from gsv import Writer, add_persistent_gaussians, add_camera


def records(n):
    value = np.zeros((n, 16), dtype=np.float32)
    value[:, 3] = .95
    value[:, 4:7] = .023
    value[:, 7:9] = 1
    value[:, 15] = -1
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    target = parser.parse_args().output
    target.mkdir(parents=True, exist_ok=True)
    times = np.array([0., .04, .08, .17, .23, .50, .72, 1.])
    ids = np.array([-19, 7, 23, 71, 103, 901], dtype=np.int64)
    frames = []
    for sample, time in enumerate(times):
        frame = records(len(ids))
        frame[:, 0] = np.arange(len(ids)) * .2 + time
        frame[:, 1] = np.arange(len(ids)) * .1
        frame[:, 2] = 3
        frame[2, 0] = .4  # stationary identity: coverage, no zero-length line
        frame[:, 12] = np.arange(len(ids)) / len(ids)
        frame[:, 13] = time
        frame[:, 14] = 1 - np.arange(len(ids)) / len(ids)
        if sample == 3:
            frame[1, 7] = 0  # no connecting line across a birth/death gap
        frames.append(frame)
    with Writer(target / 'trajectory_contract.gsv', overwrite=True) as out:
        add_persistent_gaussians(out, 'colored', ids, times, frames)
        add_persistent_gaussians(out, 'stationary', ids[:1], [0.], [frames[0][:1]])

    # Both clocks are valid GSV Double tracks. The first intentionally cannot
    # retain its final positive interval in the preview's Float32 coordinates;
    # the second verifies that a large absolute epoch alone is harmless.
    for name, clock in [
        ('trajectory_time_precision.gsv', [0., 1e9, 1e9 + 1]),
        ('trajectory_epoch_offset.gsv', [1.7e9, 1.7e9 + 1, 1.7e9 + 2]),
    ]:
        clock_frames = []
        for sample in range(3):
            frame = records(1)
            frame[0, :3] = [sample * .1, 0, 3]
            frame[0, 12:15] = [.2, .4, .8]
            clock_frames.append(frame)
        with Writer(target / name, overwrite=True,
                    metadata={'purpose': 'engineering trajectory time precision fixture'}) as out:
            add_persistent_gaussians(out, 'clock-track', np.array([37], dtype=np.int64),
                                     np.asarray(clock, dtype=np.float64), clock_frames)

    n, steps = 96, 48
    times = np.arange(steps, dtype=np.float64) / 24
    frames = []
    for time in times:
        frame = records(n)
        angle = np.arange(n) * 2 * np.pi / n
        frame[:, 0] = np.cos(angle) * (1 + .25 * np.sin(time * 3 + angle))
        frame[:, 1] = np.sin(angle) * (1 + .25 * np.cos(time * 3 + angle))
        frame[:, 2] = .5 * np.sin(time * 2 + angle * 2)
        frame[:, 12:15] = .1 + .8 * np.stack([
            .5 + .5 * np.sin(angle), .5 + .5 * np.sin(angle + 2.1),
            .5 + .5 * np.sin(angle + 4.2)], axis=1)
        frames.append(frame)
    k = np.array([[550., 0., 320.], [0., 550., 240.], [0., 0., 1.]])
    with Writer(target / 'trajectory_preview.gsv', overwrite=True,
                metadata={'purpose': 'engineering interaction fixture'}) as out:
        add_persistent_gaussians(out, 'colored-ring', np.arange(n, dtype=np.int64), times,
                                 frames, label='Colored motion field')
        for index in range(2):
            poses = np.tile(np.eye(4), (steps, 1, 1, 1))
            poses[:, 0, 0, 3] = (-1 if index == 0 else 1) + .3 * np.sin(times * 2)
            poses[:, 0, 1, 3] = .3 * np.cos(times * 2)
            poses[:, 0, 2, 3] = -3
            add_camera(out, f'camera-{index}', times, np.tile(k, (steps, 1, 1, 1)),
                       poses, [[640, 480]], label=f'Animated camera {index + 1}')


if __name__ == '__main__':
    main()
