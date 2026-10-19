"""See docs/internals/strands-chain.md, LOD."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.mmtr.material_preview import MaterialPreviewError
from file_handlers.rsz.scene_joints import times

from .chain_catalog import LodSection
from .lod import (F, NO_RECORD, LodRecord, drawn_segments, extent_scaled, frustum_planes, grown_box, in_frustum,
                  lod_factor_auto, readback_box, screen_rate, seen, vertex_level, world_box)

_AUTO, _START_LEVEL, _START_HALF = 4, 3, F(100000)
_MESH_OUTPUTS = 0x1f


@dataclass(frozen=True, slots=True)
class LodView:
    """``view_projection`` is for row vectors; ``size`` is the render size."""
    view_projection: np.ndarray
    size: tuple[int, int]
    outputs: int


@dataclass(frozen=True, slots=True)
class BoundMesh:
    fields: Mapping[str, object]
    union: tuple[np.ndarray, np.ndarray]


@dataclass(frozen=True, slots=True)
class LodRun:
    """``box`` is the world-space hair box, the game's ``Strands.AABB``."""
    level: int
    box: tuple[np.ndarray, np.ndarray]
    views: tuple[tuple[F, int] | None, ...]
    factor: F
    segments: int


class SetLod:
    def __init__(self, lod: LodSection, fields: Mapping[str, object], dynamics: Mapping[str, object],
                 record: LodRecord | None, segments: Sequence[int | None],
                 rest: tuple[Sequence[float], Sequence[float]]):
        self.lod, self.record, self.segments = lod, record, tuple(segments)
        self.rest = tuple(np.asarray(side, F)[:3] for side in rest)
        self.values = {name: fields[field] for name, field in lod.fields.items()}
        self.mesh_fields = lod.mesh_fields
        self.failsafe_on = bool(dynamics[lod.dynamics_fields["failsafe"]])
        self.level = _START_LEVEL
        self.local = (np.full(3, -_START_HALF, F), np.full(3, _START_HALF, F))
        self.latch = True
        self.drawn_level: int | None = None

    def _factor(self) -> F:
        """``get_LODFactor``."""
        if self.values["lod_auto"]:
            return F(1) if self.record is None else self.record.factor
        return F(self.values["lod_factor"])

    def _level(self, box, matrix: np.ndarray | None, size: tuple[int, int] | None) -> int:
        """Without a view the rule gives level 3: size 0 makes its rate NaN."""
        if self.values["vertex_lod"] != _AUTO:
            return int(self.values["vertex_lod"])
        if matrix is None:
            matrix, size = np.eye(4, dtype=F), (0, 0)
        return vertex_level(box, matrix, size, (self.record or NO_RECORD).ratios)

    def _mesh(self, bound: BoundMesh, name: str) -> object:
        return bound.fields[self.mesh_fields[name]]

    def _failsafe(self, bound: BoundMesh) -> tuple[np.ndarray, np.ndarray]:
        if self.lod.failsafe == "union":
            return bound.union
        return grown_box(extent_scaled(bound.union, self._mesh(bound, "extent_scaling")), self.rest)

    def culled(self, bound: BoundMesh | None, views: Sequence[LodView]) -> bool:
        """The game also tests shadow views; REasy's scene has none."""
        if not (self.lod.run_cull and bound is not None and self._mesh(bound, "frustum_culling")):
            return False
        outputs = int(self._mesh(bound, "outputs")) & _MESH_OUTPUTS
        box = self._failsafe(bound)
        return not (self._mesh(bound, "draw") and any(
            view.outputs & outputs and seen(box, frustum_planes(view.view_projection)) for view in views))

    def run(self, origin: np.ndarray, views: Sequence[LodView], readback: Sequence[int] | None,
            bound: BoundMesh | None, fresh: bool, settled: bool, opacity: float) -> LodRun:
        """Call only when ``culled`` is False."""
        first = views[0] if views else None
        matrix, size = (None, None) if first is None else (first.view_projection, first.size)
        self.latch = self.latch or fresh or not settled
        use_failsafe = self.latch and bound is not None and self.failsafe_on
        box = self._failsafe(bound) if use_failsafe else world_box(self.local, origin)
        if self.lod.level == "world":
            self.level = self._level(box, matrix, size)
        level = self.level
        if self.segments[level] is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview",
                "The strands are posed at LOD {level}, which the game does not load from their file."),
                {"level": level})
        drawn = self._views(level, box, views, opacity)
        # Max as ``vmaxss`` from 0: a NaN factor leaves it.
        reached, factor = False, F(0)
        for held in drawn:
            if held is not None:
                reached, factor = True, held[0] if held[0] > factor else factor
        factor = factor if reached else self._factor()
        grown = None if readback is None else readback_box(readback)
        self.latch = grown is None
        self.local = self.rest if grown is None else tuple(np.asarray(side, F) for side in grown)
        if self.lod.level == "local":
            self.level = self._level(self.local, None if matrix is None else times(origin, matrix), size)
        self.drawn_level = level
        return LodRun(level, box, drawn, factor, drawn_segments(factor, self.segments[level]))

    def _views(self, level: int, box, views: Sequence[LodView], opacity: float) -> tuple:
        if not (opacity > 0) or not self.values["draw"]:
            return tuple(None for _ in views)
        cull = self.values["frustum_culling"] and level == self.drawn_level
        out = []
        for view in views:
            if not view.outputs & int(self.values["outputs"]) or (
                    cull and not in_frustum(box, frustum_planes(view.view_projection))):
                out.append(None)
                continue
            factor = F(self.values["lod_factor"])
            if self.values["lod_auto"]:
                size = view.size if self.values["lod_resolution"] else None
                factor = lod_factor_auto(self.record, screen_rate(box, view.view_projection), self._factor(), size)
            out.append((factor, drawn_segments(factor, self.segments[level])))
        return tuple(out)


__all__ = ["BoundMesh", "LodRun", "LodView", "SetLod"]
