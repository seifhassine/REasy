"""See docs/internals/gui-editor.md."""

from __future__ import annotations

from file_handlers.base_handler import DocumentHandler

lazy from utils.resource_file_utils import (ResourceResolutionContext, listed_resource_paths,
                                            resolve_handler_resource_data, resource_context_for_handler)

lazy from .codec import gui_version
lazy from .document import GuiEditDocument
lazy from .editor import GuiEditor
lazy from .errors import GuiFormatError
lazy from .rules import gui_layout


class GuiHandler(DocumentHandler):
    def __init__(self):
        super().__init__()
        self.document: GuiEditDocument | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        try:
            gui_layout(gui_version(data))
        except GuiFormatError:
            return False
        return True

    def read(self, data: bytes) -> None:
        self.document = GuiEditDocument.read(data, self.filepath or "<bytes>", self)
        self.modified = False

    def resolve_resource(self, resource_path: str) -> tuple[str, bytes] | None:
        return resolve_handler_resource_data(self, resource_path, allow_selection_dialog=False)

    @property
    def files(self) -> ResourceResolutionContext:
        return resource_context_for_handler(self) or ResourceResolutionContext()

    def listed(self, extension: str) -> tuple[str, ...]:
        return listed_resource_paths(self, extension)

    def create_viewer(self):
        editor = GuiEditor(self.document, self, self.files.game)
        editor.modified_changed.connect(self._set_modified)
        return editor
