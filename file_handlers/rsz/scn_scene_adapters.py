from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np

from file_handlers.rsz.rsz_data_types import ResourceData, StringData

from .scn_scene_graph import ScnTransform, make_trs_matrix, normalize_scene_path


def decompose_trs(matrix: np.ndarray, reference: ScnTransform | None = None) -> ScnTransform:
    matrix = np.asarray(matrix, dtype=np.float32)
    position = tuple(float(v) for v in matrix[:3, 3])
    basis = matrix[:3, :3].astype(np.float32, copy=True)
    scale = np.linalg.norm(basis, axis=0)
    scale[scale < 1e-8] = 1.0
    if reference is not None:
        scale *= np.where(np.asarray(reference.scale, dtype=np.float32) < 0.0, -1.0, 1.0)
    basis /= scale
    rotation = _matrix_to_quat(basis)
    return ScnTransform(position, rotation, tuple(float(v) for v in scale), make_trs_matrix(position, rotation, tuple(float(v) for v in scale)))


def _matrix_to_quat(m: np.ndarray) -> tuple[float, float, float, float]:
    trace = float(m[0, 0] + m[1, 1] + m[2, 2])
    if trace > 0.0:
        s = (trace + 1.0) ** 0.5 * 2.0
        q = ((m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s)
    else:
        i = int(np.argmax([m[0, 0], m[1, 1], m[2, 2]]))
        if i == 0:
            s = (1.0 + m[0, 0] - m[1, 1] - m[2, 2]) ** 0.5 * 2.0
            q = (0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s)
        elif i == 1:
            s = (1.0 + m[1, 1] - m[0, 0] - m[2, 2]) ** 0.5 * 2.0
            q = ((m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s)
        else:
            s = (1.0 + m[2, 2] - m[0, 0] - m[1, 1]) ** 0.5 * 2.0
            q = ((m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s)
    length = float(np.linalg.norm(np.asarray(q, dtype=np.float32)))
    return tuple(float(v / length) for v in q) if length > 1e-8 else (0.0, 0.0, 0.0, 1.0)


@dataclass(slots=True)
class TransformAdapter:
    fields: Mapping[str, object]

    def read(self) -> ScnTransform:
        values = _values(self.fields)
        if len(values) < 3:
            raise ValueError("Transform field set has fewer than three values")
        position = _vec3(values[0])
        rotation = _quat(values[1])
        scale = _vec3(values[2])
        return ScnTransform(position, rotation, scale, make_trs_matrix(position, rotation, scale))

    def write(self, transform: ScnTransform) -> None:
        values = _values(self.fields)
        if len(values) < 3:
            raise ValueError("Transform field set has fewer than three values")
        _set_vec3(values[0], transform.position)
        _set_quat(values[1], transform.rotation)
        _set_vec3(values[2], transform.scale)

    def transform_fields(self) -> tuple[object, object, object]:
        values = _values(self.fields)
        if len(values) < 3:
            raise ValueError("Transform field set has fewer than three values")
        return tuple(values[:3])

    def owns_field(self, value: object) -> bool:
        return any(field is value for field in _values(self.fields)[:3])


@dataclass(slots=True)
class MeshAdapter:
    fields: Mapping[str, object]

    def paths(self) -> tuple[str, str]:
        values = _resource_values(self.fields)
        mesh_index = next((index for index, value in enumerate(values) if ".mesh" in value.lower()), -1)
        if mesh_index < 0:
            return "", ""
        return normalize_scene_path(values[mesh_index]), normalize_scene_path(values[mesh_index + 1]) if mesh_index + 1 < len(values) else ""


@dataclass(slots=True)
class FolderLinkAdapter:
    fields: Mapping[str, object]

    def scene_path(self) -> str:
        return normalize_scene_path(next((value for value in reversed(_resource_values(self.fields)) if ".scn" in value.lower()), ""))


@dataclass(slots=True)
class CompositeMeshAdapter:
    fields: Mapping[str, object]

    def read_transform(self) -> ScnTransform:
        values, index = _values(self.fields), self._trs_index()
        position, rotation, scale = _vec3(values[index]), _quat(values[index + 1]), _vec3(values[index + 2])
        return ScnTransform(position, rotation, scale, make_trs_matrix(position, rotation, scale))

    def write_transform(self, transform: ScnTransform) -> None:
        values, index = _values(self.fields), self._trs_index()
        _set_vec3(values[index], transform.position)
        _set_quat(values[index + 1], transform.rotation)
        _set_vec3(values[index + 2], transform.scale)

    def transform_fields(self) -> tuple[object, object, object]:
        values, index = _values(self.fields), self._trs_index()
        return tuple(values[index:index + 3])

    def owns_transform_field(self, value: object) -> bool:
        values, index = _values(self.fields), self._trs_index()
        return any(field is value for field in values[index:index + 3])

    def _trs_index(self) -> int:
        values = _values(self.fields)
        for index in range(max(0, len(values) - 2)):
            if _is_vec3(values[index]) and _is_quat(values[index + 1]) and _is_vec3(values[index + 2]):
                return index
        raise ValueError("Composite transform controller has no editable TRS fields")


def _vec3(value) -> tuple[float, float, float]:
    if not _is_vec3(value):
        raise ValueError("Expected vec3 field")
    return tuple(float(getattr(value, name)) for name in ("x", "y", "z"))


def _quat(value) -> tuple[float, float, float, float]:
    if not _is_quat(value):
        raise ValueError("Expected quaternion field")
    quat = np.asarray([getattr(value, name) for name in ("x", "y", "z", "w")], dtype=np.float32)
    length = float(np.linalg.norm(quat))
    return tuple(float(v / length) for v in quat) if length > 1e-8 else (0.0, 0.0, 0.0, 1.0)


def _set_vec3(value, data: tuple[float, float, float]) -> None:
    if not _is_vec3(value):
        raise ValueError("Expected vec3 field")
    for name, raw in zip(("x", "y", "z"), data):
        setattr(value, name, float(raw))


def _set_quat(value, data: tuple[float, float, float, float]) -> None:
    if not _is_quat(value):
        raise ValueError("Expected quaternion field")
    for name, raw in zip(("x", "y", "z", "w"), data):
        setattr(value, name, float(raw))


def _strip(value) -> str:
    return str(value or "").strip().rstrip("\x00").strip()


def _resource_values(fields: Mapping[str, object]) -> list[str]:
    return [text for value in fields.values() if isinstance(value, (ResourceData, StringData)) and (text := _strip(getattr(value, "value", "")))]


def _values(fields: Mapping[str, object]) -> list[object]:
    return list(fields.values())


def _is_vec3(value) -> bool:
    return all(hasattr(value, name) for name in ("x", "y", "z")) and not hasattr(value, "w")


def _is_quat(value) -> bool:
    return all(hasattr(value, name) for name in ("x", "y", "z", "w"))
