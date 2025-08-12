from typing import List
import struct

from .base_model import BaseModel, FileHandler
from .node_parameter import NodeParameter

class UvarNode(BaseModel):
    
    def __init__(self):
        self.name_offset: int = 0
        self.data_offset: int = 0
        self.ukn_offset: int = 0
        self.node_id: int = 0
        self.value_count: int = 0
        self.ukn_count: int = 0
        self.name: str = ""
        self.parameters: List[NodeParameter] = []
        self._orig_name_end: int | None = None
        self._orig_params_end: int | None = None
        
    def do_read(self, handler: FileHandler) -> bool:
        self.name_offset = handler.read('<Q')
        self.data_offset = handler.read('<Q')
        self.ukn_offset = handler.read('<Q')
        self.node_id = handler.read('<h')
        self.value_count = handler.read('<h')
        self.ukn_count = handler.read('<I')
        
        if self.name_offset > 0:
            end = handler.data.find(0, self.name_offset)
            if end == -1:
                end = self.name_offset
            with handler.seek_temp(self.name_offset):
                self.name = handler.read_string('ascii')
            self._orig_name_end = end + 1
        else:
            self.name = ""
            self._orig_name_end = None
        
        self.parameters = []
        self._orig_params_end = None
        if self.data_offset > 0 and self.value_count > 0:
            with handler.seek_temp(self.data_offset):
                for _ in range(self.value_count):
                    param = NodeParameter()
                    if not param.read(handler):
                        raise ValueError("Failed to read UVAR node parameter")
                    self.parameters.append(param)
                self._orig_params_end = handler.tell
        
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        self.start_offset = handler.tell
        handler.write('<Q', 0)
        handler.write('<Q', 0)
        handler.write('<Q', self.ukn_offset)
        handler.write('<h', self.node_id)
        self.value_count = len(self.parameters)
        handler.write('<h', self.value_count)
        handler.write('<I', self.ukn_count)
        return True
        
    def flush_data(self, handler: FileHandler):
        if self.name:
            name_pos = handler.tell
            handler.write_at(self.start_offset + 0, '<Q', name_pos)
            handler.write_string(self.name, 'ascii')
            handler.align(16)
        if self.parameters:
            data_pos = handler.tell
            handler.write_at(self.start_offset + 8, '<Q', data_pos)
            for p in self.parameters:
                p.write(handler)
            handler.align_write(16)
        
    def __repr__(self) -> str:
        return f"UvarNode(id={self.node_id}, name='{self.name}', values={self.value_count})"