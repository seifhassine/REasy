from __future__ import annotations

import struct
from dataclasses import dataclass

from utils.hash_util import murmur3_hash

from ..binary import ReadContext, align_up
from ..errors import MotionParseError
from ..profiles import MotionFormatProfile
from .model import (
    ASCII_VALUE_PROPERTY_TYPES,
    CONTAINER_PROPERTY_TYPES,
    UTF16_VALUE_PROPERTY_TYPES,
    Bezier3DCurve,
    ClipExtraRange,
    ClipInterpolation,
    ClipInterval,
    ClipKey,
    ClipNode,
    ClipProperty,
    ClipPropertyType,
    CompactMotClip,
    HermiteCurve,
    SpeedPoint,
)


CLIP_MAGIC = 0x50494C43


@dataclass(slots=True)
class CompactClipNodeRecord:
    offset: int
    index: int
    node: ClipNode
    child_index: int
    child_count: int
    property_index: int
    property_count: int
    ascii_name_offset: int
    unicode_name_index: int


@dataclass(slots=True)
class CompactClipPropertyRecord:
    offset: int
    index: int
    prop: ClipProperty
    member_index: int
    member_count: int
    has_last_key: bool
    last_key_index: int
    speed_index: int
    speed_count: int
    ascii_name_offset: int
    unicode_name_index: int
    flags: int


@dataclass(slots=True)
class CompactClipKeyRecord:
    offset: int
    index: int
    key: ClipKey
    payload: int
    curve_index: int
    packed_flags: int


@dataclass(slots=True)
class CompactClipSpeedRecord:
    offset: int
    index: int
    point: SpeedPoint
    curve_index: int


@dataclass(slots=True)
class CompactClipV27ParseResult:
    """One owner-neutral compact CLIP v27 payload plus its serialized layout.

    Owner containers wrap the same compact graph in different descriptor and
    boundary layouts. Keeping the record/table metadata here lets adapters
    retain editable source offsets without decoding the graph a second time.
    """

    clip: CompactMotClip
    clip_offset: int
    pointer_base: int
    header_guid: bytes
    section_relative_offsets: dict[str, int]
    section_absolute_offsets: dict[str, int]
    nodes: list[CompactClipNodeRecord]
    properties: list[CompactClipPropertyRecord]
    keys: list[CompactClipKeyRecord]
    speed_points: list[CompactClipSpeedRecord]
    last_keys: list[CompactClipKeyRecord]
    hermite_curves: list[HermiteCurve]
    bezier3d_curves: list[Bezier3DCurve]
    physical_end: int


class CompactClipV27Parser:
    """Parse the owner-neutral compact CLIP v27 graph.

    Owner adapters explicitly provide their boundary/layout policy and may
    identify interpolation modes whose payload is an ASCII-pool offset.
    """

    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot_clip=27)
        self.profile = profile

    def parse(
        self,
        context: ReadContext,
        clip_offset: int,
        following_data_offset: int | None,
        *,
        pointer_base: int,
        following_data_name: str,
        allow_missing_extra: bool = False,
        require_canonical_layout: bool = True,
        ascii_value_interpolations: frozenset[ClipInterpolation] = frozenset(),
    ) -> CompactMotClip:
        return self.parse_result(
            context,
            clip_offset,
            following_data_offset,
            pointer_base=pointer_base,
            allow_missing_extra=allow_missing_extra,
            require_canonical_layout=require_canonical_layout,
            following_data_name=following_data_name,
            ascii_value_interpolations=ascii_value_interpolations,
        ).clip

    def parse_result(
        self,
        context: ReadContext,
        clip_offset: int,
        following_data_offset: int | None,
        *,
        pointer_base: int,
        following_data_name: str,
        allow_missing_extra: bool = False,
        require_canonical_layout: bool = True,
        ascii_value_interpolations: frozenset[ClipInterpolation] = frozenset(),
    ) -> CompactClipV27ParseResult:
        layout = self.profile.mot_clip
        c = context
        c.require(clip_offset, layout.header_size, "compact MotClip header")
        if c.u32(clip_offset, "MotClip magic") != CLIP_MAGIC:
            raise MotionParseError(f"{c.label}: expected compact CLIP magic at 0x{clip_offset:X}")
        version = c.u32(clip_offset + 4, "MotClip version")
        if version != layout.version:
            raise MotionParseError(f"{c.label}: compact MotClip v{version} is unsupported")

        total_frame = c.f32(clip_offset + 8, "MotClip totalFrame")
        node_count = c.u32(clip_offset + 0xC, "MotClip nodeNum")
        property_count = c.u32(clip_offset + 0x10, "MotClip propertyNum")
        key_count = c.u32(clip_offset + 0x14, "MotClip keyNum")
        header_guid = c.bytes(clip_offset + 0x18, 16, "MotClip legacy GUID")
        fields = [c.u64(clip_offset + 0x28 + index * 8, "compact CLIP table pointer") for index in range(12)]
        (
            nodes_stored,
            props_stored,
            keys_stored,
            speed_stored,
            hermite_stored,
            bezier_stored,
            legacy_unused_stored,
            last_keys_stored,
            ascii_stored,
            unicode_stored,
            oword_stored,
            extra_stored,
        ) = fields

        def absolute(stored: int, what: str) -> int:
            value = pointer_base + stored
            c.require(value, 0, what)
            return value

        nodes_offset = absolute(nodes_stored, "MotClip node table")
        props_offset = absolute(props_stored, "MotClip property table")
        keys_offset = absolute(keys_stored, "MotClip key table")
        speed_offset = absolute(speed_stored, "MotClip speed table")
        hermite_offset = absolute(hermite_stored, "MotClip Hermite table")
        bezier_offset = absolute(bezier_stored, "MotClip Bezier table")
        legacy_unused_offset = absolute(legacy_unused_stored, "MotClip legacy-unused cursor")
        last_keys_offset = absolute(last_keys_stored, "MotClip last-key table")
        ascii_offset = absolute(ascii_stored, "MotClip ASCII strings")
        unicode_offset = absolute(unicode_stored, "MotClip UTF-16 strings")
        oword_offset = absolute(oword_stored, "MotClip OWord table")
        extra_offset = (
            absolute(extra_stored, "MotClip extra-property data")
            if extra_stored
            else None
        )

        if require_canonical_layout:
            expected = clip_offset + layout.header_size
            if nodes_offset != expected:
                raise MotionParseError(f"{c.label}: MotClip nodes are not immediately after the header")
            expected = align_up(nodes_offset + node_count * layout.node_size, 8)
            if props_offset != expected:
                raise MotionParseError(f"{c.label}: MotClip property table violates canonical layout")
            c.require_zero(nodes_offset + node_count * layout.node_size, props_offset, "MotClip node alignment")
            expected = props_offset + property_count * layout.property_size
            if keys_offset != expected:
                raise MotionParseError(f"{c.label}: MotClip key table violates canonical layout")

        nodes = self._read_nodes(c, nodes_offset, node_count, pointer_base, ascii_offset, unicode_offset)
        props = self._read_properties(c, props_offset, property_count, pointer_base, ascii_offset, unicode_offset)
        keys = self._read_keys(c, keys_offset, key_count)

        expected = keys_offset + key_count * layout.key_size
        if require_canonical_layout and speed_offset != expected:
            raise MotionParseError(f"{c.label}: MotClip speed table violates canonical layout")
        # Counts are owned by Property records; do not infer them from a boundary.
        speed_count = sum(c.u32(props_offset + index * layout.property_size + 0x10) for index in range(property_count))
        speeds = self._read_speed_points(c, speed_offset, speed_count)
        if require_canonical_layout and hermite_offset != speed_offset + speed_count * 0x18:
            raise MotionParseError(f"{c.label}: MotClip Hermite table violates canonical layout")

        last_count = sum(record.has_last_key for record in props)
        # The table address is explicit in the header; its count is explicit in
        # property flags, so parsing it here does not infer a count from layout.
        last_keys = self._read_keys(c, last_keys_offset, last_count)

        hermite_bytes = bezier_offset - hermite_offset
        if hermite_bytes < 0 or hermite_bytes % 0x10:
            raise MotionParseError(f"{c.label}: compact CLIP Hermite table has an invalid size")
        hermite_count = hermite_bytes // 0x10
        hermite_curves = [
            HermiteCurve(tuple(c.f32(hermite_offset + index * 0x10 + part * 4) for part in range(4)))
            for index in range(hermite_count)
        ]
        bezier_bytes = legacy_unused_offset - bezier_offset
        if bezier_bytes < 0 or bezier_bytes % 0x20:
            raise MotionParseError(f"{c.label}: compact CLIP Bezier3D table has an invalid size")
        bezier_count = bezier_bytes // 0x20
        bezier_curves = [
            Bezier3DCurve(tuple(c.f32(bezier_offset + index * 0x20 + part * 4) for part in range(8)))
            for index in range(bezier_count)
        ]
        if require_canonical_layout and last_keys_offset != legacy_unused_offset:
            raise MotionParseError(f"{c.label}: MotClip legacy curve cursors violate canonical layout")

        if require_canonical_layout and ascii_offset != last_keys_offset + last_count * layout.key_size:
            raise MotionParseError(f"{c.label}: MotClip ASCII strings violate canonical layout")

        self._attach_graph(c, nodes, props, keys, last_keys, speeds)
        self._attach_curves(c, keys, last_keys, speeds, hermite_curves, bezier_curves)
        oword_count = self._decode_values(
            c,
            props,
            [*keys, *last_keys],
            ascii_offset,
            unicode_offset,
            oword_offset,
            ascii_value_interpolations,
        )
        oword_end = oword_offset + oword_count * 0x10
        aligned_oword_end = align_up(oword_end, 16)
        if extra_offset is None:
            if not allow_missing_extra:
                raise MotionParseError(
                    f"{c.label}: compact CLIP extra-property pointer is absent"
                )
            extra_ranges = []
            # Owners without an extra-range table end at the exact OWord
            # boundary; any following alignment belongs to the owner layout.
            physical_end = oword_end
        else:
            if require_canonical_layout and extra_offset != aligned_oword_end:
                raise MotionParseError(f"{c.label}: MotClip extra-property block violates canonical layout")
            c.require_zero(oword_end, extra_offset, "MotClip OWord alignment")
            extra_ranges, extra_end = self._read_extra_ranges(c, extra_offset, nodes, pointer_base)
            physical_end = align_up(extra_end, 16)
            c.require_zero(extra_end, physical_end, "compact CLIP owner alignment")
        if following_data_offset is not None:
            if following_data_offset != physical_end:
                raise MotionParseError(
                    f"{c.label}: {following_data_name} does not follow compact CLIP at Align16"
                )

        if not nodes:
            raise MotionParseError(f"{c.label}: compact MotClip requires a root node")
        if header_guid != nodes[0].node.root_guid:
            raise MotionParseError(f"{c.label}: v27 header GUID does not match the root node GUID")
        clip = CompactMotClip(total_frame=total_frame, root=nodes[0].node, extra_ranges=extra_ranges)
        if require_canonical_layout:
            self._validate_string_regions(
                c,
                clip,
                ascii_offset,
                unicode_offset,
                oword_offset,
                ascii_value_interpolations,
            )
        section_names = (
            "nodes",
            "properties",
            "keys",
            "speed_points",
            "hermite_curves",
            "bezier3d_curves",
            "legacy_unused",
            "last_keys",
            "ascii_strings",
            "unicode_strings",
            "owords",
            "extra_ranges",
        )
        relative_sections = dict(zip(section_names, fields))
        absolute_sections = {
            name: pointer_base + stored if stored else 0
            for name, stored in relative_sections.items()
        }
        return CompactClipV27ParseResult(
            clip=clip,
            clip_offset=clip_offset,
            pointer_base=pointer_base,
            header_guid=header_guid,
            section_relative_offsets=relative_sections,
            section_absolute_offsets=absolute_sections,
            nodes=nodes,
            properties=props,
            keys=keys,
            speed_points=speeds,
            last_keys=last_keys,
            hermite_curves=hermite_curves,
            bezier3d_curves=bezier_curves,
            physical_end=physical_end,
        )

    def _read_nodes(
        self,
        c: ReadContext,
        offset: int,
        count: int,
        pointer_base: int,
        ascii_offset: int,
        unicode_offset: int,
    ) -> list[CompactClipNodeRecord]:
        stride = self.profile.mot_clip.node_size
        c.require(offset, count * stride, "MotClip node table")
        result: list[CompactClipNodeRecord] = []
        for index in range(count):
            record = offset + index * stride
            child_count = c.u32(record, "MotClip node child count")
            property_count = c.u32(record + 4, "MotClip node property count")
            start = c.f32(record + 8, "MotClip node start frame")
            end = c.f32(record + 0xC, "MotClip node end frame")
            root_guid = c.bytes(record + 0x10, 16, "MotClip node root GUID")
            extra_guid = c.bytes(record + 0x20, 16, "MotClip node extra GUID")
            c.require_zero(record + 0x30, record + 0x40, "MotClip node reserved bytes")
            ascii_name = c.u64(record + 0x40, "MotClip node ASCII name")
            unicode_name = c.u64(record + 0x48, "MotClip node UTF-16 name")
            child_index = c.u64(record + 0x50, "MotClip node child index")
            property_index = c.u64(record + 0x58, "MotClip node property index")
            name, _ = c.ascii_z(ascii_offset + ascii_name, "MotClip node name")
            wide_name, _ = c.utf16_z(unicode_offset + unicode_name * 2, "MotClip node wide name")
            if name != wide_name or ascii_name != unicode_name:
                raise MotionParseError(f"{c.label}: v27 node string copies differ")
            result.append(
                CompactClipNodeRecord(
                    offset=record,
                    index=index,
                    node=ClipNode(name, start, end, root_guid, extra_guid),
                    child_index=child_index,
                    child_count=child_count,
                    property_index=property_index,
                    property_count=property_count,
                    ascii_name_offset=ascii_name,
                    unicode_name_index=unicode_name,
                )
            )
        return result

    def _read_properties(
        self,
        c: ReadContext,
        offset: int,
        count: int,
        pointer_base: int,
        ascii_offset: int,
        unicode_offset: int,
    ) -> list[CompactClipPropertyRecord]:
        stride = self.profile.mot_clip.property_size
        c.require(offset, count * stride, "MotClip property table")
        result: list[CompactClipPropertyRecord] = []
        for index in range(count):
            record = offset + index * stride
            array_index = c.i32(record, "MotClip property array index")
            start = c.f32(record + 4, "MotClip property start frame")
            end = c.f32(record + 8, "MotClip property end frame")
            if c.u32(record + 0xC, "MotClip property constant") != 2:
                raise MotionParseError(f"{c.label}: v27 property constant is not two")
            speed_count = c.u32(record + 0x10, "MotClip property speed count")
            try:
                property_type = ClipPropertyType(c.u8(record + 0x14, "MotClip property type"))
            except ValueError as exc:
                raise MotionParseError(f"{c.label}: unknown MotClip property type") from exc
            flags = c.u8(record + 0x15, "MotClip property flags")
            if c.u16(record + 0x16, "MotClip property reserved"):
                raise MotionParseError(f"{c.label}: nonzero MotClip property reserved bytes")
            if c.u32(record + 0x18) or c.u32(record + 0x1C):
                raise MotionParseError(f"{c.label}: v27 property hash slots must be zero")
            name_index = c.u64(record + 0x20, "MotClip property name")
            wide_index = c.u64(record + 0x28, "MotClip property wide name")
            c.require_zero(record + 0x30, record + 0x40, "MotClip property legacy fields")
            if c.u64(record + 0x40, "MotClip property data offset"):
                raise MotionParseError(f"{c.label}: v27 property data offset is unsupported")
            member_index = c.u64(record + 0x48, "MotClip property member index")
            member_count = c.u64(record + 0x50, "MotClip property member count")
            last_key_index = c.u64(record + 0x58, "MotClip property last key index")
            speed_index = c.u64(record + 0x60, "MotClip property speed index")
            if c.u64(record + 0x68, "MotClip property clip property index"):
                raise MotionParseError(f"{c.label}: v27 clip-property links are unsupported")
            name, _ = c.ascii_z(ascii_offset + name_index, "MotClip property name")
            wide_name, _ = c.utf16_z(unicode_offset + wide_index * 2, "MotClip property wide name")
            if name != wide_name or name_index != wide_index:
                raise MotionParseError(f"{c.label}: v27 property string copies differ")
            prop = ClipProperty(
                name=name,
                property_type=property_type,
                start_frame=start,
                end_frame=end,
                array_index=array_index,
                enum_closed=bool(flags & 1),
                set_after_end_frame=bool(flags & 2),
                restoration=bool(flags & 8),
                set_delegate_enable=bool(flags & 0x10),
                prev_diff_frame_set=bool(flags & 0x20),
                next_diff_frame_set=bool(flags & 0x40),
                prev_key_value_set=bool(flags & 0x80),
            )
            result.append(
                CompactClipPropertyRecord(
                    offset=record,
                    index=index,
                    prop=prop,
                    member_index=member_index,
                    member_count=member_count,
                    has_last_key=bool(flags & 4),
                    last_key_index=last_key_index,
                    speed_index=speed_index,
                    speed_count=speed_count,
                    ascii_name_offset=name_index,
                    unicode_name_index=wide_index,
                    flags=flags,
                )
            )
        return result

    def _read_keys(self, c: ReadContext, offset: int, count: int) -> list[CompactClipKeyRecord]:
        stride = self.profile.mot_clip.key_size
        c.require(offset, count * stride, "MotClip key table")
        result: list[CompactClipKeyRecord] = []
        for index in range(count):
            record = offset + index * stride
            packed = c.u32(record + 8, "MotClip key flags")
            if packed >> 9 or c.u32(record + 0xC) or c.u64(record + 0x20):
                raise MotionParseError(f"{c.label}: nonzero v27 key reserved fields")
            raw_interpolation = packed & 0xFF
            try:
                interpolation = ClipInterpolation(raw_interpolation)
            except ValueError as exc:
                raise MotionParseError(
                    f"{c.label}: unknown MotClip interpolation {raw_interpolation}"
                ) from exc
            result.append(
                CompactClipKeyRecord(
                    offset=record,
                    index=index,
                    key=ClipKey(
                        frame=c.f32(record, "MotClip key frame"),
                        rate=c.f32(record + 4, "MotClip key rate"),
                        interpolation=interpolation,
                        offset_frame=bool((packed >> 8) & 1),
                    ),
                    payload=c.u64(record + 0x10, "MotClip key payload"),
                    curve_index=c.u64(record + 0x18, "MotClip key curve index"),
                    packed_flags=packed,
                )
            )
        return result

    def _read_speed_points(self, c: ReadContext, offset: int, count: int) -> list[CompactClipSpeedRecord]:
        c.require(offset, count * 0x18, "MotClip speed-point table")
        result: list[CompactClipSpeedRecord] = []
        for index in range(count):
            record = offset + index * 0x18
            raw_interpolation = c.u32(record + 8, "MotClip speed interpolation")
            try:
                interpolation = ClipInterpolation(raw_interpolation)
            except ValueError as exc:
                raise MotionParseError(
                    f"{c.label}: unknown MotClip interpolation {raw_interpolation}"
                ) from exc
            if c.u32(record + 0xC):
                raise MotionParseError(f"{c.label}: invalid MotClip speed point")
            result.append(
                CompactClipSpeedRecord(
                    offset=record,
                    index=index,
                    point=SpeedPoint(c.f32(record), c.f32(record + 4), interpolation),
                    curve_index=c.u64(record + 0x10),
                )
            )
        return result

    def _attach_graph(
        self,
        c: ReadContext,
        nodes: list[CompactClipNodeRecord],
        props: list[CompactClipPropertyRecord],
        keys: list[CompactClipKeyRecord],
        last_keys: list[CompactClipKeyRecord],
        speeds: list[CompactClipSpeedRecord],
    ) -> None:
        node_owners = [0] * len(nodes)
        prop_owners = [0] * len(props)
        key_owners = [0] * len(keys)
        last_owners = [0] * len(last_keys)
        speed_owners = [0] * len(speeds)
        for record in nodes:
            child_start = record.child_index
            child_stop = child_start + record.child_count
            if child_stop > len(nodes):
                raise MotionParseError(f"{c.label}: MotClip node child range is invalid")
            record.node.children = [
                item.node for item in nodes[child_start:child_stop]
            ]
            for child_index in range(child_start, child_stop):
                node_owners[child_index] += 1

            property_start = record.property_index
            property_stop = property_start + record.property_count
            if property_stop > len(props):
                raise MotionParseError(f"{c.label}: MotClip node property range is invalid")
            record.node.properties = [
                item.prop for item in props[property_start:property_stop]
            ]
            for prop_index in range(property_start, property_stop):
                prop_owners[prop_index] += 1
        if nodes and (node_owners[0] or any(value != 1 for value in node_owners[1:])):
            raise MotionParseError(f"{c.label}: MotClip nodes do not form one rooted graph")

        for record in props:
            prop = record.prop
            speed_start = record.speed_index
            speed_stop = speed_start + record.speed_count
            if speed_stop > len(speeds):
                raise MotionParseError(f"{c.label}: MotClip speed-point range is invalid")
            prop.speed_points = [
                item.point for item in speeds[speed_start:speed_stop]
            ]
            for speed_index in range(speed_start, speed_stop):
                speed_owners[speed_index] += 1

            member_start = record.member_index
            member_stop = member_start + record.member_count
            if prop.property_type in CONTAINER_PROPERTY_TYPES:
                if member_stop > len(props):
                    raise MotionParseError(f"{c.label}: MotClip property child range is invalid")
                prop.children = [
                    item.prop for item in props[member_start:member_stop]
                ]
                for child_index in range(member_start, member_stop):
                    prop_owners[child_index] += 1
            else:
                if member_stop > len(keys):
                    raise MotionParseError(f"{c.label}: MotClip property key range is invalid")
                prop.keys = [
                    item.key for item in keys[member_start:member_stop]
                ]
                for key_index in range(member_start, member_stop):
                    key_owners[key_index] += 1
            if record.has_last_key:
                if record.last_key_index >= len(last_keys):
                    raise MotionParseError(f"{c.label}: MotClip last-key index is invalid")
                record.prop.last_key = last_keys[record.last_key_index].key
                last_owners[record.last_key_index] += 1
            elif record.last_key_index:
                raise MotionParseError(f"{c.label}: unused MotClip last-key index is nonzero")
        if any(value != 1 for value in prop_owners):
            raise MotionParseError(f"{c.label}: MotClip properties do not have exactly one owner")
        if any(value != 1 for value in key_owners):
            raise MotionParseError(f"{c.label}: MotClip keys do not have exactly one owner")
        if any(value != 1 for value in last_owners):
            raise MotionParseError(f"{c.label}: MotClip last keys do not have exactly one owner")
        if any(value != 1 for value in speed_owners):
            raise MotionParseError(f"{c.label}: MotClip speed points do not have exactly one owner")

    @staticmethod
    def _attach_curves(
        c: ReadContext,
        keys: list[CompactClipKeyRecord],
        last_keys: list[CompactClipKeyRecord],
        speeds: list[CompactClipSpeedRecord],
        hermite: list[HermiteCurve],
        bezier: list[Bezier3DCurve],
    ) -> None:
        for record in [*keys, *last_keys]:
            if record.key.interpolation in (
                ClipInterpolation.HERMITE,
                ClipInterpolation.BEZIER,
            ):
                if record.curve_index >= len(hermite):
                    raise MotionParseError(
                        f"{c.label}: compact CLIP key Hermite index is out of range"
                    )
                record.key.curve = hermite[record.curve_index]
            elif record.key.interpolation == ClipInterpolation.BEZIER_3D:
                if record.curve_index >= len(bezier):
                    raise MotionParseError(
                        f"{c.label}: compact CLIP key Bezier3D index is out of range"
                    )
                record.key.curve = bezier[record.curve_index]
            elif record.curve_index:
                raise MotionParseError(f"{c.label}: non-curve compact CLIP key has a curve index")
        for record in speeds:
            point = record.point
            if point.interpolation in (
                ClipInterpolation.HERMITE,
                ClipInterpolation.BEZIER,
            ):
                if record.curve_index >= len(hermite):
                    raise MotionParseError(
                        f"{c.label}: compact CLIP speed-point Hermite index is out of range"
                    )
                point.curve = hermite[record.curve_index]
            elif point.interpolation == ClipInterpolation.BEZIER_3D:
                if record.curve_index >= len(bezier):
                    raise MotionParseError(
                        f"{c.label}: compact CLIP speed-point Bezier3D index is out of range"
                    )
                point.curve = bezier[record.curve_index]
            elif record.curve_index:
                raise MotionParseError(
                    f"{c.label}: non-curve compact CLIP speed point has a curve index"
                )

    def _decode_values(
        self,
        c: ReadContext,
        props: list[CompactClipPropertyRecord],
        key_records: list[CompactClipKeyRecord],
        ascii_offset: int,
        unicode_offset: int,
        oword_offset: int,
        ascii_value_interpolations: frozenset[ClipInterpolation],
    ) -> int:
        oword_indices: list[int] = []
        key_records_by_id = {id(record.key): record for record in key_records}
        for record in props:
            prop = record.prop
            if prop.property_type in CONTAINER_PROPERTY_TYPES:
                continue
            for key in [*prop.keys, *([prop.last_key] if prop.last_key is not None else [])]:
                raw = key_records_by_id[id(key)].payload
                if key.interpolation in ascii_value_interpolations:
                    key.value = c.ascii_z(
                        ascii_offset + raw,
                        "compact CLIP owner-specific ASCII key value",
                    )[0]
                else:
                    key.value = self._decode_value(
                        c,
                        prop.property_type,
                        raw,
                        ascii_offset,
                        unicode_offset,
                        oword_offset,
                    )
                if (
                    key.interpolation not in ascii_value_interpolations
                    and prop.property_type == ClipPropertyType.PATH_POINT3D
                ):
                    oword_indices.append(raw & 0xFFFFFFFF)
        return max(oword_indices, default=-1) + 1

    @staticmethod
    def _decode_value(
        c: ReadContext,
        property_type: ClipPropertyType,
        raw: int,
        ascii_offset: int,
        unicode_offset: int,
        oword_offset: int,
    ):
        signed = {
            ClipPropertyType.S8: ("<b", 1),
            ClipPropertyType.S16: ("<h", 2),
            ClipPropertyType.S32: ("<i", 4),
            ClipPropertyType.S64: ("<q", 8),
        }
        unsigned = {
            ClipPropertyType.BOOL: ("<B", 1),
            ClipPropertyType.U8: ("<B", 1),
            ClipPropertyType.U16: ("<H", 2),
            ClipPropertyType.U32: ("<I", 4),
            ClipPropertyType.U64: ("<Q", 8),
        }
        raw_bytes = struct.pack("<Q", raw)
        if property_type in signed:
            fmt, size = signed[property_type]
            return struct.unpack(fmt, raw_bytes[:size])[0]
        if property_type in unsigned:
            fmt, size = unsigned[property_type]
            value = struct.unpack(fmt, raw_bytes[:size])[0]
            return bool(value) if property_type == ClipPropertyType.BOOL else value
        if property_type in (ClipPropertyType.F32, ClipPropertyType.F64):
            return struct.unpack("<d", raw_bytes)[0]
        if property_type in ASCII_VALUE_PROPERTY_TYPES:
            return c.ascii_z(ascii_offset + raw, "MotClip key ASCII value")[0]
        if property_type in UTF16_VALUE_PROPERTY_TYPES:
            return c.utf16_z(unicode_offset + raw * 2, "MotClip key UTF-16 value")[0]
        if property_type == ClipPropertyType.ACTION:
            if raw != 1:
                raise MotionParseError(f"{c.label}: MotClip Action payload must equal one")
            return None
        if property_type == ClipPropertyType.PATH_POINT3D:
            if raw >> 32:
                raise MotionParseError(f"{c.label}: MotClip OWord index upper dword is nonzero")
            record = oword_offset + raw * 0x10
            value = tuple(c.f32(record + part * 4, "MotClip OWord") for part in range(3))
            if c.u32(record + 0xC, "MotClip OWord padding"):
                raise MotionParseError(f"{c.label}: MotClip PathPoint3D padding is nonzero")
            return value
        if property_type == ClipPropertyType.UNKNOWN and raw == 0:
            return None
        raise MotionParseError(f"{c.label}: keyed MotClip property type {property_type.name} is unsupported")

    def _read_extra_ranges(
        self,
        c: ReadContext,
        offset: int,
        nodes: list[CompactClipNodeRecord],
        pointer_base: int,
    ) -> tuple[list[ClipExtraRange], int]:
        c.require(offset, 0x10, "MotClip extra-property header")
        count = c.u32(offset, "MotClip extra range count")
        if c.u32(offset + 4, "MotClip legacy second extra count"):
            raise MotionParseError(f"{c.label}: v27 second extra-property list must be empty")
        table_offset = c.u64(offset + 8, "MotClip extra range table")
        table_abs = pointer_base + table_offset
        if table_abs != offset + 0x10:
            raise MotionParseError(f"{c.label}: MotClip extra range table violates canonical layout")
        c.require(table_abs, count * 0x10, "MotClip extra range records")
        cursor = table_abs + count * 0x10
        result: list[ClipExtraRange] = []
        previous_track = -1
        for index in range(count):
            record = table_abs + index * 0x10
            name_hash = c.u32(record, "MotClip extra range hash")
            track_index = c.i16(record + 4, "MotClip extra range track")
            value_count = c.i16(record + 6, "MotClip extra range count")
            value_offset = c.u64(record + 8, "MotClip extra range values")
            value_abs = pointer_base + value_offset
            if value_count < 0 or value_abs != cursor:
                raise MotionParseError(f"{c.label}: MotClip extra range values violate canonical layout")
            if track_index < 0 or track_index + 1 >= len(nodes) or track_index < previous_track:
                raise MotionParseError(f"{c.label}: MotClip extra range owner is invalid")
            owner = nodes[track_index + 1].node
            if name_hash != murmur3_hash(owner.name.encode("utf-16le")):
                raise MotionParseError(f"{c.label}: MotClip extra range hash does not match its owner")
            intervals: list[ClipInterval] = []
            for value_index in range(value_count):
                value = value_abs + value_index * 8
                begin_bits = c.u32(value, "MotClip extra begin")
                begin = None if begin_bits == 0xFFFFFFFF else c.f32(value, "MotClip extra begin")
                intervals.append(ClipInterval(begin, c.u32(value + 4, "MotClip extra span")))
            result.append(ClipExtraRange(owner, intervals))
            cursor += value_count * 8
            previous_track = track_index
        return result, cursor

    @staticmethod
    def _validate_string_regions(
        c: ReadContext,
        clip: CompactMotClip,
        ascii_offset: int,
        unicode_offset: int,
        oword_offset: int,
        ascii_value_interpolations: frozenset[ClipInterpolation],
    ) -> None:
        # Reconstruct the producer's deliberately uninterned v27 string pools.
        nodes = [clip.root, *clip.root.children]
        props: list[ClipProperty] = []

        def add_props(items: list[ClipProperty]) -> None:
            props.extend(items)
            for prop in items:
                add_props(prop.children)

        for node in nodes:
            add_props(node.properties)
        strings = [node.name for node in nodes] + [prop.name for prop in props]
        for prop in props:
            strings.extend(
                str(key.value)
                for key in prop.keys
                if prop.property_type in ASCII_VALUE_PROPERTY_TYPES
                or key.interpolation in ascii_value_interpolations
            )
        for prop in props:
            if (
                prop.last_key
                and (
                    prop.property_type in ASCII_VALUE_PROPERTY_TYPES
                    or prop.last_key.interpolation in ascii_value_interpolations
                )
            ):
                strings.append(str(prop.last_key.value))
        ascii_payload = b"".join(value.encode("ascii") + b"\0" for value in strings)
        expected_unicode = align_up(ascii_offset + len(ascii_payload), 8)
        if c.bytes(
            ascii_offset,
            expected_unicode - ascii_offset,
        ) != ascii_payload + bytes(
            expected_unicode - ascii_offset - len(ascii_payload)
        ):
            raise MotionParseError(f"{c.label}: MotClip ASCII pool is not canonical")
        wide_strings = [node.name for node in nodes] + [prop.name for prop in props]
        for prop in props:
            if prop.property_type in UTF16_VALUE_PROPERTY_TYPES:
                wide_strings.extend(
                    str(key.value)
                    for key in prop.keys
                    if key.interpolation not in ascii_value_interpolations
                )
        for prop in props:
            if (
                prop.property_type in UTF16_VALUE_PROPERTY_TYPES
                and prop.last_key
                and prop.last_key.interpolation not in ascii_value_interpolations
            ):
                wide_strings.append(str(prop.last_key.value))
        wide_payload = b"".join(value.encode("utf-16le") + b"\0\0" for value in wide_strings)
        expected_oword = align_up(unicode_offset + len(wide_payload), 8)
        if unicode_offset != expected_unicode:
            raise MotionParseError(f"{c.label}: MotClip Unicode pool is misaligned")
        if oword_offset != expected_oword:
            raise MotionParseError(f"{c.label}: MotClip OWord table is misplaced")
        if c.bytes(
            unicode_offset,
            oword_offset - unicode_offset,
        ) != wide_payload + bytes(
            oword_offset - unicode_offset - len(wide_payload)
        ):
            raise MotionParseError(f"{c.label}: MotClip UTF-16 pool is not canonical")
