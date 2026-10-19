"""See docs/internals/engine-buffers.md."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import ctypes
from dataclasses import dataclass
import re

import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from .material_preview import MaterialPreviewError

_DECLARATION = re.compile(r"(?:row_major )?(float|uint|int)(\d)?(?:x(\d))?\b")
_LOG2F = ctypes.CDLL("ucrtbase").log2f
_LOG2F.restype, _LOG2F.argtypes = ctypes.c_float, (ctypes.c_float,)
_SCALARS = {"float": np.float32, "uint": np.uint32, "int": np.int32}


@dataclass(frozen=True, slots=True)
class Camera:
    """``view`` is world to view (looking down -z, y up), 3 x 4; ``jitter`` is in clip units; ``previous`` is last
    frame's view-projection, None when still."""
    view: np.ndarray
    scale: tuple[float, float]
    near: float
    far: float
    size: tuple[int, int]
    jitter: tuple[float, float] = (0.0, 0.0)
    previous: np.ndarray | None = None
    orthographic: bool = False

    def projection(self) -> np.ndarray:
        """Reversed depth, for row vectors."""
        span = self.far - self.near
        if self.orthographic:
            return np.array([[self.scale[0], 0, 0, 0], [0, self.scale[1], 0, 0], [0, 0, 1 / span, 0],
                             [*self.jitter, self.far / span, 1]])
        a = self.near / span
        return np.array([[self.scale[0], 0, 0, 0], [0, self.scale[1], 0, 0], [*self.jitter, a, -1],
                         [0, 0, a * self.far, 0]])

    def view_matrix(self) -> np.ndarray:
        return np.vstack([np.asarray(self.view, np.float64).reshape(3, 4), [0, 0, 0, 1]])


@dataclass(frozen=True, slots=True)
class Placement:
    """``world`` is 3 x 4; ``bones`` is bones x 3 x 4 world transforms. ``previous_*`` None: still."""
    world: np.ndarray
    bones: np.ndarray | None = None
    previous_world: np.ndarray | None = None
    previous_bones: np.ndarray | None = None


CAMERA_BUFFERS = ("SceneInfo",)
PLACEMENT_BUFFERS = ("World", "InstanceWorldInfo", "SkinningMatrices")


@dataclass(frozen=True, slots=True)
class EngineConstant:
    fields: Mapping[str, Sequence[float]]
    world: str | None = None
    previous_world: str | None = None

    @property
    def placed(self) -> bool:
        return self.world is not None or self.previous_world is not None

    def laid_out(self, layout, placement: Placement) -> bytes:
        values = {name: np.asarray(value, np.float64) for name, value in self.fields.items()}
        world = np.asarray(placement.world, np.float64).reshape(3, 4)
        if self.world is not None:
            values[self.world] = world
        if self.previous_world is not None:
            values[self.previous_world] = world if placement.previous_world is None else np.asarray(
                placement.previous_world, np.float64).reshape(3, 4)
        held = {variable.name for variable in layout.variables}
        for name in values:
            if name not in held:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The engine's buffer holds no field {name}."), {"name": name})
        return _laid_out(layout, values, True).take_bytes()


class InstanceInput(bytes):
    """A vertex input the programs read per instance, e.g. RE4 SpeedTree LOD fade."""
    __slots__ = ()


def camera_key(camera: Camera) -> tuple:
    numbers = np.asarray((*camera.scale, camera.near, camera.far, *camera.size, *camera.jitter), np.float64)
    previous = None if camera.previous is None else np.asarray(camera.previous, np.float64).tobytes()
    return np.asarray(camera.view, np.float64).tobytes(), numbers.tobytes(), previous, camera.orthographic


_LAST_SCENE_VALUES: tuple[tuple, dict[str, np.ndarray]] | None = None


def scene_values(camera: Camera) -> dict[str, np.ndarray]:
    global _LAST_SCENE_VALUES
    key, last = camera_key(camera), _LAST_SCENE_VALUES
    if last is not None and last[0] == key:
        return last[1]
    values = _scene_values(camera)
    for value in values.values():
        value.setflags(write=False)
    _LAST_SCENE_VALUES = key, values
    return values


def _single_product(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """``left @ right`` in the engine's single-precision summation order."""
    a, b = np.asarray(left, np.float32), np.asarray(right, np.float32)
    total = np.zeros((4, 4), np.float32)
    for k in range(4):
        total = total + a[:, k:k + 1] * b[k:k + 1, :]
    return total.astype(np.float64)


def _shuffle(a: np.ndarray, b: np.ndarray, order: int) -> np.ndarray:
    """SSE ``shufps``."""
    return np.array([a[order & 3], a[(order >> 2) & 3], b[(order >> 4) & 3], b[(order >> 6) & 3]], np.float32)


def engine_inverse(matrix: np.ndarray) -> np.ndarray:
    """Port of ``via::matrix::inverse``; see docs/internals/engine-buffers.md."""
    f32 = np.float32
    src = np.asarray(matrix, f32).reshape(16)
    tmp = np.array([src[0], src[1], src[4], src[5]], f32)
    row1 = np.array([src[8], src[9], src[12], src[13]], f32)
    row0, row1 = _shuffle(tmp, row1, 0x88), _shuffle(row1, tmp, 0xDD)
    tmp = np.array([src[2], src[3], src[6], src[7]], f32)
    row3 = np.array([src[10], src[11], src[14], src[15]], f32)
    row2, row3 = _shuffle(tmp, row3, 0x88), _shuffle(row3, tmp, 0xDD)

    tmp = _shuffle(row2 * row3, row2 * row3, 0xB1)
    minor0, minor1 = row1 * tmp, row0 * tmp
    tmp = _shuffle(tmp, tmp, 0x4E)
    minor0, minor1 = row1 * tmp - minor0, row0 * tmp - minor1
    minor1 = _shuffle(minor1, minor1, 0x4E)

    tmp = _shuffle(row1 * row2, row1 * row2, 0xB1)
    minor0, minor3 = row3 * tmp + minor0, row0 * tmp
    tmp = _shuffle(tmp, tmp, 0x4E)
    minor0, minor3 = minor0 - row3 * tmp, row0 * tmp - minor3
    minor3 = _shuffle(minor3, minor3, 0x4E)

    tmp = _shuffle(row1, row1, 0x4E) * row3
    tmp = _shuffle(tmp, tmp, 0xB1)
    row2 = _shuffle(row2, row2, 0x4E)
    minor0, minor2 = row2 * tmp + minor0, row0 * tmp
    tmp = _shuffle(tmp, tmp, 0x4E)
    minor0, minor2 = minor0 - row2 * tmp, row0 * tmp - minor2
    minor2 = _shuffle(minor2, minor2, 0x4E)

    tmp = _shuffle(row0 * row1, row0 * row1, 0xB1)
    minor2, minor3 = row3 * tmp + minor2, row2 * tmp - minor3
    tmp = _shuffle(tmp, tmp, 0x4E)
    minor2, minor3 = row3 * tmp - minor2, minor3 - row2 * tmp

    tmp = _shuffle(row0 * row3, row0 * row3, 0xB1)
    minor1, minor2 = minor1 - row2 * tmp, row1 * tmp + minor2
    tmp = _shuffle(tmp, tmp, 0x4E)
    minor1, minor2 = row2 * tmp + minor1, minor2 - row1 * tmp

    tmp = _shuffle(row0 * row2, row0 * row2, 0xB1)
    minor1, minor3 = row3 * tmp + minor1, minor3 - row1 * tmp
    tmp = _shuffle(tmp, tmp, 0x4E)
    minor1, minor3 = minor1 - row3 * tmp, row1 * tmp + minor3

    determinant = row0 * minor0
    determinant = _shuffle(determinant, determinant, 0x4E) + determinant
    determinant = _shuffle(determinant, determinant, 0xB1) + determinant
    if determinant[0] == 0:
        return np.eye(4, dtype=f32)
    scale = f32(f32(1) / determinant[0])
    return np.array([minor0 * scale, minor1 * scale, minor2 * scale, minor3 * scale], f32)


def _scene_values(camera: Camera) -> dict[str, np.ndarray]:
    view = camera.view_matrix()
    projection = camera.projection()
    view_projection = _single_product(view.T, projection)
    size = np.asarray(camera.size, np.float64)
    previous = view_projection if camera.previous is None else np.array(camera.previous, np.float64)
    elements = projection[[0, 1, 2, 2, 2, 2, 3, 3], [0, 1, 0, 1, 2, 3, 2, 3]]
    # The planes as the engine derives them from the projection, in single precision.
    f32 = np.float32
    p22, p32 = f32(projection[2, 2]), f32(projection[3, 2])
    far = f32(p32 / p22)
    near = f32(f32(-p22 * far) / f32(f32(-1) - p22))
    ratio = f32(near / far)
    culling = (f32(1) / near, f32(1) / f32(_LOG2F(f32(far / near))))
    return {"viewProjMat": view_projection, "prevViewProjMat": previous, "transposeViewMat": view[:3],
            "transposeViewInvMat": np.linalg.inv(view)[:3], "viewProjInvMat": engine_inverse(view_projection).astype(np.float64),
            "projElement": elements,
            "projInvElements": engine_inverse(projection).astype(np.float64)[[0, 1, 2, 2, 3, 3, 3, 3],
                                                                             [0, 1, 2, 3, 0, 1, 2, 3]],
            "ZToLinear": np.array([ratio, f32(ratio - f32(1)), near], np.float64), "screenSize": size,
            "screenInverseSize": 1 / size, "cameraNearPlane": np.array([near], np.float64),
            "cameraFarPlane": np.array([far], np.float64), "cullingHelper": np.array(culling, np.float64)}


def _scene_info(camera: Camera, frame: Mapping[str, Sequence[float]]) -> dict[str, np.ndarray]:
    values = scene_values(camera)
    if values.keys() & frame.keys():
        raise ValueError(f"the frame's constants {sorted(values.keys() & frame.keys())} are the camera's fields")
    return values | {name: np.asarray(items, np.float64) for name, items in frame.items()}


def _bone_count(placement: Placement) -> int:
    return 0 if placement.bones is None else len(placement.bones)


def _instance_values(placement: Placement) -> dict[str, np.ndarray]:
    world = np.asarray(placement.world, np.float64).reshape(3, 4)
    previous = world if placement.previous_world is None else np.asarray(placement.previous_world, np.float64)
    return {"worldMat": world, "prevWorldMat": previous, "jointOffset": np.array([0]),
            "prevJointOffset": np.array([_bone_count(placement)])}


def _encoded(variable, values: np.ndarray, constant: bool) -> tuple[int, bytes]:
    match = _DECLARATION.match(variable.hlsl_declaration or "")
    if match is None or variable.matrix and not variable.hlsl_declaration.startswith("row_major "):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The engine field {name} is declared {declaration}, which the preview does not lay out."),
            {"name": variable.name, "declaration": variable.hlsl_declaration or ""})
    values = np.asarray(values, np.float64)
    if values.shape == (3, 4) and (match[2], match[3]) == ("4", "4"):
        # 4 x 4 fields hold transforms for row vectors, translation in the last row.
        values = np.vstack([values, [0, 0, 0, 1]]).T
    words = values.ravel().astype(_SCALARS[match[1]])
    width = int(match[2] or 1) * int(match[3] or 1)
    if len(words) != width * variable.array_length:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The engine field {name} holds {count} values, not {expected}."),
            {"name": variable.name, "count": len(words), "expected": width * variable.array_length})
    if variable.array_length == 1 or not constant or width * 4 % 16 == 0:
        return variable.byte_offset, words.tobytes()
    rows = bytearray(variable.byte_size)
    for index in range(variable.array_length):
        element = words[index * width:(index + 1) * width].tobytes()
        rows[16 * index:16 * index + len(element)] = element
    return variable.byte_offset, rows.take_bytes()


def _laid_out(layout, values: dict[str, np.ndarray], constant: bool) -> bytearray:
    data = bytearray(layout.byte_size)
    for variable in layout.variables:
        if variable.name in values:
            offset, encoded = _encoded(variable, values[variable.name], constant)
            data[offset:offset + len(encoded)] = encoded
    return data


def _elements(layout, values: dict[str, np.ndarray], count: int) -> bytes:
    data = np.zeros((count, layout.byte_size), np.uint8)
    for variable in layout.variables:
        if variable.name in values and count:
            rows = np.asarray(values[variable.name], np.float64).reshape(count, -1)
            offset, first = _encoded(variable, rows[0], False)
            scalar = _SCALARS[_DECLARATION.match(variable.hlsl_declaration)[1]]
            data[:, offset:offset + len(first)] = rows.astype(scalar).view(np.uint8)
    return data.tobytes()


FRAME_BUFFERS = ("Tonemap", "RangeCompressInfo", "CameraKerare", "ColorCorrectTexture", "LDRPostProcessParam",
                 "OutputColorAdjustment")
RANGE_COMPRESS_FIELDS = ("rangeCompress", "rangeDecompress", "prevRangeCompress", "prevRangeDecompress",
                         "rangeCompressForResource", "rangeDecompressForResource", "rangeCompressForCommon",
                         "rangeDecompressForCommon")


def frame_values(name: str) -> dict[str, np.ndarray]:
    """Neutral values; ``CameraKerare`` is the vignette."""
    if name == "Tonemap":
        return {"exposureAdjustment": np.array([1.0])}
    if name == "RangeCompressInfo":
        return {field: np.array([1.0]) for field in RANGE_COMPRESS_FIELDS}
    if name == "CameraKerare":
        return {"kerare_brightness": np.array([1.0])}
    if name in ("ColorCorrectTexture", "LDRPostProcessParam"):
        return {"fColorMatrix": np.eye(4)}
    if name == "OutputColorAdjustment":
        return {"fGamma": np.array([1.0]), "fUpperLimit": np.array([1.0]), "fConvertToLimit": np.array([1.0])}
    return {}


def with_words(data: bytearray, layout, words: Mapping[str, Sequence[int]]) -> bytearray:
    for variable in layout.variables:
        held = words.get(variable.name)
        if held is not None:
            data[variable.byte_offset:variable.byte_offset + 4 * len(held)] = np.asarray(held, "<u4").tobytes()
    return data


def engine_buffer(contract, resource, camera: Camera, placement: Placement,
                  frame: Mapping[str, Sequence[float]] = frozendict(),
                  captured: Mapping[str, Mapping[str, Sequence[int]]] = frozendict()) -> bytes | None:
    if resource.register_class == "b":
        layout = contract.buffer(resource.name)
        values = {"SceneInfo": lambda: _scene_info(camera, frame), "World": lambda: _instance_values(placement),
                  "CheckerBoardInfo": lambda: {"cbr": np.array([1.0, 1.0])}}.get(resource.name)
        data = _laid_out(layout, values() if values else frame_values(resource.name), True)
        if resource.name != "SceneInfo":
            with_words(data, layout, captured.get(resource.name, {}))
        return data.take_bytes()
    if resource.input_type != "structured-buffer":
        return None
    layout = contract.structured_buffer(resource.name)
    if resource.name == "InstanceWorldInfo":
        return _laid_out(layout, _instance_values(placement), False).take_bytes()
    if resource.name == "SkinningMatrices" and placement.bones is not None:
        names = [variable.name for variable in layout.variables]
        if len(names) != 3:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The engine's bone buffer has {count} fields, not three rows."), {"count": len(names)})
        previous = placement.bones if placement.previous_bones is None else placement.previous_bones
        bones = np.concatenate([np.asarray(placement.bones, np.float64).reshape(-1, 3, 4),
                                np.asarray(previous, np.float64).reshape(-1, 3, 4)])
        return _elements(layout, dict(zip(names, bones.transpose(1, 0, 2))), len(bones))
    return None


__all__ = ["CAMERA_BUFFERS", "Camera", "EngineConstant", "FRAME_BUFFERS", "InstanceInput", "PLACEMENT_BUFFERS",
           "Placement", "RANGE_COMPRESS_FIELDS", "engine_buffer", "engine_inverse", "frame_values", "scene_values",
           "with_words"]
