"""Validate the actual Debian payload, launcher and shared-language resources."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[3]
APP_ID = 'io.github.LightconeZhangYifa.GSVStudio'


@unittest.skipUnless(shutil.which('dpkg-deb'), 'Debian packaging tools are required')
class DebianPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='gsv-package-check-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name)
        process = subprocess.run([sys.executable, str(ROOT / 'viewer/tools/package_ubuntu.py'),
                                  str(cls.folder / 'dist')], capture_output=True, text=True)
        if process.returncode:
            raise RuntimeError(process.stdout + process.stderr)
        packages = list((cls.folder / 'dist').glob('*.deb'))
        if len(packages) != 1:
            raise RuntimeError('Expected one built Debian package')
        cls.package = packages[0]
        cls.stage = cls.folder / 'payload'
        subprocess.run(['dpkg-deb', '--extract', str(cls.package), str(cls.stage)], check=True)

    def test_payload_has_runtime_and_docs_without_build_host_files(self):
        app = self.stage / 'usr/share/4dgs-studio'
        self.assertTrue((app / 'ubuntu/gs_studio/application.py').is_file())
        self.assertTrue((app / 'ubuntu/gs_studio/model.py').is_file())
        self.assertTrue((app / 'ubuntu/gsv/container.py').is_file())
        self.assertTrue((app / 'Resources/en.lproj/Localizable.strings').is_file())
        self.assertTrue((app / 'Resources/zh-Hans.lproj/Localizable.strings').is_file())
        doc = self.stage / 'usr/share/doc/4dgs-studio'
        for filename in ('README.md', 'PARITY.md', 'DESKTOP_VALIDATION.md', 'FEATURE_AUDIT.md', 'copyright'):
            self.assertTrue((doc / filename).is_file())
        reports = re.findall(r'\]\((validation/[^)]+)\)', (doc / 'DESKTOP_VALIDATION.md').read_text())
        self.assertTrue(reports, 'The validation guide should link its retained reports')
        for relative in reports:
            self.assertTrue((doc / relative).is_file(), 'Packaged validation link is broken: '+relative)
        names = [str(p.relative_to(self.stage)) for p in self.stage.rglob('*')]
        self.assertFalse(any('__pycache__' in name or '.venv' in name or name.endswith('.gsv') for name in names))
        self.assertFalse(any('/tests/' in name or '/.git/' in name for name in names))

    def test_launcher_is_executable_and_targets_packaged_imports(self):
        binary = self.stage / 'usr/bin/4dgs-studio'
        self.assertTrue(os.access(binary, os.X_OK))
        content = binary.read_text()
        self.assertTrue(content.startswith('#!/usr/bin/python3\n'))
        self.assertIn('"/usr/share/4dgs-studio/ubuntu"', content)
        self.assertNotIn(str(ROOT), content)
        app = self.stage / 'usr/share/4dgs-studio/ubuntu'
        # Import from an extracted payload with no repository/current-working-
        # directory contribution, proving the shared SDK and catalogs ship.
        script = ('import sys,json; sys.path.insert(0,sys.argv[1]); '
                  'import gsv,gs_studio; from gs_studio.i18n import resources,Translator; '
                  'print(json.dumps({"sdk":gsv.__file__,"app":gs_studio.__file__, '
                  '"version":gs_studio.VERSION,"identity":gs_studio.PRODUCT_IDENTITY, '
                  '"resources":str(resources()),"en":len(Translator("en").catalog), '
                  '"zh":len(Translator("zh-Hans").catalog)}))')
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(app)],
                                cwd=self.folder, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        self.assertTrue(data['sdk'].startswith(str(app)))
        self.assertTrue(data['app'].startswith(str(app)))
        self.assertEqual(Path(data['resources']), app.parent / 'Resources')
        self.assertGreater(data['en'], 100)
        self.assertGreater(data['zh'], 100)
        with (ROOT / 'viewer/native/Info.plist').open('rb') as stream:
            source_identity = plistlib.load(stream)
        self.assertEqual(data['version'], source_identity['CFBundleShortVersionString'])
        for key in ('CFBundleDisplayName', 'CFBundleName', 'CFBundleVersion',
                    'NSHumanReadableCopyright', 'GSVStudioAuthor'):
            self.assertEqual(data['identity'][key], source_identity[key])

    def test_metadata_dependencies_and_root_archive_ownership(self):
        fields = subprocess.run(['dpkg-deb', '--field', str(self.package)],
                                check=True, capture_output=True, text=True).stdout
        for required in ('Architecture: all', 'python3-gi-cairo', 'python3-opengl',
                         'gir1.2-gtk-4.0 (>= 4.10)', 'gir1.2-adw-1'):
            self.assertIn(required, fields)
        raw = subprocess.run(['dpkg-deb', '--fsys-tarfile', str(self.package)],
                             check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            self.assertTrue(all(m.uid == 0 and m.gid == 0 for m in archive.getmembers()))

    def test_release_identity_and_upgrade_from_preview(self):
        with (ROOT / 'viewer/native/Info.plist').open('rb') as stream:
            identity = plistlib.load(stream)
        version = subprocess.check_output(['dpkg-deb', '--field', str(self.package), 'Version'], text=True).strip()
        self.assertEqual(version, identity['CFBundleShortVersionString'] + '-1')
        subprocess.run(['dpkg', '--compare-versions', version, 'gt',
                        identity['CFBundleShortVersionString'] + '~ubuntu1'], check=True)
        fields = subprocess.check_output(['dpkg-deb', '--field', str(self.package)], text=True)
        author = identity['GSVStudioAuthor']
        self.assertIn(f"Maintainer: {author['Name']} <{author['Email']}>", fields)
        self.assertIn('Homepage: ' + author['RepositoryURL'], fields)
        authors = (self.stage / 'usr/share/doc/4dgs-studio/AUTHORS').read_text()
        for key in ('Name', 'NativeName', 'ProfileURL', 'RepositoryURL'):
            self.assertIn(author[key], authors)
        component = ET.parse(self.stage / 'usr/share/metainfo' / (APP_ID + '.metainfo.xml')).getroot()
        self.assertEqual(component.find('developer/name').text, author['Name'] + ' · ' + author['NativeName'])
        release = component.find('./releases/release')
        self.assertEqual(release.get('version'), identity['CFBundleShortVersionString'])
        self.assertEqual(release.get('type'), 'stable')

    def test_desktop_mime_and_icon_registration_payload(self):
        share = self.stage / 'usr/share'
        desktop = share / 'applications' / (APP_ID + '.desktop')
        self.assertIn('Exec=4dgs-studio %F', desktop.read_text())
        self.assertIn('MimeType=application/x-gsv;', desktop.read_text())
        self.assertTrue((share / 'icons/hicolor/scalable/apps' / (APP_ID + '.svg')).is_file())
        self.assertEqual((share / 'icons/hicolor/scalable/apps' / (APP_ID + '.svg')).read_bytes(),
                         (ROOT / 'viewer/Resources/AppIcon.svg').read_bytes())
        # All raster variants derive from that SVG, retain alpha, and cover
        # native launcher/dock sizes without a second artwork source.
        import gi
        gi.require_version('GdkPixbuf', '2.0')
        from gi.repository import GdkPixbuf
        for size in (16, 22, 24, 32, 48, 64, 96, 128, 192, 256, 512):
            path = share / f'icons/hicolor/{size}x{size}/apps' / (APP_ID + '.png')
            pixels = GdkPixbuf.Pixbuf.new_from_file(str(path))
            self.assertEqual((pixels.get_width(), pixels.get_height()), (size, size))
            self.assertTrue(pixels.get_has_alpha())
        # Relocated payload imports can resolve the packaged icon without a
        # system install, CWD contribution or a source-tree fallback.
        script = ('import sys,json;sys.path.insert(0,sys.argv[1]); '
                  'from gs_studio.branding import icon_search_paths; '
                  'print(json.dumps([str(p) for p in icon_search_paths()]))')
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script,
                                 str(share / '4dgs-studio/ubuntu')], cwd=self.folder,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [str(share / 'icons')])
        self.assertTrue((share / 'mime/packages' / (APP_ID + '.xml')).is_file())
        if shutil.which('update-mime-database'):
            # Build only this extracted payload's database, then verify GIO's
            # public type/icon association in a fresh isolated process.
            env = dict(os.environ, XDG_DATA_DIRS=str(share) + ':/usr/local/share:/usr/share')
            subprocess.run(['update-mime-database', str(share / 'mime')], check=True,
                           env=env, capture_output=True, text=True)
            script = ('import json;from gi.repository import Gio; '
                      'kind,_=Gio.content_type_guess("example.gsv",b"GSV\\0\\r\\n\\x1a\\x0a"); '
                      'print(json.dumps({"type":kind,"icons":Gio.content_type_get_icon(kind).get_names()}))')
            result = subprocess.run([sys.executable, '-I', '-B', '-c', script], cwd=self.folder,
                                    env=env, check=True, capture_output=True, text=True)
            registered = json.loads(result.stdout)
            self.assertEqual(registered['type'], 'application/x-gsv')
            self.assertIn(APP_ID, registered['icons'])
        self.assertTrue((share / 'metainfo' / (APP_ID + '.metainfo.xml')).is_file())
        if shutil.which('desktop-file-validate'):
            subprocess.run(['desktop-file-validate', str(desktop)], check=True,
                           capture_output=True, text=True)
        if shutil.which('appstreamcli'):
            subprocess.run(['appstreamcli', 'validate', '--no-net',
                            str(share / 'metainfo' / (APP_ID + '.metainfo.xml'))],
                           check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
