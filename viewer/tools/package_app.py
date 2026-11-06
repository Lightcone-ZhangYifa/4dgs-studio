#!/usr/bin/env python3
"""Build and package the Apple silicon application for a GitHub Release."""
from pathlib import Path
import hashlib
import plistlib
import stat
import subprocess
import tempfile
import zipfile

VIEWER = Path(__file__).resolve().parents[1]
DIST = VIEWER / "dist"


def package():
    subprocess.run(["bash", str(VIEWER / "build_native.sh")], check=True)
    app = DIST / "4DGS Studio.app"
    contents = app / "Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    version = info["CFBundleShortVersionString"]
    binary = contents / "MacOS" / info["CFBundleExecutable"]
    resources = contents / "Resources"
    expected_resources = {
        resources / "Shaders.metal": VIEWER / "native/Shaders.metal",
        resources / "LICENSE": VIEWER.parent / "LICENSE",
        resources / "GSVStudio.icns": VIEWER / "Resources/GSVStudio.icns",
        **{resources / f"{lang}.lproj/Localizable.strings":
               VIEWER / f"Resources/{lang}.lproj/Localizable.strings"
           for lang in ("en", "zh-Hans")},
    }
    for bundled, source in expected_resources.items():
        if bundled.read_bytes() != source.read_bytes():
            raise ValueError(f"Unexpected resource contents: {bundled.relative_to(app)}")
    expected = {binary, contents / "Info.plist", contents / "_CodeSignature/CodeResources",
                *expected_resources}
    files = {p for p in app.rglob("*") if p.is_file()}
    if files != expected or any(p.is_symlink() for p in app.rglob("*")):
        raise ValueError("Application has missing or unexpected payload files")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    executable = binary.read_bytes()
    if str(Path.home()).encode() in executable or str(VIEWER).encode() in executable:
        raise ValueError("Executable embeds a private build path")
    destination = DIST / f"4DGS-Studio-{version}-macOS-arm64.zip"
    with tempfile.TemporaryDirectory(dir=DIST) as work:
        archive = Path(work) / destination.name
        # Only archive bundle bytes and portable permissions. Using explicit
        # ZipInfo records omits the host UID, GID, xattrs and build timestamps.
        with zipfile.ZipFile(archive, mode="w") as zipped:
            for path in sorted(expected):
                entry = zipfile.ZipInfo(path.relative_to(DIST).as_posix())
                entry.create_system = 3
                entry.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if path == binary else 0o644
                entry.external_attr = (stat.S_IFREG | mode) << 16
                zipped.writestr(entry, path.read_bytes())
        expected_members = {str(p.relative_to(DIST)) for p in expected}
        with zipfile.ZipFile(archive) as zipped, archive.open("rb") as raw:
            members = [item.filename for item in zipped.infolist() if not item.is_dir()]
            if set(members) != expected_members or len(members) != len(expected_members):
                raise ValueError("Release archive contains unexpected entries")
            member = str(binary.relative_to(DIST))
            if not (zipped.getinfo(member).external_attr >> 16) & stat.S_IXUSR:
                raise ValueError("Archived application executable lost its execute bit")
            # Local headers can hold extra fields absent from the central directory.
            for item in zipped.infolist():
                raw.seek(item.header_offset + 28)  # Two-byte local extra-field length.
                if item.extra or raw.read(2) != b"\x00\x00":
                    raise ValueError(f"Unexpected ZIP extra field: {item.filename}")
            if zipped.testzip() or zipped.read(member) != executable:
                raise ValueError("Release archive verification failed")
        extracted = Path(work) / "extracted"
        subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
        installed = extracted / app.name
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(installed)], check=True)
        for source in expected:
            restored = installed / source.relative_to(app)
            if restored.read_bytes() != source.read_bytes():
                raise ValueError(f"Unpacked bytes differ: {source.relative_to(app)}")
        if not ((extracted / member).stat().st_mode & stat.S_IXUSR):
            raise ValueError("Unpacked application executable lost its execute bit")
        archive.replace(destination)
    print(f"{hashlib.sha256(destination.read_bytes()).hexdigest()}  {destination.name}")
    return destination


if __name__ == "__main__":
    package()
