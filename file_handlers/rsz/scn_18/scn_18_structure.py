import struct
from utils.hex_util import read_wstring 

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

def _parse_scn_18_resource_infos(rsz_file, data):
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
    for go in rsz_file.gameobjects:
        out += go.guid
        out += struct.pack("<i", go.id)
        out += struct.pack("<i", go.parent_id)
        out += struct.pack("<H", go.component_count)
        out += struct.pack("<h", go.prefab_id)
        out += struct.pack("<i", go.ukn) 

    folder_tbl_offset = len(out)
    for fi in rsz_file.folder_infos:
        out += struct.pack("<ii", fi.id, fi.parent_id)

    
        
    prefab_info_tbl_offset = len(out)
    
    for pi in rsz_file.prefab_infos:
        out += struct.pack("<II", 0, pi.parent_id)
    
    strings_start_offset = len(out)
    current_offset = strings_start_offset
    
    new_prefab_offsets = {}
    for pi in rsz_file.prefab_infos:
        prefab_string = rsz_file._prefab_str_map.get(pi, "")
        if prefab_string:
            new_prefab_offsets[pi] = current_offset
            current_offset += len(prefab_string.encode('utf-16-le')) + 2
        else:
            new_prefab_offsets[pi] = 0
    
    
    for i, pi in enumerate(rsz_file.prefab_infos):
        pi.string_offset = new_prefab_offsets[pi]
        offset = prefab_info_tbl_offset + (i * 8)
        struct.pack_into("<I", out, offset, pi.string_offset)

    string_entries = []

    for _ in range(16):
        out += b"\x00"
        
    resource_info_tbl_offset = len(out)

    for ri in rsz_file.resource_infos:
        out += rsz_file._resource_str_map.get(ri, "").encode("utf-16-le") + b"\x00\x00"

    for pi, offset in new_prefab_offsets.items():
        if offset: 
            string_entries.append((offset, rsz_file._prefab_str_map.get(pi, "").encode("utf-16-le") + b"\x00\x00"))
    
    string_entries.sort(key=lambda x: x[0])
    
    current_offset = string_entries[0][0] if string_entries else len(out)
    while len(out) < current_offset:
        out += b"\x00"
        
    for offset, string_data in string_entries:
        while len(out) < offset:
            out += b"\x00"
        out += string_data

    if rsz_file.rsz_header:
        if special_align_enabled:
            while len(out) % 16 != 0:
                out += b"\x00"
            
        rsz_start = len(out)
        rsz_file.header.data_offset = rsz_start

        from file_handlers.rsz.scn_19.scn_19_structure import build_scn19_rsz_section
        build_scn19_rsz_section(rsz_file, out, special_align_enabled, rsz_start)

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
