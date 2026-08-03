from __future__ import annotations

import struct

from utils.hash_util import murmur3_hash

from ..binary import pad_to_alignment
from ..profiles import MotionFormatProfile
from .model import MotTree, TreeParameterType
from .validator import MotTreeV4Validator


class MotTreeV4Writer:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot_tree=4)
        self.profile = profile
        self.validator = MotTreeV4Validator(profile)

    def build(self, tree: MotTree) -> bytes:
        self.validator.validate(tree)
        out = bytearray(0x50)
        remap_offset = len(out) if tree.motion_id_remaps else 0
        for remap in tree.motion_id_remaps:
            out.extend(struct.pack("<hh", remap.target, remap.source))
        pad_to_alignment(out, 8)
        name_offset = len(out)
        out.extend(tree.name.encode("utf-16le") + b"\0\0")
        pad_to_alignment(out, 8)
        node_table = len(out)
        out.extend(bytes(len(tree.nodes) * 0x38))

        class_offsets = []
        for node in tree.nodes:
            class_offsets.append(len(out))
            out.extend(node.class_name.encode("ascii") + b"\0")

        tag_tables = [0] * len(tree.nodes)
        tag_hash_tables = [0] * len(tree.nodes)
        tag_pointer_positions: list[list[int]] = [[] for _node in tree.nodes]
        for node_index, node in enumerate(tree.nodes):
            if not node.tags:
                continue
            pad_to_alignment(out, 8)
            tag_tables[node_index] = len(out)
            for _tag in node.tags:
                tag_pointer_positions[node_index].append(len(out))
                out.extend(bytes(8))
            tag_hash_tables[node_index] = len(out)
            out.extend(bytes(len(node.tags) * 4))
        if any(node.tags for node in tree.nodes):
            pad_to_alignment(out, 8)
        for node_index, node in enumerate(tree.nodes):
            for position, tag in zip(tag_pointer_positions[node_index], node.tags):
                pad_to_alignment(out, 8)
                struct.pack_into("<Q", out, position, len(out))
                out.extend(tag.name.encode("utf-16le") + b"\0\0")

        name_offsets = []
        for node in tree.nodes:
            pad_to_alignment(out, 2)
            name_offsets.append(len(out))
            out.extend((node.name or "").encode("utf-16le") + b"\0\0")

        pad_to_alignment(out, 8)
        parameter_tables = [0] * len(tree.nodes)
        parameter_record_positions = []
        for node_index, node in enumerate(tree.nodes):
            if not self.profile.mot_tree.parameter_tables.is_present(
                node.class_name,
                len(node.parameters),
            ):
                continue
            parameter_tables[node_index] = len(out)
            positions = []
            for _parameter in node.parameters:
                positions.append(len(out))
                out.extend(bytes(0x18))
            parameter_record_positions.append((node, positions))
        for node, positions in parameter_record_positions:
            for parameter, record in zip(node.parameters, positions):
                struct.pack_into("<Q", out, record, len(out))
                out.extend(parameter.name.encode("ascii") + b"\0")
                struct.pack_into(
                    "<II",
                    out,
                    record + 8,
                    int(parameter.parameter_type),
                    murmur3_hash(parameter.name.encode("ascii")),
                )
                if parameter.parameter_type == TreeParameterType.BOOL:
                    struct.pack_into("<B", out, record + 0x10, int(parameter.value))
                elif parameter.parameter_type == TreeParameterType.U32:
                    struct.pack_into("<I", out, record + 0x10, parameter.value)
                elif parameter.parameter_type == TreeParameterType.F32:
                    struct.pack_into("<f", out, record + 0x10, parameter.value)
                else:
                    pad_to_alignment(out, 2)
                    struct.pack_into("<Q", out, record + 0x10, len(out))
                    out.extend(parameter.value.encode("utf-16le") + b"\0\0")

        pad_to_alignment(out, 16)
        link_table = len(out)
        out.extend(bytes(len(tree.links) * 0x28))
        node_index = {id(node): index for index, node in enumerate(tree.nodes)}
        for index, link in enumerate(tree.links):
            record = link_table + index * 0x28
            struct.pack_into(
                "<IIIIIIQQ",
                out,
                record,
                node_index[id(link.input_node)],
                link.input_pin,
                node_index[id(link.output_node)],
                link.output_pin,
                link.link_type,
                0,
                0,
                0,
            )
        pad_to_alignment(out, 16)
        for index, link in enumerate(tree.links):
            if link.output_guid is not None:
                struct.pack_into("<Q", out, link_table + index * 0x28 + 0x20, len(out))
                out.extend(link.output_guid)
        pad_to_alignment(out, 16)

        for index, node in enumerate(tree.nodes):
            record = node_table + index * 0x38
            struct.pack_into(
                "<QQQQQIII4B",
                out,
                record,
                class_offsets[index],
                name_offsets[index],
                tag_tables[index],
                tag_hash_tables[index],
                parameter_tables[index],
                node.authored_id,
                0,
                0 if node.name is None else murmur3_hash(node.name.encode("utf-16le")),
                len(node.tags),
                len(node.parameters),
                node.node_type,
                0,
            )
        root_index = tree.nodes.index(tree.root) if tree.root is not None else 0
        struct.pack_into("<I4sII", out, 0, self.profile.mot_tree.version, b"mtre", 0, 0)
        struct.pack_into(
            "<QQQQQQ8H",
            out,
            0x10,
            0,
            0,
            node_table,
            link_table,
            name_offset,
            remap_offset,
            len(tree.nodes),
            len(tree.links),
            root_index,
            0,
            len(tree.motion_id_remaps),
            0,
            0,
            0,
        )
        return bytes(out)

