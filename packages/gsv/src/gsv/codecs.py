"""Object semantic contracts and safe convenience writers."""

from __future__ import annotations

import copy

import numpy as np

from .container import DTYPES, SHA_RE, GSVError

CODECS = {
    "gsv.gaussian.persistent-identity": "Identity-Persistent Gaussian Trajectories",
    "gsv.gaussian.static": "Static anisotropic Gaussian field",
    "gsv.gaussian.spacetime-polynomial": "Cubic translation, additive quaternion motion, Gaussian temporal opacity",
    "gsv.gaussian.covariance4d": "Joint space-time Gaussian conditioned on time",
    "gsv.gaussian.se3-basis": "Canonical Gaussian field with weighted 6D rotation bases",
    "gsv.gaussian.canonical-deformation": "Canonical attributes plus an explicit external deformation recipe",
    "gsv.gaussian.scaffold-deformation": "Canonical Gaussians driven by a separately stored sparse motion scaffold",
    "gsv.camera.pinhole": "Independently timed calibrated pinhole camera rig with explicit acquisition facts",
    "gsv.render.profile": "Explicit visibility, footprint and compositing algorithms",
    "gsv.render.view": "Camera, field and render-profile binding with declared image output",
    "gsv.scaffold.graph": "Sparse moving nodes, topology and optional skinning attributes",
    "gsv.motion.se3-basis": "Discrete 6D rotation and translation motion bases",
    "gsv.motion.neural": "Safe numeric parameter archive with explicit architecture and warp recipe",
    "gsv.tracks.points": "Timed point tracks with visibility and validity",
    "gsv.tracks.image": "Camera-linked pixel tracks with explicit pixel convention",
    "gsv.transform.sampled-affine": "Independent timed affine map between named spaces",
    "gsv.binding.weights": "Explicit weighted row association between two objects",
    "gsv.geometry.points": "Static point cloud with optional attributes",
    "gsv.geometry.triangles": "Indexed triangle geometry",
    "gsv.observation.image": "Image/depth/mask arrays with calibration references",
    "gsv.observation.camera-rgb": "Camera-indexed RGB observations with explicit color domain and validity masks",
    "gsv.appearance.camera-affine": "Per-camera RGB gain and bias applied after compositing",
    "gsv.tensor.bundle": "Named safe numeric tensors without executable deserialization",
    "gsv.provenance.record": "Source assets, method, commands and evidence",
}

OBJECT_TYPES = {encoding: encoding.split(".")[1] for encoding in CODECS}
OBJECT_TYPES.update(
    {
        "gsv.geometry.points": "pointcloud",
        "gsv.geometry.triangles": "mesh",
        "gsv.observation.image": "image",
        "gsv.observation.camera-rgb": "image",
    }
)
RGB_GAUSSIAN_CODECS = frozenset(
    {
        "gsv.gaussian.persistent-identity",
        "gsv.gaussian.static",
        "gsv.gaussian.spacetime-polynomial",
        "gsv.gaussian.covariance4d",
        "gsv.gaussian.se3-basis",
    }
)


def _require(condition, message):
    if not condition:
        raise GSVError(message)


def _time(a):
    _require(
        a.ndim == 1 and a.dtype.kind == "f" and a.dtype.itemsize == 8,
        "Times require f64[T]",
    )
    _require(
        np.isfinite(a).all() and np.all(a[1:] > a[:-1]),
        "Times must be finite and strictly increasing",
    )


def _ids(a):
    _require(
        a.ndim == 1 and a.dtype.kind == "i" and a.dtype.itemsize == 8,
        "IDs require i64[N]",
    )
    _require(np.all(a[1:] > a[:-1]), "IDs must be sorted and unique")


def _frame(a, n):
    _require(
        a.shape == (n, 16) and a.dtype.kind == "f" and a.dtype.itemsize == 4,
        "Frame requires f32[N,16]",
    )
    _require(np.isfinite(a).all(), "Nonfinite Gaussian attributes")
    _require(np.all(a[:, 4:7] > 0), "Scale must be positive standard deviation")
    _require(np.all((a[:, 7] == 0) | (a[:, 7] == 1)), "Validity must be zero or one")
    _require(np.all((a[:, 3] >= 0) & (a[:, 3] <= 1)), "Opacity outside [0,1]")
    _require(
        np.all(np.abs(np.linalg.norm(a[:, 8:12], axis=1) - 1) < 1e-3),
        "Non-unit quaternion",
    )
    _require(
        np.all(((a[:, 15] >= 0) & (a[:, 15] <= 1)) | (a[:, 15] == -1)),
        "Invalid confidence",
    )


def add_persistent_gaussians(
    out,
    id,
    ids,
    times,
    frames,
    *,
    color_space="srgb",
    space="world",
    clock="scene",
    metadata=None,
    label=None,
    links=None,
):
    """Write Identity-Persistent Gaussian Trajectories with one invariant ID table.

    Row i identifies the same sequence-local Gaussian for every timestamp.
    Invalid rows preserve their identity; IDs must never be recycled. All source
    identities must be exported, or an explicitly documented subset selected.
    ``frames`` can be an iterator, so only one frame is needed in memory.
    """
    ids = np.asarray(ids)
    times = np.asarray(times, dtype="<f8")
    _ids(ids)
    _time(times)
    _require(
        color_space in ("srgb", "linear-srgb"),
        "Gaussian RGB requires an explicit color domain",
    )
    out.manifest["spaces"].setdefault(
        space, {"unit": "unitless", "handedness": "right", "axes": ["x", "y", "z"]}
    )
    out.manifest["clocks"].setdefault(clock, {"unit": "second"})
    data = {
        "ids": out.add_array(id + "/ids", ids),
        "times": out.add_array(id + "/times", times),
        "frames": [],
        "color_space": color_space,
    }
    lo = np.full(3, np.inf)
    hi = -lo
    for i, frame in enumerate(frames):
        _require(i < len(times), "More frames than timestamps")
        a = np.asarray(frame)
        _frame(a, len(ids))
        good = a[:, 7] > 0
        if good.any():
            extent = 4 * np.max(a[good, 4:7], axis=1, keepdims=True)
            lo = np.minimum(lo, np.min(a[good, :3] - extent, axis=0))
            hi = np.maximum(hi, np.max(a[good, :3] + extent, axis=0))
        data["frames"].append(out.add_array(f"{id}/frame/{i}", a))
    _require(len(data["frames"]) == len(times), "Fewer frames than timestamps")
    _require(
        (len(ids) == 0) == (len(times) == 0),
        "Empty identity-persistent fields have no identities and no frames",
    )
    data["bounds"] = (
        [lo.tolist(), hi.tolist()]
        if np.isfinite(lo).all()
        else [[-1, -1, -1], [1, 1, 1]]
    )
    return out.add_object(
        id,
        "gaussian",
        "gsv.gaussian.persistent-identity",
        data,
        space=space,
        clock=clock,
        metadata=metadata or {},
        label=label or id,
        links=links or {},
    )


def _camera(times, K, poses, sizes):
    _time(times)
    _require(len(times) > 0, "Camera requires at least one sample")
    _require(
        K.ndim == 4 and K.shape[0] == len(times) and K.shape[2:] == (3, 3),
        "Camera K requires [T,V,3,3]",
    )
    t, v = K.shape[:2]
    _require(
        v > 0 and poses.shape == (t, v, 4, 4) and sizes.shape == (v, 2),
        "Camera shapes disagree",
    )
    _require(
        K.dtype.kind == "f"
        and K.dtype.itemsize == 8
        and poses.dtype.kind == "f"
        and poses.dtype.itemsize == 8,
        "Camera matrices require f64",
    )
    _require(np.isfinite(K).all() and np.isfinite(poses).all(), "Nonfinite camera")
    _require(
        np.all(K[:, :, 0, 0] > 0)
        and np.all(K[:, :, 1, 1] > 0)
        and np.allclose(K[:, :, 2, :], [0, 0, 1], atol=1e-9, rtol=0)
        and np.allclose(K[:, :, 1, 0], 0, atol=1e-9, rtol=0),
        "Invalid upper-triangular pinhole intrinsics",
    )
    _require(
        np.allclose(poses[:, :, 3, :], [0, 0, 0, 1], atol=1e-9, rtol=0),
        "Invalid homogeneous pose",
    )
    R = poses[:, :, :3, :3]
    _require(
        np.allclose(R.swapaxes(-1, -2) @ R, np.eye(3), atol=1e-4)
        and np.allclose(np.linalg.det(R), 1, atol=1e-4),
        "Camera rotation must be proper rigid",
    )
    _require(
        sizes.dtype.kind == "i" and sizes.dtype.itemsize == 4 and np.all(sizes > 0),
        "Invalid image sizes",
    )


def add_camera(
    out,
    id,
    times,
    intrinsics,
    c2w,
    image_sizes,
    *,
    pixel_center="integer",
    space="world",
    clock="scene",
    view_ids=None,
    depth_mapping=None,
    view_volume=None,
    optics=None,
    acquisition=None,
    distortion=None,
    metadata=None,
    label=None,
):
    """Write a nested calibrated rig; optional optics never redefine K.

    Capture tracks are numerical [T,V] arrays. Projection depth mapping,
    authored view volume, and render-profile visibility are independent.
    """
    from .camera_render import require, validate_camera_data

    raw_sizes = np.asarray(image_sizes)
    _require(
        raw_sizes.dtype.kind in "ifu"
        and np.isfinite(raw_sizes).all()
        and np.all(raw_sizes == np.floor(raw_sizes))
        and np.all((raw_sizes > 0) & (raw_sizes < 2**31)),
        "Image sizes must be exact positive i32 integers",
    )
    times = np.asarray(times, dtype="<f8")
    K = np.asarray(intrinsics, dtype="<f8")
    poses = np.asarray(c2w, dtype="<f8")
    sizes = raw_sizes.astype("<i4")
    _camera(times, K, poses, sizes)
    out.manifest["spaces"].setdefault(
        space, {"unit": "unitless", "handedness": "right", "axes": ["x", "y", "z"]}
    )
    out.manifest["clocks"].setdefault(clock, {"unit": "second"})

    def put(name, value):
        return out.add_array(id + "/" + name, value)

    data = {
        "times": put("times", times),
        "view_ids": [str(i) for i in range(K.shape[1])]
        if view_ids is None
        else list(view_ids),
        "projection": {
            "intrinsics": put("intrinsics", K),
            "image_sizes": put("image_sizes", sizes),
            "pixel_center": pixel_center,
        },
        "pose": {"camera_to_world": put("camera_to_world", poses)},
    }
    require(not isinstance(view_ids, str), "view_ids must be a sequence of identifiers")
    lens = copy.deepcopy({"model": "none"} if distortion is None else distortion)
    require(isinstance(lens, dict), "Distortion must be a named dictionary")
    if "coefficients" in lens:
        lens["coefficients"] = put(
            "distortion/coefficients", np.asarray(lens["coefficients"], dtype="<f8")
        )
    data["projection"]["distortion"] = lens
    if depth_mapping is not None:
        data["projection"]["depth_mapping"] = copy.deepcopy(depth_mapping)
    for key, values in (
        ("view_volume", view_volume),
        ("optics", optics),
        ("acquisition", acquisition),
    ):
        if values is None:
            continue
        require(isinstance(values, dict), f"{key} must be a dictionary")
        data[key] = {}
        for name, value in values.items():
            if name == "rolling_direction":
                data[key][name] = value
                continue
            raw = np.asarray(value)
            if key == "view_volume" and name == "far_unbounded":
                require(raw.dtype.kind == "b", "far_unbounded must contain booleans")
                typed = raw
            else:
                typed = np.asarray(value, dtype="<f8")
            data[key][name] = put(key + "/" + name, typed)
    validate_camera_data(data, lambda key: _writer_array(out, key))
    return out.add_object(
        id,
        "camera",
        "gsv.camera.pinhole",
        data,
        space=space,
        clock=clock,
        metadata=metadata or {},
        label=label or id,
    )


def _writer_array(out, key):
    """Read a small descriptor array already emitted by this streaming writer."""
    descriptor = out.manifest["arrays"][key]
    buffer = out.manifest["buffers"][descriptor["buffer"]]
    dtype = np.dtype(DTYPES[descriptor["dtype"]])
    count = int(np.prod(descriptor["shape"], dtype=object))
    position = out._file.tell()
    try:
        out._file.seek(buffer["offset"] + descriptor.get("byte_offset", 0))
        payload = out._file.read(count * dtype.itemsize)
    finally:
        out._file.seek(position)
    return np.frombuffer(payload, dtype=dtype).reshape(descriptor["shape"])


def _rgb(image, height, width):
    _require(
        image.shape == (height, width, 3),
        "RGB observation dimensions must match its camera image size",
    )
    is_u8 = image.dtype.kind == "u" and image.dtype.itemsize == 1
    is_f32 = image.dtype.kind == "f" and image.dtype.itemsize == 4
    _require(is_u8 or is_f32, "RGB observations require u8[H,W,3] or f32[H,W,3]")
    if is_f32:
        _require(
            np.isfinite(image).all() and np.all((image >= 0) & (image <= 1)),
            "RGB observations must be finite in [0,1]",
        )


def _observation_indices(camera_samples, view_indices, n_times, n_views):
    _require(
        camera_samples.ndim == 1
        and camera_samples.dtype.kind == "i"
        and camera_samples.dtype.itemsize == 8,
        "camera_samples requires i64[M]",
    )
    _require(
        view_indices.shape == camera_samples.shape
        and view_indices.dtype.kind == "i"
        and view_indices.dtype.itemsize == 4,
        "view_indices requires i32[M]",
    )
    _require(
        np.all((camera_samples >= 0) & (camera_samples < n_times))
        and np.all((view_indices >= 0) & (view_indices < n_views)),
        "Observation camera sample or view index out of range",
    )


def _observation_times(times, camera_samples, view_indices, camera_times):
    _require(
        times.shape == camera_samples.shape
        and times.dtype.kind == "f"
        and times.dtype.itemsize == 8,
        "Observation times require f64[M]",
    )
    _require(np.isfinite(times).all(), "Observation times must be finite")
    _require(
        len(set(zip(times.tolist(), view_indices.tolist()))) == len(times),
        "Duplicate time/view observation",
    )
    if len(camera_times) > 1:
        _require(
            np.array_equal(times, camera_times[camera_samples]),
            "Dynamic camera observation time must match its exact calibration sample",
        )


def _exact_indices(values, dtype, name):
    raw = np.asarray(values)
    if raw.size == 0 and raw.ndim == 1:
        return raw.astype(dtype)
    _require(
        raw.ndim == 1 and raw.dtype.kind in "iu",
        f"{name} requires exact integer indices",
    )
    bounds = np.iinfo(dtype)
    _require(
        int(raw.min()) >= 0 and int(raw.max()) <= bounds.max,
        f"{name} index exceeds storage range",
    )
    return raw.astype(dtype)


def add_camera_rgb(
    out,
    id,
    camera_id,
    camera_samples,
    view_indices,
    images,
    *,
    color_space,
    times=None,
    role="ground_truth",
    masks=None,
    metadata=None,
    label=None,
):
    """Stream optional RGB observations bound to exact camera samples and views.

    ``images`` and optional ``masks`` may be iterators. No image is rescaled,
    recolored or interpolated. The helper inherits its camera's space and clock.
    Integer lists/arrays convert losslessly to i64/i32. Floating-point indices,
    booleans and out-of-range values are rejected rather than rounded.
    Optional times are on the shared object clock. Omission records indexed
    camera times. Static calibration permits independent observation times.
    """
    camera = next(
        (obj for obj in out.manifest["objects"] if obj["id"] == camera_id), None
    )
    _require(
        camera is not None and camera["encoding"] == "gsv.camera.pinhole",
        "RGB observations require a previously added pinhole camera",
    )
    _require(color_space in ("srgb", "linear-srgb"), "Unknown RGB color space")
    _require(
        role in ("ground_truth", "reference", "prediction"), "Unknown observation role"
    )
    samples = _exact_indices(camera_samples, np.int64, "camera_samples")
    views = _exact_indices(view_indices, np.int32, "view_indices")
    sizes = _writer_array(out, camera["data"]["projection"]["image_sizes"])
    camera_times = _writer_array(out, camera["data"]["times"])
    _observation_indices(samples, views, len(camera_times), len(sizes))
    observation_times = (
        camera_times[samples].copy()
        if times is None
        else np.asarray(times, dtype="<f8")
    )
    _observation_times(observation_times, samples, views, camera_times)
    data = {
        "samples": {
            "times": out.add_array(id + "/times", observation_times),
            "camera_samples": out.add_array(id + "/camera_samples", samples),
            "view_indices": out.add_array(id + "/view_indices", views),
        },
        "pixels": {"images": [], "color_space": color_space},
        "role": role,
    }
    mask_iterator = None if masks is None else iter(masks)
    if masks is not None:
        data["pixels"]["masks"] = []
    sentinel = object()
    for i, image in enumerate(images):
        _require(i < len(samples), "More RGB observations than indices")
        width, height = map(int, sizes[views[i]])
        image = np.asarray(image)
        _rgb(image, height, width)
        data["pixels"]["images"].append(out.add_array(f"{id}/image/{i}", image))
        if mask_iterator is not None:
            mask = next(mask_iterator, sentinel)
            _require(mask is not sentinel, "Fewer observation masks than images")
            mask = np.asarray(mask)
            _require(
                mask.shape == (height, width) and mask.dtype.kind == "b",
                "Observation mask requires bool[H,W]",
            )
            data["pixels"]["masks"].append(out.add_array(f"{id}/mask/{i}", mask))
    _require(
        len(data["pixels"]["images"]) == len(samples),
        "Fewer RGB observations than indices",
    )
    if mask_iterator is not None:
        _require(
            next(mask_iterator, sentinel) is sentinel,
            "More observation masks than images",
        )
    return out.add_object(
        id,
        "image",
        "gsv.observation.camera-rgb",
        data,
        space=camera["space"],
        clock=camera["clock"],
        links={"camera": camera_id},
        metadata=metadata or {},
        label=label or id,
    )


def _camera_affine(gain, bias, camera_shape):
    expected = tuple(camera_shape[:2]) + (3,)
    _require(
        gain.shape == expected and bias.shape == expected,
        "Camera radiometry requires gain/bias[T,V,3] matching camera axes",
    )
    _require(
        all(a.dtype.kind == "f" and a.dtype.itemsize == 4 for a in (gain, bias)),
        "Camera radiometry requires f32 gain and bias",
    )
    _require(
        np.isfinite(gain).all() and np.all(gain >= 0) and np.isfinite(bias).all(),
        "Camera gain must be finite nonnegative and bias finite",
    )


def add_camera_affine(
    out, id, camera_id, gain, bias, *, color_space, metadata=None, label=None
):
    """Store optional camera-bound affine radiometry as a separate peer object.

    Arrays are [T,V,3] on the camera's exact axes. Apply gain*composited_RGB+bias
    followed by the render view's declared clamp, never per Gaussian or to
    reference images. A render view opts in with its radiometry link. There is no
    implicit profile selection when several profiles refer to the same camera.
    """
    camera = next(
        (obj for obj in out.manifest["objects"] if obj["id"] == camera_id), None
    )
    _require(
        camera is not None and camera["encoding"] == "gsv.camera.pinhole",
        "Camera radiometry requires a previously added pinhole camera",
    )
    _require(color_space in ("srgb", "linear-srgb"), "Unknown radiometry color space")
    gain = np.asarray(gain, dtype="<f4")
    bias = np.asarray(bias, dtype="<f4")
    _camera_affine(
        gain,
        bias,
        out.manifest["arrays"][camera["data"]["projection"]["intrinsics"]]["shape"],
    )
    data = {
        "gain": out.add_array(id + "/gain", gain),
        "bias": out.add_array(id + "/bias", bias),
        "color_space": color_space,
    }
    return out.add_object(
        id,
        "appearance",
        "gsv.appearance.camera-affine",
        data,
        space=camera["space"],
        clock=camera["clock"],
        links={"camera": camera_id},
        metadata=metadata or {},
        label=label or id,
    )


def _recipe(recipe):
    _require(isinstance(recipe, dict), "Deformation recipe must be a dictionary")
    for key in ("source_commit", "architecture", "entrypoint"):
        _require(
            isinstance(recipe.get(key), str) and bool(recipe[key].strip()),
            f"Recipe requires {key}",
        )
    _require(
        isinstance(recipe.get("configuration"), dict),
        "Recipe requires explicit configuration dictionary",
    )


def validate_object(scene, obj):
    codec = obj["encoding"]
    data = obj["data"]
    array = scene.array
    _require(
        not codec.startswith("gsv.") or codec in CODECS,
        "Unregistered core encoding: " + codec,
    )
    if codec in OBJECT_TYPES:
        _require(obj["type"] == OBJECT_TYPES[codec], "Codec/type disagreement")
    if codec in RGB_GAUSSIAN_CODECS:
        _require(
            data.get("color_space") in ("srgb", "linear-srgb"),
            "Gaussian RGB requires data.color_space",
        )
    if codec in CODECS and obj["type"] == "gaussian":
        _require(
            "radiometry" not in obj.get("links", {}),
            "Camera radiometry belongs to a render view, not Gaussian geometry",
        )
    if (codec in CODECS and obj["type"] == "gaussian") or codec in (
        "gsv.camera.pinhole",
        "gsv.scaffold.graph",
        "gsv.motion.se3-basis",
    ):
        _require(
            isinstance(obj.get("space"), str),
            "Geometry needs an explicit coordinate space",
        )
        if codec != "gsv.gaussian.static":
            _require(
                isinstance(obj.get("clock"), str),
                "Timed geometry needs an explicit clock",
            )

    def get(key):
        _require(
            key in data
            and isinstance(data[key], str)
            and data[key] in scene.manifest["arrays"],
            f"Missing array {key} in {obj['id']}",
        )
        return array(data[key])

    if codec == "gsv.gaussian.persistent-identity":
        _require(obj["type"] == "gaussian", "Codec/type disagreement")
        ids = get("ids")
        times = get("times")
        _ids(ids)
        _time(times)
        _require(
            isinstance(data.get("frames"), list) and len(data["frames"]) == len(times),
            "Sample count mismatch",
        )
        _require((len(ids) == 0) == (len(times) == 0), "Empty sample shape mismatch")
        bounds = np.asarray(data.get("bounds"))
        _require(
            bounds.shape == (2, 3)
            and np.isfinite(bounds).all()
            and np.all(bounds[1] >= bounds[0]),
            "Invalid Gaussian bounds",
        )
        for key in data["frames"]:
            _require(key in scene.manifest["arrays"], "Unknown frame array")
            frame = array(key)
            _frame(frame, len(ids))
            valid = frame[:, 7] > 0
            if valid.any():
                xyz = frame[valid, :3].astype(np.float64)
                extent = 4 * frame[valid, 4:7].max(axis=1, keepdims=True).astype(
                    np.float64
                )
                tolerance = (
                    8
                    * np.finfo(np.float32).eps
                    * max(1.0, np.abs(bounds).max(), np.abs(xyz).max(), extent.max())
                )
                _require(
                    np.all(xyz - extent >= bounds[0] - tolerance)
                    and np.all(xyz + extent <= bounds[1] + tolerance),
                    "Declared bounds do not enclose valid Gaussian support",
                )
    elif codec == "gsv.camera.pinhole":
        from .camera_render import validate_camera_data

        validate_camera_data(data, array)
    elif codec == "gsv.render.profile":
        from .camera_render import validate_profile_data

        validate_profile_data(data)
    elif codec == "gsv.render.view":
        from .camera_render import validate_view

        validate_view(obj, scene.object, array)
    elif codec == "gsv.observation.camera-rgb":
        from .camera_render import array_ref, fields, validate_camera_data

        fields(data, {"samples", "pixels", "role"}, context="RGB observation")
        fields(
            data["samples"],
            {"times", "camera_samples", "view_indices"},
            context="observation samples",
        )
        fields(
            data["pixels"],
            {"images", "color_space"},
            {"masks"},
            context="observation pixels",
        )
        camera_id = obj.get("links", {}).get("camera")
        _require(isinstance(camera_id, str), "RGB observations require links.camera")
        camera = scene.object(camera_id)
        _require(
            camera["encoding"] == "gsv.camera.pinhole",
            "RGB observations require pinhole camera",
        )
        validate_camera_data(camera["data"], array)
        _require(
            obj.get("clock") == camera.get("clock")
            and obj.get("space") == camera.get("space"),
            "Observation clock/space must equal linked camera",
        )
        sizes = array(camera["data"]["projection"]["image_sizes"])
        times = array(camera["data"]["times"])
        samples = array_ref(
            data["samples"], "camera_samples", array, "observation samples"
        )
        views = array_ref(data["samples"], "view_indices", array, "observation samples")
        _observation_indices(samples, views, len(times), len(sizes))
        observation_times = array_ref(
            data["samples"], "times", array, "observation samples"
        )
        _observation_times(observation_times, samples, views, times)
        pixels = data["pixels"]
        _require(
            pixels.get("color_space") in ("srgb", "linear-srgb"),
            "Unknown RGB color space",
        )
        _require(
            data.get("role") in ("ground_truth", "reference", "prediction"),
            "Unknown observation role",
        )
        images = pixels.get("images")
        _require(
            isinstance(images, list) and len(images) == len(samples),
            "RGB observation count mismatch",
        )
        masks = pixels.get("masks")
        if masks is not None:
            _require(
                isinstance(masks, list) and len(masks) == len(samples),
                "Observation mask count mismatch",
            )
        for i, key in enumerate(images):
            _require(
                isinstance(key, str) and key in scene.manifest["arrays"],
                "Unknown RGB image array",
            )
            width, height = map(int, sizes[views[i]])
            _rgb(array(key), height, width)
            if masks is not None:
                _require(
                    isinstance(masks[i], str) and masks[i] in scene.manifest["arrays"],
                    "Unknown observation mask array",
                )
                mask = array(masks[i])
                _require(
                    mask.shape == (height, width) and mask.dtype.kind == "b",
                    "Observation mask requires bool[H,W]",
                )
    elif codec == "gsv.appearance.camera-affine":
        camera_id = obj.get("links", {}).get("camera")
        _require(isinstance(camera_id, str), "Camera radiometry requires links.camera")
        camera = scene.object(camera_id)
        _require(
            camera["encoding"] == "gsv.camera.pinhole",
            "Camera radiometry requires pinhole camera",
        )
        _require(
            obj.get("space") == camera.get("space")
            and obj.get("clock") == camera.get("clock"),
            "Radiometry clock/space must equal linked camera",
        )
        _require(
            data.get("color_space") in ("srgb", "linear-srgb"),
            "Unknown radiometry color space",
        )
        _camera_affine(
            get("gain"),
            get("bias"),
            array(camera["data"]["projection"]["intrinsics"]).shape,
        )
    elif codec in (
        "gsv.gaussian.spacetime-polynomial",
        "gsv.gaussian.covariance4d",
        "gsv.gaussian.se3-basis",
        "gsv.gaussian.static",
    ):
        from .evaluate import validate_native

        validate_native(scene, obj)
    elif codec in (
        "gsv.gaussian.canonical-deformation",
        "gsv.gaussian.scaffold-deformation",
    ):
        _require(
            isinstance(data.get("canonical"), dict) and bool(data["canonical"]),
            "Missing canonical attributes",
        )
        for key in data["canonical"].values():
            _require(
                key in scene.manifest["arrays"], "Missing canonical attribute array"
            )
        _recipe(data.get("recipe"))
        required = (
            "scaffold" if codec == "gsv.gaussian.scaffold-deformation" else "motion"
        )
        _require(required in obj.get("links", {}), f"Missing {required} object link")
        linked = scene.object(obj["links"][required])
        expected = (
            "gsv.scaffold.graph" if required == "scaffold" else "gsv.motion.neural"
        )
        _require(linked["encoding"] == expected, f"Invalid {required} encoding")
        if required == "scaffold":
            association = obj.get("links", {}).get("association")
            _require(
                isinstance(association, str),
                "Scaffold deformation requires association link",
            )
            _require(
                scene.object(association)["encoding"]
                in ("gsv.binding.weights", "gsv.tensor.bundle"),
                "Scaffold association requires weights or tensor bundle",
            )
        return {
            "id": obj["id"],
            "status": "verified_storage_requires_external_evaluator",
            "encoding": codec,
        }
    elif codec in ("gsv.scaffold.graph", "gsv.tracks.points"):
        ids = get("ids")
        times = get("times")
        positions = get("positions")
        _ids(ids)
        _time(times)
        _require(
            positions.shape == (len(times), len(ids), 3)
            and np.isfinite(positions).all(),
            "Invalid point trajectories",
        )
        for field in ("certainty", "visibility", "validity"):
            if field in data:
                flags = get(field)
                _require(
                    flags.dtype.kind == "b" and flags.shape == (len(times), len(ids)),
                    f"Invalid {field} flags",
                )
        if "quaternions" in data:
            q = get("quaternions")
            _require(
                q.shape == (len(times), len(ids), 4)
                and np.isfinite(q).all()
                and np.all(np.abs(np.linalg.norm(q, axis=-1) - 1) < 1e-3),
                "Invalid node orientations",
            )
        if "edges" in data:
            edges = get("edges")
            _require(
                edges.ndim == 2 and edges.shape[1] == 2 and edges.dtype.kind in "iu",
                "Invalid graph edges",
            )
            _require(
                np.all((edges >= 0) & (edges < len(ids))),
                "Graph edges index outside node table",
            )
    elif codec == "gsv.tracks.image":
        ids = get("ids")
        times = get("times")
        xy = get("pixels")
        _ids(ids)
        _time(times)
        _require(
            xy.shape == (len(times), len(ids), 2)
            and xy.dtype.kind == "f"
            and np.isfinite(xy).all(),
            "Invalid pixel trajectories",
        )
        _require(
            data.get("pixel_center") in ("integer", "half"),
            "Pixel tracks need an explicit pixel convention",
        )
        camera = obj.get("links", {}).get("camera")
        _require(
            isinstance(camera, str) and scene.object(camera)["type"] == "camera",
            "Pixel tracks require a camera object",
        )
        for field in ("visibility", "validity"):
            if field in data:
                flags = get(field)
                _require(
                    flags.dtype.kind == "b" and flags.shape == (len(times), len(ids)),
                    f"Invalid {field} flags",
                )
    elif codec == "gsv.transform.sampled-affine":
        times = get("times")
        matrices = get("matrices")
        _time(times)
        _require(
            len(times) > 0
            and matrices.shape == (len(times), 4, 4)
            and matrices.dtype.kind == "f"
            and matrices.dtype.itemsize == 8
            and np.isfinite(matrices).all(),
            "Transforms require finite f64[T,4,4]",
        )
        _require(
            np.allclose(matrices[:, 3, :], [0, 0, 0, 1], atol=1e-9, rtol=0)
            and np.all(np.abs(np.linalg.det(matrices[:, :3, :3])) > 1e-12),
            "Transforms must be invertible affine maps",
        )
        for field in ("source_space", "target_space"):
            _require(
                data.get(field) in scene.manifest.get("spaces", {}), f"Unknown {field}"
            )
        _require(
            data["source_space"] != data["target_space"],
            "Transform endpoints must differ",
        )
    elif codec == "gsv.binding.weights":
        indices = get("target_rows")
        weights = get("weights")
        source_ids = get("source_ids")
        _ids(source_ids)
        _require(
            indices.ndim == 2
            and indices.shape[0] == len(source_ids)
            and indices.dtype.kind in "iu"
            and weights.shape == indices.shape
            and weights.dtype.kind == "f",
            "Invalid binding shapes",
        )
        _require(
            np.isfinite(weights).all() and np.all(weights >= 0),
            "Binding weights must be finite and nonnegative",
        )
        links = obj.get("links", {})
        source = links.get("source")
        target = links.get("target")
        _require(
            isinstance(source, str) and isinstance(target, str),
            "Binding requires source and target object links",
        )
        target_ids = scene.object(target)["data"].get("ids")
        source_ref = scene.object(source)["data"].get("ids")
        _require(
            target_ids in scene.manifest["arrays"]
            and source_ref in scene.manifest["arrays"],
            "Binding endpoints require explicit ID tables",
        )
        all_source = array(source_ref)
        all_target = array(target_ids)
        _ids(all_source)
        _ids(all_target)
        _require(
            np.isin(source_ids, all_source).all()
            and np.all((indices >= 0) & (indices < len(all_target))),
            "Binding indices or identities outside endpoint table",
        )
        _require(
            data.get("normalization") in ("partition-of-unity", "unnormalized"),
            "Binding must declare normalization",
        )
        if data["normalization"] == "partition-of-unity":
            _require(
                np.allclose(weights.sum(axis=1), 1, atol=1e-5),
                "Binding weights must sum to one",
            )
    elif codec == "gsv.motion.se3-basis":
        rots = get("rotations6d")
        tr = get("translations")
        times = get("times")
        _time(times)
        _require(
            len(times) > 0
            and rots.ndim == 3
            and rots.shape[0] > 0
            and rots.shape[1:] == (len(times), 6)
            and tr.shape == rots.shape[:2] + (3,),
            "Invalid SE3 basis dimensions",
        )
        _require(
            np.isfinite(rots).all() and np.isfinite(tr).all(), "Nonfinite motion bases"
        )
    elif codec in ("gsv.motion.neural", "gsv.tensor.bundle"):
        _require(
            isinstance(data.get("tensors"), dict),
            "Tensor bundle requires named tensor references",
        )
        for key in data["tensors"].values():
            _require(key in scene.manifest["arrays"], "Missing tensor array")
        if codec == "gsv.motion.neural":
            _recipe(data.get("recipe"))
    elif codec == "gsv.geometry.points":
        positions = get("positions")
        _require(
            positions.ndim == 2
            and positions.shape[1] == 3
            and np.isfinite(positions).all(),
            "Invalid point cloud",
        )
        if "ids" in data:
            ids = get("ids")
            _ids(ids)
            _require(len(ids) == len(positions), "Point IDs/count disagree")
    elif codec == "gsv.geometry.triangles":
        vertices = get("vertices")
        faces = get("triangles")
        _require(
            vertices.ndim == 2
            and vertices.shape[1] == 3
            and np.isfinite(vertices).all(),
            "Invalid mesh vertices",
        )
        _require(
            faces.ndim == 2
            and faces.shape[1] == 3
            and faces.dtype.kind in "iu"
            and np.all((faces >= 0) & (faces < len(vertices))),
            "Invalid triangle indices",
        )
    elif codec == "gsv.observation.image":
        pixels = get("pixels")
        _require(2 <= pixels.ndim <= 4, "Image rank must be 2..4")
        _require(
            isinstance(data.get("semantic"), str) and bool(data["semantic"]),
            "Image must declare semantic (rgb/depth/mask/features)",
        )
    elif codec == "gsv.provenance.record":
        _require(
            isinstance(data.get("method"), str) and bool(data["method"].strip()),
            "Provenance requires method",
        )
        sources = data.get("sources")
        _require(isinstance(sources, list), "Provenance requires sources list")
        for source in sources:
            _require(
                isinstance(source, dict)
                and any(
                    isinstance(source.get(k), str) and source[k].strip()
                    for k in ("uri", "path")
                ),
                "Provenance source requires path or uri",
            )
            if "sha256" in source:
                _require(
                    isinstance(source["sha256"], str)
                    and bool(SHA_RE.fullmatch(source["sha256"])),
                    "Invalid provenance SHA-256",
                )
    elif codec not in CODECS:
        return {
            "id": obj["id"],
            "status": "preserved_unknown_encoding",
            "encoding": codec,
        }
    else:
        return {
            "id": obj["id"],
            "status": "envelope_verified_semantics_not_implemented",
            "encoding": codec,
        }
    return {"id": obj["id"], "status": "verified", "encoding": codec}
