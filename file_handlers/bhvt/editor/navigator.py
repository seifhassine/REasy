"""See docs/internals/bhvt-editor.md, Navigator."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QMimeData, QModelIndex, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHeaderView, QLineEdit, QMenu, QTabWidget, QTreeView,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

lazy from file_handlers.uvar.uvar_file import UVarFile
lazy from ui.editor_widgets import editor_strip, search_field, tool_button
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors

from ui.outline_model import OutlineModel

from .. import container_edits, edits, graph_edits
from ..model import Hfsm, Record, TimelineFsm, Tree
from ..references import own_variables
from ..structure import Transition
from ..summaries import short_name, type_name
from ..validation import ERROR
from .containers import ContainerPane

_MIME, _FOUND = "application/x-reasy-bhvt-nodes", 500
_SELECT = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows


class Hierarchy(OutlineModel):
    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self.state = state
        self.found: list | None = None
        self.dragged: list = []

    def children_of(self, item) -> list:
        state = self.state
        if item is state:
            return self.found if self.found is not None else \
                list(state.graphs) if state.layered else [state.structure.root]
        if self.found is not None:
            return []
        if isinstance(item, (Tree, Hfsm)):
            return [state.structure_of(item).root]
        model = state.model_of(item)
        return [] if model is None else state.structure_of(model).children(item)

    def owner_of(self, item):
        state = self.state
        if isinstance(item, (Tree, Hfsm)) or self.found is not None:
            return state
        if (model := state.model_of(item)) is None:
            return None
        return state.structure_of(model).parent.get(id(item)) or (model if state.layered else state)

    def display(self, item, row: int, column: int, role: int):
        graph = isinstance(item, (Tree, Hfsm))
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if column == 0:
                return self.state.title(item) if graph else item.name
            return "" if graph else self._kind(item)
        if role == Qt.ItemDataRole.DecorationRole and column == 0:
            return line_icon("layers" if graph else "link" if vars(item).get("reference_tree") else
                             "graph" if item.children else "box")
        return None

    def _kind(self, item) -> str:
        if (model := self.state.model_of(item)) is None:
            return ""
        structure = self.state.structure_of(model)
        if self.found is not None:
            return " / ".join(node.name for node in structure.path(item)[:-1])
        if not item.children:
            return ""
        if structure.is_fsm(item):
            return self.tr("States: {0}").format(len(item.children))
        selector = structure.selector(item)
        return "" if selector is None else short_name(type_name(model, *selector))

    def rename(self, item, name: str | None) -> bool:
        if isinstance(item, (Tree, Hfsm)):
            layer = self.state.layer(item)
            if layer is not None and name is not None:
                self.state.attempt(container_edits.rename, self.state.document, layer, name)
            return layer is not None
        if name is not None and name != item.name:
            edits.set_fields(self.state.document, item, self.tr("Rename"), name=name)
        return True

    def flags(self, index: QModelIndex):
        flags = super().flags(index)
        if index.isValid() and self.found is None and isinstance(self.item(index), Record):
            flags |= Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
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
        state, target = self.state, self.item(parent) if parent.isValid() else None
        if not isinstance(target, Record) or not data.hasFormat(_MIME):
            return False
        model = state.model_of(target)
        for node in self.dragged:
            if state.model_of(node) is model:
                kids = state.structure_of(model).children(target)
                at = len(kids) if row < 0 else row
                at -= any(kid is node for kid in kids[:at])
                state.attempt(graph_edits.move_node, state.document, model, node, target, at)
                row = row if row < 0 else at + 1
        return False


class Navigator(QTabWidget):
    menu_requested = Signal(object)
    raw_requested = Signal(str, object)
    checked = Signal(int)

    def __init__(self, state, parent: QWidget | None = None):
        super().__init__(parent)
        self.state, self._syncing = state, False
        self._open: dict[int, object] = {}
        self._rows: list = []
        self._found: list = []
        self.setDocumentMode(True)
        self.search = search_field(QLineEdit(self))
        self.search.setPlaceholderText(self.tr("Filter nodes"))
        self.tree = QTreeView(self)
        self.hierarchy = Hierarchy(state, self.tree)
        self.tree.setModel(self.hierarchy)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed |
                                  QAbstractItemView.EditTrigger.SelectedClicked)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setUniformRowHeights(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().resizeSection(1, 88)
        self.owners = tool_button("folder_open", self.tr(
            "Owner variables: the .uvar files whose variables the running component adds (its UserVariables, "
            "global files)"), self._choose_owners, text=self.tr("Owner files"))
        self.variables = QTreeWidget(self)
        self.variables.setHeaderLabels([self.tr("Name"), self.tr("Type"), self.tr("File")])
        self.variables.setRootIsDecorated(False)
        self.variables.setUniformRowHeights(True)
        self.variables.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.problems = QTreeWidget(self)
        self.problems.setHeaderHidden(True)
        self.problems.setRootIsDecorated(False)
        strip, row = editor_strip()
        row.addWidget(self.owners)
        row.addWidget(tool_button("edit", self.tr("Open the variable editor"), self._edit_variables,
                                  text=self.tr("Edit")))
        row.addStretch(1)
        pages = [(self.tr("Hierarchy"), (self.search, self.tree)), (self.tr("Variables"), (strip, self.variables)),
                 (self.tr("Problems"), (self.problems,))]
        if isinstance(state.document.resource.model, TimelineFsm):
            layers, row = editor_strip()
            row.addWidget(tool_button("add", self.tr("Add a layer: a tree of its own, run beside the others"),
                                      self._add_layer, text=self.tr("Layer")))
            row.addWidget(tool_button("delete", self.tr("Remove the shown layer"), self._remove_layer))
            row.addStretch(1)
            pages[0] = (pages[0][0], (layers, *pages[0][1]))
            self.containers = ContainerPane(state, self)
            self.timelines, self.holds = self.containers.timelines, self.containers.holds
            self.containers.opened.connect(self._timeline_opened)
            pages.append((self.tr("Timelines"), (self.containers,)))
        for title, widgets in pages:
            page = QWidget(self)
            column = QVBoxLayout(page)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(0)
            for widget in widgets:
                column.addWidget(widget)
            self.addTab(page, title)
        self._check = QTimer(self)
        self._check.setSingleShot(True)
        self._check.setInterval(400)
        self._check.timeout.connect(self.check)
        self.search.textChanged.connect(self._search)
        self.tree.expanded.connect(self._expanded)
        self.tree.collapsed.connect(self._collapsed)
        self.tree.doubleClicked.connect(self._entered)
        self.tree.selectionModel().selectionChanged.connect(self._tree_selected)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.variables.itemSelectionChanged.connect(self._variable_selected)
        self.problems.itemClicked.connect(self._problem_opened)
        self.currentChanged.connect(self._fill)
        state.changed.connect(self._changed)
        state.selection_changed.connect(self._sync)
        self.tree.expandToDepth(1 if state.layered else 0)
        self._check.start()

    def _changed(self, reshaped: bool) -> None:
        self._check.start()
        if not reshaped:
            self.tree.viewport().update()
        elif self.hierarchy.found is None:
            self._reset()
        else:
            self._search(self.search.text())
        self._fill()

    def _reset(self) -> None:
        self._syncing = True
        self.hierarchy.reset(self.state)
        for item in list(self._open.values()):
            index = self.hierarchy.index_of(item)
            if index.isValid():
                self.tree.expand(index)
            else:
                del self._open[id(item)]
        self._syncing = False
        self._sync()

    def _expanded(self, index: QModelIndex) -> None:
        item = self.hierarchy.item(index)
        self._open[id(item)] = item

    def _collapsed(self, index: QModelIndex) -> None:
        if not self._syncing:
            self._open.pop(id(self.hierarchy.item(index)), None)

    def _search(self, text: str) -> None:
        text, state = text.strip().casefold(), self.state
        self.hierarchy.found = [node for model in state.graphs for node in state.structure_of(model).nodes
                                if text in node.name.casefold()][:_FOUND] if text else None
        self._reset()

    def _menu(self, point) -> None:
        index, at = self.tree.indexAt(point), self.tree.viewport().mapToGlobal(point)
        if not index.isValid() or self.state.layer(self.hierarchy.item(index)) is None:
            self.menu_requested.emit(at)
            return
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.addAction(self.tr("Rename")).triggered.connect(self._rename_layer)
        menu.addAction(self.tr("Add layer")).triggered.connect(self._add_layer)
        menu.addAction(self.tr("Remove layer")).triggered.connect(self._remove_layer)
        menu.popup(at)

    def _rename_layer(self) -> None:
        self.tree.edit(self.hierarchy.index_of(self.state.model))

    def _add_layer(self) -> None:
        state = self.state
        if (layer := state.attempt(container_edits.add_tree, state.document)) is not None:
            state.show(None, layer.tree)
            for item in (layer.tree, layer.tree.nodes[0]):
                self._open[id(item)] = item
                self.tree.expand(self.hierarchy.index_of(item))

    def _remove_layer(self) -> None:
        state = self.state
        state.attempt(container_edits.remove_tree, state.document, state.layer(state.model))

    def _entered(self, index: QModelIndex) -> None:
        item = self.hierarchy.item(index)
        if isinstance(item, Record) and item.children:
            self.state.show(item, self.state.model_of(item))

    def _tree_selected(self) -> None:
        if self._syncing:
            return
        state = self.state
        items = [self.hierarchy.item(index) for index in self.tree.selectionModel().selectedRows()]
        nodes = [item for item in items if isinstance(item, Record)]
        self._syncing = True
        if nodes:
            state.reveal(nodes[-1], state.model_of(nodes[-1]))
            state.select([node for node in nodes if state.model_of(node) is state.model])
        elif items:
            state.show(None, items[-1])
        self._syncing = False

    def _sync(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        selection, index = self.tree.selectionModel(), QModelIndex()
        selection.clearSelection()
        for node in self.state.nodes():
            if (found := self.hierarchy.index_of(node)).isValid():
                selection.select(index := found, _SELECT)
        if index.isValid():
            self.tree.scrollTo(index)
        self._syncing = False

    def _fill(self) -> None:
        state = self.state
        if self.currentIndex() == 3:
            self.containers.fill()
        if self.currentIndex() != 1:
            return
        names = {id(uvar): uvar.header.name or "" for uvar in own_variables(state.model)}
        names.update((id(uvar), Path(path).name) for path, uvar in state.document.owner_variables.items())
        found = list(state.summaries.found.values())
        self.variables.blockSignals(True)
        self.variables.clear()
        self._rows = [variable for _source, variable in found]
        self.variables.addTopLevelItems([QTreeWidgetItem([variable.name, variable.type.name, names[id(source)]])
                                         for source, variable in found])
        self.variables.blockSignals(False)

    def _variable_selected(self) -> None:
        if self.variables.selectedItems():
            self.state.select([self._rows[self.variables.currentIndex().row()]])

    def _choose_owners(self) -> None:
        document = self.state.document
        paths, _filter = QFileDialog.getOpenFileNames(self, self.tr("Owner variables"),
                                                      str(Path(document.rsz.filepath).parent),
                                                      self.tr("User variables (*.uvar.*)"))
        try:
            found = {path: UVarFile(Path(path).read_bytes()) for path in paths}
        except (ValueError, OSError) as error:
            self.state.refused.emit(str(error))
            return
        if found:
            document.owner_variables = found
            self.state.refresh()

    def _edit_variables(self) -> None:
        self.raw_requested.emit("uvar", self.state.model.variable)

    def _timeline_opened(self, timeline) -> None:
        self.raw_requested.emit("timeline", timeline)

    def check(self) -> None:
        self._check.stop()
        self._found, colors = self.state.problems, current_colors()
        errors = sum(item.severity == ERROR for item in self._found)
        self.setTabText(2, self.tr("Problems ({0})").format(len(self._found)) if self._found else self.tr("Problems"))
        self.problems.clear()
        for item in self._found[:_FOUND]:
            row, tone = QTreeWidgetItem(self.problems, [item.text]), "danger" if item.severity == ERROR else "warning"
            row.setIcon(0, line_icon("error" if item.severity == ERROR else "warning", colors[tone]))
            row.setToolTip(0, item.text)
        self.checked.emit(errors)

    def _problem_opened(self, item: QTreeWidgetItem) -> None:
        problem, state = self._found[self.problems.indexOfTopLevelItem(item)], self.state
        if isinstance(problem.target, (Record, Transition)):
            state.reveal(problem.target, problem.model)
        elif problem.target is None:
            if problem.model is not state.model:
                state.show(None, problem.model)
        else:
            state.show(None, problem.model)
            state.select([variable for _source, variable in state.summaries.found.values()
                          if variable.expression is not None and any(
                              node is problem.target for node in variable.expression.nodes)][:1])
