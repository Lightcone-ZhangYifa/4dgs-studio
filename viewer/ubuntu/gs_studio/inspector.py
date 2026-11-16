"""Structured native properties; original descriptors remain available on demand."""
from __future__ import annotations

import json
import gi
import numpy as np

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GLib
from .i18n import tr


class Inspector(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_start(10)
        self.set_margin_end(10)
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        self.group = Adw.PreferencesGroup(title=GLib.markup_escape_text(tr("inspector.object", "Object")))
        self.append(self.group)
        self.rows = []
        self._row_titles = ()
        self._subtitles = ()
        self._group_title = tr("inspector.object", "Object")
        self._raw_text = None
        self._descriptor_key = None
        self._descriptor_references = None
        self._descriptor_value = None
        self._descriptor_text = None
        self.signature = None
        self.raw = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, left_margin=8, right_margin=8, bottom_margin=8)
        self.expander = Gtk.Expander(label=tr("inspector.raw", "View object descriptor…"))
        self.expander.set_child(self.raw)
        self.append(self.expander)

    def _title(self, title):
        if title != self._group_title:
            self.group.set_title(GLib.markup_escape_text(title))
            self._group_title = title

    def _show_rows(self, values):
        """Preserve GTK/A11y objects, focus and scroll while playback changes values."""
        titles = tuple(key for key, _ in values)
        subtitles = tuple(str(value) for _, value in values)
        if titles != self._row_titles:
            for row in self.rows:
                self.group.remove(row)
            self.rows = []
            for title, subtitle in zip(titles, subtitles):
                row = Adw.ActionRow(title=GLib.markup_escape_text(title),
                                   subtitle=GLib.markup_escape_text(subtitle), subtitle_selectable=True)
                row.set_subtitle_lines(0)
                self.group.add(row)
                self.rows.append(row)
        else:
            for row, previous, value in zip(self.rows, self._subtitles, subtitles):
                if value != previous:
                    row.set_subtitle(GLib.markup_escape_text(value))
        self._row_titles, self._subtitles = titles, subtitles

    def _show_raw(self, text):
        if text != self._raw_text:
            self.raw.get_buffer().set_text(text)
            self._raw_text = text

    def _descriptor_snapshot(self, document, primary, track):
        """Serialize unchanged source/authored descriptors once, outside ticks."""
        key = (id(document),primary,id(track),id(track.keyframes) if track else None,
               (track.id,track.label,track.space,track.clock) if track else None)
        if key != self._descriptor_key:
            if track:
                descriptor = {'id':track.id,'label':track.label,'space':track.space,
                              'clock':track.clock,'key_times':track.times.tolist()}
            elif primary:
                descriptor = document.object(primary)
            else:
                descriptor = {name:document.manifest.get(name,{}) for name in ('spaces','clocks','metadata')}
            self._descriptor_value = descriptor
            self._descriptor_text = json.dumps(descriptor,indent=2,ensure_ascii=False)
            self._descriptor_key = key
            # Keep identities alive while cached, avoiding object-id reuse.
            self._descriptor_references = document,track,track.keyframes if track else None
        return self._descriptor_value,self._descriptor_text

    def document(self, document, frame=None):
        """Independent document pane, matching the native workspace contract."""
        count = getattr(frame, 'count', None)
        if count is None:
            count = sum(document.manifest['arrays'].get(document.object(identifier)['data'].get('ids'), {})
                        .get('shape', [0])[0] for identifier in document.visible_ids)
        space = document.manifest.get('spaces', {}).get(document.space, {})
        clock = document.manifest.get('clocks', {}).get(document.clock, {})
        signature = ('document', id(document), document.space, document.clock, count,
                     len(document.times), tuple(document.visible_ids), json.dumps(space, sort_keys=True),
                     json.dumps(clock, sort_keys=True))
        if signature == self.signature:
            return
        self.signature = signature
        self._title(tr('document.summary', 'Document'))
        coordinate = tr('coordinate.summary')
        for value in (tr('unit.'+space.get('unit', 'unitless'), space.get('unit', 'unitless')),
                      tr('coordinate.'+space.get('handedness', ''), space.get('handedness', '—')),
                      ' · '.join(space.get('axes', []))):
            coordinate = coordinate.replace('%@', value, 1)
        clock_note = tr('clock.native')
        if 'mapping' in clock:
            clock_note = tr('clock.mapping')
            for value in (clock['mapping']['target'], str(clock['mapping']['scale']), str(clock['mapping']['offset'])):
                clock_note = clock_note.replace('%@', value, 1)
        values = [('document.file', document.path.name), ('document.objects', len(document.objects)),
                  ('document.primitives', count), ('document.samples', len(document.times)),
                  ('property.space', document.space), ('document.coordinates', coordinate if space else '—'),
                  ('property.clock', document.clock), ('property.units', tr('unit.'+document.clock_unit, document.clock_unit)),
                  ('document.clock', clock_note+'\n\n'+tr('document.clockNote')),
                  ('document.storage', tr('object.sourceSafe'))]
        self._show_rows([(tr(key),value) for key,value in values])
        _,raw_text = self._descriptor_snapshot(document,None,None)
        self._show_raw(raw_text)

    def update(self, document, primary, selected, frame, calibration, track, time, primary_member=None, elements=None):
        is_element = any(i is not None for _, i in selected)
        signature = (id(document), primary, primary_member, frozenset(selected), time if is_element or calibration else None,
                     (id(track), id(track.keyframes), len(track.keyframes), track.label) if track else None,
                     id(calibration), tuple(sorted((key, id(value)) for key,value in (elements or {}).items())))
        if signature == self.signature:
            return
        self.signature = signature
        values = []
        def prop(key, value):
            if value is None:
                value = "—"
            elif isinstance(value, np.ndarray):
                value = np.array2string(value, precision=6, suppress_small=True)
            values.append((key,value))
        descriptor,raw_text = self._descriptor_snapshot(document,primary,track)
        if primary and track is None:
            self._title(descriptor.get("label", primary))
            for key, field in [("property.identity", "id"), ("property.type", "type"), ("property.encoding", "encoding"), ("property.space", "space"), ("property.clock", "clock")]:
                prop(tr(key, field.title()), descriptor.get(field))
            role = descriptor.get('data', {}).get('role', descriptor.get("metadata", {}).get("role"))
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
            prop(tr('object.readonly'), tr('object.sourceSafe'))
            if (descriptor['type'] not in ('camera', 'image', 'render')
                    and not document.supports(descriptor)):
                prop(tr('object.storage'), tr('object.unsupported'))
        elif track:
            self._title(track.label)
            prop(tr("property.identity", "Identity"), track.id)
            prop(tr("property.space", "Space"), track.space)
            prop(tr("property.clock", "Clock"), track.clock)
            prop(tr("camera.keys", "Camera keyframes"), len(track.keyframes))
        else:
            self._title(tr("document.summary", "Document"))
            prop(tr("document.file", "File"), document.path.name)
            prop(tr("document.objects", "Objects"), len(document.objects))
            prop(tr("document.samples", "Time samples"), len(document.times))
            prop(tr("property.space", "Space"), document.space)
            prop(tr("property.clock", "Clock"), document.clock)
            prop(tr("property.units", "Units"), tr("unit."+document.clock_unit, document.clock_unit))
        if calibration is not None:
            prop(tr('camera.evaluatedTime'), calibration.details.get('evaluated_time', time))
            prop(tr("camera.imageSize", "Image size"), f"{calibration.width} × {calibration.height}")
            prop(tr("camera.pixelCenter", "Pixel centers"), calibration.pixel_center)
            prop(tr("camera.intrinsics", "K · pixels"), calibration.K)
            prop(tr("camera.pose", "Camera → world"), calibration.c2w)
            if calibration.details:
                prop(tr("camera.captureContract", "Camera and render contract"), json.dumps(calibration.details, indent=2, ensure_ascii=False))
            if descriptor.get('clock') and descriptor.get('clock') != document.clock:
                prop(tr('camera.foreignClock'), tr('camera.foreignClockHint'))
            if descriptor.get('space') and descriptor.get('space') != document.space:
                prop(tr('property.space'), tr('camera.foreignSpace'))
        if frame is not None or elements:
            for owner, identity in sorted(selected, key=lambda pair: (pair != primary_member, pair[0], pair[1] or 0)):
                if identity is None:
                    continue
                prop(tr("property.objectID"), owner)
                if (elements is not None and (owner,identity) in elements) or hasattr(frame, 'element'):
                    from gsv.evaluate import quat_matrix
                    element = elements[(owner,identity)] if elements is not None and (owner,identity) in elements else frame.element(owner, identity)
                    prop(tr("selection.exactID"), identity)
                    prop(tr('property.present'), tr('common.yes') if element is not None and element.valid else tr('element.absent'))
                    if element is None:
                        continue
                    prop(tr('property.position'), element.position)
                    prop(tr('property.confidence'), element.confidence if element.confidence >= 0 else tr('common.notProvided'))
                    source = document.object(owner)
                    if source['type'] == 'scaffold':
                        prop(tr('inspector.scaffold'), tr('scaffold.glyphNote'))
                        if element.details:
                            prop(tr('inspector.provenance'), json.dumps(element.details, ensure_ascii=False, indent=2))
                        continue
                    r = quat_matrix(element.quaternion)
                    covariance = (r * element.scales[None,:]) @ (r * element.scales[None,:]).T
                    for key, value in [("property.color",element.color),
                                       ("property.opacity",element.opacity), ("property.sigma",element.scales),
                                       ("property.quaternion",element.quaternion), ("ubuntu.covariance",covariance)]:
                        prop(tr(key), value)
                    domain = source['data'].get('color_space')
                    prop(tr('property.colorSpace'), tr('colorSpace.'+domain, domain) if domain else tr('common.notProvided'))
                    prop(tr('inspector.gaussian'), tr('element.colorNote'))
                    continue
                matching = np.flatnonzero((frame.object_ids == owner) & (frame.ids == identity))
                prop(tr("selection.exactID", "Exact element ID"), identity)
                if not len(matching):
                    prop(tr('property.present'), tr("element.absent", "Absent at this time"))
                    continue
                i = matching[0]
                prop(tr('property.present'), tr('common.yes'))
                if document.object(owner)['type'] == 'scaffold':
                    prop(tr('property.position'), frame.means[i])
                    prop(tr('property.confidence'), tr('common.notProvided'))
                    prop(tr('inspector.scaffold'), tr('scaffold.glyphNote'))
                    continue
                for key, value in [("property.position", frame.means[i]), ("ubuntu.rgb", frame.colors[i]), ("ubuntu.opacity", frame.opacity[i]), ("ubuntu.covariance", frame.covariance[i])]:
                    prop(tr(key), value)
                if hasattr(frame, 'records'):
                    prop(tr("property.sigma"), frame.records[i, 4:7])
                    prop(tr("property.quaternion"), frame.records[i, 8:12])
                    prop(tr("property.confidence"), frame.records[i, 15] if frame.records[i, 15] >= 0 else tr("common.notProvided"))
        self._show_rows(values)
        self._show_raw(raw_text)

    def comparison(self, result=None, message=None):
        self.signature = None
        self._title(tr("compare.title", "Image comparison"))
        values = []
        if message:
            values.append((tr("status.ready", "Status"), message))
        if result:
            values.extend([(tr("property.role", "Role"), tr("role."+result.role, result.role) if result.role else "—"),
                           (tr("camera.imageSize", "Image size"), f"{result.render.shape[1]} × {result.render.shape[0]}"),
                           (tr("ubuntu.colorSpace", "Color space"), result.color_space)])
            metrics = result.metrics
            if metrics is not None:
                for key in ("psnr", "ssim", "mse", "mae"):
                    value = metrics.get(key)
                    values.append((key.upper(), "—" if value is None else f"{value:.9g}"+(" dB" if key == "psnr" else "")))
                values.extend([(tr("compare.pixels", "Valid pixels"), metrics.get("valid_pixels", "—")),
                               (tr("compare.windows", "SSIM windows"), metrics.get("ssim_windows", "—"))])
            self._show_raw(str(result.details)+"\n\n"+json.dumps(metrics, indent=2, ensure_ascii=False))
        else:
            self._show_raw("")
        self._show_rows(values)
