from __future__ import annotations

import struct

from utils.hash_util import murmur3_hash

from ..binary import ReadContext, align_up
from ..errors import MotionParseError
from ..profiles import MotionFormatProfile
from .model import (
    MotTree,
    MotionIdRemap,
    TreeLink,
    TreeLinkType,
    TreeNode,
    TreeNodeType,
    TreeParameter,
    TreeParameterType,
    TreeTag,
)
from .validator import MotTreeV4Validator


class MotTreeV4Parser:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot_tree=4)
        self.profile = profile
        self.validator = MotTreeV4Validator(profile)

    def parse(self, context: ReadContext, base: int, physical_end: int) -> MotTree:
        c = context.subcontext(base, physical_end, label=f"MotTree@0x{base:X}", object_base=base)
        c.require(base, 0x50, "MotTree header")
        if c.u32(base) != self.profile.mot_tree.version or c.bytes(base + 4, 4) != b"mtre":
            raise MotionParseError(f"{c.label}: expected MotTree v{self.profile.mot_tree.version}")
        if c.u32(base + 8) or c.u32(base + 0xC):
            raise MotionParseError(f"{c.label}: unsupported MotTree error/master state")
        if c.u64(base + 0x10) or c.u64(base + 0x18):
            raise MotionParseError(f"{c.label}: v4 resources/user variables are unsupported")
        node_stored = c.u64(base + 0x20)
        link_stored = c.u64(base + 0x28)
        name_stored = c.u64(base + 0x30)
        remap_stored = c.u64(base + 0x38)
        node_count = c.u16(base + 0x40)
        link_count = c.u16(base + 0x42)
        root_index = c.u16(base + 0x44)
        if c.u16(base + 0x46):
            raise MotionParseError(f"{c.label}: v4 resources are unsupported")
        remap_count = c.u16(base + 0x48)
        if c.u16(base + 0x4A) or c.u16(base + 0x4C) or c.u16(base + 0x4E):
            raise MotionParseError(f"{c.label}: nonzero v4 header reserved fields")

        cursor = base + 0x50
        remaps = []
        if remap_count:
            if base + remap_stored != cursor:
                raise MotionParseError(f"{c.label}: motion-ID remap table violates writer cursor")
            c.require(cursor, remap_count * 4, "motion-ID remaps")
            for index in range(remap_count):
                remaps.append(MotionIdRemap(c.i16(cursor + index * 4), c.i16(cursor + index * 4 + 2)))
            cursor += remap_count * 4
        elif remap_stored:
            raise MotionParseError(f"{c.label}: empty remap table has a pointer")
        remap_end = align_up(cursor, 8)
        c.require_zero(cursor, remap_end, "motion-ID remap padding")
        cursor = remap_end
        if base + name_stored != cursor:
            raise MotionParseError(f"{c.label}: MotTree name violates writer cursor")
        name, cursor = c.utf16_z(cursor, "MotTree name")
        expected_nodes = align_up(cursor, 8)
        c.require_zero(cursor, expected_nodes, "MotTree node-table alignment")
        node_table = base + node_stored
        if node_table != expected_nodes:
            raise MotionParseError(f"{c.label}: node table violates writer cursor")
        c.require(node_table, node_count * 0x38, "MotTree node table")

        raw_nodes = []
        cursor = node_table + node_count * 0x38
        for index in range(node_count):
            record = node_table + index * 0x38
            raw_nodes.append(
                {
                    "class": c.u64(record),
                    "name": c.u64(record + 8),
                    "tags": c.u64(record + 0x10),
                    "tag_hashes": c.u64(record + 0x18),
                    "params": c.u64(record + 0x20),
                    "id": c.u32(record + 0x28),
                    "class_hash": c.u32(record + 0x2C),
                    "name_hash": c.u32(record + 0x30),
                    "tag_count": c.u8(record + 0x34),
                    "param_count": c.u8(record + 0x35),
                    "node_type": c.u8(record + 0x36),
                    "flags": c.u8(record + 0x37),
                }
            )
        class_names = []
        for raw in raw_nodes:
            if base + raw["class"] != cursor:
                raise MotionParseError(f"{c.label}: class-name strings violate writer cursor")
            value, cursor = c.ascii_z(cursor, "MotTree class name")
            class_names.append(value)

        raw_tag_tables: list[tuple[int, int] | None] = []
        for raw in raw_nodes:
            count = raw["tag_count"]
            if not count:
                if raw["tags"] or raw["tag_hashes"]:
                    raise MotionParseError(f"{c.label}: empty node tag table has pointers")
                raw_tag_tables.append(None)
                continue
            table = base + raw["tags"]
            hash_table = base + raw["tag_hashes"]
            aligned_table = align_up(cursor, 8)
            c.require_zero(cursor, aligned_table, "MotTree tag-table alignment")
            cursor = aligned_table
            if table != cursor or hash_table != table + count * 8:
                raise MotionParseError(
                    f"{c.label}: node tag tables 0x{table:X}/0x{hash_table:X} "
                    f"do not match writer cursor 0x{cursor:X}"
                )
            cursor = hash_table + count * 4
            raw_tag_tables.append((table, hash_table))

        if any(table_info is not None for table_info in raw_tag_tables):
            aligned_tag_strings = align_up(cursor, 8)
            c.require_zero(cursor, aligned_tag_strings, "MotTree tag-string alignment")
            cursor = aligned_tag_strings
        tags_by_node: list[list[TreeTag]] = []
        for raw, table_info in zip(raw_nodes, raw_tag_tables):
            if table_info is None:
                tags_by_node.append([])
                continue
            table, hash_table = table_info
            count = raw["tag_count"]
            tags = []
            for index in range(count):
                aligned_tag = align_up(cursor, 8)
                c.require_zero(cursor, aligned_tag, "MotTree tag alignment")
                cursor = aligned_tag
                pointer = base + c.u64(table + index * 8)
                if pointer != cursor:
                    raise MotionParseError(
                        f"{c.label}: node tag string 0x{pointer:X} "
                        f"does not match writer cursor 0x{cursor:X}"
                    )
                value, cursor = c.utf16_z(cursor, "MotTree tag")
                if c.u32(hash_table + index * 4) != 0:
                    raise MotionParseError(f"{c.label}: v4 tag hash must be zero")
                tags.append(TreeTag(value))
            tags_by_node.append(tags)

        node_names = []
        for raw in raw_nodes:
            aligned_name = align_up(cursor, 2)
            c.require_zero(cursor, aligned_name, "MotTree node-name alignment")
            cursor = aligned_name
            if base + raw["name"] != cursor:
                raise MotionParseError(
                    f"{c.label}: node-name string 0x{base + raw['name']:X} "
                    f"does not match writer cursor 0x{cursor:X}"
                )
            value, cursor = c.utf16_z(cursor, "MotTree node name")
            node_names.append(value)

        expected_params = align_up(cursor, 8)
        c.require_zero(cursor, expected_params, "MotTree parameter-table alignment")
        cursor = expected_params
        raw_params_by_node = []
        for node_index, raw in enumerate(raw_nodes):
            count = raw["param_count"]
            expected_table_presence = self.profile.mot_tree.parameter_tables.is_present(
                class_names[node_index],
                count,
            )
            if bool(raw["params"]) != expected_table_presence:
                raise MotionParseError(
                    f"{c.label}: parameter table presence for {class_names[node_index]!r} "
                    f"violates the v4 profile strategy"
                )
            if not count:
                if raw["params"] and base + raw["params"] != cursor:
                    raise MotionParseError(f"{c.label}: empty parameter table violates writer cursor")
                raw_params_by_node.append([])
                continue
            table = base + raw["params"]
            if table != cursor:
                raise MotionParseError(f"{c.label}: parameter table violates writer cursor")
            records = []
            for index in range(count):
                record = table + index * 0x18
                try:
                    ptype = TreeParameterType(c.u32(record + 8))
                except ValueError as exc:
                    raise MotionParseError(f"{c.label}: unsupported v4 parameter type") from exc
                records.append((c.u64(record), ptype, c.u32(record + 0xC), c.u64(record + 0x10)))
            raw_params_by_node.append(records)
            cursor += count * 0x18

        params_by_node = []
        for records in raw_params_by_node:
            params = []
            for name_pointer, ptype, property_hash, raw_value in records:
                if base + name_pointer != cursor:
                    raise MotionParseError(f"{c.label}: parameter name violates writer cursor")
                param_name, cursor = c.ascii_z(cursor, "MotTree parameter name")
                if property_hash != murmur3_hash(param_name.encode("ascii")):
                    raise MotionParseError(f"{c.label}: v4 parameter hash does not match its name")
                raw_bytes = struct.pack("<Q", raw_value)
                if ptype == TreeParameterType.BOOL:
                    if any(raw_bytes[1:]) or raw_bytes[0] > 1:
                        raise MotionParseError(f"{c.label}: invalid Bool parameter payload")
                    value = bool(raw_bytes[0])
                elif ptype == TreeParameterType.U32:
                    if raw_value >> 32:
                        raise MotionParseError(f"{c.label}: U32 parameter upper dword is nonzero")
                    value = raw_value
                elif ptype == TreeParameterType.F32:
                    if raw_value >> 32:
                        raise MotionParseError(f"{c.label}: F32 parameter padding is nonzero")
                    value = struct.unpack("<f", raw_bytes[:4])[0]
                else:
                    aligned_value = align_up(cursor, 2)
                    c.require_zero(cursor, aligned_value, "Str16 parameter alignment")
                    cursor = aligned_value
                    if base + raw_value != cursor:
                        raise MotionParseError(
                            f"{c.label}: Str16 parameter value 0x{base + raw_value:X} "
                            f"does not match writer cursor 0x{cursor:X}"
                        )
                    value, cursor = c.utf16_z(cursor, "MotTree Str16 parameter")
                params.append(TreeParameter(param_name, ptype, value))
            params_by_node.append(params)

        if any(raw["class_hash"] for raw in raw_nodes):
            raise MotionParseError(f"{c.label}: v4 class-name hash must be zero")
        logical_names: list[str | None] = []
        for value, raw in zip(node_names, raw_nodes):
            stored_hash = raw["name_hash"]
            if stored_hash == 0:
                if value:
                    raise MotionParseError(
                        f"{c.label}: unnamed v4 node has a nonempty name string"
                    )
                logical_names.append(None)
                continue
            if stored_hash != murmur3_hash(value.encode("utf-16le")):
                raise MotionParseError(f"{c.label}: v4 node name hash does not match its name")
            logical_names.append(value)

        nodes = []
        for index, raw in enumerate(raw_nodes):
            try:
                node_type = TreeNodeType(raw["node_type"])
            except ValueError as exc:
                raise MotionParseError(
                    f"{c.label}: unknown MotTree node type {raw['node_type']}"
                ) from exc
            nodes.append(
                TreeNode(
                    class_name=class_names[index],
                    name=logical_names[index],
                    authored_id=raw["id"],
                    node_type=node_type,
                    tags=tags_by_node[index],
                    parameters=params_by_node[index],
                )
            )
        if any(raw["flags"] for raw in raw_nodes):
            raise MotionParseError(f"{c.label}: v4 node flags are unsupported")

        expected_links = align_up(cursor, 16)
        c.require_zero(cursor, expected_links, "MotTree link-table alignment")
        link_table = base + link_stored
        if link_table != expected_links:
            raise MotionParseError(f"{c.label}: link table violates writer cursor")
        c.require(link_table, link_count * 0x28, "MotTree links")
        cursor = link_table + link_count * 0x28
        links = []
        raw_output_guids = []
        for index in range(link_count):
            record = link_table + index * 0x28
            input_index = c.u32(record)
            output_index = c.u32(record + 8)
            raw_link_type = c.u32(record + 0x10)
            try:
                link_type = TreeLinkType(raw_link_type)
            except ValueError as exc:
                raise MotionParseError(
                    f"{c.label}: unknown MotTree link type {raw_link_type}"
                ) from exc
            if (
                input_index >= len(nodes)
                or output_index >= len(nodes)
                or link_type not in (
                    TreeLinkType.MOTION,
                    TreeLinkType.PARAMETER,
                )
            ):
                raise MotionParseError(f"{c.label}: invalid MotTree link")
            if c.u32(record + 0x14) or c.u64(record + 0x18):
                raise MotionParseError(f"{c.label}: input GUID/reserved link data is unsupported")
            output_guid = c.u64(record + 0x20)
            raw_output_guids.append(output_guid)
            links.append(
                TreeLink(
                    nodes[input_index],
                    c.u32(record + 4),
                    nodes[output_index],
                    c.u32(record + 0xC),
                    link_type,
                )
            )
        aligned_guids = align_up(cursor, 16)
        c.require_zero(cursor, aligned_guids, "MotTree output-GUID alignment")
        cursor = aligned_guids
        for index, stored in enumerate(raw_output_guids):
            if not stored:
                continue
            if base + stored != cursor:
                raise MotionParseError(
                    f"{c.label}: output GUID 0x{base + stored:X} "
                    f"does not match writer cursor 0x{cursor:X}"
                )
            links[index].output_guid = c.bytes(cursor, 16, "MotTree output GUID")
            cursor += 16
        expected_end = align_up(cursor, 16)
        if expected_end != physical_end:
            raise MotionParseError(f"{c.label}: MotTree physical end violates writer layout")
        c.require_zero(cursor, expected_end, "MotTree physical padding")
        if node_count and root_index >= node_count:
            raise MotionParseError(f"{c.label}: MotTree root index is invalid")
        tree = MotTree(name, nodes, links, nodes[root_index] if nodes else None, remaps)
        self.validator.validate(tree)
        return tree
