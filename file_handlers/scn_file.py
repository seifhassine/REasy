####TODOs:
# 1. Refactor
# 2. Parse into mapped variables  (unfinished)
# 3. Implement writing of data section from variables (currently writes existing data as is).
# 4. Parse userdata as objects and not strings

import struct
import uuid
from typing import Tuple
from file_handlers.rcol_file import align_offset
from file_handlers.scn_data_types import *

########################################
# Utility functions
########################################

def guid_le_to_str(guid_bytes: bytes) -> str:
    if len(guid_bytes) != 16:
        return f"INVALID_GUID_{guid_bytes.hex()}"
    try:
        return str(uuid.UUID(bytes_le=guid_bytes))
    except Exception:
        return f"INVALID_GUID_{guid_bytes.hex()}"


def read_wstring(data: bytes, offset: int, max_wchars: int) -> Tuple[str, int]:
    """
    Reads a UTF-16LE string from data starting at offset.
    Stops when two consecutive null bytes are found.
    """
    pos = offset
    # If a BOM is present, skip it.
    if pos + 1 < len(data) and data[pos:pos+2] == b"\xff\xfe":
        pos += 2
    chars = []
    while pos + 1 < len(data) and len(chars) < max_wchars:
        lo = data[pos]
        hi = data[pos+1]
        if lo == 0 and hi == 0:
            pos += 2
            break
        code = lo + (hi << 8)
        chars.append(code)
        pos += 2
    return "".join(chr(c) for c in chars), pos


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

class ScnGameObject:
    SIZE = 32
    def __init__(self):
        self.guid = b"\x00" * 16
        self.id = 0
        self.parent_id = 0
        self.component_count = 0
        self.ukn = 0
        self.prefab_id = 0
    def parse(self, data: bytes, offset: int) -> int:
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
        self.ukn, = struct.unpack_from("<H", data, offset)
        offset += 2
        self.prefab_id, = struct.unpack_from("<i", data, offset) 
        offset += 4
        return offset

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
        self.reserved = 0
        self.instance_offset = 0
        self.data_offset = 0
        self.userdata_offset = 0
    def parse(self, data: bytes, offset: int) -> int:
        fmt = "<I I I I I I Q Q Q"
        (self.magic,
         self.version,
         self.object_count,
         self.instance_count,
         self.userdata_count,
         self.reserved,
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
        self.gameobjects = []
        self.folder_infos = []
        self.resource_infos = []
        self.prefab_infos = []
        self.userdata_infos = []      # from main header
        self.rsz_userdata_infos = []  #from RSZ section
        self.resource_block = b""
        self.prefab_block = b""
        self.prefab_block_start = 0
        self.rsz_header = None
        self.object_table = []
        self.instance_infos = []
        self.data = b""
        self.type_registry = None
        self.debug = False
        self.parsed_instances = []
        self.nested_instance_indexes = set()
        self._current_offset = 0 
  
        self._string_cache = {}  # Cache for frequently accessed strings, will probably be removed.
        self._type_info_cache = {}
        
        self._rsz_userdata_dict = {}
        self._rsz_userdata_set = set()

        self._resource_str_map = {}  # {ResourceInfo: str}
        self._prefab_str_map = {}    # {PrefabInfo: str}
        self._userdata_str_map = {}  # {UserDataInfo: str}
        self._rsz_userdata_str_map = {}  # {RSZUserDataInfo: str}
        self.parsed_elements = {}  # dictionary to map instance_index -> {field_name: TypedDataObject}

    def read(self, data: bytes):
        self.full_data = memoryview(data)
        self._current_offset = 0
        
        # Parse sections in batches where possible
        self._parse_header(data)
        self._parse_gameobjects(data)
        self._parse_folder_infos(data)
        self._parse_resource_infos(data)
        self._parse_prefab_infos(data)
        self._parse_userdata_infos(data)
        self._parse_blocks(data)
        self._parse_rsz_section(data)
        self._parse_instances(data)

    def _parse_header(self, data):
        self.header = ScnHeader()
        self.header.parse(data)
        self._current_offset = ScnHeader.SIZE

    def _parse_gameobjects(self, data):
        for i in range(self.header.info_count):
            go = ScnGameObject()
            self._current_offset = go.parse(data, self._current_offset)
            self.gameobjects.append(go)

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
        
        for ri in self.resource_infos:
            if ri.string_offset != 0:
                s, _ = read_wstring(self.full_data, ri.string_offset, 1000)
                self.set_resource_string(ri, s)
                
        for pi in self.prefab_infos:
            if pi.string_offset != 0:
                s, _ = read_wstring(self.full_data, pi.string_offset, 1000)
                self.set_prefab_string(pi, s)
                
        for ui in self.userdata_infos:
            if ui.string_offset != 0:
                s, _ = read_wstring(self.full_data, ui.string_offset, 1000)
                self.set_userdata_string(ui, s)

    def _parse_rsz_section(self, data):
        self._current_offset = self.header.data_offset

        self.rsz_header = ScnRSZHeader()
        self._current_offset = self.rsz_header.parse(data, self._current_offset)

        # Parse Object Table – object_count entries (4 bytes each)
        fmt = f"<{self.rsz_header.object_count}i"
        self.object_table = list(struct.unpack_from(fmt, data, self._current_offset))
        self._current_offset += self.rsz_header.object_count * 4

        # Now reset offset to the absolute instanceOffset provided in RSZHeader
        self._current_offset = self.header.data_offset + self.rsz_header.instance_offset

        # Parse Instance Infos –that has instance_count entries (8 bytes each)
        for i in range(self.rsz_header.instance_count):
            ii = ScnInstanceInfo()
            self._current_offset = ii.parse(data, self._current_offset)
            self.instance_infos.append(ii)
            
        self._current_offset = self.header.data_offset + self.rsz_header.userdata_offset

        # Parse RSZUserDataInfos – each entry is 16 bytes.
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
            
        if self.debug:
            print(f"offset for data block {self._current_offset}")

        self.data = data[self._current_offset:]
        
        self._rsz_userdata_dict = {rui.instance_id: rui for rui in self.rsz_userdata_infos}
        self._rsz_userdata_set = set(self._rsz_userdata_dict.keys())

    def _align(self, offset: int, alignment: int) -> int:
        remainder = offset % alignment
        if remainder:
            return offset + (alignment - remainder)
        return offset

    def get_rsz_userdata_string(self, rui):
        return self._rsz_userdata_str_map.get(rui, "")

    def _parse_instances(self, data):
        """Parse instance data with optimizations"""
        self.nested_instance_indexes = set()
        self.parsed_instances = []
        current_offset = 0
        type_info_cache = {}
        ignore_list = {}
        
        if self.type_registry:
            for inst in self.instance_infos:
                if inst.type_id not in type_info_cache:
                    if inst.type_id in ignore_list:
                        type_info_cache[inst.type_id] = ignore_list[inst.type_id]
                    else:
                        type_info = self.type_registry.get_type_info(inst.type_id)
                        type_info_cache[inst.type_id] = type_info if type_info is not None else {}

        for idx, inst in enumerate(self.instance_infos):
            if idx == 0:
                self.parsed_instances.append([{"name": "NULL", "value": "", "subfields": []}])
                continue

            if idx in self._rsz_userdata_set:
                if self.debug:
                    print(f"DEBUG: Skipping parsing for instance {idx} due to RSZUserDataInfos match")
                self.parsed_instances.append([])
                continue

            type_info = type_info_cache.get(inst.type_id, {})
            fields_def = type_info.get("fields", [])
            
            parsed_fields, new_offset = parse_instance_fields(
                raw=self.data,
                offset=current_offset,
                fields_def=fields_def,
                type_registry=self.type_registry,
                instance_infos=self.instance_infos,
                nested_refs=self.nested_instance_indexes,
                current_instance_index=idx,
                rsz_userdata_infos=self._rsz_userdata_dict,
                scn_file=self,
                debug=self.debug,
                parent_results=type_info.get("name", "Unknown")  
            )
            if self.debug:
                print(f"DEBUG: Instance {idx} parsed, offset moved from {current_offset:#x} to {new_offset:#x} (delta {new_offset - current_offset})")
            self.parsed_instances.append(parsed_fields)
            current_offset = new_offset

    def build(self) -> bytes:
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
            self.header.folder_tbl,
            self.header.resource_info_tbl,
            self.header.prefab_info_tbl,
            self.header.userdata_info_tbl,
            self.header.data_offset
        )

        # 2) Write gameobjects
        for go in self.gameobjects:
            out += go.guid
            out += struct.pack("<i", go.id)
            out += struct.pack("<i", go.parent_id)
            out += struct.pack("<H", go.component_count)
            out += struct.pack("<H", go.ukn)
            out += struct.pack("<i", go.prefab_id) 

        # 3) Align and write folder infos, recording folder_tbl offset
        while len(out) % 16 != 0:
            out += b"\x00"
        folder_tbl_offset = len(out)
        for fi in self.folder_infos:
            out += struct.pack("<ii", fi.id, fi.parent_id)

        # 4) Align and write resource infos, recording resource_info_tbl offset
        while len(out) % 16 != 0:
            out += b"\x00"
        resource_info_tbl_offset = len(out)
        for ri in self.resource_infos:
            out += struct.pack("<II", ri.string_offset, ri.reserved)

        # 5) Align and write prefab infos, recording prefab_info_tbl offset
        while len(out) % 16 != 0:
            out += b"\x00"
        prefab_info_tbl_offset = len(out)
        for pi in self.prefab_infos:
            out += struct.pack("<II", pi.string_offset, pi.parent_id)

        # 6) Align and write user data infos, recording userdata_info_tbl offset
        while len(out) % 16 != 0:
            out += b"\x00"
        userdata_info_tbl_offset = len(out)
        for ui in self.userdata_infos:
            out += struct.pack("<IIQ", ui.hash, ui.crc, ui.string_offset)

        # Write resource strings
        for ri in self.resource_infos:
            out += self._resource_str_map.get(ri, "").encode("utf-16-le") + b"\x00\x00"
        
        # Write prefab strings 
        for pi in self.prefab_infos:
            out += self._prefab_str_map.get(pi, "").encode("utf-16-le") + b"\x00\x00"

        # Write userdata strings
        for ui in self.userdata_infos:
            out += self._userdata_str_map.get(ui, "").encode("utf-16-le") + b"\x00\x00"

        # 9) Write RSZ header/tables/userdata
        if self.rsz_header:
            rsz_start = len(out)
            self.header.data_offset = rsz_start 

            # 9a) Write RSZ header with placeholders
            rsz_header_offset = len(out)
            out += struct.pack(
                "<I I I I I I Q Q Q",
                self.rsz_header.magic,
                self.rsz_header.version,
                self.rsz_header.object_count,
                0,  # instance_offset placeholder
                self.rsz_header.userdata_count,
                self.rsz_header.reserved,
                0,  # instance_offset placeholder
                0,  # data_offset placeholder
                0   # userdata_offset placeholder
            )
            # 9b) Write Object Table (one 4-byte integer per object)
            object_table_size = self.rsz_header.object_count * 4
            for obj_id in self.object_table:
                out += struct.pack("<i", obj_id)
            # Calculate instance offset (relative to RSZ header)
            new_instance_offset = self.rsz_header.SIZE + object_table_size


            # 9c) Write Instance Infos (each entry 8 bytes) 
            instance_infos_size = len(self.instance_infos) * 8
            for inst in self.instance_infos:
                out += struct.pack("<II", inst.type_id, inst.crc)
            # Compute the end of instance infos section (relative to RSZ header)
            instance_section_end = self.rsz_header.SIZE + object_table_size + instance_infos_size
            # Align the end to 16 bytes for userdata block start
            new_userdata_offset = self._align(rsz_start + instance_section_end, 16) - rsz_start

            # Align out to 16 bytes before writing RSZUserDataInfos.
            while (len(out) % 16):
                out += b"\x00"

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
            out[rsz_header_offset:rsz_header_offset + self.rsz_header.SIZE] = new_rsz_header

        # 10) Write instance data
        # First align to 16 bytes
        while len(out) % 16 != 0:
            out += b"\x00"
            
        # Write actual instance data block: TODO
        if self.data:
            out += self.data

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
    if type_info and "name" in type_info:
        return f"{type_info['name']} (ID: {idx})"
    return f"Instance[{idx}]"

def is_valid_reference(candidate, current_instance_index):
    return (
        candidate is not None and
        current_instance_index is not None and
        candidate > 0 and
        candidate < current_instance_index 
    )

def parse_instance_fields(
    raw: bytes,
    offset: int, 
    fields_def: list,
    type_registry=None,
    instance_infos=None,
    nested_refs=None,
    current_instance_index=None,
    rsz_userdata_infos=None,
    scn_file=None,
    debug=False,
    parent_results=None  # Now consistently included in signature
):
    """Parse fields from raw data according to field definitions
    
    Args:
        raw: Raw bytes to parse
        offset: Starting offset in raw data
        fields_def: List of field definitions
        type_registry: Type registry for resolving type names
        instance_infos: List of instance infos for reference resolution
        nested_refs: Set to track nested references
        current_instance_index: Current instance being parsed
        rsz_userdata_infos: Dictionary of RSZ userdata info lookups
        scn_file: Parent ScnFile instance for string lookups
        debug: Enable debug logging
"""
    results = []
    pos = offset
    raw_len = len(raw)
    local_align = align_offset

    # Keep track of fields parsed in current level, could be useful for relationship between subsequent fields
    current_level_fields = {}

    def get_bytes(segment):
        return segment.tobytes() if hasattr(segment, "tobytes") else segment

    for field in fields_def:
        field_start = pos
        if pos + 4 > raw_len:
            if debug:
                print(f"DEBUG: Insufficient bytes for field '{field.get('name', '<unnamed>')}' at {pos:#x}")
            return results, pos
        field_name = field.get("name", "<unnamed>")
        ftype = field.get("type", "Unknown").lower()
        fsize = field.get("size", 4)
        subresults = []
        is_array = field.get("array", False)
        field_align = int(field["align"]) if "align" in field else 1

        if is_array:
            pos = local_align(pos, 4)
        elif "align" in field and field.get("original_type", "") not in ["System.Collections.Generic.List`1<via.vec3>", "System.Collections.Generic.List`1<via.vec4>"]:
            pos = local_align(pos, field_align)
            if debug:
                print(f"DEBUG: Aligning field '{field_name}' to {pos:#x}")
        
        if debug:
            print(f"DEBUG: Reading field '{field_name}' (type: {ftype}) starting at {pos:#x}")

        if ftype == "userdata":
            if is_array:
                pos = local_align(pos, 4)
                if pos + 4 > raw_len:
                    value = "[]"
                    results.append({
                        "name": field_name,
                        "value": value,
                        "subfields": []
                    })
                    continue

                count = struct.unpack_from("<I", raw, pos)[0]
                pos += 4

                userdata_values = []
                for _ in range(count):
                    if pos + 4 > raw_len:
                        break
                    candidate = struct.unpack_from("<I", raw, pos)[0]
                    pos += 4

                    found = None
                    if rsz_userdata_infos is not None:
                        if isinstance(rsz_userdata_infos, dict):
                            found = rsz_userdata_infos.get(candidate)
                        else:
                            for rui in rsz_userdata_infos:
                                if rui.instance_id == candidate:
                                    found = rui
                                    break

                    if found is not None and scn_file is not None:
                        userdata_str = scn_file.get_rsz_userdata_string(found)
                        userdata_values.append(userdata_str)
                    else:
                        userdata_values.append(str(candidate))

                results.append({
                    "name": field_name,
                    "value": f"[{', '.join(userdata_values)}]",
                    "subfields": []
                })
                if debug:
                    print(f"DEBUG: Field '{field_name}' (userdata array) parsed from {field_start:#x} to {pos:#x}")
                '''data_obj = UserDataArrayData(userdata_values)
                if scn_file is not None:
                    scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
                continue
            else:
                pos = local_align(pos, 4) #unsure
                if pos + 4 <= raw_len:
                    instance_id = struct.unpack_from("<I", raw, pos)[0]
                else:
                    instance_id = None
                pos += 4
                found = None
                if rsz_userdata_infos is not None:
                    if isinstance(rsz_userdata_infos, dict):
                        found = rsz_userdata_infos.get(instance_id)
                        if found and scn_file is not None:
                            value = scn_file.get_rsz_userdata_string(found)
                        else:
                            value = str(instance_id)
                    else:
                        for rui in rsz_userdata_infos:
                            if rui.instance_id == instance_id:
                                found = rui
                                if scn_file is not None:
                                    value = scn_file.get_rsz_userdata_string(rui)
                                break
                        if not found:
                            value = str(instance_id)

                if nested_refs is not None and instance_id is not None and is_valid_reference(instance_id, current_instance_index): #TODO: handle userdata properly
                    nested_refs.add(instance_id)
                    results.append({
                        "name": field_name,
                        "value": f"Child index: {instance_id}",
                        "subfields": []
                    })
                else:
                    results.append({
                        "name": field_name,
                        "value": value,
                        "subfields": []
                    })
                if debug:
                    print(f"DEBUG: Field '{field_name}' parsed from {field_start:#x} to {pos:#x} (delta {pos - field_start})")
                '''data_obj = StringData(value)
                if scn_file is not None:
                    scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
                continue
        
        elif is_array and ftype not in ("object", "obb", "vec3", "vec4", "gameobjectref") and fsize != 80:
            pos = local_align(pos, 4)
            if pos + 4 > raw_len:
                continue
            count = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
            
            children = []
            total = 0
            element_size = int(field.get("size", 4))
            is_data = ftype == "data"
            is_native = field.get("native", False)
            if ((ftype in ("s32", "u32") or is_data) and is_native and element_size == 4):
                child_indexes = []
                all_values = []
                ref_names = []
                alreadyRef = False
                for i in range(count):
                    total += 1
                    if pos + element_size > raw_len:
                        break
                    value = struct.unpack_from("<I", raw, pos)[0]
                    if (is_valid_reference(value, current_instance_index) and i == 0):
                        alreadyRef = True 
                    if alreadyRef:
                        child_indexes.append(value)
                        ref_names.append(get_type_name(type_registry, instance_infos, value))
                        if nested_refs is not None:
                            nested_refs.add(value)
                    all_values.append(value)
                    pos += element_size
                
                if child_indexes:
                    subfields = []
                    for i, (idx, name) in enumerate(zip(all_values, ref_names)):
                        subfields.append({
                            "name": name,
                            "value": f"Child index: {idx}",
                            "subfields": []
                        })
                    results.append({
                        "name": field_name,
                        "value": f"Reference array ({len(all_values)} items)",
                        "subfields": subfields
                    })
                else:
                    results.append({
                        "name": field_name,
                        "value": f"Array values ({total}): {all_values}",
                        "subfields": []
                    })
                '''data_obj = ArrayData([ObjectRefData(idx) for idx in all_values], "object")
                if scn_file is not None:
                    scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
                continue
            elif ftype in ("string", "resource"):
                for i in range(count):
                    pos = local_align(pos, 4)
                    if pos + 4 > raw_len:
                        break
                    str_length = struct.unpack_from("<I", raw, pos)[0] * 2
                    pos += 4
                    if pos + str_length > raw_len:
                        break
                    segment = raw[pos:pos+str_length]
                    string_value = get_bytes(segment).decode("utf-16-le", errors="replace").rstrip('\x00')
                    children.append(string_value)
                    pos += str_length
                    total += 1
                    
                results.append({
                    "name": field_name,
                    "value": f"Array values ({total}): {children}",
                    "subfields": []
                })
                '''data_obj = ArrayData(children, ftype)
                if scn_file is not None:
                    scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
                    
            else:
                for i in range(count):
                    total += 1
                    if pos + element_size > raw_len:
                        break
                    if element_size == 1:
                        value = struct.unpack_from("<B", raw, pos)[0]
                        children.append(value)
                    elif element_size == 2:
                        value = struct.unpack_from("<H", raw, pos)[0]
                        children.append(value)
                    elif element_size == 4:
                        value = struct.unpack_from("<I", raw, pos)[0]
                        children.append(value)
                    elif element_size == 16:
                        if pos + 16 > raw_len:
                            guid_str = "N/A"
                        else:
                            pos = local_align(pos, field_align)
                            guid_bytes = get_bytes(raw[pos:pos+16])
                            guid_str = guid_le_to_str(guid_bytes)
                        children.append(guid_str)
                    else:
                        value = int.from_bytes(raw[pos:pos+element_size], byteorder="little")
                        children.append(value)
                    pos += element_size

                results.append({
                    "name": field_name,
                    "value": f"Array values ({total}): {children}",
                    "subfields": []
                })
                '''data_obj = ArrayData(children, ftype)
                if scn_file is not None:
                    scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
            if debug:
                print(f"DEBUG: Field '{field_name}' (array) parsed from {field_start:#x} to {pos:#x} (delta {pos - field_start})")
            continue

        elif ftype == "data" and fsize == 4 and field.get("native", False):
            if pos + 4 <= raw_len:
                candidate = struct.unpack_from("<I", raw, pos)[0]                
            else:
                candidate = None
            if not is_valid_reference(candidate, current_instance_index):
                value = str(candidate)
                pos += 4
                results.append({
                    "name": field.get("name", "<unnamed>"),
                    "value": value,
                    "subfields": []
                })
                if debug:
                    print(f"DEBUG: Field '{field.get('name', '<unnamed>')}' (data value) parsed from {pos - 4:#x} to {pos:#x}")
                '''data_obj = U32Data(value=int(value))
                if scn_file is not None:
                    scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
                continue
            pos += 4
            if nested_refs is not None and candidate is not None:
                nested_refs.add(candidate)
            default_ref_name = get_type_name(type_registry, instance_infos, candidate)
            base_default = default_ref_name.split(" (ID:")[0].strip()
            field_name = field.get("name", "<unnamed>").strip()
            ref_name = field_name if field_name == base_default else default_ref_name

            results.append({
                "name": ref_name,
                "value": f"Child index: {candidate}",
                "subfields": []
            })
            if debug:
                print(f"DEBUG: Field '{field_name}' (data-ref) parsed from {pos - 4:#x} to {pos:#x}")
            '''data_obj = ObjectRefData(index=candidate if candidate is not None else -1)
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
            continue        
        elif ftype == "bool":
            if pos + 1 > raw_len:
                value = "N/A"
            else:
                value = "True" if raw[pos] != 0 else "False"
            pos += 1
            
            '''data_obj = BoolData(value=(raw[pos] != 0))
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''

        elif ftype in ("s32", "int"):
            if pos + 4 > raw_len:
                value = "N/A"
            else:
                value = struct.unpack_from("<i", raw, pos)[0]
            pos += 4
            '''data_obj = S32Data(value=value)
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''

        elif ftype in ("u32", "uint"):
            if pos + 4 > raw_len:
                value = "N/A"
            else:
                value = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
            '''data_obj = U32Data(value=value)
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''

        elif ftype in ("f32", "float"):
            if pos + 4 > raw_len:
                value = "N/A"
            else:
                value = struct.unpack_from("<f", raw, pos)[0]
            pos += 4
            '''data_obj = F32Data(value=value)
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
            
        elif ftype in ("string", "resource"):
            size_field_bytes = field.get("size", 4)
            if pos + size_field_bytes > raw_len:
                value = "N/A"
                pos += size_field_bytes
            else:
                #size_val = 0
                #if(ftype == "string" and parent_results == "v2 length"):
                #    print("entered")
                #    size_val = struct.unpack_from("<B", raw, pos-4)[0]
                #else:
                size_val = struct.unpack_from("<I", raw, pos)[0]
                pos += size_field_bytes
                    
                str_byte_count = size_val * 2
                if pos + str_byte_count > raw_len:
                    value = "Truncated String"
                    pos = raw_len
                else:
                    segment = raw[pos:pos+str_byte_count]
                    value = get_bytes(segment).decode("utf-16-le", errors="replace").rstrip('\x00')
                    pos += str_byte_count
            '''data_obj = StringData(value=value)
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''

        elif ftype == "gameobjectref":
            if is_array:
                pos = local_align(pos, 4)
                if pos + 4 > raw_len:
                    value = "[]"
                else:
                    count = struct.unpack_from("<I", raw, pos)[0]
                    pos += 4
                    guids = []
                    for d in range(count):
                        if pos + 16 > raw_len:
                            break
                        pos = local_align(pos, field_align)
                        guid_bytes = get_bytes(raw[pos:pos+16])
                        guid_str = guid_le_to_str(guid_bytes)
                        guids.append(guid_str)
                        pos += 16
                    value = f"[{', '.join(guids)}]"
                #data_obj = ArrayData([GameObjectRefData(guid) for guid in guids], "gameobjectref")
            else:
                if pos + 16 > raw_len:
                    value = "N/A"
                else:
                    pos = local_align(pos, field_align)
                    guid_bytes = get_bytes(raw[pos:pos+16])
                    value = guid_le_to_str(guid_bytes)
                    pos += fsize
                '''data_obj = GameObjectRefData(value)
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''

        elif ftype == "object":
            if is_array:
                pos = local_align(pos, field_align)
                if pos + 4 > raw_len:
                    child_count = 0
                else:
                    child_count = struct.unpack_from("<I", raw, pos)[0]
                pos += 4
                child_indexes = []
                if debug:
                    print(f"DEBUG: Field '{field_name}' (array) child count: {child_count}")
                for i in range(child_count):
                    if pos + 4 > raw_len:
                        idx = None
                    else:
                        idx = struct.unpack_from("<I", raw, pos)[0]
                        child_indexes.append(idx)
                        pos += 4
                if nested_refs is not None:
                    for idx in child_indexes:
                        if idx is not None:
                            nested_refs.add(idx)
                results.append({
                    "name": field_name,
                    "value": f"Child indexes: {child_indexes}",
                    "subfields": []
                })
                if debug:
                    print(f"DEBUG: Field '{field_name}' (object array) parsed from {field_start:#x} to {pos:#x} (delta {pos - field_start})")
                '''data_obj = ArrayData([ObjectRefData(idx) for idx in child_indexes], "object")
                if scn_file is not None:
                    scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
                continue
            else:
                if pos + fsize > raw_len:
                    child_idx = None
                    pos += fsize
                else:
                    child_idx = int.from_bytes(raw[pos:pos+fsize], byteorder="little") 
                    pos += fsize
                if nested_refs is not None and child_idx is not None:
                    nested_refs.add(child_idx)
                results.append({
                    "name": field_name,
                    "value": f"Child index: {child_idx}",
                    "subfields": []
                })
                if debug:
                    print(f"DEBUG: Field '{field_name}' (object) parsed from {field_start:#x} to {pos:#x} (delta {pos - field_start})")
                '''data_obj = ObjectRefData(index=child_idx if child_idx is not None else -1)
                if scn_file is not None:
                    scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
                continue

        elif ftype == "vec3":
            if is_array:
                pos = local_align(pos, 4)
                if pos + 4 > raw_len:
                    value = "[]"
                else:
                    count = struct.unpack_from("<I", raw, pos)[0]
                    pos += 4
                    vectors = []
                    for _ in range(count):
                        pos = local_align(pos, field_align)
                        if pos + 12 > raw_len:
                            break
                        vals = struct.unpack_from("<3f", raw, pos)
                        vectors.append(f"({vals[0]:.6f}, {vals[1]:.6f}, {vals[2]:.6f})")
                        pos += 12
                    value = f"[{', '.join(vectors)}]"
                    if vectors:
                        pos = local_align(pos, field_align)
                #data_obj = ArrayData([Vec3Data(*struct.unpack_from("<3f", raw, pos + i*12)) for i in range(count)], "vec3")
            else:
                if pos + fsize > raw_len:
                    value = "N/A"
                else:
                    vals = struct.unpack_from("<3f", raw, pos)
                    value = f"({vals[0]:.6f}, {vals[1]:.6f}, {vals[2]:.6f})"
                pos += fsize
                pos = local_align(pos, field_align)
                '''data_obj = Vec3Data(*vals) if pos + fsize <= raw_len else Vec3Data(0,0,0)
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''

        elif ftype == "vec4" or (ftype == "data" and fsize == 16):
            if is_array:
                pos = local_align(pos, 4)
                if pos + 4 > raw_len:
                    value = "[]"
                else:
                    count = struct.unpack_from("<I", raw, pos)[0]
                    pos += 4
                    vectors = []
                    for _ in range(count):
                        pos = local_align(pos, field_align)
                        if pos + 16 > raw_len:
                            break
                        vals = struct.unpack_from("<4f", raw, pos)
                        vectors.append(f"({vals[0]:.6f}, {vals[1]:.6f}, {vals[2]:.6f}, {vals[3]:.6f})")
                        pos += 16
                    value = f"[{', '.join(vectors)}]"
                    if vectors:
                        pos = local_align(pos, field_align)
                data_obj = ArrayData([Vec4Data(*struct.unpack_from("<4f", raw, pos + i*16)) for i in range(count)], "vec4")
            else:
                if pos + fsize > raw_len:
                    value = "N/A"
                else:
                    vals = struct.unpack_from("<4f", raw, pos)
                    value = f"({vals[0]:.6f}, {vals[1]:.6f}, {vals[2]:.6f}, {vals[3]:.6f})"
                pos += fsize
                pos = local_align(pos, field_align)
                '''data_obj = Vec4Data(*vals) if pos + fsize <= raw_len else Vec4Data(0,0,0,0)
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''

        elif ftype == "obb" or (ftype == "data" and fsize == 80):
            if is_array:
                pos = local_align(pos, 4)
                if pos + 4 > raw_len:
                    value = "[]"
                else:
                    count = struct.unpack_from("<I", raw, pos)[0]
                    pos += 4
                    obbs = []
                    for _ in range(count):
                        pos = local_align(pos, 16)
                        if pos + 80 > raw_len:
                            break
                        floats = struct.unpack_from("<20f", raw, pos)
                        obbs.append(f"({', '.join(f'{val:.6f}' for val in floats)})")
                        pos += 80
                    value = f"[{', '.join(obbs)}]"
                    if obbs:
                        pos = local_align(pos, field_align)
                data_obj = ArrayData([OBBData(struct.unpack_from("<20f", raw, pos + i*80)) for i in range(count)], "obb")
            else:
                if pos + fsize > raw_len:
                    value = "N/A"
                else:
                    vals = struct.unpack_from("<20f", raw, pos)
                    value = f"({', '.join(f'{v:.6f}' for v in vals)})"
                pos += fsize
                pos = local_align(pos, field_align)
                '''data_obj = OBBData(vals) if pos + fsize <= raw_len else OBBData([])
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''
        else:
            #this msg is for debug purposes: remaining types
            if(ftype != "data" and ftype not in ("rect","uint2","float4","float3", "sphere", "aabb", "capsule", "area", "quaternion","s8","u8", "u64","s16", "color", "vec2", "guid", "range", "mat4", "cylinder", "float2", "size")): print("unkown type", ftype)
            if pos + fsize > raw_len:
                value = "N/A"
            else:
                value = get_bytes(raw[pos:pos+fsize]).hex()
            pos += fsize
            '''data_obj = StringData(value)
            if scn_file is not None:
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj'''

        if "fields" in field and field["fields"]:
            field_type_info = field.get("name", "Unknown")
            nested_results, pos = parse_instance_fields(
                raw=raw,
                offset=pos,
                fields_def=field["fields"],
                type_registry=type_registry,
                instance_infos=instance_infos,
                nested_refs=nested_refs,
                current_instance_index=current_instance_index,
                rsz_userdata_infos=rsz_userdata_infos,
                scn_file=scn_file,
                debug=debug,
                parent_results=field_type_info
            )
            if isinstance(nested_results, list):
                subresults.extend(nested_results)
            else:
                subresults.append(nested_results)

        results.append({"name": field_name, "value": value, "subfields": subresults})
        if debug:
            print(f"DEBUG: Field '{field_name}' parsed from {field_start:#x} to {pos:#x} (delta {pos - field_start})")

        # Store the field result for potential use by subsequent fields
        if isinstance(value, (int, float, str, bool)):
            current_level_fields[field_name] = value

    return results, pos
