class BoolData:
    def __init__(self, value: bool):
        self.value = value

class S32Data:
    def __init__(self, value: int):
        self.value = value

class U32Data:
    def __init__(self, value: int):
        self.value = value

class F32Data:
    def __init__(self, value: float):
        self.value = value

class StringData:
    def __init__(self, value: str):
        self.value = value

class GameObjectRefData:
    def __init__(self, guid_str: str):
        self.guid_str = guid_str

class ObjectRefData:
    def __init__(self, index: int):
        self.index = index

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

class OBBData:
    def __init__(self, values: list):
        self.values = values  # 20 floats

class UserDataArrayData:
    def __init__(self, values: list):
        self.values = values

class ArrayData:
    def __init__(self, values: list, element_type: str):
        self.values = values
        self.element_type = element_type
