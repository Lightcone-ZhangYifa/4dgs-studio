"""Structured native properties; original descriptors remain available on demand."""
from __future__ import annotations

import json
import gi
import numpy as np

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
from .i18n import tr


class Inspector(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_start(10)
        self.set_margin_end(10)
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        self.group = Adw.PreferencesGroup(title=tr("inspector.object", "Object"))
        self.append(self.group)
        self.rows = []
        self.signature = None
        self.raw = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, left_margin=8, right_margin=8, bottom_margin=8)
        self.expander = Gtk.Expander(label=tr("inspector.raw", "View object descriptor…"))
        self.expander.set_child(self.raw)
        self.append(self.expander)

    def update(self, document, primary, selected, frame, calibration, track, time, primary_member=None):
        is_element = any(i is not None for _, i in selected)
        signature = (id(document), primary, primary_member, frozenset(selected), time if is_element or calibration else None,
                     repr([(k.time, k.c2w.tolist(), k.K.tolist()) for k in track.keyframes]) if track else None,
                     track.label if track else None)
        if signature == self.signature:
            return
        self.signature = signature
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        def prop(key, value):
            if value is None:
                value = "—"
            elif isinstance(value, np.ndarray):
                value = np.array2string(value, precision=6, suppress_small=True)
            row = Adw.ActionRow(title=key, subtitle=str(value), subtitle_selectable=True)
            row.set_subtitle_lines(0)
            self.group.add(row)
            self.rows.append(row)
        descriptor = {}
        if primary and track is None:
            descriptor = document.object(primary)
            self.group.set_title(descriptor.get("label", primary))
            for key, field in [("property.identity", "id"), ("property.type", "type"), ("property.encoding", "encoding"), ("property.space", "space"), ("property.clock", "clock")]:
                prop(tr(key, field.title()), descriptor.get(field))
            role = descriptor.get("metadata", {}).get("role")
            if role:
                prop(tr("property.role", "Role"), role)
            data = descriptor["data"]
            if "ids" in data:
                array = document.manifest["arrays"].get(data["ids"], {})
                shape = array.get("shape", [])
                if shape:
                    prop(tr("document.primitives", "Visible IDs"), shape[0])
            if "color_space" in data:
                prop(tr("ubuntu.colorSpace", "Color space"), data["color_space"])
            if descriptor.get("links"):
                prop(tr("ubuntu.links", "Linked objects"), json.dumps(descriptor["links"], ensure_ascii=False, indent=2))
        elif track:
            self.group.set_title(track.label)
            prop(tr("property.identity", "Identity"), track.id)
            prop(tr("property.space", "Space"), track.space)
            prop(tr("property.clock", "Clock"), track.clock)
            prop(tr("camera.keys", "Camera keyframes"), ", ".join(f"{k.time:.9g}" for k in track.keyframes))
            descriptor = {"id": track.id, "label": track.label, "space": track.space, "clock": track.clock, "key_times": [k.time for k in track.keyframes]}
        else:
            self.group.set_title(tr("document.summary", "Document"))
            prop(tr("document.file", "File"), document.path.name)
            prop(tr("document.objects", "Objects"), len(document.objects))
            prop(tr("document.samples", "Time samples"), len(document.times))
            prop(tr("property.space", "Space"), document.space)
            prop(tr("property.clock", "Clock"), document.clock)
            prop(tr("property.units", "Units"), tr("unit."+document.clock_unit, document.clock_unit))
            descriptor = {key: document.manifest.get(key, {}) for key in ("spaces", "clocks", "metadata")}
        if calibration is not None:
            prop(tr("camera.imageSize", "Image size"), f"{calibration.width} × {calibration.height}")
            prop(tr("camera.pixelCenter", "Pixel centers"), calibration.pixel_center)
            prop(tr("camera.intrinsics", "K · pixels"), calibration.K)
            prop(tr("camera.pose", "Camera → world"), calibration.c2w)
            if calibration.details:
                prop(tr("camera.captureContract", "Camera and render contract"), json.dumps(calibration.details, indent=2, ensure_ascii=False))
        if frame is not None:
            for owner, identity in sorted(selected, key=lambda pair: (pair != primary_member, pair[0], pair[1] or 0)):
                if identity is None:
                    continue
                prop(tr("property.objectID"), owner)
                if hasattr(frame, 'element'):
                    from gsv.evaluate import quat_matrix
                    element = frame.element(owner, identity)
                    prop(tr("selection.exactID"), identity)
                    if element is None or not element.valid:
                        prop(tr("property.position"), tr("element.absent"))
                        continue
                    r = quat_matrix(element.quaternion)
                    covariance = (r * element.scales[None,:]) @ (r * element.scales[None,:]).T
                    for key, value in [("property.position",element.position), ("property.color",element.color),
                                       ("property.opacity",element.opacity), ("property.sigma",element.scales),
                                       ("property.quaternion",element.quaternion), ("ubuntu.covariance",covariance),
                                       ("property.confidence",element.confidence if element.confidence >= 0 else tr("common.notProvided"))]:
                        prop(tr(key), value)
                    continue
                matching = np.flatnonzero((frame.object_ids == owner) & (frame.ids == identity))
                prop(tr("selection.exactID", "Exact element ID"), identity)
                if not len(matching):
                    prop(tr("property.position", "Position XYZ"), tr("element.absent", "Absent at this time"))
                    continue
                i = matching[0]
                for key, value in [("property.position", frame.means[i]), ("ubuntu.rgb", frame.colors[i]), ("ubuntu.opacity", frame.opacity[i]), ("ubuntu.covariance", frame.covariance[i])]:
                    prop(tr(key), value)
                if hasattr(frame, 'records'):
                    prop(tr("property.sigma"), frame.records[i, 4:7])
                    prop(tr("property.quaternion"), frame.records[i, 8:12])
                    prop(tr("property.confidence"), frame.records[i, 15] if frame.records[i, 15] >= 0 else tr("common.notProvided"))
        self.raw.get_buffer().set_text(json.dumps(descriptor, indent=2, ensure_ascii=False))

    def comparison(self, result=None, message=None):
        self.signature = None
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        self.group.set_title(tr("compare.title", "Image comparison"))
        values = []
        if message:
            values.append((tr("status.ready", "Status"), message))
        if result:
            values.extend([(tr("property.role", "Role"), tr("role."+result.role, result.role) if result.role else "—"),
                           (tr("camera.imageSize", "Image size"), f"{result.render.shape[1]} × {result.render.shape[0]}"),
                           (tr("ubuntu.colorSpace", "Color space"), result.color_space)])
            metrics = result.metrics or {}
            for key in ("psnr", "ssim", "mse", "mae"):
                value = metrics.get(key)
                values.append((key.upper(), "—" if value is None else f"{value:.9g}"+(" dB" if key == "psnr" else "")))
            values.extend([(tr("compare.pixels", "Valid pixels"), metrics.get("valid_pixels", "—")),
                           (tr("compare.windows", "SSIM windows"), metrics.get("ssim_windows", "—"))])
            self.raw.get_buffer().set_text(str(result.details)+"\n\n"+json.dumps(metrics, indent=2, ensure_ascii=False))
        else:
            self.raw.get_buffer().set_text("")
        for key, value in values:
            row = Adw.ActionRow(title=key, subtitle=str(value), subtitle_selectable=True)
            row.set_subtitle_lines(0)
            self.group.add(row)
            self.rows.append(row)
