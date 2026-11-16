"""Optional libei sender for the explicitly authorized desktop test runner.

Importing this module does not connect to a desktop or generate input. The
caller owns the already-started Mutter RemoteDesktop session and all target
ownership/focus checks. ``EISInput.from_remote_session(desktop.bus,
desktop.remote)`` attaches to that same session; it never creates an output,
starts/stops a session, or changes desktop settings.

The ctypes ABI and enum values below match Ubuntu's libei 1.2.1 headers.
Protocol references:
https://gitlab.gnome.org/GNOME/mutter/-/blob/46.2/data/dbus-interfaces/org.gnome.Mutter.RemoteDesktop.xml
https://libinput.pages.freedesktop.org/libei/api/group__libei-sender.html

Use ``notify(method, variant)`` for the runner's Notify* subset. Absolute
coordinates are EIS-region logical coordinates, normally desktop coordinates;
when using stream-local regions, pass the matching region mapping ID explicitly.
No inference from monitor dimensions is made. A ready connection alone does not
prove focus or delivery: the runner must still verify its owned target's state.
"""
from __future__ import annotations

import ctypes as C
import ctypes.util
from dataclasses import dataclass, field
import math
import os
import select
import time


POINTER, ABSOLUTE, KEYBOARD, TOUCH, SCROLL, BUTTON = (1 << n for n in range(6))
CONNECT, DISCONNECT, SEAT_ADDED, SEAT_REMOVED = 1, 2, 3, 4
DEVICE_ADDED, DEVICE_REMOVED, DEVICE_PAUSED, DEVICE_RESUMED, KEYBOARD_MODIFIERS = range(5, 10)
_CAPS = (POINTER, ABSOLUTE, KEYBOARD, SCROLL, BUTTON)


class EISError(RuntimeError):
    """The server did not supply a usable input device or rejected the stream."""


def _bind(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype, function.argtypes = result, list(arguments)
    return function


class _Libei:
    def __init__(self):
        self.lib = C.CDLL(ctypes.util.find_library('ei') or 'libei.so.1')
        p, u, b, d = C.c_void_p, C.c_uint32, C.c_bool, C.c_double
        functions = {
            'ei_new_sender': (p, p), 'ei_configure_name': (None, p, C.c_char_p),
            'ei_setup_backend_fd': (C.c_int, p, C.c_int), 'ei_get_fd': (C.c_int, p),
            'ei_dispatch': (None, p), 'ei_get_event': (p, p), 'ei_unref': (p, p),
            'ei_now': (C.c_uint64, p), 'ei_event_unref': (p, p),
            'ei_event_get_type': (C.c_int, p), 'ei_event_get_seat': (p, p),
            'ei_event_get_device': (p, p), 'ei_seat_has_capability': (b, p, C.c_int),
            'ei_device_ref': (p, p), 'ei_device_unref': (p, p),
            'ei_device_has_capability': (b, p, C.c_int),
            'ei_device_get_name': (C.c_char_p, p),
            'ei_device_get_region': (p, p, C.c_size_t),
            'ei_device_get_region_at': (p, p, d, d),
            'ei_region_get_x': (u, p), 'ei_region_get_y': (u, p),
            'ei_region_get_width': (u, p), 'ei_region_get_height': (u, p),
            'ei_region_get_mapping_id': (C.c_char_p, p),
            'ei_region_get_physical_scale': (d, p),
            'ei_device_start_emulating': (None, p, u),
            'ei_device_stop_emulating': (None, p),
            'ei_device_frame': (None, p, C.c_uint64),
            'ei_device_keyboard_key': (None, p, u, b),
            'ei_device_pointer_motion': (None, p, d, d),
            'ei_device_pointer_motion_absolute': (None, p, d, d),
            'ei_device_button_button': (None, p, u, b),
            'ei_device_scroll_delta': (None, p, d, d),
            'ei_device_scroll_discrete': (None, p, C.c_int32, C.c_int32),
            'ei_device_scroll_stop': (None, p, b, b),
            'ei_device_keyboard_get_keymap': (p, p),
            'ei_keymap_get_size': (C.c_size_t, p), 'ei_keymap_get_type': (C.c_int, p),
            'ei_keymap_get_fd': (C.c_int, p),
        }
        for name in ('depressed', 'latched', 'locked'):
            functions['ei_event_keyboard_get_xkb_mods_' + name] = (u, p)
        functions['ei_event_keyboard_get_xkb_group'] = (u, p)
        for name, signature in functions.items():
            setattr(self, name.removeprefix('ei_'), _bind(self.lib, name, *signature))
        # Variadic API: each enum is a separate promoted int, terminated by a
        # NULL pointer. An ORed mask here is not the seat-binding protocol.
        self.seat_bind_capabilities = _bind(self.lib, 'ei_seat_bind_capabilities', None, p)


class _Keymap:
    """Resolve symbols against the device keymap, rather than assuming US keys."""
    def __init__(self, raw):
        self.lib = C.CDLL(ctypes.util.find_library('xkbcommon') or 'libxkbcommon.so.0')
        p, u = C.c_void_p, C.c_uint32
        signatures = {
            'context_new': (p, C.c_int), 'context_unref': (None, p),
            'keymap_new_from_buffer': (p, p, C.c_char_p, C.c_size_t, C.c_int, C.c_int),
            'keymap_unref': (None, p), 'state_new': (p, p), 'state_unref': (None, p),
            'state_update_mask': (u, p, u, u, u, u, u, u),
            'state_update_key': (u, p, u, C.c_int),
            'state_serialize_mods': (u, p, C.c_int),
            'state_serialize_layout': (u, p, C.c_int),
            'state_key_get_one_sym': (u, p, u),
            'keymap_min_keycode': (u, p), 'keymap_max_keycode': (u, p),
            'keymap_num_levels_for_key': (u, p, u, u),
            'keymap_key_get_syms_by_level': (C.c_int, p, u, u, u, C.POINTER(C.POINTER(u))),
            'keymap_key_get_mods_for_level': (C.c_size_t, p, u, u, u, C.POINTER(u), C.c_size_t),
        }
        for name, signature in signatures.items():
            setattr(self, name, _bind(self.lib, 'xkb_' + name, *signature))
        self.context = self.context_new(0)
        self.keymap = self.state = None
        try:
            if not self.context:
                raise EISError('Cannot create the XKB context')
            data = raw.rstrip(b'\0')
            self.keymap = self.keymap_new_from_buffer(self.context, data, len(data), 1, 0)
            self.state = self.state_new(self.keymap) if self.keymap else None
            if not self.state:
                raise EISError('The EIS keyboard supplied an invalid XKB keymap')
            self.minimum = self.keymap_min_keycode(self.keymap)
            self.maximum = self.keymap_max_keycode(self.keymap)
            self._modifier_keys = {}
            # A new temporary state identifies which modifier a physical key
            # depresses. Lock/toggle keys are deliberately excluded.
            temporary = self.state_new(self.keymap)
            try:
                for key in range(max(8, self.minimum), self.maximum + 1):
                    self.state_update_key(temporary, key, 1)
                    depressed = self.state_serialize_mods(temporary, 1)
                    locked = self.state_serialize_mods(temporary, 4)
                    self.state_update_key(temporary, key, 0)
                    if locked:
                        self.state_update_mask(temporary, 0, 0, 0, 0, 0, 0)
                    if depressed and not locked:
                        self._modifier_keys.setdefault(depressed, key - 8)
            finally:
                self.state_unref(temporary)
        except BaseException:
            self.close()
            raise

    def update_modifiers(self, depressed, latched, locked, group):
        self.state_update_mask(self.state, depressed, latched, locked, 0, 0, group)

    def update_key(self, evdev_code, pressed):
        self.state_update_key(self.state, evdev_code + 8, int(pressed))

    def resolve(self, symbol, synthetic=()):
        layout = self.state_serialize_layout(self.state, 1 << 7)
        effective = self.state_serialize_mods(self.state, 1 << 3)
        candidates = []
        for key in range(max(8, self.minimum), self.maximum + 1):
            for level in range(self.keymap_num_levels_for_key(self.keymap, key, layout)):
                symbols = C.POINTER(C.c_uint32)()
                size = self.keymap_key_get_syms_by_level(self.keymap, key, layout, level, C.byref(symbols))
                if size != 1 or symbols[0] != symbol:
                    continue
                masks = (C.c_uint32 * 32)()
                count = self.keymap_key_get_mods_for_level(self.keymap, key, layout, level, masks, len(masks))
                for required in masks[:count]:
                    missing = required & ~effective
                    extras = []
                    for mask, code in self._modifier_keys.items():
                        if mask & required == mask and (mask & missing == mask or code in synthetic):
                            extras.append(code)
                            missing &= ~mask
                    if missing:
                        continue
                    probe = self.state_new(self.keymap)
                    try:
                        self.state_update_mask(probe, effective | required, 0, 0, 0, 0, layout)
                        if self.state_key_get_one_sym(probe, key) == symbol:
                            candidates.append((len(extras), key - 8, tuple(extras)))
                    finally:
                        self.state_unref(probe)
        if not candidates:
            raise EISError(f'Keysym 0x{symbol:x} cannot be typed in the current EIS keymap/state')
        _, code, extra = min(candidates)
        return code, extra

    def close(self):
        if self.state:
            self.state_unref(self.state)
            self.state = None
        if self.keymap:
            self.keymap_unref(self.keymap)
            self.keymap = None
        if self.context:
            self.context_unref(self.context)
            self.context = None


@dataclass
class _Device:
    pointer: int
    capabilities: frozenset[int]
    name: str
    resumed: bool = False
    emulating: bool = False
    keymap: _Keymap | None = None
    keys: set[int] = field(default_factory=set)
    buttons: set[int] = field(default_factory=set)
    generation: int = 0


class EISInput:
    """Single-threaded sender. Constructing it consumes the connected socket FD.

    ``dispatch()`` must also be called while idle to observe device suspension.
    Each notify method dispatches before sending and emits a monotonic frame.
    Device availability and coordinates are validated before input is emitted.
    """
    def __init__(self, fd, *, timeout=5., name='4DGS Studio desktop test'):
        self.api = _Libei()
        self.context = self.api.new_sender(None)
        self.devices = {}
        self.connected = self.disconnected = False
        self.events = []
        self._sequence = 0
        self._symbols = {}
        self._pointer_device = None
        self._absolute_pin = None
        self._device_generation = 0
        if not self.context:
            os.close(fd)
            raise EISError('Cannot create the libei sender')
        try:
            self.api.configure_name(self.context, name.encode())
            # setup_backend_fd takes FD ownership, including teardown.
            result = self.api.setup_backend_fd(self.context, fd)
            if result < 0:
                raise OSError(-result, os.strerror(-result))
            self.wait_ready(timeout=timeout)
        except BaseException:
            self.close()
            raise

    @classmethod
    def from_remote_session(cls, bus, session_path, *, timeout=5.):
        """Connect to an existing session on its original Gio bus connection.

        Mutter binds the session to the D-Bus unique sender; using a new bus
        connection, a new session, or interpreting the handle as an FD is wrong.
        """
        from gi.repository import Gio, GLib
        reply, descriptors = bus.call_with_unix_fd_list_sync(
            'org.gnome.Mutter.RemoteDesktop', session_path,
            'org.gnome.Mutter.RemoteDesktop.Session', 'ConnectToEIS',
            GLib.Variant('(a{sv})', ({'device-types': GLib.Variant('u', 3)},)),
            GLib.VariantType.new('(h)'), Gio.DBusCallFlags.NONE,
            max(1, round(timeout * 1000)), None, None)
        if descriptors is None:
            raise EISError('ConnectToEIS did not return a Unix FD list')
        fd = descriptors.get(reply.unpack()[0])  # get() returns our owned dup
        return cls(fd, timeout=timeout)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _remove_device(self, pointer):
        device = self.devices.pop(pointer, None)
        if device is not None:
            if device.keymap:
                device.keymap.close()
            self.api.device_unref(pointer)
        self._symbols = {symbol: item for symbol, item in self._symbols.items() if item[0] != pointer}
        if self._pointer_device == pointer:
            self._pointer_device = None

    def dispatch(self, timeout=0.):
        if not self.context or self.disconnected:
            raise EISError('The EIS connection is closed')
        if timeout > 0:
            select.select([self.api.get_fd(self.context)], [], [], timeout)
        self.api.dispatch(self.context)
        while event := self.api.get_event(self.context):
            try:
                kind = self.api.event_get_type(event)
                self.events.append(kind)
                self.events[:] = self.events[-64:]
                if kind == CONNECT:
                    self.connected = True
                elif kind == DISCONNECT:
                    self.disconnected = True
                elif kind == SEAT_ADDED:
                    seat = self.api.event_get_seat(event)
                    caps = [C.c_int(cap) for cap in _CAPS if self.api.seat_has_capability(seat, cap)]
                    self.api.seat_bind_capabilities(seat, *caps, C.c_void_p())
                elif kind == DEVICE_ADDED:
                    pointer = self.api.event_get_device(event)
                    self.api.device_ref(pointer)
                    caps = frozenset(cap for cap in _CAPS if self.api.device_has_capability(pointer, cap))
                    name = (self.api.device_get_name(pointer) or b'').decode(errors='replace')
                    self._device_generation += 1
                    self.devices[pointer] = _Device(pointer, caps, name, generation=self._device_generation)
                elif kind == DEVICE_REMOVED:
                    self._remove_device(self.api.event_get_device(event))
                elif kind in (DEVICE_PAUSED, DEVICE_RESUMED):
                    device = self.devices[self.api.event_get_device(event)]
                    device.resumed = kind == DEVICE_RESUMED
                    if device.resumed:
                        self._sequence = (self._sequence + 1) & 0xffffffff
                        self.api.device_start_emulating(device.pointer, self._sequence)
                        device.emulating = True
                        if KEYBOARD in device.capabilities and device.keymap is None:
                            keymap = self.api.device_keyboard_get_keymap(device.pointer)
                            if keymap and self.api.keymap_get_type(keymap) == 1:
                                raw = os.pread(self.api.keymap_get_fd(keymap), self.api.keymap_get_size(keymap), 0)
                                device.keymap = _Keymap(raw)
                    else:
                        # Pause cancels server-side input state. Never replay a
                        # held key into a resumed or replacement device.
                        device.emulating = False
                        device.keys.clear(); device.buttons.clear()
                        self._symbols = {s: item for s, item in self._symbols.items() if item[0] != device.pointer}
                elif kind == KEYBOARD_MODIFIERS:
                    device = self.devices.get(self.api.event_get_device(event))
                    if device and device.keymap:
                        device.keymap.update_modifiers(
                            self.api.event_keyboard_get_xkb_mods_depressed(event),
                            self.api.event_keyboard_get_xkb_mods_latched(event),
                            self.api.event_keyboard_get_xkb_mods_locked(event),
                            self.api.event_keyboard_get_xkb_group(event))
            finally:
                self.api.event_unref(event)
        if self.disconnected:
            raise EISError('The EIS server disconnected the sender')

    def wait_ready(self, timeout=5., capabilities=(KEYBOARD, ABSOLUTE, BUTTON)):
        end = time.monotonic() + timeout
        while True:
            self.dispatch()
            if self.connected and all(any(d.resumed and cap in d.capabilities for d in self.devices.values()) for cap in capabilities):
                return
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise EISError(f'Timed out waiting for resumed EIS devices: {self.diagnostics()}')
            self.dispatch(min(.05, remaining))

    def _device(self, capability):
        self.dispatch()
        choices = [d for d in self.devices.values() if d.resumed and capability in d.capabilities]
        if capability in (BUTTON, SCROLL):
            # Mutter can expose both relative and absolute pointer devices
            # with buttons/scroll. Keep clicks on the last positioned device.
            preferred = next((d for d in choices if d.pointer == self._pointer_device), None)
            if preferred is not None:
                return preferred
            absolute = [d for d in choices if ABSOLUTE in d.capabilities]
            if len(absolute) == 1:
                return absolute[0]
        if len(choices) != 1:
            raise EISError(f'Expected one resumed EIS device for capability {capability}, got {len(choices)}')
        return choices[0]

    def _frame(self, device):
        self.api.device_frame(device.pointer, self.api.now(self.context))
        self.api.dispatch(self.context)

    def _key(self, device, code, pressed):
        if not 0 <= code <= 0x2ff:
            raise ValueError('Expected a Linux evdev keycode, without the XKB +8 offset')
        if (code in device.keys) == bool(pressed):
            return
        self.api.device_keyboard_key(device.pointer, code, bool(pressed))
        if pressed:
            device.keys.add(code)
        else:
            device.keys.discard(code)
        if device.keymap:
            device.keymap.update_key(code, pressed)

    def keycode(self, code, pressed):
        """Send a Linux evdev key, not an X11 keycode or keysym."""
        device = self._device(KEYBOARD)
        self._key(device, int(code), bool(pressed))
        self._frame(device)

    def keysym(self, symbol, pressed):
        """Type a keysym through the active device keymap, with needed modifiers."""
        symbol = int(symbol)
        device = self._device(KEYBOARD)
        if pressed:
            if symbol in self._symbols:
                return
            if device.keymap is None:
                raise EISError('The EIS keyboard did not provide an XKB keymap; use evdev keycodes')
            synthetic = {code for item in self._symbols.values() for code in item[2]}
            code, modifiers = device.keymap.resolve(symbol, synthetic)
            for modifier in modifiers:
                self._key(device, modifier, True)
            self._key(device, code, True)
            self._symbols[symbol] = (device.pointer, code, modifiers)
        else:
            item = self._symbols.pop(symbol, None)
            if item is None:
                return
            if item[0] != device.pointer:
                raise EISError('The EIS keyboard changed while a symbol was held')
            _, code, modifiers = item
            self._key(device, code, False)
            for modifier in reversed(modifiers):
                # Another held symbol may still need this synthesized modifier.
                if not any(modifier in held[2] for held in self._symbols.values()):
                    self._key(device, modifier, False)
        self._frame(device)

    def _absolute_candidates(self, x, y, mapping_id):
        choices = []
        for device in self.devices.values():
            if not device.resumed or not device.emulating or ABSOLUTE not in device.capabilities:
                continue
            region = self.api.device_get_region_at(device.pointer, x, y)
            if not region:
                continue
            actual_id = self.api.region_get_mapping_id(region)
            actual_id = actual_id.decode() if actual_id else None
            if mapping_id is None or mapping_id == actual_id:
                geometry = tuple(getter(region) for getter in (
                    self.api.region_get_x, self.api.region_get_y,
                    self.api.region_get_width, self.api.region_get_height))
                choices.append((device, (actual_id, geometry, device.capabilities,
                    self.api.region_get_physical_scale(region))))
        return choices

    def _absolute_device(self, x, y, mapping_id, timeout):
        end = time.monotonic() + min(2., max(0., float(timeout)))
        while True:
            self.dispatch()
            choices = self._absolute_candidates(x, y, mapping_id)
            pin = getattr(self, '_absolute_pin', None)
            if pin is not None:
                # A caller may pin an anonymous published device after checking
                # it against the actual sole output. Revalidate its lifetime
                # and complete region metadata; never follow a replacement.
                current = self._region_record(pin['device_id'], pin['region_index'])
                choices = [(device, target) for device, target in choices
                    if current == pin and device.pointer == pin['device_id']]
            if len(choices) == 1:
                return choices[0][0]
            # Mutter 46 creates the same absolute viewport once at client
            # initialization and again at SEAT_BIND. Both remain resumed. An
            # explicit resource mapping ID plus identical published geometry
            # and capabilities proves this is one target exposed twice. Do
            # not collapse anonymous overlaps or genuinely different targets.
            if mapping_id is not None and choices and len({target for _, target in choices}) == 1:
                return next((device for device, _ in choices if device.pointer == self._pointer_device), choices[-1][0])
            remaining = end - time.monotonic()
            if remaining <= 0:
                pointers = [device.pointer for device, _ in choices]
                raise EISError('Absolute target is outside/ambiguous across EIS regions: '
                    f'({x}, {y}), mapping={mapping_id!r}, candidate_count={len(choices)}, '
                    f'candidate_devices={pointers}, diagnostics={self.diagnostics()}')
            # Viewport replacement can remove/temporarily pause a device.
            # Wait for actual protocol events, bounded to two seconds, rather
            # than using stale constructor diagnostics or guessing an output.
            self.dispatch(min(.025, remaining))

    def absolute(self, x, y, *, mapping_id=None, timeout=2.):
        """Move inside one unambiguous published region; never guess an output.

        Brief device reconfiguration waits at most two seconds. Equivalent
        duplicate devices require an explicit matching resource mapping ID.
        """
        x, y = float(x), float(y)
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError('EIS absolute coordinates must be finite')
        if not math.isfinite(timeout):
            raise ValueError('EIS reconfiguration timeout must be finite')
        device = self._absolute_device(x, y, mapping_id, timeout)
        self._pointer_device = device.pointer
        self.api.device_pointer_motion_absolute(device.pointer, x, y)
        self._frame(device)

    def relative(self, dx, dy):
        if not math.isfinite(dx) or not math.isfinite(dy):
            raise ValueError('EIS motion must be finite')
        device = self._device(POINTER)
        self._pointer_device = device.pointer
        self.api.device_pointer_motion(device.pointer, dx, dy)
        self._frame(device)

    def button(self, code, pressed):
        code = int(code)
        if not 0x100 <= code <= 0x2ff:
            raise ValueError('Expected a Linux evdev button code, e.g. BTN_LEFT=272')
        device = self._device(BUTTON)
        if (code in device.buttons) == bool(pressed):
            return
        self.api.device_button_button(device.pointer, code, bool(pressed))
        if pressed:
            device.buttons.add(code)
        else:
            device.buttons.discard(code)
        self._frame(device)

    def scroll(self, dx, dy, *, discrete=False, finish=False):
        if not math.isfinite(dx) or not math.isfinite(dy):
            raise ValueError('EIS scroll values must be finite')
        device = self._device(SCROLL)
        if discrete:
            # libei scroll units are 120 per wheel detent.
            x, y = round(dx * 120), round(dy * 120)
            if not all(-(1 << 31) <= value < 1 << 31 for value in (x, y)):
                raise ValueError('EIS discrete scroll exceeds int32 units')
            self.api.device_scroll_discrete(device.pointer, x, y)
        else:
            self.api.device_scroll_delta(device.pointer, dx, dy)
        if finish:
            self.api.device_scroll_stop(device.pointer, True, True)
        self._frame(device)

    def notify(self, method, value, *, mapping_id=None):
        """Adapter for the runner's explicit Mutter Notify* calls only.

        Session Start/Stop must keep using the original D-Bus implementation.
        The stream path in NotifyPointerMotionAbsolute is not a mapping ID;
        the caller must pass one explicitly for stream-local EIS regions.
        """
        values = value.unpack() if hasattr(value, 'unpack') else tuple(value)
        if method == 'NotifyKeyboardKeycode':
            return self.keycode(*values)
        if method == 'NotifyKeyboardKeysym':
            return self.keysym(*values)
        if method == 'NotifyPointerMotionAbsolute':
            _, x, y = values
            return self.absolute(x, y, mapping_id=mapping_id)
        if method in ('NotifyPointerMotion', 'NotifyPointerMotionRelative'):
            return self.relative(*values)
        if method == 'NotifyPointerButton':
            return self.button(*values)
        if method in ('NotifyPointerAxis', 'NotifyPointerAxisDiscrete'):
            if method == 'NotifyPointerAxis':
                # Mutter's legacy smooth axis method carries a final flags u.
                dx, dy, flags = values
                if flags & ~1:
                    # EIS does not expose the legacy wheel/finger/source flags.
                    # Fail explicitly instead of claiming their semantics.
                    raise NotImplementedError('EIS smooth scroll source flags are not supported; use discrete wheel events')
                return self.scroll(dx, dy, finish=bool(flags & 1))
            axis, steps = values
            if axis not in (0, 1):
                raise ValueError('Mutter scroll axis must be vertical=0 or horizontal=1')
            return self.scroll(steps if axis else 0, 0 if axis else steps, discrete=True)
        raise NotImplementedError(f'EIS adapter does not implement {method}')

    def diagnostics(self):
        devices = []
        for device in self.devices.values():
            regions = []
            index = 0
            while region := self.api.device_get_region(device.pointer, index):
                mapping = self.api.region_get_mapping_id(region)
                regions.append({'x': self.api.region_get_x(region), 'y': self.api.region_get_y(region),
                    'width': self.api.region_get_width(region), 'height': self.api.region_get_height(region),
                    'mapping_id': mapping.decode() if mapping else None,
                    'physical_scale': self.api.region_get_physical_scale(region)})
                index += 1
            devices.append({'name': device.name, 'capabilities': sorted(device.capabilities),
                'resumed': device.resumed, 'emulating': device.emulating,
                'keymap': device.keymap is not None, 'regions': regions})
        return {'connected': self.connected, 'disconnected': self.disconnected,
                'events': self.events.copy(), 'devices': devices}

    def _region_record(self, device_id, region_index):
        device = self.devices.get(device_id)
        if (device is None or ABSOLUTE not in device.capabilities
                or not isinstance(region_index, int) or region_index < 0):
            return None
        region = self.api.device_get_region(device.pointer, region_index)
        if not region:
            return None
        mapping = self.api.region_get_mapping_id(region)
        return {'device_id': device.pointer, 'device_generation': device.generation,
            'region_index': region_index, 'name': device.name,
            'capabilities': sorted(device.capabilities), 'resumed': device.resumed,
            'emulating': device.emulating, 'x': self.api.region_get_x(region),
            'y': self.api.region_get_y(region), 'width': self.api.region_get_width(region),
            'height': self.api.region_get_height(region),
            'mapping_id': mapping.decode() if mapping else None,
            'physical_scale': self.api.region_get_physical_scale(region)}

    def absolute_regions(self):
        """Return copied current absolute-region metadata, without sending input.

        Entries include device_id/device_generation/region_index, published
        x/y/width/height/mapping_id/physical_scale, name/capabilities and readiness.
        Duplicates are retained so callers can validate the actual compositor
        layout rather than infer it from device count or monitor dimensions.
        """
        self.dispatch()
        regions = []
        for device in self.devices.values():
            index = 0
            while (record := self._region_record(device.pointer, index)) is not None:
                regions.append(record)
                index += 1
        return regions

    def pin_absolute_region(self, record):
        """Select one verified published region locally; generate no input.

        The caller first checks absolute_regions() against its owned target and
        actual output layout. This can disambiguate Mutter's anonymous duplicate
        devices without fabricating a mapping ID or changing the compositor.
        The exact device lifetime and all metadata are checked again before
        every subsequent motion. Removal/replacement fails closed. Pass None
        to clear the pin and return to ordinary mapping-ID selection.
        """
        self.dispatch()
        if record is None:
            self._absolute_pin = None
            return
        current = self._region_record(record.get('device_id'), record.get('region_index'))
        if current != record or not current['resumed'] or not current['emulating']:
            raise EISError('Cannot pin a stale, changed or unready EIS absolute region')
        self._absolute_pin = dict(current, capabilities=current['capabilities'].copy())

    def close(self):
        """Release only this sender; leave the caller-owned session/output alive."""
        if not self.context:
            return
        try:
            for device in self.devices.values():
                if device.resumed and device.emulating and not self.disconnected:
                    for code in sorted(device.keys):
                        self.api.device_keyboard_key(device.pointer, code, False)
                    for code in sorted(device.buttons):
                        self.api.device_button_button(device.pointer, code, False)
                    if device.keys or device.buttons:
                        self.api.device_frame(device.pointer, self.api.now(self.context))
                    self.api.device_stop_emulating(device.pointer)
            self.api.dispatch(self.context)
        finally:
            for pointer in list(self.devices):
                self._remove_device(pointer)
            self.api.unref(self.context)
            self.context = None
