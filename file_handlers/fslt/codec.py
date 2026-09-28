from __future__ import annotations

import struct

from .model import SLOT_COUNT, FsltCharRule, FsltData, FsltFont, FsltSlot

FSLT_MAGIC = b"FSLT"
SUPPORTED_VERSIONS = (2, 3, 4, 5, 6)
_FONT = struct.Struct("<4fQ")
_RULE_TAIL = struct.Struct("<2f4I")


class FsltFormatError(ValueError):
    pass


def _string_at(data: bytes, offset: int) -> str:
    if offset & 1 or not 0 < offset < len(data):
        raise FsltFormatError(f"FSLT string offset 0x{offset:X} is invalid")
    end = offset
    while end + 1 < len(data):
        if data[end : end + 2] == b"\0\0":
            try:
                return data[offset:end].decode("utf-16-le")
            except UnicodeDecodeError as exc:
                raise FsltFormatError(
                    f"FSLT string at 0x{offset:X} is not UTF-16LE"
                ) from exc
        end += 2
    raise FsltFormatError(f"FSLT string at 0x{offset:X} is not terminated")


def decode_fslt(data: bytes) -> FsltData:
    if len(data) < 8:
        raise FsltFormatError("file is too small for an FSLT header")
    version, magic = struct.unpack_from("<I4s", data, 0)
    if magic != FSLT_MAGIC:
        raise FsltFormatError(f"expected FSLT magic, got {magic!r}")
    if version not in SUPPORTED_VERSIONS:
        raise FsltFormatError(f"unsupported FSLT version {version}")
    table, extra = 8, 0
    if version == 6:
        if len(data) < 0x90:
            raise FsltFormatError("file is too small for an FSLT v6 header")
        (extra,) = struct.unpack_from("<Q", data, 8)
        table = 0x10
    offsets = struct.unpack_from(f"<{SLOT_COUNT}Q", data, table)

    def ptrs(base: int, count: int) -> list[int]:
        return [struct.unpack_from("<Q", data, base + 8 * j)[0] for j in range(count)]

    def fonts_at(addrs: list[int]) -> list[FsltFont]:
        out = []
        for p in addrs:
            rec = _FONT.unpack_from(data, p)
            out.append(FsltFont(rec[:4], _string_at(data, rec[4])))
        return out

    def rules_at(addrs: list[int]) -> list[FsltCharRule]:
        out = []
        for p in addrs:
            tail = _RULE_TAIL.unpack_from(data, p + (24 if version == 4 else 16))
            if version == 4:
                names = struct.unpack_from("<3Q", data, p)
                out.append(
                    FsltCharRule(
                        tuple(_string_at(data, n) for n in names), 0, tail[:2], tail[2:]
                    )
                )
            else:
                reserved, name = struct.unpack_from("<2Q", data, p)
                out.append(
                    FsltCharRule((_string_at(data, name),), reserved, tail[:2], tail[2:])
                )
        return out

    slots: list[FsltSlot] = []
    for index, entry in enumerate(offsets):
        if entry + 8 > len(data):
            raise FsltFormatError(f"FSLT slot {index} entry offset 0x{entry:X} is invalid")
        if version == 2:
            (count,) = struct.unpack_from("<I4x", data, entry)
            if entry + 8 + 24 * count > len(data):
                raise FsltFormatError(f"FSLT slot {index} fonts overrun the file")
            slots.append(
                FsltSlot(fonts=fonts_at([entry + 8 + 24 * j for j in range(count)]))
            )
        elif version == 3:
            n1, n2 = struct.unpack_from("<II", data, entry)
            o1, o2 = struct.unpack_from("<QQ", data, entry + 8)
            slots.append(FsltSlot(fonts_at(ptrs(o1, n1)), rules_at(ptrs(o2, n2))))
        else:
            n1, n2, n3 = struct.unpack_from("<III", data, entry)
            o1, o2, o3 = struct.unpack_from("<QQQ", data, entry + 0x10)
            slots.append(
                FsltSlot(
                    fonts_at(ptrs(o1, n1)), rules_at(ptrs(o2, n2)), rules_at(ptrs(o3, n3))
                )
            )
    return FsltData(version, extra, slots)


def encode_fslt(model: FsltData) -> bytes:
    version = model.version
    if version not in SUPPORTED_VERSIONS:
        raise FsltFormatError(f"unsupported FSLT version {version}")
    if len(model.slots) != SLOT_COUNT:
        raise FsltFormatError(f"expected {SLOT_COUNT} slots, got {len(model.slots)}")
    head = 0x90 if version == 6 else 0x88
    out = bytearray(b"\0" * head)
    patches: list[tuple[int, str]] = []

    def write_rule(rule: FsltCharRule, at: int) -> None:
        if version == 4:
            if len(rule.texts) != 3:
                raise FsltFormatError("v4 rules carry exactly 3 texts")
            for k, text in enumerate(rule.texts):
                patches.append((at + 8 * k, text))
            struct.pack_into("<2f4I", out, at + 24, *rule.adjust, *rule.params)
        else:
            if len(rule.texts) != 1:
                raise FsltFormatError("this FSLT version carries 1 rule text")
            struct.pack_into("<2Q", out, at, rule.reserved, 0)
            patches.append((at + 8, rule.texts[0]))
            struct.pack_into("<2f4I", out, at + 16, *rule.adjust, *rule.params)

    if version == 2:
        entries = []
        for slot in model.slots:
            entries.append(len(out))
            out += struct.pack("<I4x", len(slot.fonts))
            for font in slot.fonts:
                out += _FONT.pack(*font.adjust, 0)
                patches.append((len(out) - 8, font.path))
    else:
        esz = 24 if version == 3 else 40
        rsz = 48 if version == 4 else 40
        entries = [head + esz * i for i in range(SLOT_COUNT)]
        plan = [
            [(slot.fonts, 24), (slot.rules, rsz)]
            + ([] if version == 3 else [(slot.tail_rules, rsz)])
            for slot in model.slots
        ]
        pos = head + esz * SLOT_COUNT
        arrays = []
        for groups in plan:
            row = []
            for items, _ in groups:
                row.append(pos)
                pos += 8 * max(len(items), 1)
            arrays.append(row)
        obj = pos
        for groups in plan:
            for items, size in groups:
                pos += size * len(items)
        out += b"\0" * (pos - len(out))
        for i, (groups, row) in enumerate(zip(plan, arrays)):
            fmt = "<IIQQ" if version == 3 else "<III4xQQQ"
            struct.pack_into(fmt, out, entries[i], *(len(items) for items, _ in groups), *row)
        for groups, row in zip(plan, arrays):
            for (items, size), arr in zip(groups, row):
                for j in range(max(len(items), 1)):
                    struct.pack_into(
                        "<Q", out, arr + 8 * j, obj + size * j if j < len(items) else 0
                    )
                for j, item in enumerate(items):
                    at = obj + size * j
                    if isinstance(item, FsltFont):
                        struct.pack_into("<4fQ", out, at, *item.adjust, 0)
                        patches.append((at + 16, item.path))
                    else:
                        write_rule(item, at)
                obj += size * len(items)
    texts = [f.path for s in model.slots for f in s.fonts]
    for group in ("rules", "tail_rules"):
        texts += [t for s in model.slots for r in getattr(s, group) for t in r.texts]
    strings: dict[str, int] = {}
    for text in texts:
        if text not in strings:
            strings[text] = len(out)
            out += text.encode("utf-16-le") + b"\0\0"
    for at, text in patches:
        struct.pack_into("<Q", out, at, strings[text])
    struct.pack_into("<I4s", out, 0, version, FSLT_MAGIC)
    if version == 6:
        struct.pack_into("<Q", out, 8, model.extra)
    struct.pack_into(f"<{SLOT_COUNT}Q", out, 0x10 if version == 6 else 8, *entries)
    return bytes(out)
