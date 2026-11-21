"""See docs/internals/skeleton-format.md, Editing."""
from __future__ import annotations

import math
import struct
from collections import defaultdict

from PySide6.QtCore import QObject

from file_handlers.document_commands import EditDocument, ReplaceContents, SetFields
from utils.hash_util import murmur3_hash_utf16le

from .skeleton_file import Joint, SkeletonFile, read_skeleton, write_skeleton

_FLOATS = {"position": "<3f", "rotation": "<4f", "scale": "<3f"}
_LARGEST = struct.unpack("<f", b"\xff\xff\x7f\x7f")[0]


class SkeletonDocument(EditDocument):
    def __init__(self, skeleton: SkeletonFile, parent: QObject | None = None):
        super().__init__(parent)
        self.skeleton = skeleton

    @classmethod
    def read(cls, data: bytes, parent: QObject | None = None) -> SkeletonDocument:
        return cls(read_skeleton(data), parent)

    def write(self) -> bytes:
        if problems := self.problems():
            raise ValueError("\n".join(problems))
        return write_skeleton(self.skeleton)

    @property
    def joints(self) -> list[Joint]:
        return self.skeleton.joints

    def clashes(self) -> list[list[Joint]]:
        named = defaultdict(list)
        for joint in self.joints:
            named[joint.name_hash].append(joint)
        return [held for held in named.values() if len(held) > 1]

    def problems(self) -> list[str]:
        return [self.tr("The joints {0} share a name hash: the game finds only one of them.").format(
            ", ".join(joint.name for joint in held)) for held in self.clashes()]

    def under(self, joints: list[Joint]) -> list[Joint]:
        """``joints`` and every joint below them, in table order."""
        found = set(map(id, joints))
        for joint in self.joints:
            if id(joint.parent) in found:
                found.add(id(joint))
        return [joint for joint in self.joints if id(joint) in found]

    def set_values(self, joint: Joint, text: str, **values) -> None:
        for field in values.keys() & _FLOATS:
            if not all(abs(value) <= _LARGEST for value in values[field]):
                raise ValueError(self.tr("Type finite numbers a 32-bit float holds."))
            if field == "rotation":
                if not (length := math.sqrt(sum(value * value for value in values[field]))):
                    raise ValueError(self.tr("A rotation is a quaternion with a length: 0, 0, 0, 1 turns nothing."))
                if abs(length - 1) > 1e-6:
                    values[field] = [value / length for value in values[field]]
            values[field] = struct.unpack(_FLOATS[field], struct.pack(_FLOATS[field], *values[field]))
        if "parent" in values and not -1 < self.skeleton.rows()[id(values["parent"])] < self.joints.index(joint):
            raise ValueError(self.tr("A joint's parent comes before it in the table."))
        if any(getattr(joint, field) != value for field, value in values.items()):
            self.push(SetFields(self, [(joint, values)], text))

    def rename(self, joint: Joint, name: str) -> None:
        if not name:
            raise ValueError(self.tr("Type a name."))
        name_hash = murmur3_hash_utf16le(name)
        if (held := next((held for held in self.joints if held is not joint and held.name_hash == name_hash),
                         None)) is not None:
            raise ValueError(self.tr("{0} has this name, or one with its hash.").format(held.name))
        self.set_values(joint, self.tr("Rename joint"), name=name)

    def add(self, parent: Joint | None) -> Joint:
        taken, number = {joint.name_hash for joint in self.joints}, 1
        while murmur3_hash_utf16le(name := f"Joint{number}") in taken:
            number += 1
        made = Joint(name, parent)
        self.push(ReplaceContents(self, self.skeleton, "joints", [*self.joints, made], self.tr("Add joint")))
        return made

    def remove(self, joints: list[Joint]) -> None:
        if len(gone := set(map(id, self.under(joints)))) == len(self.joints):
            raise ValueError(self.tr("A skeleton keeps at least one joint."))
        with self.macro(self.tr("Delete joints")):
            if unpaired := [(joint, {"mirror": None}) for joint in self.joints
                            if id(joint) not in gone and id(joint.mirror) in gone]:
                self.push(SetFields(self, unpaired, self.tr("Delete joints")))
            self.push(ReplaceContents(self, self.skeleton, "joints",
                                      [joint for joint in self.joints if id(joint) not in gone],
                                      self.tr("Delete joints")))

    def move(self, joints: list[Joint], step: int) -> None:
        order, moving = list(self.joints), set(map(id, joints))
        rows = [row for row, held in enumerate(order) if id(held) in moving]
        if not rows or not (0 <= rows[0] + step and rows[-1] + step < len(order)):
            return
        for row in rows if step < 0 else reversed(rows):
            order[row], order[row + step] = order[row + step], order[row]
        placed = {id(joint): row for row, joint in enumerate(order)}
        if any(joint.parent is not None and placed[id(joint.parent)] > row for row, joint in enumerate(order)):
            raise ValueError(self.tr("A joint stays below its parent and above the joints under it in the table."))
        self.push(ReplaceContents(self, self.skeleton, "joints", order, self.tr("Move joints")))
