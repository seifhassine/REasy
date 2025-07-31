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

class Pfb16ResourceInfo:
    """Special ResourceInfo class for PFB.16 that contains direct string data"""
    def __init__(self, string_value=""):
        self.string_value = string_value 
        self.reserved = 0 

    @property
    def string_offset(self):
        """Compatibility property that emulates string_offset behavior
        Returns a non-zero value if we have a string, 0 otherwise"""
        return 1 if self.string_value else 0
    
    @string_offset.setter
    def string_offset(self, value):
        """Accept setting string_offset for compatibility but don't use the value"""
        pass 

def parse_pfb16_resources(rsz_file, data: bytes) -> int:
    """Parse resources for PFB.16 format where strings are stored directly
    
    This implementation uses a robust byte-by-byte scan to properly locate
    all UTF-16LE encoded resource strings in the file.
    """
    current_offset = rsz_file.header.resource_info_tbl
    
    rsz_file.resource_infos = []
    
    rsz_file._resource_str_map.clear()
    
    rsz_file.is_pfb16 = True
    
    all_strings = []
    
    for i in range(rsz_file.header.resource_count):
        string_start = current_offset
        string_bytes = bytearray()
        
        found_terminator = False
        while current_offset + 1 < len(data):
            byte_pair = data[current_offset:current_offset+2]
            if byte_pair == b'\x00\x00':
                found_terminator = True
                break
                
            string_bytes.extend(byte_pair)
            current_offset += 2
        
        try:
            string_value = string_bytes.decode('utf-16-le')
            all_strings.append(string_value)  
        except UnicodeDecodeError:
            print(f"Failed to decode string {i} at 0x{string_start:X}, bytes: {string_bytes.hex()[:32]}...")
            string_value = f"[Invalid UTF-16 string at 0x{string_start:X}]"
            all_strings.append(string_value)
        
        resource_info = Pfb16ResourceInfo(string_value=string_value)
        
        if resource_info.string_value != string_value:
            print(f"Warning: String value mismatch for resource {i}: '{string_value}' vs '{resource_info.string_value}'")
            resource_info.string_value = string_value  
            
        rsz_file.resource_infos.append(resource_info)
        rsz_file._resource_str_map[resource_info] = string_value
        
        verify_str1 = resource_info.string_value
        verify_str2 = rsz_file._resource_str_map.get(resource_info, "")
        if verify_str1 != string_value or verify_str2 != string_value:
            print(f"Warning: String verification failed for resource {i}")
            print(f"  - Original: '{string_value}'")
            print(f"  - From object: '{verify_str1}'")
            print(f"  - From map: '{verify_str2}'")
        
        if found_terminator:
            current_offset += 2  
    
    final_offset = ((current_offset + 15) & ~15)
    
    setattr(rsz_file, '_pfb16_direct_strings', all_strings)
    
    return final_offset

def build_pfb_16(rsz_file, special_align_enabled = False) -> bytes:
    """Build method specifically for PFB.16 files"""
    rsz_file.header.info_count = len(rsz_file.gameobjects)
    rsz_file.header.resource_count = len(rsz_file.resource_infos)
    rsz_file.header.gameobject_ref_info_count = len(rsz_file.gameobject_ref_infos)
    
    if (rsz_file.rsz_header):
        rsz_file.rsz_header.object_count = len(rsz_file.object_table)
        rsz_file.rsz_header.instance_count = len(rsz_file.instance_infos)
        rsz_file.rsz_header.userdata_count = len(rsz_file.rsz_userdata_infos)
    
    out = bytearray()
    
    out += struct.pack(
        "<4s3I3Q",
        rsz_file.header.signature,
        rsz_file.header.info_count,
        rsz_file.header.resource_count,
        rsz_file.header.gameobject_ref_info_count,
        0,  # gameobject_ref_info_tbl - will update later
        0,  # resource_info_tbl - will update later
        0   # data_offset - will update later
    )

    # 2) Write gameobjects - PFB format is simpler (12 bytes each)
    for go in rsz_file.gameobjects:
        out += struct.pack("<iii", go.id, go.parent_id, go.component_count)
    
    gameobject_ref_info_tbl = len(out)
    for gori in rsz_file.gameobject_ref_infos:
        out += struct.pack("<4i", gori.object_id, gori.property_id, gori.array_index, gori.target_id)
    
    resource_info_tbl = len(out)
    
    # In PFB.16, write string data directly instead of using offsets
    has_direct_strings = hasattr(rsz_file, '_pfb16_direct_strings')
    direct_strings = getattr(rsz_file, '_pfb16_direct_strings', []) if has_direct_strings else []
    
    for i, ri in enumerate(rsz_file.resource_infos):
        resource_string = ""
        
        if has_direct_strings and i < len(direct_strings):
            resource_string = direct_strings[i]
        elif hasattr(ri, 'string_value') and ri.string_value:
            resource_string = ri.string_value
        else:
            resource_string = rsz_file._resource_str_map.get(ri, "")
            
        if resource_string and resource_string.endswith('\0'):
            resource_string = resource_string[:-1]

        string_bytes = resource_string.encode('utf-16-le') + b'\x00\x00'
        out += string_bytes

    # No 16-byte alignment needed before RSZ section in PFB.16
    _ = build_pfb16_rsz_section(rsz_file, out, special_align_enabled)

    header_bytes = struct.pack(
        "<4s3I3Q", 
        rsz_file.header.signature,
        rsz_file.header.info_count,
        rsz_file.header.resource_count,
        rsz_file.header.gameobject_ref_info_count,
        gameobject_ref_info_tbl,
        resource_info_tbl,
        rsz_file.header.data_offset
    )
    out[0:Pfb16Header.SIZE] = header_bytes

    return bytes(out)

def build_pfb16_rsz_section(rsz_file, out: bytearray, special_align_enabled = False) -> int:
    """Build the RSZ section for PFB.16 files with support for embedded RSZ"""
    if special_align_enabled:
        while len(out) % 16 != 0:
            out += b"\x00"
                
    rsz_start = len(out)
    
    rsz_file.header.data_offset = rsz_start
    
    build_scn19_rsz_section(rsz_file, out, rsz_start)
    
    return rsz_start

def parse_pfb16_rsz_userdata(rsz_file, data, skip_data=False):
    """Parse embedded RSZ userdata in PFB.16 files by leveraging SCN.19 implementation"""
    return parse_scn19_rsz_userdata(rsz_file, data, skip_data)

def create_pfb16_resource(path=""):
    """Create a new Pfb16ResourceInfo with the given path"""
    resource = Pfb16ResourceInfo(path)
    return resource
