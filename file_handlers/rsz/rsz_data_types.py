#From Enums_Internal
from types import MappingProxyType

class ArrayData:
    """Array container that stores values and element type"""
    def __init__(self, values=None, element_class=None, orig_type=""):
        self.values = values if values is not None else []
        self.element_class = element_class
        self.orig_type = orig_type

    def add_element(self, element):
        if self.element_class and not isinstance(element, self.element_class):
            raise TypeError(f"Expected {self.element_class.__name__}, got {type(element).__name__}")

        self.values.append(element)
        return len(self.values) - 1

    @classmethod
    def parse(cls, _ctx):
        raise NotImplementedError("ArrayData parsing is handled separately")

class StructData:
    """Container for struct type that can hold 0 or more embedded structures"""
    def __init__(self, values=None, orig_type: str = ""):
        self.values = values if values is not None else []
        self.orig_type = orig_type

    def add_element(self, element):
        """Add an element to the struct if it matches the expected type"""
        if not isinstance(element, dict):
            raise TypeError(f"Expected dict for struct element, got {type(element).__name__}")
        self.values.append(element)
        return len(self.values) - 1

    @classmethod
    def parse(cls, _ctx):
        raise NotImplementedError("StructData parsing is handled separately")

class ObjectData:
    def __init__(self, value: int = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_uint, ctx.field_size)
        ctx.current_children.append(value)
        ctx.set_parent(value, ctx.current_instance_index)
        return cls(value, ctx.original_type)

class ResourceData:
    def __init__(self, value: str = "", orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_string_utf16()
        return cls(value, ctx.original_type)

class UserDataData:
    def __init__(self, value: int = 0, string: str = "", orig_type: str = ""):
        self.value = value
        self.string = string
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        instance_id = ctx.read_value(ctx.unpack_uint, ctx.field_size)
        value = ""
        rui = ctx.rsz_userdata_by_id.get(instance_id)
        if rui:
            value = ctx.rsz_userdata_map.get(rui, "")
        return cls(instance_id, value, ctx.original_type)

class BoolData:
    def __init__(self, value: bool = False, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        raw = ctx.read_bytes(ctx.field_size)
        value = bool(raw[0]) if len(raw) > 0 else False
        return cls(value, ctx.original_type)

class S8Data:
    def __init__(self, value: int = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_sbyte, ctx.field_size)
        return cls(value, ctx.original_type)

class U8Data:
    def __init__(self, value: int = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_ubyte, ctx.field_size)
        return cls(value, ctx.original_type)

class S16Data:
    def __init__(self, value: int = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_short, ctx.field_size)
        return cls(value, ctx.original_type)

class U16Data:
    def __init__(self, value: int = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_ushort, ctx.field_size)
        return cls(value, ctx.original_type)

class S32Data:
    def __init__(self, value: int = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_int, ctx.field_size)
        return cls(value, ctx.original_type)

class U32Data:
    def __init__(self, value: int = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_uint, ctx.field_size)
        return cls(value, ctx.original_type)

class S64Data:
    def __init__(self, value: int = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_long, ctx.field_size)
        return cls(value, ctx.original_type)

class U64Data:
    def __init__(self, value: int = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_ulong, ctx.field_size)
        return cls(value, ctx.original_type)

class F32Data:
    def __init__(self, value: float = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_float, ctx.field_size)
        return cls(value, ctx.original_type)

class F64Data:
    def __init__(self, value: float = 0, orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_value(ctx.unpack_double, ctx.field_size)
        return cls(value, ctx.original_type)

class StringData:
    def __init__(self, value: str = "", orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_string_utf16()
        return cls(value, ctx.original_type)

class Uint2Data:
    def __init__(self, x: int = 0, y: int = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_2int, 8)
        return cls(vals[0], vals[1], ctx.original_type)

class Uint3Data:
    def __init__(self, x: int = 0, y: int = 0, z: int = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.z = z
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_3int, 12)
        return cls(vals[0], vals[1], vals[2], ctx.original_type)

class Int2Data:
    def __init__(self, x: int = 0, y: int = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_2int, 8)
        return cls(vals[0], vals[1], ctx.original_type)

class Int3Data:
    def __init__(self, x: int = 0, y: int = 0, z: int = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.z = z
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_3int, ctx.field_size)
        return cls(vals[0], vals[1], vals[2], ctx.original_type)

class Int4Data:
    def __init__(self, x: int = 0, y: int = 0, z: int = 0, w: int = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.z = z
        self.w = w
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        values = [ctx.read_value(ctx.unpack_int, 4) for _ in range(4)]
        return cls(values[0], values[1], values[2], values[3], ctx.original_type)

class Float2Data:
    def __init__(self, x: float = 0, y: float = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_2float, ctx.field_size)
        return cls(vals[0], vals[1], ctx.original_type)

class Float3Data:
    def __init__(self, x: float = 0, y: float = 0, z: float = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.z = z
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_3float, ctx.field_size)
        return cls(vals[0], vals[1], vals[2], ctx.original_type)

class Float4Data:
    def __init__(self, x: float = 0, y: float = 0, z: float = 0, w: float = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.z = z
        self.w = w
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_4float, ctx.field_size)
        return cls(vals[0], vals[1], vals[2], vals[3], ctx.original_type)

class Mat4Data:
    def __init__(self, values = (0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0), orig_type: str = ""):
        self.values = []
        if isinstance(values, (tuple, list)):
            self.values.extend(float(v) for v in values[:16])
        while len(self.values) < 16:
            self.values.append(0.0)
        self.orig_type = orig_type

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)

    def __getitem__(self, idx):
        return self.values[idx]

    def __str__(self):
        return f"MAT4({', '.join(f'{v:.6f}' for v in self.values)})"

    @classmethod
    def parse(cls, ctx):
        values = ctx.read_struct(ctx.unpack_16float, ctx.field_size)
        return cls(values, ctx.original_type)

class Vec2Data:
    def __init__(self, x: float = 0, y: float = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_2float, ctx.field_size)
        return cls(vals[0], vals[1], ctx.original_type)

class Vec3Data:
    def __init__(self, x: float = 0, y: float = 0, z: float = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.z = z
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_4float, ctx.field_size)
        return cls(vals[0], vals[1], vals[2], ctx.original_type)

class Vec3ColorData:
    def __init__(self, x: float = 0, y: float = 0, z: float = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.z = z
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_4float, ctx.field_size)
        return cls(vals[0], vals[1], vals[2], ctx.original_type)

class Vec4Data:
    def __init__(self, x: float = 0, y: float = 0, z: float = 0, w: float = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.z = z
        self.w = w
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_4float, ctx.field_size)
        return cls(vals[0], vals[1], vals[2], vals[3], ctx.original_type)

class QuaternionData:
    def __init__(self, x: float = 0, y: float = 0, z: float = 0, w: float = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.z = z
        self.w = w
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_4float, ctx.field_size)
        return cls(vals[0], vals[1], vals[2], vals[3], ctx.original_type)

class GuidData:
    def __init__(self, guid_str: str = None, raw_bytes: bytes = None, orig_type: str = ""):
        if not guid_str:
            guid_str = "00000000-0000-0000-0000-000000000000"
        if not raw_bytes:
            raw_bytes = b'\0' * 16

        self.guid_str = guid_str
        self.raw_bytes = raw_bytes  # Store original bytes
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        guid_str, raw_bytes = ctx.read_guid(ctx.field_size)
        return cls(guid_str, raw_bytes, ctx.original_type)

class ColorData:
    def __init__(self, r: int = 0, g: int = 0, b: int = 0, a: int = 0, orig_type: str = ""):
        self.r = r
        self.g = g
        self.b = b
        self.a = a
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_4ubyte, ctx.field_size)
        return cls(vals[0], vals[1], vals[2], vals[3], ctx.original_type)

class AABBData:
    def __init__(self, min_x: float = 0, min_y: float = 0, min_z: float = 0,
                 max_x: float = 0, max_y: float = 0, max_z: float = 0, orig_type: str = ""):
        self.min = Vec3Data(min_x, min_y, min_z)
        self.max = Vec3Data(max_x, max_y, max_z)
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        min_vals = ctx.read_struct(ctx.unpack_4float, 16)
        max_vals = ctx.read_struct(ctx.unpack_4float, 16)
        return cls(min_vals[0], min_vals[1], min_vals[2], max_vals[0], max_vals[1], max_vals[2], ctx.original_type)

class CapsuleData:
    def __init__(self, start: Vec3Data = Vec3Data(0,0,0, ""), end: Vec3Data = Vec3Data(0,0,0, ""), radius: float = 0, orig_type: str = ""):
        self.start = start
        self.end = end
        self.radius = radius
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        start_vals = ctx.read_struct(ctx.unpack_4float, 16)
        end_vals = ctx.read_struct(ctx.unpack_4float, 16)
        radius_vals = ctx.read_struct(ctx.unpack_4float, 16)
        start_vec = Vec3Data(start_vals[0], start_vals[1], start_vals[2], "Vec3")
        end_vec = Vec3Data(end_vals[0], end_vals[1], end_vals[2], "Vec3")
        radius = radius_vals[0]
        return cls(start_vec, end_vec, radius, ctx.original_type)

class AreaData:
    def __init__(self, p0: Float2Data = Float2Data(0,0, ""), p1: Float2Data = Float2Data(0,0, ""), p2: Float2Data = Float2Data(0,0, ""), p3: Float2Data = Float2Data(0,0, ""), height: float = 0, bottom: float = 0, orig_type: str = ""):
        self.p0 = p0
        self.p1 = p1
        self.p2 = p2
        self.p3 = p3
        self.height = height
        self.bottom = bottom
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        p0_vals = ctx.read_struct(ctx.unpack_2float, 8)
        p1_vals = ctx.read_struct(ctx.unpack_2float, 8)
        p2_vals = ctx.read_struct(ctx.unpack_2float, 8)
        p3_vals = ctx.read_struct(ctx.unpack_2float, 8)
        p0 = Float2Data(p0_vals[0], p0_vals[1], "Float2")
        p1 = Float2Data(p1_vals[0], p1_vals[1], "Float2")
        p2 = Float2Data(p2_vals[0], p2_vals[1], "Float2")
        p3 = Float2Data(p3_vals[0], p3_vals[1], "Float2")
        height = ctx.read_value(ctx.unpack_float, 4)
        bottom = ctx.read_value(ctx.unpack_float, 4)
        ctx.skip(8)
        return cls(p0, p1, p2, p3, height, bottom, ctx.original_type)

class AreaDataOld:
    def __init__(self, p0: Vec2Data = Vec2Data(0,0, ""), p1: Vec2Data = Vec2Data(0,0, ""), p2: Vec2Data = Vec2Data(0,0, ""), p3: Vec2Data = Vec2Data(0,0, ""), height: float = 0, bottom: float = 0, orig_type: str = ""):
        self.p0 = p0
        self.p1 = p1
        self.p2 = p2
        self.p3 = p3
        self.height = height
        self.bottom = bottom
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        p0_vals = ctx.read_struct(ctx.unpack_4float, 16)
        p1_vals = ctx.read_struct(ctx.unpack_4float, 16)
        p2_vals = ctx.read_struct(ctx.unpack_4float, 16)
        p3_vals = ctx.read_struct(ctx.unpack_4float, 16)
        p0 = Vec2Data(p0_vals[0], p0_vals[1], "Vec2")
        p1 = Vec2Data(p1_vals[0], p1_vals[1], "Vec2")
        p2 = Vec2Data(p2_vals[0], p2_vals[1], "Vec2")
        p3 = Vec2Data(p3_vals[0], p3_vals[1], "Vec2")
        height = ctx.read_value(ctx.unpack_float, 4)
        bottom = ctx.read_value(ctx.unpack_float, 4)
        ctx.skip(8)
        return cls(p0, p1, p2, p3, height, bottom, ctx.original_type)

class ConeData:
    def __init__(self, position: Vec3Data = Vec3Data(0,0,0, ""), direction: Vec3Data = Vec3Data(0,0,0, ""), angle: float = 0, distance: float = 0, orig_type: str = ""):
        self.position = position
        self.direction = direction
        self.angle = angle
        self.distance = distance
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        position_vals = ctx.read_struct(ctx.unpack_4float, 16)
        direction_vals = ctx.read_struct(ctx.unpack_4float, 16)

        position = Vec3Data(position_vals[0], position_vals[1], position_vals[2], "Vec3")
        direction = Vec3Data(direction_vals[0], direction_vals[1], direction_vals[2], "Vec3")

        return cls(position, direction, direction_vals[3], position_vals[3], ctx.original_type)

class LineSegmentData:
    def __init__(self, start: Vec3Data = Vec3Data(0,0,0, ""), end: Vec3Data = Vec3Data(0,0,0, ""), orig_type: str = ""):
        self.start = start
        self.end = end
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        start_vals = ctx.read_struct(ctx.unpack_4float, 16)
        end_vals = ctx.read_struct(ctx.unpack_4float, 16)
        start = Vec3Data(start_vals[0], start_vals[1], start_vals[2], "Vec3")
        end = Vec3Data(end_vals[0], end_vals[1], end_vals[2], "Vec3")
        return cls(start, end, ctx.original_type)

class OBBData:
    def __init__(self, values = (0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0), orig_type: str = ""):
        # OBB data is always 20 floats
        self.values = []
        if isinstance(values, (tuple, list)):
            self.values.extend(float(v) for v in values[:20])
        while len(self.values) < 20:
            self.values.append(0.0)
        self.orig_type = orig_type

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)

    def __getitem__(self, idx):
        return self.values[idx]

    def __str__(self):
        return f"OBB({', '.join(f'{v:.6f}' for v in self.values)})"

    @classmethod
    def parse(cls, ctx):
        values = ctx.read_struct(ctx.unpack_20float, ctx.field_size)
        return cls(values, ctx.original_type)

class PointData:
    def __init__(self, x: float = 0, y: float = 0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        values = ctx.read_struct(ctx.unpack_2float, 8)
        return cls(values[0], values[1], ctx.original_type)

class PositionData:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0, orig_type: str = ""):
        self.x = x
        self.y = y
        self.z = z
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_3double, ctx.field_size)
        return cls(vals[0], vals[1], vals[2], ctx.original_type)

class RangeData:
    def __init__(self, min: float = 0, max: float = 0, orig_type: str = ""):
        self.min = min
        self.max = max
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_2float, ctx.field_size)
        return cls(vals[0], vals[1], ctx.original_type)

class RangeIData:
    def __init__(self, min: int = 0, max: int = 0, orig_type: str = ""):
        self.min = min
        self.max = max
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        vals = ctx.read_struct(ctx.unpack_2int, ctx.field_size)
        return cls(vals[0], vals[1], ctx.original_type)

class SizeData:
    def __init__(self, width: float = 0, height: float = 0, orig_type: str = ""):
        self.width = width
        self.height = height
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        values = ctx.read_struct(ctx.unpack_2float, 8)
        return cls(values[0], values[1], ctx.original_type)

class SphereData:
    def __init__(self, center: Vec3Data = Vec3Data(0,0,0, ""), radius: float = 0, orig_type: str = ""):
        self.center = center
        self.radius = radius
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        values = ctx.read_struct(ctx.unpack_4float, 16)
        center = Vec3Data(values[0], values[1], values[2], "Vec3")
        return cls(center, values[3], ctx.original_type)

class CylinderData:
    def __init__(self, center: Vec3Data = Vec3Data(0,0,0, ""), radius: float = 0, height: float = 0, orig_type: str = ""):
        self.center = center
        self.radius = radius
        self.height = height
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        center_vals = ctx.read_struct(ctx.unpack_4float, 16)
        extent_vals = ctx.read_struct(ctx.unpack_4float, 16)
        _ = ctx.read_struct(ctx.unpack_4float, 16)

        center = Vec3Data(center_vals[0], center_vals[1], center_vals[2], "Vec3")
        return cls(center, center_vals[3], extent_vals[3], ctx.original_type)

class RectData:
    def __init__(self, min_x: float = 0, min_y: float = 0, max_x: float = 0, max_y: float = 0, orig_type: str = ""):
        self.min_x = min_x
        self.min_y = min_y
        self.max_x = max_x
        self.max_y = max_y
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        values = ctx.read_struct(ctx.unpack_4float, 16)
        return cls(values[0], values[1], values[2], values[3], ctx.original_type)

class GameObjectRefData:
    def __init__(self, guid_str: str = "", raw_bytes: bytes = None, orig_type: str = ""):
        if not guid_str:
            guid_str = "00000000-0000-0000-0000-000000000000"
        if not raw_bytes:
            raw_bytes = b'\0' * 16
        self.guid_str = guid_str
        self.raw_bytes = raw_bytes  # Store original bytes
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        guid_str, raw_bytes = ctx.read_guid(ctx.field_size)
        return cls(guid_str, raw_bytes, ctx.original_type)

class RuntimeTypeData:
    def __init__(self, value: str = "", orig_type: str = ""):
        self.value = value
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        value = ctx.read_string_utf8()
        return cls(value, ctx.original_type)

class MaybeObject:
    def __init__(self, orig_type: str = ""):
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        candidate, raw = ctx.read_value_with_raw(ctx.unpack_uint, ctx.field_size)
        if not ctx.is_valid_ref(candidate):
            return RawBytesData(raw, ctx.field_size, ctx.original_type)
        ctx.current_children.append(candidate)
        ctx.set_parent(candidate, ctx.current_instance_index)
        return ObjectData(candidate, ctx.original_type)

class RawBytesData:
    """Stores raw bytes exactly as read from file"""
    def __init__(self, raw_bytes: bytes = bytes(0), field_size: int = 1, orig_type: str = ""):
        self.raw_bytes = raw_bytes
        self.field_size = field_size
        self.orig_type = orig_type

    @classmethod
    def parse(cls, ctx):
        raw = ctx.read_bytes(ctx.field_size)
        return cls(raw, ctx.field_size, ctx.original_type)


TYPE_MAPPING = {
    "bool": BoolData,
    "s32": S32Data,
    "enum": S32Data,
    "int": S32Data,
    "uint": U32Data,
    "f32": F32Data,
    "f64": F64Data,
    "float": F32Data,
    "string": StringData,
    "resource": ResourceData, # Resources are strings so far (?)
    "gameobjectref": GameObjectRefData,
    "object": ObjectData,
    "vec2": Vec2Data,
    "vec3": Vec3Data,
    "vec4": Vec4Data,
    "keyframe": Vec4Data,
    "obb": OBBData,
    "userdata": UserDataData,
    "uint2": Uint2Data,
    "uint3": Uint3Data,
    "u8": U8Data,
    "u16": U16Data,
    "u32": U32Data,
    "u64": U64Data,
    #"sphere": SphereData,
    "size": SizeData,
    "s8": S8Data,
    "s16": S16Data,
    "s64": S64Data,
    "runtimetype": RuntimeTypeData,
    #"rect": RectData,
    "range": RangeData,
    "rangei": RangeIData,
    "quaternion": QuaternionData,
    "point": PointData,
    "mat4": Mat4Data,
    #"linesegment": LineSegmentData,
    "int2": Int2Data,
    "int3": Int3Data,
    "int4": Int4Data,
    "guid": GuidData,
    "float2": Float2Data,
    "float3": Float3Data,
    "float4": Float4Data,
    "position": PositionData,
    #"cylinder": CylinderData,
    #"cone": ConeData,
    "color": ColorData,
    "capsule": CapsuleData,
    "area": AreaData,
    "aabb": AABBData,
    "data": RawBytesData,
    "struct": StructData,
}

NON_ARRAY_PARSERS = MappingProxyType({
    MaybeObject: MaybeObject.parse,
    UserDataData: UserDataData.parse,
    ObjectData: ObjectData.parse,
    Vec3Data: Vec3Data.parse,
    Vec3ColorData: Vec3ColorData.parse,
    Vec4Data: Vec4Data.parse,
    Float4Data: Float4Data.parse,
    QuaternionData: QuaternionData.parse,
    OBBData: OBBData.parse,
    Vec2Data: Vec2Data.parse,
    Float2Data: Float2Data.parse,
    Float3Data: Float3Data.parse,
    PositionData: PositionData.parse,
    Int3Data: Int3Data.parse,
    RangeIData: RangeIData.parse,
    StringData: StringData.parse,
    ResourceData: ResourceData.parse,
    RuntimeTypeData: RuntimeTypeData.parse,
    PointData: PointData.parse,
    SizeData: SizeData.parse,
    BoolData: BoolData.parse,
    S8Data: S8Data.parse,
    U8Data: U8Data.parse,
    U16Data: U16Data.parse,
    S16Data: S16Data.parse,
    S32Data: S32Data.parse,
    S64Data: S64Data.parse,
    U32Data: U32Data.parse,
    U64Data: U64Data.parse,
    F32Data: F32Data.parse,
    F64Data: F64Data.parse,
    GameObjectRefData: GameObjectRefData.parse,
    GuidData: GuidData.parse,
    Mat4Data: Mat4Data.parse,
    RangeData: RangeData.parse,
    #SphereData: SphereData.parse,
    #CylinderData: CylinderData.parse,
    #RectData: RectData.parse,
    ColorData: ColorData.parse,
    CapsuleData: CapsuleData.parse,
    AABBData: AABBData.parse,
    AreaData: AreaData.parse,
    AreaDataOld: AreaDataOld.parse,
    Uint2Data: Uint2Data.parse,
    Uint3Data: Uint3Data.parse,
    Int2Data: Int2Data.parse,
    Int4Data: Int4Data.parse,
    #LineSegmentData: LineSegmentData.parse,
    #ConeData: ConeData.parse,
    RawBytesData: RawBytesData.parse,
})

def get_type_class(field_type: str, field_size: int = 4, is_native: bool = False, is_array: bool = False, align = 4, original_type = "", field_name = "") -> type:
    """Get the appropriate data type class based on field type and size"""

    if field_type == "data":
        if field_size == 16:
            if align == 8 and is_native:
                return GuidData
            else:
                return Vec4Data
        elif field_size == 80:
            return OBBData
        elif field_size == 64 and align == 16:
            return Mat4Data
        #elif field_size == 48 and align == 16:
        #    return CapsuleData
        elif field_size == 4 and is_native:
            return MaybeObject
        elif field_size == 1:
            return U8Data

    if field_type == "obb" and field_size == 16:
        return Vec4Data
    
    if field_type == "uri" and ("GameObjectRef" in original_type):
        return GameObjectRefData
    
    if field_type == "point" and ("Range" in original_type):
        return RangeData
    
    if field_type == "vec3" and "color" in field_name.lower():
        return Vec3ColorData
        
    if is_array and is_native and field_size == 4 and (field_type in ("s32", "u32")):
        return MaybeObject

    matchedType = TYPE_MAPPING.get(field_type, RawBytesData)

    if(matchedType == AreaData and field_size == 80):
        return AreaDataOld
    return matchedType

def is_reference_type(obj):
    return isinstance(obj, (ObjectData, UserDataData))

def is_array_type(obj):
    return isinstance(obj, ArrayData)

def get_reference_value(obj):
    if is_reference_type(obj):
        return obj.value
    return 0