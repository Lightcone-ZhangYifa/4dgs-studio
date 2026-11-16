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
        row = w.outliner.get_first_child()
        while row.object_id != "field":
            row = row.get_next_sibling()
        w.outliner.select_row(row)
        self.assertEqual(w.selected, {("field", None)})
        self.pump_until(lambda: len(w.viewport.paths) > 0 and not w.frames.running)
        w.id_entry.set_text(str(2**60 + 3))
        w.id_entry.emit("activate")
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
        self.assertIsNone(w.editing_id)
        w.delete_camera()
        self.assertEqual([track.id for track in w.history.tracks], [identifier])
        user_index = next(i+1 for i, entry in enumerate(w.camera_entries) if entry[0] == identifier)
        w.camera_drop.set_selected(user_index)
        self.assertEqual(w.editing_id, identifier)
        w._pick("field", 2**60+3)
        self.assertIsNone(w.editing_id)
        w.delete_camera()
        self.assertEqual([track.id for track in w.history.tracks], [identifier])
        w.primary = identifier
        w.selected = {(identifier, None)}
        w._selection_changed()
        row = w.outliner.get_first_child()
        while row.object_id != "field":
            row = row.get_next_sibling()
        w.outliner.unselect_all()
        w.outliner.select_row(row)
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
        w.compare_toggle.set_active(True)
        self.pump_until(lambda: w.time == 0 and w._comparison_result is not None and not w.comparisons.running)
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


if __name__ == "__main__":
    unittest.main()
