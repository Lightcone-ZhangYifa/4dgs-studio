"""Calibrated camera interpolation and portable camera-only authoring files."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import OrderedDict
import copy
import math
import os
from pathlib import Path
import stat
import uuid

import numpy as np

from gsv import GSVError, Scene, Writer, add_camera
from gsv.codecs import validate_object
from gsv.evaluate import _time_weight, matrix_quat, quat_matrix
from gsv.container import SHA_RE, DTYPES


class SnapshotScene(Scene):
    """SDK validation with owned pread snapshots instead of faultable mappings.

    A trainer truncating its source file cannot SIGBUS a GTK/GPU reader. The
    original SDK remains authoritative for the envelope, hashes, Boolean
    canonicalization and object semantics. Only typed-array storage differs.
    """
    snapshot_budget = 64 * 1024**2

    def __init__(self, *args, **kwargs):
        self._snapshots = OrderedDict()
        self._snapshot_bytes = 0
        self._position_bounds = {}
        self._position_keys = set()
        super().__init__(*args, **kwargs)
        self._position_keys = {o['data'].get('positions') for o in self.objects
                               if o['encoding'] == 'gsv.scaffold.graph'
                               and isinstance(o['data'].get('positions'), str)}

    def _array_info(self, key):
        self._unchanged()
        a = self.manifest['arrays'][key]
        b = self.manifest['buffers'][a['buffer']]
        dtype = np.dtype(DTYPES[a['dtype']])
        if math.prod(a['shape']) * dtype.itemsize > self.max_array_bytes:
            raise GSVError('Array exceeds configured access limit')
        if self.verify:
            self.verify_buffer(a['buffer'])
        return a, b['offset'] + a.get('byte_offset', 0), dtype

    def _read_into(self, destination, position, cancelled=None):
        self._unchanged()
        if destination.nbytes:
            raw = memoryview(destination).cast('B')
            done = 0
            while done < len(raw):
                if cancelled is not None and cancelled():
                    raise InterruptedError('Scene request was superseded')
                chunk = raw[done:done+8*1024**2]
                count = os.preadv(self._file.fileno(), [chunk], position+done)
                if count <= 0:
                    raise GSVError('Source array was truncated during snapshot read')
                done += count
        self._unchanged()

    def array(self, key, *, cancelled=None):
        self._unchanged()
        if key in self._snapshots:
            self._snapshots.move_to_end(key)
            return self._snapshots[key]
        a, position, dtype = self._array_info(key)
        value = np.empty(a['shape'], dtype=dtype)
        self._read_into(value, position, cancelled)
        if dtype.kind == 'b' and np.any(value.view(np.uint8) > 1):
            raise GSVError('Boolean arrays require canonical 0/1 bytes')
        if (key in self._position_keys and key not in self._position_bounds and value.size
                and value.ndim == 3 and value.shape[-1] == 3):
            points = value.reshape(-1, 3)
            self._position_bounds[key] = (points.min(axis=0).astype(np.float64),
                                          points.max(axis=0).astype(np.float64))
        value.flags.writeable = False
        if value.nbytes <= self.snapshot_budget:
            while self._snapshots and self._snapshot_bytes + value.nbytes > self.snapshot_budget:
                _, old = self._snapshots.popitem(last=False)
                self._snapshot_bytes -= old.nbytes
            self._snapshots[key] = value
            self._snapshot_bytes += value.nbytes
        return value

    def array_rows(self, key, rows, *, prefix=(), cancelled=None):
        """Read selected contiguous row runs without mapping/copying the field."""
        a, position, dtype = self._array_info(key)
        shape = a['shape']
        if len(prefix) >= len(shape):
            raise GSVError('Row snapshot requires a remaining array axis')
        for axis, index in enumerate(prefix):
            if not 0 <= index < shape[axis]:
                raise GSVError('Array prefix index is out of range')
            position += int(index) * math.prod(shape[axis+1:]) * dtype.itemsize
        rows = np.asarray(rows, dtype=np.int64)
        count = shape[len(prefix)]
        if (rows.ndim != 1 or np.any(rows < 0) or np.any(rows >= count)
                or np.any(rows[1:] <= rows[:-1])):
            raise GSVError('Row snapshots require sorted unique in-range indices')
        tail = shape[len(prefix)+1:]
        out = np.empty((len(rows), *tail), dtype=dtype)
        row_bytes = math.prod(tail) * dtype.itemsize
        boundaries = np.r_[0, np.flatnonzero(np.diff(rows) != 1)+1, len(rows)]
        for start, stop in zip(boundaries[:-1], boundaries[1:]):
            if start == stop:
                continue
            self._read_into(out[start:stop], position+int(rows[start])*row_bytes, cancelled)
        if dtype.kind == 'b' and np.any(out.view(np.uint8) > 1):
            raise GSVError('Boolean arrays require canonical 0/1 bytes')
        out.flags.writeable = False
        return out

    def position_bounds(self, key):
        self._unchanged()
        if key not in self._position_bounds:
            self.array(key)
        return self._position_bounds.get(key, (np.full(3, -1.), np.full(3, 1.)))

    def close(self):
        self._snapshots.clear()
        self._snapshot_bytes = 0
        self._position_bounds.clear()
        if hasattr(self, '_file'):
            super().close()


def open_regular_scene(path, *, factory=SnapshotScene):
    """Pin a checked regular descriptor so FIFO paths and replacement cannot hang."""
    path = Path(path).resolve()
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise GSVError('GSV input must be a regular file')
        scene = factory(Path('/proc/self/fd') / str(fd))
        scene.path = path
        return scene
    finally:
        os.close(fd)


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
            or not np.allclose(k[1:, 0], 0, atol=1e-9, rtol=0)
            or not np.allclose(k[2], [0, 0, 1], atol=1e-9, rtol=0)
            or not np.allclose(pose[3], [0, 0, 0, 1], atol=1e-9, rtol=0)):
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


@dataclass(eq=False)
class CameraTrack:
    label: str
    space: str
    clock: str
    width: int
    height: int
    keyframes: list[CameraKeyframe] = field(default_factory=list)
    id: str = field(default_factory=lambda: 'camera-' + str(uuid.uuid4()))
    spaces: dict = field(default_factory=dict, repr=False)
    clocks: dict = field(default_factory=dict, repr=False)
    _times_source: object = field(default=None, init=False, repr=False)
    _times_cache: np.ndarray | None = field(default=None, init=False, repr=False)

    @property
    def times(self):
        """Read-only native key times, reused until an edit replaces the list."""
        if self._times_source is not self.keyframes:
            times = np.fromiter((key.time for key in self.keyframes), np.float64, count=len(self.keyframes))
            times.flags.writeable = False
            self._times_source, self._times_cache = self.keyframes, times
        return self._times_cache

    def __eq__(self, other):
        # A camera's stable identity is independent of its editable name/pose.
        # Dataclass array equality also makes list.index fail for same-named
        # cameras before reaching the actual selected track.
        return isinstance(other, CameraTrack) and self.id == other.id

    def clone(self):
        """Copy editable structure while sharing immutable keyframe arrays."""
        out = copy.copy(self)
        out.keyframes = []
        out._times_source = out._times_cache = None
        for key in self.keyframes:
            if key.K.flags.writeable or key.c2w.flags.writeable:
                k, pose = np.array(key.K, copy=True), np.array(key.c2w, copy=True)
                k.flags.writeable = pose.flags.writeable = False
                key = CameraKeyframe(key.time, k, pose, key.near, key.far)
            out.keyframes.append(key)
        out.spaces, out.clocks = copy.deepcopy(self.spaces), copy.deepcopy(self.clocks)
        return out

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
        try:
            with np.errstate(over='raise', invalid='raise'):
                k[0] *= self.width / width
                k[1] *= self.height / height
        except FloatingPointError as error:
            raise GSVError('Scaled camera intrinsics exceed the authoring numeric range') from error
        _validate_calibration(k, pose, self.width, self.height, near, far)
        pose[3] = [0, 0, 0, 1]
        k.flags.writeable = pose.flags.writeable = False
        key = CameraKeyframe(time, k, pose, float(near), None if far is None else float(far))
        self.keyframes = sorted([f for f in self.keyframes if f.time != time] + [key], key=lambda f: f.time)

    def at(self, time):
        if not self.keyframes:
            raise GSVError('Camera has no keyframes')
        if not np.isfinite(time):
            raise GSVError('Time must be finite')
        times = self.times
        b = min(int(np.searchsorted(times, time)), len(times) - 1)
        a = max(0, b - 1)
        weight = _time_weight(time, times[a], times[b]) if a != b else 0
        first, second = self.keyframes[a], self.keyframes[b]
        k = (1 - weight) * first.K + weight * second.K
        pose = interpolate_pose(first.c2w, second.c2w, weight)
        near = (1 - weight) * first.near + weight * second.near
        far = (first.far if weight == 0 else second.far if weight == 1 else None
               if first.far is None or second.far is None else (1 - weight) * first.far + weight * second.far)
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
        if type(limit) is not int or limit <= 0:
            raise GSVError('Camera history limit must be a positive integer')
        self.tracks = list(tracks or [])
        self._undo, self._redo = [], []
        self.limit = limit

    def remember(self):
        self._undo.append([track.clone() for track in self.tracks])
        self._undo = self._undo[-self.limit:]
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return False
        self._redo.append([track.clone() for track in self.tracks])
        self.tracks = self._undo.pop()
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append([track.clone() for track in self.tracks])
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
    valid_inherited = (isinstance(inherited, dict) and isinstance(inherited.get('manifest_sha256'), str)
                       and SHA_RE.fullmatch(inherited['manifest_sha256']))
    source_document = inherited if source.objects and all(o['type'] == 'camera' for o in source.objects) and valid_inherited else {
        'name': source.path.name, 'manifest_sha256': source.manifest_sha256}
    with Writer(path, overwrite=overwrite, metadata={
        'created_by': '4DGS Studio', 'source_document': source_document,
        'purpose': 'Authored cameras; reconstruction source remains unchanged.',
    }) as out:
        out.manifest['spaces'] = copy.deepcopy(source.manifest.get('spaces', {}))
        out.manifest['clocks'] = copy.deepcopy(source.manifest.get('clocks', {}))
        for track in tracks:
            for field in ('spaces', 'clocks'):
                for key, descriptor in getattr(track, field).items():
                    existing = out.manifest[field].get(key)
                    if existing is not None and existing != descriptor:
                        raise GSVError('Authored camera ' + field + ' descriptor disagrees with the source document')
                    out.manifest[field][key] = copy.deepcopy(descriptor)
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


def import_camera_animation(path, source, existing_ids=(), *, cancelled=None):
    """Validate every camera before returning any authored data to the caller."""
    def check_cancelled():
        if cancelled is not None and cancelled():
            raise InterruptedError('Camera import was superseded')
    check_cancelled()
    with open_regular_scene(path) as incoming:
        objects = incoming.objects
        if not objects or any(o['encoding'] != 'gsv.camera.pinhole' or o['type'] != 'camera' for o in objects):
            raise GSVError('Import a camera-only animation GSV created for this source scene')
        provenance = incoming.manifest.get('metadata', {}).get('source_document')
        digest = provenance.get('manifest_sha256') if isinstance(provenance, dict) else None
        if digest != source.manifest_sha256:
            raise GSVError('Camera animation requires the matching source manifest digest')
        used = set(existing_ids) | {o['id'] for o in source.objects}
        tracks = []
        for obj in objects:
            check_cancelled()
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
            track.spaces = {obj['space']: copy.deepcopy(incoming.manifest['spaces'][obj['space']])}
            clock = obj['clock']
            while clock not in track.clocks:
                descriptor = incoming.manifest['clocks'][clock]
                existing = source.manifest.get('clocks', {}).get(clock)
                if existing is not None and existing != descriptor:
                    raise GSVError('Imported clocks descriptor disagrees with the source document')
                track.clocks[clock] = copy.deepcopy(descriptor)
                if 'mapping' not in descriptor:
                    break
                clock = descriptor['mapping']['target']
            times = incoming.array(data['times'])
            k = incoming.array(projection['intrinsics'])
            poses = incoming.array(data['pose']['camera_to_world'])
            volume = data.get('view_volume')
            near_values = incoming.array(volume['near']) if volume else None
            far_values = incoming.array(volume['far']) if volume else None
            unbounded = incoming.array(volume['far_unbounded']) if volume else None
            frames = []
            for i, time in enumerate(times):
                if i % 128 == 0:
                    check_cancelled()
                native_time = float(time)
                if not np.isfinite(native_time):
                    raise GSVError('Camera key time must be finite')
                ki = np.array(k[i, 0], np.float64, copy=True)
                pose = np.array(poses[i, 0], np.float64, copy=True)
                if projection['pixel_center'] == 'integer':
                    ki[:2, 2] += .5
                near, far = .001, None
                if volume:
                    near = float(near_values[i, 0])
                    if not unbounded[i, 0]:
                        far = float(far_values[i, 0])
                # Fixed source/target raster makes record()'s row scaling an
                # identity. Keep its numerical checks and affine normalization,
                # then publish one ordered list instead of sorting every prefix.
                _validate_calibration(ki,pose,width,height,near,far)
                pose[3] = [0,0,0,1]
                ki.flags.writeable = pose.flags.writeable = False
                frames.append(CameraKeyframe(native_time,ki,pose,float(near),far))
            check_cancelled()
            incoming._unchanged()
            track.keyframes = frames
            tracks.append(track)
        return tracks
