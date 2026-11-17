#!/usr/bin/env bash
# Launch the development frontend using a system-site-packages virtualenv.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${GSV_PYTHON:-$repo_dir/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  python_bin=/usr/bin/python3
fi
export PYTHONPATH="$repo_dir/viewer/ubuntu:$repo_dir/packages/gsv/src${PYTHONPATH:+:$PYTHONPATH}"
if ! "$python_bin" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_foreign("cairo"); import numpy, OpenGL' ; then
  echo 'Install Ubuntu dependencies: sudo apt install python3-gi python3-gi-cairo python3-numpy python3-opengl gir1.2-gtk-4.0 gir1.2-adw-1' >&2
  exit 1
fi
exec "$python_bin" -m gs_studio "$@"
