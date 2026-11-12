from typing import List

from .base_model import BaseModel, FileHandler
lazy from .uvar_node import UvarNode
lazy from .uvar_types import NodeConnection

class UvarExpression(BaseModel):

    def __init__(self):
        super().__init__()
        self.nodes_offset: int = 0
        self.relations_offset: int = 0
        self.root: int = 0
        self.nodes: List[UvarNode] = []
        self.relations: List[NodeConnection] = []

    def do_read(self, handler: FileHandler) -> bool:
        self.nodes_offset, self.relations_offset, count, links, self.root = handler.read('<QQHHH')
        with handler.seek_temp(self.nodes_offset):
            for _ in range(count):
                node = UvarNode()
                node.read(handler)
                self.nodes.append(node)
        with handler.seek_temp(self.relations_offset):
            self.relations = [NodeConnection(*handler.read('<HHHH')) for _ in range(links)]
        return True

    def do_write(self, handler: FileHandler) -> bool:
        handler.write('<QQHHH', 0, 0, len(self.nodes), len(self.relations), self.root)
        handler.align_write(16)
        handler.write_at(self.start_offset, '<Q', handler.tell)
        for node in self.nodes:
            node.write(handler)
        for node in self.nodes:
            handler.write_at(node.start_offset, '<Q', handler.tell)
            handler.write_string(node.name, 'ascii')
            handler.align_write(16)
            if node.parameters:
                handler.write_at(node.start_offset + 8, '<Q', handler.tell)
                for parameter in node.parameters:
                    parameter.write(handler)
            handler.align_write(16)
        handler.write_at(self.start_offset + 8, '<Q', handler.tell)
        for link in self.relations:
            handler.write('<HHHH', link.input_node, link.input_pin, link.output_node, link.output_pin)
        return True

    def __repr__(self) -> str:
        return f"UvarExpression(nodes={len(self.nodes)}, relations={len(self.relations)}, root={self.root})"
