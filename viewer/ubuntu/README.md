# 4DGS Studio for Ubuntu Desktop

Native GTK 4 / libadwaita frontend for Ubuntu 24.04 LTS or later, with a desktop
OpenGL 3.3 GPU. It uses system file dialogs, keyboard focus and accessibility,
GNOME appearance preferences, file-manager opening, drag and drop, and native
Wayland or X11 windows. Sources stay read-only. Python owns the application and
shared GSV semantics; NumPy evaluates geometry and OpenGL draws instanced
anisotropic splats. There is no browser, web server or model code execution.

## Run from source

Install runtime dependencies from Ubuntu's repositories:

```sh
sudo apt install python3-gi python3-gi-cairo python3-numpy python3-opengl \
  gir1.2-gtk-4.0 gir1.2-adw-1 libgl1-mesa-dri
bash viewer/build_ubuntu.sh
bash viewer/build_ubuntu.sh /absolute/path/to/scene.gsv
```

`build_ubuntu.sh` launches directly from the checkout. It uses `.venv/bin/python`
when available, otherwise `/usr/bin/python3`. Set `GSV_PYTHON=/usr/bin/python3`
to select Ubuntu's interpreter explicitly. For development, create a virtualenv
with `/usr/bin/python3 -m venv --system-site-packages .venv`; PyGObject and its
Cairo converter come from Ubuntu, rather than compiling GTK bindings with pip.
No pip installation is required to launch the checkout. `4dgs-studio --version`
reports the installed product version.

Generate a public synthetic scene with:

```sh
PYTHONPATH=packages/gsv/src /usr/bin/python3 packages/gsv/examples/write_scene.py /tmp/example.gsv
bash viewer/build_ubuntu.sh /tmp/example.gsv
```

## Desktop installation

Build the architecture-independent Debian package without root:

```sh
python3 viewer/tools/package_ubuntu.py
sudo apt install './viewer/dist/4dgs-studio_2.3.0~ubuntu1_all.deb'
```

This registers an application launcher, the `application/x-gsv` MIME type and
an icon without changing your default application associations. Files can be
opened from Files or by `4dgs-studio path.gsv`. Distro dependencies supply GTK,
OpenGL and NumPy; the package bundles the exact shared SDK and translation
catalogs used by this source tree. Product version metadata derives from the same
`viewer/native/Info.plist` as macOS; no separate Linux version bump is needed. The SDK is private to the application and
does not replace another installed `gsv` Python package. Uninstall with
`sudo apt remove 4dgs-studio`.

## Workspace and interaction

The workspace keeps the macOS arrangement: Scene Outliner on the left,
viewport and timeline in the center, inspector on the right. Paned dividers
resize the panels. Object rows support multiple selection and per-object
visibility. Double-click a camera to look through it or a render view to compare
it. Double-click geometry from another space/clock to activate that domain;
no implicit coordinate conversion or clock alignment occurs. Unknown source
representations remain inspectable and never execute embedded model code.

| Input | Action |
| --- | --- |
| Ctrl+O / drop a GSV | Open scene |
| Click / Ctrl-click | Select / toggle a primitive or object |
| Alt+left drag / middle drag | Orbit |
| Shift+left or middle drag / right drag | Pan |
| Ctrl+left drag / wheel | Dolly |
| Pinch / rotate | Dolly / roll |
| F / Home | Frame selection / scene |
| Space / left or right arrow | Play or pause / step a source sample |
| Ctrl+K | Record the view into the selected authored camera |
| Ctrl+Z / Ctrl+Shift+Z | Undo / redo camera edits |
| Ctrl+S | Save camera animation to a separate GSV |
| Ctrl+Shift+S / Ctrl+Alt+Shift+S | Save screenshot / workspace snapshot |
| Ctrl+1 / Ctrl+2 / F11 | Toggle outliner / inspector / fullscreen |

The Viewport inspector provides turntable/trackball navigation, up axis, standard
views, trajectory density, opacity and trail length. The preview strip toggles
the grid, cameras, paths and trails, and adjusts render resolution independently
of trajectory density. The menu provides separate Previous/Next View history.
Exact signed 64-bit IDs can be entered in the inspector without floating-point
rounding. Paths split at validity gaps; a picked identity retains its path even
at zero field density. Trails follow native source intervals.

The timeline scrubs document-native time. Scroll to zoom, middle-drag to pan,
or use Fit. Playback has explicit native-units/s, speed, loop and in/out range
controls; source FPS is never inferred. Click a camera key diamond to inspect
it, drag to retime it, or enter an exact time in the Camera inspector. Camera
name, horizontal FOV, position XYZ and rotation WXYZ fields apply with Enter.
Create Camera from View, then move the viewport and record keys. Saves and
imports preserve the same camera schema, SLERP interpolation and source manifest
digest as macOS. Camera-only files opened as sources remain read-only; reopen
the original reconstruction and import its animation for editing.

Preferences follow system light/dark appearance and select English or Simplified
Chinese for the next launch. The canonical translations are shared with macOS.
Preferences are stored under `$XDG_CONFIG_HOME/4dgs-studio` (normally `~/.config`).

## Calibrated images

Choose a render view and enable Render / GT. The optional observation selector
resolves multiple explicitly linked reference collections. Captures run on a
worker, use the declared full raster and exact geometry/camera/image time, and
publish PSNR, SSIM, MSE and MAE only when compatible. Without observations the
same action shows a calibrated image. Both panes share pan and zoom, with Fit Both and one-image-pixel-per-display-pixel controls.
Image inspection transforms persist across time changes. Screenshot export saves
the completed viewport or calibrated pair; workspace export includes native
controls. Both capture before the save dialog appears. Confirmed file exports run in a
serialized FIFO queue and finish even when a scene is replaced or the window closes.
Obsolete scene evaluation and capture results cannot replace the current display.

Quantitative captures use a separate Float32 CPU EWA implementation of the
stored profile, rather than reading the interactive GL preview. Metrics and
color conversions reuse the SDK. The status tooltip identifies the capture
contract and implementation; source-renderer pixel parity is unverified.
Unsupported distortion, temporal integration and invalid comparison mappings
report a reason instead of producing scores. Captures are limited to 16 MP and
16,384 pixels on an axis. CPU capture can be slow for large scenes; it is
cancellable when the scene, time or selected view changes. See the
[feature and acceptance inventory](PARITY.md) before treating this port as a
hardware-qualified release.

## Checks

```sh
sudo apt install xvfb python3-venv desktop-file-utils appstream
env -u DISPLAY -u WAYLAND_DISPLAY PYTHONPATH=viewer/ubuntu:packages/gsv/src /usr/bin/python3 -m unittest discover -s viewer/ubuntu/tests -v
PYTHONPATH=viewer/ubuntu:packages/gsv/src LIBGL_ALWAYS_SOFTWARE=1 \
  GDK_BACKEND=x11 GDK_DEBUG=no-vsync GTK_A11Y=none GIO_USE_VFS=local \
  xvfb-run -a -s '-screen 0 1920x1080x24' \
  /usr/bin/python3 -m unittest discover -s viewer/ubuntu/tests -v
python3 viewer/tools/package_ubuntu.py
```

The first command runs headless numerical, ownership and packaging checks and
skips display-dependent tests. The second also exercises real GTK windows and
OpenGL contexts with Mesa, including playback, picking, calibrated images,
resize, GPU resource disposal, source replacement, file I/O and close races.
Only chooser completion is substituted in automated application tests; portal
interaction itself requires manual desktop acceptance. `GDK_DEBUG=no-vsync`
works around the Xvfb frame-clock limitation; do not set it on a normal desktop.

A reproducible renderer benchmark is available at
`viewer/ubuntu/tools/benchmark_viewport.py`. It separates CPU submission and
`glFinish` completed-frame time and records renderer/driver/size. This environment
has only software llvmpipe: no NVIDIA/AMD/Intel hardware frame-rate or Wayland
smoothness claim follows from these measurements. Run the same benchmark and
the manual acceptance checklist on the target desktop before release.
