import struct
from file_handlers.rsz.scn_19.scn_19_structure import (
     parse_scn19_rsz_userdata, 
    build_scn19_rsz_section
)

class Pfb16Header:
    SIZE = 40
    def __init__(self):
        self.signature = b""
        self.info_count = 0
        self.resource_count = 0
        self.gameobject_ref_info_count = 0
        self.gameobject_ref_info_tbl = 0
        self.resource_info_tbl = 0
        self.data_offset = 0
        
    def parse(self, data: bytes):
        fmt = "<4s3I3Q"
        (self.signature,
         self.info_count,
         self.resource_count,
         self.gameobject_ref_info_count,
         self.gameobject_ref_info_tbl,
         self.resource_info_tbl,
         self.data_offset) = struct.unpack_from(fmt, data, 0)

def build_pfb_16(scn_file, special_align_enabled = False) -> bytes:
    """Build method specifically for PFB.16 files"""
    scn_file.header.info_count = len(scn_file.gameobjects)
    scn_file.header.resource_count = len(scn_file.resource_infos)
    scn_file.header.gameobject_ref_info_count = len(scn_file.gameobject_ref_infos)
    
    if (scn_file.rsz_header):
        scn_file.rsz_header.object_count = len(scn_file.object_table)
        scn_file.rsz_header.instance_count = len(scn_file.instance_infos)
        scn_file.rsz_header.userdata_count = len(scn_file.rsz_userdata_infos)
    
    out = bytearray()
    
    out += struct.pack(
        "<4s3I3Q",
        scn_file.header.signature,
        scn_file.header.info_count,
        scn_file.header.resource_count,
        scn_file.header.gameobject_ref_info_count,
        0,  # gameobject_ref_info_tbl - will update later
        0,  # resource_info_tbl - will update later
        0   # data_offset - will update later
    )

    # 2) Write gameobjects - PFB format is simpler (12 bytes each)
    for go in scn_file.gameobjects:
        out += struct.pack("<iii", go.id, go.parent_id, go.component_count)
    
    gameobject_ref_info_tbl = len(out)
    for gori in scn_file.gameobject_ref_infos:
        out += struct.pack("<4i", gori.object_id, gori.property_id, gori.array_index, gori.target_id)
    
    while len(out) % 16 != 0:
        out += b"\x00"
    resource_info_tbl = len(out)
    
    new_resource_offsets = {}
    resource_strings_start = resource_info_tbl + len(scn_file.resource_infos) * 8
    current_offset = resource_strings_start
    
    for ri in scn_file.resource_infos:
        resource_string = scn_file._resource_str_map.get(ri, "")
        if resource_string:
            new_resource_offsets[ri] = current_offset
            current_offset += len(resource_string.encode('utf-16-le')) + 2  # +2 for null terminator
        else:
            new_resource_offsets[ri] = 0
            
    for ri in scn_file.resource_infos:
        ri.string_offset = new_resource_offsets[ri]
        out += struct.pack("<II", ri.string_offset, ri.reserved)

    string_entries = []
    for ri, offset in new_resource_offsets.items():
        if offset: 
            string_entries.append((offset, scn_file._resource_str_map.get(ri, "").encode("utf-16-le") + b"\x00\x00"))
            
    string_entries.sort(key=lambda x: x[0])
    for offset, string_data in string_entries:
        while len(out) < offset:
            out += b"\x00"
        out += string_data

    rsz_start = build_pfb16_rsz_section(scn_file, out, special_align_enabled)

    header_bytes = struct.pack(
        "<4s3I3Q", 
        scn_file.header.signature,
        scn_file.header.info_count,
        scn_file.header.resource_count,
        scn_file.header.gameobject_ref_info_count,
        gameobject_ref_info_tbl,
        resource_info_tbl,
        scn_file.header.data_offset
    )
    out[0:Pfb16Header.SIZE] = header_bytes

    return bytes(out)

def build_pfb16_rsz_section(scn_file, out: bytearray, special_align_enabled = False) -> int:
    """Build the RSZ section for PFB.16 files with support for embedded RSZ"""
    if special_align_enabled:
        while len(out) % 16 != 0:
            out += b"\x00"
                
    rsz_start = len(out)
    
    scn_file.header.data_offset = rsz_start
    
    build_scn19_rsz_section(scn_file, out, special_align_enabled, rsz_start)
    
    return rsz_start

def parse_pfb16_rsz_userdata(scn_file, data):
    """Parse embedded RSZ userdata in PFB.16 files by leveraging SCN.19 implementation"""
    return parse_scn19_rsz_userdata(scn_file, data)
