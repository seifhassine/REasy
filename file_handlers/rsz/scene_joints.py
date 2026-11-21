"""See docs/internals/strands-rendering.md, Transforms and joints."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

lazy from file_handlers.engine_math import make_trs_matrix, transform_product
lazy from file_handlers.skeleton.skeleton_file import read_skeleton
lazy from utils.hash_util import murmur3_hash_utf16le

F = np.float32


def trs(position: Sequence[float], rotation: Sequence[float], scale: Sequence[float]) -> np.ndarray:
    """``make_trs_matrix`` as row vectors."""
    return make_trs_matrix(position, rotation, scale).T.copy()


@dataclass(frozen=True, slots=True, eq=False)
class JointSet:
    names: tuple[str, ...]
    parents: tuple[int, ...]
    locals: np.ndarray
    symmetry: tuple[int, ...] = ()
    _hashes: dict[int, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        hashes = {}
        for index, name in enumerate(self.names):
            hashes.setdefault(murmur3_hash_utf16le(name), index)
        object.__setattr__(self, "_hashes", hashes)

    def index(self, name: str) -> int | None:
        return self._hashes.get(murmur3_hash_utf16le(name)) if name else None

    def hashed(self, key: int) -> int | None:
        return self._hashes.get(key)

    def posed(self, locals_: np.ndarray) -> JointSet:
        return JointSet(self.names, self.parents, np.asarray(locals_, F), self.symmetry)

    @classmethod
    def of_mesh(cls, mesh) -> JointSet | None:
        count = int(mesh.joint_count)
        if not count:
            return None
        return cls(tuple(mesh.names[int(mesh.bone_indices[index])] for index in range(count)),
                   tuple(int(mesh.bones[index].parent_index) for index in range(count)),
                   np.asarray(mesh.local_matrices[:count], F).reshape(count, 4, 4),
                   tuple(int(mesh.bones[index].symmetry_index) for index in range(count)))

    @classmethod
    def of_skeleton(cls, data: bytes) -> JointSet:
        """``data`` is a ``.fbxskel`` file."""
        names, parents, positions, rotations, scales, symmetry = skeleton_joint_descs(data)
        return cls(names, parents, np.asarray([trs(position, rotation, scale) for position, rotation, scale
                                               in zip(positions, rotations, scales)], F).reshape(len(names), 4, 4),
                   symmetry)


def skeleton_joint_descs(data: bytes) -> tuple[tuple[str, ...], tuple[int, ...], list, list, list, tuple[int, ...]]:
    skeleton = read_skeleton(data)
    joints, rows = skeleton.joints, skeleton.rows()
    return (tuple(joint.name for joint in joints), tuple(rows[id(joint.parent)] for joint in joints),
            [joint.position for joint in joints], [joint.rotation for joint in joints],
            [joint.scale for joint in joints], tuple(rows[id(joint.mirror)] for joint in joints))


class Placed:
    """``parent_joint`` and ``same_joints`` are the transform's ``ParentJoint`` and ``SameJointsConstraint``. Results
    are cached until ``forget``."""

    def __init__(self, local: np.ndarray, parent: Placed | None = None, parent_joint: str = "",
                 same_joints: bool = False, joints: JointSet | None = None):
        self.local, self.parent, self.joints = np.asarray(local, F), parent, joints
        parental = parent.joints if parent is not None else None
        self._hanging = None if parental is None else parental.index(parent_joint)
        self._shared = tuple(parental.index(name) if same_joints and parental is not None else None
                             for name in (joints.names if joints is not None else ()))
        depths: dict[int, int] = {}

        def depth(index: int) -> int:
            if index not in depths:
                parent = joints.parents[index]
                depths[index] = 0 if parent < 0 else depth(parent) + 1
            return depths[index]
        for index in range(len(self._shared)):
            depth(index)
        self._levels = [[index for index in sorted(depths) if depths[index] == level]
                        for level in range(max(depths.values(), default=-1) + 1)]
        self._world: np.ndarray | None = None
        self._joint_worlds: np.ndarray | None = None

    def forget(self) -> None:
        """Call when the transform or one above it moved."""
        self._world = self._joint_worlds = None

    def world(self) -> np.ndarray:
        if self._world is None:
            if self.parent is None:
                self._world = self.local
            else:
                base = self.parent.world() if self._hanging is None else self.parent.joint_world(self._hanging)
                self._world = transform_product(self.local, base)
        return self._world

    def joint_world(self, index: int) -> np.ndarray:
        if self._joint_worlds is None:
            joints, out = self.joints, np.empty_like(self.joints.locals)
            for level in self._levels:
                own = [joint for joint in level if self._shared[joint] is None]
                for joint in level:
                    if self._shared[joint] is not None:
                        out[joint] = self.parent.joint_world(self._shared[joint])
                if own:
                    out[own] = transform_product(joints.locals[own], [
                        out[joints.parents[joint]] if joints.parents[joint] >= 0 else self.world() for joint in own])
            self._joint_worlds = out
        return self._joint_worlds[index]

    def named(self, name: str) -> np.ndarray | None:
        index = None if self.joints is None else self.joints.index(name)
        return None if index is None else self.joint_world(index)


def placed_objects(document, root: Placed | None, joints_of: Callable[[object], JointSet | None],
                   fixed: Mapping[int, Placed] | None = None) -> dict[int, Placed]:
    out: dict[int, Placed] = dict(fixed or {})

    def place(item) -> Placed:
        key = item.id.local_object_id
        if key not in out:
            found = document.object_by_local_id.get(item.parent_id)
            parent = place(document.objects[found]) if found in document.objects else root
            held = item.transform
            out[key] = Placed(np.eye(4, dtype=F) if held is None else trs(held.position, held.rotation, held.scale),
                              parent, "" if held is None else held.parent_joint,
                              held is not None and held.same_joints_constraint, joints_of(item))
        return out[key]
    for item in document.objects.values():
        place(item)
    return out


__all__ = ["JointSet", "Placed", "placed_objects", "skeleton_joint_descs", "trs"]
