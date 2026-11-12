"""Binary records described in JSON; see docs/internals/clip-format.md and mot-format.md."""

from __future__ import annotations

import json
import struct

lazy from utils.app_paths import resource_path
lazy from utils.enum_manager import game_enum_definitions

_FORMATS = {"u8": "B", "u16": "H", "i16": "h", "u32": "I", "i32": "i", "u64": "Q", "f32": "f",
            "c8": "Q", "c16": "Q", "node": "Q", "guid": "16s", "f32x3": "3f", "f32x4": "4f", "f32x8": "8f"}
_WIDTHS = {"f32x3": 3, "f32x4": 4, "f32x8": 8}
_INTEGERS = {"u8": (0, 0xFF), "u16": (0, 0xFFFF), "u32": (0, 0xFFFFFFFF), "u64": (0, (1 << 64) - 1),
             "i16": (-0x8000, 0x7FFF), "i32": (-0x80000000, 0x7FFFFFFF)}


def load_table(directory: str, name: str, error: type[Exception]) -> dict:
    with open(resource_path(f"{directory}/{name}.json", required=True), encoding="utf-8") as stream:
        table = json.load(stream)
    if table.get("format") != 1:
        raise error(f"{name}.json has format {table.get('format')}, not 1")
    return table


def merged(families: dict, name: str, deep: tuple[str, ...]) -> dict:
    family = dict(families[name])
    if parent := family.pop("extends", None):
        base = merged(families, parent, deep)
        family = {**base, **family, **{key: {**base.get(key, {}), **family.get(key, {})}
                                       for key in deep if key in base or key in family}}
    return family


def game_enums(enums: dict[str, str], game: str) -> dict[str, dict[int, str]]:
    parts = [name.split(".")[:-1] for name in enums.values()]
    shared = next((index for index, group in enumerate(zip(*parts)) if len(set(group)) > 1), min(map(len, parts)))
    definitions = game_enum_definitions(game, namespace="".join(f"{part}." for part in parts[0][:shared]))
    return {enum: {member["value"]: member["name"] for member in definitions.get(name, ())}
            for enum, name in enums.items()}


class RecordCodec:
    def __init__(self, name: str, size: int, fields: list, error: type[Exception] = ValueError):
        self.name, self.size, self.error = name, size, error
        slots, fmt, at = [], ["<"], 0
        for spec in sorted((_field(item) for item in fields), key=lambda item: item["at"]):
            if spec["at"] < at:
                raise error(f"{name}: field at 0x{spec['at']:X} overlaps")
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
            raise error(f"{name}: fields end at 0x{at:X}, past the record size 0x{size:X}")
        if at < size:
            fmt.append(f"{size - at}s")
            slots.append(("pad", at, size - at))
        self._struct = struct.Struct("".join(fmt))
        self._slots = tuple(slots)
        self.fields = {slot[2]: slot[3] for slot in slots if slot[0] == "field"}
        self.offsets = {slot[2]: slot[1] for slot in slots if slot[0] in ("field", "array")}
        self.strings = tuple((name, kind) for name, kind in self.fields.items() if kind in ("c8", "c16"))
        self.names = frozenset([*self.fields, *(slot[2] for slot in slots if slot[0] == "array"),
                                *(bit[2] for slot in slots if slot[0] == "bits" for bit in slot[2])])
        self.limits = {name: _INTEGERS[kind] for name, kind in self.fields.items() if kind in _INTEGERS}
        self.limits.update((bit[2], (0, (1 << bit[1]) - 1)) for slot in slots if slot[0] == "bits" for bit in slot[2])

    def decode_table(self, data, offset: int, count: int) -> list[dict]:
        view = memoryview(data)[offset:offset + count * self.size]
        if len(view) < count * self.size:
            raise self.error(f"{self.name} table at 0x{offset:X} leaves the data")
        return [self._decode(values, offset + index * self.size)
                for index, values in enumerate(self._struct.iter_unpack(view))]

    def decode(self, data, offset: int) -> dict:
        if offset < 0 or offset + self.size > len(data):
            raise self.error(f"{self.name} at 0x{offset:X} leaves the data")
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
                        raise self.error(f"{self.name}.{field} = {value} does not fit {width} bits")
                    word |= value << lo
                values.append(word)
            else:
                values.append(slot[2] if kind == "const" else 0)
        try:
            return self._struct.pack(*values)
        except struct.error as exc:
            raise self.error(f"{self.name}: {exc}") from None

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
                    raise self.error(f"{self.name} at 0x{offset:X}: nonzero bytes at +0x{slot[1]:X}")
            elif kind == "bits":
                if value & ~slot[3]:
                    raise self.error(f"{self.name} at 0x{offset:X}: unread bits set in +0x{slot[1]:X} "
                                     f"(0x{value & ~slot[3]:X})")
                for lo, width, field in slot[2]:
                    record[field] = (value >> lo) & ((1 << width) - 1)
            elif kind == "const":
                if value != slot[2]:
                    raise self.error(f"{self.name} at 0x{offset:X}: {slot[3]} is {value}, not {slot[2]}")
            elif value:
                raise self.error(f"{self.name} at 0x{offset:X}: {slot[2]} must be zero")
        return record


def _field(item) -> dict:
    if isinstance(item, dict):
        return item
    at, kind, name = item
    return {"at": at, "type": kind, "name": name}


class StreamCodec:
    def __init__(self, name: str, steps: list, error: type[Exception] = ValueError, records: dict | None = None):
        self.name, self.error = name, error
        self._steps = _stream_steps(steps, records or {})
        self.fields = _stream_fields(self._steps, False)
        self.blanks = _stream_blanks(self._steps)
        self.limits = _stream_limits(self._steps)

    def read(self, data, offset: int) -> tuple[dict, int]:
        record = {}
        try:
            return record, self._read(self._steps, data, offset, record, None)
        except struct.error:
            raise self.error(f"{self.name} at 0x{offset:X} leaves the data") from None

    def layout(self, record: dict) -> list[str | tuple[str, ...]]:
        return list(_layout(self._steps, record))

    def write(self, record: dict) -> bytes:
        out = []
        try:
            self._write(self._steps, record, out, None)
        except (struct.error, KeyError, TypeError) as exc:
            raise self.error(f"{self.name}: {exc}") from None
        return b"".join(out)

    def _read(self, steps, data, at, record, count):
        for step in steps:
            kind = step[0]
            if kind == "value":
                _, name, code, size = step
                if count is None:
                    record[name] = struct.unpack_from("<" + code, data, at)[0]
                    at += size
                else:
                    record[name] = list(struct.unpack_from(f"<{count}{code}", data, at))
                    at += count * size
            elif kind == "lists":
                _, name, code, size = step
                items = record[name] = []
                for _ in range(count):
                    length = struct.unpack_from("<I", data, at)[0]
                    items.append(list(struct.unpack_from(f"<{length}{code}", data, at + 4)))
                    at += 4 + length * size
            elif kind in ("group", "items"):
                length = struct.unpack_from("<" + step[1], data, at)[0]
                if length < 0:
                    raise self.error(f"{self.name} at 0x{at:X}: negative count {length}")
                at += step[2]
                if kind == "group":
                    at = self._read(step[3], data, at, record, length)
                    continue
                items = record[step[4]] = []
                for _ in range(length):
                    items.append(item := {})
                    at = self._read(step[3], data, at, item, None)
            elif _holds(step[1], record):
                at = self._read(step[2], data, at, record, count)
        return at

    def _write(self, steps, record, out, count):
        for step in steps:
            kind = step[0]
            if kind in ("value", "lists"):
                value = record[step[1]]
                if count is None:
                    out.append(struct.pack("<" + step[2], value))
                    continue
                if len(value) != count:
                    raise self.error(f"{self.name}.{step[1]} has {len(value)} items, not {count}")
                if kind == "value":
                    out.append(struct.pack(f"<{count}{step[2]}", *value))
                else:
                    out.extend(struct.pack(f"<I{len(item)}{step[2]}", len(item), *item) for item in value)
            elif kind == "group":
                length = self._length(step[3], record)
                out.append(struct.pack("<" + step[1], length))
                self._write(step[3], record, out, length)
            elif kind == "items":
                out.append(struct.pack("<" + step[1], len(record[step[4]])))
                for item in record[step[4]]:
                    self._write(step[3], item, out, None)
            elif _holds(step[1], record):
                self._write(step[2], record, out, count)

    def _length(self, steps, record) -> int | None:
        for step in steps:
            if step[0] in ("value", "lists"):
                return len(record[step[1]])
            if step[0] == "if" and _holds(step[1], record) and (length := self._length(step[2], record)) is not None:
                return length
        return None


_TESTS = {"&": lambda value, mask: bool(value & mask), "!&": lambda value, mask: not value & mask,
          "<": lambda value, limit: value < limit}


def _holds(tests, record) -> bool:
    return all(test(record[field], operand) for field, test, operand in tests)


def _stream_fields(steps, counted: bool) -> dict[str, str]:
    fields = {}
    for step in steps:
        if step[0] in ("value", "lists"):
            fields[step[1]] = "list" if counted or step[0] == "lists" else "value"
        elif step[0] == "group":
            fields.update(_stream_fields(step[3], True))
        elif step[0] == "items":
            fields[step[4]] = "list"
        else:
            fields.update(_stream_fields(step[2], counted))
    return fields


def _stream_limits(steps) -> dict[str, tuple[int, int]]:
    codes = {_FORMATS[kind]: limits for kind, limits in _INTEGERS.items()}
    limits = {}
    for step in steps:
        if step[0] in ("value", "lists"):
            if step[2] in codes:
                limits[step[1]] = codes[step[2]]
        elif step[0] != "items":
            limits.update(_stream_limits(step[3] if step[0] == "group" else step[2]))
    return limits


def _layout(steps, record: dict):
    for step in steps:
        if step[0] in ("value", "lists", "items"):
            yield step[4] if step[0] == "items" else step[1]
        elif step[0] == "group":
            yield tuple(_layout(step[3], record))
        elif _holds(step[1], record):
            yield from _layout(step[2], record)


def _stream_blanks(steps, item: bool = False) -> dict:
    blanks = {}
    for step in steps:
        if step[0] in ("value", "lists"):
            blanks[step[1]] = [] if step[0] == "lists" else 0.0 if step[2] == "f" else 0
        elif step[0] == "items":
            blanks[step[4]] = _stream_blanks(step[3], True)
        elif step[0] == "group" and item:
            blanks.update({name: [] for name in _stream_fields(step[3], True)})
        else:
            blanks.update(_stream_blanks(step[3] if step[0] == "group" else step[2], item))
    return blanks


def _stream_steps(steps, records: dict) -> tuple:
    out = []
    for step in steps:
        if isinstance(step, list):
            kind, name = step
            code = _FORMATS[kind.removesuffix("[]")]
            out.append(("lists" if kind.endswith("[]") else "value", name, code, struct.calcsize("<" + code)))
        elif "record" in step:
            out.extend(_stream_steps(records[step["record"]], records))
        elif "count" in step:
            code = _FORMATS[step["count"]]
            nested = _stream_steps(records[step["of"]] if isinstance(step["of"], str) else step["of"], records)
            out.append(("items" if "items" in step else "group", code, struct.calcsize("<" + code), nested,
                        step.get("items")))
        else:
            out.append(("if", tuple((field, _TESTS[op], operand) for field, op, operand in step["if"]),
                        _stream_steps(step["then"], records)))
    return tuple(out)
