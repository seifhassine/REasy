from typing import List, Optional
import struct

from .base_model import BaseModel, FileHandler
from .node_parameter import NodeParameter

class UvarNode(BaseModel):
    
    def __init__(self):
        self.node_type: NodeValueType = NodeValueType.Unknown
        self.id: int = 0
        self.name: str = ""
        self.name_offset: int = 0
        self.data_offset: int = 0
        self.data_size: int = 0
        self.data: bytes = b''
        self.parameters: List[NodeParameter] = []
        
    def do_read(self, handler: FileHandler) -> bool:
        self.node_type = NodeValueType(handler.read('<I'))
        self.id = handler.read('<I')
        self.name_offset = handler.read('<Q')
        self.data_offset = handler.read('<Q')
        self.data_size = handler.read('<I')
        handler.skip(4)  # padding
        
        if self.name_offset > 0:
            with handler.seek_temp(self.name_offset):
                self.name = handler.read_string('ascii')
        else:
            self.name = ""
            
        if self.data_offset > 0 and self.data_size > 0:
            with handler.seek_temp(self.data_offset):
                if self.node_type == NodeValueType.SetVariable:
                    param = NodeParameter()
                    param.guid = handler.read_guid()
                    param.name_hash = handler.read('<I')
                    self.parameters.append(param)
                else:
                    self.data = handler.read_bytes(self.data_size)
            
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        handler.write('<I', self.node_type.value)
        handler.write('<I', self.id)
        handler.write('<Q', 0)  # name_offset - will be updated
        handler.write('<Q', 0)  # data_offset - will be updated
        handler.write('<I', self.data_size)
        handler.write('<I', 0)  # padding
        
        if self.name:
            name_offset = handler.tell
            handler.write_at(handler.tell - 32, '<Q', name_offset)
            handler.write_string(self.name, 'ascii')
        
        if self.data_size > 0:
            data_offset = handler.tell
            handler.write_at(handler.tell - 24, '<Q', data_offset)
            
            if self.node_type == NodeValueType.SetVariable and self.parameters:
                param = self.parameters[0]
                handler.write_guid(param.guid)
                handler.write('<I', param.name_hash)
            elif self.data:
                handler.write_bytes(self.data)
                
        return True
            
    def __repr__(self) -> str:
        return f"UvarNode(id={self.id}, type={self.node_type.name}, name='{self.name}')"