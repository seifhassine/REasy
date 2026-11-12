"""See docs/internals/bhvt-format.md."""

from __future__ import annotations

import struct

lazy from utils.hash_util import murmur3_hash
lazy from file_handlers.clip.decode import read_timeline
lazy from file_handlers.clip.encode import write_embedded_timeline
lazy from file_handlers.clip.model import ResourceKind, walk_nodes, walk_properties
lazy from file_handlers.clip.rules import profile

from .errors import BhvtFormatError
from .model import Record, TimelineEntry, TimelineFsm, TimelineTree
from .rsz_blocks import RszBlocks
from .rules import TimelineFsmRules, timeline_fsm_rules
from .sections import aligned
from .tree_codec import read_tree, write_tree

_GUID = 16


def read_timeline_fsm(data: bytes, version: int, rsz: RszBlocks) -> TimelineFsm:
    rules = timeline_fsm_rules(version)
    header = rules.header.decode(data, 0)
    if header["version"] != version:
        raise BhvtFormatError(f"timeline FSM header version {header['version']} is not the file's {version}")
    c8, c16 = header["c8_pool"], header["c16_pool"]
    trees, tree_sizes = [], []
    for at, info in zip(_table(data, header["tree_data_table"], header["tree_count"]),
                        rules.tree_info.decode_table(data, header["tree_info_table"], header["tree_count"])):
        name = _c8(data, c8 + info["name"])
        if name != _c16(data, c16 + 2 * info["unicode_name"]):
            raise BhvtFormatError(f"timeline FSM tree {name} has different c8 and c16 names")
        tree_sizes.append(info["tree_data_size"])
        trees.append(TimelineTree(name, info["layer_id"],
                                  read_tree(data, at, at + info["tree_data_size"], rules.tree.version, rsz)))
    starts = _table(data, header["timeline_data_table"], header["timeline_count"])
    ends = [*starts[1:], header["timeline_info_table"]]
    timelines, first = [], 0
    for at, end, info in zip(starts, ends, rules.timeline_info.decode_table(data, header["timeline_info_table"],
                                                                           header["timeline_count"])):
        name = _c16(data, c16 + 2 * info["unicode_name"])
        if info["unicode_name_hash"] != murmur3_hash(name.encode("utf-16le")):
            raise BhvtFormatError(f"timeline {name} has a name hash that is not its name's")
        timeline = read_timeline(bytes(data[:end]), ResourceKind.TIMELINE, at, first)
        counts = _counts(data, at)
        held = [node for root in timeline.roots for node in walk_nodes([root])]
        if (len(held), sum(1 for node in held for _ in walk_properties(node.properties))) != counts:
            raise BhvtFormatError(f"timeline {name} holds nodes or properties no node reaches")
        timelines.append(TimelineEntry(name, info["timeline_id"], timeline))
        first += counts[0]
    layers = header["layer_id_table"]
    holds = [Record({"hold_point_id": item["hold_point_id"],
                     "targets": [bytes(data[layers + _GUID * index:layers + _GUID * (index + 1)])
                                 for index in range(item["target_layer_index"],
                                                    item["target_layer_index"] + item["target_count"])]})
             for item in rules.hold_point.decode_table(data, header["hold_point_table"], header["hold_point_count"])]
    fsm = TimelineFsm(version, trees, timelines, holds)
    layout = _layout(rules, fsm, tree_sizes, [end - at for at, end in zip(starts, ends)])
    if any(header[name] != value for name, value in layout.items() if name != "end") or layout["end"] != len(data):
        raise BhvtFormatError("timeline FSM tables are not where the canonical layout puts them")
    return fsm


def write_timeline_fsm(fsm: TimelineFsm, rsz: RszBlocks) -> bytes:
    rules = timeline_fsm_rules(fsm.version)
    trees = [write_tree(item.tree, rsz) for item in fsm.trees]
    at = rules.header.size + 8 * len(fsm.trees) + sum(map(len, trees)) + rules.tree_info.size * len(fsm.trees) + \
        8 * len(fsm.timelines)
    timelines, first = [], 0
    for entry in fsm.timelines:
        blob = write_embedded_timeline(entry.timeline, at, first)
        first += _counts(blob, 0)[0]
        timelines.append(blob)
        at += len(blob)
    layout = _layout(rules, fsm, list(map(len, trees)), list(map(len, timelines)))
    c8, c16 = _pools(fsm)
    out = bytearray(rules.header.encode({**layout, "version": fsm.version, "tree_count": len(fsm.trees),
                                         "timeline_count": len(fsm.timelines),
                                         "hold_point_count": len(fsm.hold_points)}))
    tree_at = _starts(len(out) + 8 * len(trees), trees)
    out += struct.pack(f"<{len(trees)}Q", *tree_at) + b"".join(trees)
    out += b"".join(rules.tree_info.encode({"tree_data_size": len(tree), "layer_id": item.layer_id,
                                            "name": c8[item.name], "unicode_name": c16[item.name]})
                    for item, tree in zip(fsm.trees, trees))
    out += struct.pack(f"<{len(timelines)}Q", *_starts(len(out) + 8 * len(timelines), timelines))
    out += b"".join(timelines)
    out += b"".join(rules.timeline_info.encode({"unicode_name_hash": murmur3_hash(entry.name.encode("utf-16le")),
                                                "timeline_id": entry.timeline_id, "unicode_name": c16[entry.name]})
                    for entry in fsm.timelines)
    out += b"".join(rules.hold_point.encode({"hold_point_id": hold.hold_point_id, "target_count": len(hold.targets),
                                             "target_layer_index": index})
                    for hold, index in zip(fsm.hold_points, _starts(0, [hold.targets for hold in fsm.hold_points])))
    out += b"".join(target for hold in fsm.hold_points for target in hold.targets)
    out += b"".join(text.encode("utf-8") + b"\0" for text in c8)
    out += bytes(layout["c16_pool"] - len(out))
    out += b"".join(text.encode("utf-16le") + b"\0\0" for text in c16)
    out += bytes(layout["end"] - len(out))
    return bytes(out)


def _layout(rules: TimelineFsmRules, fsm: TimelineFsm, tree_sizes: list[int], timeline_sizes: list[int]) -> dict:
    c8, c16 = _pools(fsm)
    tree_info = rules.header.size + 8 * len(tree_sizes) + sum(tree_sizes)
    timeline_table = tree_info + rules.tree_info.size * len(tree_sizes)
    timeline_info = timeline_table + 8 * len(timeline_sizes) + sum(timeline_sizes)
    hold_points = timeline_info + rules.timeline_info.size * len(timeline_sizes)
    layer_ids = hold_points + rules.hold_point.size * len(fsm.hold_points)
    c8_pool = layer_ids + _GUID * sum(len(hold.targets) for hold in fsm.hold_points)
    c16_pool = aligned(c8_pool + sum(len(text.encode("utf-8")) + 1 for text in c8), rules.c16_align)
    end = aligned(c16_pool + 2 * sum(len(text) + 1 for text in c16), rules.end_align)
    return {"tree_data_table": rules.header.size, "tree_info_table": tree_info, "timeline_data_table": timeline_table,
            "timeline_info_table": timeline_info, "hold_point_table": hold_points, "layer_id_table": layer_ids,
            "c8_pool": c8_pool, "c16_pool": c16_pool, "end": end}


def _pools(fsm: TimelineFsm) -> tuple[dict[str, int], dict[str, int]]:
    c8, c16, at8, at16 = {}, {}, 0, 0
    for name in (item.name for item in fsm.trees):
        if name not in c8:
            c8[name], at8 = at8, at8 + len(name.encode("utf-8")) + 1
    for name in [*(item.name for item in fsm.trees), *(entry.name for entry in fsm.timelines)]:
        if name not in c16:
            c16[name], at16 = at16, at16 + len(name) + 1
    return c8, c16


def _starts(first: int, items: list) -> list[int]:
    starts = []
    for item in items:
        starts.append(first)
        first += len(item)
    return starts


def _table(data, at: int, count: int) -> list[int]:
    return list(struct.unpack_from(f"<{count}Q", data, at))


def _counts(data, at: int) -> tuple[int, int]:
    header = profile(struct.unpack_from("<I", data, at + 4)[0]).rules.header.decode(data, at)
    return header["node_count"], header["property_count"]


def _c8(data, at: int) -> str:
    return bytes(data[at:data.index(b"\0", at)]).decode("utf-8")


def _c16(data, at: int) -> str:
    end = at
    while data[end:end + 2] != b"\0\0":
        end += 2
    return bytes(data[at:end]).decode("utf-16le")
