from __future__ import annotations

from dataclasses import dataclass, field
import struct
from typing import List, Optional, Tuple

from .mesh_file import MeshMainVersion


@dataclass(frozen=True)
class BlendShapeAABB:
    minimum: Tuple[float, float, float]
    maximum: Tuple[float, float, float]
    minimum_w: float = 0.0
    maximum_w: float = 0.0

    @property
    def minimum4(self) -> Tuple[float, float, float, float]:
        return (*self.minimum, self.minimum_w)

    @property
    def maximum4(self) -> Tuple[float, float, float, float]:
        return (*self.maximum, self.maximum_w)


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


def _checked_slice(
    data: bytes | bytearray,
    start: int,
    size: int,
    label: str,
) -> bytes:
    end = start + size
    if start < 0 or size < 0 or end > len(data):
        raise ValueError(f"{label} range [{start}, {end}) exceeds {len(data)} bytes")
    return bytes(memoryview(data)[start:end])


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
) -> BlendShapeData:
    target_count, compression_type, _reserved, targets_offset, aabb_offset = (
        _unpack("<HHIqq", data, offset, "Legacy blendshape header")
    )
    targets: List[BlendShapeTarget] = []
    payload_cursor = 0
    channel_cursor = 0
    for target_index in range(target_count):
        (
            base_vertex_location,
            vertex_count,
            _blend_ss_index,
            channel_count,
            _target_reserved,
        ) = _unpack(
            "<IIHHI",
            data,
            targets_offset + target_index * 16,
            f"Legacy blendshape target {target_index}",
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
    return BlendShapeData(
        compression_type=compression_type,
        channel_offset=0,
        targets=targets,
    )


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
        is_none_target = target_index >= target_count
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
                targets_offset + target_index * 16,
                f"Blendshape target {target_index}",
            )
            channel_location = raw_channel_location
            if (
                version >= MeshMainVersion.DD2_OLD
                and channel_location >= channel_offset
            ):
                channel_location -= channel_offset
            ranges = []
            if range_count and ranges_offset <= 0:
                raise ValueError(
                    f"Blendshape target {target_index} has no range pointer"
                )
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
                targets_offset + target_index * 16,
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
                targets_offset + target_index * 16,
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
        targets.append(
            BlendShapeTarget(
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
        )

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
        _parse_legacy(data, offset)
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
