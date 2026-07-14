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


@dataclass(frozen=True, slots=True)
class ProbeShadeRequest:
    probe_set: SceneLightProbeSet
    inputs: tuple[ProbeShadeInput, ...]
    exposure: float
    boxes: tuple[ProbeBoxSnapshot, ...] = ()
    key: tuple = field(init=False)

    def __post_init__(self) -> None:
        exposure = float(self.exposure)
        object.__setattr__(self, "exposure", exposure)
        object.__setattr__(
            self,
            "key",
            (
                id(self.probe_set),
                exposure,
                tuple(
                    (
                        item.key,
                        id(item.vertices),
                        id(item.normals),
                        id(item.base_colors),
                        len(item.vertices),
                    )
                    for item in self.inputs
                ),
                tuple(box.fingerprint() for box in self.boxes),
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


def shade_probe_inputs(
    probe_set: SceneLightProbeSet,
    inputs: tuple[ProbeShadeInput, ...],
    *,
    exposure: float,
    boxes: tuple[ProbeBoxSnapshot, ...] = (),
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[int, np.ndarray]:
    total = sum(len(item.vertices) for item in inputs)
    completed = 0
    results: dict[int, np.ndarray] = {}
    if progress_callback is not None:
        progress_callback(0, total)

    for item in inputs:
        if cancel_requested is not None and cancel_requested():
            raise ProbeShadingCancelled()
        offset = completed
        colors = probe_set.shade_vertices(
            item.vertices,
            item.normals,
            exposure=exposure,
            progress_callback=(
                (lambda done, _count, offset=offset: progress_callback(offset + done, total))
                if progress_callback is not None
                else None
            ),
            cancel_requested=cancel_requested,
        )
        if cancel_requested is not None and cancel_requested():
            raise ProbeShadingCancelled()
        _apply_probe_display_modulation(
            colors,
            item,
            boxes,
            cancel_requested=cancel_requested,
        )
        results[item.key] = colors.astype(np.float32, copy=False)
        completed += len(item.vertices)

    if progress_callback is not None:
        progress_callback(total, total)
    return results


def _apply_probe_display_modulation(
    colors: np.ndarray,
    item: ProbeShadeInput,
    boxes: tuple[ProbeBoxSnapshot, ...],
    *,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    if boxes:
        points = np.asarray(item.vertices, dtype=np.float32).reshape(-1, 3)
        inside = np.zeros(len(points), dtype=bool)
        for box in boxes:
            if cancel_requested is not None and cancel_requested():
                raise ProbeShadingCancelled()
            local = (points - box.center) @ box.axes.T
            inside |= np.all(np.abs(local) <= (box.extent + 1e-4), axis=1)
        colors[~inside, :3] = 0.0
    if cancel_requested is not None and cancel_requested():
        raise ProbeShadingCancelled()
    if item.base_colors is not None:
        colors[:, :3] *= np.clip(item.base_colors[:, :3], 0.0, 1.0)


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
            results = shade_probe_inputs(
                request.probe_set,
                request.inputs,
                exposure=request.exposure,
                boxes=request.boxes,
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
