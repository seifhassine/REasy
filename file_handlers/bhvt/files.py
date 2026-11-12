"""See docs/internals/bhvt-format.md."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import BhvtFormatError
from .rsz_blocks import RszBlocks
lazy from .hfsm_codec import read_hfsm, read_motion_fsm_v1, write_hfsm, write_motion_fsm_v1
lazy from .motion_fsm import read_motion_fsm, write_motion_fsm
lazy from .rules import file_entry
lazy from .timeline_fsm import read_timeline_fsm, write_timeline_fsm
lazy from .tree_codec import read_tree, write_tree

MAGICS = {(0, b"BHVT"), (0, b"HFSM"), (0, b"tfs2"), (4, b"mfs2"), (4, b"mfsm")}
_NAME = re.compile(r"\.(bhvt|fsmv2|motfsm2|tmlfsm2|fsm|motfsm)\.(\d+)(?:\.[^.]+)*$", re.IGNORECASE)


@dataclass(eq=False)
class Resource:
    kind: str
    version: int
    model: object


def file_kind(path: str) -> tuple[str, int]:
    match = _NAME.search(path.replace("\\", "/").rsplit("/", 1)[-1])
    if match is None:
        raise BhvtFormatError(f"{path} is not a behavior tree or FSM file")
    return match.group(1).lower(), int(match.group(2))


def read_resource(data: bytes, path: str, rsz: RszBlocks) -> Resource:
    kind, version = file_kind(path)
    entry = file_entry(kind, version)
    if kind in ("bhvt", "fsmv2"):
        model = read_tree(data, 0, len(data), entry["tree"], rsz)
    elif kind == "fsm":
        model = read_hfsm(data, 0, len(data), entry["hfsm"], rsz)
    else:
        model = {"motfsm2": read_motion_fsm, "tmlfsm2": read_timeline_fsm, "motfsm": read_motion_fsm_v1}[kind](
            data, version, rsz)
    return Resource(kind, version, model)


def write_resource(resource: Resource, rsz: RszBlocks) -> bytes:
    writers = {"bhvt": write_tree, "fsmv2": write_tree, "fsm": write_hfsm, "motfsm2": write_motion_fsm,
               "tmlfsm2": write_timeline_fsm, "motfsm": write_motion_fsm_v1}
    return writers[resource.kind](resource.model, rsz)
