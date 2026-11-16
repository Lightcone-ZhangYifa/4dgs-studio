"""Actual GTK window workflows; run under a display or Xvfb with Mesa GL.

Native file chooser completion is substituted with a chosen local path so the
tests exercise the real asynchronous save/import callbacks without automating a
desktop portal. Every other widget, event loop, GL context and worker is real.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np
from gsv import (Writer, Scene, add_camera, add_camera_rgb,
                 add_persistent_gaussians, add_render_profile, add_render_view)


def write_scene(path, *, identity=2**60 + 3, clock_unit="second"):
    with Writer(path, overwrite=True) as out:
        out.manifest["clocks"]["scene"] = {"unit": clock_unit}
        frames = []
        for value in [0, .5, 1]:
            f = np.zeros((1, 16), np.float32)
            f[0, :3] = [value * .1, 0, 2]
            f[0, 3:7] = [.8, .08, .05, .04]
            f[0, 7:9] = 1
            f[0, 12:16] = [.9, .4, .1, -1]
            frames.append(f)
        add_persistent_gaussians(out, "field", np.array([identity], np.int64), [0, .5, 1], frames)
        K = np.array([[28., 0, 10.], [0, 28, 6.], [0, 0, 1.]])
        add_camera(out, "camera", [0, 1], np.tile(K, (2, 1, 1, 1)),
                   np.tile(np.eye(4), (2, 1, 1, 1)), [[20, 12]], pixel_center="half")
        add_camera_rgb(out, "observations", "camera", [0, 1], [0, 0],
                       [np.zeros((12, 20, 3), np.uint8)] * 2, color_space="srgb", role="reference")
        add_render_profile(out, "profile",
            visibility={"center_depth": {"near": .01, "far": None, "near_inclusive": False, "far_inclusive": True}, "camera_volume": "ignore"},
            projection={"divide_epsilon": 1e-7, "jacobian": {"mode": "symmetric-fov", "factor": 1.3}, "pixel_offset": [0, 0]},
            filter={"covariance_dilation": .3, "radius_clip": 0, "antialias_compensation": False},
            support={"bounds": "tile-major-radius", "sigma": 3, "tile_size": [16, 16], "mahalanobis_cutoff": None},
            alpha={"minimum": 1/255, "maximum": .99},
            compositing={"direction": "front-to-back", "depth_sort": "tile-stable", "tie_break": "source-order", "transmittance": {"threshold": 1e-4, "inclusive": False, "stage": "before-contribution"}})
        add_render_view(out, "render-view", "camera", ["field"], "profile", observation_ids=["observations"])


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"), "GTK integration requires a display; use xvfb-run")
class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_foreign("cairo")
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gio, GLib
        from gs_studio import application
        cls.Gio, cls.GLib, cls.application = Gio, GLib, application
        cls.app = application.StudioApplication()
        cls.app.set_flags(Gio.ApplicationFlags.NON_UNIQUE | Gio.ApplicationFlags.HANDLES_OPEN)
        cls.app.register(None)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source.gsv"
        self.other = Path(self.temp.name) / "other.gsv"
        write_scene(self.source)
        write_scene(self.other, identity=17, clock_unit="frame")
        self.window = self.application.StudioWindow(self.app)
        self.errors = []
        self.window.error = lambda error: self.errors.append(str(error))
        self.window.present()
        self.addCleanup(self.close_window)

    def pump_until(self, condition, timeout=10):
        context = self.GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for _ in range(30):
                if not context.pending():
                    break
                context.iteration(False)
            if condition():
                return
            threading.Event().wait(.005)
        self.fail(f"GTK condition timed out; status={self.window.status.get_text()!r}; errors={self.errors!r}")

    def close_window(self):
        if not self.window.closed:
            self.window._finish_close()
        self.pump_until(lambda: not any(j.running for j in (self.window.loader, self.window.frames, self.window.comparisons, self.window.file_jobs)))

    def open_scene(self, path=None):
        self.window.open_path(str(path or self.source))
        expected = Path(path or self.source).resolve()
        self.pump_until(lambda: self.window.document is not None and
                        self.window.document.path == expected and self.window.frame is not None and
                        not self.window.frames.running)
        self.pump_until(lambda: self.window.viewport._renderer is not None or self.window.viewport.render_error is not None)
        self.assertIsNone(self.window.viewport.render_error)
        self.assertEqual(self.errors, [])

    def chooser(self, path):
        Gio, GLib = self.Gio, self.GLib
        class ChosenFile:
            def __init__(self, **kwargs):
                pass
            def set_filters(self, filters):
                pass
            def set_initial_name(self, name):
                pass
            def save(self, parent, cancellable, completed):
                GLib.idle_add(lambda: completed(self, None) or False)
            open = save
            def save_finish(self, result):
                return Gio.File.new_for_path(str(path))
            open_finish = save_finish
        return mock.patch.object(self.application.Gtk, "FileDialog", ChosenFile)

    def test_scene_selection_playback_camera_animation_and_comparison(self):
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.open_scene()
        w = self.window
        self.assertEqual(w.stack.get_visible_child_name(), "scene")
        self.assertEqual(w.frame.ids.tolist(), [2**60 + 3])
        w.outliner.selection_model.select_item(w.outliner.position_for_member(('field',None)), True)
        self.assertEqual(w.selected, {("field", None)})
        self.pump_until(lambda: len(w.viewport.paths) > 0 and not w.frames.running)
        w.id_entry.set_text(str(2**60 + 3))
        w.id_entry.emit("activate")
        self.pump_until(lambda: w.selected == {("field", 2**60 + 3)})
        self.assertEqual(w.selected, {("field", 2**60 + 3)})
        w._frame_selection()
        w.toggle_playback()
        self.pump_until(lambda: w.time > .025)
        w._set_playing(False)
        self.pump_until(lambda: not w.frames.running)
        self.assertAlmostEqual(w.viewport.time, w.time)
        w._scrub(0)
        self.pump_until(lambda: w.time == 0 and not w.frames.running)
        w.lookup_action("create-camera").activate(None)
        self.assertEqual(len(w.history.tracks), 1)
        self.assertTrue(w.dirty)
        self.assertEqual([k.time for k in w._track().keyframes], [0])
        w._scrub(.5)
        self.pump_until(lambda: w.time == .5 and not w.frames.running)
        w.viewport.navigation.dolly(.1)
        w.lookup_action("record").activate(None)
        self.assertEqual([k.time for k in w._track().keyframes], [0, .5])
        w.timeline.on_key_move(1, .75)
        self.assertEqual([k.time for k in w._track().keyframes], [0, .75])
        w.lookup_action("undo").activate(None)
        self.assertEqual([k.time for k in w._track().keyframes], [0, .5])
        w.lookup_action("redo").activate(None)
        self.assertEqual([k.time for k in w._track().keyframes], [0, .75])
        animation = Path(self.temp.name) / "animation.gsv"
        with self.chooser(animation):
            w.choose_file("save")
            self.pump_until(lambda: animation.exists() and not w.progress.get_visible())
        self.assertFalse(w.dirty)
        with Scene(animation) as saved:
            saved.validate()
            self.assertEqual(saved.manifest["metadata"]["source_document"]["manifest_sha256"], w.document.manifest_sha256)
        w.delete_camera()
        self.assertEqual(w.history.tracks, [])
        with self.chooser(animation):
            w.choose_file("import")
            self.pump_until(lambda: len(w.history.tracks) == 1 and not w.progress.get_visible())
        self.assertEqual([k.time for k in w._track().keyframes], [0, .75])
        w._scrub(0)
        self.pump_until(lambda: w.time == 0 and not w.frames.running)
        w.compare_toggle.set_active(True)
        self.pump_until(lambda: len(w.image_view.images) == 2 and not w.comparisons.running)
        self.assertEqual(w.stack.get_visible_child_name(), "comparison")
        self.assertIn("PSNR", w.status.get_text())
        w.compare_toggle.set_active(False)
        self.assertEqual(w.image_view.images, [])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), digest)
        self.assertEqual(self.errors, [])

    def test_cancelled_load_disposes_then_replacement_uses_new_scene(self):
        self.open_scene()
        w, initial = self.window, self.window.document
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        created = []
        constructor = self.application.SceneDocument
        def delayed(path, **kwargs):
            doc = constructor(path, **kwargs)
            created.append(doc)
            entered.set()
            release.wait(5)
            return doc
        with mock.patch.object(self.application, "SceneDocument", delayed):
            w.open_path(str(self.other))
            self.pump_until(entered.is_set)
            w.cancel_loading()
            self.assertIs(w.document, initial)
            release.set()
            self.pump_until(lambda: created[0]._closed and not w.loader.running)
        self.assertIs(w.document, initial)
        self.assertFalse(w.progress.get_visible())
        self.open_scene(self.other)
        self.pump_until(lambda: initial._closed)
        self.assertEqual(w.frame.ids.tolist(), [17])
        self.assertEqual(w.timeline.unit, "frame")
        self.assertEqual(self.errors, [])

    def test_failed_open_keeps_document_and_close_cancels_capture(self):
        self.open_scene()
        w, initial = self.window, self.window.document
        bad = Path(self.temp.name) / "damaged.gsv"
        bad.write_bytes(b"not a GSV container")
        w.open_path(str(bad))
        self.pump_until(lambda: bool(self.errors) and not w.loader.running)
        self.assertIs(w.document, initial)
        self.errors.clear()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.application.compare_view
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        with mock.patch.object(self.application, "compare_view", delayed):
            w.compare_toggle.set_active(True)
            self.pump_until(entered.is_set)
            w._finish_close()
            release.set()
            self.pump_until(lambda: not w.comparisons.running and initial._closed)
        self.assertTrue(w.closed)
        self.assertEqual(w.image_view.images, [])
        self.assertEqual(self.errors, [])

    def test_save_snapshot_does_not_clear_new_edits_or_lock_replacement(self):
        self.open_scene()
        w = self.window
        w.create_camera()
        original = self.application.export_camera_animation
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        animation = Path(self.temp.name) / "snapshot.gsv"
        with mock.patch.object(self.application, "export_camera_animation", delayed), self.chooser(animation):
            w.choose_file("save")
            self.pump_until(entered.is_set)
            w._scrub(.5)
            self.pump_until(lambda: w.time == .5 and not w.frames.running)
            w.record_key()
            release.set()
            self.pump_until(lambda: animation.exists() and not w.file_jobs.running)
        self.assertTrue(w.dirty, "Saving the earlier snapshot cannot mark a later edit as saved")
        with Scene(animation) as scene:
            obj = scene.objects[0]
            self.assertEqual(scene.array(obj["data"]["times"]).tolist(), [0])
        self.assertEqual([k.time for k in w._track().keyframes], [0, .5])
        entered.clear()
        release.clear()
        with mock.patch.object(self.application, "export_camera_animation", delayed), self.chooser(animation):
            w.choose_file("save")
            self.pump_until(entered.is_set)
            # User-approved replacement reaches _open after the discard prompt.
            w._open(str(self.other))
            self.pump_until(lambda: w.document.path == self.other.resolve() and not w.loader.running)
            release.set()
            self.pump_until(lambda: not w.file_jobs.running)
        self.assertFalse(w._file_busy, "A superseded save must not disable later file choosers")
        self.assertEqual(self.errors, [])

    def test_old_chooser_completion_cannot_unlock_new_scene_chooser(self):
        self.open_scene()
        w, Gio = self.window, self.Gio
        pending = []
        class DeferredFile:
            def __init__(self, **kwargs):
                pass
            def set_filters(self, filters):
                pass
            def open(self, parent, cancellable, completed):
                pending.append((self, completed))
            def open_finish(self, result):
                return Gio.File.new_for_path(str(self.other))
        # The obsolete chooser's result should be fenced before reading it.
        DeferredFile.other = self.source
        with mock.patch.object(self.application.Gtk, "FileDialog", DeferredFile):
            w.choose_file("import")
            self.assertTrue(w._file_busy)
            w._open(str(self.other))
            self.pump_until(lambda: w.document.path == self.other.resolve() and not w.loader.running)
            w.choose_file("open")
            self.assertTrue(w._file_busy)
            self.assertEqual(len(pending), 2)
            dialog, completed = pending[0]
            completed(dialog, None)
            self.assertTrue(w._file_busy, "An obsolete chooser cannot change the current dialog's busy state")
        self.assertEqual(self.errors, [])

    def test_ui_png_exports_preview_resolution_workspace_and_full_comparison(self):
        import cairo
        self.open_scene()
        w = self.window
        source_digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        w.create_camera()
        self.assertTrue(w.dirty)
        w.viewport.show_grid = False
        w._set_render_scale(.5)
        expected = tuple(max(1, round(value*w.viewport.get_scale_factor()*.5))
                         for value in (w.viewport.get_width(), w.viewport.get_height()))
        self.pump_until(lambda: w.viewport._renderer.size == expected)

        def exported(name, workspace=False):
            path = Path(self.temp.name) / name
            with self.chooser(path):
                w.export_image(workspace)
                self.pump_until(lambda: path.exists() and not w.file_jobs.running and not w._file_busy)
            surface = cairo.ImageSurface.create_from_png(str(path))
            width, height = surface.get_width(), surface.get_height()
            pixels = np.frombuffer(surface.get_data(), np.uint8).reshape(height, surface.get_stride())
            pixels = pixels[:, :width*4].reshape(height, width, 4)
            return pixels[:, :, 2::-1].copy()

        preview = exported("preview.png")
        self.assertEqual(preview.shape[:2], expected[::-1])
        self.assertGreater(np.max(preview), 100)
        self.assertGreater(np.std(preview), 3)
        workspace = exported("workspace.png", workspace=True)
        self.assertGreater(workspace.shape[0], preview.shape[0])
        self.assertGreater(workspace.shape[1], preview.shape[1])
        self.assertGreater(np.mean(workspace), 10)
        self.assertGreater(np.std(workspace), 10)
        self.assertTrue(w.dirty, "Saving screenshots must preserve unsaved camera edits")

        w.compare_toggle.set_active(True)
        self.pump_until(lambda: len(w.image_view.images) == 2 and not w.comparisons.running)
        # Inspection zoom/pan must never crop or resize an exported native pair.
        w.image_view.zoom, w.image_view.pan = 2.5, (32, -17)
        paired = exported("comparison.png")
        self.assertEqual(paired.shape, (12, 40, 3))
        for i, (_, raw) in enumerate(w.image_view.images):
            np.testing.assert_array_equal(paired[:, i*20:(i+1)*20], raw[:, :, 2::-1])
        self.assertTrue(w.dirty)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_digest)
        self.assertEqual(self.errors, [])

    def test_domain_activation_updates_native_clock_visibility_and_frames(self):
        # Reuse macOS's two independent clock/space fixture for UI activation.
        import importlib.util
        fixture_file = Path(__file__).resolve().parents[2] / "native" / "tests" / "camera_render_fixtures.py"
        spec = importlib.util.spec_from_file_location("studio_domain_fixtures", fixture_file)
        fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixtures)
        path = Path(self.temp.name) / "domains.gsv"
        fixtures.write_clock_domains(path)
        self.open_scene(path)
        w = self.window
        self.assertEqual(w.document.clock, "a")
        w._activate_domain("field-b")
        self.pump_until(lambda: w.document.clock == "b" and
                        w.timeline.times.tolist() == [10, 20] and not w.frames.running and
                        w.frame.object_ids.tolist() == ["field-b"])
        self.assertEqual(w.document.space, "b")
        self.assertIn("field-b", w.visible)
        self.assertNotIn("field-a", w.visible)
        self.assertEqual(w.time, 10)
        self.assertEqual(float(w.range_start.get_text()), 10)
        self.assertEqual(float(w.range_end.get_text()), 20)
        w._activate_domain("camera-a")
        self.pump_until(lambda: w.document.clock == "a" and
                        w.timeline.times.tolist() == [0, 1] and not w.frames.running and
                        w.frame.object_ids.tolist() == ["field-a"])
        self.assertEqual(w.document.space, "a")
        self.assertIn("field-a", w.visible)
        self.assertNotIn("field-b", w.visible)
        self.assertEqual(self.errors, [])

    def test_source_and_geometry_selections_cannot_edit_previous_user_camera(self):
        self.open_scene()
        w = self.window
        w.create_camera()
        identifier = w.editing_id
        source_index = next(i+1 for i, entry in enumerate(w.camera_entries) if entry[0] == "camera")
        w.camera_drop.set_selected(source_index)
        self.assertEqual(w.editing_id,identifier)
        self.assertEqual(w.selected,{(identifier,None)})
        w.record_key()
        np.testing.assert_array_equal(w._track().keyframes[0].c2w,w._calibrations['camera',0].c2w)
        w.outliner.selection_model.select_item(w.outliner.position_for_member(('camera',None)), True)
        self.assertIsNone(w.editing_id)
        w.delete_camera()
        self.assertEqual([track.id for track in w.history.tracks], [identifier])
        user_index = next(i+1 for i, entry in enumerate(w.camera_entries) if entry[0] == identifier)
        w.outliner.selection_model.select_item(w.outliner.position_for_member((identifier,None)), True)
        w.camera_drop.set_selected(user_index)
        self.assertEqual(w.editing_id, identifier)
        w._pick("field", 2**60+3)
        self.assertIsNone(w.editing_id)
        w.delete_camera()
        self.assertEqual([track.id for track in w.history.tracks], [identifier])
        w.primary = identifier
        w.selected = {(identifier, None)}
        w._selection_changed()
        w.outliner.selection_model.select_item(w.outliner.position_for_member(('field',None)), True)
        self.assertIsNone(w.editing_id)
        self.assertEqual(self.errors, [])

    def test_authored_paths_refresh_with_keys_and_editor_preserves_focused_text(self):
        import math
        self.open_scene()
        w = self.window
        w.create_camera()
        identifier = w.editing_id
        w.position.set_text("0, 0, 0")
        w.rotation.set_text("1, 0, 0, 0")
        w._edit_track("pose")
        w.fov.set_text("40")
        w._edit_track("fov")
        w._scrub(1)
        self.pump_until(lambda: w.time == 1 and not w.frames.running)
        w.position.set_text("2, 4, 6")
        w._edit_track("pose")
        w.fov.set_text("70")
        w._edit_track("fov")
        self.pump_until(lambda: not w.frames.running and any(p.object_id == identifier for p in w.viewport.paths))
        path = next(p for p in w.viewport.paths if p.object_id == identifier)
        np.testing.assert_allclose(path.positions, [[0, 0, 0], [2, 4, 6]])
        w.viewport.grab_focus()
        w._scrub(.5)
        self.pump_until(lambda: w.time == .5 and not w.frames.running)
        np.testing.assert_allclose([float(x) for x in w.position.get_text().split(",")], [1, 2, 3])
        camera = w._track().at(.5)
        expected = math.degrees(2*math.atan(camera.width/(2*camera.K[0,0])))
        self.assertAlmostEqual(float(w.fov.get_text()), expected, places=4)
        w.position.grab_focus()
        self.pump_until(lambda: w._entry_editing(w.position))
        w.position.set_text("unfinished coordinate edit")
        w._scrub(.75)
        self.pump_until(lambda: w.time == .75 and not w.frames.running)
        self.assertEqual(w.position.get_text(), "unfinished coordinate edit")
        w._scrub(1)
        self.pump_until(lambda: w.time == 1 and not w.frames.running)
        w.position.set_text("6, 4, 2")
        w._edit_track("pose")
        self.pump_until(lambda: not w.frames.running and any(p.object_id == identifier and np.allclose(p.positions[-1], [6,4,2]) for p in w.viewport.paths))
        w._retime(1, 1.5)
        self.pump_until(lambda: w.time == 1.5 and not w.frames.running)
        self.assertEqual([key.time for key in w._track().keyframes], [0, 1.5])
        self.assertEqual(self.errors, [])

    def test_static_source_transport_includes_authored_camera_duration(self):
        path = Path(self.temp.name) / "static.gsv"
        with Writer(path) as out:
            out.manifest["spaces"]["world"] = {"unit": "meter", "handedness": "right", "axes": ["right", "down", "forward"]}
            values = {"ids": np.array([7],np.int64), "means": np.array([[0,0,2]],np.float32),
                      "scales": np.full((1,3),.08,np.float32), "quaternions": np.array([[1,0,0,0]],np.float32),
                      "colors": np.array([[1,.4,.2]],np.float32), "opacity": np.array([.8],np.float32)}
            data = {key: out.add_array("field/"+key,value) for key,value in values.items()}
            data["color_space"] = "srgb"
            out.add_object("field","gaussian","gsv.gaussian.static",data,space="world")
        self.open_scene(path)
        w = self.window
        self.assertEqual(w.document.times.tolist(), [0])
        w.create_camera()
        w._scrub(2)
        self.pump_until(lambda: w.time == 2 and not w.frames.running)
        w.record_key()
        self.assertEqual(w.timeline.times.tolist(), [0, 2])
        self.assertEqual(w._play_bounds()[:2], (0, 2))
        w._step(-1)
        self.pump_until(lambda: w.time == 0 and not w.frames.running)
        w._step(1)
        self.pump_until(lambda: w.time == 2 and not w.frames.running)
        w._scrub(0)
        self.pump_until(lambda: w.time == 0 and not w.frames.running)
        w.toggle_playback()
        self.assertTrue(w.playing)
        self.pump_until(lambda: w.time > .02)
        w._set_playing(False)
        self.assertEqual(self.errors, [])

    def test_native_text_undo_preserves_camera_and_creation_undo_clears_selection(self):
        self.open_scene()
        w = self.window
        w.create_camera()
        identifier = w.editing_id
        entry = w.track_name
        entry.grab_focus()
        self.pump_until(lambda: w._entry_editing(entry))
        before = entry.get_text()
        entry.set_position(-1)
        # The keybinding signal creates a native user-action undo group;
        # Gtk.Editable.insert_text() is a programmatic, irreversible mutation.
        w.get_focus().emit("insert-at-cursor", " edited")
        self.assertNotEqual(entry.get_text(), before)
        w.lookup_action("undo").activate(None)
        self.assertEqual(entry.get_text(), before)
        w.lookup_action("redo").activate(None)
        self.assertEqual(entry.get_text(), before+" edited")
        self.assertEqual([track.id for track in w.history.tracks], [identifier])
        self.assertNotIn("Home", self.app.get_accels_for_action("win.home"))
        w.lookup_action("camera-undo").activate(None)
        self.assertEqual(w.history.tracks, [])
        self.assertEqual(w.selected, set())
        self.assertIsNone(w.primary)
        self.assertIsNone(w.editing_id)
        self.assertEqual(self.errors, [])

    def test_comparison_chooses_explicit_capture_and_switches_view_domain(self):
        import importlib.util
        fixture_file = Path(__file__).resolve().parents[2] / "native" / "tests" / "camera_render_fixtures.py"
        spec = importlib.util.spec_from_file_location("studio_comparison_domains", fixture_file)
        fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixtures)
        path = Path(self.temp.name) / "capture-domains.gsv"
        fixtures.write_clock_domains(path)
        self.open_scene(path)
        w = self.window
        w._scrub(.25)
        self.pump_until(lambda: w.time == .25 and not w.frames.running)
        w.tabs.set_current_page(1)
        w.compare_toggle.set_active(True)
        self.pump_until(lambda: w.time == 0 and w._comparison_result is not None and not w.comparisons.running)
        self.assertEqual(w.tabs.get_current_page(), 0)
        self.assertEqual(w.capture_entries[w.capture_drop.get_selected()].time, 0)
        w.capture_drop.set_selected(1)
        self.pump_until(lambda: w.time == 1 and w._comparison_result is not None and not w.comparisons.running)
        w.render_drop.set_selected(w.render_entries.index("view-b"))
        self.pump_until(lambda: w.document.clock == "b" and w.time == 10 and
                        w._comparison_result is not None and w._comparison_result.label == "view-b" and
                        not w.comparisons.running)
        self.assertEqual(w.document.space, "b")
        self.assertEqual(w.timeline.times.tolist(), [10, 20])
        w.capture_drop.set_selected(1)
        self.pump_until(lambda: w.time == 20 and w._comparison_result is not None and not w.comparisons.running)
        self.assertEqual(w.capture_entries[w.capture_drop.get_selected()].time, 20)
        self.assertEqual(self.errors, [])

    def test_mixed_primary_member_keeps_duplicate_ids_bound_to_owners(self):
        path = Path(self.temp.name) / "shared-identity.gsv"
        with Writer(path) as out:
            for i, owner in enumerate(("field-a","field-b")):
                frame = np.zeros((1,16),np.float32)
                frame[0,:3] = [i-.5,0,2]
                frame[0,3:7] = [.8,.08,.05,.04]
                frame[0,7:9] = 1
                frame[0,12:16] = [.9,.4,.1,-1]
                add_persistent_gaussians(out,owner,np.array([7],np.int64),[0,1],[frame,frame],label="Field "+("A" if i == 0 else "B"))
        self.open_scene(path)
        w = self.window
        w._pick("field-a",7)
        w._pick("field-b",7,extend=True)
        members = {("field-a",7),("field-b",7)}
        self.assertEqual(w.selected,members)
        self.assertTrue(w.member_drop.get_visible())
        labels = [w.member_drop.get_model().get_string(i) for i in range(w.member_drop.get_model().get_n_items())]
        self.assertTrue(any("Field A" in text and "7" in text for text in labels))
        self.assertTrue(any("Field B" in text and "7" in text for text in labels))
        for owner in ("field-a","field-b"):
            w.member_drop.set_selected(w._member_entries.index((owner,7)))
            self.assertEqual(w.primary_member,(owner,7))
            self.assertEqual(w.primary,owner)
            self.assertEqual(w.selected,members)
            self.assertIsNone(w.editing_id)
            rows = w.detail_view.rows
            owner_rows = [row.get_subtitle() for row in rows if row.get_title() == self.application.tr("property.objectID")]
            self.assertEqual(owner_rows,[owner,"field-b" if owner == "field-a" else "field-a"])
            self.assertEqual(w.detail_view.group.get_title(),"Field "+("A" if owner == "field-a" else "B"))
        self.assertEqual(self.errors,[])

    def test_trajectory_capacity_error_keeps_scene_seek_and_playback_working(self):
        from gs_studio.model import TrajectoryLimitError
        self.open_scene()
        w = self.window
        original_ids = w.frame.ids.copy()
        reason = "Complete trajectories exceed the test preview memory budget"
        with mock.patch.object(w.document,"trajectories",side_effect=TrajectoryLimitError(reason)) as build:
            w._pick("field",2**60+3)
            w._scrub(.5)
            self.pump_until(lambda: w.time == .5 and not w.frames.running)
            self.assertGreater(build.call_count,0)
            np.testing.assert_array_equal(w.frame.ids,original_ids)
            self.assertIn(reason,w.status.get_text())
            self.assertEqual(w.viewport.paths,[])
            self.assertEqual(w.viewport.frame,w.frame)
            w.toggle_playback()
            self.pump_until(lambda: w.time > .55)
            self.assertTrue(w.playing)
            w._set_playing(False)
        self.assertEqual(self.errors,[])

    def test_explicit_calibrated_preview_never_scores_linked_reference_images(self):
        self.open_scene()
        w = self.window
        w.compare_toggle.set_active(True)
        self.pump_until(lambda: w._comparison_result is not None and
                        w._comparison_result.metrics is not None and not w.comparisons.running)
        self.assertEqual(len(w.image_view.images),2)
        self.assertIn("PSNR",w.status.get_text())
        w.observation_drop.set_selected(1)
        self.pump_until(lambda: w._comparison_result is not None and
                        w._comparison_result.metrics is None and not w.comparisons.running)
        self.assertIs(self.application.PREVIEW_ONLY,w.observation_entries[w.observation_drop.get_selected()])
        self.assertEqual(len(w.image_view.images),1)
        self.assertIsNone(w._comparison_result.reference)
        self.assertNotIn("PSNR",w.status.get_text())
        self.assertTrue(all(entry.observation_id is None for entry in w.capture_entries))
        self.assertEqual([entry.time for entry in w.capture_entries],[0,1])
        w.capture_drop.set_selected(1)
        self.pump_until(lambda: w.time == 1 and w._comparison_result is not None and
                        w._comparison_result.metrics is None and not w.comparisons.running)
        self.assertEqual(len(w.image_view.images),1)
        self.assertEqual(w.capture_entries[w.capture_drop.get_selected()].time,1)
        w.observation_drop.set_selected(0)
        self.pump_until(lambda: w._comparison_result is not None and
                        w._comparison_result.metrics is not None and not w.comparisons.running)
        self.assertEqual(len(w.image_view.images),2)
        # Scrubbing deliberately to an unmatched calibration time must clear
        # the completed score rather than substituting another capture row.
        w._scrub(.25)
        self.pump_until(lambda: w.time == .25 and not w.frames.running and not w.comparisons.running)
        self.assertIsNone(w._comparison_result)
        self.assertEqual(w.image_view.images,[])
        self.assertIn("exact",w.status.get_text().lower())
        self.assertEqual(self.errors,[])

    def test_empty_source_import_retains_frame_clock_and_rejects_session_conflict(self):
        import copy
        from gs_studio.camera import CameraTrack, export_camera_animation, import_camera_animation
        empty = Path(self.temp.name) / "empty.gsv"
        with Writer(empty):
            pass
        original = empty.read_bytes()
        self.open_scene(empty)
        w = self.window
        self.assertEqual(w.document.manifest["clocks"],{})
        self.assertEqual(w.document.manifest["spaces"],{})
        space = {"unit":"meter","handedness":"right","axes":["right","down","forward"]}
        K = np.array([[50.,0,32],[0,50,24],[0,0,1]])
        def animation(identifier,unit):
            track = CameraTrack("Imported "+unit,"world","timeline",64,48,id=identifier)
            track.spaces = {"world":copy.deepcopy(space)}
            track.clocks = {"timeline":{"unit":unit}}
            track.record(0,K,np.eye(4))
            moved = np.eye(4)
            moved[0,3] = 1
            track.record(30,K,moved)
            path = Path(self.temp.name) / (identifier+".gsv")
            export_camera_animation(path,[track],w.document)
            return path
        frames = animation("authored-frame","frame")
        conflict = animation("authored-second","second")
        with self.chooser(frames):
            w.choose_file("import")
            self.pump_until(lambda: len(w.history.tracks) == 1 and not w.file_jobs.running)
        self.assertEqual(w.timeline.unit,"frame")
        self.assertEqual(float(w.rate.get_text()),30)
        self.assertEqual(w.timeline.times.tolist(),[0,30])
        self.assertEqual(w._supplemental_clocks,{"timeline":{"unit":"frame"}})
        self.assertEqual(w._supplemental_spaces,{"world":space})
        exported = Path(self.temp.name) / "exported-frames.gsv"
        with self.chooser(exported):
            w.choose_file("save")
            self.pump_until(lambda: exported.exists() and not w.file_jobs.running)
        with Scene(exported) as result:
            result.validate()
            self.assertEqual(result.manifest["clocks"]["timeline"],{"unit":"frame"})
            self.assertEqual(result.manifest["spaces"]["world"],space)
            self.assertEqual(result.manifest["metadata"]["source_document"]["manifest_sha256"],w.document.manifest_sha256)
        # Both files are individually valid against the empty source; only the
        # authoring session knows that this clock name already means frames.
        candidate = import_camera_animation(conflict,w.document,existing_ids=["authored-frame"])
        self.assertEqual([track.id for track in candidate],["authored-second"])
        undo_count, dirty = len(w.history._undo),w.dirty
        with self.chooser(conflict):
            w.choose_file("import")
            self.pump_until(lambda: bool(self.errors) and not w.file_jobs.running)
        self.assertEqual(len(self.errors),1)
        self.assertEqual([track.id for track in w.history.tracks],["authored-frame"])
        self.assertEqual(len(w.history._undo),undo_count)
        self.assertEqual(w.dirty,dirty)
        self.assertEqual(w.timeline.unit,"frame")
        self.assertEqual(float(w.rate.get_text()),30)
        self.assertEqual(w._supplemental_clocks,{"timeline":{"unit":"frame"}})
        self.assertEqual(empty.read_bytes(),original)


    def test_inspector_context_key_selector_and_comparison_controls(self):
        self.open_scene()
        w = self.window
        self.assertEqual([w.tabs.get_tab_label_text(w.tabs.get_nth_page(i)) for i in range(3)],
                         ['Selection', 'Viewport', 'Document'])
        self.assertTrue(any(row.get_title() == 'Coordinate system' for row in w.document_view.rows))
        w.create_camera()
        self.assertEqual(w.tabs.get_current_page(), 0)
        self.assertTrue(w.camera_editor.get_visible())
        w._scrub(.5)
        self.pump_until(lambda: w.time == .5 and not w.frames.running)
        w.record_key()
        w.key_drop.set_selected(0)
        self.pump_until(lambda: w.time == 0 and not w.frames.running)
        self.assertEqual(w.timeline.selected_key, 0)
        w.key_time.set_text('.25')
        w.key_time.emit('activate')
        self.pump_until(lambda: w.time == .25 and not w.frames.running)
        self.assertEqual([key.time for key in w._track().keyframes], [.25,.5])
        self.assertEqual(w.key_drop.get_selected_item().get_string(), '0.25')
        w.compare_toggle.set_active(True)
        self.pump_until(lambda: w._comparison_result is not None and not w.comparisons.running)
        self.assertTrue(w.compare_bar.get_visible())
        self.assertFalse(w.view_bar.get_sensitive())
        self.assertFalse(w.viewport_controls.get_sensitive())
        self.assertFalse(w.camera_drop.get_sensitive())
        self.assertFalse(w.near_clip_control.get_sensitive())
        self.assertTrue(all(not control.get_sensitive() for control in w.preview_controls))
        self.assertFalse(w.camera_editor.get_visible())
        w.compare_toggle.set_active(False)
        self.assertFalse(w.compare_bar.get_visible())
        self.assertTrue(w.view_bar.get_sensitive())
        self.assertTrue(w.camera_drop.get_sensitive())
        self.assertTrue(w.near_clip_control.get_sensitive())
        self.assertTrue(w.camera_editor.get_visible())
        self.assertEqual(self.errors, [])

    def test_preview_rate_and_range_commit_only_after_enter(self):
        self.open_scene()
        w = self.window
        w.rate.set_text('2')
        w.rate.emit('activate')
        self.assertEqual(w._play_bounds()[2], 2)
        w.rate.set_text('incomplete')
        self.assertEqual(w._play_bounds()[2], 2)
        w.rate.emit('activate')
        self.assertEqual(w._play_bounds()[2], 2)
        w.range_start.set_text('.25')
        w.range_end.set_text('.75')
        w.range_enabled.set_active(True)
        self.assertEqual(w._play_bounds()[:2], (.25,.75))
        w.range_end.set_text('unfinished')
        self.assertEqual(w._play_bounds()[:2], (.25,.75))
        w.range_end.emit('activate')
        self.assertFalse(w.range_enabled.get_active())
        self.assertEqual(w._play_bounds()[:2], (0,1))
        self.assertEqual(self.errors, [])

    def test_camera_control_toggle_keeps_other_selection_primary(self):
        self.open_scene()
        w = self.window
        w._pick('field', 2**60+3)
        w._camera_pick('camera',0,True)
        self.assertEqual(w.selected, {('field',2**60+3),('camera',0)})
        self.assertEqual(w.primary_member, ('camera',0))
        w._camera_pick('camera',0,True)
        self.assertEqual(w.selected, {('field',2**60+3)})
        self.assertEqual(w.primary_member, ('field',2**60+3))
        self.assertEqual(w.primary, 'field')
        self.assertIsNone(w.editing_id)

    def test_rig_view_inspection_does_not_switch_viewing_camera(self):
        path = Path(self.temp.name) / 'rig.gsv'
        with Writer(path) as out:
            K = np.tile(np.eye(3), (1,2,1,1))
            K[0,:,0,0], K[0,:,1,1] = [100,200], [100,200]
            pose = np.tile(np.eye(4), (1,2,1,1))
            pose[0,1,0,3] = 3
            add_camera(out,'rig',[0],K,pose,[[100,80],[200,160]],view_ids=['left','right'])
        self.open_scene(path)
        w = self.window
        w.outliner.selection_model.select_item(w.outliner.position_for_member(('rig',None)), True)
        self.pump_until(lambda: ('rig',0) in w._inspection_calibrations and not w.frames.running)
        before = w.viewport.get_camera()[1].copy()
        self.assertTrue(w.inspection_view.get_visible())
        w.inspection_view.set_selected(1)
        self.pump_until(lambda: ('rig',1) in w._inspection_calibrations and not w.frames.running)
        self.assertEqual(w._inspection_calibrations['rig',1].width,200)
        self.assertEqual(w._inspection_calibrations['rig',1].c2w[0,3],3)
        self.assertEqual(w.camera_drop.get_selected(),0)
        np.testing.assert_array_equal(w.viewport.get_camera()[1], before)
        w._look_through()
        self.pump_until(lambda: w.camera_drop.get_selected() == 2 and not w.frames.running)
        self.assertEqual(w.viewport.get_camera()[1][0,3],3)
        self.assertEqual(self.errors,[])

    def test_apply_camera_field_is_equivalent_to_enter_and_can_be_undone(self):
        from gi.repository import Gtk
        self.open_scene()
        w = self.window
        w.create_camera()
        original = w._track().label
        w.track_name.set_text('Applied camera')
        row = w.track_name.get_parent()
        apply = row.get_last_child()
        self.assertIsInstance(apply,Gtk.Button)
        apply.emit('clicked')
        self.assertEqual(w._track().label,'Applied camera')
        w.lookup_action('camera-undo').activate(None)
        self.assertEqual(w._track().label,original)
        self.assertEqual(self.errors,[])

    def test_camera_view_preserves_selection_and_rejects_a_foreign_domain(self):
        import importlib.util
        file = Path(__file__).resolve().parents[2] / 'native/tests/camera_render_fixtures.py'
        spec = importlib.util.spec_from_file_location('studio_view_domains',file)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        path = Path(self.temp.name)/'domains.gsv'
        fixture.write_clock_domains(path)
        self.open_scene(path)
        w = self.window
        w._pick(str(w.frame.object_ids[0]),int(w.frame.ids[0]))
        self.pump_until(lambda: not w.frames.running)
        selected = set(w.selected)
        compatible = next(i+1 for i,(owner,view,authored) in enumerate(w.camera_entries)
                          if (owner,view) in w._calibrations)
        foreign = next(i+1 for i,(owner,view,authored) in enumerate(w.camera_entries)
                       if (owner,view) not in w._calibrations)
        w.camera_drop.set_selected(compatible)
        before = w.viewport.get_camera()[1].copy()
        self.assertEqual(w.selected,selected)
        w.camera_drop.set_selected(foreign)
        self.assertEqual(w.camera_drop.get_selected(),compatible)
        self.assertEqual(w.selected,selected)
        np.testing.assert_array_equal(w.viewport.get_camera()[1],before)
        self.assertEqual(self.errors,[])

    def test_authored_keys_capture_the_current_interactive_near_clip(self):
        self.open_scene()
        w = self.window
        w.near_clip_control.set_value(.025)
        w.create_camera()
        self.assertEqual(w._track().keyframes[0].near,.025)
        w._scrub(.5)
        self.pump_until(lambda: w.time == .5 and not w.frames.running)
        w.near_clip_control.set_value(.05)
        w.record_key()
        self.assertEqual(w._track().keyframes[-1].near,.05)
        self.assertEqual(self.errors,[])

    def test_fov_editor_scales_skew_with_focal_length_and_accepts_native_boundaries(self):
        import math
        self.open_scene()
        w=self.window
        w.create_camera()
        track=w._track()
        calibration=track.at(w.time)
        k=calibration.K.copy()
        k[0,0],k[1,1],k[0,1]=300.,330.,27.
        k[:2,2]=[track.width*.35,track.height*.4]
        track.record(w.time,k,calibration.c2w)
        w._camera_edited()
        self.pump_until(lambda:not w.frames.running)
        for degrees in (70.,1.,170.):
            before=w._track().at(w.time)
            width,height=w._track().width,w._track().height
            w.fov.set_text(str(degrees))
            w.fov.emit('activate')
            self.pump_until(lambda:not w.frames.running)
            self.assertEqual(self.errors,[])
            after=w._track().at(w.time)
            focal=width/(2*math.tan(math.radians(degrees)/2))
            ratio=focal/before.K[0,0]
            self.assertAlmostEqual(after.K[0,0],focal)
            self.assertAlmostEqual(after.K[1,1],before.K[1,1]*ratio)
            self.assertAlmostEqual(after.K[0,1],before.K[0,1]*ratio)
            np.testing.assert_array_equal(after.K[:2,2],before.K[:2,2])
            np.testing.assert_array_equal(after.c2w,before.c2w)
            self.assertEqual((after.width,after.height),(width,height))
            w.lookup_action('camera-undo').activate(None)
            np.testing.assert_allclose(w._track().at(w.time).K,before.K)
            w.lookup_action('camera-redo').activate(None)
            np.testing.assert_allclose(w._track().at(w.time).K,after.K)

    def test_preview_range_and_rate_commit_pause_but_uncommitted_or_invalid_rate_do_not(self):
        self.open_scene()
        w=self.window
        w._set_playing(True)
        w.rate.set_text('2')
        self.assertTrue(w.playing)
        w.rate.emit('activate')
        self.assertFalse(w.playing)
        self.assertEqual(w._preview_rate,2)
        w._set_playing(True)
        w.rate.set_text('unfinished')
        w.rate.emit('activate')
        self.assertTrue(w.playing)
        self.assertEqual(w._preview_rate,2)
        w.range_start.set_text('.25');w.range_end.set_text('.75')
        self.assertTrue(w.playing)
        w.range_enabled.set_active(True)
        self.assertFalse(w.playing)
        w._set_playing(True)
        w.range_enabled.set_active(False)
        self.assertFalse(w.playing)
        self.assertEqual(self.errors,[])

    def test_exact_time_entry_extends_session_timeline_without_changing_source(self):
        self.open_scene()
        w=self.window
        source_times=w.document.times.copy()
        w.time_entry.set_text('2.5');w.time_entry.emit('activate')
        self.pump_until(lambda:w.time==2.5 and not w.frames.running)
        self.assertIn(2.5,w.timeline.times)
        np.testing.assert_array_equal(w.document.times,source_times)
        w._step(-1)
        self.pump_until(lambda:w.time==1 and not w.frames.running)
        w._step(1000000000)
        self.pump_until(lambda:w.time==2.5 and not w.frames.running)
        self.assertFalse(w.dirty)
        self.open_scene(self.other)
        self.assertNotIn(2.5,w.timeline.times)
        self.assertEqual(self.errors,[])

    def test_exact_id_without_owner_rejects_ambiguity_instead_of_selecting_first_field(self):
        path=Path(self.temp.name)/'ambiguous-identity.gsv'
        state=np.zeros((1,16),np.float32)
        state[0,:3]=[0,0,2];state[0,3:7]=[.8,.1,.1,.1];state[0,7:9]=1;state[0,12:16]=[1,.2,.3,-1]
        with Writer(path) as out:
            for owner in ('one','two'):
                add_persistent_gaussians(out,owner,np.array([7],np.int64),[0,1],[state,state])
        self.open_scene(path)
        w=self.window
        w._pick(None,None)
        w.id_entry.set_text('7');w.id_entry.emit('activate')
        self.assertEqual(w.selected,set())
        self.assertIsNone(w.primary)
        self.assertEqual(len(self.errors),1)
        self.assertIn('7',self.errors[0])
        self.errors.clear()
        w.outliner.select_members([('two',None)],emit=True)
        w.id_entry.emit('activate')
        self.pump_until(lambda:w.selected=={('two',7)} and not w.frames.running)
        self.assertEqual(self.errors,[])

    def test_single_key_authored_camera_can_be_viewed_across_clock_but_cannot_be_edited(self):
        self.open_scene()
        w=self.window
        w.create_camera()
        track=w._track();track.clock='independent-clock'
        w._camera_edited()
        index=next(i+1 for i,(owner,_,_) in enumerate(w.camera_entries) if owner==track.id)
        w.camera_drop.set_selected(index)
        self.assertEqual(w.camera_drop.get_selected(),index)
        self.assertTrue(any(entry['object_id']==track.id for entry in w.viewport.cameras))
        self.assertEqual(w.editing_id,track.id)
        w.record_key()
        self.assertEqual(len(self.errors),1)
        w.position.set_text('1,2,3');w.position.emit('activate')
        self.assertEqual(len(self.errors),2)
        self.assertEqual(len(track.keyframes),1)
        self.errors.clear()
        calibration=track.at(0)
        track.record(1,calibration.K,calibration.c2w)
        w._camera_edited()
        self.assertEqual(w.camera_drop.get_selected(),0)
        self.assertFalse(any(entry['object_id']==track.id for entry in w.viewport.cameras))
        self.assertEqual(self.errors,[])

    def test_copy_attributes_preserves_literal_markup_and_entities_without_global_clipboard(self):
        owner='source&amp;literal &#55; <b>field</b>'
        path=Path(self.temp.name)/'literal-attributes.gsv'
        state=np.zeros((1,16),np.float32)
        state[0,:3]=[0,0,2];state[0,3:7]=[.8,.1,.1,.1];state[0,7:9]=1;state[0,12:16]=[1,.2,.3,-1]
        with Writer(path) as out:
            add_persistent_gaussians(out,owner,np.array([7],np.int64),[0,1],[state,state])
        self.open_scene(path)
        w=self.window
        w.outliner.select_members([(owner,None)],emit=True)
        self.pump_until(lambda:not w.frames.running)
        self.assertIn((self.application.tr('property.identity'),owner),w.detail_view.plain_rows)
        clipboard=mock.Mock()
        with mock.patch.object(w,'get_clipboard',return_value=clipboard):
            w._copy_attributes()
        clipboard.set.assert_called_once()
        copied=clipboard.set.call_args.args[0]
        self.assertIn(self.application.tr('property.identity')+': '+owner,copied.splitlines())
        self.assertEqual(self.errors,[])

    def test_foreign_camera_keys_cannot_seek_or_retime_and_are_hidden_from_active_lane(self):
        self.open_scene()
        w=self.window
        w.create_camera()
        track=w._track();track.clock='independent-clock'
        w._camera_edited()
        self.assertEqual(w.timeline.keys,[])
        before=w.time
        w._select_key(0)
        self.assertEqual(w.time,before)
        w.timeline.selected_key=0
        w.key_time.set_text('3');w.key_time.emit('activate')
        self.assertEqual(len(self.errors),1)
        self.assertEqual(track.keyframes[0].time,0)
        w._retime(0,4)
        self.assertEqual(len(self.errors),2)
        self.assertEqual(w._track().keyframes[0].time,0)
        self.errors.clear()
        track.clock=w.document.clock
        w._camera_edited()
        self.assertEqual(w.timeline.keys,[0])
        w.timeline.selected_key=0
        w.key_time.set_text('2');w.key_time.emit('activate')
        self.pump_until(lambda:w.time==2 and not w.frames.running)
        self.assertEqual(w._track().keyframes[0].time,2)
        self.assertEqual(self.errors,[])

    def test_document_and_header_counts_include_session_cameras_and_authored_times(self):
        import json
        empty=Path(self.temp.name)/'empty-source.gsv'
        with Writer(empty):pass
        source_bytes=empty.read_bytes()
        self.open_scene(empty)
        w=self.window
        source_manifest=json.dumps(w.document.manifest,sort_keys=True)
        w.create_camera()
        camera_id=w._track().id
        w._pick(None,None)
        w.time_entry.set_text('2.5');w.time_entry.emit('activate')
        self.pump_until(lambda:w.time==2.5 and not w.frames.running)
        w._inspect()
        rows=dict(w.document_view.plain_rows)
        self.assertEqual(rows[self.application.tr('document.objects')],'1')
        self.assertEqual(rows[self.application.tr('document.samples')],'2')
        summary=dict(w.detail_view.plain_rows)
        self.assertIsNone(w.primary)
        self.assertEqual(summary[self.application.tr('document.objects')],rows[self.application.tr('document.objects')])
        self.assertEqual(summary[self.application.tr('document.samples')],rows[self.application.tr('document.samples')])
        w.time_entry.set_text('3.5');w.time_entry.emit('activate')
        self.pump_until(lambda:w.time==3.5 and not w.frames.running)
        self.assertEqual(dict(w.detail_view.plain_rows)[self.application.tr('document.samples')],'3')
        self.assertEqual(dict(w.document_view.plain_rows)[self.application.tr('document.samples')],'3')
        w.time_entry.set_text('2.5');w.time_entry.emit('activate')
        self.pump_until(lambda:w.time==2.5 and not w.frames.running)
        self.assertEqual(w.title_widget.get_subtitle(),self.application.tr('ubuntu.counts').format(objects=1,samples=3))
        self.assertEqual(w.sample_label.get_text(),'2 / 3')
        self.assertEqual(len(w.document.objects),0)
        np.testing.assert_array_equal(w.document.times,[0])
        self.assertEqual(empty.read_bytes(),source_bytes)
        self.assertEqual(json.dumps(w.document.manifest,sort_keys=True),source_manifest)
        w.outliner.select_members([(camera_id,None)],emit=True)
        w.delete_camera()
        w._pick(None,None)
        self.pump_until(lambda:not w.frames.running)
        w._inspect()
        self.assertEqual(dict(w.document_view.plain_rows)[self.application.tr('document.objects')],'0')
        self.assertEqual(dict(w.detail_view.plain_rows)[self.application.tr('document.objects')],'0')
        self.assertEqual(json.dumps(w.document.manifest,sort_keys=True),source_manifest)
        self.assertEqual(self.errors,[])


if __name__ == "__main__":
    unittest.main()
