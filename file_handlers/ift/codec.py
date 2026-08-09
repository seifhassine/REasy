from __future__ import annotations

import math
import struct
from typing import Protocol

from .model import IftData, IftEntry
from .profiles import IFT_MAGIC, IftFormatError, IftProfile, ift_profile


class IftCodec(Protocol):
    profile: IftProfile

    def read(self, data: bytes) -> IftData: ...

    def write(self, model: IftData) -> bytes: ...


def _finite_positive(value: float, label: str) -> None:
    if not math.isfinite(value) or value <= 0.0:
        raise IftFormatError(f"{label} must be a finite positive float, got {value!r}")


def _read_utf16z(data: bytes, offset: int, label: str) -> str:
    if offset < 0 or offset >= len(data) or offset & 1:
        raise IftFormatError(
            f"{label} offset 0x{offset:X} is outside or not two-byte aligned"
        )
    end = offset
    while end + 1 < len(data):
        if data[end : end + 2] == b"\0\0":
            try:
                return data[offset:end].decode("utf-16-le")
            except UnicodeDecodeError as exc:
                raise IftFormatError(f"{label} is not valid UTF-16LE") from exc
        end += 2
    raise IftFormatError(f"{label} at 0x{offset:X} is not null terminated")


class IftV1Codec:
    profile = ift_profile(1)

    def read(self, data: bytes) -> IftData:
        if len(data) < self.profile.header_size:
            raise IftFormatError("file is too small for an IFT v1 header")
        (
            version,
            magic,
            descent,
            font_size,
            entry_count,
            reserved,
            uv_path_offset,
        ) = struct.unpack_from("<I4sffIIQ", data, 0)
        if version != self.profile.version or magic != self.profile.magic:
            raise IftFormatError(
                f"invalid IFT v1 header: version={version}, magic={magic!r}"
            )
        table_end = self.profile.header_size + entry_count * self.profile.entry_size
        if table_end > len(data):
            raise IftFormatError(
                f"IFT entry table ends at 0x{table_end:X}, beyond 0x{len(data):X}"
            )
        _finite_positive(font_size, "IFT font size")
        if not math.isfinite(descent):
            raise IftFormatError("IFT descent must be finite")

        entries: list[IftEntry] = []
        for index in range(entry_count):
            source_offset = self.profile.header_size + index * self.profile.entry_size
            name_offset, sequence_no, pattern_no, width, height = struct.unpack_from(
                "<QIIff", data, source_offset
            )
            if name_offset < table_end:
                raise IftFormatError(
                    f"IFT entry {index} name offset 0x{name_offset:X} overlaps the table"
                )
            _finite_positive(width, f"IFT entry {index} width")
            _finite_positive(height, f"IFT entry {index} height")
            entries.append(
                IftEntry(
                    name=_read_utf16z(data, name_offset, f"IFT entry {index} name"),
                    uv_sequence_no=sequence_no,
                    uv_pattern_no=pattern_no,
                    width=width,
                    height=height,
                    name_offset=name_offset,
                    source_offset=source_offset,
                )
            )

        if uv_path_offset < table_end:
            raise IftFormatError(
                f"IFT UVS path offset 0x{uv_path_offset:X} overlaps the entry table"
            )
        return IftData(
            version=version,
            descent=descent,
            font_size=font_size,
            reserved=reserved,
            uv_sequence_path=_read_utf16z(data, uv_path_offset, "IFT UVS path"),
            entries=entries,
            uv_sequence_path_offset=uv_path_offset,
        )

    def write(self, model: IftData) -> bytes:
        if model.version != self.profile.version:
            raise IftFormatError(
                f"IFT v1 codec cannot write embedded version {model.version}"
            )
        _finite_positive(model.font_size, "IFT font size")
        if not math.isfinite(model.descent):
            raise IftFormatError("IFT descent must be finite")
        if not model.uv_sequence_path:
            raise IftFormatError("IFT UVS path cannot be empty")

        table_end = self.profile.header_size + len(model.entries) * self.profile.entry_size
        output = bytearray(table_end)
        name_offsets: list[int] = []
        for index, entry in enumerate(model.entries):
            if not entry.name:
                raise IftFormatError(f"IFT entry {index} name cannot be empty")
            _finite_positive(entry.width, f"IFT entry {index} width")
            _finite_positive(entry.height, f"IFT entry {index} height")
            name_offsets.append(len(output))
            output.extend(entry.name.encode("utf-16-le") + b"\0\0")

        uv_path_offset = len(output)
        output.extend(model.uv_sequence_path.encode("utf-16-le") + b"\0\0")
        struct.pack_into(
            "<I4sffIIQ",
            output,
            0,
            model.version,
            self.profile.magic,
            model.descent,
            model.font_size,
            len(model.entries),
            model.reserved,
            uv_path_offset,
        )
        for index, (entry, name_offset) in enumerate(
            zip(model.entries, name_offsets, strict=True)
        ):
            struct.pack_into(
                "<QIIff",
                output,
                self.profile.header_size + index * self.profile.entry_size,
                name_offset,
                entry.uv_sequence_no,
                entry.uv_pattern_no,
                entry.width,
                entry.height,
            )
        return bytes(output)


IFT_CODECS: dict[int, IftCodec] = {1: IftV1Codec()}


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
