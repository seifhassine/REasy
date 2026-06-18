import struct
from utils.hex_util import read_wstring 
from file_handlers.rsz.rsz_build_utils import (
    calculate_wstring_offsets,
    encode_wstring,
    pad_to_alignment,
    write_scn_gameobjects,
    write_wstring_entries,
)

class Scn18Header:
    SIZE = 56
    def __init__(self):
        self.signature = b""
        self.info_count = 0
        self.resource_count = 0
        self.folder_count = 0
        self.userdata_count = 0
        self.prefab_count = 0    
        self.folder_tbl = 0
        self.resource_info_tbl = 0
        self.prefab_info_tbl = 0
        self.data_offset = 0
        
    def parse(self, data: bytes):
        if not data or len(data) < self.SIZE:
            raise ValueError(f"Invalid SCN.19 file data: expected at least {self.SIZE} bytes, got {len(data) if data else 0}")
            
        fmt = "<4s5I4Q"
        (self.signature,
         self.info_count,
         self.resource_count,
         self.folder_count,
         self.userdata_count,  
         self.prefab_count,    
         self.folder_tbl,
         self.resource_info_tbl,
         self.prefab_info_tbl,
         self.data_offset) = struct.unpack_from(fmt, data, 0)

def _parse_scn_18_resource_infos(rsz_file):
    rsz_file._resource_str_map.clear()
    for _ in range(rsz_file.header.resource_count):
        from file_handlers.rsz.rsz_file import RszResourceInfo
        ri = RszResourceInfo()
        rsz_file.resource_infos.append(ri)
        s, rsz_file._current_offset = read_wstring(rsz_file.full_data, rsz_file._current_offset, 1000)
        rsz_file.set_resource_string(ri, s)


def build_scn_18(rsz_file, special_align_enabled = False) -> bytes:
    """Build function for SCN.19 files with modified header structure"""
    rsz_file.header.info_count = len(rsz_file.gameobjects)
    rsz_file.header.folder_count = len(rsz_file.folder_infos)
    rsz_file.header.resource_count = len(rsz_file.resource_infos)
    rsz_file.header.userdata_count = 0  # SCN.19 doesn't use userdata_infos
    rsz_file.header.prefab_count = len(rsz_file.prefab_infos)
    
    if (rsz_file.rsz_header):
        rsz_file.rsz_header.object_count = len(rsz_file.object_table)
        rsz_file.rsz_header.instance_count = len(rsz_file.instance_infos)
        
    out = bytearray()
    
    # 1) Write header
    out += struct.pack(
        "<4s5I4Q",
        rsz_file.header.signature,
        rsz_file.header.info_count,
        rsz_file.header.resource_count,
        rsz_file.header.folder_count,
        rsz_file.header.userdata_count,  # userdata before prefab in SCN.19 (value is 0)
        rsz_file.header.prefab_count,
        0,  # folder_tbl - placeholder
        0,  # resource_info_tbl - placeholder
        0,  # prefab_info_tbl - placeholder
        0   # data_offset - placeholder
    )

    # 2) Write gameobjects
    write_scn_gameobjects(out, rsz_file.gameobjects, prefab_before_ukn=True)

    folder_tbl_offset = len(out)
    for fi in rsz_file.folder_infos:
        out += struct.pack("<ii", fi.id, fi.parent_id)

    
        
    prefab_info_tbl_offset = len(out)
    
    for pi in rsz_file.prefab_infos:
        out += struct.pack("<II", 0, pi.parent_id)
    
    strings_start_offset = len(out)
    current_offset = strings_start_offset
    
    new_prefab_offsets, current_offset = calculate_wstring_offsets(
        rsz_file.prefab_infos, rsz_file._prefab_str_map, current_offset
    )
    
    
    for i, pi in enumerate(rsz_file.prefab_infos):
        pi.string_offset = new_prefab_offsets[pi]
        offset = prefab_info_tbl_offset + (i * 8)
        struct.pack_into("<I", out, offset, pi.string_offset)

    for _ in range(16):
        out += b"\x00"
        
    resource_info_tbl_offset = len(out)

    for ri in rsz_file.resource_infos:
        out += encode_wstring(rsz_file._resource_str_map.get(ri, ""))

    write_wstring_entries(out, (new_prefab_offsets, rsz_file._prefab_str_map))

    if rsz_file.rsz_header:
        if special_align_enabled:
            pad_to_alignment(out)
            
        rsz_start = len(out)
        rsz_file.header.data_offset = rsz_start

        from file_handlers.rsz.scn_19.scn_19_structure import build_scn19_rsz_section
        build_scn19_rsz_section(rsz_file, out, rsz_start)

    rsz_file.header.folder_tbl = folder_tbl_offset
    rsz_file.header.resource_info_tbl = resource_info_tbl_offset
    rsz_file.header.prefab_info_tbl = prefab_info_tbl_offset

    header_bytes = struct.pack(
        "<4s5I4Q",
        rsz_file.header.signature,
        rsz_file.header.info_count,
        rsz_file.header.resource_count,
        rsz_file.header.folder_count,
        rsz_file.header.userdata_count, 
        rsz_file.header.prefab_count,
        rsz_file.header.folder_tbl,
        rsz_file.header.resource_info_tbl,
        rsz_file.header.prefab_info_tbl,
        rsz_file.header.data_offset
    )
    out[0:Scn18Header.SIZE] = header_bytes
    
    return bytes(out)
