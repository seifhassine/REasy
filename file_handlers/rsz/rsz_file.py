import struct
import uuid
import sys
from types import MappingProxyType
from file_handlers.rsz.rsz_data_types import (
    StructData, S8Data, U8Data, BoolData, S16Data, U16Data, S64Data, S32Data, U64Data, F64Data, F32Data,
    Vec2Data, Float2Data, RangeData, RangeIData, Float3Data, PositionData, Int3Data, Float4Data, QuaternionData,
    ColorData, ObjectData, U32Data, UserDataData, Vec3Data, Vec3ColorData, Vec4Data, Mat4Data, GameObjectRefData,
    GuidData, StringData, ResourceData, RuntimeTypeData, OBBData, RawBytesData, CapsuleData, AABBData, AreaData,
    ArrayData, MaybeObject, Uint2Data, Int2Data, Uint3Data, SizeData, PointData, AreaDataOld, get_type_class, 
    RectData, NON_ARRAY_PARSERS
)
from file_handlers.rsz.pfb_16.pfb_structure import Pfb16Header, build_pfb_16, parse_pfb16_rsz_userdata
from file_handlers.rsz.scn_19.scn_19_structure import Scn19Header, build_scn_19, parse_scn19_rsz_userdata
from file_handlers.rsz.scn_18.scn_18_structure import Scn18Header, _parse_scn_18_resource_infos, build_scn_18
from utils.hex_util import read_wstring, guid_le_to_str, align as _align 

_STRUCT_DEFINITIONS = {
    "uint": "<I",
    "int": "<i",
    "float": "<f",
    "4float": "<4f",
    "3float": "<3f",
    "3double": "<3d",
    "2float": "<2f",
    "2int": "<2i",
    "3int": "<3i",
    "sbyte": "<b",
    "ubyte": "<B",
    "4ubyte": "<4B",
    "short": "<h",
    "ushort": "<H",
    "long": "<q",
    "ulong": "<Q",
    "double": "<d",
    "20float": "<20f",
    "16float": "<16f",
    "bool": "<?",
}

_STRUCTS = {name: struct.Struct(fmt) for name, fmt in _STRUCT_DEFINITIONS.items()}
_PACKERS = {name: s.pack for name, s in _STRUCTS.items()}
_UNPACKERS = {name: s.unpack_from for name, s in _STRUCTS.items()}

pack_uint = _PACKERS["uint"]
pack_int = _PACKERS["int"]
pack_float = _PACKERS["float"]
pack_4float = _PACKERS["4float"]
pack_3float = _PACKERS["3float"]
pack_3double = _PACKERS["3double"]
pack_2float = _PACKERS["2float"]
pack_2int = _PACKERS["2int"]
pack_3int = _PACKERS["3int"]
pack_sbyte = _PACKERS["sbyte"]
pack_ubyte = _PACKERS["ubyte"]
pack_4ubyte = _PACKERS["4ubyte"]
pack_short = _PACKERS["short"]
pack_ushort = _PACKERS["ushort"]
pack_long = _PACKERS["long"]
pack_ulong = _PACKERS["ulong"]
pack_double = _PACKERS["double"]
pack_20float = _PACKERS["20float"]
pack_16float = _PACKERS["16float"]
pack_bool = _PACKERS["bool"]

unpack_uint    = _UNPACKERS["uint"]
unpack_int     = _UNPACKERS["int"]
unpack_float   = _UNPACKERS["float"]
unpack_4float  = _UNPACKERS["4float"]
unpack_3float  = _UNPACKERS["3float"]
unpack_3double = _UNPACKERS["3double"]
unpack_2float  = _UNPACKERS["2float"]
unpack_2int    = _UNPACKERS["2int"]
unpack_3int    = _UNPACKERS["3int"]
unpack_sbyte   = _UNPACKERS["sbyte"]
unpack_ubyte   = _UNPACKERS["ubyte"]
unpack_4ubyte  = _UNPACKERS["4ubyte"]
unpack_short   = _UNPACKERS["short"]
unpack_ushort  = _UNPACKERS["ushort"]
unpack_long    = _UNPACKERS["long"]
unpack_ulong   = _UNPACKERS["ulong"]
unpack_double  = _UNPACKERS["double"]
unpack_20float = _UNPACKERS["20float"]
unpack_16float = _UNPACKERS["16float"]


class _NonArrayFieldParser:
    __slots__ = (
        "file",
        "data",
        "pos",
        "base_mod",
        "field_size",
        "field_align",
        "original_type",
        "current_children",
        "set_parent",
        "is_valid_ref",
        "current_instance_index",
        "rsz_userdata_by_id",
        "rsz_userdata_map",
    )

    def __init__(self, file_obj):
        self.file = file_obj
        self.data = file_obj.data
        self.base_mod = 0
        self.pos = 0
        self.field_size = 0
        self.field_align = 1
        self.original_type = ""
        self.current_children = None
        self.set_parent = None
        self.is_valid_ref = None
        self.current_instance_index = None
        self.rsz_userdata_by_id = None
        self.rsz_userdata_map = None

    def reset_context(
        self,
        base_mod,
        current_children,
        set_parent,
        is_valid_ref,
        current_instance_index,
        rsz_userdata_by_id,
        rsz_userdata_map,
    ):
        self.data = self.file.data
        self.base_mod = base_mod
        self.current_children = current_children
        self.set_parent = set_parent
        self.is_valid_ref = is_valid_ref
        self.current_instance_index = current_instance_index
        self.rsz_userdata_by_id = rsz_userdata_by_id
        self.rsz_userdata_map = rsz_userdata_map
        self.pos = 0
        self.field_size = 0
        self.field_align = 1
        self.original_type = ""

    def configure(self, pos, field_size, field_align, original_type):
        if field_align and field_align > 1:
            remainder = (pos + self.base_mod) % field_align
            if remainder:
                pos += field_align - remainder
            self.field_align = field_align
        else:
            self.field_align = 1
        self.pos = pos
        self.field_size = field_size
        self.original_type = original_type or ""
    
    def configure_pos_only(self, pos, field_align):
        if field_align and field_align > 1:
            remainder = (pos + self.base_mod) % field_align
            if remainder:
                pos += field_align - remainder
        self.pos = pos

    def _align_in_place(self, align):
        if align and align > 1:
            remainder = (self.pos + self.base_mod) % align
            if remainder:
                self.pos += align - remainder

    def align(self, align=None):
        self._align_in_place(align or self.field_align or 1)
        return self.pos

    def _slice_bytes(self, start, end):
        return self.data[start:end]

    def read_value(self, unpack_func, size, align=None):
        pos = self.pos
        if align and align > 1:
            remainder = (pos + self.base_mod) % align
            if remainder:
                pos += align - remainder
        value = unpack_func(self.data, pos)[0]
        self.pos = pos + size
        return value

    def read_value_with_raw(self, unpack_func, size, align=None):
        pos = self.pos
        if align and align > 1:
            remainder = (pos + self.base_mod) % align
            if remainder:
                pos += align - remainder
        value = unpack_func(self.data, pos)[0]
        new_pos = pos + size
        self.pos = new_pos
        raw = self.data[pos:new_pos]
        return value, raw

    def read_struct(self, unpack_func, size, align=None):
        pos = self.pos
        if align and align > 1:
            remainder = (pos + self.base_mod) % align
            if remainder:
                pos += align - remainder
        values = unpack_func(self.data, pos)
        self.pos = pos + size
        return values

    def read_bytes(self, length, align=None):
        pos = self.pos
        if align and align > 1:
            remainder = (pos + self.base_mod) % align
            if remainder:
                pos += align - remainder
        end = pos + length
        self.pos = end
        return self.data[pos:end]

    def read_string_utf16(self):
        count = self.read_value(self.unpack_uint, 4, align=4)
        byte_count = count * 2
        start = self.pos
        end = start + byte_count
        segment = self.data[start:end].tobytes()
        value = sys.intern(segment.decode("utf-16-le")) if byte_count else ""
        self.pos = end
        return value

    def read_string_utf8(self):
        count = self.read_value(self.unpack_uint, 4, align=4)
        start = self.pos
        end = start + count
        segment = self.data[start:end].tobytes()
        value = sys.intern(segment.decode("utf-8")) if count else ""
        self.pos = end
        return value

    def read_guid(self, size, align=None):
        raw = self.read_bytes(size, align)
        return guid_le_to_str(raw), raw

    def skip(self, length, align=None):
        if align is not None:
            self.align(align)
        self.pos += length


for _name, _func in (
    ("unpack_uint", unpack_uint),
    ("unpack_int", unpack_int),
    ("unpack_float", unpack_float),
    ("unpack_double", unpack_double),
    ("unpack_short", unpack_short),
    ("unpack_ushort", unpack_ushort),
    ("unpack_sbyte", unpack_sbyte),
    ("unpack_ubyte", unpack_ubyte),
    ("unpack_long", unpack_long),
    ("unpack_ulong", unpack_ulong),
    ("unpack_2float", unpack_2float),
    ("unpack_3float", unpack_3float),
    ("unpack_4float", unpack_4float),
    ("unpack_3double", unpack_3double),
    ("unpack_2int", unpack_2int),
    ("unpack_3int", unpack_3int),
    ("unpack_4ubyte", unpack_4ubyte),
    ("unpack_16float", unpack_16float),
    ("unpack_20float", unpack_20float),
):
    setattr(_NonArrayFieldParser, _name, staticmethod(_func))


########################################
# SCN File Data Classes
########################################


class ScnHeader:
    __slots__ = ("signature","info_count","resource_count","folder_count","prefab_count",
                 "userdata_count","folder_tbl","resource_info_tbl","prefab_info_tbl",
                 "userdata_info_tbl","data_offset")
    SIZE = 64
    def __init__(self):
        self.signature = b""
        self.info_count = 0
        self.resource_count = 0
        self.folder_count = 0
        self.prefab_count = 0
        self.userdata_count = 0
        self.folder_tbl = 0
        self.resource_info_tbl = 0
        self.prefab_info_tbl = 0
        self.userdata_info_tbl = 0
        self.data_offset = 0
    def parse(self, data: bytes):
        fmt = "<4s5I5Q"
        (self.signature,
         self.info_count,
         self.resource_count,
         self.folder_count,
         self.prefab_count,
         self.userdata_count,
         self.folder_tbl,
         self.resource_info_tbl,
         self.prefab_info_tbl,
         self.userdata_info_tbl,
         self.data_offset) = struct.unpack_from(fmt, data, 0)

class PfbHeader:
    __slots__ = ("signature","info_count","resource_count","gameobject_ref_info_count",
                "userdata_count","reserved","gameobject_ref_info_tbl","resource_info_tbl",
                "userdata_info_tbl","data_offset")
    SIZE = 56
    def __init__(self):
        self.signature = b""
        self.info_count = 0
        self.resource_count = 0
        self.gameobject_ref_info_count = 0
        self.userdata_count = 0
        self.reserved = 0
        self.gameobject_ref_info_tbl = 0
        self.resource_info_tbl = 0
        self.userdata_info_tbl = 0
        self.data_offset = 0
        
    def parse(self, data: bytes):
        fmt = "<4s5I4Q"
        (self.signature,
         self.info_count,
         self.resource_count,
         self.gameobject_ref_info_count,
         self.userdata_count,
         self.reserved,
         self.gameobject_ref_info_tbl,
         self.resource_info_tbl,
         self.userdata_info_tbl,
         self.data_offset) = struct.unpack_from(fmt, data, 0)

class UsrHeader:
    __slots__ = ("signature","resource_count","userdata_count","info_count",
                 "resource_info_tbl","userdata_info_tbl","data_offset","reserved")
    SIZE = 48
    def __init__(self):
        self.signature = b""
        self.resource_count = 0
        self.userdata_count = 0
        self.info_count = 0
        self.resource_info_tbl = 0
        self.userdata_info_tbl = 0
        self.data_offset = 0
        self.reserved = 0  

    def parse(self, data: bytes):
        fmt = "<4s3I3QQ"
        (self.signature,
         self.resource_count,
         self.userdata_count,
         self.info_count,
         self.resource_info_tbl,
         self.userdata_info_tbl,
         self.data_offset,
         self.reserved) = struct.unpack_from(fmt, data, 0)

class GameObjectRefInfo:
    __slots__ = ("object_id","property_id","array_index","target_id")
    SIZE = 16
    def __init__(self):
        self.object_id = 0
        self.property_id = 0
        self.array_index = 0
        self.target_id = 0
        
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated GameObjectRefInfo at 0x{offset:X}")
        self.object_id, self.property_id, self.array_index, self.target_id = struct.unpack_from("<4i", data, offset)
        return offset + self.SIZE

class RszGameObject:
    __slots__ = ("guid","id","parent_id","component_count","ukn","prefab_id")
    SIZE = 32
    def __init__(self):
        self.guid = b"\x00" * 16
        self.id = 0
        self.parent_id = 0
        self.component_count = 0
        self.ukn = 0
        self.prefab_id = 0
    def parse(self, data: bytes, offset: int, is_scn19: bool = False) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated gameobject at 0x{offset:X}")
        self.guid = data[offset:offset+16]
        offset += 16
        self.id, = struct.unpack_from("<i", data, offset)
        offset += 4
        self.parent_id, = struct.unpack_from("<i", data, offset)
        offset += 4
        self.component_count, = struct.unpack_from("<H", data, offset)
        offset += 2
        if is_scn19:
            self.prefab_id, self.ukn = struct.unpack_from("<hi", data, offset)
        else:
            self.ukn, self.prefab_id = struct.unpack_from("<hi", data, offset)
        offset += 6
        return offset

class PfbGameObject:
    __slots__ = ("id","parent_id","component_count")
    SIZE = 12
    def __init__(self):
        self.id = 0
        self.parent_id = 0
        self.component_count = 0
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated PfbGameObject at 0x{offset:X}")
        self.id, self.parent_id, self.component_count = struct.unpack_from("<iii", data, offset)
        return offset + self.SIZE

class RszFolderInfo:
    __slots__ = ("id","parent_id")
    SIZE = 8
    def __init__(self):
        self.id = 0
        self.parent_id = 0
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated folder info at 0x{offset:X}")
        self.id, self.parent_id = struct.unpack_from("<ii", data, offset)
        return offset + self.SIZE

class RszResourceInfo:
    __slots__ = ("string_offset","reserved")
    SIZE = 8
    def __init__(self):
        self.string_offset = 0
        self.reserved = 0
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated resource info at 0x{offset:X}")
        self.string_offset, self.reserved = struct.unpack_from("<II", data, offset)
        return offset + self.SIZE

class RszPrefabInfo:
    __slots__ = ("string_offset","parent_id")
    SIZE = 8
    SIZE = 8
    def __init__(self):
        self.string_offset = 0
        self.parent_id = 0
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated prefab info at 0x{offset:X}")
        self.string_offset, self.parent_id = struct.unpack_from("<II", data, offset)
        return offset + self.SIZE

# RSZUserDataInfos – each entry is 16 bytes.
class RSZUserDataInfo:
    __slots__ = ("instance_id","hash","string_offset","crc")
    SIZE = 16
    def __init__(self):
        self.instance_id = 0   # 4 bytes: which instance this userdata is associated with
        self.hash = 0          # 4 bytes: hash
        self.string_offset = 0 # 8 bytes: offset for the userdata string (uint64)
        self.crc = 0
    def parse(self, data: bytes, offset: int, is_rszuserdata: bool = True) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated RSZUserData info at 0x{offset:X}")
        if is_rszuserdata:
            self.instance_id, self.hash, self.string_offset = struct.unpack_from("<IIQ", data, offset)
        else:
            self.hash, self.crc, self.string_offset = struct.unpack_from("<IIQ", data, offset)
        return offset + self.SIZE

class RszRSZHeader:
    __slots__ = ("magic","version","object_count","instance_count","userdata_count",
                 "reserved","instance_offset","data_offset","userdata_offset")
    def parse(self, data: bytes, offset: int) -> int:
        self.magic, self.version = struct.unpack_from("<II", data, offset)
        offset += 8
        if self.version < 4:
            # v3 has no userdata_count field
            # next: object_count, instance_count, reserved (3×4 bytes)
            self.object_count, self.instance_count = struct.unpack_from("<II", data, offset)
            offset += 8
        else:
            # v4+ has object_count, instance_count, userdata_count, reserved (4×4 bytes)
            (self.object_count,
             self.instance_count,
             self.userdata_count,
             self.reserved) = struct.unpack_from("<IIII", data, offset)
            offset += 16
        
        self.instance_offset, self.data_offset = struct.unpack_from("<QQ", data, offset)
        offset += 16

        if self.version > 3:
            # only v4+ has this
            self.userdata_offset, = struct.unpack_from("<Q", data, offset)
            offset += 8
        else:
            self.userdata_offset = 0

        return offset

    @property
    def SIZE(self):
        return 32 if self.version < 4 else 48
    
class RszInstanceInfo:
    __slots__ = ("type_id","crc")
    SIZE = 8
    def __init__(self):
        self.type_id = 0
        self.crc = 0
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated instance info at x{offset:X}")
        self.type_id, self.crc = struct.unpack_from("<II", data, offset)
        return offset + self.SIZE


########################################
# Main Rsz File Parser
########################################

class RszFile:

    def __init__(self):
        self.full_data = b""
        self.header = None
        self.is_usr = False
        self.is_pfb = False
        self.is_scn = False
        self.is_pfb16 = False
        self.has_embedded_rsz = False
        self.gameobjects = []
        self.folder_infos = []
        self.resource_infos = []
        self.prefab_infos = []
        self.userdata_infos = []      # from main header
        self.gameobject_ref_infos = []  # for PFB files
        self.rsz_userdata_infos = []  #from RSZ section
        self.resource_block = b""
        self.prefab_block = b""
        self.prefab_block_start = 0
        self.rsz_header = None
        self.object_table = []
        self.instance_infos = []
        self.data = memoryview(b"")
        self.type_registry = None
        self.parsed_instances = []
        self._current_offset = 0 
        self.game_version = "RE4"
        self.filepath = ""
        self.auto_resource_management = False
        
        self._rsz_userdata_dict = {}
        self._rsz_userdata_set = set()

        self._resource_str_map = {}  # {ResourceInfo: str}
        self._prefab_str_map = {}    # {PrefabInfo: str}
        self._userdata_str_map = {}  # {UserDataInfo: str}
        self._rsz_userdata_str_map = {}  # {RSZUserDataInfo: str}
        self.parsed_elements = {}
        self.instance_hierarchy = {}  # {instance_id: {children: [], parent: None}}
        self._gameobject_instance_ids = set()  # Set of gameobject instance IDs
        self._folder_instance_ids = set()      # Set of folder instance IDs

        self._empty_fields = MappingProxyType({})
        self._path_lower = ""
        self._is_16 = False
        self._is_18 = False
        self._is_19 = False
        self._is_scn_new = False   # scn.18 / scn.19
        self._rsz_type_cache = {}  # (ftype,fsize,is_native,is_array,align,original_type)->cls
        self._parser_pool = []
        self._prepared_field_defs = set()
        self._type_info_cache = {}


    def read(self, data: bytes, skip_data: bool = False):
        # Use memoryview for efficient slicing operations
        self.full_data = memoryview(data)
        self._current_offset = 0
        self._parser_pool.clear()
        self._prepared_field_defs.clear()
        self._type_info_cache.clear()

        self.is_usr = False
        self.is_pfb = False
        self.is_pfb16 = False
        
        self._path_lower = (self.filepath or "").lower()
        self._is_16 = self._path_lower.endswith(".16")
        self._is_18 = self._path_lower.endswith(".18")
        self._is_19 = self._path_lower.endswith(".19")
        self._is_scn_new = self._is_18 or self._is_19

        if data[:4] == b'USR\x00':
            self.is_usr = True
            self.header = UsrHeader()
        elif data[:4] == b'PFB\x00':
            self.is_pfb = True
            
            if self._is_16:
                self.header = Pfb16Header()
                self.is_pfb16 = True 
            else:
                self.header = PfbHeader()
        else:
            self.is_usr = False
            self.is_pfb = False
            self.is_scn = True
            
            if self._is_19:
                self.header = Scn19Header()
            elif self._is_18:
                self.header = Scn18Header()
            else:
                self.header = ScnHeader()
            
        self.header.parse(data)
        self._current_offset = self.header.SIZE

        # Call appropriate parsing function based on file type
        if self.is_usr:
            self._parse_usr_file(data, skip_data)
        elif self.is_pfb:
            self._parse_pfb_file(data, skip_data)
        else:
            self._parse_scn_file(data, skip_data)

    def _parse_usr_file(self, data: bytes, skip_data: bool = False  ):
        """Parse USR file structure"""
        self._parse_resource_infos(data)
        self._parse_userdata_infos(data)
        self._parse_blocks()
        self._parse_rsz_section(data)
        self._parse_instances(data, skip_data)
        
    def _parse_pfb_file(self, data: bytes, skip_data: bool = False):
        """Parse PFB file structure with game version considerations"""
        self._parse_gameobjects(data)
        self._parse_gameobject_ref_infos(data)
        
        if self.filepath.lower().endswith('.16'):
            from file_handlers.rsz.pfb_16.pfb_structure import parse_pfb16_resources
            self._current_offset = parse_pfb16_resources(self, data)
        else:
            self._parse_resource_infos(data)
            self._parse_userdata_infos(data)
            
        self._parse_blocks()
        self._parse_rsz_section(data)
        self._parse_instances(data, skip_data)
        
    def _parse_scn_file(self, data: bytes, skip_data: bool = False):
        """Parse standard SCN file structure"""
        self._parse_gameobjects(data)
        self._parse_folder_infos(data)
        if self.filepath.lower().endswith('.18'):
            _parse_scn_18_resource_infos(self)
        else:
            self._parse_resource_infos(data)
        
        self._parse_prefab_infos(data)
        # SCN.19 format doesn't have userdata_infos
        if not (self.filepath.lower().endswith('.19') or self.filepath.lower().endswith('.18')):
            self._parse_userdata_infos(data)
        self._parse_blocks()
        self._parse_rsz_section(data, skip_data)
        self._parse_instances(data, skip_data)        

    def _parse_header(self, data):
        if self.is_pfb:
            self.header = PfbHeader()
            self.header.parse(data)
            self._current_offset = PfbHeader.SIZE
        elif self.is_usr:
            self.header = UsrHeader()
            self.header.parse(data)
            self._current_offset = UsrHeader.SIZE
        else:
            self.header = ScnHeader()
            self.header.parse(data)
            self._current_offset = ScnHeader.SIZE

    def _parse_gameobjects(self, data):
        if self.is_pfb:
            # PFB files use a different GameObject structure (12 bytes)
            for _ in range(self.header.info_count):
                go = PfbGameObject()
                self._current_offset = go.parse(data, self._current_offset)
                self.gameobjects.append(go)
        else:
            # Regular SCN files use the 32-byte GameObject structure
            is_scn19 = self._is_scn_new
            for _ in range(self.header.info_count):
                go = RszGameObject()
                self._current_offset = go.parse(data, self._current_offset, is_scn19)
                self.gameobjects.append(go)

    def _parse_gameobject_ref_infos(self, data):
        for _ in range(self.header.gameobject_ref_info_count):
            gori = GameObjectRefInfo()
            self._current_offset = gori.parse(data, self._current_offset)
            self.gameobject_ref_infos.append(gori)
        self._current_offset = _align(self._current_offset, 16)

    def _parse_folder_infos(self, data):
        for _ in range(self.header.folder_count):
            fi = RszFolderInfo()
            self._current_offset = fi.parse(data, self._current_offset)
            self.folder_infos.append(fi)
        if not self._is_18:
            self._current_offset = _align(self._current_offset, 16)
        else: 
            self._current_offset += 16

    def _parse_resource_infos(self, data):
        self._current_offset = self.header.resource_info_tbl
        for _ in range(self.header.resource_count):
            ri = RszResourceInfo()
            self._current_offset = ri.parse(data, self._current_offset)
            self.resource_infos.append(ri)
        self._current_offset = _align(self._current_offset, 16)

    def _parse_prefab_infos(self, data):
        self._current_offset = self.header.prefab_info_tbl
        for _ in range(self.header.prefab_count):
            pi = RszPrefabInfo()
            self._current_offset = pi.parse(data, self._current_offset)
            self.prefab_infos.append(pi)
        self._current_offset = _align(self._current_offset, 16)

    def _parse_userdata_infos(self, data):
        self._current_offset = self.header.userdata_info_tbl
        for _ in range(self.header.userdata_count):
            ui = RSZUserDataInfo()
            self._current_offset = ui.parse(data, self._current_offset, is_rszuserdata=False)
            self.userdata_infos.append(ui)
        self._current_offset = _align(self._current_offset, 16)

    def _parse_blocks(self):

        self._prefab_str_map.clear()
        self._userdata_str_map.clear()
        self._rsz_userdata_str_map.clear()
        
        # Batch process all strings instead of one-by-one
        if not (self._is_18 and self.is_scn):
            self._resource_str_map.clear()
            for ri in self.resource_infos:
                if (ri.string_offset != 0):
                    s, _ = read_wstring(self.full_data, ri.string_offset, 1000)
                    s = sys.intern(s)
                    self.set_resource_string(ri, s)
                
        for pi in self.prefab_infos:
            if (pi.string_offset != 0):
                s, _ = read_wstring(self.full_data, pi.string_offset, 1000)
                s = sys.intern(s)
                self.set_prefab_string(pi, s)
                
        for ui in self.userdata_infos:
            if (ui.string_offset != 0):
                s, _ = read_wstring(self.full_data, ui.string_offset, 1000)
                s = sys.intern(s)
                self.set_userdata_string(ui, s)

    def _parse_rsz_section(self, data, skip_data = False):
        self._current_offset = self.header.data_offset

        self.rsz_header = RszRSZHeader()
        self._current_offset = self.rsz_header.parse(data, self._current_offset)

        # Parse Object Table first
        ot_bytes = self.rsz_header.object_count * 4
        object_table_view = memoryview(data)[self._current_offset:self._current_offset + ot_bytes].cast('i')
        self.object_table = object_table_view.tolist()
        self._current_offset += ot_bytes

        # Now we can create the gameobject and folder ID sets
        self._gameobject_instance_ids = {
            self.object_table[go.id] 
            for go in self.gameobjects 
            if go.id < len(self.object_table)
        }
        self._folder_instance_ids = {
            self.object_table[fi.id]
            for fi in self.folder_infos
            if fi.id < len(self.object_table)
        }

        # Continue with rest of RSZ section parsing
        self._current_offset = self.header.data_offset + self.rsz_header.instance_offset

        # Parse Instance Infos –that has instance_count entries (8 bytes each)
        for _ in range(self.rsz_header.instance_count):
            ii = RszInstanceInfo()
            self._current_offset = ii.parse(data, self._current_offset)
            if self.rsz_header.version < 4:
                self._current_offset += 8
            self.instance_infos.append(ii)

        self._warm_type_registry_cache()

        # Only parse userdata if v>3
        if self.rsz_header.version > 3:
            self._current_offset = self.header.data_offset + self.rsz_header.userdata_offset
            if self._is_19 or (self._is_18 and self.is_scn):
                self._parse_scn19_rsz_userdata(data, skip_data)
            elif self._is_16:
                self._current_offset = parse_pfb16_rsz_userdata(self, data, skip_data)
            else:
                self._parse_standard_rsz_userdata(data)
        
        self.data = self.full_data[self._current_offset:]
        
        self._rsz_userdata_dict = {rui.instance_id: rui for rui in self.rsz_userdata_infos}
        self._rsz_userdata_set = set(self._rsz_userdata_dict.keys())

        file_offset_of_data = self._current_offset
        self._instance_base_mod = file_offset_of_data % 16

    def _warm_type_registry_cache(self):
        """Preload type information for all parsed instance infos."""
        self._type_info_cache.clear()
        type_registry = self.type_registry
        if not type_registry:
            return

        type_ids = {inst.type_id for inst in self.instance_infos if getattr(inst, "type_id", 0)}
        if not type_ids:
            return

        pre_cache = getattr(type_registry, "pre_cache_types", None)
        if callable(pre_cache):
            pre_cache(type_ids)

        cache = self._type_info_cache
        for type_id in type_ids:
            cache[type_id] = type_registry.get_type_info(type_id)

    def _parse_standard_rsz_userdata(self, data):
        """Parse standard RSZ userdata entries (16 bytes each)"""
        self.rsz_userdata_infos = []
        for _ in range(self.rsz_header.userdata_count):
            rui = RSZUserDataInfo()
            self._current_offset = rui.parse(data, self._current_offset)
            if rui.string_offset != 0:
                abs_offset = self.header.data_offset + rui.string_offset
                s, _ = read_wstring(self.full_data, abs_offset, 1000)
                s = sys.intern(s)
                self.set_rsz_userdata_string(rui, s)
            self.rsz_userdata_infos.append(rui)
            
        last_str_offset = self.rsz_userdata_infos[-1].string_offset if self.rsz_userdata_infos else 0
        if last_str_offset:
            abs_offset = self.header.data_offset + last_str_offset
            s, new_offset = read_wstring(self.full_data, abs_offset, 1000)
            _ = sys.intern(s)
        else:
            new_offset = self._current_offset
        self._current_offset = _align(new_offset, 16)

    def _parse_scn19_rsz_userdata(self, data, skip_data = False):
        """Parse SCN.19 RSZ userdata entries (24 bytes each with embedded binary data)"""
        self.has_embedded_rsz = True
        self._current_offset = parse_scn19_rsz_userdata(self, data, skip_data)

    def get_rsz_userdata_string(self, rui):
        return self._rsz_userdata_str_map.get(rui, "")

    def _parse_instances(self, data, skip = False):
        """Parse instance data with optimizations"""
        if skip:
            return
        current_offset = 0
        # Reset processed instances tracking
        self._processed_instances = set()

        # Pre-initialize hierarchy and element dictionaries
        self.instance_hierarchy = {
            idx: {"children": [], "parent": None} 
            for idx in range(len(self.instance_infos))
        }

        # Cache type registry for faster lookups
        type_registry = self.type_registry
        type_cache = self._type_info_cache
        EMPTY = self._empty_fields

        # Process all instances
        for idx, inst in enumerate(self.instance_infos):

            if idx == 0 or inst.type_id == 0 or idx in self._rsz_userdata_set or idx in self._processed_instances:
                self.parsed_elements[idx] = EMPTY
                continue
            
            type_info = type_cache.get(inst.type_id)
            if type_info is None and type_registry:
                type_info = type_registry.get_type_info(inst.type_id)
                type_cache[inst.type_id] = type_info
            fields_def = type_info.get("fields", [])
            
            if not fields_def:
                self.parsed_elements[idx] = EMPTY
                continue

            self.parsed_elements[idx] = {}
            new_offset = self.parse_instance_fields(
                offset=current_offset,
                fields_def=fields_def,
                current_instance_index=idx
            )
            current_offset = new_offset

    def _prepare_field_definitions(self, fields_def):
        """Populate cached parsing helpers for a list of field definitions."""
        if not fields_def:
            return

        fields_id = id(fields_def)
        if fields_id in self._prepared_field_defs:
            return

        cache = self._rsz_type_cache
        NON_ARRAY_PARSERS_get = NON_ARRAY_PARSERS.get
        RawBytesData_parse = RawBytesData.parse
        
        for field in fields_def:
            if "_parse_cache" in field:
                continue

            field_name = field.get("name", "")
            field_type = field.get("type", "unknown").lower()
            field_size = field.get("size", 4)
            is_native = field.get("native", False)
            is_array = field.get("array", False)
            field_align = int(field.get("align", 1) or 1)
            if field_align <= 0:
                field_align = 1
            original_type = field.get("original_type", "") or ""

            key = (
                field_type,
                field_size,
                is_native,
                is_array,
                field_align,
                original_type,
                field_name,
            )

            rsz_type = cache.get(key)
            if rsz_type is None:
                rsz_type = get_type_class(*key)
                cache[key] = rsz_type

            parser_func = NON_ARRAY_PARSERS_get(rsz_type, RawBytesData_parse)
            default_element_cls = rsz_type if parser_func is not RawBytesData_parse else RawBytesData

            field["_parse_cache"] = (
                field_name or "<unnamed>",
                rsz_type,
                field_size,
                field_align,
                is_array,
                original_type,
                parser_func,
                default_element_cls,
            )

        self._prepared_field_defs.add(fields_id)

    def _write_field_value(self, field_def: dict, data_obj, out: bytearray):
        field_size = field_def.get("size", 4)
        field_align = field_def.get("align", 1)
        base_mod = getattr(self, "_write_base_mod", 0)

        
        if isinstance(data_obj, StructData):
            while (len(out) - base_mod) % 4:
                out.extend(b'\x00')
            count = len(data_obj.values)
            out.extend(pack_uint(count))
            if count:
                original_type = getattr(data_obj, "orig_type", None)
                if original_type and self.type_registry:
                    struct_info, _ = self.type_registry.find_type_by_name(original_type)
                    if struct_info:
                        struct_fields = struct_info.get("fields", [])
                        for struct_val in data_obj.values:
                            for sub_fd in struct_fields:
                                self._write_field_value(
                                    sub_fd, struct_val[sub_fd["name"]], out
                                )
        elif field_def.get("array", False):

            while (len(out) - base_mod) % 4:
                out.extend(b'\x00')

            count = len(data_obj.values)
            out.extend(pack_uint(count))
            
            for element in data_obj.values:
                while (len(out) - base_mod) % field_align != 0:
                    out.extend(b'\x00')

                if isinstance(element, S8Data):
                    out.extend(pack_sbyte(max(-128, min(127, element.value))))
                elif isinstance(element, U8Data):
                    out.extend(pack_ubyte(element.value))
                elif isinstance(element, BoolData):
                    out.extend(pack_bool(bool(element.value)))
                elif isinstance(element, S16Data):
                    out.extend(pack_short(element.value))
                elif isinstance(element, U16Data):
                    out.extend(pack_ushort(element.value))
                elif isinstance(element, S64Data):
                    out.extend(pack_long(element.value))
                elif isinstance(element, S32Data):
                    out.extend(pack_int(element.value))
                elif isinstance(element, U64Data):
                    out.extend(pack_ulong(element.value))
                elif isinstance(element, F64Data):
                    out.extend(pack_double(element.value))
                elif isinstance(element, Vec2Data):
                    out.extend(pack_4float(element.x, element.y, 0, 0))
                elif isinstance(element, Float2Data):
                    out.extend(pack_2float(element.x, element.y))
                elif isinstance(element, PointData):
                    out.extend(pack_2float(element.x, element.y))
                elif isinstance(element, SizeData):
                    out.extend(pack_2float(element.width, element.height))
                elif isinstance(element, RangeData):
                    out.extend(pack_2float(element.min, element.max))
                elif isinstance(element, RangeIData):
                    out.extend(pack_2int(element.min, element.max))
                elif isinstance(element, Int2Data):
                    out.extend(pack_2int(element.x, element.y))
                elif isinstance(element, Uint2Data):
                    out.extend(pack_uint(element.x))
                    out.extend(pack_uint(element.y))
                elif isinstance(element, Float3Data):
                    out.extend(pack_3float(element.x, element.y, element.z))
                elif isinstance(element, PositionData):
                    out.extend(pack_3double(element.x, element.y, element.z))
                elif isinstance(element, Int3Data):
                    out.extend(pack_3int(element.x, element.y, element.z))
                elif isinstance(element, Uint3Data):
                    out.extend(pack_uint(element.x))
                    out.extend(pack_uint(element.y))
                    out.extend(pack_uint(element.z))
                elif isinstance(element, Float4Data):
                    out.extend(pack_4float(element.x, element.y, element.z, element.w))
                elif isinstance(element, QuaternionData):
                    out.extend(pack_4float(element.x, element.y, element.z, element.w))
                elif isinstance(element, ColorData):
                    out.extend(pack_4ubyte(element.r, element.g, element.b, element.a))
                elif isinstance(element, (ObjectData, U32Data, UserDataData)):
                    value = int(element.value) & 0xFFFFFFFF
                    out.extend(pack_uint(value))
                elif isinstance(element, Vec3Data) or isinstance(element, Vec3ColorData):
                    out.extend(pack_4float(element.x, element.y, element.z, 0.00))
                elif isinstance(element, Vec4Data):
                    out.extend(pack_4float(element.x, element.y, element.z, element.w))
                elif isinstance(element, Mat4Data):
                    if isinstance(element.values, (list, tuple)):
                        float_values = [float(v) for v in element.values[:16]]
                        while len(float_values) < 16:
                            float_values.append(0.0)
                        out.extend(pack_16float(*float_values))
                    else:
                        out.extend(pack_16float(*([0.0] * 16)))
                elif isinstance(element, (GameObjectRefData, GuidData)):
                    guid = uuid.UUID(element.guid_str)
                    out.extend(guid.bytes_le)
                elif isinstance(element, (StringData, ResourceData)):
                    while (len(out) - base_mod) % 4:
                        out.extend(b'\x00')
                    if element.value:
                        value = element.value
                        if not value or value[-1] != '\x00':
                            value += '\x00'
                        str_bytes = value.encode('utf-16-le')
                        char_count = len(str_bytes) // 2
                        out.extend(pack_uint(char_count))
                        out.extend(str_bytes)
                    else:
                        out.extend(pack_uint(0))
                elif isinstance(element, RuntimeTypeData):
                    while (len(out) - base_mod) % 4:
                        out.extend(b'\x00')
                    if element.value:
                        str_bytes = element.value.encode('utf-8')
                        char_count = len(element.value)
                        if element.value[-1] == '\x00':
                            char_count = len(element.value) 
                        else:
                            char_count = len(element.value) + 1
                            str_bytes += b'\x00\x00'
                        out.extend(pack_uint(char_count))
                        out.extend(str_bytes)
                    else:
                        out.extend(pack_uint(0))
                elif isinstance(element, OBBData):
                    if isinstance(element.values, (list, tuple)):
                        float_values = [float(v) for v in element.values[:20]]
                        while len(float_values) < 20:
                            float_values.append(0.0)
                        out.extend(pack_20float(*float_values))
                    else:
                        # Fallback - write zeros
                        out.extend(pack_20float(*([0.0] * 20)))
                elif isinstance(element, RawBytesData):
                    out.extend(element.raw_bytes)
                    remaining_bytes = field_size - len(element.raw_bytes)
                    if remaining_bytes > 0:
                        out.extend(b'\x00' * remaining_bytes)
                elif isinstance(element, F32Data):
                    val_bits = pack_float(element.value)
                    out.extend(val_bits)
                elif isinstance(element, GuidData):
                    if element.raw_bytes:
                        out.extend(element.raw_bytes)
                    else:
                        try:
                            guid = uuid.UUID(element.guid_str)
                            out.extend(guid.bytes_le)
                        except ValueError:
                            out.extend(b'\x00' * 16) 
                elif isinstance(element, CapsuleData):
                    out.extend(pack_3float(element.start.x, element.start.y, element.start.z))
                    out.extend(pack_float(0.0))
                    out.extend(pack_3float(element.end.x, element.end.y, element.end.z))
                    out.extend(pack_float(0.0))
                    out.extend(pack_float(element.radius))
                    out.extend(b'\x00' * 12)

                elif isinstance(element, AABBData):
                    out.extend(pack_3float(element.min.x, element.min.y, element.min.z))
                    out.extend(pack_float(0.0))
                    out.extend(pack_3float(element.max.x, element.max.y, element.max.z))
                    out.extend(pack_float(0.0))
                    
                elif isinstance(element, RectData):
                    out.extend(pack_4float(element.min_x, element.min_y, element.max_x, element.max_y))

                elif isinstance(element, AreaData):
                    out.extend(pack_2float(element.p0.x, element.p0.y))
                    out.extend(pack_2float(element.p1.x, element.p1.y))
                    out.extend(pack_2float(element.p2.x, element.p2.y))
                    out.extend(pack_2float(element.p3.x, element.p3.y))
                    out.extend(pack_float(element.height))
                    out.extend(pack_float(element.bottom))
                    out.extend(b'\x00' * 8)

                elif isinstance(element, AreaDataOld):
                    out.extend(pack_4float(element.p0.x, element.p0.y, 0, 0))
                    out.extend(pack_4float(element.p1.x, element.p1.y, 0, 0))
                    out.extend(pack_4float(element.p2.x, element.p2.y, 0, 0))
                    out.extend(pack_4float(element.p3.x, element.p3.y, 0, 0))
                    out.extend(pack_float(element.height))
                    out.extend(pack_float(element.bottom))
                    out.extend(b'\x00' * 8)
                else:
                    val = getattr(element, 'value', 0)
                    raw_bytes = val.to_bytes(field_size, byteorder='little')
                    out.extend(raw_bytes)
        else:
            while (len(out) - base_mod) % field_align != 0:
                out.extend(b'\x00')

            if isinstance(data_obj, S8Data):
                out.extend(pack_sbyte(max(-128, min(127, data_obj.value))))
            elif isinstance(data_obj, U8Data):
                out.extend(pack_ubyte(data_obj.value))
            elif isinstance(data_obj, S16Data):
                out.extend(pack_short(data_obj.value))
            elif isinstance(data_obj, U16Data):
                out.extend(pack_ushort(data_obj.value))
            elif isinstance(data_obj, S64Data):
                out.extend(pack_long(data_obj.value))
            elif isinstance(data_obj, S32Data):
                out.extend(pack_int(data_obj.value))
            elif isinstance(data_obj, U64Data):
                out.extend(pack_ulong(data_obj.value))
            elif isinstance(data_obj, F64Data):
                out.extend(pack_double(data_obj.value))
            elif isinstance(data_obj, Vec2Data):
                out.extend(pack_4float(data_obj.x, data_obj.y, 0, 0))
            elif isinstance(data_obj, Float2Data):
                out.extend(pack_2float(data_obj.x, data_obj.y))
            elif isinstance(data_obj, PointData):
                out.extend(pack_2float(data_obj.x, data_obj.y))
            elif isinstance(data_obj, SizeData):
                out.extend(pack_2float(data_obj.width, data_obj.height))
            elif isinstance(data_obj, RangeData):
                out.extend(pack_2float(data_obj.min, data_obj.max))
            elif isinstance(data_obj, Int2Data):
                out.extend(pack_2int(data_obj.x, data_obj.y))
            elif isinstance(data_obj, Uint2Data):
                out.extend(pack_uint(data_obj.x))
                out.extend(pack_uint(data_obj.y))
            elif isinstance(data_obj, RangeIData):
                out.extend(pack_2int(data_obj.min, data_obj.max))
            elif isinstance(data_obj, Float3Data):
                out.extend(pack_3float(data_obj.x, data_obj.y, data_obj.z))
            elif isinstance(data_obj, PositionData):
                out.extend(pack_3double(data_obj.x, data_obj.y, data_obj.z))
            elif isinstance(data_obj, Int3Data):
                out.extend(pack_3int(data_obj.x, data_obj.y, data_obj.z))
            elif isinstance(data_obj, Uint3Data):
                out.extend(pack_uint(data_obj.x))
                out.extend(pack_uint(data_obj.y))
                out.extend(pack_uint(data_obj.z))
            elif isinstance(data_obj, Float4Data):
                out.extend(pack_4float(data_obj.x, data_obj.y, data_obj.z, data_obj.w))
            elif isinstance(data_obj, QuaternionData):
                out.extend(pack_4float(data_obj.x, data_obj.y, data_obj.z, data_obj.w))
            elif isinstance(data_obj, ColorData):
                out.extend(pack_4ubyte(data_obj.r, data_obj.g, data_obj.b, data_obj.a))
            elif isinstance(data_obj, (ObjectData, U32Data, UserDataData)):
                value = int(data_obj.value) & 0xFFFFFFFF
                out.extend(pack_uint(value))
            elif isinstance(data_obj, Vec3Data) or isinstance(data_obj, Vec3ColorData):
                out.extend(pack_4float(data_obj.x, data_obj.y, data_obj.z, 0.00))
            elif isinstance(data_obj, Vec4Data):
                out.extend(pack_4float(data_obj.x, data_obj.y, data_obj.z, data_obj.w))
            elif isinstance(data_obj, (GameObjectRefData, GuidData)):
                guid = uuid.UUID(data_obj.guid_str)
                out.extend(guid.bytes_le)
            elif isinstance(data_obj, (StringData, ResourceData)):
                while (len(out) - base_mod) % 4:
                    out.extend(b'\x00')
                    
                if data_obj.value:
                    value = data_obj.value
                    if not value or value[-1] != '\x00':
                        value += '\x00'
                    str_bytes = value.encode('utf-16-le')
                    char_count = len(str_bytes) // 2
                    out.extend(pack_uint(char_count))
                    out.extend(str_bytes)
                else:
                    out.extend(pack_uint(0))
            elif isinstance(data_obj, (RuntimeTypeData)):
                while (len(out) - base_mod) % 4:
                    out.extend(b'\x00')
                    
                if data_obj.value:
                    str_bytes = data_obj.value.encode('utf-8')
                    char_count = len(data_obj.value)
                    if data_obj.value[-1] == '\x00':
                        char_count = len(data_obj.value)
                    else:
                        char_count = len(data_obj.value) + 1
                        str_bytes += b'\x00\x00'
                    out.extend(pack_uint(char_count))
                    out.extend(str_bytes)
                else:
                    out.extend(pack_uint(0))
            elif isinstance(data_obj, BoolData):
                out.extend(pack_bool(bool(data_obj.value)))
            elif isinstance(data_obj, F32Data):
                val_bits = pack_float(data_obj.value)
                out.extend(val_bits)
            elif isinstance(data_obj, OBBData):
                if isinstance(data_obj.values, (list, tuple)):
                    out.extend(pack_20float(*[float(v) for v in data_obj.values]))
                else:
                    values = [float(x) for x in str(data_obj.values).strip('()').split(',')]
                    out.extend(pack_20float(*values))
            elif isinstance(data_obj, Mat4Data):
                if isinstance(data_obj.values, (list, tuple)):
                    out.extend(pack_16float(*[float(v) for v in data_obj.values]))
                else:
                    values = [float(x) for x in str(data_obj.values).strip('()').split(',')]
                    out.extend(pack_16float(*values))
            elif isinstance(data_obj, RawBytesData):
                out.extend(data_obj.raw_bytes)
                remaining_bytes = field_size - len(data_obj.raw_bytes)
                if remaining_bytes > 0:
                    out.extend(b'\x00' * remaining_bytes)
            elif isinstance(data_obj, CapsuleData):
                out.extend(pack_3float(data_obj.start.x, data_obj.start.y, data_obj.start.z))
                out.extend(pack_float(0.0))
                out.extend(pack_3float(data_obj.end.x, data_obj.end.y, data_obj.end.z))
                out.extend(pack_float(0.0))
                out.extend(pack_float(data_obj.radius))
                out.extend(b'\x00' * 12)

            elif isinstance(data_obj, AABBData):
                out.extend(pack_3float(data_obj.min.x, data_obj.min.y, data_obj.min.z))
                out.extend(pack_float(0.0))
                out.extend(pack_3float(data_obj.max.x, data_obj.max.y, data_obj.max.z))
                out.extend(pack_float(0.0))

            elif isinstance(data_obj, RectData):
                out.extend(pack_4float(data_obj.min_x, data_obj.min_y, data_obj.max_x, data_obj.max_y))

            elif isinstance(data_obj, AreaData):
                out.extend(pack_2float(data_obj.p0.x, data_obj.p0.y))
                out.extend(pack_2float(data_obj.p1.x, data_obj.p1.y))
                out.extend(pack_2float(data_obj.p2.x, data_obj.p2.y))
                out.extend(pack_2float(data_obj.p3.x, data_obj.p3.y))
                out.extend(pack_float(data_obj.height))
                out.extend(pack_float(data_obj.bottom))
                out.extend(b'\x00' * 8)

            elif isinstance(data_obj, AreaDataOld):
                out.extend(pack_4float(data_obj.p0.x, data_obj.p0.y, 0, 0))
                out.extend(pack_4float(data_obj.p1.x, data_obj.p1.y, 0, 0))
                out.extend(pack_4float(data_obj.p2.x, data_obj.p2.y, 0, 0))
                out.extend(pack_4float(data_obj.p3.x, data_obj.p3.y, 0, 0))
                out.extend(pack_float(data_obj.height))
                out.extend(pack_float(data_obj.bottom))
                out.extend(b'\x00' * 8)

            else:
                #print("last is else")
                val = getattr(data_obj, 'value', 0)
                if isinstance(val, float):
                    val_bits = pack_float(val)
                    out.extend(val_bits)
                else:
                    def write_raw_value(val):
                        try:
                            raw_bytes = val.to_bytes(field_size, byteorder='little', signed=False)
                        except OverflowError:
                            raw_bytes = val.to_bytes(field_size, byteorder='little', signed=True)
                        out.extend(raw_bytes)
                    write_raw_value(val)

    def _write_instance_data(self) -> bytes:
        """Write all instance data according to type definitions"""
        out = bytearray()
        
        for instance_id, fields in sorted(self.parsed_elements.items()):
            if instance_id == 0:  
                continue
                
            inst_info = self.instance_infos[instance_id]
            type_info = self.type_registry.get_type_info(inst_info.type_id)
            if not type_info:
                continue
                
            fields_def = type_info.get("fields", [])
            
            for field_def in fields_def:
                field_name = field_def["name"]
                if field_name in fields:
                    self._write_field_value(field_def, fields[field_name], out)
                    
        return bytes(out)
    
    def get_resources_dynamically(self):
        """Get resources dynamically based on resource fields"""
        resources = []

        def _add_resource(value):
            val = value.rstrip("\0")
            if val and val not in resources:
                resources.append(val)

        def _collect_from_fields(fields, fields_def):
            for field_def in fields_def:
                field_name = field_def["name"]
                data_obj = fields.get(field_name)
                if not data_obj:
                    continue
                _collect_from_field(data_obj, field_def)

        def _collect_from_field(data_obj, field_def):
            if field_def["type"] == "Resource":
                if not field_def.get("array", False):
                    _add_resource(data_obj.value)
                else:
                    for elem in data_obj.values:
                        _add_resource(elem.value)
                return

            if field_def["type"] == "Struct" and isinstance(data_obj, StructData):
                struct_type_name = field_def.get("original_type") or getattr(data_obj, "orig_type", "")
                if struct_type_name and self.type_registry:
                    struct_info, _ = self.type_registry.find_type_by_name(struct_type_name)
                    if struct_info:
                        struct_fields_def = struct_info.get("fields", [])
                        for struct_fields in data_obj.values:
                            _collect_from_fields(struct_fields, struct_fields_def)
                            
        def _collect_segment(parsed_elements, instance_infos, userdata_infos):
            by_instance = {}
            for rui in userdata_infos or []:
                by_instance.setdefault(rui.instance_id, []).append(rui)

            for instance_id in range(len(instance_infos)):
                fields = parsed_elements.get(instance_id)
                if fields is not None:
                    inst_info = instance_infos[instance_id]
                    type_info = self.type_registry.get_type_info(inst_info.type_id)
                    name = type_info.get("name", [])
                    fields_def = type_info.get("fields", [])

                    if name == "via.Prefab" or name == "app.global.ResourcePrefab":
                        f0, f1 = fields_def[0]["name"], fields_def[1]["name"]
                        if fields[f0].value:
                            val = fields[f1].value.rstrip("\0")
                            if val and val not in resources:
                                resources.append(val)

                    elif name == "via.Folder":
                        f4, f5 = fields_def[4]["name"], fields_def[5]["name"]
                        if fields[f4].value:
                            _add_resource(fields[f5].value)

                    else:
                        _collect_from_fields(fields, fields_def)

                for rui in by_instance.get(instance_id, []):
                    _collect_segment(
                        rui.embedded_instances,
                        rui.embedded_instance_infos,
                        getattr(rui, "embedded_userdata_infos", None),
                    )

        if getattr(self, "is_scn", False) and (self.filepath.lower().endswith(".19") or self.filepath.lower().endswith('.18')):
            _collect_segment(self.parsed_elements, self.instance_infos, self.rsz_userdata_infos)
        else:
            _collect_segment(self.parsed_elements, self.instance_infos, None)

        return resources

    def rebuild_resources(self):
        from file_handlers.rsz.pfb_16.pfb_structure import Pfb16ResourceInfo
        dynamic_resources = self.get_resources_dynamically()
        self.resource_infos.clear()
        self._resource_str_map.clear()
        if hasattr(self, '_pfb16_direct_strings'):
            self._pfb16_direct_strings = dynamic_resources
        for resource_path in dynamic_resources:
            if getattr(self, "is_pfb16", False):
                ri = Pfb16ResourceInfo(resource_path)
            else:
                ri = RszResourceInfo()
            ri.string_offset = 0
            ri.reserved = 0
            self.resource_infos.append(ri)
            self.set_resource_string(ri, resource_path)

    def build(self, special_align_enabled = False) -> bytes:
        if self.auto_resource_management:
            self.rebuild_resources()

        if self.is_usr:
            return self._build_usr(special_align_enabled)
        elif self.is_pfb:
            if self.filepath.lower().endswith('.16'):
                return build_pfb_16(self, special_align_enabled)
            else:
                return self._build_pfb(special_align_enabled)
        elif self.filepath.lower().endswith('.19'):
                    return build_scn_19(self, special_align_enabled)
        elif self.filepath.lower().endswith('.18'):
            return build_scn_18(self, special_align_enabled)
                
        self.header.info_count = len(self.gameobjects)
        self.header.folder_count = len(self.folder_infos)
        self.header.resource_count = len(self.resource_infos)
        self.header.prefab_count = len(self.prefab_infos)
        self.header.userdata_count = len(self.userdata_infos)
        
        self.rsz_header.object_count = len(self.object_table)
        self.rsz_header.instance_count = len(self.instance_infos)
        self.rsz_header.userdata_count = len(self.rsz_userdata_infos)
            
        out = bytearray()
        
        # 1) Write header
        out += struct.pack(
            "<4s5I5Q",
            self.header.signature,
            self.header.info_count,
            self.header.resource_count,
            self.header.folder_count,
            self.header.prefab_count,
            self.header.userdata_count,
            0,  # folder_tbl - placeholder
            0,  # resource_info_tbl - placeholder
            0,  # prefab_info_tbl - placeholder
            0,  # userdata_info_tbl - placeholder
            0   # data_offset - placeholder
        )

        # 2) Write gameobjects
        for go in self.gameobjects:
            out += go.guid
            out += struct.pack("<i", go.id)
            out += struct.pack("<i", go.parent_id)
            out += struct.pack("<H", go.component_count)
            out += struct.pack("<h", go.ukn)
            out += struct.pack("<i", go.prefab_id) 

        # 3) Align and write folder infos, recording folder_tbl offset
        while len(out) % 16 != 0:
            out += b"\x00"
        folder_tbl_offset = len(out)
        for fi in self.folder_infos:
            out += struct.pack("<ii", fi.id, fi.parent_id)

        # 4) Align and prepare for resource infos
        while len(out) % 16 != 0:
            out += b"\x00"
        resource_info_tbl_offset = len(out)
        
        # Calculate new string offsets for resource strings
        resource_strings_offset = 0
        new_resource_offsets = {}
        current_offset = resource_info_tbl_offset + len(self.resource_infos) * 8  # Each resource info is 8 bytes
        
        # Align to 16 after resource infos
        current_offset = _align(current_offset, 16)
        
        # Skip prefab infos table
        current_offset += len(self.prefab_infos) * 8
        
        # Align to 16 after prefab infos
        current_offset = _align(current_offset, 16)
        
        # Skip userdata infos table
        current_offset += len(self.userdata_infos) * 16  # Each userdata info is 16 bytes
        
        # Begin calculating string offsets
        resource_strings_offset = current_offset
        
        for ri in self.resource_infos:
            resource_string = self._resource_str_map.get(ri, "")
            if resource_string:
                new_resource_offsets[ri] = resource_strings_offset
                resource_strings_offset += len(resource_string.encode('utf-16-le')) + 2  # +2 for null terminator
            else:
                new_resource_offsets[ri] = 0
        
        # Calculate prefab string offsets
        prefab_strings_offset = resource_strings_offset
        new_prefab_offsets = {}
        
        for pi in self.prefab_infos:
            prefab_string = self._prefab_str_map.get(pi, "")
            if prefab_string:
                new_prefab_offsets[pi] = prefab_strings_offset
                prefab_strings_offset += len(prefab_string.encode('utf-16-le')) + 2  # +2 for null terminator
            else:
                new_prefab_offsets[pi] = 0
        
        # Calculate userdata string offsets
        userdata_strings_offset = prefab_strings_offset
        new_userdata_offsets = {}
        
        for ui in self.userdata_infos:
            userdata_string = self._userdata_str_map.get(ui, "")
            if userdata_string:
                new_userdata_offsets[ui] = userdata_strings_offset
                userdata_strings_offset += len(userdata_string.encode('utf-16-le')) + 2  # +2 for null terminator
            else:
                new_userdata_offsets[ui] = 0
        
        # Now write resource infos with updated offsets
        for ri in self.resource_infos:
            ri.string_offset = new_resource_offsets[ri]
            out += struct.pack("<II", ri.string_offset, ri.reserved)

        # 5) Align and write prefab infos with updated offsets
        while len(out) % 16 != 0:
            out += b"\x00"
        prefab_info_tbl_offset = len(out)
        for pi in self.prefab_infos:
            pi.string_offset = new_prefab_offsets[pi]
            out += struct.pack("<II", pi.string_offset, pi.parent_id)

        # 6) Align and write user data infos with updated offsets
        while len(out) % 16 != 0:
            out += b"\x00"
        userdata_info_tbl_offset = len(out)
        for ui in self.userdata_infos:
            ui.string_offset = new_userdata_offsets[ui]
            out += struct.pack("<IIQ", ui.hash, 0, ui.string_offset)

        # Write strings in order of their offsets
        string_entries = []
        
        # Only collect string entries that have calculated offsets
        for ri, offset in new_resource_offsets.items():
            if offset:
                string_entries.append((offset, self._resource_str_map.get(ri, "").encode("utf-16-le") + b"\x00\x00"))
                
        for pi, offset in new_prefab_offsets.items():
            if offset: 
                string_entries.append((offset, self._prefab_str_map.get(pi, "").encode("utf-16-le") + b"\x00\x00"))
                
        for ui, offset in new_userdata_offsets.items():
            if offset: 
                string_entries.append((offset, self._userdata_str_map.get(ui, "").encode("utf-16-le") + b"\x00\x00"))
        
        # Sort by offset
        string_entries.sort(key=lambda x: x[0])
        
        # Write strings in order
        current_offset = string_entries[0][0] if string_entries else len(out)
        while len(out) < current_offset:
            out += b"\x00"
            
        for offset, string_data in string_entries:
            while len(out) < offset:
                out += b"\x00"
            out += string_data

        # 9) Write RSZ header/tables/userdata
        if self.rsz_header:
            # Ensure RSZ header starts on 16-byte alignment
            if(special_align_enabled):
                while len(out) % 16 != 0:
                    out += b"\x00"
                
            rsz_start = len(out)
            self.header.data_offset = rsz_start

            # Calculate sizes
            object_table_size = self.rsz_header.object_count * 4
            instance_info_size = len(self.instance_infos) * 8
            
            # Calculate offsets relative to RSZ section start
            instance_info_offset = self.rsz_header.SIZE + object_table_size  # After header and object table
            
            # Userdata offset comes after instance infos
            userdata_offset = instance_info_offset + instance_info_size
            
            # Data offset comes after userdata section, and needs 16-byte alignment
            data_offset = userdata_offset + (len(self.rsz_userdata_infos) * 16)  # Each userdata entry is 16 bytes
            data_offset = _align(data_offset, 16)
            
            # Write RSZ header with corrected offsets
            rsz_header_bytes = struct.pack(
                "<5I I Q Q Q",
                0,
                0,
                0,
                0,
                0,
                0,                             # Reserved 4 bytes
                0,          # Points to instance info table
                0,                   # Points to instance data
                0                # Points to userdata section
            )
            out += rsz_header_bytes


            # 9b) Write Object Table (one 4-byte integer per object)
            for obj_id in self.object_table:
                # Ensure we write object IDs as signed integers
                out += struct.pack("<i", obj_id)
            

            # Align to 16 bytes first and calculate relative instance offset
            if(special_align_enabled):
                while len(out) % 16 != 0:
                    out += b"\x00"
                
            new_instance_offset = len(out) - rsz_start  # Calculate offset relative to RSZ start
            
            for inst in self.instance_infos:
                out += struct.pack("<II", inst.type_id, inst.crc)


            # Align to 16 at end
            while len(out) % 16 != 0:
                out += b"\x00"

            new_userdata_offset = len(out) - rsz_start 

            # 9d) Write RSZUserDataInfos entries with placeholder for string offsets.
            sorted_rsz_userdata_infos = sorted(self.rsz_userdata_infos, key=lambda rui: rui.instance_id)
            userdata_entries = []
            for rui in sorted_rsz_userdata_infos:
                entry_offset = len(out) - rsz_start
                out += struct.pack("<IIQ", rui.instance_id, rui.hash, 0)
                userdata_entries.append((entry_offset, rui))
            # Write strings for each user data entry and update its string offset.
            for entry_offset, rui in userdata_entries:
                current_string_offset = len(out) - rsz_start
                string_data = self.get_rsz_userdata_string(rui).encode("utf-16-le") + b"\x00\x00"
                out += string_data
                struct.pack_into("<Q", out, rsz_start + entry_offset + 8, current_string_offset)
            # 9e) Align before instance data block and compute new data offset
            while (len(out) % 16) != 0:
                out += b"\x00"
            new_data_offset = len(out) - rsz_start

            # 9f) Update the RSZ header with correct counts and relative offsets.
            new_rsz_header = struct.pack(
                "<I I I I I I Q Q Q",
                self.rsz_header.magic,
                self.rsz_header.version,
                self.rsz_header.object_count,
                len(self.instance_infos),           # Updated instance count
                len(self.rsz_userdata_infos),         # Updated userdata count
                self.rsz_header.reserved,
                new_instance_offset,                  # instance_offset (relative to RSZ header)
                new_data_offset,                      # data_offset (relative to RSZ header)
                new_userdata_offset                   # userdata_offset (relative to RSZ header)
            )
            out[rsz_start:rsz_start + self.rsz_header.SIZE] = new_rsz_header

        # 10) Write instance data

        while len(out) % 16 != 0:
            out += b"\x00"
            
        # Write actual instance data block
        instance_data = self._write_instance_data()
        out += instance_data

        # Before rewriting header, update all table offsets:
        self.header.folder_tbl = folder_tbl_offset
        self.header.resource_info_tbl = resource_info_tbl_offset
        self.header.prefab_info_tbl = prefab_info_tbl_offset
        self.header.userdata_info_tbl = userdata_info_tbl_offset
        # PS header.data_offset was already updated in the in the RSZ section

        # Finally, rewrite header with updated offsets
        header_bytes = struct.pack(
            "<4s5I5Q",
            self.header.signature,
            self.header.info_count,
            self.header.resource_count,
            self.header.folder_count,
            self.header.prefab_count,
            self.header.userdata_count,
            self.header.folder_tbl,
            self.header.resource_info_tbl,
            self.header.prefab_info_tbl,
            self.header.userdata_info_tbl,
            self.header.data_offset
        )
        out[0:ScnHeader.SIZE] = header_bytes
        
        
        return bytes(out)

    def _build_usr(self, special_align_enabled=False) -> bytes:
        self.header.resource_count = len(self.resource_infos)
        self.header.userdata_count = len(self.userdata_infos)
        
        if self.rsz_header:
            self.rsz_header.object_count = len(self.object_table)
            self.rsz_header.instance_count = len(self.instance_infos)
            self.rsz_header.userdata_count = len(self.rsz_userdata_infos)
        
        out = bytearray()
        
        # 1) Write USR header with zeroed offsets initially 
        out += struct.pack(
            "<4s3I3QQ", 
            self.header.signature,
            self.header.resource_count,
            self.header.userdata_count,
            self.header.info_count,
            0,  # resource_info_tbl
            0,  # userdata_info_tbl
            0,  # data_offset
            self.header.reserved  
        )

        # 2) Calculate offsets for string tables
        resource_info_tbl = _align(len(out), 16)
        resource_info_size = len(self.resource_infos) * 8  # Each resource info is 8 bytes
        
        userdata_info_tbl = _align(resource_info_tbl + resource_info_size, 16)
        userdata_info_size = len(self.userdata_infos) * 16  # Each userdata info is 16 bytes
        
        # Calculate string positions
        string_start = _align(userdata_info_tbl + userdata_info_size, 16)
        current_offset = string_start
        
        # Calculate resource string offsets
        new_resource_offsets = {}
        for ri in self.resource_infos:
            resource_string = self._resource_str_map.get(ri, "")
            if resource_string:
                new_resource_offsets[ri] = current_offset
                current_offset += len(resource_string.encode('utf-16-le')) + 2  # +2 for null terminator
            else:
                new_resource_offsets[ri] = 0
        
        # Calculate userdata string offsets
        new_userdata_offsets = {}
        for ui in self.userdata_infos:
            userdata_string = self._userdata_str_map.get(ui, "")
            if userdata_string:
                new_userdata_offsets[ui] = current_offset
                current_offset += len(userdata_string.encode('utf-16-le')) + 2  # +2 for null terminator
            else:
                new_userdata_offsets[ui] = 0
        
        # Align to 16 bytes and write resource info table
        while len(out) % 16 != 0:
            out += b"\x00"
        resource_info_tbl = len(out)
        
        for ri in self.resource_infos:
            ri.string_offset = new_resource_offsets[ri]
            out += struct.pack("<II", ri.string_offset, ri.reserved)

        # Align to 16 bytes and write userdata info table
        while len(out) % 16 != 0:
            out += b"\x00"
        userdata_info_tbl = len(out)
        
        for ui in self.userdata_infos:
            ui.string_offset = new_userdata_offsets[ui]
            out += struct.pack("<IIQ", ui.hash, 0, ui.string_offset)

        # Write strings in order of their offsets
        string_entries = []
        
        for ri, offset in new_resource_offsets.items():
            if offset:
                string_entries.append((offset, self._resource_str_map.get(ri, "").encode("utf-16-le") + b"\x00\x00"))
                
        for ui, offset in new_userdata_offsets.items():
            if offset:
                string_entries.append((offset, self._userdata_str_map.get(ui, "").encode("utf-16-le") + b"\x00\x00"))
        
        # Sort by offset
        string_entries.sort(key=lambda x: x[0])
        
        # Write strings in order
        current_offset = string_entries[0][0] if string_entries else len(out)
        while len(out) < current_offset:
            out += b"\x00"
            
        for offset, string_data in string_entries:
            while len(out) < offset:
                out += b"\x00"
            out += string_data

        # 6) RSZ Section
        if self.rsz_header:
            if special_align_enabled:
                while len(out) % 16 != 0:
                    out += b"\x00"
                    
            rsz_start = len(out)
            self.header.data_offset = rsz_start

            # Write RSZ header with placeholder offsets
            rsz_header_bytes = struct.pack(
                "<5I I Q Q Q",
                self.rsz_header.magic,
                self.rsz_header.version,
                self.rsz_header.object_count,
                len(self.instance_infos),
                len(self.rsz_userdata_infos),
                self.rsz_header.reserved,
                0,  # instance_offset - will update later
                0,  # data_offset - will update later 
                0   # userdata_offset - will update later
            )
            out += rsz_header_bytes

            # Write object table
            for obj_id in self.object_table:
                out += struct.pack("<i", obj_id)

            # Add 8 null bytes before instance infos (no 16-byte alignment)
            new_instance_offset = len(out) - rsz_start
            
            for inst in self.instance_infos:
                out += struct.pack("<II", inst.type_id, inst.crc)

            # Write userdata at 16-byte alignment
            while len(out) % 16 != 0:
                out += b"\x00"
            new_userdata_offset = len(out) - rsz_start

            # Write userdata entries and strings
            userdata_entries = []
            for rui in self.rsz_userdata_infos:
                entry_offset = len(out) - rsz_start
                out += struct.pack("<IIQ", rui.instance_id, rui.hash, 0)
                userdata_entries.append((entry_offset, rui))

            for entry_offset, rui in userdata_entries:
                string_offset = len(out) - rsz_start
                string_data = self.get_rsz_userdata_string(rui).encode("utf-16-le") + b"\x00\x00"
                out += string_data
                struct.pack_into("<Q", out, rsz_start + entry_offset + 8, string_offset)

            # Write instance data at 16-byte alignment
            while len(out) % 16 != 0:
                out += b"\x00"
            new_data_offset = len(out) - rsz_start
            
            instance_data = self._write_instance_data()
            out += instance_data

            # Update RSZ header with actual offsets
            new_rsz_header = struct.pack(
                "<5I I Q Q Q",
                self.rsz_header.magic,
                self.rsz_header.version, 
                self.rsz_header.object_count,
                len(self.instance_infos),
                len(self.rsz_userdata_infos),
                self.rsz_header.reserved,
                new_instance_offset,
                new_data_offset,
                new_userdata_offset
            )
            out[rsz_start:rsz_start + self.rsz_header.SIZE] = new_rsz_header

        # 7) Update USR header
        header_bytes = struct.pack(
            "<4s3I3QQ", 
            self.header.signature,
            self.header.resource_count,
            self.header.userdata_count,
            self.header.info_count,
            resource_info_tbl,
            userdata_info_tbl,
            self.header.data_offset,
            self.header.reserved 
        )
        out[0:UsrHeader.SIZE] = header_bytes

        return bytes(out)

    def _build_pfb(self, special_align_enabled = False) -> bytes:
        self.header.info_count = len(self.gameobjects)
        self.header.resource_count = len(self.resource_infos)
        self.header.gameobject_ref_info_count = len(self.gameobject_ref_infos)
        self.header.userdata_count = len(self.userdata_infos)
        
        if self.rsz_header:
            self.rsz_header.object_count = len(self.object_table)
            self.rsz_header.instance_count = len(self.instance_infos)
            self.rsz_header.userdata_count = len(self.rsz_userdata_infos)
        
        out = bytearray()
        
        # 1) Write PFB header with zeroed offsets initially
        out += struct.pack(
            "<4s5I4Q",
            self.header.signature,
            self.header.info_count,
            self.header.resource_count,
            self.header.gameobject_ref_info_count,
            self.header.userdata_count,
            self.header.reserved,
            0,  # gameobject_ref_info_tbl - will update later
            0,  # resource_info_tbl - will update later
            0,  # userdata_info_tbl - will update later
            0   # data_offset - will update later
        )

        # 2) Write gameobjects - PFB format is simpler (12 bytes each)
        for go in self.gameobjects:
            out += struct.pack("<iii", go.id, go.parent_id, go.component_count)
        
        # 3) Write GameObjectRefInfos
        gameobject_ref_info_tbl = len(out)
        for gori in self.gameobject_ref_infos:
            out += struct.pack("<4i", gori.object_id, gori.property_id, gori.array_index, gori.target_id)
        
        # Calculate string offsets
        resource_info_tbl = _align(len(out), 16)
        resource_info_size = len(self.resource_infos) * 8  # Each resource info is 8 bytes
        
        userdata_info_tbl = _align(resource_info_tbl + resource_info_size, 16)
        userdata_info_size = len(self.userdata_infos) * 16  # Each userdata info is 16 bytes
        
        # Calculate string positions
        string_start = _align(userdata_info_tbl + userdata_info_size, 16)
        current_offset = string_start
        
        # Calculate resource string offsets
        new_resource_offsets = {}
        for ri in self.resource_infos:
            resource_string = self._resource_str_map.get(ri, "")
            if resource_string:
                new_resource_offsets[ri] = current_offset
                current_offset += len(resource_string.encode('utf-16-le')) + 2  # +2 for null terminator
            else:
                new_resource_offsets[ri] = 0
        
        # Calculate userdata string offsets
        new_userdata_offsets = {}
        for ui in self.userdata_infos:
            userdata_string = self._userdata_str_map.get(ui, "")
            if userdata_string:
                new_userdata_offsets[ui] = current_offset
                current_offset += len(userdata_string.encode('utf-16-le')) + 2  # +2 for null terminator
            else:
                new_userdata_offsets[ui] = 0
        
        # 4) Align and write resource infos with updated offsets
        while len(out) % 16 != 0:
            out += b"\x00"
        resource_info_tbl = len(out)
        
        for ri in self.resource_infos:
            ri.string_offset = new_resource_offsets[ri]
            out += struct.pack("<II", ri.string_offset, ri.reserved)

        # 5) Align and write userdata infos with updated offsets
        while len(out) % 16 != 0:
            out += b"\x00"
        userdata_info_tbl = len(out)
        
        for ui in self.userdata_infos:
            ui.string_offset = new_userdata_offsets[ui]
            out += struct.pack("<IIQ", ui.hash, 0, ui.string_offset)

        # Write strings in order of their offsets
        string_entries = []
        
        for ri, offset in new_resource_offsets.items():
            if offset: 
                string_entries.append((offset, self._resource_str_map.get(ri, "").encode("utf-16-le") + b"\x00\x00"))
                
        for ui, offset in new_userdata_offsets.items():
            if offset: 
                string_entries.append((offset, self._userdata_str_map.get(ui, "").encode("utf-16-le") + b"\x00\x00"))
        
        # Sort by offset
        string_entries.sort(key=lambda x: x[0])
        
        # Write strings in order
        current_offset = string_entries[0][0] if string_entries else len(out)
        while len(out) < current_offset:
            out += b"\x00"
            
        for offset, string_data in string_entries:
            while len(out) < offset:
                out += b"\x00"
            out += string_data

        # 8) Build the common RSZ section
        self._build_rsz_section(out, special_align_enabled)

        # 9) Update PFB header
        header_bytes = struct.pack(
            "<4s5I4Q", 
            self.header.signature,
            self.header.info_count,
            self.header.resource_count,
            self.header.gameobject_ref_info_count,
            self.header.userdata_count,
            self.header.reserved,
            gameobject_ref_info_tbl,
            resource_info_tbl,
            userdata_info_tbl,
            self.header.data_offset
        )
        out[0:PfbHeader.SIZE] = header_bytes

        return bytes(out)
    
    def _build_rsz_section(self, out: bytearray, special_align_enabled = False):
        """Build the RSZ section that's common to all file formats.
        Returns the rsz_start position."""
        # Ensure RSZ header starts on 16-byte alignment
        if special_align_enabled:
            while len(out) % 16 != 0:
                out += b"\x00"
                
        rsz_start = len(out)
        self.header.data_offset = rsz_start

        # Write RSZ header with placeholder offsets
        rsz_header_bytes = struct.pack(
            "<5I I Q Q Q",
            self.rsz_header.magic,
            self.rsz_header.version,
            self.rsz_header.object_count,
            len(self.instance_infos),
            len(self.rsz_userdata_infos),
            self.rsz_header.reserved,
            0,  # instance_offset - will update later
            0,  # data_offset - will update later 
            0   # userdata_offset - will update later
        )
        out += rsz_header_bytes

        # Write object table
        for obj_id in self.object_table:
            out += struct.pack("<i", obj_id)

        # Write instance infos
        new_instance_offset = len(out) - rsz_start
        for inst in self.instance_infos:
            out += struct.pack("<II", inst.type_id, inst.crc)

        # Write userdata at 16-byte alignment
        while len(out) % 16 != 0:
            out += b"\x00"
        new_userdata_offset = len(out) - rsz_start

        # Write userdata entries and strings
        userdata_entries = []
        for rui in self.rsz_userdata_infos:
            entry_offset = len(out) - rsz_start
            out += struct.pack("<IIQ", rui.instance_id, rui.hash, 0)
            userdata_entries.append((entry_offset, rui))

        for entry_offset, rui in userdata_entries:
            string_offset = len(out) - rsz_start
            string_data = self.get_rsz_userdata_string(rui).encode("utf-16-le") + b"\x00\x00"
            out += string_data
            struct.pack_into("<Q", out, rsz_start + entry_offset + 8, string_offset)

        # Write instance data at 16-byte alignment
        while len(out) % 16 != 0:
            out += b"\x00"
        new_data_offset = len(out) - rsz_start
        
        instance_data = self._write_instance_data()
        out += instance_data

        # Update RSZ header with actual offsets
        new_rsz_header = struct.pack(
            "<5I I Q Q Q",
            self.rsz_header.magic,
            self.rsz_header.version, 
            self.rsz_header.object_count,
            len(self.instance_infos),
            len(self.rsz_userdata_infos),
            self.rsz_header.reserved,
            new_instance_offset,
            new_data_offset,
            new_userdata_offset
        )
        out[rsz_start:rsz_start + self.rsz_header.SIZE] = new_rsz_header

    def get_resource_string(self, ri):
        """Get resource string with special handling for PFB.16 format"""
        if self.is_pfb16:
            if ri.string_value:
                return ri.string_value
                
            if ri in self._resource_str_map:
                return self._resource_str_map[ri]
                
                    
            return f"[Resource {self.resource_infos.index(ri) if ri in self.resource_infos else '?'}]"
        
        return self._resource_str_map.get(ri, "")
    
    def get_prefab_string(self, pi):
        return self._prefab_str_map.get(pi, "")
    
    def get_userdata_string(self, ui):
        return self._userdata_str_map.get(ui, "")
    
    def set_resource_string(self, ri, new_string: str):
        """Set resource string with special handling for PFB.16 format"""
        try:
            if self.is_pfb16 and hasattr(ri, 'string_value'):
                ri.string_value = new_string
            self._resource_str_map[ri] = new_string
        except Exception as e:
            print(f"Error setting resource string: {e}")
    
    def set_prefab_string(self, pi, new_string: str):
        self._prefab_str_map[pi] = new_string
    
    def set_userdata_string(self, ui, new_string: str):
        self._userdata_str_map[ui] = new_string
    
    def set_rsz_userdata_string(self, rui, new_string: str):
        self._rsz_userdata_str_map[rui] = new_string
        
    def parse_instance_fields(self, offset: int, fields_def: list, current_instance_index=None):
        """Parse fields from raw data according to field definitions – optimized version."""
        rsz_userdata_by_id = self._rsz_userdata_dict
        parsed_elements   = self.parsed_elements.setdefault(current_instance_index, {})
        instance_hierarchy = self.instance_hierarchy
        gameobject_ids    = self._gameobject_instance_ids
        folder_ids        = self._folder_instance_ids
        rsz_userdata_map  = self._rsz_userdata_str_map
        current_hierarchy = instance_hierarchy[current_instance_index]
        current_children  = current_hierarchy["children"]

        self._prepare_field_definitions(fields_def)

        pos = offset

        # compute how the file's data-block was aligned (0 for v4+, 8 for v3, etc.)
        base_mod = getattr(self, "_instance_base_mod", 0)

        # Inline alignment for better performance
        def _align(p: int, a: int) -> int:
            if a <= 1:
                return p
            rem = (p + base_mod) % a
            return p if rem == 0 else p + (a - rem)


        hierarchy_len = len(instance_hierarchy)

        def set_parent(idx, parent_idx):
            if 0 <= idx < hierarchy_len:
                instance_hierarchy[idx]["parent"] = parent_idx
            else:
                print(f"Warning: Invalid instance index {idx} encountered (parent: {parent_idx})")

        def is_valid_ref(candidate):
            return (
                0 < candidate < current_instance_index and
                candidate not in gameobject_ids and
                candidate not in folder_ids
            )

        parser_pool = self._parser_pool
        if parser_pool:
            non_array_parser = parser_pool.pop()
        else:
            non_array_parser = _NonArrayFieldParser(self)

        non_array_parser.reset_context(
            base_mod,
            current_children,
            set_parent,
            is_valid_ref,
            current_instance_index,
            rsz_userdata_by_id,
            rsz_userdata_map,
        )

        configure = non_array_parser.configure
        read_value_with_raw = non_array_parser.read_value_with_raw
        parser_unpack_uint = non_array_parser.unpack_uint
        set_parsed = parsed_elements.__setitem__
        append_child = current_children.append
        check_valid_ref = non_array_parser.is_valid_ref
        type_registry = self.type_registry

        try:
            # Direct cache access - _prepare_field_definitions guarantees _parse_cache exists
            data = self.data
            for field in fields_def:
                cache_entry = field["_parse_cache"]
                field_name, rsz_type, fsize, field_align, is_array, original_type, parser_func, default_element_class = cache_entry

                if is_array:
                    pos = _align(pos, 4)
                    count = unpack_uint(data, pos)[0]
                    pos += 4

                    if rsz_type == StructData:
                        struct_values = []
                        current_pos = pos
                        if count > 0 and type_registry:
                            struct_type_info, _ = type_registry.find_type_by_name(original_type)
                            if struct_type_info:
                                struct_fields_def = struct_type_info.get("fields", [])
                                current_pos = _align(current_pos, field_align)
                                temp_parsed = parsed_elements
                                for _ in range(count):
                                    struct_element = {}
                                    self.parsed_elements[current_instance_index] = struct_element
                                    next_pos = self.parse_instance_fields(
                                        offset=current_pos,
                                        fields_def=struct_fields_def,
                                        current_instance_index=current_instance_index,
                                    )
                                    if next_pos > current_pos and struct_element:
                                        struct_values.append(struct_element)
                                        current_pos = next_pos
                                    else:
                                        break
                                self.parsed_elements[current_instance_index] = temp_parsed
                                pos = current_pos
                        data_obj = StructData(struct_values, original_type)

                    elif rsz_type == MaybeObject:
                        child_indexes = []
                        raw_values = []
                        already_ref = False
                        if count > 0:
                            configure_pos = non_array_parser.configure_pos_only
                            configure(pos, fsize, field_align, original_type)
                            candidate, raw_value = read_value_with_raw(parser_unpack_uint, fsize)
                            pos = non_array_parser.pos
                            if check_valid_ref(candidate):
                                already_ref = True
                                child_indexes.append(candidate)
                                append_child(candidate)
                                set_parent(candidate, current_instance_index)
                            else:
                                raw_values.append(raw_value)
                            for i in range(1, count):
                                configure_pos(pos, field_align)
                                candidate, raw_value = read_value_with_raw(parser_unpack_uint, fsize)
                                pos = non_array_parser.pos
                                if already_ref:
                                    child_indexes.append(candidate)
                                    append_child(candidate)
                                    set_parent(candidate, current_instance_index)
                                else:
                                    raw_values.append(raw_value)
                        if already_ref:
                            data_obj = ArrayData(
                                [ObjectData(idx, original_type) for idx in child_indexes],
                                ObjectData,
                                original_type,
                            )
                        else:
                            data_obj = ArrayData(
                                [RawBytesData(raw, fsize, original_type) for raw in raw_values],
                                RawBytesData,
                                original_type,
                            )

                    else:
                        if count == 0:
                            data_obj = ArrayData([], default_element_class, original_type)
                        elif count == 1:
                            configure(pos, fsize, field_align, original_type)
                            value = parser_func(non_array_parser)
                            pos = non_array_parser.pos
                            data_obj = ArrayData([value], type(value), original_type)
                        else:
                            values = []
                            values_append = values.append
                            configure_pos = non_array_parser.configure_pos_only
                            configure(pos, fsize, field_align, original_type)
                            values_append(parser_func(non_array_parser))
                            pos = non_array_parser.pos
                            for _ in range(count - 1):
                                configure_pos(pos, field_align)
                                values_append(parser_func(non_array_parser))
                                pos = non_array_parser.pos
                            
                            element_class = type(values[0])
                            data_obj = ArrayData(values, element_class, original_type)

                else:  # Non-array field
                    configure(pos, fsize, field_align, original_type)
                    data_obj = parser_func(non_array_parser)
                    pos = non_array_parser.pos

                set_parsed(field_name, data_obj)
        finally:
            parser_pool.append(non_array_parser)

        return pos