from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..mot.model import AnimationNode, Joint, Motion


Vector3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]
Matrix4 = tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Transform:
    translation: Vector3 = (0.0, 0.0, 0.0)
    rotation: Quaternion = (0.0, 0.0, 0.0, 1.0)
    scale: Vector3 = (1.0, 1.0, 1.0)


@dataclass(frozen=True, slots=True)
class RigJoint:
    name: str
    parent_index: int | None = None
    rest: Transform = Transform()
    binding_key: int | str | None = None
    inverse_bind_matrix: Matrix4 | None = None


@dataclass(frozen=True, slots=True)
class Rig:
    joints: tuple[RigJoint, ...]

    def __init__(self, joints: tuple[RigJoint, ...] | list[RigJoint]):
        object.__setattr__(self, "joints", tuple(joints))
        self._validate()

    def _validate(self) -> None:
        count = len(self.joints)
        for index, joint in enumerate(self.joints):
            parent = joint.parent_index
            if parent is not None and (parent < 0 or parent >= count or parent == index):
                raise ValueError(f"rig joint {joint.name!r} has an invalid parent index")
            values = (*joint.rest.translation, *joint.rest.rotation, *joint.rest.scale)
            if not all(math.isfinite(value) for value in values):
                raise ValueError(f"rig joint {joint.name!r} has a nonfinite rest transform")
            if sum(value * value for value in joint.rest.rotation) <= 1e-16:
                raise ValueError(f"rig joint {joint.name!r} has a zero-length rotation")
            if joint.inverse_bind_matrix is not None:
                if len(joint.inverse_bind_matrix) != 16 or not all(
                    math.isfinite(value) for value in joint.inverse_bind_matrix
                ):
                    raise ValueError(f"rig joint {joint.name!r} has an invalid inverse-bind matrix")

        state = [0] * count

        def visit(index: int) -> None:
            if state[index] == 1:
                raise ValueError("rig hierarchy contains a cycle")
            if state[index] == 2:
                return
            state[index] = 1
            parent = self.joints[index].parent_index
            if parent is not None:
                visit(parent)
            state[index] = 2

        for index in range(count):
            visit(index)

    @property
    def root_indices(self) -> tuple[int, ...]:
        roots = []
        for index, joint in enumerate(self.joints):
            root = index
            parent = joint.parent_index
            while parent is not None:
                root = parent
                parent = self.joints[parent].parent_index
            roots.append(root)
        return tuple(roots)


class DiagnosticSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class EvaluationDiagnostic:
    severity: DiagnosticSeverity
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class BoundJoint:
    rig_index: int
    motion_joint: Joint | None = None
    animation_node: AnimationNode | None = None


@dataclass(frozen=True, slots=True)
class MotionRigBinding:
    rig: Rig
    motion: Motion
    joints: tuple[BoundJoint, ...]
    diagnostics: tuple[EvaluationDiagnostic, ...] = ()

    @property
    def has_errors(self) -> bool:
        return any(item.severity is DiagnosticSeverity.ERROR for item in self.diagnostics)


@dataclass(frozen=True, slots=True)
class EvaluatedPose:
    frame: float
    local_transforms: tuple[Transform, ...]
    world_matrices: tuple[Matrix4, ...]
    skin_matrices: tuple[Matrix4 | None, ...]
    root_transforms: tuple[tuple[int, Transform], ...]
    node_weights: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class SampledLocalPose:
    frame: float
    local_transforms: tuple[Transform, ...]
    node_weights: tuple[float, ...]
