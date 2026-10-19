from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field, replace
import math
import struct
from typing import Iterable, Sequence

import numpy as np

from utils.hash_util import murmur3_hash_utf16le
from utils.hex_util import align

from .blend_shape import blend_shape_lod_map_counts, lod_map_allocation
from .contract_utils import all_groups, layer_groups
from .mesh_file import (
    MESH_MAGIC,
    MPLY_MAGIC,
    MeshBufferItemHeader,
    MeshFile,
    MeshLoadMode,
    MPLYRuntimeSettings,
    SkinWeightBuffer,
    VertexBufferType,
)
from .profiles import (
    BlendShapeLayout,
    Half2ValueDomain,
    LegacyLightMapLayout,
    MeshBufferLayout,
    MeshContentFlagLayout,
    MeshGameProfile,
    MeshHeaderLayout,
    MeshletLodFactorEncoding,
    NormalRecalcLayout,
    SkinWeightLayout,
    VertexNormalWSemantic,
)


class CanonicalMeshWriteError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CanonicalMeshWriteResult:
    main: bytes
    streaming: bytes | None = None
    verified: object = field(default=None, compare=False, repr=False)


class _BinaryBuilder:
    def __init__(self) -> None:
        self.data = bytearray()

    @property
    def tell(self) -> int:
        return len(self.data)

    def reserve(self, size: int) -> int:
        if size < 0:
            raise CanonicalMeshWriteError("cannot reserve a negative byte count")
        offset = self.tell
        self.data.extend(bytes(size))
        return offset

    def align(self, alignment: int = 16) -> int:
        target = align(self.tell, alignment)
        self.reserve(target - self.tell)
        return target

    def write(self, fmt: str, *values: object) -> int:
        offset = self.reserve(struct.calcsize(fmt))
        struct.pack_into(fmt, self.data, offset, *values)
        return offset

    def write_bytes(self, value: bytes | bytearray | memoryview) -> int:
        raw = bytes(value)
        offset = self.tell
        self.data.extend(raw)
        return offset

    def patch(self, offset: int, fmt: str, *values: object) -> None:
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > self.tell:
            raise CanonicalMeshWriteError(
                f"patch [{offset}, {offset + size}) leaves the output buffer"
            )
        struct.pack_into(fmt, self.data, offset, *values)

    def patch_bytes(self, offset: int, value: bytes | bytearray | memoryview) -> None:
        raw = bytes(value)
        if offset < 0 or offset + len(raw) > self.tell:
            raise CanonicalMeshWriteError(
                f"byte patch [{offset}, {offset + len(raw)}) leaves the output buffer"
            )
        self.data[offset : offset + len(raw)] = raw

    def bytes(self) -> bytes:
        return bytes(self.data)


@dataclass(slots=True)
class _SubmeshPlan:
    source: object
    buffer_index: int
    vertex_offset: int
    index_offset: int
    indices: tuple[int, ...]


@dataclass(slots=True)
class _TopologyPlan:
    submeshes: dict[int, _SubmeshPlan] = field(default_factory=dict)
    main_indices: dict[int, list[int]] = field(default_factory=dict)
    shadow_indices: dict[int, list[int]] = field(default_factory=dict)
    occluder_indices: dict[int, list[int]] = field(default_factory=dict)
    source_to_buffer: dict[int, int] = field(default_factory=dict)
    buffer_to_source: dict[int, int] = field(default_factory=dict)
    use_32bit_indices: bool = False

    def item(self, submesh: object) -> _SubmeshPlan:
        try:
            return self.submeshes[id(submesh)]
        except KeyError as exc:
            raise CanonicalMeshWriteError("submesh is absent from topology plan") from exc


def _plan_layer_indices(
    plan: _TopologyPlan,
    groups: Iterable[object],
    payloads: dict[int, object],
    destination: dict[int, list[int]],
    *,
    even_aligned: bool,
) -> None:
    for group in groups:
        for submesh in getattr(group, "submeshes", ()):
            if id(submesh) in plan.submeshes:
                continue
            source_buffer_index = int(getattr(submesh, "buffer_index", 0))
            payload = payloads.get(source_buffer_index)
            if payload is None:
                raise CanonicalMeshWriteError(
                    f"submesh references absent buffer {source_buffer_index}"
                )
            buffer_index = plan.source_to_buffer[source_buffer_index]
            source_values = payload.index_values
            source_start = int(getattr(submesh, "faces_index_offset", 0))
            count = int(getattr(submesh, "indices_count", 0))
            if source_start < 0 or count < 0 or source_start + count > len(source_values):
                raise CanonicalMeshWriteError(
                    "submesh index range leaves its decoded semantic payload"
                )
            target = destination.setdefault(buffer_index, [])
            if even_aligned and len(target) & 1:
                target.append(0)
            target_start = len(target)
            indices = tuple(np.asarray(source_values[source_start : source_start + count]).tolist())
            target.extend(indices)
            if even_aligned and count & 1:
                target.append(0)
            plan.submeshes[id(submesh)] = _SubmeshPlan(
                source=submesh,
                buffer_index=buffer_index,
                vertex_offset=int(getattr(submesh, "verts_index_offset", 0)),
                index_offset=target_start,
                indices=indices,
            )


def _build_topology_plan(mesh: MeshFile) -> _TopologyPlan:
    buffer = mesh.mesh_buffer
    if not mesh.meshes and mesh.shadow_mesh is None and mesh.occluder_mesh is None:
        raise CanonicalMeshWriteError(
            "ordinary MESH needs primary, shadow, or occluder topology"
        )
    source_buffers = {
        int(submesh.buffer_index)
        for group in all_groups(mesh)
        for submesh in group.submeshes
    }
    source_buffers.update(int(index) for index in buffer.streaming_buffer_headers)
    source_buffers.add(0)
    if any(index < 0 for index in source_buffers):
        raise CanonicalMeshWriteError("buffer indices must be nonnegative")
    if buffer.profile.streaming_buffer_index_is_one_based:
        # SF6/Wilds buffer IDs are the StreamingInfo slot + 1; gaps are empty slots.
        source_to_buffer = {index: index for index in source_buffers}
    else:
        source_to_buffer = {0: 0}
        for canonical_index, source_index in enumerate(
            sorted(index for index in source_buffers if index != 0),
            start=1,
        ):
            source_to_buffer[source_index] = canonical_index
    plan = _TopologyPlan(
        source_to_buffer=source_to_buffer,
        buffer_to_source={value: key for key, value in source_to_buffer.items()},
    )
    even = bool(buffer.profile.submesh_index_ranges_are_even_aligned)
    main_groups = (group for mesh_data in mesh.meshes for group in layer_groups(mesh_data))
    _plan_layer_indices(
        plan,
        main_groups,
        buffer.buffer_payloads,
        plan.main_indices,
        even_aligned=even,
    )
    shadow = mesh.shadow_mesh
    if shadow is not None:
        _plan_layer_indices(
            plan,
            layer_groups(shadow),
            shadow.payloads,
            plan.shadow_indices,
            even_aligned=even,
        )
    occluder = mesh.occluder_mesh
    if occluder is not None:
        _plan_layer_indices(
            plan,
            layer_groups(occluder),
            occluder.payloads,
            plan.occluder_indices,
            even_aligned=even,
        )
    plan.use_32bit_indices = any(
        index > 0xFFFF
        for segments in (
            plan.main_indices,
            plan.shadow_indices,
            plan.occluder_indices,
        )
        for values in segments.values()
        for index in values
    )
    if (not plan.use_32bit_indices and mesh.normal_recalc_data is not None
            and buffer.profile.normal_recalc_has_split_vertex_page_plane):
        from .normal_recalc import NormalRecalcError, _sf6_split_vertex_pages

        # Full-capacity uint16 clusters use SF6's two-page program; other layouts use uint32 normal tables.
        for entry in _normal_topologies(mesh, plan):
            try:
                _sf6_split_vertex_pages(entry.topology)
            except NormalRecalcError:
                plan.use_32bit_indices = True
                break
    return plan


def _snorm8_values(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    nonfinite = ~np.isfinite(values)
    with np.errstate(invalid="ignore"):
        encoded = np.rint(np.clip(values, -1.0, 1.0) * 127.0).astype(np.int8).view(np.uint8)
    return encoded, nonfinite


def _snorm8(values: Sequence[float]) -> bytes:
    encoded, nonfinite = _snorm8_values(np.asarray(values, dtype=np.float32))
    if nonfinite.any():
        raise CanonicalMeshWriteError("normal/tangent stream contains nonfinite values")
    return encoded.tobytes()


def _bounded_unorm8_rows(
    values: np.ndarray,
    stored_count: int,
    *,
    normalize_to_255: bool = False,
    implicit_last: bool = True,
) -> np.ndarray:
    source = np.clip(np.asarray(values[:, :stored_count], dtype=np.float64), 0.0, 1.0)
    if normalize_to_255:
        totals = source.sum(axis=1, keepdims=True)
        source = np.divide(
            source,
            totals,
            out=np.zeros_like(source),
            where=totals > 0.0,
        )
    result = np.rint(source * 255.0).astype(np.int16)
    if not implicit_last and not normalize_to_255:
        return result.astype(np.uint8)
    for row_index, row in enumerate(result):
        target = 255 if normalize_to_255 and np.any(row) else min(255, int(row.sum()))
        delta = int(row.sum()) - target
        while delta > 0:
            lane = int(np.argmax(row))
            amount = min(delta, int(row[lane]))
            row[lane] -= amount
            delta -= amount
        while delta < 0:
            lane = int(np.argmax(source[row_index] - row.astype(np.float64) / 255.0))
            amount = min(-delta, 255 - int(row[lane]))
            if amount <= 0:
                break
            row[lane] += amount
            delta += amount
    return result.astype(np.uint8)


def _pack_10bit_indices(indices: np.ndarray) -> bytes:
    if indices.ndim != 2 or indices.shape[1] < 6:
        raise CanonicalMeshWriteError("packed skin indices require six lanes")
    if np.any(indices[:, :6] < 0) or np.any(indices[:, :6] > 1023):
        raise CanonicalMeshWriteError("packed skin index exceeds its 10-bit domain")
    values = indices[:, :6].astype(np.uint32)
    words = np.empty((len(values), 2), dtype="<u4")
    words[:, 0] = values[:, 0] | (values[:, 1] << 10) | (values[:, 2] << 20)
    words[:, 1] = values[:, 3] | (values[:, 4] << 10) | (values[:, 5] << 20)
    return words.tobytes()


def _skin_weight_arrays(stream: SkinWeightBuffer) -> tuple[np.ndarray, np.ndarray]:
    shape = (int(stream.vertex_count), int(stream.influence_count))
    indices = np.asarray(stream.deform_indices).reshape(shape)
    if not np.issubdtype(indices.dtype, np.integer):
        raise CanonicalMeshWriteError("skin indices must be integers")
    weights = np.asarray(stream.weights, dtype=np.float64).reshape(shape)
    if not np.isfinite(weights).all() or np.any(weights < 0.0):
        raise CanonicalMeshWriteError("skin weights must be finite and nonnegative")
    return indices, weights


def _encode_skin_weights(
    stream: SkinWeightBuffer,
    profile: MeshGameProfile,
) -> bytes:
    count = int(stream.vertex_count)
    influence_count = int(stream.influence_count)
    indices, weights = _skin_weight_arrays(stream)
    records = np.zeros((count, 16), dtype=np.uint8)
    if profile.skin_weight_layout is SkinWeightLayout.BYTE8:
        if influence_count > 8:
            raise CanonicalMeshWriteError("byte skin stream has more than eight influences")
        if np.any(indices < 0) or np.any(indices > 255):
            raise CanonicalMeshWriteError("byte skin index leaves its 8-bit domain")
        if influence_count < 8:
            padded_indices = np.zeros((count, 8), dtype=np.uint16)
            padded_weights = np.zeros((count, 8), dtype=weights.dtype)
            padded_indices[:, :influence_count] = indices
            padded_weights[:, :influence_count] = weights
            indices, weights = padded_indices, padded_weights
        records[:, :8] = indices[:, :8].astype(np.uint8)
        stored = 7 if stream.implicit_last_weight else 8
        records[:, 8 : 8 + stored] = _bounded_unorm8_rows(
            weights, stored, implicit_last=stream.implicit_last_weight
        )
        return records.tobytes()

    if influence_count != 6:
        raise CanonicalMeshWriteError("packed-six stream must have six influences")
    records[:, :8] = np.frombuffer(
        _pack_10bit_indices(indices), dtype=np.uint8
    ).reshape(count, 8)
    stored = 5 if stream.implicit_last_weight else min(6, int(stream.stored_weight_count))
    records[:, 8 : 8 + stored] = _bounded_unorm8_rows(
        weights, stored, implicit_last=stream.implicit_last_weight
    )
    return records.tobytes()


def _encode_selected_skin_weights(
    stream: SkinWeightBuffer,
) -> tuple[bytes, bytes | None]:
    count = int(stream.vertex_count)
    declared = int(stream.declared_influence_count)
    effective = int(stream.influence_count)
    indices, weights = _skin_weight_arrays(stream)
    primary = np.zeros((count, 16), dtype=np.uint8)

    if declared == 12:
        if effective != 12:
            raise CanonicalMeshWriteError(
                "12-weight skinning needs twelve semantic influences"
            )
        continuation = np.zeros((count, 16), dtype=np.uint8)
        primary[:, :8] = np.frombuffer(
            _pack_10bit_indices(indices[:, :6]), dtype=np.uint8
        ).reshape(count, 8)
        continuation[:, :8] = np.frombuffer(
            _pack_10bit_indices(indices[:, 6:12]), dtype=np.uint8
        ).reshape(count, 8)
        units = _bounded_unorm8_rows(weights, 11)
        primary[:, 8:16] = units[:, :8]
        continuation[:, 8:11] = units[:, 8:11]
        return primary.tobytes(), continuation.tobytes()

    if declared in (6, 8):
        if effective != 6:
            raise CanonicalMeshWriteError(
                f"SkinWeightCount {declared} selects six semantic influences"
            )
        primary[:, :8] = np.frombuffer(
            _pack_10bit_indices(indices), dtype=np.uint8
        ).reshape(count, 8)
        primary[:, 8:13] = _bounded_unorm8_rows(weights, 5)
        return primary.tobytes(), None

    if declared == 4:
        if effective != 3:
            raise CanonicalMeshWriteError(
                "SkinWeightCount 4 selects three normalized semantic influences"
            )
        padded = np.zeros((count, 6), dtype=indices.dtype)
        padded[:, :3] = indices
        primary[:, :8] = np.frombuffer(
            _pack_10bit_indices(padded), dtype=np.uint8
        ).reshape(count, 8)
        primary[:, 8:11] = _bounded_unorm8_rows(
            weights,
            3,
            normalize_to_255=True,
        )
        return primary.tobytes(), None

    raise CanonicalMeshWriteError(
        f"unsupported SkinWeightCount-selected influence layout {declared}"
    )


def _canonical_element_types(payload: object) -> tuple[int, ...]:
    present: list[int] = []
    vertex_count = int(getattr(payload, "vertex_count", 0))
    if vertex_count < 0:
        raise CanonicalMeshWriteError("vertex count must not be negative")
    for name, width in (
        ("positions", 3), ("normals", 3), ("tangents", 3),
        ("uv0", 2), ("uv1", 2), ("uv2", 2), ("colors", 4),
    ):
        size = len(getattr(payload, name, ()))
        if size not in (0, vertex_count * width):
            raise CanonicalMeshWriteError(
                f"{name} plane does not match its vertex domain"
            )
    if len(getattr(payload, "tangents", ())) and not len(getattr(payload, "normals", ())):
        raise CanonicalMeshWriteError("tangents require a normal plane")
    for name in ("skin_weights", "extra_skin_weights"):
        stream = getattr(payload, name, None)
        if stream is not None and int(stream.vertex_count) != vertex_count:
            raise CanonicalMeshWriteError(f"{name} does not match its vertex domain")
    if vertex_count == 0:
        declared = tuple(
            int(value)
            for value in getattr(payload, "declared_vertex_semantics", ())
        )
        if not declared:
            raise CanonicalMeshWriteError(
                "zero-vertex payload needs an explicit semantic input-slot declaration"
            )
        if declared != tuple(sorted(set(declared))):
            raise CanonicalMeshWriteError(
                "zero-vertex semantic input slots must be unique and canonical"
            )
        if declared[0] != int(VertexBufferType.Position):
            raise CanonicalMeshWriteError(
                "zero-vertex payload must declare Position first"
            )
        return declared
    candidates = (
        (VertexBufferType.Position, len(getattr(payload, "positions", ())) == vertex_count * 3),
        (
            VertexBufferType.NormalsTangents,
            len(getattr(payload, "normals", ())) == vertex_count * 3
            and len(getattr(payload, "tangents", ())) in (0, vertex_count * 3),
        ),
        (VertexBufferType.UV0, len(getattr(payload, "uv0", ())) == vertex_count * 2),
        (VertexBufferType.UV1, len(getattr(payload, "uv1", ())) == vertex_count * 2),
        (VertexBufferType.BoneWeights, getattr(payload, "skin_weights", None) is not None),
        (VertexBufferType.Colors, len(getattr(payload, "colors", ())) == vertex_count * 4),
        (VertexBufferType.UV2, len(getattr(payload, "uv2", ())) == vertex_count * 2),
        (
            VertexBufferType.ExtraWeights,
            getattr(payload, "extra_skin_weights", None) is not None
            or (
                getattr(payload, "skin_weights", None) is not None
                and int(
                    getattr(
                        getattr(payload, "skin_weights"),
                        "declared_influence_count",
                        0,
                    )
                )
                == 12
            ),
        ),
    )
    for semantic, enabled in candidates:
        if enabled:
            present.append(int(semantic))
    if not present or present[0] != int(VertexBufferType.Position):
        raise CanonicalMeshWriteError("nonempty vertex payload needs a Position plane")
    return tuple(present)


def _encode_attribute_plane(
    payload: object,
    semantic: int,
    profile: MeshGameProfile,
) -> tuple[int, bytes]:
    count = int(getattr(payload, "vertex_count", 0))
    if semantic == int(VertexBufferType.Position):
        values = np.asarray(getattr(payload, "positions"), dtype="<f4")
        if values.size != count * 3 or not np.isfinite(values).all():
            raise CanonicalMeshWriteError("position plane does not match its vertex domain")
        return 12, values.tobytes()
    if semantic == int(VertexBufferType.NormalsTangents):
        normals = np.asarray(getattr(payload, "normals"), dtype=np.float32).reshape(count, 3)
        tangents_value = getattr(payload, "tangents")
        tangents = np.asarray(tangents_value, dtype=np.float32)
        if tangents.size == 0:
            tangents = np.tile(np.asarray((1.0, 1.0, 1.0), dtype=np.float32), (count, 1))
        else:
            tangents = tangents.reshape(count, 3)
        normal_w = np.zeros(count, dtype=np.uint8)
        if profile.vertex_normal_w_semantic is VertexNormalWSemantic.LOCAL_PIVOT_INDEX:
            decoded = np.asarray(getattr(payload, "pivot_indices"), dtype=np.uint16)
            # Read as SNORM and remapped with *127.5 + 255.5, so 128..254 encode as 129..255.
            if decoded.size != count or np.any(decoded > 254):
                raise CanonicalMeshWriteError("pivot indices leave their byte domain")
            normal_w[:] = np.where(
                decoded >= 128,
                decoded + 1,
                decoded,
            ).astype(np.uint8)
        tangent_w = np.full(count, 127, dtype=np.uint8)
        handedness = np.asarray(getattr(payload, "tangent_handedness"), dtype=np.float32)
        if handedness.size:
            if handedness.size != count:
                raise CanonicalMeshWriteError("tangent handedness count is invalid")
            tangent_w = np.frombuffer(_snorm8(handedness), dtype=np.uint8)
        packed = np.empty((count, 8), dtype=np.uint8)
        packed[:, :3] = np.frombuffer(_snorm8(normals.reshape(-1)), dtype=np.uint8).reshape(count, 3)
        packed[:, 3] = normal_w
        packed[:, 4:7] = np.frombuffer(_snorm8(tangents.reshape(-1)), dtype=np.uint8).reshape(count, 3)
        packed[:, 7] = tangent_w
        return 8, packed.tobytes()
    uv_semantics = (int(VertexBufferType.UV0), int(VertexBufferType.UV1), int(VertexBufferType.UV2))
    if semantic in uv_semantics:
        plane_index = uv_semantics.index(semantic)
        name = f"uv{plane_index}"
        values = np.asarray(getattr(payload, name), dtype=np.float64)
        if values.size != count * 2:
            raise CanonicalMeshWriteError(f"{name} plane does not match its vertex domain")
        return 4, _half2_bytes(
            values.reshape(count, 2),
            name,
            profile.canonical_ordinary_uv_half2_domains[plane_index],
        )
    if semantic == int(VertexBufferType.Colors):
        return 4, _byte_values(getattr(payload, "colors"), count * 4, "vertex colors")
    if semantic == int(VertexBufferType.BoneWeights):
        stream = getattr(payload, "skin_weights", None)
        if stream is None:
            raise CanonicalMeshWriteError("declared skin plane has no semantic stream")
        return 16, _encode_skin_weights(stream, profile)
    if semantic == int(VertexBufferType.ExtraWeights):
        stream = getattr(payload, "extra_skin_weights", None)
        if stream is None:
            raise CanonicalMeshWriteError("declared continuation plane has no semantic stream")
        return 16, _encode_skin_weights(stream, profile)
    raise CanonicalMeshWriteError(f"unsupported vertex semantic {semantic}")


@dataclass(slots=True)
class _EncodedVertexPayload:
    vertex_bytes: bytes
    element_headers: tuple[MeshBufferItemHeader, ...]


def _encode_vertex_payload(payload: object, profile: MeshGameProfile) -> _EncodedVertexPayload:
    if getattr(payload, "unknown_attributes", None):
        raise CanonicalMeshWriteError(
            "canonical writer cannot emit an unmodeled vertex attribute"
        )
    types = _canonical_element_types(payload)
    output = bytearray()
    headers: list[MeshBufferItemHeader] = []
    selected_primary: bytes | None = None
    selected_continuation: bytes | None = None
    if profile.skin_weight_count_selects_influence_layout:
        selected_stream = getattr(payload, "skin_weights", None)
        if selected_stream is not None:
            selected_primary, selected_continuation = _encode_selected_skin_weights(
                selected_stream
            )
    for semantic in types:
        if (
            semantic == int(VertexBufferType.BoneWeights)
            and selected_primary is not None
        ):
            stride, raw = 16, selected_primary
        elif (
            semantic == int(VertexBufferType.ExtraWeights)
            and selected_continuation is not None
        ):
            stride, raw = 16, selected_continuation
        else:
            stride, raw = _encode_attribute_plane(payload, semantic, profile)
        headers.append(MeshBufferItemHeader(semantic, stride, len(output)))
        output.extend(raw)
    return _EncodedVertexPayload(bytes(output), tuple(headers))


def _index_bytes(values: Sequence[int], use_32bit: bool) -> bytes:
    dtype = "<u4" if use_32bit else "<u2"
    maximum = 0xFFFFFFFF if use_32bit else 0xFFFF
    error = CanonicalMeshWriteError("index exceeds the selected native width")
    try:
        raw_values = np.asarray(values, dtype=np.int64).ravel()
    except OverflowError as exc:
        raise error from exc
    if len(raw_values) and (raw_values.min() < 0 or raw_values.max() > maximum):
        raise error
    return raw_values.astype(dtype).tobytes()


@dataclass(slots=True)
class _BlendBodyPlan:
    body: object
    buffer_index: int
    vertex_base: int = 0
    range_locations: dict[tuple[int, int], int] = field(default_factory=dict)
    range_aabbs: dict[int, tuple[tuple[float, float, float], tuple[float, float, float]]] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class _BlendPayloadPlan:
    bodies: list[_BlendBodyPlan] = field(default_factory=list)
    payloads: dict[int, bytes] = field(default_factory=dict)

    def body_plan(self, body: object) -> _BlendBodyPlan:
        for item in self.bodies:
            if item.body is body:
                return item
        raise CanonicalMeshWriteError("blend-shape body is absent from payload plan")


def _blend_stride(compression_type: int, explicit_normals: bool) -> int:
    base = {0: 4, 1: 8, 2: 16, 3: 3}.get(int(compression_type))
    if base is None:
        raise CanonicalMeshWriteError(
            f"unsupported blend-shape compression type {compression_type}"
        )
    return base + (4 if explicit_normals else 0)


def _body_buffer_index(mesh: MeshFile, body: object) -> int:
    if not mesh.meshes:
        raise CanonicalMeshWriteError("blend shapes require ordinary MeshData")
    lod_index = int(getattr(body, "lod_index", 0))
    lods = mesh.meshes[0].lods
    if not 0 <= lod_index < len(lods):
        raise CanonicalMeshWriteError(
            f"blend-shape body references absent LOD {lod_index}"
        )
    indices = {
        int(submesh.buffer_index)
        for group in lods[lod_index].mesh_groups
        for submesh in group.submeshes
    }
    if len(indices) != 1:
        raise CanonicalMeshWriteError(
            f"blend-shape LOD {lod_index} spans buffers {sorted(indices)}"
        )
    return next(iter(indices))


def _target_quantization_aabb(
    target: object,
    channels: dict[int, object],
    target_index: int,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    aabb = getattr(target, "aabb", None)
    if aabb is not None:
        minimum = tuple(float(value) for value in aabb.minimum)
        maximum = tuple(float(value) for value in aabb.maximum)
    else:
        samples: list[np.ndarray] = []
        for local_channel in range(int(target.channel_count)):
            channel = channels.get(int(target.channel_location) + local_channel)
            if channel is None:
                continue
            for segment in channel.segments:
                if int(segment.target_index) == target_index:
                    values = np.asarray(
                        segment.position_deltas, dtype=np.float32
                    ).reshape(-1, 3)
                    if len(values):
                        samples.append(values)
        if not samples:
            minimum = maximum = (0.0, 0.0, 0.0)
        else:
            combined = np.concatenate(samples, axis=0)
            if not np.isfinite(combined).all():
                raise CanonicalMeshWriteError(
                    f"blend-shape target {target_index} needs a finite quantization AABB"
                )
            minimum = tuple(float(value) for value in combined.min(axis=0))
            maximum = tuple(float(value) for value in combined.max(axis=0))
    if len(minimum) != 3 or len(maximum) != 3 or not all(
        math.isfinite(value) for value in (*minimum, *maximum)
    ):
        raise CanonicalMeshWriteError(
            f"blend-shape target {target_index} has an invalid AABB"
        )
    if any(upper < lower for lower, upper in zip(minimum, maximum)):
        raise CanonicalMeshWriteError(
            f"blend-shape target {target_index} has inverted AABB bounds"
        )
    return minimum, maximum


def _quantize_blend_positions(
    values: np.ndarray,
    minimum: Sequence[float],
    maximum: Sequence[float],
    divisor: Sequence[int],
) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32).reshape(-1, 3)
    low = np.asarray(minimum, dtype=np.float64)
    high = np.asarray(maximum, dtype=np.float64)
    div = np.asarray(divisor, dtype=np.float64)
    span = high - low
    quantized = np.zeros_like(source, dtype=np.int64)
    active = span > 0.0
    quantized[:, active] = np.rint(
        (source[:, active] - low[active]) * div[active] / span[active] - 0.5
    ).astype(np.int64)
    return np.clip(quantized, 0, np.asarray(divisor, dtype=np.int64))


def _encode_blend_segment(
    segment: object,
    *,
    compression_type: int,
    aabb: tuple[Sequence[float], Sequence[float]],
    explicit_normals: bool,
    profile: MeshGameProfile,
) -> bytes:
    positions = np.asarray(segment.position_deltas, dtype=np.float32).reshape(-1, 3)
    if not np.isfinite(positions).all():
        raise CanonicalMeshWriteError(
            "blend-shape positions must be finite; use the vertex cull mask for culled vertices"
        )
    cull = getattr(segment, "vertex_cull_mask", None)
    cull_mask = (
        np.asarray(cull, dtype=np.bool_).reshape(-1)
        if cull is not None
        else np.zeros(len(positions), dtype=np.bool_)
    )
    if len(cull_mask) != len(positions):
        raise CanonicalMeshWriteError("blend-shape cull mask has the wrong length")
    if np.any(cull_mask) and not profile.blend_shape_nan_positions_cull_vertices:
        raise CanonicalMeshWriteError(
            "this profile has no semantic NaN vertex-cull blend-shape path"
        )
    position_records: np.ndarray
    if compression_type == 0:
        if np.any(cull_mask):
            raise CanonicalMeshWriteError(
                "packed blend positions cannot encode the NaN cull sentinel"
            )
        q = _quantize_blend_positions(
            positions,
            aabb[0],
            aabb[1],
            (2047, 1023, 2047),
        ).astype(np.uint32)
        packed = q[:, 0] | (q[:, 1] << 11) | (q[:, 2] << 21)
        position_records = np.frombuffer(
            packed.astype("<u4").tobytes(),
            dtype=np.uint8,
        ).reshape(len(positions), 4)
    elif compression_type == 1:
        records = np.zeros((len(positions), 4), dtype="<f2")
        with np.errstate(over="ignore"):
            records[:, :3] = positions
        if not np.isfinite(records[~cull_mask, :3]).all():
            raise CanonicalMeshWriteError("blend-shape position exceeds binary16")
        records[cull_mask, :3] = np.float16(np.nan)
        position_records = np.frombuffer(
            records.tobytes(),
            dtype=np.uint8,
        ).reshape(len(positions), 8)
    elif compression_type == 2:
        records = np.zeros((len(positions), 4), dtype="<f4")
        records[:, :3] = positions
        records[cull_mask, :3] = np.float32(np.nan)
        # The fourth float (deformPercent) is not read at runtime.
        position_records = np.frombuffer(
            records.tobytes(),
            dtype=np.uint8,
        ).reshape(len(positions), 16)
    elif compression_type == 3:
        if np.any(cull_mask):
            raise CanonicalMeshWriteError(
                "U8x3 blend positions cannot encode the NaN cull sentinel"
            )
        position_records = _quantize_blend_positions(
            positions,
            aabb[0],
            aabb[1],
            (255, 255, 255),
        ).astype(np.uint8)
    else:
        raise CanonicalMeshWriteError(
            f"unsupported blend-shape compression type {compression_type}"
        )

    if not explicit_normals:
        return position_records.tobytes()

    normals_value = getattr(segment, "target_normals", None)
    if normals_value is None:
        raise CanonicalMeshWriteError(
            "blend-shape payload needs explicit target normals"
        )
    normals = np.asarray(normals_value, dtype=np.float32).reshape(-1, 3)
    if len(normals) != len(positions):
        raise CanonicalMeshWriteError(
            "blend-shape target-normal count does not match positions"
        )
    normal_records = np.zeros((len(normals), 4), dtype=np.uint8)
    normal_records[:, :3] = np.frombuffer(
        _snorm8(normals.reshape(-1)), dtype=np.uint8
    ).reshape(-1, 3)
    return np.concatenate(
        (position_records, normal_records),
        axis=1,
    ).tobytes()


def _optional_array_equal(left: object | None, right: object | None) -> bool:
    if left is None or right is None:
        return left is right
    return np.array_equal(np.asarray(left), np.asarray(right), equal_nan=True)


def _assert_blend_body_alias_equivalent(alias: object, owner: object, *,
                                      alias_base: int = 0, owner_base: int = 0,
                                      shared_names: bool = True) -> None:
    def body_key(body: object) -> tuple:
        return (
            int(body.compression_type),
            int(body.channel_offset),
            int(body.channel_count),
            tuple(int(value) for value in body.target_lod_map),
            tuple(int(value) for value in body.channel_lod_map),
        )

    def target_key(target: object, base: int) -> tuple:
        ranges = tuple(
            (
                int(item.base_vertex_location) - base,
                int(item.blend_shape_location),
                int(item.vertex_count),
                int(item.tag),
            )
            for item in target.ranges
        )
        return (
            int(target.channel_location),
            int(target.channel_count),
            int(target.same_target_id),
            bool(target.is_blend_shape),
            target.part_index,
            ranges,
        )

    def aabb_key(target: object) -> tuple | None:
        aabb = getattr(target, "aabb", None)
        return None if aabb is None else (tuple(aabb.minimum), tuple(aabb.maximum))

    def channel_key(channel: object) -> tuple:
        return (
            int(channel.index),
            int(channel.name_slot) if shared_names else 0,
            str(channel.name) if shared_names else "",
            len(channel.segments),
        )

    def segment_key(segment: object, base: int) -> tuple:
        return (
            int(segment.base_vertex_location) - base,
            int(segment.buffer_index),
            int(segment.target_index),
            int(segment.range_index),
        )

    if body_key(alias) != body_key(owner):
        raise CanonicalMeshWriteError(
            f"blend-shape LOD {alias.lod_index} alias metadata disagrees "
            f"with LOD {owner.lod_index}"
        )

    alias_targets = (*alias.targets, *alias.none_targets)
    owner_targets = (*owner.targets, *owner.none_targets)
    if len(alias.targets) != len(owner.targets) or len(alias_targets) != len(owner_targets):
        raise CanonicalMeshWriteError("aliased blend-shape target counts disagree")
    for alias_target, owner_target in zip(alias_targets, owner_targets):
        if target_key(alias_target, alias_base) != target_key(owner_target, owner_base):
            raise CanonicalMeshWriteError("aliased blend-shape targets disagree")
        if aabb_key(alias_target) != aabb_key(owner_target):
            raise CanonicalMeshWriteError("aliased blend-shape target bounds disagree")

    if len(alias.channels) != len(owner.channels):
        raise CanonicalMeshWriteError("aliased blend-shape channel counts disagree")
    for alias_channel, owner_channel in zip(alias.channels, owner.channels):
        if channel_key(alias_channel) != channel_key(owner_channel):
            raise CanonicalMeshWriteError("aliased blend-shape channels disagree")
        for alias_segment, owner_segment in zip(alias_channel.segments, owner_channel.segments):
            if segment_key(alias_segment, alias_base) != segment_key(owner_segment, owner_base) or not all(
                _optional_array_equal(getattr(alias_segment, name), getattr(owner_segment, name))
                for name in ("position_deltas", "target_normals", "vertex_cull_mask")
            ):
                raise CanonicalMeshWriteError("aliased blend-shape channel payloads disagree")


def _plan_blend_payloads(mesh: MeshFile, profile: MeshGameProfile) -> _BlendPayloadPlan:
    data = mesh.blend_shape_data
    result = _BlendPayloadPlan()
    if data is None or not data.bodies:
        return result
    explicit_normals = mesh.normal_recalc_data is None
    buffers: dict[int, bytearray] = {}
    plans_by_lod: dict[int, _BlendBodyPlan] = {}
    for body in sorted(data.bodies, key=lambda item: int(item.lod_index)):
        vertex_base = (min(s.verts_index_offset for g in mesh.meshes[0].lods[body.lod_index].mesh_groups
                           for s in g.submeshes) if profile.blend_shape_vertex_offsets_are_lod_relative else 0)
        alias_of_lod = getattr(body, "alias_of_lod", None)
        if alias_of_lod is not None:
            try:
                owner_plan = plans_by_lod[int(alias_of_lod)]
            except KeyError as exc:
                raise CanonicalMeshWriteError(
                    f"blend-shape LOD {body.lod_index} aliases unavailable "
                    f"LOD {alias_of_lod}"
                ) from exc
            _assert_blend_body_alias_equivalent(body, owner_plan.body,
                                               alias_base=vertex_base, owner_base=owner_plan.vertex_base,
                                               shared_names=profile.has_extended_blend_shape_layout)
            body_plan = _BlendBodyPlan(body, owner_plan.buffer_index, vertex_base=vertex_base)
            for target, owner_target in zip(body.targets, owner_plan.body.targets):
                body_plan.range_aabbs[id(target)] = owner_plan.range_aabbs[id(owner_target)]
                for range_index in range(len(target.ranges)):
                    key = (id(owner_target), range_index)
                    if key in owner_plan.range_locations:
                        body_plan.range_locations[(id(target), range_index)] = (
                            owner_plan.range_locations[key]
                        )
            result.bodies.append(body_plan)
            plans_by_lod[int(body.lod_index)] = body_plan
            continue
        buffer_index = _body_buffer_index(mesh, body)
        output = buffers.setdefault(buffer_index, bytearray())
        compression_type = int(body.compression_type)
        stride = _blend_stride(compression_type, explicit_normals)
        if compression_type == 3 and not profile.blend_shape_u8x3_uses_aabb_quantization:
            raise CanonicalMeshWriteError(
                "canonical writing cannot preserve an undecoded type-3 morph payload"
            )
        while len(output) % stride:
            output.append(0)
        body_plan = _BlendBodyPlan(body, buffer_index, vertex_base=vertex_base)
        result.bodies.append(body_plan)
        plans_by_lod[int(body.lod_index)] = body_plan
        channels = {int(channel.index): channel for channel in body.channels}
        for target_index, target in enumerate(body.targets):
            aabb = _target_quantization_aabb(target, channels, target_index)
            body_plan.range_aabbs[id(target)] = aabb
            if not bool(target.is_blend_shape):
                continue
            ranges = list(target.ranges)
            # Payload order is target/channel/range-major; channels advance by the sum of all range vertex counts.
            for local_channel in range(int(target.channel_count)):
                for range_index, range_item in enumerate(ranges):
                    while len(output) % stride:
                        output.append(0)
                    if local_channel == 0:
                        body_plan.range_locations[
                            (id(target), range_index)
                        ] = len(output) // stride
                    expected_count = int(range_item.vertex_count)
                    channel_index = int(target.channel_location) + local_channel
                    channel = channels.get(channel_index)
                    if channel is None:
                        raise CanonicalMeshWriteError(
                            f"blend-shape body {body.lod_index} has no channel {channel_index}"
                        )
                    candidates = [
                        segment
                        for segment in channel.segments
                        if int(segment.target_index) == target_index
                        and int(segment.range_index) == range_index
                    ]
                    if len(candidates) != 1:
                        raise CanonicalMeshWriteError(
                            f"blend-shape channel {channel_index} target/range "
                            f"{target_index}:{range_index} has {len(candidates)} segments"
                        )
                    segment = candidates[0]
                    if int(segment.base_vertex_location) != int(
                        range_item.base_vertex_location
                    ):
                        raise CanonicalMeshWriteError(
                            "blend-shape segment destination disagrees with its range"
                        )
                    if len(np.asarray(segment.position_deltas).reshape(-1, 3)) != expected_count:
                        raise CanonicalMeshWriteError(
                            "blend-shape segment count disagrees with its range"
                        )
                    output.extend(_encode_blend_segment(
                        segment,
                        compression_type=compression_type,
                        aabb=aabb,
                        explicit_normals=explicit_normals,
                        profile=profile,
                    ))
    result.payloads = {index: bytes(value) for index, value in buffers.items()}
    return result


@dataclass(frozen=True, slots=True)
class _NormalTopologyPlan:
    lod: object
    topology: object


def _normal_topologies(
    mesh: MeshFile,
    topology_plan: _TopologyPlan,
) -> tuple[_NormalTopologyPlan, ...]:
    from .normal_recalc import NormalRecalcLODTopology, NormalRecalcSubmesh

    if not mesh.meshes:
        return ()
    result: list[_NormalTopologyPlan] = []
    for lod_index, lod in enumerate(mesh.meshes[0].lods):
        submeshes = [
            submesh
            for group in lod.mesh_groups
            for submesh in group.submeshes
        ]
        if not submeshes:
            raise CanonicalMeshWriteError(
                f"normal-recalculation LOD {lod_index} has no submeshes"
            )
        buffer_indices = {
            topology_plan.item(submesh).buffer_index for submesh in submeshes
        }
        if len(buffer_indices) != 1:
            raise CanonicalMeshWriteError(
                f"normal-recalculation LOD {lod_index} spans buffers "
                f"{sorted(buffer_indices)}"
            )
        buffer_index = next(iter(buffer_indices))
        vertex_base = min(
            topology_plan.item(submesh).vertex_offset for submesh in submeshes
        )
        vertex_count = sum(int(group.vertex_count) for group in lod.mesh_groups)
        semantic = tuple(
            NormalRecalcSubmesh(
                topology_plan.item(submesh).vertex_offset - vertex_base,
                topology_plan.item(submesh).indices,
                int(submesh.vert_count),
            )
            for submesh in submeshes
        )
        result.append(
            _NormalTopologyPlan(
                lod,
                NormalRecalcLODTopology(
                    lod_index,
                    buffer_index,
                    vertex_base,
                    vertex_count,
                    semantic,
                    bool(topology_plan.use_32bit_indices),
                ),
            )
        )
    return tuple(result)


def _normal_redirects(data: object, lod_index: int) -> tuple[object, ...]:
    from .normal_recalc import _lod_redirects

    redirects = _lod_redirects(data, lod_index)[1]
    if not redirects:
        raise CanonicalMeshWriteError(
            f"normal-recalculation LOD {lod_index} has no semantic redirects"
        )
    return tuple(redirects)


def _normal_redirect_words(
    redirects: Sequence[object],
    profile: MeshGameProfile,
) -> tuple[int, ...]:
    words: list[int] = []
    for index, redirect in enumerate(redirects):
        target = int(redirect.target_vertex)
        if not 0 <= target < len(redirects) or target >= (1 << 22):
            raise CanonicalMeshWriteError(
                f"normal redirect {index} leaves its vertex domain"
            )
        annotation = bool(redirect.annotation) and bool(
            profile.normal_redirect_annotation_is_runtime_semantic
        )
        words.append(target | ((1 << 31) if annotation else 0))
    return tuple(words)


@dataclass(slots=True)
class _BufferBuildPlan:
    buffer_index: int
    main_headers: tuple[MeshBufferItemHeader, ...]
    extra_headers: tuple[MeshBufferItemHeader, ...]
    vertex_bytes: bytearray
    main_index_offset: int = 0
    shadow_index_offset: int = 0
    occluder_index_offset: int = 0
    normal_local_index_offset: int = 0
    normal_redirected_list_offset: int = 0
    blend_shape_offset: int = 0
    universal_bytes: bytes = b""
    vertex_prefix_size: int = 0
    separate_weight_bytes: bytes = b""
    payload_alias_of: int | None = None

    @property
    def element_count(self) -> int:
        return len(self.main_headers)

    @property
    def total_element_count(self) -> int:
        return len(self.main_headers) + len(self.extra_headers)


def _append_zero_alignment(value: bytearray, alignment: int = 16) -> None:
    value.extend(bytes(align(len(value), alignment) - len(value)))


def _auxiliary_payloads_for_buffer(
    mesh: MeshFile,
    buffer_index: int,
    profile: MeshGameProfile,
) -> list[object]:
    by_name = {
        "shadow": mesh.shadow_mesh,
        "occluder": mesh.occluder_mesh,
    }
    result: list[object] = []
    for name in profile.auxiliary_vertex_layout_order:
        layer = by_name.get(name)
        if layer is None or bool(getattr(layer, "shares_main_payload", False)):
            continue
        payload = getattr(layer, "payloads", {}).get(buffer_index)
        if payload is None or not bool(getattr(payload, "is_auxiliary_geometry", False)):
            continue
        result.append(payload)
    return result


def _buffer_owned_normal_lod_count(data: object, normal_topologies: Sequence[object]) -> int:
    count = int(data.declared_lod_count) or (
        len(data.lods) if data.lods else len(normal_topologies)
    )
    if not 0 < count <= len(normal_topologies):
        raise CanonicalMeshWriteError(
            "buffer-owned normal-recalculation LOD count is invalid"
        )
    return count


def _pack_u32_words(target: bytearray, words: Sequence[int]) -> None:
    if words:
        target.extend(struct.pack(f"<{len(words)}I", *words))


def _append_embedded_normal_tables(
    mesh: MeshFile,
    profile: MeshGameProfile,
    normal_topologies: Sequence[_NormalTopologyPlan],
    plans: dict[int, _BufferBuildPlan],
) -> None:
    from .normal_recalc import BUFFER_OWNED_NORMAL_RECALC_PARSERS, derive_sf6_normal_indices

    data = mesh.normal_recalc_data
    if data is None or profile.normal_recalc_layout not in BUFFER_OWNED_NORMAL_RECALC_PARSERS:
        return
    count = _buffer_owned_normal_lod_count(data, normal_topologies)
    by_buffer: dict[int, list[_NormalTopologyPlan]] = {}
    for item in normal_topologies[:count]:
        entries = by_buffer.setdefault(int(item.topology.buffer_index), [])
        if any(existing.lod is item.lod for existing in entries):
            continue
        if entries and profile.normal_recalc_layout is NormalRecalcLayout.MHWILDS:
            raise CanonicalMeshWriteError(
                "Wilds-family normal tables require one unique LOD per buffer"
            )
        entries.append(item)
    for buffer_index, entries in by_buffer.items():
        plan = plans[buffer_index]
        _append_zero_alignment(plan.vertex_bytes)
        plan.normal_local_index_offset = len(plan.vertex_bytes)
        redirects_by_lod: list[tuple[object, ...]] = []
        for item in entries:
            topology = item.topology
            redirects = _normal_redirects(data, int(topology.lod_index))
            if len(redirects) != int(topology.vertex_count):
                raise CanonicalMeshWriteError(
                    f"normal LOD {topology.lod_index} redirect count disagrees with topology"
                )
            redirects_by_lod.append(redirects)
            _pack_u32_words(
                plan.vertex_bytes,
                derive_sf6_normal_indices(data, topology.submeshes, int(topology.lod_index)),
            )
        _append_zero_alignment(plan.vertex_bytes)
        plan.normal_redirected_list_offset = len(plan.vertex_bytes)
        for redirects in redirects_by_lod:
            _pack_u32_words(plan.vertex_bytes, _normal_redirect_words(redirects, profile))
        _append_zero_alignment(plan.vertex_bytes)


def _build_buffer_plans(
    mesh: MeshFile,
    profile: MeshGameProfile,
    topology_plan: _TopologyPlan,
    blend_plan: _BlendPayloadPlan,
    normal_topologies: Sequence[_NormalTopologyPlan],
) -> dict[int, _BufferBuildPlan]:
    mesh_buffer = mesh.mesh_buffer
    used_buffers = set(topology_plan.source_to_buffer.values())

    source_topology_buffers = {
        int(item.source.buffer_index)
        for item in topology_plan.submeshes.values()
    }
    source_aliases = {
        int(alias): int(owner)
        for alias, owner in mesh_buffer.streaming_payload_aliases.items()
    }

    def alias_root(index: int) -> int:
        seen: set[int] = set()
        current = int(index)
        while current in source_aliases:
            if current in seen:
                raise CanonicalMeshWriteError(
                    "streaming payload alias relation contains a cycle"
                )
            seen.add(current)
            current = int(source_aliases[current])
        return current

    alias_groups: dict[int, list[int]] = {}
    for source_index in topology_plan.source_to_buffer:
        alias_groups.setdefault(alias_root(source_index), []).append(source_index)

    source_representatives: dict[int, int] = {}
    for members in alias_groups.values():
        topology_members = [
            index for index in members if index in source_topology_buffers
        ]
        if len(topology_members) > 1:
            raise CanonicalMeshWriteError(
                "one aliased streaming allocation is referenced by multiple "
                f"independent topology buffers {sorted(topology_members)}"
            )
        candidates = topology_members or [
            index for index in members if index in mesh_buffer.buffer_payloads
        ]
        if not candidates:
            raise CanonicalMeshWriteError(
                "nonempty streaming allocation has neither topology nor a "
                f"semantic payload: {sorted(members)}"
            )
        representative = int(candidates[0])
        for member in members:
            source_representatives[int(member)] = representative

    alias_of_by_buffer = {
        int(topology_plan.source_to_buffer[source_index]): int(
            topology_plan.source_to_buffer[representative]
        )
        for source_index, representative in source_representatives.items()
        if source_index != representative
    }
    plans: dict[int, _BufferBuildPlan] = {}
    for buffer_index in sorted(used_buffers):
        if buffer_index in alias_of_by_buffer:
            continue
        source_buffer_index = topology_plan.buffer_to_source.get(
            buffer_index,
            buffer_index,
        )
        payload = mesh_buffer.buffer_payloads.get(source_buffer_index)
        if payload is None:
            raise CanonicalMeshWriteError(
                f"semantic topology references absent buffer {source_buffer_index}"
            )
        encoded = _encode_vertex_payload(payload, profile)
        vertex = bytearray(encoded.vertex_bytes)
        extra_headers: list[MeshBufferItemHeader] = []
        auxiliary_payloads = _auxiliary_payloads_for_buffer(
            mesh,
            source_buffer_index,
            profile,
        )
        if auxiliary_payloads and profile.ordinary_vertex_layout_has_no_auxiliary_elements:
            raise CanonicalMeshWriteError(
                f"{profile.display_name} does not define ordinary auxiliary vertex planes"
            )
        for auxiliary in auxiliary_payloads:
            auxiliary_encoded = _encode_vertex_payload(auxiliary, profile)
            base = len(vertex)
            extra_headers.extend(
                MeshBufferItemHeader(item.type, item.size, base + item.offset)
                for item in auxiliary_encoded.element_headers
            )
            vertex.extend(auxiliary_encoded.vertex_bytes)
        separate_stream = (
            getattr(payload, "additional_vertex_weights", None)
            if profile.supports_additional_vertex_weights
            else getattr(payload, "secondary_skin_weights", None)
            if profile.supports_secondary_skin_weights
            else None
        )
        if (separate_stream is not None
                and int(separate_stream.vertex_count) != int(payload.vertex_count)):
            raise CanonicalMeshWriteError(
                "separate skin weights do not match their vertex domain"
            )
        separate_bytes = (
            _encode_skin_weights(separate_stream, profile)
            if separate_stream is not None
            else b""
        )
        plans[buffer_index] = _BufferBuildPlan(
            buffer_index,
            encoded.element_headers,
            tuple(extra_headers),
            vertex,
            separate_weight_bytes=separate_bytes,
        )

    _append_embedded_normal_tables(
        mesh,
        profile,
        normal_topologies,
        plans,
    )
    for buffer_index, payload_bytes in blend_plan.payloads.items():
        buffer_index = topology_plan.source_to_buffer.get(
            int(buffer_index),
            int(buffer_index),
        )
        try:
            plan = plans[buffer_index]
        except KeyError as exc:
            raise CanonicalMeshWriteError(
                f"blend-shape payload references absent buffer {buffer_index}"
            ) from exc
        _append_zero_alignment(plan.vertex_bytes)
        plan.blend_shape_offset = len(plan.vertex_bytes)
        plan.vertex_bytes.extend(payload_bytes)
        _append_zero_alignment(plan.vertex_bytes)

    use_32bit = bool(topology_plan.use_32bit_indices)
    for buffer_index, plan in plans.items():
        _append_zero_alignment(plan.vertex_bytes)
        plan.vertex_prefix_size = len(plan.vertex_bytes)
        universal = bytearray(plan.vertex_bytes)
        plan.main_index_offset = len(universal)
        universal.extend(
            _index_bytes(topology_plan.main_indices.get(buffer_index, ()), use_32bit)
        )
        if profile.auxiliary_index_offsets_are_boundaries:
            plan.shadow_index_offset = len(universal)
        if topology_plan.shadow_indices.get(buffer_index):
            _append_zero_alignment(universal)
            plan.shadow_index_offset = len(universal)
            universal.extend(
                _index_bytes(topology_plan.shadow_indices[buffer_index], use_32bit)
            )
        if profile.auxiliary_index_offsets_are_boundaries:
            plan.occluder_index_offset = len(universal)
        if topology_plan.occluder_indices.get(buffer_index):
            _append_zero_alignment(universal)
            plan.occluder_index_offset = len(universal)
            universal.extend(
                _index_bytes(topology_plan.occluder_indices[buffer_index], use_32bit)
            )
        _append_zero_alignment(universal)
        plan.universal_bytes = bytes(universal)

    for alias_index, owner_index in sorted(alias_of_by_buffer.items()):
        try:
            owner = plans[owner_index]
        except KeyError as exc:
            raise CanonicalMeshWriteError(
                f"streaming alias {alias_index} has no payload owner {owner_index}"
            ) from exc
        plans[alias_index] = replace(
            owner,
            buffer_index=alias_index,
            vertex_bytes=bytearray(owner.vertex_bytes),
            payload_alias_of=owner_index,
        )
    return plans


def _legacy_normal_submeshes(
    mesh: MeshFile,
    topology_plan: _TopologyPlan,
) -> tuple[object, ...]:
    from .normal_recalc import NormalRecalcSubmesh

    if not mesh.meshes or not mesh.meshes[0].lods:
        raise CanonicalMeshWriteError("normal recalculation requires LOD0")
    return tuple(
        NormalRecalcSubmesh(
            topology_plan.item(submesh).vertex_offset,
            topology_plan.item(submesh).indices,
        )
        for group in mesh.meshes[0].lods[0].mesh_groups
        for submesh in group.submeshes
    )


def _serialize_normal_section(
    mesh: MeshFile,
    profile: MeshGameProfile,
    topology_plan: _TopologyPlan,
    normal_topologies: Sequence[_NormalTopologyPlan],
    section_offset: int,
) -> bytes:
    data = mesh.normal_recalc_data
    if data is None:
        return b""
    if not profile.normal_redirect_annotation_is_runtime_semantic:
        from .normal_recalc import NormalRecalcRedirect

        def canonical_redirects(values: Sequence[object]) -> tuple[object, ...]:
            return tuple(
                NormalRecalcRedirect(int(item.target_vertex), False)
                for item in values
            )

        data = replace(
            data,
            redirects=canonical_redirects(data.redirects),
            lods=tuple(
                replace(lod, redirects=canonical_redirects(lod.redirects))
                for lod in data.lods
            ),
            edge_excluded=False,
        )
    from .normal_recalc import (
        BUFFER_OWNED_NORMAL_RECALC_PARSERS,
        LOD0_NORMAL_RECALC_SERIALIZERS,
        serialize_sf6_normal_recalc,
    )

    layout = profile.normal_recalc_layout
    if layout in LOD0_NORMAL_RECALC_SERIALIZERS:
        return LOD0_NORMAL_RECALC_SERIALIZERS[layout](
            data,
            _legacy_normal_submeshes(mesh, topology_plan),
            section_offset=section_offset,
        )
    if layout is NormalRecalcLayout.SF6:
        return serialize_sf6_normal_recalc(
            data,
            tuple(item.topology for item in normal_topologies),
            section_offset=section_offset,
            section_alignment=profile.normal_recalc_section_alignment,
            split_vertex_page_plane=profile.normal_recalc_has_split_vertex_page_plane,
        )
    if layout in BUFFER_OWNED_NORMAL_RECALC_PARSERS:
        return struct.pack("<qq", _buffer_owned_normal_lod_count(data, normal_topologies), 0)
    raise CanonicalMeshWriteError(
        f"{profile.display_name} has no canonical normal serializer for {layout.value}"
    )


def _mesh_element_types(mesh: MeshFile) -> set[int]:
    result: set[int] = set()
    for payload in mesh.mesh_buffer.buffer_payloads.values():
        result.update(_canonical_element_types(payload))
    return result


def _semantic_external_buffer_count(mesh: MeshFile, *, layouts: bool = False) -> int:
    if mesh.header.magic == MPLY_MAGIC:
        return 0
    if mesh.streaming_info is not None:
        if layouts:
            return mesh.profile.streaming_layout_count(mesh.streaming_info.entries)
        return len(mesh.streaming_info.entries)
    header_indices = {int(index) for index in mesh.mesh_buffer.streaming_buffer_headers}
    topology_indices = {
        int(submesh.buffer_index)
        for layer in (*mesh.meshes, mesh.shadow_mesh, mesh.occluder_mesh)
        if layer is not None
        for group in layer_groups(layer)
        for submesh in group.submeshes
        if int(submesh.buffer_index) != 0
    }
    indices = header_indices | topology_indices
    if not indices:
        return 0
    if (mesh.profile is not None and mesh.profile.streaming_buffer_index_is_one_based
            and not (layouts and mesh.profile.skip_empty_stream_entries)):
        return max(indices)
    return len(indices)


def _encode_content_flags(mesh: MeshFile, profile: MeshGameProfile) -> int:
    source = mesh.header.decoded_content_flags
    element_types = _mesh_element_types(mesh)
    is_mply = mesh.header.magic == MPLY_MAGIC
    raw = 0
    has_skin_storage = (
        int(VertexBufferType.BoneWeights) in element_types
        or any(
            getattr(chunk, "skin_weights", None) is not None
            for chunk in mesh.mesh_buffer.mply_chunks
        )
    )
    if bool(source.is_skinning) if source is not None else has_skin_storage:
        raw |= 1 << 0
    if mesh.bones:
        raw |= 1 << 1
    if mesh.blend_shape_data is not None and mesh.blend_shape_data.bodies and not is_mply:
        raw |= 1 << 2
    if mesh.group_pivots is not None and mesh.group_pivots.pivots:
        raw |= 1 << 3
    if any(
        bool(getattr(submesh, "is_quad", False))
        for mesh_data in mesh.meshes
        for lod in mesh_data.lods
        for group in lod.mesh_groups
        for submesh in group.submeshes
    ):
        raw |= 1 << 4
    if is_mply or (source is not None and source.streaming_bvh):
        raw |= 1 << 5
    has_uv_feature = (
        int(VertexBufferType.UV2) in element_types or mesh.header.light_map_used
    )
    if source is not None:
        if profile.content_flag_layout in {
            MeshContentFlagLayout.SF6,
            MeshContentFlagLayout.WILDS,
            MeshContentFlagLayout.DD2_UPDATED,
        }:
            has_uv_feature = has_uv_feature or bool(source.has_lightmap_uv)
        else:
            has_uv_feature = has_uv_feature or bool(source.has_tertiary_uv)
    if profile.content_uv_flag_uses_light_map_metadata:
        has_uv_feature = bool(mesh.header.light_map_used)
    if has_uv_feature:
        raw |= 1 << 6
    has_vertex_colors = (
        any(len(chunk.colors) for chunk in mesh.mesh_buffer.mply_chunks)
        if is_mply
        else int(VertexBufferType.Colors) in element_types
    )
    if (
        profile.content_flag_layout is not MeshContentFlagLayout.RE8
        and has_vertex_colors
    ):
        raw |= 1 << 7

    external_count = _semantic_external_buffer_count(mesh, layouts=True)
    if (profile.content_buffer_count_includes_resident
            and not profile.content_buffer_count_can_wrap
            and external_count + 1 > 7):
        raise CanonicalMeshWriteError(
            f"{profile.display_name} buffer count must fit the nonzero three-bit total"
        )
    stored_count = (
        (external_count + 1) & 0x7
        if profile.content_buffer_count_includes_resident
        else external_count & 0x7
    )
    separate_count = bool(profile.header_buffer_count_bits)
    if not separate_count:
        raw |= stored_count << 9
    if source is not None and source.use_ray_tracing_vertex_animation:
        raw |= 1 << (9 if separate_count else 12)
    if mesh.sdf_path:
        raw |= 1 << (10 if separate_count else 13)
    if not is_mply:
        policy = profile.canonical_ordinary_content_policy
        if policy is not None:
            rebraiding = prefer_opaque = prefer_shadow = prefer_alpha = int(policy)
        elif source is not None:
            rebraiding, prefer_opaque, prefer_shadow, prefer_alpha = (
                int(source.enable_rebraiding), int(source.prefer_opaque),
                int(source.prefer_shadow_cast), int(source.prefer_alpha_mask),
            )
        else:
            rebraiding = prefer_opaque = prefer_shadow = prefer_alpha = 0
        raw |= (rebraiding & 0x3) << (11 if separate_count else 14)
        if source is not None and source.merge_static_mesh_bvh:
            raw |= 1 << 16
        if profile.content_flag_layout is MeshContentFlagLayout.SF6:
            raw |= int(bool(prefer_opaque)) << 17
            raw |= int(bool(prefer_shadow)) << 18
            raw |= int(bool(prefer_alpha)) << 19
        else:
            raw |= (prefer_opaque & 0x3) << 17
            raw |= (prefer_shadow & 0x3) << 19
            raw |= (prefer_alpha & 0x3) << 21
    return raw & 0x00FFFFFF


def _encode_light_map(mesh: MeshFile, profile: MeshGameProfile) -> int:
    header = mesh.header
    used = int(bool(header.light_map_used))
    width = int(header.light_map_width)
    height = int(header.light_map_height)
    if not 0 <= width <= 0x7FFF or not 0 <= height <= 0x7FFF:
        raise CanonicalMeshWriteError("light-map dimensions exceed 15 bits")
    if profile.legacy_light_map_layout is LegacyLightMapLayout.BYTE_FIELDS:
        if width > 0xFF or height > 0xFF:
            raise CanonicalMeshWriteError("legacy light-map dimensions exceed one byte")
        return (used << 8) | (width << 16) | (height << 24)
    return (used << 1) | (width << 2) | (height << 17)


@dataclass(slots=True)
class _HeaderPlan:
    pointers: dict[str, int]
    on_memory_size: int | None = None

    def patch_pointer(self, builder: _BinaryBuilder, name: str, value: int) -> None:
        position = self.pointers.get(name)
        if position is None:
            if value:
                raise CanonicalMeshWriteError(
                    f"{name} is not present in this MeshHeader layout"
                )
            return
        builder.patch(position, "<q", int(value))


def _write_header_placeholder(
    builder: _BinaryBuilder,
    mesh: MeshFile,
    profile: MeshGameProfile,
) -> _HeaderPlan:
    builder.write(
        "<4I",
        int(mesh.header.magic),
        int(mesh.header.version),
        0,
        int(mesh.header.lod_hash) & 0xFFFFFFFF,
    )
    flags = _encode_content_flags(mesh, profile)
    pointers: dict[str, int] = {}

    def pointer(name: str) -> None:
        pointers[name] = builder.reserve(8)

    if profile.header_layout is MeshHeaderLayout.LEGACY:
        legacy_flags = flags & 0xFF if profile.legacy_flags_are_content_flags else 0
        builder.write("<BBH", legacy_flags, 0, len(mesh.names))
        builder.write("<I", _encode_light_map(mesh, profile))
        for name in (
            "lods_offset",
            "shadow_lods_offset",
            "occluder_mesh_offset",
            "bones_offset",
            "normal_recalc_offset",
            "blend_shapes_offset",
            "bounds_offset",
            "mesh_offset",
            "floats_offset",
            "material_indices_offset",
            "bone_indices_offset",
            "blend_shape_indices_offset",
            "name_offsets_offset",
        ):
            pointer(name)
        builder.reserve(int(profile.header_trailing_padding))
        return _HeaderPlan(pointers)

    if profile.header_layout is MeshHeaderLayout.WILDS:
        buffer_count = 0
        if profile.header_buffer_count_bits:
            buffer_count = 1 + _semantic_external_buffer_count(mesh, layouts=True)
            if buffer_count >= 1 << profile.header_buffer_count_bits:
                raise CanonicalMeshWriteError("header buffer count exceeds its field")
        builder.write(
            "<IHIH",
            _encode_light_map(mesh, profile),
            len(mesh.names),
            flags,
            buffer_count,
        )
        builder.write("<3i", 0, 0, 0)
        pointer("vertices_offset")
    else:
        builder.write("<I", flags)
        builder.write("<HH", len(mesh.names), 0)
    if profile.header_layout is MeshHeaderLayout.RE4:
        pointer("buffer_headers_offset")
    elif profile.header_layout is MeshHeaderLayout.DD2:
        builder.write("<II", _encode_light_map(mesh, profile), 0)

    union_names = (
        (
            "micro_polygon_reserved_offset",
            "meshlet_layout_offset",
            "meshlet_bvh_offset",
            "meshlet_parts_offset",
            "blend_shapes_offset",
            "buffer_layout_offset",
            "blend_shape_bone_modifier_offset",
        )
        if mesh.header.magic == MPLY_MAGIC
        else (
            "lods_offset",
            "shadow_lods_offset",
            "occluder_mesh_offset",
            "normal_recalc_offset",
            "blend_shapes_offset",
            "buffer_layout_offset",
            "blend_shape_bone_modifier_offset",
        )
    )
    for name in union_names:
        pointer(name)
    for name in (
        "floats_offset",
        "bounds_offset",
        "bones_offset",
        "material_indices_offset",
        "bone_indices_offset",
        "blend_shape_indices_offset",
    ):
        pointer(name)
    if profile.header_layout is MeshHeaderLayout.RE4:
        pointer("streaming_info_offset")
        pointer("name_offsets_offset")
        pointer("vertices_offset")
        pointer("sdf_path_offset")
        on_memory_size = None
    elif profile.header_layout is MeshHeaderLayout.DD2:
        pointer("name_offsets_offset")
        pointer("parts_name_hash_offset")
        pointer("streaming_info_offset")
        on_memory_size = builder.reserve(8)
        pointer("sdf_path_offset")
    else:
        pointer("name_offsets_offset")
        pointer("streaming_info_offset")
        pointer("sdf_path_offset")
        on_memory_size = None
    return _HeaderPlan(pointers, on_memory_size)


def _canonical_skin_count(mesh_data: object, profile: MeshGameProfile) -> int:
    payloads = getattr(mesh_data, "payloads", None)
    if payloads is None:
        buffer = getattr(mesh_data, "buffer", None)
        payloads = getattr(buffer, "buffer_payloads", {})
    influence_counts = {
        int(
            getattr(
                stream,
                "declared_influence_count",
                getattr(stream, "influence_count", 0),
            )
        )
        for payload in getattr(payloads, "values", lambda: ())()
        for stream in (getattr(payload, "skin_weights", None),)
        if stream is not None
    }
    if len(influence_counts) > 1:
        raise CanonicalMeshWriteError(
            "one MeshData uses incompatible skin-influence layouts"
        )
    influence_count = (
        next(iter(influence_counts))
        if influence_counts
        else int(getattr(mesh_data, "skin_weight_influence_count", 0))
    )
    if influence_count <= 0:
        influence_count = int(profile.skin_weight_influence_count)
    return profile.canonical_skin_weight_count(influence_count)


def _write_submesh(
    builder: _BinaryBuilder,
    submesh: object,
    item: _SubmeshPlan,
    profile: MeshGameProfile,
) -> None:
    material = int(getattr(submesh, "material_index", 0))
    if profile.submesh_has_modern_material:
        if not 0 <= material <= 0xFF:
            raise CanonicalMeshWriteError("modern material index exceeds one byte")
        builder.write("<BB", material, int(bool(getattr(submesh, "is_quad", 0))))
        if profile.submesh_has_buffer_index:
            if not 0 <= item.buffer_index <= 0xFF:
                raise CanonicalMeshWriteError("buffer index exceeds one byte")
            builder.write("<BB", item.buffer_index, 0)
        else:
            builder.write("<H", 0)
        if profile.submesh_has_ukn1:
            builder.write("<i", 0)
    else:
        if not 0 <= material <= 0xFFFF:
            raise CanonicalMeshWriteError("legacy material index exceeds uint16")
        builder.write("<HH", material, 0)
    builder.write(
        "<IIi",
        len(item.indices),
        int(item.index_offset),
        int(item.vertex_offset),
    )
    if profile.submesh_has_streaming_offsets:
        builder.write("<ii", 0, 0)
    if profile.submesh_has_wilds_tail or profile.submesh_has_runtime_ignored_tail_residue:
        builder.write("<I", 0)


def _write_group(
    builder: _BinaryBuilder,
    group: object,
    plan: _TopologyPlan,
    profile: MeshGameProfile,
) -> None:
    submeshes = list(getattr(group, "submeshes", ()))
    group_id = int(getattr(group, "group_id", getattr(group, "part_id", 0)))
    if not 0 <= group_id <= 0xFF or len(submeshes) > 0xFF:
        raise CanonicalMeshWriteError("mesh group ID/count exceeds one byte")
    allocated = sum(
        len(plan.item(item).indices)
        + (
            len(plan.item(item).indices) & 1
            if profile.submesh_index_ranges_are_even_aligned
            else 0
        )
        for item in submeshes
    )
    builder.write("<BB6sII", group_id, len(submeshes), bytes(6), int(group.vertex_count), allocated)
    for submesh in submeshes:
        _write_submesh(builder, submesh, plan.item(submesh), profile)


def _write_group_table(
    builder: _BinaryBuilder,
    groups: Sequence[object],
    plan: _TopologyPlan,
    profile: MeshGameProfile,
) -> None:
    pointer_positions = [builder.reserve(8) for _ in groups]
    builder.align(16)
    for position, group in zip(pointer_positions, groups):
        builder.patch(position, "<q", builder.tell)
        _write_group(builder, group, plan, profile)
        builder.align(16)


def _write_lod_layout_header(
    builder: _BinaryBuilder,
    layout: object,
    lods: Sequence[object],
    plan: _TopologyPlan,
    profile: MeshGameProfile,
    name: str,
) -> None:
    if not lods or len(lods) > 0xFF:
        raise CanonicalMeshWriteError(f"{name} needs 1..255 LODs")
    material_count = int(getattr(layout, "material_count", 0))
    # uvCount is runtime metadata, not the number of UV planes (RT/DD2 store 3-4 with two planes).
    uv_count = int(getattr(layout, "uv_count", 0))
    if not 0 <= material_count <= 0xFF or not 0 <= uv_count <= 0xFF:
        raise CanonicalMeshWriteError(f"{name} material/UV count exceeds one byte")
    builder.write(
        "<4B HBB",
        len(lods),
        material_count,
        uv_count,
        _canonical_skin_count(layout, profile),
        max((sum(len(group.submeshes) for group in lod.mesh_groups) for lod in lods), default=0),
        int(plan.use_32bit_indices),
        0,
    )


def _write_lod_table(
    builder: _BinaryBuilder,
    lods: Sequence[object],
    plan: _TopologyPlan,
    profile: MeshGameProfile,
    lod_offsets_by_id: dict[int, int],
) -> None:
    pointer_positions = [builder.reserve(8) for _ in lods]
    builder.align(16)
    for lod_index, (position, lod) in enumerate(zip(pointer_positions, lods)):
        offset = lod_offsets_by_id.get(id(lod))
        if offset is None:
            offset = _write_lod(builder, lod, lod_index, plan, profile)
            lod_offsets_by_id[id(lod)] = offset
        builder.patch(position, "<q", offset)


def _write_lod(
    builder: _BinaryBuilder,
    lod: object,
    lod_index: int,
    plan: _TopologyPlan,
    profile: MeshGameProfile,
) -> int:
    start = builder.tell
    groups = list(getattr(lod, "mesh_groups", ()))
    if len(groups) > 0xFF:
        raise CanonicalMeshWriteError("LOD has more than 255 mesh groups")
    builder.write("<BB", len(groups), 0)
    if profile.lod_has_level:
        level = int(getattr(lod, "lod_level", lod_index))
        if not 0 <= level <= 0xFF:
            raise CanonicalMeshWriteError("semantic LOD level exceeds one byte")
        builder.write("<BB", level, 0)
    else:
        builder.write("<H", 0)
    factor = float(getattr(lod, "lod_factor", 0.0))
    if math.isnan(factor) and profile.ordinary_lod_factor_supports_nan:
        builder.write("<I", 0x7FC00000)
    elif not math.isfinite(factor) or factor < 0.0:
        raise CanonicalMeshWriteError("LOD factor must be finite and nonnegative")
    else:
        builder.write("<f", factor)
    builder.write("<q", builder.tell + 8)
    _write_group_table(builder, groups, plan, profile)
    return start


def _write_mesh_data(
    builder: _BinaryBuilder,
    mesh_data: object,
    plan: _TopologyPlan,
    profile: MeshGameProfile,
    lod_offsets_by_id: dict[int, int],
) -> int:
    start = builder.tell
    lods = list(getattr(mesh_data, "lods", ()))
    _write_lod_layout_header(builder, mesh_data, lods, plan, profile, "MeshData")
    if profile.mesh_data_has_legacy_unknown:
        builder.write("<q", 0)
    sphere = tuple(float(v) for v in getattr(mesh_data, "bounding_sphere"))
    if len(sphere) != 4 or not all(math.isfinite(v) for v in sphere):
        raise CanonicalMeshWriteError("MeshData bounding sphere is invalid")
    builder.write("<4f", *sphere)
    minimum, maximum = getattr(mesh_data, "bounding_box")
    minimum = tuple(float(v) for v in minimum[:3])
    maximum = tuple(float(v) for v in maximum[:3])
    if not all(math.isfinite(v) for v in (*minimum, *maximum)):
        raise CanonicalMeshWriteError("MeshData bounds contain nonfinite values")
    if profile.mesh_data_has_modern_bounds:
        padding = int(profile.canonical_modern_aabb_padding)
        if not 0 <= padding <= 0xFFFFFF:
            raise CanonicalMeshWriteError(
                "canonical modern AABB padding exceeds 24 bits"
            )
        builder.write(
            "<3fI3fI",
            *minimum,
            padding,
            *maximum,
            padding << 8,
        )
    else:
        builder.write("<4f4f", *minimum, 1.0, *maximum, 1.0)
    builder.write("<q", builder.tell + 8)
    _write_lod_table(builder, lods, plan, profile, lod_offsets_by_id)
    return start


def _write_shadow_mesh(
    builder: _BinaryBuilder,
    shadow: object,
    plan: _TopologyPlan,
    profile: MeshGameProfile,
    lod_offsets_by_id: dict[int, int],
) -> int:
    start = builder.tell
    lods = list(getattr(shadow, "lods", ()))
    _write_lod_layout_header(builder, shadow, lods, plan, profile, "shadow mesh")
    if profile.shadow_has_legacy_pad:
        builder.write("<q", 0)
    table_pointer = builder.reserve(8)
    if profile.shadow_has_reserved_slots:
        builder.reserve(6 * 8)
    builder.patch(table_pointer, "<q", builder.tell)
    _write_lod_table(builder, lods, plan, profile, lod_offsets_by_id)
    return start


def _write_occluder_mesh(
    builder: _BinaryBuilder,
    occluder: object,
    plan: _TopologyPlan,
    profile: MeshGameProfile,
) -> int:
    start = builder.tell
    groups = list(getattr(occluder, "mesh_groups", ()))
    if not groups or len(groups) > 0xFF:
        raise CanonicalMeshWriteError("occluder mesh needs 1..255 groups")
    builder.write("<BBBB", len(groups), 0, 0, 0)
    factor = float(getattr(occluder, "lod_factor", 0.0))
    if not math.isfinite(factor) or factor < 0.0:
        raise CanonicalMeshWriteError("occluder LOD factor is invalid")
    builder.write("<f", factor)
    builder.write("<q", builder.tell + 8)
    _write_group_table(builder, groups, plan, profile)
    return start


def _write_skeleton(
    builder: _BinaryBuilder,
    mesh: MeshFile,
    profile: MeshGameProfile,
) -> int:
    start = builder.tell
    count = len(mesh.bones)
    if count != mesh.joint_count:
        raise CanonicalMeshWriteError("skeleton joint count does not match JointNode list")
    remap = tuple(int(value) for value in mesh.bone_remap_indices)
    if len(remap) > profile.max_deform_bones:
        raise CanonicalMeshWriteError(
            f"{profile.display_name} supports only {profile.max_deform_bones} deform bones"
        )
    if any(value < -0x8000 or value > 0x7FFF for value in remap):
        raise CanonicalMeshWriteError("skeleton remap index exceeds int16")
    if len(mesh.local_matrices) != count or len(mesh.world_matrices) != count or len(mesh.inverse_bind_matrices) != count:
        raise CanonicalMeshWriteError("skeleton matrix arrays do not match joint count")
    builder.write("<4i", count, len(remap), 0, 0)
    hierarchy_pointer = builder.reserve(8)
    local_pointer = builder.reserve(8)
    world_pointer = builder.reserve(8)
    inverse_pointer = builder.reserve(8)
    if remap:
        builder.write(f"<{len(remap)}h", *remap)
        builder.align(int(profile.skeleton_hierarchy_alignment))
    hierarchy = builder.tell
    builder.patch(hierarchy_pointer, "<q", hierarchy)
    for expected_index, node in enumerate(mesh.bones):
        index = int(node.index)
        if index != expected_index:
            raise CanonicalMeshWriteError("JointNode indices must form a dense ordered domain")
        segment_scale = int(bool(node.segment_scale))
        builder.write(
            "<5hH2h",
            index,
            int(node.parent_index),
            int(node.sibling_index),
            int(node.child_index),
            int(node.symmetry_index),
            segment_scale,
            0,
            0,
        )
    builder.align(16)

    def matrices(pointer_position: int, values: Sequence[Sequence[float]]) -> None:
        builder.patch(pointer_position, "<q", builder.tell)
        for matrix in values:
            flat = tuple(float(value) for value in matrix)
            if len(flat) != 16 or not all(math.isfinite(value) for value in flat):
                raise CanonicalMeshWriteError("skeleton matrix is not a finite Matrix4x4")
            builder.write("<16f", *flat)

    matrices(local_pointer, mesh.local_matrices)
    matrices(world_pointer, mesh.world_matrices)
    matrices(inverse_pointer, mesh.inverse_bind_matrices)
    builder.align(16)
    return start


def _write_blend_aabbs(builder: _BinaryBuilder, body_plan: _BlendBodyPlan) -> None:
    for target in body_plan.body.targets:
        minimum, maximum = body_plan.range_aabbs[id(target)]
        builder.write("<4f4f", *minimum, 0.0, *maximum, 0.0)


def _canonical_lod_map(
    values: Sequence[int],
    *,
    semantic_count: int,
    allocated_count: int,
    lod_index: int,
) -> tuple[int, ...]:
    if allocated_count < semantic_count:
        raise CanonicalMeshWriteError("blend-shape LOD-map allocation is undersized")
    if values:
        if len(values) < semantic_count:
            raise CanonicalMeshWriteError(
                "blend-shape LOD map is shorter than its semantic domain"
            )
        semantic = tuple(int(value) for value in values[:semantic_count])
    elif lod_index == 0:
        semantic = (-1,) * semantic_count
    else:
        semantic = tuple(range(semantic_count))
    if any(value < -0x80000000 or value > 0x7FFFFFFF for value in semantic):
        raise CanonicalMeshWriteError("blend-shape LOD-map value exceeds int32")
    return (*semantic, *((0,) * (allocated_count - semantic_count)))


def _write_blend_body(
    builder: _BinaryBuilder,
    body_plan: _BlendBodyPlan,
    profile: MeshGameProfile,
    lod_map_counts: tuple[int, int],
) -> int:
    body = body_plan.body
    targets = list(body.targets)
    none_targets = list(body.none_targets)
    all_targets = [*targets, *none_targets]
    start = builder.tell
    if profile.has_modern_blend_shape_targets:
        if len(targets) > 0xFFFF or len(none_targets) > 0xFFFF:
            raise CanonicalMeshWriteError("blend-shape target count exceeds uint16")
        builder.write("<HH", len(targets), len(none_targets))
        channel_offset = int(body.channel_offset)
        channel_count = int(body.channel_count)
        compression_type = int(body.compression_type)
        if not 0 <= channel_offset <= 0xFFFF or not 0 <= channel_count <= 0xFFFF:
            raise CanonicalMeshWriteError("blend-shape channel domain exceeds uint16")
        if not 0 <= compression_type <= 0xFF:
            raise CanonicalMeshWriteError("blend-shape compression type exceeds uint8")
        if profile.has_extended_blend_shape_layout:
            builder.write("<HHB7s", channel_offset, channel_count, compression_type, bytes(7))
        else:
            builder.write("<BHH7s", compression_type, channel_count, channel_offset, bytes(7))
        targets_pointer = builder.reserve(8)
        aabb_pointer = builder.reserve(8)
        target_map_pointer = builder.reserve(8)
        channel_map_pointer = builder.reserve(8)
    else:
        if len(targets) > 0xFFFF:
            raise CanonicalMeshWriteError("blend-shape target count exceeds uint16")
        builder.write("<HB13s", len(targets), int(body.compression_type), bytes(13))
        targets_pointer = builder.reserve(8)
        aabb_pointer = builder.reserve(8)
        if profile.blend_shape_layout.has_lod_maps:
            target_map_pointer = builder.reserve(8)
            channel_map_pointer = builder.reserve(8)
        else:
            target_map_pointer = channel_map_pointer = -1
    builder.align(16)

    if all_targets:
        builder.patch(targets_pointer, "<q", builder.tell)
    target_records: list[tuple[int, object, bool]] = []
    for target_index, target in enumerate(all_targets):
        active = target_index < len(targets) and bool(target.is_blend_shape)
        if profile.has_modern_blend_shape_targets:
            channel_location = int(target.channel_location)
            if profile.has_extended_blend_shape_layout:
                channel_location += int(body.channel_offset)
            channel_count = int(target.channel_count)
            same_target = int(target.same_target_id)
            ranges = list(target.ranges)
            if not (
                0 <= channel_location <= 0xFFFF
                and 0 <= channel_count <= 0xFFFF
                and 0 <= same_target <= 0xFFFF
                and len(ranges) <= 0xFF
            ):
                raise CanonicalMeshWriteError("blend-shape target fields exceed native widths")
            builder.write(
                "<HHHBB",
                channel_location,
                channel_count,
                same_target,
                len(ranges),
                int(active),
            )
            range_pointer = builder.reserve(8)
            target_records.append((range_pointer, target, active))
        else:
            ranges = list(target.ranges)
            if len(ranges) != 1:
                raise CanonicalMeshWriteError(
                    "pre-RE4 blend-shape targets require exactly one semantic range"
                )
            item = ranges[0]
            part_index = int(target.part_index or 0)
            if not 0 <= part_index <= 0xFFFF or not 0 <= int(target.channel_count) <= 0xFFFF:
                raise CanonicalMeshWriteError("legacy blend-shape target field exceeds uint16")
            builder.write(
                "<IIHHI",
                int(item.base_vertex_location) - body_plan.vertex_base if item.vertex_count else 0,
                int(item.vertex_count),
                part_index,
                int(target.channel_count),
                0,
            )

    if profile.has_modern_blend_shape_targets:
        for range_pointer, target, active in target_records:
            ranges = list(target.ranges)
            if not ranges:
                continue
            builder.patch(range_pointer, "<q", builder.tell)
            for range_index, item in enumerate(ranges):
                location = (
                    body_plan.range_locations.get((id(target), range_index), 0)
                    if active
                    else 0
                )
                part_id = int(getattr(item, "part_id", 0))
                surface_range_id = int(
                    getattr(item, "surface_range_id", 0)
                )
                if not (
                    0 <= part_id <= 0xFF
                    and 0 <= surface_range_id <= 0xFF
                ):
                    raise CanonicalMeshWriteError(
                        "blend-shape range identity exceeds its byte domain"
                    )
                if profile.blend_shape_range_has_part_surface_identity:
                    if (
                        profile.blend_shape_range_identity_is_corpus_zero
                        and (part_id or surface_range_id)
                    ):
                        raise CanonicalMeshWriteError(
                            f"{profile.display_name} has no shipped nonzero "
                            "blend-shape range identity"
                        )
                    packed_identity = part_id | (surface_range_id << 8)
                else:
                    if part_id or surface_range_id:
                        raise CanonicalMeshWriteError(
                            f"{profile.display_name} has no blend-shape "
                            "part/surface range identity"
                        )
                    packed_identity = 0
                builder.write(
                    "<IIII",
                    int(item.base_vertex_location),
                    int(location),
                    int(item.vertex_count),
                    packed_identity,
                )
    builder.align(16)

    if targets:
        builder.patch(aabb_pointer, "<q", builder.tell)
        _write_blend_aabbs(builder, body_plan)
    builder.align(16)

    target_semantic_count, channel_semantic_count = lod_map_counts
    target_allocated_count = lod_map_allocation(target_semantic_count)
    channel_allocated_count = lod_map_allocation(channel_semantic_count)

    if target_allocated_count:
        builder.patch(target_map_pointer, "<q", builder.tell)
        values = _canonical_lod_map(
            body.target_lod_map,
            semantic_count=target_semantic_count,
            allocated_count=target_allocated_count,
            lod_index=int(body.lod_index),
        )
        builder.write(f"<{len(values)}i", *values)
    if channel_allocated_count:
        builder.patch(channel_map_pointer, "<q", builder.tell)
        values = _canonical_lod_map(
            body.channel_lod_map,
            semantic_count=channel_semantic_count,
            allocated_count=channel_allocated_count,
            lod_index=int(body.lod_index),
        )
        builder.write(f"<{len(values)}i", *values)
    builder.align(16)
    return start


def _write_blend_shapes(
    builder: _BinaryBuilder,
    mesh: MeshFile,
    profile: MeshGameProfile,
    payload_plan: _BlendPayloadPlan,
) -> int:
    data = mesh.blend_shape_data
    if data is None or not data.bodies:
        raise CanonicalMeshWriteError("blend-shape section has no semantic bodies")
    bodies = sorted(data.bodies, key=lambda item: int(item.lod_index))
    if tuple(int(item.lod_index) for item in bodies) != tuple(range(len(bodies))):
        raise CanonicalMeshWriteError(
            "blend-shape bodies must form a dense LOD-index domain"
        )
    if profile.blend_shape_layout is BlendShapeLayout.LEGACY:
        if len(bodies) != 1:
            raise CanonicalMeshWriteError("legacy blend-shape layout owns one body")
        body = bodies[0]
        body_plan = payload_plan.body_plan(body)
        targets = list(body.targets)
        start = builder.tell
        targets_pointer = builder.reserve(24)
        builder.align(16)
        target_offset = builder.tell
        payload_cursor = 0
        for target_index, target in enumerate(targets):
            ranges = list(target.ranges)
            if len(ranges) != 1:
                raise CanonicalMeshWriteError(
                    "legacy blend-shape target needs one semantic range"
                )
            item = ranges[0]
            location = body_plan.range_locations.get((id(target), 0), 0)
            if location != payload_cursor:
                raise CanonicalMeshWriteError(
                    "legacy blend-shape payload must be densely target-ordered"
                )
            builder.write(
                "<IIHHI",
                int(item.base_vertex_location),
                int(item.vertex_count),
                0,
                int(target.channel_count),
                0,
            )
            payload_cursor += int(item.vertex_count) * int(target.channel_count)
        builder.write("<ii", 0, 0)
        builder.align(16)
        aabb_offset = builder.tell if targets else 0
        if targets:
            _write_blend_aabbs(builder, body_plan)
        builder.align(16)
        builder.patch(
            targets_pointer,
            "<HHIqq",
            len(targets),
            int(body.compression_type),
            0,
            target_offset,
            aabb_offset,
        )
        return start

    start = builder.tell
    if len(bodies) > 0xFF:
        raise CanonicalMeshWriteError("blend-shape LOD count exceeds uint8")
    if profile.has_extended_blend_shape_layout:
        builder.write("<B15s", len(bodies), bytes(15))
    else:
        builder.write("<B7s", len(bodies), bytes(7))
    if profile.has_blend_shape_lod_maps:
        bodies_pointer = builder.reserve(8)
        builder.reserve(8)
        builder.reserve(8)
    else:
        bodies_pointer = builder.reserve(8)
    builder.align(16)
    builder.patch(bodies_pointer, "<q", builder.tell)
    body_pointer_positions = [builder.reserve(8) for _ in bodies]
    builder.align(16)
    body_offsets_by_lod: dict[int, int] = {}
    for pointer_position, body, lod_map_counts in zip(body_pointer_positions, bodies,
                                                      blend_shape_lod_map_counts(bodies, profile)):
        alias_of_lod = getattr(body, "alias_of_lod", None)
        if alias_of_lod is not None:
            try:
                owner_offset = body_offsets_by_lod[int(alias_of_lod)]
            except KeyError as exc:
                raise CanonicalMeshWriteError(
                    f"blend-shape LOD {body.lod_index} aliases unavailable "
                    f"LOD {alias_of_lod}"
                ) from exc
            builder.patch(pointer_position, "<q", owner_offset)
            body_offsets_by_lod[int(body.lod_index)] = owner_offset
            continue
        body_offset = builder.tell
        body_offsets_by_lod[int(body.lod_index)] = body_offset
        builder.patch(pointer_position, "<q", body_offset)
        _write_blend_body(builder, payload_plan.body_plan(body), profile, lod_map_counts)
    return start


def _write_blend_shape_bone_modifier(
    builder: _BinaryBuilder,
    modifier: object,
) -> int:
    start = builder.tell
    effectors = list(modifier.effectors)
    builder.write("<II", len(effectors), 0)
    pointers_offset = builder.tell + 8
    builder.write("<q", pointers_offset if effectors else 0)
    pointer_positions = [builder.reserve(8) for _ in effectors]
    builder.align(16)
    for pointer_position, effector in zip(pointer_positions, effectors):
        builder.patch(pointer_position, "<q", builder.tell)
        indices = tuple(int(value) for value in effector.vertex_indices)
        positions = np.asarray(effector.vertex_positions, dtype=np.float32).reshape(-1, 3)
        if len(indices) != len(positions):
            raise CanonicalMeshWriteError(
                "blend-shape bone-modifier index/reference counts disagree"
            )
        if any(value < 0 or value > 0xFFFFFFFF for value in indices):
            raise CanonicalMeshWriteError("bone-modifier vertex index exceeds uint32")
        builder.write("<II", int(effector.joint_index), len(indices))
        indices_pointer = builder.reserve(8)
        positions_pointer = builder.reserve(8)
        builder.align(16)
        if indices:
            builder.patch(indices_pointer, "<q", builder.tell)
            builder.write(f"<{len(indices)}I", *indices)
            builder.align(16)
            builder.patch(positions_pointer, "<q", builder.tell)
            if not np.isfinite(positions).all():
                raise CanonicalMeshWriteError(
                    "bone-modifier reference position is nonfinite"
                )
            builder.write_bytes(positions.astype("<f4").tobytes())
            builder.align(16)
    return start


@dataclass(slots=True)
class _GroupPivotSlot:
    layout_offset: int
    data_pointer_position: int
    values: tuple[tuple[float, float, float], ...]


def _write_group_pivot_layout(
    builder: _BinaryBuilder,
    pivots: object,
    profile: MeshGameProfile,
) -> _GroupPivotSlot:
    start = builder.tell
    values = tuple(
        tuple(float(value) for value in item) for item in pivots.pivots
    )
    if any(
        len(item) != 3
        or not all(math.isfinite(value) for value in item)
        for item in values
    ):
        raise CanonicalMeshWriteError("group pivot table contains an invalid Float3")
    builder.write("<ii", len(values) * 12, 0)
    data_pointer = builder.reserve(8)
    if values and not profile.group_pivot_payload_is_trailing:
        builder.patch(data_pointer, "<q", builder.tell)
        for value in values:
            builder.write("<3f", *value)
        builder.align(16)
        values = ()
    return _GroupPivotSlot(start, data_pointer, values)


def _write_group_pivot_buffer(
    builder: _BinaryBuilder,
    slot: _GroupPivotSlot,
) -> None:
    if not slot.values:
        return
    builder.patch(slot.data_pointer_position, "<q", builder.tell)
    for value in slot.values:
        builder.write("<3f", *value)


def _write_bounding_boxes(
    builder: _BinaryBuilder,
    bounds: object,
    profile: MeshGameProfile,
) -> int:
    start = builder.tell
    boxes = list(bounds.boxes)
    builder.write("<ii", len(boxes), 0)
    data_pointer = builder.reserve(8)
    if boxes:
        builder.patch(data_pointer, "<q", builder.tell)
        homogeneous_w = float(profile.canonical_bounding_box_w)
        if not math.isfinite(homogeneous_w):
            raise CanonicalMeshWriteError(
                "canonical bounding-box W lane must be finite"
            )
        for box in boxes:
            minimum = tuple(float(value) for value in box.minimum)
            maximum = tuple(float(value) for value in box.maximum)
            if len(minimum) != 3 or len(maximum) != 3 or not all(
                math.isfinite(value) for value in (*minimum, *maximum)
            ):
                raise CanonicalMeshWriteError("bounding-box table contains invalid bounds")
            builder.write(
                "<4f4f",
                *minimum,
                homogeneous_w,
                *maximum,
                homogeneous_w,
            )
    builder.align(16)
    return start


def _resolved_name_indices(
    names: Sequence[str],
    requested: Sequence[int],
    semantic_names: Sequence[str],
    count: int,
    label: str,
) -> tuple[int, ...]:
    if len(requested) == count and all(0 <= int(value) < len(names) for value in requested):
        return tuple(int(value) for value in requested)
    first_index: dict[str, int] = {}
    for index, value in enumerate(names):
        first_index.setdefault(str(value), index)
    if len(semantic_names) != count:
        raise CanonicalMeshWriteError(f"{label} name domain does not match its semantic count")
    result: list[int] = []
    for value in semantic_names:
        try:
            result.append(first_index[str(value)])
        except KeyError as exc:
            raise CanonicalMeshWriteError(f"{label} name {value!r} is absent from the name table") from exc
    return tuple(result)


def _write_i16_index_section(builder: _BinaryBuilder, values: Sequence[int]) -> int:
    start = builder.tell
    if any(int(value) < -0x8000 or int(value) > 0x7FFF for value in values):
        raise CanonicalMeshWriteError("name-table index exceeds int16")
    if values:
        builder.write(f"<{len(values)}h", *(int(value) for value in values))
    builder.align(16)
    return start


def _write_name_table(
    builder: _BinaryBuilder,
    names: Sequence[str],
    profile: MeshGameProfile,
) -> int:
    start = builder.tell
    pointer_positions = [builder.reserve(8) for _ in names]
    builder.align(int(profile.name_string_data_alignment))
    for pointer_position, value in zip(pointer_positions, names):
        builder.patch(pointer_position, "<q", builder.tell)
        try:
            encoded = str(value).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CanonicalMeshWriteError("mesh name is not valid UTF-8") from exc
        if b"\x00" in encoded:
            raise CanonicalMeshWriteError("mesh name contains an embedded NUL")
        builder.write_bytes(encoded + b"\x00")
    builder.align(16)
    return start


def _write_sdf_path(
    builder: _BinaryBuilder,
    value: str,
    profile: MeshGameProfile,
) -> int:
    start = builder.tell
    encoded = str(value).encode("utf-16le") + b"\x00\x00"
    if profile.header_layout in {MeshHeaderLayout.DD2, MeshHeaderLayout.WILDS}:
        builder.write_bytes(encoded)
    else:
        pointer = builder.reserve(8)
        builder.patch(pointer, "<q", builder.tell)
        builder.write_bytes(encoded)
    builder.align(16)
    return start


def _encode_element_headers(headers: Sequence[MeshBufferItemHeader]) -> bytes:
    output = bytearray()
    for item in headers:
        semantic = int(item.type)
        stride = int(item.size)
        offset = int(item.offset)
        if not 0 <= semantic <= 0xFFFF or not 0 <= stride <= 0xFFFF:
            raise CanonicalMeshWriteError("vertex-element type/stride exceeds uint16")
        if not 0 <= offset <= 0xFFFFFFFF:
            raise CanonicalMeshWriteError("vertex-element offset exceeds uint32")
        output.extend(struct.pack("<HHI", semantic, stride, offset))
    return bytes(output)


def _encode_buffer_layout_record(
    profile: MeshGameProfile,
    plan: _BufferBuildPlan,
    *,
    element_headers_offset: int,
    vertex_buffer_offset: int,
    separate_weight_offset: int,
    streaming: bool,
) -> bytes:
    output = _BinaryBuilder()
    output.write("<q", int(element_headers_offset))
    output.write("<q", int(vertex_buffer_offset))
    total_size = len(plan.universal_bytes)
    vertex_size = int(plan.vertex_prefix_size)
    index_size = total_size - vertex_size

    def optional_relative_offset(value: int) -> int:
        encoded = int(value)
        if not encoded and profile.optional_gpu_offsets_use_relative_null:
            encoded = -int(vertex_buffer_offset)
        if not -(1 << 31) <= encoded < (1 << 31):
            raise CanonicalMeshWriteError(
                "buffer-relative optional GPU offset exceeds int32"
            )
        return encoded
    if streaming:
        output.write("<q", int(separate_weight_offset))
        output.write("<II", total_size, vertex_size)
    elif profile.has_shape_key_weight_pointer or profile.supports_additional_vertex_weights:
        output.write("<q", int(separate_weight_offset))
        if profile.buffer_layout is MeshBufferLayout.DD2:
            output.write("<II", total_size, vertex_size)
        else:
            output.write("<ii", total_size, vertex_size)
    else:
        output.write("<q", int(vertex_buffer_offset + plan.main_index_offset))
        if profile.has_rt_buffer_padding:
            output.write("<q", 0)
        if profile.has_split_legacy_buffer_sizes:
            output.write("<II", vertex_size, index_size)
        elif profile.legacy_buffer_sizes_are_vertex_and_index:
            output.write("<ii", vertex_size, index_size)
        else:
            output.write("<ii", total_size, 0)
    output.write("<HH", plan.element_count, plan.total_element_count)
    if profile.has_shape_key_weight_size:
        prefix_size = (
            0 if profile.additional_vertex_weights_size_in_modern_tail
            else len(plan.separate_weight_bytes)
        )
        output.write("<iiii", prefix_size, 0, 0, 0)
    if profile.auxiliary_index_offsets_are_boundaries and not (
        vertex_size
        <= int(plan.shadow_index_offset)
        <= int(plan.occluder_index_offset)
        <= total_size
    ):
        raise CanonicalMeshWriteError(
            "auxiliary index boundaries are not monotonic within the GPU buffer"
        )
    # Legacy-RT offsets are relative to the index buffer; RE4+ uses the start of the universal allocation.
    auxiliary_offset_base = 0 if profile.has_auxiliary_index_offsets else plan.main_index_offset

    def encoded_auxiliary_offset(value: int) -> int:
        if not value and not profile.has_auxiliary_index_offsets:
            return 0
        return int(value) - int(auxiliary_offset_base)

    shadow_index_offset = encoded_auxiliary_offset(plan.shadow_index_offset)
    occluder_index_offset = encoded_auxiliary_offset(plan.occluder_index_offset)
    if min(shadow_index_offset, occluder_index_offset) < 0:
        raise CanonicalMeshWriteError(
            "auxiliary index offset precedes its profile-owned offset base"
        )
    output.write("<II", shadow_index_offset, occluder_index_offset)
    if profile.has_auxiliary_index_offsets:
        if profile.has_wilds_buffer_tail:
            output.write(
                "<iiiii",
                optional_relative_offset(plan.normal_local_index_offset),
                optional_relative_offset(plan.normal_redirected_list_offset),
                optional_relative_offset(plan.blend_shape_offset),
                (len(plan.separate_weight_bytes)
                 if profile.additional_vertex_weights_size_in_modern_tail else 0),
                plan.buffer_index,
            )
        else:
            additional_size = (
                len(plan.separate_weight_bytes)
                if profile.supports_additional_vertex_weights
                else 0
            )
            output.write(
                "<iIIQ",
                optional_relative_offset(plan.blend_shape_offset),
                additional_size,
                plan.buffer_index,
                0,
            )
    else:
        output.write("<i", optional_relative_offset(plan.blend_shape_offset))
    expected = (
        profile.streaming_header_size
        if streaming or profile.header_layout is not MeshHeaderLayout.LEGACY
        else (56 if profile.has_rt_buffer_padding else 48)
    )
    if len(output.data) != expected:
        raise CanonicalMeshWriteError(
            f"{profile.display_name} BufferLayout encoded {len(output.data)} "
            f"bytes; profile declares {expected}"
        )
    return output.bytes()


@dataclass(slots=True)
class _BufferRecordSlot:
    plan: _BufferBuildPlan
    offset: int
    streaming: bool
    element_headers_offset: int = 0
    vertex_buffer_offset: int = 0
    separate_weight_offset: int = 0


def _write_streaming_info(
    builder: _BinaryBuilder,
    streaming_plans: Sequence[_BufferBuildPlan],
    slot_count: int,
) -> tuple[int, bytes, dict[int, int]]:
    start = builder.tell
    by_index = {int(item.buffer_index): item for item in streaming_plans}
    if len(by_index) != len(streaming_plans) or any(index <= 0 for index in by_index):
        raise CanonicalMeshWriteError("streaming buffer IDs must be unique and positive")
    if slot_count < max(by_index, default=0):
        raise CanonicalMeshWriteError(
            "StreamingInfo slot count does not cover every streaming buffer"
        )
    external = bytearray()
    starts: dict[int, int] = {}
    storage_starts: dict[int, int] = {}
    entries: list[tuple[int, int]] = []
    for buffer_index in range(1, slot_count + 1):
        plan = by_index.get(buffer_index)
        if plan is None:
            entries.append((len(external), 0))
            continue
        storage_plan = plan
        storage_index = int(plan.buffer_index)
        if plan.payload_alias_of is not None:
            storage_index = int(plan.payload_alias_of)
            try:
                storage_plan = by_index[storage_index]
            except KeyError as exc:
                raise CanonicalMeshWriteError(
                    f"streaming buffer {buffer_index} aliases missing buffer "
                    f"{storage_index}"
                ) from exc
            if storage_plan.payload_alias_of is not None:
                raise CanonicalMeshWriteError(
                    "streaming payload aliases must point directly to an owner"
                )
            if plan.universal_bytes != storage_plan.universal_bytes:
                raise CanonicalMeshWriteError(
                    f"streaming buffer {buffer_index} alias payload disagrees "
                    f"with owner {storage_index}"
                )
        if storage_index not in storage_starts:
            _append_zero_alignment(external)
            storage_starts[storage_index] = len(external)
            external.extend(storage_plan.universal_bytes)
        starts[buffer_index] = storage_starts[storage_index]
        entries.append((starts[buffer_index], len(storage_plan.universal_bytes)))
    builder.write("<ii", len(entries), 0)
    entries_pointer = builder.tell + 8
    builder.write("<q", entries_pointer if entries else 0)
    for entry_start, entry_size in entries:
        builder.write("<II", entry_start, entry_size)
    builder.align(16)
    return start, bytes(external), starts


def _patch_header_file_size(builder: _BinaryBuilder) -> None:
    if builder.tell > 0xFFFFFFFF:
        raise CanonicalMeshWriteError("MESH resource exceeds uint32 fileSize")
    builder.patch(8, "<I", builder.tell)  # fileSize follows magic and version.


def _write_parts_name_hashes(builder: _BinaryBuilder, mesh: MeshFile) -> int:
    values = tuple(int(value) for value in mesh.parts_name_hashes)
    if any(value < 0 or value > 0xFFFFFFFF for value in values):
        raise CanonicalMeshWriteError("part-name hash exceeds uint32")
    start = builder.tell
    if values:
        builder.write(f"<{len(values)}I", *values)
    builder.align(16)
    return start


def _pack_blend_names(mesh, profile):
    from .blend_shape import blend_shape_name_offsets

    blend = mesh.blend_shape_data
    if blend is None or not blend.bodies:
        return mesh
    offsets = blend_shape_name_offsets(blend, profile)
    slots = {}
    for body in blend.bodies:
        for channel in body.channels:
            if not 0 <= channel.name_slot < len(mesh.blend_shape_indices):
                raise CanonicalMeshWriteError("blend-shape channel has no semantic name")
            slot = offsets[body.lod_index] + channel.index
            index = mesh.blend_shape_indices[channel.name_slot]
            if not 0 <= index < len(mesh.names):
                raise CanonicalMeshWriteError("blend-shape channel name leaves the string table")
            if slot in slots and mesh.names[slots[slot]] != mesh.names[index]:
                raise CanonicalMeshWriteError("shared blend-shape name slot has conflicting channel names")
            slots[slot] = index
    count = max(offsets[b.lod_index] + b.channel_count for b in blend.bodies)
    result = copy(mesh)
    result.names = list(mesh.names)
    result.blend_shape_indices = list(mesh.blend_shape_indices)
    while len(result.blend_shape_indices) < count:
        slot = len(result.blend_shape_indices)
        name = mesh.names[slots[slot]] if slot in slots else ""
        result.blend_shape_indices.append(len(result.names))
        result.names.append(name)
    for slot, index in slots.items():
        result.blend_shape_indices[slot] = index
    return result


def serialize_mesh(mesh: MeshFile) -> CanonicalMeshWriteResult:
    if not isinstance(mesh, MeshFile):
        raise TypeError("serialize_mesh expects MeshFile")
    if mesh.load_mode is not MeshLoadMode.FULL:
        raise CanonicalMeshWriteError("canonical serialization requires FULL mesh data")
    profile = mesh.profile
    if profile is None or not profile.canonical_import_contract:
        raise CanonicalMeshWriteError("mesh profile has no complete canonical contract")
    mesh = _pack_blend_names(mesh, profile)
    file_versions = {
        file_version
        for internal_version, file_version in profile.canonical_version_pairs
        if internal_version == mesh.header.version
    }
    if len(file_versions) != 1:
        raise CanonicalMeshWriteError(
            "mesh version must identify one verified canonical resource version in its profile"
        )
    if mesh.header.magic == MESH_MAGIC:
        result = _serialize_ordinary_mesh(mesh, profile)
    elif mesh.header.magic == MPLY_MAGIC:
        result = _serialize_mply(mesh, profile)
    else:
        raise CanonicalMeshWriteError(f"unsupported mesh magic 0x{mesh.header.magic:08X}")
    try:
        rebuilt = MeshFile()
        rebuilt.read(
            result.main,
            file_version=file_versions.pop(),
            streaming_data=result.streaming,
        )
        entries = rebuilt.streaming_info.entries if rebuilt.streaming_info else ()
        streaming_size = max((entry.start + entry.size for entry in entries), default=0)
        if len(result.streaming or b"") != streaming_size:
            raise ValueError(
                "generated streaming companion must exactly cover its declared allocations"
            )
        rebuilt.assert_import_safe()
    except (ValueError, EOFError, struct.error) as exc:
        raise CanonicalMeshWriteError(
            f"{profile.display_name} canonical output is not authoring-safe: {exc}"
        ) from exc
    return replace(result, verified=rebuilt)


def _write_material_and_bone_indices(
    builder: _BinaryBuilder,
    header_plan: object,
    mesh: MeshFile,
    names: Sequence[str],
    material_count: int,
) -> None:
    if material_count:
        material_indices = _resolved_name_indices(
            names,
            mesh.material_indices,
            mesh.material_names,
            material_count,
            "material",
        )
        material_offset = _write_i16_index_section(builder, material_indices)
        header_plan.patch_pointer(builder, "material_indices_offset", material_offset)
    if mesh.joint_count:
        if len(mesh.bone_indices) == mesh.joint_count and all(
            0 <= int(value) < len(names) for value in mesh.bone_indices
        ):
            bone_indices = tuple(int(value) for value in mesh.bone_indices)
        else:
            bone_indices = tuple(range(material_count, material_count + mesh.joint_count))
            if any(value >= len(names) for value in bone_indices):
                raise CanonicalMeshWriteError(
                    "skeleton has no complete semantic name-index domain"
                )
        bone_offset = _write_i16_index_section(builder, bone_indices)
        header_plan.patch_pointer(builder, "bone_indices_offset", bone_offset)


def _write_bounds_and_sdf_path(
    builder: _BinaryBuilder,
    header_plan: object,
    mesh: MeshFile,
    profile: MeshGameProfile,
) -> None:
    if mesh.bounding_boxes is not None and mesh.bounding_boxes.boxes:
        bounds_offset = _write_bounding_boxes(builder, mesh.bounding_boxes, profile)
        header_plan.patch_pointer(builder, "bounds_offset", bounds_offset)
    if mesh.sdf_path:
        sdf_offset = _write_sdf_path(builder, mesh.sdf_path, profile)
        header_plan.patch_pointer(builder, "sdf_path_offset", sdf_offset)


def _serialize_ordinary_mesh(
    mesh: MeshFile,
    profile: MeshGameProfile,
) -> CanonicalMeshWriteResult:
    if len(mesh.meshes) > 1:
        raise CanonicalMeshWriteError(
            "canonical ordinary MESH owns at most one primary MeshData layout"
        )
    if mesh.mesh_buffer is None:
        raise CanonicalMeshWriteError("ordinary MESH has no MeshBuffer")

    topology_plan = _build_topology_plan(mesh)
    normal_topologies = (
        _normal_topologies(mesh, topology_plan)
        if mesh.normal_recalc_data is not None
        else ()
    )
    blend_plan = _plan_blend_payloads(mesh, profile)
    buffer_plans = _build_buffer_plans(
        mesh,
        profile,
        topology_plan,
        blend_plan,
        normal_topologies,
    )
    resident = buffer_plans[0]
    if not profile.skip_empty_stream_entries:
        for index in range(1, _semantic_external_buffer_count(mesh) + 1):
            if index not in buffer_plans:
                headers = tuple(MeshBufferItemHeader(h.type, h.size, 0) for h in resident.main_headers)
                buffer_plans[index] = _BufferBuildPlan(index, headers, (), bytearray())
    streaming_plans = [
        buffer_plans[index]
        for index in sorted(buffer_plans)
        if index != 0 and (buffer_plans[index].universal_bytes or not profile.skip_empty_stream_entries)
    ]
    if streaming_plans and not profile.supports_streaming_buffers:
        raise CanonicalMeshWriteError(
            f"{profile.display_name} has no external streaming-buffer layout"
        )
    if any(item.separate_weight_bytes for item in streaming_plans):
        raise CanonicalMeshWriteError(
            "separate skin-weight streams are not defined for external buffers"
        )

    builder = _BinaryBuilder()
    header_plan = _write_header_placeholder(builder, mesh, profile)
    lod_offsets_by_id: dict[int, int] = {}

    # Sections follow the order of shipped files.
    if mesh.meshes:
        main_offset = _write_mesh_data(
            builder,
            mesh.meshes[0],
            topology_plan,
            profile,
            lod_offsets_by_id,
        )
        header_plan.patch_pointer(builder, "lods_offset", main_offset)
        builder.align(16)

    if mesh.shadow_mesh is not None:
        shadow_offset = _write_shadow_mesh(
            builder,
            mesh.shadow_mesh,
            topology_plan,
            profile,
            lod_offsets_by_id,
        )
        header_plan.patch_pointer(builder, "shadow_lods_offset", shadow_offset)
        builder.align(16)
    if mesh.occluder_mesh is not None:
        occluder_offset = _write_occluder_mesh(
            builder,
            mesh.occluder_mesh,
            topology_plan,
            profile,
        )
        header_plan.patch_pointer(builder, "occluder_mesh_offset", occluder_offset)
        builder.align(16)

    if mesh.joint_count or mesh.bones:
        skeleton_offset = _write_skeleton(builder, mesh, profile)
        header_plan.patch_pointer(builder, "bones_offset", skeleton_offset)

    if mesh.normal_recalc_data is not None:
        builder.align(max(4, int(profile.normal_recalc_section_alignment)))
        normal_offset = builder.tell
        normal_bytes = _serialize_normal_section(
            mesh,
            profile,
            topology_plan,
            normal_topologies,
            normal_offset,
        )
        builder.write_bytes(normal_bytes)
        builder.align(16)
        header_plan.patch_pointer(builder, "normal_recalc_offset", normal_offset)

    blend_data = mesh.blend_shape_data
    if blend_data is not None and blend_data.bodies:
        blend_offset = _write_blend_shapes(
            builder,
            mesh,
            profile,
            blend_plan,
        )
        header_plan.patch_pointer(builder, "blend_shapes_offset", blend_offset)
        builder.align(16)

    pivot_slot: _GroupPivotSlot | None = None
    if (
        mesh.group_pivots is not None
        and mesh.group_pivots.pivots
        and not profile.group_pivot_payload_is_trailing
    ):
        # DMC5-DD2 write the pivot descriptor and its Float3 array after normal/blend data.
        pivot_slot = _write_group_pivot_layout(builder, mesh.group_pivots, profile)
        header_plan.patch_pointer(builder, "floats_offset", pivot_slot.layout_offset)

    if blend_data is not None and blend_data.bone_modifier is not None:
        if "blend_shape_bone_modifier_offset" not in header_plan.pointers:
            raise CanonicalMeshWriteError(
                "this header has no independent blend-shape bone-modifier pointer"
            )
        modifier_offset = _write_blend_shape_bone_modifier(
            builder,
            blend_data.bone_modifier,
        )
        header_plan.patch_pointer(
            builder,
            "blend_shape_bone_modifier_offset",
            modifier_offset,
        )
        builder.align(16)

    names = tuple(str(value) for value in mesh.names)
    material_count = int(mesh.meshes[0].material_count) if mesh.meshes else 0
    _write_material_and_bone_indices(builder, header_plan, mesh, names, material_count)

    blend_name_count = len(names) - material_count - mesh.joint_count
    if blend_name_count < 0:
        raise CanonicalMeshWriteError(
            "material/joint name domains exceed the global name table"
        )
    if blend_name_count:
        if blend_data is None:
            raise CanonicalMeshWriteError(
                "global blend-shape names exist without blend-shape metadata"
            )
        if len(mesh.blend_shape_indices) == blend_name_count and all(
            0 <= int(value) < len(names) for value in mesh.blend_shape_indices
        ):
            blend_indices = tuple(int(value) for value in mesh.blend_shape_indices)
        else:
            first = material_count + mesh.joint_count
            blend_indices = tuple(range(first, len(names)))
        blend_indices_offset = _write_i16_index_section(builder, blend_indices)
        header_plan.patch_pointer(
            builder,
            "blend_shape_indices_offset",
            blend_indices_offset,
        )

    if names:
        name_table_offset = _write_name_table(builder, names, profile)
        header_plan.patch_pointer(builder, "name_offsets_offset", name_table_offset)

    if mesh.parts_name_hashes:
        if not mesh.meshes:
            raise CanonicalMeshWriteError(
                "part-name hashes require primary MeshData topology"
            )
        expected_hash_count = (
            len(mesh.meshes[0].lods[0].mesh_groups)
            if mesh.meshes[0].lods
            else 0
        )
        if len(mesh.parts_name_hashes) != expected_hash_count:
            raise CanonicalMeshWriteError(
                "part-name hash count does not match LOD0 part topology"
            )
        hashes_offset = _write_parts_name_hashes(builder, mesh)
        header_plan.patch_pointer(builder, "parts_name_hash_offset", hashes_offset)

    _write_bounds_and_sdf_path(builder, header_plan, mesh, profile)

    builder.align(16)
    buffer_layout_offset = builder.tell
    def reserve_record(plan: _BufferBuildPlan, streaming: bool) -> _BufferRecordSlot:
        size = len(_encode_buffer_layout_record(
            profile,
            plan,
            element_headers_offset=0,
            vertex_buffer_offset=0,
            separate_weight_offset=0,
            streaming=streaming,
        ))
        return _BufferRecordSlot(plan, builder.reserve(size), streaming)

    resident_slot = reserve_record(resident, False)
    stream_slots = [reserve_record(item, True) for item in streaming_plans]
    buffer_pointer_name = (
        "mesh_offset"
        if profile.header_layout is MeshHeaderLayout.LEGACY
        else "buffer_layout_offset"
    )
    header_plan.patch_pointer(builder, buffer_pointer_name, buffer_layout_offset)
    if "buffer_headers_offset" in header_plan.pointers:
        header_plan.patch_pointer(builder, "buffer_headers_offset", buffer_layout_offset)

    if (
        mesh.group_pivots is not None
        and mesh.group_pivots.pivots
        and profile.group_pivot_payload_is_trailing
    ):
        pivot_slot = _write_group_pivot_layout(builder, mesh.group_pivots, profile)
        header_plan.patch_pointer(builder, "floats_offset", pivot_slot.layout_offset)

    external_data = b""
    external_starts: dict[int, int] = {}
    if "streaming_info_offset" in header_plan.pointers:
        streaming_info_offset, external_data, external_starts = _write_streaming_info(
            builder,
            streaming_plans,
            _semantic_external_buffer_count(mesh),
        )
        header_plan.patch_pointer(
            builder,
            "streaming_info_offset",
            streaming_info_offset,
        )

    for slot in (resident_slot, *stream_slots):
        builder.align(16)
        slot.element_headers_offset = builder.tell
        builder.write_bytes(
            _encode_element_headers(
                (*slot.plan.main_headers, *slot.plan.extra_headers)
            )
        )

    builder.align(16)
    resident_slot.vertex_buffer_offset = builder.tell
    builder.write_bytes(resident.universal_bytes)
    if resident.separate_weight_bytes:
        builder.align(16)
        resident_slot.separate_weight_offset = builder.tell
        builder.write_bytes(resident.separate_weight_bytes)
    builder.align(16)
    if pivot_slot is not None:
        _write_group_pivot_buffer(builder, pivot_slot)

    for slot in stream_slots:
        slot.vertex_buffer_offset = external_starts[slot.plan.buffer_index]
    for slot in (resident_slot, *stream_slots):
        encoded_record = _encode_buffer_layout_record(
            profile,
            slot.plan,
            element_headers_offset=slot.element_headers_offset,
            vertex_buffer_offset=slot.vertex_buffer_offset,
            separate_weight_offset=slot.separate_weight_offset,
            streaming=slot.streaming,
        )
        builder.patch_bytes(slot.offset, encoded_record)

    if "vertices_offset" in header_plan.pointers:
        header_plan.patch_pointer(
            builder,
            "vertices_offset",
            resident_slot.vertex_buffer_offset,
        )
    if header_plan.on_memory_size is not None:
        # DD2 uses this field as the resident GPU backing locator (the mirrored blob base for MPLY).
        builder.patch(
            header_plan.on_memory_size,
            "<Q",
            resident_slot.vertex_buffer_offset,
        )
    _patch_header_file_size(builder)
    return CanonicalMeshWriteResult(
        main=builder.bytes(),
        streaming=(external_data if external_data else None),
    )


@dataclass(slots=True)
class _MplyChunkPlan:
    chunk: object
    faces: np.ndarray
    center: tuple[float, float, float]
    relative_aabb: tuple[int, int, int, int, int, int]
    encoded_positions: bytes
    flags_raw: int
    no_tangent: bool
    tangent_mask: bytes
    skin_bytes: bytes
    surface_records: bytes
    color_bytes: bytes
    bounds_half_extent: tuple[float, float, float]
    external_index_offset: int = 0
    blob_position_offset: int = 0
    uv_bytes: tuple[bytes, bytes, bytes] = (b"", b"", b"")
    uv_error: CanonicalMeshWriteError | None = None


def _mply_float3(
    values: Sequence[float],
    field: str,
) -> tuple[float, float, float]:
    array_value = np.asarray(values, dtype=np.float64)
    if array_value.shape != (3,) or not np.isfinite(array_value).all():
        raise CanonicalMeshWriteError(f"{field} must be one finite Float3")
    result = array_value.astype(np.float32)
    if not np.isfinite(result).all():
        raise CanonicalMeshWriteError(f"{field} exceeds float32")
    return tuple(float(value) for value in result)


def _half2_values(
    values: np.ndarray,
    infinity_replacement: float | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    nan_mask = np.isnan(values)
    infinity_mask = np.isinf(values)
    if infinity_replacement is not None and math.isfinite(infinity_replacement) and infinity_mask.any():
        values = values.copy()
        values[infinity_mask] = infinity_replacement
    with np.errstate(over="ignore"):
        half = values.astype("<f2")
    overflow = (np.isfinite(values) & ~np.isfinite(half)).any(axis=1)
    half.view("<u2")[nan_mask] = 0x7E00
    return half, nan_mask.any(axis=1), infinity_mask.any(axis=1), overflow


def _half2_error(
    field: str,
    domain: Half2ValueDomain,
    infinity_replacement: float | None,
    nan: bool,
    infinity: bool,
    overflow: bool,
) -> CanonicalMeshWriteError | None:
    if nan and domain not in {Half2ValueDomain.IEEE754, Half2ValueDomain.FINITE_OR_NAN}:
        return CanonicalMeshWriteError(f"{field} contains NaN")
    if infinity and domain in {Half2ValueDomain.FINITE, Half2ValueDomain.FINITE_OR_NAN}:
        return CanonicalMeshWriteError(f"{field} contains infinity")
    if infinity and infinity_replacement is not None and not math.isfinite(infinity_replacement):
        return CanonicalMeshWriteError(f"{field} infinity replacement must be finite")
    if overflow:
        return CanonicalMeshWriteError(f"{field} exceeds binary16")
    return None


def _half2_bytes(
    values: Sequence[Sequence[float]],
    field: str,
    domain: Half2ValueDomain,
) -> bytes:
    array_value = np.asarray(values, dtype=np.float64)
    if array_value.ndim != 2 or array_value.shape[1] != 2:
        raise CanonicalMeshWriteError(f"{field} must contain Float2 values")
    half, nan, infinity, overflow = _half2_values(array_value, None)
    error = _half2_error(field, domain, None, bool(nan.any()), bool(infinity.any()), bool(overflow.any()))
    if error is not None:
        raise error
    return half.tobytes()


def _byte_values(values: Sequence[int], count: int, field: str) -> bytes:
    encoded, errors = _byte_planes({0: values}, {0: count}, field)
    if errors:
        raise errors[0]
    return encoded[0]


def _mply_no_tangent(chunk: object, profile: MeshGameProfile) -> bool:
    tangent_policy = profile.canonical_mply_stores_tangents
    if tangent_policy is not None:
        return not tangent_policy
    flags = getattr(chunk, "flags", None)
    if flags is not None:
        # DD2 supplies placeholder tangent vectors when the native plane is absent.
        return bool(flags.is_meshlet_no_tangent)
    return not len(getattr(chunk, "tangents", ()))


def _mply_tangent_mode_and_mask(
    chunk: object,
    profile: MeshGameProfile,
    no_tangent: bool,
    vertex_count: int,
) -> tuple[int, bytes]:
    if not no_tangent or not profile.meshlet_uses_mhwilds_tangent_handedness:
        return 0, b""
    values = np.asarray(getattr(chunk, "tangent_ws", ())).reshape(-1).astype(np.int64)
    if not len(values):
        values = np.full(vertex_count, 0x7F, np.int64)
    if len(values) != vertex_count or np.any((values < 0) | (values > 0xFF)):
        raise CanonicalMeshWriteError(
            "MPLY tangent orientation does not match its vertex domain"
        )
    negative = (values & 0x80) != 0
    if not negative.any():
        return 0, b""
    if negative.all():
        return 1, b""
    result = np.zeros(((vertex_count + 31) // 32) * 32, np.uint8)
    result[:vertex_count] = negative
    return 2, np.packbits(result, bitorder="little").tobytes()


def _mply_skin_bytes(
    chunk: object,
    profile: MeshGameProfile,
    vertex_count: int,
) -> tuple[bytes, bool]:
    stream = getattr(chunk, "skin_weights", None)
    if stream is None:
        return b"", False
    if int(stream.vertex_count) != vertex_count:
        raise CanonicalMeshWriteError(
            "MPLY skin stream does not match its chunk vertex count"
        )
    encoded = _encode_skin_weights(stream, profile)
    records = np.frombuffer(encoded, dtype=np.uint8).reshape(vertex_count, 16)
    shared = bool(vertex_count and np.all(records == records[0]))
    return (bytes(records[0]) if shared else encoded), shared


def _mply_flag_word(
    profile: MeshGameProfile,
    *,
    no_tangent: bool,
    use_uv1: bool,
    use_uv2: bool,
    use_color: bool,
    use_skin: bool,
    shared_skin: bool,
    tangent_mode: int,
    exponent: int,
) -> int:
    raw = 0
    if profile.meshlet_uses_pragmata_flag_bits:
        raw |= int(no_tangent) << 5
        raw |= int(use_skin) << 6
        raw |= int(use_skin and shared_skin) << 7
    else:
        raw |= int(use_skin and shared_skin) << 5
        raw |= int(no_tangent) << 6
        raw |= int(use_skin) << 11
    raw |= int(use_color) << 8
    raw |= int(use_uv1) << 9
    raw |= int(use_uv2) << 10
    raw |= (int(tangent_mode) & 0x3) << 14
    raw |= (int(exponent) & 0xFF) << 16
    if profile.meshlet_uses_dual_exponent_position_decode:
        raw |= (int(exponent) & 0xFF) << 24
    return raw


# Numbered in the order the checks apply; the lowest failed number is what a chunk reports.
(_VERTEX_COUNT, _POSITIONS, _INDEX_COUNT, _INDEX_RANGE, _MATERIAL_ID, _PART_ID, _CLUSTER_AABB, _CENTER, _EXPONENT,
 _NORMALS, _NORMAL_W, _TANGENTS, _TANGENT_W, _UV0, _UV1, _UV2, _COLORS) = range(17)
_MPLY_UV_PLANES = ("uv0", "uv1", "uv2")


class _ChunkChecks:
    def __init__(self, count: int) -> None:
        self.first: list[tuple[int, Exception] | None] = [None] * count

    def fail(self, index: int, check: int, error: Exception) -> None:
        if self.first[index] is None or check < self.first[index][0]:
            self.first[index] = (check, error)

    def fail_all(self, indices: Iterable[int], check: int, message: str) -> None:
        for index in indices:
            self.fail(index, check, CanonicalMeshWriteError(message))

    def fail_each(self, check: int, errors: dict[int, Exception]) -> None:
        for index, error in errors.items():
            self.fail(index, check, error)

    def passed(self, index: int, check: int) -> bool:
        return self.first[index] is None or self.first[index][0] >= check


def _segments(arrays: list[np.ndarray], width: int, dtype: object) -> tuple[np.ndarray, np.ndarray]:
    lengths = [len(array) for array in arrays]
    starts = np.concatenate(([0], np.cumsum(lengths)[:-1])).astype(np.int64) if lengths else np.zeros(0, np.int64)
    if not arrays:
        return np.zeros((0, width) if width else 0, dtype), starts
    return np.concatenate(arrays).astype(dtype, copy=False), starts


def _segment_any(mask: np.ndarray, starts: np.ndarray, lengths: np.ndarray) -> np.ndarray:
    result = np.zeros(len(starts), bool)
    filled = lengths > 0
    if filled.any():
        result[filled] = np.logical_or.reduceat(mask, starts[filled])
    return result


def _byte_planes(planes: dict[int, object], counts: dict[int, int], field: str
                 ) -> tuple[dict[int, bytes], dict[int, CanonicalMeshWriteError]]:
    valid = {}
    errors: dict[int, CanonicalMeshWriteError] = {}
    for index, values in planes.items():
        if len(values) != counts[index]:
            errors[index] = CanonicalMeshWriteError(f"{field} does not match its vertex count")
            continue
        values = np.asarray(values).reshape(-1)
        if values.dtype.kind not in "iub":
            errors[index] = CanonicalMeshWriteError(f"{field} must contain integers in its byte domain")
            continue
        valid[index] = values
    indices = list(valid)
    joined, starts = _segments([valid[i] for i in indices], 0, np.int64)
    lengths = np.array([len(valid[i]) for i in indices], np.int64)
    outside = _segment_any((joined < 0) | (joined > 0xFF), starts, lengths)
    encoded = joined.astype(np.uint8).tobytes()
    result = {}
    for index, begin, length, bad in zip(indices, starts.tolist(), lengths.tolist(), outside.tolist()):
        if bad:
            errors[index] = CanonicalMeshWriteError(f"{field} must contain integers in its byte domain")
        else:
            result[index] = encoded[begin:begin + length]
    return result, errors


def _uv_planes(chunks: Sequence[object], indices: list[int], counts: np.ndarray, profile: MeshGameProfile
               ) -> tuple[dict[int, tuple[bytes, bytes, bytes]], dict[int, CanonicalMeshWriteError]]:
    encoded = {index: [b"", b"", b""] for index in indices}
    errors: dict[int, CanonicalMeshWriteError] = {}
    for plane, name in enumerate(_MPLY_UV_PLANES):
        field = f"MPLY {name.upper()}"
        domain = profile.canonical_mply_uv_half2_domains[plane]
        replacement = profile.canonical_mply_uv_infinity_replacements[plane]
        replacement = None if replacement is None else float(replacement)
        used = [index for index in indices if index not in errors and len(getattr(chunks[index], name))]
        values, starts = _segments([np.asarray(getattr(chunks[i], name), np.float64) for i in used], 2, np.float64)
        lengths = counts[used].astype(np.int64) if used else np.zeros(0, np.int64)
        half, nan_rows, infinity_rows, overflow_rows = _half2_values(values, replacement)
        has_nan = _segment_any(nan_rows, starts, lengths)
        has_infinity = _segment_any(infinity_rows, starts, lengths)
        overflow = _segment_any(overflow_rows, starts, lengths)
        raw = half.tobytes()
        for index, begin, length, nan, infinity, over in zip(used, starts.tolist(), lengths.tolist(),
                                                              has_nan.tolist(), has_infinity.tolist(),
                                                              overflow.tolist()):
            error = _half2_error(field, domain, replacement, nan, infinity, over)
            if error is not None:
                errors[index] = error
            else:
                encoded[index][plane] = raw[begin * 4:(begin + length) * 4]
    return {index: tuple(planes) for index, planes in encoded.items() if index not in errors}, errors


def _prepare_mply_chunks(chunks: Sequence[object], profile: MeshGameProfile) -> list[_MplyChunkPlan]:
    """Errors match a chunk-by-chunk pass: the first failing chunk and its first check (``_VERTEX_COUNT`` ...
    ``_COLORS``, then skin stream and tangent orientation). UV encoding errors are raised when that chunk is written."""
    chunks = list(chunks)
    checks = _ChunkChecks(len(chunks))
    counts = np.zeros(len(chunks), np.int64)
    positions, faces, normals, tangents = {}, {}, {}, {}
    normal_ws, tangent_ws, colors = {}, {}, {}
    sources: dict[int, tuple[np.ndarray, np.ndarray] | None] = {}
    no_tangent = [False] * len(chunks)
    for index, chunk in enumerate(chunks):
        count = counts[index] = int(chunk.vert_count)
        if not 1 <= count <= 0xFF:
            checks.fail(index, _VERTEX_COUNT, CanonicalMeshWriteError("MPLY chunk vertex count must fit one byte"))
            continue
        if count > (profile.meshlet_max_vertices or count):
            checks.fail(index, _VERTEX_COUNT, CanonicalMeshWriteError(
                f"MPLY chunk has {count} vertices; the game draws at most {profile.meshlet_max_vertices}"))
            continue
        steps = (
            (_POSITIONS, lambda: positions.__setitem__(index, np.asarray(chunk.positions, dtype=np.float64))),
            (_INDEX_COUNT, lambda: faces.__setitem__(index, np.asarray(chunk.faces).reshape(-1).astype(np.int64))),
            (_MATERIAL_ID, lambda: int(chunk.material_id)),
            (_PART_ID, lambda: int(chunk.part_id)),
            (_NORMALS, lambda: normals.__setitem__(index, np.asarray(chunk.normals, dtype=np.float32))),
            (_TANGENTS, lambda: tangents.__setitem__(index, np.asarray(chunk.tangents, dtype=np.float32))),
        )
        for check, step in steps:
            try:
                step()
            except Exception as error:
                checks.fail(index, check, error)
        if index in positions and positions[index].shape != (count, 3):
            checks.fail(index, _POSITIONS,
                        CanonicalMeshWriteError("MPLY chunk positions are not a finite vertex domain"))
        if index in faces and (len(faces[index]) % 3 or len(faces[index]) > 0x1FF):
            checks.fail(index, _INDEX_COUNT,
                        CanonicalMeshWriteError("MPLY chunk index count must be triangular and fit nine bits"))
        elif index in faces and (triangles := len(faces[index]) // 3) > (profile.meshlet_max_triangles or triangles):
            checks.fail(index, _INDEX_COUNT, CanonicalMeshWriteError(
                f"MPLY chunk has {triangles} triangles; the game draws at most {profile.meshlet_max_triangles}"))
        for check, label, value in ((_MATERIAL_ID, "material ID", chunk.material_id),
                                    (_PART_ID, "part ID", chunk.part_id)):
            if checks.passed(index, check + 1) and not 0 <= int(value) <= 0xFF:
                checks.fail(index, check, CanonicalMeshWriteError(f"MPLY {label} exceeds one byte"))
        source_aabb = getattr(chunk, "cluster_aabb", None)
        if source_aabb is None:
            sources[index] = None
        else:
            try:
                center = np.asarray(source_aabb.center, dtype=np.float64)
                half_extent = np.asarray(source_aabb.half_extent, dtype=np.float64)
            except Exception as error:
                checks.fail(index, _CLUSTER_AABB, error)
            else:
                if center.shape != (3,) or half_extent.shape != (3,):
                    checks.fail(index, _CLUSTER_AABB, CanonicalMeshWriteError("MPLY cluster AABB is invalid"))
                else:
                    sources[index] = (center, half_extent)
        if index in normals and normals[index].shape != (count, 3):
            checks.fail(index, _NORMALS, CanonicalMeshWriteError("MPLY normals do not match their vertex domain"))
        normal_ws[index] = chunk.normal_ws
        no_tangent[index] = _mply_no_tangent(chunk, profile)
        if not no_tangent[index]:
            if index in tangents and tangents[index].shape != (count, 3):
                checks.fail(index, _TANGENTS, CanonicalMeshWriteError("MPLY tangents do not match their vertex domain"))
            tangent_ws[index] = chunk.tangent_ws
        if np.asarray(chunk.uv0).shape != (count, 2):
            checks.fail(index, _UV0, CanonicalMeshWriteError("MPLY UV0 does not match its vertex domain"))
        for check, name in ((_UV1, "uv1"), (_UV2, "uv2")):
            values = getattr(chunk, name)
            if len(values) and np.asarray(values).shape != (count, 2):
                checks.fail(index, check,
                            CanonicalMeshWriteError(f"MPLY {name.upper()} does not match its vertex domain"))
        if len(chunk.colors):
            colors[index] = chunk.colors

    shaped = [index for index in positions if checks.passed(index, _POSITIONS + 1)]
    joined, starts = _segments([positions[i] for i in shaped], 3, np.float64)
    lengths = counts[shaped].astype(np.int64) if shaped else np.zeros(0, np.int64)
    finite = ~_segment_any(~np.isfinite(joined).all(axis=1), starts, lengths)
    checks.fail_all([i for i, ok in zip(shaped, finite.tolist()) if not ok], _POSITIONS,
                    "MPLY chunk positions are not a finite vertex domain")
    in_range = [index for index in faces if checks.passed(index, _INDEX_COUNT + 1)]
    face_values, face_starts = _segments([faces[i] for i in in_range], 0, np.int64)
    face_lengths = np.array([len(faces[i]) for i in in_range], np.int64)
    limits = np.repeat(counts[in_range], face_lengths) if in_range else np.zeros(0, np.int64)
    leaves = _segment_any((face_values < 0) | (face_values >= limits), face_starts, face_lengths)
    checks.fail_all([i for i, bad in zip(in_range, leaves.tolist()) if bad], _INDEX_RANGE,
                    "MPLY indices leave the cluster-local byte domain")

    placed = [index for index, ok in zip(shaped, finite.tolist()) if ok and index in sources]
    picked = np.array([shaped.index(i) for i in placed], np.int64) if placed else np.zeros(0, np.int64)
    rows = np.concatenate([np.arange(starts[k], starts[k] + lengths[k]) for k in picked]) if placed else \
        np.zeros(0, np.int64)
    points = joined[rows]
    point_starts = np.concatenate(([0], np.cumsum(lengths[picked])[:-1])).astype(np.int64) if placed else \
        np.zeros(0, np.int64)
    centers = np.zeros((len(placed), 3))
    halves = np.zeros((len(placed), 3))
    if placed:
        minimum = np.minimum.reduceat(points, point_starts, axis=0)
        maximum = np.maximum.reduceat(points, point_starts, axis=0)
        centers = (minimum + maximum) * 0.5
        halves = (maximum - minimum) * 0.5
    for slot, index in enumerate(placed):
        if sources[index] is not None:
            centers[slot], halves[slot] = sources[index]
    explicit = np.array([sources[i] is not None for i in placed], bool)
    bad_aabb = explicit & ~(np.isfinite(centers).all(axis=1) & np.isfinite(halves).all(axis=1)
                            & (halves >= 0.0).all(axis=1))
    checks.fail_all([i for i, bad in zip(placed, bad_aabb.tolist()) if bad], _CLUSTER_AABB,
                    "MPLY cluster AABB is invalid")
    with np.errstate(over="ignore"):
        single = centers.astype(np.float32)
    nonfinite = ~np.isfinite(centers).all(axis=1) & ~bad_aabb
    checks.fail_all([i for i, bad in zip(placed, nonfinite.tolist()) if bad], _CENTER,
                    "MPLY cluster center must be one finite Float3")
    overflow = np.isfinite(centers).all(axis=1) & ~np.isfinite(single).all(axis=1) & ~bad_aabb
    checks.fail_all([i for i, bad in zip(placed, overflow.tolist()) if bad], _CENTER,
                    "MPLY cluster center exceeds float32")
    sized = [slot for slot, index in enumerate(placed) if checks.passed(index, _CENTER + 1)]
    plans_geometry = {}
    if sized:
        sized_indices = [placed[slot] for slot in sized]
        centers = single[sized].astype(np.float64)
        halves = halves[sized]
        sized_rows = np.concatenate([np.arange(point_starts[slot], point_starts[slot] + lengths[picked[slot]])
                                     for slot in sized])
        sized_lengths = lengths[picked[sized]]
        sized_starts = np.concatenate(([0], np.cumsum(sized_lengths)[:-1])).astype(np.int64)
        segment = np.repeat(np.arange(len(sized)), sized_lengths)
        offsets = points[sized_rows] - centers[segment]
        reach = np.maximum.reduceat(np.abs(offsets), sized_starts, axis=0)
        halves = np.maximum(halves, reach)
        required = np.maximum(np.maximum(reach.max(axis=1) / 32767.0, halves.max(axis=1) / 65535.0), 0.0)
        legacy = profile.meshlet_uses_legacy_flag_bits
        floor = 125 if legacy else 0
        exponents = np.array([floor if unit <= 0.0 else max(floor, int(math.ceil(math.log2(unit))) + 143)
                              for unit in required.tolist()], np.int64)
        pending = np.ones(len(sized), bool)
        quantized = np.zeros((len(offsets), 3), np.int64)
        extents = np.zeros((len(sized), 3), np.int64)
        while pending.any():
            too_large = pending & (exponents > 0xFF)
            checks.fail_all([sized_indices[k] for k in np.flatnonzero(too_large)], _EXPONENT,
                            "MPLY position exponent exceeds one byte")
            outside = pending & ~too_large & legacy & (exponents > 139)
            checks.fail_all([sized_indices[k] for k in np.flatnonzero(outside)], _EXPONENT,
                            "DD2 MPLY positions exceed its canonical exponent domain")
            pending &= ~(too_large | outside)
            if not pending.any():
                break
            units = np.ldexp(1.0, exponents - 143)
            active = pending[segment]
            quantized[active] = np.rint(offsets[active] / units[segment[active], None]).astype(np.int64) + 32768
            extents[pending] = np.ceil(halves[pending] / units[pending, None]).astype(np.int64)
            fits = np.logical_and.reduceat(np.all((quantized >= 0) & (quantized <= 0xFFFF), axis=1), sized_starts)
            fits &= np.all((extents >= 0) & (extents <= 0xFFFF), axis=1)
            exponents[pending & ~fits] += 1
            pending &= ~fits
        encoded = quantized.astype("<u2").tobytes()
        decoded = extents.astype(np.float64) * np.ldexp(1.0, exponents - 143)[:, None]
        for slot, index in enumerate(sized_indices):
            begin, length = int(sized_starts[slot]), int(sized_lengths[slot])
            plans_geometry[index] = (tuple(centers[slot].tolist()), extents[slot].tolist(),
                                     encoded[begin * 6:(begin + length) * 6], int(exponents[slot]),
                                     tuple(decoded[slot].tolist()))

    def snorm8_planes(planes: dict[int, np.ndarray], check: int, message: str) -> dict[int, np.ndarray]:
        indices = [index for index in planes if checks.passed(index, check + 1)]
        values, value_starts = _segments([planes[i] for i in indices], 3, np.float32)
        value_lengths = counts[indices].astype(np.int64) if indices else np.zeros(0, np.int64)
        signed, nonfinite = _snorm8_values(values)
        bad = _segment_any(nonfinite.any(axis=1), value_starts, value_lengths)
        checks.fail_all([i for i, flag in zip(indices, bad.tolist()) if flag], check, message)
        return {index: signed[begin:begin + length]
                for index, begin, length, flag in zip(indices, value_starts.tolist(), value_lengths.tolist(),
                                                      bad.tolist()) if not flag}

    def byte_planes(planes: dict[int, object], check: int, width: int, field: str) -> dict[int, bytes]:
        encoded, errors = _byte_planes({i: planes[i] for i in planes if checks.passed(i, check)},
                                       {i: int(counts[i]) * width for i in planes}, field)
        checks.fail_each(check, errors)
        return encoded

    normal_bytes = snorm8_planes(normals, _NORMALS, "MPLY normals do not match their vertex domain")
    normal_w = byte_planes(normal_ws, _NORMAL_W, 1, "MPLY normal W")
    tangent_bytes = snorm8_planes({i: tangents[i] for i in tangents if not no_tangent[i]}, _TANGENTS,
                                  "MPLY tangents do not match their vertex domain")
    tangent_w = byte_planes(tangent_ws, _TANGENT_W, 1, "MPLY tangent W")
    color_bytes = byte_planes(colors, _COLORS, 4, "MPLY vertex colors")
    prepared = [index for index in range(len(chunks)) if checks.first[index] is None]
    uv_bytes, uv_errors = _uv_planes(chunks, prepared, counts, profile)

    plans = []
    for index, chunk in enumerate(chunks):
        if checks.first[index] is not None:
            raise checks.first[index][1]
        count = int(counts[index])
        skin_bytes, skin_shared = _mply_skin_bytes(chunk, profile, count)
        tangent_mode, tangent_mask = _mply_tangent_mode_and_mask(chunk, profile, no_tangent[index], count)
        center, extent, encoded_positions, exponent, decoded_half = plans_geometry[index]
        surface = np.empty((count, 4 if no_tangent[index] else 8), np.uint8)
        surface[:, :3] = normal_bytes[index].reshape(count, 3)
        surface[:, 3] = np.frombuffer(normal_w[index], np.uint8)
        if not no_tangent[index]:
            surface[:, 4:7] = tangent_bytes[index].reshape(count, 3)
            surface[:, 7] = np.frombuffer(tangent_w[index], np.uint8)
        plans.append(_MplyChunkPlan(
            chunk=chunk,
            faces=faces[index],
            center=center,
            relative_aabb=(32768, extent[0], 32768, extent[1], 32768, extent[2]),
            encoded_positions=encoded_positions,
            flags_raw=_mply_flag_word(profile, no_tangent=no_tangent[index], use_uv1=bool(len(chunk.uv1)),
                                      use_uv2=bool(len(chunk.uv2)), use_color=index in colors,
                                      use_skin=bool(skin_bytes), shared_skin=skin_shared,
                                      tangent_mode=tangent_mode, exponent=exponent),
            no_tangent=no_tangent[index],
            tangent_mask=tangent_mask,
            skin_bytes=skin_bytes,
            surface_records=surface.tobytes(),
            color_bytes=color_bytes.get(index, b""),
            bounds_half_extent=decoded_half,
            uv_bytes=uv_bytes.get(index, (b"", b"", b"")),
            uv_error=uv_errors.get(index),
        ))
    return plans


def _write_mply_chunk(
    builder: _BinaryBuilder,
    plan: _MplyChunkPlan,
    profile: MeshGameProfile,
    blob_base: int,
) -> None:
    chunk = plan.chunk
    vertex_count = int(chunk.vert_count)
    builder.write("<3f4B", *plan.center, vertex_count, len(plan.faces) // 3, int(chunk.material_id), int(chunk.part_id))
    builder.write("<6HI", *plan.relative_aabb, plan.flags_raw)
    builder.write_bytes(plan.faces.astype(np.uint8).tobytes())
    builder.align(4)
    plan.blob_position_offset = builder.tell - blob_base
    builder.write_bytes(plan.encoded_positions)
    builder.align(4)

    builder.write_bytes(plan.surface_records)
    builder.write_bytes(plan.tangent_mask)
    if plan.uv_error is not None:
        raise plan.uv_error
    uv0, uv1, uv2 = plan.uv_bytes
    builder.write_bytes(uv0)
    builder.write_bytes(uv1)
    if profile.meshlet_skin_before_texcoord3 and plan.skin_bytes:
        builder.write_bytes(plan.skin_bytes)
    builder.write_bytes(uv2)
    builder.write_bytes(plan.color_bytes)
    if not profile.meshlet_skin_before_texcoord3 and plan.skin_bytes:
        builder.write_bytes(plan.skin_bytes)


def _semantic_mply_pivots(
    mesh: MeshFile,
) -> tuple[tuple[float, float, float], ...]:
    values = tuple(mesh.group_pivots.pivots) if mesh.group_pivots is not None else ()
    return tuple(
        _mply_float3(value, f"MPLY group pivot {index}")
        for index, value in enumerate(values)
    )


def _canonical_mply_bvh_quantizer(
    plans: Sequence[_MplyChunkPlan],
    profile: MeshGameProfile,
) -> tuple[
    tuple[float, float, float],
    float,
    tuple[tuple[int, int, int], ...],
]:
    if not profile.meshlet_has_extended_indices:
        return (0.0, 0.0, 0.0), 0.0, ()
    centers = np.asarray([plan.center for plan in plans], dtype=np.float64)
    half_extents = np.asarray(
        [plan.bounds_half_extent for plan in plans],
        dtype=np.float64,
    )
    offset = centers.min(axis=0).astype(np.float32).astype(np.float64)
    span = float(np.max(centers.max(axis=0) - offset))
    half = float(np.max(half_extents))
    required = max(span * (65536.0 / 65535.0), half * 1.0001, 0.0)
    scale = 1.0 if required <= 0.0 else math.ldexp(1.0, int(math.ceil(math.log2(required))))
    scale = float(np.float32(scale))
    if not math.isfinite(scale) or scale <= 0.0:
        raise CanonicalMeshWriteError("MPLY BVH scale is not representable")
    quantized = np.rint((centers - offset) * (65536.0 / scale)).astype(np.int64)
    if np.any(quantized < 0) or np.any(quantized > 0xFFFF):
        raise CanonicalMeshWriteError("MPLY BVH centers exceed their uint16 domain")
    return (
        tuple(float(value) for value in offset),
        scale,
        tuple(tuple(int(value) for value in row) for row in quantized),
    )


def _encode_mply_bvh(
    profile: MeshGameProfile,
    cluster_table_offset: int,
    quantized_center_offset: int,
    offset: tuple[float, float, float],
    scale: float,
    counts: Sequence[int],
    cluster_offsets: Sequence[int],
) -> bytes:
    output = _BinaryBuilder()
    output.write("<QQ3ff", cluster_table_offset, quantized_center_offset, *offset, scale)
    if profile.meshlet_has_extended_indices:
        for count in counts:
            if not 0 <= int(count) <= 0xFFFF:
                raise CanonicalMeshWriteError("MPLY cluster count exceeds uint16")
            output.write("<HH", int(count), 0)
    else:
        if any(int(count) < 0 or int(count) > 0xFFFFFFFF for count in counts):
            raise CanonicalMeshWriteError("MPLY cluster count exceeds uint32")
        output.write("<8I", *(int(count) for count in counts))
    output.write("<8I", *(int(value) for value in cluster_offsets))
    output.write("<8I", *([0] * 8))
    return output.bytes()


def _encode_mply_meshlet_header(
    settings: MPLYRuntimeSettings,
    plans: Sequence[_MplyChunkPlan],
    lod_count: int,
    lod_offsets: Sequence[int],
    lod_end_offsets: Sequence[int],
    profile: MeshGameProfile,
) -> bytes:
    minima = np.asarray(
        [
            np.asarray(plan.center) - np.asarray(plan.bounds_half_extent)
            for plan in plans
        ],
        dtype=np.float64,
    ).min(axis=0)
    maxima = np.asarray(
        [
            np.asarray(plan.center) + np.asarray(plan.bounds_half_extent)
            for plan in plans
        ],
        dtype=np.float64,
    ).max(axis=0)
    min_aabb = _mply_float3(minima, "MPLY header minimum")
    max_aabb = _mply_float3(maxima, "MPLY header maximum")
    if any(int(value) < 0 or int(value) > 0xFFFFFFFF for value in (*lod_offsets, *lod_end_offsets)):
        raise CanonicalMeshWriteError("MPLY LOD address exceeds uint32")

    active_source_factors = tuple(float(value) for value in settings.lod_factors)
    if profile.meshlet_lod_factor_encoding is MeshletLodFactorEncoding.PACKED_BINARY16:
        active_source_factors = tuple(float(index) for index in range(lod_count))
    factors = [
        active_source_factors[index]
        if index < len(active_source_factors)
        else float(index)
        for index in range(lod_count)
    ] + [0.0] * (8 - lod_count)
    if not all(math.isfinite(value) and value >= 0.0 for value in factors):
        raise CanonicalMeshWriteError("MPLY LOD factors must be finite and nonnegative")

    if profile.meshlet_lod_factor_encoding is MeshletLodFactorEncoding.CPU_UNUSED:
        payload0 = [0] * 8
    elif profile.meshlet_lod_factor_encoding is MeshletLodFactorEncoding.PACKED_BINARY16:
        try:
            factor_bytes = struct.pack("<8e", *factors)
        except (OverflowError, struct.error) as exc:
            raise CanonicalMeshWriteError("MPLY LOD factor exceeds binary16") from exc
        payload0 = list(struct.unpack("<4I", factor_bytes))
        threshold_policy = profile.canonical_mply_raster_switch_threshold
        threshold = (
            int(threshold_policy)
            if threshold_policy is not None
            else int(settings.meshlet_raster_switch_threshold)
        )
        strategy = int(settings.lod_strategy)
        if not 0 <= threshold <= 7 or strategy not in (0, 1):
            raise CanonicalMeshWriteError("MPLY LOD control enum is invalid")
        payload0.extend([threshold | (strategy << 3), 0, 0, 0])
    else:
        payload0 = [
            struct.unpack("<I", struct.pack("<f", value))[0]
            for value in factors
        ]
    payload1 = [int(value) for value in lod_end_offsets]
    return struct.pack(
        "<3fI3fI8I8I8I",
        *min_aabb,
        lod_count,
        *max_aabb,
        (1 << lod_count) - 1,
        *(int(value) for value in lod_offsets),
        *payload0,
        *payload1,
    )


def _mply_material_count(mesh: MeshFile, chunks: Sequence[object]) -> int:
    named_count = len(mesh.material_names)
    indexed_count = len(mesh.material_indices)
    if named_count and indexed_count and named_count != indexed_count:
        raise CanonicalMeshWriteError(
            "MPLY material name and name-index domains have different counts"
        )
    count = named_count or indexed_count
    if not count and mesh.meshes:
        count = int(mesh.meshes[0].material_count)
    required = max((int(chunk.material_id) for chunk in chunks), default=-1) + 1
    if count < required:
        raise CanonicalMeshWriteError(
            "MPLY material-name partition does not cover every chunk material ID"
        )
    if count > 0xFF:
        raise CanonicalMeshWriteError("MPLY material count exceeds one byte")
    return count


def _serialize_mply(
    mesh: MeshFile,
    profile: MeshGameProfile,
) -> CanonicalMeshWriteResult:
    mesh_buffer = mesh.mesh_buffer
    if mesh_buffer is None:
        raise CanonicalMeshWriteError("MPLY has no semantic meshlet buffer")
    settings = mesh_buffer.mply_runtime_settings or MPLYRuntimeSettings()

    chunks = list(mesh_buffer.mply_chunks)
    if not chunks:
        raise CanonicalMeshWriteError("MPLY needs at least one semantic chunk")
    if any(not 0 <= int(chunk.lod_index) < 8 for chunk in chunks):
        raise CanonicalMeshWriteError("MPLY LOD domain must be dense and fit eight slots")
    lod_count = max(int(chunk.lod_index) for chunk in chunks) + 1
    chunks_by_lod = [
        [chunk for chunk in chunks if int(chunk.lod_index) == lod_index]
        for lod_index in range(lod_count)
    ]
    if any(not values for values in chunks_by_lod):
        raise CanonicalMeshWriteError("MPLY chunk LOD ownership is not a dense domain")

    prepared = iter(_prepare_mply_chunks([chunk for lod_chunks in chunks_by_lod for chunk in lod_chunks], profile))
    plans_by_lod = [[next(prepared) for _chunk in lod_chunks] for lod_chunks in chunks_by_lod]
    chunk_plans = [plan for lod_plans in plans_by_lod for plan in lod_plans]

    external = bytearray()
    for plan in chunk_plans:
        plan.external_index_offset = len(external)
        if len(plan.faces):
            if (profile.meshlet_streaming_indices_exclude_no_ray_tracing
                    and plan.chunk.no_ray_tracing):
                external.extend(bytes(len(plan.faces) * 2))
            else:
                external.extend(plan.faces.astype("<u2").tobytes())
    if len(external) > 0xFFFFFFFF:
        raise CanonicalMeshWriteError("MPLY streaming index buffer exceeds uint32")

    builder = _BinaryBuilder()
    header_plan = _write_header_placeholder(builder, mesh, profile)

    # All five complete MPLY families place the resident MeshletLayout first.
    builder.align(16)
    meshlet_layout_offset = builder.tell
    layout_size = 160 if profile.meshlet_has_wilds_layout else 144
    layout_slot = builder.reserve(layout_size)
    header_plan.patch_pointer(builder, "meshlet_layout_offset", meshlet_layout_offset)

    if mesh.joint_count or mesh.bones:
        skeleton_offset = _write_skeleton(builder, mesh, profile)
        header_plan.patch_pointer(builder, "bones_offset", skeleton_offset)

    names = tuple(str(value) for value in mesh.names)
    material_count = _mply_material_count(mesh, chunks)
    _write_material_and_bone_indices(builder, header_plan, mesh, names, material_count)
    if names:
        names_offset = _write_name_table(builder, names, profile)
        header_plan.patch_pointer(builder, "name_offsets_offset", names_offset)
        if profile.meshlet_has_implicit_part_hashes:
            lod0_part_ids = tuple(
                dict.fromkeys(int(plan.chunk.part_id) for plan in plans_by_lod[0])
            )
            if lod0_part_ids:
                builder.write(
                    f"<{len(lod0_part_ids)}I",
                    *(
                        murmur3_hash_utf16le(f"Part {part_id}")
                        for part_id in lod0_part_ids
                    ),
                )
                builder.align(16)

    _write_bounds_and_sdf_path(builder, header_plan, mesh, profile)

    part_ids = tuple(sorted({int(plan.chunk.part_id) for plan in chunk_plans}))
    if any(value < 0 or value > 0xFF for value in part_ids):
        raise CanonicalMeshWriteError("MPLY part ID exceeds its byte domain")
    parts_indices_offset = builder.tell if part_ids else 0
    if part_ids:
        builder.write_bytes(bytes(part_ids))
    builder.align(16)
    parts_layout_offset = builder.tell
    builder.write("<QII", parts_indices_offset, len(part_ids), 0)
    header_plan.patch_pointer(builder, "meshlet_parts_offset", parts_layout_offset)

    # Sections follow the order of shipped files.
    streaming_entry_offset = builder.tell
    builder.write("<II", 0, len(external))
    builder.align(16)
    streaming_info_offset = builder.tell
    builder.write("<iiq", 1, 0, streaming_entry_offset)
    header_plan.patch_pointer(builder, "streaming_info_offset", streaming_info_offset)

    cluster_table_offset = builder.tell
    cluster_slots = [builder.reserve(16) for _ in chunk_plans]

    bvh_offset, bvh_scale, quantized_centers = _canonical_mply_bvh_quantizer(
        chunk_plans,
        profile,
    )
    quantized_center_offset = 0
    if profile.meshlet_has_extended_indices:
        quantized_center_offset = builder.tell
        for center in quantized_centers:
            builder.write("<3H", *center)
        builder.align(16)

    meshlet_bvh_offset = builder.tell
    bvh_slot = builder.reserve(128)
    header_plan.patch_pointer(builder, "meshlet_bvh_offset", meshlet_bvh_offset)
    builder.align(16)

    blob_base = builder.tell
    blob_header_slot = builder.reserve(128)
    if profile.meshlet_has_wilds_layout:
        pivots = _semantic_mply_pivots(mesh)
        if len(pivots) > 256:
            raise CanonicalMeshWriteError("MPLY group-pivot count exceeds 256")
        pivot_relative_offset = 144 if pivots else 0
        serialized_count = 0 if len(pivots) == 256 else len(pivots)
        skin_weight_count = 0
        if profile.meshlet_extension_has_skin_weight_count and mesh.bones:
            skin_weight_count = profile.canonical_skin_weight_count(
                profile.skin_weight_influence_count
            )
        pivot_descriptor = struct.pack(
            "<BBHIII",
            serialized_count,
            pivot_relative_offset,
            0,
            skin_weight_count,
            0,
            0,
        )
        builder.write_bytes(pivot_descriptor)
        for pivot in pivots:
            builder.write("<3f", *pivot)

    lod_offsets = [0] * 8
    lod_end_offsets = [0] * 8
    if profile.meshlet_has_wilds_layout:
        # Wilds+ serialize regions coarsest to finest; the header stays indexed from LOD0.
        for lod_index in reversed(range(lod_count)):
            lod_offsets[lod_index] = builder.tell - blob_base
            lod_plans = plans_by_lod[lod_index]
            builder.write("<I", len(lod_plans))
            chunk_pointer_slots = [builder.reserve(4) for _ in lod_plans]
            for pointer_slot, plan in zip(chunk_pointer_slots, lod_plans):
                builder.patch(pointer_slot, "<I", builder.tell - blob_base)
                _write_mply_chunk(builder, plan, profile, blob_base)
            lod_end_offsets[lod_index] = builder.tell
    else:
        dd2_pointer_slots: list[list[int]] = []
        for lod_index, lod_plans in enumerate(plans_by_lod):
            lod_offsets[lod_index] = builder.tell - blob_base
            builder.write("<I", len(lod_plans))
            dd2_pointer_slots.append([builder.reserve(4) for _ in lod_plans])
        for lod_index in reversed(range(lod_count)):
            for pointer_slot, plan in zip(
                dd2_pointer_slots[lod_index],
                plans_by_lod[lod_index],
            ):
                builder.patch(pointer_slot, "<I", builder.tell - blob_base)
                _write_mply_chunk(builder, plan, profile, blob_base)
            lod_end_offsets[lod_index] = builder.tell

    blob_size = builder.tell - blob_base
    if blob_size > 0xFFFFFFFF:
        raise CanonicalMeshWriteError("MPLY GPU blob exceeds uint32")

    meshlet_header = _encode_mply_meshlet_header(
        settings,
        chunk_plans,
        lod_count,
        lod_offsets,
        lod_end_offsets,
        profile,
    )
    builder.patch_bytes(blob_header_slot, meshlet_header)
    layout_record = bytearray(struct.pack("<Q", blob_base))
    layout_record.extend(meshlet_header)
    if profile.meshlet_has_wilds_layout:
        layout_record.extend(pivot_descriptor)
    layout_record.extend(struct.pack("<II", blob_size, 0))
    builder.patch_bytes(layout_slot, layout_record)

    counts = [len(values) for values in plans_by_lod] + [0] * (8 - lod_count)
    cluster_offsets: list[int] = []
    running_cluster_offset = 0
    for count in counts:
        cluster_offsets.append(running_cluster_offset if count else 0)
        running_cluster_offset += count * 16
    bvh_record = _encode_mply_bvh(
        profile,
        cluster_table_offset,
        quantized_center_offset,
        bvh_offset,
        bvh_scale,
        counts,
        cluster_offsets,
    )
    builder.patch_bytes(bvh_slot, bvh_record)

    for global_index, (slot, plan) in enumerate(zip(cluster_slots, chunk_plans)):
        word0 = (
            int(plan.chunk.vert_count)
            | (len(plan.faces) << 8)
            | (int(bool(plan.chunk.no_ray_tracing)) << 17)
            | (int(bool(plan.chunk.alpha_test)) << 18)
            | (int(bool(plan.chunk.transparent)) << 19)
            | (int(bool(plan.chunk.meshlet_material_flag_extension)) << 22)
            | (int(plan.chunk.material_id) << 23)
        )
        center_index = global_index if profile.meshlet_has_extended_indices else 0
        word1 = int(plan.chunk.part_id) | (center_index << 8)
        builder.patch(
            slot,
            "<4I",
            word0,
            word1,
            plan.blob_position_offset,
            plan.external_index_offset,
        )

    if profile.meshlet_has_wilds_layout:
        header_plan.patch_pointer(builder, "vertices_offset", blob_base)
        header_plan.patch_pointer(builder, "floats_offset", pivot_relative_offset)
    if header_plan.on_memory_size is not None:
        builder.patch(header_plan.on_memory_size, "<Q", blob_base)
    _patch_header_file_size(builder)
    return CanonicalMeshWriteResult(main=builder.bytes(), streaming=bytes(external))


__all__ = [
    "CanonicalMeshWriteError",
    "CanonicalMeshWriteResult",
    "serialize_mesh",
]
