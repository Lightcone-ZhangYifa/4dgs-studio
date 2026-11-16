"""GTK 4 / libadwaita application shell for the shared GSV scene contract."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import sys
import time

import gi
import numpy as np

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_foreign("cairo")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APPLICATION_ID, VERSION
from .i18n import tr, read_preferences, save_preferences
from .jobs import LatestJob, CommandJobs
from .model import SceneDocument, Trajectory
from .camera import CameraTrack, CameraHistory, export_camera_animation, import_camera_animation
from .timeline import Timeline
from .viewport import Viewport
from .image_view import ComparisonCanvas
from .inspector import Inspector
from .comparison import compare_view
from .capture_catalog import build_capture_catalog


def box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, **kwargs):
    return Gtk.Box(orientation=orientation, spacing=spacing, **kwargs)


def label(text, **kwargs):
    return Gtk.Label(label=text, **kwargs)


def button(text, callback, icon=None):
    widget = Gtk.Button(icon_name=icon) if icon else Gtk.Button(label=text)
    widget.set_tooltip_text(text)
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])
    widget.connect("clicked", lambda *_: callback())
    return widget


def compact_dropdown(widget, width):
    """Keep native selectors readable without letting long source names resize the workbench."""
    def factory(chars):
        result = Gtk.SignalListItemFactory()
        result.connect("setup", lambda _, item: item.set_child(Gtk.Label(xalign=0, ellipsize=3, max_width_chars=chars)))
        def bind(_, item):
            value = item.get_item().get_string()
            item.get_child().set_text(value)
            item.get_child().set_tooltip_text(value)
        result.connect("bind", bind)
        return result
    widget.set_factory(factory(width))
    widget.set_list_factory(factory(60))
    return widget


def scroller(child, **kwargs):
    scroll = Gtk.ScrolledWindow(**kwargs)
    scroll.set_child(child)
    return scroll


class StudioWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="4DGS Studio", default_width=1440, default_height=900)
        self.document = None
        self.closed = False
        self.dirty = False
        self.playing = False
        self.time = self.target_time = 0.0
        self.last_tick = time.monotonic()
        self.visible = set()
        self.selected = set()
        self._outline_extend = False
        self._outline_primary = None
        self.primary = None
        self.frame = None
        self.history = CameraHistory()
        self.editing_id = None
        self.camera_entries = []
        self.render_entries = []
        self.capture_catalog = {}
        self.capture_entries = []
        self._calibrations = {}
        self._inspected_views = {}
        self._updating = False
        self._paths_revision = 0
        self._path_signature = None
        self._paths_revision += 1
        self._frame_scene = False
        self._domain_busy = False
        self._domain_revision = 0
        self._file_busy = False
        self._chooser_generation = 0
        self._pending_confirm = False
        self._compare_revision = 0
        self._comparison_result = None
        self._edit_revision = 0
        self.file_jobs = CommandJobs(lambda fn: GLib.idle_add(fn, priority=GLib.PRIORITY_DEFAULT))
        self.loader = LatestJob(lambda fn: GLib.idle_add(fn, priority=GLib.PRIORITY_DEFAULT))
        self.frames = LatestJob(lambda fn: GLib.idle_add(fn, priority=GLib.PRIORITY_DEFAULT), allow_intermediate=True)
        self.comparisons = LatestJob(lambda fn: GLib.idle_add(fn, priority=GLib.PRIORITY_DEFAULT))
        self._build()
        self._actions()
        self.connect("close-request", self._close_requested)
        self._timer = GLib.timeout_add(16, self._tick)

    def _build(self):
        root = box(Gtk.Orientation.VERTICAL, 0)
        self.set_content(root)
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="4DGS Studio", subtitle=tr("file.openGSV", "Open GSV Scene"))
        header.set_title_widget(self.title_widget)
        header.pack_start(button(tr("file.open", "Open…"), lambda: self.choose_file("open"), "document-open-symbolic"))
        header.pack_start(button(tr("camera.save", "Save Camera Animation…"), lambda: self.choose_file("save"), "document-save-symbolic"))
        self.spinner = Gtk.Spinner()
        header.pack_end(self.spinner)
        menu = Gio.Menu()
        for title, action in [
            (tr("camera.create", "Create Camera from View"), "create-camera"),
            (tr("camera.recordAtTime", "Record Keyframe at Current Time"), "record"),
            (tr("camera.import", "Import Camera Animation…"), "import-camera"),
            (tr("camera.undo", "Undo Camera Edit"), "undo"),
            (tr("camera.redo", "Redo Camera Edit"), "redo"),
            (tr("view.previous", "Previous View"), "previous-view"),
            (tr("view.next", "Next View"), "next-view"),
            (tr("file.screenshot", "Save Screenshot…"), "screenshot"),
            (tr("file.workspaceSnapshot", "Save Workspace Snapshot…"), "workspace-snapshot"),
            (tr("view.toggleOutliner", "Toggle Scene Sidebar"), "toggle-outliner"),
            (tr("view.toggleInspector", "Toggle Inspector"), "toggle-inspector"),
            (tr("view.fullscreen", "Enter Full Screen"), "fullscreen"),
            (tr("common.preferences", "Preferences"), "preferences"),
            (tr("nav.help", "Navigation guide"), "help"),
            (tr("about.title", "About 4DGS Studio"), "about"),
        ]:
            menu.append(title, "win."+action)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        root.append(header)
        self.progress = box(spacing=10, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.progress_label = label("", hexpand=True, xalign=0)
        self.progress.append(self.progress_label)
        self.cancel_button = button(tr("common.cancel", "Cancel"), self.cancel_loading)
        self.progress.append(self.cancel_button)
        self.progress.set_visible(False)
        root.append(self.progress)
        self.outer_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        root.append(self.outer_paned)
        left = box(Gtk.Orientation.VERTICAL, 6, width_request=200)
        left.add_css_class("sidebar")
        left.append(label(tr("outliner.title", "Scene Outliner"), xalign=0, margin_start=12, margin_top=10))
        self.search = Gtk.SearchEntry(placeholder_text=tr("outliner.search", "Search objects"), margin_start=8, margin_end=8)
        self.search.connect("search-changed", lambda *_: self._filter_objects())
        left.append(self.search)
        self.outliner = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE, activate_on_single_click=False)
        self.outliner.connect("selected-rows-changed", self._object_selection)
        self.outliner.connect("row-activated", self._object_activated)
        outline_click = Gtk.GestureClick(button=1, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        def outline_pressed(gesture, n, x, y):
            self._outline_extend = bool(gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK)
            row = self.outliner.get_row_at_y(int(y))
            self._outline_primary = row.object_id if row else None
        outline_click.connect("pressed", outline_pressed)
        self.outliner.add_controller(outline_click)
        left.append(scroller(self.outliner, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.outer_paned.set_start_child(left)
        self.outer_paned.set_resize_start_child(False)
        self.outer_paned.set_position(230)
        self.inner_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.outer_paned.set_end_child(self.inner_paned)
        center = box(Gtk.Orientation.VERTICAL, 0, width_request=400)
        self.inner_paned.set_start_child(center)
        self.inner_paned.set_resize_end_child(False)
        self.inner_paned.set_position(920)
        view_bar = box(spacing=6, margin_start=8, margin_end=8, margin_top=6, margin_bottom=6)
        self.camera_drop = Gtk.DropDown.new_from_strings([tr("view.free", "Free perspective")])
        compact_dropdown(self.camera_drop, 30)
        self.camera_drop.set_hexpand(True)
        self.camera_drop.connect("notify::selected", self._camera_changed)
        view_bar.append(self.camera_drop)
        tools = Gtk.DropDown.new_from_strings([tr("nav.select", "Select"), tr("nav.orbit", "Orbit"), tr("nav.pan", "Pan"), tr("nav.dolly", "Dolly")])
        tools.connect("notify::selected", lambda w, _: setattr(self.viewport, "tool", ["select", "orbit", "pan", "dolly"][w.get_selected()]))
        view_bar.append(tools)
        view_bar.append(button(tr("view.focus", "Frame selection · F"), self._frame_selection, "zoom-in-symbolic"))
        view_bar.append(button(tr("view.frameAll", "Frame scene · Home"), self._home, "zoom-fit-best-symbolic"))
        center.append(view_bar)
        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.viewport = Viewport()
        self.viewport.on_pick = self._pick
        self.viewport.on_camera_pick = self._camera_pick
        self.viewport.on_error = lambda message: self.status.set_text(message)
        self.viewport.on_stats = self._render_stats
        self.viewport.on_camera_changed = self._free_camera
        self.viewport.on_key = self._viewport_key
        self.stack.add_named(self.viewport, "scene")
        self.image_view = ComparisonCanvas()
        self.stack.add_named(self.image_view, "comparison")
        self.empty = Adw.StatusPage(title="4DGS Studio", description=tr("file.openGSV", "Open a GSV scene to begin"), icon_name="applications-graphics-symbolic")
        self.empty.set_child(button(tr("file.openGSV", "Open GSV Scene"), lambda: self.choose_file("open")))
        self.stack.add_named(self.empty, "empty")
        self.stack.set_visible_child_name("empty")
        center.append(self.stack)
        preview = box(spacing=4, margin_start=8, margin_end=8, margin_top=6, margin_bottom=6)
        for key, text, attribute in [("display.grid", "Grid", "show_grid"), ("category.cameras", "Cameras", "show_cameras"), ("preview.paths", "Paths", "show_paths"), ("preview.trails", "Trails", "show_trails")]:
            toggle = Gtk.ToggleButton(label=tr(key, text), active=True)
            toggle.connect("toggled", self._toggle_preview, attribute)
            preview.append(toggle)
        preview.append(label(tr("display.quality", "Resolution"), margin_start=8))
        resolution = Gtk.DropDown.new_from_strings(["100%", "75%", "50%", "25%"])
        resolution.connect("notify::selected", lambda w, _: self._set_render_scale([1, .75, .5, .25][w.get_selected()]))
        preview.append(resolution)
        self.compare_toggle = Gtk.ToggleButton(label=tr("compare.button", "Render / GT"))
        self.compare_toggle.connect("toggled", self._comparison_toggled)
        preview.append(self.compare_toggle)
        center.append(preview)
        compare_bar = box(spacing=6, margin_start=8, margin_end=8, margin_bottom=6)
        self.render_drop = Gtk.DropDown.new_from_strings([tr("render.noViews", "No render views")])
        compact_dropdown(self.render_drop, 18)
        self.render_drop.set_hexpand(True)
        self.render_drop.connect("notify::selected", lambda *_: self._render_selected())
        compare_bar.append(self.render_drop)
        self.observation_drop = Gtk.DropDown.new_from_strings([tr("ubuntu.autoObservation", "Auto observation")])
        compact_dropdown(self.observation_drop, 14)
        self.observation_entries = [None]
        self.observation_drop.connect("notify::selected", lambda *_: self._observation_selected())
        compare_bar.append(self.observation_drop)
        self.capture_drop = Gtk.DropDown.new_from_strings([tr("timeline.source")])
        compact_dropdown(self.capture_drop, 20)
        self.capture_drop.set_tooltip_text(tr("compare.observation"))
        self.capture_drop.connect("notify::selected", lambda *_: self._capture_selected())
        compare_bar.append(self.capture_drop)
        compare_bar.append(button(tr("compare.fit", "Fit images"), lambda: self.image_view.reset(), "zoom-fit-best-symbolic"))
        compare_bar.append(button(tr("compare.actualPixels", "Actual pixels"), lambda: self.image_view.actual_pixels(), "zoom-original-symbolic"))
        compare_bar.append(button(tr("compare.refresh", "Refresh comparison"), self._request_comparison, "view-refresh-symbolic"))
        center.append(compare_bar)
        self.domain_label = label("", xalign=0, margin_start=10, margin_bottom=4, ellipsize=3)
        self.domain_label.add_css_class("dim-label")
        center.append(self.domain_label)
        self.timeline = Timeline()
        self.timeline.on_seek = self._scrub
        self.timeline.on_key_select = self._select_key
        self.timeline.on_key_move = self._retime
        self.timeline.on_play = self.toggle_playback
        center.append(self.timeline)
        center.append(self._transport())
        self.inspector = self._inspector()
        self.inner_paned.set_end_child(self.inspector)
        self.status = label(tr("status.ready", "Ready"), xalign=0, hexpand=True, margin_start=10, margin_end=10, margin_top=5, margin_bottom=5, ellipsize=3)
        self.status.set_selectable(True)
        status_bar = box(spacing=10)
        status_bar.append(self.status)
        self.performance_label = label("", xalign=1, margin_end=10, width_chars=36, max_width_chars=36, ellipsize=3)
        self._last_stats_update = 0.
        self.performance_label.add_css_class("dim-label")
        status_bar.append(self.performance_label)
        root.append(status_bar)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._drop)
        self.add_controller(drop)

    def _transport(self):
        column = box(Gtk.Orientation.VERTICAL, 5, margin_start=8, margin_end=8, margin_top=6, margin_bottom=8)
        row = box(spacing=4)
        row.append(button(tr("timeline.first", "First sample"), lambda: self._step(-1000000000), "media-skip-backward-symbolic"))
        row.append(button(tr("timeline.previous", "Previous sample"), lambda: self._step(-1), "media-seek-backward-symbolic"))
        self.play_button = button(tr("timeline.play", "Play / pause · Space"), self.toggle_playback, "media-playback-start-symbolic")
        row.append(self.play_button)
        row.append(button(tr("timeline.next", "Next sample"), lambda: self._step(1), "media-seek-forward-symbolic"))
        row.append(button(tr("timeline.last", "Last sample"), lambda: self._step(1000000000), "media-skip-forward-symbolic"))
        self.time_entry = Gtk.Entry(width_chars=13, placeholder_text=tr("timeline.time", "Time"))
        self.time_entry.connect("activate", lambda w: self._guard(lambda: self._scrub(float(w.get_text()))))
        row.append(self.time_entry)
        self.loop = Gtk.CheckButton(label=tr("timeline.loop", "Loop"), active=True)
        row.append(self.loop)
        self.snap = Gtk.CheckButton(label=tr("timeline.snap", "Snap"), active=True)
        self.snap.connect("toggled", lambda w: setattr(self.timeline, "snap", w.get_active()))
        row.append(self.snap)
        speed = Gtk.DropDown.new_from_strings(["0.25×", "0.5×", "1×", "2×", "4×"])
        speed.set_selected(2)
        self.speed = speed
        row.append(speed)
        row.append(button(tr("timeline.fit", "Fit timeline"), self.timeline.fit, "zoom-fit-best-symbolic"))
        column.append(row)
        row2 = box(spacing=6)
        self.range_enabled = Gtk.CheckButton(label=tr("timeline.workRange", "Preview range"))
        self.range_enabled.connect("toggled", lambda *_: self._preview_range_changed())
        row2.append(self.range_enabled)
        self.range_start = Gtk.Entry(width_chars=10, placeholder_text=tr("timeline.in", "In time"))
        self.range_end = Gtk.Entry(width_chars=10, placeholder_text=tr("timeline.out", "Out time"))
        self.range_start.connect("activate", lambda *_: self._preview_range_changed())
        self.range_end.connect("activate", lambda *_: self._preview_range_changed())
        row2.append(self.range_start)
        row2.append(self.range_end)
        row2.append(label(tr("timeline.previewLabel", "Preview")+" /s"))
        self.rate = Gtk.Entry(text="1", width_chars=7)
        row2.append(self.rate)
        self.sample_label = label("", hexpand=True, xalign=1)
        row2.append(self.sample_label)
        column.append(row2)
        return column

    def _inspector(self):
        panel = box(Gtk.Orientation.VERTICAL, 6, width_request=270)
        panel.add_css_class("sidebar")
        self.selection_label = label(tr("selection.none", "No selection"), xalign=0, margin_start=12, margin_top=10, wrap=True)
        panel.append(self.selection_label)
        find = box(spacing=4, margin_start=8, margin_end=8)
        self.id_entry = Gtk.Entry(placeholder_text=tr("selection.exactID", "Exact Gaussian ID"), hexpand=True, width_chars=12)
        self.id_entry.connect("activate", lambda *_: self._find_id())
        find.append(self.id_entry)
        find.append(button(tr("selection.find", "Find ID"), self._find_id, "edit-find-symbolic"))
        panel.append(find)
        self.tabs = Gtk.Notebook(vexpand=True)
        self.detail_view = Inspector()
        self.tabs.append_page(scroller(self.detail_view), label(tr("inspector.object", "Properties")))
        camera_box = box(Gtk.Orientation.VERTICAL, 8, margin_start=10, margin_end=10, margin_top=10, margin_bottom=10)
        camera_box.append(button(tr("camera.create", "Create Camera from View"), self.create_camera))
        camera_box.append(button(tr("camera.record", "Record view at playhead"), self.record_key))
        self.track_name = self._field(camera_box, tr("camera.name", "Camera name"), lambda: self._edit_track("name"))
        self.key_time = self._field(camera_box, tr("camera.keyTime", "Keyframe time"), lambda: self._edit_track("time"))
        self.fov = self._field(camera_box, tr("camera.keyFOV", "Horizontal FOV (degrees)"), lambda: self._edit_track("fov"))
        self.position = self._field(camera_box, "XYZ", lambda: self._edit_track("pose"))
        self.rotation = self._field(camera_box, "WXYZ", lambda: self._edit_track("pose"))
        key_actions = box(spacing=4)
        key_actions.append(button(tr("camera.keyDelete", "Delete key"), self.delete_key))
        key_actions.append(button(tr("camera.delete", "Delete camera"), self.delete_camera))
        camera_box.append(key_actions)
        camera_box.append(button(tr("camera.undo", "Undo Camera Edit"), lambda: self._undo(False)))
        camera_box.append(button(tr("camera.redo", "Redo Camera Edit"), lambda: self._undo(True)))
        camera_box.append(button(tr("camera.save", "Save Camera Animation…"), lambda: self.choose_file("save")))
        camera_box.append(button(tr("camera.import", "Import Camera Animation…"), lambda: self.choose_file("import")))
        self.tabs.append_page(scroller(camera_box), label(tr("camera.view", "Camera")))
        view = box(Gtk.Orientation.VERTICAL, 10, margin_start=10, margin_end=10, margin_top=10)
        view.append(label(tr("nav.title", "Navigation"), xalign=0))
        mode = Gtk.DropDown.new_from_strings([tr("navigation.turntable", "Turntable"), tr("navigation.trackball", "Trackball")])
        mode.connect("notify::selected", lambda w, _: self._set_view_property("navigation_mode", ["turntable", "trackball"][w.get_selected()]))
        view.append(mode)
        view.append(button(tr("navigation.pivotView", "Use visible scene depth"), lambda: self.viewport.center_on_depth()))
        pivot_toggle = Gtk.CheckButton(label=tr("navigation.showPivot", "Show orbit center"), active=True)
        pivot_toggle.connect("toggled", lambda w: self._set_view_property("show_pivot", w.get_active()))
        view.append(pivot_toggle)
        background = Gtk.DropDown.new_from_strings([tr("background.studio", "Studio"), tr("background.black", "Black"), tr("background.gray", "Gray"), tr("background.white", "White")])
        background.connect("notify::selected", lambda w, _: self._set_view_property("background", ["studio", "black", "gray", "white"][w.get_selected()]))
        view.append(label(tr("preview.background", "Preview background"), xalign=0))
        view.append(background)
        axes = Gtk.DropDown.new_from_strings(["Y up", "Z up", "X up", "−Y up", "−Z up", "−X up"])
        axes.connect("notify::selected", lambda w, _: self._set_view_property("up_axis", ["y", "z", "x", "-y", "-z", "-x"][w.get_selected()]))
        view.append(axes)
        for text, preset in [("Front", "front"), ("Right", "right"), ("Top", "top"), ("Isometric", "isometric")]:
            view.append(button(tr("view."+preset,text), lambda p=preset: self._preset(p)))
        for key, text, prop, value, low, high, step in [
            ("preview.pathDensity", "Path density", "path_density", 1, .01, 1, .01),
            ("preview.pathOpacity", "Path opacity", "path_opacity", .18, 0, 1, .01),
            ("preview.trailFrames", "Trail intervals", "trail_intervals", 8, 1, 1000, 1),
            ("display.size", "Gaussian size", "gaussian_scale", 1, .1, 4, .05),
            ("display.opacity", "Opacity multiplier", "opacity_multiplier", 1, .05, 2, .05),
            ("display.exposure", "Exposure multiplier", "exposure_multiplier", 1, .1, 4, .05),
            ("display.nearClip", "Near clip in scene units", "near_clip", .001, .0001, 100, .001),
            ("navigation.sensitivity", "Navigation sensitivity", "sensitivity", 1, .1, 4, .1),
        ]:
            view.append(label(tr(key, text), xalign=0))
            spin = Gtk.SpinButton.new_with_range(low, high, step)
            spin.set_value(value)
            spin.set_digits(0 if step == 1 else 6 if prop == "near_clip" else 2)
            spin.connect("value-changed", self._view_option, prop)
            view.append(spin)
        self.tabs.append_page(scroller(view), label(tr("inspector.viewport", "Viewport")))
        panel.append(self.tabs)
        return panel

    def _field(self, parent, text, callback):
        parent.append(label(text, xalign=0))
        entry = Gtk.Entry()
        entry.connect("activate", lambda *_: self._guard(callback))
        entry.set_tooltip_text(text+" · Enter")
        parent.append(entry)
        return entry

    def _actions(self):
        actions = {
            "open": lambda: self.choose_file("open"), "save": lambda: self.choose_file("save"),
            "create-camera": self.create_camera, "record": self.record_key,
            "import-camera": lambda: self.choose_file("import"),
            "undo": lambda: self._undo_or_text(False), "redo": lambda: self._undo_or_text(True),
            "camera-undo": lambda: self._undo(False), "camera-redo": lambda: self._undo(True), "close": self.close,
            "previous-view": lambda: self._view_history(-1), "next-view": lambda: self._view_history(1),
            "screenshot": lambda: self.export_image(False), "workspace-snapshot": lambda: self.export_image(True),
            "toggle-outliner": lambda: self.outer_paned.get_start_child().set_visible(not self.outer_paned.get_start_child().get_visible()),
            "toggle-inspector": lambda: self.inspector.set_visible(not self.inspector.get_visible()),
            "fullscreen": lambda: self.unfullscreen() if self.is_fullscreen() else self.fullscreen(),
            "preferences": self.preferences, "help": self.help, "about": self.about,
            "play": self.toggle_playback, "frame": self._frame_selection, "home": self._home,
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda a, p, fn=callback: self._guard(fn))
            self.add_action(action)
        for name, keys in {"open": ["<Primary>o"], "save": ["<Primary>s"], "screenshot": ["<Primary><Shift>s"], "workspace-snapshot": ["<Primary><Alt><Shift>s"], "create-camera": ["<Primary><Shift>k"], "toggle-outliner": ["<Primary>1"], "toggle-inspector": ["<Primary>2"], "fullscreen": ["F11"], "record": ["<Primary>k"], "undo": ["<Primary>z"], "redo": ["<Primary><Shift>z"], "camera-undo": ["<Primary><Alt>z"], "camera-redo": ["<Primary><Alt><Shift>z"], "close": ["<Primary>w"], "previous-view": ["<Primary><Alt>bracketleft"], "next-view": ["<Primary><Alt>bracketright"]}.items():
            self.get_application().set_accels_for_action("win."+name, keys)

    def _entry_editing(self, entry):
        focus = self.get_focus()
        return focus is entry or (focus is not None and focus.is_ancestor(entry))

    def _undo_or_text(self, redo):
        focus = self.get_focus()
        # Gtk.Entry's Gtk.Text child handles native undo without changing cameras.
        if isinstance(focus, (Gtk.Editable, Gtk.TextView)):
            action = "text.redo" if redo else "text.undo"
            focus.activate_action(action, None)
            return
        self._undo(redo)

    def _guard(self, work):
        try:
            return work()
        except Exception as exc:
            self.error(exc)

    def error(self, error):
        if self.closed:
            return
        self.status.set_text(str(error))
        dialog = Adw.MessageDialog(transient_for=self, heading=tr("error.title", "Scene operation failed"), body=str(error))
        dialog.add_response("ok", tr("common.ok", "OK"))
        dialog.present()

    def _set_loading(self, message=None, cancellable=True):
        self.progress.set_visible(message is not None)
        self.cancel_button.set_visible(cancellable)
        self.progress_label.set_text(message or "")
        self.spinner.set_spinning(message is not None)

    def cancel_loading(self):
        self.loader.invalidate()
        self._set_loading(None)

    def choose_file(self, operation, after=None):
        if self._file_busy:
            return
        if operation != "open" and self.document is None:
            self.error(tr("camera.openFirst", "Open a source GSV document first."))
            return
        dialog = Gtk.FileDialog(title={"open": tr("file.openGSV", "Open GSV Scene"), "save": tr("camera.saveTitle", "Save Camera Animation"), "import": tr("camera.importTitle", "Import Camera Animation")}[operation], modal=True)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        file_filter = Gtk.FileFilter(name="GSV scene (*.gsv)")
        file_filter.add_pattern("*.gsv")
        filters.append(file_filter)
        dialog.set_filters(filters)
        if operation == "save":
            if not self.history.tracks:
                self.error(tr("ubuntu.createBeforeSave", "Create a camera before saving camera animation."))
                return
            dialog.set_initial_name("camera-animation.gsv")
        self._file_busy = True
        source = self.document
        chooser_generation = self._chooser_generation
        def done(dialog, result):
            if self.closed or chooser_generation != self._chooser_generation:
                return
            self._file_busy = False
            try:
                chosen = dialog.save_finish(result) if operation == "save" else dialog.open_finish(result)
                path = chosen.get_path()
                if path is None:
                    raise ValueError(tr("ubuntu.localFile", "Choose a local GSV file."))
                if self.closed:
                    return
                if operation == "open":
                    self.open_path(path)
                elif source is not self.document:
                    raise ValueError(tr("ubuntu.changedDuringDialog", "Scene changed while the camera dialog was open."))
                elif operation == "save":
                    tracks = [t.clone() for t in self.history.tracks]
                    edit_revision = self._edit_revision
                    self._file_busy = True
                    self._set_loading(tr("camera.saveTitle", "Saving camera animation…"), cancellable=False)
                    def saved(value, error):
                        self._file_busy = False
                        self._set_loading(None)
                        if error:
                            self.error(error)
                        else:
                            if edit_revision == self._edit_revision:
                                self.dirty = False
                            self.status.set_text(str(path))
                            if after and not self.dirty:
                                after()
                    self.file_jobs.submit(lambda: export_camera_animation(path, tracks, source, overwrite=True), saved)
                else:
                    self._file_busy = True
                    self._set_loading(tr("camera.importTitle", "Importing camera animation…"), cancellable=False)
                    def imported(tracks, error):
                        self._file_busy = False
                        self._set_loading(None)
                        if error:
                            self.error(error)
                        else:
                            self.history.remember()
                            self.history.tracks.extend(tracks)
                            self.visible.update(t.id for t in tracks)
                            self.editing_id = tracks[0].id if tracks else self.editing_id
                            if tracks:
                                self.primary = tracks[0].id
                                self.selected = {(self.primary, None)}
                            self._camera_edited()
                    existing_ids = [t.id for t in self.history.tracks]
                    self.file_jobs.submit(lambda: import_camera_animation(path, source, existing_ids=existing_ids), imported)
            except GLib.Error as exc:
                if not exc.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                    self.error(exc)
            except Exception as exc:
                self.error(exc)
        if operation == "save":
            dialog.save(self, None, done)
        else:
            dialog.open(self, None, done)

    def _confirm_discard(self, callback):
        if not self.dirty:
            callback()
            return
        if self._pending_confirm:
            return
        self._pending_confirm = True
        dialog = Adw.MessageDialog(transient_for=self, heading=tr("camera.openUnsaved", "Unsaved camera animation"), body=tr("camera.openUnsavedHint", "Save your camera animation before continuing."))
        dialog.add_response("cancel", tr("common.cancel", "Cancel"))
        dialog.add_response("discard", tr("common.discard", "Discard"))
        dialog.add_response("save", tr("camera.save", "Save Camera Animation…"))
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        def response(dialog, response):
            self._pending_confirm = False
            if response == "discard":
                self.dirty = False
                callback()
            elif response == "save":
                self.choose_file("save", callback)
        dialog.connect("response", response)
        dialog.present()

    def open_path(self, path):
        self._confirm_discard(lambda: self._open(path))

    def _open(self, path):
        self._set_playing(False)
        self._set_loading(tr("file.openGSV", "Opening GSV scene…"))
        revision = self.loader.revision + 1
        def load():
            document = SceneDocument(path, cancelled=lambda: self.closed or revision != self.loader.revision)
            try:
                catalog = build_capture_catalog(document)
                return document, catalog
            except BaseException:
                document.close()
                raise
        self.loader.submit(load, self._loaded, lambda value: value[0].close())

    def _loaded(self, value, error):
        self._set_loading(None)
        if error:
            self.error(error)
            return
        document, self.capture_catalog = value
        self.frames.invalidate()
        self.file_jobs.invalidate()
        self.comparisons.invalidate()
        self._compare_revision += 1
        self._domain_busy = False
        self._domain_revision += 1
        self._calibrations = {}
        self._inspected_views = {}
        self.image_view.reset()
        self._comparison_result = None
        self._chooser_generation += 1
        self._file_busy = False
        old, self.document = self.document, document
        if old is not None:
            self.frames.executor.submit(old.close)
        self.history = CameraHistory()
        self.editing_id = None
        self.dirty = False
        self.selected.clear()
        self.primary = None
        self.visible = set(document.visible_ids) | {o["id"] for o in document.cameras if o.get("space") == document.space and o.get("clock") == document.clock}
        self.title_widget.set_title(Path(document.path).name)
        self.set_title(Path(document.path).name+" — 4DGS Studio")
        self.title_widget.set_subtitle(tr("ubuntu.counts").format(objects=len(document.objects), samples=len(document.times)))
        self.timeline.times = document.times
        self.timeline.unit = getattr(document, "time_unit", "second")
        self.timeline.fit()
        self.time = self.target_time = float(document.times[0])
        self.range_start.set_text(f"{document.times[0]:.17g}")
        self.range_end.set_text(f"{document.times[-1]:.17g}")
        self.range_enabled.set_active(False)
        self.rate.set_text("30" if self.timeline.unit == "frame" else "1")
        self._path_signature = None
        self._paths_revision += 1
        self._frame_scene = True
        self._rebuild_objects()
        self._rebuild_cameras()
        self.render_entries = [o["id"] for o in document.objects if o["encoding"] == "gsv.render.view"]
        self._updating = True
        self.render_drop.set_model(Gtk.StringList.new([document.scene.object(id).get("label", id) for id in self.render_entries] or [tr("render.noViews", "No render views")]))
        self.render_drop.set_selected(0)
        self.compare_toggle.set_active(False)
        self._updating = False
        self.stack.set_visible_child_name("scene")
        self.domain_label.set_text(tr("ubuntu.domain").format(space=document.space, clock=document.clock, unit=tr("unit."+self.timeline.unit,self.timeline.unit)))
        self._render_selected()
        self._request_frame(self.time)
        self._inspect()

    def _rebuild_objects(self):
        self._updating = True
        while self.outliner.get_first_child():
            self.outliner.remove(self.outliner.get_first_child())
        if self.document:
            for obj in [*self.document.objects, *[{"id": t.id, "label": t.label, "type": "camera", "encoding": "gsv.camera.pinhole", "authored": True} for t in self.history.tracks]]:
                row = Gtk.ListBoxRow()
                row.object_id = obj["id"]
                row.object_info = obj
                content = box(spacing=6, margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
                check = Gtk.CheckButton(active=obj["id"] in self.visible)
                check.set_tooltip_text(tr("scene.visibility", "Object visibility"))
                check.set_sensitive(obj["id"] in self.document.supported_ids or obj["type"] == "camera")
                check.connect("toggled", self._visibility_changed, obj["id"])
                content.append(check)
                names = box(Gtk.Orientation.VERTICAL, 2, hexpand=True)
                names.append(label(obj.get("label", obj["id"]), xalign=0, ellipsize=3))
                sub = label(obj["type"] + (" · user" if obj.get("authored") else ""), xalign=0)
                sub.add_css_class("dim-label")
                names.append(sub)
                content.append(names)
                row.set_child(content)
                self.outliner.append(row)
                if (obj["id"], None) in self.selected:
                    self.outliner.select_row(row)
        self._updating = False
        self._filter_objects()

    def _filter_objects(self):
        text = self.search.get_text().casefold()
        row = self.outliner.get_first_child()
        while row:
            obj = row.object_info
            row.set_visible(text in (obj.get("label", "")+" "+obj["id"]+" "+obj["type"]).casefold())
            row = row.get_next_sibling()

    def _object_selection(self, widget):
        if self._updating:
            return
        elements = {(o,i) for o,i in self.selected if i is not None} if self._outline_extend else set()
        self.selected = elements | {(r.object_id, None) for r in widget.get_selected_rows()}
        rows = widget.get_selected_rows()
        identifiers = [r.object_id for r in rows]
        self.primary = self._outline_primary if self._outline_primary in identifiers else identifiers[-1] if identifiers else None
        self.editing_id = self.primary if self.primary in {t.id for t in self.history.tracks} else None
        self._selection_changed()

    def _object_activated(self, widget, row):
        obj = row.object_info
        if not obj.get("authored") and (obj.get("space") != self.document.space or (obj.get("clock") and obj.get("clock") != self.document.clock)) and (obj["id"] in self.document.supported_ids or obj["encoding"] in ("gsv.camera.pinhole", "gsv.render.view")):
            self._activate_domain(row.object_id, lambda: self._object_activated(widget, row))
            return
        for i, (identifier, view, authored) in enumerate(self.camera_entries):
            if identifier == row.object_id:
                self.camera_drop.set_selected(i+1)
                return
        if row.object_id in self.render_entries:
            self.render_drop.set_selected(self.render_entries.index(row.object_id))
            self.compare_toggle.set_active(True)

    def _visibility_changed(self, check, identifier):
        if self._updating:
            return
        if check.get_active():
            obj = next((o for o in self.document.objects if o["id"] == identifier), None)
            if obj and obj["id"] in self.document.supported_ids and (obj.get("space") != self.document.space or (obj["encoding"] != "gsv.gaussian.static" and obj.get("clock") != self.document.clock)):
                check.set_active(False)
                self.status.set_text(tr("ubuntu.domainHint", "Double-click this object to activate its coordinate space and clock."))
                return
            self.visible.add(identifier)
        else:
            self.visible.discard(identifier)
        self._request_frame(self.target_time)

    def _selection_changed(self):
        editing = self.primary if self.primary in {t.id for t in self.history.tracks} else None
        if editing != self.editing_id:
            self.timeline.selected_key = None
        self.editing_id = editing
        self.viewport.set_selection(self.selected)
        self.viewport.queue_render()
        self._path_signature = None
        self._paths_revision += 1
        self._request_frame(self.target_time)
        self._inspect()
        self._refresh_camera_editor(force=True)

    def _pick(self, object_id, identity, extend=False):
        item = (object_id, int(identity)) if object_id is not None else None
        if not extend:
            self.selected.clear()
        if item:
            if extend and item in self.selected:
                self.selected.remove(item)
            else:
                self.selected.add(item)
            self.primary = object_id
            self.id_entry.set_text(str(identity))
        else:
            self.primary = None
        self._updating = True
        self.outliner.unselect_all()
        row = self.outliner.get_first_child()
        while row:
            if (row.object_id, None) in self.selected:
                self.outliner.select_row(row)
            row = row.get_next_sibling()
        self._updating = False
        self._selection_changed()

    def _find_id(self):
        def find():
            identity = int(self.id_entry.get_text().strip())
            if not -(1 << 63) <= identity < 1 << 63:
                raise ValueError(tr("selection.invalidID", "Enter an exact signed 64-bit integer ID."))
            if self.frame is None:
                return
            indices = np.flatnonzero(self.frame.ids == identity)
            if hasattr(self.frame, 'element'):
                indices = [i for i in indices if self.frame.element(str(self.frame.object_ids[i]), identity).valid]
            if self.primary:
                matching = [i for i in indices if self.frame.object_ids[i] == self.primary]
                indices = matching or indices
            if not len(indices):
                raise ValueError(tr("ubuntu.idAbsent", "Identity is not present at the displayed time."))
            self._pick(str(self.frame.object_ids[indices[0]]), identity)
        self._guard(find)

    def _request_frame(self, requested_time):
        if not self.document or self.closed or self._domain_busy:
            return
        requested_time = float(requested_time)
        if not math.isfinite(requested_time):
            raise ValueError(tr("timeline.invalidTime", "Enter a finite document time."))
        self.target_time = requested_time
        doc, visible, selected = self.document, set(self.visible), set(self.selected)
        signature = (frozenset(selected), frozenset(visible), float(self.viewport.path_density), self._edit_revision, doc.path)
        rebuild = signature != self._path_signature
        density = self.viewport.path_density
        paths_revision = self._paths_revision
        frame_generation = self.frames.generation
        authored = copy.deepcopy([t for t in self.history.tracks if (t.id, None) in selected]) if rebuild else []
        def evaluate():
            frame = doc.evaluate(requested_time, object_ids=[id for id in dict.fromkeys([*doc.visible_ids, *doc.supported_ids]) if id in visible], cancelled=lambda: self.closed or doc is not self.document or frame_generation != self.frames.generation)
            paths = None
            if rebuild:
                paths = []
                for identifier in {o for o, i in selected}:
                    obj = next((o for o in doc.objects if o["id"] == identifier), None)
                    if obj and identifier in visible and obj.get("space") == doc.space and (obj.get("clock") == doc.clock or obj["encoding"] == "gsv.gaussian.static") and obj["type"] in ("gaussian", "scaffold", "camera"):
                        ids = None if (identifier, None) in selected else [i for o, i in selected if o == identifier and i is not None]
                        paths.extend(doc.trajectories(identifier, ids=ids, density=density if ids is None else 0, cancelled=lambda: self.closed or doc is not self.document or paths_revision != self._paths_revision))
                for track in authored:
                    if track.id in visible and track.space == doc.space and track.clock == doc.clock and len(track.keyframes) > 1:
                        points = np.stack([k.c2w[:3,3] for k in track.keyframes])
                        times = np.array([k.time for k in track.keyframes], np.float64)
                        paths.append(Trajectory(points, times, np.tile([.4,.7,1.], (len(times),1)), None, track.id))
            calibrations = {}
            for camera in doc.cameras:
                if camera.get("space") != doc.space:
                    continue
                if camera.get("clock") != doc.clock and len(doc.scene.array(camera["data"]["times"])) != 1:
                    continue
                for view in range(len(camera["data"]["view_ids"])):
                    calibrations[(camera["id"], view)] = doc.camera(camera["id"], requested_time, view)
            return frame, paths, requested_time, calibrations
        def display(value, error):
            if doc is not self.document:
                return
            if isinstance(error, InterruptedError):
                return
            if error:
                self._set_playing(False)
                self.status.set_text(str(error))
                return
            frame, paths, actual_time, calibrations = value
            self._calibrations = calibrations
            self.frame, self.time = frame, actual_time
            self.viewport.set_frame(frame)
            if paths is not None:
                self.viewport.set_paths(paths)
                self._path_signature = signature
            self.viewport.time = actual_time
            self.viewport.source_times = doc.times
            if self._frame_scene:
                self.viewport.frame_scene()
                self._frame_scene = False
            self._update_camera_pose()
            self._refresh_camera_editor()
            self.timeline.time = actual_time
            self.timeline.queue_draw()
            if not self._entry_editing(self.time_entry):
                self.time_entry.set_text(f"{actual_time:.17g}")
            index = int(np.clip(np.searchsorted(doc.times, actual_time), 0, len(doc.times)-1))
            self.sample_label.set_text(f"{index+1} / {len(doc.times)}")
            warnings = getattr(frame, "warnings", [])
            self.status.set_text(tr("ubuntu.frame").format(count=f"{getattr(frame,'valid_count',len(frame.ids)):,}",time=f"{actual_time:.9g}") + (" · "+"; ".join(warnings) if warnings else ""))
            self._inspect()
            if self.compare_toggle.get_active():
                self._request_comparison()
        self.frames.submit(evaluate, display)

    def _scrub(self, value):
        self._set_playing(False)
        self._guard(lambda: self._request_frame(value))

    def _step(self, direction):
        if self.document is None:
            return
        self._set_playing(False)
        times = self.timeline.times
        index = int(np.searchsorted(times, self.time, side="right" if direction > 0 else "left"))
        index += 0 if direction > 0 else -1
        if abs(direction) > 1:
            index = len(times)-1 if direction > 0 else 0
        self._request_frame(float(times[max(0, min(index, len(times)-1))]))

    def _set_playing(self, playing):
        self.playing = playing
        self.last_tick = time.monotonic()
        self.play_button.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")

    def toggle_playback(self):
        if self.document is None or len(self.timeline.times) < 2:
            return
        if not self.playing:
            try:
                self._play_bounds()
            except ValueError as error:
                self.error(error)
                return
        self._set_playing(not self.playing)

    def _preview_range_changed(self):
        self.timeline.preview_range = None
        if self.document and self.range_enabled.get_active():
            try:
                lower, upper, _ = self._play_bounds()
                self.timeline.preview_range = (lower, upper)
            except ValueError as error:
                self.status.set_text(str(error))
        self.timeline.queue_draw()

    def _play_bounds(self):
        lower, upper = map(float, (self.timeline.times[0], self.timeline.times[-1]))
        if self.range_enabled.get_active():
            a, b = float(self.range_start.get_text()), float(self.range_end.get_text())
            if not (lower <= a < b <= upper):
                raise ValueError(tr("timeline.invalidRange", "Preview in/out must be ordered and within the document timeline."))
            lower, upper = a, b
        rate = float(self.rate.get_text())
        if not math.isfinite(rate) or not 0 < rate <= 10000:
            raise ValueError(tr("timeline.invalidRate", "Preview rate must be greater than zero and at most 10000 units/s."))
        return lower, upper, rate

    def _tick(self):
        if self.closed:
            return False
        now = time.monotonic()
        if self.playing and self.document:
            try:
                lower, upper, rate = self._play_bounds()
                delta = min(.25, now-self.last_tick) * rate * [.25, .5, 1, 2, 4][self.speed.get_selected()]
                target = max(lower, self.target_time) + delta
                if target > upper:
                    if self.loop.get_active() and upper > lower:
                        target = lower+(target-lower) % (upper-lower)
                    else:
                        target = upper
                        self._set_playing(False)
                self._request_frame(target)
            except Exception as exc:
                self._set_playing(False)
                self.error(exc)
        self.last_tick = now
        return True

    def _rebuild_cameras(self):
        names = [tr("view.free", "Free perspective")]
        entries = []
        if self.document:
            for obj in self.document.cameras:
                for i, view in enumerate(obj["data"]["view_ids"]):
                    entries.append((obj["id"], i, False))
                    names.append(obj.get("label", obj["id"])+" · "+view)
            for track in self.history.tracks:
                entries.append((track.id, 0, True))
                names.append(track.label+" · user")
        old = self.camera_entries[self.camera_drop.get_selected()-1] if 0 < self.camera_drop.get_selected() <= len(self.camera_entries) else None
        self._updating = True
        self.camera_entries = entries
        self.camera_drop.set_model(Gtk.StringList.new(names))
        self.camera_drop.set_selected(entries.index(old)+1 if old in entries else 0)
        self._updating = False

    def _track(self):
        return next((t for t in self.history.tracks if t.id == self.editing_id), None)

    def _camera_changed(self, widget, _param):
        if self._updating or not self.document:
            return
        index = widget.get_selected()
        if index > 0:
            identifier, view, authored = self.camera_entries[index-1]
            self._inspected_views[identifier] = view
            self.editing_id = identifier if authored else None
            self.primary = identifier
            self.selected = {(identifier, None)}
            self.timeline.selected_key = None
            self._selection_changed()
        else:
            self.viewport.set_camera(None)

    def _update_camera_pose(self):
        if not self.document:
            return
        wireframes = []
        for identifier, view, authored in self.camera_entries:
            try:
                if authored:
                    track = next(t for t in self.history.tracks if t.id == identifier)
                    if (track.space, track.clock) != (self.document.space or "world", self.document.clock or "timeline"):
                        continue
                    calibration = track.at(self.time)
                else:
                    obj = self.document.object(identifier)
                    if (identifier, view) not in self._calibrations:
                        continue
                    calibration = self._calibrations[(identifier, view)]
                if identifier in self.visible:
                    wireframes.append({"calibration": calibration, "object_id": identifier, "view": view, "selected": self.primary == identifier})
            except Exception:
                continue
        self.viewport.set_camera_wireframes(wireframes)
        index = self.camera_drop.get_selected()
        if 0 < index <= len(self.camera_entries):
            identifier, view, authored = self.camera_entries[index-1]
            try:
                if authored:
                    track = next(t for t in self.history.tracks if t.id == identifier)
                    if (track.space, track.clock) != (self.document.space or "world", self.document.clock or "timeline"):
                        raise ValueError("Camera and viewport space/clock differ. Select compatible geometry first.")
                    calibration = track.at(self.time)
                else:
                    obj = self.document.object(identifier)
                    if (identifier, view) not in self._calibrations:
                        raise ValueError("Camera and viewport space/clock differ. Select compatible geometry first.")
                    calibration = self._calibrations[(identifier, view)]
                self.viewport.set_camera(calibration, record_history=False)
            except Exception as error:
                self._updating = True
                self.camera_drop.set_selected(0)
                self._updating = False
                self.status.set_text(str(error))

    def _camera_pick(self, identifier, view):
        self._inspected_views[identifier] = view
        for i, (id, v, authored) in enumerate(self.camera_entries):
            if (id, v) == (identifier, view):
                self.primary = id
                self.selected = {(id, None)}
                self.editing_id = id if authored else None
                self._selection_changed()
                return

    def _free_camera(self):
        self._updating = True
        self.camera_drop.set_selected(0)
        self._updating = False

    def create_camera(self):
        def create():
            if self.document is None:
                raise ValueError(tr("camera.openFirst", "Open a GSV document first."))
            k, pose, width, height = self.viewport.get_camera()
            track = CameraTrack(tr("ubuntu.newCamera").format(number=len(self.history.tracks)+1), self.document.space or "world", self.document.clock or "timeline", width, height)
            track.record(self.time, k, pose)
            self.history.remember()
            self.history.tracks.append(track)
            self.visible.add(track.id)
            self.editing_id = self.primary = track.id
            self.selected = {(track.id, None)}
            self._camera_edited()
            self.tabs.set_current_page(1)
        self._guard(create)

    def record_key(self):
        def record():
            track = self._track()
            if track is None:
                raise ValueError(tr("ubuntu.selectAuthored", "Create or select an authored camera before recording a keyframe."))
            if (track.space, track.clock) != (self.document.space or "world", self.document.clock or "timeline"):
                raise ValueError(tr("camera.recordDomainMismatch", "Camera and viewport domains differ."))
            k, pose, width, height = self.viewport.get_camera()
            edited = track.clone()
            edited.record(self.time, k, pose, width, height)
            self.history.remember()
            self.history.tracks[self.history.tracks.index(track)] = edited
            self._camera_edited()
        self._guard(record)

    def _camera_edited(self):
        self._path_signature = None
        self._paths_revision += 1
        self._edit_revision += 1
        self.dirty = True
        self._rebuild_objects()
        self._rebuild_cameras()
        self._refresh_camera_editor()
        self._update_camera_pose()
        self._inspect()
        self._update_timeline()
        self.viewport.set_selection(self.selected)
        self._request_frame(self.time)

    def _update_timeline(self):
        if not self.document:
            return
        extra = [k.time for t in self.history.tracks if t.clock == (self.document.clock or "timeline") and t.space == self.document.space for k in t.keyframes]
        self.timeline.times = np.unique(np.concatenate([self.document.times, extra]))
        self.timeline.queue_draw()

    def _refresh_camera_editor(self, force=False):
        track = self._track()
        self.timeline.keys = [k.time for k in track.keyframes] if track else []
        self.timeline.queue_draw()
        if not track:
            for entry in (self.track_name, self.key_time, self.fov, self.position, self.rotation):
                entry.set_text("")
            return
        from gsv.evaluate import matrix_quat
        calibration = track.at(self.time)
        index = self.timeline.selected_key
        key = track.keyframes[index] if index is not None and index < len(track.keyframes) else None
        values = [(self.track_name, track.label),
                  (self.key_time, f"{key.time:.17g}" if key else ""),
                  (self.fov, f"{math.degrees(2*math.atan(track.width/(2*calibration.K[0,0]))):.7g}"),
                  (self.position, ", ".join(f"{x:.9g}" for x in calibration.c2w[:3,3])),
                  (self.rotation, ", ".join(f"{x:.9g}" for x in matrix_quat(calibration.c2w[:3,:3])))]
        for entry, value in values:
            if force or not self._entry_editing(entry):
                entry.set_text(value)

    def _select_key(self, index):
        track = self._track()
        if track and 0 <= index < len(track.keyframes):
            self.timeline.selected_key = index
            self._scrub(track.keyframes[index].time)
            self._refresh_camera_editor()

    def _retime(self, index, value):
        def edit():
            track = self._track()
            if track is None:
                return
            changed = track.clone()
            changed.retime(index, value)
            self.history.remember()
            self.history.tracks[self.history.tracks.index(track)] = changed
            self.timeline.selected_key = next(i for i,k in enumerate(changed.keyframes) if k.time == value)
            self._camera_edited()
            self._scrub(value)
        self._guard(edit)

    def _edit_track(self, operation):
        track = self._track()
        if not track:
            raise ValueError(tr("ubuntu.selectCamera", "Select an authored camera first."))
        if operation in ("pose", "fov") and (track.space, track.clock) != (self.document.space, self.document.clock):
            raise ValueError(tr("camera.transformDomain"))
        changed = track.clone()
        if operation == "name":
            name = self.track_name.get_text().strip()
            if not 1 <= len(name) <= 120:
                raise ValueError(tr("camera.invalidName", "Camera names require 1–120 characters."))
            changed.label = name
        elif operation == "time":
            index = self.timeline.selected_key
            if index is None:
                raise ValueError(tr("ubuntu.selectKey", "Select a keyframe in the timeline first."))
            self._retime(index, float(self.key_time.get_text()))
            return
        else:
            from gsv.evaluate import quat_matrix
            calibration = track.at(self.time)
            k, pose = calibration.K.copy(), calibration.c2w.copy()
            if operation == "fov":
                fov = float(self.fov.get_text())
                if not math.isfinite(fov) or not 1 < fov < 170:
                    raise ValueError(tr("camera.invalidFOV", "Horizontal FOV must be between 1° and 170°."))
                focal = track.width/(2*math.tan(math.radians(fov)/2))
                k[1,1] *= focal/k[0,0]
                k[0,0] = focal
            else:
                p = np.array([float(s) for s in self.position.get_text().replace(",", " ").split()])
                q = np.array([float(s) for s in self.rotation.get_text().replace(",", " ").split()])
                if p.shape != (3,) or q.shape != (4,) or not np.isfinite(p).all() or not np.isfinite(q).all() or np.linalg.norm(q) < 1e-12:
                    raise ValueError(tr("camera.invalidTransform", "Enter finite XYZ and nonzero WXYZ components."))
                pose[:3,3], pose[:3,:3] = p, quat_matrix(q)
            changed.record(self.time, k, pose)
        self.history.remember()
        self.history.tracks[self.history.tracks.index(track)] = changed
        self._camera_edited()

    def delete_key(self):
        def remove():
            track = self._track()
            index = self.timeline.selected_key
            if track is None or index is None:
                return
            changed = track.clone()
            changed.remove(index)
            self.history.remember()
            self.history.tracks[self.history.tracks.index(track)] = changed
            self.timeline.selected_key = None
            self._camera_edited()
        self._guard(remove)

    def delete_camera(self):
        track = self._track()
        if track:
            self.history.remember()
            self.history.tracks.remove(track)
            self.selected.discard((track.id, None))
            self.editing_id = self.primary = None
            self._camera_edited()

    def _undo(self, redo):
        if (self.history.redo() if redo else self.history.undo()):
            existing = {o["id"] for o in self.document.objects} | {t.id for t in self.history.tracks}
            self.selected = {(o,i) for o,i in self.selected if o in existing}
            if self.primary not in existing:
                self.primary = None
            self.editing_id = self.primary if self.primary in {t.id for t in self.history.tracks} else None
            self.timeline.selected_key = None
            self._camera_edited()

    def _inspect(self):
        if not self.document:
            return
        if self.compare_toggle.get_active():
            self.detail_view.comparison(self._comparison_result, message=None if self._comparison_result else tr("ubuntu.rendering"))
            return
        track = next((t for t in self.history.tracks if t.id == self.primary), None)
        calibration = track.at(self.time) if track else self._calibrations.get((self.primary, self._inspected_views.get(self.primary, 0)))
        self.selection_label.set_text(tr("ubuntu.selected").format(count=len(self.selected)) + (" · "+self.primary if self.primary else ""))
        self.detail_view.update(self.document, self.primary, self.selected, self.frame, calibration, track, self.time)

    def _render_selected(self):
        if self._updating or not self.document:
            return
        index = self.render_drop.get_selected()
        observations = []
        if 0 <= index < len(self.render_entries):
            observations = self.document.object(self.render_entries[index])["links"].get("observations", [])
        self.observation_entries = [None, *observations]
        self._updating = True
        self.observation_drop.set_model(Gtk.StringList.new([tr("ubuntu.autoObservation"), *[self.document.object(id).get("label", id) for id in observations]]))
        self.observation_drop.set_selected(0)
        self._updating = False
        self._rebuild_capture_rows()
        if self.compare_toggle.get_active():
            self._activate_capture()

    def _observation_selected(self):
        if self._updating:
            return
        self._rebuild_capture_rows()
        if self.compare_toggle.get_active():
            self._activate_capture()

    def _rebuild_capture_rows(self):
        index = self.render_drop.get_selected()
        view_id = self.render_entries[index] if index < len(self.render_entries) else None
        observation = self.observation_entries[min(self.observation_drop.get_selected(), len(self.observation_entries)-1)]
        self.capture_entries = [entry for entry in self.capture_catalog.get(view_id, [])
                                if observation is None or entry.observation_id == observation]
        exact = next((i for i,e in enumerate(self.capture_entries) if e.time == self.time), 0)
        self._updating = True
        self.capture_drop.set_model(Gtk.StringList.new([e.label for e in self.capture_entries] or [tr("render.previewAction")]))
        self.capture_drop.set_selected(exact)
        self.capture_drop.set_sensitive(bool(self.capture_entries))
        self._updating = False

    def _capture_selected(self):
        if not self._updating and self.compare_toggle.get_active():
            self._activate_capture()

    def _activate_capture(self):
        index = self.render_drop.get_selected()
        if self.document is None or not index < len(self.render_entries):
            self._request_comparison()
            return
        view_id = self.render_entries[index]
        view = self.document.object(view_id)
        def seek():
            row = self.capture_drop.get_selected()
            if row < len(self.capture_entries):
                self._scrub(self.capture_entries[row].time)
            else:
                self._request_comparison()
        if (view.get("space"), view.get("clock")) != (self.document.space, self.document.clock):
            self._activate_domain(view_id, seek)
        else:
            self._set_playing(False)
            seek()

    def _activate_domain(self, identifier, after=None):
        self._domain_revision += 1
        domain_revision = self._domain_revision
        self._domain_busy = True
        self.editing_id = None
        self.timeline.selected_key = None
        self._paths_revision += 1
        doc = self.document
        self._set_playing(False)
        self.frames.invalidate()
        self._set_loading(tr("ubuntu.activateDomain", "Activating coordinate space and clock…"), cancellable=False)
        future = self.frames.executor.submit(doc.activate_domain, identifier)
        def finished(future):
            try:
                result, error = future.result(), None
            except Exception as exc:
                result, error = None, exc
            def publish():
                if self.closed or doc is not self.document or domain_revision != self._domain_revision:
                    return False
                self._domain_busy = False
                self._set_loading(None)
                if error:
                    self.error(error)
                    return False
                self.visible = set(result) | {o["id"] for o in doc.cameras if o.get("space") == doc.space}
                self.timeline.unit = doc.clock_unit
                self._update_timeline()
                self.timeline.fit()
                self.target_time = self.time = float(doc.times[0])
                self._path_signature = None
                self._paths_revision += 1
                self._frame_scene = True
                self._calibrations = {}
                self.range_start.set_text(f"{doc.times[0]:.17g}")
                self.range_end.set_text(f"{doc.times[-1]:.17g}")
                self.range_enabled.set_active(False)
                self.domain_label.set_text(tr("ubuntu.domain").format(space=doc.space, clock=doc.clock, unit=tr("unit."+doc.clock_unit,doc.clock_unit)))
                self._rebuild_objects()
                self._request_frame(self.time)
                if after:
                    after()
                return False
            GLib.idle_add(publish, priority=GLib.PRIORITY_DEFAULT)
        future.add_done_callback(finished)

    def _request_comparison(self):
        if self._updating or self._domain_busy or not self.document or not self.compare_toggle.get_active():
            return
        index = self.render_drop.get_selected()
        if not 0 <= index < len(self.render_entries):
            self.status.set_text(tr("ubuntu.renderPrompt", "Select a render view to compare calibrated images."))
            return
        self._compare_revision += 1
        revision = self._compare_revision
        path, view_id, actual_time = self.document.path, self.render_entries[index], self.time
        observation_id = self.observation_entries[min(self.observation_drop.get_selected(), len(self.observation_entries)-1)]
        row = self.capture_drop.get_selected()
        matching = next((i for i,e in enumerate(self.capture_entries) if e.time == actual_time and
                        (row >= len(self.capture_entries) or e.observation_id == self.capture_entries[row].observation_id)), None)
        if matching is not None:
            observation_id = self.capture_entries[matching].observation_id
        self._updating = True
        self.capture_drop.set_selected(matching if matching is not None else (1 << 32)-1)
        self._updating = False
        self.image_view.clear()
        self._comparison_result = None
        self.detail_view.comparison(message=tr("ubuntu.rendering", "Rendering calibrated image…"))
        self.status.set_text(tr("ubuntu.rendering", "Rendering calibrated image…"))
        def capture():
            # Captures own their file descriptor, independent of scene replacement.
            with SceneDocument(path, cancelled=lambda: self.closed or revision != self._compare_revision) as source:
                return compare_view(source, view_id, actual_time, observation_id=observation_id, cancelled=lambda: self.closed or revision != self._compare_revision)
        def completed(result, error):
            if revision != self._compare_revision:
                return
            if error:
                self.image_view.clear()
                self.detail_view.comparison(message=str(error))
                self.status.set_text(str(error))
                return
            self._comparison_result = result
            self.detail_view.comparison(result)
            self.image_view.set_images(result.render, result.reference, result.color_space)
            scores = ""
            if result.metrics:
                scores = " · ".join(f"{key.upper()} {result.metrics[key]:.6g}" if result.metrics.get(key) is not None else f"{key.upper()} —" for key in ["psnr", "ssim", "mse", "mae"])
            self.status.set_text(result.label+" · "+(result.role or "render")+" · CPU reference · "+scores)
            self.status.set_tooltip_text("\n".join(result.details) if not isinstance(result.details, str) else result.details)
        self.comparisons.submit(capture, completed)

    def _comparison_toggled(self, widget):
        if self._updating:
            return
        self._compare_revision += 1
        self.comparisons.invalidate()
        if widget.get_active():
            self.performance_label.set_text("")
            self._set_playing(False)
            self.stack.set_visible_child_name("comparison")
            self._rebuild_capture_rows()
            self._activate_capture()
        else:
            self.stack.set_visible_child_name("scene" if self.document else "empty")
            self.image_view.clear()
            self._inspect()

    def _render_stats(self, stats):
        if not self.compare_toggle.get_active() and time.monotonic() - self._last_stats_update >= .25:
            self._last_stats_update = time.monotonic()
            self.performance_label.set_text(f"CPU {stats['cpu_submission_ms']:.1f} ms · {stats['render_width']} × {stats['render_height']}")
            self.performance_label.set_tooltip_text(tr("ubuntu.submissionTiming", "CPU frame submission time; GPU completion and display frame rate are not measured."))

    def _toggle_preview(self, widget, attribute):
        setattr(self.viewport, attribute, widget.get_active())
        self.viewport.queue_render()

    def _set_render_scale(self, scale):
        self.viewport.resolution_scale = scale
        self.viewport.queue_render()

    def _set_view_property(self, name, value):
        if name == "up_axis":
            self.viewport.set_up_axis(value)
        else:
            setattr(self.viewport, name, value)
        self.viewport.queue_render()

    def _view_option(self, spin, attribute):
        setattr(self.viewport, attribute, spin.get_value())
        if attribute == "path_density":
            self._path_signature = None
            self._paths_revision += 1
            self._request_frame(self.target_time)
        self.viewport.queue_render()

    def _frame_selection(self):
        self._free_camera()
        camera = self._calibrations.get((self.primary, self._inspected_views.get(self.primary, 0)))
        if camera is not None and self.frame is not None:
            self.viewport.navigation.pivot = camera.c2w[:3,3].copy()
            self.viewport.navigation.c2w[:3,3] = camera.c2w[:3,3] - self.viewport.navigation.c2w[:3,2] * max(1, np.linalg.norm(camera.c2w[:3,3])*.25)
            self.viewport.free_camera()
            self.viewport.queue_render()
        else:
            self.viewport.frame_selection()

    def _home(self):
        self._free_camera()
        self.viewport.frame_scene()

    def _preset(self, preset):
        self._free_camera()
        self.viewport.preset(preset)

    def _view_history(self, direction):
        self._free_camera()
        if direction < 0:
            self.viewport.previous_view()
        else:
            self.viewport.next_view()

    def _viewport_key(self, keyval, state):
        if keyval == Gdk.KEY_space:
            self.toggle_playback()
        elif keyval == Gdk.KEY_Left:
            self._step(-1)
        elif keyval == Gdk.KEY_Right:
            self._step(1)
        elif keyval == Gdk.KEY_Escape:
            self._set_playing(False)
            self._pick(None, None)
        else:
            return False
        return True

    def _drop(self, target, value, x, y):
        files = value.get_files()
        if files and files[0].get_path():
            self.open_path(files[0].get_path())
            return True
        return False

    def export_image(self, workspace=False):
        if self._file_busy:
            return
        from .snapshots import snapshot_png
        try:
            # Capture before a portal/save sheet dims the parent window.
            if workspace:
                png = snapshot_png(self)
            elif self.compare_toggle.get_active():
                import io
                stream = io.BytesIO()
                self.image_view.snapshot_surface().write_to_png(stream)
                png = stream.getvalue()
            else:
                png = snapshot_png(self.viewport)
        except Exception as exc:
            self.error(exc)
            return
        dialog = Gtk.FileDialog(title=tr("file.workspaceSnapshot" if workspace else "file.screenshot"), modal=True)
        dialog.set_initial_name("4dgs-workspace.png" if workspace else "4dgs-screenshot.png")
        image_filters = Gio.ListStore.new(Gtk.FileFilter)
        image_filter = Gtk.FileFilter(name="PNG")
        image_filter.add_mime_type("image/png")
        image_filters.append(image_filter)
        dialog.set_filters(image_filters)
        self._file_busy = True
        generation = self._chooser_generation
        def done(dialog, result):
            if self.closed or generation != self._chooser_generation:
                return
            self._file_busy = False
            try:
                target = dialog.save_finish(result).get_path()
                if target is None:
                    raise ValueError(tr("ubuntu.localFile"))
                if self.document and Path(target).exists() and Path(target).samefile(self.document.path):
                    raise ValueError(tr("camera.exportOverwrite", "Source reconstruction cannot be overwritten."))
                def save():
                    from .snapshots import write_png
                    write_png(target, png)
                self._file_busy = True
                def saved(_, error):
                    self._file_busy = False
                    if error:
                        self.error(error)
                    else:
                        self.status.set_text(str(target))
                self.file_jobs.submit(save, saved)
            except GLib.Error as error:
                if not error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                    self.error(error)
            except Exception as error:
                self.error(error)
        dialog.save(self, None, done)

    def preferences(self):
        dialog = Adw.PreferencesWindow(transient_for=self, modal=True, title=tr("common.preferences", "Preferences"))
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=tr("language.title", "Language and appearance"), description=tr("language.restartMessage"))
        prefs = read_preferences()
        language = Adw.ComboRow(title=tr("language.title", "Language"), model=Gtk.StringList.new([tr("ubuntu.system", "System"), "English", "简体中文"]))
        languages = ["system", "en", "zh-Hans"]
        language.set_selected(languages.index(prefs.get("language", "system")) if prefs.get("language", "system") in languages else 0)
        def changed_language(widget, _):
            prefs["language"] = languages[widget.get_selected()]
            save_preferences(prefs)
        language.connect("notify::selected", changed_language)
        group.add(language)
        appearance = Adw.ComboRow(title=tr("ubuntu.appearance", "Appearance"), model=Gtk.StringList.new([tr("ubuntu.system", "System"), tr("ubuntu.light", "Light"), tr("ubuntu.dark", "Dark")]))
        appearance.set_selected(prefs.get("appearance", 0))
        def changed_appearance(widget, _):
            prefs["appearance"] = widget.get_selected()
            save_preferences(prefs)
            Adw.StyleManager.get_default().set_color_scheme([Adw.ColorScheme.DEFAULT, Adw.ColorScheme.FORCE_LIGHT, Adw.ColorScheme.FORCE_DARK][widget.get_selected()])
        appearance.connect("notify::selected", changed_appearance)
        group.add(appearance)
        page.add(group)
        dialog.add(page)
        dialog.present()

    def help(self):
        dialog = Adw.MessageDialog(transient_for=self, heading=tr("nav.help", "Navigation guide"), body=tr("nav.guide"))
        dialog.add_response("ok", tr("common.ok", "OK"))
        dialog.present()

    def about(self):
        dialog = Adw.AboutWindow(transient_for=self, modal=True, application_name="4DGS Studio", application_icon=APPLICATION_ID, developer_name="Yifa Zhang · 张亦法", version=VERSION, website="https://github.com/Lightcone-ZhangYifa/4dgs-studio", license_type=Gtk.License.MIT_X11)
        dialog.present()

    def _close_requested(self, window):
        if self.closed:
            return False
        if self.dirty:
            self._confirm_discard(self._finish_close)
            return True
        self._finish_close()
        return True

    def _finish_close(self):
        self.closed = True
        self._chooser_generation += 1
        self._set_playing(False)
        self._compare_revision += 1
        GLib.source_remove(self._timer)
        self.frames.invalidate()
        if self.document:
            self.frames.executor.submit(self.document.close)
            self.document = None
        for worker in (self.loader, self.frames, self.comparisons, self.file_jobs):
            worker.close()
        self.destroy()


class StudioApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.add_main_option("version", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Show product version", None)
        self.connect("handle-local-options", self._local_options)

    def _local_options(self, app, options):
        if options.contains("version"):
            print("4DGS Studio " + VERSION)
            return 0
        return -1

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name("4DGS Studio")
        Gtk.Window.set_default_icon_name(APPLICATION_ID)
        appearance = read_preferences().get("appearance", 0)
        Adw.StyleManager.get_default().set_color_scheme([Adw.ColorScheme.DEFAULT, Adw.ColorScheme.FORCE_LIGHT, Adw.ColorScheme.FORCE_DARK][appearance if appearance in (0,1,2) else 0])
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: [window.close() for window in list(self.get_windows()) if isinstance(window, StudioWindow)])
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def do_activate(self):
        window = self.get_active_window()
        if not window:
            window = StudioWindow(self)
        window.present()

    def do_open(self, files, n_files, hint):
        for file in files:
            path = file.get_path()
            if path is None:
                continue
            window = StudioWindow(self)
            window.present()
            window.open_path(path)


def main():
    return StudioApplication().run(sys.argv)
