# Ubuntu and macOS behavior contract

This inventory describes the Ubuntu implementation and its verification boundary.
It is not a claim of pixel-identical rendering or hardware-independent performance.
Both frontends consume the normative [GSV specification](../../docs/gsv/README.md)
and [camera/render contract](../../docs/gsv/RENDER_CONTRACT.md).

The recorded full Ubuntu regression ran **238 tests: 236 passed and two
benchmark tests skipped**. The Python SDK suite passed **78 tests**. Actual GNOME
window regressions separately passed **31/31** application tests on NVIDIA
Xwayland and **31/31** on software Wayland at source `8d54a00`, with chooser completion substituted.
Those working-tree runs and the earlier clean `599ca1c` basic native-portal
acceptance have different scopes. The expanded current-source `--full` desktop
workflow is **not yet accepted**; see [DESKTOP_VALIDATION.md](DESKTOP_VALIDATION.md).
Subsequent working-tree EIS evidence covers five parity groups, three comparison
groups and three lifecycle groups. A preferences check stopped on a runner
ComboRow-name/value mismatch; its correction awaits the final clean-source run.
These partial results do not upgrade the expanded workflow to accepted.

## Shared data and editing behavior

| Area | Ubuntu implementation | Verification / boundary |
| --- | --- | --- |
| Container integrity | Python SDK envelope/digest/semantic validation, with frontend-owned bounded `preadv` snapshots. A pinned regular-file descriptor rejects FIFO inputs and avoids live-mapping SIGBUS after truncation. Sources remain read-only; unknown vendor objects remain opaque. | SDK suite; model tests isolate source truncation in a subprocess and cover truncation during a read, source-mutation cache hits, cancellation and descriptor cleanup. No metadata entrypoint or deformation recipe is executed. |
| Representation selection | Explicit render-view field order takes precedence. Otherwise one named space/clock cohort is selected. A linked persistent display cache does not render simultaneously with its native source. | Model domain and cache tests. The outliner retains hidden native peers. An unsupported bound evaluator cannot silently substitute another representation. |
| Gaussian fields | Static, persistent-identity, spacetime-polynomial, covariance4D and SE3-basis fields use the SDK's safe numerical evaluators. | SDK evaluator suites and Linux static/persistent tests. Canonical/scaffold deformation recipes require their declared external evaluator or an explicitly linked evaluated display cache. |
| Persistent interpolation | Linear position, scale, alpha and color; shortest-arc quaternion SLERP. An identity is active between samples only if both bounding states are valid. | Linux midpoint, identity and visibility-gap tests. This matches macOS browsing policy; the SDK reference evaluator's nearest-sample policy is unchanged. |
| Identity and selection | Signed 64-bit IDs remain integers and local to their owner. Object/element/camera-view selection is independent of the View camera dropdown. That dropdown changes only the viewed camera; Outliner selection determines the authored editing target. | Owner-qualified ID, rig-view inspection and view-preserves-selection tests. A selected authored track can record the currently viewed source camera. Foreign-domain viewing is rejected without altering the previous view. |
| Native time | Float64 source times and declared units are retained. No capture rate is inferred from sample spacing. | Extreme-clock and native-timestamp tests. Preview playback rate is a user setting, not capture metadata. |
| Trajectories | Native source samples, deterministic nested density subsets and separate paths across validity gaps. Individually selected IDs survive a reduced field density. | Linux gap/density/cancellation tests. The FNV1a / SplitMix ordering matches macOS. A memory ceiling produces an explicit error rather than silently truncating identities or samples. |
| Scaffold graphs | Source positions become display glyphs; visibility/validity suppress glyphs and incident topology edges. Certainty remains independent evidence. | Linux graph/gap test. Glyph dimensions and colors are display settings rather than training/source attributes. |
| Camera browsing | Intrinsics/translation interpolate linearly; orientation uses shortest-arc SLERP. Native raster, skew, pixel centers and named rig views are preserved. | Linux camera tests; SDK camera fixture suite. Distorted cameras remain inspectable but cannot become calibrated previews. |
| Camera authoring | Separate camera-only GSV files, explicit volume, `role=user`, fixed image raster, undo/redo, retiming and source-manifest SHA256 linkage. | Linux roundtrip, descriptor/digest, duplicate-ID, retiming and symlink/hardlink source-protection tests. The source reconstruction is never repacked. |
| Camera interoperability | Same nested pinhole schema, source-document metadata and interpolation declaration as `GSVCameraWriter.swift`. Imported integer-centered K is shifted once into the half-centered authoring raster. | A schema-equivalent macOS-writer fixture is tested. A macOS application roundtrip still requires macOS hardware; opening camera-only files remains read-only as in the existing macOS loader. |
| Large authored animations | Cached read-only Float64 key times make steady seek logarithmic in key count. Inspector rows update in place; full key selectors/descriptors rebuild on track/list changes. Imports validate each key and publish one ordered list, with optional cancellation every 128 keys. | 100,000-key seek/cache and Inspector tests; 10,000-key import plus invalid-middle-key, cancellation, read-only matrices and unchanged source hashes. Measured import growth is linear; per-key validation remains a worker cost. |

## Interaction inventory and automated evidence

The controls retain the macOS operation model while using GTK widgets and Linux
Control-based accelerators. The operation assertions below run in mapped GTK
windows under **Xvfb** with Mesa and Ubuntu's PyOpenGL 3.1.7; the application
subset additionally has the actual GNOME window evidence recorded above.
They establish controller behavior, ownership and pixel preservation while
substituting chooser completion. Physical keyboard/touchpad, screen-reader and
native file-portal acceptance remain a separate evidence category.

| User operation | Ubuntu behavior | Acceptance evidence |
| --- | --- | --- |
| Open, cancel and replace scenes | A canceled or superseded load cannot replace the current document; obsolete descriptors close. A failed open keeps the previous scene. | `test_application.py` covers canceled load disposal, source replacement, a damaged file and close during capture. Cancellation is passed into the real document constructor. |
| Select an object, camera or identity | Exact signed IDs remain owner-qualified. Selecting source geometry/cameras in the Outliner clears the authored editing target; changing the **View camera dropdown does not change selection or that target**. A native member selector changes primary inspection without changing mixed membership. | Outliner, viewport and member-selector tests distinguish selection from viewing. `test_camera_view_preserves_selection_and_rejects_a_foreign_domain` also checks recording the displayed source view into a selected authored track. Duplicate ID 7 retains owner-qualified membership and primary-first properties. |
| Author and import cameras | Create, record, rename, key transform/FOV, retime, delete, undo/redo, save and import use separate user camera tracks. Imported tracks become visible and selected. Source-domain guards prevent foreign-space/clock transform edits. | Camera model tests plus UI create/record/retime/undo/redo/export/import workflow. Retiming seeks the moved key; undoing creation removes orphan selection. The UI file chooser's completion is substituted with a chosen local path in these automated tests. |
| Scrub an authored camera | XYZ, quaternion and FOV refresh at committed time. A focused text field retains an unfinished edit. Camera paths update when keys or poses change. | `test_application.py` checks interpolated XYZ/FOV, focused text preservation, path endpoints after edits and the retimed playhead. |
| Play a static source with authored animation | Transport uses the active timeline including authored keys, rather than only the source sample count. Sample stepping reaches the authored duration. | A static Gaussian fixture with one source time and two user keys supports playback and stepping across both keys. |
| Undo text or camera changes | Standard Undo/Redo acts on focused GTK text; dedicated camera history remains available through its own commands. Home remains a local viewport/timeline key rather than a global text-field accelerator. | A native `Gtk.Text` user insertion is undone/redone without changing camera history. Dedicated camera undo then removes the created track and stale selection. |
| Inspect calibrated captures | Entering comparison from a fractional playhead chooses an explicit observation row. Changing a render view activates its named space/clock and exact capture time. The compact capture selector exposes observation rows. | A two-domain macOS fixture verifies capture times 0/1 and 10/20, matching field visibility, clock and returned comparison. No nearest image is used to compute scores. |
| Edit the timeline | Scroll and pinch preserve time under the pointer; Alt temporarily bypasses snapping. Escape cancels a key drag. Left/Right step native source times, Home fits and Space invokes playback. Preview range shading follows native times. | Seven tests in `test_inspection_controls.py` use actual GTK allocations and controller callbacks with explicit gesture/modifier state; one compares Cairo pixels inside/outside the preview interval. |
| Recover from annotation capacity limits | An explicit trajectory preview capacity error reports its reason while the complete scene geometry continues to seek and play. Capacity failure does not truncate source identities or invent incomplete paths. | A UI regression injects `TrajectoryLimitError`, seeks to a new time, checks all original IDs and the reported reason, then confirms playback continues. Other source/decoder failures retain their ordinary error path. |
| Inspect the image pair | Plain scroll pans; Alt-scroll and pinch zoom around the pointer in both panes. Fit and actual-pixel controls preserve original image pixels. | Tests check source coordinates under both pane anchors, cumulative pinch scale, one image pixel per display backing pixel, unchanged raw RGB and full-pair export. Physical multi-touch delivery remains a desktop acceptance item. |
| Export screenshots | Viewport PNG follows preview resolution. Workspace PNG includes the workbench. Comparison export retains both full native rasters regardless of inspection pan/zoom. | UI PNG tests decode exported images, compare the full pair exactly and retain source SHA256 and dirty camera state. Snapshot tests additionally cover atomic replacement, Unicode paths, failure preservation and symlink rejection. |
| Complete an accepted file operation | Chosen writes execute FIFO across source replacement. Closing drains accepted commands without blocking GTK or requiring a later UI callback. Preview reads retain latest-request coalescing. | `test_jobs.py` writes three chosen files around invalidation and proves all complete in order; it also checks close during queued work, stale-result disposal and continuous preview publication. |
| Inspect hierarchy and source values | Native GTK4 TreeListModel/TreeExpander/MultiSelection/ListView, directly inside its ScrolledWindow. Seven ordered categories show counts; only multi-view rigs have view children. Source and inactive values remain inspectable without changing domain. | `test_outliner.py` checks exact view membership, nonselectable groups, search/collapse restoration and bounded row binding for 2,000 objects. Inspector tests cover document coordinates/clocks/storage, inactive attributes and scaffold glyph disclosure. |
| Preserve native operation details | Viewport 1/3/7 presets; rate/range commits pause; exact Time extends session extents; FOV scales skew and focal lengths at inclusive 1°–170°; same-space one-key cameras can be viewed across clocks while editing/key-time actions remain strict. | Focused application regressions also reject ownerless ambiguous IDs, preserve literal copied attributes and check workspace object/sample counts without modifying the source manifest. |

Document object/sample rows and header counts include authored cameras and
session timeline entries, as in the native workspace. Source descriptor JSON
remains unchanged. Clipboard export uses cached plain property strings, avoiding
Adwaita-version-dependent markup getter decoding. Source and Debian About
metadata now read the same canonical `Info.plist` author/product identity;
four packaging assertions passed for the installed metadata/resource layout.

The macOS comparison pane, timeline and authoring code were audited against these
operations before the regression cases were added. These tests do not establish
an application-to-application macOS camera exchange: that still requires running
both applications on their respective systems. Nor do they establish identical
window decoration, font metrics or source-renderer pixel parity.

## Desktop and rendering

Ubuntu uses GTK 4, libadwaita and `Gtk.GLArea`, with native file dialogs and drag
and drop, system theme integration, desktop/MIME registration and GTK input
controllers. The workbench keeps the macOS arrangement: outliner, viewport,
inspector, preview controls and native-time timeline. Linux uses Control-based
desktop accelerators; the corresponding macOS commands use Command.

The interactive renderer uses OpenGL instanced anisotropic splats, stable global
depth ordering and premultiplied compositing. OpenGL 4.3 hardware uses an eight-pass
stable GPU radix sort; OpenGL 3.3 and software drivers retain stable NumPy sorting.
Covariance projection and footprint evaluation run in shaders. Lower
preview resolution changes raster work without removing source identities.
Line overlays are display annotations, separate from quantitative RGB.

### Interactive preview policy

The preview follows the macOS default browsing approximation. Its Jacobian is
clamped to the skew-aware image bounds expanded by a factor of 1.3; the projected
center itself is not clamped. Covariance dilation is 0.3 framebuffer pixels².
Ellipses use a radial 3σ cutoff, with activated opacity clamped to `[0, .999]`
before Gaussian evaluation and an alpha rejection threshold of `1/255`.
Gaussian size scales covariance by the square of the display multiplier.
The desktop center near clip defaults to 0.01 scene units, exclusive; it is editable
within `[0.0001, 100]`, matching the effective macOS workspace options. The lower-level
renderer default is separate from these explicitly applied desktop options.
The studio background is RGB `(0.055, 0.061, 0.071)`. Background, opacity, size,
exposure and orbit-center display settings affect only the interactive preview.
Integer-centered camera K is shifted once into the half-centered framebuffer;
letterboxing, skew and HiDPI scaling preserve the source calibration. Picking
uses the actual resolution-scaled framebuffer covariance and alpha footprint.

This preview does **not** execute arbitrary stored profile policies for pixel
offset, tile support, camera view volume, antialias compensation or early
transmittance termination. Use calibrated capture for the declared render-view
profile. Scores never originate from the interactive preview.

Gaussian composition uses stable descending center depth and premultiplied
back-to-front blending. Both sorting backends compute the same explicitly
rounded Float32 world-depth keys, omit the common camera translation and retain
source order for exact ties (including signed zero). The compute implementation
uses four-bit histogram/prefix/scatter passes with portable shared atomic bit
masks and popcount; no subgroup-size or vendor ballot extension is required.
GPU order is checked against CPU order across one million identities, mixed
weights, cancellation-prone Float32 arithmetic, partial workgroups and ties.
A rendered fixture also checks byte-identical output after switching order
backends. Compute-capability, allocation or execution failure rebuilds the exact
frame using the CPU fallback and records the backend/reason in renderer stats.
Software GL deliberately selects the CPU implementation. Hardware automatically
uses compute for scenes of at least 16,384 primitives. Translation-only pan and
dolly, or selection changes, reuse the previous exact order rather than sort.

Static/persistent cohorts retain two immutable source endpoint buffers (64 bytes
per primitive per endpoint) across interpolated display times. Position, scale,
opacity and RGB interpolate in the vertex shader; rotation uses shortest-arc
quaternion SLERP before forming covariance. Scalar time weights require only a
uniform update; mixed cohorts upload per-row time weights and exact endpoint vs
interior validity policy. A truly interior Float64 time still requires both
source samples valid when its interpolation fraction rounds to 0 or 1 in
Float32. Source identities are never removed from this resident representation.
Analytical encodings and scaffold glyphs retain the explicit SDK/CPU frame path.
Calibrated scientific capture retains its independent exact-time evaluator.

GPU sorting reads the interpolated resident means and writes source indices
without a CPU readback or index upload. The GL 3.3 fallback interpolates only
positions for sorting, without materializing all covariances on GTK. Endpoint
changes still require source I/O, preparation and GPU uploads; first upload,
interval streaming and steady interpolation are different performance cases.
The shader projects each of the six quad vertices. Buffer-texture and compute
limits are checked; capacity failure is explicit. No primitive is silently
removed to meet a frame-rate target. GPU geometry, camera matrices and footprint
arithmetic use Float32, so cross-backend pixel equivalence and extreme-coordinate
precision require measured fixtures rather than inference from format support.

Prepared-frame picking reuses the same vertex projection and footprint rules
through baseline OpenGL 3.3 transform feedback. Each primitive returns Float32
center-depth bits and its integer source index; minimum depth then source index
implements the macOS nearest/tie policy without normalized depth-buffer rounding
or a full CPU covariance evaluation. A pick reads eight bytes per primitive;
it does not promise constant work for arbitrarily large scenes. A single chosen
identity is then evaluated through its exact `(object, signed ID)` lookup.
The CPU/GPU-interpolated rendering test agrees within one output byte, including
rotation, nonuniform scale, multiple time cohorts and source validity gaps.

### Performance reporting and frustum-visible counts

GPU time is obtained through bounded asynchronous elapsed/timestamp queries;
results are read only after availability. The completed-render count/rate records
GPU completion, not compositor presentation or monitor refresh. CPU submission
and estimated GPU storage are reported separately. Instrumentation failures
retain rendering and expose unavailable values rather than synthetic timings.

`gpu_visibility.GpuVisibility` compiles the **same vertex projection** with a
one-point geometry filter. `GL_PRIMITIVES_GENERATED`, available on baseline
OpenGL 3.3, counts one eligible Gaussian: valid interpolation state, activated
opacity at least `1/255`, center beyond the exclusive near clip, finite
projection and 3σ bounding box intersecting the raster. Occlusion by another
Gaussian does not remove it from this frustum count. No million-row CPU scan or
per-primitive result readback is introduced. The query pool is bounded to three
objects and production samples are submitted at most 10 Hz.

The label uses the latest completed GPU sample, with sample age/source time in
the tooltip. Camera/time changes retain that last sample while a later one is
pending; changing the source identity layout or visible owner cohort starts a
new epoch and rejects old results. CPU fallback frames carry their original
cohort identity count separately from the currently active raster records, so
the denominator includes inactive source IDs. Empty geometry is known exactly
as zero without a GPU sample. Unsupported or failed nonempty queries report
“—”, never active-count substitution. GL 3.3 tests cover unavailable-query reads,
epochs, empty/all-inactive data, mixed time weights and failure recovery.

A separate 2026-11-16 headless NVIDIA EGL-device run used RTX 4090, driver
620.70.06, OpenGL 4.3, 1,000,000 resident dynamic Gaussians and a 1280×720 raster.
Forcing a sample every frame changed median GPU render time from 3.070 to
4.522 ms (about **1.45 ms per visibility sample**) and median completed work
from 8.644 to 9.552 ms, including sorting. The production 10 Hz case issued one
visibility query across 12 measured frames. Its 8.324 ms median is within this
shared-machine run's variation and is not evidence that counting speeds up
rendering. These are offscreen GPU measurements, not native-input or desktop
presentation acceptance; earlier renderer tables below predate this extra pass.

GTK reuses the completed GL texture during unrelated parent redraws. Explicit
scene/view updates and viewport resizing request rendering; a paused static
scene does not repeatedly project, sort and draw because a label changes.
Pointer-anchored wheel/pinch dolly uses the existing pivot plane (including skew
and off-center K), trackball composes camera-local yaw/pitch, and roll preserves
the optical center. Escape/focus loss stops an active gesture at its current
pose, retaining the native navigation history boundary.

Overlay occlusion uses a Gaussian center-depth surface wherever footprint alpha
is at least 0.1. Opaque trails write actual line depth. Translucent paths use the
same weighted blended transparency formula as the Metal renderer:
`weight = clamp(1 / (1 + .03 * camera_depth²), .01, 1)`. Color/alpha accumulation
uses RGBA32F, and revealage uses R16F; a final composite combines weighted color
with total coverage. Two draw passes provide the independently blended targets
on baseline OpenGL 3.3. Crossing-path color is independent of segment order,
within floating-point rounding; it remains an order-independent transparency
approximation rather than an exact sorted-volume integral. The real GL test
reverses red/blue crossing paths and checks the image within one output byte.
Grid, camera wireframes and scaffold edges remain independent display overlays.

The quantitative renderer is a separate cancellable NumPy implementation of the
declared EWA profile. It retains explicit pixel-center, support, alpha, depth,
background and camera-affine policies. Render/reference metrics use the shared
SDK metric implementation, exact image/geometry/calibration times and explicit
observation membership. Scores are never calculated from an interpolated scene,
display screenshot, navigation exposure or scaled preview. Shutter integration
and distorted camera models remain inspection-only, matching macOS capability
limits. Transformed output can be previewed but cannot acquire a native-raster
reference score. The image limit is 16 megapixels and 16,384 pixels per axis.

## Validation and remaining hardware acceptance

`tests/test_model.py` covers scene ownership, interpolation, domain switching,
identity, paths, graph topology and camera authoring. `tests/test_comparison.py`
covers analytical EWA projections/compositing and shared macOS contract fixtures.
The existing SDK tests remain the authority for container and evaluator math.
GPU and desktop smoke checks should run through the Ubuntu test launcher under
a real desktop or Xvfb as described in the Ubuntu guide.

Software-rendered X11 checks establish startup and rendering behavior; they do
not establish a production GPU performance claim. Release acceptance must also
cover Ubuntu Wayland and X11 on Intel, AMD and NVIDIA hardware, high-DPI scaling,
touchpad/gesture input, accessibility traversal, large real scenes and measured
frame latency. Metal and OpenGL use different approximation and numerical paths;
the supported render contract and source data are shared, while cross-backend
pixel equivalence requires measured fixtures on both systems.

### Authored-camera cache and import measurements

The 100,000-key synthetic track retained every native time. Initial time-index
construction plus seek took 6.14 ms; warm seek median/P95 were 0.413/0.431 ms.
The former full-array construction alone measured 5.75 ms per seek. Inspector
initial descriptor/widget construction took 90.26 ms, while warm value updates
were 0.236/0.254 ms median/P95. Unchanged rows, raw JSON and key-selector entries
are retained; editing replaces the key list and invalidates the relevant cache.

Direct SDK array fixtures measured **1.720 seconds for 10,000 keys** and
**17.268 seconds for 100,000 keys**, a 10.042× time increase for 10× the input.
Imports still run the existing finite/pinhole/raster/affine calibration checks
for every key, preserve near/far, convert integer pixel centers once, and make
K/pose arrays read-only. They build and assign one validated list, rather than
calling `record()` to repeatedly scan/sort every prefix. Optional cancellation
checks occur between 128-key chunks; both input file hashes were unchanged.
These CPU measurements explain large-file loading latency, not playback FPS.
The fixtures and reproduction command are in [FEATURE_AUDIT.md](FEATURE_AUDIT.md).

### Recorded software-renderer benchmark

The reproducible `tools/benchmark_viewport.py` run used Xvfb with
`GDK_BACKEND=x11`, `GDK_DEBUG=no-vsync`, and `LIBGL_ALWAYS_SOFTWARE=1`, at
640×480 framebuffer pixels. Renderer: llvmpipe LLVM 20.1.2 (256 bits), OpenGL
4.5 Core, Mesa 25.2.8. Three measured orbit frames followed one first-upload
frame; grid, paths and cameras were disabled. The completed time includes an
explicit `glFinish`, rather than treating command submission as GPU completion.

| Splats | First upload + completed draw | Median orbit submission | Median completed orbit |
| ---: | ---: | ---: | ---: |
| 100,000 | 239.8 ms | 163.1 ms | 163.2 ms |
| 1,000,000 | 1814.8 ms | 1759.5 ms | 1760.1 ms |

These measurements demonstrate a software-renderer scaling boundary. They do
not establish smooth playback for large scenes or predict Intel/AMD/NVIDIA GPU
latency. Reducing raster resolution changes fragment cost; CPU scene evaluation
and depth sorting still scale with the full primitive count. Capture-worker
latency and translucent-path rendering require separate scene-specific timings.

### Recorded NVIDIA desktop-context benchmark

A native GNOME Xwayland context reported **NVIDIA Corporation / NVIDIA GeForce
RTX 4090/PCIe/SSE2**, OpenGL 4.6.0 NVIDIA 620.70.06, `GdkX11GLContextGLX`.
Per-process PRIME offload selected this GPU; no system driver or desktop setting
was changed. The inherited Wayland session instead created a software llvmpipe
context: physical NVIDIA device presence alone is not evidence of acceleration.
The Viewport graphics diagnostics expose the actual context vendor/renderer,
version and display backend. EGL and GLX contexts are supported in one frontend.

`tools/benchmark_viewport.py --width 1280 --height 720 --iterations 10` measures
renderer command submission and completed work using `glFinish`. Ten orbit
frames followed first upload; grid, trajectories and cameras were disabled.
The CPU comparison used the same synthetic scene and stable key policy.

| 1280×720 work | Count | Median completed | P95 completed |
| --- | ---: | ---: | ---: |
| CPU-sort orbit | 1,000,000 | 145.9 ms | 160.4 ms |
| GPU-sort orbit | 100,000 | 3.67 ms | 8.08 ms |
| GPU-sort orbit | 1,000,000 | 10.58 ms | 34.32 ms |
| Resident endpoint playback | 100,000 | 1.69 ms | 3.26 ms |
| Resident endpoint playback | 1,000,000 | 10.66 ms | 13.96 ms |
| Prepared-frame exact pick (five picks) | 100,000 | 2.17 ms | 3.86 ms |
| Prepared-frame exact pick (five picks) | 1,000,000 | 12.25 ms | 14.29 ms |

The playback run uses `--operation playback --pick`: positions, anisotropic
scales and quaternion rotations change within one resident source interval.
The one-million-primitive first endpoint upload and completed draw took 210.4 ms;
its two resident endpoint buffers occupy 128 MB, plus order, selection, compute
scratch and framebuffer storage. These are reproducible renderer throughput
measurements on this shared desktop GPU, not full application presentation FPS,
physical touchpad latency, source-streaming throughput or a guarantee for other
scenes/drivers. Dense translucent trajectory costs and quantitative capture remain
separate workloads. The SDK-backed warm `prepare_frame` path separately measured
about 0.20 ms median for one million source identities; cold endpoint preparation
and source validation are not included in steady resident playback numbers.

### Latest actual GNOME resident-renderer measurement

At `8d54a00`, `benchmark_viewport.py` ran in a normal GtkGLArea on the existing
GNOME display, using NVIDIA PRIME Xwayland, RTX 4090, OpenGL 4.6 and
`GdkX11GLContextGLX`. Each case used a 1280×720 raster, one warm-up and 11
measured resident frames. Completed work includes an explicit `glFinish`.

| Operation | Gaussians | Median completed | P95 completed | Median CPU submission |
| --- | ---: | ---: | ---: | ---: |
| Orbit | 100,000 | 1.354 ms | 2.148 ms | 0.798 ms |
| Prepared playback | 100,000 | 1.147 ms | 4.125 ms | 0.835 ms |
| Orbit | 1,000,000 | 6.826 ms | 7.673 ms | 0.771 ms |
| Prepared playback | 1,000,000 | 7.014 ms | 7.079 ms | 0.778 ms |

The million-Gaussian exact pick median was 7.091 ms in the orbit run. JSON/logs
remain at `/tmp/gsv-real-viewport-{orbit,playback}-release.{json,log}` in the
verification workspace. These are resident renderer completion measurements,
excluding source reading and first upload; they are not compositor presentation
FPS or physical-input latency. They must not be mixed with the different
EGL-device source-interval streaming workload below. No persistent display
setting changed during the benchmark.

### Source-interval streaming boundary

`tools/benchmark_streaming.py --count 1000000 --intervals 4` creates a temporary
five-sample GSV and uses an NVIDIA EGL-device pbuffer (RTX 4090, OpenGL 4.3,
driver 620.70.06) at 1280×720. It opens no desktop window. Actual SDK preparation
is timed separately from GPU upload, sorting and completed draw. Buffer identity,
not a floating-point approximation or content sample, decides endpoint reuse.

| Work | Measured time / transfer |
| --- | --- |
| Source open and validation | 1401.9 ms |
| Cold preparation before shared owner-label allocation | 122.9 / 112.0 / 105.6 / 114.2 ms |
| Cold preparation with shared owner labels, four intervals | 45.6 / 35.8 / 35.1 / 35.5 ms |
| Later interval completed draw, upload both endpoints | 31.6 / 31.0 / 31.5 ms; 128 MB each |
| Later interval completed draw, rotate resident endpoint | 22.3 / 20.1 / 19.5 ms; 64 MB each |

When the next immutable first endpoint is the previous second endpoint, their
GPU buffers/textures/capacities exchange roles and only the new second endpoint
uploads. Backward seeking performs the corresponding inverse rotation. If
array identity does not match (including newly assembled mixed cohorts), the
ordinary two-endpoint upload remains correct. Regression tests check forward
and backward upload counts, zero uploads within a resident interval and images
against independently materialized CPU geometry within one output byte.

This reduces the measured interval transfer by half. Separately, owner-label
metadata arrays now share one immutable Python string per field, eliminating
one million duplicate string allocations; this reduced later cold preparation
from roughly 110 ms to roughly 35 ms in the fixture. That preparation still
precedes GPU work. First allocation also differs from
updating an existing allocation. Source intervals that change frequently can
therefore produce animation hitches despite a fast resident-frame renderer.
Those intervals are prepared off the GTK thread, with the prior committed frame
retained while work is pending; that preserves UI responsiveness, not a promise
of continuous full-rate source streaming. The implementation and evidence do
not justify an unconditional 60 FPS playback claim for large files.

The later safe-source-read audit replaces live source mappings with owned,
bounded `preadv` snapshots to prevent a concurrent source truncation from
crashing the process. With that implementation, the separate
`tools/benchmark_scene_io.py million` run measured 705.17 ms source validation,
54.83 ms first preparation and 0.193 / 0.202 ms median/P95 resident preparation
for one million identities. Peak process resident high-water memory was
224.66 MiB. These are warm OS page-cache measurements in a fresh process;
they do not measure cold-disk throughput. The earlier 35 ms cold-preparation
table predates this source-safety change. Sparse scaffold inspection and one
eight-sample trajectory read 24 and 96 source bytes respectively, with a position
blob larger than the 64 MiB snapshot budget. [FEATURE_AUDIT.md](FEATURE_AUDIT.md)
retains the full workload and reproduction details.

### Desktop release acceptance

Before publishing a hardware-qualified build, record the Ubuntu version, display
protocol, GPU, driver, scene counts and raster dimensions for these checks:

- Run startup, file-manager opening, drag/drop and native open/save dialogs on
  Ubuntu GNOME Wayland and X11 with Intel, AMD and NVIDIA drivers.
- Use 100%, 150% and 200% display scale; resize/move the window between monitors;
  verify source-camera letterboxing, exact identity picking and screenshots.
- Exercise pointer orbit/pan/dolly, Ctrl-click selection, touchpad scrolling,
  pinch/roll, keyboard-only traversal and screen-reader labels.
- Compare native sample stepping, trails, mixed object/identity selection,
  camera paths, camera authoring and undo/redo against the macOS workbench.
- Exchange camera animations between the macOS and Ubuntu applications, confirm
  source-digest matching and compare render-contract fixtures on both backends.
- Measure interactive frame latency and memory on representative large scenes;
  record first upload, playback and orbit separately, at each chosen resolution.
- Cancel captures and imports, replace documents while workers are active and
  close windows during I/O; confirm source hashes and application responsiveness.

NVIDIA Xwayland context rendering and automated controller workflows have the
evidence above. Intel/AMD coverage, cross-application macOS exchange and physical
input/accessibility acceptance remain separate release checks. Automated tests
and package validation do not substitute for those measurements.
