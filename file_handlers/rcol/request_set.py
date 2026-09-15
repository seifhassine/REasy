from typing import List, Optional
from .base_model import BaseModel, FileHandler
from utils.hash_util import murmur3_hash

def calc_hash(text: str) -> int:
    """Calculate murmur3 hash for UTF-16LE encoded string"""
    return murmur3_hash(text.encode('utf-16le'))

class RequestSetInfo(BaseModel):
    """Request set information"""
    def __init__(self):
        super().__init__()
        self.id = 0
        self.field0 = 0  # For v25, field at +0 (might not be ID)
        self.group_index = 0
        self.shape_offset = 0
        self.status = 0
        
        self.request_set_userdata_index = 0  # >= rcol.25
        self.group_userdata_index_start = 0  # >= rcol.25
        self.request_set_index = 0  # >= rcol.25
        self.name = ""
        self.name_hash = 0
        
        self.key_name = ""
        self.key_hash = 0

    def do_read(self, handler: FileHandler) -> bool:
        if handler.file_version >= 25:
            self.field0 = handler.read_uint32()  # This might not always be ID
            self.group_index = handler.read_int32()
            self.request_set_userdata_index = handler.read_int32()
            self.group_userdata_index_start = handler.read_int32()
            self.status = handler.read_int32()
            self.request_set_index = handler.read_int32()  # This seems to be the real ID
            
            self.id = self.request_set_index
            
            self.name = handler.read_offset_wstring()
            self.key_name = handler.read_offset_wstring()
            self.name_hash = handler.read_uint32()
            self.key_hash = handler.read_uint32()
        else:
            if handler.file_version >= 20:
                self.field0 = handler.read_uint32()
            else:
                self.id = handler.read_uint32()
            self.group_index = handler.read_int32()
            self.shape_offset = handler.read_int32()
            self.status = handler.read_int32()
            self.name = handler.read_offset_wstring()
            self.name_hash = handler.read_uint32()
            if handler.file_version >= 20:
                self.request_set_index = handler.read_uint32()
                self.id = self.request_set_index
            else:
                handler.skip(4)
            self.key_name = handler.read_offset_wstring()
            self.key_hash = handler.read_uint32()
            handler.skip(4)
            
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        if handler.file_version >= 25:
            handler.write_uint32(self.field0)
            handler.write_int32(self.group_index)
            self.name_hash = calc_hash(self.name)
            self.key_hash = calc_hash(self.key_name)
            # TODO make sure we set request_set_userdata_index
            handler.write_int32(self.request_set_userdata_index)
            handler.write_int32(self.group_userdata_index_start)
            handler.write_int32(self.status)
            handler.write_int32(self.request_set_index)
            handler.write_offset_wstring(self.name, context="request_name")
            handler.write_offset_wstring(self.key_name, context="request_key")
            handler.write_uint32(self.name_hash)
            handler.write_uint32(self.key_hash)
        else:
            if handler.file_version >= 20:
                handler.write_uint32(self.field0)
            else:
                handler.write_uint32(self.id)
            handler.write_int32(self.group_index)
            self.name_hash = calc_hash(self.name)
            self.key_hash = calc_hash(self.key_name)
            handler.write_int32(self.shape_offset)
            handler.write_int32(self.status)
            handler.write_offset_wstring(self.name, context="request_name")
            handler.write_uint32(self.name_hash)
            if handler.file_version >= 20:
                handler.write_uint32(self.request_set_index)
            else:
                handler.write_int32(0)  # padding
            handler.write_offset_wstring(self.key_name, context="request_key")
            handler.write_uint32(self.key_hash)
            handler.write_int32(0)  # padding
            
        return True
        
    def __str__(self):
        return self.name

class RequestSet:
    """Request set with info and references"""
    def __init__(self, index: int = 0, info: Optional[RequestSetInfo] = None):
        self.index = index
        self.info = info or RequestSetInfo()
        self.group = None  # RcolGroup reference
        self.instance = None  # RSZ instance
        self.shape_userdata: List = []  # List of RSZ instances
        
    def __str__(self):
        return f"[{self.index:08d}] {self.info.name}"
        
    def clone(self):
        """Create a clone of the request set"""
        clone = RequestSet(self.index, self.info.clone())
        clone.instance = self.instance
        clone.group = self.group
        clone.shape_userdata = list(self.shape_userdata)
        return clone

class IgnoreTag(BaseModel):
    """Ignore tag information"""
    def __init__(self):
        super().__init__()
        self.tag = ""
        self.hash = 0
        
    def do_read(self, handler: FileHandler) -> bool:
        self.tag = handler.read_offset_wstring()
        self.hash = handler.read_uint32()
        handler.read_int32() # reserved
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        handler.write_offset_wstring(self.tag)
        handler.write_uint32(self.hash)
        handler.write_int32(0)
        return True
        
    def __str__(self):
        return self.tag
