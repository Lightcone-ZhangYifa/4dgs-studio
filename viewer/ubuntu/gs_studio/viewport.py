"""GTK4 GLArea with native pointer, keyboard and gesture controllers."""
from __future__ import annotations

import math
import time as clock
from types import SimpleNamespace
import numpy as np
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GLib
import os
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
from OpenGL import GL as gl

from .navigation import Navigation, operation, rotation, trail_start
from .gl_renderer import Renderer, projected_ellipses


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def project_segments(points, colors, k, c2w, width, height, near=.001):
    """Clip independent world segments at the near plane before projection."""
    points = np.asarray(points, np.float64).reshape(-1, 2, 3)
    if not len(points):
        return np.empty((0, 8), np.float32)
    colors = np.asarray(colors, np.float64).reshape(-1, 2, 4)
    camera = (points - c2w[:3, 3]) @ c2w[:3, :3]
    valid = np.isfinite(camera).all(axis=(1, 2)) & (np.max(camera[:, :, 2], axis=1) > near)
    camera, colors = camera[valid].copy(), colors[valid].copy()
    if not len(camera):
        return np.empty((0, 8), np.float32)
    for endpoint in (0, 1):
        other = 1 - endpoint
        cut = camera[:, endpoint, 2] < near
        delta = camera[cut, other] - camera[cut, endpoint]
        fraction = (near - camera[cut, endpoint, 2]) / delta[:, 2]
        camera[cut, endpoint] += delta * fraction[:, None]
        colors[cut, endpoint] += (colors[cut, other] - colors[cut, endpoint]) * fraction[:, None]
    projected = camera @ k.T
    xy = projected[:, :, :2] / camera[:, :, 2, None]
    # Reject fully offscreen segments; GL clips crossing segments itself.
    keep = ~((xy[:, :, 0].max(axis=1) < 0) | (xy[:, :, 0].min(axis=1) > width) | (xy[:, :, 1].max(axis=1) < 0) | (xy[:, :, 1].min(axis=1) > height))
    xy, camera, colors = xy[keep], camera[keep], colors[keep]
    data = np.empty((len(xy), 2, 8), np.float32)
    data[:, :, :2] = xy
    data[:, :, 2] = np.clip(1 - near / camera[:, :, 2] - 1e-6, 0, 1)
    data[:, :, 3:7] = colors
    data[:, :, 7] = camera[:, :, 2]
    return data.reshape(-1, 8)


class Viewport(Gtk.GLArea):
    """All methods, including set_frame(), are called on GTK's main thread.

    on_pick(object_id, int_id, extend) publishes the same signed identity as GSV;
    empty clicks publish (None,None,extend). on_camera_pick(object_id,view) handles
    camera wireframes. on_key(keyval,state) may consume transport shortcuts.
    on_stats(dict) reports CPU submission time (never mislabelled GPU timing).
    """
    __gtype_name__ = 'GSVStudioViewport'

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True, focusable=True)
        self.set_required_version(3, 3)
        if hasattr(self, 'set_allowed_apis'):
            self.set_allowed_apis(Gdk.GLAPI.GL)
        else:
            self.set_use_es(False)
        self.set_has_depth_buffer(True)
        # Parent labels and accessibility redraws reuse the completed texture.
        # Projection/sort/render is submitted only for explicit scene/view changes.
        self.set_auto_render(False)
        self.navigation = Navigation()
        self.frame = None
        self.selected = set()
        self.on_pick = self.on_camera_pick = self.on_camera_changed = None
        self.on_key = self.on_error = self.on_stats = None
        self.show_grid = self.show_cameras = self.show_paths = self.show_trails = True
        self.path_opacity = .18
        self.path_density = 1.
        self.trail_intervals = 8
        self.resolution_scale = 1.
        self.exposure = 0.
        self.exposure_multiplier = 1.
        self.gaussian_scale = 1.
        self.opacity_multiplier = 1.
        self.near_clip = .001
        self.background = 'studio'
        self.show_pivot = True
        self.sensitivity = 1.
        self.tool = 'select'
        self.time = 0.
        self.source_times = np.empty(0, np.float64)
        self.paths = []
        self.cameras = []
        self.render_error = None
        self.graphics_info = {}
        self._calibration = None
        self._renderer = None
        self._dirty = True
        self._last_upload_pose = None
        self._last_selection = None
        self._history = []
        self._history_index = -1
        self._dragged = False
        self._drag_cancelled = False
        self._gesture_cancelled = False
        self._navigation_active = False
        self._previous_delta = np.zeros(2)
        self._scroll_history = 0
        self._history_add()
        self.connect('realize', self._realize)
        self.connect('unrealize', self._unrealize)
        self.connect('render', self._render)
        self.connect('resize', lambda *_: self.queue_render())
        drag = Gtk.GestureDrag.new()
        drag.set_button(0)
        drag.connect('drag-begin', self._drag_begin)
        drag.connect('drag-update', self._drag_update)
        drag.connect('drag-end', self._drag_end)
        self.add_controller(drag)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect('scroll', self._scroll)
        self.add_controller(scroll)
        keys = Gtk.EventControllerKey.new()
        keys.connect('key-pressed', self._key)
        self.add_controller(keys)
        focus = Gtk.EventControllerFocus.new()
        focus.connect('leave', lambda *_: self.cancel_navigation())
        self.add_controller(focus)
        zoom = Gtk.GestureZoom.new()
        zoom.connect('begin', self._zoom_begin)
        zoom.connect('scale-changed', self._zoom)
        zoom.connect('end', lambda *_: self._end_navigation())
        self.add_controller(zoom)
        rotate = Gtk.GestureRotate.new()
        rotate.connect('begin', self._roll_begin)
        rotate.connect('angle-changed', self._roll)
        rotate.connect('end', lambda *_: self._end_navigation())
        self.add_controller(rotate)

    def _realize(self, _area):
        self.make_current()
        if self.get_error():
            self._report_error(str(self.get_error()))
            return
        try:
            display_type = self.get_display().__gtype__.name
            self.graphics_info = {
                'vendor': (gl.glGetString(gl.GL_VENDOR) or b'').decode(errors='replace'),
                'renderer': (gl.glGetString(gl.GL_RENDERER) or b'').decode(errors='replace'),
                'version': (gl.glGetString(gl.GL_VERSION) or b'').decode(errors='replace'),
                'display_backend': 'wayland' if 'Wayland' in display_type else 'x11' if 'X11' in display_type else display_type,
            }
            self._renderer = Renderer()
            self._renderer.initialize()
            self.render_error = None
            self._dirty = True
            self.queue_render()
        except Exception as error:
            self._report_error(str(error))

    def _unrealize(self, _area):
        if self._scroll_history:
            GLib.source_remove(self._scroll_history)
            self._scroll_history = 0
        self.make_current()
        if self._renderer and not self.get_error():
            self._renderer.close()
        self._renderer = None
        self._last_upload_pose = None

    def _report_error(self, text):
        first = self.render_error != text
        self.render_error = text
        if first and self.on_error:
            self.on_error(text)

    def set_frame(self, frame):
        self.frame = frame
        self._dirty = True
        self.queue_render()

    def set_paths(self, paths):
        self.paths = list(paths)
        self.queue_render()

    def set_camera_wireframes(self, calibrations):
        self.cameras = list(calibrations)
        self.queue_render()

    def set_selection(self, selected):
        self.selected = set(selected)
        self._dirty = True
        self.queue_render()

    def _dimensions(self):
        return max(1, self.get_width()), max(1, self.get_height())

    def get_camera(self):
        width, height = self._dimensions()
        if self._calibration is not None:
            k = np.array(_get(self._calibration, 'K'), np.float64, copy=True)
            native_width, native_height = _get(self._calibration, 'width'), _get(self._calibration, 'height')
            scale = min(width / native_width, height / native_height)
            k[:2, :] *= scale
            # Convert integer raster centers to half-integer GL framebuffer centers
            # before fitting the native raster, including its half-pixel shift.
            shift = .5 if _get(self._calibration, 'pixel_center', 'integer') == 'integer' else 0.
            k[0, 2] += (width - native_width * scale) / 2 + shift * scale
            k[1, 2] += (height - native_height * scale) / 2 + shift * scale
        else:
            k = self.navigation.intrinsics(width, height)
        return k, self.navigation.c2w.copy(), width, height

    def set_camera(self, calibration, *, record_history=True):
        if calibration is None:
            self.free_camera()
            return
        distortion = _get(calibration, 'distortion', 'none')
        if isinstance(distortion, dict):
            distortion = distortion.get('model', 'none')
        if distortion != 'none':
            raise ValueError('Distorted cameras are inspectable but cannot be used as calibrated previews')
        if record_history:
            self._history_add()
        self._calibration = calibration
        self.navigation.c2w = np.array(_get(calibration, 'c2w'), np.float64, copy=True)
        self.navigation.fov = math.degrees(2 * math.atan(_get(calibration, 'height') / (2 * _get(calibration, 'K')[1, 1])))
        # Source-camera sampling must preserve an explicit world-space pivot.
        # Navigation measures its current depth/distance from the new pose.
        self._dirty = True
        if record_history:
            self._history_add()
        self.queue_render()

    def free_camera(self):
        self._calibration = None
        self.queue_render()

    def frame_scene(self):
        self._history_add()
        self._calibration = None
        if self.frame is not None and len(self.frame.ids):
            if getattr(self.frame, 'is_prepared', False):
                self.navigation.frame(np.stack(self.frame.bounds), *self._dimensions())
            else:
                self._frame_points(self.frame.means, self.frame.covariance)
        self._history_add()
        self._changed()

    def frame_selection(self):
        if self.frame is None or not self.selected:
            return self.frame_scene()
        self._history_add()
        self._calibration = None
        if getattr(self.frame, 'is_prepared', False):
            bounds = []
            for owner, identity in self.selected:
                if identity is None:
                    owned = getattr(self.frame, 'object_bounds', {}).get(owner)
                    if owned is not None:
                        bounds.extend(owned)
                    elif any(o == owner for o, _, _, _ in self.frame.segments):
                        bounds.extend(self.frame.bounds)
                else:
                    element = self.frame.element(owner, identity)
                    if element is not None and element.valid:
                        radius = 3 * self.gaussian_scale * np.linalg.norm(element.scales)
                        bounds.extend([element.position-radius,element.position+radius])
            if bounds:
                self.navigation.frame(np.asarray(bounds), *self._dimensions())
        else:
            flags = np.zeros(len(self.frame.means), bool)
            for object_id, identity in self.selected:
                flags |= ((self.frame.object_ids == object_id) if identity is None else ((self.frame.object_ids == object_id) & (self.frame.ids == identity)))
            if np.any(flags):
                self._frame_points(self.frame.means[flags], self.frame.covariance[flags])
        chosen = [self._camera_segments(c) for c in self.cameras
                  if (_get(c, 'object_id', _get(c, 'id')), None) in self.selected]
        if chosen:
            self.navigation.frame(np.concatenate(chosen).reshape(-1,3), *self._dimensions())
        self._history_add()
        self._changed()

    def _frame_points(self, means, covariance):
        radius = 3 * self.gaussian_scale * np.sqrt(np.maximum(np.diagonal(covariance, axis1=1, axis2=2), 0))
        low, high = (means - radius).min(axis=0), (means + radius).max(axis=0)
        self.navigation.frame(np.stack([low, high]), *self._dimensions())

    def set_up_axis(self, name):
        value = str(name).lower()
        sign = -1 if value.startswith('-') else 1
        value = value.lstrip('+-')
        if value not in ('x', 'y', 'z'):
            raise ValueError('Navigation up axis must be x, y or z')
        self.navigation.up = np.eye(3)['xyz'.index(value)] * sign
        self.queue_render()

    @property
    def navigation_mode(self):
        return self.navigation.mode

    @navigation_mode.setter
    def navigation_mode(self, value):
        self.navigation.mode = value

    def preset(self, direction):
        vectors = {'front': (0, 0, 1), 'back': (0, 0, -1), 'left': (-1, 0, 0), 'right': (1, 0, 0), 'top': (0, 1, 0), 'bottom': (0, -1, 0), 'isometric': (1, 1, 1)}
        if direction not in vectors:
            raise ValueError('Unknown viewpoint')
        self._history_add()
        self._calibration = None
        back = np.array(vectors[direction], np.float64)
        back /= np.linalg.norm(back)
        up = self.navigation.up
        if abs(back @ up) > .99:
            up = np.array([0., 0., -1.]) if abs(back[2]) < .99 else np.array([0., 1., 0.])
        right = np.cross(up, back); right /= np.linalg.norm(right)
        down = np.cross(right, -back)
        self.navigation.c2w[:3, :3] = np.stack([right, down, -back], axis=1)
        self.frame_scene()

    def center_on_depth(self):
        if self.frame is None or not len(self.frame.ids):
            return
        k, pose, width, height = self.get_camera()
        if getattr(self.frame, 'is_prepared', False):
            weight = np.float32(self.frame.weight) if self.frame.weights is None else self.frame.weights[:,None]
            means = self.frame.first[:,:3]*(1-weight)+self.frame.second[:,:3]*weight
            valid = np.empty(len(self.frame.ids),bool)
            for _,start,stop,w in self.frame.segments:
                a,b = self.frame.first[start:stop,7]>.5,self.frame.second[start:stop,7]>.5
                valid[start:stop] = a if w==0 else b if w==1 else a&b
            means = means[valid]
        else:
            means = self.frame.means
        points = (means - pose[:3, 3]) @ pose[:3, :3]
        valid = points[:, 2] > 1e-4
        if not np.any(valid):
            return
        projected = points[valid] @ k.T
        pixel = projected[:, :2] / points[valid, 2, None]
        visible = (pixel[:, 0] >= 0) & (pixel[:, 0] < width) & (pixel[:, 1] >= 0) & (pixel[:, 1] < height)
        if np.any(visible):
            depth = float(np.median(points[valid, 2][visible]))
            self.navigation.pivot = pose[:3, 3] + pose[:3, 2] * depth
            self.queue_render()

    def _history_add(self):
        snapshot = (self.navigation.c2w.copy(), self.navigation.pivot.copy(), self.navigation.fov, self._calibration)
        if self._history_index >= 0:
            old = self._history[self._history_index]
            if np.array_equal(old[0], snapshot[0]) and np.array_equal(old[1], snapshot[1]) and old[2] == snapshot[2] and old[3] is snapshot[3]:
                return
        del self._history[self._history_index + 1:]
        self._history.append(snapshot)
        if len(self._history) > 64:
            self._history.pop(0)
        self._history_index = len(self._history) - 1

    def previous_view(self):
        self._history_add()
        self._history_restore(self._history_index - 1)

    def next_view(self):
        self._history_restore(self._history_index + 1)

    def _history_restore(self, index):
        if not 0 <= index < len(self._history):
            return
        self._history_index = index
        pose, pivot, fov, self._calibration = self._history[index]
        self.navigation.c2w = pose.copy()
        self.navigation.pivot = pivot.copy()
        self.navigation.fov = fov
        self._changed()

    def _begin_navigation(self):
        self._navigation_active = True
        self._history_add()

    def _end_navigation(self):
        self._navigation_active = False
        self._history_add()

    def cancel_navigation(self):
        """Stop active input at its current pose, matching macOS Escape behavior."""
        import sys
        if sys.is_finalizing():
            return False
        active = self._navigation_active
        self._drag_cancelled = self._gesture_cancelled = True
        if self._scroll_history:
            GLib.source_remove(self._scroll_history)
            self._scroll_history = 0
        self._end_navigation()
        return active

    def _changed(self):
        self._dirty = True
        self.queue_render()
        if self.on_camera_changed:
            self.on_camera_changed()

    def _drag_begin(self, gesture, x, y):
        self.grab_focus()
        self._begin_navigation()
        state = gesture.get_current_event_state()
        self._drag_operation = operation(gesture.get_current_button(), alt=bool(state & Gdk.ModifierType.ALT_MASK), shift=bool(state & Gdk.ModifierType.SHIFT_MASK), control=bool(state & Gdk.ModifierType.CONTROL_MASK), tool=self.tool)
        self._extend = bool(state & Gdk.ModifierType.CONTROL_MASK)
        self._drag_cancelled = False
        self._drag_origin = np.array([x, y])
        self._previous_delta = np.zeros(2)
        self._dragged = False

    def _drag_update(self, _gesture, dx, dy):
        if self._drag_cancelled:
            return
        delta = np.array([dx, dy])
        if not self._dragged:
            length = np.linalg.norm(delta)
            if length < 4:
                return
            self._dragged = True
            change = delta * (1 - 4 / length)
        else:
            change = delta - self._previous_delta
        self._previous_delta = delta
        change *= min(5., max(.05, self.sensitivity))
        if self._drag_operation == 'select':
            return
        if self._drag_operation == 'orbit':
            self.navigation.orbit(*change)
        elif self._drag_operation == 'pan':
            self.navigation.pan(*change, self.get_camera()[0])
        elif self._drag_operation == 'dolly':
            self.navigation.dolly((change[0] + change[1]) * .24, pixel=np.array(self._dimensions()) * .5, k=self.get_camera()[0])
        self._changed()

    def _drag_end(self, _gesture, dx, dy):
        if not self._drag_cancelled and not self._dragged and (self._drag_operation == 'select' or self._extend):
            self.pick(*(self._drag_origin + [dx, dy]), extend=self._extend)
        self._end_navigation()

    def _event_pixel(self, controller):
        event = controller.get_current_event()
        if event is not None:
            valid, x, y = event.get_position()
            native = self.get_native()
            if valid and native is not None:
                gi.require_version('Graphene', '1.0')
                from gi.repository import Graphene
                # GDK events use surface coordinates; CSD shadows can translate
                # that origin before the normal widget-to-widget transformation.
                offset_x, offset_y = native.get_surface_transform()
                point = Graphene.Point(); point.init(x + offset_x, y + offset_y)
                ok, local = native.compute_point(self, point)
                if ok:
                    return np.clip([local.x, local.y], [0, 0], self._dimensions())
        return np.array(self._dimensions()) * .5

    def _gesture_pixel(self, gesture):
        valid, x, y = gesture.get_bounding_box_center()
        return np.clip([x, y], [0, 0], self._dimensions()) if valid else self._event_pixel(gesture)

    def _scroll(self, controller, dx, dy):
        if not self._scroll_history:
            self._begin_navigation()
        else:
            GLib.source_remove(self._scroll_history)
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.ALT_MASK:
            gain = min(5., max(.05, self.sensitivity))
            self.navigation.orbit(dx * gain, dy * gain)
        else:
            unit = controller.get_unit() if hasattr(controller, 'get_unit') else None
            smooth = unit == Gdk.ScrollUnit.SURFACE if unit is not None else False
            if smooth and not (state & Gdk.ModifierType.CONTROL_MASK):
                gain = min(5., max(.05, self.sensitivity))
                self.navigation.pan(dx * gain, dy * gain, self.get_camera()[0])
            else:
                gain = min(5., max(.05, self.sensitivity))
                self.navigation.dolly(dy * (.18 if smooth else 2) * gain, pixel=self._event_pixel(controller), k=self.get_camera()[0])
        self._changed()
        self._scroll_history = GLib.timeout_add(220, self._finish_scroll)
        return True

    def _finish_scroll(self):
        self._scroll_history = 0
        self._end_navigation()
        return GLib.SOURCE_REMOVE

    def _zoom_begin(self, gesture, _sequence):
        gesture._last_scale = 1.
        self._gesture_cancelled = False
        self._begin_navigation()

    def _zoom(self, gesture, scale):
        if self._gesture_cancelled or not math.isfinite(scale) or scale <= 0:
            return
        previous = getattr(gesture, '_last_scale', 1.)
        self.navigation.dolly(-math.log(max(scale, 1e-6) / previous) / .025, pixel=self._gesture_pixel(gesture), k=self.get_camera()[0])
        gesture._last_scale = scale
        self._changed()

    def _roll_begin(self, gesture, _sequence):
        gesture._last_angle = 0.
        self._gesture_cancelled = False
        self._begin_navigation()

    def _roll(self, gesture, _angle, delta):
        if self._gesture_cancelled or not math.isfinite(delta):
            return
        previous = getattr(gesture, '_last_angle', 0.)
        gesture._last_angle = delta
        self.navigation.roll(previous - delta)
        self._changed()

    def _key(self, _controller, keyval, _keycode, state):
        if keyval == Gdk.KEY_Escape and self.cancel_navigation():
            return True
        if self.on_key and self.on_key(keyval, state):
            return True
        if keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.frame_selection(); return True
        if keyval == Gdk.KEY_Home:
            self.frame_scene(); return True
        if keyval == Gdk.KEY_bracketleft:
            self.previous_view(); return True
        if keyval == Gdk.KEY_bracketright:
            self.next_view(); return True
        return False

    def pick(self, x, y, extend=False):
        if self.frame is None or not len(self.frame.ids):
            if self.on_pick:
                self.on_pick(None, None, extend)
            return None
        k, pose, width, height = self.get_camera()
        # Match the actual render raster, including HiDPI and preview resolution.
        # The .3 px² antialias filter lives in framebuffer pixels, not GTK units.
        factor = max(1, self.get_scale_factor())
        scale = min(1., max(.1, float(self.resolution_scale)))
        pick_width, pick_height = max(1, round(width * factor * scale)), max(1, round(height * factor * scale))
        sx, sy = pick_width / width, pick_height / height
        pick_k = k.copy(); pick_k[0] *= sx; pick_k[1] *= sy
        index = None
        if getattr(self.frame, 'is_prepared', False):
            self.make_current()
            if self._renderer is None or self.render_error or self.get_error():
                return None
            self._renderer.upload(self.frame, pose, self.selected)
            index = self._renderer.pick(pick_k, pose, pick_width, pick_height, [x*sx,y*sy], gaussian_scale=self.gaussian_scale, opacity_scale=self.opacity_multiplier, near_depth=self.near_clip)
        else:
            centers, covariance, depth = projected_ellipses(self.frame, pick_k, pose, width=pick_width, height=pick_height, near=self.near_clip, gaussian_scale=self.gaussian_scale)
            delta = np.array([x * sx, y * sy]) - centers
            xx, xy, yy = covariance[:, 0, 0], covariance[:, 0, 1], covariance[:, 1, 1]
            determinant = xx * yy - xy * xy
            r2 = (yy * delta[:, 0] ** 2 - 2 * xy * delta[:, 0] * delta[:, 1] + xx * delta[:, 1] ** 2) / np.maximum(determinant, 1e-24)
            valid = (depth > self.near_clip) & np.isfinite(r2) & (r2 <= 9) & (np.clip(np.asarray(self.frame.opacity) * self.opacity_multiplier, 0., .999) * np.exp(-.5 * np.maximum(r2, 0)) >= .1)
            indices = np.flatnonzero(valid)
            if len(indices):
                index = indices[np.argmin(depth[indices])]
        if index is not None:
            result = (str(self.frame.object_ids[index]), int(self.frame.ids[index]))
            if getattr(self.frame, 'is_prepared', False):
                self.navigation.pivot = self.frame.element(*result).position.astype(np.float64)
            else:
                self.navigation.pivot = np.array(self.frame.means[index], np.float64)
            if extend:
                if result in self.selected:
                    self.selected.remove(result)
                else:
                    self.selected.add(result)
            else:
                self.selected = {result}
            self._dirty = True
            self.queue_render()
            if self.on_pick:
                self.on_pick(*result, extend)
            return result
        # Wireframe picking shares the same clipped endpoints as drawing.
        best = None
        best_distance = 7.
        for calibration in self.cameras if self.show_cameras else []:
            segments = self._camera_segments(calibration)
            projected = project_segments(segments, np.ones((len(segments), 2, 4)), k, pose, width, height, near=self.near_clip)
            if not len(projected):
                continue
            endpoints = projected[:, :2].reshape(-1, 2, 2)
            delta = endpoints[:, 1] - endpoints[:, 0]
            t = np.clip(np.sum((np.array([x, y]) - endpoints[:, 0]) * delta, axis=1) / np.maximum(np.sum(delta * delta, axis=1), 1e-12), 0, 1)
            distance = np.linalg.norm(endpoints[:, 0] + delta * t[:, None] - [x, y], axis=1).min()
            if distance < best_distance:
                best, best_distance = calibration, distance
        if best is not None and self.on_camera_pick:
            self.on_camera_pick(_get(best, 'object_id', _get(best, 'id')), _get(best, 'view', 0))
            return None
        if not extend:
            self.selected.clear()
            self._dirty = True
            self.queue_render()
        if self.on_pick:
            self.on_pick(None, None, extend)
        return None

    def _camera_segments(self, calibration):
        calibration = _get(calibration, 'calibration', calibration)
        k = np.asarray(_get(calibration, 'K'))
        pose = np.asarray(_get(calibration, 'c2w'))
        width, height = _get(calibration, 'width'), _get(calibration, 'height')
        size = max(float(np.linalg.norm(self.navigation.c2w[:3, 3] - self.navigation.pivot)) * .035, 1e-3)
        shift = -.5 if _get(calibration, 'pixel_center', 'integer') == 'integer' else 0.
        pixels = np.array([[shift, shift, 1], [width+shift, shift, 1], [width+shift, height+shift, 1], [shift, height+shift, 1]])
        corners = (pixels @ np.linalg.inv(k).T * size) @ pose[:3, :3].T + pose[:3, 3]
        origin = pose[:3, 3]
        return np.asarray([[origin, p] for p in corners] + [[corners[i], corners[(i + 1) % 4]] for i in range(4)])

    def _grid_segments(self):
        up = int(np.argmax(np.abs(self.navigation.up)))
        axes = [i for i in range(3) if i != up]
        distance = max(float(np.linalg.norm(self.navigation.c2w[:3, 3] - self.navigation.pivot)), 1e-6)
        step = 10 ** math.floor(math.log10(distance) - 1)
        grid, colors = [], []
        for axis in axes:
            other = axes[1] if axis == axes[0] else axes[0]
            for offset in range(-10, 11):
                a = np.zeros(3); b = np.zeros(3)
                a[axis] = -10 * step; b[axis] = 10 * step
                a[other] = b[other] = offset * step
                grid.append([a, b]); colors.append([[.18, .22, .28, .6]] * 2)
        for axis in range(3):
            end = np.zeros(3); end[axis] = 2 * step
            color = np.eye(3)[axis] * .75 + .15
            grid.append([np.zeros(3), end]); colors.append([[*color, .9]] * 2)
        return np.asarray(grid), np.asarray(colors)

    def _path_segments(self):
        pieces, colors = [], []
        tail = trail_start(self.time, self.source_times, max(0, int(self.trail_intervals)))
        for path in self.paths:
            points = _get(path, 'positions', _get(path, 'points', path if isinstance(path, np.ndarray) else None))
            if points is None:
                continue
            points = np.asarray(points)
            if len(points) < 2:
                continue
            identity = _get(path, 'identity')
            object_id = _get(path, 'object_id')
            # SceneDocument.trajectories already applies the shared stable
            # identity density policy and unions individually selected paths.
            color = _get(path, 'colors')
            if color is None:
                base = _get(path, 'color', [1., .7, .15])
                color = np.broadcast_to(base, (len(points), 3))
            color = np.asarray(color)
            rgba = np.column_stack([color[:, :3], np.full(len(points), self.path_opacity)])
            segments = np.stack([points[:-1], points[1:]], axis=1)
            segment_colors = np.stack([rgba[:-1], rgba[1:]], axis=1)
            valid = np.isfinite(segments).all(axis=(1, 2))
            if self.show_paths:
                pieces.append(segments[valid]); colors.append(segment_colors[valid])
            times = _get(path, 'times')
            if not self.show_trails or times is None:
                continue
            times = np.asarray(times, np.float64)
            live = valid & (times[1:] >= tail) & (times[:-1] <= self.time) & (times[1:] > times[:-1])
            if not np.any(live):
                continue
            trail = segments[live].copy()
            tc = segment_colors[live].copy()
            a, b = times[:-1][live], times[1:][live]
            with np.errstate(over='ignore', invalid='ignore'):
                span = b - a
                start_offset, end_offset = tail - a, self.time - a
                scale = ~np.isfinite(span) | ~np.isfinite(start_offset) | ~np.isfinite(end_offset)
                span = np.where(scale, b * .5 - a * .5, span)
                start_offset = np.where(scale, tail * .5 - a * .5, start_offset)
                end_offset = np.where(scale, self.time * .5 - a * .5, end_offset)
                start = np.clip(start_offset / span, 0, 1)
                end = np.clip(end_offset / span, 0, 1)
            delta = trail[:, 1] - trail[:, 0]
            cd = tc[:, 1] - tc[:, 0]
            trail[:, 1] = trail[:, 0] + delta * end[:, None]
            trail[:, 0] += delta * start[:, None]
            tc[:, 1] = tc[:, 0] + cd * end[:, None]
            tc[:, 0] += cd * start[:, None]
            tc[:, :, 3] = 1.
            pieces.append(trail); colors.append(tc)
        if not pieces:
            return np.empty((0, 2, 3)), np.empty((0, 2, 4))
        return np.concatenate(pieces), np.concatenate(colors)

    def snapshot_rgba(self):
        """Copy the completed preview raster at its current resolution setting.

        Must run on GTK's main thread while this viewport is realized. The image
        has a top-left origin, owns its bytes, and never alters scene/camera state.
        """
        if not self.get_realized():
            raise RuntimeError('The viewport must be visible before saving a screenshot')
        self.make_current()
        if self.get_error() or self.render_error or self._renderer is None or not self._renderer.fbo:
            raise RuntimeError(str(self.get_error() or self.render_error or 'No completed viewport image'))
        draw = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        read = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        pack = int(gl.glGetIntegerv(gl.GL_PACK_ALIGNMENT))
        try:
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self._renderer.fbo)
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
            width, height = self._renderer.size
            data = gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
            return np.frombuffer(data, np.uint8).reshape(height, width, 4)[::-1].copy()
        finally:
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, pack)
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, read)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, draw)

    def _render(self, _area, _context):
        if self.render_error or self._renderer is None:
            return True
        start = clock.perf_counter()
        renderer = self._renderer
        destination = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        scale_factor = max(1, self.get_scale_factor())
        logical_width, logical_height = self._dimensions()
        width, height = logical_width * scale_factor, logical_height * scale_factor
        scale = min(1., max(.1, float(self.resolution_scale)))
        render_width, render_height = max(1, round(width * scale)), max(1, round(height * scale))
        k, pose, _, _ = self.get_camera()
        k[0] *= render_width / logical_width
        k[1] *= render_height / logical_height
        try:
            if self._dirty or self._last_upload_pose is None or not np.array_equal(pose, self._last_upload_pose) or self._last_selection != self.selected:
                renderer.upload(self.frame, pose, self.selected)
                self._last_upload_pose = pose.copy()
                self._last_selection = self.selected.copy()
                self._dirty = False
            renderer.target(render_width, render_height)
            renderer.begin(k, pose, render_width, render_height, exposure=self.exposure, exposure_multiplier=self.exposure_multiplier, gaussian_scale=self.gaussian_scale, opacity_scale=self.opacity_multiplier, near_depth=self.near_clip, background={'studio': (.018, .025, .04), 'black': (0., 0., 0.), 'gray': (.18, .18, .18), 'white': (1., 1., 1.)}.get(self.background, (.018, .025, .04)))
            if self.show_grid:
                points, colors = self._grid_segments()
                renderer.lines(project_segments(points, colors, k, pose, render_width, render_height, near=self.near_clip), render_width, render_height, occlude=False)
            # lines() changes the current shader; splats() restores its program.
            renderer.splats()
            renderer.splats(depth_only=True)
            edges = _get(self.frame, 'edges') if self.frame is not None else None
            if edges is not None and len(edges):
                edge_colors = np.tile([.7, .86, .75, .85], (len(edges), 2, 1))
                renderer.lines(project_segments(edges, edge_colors, k, pose, render_width, render_height, near=self.near_clip), render_width, render_height)
            if self.show_paths or self.show_trails:
                points, colors = self._path_segments()
                renderer.paths(project_segments(points, colors, k, pose, render_width, render_height, near=self.near_clip), render_width, render_height)
            if self.show_cameras:
                for calibration in self.cameras:
                    points = self._camera_segments(calibration)
                    rgba = [1., .76, .12, 1.] if _get(calibration, 'selected', False) else [.4, .7, 1., .8]
                    color = np.tile(rgba, (len(points), 2, 1))
                    renderer.lines(project_segments(points, color, k, pose, render_width, render_height, near=self.near_clip), render_width, render_height)
            # Orbit center is a fixed-size display marker, outside quantitative RGB.
            pivot = pose[:3, :3].T @ (self.navigation.pivot - pose[:3, 3])
            if self.show_pivot and pivot[2] > self.near_clip:
                center = k @ pivot; center = center[:2] / pivot[2]
                radius = 9 * scale_factor * scale
                mark = np.tile([0., 0., 0., 1., .65, .12, 1.], (4, 1))
                mark[:, :2] = center + np.array([[-radius, 0], [radius, 0], [0, -radius], [0, radius]])
                renderer.lines(mark, render_width, render_height, occlude=False)
            renderer.finish(destination, width, height)
        except Exception as error:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, destination)
            self._report_error(str(error))
        if self.on_stats:
            self.on_stats({'cpu_submission_ms': (clock.perf_counter() - start) * 1000, 'splats': renderer.count, 'render_width': render_width, 'render_height': render_height, 'sort_backend': renderer.sort_backend, 'sort_reused': renderer.last_sort_reused, 'sort_submission_ms': renderer.last_sort_submission_ms, 'sort_reason': renderer.sort_reason, 'graphics_info': self.graphics_info})
        return True
