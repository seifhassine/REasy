"""See docs/internals/efcsv-format.md."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

_HEADER = struct.Struct("<II")


@dataclass(slots=True)
class EfcsvFile:
    columns: int = 3
    rows: list[tuple[float, ...]] = field(default_factory=list)


def read_efcsv(data: bytes) -> EfcsvFile:
    if len(data) < _HEADER.size:
        raise ValueError("an .efcsv file starts with its row and column counts")
    rows, columns = _HEADER.unpack_from(data)
    if len(data) < _HEADER.size + 4 * rows * columns:
        raise ValueError(f"the file is shorter than its {rows} rows of {columns} values")
    values = struct.unpack_from(f"<{rows * columns}f", data, _HEADER.size)
    return EfcsvFile(columns, [values[at * columns:(at + 1) * columns] for at in range(rows)])


def write_efcsv(table: EfcsvFile) -> bytes:
    if any(len(row) != table.columns for row in table.rows):
        raise ValueError(f"every row holds the table's {table.columns} values")
    return _HEADER.pack(len(table.rows), table.columns) + struct.pack(
        f"<{len(table.rows) * table.columns}f", *(value for row in table.rows for value in row))


def table_shape(data: bytes) -> tuple[int, int] | None:
    try:
        table = read_efcsv(data)
    except ValueError:
        return None
    return len(table.rows), table.columns
