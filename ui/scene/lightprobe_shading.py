from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .lightprobe_preview import SceneLightProbeSet


@dataclass(frozen=True, slots=True)
class ProbeBoxSnapshot:
    axes: np.ndarray
    center: np.ndarray
    extent: np.ndarray

    def fingerprint(self) -> tuple[bytes, bytes, bytes]:
        return self.axes.tobytes(), self.center.tobytes(), self.extent.tobytes()


@dataclass(frozen=True, slots=True)
class ProbeShadeLayer:
    key: str
    probe_set: SceneLightProbeSet
    boxes: tuple[ProbeBoxSnapshot, ...] = ()
    priority: int = 0
    intensity: float = 1.0

    def fingerprint(self) -> tuple:
        return (
            self.key,
            id(self.probe_set),
            int(self.priority),
            round(float(self.intensity), 6),
            tuple(box.fingerprint() for box in self.boxes),
        )


def snapshot_probe_boxes(boxes: Iterable[object]) -> tuple[ProbeBoxSnapshot, ...]:
    snapshots = []
    for box in boxes:
        try:
            axes = np.asarray(getattr(box, "axes"), dtype=np.float32).reshape(3, 3)
            center = np.asarray(getattr(box, "center"), dtype=np.float32).reshape(3)
            extent = np.asarray(getattr(box, "extent"), dtype=np.float32).reshape(3)
        except Exception:
            continue
        if np.isfinite(axes).all() and np.isfinite(center).all() and np.isfinite(extent).all():
            snapshots.append(ProbeBoxSnapshot(axes.copy(), center.copy(), extent.copy()))
    return tuple(snapshots)


def layer_order(layer: ProbeShadeLayer) -> tuple[float, float, str]:
    volumes = [float(np.prod(np.maximum(box.extent, 0.0) * 2.0)) for box in layer.boxes]
    coverage = sum(volumes) if volumes else float("inf")
    return -int(layer.priority), coverage, layer.key

