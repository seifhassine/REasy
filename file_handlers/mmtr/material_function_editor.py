from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QLabel, QLineEdit, QPushButton, QSpinBox, QTabWidget, QTreeWidgetItem, QVBoxLayout,
)

from file_handlers.sdf.shader_graph import (
    ShaderContract, ShaderGraph, ShaderGraphDiagnostic, ShaderGraphError,
    connection_compatible, node_pins,
)
from file_handlers.sdf.shader_graph_editor import add_palette_templates
from .material_editor import MmtrMaterialEditor
from .material_function import (
    MaterialFunctionAsset, MaterialFunctionPin, expand_material_functions,
)
from .material_project import MmtrMaterialProject
from file_handlers.sdf.shader_graph_math import NUMERIC_VALUE_TYPES, numeric_type_label, value_width
from file_handlers.sdf.shader_graph_buffer import structured_buffer_stride
from file_handlers.sdf.shader_graph_texture import (TEXTURE_OBJECT_TYPES, TEXTURE_SPATIAL_WIDTHS,
    TEXTURE_ELEMENT_TYPES, WRITABLE_TEXTURE_DIMENSIONS, texture_object_parts, texture_object_type, texture_object_sample_count)


PORT_TYPES = (
    (QT_TRANSLATE_NOOP("MaterialFunctionCanvas", 'Descriptor table'), 'resource_array<texture2d,0>'),
    (QT_TRANSLATE_NOOP("MaterialFunctionCanvas", "Byte-address buffer"), "byte_address_buffer"),
    (QT_TRANSLATE_NOOP("MaterialFunctionCanvas", 'Writable byte-address buffer'), 'rw_byte_address_buffer'),
    (QT_TRANSLATE_NOOP("MaterialFunctionCanvas", 'Execution'), 'execution'),
    (QT_TRANSLATE_NOOP("MaterialFunctionCanvas", 'Constant buffer'), 'constant_buffer<16>'),
    *((numeric_type_label(value_type), value_type) for value_type in NUMERIC_VALUE_TYPES),
    (QT_TRANSLATE_NOOP("MaterialFunctionCanvas", "Texture reference"), "texture"),
    *((kind.replace("texture", "Texture "), kind) for kind in TEXTURE_SPATIAL_WIDTHS),
    *(("Writable " + kind.replace('texture', 'Texture '), 'rw_' + kind) for kind in WRITABLE_TEXTURE_DIMENSIONS),
    (QT_TRANSLATE_NOOP("MaterialFunctionCanvas", "Sampler"), "sampler"),
    ('Depth comparison sampler', 'comparison_sampler'),
)


def new_material_function(value_type="float"):
    graph = ShaderGraph("material_function", "")
    source = graph.add_node("function_input", x=0, y=0,
        properties=dict(pin_id="input", display_name="Input", value_type=value_type))
    output = graph.add_node("function_output", x=440, y=0,
        properties=dict(pin_id="result", display_name="Result", value_type=value_type))
    graph.connect(source.node_id, "value", output.node_id, "value")
    return MaterialFunctionAsset(uuid4().hex, "New Function", "User Functions", "",
        (MaterialFunctionPin("input", "Input", value_type),),
        (MaterialFunctionPin("result", "Result", value_type),), graph)


def function_canvas(asset, dialog, owner, **options):
    canvas = MaterialFunctionCanvas(asset, dialog,
        source_profile=getattr(getattr(owner, "project", None), "source_profile", ""),
        renderer_contract=getattr(owner, "_renderer_contract", None) or getattr(owner, "_contract", None), **options)
    canvas.project_dir = getattr(owner, "project_dir", "")
    return canvas


def apply_buttons(dialog, layout):
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return buttons


class MaterialFunctionCanvas(MmtrMaterialEditor):
    interface_changed = Signal()

    def __init__(self, asset, parent=None, *, source_profile="", allow_defaults=True, renderer_contract=None):
        self._renderer_contract = renderer_contract
        super().__init__(parent)
        self.allow_defaults = allow_defaults
        self._original = asset
        self._defaults = {port.pin_id: port.default for port in asset.inputs}
        self._project = MmtrMaterialProject(asset.name, asset.source_profile or source_profile,
            deepcopy(asset.graph), [], [])
        self._contract = ShaderContract("material_function", 0, 0, (), (), (), ())
        self.title.setText(self.tr("Function"))
        self.material_name.setText(asset.name)
        self.profile_badge.setText(asset.source_profile or self.tr("Portable"))
        self.graph_menu.hide()
        self.variant_button.hide()
        self.advanced_menu.hide()
        self.add_parameter_action.setVisible(False)
        self.add_texture_action.setVisible(False)
        for output, label in ((False, self.tr("Function Input")), (True, self.tr("Function Output"))):
            action = QAction(label, self)
            action.triggered.connect(lambda _checked=False, output=output: self.add_port(output=output))
            self.add_menu.menu().addAction(action)
        self.palette_title.setText(self.tr("Function Nodes"))
        self._set_scene()
        self._populate_palette()
        self._populate_properties(None)
        self._refresh_readiness()
        self.setEnabled(True)
        self.fit_graph()

    def _can_edit_interface(self):
        return False

    def asset(self):
        def ports(kind):
            return tuple(MaterialFunctionPin(
                node.properties["pin_id"], node.properties["display_name"], node.properties["value_type"],
                self._defaults.get(node.properties["pin_id"]) is None,
                self._defaults.get(node.properties["pin_id"]) if kind == "function_input" else None,
            ) for node in self._project.graph.nodes if node.kind == kind)
        profile = self._function_profile()
        asset = replace(self._original, name=self.material_name.text().strip(), source_profile=profile,
            inputs=ports("function_input"), outputs=ports("function_output"), graph=deepcopy(self._project.graph))
        expand_material_functions(asset.graph, source_profile=self._project.source_profile or None, live_only=True)
        return asset

    def _function_profile(self):
        profiles = {str(node.properties.get("function", {}).get("source_profile", ""))
                    for node in self._project.graph.nodes if node.kind == "material_function"}
        profiles.add(self._original.source_profile)
        profiles.discard("")
        if len({profile.casefold() for profile in profiles}) > 1:
            raise ShaderGraphError("A function cannot combine different games' renderer-specific calculations.")
        return next(iter(profiles), "")

    def _diagnostics(self, *_args):
        try:
            self.asset()
            return ()
        except ShaderGraphError as exc:
            # shader_graph_editor rejects a wire on the ``function-cycle`` code.
            code = "function-cycle" if "cannot contain loops" in str(exc) else "function"
            return (ShaderGraphDiagnostic("error", code, str(exc)),)

    def _set_scene(self):
        self.scene.set_graph(self._project.graph, self._contract, validator=self._diagnostics)

    def _refresh_readiness(self):
        issues = self._diagnostics()
        if not issues:
            self.profile_badge.setText(self._function_profile() or self.tr("Portable"))
        self.status.setText(issues[0].message if issues else self.tr("Ready. Changes apply only to this function instance."))
        self.status.setStyleSheet("QLabel { color:#dc8585; }" if issues else "QLabel { color:#80c99b; }")

    def _populate_palette(self):
        super()._populate_palette()
        for index in reversed(range(self.palette.topLevelItemCount())):
            root = self.palette.topLevelItem(index)
            if root.text(0).startswith((self.tr("Parameters"), self.tr("Textures"))):
                self.palette.takeTopLevelItem(index)
        interface = QTreeWidgetItem([self.tr("Function Interface")])
        self.palette.insertTopLevelItem(0, interface)
        self._interface_node_ids = set()
        for node in self._project.graph.nodes:
            if node.kind in {"function_input", "function_output"}:
                self._interface_node_ids.add(node.node_id)
                item = QTreeWidgetItem([node.properties["display_name"]])
                item.setData(0, Qt.ItemDataRole.UserRole, ("select_function_port", node.node_id))
                interface.addChild(item)
        interface.setExpanded(True)
        sampling = QTreeWidgetItem([self.tr('Textures')])
        self.palette.addTopLevelItem(sampling)
        from file_handlers.sdf.shader_graph_texture import TEXTURE_TEMPLATES
        add_palette_templates(sampling, TEXTURE_TEMPLATES)
        buffers = QTreeWidgetItem([self.tr("Buffers")])
        item = QTreeWidgetItem([self.tr("Read Buffer Word")])
        item.setToolTip(0, self.tr("Read a 32-bit word at a byte offset. The address must be a multiple of four."))
        item.setData(0, Qt.ItemDataRole.UserRole, ("buffer_load", {}))
        buffers.addChild(item)
        for kind in sorted({node.properties.get("value_type", "") for node in self._project.graph.nodes if node.kind == "function_input"}):
            stride = structured_buffer_stride(kind)
            if stride is not None:
                item = QTreeWidgetItem([self.tr("Read Record Word ({stride} byte records)").format(stride=stride)])
                item.setData(0, Qt.ItemDataRole.UserRole, ("buffer_load", {"stride": stride, "byte_offset": 0}))
                buffers.addChild(item)
        self.palette.addTopLevelItem(buffers)
        from file_handlers.sdf.shader_graph_geometry import geometry_templates
        from file_handlers.sdf.shader_graph_mesh import mesh_templates
        from file_handlers.sdf.shader_graph_storage import storage_templates
        for title,templates in (
            (self.tr('Geometry Emission'),geometry_templates(self._renderer_contract)),
            (self.tr('Mesh Outputs'),mesh_templates(self._renderer_contract)),
            (self.tr('Shared Data'),storage_templates(self._renderer_contract,self.project.graph)),
        ):
            if not templates:
                continue
            geometry = QTreeWidgetItem([title])
            self.palette.addTopLevelItem(geometry)
            add_palette_templates(geometry, templates)

    def _graph_changed(self, *, rebuild_scene=False):
        super()._graph_changed(rebuild_scene=rebuild_scene)
        ports = [node for node in self._project.graph.nodes if node.kind in {"function_input", "function_output"}]
        if {node.node_id for node in ports} != self._interface_node_ids:
            self._defaults = {node.properties["pin_id"]: self._defaults.get(node.properties["pin_id"])
                              for node in ports if node.kind == "function_input"}
            self._populate_palette()
        interface = tuple((node.kind, node.properties['pin_id'], node.properties.get('display_name'), node.properties['value_type'])
                          for node in ports)
        if interface != getattr(self, '_published_interface', None):
            self._published_interface = interface
            self.interface_changed.emit()

    def _add_palette_node(self):
        item = self.palette.currentItem()
        payload = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if isinstance(payload, (tuple, list)) and payload[0] == "select_function_port":
            self.scene.rebuild(select_node=payload[1])
            self.view.centerOn(self.scene.node_items[payload[1]])
            return
        super()._add_palette_node()

    def _palette_item_selected(self, item, previous):
        super()._palette_item_selected(item, previous)
        payload = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if isinstance(payload, (tuple, list)) and payload[0] == "select_function_port":
            self.add_selected_button.setText(self.tr("Find in Graph"))
            self._populate_properties(self._project.graph.node(payload[1]))

    def add_port(self, *, output=False):
        identity = "port_" + uuid4().hex
        node = self._project.graph.add_node("function_output" if output else "function_input",
            x=600 if output else 0, y=len(self._project.graph.nodes) * 100,
            properties=dict(pin_id=identity, display_name="Result" if output else "Input", value_type="float"))
        self.scene.rebuild(select_node=node.node_id)
        self._populate_palette()
        self._graph_changed()
        return node

    def _populate_properties(self, node):
        if node is None or node.kind not in {"function_input", "function_output"}:
            super()._populate_properties(node)
            if node is None:
                self._clear_properties()
                self.properties.addRow(QLabel(self.tr("Select a node or add a named function input/output.")))
            return
        self._clear_properties()
        self.selection_title.setText(self.tr("Function Output") if node.kind == "function_output" else self.tr("Function Input"))
        name = QLineEdit(node.properties["display_name"])
        name.setMaxLength(128)
        name.editingFinished.connect(lambda: self._rename_port(node, name.text()))
        self.properties.addRow(self.tr("Name"), name)
        types = QComboBox()
        for label, value in PORT_TYPES:
            types.addItem(self.tr(label), value)
        current_type = node.properties['value_type']
        from file_handlers.sdf.shader_graph_resource import resource_array_parts, resource_array_type, is_writable_resource_type
        from file_handlers.sdf.shader_graph_editor import _resource_object_controls
        array = resource_array_parts(current_type)
        if array and current_type != 'resource_array<texture2d,0>':
            types.addItem(self.tr('Descriptor table ({count})').format(count=array[1] or self.tr('unbounded')), current_type)
        stride = structured_buffer_stride(current_type)
        from file_handlers.sdf.shader_graph_constant_buffer import constant_buffer_size
        constant_size = constant_buffer_size(current_type)
        if constant_size and current_type != 'constant_buffer<16>':
            types.addItem(self.tr('Constant buffer ({size} bytes)').format(size=constant_size),current_type)
        if stride is not None:
            types.addItem(self.tr("Structured buffer ({stride} byte records)").format(stride=stride), current_type)
        writable = is_writable_resource_type(current_type)
        texture = texture_object_parts(current_type) if current_type in TEXTURE_OBJECT_TYPES else None
        if writable and not stride and current_type not in dict(PORT_TYPES).values():
            types.addItem(current_type, current_type)
        sample_count = texture_object_sample_count(current_type) if texture else 0
        types.setCurrentIndex(types.findData(texture[0] if texture else current_type))
        types.currentIndexChanged.connect(lambda: self._change_port_type(node,
            texture_object_type(types.currentData(), texture[1], sample_count if 'ms' in str(types.currentData()) else 0)
            if texture and types.currentData() in TEXTURE_SPATIAL_WIDTHS else types.currentData(), types))
        self.properties.addRow(self.tr("Type"), types)
        if writable or constant_size:
            _resource_object_controls(self.properties, current_type,
                lambda value: self._change_port_type(node, value, None))
        if array:
            _resource_object_controls(self.properties, array[0],
                lambda value: self._change_port_type(node, resource_array_type(value, array[1]), None))
            count = QSpinBox()
            count.setRange(0, 1_048_576)
            count.setSpecialValueText(self.tr('Unbounded'))
            count.setValue(array[1])
            count.valueChanged.connect(lambda value: self._change_port_type(node, resource_array_type(array[0], value), None))
            self.properties.addRow(self.tr('Descriptor count'), count)
        if texture:
            element = QComboBox()
            for kind in TEXTURE_ELEMENT_TYPES:
                element.addItem(numeric_type_label(kind), kind)
            element.setCurrentIndex(element.findData(texture[1]))
            element.currentIndexChanged.connect(lambda: self._change_port_type(node, texture_object_type(texture[0], element.currentData(), sample_count), element))
            self.properties.addRow(self.tr('Texel value'), element)
            if 'ms' in texture[0]:
                count = QComboBox()
                for value in range(33):
                    count.addItem(str(value) if value else self.tr('Runtime'), value)
                count.setCurrentIndex(count.findData(sample_count))
                count.currentIndexChanged.connect(lambda: self._change_port_type(node,
                    texture_object_type(*texture, count.currentData()), count))
                self.properties.addRow(self.tr('Sample count'), count)
        if self.allow_defaults and node.kind == "function_input" and node.properties["value_type"] in NUMERIC_VALUE_TYPES:
            identity = node.properties["pin_id"]
            value = self._defaults.get(identity)
            default = QCheckBox(self.tr("Default when not connected"))
            default.setChecked(value is not None)
            default.toggled.connect(lambda enabled: self._toggle_default(node, enabled))
            self.properties.addRow(default)
            for index, component in enumerate(value or ()):
                kind = node.properties['value_type']
                if kind.startswith('bool'):
                    control = QCheckBox()
                    control.setChecked(component)
                    control.toggled.connect(lambda v, index=index: self._set_default(identity, index, v))
                    self.properties.addRow(self.tr('Default ') + 'XYZW'[index], control)
                    continue
                control = QDoubleSpinBox()
                control.setDecimals(6 if kind.startswith('float') else 0)
                control.setRange(*((-1e30, 1e30) if kind.startswith('float') else (0, 0xffffffff) if kind.startswith('uint') else (-0x80000000, 0x7fffffff)))
                control.setValue(component)
                control.valueChanged.connect(lambda v, index=index, kind=kind: self._set_default(identity, index, v if kind.startswith('float') else int(v)))
                self.properties.addRow(self.tr("Default ") + "XYZW"[index], control)
        delete = QPushButton(self.tr("Remove Port"))
        delete.clicked.connect(lambda: self._delete_node(node.node_id))
        self.properties.addRow(delete)

    def _rename_port(self, node, name):
        if not name.strip():
            self._populate_properties(node)
            return
        self._set_node_property(node, "display_name", name.strip())
        self._populate_palette()

    def _change_port_type(self, node, value_type, control):
        previous = node.properties["value_type"]
        if previous == value_type:
            return
        candidate = replace(node, properties={**node.properties, "value_type": value_type})
        for link in self._project.graph.links:
            if node.node_id not in (link.source_node, link.target_node):
                continue
            source = candidate if link.source_node == node.node_id else self._project.graph.node(link.source_node)
            target = candidate if link.target_node == node.node_id else self._project.graph.node(link.target_node)
            output = next(p for p in node_pins(source, None) if p.direction == "output" and p.name == link.source_pin)
            input_pin = next(p for p in node_pins(target, None) if p.direction == "input" and p.name == link.target_pin)
            if not connection_compatible(output.value_type, input_pin.value_type):
                if control is None:
                    self._populate_properties(node)
                    self._show_message(self.tr("Disconnect this port before changing to an incompatible type."))
                    return
                restore = previous
                if previous in TEXTURE_OBJECT_TYPES:
                    dimension, element = texture_object_parts(previous)
                    restore = dimension if control.findData(dimension) >= 0 else element if control.findData(element) >= 0 else texture_object_sample_count(previous)
                control.blockSignals(True)
                control.setCurrentIndex(control.findData(restore))
                control.blockSignals(False)
                self._show_message(self.tr("Disconnect this port before changing to an incompatible type."))
                return
        self._defaults.pop(node.properties["pin_id"], None)
        self._set_node_property(node, "value_type", value_type)
        self._populate_properties(node)

    def _toggle_default(self, node, enabled):
        kind = node.properties["value_type"]
        width = value_width(kind)
        zero = 0.0 if kind.startswith('float') else False if kind.startswith('bool') else 0
        self._defaults[node.properties["pin_id"]] = (zero,) * width if enabled else None
        self._populate_properties(node)
        self._graph_changed()

    def _set_default(self, identity, index, value):
        values = list(self._defaults[identity])
        values[index] = value
        self._defaults[identity] = tuple(values)
        self._graph_changed()


class MaterialFunctionDialog(QDialog):
    def __init__(self, asset, parent=None, *, validate_replacement=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Edit Function — ") + asset.name)
        self.resize(1350, 820)
        self.result_asset = None
        self.validate_replacement = validate_replacement
        root = QVBoxLayout(self)
        self.editor = function_canvas(asset, self, parent)
        if parent is not None:
            self.editor._populate_palette()
        root.addWidget(self.editor)
        self.buttons = apply_buttons(self, root)

    def accept(self):
        try:
            asset = self.editor.asset()
            if self.validate_replacement is not None:
                self.validate_replacement(asset)
        except (ShaderGraphError, ValueError) as exc:
            self.editor._show_message(str(exc))
            return
        self.result_asset = asset
        super().accept()


class MaterialBranchDialog(QDialog):
    def __init__(self, node, parent=None, *, validate_replacement=None):
        super().__init__(parent)
        from .material_branch import branch_arms
        self.setWindowTitle(self.tr("Edit Branch"))
        self.resize(1350, 860)
        self.result_properties = None
        self.validate_replacement = validate_replacement
        self.branch_name = node.properties.get("display_name", "Branch")
        root = QVBoxLayout(self)
        note = QLabel(self.tr("Only the selected arm executes. Named inputs and results are shared between tabs; calculations are independent."))
        note.setWordWrap(True)
        root.addWidget(note)
        self.tabs = QTabWidget()
        self.editors = []
        self._syncing = False
        for label, arm in zip((self.tr("When True"), self.tr("When False")), branch_arms(node)):
            editor = function_canvas(arm, self, parent, allow_defaults=False)
            self.tabs.addTab(editor, label)
            self.editors.append(editor)
        for editor in self.editors:
            editor.interface_changed.connect(lambda source=editor: self._sync_interface(source))
        root.addWidget(self.tabs)
        apply_buttons(self, root)

    def _sync_interface(self, source):
        if self._syncing:
            return
        from file_handlers.sdf.shader_graph import validate_node_update
        self._syncing = True
        try:
            target = self.editors[1 - self.editors.index(source)]
            ports = {node.properties["pin_id"]: node for node in source.project.graph.nodes
                     if node.kind in {"function_input", "function_output"}}
            other = {node.properties["pin_id"]: node for node in target.project.graph.nodes
                     if node.kind in {"function_input", "function_output"}}
            for identity in ports.keys() & other.keys():
                try:
                    validate_node_update(target.project.graph, other[identity], ports[identity].properties, target._contract)
                except ShaderGraphError:
                    ports[identity].properties = deepcopy(other[identity]).properties
                    source.scene.rebuild(select_node=ports[identity].node_id)
                    source._populate_properties(ports[identity])
                    source._show_message(self.tr("Disconnect incompatible wires in the other branch arm before changing this port type."))
                    return
            for identity, node in other.items():
                if identity not in ports:
                    target.project.graph.remove_node(node.node_id)
            for identity, node in ports.items():
                if identity in other:
                    other[identity].properties = deepcopy(node).properties
                else:
                    target.project.graph.add_node(node.kind, x=node.x, y=node.y, properties=deepcopy(node).properties)
            source._defaults = {identity: None for identity in ports}
            target._defaults = dict(source._defaults)
            target.scene.rebuild()
            target._populate_palette()
            target._refresh_readiness()
        finally:
            self._syncing = False

    def accept(self):
        from .material_branch import branch_properties
        try:
            properties = branch_properties(*(editor.asset() for editor in self.editors), name=self.branch_name)
            if self.validate_replacement:
                self.validate_replacement(properties)
        except (ShaderGraphError, ValueError) as exc:
            self.editors[self.tabs.currentIndex()]._show_message(str(exc))
            return
        self.result_properties = properties
        super().accept()
