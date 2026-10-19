from __future__ import annotations

from dataclasses import dataclass, field
import struct
from typing import List, Optional, Tuple

import numpy as np

from .mesh_file import _checked_slice
from .models import MeshAABB
lazy from .profiles import (
    BlendShapeLayout,
    MeshGameProfile,
    MeshMainVersion,
    coerce_mesh_profile,
)


@dataclass(frozen=True)
class BlendShapeTargetRange:
    base_vertex_location: int
    blend_shape_location: int
    vertex_count: int
    part_id: int = 0
    surface_range_id: int = 0
    derived_from_target: bool = False

    @property
    def tag(self) -> int:
        return int(self.part_id) | (int(self.surface_range_id) << 8)


@dataclass
class BlendShapeTarget:
    channel_location: int
    channel_count: int
    same_target_id: int
    ranges: List[BlendShapeTargetRange]
    aabb: Optional[MeshAABB]
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
    buffer_index: int = 0
    lod_index: int = 0
    target_index: int = 0
    range_index: int = 0
    deform_percent: Optional[np.ndarray] = None
    vertex_cull_mask: Optional[np.ndarray] = None

    def position_contribution(self, weight: float) -> np.ndarray:
        return self.position_deltas * float(weight)


@dataclass
class BlendShapeBody:
    lod_index: int
    compression_type: int
    channel_offset: int
    channel_count: int
    targets: List[BlendShapeTarget]
    none_targets: List[BlendShapeTarget] = field(default_factory=list)
    channels: List[BlendShapeChannel] = field(default_factory=list)
    target_lod_map: List[int] = field(default_factory=list)
    channel_lod_map: List[int] = field(default_factory=list)
    alias_of_lod: Optional[int] = None
    reserve: bytes = b""
    body_offset: int = 0
    target_offset: int = 0
    aabb_offset: int = 0
    target_lod_map_offset: int = 0
    channel_lod_map_offset: int = 0
    raw_unvisualized_payload: bytes = b""


@dataclass(frozen=True)
class BlendShapeBoneModifierEffector:
    joint_index: int
    vertex_indices: Tuple[int, ...]
    vertex_positions: np.ndarray

    def sampled_positions(self, positions: np.ndarray) -> np.ndarray:
        source = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
        indices = np.asarray(self.vertex_indices, dtype=np.int64)
        if np.any(indices < 0) or np.any(indices >= len(source)):
            raise ValueError(
                f"bone modifier for joint {self.joint_index} indexes outside "
                "the position buffer"
            )
        return np.ascontiguousarray(source[indices], dtype=np.float32)

    def position_deltas(self, deformed_positions: np.ndarray) -> np.ndarray:
        sampled = self.sampled_positions(deformed_positions)
        reference = np.asarray(
            self.vertex_positions,
            dtype=np.float32,
        ).reshape(-1, 3)
        if len(reference) != len(sampled):
            raise ValueError(
                f"bone modifier for joint {self.joint_index} has "
                "mismatched index/reference counts"
            )
        return np.ascontiguousarray(sampled - reference, dtype=np.float32)


@dataclass
class BlendShapeBoneModifier:
    offset: int
    reserve: int
    effectors: List[BlendShapeBoneModifierEffector] = field(default_factory=list)

    def position_deltas(
        self,
        deformed_positions: np.ndarray,
    ) -> Tuple[np.ndarray, ...]:
        return tuple(
            effector.position_deltas(deformed_positions)
            for effector in self.effectors
        )


class BlendShapeData:
    def __init__(
        self,
        bodies: Optional[List[BlendShapeBody]] = None,
        layout_reserve: bytes = b"",
        bone_modifier: Optional[BlendShapeBoneModifier] = None,
        layout_unknown_fields: Tuple[int, ...] = (),
        legacy_aux_offsets: Tuple[int, int] = (0, 0),
        legacy_blend_s: Tuple[int, ...] = (),
        legacy_blend_ss: Tuple[int, ...] = (),
        unresolved_semantics: Optional[List[str]] = None,
    ) -> None:
        self.layout_reserve = bytes(layout_reserve)
        self.bone_modifier = bone_modifier
        self.layout_unknown_fields = tuple(layout_unknown_fields)
        self.legacy_aux_offsets = tuple(legacy_aux_offsets)
        self.legacy_blend_s = tuple(legacy_blend_s)
        self.legacy_blend_ss = tuple(legacy_blend_ss)
        self.unresolved_semantics = list(unresolved_semantics or ())
        self.bodies = list(bodies or ())

    def _primary_body(self) -> Optional[BlendShapeBody]:
        return min(self.bodies, key=lambda body: body.lod_index, default=None)

    @property
    def channels(self) -> List[BlendShapeChannel]:
        body = self._primary_body()
        return body.channels if body is not None else []

    @property
    def targets(self) -> List[BlendShapeTarget]:
        body = self._primary_body()
        if body is None:
            return []
        if not body.none_targets:
            return body.targets
        return [*body.targets, *body.none_targets]

    @property
    def target_channel_count(self) -> int:
        return sum(
            target.channel_count
            for target in self.targets
            if target.is_blend_shape
        )

    def set_channel_names(self, name_indices: List[int], names: List[str]) -> None:
        for body in self.bodies:
            for channel in body.channels:
                channel.name = ""
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
) -> MeshAABB:
    values = _unpack(
        "<8f",
        data,
        offset + index * 32,
        f"Blendshape AABB {index}",
    )
    return MeshAABB(
        tuple(values[:3]),
        tuple(values[4:7]),
        minimum_w=values[3],
        maximum_w=values[7],
    )


def _parse_legacy(
    data: bytes | bytearray,
    offset: int,
    profile: MeshGameProfile,
) -> BlendShapeData:
    target_count, compression_type, reserved, targets_offset, aabb_offset = (
        _unpack("<HHIqq", data, offset, "Legacy blendshape header")
    )
    canonical_legacy = profile.legacy_reserved_storage_is_corpus_zero
    if canonical_legacy and reserved:
        raise ValueError(
            f"{profile.display_name} legacy blendshape header reserve must be zero"
        )
    if target_count and targets_offset <= 0:
        raise ValueError("Legacy blendshape header has no target list")
    raw_targets = []
    payload_cursor = 0
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
        if canonical_legacy and blend_ss_index:
            raise ValueError(
                f"{profile.display_name} legacy target {target_index} "
                "blendSSIndex must be zero"
            )
        if canonical_legacy and target_reserved:
            raise ValueError(
                f"{profile.display_name} legacy target {target_index} "
                "reserve must be zero"
            )
        raw_targets.append(
            (base_vertex_location, vertex_count, blend_ss_index, channel_count, target_reserved, payload_cursor)
        )
        payload_cursor += vertex_count * channel_count

    blend_s_offset = blend_ss_offset = 0
    blend_s: Tuple[int, ...] = ()
    blend_ss: Tuple[int, ...] = ()
    total_shape_entries = sum(item[3] for item in raw_targets)
    if target_count:
        blend_s_offset, blend_ss_offset = _unpack(
            "<ii",
            data,
            targets_offset + target_count * 16,
            "Legacy blendshape auxiliary offsets",
        )
        if canonical_legacy and (blend_s_offset or blend_ss_offset):
            raise ValueError(
                f"{profile.display_name} legacy blendshape auxiliary suffix "
                "must be zero"
            )
        if blend_s_offset:
            blend_s = tuple(
                _unpack("<3i", data, blend_s_offset, "Legacy blendS table")
            )
        if blend_ss_offset and total_shape_entries:
            blend_ss = tuple(
                _unpack(
                    f"<{total_shape_entries}i",
                    data,
                    blend_ss_offset,
                    "Legacy blendSS table",
                )
            )

    targets: List[BlendShapeTarget] = []
    channel_cursor = 0
    unresolved = []
    for target_index, item in enumerate(raw_targets):
        (base_vertex_location, vertex_count, blend_ss_index, channel_count,
         target_reserved, target_payload_cursor) = item
        channel_location = channel_cursor
        if blend_ss and 0 <= blend_ss_index < len(blend_ss):
            mapped = blend_ss[
                blend_ss_index : blend_ss_index + channel_count
            ]
            if len(mapped) == channel_count and mapped:
                if tuple(mapped) == tuple(range(mapped[0], mapped[0] + len(mapped))):
                    channel_location = mapped[0]
                else:
                    unresolved.append(
                        f"Legacy target {target_index} has a non-contiguous "
                        "blendSS channel map; raw values are preserved."
                    )
        aabb = (
            _read_aabb(data, aabb_offset, target_index)
            if aabb_offset > 0
            else None
        )
        targets.append(
            BlendShapeTarget(
                channel_location=channel_location,
                channel_count=channel_count,
                same_target_id=target_index,
                # No delta place is recorded: files pack deltas after earlier targets' channels, while RE2, RE3 and DMC5
                # read them at the sum of earlier vertex counts (``pretransform.VERTEX_SUM``).
                ranges=[
                    BlendShapeTargetRange(
                        base_vertex_location=base_vertex_location,
                        blend_shape_location=target_payload_cursor,
                        vertex_count=vertex_count,
                        derived_from_target=True,
                    )
                ],
                aabb=aabb,
                is_blend_shape=bool(channel_count and vertex_count),
                raw_reserve=(target_reserved & 0xFFFF, target_reserved >> 16),
            )
        )
        if target_reserved and not canonical_legacy:
            unresolved.append(
                f"Legacy target {target_index} reserve is nonzero "
                f"(0x{target_reserved:08X}); its meaning is unknown."
            )
        channel_cursor += channel_count
    if reserved and not canonical_legacy:
        unresolved.append(
            f"Legacy blendshape header reserve is nonzero (0x{reserved:08X}); "
            "its runtime meaning is unknown."
        )
    if blend_s and not canonical_legacy:
        unresolved.append(
            "Legacy blendS contains three auxiliary integers whose runtime "
            "meaning is unknown."
        )
    body = BlendShapeBody(
        lod_index=0,
        compression_type=compression_type,
        channel_offset=0,
        channel_count=channel_cursor,
        targets=targets,
        body_offset=offset,
        target_offset=targets_offset,
        aabb_offset=aabb_offset,
    )
    return BlendShapeData(
        bodies=[body],
        legacy_aux_offsets=(blend_s_offset, blend_ss_offset),
        legacy_blend_s=blend_s,
        legacy_blend_ss=blend_ss,
        unresolved_semantics=unresolved,
    )


def _read_target_ranges(
    data: bytes | bytearray,
    target_index: int,
    range_count: int,
    ranges_offset: int,
    profile: MeshGameProfile,
) -> List[BlendShapeTargetRange]:
    if range_count and ranges_offset <= 0:
        raise ValueError(f"Blendshape target {target_index} has no range pointer")

    ranges = []
    for range_index in range(range_count):
        base_vertex, blend_location, vertex_count, packed_identity = _unpack(
            "<IIII",
            data,
            ranges_offset + range_index * 16,
            f"Blendshape target {target_index} range {range_index}",
        )
        if profile.blend_shape_range_has_part_surface_identity:
            if packed_identity & 0xFFFF0000:
                raise ValueError(
                    f"{profile.display_name} blendshape target {target_index} "
                    f"range {range_index} identity reserve must be zero"
                )
            part_id = packed_identity & 0xFF
            surface_range_id = (packed_identity >> 8) & 0xFF
        else:
            if packed_identity:
                raise ValueError(
                    f"{profile.display_name} blendshape target {target_index} "
                    f"range {range_index} reserve must be zero, got "
                    f"0x{packed_identity:08X}"
                )
            part_id = surface_range_id = 0
        ranges.append(
            BlendShapeTargetRange(
                base_vertex_location=base_vertex,
                blend_shape_location=blend_location,
                vertex_count=vertex_count,
                part_id=part_id,
                surface_range_id=surface_range_id,
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
    profile: MeshGameProfile,
) -> tuple[BlendShapeTarget, int, int]:
    is_none_target = target_index >= target_count
    target_offset = targets_offset + target_index * 16
    if profile.has_modern_blend_shape_targets:
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
        if (
            profile.has_extended_blend_shape_layout
            and channel_location >= channel_offset
        ):
            channel_location -= channel_offset
        ranges = _read_target_ranges(
            data,
            target_index,
            range_count,
            ranges_offset,
            profile,
        )
        part_index = None
        raw_reserve = None
        compatibility_fields_derived = False
    else:
        # RE8 and the other pre-RE4 layouts store one implicit range per target.
        base_vertex, vertex_count, part_index, channel_count = _unpack(
            "<IIHH",
            data,
            target_offset,
            f"Pre-RE4 blendshape target {target_index}",
        )
        if profile.blend_shape_layout in {
            BlendShapeLayout.RE2_RT,
            BlendShapeLayout.MHRISE,
        }:
            game_label = (
                "RE2 RT"
                if profile.blend_shape_layout is BlendShapeLayout.RE2_RT
                else "MHRise"
            )
            # The +12 halfword is never read at runtime; the final halfword is zero padding.
            _checked_slice(
                data,
                target_offset + 12,
                2,
                f"{game_label} blendshape target {target_index} producer residue",
            )
            target_padding = _unpack(
                "<H",
                data,
                target_offset + 14,
                f"{game_label} blendshape target {target_index} padding",
            )[0]
            if target_padding:
                raise ValueError(
                    f"{game_label} blendshape target {target_index} padding must be "
                    f"zero, got 0x{target_padding:04X}"
                )
            raw_reserve = None
        else:
            shared_field = _unpack(
                "<I",
                data,
                target_offset + 12,
                f"Pre-RE4 blendshape target {target_index} shared field",
            )[0]
            raw_reserve = (shared_field & 0xFFFF, shared_field >> 16)
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


def lod_map_allocation(count: int) -> int:
    return (int(count) + 3) & ~3


def blend_shape_lod_map_counts(bodies, profile: MeshGameProfile) -> list[tuple[int, int]]:
    if profile.blend_shape_layout.has_lod_maps:
        return [(len(body.targets), sum(int(target.channel_count) for target in (*body.targets, *body.none_targets)))
                for body in bodies]
    if not profile.has_modern_blend_shape_targets:
        return [(0, 0)] * len(bodies)
    shared = (
        max((len(body.targets) for body in bodies), default=0),
        max((max([int(body.channel_count), *(int(target.channel_location) + int(target.channel_count)
                                             for target in (*body.targets, *body.none_targets))])
             for body in bodies), default=0),
    )
    return [shared] * len(bodies)


def _read_lod_map(
    data: bytes | bytearray,
    offset: int,
    count: int,
    limit: int,
    label: str,
) -> List[int]:
    if count <= 0:
        return []
    if offset <= 0:
        raise ValueError(f"{label} has no pointer for {count} entries")
    required_size = count * 4
    required_end = offset + required_size
    if limit < required_end:
        raise ValueError(
            f"{label} cannot hold its semantic {count}-entry array"
        )
    if limit != required_end:
        raise ValueError(
            f"{label} allocation does not exactly match its semantic "
            f"{count}-entry array ({required_end} != {limit})"
        )
    return list(_unpack(f"<{count}i", data, offset, label))


def parse_bone_modifier(
    data: bytes | bytearray,
    offset: int,
) -> Optional[BlendShapeBoneModifier]:
    if offset <= 0:
        return None
    effector_count, reserve, effectors_offset = _unpack(
        "<IIq", data, offset, "Blendshape bone-modifier header"
    )
    if effector_count > 65536:
        raise ValueError(
            f"Blendshape bone-modifier effector count {effector_count} is invalid"
        )
    if effector_count and effectors_offset <= 0:
        raise ValueError("Blendshape bone modifier has no effector list")
    offsets = (
        _unpack(
            f"<{effector_count}q",
            data,
            effectors_offset,
            "Blendshape bone-modifier pointers",
        )
        if effector_count
        else ()
    )
    result = BlendShapeBoneModifier(offset, reserve)
    for effector_index, effector_offset in enumerate(offsets):
        if effector_offset <= 0:
            continue
        joint_index, vertex_count, indices_offset, positions_offset = _unpack(
            "<IIqq",
            data,
            effector_offset,
            f"Blendshape bone-modifier effector {effector_index}",
        )
        if vertex_count and (indices_offset <= 0 or positions_offset <= 0):
            raise ValueError(
                f"Blendshape effector {effector_index} has no vertex indices or positions"
            )
        indices = (
            tuple(
                _unpack(
                    f"<{vertex_count}I",
                    data,
                    indices_offset,
                    f"Blendshape effector {effector_index} indices",
                )
            )
            if vertex_count
            else ()
        )
        position_raw = _checked_slice(
            data,
            positions_offset,
            vertex_count * 12,
            f"Blendshape effector {effector_index} positions",
        )
        positions = np.frombuffer(position_raw, dtype="<f4").reshape(-1, 3).copy()
        result.effectors.append(
            BlendShapeBoneModifierEffector(
                joint_index,
                indices,
                positions,
            )
        )
    return result


def _parse_modern(
    data: bytes | bytearray,
    offset: int,
    profile: MeshGameProfile,
    *,
    section_end: Optional[int] = None,
    external_bone_modifier_offset: int = 0,
) -> BlendShapeData:
    lod_count = _unpack("<B", data, offset, "Blendshape LOD count")[0]
    if lod_count <= 0:
        raise ValueError("Blendshape layout has no LOD bodies")

    layout_reserve_size = (
        15 if profile.has_extended_blend_shape_layout else 7
    )
    layout_reserve = _checked_slice(
        data,
        offset + 1,
        layout_reserve_size,
        "Blendshape layout reserve",
    )
    unresolved: List[str] = []
    if (
        any(layout_reserve)
        and not profile.blend_shape_reserved_storage_is_inert
    ):
        unresolved.append("BlendShapeLayout reserved bytes are nonzero.")
    if profile.blend_shape_layout is BlendShapeLayout.RE_RT:
        unresolved.append(
            "The final four bytes of each RE_RT blendshape target are preserved "
            "as a shared raw field; their meaning is unknown."
        )

    bone_modifier_offset = external_bone_modifier_offset
    # Some serializers keep an unread 8-byte tail at +24..+31.
    layout_unknown_fields: Tuple[int, ...] = ()
    if profile.has_blend_shape_lod_maps:
        pointer_offset = offset + (
            16 if profile.has_extended_blend_shape_layout else 8
        )
        bodies_offset, layout_bone_modifier_offset = _unpack(
            "<qq",
            data,
            pointer_offset,
            "Blendshape layout pointers",
        )
        if layout_bone_modifier_offset > 0:
            bone_modifier_offset = layout_bone_modifier_offset
        if profile.blend_shape_layout_tail_is_producer_residue:
            game_label = profile.display_name
            _checked_slice(
                data,
                pointer_offset + 16,
                6,
                f"{game_label} BlendShapeLayout producer residue",
            )
            layout_padding = _unpack(
                "<H",
                data,
                pointer_offset + 22,
                f"{game_label} BlendShapeLayout padding",
            )[0]
            if layout_padding:
                raise ValueError(
                    f"{game_label} BlendShapeLayout padding must be zero, got "
                    f"0x{layout_padding:04X}"
                )
        elif profile.blend_shape_layout in {BlendShapeLayout.RE_RT}:
            layout_unknown_fields = tuple(
                _unpack(
                    "<4H",
                    data,
                    pointer_offset + 16,
                    "Blendshape layout unknown fields",
                )
            )
            if any(layout_unknown_fields[:3]):
                unresolved.append(
                    "RE_RT BlendShapeLayout contains three preserved uint16 "
                    "fields whose semantics are unknown."
                )
    else:
        bodies_offset = _unpack(
            "<q",
            data,
            offset + 8,
            "Blendshape body-list pointer",
        )[0]

    if bodies_offset <= 0:
        raise ValueError("Blendshape body-list pointer is absent")
    body_offsets = tuple(
        _unpack(
            f"<{lod_count}q",
            data,
            bodies_offset,
            "Blendshape body pointers",
        )
    )
    if not any(body_offset > 0 for body_offset in body_offsets):
        raise ValueError("Blendshape layout has no valid body pointer")

    section_limit = len(data) if section_end is None else min(section_end, len(data))
    bodies: List[BlendShapeBody] = []
    body_owner_by_offset: dict[int, int] = {}
    for lod_index, body_offset in enumerate(body_offsets):
        if body_offset <= 0:
            continue
        alias_of_lod = body_owner_by_offset.get(int(body_offset))
        if alias_of_lod is None:
            body_owner_by_offset[int(body_offset)] = lod_index
        if profile.has_modern_blend_shape_targets:
            target_count, none_target_count = _unpack(
                "<HH", data, body_offset, f"Blendshape body {lod_index} counts"
            )
            if profile.has_extended_blend_shape_layout:
                channel_offset, body_channel_count, compression_type = _unpack(
                    "<HHB",
                    data,
                    body_offset + 4,
                    f"Blendshape body {lod_index} channels",
                )
            else:
                compression_type, body_channel_count, channel_offset = _unpack(
                    "<BHH",
                    data,
                    body_offset + 4,
                    f"Blendshape body {lod_index} channels",
                )
            body_reserve = _checked_slice(
                data,
                body_offset + 9,
                7,
                f"Blendshape body {lod_index} reserve",
            )
            (
                targets_offset,
                aabb_offset,
                target_lod_map_offset,
                channel_lod_map_offset,
            ) = _unpack(
                "<qqqq",
                data,
                body_offset + 16,
                f"Blendshape body {lod_index} pointers",
            )
        else:
            target_count, compression_type = _unpack(
                "<HB",
                data,
                body_offset,
                f"Pre-RE4 blendshape body {lod_index}",
            )
            none_target_count = 0
            channel_offset = 0
            body_channel_count = 0
            body_reserve = _checked_slice(
                data,
                body_offset + 3,
                13,
                f"Pre-RE4 blendshape body {lod_index} reserve",
            )
            targets_offset, aabb_offset = _unpack(
                "<qq",
                data,
                body_offset + 16,
                f"Pre-RE4 blendshape body {lod_index} pointers",
            )
            if profile.blend_shape_layout.has_lod_maps:
                target_lod_map_offset, channel_lod_map_offset = _unpack(
                    "<qq",
                    data,
                    body_offset + 32,
                    f"RE_RT blendshape body {lod_index} LOD maps",
                )
            else:
                target_lod_map_offset = channel_lod_map_offset = 0
        if (target_count or none_target_count) and targets_offset <= 0:
            raise ValueError(f"Blendshape body {lod_index} has no target list")
        if (
            any(body_reserve)
            and not profile.blend_shape_reserved_storage_is_inert
        ):
            unresolved.append(
                f"Blendshape body {lod_index} reserved bytes are nonzero."
            )

        parsed_targets: List[BlendShapeTarget] = []
        sequential_channel = 0
        sequential_payload = 0
        for target_index in range(target_count + none_target_count):
            target, sequential_channel, sequential_payload = _read_modern_target(
                data,
                target_index=target_index,
                target_count=target_count,
                targets_offset=targets_offset,
                aabb_offset=aabb_offset,
                channel_offset=channel_offset,
                sequential_channel=sequential_channel,
                sequential_payload=sequential_payload,
                profile=profile,
            )
            parsed_targets.append(target)
        active_targets = parsed_targets[:target_count]
        none_targets = parsed_targets[target_count:]
        canonical_channels = max(
            [body_channel_count]
            + [
                target.channel_location + target.channel_count
                for target in parsed_targets
            ]
        )
        if not body_channel_count:
            body_channel_count = canonical_channels
        bodies.append(
            BlendShapeBody(
                lod_index=lod_index,
                compression_type=compression_type,
                channel_offset=channel_offset,
                channel_count=body_channel_count,
                targets=active_targets,
                none_targets=none_targets,
                alias_of_lod=alias_of_lod,
                reserve=body_reserve,
                body_offset=body_offset,
                target_offset=targets_offset,
                aabb_offset=aabb_offset,
                target_lod_map_offset=target_lod_map_offset,
                channel_lod_map_offset=channel_lod_map_offset,
            )
        )

    for body, (target_map_count, channel_map_count) in zip(bodies, blend_shape_lod_map_counts(bodies, profile)):
        body_end = min(
            [section_limit]
            + [
                candidate
                for candidate in body_offsets
                if candidate > body.body_offset
            ]
            + ([bone_modifier_offset] if bone_modifier_offset > body.body_offset else [])
        )
        target_limit = body_end
        channel_limit = body_end
        if (
            body.channel_lod_map_offset > body.target_lod_map_offset
            and body.channel_lod_map_offset < target_limit
        ):
            target_limit = body.channel_lod_map_offset
        if (
            body.target_lod_map_offset > body.channel_lod_map_offset
            and body.target_lod_map_offset < channel_limit
        ):
            channel_limit = body.target_lod_map_offset
        body.target_lod_map = _read_lod_map(
            data,
            body.target_lod_map_offset,
            lod_map_allocation(target_map_count),
            target_limit,
            f"Blendshape body {body.lod_index} target LOD map",
        )
        body.channel_lod_map = _read_lod_map(
            data,
            body.channel_lod_map_offset,
            lod_map_allocation(channel_map_count),
            channel_limit,
            f"Blendshape body {body.lod_index} channel LOD map",
        )

    bone_modifier = parse_bone_modifier(data, bone_modifier_offset)
    return BlendShapeData(
        layout_reserve=layout_reserve,
        bodies=bodies,
        bone_modifier=bone_modifier,
        layout_unknown_fields=layout_unknown_fields,
        unresolved_semantics=unresolved,
    )


def parse_blend_shapes(
    data: bytes | bytearray,
    offset: int,
    profile: MeshGameProfile | MeshMainVersion | int,
    *,
    section_end: Optional[int] = None,
    external_bone_modifier_offset: int = 0,
) -> BlendShapeData:
    profile = coerce_mesh_profile(profile)
    result = (
        _parse_legacy(data, offset, profile)
        if profile.blend_shape_layout is BlendShapeLayout.LEGACY
        else _parse_modern(
            data,
            offset,
            profile,
            section_end=section_end,
            external_bone_modifier_offset=external_bone_modifier_offset,
        )
    )
    name_offsets = blend_shape_name_offsets(result, profile)
    for body in result.bodies:
        active_channel_ids = sorted(
            {
                channel_index
                for target in body.targets
                if target.is_blend_shape and target.ranges
                for channel_index in range(
                    target.channel_location,
                    target.channel_location + target.channel_count,
                )
            }
        )
        body.channels = [
            BlendShapeChannel(
                index=channel_index,
                name_slot=name_offsets[body.lod_index] + channel_index,
            )
            for channel_index in active_channel_ids
        ]
    return result


def blend_shape_name_offsets(blend, profile):
    offsets, cursor = {}, 0
    for body in sorted(blend.bodies, key=lambda item: item.lod_index):
        offsets[body.lod_index] = body.channel_offset if profile.has_extended_blend_shape_layout else cursor
        cursor += body.channel_count
    return offsets


def read_blend_shape_name_indices(
    data: bytes | bytearray,
    offset: int,
    *,
    semantic_name_count: int,
    section_size: int,
    blend_shapes: BlendShapeData,
) -> List[int]:
    if section_size < 0:
        raise ValueError("Blendshape name-index section has a negative size")
    index_count = int(semantic_name_count)
    if index_count < 0:
        raise ValueError("Blendshape name-index count is negative")
    channels = [
        *body.channels
        for body in blend_shapes.bodies
    ]
    active_channel_slot_count = max(
        (channel.name_slot + 1 for channel in channels),
        default=blend_shapes.target_channel_count,
    )
    if active_channel_slot_count > index_count:
        raise ValueError(
            "Blendshape active channel slots exceed the semantic name "
            f"partition ({active_channel_slot_count} > {index_count})"
        )
    required_size = index_count * 2
    allocated_size = (required_size + 15) & ~15
    if section_size != allocated_size:
        raise ValueError(
            "Blendshape name-index allocation must exactly match its semantic "
            f"{index_count}-entry array aligned to 16 bytes "
            f"({section_size} != {allocated_size})"
        )
    alignment_padding = _checked_slice(
        data,
        offset + required_size,
        allocated_size - required_size,
        "Blendshape name-index alignment padding",
    )
    if any(alignment_padding):
        raise ValueError("Blendshape name-index alignment padding is nonzero")
    if index_count <= 0:
        return []
    raw = _checked_slice(data, offset, index_count * 2, "Blendshape name indices")
    return list(struct.unpack(f"<{index_count}h", raw))


def _note_unresolved(blend_shapes: BlendShapeData, message: str) -> None:
    if message not in blend_shapes.unresolved_semantics:
        blend_shapes.unresolved_semantics.append(message)


def decode_blend_shape_payloads(
    blend_shapes: BlendShapeData,
    payloads: dict[int, object],
    payload_offsets: dict[int, int],
    vertex_counts: dict[int, int],
    body_buffer_indices: dict[int, int],
    profile: MeshGameProfile | MeshMainVersion | int,
    *,
    explicit_normals: bool,
) -> None:
    profile = coerce_mesh_profile(profile)

    implicit_offsets = {}
    body_offsets = {}
    for body in blend_shapes.bodies:
        body.raw_unvisualized_payload = b""
        for channel in body.channels:
            channel.segments.clear()
        buffer_index = int(body_buffer_indices.get(body.lod_index, 0))
        payload = payloads.get(buffer_index)
        if payload is None:
            raise ValueError(
                f"Blendshape LOD {body.lod_index} needs mesh buffer {buffer_index}"
            )
        vertex_bytes = bytes(getattr(payload, "vertex_bytes", b""))
        payload_offset = int(payload_offsets.get(buffer_index, 0))
        vertex_count = int(vertex_counts.get(buffer_index, 0))
        base_stride = {0: 4, 1: 8, 2: 16}.get(body.compression_type)
        if body.compression_type == 3:
            if profile.blend_shape_type3_is_enum_sentinel:
                raise ValueError(
                    f"{profile.display_name} BlendShapeType_Max (3) is an "
                    "enum sentinel and "
                    "cannot describe a blend-shape payload"
                )
            base_stride = 3
            if not profile.blend_shape_u8x3_uses_aabb_quantization:
                _note_unresolved(
                    blend_shapes,
                    "BlendShapeType value 3 is stored as packed U8x3 in "
                    "newer files, but this profile has no verified "
                    "dequantization contract; raw payload bytes are "
                    "preserved."
                )
        if base_stride is None:
            raise ValueError(
                f"Unsupported blendshape compression type {body.compression_type}"
            )
        stride = base_stride + (4 if explicit_normals else 0)
        # Range locations address channel zero; later channels advance by the vertex count of all the target's ranges.
        maximum_entry = 0
        target_channel_strides = []
        for target in body.targets:
            channel_stride = sum(
                int(item.vertex_count) for item in target.ranges
            )
            target_channel_strides.append(channel_stride)
            channel_zero_end = max(
                (
                    int(item.blend_shape_location)
                    + int(item.vertex_count)
                    for item in target.ranges
                ),
                default=0,
            )
            maximum_entry = max(
                maximum_entry,
                channel_zero_end
                + max(0, int(target.channel_count) - 1) * channel_stride,
            )
        required_size = maximum_entry * stride
        if not profile.has_modern_blend_shape_targets:
            cursor = implicit_offsets.get(buffer_index, 0)
            cursor += -cursor % stride
            if body.alias_of_lod is not None:
                cursor = body_offsets[body.alias_of_lod]
            else:
                implicit_offsets[buffer_index] = cursor + required_size
            body_offsets[body.lod_index] = cursor
            payload_offset += cursor
        if not required_size:
            continue
        if payload_offset <= 0:
            raise ValueError(
                f"Blendshape LOD {body.lod_index} has no payload offset"
            )
        raw_payload = _checked_slice(
            vertex_bytes,
            payload_offset,
            required_size,
            f"Blendshape LOD {body.lod_index} payload",
        )
        if (
            body.compression_type == 3
            and not profile.blend_shape_u8x3_uses_aabb_quantization
        ):
            body.raw_unvisualized_payload = raw_payload
            continue

        if (
            body.compression_type == 1
            and not (
                profile.blend_shape_standard_w_is_padding
                or profile.blend_shape_standard_w_is_runtime_ignored
            )
        ):
            _note_unresolved(
                blend_shapes,
                "BlendShapeVertexStandard stores Half4 position. XYZ are decoded "
                "as position deltas; position.w is preserved but its runtime "
                "meaning is unknown for this profile."
            )
        if explicit_normals and not (
            profile.blend_shape_normal_w_is_padding
            or profile.blend_shape_normal_w_is_runtime_ignored
        ):
            _note_unresolved(
                blend_shapes,
                "BlendShapeVertexNormal* appends Norm4<int> normal. XYZ are "
                "decoded as target normals; normal.w is preserved without a "
                "speculative runtime interpretation for this profile."
            )

        channels = {channel.index: channel for channel in body.channels}
        for target_index, target in enumerate(body.targets):
            if not target.is_blend_shape:
                continue
            channel_stride = target_channel_strides[target_index]
            quantized_minimum = quantized_step = None
            if body.compression_type in (0, 3):
                if target.aabb is None:
                    raise ValueError(
                        f"Blendshape target {target_index} has no quantization AABB"
                    )
                quantized_minimum = np.asarray(
                    target.aabb.minimum,
                    dtype=np.float32,
                )
                divisor = np.asarray(
                    (2047.0, 1023.0, 2047.0)
                    if body.compression_type == 0
                    else (255.0, 255.0, 255.0),
                    dtype=np.float32,
                )
                quantized_step = (
                    np.asarray(target.aabb.maximum, dtype=np.float32)
                    - quantized_minimum
                ) / divisor
                if not (
                    np.isfinite(quantized_minimum).all()
                    and np.isfinite(quantized_step).all()
                ):
                    raise ValueError(
                        f"Blendshape target {target_index} has a nonfinite AABB"
                    )
            for local_channel in range(target.channel_count):
                channel_index = target.channel_location + local_channel
                channel = channels.get(channel_index)
                if channel is None:
                    continue
                for range_index, item in enumerate(target.ranges):
                    destination_end = (
                        item.base_vertex_location + item.vertex_count
                    )
                    if (
                        item.base_vertex_location < 0
                        or item.vertex_count < 0
                        or destination_end > vertex_count
                    ):
                        raise ValueError(
                            f"Blendshape LOD {body.lod_index} channel "
                            f"{channel_index} has an invalid destination range"
                        )
                    entry_index = (
                        item.blend_shape_location
                        + local_channel * channel_stride
                    )
                    start = entry_index * stride
                    records = np.frombuffer(
                        _checked_slice(
                            raw_payload,
                            start,
                            item.vertex_count * stride,
                            f"Blendshape target {target_index} range {range_index}",
                        ),
                        dtype=np.uint8,
                    ).reshape(item.vertex_count, stride)
                    position_w = None
                    deform_percent = None
                    if body.compression_type == 0:
                        packed = records[:, :4].copy().view("<u4").reshape(-1)
                        quantized = np.column_stack(
                            (
                                packed & 0x7FF,
                                (packed >> 11) & 0x3FF,
                                (packed >> 21) & 0x7FF,
                            )
                        ).astype(np.float32)
                        position_deltas = (
                            quantized_minimum
                            + (quantized + 0.5) * quantized_step
                        )
                    elif body.compression_type == 1:
                        half4 = (
                            records[:, :8]
                            .copy()
                            .view("<f2")
                            .reshape(item.vertex_count, 4)
                        )
                        position_deltas = half4[:, :3].astype(np.float32)
                        position_w = half4[:, 3].astype(np.float32)
                    elif body.compression_type == 2:
                        quality = (
                            records[:, :16]
                            .copy()
                            .view("<f4")
                            .reshape(item.vertex_count, 4)
                        )
                        position_deltas = quality[:, :3].astype(np.float32)
                        deform_percent = quality[:, 3].astype(np.float32)
                    else:
                        quantized = records[:, :3].astype(np.float32)
                        position_deltas = (
                            quantized_minimum
                            + (quantized + 0.5) * quantized_step
                        )
                    vertex_cull_mask = np.any(
                        np.isnan(position_deltas),
                        axis=1,
                    )
                    if np.isinf(position_deltas).any():
                        raise ValueError(
                            f"Blendshape channel {channel_index} contains "
                            "an infinite position delta"
                        )
                    if np.any(vertex_cull_mask):
                        if not profile.blend_shape_nan_positions_cull_vertices:
                            raise ValueError(
                                f"Blendshape channel {channel_index} contains "
                                "a nonfinite position delta"
                            )
                        # SF6 feeds these NaNs through the blend shader; the preview culls the vertex instead.
                        position_deltas = position_deltas.copy()
                        position_deltas[vertex_cull_mask] = 0.0
                    else:
                        vertex_cull_mask = None
                    packed_normal = (
                        records[:, base_stride : base_stride + 4]
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
                            buffer_index,
                            body.lod_index,
                            target_index,
                            range_index,
                            deform_percent,
                            vertex_cull_mask,
                        )
                    )
