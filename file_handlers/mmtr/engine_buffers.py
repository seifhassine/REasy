"""See docs/internals/engine-buffers.md."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import ctypes
from dataclasses import dataclass
import re

import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from utils.native_build import ensure_fastmesh

lazy from .material_preview import MaterialPreviewError

_DECLARATION = re.compile(r"(?:row_major )?(float|uint|int)(\d)?(?:x(\d))?\b")
_LOG2F = ctypes.CDLL("ucrtbase").log2f
_LOG2F.restype, _LOG2F.argtypes = ctypes.c_float, (ctypes.c_float,)
_SCALARS = {"float": np.float32, "uint": np.uint32, "int": np.int32}
_fastmesh = ensure_fastmesh()


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


# The last few cameras' values: a frame asks for its camera's and a widened one's.
_SCENE_VALUES: dict[tuple, dict[str, np.ndarray]] = {}
# By a read-only value's and a field's ids: the value, the field and where and how it is laid out.
_ENCODED: dict[tuple[int, int, bool], tuple] = {}
# ``_scene_info``'s values by the ids of the camera's and the frame's: them and it.
_SCENE_INFOS: dict[tuple[int, int], tuple] = {}
# By the ids of unchanging values and a layout of the same fields as others: them, the layout and its bytes.
_LAID: dict[tuple[int, int, bool], tuple] = {}
# By a layout's id: it and the first layout seen of the same size and fields.
_LAYOUTS: dict[int, tuple] = {}
_FIRST_LAYOUTS: dict[tuple, object] = {}


def scene_values(camera: Camera) -> dict[str, np.ndarray]:
    key = camera_key(camera)
    values = _SCENE_VALUES.get(key)
    if values is None:
        values = _scene_values(camera)
        for value in values.values():
            value.setflags(write=False)
        if len(_SCENE_VALUES) >= 4:
            del _SCENE_VALUES[next(iter(_SCENE_VALUES))]
        _SCENE_VALUES[key] = values
    return values


def _single_product(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """``left @ right`` in the engine's single-precision summation order."""
    a, b = np.asarray(left, np.float32), np.asarray(right, np.float32)
    total = np.zeros((4, 4), np.float32)
    for k in range(4):
        total = total + a[:, k:k + 1] * b[k:k + 1, :]
    return total.astype(np.float64)


def engine_inverse(matrix: np.ndarray) -> np.ndarray:
    """Port of ``via::matrix::inverse``, native (``fastmesh.engine_inverse``); see docs/internals/engine-buffers.md."""
    return np.frombuffer(_fastmesh.engine_inverse(np.ascontiguousarray(matrix, np.float32).tobytes()),
                         np.float32).reshape(4, 4).copy()


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
    """Not to be changed: held for the camera and frame."""
    values = scene_values(camera)
    held = _SCENE_INFOS.get((id(values), id(frame)))
    if held is not None and held[0] is values and held[1] is frame:
        return held[2]
    if values.keys() & frame.keys():
        raise ValueError(f"the frame's constants {sorted(values.keys() & frame.keys())} are the camera's fields")
    merged = values | {name: np.asarray(items, np.float64) for name, items in frame.items()}
    if len(_SCENE_INFOS) >= 8:
        del _SCENE_INFOS[next(iter(_SCENE_INFOS))]
    _SCENE_INFOS[id(values), id(frame)] = values, frame, merged
    return merged


def _first_layout(layout):
    held = _LAYOUTS.get(id(layout))
    if held is None or held[0] is not layout:
        # What ``_encoded`` reads of each field.
        fields = tuple((item.name, item.byte_offset, item.byte_size, item.array_length, item.matrix,
                        item.hlsl_declaration) for item in layout.variables)
        held = _LAYOUTS[id(layout)] = layout, _FIRST_LAYOUTS.setdefault((layout.byte_size, fields), layout)
    return held[1]


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
    if not any(values is held for held in _SCENE_VALUES.values()) and not any(
            values is held[2] for held in _SCENE_INFOS.values()):
        return _laid_out_anew(layout, values, constant)
    layout = _first_layout(layout)
    key = id(values), id(layout), constant
    held = _LAID.get(key)
    if held is None or held[0] is not values or held[1] is not layout:
        if len(_LAID) >= 256:
            _LAID.clear()
        held = _LAID[key] = values, layout, bytes(_laid_out_anew(layout, values, constant))
    return bytearray(held[2])


def _laid_out_anew(layout, values: dict[str, np.ndarray], constant: bool) -> bytearray:
    data = bytearray(layout.byte_size)
    for variable in layout.variables:
        if variable.name not in values:
            continue
        value = values[variable.name]
        if isinstance(value, np.ndarray) and not value.flags.writeable:
            key = id(value), id(variable), constant
            held = _ENCODED.get(key)
            if held is None or held[0] is not value or held[1] is not variable:
                if len(_ENCODED) >= 4096:
                    _ENCODED.clear()
                held = _ENCODED[key] = value, variable, *_encoded(variable, value, constant)
            offset, encoded = held[2:]
        else:
            offset, encoded = _encoded(variable, value, constant)
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
