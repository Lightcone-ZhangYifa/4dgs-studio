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

from . import APPLICATION_ID, PRODUCT_IDENTITY, VERSION
from .i18n import tr, read_preferences, save_preferences
from .jobs import LatestJob, CommandJobs
from .model import SceneDocument, Trajectory, TrajectoryLimitError
from .camera import CameraTrack, CameraHistory, export_camera_animation, import_camera_animation
from .timeline import Timeline
from .viewport import Viewport
from .image_view import ComparisonCanvas
from .inspector import Inspector
from .outliner import SceneOutliner
from .comparison import compare_view, render_view
from .capture_catalog import build_capture_catalog


PREVIEW_ONLY = object()

def box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, **kwargs):
    return Gtk.Box(orientation=orientation, spacing=spacing, **kwargs)


def label(text, **kwargs):
    return Gtk.Label(label=text, **kwargs)


def accessible(widget, name):
    widget.update_property([Gtk.AccessibleProperty.LABEL], [name])
    widget.set_tooltip_text(name)
    return widget


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
        self._preview_rate = 1.
        self._preview_range = None
        self.visible = set()
        self.selected = set()
        self._outline_extend = False
        self._outline_primary = None
        self.primary = None
        self.primary_member = None
        self._member_entries = []
        self.frame = None
        self.history = CameraHistory()
        self.editing_id = None
        self.camera_entries = []
        self._viewed_entry = None
        self.render_entries = []
        self._supplemental_clocks = {}
        self._supplemental_spaces = {}
        self._authored_timeline = {}
        self.capture_catalog = {}
        self.capture_entries = []
        self._calibrations = {}
        self._inspection_calibrations = {}
        self._inspected_elements = {}
        self._editor_signature = None
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
        self._stats_source = 0
        self._pending_stats = None
        self._snapshot_request = None
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
        for key, fallback, items in [
            ("menu.file", "File", [("file.open", "Open Scene…", "open"),
                ("file.screenshot", "Save Screenshot…", "screenshot"),
                ("file.workspaceSnapshot", "Save Workspace Snapshot…", "workspace-snapshot"),
                ("menu.closeWindow", "Close Window", "close")]),
            ("menu.view", "View", [("view.frameScene", "Frame Scene", "home"),
                ("view.focusSelection", "Focus Selection", "frame"),
                ("view.top", "Top", "top"), ("view.front", "Front", "front"),
                ("view.right", "Right", "right"), ("view.isometric", "Isometric", "isometric"),
                ("view.previous", "Previous View", "previous-view"),
                ("view.next", "Next View", "next-view"),
                ("view.toggleOutliner", "Toggle Scene Sidebar", "toggle-outliner"),
                ("view.toggleInspector", "Toggle Inspector", "toggle-inspector"),
                ("view.fullscreen", "Enter Full Screen", "fullscreen")]),
            ("menu.camera", "Camera", [("camera.createFromView", "Create Camera from View", "create-camera"),
                ("camera.recordAtTime", "Record Keyframe at Current Time", "record"),
                ("camera.saveAnimation", "Save Camera Animation…", "save"),
                ("camera.importAnimation", "Import Camera Animation…", "import-camera"),
                ("camera.undo", "Undo Camera Edit", "camera-undo"),
                ("camera.redo", "Redo Camera Edit", "camera-redo")]),
            ("menu.playback", "Playback", [("timeline.playPause", "Play / Pause", "play"),
                ("timeline.firstFrame", "First Frame", "first"),
                ("timeline.previousFrame", "Previous Frame", "previous"),
                ("timeline.nextFrame", "Next Frame", "next"),
                ("timeline.lastFrame", "Last Frame", "last")]),
        ]:
            submenu = Gio.Menu()
            for item_key, item_fallback, action in items:
                submenu.append(tr(item_key, item_fallback), "win."+action)
            menu.append_submenu(tr(key, fallback), submenu)
        for title, action in [
            (tr("common.preferences", "Preferences"), "preferences"),
            (tr("nav.help", "Navigation guide"), "help"),
            (tr("ubuntu.graphicsInformation", "Graphics Information"), "graphics-information"),
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
        self.outliner = SceneOutliner()
        self.outliner.set_vexpand(True)
        self.outliner.on_selection = self._object_selection
        self.outliner.on_activate = self._object_activated
        self.outliner.on_visibility = self._visibility_changed
        left.append(self.outliner)
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
        self.view_bar = view_bar
        self.camera_drop = Gtk.DropDown.new_from_strings([tr("view.free", "Free perspective")])
        accessible(self.camera_drop, tr("camera.view", "Camera"))
        compact_dropdown(self.camera_drop, 30)
        self.camera_drop.set_hexpand(True)
        self.camera_drop.connect("notify::selected", self._camera_changed)
        view_bar.append(self.camera_drop)
        tools = Gtk.DropDown.new_from_strings([tr("nav.select", "Select"), tr("nav.orbit", "Orbit"), tr("nav.pan", "Pan"), tr("nav.dolly", "Dolly")])
        accessible(tools, tr("nav.title", "Navigation"))
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
        self.viewport.on_stats = self._queue_render_stats
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
        self.preview_controls = []
        for key, text, attribute in [("display.grid", "Grid", "show_grid"), ("category.cameras", "Cameras", "show_cameras"), ("preview.paths", "Paths", "show_paths"), ("preview.trails", "Trails", "show_trails")]:
            toggle = Gtk.ToggleButton(label=tr(key, text), active=True)
            toggle.connect("toggled", self._toggle_preview, attribute)
            preview.append(toggle)
            self.preview_controls.append(toggle)
        preview.append(label(tr("display.quality", "Resolution"), margin_start=8))
        resolution = Gtk.DropDown.new_from_strings(["100%", "75%", "50%"])
        accessible(resolution, tr("display.quality", "Resolution"))
        resolution.connect("notify::selected", lambda w, _: self._set_render_scale([1, .75, .5][w.get_selected()]))
        preview.append(resolution)
        self.preview_controls.append(resolution)
        self.compare_toggle = Gtk.ToggleButton(label=tr("compare.button", "Render / GT"))
        self.compare_toggle.connect("toggled", self._comparison_toggled)
        preview.append(self.compare_toggle)
        center.append(preview)
        compare_bar = box(spacing=6, margin_start=8, margin_end=8, margin_bottom=6)
        self.compare_bar = compare_bar
        compare_bar.set_visible(False)
        self.render_drop = Gtk.DropDown.new_from_strings([tr("render.noViews", "No render views")])
        accessible(self.render_drop, tr("render.viewSelector", "Render view and source profile"))
        compact_dropdown(self.render_drop, 18)
        self.render_drop.set_hexpand(True)
        self.render_drop.connect("notify::selected", lambda *_: self._render_selected())
        compare_bar.append(self.render_drop)
        self.observation_drop = Gtk.DropDown.new_from_strings([tr("ubuntu.autoObservation", "Auto observation")])
        accessible(self.observation_drop, tr("compare.observation", "Camera observation"))
        compact_dropdown(self.observation_drop, 14)
        self.observation_entries = [None]
        self.observation_drop.connect("notify::selected", lambda *_: self._observation_selected())
        compare_bar.append(self.observation_drop)
        self.capture_drop = Gtk.DropDown.new_from_strings([tr("timeline.source")])
        accessible(self.capture_drop, tr("ubuntu.cameraCapture", "Camera capture"))
        compact_dropdown(self.capture_drop, 20)
        self.capture_drop.connect("notify::selected", lambda *_: self._capture_selected())
        compare_bar.append(self.capture_drop)
        compare_bar.append(button(tr("compare.fit", "Fit images"), lambda: self.image_view.reset(), "zoom-fit-best-symbolic"))
        compare_bar.append(button(tr("compare.actualPixels", "Actual pixels"), lambda: self.image_view.actual_pixels(), "zoom-original-symbolic"))
        compare_bar.append(button(tr("compare.refresh", "Refresh comparison"), self._request_comparison, "view-refresh-symbolic"))
        compare_bar.append(button(tr("compare.close", "Return to 3D"), lambda: self.compare_toggle.set_active(False)))
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
        accessible(self.time_entry, tr("timeline.time", "Time"))
        self.time_entry.connect("activate", lambda *_: self._guard(self._author_time_changed))
        row.append(self.time_entry)
        self.loop = Gtk.CheckButton(label=tr("timeline.loop", "Loop"), active=True)
        row.append(self.loop)
        self.snap = Gtk.CheckButton(label=tr("timeline.snap", "Snap"), active=True)
        self.snap.connect("toggled", lambda w: setattr(self.timeline, "snap", w.get_active()))
        row.append(self.snap)
        speed = Gtk.DropDown.new_from_strings(["0.25×", "0.5×", "1×", "2×", "4×"])
        accessible(speed, tr("timeline.speed", "Playback speed"))
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
        accessible(self.range_start, tr("timeline.in", "In time"))
        accessible(self.range_end, tr("timeline.out", "Out time"))
        self.range_start.connect("activate", lambda *_: self._preview_range_changed())
        self.range_end.connect("activate", lambda *_: self._preview_range_changed())
        row2.append(self.range_start)
        row2.append(self.range_end)
        row2.append(label(tr("timeline.previewLabel", "Preview")+" /s"))
        self.rate = Gtk.Entry(text="1", width_chars=7)
        accessible(self.rate, tr("timeline.previewLabel", "Preview")+" /s")
        self.rate.connect("activate", lambda *_: self._preview_rate_changed())
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
        self.member_drop = compact_dropdown(Gtk.DropDown.new_from_strings([]), 28)
        self.member_drop.set_tooltip_text(tr("inspector.selection"))
        self.member_drop.connect("notify::selected", self._member_selected)
        self.member_drop.set_visible(False)
        panel.append(self.member_drop)
        find = box(spacing=4, margin_start=8, margin_end=8)
        self.id_entry = Gtk.Entry(placeholder_text=tr("selection.exactID", "Exact Gaussian ID"), hexpand=True, width_chars=12)
        accessible(self.id_entry, tr("selection.exactID", "Exact Gaussian ID"))
        self.id_entry.connect("activate", lambda *_: self._find_id())
        find.append(self.id_entry)
        find.append(button(tr("selection.find", "Find ID"), self._find_id, "edit-find-symbolic"))
        panel.append(find)
        self.tabs = Gtk.Notebook(vexpand=True)
        self.detail_view = Inspector()
        selection_box = box(Gtk.Orientation.VERTICAL, 8)
        selection_box.append(self.detail_view)
        selection_actions = box(Gtk.Orientation.VERTICAL, spacing=4, margin_start=10, margin_end=10)
        selection_actions.append(button(tr("view.focus", "Frame selection"), self._frame_selection))
        selection_actions.append(button(tr("selection.copy", "Copy attributes"), self._copy_attributes))
        selection_box.append(selection_actions)
        self.inspection_view = accessible(Gtk.DropDown.new_from_strings([]), tr("camera.rig", "Camera rig"))
        self.inspection_view.connect("notify::selected", self._inspection_view_changed)
        self.inspection_view.set_visible(False)
        selection_box.append(self.inspection_view)
        self.look_through = button(tr("camera.lookThrough", "Look through this camera"), self._look_through)
        selection_box.append(self.look_through)
        self.look_through.set_visible(False)
        self.compare_observation = button(tr("compare.open"), self._compare_selected_observation)
        self.preview_render = button(tr("render.previewAction"), self._preview_selected_render)
        self.compare_observation.set_visible(False)
        self.preview_render.set_visible(False)
        selection_box.append(self.compare_observation)
        selection_box.append(self.preview_render)
        self.create_from_selection = button(tr("camera.create", "Create camera"), self.create_camera)
        selection_box.append(self.create_from_selection)
        camera_box = box(Gtk.Orientation.VERTICAL, 8, margin_start=10, margin_end=10, margin_top=10, margin_bottom=10)
        self.camera_editor = camera_box
        self.camera_editor.set_visible(False)
        camera_box.append(button(tr("camera.create", "Create Camera from View"), self.create_camera))
        camera_box.append(button(tr("camera.record", "Record view at playhead"), self.record_key))
        self.track_name = self._field(camera_box, tr("camera.name", "Camera name"), lambda: self._edit_track("name"))
        camera_box.append(label(tr("camera.keys", "Camera keyframes"), xalign=0))
        self.key_drop = accessible(Gtk.DropDown.new_from_strings([]), tr("camera.keys", "Camera keyframes"))
        self.key_drop.connect("notify::selected", self._key_chosen)
        camera_box.append(self.key_drop)
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
        selection_box.append(camera_box)
        # Authored controls are the primary task in this context. Keep them
        # ahead of expanded calibration/provenance details in the same page.
        selection_box.reorder_child_after(camera_box, None)
        self.tabs.append_page(scroller(selection_box), label(tr("inspector.selection", "Selection")))
        view = box(Gtk.Orientation.VERTICAL, 10, margin_start=10, margin_end=10, margin_top=10)
        self.viewport_controls = view
        view.append(label(tr("nav.title", "Navigation"), xalign=0))
        mode = Gtk.DropDown.new_from_strings([tr("navigation.turntable", "Turntable"), tr("navigation.trackball", "Trackball")])
        accessible(mode, tr("navigation.orbitStyle", "Orbit style"))
        mode.connect("notify::selected", lambda w, _: self._set_view_property("navigation_mode", ["turntable", "trackball"][w.get_selected()]))
        view.append(mode)
        view.append(button(tr("navigation.pivotSelection", "Use selection center"), self._pivot_selection))
        view.append(button(tr("navigation.pivotView", "Use visible scene depth"), lambda: self.viewport.center_on_depth()))
        pivot_toggle = Gtk.CheckButton(label=tr("navigation.showPivot", "Show orbit center"), active=True)
        pivot_toggle.connect("toggled", lambda w: self._set_view_property("show_pivot", w.get_active()))
        view.append(pivot_toggle)
        background = Gtk.DropDown.new_from_strings([tr("background.studio", "Studio"), tr("background.black", "Black"), tr("background.gray", "Gray"), tr("background.white", "White")])
        accessible(background, tr("preview.background", "Preview background"))
        background.connect("notify::selected", lambda w, _: self._set_view_property("background", ["studio", "black", "gray", "white"][w.get_selected()]))
        view.append(label(tr("preview.background", "Preview background"), xalign=0))
        view.append(background)
        axes = Gtk.DropDown.new_from_strings(["Y up", "Z up", "X up", "−Y up", "−Z up", "−X up"])
        accessible(axes, tr("nav.upAxis"))
        axes.connect("notify::selected", lambda w, _: self._set_view_property("up_axis", ["y", "z", "x", "-y", "-z", "-x"][w.get_selected()]))
        view.append(axes)
        for text, preset in [("Front", "front"), ("Right", "right"), ("Top", "top"), ("Isometric", "isometric")]:
            view.append(button(tr("view."+preset,text), lambda p=preset: self._preset(p)))
        for key, text, prop, value, low, high, step in [
            ("preview.pathDensity", "Path density", "path_density", 100, 1, 100, 1),
            ("preview.pathOpacity", "Path opacity", "path_opacity", 18, 0, 100, 1),
            ("preview.trailFrames", "Trail intervals", "trail_intervals", 8, 1, 4096, 1),
            ("display.size", "Gaussian size", "gaussian_scale", 1, .1, 4, .05),
            ("display.opacity", "Opacity multiplier", "opacity_multiplier", 1, .05, 2, .05),
            ("display.exposure", "Exposure multiplier", "exposure_multiplier", 1, .1, 4, .05),
            ("display.nearClip", "Near clip in scene units", "near_clip", .01, .0001, 100, .001),
            ("navigation.sensitivity", "Navigation sensitivity", "sensitivity", 1, .1, 4, .1),
        ]:
            view.append(label(tr(key, text)+(" (%)" if prop in ("path_density", "path_opacity") else ""), xalign=0))
            spin = Gtk.SpinButton.new_with_range(low, high, step)
            accessible(spin, tr(key, text))
            spin.set_value(value)
            spin.set_digits(0 if step == 1 else 6 if prop == "near_clip" else 2)
            spin.connect("value-changed", self._view_option, prop)
            if prop == 'near_clip':
                self.near_clip_control = spin
            view.append(spin)
        self.tabs.append_page(scroller(view), label(tr("inspector.viewport", "Viewport")))
        self.document_view = Inspector()
        self.tabs.append_page(scroller(self.document_view), label(tr("inspector.document", "Document")))
        self.tabs.connect("switch-page", lambda *_: self._inspect())
        panel.append(self.tabs)
        return panel

    def _field(self, parent, text, callback):
        parent.append(label(text, xalign=0))
        entry = Gtk.Entry(hexpand=True, width_chars=1)
        accessible(entry, text)
        entry.connect("activate", lambda *_: self._guard(callback))
        entry.set_tooltip_text(text+" · Enter")
        row = box(spacing=4)
        row.append(entry)
        apply = button(tr("common.apply", "Apply"), lambda: self._guard(callback))
        accessible(apply, text+" · "+tr("common.apply", "Apply"))
        row.append(apply)
        parent.append(row)
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
            "graphics-information": self.graphics_information,
            "play": self.toggle_playback, "frame": self._frame_selection, "home": self._home,
            "first": lambda: self._step(-1000000000), "previous": lambda: self._step(-1),
            "next": lambda: self._step(1), "last": lambda: self._step(1000000000),
            "top": lambda: self._preset("top"), "front": lambda: self._preset("front"),
            "right": lambda: self._preset("right"), "isometric": lambda: self._preset("isometric"),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda a, p, fn=callback: self._guard(fn))
            self.add_action(action)
        for name, keys in {"open": ["<Primary>o"], "save": ["<Primary>s"], "screenshot": ["<Primary><Shift>s"], "workspace-snapshot": ["<Primary><Alt><Shift>s"], "create-camera": ["<Primary><Shift>k"], "toggle-outliner": ["<Primary>1"], "toggle-inspector": ["<Primary>2"], "fullscreen": ["F11"], "record": ["<Primary>k"], "undo": ["<Primary>z"], "redo": ["<Primary><Shift>z"], "camera-undo": ["<Primary><Alt>z"], "camera-redo": ["<Primary><Alt><Shift>z"], "close": ["<Primary>w"], "previous-view": ["<Primary><Alt>bracketleft"], "next-view": ["<Primary><Alt>bracketright"], "frame": ["<Primary>f"], "home": ["<Primary>r"], "help": ["<Primary>question"]}.items():
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
                            # Supplemental domains belong to this authoring
                            # session; importing a different unit under the same
                            # clock name must not reinterpret existing cameras.
                            new_clocks, new_spaces = {}, {}
                            for track in tracks:
                                for table, supplied, inherited, target in (
                                    ("clocks", track.clocks, self._supplemental_clocks, new_clocks),
                                    ("spaces", track.spaces, self._supplemental_spaces, new_spaces)):
                                    for name, descriptor in supplied.items():
                                        existing = self.document.manifest.get(table, {}).get(name, inherited.get(name, target.get(name)))
                                        if existing is not None and existing != descriptor:
                                            self.error(tr("camera.importDomainMismatch"))
                                            return
                                        target[name] = descriptor
                            self.history.remember()
                            self._supplemental_clocks.update(new_clocks)
                            self._supplemental_spaces.update(new_spaces)
                            self.history.tracks.extend(tracks)
                            self.visible.update(t.id for t in tracks)
                            self.editing_id = tracks[0].id if tracks else self.editing_id
                            if tracks:
                                self.primary = tracks[0].id
                                self.selected = {(self.primary, None)}
                            self._camera_edited()
                    existing_ids = [t.id for t in self.history.tracks]
                    self.file_jobs.submit(lambda: import_camera_animation(path, source, existing_ids=existing_ids,
                        cancelled=lambda: self.closed or source is not self.document or chooser_generation != self._chooser_generation), imported)
            except GLib.Error as exc:
                if not exc.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                    self.error(exc)
            except Exception as exc:
                self.error(exc)
        if operation == "save":
            dialog.save(self, None, done)
        else:
            dialog.open(self, None, done)

    def _confirm_discard(self, callback, *, closing=False):
        if not self.dirty:
            callback()
            return
        if self._pending_confirm:
            return
        self._pending_confirm = True
        dialog = Adw.MessageDialog(transient_for=self, heading=tr("camera.closeUnsaved" if closing else "camera.openUnsaved"), body=tr("camera.closeUnsavedHint" if closing else "camera.openUnsavedHint"))
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
        self._inspection_calibrations = {}
        self._inspected_elements = {}
        self._inspected_views = {}
        self.image_view.clear()
        self.image_view.reset()
        self._comparison_result = None
        self._chooser_generation += 1
        if self._snapshot_request:
            self._snapshot_request.cancel()
            self._snapshot_request = None
        self._file_busy = False
        old, self.document = self.document, document
        self.frame = None
        self.viewport.set_frame(None)
        self.viewport.clear_navigation_history()
        if old is not None:
            self.frames.executor.submit(old.close)
        self.history = CameraHistory()
        self._supplemental_clocks = {}
        self._supplemental_spaces = {}
        self._authored_timeline = {}
        self.editing_id = None
        self._viewed_entry = None
        self.dirty = False
        self.selected.clear()
        self.primary = None
        self.primary_member = None
        self._member_entries = []
        self.search.set_text("")
        self._refresh_members()
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
        self._preview_rate = float(self.rate.get_text())
        self._preview_range = None
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
        self._comparison_controls(False)
        self._updating = False
        self.stack.set_visible_child_name("scene")
        self.domain_label.set_text(tr("ubuntu.domain").format(space=document.space, clock=document.clock, unit=tr("unit."+self.timeline.unit,self.timeline.unit)))
        self._render_selected()
        self._request_frame(self.time)
        self._inspect()

    def _rebuild_objects(self):
        self._updating = True
        if self.document:
            objects = [*self.document.objects, *[{"id": t.id, "label": t.label, "type": "camera", "encoding": "gsv.camera.pinhole", "authored": True} for t in self.history.tracks]]
            self.outliner.set_objects(objects, visible=self.visible, supported=self.document.supported_ids,
                                      selected=self._outline_members())
        self._updating = False
        self._filter_objects()

    def _is_camera(self, owner):
        return owner in {t.id for t in self.history.tracks} or (self.document is not None and self.document._objects.get(owner, {}).get('type') == 'camera')

    def _outline_members(self):
        def member(owner, identity):
            obj = self.document._objects.get(owner,{}) if self.document else {}
            return owner, identity if len(obj.get('data',{}).get('view_ids',[])) > 1 else None
        return list(dict.fromkeys(member(owner,identity) for owner,identity in self.selected))

    def _filter_objects(self):
        self.outliner.set_query(self.search.get_text())

    def _object_selection(self, owners, primary, view, extend):
        if self._updating:
            return
        previous, self.selected = self.selected, set()
        for owner, member in self.outliner.selected_members:
            retained = {(o,i) for o,i in previous if o == owner} if extend and not self._is_camera(owner) else set()
            self.selected.update(retained or {(owner,member)})
        self.primary = primary
        if view is not None:
            self._inspected_views[primary] = view
        self.primary_member = next((item for item in self.selected if item == (primary,view)),
                                   next((item for item in self.selected if item[0] == primary), None))
        self._selection_changed()

    def _object_activated(self, owner, view=None):
        obj = next((o for o in self.document.objects if o['id'] == owner), None)
        if obj is None:
            obj = {'id': owner, 'authored': True}
        if not obj.get("authored") and (obj.get("space") != self.document.space or (obj.get("clock") and obj.get("clock") != self.document.clock)) and (obj["id"] in self.document.supported_ids or obj["encoding"] in ("gsv.camera.pinhole", "gsv.render.view")):
            self._activate_domain(owner, lambda: self._object_activated(owner, view))
            return
        for i, (identifier, camera_view, authored) in enumerate(self.camera_entries):
            if identifier == owner and camera_view == (view or 0):
                self.camera_drop.set_selected(i+1)
                return
        if owner in self.render_entries:
            self.render_drop.set_selected(self.render_entries.index(owner))
            self.compare_toggle.set_active(True)

    def _visibility_changed(self, identifier, enabled):
        if self._updating:
            return
        if enabled:
            obj = next((o for o in self.document.objects if o["id"] == identifier), None)
            if obj and obj["id"] in self.document.supported_ids and (obj.get("space") != self.document.space or (obj["encoding"] != "gsv.gaussian.static" and obj.get("clock") != self.document.clock)):
                self.outliner.set_object_visibility(self.visible)
                self.status.set_text(tr("ubuntu.domainHint", "Double-click this object to activate its coordinate space and clock."))
                return
            self.visible.add(identifier)
        else:
            self.visible.discard(identifier)
        self.outliner.set_object_visibility(self.visible)
        self._request_frame(self.target_time)

    def _refresh_members(self):
        self._member_entries = sorted(self.selected, key=lambda pair: (pair[0], pair[1] is not None, pair[1] or 0))
        if self.primary_member not in self.selected or (self.primary_member and self.primary_member[0] != self.primary):
            self.primary_member = next((pair for pair in self._member_entries if pair[0] == self.primary), None)
        labels = []
        for owner, identity in self._member_entries:
            obj = next((o for o in self.document.objects if o['id']==owner), None) if self.document else None
            track = next((t for t in self.history.tracks if t.id==owner), None)
            name = track.label if track else obj.get('label',owner) if obj else owner
            suffix = str(identity)
            if identity is not None and self._is_camera(owner):
                views = obj.get('data',{}).get('view_ids',[]) if obj else []
                suffix = views[identity] if identity < len(views) else str(identity)
            labels.append(name+(" · "+suffix if identity is not None else ""))
        self._updating = True
        self.member_drop.set_model(Gtk.StringList.new(labels))
        self.member_drop.set_selected(self._member_entries.index(self.primary_member) if self.primary_member in self._member_entries else 0)
        self.member_drop.set_visible(len(labels)>1)
        self._updating = False

    def _member_selected(self, widget, param):
        if self._updating or widget.get_selected() >= len(self._member_entries):
            return
        self.primary_member = self._member_entries[widget.get_selected()]
        self.primary = self.primary_member[0]
        if self._is_camera(self.primary) and self.primary_member[1] is not None:
            self._inspected_views[self.primary] = self.primary_member[1]
            self._request_frame(self.time)
        self.editing_id = self.primary if self.primary in {t.id for t in self.history.tracks} else None
        self._inspect()
        self._refresh_camera_editor(force=True)

    def _selection_changed(self):
        if self.primary_member not in self.selected or (self.primary_member and self.primary_member[0] != self.primary):
            ordered = sorted(self.selected, key=lambda pair: (pair[0], pair[1] is not None, pair[1] or 0))
            self.primary_member = next((pair for pair in ordered if pair[0] == self.primary), ordered[0] if ordered else None)
        self.primary = self.primary_member[0] if self.primary_member else None
        editing = self.primary if self.primary in {t.id for t in self.history.tracks} else None
        if editing != self.editing_id:
            self.timeline.selected_key = None
        self.editing_id = editing
        self.tabs.set_current_page(0)
        self.outliner.select_members(self._outline_members(), primary=(self.primary, self._inspected_views.get(self.primary)) if self._is_camera(self.primary) else (self.primary, None))
        self._refresh_members()
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
            self.primary_member = item
            self.id_entry.set_text(str(identity))
        else:
            self.primary = None
        self.outliner.select_members(self._outline_members())
        self._selection_changed()

    def _find_id(self):
        def find():
            identity = int(self.id_entry.get_text().strip())
            if not -(1 << 63) <= identity < 1 << 63:
                raise ValueError(tr("selection.invalidID", "Enter an exact signed 64-bit integer ID."))
            if self.frame is None:
                return
            if self.primary:
                doc, owner, displayed_time = self.document, self.primary, self.time
                future = self.frames.executor.submit(doc.element, owner, identity, displayed_time)
                def finished(future):
                    try:
                        element, error = future.result(), None
                    except Exception as exc:
                        element, error = None, exc
                    def publish():
                        if self.closed or doc is not self.document or owner != self.primary or displayed_time != self.time:
                            return False
                        if error:
                            self.error(error)
                        elif element is None:
                            self.error(tr("ubuntu.idAbsent"))
                        else:
                            self._pick(owner, identity)
                        return False
                    GLib.idle_add(publish)
                future.add_done_callback(finished)
                return
            indices = np.flatnonzero(self.frame.ids == identity)
            if not len(indices):
                raise ValueError(tr("ubuntu.idAbsent", "Identity is not present at the displayed time."))
            if len(indices) > 1:
                raise ValueError(tr("selection.ambiguousID", "Identity %@ is absent or ambiguous. Select its source object in the tree.").replace("%@",str(identity)))
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
        primary, inspected_views = self.primary, dict(self._inspected_views)
        signature = (frozenset(selected), frozenset(visible), float(self.viewport.path_density), self._edit_revision, doc.path)
        rebuild = signature != self._path_signature
        density = self.viewport.path_density
        paths_revision = self._paths_revision
        frame_generation = self.frames.generation
        authored = copy.deepcopy([t for t in self.history.tracks if any(owner == t.id for owner,_ in selected)]) if rebuild else []
        def evaluate():
            frame = doc.prepare_frame(requested_time, object_ids=[id for id in dict.fromkeys([*doc.visible_ids, *doc.supported_ids]) if id in visible], cancelled=lambda: self.closed or doc is not self.document or frame_generation != self.frames.generation)
            paths = None
            path_error = None
            if rebuild:
                paths = []
                for identifier in {o for o, i in selected}:
                    obj = next((o for o in doc.objects if o["id"] == identifier), None)
                    if obj and identifier in visible and obj.get("space") == doc.space and (obj.get("clock") == doc.clock or obj["encoding"] == "gsv.gaussian.static") and obj["type"] in ("gaussian", "scaffold", "camera"):
                        ids = None if (identifier, None) in selected else [i for o, i in selected if o == identifier and i is not None]
                        try:
                            if obj['type'] == 'camera':
                                views = range(len(obj['data']['view_ids'])) if ids is None else ids
                                for view in views:
                                    paths.extend(doc.trajectories(identifier, view=view, cancelled=lambda: self.closed or doc is not self.document or paths_revision != self._paths_revision))
                            else:
                                paths.extend(doc.trajectories(identifier, ids=ids, density=density if ids is None else 0, cancelled=lambda: self.closed or doc is not self.document or paths_revision != self._paths_revision))
                        except TrajectoryLimitError as error:
                            path_error = str(error)
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
            inspection_calibrations = dict(calibrations)
            source_camera = next((camera for camera in doc.cameras if camera['id'] == primary), None)
            if source_camera is not None:
                camera_time = requested_time if source_camera.get('clock') == doc.clock else float(doc.scene.array(source_camera['data']['times'])[0])
                view = inspected_views.get(primary, 0)
                inspection_calibrations[(primary, view)] = doc.camera(primary, camera_time, view)
            elements = {}
            for owner, identity in selected:
                if identity is not None and doc._objects.get(owner,{}).get('type') != 'camera':
                    elements[(owner, identity)] = doc.element(owner, identity, requested_time)
            return frame, paths, requested_time, calibrations, path_error, inspection_calibrations, elements
        def display(value, error):
            if doc is not self.document:
                return
            if isinstance(error, InterruptedError):
                return
            if error:
                self._set_playing(False)
                self.status.set_text(str(error))
                return
            frame, paths, actual_time, calibrations, path_error, inspection_calibrations, elements = value
            self._calibrations = calibrations
            self._inspection_calibrations = inspection_calibrations
            self._inspected_elements = elements
            self.frame, self.time = frame, actual_time
            self.viewport.set_frame(frame)
            if paths is not None:
                self.viewport.set_paths(paths)
                self._path_signature = signature
            self.viewport.time = actual_time
            self.viewport.source_times = doc.times
            if self._frame_scene:
                self.viewport.frame_scene()
                self.viewport.clear_navigation_history()
                self._frame_scene = False
            self._update_camera_pose()
            self._refresh_camera_editor()
            self.timeline.time = actual_time
            self.timeline.queue_draw()
            if not self._entry_editing(self.time_entry):
                self.time_entry.set_text(f"{actual_time:.17g}")
            times = self.timeline.times
            index = int(np.clip(np.searchsorted(times,actual_time,side='right')-1,0,len(times)-1))
            self.sample_label.set_text(f"{index+1} / {len(times)}")
            warnings = [*getattr(frame, "warnings", []), *([path_error] if path_error else [])]
            self.status.set_text(tr("ubuntu.frame").format(count=f"{getattr(frame,'valid_count',len(frame.ids)):,}",time=f"{actual_time:.9g}") + (" · "+"; ".join(warnings) if warnings else ""))
            self._inspect()
            if self.compare_toggle.get_active():
                self._request_comparison()
        self.frames.submit(evaluate, display)

    def _scrub(self, value):
        self._set_playing(False)
        self._guard(lambda: self._request_frame(value))

    def _author_time_changed(self):
        value = float(self.time_entry.get_text())
        if not math.isfinite(value):
            raise ValueError(tr("timeline.invalidTime", "Enter a finite document time."))
        if not self.document:
            return
        self._set_playing(False)
        self._authored_timeline.setdefault(self.document.clock,set()).add(value)
        self._update_timeline()
        self._request_frame(value)

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
        self._set_playing(False)
        self._preview_range = None
        self.timeline.preview_range = None
        if self.document and self.range_enabled.get_active():
            try:
                lower, upper = float(self.range_start.get_text()), float(self.range_end.get_text())
                if not self.timeline.times[0] <= lower < upper <= self.timeline.times[-1]:
                    raise ValueError(tr("timeline.invalidRange"))
                self._preview_range = self.timeline.preview_range = (lower, upper)
            except ValueError as error:
                self.status.set_text(str(error))
                self.range_enabled.set_active(False)
        self.timeline.queue_draw()

    def _preview_rate_changed(self):
        try:
            rate = float(self.rate.get_text())
            if not math.isfinite(rate) or not 0 < rate <= 10000:
                raise ValueError(tr("timeline.invalidRate"))
            self._preview_rate = rate
            self._set_playing(False)
        except ValueError:
            self.status.set_text(tr("timeline.invalidRate"))

    def _play_bounds(self):
        lower, upper = map(float, (self.timeline.times[0], self.timeline.times[-1]))
        if self._preview_range is not None:
            lower, upper = self._preview_range
        return lower, upper, self._preview_rate

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
        old = self._viewed_entry
        self._updating = True
        self.camera_entries = entries
        self.camera_drop.set_model(Gtk.StringList.new(names))
        self.camera_drop.set_selected(entries.index(old)+1 if old in entries else 0)
        if old not in entries:
            self._viewed_entry = None
        self._updating = False

    def _track(self):
        return next((t for t in self.history.tracks if t.id == self.editing_id), None)

    def _camera_changed(self, widget, _param):
        if self._updating or not self.document:
            return
        index = widget.get_selected()
        if index > 0:
            identifier, view, authored = self.camera_entries[index-1]
            track = next((t for t in self.history.tracks if t.id == identifier), None) if authored else None
            compatible = (track.space == self.document.space and (track.clock == self.document.clock or len(track.keyframes) == 1)) if track else (identifier,view) in self._calibrations
            if not compatible:
                self._updating = True
                self.camera_drop.set_selected(self.camera_entries.index(self._viewed_entry)+1 if self._viewed_entry in self.camera_entries else 0)
                self._updating = False
                self.status.set_text(tr('camera.viewportDomainMismatch'))
                return
            calibration = track.at(self.time) if track else self._calibrations[(identifier,view)]
            self._viewed_entry = self.camera_entries[index-1]
            self.viewport.set_camera(calibration)
            self.viewport.center_on_depth()
            self._adopt_camera_profile(identifier,view)
        else:
            self._viewed_entry = None
            self.viewport.set_camera(None)

    def _adopt_camera_profile(self, owner, view):
        profiles = {item['links']['profile'] for item in self.document.render_views
                    if item['links']['camera'] == owner and item['data']['camera_view'] == view}
        if len(profiles) == 1:
            profile = self.document.object(next(iter(profiles)))
            near = profile.get('data',{}).get('visibility',{}).get('center_depth',{}).get('near')
            if near is None or not math.isfinite(float(near)) or float(near) < 0:
                return
            near = float(near)
            self.viewport.near_clip = max(near, float(np.finfo(np.float32).tiny))
            self._updating = True
            self.near_clip_control.set_range(min(.0001,near),max(100,near))
            self.near_clip_control.set_value(near)
            self._updating = False
            self.viewport.queue_render()

    def _update_camera_pose(self):
        if not self.document:
            return
        wireframes = []
        for identifier, view, authored in self.camera_entries:
            try:
                if authored:
                    track = next(t for t in self.history.tracks if t.id == identifier)
                    if track.space != (self.document.space or "world") or (len(track.keyframes) > 1 and track.clock != (self.document.clock or "timeline")):
                        continue
                    calibration = track.at(self.time)
                else:
                    obj = self.document.object(identifier)
                    if (identifier, view) not in self._calibrations:
                        continue
                    calibration = self._calibrations[(identifier, view)]
                wireframes.append({"calibration": calibration, "object_id": identifier, "view": view,
                                   "visible": identifier in self.visible,
                                   "selected": (identifier,None) in self.selected or (identifier,view) in self.selected})
            except Exception:
                continue
        self.viewport.set_camera_wireframes(wireframes)
        index = self.camera_drop.get_selected()
        if 0 < index <= len(self.camera_entries):
            identifier, view, authored = self.camera_entries[index-1]
            try:
                if authored:
                    track = next(t for t in self.history.tracks if t.id == identifier)
                    if track.space != (self.document.space or "world") or (len(track.keyframes) > 1 and track.clock != (self.document.clock or "timeline")):
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

    def _camera_pick(self, identifier, view, extend=False):
        self._inspected_views[identifier] = view
        for i, (id, v, authored) in enumerate(self.camera_entries):
            if (id, v) == (identifier, view):
                self.primary = id
                item = (id, None if authored else view)
                if extend:
                    self.selected.symmetric_difference_update({item})
                else:
                    self.selected = {item}
                self.primary_member = item
                self.editing_id = id if authored else None
                self._rebuild_objects()
                self._selection_changed()
                return

    def _inspection_view_changed(self, widget, _param):
        if not self._updating and self.primary is not None:
            view = widget.get_selected()
            self._inspected_views[self.primary] = view
            self.selected.discard(self.primary_member)
            self.primary_member = (self.primary,view)
            self.selected.add(self.primary_member)
            self._selection_changed()

    def _free_camera(self):
        self._viewed_entry = None
        self._updating = True
        self.camera_drop.set_selected(0)
        self._updating = False

    def create_camera(self):
        def create():
            if self.document is None:
                raise ValueError(tr("camera.openFirst", "Open a GSV document first."))
            k, pose, width, height = self.viewport.get_camera()
            track = CameraTrack(tr("ubuntu.newCamera").format(number=len(self.history.tracks)+1), self.document.space or "world", self.document.clock or "timeline", width, height)
            clock = self.document.manifest.get("clocks", {}).get(track.clock,
                    self._supplemental_clocks.get(track.clock, {"unit": self.timeline.unit}))
            space = self.document.manifest.get("spaces", {}).get(track.space,
                    self._supplemental_spaces.get(track.space, {"unit": "unitless", "handedness": "right", "axes": ["x", "y", "z"]}))
            self._supplemental_clocks.setdefault(track.clock, copy.deepcopy(clock))
            self._supplemental_spaces.setdefault(track.space, copy.deepcopy(space))
            track.clocks = copy.deepcopy(self._supplemental_clocks)
            track.spaces = copy.deepcopy(self._supplemental_spaces)
            track.record(self.time, k, pose, near=self.viewport.near_clip)
            self.history.remember()
            self.history.tracks.append(track)
            self.visible.add(track.id)
            self.editing_id = self.primary = track.id
            self.selected = {(track.id, None)}
            self._camera_edited()
            self.tabs.set_current_page(0)
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
            edited.record(self.time, k, pose, width, height, near=self.viewport.near_clip)
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
        self._refresh_members()
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
        extra.extend(self._authored_timeline.get(self.document.clock,()))
        self.timeline.times = np.unique(np.concatenate([self.document.times, extra]))
        self.title_widget.set_subtitle(tr("ubuntu.counts").format(
            objects=len(self.document.objects)+len(self.history.tracks),samples=len(self.timeline.times)))
        descriptor = self.document.manifest.get("clocks", {}).get(self.document.clock,
                      self._supplemental_clocks.get(self.document.clock, {}))
        unit = descriptor.get("unit", self.document.clock_unit)
        if unit != self.timeline.unit:
            self.timeline.unit = unit
            self.rate.set_text("30" if unit == "frame" else "1")
            self._preview_rate = float(self.rate.get_text())
        self.domain_label.set_text(tr("ubuntu.domain").format(space=self.document.space, clock=self.document.clock, unit=tr("unit."+unit,unit)))
        self.timeline.queue_draw()

    def _refresh_camera_editor(self, force=False):
        track = self._track()
        compatible = track is not None and (track.space,track.clock) == (self.document.space,self.document.clock)
        signature = (id(track),id(track.keyframes),compatible) if track else (None,None,False)
        changed_keys = signature != self._editor_signature
        if changed_keys:
            self._editor_signature = signature
            self.timeline.keys = track.times.tolist() if compatible else []
        self.timeline.key_name = track.label if track else ""
        self.camera_editor.set_visible(track is not None and not self.compare_toggle.get_active())
        if changed_keys:
            key_names = [f"{key.time:.17g}" for key in track.keyframes] if track else []
            self._updating = True
            self.key_drop.set_model(Gtk.StringList.new(key_names))
            self._updating = False
        self._updating = True
        self.key_drop.set_selected(self.timeline.selected_key if self.timeline.selected_key is not None else Gtk.INVALID_LIST_POSITION)
        self._updating = False
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
            if (force or not self._entry_editing(entry)) and entry.get_text() != value:
                entry.set_text(value)

    def _key_chosen(self, widget, _param):
        if not self._updating:
            self._select_key(widget.get_selected())

    def _select_key(self, index):
        track = self._track()
        if track and 0 <= index < len(track.keyframes):
            if (track.space,track.clock) != (self.document.space,self.document.clock):
                self.status.set_text(tr("camera.keyDomainMismatch"))
                return
            self.timeline.selected_key = index
            self._scrub(track.keyframes[index].time)
            self._refresh_camera_editor()

    def _retime(self, index, value):
        def edit():
            track = self._track()
            if track is None:
                return
            if (track.space,track.clock) != (self.document.space,self.document.clock):
                raise ValueError(tr("camera.keyDomainMismatch"))
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
                if not math.isfinite(fov) or not 1 <= fov <= 170:
                    raise ValueError(tr("camera.invalidFOV", "Horizontal FOV must be between 1° and 170°."))
                focal = track.width/(2*math.tan(math.radians(fov)/2))
                ratio = focal/k[0,0]
                k[1,1] *= ratio
                k[0,1] *= ratio
                k[0,0] = focal
            else:
                p = np.array([float(s) for s in self.position.get_text().replace(",", " ").split()])
                q = np.array([float(s) for s in self.rotation.get_text().replace(",", " ").split()])
                if p.shape != (3,) or q.shape != (4,) or not np.isfinite(p).all() or not np.isfinite(q).all() or np.linalg.norm(q) < 1e-12:
                    raise ValueError(tr("camera.invalidTransform", "Enter finite XYZ and nonzero WXYZ components."))
                pose[:3,3], pose[:3,:3] = p, quat_matrix(q)
            changed.record(self.time, k, pose, near=self.viewport.near_clip, far=calibration.far)
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
        self.document_view.document(self.document,self.frame,
            object_count=len(self.document.objects)+len(self.history.tracks),sample_count=len(self.timeline.times))
        self.create_from_selection.set_visible(not self.selected and not self.compare_toggle.get_active())
        self.camera_editor.set_visible(self._track() is not None and not self.compare_toggle.get_active())
        self.look_through.set_visible(self.primary is not None and any(e[0] == self.primary for e in self.camera_entries) and not self.compare_toggle.get_active())
        if self.compare_toggle.get_active():
            self.detail_view.comparison(self._comparison_result, message=None if self._comparison_result else tr("ubuntu.rendering"))
            return
        track = next((t for t in self.history.tracks if t.id == self.primary), None)
        calibration = track.at(self.time) if track else self._inspection_calibrations.get((self.primary, self._inspected_views.get(self.primary, 0)))
        source = next((obj for obj in self.document.objects if obj["id"] == self.primary), None)
        views = source["data"].get("view_ids", []) if source and source["encoding"] == "gsv.camera.pinhole" else []
        self.inspection_view.set_visible(len(views) > 1)
        current = self.inspection_view.get_model()
        self._updating = True
        if current is None or [current.get_string(i) for i in range(current.get_n_items())] != views:
            self.inspection_view.set_model(Gtk.StringList.new(views))
        self.inspection_view.set_selected(self._inspected_views.get(self.primary, 0))
        self._updating = False
        self.compare_observation.set_visible(source is not None and source["encoding"] == "gsv.observation.camera-rgb")
        self.preview_render.set_visible(source is not None and source["encoding"] == "gsv.render.view")
        caption = track.label if track else source.get("label", self.primary) if source else self.primary
        if self.primary_member and self.primary_member[1] is not None:
            view_index = self.primary_member[1]
            views = source.get('data',{}).get('view_ids',[]) if source else []
            caption = (caption or "")+" · "+(views[view_index] if self._is_camera(self.primary) and view_index < len(views) else str(view_index))
        self.selection_label.set_text(tr("ubuntu.selected").format(count=len(self.selected)) + (" · "+caption if caption else ""))
        self.selection_label.set_tooltip_text(self.primary or "")
        properties = {(owner, None if self._is_camera(owner) else identity) for owner,identity in self.selected}
        self.detail_view.update(self.document, self.primary, properties, self.frame, calibration, track, self.time, self.primary_member, elements=self._inspected_elements)

    def _render_selected(self):
        if self._updating or not self.document:
            return
        index = self.render_drop.get_selected()
        observations = []
        if 0 <= index < len(self.render_entries):
            observations = self.document.object(self.render_entries[index])["links"].get("observations", [])
        self.observation_entries = [None, PREVIEW_ONLY, *observations]
        self._updating = True
        self.observation_drop.set_model(Gtk.StringList.new([tr("ubuntu.autoObservation"), tr("render.previewAction"), *[self.document.object(id).get("label", id) for id in observations]]))
        self.observation_drop.set_selected(0)
        self._updating = False
        self._rebuild_capture_rows()
        if self.compare_toggle.get_active():
            self._activate_capture()

    def _preview_selected_render(self):
        if self.primary not in self.render_entries:
            return
        self.render_drop.set_selected(self.render_entries.index(self.primary))
        self.observation_drop.set_selected(1)
        self.compare_toggle.set_active(True)

    def _compare_selected_observation(self):
        for view_id, entries in self.capture_catalog.items():
            if any(entry.observation_id == self.primary for entry in entries):
                self.render_drop.set_selected(self.render_entries.index(view_id))
                self.observation_drop.set_selected(self.observation_entries.index(self.primary))
                self.compare_toggle.set_active(True)
                return
        self.error(tr("compare.unmatched"))

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
        entries = self.capture_catalog.get(view_id, [])
        if observation is PREVIEW_ONLY:
            self.capture_entries = [entry for entry in entries if entry.observation_id is None]
        elif observation is None:
            matched = [entry for entry in entries if entry.observation_id is not None]
            self.capture_entries = matched or [entry for entry in entries if entry.observation_id is None]
        else:
            self.capture_entries = [entry for entry in entries if entry.observation_id == observation]
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
                self._update_timeline()
                self.timeline.fit()
                self.target_time = self.time = float(doc.times[0])
                self._path_signature = None
                self._paths_revision += 1
                self._frame_scene = True
                self.viewport.clear_navigation_history()
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
        preview_only = observation_id is PREVIEW_ONLY or not any(entry.observation_id is not None for entry in self.capture_entries)
        if observation_id is PREVIEW_ONLY:
            observation_id = None
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
                cancelled = lambda: self.closed or revision != self._compare_revision
                if preview_only:
                    return render_view(source, view_id, actual_time, cancelled=cancelled)
                return compare_view(source, view_id, actual_time, observation_id=observation_id, cancelled=cancelled)
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
            self.image_view.set_images(result.render, result.reference, result.color_space, reference_role=result.role)
            scores = ""
            if result.metrics:
                scores = " · ".join(f"{key.upper()} {result.metrics[key]:.6g}" if result.metrics.get(key) is not None else f"{key.upper()} —" for key in ["psnr", "ssim", "mse", "mae"])
            self.status.set_text(result.label+" · "+(result.role or "render")+" · CPU reference · "+scores)
            self.status.set_tooltip_text("\n".join(result.details) if not isinstance(result.details, str) else result.details)
        self.comparisons.submit(capture, completed)

    def _comparison_controls(self, active):
        self.compare_bar.set_visible(active)
        for container in (self.view_bar, self.viewport_controls):
            container.set_sensitive(not active)
            # GTK 4.14 exposes each child's own sensitive flag through AT-SPI,
            # even when its parent is disabled. Keep the public controls and
            # their accessible states consistent while inspecting fixed images.
            child = container.get_first_child()
            while child is not None:
                child.set_sensitive(not active)
                child = child.get_next_sibling()
        for control in self.preview_controls:
            control.set_sensitive(not active)

    def _comparison_toggled(self, widget):
        if self._updating:
            return
        self._comparison_controls(widget.get_active())
        self._compare_revision += 1
        self.comparisons.invalidate()
        if widget.get_active():
            self.performance_label.set_text("")
            self._set_playing(False)
            self.stack.set_visible_child_name("comparison")
            self.tabs.set_current_page(0)
            self._rebuild_capture_rows()
            self._activate_capture()
        else:
            self.stack.set_visible_child_name("scene" if self.document else "empty")
            self.image_view.clear()
            self._inspect()

    def _queue_render_stats(self, stats):
        # GLArea::render runs inside GTK snapshotting. Changing labels there
        # invalidates the allocation of the tree currently being snapshotted.
        self._pending_stats = stats
        if not self._stats_source:
            self._stats_source = GLib.idle_add(self._publish_render_stats)

    def _publish_render_stats(self):
        self._stats_source = 0
        if not self.closed and self._pending_stats is not None:
            self._render_stats(self._pending_stats)
        self._pending_stats = None
        return False

    def _render_stats(self, stats):
        self._graphics_stats = stats
        if not self.compare_toggle.get_active() and (not self.playing or time.monotonic() - self._last_stats_update >= .25):
            self._last_stats_update = time.monotonic()
            def number(value):
                return "—" if value is None else f"{value:.2f}"
            gpu = stats.get("gpu_ms")
            visible = stats.get("visible_count")
            performance = tr("ubuntu.completedTiming").format(
                gpu="—" if gpu is None else f"{gpu:.1f} ms", active="—" if visible is None else f"{visible:,}",
                total=f"{stats.get('source_count', 0):,}", scale=round(self.viewport.resolution_scale*100))
            if performance != self.performance_label.get_text():
                self.performance_label.set_text(performance)
            self.performance_label.set_tooltip_text(tr("ubuntu.completedTimingDetail").format(
                render=number(stats.get("gpu_render_ms")), sort=number(stats.get("gpu_sort_ms")),
                fps=number(stats.get("gpu_completed_fps")), memory=number(stats.get("resident_bytes",0)/1024**2),
                cpu=number(stats["cpu_submission_ms"]))+"\n"+self.viewport.graphics_info.get("renderer", "")+
                "\n"+stats.get("sort_reason", "")+"\n"+stats.get("gpu_timing_reason", "")+
                "\n"+tr("ubuntu.visibilityTiming").format(age=number(stats.get("visibility_age_ms")),
                    source_time="—" if stats.get("visibility_source_time") is None else stats["visibility_source_time"])+"\n"+stats.get("visibility_reason", ""))

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
        if self._updating:
            return
        value = spin.get_value()
        if attribute == 'near_clip' and not .0001 <= value <= 100:
            self.status.set_text(tr('display.invalidClip'))
            return
        if attribute in ("path_density", "path_opacity"):
            value /= 100
        setattr(self.viewport, attribute, value)
        if attribute == "path_density":
            self._path_signature = None
            self._paths_revision += 1
            self._request_frame(self.target_time)
        self.viewport.queue_render()

    def _frame_selection(self):
        self._free_camera()
        self.viewport.frame_selection()

    def _pivot_selection(self):
        self.viewport.center_on_selection()

    def _look_through(self):
        view = self._inspected_views.get(self.primary, 0)
        for index, (owner, entry, _) in enumerate(self.camera_entries):
            if (owner, entry) == (self.primary, view):
                self.camera_drop.set_selected(index+1)
                return

    def _copy_attributes(self):
        values = [self.selection_label.get_text()]
        values.extend(title+": "+value for title,value in self.detail_view.plain_rows)
        self.get_clipboard().set("\n".join(values))

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
        elif keyval in (Gdk.KEY_1, Gdk.KEY_3, Gdk.KEY_7):
            self._preset({Gdk.KEY_1: 'front', Gdk.KEY_3: 'right', Gdk.KEY_7: 'top'}[keyval])
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
        from .snapshots import capture_png_when_ready
        self._file_busy = True
        generation = self._chooser_generation
        def completed(png, error):
            if self.closed or generation != self._chooser_generation:
                return
            self._snapshot_request = None
            if error:
                self._file_busy = False
                self.error(error)
                return
            self._save_captured_png(workspace, png, generation)
        if not workspace and self.compare_toggle.get_active():
            try:
                if not self.image_view.images:
                    raise ValueError(tr("compare.snapshotPending"))
                import io
                stream = io.BytesIO()
                self.image_view.snapshot_surface().write_to_png(stream)
                completed(stream.getvalue(), None)
            except Exception as error:
                completed(None, error)
        else:
            # Wait for GTK's completed native snapshot rather than opening a
            # save sheet against an invalidated layout or an old GL framebuffer.
            self._snapshot_request = capture_png_when_ready(self if workspace else self.viewport,
                completed, cancelled=lambda: self.closed or generation != self._chooser_generation,
                ready=lambda: self.document is None or (self.frame is not None and not self._domain_busy))

    def _save_captured_png(self, workspace, png, generation):
        dialog = Gtk.FileDialog(title=tr("file.workspaceSnapshot" if workspace else "file.screenshot"), modal=True)
        dialog.set_initial_name("4dgs-workspace.png" if workspace else "4dgs-screenshot.png")
        image_filters = Gio.ListStore.new(Gtk.FileFilter)
        image_filter = Gtk.FileFilter(name="PNG")
        image_filter.add_mime_type("image/png")
        image_filters.append(image_filter)
        dialog.set_filters(image_filters)
        self._file_busy = True
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

    def graphics_information(self):
        information = dict(self.viewport.graphics_info)
        stats = getattr(self, "_graphics_stats", {})
        information["sorting"] = stats.get("sort_backend", "—")
        information["sorting_details"] = stats.get("sort_reason", "—")
        dialog = Adw.MessageDialog(transient_for=self, heading=tr("ubuntu.graphicsInformation"), body="\n\n".join(f"{key}: {value}" for key,value in information.items()))
        dialog.add_response("ok", tr("common.ok"))
        dialog.present()

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
        from urllib.parse import urlsplit
        author = PRODUCT_IDENTITY.get('GSVStudioAuthor', {})
        names = list(dict.fromkeys(value.strip() for key in ('Name', 'NativeName')
            if isinstance(value := author.get(key), str) and value.strip()))
        dialog = Adw.AboutWindow(transient_for=self, modal=True,
            application_name=PRODUCT_IDENTITY.get('CFBundleDisplayName', PRODUCT_IDENTITY.get('CFBundleName', '4DGS Studio')),
            application_icon=APPLICATION_ID, developer_name=' · '.join(names), version=VERSION,
            copyright=PRODUCT_IDENTITY.get('NSHumanReadableCopyright', ''),
            website="https://github.com/Lightcone-ZhangYifa/4dgs-studio", license_type=Gtk.License.MIT_X11)
        profile = author.get('ProfileURL', '').strip()
        address = urlsplit(profile)
        if address.scheme == 'https' and address.hostname and address.username is None and address.password is None:
            dialog.add_link(tr("about.github", "GitHub"), profile)
        dialog.present()

    def _close_requested(self, window):
        if self.closed:
            return False
        if self.dirty:
            self._confirm_discard(self._finish_close, closing=True)
            return True
        self._finish_close()
        return True

    def _finish_close(self):
        self.closed = True
        self._chooser_generation += 1
        if self._snapshot_request:
            self._snapshot_request.cancel()
            self._snapshot_request = None
        self._set_playing(False)
        self._compare_revision += 1
        GLib.source_remove(self._timer)
        if self._stats_source:
            GLib.source_remove(self._stats_source)
            self._stats_source = 0
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
