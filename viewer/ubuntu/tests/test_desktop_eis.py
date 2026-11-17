"""Read-only sender selection tests: no desktop, sockets or input connection."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from desktop_eis import ABSOLUTE, BUTTON, SCROLL, EISError, EISInput, _Device


class EISRegionTests(unittest.TestCase):
    def sender(self, regions):
        sender = EISInput.__new__(EISInput)
        sender.devices = {pointer: _Device(pointer, frozenset((ABSOLUTE, BUTTON, SCROLL)),
            f'device {pointer}', resumed=True, emulating=True) for pointer in regions}
        sender._pointer_device = None
        sender._absolute_pin = None
        sender.dispatch = mock.Mock()
        sender._frame = mock.Mock()
        sender.diagnostics = mock.Mock(return_value={'devices': 'live-test-regions'})
        sender.api = SimpleNamespace(
            device_get_region_at=lambda pointer, x, y: pointer if (
                regions[pointer][1] <= x < regions[pointer][1] + regions[pointer][3]
                and regions[pointer][2] <= y < regions[pointer][2] + regions[pointer][4]) else None,
            region_get_mapping_id=lambda pointer: regions[pointer][0],
            region_get_x=lambda pointer: regions[pointer][1],
            region_get_y=lambda pointer: regions[pointer][2],
            region_get_width=lambda pointer: regions[pointer][3],
            region_get_height=lambda pointer: regions[pointer][4],
            region_get_physical_scale=lambda pointer: regions[pointer][5] if len(regions[pointer]) > 5 else 1.,
            device_get_region=lambda pointer, index: pointer if index == 0 else None,
            device_pointer_motion_absolute=mock.Mock())
        return sender

    def test_explicit_mapping_collapses_only_equivalent_mutter_duplicates(self):
        sender = self.sender({10: (b'stream', 0, 0, 1920, 1080), 20: (b'stream', 0, 0, 1920, 1080)})
        sender.absolute(94, 60, mapping_id='stream', timeout=0)
        sender.api.device_pointer_motion_absolute.assert_called_once_with(20, 94., 60.)
        sender._pointer_device = 10
        sender.absolute(94, 60, mapping_id='stream', timeout=0)
        self.assertEqual(sender.api.device_pointer_motion_absolute.call_args.args[0], 10)

    def test_anonymous_or_different_geometry_remains_ambiguous(self):
        for regions, mapping in [
            ({10: (None, 0, 0, 1920, 1080), 20: (None, 0, 0, 1920, 1080)}, None),
            ({10: (b'stream', 0, 0, 1920, 1080), 20: (b'stream', 0, 0, 1000, 800)}, 'stream'),
            ({10: (b'first', 0, 0, 1920, 1080), 20: (b'second', 0, 0, 1920, 1080)}, None),
        ]:
            with self.subTest(regions=regions):
                sender = self.sender(regions)
                with self.assertRaisesRegex(EISError, 'candidate_count=2.*live-test-regions'):
                    sender.absolute(94, 60, mapping_id=mapping, timeout=0)
                sender.api.device_pointer_motion_absolute.assert_not_called()

    def test_mapping_filters_devices_and_rejects_unknown_or_outside(self):
        sender = self.sender({10: (b'first', 0, 0, 1920, 1080), 20: (b'second', 0, 0, 1920, 1080)})
        sender.absolute(94, 60, mapping_id='first', timeout=0)
        self.assertEqual(sender.api.device_pointer_motion_absolute.call_args.args[0], 10)
        for x, y, mapping in [(94, 60, 'unknown'), (1920, 60, 'first'), (-1, 60, 'first')]:
            with self.assertRaisesRegex(EISError, 'candidate_count=0'):
                sender.absolute(x, y, mapping_id=mapping, timeout=0)

    def test_waits_for_device_resume_without_using_paused_target(self):
        sender = self.sender({10: (b'stream', 0, 0, 1920, 1080)})
        sender.devices[10].resumed = False
        calls = []
        def dispatch(timeout=0):
            calls.append(timeout)
            if len(calls) == 3:
                sender.devices[10].resumed = True
        sender.dispatch.side_effect = dispatch
        sender.absolute(94, 60, mapping_id='stream')
        self.assertEqual(len(calls), 3)
        self.assertGreater(calls[1], 0)
        sender.api.device_pointer_motion_absolute.assert_called_once_with(10, 94., 60.)

    def test_reconfiguration_timeout_never_exceeds_two_seconds(self):
        sender = self.sender({10: (b'stream', 0, 0, 1920, 1080)})
        sender.devices[10].resumed = False
        with mock.patch('desktop_eis.time.monotonic', side_effect=(10., 10.5, 12.01)):
            with self.assertRaisesRegex(EISError, 'candidate_count=0.*live-test-regions'):
                sender.absolute(94, 60, mapping_id='stream', timeout=100.)
        sender.api.device_pointer_motion_absolute.assert_not_called()

    def test_public_regions_and_explicit_pin_are_read_only_and_keep_scale(self):
        sender = self.sender({10: (None, 0, 0, 3420, 2148, 2.), 20: (None, 0, 0, 3420, 2148, 2.)})
        regions = sender.absolute_regions()
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]['physical_scale'], 2.)
        self.assertEqual(regions[0]['mapping_id'], None)
        sender.pin_absolute_region(regions[0])
        sender.api.device_pointer_motion_absolute.assert_not_called()
        sender._frame.assert_not_called()
        sender.absolute(94, 60, timeout=0)
        sender.api.device_pointer_motion_absolute.assert_called_once_with(10, 94., 60.)
        sender.pin_absolute_region(None)
        with self.assertRaisesRegex(EISError, 'candidate_count=2'):
            sender.absolute(94, 60, timeout=0)

    def test_pin_rejects_forged_metadata_and_reused_device_lifetime(self):
        sender = self.sender({10: (None, 0, 0, 3420, 2148, 2.), 20: (None, 0, 0, 3420, 2148, 2.)})
        region = sender.absolute_regions()[0]
        with self.assertRaisesRegex(EISError, 'stale, changed or unready'):
            sender.pin_absolute_region(dict(region, width=1920))
        sender.pin_absolute_region(region)
        sender.devices[10].generation += 1
        with self.assertRaisesRegex(EISError, 'candidate_count=0'):
            sender.absolute(94, 60, timeout=0)
        sender.api.device_pointer_motion_absolute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
