# Ubuntu and macOS behavior contract

This inventory describes the Ubuntu implementation and its verification boundary.
It is not a claim of pixel-identical rendering or hardware-independent performance.
Both frontends consume the normative [GSV specification](../../docs/gsv/README.md)
and [camera/render contract](../../docs/gsv/RENDER_CONTRACT.md).

## Shared data and editing behavior

| Area | Ubuntu implementation | Verification / boundary |
| --- | --- | --- |
| Container integrity | Existing `gsv.Scene` validates envelope, manifest digest, buffer digests and semantic array contracts. Source files are read-only. Unknown vendor objects remain opaque. | SDK container suite; model source-mutation detection test. No metadata entrypoint or deformation recipe is executed. |
| Representation selection | Explicit render-view field order takes precedence. Otherwise one named space/clock cohort is selected. A linked persistent display cache does not render simultaneously with its native source. | Model domain and cache tests. The outliner retains hidden native peers. An unsupported bound evaluator cannot silently substitute another representation. |
| Gaussian fields | Static, persistent-identity, spacetime-polynomial, covariance4D and SE3-basis fields use the SDK's safe numerical evaluators. | SDK evaluator suites and Linux static/persistent tests. Canonical/scaffold deformation recipes require their declared external evaluator or an explicitly linked evaluated display cache. |
| Persistent interpolation | Linear position, scale, alpha and color; shortest-arc quaternion SLERP. An identity is active between samples only if both bounding states are valid. | Linux midpoint, identity and visibility-gap tests. This matches macOS browsing policy; the SDK reference evaluator's nearest-sample policy is unchanged. |
| Identity and selection | Signed 64-bit IDs remain integers; IDs are local to the owning object. Object and element selection are separate from the viewing camera. | Linux tests retain IDs above 2^60. Picking and exact-ID lookup use `(object, identity)` pairs. |
| Native time | Float64 source times and declared units are retained. No capture rate is inferred from sample spacing. | Extreme-clock and native-timestamp tests. Preview playback rate is a user setting, not capture metadata. |
| Trajectories | Native source samples, deterministic nested density subsets and separate paths across validity gaps. Individually selected IDs survive a reduced field density. | Linux gap/density/cancellation tests. The FNV1a / SplitMix ordering matches macOS. A memory ceiling produces an explicit error rather than silently truncating identities or samples. |
| Scaffold graphs | Source positions become display glyphs; visibility/validity suppress glyphs and incident topology edges. Certainty remains independent evidence. | Linux graph/gap test. Glyph dimensions and colors are display settings rather than training/source attributes. |
| Camera browsing | Intrinsics/translation interpolate linearly; orientation uses shortest-arc SLERP. Native raster, skew, pixel centers and named rig views are preserved. | Linux camera tests; SDK camera fixture suite. Distorted cameras remain inspectable but cannot become calibrated previews. |
| Camera authoring | Separate camera-only GSV files, explicit volume, `role=user`, fixed image raster, undo/redo, retiming and source-manifest SHA256 linkage. | Linux roundtrip, descriptor/digest, duplicate-ID, retiming and symlink/hardlink source-protection tests. The source reconstruction is never repacked. |
| Camera interoperability | Same nested pinhole schema, source-document metadata and interpolation declaration as `GSVCameraWriter.swift`. Imported integer-centered K is shifted once into the half-centered authoring raster. | A schema-equivalent macOS-writer fixture is tested. A macOS application roundtrip still requires macOS hardware; opening camera-only files remains read-only as in the existing macOS loader. |

## Interaction inventory and automated evidence

The controls retain the macOS operation model while using GTK widgets and Linux
Control-based accelerators. The evidence below comes from mapped GTK windows
under **Xvfb**, with Mesa software rendering and Ubuntu's PyOpenGL 3.1.7. It
establishes controller behavior, ownership and pixel preservation; it is separate
from physical keyboard/touchpad, screen-reader and native file-portal acceptance
on a user's actual GNOME desktop.

| User operation | Ubuntu behavior | Acceptance evidence |
| --- | --- | --- |
| Open, cancel and replace scenes | A canceled or superseded load cannot replace the current document; obsolete descriptors close. A failed open keeps the previous scene. | `test_application.py` covers canceled load disposal, source replacement, a damaged file and close during capture. Cancellation is passed into the real document constructor. |
| Select an object, camera or identity | Exact signed IDs remain bound to their owning object. Selecting source geometry or a source camera clears the previous authored-camera editing target. A native member selector changes the primary inspection item while retaining mixed membership. | Tests select through the Outliner, viewport callback and camera menu, then confirm Delete cannot remove the previously selected user camera. A two-object fixture sharing ID 7 checks unchanged membership, owner-labeled choices, primary-first inspector properties and the correct owning object after each primary change. |
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
The default center near clip is 0.001 scene units, exclusive; it is editable.
The studio background is RGB `(0.018, 0.025, 0.04)`. Background, opacity, size,
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
