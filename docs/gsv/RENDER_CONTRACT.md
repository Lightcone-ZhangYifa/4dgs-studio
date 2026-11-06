# GSV camera, render and observation contract

This is the normative contract shared by the Python SDK, exporters and 4DGS Studio.
The binary container, named spaces/clocks and optional peer objects are independent
of these encodings. Empty containers are valid. Names in `data` below are literal.
An array reference means a string key in the manifest array table. All numeric
JSON values are finite. A JSON null upper limit denotes an unbounded interval.

## Camera: `gsv.camera.pinhole` (type `camera`)

```
data = {
  times: f64[T],
  view_ids: unique string[V],
  projection: {
    intrinsics: f64[T,V,3,3], image_sizes: i32[V,2],
    pixel_center: "integer" | "half",
    distortion: {model:"none"}
  },
  pose: {camera_to_world: f64[T,V,4,4]}
}
```

`T,V` are positive. Width precedes height. Camera coordinates are right/down/forward;
column vectors, a rigid c2w transform, positive forward depth. K is authoritative,
including skew and principal point. Pixel center applies to K and observations;
no renderer may silently apply another half-pixel conversion. View identifiers
are stable within this rig and independent of array position. `times` is on the
object clock. One sample is a static calibration, usable at every model time.
Multiple samples must be explicitly interpolated for browsing; exact observation
comparison always uses the observation's indexed calibration sample at its own
explicit image time. A static calibration need not duplicate matrices for video.

Optional projection `depth_mapping` is descriptive, never a visibility rule:
`{near: positive finite number, far: number|null, ndc_range:"zero-to-one"|"minus-one-to-one", direction:"forward"|"reversed"}`.
Far, when finite, exceeds near. These scalar values describe source projection
matrix construction. They do not constrain K rays or Gaussian support.

Optional `view_volume` is `{near:f64[T,V], far:f64[T,V], far_unbounded:bool[T,V]}`.
Near is nonnegative; finite far exceeds near; unbounded far slots store canonical
zero. This authored axial view volume is applied only when a render profile
explicitly requests it. It is not automatically a fragment clip operation.

Optional `optics` is a dictionary of f64[T,V] tracks: `focal_length_mm`,
`sensor_width_mm`, `sensor_height_mm`, `f_number`, `focus_distance` (space units).
Values are positive. These are descriptive physical capture facts; K remains
primary, and conflicting optics must not replace calibration.

Optional `acquisition` contains f64[T,V] tracks `exposure_seconds` (>0), `iso` (>0),
`shutter_open` / `shutter_close` (paired offsets in camera clock units, close >= open),
`rolling_readout` (nonnegative camera clock units), and optional
`rolling_direction:"top-to-bottom"|"bottom-to-top"|"left-to-right"|"right-to-left"`.
Nonzero readout requires a direction. Capture exposure is not a gain multiplier.
Instantaneous rendering does not simulate these facts.

Distortion can instead be an explicitly named model with `coefficients:f64[T,V,D]`:
`opencv-brown` (D=5; k1,k2,p1,p2,k3) or `opencv-fisheye` (D=4; k1..k4).
Both describe forward ideal-normalized to distorted-normalized coordinates using
the OpenCV equations. Additional mathematical models require new named contracts,
never inference from coefficient count. Unsupported distortion is inspectable;
Studio must refuse calibrated rendering/comparison rather than silently ignore it.

## Render profile: `gsv.render.profile` (type `render`)

A reusable algorithm description. Every field below is required, not inferred
from a paper name. Optional metadata/provenance explains the actual implementation.

```
data = {
 algorithm: "ewa-splat",
 visibility: {
   center_depth: {near: number, far: number|null,
                  near_inclusive: bool, far_inclusive: bool},
   camera_volume: "ignore" | "gaussian-center"
 },
 projection: {
   divide_epsilon: number,
   jacobian: {mode:"symmetric-fov"|"image-bounds"|"unclamped", factor:number},
   pixel_offset: [number,number]
 },
 filter: {covariance_dilation:number, radius_clip:number,
          antialias_compensation:bool},
 support: {bounds:"tile-major-radius"|"opacity-aware", sigma:number,
           tile_size:[integer,integer], mahalanobis_cutoff:number|null},
 alpha: {maximum:number, minimum:number},
 compositing: {
   direction:"front-to-back", depth_sort:"tile-stable"|"global-stable",
   tie_break:"source-order",
   transmittance:{threshold:number, inclusive:bool, stage:"before-contribution"}
 }
}
```

Depths/radius_clip/dilation/epsilon/threshold are nonnegative; far>near if finite;
0<=minimum<=maximum<=1, threshold<=1. sigma, jacobian factor and tile dimensions
are positive. Dilation is in pixel squared; sigma and Mahalanobis distance are
dimensionless. `pixel_offset` shifts the projected mean in the camera's stated
raster coordinates, after K projection; it does not change pixel sample locations.
Inria-style ndc2Pix can therefore explicitly contribute [-.5,-.5] when needed.
Exporters must account for source K derivation and apply this conversion once.
`symmetric-fov` clamps camera x/z and y/z to ±factor*W/(2fx) and
±factor*H/(2fy). `image-bounds` uses the off-center bounds
[−cx/fx−m_x, (W−cx)/fx+m_x] and [−cy/fy−m_y, (H−cy)/fy+m_y],
where m_x=(factor−1)*W/(2fx), m_y=(factor−1)*H/(2fy). Its factor is at
least1. These are derivative-only bounds in normalized camera coordinates,
not clipping of the projected mean. `unclamped` performs no such restriction.
Camera view-volume, if enabled, additionally tests Gaussian centers (near included,
finite far included); it never claims complete geometric/fragment clipping.
`mahalanobis_cutoff:null` imposes no explicit per-fragment support gate beyond
bounding and alpha rejection. Bounds, tile support and derivative clamp are
different operations. A stored profile can exceed a renderer's capabilities.

## Render view: `gsv.render.view` (type `render`)

```
links = {camera:"camera-id", fields:["gaussian-id", ...], profile:"profile-id",
         radiometry?:"appearance-id", observations?:["observation-id", ...]}
data = {
 camera_view: integer,
 output: {
   image_size:[width,height], camera_to_output:[[sx,0,tx],[0,sy,ty],[0,0,1]],
   color_space:"srgb"|"linear-srgb", background:[r,g,b], clamp:"unit"|"none"
 },
 temporal: {mode:"instantaneous"}
}
```

Fields are an explicit, nonempty ordered set. This prevents double-rendering a
native representation and its evaluated cache. All fields/camera use the same
space; timed fields use the camera clock. A truly static Gaussian field may omit
its clock. Other relations require a named binding that explicitly resolves them. Output
K is camera_to_output*K, sx,sy>0. Pixel center convention is inherited; transforms
must include the appropriate center offset when resampling. All directly RGB-bearing Gaussian codecs (persistent-identity, static,
spacetime-polynomial, covariance4d, se3-basis) require `data.color_space` as
`srgb` or `linear-srgb`. Render-view output and all participating directly RGB-bearing
fields must agree; no implicit transfer conversion occurs. Native deformation
parameter archives do not acquire invented RGB semantics. Background is in the
pre-affine output color domain. Order is field appearance evaluation, splat
composition with background, optional camera-affine, output clamp. Display view
transforms are not part of stored captured RGB or quantitative comparison.
Each view binds one physical rig view; any number of render views may be stored.
Static calibrations do not restrict a Gaussian field's time domain.

Optional temporal mode `integrated` stores `shutter_open`, `shutter_close` (finite
clock offsets), `samples` (positive integer), `sampling:"uniform-midpoint"`.
It is a renderer capability, not automatically implemented by a camera's capture
metadata. Studio inspects but refuses unsupported temporal integration.

## Optional camera RGB observations

`gsv.observation.camera-rgb` (type `image`) links to its camera and uses:

```
data = {
 samples:{times:f64[M], camera_samples:i64[M], view_indices:i32[M]},
 pixels:{images:[array refs], masks?:[bool[H,W]], color_space:"srgb"|"linear-srgb"},
 role:"ground_truth"|"reference"|"prediction"
}
```

Each image has a finite explicit time on the observation/camera clock. The pair
(time, view_index) is unique; ordering is unrestricted. For a camera with more
than one calibration sample, image time exactly equals its indexed camera time.
For a one-sample static calibration, image times are independent: repeated
calibration indices are expected for video, with no pose duplication required.
u8 RGB is divided by255; f32 RGB is finite in[0,1]. Each image uses its indexed
camera's full native raster. No implicit crop, resize, temporal nearest match or
color conversion is allowed. Store rectified/cropped images with the corresponding
separate calibrated camera, and record original asset hash and processing as
provenance. Camera-affine is a peer `gsv.appearance.camera-affine`; only render-view
links select it. Gaussian geometry does not own camera postprocessing.

Quantitative render/GT comparison requires explicit observation membership in a
render view, matching camera/view, identity camera_to_output, native resolution,
unit output clamp, matching pixel color domain, exact calibration sample, supported
projection and temporal semantics. A renderer may report approximate profile
agreement separately, but must expose its effective profile differences. Capture
keys include view/profile/appearance/camera/time/output and current scene revision;
obsolete captures must not publish scores. PSNR/SSIM measure these declared
conditions, not novel-view or physical trajectory truth.

## SDK surface and implementation rules

`add_camera(...)` retains convenient numerical arguments but writes only this
nested contract; add optional `view_ids`, `depth_mapping`, `view_volume`, `optics`,
`acquisition`, `distortion` keywords. Helpers convert numeric tracks to typed arrays.
`add_render_profile(out,id,*,visibility,projection,filter,support,alpha,compositing,
algorithm="ewa-splat",metadata=None,label=None)` writes and validates a profile.
`add_render_view(out,id,camera_id,field_ids,profile_id,*,camera_view=0,image_size=None,
camera_to_output=None,color_space="srgb",background=(0,0,0),clamp="unit",
temporal=None,radiometry_id=None,observation_ids=None,metadata=None,label=None)`
resolves omitted output raster/transform to its camera raster/identity and writes
all required values. `add_camera_rgb` adds optional `times=None`; absent helper input derives times
from indexed camera samples, but the stored times array is always explicit. It
writes nested samples/pixels. `add_camera_affine` retains its current numerical contract.

Readers require these structures. There are no flat camera/observation fallbacks,
no executable metadata defaults, and no paper-name-based rendering decisions.
`add_persistent_gaussians` accepts `color_space="srgb"` and always writes the
typed color domain. Analytic RGB writers declare the same field explicitly.
Semantic validators check all cross-object associations, required shapes, time,
space, masks, reference targets and numerical ranges. Unknown vendor objects stay
opaque and are never executed. Unsupported capabilities are explicit, not guessed.
