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


def world_basis(up):
    up=np.asarray(up,np.float64)
    back=np.array([0.,-1.,0.]) if abs(up[2])>.9 else np.array([0.,0.,1.])
    right=np.cross(up,back);right/=np.linalg.norm(right)
    return np.stack([right,up,np.cross(right,up)],axis=1)


def viewing_rotation(back,up):
    back=np.asarray(back,np.float64);back/=np.linalg.norm(back)
    forward=-back
    right=np.cross(forward,up);right/=np.linalg.norm(right)
    down=np.cross(forward,right)
    return np.stack([right,down,forward],axis=1)


def default_pose():
    direction=np.array([math.sin(math.pi/5)*math.cos(math.pi/9),math.sin(math.pi/9),math.cos(math.pi/5)*math.cos(math.pi/9)])
    pose=np.eye(4);pose[:3,:3]=viewing_rotation(direction,np.array([0.,1.,0.]));pose[:3,3]=direction*4
    return pose


@dataclass
class Navigation:
    c2w: np.ndarray = field(default_factory=default_pose)
    pivot: np.ndarray = field(default_factory=lambda: np.zeros(3))
    up: np.ndarray = field(default_factory=lambda: np.array([0., 1., 0.]))
    fov: float = 360. / 7.
    mode: str = "turntable"

    def intrinsics(self, width, height):
        f = .5 * max(1, min(width, height)) / math.tan(math.radians(self.fov) / 2)
        return np.array([[f, 0., width / 2], [0., f, height / 2], [0., 0., 1.]])

    def rotate(self, axis, angle, *, pivot=None):
        if not np.isfinite(angle):
            return
        center = self.pivot if pivot is None else np.asarray(pivot, np.float64)
        r = rotation(axis, angle)
        self.c2w[:3, :3] = r @ self.c2w[:3, :3]
        self.c2w[:3, 3] = center + r @ (self.c2w[:3, 3] - center)
        # Remove numerical drift without changing the camera position.
        u, _, vh = np.linalg.svd(self.c2w[:3, :3])
        self.c2w[:3, :3] = u @ vh

    def orbit(self, dx, dy):
        if not np.isfinite([dx, dy]).all():
            return
        if self.mode == "trackball":
            # Match native camera-local yaw then pitch, preserving authored roll.
            right, down = self.c2w[:3, 0].copy(), self.c2w[:3, 1].copy()
            r = rotation(right, dy * .006) @ rotation(down, dx * .006)
            self.c2w[:3, :3] = r @ self.c2w[:3, :3]
            self.c2w[:3, 3] = self.pivot + r @ (self.c2w[:3, 3] - self.pivot)
            u, _, vh = np.linalg.svd(self.c2w[:3, :3])
            self.c2w[:3, :3] = u @ vh
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
        if not np.isfinite([dx,dy]).all():
            return
        depth = max(float((self.pivot - self.c2w[:3, 3]) @ self.c2w[:3, 2]), 1e-4)
        y = dy / k[1, 1]
        x = (dx - k[0, 1] * y) / k[0, 0]
        shift = -(self.c2w[:3, 0] * x + self.c2w[:3, 1] * y) * depth
        self.c2w[:3, 3] += shift
        self.pivot += shift

    def point_on_pivot_plane(self, pixel, k):
        """Unproject a GTK pixel onto the existing pivot plane, including skew."""
        origin = self.c2w[:3, 3]
        depth = max(float((self.pivot - origin) @ self.c2w[:3, 2]), 1e-4)
        y = (pixel[1] - k[1, 2]) / k[1, 1]
        x = (pixel[0] - k[0, 2] - k[0, 1] * y) / k[0, 0]
        return origin + (self.c2w[:3, 0] * x + self.c2w[:3, 1] * y + self.c2w[:3, 2]) * depth

    def dolly(self, delta, *, pixel=None, k=None):
        if not np.isfinite(delta) or delta == 0:
            return
        origin = self.c2w[:3, 3].copy()
        offset = origin - self.pivot
        distance = max(float(np.linalg.norm(offset)), 1e-4)
        target = float(np.clip(distance * math.exp(float(np.clip(delta * .025, -20, 20))), 1e-4, 1e8))
        ratio = target / distance
        anchor = self.pivot.copy()
        if pixel is not None and k is not None and np.isfinite(pixel).all():
            anchor = self.point_on_pivot_plane(pixel, k)
        self.c2w[:3, 3] = anchor + (origin - anchor) * ratio
        self.pivot = anchor + (self.pivot - anchor) * ratio

    def roll(self, radians):
        # Camera roll rotates about the optical center, never around scene depth.
        self.rotate(self.c2w[:3, 2], radians, pivot=self.c2w[:3, 3].copy())

    def focus(self,center,radius,width,height,k=None):
        center=np.asarray(center,np.float64)
        if not np.isfinite(center).all() or not math.isfinite(radius):
            return
        k=self.intrinsics(width,height) if k is None else k
        short_fov=min(2*math.atan(width/(2*k[0,0])),2*math.atan(height/(2*k[1,1])))
        distance=max(.02,max(radius,.001)*1.12/max(.01,math.sin(short_fov*.5)))
        self.pivot=center.copy()
        self.c2w[:3,3]=self.pivot-self.c2w[:3,2]*distance

    def reset(self,center,radius):
        direction=world_basis(self.up)@np.array([math.sin(math.pi/5)*math.cos(math.pi/9),math.sin(math.pi/9),math.cos(math.pi/5)*math.cos(math.pi/9)])
        self.pivot=np.asarray(center,np.float64).copy()
        self.c2w[:3,:3]=viewing_rotation(direction,self.up)
        self.c2w[:3,3]=self.pivot+direction*max(float(radius)*2.8,.05)
        self.fov=360./7.

    def preset(self,name):
        local={'front':(0,0,1),'back':(0,0,-1),'right':(1,0,0),'left':(-1,0,0),'isometric':(1,1,1),
               'top':(0,math.cos(.0001),math.sin(.0001)), 'bottom':(0,-math.cos(.0001),math.sin(.0001))}
        if name not in local:
            raise ValueError('Unknown viewpoint')
        distance=max(float(np.linalg.norm(self.c2w[:3,3]-self.pivot)),1e-4)
        back=world_basis(self.up)@np.asarray(local[name],np.float64);back/=np.linalg.norm(back)
        self.c2w[:3,:3]=viewing_rotation(back,self.up)
        self.c2w[:3,3]=self.pivot+back*distance
        self.fov=360./7.

    def frame(self,points,width,height):
        points=np.asarray(points)
        if not len(points):return
        points=points[np.isfinite(points).all(axis=1)]
        if not len(points):return
        low,high=points.min(axis=0),points.max(axis=0)
        self.focus(low*.5+high*.5,max(float(np.linalg.norm(high-low))*.5,1e-3),width,height)
