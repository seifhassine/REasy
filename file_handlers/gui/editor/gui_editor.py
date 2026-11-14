"""See docs/internals/gui-editor.md, Workspace."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QFrame, QMenu, QSplitter, QVBoxLayout, QWidget

lazy from ui.breadcrumbs import breadcrumb_separator
lazy from ui.editor_widgets import ElidedLabel, editor_strip, icon_action, styled_label, tool_button, toolbar_separator
lazy from ui.field_form import FieldForm, shape
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role

from ui.search_popup import SearchPopup, below

from .. import clipboard, edits
from ..document import GuiEditDocument
from ..errors import GuiEditError
from .canvas import Canvas
from .inspector import Inspector
from .navigator import Navigator, short
from .state import EditorState
from .timeline import Timeline


class GuiEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, document: GuiEditDocument, handler, game: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.document, self.handler = document, handler
        self.state = state = EditorState(document, handler.filepath, handler.resolve_resource,
                                         handler.locate_resource, handler.listed, game, self)
        self.popup = SearchPopup(self, state.attempt)
        self._panel: QFrame | None = None
        self._listed: tuple = ()
        self._menu_at = QPoint()
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.navigator = Navigator(state, self.popup, self.splitter)
        self.center = QSplitter(Qt.Orientation.Vertical, self.splitter)
        stage = QWidget(self.center)
        column = QVBoxLayout(stage)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.canvas = Canvas(state, stage)
        column.addWidget(self._crumbs(stage))
        column.addWidget(self.canvas, 1)
        self.timeline = Timeline(state, self.popup, self.center)
        self.center.addWidget(stage)
        self.center.addWidget(self.timeline)
        self.center.setStretchFactor(0, 1)
        self.center.setSizes([520, 240])
        self.inspector = Inspector(state, self.popup, self.splitter)
        for widget in (self.navigator, self.center, self.inspector):
            self.splitter.addWidget(widget)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([300, 760, 400])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar())
        layout.addWidget(self.splitter, 1)
        self._clear = QTimer(self)
        self._clear.setSingleShot(True)
        self._clear.setInterval(8000)
        self._clear.timeout.connect(self.status.clear)
        self._shortcuts()
        document.undo_stack.cleanChanged.connect(self._clean_changed)
        for signal in (state.changed, state.selection_changed, state.scope_changed):
            signal.connect(self._update)
        state.refused.connect(self._refused)
        self.canvas.menu_requested.connect(self.show_menu)
        self.canvas.drawn.connect(self._drawn)
        self.canvas.drawn.connect(self.navigator.list_sources)
        self.navigator.menu_requested.connect(self.show_menu)
        self.navigator.checked.connect(self._checked)
        self._update()

    def _crumbs(self, parent: QWidget) -> QWidget:
        strip, row = editor_strip("header", (6, 2, 6, 2), 0)
        strip.setParent(parent)
        self.home = tool_button("home", self.tr("Back to the whole file"), self._leave,
                                text=Path(self.state.source).name, parent=strip)
        self.arrow = breadcrumb_separator(strip)
        self.alone = styled_label("", strip, heading="panel")
        self.alone.setToolTip(self.tr("The symbol alone: its own objects and states, as every control that places it "
                                      "gets them"))
        for widget in (self.home, self.arrow, self.alone):
            row.addWidget(widget)
        row.addStretch(1)
        return strip

    def _toolbar(self) -> QWidget:
        strip, row = editor_strip()
        strip.setParent(self)
        stack, canvas = self.document.undo_stack, self.canvas
        self.undo = tool_button("undo", self.tr("Undo (Ctrl+Z)"), stack.undo, parent=strip)
        self.redo = tool_button("redo", self.tr("Redo (Ctrl+Y)"), stack.redo, parent=strip)
        self.add = tool_button("add", self.tr("Add an object, or place a symbol, in the selected control"),
                               self._add, text=self.tr("Object"), parent=strip)
        self.remove = tool_button("delete", self.tr("Delete (Del)"), self.delete, parent=strip)
        self.copy_button = tool_button("copy", self.tr("Copy (Ctrl+C)"), self.copy, parent=strip)
        self.paste_button = tool_button("paste", self.tr("Paste (Ctrl+V)"), self.paste, parent=strip)
        self.fit = tool_button("fit", self.tr("Fit the screen (F)"), canvas.fit, parent=strip)
        self.guides = tool_button("grid", self.tr("Outline every object"), parent=strip)
        self.guides.setCheckable(True)
        self.guides.toggled.connect(canvas.set_guides)
        self.live = tool_button("target", self.tr("Use the GUI as the game does: states run, and the pointer and the "
                                                  "game's keys move its lists. The pointer edits nothing meanwhile."),
                                text=self.tr("Try"), parent=strip)
        self.live.setCheckable(True)
        self.live.toggled.connect(self._try)
        self.preview = tool_button("settings", self.tr("What the game sets and a file does not hold: window size, "
                                                       "safe area, language, input device"), self._settings,
                                   text=self.tr("Preview"), parent=strip)
        self.status = ElidedLabel(strip)
        set_style_role(self.status, tone="danger")
        self.limits = tool_button("info", self.tr("What the preview does not draw as the game does"),
                                  self._show_limits, text="0", parent=strip)
        self.limits.hide()
        self.issues = tool_button("error", self.tr("Errors: the file cannot be saved until they are fixed"),
                                  self._show_problems, text="0", parent=strip)
        self.issues.setIcon(line_icon("error", current_colors()["danger"]))
        self.issues.hide()
        for widget in (self.undo, self.redo, toolbar_separator(), self.add, self.remove, toolbar_separator(),
                       self.copy_button, self.paste_button, toolbar_separator(), self.fit, self.guides, self.live,
                       self.preview):
            row.addWidget(widget)
        row.addWidget(self.status, 1)
        row.addWidget(self.limits)
        row.addWidget(self.issues)
        self.undo.setEnabled(stack.canUndo())
        self.redo.setEnabled(stack.canRedo())
        stack.canUndoChanged.connect(self.undo.setEnabled)
        stack.canRedoChanged.connect(self.redo.setEnabled)
        return strip

    def _shortcuts(self) -> None:
        stack = self.document.undo_stack
        for keys, slot in ((QKeySequence.StandardKey.Undo, stack.undo), (QKeySequence.StandardKey.Redo, stack.redo)):
            QShortcut(keys, self, slot).setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        keys = ((QKeySequence.StandardKey.Delete, self.delete), (QKeySequence.StandardKey.Copy, self.copy),
                (QKeySequence.StandardKey.Paste, self.paste), ("Ctrl+D", self.duplicate))
        self._canvas_keys = [QShortcut(key, self.canvas, slot) for key, slot in (*keys, ("F", self.canvas.fit))]
        for shortcut in (*self._canvas_keys, *(QShortcut(key, self.navigator.tree, slot) for key, slot in keys)):
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)

    def _update(self, *_changes) -> None:
        state = self.state
        scoped = state.scope is not None
        self.home.setEnabled(scoped)
        self.arrow.setVisible(scoped)
        self.alone.setVisible(scoped)
        self.alone.setText(state.scope.name if scoped else "")
        chosen = [node for node in state.selection if state.local(node) and node.parent is not None]
        self.remove.setEnabled(bool(chosen))
        self.copy_button.setEnabled(bool(chosen))

    def _leave(self) -> None:
        self.state.open_symbol(None)

    def _try(self, on: bool) -> None:
        state = self.state
        state.stage.interact(on)
        self.canvas.set_live(on)
        for shortcut in self._canvas_keys:
            shortcut.setEnabled(not on)
        state.reset()
        state.play(on)
        if on:
            self.canvas.setFocus()

    def _entries(self) -> list[tuple]:
        state, gui = self.state, self.document.gui
        found = [(short(name), self.tr("new object"), ("class", name)) for name in self.document.authoring.palette]
        found += [(symbol.name, self.tr("symbol of this file"), ("symbol", symbol)) for symbol in gui.symbols[1:]]
        return found + [(symbol.name, key, ("symbol", symbol)) for key, resource in state.workspace.resources.items()
                        if key != state.workspace.root_key for symbol in resource.document.symbols[1:]]

    def _add(self) -> None:
        self._offer(below(self.add))

    def _add_here(self) -> None:
        self._offer(self._menu_at)

    def _offer(self, at: QPoint) -> None:
        self.popup.pick(self._entries(), at, self.tr("Add an object or place a symbol"), self._added)

    def _added(self, choice: tuple) -> None:
        state, document, (kind, value) = self.state, self.document, choice
        parent, symbol, index = state.container()
        if kind == "symbol":
            made = edits.add_object(document, symbol, value.type_name, index, value)
        elif document.authoring.is_control(value):
            made = edits.add_control(document, symbol, value, index)
        else:
            made = edits.add_object(document, symbol, value, index)
        state.select_new(parent, [made])

    def delete(self) -> None:
        self.state.attempt(self._delete, list(self.state.selection))

    def _delete(self, nodes: list) -> None:
        for node in nodes:
            if reason := self.state.fixed(node):
                raise GuiEditError(reason)
        if nodes:
            edits.remove_objects(self.document, list(dict.fromkeys(node.object for node in nodes)))

    def copy(self) -> None:
        state = self.state
        objects = list(dict.fromkeys(node.object for node in state.selection
                                     if state.local(node) and node.parent is not None))
        if objects:
            state.attempt(clipboard.hold, self.document, objects)

    def paste(self) -> None:
        self.state.attempt(self._paste, False)

    def paste_inside(self) -> None:
        self.state.attempt(self._paste, True)

    def _paste(self, into: bool) -> None:
        state, data = self.state, clipboard.held()
        if data is not None:
            parent, symbol, index = state.container(into)
            state.select_new(parent, clipboard.paste(self.document, data, symbol, index))

    def duplicate(self) -> None:
        self.state.attempt(self._duplicate, list(self.state.selection))

    def _duplicate(self, nodes: list) -> None:
        state = self.state
        if not nodes:
            return
        parent = nodes[-1].parent
        if parent is None or any(node.parent is not parent for node in nodes):
            raise GuiEditError(self.tr("Select objects of one control to duplicate them."))
        symbol, objects = state.inside(parent), [node.object for node in nodes]
        index = max(symbol.objects.index(obj) for obj in objects) + 1
        state.select_new(parent, clipboard.paste(self.document, clipboard.copied(self.document, objects), symbol,
                                                 index))

    def show_menu(self, at: QPoint) -> None:
        state, node, self._menu_at = self.state, self.state.current, at
        home = state.home(node) if node is not None else None
        held = node.prototype if node is not None else None
        placed = clipboard.held() is not None
        own = node is not None and state.local(node) and node.parent is not None
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        for icon, text, slot, shown in (
                ("add", self.tr("Add..."), self._add_here, True),
                ("external", self.tr("Open Symbol Alone"), self._open_current,
                 home is not None and node.parent is not None and not state.stand(node)),
                ("split", self.tr("Make Unique"), self._unique_current, held is not None and own),
                ("copy", self.tr("Copy"), self.copy, own),
                ("paste", self.tr("Paste"), self.paste, placed),
                ("paste", self.tr("Paste Inside"), self.paste_inside, placed and home is not None),
                ("copy", self.tr("Duplicate"), self.duplicate, own),
                ("delete", self.tr("Delete"), self.delete, own)):
            if shown:
                icon_action(menu, icon, text, icon == "delete").triggered.connect(slot)
        menu.popup(at)

    def _open_current(self) -> None:
        self.state.open_symbol(self.state.home(self.state.current))

    def _unique_current(self) -> None:
        state, node = self.state, self.state.current
        state.attempt(edits.make_unique, self.document, node.object, node.prototype.symbol)

    def _settings(self) -> None:
        if self._panel is None:
            self._panel = QFrame(self, Qt.WindowType.Popup)
            self._panel.setFrameShape(QFrame.Shape.StyledPanel)
            QVBoxLayout(self._panel).setContentsMargins(4, 4, 4, 4)
        self._list_settings()
        self._panel.move(below(self.preview))
        self._panel.show()

    def _list_settings(self) -> None:
        fields, column = self.state.stage.settings(self._list_settings), self._panel.layout()
        self.canvas.update()
        if (listed := (shape(fields), [field.tooltip for field in fields])) == self._listed:
            column.itemAt(0).widget().refresh()
            return
        if self._listed:
            (old := column.takeAt(0).widget()).retire()
            old.deleteLater()
        self._listed = listed
        column.addWidget(form := FieldForm(fields, self._panel))
        form.show()
        self._panel.adjustSize()

    def _drawn(self) -> None:
        count = len(self.state.stage.gaps)
        self.limits.setText(str(count))
        self.limits.setVisible(count > 0)

    def _show_limits(self) -> None:
        self.popup.pick([(reason, path, path) for path, reason in self.state.stage.gaps], below(self.limits),
                        self.tr("Not drawn as the game does"), self._reveal, fit=True)

    def _reveal(self, path: str) -> None:
        if (node := self.state.scene.nodes_by_path.get(path)) is not None:
            self.state.select([node])

    def _clean_changed(self, clean: bool) -> None:
        self.modified_changed.emit(not clean)

    def _refused(self, text: str) -> None:
        self.status.setText(text)
        self._clear.start()

    def _checked(self, errors: int) -> None:
        self.issues.setText(str(errors))
        self.issues.setVisible(errors > 0)

    def _show_problems(self) -> None:
        self.navigator.setCurrentIndex(2)
