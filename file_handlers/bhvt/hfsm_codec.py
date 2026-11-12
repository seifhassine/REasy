"""See docs/internals/bhvt-format.md."""

from __future__ import annotations

import struct
from itertools import chain

from .errors import BhvtFormatError
from .model import Hfsm, MotionFsmEntry, MotionFsmV1, Record
from .references import field_value
from .rsz_blocks import RszBlocks
from .rules import HfsmRules, hfsm_rules, motion_fsm_v1_rules
from .sections import READERS, U32, WRITERS, aligned, name_at, offsets_of, padding, stops, write_names, write_pool
from .tree_codec import resource_paths


def read_hfsm(data: bytes, base: int, end: int, version: int, rsz: RszBlocks) -> Hfsm:
    rules = hfsm_rules(version)
    header = rules.header.decode(data, base)
    if header["version"] != version:
        raise BhvtFormatError(f"HFSM header version {header['version']} is not {version}")
    starts = {section.name: base + header[section.offset] for section in rules.sections}
    ends = stops(starts, end)
    values = {section.name: _READERS[section.kind](data, starts[section.name], ends[section.name], base, rules, rsz)
              for section in rules.sections}
    pool, root = values["names"], values["states"]
    for node in walk(root):
        node.name = name_at(pool, vars(node).pop("name_offset"))
    _attach_selectors(root, values["selectors"])
    keys = list(_action_keys(root, values["actions"]).items())
    if [(entry.key, entry.index) for entry in values["action_index_dictionary"]] != keys:
        raise BhvtFormatError("HFSM action index dictionary is not its states' actions in walk order")
    for condition in values["conditions"]:
        offset = vars(condition).pop("name_offset")
        if offset < -1:
            raise BhvtFormatError(f"condition {condition.uid} has name offset {offset}")
        condition.name = None if offset == -1 else name_at(pool, offset)
    hfsm = Hfsm(version, Record({name: value for name, value in header.items()
                                 if name != "version" and not name.endswith("_offset")}), root,
                {section.name: values[section.name] for section in rules.sections if section.kind == "rsz"},
                values["conditions"], values["resource_paths"], values["variable"])
    if hfsm.resource_paths != resource_paths(hfsm):
        raise BhvtFormatError("the resource path pool is not the HFSM's resource fields")
    return hfsm


def write_hfsm(hfsm: Hfsm, rsz: RszBlocks) -> bytes:
    rules = hfsm_rules(hfsm.version)
    out = bytearray(rules.header.size)
    derived = {"names": _name_offsets(hfsm), "parents": [node for node in walk(hfsm.root) if node.children],
               "paths": resource_paths(hfsm)}
    offsets = {}
    for section in rules.sections:
        out += bytes(-len(out) % section.align)
        offsets[section.offset] = len(out)
        _WRITERS[section.kind](hfsm, section, out, rules, rsz, derived)
    out[:rules.header.size] = rules.header.encode({"version": hfsm.version, **hfsm.header.fields(), **offsets})
    return bytes(out)


def walk(root: Record):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _attach_selectors(root: Record, selectors: list[Record]) -> None:
    parents = [node for node in walk(root) if node.children]
    if len(parents) != len(selectors):
        raise BhvtFormatError(f"HFSM has {len(parents)} parent states but {len(selectors)} selectors")
    numbers = {id(node): number for number, node in enumerate(parents, 1)}
    for node in walk(root):
        if vars(node).pop("selector_id") != numbers.get(id(node), 0):
            raise BhvtFormatError(f"state {node.name} does not use the selector of its place among parent states")
        node.selector = None
    for node, selector in zip(parents, selectors):
        states = vars(selector).pop("states")
        if [state.id for state in states] != [child.id for child in node.children]:
            raise BhvtFormatError(f"the selector of state {node.name} does not list its child states")
        selector.state_attributes = [state.attribute for state in states]
        node.selector = selector


def _action_keys(root: Record, actions) -> dict[int, int]:
    keys = dict.fromkeys((action.list_no << 32) | action.id for node in walk(root) for action in node.actions)
    if len(keys) != len(actions.object_table):
        raise BhvtFormatError(f"HFSM states use {len(keys)} actions, its action block holds "
                              f"{len(actions.object_table)}")
    return {key: index for index, key in enumerate(keys)}


def _name_offsets(hfsm: Hfsm) -> dict[str, int]:
    return offsets_of(chain((node.name for node in walk(hfsm.root)),
                            (condition.name for condition in hfsm.conditions if condition.name is not None)))


def read_motion_fsm_v1(data: bytes, version: int, rsz: RszBlocks) -> MotionFsmV1:
    rules = motion_fsm_v1_rules(version)
    header = rules.header.decode(data, 0)
    count = header["fsm_count"]
    infos = rules.fsm_info.decode_table(data, header["fsm_info_table"], count)
    extended = rules.extended_data.decode_table(data, header["extended_data_table"], count)
    stored, fsms = list(struct.unpack_from(f"<{count}Q", data, header["fsm_data_table"])), []
    for at, info, ext in zip(stored, infos, extended):
        transitions = rules.transition_data.decode_table(data, ext["transition_data_table"],
                                                         ext["transition_data_count"])
        fsms.append(MotionFsmEntry(Record({name: value for name, value in info.items() if name != "data_size"}),
                                   read_hfsm(data, at, at + info["data_size"], rules.hfsm.version, rsz),
                                   [Record(item) for item in transitions]))
    model = MotionFsmV1(version, fsms)
    layout, starts, tables, end = _layout(rules, model, [info["data_size"] for info in infos])
    found = (stored, [ext["transition_data_table"] for ext in extended], len(data))
    if any(header[name] != value for name, value in layout.items()) or (starts, tables, end) != found:
        raise BhvtFormatError("motion FSM tables are not where the canonical layout puts them")
    return model


def write_motion_fsm_v1(fsm: MotionFsmV1, rsz: RszBlocks) -> bytes:
    rules = motion_fsm_v1_rules(fsm.version)
    blobs = [write_hfsm(entry.hfsm, rsz) for entry in fsm.fsms]
    header, starts, tables, end = _layout(rules, fsm, list(map(len, blobs)))
    out = bytearray(rules.header.encode(header))
    out += struct.pack(f"<{len(starts)}Q", *starts)
    for at, blob in zip(starts, blobs):
        out += bytes(at - len(out)) + blob
    out += bytes(header["fsm_info_table"] - len(out))
    out += b"".join(rules.fsm_info.encode({**entry.info.fields(), "data_size": len(blob)})
                    for entry, blob in zip(fsm.fsms, blobs))
    out += bytes(header["extended_data_table"] - len(out))
    out += b"".join(rules.extended_data.encode({"transition_data_table": table,
                                                "transition_data_count": len(entry.transitions)})
                    for entry, table in zip(fsm.fsms, tables))
    for entry, table in zip(fsm.fsms, tables):
        out += bytes(table - len(out)) + b"".join(rules.transition_data.encode(item.fields())
                                                  for item in entry.transitions)
    return bytes(out + bytes(end - len(out)))


def _layout(rules, fsm: MotionFsmV1, sizes: list[int]) -> tuple[dict, list[int], list[int], int]:
    at = aligned(rules.header.size + 8 * len(fsm.fsms), rules.align)
    starts = []
    for size in sizes:
        starts.append(at)
        at = aligned(at + size, rules.align)
    info_at = at
    extended_at = aligned(info_at + rules.fsm_info.size * len(fsm.fsms), rules.align)
    at = aligned(extended_at + rules.extended_data.size * len(fsm.fsms), rules.align)
    tables = []
    for entry in fsm.fsms:
        tables.append(at := aligned(at, rules.align))
        at += rules.transition_data.size * len(entry.transitions)
    header = {"version": fsm.version, "fsm_data_table": rules.header.size, "fsm_info_table": info_at,
              "extended_data_table": extended_at, "fsm_count": len(fsm.fsms)}
    return header, starts, tables, at


def _read_node(data, at, rules: HfsmRules):
    fields, at = rules.node.read(data, at)
    node = Record(fields)
    node.children = []
    count = U32.unpack_from(data, at)[0]
    at += 4
    for _ in range(count):
        child, at = _read_node(data, at, rules)
        node.children.append(child)
    return node, at


def _read_states(data, at, stop, base, rules: HfsmRules, rsz):
    count = U32.unpack_from(data, at)[0]
    root, end = _read_node(data, at + 4, rules)
    if sum(1 for _ in walk(root)) != count:
        raise BhvtFormatError(f"HFSM state table holds {count} nodes, its tree {sum(1 for _ in walk(root))}")
    padding(data, end, stop, "states")
    return root


def _read_selectors(data, at, stop, base, rules: HfsmRules, rsz):
    count = U32.unpack_from(data, at)[0]
    at += 4
    selectors = []
    for _ in range(count):
        fields, at = rules.selector.read(data, at)
        if fields.pop("state_node_count") != len(fields["states"]):
            raise BhvtFormatError("HFSM selector state count differs from its state list")
        selectors.append(Record(fields))
    padding(data, at, stop, "selectors")
    return selectors


def _read_records(data, at, stop, codec, name):
    count = U32.unpack_from(data, at)[0]
    items = [Record(item) for item in codec.decode_table(data, at + 4, count)]
    padding(data, at + 4 + codec.size * count, stop, name)
    return items


def _write_node(node: Record, out, rules: HfsmRules, derived: dict, numbers: dict[int, int]):
    fields = {key: value for key, value in node.fields().items() if key not in ("name", "children", "selector")}
    out += rules.node.write({**fields, "name_offset": derived["names"][node.name],
                             "selector_id": numbers.get(id(node), 0)}) + U32.pack(len(node.children))
    for child in node.children:
        _write_node(child, out, rules, derived, numbers)


def _write_states(hfsm: Hfsm, section, out, rules: HfsmRules, rsz, derived):
    out += U32.pack(sum(1 for _ in walk(hfsm.root)))
    _write_node(hfsm.root, out, rules, derived, {id(node): number for number, node in enumerate(derived["parents"], 1)})


def _write_selectors(hfsm: Hfsm, section, out, rules: HfsmRules, rsz, derived):
    out += U32.pack(len(derived["parents"]))
    for node in derived["parents"]:
        fields = {key: value for key, value in node.selector.fields().items() if key != "state_attributes"}
        states = [{"id": child.id, "attribute": attribute}
                  for child, attribute in zip(node.children, node.selector.state_attributes, strict=True)]
        out += rules.selector.write({**fields, "states": states, "state_node_count": len(states)})


def _write_conditions(hfsm: Hfsm, section, out, rules: HfsmRules, rsz, derived):
    out += U32.pack(len(hfsm.conditions))
    for condition in hfsm.conditions:
        fields = {key: value for key, value in condition.fields().items() if key != "name"}
        offset = -1 if condition.name is None else derived["names"][condition.name]
        out += rules.condition.encode({**fields, "name_offset": offset})


def _write_dictionary(hfsm: Hfsm, section, out, rules: HfsmRules, rsz, derived):
    actions, names = hfsm.blocks["actions"], rules.graph["action_fields"]
    keys = _action_keys(hfsm.root, actions)
    for key, index in keys.items():
        fields = actions.parsed_elements[actions.object_table[index]]
        if (field_value(fields, names["list_no"]) << 32) | field_value(fields, names["id"]) != key:
            raise BhvtFormatError(f"HFSM action object {index} is not action {key & 0xFFFFFFFF} of list {key >> 32}")
    out += U32.pack(len(keys)) + b"".join(rules.dictionary_entry.encode({"key": key, "index": index})
                                          for key, index in keys.items())


_READERS = {**READERS, "states": _read_states, "selectors": _read_selectors,
            "conditions": lambda data, at, stop, base, rules, rsz: _read_records(data, at, stop, rules.condition,
                                                                                 "conditions"),
            "dictionary": lambda data, at, stop, base, rules, rsz: _read_records(data, at, stop, rules.dictionary_entry,
                                                                                 "dictionary")}
_WRITERS = {**WRITERS, "states": _write_states, "selectors": _write_selectors, "conditions": _write_conditions,
            "dictionary": _write_dictionary,
            "names": lambda hfsm, section, out, rules, rsz, derived: write_names(derived["names"], out),
            "paths": lambda hfsm, section, out, rules, rsz, derived: write_pool(len(derived["paths"]),
                                                                               derived["paths"], out)}
