#!/usr/bin/env python3
"""Analytical CPU vs production Metal projection/interpolation/compositing QA.

CPU computes world covariance by rotating basis vectors using quaternion vector
products, pushes it through the perspective Jacobian, and directly evaluates
Mahalanobis distances at pixel centers. It never uses GPU axes as ground truth.
This tests the documented approximate viewer renderer, not gsplat parity.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
NATIVE = ROOT / 'viewer/native'


def rotation(q):
    q = np.asarray(q, dtype=np.float64); q /= np.linalg.norm(q)
    vectors = np.eye(3)
    # Each row is a rotated canonical basis vector; transpose to basis columns.
    return (vectors + 2*q[0]*np.cross(q[1:], vectors) + 2*np.cross(q[1:], np.cross(q[1:], vectors))).T


def quaternion(axis, angle):
    axis = np.asarray(axis, dtype=float); axis /= np.linalg.norm(axis)
    return np.r_[np.cos(angle/2), axis*np.sin(angle/2)]


def interpolate(q0, q1, fraction):
    # Relative rotation / quaternion exponential gives constant angular speed.
    a = q0 / np.linalg.norm(q0); b = q1 / np.linalg.norm(q1)
    if a @ b < 0: b = -b
    conj = np.r_[a[0], -a[1:]]
    relative = np.r_[conj[0]*b[0] - conj[1:]@b[1:],
                     conj[0]*b[1:] + b[0]*conj[1:] + np.cross(conj[1:], b[1:])]
    angle = np.arctan2(np.linalg.norm(relative[1:]), relative[0])
    if abs(angle) < 1e-8: return a.copy()
    step = np.r_[np.cos(fraction*angle), relative[1:]/np.linalg.norm(relative[1:])*np.sin(fraction*angle)]
    return np.r_[a[0]*step[0] - a[1:]@step[1:], a[0]*step[1:] + step[0]*a[1:] + np.cross(a[1:], step[1:])]


def record(mean=(0, 0, 3), scales=(.18, .05, .04), q=(1, 0, 0, 0), alpha=.7, color=(.9, .3, .1), valid=True):
    return np.r_[mean, alpha, scales, float(valid), q, color, -1].astype(np.float32).tolist()


def case(name, a, b=None, interpolation=0, **kwargs):
    result = dict(name=name, a=a, b=b, interpolation=interpolation, worldToCamera=np.eye(4).tolist(),
                  K=[[96., 0, 32.], [0, 101., 32.], [0, 0, 1]], width=80, height=64,
                  integerPixelCenters=True, background=[0., 0., 0., 0.], pick=None)
    result.update(kwargs)
    return result


def fixtures():
    q = quaternion([.3, .7, .2], 1.07)
    camera = np.eye(4); camera[:3, :3] = rotation(quaternion([0, 1, 0], .11)); camera[:3, 3] = [-.2, .03, .1]
    anisotropic = record(mean=(-.1, .03, 3.4), scales=(.19, .045, .07), q=q, alpha=.8)
    data = [case('off_axis_rotated_covariance_skew', [anisotropic], worldToCamera=camera.tolist(),
                 K=[[94., 7.5, 29.3], [0, 101., 30.1], [0, 0, 1]], background=[.02, .03, .06, 1.])]
    near = record(mean=(0, 0, 2.5), scales=(.16, .12, .04), alpha=.5, color=(1, 0, 0))
    far = record(mean=(0, 0, 5), scales=(.32, .24, .08), alpha=.5, color=(0, 0, 1))
    data += [case('overlap_near_first', [near, far], pick=[32, 32]),
             case('overlap_far_first', [far, near], pick=[32, 32])]
    q90 = quaternion([0, 0, 1], np.pi/2)
    for name, endpoint in [('slerp_90', q90), ('slerp_90_negative_equivalent', -q90)]:
        for fraction in (0, .25, .5, 1):
            data.append(case(f'{name}_t{fraction}', [record()], [record(q=endpoint)], fraction))
    birth_a = [record(mean=(-.25, 0, 3)), record(mean=(.25, 0, 3), valid=False), record(), record(valid=False), record(alpha=0)]
    birth_b = [record(mean=(-.15, .03, 3), valid=False), record(mean=(.35, .03, 3)),
               record(mean=(.1, .05, 3.2), q=q90, scales=(.22, .07, .05), alpha=.4, color=(.2, .7, .4)),
               record(valid=False), record(alpha=0)]
    for fraction in (0, .5, 1): data.append(case(f'birth_death_t{fraction}', birth_a, birth_b, fraction))
    data += [case('near_plane_and_offscreen', [record(mean=(0, 0, .0005)), record(mean=(10, 0, 3)), record()], pick=[32, 32]),
             case('half_integer_camera_contract', [record()], integerPixelCenters=False)]
    data += [case('near_offscreen_dark_splat_cannot_blank_image',
                  [record(), record(mean=(2, .5, .02), scales=(.01, .01, .01), alpha=.99, color=(0, 0, 0))]),
             case('stabilized_skew_offcenter',
                  [record(mean=(1.2, -.4, .8), scales=(.4, .08, .2), q=q)],
                  K=[[48., 12., 9.], [0, 53., 19.], [0, 0, 1]]),
             case('stabilized_negative_skew',
                  [record(mean=(-.8, .7, .7), scales=(.35, .1, .2), q=q)],
                  K=[[48., -12., 68.], [0, 53., 41.], [0, 0, 1]]),
             case('offscreen_center_is_not_clamped',
                  [record(mean=(1.8, 0, 2), scales=(.4, .2, .1))])]
    data.append(case('circular_projected_covariance', [record(scales=(.2, .2, .2))],
                     K=[[48., 0, 32.], [0, 48., 32.], [0, 0, 1]], pick=[32, 32]))
    return data


def expected(item):
    a = np.asarray(item['a'], dtype=np.float64); b = np.asarray(item['b'] if item['b'] is not None else item['a'], dtype=np.float64)
    fraction = item['interpolation']; camera = np.asarray(item['worldToCamera']); k = np.asarray(item['K'])
    height, width = item['height'], item['width']
    y, x = np.mgrid[:height, :width]
    pixels = np.stack([x+.5, y+.5], axis=-1)
    projected = []
    for index, (first, second) in enumerate(zip(a, b)):
        state = (1-fraction)*first + fraction*second
        valid = first[7] if fraction <= 0 else second[7] if fraction >= 1 else first[7] and second[7]
        position = camera @ np.r_[state[:3], 1]
        alpha = np.clip(state[3], 0, .999)
        if not valid or position[2] <= .001 or alpha < 1/255:
            projected.append({'visible': False, 'source': index}); continue
        z = position[2]
        center = (k @ position[:3])[:2] / z + (.5 if item['integerPixelCenters'] else 0)
        orient = rotation(interpolate(first[8:12], second[8:12], fraction))
        world_cov = orient @ np.diag(state[4:7]**2) @ orient.T
        camera_cov = camera[:3, :3] @ world_cov @ camera[:3, :3].T
        # An independent inverse-K corner construction also covers camera skew.
        # Clamp derivative coordinates only; the projected mean stays physical.
        framebuffer_k = k.copy()
        if item['integerPixelCenters']: framebuffer_k[:2, 2] += .5
        corner_rays = np.array([[0, 0, 1], [width, 0, 1], [0, height, 1], [width, height, 1]]) @ np.linalg.inv(framebuffer_k).T
        low, high = corner_rays[:, :2].min(0), corner_rays[:, :2].max(0)
        derivative_ray = np.clip(position[:2]/z, low-.15*(high-low), high+.15*(high-low))
        jacobian = np.array([[k[0, 0]/z, k[0, 1]/z, -(k[0, 0]*derivative_ray[0] + k[0, 1]*derivative_ray[1])/z],
                             [0, k[1, 1]/z, -k[1, 1]*derivative_ray[1]/z]])
        covariance = jacobian @ camera_cov @ jacobian.T + np.eye(2)*.3
        bound = 3*np.sqrt(np.diag(covariance))
        if np.any(center+bound < 0) or np.any(center-bound > [width, height]):
            projected.append({'visible': False, 'source': index}); continue
        projected.append({'visible': True, 'source': index, 'center': center, 'depth': z,
                          'covariance': covariance, 'alpha': alpha, 'color': state[12:15]})
    # Evaluate the Gaussian function directly; no vertex/axes formulas copied.
    rgba = np.broadcast_to(np.asarray(item['background']), (height, width, 4)).copy()
    ordered = sorted((p for p in projected if p['visible']), key=lambda p: (-p['depth'], p['source']))
    for p in ordered:
        difference = pixels - p['center']
        radius2 = np.einsum('...i,ij,...j->...', difference, np.linalg.inv(p['covariance']), difference)
        alpha = np.minimum(.999, p['alpha']*np.exp(-radius2/2))
        alpha[(radius2 > 9) | (alpha < 1/255)] = 0
        source = np.concatenate([p['color'][None, None, :]*alpha[..., None], alpha[..., None]], axis=-1)
        rgba = source + rgba*(1-alpha[..., None])
        rgba = np.rint(np.clip(rgba, 0, 1)*255)/255  # Each blend stores BGRA8Unorm.
    indices = [p['source'] for p in ordered] + [p['source'] for p in projected if not p['visible']]
    return projected, np.rint(rgba*255).astype(np.uint8), indices


def verify(cases, actual):
    results = []
    assert len(actual["results"]) == len(cases)
    for item, gpu in zip(cases, actual['results']):
        assert item['name'] == gpu['name']
        cpu, image, indices = expected(item)
        assert gpu['sortedIndices'] == indices, (item['name'], 'sort', gpu['sortedIndices'], indices)
        assert gpu['visibleCount'] == sum(p['visible'] for p in cpu)
        max_center = max_covariance = 0.
        for p, g in zip(cpu, gpu['projected']):
            assert bool(g['visible']) == p['visible'], (item['name'], 'validity')
            if not p['visible']: continue
            observed = np.asarray(g['centerDepth'])
            axes = np.asarray(g['axes']).reshape(2, 2)
            max_center = max(max_center, float(np.max(np.abs(observed[:2] - p['center']))))
            covariance = axes.T @ axes
            max_covariance = max(max_covariance, float(np.max(np.abs(covariance - p['covariance']))))
            np.testing.assert_allclose(observed, np.r_[p['center'], p['depth'], p['alpha']], rtol=2e-5, atol=2e-4)
            np.testing.assert_allclose(covariance, p['covariance'], rtol=4e-5, atol=2e-4)
        bgra = np.frombuffer(base64.b64decode(gpu['bgra_base64']), dtype=np.uint8).reshape(item['height'], item['width'], 4)
        rgba = bgra[..., [2, 1, 0, 3]]
        error = np.abs(rgba.astype(int)-image.astype(int))
        # +/-2 quantization units allows float32 exp/interpolation and Unorm rounding.
        assert error.max() <= 2, (item['name'], 'pixel', int(error.max()), np.unravel_index(error.argmax(), error.shape))
        if item['name'].startswith('overlap_'):
            np.testing.assert_allclose(rgba[32, 32], [128, 0, 64, 191], atol=1)
            assert gpu['pickedIndex'] == (0 if item['name'] == 'overlap_near_first' else 1)
        if item['name'] == 'near_plane_and_offscreen': assert gpu['pickedIndex'] == 2
        results.append({'name': item['name'], 'count': len(cpu), 'visible': gpu['visibleCount'],
                        'maximum_center_error_px': max_center, 'maximum_covariance_error_px2': max_covariance,
                        'maximum_pixel_error_unorm8': int(error.max()), 'mean_pixel_error_unorm8': float(error.mean()),
                        'nonzero_alpha_pixels': int(np.count_nonzero(rgba[..., 3])), 'passed': True})
    # Reversing buffer order must preserve the physically ordered alpha-over result.
    assert actual['results'][1]['bgra_base64'] == actual['results'][2]['bgra_base64']
    return results


def main(report):
    cases = fixtures()
    report = Path(report); report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gsv-metal-qa-') as temp:
        temp = Path(temp)
        sources = [NATIVE/'Navigation.swift', NATIVE/'GaussianRenderer.swift', NATIVE/'TrajectoryRendering.swift', NATIVE/'CameraPicking.swift', NATIVE/'Shaders.metal', Path(__file__).with_name('MetalGeometryProbe.swift')]
        hashes = {}
        for source in sources:
            data = source.read_bytes(); (temp/source.name).write_bytes(data)
            hashes[str(source.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
        hashes[str(Path(__file__).relative_to(ROOT))] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        (temp/'input.json').write_text(json.dumps(cases, allow_nan=False))
        subprocess.run(['xcrun', 'swiftc', '-swift-version', '5', '-D', 'STUDIO_TESTING', '-O', str(temp/'GaussianRenderer.swift'),str(temp/'TrajectoryRendering.swift'),str(temp/'CameraPicking.swift'),str(temp/'Navigation.swift'), str(temp/'MetalGeometryProbe.swift'),
                        '-framework', 'AppKit', '-framework', 'MetalKit', '-framework', 'Metal', '-o', str(temp/'probe')], check=True)
        subprocess.run([str(temp/'probe'), str(temp/'input.json'), str(temp/'actual.json'), str(temp/'Shaders.metal')], check=True)
        actual = json.loads((temp/'actual.json').read_text())
        # Keep inputs and readback for reproducibility and inspection, even if a check fails.
        shutil.copyfile(temp/'input.json', report.with_suffix('.inputs.json'))
        shutil.copyfile(temp/'actual.json', report.with_suffix('.readback.json'))
        results = verify(cases, actual)
    data = {'schema': 'gsv-independent-metal-geometry-qa-v1', 'device': actual['device'], 'passed': True,
            'cases': results, 'tested_source_sha256': hashes, 'synthetic': True, 'not_research_evidence': True,
            'oracle': 'Independent float64 covariance/Jacobian/Mahalanobis CPU evaluation; quaternion relative-rotation interpolation',
            'pixel_contract': 'OpenCV +z forward/+y down, integer K -> framebuffer +0.5, BGRA8Unorm premultiplied alpha over',
            'limits': ['Validates frustum-stabilized first-order covariance +0.3pixel² / center-depth /3sigma viewer approximation.',
                       'No gsplat or other differentiable renderer parity claim; no SH evaluation.',
                       'Synthetic correctness fixtures are not model accuracy or real data research results.']}
    report.write_text(json.dumps(data, indent=2)+'\n')
    print(json.dumps({'report': str(report), 'passed': True, 'cases': len(results),
                      'max_pixel_error_unorm8': max(r['maximum_pixel_error_unorm8'] for r in results)}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', type=Path, required=True)
    main(p.parse_args().report)
