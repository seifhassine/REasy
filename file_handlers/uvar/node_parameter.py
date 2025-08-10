from typing import Optional, Any, Union
import struct
import uuid

from .base_model import BaseModel, FileHandler
from .uvar_types import NodeValueType

class NodeParameter(BaseModel):
    
    def __init__(self):
        super().__init__()
        self.name_hash: int = 0
        self.type: NodeValueType = NodeValueType.Unknown
        self.value: Optional[Any] = None
        
    def do_read(self, handler: FileHandler) -> bool:
        try:
            self.name_hash = handler.read('<I')
            self.type = NodeValueType(handler.read('<i'))
            
            if self.name_offset > 0:
                with handler.seek_temp(self.name_offset):
                    self.name = handler.read_wstring()
            
            if self.type == NodeValueType.UInt32Maybe:
                self.value = handler.read('<I')
            elif self.type == NodeValueType.Int32:
                self.value = handler.read('<i')
            elif self.type == NodeValueType.Single:
                self.value = handler.read('<f')
            elif self.type == NodeValueType.Guid:
                guid_offset = handler.read('<Q')
                with handler.seek_temp(guid_offset):
                    self.value = handler.read_guid()
            else:
                raise NotImplementedError(f"Unhandled UVAR node value type id {self.type}")
                
            return True
            
        except Exception as e:
            print(f"Error reading NodeParameter: {e}")
            return False
            
    def do_write(self, handler: FileHandler) -> bool:
        try:
            handler.write('<I', self.name_hash)
            handler.write('<i', self.type.value)
            
            if self.type == NodeValueType.Int32:
                handler.write('<i', self.value if self.value is not None else 0)
            elif self.type == NodeValueType.UInt32Maybe:
                handler.write('<I', self.value if self.value is not None else 0)
            elif self.type == NodeValueType.Single:
                handler.write('<f', self.value if self.value is not None else 0.0)
            elif self.type == NodeValueType.Guid:
                handler.write('<Q', handler.tell + 8)
                if isinstance(self.value, uuid.UUID):
                    handler.write_guid(self.value)
                elif isinstance(self.value, str):
                    handler.write_guid(uuid.UUID(self.value))
                else:
                    handler.write_guid(uuid.UUID(int=0))
            else:
                raise NotImplementedError(f"Unhandled UVAR node value type id {self.type}")
                
            handler.align_write(16)
            return True
            
        except Exception as e:
            print(f"Error writing NodeParameter: {e}")
            return False
    
    def __repr__(self) -> str:
        return f"NodeParameter(name_hash={self.name_hash:08x}, type={self.type.name}, value={self.value})"