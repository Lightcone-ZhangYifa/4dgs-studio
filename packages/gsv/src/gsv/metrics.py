"""Explicit-domain RGB metrics for camera-bound observations.

The inputs are aligned, full-resolution RGB arrays in the same declared color
domain. This module never resizes, registers, clips, masks or recolors inputs
implicitly. These are image agreement metrics, not reconstruction ground truth.
"""

from __future__ import annotations

import math

import numpy as np

from .container import GSVError

COLOR_SPACES = ("srgb", "linear-srgb")


def _normalized_rgb(image):
    image = np.asarray(image)
    if image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) == 0:
        raise GSVError("Metrics require nonempty RGB[H,W,3]")
    if image.dtype == np.uint8:
        return image.astype(np.float64) / 255.0
    if image.dtype.kind != "f" or image.dtype.itemsize not in (4, 8):
        raise GSVError("Metrics require uint8, float32 or float64 RGB")
    if not np.isfinite(image).all() or not np.all((image >= 0) & (image <= 1)):
        raise GSVError("Metrics RGB must be finite in [0,1]")
    return image.astype(np.float64, copy=False)


def convert_rgb(image, *, source, target):
    """Explicit sRGB transfer conversion; return normalized float64 RGB.

    Both domains use sRGB primaries/D65. There is no gamut/ICC conversion, HDR
    tone mapping, alpha compositing or automatic exposure correction.
    """
    if source not in COLOR_SPACES or target not in COLOR_SPACES:
        raise GSVError("Color domain must be srgb or linear-srgb")
    rgb = _normalized_rgb(image)
    if source == target:
        return rgb.copy()
    if source == "srgb":
        return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * rgb ** (1 / 2.4) - 0.055)


def _gaussian_valid(image, kernel):
    # Two separable convolutions avoid an H*W*11*11 temporary array.
    height, width = image.shape[:2]
    horizontal = np.zeros((height, width - 10, 3), dtype=np.float64)
    for offset, weight in enumerate(kernel):
        horizontal += weight * image[:, offset : offset + width - 10]
    result = np.zeros((height - 10, width - 10, 3), dtype=np.float64)
    for offset, weight in enumerate(kernel):
        result += weight * horizontal[offset : offset + height - 10]
    return result


def _valid_centers(mask):
    height, width = mask.shape
    horizontal = np.ones((height, width - 10), dtype=bool)
    for offset in range(11):
        horizontal &= mask[:, offset : offset + width - 10]
    result = np.ones((height - 10, width - 10), dtype=bool)
    for offset in range(11):
        result &= horizontal[offset : offset + height - 10]
    return result


def compare_rgb(prediction, reference, *, color_space, mask=None):
    """Compute MSE, MAE, PSNR and Gaussian-window SSIM in an explicit domain.

    uint8 is normalized by 255. Float inputs must be in [0,1]. MSE/MAE average
    all three channels of selected pixels. PSNR uses a fixed range of 1 and is
    +inf for exact equality. SSIM uses an 11x11 Gaussian (sigma 1.5), K1=.01,
    K2=.03, population covariance, equal channel means, and only full windows
    inside the image whose entire support passes the optional boolean mask.

    The result includes sample counts and explicit unsupported reasons. A mask
    selecting no pixels yields None metrics, never an apparently perfect score.
    The caller must ensure the exact camera/time/image-size alignment.
    """
    if color_space not in COLOR_SPACES:
        raise GSVError("Declare metric color_space as srgb or linear-srgb")
    prediction = _normalized_rgb(prediction)
    reference = _normalized_rgb(reference)
    if prediction.shape != reference.shape:
        raise GSVError("Metric image shapes differ; no implicit resampling is allowed")
    height, width = prediction.shape[:2]
    if mask is None:
        mask = np.ones((height, width), dtype=bool)
    else:
        mask = np.asarray(mask)
        if mask.dtype.kind != "b" or mask.shape != (height, width):
            raise GSVError("Metric mask requires bool[H,W]")
    count = int(mask.sum())
    result = {
        "color_space": color_space,
        "data_range": 1.0,
        "width": width,
        "height": height,
        "valid_pixels": count,
        "mask_policy": "selected-pixels; ssim-full-valid-support",
        "ssim_window": 11,
        "ssim_sigma": 1.5,
        "ssim_covariance": "population",
        "ssim_windows": 0,
        "mse": None,
        "mae": None,
        "psnr": None,
        "ssim": None,
        "status": "verified",
        "ssim_status": "verified",
    }
    if not count:
        result.update(status="no_valid_pixels", ssim_status="no_valid_pixels")
        return result
    difference = prediction[mask] - reference[mask]
    mse = float(np.mean(difference * difference))
    result.update(
        mse=mse,
        mae=float(np.mean(np.abs(difference))),
        psnr=math.inf if mse == 0 else -10 * math.log10(mse),
    )
    if min(height, width) < 11:
        result["ssim_status"] = "image_smaller_than_window"
        return result
    valid_centers = _valid_centers(mask)
    result["ssim_windows"] = int(valid_centers.sum())
    if not result["ssim_windows"]:
        result["ssim_status"] = "no_full_valid_window"
        return result
    kernel = np.exp(-(np.arange(-5, 6, dtype=np.float64) ** 2) / (2 * 1.5**2))
    kernel /= kernel.sum()
    mean_p = _gaussian_valid(prediction, kernel)
    mean_r = _gaussian_valid(reference, kernel)
    var_p = np.maximum(0, _gaussian_valid(prediction * prediction, kernel) - mean_p**2)
    var_r = np.maximum(0, _gaussian_valid(reference * reference, kernel) - mean_r**2)
    covariance = _gaussian_valid(prediction * reference, kernel) - mean_p * mean_r
    c1, c2 = 0.01**2, 0.03**2
    scores = (
        (2 * mean_p * mean_r + c1)
        * (2 * covariance + c2)
        / ((mean_p**2 + mean_r**2 + c1) * (var_p + var_r + c2))
    )
    result["ssim"] = float(np.mean(scores[valid_centers]))
    return result
