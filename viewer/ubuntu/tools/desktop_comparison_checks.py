"""Comparison acceptance through the owned application's public desktop UI.

Called by the explicitly opted-in GNOME runner after ``desktop_parity.run_full``.
No application object, GTK widget reference or private application method is
imported. Native save dialogs and independently decoded PNGs are the evidence.
"""
from __future__ import annotations

import hashlib
import re
import time

import gi
import numpy as np

gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Atspi, GdkPixbuf, GLib

from desktop_parity import activate, choose, named, select_object, showing, tab
from test_desktop_session import pump, text, wait_for


def _labels(desktop):
    return [node.get_name() for node in desktop.app_nodes()
            if node.get_role_name() == 'label' and showing(node)]


def _canvas(desktop):
    return desktop.find(lambda node: node.get_name() == 'Image comparison'
                        and 'Component' in node.get_interfaces()
                        and showing(node)
                        and node.get_state_set().contains(Atspi.StateType.FOCUSABLE))


def _ready(desktop, *, scores):
    def completed():
        for label in _labels(desktop):
            if 'CPU reference' in label and 'reference-view' in label:
                has_scores = bool(re.search(r'PSNR\s+[-+\d]', label))
                if has_scores == scores:
                    return label
        return None
    return wait_for(completed, 'completed calibrated '+('comparison' if scores else 'preview'), 30)


def _time(desktop):
    try:
        return float(text(named(desktop, 'TIME', 'EditableText')))
    except ValueError:
        return None


def _png(path, size=None):
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
    dimensions = (pixbuf.get_width(), pixbuf.get_height())
    if size is not None and dimensions != size:
        raise AssertionError(f'{path.name}: {dimensions} != native raster {size}')
    # GdkPixbuf omits unused padding after the final row. Construct with explicit
    # row strides rather than assuming its byte buffer is height*rowstride long.
    pixels = np.ndarray((dimensions[1], dimensions[0], pixbuf.get_n_channels()),
                        np.uint8, buffer=pixbuf.get_pixels(),
                        strides=(pixbuf.get_rowstride(), pixbuf.get_n_channels(), 1))[:, :, :3].copy()
    if not np.any(pixels):
        raise AssertionError(path.name+' contains only black pixels')
    return pixels


def _save(desktop, output, name, *, workspace=False, size=None):
    path = output / name
    desktop.native_save('workspace-snapshot' if workspace else 'screenshot',
                        'Save Workspace Snapshot…' if workspace else 'Save Screenshot…', path)
    return path, _png(path, size)


def _assert_locked(desktop, locked):
    observed = {}
    for name in ('Coordinate grid', 'Cameras', 'Paths', 'Trails', 'Viewport quality', 'Camera'):
        # An outliner category can have the same title as a footer control.
        # Require its actual accessible action, including a dropdown's inner
        # toggle, instead of accidentally checking a plain category label.
        node = named(desktop, name, 'Action')
        node.clear_cache()
        enabled = node.get_state_set().contains(Atspi.StateType.SENSITIVE)
        if enabled == locked:
            raise AssertionError(f'{name}: expected '+('disabled' if locked else 'enabled'))
        observed[name] = enabled
    if locked:
        canvas = wait_for(lambda: _canvas(desktop), 'visible accessible comparison canvas')
        if ('EditableText' in canvas.get_interfaces()
                or canvas.get_state_set().contains(Atspi.StateType.EDITABLE)):
            raise AssertionError('Comparison image unexpectedly exposes editable source content')
        tab(desktop, 'Viewport')
        for name in ('Orbit style', 'Gaussian size', 'Opacity multiplier', 'Exposure multiplier',
                     'Near clip in scene units', 'Preview background'):
            interface = 'Action' if name in ('Orbit style', 'Preview background') else 'Value'
            node = named(desktop, name, interface)
            node.clear_cache()
            if node.get_state_set().contains(Atspi.StateType.SENSITIVE):
                raise AssertionError('Comparison did not lock preview control: '+name)
            observed[name] = False
        tab(desktop, 'Selection')
    return observed


def _image_region(desktop, pixels):
    """A central reference-pane crop excludes labels, controls and focus rings."""
    canvas = Atspi.Component.get_extents(_canvas(desktop), Atspi.CoordType.WINDOW)
    window = Atspi.Component.get_extents(desktop.window(), Atspi.CoordType.WINDOW)
    sx, sy = pixels.shape[1]/window.width, pixels.shape[0]/window.height
    x0, x1 = [round((canvas.x-window.x+canvas.width*fraction)*sx) for fraction in (.60,.90)]
    y0, y1 = [round((canvas.y-window.y+canvas.height*fraction)*sy) for fraction in (.35,.75)]
    region = pixels[max(0,y0):min(pixels.shape[0],y1), max(0,x0):min(pixels.shape[1],x1)]
    if min(region.shape[:2]) < 8:
        raise AssertionError('Cannot map the owned comparison pane into its workspace export')
    return region


def _return_to_scene(desktop):
    activate(desktop, named(desktop, 'Return to 3D'))
    wait_for(lambda: _canvas(desktop) is None, 'comparison closes and 3D viewport returns')
    _assert_locked(desktop, False)


def _native_image_gesture(desktop, report):
    """Pan and Alt-wheel on the explicitly mapped owned comparison canvas."""
    if not desktop.pointer_stream:
        return {'tested': False, 'reason': 'No verified native absolute pointer mapping'}
    canvas = wait_for(lambda: _canvas(desktop), 'comparison canvas pointer target')
    rect = Atspi.Component.get_extents(canvas, Atspi.CoordType.WINDOW)
    if rect.width < 160 or rect.height < 120:
        raise AssertionError('Comparison canvas is too small for an owned-window gesture')
    from desktop_coordinates import owned_target_point
    start = owned_target_point(desktop, canvas, x_fraction=.70, y_fraction=.55)
    end = owned_target_point(desktop, canvas, x_fraction=.75, y_fraction=.60)
    if start is None or end is None:
        raise RuntimeError('Native comparison window coordinates could not be verified')
    desktop.key_call('NotifyPointerMotionAbsolute', GLib.Variant('(sdd)', (desktop.pointer_stream, *start)))
    pump(.1)
    desktop.key_call('NotifyPointerButton', GLib.Variant('(ib)', (272, True)))
    try:
        for index in range(1, 5):
            x,y = [a+(b-a)*index/4 for a,b in zip(start,end)]
            desktop.key_call('NotifyPointerMotionAbsolute', GLib.Variant('(sdd)', (desktop.pointer_stream, x,y)))
            pump(.04)
    finally:
        desktop.key_call('NotifyPointerButton', GLib.Variant('(ib)', (272, False)))
    desktop.key_call('NotifyKeyboardKeysym', GLib.Variant('(ub)', (0xffe9, True)))
    try:
        desktop.key_call('NotifyPointerAxisDiscrete', GLib.Variant('(ui)', (0, -2)))
    finally:
        desktop.key_call('NotifyKeyboardKeysym', GLib.Variant('(ub)', (0xffe9, False)))
    pump(.3)
    return {'tested': True, 'backend': report['backend'], 'canvas_rect': [rect.x,rect.y,rect.width,rect.height],
            'start':list(start), 'end':list(end), 'alt_wheel_steps':2}


def run_comparison_checks(desktop, output, report):
    """Add ``comparison_checks`` and artifacts; leave the normal 3D view active."""
    from gsv import Scene

    report['comparison_checks'] = []
    report.setdefault('artifacts', {})
    def check(identifier, operation):
        started = time.monotonic()
        observation = operation()
        report['comparison_checks'].append({'id': identifier, 'passed': True,
            'elapsed_seconds': round(time.monotonic()-started, 3), 'observed': observation})
        print('desktop comparison passed: '+identifier, flush=True)

    def preview():
        desktop.window_action('first')
        tab(desktop, 'Selection')
        select_object(desktop, 'reference-view')
        activate(desktop, named(desktop, 'Preview render view'))
        status = _ready(desktop, scores=False)
        if any(label in ('PSNR', 'SSIM', 'MSE', 'MAE') for label in _labels(desktop)):
            raise AssertionError('Render-only preview exposes comparison score rows')
        controls = _assert_locked(desktop, True)
        _, image = _save(desktop, output, 'comparison-preview.png', size=(640, 480))
        _return_to_scene(desktop)
        return {'status': status, 'png_dimensions': [image.shape[1], image.shape[0]],
                'metrics_absent': True, 'preview_controls_enabled': controls, 'returned_to_3d': True}
    check('selected-render-preview-single-native-png-and-return', preview)

    tab(desktop, 'Selection')
    select_object(desktop, 'reference-images')
    activate(desktop, named(desktop, 'Compare render with camera observation'))
    _ready(desktop, scores=True)
    choose(desktop, 'Camera capture', 0, '0 · reference-images')
    _ready(desktop, scores=True)
    with Scene(output / 'synthetic.gsv') as source:
        pixels = source.object('reference-images')['data']['pixels']['images']
        references = [source.array(identifier).copy() for identifier in pixels]

    initial_path, initial_pair = _save(desktop, output, 'comparison-pair-original.png', size=(1280, 480))
    np.testing.assert_array_equal(initial_pair[:, 640:], references[0],
                                  err_msg='Exported reference differs from the original native observation')
    original_hash = hashlib.sha256(initial_path.read_bytes()).hexdigest()

    def inspection():
        controls = _assert_locked(desktop, True)
        activate(desktop, named(desktop, 'Fit both'))
        activate(desktop, named(desktop, 'One image pixel per display backing pixel'))
        _, before = _save(desktop, output, 'comparison-workspace-before-input.png', workspace=True)
        before_region = _image_region(desktop, before)
        gesture = _native_image_gesture(desktop, report)
        _, after = _save(desktop, output, 'comparison-workspace.png', workspace=True)
        if gesture['tested']:
            after_region = _image_region(desktop, after)
            if before_region.shape != after_region.shape:
                raise AssertionError('Workspace resized during the controlled comparison gesture')
            changed = int(np.count_nonzero(np.any(before_region != after_region, axis=2)))
            if changed < max(4, before_region.shape[0]*before_region.shape[1]*.01):
                raise AssertionError('Native image pan/Alt-wheel did not visibly change the reference pane')
            gesture['changed_reference_crop_pixels'] = changed
        pair_path, pair = _save(desktop, output, 'comparison-pair-inspected.png', size=(1280, 480))
        np.testing.assert_array_equal(pair, initial_pair, err_msg='Image inspection changed the exported native pair')
        inspected_hash = hashlib.sha256(pair_path.read_bytes()).hexdigest()
        if inspected_hash != original_hash:
            raise AssertionError('Native pair PNG hash changed after Fit/1:1/pan/zoom')
        return {'pair_sha256': original_hash, 'inspected_pair_sha256': inspected_hash,
                'workspace_dimensions': [after.shape[1], after.shape[0]],
                'native_input': gesture, 'preview_controls_enabled': controls,
                'comparison_canvas_read_only': True}
    check('selected-observation-linked-inspection-pair-and-workspace-png', inspection)

    def captures():
        choose(desktop, 'Camera capture', 1, '1 · reference-images')
        wait_for(lambda: _time(desktop) == 1., 'capture selector seeks exact time 1')
        second_status = _ready(desktop, scores=True)
        _, second = _save(desktop, output, 'comparison-second-capture.png', size=(1280, 480))
        np.testing.assert_array_equal(second[:, 640:], references[1],
                                      err_msg='Second capture did not export its own observation')
        choose(desktop, 'Camera capture', 0, '0 · reference-images')
        wait_for(lambda: _time(desktop) == 0., 'capture selector returns to exact time 0')
        first_status = _ready(desktop, scores=True)
        _, first = _save(desktop, output, 'comparison-first-capture-restored.png', size=(1280, 480))
        np.testing.assert_array_equal(first, initial_pair,
                                      err_msg='Returning to the first capture did not restore the exact pair')
        _return_to_scene(desktop)
        return {'capture_times': [1., 0.], 'second_capture_status': second_status,
                'first_capture_status': first_status, 'native_observations_verified': True,
                'returned_to_3d': True}
    check('exact-capture-selector-second-and-first-native-rasters', captures)

    for path in sorted(output.glob('comparison-*.png')):
        report['artifacts'][path.name] = {'bytes': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
