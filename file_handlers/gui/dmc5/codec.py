"""Bounds-checked GUIR decoder into the semantic object graph."""

from __future__ import annotations

import struct
import uuid

from file_handlers.clip.enums import PropertyType
from file_handlers.motion.binary import ReadContext
from file_handlers.motion.errors import MotionParseError
from file_handlers.motion.mot_clip.model import ClipInterpolation
from file_handlers.motion.mot_clip.parser import CompactClipV27Parser

from ..errors import GuiFormatError
from ..model import (
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
from ..profiles import GuiFormatProfile
from .adapter import DMC5_GUI_ADAPTER
from .property_codec import decode_dmc5_gui_value


MAX_RECORDS = 1_000_000
ASCII_EVENT_MODES = frozenset(
    {ClipInterpolation.EVENT, ClipInterpolation.PASS_EVENT}
)

class Reader:
    def __init__(self, data: bytes, source: str) -> None:
        self.data = data
        self.source = source

    def require(self, offset: int, size: int, what: str) -> None:
        if offset < 0 or size < 0 or offset > len(self.data) - size:
            raise GuiFormatError(
                f"{self.source}: {what} at 0x{offset:X} is outside the file"
            )

    def unpack(self, fmt: str, offset: int, what: str):
        size = struct.calcsize(fmt)
        self.require(offset, size, what)
        return struct.unpack_from(fmt, self.data, offset)

    def u32(self, offset: int, what: str) -> int:
        return self.unpack("<I", offset, what)[0]

    def i32(self, offset: int, what: str) -> int:
        return self.unpack("<i", offset, what)[0]

    def u64(self, offset: int, what: str) -> int:
        return self.unpack("<Q", offset, what)[0]

    def f32(self, offset: int, what: str) -> float:
        return self.unpack("<f", offset, what)[0]

    def guid(self, offset: int, what: str) -> uuid.UUID:
        self.require(offset, 16, what)
        return uuid.UUID(bytes_le=self.data[offset : offset + 16])

    def ascii(self, offset: int, what: str) -> str:
        self.require(offset, 1, what)
        end = self.data.find(b"\0", offset)
        if end < 0:
            raise GuiFormatError(f"{self.source}: unterminated {what}")
        try:
            return self.data[offset:end].decode("ascii")
        except UnicodeDecodeError as exc:
            raise GuiFormatError(f"{self.source}: {what} is not ASCII") from exc

    def utf16(self, offset: int, what: str) -> str:
        self.require(offset, 2, what)
        for end in range(offset, len(self.data) - 1, 2):
            if self.data[end : end + 2] == b"\0\0":
                try:
                    return self.data[offset:end].decode("utf-16le")
                except UnicodeDecodeError as exc:
                    raise GuiFormatError(
                        f"{self.source}: {what} is not valid UTF-16"
                    ) from exc
        raise GuiFormatError(f"{self.source}: unterminated {what}")

    def table(self, offset: int, stride: int, what: str) -> tuple[int, int]:
        self.require(offset, 8, what)
        count = self.u32(offset, f"{what} count")
        reserved = self.u32(offset + 4, f"{what} reserved")
        if reserved:
            raise GuiFormatError(f"{self.source}: {what} reserved value is nonzero")
        if count > MAX_RECORDS:
            raise GuiFormatError(f"{self.source}: implausible {what} count {count}")
        records = offset + 8
        self.require(records, count * stride, what)
        return count, records


class Dmc5GuiCodec:
    def __init__(self, data: bytes, source: str, profile: GuiFormatProfile) -> None:
        DMC5_GUI_ADAPTER.require_profile(profile, source)
        self.data = bytes(data)
        self.source = source
        self.profile = profile
        self.r = Reader(self.data, source)
        self._clip_parser = CompactClipV27Parser(profile.motion)
        self._clip_context = ReadContext.from_bytes(self.data, source)
        self._objects: dict[int, GuiObject] = {}
        self._animations: dict[int, GuiAnimation] = {}
        self._animation_ends: dict[int, int] = {}
        self._transition_offsets: dict[int, int] = {}

    def parse(self) -> GuiDocument:
        r = self.r
        r.require(0, 0x30, "GUIR header")
        version = r.u32(0, "GUIR version")
        if self.data[4:8] != GUI_MAGIC:
            raise GuiFormatError(f"{self.source}: expected GUIR magic")
        if version != self.profile.version:
            raise GuiFormatError(
                f"{self.source}: expected GUI version {self.profile.version}, got {version}"
            )
        root_offset, metadata_offset, bindings_offset, imports_offset, assets_offset = (
            r.u64(8 + index * 8, "GUIR section pointer") for index in range(5)
        )
        root_object_offset, symbol_offsets = self._root(root_offset)
        symbols = [self._symbol(value) for value in symbol_offsets]
        root_object = self._object(root_object_offset) if root_object_offset else None
        object_offsets, animation_offsets = self._native_declarations(
            root_offset,
            symbol_offsets,
            metadata_offset,
        )
        for animation_offset, target_offset in self._transition_offsets.items():
            if not target_offset:
                continue
            try:
                self._animations[animation_offset].transition = self._animations[target_offset]
            except KeyError as exc:
                raise GuiFormatError(
                    f"{self.source}: animation transition targets an unowned descriptor"
                ) from exc
        self._empty_metadata(metadata_offset)
        document = GuiDocument(
            version=version,
            root_object=root_object,
            symbols=symbols,
            bindings=self._bindings(bindings_offset),
            imported_gui_paths=self._paths(imports_offset, "imported GUI paths"),
            asset_paths=self._paths(assets_offset, "asset dependency paths"),
            source=self.source,
            objects=[self._objects[value] for value in object_offsets],
            animations=[self._animations[value] for value in animation_offsets],
        )
        try:
            document.validate_relationships()
        except ValueError as exc:
            raise GuiFormatError(f"{self.source}: {exc}") from exc
        return document

    def _native_declarations(
        self,
        root_offset: int,
        symbol_offsets: list[int],
        metadata_offset: int,
    ) -> tuple[list[int], list[int]]:
        """Read the native declaration stream, including unowned records."""
        r = self.r
        cursor = root_offset + 0x18 + len(symbol_offsets) * 8
        for symbol in symbol_offsets:
            cursor = max(cursor, symbol + 0x30)
            for field in (0x20, 0x28):
                table = r.u64(symbol + field, "symbol declaration table")
                count = r.u32(table, "symbol declaration count")
                r.require(table + 8, count * 8, "symbol declarations")
                cursor = max(cursor, table + 8 + count * 8)

        object_offsets: list[int] = []
        while cursor < metadata_offset:
            r.require(cursor, 0x30, "GUI declaration")
            if self.data[cursor + 0x28 : cursor + 0x2C] == b"CLIP":
                break
            record = cursor
            defaults = r.u64(cursor + 0x38, "animation defaults")
            count, _ = r.table(defaults, 0x18, "animation defaults")
            next_cursor = defaults + 8 + count * 0x18
            if not cursor < next_cursor <= metadata_offset:
                raise GuiFormatError(f"{self.source}: invalid object declaration span")
            cursor = next_cursor
            self._object(record)
            object_offsets.append(record)

        animation_offsets: list[int] = []
        while cursor < metadata_offset:
            record = cursor
            self._animation(record)
            cursor = self._animation_ends[record]
            animation_offsets.append(record)
        if (
            cursor != metadata_offset
            or set(self._objects) != set(object_offsets)
            or set(self._animations) != set(animation_offsets)
        ):
            raise GuiFormatError(
                f"{self.source}: invalid native GUI declaration stream"
            )
        return object_offsets, animation_offsets

    def _root(self, offset: int) -> tuple[int, list[int]]:
        r = self.r
        r.require(offset, 0x18, "root table")
        first = r.u64(offset, "first symbol")
        root_object = r.u64(offset + 8, "root object")
        count = r.u32(offset + 0x10, "symbol count")
        if r.u32(offset + 0x14, "root reserved"):
            raise GuiFormatError(f"{self.source}: root reserved value is nonzero")
        if count > MAX_RECORDS:
            raise GuiFormatError(f"{self.source}: implausible symbol count {count}")
        r.require(offset + 0x18, count * 8, "symbol pointers")
        values = [r.u64(offset + 0x18 + index * 8, "symbol pointer") for index in range(count)]
        if values and first != values[0]:
            raise GuiFormatError(f"{self.source}: first-symbol pointers disagree")
        if not values and first:
            raise GuiFormatError(f"{self.source}: empty root has a first-symbol pointer")
        return root_object, values

    def _symbol(self, offset: int) -> GuiSymbol:
        r = self.r
        r.require(offset, 0x30, "symbol")
        name = r.utf16(r.u64(offset + 0x10, "symbol name"), "symbol name")
        type_name = r.ascii(r.u64(offset + 0x18, "symbol type"), "symbol type")
        objects = [
            self._object(value)
            for value in self._pointers(
                r.u64(offset + 0x20, "object list"), "symbol objects"
            )
        ]
        animation_list = r.u64(offset + 0x28, "animation list")
        r.require(animation_list, 8, "symbol animations")
        count = r.u32(animation_list, "animation descriptor count")
        distinct = r.u32(animation_list + 4, "distinct animation state count")
        if count > MAX_RECORDS:
            raise GuiFormatError(f"{self.source}: implausible animation count {count}")
        records = animation_list + 8
        r.require(records, count * 8, "animation pointers")
        animations = [self._animation(r.u64(records + index * 8, "animation pointer")) for index in range(count)]
        actual_distinct = len({item.name for item in animations})
        if distinct != actual_distinct:
            raise GuiFormatError(
                f"{self.source}: symbol {name!r} declares {distinct} animation states, found {actual_distinct}"
            )
        return GuiSymbol(r.guid(offset, "symbol GUID"), name, type_name, objects, animations)

    def _object(self, offset: int) -> GuiObject:
        if offset in self._objects:
            return self._objects[offset]
        r = self.r
        r.require(offset, 0x48, "GUI object")
        type_name = r.ascii(r.u64(offset + 0x28, "object type"), "object type")
        obj = GuiObject(
            instance_guid=r.guid(offset, "object GUID"),
            prototype_guid=r.guid(offset + 0x10, "object prototype GUID"),
            name=r.utf16(r.u64(offset + 0x20, "object name"), "object name"),
            type_name=type_name,
        )
        self._objects[offset] = obj
        obj.properties = self._properties(r.u64(offset + 0x30, "object properties"), "object properties")
        obj.animation_defaults = self._properties(r.u64(offset + 0x38, "animation defaults"), "animation defaults")
        special = r.u64(offset + 0x40, "object special data")
        if special:
            if type_name == "via.gui.TextureSet":
                obj.special_data = self._texture_set(special)
            elif type_name in {"via.gui.Scale9Grid", "via.gui.BlurFilter"}:
                obj.special_data = self._nine_slice(special)
            else:
                raise GuiFormatError(
                    f"{self.source}: unsupported special payload for {type_name!r}"
                )
        return obj

    def _animation(self, offset: int) -> GuiAnimation:
        if offset in self._animations:
            return self._animations[offset]
        r = self.r
        r.require(offset, 0xB0, "animation descriptor")
        loop = r.u64(offset + 0x10, "animation loop")
        if loop not in (0, 1):
            raise GuiFormatError(f"{self.source}: animation loop flag is not Boolean")
        payload = offset + 0x28
        try:
            result = self._clip_parser.parse_result(
                self._clip_context,
                payload,
                None,
                pointer_base=payload,
                following_data_name="next GUI structure",
                allow_missing_extra=True,
                require_canonical_layout=False,
                ascii_value_interpolations=ASCII_EVENT_MODES,
            )
        except MotionParseError as exc:
            raise GuiFormatError(str(exc)) from exc
        animation = GuiAnimation(
            guid=r.guid(offset, "animation GUID"),
            name=r.utf16(r.u64(offset + 0x18, "animation name"), "animation name"),
            loop=bool(loop),
            clip=result.clip,
        )
        self._animations[offset] = animation
        self._animation_ends[offset] = result.physical_end
        self._transition_offsets[offset] = r.u64(offset + 0x20, "animation transition")
        return animation

    def _properties(self, offset: int, what: str) -> list[GuiProperty]:
        if not offset:
            return []
        count, records = self.r.table(offset, 0x18, what)
        return [self._property(records + index * 0x18, f"{what} {index}") for index in range(count)]

    def _property(self, offset: int, what: str) -> GuiProperty:
        r = self.r
        try:
            kind = PropertyType(r.u32(offset, f"{what} type"))
        except ValueError as exc:
            raise GuiFormatError(f"{self.source}: {what} uses an unknown property type") from exc
        mask = r.i32(offset + 4, f"{what} component mask")
        name = r.ascii(r.u64(offset + 8, f"{what} name"), f"{what} name")
        raw = r.u64(offset + 0x10, f"{what} value")
        return GuiProperty(
            name,
            kind,
            decode_dmc5_gui_value(r, kind, raw, what),
            mask,
        )

    def _texture_set(self, offset: int) -> GuiTextureSet:
        r = self.r
        count = r.u32(offset, "texture-set count")
        if count > MAX_RECORDS:
            raise GuiFormatError(f"{self.source}: implausible texture-set count")
        r.require(offset + 4, count * 0x18, "texture-set entries")
        result = []
        for index in range(count):
            record = offset + 4 + index * 0x18
            sequence = r.u32(record, "texture-set sequence")
            result.append(
                GuiTextureEntry(
                    sequence & 0x7FFF_FFFF,
                    r.u32(record + 4, "texture-set pattern"),
                    tuple(r.unpack("<4f", record + 8, "texture-set bounds")),
                    bool(sequence & 0x8000_0000),
                )
            )
        return GuiTextureSet(result)

    def _nine_slice(self, offset: int) -> GuiNineSlice:
        r = self.r
        r.require(offset, 0xA0, "nine-slice data")
        cells = []
        for index, name in enumerate(NINE_SLICE_CELL_NAMES):
            texture = offset + 0x10 + index * 8
            repeat = offset + 0x58 + index * 8
            cells.append(
                GuiNineSliceCell(
                    name,
                    r.u32(texture, "nine-slice sequence"),
                    r.u32(texture + 4, "nine-slice pattern"),
                    tuple(r.unpack("<2f", repeat, "nine-slice repeat size")),
                )
            )
        return GuiNineSlice(tuple(r.unpack("<4f", offset, "nine-slice borders")), cells)

    def _bindings(self, offset: int) -> list[GuiBinding]:
        count, records = self.r.table(offset, 0x28, "property bindings")
        result = []
        for index in range(count):
            record = records + index * 0x28
            result.append(
                GuiBinding(
                    self.r.utf16(self.r.u64(record, "binding path"), "binding path"),
                    self.r.ascii(self.r.u64(record + 8, "binding type"), "binding type"),
                    self._property(record + 0x10, f"binding {index}"),
                )
            )
        return result

    def _paths(self, offset: int, what: str) -> list[str]:
        return [self.r.utf16(value, what) for value in self._pointers(offset, what)]

    def _pointers(self, offset: int, what: str) -> list[int]:
        count, records = self.r.table(offset, 8, what)
        return [self.r.u64(records + index * 8, f"{what} pointer") for index in range(count)]

    def _empty_metadata(self, offset: int) -> None:
        count, _ = self.r.table(offset, 0x30, "type/field metadata")
        if count:
            raise GuiFormatError(
                f"{self.source}: nonempty DMC5 type/field metadata is not semantically understood"
            )
