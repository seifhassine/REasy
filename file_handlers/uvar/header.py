from typing import List, Optional, TYPE_CHECKING
import uuid

if TYPE_CHECKING:
    from .variable import Variable

from .base_model import BaseModel, FileHandler
from .uvar_types import UVAR_MAGIC
from utils.hash_util import murmur3_hash

class HeaderStruct(BaseModel):
    def __init__(self):
        super().__init__()
        self.version: int = 3
        self.magic: int = UVAR_MAGIC
        self.strings_offset: int = 0
        self.data_offset: int = 0
        self.embeds_info_offset: int = 0
        self.hash_info_offset: int = 0
        self.ukn: int = 0
        self.uvar_hash: int = 0
        self.variable_count: int = 0
        self.embed_count: int = 0
        self.name: Optional[str] = None
        
    def do_read(self, handler: FileHandler) -> bool:
        self.version = handler.read('<I')
        self.magic = handler.read('<I')
        
        if self.magic != UVAR_MAGIC:
            raise ValueError(f"Invalid UVAR magic: {self.magic:08x}")
            
        self.strings_offset = handler.read('<Q')
        self.data_offset = handler.read('<Q')
        self.embeds_info_offset = handler.read('<Q')
        self.hash_info_offset = handler.read('<Q')
        
        if self.version < 3:
            self.ukn = handler.read('<Q')
            
        self.uvar_hash = handler.read('<I')
        self.variable_count = handler.read('<h')
        self.embed_count = handler.read('<h')
        
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        handler.write('<I', self.version)
        handler.write('<I', self.magic)
        
        handler.write('<Q', self.strings_offset)
        handler.write('<Q', self.data_offset)
        handler.write('<Q', self.embeds_info_offset)
        handler.write('<Q', self.hash_info_offset)
        
        if self.version < 3:
            handler.write('<Q', self.ukn)
            
        if self.name:
            self.uvar_hash = murmur3_hash(self.name.encode('utf-16le'))
        else:
            self.uvar_hash = murmur3_hash(b'')
        handler.write('<I', self.uvar_hash)
        handler.write('<h', self.variable_count)
        handler.write('<h', self.embed_count)
        
        return True
            
    def __repr__(self) -> str:
        return f"HeaderStruct(version={self.version}, name='{self.name}', vars={self.variable_count}, embeds={self.embed_count})"


class HashData(BaseModel):
    
    def __init__(self):
        super().__init__()
        self.guids_offset: int = 0
        self.maps_offset: int = 0
        self.hash_offset: int = 0
        self.hash_map_offset: int = 0
        
        self.count: int = 0
        self.guids: List[uuid.UUID] = []
        self.guid_map: List[int] = []
        self.name_hashes: List[int] = []
        self.name_hash_map: List[int] = []
        
    def do_read(self, handler: FileHandler) -> bool:
        self.guids_offset = handler.read('<Q')
        self.maps_offset = handler.read('<Q')
        self.hash_offset = handler.read('<Q')
        self.hash_map_offset = handler.read('<Q')
        
        if self.count == 0:
            return True

        if self.guids_offset > 0 and self.guids_offset < len(handler.data):
            with handler.seek_temp(self.guids_offset):
                self.guids = [handler.read_guid() for _ in range(self.count)]
            
        if self.maps_offset > 0 and self.maps_offset < len(handler.data):
            with handler.seek_temp(self.maps_offset):
                self.guid_map = [handler.read('<I') for _ in range(self.count)]
            
        if self.hash_offset > 0 and self.hash_offset < len(handler.data):
            with handler.seek_temp(self.hash_offset):
                self.name_hashes = [handler.read('<I') for _ in range(self.count)]
            
        if self.hash_map_offset > 0 and self.hash_map_offset < len(handler.data):
            with handler.seek_temp(self.hash_map_offset):
                self.name_hash_map = [handler.read('<I') for _ in range(self.count)]
            
        return True
            
    def do_write(self, handler: FileHandler) -> bool:
        count = len(self.guids)
        self.guids_offset = handler.tell + 32
        self.maps_offset = self.guids_offset + count * 16
        self.hash_offset = self.maps_offset + count * 4
        self.hash_map_offset = self.hash_offset + count * 4
        
        handler.write('<Q', self.guids_offset)
        handler.write('<Q', self.maps_offset)
        handler.write('<Q', self.hash_offset)
        handler.write('<Q', self.hash_map_offset)
        
        handler.seek(self.guids_offset)
        for guid in self.guids:
            handler.write_guid(guid)
            
        handler.seek(self.maps_offset)
        for idx in self.guid_map:
            handler.write('<I', idx)
            
        handler.seek(self.hash_offset)
        for hash_val in self.name_hashes:
            handler.write('<I', hash_val)
            
        handler.seek(self.hash_map_offset)
        for idx in self.name_hash_map:
            handler.write('<I', idx)
            
        return True
            
    def rebuild(self, variables: List['Variable']):
        self.count = len(variables)
        self.guids = []
        self.guid_map = []
        self.name_hashes = []
        self.name_hash_map = []
        
        if self.count == 0:
            return
            
        indexed_vars = list(enumerate(variables))
        
        sorted_by_hash = sorted(indexed_vars, key=lambda x: x[1].name_hash)
        for idx, var in sorted_by_hash:
            self.name_hashes.append(var.name_hash)
            self.name_hash_map.append(idx)
            
        sorted_by_guid = sorted(indexed_vars, key=lambda x: x[1].guid.bytes_le)
        for idx, var in sorted_by_guid:
            self.guids.append(var.guid)
            self.guid_map.append(idx)
            
    def __repr__(self) -> str:
        return f"HashData(count={self.count})"