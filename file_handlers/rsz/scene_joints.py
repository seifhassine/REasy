"""See docs/internals/strands-rendering.md, Transforms and joints."""
from __future__ import annotations

import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

lazy from utils.hash_util import murmur3_hash_utf16le

F = np.float32
# A ``.fbxskel``'s joints: their offset, hash table offset and count in its header, and the bytes of each.
_SKELETON_HEADER, _SKELETON_JOINT = struct.Struct("<QQI"), 64


def trs(position: Sequence[float], rotation: Sequence[float], scale: Sequence[float]) -> np.ndarray:
    x, y, z, w = (F(value) for value in rotation)
    x2, y2, z2, one = x + x, y + y, z + z, F(1)
    xx, yy, zz, xy, xz, yz, wx, wy, wz = x * x2, y * y2, z * z2, x * y2, x * z2, y * z2, w * x2, w * y2, w * z2
    out = np.array([[(one - yy) - zz, xy + wz, xz - wy, 0], [xy - wz, (one - xx) - zz, yz + wx, 0],
                    [xz + wy, yz - wx, (one - xx) - yy, 0], [*position, 1]], F)
    out[:3] *= np.asarray(scale, F)[:, None]
    return out


def times(matrix: np.ndarray, parent: np.ndarray) -> np.ndarray:
    """Sums the row products in the engines' order."""
    a, p = np.asarray(matrix, F), np.asarray(parent, F)
    return (a[..., :, 0, None] * p[..., None, 0, :] + a[..., :, 1, None] * p[..., None, 1, :]) + (
        a[..., :, 2, None] * p[..., None, 2, :] + a[..., :, 3, None] * p[..., None, 3, :])


@dataclass(frozen=True, slots=True, eq=False)
class JointSet:
    """``parents`` hold each joint's parent index, negative for none."""
    names: tuple[str, ...]
    parents: tuple[int, ...]
    locals: np.ndarray
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
        return JointSet(self.names, self.parents, np.asarray(locals_, F))

    @classmethod
    def of_mesh(cls, mesh) -> JointSet | None:
        count = int(mesh.joint_count)
        if not count:
            return None
        return cls(tuple(mesh.names[int(mesh.bone_indices[index])] for index in range(count)),
                   tuple(int(mesh.bones[index].parent_index) for index in range(count)),
                   np.asarray(mesh.local_matrices[:count], F).reshape(count, 4, 4))

    @classmethod
    def of_skeleton(cls, data: bytes) -> JointSet:
        """``data`` is a ``.fbxskel`` file."""
        start, _hashes, count = _SKELETON_HEADER.unpack_from(data, 0x10)
        names, parents, locals_ = [], [], []
        for at in range(start, start + count * _SKELETON_JOINT, _SKELETON_JOINT):
            name, _hash, parent = struct.unpack_from("<QIh", data, at)
            end = name
            while data[end:end + 2] != b"\0\0":
                end += 2
            names.append(data[name:end].decode("utf-16-le"))
            parents.append(parent)
            rotation, position, scale = (struct.unpack_from(form, data, at + offset)
                                         for form, offset in (("<4f", 0x10), ("<3f", 0x20), ("<3f", 0x2c)))
            locals_.append(trs(position, rotation, scale))
        return cls(tuple(names), tuple(parents), np.asarray(locals_, F).reshape(count, 4, 4))


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
                self._world = times(self.local, base)
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
                    out[own] = times(joints.locals[own], [out[joints.parents[joint]] if joints.parents[joint] >= 0
                                                          else self.world() for joint in own])
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


__all__ = ["JointSet", "Placed", "placed_objects", "times", "trs"]
