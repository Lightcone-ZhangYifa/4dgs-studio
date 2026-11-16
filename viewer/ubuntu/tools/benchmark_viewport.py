#!/usr/bin/env python3
"""Reproducible preview timings, including explicit GPU completion.

Run with a real GPU for desktop results. Software Mesa verifies functionality,
not representative hardware performance. The benchmark never writes scene data.
"""
from __future__ import annotations
import argparse
import json
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
    args = parser.parse_args()
    if args.iterations < 1 or any(n < 1 for n in args.counts):
        parser.error('counts and iterations must be positive')
    app = Gtk.Application(application_id='org.gsv.Studio.Benchmark')
    result = {}
    failure = []

    def activated(app):
        window = Gtk.ApplicationWindow(application=app, default_width=args.width, default_height=args.height)
        viewport = Viewport()
        viewport.show_grid = viewport.show_paths = viewport.show_trails = viewport.show_cameras = False
        window.set_child(viewport)
        window.present()

        def run():
            try:
                viewport.make_current()
                if viewport.render_error:
                    raise RuntimeError(viewport.render_error)
                renderer = viewport._renderer
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
                    times = []
                    for index in range(args.iterations + 1):
                        viewport.navigation.orbit(2, 0)
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
                        times.append({'submission_ms': (submitted-start)*1000, 'completed_ms': (finished-start)*1000})
                    result['measurements'].append({'splats': count, 'first_upload': times[0], 'orbit_median_submission_ms': float(np.median([t['submission_ms'] for t in times[1:]])), 'orbit_median_completed_ms': float(np.median([t['completed_ms'] for t in times[1:]])), 'geometry_bytes': count*64, 'orbit_index_upload_bytes': count*4})
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
