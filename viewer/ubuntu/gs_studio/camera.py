"""Calibrated camera interpolation and portable camera-only authoring files."""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
from pathlib import Path
import uuid

import numpy as np

from gsv import GSVError, Scene, Writer, add_camera
from gsv.codecs import validate_object
from gsv.evaluate import _time_weight, matrix_quat, quat_matrix


@dataclass(frozen=True)
class Calibration:
    K: np.ndarray
    c2w: np.ndarray
    width: int
    height: int
    pixel_center: str = 'half'
    distortion: str = 'none'
    near: float = .001
    far: float | None = None
    details: dict = field(default_factory=dict)


def slerp(first, second, weight):
    """Vectorized shortest-arc unit quaternion interpolation in wxyz order."""
    first, second = np.asarray(first, np.float64), np.asarray(second, np.float64)
    first = first / np.linalg.norm(first, axis=-1, keepdims=True)
    second = second / np.linalg.norm(second, axis=-1, keepdims=True)
    dot = np.sum(first * second, axis=-1, keepdims=True)
    second = np.where(dot < 0, -second, second)
    dot = np.clip(np.abs(dot), 0, 1)
    angle = np.arccos(dot)
    denominator = np.sin(angle)
    linear = dot > .9995
    safe = np.where(linear, 1., denominator)
    result = (first * (np.sin((1 - weight) * angle) / safe)
              + second * (np.sin(weight * angle) / safe))
    result = np.where(linear, (1 - weight) * first + weight * second, result)
    return result / np.linalg.norm(result, axis=-1, keepdims=True)


def interpolate_pose(first, second, weight):
    out = np.eye(4, dtype=np.float64)
    out[:3, :3] = quat_matrix(slerp(matrix_quat(first[:3, :3]), matrix_quat(second[:3, :3]), weight))
    out[:3, 3] = (1 - weight) * first[:3, 3] + weight * second[:3, 3]
    return out


def _validate_calibration(k, pose, width, height, near, far):
    if (np.shape(k) != (3, 3) or np.shape(pose) != (4, 4)
            or not np.isfinite(k).all() or not np.isfinite(pose).all()
            or k[0, 0] <= 0 or k[1, 1] <= 0
            or not np.allclose(k[1:, 0], 0, atol=1e-9)
            or not np.allclose(k[2], [0, 0, 1], atol=1e-9)
            or not np.allclose(pose[3], [0, 0, 0, 1], atol=1e-9)):
        raise GSVError('Camera requires finite pinhole intrinsics and an affine pose')
    rotation = pose[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-4) or not np.isclose(np.linalg.det(rotation), 1, atol=1e-4):
        raise GSVError('Camera rotation must be proper and orthonormal')
    if not (isinstance(width, (int, np.integer)) and isinstance(height, (int, np.integer))
            and 0 < width < 2**31 and 0 < height < 2**31):
        raise GSVError('Camera image size requires positive int32 dimensions')
    if not np.isfinite(near) or near < 0 or (far is not None and (not np.isfinite(far) or far <= near)):
        raise GSVError('Camera view volume requires a finite near and a greater far or no far')


@dataclass(frozen=True)
class CameraKeyframe:
    time: float
    K: np.ndarray
    c2w: np.ndarray
    near: float = .001
    far: float | None = None


@dataclass
class CameraTrack:
    label: str
    space: str
    clock: str
    width: int
    height: int
    keyframes: list[CameraKeyframe] = field(default_factory=list)
    id: str = field(default_factory=lambda: 'camera-' + str(uuid.uuid4()))

    def record(self, time, K, c2w, width=None, height=None, near=.001, far=None):
        time = float(time)
        if not np.isfinite(time):
            raise GSVError('Camera key time must be finite')
        k, pose = np.array(K, np.float64, copy=True), np.array(c2w, np.float64, copy=True)
        width = self.width if width is None else width
        height = self.height if height is None else height
        _validate_calibration(k, pose, width, height, near, far)
        _validate_calibration(k, pose, self.width, self.height, near, far)
        # Viewport captures use half pixel coordinates. Scale whole rows,
        # including skew and principal point, into this camera's fixed raster.
        k[0] *= self.width / width
        k[1] *= self.height / height
        pose[3] = [0, 0, 0, 1]
        k.flags.writeable = pose.flags.writeable = False
        key = CameraKeyframe(time, k, pose, float(near), None if far is None else float(far))
        self.keyframes = sorted([f for f in self.keyframes if f.time != time] + [key], key=lambda f: f.time)

    def at(self, time):
        if not self.keyframes:
            raise GSVError('Camera has no keyframes')
        if not np.isfinite(time):
            raise GSVError('Time must be finite')
        times = np.array([f.time for f in self.keyframes], np.float64)
        b = min(int(np.searchsorted(times, time)), len(times) - 1)
        a = max(0, b - 1)
        weight = _time_weight(time, times[a], times[b]) if a != b else 0
        first, second = self.keyframes[a], self.keyframes[b]
        k = (1 - weight) * first.K + weight * second.K
        pose = interpolate_pose(first.c2w, second.c2w, weight)
        near = (1 - weight) * first.near + weight * second.near
        far = None if first.far is None or second.far is None else (1 - weight) * first.far + weight * second.far
        return Calibration(k, pose, self.width, self.height, near=near, far=far)

    def retime(self, index, new_time):
        new_time = float(new_time)
        if not np.isfinite(new_time):
            raise GSVError('Camera key time must be finite')
        if not 0 <= index < len(self.keyframes):
            raise GSVError('Camera key index is out of range')
        if any(i != index and f.time == new_time for i, f in enumerate(self.keyframes)):
            raise GSVError('A camera key already exists at this time')
        old = self.keyframes[index]
        frames = list(self.keyframes)
        frames[index] = CameraKeyframe(new_time, old.K, old.c2w, old.near, old.far)
        self.keyframes = sorted(frames, key=lambda f: f.time)

    def remove(self, index):
        if not 0 <= index < len(self.keyframes):
            raise GSVError('Camera key index is out of range')
        if len(self.keyframes) <= 1:
            raise GSVError('A camera must retain at least one keyframe')
        self.keyframes = self.keyframes[:index] + self.keyframes[index + 1:]


class CameraHistory:
    """Bounded snapshots of authored cameras, independent of source document data."""
    def __init__(self, tracks=None, limit=100):
        self.tracks = list(tracks or [])
        self._undo, self._redo = [], []
        self.limit = limit

    def remember(self):
        self._undo.append(copy.deepcopy(self.tracks))
        self._undo = self._undo[-self.limit:]
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return False
        self._redo.append(copy.deepcopy(self.tracks))
        self.tracks = self._undo.pop()
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append(copy.deepcopy(self.tracks))
        self.tracks = self._redo.pop()
        return True


def export_camera_animation(path, tracks, source, *, overwrite=False):
    """Atomic SDK writer; camera files interoperate with the macOS authoring UI."""
    path = Path(path).resolve()
    if path == source.path.resolve():
        raise GSVError('Save camera animation to a separate file; the reconstruction cannot be overwritten')
    # Comparing resolved paths covers symlinks; samefile also covers hard links.
    if path.exists() and path.samefile(source.path):
        raise GSVError('Camera export cannot overwrite the source reconstruction')
    if not tracks:
        raise GSVError('There are no authored cameras to save')
    ids = [t.id for t in tracks]
    if len(set(ids)) != len(ids):
        raise GSVError('Camera identities must be unique')
    inherited = source.manifest.get('metadata', {}).get('source_document')
    source_document = inherited if source.objects and all(o['type'] == 'camera' for o in source.objects) and inherited else {
        'name': source.path.name, 'manifest_sha256': source.manifest_sha256}
    with Writer(path, overwrite=overwrite, metadata={
        'created_by': '4DGS Studio', 'source_document': source_document,
        'purpose': 'Authored cameras; reconstruction source remains unchanged.',
    }) as out:
        out.manifest['spaces'] = copy.deepcopy(source.manifest.get('spaces', {}))
        out.manifest['clocks'] = copy.deepcopy(source.manifest.get('clocks', {}))
        for track in tracks:
            if not track.keyframes:
                raise GSVError(f'Camera {track.label} has no keyframes')
            frames = sorted(track.keyframes, key=lambda f: f.time)
            for key in frames:
                _validate_calibration(key.K, key.c2w, track.width, track.height, key.near, key.far)
            add_camera(out, track.id, [f.time for f in frames],
                       np.stack([f.K for f in frames])[:, None],
                       np.stack([f.c2w for f in frames])[:, None],
                       [[track.width, track.height]], pixel_center='half',
                       space=track.space, clock=track.clock, label=track.label,
                       view_volume={
                           'near': np.array([[f.near] for f in frames], np.float64),
                           'far': np.array([[0 if f.far is None else f.far] for f in frames], np.float64),
                           'far_unbounded': np.array([[f.far is None] for f in frames], bool),
                       }, metadata={'role': 'user', 'created_by': '4DGS Studio',
                                    'interpolation': 'translation/K linear; rotation shortest-arc SLERP'})


def import_camera_animation(path, source, existing_ids=()):
    """Validate every camera before returning any authored data to the caller."""
    with Scene(path) as incoming:
        objects = incoming.objects
        if not objects or any(o['encoding'] != 'gsv.camera.pinhole' or o['type'] != 'camera' for o in objects):
            raise GSVError('Import a camera-only animation GSV created for this source scene')
        digest = incoming.manifest.get('metadata', {}).get('source_document', {}).get('manifest_sha256')
        if digest != source.manifest_sha256:
            raise GSVError('Camera animation requires the matching source manifest digest')
        used = set(existing_ids) | {o['id'] for o in source.objects}
        tracks = []
        for obj in objects:
            validate_object(incoming, obj)
            data, projection = obj['data'], obj['data']['projection']
            if (obj.get('metadata', {}).get('role') != 'user' or len(data['view_ids']) != 1
                    or projection['distortion']['model'] != 'none'
                    or data.get('optics') or data.get('acquisition') or projection.get('depth_mapping')):
                raise GSVError('Editable import requires one-view user cameras without capture optics or acquisition')
            if obj['id'] in used:
                raise GSVError('Camera identity already exists in this scene: ' + obj['id'])
            used.add(obj['id'])
            if ((source.space and obj['space'] != source.space)
                    or (source.clock and obj['clock'] != source.clock)):
                raise GSVError('Imported camera and scene spaces/clocks differ; explicit alignment is required')
            for field, key in [('spaces', obj['space']), ('clocks', obj['clock'])]:
                incoming_descriptor = incoming.manifest.get(field, {}).get(key)
                source_descriptor = source.manifest.get(field, {}).get(key)
                if incoming_descriptor is None or (source_descriptor is not None and incoming_descriptor != source_descriptor):
                    raise GSVError('Imported ' + field + ' descriptor disagrees with the source document')
            width, height = map(int, incoming.array(projection['image_sizes'])[0])
            track = CameraTrack(obj.get('label', obj['id']), obj['space'], obj['clock'], width, height, id=obj['id'])
            times = incoming.array(data['times'])
            k = incoming.array(projection['intrinsics'])
            poses = incoming.array(data['pose']['camera_to_world'])
            volume = data.get('view_volume')
            for i, time in enumerate(times):
                ki = np.array(k[i, 0], copy=True)
                if projection['pixel_center'] == 'integer':
                    ki[:2, 2] += .5
                near, far = .001, None
                if volume:
                    near = float(incoming.array(volume['near'])[i, 0])
                    if not incoming.array(volume['far_unbounded'])[i, 0]:
                        far = float(incoming.array(volume['far'])[i, 0])
                track.record(time, ki, poses[i, 0], near=near, far=far)
            tracks.append(track)
        return tracks
