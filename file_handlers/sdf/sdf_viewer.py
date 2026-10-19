from __future__ import annotations

from collections import Counter
import struct

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

lazy from .dxbc import DxbcDisassemblyError, disassemble_dxbc
lazy from .editing import SdfBinaryEditor, SdfEditError
lazy from .models import STAGE_KEYS, STATE_KINDS, SdfProgram, SdfRayTracingLibrary, stage_display_name
lazy from .pipeline_view import PipelineStatePreview, SdfPipelineGraph
lazy from .program_editor import SdfProgramEditor
lazy from .program_overview import SdfProgramOverview
lazy from .raytracing import raytracing_export_fields, serialize_raytracing_library
lazy from .shader_authoring_editor import SdfShaderAuthoringEditor
lazy from .state_editor import SdfStateEditor


def _hex_offset(value: int) -> str:
    return f"0x{value:X}" if value else "0"


def _default_value_format(variable) -> str | None:
    raw = variable.default_value
    if raw is None:
        return None
    type_id = variable.variable_type.variable_type_id
    formats = {
        1: ("I", lambda value: "true" if value else "false"),
        2: ("i", str),
        3: ("f", lambda value: f"{value:.9g}"),
        19: ("I", str),
        20: ("B", str),
        39: ("d", lambda value: f"{value:.17g}"),
        58: ("h", str),
        59: ("H", str),
        60: ("e", lambda value: f"{value:.7g}"),
        61: ("q", str),
        62: ("Q", str),
    }
    entry = formats.get(type_id)
    if entry is None:
        return None
    code, render = entry
    width = struct.calcsize("<" + code)
    if not raw or len(raw) % width:
        return None
    values = struct.unpack("<" + code * (len(raw) // width), raw)
    return ", ".join(render(value) for value in values)


def format_default_value(variable) -> str:
    decoded = _default_value_format(variable)
    if decoded is not None:
        shape = variable.variable_type
        dimensions = f"{shape.rows}×{shape.columns}"
        if shape.element_count:
            dimensions += f"[{shape.element_count}]"
        return f"{shape.variable_type} {dimensions}: {decoded}"
    return "default value has no registered scalar formatter"


class SdfViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler, parent=None, *, editor_only: bool = False):
        super().__init__(parent)
        self.handler = handler
        self._editor_only = bool(editor_only)
        self._modified = False
        self._history_transition = False
        self._disassembly_cache: dict[tuple[int, int], str] = {}

        root = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.summary = QLabel()
        controls.addWidget(self.summary, 1)
        if self._editor_only:
            root.addLayout(controls)
            self.tabs = QTabWidget()
            root.addWidget(self.tabs, 1)
            self._initialize_history()
            return
        self._current_program_key: tuple[int, int] | None = None
        self._active_target_index = 0
        self._program_items: list[QTreeWidgetItem] = []
        self._program_tree_generation = 0
        self._program_view_generation = 0
        self._rendered_program_tabs: dict[str, int] = {}
        self.hardware_variant_label = QLabel(self.tr("Hardware variant"))
        controls.addWidget(self.hardware_variant_label)
        self.target_selector = QComboBox()
        self.target_selector.setMinimumContentsLength(18)
        controls.addWidget(self.target_selector)
        self.purpose_filter = QComboBox()
        self.purpose_filter.setMinimumContentsLength(16)
        self.purpose_filter.setToolTip(
            self.tr("Show only passes used for a particular rendering purpose.")
        )
        controls.addWidget(self.purpose_filter)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(self.tr("Find a pass…"))
        controls.addWidget(self.filter)
        self.undo_button = QPushButton(self.tr("Undo"))
        self.redo_button = QPushButton(self.tr("Redo"))
        self.undo_button.setEnabled(False)
        self.redo_button.setEnabled(False)
        controls.addWidget(self.undo_button)
        controls.addWidget(self.redo_button)
        root.addLayout(controls)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.programs = QTreeWidget()
        self.programs.setRootIsDecorated(False)
        self.programs.setAlternatingRowColors(False)
        self.programs.setUniformRowHeights(True)
        self.programs.setHeaderLabels(
            [self.tr("Pass"), self.tr("Purpose"), self.tr("Stages")]
        )
        self.programs.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            self.programs.header().setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        self.main_splitter.addWidget(self.programs)

        self.tabs = QTabWidget()
        self.pass_workspace = QSplitter(Qt.Orientation.Horizontal)
        self.overview = SdfProgramOverview()
        self.program_editor = SdfProgramEditor()
        self.pass_workspace.addWidget(self.overview)
        self.pass_workspace.addWidget(self.program_editor)
        self.pass_workspace.setStretchFactor(0, 3)
        self.pass_workspace.setStretchFactor(1, 2)
        self.pass_workspace.setSizes([650, 430])
        self.tabs.addTab(self.pass_workspace, self.tr("Pass"))

        self.render_workspace = QSplitter(Qt.Orientation.Horizontal)
        render_preview = QSplitter(Qt.Orientation.Vertical)
        self.pipeline = SdfPipelineGraph()
        self.state_preview = PipelineStatePreview()
        render_preview.addWidget(self.pipeline)
        render_preview.addWidget(self.state_preview)
        render_preview.setStretchFactor(0, 3)
        render_preview.setStretchFactor(1, 2)
        self.state_editor = SdfStateEditor()
        self.render_workspace.addWidget(render_preview)
        self.render_workspace.addWidget(self.state_editor)
        self.render_workspace.setStretchFactor(0, 2)
        self.render_workspace.setStretchFactor(1, 1)
        self.render_workspace.setSizes([720, 360])
        self.tabs.addTab(self.render_workspace, self.tr("Render"))

        self.shader_graph = SdfShaderAuthoringEditor()
        self.shader_graph.source_path = str(getattr(handler, "filepath", "") or "")
        self.tabs.addTab(self.shader_graph, self.tr("Replacement Shader"))

        self.advanced_tabs = QTabWidget()
        self.details = QTreeWidget()
        self.details.setHeaderLabels([self.tr("Field"), self.tr("Value")])
        self.details.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.details.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.advanced_tabs.addTab(self.details, self.tr("Shader details"))

        self.disassembly = QPlainTextEdit()
        self.disassembly.setReadOnly(True)
        self.disassembly.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.advanced_tabs.addTab(self.disassembly, self.tr("Shader assembly"))
        self.diagnostics = QTreeWidget()
        self.diagnostics.setHeaderLabels(
            [self.tr("Diagnostic"), self.tr("Details")]
        )
        self.diagnostics.header().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.diagnostics.header().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.Stretch,
        )
        self.tabs.addTab(self.advanced_tabs, self.tr("Advanced"))
        self.main_splitter.addWidget(self.tabs)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setSizes([360, 1080])
        root.addWidget(self.main_splitter, 1)

        self.programs.currentItemChanged.connect(self._selection_changed)
        self.tabs.currentChanged.connect(self._active_tab_changed)
        self.advanced_tabs.currentChanged.connect(self._active_tab_changed)
        self.overview.navigate_requested.connect(self._overview_navigate)
        self.filter.textChanged.connect(self._apply_filter)
        self.purpose_filter.currentIndexChanged.connect(
            lambda _index: self._apply_filter(self.filter.text())
        )
        self.target_selector.currentIndexChanged.connect(self._target_changed)
        self.pipeline.node_selected.connect(self._pipeline_node_selected)
        self.state_editor.state_edit_requested.connect(self._state_edit_requested)
        self.state_editor.state_preview_changed.connect(
            self.state_preview.set_state_override
        )
        self.state_editor.blend_target_preview_changed.connect(
            self.state_preview.set_render_target
        )
        self.program_editor.general_edit_requested.connect(self._general_edit_requested)
        self.program_editor.shader_replace_requested.connect(self._shader_replace_requested)
        self.shader_graph.apply_requested.connect(self._shader_graph_apply_requested)
        self.program_editor.shader_clear_requested.connect(self._shader_clear_requested)
        self.program_editor.input_layout_edit_requested.connect(self._input_layout_edit_requested)
        self.program_editor.compact_declarations_edit_requested.connect(
            self._compact_declarations_edit_requested
        )
        self.program_editor.binding_edit_requested.connect(self._binding_edit_requested)
        self.program_editor.metadata_edit_requested.connect(self._metadata_edit_requested)
        self._initialize_history()
        self._refresh()

    def _initialize_history(self) -> None:
        self.handler.modified_changed.connect(self._handler_modified_changed)
        if self._editor_only:
            return
        self.undo_button.clicked.connect(self._undo)
        self.redo_button.clicked.connect(self._redo)
        initial_data = bytes(self.handler.sdf.raw_data)
        self._history = [initial_data]
        self._history_index = 0
        self._saved_history_index = 0

    @property
    def modified(self) -> bool:
        return self._modified

    @modified.setter
    def modified(self, value: bool) -> None:
        self._modified = bool(value)

    def _mark_modified(self, value: bool) -> None:
        normalized = bool(value)
        if self.handler.modified != normalized:
            self.handler.modified = normalized
        else:
            self._handler_modified_changed(normalized)

    def _handler_modified_changed(self, value: bool) -> None:
        normalized = bool(value)
        if not normalized and not self._history_transition and not self._editor_only:
            self._saved_history_index = self._history_index
        if self._modified != normalized:
            self._modified = normalized
            self.modified_changed.emit(normalized)

    @property
    def document(self):
        if self.handler.sdf is None:
            raise ValueError("no SDF file is loaded")
        return self.handler.sdf.require_document()

    def _program_purpose(self, program: SdfProgram) -> str:
        present = {shader.role for shader in program.shaders if shader.present}
        if "compute" in present and not (present - {"compute"}):
            return self.tr("Compute")
        if "pixel" not in present:
            return self.tr("Geometry / depth")
        return self.tr("Graphics")

    def _refresh(self) -> None:
        document = self.document
        self.overview.set_document(document)
        wanted = self._current_program_key
        programs = [*target.programs for target in document.targets]
        self._shared_shader_counts = Counter(
            (shader.offset, shader.size)
            for program in programs
            for shader in (program.input_signature, *program.shaders)
            if shader.present
        )
        self._state_reference_counts = {
            kind: Counter(
                getattr(program, field).offset for program in programs
                if getattr(program, field) is not None
            )
            for kind, (field, _method) in STATE_KINDS.items()
        }
        self.summary.setText(
            self.tr(
                "{profile} · {programs} rendering passes · {targets} compiled variants"
            ).format(
                profile=document.profile.display_name,
                targets=document.header.target_count,
                programs=len({p.name for p in programs}),
            )
        )
        self._update_diagnostics_panel()
        if not document.targets:
            with QSignalBlocker(self.target_selector):
                self.target_selector.clear()
            self._active_target_index = -1
            self._rebuild_program_tree(None)
            return
        target_indices = {target.index for target in document.targets}
        if wanted is not None and wanted[0] in target_indices:
            target_index = wanted[0]
        elif self._active_target_index in target_indices:
            target_index = self._active_target_index
        else:
            target_index = document.targets[0].index
        self._active_target_index = target_index

        with QSignalBlocker(self.target_selector):
            self.target_selector.clear()
            for target in document.targets:
                label = target.display_name
                if target.selection_capability_bit is None:
                    label += self.tr(" · baseline")
                self.target_selector.addItem(label, target.index)
                combo_index = self.target_selector.count() - 1
                self.target_selector.setItemData(
                    combo_index,
                    (
                        self.tr("Default hardware feature set")
                        if target.selection_capability_bit is None
                        else self.tr("Selected by graphics capability bit {bit}").format(
                            bit=target.selection_capability_bit
                        )
                    ),
                    Qt.ItemDataRole.ToolTipRole,
                )
            selected_index = self.target_selector.findData(target_index)
            self.target_selector.setCurrentIndex(max(0, selected_index))

        program_index = wanted[1] if wanted is not None else 0
        self._rebuild_program_tree((target_index, program_index))

    def _rebuild_program_tree(
        self,
        wanted: tuple[int, int] | None,
    ) -> None:
        target = self._target_for_index(self._active_target_index)
        if target is None:
            self._program_tree_generation += 1
            self.programs.clear()
            self._program_items.clear()
            self.purpose_filter.clear()
            self.purpose_filter.setVisible(False)
            self._current_program_key = None
            self._show_file_summary()
            return
        self._program_tree_generation += 1
        purposes = tuple(
            dict.fromkeys(self._program_purpose(program) for program in target.programs)
        )
        previous_purpose = self.purpose_filter.currentData()
        with QSignalBlocker(self.purpose_filter):
            self.purpose_filter.clear()
            self.purpose_filter.addItem(self.tr("All pass types"), "")
            for purpose in purposes:
                self.purpose_filter.addItem(purpose, purpose)
            previous_index = self.purpose_filter.findData(previous_purpose)
            self.purpose_filter.setCurrentIndex(max(0, previous_index))
            self.purpose_filter.setVisible(len(purposes) > 1)
        selected_item = None
        with QSignalBlocker(self.programs):
            self.programs.clear()
            self._program_items.clear()
            for program in target.programs:
                stages = ", ".join(
                    stage_display_name(shader.role)
                    for shader in program.shaders
                    if shader.present
                )
                item = QTreeWidgetItem(
                    [program.name, self._program_purpose(program), stages]
                )
                item.setData(
                    0, Qt.ItemDataRole.UserRole,
                    (self._program_tree_generation, target.index, program.program_index),
                )
                item.setToolTip(
                    0,
                    self.tr("{name}\n{target}, pass {program}").format(
                        name=program.name,
                        target=target.display_name,
                        program=program.program_index,
                    ),
                )
                self.programs.addTopLevelItem(item)
                self._program_items.append(item)
                if wanted == (target.index, program.program_index):
                    selected_item = item
            if self._program_items:
                self.programs.setCurrentItem(selected_item or self._program_items[0])
        if self._program_items:
            self._selection_changed(self.programs.currentItem())
            self._apply_filter(self.filter.text())
        else:
            self._current_program_key = None
            self._show_file_summary()

    def _target_changed(self, _index: int) -> None:
        target_index = self.target_selector.currentData()
        if target_index is None:
            return
        target_index = int(target_index)
        if target_index == self._active_target_index:
            return
        program_index = (
            self._current_program_key[1]
            if self._current_program_key is not None
            else 0
        )
        self._active_target_index = target_index
        self._rebuild_program_tree((target_index, program_index))

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        purpose = str(self.purpose_filter.currentData() or "")
        for item in self._program_items:
            haystack = " ".join(
                item.text(column) for column in range(item.columnCount())
            )
            haystack += " " + item.toolTip(0)
            haystack = haystack.casefold()
            item.setHidden(
                bool(needle and needle not in haystack)
                or bool(purpose and item.text(1) != purpose)
            )

    def _selection_changed(self, item: QTreeWidgetItem | None, _previous=None) -> None:
        try:
            selection = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        except RuntimeError:
            return
        if not selection:
            self._current_program_key = None
            self._show_file_summary()
            return
        generation, target_index, program_index = selection
        if generation != self._program_tree_generation:
            return
        program = self._program_for_key(target_index, program_index)
        if program is None:
            return
        self._current_program_key = (target_index, program_index)
        self._show_program(program)

    def _target_for_index(self, target_index: int):
        return next((target for target in self.document.targets if target.index == target_index), None)

    def _program_for_key(self, target_index: int, program_index: int):
        target = self._target_for_index(target_index)
        if target is None:
            return None
        return next(
            (candidate for candidate in target.programs if candidate.program_index == program_index),
            None,
        )

    def _add(self, parent, field: str, value="") -> QTreeWidgetItem:
        item = QTreeWidgetItem([str(field), str(value)])
        if parent is None:
            self.details.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _update_diagnostics_panel(self) -> None:
        self.diagnostics.clear()
        notes = self.document.notes
        tables = self.document.detached_variable_tables
        if notes:
            notes_root = QTreeWidgetItem(
                [
                    self.tr("Compatibility notes"),
                    self.tr("{count} items").format(count=len(notes)),
                ]
            )
            self.diagnostics.addTopLevelItem(notes_root)
            for note in notes:
                notes_root.addChild(
                    QTreeWidgetItem(
                        [
                            note.field,
                            f"[{note.status}] {note.message}",
                        ]
                    )
                )
        if tables:
            root = QTreeWidgetItem(
                [
                    self.tr("Unassigned parameter definitions"),
                    self.tr("{count} parameter groups").format(
                        count=len(tables)
                    ),
                ]
            )
            self.diagnostics.addTopLevelItem(root)
            root.addChild(
                QTreeWidgetItem(
                    [
                        self.tr("Meaning"),
                        self.tr(
                            "These decoded constant parameters are not linked to an active pass; they are kept for "
                            "compatibility."
                        ),
                    ]
                )
            )
            for table_index, table in enumerate(tables, start=1):
                table_item = QTreeWidgetItem(
                    [
                        self.tr("Parameter group {index}").format(
                            index=table_index
                        ),
                        self.tr("{count} variables").format(
                            count=len(table.variables)
                        ),
                    ]
                )
                root.addChild(table_item)
                for variable in table.variables:
                    table_item.addChild(
                        QTreeWidgetItem(
                            [
                                variable.name,
                                self.tr(
                                    "{size} bytes at buffer position {offset}"
                                ).format(
                                    offset=variable.byte_offset,
                                    size=variable.byte_size,
                                ),
                            ]
                        )
                    )

        tab_index = self.advanced_tabs.indexOf(self.diagnostics)
        if notes or tables:
            if tab_index < 0:
                self.advanced_tabs.addTab(
                    self.diagnostics,
                    self.tr("Diagnostics"),
                )
            self.diagnostics.expandToDepth(2)
        elif tab_index >= 0:
            self.advanced_tabs.removeTab(tab_index)

    def _show_file_summary(self) -> None:
        document = self.document
        self.overview.show_file_summary(document)
        self._program_view_generation += 1
        self.pipeline.clear_message(
            self.tr("Select a compiled program to visualize its graphics or compute flow.")
        )
        self.state_preview.set_program(None)
        self.program_editor.setEnabled(False)
        self.state_editor.setEnabled(False)
        self.shader_graph.clear_program()
        self.details.clear()
        self.disassembly.setPlainText(
            self.tr("Select a program to disassemble its compiled Shader Model 5 stages.")
        )
        self._rendered_program_tabs = dict.fromkeys(("details", "disassembly", "authoring"),
                                                     self._program_view_generation)

    def _show_program(self, program: SdfProgram) -> None:
        self.overview.set_program(program)
        self.pipeline.set_program(program)
        self.state_preview.set_program(program)
        self.program_editor.setEnabled(True)
        self.state_editor.setEnabled(True)
        self.shader_graph.set_program(program, self.document)
        self._program_view_generation += 1
        self._rendered_program_tabs.clear()
        self._render_active_program_tab(program)

    def _populate_authoring(self, program: SdfProgram) -> None:
        self.program_editor.set_program(program, self.document, self._shared_shader_counts)
        state_counts = {
            kind: self._state_reference_counts[kind].get(getattr(getattr(program, field), "offset", None), 0)
            for kind, (field, _method) in STATE_KINDS.items()
        }
        self.state_editor.set_program(program, self.document.profile, state_counts)

    def _populate_program_details(self, program: SdfProgram) -> None:
        self.details.clear()
        shaders = self._add(None, self.tr("Compiled shader reflection"))
        self._add_shader(shaders, program.input_signature)
        for shader in program.shaders:
            self._add_shader(shaders, shader)
        self.details.expandToDepth(2)

    def _render_active_program_tab(self, program: SdfProgram) -> None:
        current = self.tabs.currentWidget()
        if current in {self.pass_workspace, self.render_workspace}:
            key = "authoring"
        elif current is self.advanced_tabs:
            advanced = self.advanced_tabs.currentWidget()
            if advanced is self.details:
                key = "details"
            elif advanced is self.disassembly:
                key = "disassembly"
            else:
                return
        else:
            return
        if self._rendered_program_tabs.get(key) == self._program_view_generation:
            return
        if key == "authoring":
            self._populate_authoring(program)
        elif key == "details":
            self._populate_program_details(program)
        else:
            self.disassembly.setPlainText(self._program_disassembly_text(program))
        self._rendered_program_tabs[key] = self._program_view_generation

    def _active_tab_changed(self, _index: int) -> None:
        if self._current_program_key is None:
            return
        program = self._program_for_key(*self._current_program_key)
        if program is not None:
            self._render_active_program_tab(program)

    def _selected_program_key(self) -> tuple[int, int]:
        if self._current_program_key is None:
            raise SdfEditError("select a program before editing")
        return self._current_program_key

    def _commit_edit(self, operation) -> bool:
        self._history_transition = True
        try:
            changed = self.handler.apply_edit(operation)
        except Exception as exc:
            QMessageBox.critical(
                self,
                self.tr("SDF/MMTR edit rejected"),
                str(exc),
            )
            return False
        finally:
            self._history_transition = False
        if changed is False:
            return True
        if not self._editor_only:
            self._history = self._history[: self._history_index + 1]
            self._history.append(bytes(self.handler.sdf.raw_data))
            self._history_index += 1
            if len(self._history) > 12:
                removed = len(self._history) - 12
                del self._history[:removed]
                self._history_index -= removed
                self._saved_history_index = max(-1, self._saved_history_index - removed)
            self._update_history_buttons()
        self._disassembly_cache.clear()
        self._mark_modified(True)
        self._refresh()
        return True

    def _update_history_buttons(self) -> None:
        self.undo_button.setEnabled(self._history_index > 0)
        self.redo_button.setEnabled(self._history_index + 1 < len(self._history))

    def _restore_history(self, index: int) -> None:
        if not 0 <= index < len(self._history):
            return
        self._history_transition = True
        try:
            self.handler.read(self._history[index])
            self._history_index = index
            self.handler.modified = self._history_index != self._saved_history_index
        finally:
            self._history_transition = False
        self._modified = self.handler.modified
        self._disassembly_cache.clear()
        self._update_history_buttons()
        self._refresh()

    def _undo(self) -> None:
        self._restore_history(self._history_index - 1)

    def _redo(self) -> None:
        self._restore_history(self._history_index + 1)

    def _edit_selected(self, method: str, *args, **kwargs) -> None:
        target_index, program_index = self._selected_program_key()
        self._commit_edit(
            lambda editor: getattr(editor, method)(target_index, program_index, *args, **kwargs)
        )

    def _state_edit_requested(self, kind: str, values, isolate: bool) -> None:
        self._edit_selected(STATE_KINDS[kind][1], values, isolate=isolate)

    def _general_edit_requested(self, values) -> None:
        target_index, program_index = self._selected_program_key()
        current = self._program_for_key(target_index, program_index)
        if current is None:
            return

        def operation(editor: SdfBinaryEditor) -> None:
            if str(values["name"]) != current.name:
                editor.rename_logical_program(program_index, values["name"])
            editor.set_primitive_topology(target_index, program_index, values["topology_id"])
            editor.set_compute_group_size(target_index, program_index, values["compute_group_size"])

        self._commit_edit(operation)

    def _shader_replace_requested(self, role: str, raw: bytes) -> None:
        self._edit_selected("replace_shader", role, raw)

    def _shader_graph_apply_requested(self, role: str, raw: bytes, targets) -> None:
        requested = tuple((int(target), int(program)) for target, program in targets)
        if not requested:
            return

        library = None
        if isinstance(raw, SdfRayTracingLibrary):
            library = serialize_raytracing_library(raw)

        def operation(editor: SdfBinaryEditor) -> None:
            for target_index, program_index in requested:
                if library is not None:
                    editor.replace_shader_payload(target_index, program_index, role, library,
                                                  binary_format='dxil-raytracing-library')
                else:
                    editor.replace_shader(target_index, program_index, role, raw)

        self._commit_edit(operation)

    def _shader_clear_requested(self, role: str) -> None:
        self._edit_selected("clear_shader", role)

    def _input_layout_edit_requested(self, elements) -> None:
        self._edit_selected("set_input_layout", elements, isolate=True)

    def _compact_declarations_edit_requested(self, declarations) -> None:
        self._edit_selected("set_compact_declarations", declarations, isolate=True)

    def _binding_edit_requested(self, values) -> None:
        self._edit_selected(
            "set_binding", values["resource_kind"], values["binding_index"],
            bind_slots=values["bind_slots"], stage_mask=values["stage_mask"],
            type_code=values["type_code"], isolate=True,
        )

    def _metadata_edit_requested(self, values) -> None:
        self._edit_selected(
            "set_resource_metadata", values["resource_kind"], values["resource_index"],
            name=values["name"], byte_size=values["byte_size"], variables=values["variables"],
        )

    def _overview_navigate(self, action: str) -> None:
        if action == "reflection":
            self.tabs.setCurrentWidget(self.advanced_tabs)
            self.advanced_tabs.setCurrentWidget(self.details)
            return
        if action == "assembly":
            self.tabs.setCurrentWidget(self.advanced_tabs)
            self.advanced_tabs.setCurrentWidget(self.disassembly)
            return

        if action == "state":
            self.tabs.setCurrentWidget(self.render_workspace)
            return
        self.tabs.setCurrentWidget(self.pass_workspace)
        self.program_editor.show_page(action)

    def _pipeline_node_selected(self, key: str, payload) -> None:
        if key in {"rasterizer", "output"}:
            self.tabs.setCurrentWidget(self.render_workspace)
            return
        self.tabs.setCurrentWidget(self.pass_workspace)
        if key in STAGE_KEYS:
            self.program_editor.show_page("shaders")
        elif key == "input":
            self.program_editor.show_page("input")
        elif key.startswith("resources"):
            self.program_editor.show_page("resources")

    def _add_shader(self, parent, shader) -> None:
        item = self._add(parent, shader.role, "absent" if not shader.present else f"{shader.size:,} bytes")
        if not shader.present:
            return
        self._add(item, "offset", _hex_offset(shader.offset))
        if shader.raytracing_library is not None:
            exports = self._add(item, "Ray-tracing library exports")
            self._add(exports, "layout", shader.raytracing_library.export_layout)
            for field in raytracing_export_fields(shader.raytracing_library.export_layout):
                self._add(exports, field, getattr(shader.raytracing_library, field) or "unbound")
        reflection = shader.reflection
        if reflection is None:
            return
        self._add(item, "shader model", f"{reflection.stage}_{reflection.model_major}_{reflection.model_minor}")
        self._add(item, "DXBC checksum", reflection.checksum_hex)
        self._add(item, "SHA-256", reflection.sha256)
        self._add(item, "container version", reflection.container_version)
        self._add(item, "container size", reflection.total_size)
        chunks = self._add(item, "DXBC chunks", len(reflection.chunks))
        for chunk in reflection.chunks:
            self._add(
                chunks,
                chunk.tag,
                f"{chunk.size:,} bytes @ {_hex_offset(chunk.offset)}",
            )
        self._add_signature(item, "input signature", reflection.input_signature)
        self._add_signature(item, "output signature", reflection.output_signature)
        self._add_signature(item, "patch signature", reflection.patch_signature)
        if reflection.reflection is not None:
            rdef = reflection.reflection
            rdef_item = self._add(item, "RDEF reflection")
            if rdef.library_functions:
                functions = self._add(rdef_item, "Library functions")
                for function in rdef.library_functions:
                    self._add(functions, function.name, function.stage)
            self._add(rdef_item, "creator", rdef.creator)
            self._add(
                rdef_item,
                "shader type",
                f"{rdef.shader_type} ({rdef.shader_type_id}); "
                f"model {rdef.major_version}.{rdef.minor_version}",
            )
            self._add(rdef_item, "compiler flags", f"0x{rdef.flags:08X}")
            self._add(
                rdef_item,
                "descriptor ABI",
                f"{rdef.descriptor_abi}; tag 0x{rdef.descriptor_tag:08X}; "
                f"resource stride {rdef.resource_stride}",
            )
            self._add(
                rdef_item,
                "descriptor strides",
                f"header={rdef.header_size}, cbuffer={rdef.constant_buffer_stride}, "
                f"resource={rdef.resource_stride}, variable={rdef.variable_stride}, "
                f"type={rdef.type_stride}, member={rdef.type_member_stride}",
            )
            resources = self._add(rdef_item, "bound resources", len(rdef.resources))
            for resource in rdef.resources:
                value = (
                    f"{resource.input_type} {resource.dimension}; "
                    f"slot {resource.bind_point}, count {resource.bind_count}"
                )
                resource_item = self._add(resources, resource.name, value)
                self._add(
                    resource_item,
                    "input type",
                    f"{resource.input_type} ({resource.input_type_id})",
                )
                self._add(
                    resource_item,
                    "return type",
                    f"{resource.return_type} ({resource.return_type_id})",
                )
                self._add(
                    resource_item,
                    "dimension",
                    f"{resource.dimension} ({resource.dimension_id})",
                )
                self._add(resource_item, "sample count", resource.sample_count)
                self._add(resource_item, "flags", f"0x{resource.flags:08X}")
                self._add(resource_item, "register space / ID", f"{resource.register_space} / {resource.resource_id}")
            buffers = self._add(rdef_item, "constant buffers", len(rdef.constant_buffers))
            for buffer in rdef.constant_buffers:
                buffer_item = self._add(
                    buffers,
                    buffer.name,
                    f"{buffer.byte_size} bytes; {buffer.variable_count} variables",
                )
                self._add(
                    buffer_item,
                    "buffer type",
                    f"{buffer.buffer_type} ({buffer.buffer_type_id})",
                )
                self._add(buffer_item, "flags", f"0x{buffer.flags:08X}")
                for variable in buffer.variables:
                    variable_item = self._add(
                        buffer_item,
                        variable.name,
                        f"{variable.variable_type.display_name}; "
                        f"offset {variable.start_offset}, size {variable.size}",
                    )
                    self._add(variable_item, "flags", f"0x{variable.flags:08X}")
                    self._add(
                        variable_item,
                        "texture range",
                        f"start {variable.start_texture}, count {variable.texture_size}",
                    )
                    self._add(
                        variable_item,
                        "sampler range",
                        f"start {variable.start_sampler}, count {variable.sampler_size}",
                    )
                    if variable.default_value is not None:
                        self._add(
                            variable_item,
                            "default value",
                            format_default_value(variable),
                        )
                    self._add_dxbc_type(variable_item, variable.variable_type)
            types = self._add(rdef_item, "referenced type descriptors", len(rdef.types))
            for variable_type in rdef.types:
                self._add_dxbc_type(types, variable_type, expand_members=False)
        if reflection.statistics:
            statistics = self._add(item, "Shader statistics", len(reflection.statistics))
            for statistic in reflection.statistics:
                self._add(statistics, statistic.name, statistic.display_value)
        if reflection.feature_flags:
            features = self._add(
                item,
                "Shader feature flags",
                f"0x{reflection.feature_flags:016X}",
            )
            for feature in reflection.features:
                self._add(features, feature, "required")
            if reflection.unknown_feature_flags:
                self._add(
                    features,
                    "unrecognized bits",
                    f"0x{reflection.unknown_feature_flags:016X}",
                )

    def _add_dxbc_type(
        self,
        parent,
        variable_type,
        ancestors=frozenset(),
        *,
        expand_members: bool = True,
    ) -> None:
        item = self._add(
            parent,
            "type",
            f"{variable_type.display_name}; {variable_type.variable_class}; "
            f"{variable_type.rows} × {variable_type.columns}",
        )
        self._add(
            item,
            "class / type IDs",
            f"{variable_type.variable_class_id} / {variable_type.variable_type_id}",
        )
        self._add(item, "D3D variable type", variable_type.variable_type)
        self._add(item, "array elements", variable_type.element_count)
        if variable_type.offset in ancestors:
            self._add(item, "members", "recursive reference")
            return
        lineage = ancestors | {variable_type.offset}
        members = self._add(item, "members", variable_type.member_count)
        for member in variable_type.members:
            member_item = self._add(
                members,
                member.name,
                f"{member.variable_type.display_name}; byte {member.byte_offset}",
            )
            if expand_members:
                self._add_dxbc_type(member_item, member.variable_type, lineage)

    def _add_signature(self, parent, label: str, parameters) -> None:
        group = self._add(parent, label, len(parameters))
        for parameter in parameters:
            name = f"{parameter.semantic_name}{parameter.semantic_index}"
            value = (
                f"r{parameter.register}.{parameter.components}; {parameter.component_type}; "
                f"{parameter.system_value}"
            )
            item = self._add(group, name, value)
            self._add(item, "register", parameter.register)
            self._add(
                item,
                "mask / read-write mask",
                f"0x{parameter.mask:X} / 0x{parameter.read_write_mask:X}",
            )
            self._add(item, "stream", parameter.stream)
            self._add(item, "minimum precision", parameter.minimum_precision)

    def _program_disassembly_text(self, program: SdfProgram) -> str:
        sections: list[str] = []
        for shader in program.shaders:
            if not shader.present:
                continue
            cache_key = (shader.offset, shader.size)
            assembly = self._disassembly_cache.get(cache_key)
            if assembly is None:
                raw = self.document.original_data[shader.offset : shader.offset + shader.size]
                try:
                    assembly = disassemble_dxbc(raw)
                except DxbcDisassemblyError as exc:
                    assembly = self.tr("Disassembly unavailable: {error}").format(error=exc)
                self._disassembly_cache[cache_key] = assembly
            sections.append(f"// {shader.role} @ 0x{shader.offset:X} ({shader.size} bytes)\n{assembly}")
        return "\n\n".join(sections) or self.tr("This program has no executable shader stages.")

__all__ = ["SdfViewer"]
