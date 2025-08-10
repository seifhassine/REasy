from typing import List
import struct

from .base_model import BaseModel, FileHandler
from .uvar_node import UvarNode
from .uvar_types import NodeConnection

class UvarExpression(BaseModel):
    
    def __init__(self):
        super().__init__()
        self.nodes_offset: int = 0
        self.nodes_count: int = 0
        self.relations_offset: int = 0
        self.relations_count: int = 0
        
        self.nodes: List[UvarNode] = []
        self.relations: List[NodeConnection] = []
        
    def do_read(self, handler: FileHandler) -> bool:
        self.nodes_offset = handler.read('<Q')
        self.nodes_count = handler.read('<I')
        self.relations_offset = handler.read('<Q')
        self.relations_count = handler.read('<I')
        
        if self.nodes_count > 0 and self.nodes_offset > 0:
            with handler.seek_temp(self.nodes_offset):
                for i in range(self.nodes_count):
                    node = UvarNode()
                    if not node.read(handler):
                        raise ValueError(f"Failed to read node {i}")
                    self.nodes.append(node)
            
        if self.relations_count > 0 and self.relations_offset > 0:
            with handler.seek_temp(self.relations_offset):
                for i in range(self.relations_count):
                    relation = NodeConnection()
                    relation.src_node = handler.read('<I')
                    relation.src_port = handler.read('<I')
                    relation.dst_node = handler.read('<I')
                    relation.dst_port = handler.read('<I')
                    self.relations.append(relation)
            
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        handler.write('<Q', 0)  # nodes_offset - will be updated
        handler.write('<I', len(self.nodes))
        handler.write('<Q', 0)  # relations_offset - will be updated
        handler.write('<I', len(self.relations))
        
        if self.nodes:
            nodes_offset = handler.tell
            handler.write_at(handler.tell - 28, '<Q', nodes_offset)
            
            for node in self.nodes:
                if not node.write(handler):
                    raise ValueError("Failed to write node")
                    
        if self.relations:
            relations_offset = handler.tell
            handler.write_at(handler.tell - 16, '<Q', relations_offset)
            
            for relation in self.relations:
                handler.write('<I', relation.src_node)
                handler.write('<I', relation.src_port)
                handler.write('<I', relation.dst_node)
                handler.write('<I', relation.dst_port)
                
        return True
            
    def __repr__(self) -> str:
        return f"UvarExpression(nodes={len(self.nodes)}, relations={len(self.relations)})"