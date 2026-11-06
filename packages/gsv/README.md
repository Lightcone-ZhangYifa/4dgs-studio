# GSV Python

**GSV** is the Python package and the container format. It stores independently
optional Gaussian representations, cameras, render profiles/views, observations, motion bases, scaffolds,
tracks, meshes, bindings, tensors and provenance in one random-access file.
Empty documents, camera-only documents and geometry without GT are valid.

The SDK runs on macOS and Linux with Python >=3.10 and NumPy >=1.24. Distribution and import names are
both `gsv`. CUDA, PyTorch, SciPy and paper repositories are not dependencies of
container I/O, geometry reference evaluation or RGB metrics.

```sh
python -m pip install -e packages/gsv
gsv info scene.gsv
gsv validate scene.gsv
```

## Representation and object model

`gsv.gaussian.persistent-identity` names **Identity-Persistent Gaussian
Trajectories / 身份持续高斯轨迹**. It is one native Gaussian representation at the
same level as spacetime polynomial, joint covariance4D, SE3 motion bases and
canonical/scaffold deformation. It is not a universal base class above them.

Each `int64` identity owns the same row across all temporal states. Inactive
states retain their identity; IDs are never recycled or independently re-matched
per frame. All object types are peers linked through explicit references. An
evaluated display cache can link to another representation while remaining
clearly identified as a derived artifact.

Contracts: [file specification](../../docs/gsv/SPECIFICATION.md),
[encoding registry](../../docs/gsv/ENCODINGS.md),
[camera and render contract](../../docs/gsv/RENDER_CONTRACT.md),
[design rationale](../../docs/gsv/DESIGN.md).

## Write identity-persistent geometry

```python
import numpy as np
from gsv import Writer, add_persistent_gaussians

ids = np.array([7, 2**60 + 3], dtype=np.int64)
times = np.array([0.0, 0.5, 1.0], dtype=np.float64)


def states():
    for t in times:
        frame = np.zeros((2, 16), dtype=np.float32)
        frame[:, :3] = [[t, 0, 2], [0, t, 2]]
        frame[:, 3] = 0.8  # Activated alpha
        frame[:, 4:7] = 0.02  # Positive standard deviations
        frame[:, 7] = 1  # Valid identity state
        frame[:, 8] = 1  # Unit quaternion WXYZ
        frame[:, 12:15] = 0.5  # Evaluated RGB
        frame[:, 15] = -1  # Confidence unknown
        yield frame


with Writer("trajectories.gsv") as out:
    out.manifest["spaces"]["world"] = {
        "unit": "meter",
        "handedness": "right",
        "axes": ["x", "y", "z"],
    }
    out.manifest["clocks"]["scene"] = {"unit": "second"}
    add_persistent_gaussians(
        out,
        "trajectories",
        ids,
        times,
        states(),
        color_space="srgb",
        metadata={"purpose": "native_representation"},
    )
```

The generator streams a sequence without holding all frames in memory. Each
frame is strictly float32[N,16]: xyz, alpha, scale xyz, validity, WXYZ, RGB,
confidence. IDs must already be sorted unique int64, never cast through float.
All lanes remain finite even for invalid states. Cameras have independent time
axes; their timestamp count never determines a Gaussian trajectory length.
RGB-bearing Gaussian codecs require `data.color_space` (`srgb` or
`linear-srgb`). The persistent helper writes it explicitly from `color_space`,
whose convenience default is `srgb`. Direct writers for static, spacetime,
covariance4D and SE3-basis must provide the field themselves. Metadata never
overrides this declaration. Render views reject mismatched field/output domains;
convert source RGB explicitly before writing when needed.

Set capture space/clock descriptors before calling helpers. Their fallback is a
unitless world and seconds, appropriate only when intended. An unknown frame rate
must use a frame clock and original source indices. Existing descriptors remain.

## Camera and optional GT photographs

`add_camera` accepts times[T], K[T,V,3,3], c2w[T,V,4,4] and image_sizes[V,2]
(width, height). Matrices use column-vector math and row-major storage. Camera
axes are right/down/forward; pixel centers must match source calibration.
The helper writes nested `projection` and `pose` descriptors and stable `view_ids`.
Pass `view_ids=["front", "side"]` when acquisition identifiers are known; omitted
IDs are explicit decimal strings of the view positions. A single camera sample is
static calibration usable throughout a field's time domain.

Optional keywords retain additional capture facts:

- `depth_mapping={"near": .01, "far": None, "ndc_range": "zero-to-one", "direction": "forward"}` describes source projection depth mapping. It never changes rays or acts as a Gaussian filter.
- `view_volume={"near": near, "far": far, "far_unbounded": flags}` stores f64[T,V] authored depth limits and bool[T,V] flags. Unbounded far slots contain zero; finite far must exceed near. Only a render profile opting into camera-volume filtering applies it.
- `optics={"focal_length_mm": values, "f_number": values, ...}` and `acquisition={"exposure_seconds": values, "iso": values, ...}` accept numerical tracks shaped [T,V]. K remains authoritative. Shutter-open/close tracks are paired; nonzero rolling readout requires its named direction.
- `distortion={"model": "opencv-brown", "coefficients": values}` stores f64[T,V,5] k1,k2,p1,p2,k3; `opencv-fisheye` uses f64[T,V,4]. Omission writes explicit `{"model":"none"}`. Stored distorted cameras are valid even when a particular renderer cannot render them; silently ignoring distortion is forbidden.

This function writes already-decoded input images; it does not invent images
from a reconstruction. The supplied source images must have the same resolution
and rectification as the camera:

```python
import numpy as np
from gsv import Writer, add_camera, add_camera_rgb


def write_capture(path, first_rgb, last_rgb):
    # Input RGB uint8[480,640,3] or float32[480,640,3] in [0,1].
    K = np.array([[500.0, 0, 320], [0, 500, 240], [0, 0, 1]])
    intrinsics = np.repeat(K[None, None], 2, axis=0)
    intrinsics[1, 0, 0, 0] = 520
    poses = np.repeat(np.eye(4)[None, None], 2, axis=0)
    poses[1, 0, 0, 3] = 0.1
    with Writer(path) as out:
        add_camera(
            out,
            "capture-camera",
            [0.0, 1.0],
            intrinsics,
            poses,
            [[640, 480]],
            pixel_center="half",
            metadata={"role": "calibration"},
        )
        add_camera_rgb(
            out,
            "input-photos",
            "capture-camera",
            camera_samples=[0, 1],
            view_indices=[0, 0],
            images=iter([first_rgb, last_rgb]),
            color_space="srgb",
            role="ground_truth",
            metadata={"split": "train", "source": "record exact acquisition here"},
        )
```

Camera samples are **integer row indices in that camera**, not timestamp values.
View indices address its rig views. Integer lists/arrays convert losslessly to
i64/i32; float/bool/overflow indices fail. Observations store their own explicit
f64[M] `samples.times` on the shared clock. Optional `times=` supplies those
timestamps; omission records the indexed camera timestamps. For a one-sample
static camera, pass `camera_samples=[0, 0, 0]`, `times=[0., .5, 1.]` to store a
video without repeating calibration. For a multi-sample camera each observation
time must exactly equal its indexed calibration time. `(time, view)` pairs must
be unique within an observation object; arbitrary row order is allowed.
Every image size must exactly match its selected view.
`masks=` optionally supplies bool[H,W] validity masks, one per image. Images and
masks can be iterators. Empty observation collections are valid.

`color_space` is `srgb` or `linear-srgb`; `role` is `ground_truth`, `reference`
or `prediction`. No gamma, crop, resizing, exposure, distortion correction or
timestamp guessing occurs. Space and clock are inherited from the camera and
validated for exact equality. Multiple observation objects may share a camera.
A GT role identifies the image reference, not proof of physical geometry; a
training photograph remains training data. Source-rendered images must be labeled
predictions, and synthetic examples clearly identified.

For a camera-only document omit `add_camera_rgb`. An empty document:

```python
from gsv import Writer

with Writer("empty.gsv"):
    pass
```

## Bind a camera to an explicit renderer and field set

`add_render_profile` stores algorithm semantics; `add_render_view` chooses the
camera view, Gaussian fields, profile and output. Gaussian-center near/far,
covariance filtering, support bounds and compositing thresholds are separate
from camera calibration. There are no policy defaults selected by paper name.

```python
from gsv import add_render_profile, add_render_view

# Inside a Writer containing capture-camera, trajectories and input-photos:
add_render_profile(
    out,
    "ewa-profile",
    visibility={
        "center_depth": {
            "near": 0.2,
            "far": None,
            "near_inclusive": False,
            "far_inclusive": True,
        },
        "camera_volume": "ignore",
    },
    projection={
        "divide_epsilon": 1e-7,
        "jacobian": {"mode": "symmetric-fov", "factor": 1.3},
        "pixel_offset": [0.0, 0.0],
    },
    filter={
        "covariance_dilation": 0.3,
        "radius_clip": 0.0,
        "antialias_compensation": False,
    },
    support={
        "bounds": "tile-major-radius",
        "sigma": 3.0,
        "tile_size": [16, 16],
        "mahalanobis_cutoff": None,
    },
    alpha={"minimum": 1 / 255, "maximum": 0.99},
    compositing={
        "direction": "front-to-back",
        "depth_sort": "tile-stable",
        "tie_break": "source-order",
        "transmittance": {
            "threshold": 1e-4,
            "inclusive": False,
            "stage": "before-contribution",
        },
    },
)
add_render_view(
    out,
    "front-view",
    "capture-camera",
    ["trajectories"],
    "ewa-profile",
    camera_view=0,
    color_space="srgb",
    background=(0, 0, 0),
    observation_ids=["input-photos"],
)
```

All required policy fields are stored. The example values describe one explicit
EWA profile; select actual source values when exporting another renderer. A
consumer must disclose unsupported or approximate profile behavior. A successful
file validation does not assert pixel parity with an external renderer.

Omitting `image_size`/`camera_to_output` selects the camera's native raster and
identity transform. Explicit scale/crop uses a positive diagonal affine 3x3
`camera_to_output`; output K is that matrix times camera K. Observation pixels
are never transformed implicitly. Quantitative GT comparison requires native
resolution, identity transform, unit clamp, matching color domain, explicit
observation membership, and the exact calibration sample. A multi-view observation
collection may be linked; each comparison still matches its indexed rig view.

Render fields are ordered, unique and nonempty; select either the native object
or its evaluated display cache. Fields and camera must share space/clock, except
a truly static Gaussian field may omit its clock. Independent camera and field
sample counts are allowed. `temporal={"mode":"instantaneous"}` is written by
default. An explicit `integrated` policy records shutter offsets, a positive
sample count and `sampling="uniform-midpoint"`; consumers must refuse this mode
unless they implement it. Capture exposure metadata alone never enables blur.

## Read and evaluate

```python
import numpy as np
from gsv import Scene
from gsv.evaluate import evaluate

with Scene("trajectories.gsv") as scene:
    print(scene.validate())
    obj = scene.object("trajectories")
    ids = scene.array(obj["data"]["ids"])
    geometry = evaluate(scene, "trajectories", 0.5)
    # geometry is float32[N,16]; IDs are obtained separately.
    editable = np.array(geometry, copy=True)
    scene.copy("trajectories-copy.gsv")
```

Opening checks the envelope and manifest hash; first array access verifies its
buffer by default. `Scene.array` returns a readonly memory map (an empty array
for zero-length data). `scene.validate()` checks every buffer and known object
contract. Boolean views are indexed by buffer so payload verification does not
scan the full array table for every frame; shared-buffer views remain validated.
The default 16 GiB access limit applies per array and can be changed
with `max_array_bytes`. `verify=False` skips lazy payload hashes, not manifest or
structural checks; explicit `validate()` still verifies all buffers.

The reference evaluator supports persistent identity, static, spacetime
polynomial, covariance4D and SE3-basis geometry. Persistent identity returns
the nearest temporal state, with earlier-state ties. It does not render images,
run neural deformation or SH/learned appearance, or automatically align spaces.
Returned geometry is not a source-render parity claim.

If a frame clock maps by `{"target":"seconds","scale":0.04,"offset":2}`,
`scene.convert_time(50, "frames", "seconds")` returns 4 seconds. Unrelated clocks
fail instead of guessing a frame rate. Treat an opened scene's manifest as a
description of that read-only document. Array-layout changes on an existing
`Scene` are unsupported; use a new Writer for edited documents.

## RGB metrics in a declared color domain

An optional camera-affine radiometry object retains a source model's learned
camera color calibration separately from geometry and GT. It applies to the
fully composited image before final clipping, never to individual splats:

```python
from gsv import add_camera_affine, add_render_view

# Inside the same Writer, after adding capture-camera and the Gaussian object:
add_camera_affine(
    out, "camera-radiometry", "capture-camera", gain, bias, color_space="srgb"
)  # gain/bias float32[T,V,3]
add_render_view(
    out,
    "corrected-view",
    "capture-camera",
    ["trajectories"],
    "ewa-profile",
    color_space="srgb",
    radiometry_id="camera-radiometry",
    observation_ids=["input-photos"],
)

# At an exact camera sample/view, after compositing all linked geometry:
metric_rgb = np.clip(gain[t, v] * raw_composited_rgb + bias[t, v], 0, 1)
```

The camera, radiometry and render view share a space/clock. Gaussian geometry
does not own camera postprocessing. A render view selects at most one affine
profile for the complete field set; omission means no affine transform. Profile
camera/domain mismatch is invalid rather than fitting exposure to the GT.
Source log-gain tensors can also be retained in a tensor bundle. Final clipping
follows the view's explicit `clamp` policy; metrics require `unit`.

```python
from gsv import compare_rgb, convert_rgb

# render_rgb and gt_rgb must use exactly the same camera sample and dimensions.
report = compare_rgb(render_rgb, gt_rgb, color_space="srgb", mask=valid_pixels)
print(report["psnr"], report["ssim"], report["ssim_status"])

# Convert explicitly if the renderer emits linear-sRGB and GT is encoded sRGB.
linear_gt = convert_rgb(gt_rgb, source="srgb", target="linear-srgb")
linear_report = compare_rgb(linear_render_rgb, linear_gt, color_space="linear-srgb")
```

Metrics accept RGB uint8 or float32/float64 in [0,1]. Range is 1; MSE/MAE average
all channels of selected pixels and PSNR is `10 log10(1/MSE)` (+inf for equality).
SSIM uses Gaussian11, sigma1.5, K1=.01/K2=.03, population covariance, complete
inner windows, and equal RGB channel averaging. A mask excludes whole SSIM
windows unless all their pixels are valid. Reports include counts/domain and
unavailable reasons for small images, empty masks or absent valid windows.
There is no implicit clipping, alignment, resampling or color conversion.
Positive infinite PSNR is a Python float; JSON callers must represent it
explicitly rather than writing a nonstandard Infinity literal.

## Source parameters and extensions

Use `Writer.add_array` and `Writer.add_object` for other registered encodings.
Neural recipes require `source_commit`, `architecture`, `entrypoint` and a
`configuration` dictionary. These are descriptive, never executed on file open.
Scaffold deformation also requires scaffold and association links. Provenance
records require a method and sources (path/uri plus optional digest). Keep
original parameters/native objects separate from evaluated display caches,
recording source code/checkpoint and fidelity.

Vendor encodings are safely preserved without interpretation. The reserved
`gsv.*` namespace rejects unregistered identifiers. `Scene.copy` preserves opaque
objects, extension fields and buffers while allowing storage offsets to change.
`CODECS` is a descriptive contract registry, not a callable plugin loader.
Add validators/evaluators explicitly when adding a mathematical encoding.

Writers validate before atomic publication and refuse overwriting by default;
`overwrite=True` permits replacing a file. Errors abort the temporary file.
After close or abort, mutating writer methods fail instead of accepting unsaved
changes. Array contents are never executable pickle objects. Boolean bytes are
canonical 0/1, including typed views added over other buffers. Hashes establish
integrity, not scientific accuracy or authorship.

## Examples, CLI and development

The runnable [scene example](examples/write_scene.py) includes persistent IDs
above 2^53, time-varying camera K/c2w, explicit render profile/view and synthetic reference images. Its images
are labeled `reference`, not independent measured reconstruction GT.

```sh
python packages/gsv/examples/write_scene.py /tmp/example.gsv
gsv validate /tmp/example.gsv
python -m unittest discover -s packages/gsv/tests -v
python -m pip wheel packages/gsv --no-deps --wheel-dir packages/gsv/dist
python packages/gsv/benchmarks/core.py
```

The optional CPU benchmark reports repeated integrity-audit and batched rotation
conversion timings with workload sizes and a numerical roundtrip check. Reads
may use the OS cache; these measurements are neither disk bandwidth nor display FPS.

`gsv camera-from-json cameras.json cameras.gsv` accepts a top-level `cameras`
list of `add_camera` keyword arguments plus optional `spaces`, `clocks`, and
document `metadata`. It does not add image observations automatically. Empty
camera lists are valid. The package ships under the [MIT license](LICENSE).
