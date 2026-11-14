"""See docs/internals/gui-format.md."""

from __future__ import annotations

import math
import struct
import uuid
from pathlib import Path

lazy from file_handlers.clip.decode import MAGIC as CLIP_MAGIC, read_motclip
lazy from file_handlers.clip.errors import ClipFormatError
lazy from file_handlers.clip.rules import PropertyType

from .errors import GuiFormatError
from .model import (
    GUI_MAGIC,
    NINE_SLICE_CELL_NAMES,
    GuiAnimation,
    GuiBinding,
    GuiDocument,
    GuiNineSlice,
    GuiNineSliceCell,
    GuiObject,
    GuiProperty,
    GuiSymbol,
    GuiTextureEntry,
    GuiTextureSet,
)
lazy from .profiles import gui_runtime
from .rules import STRINGS, gui_layout
from .storage import Stored, merged


def gui_version(data: bytes) -> int:
    if len(data) < 8 or data[4:8] != GUI_MAGIC:
        raise GuiFormatError("expected GUIR magic")
    return int.from_bytes(data[:4], "little")


def parse_gui(data: bytes, source: str = "<bytes>") -> GuiDocument:
    try:
        return _Reader(bytes(data)).document(source)
    except (GuiFormatError, ClipFormatError) as exc:
        raise GuiFormatError(f"{source}: {exc}") from exc


def parse_gui_file(path: str | Path) -> GuiDocument:
    source = Path(path)
    return parse_gui(source.read_bytes(), str(source))


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.layout = gui_layout(gui_version(data))
        self.records = self.layout.records
        self.objects: dict[int, GuiObject] = {}
        self.animations: dict[int, GuiAnimation] = {}
        self.ends: dict[int, int] = {}
        self.transitions: dict[int, int] = {}
        self.declared = 0

    def document(self, source: str) -> GuiDocument:
        header = self.layout.header.decode(self.data, 0)
        root = self.records["root"].decode(self.data, header["root"])
        symbols_at = header["root"] + self.records["root"].size
        symbol_offsets = self.pointers(symbols_at, root["count"])
        if root["first_symbol"] != (symbol_offsets[0] if symbol_offsets else 0):
            raise GuiFormatError("the root's first-symbol pointer disagrees with its symbol table")
        self.declared = symbols_at + len(symbol_offsets) * self.records["pointer"].size
        symbols = [self.symbol(offset) for offset in symbol_offsets]
        root_object = self.object(root["root_object"]) if root["root_object"] else None
        objects, animations = self.declarations(header["metadata"])
        for offset, target in self.transitions.items():
            if target:
                if target not in self.animations:
                    raise GuiFormatError("an animation transition targets an unowned descriptor")
                self.animations[offset].transition = self.animations[target]
        if self.records["list"].decode(self.data, header["metadata"])["count"]:
            raise GuiFormatError("type/field metadata records are not understood")
        document = GuiDocument(
            version=self.layout.version,
            root_object=root_object,
            symbols=symbols,
            bindings=self.bindings(header["bindings"]),
            imported_gui_paths=[row["path"] for row in self.table("path", header["imports"])],
            source=source,
            objects=objects,
            animations=animations,
        )
        try:
            document.validate_relationships()
        except ValueError as exc:
            raise GuiFormatError(str(exc)) from exc
        return document

    def read(self, name: str, offset: int) -> dict:
        codec = self.records[name]
        return self.strings(codec, codec.decode(self.data, offset))

    def table(self, name: str, offset: int) -> list[dict]:
        listing, codec = self.records["list"], self.records[name]
        count = listing.decode(self.data, offset)["count"]
        return [self.strings(codec, row) for row in codec.decode_table(self.data, offset + listing.size, count)]

    def strings(self, codec, record: dict) -> dict:
        for field, kind in codec.strings:
            record[field] = self.text(record[field], kind)
        return record

    def text(self, offset: int, kind: str) -> str:
        encoding, terminator = STRINGS[kind]
        end = self.data.find(terminator, offset)
        while end >= 0 and (end - offset) % len(terminator):
            end = self.data.find(terminator, end + 1)
        if end < 0:
            raise GuiFormatError(f"the string at 0x{offset:X} is not terminated")
        try:
            return self.data[offset:end].decode(encoding)
        except UnicodeDecodeError as exc:
            raise GuiFormatError(f"the string at 0x{offset:X} is not {encoding}") from exc

    def pointers(self, offset: int, count: int) -> list[int]:
        return [row["at"] for row in self.records["pointer"].decode_table(self.data, offset, count)]

    def symbol(self, offset: int) -> GuiSymbol:
        record = self.read("symbol", offset)
        listing, animation_list = self.records["list"], self.records["animation_list"]
        object_offsets = self.pointers(record["objects"] + listing.size,
                                       listing.decode(self.data, record["objects"])["count"])
        objects = [self.object(at) for at in object_offsets]
        states = animation_list.decode(self.data, record["animations"])
        animation_offsets = self.pointers(record["animations"] + animation_list.size, states["count"])
        animations = [self.animation(at) for at in animation_offsets]
        names = len({item.name for item in animations})
        if states["states"] != names:
            raise GuiFormatError(f"symbol {record['name']!r} declares {states['states']} animation states, "
                                 f"found {names}")
        pointer = self.records["pointer"].size
        self.declared = max(self.declared, offset + self.records["symbol"].size,
                            record["objects"] + listing.size + len(object_offsets) * pointer,
                            record["animations"] + animation_list.size + len(animation_offsets) * pointer)
        return GuiSymbol(uuid.UUID(bytes_le=record["guid"]), record["name"], record["type"], objects, animations)

    def object(self, offset: int) -> GuiObject:
        if offset in self.objects:
            return self.objects[offset]
        record = self.read("object", offset)
        obj = self.objects[offset] = GuiObject(uuid.UUID(bytes_le=record["guid"]),
                                               uuid.UUID(bytes_le=record["prototype"]), record["name"], record["type"])
        properties, name = self.properties(record["properties"]), self.layout.name_property[0]
        if any(item.name == name and item.value != obj.name for item in properties):
            raise GuiFormatError(f"object {obj.name!r} holds another {name}")
        try:
            obj.properties = merged([item for item in properties if item.name != name],
                                    self.properties(record["defaults"]), self.order(obj.type_name))
        except ValueError as exc:
            raise GuiFormatError(f"object {obj.name!r}: {exc}") from exc
        if record["special"]:
            kind = self.layout.special.get(obj.type_name)
            if kind is None:
                raise GuiFormatError(f"unsupported special payload for {obj.type_name!r}")
            obj.special_data = {"texture_set": self.texture_set, "nine_slice": self.nine_slice}[kind](record["special"])
        return obj

    def order(self, type_name: str) -> dict[str, int]:
        runtime = gui_runtime(self.layout.version)
        return runtime.runtime_type(type_name).order if type_name in runtime.types else {}

    def properties(self, offset: int) -> list[Stored]:
        return [self.property(row) for row in self.table("property", offset)] if offset else []

    def property(self, row: dict) -> Stored:
        try:
            kind = PropertyType(row["type"])
        except ValueError as exc:
            raise GuiFormatError(f"property {row['name']!r} uses unknown property type {row['type']}") from exc
        return Stored(row["name"], kind, self.value(kind, row["value"]), row["mask"])

    def value(self, kind: PropertyType, raw: int):
        storage = self.layout.values.get(kind)
        if storage is None:
            raise GuiFormatError(f"GUIR {self.layout.version} stores no {kind.name} values")
        if len(storage) == 1:
            value = struct.unpack_from("<" + storage, raw.to_bytes(8, "little"))[0]
            if storage == "d" and not math.isfinite(value):
                raise GuiFormatError(f"a {kind.name} value is not finite")
            return value
        if not raw:
            return None
        if storage in STRINGS:
            return self.text(raw, storage)
        payload = struct.Struct("<" + self.layout.payloads[storage])
        if raw + payload.size > len(self.data):
            raise GuiFormatError(f"the {kind.name} value at 0x{raw:X} leaves the data")
        values = payload.unpack_from(self.data, raw)
        return uuid.UUID(bytes_le=values[0]) if kind == PropertyType.GUID else list(values)

    def texture_set(self, offset: int) -> GuiTextureSet:
        head = self.records["texture_set"]
        rows = self.records["texture_entry"].decode_table(self.data, offset + head.size,
                                                          head.decode(self.data, offset)["count"])
        return GuiTextureSet([GuiTextureEntry(row["sequence"], row["pattern"], row["bounds"], bool(row["tagged"]))
                              for row in rows])

    def nine_slice(self, offset: int) -> GuiNineSlice:
        head, cell = self.records["nine_slice"], self.records["nine_slice_cell"]
        count = len(NINE_SLICE_CELL_NAMES)
        cells = cell.decode_table(self.data, offset + head.size, count)
        sizes = self.records["nine_slice_repeat"].decode_table(self.data, offset + head.size + count * cell.size, count)
        return GuiNineSlice(head.decode(self.data, offset)["borders"],
                            [GuiNineSliceCell(name, item["sequence"], item["pattern"], (size["width"], size["height"]))
                             for name, item, size in zip(NINE_SLICE_CELL_NAMES, cells, sizes)])

    def animation(self, offset: int) -> GuiAnimation:
        if offset in self.animations:
            return self.animations[offset]
        record = self.read("animation", offset)
        start = offset + self.records["animation"].size
        clip, self.ends[offset] = read_motclip(self.data, start, start)
        self.transitions[offset] = record["transition"]
        self.animations[offset] = GuiAnimation(uuid.UUID(bytes_le=record["guid"]), record["name"],
                                               bool(record["loop"]), clip)
        return self.animations[offset]

    def declarations(self, end: int) -> tuple[list[GuiObject], list[GuiAnimation]]:
        cursor, clip_at = self.declared, self.records["animation"].size
        listing, properties = self.records["list"], self.records["property"].size
        object_offsets: list[int] = []
        while cursor < end and self.data[cursor + clip_at:cursor + clip_at + len(CLIP_MAGIC)] != CLIP_MAGIC:
            defaults = self.records["object"].decode(self.data, cursor)["defaults"]
            following = defaults + listing.size + listing.decode(self.data, defaults)["count"] * properties
            if not cursor < following <= end:
                raise GuiFormatError("invalid object declaration span")
            self.object(cursor)
            object_offsets.append(cursor)
            cursor = following
        animation_offsets: list[int] = []
        while cursor < end:
            self.animation(cursor)
            animation_offsets.append(cursor)
            cursor = self.ends[cursor]
        if cursor != end or set(self.objects) != set(object_offsets) or set(self.animations) != set(animation_offsets):
            raise GuiFormatError("invalid native GUI declaration stream")
        return [self.objects[at] for at in object_offsets], [self.animations[at] for at in animation_offsets]

    def bindings(self, offset: int) -> list[GuiBinding]:
        listing, binding, prop = self.records["list"], self.records["binding"].size, self.records["property"].size
        result = []
        for index in range(listing.decode(self.data, offset)["count"]):
            at = offset + listing.size + index * (binding + prop)
            row, value = self.read("binding", at), self.property(self.read("property", at + binding))
            if value.mask != -1:
                raise GuiFormatError(f"the binding of {row['path']!r} sets only some components of {value.name}")
            result.append(GuiBinding(row["path"], row["type"], GuiProperty(value.name, value.type, value.value)))
        return result
