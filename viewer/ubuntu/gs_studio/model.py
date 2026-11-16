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


def _check_cancel(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError('Scene request was superseded')


class _CancellationFile:
    """A loading-only file proxy; the authoritative SDK verifier is unchanged.

    Its buffered checksumming calls fileno() before each bounded pread, giving
    cancellation a checkpoint even inside a single large source buffer. The
    original descriptor is restored before a document is published.
    """
    def __init__(self, stream, cancelled):
        self.stream, self.cancelled = stream, cancelled

    def fileno(self):
        _check_cancel(self.cancelled)
        return self.stream.fileno()

    def __getattr__(self, name):
        return getattr(self.stream, name)


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


@dataclass(frozen=True)
class GaussianElement:
    id: int
    object_id: str
    position: np.ndarray
    scales: np.ndarray
    quaternion: np.ndarray
    color: np.ndarray
    opacity: float
    valid: bool
    confidence: float


@dataclass(frozen=True)
class _Endpoints:
    first: np.ndarray
    second: np.ndarray
    ids: np.ndarray
    object_ids: np.ndarray
    low: np.ndarray
    high: np.ndarray
    first_valid: np.ndarray
    second_valid: np.ndarray
    ranges: tuple


def _mix_records(first, second, weight, *, cancelled=None):
    if weight == 0:
        return first
    if weight == 1:
        return second
    records = np.empty_like(first)
    for start in range(0, len(first), 65_536):
        _check_cancel(cancelled)
        stop = min(start + 65_536, len(first))
        left, right = first[start:stop], second[start:stop]
        out = records[start:stop]
        out[:] = left.astype(np.float64) * (1 - weight) + right.astype(np.float64) * weight
        out[:, 7] = (left[:, 7] > .5) & (right[:, 7] > .5)
        if np.array_equal(left[:, 8:12], right[:, 8:12]):
            out[:, 8:12] = left[:, 8:12]
        else:
            out[:, 8:12] = slerp(left[:, 8:12], right[:, 8:12], weight)
        out[(left[:, 15] < 0) | (right[:, 15] < 0), 15] = -1
    records.flags.writeable = False
    return records


def _cpu_frame(packed, ids, owners, *, edges=None, cancelled=None):
    """Shared CPU materialization; never runs implicitly from prepared properties."""
    cov = np.empty((len(packed), 3, 3), np.float32)
    try:
        with np.errstate(over='raise', invalid='raise'):
            for start in range(0, len(packed), 65_536):
                _check_cancel(cancelled)
                stop = min(start + 65_536, len(packed))
                chunk = packed[start:stop]
                rotation = quat_matrix(chunk[:, 8:12]).astype(np.float32, copy=False)
                scaled = rotation * chunk[:, None, 4:7]
                cov[start:stop] = scaled @ scaled.swapaxes(-1, -2)
    except FloatingPointError as error:
        raise GSVError('Gaussian covariance exceeds the preview Float32 range; source parameters remain unchanged') from error
    frame = Frame(packed[:, :3], cov, packed[:, 12:15], packed[:, 3], ids, owners, packed,
                  np.empty((0, 2, 3), np.float32) if edges is None else edges)
    for array in vars(frame).values():
        array.flags.writeable = False
    _check_cancel(cancelled)
    return frame


class PreparedFrame:
    """Resident source endpoints; GPU browsing requires no full CPU geometry.

    Invalid rows retain their source identities and are suppressed by the
    renderer. Only element() and explicit materialize() evaluate attributes on
    the CPU. Endpoints own immutable storage, independent of document lifetime.
    """
    is_prepared = True

    def __init__(self, endpoints, weights, key, time, *, cancelled=None):
        self.first, self.second = endpoints.first, endpoints.second
        self.ids, self.object_ids = endpoints.ids, endpoints.object_ids
        self.bounds = endpoints.low, endpoints.high
        self.endpoint_key, self.time = key, float(time)
        self.count = len(self.ids)
        self.segments = tuple((owner, start, stop, weight)
                              for (owner, start, stop), weight in zip(endpoints.ranges, weights))
        self.weight = float(weights[0]) if len(weights) == 1 else None
        self.weights = None
        if len(weights) > 1:
            self.weights = np.empty(self.count, np.float32)
            for _, start, stop, weight in self.segments:
                self.weights[start:stop] = weight
            self.weights.flags.writeable = False
        self.valid_count = 0
        for _, start, stop, weight in self.segments:
            _check_cancel(cancelled)
            if weight == 0:
                valid = endpoints.first_valid[start:stop]
            elif weight == 1:
                valid = endpoints.second_valid[start:stop]
            else:
                valid = endpoints.first_valid[start:stop] & endpoints.second_valid[start:stop]
            self.valid_count += int(np.count_nonzero(valid))

    def __len__(self):
        return self.count

    def element(self, owner, identity):
        matches = []
        for object_id, start, stop, weight in self.segments:
            if owner is not None and owner != object_id:
                continue
            ids = self.ids[start:stop]
            row = int(np.searchsorted(ids, identity))
            if row == len(ids) or int(ids[row]) != identity:
                continue
            index = start + row
            record = _mix_records(self.first[index:index+1], self.second[index:index+1], weight)[0]
            matches.append(GaussianElement(int(ids[row]), object_id, record[:3].copy(), record[4:7].copy(),
                                           record[8:12].copy(), record[12:15].copy(), float(record[3]),
                                           bool(record[7] > .5), float(record[15])))
        return matches[0] if len(matches) == 1 else None

    def materialize(self, *, cancelled=None):
        records = np.empty_like(self.first)
        for _, start, stop, weight in self.segments:
            _check_cancel(cancelled)
            records[start:stop] = _mix_records(self.first[start:stop], self.second[start:stop], weight,
                                              cancelled=cancelled)
        valid = records[:, 7] > .5
        return _cpu_frame(records[valid], self.ids[valid].copy(), self.object_ids[valid].copy(), cancelled=cancelled)


class SceneDocument:
    """One read-only GSV document; no reconstruction arrays are ever modified.

    The default viewport selects the first render view's explicit field set, or
    one compatible space/clock cohort. Other peer objects remain inspectable.
    No unknown code, deformation recipe or metadata entrypoint is executed.
    """
    def __init__(self, path, *, cancelled=None):
        self.path = Path(path).resolve()
        self._lock = threading.RLock()
        self._closed = False
        _check_cancel(cancelled)
        self.scene = Scene(self.path)
        original_file = self.scene._file
        if cancelled is not None:
            self.scene._file = _CancellationFile(original_file, cancelled)
        try:
            _check_cancel(cancelled)
            self.manifest = self.scene.manifest
            self.objects = self.scene.objects
            self._objects = {o['id']: o for o in self.objects}
            self.manifest_sha256 = HEADER.unpack(os.pread(self.scene._file.fileno(), HEADER.size, 0))[4].hex()
            self.digest = self.manifest_sha256
            # Semantic validation is deliberately done on the loading worker.
            # It catches damaged later frames before playback starts.
            for obj in self.objects:
                _check_cancel(cancelled)
                validate_object(self.scene, obj)
            self.render_views = [o for o in self.objects if o['encoding'] == 'gsv.render.view']
            self.cameras = [o for o in self.objects if o['encoding'] == 'gsv.camera.pinhole']
            self.supported_ids = [o['id'] for o in self.objects if self.supports(o)]
            self.unsupported = [o for o in self.objects if o['type'] == 'gaussian' and not self.supports(o)]
            self._cache = OrderedDict()
            self.cache_budget = 256 * 1024**2
            self._cache_bytes = 0
            self._object_time_cache = {}
            self._scaffold_sizes = {}
            self._active_render_view = self.render_views[0]['id'] if self.render_views else None
            self._active_camera = None
            self.visible_ids = self._default_visible()
            basis = self._domain_basis(self.visible_ids)
            self.space = (basis.get('space') if basis else None) or 'world'
            self.clock = (basis.get('clock') if basis else None) or 'timeline'
            self.times = self._timeline()
            self.clock_unit = self.manifest.get('clocks', {}).get(self.clock, {}).get('unit', 'second')
            self.authored_cameras = []
            _check_cancel(cancelled)
        except BaseException:
            self.scene.close()
            raise
        finally:
            self.scene._file = original_file

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
        basis = next((o for o in candidates if o['encoding'] != 'gsv.gaussian.static'), candidates[0])
        return [o['id'] for o in candidates if o.get('space') == basis.get('space')
                and (o.get('clock') == basis.get('clock') or o.get('clock') is None
                     or o['encoding'] == 'gsv.gaussian.static')]

    def _domain_basis(self, identifiers):
        if self._active_render_view:
            view = self.object(self._active_render_view)
            return self.object(view['links']['camera'])
        objects = [self.object(x) for x in identifiers]
        timed = [o for o in objects if o['encoding'] != 'gsv.gaussian.static']
        if timed:
            return next((o for o in timed if len(self.object_times(o['id'])) > 1), timed[0])
        first = next(iter(objects), None)
        if first is None and hasattr(self, 'space'):
            return next((c for c in self.cameras if c.get('space') == self.space
                         and c.get('clock') == self.clock), None)
        return next((c for c in self.cameras if first is None or c.get('space') == first.get('space')),
                    first or next(iter(self.cameras), None))

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
        with self._lock:
            self._require_open()
            if identifier not in self._object_time_cache:
                result = self._object_times(identifier)
                result.flags.writeable = False
                self._object_time_cache[identifier] = result
            return self._object_time_cache[identifier]

    def _object_times(self, identifier):
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
                  if c.get('clock') == obj.get('clock') and c.get('space') == obj.get('space')]
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
            self._active_render_view = object_id if obj['encoding'] == 'gsv.render.view' else None
            self._active_camera = object_id if obj['encoding'] == 'gsv.camera.pinhole' else None
            self.visible_ids = identifiers
            basis = self._domain_basis(identifiers)
            self.space = space or (basis.get('space') if basis else None) or 'world'
            self.clock = clock or (basis.get('clock') if basis else None) or 'timeline'
            self.clock_unit = self.manifest.get('clocks', {}).get(self.clock, {}).get('unit', 'second')
            self.times = self._timeline()
            return list(self.visible_ids)

    def _timeline(self):
        tracks = [self.object_times(x) for x in self.visible_ids
                  if self.object(x)['encoding'] != 'gsv.gaussian.static']
        if self._active_render_view:
            camera = self.object(self.object(self._active_render_view)['links']['camera'])
            tracks.append(self.scene.array(camera['data']['times']))
        elif self._active_camera:
            tracks.append(self.scene.array(self.object(self._active_camera)['data']['times']))
        elif not tracks:
            camera = next((o for o in self.cameras if o.get('clock') == self.clock
                           and o.get('space') == self.space), None)
            if camera:
                tracks.append(self.scene.array(camera['data']['times']))
        for o in self.objects:
            if (o['encoding'] == 'gsv.observation.camera-rgb' and o.get('clock') == self.clock
                    and o.get('space') == self.space):
                tracks.append(self.scene.array(o['data']['samples']['times']))
        nonempty = [t for t in tracks if len(t)]
        return np.unique(np.concatenate(nonempty)) if nonempty else np.array([0.0], np.float64)

    def set_visible(self, identifiers):
        with self._lock:
            self._require_open()
            identifiers = list(dict.fromkeys(identifiers))
            basis = self._check_domains(identifiers)
            self._active_render_view = None
            self._active_camera = None
            self.visible_ids = identifiers
            basis = self._domain_basis(identifiers) or basis
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

    @staticmethod
    def _cache_size(value):
        if isinstance(value, np.ndarray):
            return value.nbytes
        roots = {}
        for array in vars(value).values():
            if not isinstance(array, np.ndarray):
                continue
            while isinstance(array.base, np.ndarray):
                array = array.base
            roots[id(array)] = array
        return sum(array.nbytes for array in roots.values())

    def _cached(self, key):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

    def _remember(self, key, value):
        size = self._cache_size(value)
        if size <= self.cache_budget:
            while self._cache and (len(self._cache) >= 6 or self._cache_bytes + size > self.cache_budget):
                _, discarded = self._cache.popitem(last=False)
                self._cache_bytes -= self._cache_size(discarded)
            self._cache[key] = value
            self._cache_bytes += size
        return value

    def _records(self, obj, time, cancelled=None):
        """Byte-bounded source-sample LRU; interpolation scratch stays chunked."""
        codec, data = obj['encoding'], obj['data']
        if codec in ('gsv.gaussian.persistent-identity', 'gsv.scaffold.graph'):
            times = self.object_times(obj['id'])
            n = len(self.scene.array(data['ids']))
            if not len(times):
                return np.empty((0, 16), np.float32)
            b = min(int(np.searchsorted(times, time)), len(times) - 1)
            a = max(0, b - 1)
            w = _time_weight(time, times[a], times[b]) if a != b else 0.0
            if time >= times[b]:
                a, w = b, 0.0

            def at(index):
                _check_cancel(cancelled)
                key = ('sample', obj['id'], index)
                cached = self._cached(key)
                if cached is not None:
                    return cached
                if codec == 'gsv.gaussian.persistent-identity':
                    row = np.array(self.scene.array(data['frames'][index]), copy=True)
                    _check_cancel(cancelled)
                    row.flags.writeable = False
                    return self._remember(key, row)
                positions = self.scene.array(data['positions'])
                row = np.zeros((n, 16), np.float32)
                row[:, :3] = positions[index]
                row[:, 3] = .95
                # Glyph dimensions are a display choice, independent of node
                # topology and source certainty/visibility/validity evidence.
                if obj['id'] not in self._scaffold_sizes:
                    points = positions.reshape(-1, 3)
                    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
                    for start in range(0, len(points), 65_536):
                        _check_cancel(cancelled)
                        chunk = points[start:start + 65_536]
                        lo = np.minimum(lo, np.min(chunk, axis=0))
                        hi = np.maximum(hi, np.max(chunk, axis=0))
                    span = hi - lo if len(points) else np.ones(3)
                    self._scaffold_sizes[obj['id']] = max(float(np.linalg.norm(span)) * .0015, .0001)
                row[:, 4:7] = self._scaffold_sizes[obj['id']]
                row[:, 7] = 1
                for mask in ('visibility', 'validity'):
                    if mask in data:
                        row[:, 7] *= self.scene.array(data[mask])[index]
                row[:, 8] = 1
                row[:, 12:15] = [.3, .95, .6]
                row[:, 15] = -1
                _check_cancel(cancelled)
                row.flags.writeable = False
                return self._remember(key, row)

            first = at(a)
            if not w:
                records = first
            elif w == 1:
                records = at(b)
            else:
                second = at(b)
                records = _mix_records(first, second, w, cancelled=cancelled)
        else:
            key = ('native', obj['id'], 0. if codec == 'gsv.gaussian.static' else float(time))
            cached = self._cached(key)
            if cached is not None:
                return cached
            _check_cancel(cancelled)
            records = evaluate_native(self.scene, obj['id'], time)
            _check_cancel(cancelled)
            records.flags.writeable = False
            return self._remember(key, records)
        records.flags.writeable = False
        return records

    def prepare_frame(self, time, object_ids=None, *, cancelled=None):
        """Prepare all static/persistent source identities for resident GPU use.

        Each cohort's source endpoints are assembled once per interval. Only
        scalar/per-field time weights and validity counts change while seeking.
        Analytical fields and scaffold glyphs retain the explicit CPU fallback.
        """
        with self._lock:
            self._require_open()
            _check_cancel(cancelled)
            time = float(time)
            if not np.isfinite(time):
                raise GSVError('Time must be finite')
            identifiers = list(dict.fromkeys(self.visible_ids if object_ids is None else object_ids))
            self._check_domains(identifiers)
            if not identifiers:
                return Frame.empty()
            if any(self.object(x)['encoding'] not in ('gsv.gaussian.static', 'gsv.gaussian.persistent-identity')
                   for x in identifiers):
                return self.evaluate(time, identifiers, cancelled=cancelled)
            samples, weights = [], []
            for identifier in identifiers:
                obj = self.object(identifier)
                if obj['encoding'] == 'gsv.gaussian.static':
                    samples.append((identifier, None, None))
                    weights.append(0.)
                    continue
                times = self.object_times(identifier)
                if not len(times):
                    samples.append((identifier, None, None))
                    weights.append(0.)
                    continue
                a = min(max(int(np.searchsorted(times, time, side='right')) - 1, 0), len(times)-1)
                b = min(a + 1, len(times)-1)
                samples.append((identifier, a, b))
                weights.append(_time_weight(time, times[a], times[b]) if a != b else 0.)
            key = ('endpoints', tuple(samples))
            endpoints = self._cached(key)
            if endpoints is None:
                first, second, identities, owners, ranges = [], [], [], [], []
                lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
                offset = 0
                for identifier, a, b in samples:
                    _check_cancel(cancelled)
                    obj = self.object(identifier)
                    data = obj['data']
                    ids = np.array(self.scene.array(data['ids']), copy=True)
                    if a is None:
                        left = right = self._records(obj, time, cancelled)
                    else:
                        times = self.object_times(identifier)
                        left = self._records(obj, float(times[a]), cancelled)
                        right = self._records(obj, float(times[b]), cancelled)
                    first.append(left)
                    second.append(right)
                    identities.append(ids)
                    owners.append(np.full(len(ids), identifier, dtype=object))
                    ranges.append((identifier, offset, offset+len(ids)))
                    offset += len(ids)
                    if obj['encoding'] == 'gsv.gaussian.persistent-identity':
                        bound = np.asarray(data['bounds'], np.float64)
                        lo, hi = np.minimum(lo, bound[0]), np.maximum(hi, bound[1])
                    elif len(left):
                        extent = 4 * left[:, 4:7].max(axis=1).astype(np.float64)
                        lo = np.minimum(lo, np.min(left[:, :3].astype(np.float64)-extent[:, None], axis=0))
                        hi = np.maximum(hi, np.max(left[:, :3].astype(np.float64)+extent[:, None], axis=0))
                if not offset:
                    return Frame.empty()
                left = first[0] if len(first) == 1 else np.concatenate(first)
                right = second[0] if len(second) == 1 else np.concatenate(second)
                endpoints = _Endpoints(left, right,
                                       identities[0] if len(identities) == 1 else np.concatenate(identities),
                                       owners[0] if len(owners) == 1 else np.concatenate(owners),
                                       lo, hi, left[:, 7] > .5, right[:, 7] > .5, tuple(ranges))
                for array in vars(endpoints).values():
                    if isinstance(array, np.ndarray):
                        array.flags.writeable = False
                _check_cancel(cancelled)
                self._remember(key, endpoints)
            self.scene._unchanged()
            endpoint_key = (str(self.path), self.manifest_sha256, tuple(samples))
            return PreparedFrame(endpoints, weights, endpoint_key, time, cancelled=cancelled)

    def evaluate(self, time, object_ids=None, *, cancelled=None):
        with self._lock:
            self._require_open()
            _check_cancel(cancelled)
            time = float(time)
            if not np.isfinite(time):
                raise GSVError('Time must be finite')
            identifiers = list(dict.fromkeys(self.visible_ids if object_ids is None else object_ids))
            self._check_domains(identifiers)
            effective_times = []
            for identifier in identifiers:
                obj = self.object(identifier)
                codec = obj['encoding']
                if codec == 'gsv.gaussian.static':
                    effective_times.append(None)
                elif codec in ('gsv.gaussian.persistent-identity', 'gsv.scaffold.graph'):
                    times = self.object_times(identifier)
                    effective_times.append(float(np.clip(time, times[0], times[-1])) if len(times) else None)
                else:
                    effective_times.append(time)
            frame_key = ('frame', tuple(identifiers), tuple(effective_times))
            cached = self._cached(frame_key)
            if cached is not None:
                return cached
            records, ids, owners, edges = [], [], [], []
            for identifier in identifiers:
                _check_cancel(cancelled)
                obj = self.object(identifier)
                packed = self._records(obj, time, cancelled)
                valid = packed[:, 7] > .5
                if obj['encoding'] == 'gsv.scaffold.graph' and 'edges' in obj['data']:
                    topology = np.asarray(self.scene.array(obj['data']['edges']), np.int64)
                    visible_edges = valid[topology].all(axis=1)
                    edges.append(packed[topology[visible_edges], :3].copy())
                records.append(packed if np.all(valid) else packed[valid])
                ids.append(np.asarray(self.scene.array(obj['data']['ids']))[valid])
                owners.append(np.full(int(valid.sum()), identifier, dtype=object))
            _check_cancel(cancelled)
            self.scene._unchanged()
            if not records:
                return Frame.empty()
            packed = records[0] if len(records) == 1 else np.concatenate(records)
            frame = _cpu_frame(packed, np.concatenate(ids), np.concatenate(owners),
                               edges=np.concatenate(edges) if edges else None, cancelled=cancelled)
            return self._remember(frame_key, frame)

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
                far_unbounded = (unbounded[a, view] if weight == 0 else unbounded[b, view]
                                 if weight == 1 else unbounded[a, view] or unbounded[b, view])
                if not far_unbounded:
                    values = self.scene.array(volume['far'])
                    far = float((1 - weight) * values[a, view] + weight * values[b, view])
            nearest = a if weight <= .5 else b
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
                self._object_time_cache.clear()
                self._scaffold_sizes.clear()
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
