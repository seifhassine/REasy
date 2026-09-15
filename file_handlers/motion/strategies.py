from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


class SkeletonJointLike(Protocol):
    parent: object | None
    joint_map_extra_type: int


@dataclass(frozen=True, slots=True)
class SkeletonTopologyIndexStrategy:
    """Select whether the redundant child/sibling navigation index is emitted."""

    indexed_parented_joint_types: frozenset[int]

    def is_present(self, joints: Sequence[SkeletonJointLike]) -> bool:
        return any(
            joint.parent is not None
            and joint.joint_map_extra_type in self.indexed_parented_joint_types
            for joint in joints
        )


@dataclass(frozen=True, slots=True)
class MotTreeParameterTableStrategy:
    """Select table presence from node class and semantic parameter count."""

    materialized_empty_classes: frozenset[str]

    def is_present(self, class_name: str, parameter_count: int) -> bool:
        return parameter_count > 0 or class_name in self.materialized_empty_classes
