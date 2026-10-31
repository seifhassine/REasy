"""See docs/internals/clip-format.md."""

from __future__ import annotations

import struct


class ClipFormatError(ValueError):
    pass


_FORMATS = {"u8": "B", "u16": "H", "i16": "h", "u32": "I", "i32": "i", "u64": "Q", "f32": "f",
            "c8": "Q", "c16": "Q", "node": "Q", "guid": "16s", "f32x3": "3f", "f32x4": "4f", "f32x8": "8f"}
_WIDTHS = {"f32x3": 3, "f32x4": 4, "f32x8": 8}
_INTEGERS = {"u8": (0, 0xFF), "u16": (0, 0xFFFF), "u32": (0, 0xFFFFFFFF), "u64": (0, (1 << 64) - 1),
             "i16": (-0x8000, 0x7FFF), "i32": (-0x80000000, 0x7FFFFFFF)}


class RecordCodec:
    def __init__(self, name: str, size: int, fields: list):
        self.name, self.size = name, size
        slots, fmt, at = [], ["<"], 0
        for spec in sorted((_field(item) for item in fields), key=lambda item: item["at"]):
            if spec["at"] < at:
                raise ClipFormatError(f"{name}: field at 0x{spec['at']:X} overlaps")
            if spec["at"] > at:
                fmt.append(f"{spec['at'] - at}s")
                slots.append(("pad", at, spec["at"] - at))
            kind = spec["type"]
            fmt.append(_FORMATS[kind])
            if "bits" in spec:
                bits = tuple((lo, width, field) for lo, width, field in spec["bits"])
                mask = sum(((1 << width) - 1) << lo for lo, width, _ in bits)
                slots.append(("bits", spec["at"], bits, mask))
            elif "const" in spec:
                slots.append(("const", spec["at"], spec["const"], spec.get("what", "")))
            elif "zero" in spec:
                slots.append(("zero", spec["at"], spec["zero"]))
            elif kind in _WIDTHS:
                slots.append(("array", spec["at"], spec["name"], _WIDTHS[kind]))
            else:
                slots.append(("field", spec["at"], spec["name"], kind))
            at = spec["at"] + struct.calcsize("<" + _FORMATS[kind])
        if at > size:
            raise ClipFormatError(f"{name}: fields end at 0x{at:X}, past the record size 0x{size:X}")
        if at < size:
            fmt.append(f"{size - at}s")
            slots.append(("pad", at, size - at))
        self._struct = struct.Struct("".join(fmt))
        self._slots = tuple(slots)
        self.fields = {slot[2]: slot[3] for slot in slots if slot[0] == "field"}
        self.strings = tuple((name, kind) for name, kind in self.fields.items() if kind in ("c8", "c16"))
        self.names = frozenset([*self.fields, *(slot[2] for slot in slots if slot[0] == "array"),
                                *(bit[2] for slot in slots if slot[0] == "bits" for bit in slot[2])])
        self.limits = {name: _INTEGERS[kind] for name, kind in self.fields.items() if kind in _INTEGERS}
        self.limits.update((bit[2], (0, (1 << bit[1]) - 1)) for slot in slots if slot[0] == "bits" for bit in slot[2])

    def decode_table(self, data, offset: int, count: int) -> list[dict]:
        view = memoryview(data)[offset:offset + count * self.size]
        return [self._decode(values, offset + index * self.size)
                for index, values in enumerate(self._struct.iter_unpack(view))]

    def decode(self, data, offset: int) -> dict:
        return self._decode(self._struct.unpack_from(data, offset), offset)

    def encode(self, record: dict) -> bytes:
        values = []
        for slot in self._slots:
            kind = slot[0]
            if kind == "pad":
                values.append(bytes(slot[2]))
            elif kind == "field":
                values.append(record[slot[2]])
            elif kind == "array":
                values.extend(record[slot[2]])
            elif kind == "bits":
                word = 0
                for lo, width, field in slot[2]:
                    value = int(record[field])
                    if not 0 <= value < 1 << width:
                        raise ClipFormatError(f"{self.name}.{field} = {value} does not fit {width} bits")
                    word |= value << lo
                values.append(word)
            else:
                values.append(slot[2] if kind == "const" else 0)
        try:
            return self._struct.pack(*values)
        except struct.error as exc:
            raise ClipFormatError(f"{self.name}: {exc}") from None

    def _decode(self, values: tuple, offset: int) -> dict:
        record, position = {}, 0
        for slot in self._slots:
            kind = slot[0]
            if kind == "array":
                record[slot[2]] = values[position:position + slot[3]]
                position += slot[3]
                continue
            value = values[position]
            position += 1
            if kind == "field":
                record[slot[2]] = value
            elif kind == "pad":
                if any(value):
                    raise ClipFormatError(f"{self.name} at 0x{offset:X}: nonzero bytes at +0x{slot[1]:X}")
            elif kind == "bits":
                if value & ~slot[3]:
                    raise ClipFormatError(f"{self.name} at 0x{offset:X}: unread bits set in +0x{slot[1]:X} "
                                          f"(0x{value & ~slot[3]:X})")
                for lo, width, field in slot[2]:
                    record[field] = (value >> lo) & ((1 << width) - 1)
            elif kind == "const":
                if value != slot[2]:
                    raise ClipFormatError(f"{self.name} at 0x{offset:X}: {slot[3]} is {value}, not {slot[2]}")
            elif value:
                raise ClipFormatError(f"{self.name} at 0x{offset:X}: {slot[2]} must be zero")
        return record


def _field(item) -> dict:
    if isinstance(item, dict):
        return item
    at, kind, name = item
    return {"at": at, "type": kind, "name": name}
