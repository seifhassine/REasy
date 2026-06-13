from __future__ import annotations

import struct

from .enums import CLIP_MAGIC
from .parser import ClipParser, ParsedClip
from .reader import ClipParserError
from .writer import ClipWriter


class ClipFile:
    def __init__(self):
        self._parsed: ParsedClip | None = None
        self._parser = ClipParser()
        self._writer = ClipWriter()

    @staticmethod
    def can_handle(data: bytes) -> bool:
        return len(data) >= 8 and struct.unpack_from("<I", data, 0)[0] == CLIP_MAGIC

    def read(self, data: bytes) -> bool:
        self._parsed = self._parser.parse(data)
        return True

    def write(self) -> bytes:
        if self._parsed is None:
            raise ClipParserError("No parsed CLIP data available")
        return self._writer.build(self._parsed)

    @property
    def parsed(self) -> ParsedClip:
        if self._parsed is None:
            raise ClipParserError("CLIP file not parsed yet")
        return self._parsed
