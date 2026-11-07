from __future__ import annotations

lazy import numpy as np


def project(points: np.ndarray, clip: np.ndarray, camera, ratio) -> tuple[np.ndarray, np.ndarray]:
    """Positions in widget pixels, and whether each is in front of the camera."""
    clip = np.asarray(clip, np.float32)
    at = np.asarray(points, np.float32) @ clip[:3] + clip[3]
    w = at[:, 3]
    seen = w > camera.near * 0.5
    safe = np.where(seen, w, 1)
    width, height = camera.size
    screen = np.column_stack(((at[:, 0] / safe + 1) * 0.5 * width, (1 - at[:, 1] / safe) * 0.5 * height))
    return screen / ratio, seen


def nearest(screen: np.ndarray, seen: np.ndarray, pos, radius: float) -> int | None:
    """The seen point nearest ``pos`` within ``radius`` widget pixels."""
    distances = np.where(seen, np.linalg.norm(screen - np.asarray(pos, np.float32), axis=1), np.inf)
    at = int(np.argmin(distances)) if len(distances) else -1
    return at if at >= 0 and distances[at] <= radius else None
