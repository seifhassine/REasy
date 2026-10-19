from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from file_handlers.mmtr.textures_view import MOST_INFLUENCES

from .scene_model import SceneSkinningBinding


@dataclass(slots=True)
class _Skin:
    binding: SceneSkinningBinding
    joints: np.ndarray
    bones: np.ndarray
    positions: np.ndarray
    normals: np.ndarray | None
    rows: np.ndarray


def _vec3(values) -> np.ndarray:
    return np.ascontiguousarray(np.asarray(values, dtype=np.float32).reshape(-1, 3))


def _rows(matrices: np.ndarray) -> np.ndarray:
    """Row-vector 4 x 4 matrices to 3 x 4 rows for column vectors."""
    return np.ascontiguousarray(np.transpose(matrices[:, :, :3], (0, 2, 1)))


class SceneSkinning:
    def __init__(self) -> None:
        self._skins: dict[str, _Skin] = {}

    def influences(self, key: str) -> tuple[np.ndarray, np.ndarray] | None:
        """n x k each; bones index the rows ``posed`` returns."""
        skin = self._skins.get(str(key))
        return None if skin is None else (skin.joints, skin.binding.weights)

    def posed(self, key: str) -> tuple[np.ndarray, np.ndarray | None, np.ndarray]:
        """Each bone's rows are 3 x 4, for column vectors."""
        skin = self._skin(key)
        return skin.positions, skin.normals, skin.rows

    def set_binding(self, key: str, binding: SceneSkinningBinding) -> None:
        influence_count = binding.joint_indices.shape[1]
        if influence_count > MOST_INFLUENCES:
            raise ValueError(f"a vertex has at most {MOST_INFLUENCES} influences; got {influence_count}")
        bones = np.unique(binding.joint_indices[binding.weights > 0.0])
        dense = np.zeros(int(binding.joint_indices.max()) + 1, dtype=np.uint16)
        dense[bones] = np.arange(len(bones), dtype=np.uint16)
        self._skins[str(key)] = _Skin(binding, dense[binding.joint_indices], bones, binding.positions,
                                      binding.normals, _rows(np.tile(np.eye(4, dtype=np.float32), (len(bones), 1, 1))))

    def update_source(self, key: str, positions: np.ndarray, normals: np.ndarray | None) -> None:
        skin = self._skin(key)
        positions = _vec3(positions)
        normals = None if normals is None else _vec3(normals)
        if len(positions) != len(skin.binding.positions):
            raise ValueError("skinning source vertex count changed")
        if (normals is None) != (skin.binding.normals is None):
            raise ValueError("skinning source normal layout changed")
        if normals is not None and len(normals) != len(positions):
            raise ValueError("skinning source normals do not match positions")
        if not np.isfinite(positions).all() or (normals is not None and not np.isfinite(normals).all()):
            raise ValueError("skinning source contains a nonfinite value")
        skin.positions, skin.normals = positions, normals

    def update_palette(self, key: str, matrices: np.ndarray) -> None:
        """``matrices`` are row-vector skinning matrices, one per joint."""
        skin = self._skin(key)
        matrices = np.asarray(matrices, dtype=np.float32).reshape(-1, 4, 4)
        if not len(matrices) or not np.isfinite(matrices).all():
            raise ValueError("skin matrix palette is empty or non-finite")
        if int(skin.bones[-1]) >= len(matrices):
            raise ValueError(f"skinned mesh {key!r} references a joint outside its palette")
        skin.rows = _rows(matrices[skin.bones])

    def remove(self, keys: set[str] | None = None) -> set[str]:
        removed = set(self._skins) if keys is None else set(self._skins) & set(keys)
        for key in removed:
            del self._skins[key]
        return removed

    def clear(self) -> None:
        self._skins.clear()

    def _skin(self, key: str) -> _Skin:
        try:
            return self._skins[str(key)]
        except KeyError as exc:
            raise ValueError(f"skinned mesh {key!r} is not registered") from exc


__all__ = ["SceneSkinning"]
