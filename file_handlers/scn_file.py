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
from utils.hex_util import *


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
        self.parsed_elements = {}

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
                nested_refs=self.nested_instance_indexes,
                current_instance_index=idx,
                scn_file=self,
                debug=self.debug
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

def ensure_enough_data(dataLen, offset, size):
    if offset + size > dataLen:
        raise ValueError(f"Insufficient data at offset {offset:#x}")

def parse_instance_fields(
    raw: bytes,
    offset: int, 
    fields_def: list,
    nested_refs=None,
    current_instance_index=None,
    scn_file=None,
    debug=False
):
    """Parse fields from raw data according to field definitions
    
    Args:
        raw: Raw bytes to parse
        offset: Starting offset in raw data
        fields_def: List of field definitions
        nested_refs: Set to track nested references
        current_instance_index: Current instance being parsed
        scn_file: Parent ScnFile instance
        debug: Enable debug logging
"""
    results = []
    pos = offset
    local_align = align_offset
    rsz_userdataInfos = scn_file.rsz_userdata_infos

    raw_len = len(raw)

    def get_bytes(segment):
        return segment.tobytes() if hasattr(segment, "tobytes") else segment

    for field in fields_def:
        field_name = field.get("name", "<unnamed>")
        ftype = field.get("type", "Unknown").lower()
        fsize = field.get("size", 4)
        is_native = field.get("native", False)
        is_array = field.get("array", False)
        rsz_type = get_type_class(field.get("type", "Unknown").lower(), field.get("size", 4), is_native, is_array)
        subresults = []
        data_obj = None
        field_align = int(field["align"]) if "align" in field else 1
        value = None

        if is_array:
            pos = local_align(pos, 4)
            ensure_enough_data(raw_len, pos, 4)
        else:
            pos = local_align(pos, field_align)
    
        if(is_array):
            count = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
            
            if rsz_type == MaybeObject:          
                children = []
                child_indexes = []
                all_values = []
                ref_names = []
                alreadyRef = False
                for i in range(count):
                    ensure_enough_data(raw_len, pos, fsize)
                    value = struct.unpack_from("<I", raw, pos)[0]
                    if (is_valid_reference(value, current_instance_index) and i == 0):
                        alreadyRef = True 
                    if alreadyRef:
                        child_indexes.append(value)
                        ref_names.append(get_type_name(scn_file.type_registry, scn_file.instance_infos, value))
                        nested_refs.add(value)
                    all_values.append(value)
                    pos += fsize
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
                        "value": f"Array values ({all_values.count}): {all_values}",
                        "subfields": []
                    })
                data_obj = ArrayData(list(map(ObjectData, child_indexes)), ObjectData)
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj
                continue

            elif rsz_type == UserDataData:
                userdata_values = []
                userdatas = []
                for _ in range(count):
                    ensure_enough_data(raw_len, pos, 4)
                    candidate = struct.unpack_from("<I", raw, pos)[0]
                    userdatas.append(candidate)
                    pos += 4
                    found = None
                    for rui in rsz_userdataInfos:
                        if rui.instance_id == candidate:
                            found = rui
                            userdata_str = scn_file.get_rsz_userdata_string(found)
                            userdata_values.append(userdata_str)
                            break
                results.append({
                    "name": field_name,
                    "value": f"[{', '.join(userdata_values)}]",
                    "subfields": []
                })
                data_obj = ArrayData(list(map(UserDataData, userdatas)), GameObjectRefData)
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj
                continue
                
            elif rsz_type == GameObjectRefData:
                guids = []
                for d in range(count):
                    ensure_enough_data(raw_len, pos, fsize)
                    pos = local_align(pos, field_align)
                    guid_bytes = get_bytes(raw[pos:pos+fsize])
                    guid_str = guid_le_to_str(guid_bytes)
                    guids.append(guid_str)
                    pos += fsize
                value = f"[{', '.join(guids)}]"
                data_obj = ArrayData(list(map(GameObjectRefData, guids)), GameObjectRefData)
            
            elif rsz_type == ObjectData:
                child_indexes = []
                for i in range(count):
                    ensure_enough_data(raw_len, pos, fsize)
                    idx = struct.unpack_from("<I", raw, pos)[0]
                    child_indexes.append(idx)
                    pos += fsize
                if nested_refs is not None:
                    for idx in child_indexes:
                        if idx is not None:
                            nested_refs.add(idx)
                results.append({
                    "name": field_name,
                    "value": f"Child indexes: {child_indexes}",
                    "subfields": []
                })
                data_obj = ArrayData(list(map(ObjectData,child_indexes)), ObjectData)
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj
                continue
            
            elif rsz_type == Vec3Data:
                vectors = []
                vec3_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    ensure_enough_data(raw_len, pos, 12)
                    vals = struct.unpack_from("<3f", raw, pos)
                    vectors.append(f"({vals[0]:.6f}, {vals[1]:.6f}, {vals[2]:.6f})")
                    vec3_objects.append(Vec3Data(vals[0], vals[1], vals[2]))
                    pos += fsize
                value = f"[{', '.join(vectors)}]"
                if vectors:
                    pos = local_align(pos, field_align)
                data_obj = ArrayData(vec3_objects, Vec3Data)

            elif rsz_type == Vec4Data:
                vectors = []
                vec4_objects = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    ensure_enough_data(raw_len, pos, fsize)
                    vals = struct.unpack_from("<4f", raw, pos)
                    vectors.append(f"({vals[0]:.6f}, {vals[1]:.6f}, {vals[2]:.6f}, {vals[3]:.6f})")
                    vec4_objects.append(Vec4Data(vals[0], vals[1], vals[2], vals[3]))
                    pos += fsize
                value = f"[{', '.join(vectors)}]"
                if vectors:
                    pos = local_align(pos, field_align)
                data_obj = ArrayData(vec4_objects, Vec4Data)

            elif rsz_type == OBBData:
                obbs = []
                for _ in range(count):
                    pos = local_align(pos, field_align)
                    ensure_enough_data (raw_len, pos, fsize)
                    floats = struct.unpack_from("<20f", raw, pos)
                    obbs.append(f"({', '.join(f'{val:.6f}' for val in floats)})")
                    pos += fsize
                value = f"[{', '.join(obbs)}]"
                if obbs:
                    pos = local_align(pos, field_align)
                data_obj = ArrayData(list(map(OBBData,obbs)), OBBData)
            
            elif rsz_type == StringData or rsz_type == ResourceData:
                children = []
                for i in range(count):
                    pos = local_align(pos, 4)
                    ensure_enough_data(raw_len, pos, 4)
                    str_length = struct.unpack_from("<I", raw, pos)[0] * 2
                    pos += 4
                    ensure_enough_data(raw_len, pos, str_length)
                    segment = raw[pos:pos+str_length]
                    string_value = get_bytes(segment).decode("utf-16-le", errors="replace").rstrip('\x00')
                    children.append(string_value)
                    pos += str_length
                    
                results.append({
                    "name": field_name,
                    "value": f"Array values ({children.count}): {children}",
                    "subfields": []
                })
                data_obj = ArrayData(list(map(rsz_type, children)), rsz_type)

            else:
                children = []
                for i in range(count):
                    ensure_enough_data(raw_len, pos, fsize)
                    if fsize == 1:
                        value = struct.unpack_from("<B", raw, pos)[0]
                    elif fsize == 2:
                        value = struct.unpack_from("<H", raw, pos)[0]
                    elif fsize == 4:
                        value = struct.unpack_from("<I", raw, pos)[0]
                    elif fsize == 16:
                        pos = local_align(pos, field_align)
                        guid_bytes = get_bytes(raw[pos:pos+16])
                        value = guid_le_to_str(guid_bytes)
                    else:
                        value = int.from_bytes(raw[pos:pos+fsize], byteorder="little")
                    children.append(value)
                    pos += fsize
                results.append({
                    "name": field_name,
                    "value": f"Array values ({len(children)}): {children}",
                    "subfields": []
                })
                data_obj = ArrayData(list(map(U32Data, children)), U32Data)

            scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj

        else:
            ensure_enough_data(raw_len, pos, 4)

            if rsz_type == MaybeObject:     
                candidate = struct.unpack_from("<I", raw, pos)[0]        
                if not is_valid_reference(candidate, current_instance_index):
                    value = str(candidate)
                    pos += 4
                    results.append({
                        "name": field.get("name", "<unnamed>"),
                        "value": value,
                        "subfields": []
                    })
                    data_obj = U32Data(candidate)
                    scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj
                    continue
                pos += 4
                nested_refs.add(candidate)
                default_ref_name = get_type_name(scn_file.type_registry, scn_file.instance_infos, candidate)
                base_default = default_ref_name.split(" (ID:")[0].strip()
                field_name = field.get("name", "<unnamed>").strip()
                ref_name = field_name if field_name == base_default else default_ref_name

                results.append({
                    "name": ref_name,
                    "value": f"Child index: {candidate}",
                    "subfields": []
                })
                data_obj = GameObjectRefData(candidate)
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj
                continue   

            elif rsz_type == UserDataData:
                instance_id = struct.unpack_from("<I", raw, pos)[0]
                pos += 4
                found = None
                value = str(instance_id)  
                
                if rsz_userdataInfos is not None:
                    if isinstance(rsz_userdataInfos, dict):
                        found = rsz_userdataInfos.get(instance_id)
                        if found and scn_file is not None:
                            value = scn_file.get_rsz_userdata_string(found)
                    else:
                        for rui in rsz_userdataInfos:
                            if rui.instance_id == instance_id:
                                found = rui
                                if scn_file is not None:
                                    value = scn_file.get_rsz_userdata_string(rui)
                                break

                if nested_refs is not None and is_valid_reference(instance_id, current_instance_index): #TODO: handle userdata properly
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
                data_obj = UserDataData(instance_id)
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj
                continue 
            
            elif rsz_type == GameObjectRefData:
                guid_bytes = get_bytes(raw[pos:pos+fsize])
                value = guid_le_to_str(guid_bytes)
                data_obj = GameObjectRefData(value) 
                pos += fsize

                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj

            elif rsz_type == ObjectData:
                child_idx = int.from_bytes(raw[pos:pos+fsize], byteorder="little") 
                pos += fsize
                if nested_refs is not None and child_idx is not None:
                    nested_refs.add(child_idx)
                results.append({
                    "name": field_name,
                    "value": f"Child index: {child_idx}",
                    "subfields": []
                })
                data_obj = ObjectData(child_idx)
                scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name] = data_obj
                continue

            elif rsz_type == Vec3Data:
                vals = struct.unpack_from("<3f", raw, pos)
                value = f"({vals[0]:.6f}, {vals[1]:.6f}, {vals[2]:.6f})"
                pos += fsize
                pos = local_align(pos, field_align)
                data_obj = Vec3Data(*vals)
        
            elif rsz_type == Vec4Data:
                vals = struct.unpack_from("<4f", raw, pos)
                value = f"({vals[0]:.6f}, {vals[1]:.6f}, {vals[2]:.6f}, {vals[3]:.6f})"         
                data_obj = Vec4Data(*vals)
                pos += fsize
                pos = local_align(pos, field_align)
        
            elif rsz_type == OBBData:
                vals = struct.unpack_from("<20f", raw, pos)
                value = f"({', '.join(f'{v:.6f}' for v in vals)})"
                data_obj = OBBData(vals)  
                pos += fsize
                pos = local_align(pos, field_align)
            
            elif rsz_type == StringData or rsz_type == ResourceData:
                count = struct.unpack_from("<I", raw, pos)[0]
                pos += 4
                str_byte_count = count * 2
                ensure_enough_data(raw_len, pos, str_byte_count)
                segment = raw[pos:pos+str_byte_count]
                value = get_bytes(segment).decode("utf-16-le", errors="replace").rstrip('\x00')
                pos += str_byte_count
                data_obj = StringData(value)

            elif rsz_type == BoolData:
                value = raw[pos] 
                data_obj = BoolData(value)
                pos += fsize

            elif rsz_type == S32Data or rsz_type == U32Data:
                fmt = "<i" if rsz_type == S32Data else "<I"
                value = struct.unpack_from(fmt, raw, pos)[0]
                pos += fsize
                data_obj = rsz_type(value)

            elif rsz_type == F32Data:
                value = struct.unpack_from("<f", raw, pos)[0]
                pos += fsize
                data_obj = F32Data(value)
                
            else:
                #for debug purposes: remaining types
                if(ftype != "data" and ftype not in ("rect","uint2","float4","float3", "sphere", "aabb", "capsule", "area", "quaternion","s8","u8", "u64","s16", "color", "vec2", "guid", "range", "mat4", "cylinder", "float2", "size")): print("unkown type", ftype)
                ensure_enough_data(raw_len, pos, fsize)
                value = get_bytes(raw[pos:pos+fsize]).hex()
                pos += fsize
                data_obj = StringData(value)

            scn_file.parsed_elements.setdefault(current_instance_index, {})[field_name]

        if "fields" in field and field["fields"]:
            nested_results, pos = parse_instance_fields(
                raw=raw,
                offset=pos,
                fields_def=field["fields"],
                nested_refs=nested_refs,
                current_instance_index=current_instance_index,
                scn_file=scn_file,
                debug=debug
            )
            if isinstance(nested_results, list):
                subresults.extend(nested_results)
            else:
                subresults.append(nested_results)

        results.append({"name": field_name, "value": value, "subfields": subresults})

    return results, pos
