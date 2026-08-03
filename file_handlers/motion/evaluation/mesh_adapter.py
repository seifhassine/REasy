from __future__ import annotations

from typing import Protocol, Sequence

from .math3d import decompose_row_srt
from .model import Matrix4, Rig, RigJoint


class ReEngineMeshLike(Protocol):
    joint_count: int
    bones: Sequence[object]
    bone_indices: Sequence[int]
    names: Sequence[str]
    local_matrices: Sequence[Sequence[float]]
    inverse_bind_matrices: Sequence[Sequence[float]]


def rig_from_re_engine_mesh(mesh: ReEngineMeshLike) -> Rig:
    """Project a parsed RE Engine mesh skeleton into the evaluation model."""
    count = int(mesh.joint_count)
    if count < 0 or len(mesh.bones) < count or len(mesh.local_matrices) < count:
        raise ValueError("mesh skeleton arrays do not cover joint_count")
    if len(mesh.bone_indices) < count:
        raise ValueError("mesh has no name index for every joint")

    joints: list[RigJoint] = []
    for index in range(count):
        name_index = int(mesh.bone_indices[index])
        if name_index < 0 or name_index >= len(mesh.names):
            raise ValueError(f"mesh joint {index} has an invalid name index")
        parent = int(getattr(mesh.bones[index], "parent_index"))
        inverse: Matrix4 | None = None
        if index < len(mesh.inverse_bind_matrices):
            values = tuple(float(value) for value in mesh.inverse_bind_matrices[index])
            if len(values) != 16:
                raise ValueError(f"mesh joint {index} has an invalid inverse-bind matrix")
            inverse = values
        name = mesh.names[name_index]
        joints.append(RigJoint(
            name=name,
            parent_index=None if parent < 0 else parent,
            rest=decompose_row_srt(mesh.local_matrices[index]),
            inverse_bind_matrix=inverse,
        ))
    return Rig(joints)
