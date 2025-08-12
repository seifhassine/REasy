from typing import Optional, Any
import uuid

from .base_model import BaseModel, FileHandler
from .uvar_types import NodeValueType

RAW_TYPE_TO_NODE_VALUE = {
    6: NodeValueType.UInt32Maybe,
    7: NodeValueType.Int32,
    8: NodeValueType.Single,
    10: NodeValueType.Single,
    18: NodeValueType.Guid,
    20: NodeValueType.Guid,
}

PARAM_READERS = {
    NodeValueType.UInt32Maybe: lambda h: h.read('<I'),
    NodeValueType.Int32:       lambda h: h.read('<i'),
    NodeValueType.Single:      lambda h: h.read('<f'),
}

PARAM_WRITERS = {
    NodeValueType.UInt32Maybe: lambda h, v: h.write('<I', v if v is not None else 0),
    NodeValueType.Int32:       lambda h, v: h.write('<i', v if v is not None else 0),
    NodeValueType.Single:      lambda h, v: h.write('<f', v if v is not None else 0.0),
}

class NodeParameter(BaseModel):
    
    def __init__(self):
        super().__init__()
        self.name_hash: int = 0
        self.type: NodeValueType = NodeValueType.Unknown
        self.value: Optional[Any] = None
        self.end_offset: Optional[int] = None
        self.raw_type_code: Optional[int] = None
        
    def do_read(self, handler: FileHandler) -> bool:
        try:
            self.name_hash = handler.read('<I')
            raw_type = handler.read('<i')
            self.raw_type_code = raw_type
            mapped = RAW_TYPE_TO_NODE_VALUE.get(raw_type)
            if mapped is None:
                raise NotImplementedError(f"Unhandled UVAR node value type id {raw_type}")
            self.type = mapped
            
            if self.type == NodeValueType.Guid:
                guid_offset = handler.read('<Q')
                with handler.seek_temp(guid_offset):
                    self.value = handler.read_guid()
            else:
                reader = PARAM_READERS.get(self.type)
                if reader is None:
                    raise NotImplementedError(f"Unhandled UVAR node value reader for type {self.type}")
                self.value = reader(handler)
            
            handler.align(16)
            self.end_offset = handler.tell
            return True
            
        except Exception as e:
            print(f"Error reading NodeParameter: {e}")
            return False
            
    def do_write(self, handler: FileHandler) -> bool:
        try:
            handler.write('<I', self.name_hash)
            if self.raw_type_code is not None:
                handler.write('<i', self.raw_type_code)
            else:
                handler.write('<i', self.type.value)
            
            if self.type == NodeValueType.Guid:
                handler.write('<Q', handler.tell + 8)
                if isinstance(self.value, uuid.UUID):
                    handler.write_guid(self.value)
                elif isinstance(self.value, str):
                    handler.write_guid(uuid.UUID(self.value))
                else:
                    handler.write_guid(uuid.UUID(int=0))
            else:
                writer = PARAM_WRITERS.get(self.type)
                if writer is None:
                    raise NotImplementedError(f"Unhandled UVAR node value writer for type {self.type}")
                writer(handler, self.value)
                
            handler.align_write(16)
            return True
            
        except Exception as e:
            print(f"Error writing NodeParameter: {e}")
            return False
    
    def __repr__(self) -> str:
        return f"NodeParameter(name_hash={self.name_hash:08x}, type={self.type.name}, value={self.value})"