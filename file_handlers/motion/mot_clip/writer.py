from __future__ import annotations

import struct

from utils.hash_util import murmur3_hash

from ..binary import pad_to_alignment
from ..errors import MotionWriteError
from ..profiles import MotionFormatProfile
from .model import (
    ASCII_VALUE_PROPERTY_TYPES,
    CONTAINER_PROPERTY_TYPES,
    UTF16_VALUE_PROPERTY_TYPES,
    Bezier3DCurve,
    ClipInterpolation,
    ClipKey,
    ClipProperty,
    ClipPropertyType,
    CompactMotClip,
    HermiteCurve,
)
from .validator import CompactMotClipV27Validator


_SIGNED_VALUE_FORMATS = {
    ClipPropertyType.S8: "<b",
    ClipPropertyType.S16: "<h",
    ClipPropertyType.S32: "<i",
    ClipPropertyType.S64: "<q",
}
_UNSIGNED_VALUE_FORMATS = {
    ClipPropertyType.U8: "<B",
    ClipPropertyType.U16: "<H",
    ClipPropertyType.U32: "<I",
    ClipPropertyType.U64: "<Q",
}


class CompactMotClipV27Writer:
    """Deterministic v27 producer. All stored pointers use ``pointer_base``."""

    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot_clip=27)
        self.profile = profile
        self.validator = CompactMotClipV27Validator(profile)

    def build(
        self,
        clip: CompactMotClip,
        *,
        origin_offset: int,
        include_extra_ranges: bool = True,
        ascii_value_interpolations: frozenset[ClipInterpolation] = frozenset(),
    ) -> bytes:
        self.validator.validate(
            clip,
            ascii_value_interpolations=ascii_value_interpolations,
        )
        if not include_extra_ranges and clip.extra_ranges:
            raise MotionWriteError(
                "this compact CLIP owner cannot serialize extra ranges"
            )
        layout = self.profile.mot_clip
        nodes = [clip.root, *clip.root.children]
        props = self.validator.flatten_properties(nodes)
        node_index = {id(node): index for index, node in enumerate(nodes)}
        prop_index = {id(prop): index for index, prop in enumerate(props)}

        key_entries: list[tuple[ClipProperty, ClipKey]] = []
        last_entries: list[tuple[ClipProperty, ClipKey]] = []
        speed_entries = []
        for prop in props:
            if prop.property_type not in CONTAINER_PROPERTY_TYPES:
                key_entries.extend((prop, key) for key in prop.keys)
                if prop.last_key is not None:
                    last_entries.append((prop, prop.last_key))
                speed_entries.extend((prop, point) for point in prop.speed_points)
        key_index = {id(key): index for index, (_prop, key) in enumerate(key_entries)}
        last_index = {id(key): index for index, (_prop, key) in enumerate(last_entries)}
        speed_index = {id(point): index for index, (_prop, point) in enumerate(speed_entries)}

        all_curve_objects = [
            item.curve
            for _prop, item in [*key_entries, *last_entries, *speed_entries]
            if item.curve is not None
        ]
        hermite = [curve for curve in all_curve_objects if isinstance(curve, HermiteCurve)]
        bezier = [curve for curve in all_curve_objects if isinstance(curve, Bezier3DCurve)]
        hermite_index = {id(curve): index for index, curve in enumerate(hermite)}
        bezier_index = {id(curve): index for index, curve in enumerate(bezier)}

        # v27 pools are occurrence-ordered and deliberately not interned.
        ascii_values: list[str] = [node.name for node in nodes] + [prop.name for prop in props]
        unicode_values: list[str] = [node.name for node in nodes] + [prop.name for prop in props]
        ascii_key_offsets: dict[int, int] = {}
        unicode_key_offsets: dict[int, int] = {}
        ascii_cursor = sum(len(value.encode("ascii")) + 1 for value in ascii_values)
        unicode_cursor = sum(len(value.encode("utf-16le")) + 2 for value in unicode_values) // 2
        for prop, key in [*key_entries, *last_entries]:
            if (
                key.interpolation in ascii_value_interpolations
                or prop.property_type in ASCII_VALUE_PROPERTY_TYPES
            ):
                value = str(key.value)
                ascii_key_offsets[id(key)] = ascii_cursor
                ascii_cursor += len(value.encode("ascii")) + 1
                ascii_values.append(value)
            elif prop.property_type in UTF16_VALUE_PROPERTY_TYPES:
                value = str(key.value)
                unicode_key_offsets[id(key)] = unicode_cursor
                unicode_cursor += (len(value.encode("utf-16le")) + 2) // 2
                unicode_values.append(value)
        ascii_offsets = self._occurrence_offsets(ascii_values, "ascii")
        unicode_offsets = [value // 2 for value in self._occurrence_offsets(unicode_values, "utf-16le")]
        ascii_payload = b"".join(value.encode("ascii") + b"\0" for value in ascii_values)
        unicode_payload = b"".join(value.encode("utf-16le") + b"\0\0" for value in unicode_values)

        out = bytearray(layout.header_size)

        def stored(local: int) -> int:
            return origin_offset + local

        table_offsets: dict[str, int] = {}
        table_offsets["nodes"] = len(out)
        for index, node in enumerate(nodes):
            record = bytearray(layout.node_size)
            children = node.children
            struct.pack_into("<IIff", record, 0, len(children), len(node.properties), node.start_frame, node.end_frame)
            record[0x10:0x20] = node.root_guid
            record[0x20:0x30] = node.extra_guid
            struct.pack_into("<Q", record, 0x40, ascii_offsets[index])
            struct.pack_into("<Q", record, 0x48, unicode_offsets[index])
            struct.pack_into("<Q", record, 0x50, node_index[id(children[0])] if children else 0)
            struct.pack_into("<Q", record, 0x58, prop_index[id(node.properties[0])] if node.properties else 0)
            out.extend(record)
        pad_to_alignment(out, 8)

        table_offsets["properties"] = len(out)
        property_ascii_start = len(nodes)
        property_unicode_start = len(nodes)
        for index, prop in enumerate(props):
            record = bytearray(layout.property_size)
            flags = (
                int(prop.enum_closed)
                | (int(prop.set_after_end_frame) << 1)
                | (int(prop.last_key is not None) << 2)
                | (int(prop.restoration) << 3)
                | (int(prop.set_delegate_enable) << 4)
                | (int(prop.prev_diff_frame_set) << 5)
                | (int(prop.next_diff_frame_set) << 6)
                | (int(prop.prev_key_value_set) << 7)
            )
            struct.pack_into(
                "<iffII",
                record,
                0,
                prop.array_index,
                prop.start_frame,
                prop.end_frame,
                2,
                len(prop.speed_points),
            )
            struct.pack_into("<BB", record, 0x14, int(prop.property_type), flags)
            struct.pack_into("<Q", record, 0x20, ascii_offsets[property_ascii_start + index])
            struct.pack_into("<Q", record, 0x28, unicode_offsets[property_unicode_start + index])
            if prop.property_type in CONTAINER_PROPERTY_TYPES:
                member_start = prop_index[id(prop.children[0])] if prop.children else 0
                member_count = len(prop.children)
            else:
                member_start = key_index[id(prop.keys[0])] if prop.keys else 0
                member_count = len(prop.keys)
            struct.pack_into(
                "<QQQ",
                record,
                0x48,
                member_start,
                member_count,
                last_index[id(prop.last_key)] if prop.last_key else 0,
            )
            struct.pack_into("<Q", record, 0x60, speed_index[id(prop.speed_points[0])] if prop.speed_points else 0)
            out.extend(record)

        table_offsets["keys"] = len(out)
        key_payload_patches: list[tuple[int, ClipProperty, ClipKey]] = []
        for prop, key in key_entries:
            key_payload_patches.append((len(out) + 0x10, prop, key))
            out.extend(self._key_record(key, hermite_index, bezier_index))

        table_offsets["speed"] = len(out)
        for _prop, point in speed_entries:
            curve_index = self._curve_index(point, hermite_index, bezier_index)
            out.extend(struct.pack("<ffIIQ", point.frame, point.rate, point.interpolation, 0, curve_index))

        table_offsets["hermite"] = len(out)
        for curve in hermite:
            out.extend(struct.pack("<4f", *curve.values))
        table_offsets["bezier"] = len(out)
        for curve in bezier:
            out.extend(struct.pack("<8f", *curve.values))
        table_offsets["legacy_unused"] = len(out)
        table_offsets["last"] = len(out)
        for prop, key in last_entries:
            key_payload_patches.append((len(out) + 0x10, prop, key))
            out.extend(self._key_record(key, hermite_index, bezier_index))

        table_offsets["ascii"] = len(out)
        out.extend(ascii_payload)
        pad_to_alignment(out, 8)
        table_offsets["unicode"] = len(out)
        out.extend(unicode_payload)
        pad_to_alignment(out, 8)
        table_offsets["oword"] = len(out)
        oword_index: dict[int, int] = {}
        for prop, key in [*key_entries, *last_entries]:
            if prop.property_type == ClipPropertyType.PATH_POINT3D:
                oword_index[id(key)] = (len(out) - table_offsets["oword"]) // 0x10
                out.extend(struct.pack("<3fI", *key.value, 0))
        if include_extra_ranges:
            pad_to_alignment(out, 16)
            table_offsets["extra"] = len(out)
            extra_header = len(out)
            out.extend(bytes(0x10 + len(clip.extra_ranges) * 0x10))
            struct.pack_into("<IIQ", out, extra_header, len(clip.extra_ranges), 0, stored(extra_header + 0x10))
            values_start = extra_header + 0x10 + len(clip.extra_ranges) * 0x10
            for index, extra in enumerate(clip.extra_ranges):
                record = extra_header + 0x10 + index * 0x10
                owner_index = node_index[id(extra.owner)] - 1
                struct.pack_into(
                    "<IhhQ",
                    out,
                    record,
                    murmur3_hash(extra.owner.name.encode("utf-16le")),
                    owner_index,
                    len(extra.intervals),
                    stored(values_start),
                )
                values_start += len(extra.intervals) * 8
            for extra in clip.extra_ranges:
                for interval in extra.intervals:
                    begin_bits = (
                        0xFFFFFFFF
                        if interval.begin_frame is None
                        else struct.unpack(
                            "<I",
                            struct.pack("<f", interval.begin_frame),
                        )[0]
                    )
                    out.extend(struct.pack("<II", begin_bits, interval.frame_span))
        else:
            table_offsets["extra"] = 0

        for position, prop, key in key_payload_patches:
            payload = self._encode_value(
                prop.property_type,
                key,
                ascii_key_offsets,
                unicode_key_offsets,
                oword_index,
                ascii_value_interpolations,
            )
            struct.pack_into("<Q", out, position, payload)

        struct.pack_into(
            "<IIfIII16s",
            out,
            0,
            0x50494C43,
            layout.version,
            clip.total_frame,
            len(nodes),
            len(props),
            len(key_entries),
            clip.root.root_guid,
        )
        for index, name in enumerate(
            (
                "nodes",
                "properties",
                "keys",
                "speed",
                "hermite",
                "bezier",
                "legacy_unused",
                "last",
                "ascii",
                "unicode",
                "oword",
                "extra",
            )
        ):
            local = table_offsets[name]
            struct.pack_into("<Q", out, 0x28 + index * 8, stored(local) if local else 0)
        return bytes(out)

    @staticmethod
    def _key_record(key, hermite_index, bezier_index) -> bytes:
        packed = key.interpolation | (int(key.offset_frame) << 8)
        curve_index = CompactMotClipV27Writer._curve_index(
            key,
            hermite_index,
            bezier_index,
        )
        return struct.pack("<ffIIQQQ", key.frame, key.rate, packed, 0, 0, curve_index, 0)

    @staticmethod
    def _curve_index(item, hermite_index, bezier_index) -> int:
        if item.interpolation in (
            ClipInterpolation.HERMITE,
            ClipInterpolation.BEZIER,
        ):
            return hermite_index[id(item.curve)]
        if item.interpolation == ClipInterpolation.BEZIER_3D:
            return bezier_index[id(item.curve)]
        return 0

    @staticmethod
    def _encode_value(
        property_type,
        key,
        ascii_offsets,
        unicode_offsets,
        oword_offsets,
        ascii_value_interpolations,
    ) -> int:
        value = key.value
        if key.interpolation in ascii_value_interpolations:
            return ascii_offsets[id(key)]
        if property_type == ClipPropertyType.BOOL:
            return int(value)
        if property_type in _SIGNED_VALUE_FORMATS:
            return int.from_bytes(
                struct.pack(_SIGNED_VALUE_FORMATS[property_type], int(value)).ljust(8, b"\0"),
                "little",
            )
        if property_type in _UNSIGNED_VALUE_FORMATS:
            return int.from_bytes(
                struct.pack(_UNSIGNED_VALUE_FORMATS[property_type], int(value)).ljust(8, b"\0"),
                "little",
            )
        if property_type in (ClipPropertyType.F32, ClipPropertyType.F64):
            return struct.unpack("<Q", struct.pack("<d", float(value)))[0]
        if property_type in ASCII_VALUE_PROPERTY_TYPES:
            return ascii_offsets[id(key)]
        if property_type in UTF16_VALUE_PROPERTY_TYPES:
            return unicode_offsets[id(key)]
        if property_type == ClipPropertyType.ACTION:
            return 1
        if property_type == ClipPropertyType.PATH_POINT3D:
            return oword_offsets[id(key)]
        if property_type == ClipPropertyType.UNKNOWN:
            return 0
        raise MotionWriteError(f"cannot encode keyed compact property type {property_type.name}")

    @staticmethod
    def _occurrence_offsets(values: list[str], encoding: str) -> list[int]:
        result = []
        cursor = 0
        terminator = 2 if encoding == "utf-16le" else 1
        for value in values:
            result.append(cursor)
            cursor += len(value.encode(encoding)) + terminator
        return result
