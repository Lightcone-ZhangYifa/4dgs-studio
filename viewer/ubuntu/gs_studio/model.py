"""Immutable, SDK-backed scene access for the native desktop frontend.

All public operations serialize against close(), so an obsolete background job
cannot read a closed descriptor. Returned frames own their storage and remain
valid when their source document is replaced. Persistent states use the same
linear attributes / shortest-arc rotation interpolation as the macOS viewport;
an identity is visible between samples only when both endpoints are valid.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import os
import threading

import numpy as np

from gsv import GSVError, Scene
from gsv.codecs import RGB_GAUSSIAN_CODECS, validate_object
from gsv.container import HEADER
from gsv.evaluate import evaluate as evaluate_native, quat_matrix, _time_weight

from .camera import Calibration, interpolate_pose, slerp


@dataclass(frozen=True)
class Frame:
    means: np.ndarray
    covariance: np.ndarray
    colors: np.ndarray
    opacity: np.ndarray
    ids: np.ndarray
    object_ids: np.ndarray
    records: np.ndarray
    edges: np.ndarray = field(default_factory=lambda: np.empty((0, 2, 3), np.float32))

    @classmethod
    def empty(cls):
        return cls(np.empty((0, 3), np.float32), np.empty((0, 3, 3), np.float32),
                   np.empty((0, 3), np.float32), np.empty(0, np.float32),
                   np.empty(0, np.int64), np.empty(0, object), np.empty((0, 16), np.float32))

    def __len__(self):
        return len(self.ids)


@dataclass(frozen=True)
class Trajectory:
    positions: np.ndarray
    times: np.ndarray
    colors: np.ndarray
    identity: int | None
    object_id: str

    @property
    def color(self):
        return self.colors.mean(axis=0)


class SceneDocument:
    """One read-only GSV document; no reconstruction arrays are ever modified.

    The default viewport selects the first render view's explicit field set, or
    one compatible space/clock cohort. Other peer objects remain inspectable.
    No unknown code, deformation recipe or metadata entrypoint is executed.
    """
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._lock = threading.RLock()
        self._closed = False
        self.scene = Scene(self.path)
        try:
            self.manifest = self.scene.manifest
            self.objects = self.scene.objects
            self._objects = {o['id']: o for o in self.objects}
            self.manifest_sha256 = HEADER.unpack(os.pread(self.scene._file.fileno(), HEADER.size, 0))[4].hex()
            self.digest = self.manifest_sha256
            # Semantic validation is deliberately done on the loading worker.
            # It catches damaged later frames before playback starts.
            for obj in self.objects:
                validate_object(self.scene, obj)
            self.render_views = [o for o in self.objects if o['encoding'] == 'gsv.render.view']
            self.cameras = [o for o in self.objects if o['encoding'] == 'gsv.camera.pinhole']
            self.supported_ids = [o['id'] for o in self.objects if self.supports(o)]
            self.unsupported = [o for o in self.objects if o['type'] == 'gaussian' and not self.supports(o)]
            self._cache = OrderedDict()
            self.cache_budget = 256 * 1024**2
            self._cache_bytes = 0
            self.visible_ids = self._default_visible()
            basis = next((self._objects[x] for x in self.visible_ids
                          if self._objects[x].get('clock')), None)
            basis = basis or next((self._objects[x] for x in self.visible_ids), None)
            basis = basis or next(iter(self.cameras), None)
            self.space = (basis.get('space') if basis else None) or 'world'
            self.clock = (basis.get('clock') if basis else None) or 'timeline'
            self.times = self._timeline()
            self.clock_unit = self.manifest.get('clocks', {}).get(self.clock, {}).get('unit', 'second')
            self.authored_cameras = []
        except BaseException:
            self.scene.close()
            raise

    @staticmethod
    def supports(obj):
        return obj['encoding'] in RGB_GAUSSIAN_CODECS or obj['encoding'] == 'gsv.scaffold.graph'

    def _default_visible(self):
        if self.render_views:
            fields = self.render_views[0]['links']['fields']
            # A render binding must be explicit even when it cannot be drawn.
            # Do not silently substitute a different reconstruction.
            if all(x in self.supported_ids for x in fields):
                return list(fields)
            return []
        gaussians = [self._objects[x] for x in self.supported_ids
                     if self._objects[x]['type'] == 'gaussian']
        candidates = gaussians or [self._objects[x] for x in self.supported_ids]
        if not candidates:
            return []
        # Prefer a declared derived cache over its own renderable native source.
        # The original native peer remains available in the outliner.
        candidates = self._without_cached_sources(candidates)
        if not candidates:
            return []
        basis = candidates[0]
        return [o['id'] for o in candidates if o.get('space') == basis.get('space')
                and (o.get('clock') == basis.get('clock') or o.get('clock') is None
                     or o['encoding'] == 'gsv.gaussian.static')]

    def _without_cached_sources(self, candidates):
        source_ids = set()
        for obj in candidates:
            source = obj.get('links', {}).get('source')
            if (obj['encoding'] == 'gsv.gaussian.persistent-identity'
                    and isinstance(source, str) and source != obj['id']
                    and self.object(source)['type'] == 'gaussian'):
                source_ids.add(source)
        return [o for o in candidates if o['id'] not in source_ids]

    def _require_open(self):
        if self._closed:
            raise GSVError('Scene document is closed')
        self.scene._unchanged()

    @contextmanager
    def read_lock(self):
        """Hold source ownership across a multi-array calibrated capture."""
        with self._lock:
            self._require_open()
            yield self.scene
            self.scene._unchanged()

    def object(self, identifier):
        try:
            return self._objects[identifier]
        except KeyError as e:
            raise GSVError(f'Unknown object: {identifier}') from e

    def object_times(self, identifier):
        obj = self.object(identifier)
        data = obj['data']
        if 'times' in data:
            return np.array(self.scene.array(data['times']), dtype=np.float64)
        if obj['encoding'] == 'gsv.gaussian.se3-basis':
            motion = self.object(obj['links']['motion'])
            times = self.scene.array(motion['data']['times'])
            return np.asarray(self.scene.convert_time(times, motion['clock'], obj['clock']))
        # Analytic continuous-time models have no source sample clock. Reuse
        # explicit camera times, never infer a capture FPS or training interval.
        tracks = [self.scene.array(c['data']['times']) for c in self.cameras
                  if c.get('clock') == obj.get('clock')]
        if tracks and obj['encoding'] != 'gsv.gaussian.static':
            return np.unique(np.concatenate(tracks))
        return np.array([0.0], np.float64)

    @property
    def time_unit(self):
        return self.clock_unit

    def activate_domain(self, object_id):
        """Activate one explicitly selected peer's compatible geometry cohort.

        A render view activates its exact ordered field binding. Other peers
        activate geometry sharing their named domain, without applying any
        hidden spatial transform or undeclared clock conversion.
        """
        with self._lock:
            self._require_open()
            obj = self.object(object_id)
            space, clock = obj.get('space'), obj.get('clock')
            if obj['encoding'] == 'gsv.render.view':
                identifiers = list(obj['links']['fields'])
            elif self.supports(obj) or obj['encoding'] == 'gsv.camera.pinhole':
                identifiers = [x for x in self.supported_ids
                               if self.object(x).get('space') == space
                               and (self.object(x).get('clock') == clock
                                    or self.object(x)['encoding'] == 'gsv.gaussian.static')]
                if obj['type'] != 'scaffold' and any(self.object(x)['type'] == 'gaussian' for x in identifiers):
                    identifiers = [x for x in identifiers if self.object(x)['type'] == 'gaussian']
                identifiers = [o['id'] for o in self._without_cached_sources([self.object(x) for x in identifiers])]
            else:
                raise GSVError('This peer object has no viewport coordinate domain')
            self._check_domains(identifiers)
            self.visible_ids = identifiers
            self.space, self.clock = space or 'world', clock or 'timeline'
            self.clock_unit = self.manifest.get('clocks', {}).get(self.clock, {}).get('unit', 'second')
            self.times = self._timeline()
            return list(self.visible_ids)

    def _timeline(self):
        tracks = [self.object_times(x) for x in self.visible_ids
                  if self.object(x)['encoding'] != 'gsv.gaussian.static']
        tracks += [self.scene.array(o['data']['times']) for o in self.cameras
                   if o.get('clock') == self.clock]
        for o in self.objects:
            if o['encoding'] == 'gsv.observation.camera-rgb' and o.get('clock') == self.clock:
                tracks.append(self.scene.array(o['data']['samples']['times']))
        nonempty = [t for t in tracks if len(t)]
        return np.unique(np.concatenate(nonempty)) if nonempty else np.array([0.0], np.float64)

    def set_visible(self, identifiers):
        with self._lock:
            self._require_open()
            identifiers = list(dict.fromkeys(identifiers))
            basis = self._check_domains(identifiers)
            self.visible_ids = identifiers
            if basis:
                self.space = basis.get('space') or 'world'
                self.clock = basis.get('clock') or self.clock or 'timeline'
                self.clock_unit = self.manifest.get('clocks', {}).get(self.clock, {}).get('unit', 'second')
            self.times = self._timeline()

    def _check_domains(self, identifiers):
        objects = [self.object(x) for x in identifiers]
        for obj in objects:
            if not self.supports(obj):
                raise GSVError(f"No safe evaluator for {obj['encoding']}; inspect its source or declared display cache")
        basis = next((o for o in objects if o['encoding'] != 'gsv.gaussian.static'),
                     objects[0] if objects else None)
        if basis and any(o.get('space') != basis.get('space') or
                         (o['encoding'] != 'gsv.gaussian.static' and o.get('clock') != basis.get('clock'))
                         for o in objects):
            raise GSVError('Visible geometry requires matching coordinate spaces and clocks; explicit alignment is required')
        return basis

    def _records(self, obj, time):
        """Four-sample, byte-bounded LRU. Returned records never alias the file."""
        key = (obj['id'], 0.0 if obj['encoding'] == 'gsv.gaussian.static' else float(time))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        codec, data = obj['encoding'], obj['data']
        if codec in ('gsv.gaussian.persistent-identity', 'gsv.scaffold.graph'):
            times = self.scene.array(data['times'])
            n = len(self.scene.array(data['ids']))
            if not len(times):
                return np.empty((0, 16), np.float32)
            b = min(int(np.searchsorted(times, time)), len(times) - 1)
            a = max(0, b - 1)
            w = _time_weight(time, times[a], times[b]) if a != b else 0.0
            if time >= times[b]:
                a, w = b, 0.0

            def at(index):
                if codec == 'gsv.gaussian.persistent-identity':
                    return np.array(self.scene.array(data['frames'][index]), copy=True)
                positions = self.scene.array(data['positions'])
                row = np.zeros((n, 16), np.float32)
                row[:, :3] = positions[index]
                row[:, 3] = .95
                # Glyph dimensions are a display choice, independent of node
                # topology and source certainty/visibility/validity evidence.
                span = np.ptp(positions[index].astype(np.float64), axis=0) if n else np.ones(3)
                row[:, 4:7] = max(float(np.linalg.norm(span)) * .0015, .0001)
                row[:, 7] = 1
                for mask in ('visibility', 'validity'):
                    if mask in data:
                        row[:, 7] *= self.scene.array(data[mask])[index]
                row[:, 8] = 1
                row[:, 12:15] = [.3, .95, .6]
                row[:, 15] = -1
                return row

            first = at(a)
            if not w:
                records = first
            elif w == 1:
                records = at(b)
            else:
                second = at(b)
                records = (first.astype(np.float64) * (1 - w) + second.astype(np.float64) * w).astype(np.float32)
                records[:, 7] = (first[:, 7] > .5) & (second[:, 7] > .5)
                records[:, 8:12] = slerp(first[:, 8:12], second[:, 8:12], w)
                records[(first[:, 15] < 0) | (second[:, 15] < 0), 15] = -1
        else:
            records = evaluate_native(self.scene, obj['id'], time)
        records.flags.writeable = False
        # Cache at most four frames globally, with an explicit memory ceiling.
        if records.nbytes <= self.cache_budget:
            while self._cache and (len(self._cache) >= 4 or self._cache_bytes + records.nbytes > self.cache_budget):
                _, discarded = self._cache.popitem(last=False)
                self._cache_bytes -= discarded.nbytes
            self._cache[key] = records
            self._cache_bytes += records.nbytes
        return records

    def evaluate(self, time, object_ids=None):
        with self._lock:
            self._require_open()
            time = float(time)
            if not np.isfinite(time):
                raise GSVError('Time must be finite')
            identifiers = self.visible_ids if object_ids is None else list(object_ids)
            self._check_domains(identifiers)
            records, ids, owners, edges = [], [], [], []
            for identifier in identifiers:
                obj = self.object(identifier)
                packed = self._records(obj, time)
                valid = packed[:, 7] > .5
                if obj['encoding'] == 'gsv.scaffold.graph' and 'edges' in obj['data']:
                    topology = np.asarray(self.scene.array(obj['data']['edges']), np.int64)
                    visible_edges = valid[topology].all(axis=1)
                    edges.append(packed[topology[visible_edges], :3].copy())
                records.append(packed[valid])
                ids.append(np.asarray(self.scene.array(obj['data']['ids']))[valid])
                owners.append(np.full(int(valid.sum()), identifier, dtype=object))
            self.scene._unchanged()
            if not records:
                return Frame.empty()
            packed = np.concatenate(records)
            rotation = quat_matrix(packed[:, 8:12]).astype(np.float32)
            scaled = rotation * packed[:, None, 4:7]
            cov = scaled @ scaled.swapaxes(-1, -2)
            frame = Frame(packed[:, :3].copy(), cov, packed[:, 12:15].copy(), packed[:, 3].copy(),
                          np.concatenate(ids), np.concatenate(owners), packed,
                          np.concatenate(edges) if edges else np.empty((0, 2, 3), np.float32))
            for array in vars(frame).values():
                array.flags.writeable = False
            return frame

    def camera(self, object_id, time, view=0):
        with self._lock:
            self._require_open()
            obj = self.object(object_id)
            if obj['encoding'] != 'gsv.camera.pinhole':
                raise GSVError('Object is not a calibrated camera')
            data = obj['data']
            times = self.scene.array(data['times'])
            projection = data['projection']
            sizes = self.scene.array(projection['image_sizes'])
            if not 0 <= view < len(sizes):
                raise GSVError('Camera view index is out of range')
            if not np.isfinite(time):
                raise GSVError('Time must be finite')
            b = min(int(np.searchsorted(times, time)), len(times) - 1)
            a = max(0, b - 1)
            weight = _time_weight(time, times[a], times[b]) if a != b else 0.0
            poses = self.scene.array(data['pose']['camera_to_world'])
            ks = self.scene.array(projection['intrinsics'])
            pose = interpolate_pose(poses[a, view], poses[b, view], weight)
            k = (1 - weight) * ks[a, view] + weight * ks[b, view]
            width, height = (int(x) for x in sizes[view])
            volume = data.get('view_volume')
            near, far = .001, None
            if volume:
                near_values = self.scene.array(volume['near'])
                near = float((1 - weight) * near_values[a, view] + weight * near_values[b, view])
                unbounded = self.scene.array(volume['far_unbounded'])
                if not (unbounded[a, view] or unbounded[b, view]):
                    values = self.scene.array(volume['far'])
                    far = float((1 - weight) * values[a, view] + weight * values[b, view])
            nearest = a if weight < .5 else b
            details = {'view_id': data['view_ids'][view],
                       'inspection_sample_time': float(times[nearest]),
                       'distortion': projection['distortion']['model']}
            if 'coefficients' in projection['distortion']:
                details['distortion_coefficients'] = self.scene.array(projection['distortion']['coefficients'])[nearest, view].tolist()
            if 'depth_mapping' in projection:
                details['depth_mapping'] = dict(projection['depth_mapping'])
            for section in ('optics', 'acquisition'):
                if section in data:
                    details[section] = {key: value if key == 'rolling_direction' else float(self.scene.array(value)[nearest, view])
                                        for key, value in data[section].items()}
            if volume:
                details['authored_view_volume'] = {'near': float(near_values[nearest, view]), 'far': None if unbounded[nearest, view] else float(self.scene.array(volume['far'])[nearest, view])}
            return Calibration(k.copy(), pose, width, height, projection['pixel_center'],
                               projection['distortion']['model'], near, far, details)

    def trajectories(self, object_id, ids=None, density=1, *, cancelled=None, max_bytes=256 * 1024**2, view=0):
        """Extract every requested source segment, preserving every validity gap.

        Explicit identities are unioned with the deterministic field subset, so
        lowering density never removes a picked identity. Capacity failures are
        visible errors; there is no silent sample truncation.
        """
        with self._lock:
            self._require_open()
            obj = self.object(object_id)
            if obj['encoding'] == 'gsv.camera.pinhole':
                data = obj['data']
                times = np.array(self.scene.array(data['times']), copy=True)
                poses = self.scene.array(data['pose']['camera_to_world'])
                if not 0 <= view < poses.shape[1]:
                    raise GSVError('Camera view index is out of range')
                if cancelled and cancelled():
                    raise InterruptedError('Trajectory request was superseded')
                if len(times) < 2:
                    return []
                if len(times) * 64 > max_bytes:
                    raise GSVError('Complete trajectories exceed the preview memory budget')
                points = np.array(poses[:, view, :3, 3], np.float64, copy=True)
                colors = np.tile(np.array([.4, .7, 1.], np.float32), (len(times), 1))
                return [Trajectory(points, times, colors, None, object_id)]
            if not self.supports(obj):
                raise GSVError('This object has no safe trajectory evaluator')
            identity = np.asarray(self.scene.array(obj['data']['ids']))
            times = self.object_times(object_id)
            if len(times) < 2 or not len(identity):
                return []
            if not np.isfinite(density) or not 0 <= density <= 1:
                raise GSVError('Trajectory density must be in [0,1]')
            rows = density_rows(identity, object_id, density)
            if ids is not None:
                requested = {int(x) for x in ids}
                rows = np.union1d(rows, np.flatnonzero(np.isin(identity, list(requested))))
            if not len(rows):
                return []
            if len(rows) * (len(times) * 64 + 512) > max_bytes:
                raise GSVError('Complete trajectories exceed the preview memory budget; lower trajectory density')
            # Only selected rows are retained; source frames remain byte-bounded.
            points = np.empty((len(times), len(rows), 3), np.float32)
            colors = np.empty_like(points)
            valid = np.empty((len(times), len(rows)), bool)
            for i, time in enumerate(times):
                if cancelled and cancelled():
                    raise InterruptedError('Trajectory request was superseded')
                if obj['encoding'] == 'gsv.gaussian.persistent-identity':
                    # Native timestamps need no interpolation. Advanced indexing
                    # reads just requested mmap rows; selecting one identity must
                    # not copy every source primitive or evict playback buffers.
                    records = self.scene.array(obj['data']['frames'][i])[rows]
                    points[i], colors[i], valid[i] = records[:, :3], records[:, 12:15], records[:, 7] > .5
                elif obj['encoding'] == 'gsv.scaffold.graph':
                    data = obj['data']
                    points[i] = self.scene.array(data['positions'])[i, rows]
                    colors[i] = [.3, .95, .6]
                    valid[i] = True
                    for mask in ('visibility', 'validity'):
                        if mask in data:
                            valid[i] &= self.scene.array(data[mask])[i, rows]
                else:
                    records = self._records(obj, float(time))[rows]
                    points[i], colors[i], valid[i] = records[:, :3], records[:, 12:15], records[:, 7] > .5
            paths = []
            for j, row in enumerate(rows):
                if j % 256 == 0 and cancelled and cancelled():
                    raise InterruptedError('Trajectory request was superseded')
                boundaries = np.diff(np.r_[False, valid[:, j], False].astype(np.int8))
                for start, stop in zip(np.flatnonzero(boundaries == 1), np.flatnonzero(boundaries == -1)):
                    if stop - start < 2:
                        continue
                    paths.append(Trajectory(points[start:stop, j].copy(), times[start:stop].copy(),
                                            colors[start:stop, j].copy(), int(identity[row]), object_id))
            self.scene._unchanged()
            return paths

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._cache.clear()
                self._cache_bytes = 0
                self.scene.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def density_rows(ids, object_id, density):
    """Same FNV1a / SplitMix ordering as macOS, preserving signed int64 IDs."""
    count = int(len(ids) * min(1., max(0., density)))
    if count == len(ids):
        return np.arange(len(ids), dtype=np.int64)
    mask = (1 << 64) - 1
    seed = 14695981039346656037
    for byte in object_id.encode('utf-8'):
        seed = ((seed ^ byte) * 1099511628211) & mask
    with np.errstate(over='ignore'):
        x = np.asarray(ids, np.int64).view(np.uint64) ^ np.uint64(seed)
        x = (x ^ (x >> np.uint64(30))) * np.uint64(0xbf58476d1ce4e5b9)
        x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94d049bb133111eb)
        x ^= x >> np.uint64(31)
    rank = np.argsort(x, kind='stable')
    return np.sort(rank[:count])
