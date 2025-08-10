from typing import Optional, Any, Type
import uuid

from .base_model import BaseModel, FileHandler
from .uvar_types import TypeKind, UvarFlags, Vec3, Int3, Uint3, Position, get_python_type
from .uvar_expression import UvarExpression
from utils.hash_util import murmur3_hash

TYPE_FORMATS = {
    TypeKind.Boolean: '<B',
    TypeKind.Int8: '<b',
    TypeKind.Uint8: '<B',
    TypeKind.Int16: '<h',
    TypeKind.Uint16: '<H',
    TypeKind.Int32: '<i',
    TypeKind.Uint32: '<I',
    TypeKind.Int64: '<q',
    TypeKind.Uint64: '<Q',
    TypeKind.Single: '<f',
    TypeKind.Double: '<d',
}

class Variable(BaseModel):
    def __init__(self):
        super().__init__()
        self.guid: uuid.UUID = uuid.UUID(int=0)
        self.name_offset: int = 0
        self.name: str = ""
        self.value_offset: int = 0
        self.expression_offset: int = 0
        self.type: TypeKind = TypeKind.Trigger
        self.flags: int = 0
        self.name_hash: int = 0
        
        self.value: Optional[Any] = None
        self.expression: Optional[UvarExpression] = None
        
    @property
    def is_vec3(self) -> bool:
        return (self.flags & UvarFlags.IsVec3) != 0
        
    @property
    def value_type(self) -> Optional[Type]:
        return get_python_type(self.type, self.flags)
        
    def reset_value(self):
        var_type = self.value_type
        if var_type is None:
            self.value = None
            return
            
        if var_type == list and self.is_vec3:
            if self.type in [TypeKind.Int8, TypeKind.Uint8]:
                self.value = [0, 0, 0]
            elif self.type in [TypeKind.Int16, TypeKind.Uint16]:
                self.value = [0, 0, 0]
            elif self.type == TypeKind.Int64:
                self.value = [0, 0, 0]
        elif var_type == str:
            self.value = ""
        elif var_type == bool:
            self.value = False
        elif var_type == int:
            self.value = 0
        elif var_type == float:
            self.value = 0.0
        elif var_type == uuid.UUID:
            self.value = uuid.UUID(int=0)
        elif var_type == Vec3:
            self.value = Vec3(0.0, 0.0, 0.0)
        elif var_type == Int3:
            self.value = Int3(0, 0, 0)
        elif var_type == Uint3:
            self.value = Uint3(0, 0, 0)
        elif var_type == Position:
            self.value = Position(0.0, 0.0, 0.0)
        elif var_type == tuple:
            if self.type == TypeKind.Vec2:
                self.value = (0.0, 0.0)
            elif self.type == TypeKind.Vec4:
                self.value = (0.0, 0.0, 0.0, 0.0)
        elif var_type == list and self.type == TypeKind.Matrix:
            self.value = [[0.0] * 4 for _ in range(4)]
        else:
            self.value = None
            
    def do_read(self, handler: FileHandler) -> bool:
        self.start_offset = handler.tell
        
        self.guid = handler.read_guid()
        self.name_offset = handler.read('<Q')
        self.value_offset = handler.read('<Q')
        self.expression_offset = handler.read('<Q')
        
        type_bits = handler.read('<I')
        self.type = TypeKind(type_bits & 0xFFFFFF)
        self.flags = (type_bits >> 24) & 0xFF
        
        self.name_hash = handler.read('<I')
        
        if self.name_offset > 0:
            with handler.seek_temp(self.name_offset):
                self.name = handler.read_wstring()
        else:
            self.name = ""
            
        if self.value_offset > 0:
            with handler.seek_temp(self.value_offset):
                self.read_value(handler)
        else:
            self.reset_value()
            
        if self.expression_offset > 0:
            with handler.seek_temp(self.expression_offset):
                self.expression = UvarExpression()
                if not self.expression.read(handler):
                    raise ValueError(f"Failed to read expression for variable {self.name}")
            
        return True
            
    def read_value(self, handler: FileHandler):
        if self.is_vec3:
            if self.type == TypeKind.Int8:
                self.value = [handler.read('<b') for _ in range(3)]
            elif self.type == TypeKind.Uint8:
                self.value = [handler.read('<B') for _ in range(3)]
            elif self.type == TypeKind.Int16:
                self.value = [handler.read('<h') for _ in range(3)]
            elif self.type == TypeKind.Uint16:
                self.value = [handler.read('<H') for _ in range(3)]
            elif self.type == TypeKind.Int32:
                self.value = Int3.unpack(handler.data, handler.tell)
                handler.skip(12)
            elif self.type == TypeKind.Uint32:
                self.value = Uint3.unpack(handler.data, handler.tell)
                handler.skip(12)
            elif self.type == TypeKind.Single:
                self.value = Vec3.unpack(handler.data, handler.tell)
                handler.skip(12)
            elif self.type == TypeKind.Double:
                self.value = Position.unpack(handler.data, handler.tell)
                handler.skip(24)
            else:
                raise Exception(f"Bad type {self.type}")
        else:
            if self.type == TypeKind.C8:
                str_offset = handler.read('<Q')
                with handler.seek_temp(str_offset):
                    self.value = handler.read_string('ascii')
            elif self.type == TypeKind.C16:
                str_offset = handler.read('<Q')
                with handler.seek_temp(str_offset):
                    self.value = handler.read_wstring()
            elif self.type == TypeKind.String:
                self.value = handler.read_wstring()
            elif self.type == TypeKind.Trigger:
                self.value = None
            elif self.type in TYPE_FORMATS:
                fmt = TYPE_FORMATS[self.type]
                self.value = handler.read(fmt)
                if self.type == TypeKind.Boolean:
                    self.value = bool(self.value)
            elif self.type == TypeKind.Vec2:
                self.value = (handler.read('<f'), handler.read('<f'))
            elif self.type == TypeKind.Vec3:
                self.value = Vec3.unpack(handler.data, handler.tell)
                handler.skip(12)
            elif self.type == TypeKind.Vec4:
                self.value = tuple(handler.read('<f') for _ in range(4))
            elif self.type == TypeKind.Matrix:
                self.value = [[handler.read('<f') for _ in range(4)] for _ in range(4)]
            elif self.type == TypeKind.GUID:
                self.value = handler.read_guid()
            elif self.type == TypeKind.Enum:
                self.value = handler.read('<i')
            else:
                raise Exception(f"Unhandled variable type {self.name} = {self.type}")
                
    def do_write(self, handler: FileHandler) -> bool:
        self.start_offset = handler.tell
        
        if self.name:
            self.name_hash = murmur3_hash(self.name.encode('utf-16le'))
        else:
            self.name_hash = murmur3_hash(b'')
            
        handler.write_guid(self.guid)
        handler.write('<Q', 0)  # name_offset - will be updated later
        handler.write('<Q', 0)  # value_offset - will be updated later
        handler.write('<Q', 0)  # expression_offset - will be updated later
        
        type_bits = (self.type.value & 0xFFFFFF) | ((self.flags & 0xFF) << 24)
        handler.write('<I', type_bits)
        handler.write('<I', self.name_hash)
        
        return True
            
    def write_value(self, handler: FileHandler):
        if self.value is None:
            return
            
        handler.write_at(self.start_offset + 24, '<Q', handler.tell)
        self.value_offset = handler.tell
        
        if self.is_vec3:
            if self.type == TypeKind.Int8:
                for v in self.value:
                    handler.write('<b', v)
            elif self.type == TypeKind.Uint8:
                for v in self.value:
                    handler.write('<B', v)
            elif self.type == TypeKind.Int16:
                for v in self.value:
                    handler.write('<h', v)
            elif self.type == TypeKind.Uint16:
                for v in self.value:
                    handler.write('<H', v)
            elif self.type == TypeKind.Int32:
                handler.write_bytes(self.value.pack())
            elif self.type == TypeKind.Uint32:
                handler.write_bytes(self.value.pack())
            elif self.type == TypeKind.Single:
                handler.write_bytes(self.value.pack())
            elif self.type == TypeKind.Double:
                handler.write_bytes(self.value.pack())
            else:
                raise Exception(f"Unhandled vec3 variable type {self.name} = {self.type}")
        else:
            if self.type == TypeKind.C8:
                handler.write('<Q', handler.tell + 8)
                if self.value is None:
                    handler.write_string("", 'ascii')
                else:
                    handler.write_string(self.value, 'ascii')
            elif self.type == TypeKind.C16:
                handler.write('<Q', handler.tell + 8)
                if self.value is None:
                    handler.write_wstring("")
                else:
                    handler.write_wstring(self.value)
            elif self.type == TypeKind.String:
                if self.value is None:
                    handler.write_wstring("")
                else:
                    handler.write_wstring(self.value)
            elif self.type == TypeKind.Trigger:
                pass 
            elif self.type in TYPE_FORMATS:
                fmt = TYPE_FORMATS[self.type]
                value = (1 if self.value else 0) if self.type == TypeKind.Boolean else self.value
                handler.write(fmt, value)
            elif self.type == TypeKind.Vec2:
                handler.write('<ff', *self.value)
            elif self.type == TypeKind.Vec3:
                handler.write_bytes(self.value.pack())
            elif self.type == TypeKind.Vec4:
                for v in self.value:
                    handler.write('<f', v)
            elif self.type == TypeKind.Matrix:
                for row in self.value:
                    for v in row:
                        handler.write('<f', v)
            elif self.type == TypeKind.GUID:
                handler.write_guid(self.value)
            elif self.type == TypeKind.Enum:
                handler.write('<i', self.value)
            else:
                raise Exception(f"Unhandled variable type {self.name} = {self.type}")
                
        handler.align_write(4)
        
    def write_expression(self, handler: FileHandler):
        if self.expression is None:
            return
            
        # Update expression offset
        handler.write_at(self.start_offset + 32, '<Q', handler.tell)
        self.expression_offset = handler.tell
        
        self.expression.write(handler)
        
    def __repr__(self) -> str:
        value_str = str(self.value)
        if isinstance(self.value, list) and len(value_str) > 50:
            value_str = f"[{len(self.value)} items]"
        return f"Variable(name='{self.name}', type={self.type.name}, value={value_str})"