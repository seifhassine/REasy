from __future__ import annotations

from dataclasses import dataclass, field

from file_handlers.font.sfnt import SfntFont

from .codec import decode_oft, encode_oft
from .profiles import OFT_PROFILES


@dataclass
class OftFile:
    version: int = 1
    sfnt_data: bytes = b""
    raw_data: bytes = field(default=b"", repr=False, compare=False)

    @classmethod
    def from_bytes(cls, data: bytes, *, version: int = 1) -> "OftFile":
        result = cls()
        result.read(data, version=version)
        return result

    @staticmethod
    def can_handle(data: bytes) -> bool:
        return any(
            len(data) >= len(profile.magic)
            and data[: len(profile.magic)] == profile.magic
            for profile in OFT_PROFILES.values()
        )

    def read(self, data: bytes, *, version: int = 1) -> bool:
        self.version = int(version)
        self.raw_data = bytes(data)
        self.sfnt_data = decode_oft(self.raw_data, version=self.version)
        return True

    def font(self, *, face_index: int = 0) -> SfntFont:
        return SfntFont(self.sfnt_data, face_index=face_index)

    def write(self) -> bytes:
        return encode_oft(self.sfnt_data, version=self.version)
