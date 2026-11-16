"""Validate the actual Debian payload, launcher and shared-language resources."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


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
        for filename in ('README.md', 'PARITY.md', 'copyright'):
            self.assertTrue((doc / filename).is_file())
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

    def test_desktop_mime_and_icon_registration_payload(self):
        share = self.stage / 'usr/share'
        desktop = share / 'applications' / (APP_ID + '.desktop')
        self.assertIn('Exec=4dgs-studio %F', desktop.read_text())
        self.assertIn('MimeType=application/x-gsv;', desktop.read_text())
        self.assertTrue((share / 'icons/hicolor/scalable/apps' / (APP_ID + '.svg')).is_file())
        self.assertTrue((share / 'mime/packages' / (APP_ID + '.xml')).is_file())
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
