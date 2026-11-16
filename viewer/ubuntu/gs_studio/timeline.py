"""Native-time timeline with source ticks, key dragging, zoom and pan."""
from __future__ import annotations

import math
import gi
import numpy as np

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk
from .i18n import tr


def fraction(value, lower, upper):
    """Unclamped affine fraction without overflowing a valid finite clock."""
    span, offset = upper - lower, value - lower
    if math.isfinite(span) and math.isfinite(offset):
        return offset / span
    return (value * .5 - lower * .5) / (upper * .5 - lower * .5)


def interpolate(lower, upper, weight):
    """Stable affine interpolation, including extrapolation while panning."""
    if weight == 0:
        return lower
    if weight == 1:
        return upper
    span = upper - lower
    if math.isfinite(span):
        value = lower + weight * span
        if math.isfinite(value):
            return value
    return ((1 - weight) * (lower * .5) + weight * (upper * .5)) * 2


class Timeline(Gtk.DrawingArea):
    def __init__(self):
        super().__init__(content_height=108, hexpand=True, focusable=True)
        self.times = np.array([0.0])
        self.time = 0.0
        self.keys = []
        self.selected_key = None
        self.lower, self.upper = -0.5, 0.5
        self.snap = True
        self.unit = "second"
        self.on_seek = lambda time: None
        self.on_play = lambda: None
        self.preview_range = None
        self._snap_bypass = False
        self._pointer = None
        self._cancelled = False
        self.on_key_select = lambda index: None
        self.on_key_move = lambda index, time: None
        self._moving = None
        self._ghost = None
        self._start_x = 0
        self.set_draw_func(self._draw)
        self.set_tooltip_text(tr("timeline.canvas", "Timeline. Click to scrub; drag a camera keyframe to retime."))
        self.update_property([Gtk.AccessibleProperty.LABEL], [tr("timeline.canvas")])
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        pan = Gtk.GestureDrag(button=2)
        pan.connect("drag-begin", lambda *_: setattr(self, "_pan_range", (self.lower, self.upper)))
        pan.connect("drag-update", self._pan)
        self.add_controller(pan)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _,x,y: setattr(self, "_pointer", (x,y)))
        self.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        zoom = Gtk.GestureZoom()
        zoom.connect("begin", lambda *_: setattr(self, "_last_zoom", 1.))
        zoom.connect("scale-changed", self._pinch)
        self.add_controller(zoom)

    def fit(self):
        lo, hi = float(self.times[0]), float(self.times[-1])
        if lo == hi:
            delta = max(1., math.ulp(lo) * 8)
            a, b = lo - delta * .5, hi + delta * .5
            lo, hi = (a if math.isfinite(a) else lo), (b if math.isfinite(b) else hi)
        if not (math.isfinite(lo) and math.isfinite(hi) and hi > lo):
            raise ValueError("Timeline requires finite, increasing native time")
        a, b = interpolate(lo, hi, -.025), interpolate(lo, hi, 1.025)
        if math.isfinite(a):
            lo = a
        if math.isfinite(b):
            hi = b
        self.lower, self.upper = lo, hi
        self.queue_draw()

    def _x(self, value):
        return 112 + fraction(value, self.lower, self.upper) * max(1, self.get_width() - 128)

    def _time(self, x):
        weight = min(1., max(0., (x - 112) / max(1, self.get_width() - 128)))
        value = interpolate(self.lower, self.upper, weight)
        value = min(float(self.times[-1]), max(float(self.times[0]), value))
        if self.snap and not self._snap_bypass:
            i = int(np.searchsorted(self.times, value))
            nearest = min((self.times[max(0, i-1)], self.times[min(i, len(self.times)-1)]), key=lambda t: abs(self._x(float(t))-x))
            if abs(self._x(nearest) - x) <= 7:
                value = float(nearest)
        return value

    def _begin(self, gesture, x, y):
        self.grab_focus()
        self._cancelled = False
        self._snap_bypass = bool(gesture.get_current_event_state() & Gdk.ModifierType.ALT_MASK)
        self._start_x = x
        self._moving = None
        if y > 68:
            for i, key in enumerate(self.keys):
                if abs(self._x(key) - x) <= 8:
                    self._moving = i
                    self.selected_key = i
                    self._ghost = key
                    self.on_key_select(i)
                    break
        if self._moving is None:
            self.on_seek(self._time(x))

    def _update(self, gesture, dx, dy):
        if self._cancelled:
            return
        self._snap_bypass = bool(gesture.get_current_event_state() & Gdk.ModifierType.ALT_MASK)
        value = self._time(self._start_x + dx)
        if self._moving is not None:
            self._ghost = value
            self.queue_draw()
        else:
            self.on_seek(value)

    def _end(self, gesture, dx, dy):
        if not self._cancelled and self._moving is not None and abs(dx) > 2:
            self.on_key_move(self._moving, self._ghost)
        self._moving = self._ghost = None
        self._snap_bypass = False
        self.queue_draw()

    def _pan(self, gesture, dx, dy):
        lo, hi = self._pan_range
        shift = dx / max(1, self.get_width()-128)
        self._assign(interpolate(lo, hi, -shift), interpolate(lo, hi, 1-shift))

    def _assign(self, lo, hi):
        if math.isfinite(lo) and math.isfinite(hi) and hi > lo:
            self.lower, self.upper = lo, hi
            self.queue_draw()

    def _zoom_at_pointer(self, factor):
        x = self._pointer[0] if self._pointer else self.get_width()/2
        weight = min(1., max(0., (x-112)/max(1, self.get_width()-128)))
        self._assign(interpolate(self.lower,self.upper,weight*(1-factor)),
                     interpolate(self.lower,self.upper,weight+(1-weight)*factor))

    def _scroll(self, controller, dx, dy):
        self._zoom_at_pointer(math.exp(max(-2,min(2,dy*.16))))
        return True

    def _pinch(self, gesture, scale):
        previous = getattr(self, "_last_zoom", 1.)
        self._zoom_at_pointer(previous/max(scale,1e-6))
        self._last_zoom = max(scale,1e-6)

    def _key(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self._cancelled = True
            self._moving = self._ghost = None
            self.queue_draw()
        elif keyval == Gdk.KEY_Home:
            self.fit()
        elif keyval == Gdk.KEY_space:
            self.on_play()
        elif keyval in (Gdk.KEY_Left,Gdk.KEY_Right):
            direction = 1 if keyval == Gdk.KEY_Right else -1
            index = np.searchsorted(self.times,self.time,side="right" if direction>0 else "left") + (0 if direction>0 else -1)
            self.on_seek(float(self.times[max(0,min(int(index),len(self.times)-1))]))
        else:
            return False
        return True

    def _draw(self, widget, cr, width, height):
        cr.set_source_rgb(.07, .08, .10)
        cr.paint()
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(10)
        def text(x, y, value):
            cr.set_source_rgb(.66, .69, .73)
            cr.move_to(x, y)
            cr.show_text(str(value))
        text(12, 22, tr("timeline.time", "TIME") + " · " + tr("unit."+self.unit, self.unit))
        text(12, 52, tr("timeline.source", "Source samples"))
        text(12, 84, tr("timeline.cameraKeys", "Camera keys"))
        cr.save()
        cr.rectangle(110, 0, max(1, width-110), height)
        cr.clip()
        if self.preview_range:
            a,b = self.preview_range
            x1,x2 = max(110,self._x(a)),min(width,self._x(b))
            if x2>x1:
                cr.set_source_rgba(.25,.55,1.,.12)
                cr.rectangle(x1,25,x2-x1,height-25)
                cr.fill()
        for i in range(7):
            t = interpolate(self.lower, self.upper, i/6)
            x = self._x(t)
            cr.set_source_rgba(.6, .65, .7, .15)
            cr.rectangle(x, 26, 1, height-26)
            cr.fill()
            text(x+3, 20, f"{t:.7g}")
        with np.errstate(over='ignore', invalid='ignore'):
            span = self.upper-self.lower
            if math.isfinite(span):
                ratios = (self.times-self.lower)/span
                fallback = ((self.times*.5-self.lower*.5)/(self.upper*.5-self.lower*.5))
                ratios = np.where(np.isfinite(ratios), ratios, fallback)
            else:
                ratios = ((self.times*.5-self.lower*.5)/(self.upper*.5-self.lower*.5))
            ticks = np.unique(np.clip(112+ratios*max(1,width-128), -1000, width+1000).astype(int))
        cr.set_source_rgba(.25, .8, .8, .8)
        for x in ticks:
            if 110 <= x <= width:
                cr.rectangle(int(x), 40, 1, 13)
        cr.fill()
        for i, t in enumerate(self.keys):
            x = self._x(self._ghost if self._moving == i else t)
            if not math.isfinite(x) or x < 100 or x > width+12:
                continue
            cr.set_source_rgb(*((1, .65, .22) if self.selected_key == i else (.3, .65, 1)))
            cr.move_to(x, 71)
            cr.line_to(x+6, 78)
            cr.line_to(x, 85)
            cr.line_to(x-6, 78)
            cr.close_path()
            cr.fill()
        x = self._x(self.time)
        if math.isfinite(x) and 100 <= x <= width+12:
            cr.set_source_rgb(1, .66, .2)
            cr.rectangle(x, 25, 1.5, height-25)
            cr.fill()
        cr.restore()
