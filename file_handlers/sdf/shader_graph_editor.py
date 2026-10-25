from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
import struct

from PySide6.QtCore import (
    QCoreApplication,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
lazy from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

lazy from file_handlers.mmtr import material_implementation, material_region_read
lazy from file_handlers.mmtr.material_branch import new_branch_properties
lazy from file_handlers.mmtr.material_function import material_function_from_node, replace_material_function
lazy from file_handlers.mmtr.material_function_editor import MaterialBranchDialog, MaterialFunctionDialog
lazy from file_handlers.mmtr.material_loop import new_loop_properties
lazy from file_handlers.mmtr.material_loop_editor import MaterialLoopDialog
lazy from file_handlers.mmtr.surface_meaning import shader_bytes
lazy from services.file_io import write_files_atomically
lazy from ui.icons import line_icon
lazy from ui.editor_widgets import delete_button, editor_strip, pane_header, search_field, strip_button, styled_label, toolbar_separator
lazy from ui.styles import current_colors, set_style_role

lazy from . import dxc
lazy from .aggregate_values import constant_value_fields
lazy from .contracts import shader_contract_for_program
lazy from .dxbc import parse_dxbc
lazy from .hlsl import HlslCompileError, hlsl_compiler_available
lazy from .hlsl_target import compile_hlsl_for_target
lazy from .models import stage_display_name
lazy from .shader_graph_buffer import BufferRead, structured_buffer_stride, structured_buffer_type, structured_word_bits
lazy from .shader_graph_code import code_pin_text, updated_code
lazy from .shader_graph_constant_buffer import constant_buffer_size, constant_buffer_type
lazy from .shader_graph_execution import ATOMIC_OPERATIONS, EXECUTION_TEMPLATES
lazy from .shader_graph_geometry import geometry_templates
lazy from .shader_graph_hull import hull_templates
lazy from .shader_graph_indexed_value import MAX_INDEXED_VALUES
lazy from .shader_graph_invocation import INVOCATION_INPUTS, invocation_stages
lazy from .shader_graph_layout import arrange_shader_graph
lazy from .shader_graph_mesh import mesh_templates
lazy from .shader_graph_precision import PRECISION_OPERATIONS
lazy from .shader_graph_resource import (RESOURCE_TEMPLATES, coherent_resource_element, is_writable_resource_type,
    resource_coherence, resource_properties)
lazy from .shader_graph_storage import storage_templates
lazy from .shader_graph_tessellation import domain_templates
lazy from .shader_graph_math import (BASIC_MATH_NODES, BASIC_MATH_KINDS, EXTRA_MATH_NODES, UNARY_FUNCTIONS, COMPARE_RELATIONS,
    TYPED_MATH_NODES, TYPED_MATH_CATEGORIES, NUMERIC_VALUE_TYPES, is_float_type, node_label, numeric_suffix,
    operation_value_types, numeric_type_label, value_width)
lazy from .shader_graph_texture import (TEXTURE_MODES, TEXTURE_TEMPLATES, TEXTURE_ELEMENT_TYPES, TEXTURE_LOAD_COORDINATES,
    TEXTURE_OBJECT_TYPES, TEXTURE_SPATIAL_WIDTHS, WRITABLE_TEXTURE_DIMENSIONS, WRITABLE_TEXTURE_OBJECT_TYPES,
    TextureOperation, texture_object_parts, texture_object_sample_count, texture_object_type, texture_property_update)
from .shader_graph import (
    SHADER_GRAPH_STAGES,
    MAX_SOURCE_LENGTH,
    TEXTURE_COORDINATE_TYPES,
    ShaderAuthoringProject,
    ShaderContract,
    ShaderGraph,
    ShaderGraphError,
    ShaderGraphLink,
    ShaderGraphNode,
    ShaderPin,
    validate_node_update,
    connection_adapter,
    connection_compatible,
    generate_graph_hlsl,
    new_shader_graph,
    node_pins,
    node_title,
    shader_graph_node_extension,
    validate_graph,
    validate_shader_contract,
)


_TYPE_COLORS = {
    "float": "#8bcf96",
    "float2": "#70c8c6",
    "float3": "#62a9dc",
    "float4": "#b58bd8",
    "int": "#d9a45d",
    "int2": "#d9a45d",
    "int3": "#d9a45d",
    "int4": "#d9a45d",
    "uint": "#d9be5d",
    "uint2": "#d9be5d",
    "uint3": "#d9be5d",
    "uint4": "#d9be5d",
}

_NODE_HUES = {
    "input": "#24465c",
    "output": "#3e3158",
    "parameter": "#4b3a2b",
    "texture_sample": "#2d4e45",
    "constant": "#3e3d50",
    "material_parameter": "#6a472d",
    "material_texture": "#286052",
    "material_surface": "#60477b",
    "material_function": "#4e477f",
    "texture_coordinates": "#28546a",
    "sample_texture": "#286052",
}
_DEFAULT_NODE_HUE = "#303b45"
_BOUNDARY_NODE_HUE = "#60477b"
_USER_FUNCTION_NODE_HUE = "#4d5e96"
_INTERFACE_ONLY_NODE_HUE = "#69572e"

_GRAPH_TOKENS = ("canvas_bg", "canvas_grid_minor", "canvas_grid_major", "node_bg", "node_header", "node_border",
                 "accent", "text_bright", "text_muted")
_graph_palette_cache: list = [None, None]

translate = QCoreApplication.translate


def graph_palette() -> dict:
    colors = current_colors()
    if _graph_palette_cache[0] != colors:
        _graph_palette_cache[:] = [colors, {name: QColor(colors[name]) for name in _GRAPH_TOKENS}]
    return _graph_palette_cache[1]


def draw_canvas_grid(painter: QPainter, rect: QRectF, minor: int = 24, major: int = 96) -> None:
    palette = graph_palette()
    painter.fillRect(rect, palette["canvas_bg"])
    for spacing, color in ((minor, palette["canvas_grid_minor"]), (major, palette["canvas_grid_major"])):
        painter.setPen(QPen(color, 1.0))
        left = int(rect.left()) - int(rect.left()) % spacing
        top = int(rect.top()) - int(rect.top()) % spacing
        for x in range(left, int(rect.right()) + spacing, spacing):
            painter.drawLine(x, rect.top(), x, rect.bottom())
        for y in range(top, int(rect.bottom()) + spacing, spacing):
            painter.drawLine(rect.left(), y, rect.right(), y)


def paint_node_card(painter: QPainter, rect: QRectF, header_height: float, *, header: QColor | None = None,
                    band: QColor | None = None, outline: QPen | None = None) -> None:
    palette = graph_palette()
    path = QPainterPath()
    path.addRoundedRect(rect, 4.0, 4.0)
    painter.fillPath(path, palette["node_bg"])
    painter.save()
    painter.setClipPath(path)
    painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), header_height),
                     palette["node_header"] if header is None else header)
    if band is not None:
        painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 3.0), band)
    painter.restore()
    border = QPen(palette["node_border"], 1.0)
    painter.setPen(border)
    painter.drawLine(QPointF(rect.left(), rect.top() + header_height), QPointF(rect.right(), rect.top() + header_height))
    painter.setPen(border if outline is None else outline)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)


def show_status(label: QLabel, text: str, tone: str = "") -> None:
    label.setText(text)
    set_style_role(label, tone=tone)


def _display_name(value: str) -> str:
    text = str(value or "").replace("_", " ").replace("-", " ")
    for prefix in ("VAR ", "TEX ", "CB "):
        if text.upper().startswith(prefix):
            text = text[len(prefix) :]
            break
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return " ".join(text.split()) or "Unnamed"


def _precision_properties(form, node, set_property):
    if node.kind not in PRECISION_OPERATIONS:
        return
    control = QCheckBox(translate("SdfShaderGraphEditor", 'Preserve calculation order'))
    control.setChecked(node.properties.get('precise', False))
    control.setToolTip(translate("SdfShaderGraphEditor", 'Prevent reassociation and contraction of this operation. This does not change the precision policy of connected calculations.'))
    control.toggled.connect(lambda value: set_property('precise', value))
    form.addRow(control)


def _wide_integer_control(value_type, value, update):
    control = QLineEdit(str(int(value)))
    control.setMaxLength(20)
    def commit():
        from .shader_graph import shader_value_literal
        try:
            number = int(control.text(), 10)
            shader_value_literal(value_type.rstrip('234'), [number])
        except ValueError:
            control.setStyleSheet(f"QLineEdit {{ border-color: {current_colors()['danger']}; }}")
            return
        control.setStyleSheet('')
        update(number)
    control.editingFinished.connect(commit)
    return control


_SEVERITY_TONES = {"error": "danger", "warning": "warning", "info": "info"}


def _diagnostic_item(label: str, severity: str, text: str) -> QTreeWidgetItem:
    item = QTreeWidgetItem([label, text])
    if severity in _SEVERITY_TONES:
        item.setForeground(0, QColor(current_colors()[_SEVERITY_TONES[severity]]))
    return item


def _sampling_resources(contract) -> tuple[list, list]:
    """Flag 0x2 marks a comparison sampler."""
    return ([item for item in contract.resources if item.sampleable],
            [item for item in contract.resources if item.input_type == "sampler" and not (item.flags & 0x2)])


def _constant_scalar(value_type: str) -> type:
    return float if is_float_type(value_type) else bool if value_type.startswith('bool') else int


def add_palette_templates(parent: QTreeWidgetItem, templates) -> None:
    for kind, label, properties in templates:
        QTreeWidgetItem(parent, [node_label(label)]).setData(0, Qt.ItemDataRole.UserRole, (kind, dict(properties)))


def filter_palette(tree: QTreeWidget, text: str, *, search_role=None, reveal_matching_groups: bool = True,
                   hide_empty_groups: bool = False) -> None:
    needle = str(text).strip().casefold()

    def matches(item: QTreeWidgetItem) -> bool:
        if not needle:
            return True
        terms = item.text(0) if search_role is None else f"{item.text(0)} {item.data(0, search_role) or ''}"
        return needle in terms.casefold()

    def update(item: QTreeWidgetItem, revealed: bool) -> bool:
        own_match = revealed or matches(item)
        reveal = reveal_matching_groups and own_match
        child_visible = any([update(item.child(index), reveal) for index in range(item.childCount())])
        visible = own_match or child_visible
        if (hide_empty_groups and not needle and not item.childCount()
                and item.data(0, Qt.ItemDataRole.UserRole) is None):
            visible = False
        item.setHidden(not visible)
        if needle and visible and item.childCount() and not revealed:
            item.setExpanded(True)
        return visible

    for root_index in range(tree.topLevelItemCount()):
        update(tree.topLevelItem(root_index), False)


def _numeric_properties(form, node, set_property):
    for key in (('source_type', 'value_type') if node.kind in {'convert', 'reinterpret'} else ('value_type',)):
        control = QComboBox()
        current = node.properties.get(key, 'float' if key == 'source_type' else 'uint' if node.kind in TYPED_MATH_NODES else 'float4')
        choices = operation_value_types(node.kind)
        if node.kind in {'convert', 'reinterpret'}:
            choices = tuple(value for value in choices if numeric_suffix(value) == numeric_suffix(current))
        for value in choices:
            control.addItem(translate("SdfShaderGraphEditor", numeric_type_label(value)), value)
        control.setCurrentIndex(control.findData(current))
        control.currentIndexChanged.connect(lambda _index, key=key, control=control: set_property(key, control.currentData()))
        form.addRow(translate("SdfShaderGraphEditor", 'Input type') if key == 'source_type'
                    else translate("SdfShaderGraphEditor", 'Value type'), control)
    if node.kind in {'extract_bits', 'insert_bits'}:
        for key, label, default, maximum in (('width', 'Bit count', 1, 32), ('offset', 'Start bit', 0, 31)):
            control = QSpinBox()
            control.setRange(0, maximum)
            control.setValue(node.properties.get(key, default))
            control.valueChanged.connect(lambda value, key=key: set_property(key, value))
            form.addRow(translate("SdfShaderGraphEditor", label), control)
    if node.kind in {'indexed_select', 'array_read'}:
        count = QSpinBox()
        count.setRange(1, MAX_INDEXED_VALUES)
        count.setValue(node.properties.get('count', 4))
        count.valueChanged.connect(lambda value: set_property('count', value))
        form.addRow(translate("SdfShaderGraphEditor", 'Number of choices'), count)
    if node.kind == 'indexed_select':
        _default_policy_properties(form, node, set_property)


def _table_properties(form, node, set_property):
    domain, words = node.properties['value_type'], list(node.properties['words'])
    decode = {'float': lambda word: struct.unpack('<f', struct.pack('<I', word))[0],
              'int': lambda word: word - (1 << 32) if word & 0x80000000 else word, 'uint': int}[domain]
    encode = {'float': lambda value: struct.unpack('<I', struct.pack('<f', value))[0],
              'int': lambda value: int(value) & 0xFFFFFFFF, 'uint': int}[domain]

    def change(index, value):
        updated = list(words)
        updated[index] = encode(value)
        set_property('words', updated)

    for index, word in enumerate(words):
        editor = QDoubleSpinBox()
        editor.setDecimals(6 if domain == 'float' else 0)
        editor.setRange(*{'float': (-1.0e9, 1.0e9), 'int': (-0x80000000, 0x7FFFFFFF), 'uint': (0, 0xFFFFFFFF)}[domain])
        editor.setValue(decode(word))
        editor.valueChanged.connect(lambda value, index=index: change(index, value))
        form.addRow(translate("SdfShaderGraphEditor", "Entry {index}").format(index=index), editor)
    _default_policy_properties(form, node, set_property)


def _default_policy_properties(form, node, set_property):
    default = QCheckBox(translate("SdfShaderGraphEditor", 'Define an out-of-range result'))
    default.setChecked(node.properties.get('has_default', True))
    default.toggled.connect(lambda enabled: set_property('has_default', enabled))
    form.addRow(default)
    if not default.isChecked():
        note = QLabel(translate("SdfShaderGraphEditor", 'No default is assigned. Results outside the listed choices are unspecified. Enable a default and connect its value to define them.'))
        note.setWordWrap(True)
        form.addRow(note)


def _execution_properties(form, node, update):
    if node.kind in {'memory_load', 'memory_store', 'memory_atomic'}:
        stride = QSpinBox()
        stride.setRange(0, 2048)
        stride.setSingleStep(4)
        stride.setSpecialValueText(translate("SdfShaderGraphEditor", 'Raw byte addressing'))
        stride.setValue(node.properties.get('stride', 0))
        def change_stride(value):
            if value % 4:
                stride.setValue(node.properties.get('stride', 0))
                return
            update('stride', value)
        stride.valueChanged.connect(change_stride)
        form.addRow(translate("SdfShaderGraphEditor", 'Record stride (bytes)'), stride)
    if node.kind in {'image_load', 'image_store', 'image_atomic'}:
        dimensions = QComboBox()
        for dimension in WRITABLE_TEXTURE_DIMENSIONS:
            dimensions.addItem(translate("SdfShaderGraphEditor", dimension.replace('texture', 'Texture ')), dimension)
        dimensions.setCurrentIndex(dimensions.findData(node.properties.get('dimension', 'texture2d')))
        dimensions.currentIndexChanged.connect(lambda: update('dimension', dimensions.currentData()))
        form.addRow(translate("SdfShaderGraphEditor", 'Image dimension'), dimensions)
        elements = QComboBox()
        for domain in ('uint', 'int', 'uint64_t', 'int64_t') if node.kind == 'image_atomic' else TEXTURE_ELEMENT_TYPES:
            elements.addItem(numeric_type_label(domain), domain)
        elements.setCurrentIndex(elements.findData(node.properties.get('element_type', 'uint' if node.kind == 'image_atomic' else 'float4')))
        elements.currentIndexChanged.connect(lambda: update('element_type', elements.currentData()))
        form.addRow(translate("SdfShaderGraphEditor", 'Texel value'), elements)
    if node.kind in {'image_load', 'image_store', 'image_atomic', 'memory_load', 'memory_store', 'memory_atomic'}:
        coherence = QCheckBox(translate("SdfShaderGraphEditor", 'Globally coherent resource'))
        coherence.setChecked(node.properties.get('globally_coherent', False))
        coherence.setToolTip(translate("SdfShaderGraphEditor", "Must match the connected resource. Lets device "
                                                               "barriers make writes visible to other groups; it "
                                                               "does not synchronize groups."))
        coherence.toggled.connect(lambda value: update('globally_coherent', value))
        form.addRow(coherence)
    if node.kind in {'capture_value', 'memory_atomic', 'image_atomic'}:
        types = QComboBox()
        choices = (NUMERIC_VALUE_TYPES if node.kind == 'capture_value'
                   else ('uint', 'int', 'uint64_t', 'int64_t') if node.kind == 'memory_atomic'
                   else ('uint64_t', 'int64_t') if node.properties.get('element_type', 'uint').endswith('64_t')
                   else ('uint', 'int'))
        for domain in choices:
            types.addItem(translate("SdfShaderGraphEditor", numeric_type_label(domain)), domain)
        types.setCurrentIndex(types.findData(node.properties.get('value_type', node.properties.get('element_type','uint') if node.kind == 'image_atomic' else 'uint' if node.kind == 'memory_atomic' else 'float')))
        types.currentIndexChanged.connect(lambda: update('value_type', types.currentData()))
        form.addRow(translate("SdfShaderGraphEditor", 'Value type'), types)
    if node.kind in {'memory_atomic', 'image_atomic'}:
        operations = QComboBox()
        for operation in ATOMIC_OPERATIONS:
            operations.addItem(translate("SdfShaderGraphEditor", operation.replace('_', ' ').title()), operation)
        operations.setCurrentIndex(operations.findData(node.properties.get('operation', 'add')))
        operations.currentIndexChanged.connect(lambda: update('operation', operations.currentData()))
        form.addRow(translate("SdfShaderGraphEditor", 'Operation'), operations)
    elif node.kind == 'memory_barrier':
        scope = QComboBox()
        for value, label in (('device', 'Device memory'), ('group', 'Group-shared memory'), ('all', 'Both memory scopes')):
            scope.addItem(translate("SdfShaderGraphEditor", label), value)
        scope.setCurrentIndex(scope.findData(node.properties.get('scope', 'device')))
        scope.currentIndexChanged.connect(lambda: update('scope', scope.currentData()))
        form.addRow(translate("SdfShaderGraphEditor", 'Memory scope'), scope)
        sync = QCheckBox(translate("SdfShaderGraphEditor", 'Wait for all threads in the group'))
        sync.setChecked(node.properties.get('synchronize', False))
        sync.toggled.connect(lambda value: update('synchronize', value))
        form.addRow(sync)
    note = QLabel(translate("SdfShaderGraphEditor", 'Execution connections determine order. Image coordinates identify a texel in the bound mip; array textures also require a layer.') if node.kind.startswith('image_')
                  else translate("SdfShaderGraphEditor", 'Execution connections determine order. A buffer word address is in bytes and must be aligned to four bytes.'))
    note.setWordWrap(True)
    form.addRow(note)


def _invocation_properties(form, node, update, *, stage):
    selection = QComboBox()
    for meaning, (title, _, _) in INVOCATION_INPUTS.items():
        if stage not in invocation_stages(meaning):
            continue
        selection.addItem(node_label(title), meaning)
    selection.setCurrentIndex(selection.findData(node.properties.get('meaning')))
    selection.currentIndexChanged.connect(lambda: update('meaning', selection.currentData()))
    form.addRow(translate("SdfShaderGraphEditor", 'Invocation value'), selection)


def _structured_word_control(form, stride, bits, update):
    width = QComboBox()
    for value in (32, 64):
        if stride % (value // 8) == 0:
            width.addItem(translate("SdfShaderGraphEditor", '{bits}-bit words').format(bits=value), value)
    width.setCurrentIndex(width.findData(bits))
    width.currentIndexChanged.connect(lambda: update(width.currentData()))
    form.addRow(translate("SdfShaderGraphEditor", 'Storage word width'), width)


def _resource_object_controls(form, current, update, *, samplers=True):
    coherent = coherent_resource_element(current)
    unqualified_update = update
    current = coherent or current
    if is_writable_resource_type(current):
        coherence = QCheckBox(translate("SdfShaderGraphEditor", 'Globally coherent resource'))
        coherence.setChecked(coherent is not None)
        coherence.toggled.connect(lambda enabled: unqualified_update(resource_coherence(current, enabled)))
        form.addRow(coherence)
    update = lambda value: unqualified_update(resource_coherence(value, coherent is not None and is_writable_resource_type(value)))
    writable = current in WRITABLE_TEXTURE_OBJECT_TYPES
    texture = texture_object_parts(current[3:] if writable else current) if writable or current in TEXTURE_OBJECT_TYPES else None
    stride = structured_buffer_stride(current)
    word_bits = structured_word_bits(current) if stride else 32
    constant_size = constant_buffer_size(current)
    kind = QComboBox()
    for key in (*TEXTURE_SPATIAL_WIDTHS, *('rw_' + dimension for dimension in WRITABLE_TEXTURE_DIMENSIONS), 'byte_address_buffer', 'rw_byte_address_buffer', 'structured_buffer', 'rw_structured_buffer', 'constant_buffer', 'sampler', 'comparison_sampler'):
        if not samplers and key in {'sampler', 'comparison_sampler'}:
            continue
        kind.addItem(translate("SdfShaderGraphEditor", key.replace('_', ' ').replace('texture', 'Texture ').title()), key)
    kind.setCurrentIndex(kind.findData(('rw_' if writable else '') + texture[0] if texture else ('rw_' if current.startswith('rw_') else '') + 'structured_buffer' if stride else 'constant_buffer' if constant_size else current))
    def change_kind():
        selected = kind.currentData()
        if selected == 'constant_buffer':
            update(constant_buffer_type(constant_size or 16))
            return
        if selected.startswith('rw_texture'):
            update('rw_' + texture_object_type(selected[3:], texture[1] if texture else 'float4'))
            return
        update(texture_object_type(selected, texture[1] if texture else 'float4',
                   texture_object_sample_count(current) if texture and not writable and 'ms' in selected else 0) if selected in TEXTURE_SPATIAL_WIDTHS
               else structured_buffer_type(stride or 16, writable=selected.startswith('rw_'), word_bits=word_bits) if selected in {'structured_buffer', 'rw_structured_buffer'} else selected)
    kind.currentIndexChanged.connect(change_kind)
    form.addRow(translate("SdfShaderGraphEditor", 'Resource type'), kind)
    if texture:
        count = 0 if writable else texture_object_sample_count(current)
        element = QComboBox()
        for value in TEXTURE_ELEMENT_TYPES:
            element.addItem(numeric_type_label(value), value)
        element.setCurrentIndex(element.findData(texture[1]))
        element.currentIndexChanged.connect(lambda: update(('rw_' if writable else '') + texture_object_type(texture[0], element.currentData(), count)))
        form.addRow(translate("SdfShaderGraphEditor", 'Texel value'), element)
        if 'ms' in texture[0]:
            samples = QSpinBox()
            samples.setRange(0, 32)
            samples.setSpecialValueText(translate("SdfShaderGraphEditor", 'Runtime'))
            samples.setValue(count)
            samples.valueChanged.connect(lambda value: update(texture_object_type(*texture, value)))
            form.addRow(translate("SdfShaderGraphEditor", 'Sample count'), samples)
    elif constant_size:
        _constant_size_control(form,constant_size,lambda size:update(constant_buffer_type(size)))
    elif stride:
        _structured_word_control(form, stride, word_bits,
            lambda bits: update(structured_buffer_type(stride, writable=current.startswith('rw_'), word_bits=bits)))
        size = QSpinBox()
        size.setRange(word_bits // 8, 2048)
        size.setSingleStep(word_bits // 8)
        size.setValue(stride)
        def change_stride(value):
            if value % (word_bits // 8):
                size.blockSignals(True)
                size.setValue(stride)
                size.blockSignals(False)
                size.setToolTip(translate("SdfShaderGraphEditor", 'Record stride must be a multiple of the selected word size.'))
                return
            update(structured_buffer_type(value, writable=current.startswith('rw_'), word_bits=word_bits))
        size.valueChanged.connect(change_stride)
        form.addRow(translate("SdfShaderGraphEditor", 'Record stride'), size)


def _resource_properties(form, node, update):
    if node.kind == 'resource_dimensions':
        _resource_object_controls(form, node.properties.get('resource_type', 'texture2d'), lambda value: update('resource_type', value), samplers=False)
        return
    element, array = resource_properties(node)
    _resource_object_controls(form, element, lambda value: update('element_type', value))
    if array:
        count = QSpinBox()
        count.setRange(0, 1_048_576)
        count.setSpecialValueText(translate("SdfShaderGraphEditor", 'Unbounded'))
        count.setValue(node.properties['count'])
        count.valueChanged.connect(lambda value: update('count', value))
        form.addRow(translate("SdfShaderGraphEditor", 'Descriptor count'), count)
    policy = QCheckBox(translate("SdfShaderGraphEditor", 'Index may differ between lanes'))
    policy.setChecked(node.properties.get('non_uniform', False))
    policy.toggled.connect(lambda value: update('non_uniform', value))
    form.addRow(policy)


def _constant_size_control(form, current, update):
    size = QSpinBox()
    size.setRange(4,65536)
    size.setSingleStep(4)
    size.setValue(current)
    def change(value):
        if value % 4:
            size.blockSignals(True)
            size.setValue(current)
            size.blockSignals(False)
            return
        update(value)
    size.valueChanged.connect(change)
    form.addRow(translate("SdfShaderGraphEditor", 'Declared byte extent'),size)


def _buffer_properties(form, node, update):
    if node.kind == 'constant_buffer_load':
        _constant_size_control(form,node.properties['byte_size'],lambda value:update('byte_size',value))
        note = QLabel(translate("SdfShaderGraphEditor", 'Read a four-byte word inside the declared constant buffer. Connect its byte address and reinterpret the word for float or signed values.'))
        note.setWordWrap(True)
        form.addRow(note)
        return
    operation = BufferRead.from_properties(node.properties)
    if operation.stride:
        form.addRow(translate("SdfShaderGraphEditor", "Record stride"), QLabel(translate("SdfShaderGraphEditor", "{stride} bytes (resource contract)").format(stride=operation.stride)))
        dynamic = QCheckBox(translate("SdfShaderGraphEditor", 'Calculate the word offset'))
        dynamic.setChecked(operation.dynamic_offset)
        def change_addressing(enabled):
            if enabled and node.properties.get('byte_offset', 0):
                update('byte_offset', 0)
            update('dynamic_offset', enabled)
        dynamic.toggled.connect(change_addressing)
        form.addRow(dynamic)
        if operation.dynamic_offset:
            form.addRow(QLabel(translate("SdfShaderGraphEditor", 'Connect a four-byte-aligned byte offset within the record.')))
            return
        offset = QSpinBox()
        offset.setRange(0, operation.stride - 4)
        offset.setSingleStep(4)
        offset.setValue(operation.byte_offset)
        offset.valueChanged.connect(lambda value: update("byte_offset", value))
        form.addRow(translate("SdfShaderGraphEditor", "Word byte offset"), offset)
    else:
        note = QLabel(translate("SdfShaderGraphEditor", "Reads one 32-bit word. Supply a byte offset aligned to four bytes; use Reinterpret Bits for float/signed values."))
        note.setWordWrap(True)
        form.addRow(note)


def _code_properties(form, node, apply):
    inputs = QLineEdit(code_pin_text(node.properties, "inputs"))
    outputs = QLineEdit(code_pin_text(node.properties, "outputs"))
    for field in (inputs, outputs):
        field.setPlaceholderText(translate("SdfShaderGraphEditor", "float3 colour, float2 uv"))
    body = QPlainTextEdit(str(node.properties.get("body", "")))
    body.setMinimumHeight(180)
    body.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    set_style_role(body, mono=True)
    note = QLabel(translate("SdfShaderGraphEditor",
                            "Assign every output in the body; each starts at zero. The body reads only its inputs."))
    note.setWordWrap(True)
    set_style_role(note, tone="muted")
    button = QPushButton(translate("SdfShaderGraphEditor", "Apply Code"))
    set_style_role(button, variant="primary")
    button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    button.clicked.connect(lambda: apply(inputs.text(), outputs.text(), body.toPlainText()))
    form.addRow(translate("SdfShaderGraphEditor", "Inputs"), inputs)
    form.addRow(translate("SdfShaderGraphEditor", "Outputs"), outputs)
    form.addRow(note)
    form.addRow(body)
    form.addRow(button)


def _texture_properties(form, node, set_property):
    operation = TextureOperation.from_properties(node.kind, node.properties)
    dimensions = TEXTURE_LOAD_COORDINATES if node.kind == 'texture_load' else TEXTURE_COORDINATE_TYPES
    dimension = QComboBox()
    for kind in dimensions:
        dimension.addItem(kind.replace('texture', 'Texture '), kind)
    dimension.setCurrentIndex(dimension.findData(operation.dimension))
    dimension.currentIndexChanged.connect(lambda: set_property('dimension', dimension.currentData()))
    form.addRow(translate("SdfShaderGraphEditor", 'Texture type'), dimension)
    element = QComboBox()
    for kind in TEXTURE_ELEMENT_TYPES:
        if node.kind == 'texture_load' or kind.startswith('float') or operation.gather and kind not in {'int64_t','uint64_t'} and not operation.comparison:
            element.addItem(numeric_type_label(kind), kind)
    element.setCurrentIndex(element.findData(operation.element))
    element.currentIndexChanged.connect(lambda: set_property('element_type', element.currentData()))
    form.addRow(translate("SdfShaderGraphEditor", 'Texel value'), element)
    if 'ms' in operation.dimension:
        count = QSpinBox()
        count.setRange(0, 32)
        count.setSpecialValueText(translate("SdfShaderGraphEditor", 'Runtime'))
        count.setValue(operation.sample_count)
        count.valueChanged.connect(lambda value: set_property('sample_count', value))
        form.addRow(translate("SdfShaderGraphEditor", 'Sample count'), count)
    if node.kind == 'sample_texture':
        mode = QComboBox()
        for kind, label in TEXTURE_MODES.items():
            try:
                TextureOperation.from_properties(node.kind, {**node.properties,'mode':kind,'channel':0,'dynamic_offset':False,'minimum_lod':False})
            except ValueError:
                continue
            mode.addItem(translate("SdfShaderGraphEditor", label), kind)
        mode.setCurrentIndex(mode.findData(operation.mode))
        mode.currentIndexChanged.connect(lambda: set_property('mode', mode.currentData()))
        form.addRow(translate("SdfShaderGraphEditor", 'Sampling'), mode)
        if operation.mode in {'implicit', 'bias', 'gradients', 'compare'}:
            clamp = QCheckBox(translate("SdfShaderGraphEditor", 'Minimum mip clamp'))
            clamp.setChecked(operation.minimum_lod)
            clamp.toggled.connect(lambda enabled: set_property('minimum_lod', enabled))
            form.addRow(clamp)
        if operation.gather:
            channel = QComboBox()
            for index, label in enumerate(('Red','Green','Blue','Alpha')):
                channel.addItem(translate("SdfShaderGraphEditor", label),index)
            channel.setCurrentIndex(operation.channel)
            channel.currentIndexChanged.connect(lambda: set_property('channel',channel.currentData()))
            form.addRow(translate("SdfShaderGraphEditor", 'Gather channel'),channel)
            note = QLabel(translate("SdfShaderGraphEditor", 'Four neighboring texels at the finest mip: lower-left, lower-right, upper-right, upper-left.'))
            note.setWordWrap(True)
            form.addRow(note)
            if 'cube' not in operation.dimension and not operation.offset:
                dynamic = QCheckBox(translate("SdfShaderGraphEditor", 'Connect texel offset'))
                dynamic.setChecked(operation.dynamic_offset)
                dynamic.toggled.connect(lambda enabled: set_property('dynamic_offset',enabled))
                form.addRow(dynamic)
    if 'cube' not in operation.dimension and not operation.dynamic_offset:
        enabled = QCheckBox(translate("SdfShaderGraphEditor", 'Texel offset'))
        enabled.setChecked(bool(operation.offset))
        enabled.toggled.connect(lambda active: set_property('offset', [0] * TEXTURE_SPATIAL_WIDTHS[operation.dimension] if active else []))
        form.addRow(enabled)
        for index, value in enumerate(operation.offset):
            control = QSpinBox()
            control.setRange(*((-32,31) if operation.gather else (-8,7)))
            control.setValue(value)
            control.valueChanged.connect(lambda value, index=index: set_property('offset',
                [value if axis == index else previous for axis, previous in enumerate(node.properties['offset'])]))
            form.addRow(translate("SdfShaderGraphEditor", 'Offset ') + 'XYZ'[index], control)


def _comparison_properties(form, node, set_property):
    _numeric_properties(form, node, set_property)
    relation = QComboBox()
    for key in COMPARE_RELATIONS:
        relation.addItem(translate("SdfShaderGraphEditor", key.replace("_", " ").title()), key)
    relation.setCurrentIndex(relation.findData(node.properties.get("relation", "less")))
    relation.currentIndexChanged.connect(lambda _index: set_property("relation", relation.currentData()))
    nan_result = QCheckBox(translate("SdfShaderGraphEditor", "True if either input is NaN"))
    nan_result.setChecked(node.properties.get("nan_result", False))
    nan_result.toggled.connect(lambda value: set_property("nan_result", value))
    form.addRow(translate("SdfShaderGraphEditor", "Comparison"), relation)
    if is_float_type(node.properties.get('value_type', 'float')):
        form.addRow(nan_result)


def _source_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _friendly_pin_type(value_type: str) -> str:
    return {
        "float": "Scalar",
        "float2": "Vector 2",
        "float3": "Vector 3",
        "float4": "Vector 4",
        "int": "Integer",
        "uint": "Unsigned integer",
    }.get(str(value_type), str(value_type).replace("_", " ").title())


def connection_path(start: QPointF, end: QPointF, min_distance: float) -> QPainterPath:
    distance = max(min_distance, abs(end.x() - start.x()) * 0.48)
    path = QPainterPath(start)
    path.cubicTo(
        QPointF(start.x() + distance, start.y()),
        QPointF(end.x() - distance, end.y()),
        end,
    )
    return path


class _ShaderPortItem(QGraphicsEllipseItem):
    SIZE = 10.0

    def __init__(self, node_item: "_ShaderNodeItem", pin: ShaderPin):
        super().__init__(-self.SIZE / 2, -self.SIZE / 2, self.SIZE, self.SIZE, node_item)
        self.node_item = node_item
        self.pin = pin
        color = QColor(_TYPE_COLORS.get(pin.value_type, "#a7b0b7"))
        self.setBrush(QBrush(color))
        self.setPen(QPen(graph_palette()["node_bg"], 1.5))
        self.setAcceptHoverEvents(True)
        self.setToolTip(f"{pin.label} · {_friendly_pin_type(pin.value_type)}")
        self.setZValue(4)

    def hoverEnterEvent(self, event) -> None:
        self.setScale(1.35)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.setScale(1.0)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:
        scene = self.scene()
        if isinstance(scene, _ShaderGraphScene) and event.button() == Qt.MouseButton.LeftButton:
            if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                scene.disconnect_port(self)
                event.accept()
                return
            self._pressed_at = event.scenePos()
            scene.port_clicked(self)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        scene = self.scene()
        if isinstance(scene, _ShaderGraphScene) and scene.pending_source is self:
            scene.update_pending_wire(event.scenePos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        scene = self.scene()
        if isinstance(scene, _ShaderGraphScene) and scene.pending_source is self:
            distance = event.scenePos() - getattr(self, "_pressed_at", event.scenePos())
            if abs(distance.x()) + abs(distance.y()) > 5:
                target = next((item for item in scene.items(event.scenePos())
                               if isinstance(item, _ShaderPortItem) and item.pin.direction == "input"), None)
                if target is not None:
                    scene.port_clicked(target)
                else:
                    scene.cancel_connection()
                event.accept()
                return
        super().mouseReleaseEvent(event)


class _ShaderNodeItem(QGraphicsObject):
    WIDTH = 300.0
    HEADER = 30.0
    ROW = 25.0

    def __init__(self, node: ShaderGraphNode, contract: ShaderContract, pin_filter=None):
        super().__init__()
        self.node = node
        pins = node_pins(node, contract)
        if pin_filter is not None:
            pins = pin_filter(node, pins)
        self.inputs = tuple(item for item in pins if item.direction == "input")
        self.outputs = tuple(item for item in pins if item.direction == "output")
        metrics = QFontMetricsF(QApplication.font())
        self.caption = node_title(node)
        left = max((metrics.horizontalAdvance(pin.label) for pin in self.inputs), default=0)
        right = max((metrics.horizontalAdvance(pin.label) for pin in self.outputs), default=0)
        self.WIDTH = max(180.0, min(360.0, max(left + right + 55, metrics.horizontalAdvance(self.caption) + 32)))
        self.setToolTip(self.caption)
        self.height = self.HEADER + max(1, len(self.inputs), len(self.outputs)) * self.ROW + 12.0
        self.port_items: dict[tuple[str, str], _ShaderPortItem] = {}
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self.setZValue(2)
        self._build_ports()

    def _build_ports(self) -> None:
        for index, pin in enumerate(self.inputs):
            port = _ShaderPortItem(self, pin)
            port.setPos(0.0, self.HEADER + (index + 0.65) * self.ROW)
            self.port_items[("input", pin.name)] = port
        for index, pin in enumerate(self.outputs):
            port = _ShaderPortItem(self, pin)
            port.setPos(self.WIDTH, self.HEADER + (index + 0.65) * self.ROW)
            self.port_items[("output", pin.name)] = port

    def boundingRect(self) -> QRectF:
        return QRectF(-8.0, -3.0, self.WIDTH + 16.0, self.height + 6.0)

    def _hue(self) -> str:
        properties = self.node.properties
        if bool(properties.get("interface_only", False)):
            return _INTERFACE_ONLY_NODE_HUE
        if properties.get("authoring_origin") == "user_function":
            return _USER_FUNCTION_NODE_HUE
        if properties.get("semantic_reconstruction_boundary"):
            return _BOUNDARY_NODE_HUE
        return _NODE_HUES.get(self.node.kind, _DEFAULT_NODE_HUE)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        palette = graph_palette()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        hue = QColor(self._hue())
        header = palette["node_header"]
        tint = QColor.fromRgbF(*(base + (accent - base) * 0.45 for base, accent in (
            (header.redF(), hue.redF()), (header.greenF(), hue.greenF()), (header.blueF(), hue.blueF()))))
        paint_node_card(painter, QRectF(0.0, 0.0, self.WIDTH, self.height), self.HEADER, header=tint,
                        band=hue.lighter(190), outline=QPen(palette["accent"], 2.0) if self.isSelected() else None)
        painter.setPen(palette["text_bright"])
        title_font = QFont(painter.font())
        title_font.setBold(True)
        painter.setFont(title_font)
        title = self.caption
        if bool(self.node.properties.get("interface_only", False)):
            title = self.tr("{title} · connection not recovered").format(
                title=title
            )
        painter.drawText(
            QRectF(10.0, 3.0, self.WIDTH - 20.0, self.HEADER - 3.0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, int(self.WIDTH - 20)),
        )
        painter.setFont(QFont())
        painter.setPen(palette["text_muted"])
        for index, pin in enumerate(self.inputs):
            top = self.HEADER + index * self.ROW
            painter.drawText(
                QRectF(13.0, top, (self.WIDTH * 0.6 if self.outputs else self.WIDTH) - 26.0, self.ROW),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                painter.fontMetrics().elidedText(pin.label, Qt.TextElideMode.ElideRight,
                    int((self.WIDTH * 0.6 if self.outputs else self.WIDTH) - 26.0)),
            )
        for index, pin in enumerate(self.outputs):
            top = self.HEADER + index * self.ROW
            painter.drawText(
                QRectF(self.WIDTH * 0.6 if self.inputs else 13.0, top,
                    (self.WIDTH * 0.4 - 13.0) if self.inputs else self.WIDTH - 26.0, self.ROW),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                painter.fontMetrics().elidedText(pin.label, Qt.TextElideMode.ElideLeft,
                    int((self.WIDTH * 0.4 - 13.0) if self.inputs else self.WIDTH - 26.0)),
            )

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            scene = self.scene()
            if isinstance(scene, _ShaderGraphScene):
                scene.node_moved(self)
        return result


class _ShaderConnectionItem(QGraphicsPathItem):
    def __init__(
        self,
        link: ShaderGraphLink,
        source: _ShaderPortItem,
        target: _ShaderPortItem,
    ):
        super().__init__()
        self.link = link
        self.source = source
        self.target = target
        color = QColor(_TYPE_COLORS.get(source.pin.value_type, "#a7b0b7"))
        self.setPen(QPen(color, 2.4))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(0)
        self.update_path()

    def update_path(self) -> None:
        self.setPath(connection_path(self.source.scenePos(), self.target.scenePos(), 70.0))

    def paint(self, painter: QPainter, option, widget=None) -> None:
        pen = QPen(self.pen())
        if self.isSelected():
            pen.setWidthF(4.0)
            pen.setColor(pen.color().lighter(145))
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())


class _ShaderGraphScene(QGraphicsScene):
    graph_changed = Signal()
    layout_changed = Signal()
    node_selected = Signal(str)
    node_activated = Signal(str)
    message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.graph: ShaderGraph | None = None
        self.contract: ShaderContract | None = None
        self.node_items: dict[str, _ShaderNodeItem] = {}
        self.connections: list[_ShaderConnectionItem] = []
        self.pending_source: _ShaderPortItem | None = None
        self._pending_wire = None
        self.validator = validate_graph
        self.pin_filter = None
        self._rebuilding = False
        self.selectionChanged.connect(self._selection_changed)

    def set_graph(
        self,
        graph: ShaderGraph | None,
        contract: ShaderContract | None,
        *,
        validator=None,
    ) -> None:
        self.graph, self.contract = graph, contract
        self.validator = validator or validate_graph
        self.rebuild()

    def rebuild(self, *, select_node: str = "") -> None:
        previous_selection = {
            item.node.node_id for item in self.selectedItems()
            if isinstance(item, _ShaderNodeItem)
        }
        self._rebuilding = True
        try:
            self.clear()
            self.node_items.clear()
            self.connections.clear()
            self.pending_source = None
            self._pending_wire = None
            if self.graph is None or self.contract is None:
                self.setSceneRect(QRectF(0.0, 0.0, 900.0, 520.0))
                return
            for node in self.graph.nodes:
                item = _ShaderNodeItem(node, self.contract, self.pin_filter)
                self.addItem(item)
                item.setPos(node.x, node.y)
                self.node_items[node.node_id] = item
                if node.node_id == select_node:
                    item.setSelected(True)
            for link in self.graph.links:
                source_node = self.node_items.get(link.source_node)
                target_node = self.node_items.get(link.target_node)
                source = source_node.port_items.get(("output", link.source_pin)) if source_node else None
                target = target_node.port_items.get(("input", link.target_pin)) if target_node else None
                if source is None or target is None:
                    continue
                connection = _ShaderConnectionItem(link, source, target)
                self.addItem(connection)
                self.connections.append(connection)
            bounds = self.itemsBoundingRect().adjusted(-100.0, -100.0, 100.0, 100.0)
            self.setSceneRect(bounds if not bounds.isEmpty() else QRectF(0.0, 0.0, 900.0, 520.0))
        finally:
            self._rebuilding = False
            selected = {
                item.node.node_id for item in self.selectedItems()
                if isinstance(item, _ShaderNodeItem)
            }
            if selected != previous_selection:
                self._selection_changed()

    def _selection_changed(self) -> None:
        if self._rebuilding:
            return
        node = next((item for item in self.selectedItems() if isinstance(item, _ShaderNodeItem)), None)
        self.node_selected.emit(node.node.node_id if node is not None else "")

    def node_moved(self, item: _ShaderNodeItem) -> None:
        if self._rebuilding:
            return
        position = item.pos()
        item.node.x, item.node.y = position.x(), position.y()
        for connection in self.connections:
            if connection.source.node_item is item or connection.target.node_item is item:
                connection.update_path()
        self.layout_changed.emit()

    def port_clicked(self, port: _ShaderPortItem) -> None:
        if self.graph is None or self.contract is None:
            return
        if port.pin.direction == "output":
            self.cancel_connection()
            self.pending_source = port
            self.message.emit(
                f"Connecting {port.pin.label} — drag or click an input. Escape cancels."
            )
            return
        if self.pending_source is None:
            self.message.emit("Drag an output here to connect. Alt-click this port to disconnect.")
            return
        source = self.pending_source
        self.cancel_connection()
        bridge = None
        if not connection_compatible(source.pin.value_type, port.pin.value_type):
            bridge = self._bridge(source, port)
            if bridge is None:
                self.message.emit(
                    f"Cannot connect {source.pin.value_type} to {port.pin.value_type}. Use a component or compose node."
                )
                return
        previous, previous_nodes = list(self.graph.links), list(self.graph.nodes)
        try:
            if bridge is None:
                self.graph.connect(
                    source.node_item.node.node_id,
                    source.pin.name,
                    port.node_item.node.node_id,
                    port.pin.name,
                )
            else:
                inserted = bridge()
            diagnostics = self.validator(self.graph, self.contract)
        except ShaderGraphError as exc:
            self.graph.links[:], self.graph.nodes[:] = previous, previous_nodes
            self.message.emit(f"Connection rejected: {exc}")
            return
        if any(item.code in {"cycle", "function-cycle"} and item.severity == "error" for item in diagnostics):
            self.graph.links[:], self.graph.nodes[:] = previous, previous_nodes
            self.message.emit("Connection rejected: it would create a cycle.")
            return
        selected = port.node_item.node.node_id
        self.rebuild(select_node=selected)
        self.graph_changed.emit()
        self.message.emit("Connected." if bridge is None else
                          f"Connected through {inserted} so {source.pin.value_type} fits {port.pin.value_type}.")

    def _bridge(self, source, port):
        start, end = source.node_item.node, port.node_item.node
        x, y = (start.x + end.x) / 2, (start.y + end.y) / 2
        adapter = connection_adapter(source.pin.value_type, port.pin.value_type)
        if adapter is not None:
            def convert():
                kind, properties, input_pin, output_pin = adapter
                node = self.graph.add_node(kind, x=x, y=y, properties=properties)
                self.graph.connect(start.node_id, source.pin.name, node.node_id, input_pin)
                self.graph.connect(node.node_id, output_pin, end.node_id, port.pin.name)
                return f"a {node_title(node)} node"
            return convert
        width = TEXTURE_SPATIAL_WIDTHS.get(str(end.properties.get("dimension", "")))
        if (port.pin.name != "coordinates" or "array" not in str(end.properties.get("dimension", "")) or width is None
                or source.pin.value_type != (f"float{width}" if width > 1 else "float")
                or port.pin.value_type != f"float{width + 1}"):
            return None

        def first_slice():
            compose = self.graph.add_node("compose", x=x + 140, y=y, properties={"value_type": port.pin.value_type})
            for index in range(width):
                if width == 1:
                    self.graph.connect(start.node_id, source.pin.name, compose.node_id, "x")
                    continue
                component = self.graph.add_node("swizzle", x=x - 140, y=y + index * 90, properties={
                    "source_type": source.pin.value_type, "mask": "xyzw"[index]})
                self.graph.connect(start.node_id, source.pin.name, component.node_id, "value")
                self.graph.connect(component.node_id, "result", compose.node_id, "xyzw"[index])
            slice_index = self.graph.add_node("constant", x=x - 140, y=y + width * 90,
                                              properties={"value_type": "float", "values": [0.0]})
            self.graph.connect(slice_index.node_id, "value", compose.node_id, "xyzw"[width])
            self.graph.connect(compose.node_id, "value", end.node_id, port.pin.name)
            return "slice 0 of the array"
        return first_slice

    def disconnect_port(self, port):
        if self.graph is None:
            return
        self.cancel_connection()
        identity = port.node_item.node.node_id
        before = len(self.graph.links)
        self.graph.links[:] = [link for link in self.graph.links if not (
            (link.target_node == identity and link.target_pin == port.pin.name)
            if port.pin.direction == "input" else
            (link.source_node == identity and link.source_pin == port.pin.name))]
        if len(self.graph.links) != before:
            self.rebuild(select_node=identity)
            self.graph_changed.emit()
            self.message.emit("Connection removed.")

    def cancel_connection(self):
        self.pending_source = None
        if self._pending_wire is not None:
            self.removeItem(self._pending_wire)
            self._pending_wire = None

    def update_pending_wire(self, position):
        if self.pending_source is None:
            return
        if self._pending_wire is None:
            self._pending_wire = QGraphicsPathItem()
            self._pending_wire.setPen(QPen(graph_palette()["accent"], 2.0))
            self._pending_wire.setZValue(1)
            self.addItem(self._pending_wire)
        start = self.pending_source.scenePos()
        path = QPainterPath(start)
        distance = max(60.0, abs(position.x() - start.x()) / 2)
        path.cubicTo(start + QPointF(distance, 0), position - QPointF(distance, 0), position)
        self._pending_wire.setPath(path)

    def mouseDoubleClickEvent(self, event):
        node = next((item for item in self.items(event.scenePos()) if isinstance(item, _ShaderNodeItem)), None)
        if node is not None:
            self.node_activated.emit(node.node.node_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def delete_selected(self) -> None:
        if self.graph is None:
            return
        selected = list(self.selectedItems())
        links = [item.link for item in selected if isinstance(item, _ShaderConnectionItem)]
        nodes = [item.node for item in selected if isinstance(item, _ShaderNodeItem)]
        changed = False
        for link in links:
            try:
                self.graph.links.remove(link)
                changed = True
            except ValueError:
                pass
        for node in nodes:
            try:
                self.graph.remove_node(node.node_id)
                changed = True
            except ShaderGraphError as exc:
                self.message.emit(str(exc))
        if changed:
            self.rebuild()
            self.graph_changed.emit()


class _ShaderGraphView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setMinimumSize(320, 240)

    def fit_scene(self) -> None:
        bounds = self.scene().itemsBoundingRect()
        if not bounds.isEmpty():
            self.fitInView(bounds.adjusted(-45.0, -45.0, 45.0, 45.0), Qt.AspectRatioMode.KeepAspectRatio)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        draw_canvas_grid(painter, rect)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and isinstance(self.scene(), _ShaderGraphScene):
            self.scene().cancel_connection()
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}:
            scene = self.scene()
            if isinstance(scene, _ShaderGraphScene):
                scene.delete_selected()
                event.accept()
                return
        super().keyPressEvent(event)


class SdfShaderGraphEditor(QWidget):
    apply_requested = Signal(str, object, object)
    graph_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._program = None
        self._standalone_graph = False
        self._standalone_early_depth = False
        self._document = None
        self._contract: ShaderContract | None = None
        self._graph: ShaderGraph | None = None
        self._graph_cache: dict[tuple[int, int, str], ShaderGraph] = {}
        self._source_cache: dict[tuple[int, int, str], tuple[str, bool]] = {}
        self._source_origin_cache: dict[tuple[int, int, str], tuple[str, str]] = {}
        self._retained_keys: set[tuple[int, int, str]] = set()
        self._active_key: tuple[int, int, str] | None = None
        self._source_internal = False
        self._source_is_generated = False
        self._generated_source = None
        self._compiled_bytecode: bytes | None = None
        self._compiled_contract = None
        self._compiled_source_digest = ""
        self._compiled_targets: tuple[tuple[int, int], ...] = ()
        self._selected_node_id = ""
        self.source_path = ""
        self._build_ui()
        self._compile_timer = QTimer(self)
        self._compile_timer.setSingleShot(True)
        self._compile_timer.setInterval(350)
        self._compile_timer.timeout.connect(
            lambda: self._compile(quiet=True)
        )
        self.setEnabled(False)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header, header_row = editor_strip()
        title = self.title_label = styled_label(self.tr("Replacement Shader Graph"), heading="panel")
        header_row.addWidget(title)
        self.pass_label = QLabel(self.tr("Select a compiled pass."))
        self.pass_label.setWordWrap(True)
        set_style_role(self.pass_label, tone="muted")
        header_row.addSpacing(6)
        header_row.addWidget(self.pass_label, 1)
        stage_label = styled_label(self.tr("Stage"), tone="muted")
        header_row.addWidget(stage_label)
        self.stage_selector = QComboBox()
        self.stage_selector.currentIndexChanged.connect(self._stage_changed)
        header_row.addWidget(self.stage_selector)
        self.contract_badge = QLabel()
        self.contract_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_style_role(self.contract_badge, badge="neutral")
        self.contract_badge.hide()
        header_row.addSpacing(4)
        header_row.addWidget(self.contract_badge)
        root.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(8, 6, 8, 6)
        body.setSpacing(6)
        root.addLayout(body, 1)
        provenance = self.provenance_label = QLabel(
            self.tr(
                "This workspace builds a replacement for the selected compiled stage; it does not show the original "
                "source graph. The shipped shader remains unchanged until Apply."
            )
        )
        provenance.setWordWrap(True)
        set_style_role(provenance, banner="info")
        body.addWidget(provenance)

        self.new_button = strip_button(QPushButton(), "new_file", tooltip=self.tr("New replacement graph"))
        self.open_project_button = strip_button(QPushButton(), "folder_open", tooltip=self.tr("Open graph project…"))
        self.save_project_button = strip_button(QPushButton(), "save", tooltip=self.tr("Save graph project…"))
        self.fit_button = strip_button(QPushButton(), "fit", tooltip=self.tr("Fit graph"))
        self.arrange_button = strip_button(QPushButton(), "graph", tooltip=self.tr("Arrange graph"))
        self.delete_button = strip_button(QPushButton(), "delete", tooltip=self.tr("Delete selected"))
        self.new_button.clicked.connect(self._new_graph)
        self.open_project_button.clicked.connect(self._open_project)
        self.save_project_button.clicked.connect(self._save_project)
        self.fit_button.clicked.connect(self.fit_graph)
        self.arrange_button.clicked.connect(self._arrange_graph)
        self.delete_button.clicked.connect(self._delete_selected)
        graph_bar, graph_row = editor_strip("header")
        for widget in (self.new_button, self.open_project_button, self.save_project_button, toolbar_separator(),
                       self.fit_button, self.arrange_button, self.delete_button):
            graph_row.addWidget(widget)
        graph_row.addStretch(1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        palette_page = QWidget()
        palette_layout = QVBoxLayout(palette_page)
        palette_layout.setContentsMargins(0, 0, 0, 0)
        palette_layout.setSpacing(0)
        palette_layout.addWidget(pane_header(self.tr("Add a node")))
        palette_body = QVBoxLayout()
        palette_body.setContentsMargins(6, 6, 6, 6)
        palette_body.setSpacing(4)
        palette_layout.addLayout(palette_body, 1)
        self.palette_search = search_field(QLineEdit())
        self.palette_search.setPlaceholderText(self.tr("Find values, textures, or math…"))
        self.palette_search.textChanged.connect(self._filter_palette)
        palette_body.addWidget(self.palette_search)
        self.palette = QTreeWidget()
        self.palette.setHeaderHidden(True)
        self.palette.setRootIsDecorated(True)
        self.palette.itemDoubleClicked.connect(lambda _item, _column: self._add_palette_node())
        palette_body.addWidget(self.palette, 1)
        add_button = QPushButton(self.tr("Add selected node"))
        add_button.setIcon(line_icon("add"))
        add_button.clicked.connect(self._add_palette_node)
        palette_body.addWidget(add_button)
        splitter.addWidget(palette_page)

        self.scene = _ShaderGraphScene(self)
        self.view = _ShaderGraphView()
        self.view.setScene(self.scene)
        self.scene.graph_changed.connect(self._graph_changed)
        self.scene.layout_changed.connect(self._layout_changed)
        self.scene.node_selected.connect(self._node_selected)
        self.scene.node_activated.connect(self._edit_function_region)
        self.scene.message.connect(self._show_message)
        graph_page = QWidget()
        graph_layout = QVBoxLayout(graph_page)
        graph_layout.setContentsMargins(0, 0, 0, 0)
        graph_layout.setSpacing(0)
        graph_layout.addWidget(graph_bar)
        graph_layout.addWidget(self.view, 1)
        splitter.addWidget(graph_page)

        self.inspector_tabs = QTabWidget()
        self.properties_scroll = QScrollArea()
        self.properties_scroll.setWidgetResizable(True)
        self.properties_host = QWidget()
        self.properties_form = QFormLayout(self.properties_host)
        self.properties_form.setContentsMargins(8, 8, 8, 8)
        self.properties_scroll.setWidget(self.properties_host)
        self.inspector_tabs.addTab(self.properties_scroll, self.tr("Properties"))

        source_page = QWidget(self)
        source_page.hide()
        source_layout = QVBoxLayout(source_page)
        source_layout.setContentsMargins(6, 6, 6, 6)
        source_layout.setSpacing(4)
        source_actions = QHBoxLayout()
        source_actions.setSpacing(4)
        regenerate = strip_button(QPushButton(self.tr("Generate from graph")), "refresh", variant="")
        import_source = strip_button(QPushButton(self.tr("Open HLSL…")), "folder_open", variant="")
        export_source = strip_button(QPushButton(self.tr("Save HLSL…")), "save", variant="")
        regenerate.clicked.connect(self._regenerate_source)
        import_source.clicked.connect(self._open_hlsl)
        export_source.clicked.connect(self._save_hlsl)
        source_actions.addWidget(regenerate)
        source_actions.addWidget(import_source)
        source_actions.addWidget(export_source)
        source_actions.addStretch(1)
        source_layout.addLayout(source_actions)
        self.source_editor = QPlainTextEdit()
        self.source_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        set_style_role(self.source_editor, mono=True)
        self.source_editor.textChanged.connect(self._source_changed)
        source_layout.addWidget(self.source_editor, 1)
        self._developer_source_page = source_page

        diagnostics_page = QWidget()
        self.validation_page = diagnostics_page
        diagnostics_layout = QVBoxLayout(diagnostics_page)
        diagnostics_layout.setContentsMargins(6, 6, 6, 6)
        diagnostics_layout.setSpacing(4)
        self.diagnostics_summary = QLabel()
        self.diagnostics_summary.setWordWrap(True)
        set_style_role(self.diagnostics_summary, tone="muted")
        diagnostics_layout.addWidget(self.diagnostics_summary)
        self.diagnostics = QTreeWidget()
        self.diagnostics.setHeaderLabels([self.tr("Status"), self.tr("Diagnostic")])
        self.diagnostics.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.diagnostics.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.diagnostics.setRootIsDecorated(False)
        diagnostics_layout.addWidget(self.diagnostics, 1)
        self.inspector_tabs.addTab(diagnostics_page, self.tr("Validation"))
        splitter.addWidget(self.inspector_tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([245, 780, 420])
        body.addWidget(splitter, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.message = QLabel()
        self.message.setWordWrap(True)
        footer.addWidget(self.message, 1)
        self.apply_button = QPushButton(self.tr("Apply Replacement"))
        set_style_role(self.apply_button, variant="primary")
        self.apply_button.clicked.connect(self._apply)
        self.apply_button.setEnabled(False)
        footer.addWidget(self.apply_button)
        body.addLayout(footer)

    @property
    def graph(self) -> ShaderGraph | None:
        return self._graph

    def set_graph(self, graph: ShaderGraph, contract: ShaderContract, *, early_depth_stencil: bool = False) -> None:
        self._standalone_graph = True
        self._standalone_early_depth = bool(early_depth_stencil)
        self._compile_timer.stop()
        self._invalidate_compilation()
        self._program = self._document = None
        self._active_key = None
        self._contract, self._graph = contract, graph
        self.title_label.setText(self.tr("Editable Implementation"))
        self.provenance_label.setText(self.tr(
            "These nodes replace the whole calculation. Outputs keep their native meanings; they are not read as "
            "surface channels. The material editor applies the changes."
        ))
        for widget in (self.stage_selector, self.open_project_button, self.save_project_button,
                       self.apply_button, self.new_button):
            widget.hide()
        self.pass_label.setText(contract.stage.title())
        self.setEnabled(True)
        with material_region_read.suspend_region_read_session():
            self._populate_palette()
        self._populate_properties(None)
        with material_region_read.region_read_session():
            self.scene.set_graph(graph, contract)
            self._refresh_graph_state(regenerate=False)
        self.fit_graph()

    @property
    def contract(self) -> ShaderContract | None:
        return self._contract

    def set_program(self, program, document) -> None:
        if self._standalone_graph:
            self._standalone_graph = False
            self.title_label.setText(self.tr("Replacement Shader Graph"))
            self.provenance_label.setText(self.tr("This workspace authors a replacement for the selected stage. The shipped shader remains unchanged until Apply."))
            for widget in (self.stage_selector, self.open_project_button, self.save_project_button,
                           self.apply_button, self.new_button):
                widget.show()
        old_stage = str(self.stage_selector.currentData() or "")
        self._program, self._document = program, document
        references = [
            shader
            for shader in program.shaders
            if shader.present and shader.reflection is not None and shader.role != "input_signature"
        ]
        self.stage_selector.blockSignals(True)
        self.stage_selector.clear()
        for shader in references:
            self.stage_selector.addItem(
                stage_display_name(shader.role),
                shader.role,
            )
        preferred = old_stage if any(item.role == old_stage for item in references) else "pixel"
        index = self.stage_selector.findData(preferred)
        if index < 0 and self.stage_selector.count():
            index = 0
        self.stage_selector.setCurrentIndex(index)
        self.stage_selector.blockSignals(False)
        self.setEnabled(bool(references))
        self.pass_label.setText(
            self.tr("{name} · target {target}").format(
                name=program.name,
                target=program.target_index,
            )
        )
        self._activate_stage()

    def clear_program(self) -> None:
        self._leave_active_graph()
        self._program = self._document = self._contract = self._graph = None
        self._active_key = None
        self.stage_selector.clear()
        self.scene.set_graph(None, None)
        self.setEnabled(False)

    def _stage_changed(self, _index: int) -> None:
        self._activate_stage()

    def _leave_active_graph(self) -> None:
        key = self._active_key
        if key is None:
            return
        if key in self._retained_keys:
            self._source_cache[key] = (
                self.source_editor.toPlainText(),
                self._source_is_generated,
            )
            return
        self._graph_cache.pop(key, None)
        self._source_cache.pop(key, None)
        self._source_origin_cache.pop(key, None)

    def _activate_stage(self) -> None:
        if self._program is None:
            return
        self._leave_active_graph()
        stage = str(self.stage_selector.currentData() or "")
        shader = self._program.shader(stage)
        if shader is None or shader.reflection is None:
            self._contract = self._graph = None
            self.scene.set_graph(None, None)
            return
        self._contract = ShaderContract.from_shader(shader.reflection)
        if self._contract.stage == 'compute':
            self._contract = shader_contract_for_program(self._document.profile, self._program, stage)
        if shader.binary_format == 'dxbc-container':
            self._contract = material_implementation.implementation_contract(self._contract, shader.payload)
        key = (self._program.target_index, self._program.program_index, stage)
        self._active_key = key
        graph = self._graph_cache.get(key)
        if graph is None:
            graph = new_shader_graph(self._contract)
            self._graph_cache[key] = graph
        self._graph = graph
        if graph.contract_fingerprint != self._contract.fingerprint:
            compatibility = validate_graph(graph, self._contract)
            if not any(item.severity == "error" for item in compatibility):
                graph.contract_fingerprint = self._contract.fingerprint
        self.scene.set_graph(graph, self._contract)
        graph_enabled = stage in SHADER_GRAPH_STAGES
        self.palette.setEnabled(graph_enabled)
        self.new_button.setEnabled(graph_enabled)
        self._populate_palette()
        self._populate_properties(None)
        self._invalidate_compilation()
        cached_source = self._source_cache.get(key)
        if cached_source is not None:
            self._set_source(cached_source[0], generated=cached_source[1])
            self._refresh_graph_state(regenerate=False)
        else:
            self._set_source(
                self.tr(
                    "// Complete the visual graph to generate reflected HLSL for this pass.\n"
                ),
                generated=False,
            )
            self._refresh_graph_state(regenerate=graph_enabled)
        if graph_enabled:
            self._show_message(
                self.tr(
                    "Authoring a replacement; the current compiled shader stays unchanged until Apply. "
                    "Connect outputs, then compile. Alt-click a port to disconnect it; Ctrl+wheel zooms."
                )
            )
        else:
            self.apply_button.setEnabled(False)
            self._set_badge(self.tr("High-level replacement unavailable"), "danger")
            self._show_message(
                self.tr(
                    "This compiled stage is kept unchanged: its structure or side effects cannot be rebuilt from "
                    "graph nodes yet."
                )
            )

    def _populate_palette(self) -> None:
        self.palette.clear()
        contract = self._contract
        if contract is None or contract.stage not in SHADER_GRAPH_STAGES:
            return

        def category(label: str) -> QTreeWidgetItem:
            item = QTreeWidgetItem([label])
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            self.palette.addTopLevelItem(item)
            item.setExpanded(True)
            return item

        values = category(self.tr("Values"))
        constant = QTreeWidgetItem([self.tr("Constant")])
        constant.setData(0, Qt.ItemDataRole.UserRole, ("constant", {"value_type": "float4", "values": [0.0, 0.0, 0.0, 1.0]}))
        values.addChild(constant)
        for field in contract.inputs:
            if contract.stage in {'geometry','domain','hull'} and field.register == 0xffffffff:
                continue
            item = QTreeWidgetItem([field.display_name])
            item.setData(0, Qt.ItemDataRole.UserRole, ('primitive_input' if contract.stage in {'geometry','domain','hull'} else 'input', {"semantic": field.semantic_name, "semantic_index": field.semantic_index}))
            values.addChild(item)
        add_palette_templates(values, (*domain_templates(contract), *hull_templates(contract)))
        objects = [r for r in contract.resources if r.texture_value_type or r.input_type in {'sampler', 'byte-address-buffer', 'rw-byte-address-buffer', 'structured-buffer'}]
        if objects:
            objects_root = category(self.tr("Resource objects"))
            for resource in objects:
                item = QTreeWidgetItem([resource.name])
                item.setData(0, Qt.ItemDataRole.UserRole, ("shader_resource", {"resource": resource.name}))
                objects_root.addChild(item)
        sampling = category(self.tr("Texture sampling"))
        add_palette_templates(sampling, TEXTURE_TEMPLATES)
        buffers = category(self.tr("Buffer reads"))
        for stride in sorted({0, *(r.sample_count for r in objects if r.input_type == 'structured-buffer')}):
            item = QTreeWidgetItem([self.tr("Read Record Word ({stride} byte records)").format(stride=stride) if stride else self.tr("Read Buffer Word")])
            item.setData(0, Qt.ItemDataRole.UserRole, ("buffer_load", {"stride": stride, "byte_offset": 0}))
            buffers.addChild(item)
        control = category(self.tr("Control flow"))
        item = QTreeWidgetItem([self.tr("Branch")])
        item.setData(0, Qt.ItemDataRole.UserRole, ("branch", new_branch_properties()))
        repeat = QTreeWidgetItem(control, [self.tr('Repeat')])
        repeat.setData(0, Qt.ItemDataRole.UserRole, ('repeat', new_loop_properties()))
        control.addChild(item)
        code = QTreeWidgetItem(control, [self.tr("Code (HLSL)")])
        code.setData(0, Qt.ItemDataRole.UserRole, ("code", {
            "inputs": [{"name": "value", "type": "float3"}], "outputs": [{"name": "result", "type": "float3"}],
            "body": "result = value;"}))

        parameters = [
            (buffer, variable)
            for buffer in contract.constant_buffers
            for variable in buffer.variables
            if (variable.graph_type is not None or self._standalone_graph)
            and variable.name.casefold() != "capcom_material_reserve"
        ]
        if parameters:
            parameter_root = category(self.tr("Material and renderer values"))
            buffer_roots: dict[str, QTreeWidgetItem] = {}
            for buffer, variable in parameters:
                parent = buffer_roots.get(buffer.name)
                if parent is None:
                    parent = QTreeWidgetItem([_display_name(buffer.name)])
                    parameter_root.addChild(parent)
                    buffer_roots[buffer.name] = parent
                item = QTreeWidgetItem([_display_name(variable.name)])
                item.setToolTip(0, f"{buffer.name}.{variable.name} · {variable.graph_type} · b{buffer.bind_point}+{variable.byte_offset}")
                properties = {"buffer": buffer.name, "variable": variable.name}
                if variable.graph_type is None:
                    try:
                        fields = constant_value_fields(variable)
                        if not fields:
                            raise ShaderGraphError("No declared logical fields.")
                        properties["field_path"] = fields[0].path
                        item.setToolTip(0, self.tr("Choose a declared field or matrix row in the node properties."))
                    except ShaderGraphError as exc:
                        item.setDisabled(True)
                        item.setToolTip(0, str(exc))
                item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    ("parameter", properties),
                )
                parent.addChild(item)
                parent.setExpanded(buffer.name.casefold() == "usermaterial")

        textures, samplers = _sampling_resources(contract)
        if textures and samplers:
            texture_root = category(self.tr("Textures"))
            for resource in textures:
                item = QTreeWidgetItem([_display_name(resource.name)])
                item.setToolTip(0, f"{resource.dimension} · t{resource.bind_point}")
                item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    (
                        "texture_sample",
                        {"texture": resource.name, "sampler": samplers[0].name},
                    ),
                )
                texture_root.addChild(item)

        math_root = category(self.tr("Math"))
        for kind, label in (*BASIC_MATH_NODES, *EXTRA_MATH_NODES):
            properties = {"value_type": "float" if kind == 'compare' else "float4"}
            if kind == "swizzle":
                properties = {"source_type": "float4", "mask": "rgb"}
            item = QTreeWidgetItem([node_label(label)])
            item.setData(0, Qt.ItemDataRole.UserRole, (kind, properties))
            math_root.addChild(item)

        for category_name, templates in TYPED_MATH_CATEGORIES:
            typed_root = category(node_label(category_name))
            add_palette_templates(typed_root, templates)
        execution = category(self.tr('Execution'))
        add_palette_templates(execution, EXECUTION_TEMPLATES)
        if self._graph is not None:
            for meaning, (label, _, _) in INVOCATION_INPUTS.items():
                if self._graph.stage not in invocation_stages(meaning):
                    continue
                item = QTreeWidgetItem([node_label(label)])
                item.setData(0, Qt.ItemDataRole.UserRole, ('invocation_input', {'meaning': meaning}))
                execution.addChild(item)
        add_palette_templates(execution, (*geometry_templates(contract), *mesh_templates(contract),
                                          *storage_templates(contract, self._graph)))
        resources = category(self.tr('Resource Addressing'))
        add_palette_templates(resources, RESOURCE_TEMPLATES)

    def _filter_palette(self, text: str) -> None:
        filter_palette(self.palette, text)

    def _add_palette_node(self) -> None:
        if self._graph is None:
            return
        item = self.palette.currentItem()
        payload = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(payload, (tuple, list)) or len(payload) != 2:
            return
        center = self.view.mapToScene(self.view.viewport().rect().center())
        try:
            node = self._graph.add_node(
                str(payload[0]),
                x=center.x(),
                y=center.y(),
                properties=dict(payload[1]),
            )
        except ShaderGraphError as exc:
            self._show_message(str(exc))
            return
        self.scene.rebuild(select_node=node.node_id)
        self._selected_node_id = node.node_id
        self._populate_properties(node)
        self._graph_changed()

    def _node_selected(self, node_id: str) -> None:
        self._selected_node_id = node_id
        node = self._graph.node(node_id) if self._graph is not None and node_id else None
        self._populate_properties(node)

    def _clear_form(self) -> None:
        while self.properties_form.count():
            item = self.properties_form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _populate_properties(self, node: ShaderGraphNode | None) -> None:
        self._clear_form()
        if node is None or self._contract is None:
            label = QLabel(self.tr("Select a node to edit it. Ports are connected directly in the graph."))
            label.setWordWrap(True)
            set_style_role(label, tone="muted")
            self.properties_form.addRow(label)
            return
        heading = styled_label(node_title(node), heading="section")
        self.properties_form.addRow(heading)
        self.properties_form.addRow(self.tr("Node ID"), QLabel(node.node_id[:12]))
        _precision_properties(self.properties_form, node,
            lambda key, value: self._set_node_property(node, key, value, rebuild_properties=False))
        if node.kind == "code":
            _code_properties(self.properties_form, node, lambda inputs, outputs, body, n=node: self._set_code(
                n, inputs, outputs, body))
        if node.kind == "constant":
            type_combo = QComboBox()
            type_combo.addItems(NUMERIC_VALUE_TYPES)
            current_type = str(node.properties.get("value_type", "float4"))
            if type_combo.findText(current_type) < 0:
                type_combo.addItem(current_type)
            type_combo.setCurrentText(current_type)
            type_combo.currentTextChanged.connect(
                lambda value, n=node: self._set_node_property(n, "value_type", value, rebuild_properties=True)
            )
            self.properties_form.addRow(self.tr("Type"), type_combo)
            width = value_width(type_combo.currentText())
            values = list(node.properties.get("values", []))
            for index, channel in enumerate("XYZW"[:width]):
                if current_type.rstrip('234').endswith('64_t'):
                    editor = _wide_integer_control(current_type, values[index] if index < len(values) else 0,
                        lambda value, i=index: self._set_constant_value(node,i,value))
                    self.properties_form.addRow(channel,editor)
                    continue
                if current_type.startswith("bool"):
                    editor = QCheckBox()
                    editor.setChecked(bool(values[index]) if index < len(values) else False)
                    editor.toggled.connect(lambda value, i=index, n=node: self._set_constant_value(n, i, value))
                    self.properties_form.addRow(channel, editor)
                    continue
                editor = QDoubleSpinBox()
                floating = is_float_type(current_type)
                editor.setRange(*((-1_000_000.0, 1_000_000.0) if floating else
                                  (0, 0xffffffff) if current_type.rstrip('234') in {'uint', 'min16uint'} else (-0x80000000, 0x7fffffff)))
                editor.setDecimals(6 if floating else 0)
                editor.setSingleStep(0.05 if floating else 1)
                editor.setValue(float(values[index]) if index < len(values) else 0.0)
                editor.valueChanged.connect(
                    lambda value, i=index, n=node: self._set_constant_value(n, i, value)
                )
                self.properties_form.addRow(channel, editor)
        elif node.kind == "compare":
            _comparison_properties(self.properties_form, node,
                lambda key, value: self._set_node_property(node, key, value, rebuild_properties=False))
        elif (node.kind in BASIC_MATH_KINDS - {"normal_unpack", "swizzle"} or node.kind in UNARY_FUNCTIONS
              or node.kind in TYPED_MATH_NODES
              or node.kind in {"multiply_add", "select", "indexed_select", "array_read", "unassigned_value"}):
            _numeric_properties(self.properties_form, node,
                lambda key, value: self._set_node_property(node, key, value, rebuild_properties=True))
        elif node.kind == "constant_table":
            _table_properties(self.properties_form, node,
                lambda key, value: self._set_node_property(node, key, value, rebuild_properties=True))
        elif node.kind == "swizzle":
            type_combo = QComboBox()
            type_combo.addItems(list(NUMERIC_VALUE_TYPES))
            type_combo.setCurrentText(str(node.properties.get("source_type", "float4")))
            type_combo.currentTextChanged.connect(
                lambda value, n=node: self._set_node_property(n, "source_type", value, rebuild_properties=False)
            )
            mask = QLineEdit(str(node.properties.get("mask", "rgb")))
            mask.setMaxLength(4)
            mask.editingFinished.connect(
                lambda n=node, editor=mask: self._set_node_property(n, "mask", editor.text().casefold(), rebuild_properties=False)
            )
            self.properties_form.addRow(self.tr("Input type"), type_combo)
            self.properties_form.addRow(self.tr("Components"), mask)
        elif node.kind == 'invocation_input':
            _invocation_properties(self.properties_form, node, lambda key, value: self._set_node_property(node, key, value, rebuild_properties=True), stage=self._graph.stage)
        elif node.kind in {'emit_vertex','end_primitive','primitive_input'}:
            note = QLabel(self.tr('Connect Vertex Index to select an attribute from this input primitive.') if node.kind == 'primitive_input'
                          else self.tr('Output stream {stream}. Execution connections determine emission order.').format(stream=node.properties.get('stream',0)))
            note.setWordWrap(True)
            self.properties_form.addRow(note)
        elif node.kind in {'execution_begin', 'capture_value', 'discard_pixel', 'memory_load', 'memory_store', 'memory_atomic', 'memory_barrier', 'image_load', 'image_store', 'image_atomic'}:
            _execution_properties(self.properties_form, node, lambda key, value: self._set_node_property(node, key, value, rebuild_properties=True))
        elif node.kind in {'resource_index', 'descriptor_heap', 'resource_dimensions'}:
            _resource_properties(self.properties_form, node, lambda key, value: self._set_node_property(node, key, value, rebuild_properties=True))
        elif node.kind in {'buffer_load','constant_buffer_load'}:
            _buffer_properties(self.properties_form, node, lambda key, value: self._set_node_property(node, key, value, rebuild_properties=False))
        elif node.kind in {'branch', 'repeat', 'material_function'}:
            edit = QPushButton({'branch': self.tr('Edit Branch…'), 'repeat': self.tr('Edit Repeat…'),
                                'material_function': self.tr('Edit Function…')}[node.kind])
            edit.clicked.connect(lambda: self._edit_function_region(node.node_id))
            self.properties_form.addRow(edit)
        elif node.kind in {'sample_texture', 'texture_load'}:
            _texture_properties(self.properties_form, node, lambda key, value: self._set_node_property(node, key, value, rebuild_properties=True))
        elif node.kind == "shader_resource":
            resource = QComboBox()
            for item in self._contract.resources:
                if item.texture_value_type or item.input_type in {'sampler', 'byte-address-buffer', 'rw-byte-address-buffer', 'structured-buffer', 'rw-structured-buffer'}:
                    resource.addItem(item.name, item.name)
            resource.setCurrentIndex(resource.findData(node.properties.get("resource")))
            resource.currentIndexChanged.connect(lambda _index: self._set_node_property(node, "resource", resource.currentData(), rebuild_properties=True))
            self.properties_form.addRow(self.tr("Renderer binding"), resource)
            selected = self._contract.resource(node.properties.get('resource'))
            if selected is not None and selected.input_type in {'structured-buffer', 'rw-structured-buffer'}:
                _structured_word_control(self.properties_form, selected.sample_count, node.properties.get('word_bits', 32),
                    lambda bits: self._set_node_property(node, 'word_bits', bits, rebuild_properties=True))
            if selected is not None and selected.register_class == 'u':
                coherence = QCheckBox(self.tr('Globally coherent resource'))
                coherence.setChecked(node.properties.get('globally_coherent', False))
                coherence.toggled.connect(lambda value: self._set_node_property(node, 'globally_coherent', value, rebuild_properties=False))
                self.properties_form.addRow(coherence)
        elif node.kind == "texture_sample":
            textures, samplers = _sampling_resources(self._contract)
            texture_combo = QComboBox()
            sampler_combo = QComboBox()
            for item in textures:
                texture_combo.addItem(_display_name(item.name), item.name)
            for item in samplers:
                sampler_combo.addItem(_display_name(item.name), item.name)
            texture_combo.setCurrentIndex(max(0, texture_combo.findData(node.properties.get("texture"))))
            sampler_combo.setCurrentIndex(max(0, sampler_combo.findData(node.properties.get("sampler"))))
            texture_combo.currentIndexChanged.connect(
                lambda _index, n=node, editor=texture_combo: self._set_node_property(n, "texture", editor.currentData(), rebuild_properties=False)
            )
            sampler_combo.currentIndexChanged.connect(
                lambda _index, n=node, editor=sampler_combo: self._set_node_property(n, "sampler", editor.currentData(), rebuild_properties=False)
            )
            self.properties_form.addRow(self.tr("Texture"), texture_combo)
            self.properties_form.addRow(self.tr("Sampler"), sampler_combo)
        elif node.kind == "parameter":
            self.properties_form.addRow(self.tr("Buffer"), QLabel(str(node.properties.get("buffer", ""))))
            self.properties_form.addRow(self.tr("Value"), QLabel(str(node.properties.get("variable", ""))))
            if "field_path" in node.properties:
                buffer = self._contract.buffer(str(node.properties.get("buffer", "")))
                variable = next((item for item in buffer.variables if item.name == node.properties.get("variable")), None) if buffer else None
                if variable is not None:
                    fields = QComboBox()
                    for item in constant_value_fields(variable):
                        fields.addItem(f"{variable.name}{item.path} · {item.value_type}", item.path)
                    fields.setCurrentIndex(fields.findData(node.properties["field_path"]))
                    fields.currentIndexChanged.connect(lambda _index: self._set_node_property(node, "field_path", fields.currentData(), rebuild_properties=False))
                    self.properties_form.addRow(self.tr("Logical field"), fields)
        elif node.kind == "input":
            field = self._contract.input(
                str(node.properties.get("semantic", "")),
                int(node.properties.get("semantic_index", 0)),
            )
            if field is not None:
                self.properties_form.addRow(self.tr("Semantic"), QLabel(field.display_name))
                self.properties_form.addRow(self.tr("Type"), QLabel(field.value_type))
                self.properties_form.addRow(self.tr("Register"), QLabel(f"v{field.register}"))
        elif node.kind == "output":
            outputs = ", ".join(item.display_name for item in self._contract.outputs)
            self.properties_form.addRow(self.tr("Required outputs"), QLabel(outputs or self.tr("Depth/visibility only")))
        else:
            extension = shader_graph_node_extension(node.kind)
            note = QLabel(
                node_label(extension.description)
                if extension is not None and extension.description
                else self.tr("This profile-owned node has no editable properties.")
            )
            note.setWordWrap(True)
            self.properties_form.addRow(note)
        if node.kind != "output":
            remove = delete_button(self.tr("Delete node"))
            remove.clicked.connect(lambda _checked=False, identity=node.node_id: self._delete_node(identity))
            self.properties_form.addRow(remove)

    def _set_constant_value(self, node: ShaderGraphNode, index: int, value: float) -> None:
        values = list(node.properties.get("values", []))
        while len(values) <= index:
            values.append(0.0)
        values[index] = _constant_scalar(str(node.properties.get("value_type", "float4")))(value)
        node.properties["values"] = values
        self._graph_changed(rebuild_scene=False)

    def _edit_function_region(self, node_id):
        node = self._graph.node(node_id) if self._graph else None
        if node is None or node.kind not in {"branch", "repeat", "material_function"}:
            return
        try:
            if node.kind in {"branch", "repeat"}:
                dialog_type = MaterialBranchDialog if node.kind == 'branch' else MaterialLoopDialog
                dialog = dialog_type(node, self, validate_replacement=lambda properties:
                    validate_node_update(self._graph, node, properties, self._contract))
            else:
                dialog = MaterialFunctionDialog(material_function_from_node(node), self, validate_replacement=lambda asset:
                    replace_material_function(deepcopy(self._graph), node_id, asset))
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if node.kind in {"branch", "repeat"}:
                node.properties = dialog.result_properties
            else:
                replace_material_function(self._graph, node_id, dialog.result_asset)
        except ShaderGraphError as exc:
            self._show_message(str(exc))
            return
        self.scene.rebuild(select_node=node_id)
        self._populate_properties(node)
        self._graph_changed()

    def _set_node_property(
        self,
        node: ShaderGraphNode,
        key: str,
        value,
        *,
        rebuild_properties: bool,
    ) -> None:
        if node.properties.get(key) == value:
            return
        try:
            properties = {**node.properties, key: value}
            if node.kind == 'shader_resource' and key == 'resource' and properties.get('word_bits', 32) == 64:
                selected = self._contract.resource(value)
                if selected is None or selected.input_type not in {'structured-buffer', 'rw-structured-buffer'} or selected.sample_count % 8:
                    properties.pop('word_bits', None)
            if node.kind in {'sample_texture','texture_load'}:
                properties = texture_property_update(node.properties,key,value)
            validate_node_update(self._graph, node, properties, self._contract)
        except ShaderGraphError as exc:
            self._populate_properties(node)
            self._show_message(str(exc))
            return
        node.properties = properties
        if node.kind == "constant" and key == "value_type":
            width = value_width(value)
            scalar = _constant_scalar(value)
            values = node.properties.get("values", [])
            node.properties["values"] = [scalar(values[i]) if i < len(values) else scalar(0) for i in range(width)]
        self.scene.rebuild(select_node=node.node_id)
        if rebuild_properties:
            self._populate_properties(node)
        self._graph_changed(rebuild_scene=False)

    def _set_code(self, node: ShaderGraphNode, inputs: str, outputs: str, body: str) -> None:
        try:
            node.properties = updated_code(self._graph, node, inputs, outputs, body)
        except ShaderGraphError as exc:
            self._show_message(str(exc))
            return
        self.scene.rebuild(select_node=node.node_id)
        self._graph_changed(rebuild_scene=False)

    def _delete_node(self, node_id: str) -> None:
        if self._graph is None:
            return
        try:
            self._graph.remove_node(node_id)
        except ShaderGraphError as exc:
            self._show_message(str(exc))
            return
        self.scene.rebuild()
        self._populate_properties(None)
        self._graph_changed(rebuild_scene=False)

    def _delete_selected(self) -> None:
        self.scene.delete_selected()

    def _new_graph(self) -> None:
        if self._contract is None or self._program is None or self._contract.stage not in SHADER_GRAPH_STAGES:
            return
        if self._graph is not None and (len(self._graph.nodes) > len(self._contract.inputs) + 1 or self._graph.links):
            answer = QMessageBox.question(
                self,
                self.tr("Start a new shader graph"),
                self.tr("Discard the current unsaved graph for this pass and stage?"),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._graph = new_shader_graph(self._contract)
        key = (self._program.target_index, self._program.program_index, self._contract.stage)
        self._graph_cache[key] = self._graph
        self._source_cache.pop(key, None)
        self._retained_keys.add(key)
        self.scene.set_graph(self._graph, self._contract)
        self._populate_properties(None)
        self._graph_changed(rebuild_scene=False)
        self.fit_graph()

    def _graph_changed(self, *, rebuild_scene: bool = False) -> None:
        if self._active_key is not None:
            self._retained_keys.add(self._active_key)
        if rebuild_scene:
            self.scene.rebuild(select_node=self._selected_node_id)
        self._invalidate_compilation()
        self._refresh_graph_state(regenerate=not self._standalone_graph)
        self.graph_changed.emit()

    def _layout_changed(self) -> None:
        if self._active_key is not None:
            self._retained_keys.add(self._active_key)
        self.graph_changed.emit()

    def _refresh_graph_state(self, *, regenerate: bool) -> None:
        if self._graph is None or self._contract is None:
            return
        diagnostics = validate_graph(self._graph, self._contract)
        errors = [item for item in diagnostics if item.severity == "error"]
        warnings = [item for item in diagnostics if item.severity == "warning"]
        if errors:
            self._set_badge(self.tr("{count} graph issues").format(count=len(errors)), "danger")
        elif warnings:
            self._set_badge(self.tr("Graph ready · contract changed"), "warning")
        else:
            self._set_badge(self.tr("Graph ready · reflected contract"), "success")
        self._show_graph_diagnostics(diagnostics)
        if regenerate and not errors and self._contract.stage in SHADER_GRAPH_STAGES:
            try:
                generated = self._generate_source()
            except ShaderGraphError as exc:
                self._show_message(str(exc))
            else:
                self._set_source(generated.source, generated=True, metadata=generated)
                self._compile_timer.start()

    def _show_graph_diagnostics(self, diagnostics) -> None:
        self.diagnostics.clear()
        for diagnostic in diagnostics:
            self.diagnostics.addTopLevelItem(
                _diagnostic_item(diagnostic.severity.title(), diagnostic.severity, diagnostic.message))
        errors = sum(item.severity == "error" for item in diagnostics)
        self.diagnostics_summary.setText(
            self.tr("Connect every required stage output before compiling.")
            if errors
            else self.tr("Graph structure is valid. Compile to verify the emitted DXBC contract.")
        )

    def _set_source(self, source: str, *, generated: bool, metadata=None) -> None:
        self._source_internal = True
        try:
            self.source_editor.setPlainText(source)
        finally:
            self._source_internal = False
        self._source_is_generated = generated
        self._generated_source = metadata if generated else None
        if self._active_key is not None:
            self._source_cache[self._active_key] = (source, generated)
            if generated:
                self._source_origin_cache.pop(self._active_key, None)

    def _source_changed(self) -> None:
        if self._source_internal:
            return
        self._source_is_generated = False
        self._generated_source = None
        if self._active_key is not None:
            self._retained_keys.add(self._active_key)
            self._source_cache[self._active_key] = (
                self.source_editor.toPlainText(),
                False,
            )
        self._invalidate_compilation()
        self._set_badge(self.tr("Custom HLSL · compile required"), "info")

    def set_source_page_visible(self, visible: bool) -> None:
        index = self.inspector_tabs.indexOf(self._developer_source_page)
        if visible and index < 0:
            self.inspector_tabs.addTab(self._developer_source_page, self.tr("HLSL"))
            self.inspector_tabs.setCurrentWidget(self._developer_source_page)
        elif not visible and index >= 0:
            self.inspector_tabs.removeTab(index)

    def source_text(self) -> str:
        return self.source_editor.toPlainText()

    def source_is_generated(self) -> bool:
        return bool(self._source_is_generated)

    def set_source_text(self, source: str, *, generated: bool = False) -> None:
        self._set_source(source, generated=generated)

    def generated_source_text(self) -> str:
        return self._generate_source().source

    def _regenerate_source(self) -> None:
        if self._graph is None or self._contract is None:
            return
        try:
            generated = self._generate_source()
        except ShaderGraphError as exc:
            QMessageBox.warning(self, self.tr("Graph is incomplete"), str(exc))
            return
        self._set_source(generated.source, generated=True, metadata=generated)
        self._invalidate_compilation()
        self.inspector_tabs.setCurrentWidget(self.properties_scroll)

    def _generate_source(self):
        shader = self._program.shader(self._contract.stage) if self._program is not None else None
        early_depth = (material_implementation.implementation_execution(shader_bytes(shader)).early_depth_stencil if shader is not None
                       else self._standalone_early_depth)
        return generate_graph_hlsl(self._graph, self._contract, early_depth_stencil=early_depth)

    def _default_project_path(self) -> str:
        path = str(self.source_path or "")
        return f"{path}.reasyshader.json" if path else "shader.reasyshader.json"

    def _save_project(self) -> None:
        if self._graph is None or self._contract is None:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Save shader graph project"),
            self._default_project_path(),
            self.tr("REasy shader graph (*.reasyshader.json);;JSON files (*.json)"),
        )
        if not filename:
            return
        source_override = self.source_editor.toPlainText()
        try:
            generated = generate_graph_hlsl(self._graph, self._contract).source
        except ShaderGraphError:
            generated = None
        if generated == source_override:
            source_override = None
        project = ShaderAuthoringProject(self._graph, source_override)
        try:
            write_files_atomically({filename: project.to_json().encode("utf-8")})
        except (OSError, ShaderGraphError) as exc:
            QMessageBox.critical(self, self.tr("Shader graph save failed"), str(exc))
            return
        self._show_message(self.tr("Saved shader graph project."))

    def _open_project(self) -> None:
        if self._contract is None or self._program is None:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Open shader graph project"),
            "",
            self.tr("REasy shader graph (*.reasyshader.json *.json);;All files (*)"),
        )
        if not filename:
            return
        try:
            raw = Path(filename).read_bytes()
            if len(raw) > MAX_SOURCE_LENGTH * 2:
                raise ShaderGraphError("shader graph project is too large")
            project = ShaderAuthoringProject.from_json(raw.decode("utf-8"))
        except (OSError, UnicodeError, ShaderGraphError) as exc:
            QMessageBox.critical(self, self.tr("Shader graph open failed"), str(exc))
            return
        stage_index = self.stage_selector.findData(project.graph.stage)
        if stage_index < 0:
            QMessageBox.warning(
                self,
                self.tr("Shader graph is incompatible"),
                self.tr("This pass has no {stage} stage.").format(stage=project.graph.stage),
            )
            return
        self.stage_selector.setCurrentIndex(stage_index)
        if self._contract is None:
            return
        self._graph = project.graph
        key = (self._program.target_index, self._program.program_index, self._contract.stage)
        self._graph_cache[key] = self._graph
        self._retained_keys.add(key)
        self.scene.set_graph(self._graph, self._contract)
        self._populate_properties(None)
        self._refresh_graph_state(regenerate=project.source_override is None)
        if project.source_override is not None:
            self._set_source(project.source_override, generated=False)
        self._invalidate_compilation()
        self.fit_graph()

    def _open_hlsl(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Open HLSL source"),
            "",
            self.tr("HLSL source (*.hlsl *.fx);;All files (*)"),
        )
        if not filename:
            return
        try:
            raw = Path(filename).read_bytes()
            if len(raw) > MAX_SOURCE_LENGTH:
                raise OSError("HLSL source exceeds the 4 MiB authoring limit")
            source = raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            QMessageBox.critical(self, self.tr("HLSL open failed"), str(exc))
            return
        self._set_source(source, generated=False)
        if self._active_key is not None:
            self._retained_keys.add(self._active_key)
            self._source_origin_cache[self._active_key] = (
                filename,
                _source_digest(source),
            )
        self._invalidate_compilation()

    def _save_hlsl(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Save HLSL source"),
            "shader.hlsl",
            self.tr("HLSL source (*.hlsl);;All files (*)"),
        )
        if not filename:
            return
        try:
            write_files_atomically({filename: self.source_editor.toPlainText().encode("utf-8")})
        except OSError as exc:
            QMessageBox.critical(self, self.tr("HLSL save failed"), str(exc))
            return
        if self._active_key is not None:
            source = self.source_editor.toPlainText()
            self._source_origin_cache[self._active_key] = (
                filename,
                _source_digest(source),
            )

    def _scope_targets(self) -> tuple[tuple[int, int], ...]:
        if self._program is None or self._document is None:
            return ()
        targets = []
        for target in self._document.targets:
            if self._program.program_index >= len(target.programs):
                continue
            targets.append((target.index, self._program.program_index))
        return tuple(targets)

    def _program_for_target(self, target_index: int, program_index: int):
        if self._document is None:
            return None
        target = next((item for item in self._document.targets if item.index == target_index), None)
        if target is None or not 0 <= program_index < len(target.programs):
            return None
        return target.programs[program_index]

    def _replacement_issues(self, candidate, bytecode, targets):
        issues, compatible = [], True
        execution = material_implementation.implementation_execution(bytecode)
        for target_index, program_index in targets:
            program = self._program_for_target(target_index, program_index)
            shader = program.shader(self._contract.stage) if program is not None else None
            scope = f'Target {target_index}'
            try:
                if shader is None or not shader.present or shader.reflection is None:
                    raise ShaderGraphError(f'Pass has no reflected {self._contract.stage} stage.')
                native = shader_bytes(shader)
                reference = material_implementation.implementation_contract(ShaderContract.from_shader(shader.reflection), native)
                report = validate_shader_contract(reference, candidate)
                compatible &= report.compatible
                issues.extend((issue.severity, scope, issue.message) for issue in report.issues)
                material_implementation._check_interpolation(reference, candidate)
                if material_implementation.implementation_execution(native) != execution:
                    raise ShaderGraphError('The replacement changes native early-depth execution.')
            except ValueError as exc:
                compatible = False
                issues.append(('error', scope, str(exc)))
        return issues, compatible

    def _compile(self, *, quiet: bool = False) -> None:
        if self._contract is None or self._standalone_graph or self._program is None or self._document is None:
            return
        source = self.source_editor.toPlainText()
        if not source.strip():
            if not quiet:
                QMessageBox.warning(
                    self,
                    self.tr("Replacement graph is incomplete"),
                    self.tr("Connect the required graph outputs first."),
                )
            return
        if self._contract.model_major < 6 and not hlsl_compiler_available():
            self._show_compile_failure(
                self.tr(
                    "The background Shader Model 5 compiler is unavailable. Install d3dcompiler_47.dll."
                )
            )
            return
        try:
            if self._source_is_generated:
                generated = self._generated_source or self._generate_source()
                if generated.source != source:
                    raise ShaderGraphError('The generated source no longer matches this graph. Generate it again before compiling.')
                self._generated_source = generated
                shader = self._program.shader(self._contract.stage)
                result = material_implementation.compile_implementation(generated, self._contract, shader.binary_format)
            elif re.search(r"^\s*#\s*include\b", source, re.MULTILINE):
                origin = self._source_origin_cache.get(self._active_key)
                if origin is None or origin[1] != _source_digest(source):
                    raise HlslCompileError(
                        "Relative #include directives require a saved source file. "
                        "Save HLSL, then compile again so includes resolve from its directory."
                    )
                if self._contract.model_major >= 6:
                    raise ShaderGraphError('DXC source-file includes need an include resolver; this path must not be sent to the Shader Model 5 compiler.')
                result = compile_hlsl_for_target(
                    path=origin[0],
                    entry_point="main",
                    target=self._contract.target,
                    warnings_as_errors=True,
                )
            else:
                result = compile_hlsl_for_target(
                    source,
                    entry_point="main",
                    target=self._contract.target,
                    warnings_as_errors=True,
                )
        except (HlslCompileError, dxc.DxcError, ShaderGraphError) as exc:
            self._show_compile_failure(str(exc))
            if not quiet:
                self.inspector_tabs.setCurrentWidget(self.validation_page)
            return
        targets = self._scope_targets()
        try:
            candidate = getattr(result, 'semantic_contract', None)
            if candidate is None:
                reflected = parse_dxbc(result.bytecode)
                if reflected.model_major >= 6:
                    reflected = dxc.enrich_dxil_shader(reflected, result.bytecode)
                candidate = material_implementation.implementation_contract(ShaderContract.from_shader(reflected), result.bytecode)
            all_issues, compatible = self._replacement_issues(candidate, result.bytecode, targets)
        except ValueError as exc:
            self._show_compile_failure(str(exc))
            return
        if result.diagnostics:
            all_issues.insert(0, ("warning", "Compiler", result.diagnostics))
        self._show_contract_report(all_issues, compatible)
        self._compiled_bytecode = result.bytecode if compatible else None
        self._compiled_contract = candidate if compatible else None
        self._compiled_source_digest = _source_digest(source) if compatible else ""
        self._compiled_targets = targets if compatible else ()
        self.apply_button.setEnabled(compatible)
        if not quiet:
            self.inspector_tabs.setCurrentWidget(self.validation_page)
        if compatible:
            self._set_badge(self.tr("Replacement verified · ready to apply"), "success")

    def _show_compile_failure(self, message: str) -> None:
        self._invalidate_compilation()
        self.diagnostics.clear()
        self.diagnostics.addTopLevelItem(_diagnostic_item(self.tr("Error"), "error", message))
        self.diagnostics_summary.setText(
            self.tr("The background replacement build failed. The original shader was not changed.")
        )
        self._set_badge(self.tr("Replacement build failed"), "danger")

    def _set_badge(self, text: str, tone: str) -> None:
        self.contract_badge.setText(text)
        set_style_role(self.contract_badge, badge=tone)
        self.contract_badge.show()

    def _show_contract_report(self, issues, compatible: bool) -> None:
        self.diagnostics.clear()
        for severity, scope, message in issues:
            self.diagnostics.addTopLevelItem(_diagnostic_item(severity.title(), severity, f"{scope}: {message}"))
        self.diagnostics_summary.setText(
            self.tr("Verified replacement for {count} hardware variant(s).").format(
                count=len(self._scope_targets()),
            )
            if compatible
            else self.tr("The replacement is incompatible and cannot be applied.")
        )

    def _invalidate_compilation(self, *_args) -> None:
        self._compiled_bytecode = None
        self._compiled_contract = None
        self._compiled_source_digest = ""
        self._compiled_targets = ()
        self.apply_button.setEnabled(False)

    def _apply(self) -> None:
        if self._compiled_bytecode is None or self._contract is None or self._standalone_graph:
            return
        source = self.source_editor.toPlainText()
        targets = self._scope_targets()
        if _source_digest(source) != self._compiled_source_digest or targets != self._compiled_targets:
            self._invalidate_compilation()
            QMessageBox.warning(
                self,
                self.tr("Compile again"),
                self.tr("The source or apply scope changed after validation."),
            )
            return
        issues, compatible = self._replacement_issues(self._compiled_contract, self._compiled_bytecode, targets)
        if not compatible:
            self._invalidate_compilation()
            self._show_contract_report(issues, False)
            QMessageBox.warning(
                self,
                self.tr("Pass contract changed"),
                self.tr("The selected pass changed after compilation. Compile again."),
            )
            return
        self.apply_requested.emit(self._contract.stage, self._compiled_bytecode, targets)

    def _show_message(self, message: str) -> None:
        self.message.setText(str(message))

    def fit_graph(self) -> None:
        self.view.fit_scene()

    def _arrange_graph(self) -> None:
        if self._graph is None:
            return
        try:
            arrange_shader_graph(self._graph, self._contract)
        except ShaderGraphError as exc:
            self._show_message(str(exc))
            return
        self.scene.rebuild(select_node=self._selected_node_id)
        self._layout_changed()
        self.fit_graph()


__all__ = ["SdfShaderGraphEditor"]
