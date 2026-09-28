from __future__ import annotations

import math
import struct

from .model import IftData, IftEntry, IftGroup, IftSubRecord
from .profiles import IFT_MAGIC, IFT_PROFILES, IftFormatError, ift_profile

_ENTRY = "<QIIff"


def _finite(value: float, label: str) -> None:
    if not math.isfinite(value):
        raise IftFormatError(f"{label} must be finite, got {value!r}")


def _positive(value: float, label: str) -> None:
    _finite(value, label)
    if value <= 0.0:
        raise IftFormatError(f"{label} must be positive, got {value!r}")


def _check(data: bytes, at: int, size: int, label: str) -> None:
    if at < 0 or at + size > len(data):
        raise IftFormatError(f"{label} ends at 0x{at + size:X}, beyond 0x{len(data):X}")


def _utf16z(data: bytes, offset: int, label: str) -> str:
    if offset < 0 or offset & 1 or offset + 2 > len(data):
        raise IftFormatError(f"{label} offset 0x{offset:X} is outside or misaligned")
    end = offset
    while data[end : end + 2] != b"\0\0":
        end += 2
        if end + 1 >= len(data):
            raise IftFormatError(f"{label} at 0x{offset:X} is not null terminated")
    try:
        return data[offset:end].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise IftFormatError(f"{label} is not valid UTF-16LE") from exc


def _read_entry(data: bytes, at: int, label: str) -> IftEntry:
    name_at, seq, pat, width, height = struct.unpack_from(_ENTRY, data, at)
    _positive(width, f"{label} width")
    _positive(height, f"{label} height")
    return IftEntry(
        _utf16z(data, name_at, f"{label} name"), seq, pat, width, height, name_at, at
    )


def _read_entries(data: bytes, at: int, count: int, label: str) -> list[IftEntry]:
    _check(data, at, count * 0x18, f"{label} table")
    return [_read_entry(data, at + i * 0x18, f"{label} {i}") for i in range(count)]


def _read_glyph_table(data: bytes, at: int, label: str) -> tuple[list[IftEntry], int]:
    _check(data, at, 0x10, f"{label} header")
    count, _, path_at = struct.unpack_from("<IIQ", data, at)
    return _read_entries(data, at + 0x10, count, f"{label} entry"), path_at


def _read_groups(data: bytes, at: int, count: int) -> list[IftGroup]:
    _check(data, at, count * 0x18, "IFT group table")
    groups: list[IftGroup] = []
    for i in range(count):
        name_at, sub_count, subs_at = struct.unpack_from("<3Q", data, at + i * 0x18)
        _check(data, subs_at, sub_count * 0x28, f"IFT group {i} sub records")
        subs: list[IftSubRecord] = []
        for j in range(sub_count):
            reserved, a_at, b_at, blob_at, width, blob_size = struct.unpack_from(
                "<4Q2I", data, subs_at + j * 0x28
            )
            _check(data, blob_at, blob_size, f"IFT group {i} sub {j} blob")
            subs.append(
                IftSubRecord(
                    _utf16z(data, a_at, f"IFT group {i} sub {j} name A"),
                    _utf16z(data, b_at, f"IFT group {i} sub {j} name B"),
                    bytes(data[blob_at : blob_at + blob_size]),
                    width,
                    reserved,
                )
            )
        groups.append(IftGroup(_utf16z(data, name_at, f"IFT group {i} name"), subs))
    return groups


class _Strings:
    def __init__(self, base: int) -> None:
        self.base = base
        self.data = bytearray()
        self._seen: dict[str, int] = {}

    def intern(self, text: str) -> int:
        if text not in self._seen:
            self._seen[text] = self.base + len(self.data)
            self.data.extend(text.encode("utf-16-le") + b"\0\0")
        return self._seen[text]


class IftCodec:
    def __init__(self, version: int) -> None:
        self.profile = ift_profile(version)

    def read(self, data: bytes) -> IftData:
        v, header = self.profile.version, self.profile.header_size
        _check(data, 0, header, f"IFT v{v} header")
        version, magic = struct.unpack_from("<I4s", data, 0)
        if (version, magic) != (v, IFT_MAGIC):
            raise IftFormatError(
                f"invalid IFT v{v} header: version={version}, magic={magic!r}"
            )
        groups: list[IftGroup] = []
        if v == 1:
            descent, font_size, count, reserved, path_at = struct.unpack_from(
                "<ffIIQ", data, 8
            )
            extras = (0.0, 1.0)
            main = (_read_entries(data, header, count, "IFT entry"), path_at)
            aux: tuple[list[IftEntry], int] = ([], 0)
        else:
            f0, f1, descent, font_size = struct.unpack_from("<4f", data, 8)
            extras = (f0, f1)
            if v == 3:
                count, reserved, path_at = struct.unpack_from("<IIQ", data, 0x18)
                main = (_read_entries(data, header, count, "IFT entry"), path_at)
                aux = ([], 0)
            else:
                reserved = 0
                main, aux = (
                    _read_glyph_table(data, at, f"IFT table {i + 1}")
                    for i, at in enumerate(struct.unpack_from("<2Q", data, 0x18))
                )
                if v == 7:
                    count, reserved, groups_at = struct.unpack_from("<IIQ", data, 0x28)
                    groups = _read_groups(data, groups_at, count)
        _positive(font_size, "IFT font size")
        _finite(descent, "IFT descent")
        entries, path_at = main
        aux_entries, aux_path_at = aux
        return IftData(
            version=v,
            descent=descent,
            font_size=font_size,
            reserved=reserved,
            uv_sequence_path=_utf16z(data, path_at, "IFT UVS path"),
            entries=entries,
            extra_floats=extras,
            aux_path=(
                _utf16z(data, aux_path_at, "IFT aux path")
                if aux_entries or aux_path_at
                else ""
            ),
            aux_entries=aux_entries,
            groups=groups,
        )

    def write(self, model: IftData) -> bytes:
        v, header = self.profile.version, self.profile.header_size
        if model.version != v:
            raise IftFormatError(
                f"IFT v{v} codec cannot write embedded version {model.version}"
            )
        _positive(model.font_size, "IFT font size")
        _finite(model.descent, "IFT descent")
        if not model.uv_sequence_path:
            raise IftFormatError("IFT UVS path cannot be empty")
        e1, e2 = model.entries, model.aux_entries
        two_tables = v in (4, 7)
        if not two_tables and (e2 or model.groups or model.aux_path):
            raise IftFormatError(f"IFT v{v} has no aux table or groups")
        if v == 4 and model.groups:
            raise IftFormatError(f"IFT v{v} has no groups")
        for index, entry in enumerate(model.all_entries):
            if not entry.name:
                raise IftFormatError(f"IFT entry {index} name cannot be empty")
            _positive(entry.width, f"IFT entry {index} width")
            _positive(entry.height, f"IFT entry {index} height")

        t2 = header + 0x10 + 0x18 * len(e1)
        cursor = t2 + 0x10 + 0x18 * len(e2) if two_tables else header + 0x18 * len(e1)
        groups_at = 0
        if v == 7 and model.groups:
            groups_at = cursor
            cursor += 0x18 * len(model.groups)
        sub_ats: list[int] = []
        for group in model.groups:
            sub_ats.append(cursor if group.subs else 0)
            cursor += 0x28 * len(group.subs)
        flat_subs = [s for g in model.groups for s in g.subs]
        blob_ats: list[int] = []
        for sub in flat_subs:
            blob_ats.append(cursor)
            cursor += len(sub.blob)

        strings = _Strings(cursor)
        names1 = [strings.intern(e.name) for e in e1]
        path1 = strings.intern(model.uv_sequence_path)
        names2 = [strings.intern(e.name) for e in e2]
        path2 = strings.intern(model.aux_path) if two_tables else 0
        group_names = [strings.intern(g.name) for g in model.groups]
        sub_a = [strings.intern(s.name_a) for s in flat_subs]
        sub_b = [strings.intern(s.name_b) for s in flat_subs]

        out = bytearray(cursor)
        out += strings.data
        f0, f1 = model.extra_floats
        if v == 1:
            struct.pack_into(
                "<I4sffIIQ", out, 0, v, IFT_MAGIC, model.descent, model.font_size,
                len(e1), model.reserved, path1,
            )
        else:
            struct.pack_into("<I4s4f", out, 0, v, IFT_MAGIC, f0, f1, model.descent, model.font_size)
            if v == 3:
                struct.pack_into("<IIQ", out, 0x18, len(e1), model.reserved, path1)
            else:
                struct.pack_into("<2Q", out, 0x18, header, t2)
                struct.pack_into("<IIQ", out, header, len(e1), 0, path1)
                struct.pack_into("<IIQ", out, t2, len(e2), 0, path2)
                if v == 7:
                    struct.pack_into(
                        "<IIQ", out, 0x28, len(model.groups), model.reserved, groups_at
                    )
        if two_tables:
            e1_at, e2_at = header + 0x10, t2 + 0x10
        else:
            e1_at, e2_at = header, 0
        for entries, name_ats, at in ((e1, names1, e1_at), (e2, names2, e2_at)):
            for i, (entry, name_at) in enumerate(zip(entries, name_ats, strict=True)):
                struct.pack_into(_ENTRY, out, at + i * 0x18, name_at,
                                 entry.uv_sequence_no, entry.uv_pattern_no, entry.width, entry.height)
        k = 0
        for gi, group in enumerate(model.groups):
            struct.pack_into(
                "<3Q", out, groups_at + gi * 0x18, group_names[gi], len(group.subs), sub_ats[gi]
            )
            for j, sub in enumerate(group.subs):
                struct.pack_into(
                    "<4Q2I", out, sub_ats[gi] + j * 0x28, sub.reserved, sub_a[k], sub_b[k],
                    blob_ats[k], sub.width, len(sub.blob),
                )
                out[blob_ats[k] : blob_ats[k] + len(sub.blob)] = sub.blob
                k += 1
        return bytes(out)


IFT_CODECS: dict[int, IftCodec] = {v: IftCodec(v) for v in IFT_PROFILES}


def ift_codec(version: int) -> IftCodec:
    try:
        return IFT_CODECS[int(version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(IFT_CODECS)))
        raise IftFormatError(
            f"unsupported IFT version {version}; supported versions: {supported}"
        ) from exc


def decode_ift(data: bytes) -> IftData:
    if len(data) < 8:
        raise IftFormatError("file is too small for an IFT header")
    version, magic = struct.unpack_from("<I4s", data, 0)
    if magic != IFT_MAGIC:
        raise IftFormatError(f"expected IFNT magic, got {magic!r}")
    return ift_codec(version).read(data)


def encode_ift(model: IftData) -> bytes:
    return ift_codec(model.version).write(model)
