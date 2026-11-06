# GSV object encodings

The [container specification](SPECIFICATION.md) controls framing, typed storage,
references and integrity. This registry controls object meaning. A file may mix
any number of these encodings, or contain none. Native parameters and evaluated
caches have separate identities and explicit source links.

## Gaussian representations

All Gaussian identities are sequence-local signed int64 values. Scales below
are standard deviations, opacity is activated alpha, rotations use wxyz, and
colors are finite per-primitive RGB (which may exceed display range). Original
logits/log-scales/SH coefficients belong in source tensors when converted to
activated parameters. No conversion may silently discard them.
Every directly RGB-bearing Gaussian encoding in the first five analytical/explicit
families requires typed `data.color_space` (`srgb` or `linear-srgb`). A render
view cannot silently mix these domains. Native deformation tensor archives do
not claim evaluated RGB until an explicit source evaluator provides it.

| Encoding | Required data arrays | Evaluation |
|---|---|---|
| `gsv.gaussian.persistent-identity` | ids[N], times[T], frames[T] references | Identity-Persistent Gaussian Trajectories; invariant sequence-local identity rows, explicit temporal states |
| `gsv.gaussian.static` | ids[N], means[N,3], scales[N,3], quaternions[N,4], opacity[N], colors[N,3] | Static anisotropic ellipsoids |
| `gsv.gaussian.spacetime-polynomial` | static fields plus motion[N,3,3], omega[N,4], time_centers[N], time_widths[N] | Cubic center motion, additive quaternion motion, temporal opacity |
| `gsv.gaussian.covariance4d` | ids[N], means[N,4], covariance[N,4,4], opacity[N], colors[N,3] | Conditional 3D Gaussian from joint space-time covariance |
| `gsv.gaussian.se3-basis` | ids[N], canonical_means[N,3], scales[N,3], quaternions[N,4], opacity[N], colors[N,3], coefficients[N,K] | Links a `gsv.motion.se3-basis` object |
| `gsv.gaussian.canonical-deformation` | canonical field-name→array mapping, explicit recipe | Links motion object; source evaluator needed |
| `gsv.gaussian.scaffold-deformation` | canonical field-name→array mapping, explicit recipe | Links scaffold graph and explicit association objects; source evaluator needed |

### Identity-Persistent Gaussian Trajectories

The `gsv.gaussian.persistent-identity` encoding is a native representation peer,
not a generic base class above other papers' Gaussian representations. Each ID
owns a complete trajectory of Gaussian geometry, opacity, orientation and color
across explicit times. A fixed union table preserves IDs through inactive states;
row order never changes identity and an inactive ID is never recycled. Identity
is sequence-local, arbitrary up to one global permutation. Independent per-frame
matching destroys this contract even when the rendered images stay identical.

All temporal states are f32[N,16] using the normative attribute layout in the
specification. Temporal interpolation is a consumer policy; the reference
evaluator chooses the nearest state. The same numerical layout can carry a
clearly identified evaluated cache linked to another native representation, but
that is a derived artifact, not a declaration that the source method changed.

### Spacetime polynomial

With d = t − time_center, μ(t) = μ + m1 d + m2 d² + m3 d³;
q(t) = normalize(q_base + omega d); α(t) = α_base exp(−(d/width)²).
The quaternion base may be unnormalized. Normalizing it before adding omega
changes the source model. The temporal exponent has no factor 1/2. This is the
Spacetime Gaussians parameterization, not a joint four-dimensional Gaussian.
Full STG learned screen-space appearance is a separate network/feature object;
direct RGB samples do not reproduce that decoder. The Lite RGB route can retain
its unconstrained primitive colors exactly.

### Joint covariance4D

Partition the positive-definite covariance into spatial block A, cross vector b
and temporal variance c. At time t, d=t−μ_t:

* center = μ_xyz + b d/c;
* spatial covariance = A − bbᵀ/c;
* alpha = alpha_base exp(−d²/(2(c+temporal_prefilter))).

The optional temporal_prefilter is nonnegative variance and affects the opacity
only. Storing the covariance avoids dependence on a particular SO(4) quaternion
pair convention. A time-coordinate rescaling must transform both covariance
cross terms and temporal variance, not only timestamps. Full source SH in space
and time remains separately archived.

### SE3 motion bases

The motion object stores `times[T]`, `rotations6d[K,T,6]` and
`translations[K,T,3]`. Per-Gaussian coefficients are nonnegative and sum to one.
First blend the raw six rotation coordinates using those coefficients; then
Gram–Schmidt the first and second 3-vectors into rotation columns. Blend
translations with the same coefficients. Transform the canonical Gaussian
center and orientation with that resulting rigid transform. Blending already
normalized rotations gives a different model. The reference evaluator linearly
interpolates basis parameters between stored times for inspection; this is an
explicit display rule, not additional source training evidence.

### Canonical and scaffold deformation

HexPlane features, Fourier MLP deformation, control-node warps and dual-quaternion
skinning share reusable structural components but differ in activation order,
coordinate normalization, rotation updates, SH handling and motion masks.
The recipe requires nonempty strings `source_commit`, `architecture`, `entrypoint`
and an explicit `configuration` dictionary containing the actual flags. A commit
can identify an immutable source snapshot (`snapshot:<digest>`) when Git is not
the execution source. Additional fields retain activation order, source tensor
layout and appearance rules. `canonical-deformation` links a
`gsv.motion.neural` object; `scaffold-deformation` links a `gsv.scaffold.graph`
and an `association` object encoded as `gsv.binding.weights` or
`gsv.tensor.bundle`. The tensor alternative retains source-specific binding
parameters; their recipe must specify exactly how to use them.

Safe tensor storage does not by itself confer evaluation
support. An evaluated cache declares upstream evaluator commit, checkpoint digest,
queried source times, identity mapping and appearance fidelity.

## Peer objects

| Encoding | Data and meaning |
|---|---|
| `gsv.camera.pinhole` | Independent times and stable views; nested projection/pose, optional view volume, optics and acquisition; see camera/render contract |
| `gsv.render.profile` | Explicit EWA visibility, projection, filtering, support, alpha and compositing; renderer capability is separate |
| `gsv.render.view` | Concrete camera view + ordered field set + profile + optional radiometry/observations + raster/color/time output contract |
| `gsv.scaffold.graph` | ids[N], times[T], positions[T,N,3], optional quaternions[T,N,4], edges[E,2] indexing node rows, and separate certainty/visibility/validity[T,N] masks |
| `gsv.motion.se3-basis` | Raw six-dimensional rotation bases and translations on a declared time axis |
| `gsv.motion.neural` | Named numeric tensors plus required architecture/warp recipe; never executable on file open |
| `gsv.tracks.points` | ids[N], times[T], positions[T,N,3]; observation visibility and reconstruction validity are different masks |
| `gsv.tracks.image` | ids[N], times[T], pixels[T,N,2], integer/half pixel center, links.camera; independent visibility and validity masks |
| `gsv.transform.sampled-affine` | times[T], matrices f64[T,4,4], source_space and target_space; column-vector map from source to target, invertible, no implied interpolation |
| `gsv.binding.weights` | source_ids[N], target_rows integer[N,K], weights float[N,K], normalization; links.source/target must expose ID tables |
| `gsv.geometry.points` | positions[N,3], optional stable ids and attributes |
| `gsv.geometry.triangles` | vertices[N,3], integer triangles[F,3] indexing rows; attributes can be independent tensors |
| `gsv.observation.image` | pixels array with explicit semantic such as rgb/depth/mask/features; links to camera/time/provenance |
| `gsv.observation.camera-rgb` | Optional exact-camera RGB: independent image times, indexed calibration and nested pixels with masks, color domain and role; links.camera |
| `gsv.appearance.camera-affine` | Optional gain/bias f32[T,V,3] on linked camera axes; explicit color_space; applied after full compositing and before final clipping; render-view selection through links.radiometry |
| `gsv.tensor.bundle` | Name→array references for lossless parameters, associations, uncertainty or features |
| `gsv.provenance.record` | Required method string and sources list; each source has path or uri, optional SHA-256; additional acquisition, transformation and evaluation evidence retained |

Scaffolds are not required to own a Gaussian field. A camera-only document,
observation graph without any Gaussian field, or empty document is as valid as a full reconstruction.
Topology and skinning indices are never inferred silently. An absent visibility
mask does not mean every point was observed. A GT camera aligned for display has
an explicit transform and remains distinguishable from an estimated camera.

Transform objects are independent data. Consumers must opt into a particular
transform path; merely including a transform never silently relocates other
objects. An affine transform can change covariance and scale, so transforming
only Gaussian centers is incorrect. Bindings store target **row indices**, not
target numeric identities. Every slot references a real row even at zero weight;
there is no ambiguous -1 sentinel. Partition-of-unity and unnormalized weights
are distinct contracts, and neither alone specifies a deformation recipe.

Exact camera, render-profile, render-view and RGB schemas are specified in
[Camera and rendering](RENDER_CONTRACT.md).
