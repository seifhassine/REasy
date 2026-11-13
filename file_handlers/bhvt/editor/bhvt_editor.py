"""See docs/internals/bhvt-editor.md, Workspace."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QSplitter, QStackedWidget, QVBoxLayout, QWidget

lazy from ui.editor_widgets import ElidedLabel, editor_strip, tool_button, toolbar_separator
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role

from ..document import BhvtDocument
from .canvas import GraphCanvas
from .crumbs import Crumbs
from .inspector import Inspector
from .navigator import Navigator
from .pickers import SearchPopup, below, node_entries
from .raw_view import RawView
from .state import EditorState


class BhvtEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, document: BhvtDocument, host, parent: QWidget | None = None):
        super().__init__(parent)
        self.document, self.handler = document, host
        self.raw: RawView | None = None
        self.state = state = EditorState(document, self)
        self.popup = SearchPopup(self, state.attempt)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.navigator = Navigator(state, self.splitter)
        center = QWidget(self.splitter)
        column = QVBoxLayout(center)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.crumbs = Crumbs(state, self.popup, center)
        self.canvas = GraphCanvas(state, self.popup, center)
        column.addWidget(self.crumbs)
        column.addWidget(self.canvas, 1)
        self.inspector = Inspector(state, self.popup, self.splitter)
        for widget in (self.navigator, center, self.inspector):
            self.splitter.addWidget(widget)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([300, 700, 380])
        self.views = QStackedWidget(self)
        self.views.addWidget(self.splitter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar())
        layout.addWidget(self.views, 1)
        self._clear = QTimer(self)
        self._clear.setSingleShot(True)
        self._clear.setInterval(8000)
        self._clear.timeout.connect(self.status.clear)
        self._shortcuts()
        document.undo_stack.cleanChanged.connect(self._clean_changed)
        for signal in (state.changed, state.selection_changed, state.scope_changed):
            signal.connect(self._update)
        state.refused.connect(self._refused)
        self.canvas.link_requested.connect(self.inspector.add_transition)
        self.navigator.menu_requested.connect(self.canvas.show_menu)
        self.navigator.checked.connect(self._checked)
        for pane in (self.navigator, self.inspector):
            pane.raw_requested.connect(self._open_raw)
        self._update()

    def _toolbar(self) -> QWidget:
        strip, row = editor_strip()
        stack, canvas = self.document.undo_stack, self.canvas
        self.back = tool_button("arrow_left", self.tr("Back (Alt+Left)"), self._back)
        self.forward = tool_button("arrow_right", self.tr("Forward (Alt+Right)"), self._forward)
        self.undo = tool_button("undo", self.tr("Undo (Ctrl+Z)"), stack.undo)
        self.redo = tool_button("redo", self.tr("Redo (Ctrl+Y)"), stack.redo)
        self.add = tool_button("add", self.tr("Add a state, or a child of the selected node"), self._add,
                               text=self.tr("Node"))
        self.link = tool_button("link", self.tr("Add a transition from the selected state"),
                                self.inspector.add_transition, text=self.tr("Transition"))
        self.remove = tool_button("delete", self.tr("Delete (Del)"), canvas.delete)
        self.copy = tool_button("copy", self.tr("Copy (Ctrl+C)"), canvas.copy)
        self.paste = tool_button("paste", self.tr("Paste (Ctrl+V)"), canvas.paste)
        self.arrange = tool_button("grid", self.tr("Arrange"), canvas.arrange)
        self.fit = tool_button("fit", self.tr("Fit (F)"), canvas.fit)
        self.find = tool_button("search", self.tr("Search nodes"), self._search)
        self.status = ElidedLabel()
        set_style_role(self.status, tone="danger")
        self.issues = tool_button("error", self.tr("Errors: the file cannot be saved until they are fixed"),
                                  self._show_problems, text="0")
        self.issues.setIcon(line_icon("error", current_colors()["danger"]))
        self.issues.hide()
        self.raw_button = tool_button("code", self.tr("Raw data: every section, table and embedded file"),
                                      text=self.tr("Raw"))
        self.raw_button.setCheckable(True)
        self.raw_button.toggled.connect(self._show_raw)
        for widget in (self.back, self.forward, toolbar_separator(), self.undo, self.redo, toolbar_separator(),
                       self.add, self.link, self.remove, toolbar_separator(), self.copy, self.paste,
                       toolbar_separator(), self.arrange, self.fit, self.find):
            row.addWidget(widget)
        row.addWidget(self.status, 1)
        row.addWidget(self.issues)
        row.addWidget(self.raw_button)
        self.undo.setEnabled(stack.canUndo())
        self.redo.setEnabled(stack.canRedo())
        stack.canUndoChanged.connect(self.undo.setEnabled)
        stack.canRedoChanged.connect(self.redo.setEnabled)
        return strip

    def _shortcuts(self) -> None:
        stack, canvas = self.document.undo_stack, self.canvas
        for keys, slot in ((QKeySequence.StandardKey.Undo, stack.undo), (QKeySequence.StandardKey.Redo, stack.redo),
                           (QKeySequence.StandardKey.Copy, canvas.copy), (QKeySequence.StandardKey.Paste, canvas.paste),
                           ("Ctrl+D", canvas.duplicate), ("Alt+Left", self._back), ("Alt+Right", self._forward)):
            QShortcut(keys, self.splitter, slot).setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence.StandardKey.Delete, self.navigator.tree, canvas.delete).setContext(
            Qt.ShortcutContext.WidgetShortcut)

    def _update(self) -> None:
        state, shown = self.state, self.views.currentIndex() == 0
        structure, nodes = state.structure, state.nodes()
        self.back.setEnabled(shown and state.can_step(-1))
        self.forward.setEnabled(shown and state.can_step(1))
        self.link.setEnabled(shown and bool(nodes) and structure.is_state(nodes[-1]))
        self.remove.setEnabled(shown and (bool(state.transitions()) or
                                          any(node is not structure.root for node in nodes)))
        self.copy.setEnabled(shown and bool(nodes))
        for button in (self.add, self.paste, self.arrange, self.fit, self.find):
            button.setEnabled(shown)

    def _add(self) -> None:
        self.canvas.add_child()

    def _back(self) -> None:
        self.state.step(-1)

    def _forward(self) -> None:
        self.state.step(1)

    def _search(self) -> None:
        structure = self.state.structure
        self.popup.pick(node_entries(structure, structure.nodes), below(self.find), self.tr("Search nodes"),
                        self.state.reveal)

    def _show_raw(self, on: bool) -> None:
        if self.raw is None:
            self.raw = RawView(self.document, self.handler, self.views)
            self.raw.embedded.edited.connect(self._raw_edited)
            self.views.addWidget(self.raw)
        self.views.setCurrentIndex(int(on))
        if not on:
            self.state.refresh()
        self._update()

    def _open_raw(self, kind: str, model: object) -> None:
        self.raw_button.setChecked(True)
        self.raw.open(kind, model)

    def _raw_edited(self, direct: bool) -> None:
        self.document.embedded_modified = True
        if direct:
            self.document.undo_stack.clear()
        self.modified_changed.emit(True)

    def _clean_changed(self, clean: bool) -> None:
        self.modified_changed.emit(not clean or self.document.embedded_modified)

    def _refused(self, text: str) -> None:
        self.status.setText(text)
        self._clear.start()

    def _checked(self, errors: int) -> None:
        self.issues.setText(str(errors))
        self.issues.setVisible(errors > 0)

    def _show_problems(self) -> None:
        self.raw_button.setChecked(False)
        self.navigator.setCurrentIndex(2)
