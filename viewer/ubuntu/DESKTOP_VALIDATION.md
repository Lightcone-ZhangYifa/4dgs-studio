# Native desktop acceptance

The opt-in [desktop runner](tools/test_desktop_session.py) starts the normal
application as a separate process and operates its real GTK accessibility tree,
GNOME file chooser portals, keyboard input and file exports. It does not mock
widgets, call application internals or use Xvfb. Existing Studio processes are
rejected unless their test PID is explicitly supplied.

## Verified on 2026-11-16

| Application protocol | Actual OpenGL implementation | Result |
| --- | --- | --- |
| Native GNOME Wayland | Mesa 25.2.8, llvmpipe, OpenGL 4.5 | Native file opening, playback, timeline entry, camera editing/import/export, workspace export and calibrated comparison passed. |
| GNOME Xwayland with NVIDIA PRIME | NVIDIA GeForce RTX 4090, driver 620.70.06, OpenGL 4.6 | The same workflow passed, including real XTEST middle-drag input verified by different exported camera poses. |

Both runs used Ubuntu's PyOpenGL **3.1.7**, Python **3.12.3**, NumPy **1.26.4**,
GTK **4.14.5** and libadwaita **1.5.0**. The test session had no connected monitor.
A temporary **1920 × 1080 Mutter virtual output** supplied a real native
compositor output and keyboard focus. The runner discarded PipeWire video into
a `fakesink`, stopped the session afterwards, and verified the logical-output
count returned from one to zero. No physical-monitor or manual Wayland mouse
acceptance is claimed; Wayland's strict mapped-pointer check is a separate
`--test-orbit` option.

The actual workflow verifies:

1. Open a generated GSV through the native chooser using the real location
   entry, keyboard path input and Return; play and pause the loaded scene.
2. Select its Gaussian field in the native outliner and request its paths.
3. Create a camera, edit the actual timeline entry, record keys at **0** and
   **0.5**, and save through the native camera file chooser.
4. Undo and redo a camera edit, delete the authored camera, import the saved
   animation through the native chooser, and save it again. The two camera
   exports retain identical identities, timestamps and poses; tested exports
   were byte-identical.
5. Save the application's own workspace PNG. On Xwayland the recorded camera
   poses also prove that native viewport drag input changed the view.
6. Activate Render / GT, wait for numerical scores, and export the calibrated
   pair at **1280 × 480**, preserving two **640 × 480** source rasters.
7. Read the application's actual Graphics Information dialog, verify the source
   GSV's SHA256 is unchanged, close the owned test process and remove the
   temporary compositor output.

The reference image is an intentionally unrelated synthetic API image. The
reported PSNR near **5.15735** verifies comparison plumbing, not reconstruction
quality. Quantitative renderer tests and cross-platform contract fixtures are
listed in [PARITY.md](PARITY.md).

## Reproduce

Run from a GNOME session with the application's distribution dependencies and
AT-SPI bindings installed. The optional virtual-output path also needs GStreamer
GI bindings and its PipeWire plugin; Xwayland pointer input uses `libXtst`.
Use a new output directory for each run and avoid concurrent desktop interaction.

```sh
/usr/bin/python3 viewer/ubuntu/tools/test_desktop_session.py \
  --allow-desktop-input --backend wayland --python /usr/bin/python3 \
  --output /tmp/4dgs-wayland-acceptance
```

For NVIDIA PRIME offload, use the GNOME session's local Xwayland `DISPLAY` and
`XAUTHORITY`; the runner applies driver selection only to the tested process:

```sh
/usr/bin/python3 viewer/ubuntu/tools/test_desktop_session.py \
  --allow-desktop-input --backend x11 --prime --python /usr/bin/python3 \
  --output /tmp/4dgs-xwayland-acceptance
```

Add `--virtual-output` only when the running GNOME compositor has zero active
outputs, such as a disconnected remote desktop. The session-scoped output does
not change persistent display settings. The application receives an isolated
test configuration directory. Native file portals may remember their last folder
and recent synthetic test files as part of their ordinary desktop behavior.

Each output directory contains `report.json`, the application log, synthetic
source, both camera exports, workspace PNG and calibrated comparison PNG. The
report records the source commit and working-tree changes, actual library and
driver versions, completed steps, artifact hashes, source integrity and cleanup.
For release evidence, rerun from the committed source and retain both reports.
Only the application's workspace and its calibrated images are saved; no full
desktop screenshot or other application's content is captured.
