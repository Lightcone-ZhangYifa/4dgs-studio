#!/usr/bin/env python3
"""Exercise a real GNOME desktop, GTK accessibility and native portal dialogs.

This is an opt-in integration test, never a unit-test replacement. It neither
starts Xvfb nor mocks GTK. A disconnected GNOME session can optionally receive
one temporary Mutter virtual output; PipeWire frames go to fakesink, not a
recording. Only the tested application's own workspace/viewport are exported.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import struct
import sys
import time
import traceback
import warnings

import gi
import numpy as np
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi, Gio, GLib

ROOT = Path(__file__).resolve().parents[3]
APP_ID = 'io.github.LightconeZhangYifa.GSVStudio'
APP_PATH = '/io/github/LightconeZhangYifa/GSVStudio'
warnings.filterwarnings('ignore', category=DeprecationWarning, module=__name__)
sys.path[:0] = [str(ROOT / 'packages/gsv/src'), str(ROOT / 'viewer/ubuntu')]


def pump(seconds=.05):
    end = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        for _ in range(100):
            if not context.pending():
                break
            context.iteration(False)
        time.sleep(.005)


def wait_for(check, description, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump()
        try:
            result = check()
        except GLib.Error as error:
            if 'Object does not exist' in str(error) or 'defunct' in str(error).lower():
                continue
            raise
        if result:
            return result
    raise RuntimeError('Timed out: ' + description)


def walk(node):
    node.clear_cache()
    yield node
    try:
        for i in range(node.get_child_count()):
            yield from walk(node.get_child_at_index(i))
    except GLib.Error:
        return


def text(node):
    if 'Text' not in node.get_interfaces():
        return node.get_name()
    return Atspi.Text.get_text(node, 0, Atspi.Text.get_character_count(node))


def action(node, name=None):
    names = [Atspi.Action.get_action_name(node, i) for i in range(Atspi.Action.get_n_actions(node))]
    index = names.index(name) if name is not None else 0
    if not names or not Atspi.Action.do_action(node, index):
        raise RuntimeError(f'Accessible action failed: {name or names}')
    pump(.15)


class Desktop:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.pid = None
        self.remote = None
        self.remote_started = False
        self.pointer_stream = None
        self.allow_close = False
        self.allow_discard = False
        self.backend = None
        self.virtual_size = None
        self.maximized = False
        self.eis = None
        self.pointer_mapping_id = None

    def call(self, destination, path, interface, method, value=None):
        return self.bus.call_sync(destination, path, interface, method, value, None,
                                  Gio.DBusCallFlags.NONE, 10_000, None)

    def owner_pid(self):
        try:
            return self.call('org.freedesktop.DBus', '/org/freedesktop/DBus',
                             'org.freedesktop.DBus', 'GetConnectionUnixProcessID',
                             GLib.Variant('(s)', (APP_ID,))).unpack()[0]
        except GLib.Error as error:
            if 'NameHasNoOwner' in str(error):
                return None
            raise

    def apps(self):
        root = Atspi.get_desktop(0)
        root.clear_cache()
        return [root.get_child_at_index(i) for i in range(root.get_child_count())]

    def app(self):
        return next((a for a in self.apps() if a.get_process_id() == self.pid), None)

    def window(self):
        app = self.app()
        if app is None:
            return None
        return next((n for n in walk(app) if n.get_role_name() == 'frame' and
                     '4DGS Studio' in n.get_name()), None)

    def app_nodes(self):
        app = self.app()
        return list(walk(app)) if app is not None else []

    def find(self, predicate, root=None):
        for node in walk(root) if root else self.app_nodes():
            try:
                if predicate(node):
                    return node
            except GLib.Error as error:
                if 'Object does not exist' not in str(error) and 'defunct' not in str(error).lower():
                    raise
        return None

    def button(self, name, root=None):
        return self.find(lambda n: n.get_role_name() in ('push button', 'toggle button')
                         and n.get_name() == name, root)

    def window_action(self, name):
        action(wait_for(self.window, 'owned application window'), 'win.' + name)

    def status(self):
        node = self.find(lambda n: n.get_role_name() == 'label' and
                         re.search(r'Gaussians.*t=', n.get_name()))
        return node.get_name() if node else ''

    def current_time(self):
        match = re.search(r't=([-+.\deE]+)', self.status())
        return float(match.group(1)) if match else None

    def portal(self, title):
        # Inspect only our app or the native GNOME portal and the exact title
        # requested immediately beforehand. Never enumerate another app's UI.
        for app in self.apps():
            pid = app.get_process_id()
            try:
                portal = 'xdg-desktop-portal-gnome' in Path(f'/proc/{pid}/cmdline').read_bytes().decode()
            except (OSError, UnicodeError):
                portal = False
            if pid != self.pid and not portal:
                continue
            app.clear_cache()
            for index in range(app.get_child_count()):
                node = app.get_child_at_index(index)
                node.clear_cache()
                if (node.get_role_name() in ('dialog', 'file chooser') and node.get_name() == title
                        and node.get_state_set().contains(Atspi.StateType.SHOWING)
                        and not node.get_state_set().contains(Atspi.StateType.DEFUNCT)):
                    return node
        return None

    def keyboard_create(self):
        if self.remote:
            return
        self.remote = self.call('org.gnome.Mutter.RemoteDesktop', '/org/gnome/Mutter/RemoteDesktop',
                                'org.gnome.Mutter.RemoteDesktop', 'CreateSession').unpack()[0]

    def keyboard_start(self):
        self.keyboard_create()
        if not self.remote_started:
            self.key_call('Start')
            self.remote_started = True

    def keyboard_stop(self):
        if self.eis is not None:
            self.eis.close()
            self.eis = None
        if self.remote:
            try:
                if self.remote_started:
                    self.key_call('Stop')
            finally:
                self.remote, self.remote_started = None, False

    def key_call(self, method, value=None):
        if self.eis is not None and method.startswith('Notify'):
            return self.eis.notify(method, value, mapping_id=self.pointer_mapping_id)
        return self.call('org.gnome.Mutter.RemoteDesktop', self.remote,
                         'org.gnome.Mutter.RemoteDesktop.Session', method, value)

    def chord(self, key, control=False):
        pressed = []
        try:
            for code in ([0xffe3] if control else []) + [key]:
                self.key_call('NotifyKeyboardKeysym', GLib.Variant('(ub)', (code, True)))
                pressed.append(code)
                pump(.035)
        finally:
            for code in reversed(pressed):
                self.key_call('NotifyKeyboardKeysym', GLib.Variant('(ub)', (code, False)))
                pump(.035)

    def click(self, node):
        """Click only a control with verified owned-window/output coordinates."""
        from desktop_coordinates import owned_target_point
        point = owned_target_point(self, node)
        if point is None:
            return False
        x, y = point
        if self.pointer_stream:
            # Route clicks through Mutter, including Xwayland windows. XTEST
            # can deliver GTK events without moving the compositor's seat focus,
            # which leaves a subsequently opened Wayland portal without keys.
            self.key_call('NotifyPointerMotionAbsolute', GLib.Variant('(sdd)', (self.pointer_stream, x, y)))
            pump(.15)
            self.key_call('NotifyPointerButton', GLib.Variant('(ib)', (272, True)))
            pump(.08)
            self.key_call('NotifyPointerButton', GLib.Variant('(ib)', (272, False)))
        elif self.backend == 'x11':
            xlib = ctypes.CDLL(ctypes.util.find_library('X11'))
            xtst = ctypes.CDLL(ctypes.util.find_library('Xtst'))
            xlib.XOpenDisplay.argtypes, xlib.XOpenDisplay.restype = [ctypes.c_char_p], ctypes.c_void_p
            xlib.XFlush.argtypes = [ctypes.c_void_p]
            xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
            xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
            xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
            display = xlib.XOpenDisplay(None)
            if not display:
                raise RuntimeError('The owned Xwayland display is unavailable')
            try:
                xtst.XTestFakeMotionEvent(display, -1, round(x), round(y), 0)
                xtst.XTestFakeButtonEvent(display, 1, True, 0)
                xtst.XTestFakeButtonEvent(display, 1, False, 0)
                xlib.XFlush(display)
            finally:
                xlib.XCloseDisplay(display)
        pump(.2)
        return True

    def native_open(self, operation, path):
        title = 'Open GSV Scene' if operation == 'open' else 'Import Camera Animation'
        from desktop_coordinates import activate_owned_x11
        activate_owned_x11(self)
        opened = self.button('Open Scene…') if operation == 'open' else None
        clicked = opened is not None and self.click(opened)
        if clicked:
            try:
                wait_for(lambda: self.portal(title), 'clicked native '+title, 2)
            except RuntimeError:
                clicked = False
        if not clicked:
            self.window_action('open' if operation == 'open' else 'import-camera')
        dialog = wait_for(lambda: self.portal(title), 'native ' + title)
        self.debug_portal = dialog
        pump(.5)
        self.chord(ord('l'), control=True)
        def location_entry():
            entries = [n for n in walk(dialog) if 'EditableText' in n.get_interfaces()]
            return next((n for n in entries if n.get_name() != 'Search' and
                         (n.get_state_set().contains(Atspi.StateType.FOCUSED) or
                          n.get_state_set().contains(Atspi.StateType.SHOWING))), None)
        entry = location_entry()
        if entry is None:
            for code,state in [(29,True),(38,True),(38,False),(29,False)]:
                self.key_call('NotifyKeyboardKeycode', GLib.Variant('(ub)', (code,state)))
                pump(.04)
        entry = wait_for(location_entry, 'focused native Location field (Search is excluded)', 5)
        self.chord(ord('a'), control=True)
        for character in str(path):
            code = ord(character) if ord(character) < 128 else 0x01000000+ord(character)
            self.key_call('NotifyKeyboardKeysym', GLib.Variant('(ub)', (code, True)))
            self.key_call('NotifyKeyboardKeysym', GLib.Variant('(ub)', (code, False)))
        pump(.3)
        if text(entry) != str(path):
            raise RuntimeError('Native location input did not match the selected synthetic fixture')
        self.chord(0xff0d)
        pump(.5)
        if self.portal(title) is not None:
            opened = self.button('Open', dialog)
            opened.clear_cache()
            if opened and opened.get_state_set().contains(Atspi.StateType.SENSITIVE):
                action(opened)
            else:
                action(entry, 'activate')
                pump(.3)
                if self.portal(title) is not None:
                    self.chord(0xff0d)
        try:
            wait_for(lambda: self.portal(title) is None, 'native open completion', 5)
            self.debug_portal = None
        except RuntimeError as error:
            raise RuntimeError(f'{error}; location={text(entry)!r}; app_status={self.status()!r}') from error

    def orbit(self, backend):
        """Drag only the current owned viewport with a verified EIS mapping."""
        from desktop_coordinates import owned_target_point
        leaves = []
        for node in self.app_nodes():
            if (node.get_role_name() in ('panel', 'filler', 'drawing area') and node.get_child_count() == 0
                    and node.get_state_set().contains(Atspi.StateType.SHOWING)
                    and node.get_state_set().contains(Atspi.StateType.FOCUSABLE)
                    and 'Component' in node.get_interfaces()):
                rect = Atspi.Component.get_extents(node, Atspi.CoordType.WINDOW)
                if rect.width > 300 and rect.height > 180:
                    leaves.append((rect.width*rect.height, node, rect))
        if not leaves or not self.pointer_stream:
            raise RuntimeError('Cannot verify the owned viewport and native pointer mapping')
        _, node, rect = max(leaves, key=lambda value: value[0])
        start = owned_target_point(self, node)
        end = owned_target_point(self, node, x_fraction=.56, y_fraction=.56)
        if start is None or end is None:
            raise RuntimeError('The owned viewport coordinates are ambiguous')
        self.pointer_target = {'rect': [rect.x, rect.y, rect.width, rect.height],
                               'start': list(start), 'end': list(end)}
        self.key_call('NotifyPointerMotionAbsolute', GLib.Variant('(sdd)', (self.pointer_stream, *start)))
        pump(.15)
        self.key_call('NotifyPointerButton', GLib.Variant('(ib)', (274, True)))
        try:
            pump(.1)
            for i in range(1, 7):
                x, y = [a+(b-a)*i/6 for a,b in zip(start,end)]
                self.key_call('NotifyPointerMotionAbsolute', GLib.Variant('(sdd)', (self.pointer_stream, x, y)))
                pump(.04)
        finally:
            self.key_call('NotifyPointerButton', GLib.Variant('(ib)', (274, False)))
        pump(.3)

    def seek(self, value):
        entry = self.find(lambda n: 'EditableText' in n.get_interfaces()
                          and n.get_attributes().get('placeholder-text') == 'TIME')
        if entry is None:
            raise RuntimeError('The native timeline entry is missing')
        self.click(entry)
        # GTK 4's inner GtkText accessibility activate action does not forward
        # GtkEntry::activate on this desktop version. Use actual keyboard focus
        # traversal and Return, then verify the committed source timestamp.
        for _ in range(80):
            entry.clear_cache()
            if entry.get_state_set().contains(Atspi.StateType.FOCUSED):
                break
            self.chord(0xff09)
        else:
            raise RuntimeError('Could not focus the owned timeline entry by native Tab traversal')
        if not Atspi.EditableText.set_text_contents(entry, str(value)):
            raise RuntimeError('Timeline entry editing failed')
        self.chord(0xff0d)
        wait_for(lambda: self.current_time() == value, 'timeline entry commits native source time')

    def native_save(self, operation, title, path):
        self.window_action(operation)
        dialog = wait_for(lambda: self.portal(title), 'native ' + title)
        entries = [n for n in walk(dialog) if 'EditableText' in n.get_interfaces()]
        entry = next((n for n in entries if n.get_name() == 'Name'), None)
        entry = entry or next((n for n in entries if text(n).endswith(('.gsv', '.png'))), None)
        if entry is None:
            raise RuntimeError('Native save chooser has no identifiable Name field')
        if not Atspi.EditableText.set_text_contents(entry, str(path)):
            raise RuntimeError('Native Name field refused the selected output path')
        pump(.2)
        action(self.button('Save', dialog))
        wait_for(lambda: path.is_file(), 'native save: ' + path.name, 30)
        wait_for(lambda: self.portal(title) is None, 'native save chooser dismissal')

    def close(self):
        if self.pid and self.owner_pid() == self.pid and self.allow_close:
            self.call(APP_ID, APP_PATH, 'org.gtk.Actions', 'Activate',
                      GLib.Variant('(sava{sv})', ('quit', [], {})))
            # Only discard camera edits created in this explicit test session.
            pump(.2)
            discard = self.button('Discard') if self.allow_discard else None
            if discard:
                action(discard)
            wait_for(lambda: self.owner_pid() != self.pid, 'owned application shutdown')
        elif self.pid and self.owner_pid() == self.pid:
            cancel = self.button('Cancel')
            if cancel:
                action(cancel)

    def monitor_count(self):
        value = self.call('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
                          'org.gnome.Mutter.DisplayConfig', 'GetCurrentState').unpack()
        return len(value[2])


class VirtualOutput:
    """Session-owned native compositor output, only when no output is active."""
    def __init__(self, desktop):
        self.desktop, self.session, self.pipeline = desktop, None, None
        self.subscription = None

    def start(self):
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        self.Gst = Gst
        Gst.init(None)
        d = self.desktop
        d.keyboard_create()
        session_id = d.call('org.gnome.Mutter.RemoteDesktop', d.remote,
                            'org.freedesktop.DBus.Properties', 'Get', GLib.Variant('(ss)',
                            ('org.gnome.Mutter.RemoteDesktop.Session', 'SessionId'))).unpack()[0]
        if isinstance(session_id, GLib.Variant):
            session_id = session_id.unpack()
        self.session = d.call('org.gnome.Mutter.ScreenCast', '/org/gnome/Mutter/ScreenCast',
                              'org.gnome.Mutter.ScreenCast', 'CreateSession',
                              GLib.Variant('(a{sv})', ({'remote-desktop-session-id': GLib.Variant('s', session_id)},))).unpack()[0]
        stream = d.call('org.gnome.Mutter.ScreenCast', self.session,
                        'org.gnome.Mutter.ScreenCast.Session', 'RecordVirtual',
                        GLib.Variant('(a{sv})', ({},))).unpack()[0]
        nodes = []
        self.subscription = d.bus.signal_subscribe('org.gnome.Mutter.ScreenCast',
            'org.gnome.Mutter.ScreenCast.Stream', 'PipeWireStreamAdded', stream, None,
            Gio.DBusSignalFlags.NONE, lambda *args: nodes.append(args[5].unpack()[0]))
        d.pointer_stream = stream
        parameters = d.call('org.gnome.Mutter.ScreenCast', stream, 'org.freedesktop.DBus.Properties',
                            'Get', GLib.Variant('(ss)', ('org.gnome.Mutter.ScreenCast.Stream','Parameters'))).unpack()[0]
        if isinstance(parameters,GLib.Variant):
            parameters = parameters.unpack()
        d.pointer_mapping_id = parameters.get('mapping-id')
        if isinstance(d.pointer_mapping_id,GLib.Variant):
            d.pointer_mapping_id = d.pointer_mapping_id.unpack()
        d.keyboard_start()
        node = wait_for(lambda: nodes[0] if nodes else None, 'temporary native output stream')
        self.pipeline = Gst.parse_launch(f'pipewiresrc path={node} ! video/x-raw,format=BGRx,'
            'width=1920,height=1080,framerate=0/1,max-framerate=30/1 ! fakesink sync=false')
        self.pipeline.set_state(Gst.State.PLAYING)
        wait_for(lambda: d.monitor_count() > 0, 'temporary native logical monitor')
        d.virtual_size = (1920,1080)

    def close(self):
        try:
            self.desktop.virtual_size = None
            if self.pipeline is not None:
                self.pipeline.set_state(self.Gst.State.NULL)
        finally:
            if self.session:
                self.desktop.keyboard_stop()
                self.desktop.pointer_stream = None
                self.desktop.pointer_mapping_id = None
                self.session = None
            if self.subscription is not None:
                self.desktop.bus.signal_unsubscribe(self.subscription)


def make_fixture(path):
    spec = importlib.util.spec_from_file_location('gsv_desktop_fixture', ROOT / 'packages/gsv/examples/write_scene.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_scene(path)


def run(args):
    if not args.allow_desktop_input:
        raise RuntimeError('Pass --allow-desktop-input to explicitly allow actions in the tested desktop window.')
    if 'Xvfb' in os.environ.get('GSV_TEST_DISPLAY_SERVER', ''):
        raise RuntimeError('This tool requires a real GNOME compositor, not Xvfb')
    if args.backend == 'x11':
        match = re.fullmatch(r':(\d+)(?:\.\d+)?', os.environ.get('DISPLAY', ''))
        if not match:
            raise RuntimeError('Select the local GNOME Xwayland DISPLAY; SSH-forwarded displays are excluded')
        lock = Path('/tmp') / ('.X'+match.group(1)+'-lock')
        if lock.is_file():
            try:
                server = Path('/proc') / lock.read_text().strip() / 'cmdline'
                if b'Xvfb' in server.read_bytes():
                    raise RuntimeError('The selected DISPLAY belongs to Xvfb; use the actual GNOME display')
            except (OSError, ValueError):
                pass
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    desktop = Desktop()
    desktop.backend = args.backend
    existing = desktop.owner_pid()
    if existing and existing != args.attach_pid:
        raise RuntimeError(f'An existing Studio process ({existing}) owns the application ID. Close it, or explicitly attach to your test process with --attach-pid.')
    report = {'backend': args.backend, 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'working_tree_changes': subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True).strip().splitlines(),
              'initial_logical_monitors': desktop.monitor_count(), 'temporary_virtual_output': False,
              'steps': [], 'artifacts': {}}
    virtual = VirtualOutput(desktop)
    log = None
    try:
        if report['initial_logical_monitors'] == 0:
            if not args.virtual_output:
                raise RuntimeError('The GNOME session has no active output and keyboard focus. Connect a desktop display or explicitly pass --virtual-output.')
            virtual.start()
            report['temporary_virtual_output'] = True
        desktop.keyboard_start()
        report['input_backend'] = args.input_backend
        report['input_events_synthetic'] = True
        report['input_scope'] = 'Real Mutter compositor events and public accessibility actions on the owned test application; no manual hardware-input claim'
        if args.input_backend == 'eis':
            from desktop_eis import EISInput
            desktop.eis = EISInput.from_remote_session(desktop.bus, desktop.remote)
            report['input_devices'] = desktop.eis.diagnostics()
            if not report['temporary_virtual_output']:
                from desktop_coordinates import configure_existing_output
                report['existing_output_mapping'] = configure_existing_output(desktop)
        fixture = output / 'synthetic.gsv'
        make_fixture(fixture)
        source_digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
        if existing:
            desktop.pid = existing
        else:
            env = os.environ.copy()
            env.update(GDK_BACKEND=args.backend, PYTHONPATH=str(ROOT / 'viewer/ubuntu')+':'+str(ROOT / 'packages/gsv/src')+
                       (':'+env['PYTHONPATH'] if env.get('PYTHONPATH') else ''),
                       LANGUAGE='en_US', XDG_CONFIG_HOME=str(output / 'test-config'))
            env.pop('PYOPENGL_PLATFORM', None)
            if args.prime:
                env.update(__NV_PRIME_RENDER_OFFLOAD='1', __GLX_VENDOR_LIBRARY_NAME='nvidia',
                           __EGL_VENDOR_LIBRARY_FILENAMES='/usr/share/glvnd/egl_vendor.d/10_nvidia.json')
            python = args.python or (ROOT / '.venv/bin/python' if (ROOT / '.venv/bin/python').exists() else Path('/usr/bin/python3'))
            versions = subprocess.check_output([str(python), '-c',
                'import json,sys,OpenGL,numpy; import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); '
                'from gi.repository import Gtk,Adw; print(json.dumps({"python":sys.version.split()[0],'
                '"PyOpenGL":OpenGL.__version__,"PyOpenGL_file":OpenGL.__file__,"numpy":numpy.__version__,'
                '"Gtk":[Gtk.get_major_version(),Gtk.get_minor_version(),Gtk.get_micro_version()],'
                '"Adw":[Adw.get_major_version(),Adw.get_minor_version(),Adw.get_micro_version()]}))'],
                env=env, cwd=output, text=True)
            report['runtime_libraries'] = json.loads(versions)
            log = (output / 'application.log').open('w')
            process = subprocess.Popen([str(python), '-m', 'gs_studio'], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            desktop.pid = process.pid
            desktop.allow_close = True
        wait_for(lambda: desktop.owner_pid() == desktop.pid and desktop.window(), 'normal application launch')
        report['pid'] = desktop.pid
        desktop.keyboard_start()
        if not existing:
            action(desktop.window(), 'window.toggle-maximized')
            desktop.maximized = True
            pump(.5)
        from desktop_coordinates import activate_owned_x11
        report['initial_window_activated'] = activate_owned_x11(desktop)
        desktop.native_open('open', fixture)
        desktop.allow_close = True
        wait_for(lambda: desktop.current_time() == 0, 'source scene loaded')
        report['steps'].append('native-open-dialog')
        desktop.window_action('play')
        wait_for(lambda: desktop.current_time() is not None and desktop.current_time() > 0, 'playback advances source time')
        desktop.window_action('play')
        report['steps'].append('play-pause')
        row = desktop.find(lambda n: n.get_role_name() == 'list item' and any(
            c.get_role_name() == 'label' and c.get_name() == 'moving-points' for c in walk(n)))
        if row is None:
            raise RuntimeError('Synthetic Gaussian field is missing from the native outliner')
        if not Atspi.Selection.select_child(row.get_parent(), row.get_index_in_parent()):
            raise RuntimeError('Native outliner selection action failed')
        pump(.5)
        report['steps'].append('outliner-field-selection-and-path-request')
        action(desktop.button('First sample'))
        wait_for(lambda: desktop.current_time() == 0, 'first source sample')
        desktop.window_action('create-camera')
        desktop.allow_discard = True
        test_orbit = bool(desktop.pointer_stream) and (args.backend == 'x11' or args.test_orbit)
        report['orbit_input_tested'] = False
        if test_orbit:
            desktop.orbit(args.backend)
            report['pointer_target'] = desktop.pointer_target
        desktop.seek(.5)
        desktop.window_action('record')
        camera = output / 'authored-cameras.gsv'
        desktop.native_save('save', 'Save Camera Animation', camera)
        from gsv import Scene
        with Scene(camera) as scene:
            scene.validate()
            times = scene.array(scene.objects[0]['data']['times']).tolist()
            if times != [0., .5]:
                raise AssertionError(f'Actual camera export has incorrect recorded times: {times}')
            poses = scene.array(scene.objects[0]['data']['pose']['camera_to_world']).copy()
            if test_orbit:
                if np.allclose(poses[0], poses[1]):
                    raise AssertionError('Native viewport drag did not change the recorded camera pose')
                report['orbit_input_tested'] = True
                report['steps'].append('native-middle-drag-orbit-proved-by-exported-poses')
        report['steps'].append('two-key-camera-authoring-and-native-save')
        desktop.window_action('camera-undo')
        desktop.window_action('camera-redo')
        action(desktop.button('Delete camera'))
        desktop.native_open('import', camera)
        wait_for(lambda: desktop.find(lambda n: n.get_role_name() == 'label' and n.get_name() == 'Camera 1'),
                 'imported camera appears in native outliner')
        roundtrip = output / 'roundtrip-cameras.gsv'
        desktop.native_save('save', 'Save Camera Animation', roundtrip)
        with Scene(camera) as before, Scene(roundtrip) as after:
            before.validate()
            after.validate()
            if before.objects[0]['id'] != after.objects[0]['id']:
                raise AssertionError('Native camera import changed camera identity')
            for key in ('times',):
                np.testing.assert_array_equal(before.array(before.objects[0]['data'][key]),
                                              after.array(after.objects[0]['data'][key]))
            np.testing.assert_array_equal(before.array(before.objects[0]['data']['pose']['camera_to_world']),
                                          after.array(after.objects[0]['data']['pose']['camera_to_world']))
        report['steps'].append('native-camera-undo-redo-delete-import-roundtrip')
        if args.full:
            if not desktop.maximized:
                action(desktop.window(), 'window.toggle-maximized')
                desktop.maximized = True
                pump(.3)
            report['requested_full_checks'] = args.full_checks
            if 'parity' in args.full_checks:
                from desktop_parity import run_full
                run_full(desktop, output, report)
            if 'timeline' in args.full_checks:
                from desktop_timeline_checks import run_timeline_checks
                run_timeline_checks(desktop, output, report)
            if 'navigation' in args.full_checks:
                from desktop_timeline_checks import run_navigation_key_checks
                run_navigation_key_checks(desktop, output, report)
            if 'comparison' in args.full_checks:
                from desktop_comparison_checks import run_comparison_checks
                run_comparison_checks(desktop, output, report)
            if 'lifecycle' in args.full_checks:
                from desktop_lifecycle_checks import run_lifecycle_checks
                run_lifecycle_checks(desktop, output, report)
        workspace = output / 'workspace.png'
        desktop.native_save('workspace-snapshot', 'Save Workspace Snapshot…', workspace)
        report['steps'].append('native-workspace-png-save')
        action(desktop.button('Render / GT'))
        score = wait_for(lambda: desktop.find(lambda n: n.get_role_name() == 'label' and
                         re.search(r'PSNR\s+[-+\d]', n.get_name())),
                         'actual calibrated comparison metrics', 30)
        report['comparison_metrics'] = score.get_name()
        comparison = output / 'comparison.png'
        desktop.native_save('screenshot', 'Save Screenshot…', comparison)
        size = struct.unpack('>II', comparison.read_bytes()[16:24])
        if size != (1280, 480):
            raise AssertionError(f'Comparison export changed native image dimensions: {size}')
        report['steps'].append('render-reference-metrics-and-native-resolution-png')
        desktop.window_action('graphics-information')
        pump(.3)
        labels = [n.get_name() for n in desktop.app_nodes() if n.get_role_name() == 'label' and
                  any(word in n.get_name() for word in ('OpenGL', 'Vendor', 'Renderer', 'Protocol', 'NVIDIA', 'llvmpipe'))]
        report['graphics_information'] = labels
        close = desktop.button('OK')
        if close:
            action(close)
        if hashlib.sha256(fixture.read_bytes()).hexdigest() != source_digest:
            raise AssertionError('The source reconstruction changed during the native workflow')
        report['source_unchanged'] = True
        report['passed'] = True
        for path in (fixture, camera, roundtrip, workspace, comparison):
            report['artifacts'][path.name] = {'bytes': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        return report
    except Exception as error:
        report.update(passed=False, error=str(error), traceback=traceback.format_exc())
        if args.inspect_failure:
            print('Owned desktop inspection pause: '+str(error), flush=True)
            pump(min(60, args.inspect_failure))
        if getattr(desktop,'debug_portal',None) is not None:
            try:
                controls = [{'name':n.get_name(),'role':n.get_role_name(), 'states':[s.value_nick for s in n.get_state_set().get_states()],
                             'description':n.get_description(), 'actions':[Atspi.Action.get_action_name(n,i) for i in range(Atspi.Action.get_n_actions(n))]}
                            for n in walk(desktop.debug_portal) if n.get_role_name() in
                            ('push button','toggle button','text','combo box','dialog','file chooser','menu button')]
                (output/'portal-controls.json').write_text(json.dumps(controls,indent=2,ensure_ascii=False)+'\n')
            except Exception:
                pass
        try:
            nodes = [{'name': node.get_name(), 'role': node.get_role_name(),
                      'description': node.get_description(), 'attributes': node.get_attributes(),
                      'interfaces': node.get_interfaces(),
                      'states': [state.value_nick for state in node.get_state_set().get_states()]}
                     for node in desktop.app_nodes()]
            (output / 'accessibility.json').write_text(json.dumps(nodes, indent=2, ensure_ascii=False)+'\n')
        except Exception:
            pass
        return report
    finally:
        try:
            desktop.close()
        except Exception as error:
            report['cleanup_error'] = str(error)
            report['passed'] = False
        finally:
            try:
                virtual.close()
                desktop.keyboard_stop()
                if report['temporary_virtual_output']:
                    wait_for(lambda: desktop.monitor_count() == report['initial_logical_monitors'],
                             'temporary output removal')
                report['final_logical_monitors'] = desktop.monitor_count()
            except Exception as error:
                report['output_cleanup_error'] = str(error)
                report['passed'] = False
            finally:
                if log:
                    log.close()
                (output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
                print(json.dumps(report, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-desktop-input', action='store_true')
    parser.add_argument('--backend', choices=['wayland', 'x11'], required=True)
    parser.add_argument('--output', type=Path, required=True, help='New directory for synthetic fixtures, exports and JSON evidence')
    parser.add_argument('--attach-pid', type=int, help='Explicitly replace documents in this owned test process; never attach to user work')
    parser.add_argument('--virtual-output', action='store_true', help='When GNOME has zero outputs, create one session-owned output and remove it on exit')
    parser.add_argument('--prime', action='store_true', help='Select NVIDIA PRIME offload for this process only')
    parser.add_argument('--test-orbit', action='store_true', help='Also require the verified native pointer drag on Wayland; mapped Xwayland runs always require it')
    parser.add_argument('--python', type=Path)
    parser.add_argument('--input-backend', choices=('eis','legacy'), default='eis', help='Native Mutter EIS/libei input, or the legacy RemoteDesktop notifications')
    parser.add_argument('--full', action='store_true', help='Also exercise inspector pages, all transport commands, camera fields, key editing, preview settings, resolution exports and workbench commands')
    parser.add_argument('--full-checks', nargs='+', choices=('parity','timeline','navigation','comparison','lifecycle'),
                        default=['parity','timeline','navigation','comparison','lifecycle'], help='Run selected expanded groups; the report records this scope explicitly')
    parser.add_argument('--inspect-failure', type=int, default=0, help='Keep the owned test UI for up to 60 seconds after failure, then perform normal cleanup')
    args = parser.parse_args()
    try:
        return 0 if run(args)['passed'] else 1
    except Exception as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
