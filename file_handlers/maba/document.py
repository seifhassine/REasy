"""See docs/internals/maba-format.md, Editing."""
from __future__ import annotations

import re

import numpy as np
from PySide6.QtCore import QObject
from PySide6.QtGui import QUndoCommand

from file_handlers.document_commands import Change, EditDocument, ReplaceContents, SetFields
from utils.hash_util import murmur3_hash_utf16le

from .maba_file import VERSION, MabaFile, MotionRange, OrclDatabase, OrclKey, read_maba, write_maba
from .rules import maba_rules

_HASH = re.compile(r"0x([0-9a-fA-F]{1,8})")
_NUMBERED = re.compile(r"(.*?)(\d*)")
MAX_KEY_SIZE = 255


def _state(database: OrclDatabase) -> tuple:
    return (list(database.keys), [(key, key.size) for key in database.keys],
            [(motion, motion.values) for motion in database.ranges])


def _copy(motion: MotionRange) -> MotionRange:
    return MotionRange(motion.bank_id, motion.motion_id, motion.motion_frames, list(motion.tags), motion.frames.copy(),
                       motion.values.copy(), motion.tags_shared_with)


def _same(held, value) -> bool:
    return np.array_equal(held, value) if isinstance(held, np.ndarray) else held == value


class _Rekey(QUndoCommand):
    def __init__(self, document: MabaDocument, database: OrclDatabase, keys: list[OrclKey], sizes: dict, text: str):
        super().__init__(text)
        self.document, self.database, self.before = document, database, _state(database)
        columns, at = {}, 0
        for key in database.keys:
            columns[id(key)], at = (at, key.size), at + key.size
        values = []
        for motion in database.ranges:
            pieces = [np.zeros((len(motion.frames), 0), np.float32)]
            for key in keys:
                start, held = columns.get(id(key), (0, 0))
                size = sizes.get(id(key), key.size)
                pieces += [motion.values[:, start:start + min(held, size)],
                           np.zeros((len(motion.frames), max(size - held, 0)), np.float32)]
            values.append((motion, np.concatenate(pieces, axis=1)))
        self.after = list(keys), [(key, sizes.get(id(key), key.size)) for key in keys], values

    def redo(self) -> None:
        self._apply(self.after)

    def undo(self) -> None:
        self._apply(self.before)

    def _apply(self, state: tuple) -> None:
        keys, sizes, values = state
        self.database.keys[:] = keys
        for key, size in sizes:
            key.size = size
        for motion, held in values:
            motion.values = held
        self.document.changed.emit(Change(self.database, "keys", True))


class _Values(QUndoCommand):
    def __init__(self, document: MabaDocument, values: list[tuple[MotionRange, np.ndarray]], text: str):
        super().__init__(text)
        self.document, self.after = document, values
        self.before = [(motion, motion.values) for motion, _held in values]

    def redo(self) -> None:
        self._apply(self.after)

    def undo(self) -> None:
        self._apply(self.before)

    def _apply(self, values: list[tuple[MotionRange, np.ndarray]]) -> None:
        for motion, held in values:
            motion.values = held
        self.document.changed.emit(Change(self.document.maba, "values"))


class MabaDocument(EditDocument):
    def __init__(self, maba: MabaFile, parent: QObject | None = None):
        super().__init__(parent)
        self.maba, self.rules = maba, maba_rules(VERSION)

    @classmethod
    def read(cls, data: bytes, parent: QObject | None = None) -> MabaDocument:
        return cls(read_maba(data), parent)

    def write(self) -> bytes:
        return write_maba(self.maba)

    @property
    def databases(self) -> list[OrclDatabase]:
        return self.maba.databases

    def command_name(self, command: int) -> str:
        return found[0] if (found := self.rules.commands.get(command)) else f"0x{command:08X}"

    def key_class(self, key: OrclKey) -> str:
        return found[1] if (found := self.rules.commands.get(key.command)) else self.rules.default_class

    def components(self, key: OrclKey) -> list[str]:
        letters = found[2] if (found := self.rules.commands.get(key.command)) else ""
        return list(letters) if len(letters) == key.size else [str(at) for at in range(key.size)]

    def tag_name(self, tag: int) -> str:
        return self.rules.tags.get(tag, f"0x{tag:08X}")

    def hash_of(self, text: str) -> int:
        if not (text := text.strip()):
            raise ValueError(self.tr("Type a name, or 0x and the name's hash."))
        return int(found.group(1), 16) if (found := _HASH.fullmatch(text)) else murmur3_hash_utf16le(text)

    def set_fields(self, target, text: str, **values) -> None:
        if not all(_same(getattr(target, name), value) for name, value in values.items()):
            self.push(SetFields(self, [(target, values)], text))

    def rename_database(self, database: OrclDatabase, text: str) -> None:
        if not (name := text.strip()):
            raise ValueError(self.tr("Type a database name."))
        self.set_fields(database, self.tr("Rename database"), name=name)

    def rename_key(self, key: OrclKey, text: str) -> None:
        if not (name := " ".join(text.split())):
            raise ValueError(self.tr("Type the key's kind and joints, as in pos L_Leg_Foot root."))
        self.set_fields(key, self.tr("Rename key"), name=name)

    def set_key_size(self, database: OrclDatabase, key: OrclKey, size: int) -> None:
        if not 0 <= size <= MAX_KEY_SIZE:
            raise ValueError(self.tr("A key holds 0 to {0} values.").format(MAX_KEY_SIZE))
        if size != key.size:
            self.push(_Rekey(self, database, database.keys, {id(key): size}, self.tr("Set key size")))

    def set_tags(self, motion: MotionRange, text: str) -> None:
        self.set_fields(motion, self.tr("Set tags"), tags=[self.hash_of(part) for part in text.split(",")
                                                           if part.strip()])

    def set_cell(self, motion: MotionRange, row: int, column: int | None, value) -> None:
        if column is None:
            frames = motion.frames.copy()
            frames[row] = value
            self.set_fields(motion, self.tr("Set frame"), frames=frames)
        else:
            values = motion.values.copy()
            values[row, column] = value
            self.set_fields(motion, self.tr("Set value"), values=values)

    def apply_samples(self, motions: list[MotionRange], values: dict[int, np.ndarray]) -> None:
        if changed := [(motion, held) for motion in motions if (held := values.get(id(motion))) is not None and
                       not np.array_equal(held, motion.values)]:
            self.push(_Values(self, changed, self.tr("Sample values")))

    def reorder_rows(self, motion: MotionRange, order: list[int | None], text: str) -> None:
        width = motion.values.shape[1]
        self.set_fields(motion, text, frames=np.array([0 if row is None else motion.frames[row] for row in order],
                                                      np.uint32),
                        values=np.array([np.zeros(width, np.float32) if row is None else motion.values[row]
                                         for row in order], np.float32).reshape(len(order), width))

    def new_databases(self, sources: list[OrclDatabase | None]) -> list[OrclDatabase]:
        taken, made = {database.name for database in self.databases}, []
        for source in sources:
            stem, digits = _NUMBERED.fullmatch("Database" if source is None else source.name).groups()
            number = int(digits or 0) + 1
            while (name := f"{stem}{number:0{len(digits)}d}") in taken:
                number += 1
            taken.add(name)
            made.append(OrclDatabase(name) if source is None else OrclDatabase(
                name, self.new_keys(source.keys), [_copy(motion) for motion in source.ranges]))
        return made

    def new_keys(self, sources: list[OrclKey | None]) -> list[OrclKey]:
        command, (name, _, components) = next(iter(self.rules.commands.items()))
        return [OrclKey(f"{name} root root", command, len(components)) if source is None else
                OrclKey(source.name, source.command, source.size) for source in sources]

    def new_ranges(self, database: OrclDatabase, sources: list[MotionRange | None]) -> list[MotionRange]:
        return [MotionRange(0, 0, 0, [], np.zeros(1, np.uint32), np.zeros((1, database.frame_size), np.float32))
                if source is None else _copy(source) for source in sources]

    def replace(self, owner, field: str, contents: list, text: str) -> None:
        if [*map(id, contents)] != [*map(id, getattr(owner, field))]:
            self.push(_Rekey(self, owner, contents, {}, text) if field == "keys" else
                      ReplaceContents(self, owner, field, contents, text))
