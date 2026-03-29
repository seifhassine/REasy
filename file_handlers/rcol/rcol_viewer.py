from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import os
import uuid

import numpy as np
from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSizePolicy,
    QTabWidget,
    QAbstractItemView,
    QInputDialog,
    QDoubleSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QTextEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from file_handlers.rsz.rsz_handler import RszHandler
from file_handlers.rsz.rsz_object_operations import RszObjectOperations
from file_handlers.pyside.component_selector import ComponentSelectorDialog
from utils.id_manager import IdManager

from .request_set import IgnoreTag, RequestSet
from .rcol_structures import RcolGroup, RcolShape
from .shape_types import ShapeType, create_shape
from .rcol_scene import SceneAttachment, build_scene_meshes
from file_handlers.mesh.mesh_file import MeshFile
from file_handlers.mesh.mesh_handler import MeshHandler
from ui.scene.scene_preview import SceneDrawMesh, ScenePreviewWidget
from utils.resource_file_utils import get_path_prefix_for_game, resolve_resource_data


@dataclass(frozen=True)
class NavPayload:
    kind: str
    group_index: int | None = None
    shape_index: int | None = None
    request_index: int | None = None
    ignore_index: int | None = None
    auto_joint_index: int | None = None
    mirror: bool = False


@dataclass
class MeshAttachmentEntry:
    filepath: str
    attachment: SceneAttachment
    enabled: bool = True


class RcolViewer(QWidget):
    """High-level RCOL editor with relationship-first UX."""

    modified_changed = Signal(bool)
    NODE_ROLE = Qt.UserRole + 1
    TAB_ACTIONS = {
        "groups": {"add_label": "+ Group", "remove_label": "- Group", "add_fn": "_add_group", "remove_fn": "_remove_selected_group", "kind": "group"},
        "request_sets": {"add_label": "+ Request", "remove_label": "- Request", "add_fn": "_add_request_set", "remove_fn": "_remove_selected_request_set", "kind": "request_set"},
        "ignore_tags": {"add_label": "+ Tag", "remove_label": "- Tag", "add_fn": "_add_ignore_tag", "remove_fn": "_remove_selected_ignore_tag", "kind": "ignore_tag"},
        "auto_joints": {"add_label": "+ Joint", "remove_label": "- Joint", "add_fn": "_add_auto_joint", "remove_fn": "_remove_selected_auto_joint", "kind": "auto_joint"},
    }

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._modified = False
        self.nav_tabs: QTabWidget | None = None
        self.nav_trees: dict[str, QTreeWidget] = {}
        self.add_tab_btn: QPushButton | None = None
        self.remove_tab_btn: QPushButton | None = None
        self.tab_actions_row: QWidget | None = None
        self.detail_title: QLabel | None = None
        self.path_label: QLabel | None = None
        self.detail_form: QFormLayout | None = None
        self._detail_host: QWidget | None = None
        self._embedded_headless_viewer: QWidget | None = None
        self._embedded_headless_handler: RszHandler | None = None
        self._headless_status: QLabel | None = None
        self._headless_body: QVBoxLayout | None = None
        self._scene_preview: ScenePreviewWidget | None = None
        self._preview_tabs: QTabWidget | None = None
        self._preview_dock: QDockWidget | None = None
        self._dock_attach_attempted = False
        self._root_layout: QVBoxLayout | None = None
        self._preview_hint: QLabel | None = None
        self._load_mesh_button: QPushButton | None = None
        self._load_mesh_from_pak_button: QPushButton | None = None
        self._mesh_pak_path_input: QLineEdit | None = None
        self._attached_mesh_list: QListWidget | None = None
        self._attached_mesh_entries: list[MeshAttachmentEntry] = []

        self._setup_ui()
        self._rebuild_navigation()

    @property
    def modified(self) -> bool:
        return self._modified

    @modified.setter
    def modified(self, value: bool):
        if self._modified != value:
            self._modified = value
            self.modified_changed.emit(value)

    @property
    def rcol(self):
        return getattr(self.handler, "rcol", None)

    # ---------- UI ----------
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        self._root_layout = layout
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Horizontal, self)
        layout.addWidget(splitter, 3)

        left_panel = QFrame()
        left_panel.setObjectName("rcolLeftPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(6)
        self.nav_tabs = QTabWidget()
        self.nav_tabs.setDocumentMode(True)
        self.nav_tabs.currentChanged.connect(self._on_nav_tab_changed)
        left_layout.addWidget(self.nav_tabs)

        self.nav_trees = {
            "groups": self._create_nav_tree(),
            "request_sets": self._create_nav_tree(),
            "ignore_tags": self._create_nav_tree(),
            "auto_joints": self._create_nav_tree(),
        }
        self.nav_tabs.addTab(self.nav_trees["groups"], "Groups")
        self.nav_tabs.addTab(self.nav_trees["request_sets"], "Request Sets")
        if self._supports_ignore_tags():
            self.nav_tabs.addTab(self.nav_trees["ignore_tags"], "Ignore Tags")
        if self._supports_auto_joints():
            self.nav_tabs.addTab(self.nav_trees["auto_joints"], "Auto Joints")
        self.tab_actions_row = QWidget()
        action_layout = QHBoxLayout(self.tab_actions_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        self.add_tab_btn = QPushButton("+")
        self.remove_tab_btn = QPushButton("-")
        self.add_tab_btn.clicked.connect(self._on_add_for_active_tab)
        self.remove_tab_btn.clicked.connect(self._on_remove_for_active_tab)
        action_layout.addWidget(self.add_tab_btn)
        action_layout.addWidget(self.remove_tab_btn)
        left_layout.addWidget(self.tab_actions_row)

        splitter.addWidget(left_panel)

        right_panel = QFrame()
        right_panel.setObjectName("rcolRightPanel")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(12, 10, 12, 10)
        right_layout.setSpacing(8)

        self.detail_title = QLabel("Select a node")
        self.detail_title.setObjectName("rcolDetailTitle")
        self.path_label = QLabel("")
        self.path_label.setObjectName("rcolPath")
        right_layout.addWidget(self.detail_title)
        right_layout.addWidget(self.path_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._detail_host = QWidget()
        self.detail_form = QFormLayout(self._detail_host)
        self.detail_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.detail_form.setVerticalSpacing(8)
        scroll.setWidget(self._detail_host)
        right_layout.addWidget(scroll)

        splitter.addWidget(right_panel)
        splitter.setSizes([420, 860])

        self._preview_tabs = QTabWidget()
        self._preview_tabs.setDocumentMode(True)
        layout.addWidget(self._preview_tabs, 3)

        preview_tab = QFrame()
        preview_layout = QVBoxLayout(preview_tab)
        preview_layout.setContentsMargins(10, 8, 10, 8)
        preview_layout.setSpacing(4)
        self._preview_hint = QLabel("Select a shape or request set to highlight it.")
        self._preview_hint.setObjectName("rcolPath")
        preview_layout.addWidget(self._preview_hint)
        mesh_actions_row = QHBoxLayout()
        self._load_mesh_button = QPushButton("Load Mesh…")
        self._load_mesh_button.clicked.connect(self._on_load_mesh_clicked)
        mesh_actions_row.addWidget(self._load_mesh_button)
        self._load_mesh_from_pak_button = QPushButton("Load Mesh from PAK")
        self._load_mesh_from_pak_button.clicked.connect(self._on_load_mesh_from_pak_clicked)
        mesh_actions_row.addWidget(self._load_mesh_from_pak_button)
        self._mesh_pak_path_input = QLineEdit(preview_tab)
        self._mesh_pak_path_input.setPlaceholderText(
            "Example: natives/stm/_chainsaw/character/ch/cha0/cha000/10/cha000_10.mesh"
        )
        self._mesh_pak_path_input.setClearButtonEnabled(True)
        mesh_actions_row.addWidget(self._mesh_pak_path_input, 1)
        preview_layout.addLayout(mesh_actions_row)
        self._attached_mesh_list = QListWidget(preview_tab)
        self._attached_mesh_list.itemChanged.connect(self._on_attached_mesh_item_changed)
        self._attached_mesh_list.setMaximumHeight(110)
        preview_layout.addWidget(self._attached_mesh_list)
        self._scene_preview = ScenePreviewWidget(preview_tab)
        self._scene_preview.setMinimumHeight(300)
        preview_layout.addWidget(self._scene_preview, 1)
        self._preview_tabs.addTab(preview_tab, "3D")

        headless_tab = QFrame()
        headless_panel = QVBoxLayout(headless_tab)
        headless_panel.setContentsMargins(10, 8, 10, 8)
        headless_panel.setSpacing(4)
        self._headless_status = QLabel("")
        self._headless_status.setObjectName("rcolPath")
        headless_panel.addWidget(self._headless_status)
        headless_body_host = QWidget()
        self._headless_body = QVBoxLayout(headless_body_host)
        self._headless_body.setContentsMargins(0, 0, 0, 0)
        self._headless_body.setSpacing(0)
        headless_panel.addWidget(headless_body_host, 1)
        self._preview_tabs.addTab(headless_tab, "RSZ")

        self.setStyleSheet(
            """
            QFrame#rcolLeftPanel, QFrame#rcolRightPanel { border: 1px solid rgba(127,127,127,0.22); border-radius: 8px; }
            QLabel#rcolDetailTitle { font-size: 15px; font-weight: 650; }
            QLabel#rcolHeadlessTitle { font-size: 13px; font-weight: 600; }
            QLabel#rcolPath { color: palette(mid); }
            """
        )

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_preview_dock()

    def _ensure_preview_dock(self):
        if self._dock_attach_attempted or self._preview_tabs is None:
            return
        self._dock_attach_attempted = True

        host_window = self.window()
        if not isinstance(host_window, QMainWindow):
            return

        for stale_dock in host_window.findChildren(QDockWidget, "rcolPreviewDock"):
            if stale_dock is self._preview_dock:
                continue
            stale_dock.deleteLater()

        if self._root_layout is not None:
            self._root_layout.removeWidget(self._preview_tabs)

        dock = QDockWidget("RCOL 3D/RSZ", host_window)
        dock.setObjectName("rcolPreviewDock")
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        dock.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        dock.topLevelChanged.connect(self._on_preview_dock_top_level_changed)
        dock.setWidget(self._preview_tabs)
        host_window.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self._preview_dock = dock
        self._on_preview_dock_top_level_changed(dock.isFloating())

    def cleanup(self):
        if self._preview_dock is not None:
            self._preview_dock.deleteLater()
            self._preview_dock = None

    def _on_preview_dock_top_level_changed(self, is_floating: bool):
        if self._preview_dock is None:
            return
        if is_floating:
            self._preview_dock.setWindowTitle("RCOL 3D/RSZ")
        else:
            self._preview_dock.setWindowTitle("")

    # ---------- Navigation ----------
    def _create_nav_tree(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setSelectionMode(QAbstractItemView.SingleSelection)
        tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        return tree

    def _rebuild_navigation(self, payload_to_select: NavPayload | None = None):
        for tree in self.nav_trees.values():
            tree.clear()
        rcol = self.rcol
        if not rcol:
            return

        self._populate_groups_section(self.nav_trees["groups"])
        self._populate_request_sets_section(self.nav_trees["request_sets"])
        if self._supports_ignore_tags():
            self._populate_ignore_tags_section(self.nav_trees["ignore_tags"])
        if self._supports_auto_joints():
            self._populate_auto_joints_section(self.nav_trees["auto_joints"])

        self._refresh_headless_region()
        self._refresh_scene_preview(payload_to_select)

        fallback = payload_to_select or NavPayload(kind="groups")
        self._select_payload(fallback)
        self._sync_tab_action_state()

    def _populate_groups_section(self, tree: QTreeWidget):
        supports_mirror_shapes = self._supports_mirror_shapes()
        groups_root = self._add_item(tree, f"Groups ({len(self.rcol.groups)})", NavPayload(kind="groups"))
        for g_idx, group in enumerate(self.rcol.groups):
            group_name = group.info.name or f"Group {g_idx + 1}"
            group_item = self._add_item(tree, f"[{g_idx}] {group_name}", NavPayload(kind="group", group_index=g_idx), parent=groups_root)
            regular_root = self._add_item(
                tree,
                f"Regular Shapes ({len(group.shapes)})",
                NavPayload(kind="group_shapes", group_index=g_idx, mirror=False),
                parent=group_item,
            )
            for s_idx, shape in enumerate(group.shapes):
                self._add_item(
                    tree,
                    f"• [{s_idx}] {shape.info.name or f'Shape {s_idx + 1}'}",
                    NavPayload(kind="shape", group_index=g_idx, shape_index=s_idx, mirror=False),
                    parent=regular_root,
                )
            if supports_mirror_shapes:
                mirror_shapes = group.extra_shapes or []
                mirror_root = self._add_item(
                    tree,
                    f"Mirror Shapes ({len(mirror_shapes)})",
                    NavPayload(kind="group_shapes", group_index=g_idx, mirror=True),
                    parent=group_item,
                )
                for s_idx, shape in enumerate(mirror_shapes):
                    self._add_item(
                        tree,
                        f"◦ [{s_idx}] {shape.info.name or f'Mirror Shape {s_idx + 1}'}",
                        NavPayload(kind="shape", group_index=g_idx, shape_index=s_idx, mirror=True),
                        parent=mirror_root,
                    )
        tree.expandToDepth(2)

    def _populate_request_sets_section(self, tree: QTreeWidget):
        root = self._add_item(tree, f"Request Sets ({len(self.rcol.request_sets)})", NavPayload(kind="request_sets"))
        for r_idx, req in enumerate(self.rcol.request_sets):
            self._add_item(tree, self._request_set_label(r_idx), NavPayload(kind="request_set", request_index=r_idx), parent=root)
        tree.expandToDepth(0)

    def _populate_ignore_tags_section(self, tree: QTreeWidget):
        ignore_tags = self.rcol.ignore_tags or []
        root = self._add_item(tree, f"Ignore Tags ({len(ignore_tags)})", NavPayload(kind="ignore_tags"))
        for idx, tag in enumerate(ignore_tags):
            self._add_item(tree, f"[{idx}] {tag.tag or '(empty)'}", NavPayload(kind="ignore_tag", ignore_index=idx), parent=root)
        tree.expandToDepth(0)

    def _populate_auto_joints_section(self, tree: QTreeWidget):
        joints = self.rcol.auto_generate_joint_descs or []
        root = self._add_item(tree, f"Auto Joints ({len(joints)})", NavPayload(kind="auto_joints"))
        for idx, name in enumerate(joints):
            self._add_item(tree, f"[{idx}] {name or '(empty)'}", NavPayload(kind="auto_joint", auto_joint_index=idx), parent=root)
        tree.expandToDepth(0)

    def _add_item(self, tree: QTreeWidget, text: str, payload: NavPayload, parent: QTreeWidgetItem | None = None):
        item = QTreeWidgetItem([text])
        item.setData(0, self.NODE_ROLE, payload)
        if parent is None:
            tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _iter_items(self):
        for tree in self.nav_trees.values():
            stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
            while stack:
                item = stack.pop()
                if item is None:
                    continue
                yield tree, item
                for i in range(item.childCount() - 1, -1, -1):
                    stack.append(item.child(i))

    def _select_payload(self, payload: NavPayload):
        for tree, item in self._iter_items():
            if item.data(0, self.NODE_ROLE) == payload:
                self.nav_tabs.setCurrentWidget(tree)
                tree.setCurrentItem(item)
                return

    def _find_item_by_payload(self, payload: NavPayload) -> tuple[QTreeWidget, QTreeWidgetItem] | None:
        for tree, item in self._iter_items():
            if item.data(0, self.NODE_ROLE) == payload:
                return tree, item
        return None

    # ---------- Selection and rendering ----------
    def _on_tree_selection_changed(self):
        tree = self.sender()
        selected = tree.selectedItems() if isinstance(tree, QTreeWidget) else []
        if not selected:
            return
        item = selected[0]
        payload = item.data(0, self.NODE_ROLE)
        if not self._is_payload_valid(payload):
            raise IndexError(f"Invalid tree payload selection: {payload}")
        self.path_label.setText(self._build_path(item))
        self._render_payload(payload)
        self._sync_tab_action_state()

    def _on_nav_tab_changed(self, _index: int):
        self._sync_tab_action_state()

    def _active_tab_key(self) -> str | None:
        if self.nav_tabs is None:
            return None
        widget = self.nav_tabs.currentWidget()
        for key, tree in self.nav_trees.items():
            if tree is widget:
                return key
        return None

    def _sync_tab_action_state(self):
        if self.add_tab_btn is None or self.remove_tab_btn is None or self.nav_tabs is None:
            return
        tab = self._active_tab_key()
        cfg = self.TAB_ACTIONS.get(tab)
        if not cfg:
            return
        self.add_tab_btn.setText(cfg["add_label"])
        self.remove_tab_btn.setText(cfg["remove_label"])
        self.add_tab_btn.setEnabled(True)
        selected_index = self._selected_index_for_kind(cfg["kind"])
        if tab == "groups":
            self.remove_tab_btn.setEnabled(selected_index is not None and len(self.rcol.groups) > 1)
        else:
            self.remove_tab_btn.setEnabled(selected_index is not None)

    def _on_add_for_active_tab(self):
        tab = self._active_tab_key()
        cfg = self.TAB_ACTIONS.get(tab)
        if cfg:
            getattr(self, cfg["add_fn"])()

    def _on_remove_for_active_tab(self):
        tab = self._active_tab_key()
        cfg = self.TAB_ACTIONS.get(tab)
        if cfg:
            getattr(self, cfg["remove_fn"])()

    def _request_set_label(self, request_index: int) -> str:
        req = self.rcol.request_sets[request_index]
        name = req.info.name or f"Request Set {request_index + 1}"
        key = req.info.key_name or "(no key)"
        group_name = "(unassigned)"
        if 0 <= req.info.group_index < len(self.rcol.groups):
            group_name = self.rcol.groups[req.info.group_index].info.name or f"Group {req.info.group_index}"
        return f"[{request_index}] {name} · Group: {group_name} · key: {key}"

    def _selected_index_for_kind(self, kind: str) -> int | None:
        tab = self._active_tab_key()
        tree = self.nav_trees.get(tab) if tab else None
        if not tree:
            return None
        selected = tree.selectedItems()
        if not selected:
            return None
        payload = selected[0].data(0, self.NODE_ROLE)
        if not payload or payload.kind != kind:
            return None
        attr_map = {
            "group": "group_index",
            "request_set": "request_index",
            "ignore_tag": "ignore_index",
            "auto_joint": "auto_joint_index",
        }
        return getattr(payload, attr_map[kind], None)

    def _build_path(self, item: QTreeWidgetItem) -> str:
        parts = []
        current = item
        while current is not None:
            parts.append(current.text(0))
            current = current.parent()
        if len(parts) <= 1:
            return parts[0] if parts else ""
        return " › ".join(reversed(parts))

    def _current_payload(self) -> NavPayload | None:
        tab = self._active_tab_key()
        tree = self.nav_trees.get(tab) if tab else None
        if not tree:
            return None
        selected = tree.selectedItems()
        if not selected:
            return None
        return selected[0].data(0, self.NODE_ROLE)

    def _preview_key_for_shape(self, group_index: int, shape_index: int, mirror: bool) -> str:
        return f"g{group_index}:{'m' if mirror else 'r'}:{shape_index}"

    def _highlight_keys_for_payload(self, payload: NavPayload | None) -> set[str]:
        if payload is None:
            return set()
        if payload.kind == "shape" and payload.group_index is not None and payload.shape_index is not None:
            return {self._preview_key_for_shape(payload.group_index, payload.shape_index, payload.mirror)}
        if payload.kind == "request_set" and payload.request_index is not None:
            if not (0 <= payload.request_index < len(self.rcol.request_sets)):
                return set()
            req = self.rcol.request_sets[payload.request_index]
            group_index = int(getattr(req.info, "group_index", -1))
            if not (0 <= group_index < len(self.rcol.groups)):
                return set()
            return {
                self._preview_key_for_shape(group_index, shape_index, False)
                for shape_index in range(len(self.rcol.groups[group_index].shapes))
            }
        return set()

    def _refresh_scene_preview(self, payload: NavPayload | None = None):
        if self._scene_preview is None or not self.rcol:
            return
        highlighted = self._highlight_keys_for_payload(payload)
        self._scene_preview.set_scene(
            build_scene_meshes(self.rcol, attachments=self._active_attachments()),
            highlighted_keys=highlighted,
        )
        if self._preview_hint is not None:
            if payload and payload.kind == "request_set":
                self._preview_hint.setText("Request set selected: all linked group shapes are highlighted.")
            elif payload and payload.kind == "shape":
                occurrence_count = 0
                if payload.group_index is not None and not payload.mirror:
                    occurrence_count = sum(
                        1
                        for request_set in self.rcol.request_sets
                        if int(getattr(request_set.info, "group_index", -1)) == payload.group_index
                    )
                if occurrence_count > 0:
                    self._preview_hint.setText(
                        f"Shape selected: highlighted across {occurrence_count} request-set occurrence(s)."
                    )
                else:
                    self._preview_hint.setText("Shape selected: no request-set occurrences found.")
            else:
                self._preview_hint.setText("Select a shape or request set to highlight it.")

    def _on_load_mesh_clicked(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Select Mesh",
            "",
            "Mesh Files (*.mesh.*);;All Files (*)",
        )
        if not filepath:
            return

        try:
            mesh = self._load_mesh_via_handler(filepath)
            attachment = self._build_attachment_scene(mesh, attachment_key=f"attached_mesh_{len(self._attached_mesh_entries)}")
            self._attached_mesh_entries.append(MeshAttachmentEntry(filepath=filepath, attachment=attachment, enabled=True))
            self._rebuild_attached_mesh_list()
            self._refresh_scene_preview(self._current_payload())
        except Exception as exc:
            QMessageBox.warning(self, "Mesh Load Failed", f"Unable to load mesh:\n{exc}")

    def _on_load_mesh_from_pak_clicked(self):
        app = getattr(self.handler, "app", None)
        proj = getattr(app, "proj_dock", None) if app is not None else None
        if proj is None or not getattr(proj, "project_dir", None):
            QMessageBox.information(
                self,
                "Project Mode Required",
                'You are not in project mode. Please open a project ("File" > "New Mod/Open Project").',
            )
            return

        resource_path = (self._mesh_pak_path_input.text() if self._mesh_pak_path_input else "").strip()
        if not resource_path:
            QMessageBox.information(self, "Load Mesh from PAK", "Please enter a mesh path first.")
            return

        path_prefix = get_path_prefix_for_game(str(getattr(app, "current_game", "") or ""))
        resolved = resolve_resource_data(
            resource_path,
            getattr(proj, "project_dir", None),
            getattr(proj, "unpacked_dir", None),
            path_prefix,
            getattr(proj, "_pak_cached_reader", None),
            getattr(proj, "_pak_selected_paks", None),
            self,
        )
        if not resolved:
            QMessageBox.warning(self, "Mesh Load Failed", f"Unable to resolve mesh path:\n{resource_path}")
            return

        resolved_path, mesh_data = resolved
        try:
            mesh = self._load_mesh_via_handler(resolved_path, mesh_data)
            attachment = self._build_attachment_scene(mesh, attachment_key=f"attached_mesh_{len(self._attached_mesh_entries)}")
            self._attached_mesh_entries.append(MeshAttachmentEntry(filepath=resolved_path, attachment=attachment, enabled=True))
            self._rebuild_attached_mesh_list()
            self._refresh_scene_preview(self._current_payload())
        except Exception as exc:
            QMessageBox.warning(self, "Mesh Load Failed", f"Unable to load mesh:\n{exc}")

    def _rebuild_attached_mesh_list(self):
        if self._attached_mesh_list is None:
            return
        blocker = QSignalBlocker(self._attached_mesh_list)
        self._attached_mesh_list.clear()
        for entry in self._attached_mesh_entries:
            item = QListWidgetItem(os.path.basename(entry.filepath) or entry.filepath)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if entry.enabled else Qt.Unchecked)
            item.setToolTip(entry.filepath)
            self._attached_mesh_list.addItem(item)
        del blocker

    def _on_attached_mesh_item_changed(self, _item: QListWidgetItem):
        if self._attached_mesh_list is None:
            return
        for idx in range(self._attached_mesh_list.count()):
            if idx >= len(self._attached_mesh_entries):
                continue
            self._attached_mesh_entries[idx].enabled = self._attached_mesh_list.item(idx).checkState() == Qt.Checked
        self._refresh_scene_preview(self._current_payload())

    def _active_attachments(self) -> list[SceneAttachment]:
        return self._aligned_attachments([entry.attachment for entry in self._attached_mesh_entries if entry.enabled])

    def _aligned_attachments(self, attachments: list[SceneAttachment]) -> list[SceneAttachment]:
        aligned: list[SceneAttachment] = []
        aggregate_joints: dict[str, np.ndarray] = {}
        for attachment in attachments:
            transforms = attachment.joint_transforms or {}
            translation_delta = np.zeros(3, dtype=np.float32)
            overlap_count = 0
            for name, matrix in transforms.items():
                if name not in aggregate_joints:
                    continue
                translation_delta += aggregate_joints[name][:3, 3] - matrix[:3, 3]
                overlap_count += 1
            if overlap_count > 0:
                translation_delta /= float(overlap_count)

            aligned_mesh = self._offset_mesh(attachment.mesh, translation_delta)
            aligned_transforms = self._offset_joint_map(transforms, translation_delta)
            for name, matrix in aligned_transforms.items():
                aggregate_joints.setdefault(name, matrix)
            aligned.append(SceneAttachment(mesh=aligned_mesh, joint_transforms=aligned_transforms))
        return aligned

    def _offset_mesh(self, mesh: SceneDrawMesh | None, translation_delta: np.ndarray) -> SceneDrawMesh | None:
        if mesh is None:
            return None
        if np.linalg.norm(translation_delta) < 1e-8:
            return mesh
        return SceneDrawMesh(
            key=mesh.key,
            vertices=(mesh.vertices + translation_delta).astype(np.float32, copy=False),
            indices=mesh.indices,
            color=mesh.color,
            force_solid=mesh.force_solid,
            ignore_highlight_filter=mesh.ignore_highlight_filter,
        )

    def _offset_joint_map(self, transforms: dict[str, np.ndarray], translation_delta: np.ndarray) -> dict[str, np.ndarray]:
        if np.linalg.norm(translation_delta) < 1e-8:
            return {name: matrix.copy() for name, matrix in transforms.items()}
        aligned: dict[str, np.ndarray] = {}
        for name, matrix in transforms.items():
            updated = matrix.copy()
            updated[:3, 3] += translation_delta
            aligned[name] = updated
        return aligned

    def _load_mesh_via_handler(self, filepath: str, data: bytes | None = None) -> MeshFile:
        handler = MeshHandler()
        handler.filepath = filepath
        handler.app = getattr(self.handler, "app", None)
        if data is None:
            with open(filepath, "rb") as stream:
                data = stream.read()
        handler.read(data)
        mesh = getattr(handler, "mesh", None)
        if mesh is None:
            raise ValueError("Mesh handler did not produce a parsed mesh.")
        return mesh

    def _build_attachment_scene(self, mesh: MeshFile, attachment_key: str) -> SceneAttachment:
        return SceneAttachment(
            mesh=self._build_mesh_draw(mesh, attachment_key),
            joint_transforms=self._build_joint_transform_map(mesh),
        )

    def _build_mesh_draw(self, mesh: MeshFile, key: str) -> SceneDrawMesh | None:
        mesh_buffer = getattr(mesh, "mesh_buffer", None)
        if mesh_buffer is None or not getattr(mesh, "meshes", None):
            return None
        vertex_chunks: list[np.ndarray] = []
        index_chunks: list[np.ndarray] = []
        vertex_base = 0
        lod0 = mesh.meshes[0].lods[0] if mesh.meshes and mesh.meshes[0].lods else None
        if lod0 is None:
            return None

        for group in lod0.mesh_groups:
            for submesh in group.submeshes:
                payload = mesh_buffer.buffer_payloads.get(int(getattr(submesh, "buffer_index", 0)))
                if payload is None:
                    payload = mesh_buffer.buffer_payloads.get(0)
                if payload is None:
                    continue

                payload_verts = np.asarray(getattr(payload, "positions", []), dtype=np.float32).reshape(-1, 3)
                if payload_verts.size == 0:
                    continue
                payload_indices_raw = getattr(payload, "integer_faces", None)
                if payload_indices_raw is None:
                    payload_indices_raw = getattr(payload, "faces", None)
                if payload_indices_raw is None:
                    continue
                payload_indices = np.asarray(payload_indices_raw, dtype=np.uint32).reshape(-1)

                start = int(submesh.faces_index_offset)
                count = int(submesh.indices_count)
                if count <= 0:
                    continue
                local = payload_indices[start:start + count]
                if local.size < 3:
                    continue
                usable = (local.size // 3) * 3
                local = local[:usable] + np.uint32(int(submesh.verts_index_offset))
                valid = local < len(payload_verts)
                valid = valid.reshape(-1, 3).all(axis=1)
                if not np.any(valid):
                    continue
                triangle_indices = local.reshape(-1, 3)[valid].reshape(-1)

                vertex_chunks.append(payload_verts)
                index_chunks.append(triangle_indices + np.uint32(vertex_base))
                vertex_base += len(payload_verts)

        if not vertex_chunks or not index_chunks:
            return None
        verts = np.concatenate(vertex_chunks, axis=0).astype(np.float32, copy=False)
        indices = np.concatenate(index_chunks, axis=0).astype(np.uint32, copy=False)
        return SceneDrawMesh(
            key=key,
            vertices=verts,
            indices=indices,
            color=(0.35, 0.35, 0.38),
            force_solid=True,
            ignore_highlight_filter=True,
        )

    def _build_joint_transform_map(self, mesh: MeshFile) -> dict[str, np.ndarray]:
        names = list(getattr(mesh, "names", []) or [])
        bone_name_indices = list(getattr(mesh, "bone_indices", []) or [])
        if not names or not bone_name_indices:
            return {}

        raw_matrices = list(getattr(mesh, "world_matrices", None) or getattr(mesh, "local_matrices", None) or [])
        if not raw_matrices:
            return {}

        joint_transforms: dict[str, np.ndarray] = {}
        max_count = min(len(raw_matrices), len(bone_name_indices))
        for joint_index in range(max_count):
            name_index = int(bone_name_indices[joint_index])
            if not (0 <= name_index < len(names)):
                continue
            name = str(names[name_index] or "")
            if not name or name in joint_transforms:
                continue

            matrix = self._as_joint_matrix(raw_matrices[joint_index])
            if matrix is None:
                continue
            joint_transforms[name] = matrix
        return joint_transforms

    def _as_joint_matrix(self, raw_matrix) -> np.ndarray | None:
        arr = np.asarray(raw_matrix, dtype=np.float32)
        if arr.size != 16:
            return None
        row_major = arr.reshape(4, 4)
        if self._is_valid_joint_matrix(row_major):
            return row_major.copy()
        
        column_major = row_major.T
        if self._is_valid_joint_matrix(column_major):
            return column_major.copy()
        return None

    def _is_valid_joint_matrix(self, matrix: np.ndarray) -> bool:
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            return False
        
        bottom = matrix[3]
        if not (abs(float(bottom[0])) < 1e-3 and abs(float(bottom[1])) < 1e-3 and abs(float(bottom[2])) < 1e-3):
            return False
        if not abs(float(bottom[3]) - 1.0) < 1e-2:
            return False
        translation = matrix[:3, 3]
        if np.linalg.norm(translation) > 1e6:
            return False
        return True

    def _clear_detail(self):
        while self.detail_form.count():
            item = self.detail_form.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _render_payload(self, payload: NavPayload):
        if not self._is_payload_valid(payload):
            raise IndexError(f"Invalid payload render request: {payload}")
        self._clear_detail()
        handlers = {
            "groups": self._render_groups_overview,
            "group": self._render_group,
            "group_shapes": self._render_group_shapes,
            "shape": self._render_shape,
            "request_sets": self._render_request_sets_overview,
            "request_set": self._render_request_set,
            "ignore_tags": self._render_ignore_tags_overview,
            "ignore_tag": self._render_ignore_tag,
            "auto_joints": self._render_auto_joints_overview,
            "auto_joint": self._render_auto_joint,
        }
        fn = handlers.get(payload.kind)
        if not fn:
            self.detail_title.setText("Unsupported node")
            self._row_text("Info", "No editor is available for this node.")
            return
        fn(payload)
        self._refresh_scene_preview(payload)

    # ---------- Helpers ----------
    def _row_text(self, label: str, text: str):
        value_label = QLabel(text)
        value_label.setWordWrap(False)
        value_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.detail_form.addRow(label, value_label)

    def _row_multiline_text(self, label: str, lines: list[str], *, max_lines: int = 10):
        text = "\n".join(lines) if lines else "(none)"
        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        visible = max(2, min(max_lines, max(1, len(lines))))
        line_height = editor.fontMetrics().lineSpacing()
        editor.setFixedHeight((visible * line_height) + 12)
        self.detail_form.addRow(label, editor)

    def _format_guid(self, value: Any) -> str:
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
            if len(raw) == 16:
                try:
                    return str(uuid.UUID(bytes_le=raw))
                except Exception:
                    return raw.hex()
            return raw.hex()
        return str(value or "")

    def _resolve_type_registry(self):
        return getattr(self.rcol, "type_registry", None) or getattr(self.handler, "type_registry", None)

    def _resolve_instance_type_name(self, instance_id: int) -> str:
        if not self.rcol.rsz or not (0 <= instance_id < len(self.rcol.rsz.instance_infos)):
            return "(invalid)"
        inst = self.rcol.rsz.instance_infos[instance_id]
        registry = self._resolve_type_registry()
        if registry:
            type_info = registry.get_type_info(int(inst.type_id))
            if type_info and "name" in type_info:
                return type_info["name"]
        return f"Type 0x{int(inst.type_id):08X}"

    def _extract_instance_id(self, value: Any) -> int | None:
        if isinstance(value, int):
            return value
        candidate = getattr(value, "index", None) if value is not None else None
        return candidate if isinstance(candidate, int) else None

    def _get_request_set_object_entries(self, request_index: int) -> list[str]:
        if not self.rcol.rsz:
            return []
        object_table = self.rcol.rsz.object_table or []
        if not (0 <= request_index < len(self.rcol.request_sets)):
            return []

        request_set = self.rcol.request_sets[request_index]
        associated_ids = []
        main_instance_id = self._extract_instance_id(request_set.instance)
        if main_instance_id is not None:
            associated_ids.append(main_instance_id)
        for item in request_set.shape_userdata or []:
            item_id = self._extract_instance_id(item)
            if item_id is not None:
                associated_ids.append(item_id)
        if not associated_ids:
            return []
        id_set = set(associated_ids)

        rows = []
        for obj_index, instance_id in enumerate(object_table):
            if instance_id not in id_set:
                continue
            instance_id = object_table[obj_index]
            rows.append(f"[{obj_index}] -> Instance {instance_id} ({self._resolve_instance_type_name(instance_id)})")
        return rows

    def _get_shape_object_id_entries(self, payload: NavPayload) -> list[str]:
        if not self.rcol.rsz:
            return []
        if payload.group_index is None or payload.shape_index is None:
            return []
        object_table = self.rcol.rsz.object_table or []
        if payload.group_index < 0 or payload.group_index >= len(self.rcol.groups):
            return []
        group = self.rcol.groups[payload.group_index]
        if payload.mirror:
            return []
        if payload.shape_index < 0 or payload.shape_index >= len(group.shapes):
            return []

        rows = []
        shape = group.shapes[payload.shape_index]
        # Base shape object-id mapping is independent of request-set assignments.
        if self.handler.file_version < 25:
            base_index = shape.info.user_data_index
            if isinstance(base_index, int) and 0 <= base_index < len(object_table):
                base_instance_id = object_table[base_index]
                rows.append(
                    f"Base Shape -> [{base_index}] "
                    f"(Instance {base_instance_id}, {self._resolve_instance_type_name(base_instance_id)})"
                )
            elif isinstance(base_index, int) and base_index >= 0:
                rows.append(f"Base Shape -> [{base_index}] (unassigned)")

        for req_idx, request_set in enumerate(self.rcol.request_sets):
            if request_set.info.group_index != payload.group_index:
                continue
            if self.handler.file_version < 25:
                same_group_sets = [
                    rs for rs in self.rcol.request_sets
                    if rs.info.group_index == payload.group_index
                ]
                is_primary_group_request = len(same_group_sets) > 0 and same_group_sets[0] is request_set
                if is_primary_group_request:
                    object_index = shape.info.user_data_index
                else:
                    object_index = shape.info.user_data_index + request_set.info.shape_offset
            else:
                object_index = request_set.info.group_userdata_index_start + payload.shape_index
            if not isinstance(object_index, int) or object_index < 0 or object_index >= len(object_table):
                rows.append(f"RequestSet [{req_idx}] -> (unassigned)")
                continue
            instance_id = object_table[object_index]
            rows.append(
                f"RequestSet [{req_idx}] -> [{object_index}] "
                f"(Instance {instance_id}, {self._resolve_instance_type_name(instance_id)})"
            )
        return rows

    def _prompt_request_set_type(self) -> str | None:
        registry = self._resolve_type_registry()
        if not registry:
            QMessageBox.warning(self, "Missing Registry", "No type registry is loaded. Cannot add request-set userdata.")
            return None

        dialog = ComponentSelectorDialog(
            self,
            type_registry=registry,
            required_parent_name="via.physics.RequestSetColliderUserData",
            include_parent=True,
        )
        dialog.setWindowTitle("Select Instance Type")
        dialog.search_input.setPlaceholderText("Type instance type name...")
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.get_selected_component()

    def _prompt_shape_userdata_type(self) -> str | None:
        registry = self._resolve_type_registry()
        if not registry:
            QMessageBox.warning(self, "Missing Registry", "No type registry is loaded. Cannot add shape userdata.")
            return None

        dialog = ComponentSelectorDialog(
            self,
            type_registry=registry,
            required_parent_name="via.UserData",
        )
        dialog.setWindowTitle("Select Shape UserData Type")
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.get_selected_component()

    def _prompt_request_set_group_index(self) -> int | None:
        if not self.rcol.groups:
            QMessageBox.warning(self, "Add Request Set", "No groups are available. Add a group first.")
            return None

        group_labels = []
        for group_index, group in enumerate(self.rcol.groups):
            group_name = (group.info.name or "").strip() or f"Group {group_index + 1}"
            group_labels.append(f"[{group_index}] {group_name}")

        selected_label, confirmed = QInputDialog.getItem(
            self,
            "Select Request Set Group",
            "Assign this request set to group:",
            group_labels,
            0,
            False,
        )
        if not confirmed:
            return None

        return group_labels.index(selected_label)

    def _append_headless_request_userdata(self, type_name: str, group_shape_count: int) -> tuple[int, int, int]:
        if not self.rcol.rsz:
            raise ValueError("This RCOL does not contain a headless RSZ block.")

        embedded_viewer = self._embedded_headless_viewer
        if embedded_viewer is None:
            raise ValueError("Headless RSZ viewer is unavailable.")

        request_set_userdata_index, group_userdata_index_start, root_instance_id = embedded_viewer.add_headless_request_userdata(
            type_name,
            group_shape_count=group_shape_count,
        )

        try:
            self.rcol.user_data_bytes = self.rcol.rsz.build_headless()
        except Exception:
            # Keep in-memory model edits even if preview rebuild is unavailable.
            pass

        return request_set_userdata_index, group_userdata_index_start, root_instance_id

    def _next_request_id(self) -> int:
        return max((rs.info.id for rs in self.rcol.request_sets), default=-1) + 1

    def _insert_root_object_id_at(self, desired_object_index: int, appended_object_index: int):
        if not self.rcol.rsz:
            return
        object_table = self.rcol.rsz.object_table
        if object_table is None:
            return
        if not (0 <= appended_object_index < len(object_table)):
            return
        insert_index = max(0, min(desired_object_index, len(object_table) - 1))
        if insert_index == appended_object_index:
            return

        # Snapshot pre-insert effective shape-userdata starts for legacy (pre-v25)
        # request sets so shape_offset can be shifted when insertion happens before
        # their per-group userdata window.
        legacy_request_start_indices: dict[int, int] = {}
        legacy_group_base_min_indices: dict[int, int] = {}
        if self.handler.file_version < 25:
            for request_set in self.rcol.request_sets:
                group_index = int(getattr(request_set.info, "group_index", -1))
                if not (0 <= group_index < len(self.rcol.groups)):
                    continue
                group = self.rcol.groups[group_index]
                if not group.shapes:
                    continue
                base_indices = [
                    int(getattr(shape.info, "user_data_index", 0) or 0)
                    for shape in group.shapes
                ]
                if not base_indices:
                    continue
                shape_offset = int(getattr(request_set.info, "shape_offset", 0) or 0)
                legacy_group_base_min_indices[id(request_set)] = min(base_indices)
                legacy_request_start_indices[id(request_set)] = min(
                    base_index + shape_offset
                    for base_index in base_indices
                )

        inserted_value = object_table[appended_object_index]
        object_ops = self._get_headless_object_operations()
        if object_ops:
            object_ops._insert_into_object_table(insert_index, inserted_value)
            shifted_old_index = appended_object_index + (1 if insert_index <= appended_object_index else 0)
            object_ops._remove_from_object_table(shifted_old_index)
        else:
            inserted_value = object_table.pop(appended_object_index)
            object_table.insert(insert_index, inserted_value)

        # Every object-table index at/after insertion point shifts by +1.
        if self.handler.file_version < 25:
            for group in self.rcol.groups:
                for shape in group.shapes:
                    idx = getattr(shape.info, "user_data_index", -1)
                    if isinstance(idx, int) and idx >= insert_index:
                        shape.info.user_data_index = idx + 1
            for request_set in self.rcol.request_sets:
                start_idx = legacy_request_start_indices.get(id(request_set))
                base_min_idx = legacy_group_base_min_indices.get(id(request_set))
                if start_idx is None:
                    continue
                if base_min_idx is None:
                    continue
                # If group base indices are shifted by this insertion, effective request
                # windows already move with them and shape_offset must remain unchanged.
                if base_min_idx < insert_index <= start_idx:
                    current_offset = int(getattr(request_set.info, "shape_offset", 0) or 0)
                    request_set.info.shape_offset = current_offset + 1

        for request_set in self.rcol.request_sets:
            info = request_set.info
            if self.handler.file_version >= 25:
                if isinstance(info.request_set_userdata_index, int) and info.request_set_userdata_index >= insert_index:
                    info.request_set_userdata_index += 1
                if isinstance(info.group_userdata_index_start, int) and info.group_userdata_index_start >= insert_index:
                    info.group_userdata_index_start += 1

    def _remove_request_root_object_id_at(self, object_index: int):
        if not self.rcol.rsz:
            return
        object_table = self.rcol.rsz.object_table
        if object_table is None:
            return
        if not (0 <= object_index < len(object_table)):
            return

        # Snapshot pre-removal  starts for legacy (pre-v25) request sets.
        legacy_request_start_indices: dict[int, int] = {}
        legacy_group_base_min_indices: dict[int, int] = {}
        if self.handler.file_version < 25:
            for request_set in self.rcol.request_sets:
                group_index = int(getattr(request_set.info, "group_index", -1))
                if not (0 <= group_index < len(self.rcol.groups)):
                    continue
                group = self.rcol.groups[group_index]
                if not group.shapes:
                    continue
                base_indices = [
                    int(getattr(shape.info, "user_data_index", 0) or 0)
                    for shape in group.shapes
                ]
                if not base_indices:
                    continue
                shape_offset = int(getattr(request_set.info, "shape_offset", 0) or 0)
                legacy_group_base_min_indices[id(request_set)] = min(base_indices)
                legacy_request_start_indices[id(request_set)] = min(
                    base_index + shape_offset
                    for base_index in base_indices
                )

        root_instance_id = object_table[object_index]
        object_ops = self._get_headless_object_operations()
        embedded_viewer = self._embedded_headless_viewer
        if embedded_viewer and root_instance_id > 0:
            embedded_viewer.array_operations._delete_instance_and_children(root_instance_id)

        if object_ops:
            object_ops._remove_from_object_table(object_index)
        else:
            object_table.pop(object_index)

        # Every object-table index after the removed index shifts by -1.
        if self.handler.file_version < 25:
            for group in self.rcol.groups:
                for shape in group.shapes:
                    idx = getattr(shape.info, "user_data_index", -1)
                    if isinstance(idx, int) and idx > object_index:
                        shape.info.user_data_index = idx - 1
            for request_set in self.rcol.request_sets:
                start_idx = legacy_request_start_indices.get(id(request_set))
                base_min_idx = legacy_group_base_min_indices.get(id(request_set))
                if start_idx is None:
                    continue
                if base_min_idx is None:
                    continue
                # If group base indices are shifted by this deletion, effective request
                # windows already move with them and shape_offset must remain unchanged.
                if base_min_idx <= object_index < start_idx:
                    current_offset = int(getattr(request_set.info, "shape_offset", 0) or 0)
                    request_set.info.shape_offset = max(0, current_offset - 1)

        for request_set in self.rcol.request_sets:
            info = request_set.info
            if self.handler.file_version >= 25:
                if isinstance(info.request_set_userdata_index, int) and info.request_set_userdata_index > object_index:
                    info.request_set_userdata_index -= 1
                if isinstance(info.group_userdata_index_start, int) and info.group_userdata_index_start > object_index:
                    info.group_userdata_index_start -= 1

    def _get_headless_object_operations(self):
        embedded_viewer = self._embedded_headless_viewer
        if embedded_viewer is None:
            return None
        if not getattr(embedded_viewer, "object_operations", None):
            embedded_viewer.object_operations = RszObjectOperations(embedded_viewer)
        return embedded_viewer.object_operations

    def _get_request_shape_object_indices(self, request_set: RequestSet) -> list[int]:
        if not self.rcol.rsz or request_set.group is None:
            return []
        object_table = self.rcol.rsz.object_table or []
        shape_count = len(request_set.group.shapes)
        indices = []
        for shape_index in range(shape_count):
            if self.handler.file_version >= 25:
                obj_index = request_set.info.group_userdata_index_start + shape_index
            else:
                shape = request_set.group.shapes[shape_index]
                obj_index = shape.info.user_data_index + request_set.info.shape_offset
            if isinstance(obj_index, int) and 0 <= obj_index < len(object_table):
                indices.append(obj_index)
        return indices

    def _resolve_group_shape_template_instance_ids(self, group_index: int) -> list[int]:
        if not self.rcol.rsz or group_index < 0 or group_index >= len(self.rcol.groups):
            return []
        group = self.rcol.groups[group_index]
        object_table = self.rcol.rsz.object_table or []
        template_request = next(
            (rs for rs in self.rcol.request_sets if rs.info.group_index == group_index),
            None,
        )
        if template_request is None:
            return []

        instance_ids = []
        for shape_index in range(len(group.shapes)):
            if self.handler.file_version >= 25:
                obj_index = template_request.info.group_userdata_index_start + shape_index
            else:
                shape = group.shapes[shape_index]
                obj_index = shape.info.user_data_index + template_request.info.shape_offset
            if 0 <= obj_index < len(object_table):
                instance_ids.append(object_table[obj_index])
            else:
                instance_ids.append(0)
        return instance_ids

    def _initialize_shape_userdata_instances(self, group_userdata_index_start: int, template_instance_ids: list[int]):
        if not self.rcol.rsz or not template_instance_ids:
            return
        object_table = self.rcol.rsz.object_table or []

        for offset, template_instance_id in enumerate(template_instance_ids):
            object_index = group_userdata_index_start + offset
            if not (0 <= object_index < len(object_table)):
                continue
            new_instance_id = self._create_instance_from_template(template_instance_id)
            if new_instance_id > 0:
                object_table[object_index] = new_instance_id

    def _create_instance_from_template(self, template_instance_id: int) -> int:
        if not self.rcol.rsz:
            return -1
        object_ops = self._get_headless_object_operations()
        registry = self._resolve_type_registry()
        if object_ops is None or registry is None:
            return -1
        if not isinstance(template_instance_id, int) or template_instance_id <= 0:
            return -1
        if template_instance_id >= len(self.rcol.rsz.instance_infos):
            return -1

        template_instance = self.rcol.rsz.instance_infos[template_instance_id]
        type_id = int(getattr(template_instance, "type_id", 0) or 0)
        if not type_id:
            return -1
        type_info = registry.get_type_info(type_id)
        if not type_info:
            return -1
        return object_ops._create_object_instance_with_nested_objects(
            type_info,
            type_id,
            len(self.rcol.rsz.instance_infos),
        )

    def _create_instance_from_type_name(self, type_name: str) -> int:
        if not self.rcol.rsz:
            return -1
        object_ops = self._get_headless_object_operations()
        registry = self._resolve_type_registry()
        if object_ops is None or registry is None:
            return -1
        type_info, type_id = registry.find_type_by_name(type_name)
        if not type_info or not type_id:
            return -1
        return object_ops._create_object_instance_with_nested_objects(
            type_info,
            type_id,
            len(self.rcol.rsz.instance_infos),
        )

    def _initialize_shape_userdata_instances_at_indices(self, target_indices: list[int], template_instance_ids: list[int]):
        if not self.rcol.rsz or not target_indices or not template_instance_ids:
            return

        pending_insertions = []
        for offset, object_index in enumerate(target_indices):
            if offset >= len(template_instance_ids):
                break
            if object_index < 0:
                continue
            template_instance_id = template_instance_ids[offset]
            new_instance_id = self._create_instance_from_template(template_instance_id)
            if new_instance_id > 0:
                pending_insertions.append((object_index, new_instance_id))

        for object_index, instance_id in sorted(pending_insertions, key=lambda item: item[0], reverse=True):
            object_table = self.rcol.rsz.object_table
            object_table.append(instance_id)
            self._insert_root_object_id_at(object_index, len(object_table) - 1)

    def _line_edit(self, label: str, value: str, on_commit):
        edit = QLineEdit(value)
        edit.editingFinished.connect(lambda: on_commit(edit.text()))
        self.detail_form.addRow(label, edit)
        return edit

    def _float_spin(self, label: str, value: float, on_change, *, minimum: float = -1000000.0, maximum: float = 1000000.0, step: float = 0.05):
        spin = QDoubleSpinBox()
        spin.setDecimals(6)
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setValue(float(value))
        spin.valueChanged.connect(lambda v: self._on_float_spin_changed(on_change, float(v)))
        self.detail_form.addRow(label, spin)
        return spin

    def _on_float_spin_changed(self, on_change, value: float):
        on_change(value)
        self._mark_modified()
        self._refresh_scene_preview(self._current_payload())

    def _vec_spin_row(self, label: str, values: list[float], on_change, *, size: int = 3):
        row_widget = QWidget()
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        spins: list[QDoubleSpinBox] = []
        for i in range(size):
            spin = QDoubleSpinBox()
            spin.setDecimals(6)
            spin.setRange(-1000000.0, 1000000.0)
            spin.setSingleStep(0.05)
            spin.setValue(float(values[i] if i < len(values) else 0.0))
            row.addWidget(spin)
            spins.append(spin)

        def emit_change(_v):
            on_change([float(s.value()) for s in spins])
            self._mark_modified()
            self._refresh_scene_preview(self._current_payload())

        for spin in spins:
            spin.valueChanged.connect(emit_change)
        self.detail_form.addRow(label, row_widget)
        return spins

    def _combo(self, label: str, options: list[tuple[str, Any]], current_value, on_change):
        combo = QComboBox()
        selected = 0
        for idx, (text, value) in enumerate(options):
            combo.addItem(text, value)
            if value == current_value:
                selected = idx
        combo.setCurrentIndex(selected)
        combo.currentIndexChanged.connect(lambda _idx: on_change(combo.currentData()))
        self.detail_form.addRow(label, combo)
        return combo

    def _action_button(self, label: str, callback):
        btn = QPushButton(label)
        btn.clicked.connect(callback)
        self.detail_form.addRow("", btn)
        return btn

    def _mark_modified(self):
        self.modified = True
        if hasattr(self.handler, "modified"):
            self.handler.modified = True

    def _refresh_structure(
        self,
        select_payload: NavPayload,
        *,
        sections: tuple[str, ...] = (),
        refresh_headless: bool = False,
    ):
        self._sync_relationships()
        if not sections:
            with QSignalBlocker(self.nav_tabs):
                self._rebuild_navigation(payload_to_select=select_payload)
        else:
            for section in sections:
                self._rebuild_section(section)
            if refresh_headless:
                self._refresh_headless_region()
        self._select_and_render(select_payload)
        self._sync_tab_action_state()
        self._mark_modified()

    def _refresh_inplace(self, select_payload: NavPayload, *, update_tree_label: bool = False):
        self._sync_relationships()
        if update_tree_label:
            self._update_item_label(select_payload)

        found = self._find_item_by_payload(select_payload)
        if found is not None:
            tree, item = found
            with QSignalBlocker(tree):
                tree.setCurrentItem(item)
            self.nav_tabs.setCurrentWidget(tree)
            self.path_label.setText(self._build_path(item))
            self._render_payload(select_payload)
        else:
            raise IndexError(f"Unable to find payload for in-place refresh: {select_payload}")
        self._mark_modified()

    def _update_item_label(self, payload: NavPayload):
        found = self._find_item_by_payload(payload)
        if not found:
            return
        _, item = found

        if payload.kind == "group" and payload.group_index is not None:
            group = self.rcol.groups[payload.group_index]
            item.setText(0, f"[{payload.group_index}] {group.info.name or f'Group {payload.group_index + 1}'}")
        elif payload.kind == "shape" and payload.group_index is not None and payload.shape_index is not None:
            shape = self._get_shape(payload)
            prefix = "◦" if payload.mirror else "•"
            default_name = f"{'Mirror Shape' if payload.mirror else 'Shape'} {payload.shape_index + 1}"
            item.setText(0, f"{prefix} [{payload.shape_index}] {shape.info.name or default_name}")
        elif payload.kind == "request_set" and payload.request_index is not None:
            item.setText(0, self._request_set_label(payload.request_index))
        elif payload.kind == "ignore_tag" and payload.ignore_index is not None:
            tag = self.rcol.ignore_tags[payload.ignore_index]
            item.setText(0, f"[{payload.ignore_index}] {tag.tag or '(empty)'}")
        elif payload.kind == "auto_joint" and payload.auto_joint_index is not None:
            name = self.rcol.auto_generate_joint_descs[payload.auto_joint_index]
            item.setText(0, f"[{payload.auto_joint_index}] {name or '(empty)'}")

    def _invalidate_group_node(self, group_index: int):
        self._rebuild_section("groups")
        self._select_payload(NavPayload(kind="group", group_index=group_index))

    def _finalize_context_update(self, select_payload: NavPayload):
        self._select_and_render(select_payload)
        self._mark_modified()

    def _select_and_render(self, payload: NavPayload):
        found = self._find_item_by_payload(payload)
        if found is None:
            raise IndexError(f"Unable to resolve payload after tree update: {payload}")
        tree, item = found
        with QSignalBlocker(tree):
            tree.setCurrentItem(item)
        self.nav_tabs.setCurrentWidget(tree)
        self.path_label.setText(self._build_path(item))
        self._render_payload(payload)
        self._sync_tab_action_state()

    def _rebuild_section(self, section_kind: str):
        tree = self.nav_trees.get(section_kind)
        if not tree:
            return
        with QSignalBlocker(tree):
            tree.clear()
            if section_kind == "groups":
                self._populate_groups_section(tree)
            elif section_kind == "request_sets":
                self._populate_request_sets_section(tree)
            elif section_kind == "ignore_tags":
                self._populate_ignore_tags_section(tree)
            elif section_kind == "auto_joints":
                self._populate_auto_joints_section(tree)

    def _shape_list(self, group_index: int, mirror: bool) -> list[RcolShape]:
        group = self.rcol.groups[group_index]
        return group.extra_shapes if mirror else group.shapes

    def _supports_mirror_shapes(self) -> bool:
        return getattr(self.handler, "file_version", 25) >= 25

    def _supports_ignore_tags(self) -> bool:
        return getattr(self.handler, "file_version", 25) > 11

    def _supports_auto_joints(self) -> bool:
        return getattr(self.handler, "file_version", 25) > 11

    def _get_shape(self, payload: NavPayload) -> RcolShape:
        return self._shape_list(payload.group_index, payload.mirror)[payload.shape_index]

    def _is_payload_valid(self, payload: NavPayload | None) -> bool:
        if payload is None:
            return False
        if payload.kind in {"groups", "request_sets", "ignore_tags", "auto_joints"}:
            return True
        if payload.kind in {"group", "group_shapes"}:
            return payload.group_index is not None and 0 <= payload.group_index < len(self.rcol.groups)
        if payload.kind == "shape":
            if payload.group_index is None or payload.shape_index is None:
                return False
            if not (0 <= payload.group_index < len(self.rcol.groups)):
                return False
            shape_list = self._shape_list(payload.group_index, payload.mirror)
            return 0 <= payload.shape_index < len(shape_list)
        if payload.kind == "request_set":
            return payload.request_index is not None and 0 <= payload.request_index < len(self.rcol.request_sets)
        if payload.kind == "ignore_tag":
            tags = self.rcol.ignore_tags or []
            return payload.ignore_index is not None and 0 <= payload.ignore_index < len(tags)
        if payload.kind == "auto_joint":
            joints = self.rcol.auto_generate_joint_descs or []
            return payload.auto_joint_index is not None and 0 <= payload.auto_joint_index < len(joints)
        return True

    # ---------- Panels ----------
    def _render_groups_overview(self, _payload: NavPayload):
        self.detail_title.setText("Groups")
        self._row_text("Count", str(len(self.rcol.groups)))

    def _render_group(self, payload: NavPayload):
        group = self.rcol.groups[payload.group_index]
        self.detail_title.setText(f"Group [{payload.group_index}]")

        self._line_edit("Name", group.info.name or "", lambda text: self._set_group_name(payload.group_index, text))
        self._row_text("GUID", self._format_guid(group.info.guid))
        self._row_text("Layer GUID", self._format_guid(group.info.layer_guid))
        mask_guid_lines = [self._format_guid(mask_guid) for mask_guid in (group.info.mask_guids or [])]
        self._row_multiline_text("Mask GUIDs", mask_guid_lines or ["(none)"], max_lines=12)
        self._row_text("Regular Shapes", str(len(group.shapes)))
        if self._supports_mirror_shapes():
            self._row_text("Mirror Shapes", str(len(group.extra_shapes or [])))

        actions = QWidget()
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        add_shape = QPushButton("Add Shape")
        add_shape.clicked.connect(lambda: self._add_shape(payload.group_index, mirror=False))
        row.addWidget(add_shape)
        if self._supports_mirror_shapes():
            add_mirror = QPushButton("Add Mirror Shape")
            add_mirror.clicked.connect(lambda: self._add_shape(payload.group_index, mirror=True))
            row.addWidget(add_mirror)
        self.detail_form.addRow("", actions)

    def _render_group_shapes(self, payload: NavPayload):
        group = self.rcol.groups[payload.group_index]
        mirror_text = "Mirror" if payload.mirror else "Regular"
        self.detail_title.setText(f"{mirror_text} Shapes")
        count = len(self._shape_list(payload.group_index, payload.mirror))
        self._row_text("Group", group.info.name or f"Group {payload.group_index}")
        self._row_text("Count", str(count))
        self._action_button(
            f"Add {mirror_text} Shape",
            lambda: self._add_shape(payload.group_index, mirror=payload.mirror),
        )

    def _render_shape(self, payload: NavPayload):
        shape = self._get_shape(payload)
        self.detail_title.setText(f"{'Mirror ' if payload.mirror else ''}Shape [{payload.shape_index}]")

        self._line_edit("Name", shape.info.name or "", lambda text: self._set_shape_name(payload, text))
        self._row_text("GUID", self._format_guid(shape.info.guid))
        self._line_edit(
            "Primary Joint",
            shape.info.primary_joint_name_str or "",
            lambda text: self._set_primary_joint(payload, text),
        )
        self._line_edit(
            "Secondary Joint",
            shape.info.secondary_joint_name_str or "",
            lambda text: self._set_secondary_joint(payload, text),
        )

        type_options = [(st.name, int(st)) for st in ShapeType if st not in (ShapeType.Invalid, ShapeType.Max)]
        self._combo("Shape Type", type_options, int(shape.info.shape_type), lambda value: self._set_shape_type(payload, int(value)))
        self._render_shape_geometry_fields(payload, shape)
        associated_shape_object_ids = self._get_shape_object_id_entries(payload)
        self._row_multiline_text("Associated RSZ Object ID", associated_shape_object_ids or ["(none)"], max_lines=10)

        self._action_button("Delete Shape", lambda: self._remove_shape(payload))

    def _render_shape_geometry_fields(self, payload: NavPayload, shape: RcolShape):
        shape_data = shape.shape
        if shape_data is None:
            self._row_text("Geometry", "Unsupported or missing payload.")
            return

        if hasattr(shape_data, "center") and hasattr(shape_data, "radius"):
            self._vec_spin_row("Center", list(shape_data.center), lambda v: setattr(shape_data, "center", v))
            self._float_spin("Radius", float(shape_data.radius), lambda v: setattr(shape_data, "radius", v), minimum=0.0)
            return

        if hasattr(shape_data, "start") and hasattr(shape_data, "end") and hasattr(shape_data, "radius"):
            self._vec_spin_row("Start", list(shape_data.start), lambda v: setattr(shape_data, "start", v))
            self._vec_spin_row("End", list(shape_data.end), lambda v: setattr(shape_data, "end", v))
            self._float_spin("Radius", float(shape_data.radius), lambda v: setattr(shape_data, "radius", v), minimum=0.0)
            return

        if hasattr(shape_data, "min") and hasattr(shape_data, "max"):
            self._vec_spin_row("Min", list(shape_data.min), lambda v: setattr(shape_data, "min", v))
            self._vec_spin_row("Max", list(shape_data.max), lambda v: setattr(shape_data, "max", v))
            return

        if hasattr(shape_data, "extent") and hasattr(shape_data, "matrix"):
            self._vec_spin_row("Extent", list(shape_data.extent), lambda v: setattr(shape_data, "extent", v))
            for row_index in range(4):
                self._vec_spin_row(
                    f"Matrix Row {row_index}",
                    list(shape_data.matrix[row_index]),
                    lambda v, idx=row_index: self._set_matrix_row(shape_data, idx, v),
                    size=4,
                )
            return

        if hasattr(shape_data, "points") and hasattr(shape_data, "height") and hasattr(shape_data, "bottom"):
            for idx in range(4):
                self._vec_spin_row(
                    f"Point {idx}",
                    list(shape_data.points[idx]),
                    lambda v, i=idx: self._set_area_point(shape_data, i, v),
                    size=2,
                )
            self._float_spin("Bottom", float(shape_data.bottom), lambda v: setattr(shape_data, "bottom", v))
            self._float_spin("Height", float(shape_data.height), lambda v: setattr(shape_data, "height", v), minimum=0.0)
            return

        if hasattr(shape_data, "vertices"):
            for idx in range(3):
                vert = shape_data.vertices[idx] if idx < len(shape_data.vertices) else [0.0, 0.0, 0.0]
                self._vec_spin_row(
                    f"Vertex {idx}",
                    list(vert),
                    lambda v, i=idx: self._set_triangle_vertex(shape_data, i, v),
                )
            return

        self._row_text("Geometry", "No editor for this shape payload yet.")

    def _render_request_sets_overview(self, _payload: NavPayload):
        self.detail_title.setText("Request Sets")
        self._row_text("Count", str(len(self.rcol.request_sets)))

    def _render_request_set(self, payload: NavPayload):
        request_set = self.rcol.request_sets[payload.request_index]
        self.detail_title.setText(f"Request Set [{payload.request_index}]")

        self._line_edit("Name", request_set.info.name or "", lambda text: self._set_request_name(payload.request_index, text))
        self._line_edit("Key", request_set.info.key_name or "", lambda text: self._set_request_key(payload.request_index, text))

        linked_group = request_set.group
        self._row_text("Linked Shape Count", str(len(linked_group.shapes) if linked_group else 0))
        associated_objects = self._get_request_set_object_entries(payload.request_index)
        self._row_multiline_text("Associated Objects", associated_objects, max_lines=12)


    def _render_ignore_tags_overview(self, _payload: NavPayload):
        self.detail_title.setText("Ignore Tags")
        self._row_text("Count", str(len(self.rcol.ignore_tags or [])))

    def _render_ignore_tag(self, payload: NavPayload):
        tag = self.rcol.ignore_tags[payload.ignore_index]
        self.detail_title.setText(f"Ignore Tag [{payload.ignore_index}]")
        self._line_edit("Tag", tag.tag or "", lambda text: self._set_ignore_tag(payload.ignore_index, text))
        self._row_text("Note", "Tag hash is auto-managed on save.")

    def _render_auto_joints_overview(self, _payload: NavPayload):
        self.detail_title.setText("Auto Generate Joints")
        self._row_text("Count", str(len(self.rcol.auto_generate_joint_descs or [])))

    def _render_auto_joint(self, payload: NavPayload):
        idx = payload.auto_joint_index
        self.detail_title.setText(f"Auto Joint [{idx}]")
        desc = self.rcol.auto_generate_joint_descs[idx]
        self._line_edit("Descriptor", desc or "", lambda text: self._set_auto_joint_desc(idx, text))

        if idx < len(self.rcol.auto_generate_joint_entry_meta):
            self._row_text("Metadata", str(self.rcol.auto_generate_joint_entry_meta[idx] or {}))

    def _refresh_headless_region(self):
        if self._headless_body is None or self._headless_status is None:
            return

        has_rsz = bool(getattr(self.rcol, "rsz", None) and getattr(self.rcol, "user_data_bytes", b""))
        byte_length = len(getattr(self.rcol, "user_data_bytes", b""))
        self._headless_status.setText(f"Present: {'Yes' if has_rsz else 'No'} · Byte Length: {byte_length}")

        while self._headless_body.count():
            item = self._headless_body.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()
        self._embedded_headless_viewer = None
        self._embedded_headless_handler = None

        if not has_rsz:
            self._headless_body.addWidget(QLabel("No headless RSZ block was found in this RCOL."))
            return

        try:
            rsz_handler = RszHandler()
            rsz_handler.app = getattr(self.handler, "app", None)
            rsz_handler.dark_mode = getattr(self.handler, "dark_mode", False)
            rsz_handler.show_advanced = True
            rsz_handler.filepath = (getattr(self.handler, "filepath", "") or "") + ".wcc"
            rsz_handler.type_registry = self.rcol.type_registry
            rsz_handler.rsz_file = self.rcol.rsz
            rsz_handler.id_manager = IdManager.instance()
            if rsz_handler.app and hasattr(rsz_handler.app, "settings"):
                rsz_handler.set_game_version(rsz_handler.app.settings.get("game_version", "RE4"))
            viewer = rsz_handler.create_viewer()
        except Exception as exc:
            self._headless_body.addWidget(QLabel(f"Failed to build headless RSZ region: {exc}"))
            return

        if viewer is None:
            self._headless_body.addWidget(QLabel("Failed to create the embedded headless RSZ viewer."))
            return

        viewer.modified_changed.connect(lambda changed: self._mark_modified() if changed else None)
        self._embedded_headless_viewer = viewer
        self._embedded_headless_handler = rsz_handler
        self._headless_body.addWidget(viewer)


    # ---------- Data operations ----------
    def _add_group(self):
        group = RcolGroup()
        group.info.name = f"Group_{len(self.rcol.groups)}"
        group.info.guid = b"\x00" * 16
        group.info.layer_guid = b"\x00" * 16
        group.info.mask_guids = []
        group.extra_shapes = []
        self.rcol.groups.append(group)
        self._refresh_structure(
            NavPayload(kind="group", group_index=len(self.rcol.groups) - 1),
            sections=("groups",),
        )

    def _remove_selected_group(self):
        group_index = self._selected_index_for_kind("group")
        if group_index is None:
            QMessageBox.information(self, "Remove Group", "Select a group entry in the Groups tab first.")
            return
        self._remove_group(group_index)

    def _remove_selected_request_set(self):
        index = self._selected_index_for_kind("request_set")
        if index is None:
            QMessageBox.information(self, "Remove Request Set", "Select a request set entry first.")
            return
        self._remove_request_set(index)

    def _remove_selected_ignore_tag(self):
        index = self._selected_index_for_kind("ignore_tag")
        if index is None:
            QMessageBox.information(self, "Remove Ignore Tag", "Select an ignore tag entry first.")
            return
        self._remove_ignore_tag(index)

    def _add_auto_joint(self):
        if self.rcol.auto_generate_joint_descs is None:
            self.rcol.auto_generate_joint_descs = []
        self.rcol.auto_generate_joint_descs.append("")
        if hasattr(self.rcol, "auto_generate_joint_entry_meta") and self.rcol.auto_generate_joint_entry_meta is not None:
            self.rcol.auto_generate_joint_entry_meta.append({})
        self._refresh_structure(
            NavPayload(kind="auto_joint", auto_joint_index=len(self.rcol.auto_generate_joint_descs) - 1),
            sections=("auto_joints",),
        )

    def _remove_selected_auto_joint(self):
        index = self._selected_index_for_kind("auto_joint")
        if index is None:
            QMessageBox.information(self, "Remove Auto Joint", "Select an auto joint entry first.")
            return
        self._remove_auto_joint(index)

    def _remove_group(self, group_index: int):
        if len(self.rcol.groups) <= 1:
            QMessageBox.warning(self, "Cannot Delete", "At least one group must remain.")
            return
        assigned_request_count = sum(
            1
            for request_set in self.rcol.request_sets
            if int(getattr(request_set.info, "group_index", -1)) == group_index
        )
        if assigned_request_count > 0:
            QMessageBox.warning(
                self,
                "Cannot Delete",
                f"Group [{group_index}] has {assigned_request_count} request set(s) assigned. "
                "Remove or reassign those request sets before deleting the group.",
            )
            return
        if self.rcol.rsz and self.handler.file_version < 25:
            group = self.rcol.groups[group_index]
            base_userdata_indices = [
                idx for idx in (
                    getattr(shape.info, "user_data_index", -1)
                    for shape in group.shapes
                )
                if isinstance(idx, int) and idx >= 0
            ]
            for object_index in sorted(set(base_userdata_indices), reverse=True):
                self._remove_request_root_object_id_at(object_index)
        del self.rcol.groups[group_index]
        for request_set in self.rcol.request_sets:
            if request_set.info.group_index > group_index:
                request_set.info.group_index -= 1
            elif request_set.info.group_index >= len(self.rcol.groups):
                request_set.info.group_index = max(0, len(self.rcol.groups) - 1)
        self._refresh_structure(
            NavPayload(kind="groups"),
            sections=("groups", "request_sets"),
        )

    def _set_group_name(self, group_index: int, value: str):
        self.rcol.groups[group_index].info.name = value
        self._refresh_inplace(NavPayload(kind="group", group_index=group_index), update_tree_label=True)

    def _add_shape(self, group_index: int, mirror: bool):
        if mirror and not self._supports_mirror_shapes():
            QMessageBox.information(
                self,
                "Unsupported Operation",
                "Mirror shapes are only supported in RCOL version 25+ files.",
            )
            return
        selected_userdata_type = None
        if not mirror and self.rcol.rsz:
            selected_userdata_type = self._prompt_shape_userdata_type()
            if not selected_userdata_type:
                return

        group = self.rcol.groups[group_index]
        old_shape_count = len(group.shapes)

        shape = RcolShape()
        shape.info.shape_type = ShapeType.Sphere
        shape.info.guid = b"\x00" * 16
        shape.shape = create_shape(ShapeType.Sphere)
        prefix = "MirrorShape" if mirror else "Shape"
        shape.info.name = f"{prefix}_{len(self._shape_list(group_index, mirror))}"
        if self.handler.file_version < 25 and not mirror:
            if self.rcol.rsz and selected_userdata_type:
                target_requests = [
                    rs for rs in self.rcol.request_sets
                    if rs.info.group_index == group_index
                ]
                insertion_indices = []
                if target_requests:
                    sorted_requests = sorted(
                        target_requests,
                        key=lambda rs: int(getattr(rs.info, "shape_offset", 0) or 0),
                    )
                    for request_set in sorted_requests:
                        request_indices = self._get_request_shape_object_indices(request_set)
                        if request_indices:
                            insertion_indices.append(max(request_indices) + 1)
                        else:
                            insertion_indices.append(len(self.rcol.rsz.object_table or []))
                else:
                    insertion_indices.append(len(self.rcol.rsz.object_table or []))

                shift = 0
                for insertion_index in insertion_indices:
                    new_instance_id = self._create_instance_from_type_name(selected_userdata_type)
                    if new_instance_id <= 0:
                        continue
                    object_table = self.rcol.rsz.object_table
                    if object_table is None:
                        continue
                    object_table.append(new_instance_id)
                    self._insert_root_object_id_at(insertion_index + shift, len(object_table) - 1)
                    shift += 1

                if insertion_indices:
                    shape.info.user_data_index = insertion_indices[0]
            else:
                shape.info.user_data_index = old_shape_count

        self._shape_list(group_index, mirror).append(shape)
        if self.rcol.rsz and selected_userdata_type and self.handler.file_version >= 25 and not mirror:
            target_requests = [
                rs for rs in self.rcol.request_sets
                if rs.info.group_index == group_index
            ]
            for request_set in sorted(
                target_requests,
                key=lambda rs: int(getattr(rs.info, "group_userdata_index_start", 0) or 0),
            ):
                insertion_index = request_set.info.group_userdata_index_start + old_shape_count
                new_instance_id = self._create_instance_from_type_name(selected_userdata_type)
                if new_instance_id <= 0:
                    continue
                object_table = self.rcol.rsz.object_table
                if object_table is None:
                    continue
                object_table.append(new_instance_id)
                self._insert_root_object_id_at(insertion_index, len(object_table) - 1)
        select_payload = NavPayload(
            kind="shape",
            group_index=group_index,
            shape_index=len(self._shape_list(group_index, mirror)) - 1,
            mirror=mirror,
        )
        self._sync_relationships()
        self._invalidate_group_node(group_index)
        if self.rcol.rsz:
            self._refresh_headless_region()
        self._finalize_context_update(select_payload)

    def _remove_shape(self, payload: NavPayload):
        shape_list = self._shape_list(payload.group_index, payload.mirror)
        if payload.shape_index >= len(shape_list):
            return

        if not payload.mirror and self.rcol.rsz:
            request_sets = [
                rs for rs in self.rcol.request_sets
                if rs.info.group_index == payload.group_index
            ]
            object_indices_to_remove = []
            if self.handler.file_version < 25:
                base_object_index = shape_list[payload.shape_index].info.user_data_index
                if isinstance(base_object_index, int) and base_object_index >= 0:
                    object_indices_to_remove.append(base_object_index)
            for request_set in request_sets:
                request_indices = self._get_request_shape_object_indices(request_set)
                if payload.shape_index < len(request_indices):
                    object_indices_to_remove.append(request_indices[payload.shape_index])
            for object_index in sorted(set(object_indices_to_remove), reverse=True):
                self._remove_request_root_object_id_at(object_index)

        del shape_list[payload.shape_index]
        self._sync_relationships()
        self._invalidate_group_node(payload.group_index)
        if self.rcol.rsz:
            self._refresh_headless_region()
        self._finalize_context_update(NavPayload(kind="group", group_index=payload.group_index))

    def _set_shape_name(self, payload: NavPayload, value: str):
        self._get_shape(payload).info.name = value
        self._refresh_inplace(payload, update_tree_label=True)

    def _set_primary_joint(self, payload: NavPayload, value: str):
        self._get_shape(payload).info.primary_joint_name_str = value
        self._refresh_inplace(payload)

    def _set_secondary_joint(self, payload: NavPayload, value: str):
        self._get_shape(payload).info.secondary_joint_name_str = value
        self._refresh_inplace(payload)

    @staticmethod
    def _set_matrix_row(shape_data, row_index: int, row_values: list[float]):
        shape_data.matrix[row_index] = [float(v) for v in row_values[:4]]

    @staticmethod
    def _set_area_point(shape_data, point_index: int, values: list[float]):
        shape_data.points[point_index] = [float(values[0]), float(values[1])]

    @staticmethod
    def _set_triangle_vertex(shape_data, vertex_index: int, values: list[float]):
        while len(shape_data.vertices) <= vertex_index:
            shape_data.vertices.append([0.0, 0.0, 0.0])
        shape_data.vertices[vertex_index] = [float(v) for v in values[:3]]

    def _set_shape_type(self, payload: NavPayload, shape_type: int):
        shape = self._get_shape(payload)
        new_type = ShapeType(shape_type)
        if shape.info.shape_type == new_type:
            return
        shape.info.shape_type = new_type
        shape.shape = create_shape(new_type)
        self._refresh_inplace(payload)

    def _add_request_set(self):
        selected_type = self._prompt_request_set_type()
        if not selected_type:
            return
        target_group_index = self._prompt_request_set_group_index()
        if target_group_index is None:
            return

        new_request_id = self._next_request_id()
        target_group = self.rcol.groups[target_group_index]
        group_shape_count = len(target_group.shapes) if target_group else 0
        existing_group_requests = [
            rs for rs in self.rcol.request_sets
            if rs.info.group_index == target_group_index
        ]
        is_first_group_request = len(existing_group_requests) == 0

        reserve_shape_userdata_slots = (
            self.handler.file_version >= 25
            and not is_first_group_request
            and group_shape_count > 0
        )
        reserved_shape_slots = group_shape_count if reserve_shape_userdata_slots else 0
        template_shape_instance_ids = (
            self._resolve_group_shape_template_instance_ids(target_group_index)
            if (not is_first_group_request and group_shape_count > 0)
            else []
        )

        try:
            request_set_userdata_index, group_userdata_index_start, root_instance_id = self._append_headless_request_userdata(
                selected_type,
                reserved_shape_slots,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Add Request Set", f"Failed to initialize request-set userdata:\n{exc}")
            return

        if self.handler.file_version < 25:
            new_request_table_index = len(self.rcol.request_sets)
            self._insert_root_object_id_at(new_request_table_index, request_set_userdata_index)
            request_set_userdata_index = new_request_table_index
            if new_request_table_index <= group_userdata_index_start:
                group_userdata_index_start += 1

        new_shape_offset = 0
        if is_first_group_request:
            new_shape_offset = 0
        elif self.handler.file_version < 25:
            new_shape_offset = max(
                (
                    int(getattr(rs.info, "shape_offset", 0) or 0)
                    for rs in existing_group_requests
                ),
                default=0,
            ) + group_shape_count
        if template_shape_instance_ids:
            if self.handler.file_version < 25 and target_group:
                target_object_indices = [
                    shape.info.user_data_index + new_shape_offset
                    for shape in target_group.shapes
                ]
                self._initialize_shape_userdata_instances_at_indices(
                    target_object_indices,
                    template_shape_instance_ids,
                )
            elif reserved_shape_slots > 0:
                self._initialize_shape_userdata_instances(group_userdata_index_start, template_shape_instance_ids)

        request_set = RequestSet(index=len(self.rcol.request_sets))
        request_set.info.name = f"RequestSet_{len(self.rcol.request_sets)}"
        request_set.info.group_index = target_group_index
        request_set.info.id = new_request_id
        request_set.info.request_set_userdata_index = request_set_userdata_index
        request_set.info.group_userdata_index_start = group_userdata_index_start
        request_set.info.request_set_index = new_request_id
        request_set.info.field0 = request_set.info.request_set_index
        if self.handler.file_version < 25:
            request_set.info.shape_offset = new_shape_offset
        elif is_first_group_request:
            request_set.info.shape_offset = 0
        request_set.group = target_group
        request_set.instance = root_instance_id
        request_set.shape_userdata = []
        self.rcol.request_sets.append(request_set)
        self._refresh_structure(
            NavPayload(kind="request_set", request_index=len(self.rcol.request_sets) - 1),
            sections=("request_sets",),
            refresh_headless=True,
        )

    def _remove_request_set(self, request_index: int):
        if request_index >= len(self.rcol.request_sets):
            return
        request_set = self.rcol.request_sets[request_index]
        root_object_index = (
            request_set.info.request_set_userdata_index
            if self.handler.file_version >= 25
            else request_index
        )
        same_group_requests = [
            rs for rs in self.rcol.request_sets
            if rs.info.group_index == request_set.info.group_index
        ]
        should_remove_shape_objects = len(same_group_requests) > 1
        target_indices = [root_object_index]
        if should_remove_shape_objects:
            target_indices.extend(self._get_request_shape_object_indices(request_set))

        for object_index in sorted(set(target_indices), reverse=True):
            self._remove_request_root_object_id_at(object_index)
        del self.rcol.request_sets[request_index]

        if self.handler.file_version < 25:
            deleted_shape_offset = int(getattr(request_set.info, "shape_offset", 0) or 0)
            target_group_index = request_set.info.group_index
            if 0 <= target_group_index < len(self.rcol.groups):
                removed_group_shape_count = len(self.rcol.groups[target_group_index].shapes)
            else:
                removed_group_shape_count = len(request_set.group.shapes) if request_set.group else 0
            for remaining_request in self.rcol.request_sets:
                info = remaining_request.info
                if info.group_index != target_group_index:
                    continue
                current_offset = int(getattr(info, "shape_offset", 0) or 0)
                if current_offset > deleted_shape_offset:
                    info.shape_offset = current_offset - removed_group_shape_count

        self._refresh_structure(
            NavPayload(kind="request_sets"),
            sections=("request_sets",),
            refresh_headless=True,
        )

    def _set_request_name(self, request_index: int, value: str):
        self.rcol.request_sets[request_index].info.name = value
        self._refresh_inplace(NavPayload(kind="request_set", request_index=request_index), update_tree_label=True)

    def _set_request_key(self, request_index: int, value: str):
        self.rcol.request_sets[request_index].info.key_name = value
        self._refresh_inplace(NavPayload(kind="request_set", request_index=request_index), update_tree_label=True)

    def _add_ignore_tag(self):
        if self.rcol.ignore_tags is None:
            self.rcol.ignore_tags = []
        tag = IgnoreTag()
        self.rcol.ignore_tags.append(tag)
        self._refresh_structure(
            NavPayload(kind="ignore_tag", ignore_index=len(self.rcol.ignore_tags) - 1),
            sections=("ignore_tags",),
        )

    def _remove_ignore_tag(self, index: int):
        if not self.rcol.ignore_tags or index >= len(self.rcol.ignore_tags):
            return
        del self.rcol.ignore_tags[index]
        self._refresh_structure(
            NavPayload(kind="ignore_tags"),
            sections=("ignore_tags",),
        )

    def _set_ignore_tag(self, index: int, text: str):
        self.rcol.ignore_tags[index].tag = text
        self._refresh_inplace(NavPayload(kind="ignore_tag", ignore_index=index), update_tree_label=True)

    def _set_auto_joint_desc(self, index: int, text: str):
        self.rcol.auto_generate_joint_descs[index] = text
        self._refresh_inplace(NavPayload(kind="auto_joint", auto_joint_index=index), update_tree_label=True)

    def _remove_auto_joint(self, index: int):
        joints = self.rcol.auto_generate_joint_descs or []
        if index >= len(joints):
            return
        del joints[index]
        if (
            hasattr(self.rcol, "auto_generate_joint_entry_meta")
            and self.rcol.auto_generate_joint_entry_meta is not None
            and index < len(self.rcol.auto_generate_joint_entry_meta)
        ):
            del self.rcol.auto_generate_joint_entry_meta[index]
        self._refresh_structure(
            NavPayload(kind="auto_joints"),
            sections=("auto_joints",),
        )

    def _sync_relationships(self):
        for idx, request_set in enumerate(self.rcol.request_sets):
            request_set.index = idx
            if not self.rcol.groups:
                request_set.group = None
                request_set.info.group_index = -1
                continue

            if request_set.info.group_index < 0 or request_set.info.group_index >= len(self.rcol.groups):
                request_set.info.group_index = 0
            request_set.group = self.rcol.groups[request_set.info.group_index]
