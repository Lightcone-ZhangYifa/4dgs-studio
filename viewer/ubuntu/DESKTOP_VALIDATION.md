# Desktop validation and native acceptance

The opt-in [desktop runner](tools/test_desktop_session.py) starts the normal
application as a separate process and operates its real GTK accessibility tree,
GNOME file chooser portals, keyboard input and file exports. It does not mock
widgets, call application internals or use Xvfb. Existing Studio processes are
rejected unless their test PID is explicitly supplied.

Two verification paths must be kept separate: the runner above tests native
portal/input delivery, while `test_application.py` can exercise real compositor,
GTK and OpenGL windows with chooser completion still replaced by a chosen path.
The latter is useful runtime evidence but does not test native file-dialog input.
**There is no complete passing current-source expanded `--full` desktop report.**

## Current automated and real-window evidence

The recorded Ubuntu regression ran **238 tests: 236 passed, two benchmark tests
skipped**; the SDK suite passed **78 tests**. These numerical, GTK and GL checks
do not by themselves establish native-portal or physical-input acceptance.

On 2026-11-16, the then-current 31 application tests also ran directly on the
actual GNOME session at source
`8d54a006ae304d19ea1166e6461efea495db4d1a`, with only the four documentation
files changed in the working tree, as recorded in the report:

| Actual GNOME window regression | Observed graphics implementation | Application tests |
| --- | --- | --- |
| Xwayland, per-process NVIDIA PRIME | RTX 4090, NVIDIA 620.70.06, OpenGL 4.6 | 31/31 passed, 13.427 seconds in unittest |
| Wayland | Mesa 25.2.8 llvmpipe LLVM 20.1.2, OpenGL 4.5 | 31/31 passed, 11.619 seconds in unittest |

The existing session output was 3420×2148 at scale 2; logical-monitor count
remained **1→1**. No temporary output was added for these final runs. All owned
test processes exited and Studio had no remaining application-ID owner. The
harness injected no keyboard or pointer input and captured no other desktop
content. The actual application widgets, workers and rendering contexts ran;
**file chooser completion remained mocked**. This includes regression checks for
view-camera/selection independence, recording the current view, Apply/Enter
camera edits and current-near-clip authoring, not portal delivery of those edits.

The working-session report/logs are
`/tmp/gsv-real-application-release-report.json` and
`/tmp/gsv-real-application-release-{x11,wayland}.log`; these temporary artifacts
are separate from the checked-in historical reports below. The earlier
`f194762` probe's finalization traceback was corrected and the `8d54a00`
probe/application logs contained no `Traceback` or `CRITICAL` entries. Wayland
retained Mesa device-enumeration warnings and used software rendering; no
warning-free or hardware-accelerated Wayland result is claimed.

An additional normal-GtkGLArea NVIDIA Xwayland benchmark at `8d54a00` measured
one million resident Gaussians at 1280×720: orbit median/P95 completed work
6.826/7.673 ms and prepared playback 7.014/7.079 ms, after one warm-up and across
11 measured frames. Completion uses `glFinish`; source reads, first upload and
desktop presentation FPS are excluded. [PARITY.md](PARITY.md) records the
100,000-Gaussian cases, CPU submission and exact-pick timing separately from
EGL source streaming.

## Later working-tree EIS evidence: partial

The current source includes the later corrections for FOV
skew/boundary handling, viewport 1/3/7 shortcuts, commit-to-pause transport,
constant-camera viewing versus strict key edits, ownerless ambiguous-ID
rejection, typed session times/workspace counts and exact plain-text attribute
copying. Source and Debian About identity now use the canonical `Info.plist`.
The 238-test result and 31-case actual-window runs above cover these assertions,
including literal attribute copying, foreign-key guards and workspace counts.
The real-window test harness still substitutes chooser completion.

Actual EIS-driven runs have since passed **five parity groups, three comparison
groups and three lifecycle groups**. The final preferences check stopped because
the test tool treated an AdwComboRow named “Language” as its selected value.
That tool issue has been corrected. A complete current-source `--full` passing
report is still pending. Earlier EIS sessions used evolving product/runner
revisions rather than the clean historical `599ca1c` source. A final clean-source rerun must
record its own commit, artifacts and cleanup before these partial results can
be described as full acceptance.

## Historical basic native acceptance on 2026-11-16

| Application protocol | Actual OpenGL implementation | Result |
| --- | --- | --- |
| Native GNOME Wayland at `599ca1c` | Mesa 25.2.8, llvmpipe, OpenGL 4.5 | The recorded basic native file opening, playback, timeline entry, camera editing/import/export, workspace export and calibrated comparison passed. |
| GNOME Xwayland with NVIDIA PRIME at `599ca1c` | NVIDIA GeForce RTX 4090, driver 620.70.06, OpenGL 4.6 | The same basic workflow passed, including real XTEST middle-drag input verified by different exported camera poses. |

Both historical runs and the final real-window regressions used Ubuntu's
PyOpenGL **3.1.7**, Python **3.12.3**, NumPy **1.26.4**,
GTK **4.14.5** and libadwaita **1.5.0**. For the historical basic runs, the
session had no connected monitor.
A temporary **1920 × 1080 Mutter virtual output** supplied a real native
compositor output and keyboard focus. The runner discarded PipeWire video into
a `fakesink`, stopped the session afterwards, and verified the logical-output
count returned from one to zero. No physical-monitor or manual Wayland mouse
acceptance is claimed; Wayland's strict mapped-pointer check is a separate
`--test-orbit` option.

The historical basic workflow verified:

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

### Historical clean-source basic acceptance

Both saved basic runs tested commit
`599ca1c0be9ddeec372795bbbdd14ae499294005` with an empty working-tree change
list. The unmodified machine-readable reports are retained in
[validation/2026-11-16/wayland.json](validation/2026-11-16/wayland.json) and
[validation/2026-11-16/xwayland.json](validation/2026-11-16/xwayland.json).
Wayland completed all seven workflow steps; Xwayland completed those steps plus
the pointer-input check. Both reports confirm source integrity, byte-identical
camera export/import/export, successful PNG export and zero remaining logical
outputs. Both owned application processes were verified to have exited.

The original reports, application logs, GSV exports and PNG images are also
retained in the verification workspace under
`viewer/dist/desktop-validation/{wayland,xwayland}/`. The common synthetic source
has SHA256
`5a70cc062e17e5676e4103299ec11d967023c964af436fe689b9911af34aba89`;
the calibrated comparison PNG from both protocols has SHA256
`33e90a717dee420ba30d10b2b2acd99f88c43535e19f2d9d74e0ff0055680378`.

## Reproduce

Run from a GNOME session with the application's distribution dependencies and
AT-SPI bindings installed. The current runner defaults to Mutter's EIS/libei
input path and needs `libei.so.1`; `--input-backend legacy` selects the older
RemoteDesktop notification path. The optional virtual-output path also needs
GStreamer GI bindings and its PipeWire plugin. The historical Xwayland pointer
check used `libXtst`; it does not establish current EIS delivery.
Use a new output directory for each run and avoid concurrent desktop interaction.
These commands attempt the runner's current workflow; the historical results
above do not imply that the expanded current `--full` workflow has passed.

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

Use `--full` to request the expanded inspector, transport, camera-field, preview,
export and workbench checks. EIS/absolute-pointer region mapping and native
portal focus must be established for the actual session. An incomplete or
failed run remains partial evidence even when some earlier steps succeed;
neither mapped unit-test controller callbacks nor mocked chooser completions
can substitute for that missing native-input acceptance.
