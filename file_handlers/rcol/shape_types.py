from enum import IntEnum
from typing import List, Tuple

class ShapeType(IntEnum):
    Aabb = 0x0
    Sphere = 0x1
    ContinuousSphere = 0x2
    Capsule = 0x3
    ContinuousCapsule = 0x4
    Box = 0x5
    Mesh = 0x6
    HeightField = 0x7
    StaticCompound = 0x8
    Area = 0x9
    Triangle = 0xA
    SkinningMesh = 0xB
    Cylinder = 0xC
    DeformableMesh = 0xD
    Invalid = 0xE
    Max = 0xF

class BaseShape:
    """Base class for all shape types"""
    def __init__(self):
        self.data = []
    
    def read_vec3_padded(self, handler):
        """Read 3 floats + padding"""
        vec = [handler.read_float() for _ in range(3)]
        handler.skip(4)  # padding
        return vec
    
    def write_vec3_padded(self, handler, vec):
        """Write 3 floats + padding"""
        for v in vec:
            handler.write_float(v)
        handler.write_int32(0)  # padding
    
    def read_vec3(self, handler):
        """Read 3 floats"""
        return [handler.read_float() for _ in range(3)]
    
    def write_vec3(self, handler, vec):
        """Write 3 floats"""
        for v in vec:
            handler.write_float(v)

class AABB(BaseShape):
    """Axis-Aligned Bounding Box"""
    def __init__(self):
        self.min = [0.0, 0.0, 0.0]
        self.max = [0.0, 0.0, 0.0]
        
    def read(self, handler):
        self.min = self.read_vec3_padded(handler)
        self.max = self.read_vec3_padded(handler)
        
    def write(self, handler):
        self.write_vec3_padded(handler, self.min)
        self.write_vec3_padded(handler, self.max)
        
    def __str__(self):
        return f"AABB(min={self.min}, max={self.max})"

class Sphere(BaseShape):
    """Sphere shape"""
    def __init__(self):
        self.center = [0.0, 0.0, 0.0]
        self.radius = 0.0
        
    def read(self, handler):
        self.center = self.read_vec3(handler)
        self.radius = handler.read_float()
        
    def write(self, handler):
        self.write_vec3(handler, self.center)
        handler.write_float(self.radius)
        
    def __str__(self):
        return f"Sphere(center={self.center}, radius={self.radius})"

class Capsule(BaseShape):
    """Capsule shape"""
    def __init__(self):
        self.start = [0.0, 0.0, 0.0]
        self.end = [0.0, 0.0, 0.0]
        self.radius = 0.0
        self.padding = [0.0, 0.0, 0.0]
        
    def read(self, handler):
        # via.Capsule is 3x 16-byte blocks:
        #   [start.xyz, _], [end.xyz, _], [radius, _, _, _]
        self.start = self.read_vec3_padded(handler)
        self.end = self.read_vec3_padded(handler)
        self.radius = handler.read_float()
        self.padding = [handler.read_float() for _ in range(3)]
        
    def write(self, handler):
        self.write_vec3_padded(handler, self.start)
        self.write_vec3_padded(handler, self.end)
        handler.write_float(self.radius)
        for value in self.padding[:3]:
            handler.write_float(value)
        for _ in range(max(0, 3 - len(self.padding))):
            handler.write_float(0.0)
        
    def __str__(self):
        return f"Capsule(start={self.start}, end={self.end}, radius={self.radius})"

class OBB(BaseShape):
    """Oriented Bounding Box"""
    def __init__(self):
        self.matrix = [[0.0] * 4 for _ in range(4)]
        self.extent = [0.0, 0.0, 0.0]
        self.padding = 0.0
        
    def read(self, handler):
        # Read 4x4 matrix
        for i in range(4):
            for j in range(4):
                self.matrix[i][j] = handler.read_float()
        self.extent = self.read_vec3(handler)
        self.padding = handler.read_float()
        
    def write(self, handler):
        # Write 4x4 matrix
        for i in range(4):
            for j in range(4):
                handler.write_float(self.matrix[i][j])
        self.write_vec3(handler, self.extent)
        handler.write_float(self.padding)
        
    def __str__(self):
        return f"OBB(extent={self.extent})"

class Area(BaseShape):
    """Area shape (4x Vec2 + height + bottom + 8-byte padding)"""
    def __init__(self):
        self.points = [[0.0, 0.0] for _ in range(4)]
        self.height = 0.0
        self.bottom = 0.0
        self.padding = [0.0, 0.0]
        
    def read(self, handler):
        self.points = [[handler.read_float(), handler.read_float()] for _ in range(4)]
        self.height = handler.read_float()
        self.bottom = handler.read_float()
        self.padding = [handler.read_float(), handler.read_float()]
            
    def write(self, handler):
        for point in self.points:
            handler.write_float(point[0])
            handler.write_float(point[1])
        handler.write_float(self.height)
        handler.write_float(self.bottom)
        handler.write_float(self.padding[0] if len(self.padding) > 0 else 0.0)
        handler.write_float(self.padding[1] if len(self.padding) > 1 else 0.0)
            
    def __str__(self):
        return f"Area(points={self.points}, height={self.height}, bottom={self.bottom})"

class Triangle(BaseShape):
    """Triangle shape - 3 vertices"""
    def __init__(self):
        self.vertices = []
        
    def read(self, handler):
        self.vertices = [self.read_vec3_padded(handler) for _ in range(3)]
            
    def write(self, handler):
        for vertex in self.vertices:
            self.write_vec3_padded(handler, vertex)
            
    def __str__(self):
        return f"Triangle(vertices={self.vertices})"

# Cylinder is identical to Capsule in structure
class Cylinder(Capsule):
    """Cylinder shape"""
    def __str__(self):
        return f"Cylinder(start={self.start}, end={self.end}, radius={self.radius})"

# Shape factory
SHAPE_MAP = {
    ShapeType.Aabb: AABB,
    ShapeType.Sphere: Sphere,
    ShapeType.ContinuousSphere: Sphere,
    ShapeType.Capsule: Capsule,
    ShapeType.ContinuousCapsule: Capsule,
    ShapeType.Box: OBB,
    ShapeType.Area: Area,
    ShapeType.Triangle: Triangle,
    ShapeType.Cylinder: Cylinder,
}

def create_shape(shape_type: ShapeType):
    """Factory function to create shape based on type"""
    shape_class = SHAPE_MAP.get(shape_type)
    if shape_class:
        return shape_class()
    raise ValueError(f"Unsupported RCOL shape type {shape_type}")

def read_shape(handler, shape_type: ShapeType):
    """Read shape data based on type"""
    shape = create_shape(shape_type)
    shape.read(handler)
    return shape

def write_shape(handler, shape_type: ShapeType, shape):
    """Write shape data based on type"""
    if shape is None:
        raise ValueError(f"Cannot write RCOL shape type {shape_type}: shape payload is missing")
    shape.write(handler)
