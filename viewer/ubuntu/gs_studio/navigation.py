"""Pure camera/navigation mathematics, in GSV right/down/forward coordinates."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import numpy as np


def rotation(axis, angle):
    axis = np.asarray(axis, dtype=np.float64)
    axis = axis / max(float(np.linalg.norm(axis)), 1e-15)
    x, y, z = axis
    cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + math.sin(angle) * cross + (1 - math.cos(angle)) * (cross @ cross)


def operation(button, *, alt=False, shift=False, control=False, tool="select"):
    """GTK buttons are 1=left, 2=middle, 3=right; matches the macOS policy."""
    if alt:
        if button == 3 or control:
            return "dolly"
        if button == 2 or shift:
            return "pan"
        return "orbit"
    if button == 2:
        return "pan" if shift else "orbit"
    if button == 3 or shift:
        return "pan"
    if control:
        return "dolly"
    return tool


def trail_start(time, times, frames=8):
    times = np.asarray(times, dtype=np.float64)
    if not len(times) or frames <= 0:
        return float(time)
    lower = max(0, int(np.searchsorted(times, time, side="right")) - 1)
    if lower < frames:
        return float(times[0])
    start = lower - frames
    if lower + 1 == len(times):
        return float(times[start])
    # Scale differences for wide zero-crossing clocks, as in the SDK evaluator.
    a, b, t = float(times[lower]), float(times[lower + 1]), float(time)
    span = b - a
    phase = (t - a) / span if math.isfinite(span) else (t * .5 - a * .5) / (b * .5 - a * .5)
    phase = min(1., max(0., phase))
    return float(times[start] * (1 - phase) + times[start + 1] * phase)


@dataclass
class Navigation:
    c2w: np.ndarray = field(default_factory=lambda: np.array([[1., 0, 0, 0], [0, -1., 0, 0], [0, 0, -1., 3], [0, 0, 0, 1.]]))
    pivot: np.ndarray = field(default_factory=lambda: np.zeros(3))
    up: np.ndarray = field(default_factory=lambda: np.array([0., 1., 0.]))
    fov: float = 50.
    mode: str = "turntable"

    def intrinsics(self, width, height):
        f = .5 * max(1, height) / math.tan(math.radians(self.fov) / 2)
        return np.array([[f, 0., width / 2], [0., f, height / 2], [0., 0., 1.]])

    def rotate(self, axis, angle):
        r = rotation(axis, angle)
        self.c2w[:3, :3] = r @ self.c2w[:3, :3]
        self.c2w[:3, 3] = self.pivot + r @ (self.c2w[:3, 3] - self.pivot)
        # Remove numerical drift without changing the camera position.
        u, _, vh = np.linalg.svd(self.c2w[:3, :3])
        self.c2w[:3, :3] = u @ vh

    def orbit(self, dx, dy):
        if self.mode == "trackball":
            axis = self.c2w[:3, 1] * -dx + self.c2w[:3, 0] * dy
            length = float(np.linalg.norm(axis))
            if length > 1e-12:
                self.rotate(axis, length * .006)
            return
        self.rotate(self.up, -dx * .006)
        forward = self.c2w[:3, 2]
        elevation = math.asin(float(np.clip(forward @ self.up, -1., 1.)))
        limit = math.pi / 2 - .0087
        target = min(max(limit, elevation), max(min(-limit, elevation), elevation + dy * .006))
        axis = np.cross(forward, self.up)
        if np.linalg.norm(axis) < 1e-8:
            axis = self.c2w[:3, 0] - self.up * (self.c2w[:3, 0] @ self.up)
        self.rotate(axis, target - elevation)

    def pan(self, dx, dy, k):
        depth = max(float((self.pivot - self.c2w[:3, 3]) @ self.c2w[:3, 2]), 1e-4)
        y = dy / k[1, 1]
        x = (dx - k[0, 1] * y) / k[0, 0]
        shift = -(self.c2w[:3, 0] * x + self.c2w[:3, 1] * y) * depth
        self.c2w[:3, 3] += shift
        self.pivot += shift

    def dolly(self, delta):
        offset = self.c2w[:3, 3] - self.pivot
        distance = float(np.linalg.norm(offset))
        if distance <= 1e-12:
            offset = -self.c2w[:3, 2]
            distance = 1.
        target = float(np.clip(distance * math.exp(float(np.clip(delta * .08, -20, 20))), 1e-6, 1e12))
        self.c2w[:3, 3] = self.pivot + offset * (target / distance)

    def frame(self, points, width, height):
        points = np.asarray(points)
        if not len(points):
            return
        points = points[np.isfinite(points).all(axis=1)]
        if not len(points):
            return
        low, high = points.min(axis=0), points.max(axis=0)
        self.pivot = low * .5 + high * .5
        radius = max(float(np.linalg.norm(high - low)) * .5, 1e-3)
        half_y = math.radians(self.fov) * .5
        half_x = math.atan(math.tan(half_y) * max(1, width) / max(1, height))
        distance = radius / max(math.sin(min(half_x, half_y)), 1e-4) * 1.15
        self.c2w[:3, 3] = self.pivot - self.c2w[:3, 2] * distance
