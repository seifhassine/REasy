from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .math3d import transform_matrices
from .model import Rig, Vector3


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
        joints = constrained_rig.joints
        self._local_matrices = transform_matrices(constrained_rig.rest).astype(np.float32)
        owned = [index for index, owner in enumerate(self._owner_indices) if owner is not None]
        self._owned = np.asarray(owned, dtype=np.int64)
        self._owned_sources = np.asarray([self._owner_indices[index] for index in owned], dtype=np.int64)
        followers = [index for index, owner in enumerate(self._owner_indices) if owner is None]
        self._follower_roots = np.asarray(
            [index for index in followers if joints[index].parent_index is None], dtype=np.int64)
        self._follower_levels = self._levels(
            [index for index in followers if joints[index].parent_index is not None])
        self._owner_roots = owner_rig.root_indices
        self._display_roots = np.array([-1 if root is None else root for root in self._mapped_root_indices()],
                                       dtype=np.int64)

    def world_matrices(self, owner_world: np.ndarray) -> np.ndarray:
        owner_world = np.asarray(owner_world, dtype=np.float32).reshape(-1, 4, 4)
        if len(owner_world) != len(self.owner_rig.joints):
            raise ValueError("owner pose does not match its rig")
        if not np.isfinite(owner_world).all():
            raise ValueError("owner pose contains a non-finite joint matrix")

        world = np.empty((len(self.constrained_rig.joints), 4, 4), dtype=np.float32)
        world[self._owned] = owner_world[self._owned_sources] @ self._pose_to_constrained
        world[self._follower_roots] = self._local_matrices[self._follower_roots]
        for children, parents in self._follower_levels:
            world[children] = self._local_matrices[children] @ world[parents]
        return world

    def skin_matrices(
        self,
        owner_world: np.ndarray,
        root_deltas: Sequence[tuple[int, Vector3]] = (),
    ) -> np.ndarray:
        skin = self.constrained_rig.skin_matrices(self.world_matrices(owner_world)).astype(np.float32)
        for root, delta in root_deltas:
            skin[self._display_roots == int(root), 3, :3] -= (
                np.asarray(delta, dtype=np.float32) @ self._pose_to_constrained[:3, :3]
            )
        return skin

    def _levels(self, indices: list[int]) -> tuple[tuple[np.ndarray, np.ndarray], ...]:
        joints = self.constrained_rig.joints
        levels: list[tuple[np.ndarray, np.ndarray]] = []
        placed = {index for index, owner in enumerate(self._owner_indices)
                  if owner is not None or joints[index].parent_index is None}
        pending = list(indices)
        while pending:
            ready = [index for index in pending if joints[index].parent_index in placed]
            if not ready:
                raise ValueError("constrained rig hierarchy contains a cycle")
            levels.append((np.asarray(ready, dtype=np.int64),
                           np.asarray([joints[index].parent_index for index in ready], dtype=np.int64)))
            placed.update(ready)
            pending = [index for index in pending if index not in placed]
        return tuple(levels)

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
