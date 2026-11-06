#!/usr/bin/env bash
set -euo pipefail
VIEWER_ROOT="$(cd "$(dirname "$0")" && pwd)"
DIST="$VIEWER_ROOT/dist"
APP="$DIST/4DGS Studio.app"
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
  echo "4DGS Studio requires an Apple silicon Mac." >&2
  exit 1
fi
python3 "$VIEWER_ROOT/tools/audit_localization.py"
mkdir -p "$DIST"
STAGING="$(mktemp -d "$DIST/.build-XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
CANDIDATE="$STAGING/4DGS Studio.app"
mkdir -p "$CANDIDATE/Contents/MacOS" "$CANDIDATE/Contents/Resources"
cd "$VIEWER_ROOT"
xcrun swiftc native/*.swift -swift-version 5 -O -module-name FourDGSStudio \
  -file-prefix-map "$VIEWER_ROOT=viewer" -debug-prefix-map "$VIEWER_ROOT=viewer" \
  -target arm64-apple-macosx13.0 \
  -framework AppKit -framework MetalKit -framework CryptoKit \
  -o "$CANDIDATE/Contents/MacOS/4DGSStudio"
cp "$VIEWER_ROOT/native/Info.plist" "$CANDIDATE/Contents/Info.plist"
cp "$VIEWER_ROOT/native/Shaders.metal" "$CANDIDATE/Contents/Resources/"
cp "$VIEWER_ROOT/../LICENSE" "$CANDIDATE/Contents/Resources/LICENSE"
cp "$VIEWER_ROOT/Resources/GSVStudio.icns" "$CANDIDATE/Contents/Resources/"
cp -R "$VIEWER_ROOT/Resources/"*.lproj "$CANDIDATE/Contents/Resources/"
codesign --force --sign - "$CANDIDATE"
codesign --verify --deep --strict "$CANDIDATE"
# Publish only a complete, verified bundle. Retain an interrupted predecessor
# outside the disposable staging directory so a later build can recover it.
python3 - "$CANDIDATE" "$APP" <<'PY'
from pathlib import Path
import os
import shutil
import sys

candidate, app = map(Path, sys.argv[1:])
previous = app.with_name('.4DGS Studio.previous.app')
if previous.exists():
    if not app.exists():
        os.replace(previous, app)
    else:
        shutil.rmtree(previous)
if app.exists():
    os.replace(app, previous)
try:
    os.replace(candidate, app)
except BaseException:
    if previous.exists() and not app.exists():
        os.replace(previous, app)
    raise
if previous.exists():
    shutil.rmtree(previous)
PY
printf 'Built %s\n' "$APP"
