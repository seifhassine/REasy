from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QStyle,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from file_handlers.rsz.scn_scene_loader import ScnSceneLoader, ScnSceneSource, scn_identity_keys
from file_handlers.rsz.scn_scene_preview import ScnScenePreviewWidget
from file_handlers.rsz.rsz_handler import RszHandler


def scn_source_from_tab(tab) -> ScnSceneSource | None:
    handler = getattr(tab, "handler", None)
    rsz_file = getattr(handler, "rsz_file", None)
    if not isinstance(handler, RszHandler) or not getattr(rsz_file, "is_scn", False):
        return None
    path = str(getattr(tab, "pak_source_path", None) or "")
    if not path:
        return None
    label = Path(path.replace("\\", "/")).name or "SCN"
    return ScnSceneSource(path=path, handler=handler, label=label)


class ScnSceneTab:
    skip_detached_menus = suppress_general_shortcuts = True

    def __init__(self, app, title: str):
        self.app = app
        self.title = title
        self.parent_notebook = None
        self.filename = None
        self.handler = None
        self.viewer = None
        self.tree = None
        self.modified = False
        self.pak_source_path = None
        self.sources: list[ScnSceneSource] = []
        self._owned_scn_paths: set[str] = set()
        self._hidden_renderables: set[str] = set()
        self._scn_icon = self._gameobject_icon = self._remove_icon = QIcon()
        self._eye_icon = self._eye_off_icon = QIcon()

        self.notebook_widget = QWidget()
        self.notebook_widget.parent_tab = self
        self._scn_icon = self.notebook_widget.style().standardIcon(QStyle.SP_DirIcon)
        self._gameobject_icon = self._make_gameobject_icon()
        self._remove_icon = self._make_remove_icon()
        self._eye_icon = self._make_eye_icon(False)
        self._eye_off_icon = self._make_eye_icon(True)
        layout = QVBoxLayout(self.notebook_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.preview = ScnScenePreviewWidget(
            self.notebook_widget,
            sources_getter=lambda: self.sources,
            graphs_changed_callback=self._on_graphs_changed,
        )
        layout.addWidget(self.preview, 1)

        panel = QFrame(self.preview.preview)
        panel.setObjectName("sceneInspector")
        panel.viewport_anchor = "right"
        panel.setMinimumSize(280, 44)
        panel.setMaximumSize(760, 900)
        panel.resize(380, 620)
        panel.setStyleSheet("""
            QFrame#sceneInspector { background:rgba(9,13,18,218); border:1px solid rgba(90,108,126,180); border-radius:8px; color:#dce3ea; }
            QWidget#sceneInspectorBody { background-color:transparent; border:0; }
            QLabel { color:#dce3ea; background-color:transparent; }
            QLabel#sceneStatus { color:#9fb0bf; background-color:transparent; font-size:10px; }
            QLabel#overlayResizeGrip { color:#5d6f80; background-color:transparent; font-size:10px; }
            QTreeWidget { background:rgba(8,12,16,185); border:1px solid #2d3640; outline:0; padding:2px; }
            QTreeWidget::item { padding:3px 4px; }
            QTreeWidget::item:selected { background:#276f75; }
            QToolButton { background:#1a232c; border:1px solid #3a4652; padding:3px; border-radius:4px; }
            QToolButton:hover { background:#253342; border-color:#4eb4a6; }
            QToolButton:disabled { color:#69737d; background:#121820; }
        """)
        self.source_panel = panel

        side = QVBoxLayout(panel)
        side.setContentsMargins(10, 10, 10, 10)
        side.setSpacing(6)

        header = QHBoxLayout()
        self.source_fold_button = self._tool_button("v", "Fold panel")
        self.source_label = QLabel(panel)
        self.menu_button = self._tool_button("≡", "Scene menu", self._show_scene_button_menu)
        self.add_button = self._tool_button(QStyle.SP_FileDialogNewFolder, "Add open SCN", lambda: self.app.scenes.add_to_scene(self))
        self.remove_button = self._tool_button(self._remove_icon, "Remove selected from scene", self.remove_selected_sources)
        self.rename_button = self._tool_button(QStyle.SP_FileDialogDetailedView, "Rename scene", lambda: self.app.scenes.rename_scene(self))
        self.close_button = self._tool_button(QStyle.SP_DialogCloseButton, "Close scene", lambda: self.app.scenes.close_scene(self))
        header.addWidget(self.source_fold_button)
        header.addWidget(self.source_label, 1)
        for button in (self.menu_button, self.add_button, self.preview.refresh_button, self.remove_button, self.rename_button, self.close_button):
            header.addWidget(button)
        side.addLayout(header)
        self.source_body = QWidget(panel)
        self.source_body.setObjectName("sceneInspectorBody")
        self.source_body.setAttribute(Qt.WA_StyledBackground, True)
        body = QVBoxLayout(self.source_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        side.addWidget(self.source_body, 1)
        self.preview.status_label.setObjectName("sceneStatus")
        self.preview.status_label.setAttribute(Qt.WA_StyledBackground, False)
        body.addWidget(self.preview.status_label)

        self.source_tree = QTreeWidget(panel)
        self.source_tree.setColumnCount(2)
        self.source_tree.setHeaderHidden(True)
        self.source_tree.header().setStretchLastSection(False)
        self.source_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.source_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.source_tree.setColumnWidth(1, 22)
        self.source_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.source_tree.customContextMenuRequested.connect(self._show_source_menu)
        self.source_tree.itemSelectionChanged.connect(self._refresh_controls)
        self.source_tree.itemClicked.connect(self._toggle_visibility_item)
        body.addWidget(self.source_tree, 1)
        self.warning_label = QLabel(panel)
        self.warning_label.setStyleSheet("color:#e6c84f; background-color:transparent;")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        body.addWidget(self.warning_label)
        self.preview.diagnostics.setMaximumHeight(88)
        body.addWidget(self.preview.diagnostics)
        self.shortcut_note = QLabel("Main REasy shortcuts are disabled in Scene tabs.", panel)
        self.shortcut_note.setStyleSheet("color:#7f8b96; background-color:transparent; font-size:10px;")
        self.shortcut_note.setAttribute(Qt.WA_StyledBackground, False)
        self.shortcut_note.setWordWrap(True)
        body.addWidget(self.shortcut_note)
        self.update_source_summary()
        self._refresh_controls()
        self.preview.preview.setup_viewport_overlay(panel, self.source_body, self.source_fold_button)
        self.preview.preview.place_viewport_overlays()

    def _tool_button(self, icon: QStyle.StandardPixmap | QIcon | str, tip: str, slot=None) -> QToolButton:
        button = QToolButton(self.notebook_widget)
        if isinstance(icon, str):
            button.setText(icon)
        else:
            button.setIcon(icon if isinstance(icon, QIcon) else self.notebook_widget.style().standardIcon(icon))
        button.setToolTip(tip)
        button.setFixedSize(26, 26)
        if slot:
            button.clicked.connect(slot)
        return button

    @staticmethod
    def _make_gameobject_icon() -> QIcon:
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#dce3ea"), 1.2))
        painter.setBrush(QColor("#222a33"))
        painter.drawRoundedRect(2, 5, 12, 7, 3, 3)
        painter.drawEllipse(4, 7, 2, 2)
        painter.drawEllipse(10, 7, 2, 2)
        painter.end()
        return QIcon(pixmap)

    @staticmethod
    def _make_remove_icon() -> QIcon:
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#dce3ea"), 1.4))
        painter.drawEllipse(3, 3, 10, 10)
        painter.drawLine(5, 8, 11, 8)
        painter.end()
        return QIcon(pixmap)

    @staticmethod
    def _make_eye_icon(closed: bool) -> QIcon:
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#dce3ea"), 1.2))
        painter.drawEllipse(3, 5, 10, 6)
        if closed:
            painter.drawLine(3, 12, 13, 4)
        else:
            painter.setBrush(QColor("#dce3ea"))
            painter.drawEllipse(7, 7, 2, 2)
        painter.end()
        return QIcon(pixmap)

    def add_source(self, source: ScnSceneSource, owned_keys: set[str] | None = None) -> bool:
        if owned_keys is None:
            owned_keys = self.app.scenes.source_identity_keys(source)
        root_keys = scn_identity_keys(source.path)
        if any(scn_identity_keys(existing.path) & root_keys for existing in self.sources):
            return False
        overlap = (self.owned_scn_paths() & owned_keys) - root_keys
        self.sources.append(source)
        self._owned_scn_paths.update(owned_keys)
        self._refresh_warning(bool(overlap))
        self.update_source_summary()
        self.preview.add_source(source)
        return True

    def remove_selected_sources(self) -> None:
        rows = sorted({row for row in (item.data(0, Qt.UserRole) for item in self.source_tree.selectedItems()) if isinstance(row, int)}, reverse=True)
        if not rows:
            return
        removed_keys = {key for item in self.source_tree.selectedItems() for key in (item.data(1, Qt.UserRole) or ())}
        self._hidden_renderables.difference_update(removed_keys)
        for row in rows:
            del self.sources[row]
        self.warning_label.hide()
        self.preview.remove_sources(set(rows))

    def owned_scn_paths(self) -> set[str]:
        return set(self._owned_scn_paths)

    def rebuild_owned_scn_paths(self, graphs=None) -> None:
        self._owned_scn_paths = self.app.scenes.sources_identity_keys(self.sources, graphs)

    def update_source_summary(self) -> None:
        count = len(self.sources)
        self.source_label.setText(f"{self.title} | {count} SCN source{'s' if count != 1 else ''}")
        self.source_tree.clear()
        if not self.sources:
            self.source_tree.addTopLevelItem(QTreeWidgetItem(["Empty scene", ""]))
            self._refresh_controls()
            return
        graphs = self.preview.graphs if len(self.preview.graphs) == len(self.sources) else []
        for row, source in enumerate(self.sources):
            item = self._scn_item(source.label or source.path, row)
            self.source_tree.addTopLevelItem(item)
            if graphs:
                self._populate_graph_item(item, graphs[row])
        self.source_tree.expandToDepth(1)
        self._refresh_controls()

    def update_tab_title(self) -> None:
        if self.parent_notebook is None:
            return
        index = self.parent_notebook.indexOf(self.notebook_widget)
        if index != -1:
            self.parent_notebook.setTabText(index, self.title)

    def _on_graphs_changed(self) -> None:
        if self.app is not None:
            self.rebuild_owned_scn_paths(self.preview.graphs)
            self.update_source_summary()
            self._refresh_warning()
            self.app.scenes.refresh_actions()
            self.app.scenes.refresh_buttons()

    def cleanup(self) -> None:
        self.sources.clear()
        self._owned_scn_paths.clear()
        self.preview.cleanup()
        self.preview.deleteLater()
        self.notebook_widget.parent_tab = None
        self.notebook_widget.deleteLater()

    def on_save(self) -> bool:
        return False

    def direct_save(self) -> bool:
        return False

    def reload_file(self) -> None:
        self.preview.request_refresh()

    def open_find_dialog(self) -> None:
        return None

    def _refresh_controls(self) -> None:
        has_selection = any(isinstance(item.data(0, Qt.UserRole), int) for item in self.source_tree.selectedItems())
        self.add_button.setEnabled(bool(self.app.scenes.open_scn_sources()))
        self.remove_button.setEnabled(has_selection)

    def _refresh_warning(self, include_overlap: bool = False) -> None:
        show = include_overlap or any(d.code == "duplicate_linked_scn" for graph in self.preview.graphs for d in graph.diagnostics)
        self.warning_label.setVisible(show)
        self.warning_label.setText("Warning: linked SCNs already in this scene will be skipped." if show else "")

    def _show_source_menu(self, pos) -> None:
        menu = QMenu(self.source_tree)
        remove_act = menu.addAction("Remove from Scene", self.remove_selected_sources)
        remove_act.setEnabled(self.remove_button.isEnabled())
        menu.addSeparator()
        self.app.scenes.populate_scene_menu(menu, clear=False)
        menu.exec(self.source_tree.mapToGlobal(pos))

    def _show_scene_button_menu(self) -> None:
        menu = QMenu(self.source_panel)
        self.app.scenes.populate_scene_menu(menu)
        menu.exec(self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft()))

    def _toggle_visibility_item(self, item: QTreeWidgetItem, column: int) -> None:
        keys = set(item.data(1, Qt.UserRole) or ())
        if column != 1 or not keys:
            return
        if keys <= self._hidden_renderables:
            self._hidden_renderables.difference_update(keys)
        else:
            self._hidden_renderables.update(keys)
        self.preview.set_hidden_renderables(self._hidden_renderables)
        self._refresh_visibility_icons()

    def _refresh_visibility_icons(self) -> None:
        def refresh(item: QTreeWidgetItem) -> None:
            self._set_visibility_keys(item, item.data(1, Qt.UserRole) or ())
            for index in range(item.childCount()):
                refresh(item.child(index))
        for index in range(self.source_tree.topLevelItemCount()):
            refresh(self.source_tree.topLevelItem(index))

    def _set_visibility_keys(self, item: QTreeWidgetItem, keys) -> None:
        keys = tuple(keys)
        item.setData(1, Qt.UserRole, keys)
        item.setIcon(1, self._eye_off_icon if keys and set(keys) <= self._hidden_renderables else self._eye_icon if keys else QIcon())

    def _scn_item(self, label: str, source_row: int | None = None, keys=()) -> QTreeWidgetItem:
        item = QTreeWidgetItem([Path(label.replace("\\", "/")).name or label, ""])
        item.setIcon(0, self._scn_icon)
        item.setData(0, Qt.UserRole, source_row)
        self._set_visibility_keys(item, keys)
        return item

    def _gameobject_item(self, scene_object, keys=()) -> QTreeWidgetItem:
        label = scene_object.name or scene_object.type_name or f"GameObject {scene_object.id.local_object_id}"
        item = QTreeWidgetItem([label, ""])
        item.setIcon(0, self._gameobject_icon)
        self._set_visibility_keys(item, keys)
        return item

    def _populate_graph_item(self, root_item: QTreeWidgetItem, graph) -> None:
        children: dict[str, list] = {}
        for instance in graph.document_instances.values():
            if instance.parent_instance_id:
                children.setdefault(instance.parent_instance_id, []).append(instance)
        by_instance: dict[str, set[str]] = {}
        by_object: dict[tuple[str, object], set[str]] = {}
        for renderable in graph.renderables:
            by_instance.setdefault(renderable.document_instance_id, set()).add(renderable.key)
            by_object.setdefault((renderable.document_instance_id, renderable.source_object_id), set()).add(renderable.key)
        subtree_cache: dict[str, set[str]] = {}

        def subtree_keys(instance_id: str) -> set[str]:
            if instance_id not in subtree_cache:
                keys = set(by_instance.get(instance_id, ()))
                for child in children.get(instance_id, ()):
                    keys.update(subtree_keys(child.instance_id))
                subtree_cache[instance_id] = keys
            return subtree_cache[instance_id]

        def add_document(parent_item: QTreeWidgetItem, instance_id: str, root: bool = False) -> None:
            instance = graph.document_instances.get(instance_id)
            document = graph.documents.get(instance.document_id if instance else graph.root_document_id)
            if document is None:
                return
            item = parent_item if root else self._scn_item(document.source_path or document.document_id, keys=subtree_keys(instance_id))
            if root:
                self._set_visibility_keys(item, subtree_keys(instance_id))
            if not root:
                parent_item.addChild(item)
            for child in children.get(instance_id, []):
                add_document(item, child.instance_id)
            for scene_object in document.objects.values():
                if keys := by_object.get((instance_id, scene_object.id), ()):
                    item.addChild(self._gameobject_item(scene_object, keys))

        add_document(root_item, graph.root_instance_id, True)


class ScnSceneController:
    def __init__(self, app):
        self.app = app
        self.counter = 0
        self.loader = ScnSceneLoader()
        self._identity_cache: dict[tuple[int, str], set[str]] = {}

    def tabs(self) -> list[ScnSceneTab]:
        return [tab for tab in self.app.tabs.values() if isinstance(tab, ScnSceneTab)]

    def create_tab(self) -> ScnSceneTab:
        self.counter += 1
        tab = ScnSceneTab(self.app, f"Scene {self.counter}")
        tab.parent_notebook = self.app.notebook
        self.app.notebook.addTab(tab.notebook_widget, tab.title)
        self.app.tabs[tab.notebook_widget] = tab
        self.app.project_workspace.sessions.add_tab(tab)
        self.app.notebook.setCurrentWidget(tab.notebook_widget)
        self.refresh_actions()
        self.app._refresh_homepage()
        return tab

    def add_active_scn(self) -> None:
        source = scn_source_from_tab(self.app.get_active_tab())
        self.add_to_scene(source=source, scene_tab=None if source is not None else self.active_scene())

    def add_to_scene(self, scene_tab: ScnSceneTab | None = None, source: ScnSceneSource | None = None) -> None:
        source = source or scn_source_from_tab(self.app.get_active_tab()) or self._choose_open_source()
        if source is None:
            QMessageBox.information(self.app, "Scene", "Open an SCN from a PAK first.")
            return
        owner = self.owner_for(scn_identity_keys(source.path))
        if owner is not None and owner is not scene_tab:
            self.focus(owner)
            QMessageBox.information(self.app, "Scene", f"{source.label or source.path} is already open in {owner.title}.")
            return
        owned_keys = self.source_identity_keys(source)
        conflict = self.owner_for(owned_keys, exclude=scene_tab)
        if conflict is not None and conflict is not owner:
            self.focus(conflict)
            QMessageBox.information(self.app, "Scene", f"{source.label or source.path} is already open in {conflict.title}.")
            return
        scene_tab = scene_tab or owner or self._choose_tab()
        if scene_tab is None:
            return
        if scene_tab.add_source(source, owned_keys):
            self.focus(scene_tab)
            self.app.status_bar.showMessage(f"Added {source.label or source.path} to {scene_tab.title}", 3000)
        else:
            QMessageBox.information(self.app, "Scene", f"{source.label or source.path} is already in {scene_tab.title}.")
        self.refresh_actions()
        self.refresh_buttons()

    def rename_scene(self, scene_tab: ScnSceneTab | None = None) -> None:
        scene_tab = scene_tab or self.active_scene()
        if scene_tab is None:
            return
        title, ok = QInputDialog.getText(self.app, "Rename Scene", "Name:", text=scene_tab.title)
        if ok and title.strip():
            scene_tab.title = title.strip()
            scene_tab.update_tab_title()
            scene_tab.update_source_summary()
            self.refresh_actions()

    def close_scene(self, scene_tab: ScnSceneTab | None = None) -> None:
        scene_tab = scene_tab or self.active_scene()
        if scene_tab is not None:
            self.app._close_tab_object(scene_tab, record_history=False)

    def populate_scene_menu(self, menu: QMenu, *, clear: bool = True) -> None:
        if clear:
            menu.clear()
        active_source = scn_source_from_tab(self.app.get_active_tab())
        source_owner = self.owner_for(scn_identity_keys(active_source.path)) if active_source is not None else None
        scenes = self.tabs()
        if scenes:
            for scene in scenes:
                scene_menu = menu.addMenu(scene.title)
                if active_source is not None:
                    add_act = scene_menu.addAction("Add Active SCN", lambda _checked=False, tab=scene, source=active_source: self.add_to_scene(tab, source))
                    add_act.setEnabled(source_owner is None)
                    scene_menu.addSeparator()
                scene_menu.addAction("Go To", lambda _checked=False, tab=scene: self.focus(tab))
                scene_menu.addAction("Delete", lambda _checked=False, tab=scene: self.close_scene(tab))
        else:
            empty = menu.addAction("No Scenes")
            empty.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Create Scene", lambda _checked=False: self.create_tab())

    def populate_add_to_scene_menu(self, menu: QMenu, source: ScnSceneSource | None) -> None:
        menu.clear()
        if source is None:
            action = menu.addAction("Open an SCN from a PAK first")
            action.setEnabled(False)
            return
        scenes = self.tabs()
        for scene in scenes:
            menu.addAction(scene.title, lambda _checked=False, tab=scene, src=source: self.add_to_scene(tab, src))
        if scenes:
            menu.addSeparator()
        menu.addAction("Add to New Scene", lambda _checked=False, src=source: self.add_to_new_scene(src))

    def add_to_new_scene(self, source: ScnSceneSource) -> ScnSceneTab:
        scene = self.create_tab()
        self.add_to_scene(scene, source)
        return scene

    def sync_tab(self, tab) -> None:
        source = scn_source_from_tab(tab)
        if source is None:
            return
        keys = scn_identity_keys(source.path)
        for scene in self.tabs():
            for existing in scene.sources:
                if scn_identity_keys(existing.path) & keys:
                    existing.path, existing.handler, existing.label = source.path, source.handler, source.label
                    scene.update_source_summary()
                    scene.rebuild_owned_scn_paths()
                    scene.preview.set_stale()
        self.refresh_actions()
        self.refresh_buttons()

    def owner_for(self, keys: set[str], *, exclude: ScnSceneTab | None = None) -> ScnSceneTab | None:
        return next((scene for scene in self.tabs() if scene is not exclude and scene.owned_scn_paths() & keys), None)

    def active_scene(self) -> ScnSceneTab | None:
        tab = self.app.get_active_tab()
        return tab if isinstance(tab, ScnSceneTab) else None

    def source_identity_keys(self, source: ScnSceneSource) -> set[str]:
        key = self._identity_cache_key(source)
        if key not in self._identity_cache:
            keys = scn_identity_keys(source.path)
            try:
                graphs = self.loader.build_graphs([source])
                keys.update(ScnSceneLoader.document_identity_keys(graphs))
            except Exception:
                pass
            self._identity_cache[key] = keys
        return set(self._identity_cache[key])

    def sources_identity_keys(self, sources: list[ScnSceneSource], graphs=None) -> set[str]:
        if graphs is None:
            return set().union(*(self.source_identity_keys(source) for source in sources)) if sources else set()
        keys = set().union(*(scn_identity_keys(source.path) for source in sources)) if sources else set()
        keys.update(ScnSceneLoader.document_identity_keys(list(graphs)))
        return keys

    @staticmethod
    def _identity_cache_key(source: ScnSceneSource) -> tuple[int, str]:
        return (id(getattr(source.handler, "rsz_file", None)), str(source.path).replace("\\", "/").lower())

    def focus(self, tab) -> None:
        workspace = self.app.project_workspace
        session = workspace.sessions.session_for_tab(tab)
        if session is not None and session.key != workspace.sessions.active_key:
            if session.path:
                workspace.activate(session.path, session.game)
            else:
                workspace.sessions.activate(None)
                workspace._sync_tabs()
                self.app.current_project = self.app.current_game = None
                self.app.proj_dock.set_project(None)
                self.app.proj_dock.hide()
        index = self.app.notebook.indexOf(tab.notebook_widget)
        if index != -1:
            self.app.notebook.setCurrentIndex(index)
            return
        for window in self.app.project_workspace.sessions.windows_for([tab]):
            window.show()
            window.raise_()
            window.activateWindow()

    def refresh_actions(self) -> None:
        menu = getattr(self.app, "scene_menu", None)
        if menu is not None and menu.isVisible():
            self.populate_scene_menu(menu)

    def refresh_buttons(self) -> None:
        for tab in self.app.tabs.values():
            if isinstance(tab, ScnSceneTab):
                tab._refresh_controls()
            refresh = getattr(getattr(tab, "viewer", None), "refresh_scene_button", None)
            if callable(refresh):
                refresh()

    def mark_stale(self, handler) -> None:
        stale_id = id(getattr(handler, "rsz_file", None))
        self._identity_cache = {key: value for key, value in self._identity_cache.items() if key[0] != stale_id}
        for scene in self.tabs():
            if any(source.handler is handler for source in scene.sources):
                scene.preview.set_stale()

    def open_scn_sources(self) -> list[ScnSceneSource]:
        sources: list[ScnSceneSource] = []
        seen: set[str] = set()
        for tab in self.app.tabs.values():
            source = scn_source_from_tab(tab)
            if source is None or source.path in seen:
                continue
            seen.add(source.path)
            sources.append(source)
        return sources

    def _choose_open_source(self) -> ScnSceneSource | None:
        sources = self.open_scn_sources()
        if len(sources) <= 1:
            return sources[0] if sources else None
        labels = [source.label or source.path for source in sources]
        label, ok = QInputDialog.getItem(self.app, "Add SCN to Scene", "Open SCN:", labels, 0, False)
        return sources[labels.index(label)] if ok and label in labels else None

    def _choose_tab(self) -> ScnSceneTab | None:
        scenes = self.tabs()
        if not scenes:
            return self.create_tab()
        titles = [scene.title for scene in scenes] + ["New Scene"]
        title, ok = QInputDialog.getItem(self.app, "Add SCN to Scene", "Scene:", titles, 0, False)
        if not ok:
            return None
        return self.create_tab() if title == "New Scene" else scenes[titles.index(title)]
