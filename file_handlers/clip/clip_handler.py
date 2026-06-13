from __future__ import annotations

from typing import Optional

from file_handlers.base_handler import BaseFileHandler

from .clip_file import ClipFile
from .reader import ClipParserError


class ClipHandler(BaseFileHandler):
    """Application file handler for CLIP/TML/UCurve-family files."""

    def __init__(self):
        super().__init__()
        self.clip_file: Optional[ClipFile] = None
        self.raw_data: bytes | bytearray = b""
        self.filepath: str = ""

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return ClipFile.can_handle(data)

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes):
        self.raw_data = data
        clip_file = ClipFile()
        clip_file.read(data)
        self.clip_file = clip_file
        self.modified = False

    @property
    def parsed(self):
        if self.clip_file is None:
            raise ClipParserError("No CLIP file loaded")
        return self.clip_file.parsed

    def rebuild(self) -> bytes:
        result = self.validate_graph_rebuild(accept_reparse=True) if self.clip_file is not None else b""
        self.modified = False
        return result

    def validate_graph_rebuild(self, accept_reparse: bool = False) -> bytes:
        if self.clip_file is None:
            raise ClipParserError("No CLIP file loaded")
        result = self.clip_file.write()
        reparsed = ClipFile()
        reparsed.read(result)
        if accept_reparse:
            self.clip_file = reparsed
            self.raw_data = result
        return result

    def create_viewer(self):
        from .clip_viewer import ClipViewer

        viewer = ClipViewer(self)
        viewer.modified_changed.connect(self.modified_changed.emit)
        viewer.modified_changed.connect(lambda val: setattr(self, "modified", bool(val)))
        return viewer
