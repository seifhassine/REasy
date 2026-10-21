from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

lazy from .models import RESOURCE_KINDS, STAGE_KEYS, shader_target
lazy from .dxc import dxc_available
lazy from .hlsl import hlsl_compiler_available
lazy from .hlsl_target import compile_hlsl_for_target, uses_dxc
lazy from .state_editor import select_preserved
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role


def _spin(maximum=0xFFFF) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(0, maximum)
    return widget


def _select_preserved_value(combo: QComboBox, value: int, label: str) -> None:
    select_preserved(combo, value, QCoreApplication.translate(
        "SdfProgramEditor", "{label} ({value}; unrecognized, preserved)").format(label=label, value=value))


def _reflected_target(shader) -> str:
    reflection = getattr(shader, "reflection", None)
    if reflection is None:
        return ""
    return shader_target(reflection.stage, reflection.model_major, reflection.model_minor)


def _compiler_missing(target: str) -> str:
    if uses_dxc(target):
        return "" if dxc_available() else QCoreApplication.translate(
            "SdfProgramEditor", "Microsoft DirectX Shader Compiler (dxcompiler.dll) was not found on this system.")
    return "" if hlsl_compiler_available() else QCoreApplication.translate(
        "SdfProgramEditor", "Microsoft D3DCompiler was not found on this system.")


class _HlslCompileDialog(QDialog):
    def __init__(self, role: str, parent=None, *, reflected_target: str = ""):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Compile HLSL for {role}").format(role=role))
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.entry = QLineEdit("main")
        self.target = QComboBox()
        stage = "vertex" if role == "input_signature" else role
        target = str(reflected_target or shader_target(stage, 5, 0))
        self.target.addItem(target, target)
        if target.startswith("lib_"):
            self.entry.clear()
            self.entry.setEnabled(False)
            self.entry.setPlaceholderText(self.tr("Uses the library's named exports"))
        self.debug = QCheckBox(self.tr("Debug symbols; disable optimization"))
        form.addRow(self.tr("Entry point"), self.entry)
        form.addRow(self.tr("Shader Model target"), self.target)
        form.addRow(self.debug)
        root.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)


class SdfProgramEditor(QWidget):
    general_edit_requested = Signal(object)
    shader_replace_requested = Signal(str, object)
    shader_clear_requested = Signal(str)
    input_layout_edit_requested = Signal(object)
    compact_declarations_edit_requested = Signal(object)
    binding_edit_requested = Signal(object)
    metadata_edit_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._program = None
        self._profile = None
        self._document = None
        # QTreeWidget.clear() emits currentItemChanged before the old item is destroyed.
        self._program_generation = 0
        self._binding_rows: list[tuple[int, int, object, object]] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self._build_general()
        self._build_shaders()
        self._build_input_layout()
        self._build_linkage()
        self._build_bindings()
        self.pages = dict(zip(("program", "shaders", "input", "linkage", "resources"),
                              map(self.tabs.widget, range(self.tabs.count()))))

    def show_page(self, name: str) -> None:
        if (page := self.pages.get(name)) is not None:
            self.tabs.setCurrentWidget(page)

    def _build_general(self) -> None:
        page = QWidget()
        form = QFormLayout(page)
        self.name = QLineEdit()
        self.name.setToolTip(
            self.tr("Program names are logical permutation keys and are renamed across every target variant.")
        )
        form.addRow(self.tr("Program name"), self.name)
        self.topology = QComboBox()
        form.addRow(self.tr("Primitive topology"), self.topology)
        group_host = QWidget()
        group_layout = QHBoxLayout(group_host)
        group_layout.setContentsMargins(0, 0, 0, 0)
        self.group_size = [_spin() for _ in range(3)]
        for label, widget in zip("XYZ", self.group_size):
            group_layout.addWidget(QLabel(label))
            group_layout.addWidget(widget)
        form.addRow(self.tr("Compute threads/group"), group_host)
        self.general_note = QLabel()
        self.general_note.setWordWrap(True)
        set_style_role(self.general_note, tone="muted")
        form.addRow(self.general_note)
        button = QPushButton(self.tr("Apply pass settings"))
        set_style_role(button, variant="primary")
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        button.clicked.connect(self._apply_general)
        form.addRow(button)
        self.tabs.addTab(page, self.tr("Pass setup"))

    def _build_shaders(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            self.tr(
                "Stages contain compiled shader programs (DXBC/DXIL containers, or SPIR-V modules). "
                "Replacements are parsed and stage-checked before save."
            )
        )
        note.setWordWrap(True)
        set_style_role(note, tone="muted")
        layout.addWidget(note)
        self.shaders = QTreeWidget()
        self.shaders.setHeaderLabels(
            [self.tr("Role"), self.tr("Shader model"), self.tr("Size"), self.tr("Sharing")]
        )
        self.shaders.currentItemChanged.connect(self._update_shader_buttons)
        self.shaders.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 4):
            self.shaders.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.shaders, 1)
        buttons = QHBoxLayout()
        self.import_shader = QPushButton(self.tr("Replace from file…"))
        self.compile_shader = QPushButton(self.tr("Compile HLSL…"))
        self.export_shader = QPushButton(self.tr("Export…"))
        self.clear_shader = QPushButton(self.tr("Unbind stage"))
        self.import_shader.setIcon(line_icon("import"))
        self.compile_shader.setIcon(line_icon("code"))
        self.export_shader.setIcon(line_icon("export"))
        self.clear_shader.setIcon(line_icon("remove", current_colors()["danger"]))
        set_style_role(self.clear_shader, variant="danger")
        self.import_shader.clicked.connect(self._import_shader)
        self.compile_shader.clicked.connect(self._compile_shader)
        self.export_shader.clicked.connect(self._export_shader)
        self.clear_shader.clicked.connect(self._clear_shader)
        for button in (self.import_shader, self.compile_shader, self.export_shader, self.clear_shader):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.tabs.addTab(page, self.tr("Compiled shaders"))

    def _table_page(self, title: str, note: QLabel, headers: list[str], stretch: tuple[int, ...],
                    add: tuple[str, object], remove, apply: tuple[str, object]) -> QTableWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note.setWordWrap(True)
        set_style_role(note, tone="muted")
        layout.addWidget(note)
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        for column in stretch:
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table, 1)
        buttons = QHBoxLayout()
        for index, (label, slot) in enumerate((add, (self.tr("Remove selected"), remove), apply)):
            if index == 2:
                buttons.addStretch(1)
            button = QPushButton(label)
            if index == 2:
                set_style_role(button, variant="primary")
            else:
                button.setIcon(line_icon(("add", "remove")[index]))
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.tabs.addTab(page, title)
        return table

    def _build_input_layout(self) -> None:
        self.input_validation = QLabel()
        self.inputs = self._table_page(
            self.tr("Mesh inputs"), self.input_validation,
            [self.tr("Semantic"), self.tr("Index"), self.tr("Format"), self.tr("Slot"),
             self.tr("Byte offset"), self.tr("Per instance")], (0, 2),
            (self.tr("Add element"), lambda: self._add_input_row()), self._remove_input_row,
            (self.tr("Apply input layout"), self._apply_input_layout),
        )

    def _build_bindings(self) -> None:
        page = QWidget()
        root = QVBoxLayout(page)
        self.binding_table = QTreeWidget()
        self.binding_table.setHeaderLabels(
            [self.tr("Resource"), self.tr("Kind"), self.tr("Type"), self.tr("Bound stages")]
        )
        self.binding_table.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 4):
            self.binding_table.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.binding_table.currentItemChanged.connect(self._binding_selected)
        root.addWidget(self.binding_table, 1)
        metadata_host = QGroupBox(self.tr("Interface metadata"))
        metadata_form = QFormLayout(metadata_host)
        self.resource_name = QLineEdit()
        metadata_form.addRow(self.tr("Engine binding name"), self.resource_name)
        self.cb_size = _spin(0x7FFFFFFF)
        metadata_form.addRow(self.tr("Constant-buffer size"), self.cb_size)
        self.variable_table = QTableWidget(0, 3)
        self.variable_table.setHorizontalHeaderLabels(
            [self.tr("Variable"), self.tr("Byte offset"), self.tr("Byte size")]
        )
        self.variable_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        metadata_form.addRow(self.tr("Variables"), self.variable_table)
        self.metadata_note = QLabel(
            self.tr(
                "Binding names and their engine lookup identifiers are kept in sync automatically. "
                "Keep this interface consistent with replacement shader inputs."
            )
        )
        self.metadata_note.setWordWrap(True)
        set_style_role(self.metadata_note, tone="muted")
        metadata_form.addRow(self.metadata_note)
        self.metadata_apply = QPushButton(self.tr("Apply interface metadata"))
        set_style_role(self.metadata_apply, variant="primary")
        self.metadata_apply.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.metadata_apply.clicked.connect(self._apply_metadata)
        metadata_form.addRow(self.metadata_apply)
        root.addWidget(metadata_host)
        form_host = QGroupBox(self.tr("Stage bindings"))
        form = QFormLayout(form_host)
        self.binding_type = QComboBox()
        form.addRow(self.tr("Resource class"), self.binding_type)
        self.stage_enabled: list[QCheckBox] = []
        self.stage_slots: list[QSpinBox] = []
        for label in ("VS", "HS", "DS", "GS", "PS", "CS"):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            enabled, slot = QCheckBox(self.tr("Bound")), _spin(0xFF)
            row_layout.addWidget(enabled)
            row_layout.addWidget(QLabel(self.tr("slot")))
            row_layout.addWidget(slot)
            row_layout.addStretch(1)
            form.addRow(label, row)
            self.stage_enabled.append(enabled)
            self.stage_slots.append(slot)
        self.binding_apply = QPushButton(self.tr("Apply selected binding"))
        set_style_role(self.binding_apply, variant="primary")
        self.binding_apply.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.binding_apply.clicked.connect(self._apply_binding)
        form.addRow(self.binding_apply)
        root.addWidget(form_host)
        self.tabs.addTab(page, self.tr("Data and textures"))

    def _build_linkage(self) -> None:
        note = QLabel(self.tr(
            "These compact declarations link engine vertex semantics and registers "
            "to the compiled shader signature."
        ))
        self.declarations = self._table_page(
            self.tr("Shader interface"), note,
            [self.tr("Semantic"), self.tr("Index"), self.tr("Register"), self.tr("Component"),
             self.tr("System value")], (0, 4),
            (self.tr("Add declaration"), lambda: self._add_declaration_row()), self._remove_declaration_row,
            (self.tr("Apply signature linkage"), self._apply_declarations),
        )

    def set_program(self, program, document, shared_shader_counts: dict[tuple[int, int], int]) -> None:
        self._program_generation += 1
        self._program, self._profile, self._document = program, document.profile, document
        self.name.setText(program.name)
        compute = program.shader("compute")
        compute_present = bool(compute and compute.present)
        raytracing = program.raytracing_library is not None
        self.topology.clear()
        if compute_present:
            # The serialized word is a zero wave limit, not a Topology ID.
            self.topology.addItem(self.tr("Not applicable"), 0)
        else:
            for value, label in sorted(self._profile.primitive_topology_names.items()):
                self.topology.addItem(label, value)
        _select_preserved_value(self.topology, program.primitive_topology_id, program.primitive_topology)
        for widget, value in zip(self.group_size, program.compute_group_size):
            widget.setValue(value)
        self.topology.setEnabled(not compute_present)
        for widget in self.group_size:
            widget.setEnabled(compute_present and not raytracing)
        self.general_note.setText(
            self.tr("DXR libraries use DispatchRays; compute dimensions remain 0 × 0 × 0.")
            if raytracing else
            self.tr("Compute stages require three non-zero dimensions.")
            if compute_present
            else self.tr("Graphics programs keep compute dimensions at 0 × 0 × 0.")
        )
        self._populate_shaders(shared_shader_counts)
        self._populate_inputs()
        self._populate_declarations()
        self._populate_bindings()

    def _populate_shaders(self, shared_counts) -> None:
        self.shaders.clear()
        references = (self._program.input_signature, *self._program.shaders)
        for shader in references:
            reflection = shader.reflection
            model = (
                f"{reflection.stage}_{reflection.model_major}_{reflection.model_minor}"
                if reflection is not None
                else f"SPIR-V {shader.spirv.execution_model_name}"
                if shader.spirv is not None
                else "—"
            )
            sharing = shared_counts.get((shader.offset, shader.size), 0) if shader.present else 0
            item = QTreeWidgetItem(
                [
                    shader.role.replace("_", " ").title(),
                    model,
                    f"{shader.size:,} B" if shader.present else "unbound",
                    f"{sharing} pass variants" if sharing else "—",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, shader.role)
            self.shaders.addTopLevelItem(item)
        if self.shaders.topLevelItemCount():
            self.shaders.setCurrentItem(self.shaders.topLevelItem(0))
        self._update_shader_buttons()

    def _update_shader_buttons(self, *_items) -> None:
        role, shader = self._selected_shader()
        present = bool(shader is not None and shader.present)
        spirv = bool(shader is not None and shader.spirv is not None)
        self.import_shader.setEnabled(role is not None)
        self.export_shader.setEnabled(present)
        self.clear_shader.setEnabled(present)
        if spirv:
            missing = self.tr("SPIR-V stages need the game's descriptor linkage; author them with the shader graph.")
        else:
            missing = _compiler_missing(_reflected_target(shader)) if role is not None else ""
        self.compile_shader.setEnabled(role is not None and not missing)
        self.compile_shader.setToolTip(missing)

    def _input_widgets(self, row: int):
        return tuple(self.inputs.cellWidget(row, column) for column in range(6))

    def _add_input_row(self, element=None) -> None:
        row = self.inputs.rowCount()
        self.inputs.insertRow(row)
        semantic, semantic_index, fmt, slot, offset, instance = (
            QComboBox(), _spin(0xFF), QComboBox(), _spin(0xFF), _spin(0x7FFFFFFF), QCheckBox()
        )
        for semantic_id, name in sorted(self._profile.semantic_names.items()):
            semantic.addItem(name, semantic_id)
        for format_id, (_dxgi_id, name) in sorted(self._profile.input_formats.items()):
            fmt.addItem(name, format_id)
        widgets = (semantic, semantic_index, fmt, slot, offset, instance)
        for column, widget in enumerate(widgets):
            self.inputs.setCellWidget(row, column, widget)
        if element is not None:
            _select_preserved_value(
                semantic,
                element.semantic_id,
                element.semantic,
            )
            semantic_index.setValue(element.semantic_index)
            _select_preserved_value(
                fmt,
                element.format_id,
                element.dxgi_format,
            )
            slot.setValue(element.input_slot)
            offset.setValue(element.aligned_byte_offset)
            instance.setChecked(element.per_instance)

    def _populate_inputs(self) -> None:
        self.inputs.setRowCount(0)
        layout = self._program.input_layout
        self.inputs.setEnabled(layout is not None)
        if layout is None:
            self.input_validation.setText(self.tr("This pass does not define mesh inputs."))
            set_style_role(self.input_validation, tone="muted")
            return
        for element in layout.elements:
            self._add_input_row(element)
        signature = self._program.shader("vertex")
        reflected = signature.reflection.input_signature if signature and signature.reflection else ()
        layout_keys = {(item.semantic.upper(), item.semantic_index) for item in layout.elements}
        shader_keys = {(item.semantic_name.upper(), item.semantic_index) for item in reflected}
        missing = sorted(shader_keys - layout_keys)
        extra = sorted(layout_keys - shader_keys)
        messages = []
        if missing:
            messages.append("shader inputs without layout elements: " + ", ".join(f"{name}{index}" for name, index in missing))
        if extra:
            messages.append("layout-only/system inputs: " + ", ".join(f"{name}{index}" for name, index in extra))
        self.input_validation.setText(
            " · ".join(messages) if messages else self.tr("Layout semantics match reflected vertex inputs.")
        )
        set_style_role(self.input_validation, tone="warning" if messages else "muted")

    def _remove_input_row(self) -> None:
        row = self.inputs.currentRow()
        if row >= 0:
            self.inputs.removeRow(row)

    def _apply_input_layout(self) -> None:
        elements = []
        for row in range(self.inputs.rowCount()):
            semantic, semantic_index, fmt, slot, offset, instance = self._input_widgets(row)
            elements.append(
                {
                    "semantic_id": int(semantic.currentData()),
                    "semantic_index": semantic_index.value(),
                    "format_id": int(fmt.currentData()),
                    "input_slot": slot.value(),
                    "aligned_byte_offset": offset.value(),
                    "per_instance": instance.isChecked(),
                }
            )
        self.input_layout_edit_requested.emit(elements)

    def _add_declaration_row(self, declaration=None) -> None:
        row = self.declarations.rowCount()
        self.declarations.insertRow(row)
        semantic, semantic_index, register = QComboBox(), _spin(0x0F), QSpinBox()
        register.setRange(-0x4000, 0x3FFF)
        component, system_value = QComboBox(), QComboBox()
        for semantic_id, name in sorted(self._profile.semantic_names.items()):
            if semantic_id <= 0x0F:
                semantic.addItem(name, semantic_id)
        for type_id, name in sorted(self._profile.component_type_names.items()):
            component.addItem(name, type_id)
        for value_id, name in sorted(self._profile.system_value_names.items()):
            system_value.addItem(name, value_id)
        widgets = (semantic, semantic_index, register, component, system_value)
        for column, widget in enumerate(widgets):
            self.declarations.setCellWidget(row, column, widget)
        semantic.setProperty("sdf_register_low_bit", 0)
        if declaration is not None:
            _select_preserved_value(
                semantic,
                declaration.semantic_id,
                declaration.semantic,
            )
            semantic_index.setValue(declaration.semantic_index)
            register.setValue(declaration.register)
            _select_preserved_value(
                component,
                declaration.component_type_id,
                declaration.component_type,
            )
            _select_preserved_value(
                system_value,
                declaration.system_value_id,
                declaration.system_value,
            )
            semantic.setProperty(
                "sdf_register_low_bit",
                int(declaration.register_low_bit),
            )

    def _populate_declarations(self) -> None:
        self.declarations.setRowCount(0)
        for declaration in self._program.compact_declarations:
            self._add_declaration_row(declaration)

    def _remove_declaration_row(self) -> None:
        row = self.declarations.currentRow()
        if row >= 0:
            self.declarations.removeRow(row)

    def _apply_declarations(self) -> None:
        declarations = []
        for row in range(self.declarations.rowCount()):
            semantic, semantic_index, register, component, system_value = (
                self.declarations.cellWidget(row, column) for column in range(5)
            )
            declarations.append(
                {
                    "semantic_id": int(semantic.currentData()),
                    "semantic_index": semantic_index.value(),
                    "register": register.value(),
                    "component_type_id": int(component.currentData()),
                    "system_value_id": int(system_value.currentData()),
                    "register_low_bit": int(
                        semantic.property("sdf_register_low_bit") or 0
                    ),
                }
            )
        self.compact_declarations_edit_requested.emit(declarations)

    def _populate_bindings(self) -> None:
        with QSignalBlocker(self.binding_table):
            self.binding_table.clear()
            self._binding_rows.clear()
            metadata = self._program.resource_metadata
            for kind_index, kind in enumerate(RESOURCE_KINDS):
                if kind_index >= len(self._program.bindings):
                    continue
                resources = metadata[kind_index] if kind_index < len(metadata) else ()
                for binding_index, binding in enumerate(self._program.bindings[kind_index]):
                    resource = resources[binding_index] if binding_index < len(resources) else None
                    stages = ", ".join(
                        f"{stage.upper()}:{binding.bind_slots[index]}"
                        for index, stage in enumerate(STAGE_KEYS)
                        if binding.stage_mask & (1 << index)
                    ) or "unbound"
                    resource_name = (
                        resource.name
                        if resource is not None
                        else self.tr("<missing {kind} metadata #{index}>").format(
                            kind=kind.replace("_", " "), index=binding_index
                        )
                    )
                    item = QTreeWidgetItem(
                        [resource_name, kind.replace("_", " "), binding.type_name, stages]
                    )
                    item.setData(
                        0,
                        Qt.ItemDataRole.UserRole,
                        (self._program_generation, kind_index, binding_index),
                    )
                    self.binding_table.addTopLevelItem(item)
                    self._binding_rows.append((kind_index, binding_index, binding, resource))
            if self.binding_table.topLevelItemCount():
                self.binding_table.setCurrentItem(self.binding_table.topLevelItem(0))

        self._binding_selected(self.binding_table.currentItem())

    def _binding_from_item(self, item):
        if item is None or self._program is None:
            return None
        try:
            selection = item.data(0, Qt.ItemDataRole.UserRole)
        except RuntimeError:
            # A queued callback can outlive the C++ QTreeWidgetItem wrapper.
            return None
        if not isinstance(selection, (tuple, list)) or len(selection) != 3:
            return None
        try:
            generation, kind_index, binding_index = (int(value) for value in selection)
        except (TypeError, ValueError):
            return None
        if generation != self._program_generation:
            return None
        if not 0 <= kind_index < min(len(RESOURCE_KINDS), len(self._program.bindings)):
            return None
        bindings = self._program.bindings[kind_index]
        if not 0 <= binding_index < len(bindings):
            return None
        metadata = self._program.resource_metadata
        resources = metadata[kind_index] if kind_index < len(metadata) else ()
        resource = resources[binding_index] if binding_index < len(resources) else None
        return kind_index, binding_index, bindings[binding_index], resource

    def _clear_binding_form(self) -> None:
        self.binding_apply.setEnabled(False)
        self.metadata_apply.setEnabled(False)
        self.resource_name.clear()
        self.cb_size.setValue(0)
        self.cb_size.setVisible(False)
        self.variable_table.setRowCount(0)
        self.variable_table.setVisible(False)
        self.binding_type.clear()
        for enabled, slot in zip(self.stage_enabled, self.stage_slots):
            enabled.setChecked(False)
            slot.setValue(0)

    def _binding_selected(self, item, _previous=None) -> None:
        selected = self._binding_from_item(item)
        if selected is None:
            self._clear_binding_form()
            return
        kind_index, _binding_index, binding, resource = selected
        self.binding_apply.setEnabled(True)
        self.metadata_apply.setEnabled(resource is not None)
        kind = RESOURCE_KINDS[kind_index]
        self.resource_name.setText(resource.name if resource is not None else "")
        is_buffer = kind_index == 0
        self.cb_size.setVisible(is_buffer)
        self.variable_table.setVisible(is_buffer)
        self.cb_size.setValue(resource.byte_size if is_buffer and resource is not None else 0)
        self.variable_table.setRowCount(0)
        if is_buffer and resource is not None:
            for variable in resource.variables:
                row = self.variable_table.rowCount()
                self.variable_table.insertRow(row)
                for column, value in enumerate(
                    (variable.name, str(variable.byte_offset), str(variable.byte_size))
                ):
                    self.variable_table.setItem(row, column, QTableWidgetItem(value))
        self.binding_type.clear()
        types = self._profile.resource_type_names.get(kind, {})
        for code, name in sorted(types.items()):
            self.binding_type.addItem(name, code)
        select_preserved(self.binding_type, binding.type_code, self.tr("{name} ({value}; preserved)").format(
            name=binding.type_name, value=f"0x{binding.type_code:02X}"))
        for stage_index, (enabled, slot) in enumerate(zip(self.stage_enabled, self.stage_slots)):
            enabled.setChecked(bool(binding.stage_mask & (1 << stage_index)))
            slot.setValue(binding.bind_slots[stage_index])

    def _apply_binding(self) -> None:
        selected = self._binding_from_item(self.binding_table.currentItem())
        if selected is None:
            return
        kind_index, binding_index, _binding, _resource = selected
        self.binding_edit_requested.emit(
            {
                "resource_kind": RESOURCE_KINDS[kind_index],
                "binding_index": binding_index,
                "bind_slots": [slot.value() for slot in self.stage_slots],
                "stage_mask": sum(
                    1 << index for index, enabled in enumerate(self.stage_enabled) if enabled.isChecked()
                ),
                "type_code": int(self.binding_type.currentData()),
            }
        )

    def _apply_metadata(self) -> None:
        selected = self._binding_from_item(self.binding_table.currentItem())
        if selected is None:
            return
        kind_index, binding_index, _binding, resource = selected
        if resource is None:
            return
        variables = None
        byte_size = None
        if kind_index == 0:
            byte_size = self.cb_size.value()
            variables = []
            for row in range(self.variable_table.rowCount()):
                try:
                    variables.append(
                        {
                            "name": self.variable_table.item(row, 0).text(),
                            "byte_offset": int(self.variable_table.item(row, 1).text(), 0),
                            "byte_size": int(self.variable_table.item(row, 2).text(), 0),
                        }
                    )
                except (AttributeError, ValueError) as exc:
                    QMessageBox.warning(
                        self,
                        self.tr("Invalid constant-buffer variable"),
                        self.tr("Variable offsets and sizes must be integers: {error}").format(error=exc),
                    )
                    return
        self.metadata_edit_requested.emit(
            {
                "resource_kind": RESOURCE_KINDS[kind_index],
                "resource_index": binding_index,
                "name": self.resource_name.text(),
                "byte_size": byte_size,
                "variables": variables,
            }
        )

    def _apply_general(self) -> None:
        self.general_edit_requested.emit(
            {
                "name": self.name.text(),
                "topology_id": int(self.topology.currentData()),
                "compute_group_size": tuple(widget.value() for widget in self.group_size),
            }
        )

    def _selected_shader(self):
        item = self.shaders.currentItem()
        if item is None or self._program is None:
            return None, None
        role = str(item.data(0, Qt.ItemDataRole.UserRole))
        shader = self._program.input_signature if role == "input_signature" else self._program.shader(role)
        return role, shader

    def _import_shader(self) -> None:
        role, shader = self._selected_shader()
        if role is None:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select a compiled shader for {role}").format(role=role),
            "",
            self.tr("SPIR-V module (*.spv);;All files (*)")
            if shader is not None and shader.spirv is not None else
            self.tr("Direct3D shader bytecode (*.dxbc *.cso *.bin);;All files (*)"),
        )
        if not filename:
            return
        try:
            raw = Path(filename).read_bytes()
        except OSError as exc:
            QMessageBox.critical(self, self.tr("Shader import failed"), str(exc))
            return
        self.shader_replace_requested.emit(role, raw)

    def _export_shader(self) -> None:
        role, shader = self._selected_shader()
        if role is None or shader is None or not shader.present:
            return
        spirv = shader.spirv is not None
        filename, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Export {role} shader").format(role=role),
            f"{self._program.name}_{role}.{'spv' if spirv else 'dxbc'}",
            self.tr("SPIR-V module (*.spv);;All files (*)") if spirv else
            self.tr("Direct3D shader bytecode (*.dxbc);;All files (*)"),
        )
        if not filename:
            return
        try:
            Path(filename).write_bytes(
                shader.spirv.module if spirv
                else shader.raytracing_library.bytecode if shader.raytracing_library else shader.payload
            )
        except OSError as exc:
            QMessageBox.critical(self, self.tr("Shader export failed"), str(exc))

    def _compile_shader(self) -> None:
        role, shader = self._selected_shader()
        if role is None:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select HLSL source for {role}").format(role=role),
            "",
            self.tr("HLSL source (*.hlsl *.fx);;All files (*)"),
        )
        if not filename:
            return
        dialog = _HlslCompileDialog(role, self, reflected_target=_reflected_target(shader))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            raw = compile_hlsl_for_target(
                path=filename, entry_point=dialog.entry.text(), target=str(dialog.target.currentData()),
                debug=dialog.debug.isChecked(),
            ).bytecode
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, self.tr("HLSL compilation failed"), str(exc))
            return
        self.shader_replace_requested.emit(role, raw)

    def _clear_shader(self) -> None:
        role, shader = self._selected_shader()
        if role is None or shader is None or not shader.present:
            return
        answer = QMessageBox.question(
            self,
            self.tr("Unbind shader"),
            self.tr("Unbind {role} from only this pass?").format(role=role),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.shader_clear_requested.emit(role)


__all__ = ["SdfProgramEditor"]
