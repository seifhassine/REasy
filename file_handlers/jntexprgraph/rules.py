"""See docs/internals/jntexprgraph-format.md."""

from __future__ import annotations

import functools
import struct
from dataclasses import dataclass

lazy from file_handlers.layout_codec import RecordCodec, load_table
lazy from utils.hash_util import murmur3_hash_ascii

from .errors import ExprGraphFormatError


@dataclass(frozen=True, slots=True)
class Layouts:
    versions: dict[int, bytes]
    align: dict[str, int]
    values: dict[int, struct.Struct]
    records: dict[str, RecordCodec]
    extras: dict[int, dict]


@functools.cache
def layouts() -> Layouts:
    table = load_table("resources/data/jntexprgraph", "layouts", ExprGraphFormatError)
    extras = {}
    for name, spec in table["extras"].items():
        if "code" in spec:
            spec = {"code": {int(op): (word, struct.Struct("<" + fmt)) for op, (word, fmt) in spec["code"].items()}}
            spec["opcodes"] = {word: op for op, (word, _) in spec["code"].items()}
        extras[murmur3_hash_ascii(name)] = spec
    return Layouts({int(version): entry["magic"].encode() for version, entry in table["versions"].items()},
                   table["align"], {int(code): struct.Struct("<" + fmt) for code, fmt in table["values"].items()},
                   {name: RecordCodec(name, spec["size"], spec["fields"], ExprGraphFormatError)
                    for name, spec in table["records"].items()}, extras)


def value_struct(code: int) -> struct.Struct:
    found = layouts().values.get(code)
    if found is None:
        raise ExprGraphFormatError(f"value type {code} has no layout")
    return found
