"""Stable compute-sort acceptance, CPU fallback and optional resident benchmark."""
import json
import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np
from gs_studio.gpu_sort import GpuSorter, depth_order_cpu


class CpuSortTests(unittest.TestCase):
    def test_signed_zero_nan_infinity_and_stable_ties(self):
        means = np.zeros((9, 3), np.float32)
        means[:, 2] = [2, -1, 2, 0., -0., np.inf, -np.inf, np.nan, 1]
        np.testing.assert_array_equal(depth_order_cpu(means, [0, 0, 1]), [5, 0, 2, 8, 3, 4, 1, 6, 7])

    def test_translation_is_ignored_and_float32_steps_are_explicit(self):
        points = np.array([[1e8, 1, -1e8], [0, .5, 0]], np.float32)
        transform = np.eye(4, dtype=np.float64)
        transform[2] = [1, 1, 1, 1e30]
        np.testing.assert_array_equal(depth_order_cpu(points, transform), [1, 0])

    def test_prepared_endpoints_and_mixed_weights_keep_stable_ties(self):
        first = np.zeros((4,3), np.float32)
        first[:,2] = [4, 3, 2, 1]
        second = first[::-1].copy()
        np.testing.assert_array_equal(depth_order_cpu(first,[0,0,1],second_means=second,weight=0), [0,1,2,3])
        np.testing.assert_array_equal(depth_order_cpu(first,[0,0,1],second_means=second,weight=.5), [0,1,2,3])
        np.testing.assert_array_equal(depth_order_cpu(first,[0,0,1],second_means=second,weight=1), [3,2,1,0])

    def test_opengl33_falls_back_without_allocating_compute_objects(self):
        with mock.patch("gs_studio.gpu_sort.gl.glGetIntegerv", side_effect=[3, 3]), mock.patch("gs_studio.gpu_sort.gl.glGenBuffers") as allocate:
            sorter = GpuSorter()
            self.assertFalse(sorter.initialize())
            self.assertIn("4.3", sorter.reason)
            allocate.assert_not_called()
            with self.assertRaises(RuntimeError):
                sorter.sort(1, 2, 1, [0, 0, 1])
            sorter.close()


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"), "Compute acceptance requires a GL display; use Xvfb or a native GPU context")
class GpuSortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk, Gdk, GLib
        from OpenGL import GL
        cls.GLib, cls.gl = GLib, GL
        Gtk.init()
        cls.area = Gtk.GLArea()
        cls.area.set_required_version(4, 3)
        if hasattr(cls.area, "set_allowed_apis"):
            cls.area.set_allowed_apis(Gdk.GLAPI.GL)
        cls.area.connect("render", lambda *_: True)
        cls.window = Gtk.Window(title="4DGS Studio GPU sort checks", child=cls.area, default_width=160, default_height=100)
        cls.window.present()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            context = GLib.MainContext.default()
            for _ in range(20):
                if not context.pending():
                    break
                context.iteration(False)
            if cls.area.get_realized():
                cls.area.make_current()
                if cls.area.get_error():
                    cls.window.destroy()
                    raise unittest.SkipTest(str(cls.area.get_error()))
                break
            threading.Event().wait(.005)
        else:
            cls.window.destroy()
            raise RuntimeError("GL sort test context did not realize")
        cls.sorter = GpuSorter(force=True)
        if not cls.sorter.initialize():
            cls.window.destroy()
            raise RuntimeError(cls.sorter.reason)

    @classmethod
    def tearDownClass(cls):
        cls.area.make_current()
        cls.sorter.close()
        cls.window.destroy()

    def setUp(self):
        self.area.make_current()
        self.buffers = []

    def tearDown(self):
        if self.buffers:
            self.gl.glDeleteBuffers(len(self.buffers), self.buffers)

    def resident(self, points):
        gl = self.gl
        n = len(points)
        data = np.zeros((n, 16), np.float32)
        data[:, :3] = points
        buffers = [int(x) for x in gl.glGenBuffers(2)]
        self.buffers.extend(buffers)
        for buffer, size, source in ((buffers[0], max(64, data.nbytes), data if n else None),
                                     (buffers[1], max(4, n*4), None)):
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, buffer)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, size, source, gl.GL_DYNAMIC_DRAW)
        return buffers

    def read_order(self, buffer, count):
        self.gl.glBindBuffer(self.gl.GL_ARRAY_BUFFER, buffer)
        return np.frombuffer(self.gl.glGetBufferSubData(self.gl.GL_ARRAY_BUFFER, 0, count*4), np.uint32).copy()

    def test_multiple_groups_partial_group_ties_and_buffer_reuse(self):
        rng = np.random.default_rng(188)
        for count in (0, 1, 255, 256, 257, 4099, 8193, 7):
            with self.subTest(count=count):
                points = rng.integers(-4, 5, (count, 3)).astype(np.float32)
                data, order = self.resident(points)
                direction = np.array([1., -.5, .25], np.float32)
                self.sorter.sort(data, order, count, direction)
                np.testing.assert_array_equal(self.read_order(order, count), depth_order_cpu(points, direction))

    def test_signed_zero_nonfinite_and_precise_float32_cancellation(self):
        points = np.zeros((1029, 3), np.float32)
        points[:, 2] = np.resize([2., -1., 2., -0., 0., np.inf, -np.inf, np.nan], len(points))
        data, order = self.resident(points)
        self.sorter.sort(data, order, len(points), [0, 0, 1])
        np.testing.assert_array_equal(self.read_order(order, len(points)), depth_order_cpu(points, [0, 0, 1]))
        finite = np.tile(np.array([[1e8, 1, -1e8], [0, .5, 0], [0, 0, 0]], np.float32), (257, 1))
        data, order = self.resident(finite)
        self.sorter.sort(data, order, len(finite), [1, 1, 1])
        np.testing.assert_array_equal(self.read_order(order, len(finite)), depth_order_cpu(finite, [1, 1, 1]))

    def test_restores_program_and_storage_bindings(self):
        gl = self.gl
        points = np.array([[0, 0, 2], [0, 0, 1]], np.float32)
        data, order = self.resident(points)
        gl.glUseProgram(0)
        gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, data)
        before = int(gl.glGetIntegerv(gl.GL_SHADER_STORAGE_BUFFER_BINDING))
        bindings = [int(np.asarray(gl.glGetIntegeri_v(gl.GL_SHADER_STORAGE_BUFFER_BINDING, i)).reshape(-1)[0]) for i in range(6)]
        self.sorter.sort(data, order, 2, np.eye(4))
        self.assertEqual(int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM)), 0)
        self.assertEqual(int(gl.glGetIntegerv(gl.GL_SHADER_STORAGE_BUFFER_BINDING)), before)
        self.assertEqual([int(np.asarray(gl.glGetIntegeri_v(gl.GL_SHADER_STORAGE_BUFFER_BINDING, i)).reshape(-1)[0]) for i in range(6)], bindings)

    def weights_buffer(self, values):
        gl = self.gl
        buffer = int(gl.glGenBuffers(1))
        self.buffers.append(buffer)
        values = np.ascontiguousarray(values, np.float32)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, buffer)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, values.nbytes, values, gl.GL_DYNAMIC_DRAW)
        return buffer

    def test_prepared_scalar_and_per_row_weights_with_interleaved_boundaries(self):
        rng = np.random.default_rng(377)
        first = rng.integers(-3,4,(4099,3)).astype(np.float32)
        second = rng.integers(-3,4,(4099,3)).astype(np.float32)
        data, order = self.resident(first)
        other, _ = self.resident(second)
        forward = [1.,-.5,.25]
        for weight in (0., .25, .5, .75, 1.):
            with self.subTest(weight=weight):
                self.sorter.sort(data,order,len(first),forward,second_buffer=other,weight=weight)
                np.testing.assert_array_equal(self.read_order(order,len(first)),
                    depth_order_cpu(first,forward,second_means=second,weight=weight))
        values = np.resize(np.array([0,.25,.5,.75,1.],np.float32),len(first))
        for stride in (1,2):
            interleaved = values if stride == 1 else np.column_stack([values,np.where(values == 0,0,np.where(values == 1,1,2))])
            weights = self.weights_buffer(interleaved)
            self.sorter.sort(data,order,len(first),forward,second_buffer=other,weight_buffer=weights,weight_stride=stride)
            np.testing.assert_array_equal(self.read_order(order,len(first)),
                depth_order_cpu(first,forward,second_means=second,weight=values))

    def test_renderer_pixels_match_cpu_order(self):
        from gs_studio.gl_renderer import Renderer
        gl, rng = self.gl, np.random.default_rng(771)
        points = rng.uniform([-.4, -.3, 1], [.4, .3, 3], (513, 3)).astype(np.float32)
        # Deliberate depth ties exercise source-order alpha composition.
        points[:50, 2] = 2
        frame = SimpleNamespace(means=points, covariance=np.tile(np.diag([.003, .006, .002]), (len(points), 1, 1)).astype(np.float32),
            colors=rng.random((len(points), 3), dtype=np.float32), opacity=np.full(len(points), .3, np.float32),
            ids=np.arange(len(points), dtype=np.int64), object_ids=np.full(len(points), "field"))
        renderer = Renderer()
        renderer.initialize()
        try:
            pose, K = np.eye(4), np.array([[55., 0, 32], [0, 55, 24], [0, 0, 1]])
            renderer.upload(frame, pose, set())
            reference = depth_order_cpu(points, np.eye(4))
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, renderer.order_buffer)
            gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, reference.nbytes, reference)
            def image():
                renderer.target(64, 48)
                renderer.begin(K, pose, 64, 48)
                renderer.splats()
                return bytes(gl.glReadPixels(0, 0, 64, 48, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE))
            cpu = image()
            self.sorter.sort(renderer.buffer, renderer.order_buffer, len(points), np.eye(4))
            gpu = image()
            self.assertEqual(cpu, gpu)
            self.assertGreater(np.frombuffer(cpu, np.uint8).reshape(-1, 4)[:, :3].max(), 50)
        finally:
            renderer.close()

    @unittest.skipUnless(os.environ.get("GSV_GPU_SORT_BENCH"), "Set GSV_GPU_SORT_BENCH=1 for resident sort timings")
    def test_million_resident_sort_benchmark(self):
        points = np.random.default_rng(129).uniform(-10, 10, (1_000_000, 3)).astype(np.float32)
        direction = np.array([.271, -.623, .733], np.float32)
        data, order = self.resident(points)
        gl = self.gl
        self.sorter.sort(data, order, len(points), direction)
        gl.glFinish()
        gpu, cpu = [], []
        for _ in range(5):
            gl.glFinish()
            start = time.perf_counter()
            self.sorter.sort(data, order, len(points), direction)
            gl.glFinish()
            gpu.append((time.perf_counter()-start)*1000)
            start = time.perf_counter()
            expected = depth_order_cpu(points, direction)
            cpu.append((time.perf_counter()-start)*1000)
        np.testing.assert_array_equal(self.read_order(order, len(points)), expected)
        print(json.dumps({"renderer": gl.glGetString(gl.GL_RENDERER).decode(), "count": len(points),
                          "gpu_completed_ms": gpu, "cpu_ms": cpu,
                          "gpu_median_ms": float(np.median(gpu)), "cpu_median_ms": float(np.median(cpu))}), flush=True)

    @unittest.skipUnless(os.environ.get("GSV_GPU_SORT_BENCH"), "Set GSV_GPU_SORT_BENCH=1 for resident prepared-sort timings")
    def test_million_prepared_mixed_weights_benchmark(self):
        rng = np.random.default_rng(422)
        first, second = [rng.uniform(-10,10,(1_000_000,3)).astype(np.float32) for _ in range(2)]
        values = rng.uniform(0,1,len(first)).astype(np.float32)
        interleaved = np.column_stack([values,np.full(len(first),2,np.float32)])
        data, order = self.resident(first)
        other, _ = self.resident(second)
        weights = self.weights_buffer(interleaved)
        direction = np.array([.271,-.623,.733],np.float32)
        gl = self.gl
        def sort():
            self.sorter.sort(data,order,len(first),direction,second_buffer=other,weight_buffer=weights,weight_stride=2)
        sort()
        gl.glFinish()
        times = []
        for _ in range(5):
            start = time.perf_counter()
            sort()
            gl.glFinish()
            times.append((time.perf_counter()-start)*1000)
        expected = depth_order_cpu(first,direction,second_means=second,weight=values)
        np.testing.assert_array_equal(self.read_order(order,len(first)),expected)
        print(json.dumps({"renderer":gl.glGetString(gl.GL_RENDERER).decode(),"count":len(first),
            "prepared_gpu_completed_ms":times,"prepared_gpu_median_ms":float(np.median(times))}),flush=True)


if __name__ == "__main__":
    unittest.main()
