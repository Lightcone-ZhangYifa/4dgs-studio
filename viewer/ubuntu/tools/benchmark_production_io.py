#!/usr/bin/env python3
"""Read-only production GSV loading, endpoint reuse and sparse inspection timings.

Cold means first application operation, with no OS page-cache control. Source
pread counters measure API traffic, not physical disk reads. No GTK/GPU is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'viewer/ubuntu'), str(ROOT / 'packages/gsv/src')]

import numpy as np
from benchmark_scene_io import PreadCounter, environment, memory, summary, timed
from gs_studio.model import SceneDocument


def signature(path):
    s = path.stat()
    return dict(device=s.st_dev, inode=s.st_ino, size=s.st_size,
                mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns)


def measure(args):
    source = args.scene.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = signature(source)
    report = dict(schema_version=1, environment=environment(), source=str(source),
                  source_before=before, scope=__doc__, intervals=[], success=False)
    report['runtime_sha256'] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (ROOT/'viewer/ubuntu/gs_studio/model.py', ROOT/'viewer/ubuntu/gs_studio/camera.py')}
    document = None
    try:
        with PreadCounter(source) as counter:
            document, report['load'] = timed(counter, lambda: SceneDocument(source))
            report.update(manifest_sha256=document.manifest_sha256,
                          visible=document.visible_ids, objects=len(document.objects),
                          cameras=len(document.cameras), render_views=len(document.render_views),
                          times=document.times.tolist(), cache_budget_bytes=document.cache_budget)
            times = document.times
            indices = sorted(set([*range(min(args.intervals, max(1, len(times)-1))),
                                  max(0, (len(times)-1)//2), max(0, len(times)-2)]))
            for index in indices:
                a, b = float(times[index]), float(times[min(index+1, len(times)-1)])
                frame, cold = timed(counter, lambda: document.prepare_frame(a+(b-a)*.25))
                warm, reuse = [], []
                counter.reset()
                for w in np.linspace(.3, .8, args.iterations):
                    start = time.perf_counter()
                    next_frame = document.prepare_frame(a+(b-a)*float(w))
                    warm.append((time.perf_counter()-start)*1000)
                    reuse.append(next_frame.first is frame.first and next_frame.second is frame.second)
                warm_reads = counter.snapshot()
                # Independently scan the stored masks outside the measured
                # region; cached counts must retain every source identity.
                valid = 0
                for _, start, stop, weight in next_frame.segments:
                    left, right = next_frame.first[start:stop,7] > .5, next_frame.second[start:stop,7] > .5
                    valid += int(np.count_nonzero(left if weight == 0 else right if weight == 1 else left & right))
                if valid != next_frame.valid_count:
                    raise AssertionError('Prepared validity count differs from stored source masks')
                report['intervals'].append(dict(index=index, time_range=[a,b], count=len(frame),
                    valid_count=getattr(frame, 'valid_count', len(frame)), cold_prepare=cold,
                    warm_prepare=dict(summary(warm), python_source_reads=warm_reads,
                                      all_endpoint_storage_reused=all(reuse)),
                    validity_count_verified=True, cache_bytes=document._cache_bytes, memory=memory()))
            owner = next((x for x in document.visible_ids
                          if document.object(x)['type'] == 'gaussian'), None)
            if owner:
                obj = document.object(owner)
                ids = document.scene.array(obj['data']['ids'])
                if len(ids):
                    identity = int(ids[len(ids)//2])
                    element, inspection = timed(counter, lambda: document.element(owner, identity, float(times[0])))
                    paths, trajectory = timed(counter, lambda: document.trajectories(owner, ids=[identity], density=0))
                    report['selected_identity'] = dict(owner=owner, identity=identity,
                        element_found=element is not None, inspection=inspection, trajectory=trajectory,
                        paths=len(paths), path_samples=sum(len(p.times) for p in paths))
            calibration_times = []
            for value in (float(times[0]), float(times[len(times)//2]), float(times[-1])):
                started = time.perf_counter()
                count = 0
                for camera in document.cameras:
                    if camera.get('space') == document.space and camera.get('clock') == document.clock:
                        for view in range(len(camera['data']['view_ids'])):
                            document.camera(camera['id'], value, view)
                            count += 1
                calibration_times.append(dict(time=value, views=count, ms=(time.perf_counter()-started)*1000))
            report['all_camera_calibrations'] = calibration_times
            report['memory'] = memory()
            report['success'] = True
    except BaseException as error:
        report.update(error=str(error), traceback=traceback.format_exc())
        raise
    finally:
        if document is not None:
            document.close()
        report['source_after'] = signature(source)
        report['source_unchanged'] = before == report['source_after']
        report['success'] = report['success'] and report['source_unchanged']
        (output / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
    if not report['success']:
        raise RuntimeError('Production source changed during measurement')
    print(json.dumps(dict(output=str(output), load_ms=report['load']['ms'],
                         counts=[i['count'] for i in report['intervals']],
                         warm_ms=[i['warm_prepare']['median_ms'] for i in report['intervals']],
                         source_unchanged=report['source_unchanged'])))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scene', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory')
    parser.add_argument('--iterations', type=int, default=15)
    parser.add_argument('--intervals', type=int, default=4)
    args = parser.parse_args()
    if args.iterations < 1 or args.intervals < 1:
        parser.error('iterations and intervals must be positive')
    measure(args)
