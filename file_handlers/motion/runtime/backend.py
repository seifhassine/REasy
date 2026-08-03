from __future__ import annotations

from typing import Protocol

from .joint_map import JointMapDefinition
from .model import (
    MotionBankDefinition,
    MotionMaterialController,
    MotionRuntimeScene,
)


class EntityMotionBackend(Protocol):
    """Game runtime semantics projected into the shared motion graph."""

    game_version: str

    def adapt(self, parsed: object) -> MotionRuntimeScene: ...

    def parse_motion_bank(self, data: bytes) -> MotionBankDefinition: ...

    def material_controllers(
        self,
        parsed: object,
    ) -> tuple[MotionMaterialController, ...]: ...

    def parse_joint_map(
        self,
        data: bytes,
        *,
        label: str,
    ) -> JointMapDefinition: ...
