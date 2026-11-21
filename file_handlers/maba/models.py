"""See docs/internals/maba-format.md, Editing."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal

lazy from ui.styles import current_colors, qcolor

from .document import MAX_KEY_SIZE, MabaDocument
from .motions import BankMotion

_DISPLAY, _EDIT, _TIP, _FORE = (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole, Qt.ItemDataRole.ToolTipRole,
                                Qt.ItemDataRole.ForegroundRole)
_U32 = 0xFFFFFFFF


class ListModel(QAbstractTableModel):
    refused = Signal(str)
    editable: frozenset[int] = frozenset()

    def __init__(self, document: MabaDocument, parent=None):
        super().__init__(parent)
        self.document, self.owner, self._shape = document, None, (0, 0)
        document.changed.connect(self._changed)

    def items(self) -> list:
        raise NotImplementedError

    def headers(self) -> list[tuple[str, str]]:
        raise NotImplementedError

    def ready(self) -> bool:
        return self.owner is not None

    def holder(self):
        return self.owner

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items())

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers())

    def headerData(self, section: int, orientation, role=_DISPLAY):
        if orientation == Qt.Orientation.Vertical:
            return str(section) if role == _DISPLAY else None
        shown = {_DISPLAY: 0, _TIP: 1}.get(role)
        return None if shown is None else self.headers()[section][shown]

    def flags(self, index: QModelIndex):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return flags | Qt.ItemFlag.ItemIsEditable if index.column() in self.editable else flags

    def setData(self, index: QModelIndex, value, role=_EDIT) -> bool:
        try:
            self.edit(self.items()[index.row()], index.column(), str(value).strip())
        except ValueError as error:
            self.refused.emit(str(error))
            return False
        return True

    def set_owner(self, owner) -> None:
        self.beginResetModel()
        self.owner = owner
        self._shape = (self.rowCount(), self.columnCount())
        self.endResetModel()

    def whole(self, text: str, high: int = _U32) -> int:
        try:
            value = int(text, 16 if text.lower().startswith("0x") else 10)
        except ValueError:
            value = -1
        if not 0 <= value <= high:
            raise ValueError(self.tr("Type a whole number from 0 to {0}.").format(high))
        return value

    def _changed(self, change=None) -> None:
        shape = (self.rowCount(), self.columnCount())
        if shape != self._shape or change is not None and change.structural and change.target is self.holder():
            self._shape = shape
            self.beginResetModel()
            self.endResetModel()
        elif all(shape):
            self.dataChanged.emit(self.index(0, 0), self.index(shape[0] - 1, shape[1] - 1))
            self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, shape[1] - 1)


class DatabaseModel(ListModel):
    editable = frozenset({0})

    def items(self) -> list:
        return self.document.databases

    def ready(self) -> bool:
        return True

    def holder(self):
        return self.document.maba

    def headers(self) -> list[tuple[str, str]]:
        return [(self.tr("Database"), self.tr("Query sets list the databases to merge by name; the game matches the "
                                              "murmur3 hash of the name.")),
                (self.tr("Keys"), self.tr("Values each sampled frame holds. A merged query takes its keys from the "
                                          "first database it names.")),
                (self.tr("Motions"), self.tr("Motion ranges: one sampled motion each.")),
                (self.tr("Rows"), self.tr("Sampled frames of all its motions."))]

    def data(self, index: QModelIndex, role=_DISPLAY):
        database = self.items()[index.row()]
        if role in (_DISPLAY, _EDIT):
            return (database.name, len(database.keys), len(database.ranges),
                    sum(len(motion.frames) for motion in database.ranges))[index.column()]
        return None

    def edit(self, database, column: int, text: str) -> None:
        self.document.rename_database(database, text)

    def make(self, sources: list) -> list:
        return self.document.new_databases(sources)

    def commit(self, items: list, text: str) -> None:
        self.document.replace(self.document.maba, "databases", items, text)


class KeyModel(ListModel):
    editable = frozenset({0, 1, 5})

    def items(self) -> list:
        return [] if self.owner is None else self.owner.keys

    def headers(self) -> list[tuple[str, str]]:
        return [(self.tr("Key"), self.tr("Kind, joint and parent joint. The game matches keys by the murmur3 hash of "
                                         "this name and reads the joints' hashes, so a suffix such as >20 belongs to "
                                         "the name only.")),
                (self.tr("Command"), self.tr("Picks the key's class. Type a name, or 0x and a hash.")),
                (self.tr("Class"), self.tr("The class the game creates for this Command.")),
                (self.tr("Joint"), self.tr("JointName: read by OrclKey_Pos, OrclKey_Dir and OrclKey_LocalRot keys "
                                           "with two joints.")),
                (self.tr("Parent Joint"), self.tr("ParentJointName: read with the joint.")),
                (self.tr("Size"), self.tr("KeyValueSize: values per sampled frame. Changing it adds zero columns "
                                          "to every motion or drops its last ones.")),
                (self.tr("Column"), self.tr("KeyValueOffset: where the key's values start in a row."))]

    def data(self, index: QModelIndex, role=_DISPLAY):
        keys, row, column = self.owner.keys, index.row(), index.column()
        key = keys[row]
        joints = key.joints
        if role in (_DISPLAY, _EDIT):
            return (key.name, self.document.command_name(key.command), self.document.key_class(key),
                    joints[0] if joints else "", joints[1] if len(joints) > 1 else "", key.size,
                    sum(held.size for held in keys[:row]))[column]
        if role == _FORE and column in (3, 4) and (
                len(joints) < 2 or self.document.key_class(key) not in self.document.rules.joint_classes):
            return qcolor(current_colors()["text_muted"])
        if role == _TIP and column == 1:
            return f"0x{key.command:08X}"
        return None

    def edit(self, key, column: int, text: str) -> None:
        if column == 0:
            self.document.rename_key(key, text)
        elif column == 1:
            self.document.set_fields(key, self.tr("Set command"), command=self.document.hash_of(text))
        else:
            self.document.set_key_size(self.owner, key, self.whole(text, MAX_KEY_SIZE))

    def make(self, sources: list) -> list:
        return self.document.new_keys(sources)

    def commit(self, items: list, text: str) -> None:
        self.document.replace(self.owner, "keys", items, text)


class RangeModel(ListModel):
    editable = frozenset({0, 1, 2, 3, 5})

    def __init__(self, document: MabaDocument, parent=None):
        super().__init__(document, parent)
        self.motions: dict[tuple[int, int], BankMotion] | None = None
        self.by_name: dict[str, tuple[int, int]] = {}

    def set_motions(self, motions: dict[tuple[int, int], BankMotion] | None) -> None:
        self.motions = motions
        self.by_name = {} if motions is None else {
            motion.name: key for key, motion in sorted(motions.items(), reverse=True)}
        self._changed()

    def items(self) -> list:
        return [] if self.owner is None else self.owner.ranges

    def headers(self) -> list[tuple[str, str]]:
        return [(self.tr("Bank"), self.tr("BankID: the motion bank's ID in the character's motbank.")),
                (self.tr("Motion ID"), self.tr("MotID within that bank.")),
                (self.tr("Motion"), self.tr("Named from the motion bank picked above. Pick a motion to set its bank "
                                            "and ID.")),
                (self.tr("Motion Frames"), self.tr("MotionFrames: every shipped range holds the motion's end frame "
                                                   "+ 1.")),
                (self.tr("Rows"), self.tr("Frames sampled from the motion.")),
                (self.tr("Tags"), self.tr("Tag names, separated by commas, or 0x and a hash. Queries can keep or "
                                          "skip motions by tag."))]

    def data(self, index: QModelIndex, role=_DISPLAY):
        motion, column = self.owner.ranges[index.row()], index.column()
        found = None if self.motions is None else self.motions.get((motion.bank_id, motion.motion_id))
        if role in (_DISPLAY, _EDIT):
            name = "" if self.motions is None else self.tr("Not in the bank") if found is None else found.name
            return (motion.bank_id, motion.motion_id, name, motion.motion_frames, len(motion.frames),
                    ", ".join(map(self.document.tag_name, motion.tags)))[column]
        if role == _FORE and (column == 2 and self.motions is not None and found is None or column == 3 and
                              found is not None and motion.motion_frames != found.end_frame + 1):
            return qcolor(current_colors()["danger"])
        if role == _FORE and column == 5 and any(tag not in self.document.rules.tags for tag in motion.tags):
            return qcolor(current_colors()["text_muted"])
        if role == _TIP and found is not None and column in (2, 3):
            return found.list_path if column == 2 else self.tr("The motion ends at frame {0:g}.").format(
                found.end_frame)
        return None

    def edit(self, motion, column: int, text: str) -> None:
        document = self.document
        if column == 2:
            if self.motions is None:
                raise ValueError(self.tr("Pick a motion bank above to name the motions."))
            if (picked := self.by_name.get(text)) is None:
                raise ValueError(self.tr("The picked motion bank has no motion named {0}.").format(text))
            document.set_fields(motion, self.tr("Set motion"), bank_id=picked[0], motion_id=picked[1])
        elif column == 5:
            document.set_tags(motion, text)
        else:
            field = {0: "bank_id", 1: "motion_id", 3: "motion_frames"}[column]
            document.set_fields(motion, self.tr("Set {0}").format(self.headers()[column][0]),
                                **{field: self.whole(text)})

    def make(self, sources: list) -> list:
        return self.document.new_ranges(self.owner, sources)

    def commit(self, items: list, text: str) -> None:
        self.document.replace(self.owner, "ranges", items, text)


class RowModel(ListModel):
    def flags(self, index: QModelIndex):
        return super().flags(index) | Qt.ItemFlag.ItemIsEditable

    def items(self) -> list:
        return [] if self.owner is None else list(range(len(self.owner[1].frames)))

    def headers(self) -> list[tuple[str, str]]:
        heads = [(self.tr("Frame"), self.tr("FramesIndices: the motion frame this row samples."))]
        if self.owner is not None:
            document = self.document
            heads += [(f"{key.name}\n{component}", f"{key.name} ({document.command_name(key.command)})")
                      for key in self.owner[0].keys for component in document.components(key)]
        return heads

    def data(self, index: QModelIndex, role=_DISPLAY):
        motion, row, column = self.owner[1], index.row(), index.column() - 1
        if role not in (_DISPLAY, _EDIT):
            return None
        return int(motion.frames[row]) if column < 0 else str(np.float32(motion.values[row, column]))

    def edit(self, row: int, column: int, text: str) -> None:
        if column == 0:
            self.document.set_cell(self.owner[1], row, None, self.whole(text))
            return
        try:
            value = np.float32(float(text))
        except ValueError:
            raise ValueError(self.tr("Type a number.")) from None
        self.document.set_cell(self.owner[1], row, column - 1, value)

    def make(self, sources: list) -> list:
        return sources

    def commit(self, items: list, text: str) -> None:
        self.document.reorder_rows(self.owner[1], items, text)
