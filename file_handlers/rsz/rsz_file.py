import struct
import uuid
from file_handlers.rcol_file import align_offset
from file_handlers.rsz.rsz_data_types import *
from file_handlers.rsz.pfb_16.pfb_structure import Pfb16Header, build_pfb_16, parse_pfb16_rsz_userdata
from file_handlers.rsz.scn_19.scn_19_structure import Scn19Header, build_scn_19, parse_scn19_rsz_userdata
from utils.hex_util import read_wstring, guid_le_to_str


########################################
# SCN File Data Classes
########################################

class ScnHeader:
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

class ScnGameObject:
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

class ScnFolderInfo:
    SIZE = 8
    def __init__(self):
        self.id = 0
        self.parent_id = 0
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated folder info at 0x{offset:X}")
        self.id, self.parent_id = struct.unpack_from("<ii", data, offset)
        return offset + self.SIZE

class ScnResourceInfo:
    SIZE = 8
    def __init__(self):
        self.string_offset = 0
        self.reserved = 0
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated resource info at 0x{offset:X}")
        self.string_offset, self.reserved = struct.unpack_from("<II", data, offset)
        return offset + self.SIZE

class ScnPrefabInfo:
    SIZE = 8
    def __init__(self):
        self.string_offset = 0
        self.parent_id = 0
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated prefab info at 0x{offset:X}")
        self.string_offset, self.parent_id = struct.unpack_from("<II", data, offset)
        return offset + self.SIZE

class ScnUserDataInfo:
    SIZE = 16
    def __init__(self):
        self.hash = 0
        self.crc = 0
        self.string_offset = 0  # 8 bytes (uint64)
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated userdata info at 0x{offset:X}")
        self.hash, self.crc, self.string_offset = struct.unpack_from("<IIQ", data, offset)
        return offset + self.SIZE

# RSZUserDataInfos – each entry is 16 bytes.
class ScnRSZUserDataInfo:
    SIZE = 16
    def __init__(self):
        self.instance_id = 0   # 4 bytes: which instance this userdata is associated with
        self.hash = 0          # 4 bytes: hash
        self.string_offset = 0 # 8 bytes: offset for the userdata string (uint64)
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated RSZUserData info at 0x{offset:X}")
        self.instance_id, self.hash, self.string_offset = struct.unpack_from("<IIQ", data, offset)
        return offset + self.SIZE

class ScnRSZHeader:
    SIZE = 48
    def __init__(self):
        self.magic = 0
        self.version = 0
        self.object_count = 0
        self.instance_count = 0
        self.userdata_count = 0
        self.reserved = 0  # 4 bytes reserved after userdata_count
        self.instance_offset = 0
        self.data_offset = 0
        self.userdata_offset = 0
    def parse(self, data: bytes, offset: int) -> int:
        fmt = "<5I I Q Q Q"  # Changed to group the 5 uint32s together, followed by reserved uint32
        (self.magic,
         self.version,
         self.object_count,
         self.instance_count,
         self.userdata_count,
         self.reserved,      # Parse reserved as uint32
         self.instance_offset,
         self.data_offset,
         self.userdata_offset) = struct.unpack_from(fmt, data, offset)
        return offset + self.SIZE

class ScnInstanceInfo:
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
# Main SCN File Parser
########################################

class ScnFile:
    def __init__(self):
        self.full_data = b""
        self.header = None
        self.is_usr = False
        self.is_pfb = False
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
        self.data = b""
        self.type_registry = None
        self.parsed_instances = []
        self._current_offset = 0 
        self.game_version = "RE4"
        self.filepath = ""
  
        self._string_cache = {}  # Cache for frequently accessed strings, will probably be removed.
        self._type_info_cache = {}
        
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

    def read(self, data: bytes):
        # Use memoryview for efficient slicing operations
        self.full_data = memoryview(data)
        self._current_offset = 0
        
        if data[:4] == b'USR\x00':
            self.is_usr = True
            self.is_pfb = False
            self.header = UsrHeader()
        elif data[:4] == b'PFB\x00':
            self.is_usr = False
            self.is_pfb = True
            
            if self.filepath.lower().endswith('.16'):
                self.header = Pfb16Header()
            else:
                self.header = PfbHeader()
        else:
            self.is_usr = False
            self.is_pfb = False
            
            if self.filepath.lower().endswith('.19'):
                self.header = Scn19Header()
            else:
                self.header = ScnHeader()
            
        self.header.parse(data)
        self._current_offset = self.header.SIZE

        # Call appropriate parsing function based on file type
        if self.is_usr:
            self._parse_usr_file(data)
        elif self.is_pfb:
            self._parse_pfb_file(data)
        else:
            self._parse_scn_file(data)

    def _parse_usr_file(self, data: bytes):
        """Parse USR file structure"""
        self._parse_resource_infos(data)
        self._parse_userdata_infos(data)
        self._parse_blocks(data)
        self._parse_rsz_section(data)
        self._parse_instances(data)
        
    def _parse_pfb_file(self, data: bytes):
        """Parse PFB file structure with game version considerations"""
        self._parse_gameobjects(data)
        self._parse_gameobject_ref_infos(data)
        self._parse_resource_infos(data)
        
        if not self.filepath.lower().endswith('.16'):
            self._parse_userdata_infos(data)
            
        self._parse_blocks(data)
        self._parse_rsz_section(data)
        self._parse_instances(data)
        
    def _parse_scn_file(self, data: bytes):
        """Parse standard SCN file structure"""
        self._parse_gameobjects(data)
        self._parse_folder_infos(data)
        self._parse_resource_infos(data)
        self._parse_prefab_infos(data)
        
        # SCN.19 format doesn't have userdata_infos
        if not self.filepath.lower().endswith('.19'):
            self._parse_userdata_infos(data)
            
        self._parse_blocks(data)
        self._parse_rsz_section(data)
        self._parse_instances(data)        

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
            for i in range(self.header.info_count):
                go = PfbGameObject()
                self._current_offset = go.parse(data, self._current_offset)
                self.gameobjects.append(go)
        else:
            # Regular SCN files use the 32-byte GameObject structure
            is_scn19 = self.filepath.lower().endswith('.19')
            for i in range(self.header.info_count):
                go = ScnGameObject()
                self._current_offset = go.parse(data, self._current_offset, is_scn19)
                self.gameobjects.append(go)

    def _parse_gameobject_ref_infos(self, data):
        for i in range(self.header.gameobject_ref_info_count):
            gori = GameObjectRefInfo()
            self._current_offset = gori.parse(data, self._current_offset)
            self.gameobject_ref_infos.append(gori)
        self._current_offset = self._align(self._current_offset, 16)

    def _parse_folder_infos(self, data):
        for i in range(self.header.folder_count):
            fi = ScnFolderInfo()
            self._current_offset = fi.parse(data, self._current_offset)
            self.folder_infos.append(fi)
        self._current_offset = self._align(self._current_offset, 16)

    def _parse_resource_infos(self, data):
        for i in range(self.header.resource_count):
            ri = ScnResourceInfo()
            self._current_offset = ri.parse(data, self._current_offset)
            self.resource_infos.append(ri)
        self._current_offset = self._align(self._current_offset, 16)

    def _parse_prefab_infos(self, data):
        for i in range(self.header.prefab_count):
            pi = ScnPrefabInfo()
            self._current_offset = pi.parse(data, self._current_offset)
            self.prefab_infos.append(pi)
        self._current_offset = self._align(self._current_offset, 16)

    def _parse_userdata_infos(self, data):
        for i in range(self.header.userdata_count):
            ui = ScnUserDataInfo()
            self._current_offset = ui.parse(data, self._current_offset)
            self.userdata_infos.append(ui)
        self._current_offset = self._align(self._current_offset, 16)

    def _parse_blocks(self, data):
        view = memoryview(data)
        null_pattern = b'\x00\x00\x00'
        
        # Resource block
        start = self._current_offset
        end = view.obj.find(null_pattern, start)
        if end != -1:
            self.resource_block = bytes(view[start:end])
            self._current_offset = end + 3

        # Prefab block
        start = self._current_offset
        end = view.obj.find(null_pattern, start)
        if end != -1:
            self.prefab_block = bytes(view[start:end])
            self._current_offset = end + 3

        self._resource_str_map.clear()
        self._prefab_str_map.clear()
        self._userdata_str_map.clear()
        self._rsz_userdata_str_map.clear()
        
        # Batch process all strings instead of one-by-one
        for ri in self.resource_infos:
            if (ri.string_offset != 0):
                s, _ = read_wstring(self.full_data, ri.string_offset, 1000)
                self.set_resource_string(ri, s)
                
        for pi in self.prefab_infos:
            if (pi.string_offset != 0):
                s, _ = read_wstring(self.full_data, pi.string_offset, 1000)
                self.set_prefab_string(pi, s)
                
        for ui in self.userdata_infos:
            if (ui.string_offset != 0):
                s, _ = read_wstring(self.full_data, ui.string_offset, 1000)
                self.set_userdata_string(ui, s)

    def _parse_rsz_section(self, data):
        self._current_offset = self.header.data_offset

        self.rsz_header = ScnRSZHeader()
        self._current_offset = self.rsz_header.parse(data, self._current_offset)

        # Parse Object Table first
        fmt = f"<{self.rsz_header.object_count}i"
        self.object_table = list(struct.unpack_from(fmt, data, self._current_offset))
        self._current_offset += self.rsz_header.object_count * 4

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
        for i in range(self.rsz_header.instance_count):
            ii = ScnInstanceInfo()
            self._current_offset = ii.parse(data, self._current_offset)
            self.instance_infos.append(ii)
            
        self._current_offset = self.header.data_offset + self.rsz_header.userdata_offset

        # Special handling for SCN.19 format which has different RSZ userdata structure
        if self.filepath.lower().endswith('.19'):
            self._parse_scn19_rsz_userdata(data)
        # Special handling for PFB.16 format which might have embedded RSZ
        elif self.filepath.lower().endswith('.16'):
            self._current_offset = parse_pfb16_rsz_userdata(self, data)
        else:
            # Standard RSZ userdata parsing
            self._parse_standard_rsz_userdata(data)
        
        self.data = data[self._current_offset:]
        
        self._rsz_userdata_dict = {rui.instance_id: rui for rui in self.rsz_userdata_infos}
        self._rsz_userdata_set = set(self._rsz_userdata_dict.keys())

    def _parse_standard_rsz_userdata(self, data):
        """Parse standard RSZ userdata entries (16 bytes each)"""
        self.rsz_userdata_infos = []
        for i in range(self.rsz_header.userdata_count):
            rui = ScnRSZUserDataInfo()
            self._current_offset = rui.parse(data, self._current_offset)
            if rui.string_offset != 0:
                abs_offset = self.header.data_offset + rui.string_offset
                s, _ = read_wstring(self.full_data, abs_offset, 1000)
                self.set_rsz_userdata_string(rui, s)
            self.rsz_userdata_infos.append(rui)
            
        last_str_offset = self.rsz_userdata_infos[-1].string_offset if self.rsz_userdata_infos else 0
        if last_str_offset:
            abs_offset = self.header.data_offset + last_str_offset
            s, new_offset = read_wstring(self.full_data, abs_offset, 1000)
        else:
            new_offset = self._current_offset
        self._current_offset = self._align(new_offset, 16)

    def _parse_scn19_rsz_userdata(self, data):
        """Parse SCN.19 RSZ userdata entries (24 bytes each with embedded binary data)"""
        self._current_offset = parse_scn19_rsz_userdata(self, data)

    def _align(self, offset: int, alignment: int) -> int:
        remainder = offset % alignment
        if remainder:
            return offset + (alignment - remainder)
        return offset

    def get_rsz_userdata_string(self, rui):
        return self._rsz_userdata_str_map.get(rui, "")

    def _parse_instances(self, data):
        """Parse instance data with optimizations"""
        self.parsed_instances = []
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
        
        # Process all instances
        for idx, inst in enumerate(self.instance_infos):
            if idx == 0:
                self.parsed_elements[idx] = {}  # Initialize empty dict for NULL entry
                continue

            if idx in self._rsz_userdata_set or idx in self._processed_instances:
                self.parsed_elements[idx] = {}
                continue
                
            # Get type info from registry - cache type info per instance type
            if inst.type_id not in self._type_info_cache:
                self._type_info_cache[inst.type_id] = type_registry.get_type_info(inst.type_id) if type_registry else {}
            
            type_info = self._type_info_cache[inst.type_id]
            fields_def = type_info.get("fields", [])
            
            if not fields_def:
                self.parsed_elements[idx] = {}
                continue

            self.parsed_elements[idx] = {}
            new_offset = parse_instance_fields(
                raw=self.data,
                offset=current_offset,
                fields_def=fields_def,
                current_instance_index=idx,
                scn_file=self
            )
            current_offset = new_offset

    def _write_field_value(self, field_def: dict, data_obj, out: bytearray):
        field_size = field_def.get("size", 4)
        field_align = field_def.get("align", 1)
        field_type = field_def.get("type", "").lower()
        
        if isinstance(data_obj, StructData):
            if data_obj.values:
                for struct_value in data_obj.values:
                    for field_name, field_value in struct_value.items():
                        original_type = data_obj.orig_type
                        if original_type and self.type_registry:
                            struct_type_info, _ = self.type_registry.find_type_by_name(original_type)
                            if struct_type_info:
                                struct_fields = struct_type_info.get("fields", [])
                                for struct_field in struct_fields:
                                    if struct_field.get("name") == field_name:
                                        self._write_field_value(struct_field, field_value, out)
                                        break
        elif field_def.get("array", False):

            while len(out) % 4:
                out.extend(b'\x00') 

            if isinstance(data_obj, ArrayData):
                count = len(data_obj.values)
                out.extend(struct.pack("<I", count))
                
                for element in data_obj.values:
                    while len(out) % field_align:
                        out.extend(b'\x00')
                    
                    if isinstance(element, S8Data):
                        out.extend(struct.pack("<b", max(-128, min(127, element.value))))
                    elif isinstance(element, U8Data):
                        out.extend(struct.pack("<B", element.value & 0xFF))
                    elif isinstance(element, BoolData):
                        out.extend(struct.pack("<?", element.value))
                    elif isinstance(element, S16Data):
                        out.extend(struct.pack("<h", element.value & 0xFFFF))
                    elif isinstance(element, U16Data):
                        out.extend(struct.pack("<H", element.value & 0xFFFF))
                    elif isinstance(element, S64Data):
                        out.extend(struct.pack("<q", element.value))
                    elif isinstance(element, S32Data):
                        out.extend(struct.pack("<i", element.value))
                    elif isinstance(element, U64Data):
                        out.extend(struct.pack("<Q", element.value))
                    elif isinstance(element, F64Data):
                        out.extend(struct.pack("<d", element.value))
                    elif isinstance(element, Vec2Data):
                        out.extend(struct.pack("<4f", element.x, element.y, 0, 0))
                    elif isinstance(element, Float2Data):
                        out.extend(struct.pack("<2f", element.x, element.y))
                    elif isinstance(element, RangeData):
                        out.extend(struct.pack("<2f", element.min, element.max))
                    elif isinstance(element, RangeIData):
                        out.extend(struct.pack("<2i", element.min, element.max))
                    elif isinstance(element, Float3Data):
                        out.extend(struct.pack("<3f", element.x, element.y, element.z))
                    elif isinstance(element, Float4Data):
                        out.extend(struct.pack("<4f", element.x, element.y, element.z, element.w))
                    elif isinstance(element, QuaternionData):
                        out.extend(struct.pack("<4f", element.x, element.y, element.z, element.w))
                    elif isinstance(element, ColorData):
                        out.extend(struct.pack("<4B", element.r, element.g, element.b, element.a))
                    elif isinstance(element, (ObjectData, U32Data)):
                        value = int(element.value) & 0xFFFFFFFF
                        out.extend(struct.pack("<I", value))
                    elif isinstance(element, Vec3Data):
                        out.extend(struct.pack("<4f", element.x, element.y, element.z, 0.00))
                    elif isinstance(element, Vec4Data):
                        out.extend(struct.pack("<4f", element.x, element.y, element.z, element.w))
                    elif isinstance(element, Mat4Data):
                        if isinstance(element.values, (list, tuple)):
                            float_values = [float(v) for v in element.values[:16]]
                            while len(float_values) < 16:
                                float_values.append(0.0)
                            out.extend(struct.pack("<16f", *float_values))
                        else:
                            print("Mat4Data error while writing")
                            out.extend(struct.pack("<16f", *([0.0] * 16)))
                    elif isinstance(element, (GameObjectRefData, GuidData)):
                        guid = uuid.UUID(element.guid_str)
                        out.extend(guid.bytes_le)
                    elif isinstance(element, (StringData, ResourceData)):
                        while len(out) % 4:
                            out.extend(b'\x00')
                        if element.value:
                            str_bytes = element.value.encode('utf-16-le')
                            char_count = len(element.value)
                            if element.value[-1] == '\x00':
                                char_count = len(element.value) 
                            else:
                                char_count = len(element.value) + 1  
                                str_bytes += b'\x00\x00' 
                            out.extend(struct.pack("<I", char_count))
                            out.extend(str_bytes)
                        else:
                            out.extend(struct.pack("<I", 0))
                    elif isinstance(element, OBBData):
                        if isinstance(element.values, (list, tuple)):
                            float_values = [float(v) for v in element.values[:20]]
                            while len(float_values) < 20:
                                float_values.append(0.0)
                            out.extend(struct.pack("<20f", *float_values))
                        else:
                            # Fallback - write zeros
                            out.extend(struct.pack("<20f", *([0.0] * 20)))
                    elif isinstance(element, RawBytesData):
                        out.extend(element.raw_bytes)
                    elif isinstance(element, UserDataData):
                        out.extend(struct.pack("<I", element.index))
                        #print("userdata index is ", element.index)
                    elif isinstance(element, F32Data):
                        val_bits = struct.pack("<f", element.value)
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
                    else:
                        val = getattr(element, 'value', 0)
                        raw_bytes = val.to_bytes(field_size, byteorder='little')
                        out.extend(raw_bytes)
        else:
            while len(out) % field_align:
                out.extend(b'\x00') 

            #print("last is noot array")
            if isinstance(data_obj, S8Data):
                out.extend(struct.pack("<b", max(-128, min(127, data_obj.value))))
            elif isinstance(data_obj, U8Data):
                #print("last is u8")
                out.extend(struct.pack("<B", data_obj.value & 0xFF))
            elif isinstance(data_obj, S16Data):
                #print("last is s16")
                out.extend(struct.pack("<h", data_obj.value & 0xFFFF))
            elif isinstance(data_obj, U16Data):
                #print("last is u16")
                out.extend(struct.pack("<H", data_obj.value & 0xFFFF))
            elif isinstance(data_obj, S64Data):
                #print("last is s64")
                out.extend(struct.pack("<q", data_obj.value))
            elif isinstance(data_obj, S32Data):
                #print("last is s64")
                out.extend(struct.pack("<i", data_obj.value))
            elif isinstance(data_obj, U64Data):
                #print("last is u64")
                out.extend(struct.pack("<Q", data_obj.value))
            elif isinstance(data_obj, F64Data):
                #print("last is f64")
                out.extend(struct.pack("<d", data_obj.value))
            elif isinstance(data_obj, Vec2Data):
                #print("last is vec2")
                out.extend(struct.pack("<4f", data_obj.x, data_obj.y, 0, 0))
            elif isinstance(data_obj, Float2Data):
                #print("last is float2")
                out.extend(struct.pack("<2f", data_obj.x, data_obj.y))
            elif isinstance(data_obj, RangeData):
                #print("last is range")
                out.extend(struct.pack("<2f", data_obj.min, data_obj.max))
            elif isinstance(data_obj, RangeIData):
                #print("last is rangeI")
                out.extend(struct.pack("<2i", data_obj.min, data_obj.max))
            elif isinstance(data_obj, Float3Data):
                #print("last is float3")
                out.extend(struct.pack("<3f", data_obj.x, data_obj.y, data_obj.z))
            elif isinstance(data_obj, Float4Data):
                out.extend(struct.pack("<4f", data_obj.x, data_obj.y, data_obj.z, data_obj.w))
            elif isinstance(data_obj, QuaternionData):
                out.extend(struct.pack("<4f", data_obj.x, data_obj.y, data_obj.z, data_obj.w))
            elif isinstance(data_obj, ColorData):
                out.extend(struct.pack("<4B", data_obj.r, data_obj.g, data_obj.b, data_obj.a))
            elif isinstance(data_obj, (ObjectData, U32Data)):
                value = int(data_obj.value) & 0xFFFFFFFF
                out.extend(struct.pack("<I", value))
            elif isinstance(data_obj, Vec3Data):
                out.extend(struct.pack("<4f", data_obj.x, data_obj.y, data_obj.z, 0.00))
            elif isinstance(data_obj, Vec4Data):
                out.extend(struct.pack("<4f", data_obj.x, data_obj.y, data_obj.z, data_obj.w))
            elif isinstance(data_obj, (GameObjectRefData, GuidData)):
                # Use UUID directly to avoid string cleaning issues
                guid = uuid.UUID(data_obj.guid_str)
                out.extend(guid.bytes_le)
            elif isinstance(data_obj, (StringData, ResourceData)):
                while len(out) % 4:
                    out.extend(b'\x00')
                    
                if data_obj.value:
                    str_bytes = data_obj.value.encode('utf-16-le')
                    char_count = len(data_obj.value)
                    if data_obj.value[-1] == '\x00':
                        char_count = len(data_obj.value)  
                    else:
                        char_count = len(data_obj.value) + 1  
                        str_bytes += b'\x00\x00' 
                    out.extend(struct.pack("<I", char_count))
                    out.extend(str_bytes)
                else:
                    out.extend(struct.pack("<I", 0))
            elif isinstance(data_obj, BoolData):
                #print("last is bool")
                out.extend(struct.pack("<?", bool(data_obj.value)))
            elif isinstance(data_obj, F32Data):
                #print("last is f32")    
                val_bits = struct.pack("<f", data_obj.value)
                out.extend(val_bits)
            elif isinstance(data_obj, OBBData):
                if isinstance(data_obj.values, (list, tuple)):
                    out.extend(struct.pack("<20f", *[float(v) for v in data_obj.values]))
                else:
                    values = [float(x) for x in str(data_obj.values).strip('()').split(',')]
                    out.extend(struct.pack("<20f", *values))
            elif isinstance(data_obj, Mat4Data):
                if isinstance(data_obj.values, (list, tuple)):
                    out.extend(struct.pack("<16f", *[float(v) for v in data_obj.values]))
                else:
                    values = [float(x) for x in str(data_obj.values).strip('()').split(',')]
                    out.extend(struct.pack("<16f", *values))
            elif isinstance(data_obj, RawBytesData):
                #print("last is rawbytes")
                out.extend(data_obj.raw_bytes)
            elif isinstance(data_obj, UserDataData):
                #print(f"Writing userdata with index {data_obj.index}")  # Debug print
                out.extend(struct.pack("<I", data_obj.index))
            elif isinstance(data_obj, CapsuleData):
                out.extend(struct.pack("<3f", data_obj.start.x, data_obj.start.y, data_obj.start.z))
                out.extend(struct.pack("<f", 0.0)) 
                out.extend(struct.pack("<3f", data_obj.end.x, data_obj.end.y, data_obj.end.z))
                out.extend(struct.pack("<f", 0.0))
                out.extend(struct.pack("<f", data_obj.radius))
                out.extend(b'\x00' * 12)
            else:
                #print("last is else")
                val = getattr(data_obj, 'value', 0)
                if isinstance(val, float):
                    val_bits = struct.pack("<f", val)  
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

    def build(self, special_align_enabled = False) -> bytes:
        if self.is_usr:
            return self._build_usr(special_align_enabled)
        elif self.is_pfb:
            if self.filepath.lower().endswith('.16'):
                return build_pfb_16(self, special_align_enabled)
            else:
                return self._build_pfb(special_align_enabled)
        else:
            if self.filepath.lower().endswith('.19'):
                return build_scn_19(self, special_align_enabled)
        
        self.header.info_count = len(self.gameobjects)
        self.header.folder_count = len(self.folder_infos)
        self.header.resource_count = len(self.resource_infos)
        self.header.prefab_count = len(self.prefab_infos)
        self.header.userdata_count = len(self.userdata_infos)
        
        if self.rsz_header:
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
            if self.filepath.lower().endswith('.19'):
                out += struct.pack("<h", go.prefab_id)
                out += struct.pack("<i", go.ukn) 
            else:
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
        current_offset = self._align(current_offset, 16)
        
        # Skip prefab infos table
        current_offset += len(self.prefab_infos) * 8
        
        # Align to 16 after prefab infos
        current_offset = self._align(current_offset, 16)
        
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
            out += struct.pack("<IIQ", ui.hash, ui.crc, ui.string_offset)

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
            data_offset = self._align(data_offset, 16)
            
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
            object_table_size = self.rsz_header.object_count * 4
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
            userdata_entries = []
            for rui in self.rsz_userdata_infos:
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
        resource_info_tbl = self._align(len(out), 16)
        resource_info_size = len(self.resource_infos) * 8  # Each resource info is 8 bytes
        
        userdata_info_tbl = self._align(resource_info_tbl + resource_info_size, 16)
        userdata_info_size = len(self.userdata_infos) * 16  # Each userdata info is 16 bytes
        
        # Calculate string positions
        string_start = self._align(userdata_info_tbl + userdata_info_size, 16)
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
            out += struct.pack("<IIQ", ui.hash, ui.crc, ui.string_offset)

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
        resource_info_tbl = self._align(len(out), 16)
        resource_info_size = len(self.resource_infos) * 8  # Each resource info is 8 bytes
        
        userdata_info_tbl = self._align(resource_info_tbl + resource_info_size, 16)
        userdata_info_size = len(self.userdata_infos) * 16  # Each userdata info is 16 bytes
        
        # Calculate string positions
        string_start = self._align(userdata_info_tbl + userdata_info_size, 16)
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
            out += struct.pack("<IIQ", ui.hash, ui.crc, ui.string_offset)

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
        rsz_start = self._build_rsz_section(out, special_align_enabled)

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

    def _build_rsz_section(self, out: bytearray, special_align_enabled = False) -> int:
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
        
        return rsz_start

    def get_resource_string(self, ri):
        return self._resource_str_map.get(ri, "")
    
    def get_prefab_string(self, pi):
        return self._prefab_str_map.get(pi, "")
    
    def get_userdata_string(self, ui):
        return self._userdata_str_map.get(ui, "")
    
    def get_rsz_userdata_string(self, rui):
        return self._rsz_userdata_str_map.get(rui, "")

    def set_resource_string(self, ri, new_string: str):
        self._resource_str_map[ri] = new_string  
    
    def set_prefab_string(self, pi, new_string: str):
        self._prefab_str_map[pi] = new_string
    
    def set_userdata_string(self, ui, new_string: str):
        self._userdata_str_map[ui] = new_string
    
    def set_rsz_userdata_string(self, rui, new_string: str):
        self._rsz_userdata_str_map[rui] = new_string

def get_type_name(type_registry, instance_infos, idx):
    if not type_registry or not instance_infos or idx >= len(instance_infos):
        return f"Instance[{idx}]"
    inst_info = instance_infos[idx]
    type_info = type_registry.get_type_info(inst_info.type_id)
    if (type_info and "name" in type_info):
        return f"{type_info['name']} (ID: {idx})"
    return f"Instance[{idx}]"

def is_valid_reference(candidate, current_instance_index, scn_file=None):
    return (0 < candidate < current_instance_index and 
            candidate not in scn_file._gameobject_instance_ids and 
            candidate not in scn_file._folder_instance_ids)

def ensure_enough_data(dataLen, offset, size):
    if offset + size > dataLen:
        raise ValueError(f"Insufficient data at offset {offset:#x}")

def parse_instance_fields(
    raw: bytes,
    offset: int, 
    fields_def: list,
    current_instance_index=None,
    scn_file=None
):
    """Parse fields from raw data according to field definitions - optimized version"""
    pos = offset
    local_align = align_offset
    rsz_userdataInfos = scn_file.rsz_userdata_infos
    
    unpack_uint = struct.Struct("<I").unpack_from
    unpack_float = struct.Struct("<f").unpack_from
    unpack_4float = struct.Struct("<4f").unpack_from
    unpack_2float = struct.Struct("<2f").unpack_from
    unpack_2int = struct.Struct("<2i").unpack_from
    unpack_int = struct.Struct("<i").unpack_from
    unpack_sbyte = struct.Struct("<b").unpack_from
    unpack_ubyte = struct.Struct("<B").unpack_from
    unpack_4ubyte = struct.Struct("<4B").unpack_from
    unpack_short = struct.Struct("<h").unpack_from
    unpack_ushort = struct.Struct("<H").unpack_from
    unpack_long = struct.Struct("<q").unpack_from
    unpack_ulong = struct.Struct("<Q").unpack_from
    unpack_double = struct.Struct("<d").unpack_from
    unpack_20float = struct.Struct("<20f").unpack_from
    unpack_16float = struct.Struct("<16f").unpack_from

    raw_len = len(raw)
    parsed_elements = scn_file.parsed_elements.setdefault(current_instance_index, {})
    instance_hierarchy = scn_file.instance_hierarchy
    gameobject_ids = scn_file._gameobject_instance_ids
    folder_ids = scn_file._folder_instance_ids
    rsz_userdata_map = scn_file._rsz_userdata_str_map
    
    # a direct reference to dictionary element for faster access
    current_hierarchy = instance_hierarchy[current_instance_index]
    current_children = current_hierarchy["children"]
    
    def is_valid_ref(candidate):
        return (0 < candidate < current_instance_index and 
                candidate not in gameobject_ids and 
                candidate not in folder_ids)

    def get_bytes(segment):
        return segment.tobytes() if hasattr(segment, "tobytes") else segment

    def set_parent_safely(idx, parent_idx):
        """Helper to safely set parent relationship"""
        if idx >= 0 and idx < len(instance_hierarchy):
            instance_hierarchy[idx]["parent"] = parent_idx
        else:
            print(f"Warning: Invalid instance index {idx} encountered (parent: {parent_idx})")

    for field in fields_def:
        field_name = field.get("name", "<unnamed>")
        ftype = field.get("type", "unknown").lower()
        fsize = field.get("size", 4)
        is_native = field.get("native", False)
        is_array = field.get("array", False)
        original_type = field.get("original_type", "") 
        field_align = int(field["align"]) if "align" in field else 1
        rsz_type = get_type_class(ftype, fsize, is_native, is_array, field_align, original_type)
        data_obj = None
        
        # Special handling for Struct types (both array and non-array)
        if rsz_type == StructData:
            struct_type_info = None
            struct_type_id = None
            if original_type and scn_file.type_registry:
                struct_type_info, struct_type_id = scn_file.type_registry.find_type_by_name(original_type)

            struct_values = []
            
            # If we have a valid type definition, try to parse it
            if struct_type_info and struct_type_id and pos < len(raw):
                struct_fields_def = struct_type_info.get("fields", [])
                
                # Create processed instances tracking set if it doesn't exist
                if not hasattr(scn_file, '_processed_instances'):
                    scn_file._processed_instances = set()
                
                # Start checking from the next instance
                next_instance_idx = current_instance_index + 1
                current_pos = pos
                
                # Look for consecutive instances of the right type
                while (next_instance_idx < len(scn_file.instance_infos) and 
                       next_instance_idx not in scn_file._processed_instances):
                    # Check if this instance matches our struct type
                    if scn_file.instance_infos[next_instance_idx].type_id != struct_type_id:
                        break
                    
                    # Mark this instance as being processed
                    scn_file._processed_instances.add(next_instance_idx)
                    
                    struct_parsed = {}
                    try:
                        # Parse the fields of the struct
                        next_pos = parse_instance_fields(
                            raw=raw,
                            offset=current_pos,
                            fields_def=struct_fields_def,
                            current_instance_index=next_instance_idx,
                            scn_file=scn_file
                        )
                        
                        # If we successfully parsed something, add it
                        if next_pos > current_pos:
                            # Copy the parsed data for this struct field
                            struct_parsed = scn_file.parsed_elements.get(next_instance_idx, {})
                            
                            # Add to our struct values if we got something
                            if struct_parsed:
                                struct_values.append(struct_parsed)
                            
                            current_pos = next_pos
                    except Exception as e: # TODO: Temporary until I'm 100% sure we're parsing Structs structures correctly
                        print(f"Error parsing struct type {original_type}: {e}")
                        break
                    
                    next_instance_idx += 1
                
                pos = current_pos
            
            data_obj = StructData(struct_values, original_type)
            
        elif is_array:
            pos = local_align(pos, 4)
            count = unpack_uint(raw, pos)[0]
            pos += 4
            
            if rsz_type == MaybeObject:          
                children = []
                child_indexes = []
                all_values = []
                alreadyRef = False
                
                for i in range(count):
                    pos = local_align(pos, field_align)
                        
                    value = unpack_uint(raw, pos)[0]
                    if is_valid_ref(value) and i == 0:
                        alreadyRef = True 
                    if alreadyRef:
                        child_indexes.append(value)
                        current_children.append(value)
                        set_parent_safely(value, current_instance_index)
                        
                    all_values.append(value)
                    pos += fsize
                    
                data_obj = ArrayData(
                    list(map(lambda x: ObjectData(x, original_type), child_indexes)) if alreadyRef else 
                    list(map(lambda x: U32Data(x, original_type), all_values)),
                    ObjectData if alreadyRef else U32Data,
                    original_type
                )

            elif rsz_type == UserDataData:
                userdata_values = []
                userdatas = []
                
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    candidate = unpack_uint(raw, pos)[0]
                    userdatas.append(candidate)
                    pos += 4
                    
                    found = None
                    for rui in rsz_userdataInfos:
                        if rui.instance_id == candidate:
                            found = rui
                            userdata_values.append(rsz_userdata_map.get(rui, f"Empty Userdata {candidate}"))
                            break
                    
                    if not found:
                        userdata_values.append(f"Empty Userdata {candidate}")
                        
                data_obj = ArrayData(
                    list(map(lambda val, idx: UserDataData(val, idx, original_type), userdata_values, userdatas)) if userdatas else [],
                    UserDataData,
                    original_type
                )
                
            elif rsz_type == GameObjectRefData:
                guids = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    guid_bytes = get_bytes(raw[pos:pos+fsize])
                    guid_str = guid_le_to_str(guid_bytes)
                    guids.append(guid_str)
                    pos += fsize
                    
                data_obj = ArrayData(
                    list(map(GameObjectRefData, guids)), 
                    GameObjectRefData,
                    original_type
                )
            
            elif rsz_type == ObjectData:
                child_indexes = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    ensure_enough_data(raw_len, pos, fsize)
                    idx = unpack_uint(raw, pos)[0]
                    child_indexes.append(idx)
                    current_children.append(idx)
                    set_parent_safely(idx, current_instance_index)
                    pos += fsize
                    
                data_obj = ArrayData(
                    list(map(ObjectData, child_indexes)), 
                    ObjectData,
                    original_type
                )
            
            elif rsz_type == Vec3Data:
                vec3_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    vals = unpack_4float(raw, pos)
                    vec3_objects.append(Vec3Data(vals[0], vals[1], vals[2], original_type))
                    pos += fsize
                    
                pos = local_align(pos, field_align) if vec3_objects else pos
                data_obj = ArrayData(vec3_objects, Vec3Data, original_type)

            elif rsz_type == Vec4Data:
                vec4_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    vals = unpack_4float(raw, pos)
                    vec4_objects.append(Vec4Data(*vals, original_type))
                    pos += fsize
                    
                pos = local_align(pos, field_align) if vec4_objects else pos
                data_obj = ArrayData(vec4_objects, Vec4Data, original_type)

            elif rsz_type == Float4Data:
                vec4_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    vals = unpack_4float(raw, pos)
                    vec4_objects.append(Float4Data(*vals, original_type))
                    pos += fsize
                    
                pos = local_align(pos, field_align) if vec4_objects else pos
                data_obj = ArrayData(vec4_objects, Float4Data, original_type)

            elif rsz_type == Mat4Data:
                mat4_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    floats = unpack_16float(raw, pos)
                    mat4_objects.append(Mat4Data(list(floats), original_type))
                    pos += fsize
                    
                data_obj = ArrayData(mat4_objects, Mat4Data, original_type)

            elif rsz_type == QuaternionData:
                vec4_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    vals = unpack_4float(raw, pos)
                    vec4_objects.append(QuaternionData(*vals, original_type))
                    pos += fsize
                    
                pos = local_align(pos, field_align) if vec4_objects else pos
                data_obj = ArrayData(vec4_objects, QuaternionData, original_type)

            elif rsz_type == RangeData:
                range_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    vals = unpack_2float(raw, pos)
                    range_objects.append(RangeData(vals[0], vals[1], original_type))
                    pos += fsize
                    
                data_obj = ArrayData(range_objects, RangeData, original_type)

            elif rsz_type == RangeIData:
                range_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    vals = unpack_2int(raw, pos)
                    range_objects.append(RangeIData(vals[0], vals[1], original_type))
                    pos += fsize
                    
                data_obj = ArrayData(range_objects, RangeIData, original_type)

            elif rsz_type == OBBData:
                obb_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    floats = unpack_20float(raw, pos)
                    obb_objects.append(OBBData(list(floats), original_type))
                    pos += fsize
                    
                data_obj = ArrayData(obb_objects, OBBData, original_type)
            
            elif rsz_type == StringData or rsz_type == ResourceData:
                children = []
                for _ in range(count):
                    pos = local_align(pos, 4)
                    str_length = unpack_uint(raw, pos)[0] * 2
                    pos += 4
                    segment = raw[pos:pos+str_length]
                    
                    try:
                        value = segment.decode('utf-16-le')
                    except UnicodeDecodeError:
                        # Fallback to manual conversion
                        chars = []
                        for j in range(0, len(segment), 2):
                            code_point = segment[j] | (segment[j+1] << 8)
                            chars.append(chr(code_point))
                        value = ''.join(chars)
                        
                    children.append(value)
                    pos += str_length
                    
                data_obj = ArrayData(list(map(rsz_type, children)), rsz_type, original_type)

            elif rsz_type == S8Data:
                values = []
                for _ in range(count):
                    value = unpack_sbyte(raw, pos)[0]
                    values.append(S8Data(value, original_type))
                    pos += 1
                    
                data_obj = ArrayData(values, S8Data, original_type)

            elif rsz_type == U8Data:
                values = []
                for _ in range(count):
                    value = unpack_ubyte(raw, pos)[0]
                    values.append(U8Data(value, original_type))
                    pos += 1
                    
                data_obj = ArrayData(values, U8Data, original_type)

            elif rsz_type == BoolData:
                values = []
                for _ in range(count):
                    value = raw[pos] != 0
                    values.append(BoolData(value, original_type))
                    pos += 1
                    
                data_obj = ArrayData(values, BoolData, original_type)
                
            elif rsz_type == S32Data:
                values = []
                for _ in range(count):
                    value = unpack_int(raw, pos)[0]
                    values.append(S32Data(value, original_type))
                    pos += fsize
                    
                data_obj = ArrayData(values, S32Data, original_type)

            elif rsz_type == U32Data:
                values = []
                for _ in range(count):
                    value = unpack_uint(raw, pos)[0]
                    values.append(U32Data(value, original_type))
                    pos += fsize
                    
                data_obj = ArrayData(values, U32Data, original_type)

            elif rsz_type == S16Data:
                values = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    value = unpack_short(raw, pos)[0]
                    values.append(S16Data(value, original_type))
                    pos += 2
                    
                data_obj = ArrayData(values, S16Data, original_type)

            elif rsz_type == U16Data:
                values = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    value = unpack_ushort(raw, pos)[0]
                    values.append(U16Data(value, original_type))
                    pos += 2
                    
                data_obj = ArrayData(values, U16Data, original_type)

            elif rsz_type == S64Data:
                values = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    value = unpack_long(raw, pos)[0]
                    values.append(S64Data(value, original_type))
                    pos += 8
                    
                data_obj = ArrayData(values, S64Data, original_type)

            elif rsz_type == U64Data:
                values = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    value = unpack_ulong(raw, pos)[0]
                    values.append(U64Data(value, original_type))
                    pos += 8
                    
                data_obj = ArrayData(values, U64Data, original_type)

            elif rsz_type == F32Data:
                values = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    value = unpack_float(raw, pos)[0]
                    values.append(F32Data(value, original_type))
                    pos += 4
                    
                data_obj = ArrayData(values, F32Data, original_type)

            elif rsz_type == F64Data:
                values = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    value = unpack_double(raw, pos)[0]
                    values.append(F64Data(value, original_type))
                    pos += 8
                    
                data_obj = ArrayData(values, F64Data, original_type)

            elif rsz_type == GuidData:
                guids = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    guid_bytes = get_bytes(raw[pos:pos+fsize])
                    guid_str = guid_le_to_str(guid_bytes)
                    guids.append((guid_str, guid_bytes))
                    pos += fsize
                    
                data_obj = ArrayData([GuidData(g[0], g[1], original_type) for g in guids], GuidData, original_type)

            elif rsz_type == ColorData:
                color_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    vals = unpack_4ubyte(raw, pos)
                    color_objects.append(ColorData(vals[0], vals[1], vals[2], vals[3], original_type))
                    pos += fsize
                    
                pos = local_align(pos, field_align) if color_objects else pos
                data_obj = ArrayData(color_objects, ColorData, original_type)

            else:
                children = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    raw_bytes = get_bytes(raw[pos:pos+fsize])
                    children.append(RawBytesData(raw_bytes, fsize, original_type))
                    pos += fsize
                    
                data_obj = ArrayData(children, RawBytesData, original_type)

        else:  # Not an array
            pos = local_align(pos, field_align)
            if rsz_type == MaybeObject:
                candidate = unpack_uint(raw, pos)[0]
                pos += 4
                
                if not is_valid_ref(candidate):
                    data_obj = U32Data(candidate, original_type)
                else:
                    data_obj = ObjectData(candidate, original_type)
                    current_children.append(candidate)
                    set_parent_safely(candidate, current_instance_index)

            elif rsz_type == UserDataData:
                instance_id = unpack_uint(raw, pos)[0]
                pos += 4
                
                value = None
                for rui in rsz_userdataInfos:
                    if rui.instance_id == instance_id:
                        value = rsz_userdata_map.get(rui, "")
                        break
                
                data_obj = UserDataData(value if value else "", instance_id, original_type)
            
            elif rsz_type == GameObjectRefData:
                guid_bytes = get_bytes(raw[pos:pos+fsize])
                guid_str = guid_le_to_str(guid_bytes)
                pos += fsize
                data_obj = GameObjectRefData(guid_str, guid_bytes, original_type)

            elif rsz_type == ObjectData:
                child_idx = unpack_uint(raw, pos)[0]
                pos += fsize
                data_obj = ObjectData(child_idx, original_type)
                current_children.append(child_idx)
                set_parent_safely(child_idx, current_instance_index)

            elif rsz_type == Vec3Data:
                vals = unpack_4float(raw, pos)
                pos += fsize
                pos = local_align(pos, field_align)
                data_obj = Vec3Data(vals[0], vals[1], vals[2], original_type)
        
            elif rsz_type == Vec4Data:
                vals = unpack_4float(raw, pos)
                pos += fsize
                pos = local_align(pos, field_align)
                data_obj = Vec4Data(*vals, original_type)

            elif rsz_type == Float4Data:
                vals = unpack_4float(raw, pos)
                pos += fsize
                pos = local_align(pos, field_align)
                data_obj = Float4Data(*vals, original_type)
        
            elif rsz_type == Mat4Data:
                vals = unpack_16float(raw, pos)
                pos += fsize
                pos = local_align(pos, field_align)
                data_obj = Mat4Data(vals, original_type)

            elif rsz_type == QuaternionData:
                vals = unpack_4float(raw, pos)
                pos += fsize
                pos = local_align(pos, field_align)
                data_obj = QuaternionData(*vals, original_type)
        
            elif rsz_type == OBBData:
                vals = unpack_20float(raw, pos)
                pos += fsize
                pos = local_align(pos, field_align)
                data_obj = OBBData(vals, original_type)

            elif rsz_type == RangeData:
                vals = unpack_2float(raw, pos)
                pos += fsize
                data_obj = RangeData(vals[0], vals[1], original_type)

            elif rsz_type == RangeIData:
                vals = unpack_2int(raw, pos)
                pos += fsize
                data_obj = RangeIData(vals[0], vals[1], original_type)
            
            elif rsz_type == StringData or rsz_type == ResourceData:
                count = unpack_uint(raw, pos)[0]
                pos += 4
                str_byte_count = count * 2
                segment = raw[pos:pos+str_byte_count]
                
                try:
                    value = segment.decode('utf-16-le')
                except UnicodeDecodeError:
                    # Fallback to manual conversion
                    chars = []
                    for i in range(0, len(segment), 2):
                        code_point = segment[i] | (segment[i+1] << 8)
                        chars.append(chr(code_point))
                    value = ''.join(chars)
                    
                pos += str_byte_count
                data_obj = StringData(value, original_type)

            elif rsz_type == BoolData:
                value = raw[pos] != 0
                pos += fsize
                data_obj = BoolData(value, original_type)

            elif rsz_type == S8Data:
                value = unpack_sbyte(raw, pos)[0]
                pos += fsize
                data_obj = S8Data(value, original_type)

            elif rsz_type == U8Data:
                value = unpack_ubyte(raw, pos)[0]
                pos += fsize
                data_obj = U8Data(value, original_type)

            elif rsz_type == S32Data:
                value = unpack_int(raw, pos)[0]
                pos += fsize
                data_obj = S32Data(value, original_type)

            elif rsz_type == U32Data:
                value = unpack_uint(raw, pos)[0]
                pos += fsize
                data_obj = U32Data(value, original_type)

            elif rsz_type == U64Data:
                value = unpack_ulong(raw, pos)[0]
                pos += fsize
                data_obj = U64Data(value, original_type)

            elif rsz_type == F32Data:
                value = unpack_float(raw, pos)[0]
                pos += fsize
                data_obj = F32Data(value, original_type)
                
            elif rsz_type == GameObjectRefData or rsz_type == GuidData:
                guid_bytes = get_bytes(raw[pos:pos+fsize])
                guid_str = guid_le_to_str(guid_bytes)
                pos += fsize
                data_obj = rsz_type(guid_str, guid_bytes, original_type)

            elif rsz_type == ColorData:
                vals = unpack_4ubyte(raw, pos)
                pos += fsize
                pos = local_align(pos, field_align)
                data_obj = ColorData(vals[0], vals[1], vals[2], vals[3], original_type)

            elif rsz_type == CapsuleData:
                start_vals = unpack_4float(raw, pos)
                pos += 16
                end_vals = unpack_4float(raw, pos)
                pos += 16
                radius, *_ = struct.unpack_from("<f", raw, pos) 
                pos += 16
                start_vec = Vec3Data(start_vals[0], start_vals[1], start_vals[2], "Vec3")
                end_vec = Vec3Data(end_vals[0], end_vals[1], end_vals[2], "Vec3")
                data_obj = CapsuleData(start_vec, end_vec, radius, original_type)

            else:
                raw_bytes = get_bytes(raw[pos:pos+fsize])
                pos += fsize
                data_obj = RawBytesData(raw_bytes, fsize, original_type)

        parsed_elements[field_name] = data_obj

    return pos
