"""Cross-platform scene and authoring acceptance tests, without GTK or GPU."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

from gsv import GSVError, Scene, Writer, add_camera, add_persistent_gaussians
from gs_studio.model import SceneDocument, PreparedFrame, density_rows
from gs_studio.camera import (CameraHistory, CameraTrack, export_camera_animation,
                              import_camera_animation)
from gs_studio import model


def record(x=0., valid=True):
    a = np.zeros((2, 16), np.float32)
    a[:, :3] = [[x, 0, 2], [1 + x, 0, 2]]
    a[:, 3] = .8
    a[:, 4:7] = [.1, .2, .3]
    a[:, 7] = [valid, True]
    a[:, 8] = 1
    a[:, 12:15] = [[1, .1, .2], [.2, .3, 1]]
    a[:, 15] = -1
    return a


def add_static(out, identifier='static', *, clock=None):
    out.manifest['spaces'].setdefault('world', {'unit': 'meter', 'axes': ['x', 'y', 'z'], 'handedness': 'right'})
    if clock:
        out.manifest['clocks'][clock] = {'unit': 'normalized'}
    values = {'ids': np.array([9], np.int64), 'means': np.array([[1., 2., 3.]]),
              'scales': np.ones((1, 3)), 'quaternions': np.array([[1., 0., 0., 0.]]),
              'opacity': np.array([.5]), 'colors': np.array([[2., -.5, .3]])}
    data = {name: out.add_array(identifier + '/' + name, value) for name, value in values.items()}
    data['color_space'] = 'srgb'
    return out.add_object(identifier, 'gaussian', 'gsv.gaussian.static', data, space='world',
                          **({'clock': clock} if clock else {}))


def write(path, *, gap=False, clocks=False):
    with Writer(path) as out:
        ids = np.array([-2**60 - 7, 2**60 + 3], np.int64)
        frames = [record(i, valid=not (gap and i == 2)) for i in range(5)]
        add_persistent_gaussians(out, 'field', ids, np.arange(5, dtype=np.float64), iter(frames))
        k = np.array([[200., 3., 160.], [0., 220., 120.], [0., 0., 1.]])
        pose = np.repeat(np.eye(4)[None, None], 2, axis=0)
        pose[1, 0, :3, :3] = np.diag([-1., -1., 1.])
        pose[1, 0, 0, 3] = 2
        add_camera(out, 'camera', [0., 4.], np.repeat(k[None, None], 2, axis=0), pose,
                   [[320, 240]], pixel_center='half')
        if clocks:
            add_persistent_gaussians(out, 'foreign', ids, [0.], iter([record()]), clock='foreign-time', space='elsewhere')


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'source.gsv'
        write(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_interpolation_covariance_and_int64_identity(self):
        with SceneDocument(self.path) as doc:
            frame = doc.evaluate(.5)
            np.testing.assert_allclose(frame.means[:, 0], [.5, 1.5])
            np.testing.assert_allclose(frame.covariance[0], np.diag([.01, .04, .09]), atol=1e-7)
            self.assertEqual(frame.ids.tolist(), [-2**60 - 7, 2**60 + 3])
            self.assertEqual(frame.object_ids.tolist(), ['field', 'field'])
            self.assertFalse(frame.means.flags.writeable)
            self.assertEqual(doc.times.tolist(), [0., 1., 2., 3., 4.])

    def test_source_samples_are_reused_across_interpolated_seeks(self):
        with SceneDocument(self.path) as doc:
            with mock.patch.object(doc.scene, 'array', wraps=doc.scene.array) as read:
                first = doc.evaluate(.25)
                second = doc.evaluate(.4)
                self.assertIs(doc.evaluate(.25), first)
                self.assertIsNot(second, first)
                frame_reads = [call.args[0] for call in read.call_args_list if '/frame/' in call.args[0]]
                self.assertEqual(frame_reads, ['field/frame/0', 'field/frame/1'])
            self.assertIs(doc.evaluate(-2), doc.evaluate(-1))
            self.assertLessEqual(doc._cache_bytes, doc.cache_budget)

    def test_prepared_frame_keeps_resident_endpoints_without_cpu_covariance(self):
        with SceneDocument(self.path) as doc:
            with mock.patch.object(model, 'quat_matrix', side_effect=AssertionError('unexpected CPU covariance')):
                prepared = doc.prepare_frame(.25)
                self.assertIsInstance(prepared, PreparedFrame)
                self.assertIsNone(prepared.weights)
                self.assertEqual(prepared.count, 2)
                with mock.patch.object(doc.scene, 'array', wraps=doc.scene.array) as read:
                    next_frame = doc.prepare_frame(.4)
                self.assertEqual(read.call_count, 0)
                self.assertIs(prepared.first, next_frame.first)
                self.assertIs(prepared.second, next_frame.second)
                self.assertEqual(prepared.endpoint_key, next_frame.endpoint_key)
                self.assertFalse(prepared.first.flags.writeable)
            cpu = doc.evaluate(.25)
            actual = prepared.materialize()
            np.testing.assert_allclose(actual.records, cpu.records)
            np.testing.assert_allclose(actual.covariance, cpu.covariance)
            selected = prepared.element('field', -2**60 - 7)
            np.testing.assert_allclose(selected.position, cpu.means[0])
            self.assertTrue(selected.valid)
            self.assertIsNone(prepared.element('field', 123456))
        # GPU snapshots and selected-row inspection do not own a live descriptor.
        np.testing.assert_allclose(prepared.materialize().records, cpu.records)

    def test_prepared_validity_preserves_inactive_identities_at_sample_boundaries(self):
        path = Path(self.temp.name) / 'prepared-gaps.gsv'
        write(path, gap=True)
        with SceneDocument(path) as doc:
            frame = doc.prepare_frame(3-1e-10)
            self.assertEqual(frame.count, 2)
            self.assertEqual(frame.valid_count, 1)
            self.assertTrue(0 < frame.weight < 1)
            self.assertFalse(frame.element('field', -2**60 - 7).valid)
            self.assertEqual(len(frame.materialize()), 1)
            self.assertEqual(doc.prepare_frame(3).valid_count, 2)
            self.assertEqual(doc.prepare_frame(2).valid_count, 1)

    def test_prepared_public_snapshot_storage_is_immutable_and_survives_source_replacement(self):
        with SceneDocument(self.path) as doc:
            frame = doc.prepare_frame(.25)
            original = frame.materialize().records.copy()
            for array in (frame.first, frame.second, frame.ids, frame.object_ids, *frame.bounds):
                with self.assertRaises(ValueError):
                    array.flat[0] = 'changed' if array.dtype.kind == 'O' else 0
            element = frame.element('field', -2**60 - 7)
            element.position[:] = 100
            self.assertNotEqual(frame.element('field', -2**60 - 7).position[0], 100)
        self.path.write_bytes(b'source replaced after close')
        np.testing.assert_allclose(frame.materialize().records, original)

    def test_prepared_mixed_fields_use_independent_native_intervals_and_global_source_order(self):
        path = Path(self.temp.name) / 'prepared-mixed.gsv'
        with Writer(path) as out:
            ids = np.array([-2**60 - 7, 2**60 + 3], np.int64)
            add_persistent_gaussians(out, 'one', ids, [0., 1.], [record(), record(1)])
            add_persistent_gaussians(out, 'two', ids, [0., 2.], [record(), record(2)])
            add_static(out, 'static-peer')
        with SceneDocument(path) as doc:
            order = ['two', 'static-peer', 'one']
            frame = doc.prepare_frame(.5, order)
            self.assertIsNone(frame.weight)
            np.testing.assert_allclose(frame.weights, [.25, .25, 0., .5, .5])
            self.assertEqual(frame.count, 5)
            self.assertIsNone(frame.element(None, 2**60 + 3))
            np.testing.assert_allclose(frame.materialize().records, doc.evaluate(.5, order).records)
            later = doc.prepare_frame(.75, order)
            self.assertEqual(frame.endpoint_key, later.endpoint_key)
            self.assertNotEqual(frame.endpoint_key, doc.prepare_frame(1.5, order).endpoint_key)
            self.assertLessEqual(doc._cache_bytes, doc.cache_budget)

    def test_cancelled_load_checks_each_checksum_chunk_and_closes_descriptor(self):
        path = Path(self.temp.name) / 'large-buffer.gsv'
        count = 150_000  # A single frame exceeds the SDK's 8 MiB read chunk.
        with Writer(path) as out:
            add_persistent_gaussians(out, 'large', np.arange(count, dtype=np.int64), [0.],
                                     [np.tile(record()[0], (count, 1))])
        opened, stopped = [], [False]
        real_scene, real_pread = model.Scene, model.os.pread
        def open_scene(*args, **kwargs):
            scene = real_scene(*args, **kwargs)
            opened.append(scene)
            return scene
        def pread(fd, length, offset):
            result = real_pread(fd, length, offset)
            if length == 8 * 1024**2:
                stopped[0] = True
            return result
        with mock.patch.object(model, 'Scene', side_effect=open_scene), mock.patch.object(model.os, 'pread', side_effect=pread):
            with self.assertRaises(InterruptedError):
                SceneDocument(path, cancelled=lambda: stopped[0])
        self.assertTrue(stopped[0])
        self.assertTrue(opened[0]._file.closed)

    def test_loading_cancellation_does_not_poison_accepted_document(self):
        stopped = [False]
        with SceneDocument(self.path, cancelled=lambda: stopped[0]) as doc:
            stopped[0] = True
            self.assertEqual(len(doc.evaluate(.5)), 2)
            with self.assertRaises(InterruptedError):
                doc.evaluate(.75, cancelled=lambda: True)
            self.assertEqual(len(doc.evaluate(.75)), 2)

    def test_cancelled_interpolation_does_not_publish_a_partial_frame(self):
        path = Path(self.temp.name) / 'cancel-interpolation.gsv'
        count = 70_000
        first = np.tile(record()[0], (count, 1))
        second = first.copy()
        second[:, 0] = 1
        second[:, 8:12] = [np.sqrt(1-.01**2), .01, 0, 0]
        with Writer(path) as out:
            add_persistent_gaussians(out, 'large', np.arange(count, dtype=np.int64), [0., 1.], [first, second])
        stopped = [False]
        real_slerp = model.slerp
        def completed_chunk(*args):
            result = real_slerp(*args)
            stopped[0] = True
            return result
        with SceneDocument(path) as doc:
            with mock.patch.object(model, 'slerp', side_effect=completed_chunk):
                with self.assertRaises(InterruptedError):
                    doc.evaluate(.5, cancelled=lambda: stopped[0])
            self.assertFalse(any(key[0] == 'frame' for key in doc._cache))
            frame = doc.evaluate(.5)
            self.assertEqual(len(frame), count)
            np.testing.assert_allclose(frame.means[:, 0], .5)
            self.assertLessEqual(doc._cache_bytes, doc.cache_budget)

    def test_static_covariance_and_complete_frame_are_cached_across_time(self):
        path = Path(self.temp.name) / 'static-cache.gsv'
        with Writer(path) as out:
            add_static(out)
        with SceneDocument(path) as doc, mock.patch.object(model, 'quat_matrix', wraps=model.quat_matrix) as rotations:
            frame = doc.evaluate(0)
            self.assertIs(doc.evaluate(1000), frame)
            self.assertEqual(rotations.call_count, 1)
            self.assertFalse(frame.records.flags.writeable)
            self.assertLessEqual(doc._cache_bytes, doc.cache_budget)

    def test_static_first_field_does_not_hide_dynamic_native_clock(self):
        path = Path(self.temp.name) / 'cohort.gsv'
        k = np.array([[100., 0, 160], [0, 100., 120], [0, 0, 1]])
        with Writer(path) as out:
            add_static(out, clock='unrelated')
            add_persistent_gaussians(out, 'moving', np.array([3, 9], np.int64), [0., 1.], [record(), record(1)])
            add_camera(out, 'active-camera', [.2, .8], np.tile(k, (2, 1, 1, 1)),
                       np.tile(np.eye(4), (2, 1, 1, 1)), [[320, 240]])
            add_camera(out, 'foreign-camera', [-100., 100.], np.tile(k, (2, 1, 1, 1)),
                       np.tile(np.eye(4), (2, 1, 1, 1)), [[320, 240]], space='elsewhere')
        with SceneDocument(path) as doc:
            self.assertEqual(doc.visible_ids, ['static', 'moving'])
            self.assertEqual(doc.clock, 'scene')
            np.testing.assert_array_equal(doc.times, [0., 1.])
            self.assertEqual(len(doc.evaluate(.5)), 3)
            doc.activate_domain('active-camera')
            np.testing.assert_array_equal(doc.times, [0., .2, .8, 1.])

    def test_static_scene_uses_compatible_camera_clock_without_invented_samples(self):
        path = Path(self.temp.name) / 'static-clock.gsv'
        k = np.array([[100., 0, 160], [0, 100., 120], [0, 0, 1]])
        with Writer(path) as out:
            add_static(out)
            add_camera(out, 'camera', [3., 7.], np.tile(k, (2, 1, 1, 1)),
                       np.tile(np.eye(4), (2, 1, 1, 1)), [[320, 240]], clock='capture')
        with SceneDocument(path) as doc:
            self.assertEqual(doc.clock, 'capture')
            np.testing.assert_array_equal(doc.times, [3., 7.])

    def test_frame_survives_close_and_closed_access_fails(self):
        doc = SceneDocument(self.path)
        frame = doc.evaluate(0)
        doc.close()
        doc.close()
        self.assertEqual(frame.ids[1], 2**60 + 3)
        with self.assertRaisesRegex(GSVError, 'closed'):
            doc.evaluate(1)

    def test_validity_gap_never_bridged(self):
        gap_path = Path(self.temp.name) / 'gaps.gsv'
        write(gap_path, gap=True)
        with SceneDocument(gap_path) as doc:
            self.assertEqual(len(doc.evaluate(1.5)), 1)
            self.assertEqual(len(doc.evaluate(2.5)), 1)
            paths = doc.trajectories('field', ids=[-2**60 - 7], density=0)
            self.assertEqual([p.times.tolist() for p in paths], [[0., 1.], [3., 4.]])
            self.assertTrue(all(p.identity == -2**60 - 7 for p in paths))

    def test_density_nested_and_picks_override_zero(self):
        ids = np.arange(-20, 80, dtype=np.int64)
        rows = density_rows(ids, 'field', .25)
        self.assertEqual(len(rows), 25)
        self.assertTrue(set(rows) <= set(density_rows(ids, 'field', .5)))
        with SceneDocument(self.path) as doc:
            self.assertEqual(len(doc.trajectories('field', density=0)), 0)
            self.assertEqual(len(doc.trajectories('field', ids=[2**60 + 3], density=0)), 1)
            with self.assertRaisesRegex(GSVError, 'memory budget'):
                doc.trajectories('field', max_bytes=1)
            with self.assertRaises(InterruptedError):
                doc.trajectories('field', cancelled=lambda: True)

    def test_inspection_retains_physical_capture_and_named_rig_views(self):
        path = Path(self.temp.name) / 'physical-camera.gsv'
        with Writer(path) as out:
            k = np.array([[200., 3., 160.], [0., 220., 120.], [0., 0., 1.]])
            add_camera(out, 'physical', [0., 2.], np.tile(k, (2, 2, 1, 1)),
                       np.tile(np.eye(4), (2, 2, 1, 1)), [[320, 240], [320, 240]],
                       view_ids=['left', 'right'],
                       optics={'f_number': [[2., 4.], [8., 16.]]},
                       acquisition={'exposure_seconds': [[.01, .02], [.03, .04]]})
        with SceneDocument(path) as doc:
            camera = doc.camera('physical', 1.8, view=1)
            self.assertEqual(camera.details['view_id'], 'right')
            self.assertEqual(camera.details['inspection_sample_time'], 2.)
            self.assertEqual(camera.details['optics']['f_number'], 16.)
            self.assertEqual(camera.details['acquisition']['exposure_seconds'], .04)
            np.testing.assert_allclose(camera.K, k)

    def test_camera_slerp_calibration_and_range(self):
        with SceneDocument(self.path) as doc:
            camera = doc.camera('camera', 2)
            np.testing.assert_allclose(camera.c2w[:3, 3], [1, 0, 0])
            np.testing.assert_allclose(camera.c2w[:3, :3].T @ camera.c2w[:3, :3], np.eye(3), atol=1e-12)
            self.assertAlmostEqual(np.linalg.det(camera.c2w[:3, :3]), 1)
            self.assertEqual((camera.width, camera.height, camera.pixel_center), (320, 240, 'half'))
            np.testing.assert_allclose(doc.camera('camera', 100).c2w[:3, 3], [2, 0, 0])
            with self.assertRaises(GSVError):
                doc.camera('camera', 0, view=9)
            with self.assertRaises(GSVError):
                doc.evaluate(float('nan'))
            paths = doc.trajectories('camera')
            self.assertEqual(len(paths), 1)
            self.assertIsNone(paths[0].identity)
            self.assertEqual(paths[0].times.tolist(), [0., 4.])
            np.testing.assert_allclose(paths[0].positions, [[0, 0, 0], [2, 0, 0]])
            with self.assertRaisesRegex(GSVError, 'view index'):
                doc.trajectories('camera', view=9)

    def test_domains_are_not_implicitly_aligned(self):
        path = Path(self.temp.name) / 'domains.gsv'
        write(path, clocks=True)
        with SceneDocument(path) as doc:
            self.assertEqual(doc.visible_ids, ['field'])
            with self.assertRaisesRegex(GSVError, 'matching coordinate'):
                doc.evaluate(0, ['field', 'foreign'])
            doc.set_visible(['foreign'])
            self.assertEqual(doc.clock, 'foreign-time')
            self.assertEqual(doc.space, 'elsewhere')
            self.assertEqual(doc.activate_domain('camera'), ['field'])
            self.assertEqual(doc.time_unit, 'second')

    def test_extreme_clocks_preserve_midpoint_and_native_times(self):
        path = Path(self.temp.name) / 'extreme.gsv'
        with Writer(path) as out:
            add_persistent_gaussians(out, 'field', np.array([3, 9], np.int64),
                                    [-1e308, 1e308], iter([record(0), record(2)]))
        with SceneDocument(path) as doc:
            np.testing.assert_allclose(doc.evaluate(0).means[:, 0], [1, 2])
            np.testing.assert_array_equal(doc.times, [-1e308, 1e308])
            path = doc.trajectories('field', density=0, ids=[3])[0]
            np.testing.assert_array_equal(path.times, [-1e308, 1e308])

    def test_linked_display_cache_is_not_double_rendered(self):
        path = Path(self.temp.name) / 'proxy.gsv'
        with Writer(path) as out:
            ids = np.array([3, 9], np.int64)
            add_persistent_gaussians(out, 'native', ids, [0.], iter([record()]))
            add_persistent_gaussians(out, 'cache', ids, [0.], iter([record()]), links={'source': 'native'},
                                    metadata={'role': 'evaluated-cache'})
        with SceneDocument(path) as doc:
            self.assertEqual(doc.visible_ids, ['cache'])
            self.assertEqual(len(doc.evaluate(0)), 2)
            self.assertEqual(doc.activate_domain('native'), ['cache'])
            self.assertEqual(doc.object('native')['type'], 'gaussian')

    def test_empty_document_can_author_camera_without_editing_manifest(self):
        path = Path(self.temp.name) / 'empty.gsv'
        with Writer(path):
            pass
        with SceneDocument(path) as doc:
            self.assertEqual((doc.space, doc.clock), ('world', 'timeline'))
            self.assertEqual(len(doc.evaluate(0)), 0)
            camera = CameraTrack('Camera', doc.space, doc.clock, 320, 240)
            camera.record(0, np.array([[100., 0, 160], [0, 100., 120], [0, 0, 1]]), np.eye(4))
            target = Path(self.temp.name) / 'empty-camera.gsv'
            export_camera_animation(target, [camera], doc)
            self.assertEqual(doc.manifest['spaces'], {})
            imported = import_camera_animation(target, doc)
            self.assertEqual(imported[0].space, 'world')
            with SceneDocument(target) as reopened:
                self.assertEqual(reopened.authored_cameras, [])
                self.assertEqual(reopened.camera(camera.id, 0).width, 320)

    def test_modified_source_is_detected_even_after_cache_hit(self):
        with SceneDocument(self.path) as doc:
            doc.evaluate(0)
            with self.path.open('ab') as f:
                f.write(b'x')
            with self.assertRaisesRegex(GSVError, 'changed'):
                doc.evaluate(0)

    def test_static_analytic_field_and_unknown_objects_inspectable(self):
        path = Path(self.temp.name) / 'static.gsv'
        with Writer(path) as out:
            out.manifest['spaces']['world'] = {'unit': 'meter', 'axes': ['x', 'y', 'z'], 'handedness': 'right'}
            data = {'color_space': 'srgb'}
            values = {'ids': np.array([9], np.int64), 'means': np.array([[1., 2., 3.]]),
                      'scales': np.ones((1, 3)), 'quaternions': np.array([[1., 0., 0., 0.]]),
                      'opacity': np.array([.5]), 'colors': np.array([[2., -.5, .3]])}
            for name, value in values.items():
                data[name] = out.add_array(name, value)
            out.add_object('static', 'gaussian', 'gsv.gaussian.static', data, space='world')
            out.add_object('vendor', 'archive', 'vendor.example', {'safe': 'opaque'})
        with SceneDocument(path) as doc:
            f = doc.evaluate(4)
            np.testing.assert_allclose(f.colors[0], [2., -.5, .3])
            self.assertEqual(doc.object('vendor')['data'], {'safe': 'opaque'})
            self.assertEqual(doc.trajectories('static'), [])

    def test_scaffold_glyphs_and_edges_obey_source_validity(self):
        path = Path(self.temp.name) / 'scaffold.gsv'
        with Writer(path) as out:
            out.manifest['spaces']['world'] = {'unit': 'meter', 'axes': ['x', 'y', 'z'], 'handedness': 'right'}
            out.manifest['clocks']['scene'] = {'unit': 'second'}
            data = {}
            for name, value in {
                'ids': np.array([3, 9], np.int64), 'times': np.array([0., 1., 2.]),
                'positions': np.array([[[0, 0, 2], [1, 0, 2]], [[0, 1, 2], [1, 1, 2]],
                                       [[0, 2, 2], [1, 2, 2]]], np.float32),
                'edges': np.array([[0, 1]], np.int32),
                'certainty': np.array([[False, True], [True, True], [True, True]]),
                'visibility': np.array([[True, True], [False, True], [True, True]]),
                'validity': np.ones((3, 2), bool),
            }.items():
                data[name] = out.add_array(name, value)
            out.add_object('graph', 'scaffold', 'gsv.scaffold.graph', data, space='world', clock='scene')
        with SceneDocument(path) as doc:
            self.assertEqual(len(doc.evaluate(0)), 2)  # Certainty is not validity.
            np.testing.assert_allclose(doc.evaluate(0).edges, [[[0, 0, 2], [1, 0, 2]]])
            self.assertEqual(len(doc.evaluate(.5).edges), 0)
            self.assertEqual(len(doc.evaluate(1).edges), 0)
            self.assertEqual(len(doc.evaluate(2).edges), 1)


class AuthoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'source.gsv'
        write(self.path)
        self.doc = SceneDocument(self.path)
        self.track = CameraTrack('User view', self.doc.space, self.doc.clock, 640, 480)
        k = np.array([[300., 5., 160.], [0., 300., 120.], [0., 0., 1.]])
        self.track.record(0, k, np.eye(4), width=320, height=240)
        second = np.eye(4)
        second[0, 3] = 2
        self.track.record(2, k, second, width=320, height=240, near=.1, far=100)

    def tearDown(self):
        self.doc.close()
        self.temp.cleanup()

    def test_camera_roundtrip_sdk_provenance_and_source_unchanged(self):
        original = self.path.read_bytes()
        target = Path(self.temp.name) / 'cameras.gsv'
        export_camera_animation(target, [self.track], self.doc)
        with Scene(target) as exported:
            exported.validate()
            self.assertEqual(exported.manifest['metadata']['source_document']['manifest_sha256'], self.doc.digest)
            self.assertEqual(exported.objects[0]['metadata']['role'], 'user')
        imported = import_camera_animation(target, self.doc)
        self.assertEqual(len(imported), 1)
        np.testing.assert_allclose(imported[0].at(1).c2w[:3, 3], [1., 0, 0])
        self.assertEqual(imported[0].at(0).K[0, 2], 320)
        self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaisesRegex(GSVError, 'already exists'):
            import_camera_animation(target, self.doc, existing_ids=[self.track.id])
        with self.assertRaisesRegex(GSVError, 'separate file'):
            export_camera_animation(self.path, [self.track], self.doc, overwrite=True)

    def test_import_checks_digest(self):
        target = Path(self.temp.name) / 'cameras.gsv'
        export_camera_animation(target, [self.track], self.doc)
        unrelated = Path(self.temp.name) / 'unrelated.gsv'
        write(unrelated, gap=True)
        with SceneDocument(unrelated) as other:
            with self.assertRaisesRegex(GSVError, 'manifest digest'):
                import_camera_animation(target, other)

    def test_exact_camera_keys_preserve_finite_far_despite_unbounded_neighbor(self):
        self.assertEqual(self.track.at(2).far, 100)
        self.assertIsNone(self.track.at(1).far)
        self.assertIsNone(self.track.at(0).far)
        target = Path(self.temp.name) / 'volume.gsv'
        export_camera_animation(target, [self.track], self.doc)
        with SceneDocument(target) as source:
            self.assertEqual(source.camera(self.track.id, 2).far, 100)
            self.assertIsNone(source.camera(self.track.id, 1).far)

    def test_invalid_camera_edits_fail_before_mutating_the_track(self):
        old = list(self.track.keyframes)
        pose = np.eye(4)
        pose[3, 3] = 1.000001
        with self.assertRaisesRegex(GSVError, 'affine pose'):
            self.track.record(4, self.track.at(0).K, pose)
        self.assertEqual([f.time for f in self.track.keyframes], [f.time for f in old])
        k = np.eye(3)
        k[0, 0] = 1e308
        with self.assertRaisesRegex(GSVError, 'numeric range'):
            self.track.record(4, k, np.eye(4), width=1, height=1)
        self.assertEqual([f.time for f in self.track.keyframes], [f.time for f in old])

    def test_camera_only_reexport_retains_original_reconstruction_digest(self):
        original = Path(self.temp.name) / 'original-cameras.gsv'
        export_camera_animation(original, [self.track], self.doc)
        with SceneDocument(original) as camera_only:
            new = CameraTrack('Another view', self.track.space, self.track.clock, 640, 480)
            new.record(0, self.track.at(0).K, np.eye(4))
            target = Path(self.temp.name) / 'new-cameras.gsv'
            export_camera_animation(target, [new], camera_only)
        imported = import_camera_animation(target, self.doc)
        self.assertEqual(imported[0].id, new.id)

    def test_imported_supplemental_domain_descriptors_survive_reexport(self):
        empty = Path(self.temp.name) / 'empty-descriptors.gsv'
        with Writer(empty):
            pass
        with SceneDocument(empty) as source:
            incoming = Path(self.temp.name) / 'external-camera.gsv'
            with Writer(incoming, metadata={'source_document': {'manifest_sha256': source.digest}}) as out:
                out.manifest['spaces']['world'] = {'unit': 'meter', 'axes': ['east', 'north', 'up'], 'handedness': 'right'}
                out.manifest['clocks']['timeline'] = {'unit': 'frame', 'mapping': {'target': 'capture', 'scale': .04, 'offset': 1.}}
                out.manifest['clocks']['capture'] = {'unit': 'second'}
                add_camera(out, 'external', [10.], np.eye(3)[None, None], np.eye(4)[None, None], [[32, 24]],
                           clock='timeline', pixel_center='half', metadata={'role': 'user'})
            tracks = import_camera_animation(incoming, source)
            outgoing = Path(self.temp.name) / 'preserved-camera.gsv'
            export_camera_animation(outgoing, tracks, source)
            with Scene(incoming) as before, Scene(outgoing) as after:
                self.assertEqual(before.manifest['spaces'], after.manifest['spaces'])
                self.assertEqual(before.manifest['clocks'], after.manifest['clocks'])
                after.validate()

    def test_malformed_source_provenance_is_a_validation_error(self):
        incoming = Path(self.temp.name) / 'bad-provenance.gsv'
        with Writer(incoming, metadata={'source_document': 'opaque vendor value'}) as out:
            add_camera(out, 'camera-user', [0.], np.eye(3)[None, None], np.eye(4)[None, None], [[32, 24]],
                       metadata={'role': 'user'})
        with self.assertRaisesRegex(GSVError, 'matching source manifest'):
            import_camera_animation(incoming, self.doc)

    def test_source_aliases_cannot_be_overwritten(self):
        symlink = Path(self.temp.name) / 'symlink.gsv'
        symlink.symlink_to(self.path)
        with self.assertRaisesRegex(GSVError, 'separate file'):
            export_camera_animation(symlink, [self.track], self.doc, overwrite=True)
        hardlink = Path(self.temp.name) / 'hardlink.gsv'
        hardlink.hardlink_to(self.path)
        with self.assertRaisesRegex(GSVError, 'source reconstruction'):
            export_camera_animation(hardlink, [self.track], self.doc, overwrite=True)

    def test_mac_writer_camera_archive_contract_and_integer_centers(self):
        # The macOS GSVCameraWriter schema is reproduced with the SDK writer,
        # including inherited provenance, explicit volume and role=user. This
        # also exercises imports from integer-centered source cameras, whose
        # editable viewport representation must shift K by exactly half a pixel.
        target = Path(self.temp.name) / 'mac-camera.gsv'
        k = np.array([[100., 2., 160.], [0., 100., 120.], [0., 0., 1.]])
        with Writer(target, metadata={'created_by': '4DGS Studio', 'source_document': {
                'name': self.path.name, 'manifest_sha256': self.doc.digest}}) as out:
            out.manifest['spaces'] = self.doc.manifest['spaces'].copy()
            out.manifest['clocks'] = self.doc.manifest['clocks'].copy()
            add_camera(out, 'camera-macos', [0., 1.], np.repeat(k[None, None], 2, axis=0),
                       np.repeat(np.eye(4)[None, None], 2, axis=0), [[320, 240]],
                       pixel_center='integer', space=self.doc.space, clock=self.doc.clock,
                       view_ids=['0'], metadata={'role': 'user', 'created_by': '4DGS Studio',
                                                'interpolation': 'translation/K linear; rotation shortest-arc SLERP'},
                       view_volume={'near': [[.01], [.1]], 'far': [[100.], [0.]],
                                    'far_unbounded': np.array([[False], [True]])})
        track = import_camera_animation(target, self.doc)[0]
        self.assertEqual(track.id, 'camera-macos')
        np.testing.assert_allclose(track.at(0).K[:2, 2], [160.5, 120.5])
        self.assertAlmostEqual(track.keyframes[0].near, .01)
        self.assertEqual(track.keyframes[0].far, 100.)
        self.assertIsNone(track.keyframes[1].far)

    def test_retime_collision_and_history(self):
        history = CameraHistory([self.track])
        history.remember()
        history.tracks[0].retime(1, 3)
        self.assertEqual(history.tracks[0].keyframes[1].time, 3)
        self.assertTrue(history.undo())
        self.assertEqual(history.tracks[0].keyframes[1].time, 2)
        self.assertTrue(history.redo())
        self.assertEqual(history.tracks[0].keyframes[1].time, 3)
        with self.assertRaisesRegex(GSVError, 'already exists'):
            history.tracks[0].retime(0, 3)
        history.tracks[0].remove(0)
        with self.assertRaisesRegex(GSVError, 'at least one'):
            history.tracks[0].remove(0)

    def test_same_named_cameras_use_stable_identity_and_immutable_history(self):
        second = self.track.clone()
        second.id = 'another-camera'
        history = CameraHistory([self.track, second])
        self.assertEqual(history.tracks.index(second), 1)
        self.assertNotEqual(self.track, second)
        history.remember()
        key = self.track.keyframes[0]
        k = key.K.copy()
        k[0, 0] *= 2
        second.record(key.time, k, key.c2w)
        self.assertEqual(second.keyframes[0].K[0, 0], 2 * key.K[0, 0])
        self.assertTrue(history.undo())
        self.assertEqual(history.tracks[1].keyframes[0].K[0, 0], key.K[0, 0])
        with self.assertRaises(ValueError):
            history.tracks[1].keyframes[0].K[0, 0] = 1
        with self.assertRaises(GSVError):
            CameraHistory(limit=0)


if __name__ == '__main__':
    unittest.main()
