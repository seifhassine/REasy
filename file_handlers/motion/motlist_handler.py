from __future__ import annotations

from file_handlers.base_handler import BaseFileHandler

from .errors import MotionWriteError
from .format_registry import require_motion_format
from .motlist_file import MotListFile


class MotListHandler(BaseFileHandler):
    """Application integration for registered semantic MOTLIST codecs."""

    def __init__(self):
        super().__init__()
        self.motlist_file: MotListFile | None = None
        self.raw_data: bytes | bytearray = b""

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return MotListFile.can_handle(data)

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes) -> None:
        facade = MotListFile(require_motion_format(data))
        facade.read(data, label=self.filepath or "MOTLIST")
        self.motlist_file = facade
        self.raw_data = data
        self.modified = False

    @property
    def model(self):
        if self.motlist_file is None:
            raise MotionWriteError("no MOTLIST file is loaded")
        return self.motlist_file.model

    def rebuild(self) -> bytes:
        if self.motlist_file is None:
            raise MotionWriteError("no MOTLIST file is loaded")
        result = self.motlist_file.write()
        reparsed = MotListFile(self.motlist_file.codec)
        reparsed.read(result, label="rebuilt MOTLIST")
        if reparsed.write() != result:
            raise MotionWriteError("MOTLIST serialization is not stable after reparsing")
        self.motlist_file = reparsed
        self.raw_data = result
        self.modified = False
        return result

    def create_viewer(self):
        from .preview.widget import MotListPreviewWidget

        viewer = MotListPreviewWidget(self)
        viewer.modified_changed.connect(self.modified_changed.emit)
        return viewer
