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
from .profiles import (
    GCF_MAGIC,
    GcfFormatError,
    GcfProfile,
    gcf_profile,
    gcf_profile_or_generic,
)


class GcfCodec(Protocol):
    profile: GcfProfile

    def read(self, data: bytes) -> tuple[GcfData, GcfLayout]: ...

    def write(self, model: GcfData) -> bytes: ...


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.refs: list[int] = []

    def require(self, offset: int, size: int, label: str) -> None:
        if offset < 0 or size < 0 or offset > len(self.data) - size:
            raise GcfFormatError(
                f"{label} is outside the file: offset=0x{offset:X}, size=0x{size:X}"
            )

    def unpack(self, fmt: str, offset: int, label: str) -> tuple[Any, ...]:
        self.require(offset, struct.calcsize(fmt), label)
        return struct.unpack_from(fmt, self.data, offset)

    def utf16(self, offset: int, label: str) -> str | None:
        self.refs.append(offset)
        if offset == 0:
            return None
        if offset & 1:
            raise GcfFormatError(f"{label} offset 0x{offset:X} is not aligned")
        end = offset
        while self.data[end : end + 2] != b"\0\0":
            self.require(end, 2, label)
            end += 2
        try:
            return self.data[offset:end].decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise GcfFormatError(f"{label} is not valid UTF-16LE") from exc


class _Builder:
    def __init__(self) -> None:
        self.data = bytearray()
        self._strings: dict[str, int] = {}

    def reserve(self, size: int) -> int:
        offset = len(self.data)
        self.data.extend(b"\0" * size)
        return offset

    def string(self, value: str | None) -> int:
        if value is None:
            return 0
        if value not in self._strings:
            self._strings[value] = len(self.data)
            self.data.extend(value.encode("utf-16-le") + b"\0\0")
        return self._strings[value]


def _finite(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise GcfFormatError(f"{label} must be finite, got {value!r}")
    return float(value)


def _section(reader: _Reader, offset: int, label: str) -> int:
    if offset == 0:
        raise GcfFormatError(f"GCF has a null {label} offset")
    reader.require(offset, 4, label)
    return offset


def _read_messages(r: _Reader, base: int) -> tuple[int, list[str | None]]:
    count, reserved = r.unpack("<II", base, "message section header")
    r.require(base + 8, count * 8, "message path table")
    return reserved, [
        r.utf16(*r.unpack("<Q", base + 8 + i * 8, "message path offset"), f"message path {i}")
        for i in range(count)
    ]


def _read_triplets(
    r: _Reader, base: int, profile: GcfProfile
) -> list[AssetLanguageTriplet]:
    (count,) = r.unpack("<I", base, "triplet count")
    r.require(base + 4, count * 12, "triplet table")
    return [
        AssetLanguageTriplet(
            i,
            profile.asset_language_name(i),
            *(
                _finite(v, f"asset-language triplet value {j}")
                for j, v in enumerate(
                    r.unpack("<fff", base + 4 + i * 12, "asset-language triplet")
                )
            ),
        )
        for i in range(count)
    ]


def _read_localize(
    r: _Reader, base: int, profile: GcfProfile
) -> tuple[int, list[LocalizeAsset]]:
    count, reserved = r.unpack("<II", base, "localize section header")
    r.require(base + 8, count * 0x10, "localize asset table")
    assets = []
    for i in range(count):
        slot, slot_reserved, path_at = r.unpack(
            "<IIQ", base + 8 + i * 0x10, "localize asset"
        )
        assets.append(
            LocalizeAsset(
                slot=slot,
                slot_name=profile.asset_language_name(slot),
                reserved=slot_reserved,
                path=r.utf16(path_at, f"localize path {i}"),
            )
        )
    return reserved, assets


def _pack_messages(b: _Builder, at: int, reserved: int, paths: list[str | None]) -> None:
    struct.pack_into("<II", b.data, at, len(paths), reserved)
    for i, path in enumerate(paths):
        struct.pack_into("<Q", b.data, at + 8 + i * 8, b.string(path))


def _pack_triplets(b: _Builder, at: int, triplets: list[AssetLanguageTriplet]) -> None:
    struct.pack_into("<I", b.data, at, len(triplets))
    for i, triplet in enumerate(triplets):
        struct.pack_into(
            "<fff",
            b.data,
            at + 4 + i * 12,
            _finite(triplet.value_0, "asset-language triplet value 0"),
            _finite(triplet.value_1, "asset-language triplet value 1"),
            _finite(triplet.value_2, "asset-language triplet value 2"),
        )


def _pack_localize(
    b: _Builder, at: int, reserved: int, assets: list[LocalizeAsset]
) -> None:
    struct.pack_into("<II", b.data, at, len(assets), reserved)
    for i, asset in enumerate(assets):
        struct.pack_into(
            "<IIQ", b.data, at + 8 + i * 0x10, asset.slot, asset.reserved, b.string(asset.path)
        )


def _slot_grid(model: GcfData, *, uniform_path_count: bool) -> list[FontSlotMapping]:
    expected = model.language_count * model.font_slot_count
    if model.language_count <= 0 or model.font_slot_count <= 0:
        raise GcfFormatError("GCF font dimensions must all be positive")
    if uniform_path_count and model.font_asset_path_count <= 0:
        raise GcfFormatError("GCF font dimensions must all be positive")
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
            if uniform_path_count and len(mapping.asset_paths) != (
                model.font_asset_path_count
            ):
                raise GcfFormatError(
                    f"font slot {language}/{slot} has {len(mapping.asset_paths)} "
                    f"paths, expected {model.font_asset_path_count}"
                )
            if not mapping.adjust_scale:
                raise GcfFormatError(f"font slot {language}/{slot} has no adjust scale")
            result.append(mapping)
    return result


class GcfGridCodec:
    def __init__(self, version: int, scales: int) -> None:
        self.profile = gcf_profile(version)
        self.scales = scales
        self.header_size = 0x28
        self.root_header_size = 0x10 if scales == 1 else 0x18

    def read(self, data: bytes) -> tuple[GcfData, GcfLayout]:
        r = _Reader(data)
        version, magic, root, message, triplet, localize = r.unpack(
            "<I4sQQQQ", 0, "GCF header"
        )
        if magic != GCF_MAGIC:
            raise GcfFormatError(f"expected GCFG magic, got {magic!r}")
        root = _section(r, root, "root")
        message = _section(r, message, "message section")
        triplet = _section(r, triplet, "triplet section")
        localize = _section(r, localize, "localize section")
        if not root < message <= triplet <= localize:
            raise GcfFormatError("GCF sections are out of order")

        if self.scales == 1:
            delay, language_count, slot_count, path_count = r.unpack(
                "<HHHH", root, "GCF v12 root"
            )
            (icon_at,) = r.unpack("<Q", root + 8, "GCF v12 icon path offset")
            ruby_ratio, root_reserved = 1.0, 0
        else:
            (
                delay,
                language_count,
                slot_count,
                path_count,
                ruby_ratio,
                root_reserved,
                icon_at,
            ) = r.unpack("<HHHHfIQ", root, "GCF root")
            _finite(ruby_ratio, "default ruby-size ratio")
        if delay not in (0, 1):
            raise GcfFormatError(
                f"isDelayLanguageFontLoad contains non-boolean value {delay}"
            )
        if not language_count or not slot_count or not path_count:
            raise GcfFormatError("GCF font dimensions must all be nonzero")
        if language_count > 512 or slot_count > 64 or path_count > 64:
            raise GcfFormatError("GCF font dimensions are implausible")

        slot_total = language_count * slot_count
        path_table = root + self.root_header_size
        adjust_table = path_table + slot_total * path_count * 8
        root_end = adjust_table + slot_total * 4 * self.scales
        if root_end != message:
            raise GcfFormatError(
                f"GCF root section ends at 0x{root_end:X}, expected 0x{message:X}"
            )

        profile = gcf_profile_or_generic(version)
        font_slots: list[FontSlotMapping] = []
        for language in range(language_count):
            for slot in range(slot_count):
                row = slot + slot_count * language
                paths = [
                    r.utf16(
                        *r.unpack(
                            "<Q",
                            path_table + (candidate + path_count * row) * 8,
                            "font path offset",
                        ),
                        f"font path language={language} slot={slot} candidate={candidate}",
                    )
                    for candidate in range(path_count)
                ]
                adjust = r.unpack(
                    "<" + "f" * self.scales,
                    adjust_table + row * 4 * self.scales,
                    "font adjust scale",
                )
                font_slots.append(
                    FontSlotMapping(
                        language_index=language,
                        language_name=profile.language_name(language),
                        slot_index=slot,
                        slot_name=profile.font_slot_name(slot),
                        asset_paths=tuple(paths),
                        adjust_scale=tuple(_finite(v, "font adjust scale") for v in adjust),
                    )
                )

        message_reserved, message_paths = _read_messages(r, message)
        triplets = _read_triplets(r, triplet, profile)
        localize_reserved, localize_assets = _read_localize(r, localize, profile)
        model = GcfData(
            version=version,
            delay_language_font_load_raw=delay,
            language_count=language_count,
            font_slot_count=slot_count,
            font_asset_path_count=path_count,
            default_ruby_size_ratio=ruby_ratio,
            root_reserved=root_reserved,
            icon_font_asset_path=r.utf16(icon_at, "icon font path"),
            font_slots=font_slots,
            message_section_reserved=message_reserved,
            message_asset_paths=message_paths,
            asset_language_triplets=triplets,
            localize_section_reserved=localize_reserved,
            localize_assets=localize_assets,
        )
        return model, GcfLayout(root, message, triplet, localize)

    def write(self, model: GcfData) -> bytes:
        if model.delay_language_font_load_raw not in (0, 1):
            raise GcfFormatError("isDelayLanguageFontLoad must be 0 or 1")
        if self.scales == 2:
            _finite(model.default_ruby_size_ratio, "default ruby-size ratio")
        slots = _slot_grid(model, uniform_path_count=True)
        for mapping in slots:
            if self.scales == 2 and len(mapping.adjust_scale) != 2:
                raise GcfFormatError(
                    f"font slot {mapping.language_index}/{mapping.slot_index} "
                    "adjust scale must be Float2"
                )

        path_total = len(slots) * model.font_asset_path_count
        b = _Builder()
        header = b.reserve(self.header_size)
        root = b.reserve(
            self.root_header_size + path_total * 8 + len(slots) * 4 * self.scales
        )
        message = b.reserve(8 + len(model.message_asset_paths) * 8)
        triplet = b.reserve(4 + len(model.asset_language_triplets) * 12)
        localize = b.reserve(8 + len(model.localize_assets) * 0x10)

        icon_offset = b.string(model.icon_font_asset_path)
        root_fields: tuple[Any, ...] = (
            model.delay_language_font_load_raw,
            model.language_count,
            model.font_slot_count,
            model.font_asset_path_count,
        )
        if self.scales == 1:
            struct.pack_into("<HHHHQ", b.data, root, *root_fields, icon_offset)
        else:
            struct.pack_into(
                "<HHHHfIQ",
                b.data,
                root,
                *root_fields,
                _finite(model.default_ruby_size_ratio, "default ruby-size ratio"),
                model.root_reserved,
                icon_offset,
            )
        path_table = root + self.root_header_size
        adjust_table = path_table + path_total * 8
        for row, mapping in enumerate(slots):
            for candidate, path in enumerate(mapping.asset_paths):
                struct.pack_into(
                    "<Q",
                    b.data,
                    path_table + (candidate + model.font_asset_path_count * row) * 8,
                    b.string(path),
                )
            adjust = (
                (mapping.scale_for(0),)
                if self.scales == 1
                else (mapping.adjust_scale[0], mapping.adjust_scale[1])
            )
            struct.pack_into(
                "<" + "f" * self.scales,
                b.data,
                adjust_table + row * 4 * self.scales,
                *(_finite(v, "font adjust scale") for v in adjust),
            )

        _pack_messages(b, message, model.message_section_reserved, model.message_asset_paths)
        _pack_triplets(b, triplet, model.asset_language_triplets)
        _pack_localize(b, localize, model.localize_section_reserved, model.localize_assets)
        struct.pack_into(
            "<I4sQQQQ", b.data, header, model.version, GCF_MAGIC, root, message, triplet, localize
        )
        return bytes(b.data)


class GcfRecordCodec:
    def __init__(self, version: int, *, fslt: bool, sentinel: bool = False) -> None:
        self.profile = gcf_profile(version)
        self.fslt = fslt
        self.sentinel = sentinel
        self.slot_count = 16 if fslt else 10
        self.header_size = 0x38 if fslt else 0x30
        self.root_header_size = 0x20 if fslt else 0x10

    def read(self, data: bytes) -> tuple[GcfData, GcfLayout]:
        r = _Reader(data)
        fields = r.unpack("<I4sQQQQQQ" if self.fslt else "<I4sQQQQQ", 0, "GCF header")
        version, magic = fields[:2]
        if magic != GCF_MAGIC:
            raise GcfFormatError(f"expected GCFG magic, got {magic!r}")
        root, message, triplet, localize = fields[2:6]
        aux, tail = (fields[6], fields[7]) if self.fslt else (0, fields[6])
        root = _section(r, root, "root")
        message = _section(r, message, "message section")
        triplet = _section(r, triplet, "triplet section")
        localize = _section(r, localize, "localize section")
        if self.fslt:
            aux = _section(r, aux, "aux section")
            tail = _section(r, tail, "tail section")
            if not root < message <= triplet <= localize <= aux <= tail:
                raise GcfFormatError("GCF sections are out of order")
            language_count, f0, f1, f2, f3, root_reserved, icon_at = r.unpack(
                "<IffffIQ", root, "GCF root"
            )
            root_floats = tuple(
                _finite(v, f"GCF root float {i}") for i, v in enumerate((f0, f1, f2, f3))
            )
            delay = 0
        else:
            tail = _section(r, tail, "tail section")
            if not root < message <= triplet <= localize <= tail:
                raise GcfFormatError("GCF sections are out of order")
            delay, language_count, ruby, icon_at = r.unpack("<HHfQ", root, "GCF v19 root")
            if delay not in (0, 1):
                raise GcfFormatError(
                    f"isDelayLanguageFontLoad contains non-boolean value {delay}"
                )
            root_floats = (_finite(ruby, "default ruby-size ratio"),)
            root_reserved = 0
        icon_path = r.utf16(icon_at, "icon font path")
        if not 1 <= language_count <= 512:
            raise GcfFormatError(f"implausible GCF language count {language_count}")

        slot_total = language_count * self.slot_count
        record_table = root + self.root_header_size
        font_slot_list_paths: list[str | None] = []
        if self.fslt:
            r.require(record_table, language_count * 8, "GCF font slot lists")
            for i in range(language_count):
                font_slot_list_paths.append(
                    r.utf16(
                        *r.unpack("<Q", record_table + i * 8, "font slot list offset"),
                        f"font slot list {i}",
                    )
                )
            record_table += language_count * 8
        r.require(record_table, slot_total * 8, "GCF font record table")
        record_data = record_table + slot_total * 8
        records_end = message
        if self.sentinel:
            (sentinel,) = r.unpack("<Q", record_data, "GCF record table end")
            record_data += 8
            records_end = sentinel or message - 8
            if not record_data <= records_end <= message:
                raise GcfFormatError(
                    f"GCF record table end 0x{sentinel:X} is inconsistent"
                )

        profile = gcf_profile_or_generic(version)
        font_slots: list[FontSlotMapping] = []
        for index in range(slot_total):
            (record_at,) = r.unpack(
                "<Q", record_table + index * 8, "font record offset"
            )
            if not record_data <= record_at < records_end:
                raise GcfFormatError(
                    f"font record offset 0x{record_at:X} is outside the "
                    "font record region"
                )
            candidate_count, scale_x, scale_y = r.unpack("<Qff", record_at, "font record")
            if candidate_count > 64:
                raise GcfFormatError(
                    f"font record candidate count {candidate_count} is implausible"
                )
            next_at = (
                r.unpack("<Q", record_table + (index + 1) * 8, "next font record offset")[0]
                if index + 1 < slot_total
                else records_end
            )
            record_end = record_at + 16 + candidate_count * 8
            if record_end != next_at:
                raise GcfFormatError(
                    f"font record {index} spans 0x{record_at:X}..0x{record_end:X}, "
                    f"next record starts at 0x{next_at:X}"
                )
            paths = [
                r.utf16(
                    *r.unpack(
                        "<Q", record_at + 16 + candidate * 8, "font record path offset"
                    ),
                    f"font record {index} path {candidate}",
                )
                for candidate in range(candidate_count)
            ]
            language, slot = divmod(index, self.slot_count)
            font_slots.append(
                FontSlotMapping(
                    language_index=language,
                    language_name=profile.language_name(language),
                    slot_index=slot,
                    slot_name=profile.font_slot_name(slot),
                    asset_paths=tuple(paths),
                    adjust_scale=(
                        _finite(scale_x, "font adjust scale X"),
                        _finite(scale_y, "font adjust scale Y"),
                    ),
                )
            )

        message_reserved, message_paths = _read_messages(r, message)
        triplets = _read_triplets(r, triplet, profile)
        localize_reserved, localize_assets = _read_localize(r, localize, profile)

        aux_asset_paths: list[str | None] = []
        if self.fslt:
            if (tail - aux) % 8:
                raise GcfFormatError("GCF aux section is not a path table")
            aux_count = (tail - aux) // 8
            if aux_count > 64:
                raise GcfFormatError(f"implausible GCF aux path count {aux_count}")
            aux_asset_paths = [
                r.utf16(*r.unpack("<Q", aux + i * 8, "aux path offset"), f"aux path {i}")
                for i in range(aux_count)
            ]
        string_base = min((p for p in r.refs if p), default=len(data))
        if string_base < tail or (string_base - tail) % 8:
            raise GcfFormatError("GCF tail section is not a path table")
        tail_count = (string_base - tail) // 8
        if not 1 <= tail_count <= 8:
            raise GcfFormatError(f"implausible GCF tail path count {tail_count}")
        tail_paths = [
            r.utf16(*r.unpack("<Q", tail + i * 8, "tail path offset"), f"tail path {i}")
            for i in range(tail_count)
        ]

        model = GcfData(
            version=version,
            delay_language_font_load_raw=delay,
            language_count=language_count,
            font_slot_count=self.slot_count,
            font_asset_path_count=max((len(m.asset_paths) for m in font_slots), default=0),
            default_ruby_size_ratio=root_floats[0],
            root_reserved=root_reserved,
            icon_font_asset_path=icon_path,
            font_slots=font_slots,
            message_section_reserved=message_reserved,
            message_asset_paths=message_paths,
            asset_language_triplets=triplets,
            localize_section_reserved=localize_reserved,
            localize_assets=localize_assets,
            font_slot_list_paths=font_slot_list_paths,
            extra_root_floats=root_floats[1:],
            aux_asset_paths=aux_asset_paths,
            aux_tail_asset_paths=tail_paths,
        )
        return model, GcfLayout(root, message, triplet, localize, aux or None, tail)

    def write(self, model: GcfData) -> bytes:
        if model.font_slot_count != self.slot_count:
            raise GcfFormatError(
                f"GCF layouts with records have {self.slot_count} font slots, "
                f"got {model.font_slot_count}"
            )
        if self.fslt:
            if len(model.font_slot_list_paths) != model.language_count:
                raise GcfFormatError(
                    f"GCF expects {model.language_count} font slot lists, "
                    f"got {len(model.font_slot_list_paths)}"
                )
            if len(model.extra_root_floats) != 3:
                raise GcfFormatError("GCF v24 root expects three trailing floats")
        elif model.font_slot_list_paths or model.extra_root_floats or model.aux_asset_paths:
            raise GcfFormatError("GCF v19 has no fslt/aux sections")
        slots = _slot_grid(model, uniform_path_count=False)
        for mapping in slots:
            if len(mapping.adjust_scale) > 2:
                raise GcfFormatError(
                    f"font slot {mapping.language_index}/{mapping.slot_index} "
                    "adjust scale exceeds Float2"
                )

        pad = 8 if self.sentinel else 0
        b = _Builder()
        header = b.reserve(self.header_size)
        root = b.reserve(
            self.root_header_size
            + (model.language_count * 8 if self.fslt else 0)
            + len(slots) * 8
            + pad
            + sum(16 + 8 * len(m.asset_paths) for m in slots)
            + pad
        )
        message = b.reserve(8 + len(model.message_asset_paths) * 8)
        triplet = b.reserve(4 + len(model.asset_language_triplets) * 12)
        localize = b.reserve(8 + len(model.localize_assets) * 0x10)
        aux = b.reserve(len(model.aux_asset_paths) * 8) if self.fslt else 0
        tail = b.reserve(len(model.aux_tail_asset_paths) * 8)

        icon_offset = b.string(model.icon_font_asset_path)
        if self.fslt:
            root_floats = (
                _finite(model.default_ruby_size_ratio, "default ruby-size ratio"),
                *(_finite(v, "GCF root float") for v in model.extra_root_floats),
            )
            struct.pack_into(
                "<IffffIQ",
                b.data,
                root,
                model.language_count,
                *root_floats,
                model.root_reserved,
                icon_offset,
            )
            path_table = root + self.root_header_size
            for i, path in enumerate(model.font_slot_list_paths):
                struct.pack_into("<Q", b.data, path_table + i * 8, b.string(path))
            record_table = path_table + model.language_count * 8
        else:
            struct.pack_into(
                "<HHfQ",
                b.data,
                root,
                model.delay_language_font_load_raw,
                model.language_count,
                _finite(model.default_ruby_size_ratio, "default ruby-size ratio"),
                icon_offset,
            )
            record_table = root + self.root_header_size

        record_cursor = record_table + len(slots) * 8 + pad
        for index, mapping in enumerate(slots):
            struct.pack_into("<Q", b.data, record_table + index * 8, record_cursor)
            struct.pack_into(
                "<Qff",
                b.data,
                record_cursor,
                len(mapping.asset_paths),
                _finite(mapping.scale_for(0), "font adjust scale X"),
                _finite(mapping.scale_for(1), "font adjust scale Y"),
            )
            for candidate, path in enumerate(mapping.asset_paths):
                struct.pack_into(
                    "<Q", b.data, record_cursor + 16 + candidate * 8, b.string(path)
                )
            record_cursor += 16 + 8 * len(mapping.asset_paths)
        if self.sentinel:
            struct.pack_into(
                "<Q",
                b.data,
                record_table + len(slots) * 8,
                record_cursor if model.root_reserved else 0,
            )

        _pack_messages(b, message, model.message_section_reserved, model.message_asset_paths)
        _pack_triplets(b, triplet, model.asset_language_triplets)
        _pack_localize(b, localize, model.localize_section_reserved, model.localize_assets)
        if self.fslt:
            for i, path in enumerate(model.aux_asset_paths):
                struct.pack_into("<Q", b.data, aux + i * 8, b.string(path))
        for i, path in enumerate(model.aux_tail_asset_paths):
            struct.pack_into("<Q", b.data, tail + i * 8, b.string(path))
        struct.pack_into(
            "<I4sQQQQQQ" if self.fslt else "<I4sQQQQQ",
            b.data,
            header,
            model.version,
            GCF_MAGIC,
            root,
            message,
            triplet,
            localize,
            *((aux, tail) if self.fslt else (tail,)),
        )
        return bytes(b.data)


_V12_CODEC: GcfCodec = GcfGridCodec(12, 1)
_V15_CODEC: GcfCodec = GcfGridCodec(15, 2)
_V19_CODEC: GcfCodec = GcfRecordCodec(19, fslt=False)
_V24_CODEC: GcfCodec = GcfRecordCodec(24, fslt=True)
_V29_CODEC: GcfCodec = GcfRecordCodec(29, fslt=True, sentinel=True)

_RT_VERSIONS = (0x01010118, 0x02020118, 0x03010118, 0x04030118)

GCF_CODECS: dict[int, GcfCodec] = {
    12: _V12_CODEC,
    15: _V15_CODEC,
    19: _V19_CODEC,
    24: _V24_CODEC,
    26: _V24_CODEC,
    27: _V24_CODEC,
    28: _V24_CODEC,
    29: _V29_CODEC,
    **{version: _V24_CODEC for version in _RT_VERSIONS},
}

_LAYOUT_CODECS: tuple[GcfCodec, ...] = (
    _V24_CODEC,
    _V29_CODEC,
    _V19_CODEC,
    _V15_CODEC,
    _V12_CODEC,
)


def gcf_codec(version: int) -> GcfCodec:
    try:
        return GCF_CODECS[int(version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(GCF_CODECS)))
        raise GcfFormatError(
            f"unsupported GCF version {version}; supported versions: {supported}"
        ) from exc


def _codec_candidates(version: int) -> tuple[GcfCodec, ...]:
    registered = GCF_CODECS.get(int(version))
    if registered is None:
        return _LAYOUT_CODECS
    return (registered, *(c for c in _LAYOUT_CODECS if c is not registered))


def decode_gcf(data: bytes) -> tuple[GcfData, GcfLayout, GcfCodec]:
    if len(data) < 8:
        raise GcfFormatError("file is too small for a GCF header")
    version, magic = struct.unpack_from("<I4s", data, 0)
    if magic != GCF_MAGIC:
        raise GcfFormatError(f"expected GCFG magic, got {magic!r}")
    failures: list[str] = []
    for codec in _codec_candidates(version):
        try:
            model, layout = codec.read(data)
        except GcfFormatError as exc:
            failures.append(f"{type(codec).__name__}: {exc}")
            continue
        return model, layout, codec
    raise GcfFormatError(
        f"no GCF layout matches version {version}: " + "; ".join(failures)
    )


def encode_gcf(model: GcfData) -> bytes:
    return gcf_codec(model.version).write(model)
