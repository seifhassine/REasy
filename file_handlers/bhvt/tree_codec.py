"""See docs/internals/bhvt-format.md."""

from __future__ import annotations

import struct
from itertools import chain

lazy from file_handlers.rsz.rsz_data_types import ArrayData, ResourceData
lazy from utils.hash_util import murmur3_hash

from .errors import BhvtFormatError
from .model import Hfsm, Node, Record, Tree
from .rsz_blocks import RszBlocks
from .rules import TreeRules, attribute_bit, tree_rules
from .sections import (READERS, U32, WRITERS, name_at, offsets_of, padding, path_offsets, read_pool, read_u32s,
                       read_uvar, stops, write_names, write_pool, write_uvar)

NO_ID = 0xFFFFFFFF
NO_PARENT = (NO_ID, 0)


def read_tree(data: bytes, base: int, end: int, version: int, rsz: RszBlocks) -> Tree:
    rules = tree_rules(version)
    header = rules.header.decode(data, base)
    starts = {section.name: base + header[section.offset] for section in rules.sections}
    ends = stops(starts, end)
    values = {section.name: _READERS[section.kind](data, starts[section.name], ends[section.name], base, rules, rsz)
              for section in rules.sections}
    nodes, action_ex_ids, static_action_ex_ids = values["nodes"]
    tree = Tree(version, Record({name: value for name, value in header.items() if not name.endswith("_offset")}), nodes,
                action_ex_ids, static_action_ex_ids,
                {section.name: values[section.name] for section in rules.sections if section.kind == "rsz"},
                values["reference_prefab_game_objects"], values["resource_paths"], values["variable"],
                values["base_variables"])
    _resolve_strings(tree, values["names"])
    if "userdata_paths" in values and values["userdata_paths"] != _userdata_pairs(tree):
        raise BhvtFormatError("the userdata path pool is not the RSZ blocks' userdata")
    if tree.resource_paths != resource_paths(tree):
        raise BhvtFormatError("the resource path pool is not the tree's reference trees and resource fields")
    return tree


def write_tree(tree: Tree, rsz: RszBlocks) -> bytes:
    rules = tree_rules(tree.version)
    out = bytearray(rules.header.size)
    offsets, paths = {}, resource_paths(tree)
    for section in rules.sections:
        out += bytes(-len(out) % section.align)
        offsets[section.offset] = len(out)
        _WRITERS[section.kind](tree, section, out, rules, rsz, paths)
    out += bytes(-len(out) % rules.end_align)
    out[:rules.header.size] = rules.header.encode({**tree.header.fields(), **offsets})
    return bytes(out)


def resource_paths(model: Tree | Hfsm) -> list[str]:
    used = [node.reference_tree for node in model.nodes
            if node.reference_tree is not None] if isinstance(model, Tree) else []
    for block in model.blocks.values():
        for index in range(len(block.instance_infos)):
            for item in block.parsed_elements[index].values():
                used += [text for value in (item.values if isinstance(item, ArrayData) else [item])
                         if isinstance(value, ResourceData) and (text := value.value.rstrip("\0"))]
    keys = {path.casefold() for path in used}
    paths = [path for path in model.resource_paths if path.casefold() in keys]
    known = {path.casefold() for path in paths}
    for path in used:
        if path.casefold() not in known:
            known.add(path.casefold())
            paths.append(path)
    return paths


def _name_offsets(tree: Tree) -> dict[str, int]:
    return offsets_of(chain((node.name for node in tree.nodes),
                            (name for item in tree.reference_prefab_game_objects for name in item.path)))


def _read_nodes(data, at, stop, base, rules: TreeRules, rsz):
    count = U32.unpack_from(data, at)[0]
    at += 4
    nodes = []
    for _ in range(count):
        fields, at = rules.node.read(data, at)
        nodes.append(Node(fields))
    action_ex_ids, at = read_u32s(data, at)
    static_action_ex_ids, at = read_u32s(data, at)
    padding(data, at, stop, "nodes")
    return nodes, action_ex_ids, static_action_ex_ids


def _read_game_objects(data, at, stop, base, rules, rsz):
    count = U32.unpack_from(data, at)[0]
    at += 4
    items = []
    for _ in range(count):
        guid = bytes(data[at:at + 16])
        path, at = read_u32s(data, at + 16)
        items.append(Record({"guid": guid, "path": path}))
    padding(data, at, stop, "reference_prefab_game_objects")
    return items


def _read_type_paths(data, at, stop, base, rules, rsz):
    items = read_pool(data, at, base, 2)
    return list(zip(items[::2], items[1::2]))


def _userdata_pairs(tree: Tree) -> list[tuple[str, str]]:
    return list(dict.fromkeys((block.type_registry.get_type_info(info.hash)["name"],
                               block.get_rsz_userdata_string(info))
                              for block in tree.blocks.values() for info in block.rsz_userdata_infos))


def _read_uvars(data, at, stop, base, rules, rsz):
    count = U32.unpack_from(data, at)[0]
    return [read_uvar(data, base + offset, base) for offset in struct.unpack_from(f"<{count}Q", data, at + 4)]


def _parents(tree: Tree) -> dict[int, Node]:
    by_key = {(node.id, node.ex_id): node for node in tree.nodes}
    return {id(by_key[key]): node for node in tree.nodes for key in zip(node.children, node.children_ex)}


def _name_hashes(tree: Tree, links: dict[int, Node]) -> dict[int, tuple[int, int]]:
    hashes = {}
    for node in tree.nodes:
        names, item = [], node
        while (parent := links.get(id(item))) is not None:
            names.append(item.name)
            item = parent
        full = ".".join(reversed(names)) if names else node.name
        hashes[id(node)] = (murmur3_hash(node.name.encode("utf-16le")), murmur3_hash(full.encode("utf-16le")))
    return hashes


def _resolve_strings(tree: Tree, pool: str):
    bit = attribute_bit(tree.version, "HasReferenceTree")
    starts = dict(zip(path_offsets(tree.resource_paths), tree.resource_paths))
    for item in tree.reference_prefab_game_objects:
        item.path = [name_at(pool, offset) for offset in item.path]
    for node in tree.nodes:
        node.name = name_at(pool, vars(node).pop("name_offset"))
    links = _parents(tree)
    hashes = _name_hashes(tree, links)
    for node in tree.nodes:
        fields, parent = vars(node), links.get(id(node))
        expected = NO_PARENT if parent is None else (parent.id, parent.ex_id)
        if (fields.pop("parent"), fields.pop("parent_ex")) != expected:
            raise BhvtFormatError(f"node {node.name} has a parent link its parent's children do not hold")
        if "name_hash" in fields and (fields.pop("name_hash"), fields.pop("full_name_hash")) != hashes[id(node)]:
            raise BhvtFormatError(f"node {node.name} has name hashes that are not its name's and path's")
        path = fields.pop("reference_tree_path_offset")
        if node.attribute & bit:
            node.reference_tree = starts[path]
        elif path:
            raise BhvtFormatError(f"node {node.name} has a reference tree path without HasReferenceTree")
        else:
            node.reference_tree = None


def _write_nodes(tree: Tree, section, out, rules: TreeRules, rsz, pool):
    links = _parents(tree)
    names, hashes = _name_offsets(tree), _name_hashes(tree, links)
    offsets = list(zip(pool, path_offsets(pool)))
    paths = {**dict(reversed([(path.casefold(), at) for path, at in offsets])), **dict(reversed(offsets))}
    out += U32.pack(len(tree.nodes))
    for node in tree.nodes:
        fields = {key: value for key, value in vars(node).items() if key not in ("name", "reference_tree")}
        parent = links.get(id(node))
        fields["parent"], fields["parent_ex"] = NO_PARENT if parent is None else (parent.id, parent.ex_id)
        fields["name_offset"] = names[node.name]
        fields["name_hash"], fields["full_name_hash"] = hashes[id(node)]
        reference = node.reference_tree
        fields["reference_tree_path_offset"] = 0 if reference is None else \
            paths[reference if reference in paths else reference.casefold()]
        out += rules.node.write(fields)
    for ids in (tree.action_ex_ids, tree.static_action_ex_ids):
        out += struct.pack(f"<I{len(ids)}I", len(ids), *ids)


def _write_game_objects(tree: Tree, section, out, rules, rsz, pool):
    names = _name_offsets(tree)
    out += U32.pack(len(tree.reference_prefab_game_objects))
    for item in tree.reference_prefab_game_objects:
        out += item.guid + struct.pack(f"<I{len(item.path)}I", len(item.path), *(names[name] for name in item.path))


def _write_type_paths(tree: Tree, section, out, rules, rsz, pool):
    pairs = _userdata_pairs(tree)
    write_pool(len(pairs), [text for pair in pairs for text in pair], out)


def _write_uvars(tree: Tree, section, out, rules, rsz, pool):
    table = len(out)
    out += U32.pack(len(tree.base_variables)) + bytes(8 * len(tree.base_variables))
    for index, uvar in enumerate(tree.base_variables):
        out += bytes(-len(out) % section.item_align)
        struct.pack_into("<Q", out, table + 4 + 8 * index, len(out))
        write_uvar(uvar, out)


_READERS = {**READERS, "nodes": _read_nodes, "game_objects": _read_game_objects, "type_paths": _read_type_paths,
            "uvars": _read_uvars}
_WRITERS = {**WRITERS, "nodes": _write_nodes, "game_objects": _write_game_objects,
            "names": lambda tree, section, out, rules, rsz, pool: write_names(_name_offsets(tree), out),
            "paths": lambda tree, section, out, rules, rsz, pool: write_pool(len(pool), pool, out),
            "type_paths": _write_type_paths, "uvars": _write_uvars}
