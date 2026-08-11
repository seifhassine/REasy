"""End-user semantic GUI editor."""

from __future__ import annotations

import copy
import json
import math
import struct
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from file_handlers.clip.enums import PropertyType
from file_handlers.motion.mot_clip.model import (
    ASCII_VALUE_PROPERTY_TYPES,
    CONTAINER_PROPERTY_TYPES,
    UTF16_VALUE_PROPERTY_TYPES,
    ClipInterpolation,
    ClipKey,
    ClipProperty,
)
from ui.styles import get_color_scheme

from .adapter import GuiPreviewControl, GuiPreviewScenario
from .dependencies import GuiDependencyCatalog
from .gui_file import GuiFile
from .model import (
    COMPONENT_NAMES,
    GuiAnimation,
    GuiBinding,
    GuiDocument,
    GuiNineSlice,
    GuiProperty,
    GuiSymbol,
    GuiTextureSet,
)
from .property_types import (
    GUI_FLOAT_TYPES,
    GUI_INTEGER_TYPES,
    GUI_INTEGER_VECTOR_TYPES,
    GUI_STRING_TYPES,
    SIGNED_VECTOR_COUNTS,
    UNSIGNED_VECTOR_COUNTS,
)
from .scene import GuiSceneNode, GuiWorkspace


ROLE_VALUE = int(Qt.ItemDataRole.UserRole)
CLIP_STRING_TYPES = ASCII_VALUE_PROPERTY_TYPES | UTF16_VALUE_PROPERTY_TYPES
ASCII_EVENT_MODES = frozenset(
    {ClipInterpolation.EVENT, ClipInterpolation.PASS_EVENT}
)
_EDITOR_MODES = ("layout", "preview", "interact")


def _category(name: str, kind: PropertyType) -> str:
    if name in {
        "Position", "Rotation", "Scale", "Size", "RegionSize", "ControlPoint",
        "ResolutionAdjust", "ScreenSize", "ScissorRect", "TilingSize",
    }:
        return "Layout"
    if name in {
        "Visible", "Color", "ColorScale", "ColorOffset", "Saturation",
        "BlendType", "GlowColor", "ShadowColor", "Priority", "MaskType",
    }:
        return "Appearance"
    if name.startswith(("Message", "Font", "Letter", "Line", "Ruby", "Text")):
        return "Text"
    if kind == PropertyType.ASSET or any(
        word in name
        for word in ("Asset", "Material", "Mesh", "Texture", "UVSequence")
    ):
        return "Resources"
    if name in {"Name", "Interactive", "HitVisible", "Play", "PlayState", "PlayFrame", "Loop"}:
        return "Behavior"
    return "Other"


def _display(value: Any) -> str:
    if isinstance(value, bool):
        return "On" if value else "Off"
    if isinstance(value, (list, tuple)):
        return ", ".join(f"{item:g}" if isinstance(item, float) else str(item) for item in value)
    return "" if value is None else str(value)


def _combined_mask(properties: list[GuiProperty]) -> int:
    if any(item.component_mask == -1 for item in properties):
        return -1
    result = 0
    for item in properties:
        result |= item.component_mask
    return result


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return list(left) == list(right)
    return left == right


def _toolbar_heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("guiToolbarHeading")
    return label


def _runtime_field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "fieldLabel")
    return label


def _mode_button(text: str, object_name: str, tooltip: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(object_name)
    button.setProperty("role", "mode")
    button.setCheckable(True)
    button.setToolTip(tooltip)
    return button


def _editor_stylesheet() -> str:
    colors = get_color_scheme()
    return f"""
        QWidget#guiEditor QLabel {{
            background: transparent;
        }}
        QWidget#guiEditor QLineEdit,
        QWidget#guiEditor QAbstractSpinBox,
        QWidget#guiEditor QComboBox {{
            color: #edf1f5;
            background: #3a3f45;
            border: 1px solid #59616a;
            border-radius: 3px;
            padding: 2px 4px;
        }}
        QWidget#guiEditor QLineEdit:focus,
        QWidget#guiEditor QAbstractSpinBox:focus,
        QWidget#guiEditor QComboBox:focus {{
            background: #3d434a;
            border-color: #3da1ad;
        }}
        QWidget#guiEditor QLineEdit:disabled,
        QWidget#guiEditor QAbstractSpinBox:disabled,
        QWidget#guiEditor QComboBox:disabled {{
            color: #7f8993;
            background: #30343a;
            border-color: #484f56;
        }}
        QWidget#guiEditor QAbstractSpinBox[role="vectorComponent"] {{
            padding-left: 3px;
            padding-right: 2px;
        }}
        QWidget#guiEditor QFrame#guiDocumentBar,
        QWidget#guiEditor QFrame#guiPreviewBar {{
            background: #34383d;
            border: 1px solid {colors['border']};
            border-radius: 5px;
        }}
        QWidget#guiEditor QFrame#guiRuntimeSettings {{
            background: #30343a;
            border: 1px solid {colors['border']};
            border-radius: 5px;
        }}
        QWidget#guiEditor QLabel#guiToolbarHeading {{
            color: #9da8b3;
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 1px;
        }}
        QWidget#guiEditor QLabel#guiModeDescription {{
            color: #aeb7c2;
            padding-left: 4px;
        }}
        QWidget#guiEditor QLabel#guiDocumentStatus {{
            color: #b9c2cb;
            padding: 0 4px;
        }}
        QWidget#guiEditor QLabel#guiModeStatus {{
            color: white;
            font-size: 10px;
            font-weight: 700;
            border: 1px solid #68737d;
            border-radius: 9px;
            padding: 3px 9px;
        }}
        QWidget#guiEditor QLabel#guiModeStatus[mode="layout"] {{
            background: #4a535c;
        }}
        QWidget#guiEditor QLabel#guiModeStatus[mode="preview"] {{
            background: #176d79;
            border-color: #3da1ad;
        }}
        QWidget#guiEditor QLabel#guiModeStatus[mode="interact"] {{
            background: #855a1d;
            border-color: #c8923c;
        }}
        QWidget#guiEditor QLabel#guiModeStatus[activity="playing"] {{
            background: #287044;
            border-color: #4ba66c;
        }}
        QWidget#guiEditor QPushButton[role="mode"] {{
            min-width: 62px;
            min-height: 20px;
            padding: 4px 10px;
            background: {colors['input_bg']};
            border: 1px solid {colors['border']};
            border-radius: 3px;
        }}
        QWidget#guiEditor QPushButton[role="mode"]:hover {{
            background: #4a5057;
            border-color: #71808d;
        }}
        QWidget#guiEditor QPushButton#guiLayoutModeButton:checked {{
            background: #526170;
            border-color: #8295a6;
        }}
        QWidget#guiEditor QPushButton#guiPreviewModeButton:checked {{
            background: #176d79;
            border-color: #45a9b5;
        }}
        QWidget#guiEditor QPushButton#guiInteractionModeButton:checked {{
            background: #855a1d;
            border-color: #d09a43;
        }}
        QWidget#guiEditor QPushButton[role="document"],
        QWidget#guiEditor QPushButton[role="utility"],
        QWidget#guiEditor QToolButton[role="transport"],
        QWidget#guiEditor QToolButton[role="settings"] {{
            min-width: 0;
            min-height: 20px;
            padding: 4px 8px;
            background: {colors['input_bg']};
            border: 1px solid {colors['border']};
            border-radius: 3px;
        }}
        QWidget#guiEditor QPushButton[role="document"]:hover,
        QWidget#guiEditor QPushButton[role="utility"]:hover,
        QWidget#guiEditor QToolButton[role="transport"]:hover,
        QWidget#guiEditor QToolButton[role="settings"]:hover {{
            background: #4a5057;
            border-color: #71808d;
        }}
        QWidget#guiEditor QPushButton[role="utility"]:checked,
        QWidget#guiEditor QToolButton[role="settings"]:checked {{
            background: #176d79;
            border-color: #45a9b5;
        }}
        QWidget#guiEditor QToolButton[role="transport"]:checked {{
            background: #287044;
            border-color: #4ba66c;
        }}
        QWidget#guiEditor QPushButton[role="document"]:disabled,
        QWidget#guiEditor QToolButton[role="transport"]:disabled {{
            color: #737d87;
            background: #30343a;
            border-color: #454b51;
        }}
        QWidget#guiEditor QFrame#guiRuntimeSettings QLabel[role="fieldLabel"] {{
            color: #aeb7c2;
            font-size: 10px;
        }}
        QWidget#guiEditor QLabel#guiFrameTotal {{
            color: #9da8b3;
            min-width: 36px;
        }}
        QWidget#guiEditor QLabel#guiScenarioCoverage {{
            color: #c8d0d8;
            background: #41474e;
            border: 1px solid #5b646d;
            border-radius: 8px;
            padding: 2px 7px;
        }}
        QWidget#guiEditor QLabel#guiScenarioCoverage[status="partial"] {{
            color: #f2d39b;
            background: #594723;
            border-color: #876b32;
        }}
        QWidget#guiEditor QLabel#guiScenarioCoverage[status="complete"] {{
            color: #c9ead3;
            background: #294e39;
            border-color: #42785a;
        }}
        QWidget#guiEditor QLabel#guiScenarioCoverage[status="custom"] {{
            color: #c9e8ed;
            background: #234c55;
            border-color: #3f7f8a;
        }}
    """


class GuiEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.handler = handler
        self.gui_file: GuiFile = handler.gui_file
        self.document: GuiDocument = self.gui_file.require_document()
        assert self.gui_file.profile is not None
        self.profile = self.gui_file.profile
        self.adapter = self.profile.adapter
        self.runtime_editor = self.adapter.editor
        self.dependencies = GuiDependencyCatalog(
            self.handler.resolve_resource,
            self.profile,
        )
        self.workspace: GuiWorkspace | None = None
        self.scene = None
        self.selected_node: GuiSceneNode | None = None
        self.selected_track: ClipProperty | None = None
        self.selected_animation: GuiAnimation | None = None
        self._preview_scenarios: tuple[GuiPreviewScenario, ...] = ()
        self._custom_preview_scenario: GuiPreviewScenario | None = None
        self._customizing_preview = False
        self._preview_diagnostic = ""
        self._updating = False
        self._build_ui()
        self._build_workspace()
        self._refresh_document()

    def _build_ui(self) -> None:
        self.setObjectName("guiEditor")
        self.setStyleSheet(_editor_stylesheet())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 5, 5, 5)
        outer.setSpacing(5)

        document_bar = QFrame(self)
        document_bar.setObjectName("guiDocumentBar")
        tools = QHBoxLayout(document_bar)
        tools.setContentsMargins(7, 5, 7, 5)
        tools.setSpacing(5)
        self.undo_button = QPushButton("Undo")
        self.undo_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack)
        )
        self.redo_button = QPushButton("Redo")
        self.redo_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward)
        )
        self.fit_button = QPushButton("Fit canvas")
        self.fit_button.setToolTip("Fit the authored GUI canvas in the viewport")
        self.reset_button = QPushButton("Revert file")
        self.reset_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogResetButton)
        )
        self.reset_button.setToolTip("Discard all unsaved GUI edits")
        for button in (
            self.undo_button,
            self.redo_button,
            self.fit_button,
            self.reset_button,
        ):
            button.setProperty("role", "document")
            tools.addWidget(button)
        tools.addStretch(1)
        self.status = QLabel()
        self.status.setObjectName("guiDocumentStatus")
        tools.addWidget(self.status)
        outer.addWidget(document_bar)

        preview_bar = QFrame(self)
        preview_bar.setObjectName("guiPreviewBar")
        preview_tools = QHBoxLayout(preview_bar)
        preview_tools.setContentsMargins(7, 5, 7, 5)
        preview_tools.setSpacing(5)
        preview_tools.addWidget(_toolbar_heading("MODE"))
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.layout_mode_button = _mode_button(
            "Layout",
            "guiLayoutModeButton",
            "Layout mode: inspect, select, and move schematic GUI elements",
        )
        self.preview_button = _mode_button(
            "Preview",
            "guiPreviewModeButton",
            "Preview mode: render game assets while retaining selection and dragging",
        )
        self.interaction_button = _mode_button(
            "Interact",
            "guiInteractionModeButton",
            self.runtime_editor.interaction_tooltip,
        )
        for index, button in enumerate(
            (
                self.layout_mode_button,
                self.preview_button,
                self.interaction_button,
            )
        ):
            self.mode_group.addButton(button, index)
            preview_tools.addWidget(button)

        self.mode_status = QLabel()
        self.mode_status.setObjectName("guiModeStatus")
        self.mode_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_tools.addWidget(self.mode_status)
        self.mode_description = QLabel()
        self.mode_description.setObjectName("guiModeDescription")
        self.mode_description.setMaximumWidth(320)
        preview_tools.addWidget(self.mode_description)
        preview_tools.addStretch(1)

        preview_tools.addWidget(_toolbar_heading("PLAYBACK"))
        self.restart_button = QToolButton()
        self.restart_button.setProperty("role", "transport")
        self.restart_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.restart_button.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_MediaSkipBackward
            )
        )
        self.restart_button.setText("Restart")
        self.restart_button.setToolTip("Restart the selected animation")
        self.play_button = QToolButton()
        self.play_button.setProperty("role", "transport")
        self.play_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.play_button.setText("Play")
        self.play_button.setCheckable(True)
        self.play_button.setToolTip("Play the selected animation")
        preview_tools.addWidget(self.restart_button)
        preview_tools.addWidget(self.play_button)
        preview_tools.addWidget(_toolbar_heading("Frame"))
        self.preview_frame = QDoubleSpinBox()
        self.preview_frame.setDecimals(2)
        self.preview_frame.setRange(0.0, 0.0)
        self.preview_frame.setSingleStep(1.0)
        self.preview_frame.setMaximumWidth(74)
        self.preview_frame.setToolTip("Current animation frame")
        preview_tools.addWidget(self.preview_frame)
        self.preview_frame_total = QLabel("/ 0")
        self.preview_frame_total.setObjectName("guiFrameTotal")
        preview_tools.addWidget(self.preview_frame_total)

        self.guides_button = QPushButton("Guides")
        self.guides_button.setProperty("role", "utility")
        self.guides_button.setCheckable(True)
        self.guides_button.setToolTip(
            "Show selection and object-boundary guides over the viewport"
        )
        preview_tools.addWidget(self.guides_button)
        self.preview_settings_button = QToolButton()
        self.preview_settings_button.setProperty("role", "settings")
        self.preview_settings_button.setCheckable(True)
        self.preview_settings_button.setText("Preview settings")
        self.preview_settings_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self.preview_settings_button.setToolTip(
            "Show viewport, localization, input, and preview scenario"
        )
        preview_tools.addWidget(self.preview_settings_button)

        self.undo_button.clicked.connect(self._undo)
        self.redo_button.clicked.connect(self._redo)
        self.mode_group.idToggled.connect(self._editor_mode_toggled)
        self.play_button.toggled.connect(self._play_toggled)
        self.restart_button.clicked.connect(self._restart_preview)
        self.guides_button.toggled.connect(self._guides_toggled)
        self.fit_button.clicked.connect(self._fit)
        self.reset_button.clicked.connect(self._reset)
        self.preview_frame.editingFinished.connect(self._preview_frame_edited)
        self.preview_settings_button.toggled.connect(
            self._runtime_settings_toggled
        )
        outer.addWidget(preview_bar)

        self.preview_settings_panel = QFrame(self)
        self.preview_settings_panel.setObjectName("guiRuntimeSettings")
        settings = QGridLayout(self.preview_settings_panel)
        settings.setContentsMargins(9, 7, 9, 7)
        settings.setHorizontalSpacing(7)
        settings.setVerticalSpacing(5)
        settings.addWidget(_toolbar_heading("PREVIEW CONTEXT"), 0, 0)
        settings.addWidget(_runtime_field_label("Viewport"), 0, 1)
        self.output_width = QSpinBox()
        self.output_height = QSpinBox()
        for field, value in ((self.output_width, 1920), (self.output_height, 1080)):
            field.setRange(1, 16384)
            field.setValue(value)
            field.setMaximumWidth(78)
            field.editingFinished.connect(self._output_size_changed)
        settings.addWidget(self.output_width, 0, 2)
        size_separator = QLabel("×")
        size_separator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        settings.addWidget(size_separator, 0, 3)
        settings.addWidget(self.output_height, 0, 4)
        settings.addWidget(_runtime_field_label("Safe area"), 0, 5)
        self.safe_area_ratio = QDoubleSpinBox()
        self.safe_area_ratio.setDecimals(3)
        self.safe_area_ratio.setRange(0.01, 1.0)
        self.safe_area_ratio.setSingleStep(0.01)
        self.safe_area_ratio.setValue(
            self.runtime_editor.default_safe_area_ratio
        )
        self.safe_area_ratio.setMaximumWidth(68)
        self.safe_area_ratio.setToolTip(self.runtime_editor.safe_area_tooltip)
        self.safe_area_ratio.valueChanged.connect(self._safe_area_changed)
        settings.addWidget(self.safe_area_ratio, 0, 6)
        settings.addWidget(_runtime_field_label("Language ID"), 0, 7)
        self.preview_language = QSpinBox()
        self.preview_language.setRange(0, 255)
        self.preview_language.setValue(self.runtime_editor.default_language)
        self.preview_language.setMaximumWidth(55)
        self.preview_language.setToolTip(
            "Runtime message language; it is not serialized in this GUI"
        )
        self.preview_language.valueChanged.connect(self._preview_language_changed)
        settings.addWidget(self.preview_language, 0, 8)
        settings.addWidget(_runtime_field_label("Input"), 0, 9)
        self.preview_input = QComboBox()
        self.preview_input.addItems(self.runtime_editor.input_devices)
        self.preview_input.setMaximumWidth(95)
        self.preview_input.setToolTip(self.runtime_editor.input_tooltip)
        self.preview_input.currentIndexChanged.connect(self._preview_input_changed)
        settings.addWidget(self.preview_input, 0, 10)
        settings.setColumnStretch(11, 1)

        settings.addWidget(_runtime_field_label("Preview scenario"), 1, 1)
        self.preview_scenario = QComboBox()
        self.preview_scenario.setMaximumWidth(420)
        self.preview_scenario.setToolTip(self.runtime_editor.scenario_tooltip)
        self.preview_scenario.currentIndexChanged.connect(
            self._preview_scenario_changed
        )
        settings.addWidget(self.preview_scenario, 1, 2, 1, 5)
        self.preview_scenario_status = QLabel("File default")
        self.preview_scenario_status.setObjectName("guiScenarioCoverage")
        settings.addWidget(self.preview_scenario_status, 1, 7, 1, 2)
        self.customize_preview_button = QPushButton("Customize")
        self.customize_preview_button.setProperty("role", "utility")
        self.customize_preview_button.setToolTip(
            "Create preview-only runtime values from the selected state"
        )
        self.customize_preview_button.clicked.connect(self._customize_preview)
        settings.addWidget(self.customize_preview_button, 1, 9)
        self.reset_custom_preview_button = QPushButton("Reset custom")
        self.reset_custom_preview_button.setProperty("role", "utility")
        self.reset_custom_preview_button.setToolTip(
            "Discard the custom preview state without changing the GUI file"
        )
        self.reset_custom_preview_button.clicked.connect(self._reset_custom_preview)
        settings.addWidget(self.reset_custom_preview_button, 1, 10)
        self.preview_settings_panel.hide()
        outer.addWidget(self.preview_settings_panel)

        splitter = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(210)
        self.tree.currentItemChanged.connect(self._tree_selection)
        splitter.addWidget(self.tree)

        self.canvas = self.adapter.create_canvas(self.dependencies)
        self.canvas.node_selected.connect(self._canvas_selection)
        self.canvas.node_moved.connect(self._canvas_move)
        self.canvas.frame_changed.connect(self._preview_frame_changed)
        self.canvas.diagnostics_changed.connect(self._preview_diagnostics_changed)
        splitter.addWidget(self.canvas)

        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(360)
        self.property_scroll = QScrollArea()
        self.property_scroll.setWidgetResizable(True)
        self.property_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.property_content = QWidget()
        self.property_content.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.property_layout = QVBoxLayout(self.property_content)
        self.property_layout.setContentsMargins(7, 7, 7, 7)
        self.property_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.property_scroll.setWidget(self.property_content)
        self.tabs.addTab(self.property_scroll, "Properties")
        self.tabs.addTab(self._preview_state_panel(), "Preview state")
        self.tabs.addTab(self._animation_panel(), "Animation")
        self.tabs.addTab(self._resource_panel(), "Resources")
        self.tabs.currentChanged.connect(self._editor_tab_changed)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        outer.addWidget(splitter, 1)

        # Select the initial mode only after the canvas exists: the mode signal
        # synchronizes both the controls and the renderer.
        self.layout_mode_button.setChecked(True)

    def _preview_state_panel(self) -> QWidget:
        scroll = QScrollArea()
        self.preview_state_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        content = QWidget()
        content.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.preview_control_layout = QVBoxLayout(content)
        self.preview_control_layout.setContentsMargins(7, 7, 7, 7)
        self.preview_control_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        tools = QHBoxLayout()
        self.save_preview_preset_button = QPushButton("Save preset")
        self.load_preview_preset_button = QPushButton("Load preset")
        self.save_preview_preset_button.clicked.connect(self._save_preview_preset)
        self.load_preview_preset_button.clicked.connect(self._load_preview_preset)
        tools.addWidget(self.save_preview_preset_button)
        tools.addWidget(self.load_preview_preset_button)
        tools.addStretch(1)
        self.preview_control_layout.addLayout(tools)
        self.preview_control_body = QVBoxLayout()
        self.preview_control_body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.preview_control_layout.addLayout(self.preview_control_body)
        scroll.setWidget(content)
        return scroll

    def _animation_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        form = QFormLayout()
        self.symbol_combo = QComboBox()
        self.animation_combo = QComboBox()
        self.animation_name = QLineEdit()
        self.animation_loop = QCheckBox("Loop at end")
        self.transition_combo = QComboBox()
        form.addRow("Instance", self.symbol_combo)
        form.addRow("State", self.animation_combo)
        form.addRow("State name", self.animation_name)
        form.addRow("Playback", self.animation_loop)
        form.addRow("Then play", self.transition_combo)
        layout.addLayout(form)
        self.symbol_combo.currentIndexChanged.connect(self._symbol_changed)
        self.animation_combo.currentIndexChanged.connect(self._animation_changed)
        self.animation_name.editingFinished.connect(self._animation_name_changed)
        self.animation_loop.toggled.connect(self._animation_loop_changed)
        self.transition_combo.currentIndexChanged.connect(self._transition_changed)

        self.track_tree = QTreeWidget()
        self.track_tree.setHeaderLabels(["Animated property", "Keys"])
        self.track_tree.currentItemChanged.connect(self._track_changed)
        layout.addWidget(self.track_tree, 1)
        self.key_table = QTableWidget(0, 3)
        self.key_table.setHorizontalHeaderLabels(["Frame", "Value", "Interpolation"])
        self.key_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.key_table.itemChanged.connect(self._key_changed)
        layout.addWidget(self.key_table, 1)
        buttons = QHBoxLayout()
        self.add_key_button = QPushButton("Add key")
        self.remove_key_button = QPushButton("Remove key")
        self.add_key_button.clicked.connect(self._add_key)
        self.remove_key_button.clicked.connect(self._remove_key)
        buttons.addWidget(self.add_key_button)
        buttons.addWidget(self.remove_key_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return panel

    def _resource_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        label = QLabel(
            "Declared dependencies. Imported properties are saved as path overrides; "
            "definition edits are localized into this GUI."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.resource_table = QTableWidget(0, 2)
        self.resource_table.setHorizontalHeaderLabels(["Kind", "Path"])
        self.resource_table.itemChanged.connect(self._resource_changed)
        layout.addWidget(self.resource_table)
        layout.addWidget(QLabel("Path bindings"))
        self.binding_table = QTableWidget(0, 3)
        self.binding_table.setHorizontalHeaderLabels(["Target", "Property", "Value"])
        self.binding_table.itemChanged.connect(self._binding_changed)
        layout.addWidget(self.binding_table)
        return panel

    def _build_workspace(self) -> None:
        current = self.preview_scenario.currentData()
        current_key = current.key if isinstance(current, GuiPreviewScenario) else None
        source = self.handler.filepath or self.document.source
        self.workspace = GuiWorkspace.from_document(
            source,
            self.document,
            self.handler.resolve_resource,
            profile=self.profile,
        )
        self.scene = self.workspace.instantiate()
        self._preview_scenarios = self.adapter.preview_scenarios(
            self.workspace.root_key,
            self.handler.resolve_resource,
            self.profile,
            self.scene,
        )
        self._rebase_custom_preview()
        self._install_preview_scenarios(current_key)

    def _rebase_custom_preview(self) -> None:
        if self._custom_preview_scenario is None:
            return
        try:
            self._custom_preview_scenario = self.adapter.rebase_custom_preview(
                self._preview_scenarios,
                self._custom_preview_scenario,
                self.scene,
            )
        except Exception as exc:
            self._custom_preview_scenario = None
            self._preview_diagnostic = f"Custom preview was reset: {exc}"

    def _install_preview_scenarios(self, selected_key: str | None = None) -> None:
        scenarios = [*self._preview_scenarios]
        if self._custom_preview_scenario is not None:
            scenarios.append(self._custom_preview_scenario)
        self.preview_scenario.blockSignals(True)
        self.preview_scenario.clear()
        for scenario in scenarios:
            self.preview_scenario.addItem(scenario.label, scenario)
            self.preview_scenario.setItemData(
                self.preview_scenario.count() - 1,
                scenario.description,
                Qt.ItemDataRole.ToolTipRole,
            )
        preferred = next(
            (
                index
                for index, scenario in enumerate(scenarios)
                if scenario.key == selected_key
            ),
            next(
                (
                    index
                    for index, scenario in enumerate(scenarios)
                    if scenario.preferred
                ),
                0,
            ),
        )
        self.preview_scenario.setCurrentIndex(preferred)
        self.preview_scenario.blockSignals(False)

    def _refresh_document(self, select_path: str = "/") -> None:
        self._updating = True
        try:
            self.tree.clear()
            item_by_path: dict[str, QTreeWidgetItem] = {}
            for node in self.scene.nodes:
                label = node.object.name if node.path != "/" else node.object.name or "Root"
                item = QTreeWidgetItem([label])
                item.setData(0, ROLE_VALUE, node)
                if node.resource.document is not self.document:
                    item.setToolTip(
                        0,
                        f"Imported from {node.resource.key}; edits are saved in this GUI",
                    )
                if node.parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    item_by_path[node.parent.path].addChild(item)
                item_by_path[node.path] = item
            self.tree.expandToDepth(2)
            self.canvas.set_document(self.scene)
            self._preview_scenario_changed(self.preview_scenario.currentIndex())
            width, height = self.scene.screen_size
            self.output_width.setValue(round(width))
            self.output_height.setValue(round(height))
            self.canvas.set_output_size(round(width), round(height))
            self._populate_animations()
            self._populate_resources()
            selected = item_by_path.get(select_path) or self.tree.topLevelItem(0)
            self.tree.setCurrentItem(selected)
        finally:
            self._updating = False
        self._update_status()

    def _rebuild_scene(self, select_path: str = "/") -> None:
        current = self.preview_scenario.currentData()
        current_key = current.key if isinstance(current, GuiPreviewScenario) else None
        self.workspace.invalidate()
        self.scene = self.workspace.instantiate()
        self._rebase_custom_preview()
        self._install_preview_scenarios(current_key)
        self._refresh_document(select_path)

    def _tree_selection(self, current, _previous) -> None:
        if current is None:
            return
        node = current.data(0, ROLE_VALUE)
        if isinstance(node, GuiSceneNode):
            self._select_node(node, from_canvas=False)

    def _canvas_selection(self, node: GuiSceneNode) -> None:
        self._select_node(node, from_canvas=True)

    def _select_node(self, node: GuiSceneNode, *, from_canvas: bool) -> None:
        self.selected_node = node
        if from_canvas:
            iterator = QTreeWidgetItemIterator(self.tree)
            while iterator.value():
                item = iterator.value()
                if item.data(0, ROLE_VALUE) is node:
                    self.tree.setCurrentItem(item)
                    break
                iterator += 1
        else:
            self.canvas.select_node(node)
        self._show_properties(node)
        self._show_preview_controls()

    def _editor_tab_changed(self, _index: int) -> None:
        if self.tabs.currentWidget() is self.preview_state_scroll:
            self._show_preview_controls()

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child is not None:
                self._clear_layout(child)

    def _show_properties(self, node: GuiSceneNode) -> None:
        self._clear_layout(self.property_layout)
        title = QLabel(node.object.name)
        font = QFont(title.font())
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        title.setFont(font)
        self.property_layout.addWidget(title)
        subtitle = QLabel(node.object.type_name.removeprefix("via.gui."))
        subtitle.setStyleSheet("color: #999")
        self.property_layout.addWidget(subtitle)
        binding_layers = self.scene.bindings_for(node)
        imported = node.resource.document is not self.document
        if imported:
            note = QLabel(
                f"Imported from {node.resource.key}. Property edits become local "
                "overrides; definition edits are localized automatically."
            )
            note.setWordWrap(True)
            self.property_layout.addWidget(note)

        effective = node.properties
        all_records = [*node.object.properties, *(item.property for item in binding_layers)]
        representative = {prop.name: prop for prop in all_records}
        groups: dict[str, list[tuple[str, GuiProperty]]] = defaultdict(list)
        for name, prop in representative.items():
            groups[_category(name, prop.type)].append((name, prop))
        order = ("Layout", "Appearance", "Text", "Resources", "Behavior", "Other")
        for category in order:
            records = groups.get(category)
            if not records:
                continue
            box = QGroupBox(category)
            form = QFormLayout(box)
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            )
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setHorizontalSpacing(8)
            form.setVerticalSpacing(5)
            for name, prop in records:
                layers = self.scene.editable_property_records(node, name)
                display_prop = copy.copy(prop)
                display_prop.component_mask = -1 if imported else _combined_mask(layers)
                widget = self._value_widget(
                    display_prop,
                    effective.get(name),
                    bool(self.scene.property_records(node, name)),
                )
                form.addRow(name, widget)
            self.property_layout.addWidget(box)
        special = self._special_widget(node)
        if special is not None:
            self.property_layout.addWidget(special)
        defaults = self._animation_defaults_widget(node)
        if defaults is not None:
            self.property_layout.addWidget(defaults)
        self.property_layout.addStretch(1)

    def _animation_defaults_widget(
        self,
        node: GuiSceneNode,
    ) -> QWidget | None:
        obj = node.object
        if not obj.animation_defaults:
            return None
        box = QGroupBox("Animation restoration defaults")
        table = QTableWidget(len(obj.animation_defaults), 2, box)
        table.setHorizontalHeaderLabels(["Property", "Restored value"])
        table.blockSignals(True)
        for row, prop in enumerate(obj.animation_defaults):
            name = QTableWidgetItem(prop.name)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            value = QTableWidgetItem(_display(prop.value))
            value.setData(ROLE_VALUE, prop)
            table.setItem(row, 0, name)
            table.setItem(row, 1, value)
        table.blockSignals(False)
        table.itemChanged.connect(self._animation_default_changed)
        layout = QVBoxLayout(box)
        layout.addWidget(table)
        return box

    def _animation_default_changed(self, item: QTableWidgetItem) -> None:
        prop = item.data(ROLE_VALUE)
        if not isinstance(prop, GuiProperty):
            return
        try:
            value = self._parse_value(prop.type, item.text(), prop.value)
        except Exception as exc:
            QMessageBox.warning(self, "Invalid animation default", str(exc))
            self._show_properties(self.selected_node)
            return
        self._edit_object_value(
            "Edit animation restoration default",
            prop,
            lambda _document, edit, target: edit.set(target, "value", value),
        )

    def _special_widget(self, node: GuiSceneNode) -> QWidget | None:
        obj = node.object
        special = obj.special_data
        if isinstance(special, GuiTextureSet):
            box = QGroupBox("Texture regions")
            table = QTableWidget(len(special.entries), 6, box)
            table.setHorizontalHeaderLabels(["Sequence", "Pattern", "Left", "Top", "Right", "Bottom"])
            table.blockSignals(True)
            for row, entry in enumerate(special.entries):
                values = [entry.sequence, entry.pattern, *entry.bounds]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(f"{value:g}" if isinstance(value, float) else str(value))
                    item.setData(ROLE_VALUE, (entry, column))
                    table.setItem(row, column, item)
            table.blockSignals(False)
            table.itemChanged.connect(self._special_changed)
            layout = QVBoxLayout(box)
            layout.addWidget(table)
            return box
        if isinstance(special, GuiNineSlice):
            box = QGroupBox("Nine-slice")
            layout = QVBoxLayout(box)
            border = QLineEdit(", ".join(f"{value:g}" for value in special.borders))
            border.editingFinished.connect(
                lambda field=border, value=special: self._nine_slice_borders(field, value)
            )
            layout.addWidget(QLabel("Borders: left, top, right, bottom"))
            layout.addWidget(border)
            table = QTableWidget(len(special.cells), 5)
            table.setHorizontalHeaderLabels(["Cell", "Sequence", "Pattern", "Repeat width", "Repeat height"])
            table.blockSignals(True)
            for row, cell in enumerate(special.cells):
                values = [cell.name.replace("_", " ").title(), cell.sequence, cell.pattern, *cell.repeat_size]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(f"{value:g}" if isinstance(value, float) else str(value))
                    item.setData(ROLE_VALUE, (cell, column))
                    if column == 0:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    table.setItem(row, column, item)
            table.blockSignals(False)
            table.itemChanged.connect(self._special_changed)
            layout.addWidget(table)
            return box
        return None

    def _special_changed(self, item: QTableWidgetItem) -> None:
        reference = item.data(ROLE_VALUE)
        if not reference:
            return
        target, column = reference
        try:
            if hasattr(target, "bounds"):
                if column == 0:
                    attribute, value = "sequence", int(item.text())
                    if not 0 <= value <= 0x7FFF_FFFF:
                        raise ValueError("sequence must be between 0 and 2147483647")
                elif column == 1:
                    attribute, value = "pattern", int(item.text())
                    if not 0 <= value <= 0xFFFF_FFFF:
                        raise ValueError("pattern must be between 0 and 4294967295")
                else:
                    bounds = list(target.bounds)
                    bounds[column - 2] = float(item.text())
                    self._require_float32(bounds[column - 2])
                    attribute, value = "bounds", tuple(bounds)
            else:
                if column == 1:
                    attribute, value = "sequence", int(item.text())
                elif column == 2:
                    attribute, value = "pattern", int(item.text())
                else:
                    repeat = list(target.repeat_size)
                    repeat[column - 3] = float(item.text())
                    self._require_float32(repeat[column - 3])
                    attribute, value = "repeat_size", tuple(repeat)
                if column in (1, 2) and not 0 <= value <= 0xFFFF_FFFF:
                    raise ValueError("sequence and pattern must be unsigned 32-bit values")
        except (ValueError, IndexError, OverflowError, struct.error) as exc:
            QMessageBox.warning(self, "Invalid texture region", str(exc))
            self._show_properties(self.selected_node)
            return
        self._edit_object_value(
            "Edit texture region",
            target,
            lambda _document, edit, localized: edit.set(localized, attribute, value),
        )

    def _nine_slice_borders(self, field: QLineEdit, special: GuiNineSlice) -> None:
        try:
            values = tuple(float(part.strip()) for part in field.text().split(","))
            if len(values) != 4:
                raise ValueError("four border values are required")
            for value in values:
                self._require_float32(value)
        except (ValueError, OverflowError, struct.error) as exc:
            QMessageBox.warning(self, "Invalid nine-slice borders", str(exc))
            field.setText(", ".join(f"{value:g}" for value in special.borders))
            return
        if values == special.borders:
            return
        self._edit_object_value(
            "Edit nine-slice borders",
            special,
            lambda _document, edit, localized: edit.set(localized, "borders", values),
        )

    @staticmethod
    def _require_float32(value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        struct.pack("<f", value)

    def _value_widget(
        self,
        prop: GuiProperty,
        value: Any,
        editable: bool,
        commit=None,
    ) -> QWidget:
        commit = commit or (
            lambda changed, name=prop.name: self._property_commit(name, changed)
        )
        if prop.type == PropertyType.BOOL:
            widget = QCheckBox()
            widget.setChecked(bool(value))
            widget.setEnabled(editable)
            widget.toggled.connect(commit)
            return widget
        components = COMPONENT_NAMES.get(prop.type)
        if components and isinstance(value, (list, tuple)):
            holder = QWidget()
            holder.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            layout = QGridLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setHorizontalSpacing(4)
            layout.setVerticalSpacing(3)
            editors = []
            integer_components = prop.type in GUI_INTEGER_VECTOR_TYPES
            components_per_row = 2 if len(value) == 4 else len(value)
            for index, component in enumerate(components[: len(value)]):
                row, pair = divmod(index, components_per_row)
                label = QLabel(component.upper())
                label.setObjectName("guiVectorComponentLabel")
                label.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                layout.addWidget(label, row, pair * 2)
                field = QDoubleSpinBox()
                field.setProperty("role", "vectorComponent")
                field.setMinimumWidth(44)
                field.setMaximumWidth(96)
                field.setSizePolicy(
                    QSizePolicy.Policy.Ignored,
                    QSizePolicy.Policy.Fixed,
                )
                if prop.type == PropertyType.COLOR:
                    field.setRange(0, 255)
                elif prop.type in UNSIGNED_VECTOR_COUNTS:
                    field.setRange(0, 0xFFFF_FFFF)
                elif prop.type in SIGNED_VECTOR_COUNTS:
                    field.setRange(-0x8000_0000, 0x7FFF_FFFF)
                else:
                    field.setRange(-1_000_000_000, 1_000_000_000)
                field.setDecimals(0 if integer_components else 4)
                field.setValue(float(value[index]))
                selected = prop.component_mask == -1 or bool(prop.component_mask & (1 << index))
                field.setEnabled(editable and selected)
                layout.addWidget(field, row, pair * 2 + 1)
                layout.setColumnStretch(pair * 2 + 1, 1)
                editors.append(field)
            for field in editors:
                field.editingFinished.connect(
                    lambda fields=editors, kind=prop.type, changed=commit: changed(
                        [int(item.value()) for item in fields]
                        if kind in GUI_INTEGER_VECTOR_TYPES
                        else [item.value() for item in fields],
                    )
                )
            return holder
        if prop.type in GUI_FLOAT_TYPES:
            widget = QDoubleSpinBox()
            widget.setRange(-1_000_000_000, 1_000_000_000)
            widget.setDecimals(6)
            widget.setValue(float(value or 0.0))
            widget.setEnabled(editable)
            widget.editingFinished.connect(
                lambda field=widget, changed=commit: changed(field.value())
            )
            return widget
        widget = QLineEdit(_display(value))
        widget.setReadOnly(not editable)
        if editable:
            widget.editingFinished.connect(
                lambda field=widget, kind=prop.type, old=value, changed=commit: self._value_text_commit(
                    kind, field, old, changed
                )
            )
        return widget

    @staticmethod
    def _parse_value(kind: PropertyType, text: str, current: Any = None) -> Any:
        if kind in GUI_STRING_TYPES:
            value = text
        elif kind in GUI_INTEGER_TYPES:
            value = int(text.strip(), 10)
        elif kind in GUI_FLOAT_TYPES:
            value = float(text.strip())
        elif kind == PropertyType.GUID:
            value = uuid.UUID(text.strip())
        elif kind == PropertyType.BOOL:
            value = text.strip().casefold() in {"true", "on", "yes", "1"}
        elif isinstance(current, (list, tuple)):
            parsed = (
                json.loads(text)
                if text.lstrip().startswith("[")
                else [part.strip() for part in text.split(",")]
            )
            convert = int if kind in GUI_INTEGER_VECTOR_TYPES else float
            value = [convert(item) for item in parsed]
        else:
            value = text
        return value

    def _value_text_commit(
        self,
        kind: PropertyType,
        field: QLineEdit,
        current: Any,
        commit,
    ) -> None:
        try:
            value = self._parse_value(kind, field.text(), current)
            self.adapter.validate_property_value(kind, value, "Value")
        except Exception as exc:
            QMessageBox.warning(self, "Invalid property value", str(exc))
            field.setText(_display(current))
            return
        commit(value)

    def _property_commit(self, name: str, value: Any) -> None:
        if self._updating or self.selected_node is None:
            return
        self._set_node_property(self.selected_node, name, value, f"Change {name}")

    def _set_node_property(
        self,
        node: GuiSceneNode,
        name: str,
        value: Any,
        label: str,
    ) -> bool:
        obj = node.object
        records = self.scene.property_records(node, name)
        if not records:
            return False
        if _same_value(node.properties.get(name), value):
            return False
        imported = node.resource.document is not self.document

        if name == "Name" and isinstance(value, str):
            old_path = node.path
            parent_path = node.parent.path.rstrip("/") if node.parent else ""
            new_path = f"{parent_path}/{value}" if node.parent else "/"

            def rename(document, edit):
                target = (
                    self.scene.localize_object(edit, node).object
                    if imported
                    else obj
                )
                document.rename_object(edit, target, value)

            changed = self._edit(label, rename, refresh_canvas=False)
            if changed:
                self._rebuild_scene(new_path if old_path != "/" else "/")
            return changed

        layers = self.scene.editable_property_records(node, name)
        components = COMPONENT_NAMES.get(records[-1].type, ())
        covered = _combined_mask(layers)
        complete = bool(layers) and (
            not components
            or covered == -1
            or covered & ((1 << len(components)) - 1) == (1 << len(components)) - 1
        )
        created = False

        def change(document, edit):
            nonlocal created
            if imported and not complete:
                binding = GuiBinding(
                    node.path,
                    obj.type_name,
                    GuiProperty(name, records[-1].type, copy.deepcopy(value), -1),
                )
                edit.set(document, "bindings", [*document.bindings, binding])
                created = True
            else:
                document.set_effective_records(edit, layers, name, value)

        changed = self._edit(label, change, refresh_canvas=not imported)
        if not changed:
            return False
        if created:
            self._rebuild_scene(node.path)
            return True
        self._refresh_scene()
        self._show_properties(node)
        return True

    def _canvas_move(self, node: GuiSceneNode, x: float, y: float) -> None:
        current = node.properties.get("Position")
        if not isinstance(current, (list, tuple)) or len(current) < 2:
            return
        value = list(current)
        value[0], value[1] = x, y
        self._set_node_property(node, "Position", value, f"Move {node.object.name}")

    def _refresh_scene(self) -> None:
        self.scene.refresh()
        self.scene.apply_bindings()
        self.canvas.update_branch(self.scene.root)

    def _edit_object_value(self, label: str, target: object, operation) -> bool:
        node = self.selected_node
        if node is None:
            return False
        imported = node.resource.document is not self.document

        def change(document, edit):
            localized = self.scene.localize_object(edit, node) if imported else None
            operation(
                document,
                edit,
                localized.copy_of(target) if localized is not None else target,
            )

        changed = self._edit(label, change, refresh_canvas=not imported)
        if changed and imported:
            self._rebuild_scene(node.path)
        return changed

    def _edit_animation_value(self, label: str, operation) -> bool:
        owner = self.symbol_combo.currentData()
        animation = self.selected_animation
        if (
            not isinstance(owner, GuiSceneNode)
            or owner.prototype is None
            or animation is None
        ):
            return False
        symbol = owner.prototype.symbol
        animation_index = symbol.animations.index(animation)
        imported = owner.prototype.resource.document is not self.document

        def change(document, edit):
            localized = self.scene.localize_prototype(edit, owner) if imported else None
            remap = localized.copy_of if localized is not None else lambda value: value
            operation(document, edit, remap)

        changed = self._edit(label, change, refresh_canvas=not imported)
        if changed and imported:
            self._rebuild_scene(owner.path)
            self._select_animation(owner.path, animation_index)
        return changed

    def _select_animation(self, owner_path: str, animation_index: int) -> None:
        for index in range(self.symbol_combo.count()):
            candidate = self.symbol_combo.itemData(index)
            if isinstance(candidate, GuiSceneNode) and candidate.path == owner_path:
                self.symbol_combo.setCurrentIndex(index)
                if 0 <= animation_index < self.animation_combo.count():
                    self.animation_combo.setCurrentIndex(animation_index)
                return

    def _edit(self, label: str, operation, *, refresh_canvas: bool = True) -> bool:
        try:
            changed = self.gui_file.edit(label, operation)
        except Exception as exc:
            QMessageBox.warning(self, "GUI edit rejected", str(exc))
            return False
        if changed:
            if refresh_canvas:
                self.canvas.document_changed()
            self.modified_changed.emit(self.gui_file.modified)
            self._update_status()
        return changed

    def _populate_animations(self) -> None:
        self.symbol_combo.clear()
        for node in self.scene.nodes:
            if node.prototype is None or not node.prototype.symbol.animations:
                continue
            symbol = node.prototype.symbol
            label = f"{symbol.name} — {node.path}"
            if node.prototype.resource.document is not self.document:
                label += " (imported)"
            self.symbol_combo.addItem(label, node)
        self._symbol_changed(0)

    def _symbol_changed(self, _index: int) -> None:
        if self._updating and self.symbol_combo.count() == 0:
            return
        owner = self.symbol_combo.currentData()
        symbol = owner.prototype.symbol if isinstance(owner, GuiSceneNode) and owner.prototype else None
        self.animation_combo.clear()
        if isinstance(symbol, GuiSymbol):
            occurrences: dict[str, int] = defaultdict(int)
            for animation in symbol.animations:
                occurrences[animation.name] += 1
                suffix = (
                    f" ({occurrences[animation.name]})"
                    if len(symbol.animation_states()[animation.name]) > 1
                    else ""
                )
                self.animation_combo.addItem(animation.name + suffix, animation)
        self._animation_changed(0)

    def _animation_changed(self, _index: int) -> None:
        animation = self.animation_combo.currentData()
        self.selected_animation = animation if isinstance(animation, GuiAnimation) else None
        was_updating = self._updating
        self._updating = True
        try:
            self.track_tree.clear()
            self.key_table.setRowCount(0)
            if self.selected_animation is None:
                return
            self.animation_name.setText(self.selected_animation.name)
            self.animation_loop.setChecked(self.selected_animation.loop)
            self.transition_combo.clear()
            self.transition_combo.addItem("Stop", None)
            owner = self.symbol_combo.currentData()
            symbol = (
                owner.prototype.symbol
                if isinstance(owner, GuiSceneNode) and owner.prototype
                else None
            )
            for candidate in symbol.animations if symbol is not None else ():
                self.transition_combo.addItem(candidate.name, candidate)
                if candidate is self.selected_animation.transition:
                    self.transition_combo.setCurrentIndex(self.transition_combo.count() - 1)
            root = self.selected_animation.clip.root
            root_item = QTreeWidgetItem([root.name or "Animation", ""])
            self.track_tree.addTopLevelItem(root_item)
            for prop in root.properties:
                self._add_track_item(root_item, prop)
            for node in root.children:
                node_item = QTreeWidgetItem([node.name, ""])
                root_item.addChild(node_item)
                for prop in node.properties:
                    self._add_track_item(node_item, prop)
            self.track_tree.expandAll()
        finally:
            self._updating = was_updating
            owner = self.symbol_combo.currentData()
            symbol = (
                owner.prototype.symbol
                if isinstance(owner, GuiSceneNode) and owner.prototype
                else None
            )
            self.canvas.select_animation(
                symbol,
                self.selected_animation,
                owner.path if isinstance(owner, GuiSceneNode) else None,
            )

    def _add_track_item(self, parent: QTreeWidgetItem, prop: ClipProperty) -> None:
        item = QTreeWidgetItem([prop.name, str(len(prop.keys)) if prop.keys else ""])
        item.setData(0, ROLE_VALUE, prop)
        parent.addChild(item)
        for child in prop.children:
            self._add_track_item(item, child)

    def _track_changed(self, current, _previous) -> None:
        prop = current.data(0, ROLE_VALUE) if current else None
        self.selected_track = prop if isinstance(prop, ClipProperty) else None
        self._populate_keys()

    def _populate_keys(self) -> None:
        was_updating = self._updating
        self._updating = True
        try:
            keys = self.selected_track.keys if self.selected_track else []
            self.key_table.setRowCount(len(keys))
            for row, key in enumerate(keys):
                frame = QTableWidgetItem(f"{key.frame:g}")
                value = QTableWidgetItem(_display(key.value))
                frame.setData(ROLE_VALUE, key)
                value.setData(ROLE_VALUE, key)
                if (
                    key.interpolation not in ASCII_EVENT_MODES
                    and self.selected_track.property_type
                    in {PropertyType.ACTION, PropertyType.UNKNOWN}
                ):
                    value.setFlags(value.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.key_table.setItem(row, 0, frame)
                self.key_table.setItem(row, 1, value)
                interpolation = QComboBox()
                for mode in self._interpolation_options(self.selected_track, key):
                    interpolation.addItem(mode.name.replace("_", " ").title(), mode)
                    if mode == key.interpolation:
                        interpolation.setCurrentIndex(interpolation.count() - 1)
                interpolation.currentIndexChanged.connect(
                    lambda _index, field=interpolation, selected=key: self._interpolation_changed(
                        selected, field.currentData()
                    )
                )
                self.key_table.setCellWidget(row, 2, interpolation)
            editable = bool(self.selected_track and self.selected_track.property_type not in CONTAINER_PROPERTY_TYPES)
            self.add_key_button.setEnabled(editable)
            self.remove_key_button.setEnabled(editable and bool(keys))
        finally:
            self._updating = was_updating

    @staticmethod
    def _interpolation_options(prop: ClipProperty | None, key: ClipKey):
        if prop is None:
            return (key.interpolation,)
        if prop.property_type == PropertyType.BOOL:
            options = [ClipInterpolation.DISCRETE]
        elif prop.property_type in CLIP_STRING_TYPES:
            options = [ClipInterpolation.DISCRETE]
            if prop.property_type in (PropertyType.STR8, PropertyType.ENUM):
                options += [ClipInterpolation.EVENT, ClipInterpolation.PASS_EVENT]
        else:
            options = [
                ClipInterpolation.DISCRETE,
                ClipInterpolation.LINEAR,
                ClipInterpolation.OFFSET_FRAME,
            ]
            if key.curve is not None:
                options += [ClipInterpolation.HERMITE, ClipInterpolation.BEZIER]
        if key.interpolation not in options:
            options.insert(0, key.interpolation)
        return tuple(dict.fromkeys(options))

    def _interpolation_changed(
        self,
        key: ClipKey,
        interpolation: ClipInterpolation,
    ) -> None:
        if self._updating or interpolation == key.interpolation:
            return
        prop = self.selected_track
        if prop is None:
            return

        def change(_document, edit, remap):
            target_key = remap(key)
            old_is_event = target_key.interpolation in ASCII_EVENT_MODES
            new_is_event = interpolation in ASCII_EVENT_MODES
            if old_is_event != new_is_event:
                if new_is_event:
                    value = "" if target_key.value is None else str(target_key.value)
                else:
                    try:
                        value = self._parse_clip_value(
                            prop.property_type,
                            str(target_key.value or ""),
                            interpolation,
                            self.adapter.validate_property_value,
                        )
                    except (TypeError, ValueError, OverflowError, struct.error):
                        value = self._default_clip_key_value(prop.property_type)
                edit.set(target_key, "value", value)
            edit.set(target_key, "interpolation", interpolation)
            if interpolation not in (ClipInterpolation.HERMITE, ClipInterpolation.BEZIER):
                edit.set(target_key, "curve", None)

        if self._edit_animation_value("Change key interpolation", change):
            self._populate_keys()

    def _key_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or self.selected_track is None or item.column() not in (0, 1):
            return
        key = item.data(ROLE_VALUE)
        if not isinstance(key, ClipKey):
            return
        try:
            if item.column() == 0:
                value = float(item.text())
                duration = self.selected_animation.clip.total_frame if self.selected_animation else value
                if not 0 <= value <= duration:
                    raise ValueError(f"frame must be between 0 and {duration:g}")

                def change(_document, edit, remap):
                    target_track = remap(self.selected_track)
                    keys = copy.deepcopy(target_track.keys)
                    keys[item.row()].frame = value
                    edit.set(
                        target_track,
                        "keys",
                        sorted(keys, key=lambda candidate: candidate.frame),
                    )
            else:
                value = self._parse_clip_value(
                    self.selected_track.property_type,
                    item.text(),
                    key.interpolation,
                    self.adapter.validate_property_value,
                )

                def change(_document, edit, remap):
                    edit.set(remap(key), "value", value)
        except Exception as exc:
            QMessageBox.warning(self, "Invalid key", str(exc))
            self._populate_keys()
            return
        if self._edit_animation_value("Edit animation key", change):
            self._populate_keys()

    def _add_key(self) -> None:
        prop = self.selected_track
        if prop is None or prop.property_type in CONTAINER_PROPERTY_TYPES:
            return
        source = prop.keys[-1] if prop.keys else prop.last_key
        if source is not None:
            key = copy.deepcopy(source)
            key.frame = min(
                self.selected_animation.clip.total_frame,
                source.frame + 1.0,
            )
        else:
            interpolation = (
                ClipInterpolation.DISCRETE
                if prop.property_type == PropertyType.BOOL
                or prop.property_type in CLIP_STRING_TYPES
                or prop.property_type in {PropertyType.ACTION, PropertyType.UNKNOWN}
                else ClipInterpolation.LINEAR
            )
            key = ClipKey(
                interpolation=interpolation,
                value=self._default_clip_key_value(prop.property_type),
            )
        keys = sorted([*prop.keys, key], key=lambda item: item.frame)
        if self._edit_animation_value(
            "Add animation key",
            lambda _document, edit, remap: edit.set(remap(prop), "keys", keys),
        ):
            self._animation_changed(self.animation_combo.currentIndex())

    def _remove_key(self) -> None:
        prop = self.selected_track
        row = self.key_table.currentRow()
        if prop is None or not 0 <= row < len(prop.keys):
            return
        keys = [item for index, item in enumerate(prop.keys) if index != row]
        if self._edit_animation_value(
            "Remove animation key",
            lambda _document, edit, remap: edit.set(remap(prop), "keys", keys),
        ):
            self._animation_changed(self.animation_combo.currentIndex())

    @staticmethod
    def _default_clip_key_value(kind: PropertyType):
        if kind == PropertyType.BOOL:
            return False
        if kind in GUI_INTEGER_TYPES:
            return 0
        if kind in GUI_FLOAT_TYPES:
            return 0.0
        if kind in CLIP_STRING_TYPES:
            return ""
        if kind == PropertyType.PATH_POINT3D:
            return (0.0, 0.0, 0.0)
        return None

    @staticmethod
    def _parse_clip_value(
        kind: PropertyType,
        text: str,
        interpolation: ClipInterpolation,
        validate,
    ):
        if interpolation in ASCII_EVENT_MODES:
            if "\0" in text:
                raise ValueError("event text cannot contain NUL")
            text.encode("ascii")
            return text
        if kind == PropertyType.BOOL:
            return text.strip().casefold() in {"true", "on", "yes", "1"}
        if kind in GUI_INTEGER_TYPES:
            value = int(text.strip(), 10)
            validate(kind, value, "Animation key")
            return value
        if kind in GUI_FLOAT_TYPES:
            value = float(text.strip())
            if not math.isfinite(value):
                raise ValueError("animation key must be finite")
            if kind == PropertyType.F32:
                struct.pack("<f", value)
            validate(kind, value, "Animation key")
            return value
        if kind in CLIP_STRING_TYPES:
            if "\0" in text:
                raise ValueError("animation strings cannot contain NUL")
            if kind in ASCII_VALUE_PROPERTY_TYPES:
                text.encode("ascii")
            return text
        if kind == PropertyType.PATH_POINT3D:
            values = tuple(float(part.strip()) for part in text.split(","))
            if len(values) != 3:
                raise ValueError("PathPoint3D requires x, y, z")
            if any(not math.isfinite(value) for value in values):
                raise ValueError("PathPoint3D components must be finite")
            for value in values:
                struct.pack("<f", value)
            return values
        if kind in {PropertyType.ACTION, PropertyType.UNKNOWN}:
            return None
        raise ValueError(f"{kind.name} cannot own compact animation keys")

    def _animation_name_changed(self) -> None:
        if self._updating or self.selected_animation is None:
            return
        value = self.animation_name.text().strip()
        if value == self.selected_animation.name:
            return
        owner = self.symbol_combo.currentData()
        symbol = owner.prototype.symbol if isinstance(owner, GuiSceneNode) and owner.prototype else None
        index = symbol.animations.index(self.selected_animation) if symbol is not None else 0
        path = owner.path if isinstance(owner, GuiSceneNode) else "/"
        if value and self._edit_animation_value(
            "Rename animation state",
            lambda _document, edit, remap: edit.set(
                remap(self.selected_animation), "name", value
            ),
        ):
            self._populate_animations()
            self._select_animation(path, index)

    def _animation_loop_changed(self, value: bool) -> None:
        if not self._updating and self.selected_animation is not None:
            self._edit_animation_value(
                "Change animation looping",
                lambda _document, edit, remap: edit.set(
                    remap(self.selected_animation), "loop", value
                ),
            )

    def _transition_changed(self, _index: int) -> None:
        if not self._updating and self.selected_animation is not None:
            target = self.transition_combo.currentData()
            self._edit_animation_value(
                "Change animation transition",
                lambda _document, edit, remap: edit.set(
                    remap(self.selected_animation), "transition", remap(target)
                ),
            )

    def _populate_resources(self) -> None:
        self.resource_table.blockSignals(True)
        self.binding_table.blockSignals(True)
        rows = [
            ("Imported GUI", path, "imported_gui_paths", index)
            for index, path in enumerate(self.document.imported_gui_paths)
        ]
        rows += [("Asset", path, "asset_paths", index) for index, path in enumerate(self.document.asset_paths)]
        self.resource_table.setRowCount(len(rows))
        for row, (kind, path, attribute, index) in enumerate(rows):
            kind_item = QTableWidgetItem(kind)
            kind_item.setFlags(kind_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            path_item = QTableWidgetItem(path)
            path_item.setData(ROLE_VALUE, (attribute, index))
            self.resource_table.setItem(row, 0, kind_item)
            self.resource_table.setItem(row, 1, path_item)
        self.binding_table.setRowCount(len(self.document.bindings))
        for row, binding in enumerate(self.document.bindings):
            target = QTableWidgetItem(binding.target_path)
            target.setFlags(target.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name = QTableWidgetItem(binding.property.name)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            value = QTableWidgetItem(_display(binding.property.value))
            value.setData(ROLE_VALUE, binding)
            self.binding_table.setItem(row, 0, target)
            self.binding_table.setItem(row, 1, name)
            self.binding_table.setItem(row, 2, value)
        self.resource_table.blockSignals(False)
        self.binding_table.blockSignals(False)

    def _resource_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != 1:
            return
        reference = item.data(ROLE_VALUE)
        if not reference:
            return
        attribute, index = reference
        values = list(getattr(self.document, attribute))
        values[index] = item.text().strip()
        if not self._edit(
            "Change dependency path",
            lambda _document, edit: edit.set(self.document, attribute, values),
        ):
            return
        if attribute == "imported_gui_paths":
            try:
                self._build_workspace()
                self._refresh_document()
            except Exception as exc:
                self.gui_file.undo()
                QMessageBox.warning(self, "Dependency change rejected", str(exc))
                self._build_workspace()
                self._refresh_document()
                self.modified_changed.emit(self.gui_file.modified)

    def _binding_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != 2:
            return
        binding = item.data(ROLE_VALUE)
        if binding is None:
            return
        try:
            value = self._parse_value(binding.property.type, item.text(), binding.property.value)
        except Exception as exc:
            QMessageBox.warning(self, "Invalid binding value", str(exc))
            self._populate_resources()
            return
        if self._edit("Edit path binding", lambda _document, edit: edit.set(binding.property, "value", value)):
            self.scene.refresh()
            self.scene.apply_bindings()
            self.canvas.update_branch(self.scene.root)

    def _undo(self) -> None:
        dependency_change = self.gui_file.undo_label == "Change dependency path"
        if self.gui_file.undo():
            self.modified_changed.emit(self.gui_file.modified)
            if dependency_change:
                self._build_workspace()
                self._refresh_document()
            else:
                self._rebuild_scene(self.selected_node.path if self.selected_node else "/")

    def _redo(self) -> None:
        dependency_change = self.gui_file.redo_label == "Change dependency path"
        if self.gui_file.redo():
            self.modified_changed.emit(self.gui_file.modified)
            if dependency_change:
                self._build_workspace()
                self._refresh_document()
            else:
                self._rebuild_scene(self.selected_node.path if self.selected_node else "/")

    def _reset(self) -> None:
        if not self.gui_file.modified:
            return
        if (
            QMessageBox.question(self, "Revert GUI", "Discard all unsaved GUI edits?")
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.gui_file.reset()
        self.document = self.gui_file.require_document()
        self._build_workspace()
        self._refresh_document()
        self.modified_changed.emit(False)

    def _fit(self) -> None:
        self.canvas.fit_document()

    @property
    def editor_mode(self) -> str:
        """The mode selected by the editor's exclusive mode control."""

        mode_id = self.mode_group.checkedId()
        if not 0 <= mode_id < len(_EDITOR_MODES):
            raise RuntimeError("GUI editor has no selected mode")
        return _EDITOR_MODES[mode_id]

    def _editor_mode_toggled(self, mode_id: int, enabled: bool) -> None:
        if not enabled:
            return
        mode = _EDITOR_MODES[mode_id]
        preview_enabled = mode != "layout"
        if not preview_enabled and self.play_button.isChecked():
            self.play_button.setChecked(False)
        self.canvas.set_preview_enabled(preview_enabled)
        self.canvas.set_interaction_enabled(mode == "interact")
        self.play_button.setEnabled(preview_enabled)
        self.restart_button.setEnabled(preview_enabled)
        self.preview_frame.setEnabled(preview_enabled)
        self.preview_frame_total.setEnabled(preview_enabled)
        self._sync_mode_status()

    def _play_toggled(self, enabled: bool) -> None:
        playing = bool(enabled) and self.editor_mode != "layout"
        if enabled != playing:
            self.play_button.blockSignals(True)
            self.play_button.setChecked(playing)
            self.play_button.blockSignals(False)
        self.canvas.set_playing(playing)
        self._sync_mode_status()

    def _sync_mode_status(self) -> None:
        mode = self.editor_mode
        playing = self.play_button.isChecked() and mode != "layout"
        self.play_button.setText("Pause" if playing else "Play")
        self.play_button.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_MediaPause
                if playing
                else QStyle.StandardPixmap.SP_MediaPlay
            )
        )
        self.play_button.setToolTip(
            "Pause the selected animation"
            if playing
            else "Play the selected animation"
        )

        if mode == "layout":
            status = "LAYOUT MODE"
            description = "Schematic view · select and move elements"
        elif mode == "interact":
            status = f"INTERACT · {'PLAYING' if playing else 'READY'}"
            description = self.runtime_editor.interaction_description
        else:
            status = f"PREVIEW · {'PLAYING' if playing else 'PAUSED'}"
            description = "Rendered and editable · drag to move elements"
        self.mode_status.setText(status)
        self.mode_status.setToolTip(description)
        self.mode_status.setProperty("mode", mode)
        self.mode_status.setProperty("activity", "playing" if playing else "idle")
        self.mode_description.setText(description)
        self.mode_description.setToolTip(description)
        style = self.mode_status.style()
        style.unpolish(self.mode_status)
        style.polish(self.mode_status)
        self.mode_status.update()

    def _runtime_settings_toggled(self, enabled: bool) -> None:
        self.preview_settings_panel.setVisible(enabled)
        self.preview_settings_button.setText(
            "Hide settings" if enabled else "Preview settings"
        )
        self.preview_settings_button.setToolTip(
            "Hide viewport, localization, input, and preview scenario"
            if enabled
            else "Show viewport, localization, input, and preview scenario"
        )

    def _restart_preview(self) -> None:
        self.canvas.restart_animation()

    def _guides_toggled(self, enabled: bool) -> None:
        self.canvas.set_guides(enabled)

    def _preview_frame_edited(self) -> None:
        if not self._updating:
            self.canvas.set_frame(self.preview_frame.value())

    def _preview_frame_changed(self, frame: float, duration: float) -> None:
        self.preview_frame.blockSignals(True)
        self.preview_frame.setRange(0.0, max(0.0, duration))
        self.preview_frame.setValue(min(max(0.0, frame), max(0.0, duration)))
        self.preview_frame.blockSignals(False)
        duration_text = f"{max(0.0, duration):.2f}".rstrip("0").rstrip(".")
        self.preview_frame_total.setText(f"/ {duration_text}")

    def _output_size_changed(self) -> None:
        if not self._updating:
            self.canvas.set_output_size(
                self.output_width.value(),
                self.output_height.value(),
            )

    def _safe_area_changed(self, ratio: float) -> None:
        self.canvas.set_safe_area_ratio(ratio)

    def _preview_language_changed(self, language: int) -> None:
        self.canvas.set_language(language)

    def _preview_input_changed(self, index: int) -> None:
        self.canvas.set_input_device(index)

    def _customize_preview(self) -> None:
        scenario = self.preview_scenario.currentData()
        if not isinstance(scenario, GuiPreviewScenario):
            return
        self._customizing_preview = True
        self.tabs.setCurrentWidget(self.preview_state_scroll)
        self._show_preview_controls()

    def _reset_custom_preview(self) -> None:
        custom = self._custom_preview_scenario
        if custom is None:
            return
        base_key = custom.base_key
        self._custom_preview_scenario = None
        self._customizing_preview = False
        self._install_preview_scenarios(base_key)
        self._preview_scenario_changed(self.preview_scenario.currentIndex())

    def _preview_control_widget(self, control: GuiPreviewControl) -> QWidget:
        commit = lambda value, selected=control: self._preview_control_changed(
            selected,
            value,
        )
        if control.options:
            field: QWidget = QComboBox()
            for option in control.options:
                field.addItem(option.label, option.value)
                if option.description:
                    field.setItemData(
                        field.count() - 1,
                        option.description,
                        Qt.ItemDataRole.ToolTipRole,
                    )
            selected_index = next(
                (
                    index
                    for index, option in enumerate(control.options)
                    if option.value == control.value
                ),
                0,
            )
            field.setCurrentIndex(selected_index)
            field.currentIndexChanged.connect(
                lambda _index, combo=field, changed=commit: changed(
                    combo.currentData()
                )
            )
        else:
            if control.value_type is None:
                return QLabel("Unavailable")
            prop = GuiProperty(
                control.label,
                control.value_type,
                copy.deepcopy(control.value),
                -1,
            )
            field = self._value_widget(prop, control.value, True, commit)
            if isinstance(field, QDoubleSpinBox):
                minimum = control.minimum
                maximum = control.maximum
                field.setRange(
                    minimum if minimum is not None else field.minimum(),
                    maximum if maximum is not None else field.maximum(),
                )
                if control.decimals is not None:
                    field.setDecimals(control.decimals)
        field.setToolTip(control.description)
        if not control.can_inherit:
            return field
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        toggle = QCheckBox("Override")
        toggle.setChecked(not control.inherited)
        toggle.setToolTip(
            "Enable a preview-only override; disable it to inherit runtime state"
        )
        field.setEnabled(not control.inherited)
        toggle.toggled.connect(
            lambda enabled, selected=control: self._preview_control_changed(
                selected,
                selected.value,
                inherit=not enabled,
            )
        )
        layout.addWidget(toggle)
        layout.addWidget(field, 1)
        return holder

    def _show_preview_controls(self) -> None:
        if not hasattr(self, "preview_control_body"):
            return
        self._clear_layout(self.preview_control_body)
        scenario = self.preview_scenario.currentData()
        self.save_preview_preset_button.setEnabled(
            isinstance(scenario, GuiPreviewScenario) and scenario.custom
        )
        self.reset_custom_preview_button.setEnabled(
            self._custom_preview_scenario is not None
        )
        if self.tabs.currentWidget() is not self.preview_state_scroll:
            return
        if not isinstance(scenario, GuiPreviewScenario):
            return
        self.customize_preview_button.setText(
            "Edit custom" if scenario.custom else "Customize"
        )
        if not scenario.custom and not self._customizing_preview:
            note = QLabel(
                "Customize this state to control understood game-runtime values "
                "without changing the GUI file."
            )
            note.setWordWrap(True)
            self.preview_control_body.addWidget(note)
            button = QPushButton("Customize this state")
            button.clicked.connect(self._customize_preview)
            self.preview_control_body.addWidget(button)
            self.preview_control_body.addStretch(1)
            return
        selected_path = (
            self.selected_node.path
            if self.selected_node is not None
            and self.selected_node.path in self.scene.nodes_by_path
            else None
        )
        try:
            controls = self.adapter.preview_controls(
                self._preview_scenarios,
                scenario,
                self.scene,
                selected_path,
            )
        except Exception as exc:
            note = QLabel(f"Custom preview controls are unavailable: {exc}")
            note.setWordWrap(True)
            self.preview_control_body.addWidget(note)
            return
        note = QLabel(
            "Preview-only controls. File properties remain editable in the "
            "Properties tab and are the only values written into the GUI."
        )
        note.setWordWrap(True)
        self.preview_control_body.addWidget(note)
        groups: dict[str, list[GuiPreviewControl]] = defaultdict(list)
        for control in controls:
            groups[control.group].append(control)
        for group, values in groups.items():
            box = QGroupBox(group)
            form = QFormLayout(box)
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            )
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            for control in values:
                form.addRow(control.label, self._preview_control_widget(control))
            self.preview_control_body.addWidget(box)
        if not controls:
            empty = QLabel("No understood runtime controls are available here.")
            empty.setWordWrap(True)
            self.preview_control_body.addWidget(empty)
        self.preview_control_body.addStretch(1)

    def _preview_control_changed(
        self,
        control: GuiPreviewControl,
        value: Any,
        *,
        inherit: bool = False,
    ) -> None:
        if self._updating:
            return
        scenario = self.preview_scenario.currentData()
        if not isinstance(scenario, GuiPreviewScenario):
            return
        try:
            custom = self.adapter.set_preview_control(
                self._preview_scenarios,
                scenario,
                self.scene,
                control.key,
                value,
                inherit=inherit,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Preview value rejected", str(exc))
            self._show_preview_controls()
            return
        self._custom_preview_scenario = custom
        self._customizing_preview = True
        self._install_preview_scenarios(custom.key)
        self._preview_scenario_changed(self.preview_scenario.currentIndex())

    def _preset_suggestion(self) -> str:
        source = Path(self.handler.filepath or self.document.source)
        name = source.name if source.name not in {"", "<bytes>"} else "gui"
        return str(source.with_name(name + ".reasy-preview.json"))

    def _save_preview_preset(self) -> None:
        scenario = self.preview_scenario.currentData()
        if not isinstance(scenario, GuiPreviewScenario) or not scenario.custom:
            return
        filename, _filter = QFileDialog.getSaveFileName(
            self,
            "Save GUI preview preset",
            self._preset_suggestion(),
            "REasy GUI preview (*.reasy-preview.json);;JSON (*.json)",
        )
        if not filename:
            return
        try:
            payload = self.adapter.export_preview_preset(
                self.workspace.root_key,
                scenario,
            )
            Path(filename).write_text(
                json.dumps(payload, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Could not save preview preset", str(exc))

    def _load_preview_preset(self) -> None:
        filename, _filter = QFileDialog.getOpenFileName(
            self,
            "Load GUI preview preset",
            self._preset_suggestion(),
            "REasy GUI preview (*.reasy-preview.json);;JSON (*.json)",
        )
        if not filename:
            return
        try:
            payload = json.loads(Path(filename).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("preview preset root must be an object")
            custom = self.adapter.import_preview_preset(
                self.workspace.root_key,
                payload,
                self._preview_scenarios,
                self.scene,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Could not load preview preset", str(exc))
            return
        self._custom_preview_scenario = custom
        self._customizing_preview = True
        self._install_preview_scenarios(custom.key)
        self._preview_scenario_changed(self.preview_scenario.currentIndex())
        self.tabs.setCurrentWidget(self.preview_state_scroll)

    def _preview_scenario_changed(self, _index: int) -> None:
        scenario = self.preview_scenario.currentData()
        self.adapter.apply_preview_scenario(self.canvas, scenario)
        if scenario is None:
            self.preview_scenario_status.clear()
            return
        self.preview_scenario_status.setText(scenario.coverage)
        status = (
            "partial"
            if scenario.issues
            else "custom"
            if scenario.custom
            else "complete"
            if scenario.coverage != "File default"
            else "default"
        )
        self.preview_scenario_status.setProperty("status", status)
        self.preview_scenario_status.style().unpolish(self.preview_scenario_status)
        self.preview_scenario_status.style().polish(self.preview_scenario_status)
        details = [scenario.description, *scenario.issues]
        self.preview_scenario_status.setToolTip("\n".join(details))
        self.preview_scenario.setToolTip(
            self.runtime_editor.scenario_tooltip + "\n\n" + "\n".join(details)
        )
        if not scenario.custom:
            self._customizing_preview = False
        self._show_preview_controls()

    def _preview_diagnostics_changed(self, message: str) -> None:
        self._preview_diagnostic = message
        self._update_status()

    def _update_status(self) -> None:
        summary = self.document.summary()
        problems = [
            *self.workspace.missing_dependencies.values(),
            *(
                f"Unresolved binding: {item.target_path}"
                for item in self.scene.unresolved_bindings
            ),
        ]
        if self._preview_diagnostic:
            problems.append(self._preview_diagnostic)
        self.status.setText(
            f"{summary['objects']} objects · {summary['animations']} animations"
            + (f" · {len(problems)} unresolved" if problems else "")
            + (" · Modified" if self.gui_file.modified else "")
        )
        self.status.setToolTip("\n".join(problems))
        self.undo_button.setEnabled(self.gui_file.can_undo)
        self.redo_button.setEnabled(self.gui_file.can_redo)
        self.undo_button.setToolTip(self.gui_file.undo_label or "Nothing to undo")
        self.redo_button.setToolTip(self.gui_file.redo_label or "Nothing to redo")

    def on_saved(self) -> None:
        self._update_status()
        self.modified_changed.emit(False)
