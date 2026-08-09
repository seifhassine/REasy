from __future__ import annotations

from dataclasses import dataclass


IFT_MAGIC = b"IFNT"


class IftFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IftProfile:
    version: int
    magic: bytes
    header_size: int
    entry_size: int


IFT_PROFILES: dict[int, IftProfile] = {
    1: IftProfile(1, IFT_MAGIC, 0x20, 0x18),
}


def ift_profile(version: int) -> IftProfile:
    try:
        return IFT_PROFILES[int(version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(IFT_PROFILES)))
        raise IftFormatError(
            f"unsupported IFT version {version}; supported versions: {supported}"
        ) from exc
