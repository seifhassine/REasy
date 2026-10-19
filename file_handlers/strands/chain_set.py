"""Under ``ResetWhenVertexLODChanged`` the engines remake rods and masses on a posed LOD change. A set's fields and
files must stay fixed for its life."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.mmtr.material_preview import MaterialPreviewError

from .chain_catalog import ConstantSources
from .chain_constants import PhysicsConstants, SetFrame
from .chain_passes import FrameState, SetShape
from .gpus import GpuStrandsPreset
from .strands_file import StrandsFile


class StrandSet:
    def __init__(self, sources: ConstantSources, layouts: Mapping, strands: StrandsFile, quantized: bool | None):
        self.constants = PhysicsConstants(sources, layouts)
        self.strands, self.quantized = strands, quantized
        self._kept = self._restart = False
        self._lod: int | None = None

    @property
    def fresh(self) -> bool:
        return not self._kept or self._restart

    def reset(self) -> None:
        """The engines' ``ResetSimulation``: rods are remade on the next frame with the solver on."""
        self._restart = True

    def frame(self, fields: Mapping[str, object], load: Callable[[str], GpuStrandsPreset | None],
              frame: SetFrame) -> tuple[Mapping[str, bytes], SetShape, FrameState]:
        values = {name: fields[field] for name, field in self.constants.sources.arguments.items()}
        if values["translation_guiding"]:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The strands are posed by translation guiding, whose programs no capture shows."))
        blocks = self.constants.frame(fields, load, self.strands, frame)
        solver = bool(values["solver"])
        if values["lod_reset"] and self._lod not in (None, frame.lod):
            self._kept = False
        state = FrameState(solver, self._kept, self._kept, self._restart, self.quantized is not None,
                           bool(self.quantized), bool(values["world_quaternion"]), bool(values["guided"]),
                           bool(values["guide_only"]), bool(values["guided_rest"]), bool(values["paused"]),
                           frame.segments)
        shape = SetShape.of(self.constants.words, solver)
        self._kept, self._restart, self._lod = solver, self._restart and not solver, frame.lod
        return blocks, shape, state


__all__ = ["StrandSet"]
