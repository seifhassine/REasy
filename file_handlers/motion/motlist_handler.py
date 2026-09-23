from __future__ import annotations

from file_handlers.base_handler import BaseFileHandler
from PySide6.QtCore import Signal

from .errors import MotionWriteError
from .format_registry import require_motion_format
from .motlist_file import MotListFile


class MotListHandler(BaseFileHandler):
    """Application integration for registered semantic MOTLIST codecs."""
    document_changed = Signal()

    def __init__(self):
        super().__init__()
        self.motlist_file: MotListFile | None = None
        self.raw_data: bytes | bytearray = b""

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return MotListFile.can_handle(data)

    def supports_editing(self) -> bool:
        from .wilds_codec import WILDS_MOTION_FORMAT_CODEC
        return self.motlist_file is not None and self.motlist_file.codec is not WILDS_MOTION_FORMAT_CODEC

    def read(self, data: bytes) -> None:
        facade = MotListFile(require_motion_format(data))
        facade.read(data, label=self.filepath or "MOTLIST")
        self.adopt_document(facade, data)

    def adopt_document(self, facade, data, *, notify=True):
        """Install an already parsed document on the handler's UI thread."""
        self.motlist_file = facade
        self.raw_data = data
        self.modified = False
        if notify:
            self.document_changed.emit()

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
        self.document_changed.emit()
        return result

    def create_viewer(self):
        from .wilds_codec import WILDS_MOTION_FORMAT_CODEC
        if self.motlist_file.codec is WILDS_MOTION_FORMAT_CODEC:
            from .preview.wilds_preview import WildsPreview
            return WildsPreview(self)
        from .mhr_storage import MhrMotList
        if isinstance(self.model, MhrMotList):
            from .preview.mhr_editor import MhrMotListEditor
            viewer = MhrMotListEditor(self)
            viewer.modified_changed.connect(self.modified_changed.emit)
            return viewer
        from .preview.widget import MotListPreviewWidget

        viewer = MotListPreviewWidget(self)
        viewer.modified_changed.connect(self.modified_changed.emit)
        return viewer
