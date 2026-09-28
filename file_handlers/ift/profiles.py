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
    version: IftProfile(version, IFT_MAGIC, header_size, 0x18)
    for version, header_size in ((1, 0x20), (3, 0x28), (4, 0x28), (7, 0x38))
}


def ift_profile(version: int) -> IftProfile:
    try:
        return IFT_PROFILES[int(version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(IFT_PROFILES)))
        raise IftFormatError(
            f"unsupported IFT version {version}; supported versions: {supported}"
        ) from exc
