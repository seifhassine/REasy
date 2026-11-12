from typing import List

from .base_model import BaseModel, FileHandler
lazy from .node_parameter import NodeParameter

class UvarNode(BaseModel):

    def __init__(self):
        super().__init__()
        self.name: str = ""
        self.node_id: int = 0
        self.class_name_hash: int = 0
        self.node_type: int = 0
        self.parameters: List[NodeParameter] = []

    def do_read(self, handler: FileHandler) -> bool:
        name_offset, parameters_offset, self.class_name_hash, _empty, self.node_id, count, self.node_type = \
            handler.read('<QQIIHHB3x')
        with handler.seek_temp(name_offset):
            self.name = handler.read_string('ascii')
        with handler.seek_temp(parameters_offset):
            for _ in range(count):
                parameter = NodeParameter()
                parameter.read(handler)
                self.parameters.append(parameter)
        return True

    def do_write(self, handler: FileHandler) -> bool:
        handler.write('<QQIIHHB3x', 0, 0, self.class_name_hash, 0, self.node_id, len(self.parameters), self.node_type)
        return True

    def __repr__(self) -> str:
        return f"UvarNode(id={self.node_id}, name='{self.name}', parameters={len(self.parameters)})"
