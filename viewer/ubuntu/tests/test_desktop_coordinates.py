"""Offline desktop coordinate contracts; no X server, EI session or input."""
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / 'tools/desktop_coordinates.py'
spec = importlib.util.spec_from_file_location('desktop_coordinate_contract', path)
coordinates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coordinates)


def rectangle(x, y, width, height):
    return SimpleNamespace(x=x, y=y, width=width, height=height)


class Node:
    def __init__(self, role, name, bounds, parent=None, pid=4242, expanded=False):
        self.role, self.name, self.bounds, self.parent = role, name, bounds, parent
        self.pid, self.expanded = pid, expanded

    def get_process_id(self):
        return self.pid

    def clear_cache(self):
        pass

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_parent(self):
        return self.parent

    def get_state_set(self):
        states = coordinates.Atspi.StateType
        return SimpleNamespace(contains=lambda state: state in (states.SHOWING, states.SENSITIVE)
                               or self.expanded and state == states.EXPANDED)


class EISMetadata:
    def __init__(self, size=(3420, 2148)):
        base = dict(device_id=10, device_generation=1, region_index=0,
                    x=0, y=0, width=size[0], height=size[1], mapping_id=None,
                    physical_scale=2., resumed=True, emulating=True)
        self.records = [base, dict(base, device_id=20, device_generation=2)]
        self.pinned = None

    def absolute_regions(self):
        return deepcopy(self.records)

    def pin_absolute_region(self, region):
        self.pinned = deepcopy(region)


class XMetadata:
    root, display, failed = 1, None, False

    def __init__(self):
        self.root_geometry = (0, 0, 3420, 2148)
        self.windows = {42: dict(pid=4242, name='Owned fixture', geometry=(132,64,3288,2084))}
        self.workarea = [132,64,3288,2084]*2

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def geometry(self, window):
        return self.root_geometry if window == self.root else self.windows[window]['geometry']

    def sync(self, *_):
        pass

    def property(self, window, name, **_):
        if name in ('_NET_CLIENT_LIST_STACKING', '_NET_CLIENT_LIST'):
            return list(self.windows)
        if name == '_NET_WM_PID':
            return [self.windows[window]['pid']]
        if name == '_NET_WM_NAME':
            return self.windows[window]['name'].encode()
        if name == '_NET_WORKAREA':
            return self.workarea
        if name == '_NET_CURRENT_DESKTOP':
            return None
        raise AssertionError('Unexpected metadata read: '+name)


class DesktopCoordinatesTests(unittest.TestCase):
    def setUp(self):
        self.state = [106,
            [[['Meta-0','MetaVendor','Virtual remote monitor','0x33'],
              [['3420x2148@60',3420,2148,60.,1.,[1.,2.],{'is-current':True}]],{}]],
            [[0,0,2.,0,True,[['Meta-0','MetaVendor','Virtual remote monitor','0x33']],{}]],
            {'layout-mode':2,'legacy-ui-scaling-factor':2}]
        self.eis, self.x = EISMetadata(), XMetadata()
        self.desktop = SimpleNamespace(eis=self.eis, virtual_size=None, backend='x11', pid=4242,
            owner_pid=lambda:4242, pointer_stream=None, pointer_mapping_id=None, maximized=True,
            call=lambda *args:SimpleNamespace(unpack=lambda:deepcopy(self.state)))
        self.frame = Node('frame','Owned fixture',rectangle(0,0,1644,1042))
        self.button = Node('push button','Control',rectangle(100,100,60,20),self.frame)
        atspi = coordinates.Atspi
        self.context = patch.multiple(coordinates, _X11=lambda:self.x,
            Atspi=SimpleNamespace(StateType=atspi.StateType, CoordType=atspi.CoordType,
                Component=SimpleNamespace(get_extents=lambda node,kind:node.bounds)))
        self.context.start()
        self.addCleanup(self.context.stop)

    def configure(self):
        return coordinates.configure_existing_output(self.desktop)

    def test_real_hidpi_metadata_keeps_xroot_and_eis_one_to_one(self):
        result = self.configure()
        self.assertEqual(result['xroot_to_eis_scale'],[1.,1.])
        self.assertEqual(result['equivalent_absolute_devices'],2)
        self.assertEqual(self.eis.pinned['device_id'],20)
        self.assertEqual(self.desktop.virtual_size,(3420,2148))
        self.assertEqual(self.desktop.pointer_stream,'eis-existing-output')
        self.assertIsNone(self.desktop.pointer_mapping_id)
        self.assertFalse(result['temporary_output_created'])
        self.assertEqual(coordinates.owned_target_point(self.desktop,self.button),(392.,284.))

    def test_fractions_use_same_verified_hidpi_conversion(self):
        self.configure()
        self.assertEqual(coordinates.owned_target_point(self.desktop,self.button,
                         x_fraction=.75,y_fraction=.25),(422.,274.))
        self.assertEqual(coordinates.owned_target_point(self.desktop,self.button,
                         x_fraction=1.,y_fraction=1.),(451.,303.))
        for value in (-.01,1.01,float('nan'),float('inf')):
            self.assertIsNone(coordinates.owned_target_point(self.desktop,self.button,x_fraction=value))

    def test_physical_atspi_bounds_are_not_scaled_twice(self):
        self.configure()
        self.frame.bounds = rectangle(0,0,3288,2084)
        self.button.bounds = rectangle(200,200,120,40)
        self.assertEqual(coordinates.owned_target_point(self.desktop,self.button),(392.,284.))

    def test_published_logical_region_is_converted_from_measured_extents(self):
        self.eis.records = EISMetadata((1710,1074)).records
        result = self.configure()
        self.assertEqual(result['xroot_to_eis_scale'],[.5,.5])
        self.assertEqual(coordinates.owned_target_point(self.desktop,self.button),(196.,142.))

    def test_foreign_or_ambiguous_windows_and_asymmetric_scale_are_rejected(self):
        self.configure()
        self.button.pid = 9000
        self.assertIsNone(coordinates.owned_target_point(self.desktop,self.button))
        self.button.pid = 4242
        self.x.windows[43] = deepcopy(self.x.windows[42])
        self.assertIsNone(coordinates.owned_target_point(self.desktop,self.button))
        del self.x.windows[43]
        self.x.windows[42]['geometry'] = (132,64,3288,1042)
        self.assertIsNone(coordinates.owned_target_point(self.desktop,self.button))

    def test_region_lifetime_or_output_changes_invalidate_mapping(self):
        self.configure()
        self.eis.records[-1]['device_generation'] = 3
        self.assertIsNone(coordinates.owned_target_point(self.desktop,self.button))
        self.eis.records[-1]['device_generation'] = 2
        self.state[1][0][0][-1] = 'different-output'
        self.assertIsNone(coordinates.owned_target_point(self.desktop,self.button))

    def test_multiple_regions_outputs_or_unknown_xroot_refuse_configuration(self):
        self.eis.records[-1]['x'] = 100
        with self.assertRaises(ValueError):self.configure()
        self.assertIsNone(self.eis.pinned)
        self.eis.records[-1]['x'] = 0
        self.state[2].append(deepcopy(self.state[2][0]))
        with self.assertRaises(ValueError):self.configure()
        self.state[2].pop()
        self.x.root_geometry = (0,0,3400,2148)
        with self.assertRaises(ValueError):self.configure()
        self.assertIsNone(self.desktop.virtual_size)

    def test_popup_with_unverified_native_origin_is_rejected(self):
        self.configure()
        combo = Node('combo box','Option',rectangle(10,10,100,40),self.frame,expanded=True)
        option = Node('list item','Choice',rectangle(10,10,100,30),combo)
        self.assertIsNone(coordinates.owned_target_point(self.desktop,option))

    def test_wayland_requires_maximized_matching_real_workarea(self):
        self.configure()
        self.desktop.backend='wayland'
        self.assertEqual(coordinates.owned_target_point(self.desktop,self.button),(392.,284.))
        self.desktop.maximized=False
        self.assertIsNone(coordinates.owned_target_point(self.desktop,self.button))

    def test_legacy_single_scale_virtual_mapping_retains_original_coordinates(self):
        self.desktop.eis = None
        self.desktop.virtual_size = (1920,1080)
        self.state[1][0][1][0][1:3] = [1920,1080]
        self.state[2][0][2] = 1.
        self.state[3]['legacy-ui-scaling-factor'] = 1
        self.x.root_geometry = (0,0,1920,1080)
        self.x.windows[42]['geometry'] = (55,45,1500,900)
        self.frame.bounds = rectangle(0,0,1500,900)
        self.button.bounds = rectangle(50,50,20,20)
        self.assertEqual(coordinates.owned_target_point(self.desktop,self.button),(115.,105.))


if __name__ == '__main__':
    unittest.main()
