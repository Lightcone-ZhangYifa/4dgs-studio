"""Shared author identity and source icon lookup, without desktop input."""
from pathlib import Path
import os
import plistlib
import unittest

from gs_studio import APPLICATION_ID, VERSION
from gs_studio import branding


ROOT = Path(__file__).resolve().parents[3]


class BrandingTests(unittest.TestCase):
    def test_brand_identity_comes_from_the_native_bundle(self):
        with (ROOT / 'viewer/native/Info.plist').open('rb') as stream:
            identity = plistlib.load(stream)
        author = identity['GSVStudioAuthor']
        self.assertEqual(branding.APPLICATION_NAME, identity['CFBundleDisplayName'])
        self.assertEqual(VERSION, identity['CFBundleShortVersionString'])
        self.assertEqual(branding.AUTHOR_DISPLAY_NAME, author['Name'] + ' · ' + author['NativeName'])
        self.assertEqual(branding.AUTHOR_EMAIL, author['Email'])
        self.assertEqual(branding.AUTHOR_PROFILE_URL, author['ProfileURL'])
        self.assertEqual(branding.REPOSITORY_URL, author['RepositoryURL'])
        self.assertEqual(branding.ISSUES_URL, author['IssuesURL'])
        self.assertEqual(branding.COPYRIGHT, identity['NSHumanReadableCopyright'])

    def test_source_icon_is_the_canonical_vector_and_resolves_at_native_sizes(self):
        paths = branding.icon_search_paths()
        self.assertEqual(paths, (ROOT / 'viewer/ubuntu/data/icons',))
        icon = paths[0] / 'hicolor/scalable/apps' / (APPLICATION_ID + '.svg')
        self.assertTrue(icon.is_symlink())
        self.assertEqual(icon.resolve(), ROOT / 'viewer/Resources/AppIcon.svg')
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk
        theme = Gtk.IconTheme.new()
        theme.set_theme_name('hicolor')
        theme.add_search_path(str(paths[0]))
        self.assertTrue(theme.has_icon(APPLICATION_ID))
        gi.require_version('GdkPixbuf', '2.0')
        from gi.repository import GdkPixbuf
        for size in (16, 32, 64, 128, 256):
            pixels = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(icon), size, size, True)
            self.assertEqual((pixels.get_width(), pixels.get_height()), (size, size))


@unittest.skipUnless(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'),
                     'Native About widget checks need a display; use xvfb-run')
class BrandingGtkTests(unittest.TestCase):
    def test_welcome_and_about_use_resolvable_shared_branding(self):
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Adw, Gio, Gtk
        from gs_studio.application import StudioApplication, StudioWindow
        app = StudioApplication()
        # ApplicationTests keeps its application alive in this same process.
        # NON_UNIQUE avoids bus-name ownership, not duplicate object exports.
        app.set_application_id(APPLICATION_ID + '.BrandingTests')
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE | Gio.ApplicationFlags.HANDLES_OPEN)
        app.register(None)
        window = StudioWindow(app)
        dialog = None
        try:
            self.assertEqual(window.empty.get_icon_name(), APPLICATION_ID)
            self.assertTrue(Gtk.IconTheme.get_for_display(window.get_display()).has_icon(APPLICATION_ID))
            window.about()
            dialog = next(item for item in Gtk.Window.get_toplevels()
                          if isinstance(item, Adw.AboutWindow) and item.get_transient_for() is window)
            self.assertEqual(dialog.get_application_name(), branding.APPLICATION_NAME)
            self.assertEqual(dialog.get_application_icon(), APPLICATION_ID)
            self.assertEqual(dialog.get_version(), VERSION)
            self.assertEqual(dialog.get_developer_name(), branding.AUTHOR_DISPLAY_NAME)
            self.assertEqual(dialog.get_developers(), [branding.AUTHOR_DISPLAY_NAME])
            self.assertEqual(dialog.get_copyright(), branding.COPYRIGHT)
            self.assertEqual(dialog.get_website(), branding.REPOSITORY_URL)
            self.assertEqual(dialog.get_issue_url(), branding.ISSUES_URL)
        finally:
            if dialog is not None:
                dialog.destroy()
            window._finish_close()


if __name__ == '__main__':
    unittest.main()
