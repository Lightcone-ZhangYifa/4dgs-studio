"""Real GTK4 hierarchy/model semantics, independent of desktop input injection."""
import os
from types import SimpleNamespace
import unittest


@unittest.skipUnless(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'), 'GTK needs a display; use xvfb-run')
class OutlinerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version('Gtk','4.0')
        gi.require_version('Gdk','4.0')
        from gi.repository import Gdk,GLib,Gtk
        from gs_studio.outliner import SceneOutliner
        Gtk.init()
        cls.Gdk,cls.GLib,cls.Gtk,cls.SceneOutliner=Gdk,GLib,Gtk,SceneOutliner

    def setUp(self):
        self.outline=self.SceneOutliner()
        self.objects=[
            {'id':'field','label':'Moving Gaussians','type':'gaussian','encoding':'gsv.gaussian.persistent-identity'},
            {'id':'rig','label':'Capture rig','type':'camera','encoding':'gsv.camera.pinhole','data':{'view_ids':['left-eye','right-eye']}},
            {'id':'graph','label':'Skeleton','type':'scaffold','encoding':'gsv.scaffold.graph'},
            {'id':'truth','label':'Held-out RGB','type':'observation','encoding':'gsv.observation.camera-rgb'},
            {'id':'colors','label':'Color correction','type':'appearance','encoding':'vendor.color'},
            {'id':'render','label':'Render target','type':'render','encoding':'gsv.render.view'},
            {'id':'opaque','label':'Opaque data','type':'archive','encoding':'vendor.blob'},
        ]
        self.selection=[];self.activated=[];self.visibility=[]
        self.outline.on_selection=lambda *args:self.selection.append(args)
        self.outline.on_activate=lambda *args:self.activated.append(args)
        self.outline.on_visibility=lambda *args:self.visibility.append(args)
        self.outline.set_objects(self.objects,{'field','rig'},{'field','graph'},[])
        self.window=None

    def tearDown(self):
        if self.window:
            self.window.destroy()
        context=self.GLib.MainContext.default()
        for _ in range(30):
            if not context.pending():break
            context.iteration(False)

    def rows(self):
        return [self.outline.model.get_item(i) for i in range(self.outline.model.get_n_items())]

    def test_native_models_canonical_categories_and_nonselectable_groups(self):
        from gs_studio.outliner import CATEGORIES
        self.assertIsInstance(self.outline.model,self.Gtk.TreeListModel)
        self.assertIsInstance(self.outline.selection_model,self.Gtk.MultiSelection)
        self.assertIsInstance(self.outline.view,self.Gtk.ListView)
        self.assertIsInstance(self.outline,self.Gtk.ScrolledWindow)
        self.assertIs(self.outline.get_child(),self.outline.view)
        self.assertEqual([r.get_item().category for r in self.rows() if r.get_item().owner is None],list(CATEGORIES))
        self.assertTrue(all(r.get_item().label.endswith('(1)') for r in self.rows() if r.get_item().owner is None))
        self.outline.selection_model.select_item(0,False)
        self.assertFalse(self.outline.selection_model.is_selected(0))
        self.assertEqual(self.outline.selected_members,[])
        self.assertEqual(self.selection,[])

    def test_camera_child_selection_only_inspects_exact_view_then_activation_is_explicit(self):
        self.outline.select_members([('rig',1)])
        position=self.outline.position_for_member(('rig',1))
        self.assertIsNotNone(position)
        self.assertEqual(self.outline.primary_member,('rig',1))
        self.assertEqual(self.activated,[])
        self.assertEqual(self.selection,[])
        self.outline.select_members([('rig',1)],emit=True)
        self.assertEqual(self.selection[-1],(['rig'],'rig',1,False))
        self.outline.view.emit('activate',position)
        self.assertEqual(self.activated,[('rig',1)])
        self.assertFalse(self.outline.view.get_single_click_activate())

    def test_ctrl_membership_keeps_rig_and_two_views_distinct_and_ordered(self):
        self.outline.select_members([('field',None),('rig',0)],primary=('rig',0))
        position=self.outline.position_for_member(('rig',1))
        self.outline._pending_primary=('rig',1)
        self.outline._extend=True
        self.outline.selection_model.select_item(position,False)
        self.assertEqual(self.outline.selected_members,[('field',None),('rig',0),('rig',1)])
        self.assertEqual(self.selection[-1],(['field','rig'],'rig',1,True))
        self.outline.selection_model.unselect_item(position)
        self.assertEqual(self.outline.selected_members,[('field',None),('rig',0)])
        self.assertEqual(self.outline.primary_member,('rig',0))

    def test_search_matches_encoding_and_hidden_selection_survives_filter(self):
        self.outline.select_members([('field',None),('rig',1)],primary=('rig',1))
        self.outline.set_query('camera-rgb')
        self.assertEqual([r.get_item().owner for r in self.rows()],[None,'truth'])
        self.assertEqual(self.outline.selected_members,[('field',None),('rig',1)])
        self.assertEqual(self.selection,[])
        self.outline.set_query('RIGHT-EYE')
        self.assertEqual([r.get_item().member for r in self.rows()],[None,('rig',None),('rig',1)])
        self.assertTrue(all(r.get_expanded() for r in self.rows()[:-1]))
        self.outline.set_query('')
        self.assertIsNotNone(self.outline.position_for_member(('rig',1)))
        self.assertEqual(self.outline.primary_member,('rig',1))

    def test_search_restores_collapsed_groups_and_select_expands_owner(self):
        group=self.rows()[0]
        group.set_expanded(False)
        self.assertIsNone(self.outline.position_for_member(('field',None)))
        self.outline.set_query('Moving')
        self.assertIsNotNone(self.outline.position_for_member(('field',None)))
        self.outline.set_query('')
        self.assertIsNone(self.outline.position_for_member(('field',None)))
        self.outline.select_members([('field',None)])
        self.assertIsNotNone(self.outline.position_for_member(('field',None)))

    def test_keyboard_left_right_expands_camera_rig_without_activation(self):
        self.outline.select_members([('rig',None)])
        self.assertIsNone(self.outline.position_for_member(('rig',0)))
        self.assertTrue(self.outline._key_pressed(None,self.Gdk.KEY_Right,0,self.Gdk.ModifierType(0)))
        self.assertIsNotNone(self.outline.position_for_member(('rig',0)))
        self.assertTrue(self.outline._key_pressed(None,self.Gdk.KEY_Left,0,self.Gdk.ModifierType(0)))
        self.assertIsNone(self.outline.position_for_member(('rig',0)))
        self.assertEqual(self.activated,[])

    def test_single_view_camera_has_no_redundant_expansion_layer(self):
        self.outline.set_objects([{'id':'single','type':'camera','encoding':'gsv.camera.pinhole',
                                  'data':{'view_ids':['main']}}])
        self.outline.select_members([('single',None)])
        position=self.outline.position_for_member(('single',None))
        self.assertFalse(self.outline.model.get_item(position).is_expandable())
        self.assertIsNone(self.outline.position_for_member(('single',0)))

    def test_large_tree_uses_direct_scrollable_viewport_and_virtualized_rows(self):
        import time
        objects=[{'id':f'field-{i}','type':'gaussian','encoding':'gsv.gaussian.static'} for i in range(2000)]
        self.outline.set_objects(objects,supported={o['id'] for o in objects})
        self.window=self.Gtk.Window(child=self.outline,default_width=420,default_height=300)
        self.window.present()
        context=self.GLib.MainContext.default();deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            for _ in range(20):
                if not context.pending():break
                context.iteration(False)
            if self.outline.view.get_mapped() and self.outline.view.get_height()>0:break
            time.sleep(.005)
        self.assertLessEqual(self.outline.view.get_height(),self.outline.get_height())
        self.assertLess(len(self.outline._bound_checks),500)
        self.assertGreater(len(self.outline._bound_checks),0)

    def test_mapped_tree_expanders_and_owner_visibility_are_independent_of_selection(self):
        import time
        self.window=self.Gtk.Window(child=self.outline,default_width=420,default_height=600)
        self.window.present()
        context=self.GLib.MainContext.default();deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            for _ in range(20):
                if not context.pending():break
                context.iteration(False)
            if ('field',None) in self.outline._bound_checks:break
            time.sleep(.005)
        check=self.outline._bound_checks[('field',None)]
        self.assertTrue(check.get_active())
        check.set_active(False)
        self.assertEqual(self.visibility,[('field',False)])
        self.assertEqual(self.outline.selected_members,[])
        self.outline.set_object_visibility({'field'})
        self.assertTrue(check.get_active())
        self.assertEqual(len(self.visibility),1)
        opaque=self.outline._bound_checks.get(('opaque',None))
        if opaque:
            self.assertFalse(opaque.get_sensitive())


if __name__=='__main__':unittest.main()
