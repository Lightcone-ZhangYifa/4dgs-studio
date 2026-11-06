# Design rationale and extension rules

GSV separates four concerns: the file envelope, typed storage, object semantics,
and consumer capabilities. This boundary is deliberate: a new paper's model
should require a new codec or recipe, not a different scene file architecture.

## Storage

The fixed 64-byte header locates a checksummed manifest. A manifest at the end
allows streaming an unknown number of samples without holding them in memory or
rewriting earlier payloads. Independent checksummed buffers support lazy loading,
corruption diagnosis and selective verification. The 16 KiB alignment permits
both operating-system memory mapping and Apple silicon buffer alignment. Typed
views give exact shapes, integer identity types and byte order; numeric IDs never
pass through a floating-point JSON representation.

The core array view is deliberately uncompressed and contiguous. A quantized or
compressed representation can store byte payloads as u8 arrays and declare a new
object codec with its decoder contract. Such payloads must not be advertised as
plain float arrays. Interpreting a future compressed codec is a consumer
capability, while copying its opaque payload is an envelope capability.

A writer streams into a same-directory temporary file, validates, flushes and
atomically publishes. Without overwrite permission it uses a no-clobber commit.
The Python writer also flushes the parent directory. An interruption before
publication cannot replace an existing completed file. This protects publication;
it does not turn mutable external storage into a transactional database.

## Object graph

Objects are independent peers, identified by strings. A scene has no required
root Gaussian field and no implicit first camera. Empty documents and camera,
scaffold, observation or transform-only documents are normal cases. Typed links
express relations without ownership: several fields can reuse one motion basis,
and several annotations can refer to one camera.

An object's encoding identifies immutable mathematical semantics. Broad types
help organize the scene tree; they do not authorize a reader to reinterpret an
unrecognized encoding as a familiar representation. Paper names are provenance,
not file format selectors. Equivalent parameterizations can share a codec after
an explicit conversion, with original tensors retained separately.

Identity-Persistent Gaussian Trajectories is one native Gaussian representation,
at the same level as the other mathematical encodings. Its persistent IDs are a
sequence-wide correspondence contract, not merely a storage detail. Other native
representations are never subordinated to it or automatically rewritten into it.

An evaluated playback cache is a distinct object linked to its native source. It
records source times, identity mapping and fidelity. A viewer may display that
cache, but must label interpolation and appearance approximations. There is no
silent fallback that changes what a native object means.

Camera RGB observations are also peer objects, not required Gaussian metadata.
Their own image times and exact calibration-sample/view indices establish
intrinsics, extrinsics and image dimensions. Static calibration may be reused
across an entire video without storing duplicate camera matrices. A GT photograph is the independent image payload,
not a camera matrix and not a reconstruction's own rendered output. Optional
masks explicitly delimit valid comparison pixels. The container carries an
observation's color domain, so viewers can make render/reference comparison
without guessing color transfer, resampling images or matching unrelated clocks.
Documents without observations are fully supported and have no image metrics.

Camera radiometry is a separate optional appearance object. It binds a calibrated
RGB gain/bias to exact camera axes and applies after the complete image composite,
before final clipping. A render view selects it through an explicit link. This preserves
learned camera response without altering camera rays, source photographs or
per-Gaussian colors, and never fits exposure against the currently displayed GT.

## Camera, algorithm and concrete image

Three different depth intervals occur in real splatting implementations. A
projection matrix maps depth to a graphics API range; an authored camera volume
describes intended visible axial bounds; an algorithm may discard a Gaussian
based only on its center depth. These intervals can differ by orders of magnitude.
A generic `near` field cannot unambiguously represent all three. GSV assigns the
first two to camera geometry and the third to an explicit render profile.

A reusable profile describes algorithm semantics, including boundary inclusion,
pixel offset, covariance filtering, alpha rejection and transmittance termination.
A separate render view binds that algorithm to one camera view, an ordered field
set and an output image. This also makes combined static/dynamic reconstruction
explicit and prevents drawing a native model and its derived cache twice.

Camera acquisition describes what happened during capture. Render temporal
integration describes what an image generator will simulate. Optical metadata
never overrides an authoritative calibration matrix. Likewise learned camera
response, physical exposure and a viewer's display transform remain distinct.
Recorded features beyond a consumer's capabilities are not silently approximated.

The contract is informed by OpenUSD's separation of cameras and render settings,
glTF's explicit projection depth ranges, OpenEXR's distinction between image and
capture metadata, and direct inspection of eight splatting implementations.
[Research and design decisions](CAMERA_RESEARCH.md) records these sources and the
conversion pitfalls; [the contract](RENDER_CONTRACT.md) specifies exact fields.

## Coordinates and clocks

A space descriptor names units, handedness and axis labels. Different names are
different spaces even when descriptions happen to match. Independent sampled
affine transform objects record explicit source-to-target maps. Consumers opt
into transforms; including one does not automatically relocate geometry.

Clocks have physical seconds, frame indices or normalized coordinates. Positive
affine mappings declare synchronization and form an acyclic graph. Conversion
requires a common declared reference; equal sample counts or equal unit strings
are insufficient. `Scene.convert_time` resolves the mapping in either direction.
Evaluation timestamps are in the evaluated object's clock. When a Gaussian field
links a motion basis using a different clock, the reference evaluator converts
between them explicitly.

A camera uses x-right/y-down/z-forward local axes and a column-vector c2w pose.
World convention is separate. Original calibration, display alignment, predicted
camera and independent GT remain distinguishable objects with provenance.
The projection declares either no distortion or an explicit mathematical model
and ordered coefficients. Ignoring a declared distortion model is not a valid
conversion. Unsupported models remain inspectable without claiming calibrated
rendering support.

## Extending the registry

1. Identify whether the new data has exactly the semantics of an existing codec.
   Different tensor layout alone does not justify a new mathematical encoding.
2. If semantics differ, choose a namespaced encoding with an unambiguous contract:
   array dtype/shape, activation, units, axes, identity, time, validity, required
   links and evaluation equations. Incompatible semantics get a new identifier.
3. Add semantic validation and an adapter with original state/provenance retained.
   Array serialization itself must never run executable checkpoint code.
4. Test a real source case. Storage roundtrip, geometry parity, source-image parity
   and native Studio rendering are separate acceptance items.
5. Declare consumer limitations. An unknown vendor object may be listed and preserved;
   it must not be marked evaluated or rendered merely because its tensors loaded.

The core `gsv.*` namespace is reserved for registered contracts. An unregistered
core identifier is an error, while a vendor namespace remains safely opaque.

The envelope and core array contracts stay unchanged throughout these steps.
Object contracts are resolved directly by encoding identifier.

## Evidence and reliability boundary

Digests detect corruption, not publisher identity or physical truth. Validation
checks stored values and implemented contracts; it does not establish that a
reconstruction is accurate. A zero source-geometry roundtrip error establishes
serialization parity, while scene quality still requires independent evaluation.
The library imposes manifest/array limits and verifies lazy payloads before use.
Opening a file never imports a model, unpickles weights or follows executable URLs.
