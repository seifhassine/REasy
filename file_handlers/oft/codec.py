from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .profiles import SFNT_MAGICS, OftFormatError, OftProfile, oft_profile


_U64_MASK = (1 << 64) - 1


def _rotate_right_u64(value: int, count: int) -> int:
    count &= 63
    value &= _U64_MASK
    if not count:
        return value
    return ((value >> count) | (value << (64 - count))) & _U64_MASK


def is_sfnt(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] in SFNT_MAGICS


class OftCodec(Protocol):
    profile: OftProfile

    def decode(self, data: bytes, *, validate_sfnt: bool = True) -> bytes: ...

    def encode(self, sfnt: bytes, *, validate_sfnt: bool = True) -> bytes: ...


@dataclass(frozen=True, slots=True)
class XorOftCodec:
    """The rotated-key OFT wrapper used by DMC5 version 1."""

    profile: OftProfile

    def key(self, payload_size: int) -> bytes:
        if payload_size < 0:
            raise ValueError("payload size cannot be negative")
        value = _rotate_right_u64(self.profile.xor_seed, payload_size & 63)
        return value.to_bytes(8, "little")

    def crypt(self, payload: bytes) -> bytes:
        key = self.key(len(payload))
        return bytes(value ^ key[index & 7] for index, value in enumerate(payload))

    def decode(self, data: bytes, *, validate_sfnt: bool = True) -> bytes:
        magic = self.profile.magic
        if len(data) < len(magic) or data[: len(magic)] != magic:
            raise OftFormatError(f"expected {magic!r} OFT wrapper magic")
        decoded = self.crypt(data[len(magic) :])
        if validate_sfnt and not is_sfnt(decoded):
            raise OftFormatError("decoded OFT payload does not have a known SFNT magic")
        return decoded

    def encode(self, sfnt: bytes, *, validate_sfnt: bool = True) -> bytes:
        if validate_sfnt and not is_sfnt(sfnt):
            raise OftFormatError("input does not have a known SFNT magic")
        return self.profile.magic + self.crypt(sfnt)


OFT_CODECS: dict[int, OftCodec] = {1: XorOftCodec(oft_profile(1))}


def oft_codec(version: int) -> OftCodec:
    try:
        return OFT_CODECS[int(version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(OFT_CODECS)))
        raise OftFormatError(
            f"unsupported OFT version {version}; supported versions: {supported}"
        ) from exc


def _xor_codec(version: int) -> XorOftCodec:
    codec = oft_codec(version)
    if not isinstance(codec, XorOftCodec):
        raise OftFormatError(f"OFT version {version} does not use the XOR wrapper")
    return codec


def font_xor_key(payload_size: int, *, version: int = 1) -> bytes:
    return _xor_codec(version).key(payload_size)


def crypt_font_payload(payload: bytes, *, version: int = 1) -> bytes:
    return _xor_codec(version).crypt(payload)


def decode_oft(
    data: bytes,
    *,
    version: int = 1,
    validate_sfnt: bool = True,
) -> bytes:
    return oft_codec(version).decode(data, validate_sfnt=validate_sfnt)


def encode_oft(
    sfnt: bytes,
    *,
    version: int = 1,
    validate_sfnt: bool = True,
) -> bytes:
    return oft_codec(version).encode(sfnt, validate_sfnt=validate_sfnt)
