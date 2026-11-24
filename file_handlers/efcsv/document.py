"""See docs/internals/efcsv-format.md, Editing."""
from __future__ import annotations

import math

from PySide6.QtCore import QObject

from file_handlers.document_commands import EditDocument, ReplaceContents
lazy from file_handlers.native_math import f32

from .efcsv_file import EfcsvFile, read_efcsv, write_efcsv


class EfcsvDocument(EditDocument):
    def __init__(self, table: EfcsvFile, parent: QObject | None = None):
        super().__init__(parent)
        self.table = table

    @classmethod
    def read(cls, data: bytes, parent: QObject | None = None) -> EfcsvDocument:
        return cls(read_efcsv(data), parent)

    def write(self) -> bytes:
        return write_efcsv(self.table)

    def _replace(self, rows: list[tuple[float, ...]], text: str) -> None:
        self.push(ReplaceContents(self, self.table, "rows", rows, text))

    def set_value(self, row: int, column: int, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError(self.tr("Type a finite number."))
        held = self.table.rows[row]
        self._replace([*self.table.rows[:row], (*held[:column], f32(value), *held[column + 1:]),
                       *self.table.rows[row + 1:]], self.tr("Set value"))

    def insert(self, row: int) -> None:
        self._replace([*self.table.rows[:row], (0.0,) * self.table.columns, *self.table.rows[row:]],
                      self.tr("Add row"))

    def remove(self, rows: list[int]) -> None:
        gone = set(rows)
        self._replace([held for at, held in enumerate(self.table.rows) if at not in gone], self.tr("Delete rows"))
