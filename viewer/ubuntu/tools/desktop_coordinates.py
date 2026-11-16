"""Conservative coordinates and explicit owned-window activation for the runner.

Coordinate lookup is read-only; ambiguous targets return None. The separate
activate_owned_x11 helper explicitly asks the window manager to activate one
PID/title-matched window. It does not synthesize keyboard or pointer events.
"""
from __future__ import annotations

import ctypes as C
import ctypes.util
import math
import time

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi, GLib


class _X11:
    def __init__(self):
        self.lib = C.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6')
        def bind(name, result, *arguments):
            function = getattr(self.lib, name)
            function.restype, function.argtypes = result, list(arguments)
            return function
        ptr, ulong, integer = C.c_void_p, C.c_ulong, C.c_int
        self.open = bind('XOpenDisplay', ptr, C.c_char_p)
        self.close = bind('XCloseDisplay', integer, ptr)
        self.root_window = bind('XDefaultRootWindow', ulong, ptr)
        self.intern = bind('XInternAtom', ulong, ptr, C.c_char_p, integer)
        self.free = bind('XFree', integer, ptr)
        self.get_property = bind('XGetWindowProperty', integer, ptr, ulong, ulong,
            C.c_long, C.c_long, integer, ulong, C.POINTER(ulong), C.POINTER(integer),
            C.POINTER(ulong), C.POINTER(ulong), C.POINTER(ptr))
        self.get_geometry = bind('XGetGeometry', integer, ptr, ulong, C.POINTER(ulong),
            C.POINTER(integer), C.POINTER(integer), C.POINTER(C.c_uint),
            C.POINTER(C.c_uint), C.POINTER(C.c_uint), C.POINTER(C.c_uint))
        self.translate = bind('XTranslateCoordinates', integer, ptr, ulong, ulong,
            integer, integer, C.POINTER(integer), C.POINTER(integer), C.POINTER(ulong))
        self.sync = bind('XSync', integer, ptr, integer)
        self.error_handler = bind('XSetErrorHandler', ptr, ptr)
        self.display = self.open(None)
        if not self.display:
            raise OSError('No authenticated local X11 display')
        self.root = self.root_window(self.display)
        self.failed = False
        # A client can disappear between the EWMH list and its geometry query.
        # Do not let Xlib's default BadWindow handler terminate the runner.
        callback = C.CFUNCTYPE(integer, ptr, ptr)
        def failed(display, event):
            if display == self.display:
                self.failed = True
            elif self.previous_handler:
                # XSetErrorHandler is process-global. Preserve the previous
                # policy for any unrelated Xlib display owned by this process.
                return callback(self.previous_handler)(display, event)
            return 0
        self.handler = callback(failed)
        self.previous_handler = self.error_handler(C.cast(self.handler, ptr))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            self.sync(self.display, 0)
        finally:
            self.error_handler(self.previous_handler)
            self.close(self.display)
            self.display = None

    def property(self, window, name, *, format, type_name):
        atom = self.intern(self.display, name.encode(), 1)
        expected = self.intern(self.display, type_name.encode(), 1)
        if not atom or not expected:
            return None
        actual_type, actual_format = C.c_ulong(), C.c_int()
        count, remaining, data = C.c_ulong(), C.c_ulong(), C.c_void_p()
        try:
            status = self.get_property(self.display, window, atom, 0, 16384, 0, expected,
                C.byref(actual_type), C.byref(actual_format), C.byref(count), C.byref(remaining), C.byref(data))
            if (status or not data or actual_type.value != expected or actual_format.value != format
                    or remaining.value or count.value > 65536):
                return None
            if format == 8:
                return C.string_at(data, count.value)
            # Xlib expands format-32 properties into native unsigned longs,
            # including on LP64; reading a packed uint32 array would be wrong.
            return list(C.cast(data, C.POINTER(C.c_ulong))[:count.value])
        finally:
            if data:
                self.free(data)

    def geometry(self, window):
        root, child = C.c_ulong(), C.c_ulong()
        local_x, local_y, x, y = C.c_int(), C.c_int(), C.c_int(), C.c_int()
        width, height, border, depth = [C.c_uint() for _ in range(4)]
        if not self.get_geometry(self.display, window, C.byref(root), C.byref(local_x), C.byref(local_y),
                                 C.byref(width), C.byref(height), C.byref(border), C.byref(depth)):
            return None
        if root.value != self.root or not self.translate(self.display, window, self.root, 0, 0,
                                                        C.byref(x), C.byref(y), C.byref(child)):
            return None
        return x.value, y.value, width.value, height.value


def _single_output(desktop):
    value = desktop.call('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
                         'org.gnome.Mutter.DisplayConfig', 'GetCurrentState').unpack()
    physical, logical = value[1], value[2]
    if len(physical) != 1 or len(logical) != 1:
        return False
    output = logical[0]
    # Require unscaled/unrotated coordinates and a sole stream origin. The
    # runner-owned virtual output is the complete desktop in this safe mode.
    return (output[0] == 0 and output[1] == 0 and float(output[2]) == 1.
            and output[3] == 0 and len(output[5]) == 1)


def _output_layout(desktop):
    state = desktop.call('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
                         'org.gnome.Mutter.DisplayConfig', 'GetCurrentState').unpack()
    physical, logical, properties = state[1:4]
    if len(physical) != 1 or len(logical) != 1 or len(logical[0][5]) != 1:
        raise ValueError('Coordinate mapping requires one physical and one logical output')
    output = logical[0]
    scale = float(output[2])
    if output[0:2] != (0, 0) and list(output[0:2]) != [0, 0]:
        raise ValueError('The sole output does not start at the verified desktop origin')
    if not math.isfinite(scale) or scale < 1 or output[3] != 0:
        raise ValueError('Unsupported output scale or rotation')
    current = [mode for mode in physical[0][1] if mode[-1].get('is-current')]
    if len(current) != 1:
        raise ValueError('No unique current physical output mode')
    mode = current[0]
    return {'monitor_spec': list(physical[0][0]), 'physical_size': [int(mode[1]), int(mode[2])],
            'scale': scale, 'layout_mode': properties.get('layout-mode'),
            'legacy_ui_scale': float(properties.get('legacy-ui-scaling-factor', 1))}


def _known_sizes(layout):
    width, height = layout['physical_size']
    sizes = {(width, height)}
    logical = (width/layout['scale'], height/layout['scale'])
    if all(math.isclose(value, round(value), abs_tol=1e-7) for value in logical):
        sizes.add(tuple(round(value) for value in logical))
    return sizes


def _region_key(region):
    return tuple(region[key] for key in ('x','y','width','height','mapping_id','physical_scale'))


def _regions(eis):
    regions = [region for region in eis.absolute_regions()
               if region.get('resumed') and region.get('emulating')]
    if not regions or len({_region_key(region) for region in regions}) != 1:
        raise ValueError('EIS does not expose one unique equivalent absolute-output region')
    return regions


def configure_existing_output(desktop):
    """Verify the existing sole output and pin its published EIS region.

    This is read-only with respect to the desktop: no output, focus or input is
    changed. Duplicate devices advertising the exact same region are allowed;
    the EIS helper pins the chosen concrete device lifetime. All coordinates
    returned by owned_target_point are then already in that EIS region's space.
    """
    if desktop.eis is None:
        raise ValueError('Existing-output coordinates require the native EIS input backend')
    layout = _output_layout(desktop)
    regions = _regions(desktop.eis)
    region = regions[-1]
    region_geometry = tuple(int(region[key]) for key in ('x','y','width','height'))
    if (region_geometry[:2] != (0, 0) or region_geometry[2:] not in _known_sizes(layout)
            or not any(math.isclose(float(region['physical_scale']), value, abs_tol=1e-7)
                       for value in (1., layout['scale'], layout['legacy_ui_scale']))):
        raise ValueError('The published EIS region does not match the sole physical/logical output extent')
    with _X11() as connection:
        root = connection.geometry(connection.root)
        if root is None or root[:2] != (0, 0) or root[2:] not in _known_sizes(layout):
            raise ValueError('Xroot dimensions do not match the current output mode/scale')
        connection.sync(connection.display, 0)
        if connection.failed:
            raise ValueError('Xroot geometry could not be read reliably')
    if _output_layout(desktop) != layout:
        raise ValueError('Output layout changed during coordinate verification')
    desktop.eis.pin_absolute_region(region)
    metadata = {'profile': 'existing-eis', 'output': layout, 'xroot_geometry': list(root),
                'ei_region': {key: region[key] for key in
                    ('device_id','device_generation','region_index','x','y','width','height','mapping_id','physical_scale')},
                'equivalent_absolute_devices': len(regions),
                'xroot_to_eis_scale': [region_geometry[2]/root[2], region_geometry[3]/root[3]],
                'coordinate_space': 'Published EIS region; physical_scale is metadata, not an extra multiplier',
                'temporary_output_created': False}
    desktop.coordinate_mapping = metadata
    # Retain compatibility with the runner's legacy field name, without
    # creating or claiming ownership of this existing monitor.
    desktop.virtual_size = root[2:]
    desktop.coordinate_scale = tuple(metadata['xroot_to_eis_scale'])
    desktop.pointer_stream = 'eis-existing-output'
    desktop.pointer_mapping_id = region['mapping_id']
    return metadata


def _mapping_valid(desktop, layout, root):
    mapping = getattr(desktop, 'coordinate_mapping', None)
    if mapping is None:
        return layout['scale'] == 1 and root[2:] == tuple(desktop.virtual_size)
    if (mapping.get('profile') != 'existing-eis' or mapping['output'] != layout
            or mapping['xroot_geometry'] != list(root) or desktop.eis is None):
        return False
    regions = _regions(desktop.eis)
    expected = mapping['ei_region']
    return any(all(region.get(key) == value for key, value in expected.items()) for region in regions)


def _gtk_to_x_scale(bounds, geometry, layout):
    scale = geometry[2]/bounds.width
    if (not math.isclose(scale, geometry[3]/bounds.height, abs_tol=1e-7)
            or not any(math.isclose(scale, candidate, abs_tol=1e-7)
                       for candidate in (1., layout['scale'], layout['legacy_ui_scale']))):
        return None
    return scale


def _input_point(desktop, point, root):
    mapping = getattr(desktop, 'coordinate_mapping', None)
    if mapping is None:
        return point
    region = mapping['ei_region']
    return (region['x']+(point[0]-root[0])*region['width']/root[2],
            region['y']+(point[1]-root[1])*region['height']/root[3])


def _frame(node, pid):
    chain = []
    for _ in range(128):
        if node is None or node.get_process_id() != pid:
            return None
        node.clear_cache()
        role = node.get_role_name()
        chain.append(node)
        if role in ('frame', 'dialog', 'window', 'alert', 'popup menu', 'menu'):
            return node, chain
        node = node.get_parent()
    return None


def owned_target_point(desktop, node, *, x_fraction=.5, y_fraction=.5):
    """Return (desktop_x, desktop_y), or None when ownership/mapping is unsure.

    X11 targets require an exact accessible-frame title and PID match against
    EWMH clients and translated geometry. GTK-to-X scale must agree on both
    axes and match a declared output/UI scale. Returned coordinates already
    use the configured EIS region's coordinate space, with no extra scaling.
    Native Wayland targets require a maximized-size accessible frame matching
    the actual current EWMH work area. No dock position is inferred from sizes.
    """
    try:
        fractions = (float(x_fraction), float(y_fraction))
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in fractions):
            return None
        virtual = desktop.virtual_size
        if (virtual is None or len(virtual) != 2 or min(virtual) <= 0
                or desktop.owner_pid() != desktop.pid):
            return None
        layout = _output_layout(desktop)
        target = _frame(node, desktop.pid)
        if target is None:
            return None
        frame, chain = target
        title = frame.get_name()
        if not title or not node.get_state_set().contains(Atspi.StateType.SHOWING):
            return None
        if not node.get_state_set().contains(Atspi.StateType.SENSITIVE):
            return None
        # A GtkPopover may expose no distinct accessible frame even though its
        # WINDOW origin belongs to a different native popup surface. A list row
        # under an expanded combo is therefore left to real keyboard navigation.
        if (any(item.get_role_name() == 'list item' for item in chain)
                and any(item.get_role_name() == 'combo box' and
                        item.get_state_set().contains(Atspi.StateType.EXPANDED) for item in chain)):
            return None
        rect = Atspi.Component.get_extents(node, Atspi.CoordType.WINDOW)
        bounds = Atspi.Component.get_extents(frame, Atspi.CoordType.WINDOW)
        if (bounds.x != 0 or bounds.y != 0 or min(bounds.width, bounds.height, rect.width, rect.height) <= 0
                or rect.x < 0 or rect.y < 0 or rect.x+rect.width > bounds.width
                or rect.y+rect.height > bounds.height):
            return None
        with _X11() as connection:
            root = connection.geometry(connection.root)
            if root is None or not _mapping_valid(desktop, layout, root):
                return None
            if desktop.backend == 'x11':
                clients = connection.property(connection.root, '_NET_CLIENT_LIST_STACKING', format=32, type_name='WINDOW')
                if clients is None:
                    clients = connection.property(connection.root, '_NET_CLIENT_LIST', format=32, type_name='WINDOW')
                matches = []
                for client in clients or []:
                    pid = connection.property(client, '_NET_WM_PID', format=32, type_name='CARDINAL')
                    if pid != [desktop.pid]:
                        continue
                    name = connection.property(client, '_NET_WM_NAME', format=8, type_name='UTF8_STRING')
                    if name is not None and name.decode('utf-8', errors='strict') == title:
                        geometry = connection.geometry(client)
                        if geometry is not None:
                            scale = _gtk_to_x_scale(bounds, geometry, layout)
                            if scale is not None:
                                matches.append((client, geometry, scale))
                if len(matches) != 1:
                    return None
                selected_client, selected_geometry, gtk_scale = matches[0]
                origin = selected_geometry[:2]
            elif desktop.backend == 'wayland':
                if not getattr(desktop, 'maximized', False):
                    return None
                current = connection.property(connection.root, '_NET_CURRENT_DESKTOP', format=32, type_name='CARDINAL')
                areas = connection.property(connection.root, '_NET_WORKAREA', format=32, type_name='CARDINAL')
                if not areas or len(areas) % 4:
                    return None
                if current and len(current) == 1:
                    offset = 4*current[0]
                elif all(areas[i:i+4] == areas[:4] for i in range(0,len(areas),4)):
                    # Wayland Mutter can omit CURRENT_DESKTOP. Identical actual
                    # work areas on every desktop determine the same origin.
                    offset = 0
                else:
                    return None
                area = areas[offset:offset+4]
                if len(area) != 4:
                    return None
                gtk_scale = _gtk_to_x_scale(bounds, area, layout)
                if gtk_scale is None:
                    return None
                origin = area[:2]
            else:
                return None
            # A fraction of one lands on the last allocated X pixel instead
            # of rounding onto an adjacent widget's exact left boundary.
            local = [min(position+extent*fraction, position+extent-1/gtk_scale)
                     for position, extent, fraction in zip((rect.x,rect.y),
                                                           (rect.width,rect.height), fractions)]
            point = (origin[0]+local[0]*gtk_scale, origin[1]+local[1]*gtk_scale)
            connection.sync(connection.display, 0)
            if connection.failed or not (0 <= point[0] < virtual[0] and 0 <= point[1] < virtual[1]):
                return None
            # A moving/resizing target is not a stable pointer destination.
            latest = Atspi.Component.get_extents(node, Atspi.CoordType.WINDOW)
            if tuple(getattr(latest, key) for key in ('x','y','width','height')) != tuple(
                    getattr(rect, key) for key in ('x','y','width','height')):
                return None
            if desktop.backend == 'x11' and connection.geometry(selected_client) != selected_geometry:
                return None
            return _input_point(desktop, point, root)
    except (OSError, ValueError, TypeError, AttributeError, IndexError, RuntimeError, GLib.Error):
        return None


def activate_owned_x11(desktop, node=None):
    """Request WM/seat activation of an exact owned X11 frame; return success.

    Sends EWMH _NET_ACTIVE_WINDOW with source=2 (pager) and timestamp=0,
    then waits up to two seconds for the WM property or accessible ACTIVE state.
    A successful XSendEvent alone is not considered successful activation. No
    XSetInputFocus, GrabFocus or synthetic keyboard/pointer event is used.
    """
    class ClientData(C.Union):
        _fields_ = [('b', C.c_char*20), ('s', C.c_short*10), ('l', C.c_long*5)]
    class ClientMessage(C.Structure):
        _fields_ = [('type', C.c_int), ('serial', C.c_ulong), ('send_event', C.c_int),
                    ('display', C.c_void_p), ('window', C.c_ulong),
                    ('message_type', C.c_ulong), ('format', C.c_int), ('data', ClientData)]
    class XEvent(C.Union):
        _fields_ = [('client', ClientMessage), ('padding', C.c_long*24)]
    try:
        if desktop.backend != 'x11' or desktop.owner_pid() != desktop.pid:
            return False
        target = _frame(node if node is not None else desktop.window(), desktop.pid)
        if target is None:
            return False
        frame = target[0]
        title = frame.get_name()
        if not title or not frame.get_state_set().contains(Atspi.StateType.SHOWING):
            return False
        with _X11() as connection:
            clients = connection.property(connection.root, '_NET_CLIENT_LIST_STACKING', format=32, type_name='WINDOW')
            if clients is None:
                clients = connection.property(connection.root, '_NET_CLIENT_LIST', format=32, type_name='WINDOW')
            matches = []
            for client in clients or []:
                if connection.property(client, '_NET_WM_PID', format=32, type_name='CARDINAL') != [desktop.pid]:
                    continue
                name = connection.property(client, '_NET_WM_NAME', format=8, type_name='UTF8_STRING')
                if name is not None and name.decode('utf-8', errors='strict') == title:
                    matches.append(client)
            if len(matches) != 1:
                return False
            client = matches[0]
            atom = connection.intern(connection.display, b'_NET_ACTIVE_WINDOW', 1)
            if not atom:
                return False
            active = connection.property(connection.root, '_NET_ACTIVE_WINDOW', format=32, type_name='WINDOW')
            event = XEvent()
            event.client.type = 33  # ClientMessage
            event.client.send_event = 1
            event.client.display = connection.display
            event.client.window = client
            event.client.message_type = atom
            event.client.format = 32
            event.client.data.l[:] = (2, 0, active[0] if active else 0, 0, 0)
            send = connection.lib.XSendEvent
            send.restype, send.argtypes = C.c_int, [C.c_void_p, C.c_ulong, C.c_int, C.c_long, C.POINTER(XEvent)]
            # SubstructureRedirectMask | SubstructureNotifyMask, as required by
            # EWMH. The WM, rather than the client, decides actual seat focus.
            if not send(connection.display, connection.root, 0, (1 << 20) | (1 << 19), C.byref(event)):
                return False
            connection.sync(connection.display, 0)
            if connection.failed:
                return False
            deadline = time.monotonic()+2
            context = GLib.MainContext.default()
            while time.monotonic() < deadline:
                for _ in range(20):
                    if not context.pending():
                        break
                    context.iteration(False)
                if desktop.owner_pid() != desktop.pid:
                    return False
                frame.clear_cache()
                active = connection.property(connection.root, '_NET_ACTIVE_WINDOW', format=32, type_name='WINDOW')
                if active == [client] or frame.get_state_set().contains(Atspi.StateType.ACTIVE):
                    return True
                time.sleep(.02)
            return False
    except (OSError, ValueError, TypeError, AttributeError, IndexError, RuntimeError, GLib.Error):
        return False
