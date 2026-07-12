from __future__ import annotations

import struct
from collections import Counter
from copy import deepcopy

from .enums import CLIP_MAGIC, PROPERTY_TYPES_WITH_CHILDREN, PropertyType, property_type_or_unknown
from .parser import (
    ParsedClip,
    header_size,
    iter_header_fields,
    key_size,
    node_size,
    prop_size,
)
from .reader import ClipParserError
from .structures import ActionKey, BoolKey, Key, NoHermiteKey, SpeedPoint
from utils.hash_util import murmur3_hash


INTERPOLATION_TYPE_HERMITE = 0x5
INTERPOLATION_TYPE_BEZIER3D = 0xC
_HEADER_PACK_FORMATS = {
    "f32": "<f",
    "u32": "<I",
    "u64": "<Q",
}
_HEADER_PACK_SIZES = {
    **{kind: struct.calcsize(fmt) for kind, fmt in _HEADER_PACK_FORMATS.items()},
    "bytes16": 16,
}


class _StringPool:
    def __init__(self, wide: bool):
        self.wide = wide
        self.data = bytearray()
        self._offsets: dict[str, int] = {}
        self.zero_value: str | None = None

    def add(self, s: str, dedupe: bool = True) -> int:
        if "\x00" in s:
            raise ClipParserError("Serialized strings cannot contain embedded NUL characters")
        if dedupe:
            existing = self._offsets.get(s)
            if existing is not None:
                return existing
        off = len(self.data) // 2 if self.wide else len(self.data)
        if not self.data:
            self.zero_value = s
        enc = s.encode("utf-16le" if self.wide else "utf-8")
        self.data.extend(enc)
        self.data.extend(b"\x00\x00" if self.wide else b"\x00")
        if dedupe:
            self._offsets[s] = off
        return off

class ClipWriter:
    def build(self, parsed: ParsedClip) -> bytes:
        # Normalization rewrites table order, counts and offsets. Work on an
        # alias-preserving copy so a failed save cannot partially mutate the
        # editor's live graph.
        working = deepcopy(parsed)
        return self._build_in_place(working)

    def _build_in_place(self, parsed: ParsedClip) -> bytes:
        self._validate_graph(parsed)
        self._rebuild_tables_from_graph(parsed)
        self._recalculate_string_hashes(parsed)
        h = parsed.header
        h.magic = CLIP_MAGIC
        h.root_node_num = len(parsed.root_node_offsets)
        h.track_num = len(parsed.tracks)
        h.clip_info_num = len(parsed.clip_infos) if h.version >= 40 else 0
        h.track_child_num = len(parsed.track_child_offsets)
        h.node_count_pragmata = len(parsed.nodes_reorder_offsets) if h.version >= 85 else 0
        h.node_num = len(parsed.nodes)
        h.property_num = len(parsed.properties)
        h.key_num = len(parsed.main_keys)
        h.bool_key_num = len(parsed.bool_keys) if h.version >= 85 else 0
        h.action_key_num = len(parsed.action_keys) if h.version >= 85 else 0
        h.no_hermite_key_num = len(parsed.no_hermite_keys) if h.version >= 85 else 0

        out = bytearray()
        out.extend(b"\x00" * header_size(h.version))

        def tell() -> int:
            return len(out)

        def align(a: int):
            pad = (a - (len(out) % a)) % a
            if pad:
                out.extend(b"\x00" * pad)

        def w32(v: int):
            out.extend(struct.pack("<I", v & 0xFFFFFFFF))

        def w64(v: int):
            out.extend(struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF))

        def wf(v: float):
            out.extend(struct.pack("<f", v))

        def legacy_property_flags(prop) -> int:
            return (
                (prop.is_enum_closed & 1)
                | ((prop.set_after_end_frame & 1) << 1)
                | ((prop.is_exist_last_key & 1) << 2)
                | ((prop.is_restoration & 1) << 3)
                | ((prop.is_set_delegate_enable & 1) << 4)
                | ((prop.is_prev_diff_frame_set & 1) << 5)
                | ((prop.is_next_diff_frame_set & 1) << 6)
                | ((prop.is_prev_key_value_set & 1) << 7)
            )

        c8_pool = _StringPool(False)
        c16_pool = _StringPool(True)
        patch_entries: list[tuple[int, str, bool, bool]] = []

        reference_keys = [
            *parsed.main_keys,
            *parsed.no_hermite_keys,
            *parsed.last_keys,
        ]
        rebuilt_user_data_assets = self._retain_reference_order(
            parsed.user_data_assets,
            [getattr(key, "user_data_asset_ref", None) for key in reference_keys],
        )
        rebuilt_user_data_asset_index_by_id: dict[int, int] = {
            id(asset): index for index, asset in enumerate(rebuilt_user_data_assets)
        }
        rebuilt_owords: list[tuple[float, float, float, float]] = self._retain_reference_order(
            parsed.owords,
            [getattr(key, "oword_ref", None) for key in reference_keys],
        )
        rebuilt_oword_index_by_id: dict[int, int] = {
            id(row): index for index, row in enumerate(rebuilt_owords)
        }

        def add_key_string_patch(pos: int, key_obj):
            preserve_zero = (
                getattr(key_obj, "raw0", None) == 0
                and getattr(key_obj, "raw1", None) == 0
                and key_obj.string_original_value is not None
                and key_obj.string_value == key_obj.string_original_value
            )
            if key_obj.string_is_wide in (0, 1):
                patch_entries.append((pos, key_obj.string_value, bool(key_obj.string_is_wide), preserve_zero))

        def write_string_ref(value: str, wide: bool):
            patch_entries.append((tell(), value, wide, False))
            w64(0)

        def assign_reference_indices(key):
            if h.version >= 62 and key.user_data_asset_ref is not None:
                index = rebuilt_user_data_asset_index_by_id[id(key.user_data_asset_ref)]
                key.user_data_asset_index = index
                key.raw0, key.raw1 = index & 0xFFFFFFFF, index >> 32
            if getattr(key, "oword_ref", None) is not None:
                key.raw0 = rebuilt_oword_index_by_id[id(key.oword_ref)]

        def allocate_string_patches(sorted_entries: list[tuple[int, str, bool, bool]]):
            dedupe = h.version >= 34
            for pos, s, is_wide, preserve_zero in sorted_entries:
                pool = c16_pool if is_wide else c8_pool
                rel = 0 if preserve_zero and pool.zero_value == s else pool.add(s, dedupe=dedupe)
                struct.pack_into("<Q", out, pos, rel)

        # Root table (patched once node table position is known)
        align(8)
        h.root_node_tbl_ptr = tell()
        root_table_patch_pos = tell()
        for _ in parsed.root_node_offsets:
            w64(0)

        # Tracks
        align(8)
        h.track_tbl_ptr = tell()
        for tr in parsed.tracks:
            out.extend(struct.pack("<B3x", tr.enable & 0xFF))
            w32(tr.reserved)
            out.extend(struct.pack("<i", tr.clip_num))
            out.extend(struct.pack("<i", tr.child_node_num))
            write_string_ref(tr.type_ascii, False)
            write_string_ref(tr.type_unicode, True)
            write_string_ref(tr.group_name, True)
            if h.version >= 40:
                w64(tr.clip_info_offset)
            w64(tr.child_node_start_index)

        # ClipInfo
        align(8)
        h.clip_info_tbl_ptr = tell() if h.version >= 40 else 0
        if h.version >= 40:
            for ci in parsed.clip_infos:
                wf(ci.frame_in); wf(ci.frame_out); wf(ci.source_in); wf(ci.source_out)
                if h.version >= 85:
                    w64(ci.root_node_count)
                write_string_ref(ci.unicode_name, True)
                if h.version >= 85:
                    w64(ci.root_node_offset)

        # Nodes reorder
        align(8)
        h.nodes_reorder_offset2 = tell() if h.version >= 85 else 0
        nodes_reorder_patch_pos = tell()
        if h.version >= 85:
            for _ in parsed.nodes_reorder_offsets:
                w64(0)

        # Track children (patched once node table position is known)
        align(8)
        h.track_child_tbl_ptr = tell()
        track_child_table_patch_pos = tell()
        for _ in parsed.track_child_offsets:
            w64(0)

        # Nodes
        align(8)
        h.node_tbl_ptr = tell()
        for n in parsed.nodes:
            rec = bytearray(node_size(h.version))
            struct.pack_into("<I", rec, 0, n.node_num)
            struct.pack_into("<I", rec, 4, n.property_num)
            struct.pack_into("<f", rec, 8, n.begin_frame)
            struct.pack_into("<f", rec, 12, n.end_frame)
            if len(n.root_node_guid) != 16:
                raise ClipParserError("Node root_node_guid must be 16 bytes")
            rec[16:32] = n.root_node_guid
            p = 32
            if h.version <= 43:
                if len(n.ex_id) != 16:
                    raise ClipParserError("Node ex_id must be 16 bytes for CLIP <= 43")
                rec[p:p + 16] = n.ex_id
                p += 16
            if h.version >= 53:
                bits = (n.node_type & 0xFF) | ((n.unique_id & 0xFFFF) << 8)
                if h.version >= 86:
                    bits |= (n.extra_property_pass_mask & 0xF) << 24
            else:
                bits = n.node_type & 0xFF
            struct.pack_into("<I", rec, p, bits); p += 4
            struct.pack_into("<I", rec, p, n.dev32_id if h.version >= 86 else 0); p += 4
            struct.pack_into("<I", rec, p, n.unique32_id if h.version >= 86 else n.name_hash)
            struct.pack_into("<I", rec, p + 4, n.unicode_name_hash)

            name_wide = h.version != 27
            patch_entries.append((tell() + p + 8, n.name, name_wide, False))
            if h.version != 34:
                patch_entries.append((tell() + p + 16, n.node_tag, True, False))
            if h.version == 34:
                struct.pack_into("<Q", rec, p + 16, n.child_offset)
                struct.pack_into("<Q", rec, p + 24, n.property_offset)
            else:
                struct.pack_into("<Q", rec, p + 24, n.child_offset)
                struct.pack_into("<Q", rec, p + 32, n.property_offset)
            out.extend(rec)

        # Patch root/track-child tables using resolved node references (raw node-record pointers).
        nsz = node_size(h.version)
        if h.version >= 85:
            for i, idx in enumerate(parsed.nodes_reorder_offsets):
                struct.pack_into("<Q", out, nodes_reorder_patch_pos + i * 8, h.node_tbl_ptr + idx * nsz)
        for i, idx in enumerate(parsed.root_node_offsets):
            struct.pack_into("<Q", out, root_table_patch_pos + i * 8, h.node_tbl_ptr + idx * nsz)
        for i, idx in enumerate(parsed.track_child_offsets):
            struct.pack_into("<Q", out, track_child_table_patch_pos + i * 8, h.node_tbl_ptr + idx * nsz)

        # Properties
        align(8)
        h.property_tbl_ptr = tell()
        for p in parsed.properties:
            rec = bytearray(prop_size(h.version))
            if h.version >= 40:
                struct.pack_into("<f", rec, 0, p.begin_frame)
                struct.pack_into("<f", rec, 4, p.end_frame)
                struct.pack_into("<I", rec, 8, p.unique32_id if h.version >= 86 else p.name_hash)
                struct.pack_into("<I", rec, 12, p.unicode_name_hash)
                patch_entries.append((tell() + 16, p.name, False, False))
                struct.pack_into("<Q", rec, 24, p.data_offset)
                struct.pack_into("<Q", rec, 32, p.key_or_child_offset)
                struct.pack_into("<H", rec, 40, p.key_num_or_element_num & 0xFFFF)
                struct.pack_into("<h", rec, 42, p.array_index)
                if h.version >= 43:
                    struct.pack_into("<B", rec, 44, p.speed_point_num & 0xFF)
                    struct.pack_into("<B", rec, 45, p.property_type & 0xFF)
                    prop_type_off = 45
                else:
                    struct.pack_into("<H", rec, 44, p.speed_point_num & 0xFFFF)
                    struct.pack_into("<B", rec, 46, p.property_type & 0xFF)
                    prop_type_off = 46
                if h.version <= 43:
                    struct.pack_into("<B", rec, prop_type_off + 1, legacy_property_flags(p))
                    struct.pack_into("<Q", rec, 48, p.last_key_offset)
                    struct.pack_into("<Q", rec, 56, p.speed_point_offset)
                    struct.pack_into("<Q", rec, 64, p.clip_property_offset)
                else:
                    fb1 = (
                        (p.is_enum_closed & 1)
                        | ((p.set_after_end_frame & 1) << 1)
                        | ((p.is_set_delegate_enable & 1) << 2)
                        | ((p.is_prev_diff_frame_set & 1) << 3)
                        | ((p.is_next_diff_frame_set & 1) << 4)
                        | ((p.is_prev_key_value_set & 1) << 5)
                        | ((p.is_delayed_execution & 1) << 6)
                    )
                    if h.version >= 85:
                        fb1 |= (p.has_set_property_delegate & 1) << 7
                    else:
                        fb1 |= (p.is_array_count_set & 1) << 7
                    # v85+ layout:
                    # - byte 46 bit7: has_set_property_delegate
                    # - byte 47 bits 0..3: extra_key_flags
                    # - byte 47 bits 4..7: aux_key_flags
                    fb2 = ((p.extra_key_flags & 0xF) if h.version < 85 else
                           ((p.extra_key_flags & 0xF) | ((p.aux_key_flags & 0xF) << 4)))
                    struct.pack_into("<B", rec, 46, fb1)
                    struct.pack_into("<B", rec, 47, fb2)
                    struct.pack_into("<Q", rec, 48, p.speed_point_offset)
            else:
                struct.pack_into("<i", rec, 0, p.array_index)
                struct.pack_into("<f", rec, 4, p.begin_frame)
                struct.pack_into("<f", rec, 8, p.end_frame)
                struct.pack_into("<I", rec, 12, 2)
                struct.pack_into("<I", rec, 16, p.speed_point_num)
                struct.pack_into(
                    "<I", rec, 20,
                    (p.property_type & 0xFF) | (legacy_property_flags(p) << 8),
                )
                struct.pack_into("<I", rec, 24, p.name_hash)
                struct.pack_into("<I", rec, 28, p.unicode_name_hash)
                patch_entries.append((tell() + 32, p.name, False, False))
                patch_entries.append((tell() + 40, p.legacy_unicode_name, True, False))
                struct.pack_into("<Q", rec, 64, p.data_offset)
                struct.pack_into("<Q", rec, 72, p.key_or_child_offset)
                struct.pack_into("<Q", rec, 80, p.key_num_or_element_num)
                struct.pack_into("<Q", rec, 88, p.last_key_offset)
                struct.pack_into("<Q", rec, 96, p.speed_point_offset)
                struct.pack_into("<Q", rec, 104, p.clip_property_offset)
            out.extend(rec)

        # Key tables
        align(8)
        h.key_tbl_ptr = tell()
        for k in parsed.main_keys:
            key_start = tell()
            assign_reference_indices(k)
            self._write_key(out, k, h.version)
            add_key_string_patch(key_start + 16, k)

        align(8)
        h.bool_keys_offset = tell() if h.version >= 85 else 0
        if h.version >= 85:
            for k in parsed.bool_keys:
                wf(k.frame)
                packed = (
                    (k.bool_value & 1)
                    | ((k.interpolation_type_to_next & 0xFF) << 1)
                    | ((k.offset_frame_flag & 1) << 9)
                    | ((k.range_v2_frame_span & 0xFFFF) << 10)
                    | ((k.reserved & 0x3F) << 26)
                )
                w32(packed)

        align(8)
        h.action_keys_offset = tell() if h.version >= 85 else 0
        if h.version >= 85:
            for k in parsed.action_keys:
                wf(k.frame)
                w32(k.interpolation_type & 0xFF)

        align(8)
        h.no_hermite_keys_offset = tell() if h.version >= 85 else 0
        if h.version >= 85:
            for k in parsed.no_hermite_keys:
                key_start = tell()
                assign_reference_indices(k)
                wf(k.frame)
                packed = (
                    (k.interpolation_type_to_next & 0xFF)
                    | ((k.offset_frame_flag & 1) << 8)
                    | ((k.range_v2_frame_span & 0xFFFF) << 9)
                    | ((k.reserved & 0x7F) << 25)
                )
                w32(packed)
                w32(k.raw0)
                w32(k.raw1)
                add_key_string_patch(key_start + 8, k)

        align(8)
        h.speed_point_tbl_ptr = tell()
        for sp in parsed.speed_points:
            wf(sp.frame)
            wf(sp.rate)
            w32(sp.interpolation_type)
            w32(0)
            w64(sp.interpolation_offset)

        align(8)
        h.interpolation_hermite_tbl_ptr = tell()
        for a, b, c, d in parsed.hermite_nodes:
            wf(a); wf(b); wf(c); wf(d)

        align(8)
        h.interpolation_hermite3d_tbl_ptr = tell()
        for row in parsed.bezier3d_nodes:
            if len(row) != 8:
                raise ClipParserError("Bezier3D row must have 8 floats")
            for v in row:
                wf(v)

        if h.version <= 27:
            align(8)
            h.legacy_clip_info_tbl_ptr = tell()

        align(8)
        h.last_key_tbl_ptr = tell() if h.version <= 43 else 0
        if h.version <= 43:
            for k in parsed.last_keys:
                key_start = tell()
                assign_reference_indices(k)
                self._write_key(out, k, h.version)
                add_key_string_patch(key_start + 16, k)

        align(8)
        h.user_data_asset_info_ptr = tell() if h.version >= 62 else 0
        if h.version >= 62:
            parsed.user_data_assets = rebuilt_user_data_assets
            for ua in parsed.user_data_assets:
                write_string_ref(ua.type_ascii, False)
                write_string_ref(ua.path_unicode, True)

        # String pools
        allocate_string_patches(sorted(patch_entries, key=lambda x: x[0]))

        align(8)
        h.c8_ptr = tell()
        out.extend(c8_pool.data)

        align(8)
        h.c16_ptr = tell()
        out.extend(c16_pool.data)

        # CLIP/TML/UCurve variants do not enforce a universal 16-byte boundary before the OWORD table.
        # Keep serialization fully data-driven and preserve natural 8-byte section alignment.
        align(8)
        h.oword_ptr = tell()
        for row in rebuilt_owords:
            if len(row) != 4:
                raise ClipParserError("OWord row must have 4 floats")
            for v in row:
                wf(v)
        data_size_pos: int | None = None
        data_size_dup_pos: int | None = None
        if parsed.header.data_ptr != 0:
            align(16)
            h.data_ptr = tell()
            w64(0)
            data_size_pos = tell()
            w64(0)
            if h.version >= 62:
                # v62+ stores the total file size twice at the end of the data table.
                data_size_dup_pos = tell()
                w64(0)
        else:
            h.data_ptr = 0

        self._write_header(out, h)
        if data_size_pos is not None:
            struct.pack_into("<Q", out, data_size_pos, len(out))
        if data_size_dup_pos is not None:
            struct.pack_into("<Q", out, data_size_dup_pos, len(out))
        return bytes(out)

    @classmethod
    def _reachable_clip_infos(cls, parsed: ParsedClip) -> list:
        """Return track-owned ClipInfos in stable table order."""
        return cls._retain_reference_order(
            parsed.clip_infos,
            [clip_info for track in parsed.tracks for clip_info in track.clip_infos],
        )

    @staticmethod
    def _semantic_root_occurrences(parsed: ParsedClip) -> list:
        """Return live root occurrences, including repeated pointer values."""
        if parsed.tracks:
            return [node for track in parsed.tracks for node in track.child_nodes]
        return [] if parsed.header.version >= 85 else list(parsed.root_nodes)

    def _rebuild_tables_from_graph(self, parsed: ParsedClip):
        old_root_nodes = list(parsed.root_nodes)
        old_track_child_nodes = list(getattr(parsed, "track_child_nodes", []))
        has_clip_info_table = parsed.header.version >= 40
        has_clip_root_ranges = parsed.header.version >= 85

        track_clip_starts: dict[int, int] = {}
        if has_clip_info_table:
            ordered_clips = self._pack_contiguous_sequences(
                self._reachable_clip_infos(parsed),
                [track.clip_infos for track in parsed.tracks if track.clip_infos],
                "Track clip",
            )
            clip_index_by_id = {id(clip): index for index, clip in enumerate(ordered_clips)}
            for track in parsed.tracks:
                track_clip_starts[id(track)] = (
                    clip_index_by_id[id(track.clip_infos[0])] if track.clip_infos else 0
                )
            parsed.clip_infos = ordered_clips
        else:
            parsed.clip_infos = []

        root_node_queue = [
            node for node in self._semantic_root_occurrences(parsed)
            if node is not None
        ]

        discovered_nodes = self._grouped_reachable(
            ([root] for root in root_node_queue),
            "child_nodes",
        )
        node_candidates = self._retain_reference_order(parsed.nodes, discovered_nodes)
        nodes = self._pack_contiguous_sequences(
            node_candidates,
            [node.child_nodes for node in node_candidates if node.child_nodes],
            "Node child",
        )
        node_to_index = {id(n): i for i, n in enumerate(nodes)}
        parsed.nodes = nodes

        for node in nodes:
            node.child_offset = node_to_index[id(node.child_nodes[0])] if node.child_nodes else 0
            node.node_num = len(node.child_nodes)
        discovered_properties = self._grouped_reachable(
            (node.properties for node in nodes if node.properties),
            "child_properties",
        )
        property_candidates = self._retain_reference_order(parsed.properties, discovered_properties)
        property_sequences = [node.properties for node in nodes if node.properties]
        property_sequences.extend(
            prop.child_properties for prop in property_candidates if prop.child_properties
        )
        properties = self._pack_contiguous_sequences(
            property_candidates,
            property_sequences,
            "Property child",
        )
        parsed.properties = properties
        prop_to_index = {id(p): i for i, p in enumerate(properties)}
        for node in nodes:
            node.property_offset = prop_to_index[id(node.properties[0])] if node.properties else 0
            node.property_num = len(node.properties)

        if parsed.header.version <= 43:
            for prop in properties:
                if prop.clip_property_ref is None:
                    prop.clip_property_offset = 0
                    continue
                target = prop_to_index.get(id(prop.clip_property_ref))
                if target is None:
                    raise ClipParserError("clip_property_ref points outside the serialized property table")
                if target == 0:
                    raise ClipParserError(
                        "clip_property_ref cannot target property index 0 because zero is the null sentinel"
                    )
                prop.clip_property_offset = target

        key_sequences: dict[int, list[list]] = {0: [], 1: [], 2: [], 3: []}
        prepared_keys: dict[int, tuple[list, int]] = {}
        for prop in properties:
            if property_type_or_unknown(prop.property_type) in PROPERTY_TYPES_WITH_CHILDREN:
                continue
            ordered_keys, key_kind = self._prepare_property_key_sequence(parsed, prop)
            prepared_keys[id(prop)] = ordered_keys, key_kind
            if ordered_keys:
                key_sequences[key_kind].append(ordered_keys)
        old_key_tables = (
            parsed.main_keys, parsed.bool_keys, parsed.action_keys, parsed.no_hermite_keys
        )
        packed_key_tables = [
            self._pack_contiguous_sequences(
                self._retain_reference_order(
                    old_table,
                    [key for sequence in key_sequences[kind] for key in sequence],
                ),
                key_sequences[kind],
                "Property key",
            )
            for kind, old_table in enumerate(old_key_tables)
        ]
        parsed.main_keys, parsed.bool_keys, parsed.action_keys, parsed.no_hermite_keys = packed_key_tables
        last_keys: list = (
            self._retain_reference_order(
                parsed.last_keys,
                [prop.last_key_ref for prop in properties if prop.last_key_ref is not None],
            )
            if parsed.header.version <= 43 else []
        )
        speed_points = self._pack_contiguous_sequences(
            self._retain_reference_order(
                parsed.speed_points,
                [point for prop in properties for point in prop.speed_points_ref],
            ),
            [prop.speed_points_ref for prop in properties if prop.speed_points_ref],
            "Property speed-point",
        )
        key_index_maps = {
            kind: {id(key): index for index, key in enumerate(table)}
            for kind, table in enumerate(packed_key_tables)
        }
        last_key_index_by_id: dict[int, int] = {id(k): i for i, k in enumerate(last_keys)}
        speed_point_index_by_id: dict[int, int] = {
            id(point): index for index, point in enumerate(speed_points)
        }
        for prop in properties:
            self._emit_property_tables(
                parsed,
                prop,
                *prepared_keys.get(id(prop), ([], 0)),
                key_index_maps,
                last_key_index_by_id,
                speed_point_index_by_id,
                prop_to_index,
            )

        parsed.last_keys = last_keys
        parsed.speed_points = speed_points

        if has_clip_root_ranges:
            root_owners = [
                (clip, clip.root_nodes, clip.root_node_offset)
                for clip in parsed.clip_infos
            ]
            parsed.root_nodes, starts = self._occurrence_table(old_root_nodes, root_owners)
            for clip in parsed.clip_infos:
                clip.root_node_offset = starts[id(clip)]
                clip.root_node_count = len(clip.root_nodes)
        else:
            required_roots = (
                [node for track in parsed.tracks for node in track.child_nodes]
                if parsed.tracks else old_root_nodes
            )
            # Root and track-child pointer tables can have different ordering,
            # so retain the root occurrence order while it still represents
            # exactly the live track roots. Reconcile it when an edit changes
            # that identity multiset, even if the total count stays unchanged.
            parsed.root_nodes = (
                list(old_root_nodes)
                if self._identity_counts(old_root_nodes) == self._identity_counts(required_roots)
                else self._reconcile_occurrences(old_root_nodes, required_roots)
            )

        parsed.root_node_offsets = self._indices_for(parsed.root_nodes, node_to_index, "Root node")
        track_owners = [
            (track, track.child_nodes, track.child_node_start_index)
            for track in parsed.tracks
        ]
        parsed.track_child_nodes, starts = self._occurrence_table(
            old_track_child_nodes,
            track_owners,
        )
        for track in parsed.tracks:
            if has_clip_info_table:
                track.clip_info_offset = track_clip_starts.get(id(track), 0) if track.clip_infos else 0
                track.clip_num = len(track.clip_infos)
            else:
                track.clip_num = -1
            track.child_node_start_index = starts[id(track)]
            track.child_node_num = len(track.child_nodes)
        parsed.track_child_offsets = self._indices_for(
            parsed.track_child_nodes,
            node_to_index,
            "Track child",
        )
        if parsed.header.version >= 85:
            parsed.nodes_reorder_nodes = self._reconcile_occurrences(
                parsed.nodes_reorder_nodes, parsed.root_nodes
            )
            parsed.nodes_reorder_offsets = [
                node_to_index[id(node)] for node in parsed.nodes_reorder_nodes
            ]
        self._recalculate_interpolation_offsets_from_references(parsed)

    def _validate_graph(self, parsed: ParsedClip):
        clip_infos = self._reachable_clip_infos(parsed)
        if parsed.header.version < 40 and clip_infos:
            raise ClipParserError("Track ClipInfo relationships require CLIP v40+")
        if parsed.header.version < 85 and any(clip.root_nodes for clip in clip_infos):
            raise ClipParserError("ClipInfo root-node ranges are only serialized in v85+")
        node_roots = self._semantic_root_occurrences(parsed)
        all_nodes = self._walk_acyclic(node_roots, "child_nodes", "Node")

        property_roots = [prop for node in all_nodes for prop in node.properties]
        all_properties = self._walk_acyclic(property_roots, "child_properties", "Property")
        self._validate_counts(parsed, clip_infos, node_roots, all_nodes, all_properties)
        self._validate_single_ownership(parsed, all_nodes, all_properties)
        if parsed.header.version < 86 and any(node.dev32_id for node in all_nodes):
            raise ClipParserError("Node.dev32_id is reserved before CLIP v86")

        c8_types = {PropertyType.ENUM, PropertyType.STR8}
        c16_types = {
            PropertyType.STR16,
            PropertyType.ASSET,
            PropertyType.RESOURCE_PATH,
            PropertyType.GAME_OBJECT_REF,
            PropertyType.GUID,
        }
        if parsed.header.version < 62:
            c16_types.add(PropertyType.USER_DATA_ASSET)
        for prop in all_properties:
            if prop.data_offset:
                raise ClipParserError(
                    "Property.data_offset has no modeled target; refusing to emit a stale offset"
                )
            ptype = property_type_or_unknown(prop.property_type)
            extras = prop.extra_keys
            if parsed.header.version < 53 and extras:
                raise ClipParserError("Properties before v53 cannot own extra keys")
            if parsed.header.version > 43 and prop.last_key_ref is not None:
                raise ClipParserError("Properties after v43 cannot own legacy last-key records")
            if parsed.header.version > 43 and prop.clip_property_ref is not None:
                raise ClipParserError("Properties after v43 cannot own clip_property_ref relationships")
            if parsed.header.version >= 85 and not 0 <= prop.aux_key_flags <= 0x3:
                raise ClipParserError("Unknown aux_key_flags value")
            payload_keys = self._property_payload_keys(prop)
            if ptype in PROPERTY_TYPES_WITH_CHILDREN:
                if payload_keys:
                    raise ClipParserError("Container properties cannot also own key payloads")
                if prop.speed_points_ref:
                    raise ClipParserError("Container properties cannot own speed points")
                continue
            if prop.child_properties:
                raise ClipParserError("Only container properties can own child properties")
            if any(type(point) is not SpeedPoint for point in prop.speed_points_ref):
                raise ClipParserError("Speed-point ranges require SpeedPoint records")
            if (
                parsed.header.version < 40
                and prop.speed_points_ref
                and ptype != PropertyType.PATH_POINT3D
            ):
                raise ClipParserError("Only PathPoint3D legacy properties can own speed points")
            if parsed.header.version < 85 and any(not isinstance(key, Key) for key in payload_keys):
                raise ClipParserError("Properties before v85 require main-key table records")
            expected_width = 0 if ptype in c8_types else 1 if ptype in c16_types else None
            for key in payload_keys:
                if expected_width is not None and getattr(key, "string_is_wide", -1) != expected_width:
                    encoding = "UTF-16" if expected_width else "ASCII"
                    raise ClipParserError(f"{encoding} string property key has no modeled string payload")
                if ptype == PropertyType.USER_DATA_ASSET and parsed.header.version >= 62:
                    if getattr(key, "user_data_asset_ref", None) is None:
                        raise ClipParserError("UserDataAsset key has no referenced asset record")
                if ptype == PropertyType.PATH_POINT3D and getattr(key, "oword_ref", None) is None:
                    raise ClipParserError("PathPoint3D key has no referenced OWord record")

        interpolation_objects = [
            key for prop in all_properties for key in self._property_payload_keys(prop)
            if isinstance(key, Key)
        ] + [point for prop in all_properties for point in prop.speed_points_ref]
        for item in interpolation_objects:
            if (
                item.interpolation_type in {INTERPOLATION_TYPE_HERMITE, INTERPOLATION_TYPE_BEZIER3D}
                and item.interpolation_ref is None
            ):
                raise ClipParserError("Interpolated key has no referenced control-point record")

        if parsed.header.version >= 85:
            for track in parsed.tracks:
                clip_roots = [node for clip in track.clip_infos for node in clip.root_nodes]
                if self._identity_counts(track.child_nodes) != self._identity_counts(clip_roots):
                    raise ClipParserError(
                        "A v85+ track child occurrence must correspond to exactly one clip root occurrence"
                    )

    @staticmethod
    def _validate_counts(parsed, clip_infos, node_roots, nodes, properties) -> None:
        version = parsed.header.version
        u32_counts = (
            (len(parsed.tracks), "Track table"),
            (len(clip_infos), "ClipInfo table"),
            (len(node_roots), "Root-node occurrence table"),
            (len(nodes), "Node table"),
            (len(properties), "Property table"),
        )
        for count, label in u32_counts:
            if count > 0xFFFFFFFF:
                raise ClipParserError(f"{label} count exceeds u32")
        for track in parsed.tracks:
            if len(track.child_nodes) > 0x7FFFFFFF:
                raise ClipParserError("Track child-node count exceeds i32")
            if version >= 40 and len(track.clip_infos) > 0x7FFFFFFF:
                raise ClipParserError("Track ClipInfo count exceeds i32")
        for node in nodes:
            if len(node.child_nodes) > 0xFFFFFFFF or len(node.properties) > 0xFFFFFFFF:
                raise ClipParserError("Node child/property count exceeds u32")
        key_counts = Counter()
        key_types = (Key, BoolKey, ActionKey, NoHermiteKey)
        for prop in properties:
            ptype = property_type_or_unknown(prop.property_type)
            range_count = len(
                prop.child_properties if ptype in PROPERTY_TYPES_WITH_CHILDREN else prop.keys
            )
            if version >= 40 and range_count > 0xFFFF:
                raise ClipParserError("Property key/child count exceeds u16")
            speed_bits = 8 if version >= 43 else 16 if version >= 40 else 32
            if len(prop.speed_points_ref) >= 1 << speed_bits:
                raise ClipParserError(f"Property speed-point count exceeds u{speed_bits}")
            if ptype not in PROPERTY_TYPES_WITH_CHILDREN:
                key_counts.update(next((kind for kind in key_types if isinstance(key, kind)), None) for key in (*prop.keys, *prop.extra_keys))
        if any(key_counts[key_type] > 0xFFFFFFFF for key_type in key_types):
            raise ClipParserError("Key-table count exceeds u32")

    @classmethod
    def _validate_single_ownership(
        cls,
        parsed: ParsedClip,
        nodes: list,
        properties: list,
    ) -> None:
        """Reject aliases between live ranged-table owners; pointer aliases remain valid."""

        def claim(groups, label: str, forbidden: set[int] | None = None):
            seen: set[int] = set()
            for group in groups:
                for item in group:
                    if id(item) in seen or id(item) in (forbidden or ()):
                        raise ClipParserError(f"{label} record has more than one live owner")
                    seen.add(id(item))

        claim((track.clip_infos for track in parsed.tracks), "ClipInfo")
        claim(
            (node.child_nodes for node in nodes),
            "Child node",
            {id(node) for node in cls._semantic_root_occurrences(parsed)},
        )
        claim(
            [*(node.properties for node in nodes), *(prop.child_properties for prop in properties)],
            "Property",
        )
        claim(
            (
                cls._property_payload_keys(prop)
                for prop in properties
                if property_type_or_unknown(prop.property_type) not in PROPERTY_TYPES_WITH_CHILDREN
            ),
            "Key",
        )
        claim((prop.speed_points_ref for prop in properties), "SpeedPoint")

    @staticmethod
    def _property_payload_keys(prop) -> list:
        return [*prop.keys, *prop.extra_keys, *([prop.last_key_ref] if prop.last_key_ref else [])]

    @classmethod
    def _walk_acyclic(cls, roots: list, child_attr: str, label: str) -> list:
        result: list = []
        seen: set[int] = set()
        active: set[int] = set()

        def visit(obj):
            obj_id = id(obj)
            if obj_id in active:
                raise ClipParserError(f"{label} graph contains a cycle")
            if obj_id in seen:
                return
            seen.add(obj_id)
            active.add(obj_id)
            result.append(obj)
            for child in getattr(obj, child_attr):
                visit(child)
            active.remove(obj_id)

        for root in roots:
            visit(root)
        return result

    @staticmethod
    def _retain_reference_order(preferred: list, references: list) -> list:
        """Keep referenced rows in old order, then append new identities."""
        references = [item for item in references if item is not None]
        wanted = {id(item) for item in references}
        result: list = []
        known: set[int] = set()
        for item in preferred:
            if id(item) in wanted and id(item) not in known:
                result.append(item)
                known.add(id(item))
        for item in references:
            if id(item) not in known:
                result.append(item)
                known.add(id(item))
        return result

    @classmethod
    def _pack_contiguous_sequences(
        cls,
        preferred: list,
        sequences: list[list],
        label: str,
    ) -> list:
        """Pack disjoint owner sequences as stable physical-table blocks."""
        universe = list(preferred)
        if len({id(item) for item in universe}) != len(universe):
            raise ClipParserError(f"{label} table repeats an object")
        blocks = [sequence for sequence in sequences if sequence]
        owned = {id(item) for sequence in blocks for item in sequence}
        blocks.extend([item] for item in universe if id(item) not in owned)

        rank = {id(item): index for index, item in enumerate(universe)}
        blocks.sort(key=lambda block: min(rank[id(item)] for item in block))
        return [item for block in blocks for item in block]

    @staticmethod
    def _indices_for(items: list, index_by_id: dict[int, int], label: str) -> list[int]:
        try:
            return [index_by_id[id(item)] for item in items]
        except KeyError as error:
            raise ClipParserError(f"{label} points outside its serialized table") from error

    @staticmethod
    def _occurrence_table(old: list, owners: list[tuple[object, list, int]]) -> tuple[list, dict[int, int]]:
        """Reuse a partitioned occurrence table, otherwise concatenate owners."""
        preserve = len(old) == sum(len(sequence) for _, sequence, _ in owners)
        starts: dict[int, int] = {}
        occupied: set[int] = set()
        for owner, sequence, start in owners:
            starts[id(owner)] = start if sequence else 0
            if not sequence:
                continue
            slots = set(range(start, start + len(sequence)))
            if preserve and (
                start < 0
                or start + len(sequence) > len(old)
                or slots & occupied
                or any(old[start + index] is not item for index, item in enumerate(sequence))
            ):
                preserve = False
            occupied.update(slots)
        if preserve:
            return list(old), starts

        table: list = []
        for owner, sequence, _ in owners:
            starts[id(owner)] = len(table) if sequence else 0
            table.extend(sequence)
        return table, starts

    @staticmethod
    def _identity_counts(items: list) -> Counter:
        return Counter(map(id, items))

    @staticmethod
    def _reconcile_occurrences(preferred: list, required: list) -> list:
        remaining = Counter(map(id, required))
        result: list = []
        for item in [*preferred, *required]:
            item_id = id(item)
            if remaining[item_id]:
                result.append(item)
                remaining[item_id] -= 1
        return result

    @classmethod
    def _grouped_reachable(cls, groups, child_attr: str) -> list:
        """Collect each sibling group before recursively visiting descendants."""
        result: list = []
        seen: set[int] = set()

        def visit(group):
            group = list(group)
            for item in group:
                if id(item) not in seen:
                    seen.add(id(item))
                    result.append(item)
            for item in group:
                children = getattr(item, child_attr)
                if children:
                    visit(children)

        for group in groups:
            visit(group)
        return result

    @staticmethod
    def _prepare_property_key_sequence(parsed: ParsedClip, prop) -> tuple[list, int]:
        prop.key_num_or_element_num = len(prop.keys)
        extras = prop.extra_keys
        if parsed.header.version < 53:
            if extras:
                raise ClipParserError("Properties before v53 cannot own extra keys")
            prop.extra_key_flags = 0
        else:
            if len(extras) > 4:
                raise ClipParserError("Properties support at most four extra-key flags")
            flags = prop.extra_key_flags & 0xF
            prop.extra_key_flags = flags if flags.bit_count() == len(extras) else (1 << len(extras)) - 1

        ordered_keys = [*prop.keys, *extras]
        if (
            parsed.header.version < 85
            and property_type_or_unknown(prop.property_type) == PropertyType.ACTION
        ):
            for key in ordered_keys:
                if not isinstance(key, Key):
                    raise ClipParserError("Legacy Action properties require normal Key records")
                key.raw0, key.raw1 = 1, 0
        key_kind = prop.aux_key_flags if parsed.header.version >= 85 else 0
        if parsed.header.version >= 85 and ordered_keys:
            key_kind = next((
                kind for kind, key_type in enumerate((Key, BoolKey, ActionKey, NoHermiteKey))
                if all(isinstance(key, key_type) for key in ordered_keys)
            ), -1)
            if key_kind < 0:
                raise ClipParserError("A property cannot mix key-table record types")
        prop.aux_key_flags = key_kind if parsed.header.version >= 85 else 0
        return ordered_keys, key_kind

    def _emit_property_tables(
        self,
        parsed: ParsedClip,
        prop,
        ordered_keys: list,
        key_kind: int,
        key_index_maps: dict[int, dict[int, int]],
        last_key_index_by_id: dict[int, int],
        speed_point_index_by_id: dict[int, int],
        property_index_by_id: dict[int, int],
    ):
        ptype = property_type_or_unknown(prop.property_type)
        if ptype in PROPERTY_TYPES_WITH_CHILDREN:
            prop.key_num_or_element_num = len(prop.child_properties)
            prop.key_or_child_offset = (
                property_index_by_id[id(prop.child_properties[0])]
                if prop.child_properties else 0
            )
            prop.speed_point_num = 0
            prop.speed_point_offset = 0
            prop.is_exist_last_key = 0
            prop.last_key_offset = 0
            return

        prop.key_or_child_offset = (
            key_index_maps[key_kind][id(ordered_keys[0])] if ordered_keys else 0
        )

        if parsed.header.version <= 43:
            has_last_key = prop.last_key_ref is not None
            prop.last_key_offset = last_key_index_by_id[id(prop.last_key_ref)] if has_last_key else 0
            prop.is_exist_last_key = int(has_last_key)

        prop.speed_point_num = len(prop.speed_points_ref)
        prop.speed_point_offset = (
            speed_point_index_by_id[id(prop.speed_points_ref[0])] if prop.speed_points_ref else 0
        )

    def _recalculate_interpolation_offsets_from_references(self, parsed: ParsedClip):
        objects = [*parsed.main_keys, *parsed.last_keys, *parsed.speed_points]
        tables = {}
        for interpolation_type, attr in (
            (INTERPOLATION_TYPE_HERMITE, "hermite_nodes"),
            (INTERPOLATION_TYPE_BEZIER3D, "bezier3d_nodes"),
        ):
            references = [
                item.interpolation_ref
                for item in objects
                if item.interpolation_type == interpolation_type and item.interpolation_ref is not None
            ]
            table = self._retain_reference_order(getattr(parsed, attr), references)
            setattr(parsed, attr, table)
            tables[interpolation_type] = {id(row): index for index, row in enumerate(table)}

        for item in objects:
            table = tables.get(item.interpolation_type)
            if table is None:
                item.interpolation_ref = None
                item.interpolation_offset = 0
            elif item.interpolation_ref is None:
                item.interpolation_offset = 0
            else:
                item.interpolation_offset = table[id(item.interpolation_ref)]

    def _recalculate_string_hashes(self, parsed: ParsedClip):
        if parsed.header.version <= 27:
            for item in [*parsed.nodes, *parsed.properties]:
                item.name_hash = item.unique32_id = item.unicode_name_hash = 0
            return
        for n in parsed.nodes:
            if parsed.header.version < 86:
                n.name_hash = murmur3_hash((n.name or "").encode("utf-8"))
            n.unicode_name_hash = murmur3_hash((n.name or "").encode("utf-16le"))
        for p in parsed.properties:
            if parsed.header.version < 86:
                p.name_hash = murmur3_hash((p.name or "").encode("utf-8"))
            unicode_name = (
                p.name if parsed.header.version == 34 else
                (p.legacy_unicode_name if parsed.header.version < 40 else p.name)
            )
            p.unicode_name_hash = murmur3_hash((unicode_name or "").encode("utf-16le"))

    def _write_key(self, out: bytearray, k, version: int):
        out.extend(struct.pack("<f", k.frame))
        out.extend(struct.pack("<f", k.rate))
        packed = (k.interpolation_type & 0xFF) | ((k.offset_frame_flag & 0x1) << 8) | ((k.reserved & 0x7FFFFF) << 9)
        out.extend(struct.pack("<I", packed))
        if version < 53:
            out.extend(b"\x00" * 4)
        else:
            out.extend(struct.pack("<I", k.frame_span))
        out.extend(struct.pack("<I", k.raw0))
        out.extend(struct.pack("<I", k.raw1))
        out.extend(struct.pack("<Q", k.interpolation_offset))
        if key_size(version) == 40:
            out.extend(b"\x00" * 8)

    def _write_header(self, out: bytearray, h):
        o = 0
        for kind, value in (("u32", h.magic), ("u32", h.version)):
            o = self._write_header_field(out, o, kind, value)
        for kind, attr in iter_header_fields(h.version):
            o = self._write_header_field(out, o, kind, getattr(h, attr) if attr else 0)

    @staticmethod
    def _write_header_field(out: bytearray, offset: int, kind: str, value) -> int:
        if kind == "bytes16":
            if len(value) != 16:
                raise ClipParserError("Header bytes16 field must be exactly 16 bytes")
            out[offset:offset + 16] = value
        else:
            struct.pack_into(_HEADER_PACK_FORMATS[kind], out, offset, value)
        return offset + _HEADER_PACK_SIZES[kind]
