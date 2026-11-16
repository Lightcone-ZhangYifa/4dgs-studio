"""Opt-in physical timeline acceptance for the real owned desktop window.

The caller invokes run_timeline_checks after desktop_parity.run_full, while the
synthetic source and its authored camera are open in 3D. No product modules,
widget references, private methods or test-only application endpoints are used.
Scrub coordinates are calibrated from public TIME/slider values at verified
canvas fractions; no private ruler margins/range are assumed. Native camera
exports independently prove key retiming, cancellation and restoration.

Importing this module does not create a session or generate input. An existing
authorized EIS sender, verified owned-window coordinates and actual focus are
required. Unavailable coordinates/focus are recorded as failed and untested,
never silently skipped or counted as a successful desktop check.
"""
from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
import time

import gi
import numpy as np
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi, GLib

from desktop_coordinates import owned_target_point
from desktop_parity import named, select_object, showing, tab
from test_desktop_session import pump, text, wait_for, walk


_ALT, _SHIFT, _HOME, _LEFT, _RIGHT, _SPACE, _ESCAPE = (
    0xffe9, 0xffe1, 0xff50, 0xff51, 0xff53, 0x20, 0xff1b)


class TimelineNotTested(RuntimeError):
    """A required owned input target could not be verified; no pass is claimed."""


def _canvas(desktop):
    try:
        return wait_for(lambda: desktop.find(lambda node:
            node.get_process_id() == desktop.pid and node.get_role_name() == 'slider'
            and node.get_name().startswith('Timeline') and showing(node)
            and 'Component' in node.get_interfaces() and 'Value' in node.get_interfaces()),
            'owned public timeline slider', 3)
    except RuntimeError as error:
        raise TimelineNotTested('No public owned timeline slider/coordinates are available') from error


def _point(desktop, node, x=.5, y=.5):
    if desktop.owner_pid() != desktop.pid or node.get_process_id() != desktop.pid:
        raise TimelineNotTested('Timeline target is no longer owned by the tested process')
    point = owned_target_point(desktop, node, x_fraction=x, y_fraction=y)
    if point is None:
        raise TimelineNotTested('Timeline coordinates could not be verified; no guessed input was sent')
    return point


def _focused(node):
    node.clear_cache()
    return node.get_state_set().contains(Atspi.StateType.FOCUSED)


def _focus(desktop):
    node = _canvas(desktop)
    _point(desktop, node)
    if not desktop.click(node):
        raise TimelineNotTested('The owned timeline click failed; keyboard checks were not attempted')
    try:
        wait_for(lambda: _focused(node), 'timeline physically clicked and actually FOCUSED', 3)
    except RuntimeError as error:
        raise TimelineNotTested('Timeline click did not establish native keyboard focus') from error
    return node


def _key(desktop, symbol, pressed):
    desktop.key_call('NotifyKeyboardKeysym', GLib.Variant('(ub)', (symbol, pressed)))


def _tap(desktop, symbol, *, require_focus=True):
    if require_focus and not _focused(_canvas(desktop)):
        raise TimelineNotTested('Timeline lost verified focus before the keyboard event')
    try:
        _key(desktop, symbol, True)
        pump(.025)
    finally:
        _key(desktop, symbol, False)
    pump(.06)


def _move(desktop, node, x, y):
    point = _point(desktop, node, x, y)
    desktop.key_call('NotifyPointerMotionAbsolute',
                     GLib.Variant('(sdd)', (desktop.pointer_stream, *point)))
    pump(.035)


def _button(desktop, code, pressed):
    desktop.key_call('NotifyPointerButton', GLib.Variant('(ib)', (code, pressed)))


def _read_time(desktop):
    value = float(text(named(desktop, 'TIME', 'EditableText')))
    if not math.isfinite(value):
        raise AssertionError('Public TIME is not finite')
    return value


def _read_key_time(desktop):
    try:
        value = float(text(named(desktop, 'Keyframe time', 'EditableText')))
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def _stable_time(desktop):
    pump(.18)
    previous = _read_time(desktop)
    def stable():
        nonlocal previous
        pump(.07)
        current = _read_time(desktop)
        same = current == previous
        previous = current
        return same
    wait_for(stable, 'stable committed timeline time', 4)
    canvas = _canvas(desktop)
    wait_for(lambda: math.isclose(float(Atspi.Value.get_current_value(canvas)), previous,
                                 rel_tol=1e-12, abs_tol=1e-12),
             'public timeline slider agrees with TIME', 3)
    return previous


def _probe(desktop, fraction, *, bypass=True):
    node = _focus(desktop)
    alt = down = False
    try:
        if bypass:
            _key(desktop, _ALT, True); alt = True
        _move(desktop, node, fraction, .42)
        _button(desktop, 272, True); down = True
        pump(.05)
        _button(desktop, 272, False); down = False
    finally:
        if down:
            _button(desktop, 272, False)
        if alt:
            _key(desktop, _ALT, False)
    if not _focused(node):
        raise TimelineNotTested('Physical timeline scrub did not retain native focus')
    return _stable_time(desktop)


def _mapping(desktop):
    # Interior fractions avoid ruler labels and clamped document endpoints.
    fractions = (.43, .66)
    times = tuple(_probe(desktop, fraction) for fraction in fractions)
    if not times[1] > times[0]:
        raise AssertionError('Physical scrub did not produce increasing interior native times')
    return fractions, times


def _fraction(mapping, value):
    (a, b), (lo, hi) = mapping
    result = a + (value-lo) * (b-a)/(hi-lo)
    if not .02 < result < .98:
        raise TimelineNotTested('The requested visible timeline target lies outside verified canvas fractions')
    return result


def _slope(mapping):
    (a, b), (lo, hi) = mapping
    return (hi-lo)/(b-a)


def _checked(desktop, name):
    node = named(desktop, name)
    if node.get_process_id() != desktop.pid:
        raise TimelineNotTested('Timeline control is not owned: '+name)
    node.clear_cache()
    return node.get_state_set().contains(Atspi.StateType.CHECKED)


def _set_checked(desktop, name, checked):
    if _checked(desktop, name) == checked:
        return
    node = named(desktop, name)
    _point(desktop, node)
    if not desktop.click(node):
        raise TimelineNotTested('Could not physically click owned timeline control: '+name)
    wait_for(lambda: _checked(desktop, name) == checked, name+' changes through native pointer input')


def _seek(desktop, value):
    entry = named(desktop, 'TIME', 'EditableText')
    _point(desktop, entry)
    if not desktop.click(entry):
        raise TimelineNotTested('Could not click owned TIME entry for restoration')
    try:
        wait_for(lambda: _focused(entry), 'owned TIME entry has physical keyboard focus', 3)
    except RuntimeError as error:
        raise TimelineNotTested('TIME entry did not obtain native focus for restoration') from error
    if not Atspi.EditableText.set_text_contents(entry, format(value, '.17g')):
        raise AssertionError('Public TIME entry rejected native time')
    _tap(desktop, 0xff0d, require_focus=False)
    wait_for(lambda: _read_time(desktop) == value, 'original native time restored')


def _export_camera(desktop, output, name):
    from gsv import Scene
    path = output / name
    desktop.native_save('save', 'Save Camera Animation', path)
    with Scene(path) as scene:
        scene.validate()
        return {'objects': copy.deepcopy(scene.objects),
                'spaces': copy.deepcopy(scene.manifest.get('spaces', {})),
                'clocks': copy.deepcopy(scene.manifest.get('clocks', {})),
                'arrays': {key: scene.array(key).copy() for key in scene.manifest['arrays']}}


def _same_camera(before, after):
    for key in ('objects', 'spaces', 'clocks'):
        if before[key] != after[key]:
            raise AssertionError('Timeline interaction changed authored camera '+key)
    if before['arrays'].keys() != after['arrays'].keys():
        raise AssertionError('Timeline interaction changed the camera array set')
    for key, value in before['arrays'].items():
        np.testing.assert_array_equal(value, after['arrays'][key],
                                      err_msg='Timeline interaction changed camera array '+key)


def _drag(desktop, start, end, *, cancel=False, middle=False, expected_key_time=None):
    node = _focus(desktop)
    button = 274 if middle else 272
    alt = down = completed = False
    try:
        if not middle:
            _key(desktop, _ALT, True); alt = True
        _move(desktop, node, start, .72 if not middle else .42)
        _button(desktop, button, True); down = True
        pump(.08)
        if expected_key_time is not None:
            wait_for(lambda: _read_key_time(desktop) == expected_key_time
                and _read_time(desktop) == expected_key_time,
                'physical key hit changes public selected Keyframe time and playhead')
        for step in range(1, 7):
            _move(desktop, node, start + (end-start)*step/6, .72 if not middle else .42)
        if cancel:
            _tap(desktop, _ESCAPE)
        _button(desktop, button, False); down = False
        completed = True
    finally:
        if down:
            # An interrupted key drag must be canceled before button release,
            # which would otherwise commit its visible ghost keyframe.
            if not middle and _focused(node):
                _tap(desktop, _ESCAPE)
            _button(desktop, button, False)
        if alt:
            _key(desktop, _ALT, False)
    if not completed:
        raise AssertionError('Timeline drag did not complete')
    return _stable_time(desktop)


def _wheel(desktop, steps, *, pan=False):
    node = _focus(desktop)
    _move(desktop, node, .55, .42)
    shifted = False
    try:
        if pan:
            _key(desktop, _SHIFT, True); shifted = True
        desktop.key_call('NotifyPointerAxisDiscrete', GLib.Variant('(ui)', (0, steps)))
        pump(.2)
    finally:
        if shifted:
            _key(desktop, _SHIFT, False)


def run_timeline_checks(desktop, output, report):
    """Append physical timeline evidence and restore source, cameras and time.

    This routine is intentionally not invoked on import. The explicit GNOME
    runner calls it between run_full and comparison/lifecycle acceptance.
    """
    output = Path(output)
    report['timeline_checks'] = []
    report.setdefault('artifacts', {})
    baseline = original_time = original_controls = None
    failure = None
    ready = False
    source = output / 'synthetic.gsv'
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source_title = desktop.window().get_name()

    def check(identifier, operation):
        started = time.monotonic()
        try:
            observed = operation()
        except Exception as error:
            report['timeline_checks'].append({'id': identifier, 'passed': False,
                'tested': not isinstance(error, TimelineNotTested),
                'elapsed_seconds': round(time.monotonic()-started, 3), 'reason': str(error)})
            raise
        report['timeline_checks'].append({'id': identifier, 'passed': True, 'tested': True,
            'elapsed_seconds': round(time.monotonic()-started, 3), 'observed': observed})
        print('desktop timeline passed: '+identifier, flush=True)
        return observed

    try:
        def preflight():
            if getattr(desktop, 'eis', None) is None or not desktop.pointer_stream:
                raise TimelineNotTested('Physical timeline checks require the authorized EIS input backend')
            _focus(desktop)
            return 'Verified owned coordinate click established actual timeline FOCUSED state'
        # Capture the original TIME before the required physical focus click,
        # since clicking the timeline itself intentionally scrubs the playhead.
        original_time = _read_time(desktop)
        original_controls = {name: _checked(desktop, name) for name in ('Snap to samples', 'Loop', 'Preview range')}
        check('owned-physical-timeline-focus', preflight)
        ready = True
        baseline = _export_camera(desktop, output, 'timeline-camera-before.gsv')
        if not baseline['objects']:
            raise TimelineNotTested('No authored camera is available for independent keyframe evidence')
        camera = baseline['objects'][0]
        camera_label = camera.get('label', camera['id'])
        key_times = np.asarray(baseline['arrays'][camera['data']['times']], np.float64)
        if len(key_times) < 2:
            raise TimelineNotTested('Two authored keys are required to prove a fresh physical key selection')
        tab(desktop, 'Selection')
        select_object(desktop, camera_label)
        _set_checked(desktop, 'Preview range', False)
        _set_checked(desktop, 'Loop', True)
        _set_checked(desktop, 'Snap to samples', True)
        _focus(desktop); _tap(desktop, _HOME)
        fit = _mapping(desktop)
        canvas = _canvas(desktop)
        lower, upper = float(Atspi.Value.get_minimum_value(canvas)), float(Atspi.Value.get_maximum_value(canvas))
        if not upper > lower:
            raise TimelineNotTested('Timeline needs at least two distinct source timestamps')
        span = upper-lower
        logical_width = Atspi.Component.get_extents(canvas, Atspi.CoordType.WINDOW).width
        tolerance = abs(_slope(fit))*2/max(1, logical_width)

        def scrub_and_snap():
            # Public transport supplies actual source ticks, independently of
            # the key handler being tested later.
            desktop.window_action('first')
            wait_for(lambda: _read_time(desktop) == lower, 'public First reaches timeline minimum')
            samples = [lower]
            for _ in range(64):
                if samples[-1] == upper:
                    break
                previous = samples[-1]
                desktop.window_action('next')
                wait_for(lambda: _read_time(desktop) > previous, 'public Next reaches a later native sample')
                samples.append(_read_time(desktop))
            if samples[-1] != upper or len(samples) < 3:
                raise TimelineNotTested('The fixture needs 3–65 public samples for the interior snap/keyboard checks')
            target = samples[len(samples)//2]
            position = _fraction(fit, target)
            nearby = position + 3/max(1, logical_width)
            _probe(desktop, .43)
            snapped = _probe(desktop, nearby, bypass=False)
            if snapped != target:
                raise AssertionError(f'Near-tick physical scrub did not snap: {snapped} != {target}')
            unsnapped = _probe(desktop, nearby, bypass=True)
            expected = target + 3/max(1, logical_width)*_slope(fit)
            if unsnapped == target or not math.isclose(unsnapped, expected, abs_tol=tolerance):
                raise AssertionError(f'Alt did not bypass near-tick snapping: {unsnapped}, expected {expected}')
            return {'public_source_samples': samples, 'physical_probe_times': list(fit[1]),
                'snap_time': snapped, 'same_point_alt_time': unsnapped, 'alt_expected': expected}
        scrub = check('physical-scrub-near-tick-snap-and-alt-bypass', scrub_and_snap)

        def keyboard():
            samples = scrub['public_source_samples']
            _probe(desktop, _fraction(fit, samples[0]))
            for symbol, expected in ((_RIGHT, samples[1]), (_RIGHT, samples[2]), (_LEFT, samples[1])):
                _tap(desktop, symbol)
                wait_for(lambda: _read_time(desktop) == expected, 'physical timeline arrow reaches exact native sample')
            _probe(desktop, _fraction(fit, lower+span*.2))
            start = _read_time(desktop)
            _tap(desktop, _SPACE)
            wait_for(lambda: _read_time(desktop) != start, 'physical Space starts playback', 4)
            _tap(desktop, _SPACE)
            paused = _stable_time(desktop)
            pump(.25)
            if _read_time(desktop) != paused:
                raise AssertionError('Second physical Space did not pause playback')
            return {'arrow_samples': samples[:3], 'space_start': start, 'space_paused_time': paused}
        check('focused-home-left-right-space-keyboard', keyboard)

        def zoom_and_pan():
            _focus(desktop); _tap(desktop, _HOME)
            before = _mapping(desktop)
            _wheel(desktop, -2)
            zoomed = _mapping(desktop)
            if not 0 < _slope(zoomed) < _slope(before)*.9:
                raise AssertionError('Physical wheel did not zoom the timeline range')
            _wheel(desktop, 12, pan=True)
            shifted = _mapping(desktop)
            if not math.isclose(_slope(shifted), _slope(zoomed), rel_tol=.02, abs_tol=tolerance):
                raise AssertionError('Shift-wheel panning unexpectedly changed timeline zoom')
            if abs(shifted[1][0]-zoomed[1][0]) <= tolerance:
                raise AssertionError('Shift-wheel did not pan the visible timeline times')
            _drag(desktop, .55, .62, middle=True)
            dragged = _mapping(desktop)
            if not math.isclose(_slope(dragged), _slope(zoomed), rel_tol=.02, abs_tol=tolerance):
                raise AssertionError('Middle-button panning unexpectedly changed timeline zoom')
            if abs(dragged[1][0]-shifted[1][0]) <= tolerance:
                raise AssertionError('Physical middle drag did not pan the timeline')
            _focus(desktop)
            time_before_home = _stable_time(desktop)
            _tap(desktop, _HOME)
            if _stable_time(desktop) != time_before_home:
                raise AssertionError('Timeline Home changed native playhead time')
            restored = _mapping(desktop)
            np.testing.assert_allclose(restored[1], before[1], rtol=0, atol=tolerance,
                err_msg='Physical Home did not restore fitted timeline mapping')
            return {'fit_probe_times': list(before[1]), 'zoom_probe_times': list(zoomed[1]),
                'shift_wheel_probe_times': list(shifted[1]), 'middle_drag_probe_times': list(dragged[1]),
                'home_restored_probe_times': list(restored[1])}
        check('physical-wheel-zoom-shift-wheel-and-middle-pan-home-fit', zoom_and_pan)

        # Choose a visible key and a destination strictly between its neighbors,
        # avoiding collisions and reordering so all other exported arrays can
        # be compared bit-for-bit.
        key_index = len(key_times)-1
        key_time = float(key_times[key_index])
        neighbor = float(key_times[key_index-1]) if key_index else lower
        destination = key_time - min(span*.1, (key_time-neighbor)*.25) if key_time > neighbor else key_time+span*.1
        if key_index+1 < len(key_times):
            destination = min(destination, (key_time+float(key_times[key_index+1]))*.5)
        start, end = _fraction(fit, key_time), _fraction(fit, destination)

        def select_other_key():
            other = 0 if key_index else 1
            _choose_public_option(desktop, 'Camera keyframes',
                [format(float(value), '.17g') for value in key_times], other)
            wait_for(lambda: _read_key_time(desktop) == float(key_times[other]),
                     'a different authored key is selected before the physical hit')

        def canceled_key():
            select_other_key()
            _drag(desktop, start, end, cancel=True, expected_key_time=key_time)
            canceled = _export_camera(desktop, output, 'timeline-camera-after-escape.gsv')
            _same_camera(baseline, canceled)
            return {'key_index': key_index, 'original_time': key_time,
                'drag_destination': destination, 'escape_before_button_release': True,
                'all_native_exported_camera_arrays_unchanged': True}
        check('selected-key-physical-drag-escape-cancel', canceled_key)

        def committed_key():
            select_other_key()
            committed_time = _drag(desktop, start, end, expected_key_time=key_time)
            changed = _export_camera(desktop, output, 'timeline-camera-after-drag.gsv')
            actual = changed['arrays'][camera['data']['times']]
            expected = key_times.copy(); expected[key_index] = committed_time
            if not math.isclose(committed_time, destination, abs_tol=tolerance) or committed_time == key_time:
                raise AssertionError('Physical key drag did not commit its visible destination')
            np.testing.assert_array_equal(actual, expected)
            expected_bundle = dict(baseline, arrays=dict(baseline['arrays'], **{camera['data']['times']: expected}))
            _same_camera(expected_bundle, changed)
            desktop.window_action('camera-undo')
            restored = _export_camera(desktop, output, 'timeline-camera-after-undo.gsv')
            _same_camera(baseline, restored)
            return {'key_index': key_index, 'before': key_time, 'after': committed_time,
                'all_other_camera_arrays_unchanged': True, 'native_undo_restored_all_arrays': True}
        check('selected-key-physical-retime-and-native-undo', committed_key)
    except BaseException as error:
        failure = error
        raise
    finally:
        restore_errors = []
        if baseline is not None:
            try:
                current = _export_camera(desktop, output, 'timeline-camera-restoration-check.gsv')
                try:
                    _same_camera(baseline, current)
                except AssertionError:
                    desktop.window_action('camera-undo')
                    current = _export_camera(desktop, output, 'timeline-camera-restoration-undo.gsv')
                    _same_camera(baseline, current)
            except Exception as error:
                restore_errors.append('camera: '+str(error))
        if ready and original_controls is not None:
            try:
                for name, value in original_controls.items():
                    _set_checked(desktop, name, value)
            except Exception as error:
                restore_errors.append('timeline controls: '+str(error))
        if ready and original_time is not None:
            try:
                _focus(desktop); _tap(desktop, _HOME)
                _seek(desktop, original_time)
            except Exception as error:
                restore_errors.append('timeline time/fit: '+str(error))
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_digest:
            restore_errors.append('source reconstruction bytes changed')
        window = desktop.window()
        if window is None or window.get_name() != source_title:
            restore_errors.append('accepted source window title changed')
        report['timeline_restoration'] = {'passed': not restore_errors,
            'source_sha256': source_digest, 'original_time': original_time, 'errors': restore_errors}
        for path in sorted(output.glob('timeline-*')):
            if path.is_file():
                report['artifacts'][path.name] = {'bytes': path.stat().st_size,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if restore_errors and failure is None:
            raise AssertionError('Timeline acceptance could not restore state: '+'; '.join(restore_errors))


def _viewport(desktop):
    candidates = []
    for node in desktop.app_nodes():
        if (node.get_process_id() == desktop.pid
                and node.get_role_name() in ('panel', 'filler', 'drawing area')
                and node.get_child_count() == 0 and showing(node)
                and node.get_name() != 'Image comparison'
                and node.get_state_set().contains(Atspi.StateType.FOCUSABLE)
                and 'Component' in node.get_interfaces()):
            bounds = Atspi.Component.get_extents(node, Atspi.CoordType.WINDOW)
            if bounds.width > 300 and bounds.height > 180:
                candidates.append(node)
    if len(candidates) != 1:
        raise TimelineNotTested('Cannot identify exactly one owned visible 3D viewport through AT-SPI')
    return candidates[0]


def _focus_viewport(desktop):
    node = _viewport(desktop)
    _point(desktop, node)
    if not desktop.click(node):
        raise TimelineNotTested('Verified owned viewport click failed; number keys were not sent')
    try:
        wait_for(lambda: _focused(node), 'owned viewport actually FOCUSED after its physical Orbit click', 3)
    except RuntimeError as error:
        raise TimelineNotTested('Viewport click did not establish native keyboard focus') from error
    return node


def _public_choice(desktop, name, options):
    node = named(desktop, name)
    values = {child.get_name() for child in walk(node) if showing(child)}
    values.add(node.get_name())
    selected = [value for value in options if value in values]
    if len(selected) != 1:
        raise TimelineNotTested('Cannot read the public selected option for '+name)
    return selected[0]


def _choose_public_option(desktop, name, options, index):
    choice = options[index]
    try:
        if _public_choice(desktop, name, options) == choice:
            return
    except TimelineNotTested:
        # A camera key dropdown legitimately starts without a selected key.
        pass
    node = named(desktop, name)
    _point(desktop, node)
    if not desktop.click(node):
        raise TimelineNotTested('Could not physically open the owned dropdown: '+name)
    wait_for(lambda: desktop.find(lambda child: child.get_process_id() == desktop.pid
        and child.get_role_name() == 'list item' and showing(child)
        and any(label.get_name() == choice for label in walk(child))),
        'owned '+name+' popup option: '+choice)
    for symbol in [_HOME] + [0xff54] * index + [0xff0d]:
        # GTK may keep accessibility focus on the combo while its popup owns
        # native keyboard routing. Require real focus somewhere in the same
        # owned application, rather than relying on an EIS call's return code.
        if not any(_focused(child) for child in desktop.app_nodes()
                   if child.get_process_id() == desktop.pid and showing(child)):
            raise TimelineNotTested(name+' popup has no confirmed owned keyboard focus')
        _tap(desktop, symbol, require_focus=False)
    def selected():
        try:
            return _public_choice(desktop, name, options) == choice
        except TimelineNotTested:
            return False
    wait_for(selected, name+' selected: '+choice)


def _navigation_choice(desktop):
    return _public_choice(desktop, 'Navigation', ('Select', 'Orbit', 'Pan', 'Dolly'))


def _choose_navigation(desktop, choice):
    options = ('Select', 'Orbit', 'Pan', 'Dolly')
    _choose_public_option(desktop, 'Navigation', options, options.index(choice))


def _pose_at(bundle, owner, native_time):
    camera = next(obj for obj in bundle['objects'] if obj['id'] == owner)
    rows = np.flatnonzero(bundle['arrays'][camera['data']['times']] == native_time)
    if len(rows) != 1:
        raise AssertionError('Native camera export omitted the exact recorded shortcut-test time')
    return bundle['arrays'][camera['data']['pose']['camera_to_world']][rows[0], 0]


def run_navigation_key_checks(desktop, output, report):
    """Prove physical 1/3/7 keys equal Front/Right/Top menu presets.

    Orbit is selected before clicking the viewport so focus cannot clear the
    authored camera target. An independent Isometric pose precedes every key,
    preventing an unimplemented shortcut from passing because the view already
    matched its reference. Records are exported and undone through public UI.
    The original camera data, viewport history position, time and tool return.
    """
    output = Path(output)
    report['navigation_key_checks'] = []
    report.setdefault('artifacts', {})
    original_tool = original_time = baseline = initial_record = None
    history_steps = 0
    failure = None
    owner = None
    source = output / 'synthetic.gsv'
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

    def editor_retained(label):
        if text(named(desktop, 'Camera name', 'EditableText')) != label:
            raise AssertionError('Physical viewport focus/key cleared the authored camera editing target')

    def record(name, label):
        editor_retained(label)
        desktop.window_action('record')
        return _export_camera(desktop, output, name)

    try:
        if getattr(desktop, 'eis', None) is None or not desktop.pointer_stream:
            raise TimelineNotTested('Viewport number-key checks require the authorized EIS backend')
        original_time = _read_time(desktop)
        original_tool = _navigation_choice(desktop)
        baseline = _export_camera(desktop, output, 'navigation-keys-camera-before.gsv')
        if not baseline['objects']:
            raise TimelineNotTested('No authored camera exists for native shortcut export evidence')
        camera = baseline['objects'][0]
        owner, label = camera['id'], camera.get('label', camera['id'])
        tab(desktop, 'Selection'); select_object(desktop, label)
        _choose_navigation(desktop, 'Orbit')
        _focus_viewport(desktop); editor_retained(label)
        initial_record = record('navigation-keys-initial-view.gsv', label)
        previous = _pose_at(initial_record, owner, original_time)
        desktop.window_action('camera-undo')
        for symbol, preset in ((ord('1'), 'front'), (ord('3'), 'right'), (ord('7'), 'top')):
            started = time.monotonic()
            identifier = 'physical-'+chr(symbol)+'-matches-menu-'+preset
            try:
                desktop.window_action(preset)
                reference = record('navigation-keys-menu-'+preset+'.gsv', label)
                pose = _pose_at(reference, owner, original_time)
                history_steps += int(not np.allclose(previous, pose, rtol=0, atol=1e-10))
                previous = pose
                desktop.window_action('camera-undo')
                desktop.window_action('isometric')
                staged = record('navigation-keys-before-'+chr(symbol)+'.gsv', label)
                isometric = _pose_at(staged, owner, original_time)
                history_steps += int(not np.allclose(previous, isometric, rtol=0, atol=1e-10))
                previous = isometric
                desktop.window_action('camera-undo')
                if np.allclose(isometric, pose, rtol=0, atol=1e-6):
                    raise AssertionError('The staging pose does not distinguish an ignored shortcut')
                viewport = _focus_viewport(desktop)
                editor_retained(label)
                if not _focused(viewport):
                    raise TimelineNotTested('Viewport lost verified focus before the actual number key')
                _tap(desktop, symbol, require_focus=False)
                actual = record('navigation-keys-physical-'+chr(symbol)+'.gsv', label)
                actual_pose = _pose_at(actual, owner, original_time)
                history_steps += int(not np.allclose(previous, actual_pose, rtol=0, atol=1e-10))
                previous = actual_pose
                np.testing.assert_allclose(actual_pose, pose, rtol=0, atol=1e-7,
                    err_msg=chr(symbol)+' did not invoke the same view as menu '+preset)
                pose_array = camera['data']['pose']['camera_to_world']
                np.testing.assert_allclose(actual['arrays'][pose_array], reference['arrays'][pose_array],
                    rtol=0, atol=1e-7, err_msg='Physical shortcut differs from menu camera poses')
                compared = dict(actual, arrays=dict(actual['arrays'], **{
                    pose_array: reference['arrays'][pose_array]}))
                _same_camera(reference, compared)
                editor_retained(label)
                desktop.window_action('camera-undo')
                report['navigation_key_checks'].append({'id': identifier, 'passed': True,
                    'tested': True, 'elapsed_seconds': round(time.monotonic()-started, 3),
                    'observed': {'physical_keysym': symbol, 'menu_preset': preset,
                        'recorded_owner': owner, 'recorded_native_time': original_time,
                        'matching_native_camera_arrays': True, 'editing_target_retained': True}})
                print('desktop navigation keys passed: '+identifier, flush=True)
            except Exception as error:
                report['navigation_key_checks'].append({'id': identifier, 'passed': False,
                    'tested': not isinstance(error, TimelineNotTested), 'reason': str(error)})
                raise
    except BaseException as error:
        failure = error
        if not report['navigation_key_checks']:
            report['navigation_key_checks'].append({'id': 'owned-orbit-viewport-keyboard-preflight',
                'passed': False, 'tested': not isinstance(error, TimelineNotTested), 'reason': str(error)})
        raise
    finally:
        errors = []
        if baseline is not None and owner is not None:
            try:
                current = _export_camera(desktop, output, 'navigation-keys-restoration-check.gsv')
                try:
                    _same_camera(baseline, current)
                except AssertionError:
                    desktop.window_action('camera-undo')
                for _ in range(history_steps):
                    desktop.window_action('previous-view')
                if initial_record is not None:
                    restored_view = record('navigation-keys-restored-view.gsv', label)
                    np.testing.assert_allclose(_pose_at(restored_view, owner, original_time),
                        _pose_at(initial_record, owner, original_time), rtol=0, atol=1e-7,
                        err_msg='Navigation shortcut checks did not restore the original viewport pose')
                    desktop.window_action('camera-undo')
                restored = _export_camera(desktop, output, 'navigation-keys-camera-restored.gsv')
                _same_camera(baseline, restored)
            except Exception as error:
                errors.append('camera/view restoration: '+str(error))
        if original_tool is not None:
            try:
                _choose_navigation(desktop, original_tool)
            except Exception as error:
                errors.append('Navigation tool: '+str(error))
        if original_time is not None:
            try:
                if _read_time(desktop) != original_time:
                    _seek(desktop, original_time)
            except Exception as error:
                errors.append('native time: '+str(error))
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_digest:
            errors.append('source reconstruction bytes changed')
        report['navigation_key_restoration'] = {'passed': not errors, 'errors': errors,
            'history_steps_restored': history_steps, 'original_tool': original_tool,
            'original_time': original_time, 'source_sha256': source_digest}
        for path in sorted(output.glob('navigation-keys-*')):
            if path.is_file():
                report['artifacts'][path.name] = {'bytes': path.stat().st_size,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if errors and failure is None:
            raise AssertionError('Navigation key acceptance could not restore state: '+'; '.join(errors))
