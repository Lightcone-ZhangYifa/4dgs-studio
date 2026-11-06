# GSV scene container

GSV is a self-contained, random-access container for reconstruction results. A
document contains zero or more independent objects. Gaussian fields are one
object type, alongside cameras, geometry, observations, motion models and
provenance. No Gaussian, camera, clock, or coordinate space is mandatory.

The envelope is deliberately independent of representation codecs. New methods
add namespaced object encodings and capability declarations without changing
the envelope. There is no document-wide version switch. An encoding identifier
has immutable semantics; incompatible semantics require a new identifier.

## Binary envelope

All integers are unsigned little-endian. Offsets are absolute file offsets.

| Offset | Bytes | Meaning |
|---|---|---|
| 0 | 8 | Magic `47 53 56 00 0d 0a 1a 0a` |
| 8 | 8 | Exact file length |
| 16 | 8 | JSON manifest offset |
| 24 | 8 | JSON manifest byte length |
| 32 | 32 | SHA-256 of the exact manifest bytes |

The manifest is UTF-8 JSON at the end of the file. It is at most 64 MiB, contains
no NaN, infinity, duplicate keys, or external URLs used as executable resources.
Buffers precede it and start at multiples of 16,384 bytes. They do not overlap
the header, manifest, or one another. Padding has no meaning and is excluded
from buffer hashes. The exact file end equals manifest offset plus length.
Writers stream buffers, then the manifest, and atomically publish the completed
file. Interrupted files must not replace an existing completed document.

## Manifest

Required fields are `format: "gsv.scene"`, `objects: []`, `buffers: {}` and
`arrays: {}`. Optional dictionaries are `spaces`, `clocks` and `metadata`.
Unknown namespaced fields and vendor-namespace objects must survive a read/write round
trip. The `gsv.*` namespace is reserved for the registered contracts: readers
reject unregistered core identifiers, including on envelope-only open. A reader
can inspect/preserve an unknown vendor encoding but must not imply it
can evaluate or render it. Loading a document never executes code.

Buffers are keyed by a document-local string ID and describe `offset`, `length`
and lowercase `sha256`. Each buffer reserves its length rounded up to 16,384
bytes, with zero padding; this entire reservation precedes the manifest and
does not overlap another buffer. Arrays are typed contiguous C-order views with `buffer`,
`dtype`, `shape`, and optional `byte_offset` (default 0). Dtypes are `f32`, `f64`,
`i8`, `u8`, `i16`, `u16`, `i32`, `u32`, `i64`, `u64`, `bool`; all multibyte values
are little-endian. Array sizes must fit their buffer; checked arithmetic is
required. Object IDs and references are strings. Numeric identities use integer
arrays, never JSON floating-point numbers. Empty arrays are valid.

Readers validate the envelope, manifest digest, nonoverlap, references and array
bounds before mapping. Buffer hashes are verified on first access or eagerly
by an explicit audit. Readers impose configurable allocation limits and detect
file changes while open. No Python pickle, dynamic import, or embedded program
is part of the format.

## Objects

Every object has unique `id`, `type`, `encoding`, and `data` fields. Optional
fields include `label`, `space`, `clock`, `metadata`, and `links`. `type` describes
the kind of information (for example `gaussian`, `camera`, `scaffold`, `motion`,
`tracks`, `pointcloud`, `mesh`, `image`, `appearance`, `render`, `tensor`, `provenance`). `encoding` defines
the interpretation of `data`; core encodings use the `gsv.` namespace. Application
extensions should use a reverse-domain namespace they control. `links` maps
semantic relationship names to object IDs or lists of IDs. Links are not ownership:
the same motion or camera object can be used by several representations.

Each encoding defines array-reference fields explicitly. Provenance records
source repository commit, asset digests, command, parameter activation, world
normalization, training/evaluation split and any lossy transformation. Derived
render caches are separate objects linked to their source and declare sampling
and fidelity; they are never silently substituted for the native representation.

## Coordinates and time

`spaces` maps IDs to descriptors (`unit`, `handedness`, `axes`, optional source
convention). Geometry expresses coordinates in its declared space. Proper rigid
camera poses use column vectors and row-major stored matrices; camera local axes
are x right, y down, z forward. Source conversions must be explicit and preserved.
Unknown scene scale is `unitless`, not assumed meters.

Gaussian fields, sampled scaffolds, pinhole cameras and SE3 motion bases require
an explicit `space`. Time-dependent Gaussian fields, sampled scaffolds, pinhole
cameras and motion bases also require a `clock`. A one-sample camera still names
its clock. Missing descriptors never imply a common world or time reference.
Static analytic Gaussian fields need no clock; empty documents need neither.

`clocks` maps IDs to descriptors with `unit` (`second`, `frame`, or `normalized`),
optional origin and domain. Unknown physical frame rate must not be invented.
An optional `mapping` declares `target` clock ID, positive `scale`, and `offset`:
target time = scale * source time + offset. Mappings must be acyclic and explicit.
Object time arrays are float64, finite and strictly increasing. A representation
can be static without a clock. Clocks do not require uniform frame intervals.
Cross-object alignment must be explicit; equal array length does not establish
synchronization. Birth/death, validity and missing observations are distinct.

## Identity-Persistent Gaussian Trajectories

`type: "gaussian"`, `encoding: "gsv.gaussian.persistent-identity"`. Data contains `ids` (i64[N]
strictly increasing), `times` (f64[T]), `frames` (T array IDs, each f32[N,16]), and
`bounds` ([[minx,miny,minz],[maxx,maxy,maxz]]). N and T may be zero together.
This is a first-class Gaussian representation, at the same level as spacetime,
joint covariance4D, motion bases and deformation encodings. Its defining contract
is the persistence of each Gaussian's identity across the entire sequence.
The i-th row of every time state belongs to exactly `ids[i]`. IDs are local to
the object/sequence, arbitrary up to one global permutation, never recycled,
and must not be re-matched independently per frame. Invalid rows retain their
identity; reappearance must identify the same Gaussian. A new Gaussian gets a
new ID in the fixed union identity table, not a reused inactive slot. Geometry
formatting cannot establish that an exporter supplied physically correct
correspondence; source tracking and association evidence are separate.

The temporal states are explicit. This does not imply an analytic continuous
representation or prescribe interpolation between stored states. A consumer
must declare its temporal evaluation rule. The Python reference evaluator picks
the nearest state (ties choose the earlier state).

The encoding can represent the native identity-persistent model or an explicitly
derived display cache from another representation. Those roles are declared in
metadata and source links; a derived cache must not replace or redefine its
native source as the identity-persistent method. No Gaussian encoding owns or
subordinates the others.

Each row is position xyz, activated opacity, positive axis standard deviations
xyz, validity 0/1, quaternion wxyz (unit, local axes to world), evaluated RGB xyz,
and confidence (0..1 or -1 unavailable). All lanes are finite, including invalid
slots. RGB can be outside [0,1]: some source renderers learn unconstrained color
and clamp after compositing. Importers must not silently clip primitive colors.
RGB is evaluated color; full view-dependent appearance belongs in a
separate array/object or a native encoding. `data.color_space` declares `srgb` or `linear-srgb`; the source view used for a
view-dependent bake belongs in provenance. No hidden identity subsampling is allowed.
Bounds enclose centers expanded by four maximum-axis standard deviations.

## Camera, render and observation graph

The normative [camera/render contract](RENDER_CONTRACT.md) defines these peer
encodings and their complete schemas:

| Encoding | Responsibility |
|---|---|
| `gsv.camera.pinhole` | Nested projection and pose, exact calibration axes, optional physical optics, capture timing and authored view volume |
| `gsv.render.profile` | Explicit Gaussian visibility, projection derivative, support, filtering, alpha and compositing semantics |
| `gsv.render.view` | A concrete camera view, ordered Gaussian fields, algorithm profile, appearance, output raster and temporal integration |
| `gsv.observation.camera-rgb` | Optional original RGB/masks own image times with exact calibration sample/view indices, pixel domain and acquisition role |

A camera is not a renderer. Its projection depth mapping, authored axial view
volume and a renderer's Gaussian-center depth test have separate meanings.
Only a render profile determines which of these tests is performed. Unbounded
limits are explicit, never a large guessed far distance or JSON infinity.

K is the authoritative ray calibration. Optical focal length, aperture and sensor
size are optional descriptive facts. Physical exposure and rolling shutter do
not implicitly apply gain or motion blur. Explicitly named distortion equations
are stored without pretending that every consumer can render them. The nested
projection/pose contract uses double-precision calibration and stable view IDs.

Render views prevent accidental mixing of cameras, fields, source appearances
and reference images. Output resolution and image-plane transform are explicit;
quantitative native-RGB comparison accepts the original camera raster and exact
indexed sample. Storing a cropped or rectified observation requires its matching
camera calibration and processing provenance. No nearest-frame image matching,
automatic exposure compensation or silently inferred field set is permitted.

GT is optional. A captured photograph remains distinct from a predicted render,
and its role alone proves neither independent evaluation nor physical truth.
Training captures remain training data. Unknown/unsupported rendering features
are inspectable; they cannot be silently treated as an exact source rendering.

## Camera-bound affine radiometry

`type: "appearance"`, `encoding: "gsv.appearance.camera-affine"` is an optional
peer object. `links.camera` names one pinhole camera; `space` and `clock` exactly
match that camera. Its `data` contains array references `gain` and `bias`, both
f32[T,V,3] matching the camera's complete timestamp/view axes, and `color_space`
(`srgb` or `linear-srgb`). Gain must be finite and nonnegative; bias finite.
There is no implicit broadcasting, temporal interpolation or profile selection.

A render view selects radiometry through `links.radiometry`, and its linked
camera and color domain must match the appearance object. It acts on the entire
fully composited image including its background:

```
RGB = gain[t,v] * composited_RGB + bias[t,v]
output = clip(RGB, 0, 1)  # when the render view requests unit clamp
```

Do not clip the composite before this affine map. Do not apply gain/bias to
individual Gaussian colors, alpha, geometry or the GT observation. The view's
explicit field set receives this one post-composite operation. Exact camera
sample/view indices choose the coefficients; the source activation, such as
`gain=exp(log_gain)`, belongs in provenance. This is retained learned appearance,
not a brightness fit against the displayed reference.

## RGB comparison metric contract

Compare aligned RGB at native camera dimensions in an explicitly selected common
color domain. Normalize u8 by 255; range is fixed to 1. Floating pixels outside
[0,1] are rejected, not clipped. Conversion between sRGB and linear-sRGB must be
explicit and recorded. No exposure compensation or automatic registration.

MSE and MAE average all three channels over mask-true pixels; PSNR is
`10 log10(1/MSE)` and positive infinity for exact equality. SSIM uses a normalized
11x11 Gaussian window, sigma=1.5, K1=.01, K2=.03, population covariance, and equal
channel averaging. Only complete windows inside the image whose entire support
is mask-true contribute; there is no padding. Tiny negative variance caused by
floating-point cancellation is clamped to zero. Fewer than 11 pixels on either
axis means SSIM is unavailable. No mask-selected pixels means all metrics are
unavailable; no complete valid window means only SSIM is unavailable. Reports
include color domain, sample counts and unavailable reasons. These are image
agreement metrics, never evidence of physical trajectory correctness.

This matches the relevant explicit configuration of the
[scikit-image structural_similarity reference](https://scikit-image.org/docs/stable/api/skimage.metrics.html#skimage.metrics.structural_similarity)
(`gaussian_weights=True`, `sigma=1.5`, `use_sample_covariance=False`, `data_range=1`).

## Extensibility and evidence

The codec registry describes native parameterizations, auxiliary objects and
consumer capability independently. A library can store an encoding before a
viewer implements its evaluation. A paper integration is complete only after
an actual source case produces a validated GSV and its fidelity is checked.
Import, training, export, native rendering and cache rendering are separate
acceptance statuses.
