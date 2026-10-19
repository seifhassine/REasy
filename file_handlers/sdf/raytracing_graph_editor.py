from dataclasses import asdict
import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSplitter,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from services.file_io import write_files_atomically

from .aggregate_values import constant_value_fields
from .raytracing import raytracing_export_fields, serialize_raytracing_library
from .raytracing_abi import validate_raytracing_replacement
from .raytracing_graph import (RAY_BUILTINS, RAY_STAGES, RayField, RayStructure, RayTracingGraphProject,
    build_raytracing_graph, generate_raytracing_library, new_raytracing_graph)
from .shader_graph import MAX_PROJECT_LENGTH, ShaderGraphError, node_pins
from .shader_graph_editor import _ShaderGraphScene, _ShaderGraphView


class RayTracingGraphEditor(QWidget):
    apply_requested = Signal(str, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = self.library = self.compiled = self.program = None
        self._projects = {}
        self._key = None
        self._selected_node = ''
        root = QVBoxLayout(self)
        self.note = QLabel(self.tr(
            'From-scratch replacement graphs, not recovered original behavior. Native payload layouts are retained; '
            'field0 / field1 labels are used where names were stripped. Nothing changes until Apply. '
            'Extra exports do not create runtime shader-table records.'))
        self.note.setWordWrap(True)
        root.addWidget(self.note)
        toolbar = QHBoxLayout()
        self.export_selector = QComboBox()
        toolbar.addWidget(self.export_selector, 1)
        for label, callback in [(self.tr('Add export'), self._add_export), (self.tr('Edit export'), self._edit_export),
                                (self.tr('Remove export'), self._remove_export),
                                (self.tr('Types / bindings'), self._edit_interface),
                                (self.tr('Open graph'), self._open), (self.tr('Save graph'), self._save)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        root.addLayout(toolbar)
        splitter = QSplitter()
        self.palette = QTreeWidget()
        self.palette.setHeaderLabel(self.tr('Add a node (double-click)'))
        splitter.addWidget(self.palette)
        self.scene = _ShaderGraphScene(self)
        self.view = _ShaderGraphView()
        self.view.setScene(self.scene)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.view, self.tr('Export graph'))
        self.source = QPlainTextEdit()
        self.source.setReadOnly(True)
        self.tabs.addTab(self.source, self.tr('Generated library'))
        splitter.addWidget(self.tabs)
        inspector = QWidget()
        form = QVBoxLayout(inspector)
        form.addWidget(QLabel(self.tr('Selected node properties (typed JSON)')))
        self.properties = QPlainTextEdit()
        form.addWidget(self.properties)
        self.set_properties = QPushButton(self.tr('Update node'))
        self.set_properties.clicked.connect(self._update_node)
        form.addWidget(self.set_properties)
        form.addWidget(QLabel(self.tr('Click an output socket, then its input.\nDelete removes selected nodes / links.')))
        splitter.addWidget(inspector)
        splitter.setSizes([220, 760, 260])
        root.addWidget(splitter, 1)
        buttons = QHBoxLayout()
        for label, callback in [(self.tr('Generate source'), self._generate), (self.tr('Compile / validate'), self._compile),
                                (self.tr('Export compiled library'), self._export_library)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        self.apply_button = QPushButton(self.tr('Apply to selected program'))
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        buttons.addWidget(self.apply_button)
        root.addLayout(buttons)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.export_selector.currentIndexChanged.connect(self._select_export)
        self.palette.itemDoubleClicked.connect(self._add_node)
        self.scene.node_selected.connect(self._select_node)
        self.scene.graph_changed.connect(self._changed)
        self.scene.layout_changed.connect(self._retain)
        self.scene.message.connect(self.status.setText)

    def set_program(self, program):
        self.program = program
        self.library = program.shader('compute').raytracing_library
        self._key = (program.target_index, program.program_index)
        self.compiled = None
        self.apply_button.setEnabled(False)
        try:
            self.project = self._projects.get(self._key) or new_raytracing_graph(self.library)
        except ValueError as exc:
            self.project = None
            self.scene.set_graph(None, None)
            self.status.setText(str(exc))
            self.setEnabled(False)
            return
        self.setEnabled(True)
        self._refresh_exports()
        self.status.setText(self.tr('Replacement workspace ready. Build and validate before applying.'))

    def clear_program(self):
        self.project = self.library = self.compiled = self.program = None
        self._key = None
        self.scene.set_graph(None, None)
        self.export_selector.clear()
        self.apply_button.setEnabled(False)

    def _retain(self):
        if self.project is not None and self._key is not None:
            self._projects[self._key] = self.project

    def _changed(self):
        self.compiled = None
        self.apply_button.setEnabled(False)
        self.source.clear()
        self._retain()
        self.status.setText(self.tr('Graph changed. Compile / validate to check this replacement.'))

    def _export(self):
        index = self.export_selector.currentIndex()
        return self.project.exports[index] if self.project and 0 <= index < len(self.project.exports) else None

    def _refresh_exports(self, name=''):
        name = name or (self._export().name if self._export() else '')
        self.export_selector.blockSignals(True)
        self.export_selector.clear()
        for export in self.project.exports:
            self.export_selector.addItem(f'{export.name} · {export.stage}', export.name)
        self.export_selector.setCurrentIndex(max(0, self.export_selector.findData(name)))
        self.export_selector.blockSignals(False)
        self._select_export()

    def _select_export(self, *_args):
        export = self._export()
        self.scene.set_graph(export.graph if export else None, self.project.contract if self.project else None,
                             validator=lambda _graph, _contract: ())
        self._select_node('')
        self._fill_palette()

    def _fill_palette(self):
        self.palette.clear()
        export = self._export()
        if export is None:
            return
        groups = {}

        def add(group, label, kind, properties=None):
            if group not in groups:
                groups[group] = QTreeWidgetItem(self.palette, [group])
            row = QTreeWidgetItem(groups[group], [label])
            row.setData(0, Qt.ItemDataRole.UserRole, (kind, properties or {}))

        for kind in ('ray_if', 'ray_loop', 'ray_return', 'ray_break', 'ray_continue'):
            add('Control flow', kind.removeprefix('ray_').replace('_', ' ').title(), kind)
        if export.callable_type:
            add('Control flow', 'Call shader', 'ray_call')
        if export.stage == 'intersection' and export.attribute_type:
            add('Control flow', 'Report hit', 'ray_report_hit')
        if export.stage == 'anyhit':
            for kind in ('ray_ignore_hit', 'ray_accept_hit'):
                add('Control flow', kind.removeprefix('ray_'), kind)
        for node in export.graph.nodes:
            if node.kind == 'ray_loop':
                add('Control flow', 'Index of ' + node.node_id[:8], 'ray_loop_index', {'loop': node.node_id})
        for domain, type_name in [('payload', export.payload_type), ('attributes', export.attribute_type),
                                  ('callable', export.callable_type)]:
            if not type_name:
                continue
            for path, value_type in self.project.fields(type_name):
                props = dict(domain=domain, field=path, value_type=value_type, display_name=domain + path)
                add('Read values', domain + path, 'ray_state', props)
                if domain != 'attributes' or export.stage not in {'closesthit', 'anyhit'}:
                    add('Write values', domain + path, 'ray_set', props)
        for builtin in RAY_BUILTINS:
            add('Ray built-ins', builtin, 'ray_builtin', {'builtin': builtin})
        for value_type in ('float', 'float2', 'float3', 'float4', 'uint', 'uint2', 'uint3', 'uint4', 'int', 'bool'):
            width = int(value_type[-1]) if value_type[-1].isdigit() else 1
            add('Constants', value_type, 'constant', dict(value_type=value_type, values=[0] * width))
        for kind in ('add', 'subtract', 'multiply', 'divide', 'minimum', 'maximum', 'dot', 'compose', 'swizzle',
                     'normalize', 'clamp', 'lerp', 'ray_compare', 'ray_binary', 'ray_cast'):
            add('Math', kind.removeprefix('ray_'), kind)
        contract = self.project.contract
        for buffer in contract.constant_buffers:
            for variable in buffer.variables:
                for field in constant_value_fields(variable):
                    add('Renderer constants', buffer.name + '.' + variable.name + field.path, 'ray_constant',
                        dict(buffer=buffer.name, variable=variable.name, field=field.path, value_type=field.value_type))
        for resource in contract.resources:
            props = dict(resource=resource.name)
            if resource.input_type == 'raytracing-acceleration-structure' and export.payload_type:
                add('Resources', 'Trace ' + resource.name, 'ray_trace', props)
            elif resource.register_class in {'t', 'u'}:
                value_type = ('uint4' if 'byte-address' in resource.input_type or resource.return_type == 'uint'
                              else 'int4' if resource.return_type == 'sint' else 'float4')
                props['value_type'] = value_type
                add('Resources', 'Read ' + resource.name, 'ray_read', props)
                if resource.register_class == 'u':
                    add('Resources', 'Store ' + resource.name, 'ray_store', props)
                if resource.sampleable:
                    for sampler in contract.resources:
                        if sampler.input_type == 'sampler' and not sampler.flags & 2:
                            add('Resources', f'Sample {resource.name} / {sampler.name}', 'ray_sample',
                                dict(props, sampler=sampler.name))
        for group in groups.values():
            group.setExpanded(True)

    def _add_node(self, item, _column=0):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        export = self._export()
        if not data or export is None:
            return
        kind, properties = data
        position = self.view.mapToScene(self.view.viewport().rect().center())
        node = export.graph.add_node(kind, x=position.x(), y=position.y(), properties=properties)
        self.scene.rebuild(select_node=node.node_id)
        self._select_node(node.node_id)
        self._changed()
        self._fill_palette()

    def _select_node(self, node_id):
        self._selected_node = node_id
        node = self._export().graph.node(node_id) if self._export() else None
        self.properties.setPlainText(json.dumps(node.properties, indent=2) if node else '')
        self.set_properties.setEnabled(node is not None)

    def _update_node(self):
        node = self._export().graph.node(self._selected_node) if self._export() else None
        if node is None:
            return
        previous = node.properties
        try:
            properties = json.loads(self.properties.toPlainText())
            if not isinstance(properties, dict):
                raise ShaderGraphError('Node properties must be a JSON object')
            node.properties = properties
            node_pins(node, self.project.contract)
            self.project.to_bytes()
        except ValueError as exc:
            node.properties = previous
            self.status.setText(str(exc))
            return
        self.scene.rebuild(select_node=node.node_id)
        self._changed()

    def _export_dialog(self, export=None):
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr('Ray-tracing export'))
        form = QFormLayout(dialog)
        name = QLineEdit(export.name if export else 'NewExport')
        stage = QComboBox()
        stage.addItems(RAY_STAGES)
        if export:
            stage.setCurrentText(export.stage)
            stage.setEnabled(False)
        form.addRow(self.tr('Export name'), name)
        form.addRow(self.tr('Stage'), stage)
        values = {}
        for field, label in [('payload_type', self.tr('Payload')), ('attribute_type', self.tr('Attributes')),
                             ('callable_type', self.tr('Callable data'))]:
            widget = QComboBox()
            widget.addItem('')
            widget.addItems([t.name for t in self.project.structures])
            if export:
                widget.setCurrentText(getattr(export, field))
            values[field] = widget
            form.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dict(name=name.text(), stage=stage.currentText(),
                    **{field: widget.currentText() for field, widget in values.items()})

    def _add_export(self):
        values = self._export_dialog()
        if values is None:
            return
        try:
            export = self.project.add_export(**values)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self._refresh_exports(export.name)
        self._changed()

    def _edit_export(self):
        export = self._export()
        if export is None:
            return
        values = self._export_dialog(export)
        if values is None:
            return
        try:
            candidate = RayTracingGraphProject.from_bytes(self.project.to_bytes())
            index = self.project.exports.index(export)
            updated = candidate.exports[index]
            for field, value in values.items():
                setattr(updated, field, value)
            candidate.bindings = {role: updated.name if name == export.name else name
                                  for role, name in candidate.bindings.items()}
            candidate.validate()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.project = candidate
        self._refresh_exports(updated.name)
        self._changed()

    def _remove_export(self):
        export = self._export()
        if export is None:
            return
        if export.name in self.project.bindings.values():
            self.status.setText(self.tr('Change the envelope binding before removing a selected export.'))
            return
        self.project.exports.remove(export)
        self._refresh_exports()
        self._changed()

    def _edit_interface(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr('Declared types and runtime export bindings'))
        form = QVBoxLayout(dialog)
        roles = ', '.join(raytracing_export_fields(self.project.export_layout))
        note = QLabel(self.tr('Explicit field types / array dimensions. Runtime bindings for this envelope: ') + roles + '.')
        note.setWordWrap(True)
        form.addWidget(note)
        editor = QPlainTextEdit(json.dumps(dict(structures=[asdict(t) for t in self.project.structures],
                                                bindings=self.project.bindings), indent=2))
        form.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addWidget(buttons)
        dialog.resize(740, 650)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            data = json.loads(editor.toPlainText())
            candidate = RayTracingGraphProject.from_bytes(self.project.to_bytes())
            candidate.structures = [RayStructure(t['name'], tuple(RayField(**f) for f in t['fields']))
                                    for t in data['structures']]
            candidate.bindings = data['bindings']
            candidate.validate()
        except (ValueError, KeyError, TypeError) as exc:
            self.status.setText(f'{type(exc).__name__}: {exc}' if not isinstance(exc, ValueError) else str(exc))
            return
        self.project = candidate
        self._refresh_exports()
        self._changed()

    def _generate(self):
        try:
            source = generate_raytracing_library(self.project)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.source.setPlainText(source)
        self.tabs.setCurrentWidget(self.source)
        self.status.setText(self.tr('Generated the entire library from graph declarations.'))

    def _compile(self):
        self.compiled = None
        self.apply_button.setEnabled(False)
        try:
            self.source.setPlainText(generate_raytracing_library(self.project))
            diagnostics = []
            compiled = build_raytracing_graph(self.project, diagnostics=diagnostics)
            validate_raytracing_replacement(self.library, compiled)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.compiled = compiled
        self.apply_button.setEnabled(True)
        self.status.setText('\n'.join((self.tr('Native DXC compilation / validation and selected-program ABI checks passed.'),
                                       *diagnostics)))

    def _apply(self):
        if self.compiled is None or not self.apply_button.isEnabled():
            return
        self.apply_requested.emit('compute', self.compiled, ((self.program.target_index, self.program.program_index),))

    def _open(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr('Open ray-tracing graph'), '', 'Ray graph (*.raygraph.json)')
        if not path:
            return
        try:
            if Path(path).stat().st_size > MAX_PROJECT_LENGTH:
                raise ShaderGraphError('Graph project is too large')
            self.project = RayTracingGraphProject.from_bytes(Path(path).read_bytes())
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        self._refresh_exports()
        self._changed()

    def _save_bytes(self, data, title, extension):
        path, _ = QFileDialog.getSaveFileName(self, title, '', extension)
        if path:
            write_files_atomically({path: data})

    def _save(self):
        if self.project is None:
            return
        try:
            self._save_bytes(self.project.to_bytes(), self.tr('Save ray-tracing graph'), self.tr('Ray graph (*.raygraph.json)'))
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def _export_library(self):
        if self.compiled is None:
            self.status.setText(self.tr('Compile the graph before exporting its library.'))
            return
        try:
            self._save_bytes(serialize_raytracing_library(self.compiled), self.tr('Export compiled DXR library'),
                             self.tr('DXR library (*.dxrlib)'))
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
