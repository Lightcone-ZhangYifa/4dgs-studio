#!/usr/bin/env python3
"""Profile a real GSV in a real Gtk.Window/Gtk.GLArea, without editing its source.

Example (keep an existing GNOME/Mutter output alive externally):
  python benchmark_production_viewport.py --scene /data/scene.gsv --backend x11 \
    --output /tmp/new-profile --camera first --width 1280 --height 720 --snapshot

No Xvfb, EGL pbuffer, input injection, monitor management or desktop capture is
used. Requests use the normal GLArea render signal. One request is in flight at
a time; GL fences are polled without blocking and native GPU timer queries are
collected after completion. These are serialized viewport latencies and GPU
work timings, NOT compositor/desktop presentation FPS or maximum throughput.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes as C
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'viewer/ubuntu'), str(ROOT / 'packages/gsv/src')]


def memory():
    values = {}
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith(('VmRSS:', 'VmHWM:')):
            values[line.split(':')[0]] = int(line.split()[1]) * 1024
    return {'rss_bytes': values.get('VmRSS'), 'rss_peak_bytes': values.get('VmHWM')}


def fingerprint(path, *, digest=True):
    """Read a regular source; exclude atime, which reads may legitimately update."""
    started = time.perf_counter()
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('The source must be a regular GSV file')
        checksum = hashlib.sha256() if digest else None
        if checksum is not None:
            while chunk := os.read(fd, 8 * 1024 * 1024):
                checksum.update(chunk)
        after = os.fstat(fd)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, name) != getattr(after, name) for name in fields):
            raise RuntimeError('Source changed during its read-only fingerprint')
        return {'stat': {name.removeprefix('st_'): getattr(after, name) for name in fields},
                'sha256': checksum.hexdigest() if checksum else None,
                'elapsed_ms': (time.perf_counter()-started)*1000}
    finally:
        os.close(fd)


def lerp(a, b, weight):
    if weight == 0:
        return float(a)
    if weight == 1:
        return float(b)
    value = (1-weight)*float(a) + weight*float(b)
    if not math.isfinite(value):
        raise ValueError('The requested interpolated native time is not finite')
    return value


def plan(times, args):
    """Document-native intervals, with resident and streaming work kept separate."""
    import bisect
    samples = [float(value) for value in times]
    if not samples or not all(math.isfinite(value) for value in samples):
        raise ValueError('The source has no finite timeline')
    index = 0
    if args.start_time is not None:
        if not samples[0] <= args.start_time <= samples[-1]:
            raise ValueError('--start-time lies outside the active document timeline')
        index = max(0, min(len(samples)-2, bisect.bisect_right(samples, args.start_time)-1))
    intervals = [(samples[i], samples[i+1]) for i in range(index, min(len(samples)-1, index+args.intervals))]
    if not intervals:
        intervals = [(samples[0], samples[0])]
    initial = lerp(*intervals[0], .25)
    cases = [{'phase': 'first_upload', 'operation': 'initial', 'time': initial, 'prepare': True,
              'include': True, 'reset': True}]
    for i in range(args.warmup+args.samples):
        cases.append({'phase': 'warm_prepare_resident', 'operation': 'resident', 'time': initial,
                      'prepare': True, 'include': i >= args.warmup})
    for number, interval in enumerate(intervals[1:], 1):
        for repeat, weight in enumerate((.25, .75)):
            cases.append({'phase': 'interval_first_visit' if not repeat else 'interval_immediate_repeat',
                'operation': 'interval_switch', 'interval': number, 'time': lerp(*interval, weight),
                'prepare': True, 'include': True})
    for operation in ('orbit', 'pan', 'dolly'):
        for i in range(args.warmup+args.samples):
            cases.append({'phase': 'resident_navigation', 'operation': operation, 'time': initial,
                'prepare': i == 0, 'reset': i == 0, 'include': i >= args.warmup, 'step': i})
    for i in range(args.warmup+args.samples):
        cases.append({'phase': 'resident_playback', 'operation': 'playback',
            'time': lerp(*intervals[0], .1+.8*i/max(1, args.warmup+args.samples-1)),
            'prepare': True, 'reset': i == 0, 'include': i >= args.warmup, 'follow_camera': True})
    if len(intervals) > 1:
        for i in range(args.samples):
            cases.append({'phase': 'streaming_playback', 'operation': 'playback',
                'time': lerp(intervals[0][0], intervals[-1][1], i/max(1, args.samples-1)),
                'prepare': True, 'reset': i == 0, 'include': True, 'follow_camera': True})
    points = ((.5, .5), (.35, .5), (.65, .5), (.5, .35), (.5, .65))
    for i in range(args.picks):
        cases.append({'phase': 'exact_pick', 'operation': 'pick', 'time': initial,
            'prepare': i == 0, 'reset': i == 0, 'include': True, 'pixel_fraction': points[i % len(points)]})
    return cases, intervals


def summarize(rows):
    import numpy as np
    result = {}
    names = ('prepare_ms', 'camera_prepare_ms', 'cpu_submission_ms', 'gpu_ms', 'gpu_sort_ms',
             'gpu_render_ms', 'queue_to_render_end_ms', 'queue_to_gl_complete_ms',
             'prepare_plus_gl_complete_ms', 'pick_blocking_wall_ms')
    groups = {}
    for row in rows:
        if row.get('include') and not row.get('extra_redraws'):
            groups.setdefault(row['phase']+'/'+row['operation'], []).append(row)
    for name, items in groups.items():
        group = {'samples': len(items)}
        for key in names:
            values = [float(row[key]) for row in items if row.get(key) is not None]
            if values:
                group[key] = {'median': float(np.median(values)), 'p95': float(np.percentile(values, 95)),
                              'minimum': min(values), 'maximum': max(values), 'samples': len(values)}
        group['endpoint_upload_bytes'] = sum(row.get('endpoint_upload_bytes', 0) for row in items)
        group['endpoint_swaps'] = sum(bool(row.get('endpoint_swapped')) for row in items)
        group['sort_reused_frames'] = sum(bool(row.get('sort_reused')) for row in items)
        group['max_rss_bytes'] = max((row.get('rss_bytes') or 0) for row in items)
        result[name] = group
    return result


class _Sync:
    """Resolve core sync entry points from the actual current GLX/EGL context."""
    def __init__(self):
        resolve = None
        self.library = None
        for library_name, current_name, resolver_name in (
                ('libGLX.so.0', 'glXGetCurrentContext', 'glXGetProcAddress'),
                ('libEGL.so.1', 'eglGetCurrentContext', 'eglGetProcAddress')):
            try:
                library = C.CDLL(library_name)
                current = getattr(library, current_name); current.restype = C.c_void_p; current.argtypes = []
                if not current():
                    continue
                resolve = getattr(library, resolver_name)
                resolve.restype = C.c_void_p; resolve.argtypes = [C.c_char_p]
                self.library = library
                break
            except OSError:
                continue
        if resolve is None:
            raise RuntimeError('No actual current GLX/EGL context for GPU completion fences')
        def function(name, result, *arguments):
            address = resolve(name.encode())
            if not address:
                raise RuntimeError(name+' is unavailable in the actual GLArea context')
            return C.CFUNCTYPE(result, *arguments)(address)
        self.fence = function('glFenceSync', C.c_void_p, C.c_uint, C.c_uint)
        self.wait = function('glClientWaitSync', C.c_uint, C.c_void_p, C.c_uint, C.c_uint64)
        self.delete = function('glDeleteSync', None, C.c_void_p)
        self.flush = function('glFlush', None)


def run_gui(args, profile):
    if args.backend != 'environment':
        os.environ['GDK_BACKEND'] = args.backend
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gio, GLib, Gtk
    import numpy as np
    import OpenGL
    from gs_studio.model import SceneDocument
    from gs_studio.viewport import Viewport
    profile['runtime_libraries'] = {'numpy': np.__version__, 'PyOpenGL': OpenGL.__version__,
        'PyOpenGL_file': OpenGL.__file__, 'Gtk': [Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()]}
    profile['process_environment'] = {key: os.environ[key] for key in (
        'GDK_BACKEND', 'GSK_RENDERER', 'DISPLAY', 'WAYLAND_DISPLAY', 'PYOPENGL_PLATFORM',
        '__NV_PRIME_RENDER_OFFLOAD', '__GLX_VENDOR_LIBRARY_NAME', '__EGL_VENDOR_LIBRARY_FILENAMES',
        'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS') if key in os.environ}

    if args.wait_output:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        end = time.monotonic()+args.wait_output
        while True:
            monitors = bus.call_sync('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
                'org.gnome.Mutter.DisplayConfig', 'GetCurrentState', None, None,
                Gio.DBusCallFlags.NONE, 5000, None).unpack()[2]
            if monitors:
                profile['logical_monitors_at_start'] = len(monitors)
                break
            if time.monotonic() >= end:
                raise RuntimeError('No existing Mutter output; keep one alive externally. This tool never creates monitors.')
            time.sleep(.1)
    app = Gtk.Application(application_id='io.github.LightconeZhangYifa.ProductionViewportBenchmark.p'+str(os.getpid()))
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='production-gsv')
    cancelled = threading.Event()
    state = {'document': None, 'frame': None, 'case': None, 'index': 0, 'closing': False,
             'last_stats': {}, 'timer': 0, 'sync': None, 'base': None, 'camera_id': None,
             'endpoint_keys': set(), 'unattributed_renders': 0, 'watchdog': 0}
    profile['samples'] = []
    began = time.perf_counter()

    def checkpoint(stage):
        current = state['case'] or {}
        progress = {'stage': stage, 'elapsed_ms': (time.perf_counter()-began)*1000,
            'samples_completed': len(profile['samples']), 'sample': current.get('sample'),
            'phase': current.get('phase'), 'operation': current.get('operation'),
            'document_loaded': state['document'] is not None,
            'graphics': profile.get('graphics'), 'memory': memory()}
        temporary = args.output/'progress.json.tmp'
        temporary.write_text(json.dumps(progress, indent=2))
        temporary.replace(args.output/'progress.json')

    def fail(error):
        if state['closing']:
            return GLib.SOURCE_REMOVE
        profile['errors'].append({'phase': state['case']['phase'] if state['case'] else 'initialization',
            'type': type(error).__name__, 'message': str(error),
            'traceback': ''.join(traceback.format_exception(error))})
        finish()
        return GLib.SOURCE_REMOVE

    def finish():
        if state['closing']:
            return
        state['closing'] = True
        cancelled.set()
        if state['timer']:
            GLib.source_remove(state['timer']); state['timer'] = 0
        if state['watchdog']:
            GLib.source_remove(state['watchdog']); state['watchdog'] = 0
        current = state['case']
        if current and current.get('fence'):
            viewport.make_current(); state['sync'].delete(current['fence']); current['fence'] = None
        if args.snapshot and not profile['errors'] and viewport.get_realized():
            try:
                from PIL import Image
                rgba = viewport.snapshot_rgba()
                target = args.output/'viewport.png'
                Image.fromarray(rgba).save(target)
                profile['viewport_snapshot'] = {'file': target.name, 'width': rgba.shape[1],
                    'height': rgba.shape[0], 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                    'capture': 'Own completed GLArea render target only; no desktop capture'}
            except Exception as error:
                profile['errors'].append({'phase': 'snapshot', 'message': str(error)})
        window.destroy()
        app.quit()

    def watchdog():
        if state['closing']:
            state['watchdog'] = 0
            return GLib.SOURCE_REMOVE
        current = state['case']
        now = time.perf_counter()
        if current is not None and now-current.get('queue_requested_at', current['worker_requested_at']) > args.frame_timeout:
            state['watchdog'] = 0
            checkpoint('request_timeout')
            fail(TimeoutError('Whole viewport request timed out, including preparation/queue-to-render; '
                'a missing render callback cannot bypass the watchdog'))
            return GLib.SOURCE_REMOVE
        if state['document'] is None and now-began > args.load_timeout:
            state['watchdog'] = 0
            checkpoint('source_load_timeout')
            fail(TimeoutError('Timed out loading/validating the read-only source'))
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def load():
        start = time.perf_counter(); rss = memory()
        if not args.quiet:
            print(json.dumps({'stage': 'source_open_validation_begin', 'memory': rss}), flush=True)
        doc = SceneDocument(args.scene, cancelled=cancelled.is_set)
        try:
            if args.field:
                doc.set_visible(args.field)
            if not doc.visible_ids:
                raise ValueError('The active document domain has no supported visible geometry')
            doc.cache_budget = round(args.cache_mib*1024**2)
            candidates = [camera for camera in doc.cameras
                if camera.get('space') == doc.space and camera.get('clock') == doc.clock]
            camera_id = None
            if args.camera != 'scene':
                if args.camera == 'first':
                    if not candidates:
                        raise ValueError('No compatible source camera; explicitly select --camera scene for bounds framing')
                    camera_id = candidates[0]['id']
                else:
                    camera_id = args.camera
                    if camera_id not in {item['id'] for item in candidates}:
                        raise ValueError('The selected source camera does not share the active space/clock')
            info = {'source_open_validation_ms': (time.perf_counter()-start)*1000,
                'memory_before': rss, 'memory_after': memory(), 'manifest_sha256': doc.manifest_sha256,
                'space': doc.space, 'clock': doc.clock, 'clock_unit': doc.clock_unit,
                'visible_fields': list(doc.visible_ids), 'camera_id': camera_id,
                'camera_view': args.view, 'source_timeline_samples': len(doc.times),
                'timeline_min': float(doc.times[0]), 'timeline_max': float(doc.times[-1]),
                'cache_budget_bytes': doc.cache_budget,
                'objects': [{'id': obj['id'], 'encoding': obj['encoding'], 'type': obj['type']}
                            for obj in doc.objects]}
            if not args.quiet:
                print(json.dumps({'stage': 'source_open_validation_complete',
                    'source_open_validation_ms': info['source_open_validation_ms'],
                    'memory': info['memory_after'], 'camera_id': camera_id}), flush=True)
            return doc, camera_id, info
        except BaseException:
            doc.close(); raise

    def prepared(future):
        if state['closing']:
            return GLib.SOURCE_REMOVE
        try:
            frame, calibration, measurement = future.result()
            current = state['case']
            current.update(measurement)
            state['frame'] = frame
            apply(current, frame, calibration)
        except Exception as error:
            fail(error)
        return GLib.SOURCE_REMOVE

    def prepare(case):
        doc = state['document']
        started = time.perf_counter()
        frame = doc.prepare_frame(case['time'], cancelled=cancelled.is_set)
        elapsed = (time.perf_counter()-started)*1000
        calibration = None
        started = time.perf_counter()
        if state['camera_id'] is not None and (state['base'] is None or case.get('follow_camera') or case.get('reset')):
            calibration = doc.camera(state['camera_id'], case['time'], args.view)
        camera_ms = (time.perf_counter()-started)*1000
        key = repr(getattr(frame, 'endpoint_key', None))
        digest = hashlib.sha256(key.encode()).hexdigest() if getattr(frame, 'is_prepared', False) else None
        return frame, calibration, {'prepare_ms': elapsed, 'camera_prepare_ms': camera_ms,
            'prepared_endpoints': bool(getattr(frame, 'is_prepared', False)),
            'endpoint_key_sha256': digest, 'source_rows': int(getattr(frame, 'source_count', len(frame.ids))),
            'frame_rows': len(frame.ids), 'valid_rows': int(getattr(frame, 'valid_count', len(frame.ids))),
            'memory_after_prepare': memory()}

    def next_case():
        if state['closing']:
            return GLib.SOURCE_REMOVE
        if state['index'] >= len(state['cases']):
            profile['passed'] = not profile['errors']
            finish(); return GLib.SOURCE_REMOVE
        current = dict(state['cases'][state['index']])
        state['index'] += 1
        current.update({'sample': state['index'], 'worker_requested_at': time.perf_counter(),
                        'fence': None, 'extra_redraws': 0})
        state['case'] = current
        checkpoint('preparing_source_frame' if current['prepare'] else 'applying_resident_navigation')
        if current['prepare']:
            future = executor.submit(prepare, current)
            future.add_done_callback(lambda result: GLib.idle_add(prepared, result))
        else:
            try:
                current.update({'prepare_ms': 0., 'camera_prepare_ms': 0.,
                    'prepared_endpoints': bool(getattr(state['frame'], 'is_prepared', False)),
                    'source_rows': int(getattr(state['frame'], 'source_count', len(state['frame'].ids))),
                    'frame_rows': len(state['frame'].ids),
                    'valid_rows': int(getattr(state['frame'], 'valid_count', len(state['frame'].ids)))})
                apply(current, state['frame'], None)
            except Exception as error:
                fail(error)
        return GLib.SOURCE_REMOVE

    def apply(current, frame, calibration):
        if state['base'] is None:
            viewport.set_frame(frame)
            # Match Studio's initial scene framing before looking through a
            # source camera: authored bounds determine the orbit pivot.
            viewport.frame_scene()
            if calibration is not None:
                viewport.set_camera(calibration)
            k, pose, width, height = viewport.get_camera()
            state['base'] = (pose.copy(), viewport.navigation.pivot.copy(), viewport.navigation.up.copy(), viewport.navigation.fov)
            profile['initial_camera'] = {'source_id': state['camera_id'], 'view': args.view,
                'source_time': current['time'], 'K_at_logical_raster': k.tolist(), 'c2w': pose.tolist(),
                'logical_raster': [width, height], 'mode': 'source calibration' if calibration else 'scene bounds framing'}
        elif current.get('reset'):
            pose, pivot, up, fov = state['base']
            viewport.navigation.c2w = pose.copy(); viewport.navigation.pivot = pivot.copy()
            viewport.navigation.up = up.copy(); viewport.navigation.fov = fov
        if current.get('follow_camera') and calibration is not None:
            viewport.set_camera(calibration)
        if current['prepare']:
            viewport.set_frame(frame)
        key = current.get('endpoint_key_sha256')
        current['endpoint_key_seen_before'] = key in state['endpoint_keys'] if key is not None else None
        if key is not None:
            state['endpoint_keys'].add(key)
        current['queue_requested_at'] = time.perf_counter()
        current['submitted_before'] = viewport._renderer.submitted_frame_count
        operation = current['operation']
        if operation in ('orbit', 'pan', 'dolly'):
            direction = 1 if current.get('step', 0) % 2 == 0 else -1
            k = viewport.get_camera()[0]
            if operation == 'orbit':
                viewport.navigation.orbit(2.*direction, .5*direction)
            elif operation == 'pan':
                viewport.navigation.pan(3.*direction, direction, k)
            else:
                viewport.navigation.dolly(.4*direction, pixel=[viewport.get_width()*.6, viewport.get_height()*.5], k=k)
        if operation == 'pick':
            viewport.make_current()
            fraction = current['pixel_fraction']
            started = time.perf_counter()
            hit = viewport.pick(viewport.get_width()*fraction[0], viewport.get_height()*fraction[1])
            current['pick_blocking_wall_ms'] = (time.perf_counter()-started)*1000
            current['pick_identity'] = list(hit) if hit is not None else None
            current['pick_timing_definition'] = 'Exact production GPU projection/readback and CPU result reduction; synchronous wall time'
        viewport.queue_render()
        checkpoint('queued_normal_glarea_render')

    def after_render(_area, _context):
        current = state['case']
        if current is None or 'queue_requested_at' not in current or state['closing']:
            state['unattributed_renders'] += 1
            return False
        if current.get('fence'):
            current['extra_redraws'] += 1
            return False
        try:
            current['render_end_at'] = time.perf_counter()
            current['submission_stats'] = dict(state['last_stats'])
            current['fence'] = state['sync'].fence(0x9117, 0)  # GL_SYNC_GPU_COMMANDS_COMPLETE
            if not current['fence']:
                raise RuntimeError('Creating the actual GLArea completion fence failed')
            state['sync'].flush()
            state['timer'] = GLib.timeout_add(1, poll_frame)
            checkpoint('waiting_nonblocking_gl_completion')
        except Exception as error:
            GLib.idle_add(fail, error)
        return False

    def poll_frame():
        current = state['case']
        try:
            viewport.make_current()
            status = state['sync'].wait(current['fence'], 0, 0)
            if status == 0x911D:  # GL_WAIT_FAILED
                raise RuntimeError('The GLArea completion fence failed')
            if status not in (0x911A, 0x911C):
                if time.perf_counter()-current['queue_requested_at'] > args.frame_timeout:
                    raise TimeoutError('Timed out waiting for the actual viewport GL work to complete')
                return GLib.SOURCE_CONTINUE
            completed = time.perf_counter()
            state['sync'].delete(current['fence']); current['fence'] = None
            renderer = viewport._renderer
            renderer.poll_metrics()
            metrics = renderer.performance_metrics()
            row = {key: value for key, value in current.items() if not key.endswith('_at') and key not in ('fence', 'submission_stats')}
            row.update(current['submission_stats'])
            # A count and its source time/pose/sequence must come from the same
            # completed snapshot, including samples newer than on_stats().
            row.update(metrics)
            row.update(memory())
            row['queue_to_render_end_ms'] = (current['render_end_at']-current['queue_requested_at'])*1000
            row['queue_to_gl_complete_ms'] = (completed-current['queue_requested_at'])*1000
            row['prepare_plus_gl_complete_ms'] = row['prepare_ms']+row['camera_prepare_ms']+row['queue_to_gl_complete_ms']
            row['worker_roundtrip_ms'] = (current['queue_requested_at']-current['worker_requested_at'])*1000
            row['completion_fence_signaled'] = True
            row['completion_includes_desktop_presentation'] = False
            if row['extra_redraws']:
                row['gpu_ms'] = row['gpu_sort_ms'] = row['gpu_render_ms'] = None
                row['gpu_attribution_warning'] = 'Unexpected extra GLArea redraw; excluded from summaries'
            profile['samples'].append(row)
            state['case'] = None; state['timer'] = 0
            checkpoint('frame_complete')
            if not args.quiet:
                print(json.dumps({'sample': row['sample'], 'phase': row['phase'], 'operation': row['operation'],
                    'prepare_ms': row['prepare_ms'], 'cpu_submission_ms': row.get('cpu_submission_ms'),
                    'gpu_ms': row.get('gpu_ms'), 'completed_wall_ms': row['queue_to_gl_complete_ms'],
                    'endpoint_upload_bytes': row.get('endpoint_upload_bytes'), 'rss_bytes': row.get('rss_bytes')}), flush=True)
            GLib.idle_add(next_case)
            return GLib.SOURCE_REMOVE
        except Exception as error:
            state['timer'] = 0
            fail(error)
            return GLib.SOURCE_REMOVE

    def ready():
        if state['closing']:
            return GLib.SOURCE_REMOVE
        if viewport.render_error or viewport.get_error():
            fail(RuntimeError(viewport.render_error or str(viewport.get_error())))
            return GLib.SOURCE_REMOVE
        if not viewport.get_realized() or viewport._renderer is None or state['document'] is None:
            return GLib.SOURCE_CONTINUE
        try:
            viewport.make_current()
            renderer = viewport._renderer; renderer.sort_mode = args.sort_mode
            if args.sort_mode == 'gpu' and not renderer.gpu_sorter.supported:
                raise RuntimeError('Explicit GPU sort unavailable: '+renderer.gpu_sorter.reason)
            info = dict(viewport.graphics_info)
            if args.require_hardware and any(value in info.get('renderer', '').lower() for value in ('llvmpipe', 'softpipe', 'software', 'swrast')):
                raise RuntimeError('Hardware required but actual GLArea renderer is '+info.get('renderer', '?'))
            state['sync'] = _Sync()
            profile['graphics'] = dict(info, context_type=viewport.get_context().__gtype__.name,
                gpu_sort_supported=renderer.gpu_sorter.supported, gpu_sort_reason=renderer.gpu_sorter.reason)
            state['cases'], intervals = plan(state['document'].times, args)
            profile['native_intervals'] = [list(pair) for pair in intervals]
            checkpoint('actual_gl_context_ready')
            # Let initial map/size allocation settle before attributing a frame.
            GLib.timeout_add(200, next_case)
            return GLib.SOURCE_REMOVE
        except Exception as error:
            fail(error)
            return GLib.SOURCE_REMOVE

    def loaded(future):
        try:
            doc, camera_id, info = future.result()
            if state['closing']:
                doc.close(); return GLib.SOURCE_REMOVE
            state['document'], state['camera_id'] = doc, camera_id
            profile['document'] = info
            checkpoint('source_loaded_and_validated')
        except Exception as error:
            fail(error)
        return GLib.SOURCE_REMOVE

    def activated(application):
        nonlocal window, viewport
        window = Gtk.ApplicationWindow(application=application, title='4DGS Production Viewport Benchmark',
            default_width=args.width, default_height=args.height)
        class ProfiledViewport(Viewport):
            def _render(self, area, context):
                # Gtk.GLArea::render has a true-handled accumulator. A regular
                # connect_after handler never runs after Viewport returns True.
                # Keep the complete product render path and observe its return
                # locally, once, without modifying or replacing product code.
                result = super()._render(area, context)
                after_render(area, context)
                return result
        viewport = ProfiledViewport()
        viewport.set_size_request(args.width, args.height)
        viewport.resolution_scale = args.resolution_scale
        viewport.show_grid = viewport.show_pivot = args.annotations == 'grid'
        viewport.show_paths = viewport.show_trails = viewport.show_cameras = False
        viewport.on_stats = lambda values: state.update(last_stats=dict(values))
        viewport.on_error = lambda message: GLib.idle_add(fail, RuntimeError(message))
        window.connect('close-request', lambda *_: (GLib.idle_add(fail, RuntimeError('The benchmark window was closed before completion')), False)[1] if not state['closing'] else False)
        window.set_child(viewport); window.present()
        checkpoint('loading_source_on_worker')
        future = executor.submit(load)
        future.add_done_callback(lambda result: GLib.idle_add(loaded, result))
        GLib.timeout_add(20, ready)
        state['watchdog'] = GLib.timeout_add(100, watchdog)

    window = viewport = None
    app.connect('activate', activated)
    try:
        profile['gtk_application_exit_code'] = app.run([])
        if not profile['passed'] and not profile['errors']:
            profile['errors'].append({'phase': 'gui', 'message': 'The GTK application exited before all measured frames completed'})
    finally:
        cancelled.set()
        executor.shutdown(wait=True, cancel_futures=True)
        if state['document'] is not None:
            state['document'].close()
        profile['gui_session_ms'] = (time.perf_counter()-began)*1000
        profile['unattributed_initial_or_idle_redraws'] = state['unattributed_renders']
        profile['summary'] = summarize(profile['samples'])


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scene', required=True, type=Path, help='Existing real production GSV; opened read-only')
    parser.add_argument('--output', required=True, type=Path, help='A NEW local directory; existing directories are refused')
    parser.add_argument('--backend', choices=('environment', 'x11', 'wayland'), default='environment')
    parser.add_argument('--camera', default='first', help='first compatible source camera, explicit camera ID, or scene for bounds framing')
    parser.add_argument('--view', type=int, default=0)
    parser.add_argument('--field', action='append', help='Explicit compatible source field ID; repeat for multiple fields')
    parser.add_argument('--width', type=int, default=1280, help='GLArea logical width; actual physical render size is recorded')
    parser.add_argument('--height', type=int, default=720, help='GLArea logical height')
    parser.add_argument('--resolution-scale', type=float, default=1.)
    parser.add_argument('--samples', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--intervals', type=int, default=4)
    parser.add_argument('--start-time', type=float)
    parser.add_argument('--picks', type=int, default=5)
    parser.add_argument('--sort-mode', choices=('auto', 'cpu', 'gpu'), default='auto')
    parser.add_argument('--cache-mib', type=float, default=256.)
    parser.add_argument('--annotations', choices=('none', 'grid'), default='grid')
    parser.add_argument('--require-hardware', action='store_true')
    parser.add_argument('--wait-output', type=float, default=0., metavar='SECONDS', help='Wait for an externally kept-alive real Mutter output; never create one')
    parser.add_argument('--frame-timeout', type=float, default=120.)
    parser.add_argument('--load-timeout', type=float, default=600.)
    parser.add_argument('--snapshot', action='store_true', help='Export only this benchmark viewport, never the desktop')
    parser.add_argument('--stat-only-integrity', action='store_true', help='Skip whole-file SHA256 before/after; report stat-only integrity explicitly')
    parser.add_argument('--note', action='append', default=[], help='Record known concurrent jobs / experiment conditions')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    if min(args.width, args.height, args.samples, args.intervals) < 1 or min(args.warmup, args.picks, args.view) < 0:
        parser.error('Dimensions/samples/intervals must be positive; warmup/picks/view must be nonnegative')
    if not .1 <= args.resolution_scale <= 1 or not math.isfinite(args.cache_mib) or args.cache_mib <= 0:
        parser.error('Resolution scale must be .1..1 and cache MiB must be finite and positive')
    if not all(math.isfinite(value) and value >= 0 for value in (args.wait_output, args.frame_timeout, args.load_timeout)) or min(args.frame_timeout, args.load_timeout) == 0:
        parser.error('Wait/timeout must be finite and nonnegative; frame timeout must be positive')
    if args.start_time is not None and not math.isfinite(args.start_time):
        parser.error('Start time must be finite')
    args.scene = args.scene.resolve(strict=True)
    args.output = args.output.absolute()
    return args


def main(argv=None):
    args = arguments(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    profile = {'format': 'gsv.production-viewport-profile/1', 'passed': False, 'errors': [],
        'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'scene': str(args.scene),
        'source_root': str(ROOT), 'source_commit': revision,
        'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'python': sys.version, 'platform': platform.platform(), 'pid': os.getpid(),
        'arguments': {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        'notes': args.note,
        'measurement_definition': {
            'context': 'Real GTK window and Gtk.GLArea normal render lifecycle',
            'request_mode': 'One frame in flight; serialized latency, not maximum throughput',
            'gpu_ms': 'Native asynchronous GPU elapsed queries for sort+render; excludes CPU preparation/upload submission',
            'completion_wall': 'Nonblocking GL fence after GLArea render; includes main-loop/poll delay, excludes later GTK/compositor presentation',
            'cpu_submission_ms': 'Normal viewport render callback CPU wall time, including upload/sort submission',
            'cold': 'First operation in this process, NOT cold storage; OS cache is uncontrolled and source hashing/validation can warm it',
            'rss': 'Linux process RSS/high-water mark including NumPy/mmap/native allocations',
            'resident_bytes': 'Renderer declared allocation estimate, not total driver VRAM',
            'source_integrity': ('Stat-only before/after; validated source manifest digest is recorded, '
                'but whole-file byte identity is not cryptographically asserted' if args.stat_only_integrity else
                'Full SHA256 and device/inode/size/mtime/ctime before/after'),
            'interaction': 'Programmatic production navigation methods; no synthesized desktop input',
            'annotations': args.annotations+' only; camera/path extraction is not part of this geometry benchmark',
            'presentation_fps_measured': False}}
    try:
        profile['source_before'] = fingerprint(args.scene, digest=not args.stat_only_integrity)
        run_gui(args, profile)
    except Exception as error:
        profile['passed'] = False
        profile['errors'].append({'phase': 'runner', 'type': type(error).__name__, 'message': str(error),
                                  'traceback': ''.join(traceback.format_exception(error))})
    finally:
        try:
            profile['source_after'] = fingerprint(args.scene, digest=not args.stat_only_integrity)
            before, after = profile.get('source_before'), profile['source_after']
            profile['source_unchanged'] = bool(before and before['stat'] == after['stat'] and before['sha256'] == after['sha256'])
            if not profile['source_unchanged']:
                profile['passed'] = False
                profile['errors'].append({'phase': 'integrity', 'message': 'Source before/after stat or SHA256 differs'})
        except Exception as error:
            profile['passed'] = False
            profile['errors'].append({'phase': 'integrity', 'message': str(error)})
        profile['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        temporary = args.output/'profile.json.tmp'
        temporary.write_text(json.dumps(profile, indent=2, allow_nan=False))
        temporary.replace(args.output/'profile.json')
    print(json.dumps({'passed': profile['passed'], 'profile': str(args.output/'profile.json'),
                      'source_unchanged': profile.get('source_unchanged'), 'errors': profile['errors']}), flush=True)
    return 0 if profile['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
