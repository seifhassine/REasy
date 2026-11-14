"""See docs/internals/gui-editor.md, Navigator."""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import QItemSelectionModel, QMimeData, QModelIndex, QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QLineEdit, QTabWidget, QTreeView, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

lazy from ui.editor_widgets import editor_strip, search_field, tool_button
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, qcolor

from ui.outline_model import OutlineModel
from ui.search_popup import below

from .. import edits
from ..model import GuiSymbol
from ..scene import GuiSceneNode, normalize_gui_resource_path
from ..validation import ERROR, WARNING
from .pickers import asset_entries

_MIME, _FOUND = "application/x-reasy-gui-nodes", 500
_SELECT = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
_ICONS = {ERROR: ("error", "danger"), WARNING: ("warning", "warning")}


def short(type_name: str) -> str:
    return type_name.rsplit(".", 1)[-1]


class Hierarchy(OutlineModel):
    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self.state = state
        self.found: list[GuiSceneNode] | None = None
        self.dragged: list[GuiSceneNode] = []

    def children_of(self, item) -> list:
        state = self.state
        if item is state:
            if self.found is not None:
                return self.found
            return state.scene.root.children if state.scope is not None else [state.scene.root]
        return [] if self.found is not None else [child for child in item.children if child.source is None]

    def owner_of(self, item):
        if self.found is not None or item.parent is None or self.state.stand(item):
            return self.state
        return item.parent

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 3

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        found = super().index(row, min(column, 1), parent)
        return self.createIndex(row, column, found.internalPointer()) if found.isValid() else found

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return (self.tr("Name"), self.tr("Class"), "")[section]
        return None

    def display(self, item, row: int, column: int, role: int):
        state = self.state
        if column == 2:
            name = state.stage.visible_field
            if role == Qt.ItemDataRole.DecorationRole:
                return line_icon("eye" if state.shown_value(item, name) else "eye_off",
                                 current_colors()["accent"] if state.scripted(item, name) else None)
            return self.tr("Shown or hidden in the preview. A click shows or hides it there as a script may; a "
                           "second click gives it back to the file. The file is not changed.") \
                if role == Qt.ItemDataRole.ToolTipRole else None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return item.object.name if column == 0 else short(item.object.type_name)
        if role == Qt.ItemDataRole.DecorationRole and column == 0:
            return line_icon("link" if not state.local(item) and not state.stand(item) else
                             "layers" if item.prototype is not None else "box")
        if role == Qt.ItemDataRole.ForegroundRole and (not item.effective_visible or column == 1):
            return qcolor(current_colors()["text_muted" if item.effective_visible else "text_subtle"])
        if role == Qt.ItemDataRole.ToolTipRole:
            if state.stand(item):
                return self.tr("The symbol itself, shown alone")
            if not state.local(item):
                return self.tr("From {0}: values set here are kept in this file").format(item.resource.key)
            if state.placed(item) > 1:
                return self.tr("Placed {0} times: its symbol is shared").format(state.placed(item))
        return None

    def rename(self, item, name: str | None) -> bool:
        state = self.state
        able = state.local(item) or state.stand(item)
        if able and name is not None:
            if state.stand(item):
                state.attempt(edits.rename_symbol, state.document, state.scope, name)
            else:
                state.attempt(edits.rename_object, state.document, item.object, name)
        return able

    def flags(self, index: QModelIndex):
        flags = super().flags(index)
        if index.isValid() and self.found is None:
            item, state = self.item(index), self.state
            if not state.fixed(item):
                flags |= Qt.ItemFlag.ItemIsDragEnabled
            if state.home(item) is not None:
                flags |= Qt.ItemFlag.ItemIsDropEnabled
        return flags

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        return [_MIME]

    def mimeData(self, indexes) -> QMimeData:
        self.dragged = [self.item(index) for index in indexes if index.column() == 0]
        data = QMimeData()
        data.setData(_MIME, b"")
        return data

    def dropMimeData(self, data, action, row: int, column: int, parent: QModelIndex) -> bool:
        state = self.state
        target = self.item(parent) if parent.isValid() else None
        objects = list(dict.fromkeys(node.object for node in self.dragged if not state.fixed(node)))
        if isinstance(target, GuiSceneNode) and objects and data.hasFormat(_MIME):
            before = state.document.undo_stack.index()
            state.attempt(self._move, objects, target, None if row < 0 else row)
            if state.document.undo_stack.index() != before:
                state.select_new(target, objects)
        return False

    def _move(self, objects: list, target: GuiSceneNode, index: int | None) -> None:
        edits.move_objects(self.state.document, objects, self.state.inside(target), index)


class Tree(QTreeView):
    eye_pressed = Signal(QModelIndex)

    def mousePressEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if index.column() == 2 and event.button() == Qt.MouseButton.LeftButton:
            self.eye_pressed.emit(index)
        else:
            super().mousePressEvent(event)


class Navigator(QTabWidget):
    menu_requested = Signal(QPoint)
    checked = Signal(int)

    def __init__(self, state, popup, parent: QWidget | None = None):
        super().__init__(parent)
        self.state, self.popup, self._syncing = state, popup, False
        self._open: dict[tuple, bool] = {}
        self._found: list = []
        self._library: list = []
        self._sources: tuple = ()
        self._groups: dict[str, bool] = {}
        self.setDocumentMode(True)
        self.search = search_field(QLineEdit(self))
        self.search.setPlaceholderText(self.tr("Filter objects"))
        self.tree = Tree(self)
        self.hierarchy = Hierarchy(state, self.tree)
        self.tree.setModel(self.hierarchy)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setUniformRowHeights(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().resizeSection(1, 96)
        self.tree.header().resizeSection(2, 26)
        self.library = QTreeWidget(self)
        self.library.setHeaderLabels([self.tr("Symbol"), self.tr("Class"), self.tr("Placed")])
        self.library.setUniformRowHeights(True)
        self.library.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.library.header().setStretchLastSection(False)
        self.library.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 84), (2, 52)):
            self.library.header().resizeSection(column, width)
        self.problems = QTreeWidget(self)
        self.problems.setHeaderHidden(True)
        self.problems.setRootIsDecorated(False)
        self.problems.setWordWrap(True)
        self.sources = QTreeWidget(self)
        self.sources.setHeaderHidden(True)
        self.sources.setUniformRowHeights(True)
        self.sources.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        strip, row = editor_strip()
        self.add_symbol = tool_button("add", self.tr("New symbol: a group of objects with its own states, placed "
                                                     "by controls"), self._new_symbol, text=self.tr("Symbol"))
        self.copy_symbol = tool_button("copy", self.tr("Duplicate the symbol"), self._duplicate_symbol)
        self.remove_item = tool_button("delete", self.tr("Remove the symbol or the import"), self._remove)
        self.open_symbol = tool_button("external", self.tr("Open the symbol alone"), self._open_symbol)
        for widget in (self.add_symbol, self.copy_symbol, self.remove_item, self.open_symbol):
            row.addWidget(widget)
        row.addStretch(1)
        imports, line = editor_strip("footer")
        self.path = QLineEdit(self)
        self.path.setPlaceholderText(self.tr("Import a GUI file: UI/GUI/.../name.gui"))
        self.path.returnPressed.connect(self._import)
        self.find = tool_button("search", self.tr("Choose one of the game's GUI files to import"), self._find)
        line.addWidget(self.path, 1)
        line.addWidget(self.find)
        line.addWidget(tool_button("add", self.tr("Import: its symbols can be placed in this file"), self._import))
        for title, widgets in ((self.tr("Hierarchy"), (self.search, self.tree)),
                               (self.tr("Library"), (strip, self.library, imports)),
                               (self.tr("Problems"), (self.problems,)), (self.tr("Sources"), (self.sources,))):
            page = QWidget(self)
            column = QVBoxLayout(page)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(0)
            for widget in widgets:
                column.addWidget(widget)
            self.addTab(page, title)
        self.setTabToolTip(3, self.tr("The files this tab has read: what the game's scenes give the view, then the "
                                      "files by kind"))
        self._check = QTimer(self)
        self._check.setSingleShot(True)
        self._check.setInterval(400)
        self._check.timeout.connect(self.check)
        self._refill = QTimer(self)
        self._refill.setSingleShot(True)
        self._refill.timeout.connect(self._fill)
        self.search.textChanged.connect(self._search)
        self.tree.expanded.connect(self._expanded)
        self.tree.collapsed.connect(self._collapsed)
        self.tree.doubleClicked.connect(self._entered)
        self.tree.eye_pressed.connect(self._eye)
        self.tree.selectionModel().selectionChanged.connect(self._tree_selected)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.library.itemChanged.connect(self._renamed)
        self.library.itemDoubleClicked.connect(self._open_symbol)
        self.library.itemSelectionChanged.connect(self._update)
        self.problems.itemClicked.connect(self._problem_opened)
        self.currentChanged.connect(self._fill)
        self.currentChanged.connect(self.list_sources)
        state.changed.connect(self._changed)
        state.selection_changed.connect(self._sync)
        self._reset()
        self._fill()
        self._check.start()

    def _changed(self, tree: bool) -> None:
        self._check.start()
        if tree:
            self._search(self.search.text())
        else:
            self.tree.viewport().update()
        self._fill()

    def _reset(self) -> None:
        state = self.state
        self._syncing = True
        self.hierarchy.reset(state)
        for node in state.scene.nodes:
            key = state.key(node)
            if self._open.setdefault(key, len(key) < 3) and (index := self.hierarchy.index_of(node)).isValid():
                self.tree.expand(index)
        self._syncing = False
        self._sync()

    def _expanded(self, index: QModelIndex) -> None:
        if not self._syncing:
            self._open[self.state.key(self.hierarchy.item(index))] = True

    def _collapsed(self, index: QModelIndex) -> None:
        if not self._syncing:
            self._open[self.state.key(self.hierarchy.item(index))] = False

    def _search(self, text: str) -> None:
        text = text.strip().casefold()
        self.hierarchy.found = [node for node in self.state.scene.nodes
                                if text in node.object.name.casefold()][:_FOUND] if text else None
        self._reset()

    def _menu(self, point: QPoint) -> None:
        self.menu_requested.emit(self.tree.viewport().mapToGlobal(point))

    def _entered(self, index: QModelIndex) -> None:
        self.tree.edit(index.siblingAtColumn(0))

    def _eye(self, index: QModelIndex) -> None:
        state, node = self.state, self.hierarchy.item(index)
        name = state.stage.visible_field
        if state.scripted(node, name):
            state.clear_script(node, name)
        else:
            state.set_script(node, name, not state.shown_value(node, name))

    def _tree_selected(self) -> None:
        if not self._syncing:
            self._syncing = True
            self.state.select([self.hierarchy.item(index) for index in self.tree.selectionModel().selectedRows()])
            self._syncing = False

    def _sync(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        selection, index = self.tree.selectionModel(), QModelIndex()
        selection.clearSelection()
        for node in self.state.selection:
            if (found := self.hierarchy.index_of(node)).isValid():
                selection.select(index := found, _SELECT)
        if index.isValid():
            self.tree.scrollTo(index)
        self._syncing = False

    def _chosen(self):
        items = self.library.selectedItems()
        return self._library[items[0].data(0, Qt.ItemDataRole.UserRole)] if items else None

    def _update(self) -> None:
        chosen, gui = self._chosen(), self.state.document.gui
        local = isinstance(chosen, GuiSymbol) and any(item is chosen for item in gui.symbols)
        self.copy_symbol.setEnabled(local)
        self.open_symbol.setEnabled(local)
        self.remove_item.setEnabled(isinstance(chosen, str) or local and chosen is not gui.symbols[0])

    def _fill(self) -> None:
        if self.currentIndex() != 1:
            return
        state, held = self.state, self._chosen()
        gui, structure = state.document.gui, state.document.structure
        self.library.blockSignals(True)
        self.library.clear()
        self._library = []

        def add(parent, texts: list[str], value, icon: str, editable: bool = False) -> QTreeWidgetItem:
            item = QTreeWidgetItem(parent, texts)
            item.setData(0, Qt.ItemDataRole.UserRole, len(self._library))
            item.setIcon(0, line_icon(icon))
            if editable:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._library.append(value)
            if value is held:
                item.setSelected(True)
            return item

        for symbol in gui.symbols:
            item = add(self.library, [symbol.name, short(symbol.type_name),
                                      str(len(structure.uses.get(symbol.guid, ())))], symbol, "layers", True)
            if symbol is state.scope:
                item.setIcon(0, line_icon("eye"))
        for path in gui.imported_gui_paths:
            key = normalize_gui_resource_path(path)
            resource = state.workspace.resources.get(key) if key != state.workspace.root_key else None
            group = add(self.library, [path, "", ""], path, "link" if resource is not None else "warning")
            group.setToolTip(0, path if resource is not None else self.tr("{0} was not found").format(path))
            for symbol in resource.document.symbols[1:] if resource is not None else ():
                add(group, [symbol.name, short(symbol.type_name), ""], symbol, "layers")
        self.library.blockSignals(False)
        self._update()

    def list_sources(self) -> None:
        state, tree, scenes, files = self.state, self.sources, {}, {}
        if self.currentIndex() != 3:
            return
        for name, *row in state.stage.sources():
            scenes.setdefault(name, []).append(row)
        for key, row in sorted(tuple(state.dependencies.loaded.items())):
            files.setdefault(key.rsplit(".", 1)[-1], []).append(row)
        if (scenes, files) == self._sources:
            return
        self._sources = scenes, files
        for at in range(tree.topLevelItemCount()):
            held = tree.topLevelItem(at)
            self._groups[held.data(0, Qt.ItemDataRole.UserRole)] = held.isExpanded()
        tree.clear()
        for name, title, rows, opened in (*((name, name, rows, True) for name, rows in scenes.items()), *(
                (kind, self.tr("{0} files ({1})").format(kind, len(rows)), rows, False)
                for kind, rows in sorted(files.items()))):
            group = QTreeWidgetItem(tree, [title])
            group.setData(0, Qt.ItemDataRole.UserRole, name)
            for path, source in rows:
                QTreeWidgetItem(group, [source]).setToolTip(0, self.tr("{0}\nread from {1}").format(path, source))
            group.setExpanded(self._groups.get(name, opened))

    def _renamed(self, item: QTreeWidgetItem, column: int) -> None:
        symbol, state = self._library[item.data(0, Qt.ItemDataRole.UserRole)], self.state
        if column == 0 and isinstance(symbol, GuiSymbol) and item.text(0) != symbol.name:
            state.attempt(edits.rename_symbol, state.document, symbol, item.text(0))
            self._refill.start()

    def _new_symbol(self) -> None:
        state, authoring = self.state, self.state.document.authoring
        self.popup.pick([(short(name), "", name) for name in authoring.palette if authoring.is_control(name)],
                        below(self.add_symbol), self.tr("Class of the new symbol"),
                        lambda name: edits.add_symbol(state.document, name))

    def _duplicate_symbol(self) -> None:
        self.state.attempt(edits.duplicate_symbol, self.state.document, self._chosen())

    def _remove(self) -> None:
        chosen, state = self._chosen(), self.state
        if isinstance(chosen, str):
            state.attempt(edits.remove_import, state.document, chosen)
        elif chosen is not None:
            if chosen is state.scope:
                state.open_symbol(None)
            state.attempt(edits.remove_symbol, state.document, chosen)

    def _open_symbol(self, *_item) -> None:
        chosen, state = self._chosen(), self.state
        if isinstance(chosen, GuiSymbol) and any(item is chosen for item in state.document.gui.symbols):
            state.open_symbol(None if chosen is state.document.gui.symbols[0] else chosen)

    def _find(self) -> None:
        state = self.state
        self.popup.pick(asset_entries(state, ("gui",)), below(self.find), self.tr("Search the game's GUI files"),
                        partial(edits.add_import, state.document))

    def _import(self) -> None:
        state, path = self.state, self.path.text().strip()
        before = state.document.undo_stack.index()
        if path:
            state.attempt(edits.add_import, state.document, path)
        if state.document.undo_stack.index() != before:
            self.path.clear()

    def check(self) -> None:
        self._check.stop()
        self._found, colors = self.state.problems, current_colors()
        errors = sum(item.severity == ERROR for item in self._found)
        self.setTabText(2, self.tr("Problems ({0})").format(len(self._found)) if self._found else self.tr("Problems"))
        self.problems.clear()
        for item in self._found[:_FOUND]:
            row = QTreeWidgetItem(self.problems, [item.text])
            icon, tone = _ICONS.get(item.severity, ("info", "info"))
            row.setIcon(0, line_icon(icon, colors[tone]))
            row.setToolTip(0, item.text)
        self.checked.emit(errors)

    def _problem_opened(self, item: QTreeWidgetItem) -> None:
        target = self._found[self.problems.indexOfTopLevelItem(item)].target
        if isinstance(target, GuiSymbol):
            self.setCurrentIndex(1)
            for row in range(self.library.topLevelItemCount()):
                held = self.library.topLevelItem(row)
                held.setSelected(self._library[held.data(0, Qt.ItemDataRole.UserRole)] is target)
        else:
            self.state.reveal(target)
