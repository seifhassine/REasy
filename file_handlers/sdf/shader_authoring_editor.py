from PySide6.QtCore import Signal
from PySide6.QtWidgets import QStackedLayout, QWidget

lazy from .raytracing_graph_editor import RayTracingGraphEditor
lazy from .shader_graph_editor import SdfShaderGraphEditor


class SdfShaderAuthoringEditor(QWidget):
    apply_requested = Signal(str, object, object)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.source_path = ''
        self.stack = QStackedLayout(self)
        self.stages = SdfShaderGraphEditor(); self.stack.addWidget(self.stages)
        self.stages.apply_requested.connect(self.apply_requested)
        self.raytracing = None

    def set_program(self,program,document):
        shader = program.shader('compute')
        if shader is not None and shader.raytracing_library is not None:
            if self.raytracing is None:
                self.raytracing = RayTracingGraphEditor()
                self.raytracing.apply_requested.connect(self.apply_requested)
                self.stack.addWidget(self.raytracing)
            self.stack.setCurrentWidget(self.raytracing)
            self.raytracing.set_program(program)
        else:
            self.stages.source_path = self.source_path
            self.stack.setCurrentWidget(self.stages)
            self.stages.set_program(program,document)

    def clear_program(self):
        self.stages.clear_program()
        if self.raytracing: self.raytracing.clear_program()
