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
from .gl_renderer import Renderer, RenderAllocationError, projected_ellipses


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


def camera_edge_hits(projected,cursor,width,height,tolerance):
    """Visible screen spans and reciprocal-depth interpolation shared with Metal."""
    if not len(projected):return np.empty((0,3),np.float64)
    vertices=np.asarray(projected).reshape(-1,2,8)
    a,b=vertices[:,0,:2],vertices[:,1,:2];delta=b-a
    first,last=np.zeros(len(a)),np.ones(len(a));valid=np.ones(len(a),bool)
    for axis,size in [(0,width),(1,height)]:
        parallel=np.abs(delta[:,axis])<1e-12
        valid &= ~parallel|((a[:,axis]>=0)&(a[:,axis]<size))
        d=np.where(parallel,1.,delta[:,axis])
        left,right=-a[:,axis]/d,(size-1e-4-a[:,axis])/d
        first=np.maximum(first,np.where(parallel,0,np.minimum(left,right)))
        last=np.minimum(last,np.where(parallel,1,np.maximum(left,right)))
    length=np.sum(delta*delta,axis=1)
    t=np.clip(np.sum((np.asarray(cursor)-a)*delta,axis=1)/np.maximum(length,1e-24),first,last)
    closest=a+delta*t[:,None]
    z=1/((1-t)/vertices[:,0,7]+t/vertices[:,1,7])
    valid &= (last>=first)&(np.sum((closest-cursor)**2,axis=1)<=tolerance*tolerance)&np.isfinite(z)&(z>0)
    return np.column_stack([closest,z])[valid]


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
        self._bounds_frame=None
        self._bounds_cache=None
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
        self.near_clip = .01
        self.background = 'studio'
        self.show_pivot = True
        self.sensitivity = 1.
        self.tool = 'select'
        self.time = 0.
        self.source_times = np.empty(0, np.float64)
        self.paths = []
        self.cameras = []
        self.render_error = None
        self.last_render_warning=None
        self._gpu_poll_source=0
        self._last_stats=None
        self.graphics_info = {}
        self._calibration = None
        self._renderer = None
        self._dirty = True
        self._render_pending = True
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
        self._scroll_operation = None
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
        scroll.connect('scroll-begin', lambda *_: setattr(self,'_scroll_operation',None))
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
        zoom.group(rotate)

    def queue_render(self):
        self._render_pending = True
        super().queue_render()

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
        if self._gpu_poll_source:
            GLib.source_remove(self._gpu_poll_source);self._gpu_poll_source=0
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
        if frame is not self.frame:
            self._bounds_frame=None;self._bounds_cache=None
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
        # Ending source-camera follow preserves the current projection and pose.
        self.queue_render()

    def _scene_bounds(self):
        if self.frame is None or not len(self.frame.ids):return None
        if getattr(self.frame,'bounds',None) is not None:return self.frame.bounds
        if self.frame is self._bounds_frame:return self._bounds_cache
        means=self.frame.means
        if not len(means):return None
        extent=3*np.sqrt(np.maximum(np.diagonal(self.frame.covariance,axis1=1,axis2=2),0))
        self._bounds_cache=(means-extent).min(axis=0),(means+extent).max(axis=0)
        self._bounds_frame=self.frame
        return self._bounds_cache

    def _scene_radius(self):
        bounds=self._scene_bounds()
        return float(np.linalg.norm(bounds[1]-bounds[0]))*.5 if bounds is not None else 3.

    def frame_scene(self):
        self._begin_navigation()
        self._calibration=None
        bounds=self._scene_bounds()
        center=np.zeros(3) if bounds is None else bounds[0]*.5+bounds[1]*.5
        self.navigation.reset(center,self._scene_radius())
        self._end_navigation();self._changed()

    def _selection_members(self):
        members=[]
        for owner,identity in self.selected:
            cameras=[entry for entry in self.cameras if _get(entry,'object_id',_get(entry,'id'))==owner]
            if cameras:
                # A rig child index is a camera view, never a Gaussian identity.
                # Hidden compatible views remain available for framing/pivot use.
                for entry in cameras:
                    if identity is None or _get(entry,'view',0)==identity:
                        calibration=_get(entry,'calibration',entry)
                        members.append((np.asarray(_get(calibration,'c2w'))[:3,3],max(.1,self._scene_radius()*.1)))
                continue
            if self.frame is not None and len(self.frame.ids):
                if getattr(self.frame,'is_prepared',False):
                    if identity is None:
                        bounds=self.frame.object_bounds.get(owner)
                        if bounds is not None:
                            members.append((bounds[0]*.5+bounds[1]*.5,max(.05,float(np.linalg.norm(bounds[1]-bounds[0]))*.5)))
                    else:
                        element=self.frame.element(owner,identity)
                        if element is not None and element.valid:
                            members.append((element.position,max(.05,float(np.linalg.norm(element.scales))*8)))
                else:
                    mask=self.frame.object_ids==owner
                    if identity is not None:mask &= self.frame.ids==identity
                    authored_bounds=getattr(self.frame,'object_bounds',{}).get(owner) if identity is None else None
                    if authored_bounds is not None:
                        low,high=authored_bounds
                        members.append((low*.5+high*.5,max(.05,float(np.linalg.norm(high-low))*.5)))
                    elif np.any(mask):
                        means=self.frame.means[mask]
                        if identity is not None:
                            radius=8*math.sqrt(max(float(np.trace(self.frame.covariance[mask][0])),0))
                            members.append((means[0],max(.05,radius)))
                        else:
                            low,high=means.min(axis=0),means.max(axis=0)
                            members.append((low*.5+high*.5,max(.05,float(np.linalg.norm(high-low))*.5)))
        return members

    def _selected_bounds(self):
        members=self._selection_members()
        if not members:return None
        low=np.min([center-radius for center,radius in members],axis=0)
        high=np.max([center+radius for center,radius in members],axis=0)
        return low,high

    def center_on_selection(self):
        bounds=self._selected_bounds()
        if bounds is not None:
            self.navigation.pivot=np.asarray(bounds[0]*.5+bounds[1]*.5,np.float64)
            self.queue_render()

    def frame_selection(self):
        if not self.selected:return self.frame_scene()
        members=self._selection_members()
        if not members:return
        k,_,width,height=self.get_camera()
        if len(members)==1:
            center,radius=members[0]
        else:
            low,high=self._selected_bounds();center=low*.5+high*.5;radius=max(.05,float(np.linalg.norm(high-low))*.5)
        self._begin_navigation()
        self.navigation.focus(center,radius,width,height,k=k)
        self._end_navigation();self._changed()

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

    def preset(self,direction):
        self._begin_navigation()
        self._calibration=None
        self.navigation.preset(direction)
        self._end_navigation();self._changed()

    def center_on_depth(self):
        k,pose,width,height=self.get_camera()
        count=len(self.frame.ids) if self.frame is not None else 0
        rows=np.arange(0,count,max(1,(count+4095)//4096))
        if count and getattr(self.frame,'is_prepared',False):
            weight=np.float32(self.frame.weight) if self.frame.weights is None else self.frame.weights[rows,None]
            means=self.frame.first[rows,:3]*(1-weight)+self.frame.second[rows,:3]*weight
            valid=np.zeros(len(rows),bool)
            for _,start,stop,w in self.frame.segments:
                selected=(rows>=start)&(rows<stop);indices=rows[selected]
                a,b=self.frame.first[indices,7]>.5,self.frame.second[indices,7]>.5
                valid[selected]=a if w==0 else b if w==1 else a&b
            means=means[valid]
        else:
            means=self.frame.means[rows] if count else np.empty((0,3))
        points=(means-pose[:3,3])@pose[:3,:3]
        points=points[np.isfinite(points).all(axis=1)&(points[:,2]>self.near_clip)]
        if len(points):
            projected=points@k.T;pixel=projected[:,:2]/points[:,2,None]
            depths=points[:,2][(pixel[:,0]>0)&(pixel[:,0]<width)&(pixel[:,1]>0)&(pixel[:,1]<height)]
        else:depths=np.empty(0)
        depth=float(np.partition(depths,len(depths)//2)[len(depths)//2]) if len(depths) else max(self.near_clip*10,min(self._scene_radius(),1e6))
        self.navigation.pivot=pose[:3,3]+pose[:3,2]*depth
        self.queue_render()

    @property
    def tool(self):return self._tool

    @tool.setter
    def tool(self,value):
        if value not in ('select','orbit','pan','dolly'):raise ValueError('Unknown viewport tool')
        self._tool=value
        self.set_cursor_from_name('default' if value=='select' else 'grab' if value=='pan' else 'crosshair')

    def _view_snapshot(self):
        return (self.navigation.c2w.copy(),self.navigation.pivot.copy(),self.navigation.fov,self._calibration,self.navigation.up.copy())

    def clear_navigation_history(self):
        self._history=[];self._history_index=-1;self._navigation_active=False
        self._history_add()

    def _history_add(self):
        snapshot = self._view_snapshot()
        if self._history_index >= 0:
            old = self._history[self._history_index]
            if np.array_equal(old[0], snapshot[0]) and np.array_equal(old[1], snapshot[1]) and old[2] == snapshot[2] and old[3] is snapshot[3] and np.array_equal(old[4],snapshot[4]):
                return
        del self._history[self._history_index + 1:]
        self._history.append(snapshot)
        if len(self._history) > 64:
            self._history.pop(0)
        self._history_index = len(self._history) - 1

    def previous_view(self):
        self._end_navigation()
        self._history[self._history_index]=self._view_snapshot()
        self._history_restore(self._history_index - 1)

    def next_view(self):
        self._history_restore(self._history_index + 1)

    def _history_restore(self, index):
        if not 0 <= index < len(self._history):
            return
        self._history_index = index
        pose, pivot, fov, self._calibration,up = self._history[index]
        self.navigation.c2w = pose.copy()
        self.navigation.pivot = pivot.copy()
        self.navigation.fov = fov
        self.navigation.up=up.copy()
        self._changed()

    def _begin_navigation(self):
        if self._navigation_active:return
        self._history[self._history_index]=self._view_snapshot()
        self._navigation_active=True

    def _end_navigation(self):
        if not self._navigation_active:return
        self._navigation_active=False
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
        self._scroll_operation=None
        self._end_navigation()
        return active

    def _changed(self):
        self._dirty = True
        self.queue_render()
        if self.on_camera_changed:
            self.on_camera_changed()

    def _drag_begin(self, gesture, x, y):
        self.grab_focus()
        self._end_navigation()
        self._scroll_operation=None
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

    def _scroll(self,controller,dx,dy):
        if not np.isfinite([dx,dy]).all() or (dx==0 and dy==0):return True
        state=controller.get_current_event_state()
        unit=controller.get_unit() if hasattr(controller,'get_unit') else None
        smooth=unit==Gdk.ScrollUnit.SURFACE if unit is not None else False
        proposed='dolly' if not smooth or state&Gdk.ModifierType.CONTROL_MASK else 'orbit' if state&Gdk.ModifierType.ALT_MASK else 'pan'
        if self._scroll_operation is None or not smooth:self._scroll_operation=proposed
        if not self._scroll_history:self._begin_navigation()
        else:GLib.source_remove(self._scroll_history)
        gain=min(5.,max(.05,self.sensitivity))
        if self._scroll_operation=='orbit':self.navigation.orbit(dx*gain,dy*gain)
        elif self._scroll_operation=='pan':self.navigation.pan(dx*gain,dy*gain,self.get_camera()[0])
        else:self.navigation.dolly(dy*(.18 if smooth else 2)*gain,pixel=self._event_pixel(controller),k=self.get_camera()[0])
        self._changed();self._scroll_history=GLib.timeout_add(220,self._finish_scroll)
        return True

    def _finish_scroll(self):
        self._scroll_history=0;self._scroll_operation=None
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

    def pick(self,x,y,extend=False):
        k,pose,width,height=self.get_camera()
        if not np.isfinite([x,y]).all() or x<0 or y<0 or x>=width or y>=height:return None
        factor=max(1,self.get_scale_factor());scale=min(1.,max(.1,float(self.resolution_scale)))
        pick_width,pick_height=max(1,round(width*factor*scale)),max(1,round(height*factor*scale))
        sx,sy=pick_width/width,pick_height/height
        pick_k=k.copy();pick_k[0]*=sx;pick_k[1]*=sy
        cursor=np.floor([x*sx,y*sy])+.5
        count=len(self.frame.ids) if self.frame is not None else 0
        index=None;gaussian_depth=math.inf
        gpu=self.get_realized() and self._renderer is not None and not self.render_error and not self.get_error()
        if gpu:
            self.make_current();self._renderer.upload(self.frame,pose,self.selected)
            hit=self._renderer.pick(pick_k,pose,pick_width,pick_height,cursor,gaussian_scale=self.gaussian_scale,opacity_scale=self.opacity_multiplier,near_depth=self.near_clip,return_depth=True)
            if hit is not None:index,gaussian_depth=hit
        elif count and not getattr(self.frame,'is_prepared',False):
            centers,covariance,depth=projected_ellipses(self.frame,pick_k,pose,width=pick_width,height=pick_height,near=self.near_clip,gaussian_scale=self.gaussian_scale)
            delta=cursor-centers;xx,xy,yy=covariance[:,0,0],covariance[:,0,1],covariance[:,1,1]
            r2=(yy*delta[:,0]**2-2*xy*delta[:,0]*delta[:,1]+xx*delta[:,1]**2)/np.maximum(xx*yy-xy*xy,1e-24)
            valid=(depth>self.near_clip)&np.isfinite(r2)&(r2<=9)&(np.clip(self.frame.opacity*self.opacity_multiplier,0,.999)*np.exp(-.5*np.maximum(r2,0))>=.1)
            candidates=np.flatnonzero(valid)
            if len(candidates):
                index=int(candidates[np.argmin(depth[candidates])]);gaussian_depth=float(depth[index])
        candidates=[]
        for order,entry in enumerate(self.cameras if self.show_cameras else []):
            if not _get(entry,'visible',True):continue
            segments=self._camera_segments(entry)
            projected=project_segments(segments,np.ones((len(segments),2,4)),pick_k,pose,pick_width,pick_height,near=self.near_clip)
            for hit in camera_edge_hits(projected,cursor,pick_width,pick_height,4*sx):
                candidates.append((entry,order,hit))
        best=None
        if candidates:
            surface=self._renderer.camera_occlusion_depths(pick_k,pose,pick_width,pick_height,[hit[:2] for _,_,hit in candidates],gaussian_scale=self.gaussian_scale,opacity_scale=self.opacity_multiplier,near_depth=self.near_clip) if gpu else np.ones(len(candidates))
            for (entry,order,hit),surface_depth in zip(candidates,surface):
                line_depth=max(0.,1-self.near_clip/hit[2])
                if line_depth>surface_depth+2e-7:continue
                key=(float(np.float32(hit[2])),order)
                if best is None or key<best[0]:best=(key,entry)
        if best is not None and best[0][0]<=gaussian_depth:
            entry=best[1];owner=_get(entry,'object_id',_get(entry,'id'));view=_get(entry,'view',0)
            if self.on_camera_pick:
                import inspect
                try:inspect.signature(self.on_camera_pick).bind(owner,view,extend)
                except (TypeError,ValueError):self.on_camera_pick(owner,view)
                else:self.on_camera_pick(owner,view,extend)
            return None
        if index is not None:
            result=(str(self.frame.object_ids[index]),int(self.frame.ids[index]))
            self.navigation.pivot=self.frame.element(*result).position.astype(np.float64) if getattr(self.frame,'is_prepared',False) else np.array(self.frame.means[index],np.float64)
            if extend:
                if result in self.selected:self.selected.remove(result)
                else:self.selected.add(result)
            else:self.selected={result}
            self._dirty=True;self.queue_render()
            if self.on_pick:self.on_pick(*result,extend)
            return result
        if not extend:
            self.selected.clear();self._dirty=True;self.queue_render()
        if self.on_pick:self.on_pick(None,None,extend)
        return None

    def _camera_entry_selected(self,entry):
        owner=_get(entry,'object_id',_get(entry,'id'))
        return _get(entry,'selected',False) or (owner,None) in self.selected or (owner,_get(entry,'view',0)) in self.selected

    def _camera_segments(self, calibration):
        calibration = _get(calibration, 'calibration', calibration)
        k = np.asarray(_get(calibration, 'K'))
        pose = np.asarray(_get(calibration, 'c2w'))
        width, height = _get(calibration, 'width'), _get(calibration, 'height')
        size=max(.05,min(1.,self._scene_radius()*.08))
        shift = -.5 if _get(calibration, 'pixel_center', 'integer') == 'integer' else 0.
        pixels = np.array([[shift, shift, 1], [width+shift, shift, 1], [width+shift, height+shift, 1], [shift, height+shift, 1]])
        corners = (pixels @ np.linalg.inv(k).T * size) @ pose[:3, :3].T + pose[:3, 3]
        origin = pose[:3, 3]
        return np.asarray([[origin, p] for p in corners] + [[corners[i], corners[(i + 1) % 4]] for i in range(4)])

    def _grid_segments(self):
        up = int(np.argmax(np.abs(self.navigation.up)))
        axes = [i for i in range(3) if i != up]
        step=1.
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
            cameras=[entry for entry in self.cameras if _get(entry,'object_id',_get(entry,'id'))==object_id] if identity is None else []
            if cameras and (not self.show_cameras or not any(_get(entry,'visible',True) for entry in cameras)):continue
            # SceneDocument.trajectories already applies the shared stable
            # identity density policy and unions individually selected paths.
            color = _get(path, 'colors')
            if color is None:
                base = _get(path, 'color', [1., .7, .15])
                color = np.broadcast_to(base, (len(points), 3))
            color = np.asarray(color)
            if identity is None and ((object_id,None) in self.selected or any(self._camera_entry_selected(entry) for entry in cameras)):
                color=np.broadcast_to([1.,.67,.15],(len(points),3))
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
            positive=end>start
            trail,tc,start,end=trail[positive],tc[positive],start[positive],end[positive]
            if not len(trail):continue
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
        from .snapshots import SnapshotPendingError
        if not self.get_realized():
            raise SnapshotPendingError('The viewport has not produced its first visible frame')
        self.make_current()
        if self.get_error() or self.render_error:
            raise RuntimeError(str(self.get_error() or self.render_error))
        if self._renderer is None or not self._renderer.fbo or self._render_pending:
            raise SnapshotPendingError('Wait for the queued viewport frame before saving')
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

    def _poll_gpu_stats(self):
        if not self.get_realized() or self._renderer is None or self.get_error():
            self._gpu_poll_source=0;return GLib.SOURCE_REMOVE
        self.make_current()
        renderer=self._renderer
        changed=renderer.gpu_metrics.poll()
        metrics=renderer.performance_metrics()
        if not renderer.gpu_metrics.supported:
            if self._last_stats is not None:
                self._last_stats.update(metrics)
                if self.on_stats:self.on_stats(dict(self._last_stats))
            self._gpu_poll_source=0;return GLib.SOURCE_REMOVE
        idle=renderer.gpu_metrics.last_result_at is not None and clock.monotonic()-renderer.gpu_metrics.last_result_at>=.5
        if self._last_stats is not None and (changed or idle):
            self._last_stats.update(metrics)
            if self.on_stats:self.on_stats(dict(self._last_stats))
        if renderer.gpu_metrics.pending or (renderer.gpu_metrics.last_result_at is not None and not idle):return GLib.SOURCE_CONTINUE
        self._gpu_poll_source=0;return GLib.SOURCE_REMOVE

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
            transparency=(self.show_paths and self.path_opacity>0 and self.path_opacity<.999 and bool(self.paths)) or (self.show_cameras and any(_get(entry,'visible',True) and not self._camera_entry_selected(entry) for entry in self.cameras))
            renderer.target(render_width,render_height,transparency=transparency)
            renderer.begin(k, pose, render_width, render_height, exposure=self.exposure, exposure_multiplier=self.exposure_multiplier, gaussian_scale=self.gaussian_scale, opacity_scale=self.opacity_multiplier, near_depth=self.near_clip, background={'studio': (.055,.061,.071), 'black': (0., 0., 0.), 'gray': (.18, .18, .18), 'white': (1., 1., 1.)}.get(self.background,(.055,.061,.071)))
            if self.show_grid:
                points, colors = self._grid_segments()
                renderer.lines(project_segments(points, colors, k, pose, render_width, render_height, near=self.near_clip), render_width, render_height, occlude=False)
            # lines() changes the current shader; splats() restores its program.
            renderer.splats()
            edges=_get(self.frame,'edges') if self.frame is not None else None
            if edges is not None and len(edges):
                edge_colors=np.tile([1.,.76,.12,1.],(len(edges),2,1))
                renderer.lines(project_segments(edges,edge_colors,k,pose,render_width,render_height,near=self.near_clip),render_width,render_height,occlude=False)
            # Native pivot is a base-pass annotation before camera/path overlays.
            pivot=pose[:3,:3].T@(self.navigation.pivot-pose[:3,3])
            if self.show_pivot and pivot[2]>self.near_clip:
                center=k@pivot;center=center[:2]/pivot[2]
                radius=9.
                mark=np.tile([0.,0.,0.,1.,.65,.12,1.],(4,1))
                mark[:,:2]=center+np.array([[-radius,0],[radius,0],[0,-radius],[0,radius]])
                renderer.lines(mark,render_width,render_height,occlude=False)
            renderer.splats(depth_only=True)
            overlay=[]
            if self.show_paths or self.show_trails:
                points,colors=self._path_segments()
                overlay.append(project_segments(points,colors,k,pose,render_width,render_height,near=self.near_clip))
            if self.show_cameras:
                for entry in self.cameras:
                    if not _get(entry,'visible',True):continue
                    points=self._camera_segments(entry)
                    selected=self._camera_entry_selected(entry)
                    rgba=[1.,.67,.15,1.] if selected else [.3,.55,.65,.65]
                    colors=np.tile(rgba,(len(points),2,1))
                    overlay.append(project_segments(points,colors,k,pose,render_width,render_height,near=self.near_clip))
            if overlay:renderer.paths(np.concatenate(overlay),render_width,render_height)
            renderer.selection_markers(k,pose,render_width,render_height,gaussian_scale=self.gaussian_scale,opacity_scale=self.opacity_multiplier,near_depth=self.near_clip)
            renderer.finish(destination, width, height)
            self.last_render_warning=None
        except RenderAllocationError as error:
            renderer.abort_frame()
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,destination)
            if renderer.fbo:
                renderer.finish(destination,width,height)
            warning=str(error)
            if warning!=self.last_render_warning and self.on_error:self.on_error(warning)
            self.last_render_warning=warning
            # A reduced-resolution retry can recover; snapshot export must still
            # wait for a completed current frame rather than export stale pixels.
            self._render_pending=True
            return True
        except Exception as error:
            renderer.abort_frame()
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, destination)
            self._report_error(str(error))
        self._render_pending = False
        self._last_stats={'cpu_submission_ms': (clock.perf_counter() - start) * 1000, 'splats': renderer.count, 'render_width': render_width, 'render_height': render_height, 'sort_backend': renderer.sort_backend, 'sort_reused': renderer.last_sort_reused, 'sort_submission_ms': renderer.last_sort_submission_ms, 'sort_reason': renderer.sort_reason, 'graphics_info': self.graphics_info, 'endpoint_upload_bytes': renderer.last_endpoint_upload_bytes, 'endpoint_swapped': renderer.last_endpoint_swapped}
        self._last_stats.update(renderer.performance_metrics())
        if self.on_stats:self.on_stats(dict(self._last_stats))
        if not self._gpu_poll_source and renderer.gpu_metrics.supported:
            self._gpu_poll_source=GLib.timeout_add(16,self._poll_gpu_stats)

        return True
