"""Native cancellation, error recovery and isolated preference acceptance.

This module only drives the owned application's public AT-SPI/keyboard UI.
The runner must launch that process with XDG_CONFIG_HOME=output/test-config;
preference edits are refused for any other configuration directory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import gi
import numpy as np

gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Atspi, GdkPixbuf

from desktop_parity import activate, choose, edit, named, save_camera, select_object, showing, tab
from test_desktop_session import pump, text, wait_for


def _owned_preference_window(desktop):
    return desktop.find(lambda node: node.get_name() == 'Preferences'
                        and node.get_role_name() in ('frame', 'dialog', 'window')
                        and showing(node))


def _close_preferences(desktop):
    desktop.chord(0xff1b)
    wait_for(lambda: _owned_preference_window(desktop) is None, 'native Preferences window closes')


def _isolated_config(desktop, output):
    environment = {}
    for item in Path(f'/proc/{desktop.pid}/environ').read_bytes().split(b'\0'):
        key, separator, value = item.partition(b'=')
        if separator:
            environment[key.decode(errors='replace')] = value.decode(errors='replace')
    expected = (output / 'test-config').resolve()
    configured = environment.get('XDG_CONFIG_HOME')
    if configured is None or Path(configured).resolve() != expected:
        raise RuntimeError('Preference acceptance requires this owned process to use '+str(expected)+
                           '; no user configuration was modified')
    path = expected / '4dgs-studio' / 'preferences.json'
    if not path.resolve().is_relative_to(expected):
        raise RuntimeError('Refusing a test preference path that resolves outside the isolated directory')
    return path


def _preferences(path):
    if not path.exists():
        return {}
    values = json.loads(path.read_text())
    if not isinstance(values, dict):
        raise AssertionError('Preferences did not persist as a JSON object')
    return values


def _same_camera(before, after):
    for key in ('id', 'label'):
        if before[key] != after[key]:
            raise AssertionError('Canceled/failed operation changed camera '+key)
    for key in ('times', 'poses', 'K', 'image_sizes'):
        np.testing.assert_array_equal(before[key], after[key],
                                      err_msg='Canceled/failed operation changed authored camera '+key)


def _rgb(path):
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
    return np.ndarray((pixbuf.get_height(), pixbuf.get_width(), pixbuf.get_n_channels()),
                      np.uint8, buffer=pixbuf.get_pixels(),
                      strides=(pixbuf.get_rowstride(), pixbuf.get_n_channels(), 1))[:, :, :3].copy()


def run_lifecycle_checks(desktop, output, report):
    """Append ``lifecycle_checks``; keep the source and original camera selected.

    Must run after the standard camera-authoring roundtrip, with the normal 3D
    view active. Cancel/error dialogs and the native file chooser are never
    replaced or mocked. The existing test camera is restored after the dirty
    close check; all outputs and preference edits remain inside ``output``.
    """
    output = Path(output)
    config = _isolated_config(desktop, output)
    report['lifecycle_checks'] = []
    report.setdefault('artifacts', {})
    source = output / 'synthetic.gsv'
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    baseline = save_camera(desktop, output, 'lifecycle-camera-before.gsv')
    title = desktop.window().get_name()

    def check(identifier, operation):
        started = time.monotonic()
        observation = operation()
        report['lifecycle_checks'].append({'id': identifier, 'passed': True,
            'elapsed_seconds': round(time.monotonic()-started, 3), 'observed': observation})
        print('desktop lifecycle passed: '+identifier, flush=True)

    def canceled_open():
        before_time = text(named(desktop, 'TIME', 'EditableText'))
        desktop.window_action('open')
        dialog = wait_for(lambda: desktop.portal('Open GSV Scene'), 'native Open GSV Scene chooser')
        cancel = wait_for(lambda: desktop.button('Cancel', dialog), 'native open chooser Cancel')
        activate(desktop, cancel)
        wait_for(lambda: desktop.portal('Open GSV Scene') is None, 'native open cancellation')
        if desktop.window().get_name() != title:
            raise AssertionError('Canceling Open changed the accepted source window')
        if text(named(desktop, 'TIME', 'EditableText')) != before_time:
            raise AssertionError('Canceling Open changed the playhead')
        after = save_camera(desktop, output, 'lifecycle-camera-after-open-cancel.gsv')
        _same_camera(baseline, after)
        return {'source_title': title, 'time_text': before_time, 'camera_arrays_unchanged': True}
    check('native-open-cancel-preserves-source-time-and-camera', canceled_open)

    def failed_open():
        corrupt = output / 'lifecycle-corrupt.gsv'
        damaged = bytearray(source.read_bytes())
        damaged[0] ^= 0x80
        corrupt.write_bytes(damaged)
        desktop.native_open('open', corrupt)
        wait_for(lambda: desktop.find(lambda node: node.get_name() == 'Scene operation failed'
                                     and showing(node)), 'owned failed-open error dialog')
        error_labels = [node.get_name() for node in desktop.app_nodes()
                        if node.get_role_name() == 'label' and showing(node)]
        activate(desktop, wait_for(lambda: desktop.button('OK'), 'owned failed-open dialog OK'))
        if desktop.window().get_name() != title:
            raise AssertionError('A corrupt file replaced the accepted document')
        select_object(desktop, 'moving-points')
        after = save_camera(desktop, output, 'lifecycle-camera-after-failed-open.gsv')
        _same_camera(baseline, after)
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise AssertionError('Failed-open recovery changed the accepted source archive')
        return {'error_dialog_visible': True, 'visible_error_labels': error_labels,
                'accepted_source_retained': True, 'camera_arrays_unchanged': True}
    check('corrupt-native-open-retains-accepted-source-and-camera', failed_open)

    def canceled_dirty_close():
        tab(desktop, 'Selection')
        select_object(desktop, baseline['label'])
        edited_name = 'Lifecycle acceptance camera'
        edit(desktop, 'Camera name', edited_name)
        desktop.window_action('close')
        cancel = wait_for(lambda: desktop.button('Cancel'), 'owned dirty-close confirmation Cancel')
        if desktop.button('Discard') is None or desktop.button('Save Camera Animation…') is None:
            raise AssertionError('Dirty close did not expose Cancel, Discard and Save choices')
        activate(desktop, cancel)
        wait_for(lambda: desktop.button('Cancel') is None, 'dirty-close confirmation dismissed')
        if desktop.owner_pid() != desktop.pid or desktop.window() is None:
            raise AssertionError('Canceling dirty close terminated the owned application')
        retained = save_camera(desktop, output, 'lifecycle-camera-after-close-cancel.gsv')
        expected = dict(baseline, label=edited_name)
        _same_camera(expected, retained)
        select_object(desktop, edited_name)
        edit(desktop, 'Camera name', baseline['label'])
        restored = save_camera(desktop, output, 'lifecycle-camera-name-restored.gsv')
        _same_camera(baseline, restored)
        return {'close_canceled': True, 'unsaved_name_retained_in_native_export': edited_name,
                'camera_arrays_unchanged': True, 'original_name_restored': True}
    check('dirty-window-close-cancel-retains-unsaved-camera', canceled_dirty_close)

    def preferences():
        before = _preferences(config)
        original_language = before.get('language', 'system')
        original_appearance = before.get('appearance', 0)
        desktop.window_action('preferences')
        wait_for(lambda: _owned_preference_window(desktop), 'native Preferences window')
        choose(desktop, 'Language', 2, '简体中文')
        wait_for(lambda: _preferences(config).get('language') == 'zh-Hans', 'isolated Chinese preference persisted')
        if desktop.window().get_name() != title:
            raise AssertionError('Changing restart-only language replaced or closed the current source')
        choose(desktop, 'Appearance', 2, 'Dark')
        wait_for(lambda: _preferences(config).get('appearance') == 2, 'isolated dark preference persisted')
        _close_preferences(desktop)
        dark = output / 'lifecycle-dark-workspace.png'
        desktop.native_save('workspace-snapshot', 'Save Workspace Snapshot…', dark)
        desktop.window_action('preferences')
        wait_for(lambda: _owned_preference_window(desktop), 'reopened native Preferences window')
        choose(desktop, 'Language', 1, 'English')
        wait_for(lambda: _preferences(config).get('language') == 'en', 'isolated English preference persisted')
        choose(desktop, 'Appearance', 1, 'Light')
        wait_for(lambda: _preferences(config).get('appearance') == 1, 'isolated light preference persisted')
        _close_preferences(desktop)
        light = output / 'lifecycle-light-workspace.png'
        desktop.native_save('workspace-snapshot', 'Save Workspace Snapshot…', light)
        dark_rgb, light_rgb = _rgb(dark), _rgb(light)
        if dark_rgb.shape != light_rgb.shape:
            raise AssertionError('Changing appearance changed workspace dimensions')
        changed_pixels = int(np.count_nonzero(np.any(dark_rgb != light_rgb, axis=2)))
        if changed_pixels < dark_rgb.shape[0]*dark_rgb.shape[1]*.05:
            raise AssertionError('Dark and Light did not visibly change the actual workbench')
        if float(light_rgb.mean()) <= float(dark_rgb.mean())+5:
            raise AssertionError('The actual Light workbench was not brighter than Dark')
        # Restore using the same native preference controls, leaving no direct
        # test-side configuration writes and no dependency on the user's theme.
        desktop.window_action('preferences')
        wait_for(lambda: _owned_preference_window(desktop), 'Preferences window for restoration')
        languages = ['system', 'en', 'zh-Hans']
        choose(desktop, 'Language', languages.index(original_language),
               ['System', 'English', '简体中文'][languages.index(original_language)])
        choose(desktop, 'Appearance', original_appearance, ['System', 'Light', 'Dark'][original_appearance])
        _close_preferences(desktop)
        restored = _preferences(config)
        if (restored.get('language'), restored.get('appearance')) != (original_language, original_appearance):
            raise AssertionError('Native preference restoration did not persist')
        return {'isolated_config': str(config), 'language_choices_persisted': ['zh-Hans', 'en'],
                'language_change_requires_restart_and_retains_source': True,
                'dark_mean_rgb': float(dark_rgb.mean()), 'light_mean_rgb': float(light_rgb.mean()),
                'changed_workspace_pixels': changed_pixels, 'original_preferences_restored': True}
    check('isolated-native-language-and-live-appearance-preferences', preferences)

    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise AssertionError('Lifecycle checks changed the source reconstruction')
    for path in sorted(output.glob('lifecycle-*')):
        if path.is_file():
            report['artifacts'][path.name] = {'bytes': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
