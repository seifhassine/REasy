from collections import deque
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ui.scene.mesh_scene import mesh_lod_parts


CAMERA_VIEWS = {
    "orbit": (20.0, -35.0),
    "front": (0.0, 0.0), "back": (0.0, 180.0),
    "right": (0.0, -90.0), "left": (0.0, 90.0),
    "top": (90.0, 0.0), "bottom": (-90.0, 0.0),
}


@dataclass(frozen=True, slots=True)
class MeshPart:
    group: int
    material: int
    name: str
    vertices: np.ndarray
    triangles: int


class MeshInspection:
    def __init__(self, mesh, preview):
        self.geometry = preview.layer, preview.lod_index
        draw = preview.scene[0]
        self.base_vertices = draw.vertices
        sources = [(group, submesh) for group, submesh in mesh_lod_parts(
            mesh, preview.lod_index, layer=preview.layer) if submesh.indices_count]
        names = mesh.material_names
        self.parts = tuple(MeshPart(
            group, submesh.material_index,
            names[submesh.material_index] if 0 <= submesh.material_index < len(names) else "",
            np.unique(batch.indices), len(batch.indices) // 3,
        ) for (group, submesh), batch in zip(sources, draw.batches, strict=True))
        self.submeshes = tuple(submesh for _group, submesh in sources)
        self.all_parts = frozenset(range(len(self.parts)))

    def vertex_indices(self, parts):
        chunks = [self.parts[i].vertices for i in sorted(self.all_parts.intersection(parts))]
        return np.unique(np.concatenate(chunks)) if chunks else np.empty(0, dtype=np.uint32)

    def statistics(self, parts):
        parts = self.all_parts.intersection(parts)
        indices = self.vertex_indices(parts)
        points = self.base_vertices[indices]
        points = points[np.isfinite(points).all(axis=1)]
        dimensions = np.ptp(points, axis=0) if len(points) else None
        return len(indices), sum(self.parts[i].triangles for i in parts), dimensions


class MeshInspector(QWidget):
    visibility_changed = Signal(object)
    frame_requested = Signal(object)
    camera_requested = Signal(str)
    orthographic_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.inspection = None
        self.visible_parts = set()
        self._visibility_history = deque(maxlen=32)
        self._items = []
        self.setMinimumWidth(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 0, 0)
        title = QLabel(self.tr("Inspect mesh"))
        layout.addWidget(title)
        camera = QHBoxLayout()
        self.camera_combo = QComboBox(self)
        for label, name in (
            (self.tr("Custom view"), "custom"), (self.tr("3D view"), "orbit"),
            (self.tr("Front (+Z)"), "front"), (self.tr("Back (−Z)"), "back"),
            (self.tr("Right (+X)"), "right"), (self.tr("Left (−X)"), "left"),
            (self.tr("Top (+Y)"), "top"), (self.tr("Bottom (−Y)"), "bottom"),
        ):
            self.camera_combo.addItem(label, name)
        self.camera_combo.model().item(0).setEnabled(False)
        self.camera_combo.setToolTip(self.tr(
            "Views use the mesh's stored axes. Axis views switch to orthographic projection.\n"
            "In the viewport: 1 front, 3 right, 7 top; Ctrl for opposite; 5 toggles projection."))
        self.camera_combo.activated.connect(lambda: self.camera_requested.emit(self.camera_combo.currentData()))
        camera.addWidget(self.camera_combo, 1)
        self.ortho_check = QCheckBox(self.tr("Orthographic"), self)
        self.ortho_check.setToolTip(self.tr("Inspect proportions without perspective distortion (5 in the viewport)."))
        self.ortho_check.toggled.connect(self.orthographic_changed)
        camera.addWidget(self.ortho_check)
        layout.addLayout(camera)
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText(self.tr("Find material or submesh…"))
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setToolTip(self.tr("Filter this list; viewport visibility stays unchanged."))
        self.filter_edit.textChanged.connect(self._filter)
        layout.addWidget(self.filter_edit)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels([self.tr("Material / submesh"), self.tr("Verts"), self.tr("Tris")])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.tree.setToolTip(self.tr(
            "Check to show; uncheck to hide. Select a material to select all its submeshes.\n"
            "Ctrl/Shift selects several rows. Double-click to show and frame the selection."))
        self.tree.itemChanged.connect(self._visibility_edited)
        self.tree.itemSelectionChanged.connect(self._update_statistics)
        self.tree.itemDoubleClicked.connect(lambda: self.frame_selection())
        layout.addWidget(self.tree, 1)
        actions = QGridLayout()
        self.isolate_button = QPushButton(self.tr("Isolate selection"), self)
        self.isolate_button.setToolTip(self.tr("Isolate selected submeshes. Press again to restore the previous visibility. Shortcut: I."))
        self.isolate_button.clicked.connect(self.isolate_selection)
        actions.addWidget(self.isolate_button, 0, 1)
        self.show_all_button = QPushButton(self.tr("Show all"), self)
        self.show_all_button.setToolTip(self.tr("Show every submesh. Shortcut: Alt+H."))
        self.show_all_button.clicked.connect(self.show_all)
        actions.addWidget(self.show_all_button, 1, 0)
        self.back_button = QPushButton(self.tr("Restore visibility"), self)
        self.back_button.setToolTip(self.tr("Restore the previous visibility, including manually hidden parts. Shortcut: Alt+Left."))
        self.back_button.setEnabled(False)
        self.back_button.clicked.connect(self.restore_visibility)
        actions.addWidget(self.back_button, 1, 1)
        layout.addLayout(actions)
        self.frame_button = QPushButton(self.tr("Frame selection"), self)
        self.frame_button.setToolTip(self.tr("Show selected submeshes and center the camera on them. Shortcut: F in the viewport."))
        self.frame_button.clicked.connect(self.frame_selection)
        actions.addWidget(self.frame_button, 0, 0)
        for button in (self.frame_button, self.isolate_button, self.show_all_button, self.back_button):
            button.setProperty("compact", True)
        self.summary = QLabel(self)
        self.selection_summary = QLabel(self)
        for label in (self.summary, self.selection_summary):
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setToolTip(self.tr(
                "Vertices count unique referenced buffer entries, including UV/normal seam duplicates.\n"
                "Triangles count rendered triangles; quads count as two. Dimensions use the base pose in mesh units."))
            layout.addWidget(label)
        self.setEnabled(False)

    def set_inspection(self, inspection):
        self.setEnabled(True)
        if self.inspection is inspection:
            return
        self.inspection = inspection
        self._visibility_history.clear()
        self.back_button.setEnabled(False)
        self.visible_parts = set(inspection.all_parts)
        self._items = []
        with QSignalBlocker(self.tree):
            self.tree.clear()
            materials = {}
            for index, part in enumerate(inspection.parts):
                if part.material not in materials:
                    name = part.name or self.tr("Unnamed material")
                    parent = QTreeWidgetItem(self.tree, [f"{part.material}: {name}"])
                    parent.setToolTip(0, parent.text(0))
                    parent.setData(0, Qt.UserRole, set())
                    materials[part.material] = parent
                parent = materials[part.material]
                parent.setData(0, Qt.UserRole, parent.data(0, Qt.UserRole) | {index})
                item = QTreeWidgetItem(parent, [self.tr("Submesh {index} · Group {group}").format(
                    index=index + 1, group=part.group), f"{len(part.vertices):,}", f"{part.triangles:,}"])
                item.setToolTip(0, item.text(0))
                item.setData(0, Qt.UserRole, {index})
                self._items.append(item)
            for item in materials.values():
                vertices, triangles, _ = inspection.statistics(item.data(0, Qt.UserRole))
                item.setText(1, f"{vertices:,}")
                item.setText(2, f"{triangles:,}")
                item.setExpanded(len(inspection.parts) <= 100)
            self._items.extend(materials.values())
            for item in self._items:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked)
        self._filter(self.filter_edit.text())

    def clear(self):
        self.inspection = None
        self.visible_parts.clear()
        self._visibility_history.clear()
        self.back_button.setEnabled(False)
        self._items.clear()
        self.tree.clear()
        self.summary.clear()
        self.selection_summary.clear()
        self.setEnabled(False)

    def selected_parts(self):
        return set().union(*(item.data(0, Qt.UserRole) for item in self.tree.selectedItems()))

    def set_visible_parts(self, parts, *, remember=True):
        if self.inspection is None:
            return
        parts = set(self.inspection.all_parts.intersection(parts))
        if parts == self.visible_parts:
            return
        if remember:
            self._visibility_history.append(frozenset(self.visible_parts))
        self.back_button.setEnabled(bool(self._visibility_history))
        self.visible_parts = parts
        with QSignalBlocker(self.tree):
            for item in self._items:
                ids = item.data(0, Qt.UserRole)
                visible = ids & parts
                item.setCheckState(0, Qt.Checked if visible == ids else Qt.PartiallyChecked if visible else Qt.Unchecked)
        self._update_statistics()
        self.visibility_changed.emit(parts)

    def isolate_selection(self):
        selected = self.selected_parts()
        if selected:
            if selected == self.visible_parts:
                self.restore_visibility()
            else:
                self.set_visible_parts(selected)

    def restore_visibility(self):
        if self._visibility_history:
            self.set_visible_parts(self._visibility_history.pop(), remember=False)

    def hide_selection(self):
        self.set_visible_parts(self.visible_parts - self.selected_parts())

    def show_all(self):
        if self.inspection is not None:
            self.set_visible_parts(self.inspection.all_parts)

    def _visibility_edited(self, item, column):
        if column != 0:
            return
        parts = item.data(0, Qt.UserRole)
        self.set_visible_parts(self.visible_parts | parts if item.checkState(0) == Qt.Checked
                               else self.visible_parts - parts)

    def frame_selection(self, *, isolate=False):
        parts = self.selected_parts()
        if parts:
            self.set_visible_parts(parts if isolate else self.visible_parts | parts)
            self.frame_requested.emit(parts)

    def _filter(self, text):
        query = text.strip().casefold()
        with QSignalBlocker(self.tree):
            self.tree.clearSelection()
            for i in range(self.tree.topLevelItemCount()):
                parent = self.tree.topLevelItem(i)
                material_match = query in parent.text(0).casefold()
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    child.setHidden(not (material_match or query in child.text(0).casefold()))
                parent.setHidden(all(parent.child(j).isHidden() for j in range(parent.childCount())))
                if query and not parent.isHidden():
                    parent.setExpanded(True)
        self._update_statistics()

    def _update_statistics(self):
        if self.inspection is None:
            return
        selected = self.selected_parts()
        for button in (self.isolate_button, self.frame_button):
            button.setEnabled(bool(selected))
        vertices, triangles, _ = self.inspection.statistics(self.visible_parts)
        self.summary.setText(self.tr("{visible} / {total} submeshes visible\n{vertices:,} vertices · {triangles:,} triangles").format(
            visible=len(self.visible_parts), total=len(self.inspection.parts), vertices=vertices, triangles=triangles))
        vertices, triangles, dimensions = self.inspection.statistics(selected)
        self.selection_summary.setVisible(bool(selected))
        if selected:
            size = " × ".join(f"{float(value):.6g}" for value in dimensions) if dimensions is not None else "—"
            self.selection_summary.setText(self.tr(
                "{count} selected · {vertices:,} vertices · {triangles:,} triangles\n"
                "Base dimensions (X × Y × Z): {size} mesh units").format(
                    count=len(selected), vertices=vertices, triangles=triangles, size=size))

    def set_camera_state(self, view, orthographic):
        with QSignalBlocker(self.camera_combo), QSignalBlocker(self.ortho_check):
            self.camera_combo.setCurrentIndex(self.camera_combo.findData(view))
            self.ortho_check.setChecked(orthographic)
