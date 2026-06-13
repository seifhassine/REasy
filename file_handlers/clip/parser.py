from __future__ import annotations

from dataclasses import dataclass, field

from .enums import CLIP_MAGIC, PROPERTY_TYPES_WITH_CHILDREN, PropertyType, property_type_or_unknown
from .reader import ClipParserError, Reader
from .structures import (
    ActionKey,
    BoolKey,
    ClipHeader,
    ClipInfo,
    Key,
    Node,
    NoHermiteKey,
    Property,
    SpeedPoint,
    Track,
    UserDataAssetInfo,
)


@dataclass(slots=True)
class ParsedClip:
    header: ClipHeader
    root_node_offsets: list[int]
    root_nodes: list[Node]
    nodes_reorder_offsets: list[int]
    nodes_reorder_nodes: list[Node]
    track_child_offsets: list[int]
    tracks: list[Track]
    clip_infos: list[ClipInfo]
    nodes: list[Node]
    properties: list[Property]
    main_keys: list[Key]
    last_keys: list[Key]
    bool_keys: list[BoolKey]
    action_keys: list[ActionKey]
    no_hermite_keys: list[NoHermiteKey]
    speed_points: list[SpeedPoint]
    hermite_nodes: list[tuple[float, float, float, float]]
    bezier3d_nodes: list[tuple[float, float, float, float, float, float, float, float]]
    user_data_assets: list[UserDataAssetInfo]
    owords: list[tuple[float, float, float, float]]
    _deleted_node_ids: set[int] = field(default_factory=set)
    _deleted_property_ids: set[int] = field(default_factory=set)


def node_size(version: int) -> int:
    if version >= 53:
        return 80
    if version == 34:
        return 88
    return 96


def prop_size(version: int) -> int:
    if version >= 53:
        return 56
    if version in (40, 43):
        return 72
    return 112


def key_size(version: int) -> int:
    return 32 if version >= 34 else 40


_HEADER_FIELD_SIZES = {
    "f32": 4,
    "u32": 4,
    "u64": 8,
    "bytes16": 16,
}
_HEADER_LAYOUT = (
    ("f32", "total_frame", 0, None),
    ("u32", "root_node_num", 0, None),
    ("u32", "track_num", 0, None),
    ("u32", "clip_info_num", 40, None),
    ("u32", "track_child_num", 0, None),
    ("u32", "node_count_pragmata", 85, None),
    ("u32", "node_num", 0, None),
    ("u32", "property_num", 0, None),
    ("u32", "key_num", 0, None),
    ("u32", None, 0, 39),
    ("bytes16", "legacy_guid", 0, 27),
    ("u32", "bool_key_num", 85, None),
    ("u32", "action_key_num", 85, None),
    ("u32", "no_hermite_key_num", 85, None),
    ("u64", "root_node_tbl_ptr", 0, None),
    ("u64", "track_tbl_ptr", 0, None),
    ("u64", "clip_info_tbl_ptr", 40, None),
    ("u64", "nodes_reorder_offset2", 85, None),
    ("u64", "track_child_tbl_ptr", 0, None),
    ("u64", "node_tbl_ptr", 0, None),
    ("u64", "property_tbl_ptr", 0, None),
    ("u64", "key_tbl_ptr", 0, None),
    ("u64", "bool_keys_offset", 85, None),
    ("u64", "action_keys_offset", 85, None),
    ("u64", "no_hermite_keys_offset", 85, None),
    ("u64", "speed_point_tbl_ptr", 0, None),
    ("u64", "interpolation_hermite_tbl_ptr", 0, None),
    ("u64", "interpolation_hermite3d_tbl_ptr", 0, None),
    ("u64", "legacy_clip_info_tbl_ptr", 0, 27),
    ("u64", "last_key_tbl_ptr", 0, 43),
    ("u64", "user_data_asset_info_ptr", 62, None),
    ("u64", "c8_ptr", 0, None),
    ("u64", "c16_ptr", 0, None),
    ("u64", "oword_ptr", 0, None),
    ("u64", "data_ptr", 0, None),
)


def iter_header_fields(version: int):
    for kind, attr, min_version, max_version in _HEADER_LAYOUT:
        if version >= min_version and (max_version is None or version <= max_version):
            yield kind, attr


def header_size(version: int) -> int:
    return 8 + sum(_HEADER_FIELD_SIZES[kind] for kind, _ in iter_header_fields(version))


EXTRA_KEY_FLAG_LAST_KEY = 0x1
EXTRA_KEY_FLAG_EXTRA_KEY1 = 0x2
EXTRA_KEY_FLAG_EXTRA_KEY2 = 0x4
EXTRA_KEY_FLAG_EXTRA_KEY3 = 0x8
EXTRA_KEY_FLAGS_MASK = (
    EXTRA_KEY_FLAG_LAST_KEY
    | EXTRA_KEY_FLAG_EXTRA_KEY1
    | EXTRA_KEY_FLAG_EXTRA_KEY2
    | EXTRA_KEY_FLAG_EXTRA_KEY3
)
EXTRA_KEY_REF_ATTRS = (
    (EXTRA_KEY_FLAG_LAST_KEY, "extra_key_last_ref"),
    (EXTRA_KEY_FLAG_EXTRA_KEY1, "extra_key1_ref"),
    (EXTRA_KEY_FLAG_EXTRA_KEY2, "extra_key2_ref"),
    (EXTRA_KEY_FLAG_EXTRA_KEY3, "extra_key3_ref"),
)


def _extra_key_count(flags: int, version: int) -> int:
    if version < 53:
        return 0
    # Bit layout:
    #  - bit 0: LastKey
    #  - bit 1: ExtraKey1
    #  - bit 2: ExtraKey2
    #  - bit 3: ExtraKey3
    return (flags & EXTRA_KEY_FLAGS_MASK).bit_count()


INTERPOLATION_TYPE_HERMITE = 0x5
INTERPOLATION_TYPE_BEZIER3D = 0xC


class ClipParser:
    @staticmethod
    def _iter_property_payload_keys(prop: Property):
        yield from prop.keys
        for _, attr in EXTRA_KEY_REF_ATTRS:
            extra = getattr(prop, attr)
            if isinstance(extra, (Key, NoHermiteKey)):
                yield extra

    @staticmethod
    def _read_header_field(r: Reader, kind: str, offset: int):
        if kind == "f32":
            return r.f32(offset)
        if kind == "u32":
            return r.u32(offset)
        if kind == "u64":
            return r.u64(offset)
        if kind == "bytes16":
            return r.bytes(offset, 16)
        raise ClipParserError(f"Unsupported header field kind: {kind}")

    def parse(self, data: bytes) -> ParsedClip:
        r = Reader(data)
        header = self._read_header(r)

        root_offsets = self._read_u64_table(r, header.root_node_tbl_ptr, header.root_node_num)
        nodes_reorder_count = header.node_count_pragmata if header.node_count_pragmata > 0 else header.root_node_num
        reorder_offsets = self._read_u64_table(r, header.nodes_reorder_offset2, nodes_reorder_count)
        track_child_offsets = self._read_u64_table(r, header.track_child_tbl_ptr, header.track_child_num)

        tracks = self._read_tracks(r, header)
        clip_infos = self._read_clip_infos(r, header)
        nodes = self._read_nodes(r, header)
        properties = self._read_properties(r, header)

        main_keys = self._read_key_table(r, header.key_tbl_ptr, header.key_num, header.version)
        last_keys = self._read_last_keys(r, header)
        bool_keys = self._read_bool_keys(r, header)
        action_keys = self._read_action_keys(r, header)
        no_hermite = self._read_no_hermite_keys(r, header)
        speed_points = self._read_speed_points(r, header)

        hermite_nodes = self._read_hermite_nodes(r, header)
        bezier3d_nodes = self._read_bezier3d_nodes(r, header)
        user_data_assets = self._read_user_data_assets(r, header)
        owords = self._read_owords(r, header)
        self._validate_data_table(r, header)

        parsed = ParsedClip(
            header=header,
            root_node_offsets=root_offsets,
            root_nodes=[],
            nodes_reorder_offsets=reorder_offsets,
            nodes_reorder_nodes=[],
            track_child_offsets=track_child_offsets,
            tracks=tracks,
            clip_infos=clip_infos,
            nodes=nodes,
            properties=properties,
            main_keys=main_keys,
            last_keys=last_keys,
            bool_keys=bool_keys,
            action_keys=action_keys,
            no_hermite_keys=no_hermite,
            speed_points=speed_points,
            hermite_nodes=hermite_nodes,
            bezier3d_nodes=bezier3d_nodes,
            user_data_assets=user_data_assets,
            owords=owords,
        )
        self._attach_ownership(parsed)
        self._attach_property_ranges(parsed)
        self._attach_interpolation_references(parsed)
        self._decode_key_string_payloads(parsed, r)
        return parsed

    def _attach_ownership(self, parsed: ParsedClip):
        nodes = parsed.nodes
        props = parsed.properties
        nsz = node_size(parsed.header.version)
        root_ptr = parsed.header.node_tbl_ptr

        def _node_from_raw(raw: int) -> Node | None:
            if raw == 0:
                return None
            rel = raw - root_ptr
            if rel >= 0 and nsz > 0 and (rel % nsz) == 0:
                idx = rel // nsz
                if 0 <= idx < len(nodes):
                    return nodes[idx]
            return None

        def _slice(items: list, start: int, count: int) -> list:
            if count <= 0 or start >= len(items):
                return []
            return items[start:min(start + count, len(items))]

        def _resolve_nodes(raw_offsets: list[int], err: str) -> list[Node]:
            resolved: list[Node] = []
            for raw in raw_offsets:
                n = _node_from_raw(raw)
                if n is None:
                    self._assert(raw == 0, err)
                    continue
                resolved.append(n)
            return resolved

        # Node -> child nodes / properties
        for n in nodes:
            n.child_nodes = _slice(nodes, n.child_offset, n.node_num)
            n.properties = _slice(props, n.property_offset, n.property_num)

        # Root-node table -> concrete node references
        root_nodes = _resolve_nodes(parsed.root_node_offsets, "Invalid root-node pointer entry")
        track_nodes = _resolve_nodes(parsed.track_child_offsets, "Invalid track-child pointer entry")
        reorder_nodes = _resolve_nodes(parsed.nodes_reorder_offsets, "Invalid nodes-reorder pointer entry")

        parsed.root_nodes = root_nodes
        parsed.nodes_reorder_nodes = reorder_nodes

        # ClipInfo -> root nodes
        for ci in parsed.clip_infos:
            ci.root_nodes = _slice(root_nodes, ci.root_node_offset, ci.root_node_count)

        # Track -> clips and child roots
        for tr in parsed.tracks:
            tr.clip_infos = _slice(parsed.clip_infos, tr.clip_info_offset, tr.clip_num)
            tr.child_nodes = _slice(track_nodes, tr.child_node_start_index, tr.child_node_num)

    def _decode_key_string_payloads(self, parsed: ParsedClip, r: Reader):
        c8_types = {PropertyType.ENUM, PropertyType.STR8}
        c16_types = {
            PropertyType.STR16, PropertyType.ASSET, PropertyType.USER_DATA_ASSET,
            PropertyType.RESOURCE_PATH, PropertyType.GAME_OBJECT_REF, PropertyType.GUID,
        }
        last_key_expected_widths: dict[int, set[bool]] = {}
        if parsed.header.version <= 43:
            for prop in parsed.properties:
                ptype = property_type_or_unknown(prop.property_type)
                if ptype not in c8_types and ptype not in c16_types:
                    continue
                if prop.is_exist_last_key and prop.last_key_offset < len(parsed.last_keys):
                    last_key_expected_widths.setdefault(prop.last_key_offset, set()).add(ptype in c16_types)

        def assign_key_string(key_obj, wide: bool):
            rel = (key_obj.raw1 << 32) | key_obj.raw0
            s = self._string(r, parsed.header, rel, wide)
            key_obj.string_value = s
            key_obj.string_original_value = s
            key_obj.string_is_wide = 1 if wide else 0

        for prop in parsed.properties:
            ptype = property_type_or_unknown(prop.property_type)
            if (
                ptype == PropertyType.USER_DATA_ASSET
                and parsed.header.version >= 62
                and parsed.header.user_data_asset_info_ptr != 0
            ):
                # v62+ stores UserDataAsset key payloads as indices into the
                # UserDataAssetInfo table, not as c16 string offsets.
                continue
            if ptype not in c8_types and ptype not in c16_types:
                continue
            wide = ptype in c16_types
            # Main / bool / action / no-hermite keys attached by _attach_property_ranges
            for k in self._iter_property_payload_keys(prop):
                assign_key_string(k, wide)

            # Legacy last-key table entries (<=43)
            if parsed.header.version <= 43 and prop.is_exist_last_key and prop.last_key_offset < len(parsed.last_keys):
                lk = parsed.last_keys[prop.last_key_offset]
                expected = last_key_expected_widths.get(prop.last_key_offset, set())
                if len(expected) > 1:
                    # Ambiguous shared slot (owned by both c8 and c16 properties): keep raw payload untyped.
                    continue
                candidate_wide = 1 if wide else 0
                # Prefer c8 candidates for shared last-key slots; keep first c8 match.
                if lk.string_is_wide == 0:
                    continue
                if lk.string_is_wide == 1 and candidate_wide == 1:
                    continue
                assign_key_string(lk, wide)



    def _string(self, r: Reader, h: ClipHeader, rel: int, wide: bool) -> str:
        return r.read_wstr(h.c16_ptr + rel * 2) if wide else r.read_cstr(h.c8_ptr + rel)

    @staticmethod
    def _assert(cond: bool, msg: str):
        if not cond:
            raise ClipParserError(msg)

    def _read_header(self, r: Reader) -> ClipHeader:
        h = ClipHeader()
        h.magic = r.u32(0)
        if h.magic != CLIP_MAGIC:
            raise ClipParserError("Not a CLIP file")
        h.version = r.u32(4)

        o = 8
        for kind, attr in iter_header_fields(h.version):
            if attr is not None:
                setattr(h, attr, self._read_header_field(r, kind, o))
            o += _HEADER_FIELD_SIZES[kind]
        self._assert(h.c8_ptr <= h.c16_ptr, "Invalid string table pointers (c8_ptr > c16_ptr)")
        return h

    def _read_u64_table(self, r: Reader, ptr: int, count: int) -> list[int]:
        if ptr == 0 or count <= 0:
            return []
        return [r.u64(ptr + i * 8) for i in range(count)]

    @staticmethod
    def _read_f32_tuple(r: Reader, offset: int, count: int) -> tuple[float, ...]:
        return tuple(r.f32(offset + i * 4) for i in range(count))

    def _read_f32_rows(self, r: Reader, start: int, end: int, width: int) -> list[tuple[float, ...]]:
        if start == 0 or end <= start:
            return []
        stride = width * 4
        return [self._read_f32_tuple(r, start + i * stride, width) for i in range((end - start) // stride)]

    def _read_tracks(self, r: Reader, h: ClipHeader) -> list[Track]:
        if h.track_tbl_ptr == 0:
            return []
        out: list[Track] = []
        stride = 56 if h.version >= 40 else 48
        for i in range(h.track_num):
            o = h.track_tbl_ptr + i * stride
            tr = Track()
            tr.enable = r.u8(o)
            tr.reserved = r.u32(o + 4)
            tr.clip_num = r.i32(o + 8)
            tr.child_node_num = r.i32(o + 12)
            tr.type_ascii = self._string(r, h, r.u64(o + 16), False)
            tr.type_unicode = self._string(r, h, r.u64(o + 24), True)
            tr.group_name = self._string(r, h, r.u64(o + 32), True)
            if h.version >= 40:
                tr.clip_info_offset = r.u64(o + 40)
                self._assert(tr.clip_info_offset <= h.clip_info_num, "Track.clip_info_offset out of range")
                tr.child_node_start_index = r.u64(o + 48)
            else:
                tr.child_node_start_index = r.u64(o + 40)
            out.append(tr)
        return out

    def _read_clip_infos(self, r: Reader, h: ClipHeader) -> list[ClipInfo]:
        if h.clip_info_tbl_ptr == 0 or h.clip_info_num == 0:
            return []
        out: list[ClipInfo] = []
        stride = 40 if h.version >= 85 else 24
        for i in range(h.clip_info_num):
            o = h.clip_info_tbl_ptr + i * stride
            ci = ClipInfo()
            ci.frame_in = r.f32(o)
            ci.frame_out = r.f32(o + 4)
            ci.source_in = r.f32(o + 8)
            ci.source_out = r.f32(o + 12)
            if h.version >= 85:
                ci.root_node_count = r.u64(o + 16)
                ci.unicode_name = self._string(r, h, r.u64(o + 24), True)
                ci.root_node_offset = r.u64(o + 32)
            else:
                ci.unicode_name = self._string(r, h, r.u64(o + 16), True)
            out.append(ci)
        return out

    def _read_nodes(self, r: Reader, h: ClipHeader) -> list[Node]:
        if h.node_tbl_ptr == 0:
            return []
        out: list[Node] = []
        sz = node_size(h.version)
        for i in range(h.node_num):
            o = h.node_tbl_ptr + i * sz
            n = Node()
            n.node_num = r.u32(o)
            n.property_num = r.u32(o + 4)
            n.begin_frame = r.f32(o + 8)
            n.end_frame = r.f32(o + 12)
            p = o + 16
            n.root_node_guid = r.bytes(p, 16); p += 16
            if h.version <= 43:
                n.ex_id = r.bytes(p, 16); p += 16

            bits = r.u32(p)
            n.node_type = bits & 0xFF
            if h.version >= 53:
                n.unique_id = (bits >> 8) & 0xFFFF
                if h.version >= 86:
                    n.extra_property_pass_mask = (bits >> 24) & 0xF
                    n.route_to_secondary_property_list = (bits >> 28) & 0x1
                    n.reserved_3bits = (bits >> 29) & 0x7
                    self._assert(n.route_to_secondary_property_list == 0 and n.reserved_3bits == 0,
                                 "Unexpected nonzero node route/reserved bits")
                else:
                    n.padding_v53 = (bits >> 24) & 0xFF
                    self._assert(n.padding_v53 == 0, "Unexpected nonzero node padding byte")
            p += 4
            n.dev32_id = r.u32(p); p += 4
            if h.version >= 86:
                n.unique32_id = r.u32(p)
                n.name_hash = 0
            else:
                n.name_hash = r.u32(p)
                n.unique32_id = 0
            n.unicode_name_hash = r.u32(p + 4)
            if h.version == 34:
                n.name = self._string(r, h, r.u64(p + 8), True)
                n.child_offset = r.u64(p + 16)
                n.property_offset = r.u64(p + 24)
            elif h.version == 27:
                n.name = self._string(r, h, r.u64(p + 8), False)
                n.node_tag = self._string(r, h, r.u64(p + 16), True)
                n.child_offset = r.u64(p + 24)
                n.property_offset = r.u64(p + 32)
            else:
                n.name = self._string(r, h, r.u64(p + 8), True)
                n.node_tag = self._string(r, h, r.u64(p + 16), True)
                n.child_offset = r.u64(p + 24)
                n.property_offset = r.u64(p + 32)
            out.append(n)
        return out

    def _read_properties(self, r: Reader, h: ClipHeader) -> list[Property]:
        if h.property_tbl_ptr == 0:
            return []
        out: list[Property] = []
        sz = prop_size(h.version)
        for i in range(h.property_num):
            o = h.property_tbl_ptr + i * sz
            p = Property()
            if h.version >= 40:
                p.begin_frame = r.f32(o)
                p.end_frame = r.f32(o + 4)
                if h.version >= 86:
                    p.unique32_id = r.u32(o + 8)
                    p.name_hash = 0
                else:
                    p.name_hash = r.u32(o + 8)
                    p.unique32_id = 0
                p.unicode_name_hash = r.u32(o + 12)
                p.name = self._string(r, h, r.u64(o + 16), False)
                p.data_offset = r.u64(o + 24)
                if h.version <= 43:
                    self._assert(p.data_offset == 0, "Legacy property dataOffset nonzero is unsupported")
                p.key_or_child_offset = r.u64(o + 32)
                p.key_num_or_element_num = r.u16(o + 40)
                p.array_index = r.i16(o + 42)
                p.speed_point_num = r.u8(o + 44) if h.version >= 43 else r.u16(o + 44)
                p.property_type = r.u8(o + (45 if h.version >= 43 else 46))
                fb1 = r.u8(o + (46 if h.version >= 43 else 47))
                p.legacy_flags_raw = fb1
                p.is_enum_closed = fb1 & 1
                p.set_after_end_frame = (fb1 >> 1) & 1
                p.is_exist_last_key = (fb1 >> 2) & 1 if h.version <= 43 else 0
                p.is_set_delegate_enable = (fb1 >> 2) & 1 if h.version >= 44 else (fb1 >> 4) & 1
                p.is_prev_diff_frame_set = (fb1 >> 3) & 1 if h.version >= 44 else (fb1 >> 5) & 1
                p.is_next_diff_frame_set = (fb1 >> 4) & 1 if h.version >= 44 else (fb1 >> 6) & 1
                p.is_prev_key_value_set = (fb1 >> 5) & 1 if h.version >= 44 else (fb1 >> 7) & 1
                p.is_delayed_execution_or_array_count_set = (fb1 >> 6) & 1 if h.version >= 44 else 0

                if h.version >= 44:
                    fb2 = r.u8(o + 47)
                    if h.version < 85:
                        p.extra_key_flags = fb2 & 0xF
                    else:
                        # v85+ layout:
                        # - byte 46 bit7: has_set_property_delegate
                        # - byte 47 bits 0..3: extra_key_flags
                        # - byte 47 bits 4..7: aux_key_flags
                        p.has_set_property_delegate = (fb1 >> 7) & 0x1
                        p.extra_key_flags = fb2 & 0xF
                        p.aux_key_flags = (fb2 >> 4) & 0xF
                        self._assert(p.aux_key_flags <= 0x3, "Unknown aux_key_flags value")

                table_p = o + 48
                if h.version <= 43:
                    p.last_key_offset = r.u64(table_p)
                    if h.version == 43:
                        self._assert(r.u8(o + 47) == 0, "v43 property reserved byte must be zero")
                    table_p += 8
                p.speed_point_offset = r.u64(table_p)
                if h.version <= 43:
                    p.clip_property_offset = r.u64(table_p + 8)
            else:
                p.array_index = r.i32(o)
                p.begin_frame = r.f32(o + 4)
                p.end_frame = r.f32(o + 8)

                always_two = r.u32(o + 12)
                self._assert(always_two == 2, "Legacy property alwaysTwo is expected to be 2")

                p.speed_point_num = r.u32(o + 16)
                legacy_bits = r.u32(o + 20)
                p.property_type = legacy_bits & 0xFF
                fb1 = (legacy_bits >> 8) & 0xFF
                p.legacy_flags_raw = fb1
                self._assert((legacy_bits >> 16) == 0, "Legacy property reserved padding must be zero")

                p.is_enum_closed = fb1 & 0x1
                p.set_after_end_frame = (fb1 >> 1) & 0x1
                p.is_exist_last_key = (fb1 >> 2) & 0x1
                # legacy bit layout carries old semantics
                p.is_set_delegate_enable = (fb1 >> 4) & 0x1
                p.is_prev_diff_frame_set = (fb1 >> 5) & 0x1
                p.is_next_diff_frame_set = (fb1 >> 6) & 0x1
                p.is_prev_key_value_set = (fb1 >> 7) & 0x1

                p.name_hash = r.u32(o + 24)
                p.unique32_id = 0
                p.unicode_name_hash = r.u32(o + 28)
                p.name = self._string(r, h, r.u64(o + 32), False)
                p.legacy_unicode_name = self._string(r, h, r.u64(o + 40), True)

                ukn_ptr = r.u64(o + 48)
                ukn_num = r.u16(o + 56)
                ukn0 = r.i16(o + 58)
                ukn1 = r.u16(o + 60)
                ukn2 = r.u16(o + 62)
                self._assert(
                    ukn_ptr == 0 and ukn_num == 0 and ukn0 == 0 and ukn1 == 0 and ukn2 == 0,
                    "Legacy property unknown fields must be zero",
                )

                p.data_offset = r.u64(o + 64)
                p.key_or_child_offset = r.u64(o + 72)
                p.key_num_or_element_num = r.u64(o + 80)
                p.last_key_offset = r.u64(o + 88)
                p.speed_point_offset = r.u64(o + 96)
                p.clip_property_offset = r.u64(o + 104)

                self._assert(p.data_offset == 0, "Legacy property dataOffset nonzero is unsupported")
                self._assert(p.clip_property_offset == 0, "Legacy property clipPropertyOffset nonzero is unsupported")
            out.append(p)
        return out

    def _read_key_table(self, r: Reader, ptr: int, count: int, version: int) -> list[Key]:
        if ptr == 0 or count == 0:
            return []
        out = []
        stride = key_size(version)
        for i in range(count):
            o = ptr + i * stride
            k = Key()
            k.frame = r.f32(o)
            k.rate = r.f32(o + 4)
            packed = r.u32(o + 8)
            k.interpolation_type = packed & 0xFF
            k.offset_frame_flag = (packed >> 8) & 0x1
            k.reserved = (packed >> 9) & 0x7FFFFF
            if version < 53:
                k.reserved2 = r.u32(o + 12)
                k.frame_span = 0
            else:
                k.reserved2 = 0
                k.frame_span = r.u32(o + 12)
            k.raw0 = r.u32(o + 16)
            k.raw1 = r.u32(o + 20)
            k.interpolation_offset = r.u64(o + 24)
            if stride == 40:
                k.legacy_tail_raw = r.bytes(o + 32, 8)
            out.append(k)
        return out

    def _read_last_keys(self, r: Reader, h: ClipHeader) -> list[Key]:
        if h.version > 43 or h.last_key_tbl_ptr == 0 or h.c8_ptr <= h.last_key_tbl_ptr:
            return []
        count = (h.c8_ptr - h.last_key_tbl_ptr) // key_size(h.version)
        return self._read_key_table(r, h.last_key_tbl_ptr, count, h.version)

    def _read_bool_keys(self, r: Reader, h: ClipHeader) -> list[BoolKey]:
        if h.version < 85 or h.bool_keys_offset == 0:
            return []
        out = []
        for i in range(h.bool_key_num):
            o = h.bool_keys_offset + i * 8
            packed = r.u32(o + 4)
            out.append(BoolKey(
                frame=r.f32(o),
                bool_value=packed & 0x1,
                interpolation_type_to_next=(packed >> 1) & 0xFF,
                offset_frame_flag=(packed >> 9) & 0x1,
                range_v2_frame_span=(packed >> 10) & 0xFFFF,
                reserved=(packed >> 26) & 0x3F,
            ))
        return out

    def _read_action_keys(self, r: Reader, h: ClipHeader) -> list[ActionKey]:
        if h.version < 85 or h.action_keys_offset == 0:
            return []
        out = []
        for i in range(h.action_key_num):
            o = h.action_keys_offset + i * 8
            packed = r.u32(o + 4)
            k = ActionKey(frame=r.f32(o), interpolation_type=packed & 0xFF, reserved=(packed >> 8) & 0xFFFFFF)
            self._assert(k.reserved == 0, "nonzero ActionKey reserved")
            out.append(k)
        return out

    def _read_no_hermite_keys(self, r: Reader, h: ClipHeader) -> list[NoHermiteKey]:
        if h.version < 85 or h.no_hermite_keys_offset == 0:
            return []
        out = []
        for i in range(h.no_hermite_key_num):
            o = h.no_hermite_keys_offset + i * 16
            packed = r.u32(o + 4)
            out.append(NoHermiteKey(
                frame=r.f32(o),
                interpolation_type_to_next=packed & 0xFF,
                offset_frame_flag=(packed >> 8) & 0x1,
                range_v2_frame_span=(packed >> 9) & 0xFFFF,
                reserved=(packed >> 25) & 0x7F,
                raw0=r.u32(o + 8),
                raw1=r.u32(o + 12),
            ))
        return out

    def _read_speed_points(self, r: Reader, h: ClipHeader) -> list[SpeedPoint]:
        if h.speed_point_tbl_ptr == 0 or h.interpolation_hermite_tbl_ptr <= h.speed_point_tbl_ptr:
            return []
        count = (h.interpolation_hermite_tbl_ptr - h.speed_point_tbl_ptr) // 24
        out = []
        for i in range(count):
            o = h.speed_point_tbl_ptr + i * 24
            self._assert(r.u32(o + 12) == 0, "nonzero SpeedPoint padding")
            out.append(SpeedPoint(r.f32(o), r.f32(o + 4), r.u32(o + 8), r.u64(o + 16)))
        return out

    def _post_interpolation_ptr(self, h: ClipHeader) -> int:
        if h.version <= 43 and h.last_key_tbl_ptr:
            return h.last_key_tbl_ptr
        if h.version >= 62 and h.user_data_asset_info_ptr:
            return h.user_data_asset_info_ptr
        return h.c8_ptr

    def _read_hermite_nodes(self, r: Reader, h: ClipHeader) -> list[tuple[float, float, float, float]]:
        start = h.interpolation_hermite_tbl_ptr
        end = h.interpolation_hermite3d_tbl_ptr or self._post_interpolation_ptr(h)
        return self._read_f32_rows(r, start, end, 4)

    def _read_bezier3d_nodes(self, r: Reader, h: ClipHeader) -> list[tuple[float, float, float, float, float, float, float, float]]:
        if h.interpolation_hermite3d_tbl_ptr == 0:
            return []
        start = h.interpolation_hermite3d_tbl_ptr
        end = self._post_interpolation_ptr(h)
        return self._read_f32_rows(r, start, end, 8)

    def _read_user_data_assets(self, r: Reader, h: ClipHeader) -> list[UserDataAssetInfo]:
        if h.user_data_asset_info_ptr == 0 or h.c8_ptr <= h.user_data_asset_info_ptr:
            return []
        count = (h.c8_ptr - h.user_data_asset_info_ptr) // 16
        out = []
        for i in range(count):
            o = h.user_data_asset_info_ptr + i * 16
            out.append(UserDataAssetInfo(
                type_ascii=self._string(r, h, r.u64(o), False),
                path_unicode=self._string(r, h, r.u64(o + 8), True),
            ))
        return out

    def _read_owords(self, r: Reader, h: ClipHeader) -> list[tuple[float, float, float, float]]:
        if h.oword_ptr == 0:
            return []
        end = h.data_ptr if h.data_ptr != 0 else r.size
        return self._read_f32_rows(r, h.oword_ptr, end, 4)

    def _validate_data_table(self, r: Reader, h: ClipHeader):
        if h.data_ptr == 0:
            return
        self._assert(h.data_ptr <= r.size, "data_ptr out of range")
        # CLIP-family data table layout:
        # - all versions: 8 zero bytes, then u64(total file size)
        # - v62+: optional/expected duplicate u64(total file size) at +16
        self._assert(h.data_ptr + 16 <= r.size, "data_ptr table truncated")
        self._assert(r.u64(h.data_ptr) == 0, "data_ptr table first qword must be zero")
        self._assert(r.u64(h.data_ptr + 8) == r.size, "data_ptr table file-size qword mismatch")
        if h.version >= 62 and h.data_ptr + 24 <= r.size:
            self._assert(r.u64(h.data_ptr + 16) == r.size, "data_ptr table duplicate file-size qword mismatch")

    def _attach_property_ranges(self, parsed: ParsedClip):
        h = parsed.header
        if h.root_node_num > 0 and h.track_child_num > 0:
            self._assert(h.track_child_num == h.root_node_num, "trackChildNum != rootNodeNum")

        if h.root_node_num > 0 and h.track_num > 0:
            total = 0
            for tr in parsed.tracks:
                # Despite the legacy field name, this value is a root-node table start index.
                child_start_index = tr.child_node_start_index
                self._assert(child_start_index + tr.child_node_num <= h.root_node_num,
                             "Track child range exceeds rootNodeNum")
                total += tr.child_node_num
            self._assert(total == h.root_node_num, "Sum of Track.childNodeNum does not match rootNodeNum")

        key_tables = [parsed.main_keys, parsed.bool_keys, parsed.action_keys, parsed.no_hermite_keys]
        key_errors = [
            "Main key range exceeds table",
            "Bool key range exceeds table",
            "Action key range exceeds table",
            "NoHermite key range exceeds table",
        ]
        key_owners = [[0] * len(keys) for keys in key_tables]

        def _assign_keys(prop: Property, key_kind: int, total: int):
            keys = key_tables[key_kind]
            start = prop.key_or_child_offset
            self._assert(start + total <= len(keys), key_errors[key_kind])
            prop.keys = keys[start:start + total]
            for idx in range(start, start + total):
                key_owners[key_kind][idx] += 1

        for prop in parsed.properties:
            prop.last_key_ref = None
            prop.extra_key_last_ref = None
            prop.extra_key1_ref = None
            prop.extra_key2_ref = None
            prop.extra_key3_ref = None
            prop.speed_points_ref = []
            prop.clip_property_ref = None
            if h.version < 40 and prop.speed_point_num > 0:
                self._assert(
                    prop.property_type == PropertyType.PATH_POINT3D,
                    "Only PathPoint3D legacy properties are expected to own speed points",
                )
                self._assert(
                    prop.speed_point_offset + prop.speed_point_num <= len(parsed.speed_points),
                    "Legacy property speed-point range exceeds table",
                )

            extra = _extra_key_count(prop.extra_key_flags, h.version)
            total = prop.key_num_or_element_num + extra
            if prop.speed_point_num > 0:
                self._assert(
                    prop.speed_point_offset + prop.speed_point_num <= len(parsed.speed_points),
                    "Property speed-point range exceeds table",
                )
                end_sp = prop.speed_point_offset + prop.speed_point_num
                prop.speed_points_ref = parsed.speed_points[prop.speed_point_offset:end_sp]
            if h.version <= 43 and prop.is_exist_last_key and prop.last_key_offset < len(parsed.last_keys):
                prop.last_key_ref = parsed.last_keys[prop.last_key_offset]
            if h.version <= 43 and prop.clip_property_offset > 0 and prop.clip_property_offset < len(parsed.properties):
                prop.clip_property_ref = parsed.properties[prop.clip_property_offset]
            if total <= 0:
                continue
            ptype = property_type_or_unknown(prop.property_type)
            if ptype in PROPERTY_TYPES_WITH_CHILDREN:
                self._assert(
                    prop.key_or_child_offset + prop.key_num_or_element_num <= len(parsed.properties),
                    "Property child range out of bounds",
                )
                end = prop.key_or_child_offset + prop.key_num_or_element_num
                prop.children = list(range(prop.key_or_child_offset, end))
                prop.child_properties = parsed.properties[prop.key_or_child_offset:end]
                continue
            key_kind = prop.aux_key_flags if (parsed.header.version >= 85 and prop.aux_key_flags < 4) else 0
            _assign_keys(prop, key_kind, total)
            if parsed.header.version >= 53:
                extra_count = _extra_key_count(prop.extra_key_flags, parsed.header.version)
                if extra_count > 0:
                    self._assert(
                        prop.key_num_or_element_num + extra_count <= len(prop.keys),
                        "Property extra-key range out of bounds",
                    )
                    ordered_extra_keys = prop.keys[prop.key_num_or_element_num:prop.key_num_or_element_num + extra_count]
                    prop.keys = prop.keys[:prop.key_num_or_element_num]
                    extra_idx = 0
                    for bit, attr in EXTRA_KEY_REF_ATTRS:
                        if prop.extra_key_flags & bit:
                            setattr(prop, attr, ordered_extra_keys[extra_idx])
                            extra_idx += 1

            if ptype == PropertyType.USER_DATA_ASSET and parsed.header.version >= 62 and parsed.user_data_assets:
                for key_obj in self._iter_property_payload_keys(prop):
                    idx = (key_obj.raw1 << 32) | key_obj.raw0
                    self._assert(
                        0 <= idx < len(parsed.user_data_assets),
                        "UserDataAsset key index out of range",
                    )
                    key_obj.user_data_asset_index = idx
                    key_obj.user_data_asset_ref = parsed.user_data_assets[idx]

            if ptype == PropertyType.PATH_POINT3D:
                for key_obj in self._iter_property_payload_keys(prop):
                    key_obj.oword_ref = None
                    oword_idx = key_obj.raw0 & 0xFFFFFFFF
                    self._assert(0 <= oword_idx < len(parsed.owords), "PathPoint3D OWord index out of range")
                    key_obj.oword_ref = parsed.owords[oword_idx]

        for key_kind, owners in enumerate(key_owners):
            bad = next((idx for idx, count in enumerate(owners) if count != 1), None)
            if bad is None:
                continue
            self._assert(False, f"Key table {key_kind} entry {bad} has owner count {owners[bad]} (expected 1)")

    def _attach_interpolation_references(self, parsed: ParsedClip):
        hermite_nodes = parsed.hermite_nodes
        bezier3d_nodes = parsed.bezier3d_nodes

        def _attach(obj):
            obj.interpolation_ref = None
            if obj.interpolation_type == INTERPOLATION_TYPE_HERMITE:
                obj.interpolation_ref = hermite_nodes[obj.interpolation_offset]
            elif obj.interpolation_type == INTERPOLATION_TYPE_BEZIER3D:
                obj.interpolation_ref = bezier3d_nodes[obj.interpolation_offset]

        for group in (parsed.main_keys, parsed.last_keys, parsed.speed_points):
            for obj in group:
                _attach(obj)
