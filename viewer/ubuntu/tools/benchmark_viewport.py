#!/usr/bin/env python3
"""Reproducible preview timings, including explicit GPU completion.

Run with a real GPU for desktop results. Software Mesa verifies functionality,
not representative hardware performance. The benchmark never writes scene data.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib
from gs_studio.viewport import Viewport
from OpenGL import GL as gl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', type=int, nargs='+', default=[100_000, 1_000_000])
    parser.add_argument('--iterations', type=int, default=3)
    parser.add_argument('--width', type=int, default=960)
    parser.add_argument('--height', type=int, default=640)
    parser.add_argument('--sort-mode', choices=['auto', 'cpu', 'gpu'], default='auto')
    parser.add_argument('--pick', action='store_true', help='Measure five exact GPU picks after the resident frame')
    parser.add_argument('--operation', choices=['orbit', 'pan', 'dolly', 'playback'], default='orbit')
    args = parser.parse_args()
    if args.iterations < 1 or any(n < 1 for n in args.counts):
        parser.error('counts and iterations must be positive')
    app = Gtk.Application(application_id='org.gsv.Studio.Benchmark')
    result = {}
    failure = []

    def activated(app):
        window = Gtk.ApplicationWindow(application=app, default_width=args.width, default_height=args.height)
        viewport = Viewport()
        viewport.set_size_request(args.width, args.height)
        viewport.show_grid = viewport.show_paths = viewport.show_trails = viewport.show_cameras = False
        window.set_child(viewport)
        window.present()

        def run():
            try:
                viewport.make_current()
                if viewport.render_error:
                    raise RuntimeError(viewport.render_error)
                renderer = viewport._renderer
                renderer.sort_mode = args.sort_mode
                if args.sort_mode == 'gpu' and not renderer.gpu_sorter.supported:
                    raise RuntimeError(renderer.gpu_sorter.reason)
                result['vendor'] = gl.glGetString(gl.GL_VENDOR).decode()
                result['display_backend'] = os.environ.get('GDK_BACKEND', 'automatic')
                result['context_type'] = viewport.get_context().__gtype__.name
                result['operation'] = args.operation
                result['requested_sort_mode'] = args.sort_mode
                result['gpu_sort_available'] = renderer.gpu_sorter.supported
                result['gpu_sort_reason'] = renderer.gpu_sorter.reason
                result['renderer'] = gl.glGetString(gl.GL_RENDERER).decode()
                result['version'] = gl.glGetString(gl.GL_VERSION).decode()
                result['measurements'] = []
                destination = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
                k, pose, width, height = viewport.get_camera()
                result['resolution'] = [width, height]
                rng = np.random.default_rng(481)
                for count in args.counts:
                    means = rng.uniform(-1., 1., size=(count, 3)).astype(np.float32)
                    covariance = np.zeros((count, 3, 3), np.float32)
                    covariance[:, range(3), range(3)] = [.0001, .0002, .0001]
                    frame = SimpleNamespace(means=means, covariance=covariance, colors=rng.uniform(.1, 1., (count, 3)).astype(np.float32), opacity=np.full(count, .7, np.float32), ids=np.arange(count, dtype=np.int64), object_ids=np.full(count, 'synthetic'))
                    if args.operation == 'playback':
                        first = np.zeros((count,16),np.float32)
                        first[:,:3] = means;first[:,3] = .7
                        first[:,4:7] = np.sqrt([.0001,.0002,.0001]);first[:,7:9] = 1
                        first[:,12:15] = frame.colors;first[:,15] = -1
                        second = first.copy();second[:,0] += .15
                        second[:,4:7] *= [1.2,.9,1.1]
                        second[:,8:12] = [np.cos(.2),0,0,np.sin(.2)]
                        prepared = dict(is_prepared=True,first=first,second=second,weights=None,ids=frame.ids,object_ids=frame.object_ids,endpoint_key=('benchmark',count))
                    times = []
                    for index in range(args.iterations + 1):
                        if args.operation == 'orbit':
                            viewport.navigation.orbit(2, 0)
                        elif args.operation == 'pan':
                            viewport.navigation.pan(2, 0, k)
                        elif args.operation == 'dolly':
                            viewport.navigation.dolly(.5)
                        else:
                            frame = SimpleNamespace(**prepared,weight=.8*index/(args.iterations+1))
                        _, pose, _, _ = viewport.get_camera()
                        gl.glFinish()
                        start = time.perf_counter()
                        renderer.upload(frame, pose, set())
                        renderer.target(width, height)
                        renderer.begin(k, pose, width, height)
                        renderer.splats()
                        renderer.splats(depth_only=True)
                        renderer.finish(destination, width, height)
                        submitted = time.perf_counter()
                        gl.glFinish()
                        finished = time.perf_counter()
                        times.append({'submission_ms': (submitted-start)*1000, 'completed_ms': (finished-start)*1000, 'sort_submission_ms': renderer.last_sort_submission_ms, 'sort_reused': renderer.last_sort_reused})
                    pick_times = []
                    if args.pick:
                        for _ in range(5):
                            started = time.perf_counter()
                            renderer.pick(k,pose,width,height,[width/2,height/2])
                            pick_times.append((time.perf_counter()-started)*1000)
                    result['measurements'].append({'pick_median_ms': float(np.median(pick_times)) if pick_times else None, 'pick_p95_ms': float(np.percentile(pick_times,95)) if pick_times else None, 'splats': count, 'first_upload': times[0], 'orbit_median_submission_ms': float(np.median([t['submission_ms'] for t in times[1:]])), 'orbit_median_completed_ms': float(np.median([t['completed_ms'] for t in times[1:]])), 'p95_completed_ms': float(np.percentile([t['completed_ms'] for t in times[1:]], 95)), 'median_sort_submission_ms': float(np.median([t['sort_submission_ms'] for t in times[1:]])), 'sort_backend': renderer.sort_backend, 'sort_reason': renderer.sort_reason, 'reused_order_frames': sum(t['sort_reused'] for t in times[1:]), 'geometry_bytes': count*(128 if args.operation == 'playback' else 64), 'resident_order_bytes': count*4, 'orbit_index_upload_bytes': count*4 if renderer.sort_backend == 'cpu' and not any(t['sort_reused'] for t in times[1:]) else 0})
                print(json.dumps(result, indent=2))
            except Exception as error:
                failure.append(error)
            window.close()
            app.quit()
            return False
        GLib.timeout_add(100, run)

    app.connect('activate', activated)
    app.run([])
    if failure:
        raise failure[0]


if __name__ == '__main__':
    main()
