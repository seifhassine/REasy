"""See docs/internals/bhvt-format.md."""

from __future__ import annotations

from .errors import BhvtFormatError
from .model import MotionFsm, Record
from .rsz_blocks import RszBlocks
from .rules import motion_fsm_rules
from .sections import aligned
from .tree_codec import read_tree, write_tree

_TABLES = ("tree_data", "transition_map_table", "transition_data_table", "transition_extra_table", "tree_info",
           "transition_map_count", "transition_data_count")


def read_motion_fsm(data: bytes, version: int, rsz: RszBlocks) -> MotionFsm:
    rules = motion_fsm_rules(version)
    header = rules.header.decode(data, 0)
    info = rules.tree_info.decode(data, header["tree_info"])
    tree_end = header["tree_data"] + info["tree_data_size"]
    if header["tree_data"] != rules.header.size or header["tree_info"] != tree_end:
        raise BhvtFormatError("motion FSM tree is not between the header and its tree info")
    tree = read_tree(data, header["tree_data"], tree_end, rules.tree.version, rsz)
    maps = rules.transition_map.decode_table(data, header["transition_map_table"], header["transition_map_count"])
    records = rules.transition_data.decode_table(data, header["transition_data_table"],
                                                 header["transition_data_count"])
    extras_at = header.get("transition_extra_table", 0)
    extra_count = max((item["extra_index"] + item["extra_count"] for item in records), default=0) if extras_at else 0
    extras = rules.transition_data.decode_table(data, extras_at, extra_count) if extras_at else []
    fsm = MotionFsm(version, Record({name: value for name, value in header.items() if name not in _TABLES}), tree,
                    [Record(item) for item in maps], [Record(item) for item in records],
                    [Record(item) for item in extras])
    layout = _layout(fsm, info["tree_data_size"])
    if layout != {name: header[name] for name in _TABLES if name in header}:
        raise BhvtFormatError("motion FSM tables are not where the canonical layout puts them")
    tables = [(at, count) for at, count in ((header["transition_data_table"], len(records)), (extras_at, len(extras)))
              if count]
    if len(data) != max((at + count * rules.transition_data.size for at, count in tables),
                        default=header["transition_data_table"]):
        raise BhvtFormatError("motion FSM has data after its transition tables")
    return fsm


def write_motion_fsm(fsm: MotionFsm, rsz: RszBlocks) -> bytes:
    rules = motion_fsm_rules(fsm.version)
    tree = write_tree(fsm.tree, rsz)
    layout = _layout(fsm, len(tree))
    out = bytearray(rules.header.encode({**fsm.header.fields(), **layout}))
    out += tree + rules.tree_info.encode({"tree_data_size": len(tree)})
    out += b"".join(rules.transition_map.encode(item.fields()) for item in fsm.transition_maps)
    out += bytes(-len(out) % rules.align)
    out += b"".join(rules.transition_data.encode(item.fields()) for item in fsm.transition_data)
    if fsm.transition_extras:
        out += bytes(-len(out) % rules.align)
        out += b"".join(rules.transition_data.encode(item.fields()) for item in fsm.transition_extras)
    return bytes(out)


def _layout(fsm: MotionFsm, tree_size: int) -> dict[str, int]:
    rules = motion_fsm_rules(fsm.version)
    tree_at = rules.header.size
    info_at = tree_at + tree_size
    map_at = info_at + rules.tree_info.size
    data_at = aligned(map_at + rules.transition_map.size * len(fsm.transition_maps), rules.align)
    data_end = data_at + rules.transition_data.size * len(fsm.transition_data)
    layout = {"tree_data": tree_at, "transition_map_table": map_at, "transition_data_table": data_at,
              "tree_info": info_at, "transition_map_count": len(fsm.transition_maps),
              "transition_data_count": len(fsm.transition_data)}
    if "transition_extra_table" in rules.header.fields:
        layout["transition_extra_table"] = aligned(data_end, rules.align) if fsm.transition_extras else 0
    return layout
