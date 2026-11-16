"""Cross-platform scene and authoring acceptance tests, without GTK or GPU."""
from pathlib import Path
import tempfile
import unittest

import numpy as np

from gsv import GSVError, Scene, Writer, add_camera, add_persistent_gaussians
from gs_studio.model import SceneDocument, density_rows
from gs_studio.camera import (CameraHistory, CameraTrack, export_camera_animation,
                              import_camera_animation)


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


if __name__ == '__main__':
    unittest.main()
