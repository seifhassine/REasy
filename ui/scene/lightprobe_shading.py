from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np
from PySide6.QtCore import QObject, QRunnable, Signal

from .lightprobe_preview import ProbeShadingCancelled, SceneLightProbeSet


@dataclass(frozen=True, slots=True)
class ProbeBoxSnapshot:
    axes: np.ndarray
    center: np.ndarray
    extent: np.ndarray

    def fingerprint(self) -> tuple[bytes, bytes, bytes]:
        return self.axes.tobytes(), self.center.tobytes(), self.extent.tobytes()


@dataclass(frozen=True, slots=True)
class ProbeShadeInput:
    key: int
    vertices: np.ndarray
    normals: np.ndarray | None
    base_colors: np.ndarray | None

    def fingerprint(self) -> tuple:
        return (
            self.key,
            id(self.vertices),
            id(self.normals),
            id(self.base_colors),
            len(self.vertices),
        )


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


@dataclass(frozen=True, slots=True)
class ProbeShadeRequest:
    layers: tuple[ProbeShadeLayer, ...]
    inputs: tuple[ProbeShadeInput, ...]
    exposure: float
    key: tuple = field(init=False)

    def __post_init__(self) -> None:
        exposure = float(self.exposure)
        object.__setattr__(self, "exposure", exposure)
        object.__setattr__(
            self,
            "key",
            (
                tuple(layer.fingerprint() for layer in self.layers),
                exposure,
                tuple(item.fingerprint() for item in self.inputs),
            ),
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


def _matching_normals(item: ProbeShadeInput, points: np.ndarray) -> np.ndarray | None:
    if item.normals is None:
        return None
    normals = np.asarray(item.normals, dtype=np.float32).reshape(-1, 3)
    return normals if len(normals) == len(points) else None


def _shade_probe_input(
    item: ProbeShadeInput,
    ordered_layers: tuple[ProbeShadeLayer, ...],
    *,
    exposure: float,
    completed: int,
    total: int,
    progress_callback: Callable[[int, int], None] | None,
    cancel_requested: Callable[[], bool] | None,
) -> np.ndarray:
    if cancel_requested is not None and cancel_requested():
        raise ProbeShadingCancelled()
    points = np.asarray(item.vertices, dtype=np.float32).reshape(-1, 3)
    normals = _matching_normals(item, points)
    colors = np.ones((len(points), 4), dtype=np.float32)
    colors[:, :3] = 0.0
    remaining = np.ones(len(points), dtype=bool)
    shaded_count = 0

    for layer in ordered_layers:
        rows = np.flatnonzero(remaining & _points_in_boxes(points, layer.boxes, cancel_requested))
        if not len(rows):
            continue
        offset = completed + shaded_count
        layer_progress = None
        if progress_callback is not None:
            layer_progress = (
                lambda done, _count, offset=offset: progress_callback(offset + done, total)
            )
        colors[rows] = layer.probe_set.shade_vertices(
            points[rows],
            None if normals is None else normals[rows],
            exposure=float(exposure) * max(0.0, float(layer.intensity)),
            progress_callback=layer_progress,
            cancel_requested=cancel_requested,
        )
        remaining[rows] = False
        shaded_count += len(rows)
        if not np.any(remaining):
            break

    if cancel_requested is not None and cancel_requested():
        raise ProbeShadingCancelled()
    if item.base_colors is not None:
        colors[:, :3] *= np.clip(item.base_colors[:, :3], 0.0, 1.0)
    return colors


def shade_probe_layers(
    layers: tuple[ProbeShadeLayer, ...],
    inputs: tuple[ProbeShadeInput, ...],
    *,
    exposure: float,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[int, np.ndarray]:
    total = sum(len(item.vertices) for item in inputs)
    completed = 0
    results: dict[int, np.ndarray] = {}
    ordered_layers = tuple(sorted(layers, key=_layer_sort_key))
    if progress_callback is not None:
        progress_callback(0, total)

    for item in inputs:
        colors = _shade_probe_input(
            item,
            ordered_layers,
            exposure=exposure,
            completed=completed,
            total=total,
            progress_callback=progress_callback,
            cancel_requested=cancel_requested,
        )
        results[item.key] = colors.astype(np.float32, copy=False)
        completed += len(item.vertices)
        if progress_callback is not None:
            progress_callback(completed, total)

    return results


def _points_in_boxes(
    points: np.ndarray,
    boxes: tuple[ProbeBoxSnapshot, ...],
    cancel_requested: Callable[[], bool] | None,
) -> np.ndarray:
    if not boxes:
        return np.ones(len(points), dtype=bool)
    inside = np.zeros(len(points), dtype=bool)
    for box in boxes:
        if cancel_requested is not None and cancel_requested():
            raise ProbeShadingCancelled()
        local = (points - box.center) @ box.axes.T
        inside |= np.all(np.abs(local) <= (box.extent + 1e-4), axis=1)
    return inside


def _layer_sort_key(layer: ProbeShadeLayer) -> tuple[float, float, str]:
    volumes = [float(np.prod(np.maximum(box.extent, 0.0) * 2.0)) for box in layer.boxes]
    coverage = sum(volumes) if volumes else float("inf")
    return -int(layer.priority), coverage, layer.key


class _ProbeShadeSignals(QObject):
    progress = Signal(int, int, int)
    finished = Signal(int, object)
    cancelled = Signal(int)
    failed = Signal(int, str)


class ProbeShadeWorker(QRunnable):
    def __init__(
        self,
        job_id: int,
        request: ProbeShadeRequest,
    ) -> None:
        super().__init__()
        self.job_id = int(job_id)
        self.request = request
        self.signals = _ProbeShadeSignals()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        request = self.request
        try:
            results = shade_probe_layers(
                request.layers,
                request.inputs,
                exposure=request.exposure,
                progress_callback=lambda done, total: self.signals.progress.emit(
                    self.job_id,
                    done,
                    total,
                ),
                cancel_requested=self._cancel_event.is_set,
            )
        except ProbeShadingCancelled:
            self.signals.cancelled.emit(self.job_id)
            return
        except Exception as exc:
            self.signals.failed.emit(self.job_id, f"{type(exc).__name__}: {exc}")
            return
        if self._cancel_event.is_set():
            self.signals.cancelled.emit(self.job_id)
        else:
            self.signals.finished.emit(self.job_id, results)
