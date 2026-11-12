"""See docs/internals/bhvt-editor.md, Raw view."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

lazy from file_handlers.clip.document import ClipDocument
lazy from file_handlers.clip.editor import ClipEditor
lazy from file_handlers.uvar.uvar_handler import UvarHandler
lazy from file_handlers.wcc.wcc_handler import WccHandler


class EmbeddedViewers(QObject):
    edited = Signal(bool)

    def __init__(self, host, parent: QObject | None = None):
        super().__init__(parent)
        self.host = host
        self._parts: dict[int, tuple[str, QObject, QWidget]] = {}

    def viewer(self, kind: str, model: object) -> QWidget:
        if id(model) not in self._parts:
            self._parts[id(model)] = (kind, *{"rsz": self._rsz, "uvar": self._uvar,
                                              "timeline": self._timeline}[kind](model))
        return self._parts[id(model)][2]

    def forget(self, model: object | None = None) -> None:
        for key in [key for key, part in self._parts.items() if part[0] == "rsz"] if model is None else [id(model)]:
            if key in self._parts:
                _kind, part, viewer = self._parts.pop(key)
                viewer.hide()
                viewer.deleteLater()
                part.deleteLater()

    def _rsz(self, block) -> tuple[QObject, QWidget]:
        for user in block.rsz_userdata_infos if block.has_embedded_rsz else ():
            vars(user).pop("reference_count", None)
        handler = WccHandler()
        handler.setParent(self)
        handler.app, handler.filepath, handler.type_registry = self.host.app, self.host.filepath, block.type_registry
        handler.set_game_version(block.game_version)
        handler.attach(block)
        handler.modified_changed.connect(self._handler_edited)
        return handler, handler.create_viewer()

    def _uvar(self, uvar) -> tuple[QObject, QWidget]:
        handler = UvarHandler()
        handler.setParent(self)
        handler.app, handler.uvar_file = self.host.app, uvar
        handler.modified_changed.connect(self._handler_edited)
        return handler, handler.create_viewer()

    def _timeline(self, timeline) -> tuple[QObject, QWidget]:
        document = ClipDocument(timeline, self)
        document.changed.connect(self._timeline_edited)
        return document, ClipEditor(document)

    def _handler_edited(self, modified: bool) -> None:
        if modified:
            self.sender().modified = False
            self.edited.emit(True)

    def _timeline_edited(self) -> None:
        self.edited.emit(False)
