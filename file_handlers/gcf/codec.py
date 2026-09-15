from __future__ import annotations

import math
import struct
from typing import Any, Protocol

from .model import (
    AssetLanguageTriplet,
    FontSlotMapping,
    GcfData,
    GcfLayout,
    LocalizeAsset,
)
from .profiles import GCF_MAGIC, GcfFormatError, GcfProfile, gcf_profile


class GcfCodec(Protocol):
    profile: GcfProfile

    def read(self, data: bytes) -> tuple[GcfData, GcfLayout]: ...

    def write(self, model: GcfData) -> bytes: ...


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def require(self, offset: int, size: int, label: str) -> None:
        if offset < 0 or size < 0 or offset > len(self.data) - size:
            raise GcfFormatError(
                f"{label} is outside the file: offset=0x{offset:X}, size=0x{size:X}"
            )

    def unpack(self, fmt: str, offset: int, label: str) -> tuple[Any, ...]:
        size = struct.calcsize(fmt)
        self.require(offset, size, label)
        return struct.unpack_from(fmt, self.data, offset)

    def utf16(self, offset: int, label: str) -> str | None:
        if offset == 0:
            return None
        if offset & 1:
            raise GcfFormatError(f"{label} offset 0x{offset:X} is not aligned")
        self.require(offset, 2, label)
        cursor = offset
        while True:
            self.require(cursor, 2, label)
            if self.data[cursor : cursor + 2] == b"\0\0":
                break
            cursor += 2
        try:
            return self.data[offset:cursor].decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise GcfFormatError(f"{label} is not valid UTF-16LE") from exc


class _Builder:
    def __init__(self) -> None:
        self.data = bytearray()
        self._strings: dict[str, int] = {}

    def align(self, alignment: int) -> None:
        padding = (-len(self.data)) % alignment
        if padding:
            self.data.extend(b"\0" * padding)

    def reserve(self, size: int, *, alignment: int = 1) -> int:
        self.align(alignment)
        offset = len(self.data)
        self.data.extend(b"\0" * size)
        return offset

    def string(self, value: str | None) -> int:
        if value is None:
            return 0
        cached = self._strings.get(value)
        if cached is not None:
            return cached
        self.align(2)
        offset = len(self.data)
        self.data.extend(value.encode("utf-16-le") + b"\0\0")
        self._strings[value] = offset
        return offset


def _require_section(reader: _Reader, offset: int, label: str) -> int:
    if offset == 0:
        raise GcfFormatError(f"GCF has a null {label} offset")
    reader.require(offset, 4, label)
    return offset


def _finite(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise GcfFormatError(f"{label} must be finite, got {value!r}")
    return float(value)


class GcfV15Codec:
    profile = gcf_profile(15)
    header_size = 0x28
    root_header_size = 0x18

    def read(self, data: bytes) -> tuple[GcfData, GcfLayout]:
        reader = _Reader(data)
        (
            version,
            magic,
            root_offset,
            message_offset,
            triplet_offset,
            localize_offset,
        ) = reader.unpack("<I4sQQQQ", 0, "GCF header")
        if version != self.profile.version or magic != self.profile.magic:
            raise GcfFormatError(
                f"invalid GCF v15 header: version={version}, magic={magic!r}"
            )

        root_offset = _require_section(reader, root_offset, "root")
        message_offset = _require_section(reader, message_offset, "message section")
        triplet_offset = _require_section(reader, triplet_offset, "triplet section")
        localize_offset = _require_section(reader, localize_offset, "localize section")
        (
            delay_raw,
            language_count,
            slot_count,
            path_count,
            ruby_ratio,
            root_reserved,
            icon_path_offset,
        ) = reader.unpack("<HHHHfIQ", root_offset, "GCF root")
        if delay_raw not in (0, 1):
            raise GcfFormatError(
                f"isDelayLanguageFontLoad contains non-boolean value {delay_raw}"
            )
        if not language_count or not slot_count or not path_count:
            raise GcfFormatError("GCF font dimensions must all be nonzero")
        _finite(ruby_ratio, "default ruby-size ratio")

        slot_total = language_count * slot_count
        path_total = slot_total * path_count
        path_table = root_offset + self.root_header_size
        reader.require(path_table, path_total * 8, "font path table")
        adjust_table = path_table + path_total * 8
        reader.require(adjust_table, slot_total * 8, "font adjust-scale table")

        font_slots: list[FontSlotMapping] = []
        for language in range(language_count):
            for slot in range(slot_count):
                paths: list[str | None] = []
                for candidate in range(path_count):
                    flat_index = candidate + path_count * (slot + slot_count * language)
                    (path_offset,) = reader.unpack(
                        "<Q", path_table + flat_index * 8, "font path offset"
                    )
                    paths.append(
                        reader.utf16(
                            path_offset,
                            f"font path language={language} slot={slot} "
                            f"candidate={candidate}",
                        )
                    )
                adjust_index = slot + slot_count * language
                scale_x, scale_y = reader.unpack(
                    "<ff", adjust_table + adjust_index * 8, "font adjust scale"
                )
                font_slots.append(
                    FontSlotMapping(
                        language_index=language,
                        language_name=self.profile.language_name(language),
                        slot_index=slot,
                        slot_name=self.profile.font_slot_name(slot),
                        asset_paths=tuple(paths),
                        adjust_scale=(scale_x, scale_y),
                    )
                )

        message_count, message_reserved = reader.unpack(
            "<II", message_offset, "message section header"
        )
        reader.require(message_offset + 8, message_count * 8, "message path table")
        message_paths: list[str | None] = []
        for index in range(message_count):
            (path_offset,) = reader.unpack(
                "<Q", message_offset + 8 + index * 8, "message path offset"
            )
            message_paths.append(reader.utf16(path_offset, f"message path {index}"))

        (triplet_count,) = reader.unpack("<I", triplet_offset, "triplet count")
        reader.require(triplet_offset + 4, triplet_count * 12, "triplet table")
        triplets: list[AssetLanguageTriplet] = []
        for index in range(triplet_count):
            value_0, value_1, value_2 = reader.unpack(
                "<fff", triplet_offset + 4 + index * 12, "asset-language triplet"
            )
            triplets.append(
                AssetLanguageTriplet(
                    index,
                    self.profile.asset_language_name(index),
                    value_0,
                    value_1,
                    value_2,
                )
            )

        localize_count, localize_reserved = reader.unpack(
            "<II", localize_offset, "localize section header"
        )
        reader.require(
            localize_offset + 8, localize_count * 0x10, "localize asset table"
        )
        localize_assets: list[LocalizeAsset] = []
        for index in range(localize_count):
            slot, reserved, path_offset = reader.unpack(
                "<IIQ", localize_offset + 8 + index * 0x10, "localize asset"
            )
            localize_assets.append(
                LocalizeAsset(
                    slot=slot,
                    slot_name=self.profile.asset_language_name(slot),
                    reserved=reserved,
                    path=reader.utf16(path_offset, f"localize path {index}"),
                )
            )

        model = GcfData(
            version=version,
            delay_language_font_load_raw=delay_raw,
            language_count=language_count,
            font_slot_count=slot_count,
            font_asset_path_count=path_count,
            default_ruby_size_ratio=ruby_ratio,
            root_reserved=root_reserved,
            icon_font_asset_path=reader.utf16(icon_path_offset, "icon font path"),
            font_slots=font_slots,
            message_section_reserved=message_reserved,
            message_asset_paths=message_paths,
            asset_language_triplets=triplets,
            localize_section_reserved=localize_reserved,
            localize_assets=localize_assets,
        )
        return model, GcfLayout(
            root_offset,
            message_offset,
            triplet_offset,
            localize_offset,
        )

    def _slot_grid(self, model: GcfData) -> list[FontSlotMapping]:
        expected = model.language_count * model.font_slot_count
        if len(model.font_slots) != expected:
            raise GcfFormatError(
                f"GCF expects {expected} font slots, got {len(model.font_slots)}"
            )
        indexed: dict[tuple[int, int], FontSlotMapping] = {}
        for mapping in model.font_slots:
            key = mapping.language_index, mapping.slot_index
            if key in indexed:
                raise GcfFormatError(f"duplicate GCF font slot {key}")
            indexed[key] = mapping
        result: list[FontSlotMapping] = []
        for language in range(model.language_count):
            for slot in range(model.font_slot_count):
                try:
                    mapping = indexed[language, slot]
                except KeyError as exc:
                    raise GcfFormatError(
                        f"missing GCF font slot language={language}, slot={slot}"
                    ) from exc
                if len(mapping.asset_paths) != model.font_asset_path_count:
                    raise GcfFormatError(
                        f"font slot {language}/{slot} has {len(mapping.asset_paths)} "
                        f"paths, expected {model.font_asset_path_count}"
                    )
                if len(mapping.adjust_scale) != 2:
                    raise GcfFormatError(
                        f"font slot {language}/{slot} adjust scale must be Float2"
                    )
                result.append(mapping)
        return result

    def write(self, model: GcfData) -> bytes:
        if model.version != self.profile.version:
            raise GcfFormatError(
                f"GCF v15 codec cannot write embedded version {model.version}"
            )
        if model.delay_language_font_load_raw not in (0, 1):
            raise GcfFormatError("isDelayLanguageFontLoad must be 0 or 1")
        if (
            model.language_count <= 0
            or model.font_slot_count <= 0
            or model.font_asset_path_count <= 0
        ):
            raise GcfFormatError("GCF font dimensions must all be positive")
        _finite(model.default_ruby_size_ratio, "default ruby-size ratio")
        slots = self._slot_grid(model)

        builder = _Builder()
        header_offset = builder.reserve(self.header_size, alignment=8)
        path_total = len(slots) * model.font_asset_path_count
        root_size = self.root_header_size + path_total * 8 + len(slots) * 8
        root_offset = builder.reserve(root_size, alignment=8)
        message_offset = builder.reserve(
            8 + len(model.message_asset_paths) * 8, alignment=8
        )
        triplet_offset = builder.reserve(
            4 + len(model.asset_language_triplets) * 12, alignment=8
        )
        localize_offset = builder.reserve(
            8 + len(model.localize_assets) * 0x10, alignment=8
        )

        icon_offset = builder.string(model.icon_font_asset_path)
        struct.pack_into(
            "<HHHHfIQ",
            builder.data,
            root_offset,
            model.delay_language_font_load_raw,
            model.language_count,
            model.font_slot_count,
            model.font_asset_path_count,
            _finite(model.default_ruby_size_ratio, "default ruby-size ratio"),
            model.root_reserved,
            icon_offset,
        )
        path_table = root_offset + self.root_header_size
        adjust_table = path_table + path_total * 8
        for native_slot_index, mapping in enumerate(slots):
            for candidate, path in enumerate(mapping.asset_paths):
                flat_index = candidate + model.font_asset_path_count * native_slot_index
                struct.pack_into(
                    "<Q",
                    builder.data,
                    path_table + flat_index * 8,
                    builder.string(path),
                )
            struct.pack_into(
                "<ff",
                builder.data,
                adjust_table + native_slot_index * 8,
                _finite(mapping.adjust_scale[0], "font adjustment scale X"),
                _finite(mapping.adjust_scale[1], "font adjustment scale Y"),
            )

        struct.pack_into(
            "<II",
            builder.data,
            message_offset,
            len(model.message_asset_paths),
            model.message_section_reserved,
        )
        for index, path in enumerate(model.message_asset_paths):
            struct.pack_into(
                "<Q",
                builder.data,
                message_offset + 8 + index * 8,
                builder.string(path),
            )

        struct.pack_into(
            "<I", builder.data, triplet_offset, len(model.asset_language_triplets)
        )
        for index, triplet in enumerate(model.asset_language_triplets):
            struct.pack_into(
                "<fff",
                builder.data,
                triplet_offset + 4 + index * 12,
                _finite(triplet.value_0, "asset-language triplet value 0"),
                _finite(triplet.value_1, "asset-language triplet value 1"),
                _finite(triplet.value_2, "asset-language triplet value 2"),
            )

        struct.pack_into(
            "<II",
            builder.data,
            localize_offset,
            len(model.localize_assets),
            model.localize_section_reserved,
        )
        for index, asset in enumerate(model.localize_assets):
            struct.pack_into(
                "<IIQ",
                builder.data,
                localize_offset + 8 + index * 0x10,
                asset.slot,
                asset.reserved,
                builder.string(asset.path),
            )

        struct.pack_into(
            "<I4sQQQQ",
            builder.data,
            header_offset,
            model.version,
            self.profile.magic,
            root_offset,
            message_offset,
            triplet_offset,
            localize_offset,
        )
        return bytes(builder.data)


GCF_CODECS: dict[int, GcfCodec] = {15: GcfV15Codec()}


def gcf_codec(version: int) -> GcfCodec:
    try:
        return GCF_CODECS[int(version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(GCF_CODECS)))
        raise GcfFormatError(
            f"unsupported GCF version {version}; supported versions: {supported}"
        ) from exc


def decode_gcf(data: bytes) -> tuple[GcfData, GcfLayout]:
    if len(data) < 8:
        raise GcfFormatError("file is too small for a GCF header")
    version, magic = struct.unpack_from("<I4s", data, 0)
    if magic != GCF_MAGIC:
        raise GcfFormatError(f"expected GCFG magic, got {magic!r}")
    return gcf_codec(version).read(data)


def encode_gcf(model: GcfData) -> bytes:
    return gcf_codec(model.version).write(model)
