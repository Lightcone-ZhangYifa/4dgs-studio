#!/usr/bin/env python3
"""Reproducible CPU-only source validation, endpoint and sparse-row benchmarks.

    python viewer/ubuntu/tools/benchmark_scene_io.py generate /tmp/gsv-scene-io
    python viewer/ubuntu/tools/benchmark_scene_io.py million /tmp/gsv-scene-io
    python viewer/ubuntu/tools/benchmark_scene_io.py scaffold /tmp/gsv-scene-io

Generation refuses existing scene files. Each measurement runs in a fresh Python
subprocess, never in the generation process, and prints one JSON result. "Cold"
means the first application operation after complete source validation, not cold
storage: the OS page cache is neither flushed nor controlled. No GTK, display,
OpenGL context or GPU is used.
"""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'viewer/ubuntu'), str(ROOT / 'packages/gsv/src')]

import numpy as np

MILLION_COUNT = 1_000_000
SCAFFOLD_COUNT = 800_000
SCAFFOLD_SAMPLES = 8
MIB = 1024**2


def memory():
    """Linux process RSS/high water mark, including native NumPy allocations."""
    status = Path('/proc/self/status').read_text().splitlines()
    values = {line.split(':', 1)[0]: int(line.split()[1])*1024
              for line in status if line.startswith(('VmRSS:', 'VmHWM:'))}
    return {'rss_MiB': values['VmRSS']/MIB, 'vmhwm_MiB': values['VmHWM']/MIB}


def environment():
    model = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                  if line.startswith('model name')), platform.machine())
    try:
        revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                           stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    return {'cpu': model, 'logical_cpus': os.cpu_count(),
            'affinity_cpus': len(os.sched_getaffinity(0)),
            'platform': platform.platform(), 'python': platform.python_version(),
            'numpy': np.__version__, 'source_commit': revision, 'worker_pid': os.getpid(),
            'thread_environment': {key: os.environ[key] for key in
                ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS')
                if key in os.environ}}


class PreadCounter(AbstractContextManager):
    """Count source Python pread APIs, restoring both originals on every exit.

    Counts actual returned bytes separately from requested bytes. These are
    process/API reads, not disk traffic; ordinary file.read and mmap faults are
    not counted. The source inode filter excludes unrelated file descriptors.
    """
    def __init__(self, source):
        stat = source.stat()
        self.identity = (stat.st_dev, stat.st_ino)
        self.original_pread = os.pread
        self.original_preadv = os.preadv
        self.descriptors = {}
        self.reset()

    def reset(self):
        self.calls = {name: {'calls': 0, 'requested_bytes': 0, 'returned_bytes': 0}
                      for name in ('pread', 'preadv')}

    def _source(self, fd):
        if fd not in self.descriptors:
            stat = os.fstat(fd)
            self.descriptors[fd] = (stat.st_dev, stat.st_ino) == self.identity
        return self.descriptors[fd]

    def _pread(self, fd, count, offset):
        tracked = self._source(fd)
        if tracked:
            self.calls['pread']['calls'] += 1
            self.calls['pread']['requested_bytes'] += count
        result = self.original_pread(fd, count, offset)
        if tracked:
            self.calls['pread']['returned_bytes'] += len(result)
        return result

    def _preadv(self, fd, buffers, offset, *args):
        buffers = tuple(buffers)
        tracked = self._source(fd)
        if tracked:
            self.calls['preadv']['calls'] += 1
            self.calls['preadv']['requested_bytes'] += sum(memoryview(buffer).nbytes for buffer in buffers)
        result = self.original_preadv(fd, buffers, offset, *args)
        if tracked:
            self.calls['preadv']['returned_bytes'] += result
        return result

    def snapshot(self):
        result = {name: values.copy() for name, values in self.calls.items()}
        result['total_calls'] = sum(values['calls'] for values in self.calls.values())
        result['total_returned_bytes'] = sum(values['returned_bytes'] for values in self.calls.values())
        return result

    def __enter__(self):
        os.pread, os.preadv = self._pread, self._preadv
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        os.pread, os.preadv = self.original_pread, self.original_preadv
        return False


def generate(directory):
    from gsv import Writer, add_persistent_gaussians

    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / name for name in ('million.gsv', 'scaffold.gsv')]
    # Preflight both paths before allocating/writing either dataset. A dangling
    # symlink is also an existing destination and must not be replaced silently.
    for path in paths:
        if path.exists() or path.is_symlink():
            raise FileExistsError(f'{path} already exists; choose a new output directory')

    records = np.zeros((MILLION_COUNT, 16), np.float32)
    records[:, 0] = np.linspace(-2, 2, MILLION_COUNT)
    records[:, 2], records[:, 3] = 3, .5
    records[:, 4:7], records[:, 7:9] = .001, 1
    records[:, 12:15], records[:, 15] = [.3, .5, .8], -1
    def frames():
        yield records
        records[:, 1] = .5
        yield records
    with Writer(paths[0], overwrite=False, validate=True) as writer:
        add_persistent_gaussians(writer, 'field', np.arange(MILLION_COUNT, dtype=np.int64),
                                 [0., 1.], frames())
    del records

    positions = np.zeros((SCAFFOLD_SAMPLES, SCAFFOLD_COUNT, 3), np.float32)
    positions[:, :, 0] = np.linspace(-2, 2, SCAFFOLD_COUNT)
    positions[:, :, 1] = np.arange(SCAFFOLD_SAMPLES)[:, None]*.1
    positions[:, :, 2] = 3
    with Writer(paths[1], overwrite=False, validate=True) as writer:
        writer.manifest['spaces']['world'] = {'unit': 'meter', 'axes': ['x','y','z'], 'handedness': 'right'}
        writer.manifest['clocks']['scene'] = {'unit': 'second'}
        arrays = {'ids': np.arange(SCAFFOLD_COUNT, dtype=np.int64),
                  'times': np.arange(SCAFFOLD_SAMPLES, dtype=np.float64), 'positions': positions}
        data = {name: writer.add_array(name, values) for name, values in arrays.items()}
        writer.add_object('graph', 'scaffold', 'gsv.scaffold.graph', data, space='world', clock='scene')
    return {'schema_version': 1, 'mode': 'generate', 'source_validation': 'Writer(validate=True)',
            'generation_only': True, 'measurement_memory_not_collected': True,
            'datasets': [{'path': str(path.resolve()), 'bytes': path.stat().st_size} for path in paths],
            'million': {'identities': MILLION_COUNT, 'samples': 2, 'record_stride_bytes': 64},
            'scaffold': {'identities': SCAFFOLD_COUNT, 'samples': SCAFFOLD_SAMPLES,
                         'positions_bytes': positions.nbytes}}


def timed(counter, operation):
    counter.reset()
    started = time.perf_counter()
    result = operation()
    elapsed = (time.perf_counter()-started)*1000
    return result, {'ms': elapsed, 'python_source_reads': counter.snapshot(), 'memory': memory()}


def summary(values):
    return {'iterations': len(values), 'median_ms': float(np.median(values)),
            'p95_ms': float(np.percentile(values, 95)), 'min_ms': float(np.min(values)),
            'max_ms': float(np.max(values))}


def measure_million(document, counter, iterations):
    frame, first = timed(counter, lambda: document.prepare_frame(.25))
    if frame.count != MILLION_COUNT:
        raise AssertionError(f'Expected {MILLION_COUNT} identities, got {frame.count}')
    warm = []
    counter.reset()
    for value in np.linspace(.3, .8, iterations):
        started = time.perf_counter()
        state = document.prepare_frame(float(value))
        warm.append((time.perf_counter()-started)*1000)
        if state.first is not frame.first or state.second is not frame.second:
            raise AssertionError('Warm interpolation did not reuse its immutable source endpoints')
    warm_reads = counter.snapshot()
    return {'identities': frame.count, 'samples': 2, 'first_time': .25,
            'cold_prepare': first, 'warm_prepare': dict(summary(warm), time_range=[.3,.8],
                python_source_reads=warm_reads, endpoint_storage_reused=True)}


def measure_scaffold(document, counter, iterations):
    counter.reset()
    obj = document.object('graph')
    ids = document.scene.array(obj['data']['ids'])
    times = document.object_times('graph')
    if len(ids) != SCAFFOLD_COUNT or len(times) != SCAFFOLD_SAMPLES:
        raise AssertionError('Scaffold dimensions differ from the generated benchmark contract')
    position_array = document.manifest['arrays'][obj['data']['positions']]
    positions_bytes = int(np.prod(position_array['shape']))*np.dtype(np.float32).itemsize
    setup_reads = counter.snapshot()
    element, inspection = timed(counter, lambda: document.element('graph', 42, 2.5))
    paths, trajectory = timed(counter, lambda: document.trajectories('graph', ids=[42], density=0))
    if element is None or element.id != 42 or len(paths) != 1 or len(paths[0].times) != SCAFFOLD_SAMPLES:
        raise AssertionError('Sparse inspection/trajectory omitted the requested identity or native samples')
    np.testing.assert_allclose(element.position, [-2+4*42/(SCAFFOLD_COUNT-1), .25, 3], atol=1e-6)
    np.testing.assert_array_equal(paths[0].times, np.arange(SCAFFOLD_SAMPLES, dtype=np.float64))
    np.testing.assert_allclose(paths[0].positions[:,1], np.arange(SCAFFOLD_SAMPLES)*.1, atol=1e-6)
    warm_inspect, warm_paths = [], []
    counter.reset()
    for _ in range(iterations):
        started = time.perf_counter()
        document.element('graph', 42, 2.5)
        warm_inspect.append((time.perf_counter()-started)*1000)
        started = time.perf_counter()
        document.trajectories('graph', ids=[42], density=0)
        warm_paths.append((time.perf_counter()-started)*1000)
    sparse_bytes = sum(stage['python_source_reads']['total_returned_bytes'] for stage in (inspection, trajectory))
    if sparse_bytes >= positions_bytes:
        raise AssertionError('Selected inspection/trajectory read the full scaffold position array')
    return {'identities': len(ids), 'samples': len(times), 'selected_identity': 42,
            'positions_array_bytes': positions_bytes, 'setup_python_source_reads': setup_reads,
            'cold_selected_element': inspection,
            'cold_selected_trajectory': trajectory, 'cold_sparse_returned_bytes': sparse_bytes,
            'warm_selected_element': summary(warm_inspect), 'warm_selected_trajectory': summary(warm_paths),
            'warm_python_source_reads': counter.snapshot(), 'path_samples_verified': len(paths[0].times),
            'snapshot_cache_MiB': document.scene._snapshot_bytes/MIB,
            'snapshot_budget_MiB': document.scene.snapshot_budget/MIB}


def measure(mode, directory, iterations):
    from gs_studio.model import SceneDocument

    source = (directory / (mode+'.gsv')).resolve(strict=True)
    original = source.stat()
    result = {'schema_version': 1, 'mode': mode, 'environment': environment(),
              'source': {'path': str(source), 'bytes': original.st_size, 'MiB': original.st_size/MIB},
              'methodology': {
                  'process': 'Fresh measurement subprocess; no dataset generation in this process',
                  'cold': 'First application operation after full source validation; OS page cache not flushed or controlled',
                  'warm': 'Same validated document and selected interval/identity, repeated operations',
                  'memory': 'Linux /proc/self/status VmHWM and VmRSS; includes native allocations, excludes generation process',
                  'reads': 'Instrumented Python os.pread/os.preadv source-inode calls and actual returned bytes; not physical disk I/O',
                  'timing': 'CPU wall time; no display or GPU; pread counter overhead is included'},
              'startup_memory': memory()}
    # A finally block inside the context manager restores both os functions
    # even if validation, an assertion or one of the measured operations fails.
    with PreadCounter(source) as counter:
        document, validation = timed(counter, lambda: SceneDocument(source))
        try:
            result['validation'] = dict(validation, validated=True,
                                        manifest_sha256=document.manifest_sha256)
            result['measurements'] = (measure_million if mode == 'million' else measure_scaffold)(document, counter, iterations)
            document.scene._unchanged()
            final = source.stat()
            if (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns) != (
                    final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
                raise AssertionError('Source changed during the measurement')
            result['source_unchanged'] = True
            result['final_memory'] = memory()
        finally:
            document.close()
    result['pread_functions_restored'] = (os.pread is counter.original_pread and os.preadv is counter.original_preadv)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('generate', 'million', 'scaffold'))
    parser.add_argument('outputdir', type=Path)
    parser.add_argument('--iterations', type=int, default=101, help='Warm operations per benchmark (default: 101)')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('--iterations must be positive')
    try:
        if args.mode == 'generate':
            result = generate(args.outputdir)
        elif args.worker:
            result = measure(args.mode, args.outputdir, args.iterations)
        else:
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), args.mode,
                str(args.outputdir.resolve()), '--iterations', str(args.iterations), '--worker'],
                check=False, text=True, stdout=subprocess.PIPE)
            if completed.returncode:
                return completed.returncode
            # Only a worker's completed JSON is forwarded; generation and parent
            # interpreter peaks cannot become the reported measurement VmHWM.
            result = json.loads(completed.stdout)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, AssertionError) as error:
        print(json.dumps({'mode': args.mode, 'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
