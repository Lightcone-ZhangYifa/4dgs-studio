"""Baseline-aligned native text for Cairo drawing areas.

Pango supplies shaping and system font fallback, including CJK glyphs. Layouts
own an independent Pango context so drawing never mutates the widget's shared
Pango context. Font sizes are logical pixels, matching drawing-area geometry.
"""
from __future__ import annotations

import gi

gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
gi.require_foreign('cairo')
from gi.repository import Pango, PangoCairo


def text_layout(widget, cr, value, font_size):
    source = widget.get_pango_context()
    layout = PangoCairo.create_layout(cr)
    context = layout.get_context()
    context.set_font_map(source.get_font_map())
    context.set_language(source.get_language())
    context.set_base_dir(source.get_base_dir())
    description = source.get_font_description()
    description = description.copy() if description is not None else Pango.FontDescription()
    description.set_absolute_size(font_size * Pango.SCALE)
    layout.set_font_description(description)
    layout.set_text(str(value), -1)
    return layout


def draw_text(widget, cr, x, baseline, value, font_size):
    """Draw at the existing Cairo baseline, preserving transform and clip."""
    cr.save()
    try:
        layout = text_layout(widget, cr, value, font_size)
        cr.move_to(x, baseline - layout.get_baseline() / Pango.SCALE)
        PangoCairo.show_layout(cr, layout)
        return layout
    finally:
        cr.restore()
