from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from utils.app_paths import PROJECT_METADATA_FOLDER
from services.file_io import write_files_atomically

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP, Qt, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from file_handlers.sdf.shader_graph import (
    ShaderContract,
    ShaderGraphError,
    ShaderGraphNode,
    node_title,
    shader_graph_node_extension,
    node_pins,
    connection_compatible,
    validate_node_update,
)
from file_handlers.sdf.shader_graph_editor import (
    _ShaderGraphScene,
    _ShaderGraphView,
    _display_name,
    _comparison_properties,
    _table_properties,
    _numeric_properties,
    _code_properties,
    _texture_properties,
    add_palette_templates,
    filter_palette,
)
from file_handlers.sdf.shader_graph_buffer import structured_buffer_stride
from file_handlers.sdf.shader_graph_math import (
    BASIC_MATH_KINDS, BASIC_MATH_NODES, EXTRA_MATH_NODES, NUMERIC_VALUE_TYPES, UNARY_FUNCTIONS, TYPED_MATH_NODES,
    TYPED_MATH_CATEGORIES, is_float_type, node_label, numeric_type_label, value_width,
)

from .material_project import (
    MaterialBuildError,
    MaterialParameterDefinition,
    MaterialProjectError,
    MaterialTextureDefinition,
    MmtrMaterialProject,
    build_material_project,
    ensure_material_interface_nodes,
    material_interface_matches_build,
    material_program_contract,
    material_program_routes,
    material_semantics_complete,
    material_authoring_coverage,
    new_material_project,
    prune_orphaned_material_inputs,
    validate_material_project,
)
from .material_function import (
    MaterialFunctionError,
    discover_material_functions,
    expand_material_function_node,
    extract_material_function,
    load_material_function,
    material_function_node_properties,
    material_function_from_node,
    replace_material_function,
    FUNCTION_VALUE_TYPES,
)
from .material_channels import ChannelPanel, channel_nodes, reach_summary, visible_channel_pins
from .pass_purpose import PURPOSES, VISIBLE, pass_purpose, purpose_title
from .surface_data import MATERIAL_COORDINATES, SURFACE_DATA_KIND
from .material_variant import material_project_sidecar_path
from .material_usage import InputUsage, material_input_usage
from .profiles.base import same_mmtr_profile


def _default_for_type(value_type: str) -> tuple[float, ...]:
    return (0.0,) * value_width(value_type)


def material_function_root(project_dir: str) -> Path:
    return (Path(project_dir) if project_dir else Path.cwd()) / PROJECT_METADATA_FOLDER / "material_functions"


def import_material_function(parent, source_profile: str):
    filename, _ = QFileDialog.getOpenFileName(
        parent,
        QCoreApplication.translate("MmtrMaterialEditor", "Import material function"),
        "",
        QCoreApplication.translate(
            "MmtrMaterialEditor",
            "REasy material function (*.reasymaterialfunction.json);;JSON (*.json);;All files (*)",
        ),
    )
    if not filename:
        return None
    asset = load_material_function(filename)
    if asset.source_profile and not same_mmtr_profile(asset.source_profile, source_profile):
        raise MaterialFunctionError(f"{asset.name} targets {asset.source_profile}, not {source_profile}.")
    return asset


def extract_selected_function(parent, graph, selected, *, source_profile: str, contract):
    name, accepted = QInputDialog.getText(
        parent,
        QCoreApplication.translate("MmtrMaterialEditor", "Create material function"),
        QCoreApplication.translate("MmtrMaterialEditor", "Function name"),
        text=QCoreApplication.translate("MmtrMaterialEditor", "Material Function"),
    )
    if not accepted or not name.strip():
        return None
    staged = deepcopy(graph)
    asset, instance = extract_material_function(
        staged, selected, name=name.strip(), source_profile=source_profile, contract=contract,
    )
    return staged, asset, instance


def save_material_function(parent, asset, root: Path) -> bool:
    safe_name = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in asset.name
    ).strip("_") or "material_function"
    filename, _ = QFileDialog.getSaveFileName(
        parent,
        QCoreApplication.translate("MmtrMaterialEditor", "Save material function"),
        str(root / f"{safe_name}.reasymaterialfunction.json"),
        QCoreApplication.translate("MmtrMaterialEditor", "REasy material function (*.reasymaterialfunction.json)"),
    )
    if not filename:
        return False
    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    write_files_atomically({filename: asset.to_json().encode("utf-8")})
    return True


class _NewParameterDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Add material parameter"))
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit("NewParameter")
        self.value_type = QComboBox()
        self.value_type.addItem(self.tr("Scalar"), "float")
        self.value_type.addItem(self.tr("Vector 2"), "float2")
        self.value_type.addItem(self.tr("Color / Vector 3"), "float3")
        self.value_type.addItem(self.tr("Color / Vector 4"), "float4")
        self.group = QLineEdit(self.tr("Material"))
        form.addRow(self.tr("Name"), self.name)
        form.addRow(self.tr("Type"), self.value_type)
        form.addRow(self.tr("Group"), self.group)
        root.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)


_TEXTURE_TYPES = (
    (QT_TRANSLATE_NOOP("MmtrMaterialEditor", "Descriptor only"), None),
    (QT_TRANSLATE_NOOP("MmtrMaterialEditor", "1D Texture"), "texture1d"),
    (QT_TRANSLATE_NOOP("MmtrMaterialEditor", "1D Texture Array"), "texture1darray"),
    (QT_TRANSLATE_NOOP("MmtrMaterialEditor", "2D Texture"), "texture2d"),
    (QT_TRANSLATE_NOOP("MmtrMaterialEditor", "2D Texture Array"), "texture2darray"),
    (QT_TRANSLATE_NOOP("MmtrMaterialEditor", "Cube Texture"), "texturecube"),
    (QT_TRANSLATE_NOOP("MmtrMaterialEditor", "3D Texture"), "texture3d"),
)


class _NewTextureDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Add material texture"))
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit("NewTexture")
        self.dimension = QComboBox()
        for label, dimension in _TEXTURE_TYPES[1:]:
            self.dimension.addItem(QCoreApplication.translate("MmtrMaterialEditor", label), dimension)
        self.default_path = QLineEdit()
        self.default_path.setPlaceholderText(
            self.tr("Texture resource path for newly assigned MDF materials")
        )
        form.addRow(self.tr("Slot name"), self.name)
        form.addRow(self.tr("Texture type"), self.dimension)
        form.addRow(self.tr("Initial texture path"), self.default_path)
        root.addLayout(form)
        note = QLabel(
            self.tr(
                "Create Variant adds this slot to the new MMTR and selected MDFs.\n\nTexture Coordinates follow the "
                "mesh's UV unwrap, which suits textures painted for that mesh. For other textures, use Textures → "
                "Placement (planar or triplanar projection)."
            )
        )
        note.setWordWrap(True)
        root.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)


class MmtrMaterialEditor(QWidget):
    variant_requested = Signal(object, object)
    dirty_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source_path = ""
        self.project_dir = ""
        self._document = None
        self._engine_inputs = None
        self._surface_data = None
        self._contract: ShaderContract | None = None
        self._contract_error = ""
        self._coverage = None
        self._project: MmtrMaterialProject | None = None
        self._last_build = None
        self._interface_changed: bool | None = None
        self._selected_node_id = ""
        self._project_path = ""
        self._dirty_state = False
        self._implementation_ui_mode = False
        self._build_ui()
        self.setEnabled(False)

    @property
    def _dirty(self) -> bool:
        return self._dirty_state

    @_dirty.setter
    def _dirty(self, value: bool) -> None:
        normalized = bool(value)
        changed = normalized != self._dirty_state
        self._dirty_state = normalized
        if changed:
            self.dirty_changed.emit(normalized)

    @property
    def project(self) -> MmtrMaterialProject | None:
        return self._project

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        title = self.title = QLabel(self.tr("Material Editor"))
        font = QFont(title.font())
        font.setBold(True)
        font.setPointSize(max(14, font.pointSize() + 4))
        title.setFont(font)
        header.addWidget(title)
        header.addWidget(QLabel(self.tr("Name")))
        self.material_name = QLineEdit()
        self.material_name.setMinimumWidth(220)
        self.material_name.editingFinished.connect(self._material_name_changed)
        header.addWidget(self.material_name)
        self.profile_badge = QLabel()
        self.profile_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.profile_badge.setStyleSheet(
            "QLabel { background:#334b5e; color:#e8f3fa; border-radius:6px; padding:6px 10px; }"
        )
        header.addWidget(self.profile_badge)
        root.addLayout(header)

        self.provenance_note = QLabel()
        self.provenance_note.setWordWrap(True)
        self.provenance_note.setStyleSheet(
            "QLabel { background:#3f3523; color:#f1e6c8; border-radius:6px; padding:7px 10px; }"
        )
        self.provenance_note.hide()
        root.addWidget(self.provenance_note)

        toolbar = QHBoxLayout()
        def action(label, callback):
            value = QAction(label, self)
            value.triggered.connect(callback)
            return value

        self.open_action = action(self.tr("Open Graph…"), self._open_project)
        self.save_action = action(self.tr("Save Graph…"), self._save_project)
        self.add_parameter_action = action(self.tr("Parameter…"), self._add_parameter)
        self.add_texture_action = action(self.tr("Texture…"), self._add_texture)
        self.import_function_action = action(self.tr("Import Function…"), self._import_material_function)
        self.new_function_action = action(self.tr("New Function…"), self._new_material_function)
        self.insert_function_action = action(self.tr("Insert New Function on Wire…"), self._new_material_function)
        self.insert_function_action.setEnabled(False)
        self.extract_function_action = action(self.tr("Create Function from Selection…"), self._extract_material_function)
        self.implementations_action = action(self.tr("Native Shader Overrides…"), self._edit_implementations)
        self.remove_input_action = action(self.tr("Delete Input Definition…"), self._remove_selected_input)
        self.delete_node_action = action(self.tr("Delete Node"), self._delete_selected_nodes)
        self.edit_function_action = action(self.tr("Edit Function…"), lambda: self._edit_material_function(self._selected_node_id))
        self.extract_function_action.setEnabled(False)
        self.edit_function_action.setEnabled(False)
        self.delete_node_action.setEnabled(False)

        def menu_button(label, actions):
            button = QToolButton(self)
            button.setText(label)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(button)
            for item in actions:
                menu.addAction(item) if item is not None else menu.addSeparator()
            button.setMenu(menu)
            toolbar.addWidget(button)
            return button

        self.graph_menu = menu_button(self.tr("Graph"), (self.open_action, self.save_action))
        self.add_menu = menu_button(self.tr("Add"), (self.add_parameter_action, self.add_texture_action, None,
            self.new_function_action, self.import_function_action))
        menu_button(self.tr("Selection"), (self.edit_function_action, self.extract_function_action, self.insert_function_action,
            None, self.delete_node_action, self.remove_input_action))
        self.fit_button = QPushButton(self.tr("Fit Graph"))
        self.fit_button.clicked.connect(self.fit_graph)
        toolbar.addWidget(self.fit_button)
        self.arrange_button = QPushButton(self.tr("Arrange"))
        self.arrange_button.clicked.connect(self._arrange_graph)
        toolbar.addWidget(self.arrange_button)
        self.render_settings_button = QPushButton(self.tr("Render Settings…"))
        self.render_settings_button.setToolTip(self.tr(
            "Double-sided, transparent, shadows and every pass's blend, depth and culling state."))
        self.render_settings_button.clicked.connect(self._edit_render_settings)
        toolbar.addWidget(self.render_settings_button)
        toolbar.addStretch(1)
        self.advanced_menu = menu_button(self.tr("Advanced"), (self.implementations_action,))
        root.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        palette_page = QWidget()
        palette_layout = QVBoxLayout(palette_page)
        palette_layout.setContentsMargins(0, 0, 0, 0)
        self.palette_title = QLabel(self.tr("This Material"))
        palette_title_font = QFont(self.palette_title.font())
        palette_title_font.setBold(True)
        self.palette_title.setFont(palette_title_font)
        palette_layout.addWidget(self.palette_title)
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("Search inputs and nodes…"))
        self.search.textChanged.connect(self._filter_palette)
        palette_layout.addWidget(self.search)
        self.every_pass_inputs = QCheckBox(self.tr("Engine inputs for every pass"))
        self.every_pass_inputs.setToolTip(self.tr(
            "Also list engine inputs that reach no pass drawing the visible image, such as shadow-only data."
        ))
        self.every_pass_inputs.toggled.connect(lambda _checked: self._populate_palette())
        palette_layout.addWidget(self.every_pass_inputs)
        self.palette = QTreeWidget()
        self.palette.setHeaderHidden(True)
        self.palette.currentItemChanged.connect(self._palette_item_selected)
        self.palette.itemDoubleClicked.connect(
            lambda _item, _column: self._add_palette_node()
        )
        palette_layout.addWidget(self.palette, 1)
        self.add_selected_button = QPushButton(
            self.tr("Add Selected to Graph")
        )
        self.add_selected_button.setEnabled(False)
        self.add_selected_button.clicked.connect(self._add_palette_node)
        palette_layout.addWidget(self.add_selected_button)
        splitter.addWidget(palette_page)

        self.scene = _ShaderGraphScene(self)
        self.scene.pin_filter = lambda node, pins: (
            visible_channel_pins(self._project.graph, node, pins) if self._project is not None else pins)
        self.view = _ShaderGraphView()
        self.view.setScene(self.scene)
        self.scene.graph_changed.connect(self._graph_changed)
        self.scene.layout_changed.connect(self._layout_changed)
        self.scene.node_selected.connect(self._node_selected)
        self.scene.node_activated.connect(self._edit_material_function)
        self.scene.selectionChanged.connect(self._selection_actions_changed)
        self.scene.message.connect(self._show_message)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._graph_context_menu)
        splitter.addWidget(self.view)

        inspector = QWidget()
        inspector_layout = QVBoxLayout(inspector)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_title = QLabel(self.tr("Select a node to edit it."))
        selection_font = QFont(self.selection_title.font())
        selection_font.setBold(True)
        selection_font.setPointSize(max(11, selection_font.pointSize() + 1))
        self.selection_title.setFont(selection_font)
        self.selection_title.setWordWrap(True)
        inspector_layout.addWidget(self.selection_title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.properties_host = QWidget()
        self.properties = QFormLayout(self.properties_host)
        scroll.setWidget(self.properties_host)
        inspector_layout.addWidget(scroll, 1)
        splitter.addWidget(inspector)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([270, 900, 350])
        root.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.status = QLabel(self.tr("Open a master material to begin."))
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.variant_button = QPushButton(self.tr("Create Variant…"))
        self.variant_button.setMinimumWidth(170)
        self.variant_button.setEnabled(False)
        self.variant_button.setToolTip(
            self.tr(
                "Create a new MMTR and update selected MDF materials together."
            )
        )
        self.variant_button.clicked.connect(self._create_variant)
        footer.addWidget(self.variant_button)
        root.addLayout(footer)

    def _refresh_document_contract(self) -> None:
        document = self._document
        self._coverage = material_authoring_coverage(document, self._project)
        keys = material_program_routes(document, self._project)
        self._contract = None
        self._contract_error = ""
        if self._coverage.uncovered_keys:
            try:
                material_program_contract(document, *self._coverage.uncovered_keys[0])
            except ValueError as exc:
                self._contract_error = str(exc)
        if keys:
            target_index, program_index, role = keys[0]
            program = document.sdf.targets[target_index].programs[program_index]
            shader = program.shader(role)
            if shader is not None and shader.present:
                try:
                    authored = next((item for item in self._project.implementations if (target_index, program_index, role) in item.routes), None)
                    self._contract = authored.contract if authored is not None else material_program_contract(
                        document,
                        target_index,
                        program_index,
                        role,
                    )
                except ValueError as exc:
                    self._contract_error = str(exc)

    def _unavailable_interface_note(self) -> str:
        if self._contract_error:
            return self.tr(
                "Invalid shader interface: {error}. The MMTR can still be saved without graph edits."
            ).format(error=self._contract_error.rstrip("."))
        if self._coverage is not None and self._coverage.material_consumer_records:
            return self.tr(
                "Some shaders cannot be rebuilt. The MMTR can still be saved without graph edits."
            )
        return self.tr(
            "No editable material inputs were found. The MMTR can still be saved."
        )

    def set_document(self, document) -> None:
        self._document = document
        opening_project = self._project is None
        unreadable_sidecar = ""
        if opening_project:
            project_upgraded = False
            sidecar = Path(self._default_project_path())
            existing_sidecar = sidecar if self.source_path and sidecar.is_file() else None
            if existing_sidecar is not None:
                try:
                    self._project = MmtrMaterialProject.from_json(
                        existing_sidecar.read_text(encoding="utf-8")
                    )
                    self._project_path = str(existing_sidecar)
                except (OSError, UnicodeError, MaterialProjectError) as exc:
                    unreadable_sidecar = self.tr(
                        "The material project {path} could not be read: {error} A new one was started; saving the "
                        "MMTR replaces that file."
                    ).format(path=existing_sidecar.name, error=exc)
                    self._project = new_material_project(document, defer_shader_recovery=True)
            else:
                self._project = new_material_project(document, defer_shader_recovery=True)
        self._refresh_document_contract()
        if opening_project:
            if self._contract is not None and self._coverage.complete:
                project_upgraded = self._upgrade_loaded_project(self._project)
            self._dirty = project_upgraded
        self.setEnabled(self._contract is not None)
        self.profile_badge.setText(document.profile.display_name)
        if self._contract is None:
            self.provenance_note.setText(self._unavailable_interface_note())
            self.provenance_note.setStyleSheet(
                "QLabel { background:#263746; color:#dceaf5; border-radius:6px; padding:7px 10px; }"
            )
            self.provenance_note.show()
        elif self._project.source_material_model:
            self.provenance_note.clear()
            self.provenance_note.hide()
        else:
            self.provenance_note.setText(
                self.tr(
                    "Some original connections could not be recovered. Connecting a disconnected input creates new behavior."
                )
            )
            self.provenance_note.setStyleSheet(
                "QLabel { background:#3f3523; color:#f1e6c8; border-radius:6px; padding:7px 10px; }"
            )
            self.provenance_note.show()
        if unreadable_sidecar:
            self.provenance_note.setText(unreadable_sidecar)
            self.provenance_note.setStyleSheet(
                "QLabel { background:#4a2a2a; color:#f5dede; border-radius:6px; padding:7px 10px; }"
            )
            self.provenance_note.show()
        if self._contract is None:
            self.profile_badge.setToolTip(self._unavailable_interface_note())
        elif self._coverage.complete:
            self.profile_badge.setToolTip("")
        else:
            self.profile_badge.setToolTip(
                self.tr(
                    "The MMTR can be saved, but rebuilding its shader graph is not fully supported."
                )
            )
        structural_safe = self._can_edit_interface()
        self.add_parameter_action.setEnabled(structural_safe)
        self.add_texture_action.setEnabled(structural_safe)
        self.remove_input_action.setEnabled(False)
        structural_note = self.tr(
            "Inputs cannot be added or removed because some shaders cannot be rebuilt."
        )
        if self._coverage.complete and not structural_safe:
            structural_note = self.tr(
                "The MDF layout is not uniquely identified, or this material belongs to a shader-LOD family. "
                "Existing calculations can be edited, but inputs cannot be added or removed here."
            )
        self.add_parameter_action.setToolTip(
            "" if structural_safe else structural_note
        )
        self.add_texture_action.setToolTip(
            "" if structural_safe else structural_note
        )
        self.remove_input_action.setToolTip(
            self.tr("Select an MDF parameter or texture slot to delete.")
            if structural_safe
            else structural_note
        )
        self.material_name.setText(self._project.name)
        self._set_scene()
        self._populate_palette()
        self._populate_properties(None)
        if opening_project and not self._project_path:
            self._arrange_graph(mark_dirty=False)
        self.fit_graph()
        self._refresh_readiness()

    def _can_edit_interface(self) -> bool:
        if self._project is not None and self._project.domain == "implementation":
            return False
        if self._document is None or self._coverage is None or not self._coverage.complete:
            return False
        return self._document.profile.shader_graph_profile.material_interface_editing_supported(
            self._document, self._project)

    def _set_scene(self) -> None:
        if self._project is None or self._contract is None:
            self.scene.set_graph(None, None)
            return
        self.scene.set_graph(
            self._project.graph,
            self._contract,
            validator=lambda _graph, contract: validate_material_project(
                self._project,
                contract,
            ),
        )
        owns = self._project.domain == "implementation"
        previous = self._implementation_ui_mode
        self._implementation_ui_mode = owns
        self.view.setEnabled(not owns)
        for action in (self.import_function_action, self.new_function_action):
            action.setEnabled(not owns)
        if owns:
            for button in (self.add_parameter_action, self.add_texture_action, self.remove_input_action):
                button.setEnabled(False)
            self.provenance_note.setText(self.tr(
                "Edit this material through Advanced → Native Shader Overrides. "
                "Parameter and texture definitions cannot be changed in this mode."
            ))
            self.provenance_note.show()
        elif previous:
            self.add_parameter_action.setEnabled(self._can_edit_interface())
            self.add_texture_action.setEnabled(self._can_edit_interface())
            if self._project.source_material_model:
                self.provenance_note.hide()
        self._selection_actions_changed()

    def _edit_render_settings(self) -> None:
        if self._project is None or self._document is None:
            return
        from .render_settings import RenderSettingsDialog
        dialog = RenderSettingsDialog(self._document, self._project.pipeline_states, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.states == self._project.pipeline_states:
            return
        self._project.pipeline_states = dialog.states
        self._dirty = True
        self._refresh_readiness()

    def _arrange_graph(self, _checked=False, *, mark_dirty=True):
        if self._project is None or self._contract is None:
            return
        from file_handlers.sdf.shader_graph_layout import arrange_shader_graph
        try:
            arrange_shader_graph(self._project.graph, self._contract,
                width_provider=lambda node: self.scene.node_items[node.node_id].WIDTH)
        except ShaderGraphError as exc:
            self._show_message(str(exc))
            return
        self.scene.rebuild()
        self.fit_graph()
        if mark_dirty:
            self._layout_changed()

    def _selection_actions_changed(self):
        if self.scene._rebuilding:
            return
        if self._project is None or self._project.domain == "implementation":
            for action in (self.delete_node_action, self.extract_function_action, self.edit_function_action, self.insert_function_action):
                action.setEnabled(False)
            return
        nodes = [item.node for item in self.scene.selectedItems() if hasattr(item, "node")]
        editable = [node for node in nodes if node.kind != "material_surface"
                    and not node.properties.get("semantic_reconstruction_boundary")]
        self.delete_node_action.setEnabled(bool(editable) or any(hasattr(item, "link") for item in self.scene.selectedItems()))
        self.extract_function_action.setEnabled(bool(nodes) and len(editable) == len(nodes)
            and not any(node.kind in {"material_parameter", "material_texture", "function_input", "function_output"} for node in nodes))
        self.edit_function_action.setEnabled(len(nodes) == 1 and nodes[0].kind in {"material_function", "branch", "repeat"})
        wires = [item for item in self.scene.selectedItems() if hasattr(item, "link")]
        self.insert_function_action.setEnabled(len(wires) == 1 and wires[0].source.pin.value_type in FUNCTION_VALUE_TYPES)

    def _graph_context_menu(self, point):
        menu = QMenu(self)
        menu.addAction(self.edit_function_action)
        menu.addAction(self.extract_function_action)
        menu.addAction(self.insert_function_action)
        menu.addAction(self.delete_node_action)
        menu.addSeparator()
        add = menu.addAction(self.tr("Find a Node…"))
        add.triggered.connect(lambda: self.search.setFocus())
        menu.addAction(self.new_function_action)
        menu.addAction(self.import_function_action)
        menu.exec(self.view.viewport().mapToGlobal(point))

    def _material_name_changed(self) -> None:
        if self._project is None:
            return
        name = (self.material_name.text().strip() or "Material")[:256]
        self.material_name.setText(name)
        if name == self._project.name:
            return
        self._project.name = name
        self._dirty = True

    def _populate_palette(self) -> None:
        self.palette.clear()
        self.add_selected_button.setEnabled(False)
        if self._project is not None and self._project.domain == "implementation":
            for item in self._project.implementations:
                self.palette.addTopLevelItem(QTreeWidgetItem([self.tr("{name} · {count} route(s)").format(name=item.name, count=len(item.routes))]))
            return
        if self._project is None:
            return

        def root(label: str) -> QTreeWidgetItem:
            item = QTreeWidgetItem([label])
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            self.palette.addTopLevelItem(item)
            return item

        parameters = root(
            self.tr("Parameters ({count})").format(
                count=len(self._project.parameters)
            )
        )
        parameters.setToolTip(
            0,
            self.tr(
                "Material parameters. Their values are stored in the MDF."
            ),
        )
        for definition in sorted(
            self._project.parameters,
            key=lambda item: (item.group.casefold(), item.name.casefold()),
        ):
            display_name = _display_name(definition.name)
            item_text = (
                f"{display_name}  ({definition.name})"
                if display_name != definition.name
                else definition.name
            )
            item = QTreeWidgetItem([item_text])
            mdf_name, mmtr_name = self._parameter_mapping(definition)
            item.setToolTip(
                0,
                self.tr(
                    "MDF parameter: {mdf}\nMMTR input: {mmtr}\nThe value itself is stored in the MDF."
                ).format(mdf=mdf_name, mmtr=mmtr_name),
            )
            self._annotate_input_item(item, self._input_usage("parameter", definition))
            item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                (
                    "material_parameter",
                    {
                        "parameter_id": definition.parameter_id,
                        "name": definition.name,
                        "display_name": _display_name(definition.name),
                        "value_type": definition.value_type,
                    },
                ),
            )
            item.setData(0, Qt.ItemDataRole.UserRole + 1, ("parameter", definition.parameter_id))
            item.setData(
                0,
                Qt.ItemDataRole.UserRole + 2,
                f"{display_name} {mdf_name} {mmtr_name}",
            )
            parameters.addChild(item)

        textures = root(
            self.tr("Textures ({count})").format(
                count=len(self._project.textures)
            )
        )
        textures.setToolTip(
            0,
            self.tr(
                "Material texture slots. File paths are stored in the MDF."
            ),
        )
        for definition in sorted(self._project.textures, key=lambda item: item.name.casefold()):
            display_name = self._texture_display_name(definition)
            item_text = (
                f"{display_name}  ({definition.name})"
                if display_name.casefold() != definition.name.casefold()
                else definition.name
            )
            item = QTreeWidgetItem([item_text])
            item.setToolTip(
                0,
                self.tr(
                    "Graph node: {display}\nTexture slot: {name}\nChange the texture path in the MDF."
                ).format(display=display_name, name=definition.name),
            )
            self._annotate_input_item(item, self._input_usage("texture", definition))
            item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                (
                    "material_texture",
                    {
                        "texture_id": definition.texture_id,
                        "name": definition.name,
                        "display_name": display_name,
                        "dimension": definition.dimension,
                    },
                ),
            )
            item.setData(0, Qt.ItemDataRole.UserRole + 1, ("texture", definition.texture_id))
            item.setData(
                0,
                Qt.ItemDataRole.UserRole + 2,
                f"{display_name} {definition.name} {definition.source_name} {definition.role}",
            )
            textures.addChild(item)

        coordinates = QTreeWidgetItem([self.tr("Texture Coordinates")])
        tooltip = self.tr("The UV set the master's own textures are read with, in each pass.")
        surface_data = self._material_surface_data()
        if surface_data:
            offer = next((item for item in surface_data if item.key == MATERIAL_COORDINATES), None)
            tooltip += "\n" + (self._works_on(offer) if offer is not None else self.tr(
                "No pass of this master reads its textures with one UV set. Use a UV set from Surface Data."))
        coordinates.setToolTip(0, tooltip)
        coordinates.setData(0, Qt.ItemDataRole.UserRole, ("texture_coordinates", {}))
        textures.addChild(coordinates)
        sample = QTreeWidgetItem([self.tr("Sample Texture")])
        sample.setToolTip(
            0,
            self.tr(
                "Connect a texture and its Material Sampler output."
            ),
        )
        sample.setData(
            0,
            Qt.ItemDataRole.UserRole,
            ("sample_texture", {"dimension": "texture2d"}),
        )
        textures.addChild(sample)
        texel = QTreeWidgetItem([self.tr('Read Texel')])
        texel.setData(0, Qt.ItemDataRole.UserRole, ('texture_load', {'dimension': 'texture2d'}))
        textures.addChild(texel)
        placement = QTreeWidgetItem(textures, [self.tr("Placement")])
        placement.setToolTip(0, self.tr(
            "Coordinates that do not depend on the mesh's UVs, inserted as ordinary nodes you can change."))
        position, normal = self._placement_data()
        for label, payload, needs, tip in (
            (self.tr("Tiling and Offset"), {"kind": "tiling"}, True,
             self.tr("Coordinates × tiling + offset. Connect any coordinates, such as a UV set.")),
            *((self.tr("Planar Projection along {axis}").format(axis=axis.upper()), {"kind": "planar", "axis": axis},
               position is not None,
               self.tr("Coordinates from the position the pass projects, seen along {axis}.").format(
                   axis=axis.upper())) for axis in "xyz"),
            (self.tr("Triplanar Projection"), {"kind": "triplanar"}, position is not None and normal is not None,
             self.tr("One texture sampled along each axis and blended by the surface normal, so it covers any "
                     "shape without UVs. Connect the texture and sampler to the three samples.")),
        ):
            item = QTreeWidgetItem(placement, [label])
            item.setData(0, Qt.ItemDataRole.UserRole, ("placement", payload))
            item.setToolTip(0, tip if needs else self.tr(
                "No pass of this material hands its pixel shader the position and normal this needs."))
            item.setDisabled(not needs)

        try:
            function_assets = discover_material_functions((material_function_root(self.project_dir),))
        except MaterialFunctionError as exc:
            function_assets = ()
            self._show_message(str(exc))
        compatible_assets = tuple(
            asset
            for asset in function_assets
            if not asset.source_profile
            or same_mmtr_profile(asset.source_profile, self._project.source_profile)
        )
        if compatible_assets:
            user_functions = root(
                self.tr("Reusable Function Assets ({count})").format(
                    count=len(compatible_assets)
                )
            )
            user_functions.setToolTip(
                0,
                self.tr("Functions saved in this project's material function folder."),
            )
            categories: dict[str, QTreeWidgetItem] = {}
            for asset in compatible_assets:
                category_name = asset.category or self.tr("User Functions")
                category = categories.get(category_name)
                if category is None:
                    category = QTreeWidgetItem([category_name])
                    user_functions.addChild(category)
                    categories[category_name] = category
                item = QTreeWidgetItem([asset.name])
                item.setToolTip(0, asset.description)
                item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    (
                        "material_function",
                        material_function_node_properties(asset),
                    ),
                )
                item.setData(
                    0,
                    Qt.ItemDataRole.UserRole + 2,
                    f"{asset.name} {asset.category} {asset.description} {asset.function_id}",
                )
                category.addChild(item)

        from .profiles.renderer_inputs import RENDERER_RESOURCE_KIND, RENDERER_VALUE_KIND, renderer_fields
        from file_handlers.sdf.aggregate_values import AGGREGATE_KINDS
        renderer_values = {value["pin"]: value for node in self._project.graph.nodes
                           for value in node.properties.get("input_pins", []) if value.get("input_kind") == "renderer_value"}
        engine_resources = {}
        availability = {}
        engine_inputs = self._material_engine_inputs()
        visible_only = not self.every_pass_inputs.isChecked() and any(
            item.purposes.get(purpose, (0, 0))[1] for item in engine_inputs for purpose in VISIBLE)
        for engine_input in engine_inputs:
            if visible_only and not any(engine_input.purposes.get(purpose, (0, 0))[0] for purpose in VISIBLE):
                continue
            found = engine_resources if engine_input.pin["input_kind"] == "renderer_resource" else renderer_values
            found.setdefault(engine_input.pin["pin"], engine_input.pin)
            availability[engine_input.pin["pin"]] = engine_input
        offers = [offer for offer in self._material_surface_data()
                  if not visible_only or any(offer.purposes.get(purpose, (0, 0))[0] for purpose in VISIBLE)]
        if offers:
            surface_root = root(self.tr("Surface Data"))
            surface_root.setToolTip(0, self.tr(
                "Values each pass hands its pixel shader, such as UV sets, the normal and the position, named after "
                "what the vertex shader computes them from."
            ))
            derived = None
            for offer in offers:
                if offer.key.startswith("derived:") and derived is None:
                    derived = QTreeWidgetItem(surface_root, [self.tr("Other computed values")])
                    derived.setToolTip(0, self.tr("Values named by the vertex attributes and engine data they are "
                                                  "computed from."))
                item = QTreeWidgetItem([f"{offer.label} ({offer.value_type})"])
                item.setData(0, Qt.ItemDataRole.UserRole, (SURFACE_DATA_KIND, {
                    "datum": offer.key, "label": offer.label, "value_type": offer.value_type}))
                tooltip = self.tr("{label} ({type})\nRead where the pass hands it to the pixel shader:\n"
                                  "{works}\nUse Where Available or Drawing For for the others.").format(
                    label=offer.label, type=offer.value_type, works=self._works_on(offer))
                if offer.carried:
                    tooltip += "\n" + self.tr(
                        "In {count} of these passes only the vertex shader reads it; reading it adds it to what the "
                        "vertex shader hands the pixel shader.").format(count=sum(offer.carried.values()))
                item.setToolTip(0, tooltip)
                (derived if offer.key.startswith("derived:") else surface_root).addChild(item)
        if renderer_values or engine_resources:
            renderer_root = root(self.tr("Renderer Inputs"))
            renderer_root.setToolTip(0, self.tr(
                "Values and resources the engine provides to this material's shaders, such as time, camera data, "
                "depth, lighting and shadow textures."
            ))
            buffers = {}
            label_counts = Counter(value["label"] for value in renderer_values.values())
            for value in sorted(renderer_values.values(),
                                key=lambda item: (item["label"].casefold(), item["source_byte_offset"])):
                buffer_name = str(value["source_buffer"])
                if buffer_name not in buffers:
                    buffers[buffer_name] = QTreeWidgetItem([buffer_name])
                    renderer_root.addChild(buffers[buffer_name])
                name = str(value["source_name"])
                if label_counts[value["label"]] > 1:
                    name += f" @ {int(value['source_byte_offset'])}"
                group = QTreeWidgetItem([name])
                group.setData(0, Qt.ItemDataRole.UserRole + 2, value["label"])
                engine_input = availability.get(value["pin"])
                if engine_input is not None:
                    group.setToolTip(0, self.tr(
                        "{label} ({type})\nReadable where its shader binds {buffer} or the game binds {buffer} in "
                        "the pass:\n{works}"
                    ).format(label=value["label"], type=value["value_type"], buffer=buffer_name,
                             works=self._works_on(engine_input))
                        + "\n" + self._engine_input_materials_note(engine_input))
                buffers[buffer_name].addChild(group)
                item = QTreeWidgetItem([self.tr("Read value")])
                item.setData(0, Qt.ItemDataRole.UserRole, (RENDERER_VALUE_KIND, dict(value)))
                group.addChild(item)
                if "value_layout" in value:
                    for kind in sorted(AGGREGATE_KINDS):
                        item = QTreeWidgetItem([shader_graph_node_extension(kind).title])
                        item.setData(0, Qt.ItemDataRole.UserRole, (kind, {"value_layout": value["value_layout"], "field_index": 0}))
                        group.addChild(item)
            categories = {}
            for value in sorted(engine_resources.values(), key=lambda item: item["label"].casefold()):
                value_type = str(value["value_type"])
                category_name = (self.tr("Engine Samplers") if value_type in {"sampler", "comparison_sampler"}
                                 else self.tr("Engine Textures") if value_type.startswith("texture")
                                 else self.tr("Engine Buffers"))
                if category_name not in categories:
                    categories[category_name] = QTreeWidgetItem([category_name])
                    renderer_root.addChild(categories[category_name])
                group = QTreeWidgetItem([str(value["label"])])
                group.setData(0, Qt.ItemDataRole.UserRole + 2, value["label"])
                engine_input = availability[value["pin"]]
                group.setToolTip(0, self.tr(
                    "{label} ({type})\nUsable where its shader binds it or the game binds it in the pass:\n{works}\n"
                    "Use Where Available or Drawing For for the others."
                ).format(label=value["label"], type=value_type, works=self._works_on(engine_input))
                    + "\n" + self._engine_input_materials_note(engine_input))
                categories[category_name].addChild(group)
                item = QTreeWidgetItem([self.tr("Use resource")])
                item.setData(0, Qt.ItemDataRole.UserRole, (RENDERER_RESOURCE_KIND, dict(value)))
                group.addChild(item)
                stride = structured_buffer_stride(value_type)
                if stride is not None or value_type == "byte_address_buffer":
                    label = (QCoreApplication.translate("SdfShaderGraphEditor", "Read Record Word ({stride} byte records)")
                             .format(stride=stride) if stride is not None
                             else QCoreApplication.translate("SdfShaderGraphEditor", "Read Buffer Word"))
                    item = QTreeWidgetItem([label])
                    item.setData(0, Qt.ItemDataRole.UserRole,
                                 ("buffer_load", {"stride": stride, "byte_offset": 0} if stride is not None else {}))
                    group.addChild(item)
        values = root(self.tr("Values"))
        for label, value_type, default in (
            (self.tr("Scalar"), "float", [0.0]),
            (self.tr("Vector 2"), "float2", [0.0, 0.0]),
            (self.tr("Color"), "float3", [1.0, 1.0, 1.0]),
            (self.tr("Vector 4"), "float4", [0.0, 0.0, 0.0, 0.0]),
            (self.tr("Boolean"), "bool", [False]),
            (self.tr("Integer"), "int", [0]),
            (self.tr("Unsigned Integer"), "uint", [0]),
        ):
            item = QTreeWidgetItem([label])
            item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                ("constant", {"value_type": value_type, "values": default}),
            )
            values.addChild(item)

        for value_type in sorted({field.value_type for value in renderer_values.values() for field in renderer_fields(value)
                                  if not field.value_type.startswith("float")}):
            item = QTreeWidgetItem([value_type])
            item.setData(0, Qt.ItemDataRole.UserRole,
                         ("constant", {"value_type": value_type, "values": [0] * value_width(value_type)}))
            values.addChild(item)

        math_root = root(self.tr("Math"))
        from .material_branch import new_branch_properties
        control_flow = root(self.tr("Control Flow"))
        item = QTreeWidgetItem([self.tr("Branch")])
        item.setData(0, Qt.ItemDataRole.UserRole, ("branch", new_branch_properties()))
        from .material_loop import new_loop_properties
        repeat = QTreeWidgetItem(control_flow, [self.tr('Repeat')])
        repeat.setData(0, Qt.ItemDataRole.UserRole, ('repeat', new_loop_properties()))
        item.setToolTip(0, self.tr("Runs the selected branch. Double-click to edit the branches."))
        control_flow.addChild(item)
        from .pass_purpose import DRAWING_FOR_KIND
        drawing = QTreeWidgetItem(control_flow, [self.tr("Drawing For")])
        drawing.setData(0, Qt.ItemDataRole.UserRole, (DRAWING_FOR_KIND, {"purposes": ["shadow"]}))
        drawing.setToolTip(0, self.tr(
            "True in the passes drawing for the chosen purposes (shadows, visible colour, …), false in the rest."
        ))
        code = QTreeWidgetItem(control_flow, [self.tr("Code (HLSL)")])
        code.setData(0, Qt.ItemDataRole.UserRole, ("code", {
            "inputs": [{"name": "value", "type": "float3"}], "outputs": [{"name": "result", "type": "float3"}],
            "body": "result = value;"}))
        code.setToolTip(0, self.tr("A small HLSL function you write: typed inputs and outputs, and a body that "
                                   "assigns the outputs."))
        from .profiles.renderer_inputs import WHERE_AVAILABLE_KIND
        available = QTreeWidgetItem(control_flow, [self.tr("Where Available")])
        available.setData(0, Qt.ItemDataRole.UserRole, (WHERE_AVAILABLE_KIND, {"value_type": "float4"}))
        available.setToolTip(0, self.tr(
            "Uses Value in the rendering passes that provide every engine input and surface data it reads, and "
            "Otherwise in the rest."
        ))
        from .vertex_values import PER_VERTEX_KIND
        per_vertex = QTreeWidgetItem(control_flow, [self.tr("Computed per Vertex")])
        per_vertex.setData(0, Qt.ItemDataRole.UserRole, (PER_VERTEX_KIND, {"value_type": "float3"}))
        per_vertex.setToolTip(0, self.tr(
            "Computes Value once per vertex, in the vertex shader; the pixel shader reads it blended across each "
            "triangle. Value can read vertex attributes from Surface Data."
        ))
        for kind, label in (*BASIC_MATH_NODES, *EXTRA_MATH_NODES):
            properties = {"value_type": "float"}
            if kind == "swizzle":
                properties = {"source_type": "float4", "mask": "rgb"}
            item = QTreeWidgetItem([node_label(label)])
            item.setData(0, Qt.ItemDataRole.UserRole, (kind, properties))
            math_root.addChild(item)
        for category_name, templates in TYPED_MATH_CATEGORIES:
            typed_root = root(node_label(category_name))
            add_palette_templates(typed_root, templates)
        from file_handlers.sdf.shader_graph_execution import EXECUTION_TEMPLATES
        execution = root(self.tr('Execution'))
        add_palette_templates(execution, EXECUTION_TEMPLATES)
        from file_handlers.sdf.shader_graph_resource import RESOURCE_TEMPLATES
        resources = root(self.tr('Resource Addressing'))
        add_palette_templates(resources, RESOURCE_TEMPLATES)
        for index in range(self.palette.topLevelItemCount()):
            item = self.palette.topLevelItem(index)
            item.setExpanded(item in (parameters, textures))
        self._filter_palette(self.search.text())

    def _works_on(self, engine_input) -> str:
        lines = []
        for purpose in PURPOSES:
            available, total = engine_input.purposes.get(purpose, (0, 0))
            if not total:
                continue
            mark = "✓" if available == total else "✗" if not available else self.tr("{count} of {total}").format(
                count=available, total=total)
            lines.append(f"  {mark} {purpose_title(purpose)}")
        return "\n".join(lines)

    def _engine_input_materials_note(self, engine_input) -> str:
        return self.tr(
            "Bound by {count} of the game's {total} master materials. If few bind it, the engine may provide it only "
            "for their objects."
        ).format(count=engine_input.materials, total=engine_input.total_materials)

    def _profile_data(self, attribute: str, compute):
        if self._document is None or self._contract is None or not self._coverage.complete:
            return ()
        cached = getattr(self, attribute)
        if cached is None or cached[0] is not self._document:
            cached = (self._document, compute(self._document, self._document.profile.shader_graph_profile))
            setattr(self, attribute, cached)
        return cached[1]

    def _material_surface_data(self):
        from .surface_data import document_surface_data
        return self._profile_data("_surface_data", document_surface_data)

    def _material_engine_inputs(self):
        from .profiles.renderer_inputs import material_engine_inputs
        return self._profile_data("_engine_inputs", material_engine_inputs)

    def _filter_palette(self, text: str) -> None:
        filter_palette(self.palette, text, search_role=Qt.ItemDataRole.UserRole + 2, reveal_matching_groups=False,
                       hide_empty_groups=True)

    def _input_usage(self, kind, definition) -> InputUsage:
        usage = material_input_usage(self._document)
        name = str(definition.source_name or definition.name).casefold()
        if kind == "texture":
            return usage.texture(name) if definition.source_name else InputUsage.UNKNOWN
        parameter = next((item for item in self._document.material_interface.parameters
                          if item.name.casefold() == name and item.byte_offset == definition.source_byte_offset), None)
        return (usage.parameter(parameter.byte_offset, parameter.byte_size)
                if parameter is not None else InputUsage.UNKNOWN)

    def _annotate_input_item(self, item, usage: InputUsage) -> None:
        if usage is not InputUsage.USED:
            badge = {
                InputUsage.UNUSED: self.tr("Unused"),
                InputUsage.DESCRIPTOR: self.tr("Descriptor only"),
                InputUsage.UNKNOWN: self.tr("Usage unverified"),
            }[usage]
            item.setText(0, f"[{badge}] {item.text(0)}")
        item.setToolTip(0, item.toolTip(0) + "\n\n" + usage.label + "\n" + usage.explanation)

    def _add_usage_property(self, kind, definition) -> None:
        usage = self._input_usage(kind, definition)
        label = QLabel(usage.label)
        label.setWordWrap(True)
        label.setToolTip(usage.explanation)
        self.properties.addRow(self.tr("Loaded shaders"), label)

    def _pass_purposes(self) -> dict[str, int]:
        if self._document is None:
            return {}
        profile = self._document.profile.shader_graph_profile
        counts = Counter(pass_purpose(self._document, profile, target, program)
                         for target, item in enumerate(self._document.sdf.targets)
                         for program in range(len(item.programs)))
        return {purpose: counts[purpose] for purpose in PURPOSES if counts[purpose]}

    def _add_channel_panel(self) -> None:
        master, surface = channel_nodes(self._project.graph)
        if master is None:
            return
        panel = ChannelPanel(self._project.graph, master, surface,
                             lambda shown: self._set_node_property(master, "shown_channels", shown))
        panel.changed.connect(lambda: self._graph_changed(rebuild_scene=True))
        self.properties.addRow(panel)

    def _add_reach_property(self, kind, name) -> None:
        master, _surface = channel_nodes(self._project.graph)
        if master is None or "reach" not in master.properties:
            return
        label = QLabel(reach_summary(master, kind, str(name)))
        label.setWordWrap(True)
        label.setToolTip(self.tr("The outputs this input changes in the shipped shaders, and in how many passes."))
        self.properties.addRow(self.tr("Reaches"), label)

    def _parameter_mapping(
        self,
        definition: MaterialParameterDefinition,
    ) -> tuple[str, str]:
        mdf_name = str(definition.source_name or definition.name)
        profile = self._document.profile.shader_graph_profile
        return mdf_name, f"{profile.user_buffer_name}.{profile.shader_parameter_name(mdf_name)}"

    def _texture_display_name(
        self,
        definition: MaterialTextureDefinition,
    ) -> str:
        if self._project is not None:
            for node in self._project.graph.nodes:
                if (
                    node.kind == "material_texture"
                    and str(node.properties.get("texture_id", ""))
                    == definition.texture_id
                ):
                    resolved = str(node.properties.get("display_name", "")).strip()
                    if resolved:
                        return resolved
        return _display_name(definition.name)

    def _palette_item_selected(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        self.remove_input_action.setEnabled(False)
        self.add_selected_button.setText(self.tr("Add Selected to Graph"))
        self.add_selected_button.setEnabled(False)
        self.add_selected_button.setToolTip("")
        if item is None or self._project is None:
            return
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(payload, (tuple, list)) and len(payload) == 2:
            self.add_selected_button.setEnabled(True)
        identity = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if not isinstance(identity, (tuple, list)) or len(identity) != 2:
            return
        kind, key = str(identity[0]), str(identity[1])
        self.remove_input_action.setEnabled(
            kind in {"parameter", "texture"}
            and self._can_edit_interface()
        )
        if kind == "parameter":
            definition = self._project.parameter(key)
            if definition is None:
                return
            self._clear_properties()
            self.selection_title.setText(self.tr("MDF → MMTR parameter mapping"))
            note = QLabel(
                self.tr("The per-material value is stored in the MDF.")
            )
            note.setWordWrap(True)
            self.properties.addRow(note)
            mdf_name, mmtr_name = self._parameter_mapping(definition)
            self.properties.addRow(self.tr("MDF name"), QLabel(mdf_name))
            self.properties.addRow(self.tr("MMTR input"), QLabel(mmtr_name))
            type_name = {
                "float": self.tr("Number"),
                "float2": self.tr("Two numbers"),
                "float3": self.tr("Color or three numbers"),
                "float4": self.tr("Color or four numbers"),
            }.get(definition.value_type, _display_name(definition.value_type))
            self.properties.addRow(self.tr("Type"), QLabel(type_name))
            self._add_usage_property("parameter", definition)
            return
        if kind == "texture":
            definition = self._project.texture(key)
            if definition is None:
                return
            self._clear_properties()
            self.selection_title.setText(self.tr("MDF → MMTR texture mapping"))
            note = QLabel(
                self.tr("Each MDF stores the texture path for this MMTR slot.")
            )
            note.setWordWrap(True)
            self.properties.addRow(note)
            self.properties.addRow(
                self.tr("MDF / MMTR name"),
                QLabel(definition.name),
            )
            self._add_texture_type_property(definition)
            self._add_usage_property("texture", definition)
            default_path = QLineEdit(definition.default_path)
            default_path.setPlaceholderText(self.tr("Empty"))
            default_path.editingFinished.connect(
                lambda identity=definition.texture_id, editor=default_path: self._set_texture_default_path(
                    identity,
                    editor.text(),
                )
            )
            self.properties.addRow(
                self.tr("Default MDF path"),
                default_path,
            )

    def _add_palette_node(self) -> None:
        if self._project is None:
            return
        item = self.palette.currentItem()
        payload = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(payload, (tuple, list)) or len(payload) != 2:
            return
        center = self.view.mapToScene(self.view.viewport().rect().center())
        try:
            if payload[0] == "placement":
                node = self._insert_placement(dict(payload[1]), center.x(), center.y())
            else:
                node = self._project.graph.add_node(
                    str(payload[0]),
                    x=center.x(),
                    y=center.y(),
                    properties=dict(payload[1]),
                )
        except ShaderGraphError as exc:
            self._show_message(str(exc))
            return
        self._select_new_node(node)

    def _add_parameter(self) -> None:
        if self._project is None or not self._can_edit_interface():
            return
        dialog = _NewParameterDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        value_type = str(dialog.value_type.currentData())
        try:
            definition = MaterialParameterDefinition(
                parameter_id=uuid4().hex,
                name=dialog.name.text().strip(),
                value_type=value_type,
                default=_default_for_type(value_type),
                role="custom",
                group=dialog.group.text().strip() or "Material",
                minimum=0.0 if value_type == "float" else None,
                maximum=1.0 if value_type == "float" else None,
            )
            if any(item.name.casefold() == definition.name.casefold() for item in self._project.parameters):
                raise MaterialProjectError(f"A parameter named {definition.name!r} already exists.")
        except MaterialProjectError as exc:
            QMessageBox.warning(self, self.tr("Parameter not added"), str(exc))
            return
        self._add_input(self._project.parameters, definition, "material_parameter", "parameter_id")

    def _add_texture(self) -> None:
        if self._project is None or not self._can_edit_interface():
            return
        dialog = _NewTextureDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            definition = MaterialTextureDefinition(
                texture_id=uuid4().hex,
                name=dialog.name.text().strip(),
                role="custom",
                dimension=str(dialog.dimension.currentData()),
                default_path=dialog.default_path.text().strip(),
            )
            if any(
                item.name.casefold() == definition.name.casefold()
                for item in self._project.textures
            ):
                raise MaterialProjectError(
                    f"A texture slot named {definition.name!r} already exists."
                )
        except MaterialProjectError as exc:
            QMessageBox.warning(self, self.tr("Texture not added"), str(exc))
            return
        self._add_input(self._project.textures, definition, "material_texture", "texture_id")

    def _add_input(self, definitions: list, definition, kind: str, key: str) -> None:
        definitions.append(definition)
        ensure_material_interface_nodes(self._project)
        node = next(
            item
            for item in self._project.graph.nodes
            if item.kind == kind and str(item.properties.get(key, "")) == getattr(definition, key)
        )
        self._populate_palette()
        self._select_new_node(node)

    def _select_new_node(self, node: ShaderGraphNode) -> None:
        self.scene.rebuild(select_node=node.node_id)
        self._selected_node_id = node.node_id
        self._populate_properties(node)
        self._graph_changed()

    def _remove_selected_input(self) -> None:
        if self._project is None or not self._can_edit_interface():
            return
        item = self.palette.currentItem()
        payload = item.data(0, Qt.ItemDataRole.UserRole + 1) if item is not None else None
        if not isinstance(payload, (tuple, list)) or len(payload) != 2:
            self._show_message(
                self.tr("Select a parameter or texture in the left panel first.")
            )
            return
        kind, identity = str(payload[0]), str(payload[1])
        if kind not in {"parameter", "texture"}:
            return
        definition = (
            self._project.parameter(identity)
            if kind == "parameter"
            else self._project.texture(identity)
        )
        if definition is None:
            return
        identity_property = "parameter_id" if kind == "parameter" else "texture_id"
        references = [
            node
            for node in self._project.graph.nodes
            if str(node.properties.get(identity_property, "")) == identity
        ]
        if references:
            answer = QMessageBox.question(
                self,
                self.tr(
                    "Delete material parameter"
                    if kind == "parameter"
                    else "Delete material texture"
                ),
                self.tr(
                    "Delete this {kind} and its {count} node reference(s) from the material? "
                    "Create Variant will remove the corresponding MDF row."
                ).format(
                    kind=self.tr("parameter" if kind == "parameter" else "texture slot"),
                    count=len(references),
                ),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        for node in references:
            self._project.graph.remove_node(node.node_id)
        if kind == "parameter":
            self._project.parameters[:] = [
                item
                for item in self._project.parameters
                if item.parameter_id != identity
            ]
        else:
            self._project.textures[:] = [
                item
                for item in self._project.textures
                if item.texture_id != identity
            ]
        self._populate_palette()
        self.scene.rebuild()
        self._populate_properties(None)
        self._graph_changed()

    def _node_selected(self, node_id: str) -> None:
        self._selected_node_id = str(node_id)
        node = self._project.graph.node(node_id) if self._project is not None and node_id else None
        self._populate_properties(node)
        self._selection_actions_changed()

    def _clear_properties(self) -> None:
        # A control may be clearing the panel from its own signal, so its deletion must wait.
        while self.properties.rowCount():
            row = self.properties.takeRow(0)
            for item in (row.labelItem, row.fieldItem):
                widget = item.widget() if item is not None else None
                if widget is not None:
                    widget.hide()
                    widget.deleteLater()

    def _populate_properties(self, node: ShaderGraphNode | None) -> None:
        self._clear_properties()
        if node is None or self._project is None:
            self.selection_title.setText(self.tr("Select a node to edit it."))
            help_label = QLabel(
                self.tr(
                    "Open an MDF to change one material's values and texture paths."
                )
            )
            help_label.setWordWrap(True)
            self.properties.addRow(help_label)
            return
        self.selection_title.setText(node_title(node))
        from file_handlers.sdf.shader_graph_editor import _precision_properties
        _precision_properties(self.properties, node,
            lambda key, value: self._set_node_property(node, key, value))
        if node.properties.get("semantic_reconstruction_boundary"):
            self.selection_title.setText(self.tr("Imported Shading"))
            note = QLabel(self.tr(
                "Its inputs are the material's values and textures; its outputs are what the shader writes. "
                "To replace a whole pass, use Native Shader Overrides in Advanced."
            ))
            note.setWordWrap(True)
            self.properties.addRow(note)
            self._add_channel_panel()
            return
        if node.properties.get("authoring_origin") == "user_function":
            origin = QLabel(self.tr("User Function Asset"))
            origin.setStyleSheet("QLabel { color:#d6a8ff; font-weight:600; }")
            self.properties.addRow(self.tr("Source"), origin)
        if node.kind == "material_surface":
            note = QLabel(
                self.tr("Edit or reroute the material inputs.")
                if bool(node.properties.get("accepts_recovered_material", False))
                else self.tr("Connect material properties here.")
            )
            note.setWordWrap(True)
            self.properties.addRow(note)
            if node.properties.get("accepts_recovered_material"):
                self._add_channel_panel()
        elif node.kind == "material_parameter":
            definition = self._project.parameter(str(node.properties.get("parameter_id", "")))
            if definition is not None:
                self._add_usage_property("parameter", definition)
                self._add_reach_property("parameter", definition.source_name or definition.name)
                if bool(node.properties.get("interface_only", False)) and self._input_usage("parameter", definition) is not InputUsage.UNUSED:
                    note = QLabel(
                        self.tr(
                            "The original connection for this input is unknown. Connecting it creates new behavior."
                        )
                    )
                    note.setWordWrap(True)
                    self.properties.addRow(note)
                self.properties.addRow(self.tr("Parameter"), QLabel(_display_name(definition.name)))
                self.properties.addRow(self.tr("Group"), QLabel(definition.group))
                for index, value in enumerate(definition.default):
                    editor = QDoubleSpinBox()
                    editor.setDecimals(6)
                    editor.setRange(
                        definition.minimum if definition.minimum is not None else -1.0e9,
                        definition.maximum if definition.maximum is not None else 1.0e9,
                    )
                    editor.setValue(value)
                    editor.valueChanged.connect(
                        lambda changed, identity=definition.parameter_id, component=index: self._set_parameter_default(identity, component, changed)
                    )
                    label = (
                        self.tr("Default {component}").format(
                            component=("X", "Y", "Z", "W")[index]
                        )
                        if len(definition.default) > 1
                        else self.tr("Default value")
                    )
                    self.properties.addRow(label, editor)
        elif node.kind == "material_texture":
            definition = self._project.texture(str(node.properties.get("texture_id", "")))
            if definition is not None:
                self._add_usage_property("texture", definition)
                self._add_reach_property("texture", definition.source_name or definition.name)
                if bool(node.properties.get("interface_only", False)) and self._input_usage("texture", definition) is not InputUsage.UNUSED:
                    note = QLabel(
                        self.tr(
                            "The original connection for this texture is unknown."
                        )
                    )
                    note.setWordWrap(True)
                    self.properties.addRow(note)
                object_targets = []
                sampled_output_used = False
                for link in self._project.graph.links:
                    if link.source_node != node.node_id:
                        continue
                    if link.source_pin == "texture_object":
                        target = self._project.graph.node(link.target_node)
                        if target is not None:
                            object_targets.append(node_title(target))
                    elif link.source_pin in {"rgba", "rgb", "r", "g", "b", "a"}:
                        sampled_output_used = True
                if object_targets and not sampled_output_used and definition.dimension is not None:
                    note = QLabel(
                        self.tr(
                            "{consumer} receives the texture object. The Coordinates input on this node is unused."
                        ).format(consumer=" / ".join(dict.fromkeys(object_targets)))
                    )
                    note.setWordWrap(True)
                    self.properties.addRow(note)
                display_name = self._texture_display_name(definition)
                self.properties.addRow(
                    self.tr("Texture"),
                    QLabel(display_name),
                )
                if display_name.casefold() != definition.name.casefold():
                    self.properties.addRow(
                        self.tr("MDF / MMTR slot"),
                        QLabel(definition.name),
                    )
                self._add_texture_type_property(definition)
                default_path = QLineEdit(definition.default_path)
                default_path.setPlaceholderText(
                    self.tr("Initial path used when this slot is added to an MDF")
                )
                default_path.editingFinished.connect(
                    lambda identity=definition.texture_id, editor=default_path: self._set_texture_default_path(
                        identity,
                        editor.text(),
                    )
                )
                self.properties.addRow(self.tr("Initial MDF path"), default_path)
        elif node.kind in {"branch", "repeat"}:
            note = QLabel(self.tr("Only the selected arm executes. Both arms share named inputs and results.") if node.kind == 'branch'
                          else self.tr('Edit the repeated step, its continuation condition, and the values carried between iterations.'))
            note.setWordWrap(True)
            self.properties.addRow(note)
            edit = QPushButton(self.tr("Edit Branch…" if node.kind == 'branch' else 'Edit Repeat…'))
            edit.clicked.connect(lambda: self._edit_material_function(node.node_id))
            self.properties.addRow(edit)
        elif node.kind == "material_function":
            live = self._project.graph.upstream_nodes(n.node_id for n in self._project.graph.nodes
                if n.kind in {"material_surface", "function_output"})
            if node.node_id not in live:
                note = QLabel(self.tr("Not connected to an output. This function does not affect the material yet."))
                note.setWordWrap(True)
                self.properties.addRow(note)
            function = node.properties.get("function", {})
            if isinstance(function, dict):
                description = str(function.get("description", "")).strip()
                if description:
                    note = QLabel(description)
                    note.setWordWrap(True)
                    self.properties.addRow(note)
                source_profile = str(function.get("source_profile", "")).strip()
                self.properties.addRow(
                    self.tr("Compatibility"),
                    QLabel(source_profile or self.tr("Portable")),
                )
            edit = QPushButton(self.tr("Edit Function…"))
            edit.clicked.connect(lambda: self._edit_material_function(node.node_id))
            self.properties.addRow(edit)
            export = QPushButton(self.tr("Save Function As…"))
            export.clicked.connect(lambda: self._export_material_function(node.node_id))
            self.properties.addRow(export)
            expand = QPushButton(self.tr("Expand to Editable Nodes"))
            expand.setToolTip(
                self.tr(
                    "Replace this function call with its nodes."
                )
            )
            expand.clicked.connect(
                lambda _checked=False, identity=node.node_id: self._expand_material_function(
                    identity
                )
            )
            self.properties.addRow(expand)
        elif node.kind == "input":
            element = QSpinBox()
            element.setRange(-1, 31)
            element.setSpecialValueText(self.tr("No primitive axis"))
            value = node.properties.get("element_index")
            element.setValue(-1 if value is None else value)
            element.setToolTip(self.tr("Vertex or control-point index for geometry and tessellation inputs."))
            element.valueChanged.connect(lambda value, n=node: self._set_node_property(n, "element_index", value if value >= 0 else None))
            self.properties.addRow(self.tr("Primitive element"), element)
        elif node.kind in {'sample_texture', 'texture_load'}:
            _texture_properties(self.properties, node, lambda key, value: self._set_node_property(node, key, value))
            if node.kind == "sample_texture":
                self._add_engine_sampler_choice(node)
        elif node.kind == "constant":
            value_type = str(node.properties.get("value_type", "float"))
            values = list(node.properties.get("values", _default_for_type(value_type)))
            for index in range(value_width(value_type)):
                if value_type.rstrip('234').endswith('64_t'):
                    from file_handlers.sdf.shader_graph_editor import _wide_integer_control
                    editor = _wide_integer_control(value_type, values[index] if index < len(values) else 0,
                        lambda value, i=index: self._set_constant(node,i,value))
                    self.properties.addRow('XYZW'[index],editor)
                    continue
                if value_type.startswith("bool"):
                    editor = QCheckBox()
                    editor.setChecked(bool(values[index]) if index < len(values) else False)
                    editor.toggled.connect(lambda changed, n=node, component=index: self._set_constant(n, component, changed))
                    self.properties.addRow("Value" if len(values) == 1 else "XYZW"[index], editor)
                    continue
                editor = QDoubleSpinBox()
                floating = is_float_type(value_type)
                editor.setDecimals(6 if floating else 0)
                editor.setRange(*((-1.0e9, 1.0e9) if floating else (0, 0xffffffff)
                                  if value_type.rstrip('234') in {'uint', 'min16uint'} else (-0x80000000, 0x7fffffff)))
                editor.setValue(float(values[index]) if index < len(values) else 0.0)
                editor.valueChanged.connect(
                    lambda changed, n=node, component=index: self._set_constant(n, component, changed)
                )
                self.properties.addRow(("X", "Y", "Z", "W")[index], editor)
        elif node.kind in {'execution_begin', 'capture_value', 'discard_pixel', 'memory_load', 'memory_store', 'memory_atomic', 'memory_barrier', 'image_load', 'image_store', 'image_atomic'}:
            from file_handlers.sdf.shader_graph_editor import _execution_properties
            _execution_properties(self.properties, node, lambda key, value: self._set_node_property(node, key, value))
        elif node.kind == 'constant_table':
            _table_properties(self.properties, node, lambda key, value: self._set_node_property(node, key, value))
        elif node.kind in {'resource_index', 'descriptor_heap', 'resource_dimensions'}:
            from file_handlers.sdf.shader_graph_editor import _resource_properties
            _resource_properties(self.properties, node, lambda key, value: self._set_node_property(node, key, value))
        elif node.kind in {'buffer_load','constant_buffer_load'}:
            from file_handlers.sdf.shader_graph_editor import _buffer_properties
            _buffer_properties(self.properties, node, lambda key, value: self._set_node_property(node, key, value))
        elif node.kind == "swizzle":
            value_type = QComboBox()
            for kind in NUMERIC_VALUE_TYPES:
                value_type.addItem(numeric_type_label(kind), kind)
            value_type.setCurrentIndex(value_type.findData(node.properties.get('source_type', 'float4')))
            value_type.currentIndexChanged.connect(lambda: self._set_node_property(node, 'source_type', value_type.currentData()))
            self.properties.addRow(self.tr('Input type'), value_type)
            mask = QLineEdit(str(node.properties.get("mask", "rgb")))
            mask.setMaxLength(4)
            mask.editingFinished.connect(
                lambda n=node, control=mask: self._set_node_property(n, "mask", control.text().casefold())
            )
            self.properties.addRow(self.tr("Components"), mask)
        elif node.kind == "compare":
            _comparison_properties(self.properties, node, lambda key, value: self._set_node_property(node, key, value))
        elif (node.kind in BASIC_MATH_KINDS - {"normal_unpack", "swizzle"} or node.kind in UNARY_FUNCTIONS
              or node.kind in TYPED_MATH_NODES
              or node.kind in {"multiply_add", "select", "indexed_select", "array_read", "unassigned_value"}):
            _numeric_properties(self.properties, node, lambda key, value: self._set_node_property(node, key, value))
        elif node.kind in {"aggregate_extract", "aggregate_replace"}:
            from file_handlers.sdf.aggregate_values import aggregate_fields
            field = QComboBox()
            for index, item in enumerate(aggregate_fields(node.properties)):
                field.addItem(f"{item.path or 'Value'} ({item.value_type})", index)
            field.setCurrentIndex(int(node.properties.get("field_index", 0)))
            field.currentIndexChanged.connect(lambda index, n=node: self._set_node_property(n, "field_index", index))
            self.properties.addRow(self.tr("Field"), field)
        elif node.kind == "texture_coordinates":
            note = QLabel(self.tr("In each pass, the UV set the master's own textures are read with. A pass that reads "
                                  "them with several UV sets has none; use a UV set from Surface Data there."))
            note.setWordWrap(True)
            self.properties.addRow(note)
        elif node.kind == "re_engine_drawing_for":
            description = QLabel(self.tr(
                "True in the passes drawing for the checked purposes, false in the rest. Each pass sees a constant, "
                "so only the matching side of a Select runs."
            ))
            description.setWordWrap(True)
            self.properties.addRow(description)
            chosen = set(node.properties.get("purposes", ()))
            for purpose, count in self._pass_purposes().items():
                box = QCheckBox(self.tr("{purpose} · {count} passes").format(purpose=purpose_title(purpose), count=count))
                box.setChecked(purpose in chosen)
                box.toggled.connect(lambda checked, n=node, item=purpose: self._set_node_property(
                    n, "purposes", sorted(({*n.properties.get("purposes", ())} | {item}) if checked
                                          else {*n.properties.get("purposes", ())} - {item})))
                self.properties.addRow(box)
        elif node.kind == "code":
            _code_properties(self.properties, node, lambda inputs, outputs, body, n=node: self._set_code(
                n, inputs, outputs, body))
        elif node.kind == SURFACE_DATA_KIND:
            offers = [offer for offer in self._material_surface_data()
                      if offer.value_type == node.properties.get("value_type")]
            control = QComboBox()
            for offer in offers:
                control.addItem(offer.label, offer.key)
            control.setCurrentIndex(control.findData(node.properties.get("datum")))
            control.currentIndexChanged.connect(lambda index, n=node, items=offers: self._set_surface_datum(
                n, items[index]))
            self.properties.addRow(self.tr("Value"), control)
            offer = next((item for item in offers if item.key == node.properties.get("datum")), None)
            if offer is not None:
                works = QLabel(self.tr("Read where the pass hands it to the pixel shader:\n{works}").format(
                    works=self._works_on(offer)))
                works.setWordWrap(True)
                self.properties.addRow(works)
        elif node.kind == "re_engine_where_available":
            control = QComboBox()
            for value_type in NUMERIC_VALUE_TYPES:
                control.addItem(QCoreApplication.translate("SdfShaderGraphEditor", numeric_type_label(value_type)), value_type)
            control.setCurrentIndex(control.findData(node.properties.get("value_type", "float4")))
            control.currentIndexChanged.connect(
                lambda _index, n=node, c=control: self._set_node_property(n, "value_type", c.currentData()))
            self.properties.addRow(QCoreApplication.translate("SdfShaderGraphEditor", "Value type"), control)
            description = QLabel(self.tr(
                "Uses Value in the rendering passes that provide every engine input and surface data it reads, and "
                "Otherwise in the rest."
            ))
            description.setWordWrap(True)
            self.properties.addRow(description)
        else:
            extension = shader_graph_node_extension(node.kind)
            if extension is not None and extension.description:
                description = QLabel(node_label(extension.description))
                description.setWordWrap(True)
                self.properties.addRow(description)
        if node.kind != "material_surface" and not bool(
            node.properties.get("semantic_reconstruction_boundary", False)
        ):
            delete = QPushButton(self.tr("Delete Node"))
            delete.clicked.connect(lambda _checked=False, identity=node.node_id: self._delete_node(identity))
            self.properties.addRow(delete)

    def _set_parameter_default(self, identity: str, component: int, value: float) -> None:
        if self._project is None:
            return
        for index, definition in enumerate(self._project.parameters):
            if definition.parameter_id != identity:
                continue
            values = list(definition.default)
            values[component] = float(value)
            self._project.parameters[index] = replace(definition, default=tuple(values))
            self._dirty = True
            self._refresh_readiness()
            return

    def _add_texture_type_property(self, definition) -> None:
        editor = QComboBox()
        for label, dimension in _TEXTURE_TYPES:
            editor.addItem(QCoreApplication.translate("MmtrMaterialEditor", label), dimension)
        editor.setCurrentIndex(editor.findData(definition.dimension))
        editor.setEnabled(self._can_edit_interface())
        editor.setToolTip(self.tr(
            "Descriptor only: the texture without sampling pins. Choose an image type to sample it; it must match "
            "the assigned textures."
        ))
        editor.currentIndexChanged.connect(
            lambda _index, identity=definition.texture_id, control=editor:
                self._set_texture_dimension(identity, control.currentData(), control)
        )
        self.properties.addRow(self.tr("Texture type"), editor)

    def _set_texture_dimension(self, identity, dimension, editor=None) -> None:
        if self._project is None or not self._can_edit_interface():
            return
        definition = self._project.texture(identity)
        if definition is None or definition.dimension == dimension:
            return
        nodes = {n.node_id: n for n in self._project.graph.nodes
                 if n.kind == "material_texture" and n.properties.get("texture_id") == identity}
        replacements = {key: replace(n, properties={**n.properties, "dimension": dimension}) for key, n in nodes.items()}
        for link in self._project.graph.links:
            if link.source_node not in nodes and link.target_node not in nodes:
                continue
            source = replacements.get(link.source_node) or self._project.graph.node(link.source_node)
            target = replacements.get(link.target_node) or self._project.graph.node(link.target_node)
            output = next((p for p in node_pins(source, self._contract) if p.direction == "output" and p.name == link.source_pin), None)
            input_pin = next((p for p in node_pins(target, self._contract) if p.direction == "input" and p.name == link.target_pin), None)
            if output is None or input_pin is None or not connection_compatible(output.value_type, input_pin.value_type):
                self._show_message(self.tr("Disconnect incompatible consumers before changing this texture's type."))
                if editor is not None:
                    editor.blockSignals(True)
                    editor.setCurrentIndex(editor.findData(definition.dimension))
                    editor.blockSignals(False)
                return
        index = self._project.textures.index(definition)
        self._project.textures[index] = replace(definition, dimension=dimension)
        for node in nodes.values():
            node.properties["dimension"] = dimension
        self._populate_palette()
        self._graph_changed(rebuild_scene=True)

    def _set_texture_default_path(self, identity: str, value: str) -> None:
        if self._project is None:
            return
        for index, definition in enumerate(self._project.textures):
            if definition.texture_id != identity:
                continue
            normalized = str(value).strip()
            if normalized == definition.default_path:
                return
            try:
                self._project.textures[index] = replace(
                    definition,
                    default_path=normalized,
                )
            except MaterialProjectError as exc:
                self._show_message(str(exc))
                return
            self._graph_changed(rebuild_scene=False)
            return

    def _set_constant(self, node: ShaderGraphNode, component: int, value: float) -> None:
        values = list(node.properties.get("values", []))
        while len(values) <= component:
            values.append(0.0)
        value_type = str(node.properties.get("value_type", "float"))
        values[component] = float(value) if is_float_type(value_type) else bool(value) if value_type.startswith("bool") else int(value)
        node.properties["values"] = values
        self._graph_changed(rebuild_scene=False)

    def _placement_data(self):
        offers = self._material_surface_data()
        position = next((item for item in offers if item.key == "position" and item.value_type == "float3"), None)
        normal = next((item for item in offers if item.key.startswith("space:") and item.value_type == "float3"), None)
        return position, normal

    def _insert_placement(self, payload, x: float, y: float):
        from .material_projection import insert_planar, insert_tiling_offset, insert_triplanar
        graph = self._project.graph
        position, normal = self._placement_data()
        if payload["kind"] == "tiling":
            return insert_tiling_offset(graph, x, y)
        if position is None or (payload["kind"] == "triplanar" and normal is None):
            raise ShaderGraphError(self.tr("No pass of this material hands its pixel shader the data this needs."))
        if payload["kind"] == "planar":
            return insert_planar(graph, position, payload["axis"], x, y)
        return insert_triplanar(graph, position, normal, x, y)

    def _add_engine_sampler_choice(self, node: ShaderGraphNode) -> None:
        from .profiles.renderer_inputs import RENDERER_RESOURCE_KIND
        graph = self._project.graph
        links = {(link.target_node, link.target_pin): link for link in graph.links}
        texture_link = links.get((node.node_id, "texture"))
        texture_node = graph.node(texture_link.source_node) if texture_link is not None else None
        if texture_node is None or texture_node.kind != RENDERER_RESOURCE_KIND:
            return
        inputs = {item.pin["pin"]: item for item in self._material_engine_inputs()}
        texture = inputs.get(texture_node.properties.get("pin"))
        if texture is None:
            return
        samplers = sorted((item for item in inputs.values() if item.pin["value_type"] in {"sampler", "comparison_sampler"}
                           and texture.routes <= item.routes), key=lambda item: str(item.pin["label"]).casefold())
        if not samplers:
            note = QLabel(self.tr("No engine sampler can be read in every pass that can read {texture}.").format(
                texture=texture.pin["label"]))
            note.setWordWrap(True)
            self.properties.addRow(note)
            return
        sampler_link = links.get((node.node_id, "sampler"))
        current = graph.node(sampler_link.source_node) if sampler_link is not None else None
        control = QComboBox()
        control.setToolTip(self.tr("Engine samplers that every pass able to read {texture} can read too.").format(
            texture=texture.pin["label"]))
        if current is None:
            control.addItem(self.tr("Choose a sampler…"), None)
        for item in samplers:
            control.addItem(str(item.pin["label"]), item.pin["pin"])
        if current is not None:
            control.setCurrentIndex(control.findData(current.properties.get("pin")))
        control.currentIndexChanged.connect(lambda _index, n=node, c=control, items=samplers: self._use_engine_sampler(
            n, next((item for item in items if item.pin["pin"] == c.currentData()), None)))
        self.properties.addRow(self.tr("Engine sampler"), control)

    def _use_engine_sampler(self, node: ShaderGraphNode, sampler) -> None:
        from .profiles.renderer_inputs import RENDERER_RESOURCE_KIND
        if sampler is None:
            return
        graph = self._project.graph
        existing = next((item for item in graph.nodes if item.kind == RENDERER_RESOURCE_KIND
                         and item.properties.get("pin") == sampler.pin["pin"]), None)
        try:
            source = existing or graph.add_node(RENDERER_RESOURCE_KIND, x=node.x - 260, y=node.y + 120,
                                                properties=dict(sampler.pin))
            graph.disconnect_target(node.node_id, "sampler")
            graph.connect(source.node_id, "value", node.node_id, "sampler")
        except ShaderGraphError as exc:
            self._show_message(str(exc))
            return
        self.scene.rebuild(select_node=node.node_id)
        self._graph_changed(rebuild_scene=False)

    def _set_code(self, node: ShaderGraphNode, inputs: str, outputs: str, body: str) -> None:
        from file_handlers.sdf.shader_graph_code import updated_code
        try:
            node.properties = updated_code(self._project.graph, node, inputs, outputs, body)
        except ShaderGraphError as exc:
            self._show_message(str(exc))
            return
        self.scene.rebuild(select_node=node.node_id)
        self._graph_changed(rebuild_scene=False)

    def _set_surface_datum(self, node: ShaderGraphNode, offer) -> None:
        # The label only names the node's pin.
        node.properties = {**node.properties, "label": offer.label}
        self._set_node_property(node, "datum", offer.key)

    def _set_node_property(self, node: ShaderGraphNode, key: str, value) -> None:
        if node.properties.get(key) == value:
            return
        properties = {**node.properties, key: value}
        if node.kind in {'sample_texture','texture_load'}:
            from file_handlers.sdf.shader_graph_texture import texture_property_update
            properties = texture_property_update(node.properties,key,value)
        if self._project is not None:
            try:
                validate_node_update(self._project.graph, node, properties, self._contract)
            except ShaderGraphError as exc:
                self._populate_properties(node)
                self._show_message(str(exc))
                return
        node.properties = properties
        self.scene.rebuild(select_node=node.node_id)
        self._graph_changed(rebuild_scene=False)

    def _delete_node(self, node_id: str) -> None:
        if self._project is None:
            return
        try:
            self._project.graph.remove_node(node_id)
        except ShaderGraphError as exc:
            self._show_message(str(exc))
            return
        self.scene.rebuild()
        self._populate_properties(None)
        self._graph_changed(rebuild_scene=False)

    def _delete_selected_nodes(self) -> None:
        self.scene.delete_selected()

    def _expand_material_function(self, node_id: str) -> None:
        if self._project is None:
            return
        try:
            created = expand_material_function_node(
                self._project.graph,
                node_id,
            )
        except MaterialFunctionError as exc:
            self._show_message(str(exc))
            return
        self._selected_node_id = created[0] if created else ""
        self.scene.rebuild(select_node=self._selected_node_id)
        selected = self._project.graph.node(self._selected_node_id)
        self._populate_properties(selected)
        self._graph_changed()
        self._show_message(
            self.tr("Function expanded. Its nodes and connections are now directly editable.")
        )

    def _new_material_function(self):
        if self._project is None:
            return
        from .material_function_editor import MaterialFunctionDialog, new_material_function
        links = [item.link for item in self.scene.selectedItems() if hasattr(item, "link")]
        link = links[0] if len(links) == 1 else None
        value_type = next((pin.value_type for pin in node_pins(self._project.graph.node(link.source_node), self._contract)
                           if pin.direction == "output" and pin.name == link.source_pin), "float") if link else "float"
        if value_type not in FUNCTION_VALUE_TYPES:
            self._show_message(self.tr("This wire carries an entire shading template, not a function value. Select one of its inputs instead."))
            return
        initial = new_material_function(value_type=value_type)
        center = self.view.mapToScene(self.view.viewport().rect().center())

        def staged_function(asset):
            graph = deepcopy(self._project.graph)
            node = graph.add_node("material_function", x=center.x(), y=center.y(),
                properties=material_function_node_properties(initial))
            if link is not None:
                graph.connect(link.source_node, link.source_pin, node.node_id, "input")
                graph.connect(node.node_id, "result", link.target_node, link.target_pin)
            replace_material_function(graph, node.node_id, asset)
            return graph, node

        dialog = MaterialFunctionDialog(initial, self, validate_replacement=lambda asset: staged_function(asset))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._project.graph, node = staged_function(dialog.result_asset)
        self._set_scene()
        self.scene.rebuild(select_node=node.node_id)
        self._graph_changed()

    def _edit_material_function(self, node_id):
        if self._project is None:
            return
        node = self._project.graph.node(node_id)
        if node is not None and node.kind in {"branch", "repeat"}:
            from .material_function_editor import MaterialBranchDialog
            from .material_loop_editor import MaterialLoopDialog
            dialog_type = MaterialBranchDialog if node.kind == 'branch' else MaterialLoopDialog
            dialog = dialog_type(node, self, validate_replacement=lambda properties:
                validate_node_update(self._project.graph, node, properties, self._contract))
            if dialog.exec() == QDialog.DialogCode.Accepted:
                node.properties = dialog.result_properties
                self.scene.rebuild(select_node=node_id)
                self._populate_properties(node)
                self._graph_changed()
            return
        if node is None or node.kind != "material_function":
            if node is not None:
                self._populate_properties(node)
            return
        from .material_function_editor import MaterialFunctionDialog
        try:
            asset = material_function_from_node(node)
            dialog = MaterialFunctionDialog(asset, self, validate_replacement=lambda edited:
                replace_material_function(deepcopy(self._project.graph), node_id, edited))
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            replace_material_function(self._project.graph, node_id, dialog.result_asset)
        except MaterialFunctionError as exc:
            self._show_message(str(exc))
            return
        self.scene.rebuild(select_node=node_id)
        self._populate_properties(self._project.graph.node(node_id))
        self._graph_changed()

    def _export_material_function(self, node_id):
        node = self._project.graph.node(node_id) if self._project is not None else None
        if node is None or node.kind != "material_function":
            return
        try:
            if save_material_function(self, material_function_from_node(node), material_function_root(self.project_dir)):
                self._populate_palette()
        except (OSError, MaterialFunctionError) as exc:
            self._show_message(str(exc))

    def _import_material_function(self) -> None:
        if self._project is None:
            return
        try:
            asset = import_material_function(self, self._project.source_profile)
            if asset is None:
                return
            center = self.view.mapToScene(self.view.viewport().rect().center())
            node = self._project.graph.add_node(
                "material_function",
                x=center.x(),
                y=center.y(),
                properties=material_function_node_properties(asset),
            )
        except ShaderGraphError as exc:
            QMessageBox.critical(self, self.tr("Function import failed"), str(exc))
            return
        self._select_new_node(node)

    def _extract_material_function(self) -> None:
        if self._project is None:
            return
        selected_ids = tuple(
            node.node_id
            for item in self.scene.selectedItems()
            for node in (getattr(item, "node", None),)
            if isinstance(node, ShaderGraphNode)
        )
        if not selected_ids:
            self._show_message(self.tr("Select calculation nodes to turn into a function."))
            return
        try:
            extracted = extract_selected_function(
                self, self._project.graph, selected_ids,
                source_profile=self._project.source_profile, contract=self._contract,
            )
        except MaterialFunctionError as exc:
            QMessageBox.warning(self, self.tr("Function not created"), str(exc))
            return
        if extracted is None:
            return
        graph, asset, instance = extracted
        try:
            if not save_material_function(self, asset, material_function_root(self.project_dir)):
                return
        except (OSError, MaterialFunctionError) as exc:
            QMessageBox.critical(self, self.tr("Function save failed"), str(exc))
            return
        self._project.graph = graph
        self._set_scene()
        self._populate_palette()
        self._select_new_node(instance)
        self._show_message(
            self.tr("Created {name}. Its inputs and outputs remain yours to wire.").format(
                name=asset.name
            )
        )

    def _edit_implementations(self):
        if self._project is None or self._document is None:
            return
        if self._document.material_family is not None:
            self._show_message(self.tr("Native Shader Overrides are not available for shader-LOD families. The "
                                       "material graph can still be edited."))
            return
        from .implementation_editor import MaterialImplementationDialog
        dialog = MaterialImplementationDialog(
            self._project,
            self._document,
            self,
            source_name=self.source_path,
            project_dir=self.project_dir,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._project = dialog.project
            self._refresh_document_contract()
            self._set_scene()
            self._populate_palette()
            self._dirty = True
            self._refresh_readiness()

    def _graph_changed(self, *, rebuild_scene: bool = False) -> None:
        if rebuild_scene:
            self.scene.rebuild(select_node=self._selected_node_id)
        if self._project is not None and self._can_edit_interface() and prune_orphaned_material_inputs(
            self._project
        ):
            self._populate_palette()
            self.remove_input_action.setEnabled(False)
        self._dirty = True
        self._refresh_readiness()

    def _layout_changed(self) -> None:
        self._dirty = True

    def _set_build_status(self, message: str, *, ready=False, warning=False, details="") -> None:
        self.variant_button.setEnabled(ready)
        self.status.setText(message)
        self.status.setToolTip(details)
        color = "#d8c27a" if warning else "#80c99b" if ready else "#dc8585"
        self.status.setStyleSheet(f"QLabel {{ color:{color}; }}")

    def _refresh_readiness(self) -> None:
        self._last_build = None
        self._interface_changed = None
        if self._document is not None and self._contract is None:
            self._set_build_status(
                self._unavailable_interface_note(), warning=not self._contract_error,
            )
            return
        issue = self._authoring_issue()
        if issue:
            self._set_build_status(issue)
            return
        self._set_build_status(
            self.tr("Ready."), ready=True,
            details=self.tr("Shader recipes are reconstructed when saving, exporting, or creating a variant.")
            if self._project._prepare_semantics is not None else "",
        )

    def _authoring_issue(self) -> str:
        if self._project is None or self._document is None:
            return self.tr("Open a master material to begin.")
        if self._contract is None or self._coverage is None or not self._coverage.complete:
            return self._unavailable_interface_note()
        try:
            self._document.profile.shader_graph_profile.validate_material_project_edit(self._project, self._document)
        except ShaderGraphError as exc:
            return str(exc)
        issues = validate_material_project(self._project, self._contract)
        errors = [item.message for item in issues if item.severity == "error"]
        if errors:
            return self.tr("Material needs attention: {message}").format(
                message=errors[0]
            )
        if not material_semantics_complete(self._project):
            return self.tr(
                "Connect every parameter and texture slot before saving."
            )
        return ""

    def _background_build(self) -> None:
        if self._project is None or self._document is None:
            return
        issue = self._authoring_issue()
        if issue:
            self._last_build = None
            self._interface_changed = None
            self._set_build_status(issue)
            return
        self._set_build_status(self.tr("Compiling all material routes…"), warning=True)
        try:
            build = build_material_project(self._project, self._document)
        except MaterialBuildError as exc:
            self._last_build = None
            self._set_build_status(str(exc), details=exc.technical_details)
            return
        self._last_build = build
        self._interface_changed = not material_interface_matches_build(
            self._document,
            build,
        )
        if self._interface_changed:
            message = self.tr(
                "Ready — Create Variant will update the MMTR and MDF together."
            )
        else:
            message = self.tr("Ready to save.")
        diagnostics = list(dict.fromkeys(item.diagnostics for member in (build.family_builds or (build,))
            for item in member.replacements if item.diagnostics))
        if diagnostics:
            message += self.tr(" Compiler advisories — hover for details.")
        self._set_build_status(
            message, ready=True, warning=bool(diagnostics), details="\n\n".join(diagnostics),
        )

    def material_save_payload(self):
        if self._contract is None or self._project is None:
            if self._dirty:
                raise MaterialProjectError(
                    self.tr("This shader graph has no valid material interface. Discard the graph edits before saving the MMTR.")
                )
            return None
        if self._document.material_family is not None:
            if not self._dirty:
                return None
            raise MaterialProjectError(self.tr("Use Create Variant to save all shader LODs and the MDF together."))
        if self._coverage is None or not self._coverage.complete:
            if not self._dirty:
                return None
            raise MaterialProjectError(
                self.tr(
                    "This shader graph cannot be fully rebuilt. "
                    "Discard the graph edits before saving the MMTR."
                )
            )
        from . import material_implementation
        from .material_region_read import region_read_session, suspend_region_read_session
        from .native_build_cache import fresh_native_build_cache
        from .profiles import re_engine_dxil_authoring

        with suspend_region_read_session(), fresh_native_build_cache(
            re_engine_dxil_authoring._NATIVE_BUILD_CACHE,
            material_implementation._IMPLEMENTATION_BUILD_CACHE,
        ):
            project_data = self._project.to_json().encode("utf-8")
            with region_read_session():
                if not material_semantics_complete(self._project):
                    raise MaterialProjectError(
                        self.tr("Connect every material input before saving.")
                    )
                self._last_build = None
                self._background_build()
        if self._last_build is None:
            raise MaterialProjectError(
                self.tr("The material is not ready to save: {issue}").format(
                    issue=self.status.text()
                )
            )
        if self._interface_changed:
            raise MaterialProjectError(
                self.tr(
                    "Use Create Variant after changing parameters or texture slots so the MMTR and MDF stay in sync."
                )
            )
        return self._last_build, project_data

    def _create_variant(self) -> None:
        if self._last_build is None:
            self._background_build()
        if self._last_build is None or self._project is None:
            QMessageBox.warning(
                self,
                self.tr("Material not ready"),
                self.tr("Resolve the material issue shown beside Create Variant."),
            )
            return
        self.variant_requested.emit(self._last_build, self._project)

    def material_applied(self) -> None:
        self._dirty = False

    def _default_project_path(self) -> str:
        return material_project_sidecar_path(
            self.source_path,
            self.project_dir,
        ) if self.source_path else "material.reasymaterial.json"

    def _save_project(self) -> None:
        if self._project is None:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Save material project"),
            self._default_project_path(),
            self.tr("REasy material (*.reasymaterial.json);;JSON (*.json)"),
        )
        if not filename:
            return
        try:
            write_files_atomically({filename: self._project.to_json().encode("utf-8")})
        except (OSError, MaterialProjectError) as exc:
            QMessageBox.critical(self, self.tr("Material save failed"), str(exc))
            return
        self._project_path = filename
        self._show_message(
            self.tr("Material project copy saved. Save the MMTR to compile and commit the material.")
        )

    def _open_project(self) -> None:
        if self._document is None:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Open material project"),
            "",
            self.tr("REasy material (*.reasymaterial.json *.json);;All files (*)"),
        )
        if not filename:
            return
        try:
            raw = Path(filename).read_bytes()
            project = MmtrMaterialProject.from_json(raw.decode("utf-8"))
            self._upgrade_loaded_project(project)
        except (OSError, UnicodeError, MaterialProjectError, ShaderGraphError) as exc:
            QMessageBox.critical(self, self.tr("Material open failed"), str(exc))
            return
        self._project = project
        self._refresh_document_contract()
        self._project_path = filename
        self.material_name.setText(project.name)
        self._set_scene()
        self._populate_palette()
        self._populate_properties(None)
        self.fit_graph()
        self._dirty = True
        self._refresh_readiness()
        if not same_mmtr_profile(project.source_profile, self._document.profile.game):
            self._show_message(
                self.tr("Material adapted to {game}.").format(
                    game=self._document.profile.display_name
                )
            )

    def _upgrade_loaded_project(self, project) -> bool:
        upgraded = project.domain != "implementation" and bool(
            self._document.profile.shader_graph_profile.upgrade_material_project(self._document, project))
        return ensure_material_interface_nodes(project) or upgraded

    def _show_message(self, message: str) -> None:
        self.status.setText(str(message))

    def fit_graph(self) -> None:
        self.view.fit_scene()


__all__ = ["MmtrMaterialEditor"]
