# 4DGS Studio for Ubuntu Desktop

Native GTK 4 / libadwaita frontend for Ubuntu 24.04 LTS or later, with a desktop
OpenGL 3.3 GPU. It uses system file dialogs, keyboard focus and accessibility,
GNOME appearance preferences, file-manager opening, drag and drop, and native
Wayland or X11 windows. Sources stay read-only. Python owns the application and
shared GSV semantics. Static and persistent fields stream immutable source
endpoints to OpenGL for interpolation, quaternion SLERP, covariance and splats;
OpenGL 4.3 hardware also sorts depth on the GPU. Other supported mathematical
encodings keep a validated NumPy fallback. There is no browser, web server or model code execution.

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
OpenGL and NumPy; the package bundles this repository’s Python gsv SDK and shared translation
catalogs used by this source tree. Product version metadata derives from the same
`viewer/native/Info.plist` as macOS; no separate Linux version bump is needed. The SDK is private to the application and
does not replace another installed `gsv` Python package. Uninstall with
`sudo apt remove 4dgs-studio`.

## Workspace and interaction

The workspace keeps the macOS arrangement: Scene Outliner on the left,
viewport and timeline in the center, inspector on the right. Paned dividers
resize the panels. The native GTK tree groups representations, cameras, motion,
observations, appearance, rendering and supporting data in the same order as
macOS. Categories show object counts and collapse; multi-view camera rigs expose
individual view children, while single-view cameras have no redundant child.
Large lists retain GTK virtualization. Selection inspects an object or camera
view without changing the viewing camera. Object rows support multiple selection
and per-object visibility. Double-click a camera to look through it or a render view to compare
it. Double-click geometry from another space/clock to activate that domain;
no implicit coordinate conversion or clock alignment occurs. Unknown source
representations remain inspectable and never execute embedded model code.

The inspector follows the same Selection / Viewport / Document organization.
Camera editing appears in its selection context; document coordinates, clocks,
units and storage information have their own page. Inspection values update in
place during playback, retaining scrolling, expanded descriptors and widget
identity. Inactive identities remain inspectable, and scaffold display glyphs
are kept separate from measured node attributes.

| Input | Action |
| --- | --- |
| Ctrl+O / drop a GSV | Open scene |
| Click / Ctrl-click | Select / toggle a primitive or object |
| Alt+left drag / middle drag | Orbit |
| Shift+left or middle drag / right drag | Pan |
| Ctrl+left drag / wheel | Dolly |
| Pinch / rotate | Dolly / roll |
| F / Home | Frame selection / scene |
| 1 / 3 / 7 in the viewport | Front / Right / Top perspective |
| Space / left or right arrow | Play or pause / step a source sample |
| Ctrl+K | Record the view into the selected authored camera |
| Ctrl+Z / Ctrl+Shift+Z | Native text undo/redo when editing, otherwise camera undo/redo |
| Ctrl+Alt+Z / Ctrl+Alt+Shift+Z | Explicit camera undo / redo |
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
name, horizontal FOV, position XYZ and rotation WXYZ fields apply with Enter or
their Apply button.
Create Camera from View, then move the viewport and record keys. Saves and
imports preserve the same camera schema, SLERP interpolation and source manifest
digest as macOS. Camera-only files opened as sources remain read-only; reopen
the original reconstruction and import its animation for editing.

Committing a valid preview rate or range pauses playback. Entering an exact
Time adds that time to the session timeline; source timestamps remain unchanged.
Document and header object/sample counts describe the workspace, including
authored cameras and session times. FOV edits accept the native 1°–170° inclusive
range and scale skew together with the two focal lengths.

The selection-member menu chooses a primary object or identity without changing
mixed selection membership; inspector fields label the owning object, even when
two objects use the same identity. Selecting a source camera or geometry through
the Outliner clears the authored-camera editing target. The **View camera
dropdown is independent of selection**: looking through a source camera does
not select it or clear an already selected authored camera. Record therefore
captures the currently displayed view into that selected authored track. An
incompatible space/clock leaves the existing view intact and reports the reason.
One-key cameras in the same space can be viewed across clocks because their
pose is constant. Recording, transform/FOV edits, key jumps and retiming still
require the active space/clock; foreign keys stay out of its timeline lane.
Without a selected owner, an ID shared by multiple fields is rejected as
ambiguous. Copy Attributes retains literal entity/tag text exactly.
Authored keys extend the transport timeline, including empty/static sources.

Authored playback reuses a read-only Float64 key-time index. The inspector shows
the key count and retains all times in its expandable descriptor; the key
selector and raw descriptor rebuild when the track changes, rather than on
every display tick. Camera imports build one validated, ordered key list, with
cancellation checkpoints every 128 keys. Large imports still perform the full
per-key numerical checks on a worker; the measured 100,000-key case took about
17 seconds. Source scenes and imported animation files stay unchanged.

Preferences follow system light/dark appearance and select English or Simplified
Chinese for the next launch. The canonical translations are shared with macOS.
Preferences are stored under `$XDG_CONFIG_HOME/4dgs-studio` (normally `~/.config`).
About and packaged identity use the canonical `viewer/native/Info.plist`,
including its author record, rather than a separate Linux author/version copy.

## Calibrated images

Choose a render view and enable Render / GT. The observation and capture-time
selectors identify the exact reference collection and timestamp. Switching views
activates the view's named space/clock. The Preview render view option explicitly
renders without a reference or scores, even if the view also links observations. Captures run on a
worker, use the declared full raster and exact geometry/camera/image time, and
publish PSNR, SSIM, MSE and MAE only when compatible. Without observations the
same action shows a calibrated image. Both panes share drag/scroll pan and Alt-scroll/pinch zoom, with Fit Both and one-image-pixel-per-display-pixel controls.
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
interaction itself requires separate desktop acceptance. `GDK_DEBUG=no-vsync`
works around the Xvfb frame-clock limitation; do not set it on a normal desktop.

Graphics Information in the application menu reports the actual current renderer,
display protocol and sorting backend. Installed NVIDIA cards alone do not prove
that a Wayland or remote-desktop context is hardware accelerated. The desktop
entry requests GNOME's standard non-default-GPU preference. When diagnosing this
host's remote session, native Wayland selected llvmpipe, while an NVIDIA PRIME
Xwayland launch created an actual RTX 4090 OpenGL 4.6 context. Driver/backend
selection remains per process; the application does not rewrite desktop or GPU
settings. The validation record in `PARITY.md` separates those paths.

The performance label reports the latest completed **frustum-visible / source
identity** count. A nonblocking GL 3.3 point/geometry query reuses the splat
projection and samples at most 10 times per second; its age and source time are
shown in the tooltip. This counts valid, opaque-enough, finite projected 3σ
bounds intersecting the raster, including Gaussians occluded by other geometry.
It is not a count of unoccluded pixels or an estimate from active IDs. Changing
the source/cohort clears old results; empty geometry is exactly zero without a
GPU sample, and an unavailable query displays “—”. The tooltip also separates
CPU submission, asynchronous GPU render/sort timing, completed-render rate and
estimated GPU storage. Completion rate is not desktop presentation FPS. Timing
or visibility-query failure leaves rendering available. Framebuffer
resizing allocates replacement targets transactionally, so allocation failure
retains the previous target and a lower preview resolution can recover.

[FEATURE_AUDIT.md](FEATURE_AUDIT.md) maps individual macOS operations to the GTK
frontend, named regressions and actual-session check IDs. The CI
`audit_studio_features.py` completeness guard covers all 80 current macOS desktop
action callbacks in the source map; that is not runtime acceptance of every
action. Both frontends follow the same GSV and camera/render specifications and share
compatibility fixtures, translation catalogs and canonical product metadata.
macOS uses its independent Swift loader/renderer; Ubuntu reuses the Python gsv SDK.

A reproducible renderer benchmark is available at
`viewer/ubuntu/tools/benchmark_viewport.py`. It separates CPU submission and
`glFinish` completed-frame time and records renderer/driver/size. Actual RTX 4090 renderer measurements are recorded in `PARITY.md`, along with
the software reference. Resident playback timing does not include source-file
reads, first endpoint uploads or desktop presentation. Run the same benchmark
and manual acceptance checklist for each target driver before release.

## Actual GNOME desktop acceptance

The [desktop acceptance record](DESKTOP_VALIDATION.md) distinguishes the clean
`599ca1c` basic native-portal runs from later working-tree window regressions.
The recorded Ubuntu suite ran 238 tests successfully, with two benchmark tests
skipped; the SDK suite passed 78 tests. A separate actual GNOME window run passed
31 application tests on each of NVIDIA Xwayland and software Wayland at
`8d54a00`, with chooser completion still substituted. **The current expanded `--full` desktop
workflow has not completed acceptance.** These results do not establish current
full portal, physical-input or cross-platform acceptance.
Later working-tree EIS runs passed five parity groups, three comparison groups
and three lifecycle groups; the preferences check stopped on a test-tool
ComboRow name/value mismatch. That tool correction still awaits a complete clean
run, so these are partial results, not a full pass.

`tools/test_desktop_session.py` requires explicit
`--allow-desktop-input`; it never runs from unit-test discovery or CI by accident.
A disconnected session can opt into a temporary Mutter output with
`--virtual-output`, which is removed on completion.

`tools/benchmark_streaming.py` separately measures source validation, cold CPU
endpoint preparation and GPU interval upload in an EGL device context without
creating a desktop window. It complements the resident renderer benchmark;
source streaming and first allocation must not be reported as steady-state FPS.
