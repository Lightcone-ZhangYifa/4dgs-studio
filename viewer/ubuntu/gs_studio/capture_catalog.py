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
        # Preserve the manifest's explicit observation order, including equal
        # timestamps in different collections. Never merge them into one image.
        result[view['id']] = entries
    return result
