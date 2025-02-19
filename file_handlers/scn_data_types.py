#From Enums_Internal

class ObjectData:
    def __init__(self, value: object):
        self.value = value

class ActionData:
    def __init__(self, value: object):
        self.value = value

class StructData:
    def __init__(self, value: object):
        self.value = value

class NativeObjectData:
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

class C8Data:
    def __init__(self, value: int):
        self.value = value

class C16Data:
    def __init__(self, value: int):
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

class MBStringData:
    def __init__(self, value: str):
        self.value = value

class EnumData: 
    def __init__(self, value: int):
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

class Uint4Data:
    def __init__(self, x: int, y: int, z: int, w: int):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

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
    def __init__(self, x: float, y: float, z: float):
        self.x = x
        self.y = y
        self.z = z

class Float4Data:
    def __init__(self, x: float, y: float, z: float, w: float):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

class Float3x3Data:
    def __init__(self, values: list):
        self.values = values  # 9 floats (3x3 matrix)

class Float3x4Data:
    def __init__(self, values: list):
        self.values = values  # 12 floats (3x4 matrix)

class Float4x3Data:
    def __init__(self, values: list):
        self.values = values  # 12 floats (4x3 matrix)

class Float4x4Data:
    def __init__(self, values: list):
        self.values = values  # 16 floats (4x4 matrix)

class Half2Data:
    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y

class Half4Data:
    def __init__(self, x: float, y: float, z: float, w: float):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

class Mat3Data:
    def __init__(self, values: list):
        self.values = values  # 9 floats (3x3 matrix)

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

class VecU4Data:
    def __init__(self, x: int, y: int, z: int, w: int):
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

class DateTimeData:
    def __init__(self, value: int):
        self.value = value  # Unix timestamp

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

class TaperedCapsuleData:
    def __init__(self, start: Vec3Data, end: Vec3Data, start_radius: float, end_radius: float):
        self.start = start
        self.end = end
        self.start_radius = start_radius
        self.end_radius = end_radius

class ConeData:
    def __init__(self, position: Vec3Data, direction: Vec3Data, angle: float, distance: float):
        self.position = position
        self.direction = direction
        self.angle = angle
        self.distance = distance

class LineData:
    def __init__(self, start: Vec3Data, direction: Vec3Data):
        self.start = start
        self.direction = direction

class LineSegmentData:
    def __init__(self, start: Vec3Data, end: Vec3Data):
        self.start = start
        self.end = end

class OBBData:
    def __init__(self, values: list):
        self.values = values  # 20 floats

class PlaneData:
    def __init__(self, normal: Vec3Data, distance: float):
        self.normal = normal
        self.distance = distance

class PlaneXZData:
    def __init__(self, normal: Vec3Data, distance: float):
        self.normal = normal
        self.distance = distance

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

class RayData:
    def __init__(self, origin: Vec3Data, direction: Vec3Data):
        self.origin = origin
        self.direction = direction

class RayYData:
    def __init__(self, origin: Vec3Data, direction: Vec3Data):
        self.origin = origin
        self.direction = direction

class SegmentData:
    def __init__(self, start: Vec3Data, end: Vec3Data):
        self.start = start
        self.end = end

class SizeData:
    def __init__(self, width: float, height: float):
        self.width = width
        self.height = height

class SphereData:
    def __init__(self, center: Vec3Data, radius: float):
        self.center = center
        self.radius = radius

class TriangleData:
    def __init__(self, p1: Vec3Data, p2: Vec3Data, p3: Vec3Data):
        self.p1 = p1
        self.p2 = p2
        self.p3 = p3

class CylinderData:
    def __init__(self, center: Vec3Data, radius: float, height: float):
        self.center = center
        self.radius = radius
        self.height = height

class EllipsoidData:
    def __init__(self, center: Vec3Data, radius_x: float, radius_y: float, radius_z: float):
        self.center = center
        self.radius_x = radius_x
        self.radius_y = radius_y
        self.radius_z = radius_z

class AreaData:
    def __init__(self, min: Vec2Data, max: Vec2Data):
        self.min = min
        self.max = max

class TorusData:
    def __init__(self, center: Vec3Data, normal: Vec3Data, major_radius: float, minor_radius: float):
        self.center = center
        self.normal = normal
        self.major_radius = major_radius
        self.minor_radius = minor_radius

class RectData:
    def __init__(self, min_x: float, min_y: float, max_x: float, max_y: float):
        self.min_x = min_x
        self.min_y = min_y
        self.max_x = max_x
        self.max_y = max_y

class Rect3DData:
    def __init__(self, center: Vec3Data, size: Vec3Data):
        self.center = center
        self.size = size

class FrustumData:
    def __init__(self, near: float, far: float, fov: float, aspect: float):
        self.near = near
        self.far = far
        self.fov = fov
        self.aspect = aspect

class KeyFrameData:
    def __init__(self, time: float, value: float):
        self.time = time
        self.value = value

class UriData:
    def __init__(self, value: str):
        self.value = value

class GameObjectRefData:
    def __init__(self, guid_str: str):
        self.guid_str = guid_str

class RuntimeTypeData:
    def __init__(self, type_name: str):
        self.type_name = type_name

class SfixData:
    def __init__(self, value: int):
        self.value = value

class Sfix2Data:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y

class Sfix3Data:
    def __init__(self, x: int, y: int, z: int):
        self.x = x
        self.y = y
        self.z = z

class Sfix4Data:
    def __init__(self, x: int, y: int, z: int, w: int):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

class PositionData:
    def __init__(self, x: float, y: float, z: float):
        self.x = x
        self.y = y
        self.z = z

class F16Data:
    def __init__(self, value: float):
        self.value = value

class DecimalData:
    def __init__(self, value: float):
        self.value = value
