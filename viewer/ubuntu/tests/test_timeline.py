"""Native-clock interaction math, including finite ranges wider than float64."""
import math
import sys
import unittest

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
import numpy as np

from gs_studio.timeline import Timeline, fraction, interpolate


class NativeTimeMathTests(unittest.TestCase):
    def test_extreme_fraction_and_interpolation(self):
        self.assertEqual(fraction(0, -1e308, 1e308), .5)
        self.assertEqual(interpolate(-1e308, 1e308, .5), 0)
        self.assertAlmostEqual(fraction(interpolate(-1e308, 1e308, .75), -1e308, 1e308), .75)
        self.assertTrue(math.isfinite(interpolate(1e308, 1.1e308, 2)))

    def test_adjacent_and_subnormal_times(self):
        lower = 1e16
        upper = math.nextafter(lower, math.inf)
        self.assertEqual(fraction(lower, lower, upper), 0)
        self.assertEqual(fraction(upper, lower, upper), 1)
        tiny = math.nextafter(0, 1)
        self.assertEqual(fraction(0, 0, tiny), 0)
        self.assertEqual(fraction(tiny, 0, tiny), 1)


@unittest.skipUnless(Gtk.init_check(), 'Requires a display (run with xvfb-run)')
class TimelineInteractionTests(unittest.TestCase):
    def setUp(self):
        self.timeline = Timeline()

    def test_fit_extreme_time_and_scrub_exact_endpoints(self):
        t = self.timeline
        t.times = np.array([-1e308, 0., 1e308])
        t.fit()
        t.snap = False
        self.assertTrue(math.isfinite(t.lower) and math.isfinite(t.upper))
        # Unallocated widgets have one pixel of interaction range.
        self.assertEqual(t._time(t._x(-1e308)), -1e308)
        self.assertEqual(t._time(t._x(1e308)), 1e308)
        self.assertAlmostEqual(fraction(t._time(t._x(0)), -1e308, 1e308), .5)
        t._scroll(None, 0, -1)
        self.assertTrue(math.isfinite(t.lower) and math.isfinite(t.upper))
        t._pan_range = t.lower, t.upper
        t._pan(None, .01, 0)
        self.assertTrue(t.upper > t.lower)

    def test_single_time_at_float_max_still_has_a_finite_window(self):
        self.timeline.times = np.array([sys.float_info.max])
        self.timeline.fit()
        self.assertTrue(math.isfinite(self.timeline.lower))
        self.assertTrue(math.isfinite(self.timeline.upper))
        self.assertGreater(self.timeline.upper, self.timeline.lower)
        self.assertEqual(self.timeline._time(self.timeline._x(sys.float_info.max)), sys.float_info.max)


if __name__ == '__main__':
    unittest.main()
