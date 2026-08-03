from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from ui.scene.scene_model import SceneDrawMesh

from .model import MotionPreviewSnapshot


@dataclass(frozen=True, slots=True)
class SkeletonSceneStyle:
    joint_radius_fraction: float = 0.012
    bone_radius_fraction: float = 0.005
    minimum_radius: float = 0.004


_JOINT_VERTICES = np.asarray(
    [
        (1.0, 0.0, 0.0),
        (-1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, -1.0, 0.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, -1.0),
    ],
    dtype=np.float32,
)
_JOINT_INDICES = np.asarray(
    [
        0,
        2,
        4,
        4,
        2,
        1,
        1,
        2,
        5,
        5,
        2,
        0,
        0,
        4,
        3,
        4,
        1,
        3,
        1,
        5,
        3,
        5,
        0,
        3,
    ],
    dtype=np.uint32,
)


def _cylinder_geometry(sides: int = 6) -> tuple[np.ndarray, np.ndarray]:
    vertices = []
    for depth in (0.0, 1.0):
        vertices.extend(
            (
                math.cos(index * 2.0 * math.pi / sides),
                math.sin(index * 2.0 * math.pi / sides),
                depth,
            )
            for index in range(sides)
        )
    indices = []
    for index in range(sides):
        following = (index + 1) % sides
        indices.extend(
            (
                index,
                following,
                index + sides,
                following,
                following + sides,
                index + sides,
            )
        )
    for index in range(1, sides - 1):
        indices.extend((0, index + 1, index))
        indices.extend((sides, sides + index, sides + index + 1))
    return np.asarray(vertices, dtype=np.float32), np.asarray(indices, dtype=np.uint32)


_BONE_VERTICES, _BONE_INDICES = _cylinder_geometry()


def _joint_matrices(positions: np.ndarray, radius: float) -> np.ndarray:
    matrices = np.tile(np.identity(4, dtype=np.float32), (len(positions), 1, 1))
    matrices[:, 0, 0] = matrices[:, 1, 1] = matrices[:, 2, 2] = radius
    matrices[:, :3, 3] = positions
    return matrices


def _bone_matrices(
    starts: np.ndarray,
    ends: np.ndarray,
    radius: float,
) -> np.ndarray:
    matrices = np.tile(np.identity(4, dtype=np.float32), (len(starts), 1, 1))
    if not len(starts):
        return matrices

    delta = ends - starts
    lengths = np.linalg.norm(delta, axis=1)
    valid = lengths > 1e-8
    axes = np.zeros_like(delta)
    axes[:, 2] = 1.0
    np.divide(delta, lengths[:, None], out=axes, where=valid[:, None])
    display_lengths = np.where(valid, lengths, 1e-6)

    references = np.zeros_like(axes)
    references[:, 1] = 1.0
    references[np.abs(axes[:, 1]) > 0.9] = (1.0, 0.0, 0.0)
    sides = np.cross(references, axes)
    sides /= np.linalg.norm(sides, axis=1)[:, None]
    ups = np.cross(axes, sides)

    matrices[:, :3, 0] = sides * radius
    matrices[:, :3, 1] = ups * radius
    matrices[:, :3, 2] = axes * display_lengths[:, None]
    matrices[:, :3, 3] = starts
    return matrices


def _weight_color(
    weight: float, *, root: bool = False
) -> tuple[float, float, float, float]:
    if root:
        return 1.0, 0.38, 0.72, 1.0
    if weight < 0.9999:
        return 1.0, min(1.0, max(0.0, 0.34 + 0.5 * weight)), 0.08, 1.0
    return 0.18, 0.78, 1.0, 1.0


class SkeletonScene:
    """Stable skeleton geometry whose pose can be updated in place."""

    def __init__(
        self,
        snapshot: MotionPreviewSnapshot,
        style: SkeletonSceneStyle = SkeletonSceneStyle(),
    ):
        positions = np.asarray(snapshot.joint_positions, dtype=np.float32).reshape(
            -1, 3
        )
        extent = (
            max(float(np.max(np.ptp(positions, axis=0))), 1.0)
            if len(positions)
            else 1.0
        )
        self.joint_radius = max(
            style.minimum_radius,
            extent * style.joint_radius_fraction,
        )
        self.bone_radius = max(
            style.minimum_radius * 0.5,
            extent * style.bone_radius_fraction,
        )
        self.joint_names = snapshot.joint_names
        self.bone_pairs = snapshot.bone_pairs
        self._joint_keys = tuple(
            f"joint:{index}:{name}"
            for index, name in enumerate(self.joint_names)
        )
        self._bone_keys = tuple(f"bone:{child}" for _parent, child in self.bone_pairs)
        self._bone_parents = np.asarray(
            [parent for parent, _child in self.bone_pairs],
            dtype=np.intp,
        )
        self._bone_children = np.asarray(
            [child for _parent, child in self.bone_pairs],
            dtype=np.intp,
        )
        parented = {child for _parent, child in self.bone_pairs}
        transforms = self.transforms(snapshot)
        self.meshes = [
            SceneDrawMesh(
                key=f"bone:{child}",
                geometry_key="motion-preview:bone",
                vertices=_BONE_VERTICES,
                indices=_BONE_INDICES,
                color=_weight_color(
                    snapshot.node_weights[child]
                    if child < len(snapshot.node_weights)
                    else 1.0
                ),
                force_solid=True,
                ignore_highlight_filter=True,
                transform_matrix=transforms[key],
            )
            for key, (_parent, child) in zip(
                self._bone_keys,
                self.bone_pairs,
                strict=True,
            )
        ]
        self.meshes.extend(
            SceneDrawMesh(
                key=f"joint:{index}:{name}",
                geometry_key="motion-preview:joint",
                vertices=_JOINT_VERTICES,
                indices=_JOINT_INDICES,
                color=_weight_color(
                    snapshot.node_weights[index]
                    if index < len(snapshot.node_weights)
                    else 1.0,
                    root=index not in parented,
                ),
                force_solid=True,
                ignore_highlight_filter=True,
                transform_matrix=transforms[key],
            )
            for index, (key, name) in enumerate(
                zip(self._joint_keys, self.joint_names, strict=True)
            )
        )

    def accepts(self, snapshot: MotionPreviewSnapshot) -> bool:
        return (
            snapshot.joint_names == self.joint_names
            and snapshot.bone_pairs == self.bone_pairs
        )

    def transforms(self, snapshot: MotionPreviewSnapshot) -> dict[str, np.ndarray]:
        if (
            snapshot.joint_names != self.joint_names
            or snapshot.bone_pairs != self.bone_pairs
        ):
            raise ValueError("skeleton topology changed")
        positions = np.asarray(snapshot.joint_positions, dtype=np.float32).reshape(
            -1, 3
        )
        bone_matrices = _bone_matrices(
            positions[self._bone_parents],
            positions[self._bone_children],
            self.bone_radius,
        )
        joint_matrices = _joint_matrices(positions, self.joint_radius)
        transforms = dict(zip(self._bone_keys, bone_matrices, strict=True))
        transforms.update(zip(self._joint_keys, joint_matrices, strict=True))
        return transforms


def build_skeleton_scene(
    snapshot: MotionPreviewSnapshot,
    style: SkeletonSceneStyle = SkeletonSceneStyle(),
) -> list[SceneDrawMesh]:
    return SkeletonScene(snapshot, style).meshes
