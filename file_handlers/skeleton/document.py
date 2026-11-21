"""See docs/internals/skeleton-editor.md, Edits."""
from __future__ import annotations

import math
import struct
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, Signal

from file_handlers.document_commands import EditDocument, ReplaceContents, SetFields
from file_handlers.engine_math import make_trs_matrix
from utils.hash_util import murmur3_hash_utf16le

from .pose import local_fields, mirrored, model_matrices, rules
from .skeleton_file import Joint, SkeletonFile, read_skeleton, write_skeleton

_FLOATS = {"position": "<3f", "rotation": "<4f", "scale": "<3f"}
_LARGEST = struct.unpack("<f", b"\xff\xff\x7f\x7f")[0]
_WRITTEN = {"position": ("position",), "rotation": ("position", "rotation"), "scale": ("scale",)}


@dataclass(frozen=True, slots=True, eq=False)
class Grab:
    worlds: np.ndarray
    moved: tuple[tuple[int, bool], ...]
    written: tuple[tuple[Joint, int, int, dict], ...]


class SkeletonDocument(EditDocument):
    noted = Signal(str)

    def __init__(self, skeleton: SkeletonFile, parent: QObject | None = None):
        super().__init__(parent)
        self.skeleton, self._worlds = skeleton, None
        self.keep_children = self.mirror = False
        self.changed.connect(self._forget)

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

    def worlds(self) -> np.ndarray:
        if self._worlds is None:
            self._worlds = model_matrices(self.joints)
        return self._worlds

    def _forget(self, _change) -> None:
        self._worlds = None

    def clashes(self) -> list[list[Joint]]:
        named = defaultdict(list)
        for joint in self.joints:
            named[joint.name_hash].append(joint)
        return [held for held in named.values() if len(held) > 1]

    def problems(self) -> list[str]:
        return [self.tr("The joints {0} share a name hash: the game finds only one of them.").format(
            ", ".join(joint.name for joint in held)) for held in self.clashes()]

    def under(self, joints: list[Joint]) -> list[Joint]:
        found = set(map(id, joints))
        for joint in self.joints:
            if id(joint.parent) in found:
                found.add(id(joint))
        return [joint for joint in self.joints if id(joint) in found]

    def _stored(self, field: str, values) -> tuple:
        if not all(abs(value) <= _LARGEST for value in values):
            raise ValueError(self.tr("Type finite numbers a 32-bit float holds."))
        if field == "rotation":
            if not (length := math.sqrt(sum(value * value for value in values))):
                raise ValueError(self.tr("A rotation is a quaternion with a length: 0, 0, 0, 1 turns nothing."))
            if abs(length - 1) > 1e-6:
                values = [value / length for value in values]
        return struct.unpack(_FLOATS[field], struct.pack(_FLOATS[field], *values))

    def set_values(self, joint: Joint, text: str, **values) -> None:
        values = {field: self._stored(field, value) if field in _FLOATS else value for field, value in values.items()}
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

    def reparent(self, joint: Joint, parent: Joint) -> None:
        if parent is joint.parent:
            return
        if parent in (branch := self.under([joint])):
            raise ValueError(self.tr("A joint cannot hang on itself or on a joint under it."))
        rows, worlds, text = self.skeleton.rows(), self.worlds().astype(np.float64), self.tr("Set parent")
        fields = local_fields(worlds[rows[id(joint)]], worlds[rows[id(parent)]],
                              {name: getattr(joint, name) for name in _FLOATS})
        with self.macro(text):
            if rows[id(parent)] > rows[id(joint)]:
                moving = set(map(id, branch))
                order = [held for held in self.joints if id(held) not in moving]
                at = order.index(parent) + 1
                self.push(ReplaceContents(self, self.skeleton, "joints", order[:at] + branch + order[at:], text))
            self.push(SetFields(self, [(joint, {"parent": parent, **fields})], text))

    def pair(self, joint: Joint, partner: Joint | None) -> None:
        paired = {id(joint): (joint, partner)} | ({} if partner is None else {id(partner): (partner, joint)})
        freed = [held for held in self.joints if id(held) not in paired and id(held.mirror) in paired]
        if edits := [(held, {"mirror": other}) for held, other in (*paired.values(), *((held, None) for held in freed))
                     if held.mirror is not other]:
            self.push(SetFields(self, edits, self.tr("Set mirror")))
        if freed:
            self.noted.emit(self.tr("{0}: no longer mirrored, its partner now mirrors another joint.").format(
                ", ".join(held.name for held in freed)))

    def grab(self, joints: list[Joint], mode: str) -> Grab:
        rows, worlds, below = self.skeleton.rows(), self.worlds().astype(np.float64), set(map(id, self.under(joints)))
        moved, alone = [(rows[id(joint)], False) for joint in joints if id(joint.parent) not in below], []
        for joint in [self.joints[row] for row, _ in moved] if self.mirror and mode != "scale" else ():
            if (partner := joint.mirror) in (None, joint) or id(partner) in below:
                continue
            if partner.mirror is joint and np.abs(mirrored(worlds[rows[id(joint)]])[:3, 3] - worlds[
                    rows[id(partner)], :3, 3]).max() <= rules()["mirror"]["tolerance"]:
                moved.append((rows[id(partner)], True))
            else:
                alone.append(joint.name)
        carried = {row for row, _ in moved}
        written = tuple((joint, row, rows[id(joint.parent)], {name: getattr(joint, name) for name in _WRITTEN[mode]})
                        for row, joint in enumerate(self.joints) if row in carried or (
                            self.keep_children and mode != "scale" and rows[id(joint.parent)] in carried))
        if flat := next((joint for joint, row, above, _ in written if not all(
                np.linalg.det(worlds[at, :3, :3]) > 0 for at in (row, above) if at >= 0)), None):
            raise ValueError(self.tr("{0} or its parent is scaled to zero or by a negative scale, which this edit does "
                                     "not cover: type its values with Children stay and Mirror off.").format(flat.name))
        if alone:
            self.noted.emit(self.tr("{0}: no partner that mirrors it across the centre plane, so only it moves.")
                            .format(", ".join(alone)))
        return Grab(worlds, tuple(moved), written)

    def drag(self, grab: Grab, matrix: np.ndarray, mode: str, typed: dict | None = None) -> None:
        moved = {row: grab.worlds[row] @ matrix if mode == "scale" else (
            mirrored(matrix) if flipped else matrix) @ grab.worlds[row] for row, flipped in grab.moved}
        exact = {grab.moved[0][0]: typed or {}}
        self.push(SetFields(self, [
            (joint, local_fields(moved.get(row, grab.worlds[row]), moved.get(above, grab.worlds[above])
                                 if above >= 0 else np.eye(4), stored) | exact.get(row, {}))
            for joint, row, above, stored in grab.written], self._text(mode, typed is not None), merge_key=grab))

    def _text(self, mode: str, typed: bool) -> str:
        return {"position": (self.tr("Move joints"), self.tr("Set position")),
                "rotation": (self.tr("Rotate joints"), self.tr("Set rotation")),
                "scale": (self.tr("Scale joints"), self.tr("Set scale"))}[mode][typed]

    def place(self, joint: Joint, mode: str, values) -> None:
        if mode == "scale" or not (self.keep_children or self.mirror):
            return self.set_values(joint, self._text(mode, True), **{mode: values})
        if (typed := self._stored(mode, values)) == getattr(joint, mode):
            return
        grab, rows = self.grab([joint], mode), self.skeleton.rows()
        base = np.eye(4) if joint.parent is None else grab.worlds[rows[id(joint.parent)]]
        world = base @ make_trs_matrix(*(typed if name == mode else getattr(joint, name) for name in _FLOATS))
        self.drag(grab, world @ np.linalg.inv(grab.worlds[rows[id(joint)]]), mode, {mode: typed})

    def move_to(self, joint: Joint, position) -> None:
        offset = np.eye(4)
        offset[:3, 3] = np.subtract(position, self.worlds()[self.skeleton.rows()[id(joint)], :3, 3])
        self.drag(self.grab([joint], "position"), offset, "position", {})
