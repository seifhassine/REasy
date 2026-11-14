"""See docs/internals/gui-format.md."""

from __future__ import annotations

import math
import struct
import uuid

lazy from file_handlers.clip.encode import write_motclip
lazy from file_handlers.clip.errors import ClipFormatError
lazy from file_handlers.clip.rules import PropertyType

from .errors import GuiFormatError, GuiWriteError
from .model import (
    NINE_SLICE_CELL_NAMES,
    GuiAnimation,
    GuiBinding,
    GuiDocument,
    GuiNineSlice,
    GuiObject,
    GuiTextureSet,
)
from .rules import STRINGS, GuiLayout, gui_layout
from .storage import Stored, animated, asset_paths, stored

_INTEGERS = frozenset("bBhHiIqQ")


def serialize_gui(document: GuiDocument) -> bytes:
    try:
        layout = gui_layout(document.version)
        document.validate_relationships()
    except ValueError as exc:
        raise GuiWriteError(str(exc)) from exc
    try:
        return _Writer(layout).build(document)
    except GuiFormatError as exc:
        raise GuiWriteError(str(exc)) from exc


def encode_value(layout: GuiLayout, kind: PropertyType, value, name: str) -> tuple[int, str, bytes]:
    storage = layout.values.get(kind)
    if storage is None:
        raise GuiWriteError(f"{name}: GUIR {layout.version} stores no {kind.name} values")
    if storage == "?":
        if type(value) is not bool:
            raise GuiWriteError(f"{name}: {kind.name} value must be Boolean")
        return int(value), "", b""
    if storage == "d":
        if type(value) not in (int, float) or not math.isfinite(value):
            raise GuiWriteError(f"{name}: floating value must be finite")
        return int.from_bytes(struct.pack("<d", value), "little"), "", b""
    if storage in _INTEGERS:
        if type(value) is not int:
            raise GuiWriteError(f"{name}: value is outside {kind.name}")
        try:
            return int.from_bytes(struct.pack("<" + storage, value), "little"), "", b""
        except struct.error:
            raise GuiWriteError(f"{name}: value is outside {kind.name}") from None
    if value is None:
        return 0, "", b""
    if storage in STRINGS:
        return 0, storage, _text(value, storage, name)
    if kind == PropertyType.GUID:
        if not isinstance(value, uuid.UUID):
            raise GuiWriteError(f"{name}: expected a UUID")
        return 0, storage, value.bytes_le
    payload = layout.payloads[storage]
    count = int(payload[:-1])
    if (not isinstance(value, (list, tuple)) or len(value) != count
            or payload[-1] in _INTEGERS and any(type(item) is not int for item in value)):
        raise GuiWriteError(f"{name}: expected {count} {kind.name} components")
    try:
        return 0, storage, struct.pack("<" + payload, *value)
    except (OverflowError, struct.error):
        raise GuiWriteError(f"{name}: invalid {kind.name} value") from None


def _text(value, kind: str, name: str) -> bytes:
    encoding, terminator = STRINGS[kind]
    if not isinstance(value, str) or "\0" in value:
        raise GuiWriteError(f"{name}: expected a string without NUL characters")
    try:
        return value.encode(encoding) + terminator
    except UnicodeEncodeError:
        raise GuiWriteError(f"{name}: {value!r} is not {encoding}") from None


def encode_special(layout: GuiLayout, type_name: str, special) -> bytes:
    kind, records = layout.special.get(type_name), layout.records
    if kind == "texture_set" and isinstance(special, GuiTextureSet):
        entry = records["texture_entry"]
        return records["texture_set"].encode({"count": len(special.entries)}) + b"".join(
            entry.encode({"sequence": item.sequence, "tagged": int(item.tagged), "pattern": item.pattern,
                          "bounds": item.bounds}) for item in special.entries)
    if kind == "nine_slice" and isinstance(special, GuiNineSlice) and len(special.cells) == len(NINE_SLICE_CELL_NAMES):
        cell, size = records["nine_slice_cell"], records["nine_slice_repeat"]
        return (records["nine_slice"].encode({"borders": special.borders})
                + b"".join(cell.encode({"sequence": item.sequence, "pattern": item.pattern}) for item in special.cells)
                + b"".join(size.encode({"width": item.repeat_size[0], "height": item.repeat_size[1]})
                           for item in special.cells))
    raise GuiWriteError(f"{type_name} cannot hold this {type(special).__name__} payload in GUIR {layout.version}")


class _Writer:
    def __init__(self, layout: GuiLayout) -> None:
        self.layout, self.records = layout, layout.records
        self.data = bytearray(layout.header.size)
        self.references: dict[str, list[tuple[int, bytes, bool]]] = {pool: [] for pool in layout.pools}

    def build(self, document: GuiDocument) -> bytes:
        records, pointer = self.records, self.records["pointer"].size
        symbols = document.symbols
        root = self.reserve(records["root"].size + len(symbols) * pointer)
        symbol_offsets = [self.reserve(records["symbol"].size) for _ in symbols]
        lists = [(self.reserve(records["list"].size + len(symbol.objects) * pointer),
                  self.reserve(records["animation_list"].size + len(symbol.animations) * pointer))
                 for symbol in symbols]
        for symbol, offset, (objects, animations) in zip(symbols, symbol_offsets, lists):
            self.put("symbol", offset, {"guid": symbol.guid.bytes_le, "name": symbol.name, "type": symbol.type_name,
                                        "objects": objects, "animations": animations})
            self.put("list", objects, {"count": len(symbol.objects)})
            self.put("animation_list", animations,
                     {"count": len(symbol.animations), "states": len(symbol.animation_states())})
        masks = {key: value for symbol in symbols for key, value in animated(symbol).items()}
        split = {id(obj): stored(obj, masks.get(id(obj), {})) for obj in document.objects}
        object_offsets = {id(obj): self.object(obj, *split[id(obj)]) for obj in document.objects}
        animation_offsets = {id(animation): self.animation(animation) for animation in document.animations}
        root_object = object_offsets[id(document.root_object)] if document.root_object is not None else 0
        self.put("root", root, {"first_symbol": symbol_offsets[0] if symbols else 0, "root_object": root_object,
                                "count": len(symbols)})
        self.pointers(root + records["root"].size, symbol_offsets)
        for symbol, (objects, animations) in zip(symbols, lists):
            self.pointers(objects + records["list"].size, [object_offsets[id(obj)] for obj in symbol.objects])
            self.pointers(animations + records["animation_list"].size,
                          [animation_offsets[id(animation)] for animation in symbol.animations])
        for animation in document.animations:
            if animation.transition is not None:
                self.link("animation", animation_offsets[id(animation)], "transition",
                          animation_offsets[id(animation.transition)])
        metadata = self.reserve(records["list"].size)
        bindings = self.bindings(document.bindings)
        imports = self.paths(document.imported_gui_paths)
        assets = self.paths(asset_paths(document, split.values()))
        self.data[:self.layout.header.size] = self.layout.header.encode(
            {"version": document.version, "root": root, "metadata": metadata, "bindings": bindings,
             "imports": imports, "assets": assets})
        return self.finish()

    def reserve(self, size: int) -> int:
        offset = len(self.data)
        self.data += bytes(size)
        return offset

    def put(self, name: str, offset: int, record: dict) -> None:
        codec = self.records[name]
        for field, kind in codec.strings:
            self.reference(offset + codec.offsets[field], kind, _text(record[field], kind, field))
        self.data[offset:offset + codec.size] = codec.encode({**record, **{field: 0 for field, _ in codec.strings}})

    def link(self, name: str, offset: int, field: str, target: int) -> None:
        struct.pack_into("<Q", self.data, offset + self.records[name].offsets[field], target)

    def reference(self, position: int, pool: str, payload: bytes, intern: bool = True) -> None:
        self.references[pool].append((position, payload, intern))

    def pointers(self, offset: int, targets: list[int]) -> None:
        size = self.records["pointer"].size
        for index, target in enumerate(targets):
            self.put("pointer", offset + index * size, {"at": target})

    def object(self, obj: GuiObject, properties: list[Stored], defaults: list[Stored]) -> int:
        offset = self.reserve(self.records["object"].size)
        self.put("object", offset, {"guid": obj.instance_guid.bytes_le, "prototype": obj.prototype_guid.bytes_le,
                                    "name": obj.name, "type": obj.type_name, "properties": 0, "defaults": 0,
                                    "special": 0})
        name, kind = self.layout.name_property
        self.link("object", offset, "properties",
                  self.properties([Stored(name, PropertyType(kind), obj.name), *properties]))
        self.link("object", offset, "defaults", self.properties(defaults))
        if obj.special_data is not None:
            self.reference(offset + self.records["object"].offsets["special"], "special",
                           encode_special(self.layout, obj.type_name, obj.special_data), False)
        return offset

    def properties(self, properties: list[Stored]) -> int:
        listing, size = self.records["list"], self.records["property"].size
        offset = self.reserve(listing.size + len(properties) * size)
        self.put("list", offset, {"count": len(properties)})
        for index, prop in enumerate(properties):
            self.property(offset + listing.size + index * size, prop)
        return offset

    def property(self, offset: int, prop: Stored) -> None:
        raw, pool, payload = encode_value(self.layout, prop.type, prop.value, prop.name)
        self.put("property", offset, {"type": int(prop.type), "mask": prop.mask, "name": prop.name, "value": raw})
        if pool:
            self.reference(offset + self.records["property"].offsets["value"], pool, payload)

    def animation(self, animation: GuiAnimation) -> int:
        try:
            clip = write_motclip(animation.clip, 0)
        except ClipFormatError as exc:
            raise GuiWriteError(f"animation {animation.name!r} cannot be serialized: {exc}") from exc
        size = self.records["animation"].size
        offset = self.reserve(size + len(clip))
        self.put("animation", offset, {"guid": animation.guid.bytes_le, "loop": int(animation.loop),
                                       "name": animation.name, "transition": 0})
        self.data[offset + size:offset + size + len(clip)] = clip
        return offset

    def bindings(self, bindings: list[GuiBinding]) -> int:
        listing, binding, prop = self.records["list"], self.records["binding"].size, self.records["property"].size
        offset = self.reserve(listing.size + len(bindings) * (binding + prop))
        self.put("list", offset, {"count": len(bindings)})
        for index, item in enumerate(bindings):
            at = offset + listing.size + index * (binding + prop)
            self.put("binding", at, {"path": item.target_path, "type": item.target_type})
            self.property(at + binding, Stored(item.property.name, item.property.type, item.property.value))
        return offset

    def paths(self, values: list[str]) -> int:
        listing, size = self.records["list"], self.records["path"].size
        offset = self.reserve(listing.size + len(values) * size)
        self.put("list", offset, {"count": len(values)})
        for index, value in enumerate(values):
            self.put("path", offset + listing.size + index * size, {"path": value})
        return offset

    def finish(self) -> bytes:
        for pool in self.layout.pools:
            placed: dict[bytes, int] = {}
            for position, payload, intern in self.references[pool]:
                offset = placed.get(payload) if intern else None
                if offset is None:
                    offset = self.reserve(len(payload))
                    self.data[offset:offset + len(payload)] = payload
                    if intern:
                        placed[payload] = offset
                struct.pack_into("<Q", self.data, position, offset)
        return bytes(self.data)
