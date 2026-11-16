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
APP_ID = "io.github.LightconeZhangYifa.GSVStudio"


def build(destination):
    sys.path.insert(0, str(ROOT / "viewer/ubuntu"))
    from gs_studio import PRODUCT_IDENTITY, VERSION
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gsv-deb-") as temporary:
        stage = Path(temporary)
        lib = stage / "usr/share/4dgs-studio"
        shutil.copytree(ROOT / "viewer/ubuntu/gs_studio", lib / "ubuntu/gs_studio", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "packages/gsv/src/gsv", lib / "ubuntu/gsv", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        identity = {key: PRODUCT_IDENTITY[key] for key in ("CFBundleDisplayName", "CFBundleName",
            "CFBundleVersion", "NSHumanReadableCopyright", "GSVStudioAuthor") if key in PRODUCT_IDENTITY}
        (lib / "ubuntu/gs_studio/product.json").write_text(json.dumps({"version": VERSION, "identity": identity}) + "\n")
        for language in ("en", "zh-Hans"):
            target = lib / f"Resources/{language}.lproj"
            target.mkdir(parents=True)
            shutil.copy2(ROOT / f"viewer/Resources/{language}.lproj/Localizable.strings", target)
        data = ROOT / "viewer/ubuntu/data"
        for file, folder in [(APP_ID+".desktop", "applications"), (APP_ID+".xml", "mime/packages"), (APP_ID+".metainfo.xml", "metainfo")]:
            if file.endswith(".xml"):
                ET.parse(data / file)
            target = stage / "usr/share" / folder
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(data / file, target)
        icons = stage / "usr/share/icons/hicolor/scalable/apps"
        icons.mkdir(parents=True)
        shutil.copy2(ROOT / "viewer/Resources/AppIcon.svg", icons / (APP_ID+".svg"))
        binary = stage / "usr/bin/4dgs-studio"
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/usr/bin/python3\nimport sys\nsys.path.insert(0, "/usr/share/4dgs-studio/ubuntu")\nfrom gs_studio.application import main\nraise SystemExit(main())\n')
        binary.chmod(0o755)
        doc = stage / "usr/share/doc/4dgs-studio"
        doc.mkdir(parents=True)
        shutil.copy2(ROOT / "LICENSE", doc / "copyright")
        shutil.copy2(ROOT / "viewer/ubuntu/README.md", doc / "README.md")
        shutil.copy2(ROOT / "viewer/ubuntu/PARITY.md", doc / "PARITY.md")
        shutil.copy2(ROOT / "viewer/ubuntu/DESKTOP_VALIDATION.md", doc / "DESKTOP_VALIDATION.md")
        shutil.copy2(ROOT / "viewer/ubuntu/FEATURE_AUDIT.md", doc / "FEATURE_AUDIT.md")
        shutil.copy2(ROOT / "viewer/ubuntu/feature-map.json", doc / "feature-map.json")
        control = stage / "DEBIAN"
        control.mkdir()
        (control / "control").write_text(f'''Package: 4dgs-studio
Version: {VERSION}~ubuntu1
Section: graphics
Priority: optional
Architecture: all
Maintainer: Yifa Zhang <139779251+Lightcone-ZhangYifa@users.noreply.github.com>
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, python3-numpy (>= 1.24), python3-opengl, gir1.2-gtk-4.0 (>= 4.10), gir1.2-adw-1 (>= 1.2)
Recommends: libgl1-mesa-dri, xdg-desktop-portal-gnome
Installed-Size: {sum(p.stat().st_size for p in stage.rglob('*') if p.is_file()) // 1024 + 1}
Homepage: https://github.com/Lightcone-ZhangYifa/4dgs-studio
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
        target = destination / f"4dgs-studio_{VERSION}~ubuntu1_all.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(target)], check=True)
        subprocess.run(["dpkg-deb", "--info", str(target)], check=True)
        return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default=ROOT / "viewer/dist")
    print(build(parser.parse_args().output))
