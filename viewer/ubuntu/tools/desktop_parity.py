"""Additional acceptance through the real application's public accessible UI.

Imported only by the explicitly opted-in GNOME desktop runner. No application
objects or widget internals are imported; exports provide independent evidence.
"""
from __future__ import annotations

import hashlib
import math
import struct
import time

import numpy as np
from gi.repository import Atspi, GLib

from test_desktop_session import action, pump, text, wait_for, walk


def showing(node):
    return node.get_state_set().contains(Atspi.StateType.SHOWING)


def named(desktop, name, interface=None):
    def find():
        matches = []
        for node in desktop.app_nodes():
            if node.get_name() != name and node.get_description() != name:
                continue
            if not showing(node):
                try:
                    Atspi.Component.scroll_to(node, Atspi.ScrollType.ANYWHERE)
                    pump(.05)
                    node.clear_cache()
                except GLib.Error:
                    pass
                if not showing(node):
                    continue
            if interface is None or interface in node.get_interfaces():
                matches.append(node)
                continue
            match = next((child for child in walk(node) if interface in child.get_interfaces() and showing(child)), None)
            if match:
                matches.append(match)
        controls = ('text','combo box','spin button','check box','push button','toggle button','list item')
        return next((node for node in matches if node.get_role_name() in controls), matches[0] if matches else None)
    return wait_for(find, 'accessible control: '+name)


def focus(desktop, node):
    # Ubuntu GTK 4.14's AT-SPI GrabFocus is not implemented consistently and can
    # log a gtk_window_get_focus critical. Use the actual keyboard route.
    if desktop.click(node):
        node.clear_cache()
    for _ in range(120):
        node.clear_cache()
        if any(child.get_state_set().contains(Atspi.StateType.FOCUSED) for child in walk(node)):
            return
        desktop.chord(0xff09)
    raise RuntimeError('Could not focus '+node.get_name())


def edit(desktop, name, value):
    node = named(desktop, name, 'EditableText')
    apply = desktop.find(lambda n: n.get_name() == name+' · Apply' and showing(n)
                         and n.get_role_name() == 'push button')
    if apply is None:
        focus(desktop, node)
    if not Atspi.EditableText.set_text_contents(node, str(value)):
        raise RuntimeError('Native text editing failed: '+name)
    if apply is not None:
        action(apply)
    else:
        desktop.chord(0xff0d)
    pump(.25)
    return node


def activate(desktop, node):
    if desktop.click(node):
        return
    focus(desktop, node)
    desktop.chord(0x20)
    pump(.15)


def set_checked(desktop, name, checked):
    node = named(desktop, name)
    state = Atspi.StateType.CHECKED if node.get_role_name() == 'check box' else Atspi.StateType.PRESSED
    if node.get_state_set().contains(state) != checked:
        activate(desktop, node)
    def matches():
        node.clear_cache()
        return node.get_state_set().contains(state) == checked
    wait_for(matches, name+' checked='+str(checked))


def tab(desktop, name):
    node = wait_for(lambda: desktop.find(lambda n: n.get_role_name() == 'page tab' and n.get_name() == name),
                    'inspector tab: '+name)
    parent = node.get_parent()
    if 'Selection' not in parent.get_interfaces() or not Atspi.Selection.select_child(parent, node.get_index_in_parent()):
        focus(desktop, node)
        desktop.chord(0x20)
    node.clear_cache()
    wait_for(lambda: node.get_state_set().contains(Atspi.StateType.SELECTED), 'selected inspector tab: '+name)


def choose(desktop, name, index, expected):
    node = named(desktop, name)
    activate(desktop, node)
    wait_for(lambda: desktop.find(lambda child: showing(child) and child.get_role_name() == 'list item'
        and any(label.get_name() == expected for label in walk(child))), 'native popup option: '+expected)
    desktop.chord(0xff50)
    for _ in range(index):
        desktop.chord(0xff54)
    desktop.chord(0xff0d)
    pump(.2)
    node.clear_cache()
    displayed = [child.get_name() for child in walk(node) if showing(child)]
    # GtkDropDown names its current value; AdwComboRow retains its field title
    # and exposes the committed value in its single visible summary child.
    if (node.get_role_name() == 'combo box' and node.get_name() not in (expected, name)) or expected not in displayed or node.get_state_set().contains(Atspi.StateType.EXPANDED):
        raise AssertionError(f'{name}: expected {expected!r}, actual selected value {node.get_name()!r}')


def spin(desktop, name, value):
    node = named(desktop, name, 'Value')
    if not Atspi.Value.set_current_value(node, value):
        raise RuntimeError('Native spin value rejected: '+name)
    pump(.2)
    actual = Atspi.Value.get_current_value(node)
    if not math.isclose(actual, value, abs_tol=1e-6):
        raise AssertionError(f'{name}: expected {value}, got {actual}')


def select_object(desktop, name):
    row = wait_for(lambda: desktop.find(lambda n: n.get_role_name() == 'list item' and any(
        c.get_role_name() == 'label' and c.get_name() == name for c in walk(n))), 'outliner: '+name)
    parent = row.get_parent()
    if not desktop.click(row):
        if not Atspi.Selection.clear_selection(parent):
            raise RuntimeError('Outliner exclusive selection could not clear old membership: '+name)
        pump(.1)
        if not Atspi.Selection.select_child(parent, row.get_index_in_parent()):
            raise RuntimeError('Outliner selection failed: '+name)
    pump(.3)
    def selected():
        row.clear_cache()
        return (row.get_state_set().contains(Atspi.StateType.SELECTED)
                and Atspi.Selection.get_n_selected_children(parent) == 1)
    wait_for(selected, 'exclusive outliner row selection: '+name, 5)


def save_camera(desktop, output, name):
    from gsv import Scene
    path = output / name
    desktop.native_save('save', 'Save Camera Animation', path)
    with Scene(path) as scene:
        scene.validate()
        obj = scene.objects[0]
        return {'id': obj['id'], 'label': obj.get('label'),
                'times': scene.array(obj['data']['times']).copy(),
                'poses': scene.array(obj['data']['pose']['camera_to_world']).copy(),
                'K': scene.array(obj['data']['projection']['intrinsics']).copy(),
                'image_sizes': scene.array(obj['data']['projection']['image_sizes']).copy()}


def run_full(desktop, output, report):
    """Check workbench commands after the basic native camera roundtrip."""
    report['full_checks'] = []
    def check(identifier, operation):
        started = time.monotonic()
        observation = operation()
        report['full_checks'].append({'id': identifier, 'passed': True,
            'elapsed_seconds': round(time.monotonic()-started, 3), 'observed': observation})
        print('desktop parity passed: '+identifier, flush=True)

    def inspector_pages():
        expected = ['Selection', 'Viewport', 'Document']
        actual = [n.get_name() for n in desktop.app_nodes() if n.get_role_name() == 'page tab']
        if actual != expected:
            raise AssertionError(f'Inspector pages {actual} != {expected}')
        tab(desktop, 'Document')
        labels = [n.get_name() for n in desktop.app_nodes() if showing(n)]
        if not any('synthetic.gsv' in name for name in labels) or 'Coordinate system' not in labels:
            raise AssertionError('Document inspector omitted its source or coordinate system')
        tab(desktop, 'Selection')
        return actual
    check('selection-viewport-document-inspector', inspector_pages)

    def sample_transport():
        for operation, expected in [('first', 0.), ('next', .5), ('next', 1.), ('previous', .5), ('last', 1.), ('first', 0.)]:
            desktop.window_action(operation)
            wait_for(lambda: desktop.current_time() == expected, operation+' reaches exact source time')
        desktop.seek(.25)
        return 'First/previous/next/last and fractional TIME entry retain native timestamps'
    check('all-sample-transport-commands', sample_transport)

    def preview_range():
        edit(desktop, 'In time', .25)
        edit(desktop, 'Out time', .75)
        set_checked(desktop, 'Preview range', True)
        set_checked(desktop, 'Loop', False)
        edit(desktop, 'Preview /s', 4)
        desktop.window_action('play')
        wait_for(lambda: desktop.current_time() == .75, 'non-loop playback stops at exact out time')
        pump(.3)
        if desktop.current_time() != .75:
            raise AssertionError('Non-loop transport continued past preview out')
        set_checked(desktop, 'Preview range', False)
        set_checked(desktop, 'Loop', True)
        edit(desktop, 'Preview /s', 1)
        choose(desktop, 'Playback speed', 4, '4×')
        choose(desktop, 'Playback speed', 2, '1×')
        desktop.seek(.5)
        return {'range': [.25, .75], 'non_loop_stop': .75, 'speed_choices': ['4×', '1×']}
    check('preview-range-rate-speed-loop', preview_range)

    def view_commands():
        # Save the actual camera after each operation to verify handedness and
        # history, without reaching into the running process.
        select_object(desktop, 'Camera 1')
        reference = save_camera(desktop, output, 'parity-before-views.gsv')
        desktop.window_action('front')
        desktop.window_action('record')
        front = save_camera(desktop, output, 'parity-front.gsv')
        pose = front['poses'][np.flatnonzero(front['times'] == .5)[0], 0]
        np.testing.assert_allclose(pose[:3,:3].T @ pose[:3,:3], np.eye(3), atol=1e-6)
        if not np.isclose(np.linalg.det(pose[:3,:3]), 1, atol=1e-6):
            raise AssertionError('Front preset produced a reflected camera')
        desktop.window_action('camera-undo')
        for operation in ('right', 'top', 'isometric', 'previous-view', 'next-view', 'home', 'frame'):
            desktop.window_action(operation)
        desktop.window_action('record')
        save_camera(desktop, output, 'parity-after-views.gsv')
        desktop.window_action('camera-undo')
        return {'preset_rotation_determinant': float(np.linalg.det(pose[:3,:3])),
                'unchanged_track_identity': reference['id'] == front['id']}
    check('all-presets-frame-and-view-history', view_commands)

    def camera_fields():
        select_object(desktop, 'Camera 1')
        tab(desktop, 'Selection')
        edit(desktop, 'Camera name', 'Desktop acceptance camera')
        edit(desktop, 'XYZ', '0.1, 0.2, 0.3')
        edit(desktop, 'WXYZ', '1, 0, 0, 0')
        edit(desktop, 'Key FOV', 70)
        changed = save_camera(desktop, output, 'parity-edited-camera.gsv')
        index = np.flatnonzero(changed['times'] == .5)[0]
        np.testing.assert_allclose(changed['poses'][index,0,:3,3], [.1,.2,.3], atol=1e-7)
        np.testing.assert_allclose(changed['poses'][index,0,:3,:3], np.eye(3), atol=1e-7)
        width = changed['image_sizes'][0,0]
        actual_fov = math.degrees(2*math.atan(width/(2*changed['K'][index,0,0,0])))
        if not math.isclose(actual_fov, 70, abs_tol=1e-5):
            raise AssertionError('Edited horizontal FOV did not persist: '+str(actual_fov))
        if changed['label'] != 'Desktop acceptance camera':
            raise AssertionError('Camera name did not persist')
        choose(desktop, 'Camera keyframes', 1, '0.5')
        edit(desktop, 'Keyframe time', .75)
        wait_for(lambda: desktop.current_time() == .75, 'key retime seeks moved key')
        retimed = save_camera(desktop, output, 'parity-retimed-camera.gsv')
        np.testing.assert_array_equal(retimed['times'], [0., .75])
        action(desktop.button('Delete key'))
        deleted = save_camera(desktop, output, 'parity-deleted-key.gsv')
        np.testing.assert_array_equal(deleted['times'], [0.])
        desktop.window_action('camera-undo')
        restored = save_camera(desktop, output, 'parity-restored-key.gsv')
        np.testing.assert_array_equal(restored['times'], [0., .75])
        return {'position': [.1,.2,.3], 'quaternion': [1,0,0,0], 'fov_degrees': 70,
                'retimed_keys': retimed['times'].tolist(), 'undo_restored_key': True}
    check('camera-name-transform-fov-key-choice-retime-delete-undo', camera_fields)

    def identity_inspection():
        select_object(desktop, 'moving-points')
        edit(desktop, 'Exact element ID', str(2**60+3))
        wait_for(lambda: desktop.find(lambda n: n.get_role_name() == 'label' and
                 str(2**60+3) in n.get_name()), 'exact int64 identity inspected')
        action(desktop.button('Copy attributes'))
        return {'exact_identity': 2**60+3, 'copy_attributes_invoked': True}
    check('exact-int64-inspection-and-copy', identity_inspection)

    def display_controls():
        tab(desktop, 'Viewport')
        for name, value in [('Path density', 25), ('Path opacity', 35), ('Trail frames', 16),
                            ('Gaussian size', 1.5), ('Opacity multiplier', .75), ('Exposure multiplier', 1.25),
                            ('Near clip in scene units', .02)]:
            spin(desktop, name, value)
        choose(desktop, 'Preview background', 1, 'Black')
        choose(desktop, 'Preview background', 0, 'Studio')
        choose(desktop, 'Orbit style', 1, 'Trackball · free rotation')
        choose(desktop, 'Orbit style', 0, 'Turntable · world up')
        action(desktop.button('Use selection as center'))
        action(desktop.button('Use visible scene depth'))
        for name in ('Coordinate grid', 'Cameras', 'Paths', 'Trails'):
            node = named(desktop, name)
            before = tuple(node.get_state_set().contains(state) for state in (Atspi.StateType.CHECKED, Atspi.StateType.PRESSED))
            activate(desktop, node)
            node.clear_cache()
            after = tuple(node.get_state_set().contains(state) for state in (Atspi.StateType.CHECKED, Atspi.StateType.PRESSED))
            if before == after:
                raise AssertionError('Preview toggle did not change: '+name)
            activate(desktop, node)
        for name, value in [('Path density',100), ('Path opacity',18), ('Trail frames',8),
                            ('Gaussian size',1), ('Opacity multiplier',1), ('Exposure multiplier',1), ('Near clip in scene units',.01)]:
            spin(desktop, name, value)
        sizes = {}
        for index, value in [(2,'50%'), (1,'75%'), (0,'100%')]:
            choose(desktop, 'Viewport quality', index, value)
            path = output / ('parity-resolution-'+value.rstrip('%')+'.png')
            desktop.native_save('screenshot', 'Save Screenshot…', path)
            sizes[value] = list(struct.unpack('>II',path.read_bytes()[16:24]))
        for ratio in (.5,.75):
            expected = [round(d*ratio) for d in sizes['100%']]
            if any(abs(a-b)>1 for a,b in zip(sizes[str(int(ratio*100))+'%'],expected)):
                raise AssertionError('Screenshot does not follow preview resolution: '+repr(sizes))
        tab(desktop, 'Selection')
        return {'preview_png_dimensions': sizes, 'settings_restored': True}
    check('all-preview-settings-toggles-and-resolution-exports', display_controls)

    def panel_commands():
        for operation in ('toggle-outliner','toggle-inspector','fullscreen'):
            desktop.window_action(operation)
            pump(.25)
            desktop.window_action(operation)
        desktop.window_action('help')
        wait_for(lambda: desktop.button('OK'), 'native navigation help')
        action(desktop.button('OK'))
        return 'Sidebar, inspector, fullscreen and navigation guide commands completed'
    check('panels-fullscreen-navigation-help', panel_commands)

    for path in sorted(output.glob('parity-*')):
        if path.is_file():
            report['artifacts'][path.name] = {'bytes': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
