"""NumPy reference evaluators for explicit mathematical encodings.

These functions evaluate geometry and per-primitive RGB. Learned screen-space
appearance decoders require their own consumer and are never silently applied.
"""

from __future__ import annotations

import numpy as np

from .container import GSVError


def _check(ok, message):
    if not ok:
        raise GSVError(message)


def _time_weight(time, start, end):
    """Clamped interpolation fraction for finite, increasing f64 endpoints."""
    # Clamp before subtracting: a finite query outside the interval can be
    # farther from an endpoint than f64 can represent.
    time, start, end = float(time), float(start), float(end)
    if time <= start:
        return 0.0
    if time >= end:
        return 1.0
    span = end - start
    if np.isfinite(span):
        return (time - start) / span
    # Only a wide, zero-crossing interval can overflow here. Scaling all three
    # operands keeps the ratio finite. Preserve the ordinary path for narrow
    # intervals, including subnormals that unconditional halving would erase.
    return (time * 0.5 - start * 0.5) / (end * 0.5 - start * 0.5)


def _unit(q):
    n = np.linalg.norm(q, axis=-1, keepdims=True)
    _check(np.all(n > 1e-12), "Degenerate quaternion")
    return q / n


def quat_matrix(q):
    q = _unit(np.asarray(q))
    w, x, y, z = np.moveaxis(q, -1, 0)
    return np.stack(
        [
            1 - 2 * (y * y + z * z),
            2 * (x * y - z * w),
            2 * (x * z + y * w),
            2 * (x * y + z * w),
            1 - 2 * (x * x + z * z),
            2 * (y * z - x * w),
            2 * (x * z - y * w),
            2 * (y * z + x * w),
            1 - 2 * (x * x + y * y),
        ],
        axis=-1,
    ).reshape(q.shape[:-1] + (3, 3))


def matrix_quat(R):
    """Branch-stable matrix to wxyz quaternion, also valid near a half turn."""
    R = np.asarray(R, dtype=np.float64)
    _check(
        R.ndim >= 2 and R.shape[-2:] == (3, 3) and np.isfinite(R).all(),
        "Rotation matrices require finite [...,3,3] values",
    )
    m = R.reshape(-1, 3, 3)
    vals = np.stack(
        [
            1 + np.trace(m, axis1=1, axis2=2),
            1 + m[:, 0, 0] - m[:, 1, 1] - m[:, 2, 2],
            1 - m[:, 0, 0] + m[:, 1, 1] - m[:, 2, 2],
            1 - m[:, 0, 0] - m[:, 1, 1] + m[:, 2, 2],
        ],
        axis=1,
    )
    branch = np.argmax(vals, axis=1)
    out = np.empty((len(m), 4), dtype=np.float64)
    # Group by the stable diagonal branch, rather than executing a Python
    # iteration per Gaussian. Arithmetic and quaternion sign stay unchanged.
    for k in range(4):
        selected = branch == k
        a = m[selected]
        s = 2 * np.sqrt(np.maximum(vals[selected, k], 0))
        if k == 0:
            columns = (
                s / 4,
                (a[:, 2, 1] - a[:, 1, 2]) / s,
                (a[:, 0, 2] - a[:, 2, 0]) / s,
                (a[:, 1, 0] - a[:, 0, 1]) / s,
            )
        elif k == 1:
            columns = (
                (a[:, 2, 1] - a[:, 1, 2]) / s,
                s / 4,
                (a[:, 0, 1] + a[:, 1, 0]) / s,
                (a[:, 0, 2] + a[:, 2, 0]) / s,
            )
        elif k == 2:
            columns = (
                (a[:, 0, 2] - a[:, 2, 0]) / s,
                (a[:, 0, 1] + a[:, 1, 0]) / s,
                s / 4,
                (a[:, 1, 2] + a[:, 2, 1]) / s,
            )
        else:
            columns = (
                (a[:, 1, 0] - a[:, 0, 1]) / s,
                (a[:, 0, 2] + a[:, 2, 0]) / s,
                (a[:, 1, 2] + a[:, 2, 1]) / s,
                s / 4,
            )
        out[selected] = np.stack(columns, axis=1)
    return _unit(out.reshape(R.shape[:-2] + (4,)))


def sixd_matrix(v):
    a, b = np.split(v, 2, axis=-1)
    an = np.linalg.norm(a, axis=-1, keepdims=True)
    _check(np.all(an > 1e-10), "Degenerate first rotation basis axis")
    x = a / an
    b = b - np.sum(x * b, axis=-1, keepdims=True) * x
    bn = np.linalg.norm(b, axis=-1, keepdims=True)
    _check(np.all(bn > 1e-10), "Degenerate second rotation basis axis")
    y = b / bn
    return np.stack([x, y, np.cross(x, y)], axis=-1)


def validate_native(scene, obj):
    _check(
        obj["data"].get("color_space") in ("srgb", "linear-srgb"),
        "Native Gaussian RGB requires data.color_space",
    )
    _check(
        isinstance(obj.get("space"), str),
        "Gaussian geometry needs an explicit coordinate space",
    )
    if obj["encoding"] != "gsv.gaussian.static":
        _check(
            isinstance(obj.get("clock"), str),
            "Dynamic Gaussian geometry needs an explicit clock",
        )
    d = obj["data"]

    def array(field):
        return scene.array(d[field])

    ids = array("ids")
    n = len(ids)
    _check(
        ids.shape == (n,)
        and ids.dtype == np.dtype("<i8")
        and np.all(ids[1:] > ids[:-1]),
        "Invalid Gaussian IDs",
    )
    common = {"opacity": (n,), "colors": (n, 3)}
    codec = obj["encoding"]
    if codec == "gsv.gaussian.covariance4d":
        common.update(means=(n, 4), covariance=(n, 4, 4))
    elif codec == "gsv.gaussian.se3-basis":
        common.update(canonical_means=(n, 3), scales=(n, 3), quaternions=(n, 4))
    else:
        common.update(means=(n, 3), scales=(n, 3), quaternions=(n, 4))
    if codec == "gsv.gaussian.spacetime-polynomial":
        common.update(
            motion=(n, 3, 3), omega=(n, 4), time_centers=(n,), time_widths=(n,)
        )
    for k, shape in common.items():
        a = array(k)
        _check(
            a.shape == shape and a.dtype.kind == "f" and np.isfinite(a).all(),
            f"Invalid native array {k}",
        )
    _check(
        np.all((array("opacity") >= 0) & (array("opacity") <= 1)),
        "Invalid activated opacity",
    )
    if codec == "gsv.gaussian.covariance4d":
        c = array("covariance")
        _check(
            np.allclose(c, c.swapaxes(-1, -2), atol=1e-6)
            and np.all(np.linalg.eigvalsh(c) > 0),
            "Covariance4D must be symmetric positive definite",
        )
        _check(
            np.isfinite(d.get("temporal_prefilter", 0))
            and d.get("temporal_prefilter", 0) >= 0,
            "Invalid temporal prefilter variance",
        )
    else:
        _check(np.all(array("scales") > 0), "Nonpositive scale")
        norms = np.linalg.norm(array("quaternions"), axis=-1)
        if codec == "gsv.gaussian.spacetime-polynomial":
            _check(np.all(norms > 1e-12), "Zero base quaternion")
        else:
            _check(np.all(np.abs(norms - 1) < 1e-3), "Nonunit base quaternion")
    if codec == "gsv.gaussian.spacetime-polynomial":
        _check(np.all(array("time_widths") > 0), "Nonpositive temporal width")
    if codec == "gsv.gaussian.se3-basis":
        basis = scene.object(obj["links"]["motion"])
        _check(
            basis["encoding"] == "gsv.motion.se3-basis", "Wrong motion basis encoding"
        )
        _check(
            obj.get("space") == basis.get("space"),
            "Motion basis and canonical Gaussians must share a coordinate space; apply an explicit transform before evaluation",
        )
        from .codecs import validate_object

        validate_object(scene, basis)
        scene.convert_time(0.0, obj["clock"], basis["clock"])
        coeff = array("coefficients")
        k = scene.array(basis["data"]["rotations6d"]).shape[0]
        _check(
            coeff.shape == (n, k)
            and np.isfinite(coeff).all()
            and np.all(coeff >= 0)
            and np.allclose(coeff.sum(-1), 1, atol=1e-5),
            "Invalid activated basis coefficients",
        )


def evaluate(scene, object_id, time):
    """Return packed float32[N,16] geometry for a supported native object.

    Persistent trajectories return the nearest stored state, with earlier ties.
    Motion bases interpolate their 6D parameters linearly in their own clock.
    """
    obj = scene.object(object_id)
    d = obj["data"]

    def array(field):
        return np.asarray(scene.array(d[field]), dtype=np.float64)

    codec = obj["encoding"]
    time = float(time)
    _check(np.isfinite(time), "Time must be finite")
    if codec == "gsv.gaussian.persistent-identity":
        _check(
            d.get("color_space") in ("srgb", "linear-srgb"),
            "Persistent Gaussian RGB requires data.color_space",
        )
        times = array("times")
        _check(len(times) > 0, "Empty Gaussian object has no frame")
        right = int(np.searchsorted(times, time))
        if right == 0:
            i = 0
        elif right == len(times):
            i = right - 1
        else:
            with np.errstate(over="ignore"):
                before = time - times[right - 1]
                after = times[right] - time
            if not np.isfinite(before) or not np.isfinite(after):
                # Halving a finite clock first prevents subtraction overflow.
                before = time * 0.5 - times[right - 1] * 0.5
                after = times[right] * 0.5 - time * 0.5
            i = right - 1 if before <= after else right
        return np.array(scene.array(d["frames"][i]), copy=True)
    _check(
        codec
        in (
            "gsv.gaussian.static",
            "gsv.gaussian.covariance4d",
            "gsv.gaussian.spacetime-polynomial",
            "gsv.gaussian.se3-basis",
        ),
        f"No reference evaluator for {codec}; preserve or use its declared external evaluator",
    )
    validate_native(scene, obj)
    n = len(scene.array(d["ids"]))
    alpha = array("opacity")
    colors = array("colors")
    if codec == "gsv.gaussian.covariance4d":
        means = array("means")
        c = array("covariance")
        v = c[:, 3, 3]
        dt = time - means[:, 3]
        pos = means[:, :3] + c[:, :3, 3] * (dt / v)[:, None]
        cov = c[:, :3, :3] - c[:, :3, 3, None] * c[:, None, 3, :3] / v[:, None, None]
        eig, R = np.linalg.eigh(cov)
        _check(np.all(eig > 0), "Nonpositive conditional covariance")
        R[:, :, 0] *= np.linalg.det(R)[:, None]
        scales = np.sqrt(eig)
        q = matrix_quat(R)
        alpha = alpha * np.exp(-0.5 * dt**2 / (v + d.get("temporal_prefilter", 0)))
    elif codec == "gsv.gaussian.spacetime-polynomial":
        dt = time - array("time_centers")
        powers = np.stack([dt, dt**2, dt**3], axis=-1)
        pos = array("means") + np.einsum("npc,np->nc", array("motion"), powers)
        q = _unit(array("quaternions") + array("omega") * dt[:, None])
        scales = array("scales")
        alpha = alpha * np.exp(-((dt / array("time_widths")) ** 2))
    elif codec == "gsv.gaussian.se3-basis":
        motion = scene.object(obj["links"]["motion"])
        basis = motion["data"]
        times = np.asarray(scene.array(basis["times"]))
        source_clock = obj.get("clock")
        target_clock = motion.get("clock")
        if source_clock != target_clock:
            _check(
                source_clock is not None and target_clock is not None,
                "Motion and field need explicit aligned clocks",
            )
            time = scene.convert_time(time, source_clock, target_clock)
        t1 = min(int(np.searchsorted(times, time)), len(times) - 1)
        t0 = max(t1 - 1, 0)
        weight = _time_weight(time, times[t0], times[t1]) if t0 != t1 else 0.0

        def interpolate(field):
            # Promote only the bounding samples. A scalar f64 weight can be
            # narrowed to f32 by NumPy 1.x when multiplied with an f32 array.
            values = scene.array(basis[field])
            left = np.asarray(values[:, t0], dtype=np.float64)
            right = np.asarray(values[:, t1], dtype=np.float64)
            return (1 - weight) * left + weight * right

        coefficients = array("coefficients")
        R = sixd_matrix(coefficients @ interpolate("rotations6d"))
        p = coefficients @ interpolate("translations")
        pos = np.einsum("nij,nj->ni", R, array("canonical_means")) + p
        q = matrix_quat(R @ quat_matrix(array("quaternions")))
        scales = array("scales")
    elif codec == "gsv.gaussian.static":
        pos = array("means")
        q = array("quaternions")
        scales = array("scales")
    else:
        raise GSVError(f"No reference evaluator for {codec}")
    # Native encodings may retain f64 parameters, whereas this convenience API
    # returns packed f32. Reject unrepresentable geometry instead of silently
    # producing infinite centers or zero-sized ellipsoids after conversion.
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        out = np.zeros((n, 16), dtype="<f4")
        out[:, :3] = pos
        out[:, 3] = alpha
        out[:, 4:7] = scales
        out[:, 7] = 1
        out[:, 8:12] = q
        out[:, 12:15] = colors
        out[:, 15] = -1
    from .codecs import _frame

    _frame(out, n)
    return out
