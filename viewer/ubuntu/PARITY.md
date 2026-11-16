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

## Desktop and rendering

Ubuntu uses GTK 4, libadwaita and `Gtk.GLArea`, with native file dialogs and drag
and drop, system theme integration, desktop/MIME registration and GTK input
controllers. The workbench keeps the macOS arrangement: outliner, viewport,
inspector, preview controls and native-time timeline. Linux uses Control-based
desktop accelerators; the corresponding macOS commands use Command.

The interactive renderer uses OpenGL instanced anisotropic splats, stable global
depth ordering and premultiplied compositing. NumPy performs the depth sort on
the CPU; covariance projection and footprint evaluation run in shaders. Lower
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
back-to-front blending. Equal CPU depths retain source order. The CPU computes
depth keys using NumPy and the Float64 camera, while Float32 geometry and GLSL
projection can round extremely close depths differently. Sorting is
`O(N log N)` on the CPU, rather than Metal's GPU radix sort. Geometry occupies
64 bytes per splat in a reusable buffer texture; navigation uploads only a
4-byte source-order index per splat. Projection runs in the vertex shader for
each of the six quad vertices. Buffer-texture limits are queried from the GPU;
an oversized scene produces an explicit error. No primitive is silently dropped
to meet a frame-rate target.

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

These manual hardware checks remain outstanding in this software-rendered
container. The automated acceptance tests and package validation are necessary
but do not substitute for those measurements.
