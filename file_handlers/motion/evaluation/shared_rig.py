from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .math3d import transform_matrix
from .model import Matrix4, Rig, Vector3


class SharedRigPoseMapper:
    """Project an evaluated shared pose onto a constrained mesh rig."""

    def __init__(
        self,
        owner_rig: Rig,
        constrained_rig: Rig,
        pose_to_constrained_matrix: Sequence[float] | np.ndarray,
    ):
        self.owner_rig = owner_rig
        self.constrained_rig = constrained_rig
        try:
            self._pose_to_constrained = np.asarray(
                pose_to_constrained_matrix,
                dtype=np.float32,
            ).reshape(4, 4).copy()
        except ValueError as exc:
            raise ValueError(
                "pose-to-constrained model transform must be a 4x4 matrix"
            ) from exc
        if not np.isfinite(self._pose_to_constrained).all():
            raise ValueError(
                "pose-to-constrained model transform contains a non-finite value"
            )
        owner_by_name = self._unique_joint_names(owner_rig, "owner")
        self._owner_indices = tuple(
            owner_by_name.get(joint.name) for joint in constrained_rig.joints
        )
        if not any(index is not None for index in self._owner_indices):
            raise ValueError("constrained rig has no joints in common with its owner")
        self._local_matrices = tuple(
            np.asarray(transform_matrix(joint.rest), dtype=np.float32).reshape(4, 4)
            for joint in constrained_rig.joints
        )
        self._owner_roots = owner_rig.root_indices
        self._display_roots = self._mapped_root_indices()

    @property
    def mapped_joint_count(self) -> int:
        return sum(index is not None for index in self._owner_indices)

    def world_matrices(
        self,
        owner_world_matrices: Sequence[Matrix4],
    ) -> np.ndarray:
        if len(owner_world_matrices) != len(self.owner_rig.joints):
            raise ValueError("owner pose does not match its rig")
        owner_world = np.asarray(
            owner_world_matrices,
            dtype=np.float32,
        ).reshape(-1, 4, 4)
        if not np.isfinite(owner_world).all():
            raise ValueError("owner pose contains a non-finite joint matrix")

        result: list[np.ndarray | None] = [None] * len(self.constrained_rig.joints)

        def resolve(index: int) -> np.ndarray:
            matrix = result[index]
            if matrix is not None:
                return matrix
            owner_index = self._owner_indices[index]
            if owner_index is not None:
                matrix = (
                    owner_world[owner_index] @ self._pose_to_constrained
                ).astype(np.float32)
            else:
                parent = self.constrained_rig.joints[index].parent_index
                matrix = (
                    self._local_matrices[index].copy()
                    if parent is None
                    else self._local_matrices[index] @ resolve(parent)
                )
            result[index] = matrix
            return matrix

        return np.stack(
            [resolve(index) for index in range(len(result))]
        ).astype(np.float32)

    def skin_matrices(
        self,
        owner_world_matrices: Sequence[Matrix4],
        root_deltas: Sequence[tuple[int, Vector3]] = (),
    ) -> np.ndarray:
        world = self.world_matrices(owner_world_matrices)
        matrices = []
        for index, joint in enumerate(self.constrained_rig.joints):
            if joint.inverse_bind_matrix is None:
                raise ValueError(
                    f"constrained rig joint {joint.name!r} has no inverse-bind matrix"
                )
            inverse_bind = np.asarray(
                joint.inverse_bind_matrix,
                dtype=np.float32,
            ).reshape(4, 4)
            matrices.append(inverse_bind @ world[index])
        skin = np.stack(matrices).astype(np.float32)

        deltas = {
            int(root): np.asarray(delta, dtype=np.float32)
            for root, delta in root_deltas
        }
        for index, root in enumerate(self._display_roots):
            delta = deltas.get(root) if root is not None else None
            if delta is not None:
                skin[index, 3, :3] -= (
                    delta @ self._pose_to_constrained[:3, :3]
                )
        return skin

    @staticmethod
    def _unique_joint_names(rig: Rig, label: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for index, joint in enumerate(rig.joints):
            if joint.name in result:
                raise ValueError(
                    f"{label} rig contains duplicate joint name {joint.name!r}"
                )
            result[joint.name] = index
        return result

    def _mapped_root_indices(self) -> tuple[int | None, ...]:
        result: list[int | None] = [None] * len(self.constrained_rig.joints)
        resolving: set[int] = set()

        def resolve(index: int) -> int | None:
            cached = result[index]
            if cached is not None:
                return cached
            if index in resolving:
                raise ValueError("constrained rig hierarchy contains a cycle")
            resolving.add(index)
            owner_index = self._owner_indices[index]
            if owner_index is not None:
                root = self._owner_roots[owner_index]
            else:
                parent = self.constrained_rig.joints[index].parent_index
                root = resolve(parent) if parent is not None else None
            resolving.remove(index)
            result[index] = root
            return root

        return tuple(resolve(index) for index in range(len(result)))
