from enum import IntEnum, IntFlag
from dataclasses import dataclass
import struct
import uuid
from typing import Optional, Type

UVAR_MAGIC = 0x72617675  # 'uvar'

class ParamType(IntEnum):
    Unknown = 0
    Bool = 1
    U8 = 2
    S8 = 3
    U16 = 4
    S16 = 5
    S32 = 6
    U32 = 7
    S64 = 8
    U64 = 9
    F32 = 10
    F64 = 11
    Str8 = 12
    Str16 = 13
    Address = 14
    Object = 15
    Vec2 = 16
    Vec3 = 17
    Vec4 = 18
    Matrix = 19
    Guid = 20

class TypeKind(IntEnum):
    Unknown = 0
    Enum = 1
    Boolean = 2
    Int8 = 3
    Uint8 = 4
    Int16 = 5
    Uint16 = 6
    Int32 = 7
    Uint32 = 8
    Int64 = 9
    Uint64 = 10
    Single = 11
    Double = 12
    C8 = 13
    C16 = 14
    String = 15
    Trigger = 16
    Vec2 = 17
    Vec3 = 18
    Vec4 = 19
    Matrix = 20
    GUID = 21
    Num = 22

class UvarFlags(IntFlag):
    IsConst = 0x20
    HasRange = 0x40
    HasTreeRange = 0x80

@dataclass
class NodeConnection:
    input_node: int = 0
    input_pin: int = 0
    output_node: int = 0
    output_pin: int = 0

@dataclass
class Vec3:
    x: float
    y: float
    z: float
    
    def pack(self) -> bytes:
        return struct.pack("<fff", self.x, self.y, self.z)
    
    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> 'Vec3':
        values = struct.unpack_from("<fff", data, offset)
        return cls(*values)

@dataclass
class ValueRange:
    value: float
    min: float
    max: float

def get_python_type(type_kind: TypeKind, flags: int) -> Optional[Type]:
    if flags & UvarFlags.HasRange:
        return ValueRange

    type_map = {
        TypeKind.Enum: int,
        TypeKind.Boolean: bool,
        TypeKind.Int8: int,
        TypeKind.Uint8: int,
        TypeKind.Int16: int,
        TypeKind.Uint16: int,
        TypeKind.Int32: int,
        TypeKind.Uint32: int,
        TypeKind.Int64: int,
        TypeKind.Uint64: int,
        TypeKind.Single: float,
        TypeKind.Double: float,
        TypeKind.C8: str,
        TypeKind.C16: str,
        TypeKind.String: str,
        TypeKind.Trigger: type(None),
        TypeKind.Vec2: tuple,
        TypeKind.Vec3: Vec3,
        TypeKind.Vec4: tuple,
        TypeKind.Matrix: list,
        TypeKind.GUID: uuid.UUID,
    }
    return type_map.get(type_kind)