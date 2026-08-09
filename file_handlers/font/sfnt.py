"""Bounded OpenType/TrueType SFNT layout semantics.

The parser exposes the data needed by RE Engine GUI layout: Unicode cmap
lookup, horizontal and vertical advances, names, legacy kerning, GPOS pair
positioning, and GSUB ``vert``/``vrt2`` single-glyph substitutions. Outlines
remain in the original bytes for a native or Qt rasterizer to consume.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import struct


class SfntFormatError(ValueError):
    pass


@dataclass(frozen=True)
class SfntTableRecord:
    tag: str
    checksum: int
    offset: int
    length: int


@dataclass(frozen=True)
class SfntCmapGroup:
    start_codepoint: int
    end_codepoint: int
    glyph: int
    constant_glyph: bool = False


@dataclass(frozen=True)
class SfntCmap(Mapping[int, int]):
    platform_id: int
    encoding_id: int
    format: int
    language: int
    offset: int
    length: int
    mapping: Mapping[int, int]
    groups: tuple[SfntCmapGroup, ...] = ()

    def glyph_index(self, codepoint: int) -> int:
        if codepoint < 0 or codepoint > 0x10FFFF:
            return 0
        glyph = self.mapping.get(codepoint)
        if glyph is not None:
            return glyph
        if not self.groups:
            return 0
        starts = tuple(group.start_codepoint for group in self.groups)
        index = bisect_right(starts, codepoint) - 1
        if index < 0:
            return 0
        group = self.groups[index]
        if codepoint > group.end_codepoint:
            return 0
        if group.constant_glyph:
            return group.glyph
        return group.glyph + codepoint - group.start_codepoint

    def __getitem__(self, codepoint: int) -> int:
        glyph = self.glyph_index(codepoint)
        if not glyph:
            raise KeyError(codepoint)
        return glyph

    def __iter__(self) -> Iterator[int]:
        return (codepoint for codepoint, _ in self.mappings())

    def __len__(self) -> int:
        return self.coverage_count

    @property
    def coverage_count(self) -> int:
        return len(self.mapping) + sum(
            group.end_codepoint - group.start_codepoint + 1
            for group in self.groups
        )

    def mappings(self) -> Iterator[tuple[int, int]]:
        yield from sorted(self.mapping.items())
        for group in self.groups:
            for codepoint in range(group.start_codepoint, group.end_codepoint + 1):
                glyph = (
                    group.glyph
                    if group.constant_glyph
                    else group.glyph + codepoint - group.start_codepoint
                )
                if glyph:
                    yield codepoint, glyph


@dataclass(frozen=True)
class SfntLineMetrics:
    ascender: int
    descender: int
    line_gap: int
    maximum_advance: int
    metric_count: int


@dataclass(frozen=True)
class SfntGlyphMetric:
    advance: int
    side_bearing: int


@dataclass(frozen=True)
class SfntValueAdjustment:
    """The scalar part of an OpenType GPOS ValueRecord."""

    x_placement: int = 0
    y_placement: int = 0
    x_advance: int = 0
    y_advance: int = 0

    def __add__(self, other: "SfntValueAdjustment") -> "SfntValueAdjustment":
        return SfntValueAdjustment(
            self.x_placement + other.x_placement,
            self.y_placement + other.y_placement,
            self.x_advance + other.x_advance,
            self.y_advance + other.y_advance,
        )

    @property
    def is_zero(self) -> bool:
        return not (
            self.x_placement
            or self.y_placement
            or self.x_advance
            or self.y_advance
        )


@dataclass(frozen=True)
class SfntPairAdjustment:
    first: SfntValueAdjustment = SfntValueAdjustment()
    second: SfntValueAdjustment = SfntValueAdjustment()

    def __add__(self, other: "SfntPairAdjustment") -> "SfntPairAdjustment":
        return SfntPairAdjustment(self.first + other.first, self.second + other.second)

    @property
    def is_zero(self) -> bool:
        return self.first.is_zero and self.second.is_zero


@dataclass(frozen=True)
class SfntName:
    platform_id: int
    encoding_id: int
    language_id: int
    name_id: int
    value: str


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def require(self, offset: int, size: int, label: str) -> None:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise SfntFormatError(
                f"{label} at 0x{offset:X}+0x{size:X} exceeds "
                f"0x{len(self.data):X}"
            )

    def unpack(self, fmt: str, offset: int, label: str) -> tuple[int, ...]:
        size = struct.calcsize(fmt)
        self.require(offset, size, label)
        return struct.unpack_from(fmt, self.data, offset)

    def u16(self, offset: int, label: str) -> int:
        return self.unpack(">H", offset, label)[0]

    def i16(self, offset: int, label: str) -> int:
        return self.unpack(">h", offset, label)[0]

    def u32(self, offset: int, label: str) -> int:
        return self.unpack(">I", offset, label)[0]


@dataclass(frozen=True)
class _GposExplicitPairs:
    pairs: Mapping[tuple[int, int], SfntPairAdjustment]

    def adjustment(self, left: int, right: int) -> SfntPairAdjustment | None:
        return self.pairs.get((left, right))


@dataclass(frozen=True)
class _GposClassPairs:
    coverage: frozenset[int]
    class_1: Mapping[int, int]
    class_2: Mapping[int, int]
    matrix: tuple[tuple[SfntPairAdjustment, ...], ...]

    def adjustment(self, left: int, right: int) -> SfntPairAdjustment | None:
        if left not in self.coverage:
            return None
        first_class = self.class_1.get(left, 0)
        second_class = self.class_2.get(right, 0)
        if first_class >= len(self.matrix):
            return None
        row = self.matrix[first_class]
        if second_class >= len(row):
            return None
        return row[second_class]


@dataclass(frozen=True)
class _GposLookup:
    subtables: tuple[_GposExplicitPairs | _GposClassPairs, ...]

    def adjustment(self, left: int, right: int) -> SfntPairAdjustment | None:
        # Subtables within one lookup are alternate coverage partitions.  The
        # first applicable subtable wins; separate lookups accumulate.
        for subtable in self.subtables:
            result = subtable.adjustment(left, right)
            if result is not None:
                return result
        return None


class SfntFont:
    """One face from a decoded SFNT or TTC payload."""

    def __init__(self, data: bytes, *, face_index: int = 0) -> None:
        self.data = data
        self._reader = _Reader(data)
        self.face_offsets = self._face_offsets()
        if face_index < 0 or face_index >= len(self.face_offsets):
            raise SfntFormatError(
                f"face index {face_index} outside 0..{len(self.face_offsets) - 1}"
            )
        self.face_index = face_index
        self.face_offset = self.face_offsets[face_index]
        self.sfnt_version, self.tables = self._read_directory()
        self.units_per_em, self.bounding_box, self.index_to_loc_format = (
            self._read_head()
        )
        self.glyph_count = self._read_glyph_count()
        self.horizontal_line_metrics, self.horizontal_metrics = self._read_metrics(
            "hhea", "hmtx"
        )
        self.vertical_line_metrics, self.vertical_metrics = self._read_metrics(
            "vhea", "vmtx", required=False
        )
        self.cmap_diagnostics: list[str] = []
        self.cmaps = self._read_cmaps()
        if not self.cmaps:
            raise SfntFormatError("font has no supported Unicode cmap")
        self.best_cmap = max(self.cmaps, key=self._cmap_score)
        self.names = self._read_names()
        self.kerning_pairs = self._read_legacy_kern()
        self.gsub_features, self.gsub_lookups = self._read_gsub()
        self.gpos_features, self.gpos_lookups = self._read_gpos()

    def _face_offsets(self) -> tuple[int, ...]:
        self._reader.require(0, 4, "sfnt signature")
        if self.data[:4] != b"ttcf":
            return (0,)
        _, count = self._reader.unpack(">II", 4, "TTC header")
        if count == 0 or count > 4096:
            raise SfntFormatError(f"invalid TTC face count {count}")
        self._reader.require(12, count * 4, "TTC face-offset array")
        result = tuple(
            self._reader.u32(12 + index * 4, "TTC face offset")
            for index in range(count)
        )
        for index, offset in enumerate(result):
            self._reader.require(offset, 12, f"TTC face {index}")
        return result

    def _read_directory(self) -> tuple[bytes, Mapping[str, SfntTableRecord]]:
        base = self.face_offset
        self._reader.require(base, 12, "sfnt offset table")
        version = self.data[base : base + 4]
        if version not in (b"OTTO", b"\x00\x01\x00\x00", b"true", b"typ1"):
            raise SfntFormatError(f"unsupported sfnt version {version!r}")
        table_count = self._reader.u16(base + 4, "sfnt table count")
        if table_count == 0 or table_count > 4096:
            raise SfntFormatError(f"invalid sfnt table count {table_count}")
        directory = base + 12
        self._reader.require(directory, table_count * 16, "sfnt table directory")
        tables: dict[str, SfntTableRecord] = {}
        for index in range(table_count):
            record_offset = directory + index * 16
            raw_tag = self.data[record_offset : record_offset + 4]
            try:
                tag = raw_tag.decode("ascii")
            except UnicodeDecodeError as exc:
                raise SfntFormatError(f"table {index} has a non-ASCII tag") from exc
            checksum, offset, length = self._reader.unpack(
                ">III", record_offset + 4, f"{tag!r} table record"
            )
            self._reader.require(offset, length, f"{tag!r} table")
            if tag in tables:
                raise SfntFormatError(f"duplicate sfnt table {tag!r}")
            tables[tag] = SfntTableRecord(tag, checksum, offset, length)
        return version, tables

    def table_data(self, tag: str) -> bytes | None:
        record = self.tables.get(tag)
        if record is None:
            return None
        return self.data[record.offset : record.offset + record.length]

    def _table(self, tag: str, minimum: int = 0) -> SfntTableRecord:
        record = self.tables.get(tag)
        if record is None:
            raise SfntFormatError(f"required sfnt table {tag!r} is absent")
        if record.length < minimum:
            raise SfntFormatError(
                f"sfnt table {tag!r} is 0x{record.length:X}, expected at least "
                f"0x{minimum:X}"
            )
        return record

    def _read_head(self) -> tuple[int, tuple[int, int, int, int], int]:
        table = self._table("head", 54)
        magic = self._reader.u32(table.offset + 12, "head magic")
        if magic != 0x5F0F3CF5:
            raise SfntFormatError(f"invalid head magic 0x{magic:08X}")
        units = self._reader.u16(table.offset + 18, "head unitsPerEm")
        if not 16 <= units <= 16384:
            raise SfntFormatError(f"invalid unitsPerEm {units}")
        box = self._reader.unpack(">hhhh", table.offset + 36, "head bounding box")
        loc_format = self._reader.i16(table.offset + 50, "head indexToLocFormat")
        if loc_format not in (0, 1):
            raise SfntFormatError(f"invalid indexToLocFormat {loc_format}")
        return units, box, loc_format

    def _read_glyph_count(self) -> int:
        table = self._table("maxp", 6)
        count = self._reader.u16(table.offset + 4, "maxp numGlyphs")
        if count == 0:
            raise SfntFormatError("font has zero glyphs")
        return count

    def _read_metrics(
        self,
        header_tag: str,
        metrics_tag: str,
        *,
        required: bool = True,
    ) -> tuple[SfntLineMetrics | None, tuple[SfntGlyphMetric, ...]]:
        header = self.tables.get(header_tag)
        metrics = self.tables.get(metrics_tag)
        if header is None and metrics is None and not required:
            return None, ()
        if header is None or metrics is None:
            raise SfntFormatError(
                f"{header_tag!r} and {metrics_tag!r} must be present together"
            )
        if header.length < 36:
            raise SfntFormatError(f"{header_tag!r} is shorter than 36 bytes")
        ascender, descender, line_gap, maximum = self._reader.unpack(
            ">hhhH", header.offset + 4, f"{header_tag} line metrics"
        )
        metric_count = self._reader.u16(
            header.offset + 34, f"{header_tag} metric count"
        )
        if metric_count == 0 or metric_count > self.glyph_count:
            raise SfntFormatError(
                f"{header_tag} metric count {metric_count} exceeds glyph count "
                f"{self.glyph_count}"
            )
        required_size = metric_count * 4 + (self.glyph_count - metric_count) * 2
        if metrics.length < required_size:
            raise SfntFormatError(
                f"{metrics_tag} is 0x{metrics.length:X}, needs 0x{required_size:X}"
            )
        result: list[SfntGlyphMetric] = []
        for index in range(metric_count):
            advance, bearing = self._reader.unpack(
                ">Hh", metrics.offset + index * 4, f"{metrics_tag} long metric"
            )
            result.append(SfntGlyphMetric(advance, bearing))
        repeated_advance = result[-1].advance
        trailing = metrics.offset + metric_count * 4
        for index in range(metric_count, self.glyph_count):
            bearing = self._reader.i16(
                trailing + (index - metric_count) * 2,
                f"{metrics_tag} trailing bearing",
            )
            result.append(SfntGlyphMetric(repeated_advance, bearing))
        return (
            SfntLineMetrics(
                ascender, descender, line_gap, maximum, metric_count
            ),
            tuple(result),
        )

    def _read_cmaps(self) -> tuple[SfntCmap, ...]:
        table = self._table("cmap", 4)
        version, count = self._reader.unpack(">HH", table.offset, "cmap header")
        if version != 0:
            raise SfntFormatError(f"unsupported cmap version {version}")
        if count > 4096:
            raise SfntFormatError(f"invalid cmap encoding count {count}")
        self._reader.require(table.offset + 4, count * 8, "cmap encoding records")
        result: list[SfntCmap] = []
        seen: set[tuple[int, int, int]] = set()
        for index in range(count):
            record = table.offset + 4 + index * 8
            platform, encoding, relative = self._reader.unpack(
                ">HHI", record, "cmap encoding record"
            )
            subtable = table.offset + relative
            self._reader.require(subtable, 2, "cmap subtable")
            format_number = self._reader.u16(subtable, "cmap format")
            key = (platform, encoding, subtable)
            if key in seen:
                continue
            seen.add(key)
            try:
                parsed = self._read_cmap_subtable(
                    platform, encoding, format_number, subtable, table
                )
            except SfntFormatError as exc:
                # Two shipped Chinese fonts contain a broken legacy Macintosh
                # format-6 record (declared length 0xFFFF, 64,767 glyphs).
                # Their Unicode platform 0/3 cmaps are valid and are the ones
                # selected by the game/Windows font path.
                if platform in (0, 3):
                    raise
                self.cmap_diagnostics.append(
                    f"ignored platform={platform} encoding={encoding} "
                    f"format={format_number}: {exc}"
                )
                continue
            if parsed is not None:
                result.append(parsed)
        return tuple(result)

    def _cmap_bounds(
        self, subtable: int, table: SfntTableRecord, length: int, label: str
    ) -> None:
        if length <= 0 or subtable < table.offset:
            raise SfntFormatError(f"invalid {label} length {length}")
        if subtable + length > table.offset + table.length:
            raise SfntFormatError(f"{label} exceeds cmap table bounds")

    def _read_cmap_subtable(
        self,
        platform: int,
        encoding: int,
        format_number: int,
        base: int,
        table: SfntTableRecord,
    ) -> SfntCmap | None:
        mapping: dict[int, int] = {}
        groups: tuple[SfntCmapGroup, ...] = ()
        if format_number in (0, 4, 6):
            length, language = self._reader.unpack(">HH", base + 2, "cmap header")
        elif format_number in (10, 12, 13):
            _, length, language = self._reader.unpack(
                ">HII", base + 2, "extended cmap header"
            )
        else:
            return None
        self._cmap_bounds(base, table, length, f"cmap format {format_number}")

        if format_number == 0:
            if length < 262:
                raise SfntFormatError("cmap format 0 is shorter than 262 bytes")
            for codepoint, glyph in enumerate(self.data[base + 6 : base + 262]):
                if glyph:
                    mapping[codepoint] = glyph
        elif format_number == 6:
            first, count = self._reader.unpack(">HH", base + 6, "cmap format 6")
            if 10 + count * 2 > length:
                raise SfntFormatError("cmap format 6 glyph array exceeds length")
            for index in range(count):
                glyph = self._reader.u16(base + 10 + index * 2, "cmap glyph")
                if glyph:
                    mapping[first + index] = glyph
        elif format_number == 10:
            first, count = self._reader.unpack(">II", base + 12, "cmap format 10")
            if 20 + count * 2 > length or first + count > 0x110000:
                raise SfntFormatError("invalid cmap format 10 character range")
            for index in range(count):
                glyph = self._reader.u16(base + 20 + index * 2, "cmap glyph")
                if glyph:
                    mapping[first + index] = glyph
        elif format_number == 4:
            seg_count_x2 = self._reader.u16(base + 6, "cmap segCountX2")
            if not seg_count_x2 or seg_count_x2 & 1:
                raise SfntFormatError(f"invalid cmap segCountX2 {seg_count_x2}")
            segment_count = seg_count_x2 // 2
            end_codes = base + 14
            start_codes = end_codes + segment_count * 2 + 2
            deltas = start_codes + segment_count * 2
            range_offsets = deltas + segment_count * 2
            if range_offsets + segment_count * 2 > base + length:
                raise SfntFormatError("cmap format 4 arrays exceed subtable")
            previous_end = -1
            for index in range(segment_count):
                end = self._reader.u16(end_codes + index * 2, "cmap endCode")
                start = self._reader.u16(start_codes + index * 2, "cmap startCode")
                delta = self._reader.i16(deltas + index * 2, "cmap idDelta")
                range_offset_word = range_offsets + index * 2
                range_offset = self._reader.u16(
                    range_offset_word, "cmap idRangeOffset"
                )
                if start > end or start <= previous_end:
                    raise SfntFormatError("cmap format 4 segments are not ordered")
                previous_end = end
                for codepoint in range(start, end + 1):
                    if codepoint == 0xFFFF:
                        continue
                    if range_offset == 0:
                        glyph = (codepoint + delta) & 0xFFFF
                    else:
                        glyph_offset = (
                            range_offset_word + range_offset + 2 * (codepoint - start)
                        )
                        if glyph_offset + 2 > base + length:
                            raise SfntFormatError(
                                "cmap format 4 glyph index exceeds subtable"
                            )
                        glyph = self._reader.u16(glyph_offset, "cmap glyph index")
                        if glyph:
                            glyph = (glyph + delta) & 0xFFFF
                    if glyph:
                        mapping[codepoint] = glyph
        else:
            group_count = self._reader.u32(base + 12, "cmap group count")
            if group_count > 0x100000 or 16 + group_count * 12 > length:
                raise SfntFormatError(f"invalid cmap group count {group_count}")
            parsed_groups: list[SfntCmapGroup] = []
            previous_end = -1
            for index in range(group_count):
                start, end, glyph = self._reader.unpack(
                    ">III", base + 16 + index * 12, "cmap group"
                )
                if start > end or start <= previous_end or end > 0x10FFFF:
                    raise SfntFormatError("cmap groups are invalid or unordered")
                previous_end = end
                parsed_groups.append(
                    SfntCmapGroup(start, end, glyph, format_number == 13)
                )
            groups = tuple(parsed_groups)

        return SfntCmap(
            platform,
            encoding,
            format_number,
            language,
            base,
            length,
            mapping,
            groups,
        )

    @staticmethod
    def _cmap_score(cmap: SfntCmap) -> tuple[int, int, int]:
        if cmap.platform_id == 3 and cmap.encoding_id == 10:
            platform = 600
        elif cmap.platform_id == 0:
            platform = 500
        elif cmap.platform_id == 3 and cmap.encoding_id == 1:
            platform = 400
        elif cmap.platform_id == 3 and cmap.encoding_id == 0:
            platform = 300
        elif cmap.platform_id == 1:
            platform = 100
        else:
            platform = 0
        format_score = {12: 60, 10: 50, 4: 40, 13: 30, 6: 20, 0: 10}.get(
            cmap.format, 0
        )
        return platform, format_score, cmap.coverage_count

    def glyph_index(self, codepoint: int) -> int:
        return self.best_cmap.glyph_index(codepoint)

    def has_codepoint(self, codepoint: int) -> bool:
        return self.glyph_index(codepoint) != 0

    def glyph_metric(self, glyph_index: int, *, vertical: bool = False) -> SfntGlyphMetric:
        if glyph_index < 0 or glyph_index >= self.glyph_count:
            raise IndexError(
                f"glyph index {glyph_index} outside 0..{self.glyph_count - 1}"
            )
        metrics = (
            self.vertical_metrics
            if vertical and self.vertical_metrics
            else self.horizontal_metrics
        )
        return metrics[glyph_index]

    def scaled_advance(
        self, glyph_index: int, pixel_size: float, *, vertical: bool = False
    ) -> float:
        return self.glyph_metric(glyph_index, vertical=vertical).advance * (
            pixel_size / self.units_per_em
        )

    def _read_names(self) -> tuple[SfntName, ...]:
        table = self.tables.get("name")
        if table is None:
            return ()
        if table.length < 6:
            raise SfntFormatError("name table is shorter than 6 bytes")
        _, count, strings_relative = self._reader.unpack(
            ">HHH", table.offset, "name header"
        )
        if 6 + count * 12 > table.length or strings_relative > table.length:
            raise SfntFormatError("name records exceed table bounds")
        strings = table.offset + strings_relative
        result: list[SfntName] = []
        for index in range(count):
            record = table.offset + 6 + index * 12
            platform, encoding, language, name_id, length, relative = (
                self._reader.unpack(">HHHHHH", record, "name record")
            )
            start = strings + relative
            if start < table.offset or start + length > table.offset + table.length:
                raise SfntFormatError("name string exceeds table bounds")
            raw = self.data[start : start + length]
            try:
                if platform in (0, 3):
                    value = raw.decode("utf-16-be")
                elif platform == 1:
                    value = raw.decode("mac_roman")
                else:
                    value = raw.decode("latin-1")
            except UnicodeDecodeError:
                continue
            result.append(SfntName(platform, encoding, language, name_id, value))
        return tuple(result)

    def name(self, name_id: int, *, language_id: int = 0x0409) -> str | None:
        candidates = [entry for entry in self.names if entry.name_id == name_id]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda entry: (
                entry.language_id == language_id,
                entry.platform_id == 3,
                entry.platform_id == 0,
            ),
        ).value

    @property
    def family_name(self) -> str | None:
        return self.name(1)

    @property
    def full_name(self) -> str | None:
        return self.name(4)

    def _read_legacy_kern(self) -> Mapping[tuple[int, int], int]:
        table = self.tables.get("kern")
        if table is None or table.length < 4:
            return {}
        version, subtable_count = self._reader.unpack(">HH", table.offset, "kern header")
        if version != 0:
            return {}
        cursor = table.offset + 4
        end = table.offset + table.length
        pairs: dict[tuple[int, int], int] = {}
        for _ in range(subtable_count):
            if cursor + 6 > end:
                raise SfntFormatError("kern subtable header exceeds table")
            _, length, coverage = self._reader.unpack(">HHH", cursor, "kern subtable")
            if length < 6 or cursor + length > end:
                raise SfntFormatError("kern subtable has invalid length")
            format_number = coverage >> 8
            horizontal = bool(coverage & 1)
            cross_stream = bool(coverage & 4)
            override = bool(coverage & 8)
            if format_number == 0 and horizontal and not cross_stream:
                if length < 14:
                    raise SfntFormatError("kern format 0 is too short")
                pair_count = self._reader.u16(cursor + 6, "kern pair count")
                if 14 + pair_count * 6 > length:
                    raise SfntFormatError("kern pair array exceeds subtable")
                for index in range(pair_count):
                    left, right, value = self._reader.unpack(
                        ">HHh", cursor + 14 + index * 6, "kern pair"
                    )
                    key = (left, right)
                    if override:
                        pairs[key] = value
                    else:
                        pairs[key] = pairs.get(key, 0) + value
            cursor += length
        return pairs

    def legacy_kerning(self, left_glyph: int, right_glyph: int) -> int:
        return self.kerning_pairs.get((left_glyph, right_glyph), 0)

    def _gpos_value_record(
        self,
        offset: int,
        value_format: int,
        label: str,
    ) -> tuple[SfntValueAdjustment, int]:
        if value_format & ~0x00FF:
            raise SfntFormatError(
                f"{label} uses reserved GPOS value-format bits 0x{value_format:04X}"
            )
        values = [0, 0, 0, 0]
        cursor = offset
        for bit in range(8):
            if not value_format & (1 << bit):
                continue
            # Bits 0..3 are signed design-unit values. Bits 4..7 are offsets
            # to optional Device/VariationIndex tables. Their PPEM-specific
            # deltas are rasterizer concerns, but the fields still occupy two
            # bytes and are bounds checked here.
            value = (
                self._reader.i16(cursor, f"{label} scalar")
                if bit < 4
                else self._reader.u16(cursor, f"{label} device offset")
            )
            if bit < 4:
                values[bit] = value
            cursor += 2
        return SfntValueAdjustment(*values), cursor

    def _class_definition(self, base: int, label: str) -> Mapping[int, int]:
        format_number = self._reader.u16(base, f"{label} format")
        result: dict[int, int] = {}
        if format_number == 1:
            start_glyph, glyph_count = self._reader.unpack(
                ">HH", base + 2, f"{label} format-1 header"
            )
            if glyph_count > self.glyph_count:
                raise SfntFormatError(f"{label} glyph count is implausible")
            for index in range(glyph_count):
                result[start_glyph + index] = self._reader.u16(
                    base + 6 + index * 2, f"{label} class value"
                )
            return result
        if format_number == 2:
            range_count = self._reader.u16(base + 2, f"{label} range count")
            if range_count > self.glyph_count + 1:
                raise SfntFormatError(f"{label} range count is implausible")
            previous_end = -1
            for index in range(range_count):
                start, end, class_value = self._reader.unpack(
                    ">HHH", base + 4 + index * 6, f"{label} range"
                )
                if start > end or start <= previous_end:
                    raise SfntFormatError(f"{label} contains invalid ranges")
                for glyph in range(start, end + 1):
                    result[glyph] = class_value
                previous_end = end
            return result
        raise SfntFormatError(f"unsupported {label} format {format_number}")

    def _pair_position(
        self, base: int
    ) -> _GposExplicitPairs | _GposClassPairs:
        (
            format_number,
            coverage_relative,
            value_format_1,
            value_format_2,
        ) = self._reader.unpack(">HHHH", base, "GPOS PairPos header")
        coverage = self._coverage(base + coverage_relative)
        if format_number == 1:
            pair_set_count = self._reader.u16(base + 8, "GPOS pair-set count")
            if pair_set_count != len(coverage):
                raise SfntFormatError(
                    "GPOS PairPos coverage and pair-set counts differ"
                )
            self._reader.require(base + 10, pair_set_count * 2, "GPOS pair-set offsets")
            pairs: dict[tuple[int, int], SfntPairAdjustment] = {}
            for first_index, first_glyph in enumerate(coverage):
                relative = self._reader.u16(
                    base + 10 + first_index * 2, "GPOS pair-set offset"
                )
                pair_set = base + relative
                pair_count = self._reader.u16(pair_set, "GPOS pair-value count")
                if pair_count > self.glyph_count:
                    raise SfntFormatError("GPOS pair-value count is implausible")
                cursor = pair_set + 2
                previous_second = -1
                for _ in range(pair_count):
                    second = self._reader.u16(cursor, "GPOS second glyph")
                    cursor += 2
                    if second <= previous_second:
                        raise SfntFormatError("GPOS PairValueRecords are not sorted")
                    first_value, cursor = self._gpos_value_record(
                        cursor, value_format_1, "GPOS first ValueRecord"
                    )
                    second_value, cursor = self._gpos_value_record(
                        cursor, value_format_2, "GPOS second ValueRecord"
                    )
                    pairs[first_glyph, second] = SfntPairAdjustment(
                        first_value, second_value
                    )
                    previous_second = second
            return _GposExplicitPairs(pairs)
        if format_number == 2:
            (
                class_def_1_relative,
                class_def_2_relative,
                class_1_count,
                class_2_count,
            ) = self._reader.unpack(">HHHH", base + 8, "GPOS class-pair header")
            if (
                class_1_count == 0
                or class_2_count == 0
                or class_1_count > self.glyph_count + 1
                or class_2_count > self.glyph_count + 1
            ):
                raise SfntFormatError("GPOS class-pair dimensions are implausible")
            class_1 = self._class_definition(
                base + class_def_1_relative, "GPOS ClassDef1"
            )
            class_2 = self._class_definition(
                base + class_def_2_relative, "GPOS ClassDef2"
            )
            cursor = base + 16
            rows: list[tuple[SfntPairAdjustment, ...]] = []
            for _ in range(class_1_count):
                row: list[SfntPairAdjustment] = []
                for _ in range(class_2_count):
                    first_value, cursor = self._gpos_value_record(
                        cursor, value_format_1, "GPOS class first ValueRecord"
                    )
                    second_value, cursor = self._gpos_value_record(
                        cursor, value_format_2, "GPOS class second ValueRecord"
                    )
                    row.append(SfntPairAdjustment(first_value, second_value))
                rows.append(tuple(row))
            return _GposClassPairs(
                frozenset(coverage), class_1, class_2, tuple(rows)
            )
        raise SfntFormatError(f"unsupported GPOS PairPos format {format_number}")

    def _read_gpos(
        self,
    ) -> tuple[Mapping[str, tuple[int, ...]], tuple[_GposLookup, ...]]:
        table = self.tables.get("GPOS")
        if table is None:
            return {}, ()
        if table.length < 10:
            raise SfntFormatError("GPOS table is shorter than its header")
        major, _, _, feature_relative, lookup_relative = self._reader.unpack(
            ">HHHHH", table.offset, "GPOS header"
        )
        if major != 1:
            raise SfntFormatError(f"unsupported GPOS major version {major}")

        feature_base = table.offset + feature_relative
        feature_count = self._reader.u16(feature_base, "GPOS feature count")
        self._reader.require(
            feature_base + 2, feature_count * 6, "GPOS feature records"
        )
        features: dict[str, list[int]] = {}
        for index in range(feature_count):
            record = feature_base + 2 + index * 6
            tag = self.data[record : record + 4].decode("ascii", errors="replace")
            relative = self._reader.u16(record + 4, "GPOS feature offset")
            feature = feature_base + relative
            lookup_count = self._reader.u16(
                feature + 2, "GPOS feature lookup count"
            )
            self._reader.require(
                feature + 4, lookup_count * 2, "GPOS feature lookup indices"
            )
            indices = [
                self._reader.u16(
                    feature + 4 + item * 2, "GPOS feature lookup index"
                )
                for item in range(lookup_count)
            ]
            bucket = features.setdefault(tag, [])
            for lookup_index in indices:
                if lookup_index not in bucket:
                    bucket.append(lookup_index)

        lookup_base = table.offset + lookup_relative
        lookup_count = self._reader.u16(lookup_base, "GPOS lookup count")
        self._reader.require(
            lookup_base + 2, lookup_count * 2, "GPOS lookup offsets"
        )
        lookup_offsets = [
            self._reader.u16(lookup_base + 2 + index * 2, "GPOS lookup offset")
            for index in range(lookup_count)
        ]
        lookups: list[_GposLookup] = []
        for relative in lookup_offsets:
            lookup = lookup_base + relative
            lookup_type, _, subtable_count = self._reader.unpack(
                ">HHH", lookup, "GPOS lookup header"
            )
            self._reader.require(
                lookup + 6, subtable_count * 2, "GPOS subtable offsets"
            )
            subtables: list[_GposExplicitPairs | _GposClassPairs] = []
            for index in range(subtable_count):
                subtable_relative = self._reader.u16(
                    lookup + 6 + index * 2, "GPOS subtable offset"
                )
                subtable = lookup + subtable_relative
                effective_type = lookup_type
                effective = subtable
                if lookup_type == 9:
                    extension_format, effective_type, extension_relative = (
                        self._reader.unpack(">HHI", subtable, "GPOS extension")
                    )
                    if extension_format != 1:
                        raise SfntFormatError(
                            f"unsupported GPOS extension format {extension_format}"
                        )
                    effective = subtable + extension_relative
                if effective_type == 2:
                    subtables.append(self._pair_position(effective))
            lookups.append(_GposLookup(tuple(subtables)))
        for tag, indices in features.items():
            for lookup_index in indices:
                if lookup_index >= len(lookups):
                    raise SfntFormatError(
                        f"GPOS feature {tag!r} references lookup {lookup_index}"
                    )
        return {tag: tuple(indices) for tag, indices in features.items()}, tuple(lookups)

    def pair_adjustment(
        self,
        left_glyph: int,
        right_glyph: int,
        *,
        vertical: bool = False,
    ) -> SfntPairAdjustment:
        """Apply the SFNT ``kern``/``vkrn`` GPOS lookups for one glyph pair."""

        feature = "vkrn" if vertical else "kern"
        result = SfntPairAdjustment()
        for lookup_index in self.gpos_features.get(feature, ()):
            adjustment = self.gpos_lookups[lookup_index].adjustment(
                int(left_glyph), int(right_glyph)
            )
            if adjustment is not None:
                result = result + adjustment
        return result

    def kerning(self, left_glyph: int, right_glyph: int) -> int:
        """Return horizontal advance kerning, preferring GPOS over ``kern``."""

        if "kern" in self.gpos_features:
            return self.pair_adjustment(left_glyph, right_glyph).first.x_advance
        return self.legacy_kerning(left_glyph, right_glyph)

    def _coverage(self, base: int) -> tuple[int, ...]:
        format_number = self._reader.u16(base, "GSUB coverage format")
        count = self._reader.u16(base + 2, "GSUB coverage count")
        if count > self.glyph_count + 1:
            raise SfntFormatError(f"invalid GSUB coverage count {count}")
        if format_number == 1:
            return tuple(
                self._reader.u16(base + 4 + index * 2, "GSUB covered glyph")
                for index in range(count)
            )
        if format_number == 2:
            covered: list[int] = []
            expected_index = 0
            for index in range(count):
                start, end, start_index = self._reader.unpack(
                    ">HHH", base + 4 + index * 6, "GSUB coverage range"
                )
                if start > end or start_index != expected_index:
                    raise SfntFormatError("invalid GSUB coverage range")
                covered.extend(range(start, end + 1))
                expected_index += end - start + 1
            return tuple(covered)
        raise SfntFormatError(f"unsupported GSUB coverage format {format_number}")

    def _single_substitution(self, base: int) -> Mapping[int, int]:
        format_number, coverage_relative = self._reader.unpack(
            ">HH", base, "GSUB SingleSubst header"
        )
        coverage = self._coverage(base + coverage_relative)
        if format_number == 1:
            delta = self._reader.i16(base + 4, "GSUB SingleSubst delta")
            return {glyph: (glyph + delta) & 0xFFFF for glyph in coverage}
        if format_number == 2:
            glyph_count = self._reader.u16(base + 4, "GSUB substitute count")
            if glyph_count != len(coverage):
                raise SfntFormatError(
                    "GSUB SingleSubst substitute/coverage counts differ"
                )
            return {
                glyph: self._reader.u16(
                    base + 6 + index * 2, "GSUB substitute glyph"
                )
                for index, glyph in enumerate(coverage)
            }
        raise SfntFormatError(f"unsupported GSUB SingleSubst format {format_number}")

    def _read_gsub(
        self,
    ) -> tuple[Mapping[str, tuple[int, ...]], tuple[Mapping[int, int], ...]]:
        table = self.tables.get("GSUB")
        if table is None:
            return {}, ()
        if table.length < 10:
            raise SfntFormatError("GSUB table is shorter than its header")
        major, _, _, feature_relative, lookup_relative = self._reader.unpack(
            ">HHHHH", table.offset, "GSUB header"
        )
        if major != 1:
            raise SfntFormatError(f"unsupported GSUB major version {major}")
        feature_base = table.offset + feature_relative
        feature_count = self._reader.u16(feature_base, "GSUB feature count")
        features: dict[str, list[int]] = {}
        for index in range(feature_count):
            record = feature_base + 2 + index * 6
            self._reader.require(record, 6, "GSUB feature record")
            tag = self.data[record : record + 4].decode("ascii", errors="replace")
            relative = self._reader.u16(record + 4, "GSUB feature offset")
            feature = feature_base + relative
            lookup_count = self._reader.u16(feature + 2, "GSUB feature lookup count")
            indices = [
                self._reader.u16(feature + 4 + item * 2, "GSUB lookup index")
                for item in range(lookup_count)
            ]
            features.setdefault(tag, []).extend(indices)

        lookup_base = table.offset + lookup_relative
        lookup_count = self._reader.u16(lookup_base, "GSUB lookup count")
        lookup_offsets = [
            self._reader.u16(lookup_base + 2 + index * 2, "GSUB lookup offset")
            for index in range(lookup_count)
        ]
        lookups: list[Mapping[int, int]] = []
        for relative in lookup_offsets:
            lookup = lookup_base + relative
            lookup_type, _, subtable_count = self._reader.unpack(
                ">HHH", lookup, "GSUB lookup header"
            )
            substitutions: dict[int, int] = {}
            for index in range(subtable_count):
                subtable_relative = self._reader.u16(
                    lookup + 6 + index * 2, "GSUB subtable offset"
                )
                subtable = lookup + subtable_relative
                effective_type = lookup_type
                effective = subtable
                if lookup_type == 7:
                    extension_format, effective_type, extension_relative = (
                        self._reader.unpack(">HHI", subtable, "GSUB extension")
                    )
                    if extension_format != 1:
                        raise SfntFormatError(
                            f"unsupported GSUB extension format {extension_format}"
                        )
                    effective = subtable + extension_relative
                if effective_type == 1:
                    substitutions.update(self._single_substitution(effective))
            lookups.append(substitutions)
        for tag, indices in features.items():
            for index in indices:
                if index >= len(lookups):
                    raise SfntFormatError(
                        f"GSUB feature {tag!r} references lookup {index}"
                    )
        return {tag: tuple(indices) for tag, indices in features.items()}, tuple(lookups)

    def substitute_glyph(self, glyph: int, feature: str) -> int:
        result = glyph
        for lookup_index in self.gsub_features.get(feature, ()):
            result = self.gsub_lookups[lookup_index].get(result, result)
        return result

    def feature_substitutions(self, feature: str) -> Mapping[int, int]:
        candidates: set[int] = set()
        for lookup_index in self.gsub_features.get(feature, ()):
            candidates.update(self.gsub_lookups[lookup_index])
        return {
            glyph: substituted
            for glyph in sorted(candidates)
            if (substituted := self.substitute_glyph(glyph, feature)) != glyph
        }

    @property
    def vertical_substitutions(self) -> Mapping[int, int]:
        feature = "vrt2" if "vrt2" in self.gsub_features else "vert"
        return self.feature_substitutions(feature)

    def vertical_glyph(self, glyph: int) -> int:
        # OpenType specifies vrt2 as the preferred, comprehensive vertical
        # feature; vert is used when vrt2 is absent.
        feature = "vrt2" if "vrt2" in self.gsub_features else "vert"
        return self.substitute_glyph(glyph, feature)

    @property
    def table_tags(self) -> tuple[str, ...]:
        return tuple(sorted(self.tables))
