"""See docs/internals/bhvt-format.md."""

from __future__ import annotations

import struct

lazy from file_handlers.uvar.base_model import FileHandler
lazy from file_handlers.uvar.uvar_file import UVarFile

from .errors import BhvtFormatError

U32 = struct.Struct("<I")


def aligned(at: int, align: int) -> int:
    return at + -at % align


def signed(value: int) -> int:
    return (value ^ 0x80000000) - 0x80000000


def stops(starts: dict[str, int], end: int) -> dict[str, int]:
    bounds = sorted({*starts.values(), end})
    return {name: next(bound for bound in bounds if bound > at) for name, at in starts.items()}


def read_u32s(data, at: int) -> tuple[list[int], int]:
    count = U32.unpack_from(data, at)[0]
    return list(struct.unpack_from(f"<{count}I", data, at + 4)), at + 4 + 4 * count


def read_names(data, at: int, stop: int, name: str) -> str:
    count = U32.unpack_from(data, at)[0]
    padding(data, at + 4 + 2 * count, stop, name)
    return bytes(data[at + 4:at + 4 + 2 * count]).decode("utf-16le")


def name_at(pool: str, offset: int) -> str:
    return pool[offset:pool.index("\0", offset)]


def read_pool(data, at: int, base: int, width: int) -> list[str]:
    count = U32.unpack_from(data, at)[0]
    if not count:
        return []
    chars = U32.unpack_from(data, at + 4)[0]
    pool = bytes(data[at + 8:at + 8 + 2 * chars]).decode("utf-16le")
    items = pool[:-1].split("\0")
    if not pool.endswith("\0") or len(items) != count * width:
        raise BhvtFormatError(f"path pool at 0x{at - base:X} does not hold {count} entries")
    return items


def read_uvar(data, at: int, base: int):
    handler = FileHandler(data, base)
    handler.seek(at)
    uvar = UVarFile()
    uvar.do_read(handler)
    return uvar


def padding(data, at: int, stop: int, name: str) -> None:
    if any(data[at:stop]) or stop - at >= 16:
        raise BhvtFormatError(f"{name}: {stop - at} unread bytes before the next section")


def offsets_of(texts) -> dict[str, int]:
    offsets, at = {}, 0
    for text in texts:
        if text not in offsets:
            offsets[text] = at
            at += len(text) + 1
    return offsets


def path_offsets(paths: list[str]) -> list[int]:
    offsets, at = [], 0
    for path in paths:
        offsets.append(at)
        at += len(path) + 1
    return offsets


def write_names(offsets: dict[str, int], out: bytearray) -> None:
    pool = "".join(f"{text}\0" for text in offsets)
    out += U32.pack(len(pool)) + pool.encode("utf-16le")


def write_pool(count: int, items: list[str], out: bytearray) -> None:
    out += U32.pack(count)
    if items:
        pool = "".join(f"{text}\0" for text in items)
        out += U32.pack(len(pool)) + pool.encode("utf-16le")


def write_uvar(uvar, out: bytearray) -> None:
    handler = FileHandler(out)
    handler.seek(len(out))
    uvar.do_write(handler)


READERS = {"rsz": lambda data, at, stop, base, rules, rsz: rsz.read(bytes(data[at:stop])),
           "names": lambda data, at, stop, base, rules, rsz: read_names(data, at, stop, "names"),
           "paths": lambda data, at, stop, base, rules, rsz: read_pool(data, at, base, 1),
           "uvar": lambda data, at, stop, base, rules, rsz: read_uvar(data, at, base)}
WRITERS = {"rsz": lambda model, section, out, rules, rsz, derived: out.extend(rsz.write(model.blocks[section.name])),
           "uvar": lambda model, section, out, rules, rsz, derived: write_uvar(model.variable, out)}
