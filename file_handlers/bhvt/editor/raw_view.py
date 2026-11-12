"""See docs/internals/bhvt-editor.md, Raw view."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QSplitter, QStackedWidget, QWidget

lazy from file_handlers.rsz.rsz_data_types import _DataValue

from ..document import BhvtDocument
from ..rules import rules_of
from .embedded import EmbeddedViewers
from .raw_outline import Outline, page_key
from .raw_pages import RecordInspector, RecordTable, TablePage, VariableTable

_EMBEDDED = ("rsz", "uvar", "timeline")


class RawView(QSplitter):
    def __init__(self, document: BhvtDocument, host, parent: QWidget | None = None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.document = document
        self.embedded = EmbeddedViewers(host, self)
        self.outline = Outline(document, self)
        self.pages = QStackedWidget(self)
        self._transient: tuple[tuple, QWidget] | None = None
        self._stale, self._scroll = False, 0
        self.addWidget(self.outline)
        self.addWidget(self.pages)
        self.setStretchFactor(1, 1)
        self.setSizes([280, 900])
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self.sync)
        self._restore = QTimer(self)
        self._restore.setSingleShot(True)
        self._restore.timeout.connect(self._scrolled)
        self.outline.currentItemChanged.connect(self._refresh.start)
        document.changed.connect(self._changed)
        self.outline.setCurrentItem(self.outline.topLevelItem(0))

    def open(self, kind: str, model: object) -> None:
        self._rebuild()
        self.outline.select_page((kind, model))
        self.sync()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh.start()

    def _changed(self, change) -> None:
        if isinstance(change.target, _DataValue):
            self.embedded.forget()
        elif change.structural:
            self.embedded.forget(change.target)
        self._stale |= change.structural or change.field in ("name", "children", "selector")
        if self.isVisible():
            self._refresh.start()

    def _rebuild(self) -> None:
        if self._stale:
            self._stale = False
            self.outline.rebuild()

    def _scrolled(self) -> None:
        if self._transient is not None:
            self._transient[1].verticalScrollBar().setValue(self._scroll)

    def sync(self) -> None:
        self._rebuild()
        page = self.outline.current_page()
        if self._transient is not None:
            shown, widget = self._transient
            self._scroll = widget.verticalScrollBar().value() if page is not None and page_key(page) == shown else 0
            widget.hide()
            widget.deleteLater()
            self._transient = None
        if page is not None and page[0] in _EMBEDDED:
            widget = self.embedded.viewer(page[0], page[1])
        elif page is not None:
            widget = self._page(page)
            self._transient = (page_key(page), widget)
            self._restore.start()
        if page is not None:
            if self.pages.indexOf(widget) < 0:
                self.pages.addWidget(widget)
            self.pages.setCurrentWidget(widget)
        self._refresh.stop()

    def _page(self, page: tuple) -> QWidget:
        if page[0] == "node":
            return RecordInspector(self.document, page[2], page[1], rules_of(page[1]).node)
        if page[0] == "record":
            return RecordInspector(self.document, page[1])
        if page[0] == "variables":
            return VariableTable(self.document, page[1])
        return TablePage(RecordTable(self.document, *page[1:]))
