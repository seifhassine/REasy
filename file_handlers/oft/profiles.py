from __future__ import annotations

from dataclasses import dataclass


OFT_MAGIC = b"FBFO"
SFNT_MAGICS = frozenset((b"OTTO", b"\x00\x01\x00\x00", b"ttcf", b"true"))


class OftFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OftProfile:
    version: int
    magic: bytes
    xor_seed: int


OFT_PROFILES: dict[int, OftProfile] = {
    1: OftProfile(version=1, magic=OFT_MAGIC, xor_seed=0xAE6E39B58A355F45),
}


def oft_profile(version: int) -> OftProfile:
    try:
        return OFT_PROFILES[int(version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(OFT_PROFILES)))
        raise OftFormatError(
            f"unsupported OFT version {version}; supported versions: {supported}"
        ) from exc
