"""Native fallback/shaping, baseline and clipping for Cairo widget captions."""
import os
import sys
import unittest

import cairo
import gi
import numpy as np

gi.require_version('Gtk', '4.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, Pango
from gs_studio.drawing_text import draw_text, text_layout
from gs_studio.i18n import Translator


@unittest.skipUnless(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'),
                     'Widget Pango font contexts require a display; use xvfb-run')
class DrawingTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Gtk.init()

    def setUp(self):
        self.widget = Gtk.DrawingArea()

    def test_native_cjk_and_english_have_no_unknown_glyphs_and_preserve_widget_context(self):
        source = self.widget.get_pango_context()
        original = source.get_font_description().to_string()
        matrix_values = lambda value: (None if value is None else
                                      (value.xx, value.xy, value.yx, value.yy, value.x0, value.y0))
        matrix = matrix_values(source.get_matrix())
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 600, 60)
        cr = cairo.Context(surface)
        chinese = Translator('zh-Hans')
        for value, size in ((chinese('timeline.time') + ' · ' + chinese('unit.second'), 10),
                            (chinese('timeline.source'), 10), (chinese('timeline.cameraKeys'), 10),
                            (chinese('ubuntu.renderLabel'), 12), (chinese('role.ground_truth'), 12),
                            (chinese('role.reference'), 12), (chinese('role.prediction'), 12),
                            ('Source samples · -1.25e-3 / Camera keys', 10)):
            layout = text_layout(self.widget, cr, value, size)
            self.assertEqual(layout.get_unknown_glyphs_count(), 0, value)
            self.assertIsNot(layout.get_context(), source)
            self.assertEqual(layout.get_font_description().get_size(), size * Pango.SCALE)
            self.assertTrue(layout.get_font_description().get_size_is_absolute())
        self.assertEqual(source.get_font_description().to_string(), original)
        self.assertEqual(matrix_values(source.get_matrix()), matrix)

    def test_inherits_widget_font_family_style_and_weight(self):
        context = self.widget.get_pango_context()
        description = Pango.FontDescription.from_string('Serif Bold Italic 14')
        context.set_font_description(description)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 40)
        layout = text_layout(self.widget, cairo.Context(surface), '中文 English 12.5', 12)
        result = layout.get_font_description()
        self.assertEqual(result.get_family(), description.get_family())
        self.assertEqual(result.get_style(), description.get_style())
        self.assertEqual(result.get_weight(), description.get_weight())
        self.assertEqual(context.get_font_description().to_string(), description.to_string())
        self.assertEqual(layout.get_unknown_glyphs_count(), 0)

    def test_scaled_baseline_and_existing_clip_contain_visible_glyphs(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                width, height = 160, 40
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width * scale, height * scale)
                surface.set_device_scale(scale, scale)
                cr = cairo.Context(surface)
                cr.rectangle(8, 0, 36, 28)
                cr.clip()
                before_clip = cr.clip_extents()
                cr.set_source_rgba(1, 1, 1, 1)
                layout = draw_text(self.widget, cr, 8, 19, '渲染 / Reference 1.25', 12)
                self.assertEqual(layout.get_unknown_glyphs_count(), 0)
                self.assertEqual(cr.clip_extents(), before_clip)
                ink, _ = layout.get_pixel_extents()
                top = 19 - layout.get_baseline() / Pango.SCALE + ink.y
                self.assertGreaterEqual(top, 0)
                self.assertLessEqual(top + ink.height, 28)
                surface.flush()
                pixels = np.ndarray((height * scale, surface.get_stride() // 4, 4), np.uint8,
                                    buffer=surface.get_data())[:, :width * scale]
                alpha = pixels[:, :, 3 if sys.byteorder == 'little' else 0]
                self.assertTrue(np.any(alpha))
                self.assertFalse(np.any(alpha[:, :8 * scale]))
                self.assertFalse(np.any(alpha[:, 44 * scale:]))
                self.assertFalse(np.any(alpha[28 * scale:]))


if __name__ == '__main__':
    unittest.main()
