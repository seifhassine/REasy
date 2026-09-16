"""FSM graph navigation and the existing native field editor in one workspace."""
import os

from PySide6.QtCore import Qt, QSortFilterProxyModel, QSignalBlocker, QTimer, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTabWidget,
                               QLineEdit, QListView, QLabel, QPushButton, QComboBox, QToolBar)

from .graph_model import MotfsmGraph
from .graph_view import MotfsmGraphView, EDGE_COLORS, EDGE_NAMES
from .motfsm_viewer import MotfsmViewer
from .motion_preview import FsmMotionPreview
from .action_editor import ActionEditor


class MotfsmGraphWorkspace(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.model = None
        self.focus = self.selected = None
        self._selected_edge = None
        self._display_mode = None
        self.history = []
        self.layouts = {}
        settings = getattr(handler.app, 'settings', None)
        if isinstance(settings, dict) and handler.filepath:
            key = os.path.normcase(os.path.abspath(handler.filepath))
            self.layouts = settings.setdefault('motfsm_graph_layouts', {}).setdefault(key, {})
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QToolBar(self)
        toolbar.setMovable(False)
        stack = handler.editor_document.undo_stack
        self.undo_action = stack.createUndoAction(self, self.tr('Undo'))
        self.redo_action = stack.createRedoAction(self, self.tr('Redo'))
        self.undo_action.setShortcut(QKeySequence.Undo)
        self.redo_action.setShortcuts([QKeySequence('Ctrl+Y'), QKeySequence('Ctrl+Shift+Z')])
        for action in (self.undo_action, self.redo_action):
            action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            self.addAction(action)
            toolbar.addAction(action)
        layout.addWidget(toolbar)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        self.graph_page = QSplitter(Qt.Horizontal, self)
        self.tabs.addTab(self.graph_page, self.tr('Graph'))
        self.fields = MotfsmViewer(handler)
        self.tabs.addTab(self.fields, self.tr('Advanced fields'))
        self.tree = self.fields.tree

        navigation = QWidget(self)
        navigation.setMinimumWidth(170)
        navigation.setMaximumWidth(400)
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(4, 4, 4, 4)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(self.tr('Find node, path or ID…'))
        self.search.setClearButtonEnabled(True)
        nav_layout.addWidget(self.search)
        self.node_count = QLabel(self)
        nav_layout.addWidget(self.node_count)
        self.node_model = QStandardItemModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.node_model)
        self.proxy.setFilterRole(Qt.UserRole+1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.navigation = QListView(self)
        self.navigation.setModel(self.proxy)
        self.navigation.setUniformItemSizes(True)
        self.navigation.setEditTriggers(QListView.NoEditTriggers)
        nav_layout.addWidget(self.navigation, 1)
        self.graph_page.addWidget(navigation)

        center = QWidget(self)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(4, 4, 4, 4)
        toolbar = QHBoxLayout()
        self.back_button = QPushButton(self.tr('Back'))
        self.up_button = QPushButton(self.tr('Up'))
        self.mode = QComboBox(self)
        self.mode.addItem(self.tr('Child nodes'), 'children')
        self.mode.addItem(self.tr('Incoming / outgoing'), 'neighbors')
        self.fit_button = QPushButton(self.tr('Fit'))
        self.fit_button.setToolTip(self.tr('Fit graph (F)'))
        self.arrange_button = QPushButton(self.tr('Auto layout'))
        for widget in (self.back_button, self.up_button, self.mode, self.fit_button, self.arrange_button):
            toolbar.addWidget(widget)
        toolbar.addStretch(1)
        center_layout.addLayout(toolbar)
        self.location = QLabel(self)
        self.location.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.location.setWordWrap(True)
        center_layout.addWidget(self.location)
        self.view = MotfsmGraphView(self)
        self.motion_preview = FsmMotionPreview(handler, self)
        self.tabs.insertTab(1, self.motion_preview, self.tr('Preview'))
        center_layout.addWidget(self.view, 1)
        legend = QLabel('   '.join(f'<span style="color:{color}">● {EDGE_NAMES[kind]}</span>'
                                  for kind, color in EDGE_COLORS.items()), self)
        center_layout.addWidget(legend)
        self.status = QLabel(self)
        center_layout.addWidget(self.status)
        self.graph_page.addWidget(center)
        inspector_page = QWidget(self)
        inspector_layout = QVBoxLayout(inspector_page)
        inspector_layout.setContentsMargins(8, 8, 8, 8)
        self.edit_actions_button = QPushButton(self.tr('Edit node Actions'), self)
        inspector_layout.addWidget(self.edit_actions_button)
        self.inspector = MotfsmViewer(handler, selection_only=True)
        self.inspector.tree.setColumnWidth(0, 205)
        self.inspector.tree.setColumnWidth(1, 125)
        self.inspector.setMinimumWidth(250)
        inspector_layout.addWidget(self.inspector)
        self.graph_page.addWidget(inspector_page)
        self.graph_page.setStretchFactor(1, 1)
        self.graph_page.setSizes([230, 860, 390])

        self.search.textChanged.connect(self._filter)
        self.navigation.selectionModel().currentChanged.connect(self._navigation_changed)
        self.view.node_selected.connect(self.select_node)
        self.view.node_activated.connect(self._enter_node)
        self.view.edge_selected.connect(self._select_edge)
        self.view.node_moved.connect(self._save_position)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.up_button.clicked.connect(self._up)
        self.back_button.clicked.connect(self._back)
        self.fit_button.clicked.connect(self.view.fit_graph)
        self.arrange_button.clicked.connect(self._arrange)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(70)
        self._refresh_timer.timeout.connect(self._refresh_graph)
        handler.modified_changed.connect(self.modified_changed.emit)
        self.actions = ActionEditor(handler, self.node_model, self)
        self.tabs.insertTab(0, self.actions, self.tr('Actions'))
        self.tabs.setCurrentWidget(self.actions)
        self.actions.node_selected.connect(self.select_node)
        self.actions.node_activated.connect(self._show_node)
        self.actions.action_selected.connect(self._action_selected)
        self.actions.preview_requested.connect(self._show_preview)
        self.actions.add_reference_requested.connect(self.fields._add_action)
        self.edit_actions_button.clicked.connect(self._edit_node_actions)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.motion_preview.actions.currentIndexChanged.connect(self._preview_action_changed)
        handler.editor_document.changed.connect(self._document_changed)
        self._refresh_graph()
        self._action_selected(self.actions.selected_key)

    def _document_changed(self, change):
        if change.topology_changed:
            self._refresh_timer.start()
        elif self.model is not None:
            self.model.document = self.handler.motfsm
            self.model.nodes = self.handler.motfsm.bhvt.nodes
            if change.node_index in self.view.node_items:
                self.view.node_items[change.node_index].update()
        if self.tabs.currentWidget() is self.motion_preview:
            self._sync_preview()

    def _edit_node_actions(self):
        self.actions.set_node(self.selected)
        self.tabs.setCurrentWidget(self.actions)

    def _show_node(self, index):
        self.navigate(index)
        self.tabs.setCurrentWidget(self.graph_page)

    def _show_preview(self):
        self.tabs.setCurrentWidget(self.motion_preview)

    def _tab_changed(self, *_):
        if self.tabs.currentWidget() is self.motion_preview:
            self._sync_preview()
        elif self.tabs.currentWidget() is self.graph_page and self.selected not in self.view.node_items:
            self.navigate(self.selected)

    def _action_selected(self, key):
        if key is not None:
            users = self.handler.editor_document.users(key)
            if users:
                node = next((r.node_index for r in users if r.node_index == self.selected), users[0].node_index)
                self.selected = node
                self._selected_edge = None
                self._select_navigation(node)
                self.inspector.show_node(node)
                with QSignalBlocker(self.view.scene()):
                    self.view.scene().clearSelection()
                    if node in self.view.node_items:
                        self.view.node_items[node].setSelected(True)
        if self.tabs.currentWidget() is self.motion_preview:
            self._sync_preview()

    def _sync_preview(self):
        key = self.actions.selected_key
        access = self.handler.editor_document
        users = access.users(key) if key is not None else ()
        node = next((r.node_index for r in users if r.node_index == self.selected),
                    users[0].node_index if users else None)
        if key is None:
            self.motion_preview.set_node(None)
        else:
            action = access.action(key)
            self.motion_preview.set_action(action.id_hash, action.ex_id, node)

    def _preview_action_changed(self, *_):
        motion = self.motion_preview.actions.currentData()
        if motion is not None:
            access = self.handler.editor_document
            action = next(a for a in access.actions() if (a.id_hash, a.ex_id) == (motion.action_id, motion.ex_id))
            self.actions.select_action(action.key)

    @property
    def modified(self):
        return self.handler.modified

    @modified.setter
    def modified(self, value):
        self.handler.modified = value

    def _refresh_graph(self):
        first = self.model is None
        transform = self.view.transform()
        center = self.view.mapToScene(self.view.viewport().rect().center())
        try:
            model = MotfsmGraph(self.handler.motfsm)
        except ValueError as exc:
            self.model = None
            self.view.clear_graph(str(exc))
            self.status.setText(str(exc))
            return
        self.model = model
        with QSignalBlocker(self.navigation.selectionModel()):
            self.node_model.clear()
            for index, node in enumerate(model.nodes):
                item = QStandardItem(f'[{index}] {node.name}')
                item.setData(index, Qt.UserRole)
                item.setData(f'{index} {node.name} {model.path(index)} {model.identity(index)} 0x{node.id_hash:08X} {node.id_hash}', Qt.UserRole+1)
                item.setToolTip(f'{model.path(index)}\n{model.identity(index)}')
                self.node_model.appendRow(item)
        self._filter(self.search.text())
        if not model.nodes:
            self.focus = self.selected = None
            self.view.clear_graph(self.tr('No nodes'))
            self.status.setText(self.tr('No nodes'))
            return
        if self.focus is None or self.focus >= len(model.nodes):
            self.focus = model.root()
            if self.focus is None:
                self.focus = 0
        if self.selected is None or self.selected >= len(model.nodes):
            self.selected = self.focus
        self._draw_graph()
        if first:
            self._select_navigation(self.focus)
            self.inspector.show_node(self.focus)
        else:
            self.view.setTransform(transform)
            self.view.centerOn(center)
            self._select_navigation(self.selected)
            with QSignalBlocker(self.view.scene()):
                self.view.scene().clearSelection()
                edge = next((item for item in self.view.edge_items if item.edge.key == self._selected_edge), None)
                item = edge or self.view.node_items.get(self.selected)
                if item is not None:
                    item.setSelected(True)
            if self.tabs.currentWidget() is self.motion_preview:
                self._sync_preview()

    def _scope_key(self):
        return self.mode.currentData()+':'+self.model.identity(self.focus)

    def _draw_graph(self):
        count, edges = self.view.set_graph(self.model, self.focus, self.mode.currentData(), self.layouts.get(self._scope_key(), {}))
        self._display_mode = self.mode.currentData()
        self.location.setText(self.model.path(self.focus))
        unresolved = sum(len(issues) for issues in self.model.issues.values())
        self.status.setText(self.tr('{nodes} nodes · {edges} links · {unresolved} unresolved references in file').format(
            nodes=count, edges=edges, unresolved=unresolved))
        self.up_button.setEnabled(self.model.parents[self.focus] is not None)
        self.back_button.setEnabled(bool(self.history))

    def navigate(self, index, *, remember=True):
        if self.model is None or not 0 <= index < len(self.model.nodes):
            return
        if remember and self.focus is not None and self._display_mode is not None and (self.focus != index or self._display_mode != self.mode.currentData()):
            self.history.append((self.focus, self._display_mode))
        self.focus = self.selected = index
        self._draw_graph()
        self.select_node(index)

    def select_node(self, index):
        self._selected_edge = None
        self.selected = index
        self._select_navigation(index)
        self.inspector.show_node(index)
        self.actions.set_node(index)
        with QSignalBlocker(self.view.scene()):
            self.view.scene().clearSelection()
            if index in self.view.node_items:
                self.view.node_items[index].setSelected(True)
        if self.tabs.currentWidget() is self.motion_preview:
            self._sync_preview()

    def _select_edge(self, edge):
        self._selected_edge = edge.key
        self.selected = edge.source
        self._select_navigation(edge.source)
        self.inspector.show_edge(edge)
        self.actions.set_node(edge.source)

    def _select_navigation(self, index):
        mapped = self.proxy.mapFromSource(self.node_model.index(index, 0))
        with QSignalBlocker(self.navigation.selectionModel()):
            self.navigation.setCurrentIndex(mapped)
        if mapped.isValid():
            self.navigation.scrollTo(mapped)

    def _navigation_changed(self, current, previous):
        if current.isValid():
            self.navigate(current.data(Qt.UserRole))

    def _filter(self, text):
        with QSignalBlocker(self.navigation.selectionModel()):
            self.proxy.setFilterFixedString(text)
        self.node_count.setText(self.tr('{shown} / {total} nodes').format(
            shown=self.proxy.rowCount(), total=self.node_model.rowCount()))

    def _enter_node(self, index):
        with QSignalBlocker(self.mode):
            self.mode.setCurrentIndex(0 if self.model.children(index) else 1)
        self.navigate(index)

    def _mode_changed(self):
        if self.model is not None and self.selected is not None:
            self.navigate(self.selected)

    def _up(self):
        parent = self.model.parents[self.focus]
        if parent is not None:
            with QSignalBlocker(self.mode):
                self.mode.setCurrentIndex(0)
            self.navigate(parent)

    def _back(self):
        while self.history:
            index, mode = self.history.pop()
            if index < len(self.model.nodes):
                with QSignalBlocker(self.mode):
                    self.mode.setCurrentIndex(self.mode.findData(mode))
                self.navigate(index, remember=False)
                return

    def _save_position(self, index, x, y):
        self.layouts.setdefault(self._scope_key(), {})[self.model.identity(index)] = [x, y]

    def _arrange(self):
        if self.model is not None and self.focus is not None:
            self.layouts.pop(self._scope_key(), None)
            self._draw_graph()

    def cleanup(self):
        self._refresh_timer.stop()
        self.motion_preview.cleanup()

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)
