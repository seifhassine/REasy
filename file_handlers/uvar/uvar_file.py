from typing import List, Optional, Union, TYPE_CHECKING, Any
import uuid

if TYPE_CHECKING:
    from .variable import Variable

from .base_model import BaseModel, FileHandler
from .header import HeaderStruct, HashData
from .variable import Variable
from .uvar_types import TypeKind
from .uvar_expression import UvarExpression
from .config import MAX_VARIABLES
from utils.hash_util import murmur3_hash

class UVarFile(BaseModel):
    def __init__(self, handler: Optional[Union[FileHandler, bytes, bytearray]] = None, is_embedded: bool = False):
        super().__init__()
        self.header = HeaderStruct()
        self.variables: List[Variable] = []
        self.embedded_uvars: List['UVarFile'] = []
        self.hash_data = HashData()
        self.is_embedded = is_embedded
        
        if handler is not None:
            if isinstance(handler, FileHandler):
                self.do_read(handler)
            else:
                self.read(handler)
            
    def do_read(self, handler: FileHandler) -> bool:
        self.header = HeaderStruct()
        if not self.header.read(handler):
            raise ValueError("Failed to read UVAR header")
            
        if self.header.strings_offset > 0:
            with handler.seek_temp(self.header.strings_offset):
                self.header.name = handler.read_wstring()
            
        self.variables.clear()
        if self.header.variable_count > 0:
            if self.header.variable_count > MAX_VARIABLES:
                raise ValueError(f"Variable count {self.header.variable_count} exceeds maximum {MAX_VARIABLES}")
            
            if self.header.data_offset > 0:
                if self.header.data_offset >= len(handler.data):
                    raise ValueError(f"data_offset {self.header.data_offset} is beyond file size {len(handler.data)}")
                
                handler.seek_relative(self.header.data_offset)
                
                temp_vars: List[Variable] = []
                for i in range(self.header.variable_count):
                    if handler.tell + 48 > len(handler.data):
                        raise ValueError(f"Not enough data to read variable {i} at position {handler.tell}")
                    var = Variable()
                    if not var.read_header(handler):
                        raise ValueError(f"Failed to read variable header {i} at position {handler.tell}")
                    temp_vars.append(var)
                
                # Read all values sequentially from the end of the variable headers, using relative positions
                data_ptr = handler.position
                for v in temp_vars:
                    if v.type == TypeKind.Trigger:
                        v.reset_value()
                        continue
                    handler.seek_relative(data_ptr)
                    start_rel = handler.position
                    v.read_value(handler)
                    consumed = max(0, handler.position - start_rel)
                    data_ptr += consumed
                    data_ptr += (4 - (data_ptr % 4)) % 4
                
                self.variables = temp_vars
                
                for v in self.variables:
                    if v.expression_offset > 0:
                        with handler.seek_temp(v.expression_offset):
                            v.expression = UvarExpression()
                            if not v.expression.read(handler):
                                raise ValueError(f"Failed to read expression for variable {v.name}")
                    
        self.embedded_uvars.clear()
        if self.header.embed_count > 0 and self.header.embeds_info_offset > 0:
            handler.seek_relative(self.header.embeds_info_offset)
            
            for i in range(self.header.embed_count):
                embed_offset = handler.read('<Q')
                if embed_offset == 0 or embed_offset >= len(handler.data):
                    raise ValueError(f"Invalid embedded UVAR offset: {embed_offset}")
                
                with handler.seek_temp(embed_offset):
                    embed_handler = FileHandler(handler.data, embed_offset)
                    embed_file = UVarFile()
                    embed_file.is_embedded = True
                    if not embed_file.do_read(embed_handler):
                        raise ValueError(f"Failed to read embedded UVAR {i}")
                    self.embedded_uvars.append(embed_file)
            
        if self.header.variable_count > 0 and self.header.hash_info_offset > 0:
            handler.seek_relative(self.header.hash_info_offset)
            self.hash_data = HashData()
            self.hash_data.count = self.header.variable_count
            if not self.hash_data.read(handler):
                raise ValueError("Failed to read hash data")
            
        return True
            
    def do_write(self, handler: FileHandler) -> bool:
        self.update_strings()
        
        if self.hash_data is None:
            self.hash_data = HashData()
        self.hash_data.rebuild(self.variables)
        
        self.header.variable_count = len(self.variables)
        self.header.embed_count = len(self.embedded_uvars)
        
        self.header.start_offset = handler.tell
        if not self.header.write(handler):
            raise ValueError("Failed to write header")
            
        handler.align_write(16)
        
        if self.variables:
            handler.write_at(self.header.start_offset + 16, '<Q', handler.tell)
            self.header.data_offset = handler.tell
            
            for var in self.variables:
                var.write(handler)
            
            base_counts: dict[int, int] = {}
            for var in self.variables:
                if var.value_offset > 0:
                    base_counts[var.value_offset] = base_counts.get(var.value_offset, 0) + 1
            group_start_by_base: dict[int, int] = {}
            
            from .uvar_types import TypeKind as _TK
            current_data_end_offset = handler.tell
            
            for var in self.variables:
                orig_base = var.value_offset
                if orig_base > 0 and base_counts.get(orig_base, 0) > 1:
                    if orig_base not in group_start_by_base:
                        group_start_by_base[orig_base] = handler.tell
                    if var.type == _TK.Trigger:
                        handler.write_at(var.start_offset + 24, '<Q', var.value_offset)
                    else:
                        var.write_value(handler, value_offset_override=group_start_by_base[orig_base])
                else:
                    if var.type == _TK.Trigger:
                        if var._original_value_offset is not None and var._original_value_offset == 0:
                            handler.write_at(var.start_offset + 24, '<Q', 0)
                            var.value_offset = 0
                        elif var.value_offset == 0:
                            handler.write_at(var.start_offset + 24, '<Q', current_data_end_offset)
                            var.value_offset = current_data_end_offset
                        else:
                            handler.write_at(var.start_offset + 24, '<Q', var.value_offset)
                    else:
                        var.write_value(handler)
                
                if var.type != _TK.Trigger:
                    current_data_end_offset = handler.tell
                
            handler.align_write(16)
            
            for var in self.variables:
                var.write_expression(handler)
                
        handler.write_at(self.header.start_offset + 8, '<Q', handler.tell)
        self.header.strings_offset = handler.tell
        
        handler.write_wstring(self.header.name or "")
        
        for var in self.variables:
            handler.write_at(var.start_offset + 16, '<Q', handler.tell)
            handler.write_wstring(var.name or "")
            
        if self.embedded_uvars:
            handler.align_write(16)
            handler.write_at(self.header.start_offset + 24, '<Q', handler.tell)
            self.header.embeds_info_offset = handler.tell
            
            embed_offsets_start = handler.tell
            handler.skip(8 * len(self.embedded_uvars))
            
            for i, embed in enumerate(self.embedded_uvars):
                handler.align_write(16)
                
                embed_start_pos = handler.tell
                handler.write_at(embed_offsets_start + i * 8, '<Q', embed_start_pos)
                
                embed_handler = FileHandler(bytearray())
                embed.do_write(embed_handler)
                embed_bytes = bytearray(embed_handler.get_bytes())
                handler.write_bytes(embed_bytes)
                
        if self.hash_data is not None:
            handler.align_write(16)
            handler.write_at(self.header.start_offset + 32, '<Q', handler.tell)
            self.header.hash_info_offset = handler.tell
            
            if not self.hash_data.write(handler):
                raise ValueError("Failed to write hash data")
            
        return True
            
    def read(self, data: Union[bytes, bytearray]) -> bool:
        handler = FileHandler(data)
        return self.do_read(handler)
        
    def write(self) -> bytes:
        handler = FileHandler(bytearray())
        if self.do_write(handler):
            return handler.get_bytes()
        return b''
        
    def update_strings(self):
        for var in self.variables:
            var.name_hash = murmur3_hash((var.name or "").encode('utf-16le'))
            
        for embed in self.embedded_uvars:
            embed.update_strings()
            
    def add_variable(self, name: str, var_type: Union[int, 'TypeKind'], value: Any = None) -> Variable:
        from .uvar_types import TypeKind
        
        var = Variable()
        var.guid = uuid.uuid4()
        var.name = name
        if isinstance(var_type, int):
            var.type = TypeKind(var_type)
        else:
            var.type = var_type
        var.flags = 0
        var.name_hash = murmur3_hash(name.encode('utf-16le')) 
        
        if value is not None:
            var.value = value
        else:
            var.reset_value()
        
        var.name_offset = 0
        var.value_offset = 0
        var.expression_offset = 0
        
        self.variables.append(var)
        return var
        
    def remove_variable(self, index: int) -> bool:
        if 0 <= index < len(self.variables):
            del self.variables[index]
            return True
        return False
        
    def find_variable_by_name(self, name: str) -> Optional[Variable]:
        for var in self.variables:
            if var.name == name:
                return var
        return None
        
    def find_variable_by_guid(self, guid: uuid.UUID) -> Optional[Variable]:
        for var in self.variables:
            if var.guid == guid:
                return var
        return None
        
    def __repr__(self) -> str:
        return f"UVarFile(name='{self.header.name}', vars={len(self.variables)}, embeds={len(self.embedded_uvars)})"