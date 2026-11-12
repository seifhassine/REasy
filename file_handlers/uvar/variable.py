from typing import Optional, Any, Type
import uuid

from .base_model import BaseModel, FileHandler
from .uvar_types import TypeKind, UvarFlags, ValueRange, Vec3, get_python_type
lazy from .uvar_expression import UvarExpression

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

def _read_offset_ascii(handler: FileHandler) -> str:
    off = handler.read('<Q')
    with handler.seek_temp(off):
        return handler.read_string('ascii')

def _read_offset_wstring(handler: FileHandler) -> str:
    off = handler.read('<Q')
    with handler.seek_temp(off):
        return handler.read_wstring()

NONVEC_READERS = {
    TypeKind.C8:      _read_offset_ascii,
    TypeKind.C16:     _read_offset_wstring,
    TypeKind.String:  lambda h: h.read_wstring(),
    TypeKind.Trigger: lambda h: None,
    TypeKind.Vec2:    lambda h: tuple(h.read('<ff')),
    TypeKind.Vec3:    lambda h: Vec3(*h.read('<fff')),
    TypeKind.Vec4:    lambda h: tuple(h.read('<ffff')),
    TypeKind.Matrix:  lambda h: [[h.read('<f') for _ in range(4)] for _ in range(4)],
    TypeKind.GUID:    lambda h: h.read_guid(),
    TypeKind.Enum:    lambda h: h.read('<i'),
}

NONVEC_WRITERS = {
    TypeKind.C8:      lambda h, v: (h.write('<Q', h.tell + 8), h.write_string(v or '', 'ascii')),
    TypeKind.C16:     lambda h, v: (h.write('<Q', h.tell + 8), h.write_wstring(v or '')),
    TypeKind.String:  lambda h, v: h.write_wstring(v or ''),
    TypeKind.Trigger: lambda h, v: None,
    TypeKind.Vec2:    lambda h, v: h.write('<ff', *(v or (0.0, 0.0))),
    TypeKind.Vec3:    lambda h, v: h.write_bytes(v.pack()),
    TypeKind.Vec4:    lambda h, v: [h.write('<f', x) for x in v],
    TypeKind.Matrix:  lambda h, v: [h.write('<f', x) for row in v for x in row],
    TypeKind.GUID:    lambda h, v: h.write_guid(v),
    TypeKind.Enum:    lambda h, v: h.write('<i', v),
}

_UNSET = sentinel("_UNSET")
_DEFAULT_VALUE_FACTORIES = {
    str: str,
    bool: bool,
    int: int,
    float: float,
    uuid.UUID: lambda: uuid.UUID(int=0),
    Vec3: lambda: Vec3(0.0, 0.0, 0.0),
}


def _create_default_value(var_type: Type, type_kind: TypeKind):
    if var_type is ValueRange:
        zero = 0.0 if type_kind in (TypeKind.Single, TypeKind.Double) else 0
        return ValueRange(zero, zero, zero)
    if var_type is tuple:
        tuple_size = {TypeKind.Vec2: 2, TypeKind.Vec4: 4}.get(type_kind)
        return (0.0,) * tuple_size if tuple_size else _UNSET
    if var_type is list and type_kind == TypeKind.Matrix:
        return [[0.0] * 4 for _ in range(4)]

    factory = _DEFAULT_VALUE_FACTORIES.get(var_type)
    return factory() if factory else None


def _write_nonvec_value(
    handler: FileHandler,
    type_kind: TypeKind,
    name: str,
    value: Any,
):
    writer = NONVEC_WRITERS.get(type_kind)
    if writer is not None:
        writer(handler, value)
        return

    fmt = TYPE_FORMATS.get(type_kind)
    if fmt is None:
        raise Exception(f"Unhandled variable type {name} = {type_kind}")

    out_val = value
    if type_kind == TypeKind.Boolean:
        out_val = 1 if value else 0
    handler.write(fmt, out_val)


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
        
        self._original_value_offset: Optional[int] = None
        
    @property
    def has_range(self) -> bool:
        return bool(self.flags & UvarFlags.HasRange)

    @property
    def value_type(self) -> Optional[Type]:
        return get_python_type(self.type, self.flags)
        
    def reset_value(self):
        var_type = self.value_type
        if var_type is None:
            self.value = None
            return

        default_value = _create_default_value(var_type, self.type)
        if default_value is not _UNSET:
            self.value = default_value
            
    def read_header(self, handler: FileHandler) -> bool:
        self.start_offset = handler.tell
        
        self.guid = handler.read_guid()
        self.name_offset = handler.read('<Q')
        self.value_offset = handler.read('<Q')
        self.expression_offset = handler.read('<Q')
        
        self._original_value_offset = self.value_offset
        
        type_bits = handler.read('<I')
        self.type = TypeKind(type_bits & 0xFFFFFF)
        self.flags = (type_bits >> 24) & 0xFF
        
        self.name_hash = handler.read('<I')
        
        if self.name_offset > 0:
            with handler.seek_temp(self.name_offset):
                self.name = handler.read_wstring()
        else:
            self.name = ""
        
        return True
            
    def do_read(self, handler: FileHandler) -> bool:
        if not self.read_header(handler):
            return False
        
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
        if self.has_range:
            self.value = ValueRange(*handler.read("<" + TYPE_FORMATS[self.type][1] * 3))
            return
        reader = NONVEC_READERS.get(self.type)
        if reader is not None:
            self.value = reader(handler)
            return
        if self.type in TYPE_FORMATS:
            fmt = TYPE_FORMATS[self.type]
            val = handler.read(fmt)
            self.value = bool(val) if self.type == TypeKind.Boolean else val
            return
        raise Exception(f"Unhandled variable type {self.name} = {self.type}")
 
    def do_write(self, handler: FileHandler) -> bool:
        self.start_offset = handler.tell
        handler.write_guid(self.guid)
        handler.write('<Q', 0)  # name_offset
        handler.write('<Q', 0)  # value_offset
        handler.write('<Q', 0)  # expression_offset
        type_bits = (self.type.value & 0xFFFFFF) | ((self.flags & 0xFF) << 24)
        handler.write('<I', type_bits)
        handler.write('<I', self.name_hash)
        return True

    def write_value(self, handler: FileHandler, value_offset_override: Optional[int] = None):
        if self.value is None:
            return
        handler.align_write(4)
        declared_offset = value_offset_override if value_offset_override is not None else handler.tell
        handler.write_at(self.start_offset + 24, '<Q', declared_offset)
        self.value_offset = declared_offset
        if self.has_range:
            handler.write("<" + TYPE_FORMATS[self.type][1] * 3, self.value.value, self.value.min, self.value.max)
        else:
            _write_nonvec_value(handler, self.type, self.name, self.value)

    def write_expression(self, handler: FileHandler):
        if self.expression is None:
            return
            
        handler.align_write(16)
        handler.write_at(self.start_offset + 32, '<Q', handler.tell)
        self.expression_offset = handler.tell
        
        self.expression.write(handler)
        
    def __repr__(self) -> str:
        value_str = str(self.value)
        if isinstance(self.value, list) and len(value_str) > 50:
            value_str = f"[{len(self.value)} items]"
        return f"Variable(name='{self.name}', type={self.type.name}, value={value_str})"
