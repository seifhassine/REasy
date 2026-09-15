from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class TreeParameterType(IntEnum):
    BOOL = 0
    U32 = 6
    F32 = 9
    STR16 = 12


class TreeNodeType(IntEnum):
    UNKNOWN = 0
    GAME_OBJECT = 1
    COMPONENT = 2
    FOLDER = 3


class TreeLinkType(IntEnum):
    UNKNOWN = 0
    MOTION = 1
    PARAMETER = 2


@dataclass(slots=True)
class TreeParameter:
    name: str
    parameter_type: TreeParameterType
    value: bool | int | float | str


@dataclass(slots=True)
class TreeTag:
    name: str


@dataclass(slots=True, eq=False)
class TreeNode:
    class_name: str
    # v4 uses a zero hash as the sentinel for an unnamed node.  A present name
    # is hashed, including the explicitly-present empty string.  The string
    # table alone cannot distinguish those two empty spellings, so preserve
    # the semantic presence distinction here instead of storing the hash.
    name: str | None = None
    authored_id: int = 1
    node_type: TreeNodeType = TreeNodeType.GAME_OBJECT
    tags: list[TreeTag] = field(default_factory=list)
    parameters: list[TreeParameter] = field(default_factory=list)


@dataclass(slots=True)
class TreeLink:
    input_node: TreeNode
    input_pin: int
    output_node: TreeNode
    output_pin: int
    link_type: TreeLinkType
    output_guid: bytes | None = None


@dataclass(slots=True)
class MotionIdRemap:
    target: int
    source: int


@dataclass(slots=True)
class MotTree:
    name: str
    nodes: list[TreeNode] = field(default_factory=list)
    links: list[TreeLink] = field(default_factory=list)
    root: TreeNode | None = None
    motion_id_remaps: list[MotionIdRemap] = field(default_factory=list)
