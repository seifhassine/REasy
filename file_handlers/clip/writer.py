from __future__ import annotations

import struct
from copy import copy

from .enums import CLIP_MAGIC, PROPERTY_TYPES_WITH_CHILDREN, PropertyType, property_type_or_unknown
from .parser import (
    EXTRA_KEY_REF_ATTRS,
    ParsedClip,
    header_size,
    iter_header_fields,
    key_size,
    node_size,
    prop_size,
)
from .reader import ClipParserError
from .structures import ActionKey, BoolKey, Key, NoHermiteKey
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

    def add(self, s: str, dedupe: bool = True) -> int:
        if dedupe:
            existing = self._offsets.get(s)
            if existing is not None:
                return existing
        off = len(self.data) // 2 if self.wide else len(self.data)
        enc = s.encode("utf-16le" if self.wide else "utf-8")
        self.data.extend(enc)
        self.data.extend(b"\x00\x00" if self.wide else b"\x00")
        if dedupe:
            self._offsets[s] = off
        return off


class ClipWriter:
    def build(self, parsed: ParsedClip) -> bytes:
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

        c8_pool = _StringPool(False)
        c16_pool = _StringPool(True)
        patch_entries: list[tuple[int, str, bool]] = []
        rebuilt_user_data_assets: list = []
        rebuilt_user_data_asset_index_by_id: dict[int, int] = {}
        rebuilt_owords: list[tuple[float, float, float, float]] = []
        rebuilt_oword_index_by_id: dict[int, int] = {}

        def add_patch_groups(*groups: list[tuple[int, str, bool]]):
            for group in groups:
                patch_entries.extend(group)

        def add_key_string_patch(target: list[tuple[int, str, bool]], pos: int, key_obj):
            if (
                getattr(key_obj, "raw0", None) == 0
                and getattr(key_obj, "raw1", None) == 0
                and key_obj.string_original_value is not None
                and key_obj.string_value == key_obj.string_original_value
            ):
                return
            if key_obj.string_is_wide == 1:
                target.append((pos, key_obj.string_value, True))
            elif key_obj.string_is_wide == 0:
                target.append((pos, key_obj.string_value, False))

        def write_string_ref(target: list[tuple[int, str, bool]], value: str, wide: bool):
            target.append((tell(), value, wide))
            w64(0)

        def index_ref(ref, table: list, index_by_id: dict[int, int]) -> int | None:
            if ref is None:
                return None
            rid = id(ref)
            if rid not in index_by_id:
                index_by_id[rid] = len(table)
                table.append(ref)
            return index_by_id[rid]

        def assign_user_data_asset_index_if_present(key_obj):
            if h.version < 62:
                return
            idx = index_ref(key_obj.user_data_asset_ref, rebuilt_user_data_assets, rebuilt_user_data_asset_index_by_id)
            if idx is None:
                return
            key_obj.user_data_asset_index = idx
            key_obj.raw0 = idx & 0xFFFFFFFF
            key_obj.raw1 = (idx >> 32) & 0xFFFFFFFF

        def assign_oword_index_if_present(key_obj):
            idx = index_ref(getattr(key_obj, "oword_ref", None), rebuilt_owords, rebuilt_oword_index_by_id)
            if idx is not None:
                key_obj.raw0 = idx & 0xFFFFFFFF

        def patch_string_pool(
            pool: _StringPool,
            wide: bool,
            sorted_entries: list[tuple[int, str, bool]],
        ):
            dedupe = h.version >= 34
            for pos, s, is_wide in sorted_entries:
                if is_wide == wide:
                    rel = pool.add(s, dedupe=dedupe)
                    struct.pack_into("<Q", out, pos, rel)
            out.extend(pool.data)

        # Root table (patched once node table position is known)
        align(8)
        h.root_node_tbl_ptr = tell()
        root_table_patch_pos = tell()
        for _ in parsed.root_node_offsets:
            w64(0)

        # Tracks
        align(8)
        h.track_tbl_ptr = tell()
        track_string_patches: list[tuple[int, str, bool]] = []
        for tr in parsed.tracks:
            out.extend(struct.pack("<B3x", tr.enable & 0xFF))
            w32(tr.reserved)
            out.extend(struct.pack("<i", tr.clip_num))
            out.extend(struct.pack("<i", tr.child_node_num))
            write_string_ref(track_string_patches, tr.type_ascii, False)
            write_string_ref(track_string_patches, tr.type_unicode, True)
            write_string_ref(track_string_patches, tr.group_name, True)
            if h.version >= 40:
                w64(tr.clip_info_offset)
                w64(tr.child_node_start_index)
            else:
                w64(tr.child_node_start_index)

        # ClipInfo
        align(8)
        h.clip_info_tbl_ptr = tell() if h.version >= 40 else 0
        clip_info_name_patches: list[tuple[int, str, bool]] = []
        if h.version >= 40:
            for ci in parsed.clip_infos:
                wf(ci.frame_in); wf(ci.frame_out); wf(ci.source_in); wf(ci.source_out)
                if h.version >= 85:
                    w64(ci.root_node_count)
                    write_string_ref(clip_info_name_patches, ci.unicode_name, True)
                    w64(ci.root_node_offset)
                else:
                    write_string_ref(clip_info_name_patches, ci.unicode_name, True)

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
        node_name_patches: list[tuple[int, str, bool]] = []
        node_tag_patches: list[tuple[int, str, bool]] = []
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
                    bits |= (n.route_to_secondary_property_list & 0x1) << 28
                    bits |= (n.reserved_3bits & 0x7) << 29
                else:
                    bits |= (n.padding_v53 & 0xFF) << 24
            else:
                bits = n.node_type & 0xFF
            struct.pack_into("<I", rec, p, bits); p += 4
            struct.pack_into("<I", rec, p, n.dev32_id); p += 4
            struct.pack_into("<I", rec, p, n.unique32_id if h.version >= 86 else n.name_hash)
            struct.pack_into("<I", rec, p + 4, n.unicode_name_hash)

            name_wide = h.version != 27
            node_name_patches.append((tell() + p + 8, n.name, name_wide))
            if h.version != 34:
                node_tag_patches.append((tell() + p + 16, n.node_tag, True))
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
        prop_name_patches: list[tuple[int, str, bool]] = []
        legacy_prop_unicode_name_patches: list[tuple[int, str, bool]] = []
        for p in parsed.properties:
            rec = bytearray(prop_size(h.version))
            if h.version >= 40:
                struct.pack_into("<f", rec, 0, p.begin_frame)
                struct.pack_into("<f", rec, 4, p.end_frame)
                struct.pack_into("<I", rec, 8, p.unique32_id if h.version >= 86 else p.name_hash)
                struct.pack_into("<I", rec, 12, p.unicode_name_hash)
                prop_name_patches.append((tell() + 16, p.name, False))
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
                    fb1 = p.legacy_flags_raw & 0xFF
                    fb1 = (fb1 & ~0xF3) | (
                        (p.is_enum_closed & 1)
                        | ((p.set_after_end_frame & 1) << 1)
                        | ((p.is_set_delegate_enable & 1) << 4)
                        | ((p.is_prev_diff_frame_set & 1) << 5)
                        | ((p.is_next_diff_frame_set & 1) << 6)
                        | ((p.is_prev_key_value_set & 1) << 7)
                    )
                    struct.pack_into("<B", rec, prop_type_off + 1, fb1)
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
                        | ((p.is_delayed_execution_or_array_count_set & 1) << 6)
                    )
                    if h.version >= 85:
                        fb1 |= (p.has_set_property_delegate & 1) << 7
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
                legacy_fb1 = p.legacy_flags_raw & 0xFF
                legacy_fb1 = (legacy_fb1 & ~0xF3) | (
                    (p.is_enum_closed & 1)
                    | ((p.set_after_end_frame & 1) << 1)
                    | ((p.is_set_delegate_enable & 1) << 4)
                    | ((p.is_prev_diff_frame_set & 1) << 5)
                    | ((p.is_next_diff_frame_set & 1) << 6)
                    | ((p.is_prev_key_value_set & 1) << 7)
                )
                struct.pack_into("<I", rec, 20, (p.property_type & 0xFF) | ((legacy_fb1 & 0xFF) << 8))
                struct.pack_into("<I", rec, 24, p.name_hash)
                struct.pack_into("<I", rec, 28, p.unicode_name_hash)
                prop_name_patches.append((tell() + 32, p.name, False))
                legacy_prop_unicode_name_patches.append((tell() + 40, p.legacy_unicode_name, True))
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
        key_string_patches: list[tuple[int, str, bool]] = []
        last_key_string_patches: list[tuple[int, str, bool]] = []
        for k in parsed.main_keys:
            key_start = tell()
            assign_user_data_asset_index_if_present(k)
            assign_oword_index_if_present(k)
            self._write_key(out, k, h.version)
            add_key_string_patch(key_string_patches, key_start + 16, k)

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
                w32((k.interpolation_type & 0xFF) | ((k.reserved & 0xFFFFFF) << 8))

        align(8)
        h.no_hermite_keys_offset = tell() if h.version >= 85 else 0
        if h.version >= 85:
            for k in parsed.no_hermite_keys:
                key_start = tell()
                assign_user_data_asset_index_if_present(k)
                assign_oword_index_if_present(k)
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
                add_key_string_patch(key_string_patches, key_start + 8, k)

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
                self._write_key(out, k, h.version)
                add_key_string_patch(last_key_string_patches, key_start + 16, k)

        align(8)
        h.user_data_asset_info_ptr = tell() if h.version >= 62 else 0
        user_data_patches: list[tuple[int, str, bool]] = []
        if h.version >= 62:
            parsed.user_data_assets = rebuilt_user_data_assets
            for ua in parsed.user_data_assets:
                write_string_ref(user_data_patches, ua.type_ascii, False)
                write_string_ref(user_data_patches, ua.path_unicode, True)

        # String pools
        add_patch_groups(
            track_string_patches,
            clip_info_name_patches,
            user_data_patches,
            key_string_patches,
            last_key_string_patches,
            node_name_patches,
            node_tag_patches,
            prop_name_patches,
            legacy_prop_unicode_name_patches,
        )
        sorted_patch_entries = sorted(patch_entries, key=lambda x: x[0])

        align(8)
        h.c8_ptr = tell()
        patch_string_pool(c8_pool, False, sorted_patch_entries)

        align(8)
        h.c16_ptr = tell()
        patch_string_pool(c16_pool, True, sorted_patch_entries)

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

    def _rebuild_tables_from_graph(self, parsed: ParsedClip):
        deleted_node_ids = getattr(parsed, "_deleted_node_ids", set())
        deleted_property_ids = getattr(parsed, "_deleted_property_ids", set())
        has_clip_info_table = parsed.header.version >= 40
        has_clip_root_ranges = parsed.header.version >= 85
        ordered_clips: list = []
        if has_clip_info_table:
            seen_clips: set[int] = set()
            for tr in parsed.tracks:
                for ci in tr.clip_infos:
                    ci_id = id(ci)
                    if ci_id in seen_clips:
                        continue
                    seen_clips.add(ci_id)
                    ordered_clips.append(ci)
            for ci in parsed.clip_infos:
                ci_id = id(ci)
                if ci_id not in seen_clips:
                    seen_clips.add(ci_id)
                    ordered_clips.append(ci)
            parsed.clip_infos = ordered_clips

        root_node_queue: list = []
        if has_clip_root_ranges:
            for ci in parsed.clip_infos:
                for n in ci.root_nodes:
                    if n is not None:
                        root_node_queue.append(n)
        else:
            root_node_queue.extend([n for n in parsed.root_nodes if n is not None])
        extra_node_queue: list = []
        for tr in parsed.tracks:
            extra_node_queue.extend([n for n in tr.child_nodes if n is not None])
        extra_node_queue.extend([n for n in parsed.nodes_reorder_nodes if n is not None])

        discovered_nodes: list = []
        node_to_index: dict[int, int] = {}
        self._emit_nodes_by_occurrence(root_node_queue, discovered_nodes, node_to_index)
        self._emit_nodes_by_occurrence(extra_node_queue, discovered_nodes, node_to_index)
        nodes = [n for n in discovered_nodes if id(n) not in deleted_node_ids]
        node_to_index = {id(n): i for i, n in enumerate(nodes)}
        parsed.nodes = nodes

        discovered_properties: list = []
        prop_to_index: dict[int, int] = {}
        for node in nodes:
            node.property_offset = 0
            node.property_num = len(node.properties)
            node.child_offset = 0
            node.node_num = 0
            child_indices: list[int] = []
            for child in node.child_nodes:
                idx = node_to_index.get(id(child))
                if idx is None:
                    continue
                child_indices.append(idx)
            if child_indices:
                node.child_offset = child_indices[0]
                node.node_num = len(child_indices)
        property_node_order: list = []
        seen_prop_nodes: set[int] = set()

        def walk_for_properties(node_obj):
            nid = id(node_obj)
            if nid in seen_prop_nodes:
                return
            seen_prop_nodes.add(nid)
            property_node_order.append(node_obj)
            for child_obj in node_obj.child_nodes:
                walk_for_properties(child_obj)

        for root in root_node_queue:
            walk_for_properties(root)
        for root in extra_node_queue:
            walk_for_properties(root)

        for node in property_node_order:
            if not node.properties:
                continue
            self._emit_properties_by_occurrence(node.properties, discovered_properties, prop_to_index)
        properties = [p for p in discovered_properties if id(p) not in deleted_property_ids]
        parsed.properties = properties
        prop_to_index = {id(p): i for i, p in enumerate(properties)}
        for node in nodes:
            prop_indices = [prop_to_index[id(p)] for p in node.properties if id(p) in prop_to_index]
            if prop_indices:
                node.property_offset = prop_indices[0]
                node.property_num = len(prop_indices)
            else:
                node.property_offset = 0
                node.property_num = 0

        main_keys: list = []
        bool_keys: list = []
        action_keys: list = []
        no_hermite_keys: list = []
        last_keys: list = []
        speed_points: list = []
        key_index_maps = {0: {}, 1: {}, 2: {}, 3: {}}
        last_key_index_by_id: dict[int, int] = {}
        for prop in properties:
            self._emit_property_tables(
                parsed,
                prop,
                main_keys,
                bool_keys,
                action_keys,
                no_hermite_keys,
                last_keys,
                speed_points,
                key_index_maps=key_index_maps,
                last_key_index_by_id=last_key_index_by_id,
            )
        parsed.main_keys = main_keys
        parsed.bool_keys = bool_keys
        parsed.action_keys = action_keys
        parsed.no_hermite_keys = no_hermite_keys
        if parsed.header.version <= 43:
            for i, k in enumerate(last_keys):
                if k is None:
                    last_keys[i] = Key()
        parsed.last_keys = last_keys
        parsed.speed_points = speed_points

        if has_clip_root_ranges:
            root_node_offsets: list[int] = []
            root_node_refs: list = []
            root_ref_counts: dict[int, int] = {}

            def append_root_node(n) -> bool:
                idx = node_to_index.get(id(n))
                if idx is None:
                    return False
                root_node_offsets.append(idx)
                root_node_refs.append(n)
                root_ref_counts[id(n)] = root_ref_counts.get(id(n), 0) + 1
                return True

            for ci in parsed.clip_infos:
                ci.root_node_offset = len(root_node_offsets) if ci.root_nodes else 0
                start_count = len(root_node_offsets)
                for n in ci.root_nodes:
                    append_root_node(n)
                ci.root_node_count = len(root_node_offsets) - start_count
                if ci.root_node_count == 0:
                    ci.root_node_offset = 0
            seen_track_refs: dict[int, int] = {}
            for tr in parsed.tracks:
                for n in tr.child_nodes:
                    nid = id(n)
                    seen_track_refs[nid] = seen_track_refs.get(nid, 0) + 1
                    if seen_track_refs[nid] > root_ref_counts.get(nid, 0):
                        append_root_node(n)
            parsed.root_node_offsets = root_node_offsets
            parsed.root_nodes = root_node_refs
        else:
            parsed.root_node_offsets = [node_to_index[id(n)] for n in parsed.root_nodes if id(n) in node_to_index]

        clip_to_index = {id(ci): i for i, ci in enumerate(parsed.clip_infos)} if has_clip_info_table else {}
        track_child_offsets: list[int] = []
        for tr in parsed.tracks:
            if has_clip_info_table:
                clip_indices = [clip_to_index[id(ci)] for ci in tr.clip_infos if id(ci) in clip_to_index]
                tr.clip_info_offset = clip_indices[0] if clip_indices else 0
                tr.clip_num = len(clip_indices)
            tr.child_node_start_index = len(track_child_offsets)
            tr.child_node_num = len(tr.child_nodes)
            for n in tr.child_nodes:
                idx = node_to_index.get(id(n))
                if idx is not None:
                    track_child_offsets.append(idx)
        parsed.track_child_offsets = track_child_offsets
        if parsed.header.version >= 85:
            available_reorder_counts: dict[int, int] = {}
            for n in parsed.root_nodes:
                if id(n) in node_to_index:
                    available_reorder_counts[id(n)] = available_reorder_counts.get(id(n), 0) + 1
            ordered_reorder_nodes = []
            for n in parsed.nodes_reorder_nodes:
                nid = id(n)
                if available_reorder_counts.get(nid, 0) > 0:
                    ordered_reorder_nodes.append(n)
                    available_reorder_counts[nid] -= 1
            for n in parsed.root_nodes:
                nid = id(n)
                if available_reorder_counts.get(nid, 0) > 0:
                    ordered_reorder_nodes.append(n)
                    available_reorder_counts[nid] -= 1
            parsed.nodes_reorder_nodes = ordered_reorder_nodes
            parsed.nodes_reorder_offsets = [node_to_index[id(n)] for n in ordered_reorder_nodes]
        self._recalculate_interpolation_offsets_from_references(parsed)

    def _emit_node(self, node, out_nodes: list, node_to_index: dict[int, int]):
        return self._emit_unique(node, out_nodes, node_to_index)

    def _emit_nodes_by_occurrence(self, roots: list, out_nodes: list, node_to_index: dict[int, int]):
        for root in roots:
            self._emit_node_group([root], out_nodes, node_to_index)

    def _emit_node_group(self, group: list, out_nodes: list, node_to_index: dict[int, int]):
        for node in group:
            self._emit_node(node, out_nodes, node_to_index)
        for node in group:
            if node.child_nodes:
                self._emit_node_group(node.child_nodes, out_nodes, node_to_index)

    def _emit_property(self, prop, out_props: list, prop_to_index: dict[int, int]):
        return self._emit_unique(prop, out_props, prop_to_index)

    @staticmethod
    def _emit_unique(obj, out_items: list, index_by_id: dict[int, int]):
        obj_id = id(obj)
        if obj_id in index_by_id:
            return False
        index_by_id[obj_id] = len(out_items)
        out_items.append(obj)
        return True

    def _emit_properties_by_occurrence(self, roots: list, out_props: list, prop_to_index: dict[int, int]):
        for prop in roots:
            self._emit_property(prop, out_props, prop_to_index)
        for prop in roots:
            if prop.child_properties:
                self._emit_properties_by_occurrence(prop.child_properties, out_props, prop_to_index)

    def _emit_property_tables(
        self,
        parsed: ParsedClip,
        prop,
        main_keys: list,
        bool_keys: list,
        action_keys: list,
        no_hermite_keys: list,
        last_keys: list,
        speed_points: list,
        key_index_maps: dict[int, dict[int, int]] | None = None,
        last_key_index_by_id: dict[int, int] | None = None,
    ):
        ptype = property_type_or_unknown(prop.property_type)
        if ptype in PROPERTY_TYPES_WITH_CHILDREN:
            prop.key_or_child_offset = 0
            prop.key_num_or_element_num = len(prop.child_properties)
            if prop.child_properties:
                first = next((i for i, p in enumerate(parsed.properties) if p is prop.child_properties[0]), 0)
                prop.key_or_child_offset = first
            prop.children = []
            return
        prop.key_num_or_element_num = len(prop.keys)
        ordered_keys = list(prop.keys)
        if parsed.header.version >= 53:
            prop.extra_key_flags = 0
            for bit, attr in EXTRA_KEY_REF_ATTRS:
                ref = getattr(prop, attr)
                if ref is not None:
                    prop.extra_key_flags |= bit
                    ordered_keys.append(ref)
        else:
            prop.extra_key_flags = 0

        if ordered_keys:
            key_table = main_keys
            key_kind = 0
            prop.aux_key_flags = 0
            if parsed.header.version >= 85:
                if all(isinstance(k, BoolKey) for k in ordered_keys):
                    key_table = bool_keys
                    key_kind = 1
                    prop.aux_key_flags = 1
                elif all(isinstance(k, ActionKey) for k in ordered_keys):
                    key_table = action_keys
                    key_kind = 2
                    prop.aux_key_flags = 2
                elif all(isinstance(k, NoHermiteKey) for k in ordered_keys):
                    key_table = no_hermite_keys
                    key_kind = 3
                    prop.aux_key_flags = 3
            key_index_map = key_index_maps[key_kind] if key_index_maps is not None else {}
            existing_indices = [key_index_map.get(id(key)) for key in ordered_keys]
            contiguous_existing = (
                all(idx is not None for idx in existing_indices)
                and existing_indices == list(range(existing_indices[0], existing_indices[0] + len(existing_indices)))
            )
            if contiguous_existing:
                prop.key_or_child_offset = existing_indices[0]
            else:
                prop.key_or_child_offset = len(key_table)
                for key in ordered_keys:
                    key_table.append(copy(key))
                    key_index_map[id(key)] = len(key_table) - 1
        else:
            prop.key_or_child_offset = 0

        if parsed.header.version <= 43:
            if prop.last_key_ref is not None:
                lk_id = id(prop.last_key_ref)
                existing = last_key_index_by_id.get(lk_id) if last_key_index_by_id is not None else None
                if existing is None:
                    desired = prop.last_key_offset if prop.last_key_offset >= 0 else len(last_keys)
                    if desired < len(last_keys) and last_keys[desired] is not None:
                        desired = len(last_keys)
                    if desired >= len(last_keys):
                        last_keys.extend([None] * (desired - len(last_keys) + 1))
                    last_keys[desired] = copy(prop.last_key_ref)
                    existing = desired
                    if last_key_index_by_id is not None:
                        last_key_index_by_id[lk_id] = existing
                prop.last_key_offset = existing
                prop.is_exist_last_key = 1
            else:
                prop.last_key_offset = 0
                prop.is_exist_last_key = 0

        prop.speed_point_offset = len(speed_points)
        prop.speed_point_num = len(prop.speed_points_ref)
        for sp in prop.speed_points_ref:
            speed_points.append(sp)

    def _recalculate_interpolation_offsets_from_references(self, parsed: ParsedClip):
        parsed.hermite_nodes = []
        parsed.bezier3d_nodes = []
        tables = {
            INTERPOLATION_TYPE_HERMITE: (parsed.hermite_nodes, {}),
            INTERPOLATION_TYPE_BEZIER3D: (parsed.bezier3d_nodes, {}),
        }

        def _recalculate(obj):
            ref = getattr(obj, "interpolation_ref", None)
            if ref is None:
                obj.interpolation_offset = 0
                return
            table_info = tables.get(obj.interpolation_type)
            if table_info is None:
                return
            table, index_by_id = table_info
            rid = id(ref)
            if rid not in index_by_id:
                index_by_id[rid] = len(table)
                table.append(ref)
            obj.interpolation_offset = index_by_id[rid]

        for k in parsed.main_keys:
            _recalculate(k)
        for k in parsed.last_keys:
            _recalculate(k)
        for sp in parsed.speed_points:
            _recalculate(sp)

    def _recalculate_string_hashes(self, parsed: ParsedClip):
        if parsed.header.version <= 27:
            for n in parsed.nodes:
                n.name_hash = 0
                n.unique32_id = 0
                n.unicode_name_hash = 0
            for p in parsed.properties:
                p.name_hash = 0
                p.unique32_id = 0
                p.unicode_name_hash = 0
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
            if k.reserved2 != 0:
                raise ClipParserError("nonzero reserved2 in key")
            out.extend(struct.pack("<I", k.reserved2))
        else:
            out.extend(struct.pack("<I", k.frame_span))
        out.extend(struct.pack("<I", k.raw0))
        out.extend(struct.pack("<I", k.raw1))
        out.extend(struct.pack("<Q", k.interpolation_offset))
        if key_size(version) == 40:
            tail = k.legacy_tail_raw if isinstance(k.legacy_tail_raw, (bytes, bytearray)) else b""
            if len(tail) != 8:
                raise ClipParserError("Legacy key tail must be exactly 8 bytes")
            out.extend(tail)

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
