"""Native GTK4 grouped scene hierarchy with precise object/view selection.

Camera view members use ``(owner, view_index)``; an object or camera rig uses
``(owner, None)``. Selection only inspects. ListView activation (double click or
Enter) is the sole route to ``on_activate``.
"""
from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from .i18n import tr


CATEGORIES = ('representations', 'cameras', 'motion', 'observations', 'appearance', 'rendering', 'data')
_TYPE_CATEGORIES = {
    **dict.fromkeys(('gaussian', 'geometry', 'pointcloud', 'point_cloud', 'mesh'), 'representations'),
    'camera': 'cameras',
    **dict.fromkeys(('scaffold', 'motion', 'transform', 'binding', 'tracks', 'track'), 'motion'),
    **dict.fromkeys(('observation', 'image', 'depth'), 'observations'),
    'appearance': 'appearance', 'render': 'rendering',
}
_ICONS = {'representations': 'package-x-generic-symbolic', 'cameras': 'camera-photo-symbolic',
          'motion': 'network-transmit-receive-symbolic', 'observations': 'image-x-generic-symbolic',
          'appearance': 'applications-graphics-symbolic', 'rendering': 'camera-video-symbolic',
          'data': 'drive-harddisk-symbolic'}


class OutlinerNode(GObject.Object):
    def __init__(self, label, *, owner=None, view_index=None, category=None, descriptor=None):
        super().__init__()
        self.label, self.owner, self.view_index = label, owner, view_index
        self.category, self.descriptor = category, descriptor
        self.children = Gio.ListStore.new(OutlinerNode)
        self.visible = False
        self.can_toggle = False

    @property
    def member(self):
        return None if self.owner is None else (self.owner, self.view_index)

    @property
    def key(self):
        return ('category', self.category) if self.owner is None else self.member


class SceneOutliner(Gtk.ScrolledWindow):
    """TreeListModel/TreeExpander/ListView scene browser.

    ``on_selection(owners, primary_owner, view_index, extend)`` reads the exact
    ordered tuples from ``selected_members``. ``on_activate(owner, view_index)``
    and ``on_visibility(owner, enabled)`` are independent callbacks. Public
    setters are silent unless ``select_members(..., emit=True)`` is requested.
    """
    def __init__(self):
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC)
        self.on_selection = lambda owners, primary, view, extend: None
        self.on_activate = lambda owner, view: None
        self.on_visibility = lambda owner, enabled: None
        self.selected_members = []
        self.primary_member = None
        self.query = ''
        self._nodes = {}
        self._groups = {}
        self._blocked = 0
        self._pending_primary = None
        self._extend = False
        self._before_search = None
        self._bound_checks = {}
        self.root_model = Gio.ListStore.new(OutlinerNode)
        self.model = Gtk.TreeListModel.new(self.root_model, False, False,
                                           lambda node: node.children if node.children.get_n_items() else None)
        self.selection_model = Gtk.MultiSelection.new(self.model)
        self.selection_model.connect('selection-changed', self._selection_changed)
        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', self._setup)
        factory.connect('bind', self._bind)
        factory.connect('unbind', self._unbind)
        self.view = Gtk.ListView.new(self.selection_model, factory)
        self.view.set_single_click_activate(False)
        self.view.set_enable_rubberband(True)
        self.view.connect('activate', self._activated)
        self.view.update_property([Gtk.AccessibleProperty.LABEL], [tr('outliner.title', 'Scene Outliner')])
        keyboard = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keyboard.connect('key-pressed', self._key_pressed)
        self.view.add_controller(keyboard)
        self.set_child(self.view)

    def set_objects(self, objects, visible=(), supported=(), selected=(), camera_views=None):
        expanded = self._expanded_keys()
        first_load = not self._groups
        self._nodes, self._groups = {}, {}
        visible, supported = set(visible), set(supported)
        camera_views = camera_views or {}
        for obj in objects:
            owner = obj['id']
            category = _TYPE_CATEGORIES.get(obj['type'], 'data')
            if category not in self._groups:
                self._groups[category] = OutlinerNode(tr('category.'+category), category=category)
            node = OutlinerNode(obj.get('label', owner), owner=owner, descriptor=obj)
            node.visible = owner in visible
            node.can_toggle = owner in supported or obj['type'] == 'camera'
            self._nodes[node.member] = node
            self._groups[category].children.append(node)
            if obj['type'] == 'camera':
                views = camera_views.get(owner, obj.get('data', {}).get('view_ids', []))
                if isinstance(views, int):
                    views = [tr('camera.viewNumber', 'View %d').replace('%d', str(i+1)) for i in range(views)]
                if len(views) <= 1:
                    continue
                for index, label in enumerate(views):
                    if isinstance(label, dict):
                        label = label.get('label', label.get('id', str(index)))
                    child = OutlinerNode(str(label), owner=owner, view_index=index, descriptor=obj)
                    self._nodes[child.member] = child
                    node.children.append(child)
        if first_load:
            expanded = {('category', key) for key in self._groups}
        self._rebuild(expanded)
        self.select_members(selected)

    def set_object_visibility(self, visible):
        """Refresh owner visibility without rebuilding hierarchy or selection."""
        visible = set(visible)
        for member, node in self._nodes.items():
            if member[1] is not None:
                continue
            node.visible = node.owner in visible
            check = self._bound_checks.get(member)
            if check is not None:
                check._binding = True
                check.set_active(node.visible)
                check._binding = False

    def set_query(self, query):
        query = str(query).strip().casefold()
        if query == self.query:
            return
        expanded = self._expanded_keys()
        if query and not self.query:
            self._before_search = expanded
        self.query = query
        self._rebuild(self._before_search if not query and self._before_search is not None else expanded)
        if not query:
            self._before_search = None

    def _matches(self, node):
        obj = node.descriptor or {}
        text = ' '.join(str(obj.get(key, '')) for key in ('label', 'id', 'type', 'encoding'))
        return self.query in (text+' '+node.label).casefold()

    def _rebuild(self, expanded):
        # Temporary filtered stores retain original node identities and children.
        self._blocked += 1
        try:
            self.root_model.remove_all()
            for category in CATEGORIES:
                original = self._groups.get(category)
                if original is None:
                    continue
                group = OutlinerNode(original.label, category=category)
                for i in range(original.children.get_n_items()):
                    node = original.children.get_item(i)
                    if not self.query:
                        group.children.append(node)
                    elif self._matches(node):
                        group.children.append(node)
                    else:
                        matches = [node.children.get_item(j) for j in range(node.children.get_n_items())
                                   if self._matches(node.children.get_item(j))]
                        if matches:
                            parent = OutlinerNode(node.label, owner=node.owner, descriptor=node.descriptor)
                            parent.visible, parent.can_toggle = node.visible, node.can_toggle
                            for match in matches:
                                parent.children.append(match)
                            group.children.append(parent)
                if group.children.get_n_items():
                    group.label = f'{tr("category."+category)} ({group.children.get_n_items()})'
                    self.root_model.append(group)
            index = 0
            while index < self.model.get_n_items():
                row = self.model.get_item(index)
                row.set_expanded(bool(self.query) or row.get_item().key in expanded)
                index += 1
            self._sync_selection()
        finally:
            self._blocked -= 1

    def _expanded_keys(self):
        return {row.get_item().key for row in (self.model.get_item(i) for i in range(self.model.get_n_items()))
                if row.get_expanded()}

    @staticmethod
    def _member(value):
        if isinstance(value, str):
            return value, None
        owner, view = value
        return owner, None if view is None else int(view)

    def select_members(self, members, primary=None, *, emit=False, extend=False):
        members = list(dict.fromkeys(self._member(member) for member in members))
        self.selected_members = [member for member in members if member in self._nodes]
        preferred = self._member(primary) if primary is not None else None
        self.primary_member = preferred if preferred in self.selected_members else next(iter(self.selected_members[-1:]), None)
        self._blocked += 1
        try:
            # External focus on a camera view opens its rig and category.
            owners = {member[0] for member in self.selected_members}
            index = 0
            while index < self.model.get_n_items():
                row = self.model.get_item(index)
                node = row.get_item()
                if node.owner is None and any(self._belongs_to(owner, node.category) for owner in owners):
                    row.set_expanded(True)
                elif (node.owner is not None and node.view_index is None
                      and any(owner == node.owner and view is not None for owner,view in self.selected_members)):
                    row.set_expanded(True)
                index += 1
            self._sync_selection()
        finally:
            self._blocked -= 1
        if emit:
            self._notify_selection(extend)

    def _belongs_to(self, owner, category):
        node = self._nodes.get((owner,None))
        return node is not None and _TYPE_CATEGORIES.get(node.descriptor['type'],'data') == category

    def position_for_member(self, member):
        member = self._member(member)
        return next((i for i in range(self.model.get_n_items())
                     if self.model.get_item(i).get_item().member == member), None)

    def _sync_selection(self):
        selected = Gtk.Bitset.new_empty()
        wanted = set(self.selected_members)
        for i in range(self.model.get_n_items()):
            if self.model.get_item(i).get_item().member in wanted:
                selected.add(i)
        self.selection_model.set_selection(selected, Gtk.Bitset.new_range(0,self.model.get_n_items()))

    def _selection_changed(self, model, position, count):
        if self._blocked:
            return
        previous = self.selected_members
        visible, selected = set(), []
        illegal = []
        for i in range(self.model.get_n_items()):
            member = self.model.get_item(i).get_item().member
            if member is not None:
                visible.add(member)
            if model.is_selected(i):
                if member is None:
                    illegal.append(i)
                else:
                    selected.append(member)
        if illegal:
            self._blocked += 1
            try:
                for i in illegal:
                    model.unselect_item(i)
            finally:
                self._blocked -= 1
        # Filtering/collapse must not silently change semantic membership.
        hidden = [member for member in previous if member not in visible]
        if self._pending_primary is not None and not self._extend:
            hidden = []
        added = [member for member in selected if member not in previous]
        self.selected_members = [member for member in previous if member in selected or member in hidden]+added
        preferred = self._pending_primary
        self.primary_member = preferred if preferred in self.selected_members else (
            self.primary_member if self.primary_member in self.selected_members else
            next(iter(self.selected_members[-1:]),None))
        self._pending_primary = None
        if self.selected_members != previous or preferred is not None:
            self._notify_selection(self._extend)

    def _notify_selection(self, extend):
        primary, view = self.primary_member or (None,None)
        self.on_selection(list(dict.fromkeys(owner for owner,_ in self.selected_members)),primary,view,bool(extend))

    def _activated(self, view, position):
        if not 0 <= position < self.model.get_n_items():
            return
        row = self.model.get_item(position)
        node = row.get_item()
        if node.member is None:
            row.set_expanded(not row.get_expanded())
        else:
            self.on_activate(node.owner,node.view_index)

    def _key_pressed(self, controller, keyval, keycode, state):
        self._extend = bool(state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
        position = self.position_for_member(self.primary_member) if self.primary_member else None
        if position is None:
            return False
        row = self.model.get_item(position)
        if keyval == Gdk.KEY_Right and row.is_expandable() and not row.get_expanded():
            row.set_expanded(True)
            return True
        if keyval == Gdk.KEY_Left and row.get_expanded():
            row.set_expanded(False)
            return True
        return False

    def _row_pressed(self, gesture, presses, x, y, item):
        row = item.get_item()
        if row is None or row.get_item().member is None:
            return
        # A checkbox or disclosure click changes its control, not the primary
        # inspection target. The gesture is attached to the content box only.
        picked = gesture.get_widget().pick(x,y,Gtk.PickFlags.DEFAULT)
        while picked is not None and picked is not gesture.get_widget():
            if picked is item._parts[0]:
                return
            picked = picked.get_parent()
        self._pending_primary = row.get_item().member
        self._extend = bool(gesture.get_current_event_state() & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
        GLib.idle_add(self._finish_pointer_primary)

    def _finish_pointer_primary(self):
        if self._pending_primary is not None:
            self._selection_changed(self.selection_model,0,0)
        return False

    def _setup(self, factory, item):
        expander = Gtk.TreeExpander()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=7,margin_top=5,margin_bottom=5,margin_end=6)
        check = Gtk.CheckButton(tooltip_text=tr('scene.visibility','Object visibility'))
        check._binding = False
        check.connect('toggled',self._toggle,item)
        icon = Gtk.Image()
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=1,hexpand=True)
        title = Gtk.Label(xalign=0,ellipsize=Pango.EllipsizeMode.END)
        detail = Gtk.Label(xalign=0,ellipsize=Pango.EllipsizeMode.END)
        detail.add_css_class('dim-label')
        names.append(title);names.append(detail)
        box.append(check);box.append(icon);box.append(names)
        expander.set_child(box)
        item.set_child(expander)
        item._parts = check,icon,title,detail
        gesture = Gtk.GestureClick(button=1,propagation_phase=Gtk.PropagationPhase.CAPTURE)
        gesture.connect('pressed',self._row_pressed,item)
        box.add_controller(gesture)

    def _bind(self, factory, item):
        row = item.get_item()
        node = row.get_item()
        expander = item.get_child()
        expander.set_list_row(row)
        group = node.owner is None
        item.set_selectable(not group)
        item.set_activatable(not group)
        check,icon,title,detail = item._parts
        check._binding = True
        check.set_visible(not group and node.view_index is None)
        check.set_sensitive(node.can_toggle)
        check.set_active(node.visible)
        check._binding = False
        if not group and node.view_index is None:
            self._bound_checks[node.member] = check
        title.set_text(node.label)
        (title.add_css_class if group else title.remove_css_class)('heading')
        detail.set_visible(not group and node.view_index is None)
        obj = node.descriptor or {}
        detail.set_text(obj.get('type','')+(' · user' if obj.get('authored') else ''))
        category = node.category or _TYPE_CATEGORIES.get(obj.get('type'),'data')
        icon.set_from_icon_name('camera-photo-symbolic' if node.view_index is not None else _ICONS[category])
        expander.set_tooltip_text(node.label if group else '\n'.join(str(obj.get(k,'')) for k in ('label','id','type','encoding')))

    def _unbind(self, factory, item):
        check = item._parts[0]
        for member,bound in tuple(self._bound_checks.items()):
            if bound is check:
                del self._bound_checks[member]
        item.get_child().set_list_row(None)

    def _toggle(self, check, item):
        if check._binding or item.get_item() is None:
            return
        node = item.get_item().get_item()
        if node.owner is None or node.view_index is not None or not node.can_toggle:
            return
        node.visible = check.get_active()
        original = self._nodes.get(node.member)
        if original is not None:
            original.visible = node.visible
        self.on_visibility(node.owner,node.visible)
