from __future__ import annotations

from collections import Counter
from colorsys import hsv_to_rgb
from copy import copy, deepcopy

lazy import numpy as np
from PySide6.QtCore import QElapsedTimer, QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSplitter, QStackedLayout, QVBoxLayout, QWidget,
)

lazy from ui.scene.mesh_scene import (
    mesh_geometry_choices, mesh_scene_payloads, payload_records_have_complete_attribute,
)
from ui.scene.scene_preview import ScenePreviewWidget
lazy from ui.editor_widgets import styled_label
lazy from ui.icons import line_icon
lazy from ui.scene.mesh_preview import MeshPreviewScene
lazy from ui.styles import current_colors
lazy from .scene_textures import plain_preview
lazy from .preview_controls import MorphControls, find_combo_data

lazy from .conversion_geometry import detach_topology, mesh_payloads
lazy from .conversion_skin import adapt_source_skinning
lazy from .conversion_visibility import materialize_skinning_visibility


def part_color(index):
    return hsv_to_rgb((index * 0.618 + 0.48) % 1, 0.48, 0.9)


class _ComparisonViewport(ScenePreviewWidget):
    camera_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent, controls="mesh", initial_rotation=(12, -22),
                         background=(0.055, 0.075, 0.10, 1.0))
        self.setMinimumSize(170, 200)

    def _build_overlay(self):
        self.fps_label = QLabel(self)
        self.fps_label.hide()

    def _update_after_camera_change(self):
        self.distance = min(80.0, max(0.2, self.distance))
        self.camera_changed.emit(self)
        self.update()

    def keyPressEvent(self, event):
        if event.key() != Qt.Key_F11:
            super().keyPressEvent(event)


def _channels(mesh, layer, lod):
    if mesh is None or layer != "main":
        return {}
    bodies = getattr(getattr(mesh, "blend_shape_data", None), "bodies", ())
    channels = [channel for body in bodies if body.lod_index == lod
                for channel in body.channels if channel.segments]
    names = Counter(channel.name for channel in channels)
    # Unique names survive format-specific channel renumbering.
    return {
        ("name", channel.name) if channel.name and names[channel.name] == 1
        else ("index", channel.index, channel.name): channel
        for channel in channels
    }


class MeshConversionPreview(QWidget):
    def __init__(self, source_mesh, parent=None):
        super().__init__(parent)
        self._meshes = [source_mesh, None]
        self._target_parts = []
        self._scenes = [[], []]
        self._preview_states = [{}, {}]
        self._busy = False
        self._disposed = False
        self._source_skinning_contract = None
        self._source_weight_mesh = None
        self._source_weight_error = ""
        self._target_message = self.tr("Check conversion to preview the result")
        self._camera = None
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._rotate)
        self._elapsed = QElapsedTimer()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.geometry_combo = QComboBox(self)
        self.geometry_combo.setAccessibleName(self.tr("Preview geometry and LOD"))
        self.geometry_combo.setToolTip(self.tr("Inspect the same LOD on both sides. Missing LODs stay empty."))
        self.mode_combo = QComboBox(self)
        for label, value in ((self.tr("Surface"), None), (self.tr("Wireframe"), "wire"),
                             (self.tr("Normals"), "normals"), (self.tr("Weights"), "skin_weights")):
            self.mode_combo.addItem(label, value)
        self.mode_combo.setToolTip(self.tr("Normals: RGB = XYZ. Weights: color = strongest joint; brightness = its weight."))
        self.fit_button = QPushButton(line_icon("fit"), self.tr("Fit both"), self)
        self.fit_button.setToolTip(self.tr("Frame both models together at the same scale and world origin."))
        self.spin_button = QPushButton(line_icon("refresh"), self.tr("Turntable"), self)
        self.spin_button.setCheckable(True)
        self.spin_button.setToolTip(self.tr("Slowly rotate both models. Drag or scroll either view to take control."))
        for widget in (self.geometry_combo, self.mode_combo):
            toolbar.addWidget(widget)
        self.part_combo = QComboBox(self)
        self.part_combo.setAccessibleName(self.tr("Preview exported part"))
        self.part_combo.setToolTip(self.tr("View the complete assembly, colored by file, or isolate one part."))
        self.part_combo.hide()
        toolbar.addWidget(self.part_combo)
        toolbar.addStretch()
        toolbar.addWidget(self.fit_button)
        toolbar.addWidget(self.spin_button)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        self.views, self.notes, self.stacks, self.placeholders, self.headings = [], [], [], [], []
        self._devices = []
        colors = current_colors()
        for index, title in enumerate((self.tr("BEFORE · Source"), self.tr("AFTER · Converted"))):
            panel = QFrame(splitter)
            panel.setObjectName("conversionPreviewPanel")
            panel.setStyleSheet("QFrame#conversionPreviewPanel { border: 1px solid %s; border-radius: 4px; }"
                                % colors["border_subtle"])
            column = QVBoxLayout(panel)
            column.setContentsMargins(6, 6, 6, 6)
            column.setSpacing(4)
            heading = styled_label(title, panel, heading="panel", tone="info" if index == 0 else "success")
            heading.setTextFormat(Qt.PlainText)
            heading.setWordWrap(True)
            column.addWidget(heading)
            stack = QStackedLayout()
            view = _ComparisonViewport(panel)
            self._devices.append(plain_preview(view, self))
            view.camera_changed.connect(self._camera_changed)
            placeholder = styled_label(parent=panel, tone="muted")
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setWordWrap(True)
            placeholder.setTextFormat(Qt.PlainText)
            stack.addWidget(view)
            stack.addWidget(placeholder)
            column.addLayout(stack, 1)
            note = styled_label(parent=panel, tone="muted")
            note.setTextFormat(Qt.PlainText)
            note.setWordWrap(True)
            column.addWidget(note)
            self.views.append(view)
            self.notes.append(note)
            self.stacks.append(stack)
            self.placeholders.append(placeholder)
            self.headings.append(heading)
        layout.addWidget(splitter, 1)

        self.morph_controls = MorphControls(self, frame_interval=lambda:
            max((view._frame_interval() for view in self.views), default=0))
        self.morph_combo = self.morph_controls.combo
        layout.addWidget(self.morph_controls)
        hint = styled_label(self.tr("Linked views · Drag to orbit · Right-drag to pan · Scroll to zoom · Geometry preview, without game materials"), self, tone="subtle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.geometry_combo.currentIndexChanged.connect(self._geometry_changed)
        self.mode_combo.currentIndexChanged.connect(self._refresh)
        self.part_combo.currentIndexChanged.connect(self._refresh)
        self.morph_controls.changed.connect(self._refresh)
        self.fit_button.clicked.connect(self.fit)
        self.spin_button.toggled.connect(self._update_turntable)
        self._populate_geometry()
        self.fit()

    def set_target(self, mesh, label="Converted"):
        if self._disposed:
            return
        self._preview_states[1].clear()
        self._target_parts = list(mesh) if isinstance(mesh, (list, tuple)) else [mesh]
        self._meshes[1] = self._target_parts[0]
        with QSignalBlocker(self.part_combo):
            self.part_combo.clear()
            self.part_combo.addItem(self.tr("All {} parts · colored by file").format(len(self._target_parts)), None)
            for index, part in enumerate(self._target_parts):
                self.part_combo.addItem(self.tr("Part {:02} · {} bones in palette").format(
                    index + 1, len(part.bone_remap_indices)), index)
            self.part_combo.setVisible(len(self._target_parts) > 1)
        self.headings[1].setText(self.tr("AFTER · {}").format(label))
        self._populate_geometry()

    def set_source_skinning(self, contract=None):
        if self._disposed:
            return
        self._preview_states[0].clear()
        self._source_skinning_contract = contract
        self._source_weight_mesh = None
        self._source_weight_error = ""
        self._refresh()

    def _effective_source_weights(self):
        if self._source_weight_error:
            raise ValueError(self._source_weight_error)
        if self._source_weight_mesh is None:
            original = self._meshes[0]
            mesh = copy(original)
            mesh.mesh_buffer = copy(original.mesh_buffer)
            memo = {id(original.mesh_buffer): mesh.mesh_buffer}
            mesh.meshes = deepcopy(original.meshes, memo)
            payloads = {id(payload): copy(payload) for payload in mesh_payloads(original)}
            mesh.mesh_buffer.buffer_payloads = {
                index: payloads[id(payload)] for index, payload in original.mesh_buffer.buffer_payloads.items()
            }
            for name in ("shadow_mesh", "occluder_mesh"):
                owner = getattr(original, name)
                if owner is not None:
                    owner = copy(owner)
                    detach_topology(owner, memo)
                    owner.payloads = {index: payloads[id(payload)] for index, payload in owner.payloads.items()}
                    setattr(mesh, name, owner)
            try:
                adapt_source_skinning(mesh, self._source_skinning_contract)
                materialize_skinning_visibility(mesh, lambda _message: None)
            except ValueError as exc:
                self._source_weight_error = str(exc)
                raise
            self._source_weight_mesh = mesh
        return self._source_weight_mesh

    def clear_target(self, message="Check conversion to preview the result"):
        if self._disposed:
            return
        self._preview_states[1].clear()
        self._meshes[1] = None
        self._target_parts = []
        self.part_combo.hide()
        self.views[1].set_scene([], reset_camera=False)
        self._target_message = message
        self._populate_geometry()

    def set_busy(self, busy):
        if self._disposed:
            return
        self._busy = bool(busy)
        if busy:
            self.morph_controls.stop()
        self.morph_controls.setEnabled(not busy)
        self._update_turntable()
        if busy:
            self._unavailable(1, self.tr("Building your converted mesh…"))
        else:
            self._refresh()

    def _populate_geometry(self):
        previous = self.geometry_combo.currentData()
        choices = sorted(set(mesh_geometry_choices(self._meshes[0]) + mesh_geometry_choices(self._meshes[1])))
        with QSignalBlocker(self.geometry_combo):
            self.geometry_combo.clear()
            for layer, lod in choices:
                self.geometry_combo.addItem(self.tr("{layer} · LOD {lod}").format(layer=layer.title(), lod=lod), (layer, lod))
            self.geometry_combo.setCurrentIndex(find_combo_data(self.geometry_combo, previous, 0))
        self._geometry_changed()

    def _geometry_changed(self):
        layer, lod = self.geometry_combo.currentData() or ("main", 0)
        previous = self.morph_combo.currentData()
        channels = {}
        for mesh in self._meshes:
            channels.update(_channels(mesh, layer, lod))
        with QSignalBlocker(self.morph_combo):
            self.morph_combo.clear()
            self.morph_combo.addItem(self.tr("Base pose"), None)
            for key, channel in channels.items():
                self.morph_combo.addItem(channel.name or self.tr("Channel {}").format(channel.index), key)
            self.morph_combo.setCurrentIndex(find_combo_data(self.morph_combo, previous, 0))
        self.morph_controls.refresh_enabled()
        self._refresh()

    def _refresh(self, *_args):
        if self._disposed:
            return
        layer, lod = self.geometry_combo.currentData() or ("main", 0)
        mode, key = self.mode_combo.currentData(), self.morph_combo.currentData()
        self.morph_controls.refresh_enabled()
        for side, mesh in enumerate(self._meshes):
            previous = self._scenes[side]
            self._scenes[side] = []
            if mesh is None or (side == 1 and self._busy):
                self._unavailable(side, self.tr("Building your converted mesh…") if self._busy else self._target_message)
                continue
            if (layer, lod) not in mesh_geometry_choices(mesh):
                self._unavailable(side, self.tr("This mesh has no {} LOD {}.").format(layer, lod))
                continue
            channel = _channels(mesh, layer, lod).get(key)
            notes = []
            if key is not None and channel is None:
                notes.append(self.tr("Morph unavailable here · Showing base pose"))
            try:
                if side == 0 and (mode == "skin_weights" or self._source_skinning_contract is not None):
                    mesh = self._effective_source_weights()
                records = mesh_scene_payloads(mesh, layer=layer, lod_index=lod)
                attribute = mode if mode not in (None, "wire") else None
                if attribute and not payload_records_have_complete_attribute(records, attribute, {"normals": 3}):
                    notes.append(self.tr("{} unavailable here · Showing surface").format(self.mode_combo.currentText()))
                    attribute = None
                selected = self.part_combo.currentData()
                parts = ([(i, part) for i, part in enumerate(self._target_parts) if selected is None or i == selected]
                         if side == 1 else [(0, mesh)])
                scene, states, updates = [], {}, []
                for index, part in parts:
                    channel = _channels(part, layer, lod).get(key)
                    color = (part_color(index)
                             if side == 1 and len(self._target_parts) > 1 else (0.72, 0.80, 0.88))
                    cache_key = (id(part), layer, lod, attribute, index)
                    state = self._preview_states[side].get(cache_key)
                    if state is None:
                        state = MeshPreviewScene(
                            part, key=f"conversion:{side}:{index}", layer=layer, lod_index=lod,
                            include_vertex_colors=False, visualize_attribute=attribute, color=color,
                        )
                        if attribute == "skin_weights" and state.scene:
                            state.attribute.mode, state.attribute.palette = "dominant_joint", part.bone_remap_indices
                            state.refresh_attribute_colors()
                    if state.update(channel.index if channel else None, self.morph_controls.weight):
                        updates.append(state)
                    states[cache_key] = state
                    scene.extend(state.scene)
                self._preview_states[side] = states
                if not scene:
                    self._unavailable(side, self.tr("No drawable geometry at this LOD."))
                    continue
                for draw in scene:
                    draw.bounds_minimum = draw.bounds_maximum = None
                view = self.views[side]
                view.color_source = "vertex" if attribute or (side == 1 and len(self._target_parts) > 1) else ""
                wireframe, lighting = ("wire" if mode == "wire" else "off"), ("off" if attribute else "fixed")
                if view.wireframe_mode != wireframe:
                    view._set_wireframe_mode(wireframe)
                if view.lighting_mode != lighting:
                    view._set_lighting_mode(lighting)
                if len(scene) == len(previous) and all(a is b for a, b in zip(scene, previous)):
                    for state in updates:
                        state.upload(view)
                else:
                    view.set_scene(scene, reset_camera=False)
                self._scenes[side] = scene
                self.stacks[side].setCurrentIndex(0)
                count = sum(len(draw.vertices) for draw in scene)
                faces = sum(len(draw.indices) // 3 for draw in scene)
                self.notes[side].setText(" · ".join([f"{count:,} vertices · {faces:,} triangles", *notes]))
            except Exception as exc:
                self._unavailable(side, self.tr("Preview unavailable: {}").format(exc))
        self._apply_camera()

    def _unavailable(self, side, message):
        self.stacks[side].setCurrentIndex(1)
        self.placeholders[side].setText(message)
        self.notes[side].clear()

    def _camera_changed(self, view):
        self.spin_button.setChecked(False)
        self._remember_camera(view)
        self._apply_camera()

    def _remember_camera(self, view):
        self._camera = (view.rot_x, view.rot_y, view.distance, view.scale, view.center.copy())

    def _apply_camera(self):
        if self._camera is not None:
            for view in self.views:
                view.rot_x, view.rot_y, view.distance, view.scale, center = self._camera
                view.center = center.copy()
                view.update()

    def fit(self):
        points = [draw.vertices[np.isfinite(draw.vertices).all(axis=1)]
                  for scene in self._scenes for draw in scene]
        points = [value for value in points if len(value)]
        if not points:
            return
        minimum = np.min([p.min(axis=0) for p in points], axis=0)
        maximum = np.max([p.max(axis=0) for p in points], axis=0)
        extent = max(float(np.max(maximum - minimum)), 1e-6)
        aspect = min(view.width() / max(1, view.height()) for view in self.views)
        self._camera = (12.0, -22.0, max(2.2, 1.8 / max(0.1, aspect)), 1 / extent,
                        (minimum + maximum) / 2)
        self._apply_camera()

    def _update_turntable(self):
        self._timer.stop()
        if not self._disposed and self.spin_button.isChecked() and self.isVisible() and not self._busy:
            self._elapsed.start()
            self._timer.start()

    def _rotate(self):
        view = self.views[0]
        view.rot_y = (view.rot_y + min(self._elapsed.restart(), 100) * 0.018) % 360
        self._remember_camera(view)
        self._apply_camera()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_turntable()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def cleanup(self):
        if self._disposed:
            return
        self._disposed = True
        self._timer.stop()
        self.morph_controls.stop()
        self._meshes = [None, None]
        self._target_parts = []
        self._scenes = [[], []]
        self._preview_states = [{}, {}]
        self._source_weight_mesh = self._source_skinning_contract = None
        for device in self._devices:
            device.set_active(False)
        for view in self.views:
            view.cleanup()

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)
