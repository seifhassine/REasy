"""See docs/internals/bhvt-format.md."""

from __future__ import annotations

import struct
from dataclasses import dataclass

lazy from file_handlers.rsz.rsz_file import RszFile


@dataclass(frozen=True, slots=True)
class RszBlocks:
    type_registry: object
    game_version: str
    filepath: str = ""

    def read(self, data: bytes) -> RszFile:
        rsz = RszFile()
        rsz.type_registry, rsz.game_version = self.type_registry, self.game_version
        rsz.read_headless(data, validate_type_registry=True)
        rsz.filepath = self.filepath
        return rsz

    def blank(self, like: RszFile) -> RszFile:
        header = like.rsz_header
        return self.read(struct.pack("<6I3Q16x", header.magic, header.version, 0, 1, 0, 0, 48, 64, 64))

    @staticmethod
    def write(rsz: RszFile) -> bytes:
        return rsz.build_headless()
