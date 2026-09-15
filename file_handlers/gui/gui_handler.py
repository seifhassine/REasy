from __future__ import annotations

from file_handlers.base_handler import BaseFileHandler
from utils.resource_file_utils import resolve_handler_resource_data

from .gui_file import GuiFile


class GuiHandler(BaseFileHandler):
    """REasy file-handler integration for supported GUIR resources."""

    def __init__(self) -> None:
        super().__init__()
        self.gui_file: GuiFile | None = None
        self.viewer = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return GuiFile.can_handle(data)

    def read(self, data: bytes) -> None:
        self.gui_file = GuiFile.from_bytes(data, self.filepath or "<bytes>")
        self.modified = False

    def rebuild(self) -> bytes:
        if self.gui_file is None:
            raise ValueError("no GUIR file is loaded")
        result = self.gui_file.write()
        self.modified = False
        if self.viewer is not None:
            self.viewer.on_saved()
        return result

    def resolve_resource(self, resource_path: str) -> tuple[str, bytes] | None:
        return resolve_handler_resource_data(
            self,
            resource_path,
            allow_selection_dialog=False,
        )

    def create_viewer(self):
        if self.gui_file is None:
            return None
        from .editor import GuiEditor

        self.viewer = GuiEditor(self)
        self.viewer.modified_changed.connect(self.modified_changed.emit)
        self.viewer.modified_changed.connect(
            lambda value: setattr(self, "modified", bool(value))
        )
        return self.viewer
