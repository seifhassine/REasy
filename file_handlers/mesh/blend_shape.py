from __future__ import annotations

from dataclasses import dataclass, field
import struct
from typing import List, Optional, Tuple

import numpy as np

from .mesh_file import MeshMainVersion, _checked_slice


@dataclass(frozen=True)
class BlendShapeAABB:
    minimum: Tuple[float, float, float]
    maximum: Tuple[float, float, float]
    minimum_w: float = 0.0
    maximum_w: float = 0.0

@dataclass(frozen=True)
class BlendShapeTargetRange:
    base_vertex_location: int
    blend_shape_location: int
    vertex_count: int
    tag: int = 0
    derived_from_target: bool = False


@dataclass
class BlendShapeTarget:
    channel_location: int
    channel_count: int
    same_target_id: int
    ranges: List[BlendShapeTargetRange]
    aabb: Optional[BlendShapeAABB]
    is_blend_shape: bool = True
    part_index: Optional[int] = None
    raw_reserve: Optional[Tuple[int, int]] = None
    compatibility_fields_derived: bool = False


@dataclass
class BlendShapeChannel:
    index: int
    name_slot: int
    name: str = ""
    segments: List["BlendShapeDeltaSegment"] = field(default_factory=list)


@dataclass
class BlendShapeDeltaSegment:
    base_vertex_location: int
    position_deltas: np.ndarray
    position_w: Optional[np.ndarray] = None
    target_normals: Optional[np.ndarray] = None
    normal_w: Optional[np.ndarray] = None


@dataclass
class BlendShapeData:
    compression_type: int
    channel_offset: int
    targets: List[BlendShapeTarget]
    channels: List[BlendShapeChannel] = field(default_factory=list)
    has_bone_modifiers: bool = False
    layout_reserve: bytes = b""
    body_reserve: bytes = b""

    @property
    def target_channel_count(self) -> int:
        return sum(
            target.channel_count
            for target in self.targets
            if target.is_blend_shape
        )

    def set_channel_names(self, name_indices: List[int], names: List[str]) -> None:
        for channel in self.channels:
            if not 0 <= channel.name_slot < len(name_indices):
                continue
            name_index = name_indices[channel.name_slot]
            if 0 <= name_index < len(names):
                channel.name = names[name_index]


def _unpack(fmt: str, data: bytes | bytearray, offset: int, label: str):
    size = struct.calcsize(fmt)
    return struct.unpack_from(fmt, _checked_slice(data, offset, size, label))


def _read_aabb(
    data: bytes | bytearray,
    offset: int,
    index: int,
) -> BlendShapeAABB:
    values = _unpack(
        "<8f",
        data,
        offset + index * 32,
        f"Blendshape AABB {index}",
    )
    return BlendShapeAABB(
        tuple(values[:3]),
        tuple(values[4:7]),
        minimum_w=values[3],
        maximum_w=values[7],
    )


def _parse_legacy(
    data: bytes | bytearray,
    offset: int,
    version: MeshMainVersion,
) -> BlendShapeData:
    target_count, compression_type, reserved, targets_offset, aabb_offset = (
        _unpack("<HHIqq", data, offset, "Legacy blendshape header")
    )
    if version == MeshMainVersion.DMC5 and reserved:
        raise ValueError("DMC5 blendshape header has nonzero reserved data")
    targets: List[BlendShapeTarget] = []
    payload_cursor = 0
    channel_cursor = 0
    for target_index in range(target_count):
        (
            base_vertex_location,
            vertex_count,
            blend_ss_index,
            channel_count,
            target_reserved,
        ) = _unpack(
            "<IIHHI",
            data,
            targets_offset + target_index * 16,
            f"Legacy blendshape target {target_index}",
        )
        if (
            version == MeshMainVersion.DMC5
            and (blend_ss_index or target_reserved)
        ):
            raise ValueError(
                f"DMC5 blendshape target {target_index} uses unsupported "
                "auxiliary channel data"
            )
        aabb = (
            _read_aabb(data, aabb_offset, target_index)
            if aabb_offset > 0
            else None
        )
        targets.append(
            BlendShapeTarget(
                channel_location=channel_cursor,
                channel_count=channel_count,
                same_target_id=target_index,
                ranges=[
                    BlendShapeTargetRange(
                        base_vertex_location=base_vertex_location,
                        blend_shape_location=payload_cursor,
                        vertex_count=vertex_count,
                    )
                ],
                aabb=aabb,
                is_blend_shape=bool(channel_count and vertex_count),
            )
        )
        payload_cursor += vertex_count * channel_count
        channel_cursor += channel_count
    if version == MeshMainVersion.DMC5 and target_count:
        auxiliary_offsets = _unpack(
            "<ii",
            data,
            targets_offset + target_count * 16,
            "DMC5 blendshape auxiliary offsets",
        )
        if any(auxiliary_offsets):
            raise ValueError(
                "DMC5 blendshape auxiliary channel tables are unsupported"
            )
    return BlendShapeData(
        compression_type=compression_type,
        channel_offset=0,
        targets=targets,
    )


def _read_target_ranges(
    data: bytes | bytearray,
    target_index: int,
    range_count: int,
    ranges_offset: int,
) -> List[BlendShapeTargetRange]:
    if range_count and ranges_offset <= 0:
        raise ValueError(f"Blendshape target {target_index} has no range pointer")

    ranges = []
    for range_index in range(range_count):
        base_vertex, blend_location, vertex_count, tag = _unpack(
            "<IIII",
            data,
            ranges_offset + range_index * 16,
            f"Blendshape target {target_index} range {range_index}",
        )
        ranges.append(
            BlendShapeTargetRange(
                base_vertex_location=base_vertex,
                blend_shape_location=blend_location,
                vertex_count=vertex_count,
                tag=tag,
            )
        )
    return ranges


def _read_modern_target(
    data: bytes | bytearray,
    *,
    target_index: int,
    target_count: int,
    targets_offset: int,
    aabb_offset: int,
    channel_offset: int,
    sequential_channel: int,
    sequential_payload: int,
    version: MeshMainVersion,
) -> tuple[BlendShapeTarget, int, int]:
    is_none_target = target_index >= target_count
    target_offset = targets_offset + target_index * 16
    if version >= MeshMainVersion.RE4:
        (
            raw_channel_location,
            channel_count,
            same_target_id,
            range_count,
            is_blend_shape,
            ranges_offset,
        ) = _unpack(
            "<HHHBBq",
            data,
            target_offset,
            f"Blendshape target {target_index}",
        )
        channel_location = raw_channel_location
        if version >= MeshMainVersion.DD2_OLD and channel_location >= channel_offset:
            channel_location -= channel_offset
        ranges = _read_target_ranges(data, target_index, range_count, ranges_offset)
        part_index = None
        raw_reserve = None
        compatibility_fields_derived = False
    elif version == MeshMainVersion.RE8:
        (
            base_vertex,
            vertex_count,
            part_index,
            channel_count,
            reserve0,
            reserve1,
        ) = _unpack(
            "<IIHHHH",
            data,
            target_offset,
            f"RE8 blendshape target {target_index}",
        )
        channel_location = sequential_channel
        same_target_id = target_index
        ranges = [
            BlendShapeTargetRange(
                base_vertex_location=base_vertex,
                blend_shape_location=sequential_payload,
                vertex_count=vertex_count,
                derived_from_target=True,
            )
        ]
        is_blend_shape = bool(channel_count and vertex_count)
        sequential_channel += channel_count
        sequential_payload += vertex_count * channel_count
        raw_reserve = (reserve0, reserve1)
        compatibility_fields_derived = True
    else:
        (
            base_vertex,
            vertex_count,
            part_index,
            channel_count,
            shared_field,
        ) = _unpack(
            "<IIHHI",
            data,
            target_offset,
            f"Pre-RE4 blendshape target {target_index}",
        )
        channel_location = sequential_channel
        same_target_id = target_index
        ranges = [
            BlendShapeTargetRange(
                base_vertex_location=base_vertex,
                blend_shape_location=sequential_payload,
                vertex_count=vertex_count,
                derived_from_target=True,
            )
        ]
        is_blend_shape = bool(channel_count and vertex_count)
        sequential_channel += channel_count
        sequential_payload += vertex_count * channel_count
        raw_reserve = (shared_field & 0xFFFF, shared_field >> 16)
        compatibility_fields_derived = True

    aabb = (
        _read_aabb(data, aabb_offset, target_index)
        if aabb_offset > 0 and target_index < target_count
        else None
    )
    target = BlendShapeTarget(
        channel_location=channel_location,
        channel_count=channel_count,
        same_target_id=same_target_id,
        ranges=ranges,
        aabb=aabb,
        is_blend_shape=bool(is_blend_shape and not is_none_target),
        part_index=part_index,
        raw_reserve=raw_reserve,
        compatibility_fields_derived=compatibility_fields_derived,
    )
    return target, sequential_channel, sequential_payload


def _parse_modern(
    data: bytes | bytearray,
    offset: int,
    version: MeshMainVersion,
) -> BlendShapeData:
    lod_count = _unpack("<B", data, offset, "Blendshape LOD count")[0]
    if lod_count <= 0:
        raise ValueError("Blendshape layout has no LOD bodies")

    layout_reserve_size = 15 if version >= MeshMainVersion.DD2_OLD else 7
    layout_reserve = _checked_slice(
        data,
        offset + 1,
        layout_reserve_size,
        "Blendshape layout reserve",
    )

    has_bone_modifiers = False
    if version == MeshMainVersion.RE_RT or version >= MeshMainVersion.RE4:
        pointer_offset = offset + (16 if version >= MeshMainVersion.DD2_OLD else 8)
        bodies_offset, bone_modifier_offset = _unpack(
            "<qq",
            data,
            pointer_offset,
            "Blendshape layout pointers",
        )
        has_bone_modifiers = bone_modifier_offset > 0
    else:
        bodies_offset = _unpack(
            "<q",
            data,
            offset + 8,
            "Blendshape body-list pointer",
        )[0]

    if bodies_offset <= 0:
        raise ValueError("Blendshape body-list pointer is absent")
    body_offsets = _unpack(
        f"<{lod_count}q",
        data,
        bodies_offset,
        "Blendshape body pointers",
    )
    body_offset = body_offsets[0]
    if body_offset <= 0:
        raise ValueError("LOD0 has no blendshape body")

    if version >= MeshMainVersion.RE4:
        target_count, none_target_count = _unpack(
            "<HH",
            data,
            body_offset,
            "Blendshape body counts",
        )
        if version >= MeshMainVersion.DD2_OLD:
            channel_offset, _body_channel_count, compression_type = _unpack(
                "<HHB",
                data,
                body_offset + 4,
                "Blendshape body channel fields",
            )
        else:
            compression_type, _body_channel_count, channel_offset = _unpack(
                "<BHH",
                data,
                body_offset + 4,
                "Blendshape body channel fields",
            )
        targets_offset, aabb_offset, _target_lod_map, _channel_lod_map = _unpack(
            "<qqqq",
            data,
            body_offset + 16,
            "Blendshape body pointers",
        )
        body_reserve = _checked_slice(
            data,
            body_offset + 9,
            7,
            "Blendshape body reserve",
        )
    else:
        target_count, compression_type = _unpack(
            "<HB",
            data,
            body_offset,
            "Pre-RE4 blendshape body",
        )
        none_target_count = 0
        channel_offset = 0
        targets_offset, aabb_offset = _unpack(
            "<qq",
            data,
            body_offset + 16,
            "Pre-RE4 blendshape body pointers",
        )
        body_reserve = _checked_slice(
            data,
            body_offset + 3,
            13,
            "Pre-RE4 blendshape body reserve",
        )

    if target_count and targets_offset <= 0:
        raise ValueError("Blendshape target-list pointer is absent")

    targets: List[BlendShapeTarget] = []
    sequential_channel = 0
    sequential_payload = 0
    total_target_count = target_count + none_target_count
    for target_index in range(total_target_count):
        target, sequential_channel, sequential_payload = _read_modern_target(
            data,
            target_index=target_index,
            target_count=target_count,
            targets_offset=targets_offset,
            aabb_offset=aabb_offset,
            channel_offset=channel_offset,
            sequential_channel=sequential_channel,
            sequential_payload=sequential_payload,
            version=version,
        )
        targets.append(target)

    return BlendShapeData(
        compression_type=compression_type,
        channel_offset=channel_offset,
        targets=targets,
        has_bone_modifiers=has_bone_modifiers,
        layout_reserve=layout_reserve,
        body_reserve=body_reserve,
    )


def parse_blend_shapes(
    data: bytes | bytearray,
    offset: int,
    version: MeshMainVersion,
) -> BlendShapeData:
    result = (
        _parse_legacy(data, offset, version)
        if version in (MeshMainVersion.RE7, MeshMainVersion.DMC5)
        else _parse_modern(data, offset, version)
    )
    active_channel_ids = sorted(
        {
            channel_index
            for target in result.targets
            if target.is_blend_shape and target.ranges
            for channel_index in range(
                target.channel_location,
                target.channel_location + target.channel_count,
            )
        }
    )
    result.channels = [
        BlendShapeChannel(
            index=channel_index,
            name_slot=result.channel_offset + channel_index,
        )
        for channel_index in active_channel_ids
    ]
    return result


def read_blend_shape_name_indices(
    data: bytes | bytearray,
    offset: int,
    section_size: int,
    version: MeshMainVersion,
    blend_shapes: Optional[BlendShapeData],
) -> List[int]:
    index_count = section_size // 2
    if version == MeshMainVersion.RE8 and blend_shapes is not None:
        index_count = min(index_count, blend_shapes.target_channel_count)
    if index_count <= 0:
        return []
    raw = _checked_slice(data, offset, index_count * 2, "Blendshape name indices")
    return list(struct.unpack(f"<{index_count}H", raw))


def decode_dmc5_blend_shape_payload(
    blend_shapes: BlendShapeData,
    vertex_bytes: bytes,
    payload_offset: int,
    vertex_count: int,
    *,
    explicit_normals: bool,
) -> None:
    """Decode DMC5 Light/Standard payloads into offset-free channel segments."""

    for channel in blend_shapes.channels:
        channel.segments.clear()
    position_stride = {0: 4, 1: 8}.get(blend_shapes.compression_type)
    if position_stride is None:
        raise ValueError(
            "DMC5 blendshape visualization supports Light and Standard payloads; "
            f"found compression type {blend_shapes.compression_type}"
        )

    channels = {channel.index: channel for channel in blend_shapes.channels}
    entry_count = sum(
        item.vertex_count * target.channel_count
        for target in blend_shapes.targets
        if target.is_blend_shape
        for item in target.ranges
    )
    stride = position_stride + (4 if explicit_normals else 0)
    required_size = entry_count * stride
    if payload_offset <= 0 or payload_offset + required_size > len(vertex_bytes):
        raise ValueError(
            "DMC5 blendshape payload exceeds the resident vertex buffer"
        )

    payload = memoryview(vertex_bytes)[
        payload_offset : payload_offset + required_size
    ]
    entry_cursor = 0
    for target in blend_shapes.targets:
        if not target.is_blend_shape:
            continue
        light_minimum = light_step = None
        if blend_shapes.compression_type == 0:
            if target.aabb is None:
                raise ValueError(
                    "DMC5 Light blendshape target has no quantization AABB"
                )
            light_minimum = np.asarray(target.aabb.minimum, dtype=np.float32)
            light_step = (
                np.asarray(target.aabb.maximum, dtype=np.float32) - light_minimum
            ) / np.asarray((2047.0, 1023.0, 2047.0), dtype=np.float32)
            if not (
                np.isfinite(light_minimum).all()
                and np.isfinite(light_step).all()
            ):
                raise ValueError(
                    "DMC5 Light blendshape target has a nonfinite AABB"
                )
        for local_channel in range(target.channel_count):
            channel_index = target.channel_location + local_channel
            channel = channels.get(channel_index)
            if channel is None:
                raise ValueError(
                    f"DMC5 blendshape channel {channel_index} has no name slot"
                )
            for item in target.ranges:
                if (
                    item.base_vertex_location < 0
                    or item.vertex_count < 0
                    or item.base_vertex_location + item.vertex_count > vertex_count
                ):
                    raise ValueError(
                        f"DMC5 blendshape channel {channel_index} has an "
                        "invalid destination vertex range"
                    )
                start = entry_cursor * stride
                stop = start + item.vertex_count * stride
                records = np.frombuffer(
                    payload[start:stop],
                    dtype=np.uint8,
                ).reshape(item.vertex_count, stride)
                position_w = None
                if blend_shapes.compression_type == 0:
                    packed = records[:, :4].copy().view("<u4").reshape(-1)
                    quantized = np.column_stack(
                        (
                            packed & 0x7FF,
                            (packed >> 11) & 0x3FF,
                            (packed >> 21) & 0x7FF,
                        )
                    ).astype(np.float32)
                    position_deltas = (
                        light_minimum + (quantized + 0.5) * light_step
                    )
                else:
                    half4 = (
                        records[:, :8]
                        .copy()
                        .view("<f2")
                        .reshape(item.vertex_count, 4)
                    )
                    position_deltas = half4[:, :3].astype(np.float32)
                    position_w = half4[:, 3].astype(np.float32)
                if not np.isfinite(position_deltas).all():
                    raise ValueError(
                        f"DMC5 blendshape channel {channel_index} contains "
                        "a nonfinite position delta"
                    )
                packed_normal = (
                    records[:, position_stride : position_stride + 4]
                    .copy()
                    .view(np.int8)
                    if explicit_normals
                    else None
                )
                target_normals = (
                    np.maximum(
                        packed_normal[:, :3].astype(np.float32) / 127.0,
                        -1.0,
                    )
                    if packed_normal is not None
                    else None
                )
                channel.segments.append(
                    BlendShapeDeltaSegment(
                        item.base_vertex_location,
                        position_deltas,
                        position_w,
                        target_normals,
                        (
                            packed_normal[:, 3].copy()
                            if packed_normal is not None
                            else None
                        ),
                    )
                )
                entry_cursor += item.vertex_count

    if entry_cursor != entry_count:
        raise ValueError("DMC5 blendshape payload entry count is inconsistent")
