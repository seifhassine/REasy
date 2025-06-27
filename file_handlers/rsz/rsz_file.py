import struct
import uuid
from file_handlers.rsz.rsz_data_types import *
from file_handlers.rsz.pfb_16.pfb_structure import Pfb16Header, build_pfb_16, parse_pfb16_rsz_userdata
from file_handlers.rsz.scn_19.scn_19_structure import Scn19Header, build_scn_19, parse_scn19_rsz_userdata
from file_handlers.rsz.scn_18.scn_18_structure import Scn18Header, _parse_scn_18_resource_infos, build_scn_18
from utils.hex_util import read_wstring, guid_le_to_str, align as _align 



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
'''
class AiwaypHeader:
    SIZE = 48
    def __init__(self):
        self.signature = b""
        self.version = 0
        self.info_count = 0
        self.resource_count = 0
        self.userdata_count = 0
        self.resource_info_tbl = 0
        self.userdata_info_tbl = 0
        self.data_offset = 0
        self.rsz_offset = 0  # RSZ section offset at position 0x94
        self.gameobject_name = ""
        self.gameobject_hash = ""  # Changed from gameobject_type to gameobject_hash
        self.unknown_value1 = 0   
        self.unknown_value2 = 0   
        self.gameobject_guid = b""

    def parse(self, data: bytes):
        fmt = "<4sI3I3Q"
        (self.signature,
         self.version,
         self.info_count,
         self.resource_count,
         self.userdata_count,
         self.resource_info_tbl,
         self.userdata_info_tbl,
         self.data_offset) = struct.unpack_from(fmt, data, 0)
        
        # Read RSZ offset from position 0x94 (148)
        if len(data) >= 152:
            self.rsz_offset, = struct.unpack_from("<I", data, 148)
        
        # Read additional AIWAYP-specific GameObject data after the header
        current_pos = 4
        
        # Read first string (name)
        if current_pos + 4 <= len(data):
            print("here")
            name_len, = struct.unpack_from("<I", data, current_pos)
            current_pos += 4
            if name_len > 0 and current_pos + (name_len * 2) <= len(data):
                name_bytes = data[current_pos:current_pos+(name_len*2)]
                try:
                    # Remove null terminators that might be part of the string
                    self.gameobject_name = name_bytes.decode('utf-16-le').rstrip('\x00')
                    print(f"Parsed AIWAYP name: '{self.gameobject_name}'")
                except UnicodeDecodeError as e:
                    print(f"Error decoding AIWAYP name: {e}")
                    self.gameobject_name = f"AIWAYP_Object_{name_len}"
                current_pos += name_len * 2
        
        current_pos = _align(current_pos, 4)
        # Read second string (type)
        if current_pos + 4 <= len(data):
            type_len, = struct.unpack_from("<I", data, current_pos)
            current_pos += 4
            if type_len > 0 and current_pos + (type_len * 2) <= len(data):
                type_bytes = data[current_pos:current_pos+(type_len*2)]
                try:
                    # Remove null terminators that might be part of the string
                    self.gameobject_type = type_bytes.decode('utf-16-le').rstrip('\0')
                    print(f"Parsed AIWAYP type: '{self.gameobject_type}'")
                except UnicodeDecodeError as e:
                    print(f"Error decoding AIWAYP type: {e}")
                    self.gameobject_type = f"AIWAYP_Type_{type_len}"
                current_pos += type_len * 2
        
        # Set default values if strings are empty
        if not self.gameobject_name:
            self.gameobject_name = "AIWAYP_Object"
        if not self.gameobject_type:
            self.gameobject_type = "AIWAYP_Type"
            
        current_pos = _align(current_pos, 4)
        # Read two unknown values (previously interpreted as ID and component count)
        if current_pos + 4 <= len(data):
            self.unknown_value1 = struct.unpack_from("<I", data, current_pos)
            current_pos += 4
            
        # Read GameObject GUID
        if current_pos + 16 <= len(data):
            self.gameobject_guid = data[current_pos:current_pos+16]
'''
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

class RszGameObject:
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

class RszFolderInfo:
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
    SIZE = 8
    def __init__(self):
        self.string_offset = 0
        self.parent_id = 0
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated prefab info at 0x{offset:X}")
        self.string_offset, self.parent_id = struct.unpack_from("<II", data, offset)
        return offset + self.SIZE

class RszUserDataInfo:
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
class RszRSZUserDataInfo:
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

class RszRSZHeader:
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
        self.is_pfb16 = False
        self.is_aiwayp = False
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
        self.auto_resource_management = False
  
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
        
        self.is_usr = False
        self.is_pfb = False
        self.is_pfb16 = False
        #self.is_aiwayp = False
        
        if data[:4] == b'USR\x00':
            self.is_usr = True
            self.header = UsrHeader()
        elif data[:4] == b'PFB\x00':
            self.is_pfb = True
            
            if self.filepath.lower().endswith('.16'):
                self.header = Pfb16Header()
                self.is_pfb16 = True 
            else:
                self.header = PfbHeader()
        #elif data[:4] == b'AIMP':
        #    self.is_aiwayp = True
        #    self.header = AiwaypHeader()
        else:
            self.is_usr = False
            self.is_pfb = False
            
            if self.filepath.lower().endswith('.19'):
                self.header = Scn19Header()
            elif self.filepath.lower().endswith('.18'):
                self.header = Scn18Header()
            else:
                self.header = ScnHeader()
            
        self.header.parse(data)
        self._current_offset = self.header.SIZE

        # Call appropriate parsing function based on file type
        if self.is_usr:
            self._parse_usr_file(data)
        elif self.is_pfb:
            self._parse_pfb_file(data)
        #elif self.is_aiwayp:
        #    self._parse_aiwayp_file(data)
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
        
        if self.filepath.lower().endswith('.16'):
            from file_handlers.rsz.pfb_16.pfb_structure import parse_pfb16_resources
            self._current_offset = parse_pfb16_resources(self, data, self._current_offset)
        else:
            self._parse_resource_infos(data)
            self._parse_userdata_infos(data)
            
        self._parse_blocks(data)
        self._parse_rsz_section(data)
        self._parse_instances(data)
        
    '''def _parse_aiwayp_file(self, data: bytes):
        """Parse AIWAYP file structure"""
        # Skip directly to RSZ section using offset at 0x94
        if self.header.rsz_offset > 0:
            # Create a root GameObject that's NOT in the object table
            # This GameObject exists outside the object table but owns all the components
            go = RszGameObject()
            go.id = -1  # Special ID to indicate it's not in the object table
            go.parent_id = -1  # No parent
            go.component_count = 0  # We don't know the component count yet
            go.guid = self.header.gameobject_guid if hasattr(self.header, 'gameobject_guid') else b'\x00' * 16
            self.gameobjects.append(go)
                
            # Store this as a special property to clearly indicate AIWAYP GameObject isn't in object table
            self.aiwayp_root_gameobject = go
            
            # Jump to RSZ section
            self._current_offset = self.header.rsz_offset
            self._parse_rsz_section(data)
            self._parse_instances(data)
        else:
            # Fallback if RSZ offset is not found
            print("Warning: No RSZ offset found in AIWAYP file, using default data_offset")
            self._current_offset = self.header.data_offset
            self._parse_rsz_section(data)
            self._parse_instances(data)'''

    def _parse_scn_file(self, data: bytes):
        """Parse standard SCN file structure"""
        self._parse_gameobjects(data)
        self._parse_folder_infos(data)
        if self.filepath.lower().endswith('.18'):
            _parse_scn_18_resource_infos(self, data)
        else:
            self._parse_resource_infos(data)
        
        self._parse_prefab_infos(data)
        # SCN.19 format doesn't have userdata_infos
        if not (self.filepath.lower().endswith('.19') or self.filepath.lower().endswith('.18')):
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
            is_scn19 = self.filepath.lower().endswith('.19') or self.filepath.lower().endswith('.18')
            for i in range(self.header.info_count):
                go = RszGameObject()
                self._current_offset = go.parse(data, self._current_offset, is_scn19)
                self.gameobjects.append(go)

    def _parse_gameobject_ref_infos(self, data):
        for i in range(self.header.gameobject_ref_info_count):
            gori = GameObjectRefInfo()
            self._current_offset = gori.parse(data, self._current_offset)
            self.gameobject_ref_infos.append(gori)
        self._current_offset = _align(self._current_offset, 16)

    def _parse_folder_infos(self, data):
        for i in range(self.header.folder_count):
            fi = RszFolderInfo()
            self._current_offset = fi.parse(data, self._current_offset)
            self.folder_infos.append(fi)
        if not self.filepath.lower().endswith('.18'): self._current_offset = _align(self._current_offset, 16)
        else: self._current_offset += 16

    def _parse_resource_infos(self, data):
        for i in range(self.header.resource_count):
            ri = RszResourceInfo()
            self._current_offset = ri.parse(data, self._current_offset)
            self.resource_infos.append(ri)
        self._current_offset = _align(self._current_offset, 16)

    def _parse_prefab_infos(self, data):
        for i in range(self.header.prefab_count):
            pi = RszPrefabInfo()
            self._current_offset = pi.parse(data, self._current_offset)
            self.prefab_infos.append(pi)
        self._current_offset = _align(self._current_offset, 16)

    def _parse_userdata_infos(self, data):
        for i in range(self.header.userdata_count):
            ui = RszUserDataInfo()
            self._current_offset = ui.parse(data, self._current_offset)
            self.userdata_infos.append(ui)
        self._current_offset = _align(self._current_offset, 16)

    def _parse_blocks(self, data):

        self._prefab_str_map.clear()
        self._userdata_str_map.clear()
        self._rsz_userdata_str_map.clear()
        
        # Batch process all strings instead of one-by-one
        if not self.filepath.lower().endswith('.18'):
            self._resource_str_map.clear()
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
        # For AIWAYP, use the RSZ offset from position 0x94
        #if self.is_aiwayp and hasattr(self.header, 'rsz_offset') and self.header.rsz_offset > 0:
        #    self._current_offset = self.header.rsz_offset
        #    self.header.data_offset = self.header.rsz_offset
        #else:
        self._current_offset = self.header.data_offset

        self.rsz_header = RszRSZHeader()
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
            ii = RszInstanceInfo()
            self._current_offset = ii.parse(data, self._current_offset)
            if self.rsz_header.version < 4:
                self._current_offset += 8
            self.instance_infos.append(ii)

        # Only parse userdata if v>3
        if self.rsz_header.version > 3:
            self._current_offset = self.header.data_offset + self.rsz_header.userdata_offset
            if self.filepath.lower().endswith('.19') or self.filepath.lower().endswith('.18'):
                self._parse_scn19_rsz_userdata(data)
            elif self.filepath.lower().endswith('.16'):
                self._current_offset = parse_pfb16_rsz_userdata(self, data)
            else:
                self._parse_standard_rsz_userdata(data)
        
        self.data = data[self._current_offset:]
        
        self._rsz_userdata_dict = {rui.instance_id: rui for rui in self.rsz_userdata_infos}
        self._rsz_userdata_set = set(self._rsz_userdata_dict.keys())

        file_offset_of_data = self._current_offset
        self._instance_base_mod = file_offset_of_data % 16

    def _parse_standard_rsz_userdata(self, data):
        """Parse standard RSZ userdata entries (16 bytes each)"""
        self.rsz_userdata_infos = []
        for i in range(self.rsz_header.userdata_count):
            rui = RszRSZUserDataInfo()
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
        self._current_offset = _align(new_offset, 16)

    def _parse_scn19_rsz_userdata(self, data):
        """Parse SCN.19 RSZ userdata entries (24 bytes each with embedded binary data)"""
        self._current_offset = parse_scn19_rsz_userdata(self, data)

    '''def _parse_aiwayp_rsz_userdata(self, data):
        """Parse AIWAYP RSZ userdata entries - AIWAYP has no embedded RSZ"""
        self.rsz_userdata_infos = []
        for i in range(self.rsz_header.userdata_count):
            rui = RszRSZUserDataInfo()
            self._current_offset = rui.parse(data, self._current_offset)
            if rui.string_offset != 0:
                abs_offset = self.header.data_offset + rui.string_offset
                s, _ = read_wstring(self.full_data, abs_offset, 1000)
                self.set_rsz_userdata_string(rui, s)
            self.rsz_userdata_infos.append(rui)
            
        self._current_offset = _align(self._current_offset, 16)'''

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

            if inst.type_id == 0:
                continue

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
                rsz_file=self
            )
            current_offset = new_offset
            
    def _write_field_value(self, field_def: dict, data_obj, out: bytearray):
        
        field_size = field_def.get("size", 4)
        field_align = field_def.get("align", 1)
        base_mod = getattr(self, "_write_base_mod", 0)
        
        if isinstance(data_obj, StructData):
            while (len(out) - base_mod) % 4:
                out.extend(b'\x00') 
            count = len(data_obj.values)
            out.extend(struct.pack("<I", count))
            if data_obj.values:
                for struct_value in data_obj.values:
                    original_type = data_obj.orig_type if hasattr(data_obj, 'orig_type') else None
                    
                    if original_type and self.type_registry:
                        struct_type_info, _ = self.type_registry.find_type_by_name(original_type)
                        if struct_type_info:
                            struct_fields = struct_type_info.get("fields", [])
                            
                            for struct_field in struct_fields:
                                field_name = struct_field.get("name")
                                if field_name and field_name in struct_value:
                                    field_data = struct_value[field_name]
                                    self._write_field_value(struct_field, field_data, out)
                        else:
                            print(f"ERROR: Could not find type info for struct type: {original_type}")
                    else:
                        print(f"ERROR: Missing original_type for StructData or type_registry not available")

        elif field_def.get("array", False):

            while (len(out) - base_mod) % 4:
                out.extend(b'\x00') 

            count = len(data_obj.values)
            out.extend(struct.pack("<I", count))
            
            for element in data_obj.values:
                while (len(out) - base_mod) % field_align != 0:
                    out.extend(b'\x00')

                if isinstance(element, S8Data):
                    out.extend(struct.pack("<b", max(-128, min(127, element.value))))
                elif isinstance(element, U8Data):
                    out.extend(struct.pack("<B", element.value))
                elif isinstance(element, BoolData):
                    out.extend(struct.pack("<?", element.value))
                elif isinstance(element, S16Data):
                    out.extend(struct.pack("<h", element.value))
                elif isinstance(element, U16Data):
                    out.extend(struct.pack("<H", element.value))
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
                elif isinstance(element, PositionData):
                    out.extend(struct.pack("<3d", element.x, element.y, element.z))
                elif isinstance(element, Int3Data):
                    out.extend(struct.pack("<3i", element.x, element.y, element.z))
                elif isinstance(element, Float4Data):
                    out.extend(struct.pack("<4f", element.x, element.y, element.z, element.w))
                elif isinstance(element, QuaternionData):
                    out.extend(struct.pack("<4f", element.x, element.y, element.z, element.w))
                elif isinstance(element, ColorData):
                    out.extend(struct.pack("<4B", element.r, element.g, element.b, element.a))
                elif isinstance(element, (ObjectData, U32Data, UserDataData)):
                    value = int(element.value) & 0xFFFFFFFF
                    out.extend(struct.pack("<I", value))
                elif isinstance(element, Vec3Data) or isinstance(element, Vec3ColorData):
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
                        out.extend(struct.pack("<16f", *([0.0] * 16)))
                elif isinstance(element, (GameObjectRefData, GuidData)):
                    guid = uuid.UUID(element.guid_str)
                    out.extend(guid.bytes_le)
                elif isinstance(element, (StringData, ResourceData)):
                    while (len(out) - base_mod) % 4:
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
                    remaining_bytes = field_size - len(element.raw_bytes)
                    if remaining_bytes > 0:
                        out.extend(b'\x00' * remaining_bytes)
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
                elif isinstance(element, CapsuleData):
                    out.extend(struct.pack("<3f", element.start.x, element.start.y, element.start.z))
                    out.extend(struct.pack("<f", 0.0)) 
                    out.extend(struct.pack("<3f", element.end.x, element.end.y, element.end.z))
                    out.extend(struct.pack("<f", 0.0))
                    out.extend(struct.pack("<f", element.radius))
                    out.extend(b'\x00' * 12)
                elif isinstance(element, AreaData):
                    out.extend(struct.pack("<2f", element.p0.x, element.p0.y))
                    out.extend(struct.pack("<2f", element.p1.x, element.p1.y))
                    out.extend(struct.pack("<2f", element.p2.x, element.p2.y))
                    out.extend(struct.pack("<2f", element.p3.x, element.p3.y))
                    out.extend(struct.pack("<f", element.height))
                    out.extend(struct.pack("<f", element.bottom))
                    out.extend(b'\x00' * 8)
                else:
                    val = getattr(element, 'value', 0)
                    raw_bytes = val.to_bytes(field_size, byteorder='little')
                    out.extend(raw_bytes)
        else:
            while (len(out) - base_mod) % field_align != 0:
                out.extend(b'\x00')

            if isinstance(data_obj, S8Data):
                out.extend(struct.pack("<b", max(-128, min(127, data_obj.value))))
            elif isinstance(data_obj, U8Data):
                out.extend(struct.pack("<B", data_obj.value))
            elif isinstance(data_obj, S16Data):
                out.extend(struct.pack("<h", data_obj.value))
            elif isinstance(data_obj, U16Data):
                out.extend(struct.pack("<H", data_obj.value))
            elif isinstance(data_obj, S64Data):
                out.extend(struct.pack("<q", data_obj.value))
            elif isinstance(data_obj, S32Data):
                out.extend(struct.pack("<i", data_obj.value))
            elif isinstance(data_obj, U64Data):
                out.extend(struct.pack("<Q", data_obj.value))
            elif isinstance(data_obj, F64Data):
                out.extend(struct.pack("<d", data_obj.value))
            elif isinstance(data_obj, Vec2Data):
                out.extend(struct.pack("<4f", data_obj.x, data_obj.y, 0, 0))
            elif isinstance(data_obj, Float2Data):
                out.extend(struct.pack("<2f", data_obj.x, data_obj.y))
            elif isinstance(data_obj, RangeData):
                out.extend(struct.pack("<2f", data_obj.min, data_obj.max))
            elif isinstance(data_obj, RangeIData):
                out.extend(struct.pack("<2i", data_obj.min, data_obj.max))
            elif isinstance(data_obj, Float3Data):
                out.extend(struct.pack("<3f", data_obj.x, data_obj.y, data_obj.z))
            elif isinstance(data_obj, PositionData):
                out.extend(struct.pack("<3d", data_obj.x, data_obj.y, data_obj.z))
            elif isinstance(data_obj, Int3Data):
                out.extend(struct.pack("<3i", data_obj.x, data_obj.y, data_obj.z))
            elif isinstance(data_obj, Float4Data):
                out.extend(struct.pack("<4f", data_obj.x, data_obj.y, data_obj.z, data_obj.w))
            elif isinstance(data_obj, QuaternionData):
                out.extend(struct.pack("<4f", data_obj.x, data_obj.y, data_obj.z, data_obj.w))
            elif isinstance(data_obj, ColorData):
                out.extend(struct.pack("<4B", data_obj.r, data_obj.g, data_obj.b, data_obj.a))
            elif isinstance(data_obj, (ObjectData, U32Data, UserDataData)):
                value = int(data_obj.value) & 0xFFFFFFFF
                out.extend(struct.pack("<I", value))
            elif isinstance(data_obj, Vec3Data) or isinstance(data_obj, Vec3ColorData):
                out.extend(struct.pack("<4f", data_obj.x, data_obj.y, data_obj.z, 0.00))
            elif isinstance(data_obj, Vec4Data):
                out.extend(struct.pack("<4f", data_obj.x, data_obj.y, data_obj.z, data_obj.w))
            elif isinstance(data_obj, (GameObjectRefData, GuidData)):
                guid = uuid.UUID(data_obj.guid_str)
                out.extend(guid.bytes_le)
            elif isinstance(data_obj, (StringData, ResourceData)):
                while (len(out) - base_mod) % 4:
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
                    out.extend(struct.pack("<I", char_count))
                    out.extend(str_bytes)
                else:
                    out.extend(struct.pack("<I", 0))
            elif isinstance(data_obj, BoolData):
                out.extend(struct.pack("<?", bool(data_obj.value)))
            elif isinstance(data_obj, F32Data):
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
                out.extend(data_obj.raw_bytes)
                remaining_bytes = field_size - len(data_obj.raw_bytes)
                if remaining_bytes > 0:
                    out.extend(b'\x00' * remaining_bytes)
            elif isinstance(data_obj, CapsuleData):
                out.extend(struct.pack("<3f", data_obj.start.x, data_obj.start.y, data_obj.start.z))
                out.extend(struct.pack("<f", 0.0)) 
                out.extend(struct.pack("<3f", data_obj.end.x, data_obj.end.y, data_obj.end.z))
                out.extend(struct.pack("<f", 0.0))
                out.extend(struct.pack("<f", data_obj.radius))
                out.extend(b'\x00' * 12)
            elif isinstance(data_obj, AreaData):
                out.extend(struct.pack("<2f", data_obj.p0.x, data_obj.p0.y))
                out.extend(struct.pack("<2f", data_obj.p1.x, data_obj.p1.y))
                out.extend(struct.pack("<2f", data_obj.p2.x, data_obj.p2.y))
                out.extend(struct.pack("<2f", data_obj.p3.x, data_obj.p3.y))
                out.extend(struct.pack("<f", data_obj.height))
                out.extend(struct.pack("<f", data_obj.bottom))
                out.extend(b'\x00' * 8)

                
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
    
    def get_resources_dynamically(self):
        """Get resources dynamically based on resource fields"""
        resources = []
        for instance_id, fields in sorted(self.parsed_elements.items()):
                
            inst_info = self.instance_infos[instance_id]
            type_info = self.type_registry.get_type_info(inst_info.type_id)
            if not type_info:
                continue
                
            fields_def = type_info.get("fields", [])
            
            for field_def in fields_def:
                field_name = field_def["name"]
                if field_def["type"] == "Resource":

                    print(f"Processing field: {field_def['name']} of type {field_def['type']}")
                    if field_def['array'] == False:
                        if fields[field_name].value and fields[field_name].value not in resources:
                            resources.append(fields[field_name].value)
                            print(f"Found dynamic resource: {fields[field_name].value}")
                    else:
                        for element in fields[field_name].values:
                            if element.value and element.value not in resources:
                                resources.append(element.value)
                                print(f"Found dynamic resource: {element.value}")
        return resources

    def build(self, special_align_enabled = False) -> bytes:
        if self.is_usr:
            return self._build_usr(special_align_enabled)
        elif self.is_pfb:
            if self.filepath.lower().endswith('.16'):
                return build_pfb_16(self, special_align_enabled)
            else:
                return self._build_pfb(special_align_enabled)
        #elif self.is_aiwayp:
        #    return self._build_aiwayp(special_align_enabled)
        
        elif self.filepath.lower().endswith('.19'):
                return build_scn_19(self, special_align_enabled)
        elif self.filepath.lower().endswith('.18'):
            return build_scn_18(self, special_align_enabled)
        
        if self.auto_resource_management:
            dynamic_resources = self.get_resources_dynamically()
            self.resource_infos.clear()
            self._resource_str_map.clear()
            
            for resource_path in dynamic_resources:
                ri = RszResourceInfo()
                ri.string_offset = 0
                ri.reserved = 0
                self.resource_infos.append(ri)
                self.set_resource_string(ri, resource_path)
        
        self.header.info_count = len(self.gameobjects)
        self.header.folder_count = len(self.folder_infos)
        self.header.resource_count = len(self.resource_infos)
        self.header.prefab_count = len(self.prefab_infos)
        self.header.userdata_count = len(self.userdata_infos)
        
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
            if self.filepath.lower().endswith('.19') or self.filepath.lower().endswith('.18'):
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
    '''
    def _build_aiwayp(self, special_align_enabled=False) -> bytes:
        """Build AIWAYP file"""
        self.header.resource_count = len(self.resource_infos)
        self.header.userdata_count = len(self.userdata_infos)
        
        if self.rsz_header:
            self.rsz_header.object_count = len(self.object_table)
            self.rsz_header.instance_count = len(self.instance_infos)
            self.rsz_header.userdata_count = len(self.rsz_userdata_infos)
        
        out = bytearray()
        
        # 1) Write AIWAYP header with zeroed offsets initially 
        out += struct.pack(
            "<4sI3I3Q", 
            self.header.signature,
            self.header.version,
            self.header.info_count,
            self.header.resource_count,
            self.header.userdata_count,
            0,  # resource_info_tbl
            0,  # userdata_info_tbl
            0   # data_offset
        )
        
        # 2) Write GameObject info
        gameobject_name = getattr(self.header, 'gameobject_name', "AIWAYP_Object")
        gameobject_type = getattr(self.header, 'gameobject_type', "AIWAYP_Type")
        
        # Write name string
        name_utf16 = gameobject_name.encode('utf-16-le')
        if not name_utf16.endswith(b'\x00\x00'):
            name_utf16 += b'\x00\x00'
        out += struct.pack("<I", (len(name_utf16) // 2))
        out += name_utf16
        
        # Write type string
        type_utf16 = gameobject_type.encode('utf-16-le')
        if not type_utf16.endswith(b'\x00\x00'):
            type_utf16 += b'\x00\x00'
        out += struct.pack("<I", (len(type_utf16) // 2))
        out += type_utf16
        
        # Write unknown values (preserve from original file)
        unknown_value1 = getattr(self.header, 'unknown_value1', 0)
        unknown_value2 = getattr(self.header, 'unknown_value2', 0)
        out += struct.pack("<II", unknown_value1, unknown_value2)
        
        # Write GameObject GUID
        gameobject_guid = getattr(self.header, 'gameobject_guid', b'\x00' * 16)
        if self.gameobjects and len(self.gameobjects) > 0:
            gameobject_guid = self.gameobjects[0].guid
        out += gameobject_guid

        # Calculate and write resource and userdata tables - similar to USR format
        resource_info_tbl = _align(len(out), 16)
        resource_info_size = len(self.resource_infos) * 8
        
        userdata_info_tbl = _align(resource_info_tbl + resource_info_size, 16)
        userdata_info_size = len(self.userdata_infos) * 16
        
        # Calculate string positions
        string_start = _align(userdata_info_tbl + userdata_info_size, 16)
        current_offset = string_start
        
        # Calculate resource string offsets
        new_resource_offsets = {}
        for ri in self.resource_infos:
            resource_string = self._resource_str_map.get(ri, "")
            if resource_string:
                new_resource_offsets[ri] = current_offset
                current_offset += len(resource_string.encode('utf-16-le')) + 2
            else:
                new_resource_offsets[ri] = 0
        
        # Calculate userdata string offsets
        new_userdata_offsets = {}
        for ui in self.userdata_infos:
            userdata_string = self._userdata_str_map.get(ui, "")
            if userdata_string:
                new_userdata_offsets[ui] = current_offset
                current_offset += len(userdata_string.encode('utf-16-le')) + 2
            else:
                new_userdata_offsets[ui] = 0
        
        # Write resource infos
        while len(out) % 16 != 0:
            out += b"\x00"
        resource_info_tbl = len(out)
        
        for ri in self.resource_infos:
            ri.string_offset = new_resource_offsets[ri]
            out += struct.pack("<II", ri.string_offset, ri.reserved)

        # Write userdata infos
        while len(out) % 16 != 0:
            out += b"\x00"
        userdata_info_tbl = len(out)
        
        for ui in self.userdata_infos:
            ui.string_offset = new_userdata_offsets[ui]
            out += struct.pack("<IIQ", ui.hash, ui.crc, ui.string_offset)

        # Write strings
        string_entries = []
        for ri, offset in new_resource_offsets.items():
            if offset:
                string_entries.append((offset, self._resource_str_map.get(ri, "").encode("utf-16-le") + b"\x00\x00"))
                
        for ui, offset in new_userdata_offsets.items():
            if offset:
                string_entries.append((offset, self._userdata_str_map.get(ui, "").encode("utf-16-le") + b"\x00\x00"))
        
        string_entries.sort(key=lambda x: x[0])
        
        current_offset = string_entries[0][0] if string_entries else len(out)
        while len(out) < current_offset:
            out += b"\x00"
            
        for offset, string_data in string_entries:
            while len(out) < offset:
                out += b"\x00"
            out += string_data

        # RSZ Section (standard format without embedded userdata)
        rsz_offset = 0
        if self.rsz_header:
            if special_align_enabled:
                while len(out) % 16 != 0:
                    out += b"\x00"
                    
            rsz_start = len(out)
            rsz_offset = rsz_start  # Save for later
            self.header.data_offset = rsz_start

            # Use common RSZ section building function
            self._build_rsz_section(out, special_align_enabled)

        # Update header
        header_bytes = struct.pack(
            "<4sI3I3Q", 
            self.header.signature,
            self.header.version,
            self.header.info_count,
            self.header.resource_count,
            self.header.userdata_count,
            resource_info_tbl,
            userdata_info_tbl,
            self.header.data_offset
        )
        out[0:AiwaypHeader.SIZE] = header_bytes
        
        # Write RSZ offset at position 0x94 (148)
        if rsz_offset > 0:
            # Ensure we have enough space
            while len(out) <= 152:
                out += b"\x00"
            struct.pack_into("<I", out, 148, rsz_offset)

        return bytes(out)
    '''
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
        """Get resource string with special handling for PFB.16 format"""
        try:
            if self.is_pfb16:
                if hasattr(ri, 'string_value') and ri.string_value:
                    return ri.string_value
                    
                if ri in self._resource_str_map:
                    return self._resource_str_map[ri]
                    
                if hasattr(self, '_pfb16_direct_strings'):
                    try:
                        idx = self.resource_infos.index(ri)
                        if 0 <= idx < len(self._pfb16_direct_strings):
                            return self._pfb16_direct_strings[idx]
                    except (ValueError, IndexError):
                        pass
                        
                return f"[Resource {self.resource_infos.index(ri) if ri in self.resource_infos else '?'}]"
            
            return self._resource_str_map.get(ri, "")
        except Exception as e:
            print(f"Error getting resource string: {e}")
            return f"[Error: {str(e)[:30]}...]"
    
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
    
def parse_instance_fields(raw: bytes, offset: int, fields_def: list,
                          current_instance_index=None, rsz_file=None):
    """Parse fields from raw data according to field definitions – optimized version."""
    children = []

    unpack_uint    = struct.Struct("<I").unpack_from
    unpack_int     = struct.Struct("<i").unpack_from
    unpack_float   = struct.Struct("<f").unpack_from
    unpack_4float  = struct.Struct("<4f").unpack_from
    unpack_3float  = struct.Struct("<3f").unpack_from
    unpack_3double = struct.Struct("<3d").unpack_from
    unpack_2float  = struct.Struct("<2f").unpack_from
    unpack_2int    = struct.Struct("<2i").unpack_from
    unpack_3int    = struct.Struct("<3i").unpack_from
    unpack_sbyte   = struct.Struct("<b").unpack_from
    unpack_ubyte   = struct.Struct("<B").unpack_from
    unpack_4ubyte  = struct.Struct("<4B").unpack_from
    unpack_short   = struct.Struct("<h").unpack_from
    unpack_ushort  = struct.Struct("<H").unpack_from
    unpack_long    = struct.Struct("<q").unpack_from
    unpack_ulong   = struct.Struct("<Q").unpack_from
    unpack_double  = struct.Struct("<d").unpack_from
    unpack_20float = struct.Struct("<20f").unpack_from
    unpack_16float = struct.Struct("<16f").unpack_from

    rsz_userdataInfos = rsz_file.rsz_userdata_infos
    parsed_elements   = rsz_file.parsed_elements.setdefault(current_instance_index, {})
    instance_hierarchy = rsz_file.instance_hierarchy
    gameobject_ids    = rsz_file._gameobject_instance_ids
    folder_ids        = rsz_file._folder_instance_ids
    rsz_userdata_map  = rsz_file._rsz_userdata_str_map
    current_hierarchy = instance_hierarchy[current_instance_index]
    current_children  = current_hierarchy["children"]

    def _align_rel(pos: int, align: int, base_mod: int) -> int:
        rem = (pos + base_mod) % align
        return pos if rem == 0 else pos + (align - rem)

    pos = offset

    # compute how the file’s data-block was aligned (0 for v4+, 8 for v3, etc.)
    base_mod = getattr(rsz_file, "_instance_base_mod", 0)

    def _align(p: int, a: int) -> int:
        return _align_rel(p, a, base_mod)
    
    def read_aligned_value(unpack_func, size, align=1):
        nonlocal pos
        pos = _align(pos, align)
        v = unpack_func(raw, pos)[0]
        pos += size
        return v

    def read_aligned_bytes(length, align=1):
        nonlocal pos
        pos = _align(pos, align)
        seg = raw[pos:pos+length]
        pos += length
        return seg.tobytes() if hasattr(seg, "tobytes") else seg

    def read_string_value():
        nonlocal pos
        pos = _align(pos, 4)
        count = read_aligned_value(unpack_uint, 4, align=4)
        str_byte_count = count * 2
        segment = raw[pos:pos+str_byte_count]
        value = segment.decode('utf-16-le')
        pos += str_byte_count
        return value

    def read_string_value_utf8():
        nonlocal pos
        pos = _align(pos, 4)
        count = read_aligned_value(unpack_uint, 4, align=4)
        segment = raw[pos:pos+count]
        value = segment.decode('utf-8')
        pos += count
        return value

    def set_parent(idx, parent_idx):
        if 0 <= idx < len(instance_hierarchy):
            instance_hierarchy[idx]["parent"] = parent_idx
        else:
            print(f"Warning: Invalid instance index {idx} encountered (parent: {parent_idx})")

    def is_valid_ref(candidate):
        return (0 < candidate < current_instance_index and 
                candidate not in gameobject_ids and 
                candidate not in folder_ids)

    for field in fields_def:
        field_name  = field.get("name", "<unnamed>")
        ftype       = field.get("type", "unknown").lower()
        fsize       = field.get("size", 4)
        is_native   = field.get("native", False)
        is_array    = field.get("array", False)
        original_type = field.get("original_type", "")
        field_align = int(field.get("align", 1))
        rsz_type    = get_type_class(ftype, fsize, is_native, is_array,
                                     field_align, original_type, field_name)
        
        data_obj = None

        if is_array:
            count = read_aligned_value(unpack_uint, 4, align=4)

            if rsz_type == StructData:
                struct_values = []
                struct_type_info = None
                if original_type and rsz_file.type_registry:
                    struct_type_info, _ = rsz_file.type_registry.find_type_by_name(original_type)
                if struct_type_info and count > 0 and pos < len(raw):
                    struct_fields_def = struct_type_info.get("fields", [])
                    current_pos = pos
                    for i in range(count):
                        struct_element = {}
                        temp_parser = type('StructParser', (), {
                            'parsed_elements': {current_instance_index: struct_element},
                            'instance_hierarchy': instance_hierarchy,
                            '_gameobject_instance_ids': gameobject_ids,
                            '_folder_instance_ids': folder_ids,
                            'rsz_userdata_infos': rsz_userdataInfos,
                            '_rsz_userdata_str_map': rsz_userdata_map,
                            'type_registry': rsz_file.type_registry
                        })()
                        next_pos = parse_instance_fields(
                            raw=raw,
                            offset=current_pos,
                            fields_def=struct_fields_def,
                            current_instance_index=current_instance_index,
                            rsz_file=temp_parser
                        )
                        if next_pos > current_pos and struct_element:
                            struct_values.append(struct_element)
                            current_pos = next_pos
                        else:
                            break
                    pos = current_pos
                data_obj = StructData(struct_values, original_type)

            elif rsz_type == MaybeObject:
                child_indexes = []
                all_values = []
                alreadyRef = False
                for i in range(count):
                    pos = _align(pos, field_align)
                    value = unpack_uint(raw, pos)[0]
                    raw_value = (raw[pos:pos+fsize].tobytes() if hasattr(raw[pos:pos+fsize], "tobytes")
                                 else raw[pos:pos+fsize])
                    if is_valid_ref(value) and i == 0:
                        alreadyRef = True
                    if alreadyRef:
                        child_indexes.append(value)
                        current_children.append(value)
                        set_parent(value, current_instance_index)
                        all_values.append(value)
                    else:
                        all_values.append(raw_value)
                    pos += fsize
                data_obj = ArrayData(
                    [ObjectData(x, original_type) for x in child_indexes] if alreadyRef else 
                    [RawBytesData(x, fsize, original_type) for x in all_values],
                    ObjectData if alreadyRef else RawBytesData,
                    original_type
                )

            elif rsz_type == UserDataData:
                userdata_values = []
                userdatas = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    candidate = read_aligned_value(unpack_uint, 4, align=field_align)
                    userdatas.append(candidate)
                    found = None
                    for rui in rsz_userdataInfos:
                        if rui.instance_id == candidate:
                            found = rui
                            userdata_values.append(rsz_userdata_map.get(rui, f"Empty Userdata {candidate}"))
                            break
                    if not found:
                        userdata_values.append(f"Empty Userdata {candidate}")
                data_obj = ArrayData(
                    [rsz_type(idx, val, original_type) for val, idx in zip(userdata_values, userdatas)],
                    rsz_type,
                    original_type
                )

            elif rsz_type == GameObjectRefData:
                guids = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    guid_bytes = read_aligned_bytes(fsize, align=field_align)
                    guid_str = guid_le_to_str(guid_bytes)
                    guids.append((guid_str, guid_bytes))
                data_obj = ArrayData([rsz_type(g, raw, original_type) for g, raw in guids], rsz_type, original_type)

            elif rsz_type == ObjectData:
                child_indexes = []
                for x in range(count):
                    pos = _align(pos, field_align)
                    idx = read_aligned_value(unpack_uint, fsize, align=field_align)
                    child_indexes.append(idx)
                    current_children.append(idx)
                    set_parent(idx, current_instance_index)
                data_obj = ArrayData([rsz_type(idx, original_type) for idx in child_indexes],
                                     rsz_type, original_type)

            elif rsz_type in (Vec3Data, Vec3ColorData):
                vec3_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    vals = unpack_4float(raw, pos)
                    vec3_objects.append(rsz_type(vals[0], vals[1], vals[2], original_type))
                    pos += fsize
                data_obj = ArrayData(vec3_objects, rsz_type, original_type)

            elif rsz_type in (Vec4Data, Float4Data, QuaternionData):
                vec4_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    vals = unpack_4float(raw, pos)
                    vec4_objects.append(rsz_type(*vals, original_type))
                    pos += fsize
                data_obj = ArrayData(vec4_objects, rsz_type, original_type)

            elif rsz_type in (Vec2Data, Float2Data):
                vec2_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    vals = unpack_2float(raw, pos)
                    vec2_objects.append(rsz_type(*vals, original_type))
                    pos += fsize
                data_obj = ArrayData(vec2_objects, rsz_type, original_type)

            elif rsz_type == Float3Data:
                f3_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    vals = unpack_3float(raw, pos)
                    f3_objects.append(rsz_type(*vals, original_type))
                    pos += fsize
                data_obj = ArrayData(f3_objects, rsz_type, original_type)

            elif rsz_type == PositionData:
                pos_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    vals = unpack_3double(raw, pos)
                    pos_objects.append(rsz_type(*vals, original_type))
                    pos += fsize
                data_obj = ArrayData(pos_objects, rsz_type, original_type)

            elif rsz_type == Int3Data:
                int3_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    vals = unpack_3int(raw, pos)
                    int3_objects.append(rsz_type(*vals, original_type))
                    pos += fsize
                data_obj = ArrayData(int3_objects, rsz_type, original_type)

            elif rsz_type == Mat4Data:
                mat4_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    floats = unpack_16float(raw, pos)
                    mat4_objects.append(rsz_type(list(floats), original_type))
                    pos += fsize
                data_obj = ArrayData(mat4_objects, rsz_type, original_type)

            elif rsz_type == RangeData:
                range_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    vals = unpack_2float(raw, pos)
                    range_objects.append(rsz_type(vals[0], vals[1], original_type))
                    pos += fsize
                data_obj = ArrayData(range_objects, rsz_type, original_type)

            elif rsz_type == RangeIData:
                range_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    vals = unpack_2int(raw, pos)
                    range_objects.append(rsz_type(vals[0], vals[1], original_type))
                    pos += fsize
                data_obj = ArrayData(range_objects, rsz_type, original_type)

            elif rsz_type == OBBData:
                obb_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    floats = unpack_20float(raw, pos)
                    obb_objects.append(rsz_type(list(floats), original_type))
                    pos += fsize
                data_obj = ArrayData(obb_objects, rsz_type, original_type)

            elif rsz_type == CapsuleData:
                area_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                        
                    start_vals = unpack_4float(raw, pos)
                    pos += 16
                    end_vals = unpack_4float(raw, pos)
                    pos += 16
                    radius, *_ = struct.unpack_from("<f", raw, pos)
                    pos += 16
                    start_vec = Vec3Data(start_vals[0], start_vals[1], start_vals[2], "Vec3")
                    end_vec = Vec3Data(end_vals[0], end_vals[1], end_vals[2], "Vec3")
                    
                    area_objects.append(rsz_type(start_vec, end_vec, radius, original_type))

                data_obj = ArrayData(area_objects, rsz_type, original_type)

            elif rsz_type == AreaData:
            
                area_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                        
                    p0_vals = unpack_2float(raw, pos)
                    pos += 8
                    p1_vals = unpack_2float(raw, pos)
                    pos += 8
                    p2_vals = unpack_2float(raw, pos)
                    pos += 8
                    p3_vals = unpack_2float(raw, pos)
                    pos += 8
                    height, = struct.unpack_from("<f", raw, pos)
                    pos += 4
                    bottom, = struct.unpack_from("<f", raw, pos)
                    pos += 12
                    p0 = Float2Data(p0_vals[0], p0_vals[1], "Float2")
                    p1 = Float2Data(p1_vals[0], p1_vals[1], "Float2")
                    p2 = Float2Data(p2_vals[0], p2_vals[1], "Float2")
                    p3 = Float2Data(p3_vals[0], p3_vals[1], "Float2")
                    
                    area_objects.append(rsz_type(p0, p1, p2, p3, height, bottom, original_type))

                data_obj = ArrayData(area_objects, rsz_type, original_type)

            elif rsz_type == StringData:
                children = []
                for _ in range(count):
                    value = read_string_value()
                    children.append(value)

                data_obj = ArrayData([StringData(s) for s in children], StringData, original_type)

            elif rsz_type == ResourceData:
                children = []
                for _ in range(count):
                    value = read_string_value()
                    children.append(value)

                data_obj = ArrayData([ResourceData(s) for s in children], ResourceData, original_type)

            elif rsz_type == RuntimeTypeData:
                children = []
                for _ in range(count):
                    value = read_string_value_utf8()
                    children.append(value)

                data_obj = ArrayData([RuntimeTypeData(s) for s in children], RuntimeTypeData, original_type)

            elif rsz_type == S8Data:
                values = []
                for _ in range(count):
                    value = read_aligned_value(unpack_sbyte, 1, align=field_align)
                    values.append(rsz_type(value, original_type))
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == U8Data:
                values = []
                for _ in range(count):
                    value = read_aligned_value(unpack_ubyte, 1, align=field_align)
                    values.append(rsz_type(value, original_type))
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == BoolData:
                values = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    value = raw[pos] != 0
                    values.append(rsz_type(value, original_type))
                    pos += 1
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == S32Data:
                values = []
                for _ in range(count):
                    value = read_aligned_value(unpack_int, fsize, align=field_align)
                    values.append(rsz_type(value, original_type))
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == U32Data:
                values = []
                for _ in range(count):
                    value = read_aligned_value(unpack_uint, fsize, align=field_align)
                    values.append(rsz_type(value, original_type))
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == S16Data:
                values = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    value = unpack_short(raw, pos)[0]
                    values.append(rsz_type(value, original_type))
                    pos += 2
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == U16Data:
                values = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    value = unpack_ushort(raw, pos)[0]
                    values.append(rsz_type(value, original_type))
                    pos += 2
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == S64Data:
                values = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    value = unpack_long(raw, pos)[0]
                    values.append(rsz_type(value, original_type))
                    pos += 8
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == U64Data:
                values = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    value = unpack_ulong(raw, pos)[0]
                    values.append(rsz_type(value, original_type))
                    pos += 8
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == F32Data:
                values = []
                for _ in range(count):
                    value = read_aligned_value(unpack_float, 4, align=field_align)
                    values.append(rsz_type(value, original_type))
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == F64Data:
                values = []
                for _ in range(count):
                    value = read_aligned_value(unpack_double, 8, align=field_align)
                    values.append(rsz_type(value, original_type))
                data_obj = ArrayData(values, rsz_type, original_type)

            elif rsz_type == GuidData:
                guids = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    guid_bytes = read_aligned_bytes(fsize, align=field_align)
                    guid_str = guid_le_to_str(guid_bytes)
                    guids.append((guid_str, guid_bytes))
                data_obj = ArrayData([rsz_type(g[0], g[1], original_type) for g in guids],
                                     rsz_type, original_type)

            elif rsz_type == ColorData:
                color_objects = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    vals = unpack_4ubyte(raw, pos)
                    color_objects.append(rsz_type(vals[0], vals[1], vals[2], vals[3], original_type))
                    pos += fsize
                pos = _align(pos, field_align) if color_objects else pos
                data_obj = ArrayData(color_objects, rsz_type, original_type)

            else:
                children = []
                for _ in range(count):
                    pos = _align(pos, field_align)
                    raw_bytes = read_aligned_bytes(fsize, align=field_align)
                    children.append(RawBytesData(raw_bytes, fsize, original_type))
                data_obj = ArrayData(children, RawBytesData, original_type)

        else:  # Non-array field
            pos = _align(pos, field_align)
            if rsz_type == MaybeObject:
                candidate = read_aligned_value(unpack_uint, fsize, align=field_align)
                raw_candidate = (raw[pos-fsize:pos].tobytes() if hasattr(raw[pos-fsize:pos], "tobytes")
                                 else raw[pos-fsize:pos])
                if not is_valid_ref(candidate):
                    data_obj = RawBytesData(raw_candidate, fsize, original_type)
                else:
                    data_obj = ObjectData(candidate, original_type)
                    current_children.append(candidate)
                    set_parent(candidate, current_instance_index)
            elif rsz_type == UserDataData:
                instance_id = read_aligned_value(unpack_uint, 4, align=field_align)
                value = ""
                for rui in rsz_userdataInfos:
                    if rui.instance_id == instance_id:
                        value = rsz_userdata_map.get(rui, "")
                        break
                data_obj = rsz_type(instance_id, value, original_type)
            elif rsz_type == GameObjectRefData:
                guid_bytes = read_aligned_bytes(fsize, align=field_align)
                guid_str = guid_le_to_str(guid_bytes)
                data_obj = rsz_type(guid_str, guid_bytes, original_type)
            elif rsz_type == ObjectData:
                child_idx = read_aligned_value(unpack_uint, fsize, align=field_align)
                data_obj = rsz_type(child_idx, original_type)
                current_children.append(child_idx)
                set_parent(child_idx, current_instance_index)
            elif rsz_type in (Vec3Data, Vec3ColorData):
                vals = unpack_4float(raw, pos)
                pos += fsize
                data_obj = rsz_type(vals[0], vals[1], vals[2], original_type)
            elif rsz_type == Vec4Data:
                vals = unpack_4float(raw, pos)
                pos += fsize
                data_obj = rsz_type(*vals, original_type)
            elif rsz_type in (Vec2Data, Float2Data):
                vals = unpack_2float(raw, pos)
                pos += fsize
                data_obj = rsz_type(*vals, original_type)
            elif rsz_type == Float3Data:
                vals = unpack_3float(raw, pos)
                pos += fsize
                data_obj = rsz_type(*vals, original_type)
            elif rsz_type == PositionData:
                vals = unpack_3double(raw, pos)
                pos += fsize
                data_obj = rsz_type(*vals, original_type)
            elif rsz_type == Int3Data:
                vals = unpack_3int(raw, pos)
                pos += fsize
                data_obj = rsz_type(*vals, original_type)
            elif rsz_type == Float4Data:
                vals = unpack_4float(raw, pos)
                pos += fsize
                data_obj = rsz_type(*vals, original_type)
            elif rsz_type == RangeIData:
                vals = unpack_2int(raw, pos)
                pos += fsize
                data_obj = rsz_type(vals[0], vals[1], original_type)
            elif rsz_type == StringData:
                value = read_string_value()
                children.append(value)
                data_obj = StringData(value, original_type)
            elif rsz_type == ResourceData:
                value = read_string_value()
                children.append(value)
                data_obj = ResourceData(value, original_type)
            elif rsz_type == RuntimeTypeData:
                value = read_string_value_utf8()
                children.append(value)
                data_obj = RuntimeTypeData(value, original_type)
            elif rsz_type == BoolData:
                value = raw[pos] != 0
                pos += fsize
                data_obj = rsz_type(value, original_type)
            elif rsz_type == S8Data:
                value = read_aligned_value(unpack_sbyte, fsize, align=field_align)
                data_obj = rsz_type(value, original_type)
            elif rsz_type == U8Data:
                value = read_aligned_value(unpack_ubyte, fsize, align=field_align)
                data_obj = rsz_type(value, original_type)
            elif rsz_type == U16Data:
                value = read_aligned_value(unpack_ushort, fsize, align=field_align)
                data_obj = rsz_type(value, original_type)
            elif rsz_type == S16Data:
                value = read_aligned_value(unpack_short, fsize, align=field_align)
                data_obj = rsz_type(value, original_type)
            elif rsz_type == S32Data:
                value = read_aligned_value(unpack_int, fsize, align=field_align)
                data_obj = rsz_type(value, original_type)
            elif rsz_type == S64Data:
                value = read_aligned_value(unpack_long, fsize, align=field_align)
                data_obj = rsz_type(value, original_type)
            elif rsz_type == U32Data:
                value = read_aligned_value(unpack_uint, fsize, align=field_align)
                data_obj = rsz_type(value, original_type)
            elif rsz_type == U64Data:
                value = read_aligned_value(unpack_ulong, fsize, align=field_align)
                data_obj = rsz_type(value, original_type)
            elif rsz_type == F32Data:
                value = read_aligned_value(unpack_float, 4, align=field_align)
                data_obj = rsz_type(value, original_type)
            elif rsz_type in (GameObjectRefData, GuidData):
                guid_bytes = read_aligned_bytes(fsize, align=field_align)
                guid_str = guid_le_to_str(guid_bytes)
                data_obj = rsz_type(guid_str, guid_bytes, original_type)
            elif rsz_type == Mat4Data:
                vals = unpack_16float(raw, pos)
                pos += fsize
                data_obj = rsz_type(vals, original_type)
            elif rsz_type == QuaternionData:
                vals = unpack_4float(raw, pos)
                pos += fsize
                data_obj = rsz_type(*vals, original_type)
            elif rsz_type == OBBData:
                vals = unpack_20float(raw, pos)
                pos += fsize
                data_obj = rsz_type(vals, original_type)
            elif rsz_type == RangeData:
                vals = unpack_2float(raw, pos)
                pos += fsize
                data_obj = rsz_type(vals[0], vals[1], original_type)
            elif rsz_type == ColorData:
                vals = unpack_4ubyte(raw, pos)
                pos += fsize
                data_obj = rsz_type(vals[0], vals[1], vals[2], vals[3], original_type)
            elif rsz_type == CapsuleData:
                start_vals = unpack_4float(raw, pos)
                pos += 16
                end_vals = unpack_4float(raw, pos)
                pos += 16
                radius, *_ = struct.unpack_from("<f", raw, pos)
                pos += 16
                start_vec = Vec3Data(start_vals[0], start_vals[1], start_vals[2], "Vec3")
                end_vec = Vec3Data(end_vals[0], end_vals[1], end_vals[2], "Vec3")
                data_obj = rsz_type(start_vec, end_vec, radius, original_type)
            elif rsz_type == AreaData:
                p0_vals = unpack_2float(raw, pos)
                pos += 8
                p1_vals = unpack_2float(raw, pos)
                pos += 8
                p2_vals = unpack_2float(raw, pos)
                pos += 8
                p3_vals = unpack_2float(raw, pos)
                pos += 8
                height, *_ = struct.unpack_from("<f", raw, pos)
                pos += 4
                bottom, *_ = struct.unpack_from("<f", raw, pos)
                pos += 12
                p0 = Float2Data(p0_vals[0], p0_vals[1], "Float2")
                p1 = Float2Data(p1_vals[0], p1_vals[1], "Float2")
                p2 = Float2Data(p2_vals[0], p2_vals[1], "Float2")
                p3 = Float2Data(p3_vals[0], p3_vals[1], "Float2")
                data_obj = rsz_type(p0, p1, p2, p3, height, bottom, original_type)
            else:
                raw_bytes = read_aligned_bytes(fsize, align=field_align)
                data_obj = RawBytesData(raw_bytes, fsize, original_type)

        parsed_elements[field_name] = data_obj

    return pos
