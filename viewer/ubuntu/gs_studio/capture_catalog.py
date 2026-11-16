"""Explicit camera/view/observation rows for the desktop comparison selector."""
from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class CaptureEntry:
    view_id: str
    observation_id: str | None
    time: float
    label: str


def build_capture_catalog(document):
    scene = document.scene
    result = {}
    for view in document.render_views:
        entries = []
        for identifier in view['links'].get('observations', []):
            observation = scene.object(identifier)
            data = observation['data']['samples']
            times = scene.array(data['times'])
            views = scene.array(data['view_indices'])
            for row in np.flatnonzero(views == view['data']['camera_view']):
                time = float(times[row])
                entries.append(CaptureEntry(view['id'], identifier, time,
                    f"{time:.17g} · {observation.get('label', identifier)}"))
        # Calibrated preview is a separate explicit choice even when a view
        # also links observations. A static calibration accepts field samples;
        # dynamic calibration and persistent fields must share exact timestamps.
        camera_times = scene.array(scene.object(view['links']['camera'])['data']['times'])
        constrained = [scene.array(scene.object(identifier)['data']['times'])
                       for identifier in view['links']['fields']
                       if scene.object(identifier)['encoding'] == 'gsv.gaussian.persistent-identity']
        if len(camera_times) > 1:
            constrained.append(camera_times)
        preview_times = np.array(constrained[0], copy=True) if constrained else camera_times[:1]
        for times in constrained[1:]:
            preview_times = np.intersect1d(preview_times, times)
        for time in preview_times:
            entries.append(CaptureEntry(view['id'], None, float(time), f"{float(time):.17g} · {view.get('label',view['id'])}"))
        # Preserve the manifest's explicit observation order, including equal
        # timestamps in different collections. Never merge them into one image.
        result[view['id']] = entries
    return result
