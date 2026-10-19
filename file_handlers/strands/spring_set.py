"""See docs/internals/strands-chain.md."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.mmtr.material_preview import MaterialPreviewError

from .chain_catalog import ConstantSources
lazy from .lod import F, spring_box, world_box
from .spring_constants import SpringConstants, SpringFrame
from .spring_passes import SpringShape, SpringState
lazy from .strands_file import StrandsFile

_START_LEVEL, _RESTART_FRAME, _SIMULATED = 3, 3, 1
_AUTO = 4


@dataclass(frozen=True, slots=True)
class SpringRun:
    blocks: Mapping[str, bytes]
    shape: SpringShape
    state: SpringState
    level: int
    box: tuple[np.ndarray, np.ndarray]


class SpringSet:
    def __init__(self, sources: ConstantSources, layouts: Mapping, strands: StrandsFile, bound: bool):
        self.constants = SpringConstants(sources, layouts)
        self.strands, self.bound = strands, bound
        self.level, self.lod_changed = _START_LEVEL, False
        self._seen_simulation, self._counted, self._strands = 0, 0, 0
        self._rest = tuple(np.asarray(side, F)[:3] for side in (strands.bounds.min, strands.bounds.max))
        self._local = self._rest

    def frame(self, fields: Mapping[str, object], frame: SpringFrame,
              readback: Sequence[int] | None) -> SpringRun:
        """``frame``'s LOD is ignored for the posed one; ``readback`` is 24 words, None before any."""
        sources = self.constants.sources
        values = {name: fields[field] for name, field in sources.arguments.items()}
        simulation, wanted = int(values["simulation"]), int(values["vertex_lod"])
        if wanted == _AUTO:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The strands choose their LOD by the screen, which no RE4 file does."))
        guided = bool(values["guided"])
        strands = self.strands.guide_count if guided else self.strand_count
        restart = False
        if simulation == _SIMULATED:
            if self._counted == _RESTART_FRAME:
                restart, self._counted = True, _RESTART_FRAME + 1
            elif self._counted < _RESTART_FRAME:
                self._counted += 1
        restart = restart or simulation != self._seen_simulation and simulation == _SIMULATED
        restart = restart or strands != self._strands
        if not any(level.index == self.level for level in self.strands.lods):
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The strands are posed at LOD {level}, which their file does not hold."),
                {"level": self.level})
        ring = self.constants.words("boundingBoxOffset")
        posed = next(index for index, level in enumerate(self.strands.lods) if level.index == self.level)
        frame = SpringFrame(frame.origin, posed, frame.delta, frame.own_joint, frame.mesh_offsets)
        blocks = self.constants.frame(fields, self.strands, frame)
        if readback is not None:
            slot = (ring[2] - 1) & 3
            self._local = spring_box(readback[slot * 6:slot * 6 + 6], self._rest, self._local)
        box = world_box(self._local, np.asarray(frame.origin, F).reshape(4, 4))
        state = SpringState(restart, self.lod_changed, simulation == _SIMULATED, self.bound,
                            bool(values["world_quaternion"]), guided, bool(values["guide_only"]))
        shape = SpringShape.of(self.constants.words)
        level, self.lod_changed = self.level, False
        if self.level != wanted:
            self.level, self.lod_changed = wanted, True
        self._seen_simulation, self._strands = simulation, strands
        return SpringRun(blocks, shape, state, level, box)

    @property
    def strand_count(self) -> int:
        return self.strands.strand_count


__all__ = ["SpringRun", "SpringSet"]
