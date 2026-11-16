"""Calibrated, cancellable CPU reference captures, independent of GTK/OpenGL.

The interactive viewport and this quantitative renderer are deliberately separate:
navigation exposure, selection, overlays and display resolution cannot change a
score. A profile describes an algorithm, not proof of source-renderer parity.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from contextlib import nullcontext
from dataclasses import dataclass
from functools import wraps
import math
from typing import Callable

import numpy as np

from gsv import GSVError
from gsv.camera_render import validate_view
from gsv.codecs import validate_object
from gsv.evaluate import evaluate, quat_matrix
from gsv.metrics import compare_rgb


MAX_AXIS = 16_384
MAX_PIXELS = 16_777_216


@dataclass(frozen=True)
class ComparisonResult:
    render: np.ndarray
    reference: np.ndarray | None
    metrics: dict | None
    label: str
    color_space: str
    role: str = "reference"
    details: str = ""
    mask: np.ndarray | None = None


def _check_cancel(cancelled):
    if cancelled():
        raise CancelledError()


def _scene(document):
    return getattr(document, "scene", document)


def _reading(function):
    @wraps(function)
    def guarded(document, *args, **kwargs):
        # SceneDocument serializes replacement/close against all capture reads;
        # pure SDK callers keep their own descriptor lifetime as usual.
        context = document.read_lock() if hasattr(document, "read_lock") else nullcontext()
        with context:
            return function(document, *args, **kwargs)
    return guarded


def _size(width, height):
    if width <= 0 or height <= 0 or max(width, height) > MAX_AXIS or width * height > MAX_PIXELS:
        raise GSVError("Calibrated captures are limited to 16 megapixels and 16,384 pixels per axis")


def _binding(document, view_id, time, cancelled):
    scene = _scene(document)
    _check_cancel(cancelled)
    if not np.isfinite(time):
        raise GSVError("Capture time must be finite")
    view = scene.object(view_id)
    if view["encoding"] != "gsv.render.view":
        raise GSVError("Select an explicit render-view object")
    validate_view(view, scene.object, scene.array)
    camera = scene.object(view["links"]["camera"])
    data = view["data"]
    width, height = data["output"]["image_size"]
    _size(width, height)
    if camera["data"]["projection"]["distortion"]["model"] != "none":
        raise GSVError("This camera's distortion is inspectable; calibrated rendering is unsupported")
    if data["temporal"]["mode"] != "instantaneous":
        raise GSVError("Shutter integration is inspectable; calibrated rendering is unsupported")
    camera_times = np.asarray(scene.array(camera["data"]["times"]))
    if len(camera_times) == 1:
        sample = 0
    else:
        matches = np.flatnonzero(camera_times == time)
        if not len(matches):
            raise GSVError("No exact camera calibration at this time; endpoint substitution is forbidden")
        sample = int(matches[0])
    return scene, view, camera, sample


def _geometry(scene, field_ids, time, cancelled):
    packed = []
    for field_id in field_ids:
        _check_cancel(cancelled)
        obj = scene.object(field_id)
        validate_object(scene, obj)
        if obj["encoding"] == "gsv.gaussian.persistent-identity":
            times = np.asarray(scene.array(obj["data"]["times"]))
            rows = np.flatnonzero(times == time)
            if not len(rows):
                raise GSVError("No exact geometry sample at this time; interpolated or endpoint-clamped trajectories cannot be compared")
            frame = np.asarray(scene.array(obj["data"]["frames"][int(rows[0])]))
        else:
            frame = evaluate(scene, field_id, time)
        packed.append(frame[frame[:, 7] > 0.5])
    return np.concatenate(packed) if packed else np.empty((0, 16), np.float32)


def _project(frame, K, c2w, width, height, profile, volume, cancelled):
    """Return projected means, inverse covariances, radii, alpha, RGB and depth."""
    _check_cancel(cancelled)
    p = profile
    depth = p["visibility"]["center_depth"]
    near, far = depth["near"], depth["far"]
    near_inclusive, far_inclusive = depth["near_inclusive"], depth["far_inclusive"]
    if volume is not None:
        vn, vf = volume
        if vn > near:
            near, near_inclusive = vn, True
        if vf is not None and (far is None or vf < far):
            far, far_inclusive = vf, True
    R = np.asarray(c2w[:3, :3], np.float64).T
    camera = (np.asarray(frame[:, :3], np.float64) - c2w[:3, 3]) @ R.T
    z = camera[:, 2]
    valid = np.isfinite(camera).all(axis=1) & (z > 0)
    valid &= (z >= near) if near_inclusive else (z > near)
    if far is not None:
        valid &= (z <= far) if far_inclusive else (z < far)
    frame, camera, z = frame[valid], camera[valid], z[valid]
    if not len(z):
        return None
    invz = 1 / z
    inv_mean_z = 1 / (z + p["projection"]["divide_epsilon"])
    # K's principal point is affine, not divided by (z + epsilon).
    xy = camera[:, :2] @ K[:2, :2].T * inv_mean_z[:, None] + K[:2, 2]
    xy += np.asarray(p["projection"]["pixel_offset"])
    rays = camera[:, :2] * invz[:, None]
    jac = p["projection"]["jacobian"]
    f = np.array([K[0, 0], K[1, 1]])
    size = np.array([width, height])
    if jac["mode"] == "symmetric-fov":
        bound = jac["factor"] * size / (2 * f)
        rays = np.clip(rays, -bound, bound)
    elif jac["mode"] == "image-bounds":
        margin = (jac["factor"] - 1) * size / (2 * f)
        rays = np.clip(rays, -K[:2, 2] / f - margin, (size - K[:2, 2]) / f + margin)
    J = np.zeros((len(z), 2, 3), np.float64)
    J[:, 0, 0] = K[0, 0] * invz
    J[:, 0, 1] = K[0, 1] * invz
    J[:, 0, 2] = -(K[0, 0] * rays[:, 0] + K[0, 1] * rays[:, 1]) * invz
    J[:, 1, 1] = K[1, 1] * invz
    J[:, 1, 2] = -K[1, 1] * rays[:, 1] * invz
    rotations = quat_matrix(np.asarray(frame[:, 8:12], np.float64))
    basis = np.einsum("ab,nbc->nac", R, rotations) * np.asarray(frame[:, None, 4:7], np.float64)
    A = J @ basis
    cov = A @ np.swapaxes(A, 1, 2)
    det_original = np.linalg.det(cov)
    dilation = p["filter"]["covariance_dilation"]
    cov[:, 0, 0] += dilation
    cov[:, 1, 1] += dilation
    det = np.linalg.det(cov)
    valid = np.isfinite(cov).all(axis=(1, 2)) & np.isfinite(xy).all(axis=1) & (det > 0)
    xy, cov, det, frame, z, det_original = (
        a[valid] for a in (xy, cov, det, frame, z, det_original)
    )
    if not len(z):
        return None
    alpha = np.array(frame[:, 3], np.float64, copy=True)
    if p["filter"]["antialias_compensation"]:
        alpha *= np.sqrt(np.maximum(0, det_original) / det)
    eig = np.linalg.eigvalsh(cov)[:, 1]
    radius = np.ceil(p["support"]["sigma"] * np.sqrt(eig))
    valid = np.isfinite(radius) & (radius > p["filter"]["radius_clip"])
    if p["support"]["bounds"] == "tile-major-radius":
        radii = np.repeat(radius[:, None], 2, axis=1)
    else:
        minimum = p["alpha"]["minimum"]
        if minimum > 0:
            support = np.sqrt(np.maximum(0, 2 * np.log(np.maximum(alpha, minimum) / minimum)))
            support = np.minimum(p["support"]["sigma"], support)
        else:
            support = p["support"]["sigma"]
        radii = np.ceil(np.sqrt(np.stack([cov[:, 0, 0], cov[:, 1, 1]], axis=1)) * np.asarray(support)[..., None])
    valid &= (alpha > 0) & (alpha >= p["alpha"]["minimum"])
    xy, cov, radii, alpha, frame, z = (a[valid] for a in (xy, cov, radii, alpha, frame, z))
    order = np.argsort(z, kind="stable")
    _check_cancel(cancelled)
    return (xy[order], np.linalg.inv(cov)[order], radii[order], alpha[order], frame[order, 12:15])


def render_ewa(frame, K, c2w, width, height, profile, *, pixel_center="half", background=(0, 0, 0), volume=None, cancelled: Callable[[], bool] = lambda: False):
    """Render Float32 RGB with bounded row scratch space and source-order ties.

    Both declared sort modes yield the same stable order at each affected pixel:
    tile membership limits the visited pixels; center-depth/source-order sorts
    are identical within a tile. Scratch arrays are limited to 128 scanlines.
    """
    _size(width, height)
    frame, K, c2w = np.asarray(frame), np.asarray(K), np.asarray(c2w)
    _check_cancel(cancelled)
    rgb = np.zeros((height, width, 3), np.float32)
    T = np.ones((height, width), np.float32)
    stopped = np.zeros((height, width), bool)
    projected = _project(frame, K, c2w, width, height, profile, volume, cancelled)
    if projected is not None:
        means, inverse, radii, opacity, colors = projected
        tile = np.array(profile["support"]["tile_size"])
        threshold = profile["compositing"]["transmittance"]["threshold"]
        inclusive = profile["compositing"]["transmittance"]["inclusive"]
        minimum, maximum = profile["alpha"]["minimum"], profile["alpha"]["maximum"]
        cutoff = profile["support"]["mahalanobis_cutoff"]
        center = 0.5 if pixel_center == "half" else 0.0
        for i, mean in enumerate(means):
            _check_cancel(cancelled)
            lower, upper = mean - radii[i], mean + radii[i]
            # Tile-major footprints cover complete intersecting tiles, as in
            # a tiled EWA rasterizer. Opacity-aware footprints are axis-aligned.
            if profile["support"]["bounds"] == "tile-major-radius":
                lower = np.floor(lower / tile) * tile
                upper = np.ceil(upper / tile) * tile
            x0, y0 = np.clip(np.floor(lower), [0, 0], [width, height]).astype(np.int64)
            x1, y1 = np.clip(np.ceil(upper), [0, 0], [width, height]).astype(np.int64)
            if x1 <= x0 or y1 <= y0:
                continue
            dx = np.arange(x0, x1, dtype=np.float64)[None, :] + center - mean[0]
            conic = inverse[i]
            for row in range(y0, y1, 128):
                _check_cancel(cancelled)
                end = min(row + 128, y1)
                dy = np.arange(row, end, dtype=np.float64)[:, None] + center - mean[1]
                distance = conic[0, 0] * dx * dx + 2 * conic[0, 1] * dx * dy + conic[1, 1] * dy * dy
                alpha = np.minimum(maximum, opacity[i] * np.exp(-0.5 * distance)).astype(np.float32)
                transmission = T[row:end, x0:x1]
                dead = stopped[row:end, x0:x1]
                active = (~dead) & (alpha >= minimum)
                if cutoff is not None:
                    active &= distance <= cutoff * cutoff
                next_T = transmission * (1 - alpha)
                stop = active & ((next_T <= threshold) if inclusive else (next_T < threshold))
                dead |= stop
                active &= ~stop
                weight = np.where(active, transmission * alpha, 0)
                rgb[row:end, x0:x1] += weight[..., None] * colors[i]
                transmission[active] = next_T[active]
    rgb += T[..., None] * np.asarray(background, np.float32)
    _check_cancel(cancelled)
    return rgb


def _capture(scene, view, camera, sample, time, cancelled):
    frame = _geometry(scene, view["links"]["fields"], time, cancelled)
    index = view["data"]["camera_view"]
    camera_data = camera["data"]
    output = view["data"]["output"]
    width, height = output["image_size"]
    # Output transforms are supported for render-only captures. Quantitative
    # comparison requires the identity camera raster below.
    K = np.asarray(output["camera_to_output"], np.float64) @ scene.array(camera_data["projection"]["intrinsics"])[sample, index]
    pose = scene.array(camera_data["pose"]["camera_to_world"])[sample, index]
    profile = scene.object(view["links"]["profile"])["data"]
    volume = None
    if profile["visibility"]["camera_volume"] == "gaussian-center":
        v = camera_data["view_volume"]
        near = float(scene.array(v["near"])[sample, index])
        far = None if scene.array(v["far_unbounded"])[sample, index] else float(scene.array(v["far"])[sample, index])
        volume = near, far
    rgb = render_ewa(frame, K, pose, width, height, profile,
                     pixel_center=camera_data["projection"]["pixel_center"], background=output["background"],
                     volume=volume, cancelled=cancelled)
    if "radiometry" in view["links"]:
        obj = scene.object(view["links"]["radiometry"])
        validate_object(scene, obj)
        gain = scene.array(obj["data"]["gain"])[sample, index]
        bias = scene.array(obj["data"]["bias"])[sample, index]
        rgb *= gain
        rgb += bias
    if output["clamp"] == "unit":
        np.clip(rgb, 0, 1, out=rgb)
    if not np.isfinite(rgb).all():
        raise GSVError("Render exceeds finite Float32 RGB range")
    _check_cancel(cancelled)
    return rgb


@_reading
def render_view(document, view_id, time, *, cancelled=lambda: False):
    """Render an exact calibrated view without requiring a reference image."""
    scene, view, camera, sample = _binding(document, view_id, time, cancelled)
    rgb = _capture(scene, view, camera, sample, time, cancelled)
    return ComparisonResult(rgb, None, None, view.get("label", view_id), view["data"]["output"]["color_space"],
                            details="CPU EWA reference · Float32 front-to-back · stable source-order depth ties · declared profile policies. Source-renderer pixel parity is unverified.")


def _metrics(prediction, reference, color_space, mask, cancelled):
    """Use the SDK metric policy with overlapping strips, bounding SSIM memory.

    A strip owns 128 rows of window *origins* and borrows ten more rows for
    their full 11×11 support. Each SSIM window is included exactly once. Pixel
    errors use non-overlapping rows. No full-image Float64 copies are retained.
    """
    height, width = prediction.shape[:2]
    count, squared, absolute = 0, 0., 0.
    for start in range(0, height, 128):
        _check_cancel(cancelled)
        end = min(start + 128, height)
        p = np.asarray(prediction[start:end], np.float64)
        r = np.asarray(reference[start:end], np.float64)
        # compare_rgb also validates shape, domain and finite input below.
        valid = np.ones(p.shape[:2], bool) if mask is None else mask[start:end]
        difference = p[valid] - r[valid]
        count += int(valid.sum())
        squared += float(np.sum(difference * difference))
        absolute += float(np.sum(np.abs(difference)))
    result, ssim_sum, windows = None, 0., 0
    for start in range(0, max(height - 10, 1), 128):
        _check_cancel(cancelled)
        end = min(start + 138, height)
        strip = compare_rgb(prediction[start:end], reference[start:end], color_space=color_space,
                            mask=None if mask is None else mask[start:end])
        if result is None:
            result = strip.copy()
        n = strip["ssim_windows"]
        if n:
            ssim_sum += strip["ssim"] * n
            windows += n
    result.update(width=width, height=height, valid_pixels=count,
                  status="verified" if count else "no_valid_pixels",
                  ssim_windows=windows, ssim=ssim_sum / windows if windows else None,
                  mse=squared / (count * 3) if count else None,
                  mae=absolute / (count * 3) if count else None,
                  psnr=None if not count else (math.inf if squared == 0 else -10 * math.log10(squared / (count * 3))))
    result["ssim_status"] = ("no_valid_pixels" if not count else
                             "image_smaller_than_window" if min(height, width) < 11 else
                             "verified" if windows else "no_full_valid_window")
    return result


@_reading
def compare_view(document, view_id, time, *, observation_id=None, cancelled=lambda: False):
    """Capture and compare an exact linked observation, or render-only if absent.

    A view with observations but without an exact matching row is an error;
    no nearest image or calibration can quietly produce a misleading score.
    """
    scene, view, camera, sample = _binding(document, view_id, time, cancelled)
    observations = view["links"].get("observations", [])
    if observation_id is not None:
        if observation_id not in observations:
            raise GSVError("Observation is not explicitly linked by this render view")
        observations = [observation_id]
    if not observations:
        return render_view(document, view_id, time, cancelled=cancelled)
    matches = []
    view_index = view["data"]["camera_view"]
    for identifier in observations:
        obj = scene.object(identifier)
        validate_object(scene, obj)
        data = obj["data"]["samples"]
        times = scene.array(data["times"])
        views = scene.array(data["view_indices"])
        for row in np.flatnonzero((times == time) & (views == view_index)):
            matches.append((obj, int(row)))
    if not matches:
        raise GSVError("No exact observation at this time and camera view; no nearest image is substituted")
    if len(matches) > 1:
        raise GSVError("Multiple linked observations match this time; select an observation explicitly")
    observation, row = matches[0]
    pixels = observation["data"]["pixels"]
    output = view["data"]["output"]
    native_size = scene.array(camera["data"]["projection"]["image_sizes"])[view_index].tolist()
    if (output["image_size"] != native_size or
            not np.array_equal(output["camera_to_output"], np.eye(3)) or
            output["clamp"] != "unit" or output["color_space"] != pixels["color_space"]):
        raise GSVError("Comparison requires the native camera raster, identity output transform, unit clamp and matching RGB domain")
    recorded_sample = int(scene.array(observation["data"]["samples"]["camera_samples"])[row])
    if sample != recorded_sample:
        raise GSVError("Observation and capture calibration samples differ")
    _check_cancel(cancelled)
    reference = np.array(scene.array(pixels["images"][row]), copy=True)
    if reference.dtype == np.uint8:
        reference = reference.astype(np.float32) / np.float32(255)
    mask = np.array(scene.array(pixels["masks"][row]), copy=True) if "masks" in pixels else None
    render = _capture(scene, view, camera, sample, time, cancelled)
    _check_cancel(cancelled)
    metrics = _metrics(render, reference, output["color_space"], mask, cancelled)
    _check_cancel(cancelled)
    return ComparisonResult(render, reference, metrics, view.get("label", view_id), output["color_space"],
                            observation["data"]["role"],
                            "CPU EWA reference; source-renderer parity unverified. Exact camera/view/time/geometry and full native raster. Float32 compositing, declared camera affine, then output clamp. No implicit gamma conversion. PSNR range 1; SSIM 11×11 Gaussian σ=1.5, full valid mask support.", mask)
