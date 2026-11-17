#!/usr/bin/env python3
"""Build a Debian application tree and .deb using only distro dependencies.

No root, pip, downloaded binary or build-host virtualenv is embedded. The shared
GSV SDK and both canonical language catalogs ship in the same application tree.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
ICON_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 192, 256, 512)
DEBIAN_REVISION = '1'


def install_icons(stage, application_id):
    """Generate every raster from the shared macOS/Ubuntu SVG, without a display."""
    import gi
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import GdkPixbuf

    source = ROOT / 'viewer/Resources/AppIcon.svg'
    icons = stage / 'usr/share/icons/hicolor'
    scalable = icons / 'scalable/apps'
    scalable.mkdir(parents=True)
    shutil.copy2(source, scalable / (application_id + '.svg'))
    for size in ICON_SIZES:
        directory = icons / f'{size}x{size}/apps'
        directory.mkdir(parents=True)
        pixels = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(source), size, size, True)
        pixels.savev(str(directory / (application_id + '.png')), 'png', [], [])


def install_appstream(source, target, application_id, name, author, repository, issues, version):
    """Export canonical identity while retaining the reviewed release history."""
    tree = ET.parse(source)
    component = tree.getroot()
    component.find('id').text = application_id
    for item in component.findall('name'):
        item.text = name
    component.find('developer/name').text = author
    component.find('launchable').text = application_id + '.desktop'
    for item in component.findall('url'):
        if item.get('type') == 'homepage':
            item.text = repository
        elif item.get('type') == 'bugtracker':
            item.text = issues
    release = component.find(f"./releases/release[@version='{version}']")
    if release is None:
        raise ValueError(f'AppStream needs a reviewed release entry for {version}')
    release.set('type', 'stable')
    tree.write(target, encoding='utf-8', xml_declaration=True)


def build(destination):
    sys.path.insert(0, str(ROOT / "viewer/ubuntu"))
    from gs_studio import APPLICATION_ID, PRODUCT_IDENTITY, VERSION
    from gs_studio.branding import (APPLICATION_NAME, AUTHOR_NAME, AUTHOR_DISPLAY_NAME,
                                    AUTHOR_EMAIL, AUTHOR_PROFILE_URL, REPOSITORY_URL, ISSUES_URL)
    debian_version = f'{VERSION}-{DEBIAN_REVISION}'
    if not all((AUTHOR_NAME, AUTHOR_EMAIL, REPOSITORY_URL, ISSUES_URL)):
        raise ValueError('Canonical application identity is missing packaging author or project fields')
    if any('\n' in value or '\r' in value for value in (AUTHOR_NAME, AUTHOR_EMAIL)):
        raise ValueError('Debian maintainer identity must be a single line')
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gsv-deb-") as temporary:
        stage = Path(temporary)
        lib = stage / "usr/share/4dgs-studio"
        shutil.copytree(ROOT / "viewer/ubuntu/gs_studio", lib / "ubuntu/gs_studio", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "packages/gsv/src/gsv", lib / "ubuntu/gsv", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        identity = {key: PRODUCT_IDENTITY[key] for key in ("CFBundleDisplayName", "CFBundleName",
            "CFBundleVersion", "NSHumanReadableCopyright", "GSVStudioAuthor") if key in PRODUCT_IDENTITY}
        (lib / "ubuntu/gs_studio/product.json").write_text(json.dumps({"version": VERSION, "identity": identity}, ensure_ascii=False) + "\n")
        for language in ("en", "zh-Hans"):
            target = lib / f"Resources/{language}.lproj"
            target.mkdir(parents=True)
            shutil.copy2(ROOT / f"viewer/Resources/{language}.lproj/Localizable.strings", target)
        data = ROOT / "viewer/ubuntu/data"
        for file, folder in [(APPLICATION_ID+".desktop", "applications"), (APPLICATION_ID+".xml", "mime/packages"), (APPLICATION_ID+".metainfo.xml", "metainfo")]:
            if file.endswith(".xml"):
                ET.parse(data / file)
            target = stage / "usr/share" / folder
            target.mkdir(parents=True, exist_ok=True)
            if file.endswith('.metainfo.xml'):
                install_appstream(data / file, target / file, APPLICATION_ID, APPLICATION_NAME,
                                  AUTHOR_DISPLAY_NAME, REPOSITORY_URL, ISSUES_URL, VERSION)
            else:
                shutil.copy2(data / file, target)
        install_icons(stage, APPLICATION_ID)
        binary = stage / "usr/bin/4dgs-studio"
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/usr/bin/python3\nimport sys\nsys.path.insert(0, "/usr/share/4dgs-studio/ubuntu")\nfrom gs_studio.application import main\nraise SystemExit(main())\n')
        binary.chmod(0o755)
        doc = stage / "usr/share/doc/4dgs-studio"
        doc.mkdir(parents=True)
        shutil.copy2(ROOT / "LICENSE", doc / "copyright")
        (doc / 'AUTHORS').write_text(f'{APPLICATION_NAME}\n\n{AUTHOR_DISPLAY_NAME}\n'
            f'{AUTHOR_PROFILE_URL}\n{REPOSITORY_URL}\n\n'
            f'Debian maintainer identity: {AUTHOR_NAME} <{AUTHOR_EMAIL}>\n'
            'The GitHub noreply address is an identity, not a support inbox.\n'
            f'Issue tracker: {ISSUES_URL}\n')
        shutil.copy2(ROOT / "viewer/ubuntu/README.md", doc / "README.md")
        shutil.copy2(ROOT / "viewer/ubuntu/PARITY.md", doc / "PARITY.md")
        shutil.copy2(ROOT / "viewer/ubuntu/DESKTOP_VALIDATION.md", doc / "DESKTOP_VALIDATION.md")
        shutil.copy2(ROOT / "viewer/ubuntu/RELEASE_VALIDATION.md", doc / "RELEASE_VALIDATION.md")
        shutil.copy2(ROOT / "viewer/ubuntu/PRODUCTION_VALIDATION.md", doc / "PRODUCTION_VALIDATION.md")
        shutil.copy2(ROOT / "viewer/ubuntu/FEATURE_AUDIT.md", doc / "FEATURE_AUDIT.md")
        shutil.copy2(ROOT / "viewer/ubuntu/feature-map.json", doc / "feature-map.json")
        shutil.copytree(ROOT / "viewer/ubuntu/validation", doc / "validation")
        control = stage / "DEBIAN"
        control.mkdir()
        (control / "control").write_text(f'''Package: 4dgs-studio
Version: {debian_version}
Section: graphics
Priority: optional
Architecture: all
Maintainer: {AUTHOR_NAME} <{AUTHOR_EMAIL}>
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, python3-numpy (>= 1.24), python3-opengl, gir1.2-gtk-4.0 (>= 4.10), gir1.2-adw-1 (>= 1.2)
Recommends: libgl1-mesa-dri, xdg-desktop-portal-gnome, fonts-noto-cjk
Installed-Size: {sum(p.stat().st_size for p in stage.rglob('*') if p.is_file()) // 1024 + 1}
Homepage: {REPOSITORY_URL}
Description: Native dynamic Gaussian scene studio
 GTK 4 and libadwaita desktop frontend with OpenGL splatting, persistent
 identity inspection, calibrated image comparison and camera animation.
 Source reconstruction data remains read-only.
''')
        # desktop-file-utils, shared-mime-info and hicolor-icon-theme own the
        # relevant dpkg triggers; installation does not modify user defaults.
        for p in stage.rglob("*"):
            if p.is_dir():
                p.chmod(0o755)
            elif p != binary:
                p.chmod(0o644)
        target = destination / f"4dgs-studio_{debian_version}_all.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(target)], check=True)
        subprocess.run(["dpkg-deb", "--info", str(target)], check=True)
        return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default=ROOT / "viewer/dist")
    print(build(parser.parse_args().output))
