#From Enums_Internal

class ArrayData:
    def __init__(self, values: list, element_type: type):
        self.values = values
        self.element_type = element_type

class ObjectData:
    def __init__(self, value: object):
        self.value = value

class ResourceData:
    def __init__(self, value: object):
        self.value = value

class UserDataData:
    def __init__(self, value: object):
        self.value = value

class BoolData:
    def __init__(self, value: bool):
        self.value = value

class S8Data:
    def __init__(self, value: int):
        self.value = value

class U8Data:
    def __init__(self, value: int):
        self.value = value

class S16Data:
    def __init__(self, value: int):
        self.value = value

class U16Data:
    def __init__(self, value: int):
        self.value = value

class S32Data:
    def __init__(self, value: int):
        self.value = value

class U32Data:
    def __init__(self, value: int):
        self.value = value

class S64Data:
    def __init__(self, value: int):
        self.value = value

class U64Data:
    def __init__(self, value: int):
        self.value = value

class F32Data:
    def __init__(self, value: float):
        self.value = value

class F64Data:
    def __init__(self, value: float):
        self.value = value

class StringData:
    def __init__(self, value: str):
        self.value = value

class Uint2Data:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y

class Uint3Data:
    def __init__(self, x: int, y: int, z: int):
        self.x = x
        self.y = y 
        self.z = z

class Int2Data:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y

class Int3Data:
    def __init__(self, x: int, y: int, z: int):
        self.x = x
        self.y = y
        self.z = z

class Int4Data:
    def __init__(self, x: int, y: int, z: int, w: int):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

class Float2Data:
    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y

class Float3Data:
    def __init__(self, x: float, y: float, z: "float"):
        self.x = x
        self.y = y
        self.z = z

class Float4Data:
    def __init__(self, x: float, y: float, z: float, w: float):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

class Mat4Data:
    def __init__(self, values: list):
        self.values = values  # 16 floats (4x4 matrix)

class Vec2Data:
    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y

class Vec3Data:
    def __init__(self, x: float, y: float, z: float):
        self.x = x
        self.y = y
        self.z = z

class Vec4Data:
    def __init__(self, x: float, y: float, z: float, w: float):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

class QuaternionData:
    def __init__(self, x: float, y: float, z: float, w: float):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

class GuidData:
    def __init__(self, guid_str: str):
        self.guid_str = guid_str

class ColorData:
    def __init__(self, r: float, g: float, b: float, a: float):
        self.r = r
        self.g = g
        self.b = b
        self.a = a

class AABBData:
    def __init__(self, min_x: float, min_y: float, min_z: float, 
                 max_x: float, max_y: float, max_z: float):
        self.min = Vec3Data(min_x, min_y, min_z)
        self.max = Vec3Data(max_x, max_y, max_z)

class CapsuleData:
    def __init__(self, start: Vec3Data, end: Vec3Data, radius: float):
        self.start = start
        self.end = end
        self.radius = radius

class ConeData:
    def __init__(self, position: Vec3Data, direction: Vec3Data, angle: float, distance: float):
        self.position = position
        self.direction = direction
        self.angle = angle
        self.distance = distance

class LineSegmentData:
    def __init__(self, start: Vec3Data, end: Vec3Data):
        self.start = start
        self.end = end

class OBBData:
    def __init__(self, values: list):
        self.values = values  # 20 floats

class PointData:
    def __init__(self, x: float, y: float, z: float):
        self.x = x
        self.y = y
        self.z = z

class RangeData:
    def __init__(self, min: float, max: float):
        self.min = min
        self.max = max

class RangeIData:
    def __init__(self, min: int, max: int):
        self.min = min
        self.max = max

class SizeData:
    def __init__(self, width: float, height: float):
        self.width = width
        self.height = height

class SphereData:
    def __init__(self, center: Vec3Data, radius: float):
        self.center = center
        self.radius = radius

class CylinderData:
    def __init__(self, center: Vec3Data, radius: float, height: float):
        self.center = center
        self.radius = radius
        self.height = height

class AreaData:
    def __init__(self, min: Vec2Data, max: Vec2Data):
        self.min = min
        self.max = max

class RectData:
    def __init__(self, min_x: float, min_y: float, max_x: float, max_y: float):
        self.min_x = min_x
        self.min_y = min_y
        self.max_x = max_x
        self.max_y = max_y

class GameObjectRefData:
    def __init__(self, guid_str: str):
        self.guid_str = guid_str

class RuntimeTypeData:
    def __init__(self, type_name: str):
        self.type_name = type_name

class MaybeObject:
    def __init__(self):
        pass
class MaybeObject2:
    def __init__(self):
        pass
#Types in re4r

TYPE_MAPPING = {
    "bool": BoolData,
    "s32": S32Data,
    "int": S32Data,
    "uint": U32Data,
    "f32": F32Data,
    "f64": F64Data,
    "float": F32Data,
    "string": StringData,
    "resource": ResourceData,
    "gameobjectref": GameObjectRefData,
    "object": ObjectData,
    "vec3": Vec3Data,
    "vec4": Vec4Data,
    "obb": OBBData,
    "userdata": UserDataData,
    "vec2": Vec2Data,
    "vec3": Vec3Data,
    "vec4": Vec4Data,
    "uint2": Uint2Data,
    "uint3": Uint3Data,
    "u8": U8Data,
    "u16": U16Data,
    "u32": U32Data,
    "u64": U64Data,
    "sphere": SphereData,
    "size": SizeData,
    "s8": S8Data,
    "s16": S16Data,
    "s32": S32Data,
    "s64": S64Data,
    "runtimetype": RuntimeTypeData,
    "rect": RectData,
    "range": RangeData,
    "rangei": RangeIData,
    "quaternion": QuaternionData,
    "point": PointData,
    "mat4": Mat4Data,
    "linesegment": LineSegmentData,
    "int2": Int2Data,
    "int3": Int3Data,
    "int4": Int4Data,
    "guid": GuidData,
    "float2": Float2Data,
    "float3": Float3Data,
    "float4": Float4Data,
    "cylinder": CylinderData,
    "cone": ConeData,
    "color": ColorData,
    "capsule": CapsuleData,
    "area": AreaData,
    "aabb": AABBData,
    "data": None,

}

def get_type_class(field_type: str, field_size: int = 4, is_native: bool = False, is_array: bool = False) -> type:
    """Get the appropriate data type class based on field type and size"""
    field_type = field_type.lower()
    
    if field_type == "data":
        if field_size == 16:
            return Vec4Data
        elif field_size == 80:
            return OBBData
        elif field_size == 4 and is_native:
            return MaybeObject
        elif field_size == 8:
            return U64Data
        elif field_size == 1:
            return U8Data
        
    if(is_array) and is_native and field_size == 4 and (field_type in ("s32", "u32")): #booleans failing first is less expensive on cpu
        return MaybeObject
    
    return TYPE_MAPPING.get(field_type, U32Data) 
