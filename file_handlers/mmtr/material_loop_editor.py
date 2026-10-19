from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QLabel, QScrollArea, QTabWidget, QVBoxLayout, QWidget
from file_handlers.sdf.shader_graph import ShaderGraphError
from file_handlers.sdf.shader_graph_math import NUMERIC_VALUE_TYPES
from .material_function_editor import apply_buttons, function_canvas
from .material_loop import loop_body, loop_properties


class MaterialLoopDialog(QDialog):
    def __init__(self, node, parent=None, *, validate_replacement=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr('Edit Repeat'))
        self.resize(1350, 860)
        body, states, self.continuation = loop_body(node)
        self.states = dict(states)
        self.result_properties = None
        self.validate_replacement = validate_replacement
        root = QVBoxLayout(self)
        note = QLabel(self.tr('The step executes at least once. Continue controls whether another iteration runs. State values receive their selected updates together after each step.'))
        note.setWordWrap(True)
        root.addWidget(note)
        self.tabs = QTabWidget()
        self.editor = function_canvas(body, self, parent, allow_defaults=False)
        self.tabs.addTab(self.editor, self.tr('Step'))
        self.bindings = QScrollArea()
        self.bindings.setWidgetResizable(True)
        self.tabs.addTab(self.bindings, self.tr('Loop State'))
        root.addWidget(self.tabs)
        self.editor.interface_changed.connect(self._refresh_bindings)
        self._refresh_bindings()
        apply_buttons(self, root)

    def _refresh_bindings(self):
        ports = [n for n in self.editor.project.graph.nodes if n.kind in {'function_input', 'function_output'}]
        inputs = [n.properties for n in ports if n.kind == 'function_input']
        outputs = [n.properties for n in ports if n.kind == 'function_output']
        self.states = {key: value for key, value in self.states.items() if any(p['pin_id'] == key for p in inputs)}
        widget = QWidget()
        form = QFormLayout(widget)

        def choice(options, current):
            combo = QComboBox()
            for label, identity in options:
                combo.addItem(label, identity)
            index = combo.findData(current)
            if index < 0:
                combo.addItem(self.tr('Missing port: ') + str(current), current)
                index = combo.count() - 1
            combo.setCurrentIndex(index)
            return combo

        continuation = choice([(self.tr('Choose a boolean result'), None),
            *((p['display_name'], p['pin_id']) for p in outputs if p['value_type'] == 'bool')], self.continuation)
        continuation.currentIndexChanged.connect(lambda: setattr(self, 'continuation', continuation.currentData()))
        form.addRow(self.tr('Continue while'), continuation)
        for pin in inputs:
            if pin['value_type'] not in NUMERIC_VALUE_TYPES:
                continue
            update = choice([(self.tr('Keep the caller value'), None),
                *((p['display_name'], p['pin_id']) for p in outputs if p['value_type'] == pin['value_type'])], self.states.get(pin['pin_id']))
            update.currentIndexChanged.connect(lambda _index, key=pin['pin_id'], combo=update: self._set_state(key, combo.currentData()))
            form.addRow(pin['display_name'], update)
        self.bindings.setWidget(widget)

    def _set_state(self, source, target):
        if target is None:
            self.states.pop(source, None)
        else:
            self.states[source] = target

    def accept(self):
        try:
            properties = loop_properties(self.editor.asset(), list(self.states.items()), self.continuation)
            if self.validate_replacement:
                self.validate_replacement(properties)
        except (ShaderGraphError, ValueError) as exc:
            self.editor._show_message(str(exc))
            self.tabs.setCurrentIndex(0)
            return
        self.result_properties = properties
        super().accept()
