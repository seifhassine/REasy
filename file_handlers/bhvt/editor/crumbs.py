"""See docs/internals/bhvt-editor.md, Workspace."""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget

lazy from ui.breadcrumbs import breadcrumb_separator
lazy from ui.editor_widgets import styled_label, tool_button
lazy from ui.styles import set_style_role

from ui.search_popup import below


class Crumbs(QWidget):
    def __init__(self, state, popup, parent: QWidget | None = None):
        super().__init__(parent)
        self.state, self.popup = state, popup
        self._shown: dict[QWidget, object] = {}
        self._signature = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        set_style_role(self, surface="header")
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(6, 2, 6, 2)
        self._row.setSpacing(0)
        self._row.addStretch(1)
        state.scope_changed.connect(self.sync)
        state.changed.connect(self.sync)
        self.sync()

    def sync(self) -> None:
        state = self.state
        path = state.structure.path(state.scope)
        graph = state.title(state.model) if state.layered else None
        signature = (graph, [(id(node), node.name) for node in path])
        if signature == self._signature:
            return
        for widget in self._shown:
            widget.hide()
            widget.deleteLater()
        self._signature, self._shown = signature, {}
        if graph is not None:
            self._add(tool_button("layers", self.tr("Choose the graph"), self._pick_graph, text=graph, parent=self))
            self._add(breadcrumb_separator(self))
        for node in path[:-1]:
            button = QToolButton(self)
            button.setObjectName("breadcrumbButton")
            button.setAutoRaise(True)
            button.setText(node.name)
            button.clicked.connect(self._open)
            self._add(button, node)
            self._add(breadcrumb_separator(self))
        self._add(styled_label(path[-1].name, self, heading="panel"))

    def _add(self, widget: QWidget, node=None) -> None:
        self._shown[widget] = node
        self._row.insertWidget(self._row.count() - 1, widget)

    def _open(self) -> None:
        self.state.show(self._shown[self.sender()])

    def _pick_graph(self) -> None:
        state = self.state
        self.popup.pick([(state.title(model), "", model) for model in state.graphs], below(self.sender()),
                        self.tr("Search graphs"), partial(state.show, None))
