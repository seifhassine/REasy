"""Native-canonical serializer for semantic GUIR documents."""

from __future__ import annotations

import struct

from file_handlers.motion.mot_clip.model import ClipInterpolation
from file_handlers.motion.mot_clip.writer import CompactMotClipV27Writer

from ..errors import GuiFormatError, GuiWriteError
from ..model import GUI_MAGIC, GuiDocument, GuiNineSlice, GuiProperty, GuiTextureSet
from ..profiles import GuiFormatProfile
from .adapter import DMC5_GUI_ADAPTER
from .property_codec import (
    DMC5_GUI_NATIVE_POOL_ORDER,
    dmc5_native_gui_value_pool,
    encode_dmc5_gui_value,
)


ASCII_EVENT_MODES = frozenset(
    {ClipInterpolation.EVENT, ClipInterpolation.PASS_EVENT}
)


class _Builder:
    """Build fixed records first, then DMC5's dense typed payload pools."""

    def __init__(self) -> None:
        self.data = bytearray(0x30)
        self.references: dict[str, list[tuple[int, bytes, bool]]] = {
            name: [] for name in DMC5_GUI_NATIVE_POOL_ORDER
        }

    def reserve(self, size: int) -> int:
        offset = len(self.data)
        self.data.extend(bytes(size))
        return offset

    def pack(self, fmt: str, offset: int, *values) -> None:
        struct.pack_into(fmt, self.data, offset, *values)

    def reference(
        self,
        position: int,
        pool: str,
        payload: bytes,
        *,
        intern: bool = True,
    ) -> None:
        try:
            references = self.references[pool]
        except KeyError as exc:
            raise GuiWriteError(f"unknown native GUI payload pool {pool!r}") from exc
        references.append((position, bytes(payload), intern))

    def ascii(self, position: int, value: str) -> None:
        if "\0" in value:
            raise GuiWriteError("ASCII strings cannot contain NUL")
        try:
            payload = value.encode("ascii") + b"\0"
        except UnicodeEncodeError as exc:
            raise GuiWriteError(f"{value!r} is not ASCII") from exc
        self.reference(position, "ascii", payload)

    def utf16(self, position: int, value: str) -> None:
        if "\0" in value:
            raise GuiWriteError("UTF-16 strings cannot contain NUL")
        self.reference(position, "wide", value.encode("utf-16le") + b"\0\0")

    def finish(self) -> bytes:
        for pool in DMC5_GUI_NATIVE_POOL_ORDER:
            offsets: dict[bytes, int] = {}
            for position, payload, intern in self.references[pool]:
                offset = offsets.get(payload) if intern else None
                if offset is None:
                    offset = self.reserve(len(payload))
                    self.data[offset : offset + len(payload)] = payload
                    if intern:
                        offsets[payload] = offset
                self.pack("<Q", position, offset)
        return bytes(self.data)


class Dmc5GuiSerializer:
    """Build DMC5's deterministic native layout from semantic relationships."""

    def __init__(self, profile: GuiFormatProfile) -> None:
        try:
            DMC5_GUI_ADAPTER.require_profile(profile, "GUI serializer")
        except GuiFormatError as exc:
            raise GuiWriteError(str(exc)) from exc
        self.profile = profile
        self.clip_writer = CompactMotClipV27Writer(profile.motion)

    def build(self, document: GuiDocument) -> bytes:
        if document.version != self.profile.version:
            raise GuiWriteError(
                f"document version {document.version} does not match {self.profile.version}"
            )
        try:
            document.validate_relationships()
        except ValueError as exc:
            raise GuiWriteError(str(exc)) from exc

        b = _Builder()
        symbols = document.symbols
        objects = document.objects
        animations = document.animations

        root = b.reserve(0x18 + len(symbols) * 8)
        symbol_offsets = {id(item): b.reserve(0x30) for item in symbols}
        symbol_lists: dict[int, tuple[int, int]] = {}
        for symbol in symbols:
            objects_list = b.reserve(8 + len(symbol.objects) * 8)
            animations_list = b.reserve(8 + len(symbol.animations) * 8)
            symbol_lists[id(symbol)] = objects_list, animations_list

        for symbol in symbols:
            offset = symbol_offsets[id(symbol)]
            objects_list, animations_list = symbol_lists[id(symbol)]
            b.data[offset : offset + 0x10] = symbol.guid.bytes_le
            b.utf16(offset + 0x10, symbol.name)
            b.ascii(offset + 0x18, symbol.type_name)
            b.pack("<QQ", offset + 0x20, objects_list, animations_list)
            b.pack("<II", objects_list, len(symbol.objects), 0)
            b.pack(
                "<II",
                animations_list,
                len(symbol.animations),
                len(symbol.animation_states()),
            )

        object_offsets: dict[int, int] = {}
        for obj in objects:
            offset = b.reserve(0x48)
            object_offsets[id(obj)] = offset
            b.data[offset : offset + 0x10] = obj.instance_guid.bytes_le
            b.data[offset + 0x10 : offset + 0x20] = obj.prototype_guid.bytes_le
            b.utf16(offset + 0x20, obj.name)
            b.ascii(offset + 0x28, obj.type_name)
            properties = self._properties(b, obj.properties)
            defaults = self._properties(b, obj.animation_defaults)
            b.pack("<QQ", offset + 0x30, properties, defaults)
            if obj.special_data is not None:
                b.reference(
                    offset + 0x40,
                    "special",
                    self._special_payload(obj.special_data),
                    intern=False,
                )

        animation_offsets: dict[int, int] = {}
        for animation in animations:
            try:
                payload = self.clip_writer.build(
                    animation.clip,
                    origin_offset=0,
                    include_extra_ranges=False,
                    ascii_value_interpolations=ASCII_EVENT_MODES,
                )
            except Exception as exc:
                raise GuiWriteError(
                    f"animation {animation.name!r} cannot be serialized: {exc}"
                ) from exc
            offset = b.reserve(0x28 + len(payload))
            animation_offsets[id(animation)] = offset
            b.data[offset : offset + 0x10] = animation.guid.bytes_le
            b.pack("<Q", offset + 0x10, int(animation.loop))
            b.utf16(offset + 0x18, animation.name)
            b.data[offset + 0x28 : offset + 0x28 + len(payload)] = payload

        first_symbol = symbol_offsets[id(symbols[0])] if symbols else 0
        root_object = object_offsets[id(document.root_object)] if document.root_object else 0
        b.pack("<QQII", root, first_symbol, root_object, len(symbols), 0)
        for index, symbol in enumerate(symbols):
            b.pack("<Q", root + 0x18 + index * 8, symbol_offsets[id(symbol)])
            objects_list, animations_list = symbol_lists[id(symbol)]
            for item_index, obj in enumerate(symbol.objects):
                b.pack("<Q", objects_list + 8 + item_index * 8, object_offsets[id(obj)])
            for item_index, animation in enumerate(symbol.animations):
                b.pack(
                    "<Q",
                    animations_list + 8 + item_index * 8,
                    animation_offsets[id(animation)],
                )
        for animation in animations:
            transition = animation.transition
            b.pack(
                "<Q",
                animation_offsets[id(animation)] + 0x20,
                animation_offsets[id(transition)] if transition is not None else 0,
            )

        metadata = b.reserve(8)
        bindings = b.reserve(8 + len(document.bindings) * 0x28)
        b.pack("<II", bindings, len(document.bindings), 0)
        for index, binding in enumerate(document.bindings):
            record = bindings + 8 + index * 0x28
            b.utf16(record, binding.target_path)
            b.ascii(record + 8, binding.target_type)
            self._property(b, record + 0x10, binding.property)
        imports = self._string_list(b, document.imported_gui_paths)
        assets = self._string_list(b, document.asset_paths)

        b.pack(
            "<I4sQQQQQ",
            0,
            document.version,
            GUI_MAGIC,
            root,
            metadata,
            bindings,
            imports,
            assets,
        )
        return b.finish()

    def _properties(self, b: _Builder, properties: list[GuiProperty]) -> int:
        offset = b.reserve(8 + len(properties) * 0x18)
        b.pack("<II", offset, len(properties), 0)
        for index, prop in enumerate(properties):
            self._property(b, offset + 8 + index * 0x18, prop)
        return offset

    @staticmethod
    def _property(b: _Builder, offset: int, prop: GuiProperty) -> None:
        b.pack("<Ii", offset, int(prop.type), int(prop.component_mask))
        b.ascii(offset + 8, prop.name)
        value = encode_dmc5_gui_value(prop.type, prop.value, prop.name)
        if value.inline is not None:
            b.pack("<Q", offset + 0x10, value.inline)
        elif value.payload is not None:
            b.reference(
                offset + 0x10,
                dmc5_native_gui_value_pool(prop.type),
                value.payload,
            )

    @staticmethod
    def _string_list(b: _Builder, values: list[str]) -> int:
        offset = b.reserve(8 + len(values) * 8)
        b.pack("<II", offset, len(values), 0)
        for index, value in enumerate(values):
            b.utf16(offset + 8 + index * 8, value)
        return offset

    @staticmethod
    def _special_payload(special: GuiTextureSet | GuiNineSlice) -> bytes:
        if isinstance(special, GuiTextureSet):
            payload = bytearray(4 + len(special.entries) * 0x18)
            struct.pack_into("<I", payload, 0, len(special.entries))
            for index, entry in enumerate(special.entries):
                record = 4 + index * 0x18
                sequence = int(entry.sequence) | (int(entry.tagged) << 31)
                struct.pack_into(
                    "<II4f",
                    payload,
                    record,
                    sequence,
                    int(entry.pattern),
                    *entry.bounds,
                )
            return bytes(payload)
        if isinstance(special, GuiNineSlice):
            if len(special.cells) != 9:
                raise GuiWriteError("nine-slice payload requires nine cells")
            payload = bytearray(0xA0)
            struct.pack_into("<4f", payload, 0, *special.borders)
            for index, cell in enumerate(special.cells):
                struct.pack_into(
                    "<II", payload, 0x10 + index * 8, cell.sequence, cell.pattern
                )
                struct.pack_into("<2f", payload, 0x58 + index * 8, *cell.repeat_size)
            return bytes(payload)
        raise GuiWriteError(f"unsupported special payload {type(special).__name__}")
