"""Two image panes sharing one zoom and pan transform; metrics use raw pixels."""
import math
import cairo
import gi
import numpy as np

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk
from gsv.metrics import convert_rgb
from .i18n import tr


class ComparisonCanvas(Gtk.DrawingArea):
    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.images = []
        self.zoom = 1.0
        self.pan = (0.0, 0.0)
        self._pointer = None
        self.set_draw_func(self._draw)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _,x,y: setattr(self, "_pointer", (x,y)))
        self.add_controller(motion)
        zoom = Gtk.GestureZoom()
        zoom.connect("begin", lambda gesture, _: setattr(self, "_zoom_last", 1.))
        zoom.connect("scale-changed", self._pinch)
        self.add_controller(zoom)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda *_: setattr(self, "_start_pan", self.pan))
        drag.connect("drag-update", self._drag)
        self.add_controller(drag)
        self.update_property([Gtk.AccessibleProperty.LABEL], [tr("ubuntu.renderAccessibility")])

    def set_images(self, render, reference, color_space):
        self.images = []
        for image in [render, reference]:
            if image is None:
                continue
            if color_space == "linear-srgb":
                image = convert_rgb(image, source=color_space, target="srgb")
            rgb = (np.clip(image, 0, 1) * 255 + .5).astype(np.uint8)
            pixels = np.empty((*rgb.shape[:2], 4), np.uint8)
            pixels[:, :, :3] = rgb[:, :, ::-1]
            pixels[:, :, 3] = 255
            self.images.append((cairo.ImageSurface.create_for_data(pixels, cairo.FORMAT_RGB24, rgb.shape[1], rgb.shape[0]), pixels))
        self.queue_draw()

    def reset(self):
        self.zoom, self.pan = 1.0, (0.0, 0.0)
        self.queue_draw()

    def actual_pixels(self):
        if not self.images:
            return
        surface = self.images[0][0]
        fit = min((self.get_width()/len(self.images)-24)/surface.get_width(), (self.get_height()-40)/surface.get_height())
        self.zoom = 1/max(.001, fit*self.get_scale_factor())
        self.pan = (0.0, 0.0)
        self.queue_draw()

    def snapshot_surface(self):
        if not self.images:
            raise ValueError("No completed calibrated image to save")
        width = sum(surface.get_width() for surface, _ in self.images)
        height = max(surface.get_height() for surface, _ in self.images)
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
        cr = cairo.Context(surface)
        offset = 0
        for image, _ in self.images:
            cr.set_source_surface(image, offset, 0)
            cr.paint()
            offset += image.get_width()
        return surface

    def clear(self):
        self.images = []
        self.queue_draw()

    def _change_zoom(self, factor):
        previous = self.zoom
        self.zoom = min(32, max(.25, previous * factor))
        width = max(1, self.get_width()) / max(1, len(self.images))
        x,y = self._pointer or (width/2, self.get_height()/2)
        anchor = (x % width - width/2, y - 28 - (self.get_height()-28)/2)
        ratio = self.zoom/previous
        self.pan = tuple(a-(a-p)*ratio for a,p in zip(anchor, self.pan))
        self.queue_draw()

    def _pinch(self, gesture, scale):
        previous = getattr(self, "_zoom_last", 1.)
        self._change_zoom(max(1e-6, scale)/previous)
        self._zoom_last = max(1e-6, scale)

    def _scroll(self, controller, dx, dy):
        if controller.get_current_event_state() & Gdk.ModifierType.ALT_MASK:
            self._change_zoom(math.exp(max(-2, min(2, -dy*.14))))
        else:
            self.pan = (self.pan[0]-dx*16, self.pan[1]-dy*16)
            self.queue_draw()
        return True

    def _drag(self, gesture, dx, dy):
        self.pan = (self._start_pan[0]+dx, self._start_pan[1]+dy)
        self.queue_draw()

    def _draw(self, widget, cr, width, height):
        cr.set_source_rgb(.05, .06, .075)
        cr.paint()
        count = max(1, len(self.images))
        pane_width = width/count
        for i, (surface, pixels) in enumerate(self.images):
            w, h = surface.get_width(), surface.get_height()
            scale = min((pane_width-24)/w, (height-40)/h)*self.zoom
            cr.save()
            cr.rectangle(i*pane_width, 28, pane_width, height-28)
            cr.clip()
            cr.translate(i*pane_width+(pane_width-w*scale)/2+self.pan[0], 28+(height-28-h*scale)/2+self.pan[1])
            cr.scale(scale, scale)
            cr.set_source_surface(surface)
            cr.get_source().set_filter(cairo.FILTER_NEAREST if self.zoom > 3 else cairo.FILTER_BILINEAR)
            cr.paint()
            cr.restore()
        cr.set_source_rgb(.7, .74, .8)
        cr.set_font_size(12)
        cr.move_to(12, 19)
        cr.show_text(tr("ubuntu.renderLabel"))
        if len(self.images) == 2:
            cr.move_to(pane_width+12, 19)
            cr.show_text(tr("ubuntu.referenceLabel"))
            cr.set_source_rgba(.5, .5, .5, .4)
            cr.rectangle(pane_width, 0, 1, height)
            cr.fill()
