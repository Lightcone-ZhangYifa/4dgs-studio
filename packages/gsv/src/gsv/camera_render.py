"""Explicit camera acquisition and renderer contracts, without executable defaults."""

from __future__ import annotations

import copy
import math
import numbers

import numpy as np

from .container import GSVError


def require(condition, message):
    if not condition:
        raise GSVError(message)


def fields(value, required, optional=(), *, context):
    require(isinstance(value, dict), f"{context} must be a dictionary")
    require(
        set(required) <= value.keys(),
        f"{context} requires {sorted(set(required) - value.keys())}",
    )
    require(
        value.keys() <= set(required) | set(optional), f"Unknown fields in {context}"
    )


def number(value, name, *, minimum=None, positive=False, maximum=None):
    require(
        isinstance(value, numbers.Real)
        and not isinstance(value, (bool, np.bool_))
        and math.isfinite(value),
        f"{name} must be finite numeric data",
    )
    if minimum is not None:
        require(value >= minimum, f"{name} is below its minimum")
    if positive:
        require(value > 0, f"{name} must be positive")
    if maximum is not None:
        require(value <= maximum, f"{name} exceeds its maximum")


def integer(value, name, *, minimum=0):
    require(
        isinstance(value, numbers.Integral)
        and not isinstance(value, (bool, np.bool_))
        and value >= minimum,
        f"{name} must be an integer >= {minimum}",
    )


def boolean(value, name):
    require(type(value) is bool, f"{name} must be a boolean")


def enum(value, choices, name):
    require(isinstance(value, str) and value in choices, f"Unsupported {name}")


def array_ref(data, key, array, context):
    require(
        key in data and isinstance(data[key], str), f"{context} requires array {key}"
    )
    try:
        return array(data[key])
    except KeyError as exc:
        raise GSVError(f"Unknown array {data[key]} in {context}") from exc


def _track(a, shape, name, *, positive=False, nonnegative=False):
    require(
        a.shape == shape and a.dtype.kind == "f" and a.dtype.itemsize == 8,
        f"{name} requires f64[T,V]",
    )
    require(np.isfinite(a).all(), f"{name} must be finite")
    if positive:
        require(np.all(a > 0), f"{name} must be positive")
    if nonnegative:
        require(np.all(a >= 0), f"{name} must be nonnegative")


def validate_camera_data(data, array):
    """Validate calibration and optional capture facts; return (T,V)."""
    from .codecs import _camera

    fields(
        data,
        {"times", "view_ids", "projection", "pose"},
        {"view_volume", "optics", "acquisition"},
        context="camera",
    )
    projection = data["projection"]
    pose = data["pose"]
    fields(
        projection,
        {"intrinsics", "image_sizes", "pixel_center", "distortion"},
        {"depth_mapping"},
        context="camera projection",
    )
    fields(pose, {"camera_to_world"}, context="camera pose")
    times = array_ref(data, "times", array, "camera")
    K = array_ref(projection, "intrinsics", array, "camera projection")
    sizes = array_ref(projection, "image_sizes", array, "camera projection")
    c2w = array_ref(pose, "camera_to_world", array, "camera pose")
    _camera(times, K, c2w, sizes)
    shape = K.shape[:2]
    t, v = shape
    ids = data["view_ids"]
    require(
        isinstance(ids, list)
        and len(ids) == v
        and all(isinstance(x, str) and bool(x.strip()) for x in ids)
        and len(set(ids)) == v,
        "Camera view_ids must contain V unique nonempty strings",
    )
    enum(projection["pixel_center"], ("integer", "half"), "pixel center")
    distortion = projection["distortion"]
    require(isinstance(distortion, dict), "Distortion must be a named model")
    model = distortion.get("model")
    enum(model, ("none", "opencv-brown", "opencv-fisheye"), "distortion model")
    fields(
        distortion,
        {"model"} if model == "none" else {"model", "coefficients"},
        context="distortion",
    )
    if model != "none":
        coefficients = array_ref(distortion, "coefficients", array, "distortion")
        d = 5 if model == "opencv-brown" else 4
        require(
            coefficients.shape == (t, v, d)
            and coefficients.dtype.kind == "f"
            and coefficients.dtype.itemsize == 8
            and np.isfinite(coefficients).all(),
            f"{model} distortion requires finite f64[T,V,{d}]",
        )
    if "depth_mapping" in projection:
        mapping = projection["depth_mapping"]
        fields(
            mapping,
            {"near", "far", "ndc_range", "direction"},
            context="projection depth mapping",
        )
        number(mapping["near"], "projection near", positive=True)
        if mapping["far"] is not None:
            number(mapping["far"], "projection far")
            require(mapping["far"] > mapping["near"], "Projection far must exceed near")
        enum(mapping["ndc_range"], ("zero-to-one", "minus-one-to-one"), "NDC range")
        enum(mapping["direction"], ("forward", "reversed"), "depth mapping direction")
    if "view_volume" in data:
        volume = data["view_volume"]
        fields(volume, {"near", "far", "far_unbounded"}, context="camera view volume")
        near = array_ref(volume, "near", array, "view volume")
        far = array_ref(volume, "far", array, "view volume")
        unbounded = array_ref(volume, "far_unbounded", array, "view volume")
        _track(near, shape, "View-volume near", nonnegative=True)
        _track(far, shape, "View-volume far", nonnegative=True)
        require(
            unbounded.shape == shape and unbounded.dtype.kind == "b",
            "far_unbounded requires bool[T,V]",
        )
        require(
            np.all(far[unbounded] == 0),
            "Unbounded view-volume far slots must be canonical zero",
        )
        require(
            np.all(far[~unbounded] > near[~unbounded]),
            "Finite view-volume far must exceed near",
        )
    if "optics" in data:
        optics = data["optics"]
        fields(
            optics,
            (),
            {
                "focal_length_mm",
                "sensor_width_mm",
                "sensor_height_mm",
                "f_number",
                "focus_distance",
            },
            context="camera optics",
        )
        for name in optics:
            _track(array_ref(optics, name, array, "optics"), shape, name, positive=True)
    if "acquisition" in data:
        acquisition = data["acquisition"]
        fields(
            acquisition,
            (),
            {
                "exposure_seconds",
                "iso",
                "shutter_open",
                "shutter_close",
                "rolling_readout",
                "rolling_direction",
            },
            context="camera acquisition",
        )
        require(
            ("shutter_open" in acquisition) == ("shutter_close" in acquisition),
            "Shutter open/close must be paired",
        )
        for name in acquisition:
            if name == "rolling_direction":
                continue
            _track(
                array_ref(acquisition, name, array, "acquisition"),
                shape,
                name,
                positive=name in ("exposure_seconds", "iso"),
                nonnegative=name == "rolling_readout",
            )
        if "shutter_open" in acquisition:
            require(
                np.all(
                    array(acquisition["shutter_close"])
                    >= array(acquisition["shutter_open"])
                ),
                "Shutter close precedes open",
            )
        if "rolling_direction" in acquisition:
            enum(
                acquisition["rolling_direction"],
                ("top-to-bottom", "bottom-to-top", "left-to-right", "right-to-left"),
                "rolling direction",
            )
        if "rolling_readout" in acquisition and np.any(
            array(acquisition["rolling_readout"]) > 0
        ):
            require(
                "rolling_direction" in acquisition,
                "Nonzero rolling readout requires a direction",
            )
    return shape


def validate_profile_data(data):
    fields(
        data,
        {
            "algorithm",
            "visibility",
            "projection",
            "filter",
            "support",
            "alpha",
            "compositing",
        },
        context="render profile",
    )
    enum(data["algorithm"], ("ewa-splat",), "render algorithm")
    visibility = data["visibility"]
    fields(visibility, {"center_depth", "camera_volume"}, context="visibility")
    depth = visibility["center_depth"]
    fields(
        depth,
        {"near", "far", "near_inclusive", "far_inclusive"},
        context="center depth",
    )
    number(depth["near"], "center near", minimum=0)
    if depth["far"] is not None:
        number(depth["far"], "center far")
        require(depth["far"] > depth["near"], "Center far must exceed near")
    boolean(depth["near_inclusive"], "near_inclusive")
    boolean(depth["far_inclusive"], "far_inclusive")
    enum(
        visibility["camera_volume"],
        ("ignore", "gaussian-center"),
        "camera volume application",
    )
    projection = data["projection"]
    fields(
        projection,
        {"divide_epsilon", "jacobian", "pixel_offset"},
        context="render projection",
    )
    number(projection["divide_epsilon"], "divide epsilon", minimum=0)
    jacobian = projection["jacobian"]
    fields(jacobian, {"mode", "factor"}, context="Jacobian policy")
    enum(
        jacobian["mode"],
        ("symmetric-fov", "image-bounds", "unclamped"),
        "Jacobian mode",
    )
    number(jacobian["factor"], "Jacobian factor", positive=True)
    if jacobian["mode"] == "image-bounds":
        require(
            jacobian["factor"] >= 1, "Image-bounds Jacobian factor must be at least 1"
        )
    offset = projection["pixel_offset"]
    require(
        isinstance(offset, list) and len(offset) == 2, "pixel_offset requires [x,y]"
    )
    for x in offset:
        number(x, "pixel offset")
    filt = data["filter"]
    fields(
        filt,
        {"covariance_dilation", "radius_clip", "antialias_compensation"},
        context="footprint filter",
    )
    number(filt["covariance_dilation"], "covariance dilation", minimum=0)
    number(filt["radius_clip"], "radius clip", minimum=0)
    boolean(filt["antialias_compensation"], "antialias compensation")
    support = data["support"]
    fields(
        support,
        {"bounds", "sigma", "tile_size", "mahalanobis_cutoff"},
        context="support policy",
    )
    enum(support["bounds"], ("tile-major-radius", "opacity-aware"), "support bounds")
    number(support["sigma"], "support sigma", positive=True)
    tile = support["tile_size"]
    require(
        isinstance(tile, list) and len(tile) == 2, "tile_size requires [width,height]"
    )
    for x in tile:
        integer(x, "tile dimension", minimum=1)
    if support["mahalanobis_cutoff"] is not None:
        number(support["mahalanobis_cutoff"], "Mahalanobis cutoff", minimum=0)
    alpha = data["alpha"]
    fields(alpha, {"minimum", "maximum"}, context="alpha policy")
    number(alpha["minimum"], "minimum alpha", minimum=0, maximum=1)
    number(alpha["maximum"], "maximum alpha", minimum=0, maximum=1)
    require(alpha["minimum"] <= alpha["maximum"], "Alpha minimum exceeds maximum")
    compositing = data["compositing"]
    fields(
        compositing,
        {"direction", "depth_sort", "tie_break", "transmittance"},
        context="compositing",
    )
    enum(compositing["direction"], ("front-to-back",), "compositing direction")
    enum(compositing["depth_sort"], ("tile-stable", "global-stable"), "depth sorting")
    enum(compositing["tie_break"], ("source-order",), "tie break")
    transmittance = compositing["transmittance"]
    fields(transmittance, {"threshold", "inclusive", "stage"}, context="transmittance")
    number(transmittance["threshold"], "transmittance threshold", minimum=0, maximum=1)
    boolean(transmittance["inclusive"], "transmittance inclusive")
    enum(transmittance["stage"], ("before-contribution",), "transmittance stage")


def _linked(obj, name, object_get, *, many=False):
    ref = obj.get("links", {}).get(name)
    if many:
        require(
            isinstance(ref, list)
            and all(isinstance(x, str) for x in ref)
            and len(ref) == len(set(ref)),
            f"{name} requires an ordered unique object list",
        )
        return [object_get(x) for x in ref]
    require(isinstance(ref, str), f"{name} requires one object link")
    return object_get(ref)


def validate_view(obj, object_get, array):
    data = obj["data"]
    fields(data, {"camera_view", "output", "temporal"}, context="render view")
    fields(
        obj.get("links", {}),
        {"camera", "fields", "profile"},
        {"radiometry", "observations"},
        context="render view links",
    )
    camera = _linked(obj, "camera", object_get)
    require(
        camera["encoding"] == "gsv.camera.pinhole",
        "Render view requires a pinhole camera",
    )
    t, v = validate_camera_data(camera["data"], array)
    require(
        obj.get("space") == camera.get("space")
        and obj.get("clock") == camera.get("clock"),
        "Render view must share camera space/clock",
    )
    index = data["camera_view"]
    integer(index, "camera_view")
    require(index < v, "Render camera view out of range")
    profile = _linked(obj, "profile", object_get)
    require(
        profile["encoding"] == "gsv.render.profile",
        "Render view requires a render profile",
    )
    validate_profile_data(profile["data"])
    if profile["data"]["visibility"]["camera_volume"] == "gaussian-center":
        require(
            "view_volume" in camera["data"],
            "Profile requests an absent camera view volume",
        )
    targets = _linked(obj, "fields", object_get, many=True)
    require(bool(targets), "Render view requires nonempty fields")
    for target in targets:
        require(
            target["type"] == "gaussian"
            and target["encoding"].startswith("gsv.gaussian."),
            "Render fields must use Gaussian encodings",
        )
        static_without_clock = (
            target["encoding"] == "gsv.gaussian.static" and "clock" not in target
        )
        require(
            target.get("space") == camera.get("space")
            and (static_without_clock or target.get("clock") == camera.get("clock")),
            "Render field and camera space/clock must agree",
        )
    output = data["output"]
    fields(
        output,
        {"image_size", "camera_to_output", "color_space", "background", "clamp"},
        context="render output",
    )
    size = output["image_size"]
    require(
        isinstance(size, list) and len(size) == 2,
        "Output image_size requires [width,height]",
    )
    for x in size:
        integer(x, "output dimension", minimum=1)
    transform = output["camera_to_output"]
    require(
        isinstance(transform, list)
        and len(transform) == 3
        and all(isinstance(row, list) and len(row) == 3 for row in transform),
        "camera_to_output requires a 3x3 matrix",
    )
    for row in transform:
        for x in row:
            number(x, "camera_to_output component")
    transform = np.asarray(transform, dtype=np.float64)
    require(
        np.array_equal(transform[2], [0, 0, 1])
        and transform[0, 1] == 0
        and transform[1, 0] == 0
        and transform[0, 0] > 0
        and transform[1, 1] > 0,
        "Output transform requires positive axis scales and translation",
    )
    enum(output["color_space"], ("srgb", "linear-srgb"), "output color domain")
    enum(output["clamp"], ("unit", "none"), "output clamp")
    from .codecs import RGB_GAUSSIAN_CODECS

    for target in targets:
        if target["encoding"] in RGB_GAUSSIAN_CODECS:
            require(
                target["data"].get("color_space") == output["color_space"],
                "Render field RGB and output color domains must match; no implicit conversion",
            )
    background = output["background"]
    require(
        isinstance(background, list) and len(background) == 3, "Background requires RGB"
    )
    for x in background:
        number(x, "background channel")
    temporal = data["temporal"]
    require(isinstance(temporal, dict), "Temporal policy must be a dictionary")
    enum(temporal.get("mode"), ("instantaneous", "integrated"), "temporal mode")
    if temporal["mode"] == "instantaneous":
        fields(temporal, {"mode"}, context="temporal policy")
    else:
        fields(
            temporal,
            {"mode", "shutter_open", "shutter_close", "samples", "sampling"},
            context="temporal integration",
        )
        number(temporal["shutter_open"], "shutter open")
        number(temporal["shutter_close"], "shutter close")
        require(
            temporal["shutter_close"] >= temporal["shutter_open"],
            "Integrated shutter close precedes open",
        )
        integer(temporal["samples"], "temporal sample count", minimum=1)
        enum(temporal["sampling"], ("uniform-midpoint",), "temporal sampling")
    if "radiometry" in obj.get("links", {}):
        radiometry = _linked(obj, "radiometry", object_get)
        require(
            radiometry["encoding"] == "gsv.appearance.camera-affine",
            "View radiometry requires camera-affine appearance",
        )
        require(
            radiometry.get("links", {}).get("camera") == camera["id"],
            "View radiometry must bind the exact camera",
        )
        require(
            radiometry.get("space") == obj.get("space")
            and radiometry.get("clock") == obj.get("clock"),
            "View radiometry space/clock differs",
        )
        require(
            radiometry["data"].get("color_space") == output["color_space"],
            "View/radiometry color domains differ",
        )
    if "observations" in obj.get("links", {}):
        for observation in _linked(obj, "observations", object_get, many=True):
            require(
                observation["encoding"] == "gsv.observation.camera-rgb",
                "View observations require camera RGB objects",
            )
            require(
                observation.get("links", {}).get("camera") == camera["id"],
                "Observation must bind the view camera",
            )
            require(
                observation.get("space") == obj.get("space")
                and observation.get("clock") == obj.get("clock"),
                "View observation space/clock differs",
            )
            # Observation collections can include other rig views; matching is
            # explicit at comparison time, never by nearest timestamp.


def _writer_object(out, identifier):
    obj = next((x for x in out.manifest["objects"] if x["id"] == identifier), None)
    require(obj is not None, f"Object {identifier!r} must be written first")
    return obj


def add_render_profile(
    out,
    id,
    *,
    visibility,
    projection,
    filter,
    support,
    alpha,
    compositing,
    algorithm="ewa-splat",
    metadata=None,
    label=None,
):
    """Store a complete explicit renderer algorithm; never derive paper defaults."""
    data = copy.deepcopy(
        dict(
            algorithm=algorithm,
            visibility=visibility,
            projection=projection,
            filter=filter,
            support=support,
            alpha=alpha,
            compositing=compositing,
        )
    )
    validate_profile_data(data)
    return out.add_object(
        id,
        "render",
        "gsv.render.profile",
        data,
        metadata=metadata or {},
        label=label or id,
    )


def add_render_view(
    out,
    id,
    camera_id,
    field_ids,
    profile_id,
    *,
    camera_view=0,
    image_size=None,
    camera_to_output=None,
    color_space="srgb",
    background=(0, 0, 0),
    clamp="unit",
    temporal=None,
    radiometry_id=None,
    observation_ids=None,
    metadata=None,
    label=None,
):
    """Bind a calibrated rig view to explicit fields, profile, and image output."""
    from .codecs import _writer_array

    camera = _writer_object(out, camera_id)
    require(
        camera["encoding"] == "gsv.camera.pinhole",
        "Render view requires a pinhole camera",
    )
    integer(camera_view, "camera_view")
    sizes = _writer_array(out, camera["data"]["projection"]["image_sizes"])
    require(camera_view < len(sizes), "Render camera view out of range")
    require(
        not isinstance(field_ids, str), "field_ids must be a sequence of object IDs"
    )
    links = {"camera": camera_id, "fields": list(field_ids), "profile": profile_id}
    if radiometry_id is not None:
        links["radiometry"] = radiometry_id
    if observation_ids is not None:
        require(
            not isinstance(observation_ids, str),
            "observation_ids must be a sequence of IDs",
        )
        links["observations"] = list(observation_ids)
    data = {
        "camera_view": int(camera_view),
        "output": {
            "image_size": sizes[camera_view].tolist()
            if image_size is None
            else list(image_size),
            "camera_to_output": np.eye(3).tolist()
            if camera_to_output is None
            else np.asarray(camera_to_output).tolist(),
            "color_space": color_space,
            "background": list(background),
            "clamp": clamp,
        },
        "temporal": copy.deepcopy(
            {"mode": "instantaneous"} if temporal is None else temporal
        ),
    }
    obj = {
        "id": id,
        "type": "render",
        "encoding": "gsv.render.view",
        "data": data,
        "links": links,
        "space": camera["space"],
        "clock": camera["clock"],
    }
    validate_view(
        obj, lambda key: _writer_object(out, key), lambda key: _writer_array(out, key)
    )
    return out.add_object(
        id,
        "render",
        "gsv.render.view",
        data,
        space=camera["space"],
        clock=camera["clock"],
        links=links,
        metadata=metadata or {},
        label=label or id,
    )
