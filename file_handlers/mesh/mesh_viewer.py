from __future__ import annotations

from collections import Counter
from functools import partial

lazy import numpy as np
from ui.scene.scene_preview import ScenePreviewWidget, view_light
from PySide6.QtCore import QSignalBlocker, Qt, Signal
lazy from PySide6.QtGui import QAction, QFont, QFontMetrics, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from file_handlers.mmtr.light_passes import light_pass_catalog_for
from file_handlers.mmtr.overlay_scene import ALL_EDGES, VISIBLE_EDGES, SceneShown
from file_handlers.mmtr.profiles import mmtr_profile_for_game
from file_handlers.mmtr.textures_view import UvPattern
from file_handlers.tex.texture_quality import DEFAULT_TEXTURE_QUALITY
lazy from settings import app_settings, remember_setting
lazy from ui.editor_widgets import editor_strip, pane_header, styled_label
lazy from ui.icons import line_icon
from ui.qt_timers import call_later
lazy from ui.scene.mesh_scene import (
    mesh_geometry_choices,
    mesh_scene_payloads,
    payload_records_have_complete_attribute,
)
lazy from ui.scene.mesh_preview import MeshPreviewScene
from ui.scene.scene_marks import bounds_marks, unorm
lazy from .preview_controls import MorphControls
lazy from .attribute_controls import AttributeControls
from .mesh_file import joint_name
lazy from .viewer_navigation import MeshNavigation
lazy from .mesh_inspector import CAMERA_VIEWS, MeshInspection, MeshInspector
lazy from .material_session import MeshMaterialCollection, MeshMaterialSession
lazy from .material_resolver import MeshMaterialResolver
lazy from .material_shader_preview import MaterialShaderControls
lazy from .normal_recalc import INDICES_PER_WORKGROUP, VERTEX_INDEX_BITS
lazy from .attributes_preview import AttributeView, AttributesPreview
lazy from .textures_preview import TexturedView, TexturesPreview
from .lit_preview import LitLabels, LitLines, LitOverlays, LitPoints, LitPreview, LitShape, LitView
lazy from .profiles import (
    BlendShapeLayout,
    LegacyLightMapLayout,
    MeshBufferLayout,
    NormalRecalcLayout,
    VertexNormalWSemantic,
)

_BOUNDS_COLOUR, _PIVOT_COLOUR, _SKELETON_COLOUR = (0.2, 0.9, 1.0), (1.0, 0.35, 0.1), (1.0, 0.85, 0.15)
# Pixels at pixel ratio 1.
_LABEL_MARGIN = 4
_DEVICE_SHADINGS = ("attributes", "lit", "textures")


# Buttons match the combo boxes' height.
_TOOLBAR_STYLESHEET = """
    QPushButton { padding: 3px 8px; min-height: 18px; max-height: 18px; }
    QToolButton { padding: 3px 6px; min-height: 18px; max-height: 18px; }
    QToolButton[popupMode="2"] { padding-right: 18px; }
"""


def _tool_bar(parent: QWidget, name: str, surface: str) -> tuple[QWidget, QHBoxLayout]:
    bar, layout = editor_strip(surface, margins=(6, 4, 6, 4))
    bar.setParent(parent)
    bar.setObjectName(name)
    bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    bar.setStyleSheet(_TOOLBAR_STYLESHEET)
    return bar, layout


def _shown_shading(shadings) -> str | None:
    return next((shading for shading in _DEVICE_SHADINGS if shading in shadings), None)


def _visible_parts(viewport) -> frozenset[int]:
    return viewport.inspection.all_parts.difference(viewport._hidden_parts.get("mesh", ()))


def _view_fields(viewport) -> tuple:
    """In ``LitView``'s positional field order."""
    return (float(viewport.rot_x), float(viewport.rot_y), float(viewport.distance), tuple(map(float, viewport.center)),
            float(viewport.scale), viewport._device_window.pixels(), (viewport._geometry_layer, viewport._geometry_lod),
            _visible_parts(viewport))


def _lod_summary(lod, profile, corpus_zero_legacy: bool) -> str:
    has_level = profile is not None and profile.lod_has_level
    parts = [f"level={lod.lod_level}"] if has_level else []
    if not corpus_zero_legacy or lod.vertex_format:
        parts.append(f"format=0x{lod.vertex_format:02X}")
    parts += [f"factor={lod.lod_factor:g}", f"groups={len(lod.mesh_groups)}"]
    return ", ".join(parts)


def _submesh_summary(submesh, profile, is_wilds: bool = False) -> str:
    parts = [f"material={submesh.material_index}"]
    if (profile is not None and profile.submesh_has_buffer_index) or submesh.buffer_index:
        parts.append(f"buffer={submesh.buffer_index}")
    parts += [
        f"vertices={submesh.vert_count}@{submesh.verts_index_offset}",
        f"indices={submesh.indices_count}@{submesh.faces_index_offset}",
    ]
    parts.append(f"quad={bool(submesh.is_quad)}")
    if is_wilds:
        parts.append(f"allocated={submesh.allocated_index_count}, pad={submesh.index_padding_count}")
    return ", ".join(parts)


class MeshViewer(QWidget):
    modified_changed = Signal(bool)
    VERTEX_COLORS_SETTINGS_KEY = "mesh_viewer_use_vertex_colors"

    def __init__(self, handler):
        super().__init__()
        self.setObjectName("meshViewer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.handler = handler
        self._closed = False
        self._material_panel_visible = False
        self._data_panel_visible = False
        self._data_tree_populated = False
        self._material_table_populated = False
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.viewport = None
        self._build_ui()
        mesh = getattr(self.handler, "mesh", None)
        if mesh is not None:
            try:
                self.viewport = _MeshViewport(
                    mesh,
                    self._settings_store(),
                    use_vertex_colors=self._setting_bool(self.VERTEX_COLORS_SETTINGS_KEY),
                )
                self.viewport.camera_changed.connect(self.inspector.set_camera_state)
                self.inspector.visibility_changed.connect(self.viewport.set_visible_parts)
                self.inspector.frame_requested.connect(self.viewport.fit_geometry)
                self.inspector.camera_requested.connect(self.viewport.set_camera_view)
                self.inspector.orthographic_changed.connect(self.viewport.set_orthographic)
                self.inspector.set_camera_state("front", False)
                self.viewport.texture_quality_changed.connect(self._on_texture_quality_changed)
                self.preview_splitter.insertWidget(0, self.viewport)
                self.preview_splitter.setStretchFactor(0, 1)
                self.preview_splitter.setStretchFactor(1, 0)
            except Exception as e:
                self.preview_splitter.insertWidget(
                    0, self._empty_state(self.tr("Failed to create viewer: {}").format(e), "danger")
                )
        else:
            self.preview_splitter.insertWidget(
                0, self._empty_state(self.tr("No mesh buffer data available to display"), "muted")
            )
        self._populate_geometry_controls()
        self._refresh_geometry()
        if self.viewport is not None:
            self.navigation = MeshNavigation(self)
        quality = getattr(self.viewport, "texture_quality", DEFAULT_TEXTURE_QUALITY)
        self._materials = MeshMaterialCollection(self.viewport, parent=self)
        self._material_session = MeshMaterialSession(
            self.handler,
            texture_quality=quality,
            parent=self._materials,
        )
        self._material_session.reset.connect(self._on_material_reset)
        self._materials.add("mesh", self._material_session, start=False)
        self.lit_preview = LitPreview(self.handler, self._material_session, self._shown_inspection, self)
        self.textures_preview = TexturesPreview(self.handler, self._material_session, self._shown_inspection,
                                                self._shown_scene, self)
        self.attributes_preview = AttributesPreview(self.handler, self._material_session, self._shown_inspection,
                                                    self._shown_scene, self._shown_attribute, self)
        if self.viewport is not None:
            self.attributes_preview.status_changed.connect(self._show_attribute_status)
            # Connected first: the attributes preview is toggled before it learns the geometry shown.
            self.viewport.geometry_changed.connect(self._sync_attributes_preview)
            self.attribute_controls.changed.connect(self.attributes_preview.colours_changed)
            # Connected before the shading controls turn a preview on.
            for shading, preview in (("attributes", self.attributes_preview), ("lit", self.lit_preview),
                                     ("textures", self.textures_preview)):
                self.viewport.geometry_changed.connect(preview.geometry_changed)
                # Sent while the viewport paints, so handled after.
                self.viewport.device_view_changed.connect(preview.view_changed, Qt.QueuedConnection)
                # Sent before the window is destroyed, so handled at once.
                self.viewport.device_window_changed.connect(preview.set_window)
                self.viewport.shown_shading_changed.connect(
                    lambda shown, preview=preview, shading=shading: preview.set_shown(shown in (None, shading)))
                preview.active_changed.connect(partial(self.viewport.set_device_shading, shading))
                preview.frame_changed.connect(partial(self.viewport.show_device_frame, shading))
        settings = self._settings_store()
        self.shader_preview = MaterialShaderControls(
            self.lit_preview, self.textures_preview, self.material_panel, game_lighting=self._game_lighting(),
            chosen=str((settings or {}).get("renderer_shading", "game")),
            remember=partial(self._save_setting, "renderer_shading"))
        if self.viewport is not None:
            self.shader_preview.shading_shown.connect(self.viewport.set_shading_controls)
            self.viewport.set_shading_controls(self.shader_preview.shading())
            self.viewport.camera_changed.connect(self._refuse_game_lighting)
            self.viewport.geometry_changed.connect(self._refuse_game_lighting)
            self._refuse_game_lighting()
            self.inspector.visibility_changed.connect(self.shader_preview.retry)
        self.material_panel.layout().insertWidget(self.material_panel.layout().indexOf(self.material_table),
                                                  self.shader_preview)
        call_later(self, 0, self._reload_materials)

    def _empty_state(self, text: str, tone: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setProperty("tone", tone)
        return label

    def _shown_inspection(self):
        return self.viewport.inspection if self.viewport is not None else None

    def _shown_scene(self):
        return self.viewport._preview_scene if self.viewport is not None else None

    def _shown_attribute(self) -> str | None:
        return self.viewport._visualize_attribute if self.viewport is not None else None

    def _sync_attributes_preview(self, _preview=None) -> None:
        self.attributes_preview.set_active(self._shown_attribute() is not None)

    def _show_attribute_status(self) -> None:
        self.attribute_controls.set_status(self.attributes_preview.status())

    def _open_conversion(self):
        from .conversion_dialog import MeshConversionDialog

        self.morph_controls.pause()
        dialog = MeshConversionDialog(self.handler, self)
        dialog.exec()
        dialog.deleteLater()

    def _import_blend(self):
        from .blender_ui import import_into_viewer

        self.morph_controls.pause()
        import_into_viewer(self)

    def _build_ui(self):
        toolbar, panels = _tool_bar(self, "meshToolbar", "toolbar")
        self.sdftex_button = QPushButton(line_icon("cube"), self.tr("Distance field…"), self)
        self.sdftex_button.clicked.connect(self._open_distance_field)
        panels.addWidget(self.sdftex_button)
        self.convert_button = QPushButton(line_icon("export"), self.tr("Convert mesh…"), self)
        self.convert_button.clicked.connect(self._open_conversion)
        self.convert_button.setEnabled(self.handler.supports_editing())
        panels.addWidget(self.convert_button)
        self.blend_import_button = QPushButton(line_icon("import"), self.tr("Import .blend…"), self)
        self.blend_import_button.setToolTip(self.tr(
            "Apply edits from a .blend exported by Convert mesh… › Blender. Untouched data stays bit-exact."))
        self.blend_import_button.clicked.connect(self._import_blend)
        self.blend_import_button.setEnabled(self.handler.supports_editing())
        panels.addWidget(self.blend_import_button)
        panels.addStretch()
        self.inspect_button = QPushButton(line_icon("layers"), self.tr("Inspect"), self)
        self.inspect_button.setToolTip(self.tr("Materials, submeshes, selection, and camera views. Ctrl+F searches the list."))
        self.panel_toggle_btn = QPushButton(line_icon("image"), self.tr("Textures"), self)
        self.panel_toggle_btn.setToolTip(self.tr("Resolved material textures."))
        self.data_toggle_btn = QPushButton(line_icon("details"), self.tr("Mesh data"), self)
        self.data_toggle_btn.setToolTip(self.tr("Resource paths and parsed mesh structure."))
        for button in (self.inspect_button, self.panel_toggle_btn, self.data_toggle_btn):
            button.setCheckable(True)
            panels.addWidget(button)
        self._layout.addWidget(toolbar)

        view_bar, view = _tool_bar(self, "meshViewToolbar", "chrome")
        view.addWidget(QLabel(self.tr("Geometry")))
        self.geometry_combo = QComboBox(self)
        self.geometry_combo.setAccessibleName(self.tr("Geometry and LOD"))
        self.geometry_combo.currentIndexChanged.connect(self._on_geometry_changed)
        view.addWidget(self.geometry_combo)
        view.addSpacing(8)
        view.addWidget(QLabel(self.tr("View")))
        self.attribute_combo = QComboBox(self)
        self.attribute_combo.setAccessibleName(self.tr("Displayed mesh attribute"))
        self.attribute_combo.setMinimumContentsLength(14)
        self.attribute_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.attribute_combo.setToolTip(self.tr(
            "Inspect decoded attributes with influence heatmaps, UV checkers, and individual color channels. "
            "Each view includes its own controls and legend."))
        self.attribute_combo.currentIndexChanged.connect(self._on_attribute_changed)
        view.addWidget(self.attribute_combo)
        self.attribute_toggle = QToolButton(self)
        self.attribute_toggle.setIcon(line_icon("compare"))
        self.attribute_toggle.setAccessibleName(self.tr("Compare surface and attribute"))
        self.attribute_toggle.setToolTip(self.tr("Switch between the surface and the last attribute view. Shortcut: V in the viewport."))
        self.attribute_toggle.setEnabled(False)
        view.addWidget(self.attribute_toggle)
        view.addSpacing(8)

        self.display_button = QToolButton(self)
        self.display_button.setText(self.tr("Display"))
        self.display_button.setIcon(line_icon("eye"))
        self.display_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.display_button.setToolTip(self.tr("Vertex colors, bounds, and pivot overlays."))
        self.display_button.setPopupMode(QToolButton.InstantPopup)
        display_menu = QMenu(self.display_button)
        display_menu.setToolTipsVisible(True)
        self.vertex_colors_action = QAction(self.tr("Vertex colors"), self)
        self.vertex_colors_action.setToolTip(self.tr("Multiply textured preview by mesh vertex colors"))
        self.bounds_action = QAction(self.tr("Bounds"), self)
        self.pivots_action = QAction(self.tr("Pivots"), self)
        for action in (self.vertex_colors_action, self.bounds_action, self.pivots_action):
            action.setCheckable(True)
            display_menu.addAction(action)
        self.vertex_colors_action.setChecked(self._setting_bool(self.VERTEX_COLORS_SETTINGS_KEY))
        self.vertex_colors_action.toggled.connect(self._on_vertex_colors_toggled)
        self.bounds_action.toggled.connect(self._on_overlay_toggled)
        self.pivots_action.toggled.connect(self._on_overlay_toggled)
        self.display_button.setMenu(display_menu)
        view.addWidget(self.display_button)
        self.fit_button = QPushButton(line_icon("fit"), self.tr("Fit visible"), self)
        self.fit_button.setToolTip(self.tr("Frame visible submeshes. F frames the selection, or visible geometry when nothing is selected."))
        self.fit_button.clicked.connect(lambda: self.viewport.fit_geometry() if self.viewport else None)
        view.addWidget(self.fit_button)
        self.navigation_button = QToolButton(self)
        self.navigation_button.setText(self.tr("Navigate"))
        self.navigation_button.setIcon(line_icon("camera"))
        self.navigation_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.navigation_button.setPopupMode(QToolButton.InstantPopup)
        self.navigation_button.setToolTip(self.tr("Camera views, selection commands, and viewport shortcuts."))
        view.addWidget(self.navigation_button)
        view.addStretch()
        self._layout.addWidget(view_bar)

        self.attribute_controls = AttributeControls(self)
        self.attribute_controls.setContentsMargins(6, 4, 6, 4)
        self.attribute_controls.changed.connect(
            lambda: self.viewport.refresh_attribute_colors() if self.viewport else None)
        self._layout.addWidget(self.attribute_controls)
        self.morph_controls = MorphControls(self, frame_interval=lambda:
            self.viewport._frame_interval() if self.viewport else 0)
        self.morph_controls.setContentsMargins(6, 4, 6, 4)
        self.morph_controls.changed.connect(self._refresh_geometry)
        self.preview_status = styled_label(parent=self, banner="danger")
        self.preview_status.setTextFormat(Qt.PlainText)
        self.preview_status.setWordWrap(True)
        self.preview_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview_status.hide()
        self._layout.addWidget(self.preview_status)

        self.preview_splitter = QSplitter(Qt.Horizontal, self)
        self.preview_splitter.setChildrenCollapsible(False)
        self._layout.addWidget(self.preview_splitter, 1)
        self._layout.addWidget(self.morph_controls)
        self.sidebar_panel = QWidget(self)
        sidebar_layout = QVBoxLayout(self.sidebar_panel)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)
        self.sidebar_title = QLabel()
        header = pane_header(self.sidebar_title)
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header.layout().setContentsMargins(8, 5, 8, 5)
        sidebar_layout.addWidget(header)
        self.sidebar = QStackedWidget(self.sidebar_panel)
        sidebar_layout.addWidget(self.sidebar, 1)
        self.preview_splitter.addWidget(self.sidebar_panel)
        self.inspector = MeshInspector(self)
        self.sidebar.addWidget(self.inspector)

        self.material_panel = QWidget(self)
        side_layout = QVBoxLayout(self.material_panel)
        side_layout.setContentsMargins(6, 6, 6, 6)
        self.mdf_label = QLabel(self.tr("MDF: unresolved"), self.material_panel)
        self.mdf_label.setTextFormat(Qt.PlainText)
        self.mdf_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.mdf_label.setWordWrap(True)
        side_layout.addWidget(self.mdf_label)
        self.open_mdf_button = QPushButton(self.tr("Open MDF"), self.material_panel)
        self.open_mdf_button.setToolTip(self.tr(
            "Open this mesh's MDF. Its materials' Edit Shader… changes the shader of this object or of every object "
            "using the same master material."))
        self.open_mdf_button.setEnabled(False)
        self.open_mdf_button.clicked.connect(self._open_mdf)
        side_layout.addWidget(self.open_mdf_button)
        self.material_table = QTableWidget(0, 5, self.material_panel)
        self.material_table.setHorizontalHeaderLabels(
            [
                self.tr("Mesh"),
                "MDF",
                self.tr("Type"),
                self.tr("Texture"),
                self.tr("Status"),
            ]
        )
        self.material_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.material_table.setSelectionMode(QTableWidget.SingleSelection)
        self.material_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.material_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.material_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.material_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.material_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.material_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.material_table.itemSelectionChanged.connect(self._update_texture_preview)
        side_layout.addWidget(self.material_table, 1)

        self.texture_preview = styled_label(
            self.tr("Select a material to preview its texture."), surface="recessed", tone="muted"
        )
        self.texture_preview.setAlignment(Qt.AlignCenter)
        self.texture_preview.setMinimumHeight(140)
        self.texture_preview.setWordWrap(True)
        side_layout.addWidget(self.texture_preview)

        self.sidebar.addWidget(self.material_panel)

        self.data_panel = QWidget(self)
        data_layout = QVBoxLayout(self.data_panel)
        data_layout.setContentsMargins(6, 6, 6, 6)
        self.sdftex_label = QLabel(self.data_panel)
        self.sdftex_label.setTextFormat(Qt.PlainText)
        self.sdftex_label.setWordWrap(True)
        self.sdftex_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        data_layout.addWidget(self.sdftex_label)
        self.data_tree = QTreeWidget(self.data_panel)
        self.data_tree.setColumnCount(2)
        self.data_tree.setHeaderLabels([self.tr("Field"), self.tr("Value")])
        self.data_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.data_tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        data_layout.addWidget(self.data_tree, 1)
        self.sidebar.addWidget(self.data_panel)
        self.sidebar_panel.hide()
        self._panel_buttons = ((self.inspect_button, self.inspector), (self.panel_toggle_btn, self.material_panel),
                               (self.data_toggle_btn, self.data_panel))
        self._panel_titles = {self.inspector: self.inspector.panel_title,
                              self.material_panel: self.tr("Resolved material textures"),
                              self.data_panel: self.tr("Parsed mesh structure")}
        for button, panel in self._panel_buttons:
            button.toggled.connect(lambda visible, page=panel: self._set_panel(page, visible))
        self._refresh_sdftex_path()

    def _refresh_sdftex_path(self):
        binding = self.handler.sdftex
        reference = binding.reference or self.tr("None")
        resolved = binding.resolved_path
        self.sdftex_label.setText(f"SDFTEX: {reference}" + (f"\n{resolved}" if resolved else ""))
        self.sdftex_button.setEnabled(binding.version is not None)
        self.sdftex_button.setToolTip(self.tr("Inspect the referenced distance volume or bake a replacement.")
            if binding.version is not None else self.tr("No verified SDFTEX resource profile for this mesh build."))

    def _open_distance_field(self):
        from file_handlers.sdftex.viewer import MeshDistanceFieldDialog

        try:
            dialog = MeshDistanceFieldDialog(self.handler, self)
            dialog.exec()
            dialog.deleteLater()
        except Exception as error:
            QMessageBox.warning(self, self.tr("Distance field"), str(error))
        self._refresh_sdftex_path()
        self._data_tree_populated = False
        if self._data_panel_visible:
            self._populate_data_tree()

    def save_completed(self, _path):
        self._refresh_sdftex_path()

    def _game_lighting(self) -> bool:
        material = MeshMaterialResolver.material_profile(self.handler)
        profile = None if material is None else mmtr_profile_for_game(material.game)
        return profile is not None and light_pass_catalog_for(profile) is not None

    def _refuse_game_lighting(self, *_told) -> None:
        self.shader_preview.set_refusal(self.viewport.lit_refusal())

    def _settings_store(self) -> dict | None:
        return app_settings(getattr(self.handler, "app", None))

    def _setting_bool(self, key: str) -> bool:
        settings = self._settings_store()
        return bool(settings.get(key, False)) if settings is not None else False

    def _save_bool_setting(self, key: str, value: bool):
        self._save_setting(key, bool(value))

    def _save_setting(self, key: str, value) -> None:
        remember_setting(getattr(self.handler, "app", None), key, value)

    def _on_texture_quality_changed(self, quality: str):
        self._materials.set_texture_quality(quality)

    def _on_vertex_colors_toggled(self, checked: bool):
        self._save_bool_setting(self.VERTEX_COLORS_SETTINGS_KEY, checked)
        if self.viewport:
            self.viewport.set_vertex_colors_enabled(checked)

    def _reload_materials(self):
        if not self._closed:
            self._material_session.reload()

    def cleanup(self):
        if self._closed:
            return
        self._closed = True
        if navigation := getattr(self, "navigation", None):
            navigation.cleanup()
        if controls := getattr(self, "morph_controls", None):
            controls.stop()
        if materials := getattr(self, "_materials", None):
            materials.clear()
        for name in ("attributes_preview", "lit_preview", "textures_preview"):
            if preview := getattr(self, name, None):
                preview.set_active(False)
        if self.viewport is not None:
            self.viewport.cleanup()
        if controls := getattr(self, "attribute_controls", None):
            controls.set_view(None, None, None)
        if inspector := getattr(self, "inspector", None):
            inspector.clear()

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)

    def _open_mdf(self):
        from pathlib import Path
        resolved = self._material_session.resolved_mdf
        app = getattr(self.handler, "app", None)
        if resolved is None or app is None:
            return
        if Path(resolved.path).is_file():
            app.add_tab(resolved.path, Path(resolved.path).read_bytes())
            return
        dock = getattr(app, "proj_dock", None)
        if dock is None or not dock._open_pak_path_in_editor(resolved.path):
            QMessageBox.information(self, self.tr("Open MDF"), self.tr(
                "{path} is in the game PAKs; open the game's PAKs in the project panel to open it.").format(
                path=resolved.path))

    def _on_material_reset(self):
        self.lit_preview.scene_changed()
        self.shader_preview.retry()
        self.textures_preview.scene_changed()
        resolved_mdf = self._material_session.resolved_mdf
        self.open_mdf_button.setEnabled(resolved_mdf is not None)
        if resolved_mdf:
            self.mdf_label.setText(
                self.tr("MDF: {path}").format(path=resolved_mdf.path)
            )
        else:
            self.mdf_label.setText(self.tr("MDF: not found"))
        self._material_table_populated = False
        if self._material_panel_visible:
            self._populate_material_table()

    def _set_panel(self, panel, visible):
        if visible:
            self.sidebar.setCurrentWidget(panel)
            self.sidebar_title.setText(self._panel_titles[panel])
        self.sidebar_panel.setVisible(visible)
        if not visible and self.viewport is not None:
            self.viewport.setFocus()
        for button, page in self._panel_buttons:
            with QSignalBlocker(button):
                button.setChecked(visible and page is panel)
        self._material_panel_visible = visible and panel is self.material_panel
        self._data_panel_visible = visible and panel is self.data_panel
        if self._material_panel_visible and not self._material_table_populated:
            self._populate_material_table()
        if self._data_panel_visible and not self._data_tree_populated:
            self._populate_data_tree()

    def _populate_geometry_controls(self):
        combo = self.geometry_combo
        labels = {"main": self.tr("Main LOD {}"), "shadow": self.tr("Shadow LOD {}"),
                  "occluder": self.tr("Occluder LOD {}")}
        with QSignalBlocker(combo):
            combo.clear()
            for layer, lod in mesh_geometry_choices(self.handler.mesh):
                combo.addItem(labels[layer].format(lod), (layer, lod))
        combo.setEnabled(combo.count() > 1)
        self._populate_blend_shape_controls()
        self._populate_attribute_controls()

    def _selected_geometry(self) -> tuple[str, int]:
        value = self.geometry_combo.currentData()
        return value if isinstance(value, tuple) and len(value) == 2 else ("main", 0)

    def _populate_blend_shape_controls(self):
        mesh = self.handler.mesh
        layer, lod_index = self._selected_geometry()
        combo = self.morph_controls.combo
        previous = combo.currentData()
        previous_name = combo.itemText(combo.currentIndex()) if previous is not None else ""
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItem(self.tr("Base"), None)
            if layer == "main" and mesh is not None:
                bodies = getattr(getattr(mesh, "blend_shape_data", None), "bodies", ())
                body = next((item for item in bodies if item.lod_index == lod_index), None)
                for channel in getattr(body, "channels", ()):
                    if channel.segments:
                        combo.addItem(channel.name or self.tr("Channel {}").format(channel.index), channel.index)
            matches = [i for i in range(combo.count()) if combo.itemText(i) == previous_name]
            restore = matches[0] if len(matches) == 1 else combo.findData(previous) if matches else 0
            combo.setCurrentIndex(max(0, restore))
        self.morph_controls.refresh_enabled()
        self.morph_controls.setVisible(combo.count() > 1)

    def _populate_attribute_controls(self):
        mesh = getattr(self.handler, "mesh", None)
        layer, lod_index = self._selected_geometry()
        combo = self.attribute_combo
        previous = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self.tr("Surface"), None)
        records = []
        if mesh is not None:
            try:
                records = mesh_scene_payloads(
                    mesh,
                    lod_index=lod_index,
                    layer=layer,
                )
            except ValueError:
                records = []

        widths = {
            "normals": 3,
            "tangents": 3,
            "normal_w": 1,
            "pivot_indices": 1,
            "tangent_handedness": 1,
            "uv0": 2,
            "uv1": 2,
            "uv2": 2,
            "colors": 4,
        }

        def has_attribute(name: str) -> bool:
            profile = getattr(mesh, "profile", None)
            normal_w_semantic = getattr(
                profile,
                "vertex_normal_w_semantic",
                VertexNormalWSemantic.UNINTERPRETED,
            )
            if name == "normal_w" and normal_w_semantic is not (
                VertexNormalWSemantic.UNINTERPRETED
            ):
                return False
            if name == "pivot_indices" and normal_w_semantic is not (
                VertexNormalWSemantic.LOCAL_PIVOT_INDEX
            ):
                return False
            if name == "bone_modifier_effectors":
                modifier = getattr(
                    getattr(mesh, "blend_shape_data", None),
                    "bone_modifier",
                    None,
                )
                return bool(
                    layer == "main"
                    and lod_index == 0
                    and getattr(modifier, "effectors", ())
                )
            return payload_records_have_complete_attribute(
                records,
                name,
                widths,
            )

        for label, name in (
            (self.tr("Normals"), "normals"),
            (self.tr("Tangents"), "tangents"),
            (self.tr("Normal W (signed)"), "normal_w"),
            (self.tr("Pivot assignment"), "pivot_indices"),
            (self.tr("Tangent handedness"), "tangent_handedness"),
            ("UV0", "uv0"),
            ("UV1", "uv1"),
            ("UV2", "uv2"),
            (self.tr("Vertex colors"), "colors"),
            (self.tr("Primary weights"), "skin_weights"),
            (self.tr("Extra weights"), "extra_skin_weights"),
            (self.tr("Additional vertex weights"), "additional_vertex_weights"),
            (self.tr("Bone-modifier effectors"), "bone_modifier_effectors"),
            (self.tr("Shape-key / secondary weights"), "secondary_skin_weights"),
        ):
            if has_attribute(name):
                combo.addItem(label, name)
        restore = combo.findData(previous)
        combo.setCurrentIndex(max(0, restore))
        combo.blockSignals(False)
        combo.setEnabled(combo.count() > 1)
        self.vertex_colors_action.setEnabled(combo.currentData() is None)

    def _on_geometry_changed(self, _index: int):
        self._populate_blend_shape_controls()
        self._populate_attribute_controls()
        self._refresh_geometry()
        self.shader_preview.retry()

    def _on_attribute_changed(self, _value: int):
        self.vertex_colors_action.setEnabled(
            self.attribute_combo.currentData() is None
        )
        self._refresh_geometry()

    def _refresh_geometry(self):
        if self.viewport is None or self._closed:
            return
        layer, lod_index = self._selected_geometry()
        try:
            self.viewport.set_geometry(
                layer,
                lod_index,
                blend_shape_channel=self.morph_controls.combo.currentData(),
                blend_shape_weight=self.morph_controls.weight,
                visualize_attribute=self.attribute_combo.currentData(),
            )
        except (ValueError, IndexError, KeyError) as error:
            self.morph_controls.stop()
            self.preview_status.setText(self.tr("Cannot preview {geometry}: {error}").format(
                geometry=self.geometry_combo.currentText(), error=error))
            self.preview_status.show()
            self.inspector.setEnabled(False)
            self.attribute_controls.setEnabled(False)
            self.viewport.hide()
        else:
            self.preview_status.hide()
            self.attribute_controls.setEnabled(True)
            self.inspector.set_inspection(self.viewport.inspection)
            self.attribute_controls.set_view(
                self.viewport._preview_scene.attribute, self.handler.mesh, self.attribute_combo.currentData())
            self.viewport.show()

    def _frame_inspection(self):
        if self.inspector.selected_parts():
            self.inspector.frame_selection()
        elif self.viewport is not None:
            self.viewport.fit_geometry()

    def _on_overlay_toggled(self, _checked: bool):
        if self.viewport is not None:
            self.viewport.set_semantic_overlays(
                bounds=self.bounds_action.isChecked(),
                pivots=self.pivots_action.isChecked(),
            )

    @staticmethod
    def _tree_item(parent, name: str, value="") -> QTreeWidgetItem:
        return QTreeWidgetItem(parent, [str(name), str(value)])

    def _field_rows(self, parent, owner, names) -> None:
        for name in names:
            value = getattr(owner, name)
            self._tree_item(parent, name, f"0x{value:X}" if name.endswith("_offset") else value)

    def _populate_wilds_skin_storage(self, parent, weights) -> None:
        declared = int(weights.declared_influence_count)
        self._tree_item(parent, "declared SkinWeightCount", declared)
        self._tree_item(parent, "effective runtime influences", weights.influence_count)
        self._tree_item(parent, "stored runtime weight lanes", weights.stored_weight_count)
        if weights.implicit_last_weight:
            self._tree_item(
                parent,
                "final runtime weight",
                "UNORM residual: (255 - sum(stored lanes)) / 255",
            )
        elif declared == 4:
            self._tree_item(
                parent,
                "runtime weight normalization",
                "three stored UNORM lanes divided by their sum",
            )

        ignored = weights.runtime_ignored_weight_bytes
        if ignored:
            ignored_stride = (
                weights.native_weight_capacity - weights.stored_weight_count
            )
            lanes = self._tree_item(
                parent,
                "Runtime-unread producer lanes",
                f"{ignored_stride} byte lane(s) per vertex",
            )
            if declared == 12 and ignored_stride == 5:
                labels = (
                    "continuation byte 11 / non-authoritative producer weight cache (runtime derives weight 12)",
                    "continuation byte 12 / corpus-zero reserve",
                    "continuation byte 13 / corpus-zero reserve",
                    "continuation byte 14 / corpus-zero reserve",
                    "continuation byte 15 / producer residue",
                )
            elif declared in (6, 8) and ignored_stride == 3:
                labels = (
                    "primary byte 13 / non-authoritative producer weight cache (runtime derives weight 6)",
                    "primary byte 14 / corpus-zero reserve",
                    "primary byte 15 / corpus-zero reserve",
                )
            else:
                labels = tuple(
                    f"runtime-unread byte lane {index}"
                    for index in range(ignored_stride)
                )
            for lane_index, label in enumerate(labels):
                lane = ignored[lane_index::ignored_stride]
                self._tree_item(
                    lanes,
                    label,
                    f"{len(lane)} bytes; {sum(value != 0 for value in lane)} nonzero",
                )

        tags = weights.packed_index_reserved_bits
        if tags:
            tag_words = max(int(weights.packed_index_word_count), 1)
            tags_by_vertex = np.frombuffer(tags, dtype=np.uint8).reshape(
                -1,
                tag_words,
            )
            tag_root = self._tree_item(
                parent,
                "Packed-index upper two-bit tags",
                f"{tag_words} word(s) per vertex; runtime masks them from indices",
            )
            for word_index in range(tag_words):
                self._tree_item(
                    tag_root,
                    f"word {word_index}",
                    sorted(set(map(int, tags_by_vertex[:, word_index]))),
                )

    def _populate_skin_storage(self, parent, weights) -> None:
        game = self.handler.mesh.profile.game
        if game == "MHWS":
            self._populate_wilds_skin_storage(parent, weights)
            return
        trailing = weights.trailing_weight_bytes
        if not trailing:
            return
        if game != "SF6":
            self._tree_item(
                parent,
                "unresolved trailing bytes",
                f"{len(trailing)} bytes, {sum(value != 0 for value in trailing)} nonzero",
            )
            return

        self._tree_item(
            parent,
            "stored/runtime lanes",
            f"{weights.stored_weight_count} stored UNORM weights; final weight = 1 - stored sum",
        )
        self._tree_item(
            parent,
            "jointWeights2.y / explicit weight 5",
            f"{len(weights.explicit_sixth_weight_bytes)} bytes; "
            "SF6 derives this weight instead of reading it",
        )
        for label, values, suffix in (
            ("jointWeights2.z / unused weight 6", weights.unused_seventh_weight_bytes, ""),
            (
                "jointWeights2.w / unused weight 7",
                weights.unused_eighth_weight_bytes,
                "; ignored by the six-weight path",
            ),
        ):
            self._tree_item(
                parent,
                label,
                f"{len(values)} bytes; {sum(value != 0 for value in values)} nonzero{suffix}",
            )
        self._tree_item(
            parent,
            "jointIndicesDec_reserved0/1",
            f"{len(weights.packed_index_reserved_bits)} fields; "
            f"raw values={sorted(set(weights.packed_index_reserved_bits))}; "
            "runtime masks both reserved fields out",
        )

    def _populate_mply_tree(self, tree, mesh_buffer) -> None:
        layout = getattr(mesh_buffer, "meshlet_layout", None)
        bvh = getattr(mesh_buffer, "meshlet_bvh", None)
        if layout is None:
            return

        root = self._tree_item(
            tree,
            self.tr("MPLY meshlet data"),
            f"GPU blob=0x{layout.gpu_meshlet_offset:X} + 0x{layout.gpu_data_size:X}",
        )
        for name, value in (
            ("GPU blob base", f"0x{layout.gpu_meshlet_offset:X}"),
            ("GPU blob size", layout.gpu_data_size),
            (
                "GPU blob end",
                f"0x{layout.gpu_meshlet_offset + layout.gpu_data_size:X}",
            ),
            ("layout reserve", layout.reserved),
            ("resident/GPU header mirror", layout.gpu_header_matches_resident),
        ):
            self._tree_item(root, name, value)

        header = layout.header
        header_root = self._tree_item(
            root,
            "MeshletHeader",
            f"{header.lod_num} LODs; valid=0x{header.valid_lod_bits:X}",
        )
        for name, value in (
            ("minimum AABB", header.min_aabb),
            ("maximum AABB", header.max_aabb),
            ("loaded LOD (serialized runtime state)", header.loaded_lod),
            ("resident LOD (serialized runtime state)", header.resident_lod),
            ("allocation LOD (serialized runtime state)", header.allocation_lod),
        ):
            self._tree_item(header_root, name, value)
        if header.lod_factors:
            self._tree_item(
                header_root,
                "active LOD factors",
                header.active_lod_factors,
            )
            if header.packed_binary16_lod_factor_lanes:
                self._tree_item(
                    header_root,
                    "serialized binary16 lanes (inactive lanes are producer-only)",
                    header.packed_binary16_lod_factor_lanes,
                )
                self._tree_item(
                    header_root,
                    "serialized meshlet raster switch threshold",
                    f"{header.serialized_meshlet_raster_switch_threshold} "
                    f"({header.serialized_meshlet_raster_switch_triangle_count} triangles)",
                )
                self._tree_item(
                    header_root,
                    "serialized threshold reserve words",
                    header.serialized_lod_payload_reserved_words,
                )
        else:
            self._tree_item(
                header_root,
                "CPU-unused serialized words",
                header.serialized_cpu_unused_words,
            )

        regions_root = self._tree_item(
            root,
            "Serialized LOD regions",
            len(layout.lod_regions),
        )
        for region in layout.lod_regions:
            region_root = self._tree_item(
                regions_root,
                f"LOD {region.lod_index}",
                f"clusters={region.cluster_count}, "
                f"table=0x{region.table_absolute_offset:X}, "
                f"end=0x{region.region_end_absolute:X}",
            )
            self._tree_item(
                region_root,
                "blob-relative table offset",
                f"0x{region.table_relative_offset:X}",
            )
            self._tree_item(
                region_root,
                "chunk offsets",
                (
                    f"{len(region.chunk_relative_offsets)} strictly ordered; "
                    f"first=0x{region.chunk_relative_offsets[0]:X}, "
                    f"last=0x{region.chunk_relative_offsets[-1]:X}"
                    if region.chunk_relative_offsets
                    else "empty"
                ),
            )

        pivot_info = layout.group_pivot_info
        if pivot_info is not None:
            pivot_root = self._tree_item(
                root,
                "MPLY group-pivot descriptor",
                f"{pivot_info.pivot_count if pivot_info.present else 0} Float3 pivots",
            )
            for name, value in (
                ("serialized count", pivot_info.serialized_count),
                ("blob-relative offset", f"0x{pivot_info.blob_relative_offset:X}"),
                ("layout/blob mirror", pivot_info.layout_mirror_matches_blob),
                (
                    "MeshHeader relative-offset mirror",
                    pivot_info.header_offset_matches_internal,
                ),
                ("reserve u16", pivot_info.reserve_u16),
                ("reserve u32", pivot_info.reserve_u32),
            ):
                self._tree_item(pivot_root, name, value)
            self._tree_item(
                pivot_root,
                "MeshHeader blob-relative offset",
                f"0x{pivot_info.header_blob_relative_offset:X}",
            )

        if bvh is not None:
            clusters = list(getattr(mesh_buffer, "mply_cluster_headers", ()))
            chunks = list(getattr(mesh_buffer, "mply_chunks", ()))
            bvh_root = self._tree_item(
                root,
                "MeshletBVH",
                f"{sum(bvh.cluster_counts)} clusters, "
                f"{len(bvh.quantized_aabb_centers)} quantized centers",
            )
            for name, value in (
                ("compacted-cluster table", f"0x{bvh.gpu_cluster_headers:X}"),
                ("quantized-center table", f"0x{bvh.gpu_cluster_quantize_offset:X}"),
                ("center origin", bvh.offset),
                ("center scale", bvh.scale),
                ("nested BVH offsets", bvh.meshlet_bvh_offsets),
            ):
                self._tree_item(bvh_root, name, value)
            cursor = 0
            for lod_index, count in enumerate(bvh.cluster_counts):
                if not count:
                    continue
                exponent = bvh.cluster_bounds_scale_exponents[lod_index]
                unit = bvh.cluster_bounds_quantization_units[lod_index]
                lod_clusters = clusters[cursor : cursor + count]
                lod_chunks = chunks[cursor : cursor + count]
                cursor += count
                lod_root = self._tree_item(
                    bvh_root,
                    f"LOD {lod_index}",
                    f"clusters={count}, byteOffset={bvh.cluster_offsets[lod_index]}, "
                    f"boundsExponent={exponent}, unit=2^(-16-({exponent}))={unit:g}",
                )
                position_modes = Counter(
                    int(item.position_compress_level) for item in lod_clusters
                )
                tangent_modes = Counter(
                    int(item.flags.tangent_handedness_mode)
                    for item in lod_chunks
                    if item.flags is not None
                )
                self._tree_item(lod_root, "position compression modes", dict(position_modes))
                self._tree_item(lod_root, "tangent orientation modes", dict(tangent_modes))
                decoded_bounds = [
                    item.culling_aabb
                    for item in lod_clusters
                    if item.culling_aabb is not None
                ]
                if decoded_bounds:
                    minimum = np.min(
                        np.asarray([item.minimum for item in decoded_bounds]),
                        axis=0,
                    )
                    maximum = np.max(
                        np.asarray([item.maximum for item in decoded_bounds]),
                        axis=0,
                    )
                    half_extents = [item.half_extent for item in decoded_bounds]
                    self._tree_item(
                        lod_root,
                        "decoded runtime culling bounds",
                        f"union min={tuple(map(float, minimum))}, "
                        f"max={tuple(map(float, maximum))}; "
                        f"halfExtent={min(half_extents):g}..{max(half_extents):g}",
                    )

            flag_root = self._tree_item(
                root,
                "Chunk stream flags",
                f"{len(mesh_buffer.mply_chunk_flags)} decoded flag words",
            )
            for field in (
                "is_meshlet_compressed_normal",
                "is_meshlet_compressed_texcoord1",
                "is_meshlet_compressed_texcoord2",
                "is_meshlet_compressed_texcoord3",
                "is_meshlet_compressed_vertex_color",
                "is_meshlet_no_tangent",
                "is_meshlet_use_skinned",
                "is_meshlet_compressed_skinned",
                "is_meshlet_use_vertex_color",
                "is_meshlet_use_texcoord2",
                "is_meshlet_use_texcoord3",
                "shader_unused_producer_bit7",
            ):
                enabled = sum(
                    bool(getattr(flags, field))
                    for flags in mesh_buffer.mply_chunk_flags
                )
                self._tree_item(flag_root, field, f"{enabled} enabled")
            trailing_bytes = sum(
                len(chunk.trailing_payload) for chunk in mesh_buffer.mply_chunks
            )
            tangent_mask_bytes = sum(
                len(chunk.tangent_mask_bytes) for chunk in mesh_buffer.mply_chunks
            )
            self._tree_item(
                flag_root,
                "per-vertex tangent-orientation masks",
                f"{tangent_mask_bytes} bytes",
            )
            self._tree_item(
                flag_root,
                "unparsed trailing payload",
                f"{trailing_bytes} bytes",
            )

        parts = getattr(mesh_buffer, "meshlet_parts_layout", None)
        if parts is not None:
            parts_root = self._tree_item(
                root,
                "MeshletPartsLayout",
                f"{parts.parts_num} semantic part IDs",
            )
            self._tree_item(
                parts_root,
                "part-ID table offset",
                f"0x{parts.parts_indices_offset:X}",
            )
            self._tree_item(parts_root, "part IDs", parts.parts_indices)
            self._tree_item(parts_root, "reserve", parts.reserve)

    def _populate_data_tree(self):
        self._data_tree_populated = True
        tree = self.data_tree
        tree.clear()
        mesh = getattr(self.handler, "mesh", None)
        if mesh is None:
            return
        header = mesh.header
        profile = getattr(mesh, "profile", None)
        overview = self._tree_item(tree, self.tr("Overview"))
        for name, value in (
            ("Load mode", mesh.load_mode.value),
            ("Version profile", getattr(profile, "display_name", "")),
            ("Format", header.format_version.name),
            ("Magic", f"0x{header.magic:08X}"),
            ("Internal version", header.version),
            ("Declared file size", header.file_size),
            ("LOD group hash", f"0x{header.lod_hash:08X}"),
            ("Strings", len(mesh.names)),
            ("Materials", len(mesh.material_names)),
        ):
            self._tree_item(overview, name, value)

        if profile is not None:
            profile_data = self._tree_item(tree, self.tr("Version profile"))
            for name, value in (
                ("Name", profile.display_name),
                ("Profile ID", profile.game),
                ("Header layout", profile.header_layout.value),
                ("Buffer layout", profile.buffer_layout.value),
                ("Blend-shape layout", profile.blend_shape_layout.value),
                ("Normal-recalc layout", profile.normal_recalc_layout.value),
                ("Skin-weight layout", profile.skin_weight_layout.value),
                ("Skin influences", profile.skin_weight_influence_count),
                ("Streaming buffers", profile.supports_streaming_buffers),
                ("Secondary weights", profile.supports_secondary_skin_weights),
                (
                    "Additional vertex weights",
                    profile.supports_additional_vertex_weights,
                ),
            ):
                self._tree_item(profile_data, name, value)
            pairs = self._tree_item(
                profile_data,
                self.tr("Accepted version pairs"),
                len(profile.version_pairs),
            )
            for internal_version, file_version in sorted(profile.version_pairs):
                self._tree_item(
                    pairs,
                    internal_version,
                    file_version,
                )

        header_data = self._tree_item(tree, self.tr("Header metadata"))
        is_dd2 = bool(
            profile is not None
            and profile.buffer_layout is MeshBufferLayout.DD2
        )
        is_wilds = bool(
            profile is not None
            and profile.buffer_layout is MeshBufferLayout.WILDS
        )
        is_modern_header = is_dd2 or is_wilds
        header_fields = ["name_count"]
        corpus_zero_legacy = bool(
            profile is not None
            and profile.legacy_reserved_storage_is_corpus_zero
        )
        if is_modern_header:
            header_fields[:0] = [
                "content_flags",
                "light_map_metadata_raw",
                "light_map_reserved",
                "light_map_used",
                "light_map_width",
                "light_map_height",
            ]
            if is_dd2:
                header_fields.append("light_map_padding")
                header_fields.append("on_memory_size")
            else:
                header_fields.append("gpu_buffer_base_offset")
        elif not corpus_zero_legacy:
            header_fields[:0] = [
                "flags",
                "legacy_flags",
                "legacy_solved_offset",
                "legacy_light_map_raw",
                "light_map_reserved",
                "light_map_used",
                "light_map_width",
                "light_map_height",
                "ukn",
            ]
        else:
            if header.legacy_solved_offset:
                header_fields.append("legacy_solved_offset")
            if (
                header.legacy_light_map_raw
                and getattr(profile, "legacy_light_map_layout", None)
                is not LegacyLightMapLayout.RE8
            ):
                header_fields.append("legacy_light_map_raw")
        if header.header_pad:
            header_fields.append("header_pad")
        for name in header_fields:
            self._tree_item(header_data, name, getattr(header, name))
        if (
            header.light_map_metadata_raw
            or (
                profile is not None
                and profile.legacy_light_map_layout is LegacyLightMapLayout.RE8
                and header.legacy_light_map_raw
            )
        ):
            light_map = self._tree_item(
                header_data,
                self.tr("Light map"),
                f"{header.light_map_width} × {header.light_map_height}",
            )
            self._tree_item(light_map, self.tr("Used"), bool(header.light_map_used))
            self._tree_item(
                light_map,
                self.tr("Reserved allocation"),
                bool(header.light_map_reserved),
            )

        offsets = self._tree_item(tree, self.tr("Header offsets"))
        if is_wilds and header.gpu_buffer_base_offset:
            self._tree_item(
                offsets,
                "gpu_buffer_base_offset",
                f"0x{int(header.gpu_buffer_base_offset):X}",
            )
        for name, value in vars(header).items():
            if is_wilds and name == "vertices_offset":
                continue
            if name.endswith("_offset") and value:
                self._tree_item(offsets, name, f"0x{int(value):X}")
        flags = getattr(header, "decoded_content_flags", None)
        if flags is not None and flags.raw:
            flag_root = self._tree_item(
                tree,
                self.tr("Enabled mesh features"),
                f"0x{flags.raw:06X}",
            )
            for name in flags.__dataclass_fields__:
                value = getattr(flags, name)
                if name != "raw" and value:
                    self._tree_item(flag_root, name, value)

        geometry = self._tree_item(tree, self.tr("Geometry"))
        for data_index, mesh_data in enumerate(mesh.meshes):
            data_root = self._tree_item(
                geometry,
                f"Main mesh {data_index}",
                f"{len(mesh_data.lods)} LODs, materials={mesh_data.material_count}, "
                f"UVs={mesh_data.uv_count}, "
                f"skinWeights={mesh_data.skin_weight_influence_count or 'none'} "
                f"(raw=0x{mesh_data.skin_weight_count_raw:02X})",
            )
            geometry_fields = [
                ("totalClusterCount", mesh_data.total_cluster_count),
                ("maxDrawCallCount", mesh_data.max_draw_call_count),
                ("32-bit indices", mesh_data.has_32bit_index_buffer),
                ("LOD table", f"0x{mesh_data.lod_offsets_offset:X}"),
                ("LOD offsets", [f"0x{value:X}" for value in mesh_data.lod_offsets]),
                ("bounding sphere", mesh_data.bounding_sphere),
                ("bounding box", mesh_data.bounding_box),
            ]
            if mesh_data.skin_weight_runtime_ignored_tag:
                geometry_fields.insert(
                    2,
                    (
                        "runtime-ignored producer tag",
                        f"0x{mesh_data.skin_weight_runtime_ignored_tag:02X}",
                    ),
                )
            if is_modern_header:
                geometry_fields.extend(
                    (
                        ("reserve", f"0x{mesh_data.reserve:02X}"),
                        ("AABB-min padding", f"0x{mesh_data.aabb_min_padding:08X}"),
                        ("chunk", mesh_data.chunk),
                        ("AABB-max padding", f"0x{mesh_data.aabb_max_padding:06X}"),
                    )
                )
            if not corpus_zero_legacy or mesh_data.shared_lod_bits:
                geometry_fields.insert(
                    2,
                    ("sharedLodBits", f"0x{mesh_data.shared_lod_bits:02X}"),
                )
            if mesh_data.ukn1:
                geometry_fields.insert(
                    3,
                    ("legacy reserved qword", f"0x{mesh_data.ukn1:016X}"),
                )
            for name, value in geometry_fields:
                self._tree_item(data_root, name, value)
            for lod_index, lod in enumerate(mesh_data.lods):
                lod_root = self._tree_item(
                    data_root, f"LOD {lod_index}", _lod_summary(lod, profile, corpus_zero_legacy)
                )
                self._tree_item(lod_root, "file offset", f"0x{lod.file_offset:X}")
                self._tree_item(
                    lod_root, "group offsets table", f"0x{lod.group_offsets_offset:X}"
                )
                self._tree_item(
                    lod_root, "group offsets", [f"0x{value:X}" for value in lod.group_offsets]
                )
                for group in lod.mesh_groups:
                    group_index_summary = (
                        f"{group.drawable_index_count} drawable / "
                        f"{group.index_span_count} allocated"
                        if is_wilds
                        else str(group.face_count)
                    )
                    group_root = self._tree_item(
                        lod_root,
                        f"Part {group.group_id}",
                        f"vertices={group.vertex_count}, indices={group_index_summary}",
                    )
                    self._tree_item(group_root, "file offset", f"0x{group.file_offset:X}")
                    if any(group.reserve):
                        self._tree_item(
                            group_root,
                            "unexpected reserved bytes",
                            group.reserve.hex(" "),
                        )
                    for sub_index, submesh in enumerate(group.submeshes):
                        sub_root = self._tree_item(
                            group_root,
                            f"Submesh {sub_index}",
                            _submesh_summary(submesh, profile, is_wilds),
                        )
                        self._tree_item(
                            sub_root,
                            "file offset",
                            f"0x{submesh.file_offset:X}",
                        )
                        if (
                            profile is not None
                            and profile.submesh_has_streaming_offsets
                        ) or submesh.streaming_offset:
                            self._tree_item(
                                sub_root,
                                "streaming byte offset",
                                submesh.streaming_offset,
                            )
                        if submesh.streaming_offset2:
                            self._tree_item(
                                sub_root,
                                "platform streaming byte offset",
                                submesh.streaming_offset2,
                            )
                        if is_wilds:
                            self._tree_item(
                                sub_root,
                                "runtime-ignored reserve byte +0x03 "
                                "(native reserve[1], canonical value 0)",
                                submesh.runtime_ignored_reserve03,
                            )
                        elif submesh.reserve:
                            self._tree_item(
                                sub_root,
                                "unexpected reserved value",
                                submesh.reserve,
                            )
                        if (
                            profile is not None
                            and profile.submesh_has_runtime_ignored_tail_residue
                        ):
                            self._tree_item(
                                sub_root,
                                "runtime-ignored producer residue",
                                f"0x{submesh.runtime_ignored_tail_residue:08X}",
                            )

        mesh_buffer = getattr(mesh, "mesh_buffer", None)
        if mesh_buffer is not None:
            buffers = self._tree_item(
                tree,
                self.tr("Vertex/index buffers"),
                f"resident + {len(mesh_buffer.streaming_buffer_headers)} streamed",
            )
            layout = self._tree_item(buffers, "Resident buffer layout")
            if is_dd2:
                layout_fields = [
                    "element_list_offset",
                    "universal_offset",
                    "additional_vertex_weights_offset",
                    "universal_buffer_size",
                    "index_buffer_offset_size",
                    "element_count",
                    "total_element_count",
                    "shadow_mesh_index_buffer_offset",
                    "occluder_mesh_index_buffer_offset",
                    "blend_shape_vertex_buffer_offset",
                    "additional_vertex_weights_size",
                    "chunk",
                    "reserved",
                ]
            elif is_wilds:
                layout_fields = [
                    "element_headers_offset",
                    "vertex_buffer_offset",
                    "shapekey_weight_buffer_offset",
                    "total_buffer_size",
                    "vertex_buffer_size",
                    "index_buffer_offset",
                    "index_buffer_size",
                    "element_count",
                    "total_element_count",
                    "shadow_mesh_index_buffer_offset",
                    "occluder_mesh_index_buffer_offset",
                    "normal_recalc_local_index_buffer_offset",
                    "normal_recalc_redirected_list_offset",
                    "blend_shape_offset",
                    "wilds_reserved",
                    "buffer_index",
                ]
            else:
                layout_fields = [
                    "element_headers_offset",
                    "vertex_buffer_offset",
                    "index_buffer_offset",
                    "vertex_buffer_size",
                    "index_buffer_size",
                    "element_count",
                    "total_element_count",
                    "shadow_mesh_index_buffer_offset",
                    "occluder_mesh_index_buffer_offset",
                ]
            if not is_modern_header and (
                profile.has_shape_key_weight_pointer and not profile.shape_key_weight_pointer_is_corpus_zero
            ):
                layout_fields.insert(3, "shapekey_weight_buffer_offset")
            if not is_modern_header and not profile.resident_buffer_tail_is_corpus_zero:
                layout_fields.extend(
                    (
                        "buffer_index_unkn",
                        "buffer_index",
                        "unknown_struct",
                    )
                )
            if not is_modern_header and not profile.legacy_buffer_sizes_are_vertex_and_index:
                layout_fields.insert(4, "total_buffer_size")
                if mesh_buffer.legacy_secondary_buffer_size:
                    layout_fields.insert(5, "legacy_secondary_buffer_size")
            self._field_rows(layout, mesh_buffer, layout_fields)
            blend_payload_file_offset = (
                mesh_buffer.vertex_buffer_offset + mesh_buffer.blend_shape_offset
            )
            if mesh.header.blend_shapes_offset and blend_payload_file_offset > 0:
                self._tree_item(
                    layout,
                    "blend shape payload offset",
                    f"0x{blend_payload_file_offset:X}",
                )
            if (
                not profile.legacy_buffer_sizes_are_vertex_and_index
                or mesh.gpu_index_buffer_size != mesh_buffer.index_buffer_size
            ):
                self._tree_item(
                    layout,
                    "GPU index buffer size (declared/derived)",
                    mesh.gpu_index_buffer_size,
                )
            if mesh_buffer.extra_buffer_headers:
                extras = self._tree_item(
                    layout,
                    "Auxiliary vertex elements",
                    len(mesh_buffer.extra_buffer_headers),
                )
                for index, item in enumerate(mesh_buffer.extra_buffer_headers):
                    self._tree_item(
                        extras,
                        str(index),
                        f"type={int(item.type)}, stride={item.size}, offset=0x{item.offset:X}",
                    )

            if mesh.streaming_info is not None:
                streaming = self._tree_item(
                    buffers,
                    "Streaming info",
                    f"entries={len(mesh.streaming_info.entries)}, loaded={mesh.streaming_data_loaded}",
                )
                self._tree_item(streaming, "pad", mesh.streaming_info.pad)
                self._tree_item(
                    streaming,
                    "entries offset",
                    f"0x{mesh.streaming_info.entries_offset:X}",
                )
                for index, entry in enumerate(mesh.streaming_info.entries):
                    item = self._tree_item(
                        streaming,
                        f"Entry {index}",
                        f"start=0x{entry.start:X}, bytes={entry.size}",
                    )
                    header_item = next(
                        (
                            header
                            for header in mesh_buffer.streaming_buffer_headers.values()
                            if header.entry_index == index
                        ),
                        None,
                    )
                    if header_item is not None:
                        header_root = self._tree_item(
                            item,
                            f"Buffer {header_item.buffer_index}",
                            f"vertexBytes={header_item.vertex_buffer_length}, "
                            f"totalBytes={header_item.unpadded_buffer_size}, "
                            f"elements={header_item.main_vertex_element_count}/"
                            f"{header_item.total_vertex_element_count}, "
                            f"blendShape=0x{header_item.blend_shape_offset:X}",
                        )
                        if is_wilds:
                            for name, value in (
                                (
                                    "normal-recalc local-index offset",
                                    header_item.normal_recalc_local_index_buffer_offset,
                                ),
                                (
                                    "normal-recalc redirected-list offset",
                                    header_item.normal_recalc_redirected_list_offset,
                                ),
                                ("blend-shape offset", header_item.blend_shape_offset),
                                ("reserved", header_item.wilds_reserved),
                                ("one-based buffer index", header_item.buffer_index),
                            ):
                                shown = (
                                    f"0x{int(value):X}"
                                    if "offset" in name
                                    else value
                                )
                                self._tree_item(header_root, name, shown)
            for buffer_index, payload in mesh_buffer.buffer_payloads.items():
                root = self._tree_item(
                    buffers,
                    f"Buffer {buffer_index}",
                    f"vertices={payload.vertex_count}, indices="
                    f"{len(payload.index_values)}",
                )
                for name, value in (
                    ("source offset", f"0x{payload.source_offset:X}"),
                    ("vertex bytes", len(payload.vertex_bytes)),
                    ("index bytes", len(payload.face_bytes)),
                    ("positions", payload.vertex_count if payload.positions else 0),
                    ("normals", payload.vertex_count if payload.normals else 0),
                    ("tangents", payload.vertex_count if payload.tangents else 0),
                    ("UV0", payload.vertex_count if payload.uv0 else 0),
                    ("UV1", payload.vertex_count if payload.uv1 else 0),
                    ("UV2", payload.vertex_count if payload.uv2 else 0),
                    ("colors", payload.vertex_count if payload.colors else 0),
                ):
                    self._tree_item(root, name, value)
                scalar_attributes = [
                    (
                        "tangent handedness (R8_SNORM)",
                        payload.tangent_handedness,
                    ),
                ]
                normal_w_semantic = getattr(
                    profile,
                    "vertex_normal_w_semantic",
                    VertexNormalWSemantic.UNINTERPRETED,
                )
                if normal_w_semantic is VertexNormalWSemantic.LOCAL_PIVOT_INDEX:
                    scalar_attributes.insert(
                        0,
                        ("local pivot-table index", payload.pivot_indices),
                    )
                elif normal_w_semantic is VertexNormalWSemantic.UNINTERPRETED:
                    scalar_attributes.insert(
                        0,
                        ("normal W (R8_SNORM)", payload.normal_w),
                    )
                for name, values in scalar_attributes:
                    if values:
                        self._tree_item(
                            root,
                            name,
                            f"{len(values)} values, min={min(values):g}, "
                            f"max={max(values):g}",
                        )
                for stream in payload.attribute_streams:
                    self._tree_item(
                        root,
                        stream.semantic,
                        f"stride={stream.stride}, offset=0x{stream.offset:X}, bytes={len(stream.data)}",
                    )
                if payload.skin_weights is not None:
                    weight_root = self._tree_item(
                        root,
                        "Skin weights",
                        f"{payload.skin_weights.influence_count} influences × "
                        f"{payload.skin_weights.vertex_count} vertices"
                        + (
                            "; final weight reconstructed by the runtime"
                            if payload.skin_weights.implicit_last_weight
                            else ""
                        ),
                    )
                    self._populate_skin_storage(weight_root, payload.skin_weights)
                if payload.extra_skin_weights is not None:
                    weight_root = self._tree_item(
                        root,
                        "Extra skin weights",
                        f"{payload.extra_skin_weights.influence_count} influences × "
                        f"{payload.extra_skin_weights.vertex_count} vertices",
                    )
                    self._populate_skin_storage(weight_root, payload.extra_skin_weights)
                if payload.additional_vertex_weights is not None:
                    weights = payload.additional_vertex_weights
                    self._tree_item(
                        root,
                        "Additional vertex weights",
                        f"complete alternative stream; "
                        f"{weights.influence_count} influences × "
                        f"{weights.vertex_count} vertices, "
                        f"{len(payload.additional_vertex_weight_bytes)} bytes; "
                        f"{weights.stored_weight_count} stored lanes + residual",
                    )
                if payload.secondary_skin_weights is not None:
                    weight_root = self._tree_item(
                        root,
                        "Shape-key / secondary weights",
                        f"{payload.secondary_skin_weights.influence_count} influences × "
                        f"{payload.secondary_skin_weights.vertex_count} vertices, "
                        f"{len(payload.secondary_skin_weight_bytes)} bytes",
                    )
                    self._populate_skin_storage(weight_root, payload.secondary_skin_weights)
                for raw_type, blocks in payload.unknown_attributes.items():
                    self._tree_item(
                        root,
                        f"Raw attribute {raw_type}",
                        f"{len(blocks)} block(s), {sum(map(len, blocks))} bytes",
                    )

            self._populate_mply_tree(tree, mesh_buffer)

        for layer_name, layer in (
            (self.tr("Shadow mesh"), getattr(mesh, "shadow_mesh", None)),
            (self.tr("Occluder mesh"), getattr(mesh, "occluder_mesh", None)),
        ):
            if layer is not None:
                layer_root = self._tree_item(
                    tree,
                    layer_name,
                    f"vertices={layer.vertex_count}, indices={layer.index_count}, "
                    f"shares main={layer.shares_main_payload}",
                )
                self._tree_item(
                    layer_root,
                    "vertices by buffer",
                    layer.vertex_counts_by_buffer,
                )
                self._tree_item(
                    layer_root,
                    "index spans by buffer",
                    layer.index_counts_by_buffer,
                )
                payload_root = self._tree_item(
                    layer_root,
                    "Decoded payloads",
                    len(layer.payloads),
                )
                for buffer_index, payload in sorted(layer.payloads.items()):
                    self._tree_item(
                        payload_root,
                        f"Buffer {buffer_index}",
                        f"vertices={payload.vertex_count}, "
                        f"indices={len(payload.index_values)}, "
                        f"source=0x{payload.source_offset:X}",
                    )
                if hasattr(layer, "material_count"):
                    self._field_rows(layer_root, layer, ("material_count", "uv_count", "skin_weight_count",
                                                         "total_mesh_count", "lod_offsets_offset"))
                    for name in ("reserve", "dmc5_pad", "reserved_slots"):
                        value = getattr(layer, name)
                        if value and (not isinstance(value, tuple) or any(value)):
                            self._tree_item(
                                layer_root,
                                f"unexpected {name.replace('_', ' ')}",
                                value,
                            )
                else:
                    occluder_fields = ["part_count", "lod_factor", "part_offsets_offset"]
                    if not corpus_zero_legacy or layer.vertex_format:
                        occluder_fields.insert(1, "vertex_format")
                    if not corpus_zero_legacy or layer.lod_level:
                        occluder_fields.insert(2, "lod_level")
                    self._field_rows(layer_root, layer, occluder_fields)
                    if layer.reserve:
                        self._tree_item(
                            layer_root,
                            "unexpected reserved value",
                            layer.reserve,
                        )
                for lod_index, lod in enumerate(layer.geometry_lods):
                    lod_root = self._tree_item(
                        layer_root, f"LOD {lod_index}", _lod_summary(lod, profile, corpus_zero_legacy)
                    )
                    for group in lod.mesh_groups:
                        group_root = self._tree_item(
                            lod_root,
                            f"Part {group.group_id}",
                            f"vertices={group.vertex_count}, indices={group.face_count}",
                        )
                        for sub_index, submesh in enumerate(group.submeshes):
                            self._tree_item(
                                group_root,
                                f"Submesh {sub_index}",
                                _submesh_summary(submesh, profile),
                            )

        skeleton = self._tree_item(
            tree,
            self.tr("Skeleton"),
            f"joints={mesh.joint_count}, deform remap={mesh.bone_remap_count}",
        )
        if any(mesh.skeleton_reserved):
            self._tree_item(
                skeleton,
                "unexpected reserved values",
                mesh.skeleton_reserved,
            )
        for name, value in (
            ("hierarchy offset", f"0x{mesh.skeleton_hierarchy_offset:X}"),
            ("local matrices offset", f"0x{mesh.skeleton_local_matrix_offset:X}"),
            ("world matrices offset", f"0x{mesh.skeleton_world_matrix_offset:X}"),
            (
                "inverse-bind matrices offset",
                f"0x{mesh.skeleton_inverse_bind_matrix_offset:X}",
            ),
            ("deform remap indices", mesh.bone_remap_indices),
            ("bone name indices", mesh.bone_indices),
        ):
            self._tree_item(skeleton, name, value)
        for index, bone in enumerate(mesh.bones):
            name = joint_name(mesh, index)
            bone_root = self._tree_item(
                skeleton,
                f"{index}: {name}",
                f"parent={bone.parent_index}, child={bone.child_index}, "
                f"sibling={bone.sibling_index}, symmetry={bone.symmetry_index}, "
                f"segmentScale={bool(bone.segment_scale)}",
            )
            if any(bone.reserved):
                self._tree_item(
                    bone_root,
                    "reserve",
                    tuple(f"0x{value:X}" for value in bone.reserved),
                )
            for matrix_name, matrices in (
                ("local", mesh.local_matrices),
                ("world", mesh.world_matrices),
                ("inverse bind", mesh.inverse_bind_matrices),
            ):
                if index < len(matrices):
                    matrix = matrices[index]
                    self._tree_item(
                        bone_root,
                        f"{matrix_name} matrix",
                        "[" + ", ".join(f"{float(value):.6g}" for value in matrix) + "]",
                    )

        blend = getattr(mesh, "blend_shape_data", None)
        if blend is not None:
            is_mhrise_blend = bool(
                profile is not None
                and profile.blend_shape_layout is BlendShapeLayout.MHRISE
            )
            (inert, aabb_w_is_ignored, standard_w_is_padding, standard_w_is_ignored, deform_percent_is_ignored,
             normal_w_is_padding, normal_w_is_ignored) = (
                bool(getattr(profile, f"blend_shape_{name}", False)) for name in (
                    "reserved_storage_is_inert", "aabb_w_is_runtime_ignored", "standard_w_is_padding",
                    "standard_w_is_runtime_ignored", "deform_percent_is_runtime_ignored", "normal_w_is_padding",
                    "normal_w_is_runtime_ignored"))
            blend_root = self._tree_item(
                tree,
                self.tr("Blend shapes"),
                f"bodies={len(blend.bodies)}, "
                f"channels={sum(len(body.channels) for body in blend.bodies)}",
            )
            if any(blend.layout_reserve) and not inert:
                self._tree_item(
                    blend_root,
                    "unexpected layout reserve",
                    blend.layout_reserve.hex(" "),
                )
            if any(blend.layout_unknown_fields):
                self._tree_item(
                    blend_root,
                    "layout unknown fields",
                    blend.layout_unknown_fields,
                )
            for name, value in (
                ("legacy auxiliary offsets", blend.legacy_aux_offsets),
                ("legacy blendS", blend.legacy_blend_s),
                ("legacy blendSS", blend.legacy_blend_ss),
            ):
                if value and any(value):
                    self._tree_item(blend_root, name, value)
            for body in blend.bodies:
                body_root = self._tree_item(
                    blend_root,
                    f"LOD {body.lod_index}",
                    f"type={body.compression_type}, targets={len(body.targets)}, "
                    f"channels={len(body.channels)} offset={body.channel_offset}",
                )
                body_details = [
                    (
                        "raw unvisualized payload",
                        len(body.raw_unvisualized_payload),
                    ),
                ]
                if not is_mhrise_blend:
                    body_details[:0] = [
                        ("body offset", f"0x{body.body_offset:X}"),
                        ("target offset", f"0x{body.target_offset:X}"),
                        ("AABB offset", f"0x{body.aabb_offset:X}"),
                    ]
                for name, value in body_details:
                    if value not in ("", 0):
                        self._tree_item(body_root, name, value)
                if any(body.reserve) and not inert:
                    self._tree_item(
                        body_root,
                        "unexpected reserved bytes",
                        body.reserve.hex(" "),
                    )
                all_targets = [
                    *(('Target', target) for target in body.targets),
                    *(('None target', target) for target in body.none_targets),
                ]
                for target_index, (target_kind, target) in enumerate(all_targets):
                    target_summary = (
                        f"channels={target.channel_count}@"
                        f"{target.channel_location}, ranges={len(target.ranges)}"
                    )
                    if not is_mhrise_blend:
                        target_summary += f", sameTarget={target.same_target_id}"
                    target_root = self._tree_item(
                        body_root,
                        f"{target_kind} {target_index}",
                        target_summary,
                    )
                    self._tree_item(target_root, "part index", target.part_index)
                    if not is_mhrise_blend:
                        self._tree_item(
                            target_root,
                            "is blend shape",
                            target.is_blend_shape,
                        )
                    if target.raw_reserve is not None and any(target.raw_reserve) and not inert:
                        self._tree_item(
                            target_root,
                            "unexpected reserved/shared values",
                            target.raw_reserve,
                        )
                    if target.aabb is not None:
                        self._tree_item(
                            target_root,
                            "Position-delta bounds",
                            (
                                f"min={target.aabb.minimum}, "
                                f"max={target.aabb.maximum}"
                                if aabb_w_is_ignored
                                else f"min={target.aabb.minimum4}, "
                                f"max={target.aabb.maximum4}"
                            ),
                        )
                        if aabb_w_is_ignored:
                            minimum = np.asarray(
                                target.aabb.minimum,
                                dtype=np.float32,
                            )
                            step = (
                                np.asarray(
                                    target.aabb.maximum,
                                    dtype=np.float32,
                                )
                                - minimum
                            ) / np.asarray(
                                (2047.0, 1023.0, 2047.0),
                                dtype=np.float32,
                            )
                            self._tree_item(
                                target_root,
                                "Compute quantization constants",
                                f"offset={minimum.tolist()}, range={step.tolist()}",
                            )
                    for range_index, item in enumerate(target.ranges):
                        self._tree_item(
                            target_root,
                            f"Range {range_index}",
                            f"base={item.base_vertex_location}, payload={item.blend_shape_location}, "
                            f"vertices={item.vertex_count}, part={item.part_id}, "
                            f"surfaceRange={item.surface_range_id}",
                        )
                maps = self._tree_item(body_root, "LOD correspondence")
                if is_mhrise_blend:
                    for target_index, target in enumerate(
                        (*body.targets, *body.none_targets)
                    ):
                        if target_index >= len(body.target_lod_map):
                            continue
                        mapped = body.target_lod_map[target_index]
                        self._tree_item(
                            maps,
                            f"Target {target_index}",
                            (
                                "base/reference LOD"
                                if mapped < 0
                                else f"canonical target {mapped}"
                            ),
                        )
                    for channel in body.channels:
                        if channel.index >= len(body.channel_lod_map):
                            continue
                        mapped = body.channel_lod_map[channel.index]
                        self._tree_item(
                            maps,
                            channel.name or f"Channel {channel.index}",
                            (
                                "base/reference LOD"
                                if mapped < 0
                                else f"canonical channel {mapped}"
                            ),
                        )
                else:
                    self._tree_item(maps, "Target map", body.target_lod_map)
                    self._tree_item(maps, "Channel map", body.channel_lod_map)
                channels_root = self._tree_item(
                    body_root,
                    "Decoded channels",
                    len(body.channels),
                )
                for channel in body.channels:
                    channel_root = self._tree_item(
                        channels_root,
                        channel.name or f"Channel {channel.index}",
                        f"index={channel.index}, nameSlot={channel.name_slot}, "
                        f"segments={len(channel.segments)}",
                    )
                    for segment_index, segment in enumerate(channel.segments):
                        values = segment.position_deltas
                        segment_root = self._tree_item(
                            channel_root,
                            f"Segment {segment_index}",
                            f"buffer={segment.buffer_index}, base={segment.base_vertex_location}, "
                            f"vertices={len(values)}, target={segment.target_index}, "
                            f"range={segment.range_index}",
                        )
                        if len(values):
                            self._tree_item(
                                segment_root,
                                "delta bounds",
                                f"min={values.min(axis=0).tolist()}, "
                                f"max={values.max(axis=0).tolist()}",
                            )
                        if (
                            segment.vertex_cull_mask is not None
                            and np.any(segment.vertex_cull_mask)
                        ):
                            self._tree_item(
                                segment_root,
                                "NaN vertex-cull sentinels",
                                int(np.count_nonzero(segment.vertex_cull_mask)),
                            )
                        if (
                            segment.position_w is not None
                            and len(segment.position_w)
                            and not standard_w_is_padding
                        ):
                            self._tree_item(
                                segment_root,
                                (
                                    "BlendShapeVertexStandard::position.w "
                                    "(runtime ignored)"
                                    if standard_w_is_ignored
                                    else "BlendShapeVertexStandard::position.w "
                                    "(unresolved runtime role)"
                                ),
                                f"min={float(segment.position_w.min()):g}, "
                                f"max={float(segment.position_w.max()):g}",
                            )
                        if (
                            segment.deform_percent is not None
                            and len(segment.deform_percent)
                        ):
                            self._tree_item(
                                segment_root,
                                (
                                    "BlendShapeVertexQuality::deformPercent "
                                    "(runtime ignored)"
                                    if deform_percent_is_ignored
                                    else "BlendShapeVertexQuality::deformPercent "
                                    "(unresolved runtime role)"
                                ),
                                f"min={float(segment.deform_percent.min()):g}, "
                                f"max={float(segment.deform_percent.max()):g}",
                            )
                        if segment.target_normals is not None:
                            self._tree_item(
                                segment_root,
                                "explicit target normals",
                                len(segment.target_normals),
                            )
                        if (
                            segment.normal_w is not None
                            and len(segment.normal_w)
                            and not normal_w_is_padding
                        ):
                            self._tree_item(
                                segment_root,
                                (
                                    "BlendShapeVertexNormal*::normal.w "
                                    "(runtime ignored)"
                                    if normal_w_is_ignored
                                    else "BlendShapeVertexNormal*::normal.w "
                                    "(unresolved runtime role)"
                                ),
                                f"{len(segment.normal_w)} values, "
                                f"min={int(segment.normal_w.min())}, "
                                f"max={int(segment.normal_w.max())}",
                            )
            modifier = blend.bone_modifier
            if modifier is not None:
                baseline_outputs = ()
                resident_payload = (
                    mesh.mesh_buffer.buffer_payloads.get(0)
                    if mesh.mesh_buffer is not None
                    else None
                )
                if resident_payload is not None and len(resident_payload.positions):
                    try:
                        baseline_outputs = modifier.position_deltas(
                            np.asarray(
                                resident_payload.positions,
                                dtype=np.float32,
                            ).reshape(-1, 3)
                        )
                    except ValueError:
                        baseline_outputs = ()
                modifier_root = self._tree_item(
                    blend_root,
                    "Bone modifiers",
                    len(modifier.effectors),
                )
                self._tree_item(modifier_root, "offset", f"0x{modifier.offset:X}")
                if modifier.reserve:
                    self._tree_item(
                        modifier_root,
                        "unexpected reserved value",
                        modifier.reserve,
                    )
                for index, effector in enumerate(modifier.effectors):
                    effector_root = self._tree_item(
                        modifier_root,
                        f"Effector {index}",
                        f"joint={effector.joint_index}, vertices={len(effector.vertex_indices)}",
                    )
                    self._tree_item(
                        effector_root,
                        "vertex indices",
                        effector.vertex_indices,
                    )
                    if len(effector.vertex_positions):
                        self._tree_item(
                            effector_root,
                            "position bounds",
                            f"min={effector.vertex_positions.min(axis=0).tolist()}, "
                            f"max={effector.vertex_positions.max(axis=0).tolist()}",
                        )
                    if index < len(baseline_outputs):
                        baseline = baseline_outputs[index]
                        if len(baseline) and float(np.max(np.abs(baseline))) > 2e-6:
                            self._tree_item(
                                effector_root,
                                "bind-pose rebinder output",
                                f"current-reference min={baseline.min(axis=0).tolist()}, "
                                f"max={baseline.max(axis=0).tolist()}",
                            )

        normal = getattr(mesh, "normal_recalc_data", None)
        if normal is not None:
            is_semantic_normal = bool(
                profile is not None
                and profile.normal_recalc_layout
                in {
                    NormalRecalcLayout.DMC5,
                    NormalRecalcLayout.RE2,
                    NormalRecalcLayout.RE3,
                    NormalRecalcLayout.RE8,
                    NormalRecalcLayout.RE2_RT,
                    NormalRecalcLayout.RE3_RT,
                    NormalRecalcLayout.MHRISE,
                    NormalRecalcLayout.RE4,
                    NormalRecalcLayout.SF6,
                    NormalRecalcLayout.MHWILDS,
                    NormalRecalcLayout.RE9,
                    NormalRecalcLayout.ONIMUSHA_WOTS,
                }
            )
            normal_summary = f"edgeExcluded={normal.edge_excluded}"
            if is_semantic_normal and normal.redirects:
                normal_summary = (
                    f"vertices={len(normal.redirects)}, "
                    "semantic LOD0 normal rebuild"
                )
            normal_root = self._tree_item(
                tree,
                self.tr("Normal recalculation"),
                normal_summary,
            )
            if any(normal.reserved):
                self._tree_item(
                    normal_root,
                    "unexpected reserved values",
                    normal.reserved,
                )
            if (
                not is_semantic_normal
                and (
                    normal.lods
                    or normal.declared_lod_count
                    or normal.body_list_offset
                )
            ):
                self._tree_item(
                    normal_root,
                    "declared LOD count",
                    normal.declared_lod_count,
                )
                self._tree_item(
                    normal_root,
                    "body-list offset",
                    f"0x{normal.body_list_offset:X}",
                )
                self._tree_item(
                    normal_root,
                    "body offsets",
                    normal.body_offsets,
                )
            if normal.redirects:
                alias_count = sum(
                    redirect.target_vertex != index
                    for index, redirect in enumerate(normal.redirects)
                )
                annotation_count = sum(
                    redirect.annotation for redirect in normal.redirects
                )
                has_edge_policy = bool(
                    profile is not None
                    and profile.normal_recalc_layout
                    in {
                        NormalRecalcLayout.RE8,
                        NormalRecalcLayout.SF6,
                        NormalRecalcLayout.MHWILDS,
                        NormalRecalcLayout.RE9,
                        NormalRecalcLayout.ONIMUSHA_WOTS,
                    }
                )
                redirect_summary = (
                    f"vertices={len(normal.redirects)}, aliases={alias_count}"
                )
                if has_edge_policy:
                    redirect_summary += f", edge-marked vertices={annotation_count}"
                redirects = self._tree_item(
                    normal_root,
                    "Normal redirects",
                    redirect_summary,
                )
                if annotation_count and has_edge_policy:
                    self._tree_item(
                        redirects,
                        "Edge write policy",
                        "enabled by the owning Mesh component",
                    )
                self._tree_item(
                    redirects,
                    "First redirects",
                    (
                        [
                            (item.target_vertex, item.annotation)
                            for item in normal.redirects[:32]
                        ]
                        if has_edge_policy
                        else [
                            item.target_vertex for item in normal.redirects[:32]
                        ]
                    ),
                )
            for lod in normal.lods:
                if is_semantic_normal:
                    lod_geometry = mesh.meshes[0].lods[lod.lod_index]
                    submeshes = [
                        *group.submeshes
                        for group in lod_geometry.mesh_groups
                    ]
                    triangle_indices = sum(
                        submesh.indices_count for submesh in submeshes
                    )
                    dispatch_batches = sum(
                        -(-submesh.indices_count // INDICES_PER_WORKGROUP)
                        for submesh in submeshes
                    )
                    maximum_slot = max(
                        (word >> VERTEX_INDEX_BITS for word in lod.face_entries),
                        default=-1,
                    )
                    shared_slots = 0
                    sharing_batches = 0
                    maximum_targets_per_slot = 1
                    if lod.work_items:
                        work_cursor = 0
                        for submesh in submeshes:
                            submesh_end = work_cursor + submesh.indices_count
                            for start in range(
                                work_cursor,
                                submesh_end,
                                INDICES_PER_WORKGROUP,
                            ):
                                slot_targets = {}
                                for item in lod.work_items[
                                    start : min(start + INDICES_PER_WORKGROUP, submesh_end)
                                ]:
                                    slot_targets.setdefault(
                                        item.workgroup_slot,
                                        set(),
                                    ).add(item.target_vertex)
                                batch_shared = sum(
                                    len(targets) > 1
                                    for targets in slot_targets.values()
                                )
                                if batch_shared:
                                    sharing_batches += 1
                                    shared_slots += batch_shared
                                    maximum_targets_per_slot = max(
                                        maximum_targets_per_slot,
                                        max(map(len, slot_targets.values())),
                                    )
                            work_cursor = submesh_end
                    lod_root = self._tree_item(
                        normal_root,
                        f"LOD {lod.lod_index}",
                        f"triangles={triangle_indices // 3}, "
                        f"submeshes={len(submeshes)}, "
                        f"compute batches={dispatch_batches}",
                    )
                    self._tree_item(
                        lod_root,
                        "Canonical redirect table",
                        f"{len(lod.vertex_entries)} vertices",
                    )
                    self._tree_item(
                        lod_root,
                        "Redirected triangle topology",
                        f"{len(lod.work_items) or triangle_indices} indices",
                    )
                    self._tree_item(
                        lod_root,
                        "Local normal accumulators used",
                        maximum_slot + 1,
                    )
                    payload = mesh.mesh_buffer.buffer_payloads.get(
                        int(getattr(lod, "buffer_index", 0))
                    )
                    capacity_slack = getattr(
                        payload, "normal_recalc_runtime_ignored_capacity_slack", b""
                    )
                    if capacity_slack:
                        nonzero_words = sum(
                            int.from_bytes(
                                capacity_slack[offset : offset + 4],
                                "little",
                            )
                            != 0
                            for offset in range(0, len(capacity_slack), 4)
                        )
                        self._tree_item(
                            lod_root,
                            "Runtime-ignored normal capacity",
                            f"{len(capacity_slack)} bytes, "
                            f"{nonzero_words} nonzero; preserved on read and "
                            "omitted by canonical construction",
                        )
                    work_padding = getattr(
                        payload, "normal_recalc_runtime_ignored_work_padding_words", ()
                    )
                    if work_padding:
                        self._tree_item(
                            lod_root,
                            "Runtime-ignored draw alignment words",
                            f"{len(work_padding)} words, "
                            f"{sum(int(word) != 0 for word in work_padding)} "
                            "nonzero; dispatch index counts skip them and "
                            "canonical construction writes zero",
                        )
                    if shared_slots:
                        self._tree_item(
                            lod_root,
                            "Shared local accumulators",
                            f"{shared_slots} slots across {sharing_batches} "
                            f"compute batches; up to "
                            f"{maximum_targets_per_slot} target writes per slot",
                        )
                    continue
                lod_root = self._tree_item(
                    normal_root,
                    f"LOD {lod.lod_index}",
                    f"vertex words={len(lod.vertex_entries)}, face words={len(lod.face_entries)}",
                )
                for name, value in (
                    ("body offset", lod.body_offset),
                    ("vertex table offset", lod.vertices_offset),
                    ("face table offset", lod.faces_offset),
                ):
                    self._tree_item(lod_root, name, f"0x{value:X}")
                self._tree_item(lod_root, "First vertex words", lod.vertex_entries[:16])
                self._tree_item(lod_root, "First face words", lod.face_entries[:16])

        if mesh.bounding_boxes is not None:
            bounds_root = self._tree_item(
                tree,
                self.tr("Bounding boxes"),
                len(mesh.bounding_boxes.boxes),
            )
            if mesh.bounding_boxes.reserve:
                self._tree_item(
                    bounds_root,
                    "unexpected reserved value",
                    mesh.bounding_boxes.reserve,
                )
            self._tree_item(
                bounds_root,
                "boxes offset",
                f"0x{mesh.bounding_boxes.boxes_offset:X}",
            )
            for index, box in enumerate(mesh.bounding_boxes.boxes):
                self._tree_item(
                    bounds_root,
                    str(index),
                    f"min={box.minimum4}, max={box.maximum4}",
                )
        if mesh.group_pivots is not None:
            pivots_root = self._tree_item(
                tree,
                self.tr("Group pivots"),
                len(mesh.group_pivots.pivots),
            )
            self._tree_item(pivots_root, "buffer bytes", mesh.group_pivots.buffer_size)
            if mesh.group_pivots.reserve:
                self._tree_item(
                    pivots_root,
                    "unexpected reserved value",
                    mesh.group_pivots.reserve,
                )
            self._tree_item(
                pivots_root,
                "buffer offset",
                f"0x{mesh.group_pivots.buffer_offset:X}",
            )
            for index, pivot in enumerate(mesh.group_pivots.pivots):
                self._tree_item(pivots_root, str(index), pivot)

        names_root = self._tree_item(
            tree,
            self.tr("Name tables"),
            f"strings={len(mesh.names)}, materials={len(mesh.material_indices)}, "
            f"bones={len(mesh.bone_indices)}, morphs={len(mesh.blend_shape_indices)}",
        )
        materials_root = self._tree_item(names_root, "Materials")
        for index, name_index in enumerate(mesh.material_indices):
            resolved = mesh.names[name_index] if 0 <= name_index < len(mesh.names) else ""
            self._tree_item(materials_root, str(index), f"{name_index}: {resolved}")
        morph_names_root = self._tree_item(names_root, "Blend-shape name indices")
        for index, name_index in enumerate(mesh.blend_shape_indices):
            resolved = mesh.names[name_index] if 0 <= name_index < len(mesh.names) else ""
            self._tree_item(morph_names_root, str(index), f"{name_index}: {resolved}")
        strings_root = self._tree_item(names_root, "Strings")
        for index, value in enumerate(mesh.names):
            offset = mesh.name_offsets[index] if index < len(mesh.name_offsets) else 0
            self._tree_item(strings_root, str(index), f"0x{offset:X}: {value}")

        sections = self._tree_item(tree, self.tr("File sections"))
        for section in mesh.sections.values():
            self._tree_item(
                sections,
                section.name,
                f"0x{section.offset:X} + 0x{section.size:X}",
            )
        if mesh.sdf_path:
            self._tree_item(tree, "SDFTEX resource", mesh.sdf_path)
        if mesh.semantic_notes:
            notes = self._tree_item(
                tree,
                self.tr("Semantic notes"),
                len(mesh.semantic_notes),
            )
            for note in mesh.semantic_notes:
                self._tree_item(notes, f"{note.section}.{note.field}", note.message)

        tree.topLevelItem(0).setExpanded(True)

    def _populate_material_table(self):
        table = self.material_table
        bindings = self._material_session.bindings
        with QSignalBlocker(table):
            table.setRowCount(len(bindings))
            for row, binding in enumerate(bindings):
                self._material_session.resolve_texture(binding)
                values = [
                    binding.mesh_material_name,
                    binding.mdf_material_name,
                    binding.texture_type,
                    binding.resolved_texture_path or binding.texture_path,
                    binding.status,
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    table.setItem(row, col, item)
            if table.rowCount():
                table.selectRow(0)
        self._material_table_populated = True
        if bindings:
            self._update_texture_preview()
        else:
            self.texture_preview.setText(self.tr("No material mappings found."))
            self.texture_preview.setToolTip("")

    def _update_texture_preview(self):
        self.texture_preview.setToolTip("")
        row = self.material_table.currentRow()
        bindings = self._material_session.bindings
        if row < 0 or row >= len(bindings):
            self.texture_preview.setPixmap(QPixmap())
            self.texture_preview.setText(
                self.tr("Select a material to preview its texture.")
            )
            return

        binding = bindings[row]
        if not binding.resolved_texture_path and not binding.texture_path:
            self.texture_preview.setPixmap(QPixmap())
            self.texture_preview.setText(binding.status)
            return

        image = self._material_session.preview_image(binding)
        if not image or image.isNull():
            self.texture_preview.setPixmap(QPixmap())
            self.texture_preview.setText(f"{binding.status}\n{binding.resolved_texture_path}")
            return

        scaled = QPixmap.fromImage(image).scaled(
            320,
            320,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.texture_preview.setText("")
        self.texture_preview.setPixmap(scaled)
        self.texture_preview.setToolTip(binding.resolved_texture_path)


class _MeshViewport(ScenePreviewWidget):
    geometry_changed = Signal(object)
    camera_changed = Signal(str, bool)
    shown_shading_changed = Signal(object)

    def __init__(self, mesh, settings: dict | None = None, *, use_vertex_colors: bool = False):
        self.mesh = mesh
        self.inspection = None
        self._closed = False
        self._lit_overlays = (None, None)
        self._lit_shape: LitShape | None = None
        self._lit_indices: tuple | None = None
        self._device_shadings: set[str] = set()
        self._geometry_layer = "main"
        self._geometry_lod = 0
        self._blend_shape_channel = None
        self._blend_shape_weight = 0.0
        self._visualize_attribute = None
        self._use_vertex_colors = bool(use_vertex_colors)
        self._show_parsed_bounds = False
        self._show_group_pivots = False
        super().__init__(
            controls="mesh",
            settings=settings,
            initial_rotation=(0.0, 0.0),
            initial_distance=3.0,
            background=(0.1, 0.1, 0.1, 1.0),
        )
        self._surface_lighting_mode = self.lighting_mode
        self._preview_scene = None
        self.set_scene([])
        self._bone_labels, self._bone_points = self._build_bone_label_points()
        self.set_viewport_overlay_folded(self.overlay, True)

    def set_geometry(
        self,
        layer: str,
        lod_index: int,
        *,
        blend_shape_channel=None,
        blend_shape_weight: float = 0.0,
        visualize_attribute: str | None = None,
    ):
        if self._closed:
            return
        selection = str(layer), int(lod_index), visualize_attribute
        changed = self._preview_scene is None or selection != (
            self._geometry_layer, self._geometry_lod, self._visualize_attribute)
        preview = (MeshPreviewScene(self.mesh, key="mesh", layer=selection[0], lod_index=selection[1],
                                   visualize_attribute=visualize_attribute) if changed else self._preview_scene)
        if not preview.scene:
            raise ValueError("No drawable geometry is available for this LOD")
        inspection = self.inspection
        if changed and (inspection is None or inspection.geometry != selection[:2]):
            inspection = MeshInspection(self.mesh, preview)
        updated = preview.update(blend_shape_channel, blend_shape_weight)
        if changed:
            if self._visualize_attribute is None:
                self._surface_lighting_mode = self.lighting_mode
            self.lighting_mode = "off" if visualize_attribute else self._surface_lighting_mode
            with QSignalBlocker(self.light_combo):
                self.light_combo.setCurrentIndex(self.light_combo.findData(self.lighting_mode))
            self.light_combo.setEnabled(visualize_attribute is None)
            self.light_combo.setToolTip(self.tr("Attribute colors are shown without lighting.") if visualize_attribute else "")
            self._prepare_inspection(preview, inspection)
            camera = self.center.copy(), self.scale
            self.set_scene(preview.scene, reset_camera=False)
            if self._preview_scene is not None:
                self.center, self.scale = camera
            else:
                self.fit_geometry()
        elif updated:
            preview.upload(self)
        self._preview_scene = preview
        self._geometry_layer, self._geometry_lod, self._visualize_attribute = selection
        self._blend_shape_channel, self._blend_shape_weight = blend_shape_channel, float(blend_shape_weight)
        if changed or updated:
            self._lit_shape = None
            self.geometry_changed.emit(preview)
        self.update()

    def refresh_attribute_colors(self):
        """Must run before the attributes preview reads the colours."""
        preview = self._preview_scene
        if preview is not None and preview.attribute is not None and not preview.attribute.is_uv:
            preview.refresh_attribute_colors()

    def set_device_shading(self, shading: str, active: bool) -> None:
        shown = _shown_shading(self._device_shadings)
        if active:
            self._device_shadings.add(shading)
        else:
            self._device_shadings.discard(shading)
        if _shown_shading(self._device_shadings) != shown:
            # Hidden previews release the window before the shown one draws.
            self.shown_shading_changed.emit(_shown_shading(self._device_shadings))
            self.set_device_frames(_shown_shading(self._device_shadings) is not None)

    def show_device_frame(self, shading: str, frame) -> None:
        if shading == _shown_shading(self._device_shadings):
            self.set_device_frame(frame)

    def _device_view(self):
        shading = _shown_shading(self._device_shadings)
        view = (self._attribute_view() if shading == "attributes" else self._lit_view() if shading == "lit"
                else self._textured_view())
        return view, self._lit_overlays_shown() if isinstance(view, LitView) else None

    def _textured_view(self):
        if self.inspection is None or self.lighting_mode not in ("off", "fixed", "software"):
            return ""
        return TexturedView(*_view_fields(self), view_light(self.lighting_mode, self.ambient, self.diffuse),
                            bool(self._use_vertex_colors), orthographic=bool(self.orthographic))

    def _attribute_view(self):
        if self.inspection is None:
            return ""
        attribute = self._preview_scene.attribute
        pattern = (UvPattern(float(attribute.uv_scale), attribute.mode == "coordinates")
                   if attribute is not None and attribute.is_uv else None)
        return AttributeView(*_view_fields(self), pattern=pattern, attribute=self._visualize_attribute,
                             orthographic=bool(self.orthographic))

    def _device_frame_shows(self, view, frame) -> bool:
        """The camera and the parts shown are not compared: a frame of the previous ones still matches."""
        drawn = frame.view
        return (isinstance(view, LitView) and type(drawn) is type(view)
                and (drawn.geometry, drawn.size) == (view.geometry, view.size)
                and (not isinstance(view, AttributeView) or drawn.attribute == view.attribute))

    def lit_refusal(self) -> str:
        if self._geometry_layer != "main":
            return self.tr("Game lighting draws the main geometry only.")
        return ""

    def _lit_view(self):
        if refusal := self.lit_refusal():
            return refusal
        if self.inspection is None:
            return self.tr("Game lighting draws the main geometry only.")
        morph = ((int(self._blend_shape_channel), float(self._blend_shape_weight))
                 if self._blend_shape_channel is not None and self._blend_shape_weight else None)
        return LitView(*_view_fields(self), morph=morph, antialiased=self.antialiasing,
                       orthographic=bool(self.orthographic))

    def _lit_overlays_shown(self) -> LitOverlays:
        wireframe = "off" if self._visualize_attribute is not None else self.wireframe_mode
        made = (self._show_parsed_bounds, self._show_group_pivots, bool(self.show_bone_labels),
                float(self._device_window.devicePixelRatioF()), unorm(self._background)[:3], wireframe,
                float(self.line_width), self._shape_shown() if wireframe != "off" else None)
        if self._lit_overlays[0] != made:
            self._lit_overlays = (made, self._make_lit_overlays(*made))
        return self._lit_overlays[1]

    def _shape_shown(self) -> LitShape | None:
        preview = self._preview_scene
        if preview is None:
            return None
        if self._lit_shape is None:
            draw = preview.scene[0]
            indices = (self._lit_indices if self._lit_indices is not None and self._lit_indices[0] is preview
                       else (preview, {index: np.array(batch.indices, np.uint32)
                                       for index, batch in enumerate(draw.batches)}))
            self._lit_indices = indices
            self._lit_shape = LitShape(np.array(draw.vertices, np.float32).reshape(-1, 3), indices[1])
        return self._lit_shape

    def _make_lit_overlays(self, bounds: bool, pivots: bool, labels: bool, ratio: float,
                           background: tuple[int, int, int], wireframe: str, line_width: float,
                           shape: LitShape | None) -> LitOverlays:
        boxes = [(minimum, maximum, _BOUNDS_COLOUR) for minimum, maximum in self._parsed_bounds()] if bounds else []
        marks = list(bounds_marks(boxes))
        pivot_data = getattr(self.mesh, "group_pivots", None)
        points = getattr(pivot_data, "pivots", ()) if pivots and pivot_data is not None else ()
        if len(points):
            marks.append(LitPoints(np.asarray(points, np.float64).reshape(-1, 3), 5.0, unorm(_PIVOT_COLOUR)))
        segments = self._skeleton_segments() if labels else []
        if segments:
            marks.append(LitLines(np.asarray(segments, np.float64), 1.5, unorm(_SKELETON_COLOUR)))
        shown_labels = None
        if labels and self._bone_labels:
            font = QFont(self.font())
            if font.pointSizeF() > 0:
                font.setPointSizeF(font.pointSizeF() * ratio)
            else:
                font.setPixelSize(max(1, round(font.pixelSize() * ratio)))
            atlas, rectangles = self._build_bone_label_atlas(font)
            rows = np.frombuffer(bytes(atlas.constBits()), np.uint8).reshape(atlas.height(), atlas.bytesPerLine())
            texels = np.ascontiguousarray(rows[:, :4 * atlas.width()]).view("<u4")
            shown_labels = LitLabels(texels, np.asarray(self._bone_points[:len(rectangles)], np.float64),
                                     np.asarray(rectangles, np.int64).reshape(-1, 4), round(_LABEL_MARGIN * ratio))
        scene = None if wireframe == "off" else SceneShown(
            ALL_EDGES if wireframe == "xray" else VISIBLE_EDGES, line_width, filled=wireframe != "wire")
        return LitOverlays(background, tuple(marks), shown_labels, scene, shape)

    def cleanup(self):
        self._closed = True
        self._preview_scene = None
        self.inspection = None
        super().cleanup()

    def _prepare_inspection(self, preview, inspection):
        if self.inspection is not inspection:
            self.inspection = inspection
            self.set_hidden_parts({}, refresh=False)
        for index, batch in enumerate(preview.scene[0].batches):
            batch.part_index = index

    def set_visible_parts(self, parts):
        if self.inspection is not None:
            self.set_hidden_parts({"mesh": self.inspection.all_parts.difference(parts)})

    def fit_geometry(self, parts=None):
        if self.inspection is None or not self._meshes:
            return
        if parts is None:
            parts = _visible_parts(self)
        indices = self.inspection.vertex_indices(parts)
        self.fit_points(self._meshes[0].vertices[indices])

    def fit_points(self, points):
        points = points[np.isfinite(points).all(axis=1)]
        if not len(points):
            return
        minimum, maximum = points.min(axis=0), points.max(axis=0)
        self.center = (minimum + maximum) * 0.5
        self.scale = 1.0 / max(float(np.max(maximum - minimum)), 1e-6)
        radius = max(float(np.linalg.norm(maximum - minimum)) * self.scale * 0.5, 0.01)
        tangent = np.tan(np.deg2rad(22.5)) * min(1.0, max(1, self.width()) / max(1, self.height()))
        divisor = tangent if self.orthographic else np.sin(np.arctan(tangent))
        self.distance = max(0.11, radius * 1.1 / divisor)
        self.update()

    def set_camera_view(self, name):
        if name not in CAMERA_VIEWS:
            return
        self.rot_x, self.rot_y = CAMERA_VIEWS[name]
        self.orthographic = name != "orbit"
        self._update_after_camera_change()

    def set_orthographic(self, enabled):
        self.orthographic = bool(enabled)
        self._update_after_camera_change()

    def _update_after_camera_change(self):
        super()._update_after_camera_change()
        view = next((name for name, angles in CAMERA_VIEWS.items()
                     if angles == (self.rot_x, self.rot_y)), "custom")
        self.camera_changed.emit(view, self.orthographic)

    def keyPressEvent(self, event):
        modifiers = event.modifiers() & ~Qt.KeypadModifier
        if modifiers in (Qt.NoModifier, Qt.ControlModifier):
            views = {Qt.Key_1: ("front", "back"), Qt.Key_3: ("right", "left"),
                     Qt.Key_7: ("top", "bottom")}
            if event.key() in views:
                self.set_camera_view(views[event.key()][bool(modifiers & Qt.ControlModifier)])
            elif event.key() == Qt.Key_5 and not modifiers:
                self.set_orthographic(not self.orthographic)
            else:
                return super().keyPressEvent(event)
            event.accept()
            return
        super().keyPressEvent(event)

    def set_semantic_overlays(self, *, bounds: bool, pivots: bool):
        self._show_parsed_bounds = bool(bounds)
        self._show_group_pivots = bool(pivots)
        self.update()

    def set_vertex_colors_enabled(self, enabled: bool):
        self._use_vertex_colors = bool(enabled)
        self.update()

    def _build_bone_label_points(self) -> tuple[list[str], np.ndarray]:
        joint_count = max(int(getattr(self.mesh, "joint_count", 0) or 0), 0)
        matrices = list(getattr(self.mesh, "world_matrices", None) or getattr(self.mesh, "local_matrices", None) or [])[:joint_count]
        if not matrices:
            return [], np.zeros((0, 3), dtype=np.float32)
        return [joint_name(self.mesh, i) for i in range(len(matrices))], np.array([m[12:15] for m in matrices], np.float32)

    def _build_bone_label_atlas(self, font: QFont) -> tuple[QImage, list[tuple[int, int, int, int]]]:
        metrics = QFontMetrics(font)
        max_width = 2048
        labels = [
            metrics.elidedText(label, Qt.ElideRight, max_width - 2)
            for label in self._bone_labels
        ]
        label_sizes = [
            (max(1, metrics.horizontalAdvance(label) + 2), max(1, metrics.height() + 2))
            for label in labels
        ]
        atlas_width = min(max_width, max(width for width, _ in label_sizes))
        atlas_width = max(atlas_width, min(max_width, 512))
        x = y = row_height = 0
        rects: list[tuple[int, int, int, int]] = []
        for width, height in label_sizes:
            if x and x + width > atlas_width:
                x = 0
                y += row_height
                row_height = 0
            rects.append((x, y, width, height))
            x += width
            row_height = max(row_height, height)

        atlas = QImage(atlas_width, max(1, y + row_height), QImage.Format.Format_RGBA8888)
        atlas.fill(Qt.transparent)
        painter = QPainter(atlas)
        painter.setFont(font)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setPen(Qt.yellow)
        for label, (x, y, _width, _height) in zip(labels, rects):
            painter.drawText(x + 1, y + metrics.ascent() + 1, label)
        painter.end()
        return atlas, rects

    def _parsed_bounds(self) -> list[tuple[np.ndarray, np.ndarray]]:
        result: list[tuple[np.ndarray, np.ndarray]] = []
        for mesh_data in getattr(self.mesh, "meshes", ()):
            minimum, maximum = mesh_data.bounding_box
            result.append(
                (
                    np.asarray(minimum[:3], dtype=np.float32),
                    np.asarray(maximum[:3], dtype=np.float32),
                )
            )
        layout = getattr(self.mesh, "bounding_boxes", None)
        for box in getattr(layout, "boxes", ()):
            result.append(
                (
                    np.asarray(box.minimum, dtype=np.float32),
                    np.asarray(box.maximum, dtype=np.float32),
                )
            )
        mesh_buffer = getattr(self.mesh, "mesh_buffer", None)
        for cluster in getattr(mesh_buffer, "mply_cluster_headers", ()):
            bounds = getattr(cluster, "culling_aabb", None)
            if bounds is not None:
                result.append(
                    (
                        np.asarray(bounds.minimum, dtype=np.float32),
                        np.asarray(bounds.maximum, dtype=np.float32),
                    )
                )
        return result

    def _skeleton_segments(self) -> list[tuple[np.ndarray, np.ndarray]]:
        segments = []
        if len(self._bone_points):
            for index, bone in enumerate(getattr(self.mesh, "bones", ())):
                parent = int(getattr(bone, "parent_index", -1))
                if (
                    0 <= index < len(self._bone_points)
                    and 0 <= parent < len(self._bone_points)
                    and parent != index
                ):
                    segments.append((self._bone_points[parent], self._bone_points[index]))
        return segments

