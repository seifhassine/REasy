from typing import List
import struct

from .base_model import BaseModel, FileHandler
from .uvar_node import UvarNode
from .uvar_types import NodeConnection

class UvarExpression(BaseModel):
    
    def __init__(self):
        super().__init__()
        self.nodes_offset: int = 0
        self.relations_offset: int = 0
        self.nodes_count: int = 0
        self.output_node_id: int = 0
        self.unknown_count: int = 0
        
        self.nodes: List[UvarNode] = []
        self.relations: List[NodeConnection] = []
        
        self._orig_relations_size: int | None = None
        self._had_zero_terminator: bool = False
        
    def do_read(self, handler: FileHandler) -> bool:
        self.nodes_offset = handler.read('<Q')
        self.relations_offset = handler.read('<Q')
        self.nodes_count = handler.read('<h')
        self.output_node_id = handler.read('<h')
        self.unknown_count = handler.read('<h')
        
        self.nodes = []
        if self.nodes_count > 0 and self.nodes_offset > 0:
            with handler.seek_temp(self.nodes_offset):
                for _ in range(self.nodes_count):
                    node = UvarNode()
                    if not node.read(handler):
                        raise ValueError("Failed to read node")
                    self.nodes.append(node)
        
        self.relations = []
        self._orig_relations_size = None
        self._had_zero_terminator = False
        if self.nodes_count == 0:
            self._orig_relations_size = 0
            return True
        if self.relations_offset > 0:
            rp = self.relations_offset
            data = handler.data
            base = getattr(handler, 'offset', 0)
            end = len(data)
            while base + rp + 8 <= end:
                next_u64 = struct.unpack_from('<Q', data, base + rp)[0]
                if next_u64 == 0:
                    break
                first_u16 = struct.unpack_from('<H', data, base + rp)[0]
                if self.nodes_count > 0 and first_u16 >= self.nodes_count:
                    break
                rel = NodeConnection()
                rel.src_node = struct.unpack_from('<H', data, base + rp)[0]
                rel.src_port = struct.unpack_from('<H', data, base + rp + 2)[0]
                rel.dst_node = struct.unpack_from('<H', data, base + rp + 4)[0]
                rel.dst_port = struct.unpack_from('<H', data, base + rp + 6)[0]
                self.relations.append(rel)
                rp += 8
            self._orig_relations_size = rp - self.relations_offset
        
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        handler.write('<QQ', 0, 0)
        handler.write('<h', len(self.nodes))
        handler.write('<h', self.output_node_id)
        handler.write('<h', self.unknown_count)
        handler.align_write(16)
        
        nodes_offset = handler.tell
        handler.write_at(self.start_offset + 0, '<Q', nodes_offset)
        for node in self.nodes:
            node.start_offset = handler.tell
            handler.write('<Q', 0)
            handler.write('<Q', 0)
            handler.write('<Q', node.ukn_offset)
            handler.write('<h', node.node_id)
            handler.write('<h', len(node.parameters))
            handler.write('<I', node.ukn_count)
        
        for node in self.nodes:
            name_pos = handler.tell
            handler.write_at(node.start_offset + 0, '<Q', name_pos)
            handler.write_string(node.name or "", 'ascii')
            handler.align_write(16)
            if node.parameters:
                data_pos = handler.tell
                handler.write_at(node.start_offset + 8, '<Q', data_pos)
                for p in node.parameters:
                    p.write(handler)
            handler.align_write(16)
        
        rel_off = handler.tell
        handler.write_at(self.start_offset + 8, '<Q', rel_off)
        if self.relations:
            for rel in self.relations:
                handler.write('<H', rel.src_node)
                handler.write('<H', rel.src_port)
                handler.write('<H', rel.dst_node)
                handler.write('<H', rel.dst_port)
        handler.align_write(16)
        return True
        
    def __repr__(self) -> str:
        return f"UvarExpression(nodes={len(self.nodes)}, relations={len(self.relations)})"