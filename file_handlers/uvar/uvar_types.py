from enum import IntEnum, IntFlag
from dataclasses import dataclass
import struct
import uuid
from typing import Optional, Any, Union, List, Type

UVAR_MAGIC = 0x72617675  # 'uvar'
UVAR_EXTENSION = ".uvar"

class NodeValueType(IntEnum):
    Unknown = 0
    UInt32Maybe = 6
    Int32 = 7
    Single = 8
    Guid = 18
    SetVariable = 1
    GetVariable = 2

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
    IsVec3 = 0x40  # Array type

@dataclass
class NodeConnection:
    src_node: int = 0
    src_port: int = 0
    dst_node: int = 0
    dst_port: int = 0
    
    def pack(self) -> bytes:
        return struct.pack("<IIII", self.src_node, self.src_port, self.dst_node, self.dst_port)
    
    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> 'NodeConnection':
        values = struct.unpack_from("<IIII", data, offset)
        return cls(*values)

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
class Int3:
    x: int
    y: int
    z: int
    
    def pack(self) -> bytes:
        return struct.pack("<iii", self.x, self.y, self.z)
    
    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> 'Int3':
        values = struct.unpack_from("<iii", data, offset)
        return cls(*values)

@dataclass
class Uint3:
    x: int
    y: int
    z: int
    
    def pack(self) -> bytes:
        return struct.pack("<III", self.x, self.y, self.z)
    
    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> 'Uint3':
        values = struct.unpack_from("<III", data, offset)
        return cls(*values)

@dataclass
class Position:
    x: float
    y: float
    z: float
    
    def pack(self) -> bytes:
        return struct.pack("<ddd", self.x, self.y, self.z)
    
    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> 'Position':
        values = struct.unpack_from("<ddd", data, offset)
        return cls(*values)

def get_python_type(type_kind: TypeKind, flags: int) -> Optional[Type]:
    if flags & UvarFlags.IsVec3:
        type_map = {
            TypeKind.Int8: List[int],    # sbyte[3]
            TypeKind.Uint8: List[int],   # byte[3]
            TypeKind.Int16: List[int],   # short[3]
            TypeKind.Uint16: List[int],  # ushort[3]
            TypeKind.Int32: Int3,
            TypeKind.Uint32: Uint3,
            TypeKind.Int64: List[int],   # long[3]
            TypeKind.Single: Vec3,
            TypeKind.Double: Position,
        }
        return type_map.get(type_kind)
    
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
        TypeKind.Vec2: tuple,  # (float, float)
        TypeKind.Vec3: Vec3,
        TypeKind.Vec4: tuple,  # (float, float, float, float)
        TypeKind.Matrix: list,  # 4x4 matrix
        TypeKind.GUID: uuid.UUID,
    }
    return type_map.get(type_kind)