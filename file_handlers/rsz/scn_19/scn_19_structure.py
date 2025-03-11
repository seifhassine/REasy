import struct
from file_handlers.rsz.rsz_data_types import *

class Scn19Header:
    SIZE = 64
    def __init__(self):
        self.signature = b""
        self.info_count = 0
        self.resource_count = 0
        self.folder_count = 0
        self.userdata_count = 0  # Note: SCN.19 doesn't actually have userdata but the field exists
        self.prefab_count = 0    
        self.folder_tbl = 0
        self.resource_info_tbl = 0
        self.prefab_info_tbl = 0
        self.userdata_info_tbl = 0
        self.data_offset = 0
        
    def parse(self, data: bytes):
        if not data or len(data) < self.SIZE:
            raise ValueError(f"Invalid SCN.19 file data: expected at least {self.SIZE} bytes, got {len(data) if data else 0}")
            
        fmt = "<4s5I5Q"
        (self.signature,
         self.info_count,
         self.resource_count,
         self.folder_count,
         self.userdata_count,  # Order is changed - userdata before prefab
         self.prefab_count,    
         self.folder_tbl,
         self.resource_info_tbl,
         self.prefab_info_tbl,
         self.userdata_info_tbl,  # This value might be 0 or invalid
         self.data_offset) = struct.unpack_from(fmt, data, 0)

# Special RSZUserDataInfo class for SCN.19 format
class Scn19RSZUserDataInfo:
    SIZE = 24  # 24 bytes per entry in SCN.19
    def __init__(self):
        self.instance_id = 0    # 4 bytes: which instance this userdata is associated with
        self.type_id = 0        # 4 bytes: type ID
        self.json_path_hash = 0 # 4 bytes: hash of the JSON path
        self.data_size = 0      # 4 bytes: size of the userdata block
        self.rsz_offset = 0     # 8 bytes: offset to the data from RSZ start (uint64)
        self.data = None        # The actual binary userdata content
        
        # Fields for embedded RSZ data
        self.embedded_rsz_header = None      # Parsed RSZ header
        self.embedded_object_table = []      # Object table entries
        self.embedded_instance_infos = []    # Instance infos
        self.embedded_userdata_infos = []    # RSZ userdata infos (if any)
        self.embedded_instances = {}         # Parsed instances
        
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated RSZUserData info at 0x{offset:X}")
        (self.instance_id, 
         self.type_id,
         self.json_path_hash, 
         self.data_size, 
         self.rsz_offset) = struct.unpack_from("<4IQ", data, offset)  # Parse as 4 uint32 + 1 uint64
        return offset + self.SIZE

class EmbeddedRSZHeader:
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
        fmt = "<5I I Q Q Q"  # 5 uint32s, 1 uint32 reserved, 3 uint64s
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

class EmbeddedInstanceInfo:
    SIZE = 8
    def __init__(self):
        self.type_id = 0
        self.crc = 0
        
    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated embedded instance info at 0x{offset:X}")
        self.type_id, self.crc = struct.unpack_from("<II", data, offset)
        return offset + self.SIZE

def parse_embedded_rsz(rui: Scn19RSZUserDataInfo, type_registry=None, recursion_depth=0, visited_blocks=None) -> bool:
    """Parse an embedded RSZ block within SCN.19 userdata, with support for recursive embedded structures
    
    Args:
        rui: The Scn19RSZUserDataInfo object containing the data to parse
        type_registry: Optional type registry for parsing instance data
        recursion_depth: Current recursion depth to prevent infinite loops
        visited_blocks: Set of already visited block hashes to prevent cycles
        
    Returns:
        bool: True if parsing was successful
    """
    
    if visited_blocks is None:
        visited_blocks = set()
    
    # Skip if no data or already visited this exact data block (avoid cycles)
    if not rui.data or len(rui.data) < EmbeddedRSZHeader.SIZE:
        return False
        
    data_hash = hash(rui.data)
    if data_hash in visited_blocks:
        return False
    visited_blocks.add(data_hash)
    
    try:
        embedded_header = EmbeddedRSZHeader()
        current_offset = embedded_header.parse(rui.data, 0)
        rui.embedded_rsz_header = embedded_header
        
        if embedded_header.object_count == 0:
            return False
            
        embedded_data_len = len(rui.data)
        if (embedded_header.instance_offset >= embedded_data_len or
            embedded_header.data_offset >= embedded_data_len or
            embedded_header.userdata_offset >= embedded_data_len):
            return False
            
        object_table_size = embedded_header.object_count * 4
        if current_offset + object_table_size > embedded_data_len:
            return False
            
        rui.embedded_object_table = list(struct.unpack_from(f"<{embedded_header.object_count}i", rui.data, current_offset))
        
        instance_offset = embedded_header.instance_offset
        
        for i in range(embedded_header.instance_count):
            if instance_offset + EmbeddedInstanceInfo.SIZE > embedded_data_len:
                break
                
            inst_info = EmbeddedInstanceInfo()
            instance_offset = inst_info.parse(rui.data, instance_offset)
            rui.embedded_instance_infos.append(inst_info)
        
        # Parse instance data if data_offset is valid
        if embedded_header.data_offset < embedded_data_len:
            #A complete mini ScnFile-like object with the needed fields for _parse_instances
            from file_handlers.rsz.rsz_file import ScnFile
            mini_scn = ScnFile()
            
            mini_scn.type_registry = type_registry
            mini_scn.rsz_header = embedded_header
            mini_scn.object_table = rui.embedded_object_table
            mini_scn.instance_infos = rui.embedded_instance_infos
            mini_scn._gameobject_instance_ids = set()
            mini_scn._folder_instance_ids = set()
            mini_scn._rsz_userdata_dict = {}
            mini_scn._rsz_userdata_set = set()
            mini_scn.parsed_elements = {}
            mini_scn.instance_hierarchy = {} 
            
            # For SCN.19 format, we need to handle embedded userdata as binary blocks, not string references
            mini_scn.rsz_userdata_infos = []
            mini_scn._rsz_userdata_str_map = {}
            
            # Parse RSZ userdata if any exists in the embedded structure
            if embedded_header.userdata_count > 0 and embedded_header.userdata_offset < embedded_data_len:
                # For SCN.19 format, userdata entries are Scn19RSZUserDataInfo objects (24 bytes each)
                userdata_offset = embedded_header.userdata_offset
                
                # Parse all embedded userdata entries
                for ud_idx in range(embedded_header.userdata_count):
                        
                    embedded_rui = Scn19RSZUserDataInfo()
                    userdata_offset = embedded_rui.parse(rui.data, userdata_offset)
                    mini_scn.rsz_userdata_infos.append(embedded_rui)
                    
                    userdata_desc = f"Embedded Binary Data (ID: {embedded_rui.instance_id}, Size: {embedded_rui.data_size})"
                    mini_scn._rsz_userdata_str_map[embedded_rui] = userdata_desc
                    
                    # Establish parent-child relationship for this userdata entry
                    if embedded_rui.instance_id not in mini_scn.instance_hierarchy:
                        mini_scn.instance_hierarchy[embedded_rui.instance_id] = {"children": [], "parent": rui.instance_id}
                    
                    # Calculate the absolute offset within this RSZ block
                    abs_embedded_offset = embedded_rui.rsz_offset
                    
                    embedded_rui.data = rui.data[abs_embedded_offset:abs_embedded_offset + embedded_rui.data_size]
            
                # Initialize userdata lookup structures
                mini_scn._rsz_userdata_dict = {rui.instance_id: rui for rui in mini_scn.rsz_userdata_infos}
                mini_scn._rsz_userdata_set = set(mini_scn._rsz_userdata_dict.keys())
            
            # Use the data portion for instance parsing
            mini_scn.data = rui.data[embedded_header.data_offset:]
            
            # Parse all instances at once using the proper method
            mini_scn._parse_instances(mini_scn.data)
            
            # Store a reference to the parent userdata_rui for context
            mini_scn.parent_userdata_rui = rui
            
            # Copy parsed elements to the current UserData object
            rui.embedded_instances = mini_scn.parsed_elements
            
            # Process any embedded userdata structures found (recursive step)
            rui.embedded_userdata_infos = mini_scn.rsz_userdata_infos
            
            # Register this mini_scn object in our registry
            domain = f"emb_{rui.instance_id}"
            
            # Also store a reverse lookup from mini_scn to parent
            mini_scn.domain = domain
            
            # Recursively parse any nested embedded RSZ userdata blocks
            for nested_idx, embedded_rui in enumerate(rui.embedded_userdata_infos):
                # Recursively parse this nested RSZ structure
                nested_success = parse_embedded_rsz(
                    embedded_rui, 
                    type_registry, 
                    recursion_depth + 1,
                    visited_blocks
                )
                
                # If successfully parsed, add it to the embedded_instances
                if nested_success:
                    rui.embedded_instances[embedded_rui.instance_id] = {
                        "embedded_rsz": embedded_rui
                    }
                    # ensure parent node in the hierarchy
                    if rui.instance_id not in mini_scn.instance_hierarchy:
                        mini_scn.instance_hierarchy[rui.instance_id] = {"children": []}
                    mini_scn.instance_hierarchy[rui.instance_id]["children"].append(embedded_rui.instance_id)
        
        return True
        
    except Exception as e:
        return False

def build_scn_19(scn_file, special_align_enabled = False) -> bytes:
    """Build function for SCN.19 files with modified header structure"""
    scn_file.header.info_count = len(scn_file.gameobjects)
    scn_file.header.folder_count = len(scn_file.folder_infos)
    scn_file.header.resource_count = len(scn_file.resource_infos)
    scn_file.header.userdata_count = 0  # SCN.19 doesn't use userdata_infos
    scn_file.header.prefab_count = len(scn_file.prefab_infos)
    
    if (scn_file.rsz_header):
        scn_file.rsz_header.object_count = len(scn_file.object_table)
        scn_file.rsz_header.instance_count = len(scn_file.instance_infos)
        scn_file.rsz_header.userdata_count = len(scn_file.rsz_userdata_infos)
        
    out = bytearray()
    
    # 1) Write header - note the changed order of userdata_count and prefab_count
    out += struct.pack(
        "<4s5I5Q",
        scn_file.header.signature,
        scn_file.header.info_count,
        scn_file.header.resource_count,
        scn_file.header.folder_count,
        scn_file.header.userdata_count,  # userdata before prefab in SCN.19 (value is 0)
        scn_file.header.prefab_count,
        0,  # folder_tbl - placeholder
        0,  # resource_info_tbl - placeholder
        0,  # prefab_info_tbl - placeholder
        0,  # userdata_info_tbl - placeholder (will remain 0)
        0   # data_offset - placeholder
    )

    # 2) Write gameobjects
    for go in scn_file.gameobjects:
        out += go.guid
        out += struct.pack("<i", go.id)
        out += struct.pack("<i", go.parent_id)
        out += struct.pack("<H", go.component_count)
        out += struct.pack("<H", go.ukn)
        out += struct.pack("<i", go.prefab_id) 

    while len(out) % 16 != 0:
        out += b"\x00"
    folder_tbl_offset = len(out)
    for fi in scn_file.folder_infos:
        out += struct.pack("<ii", fi.id, fi.parent_id)

    while len(out) % 16 != 0:
        out += b"\x00"
    resource_info_tbl_offset = len(out)
    
    resource_strings_offset = 0
    new_resource_offsets = {}
    current_offset = resource_info_tbl_offset + len(scn_file.resource_infos) * 8
    
    current_offset = scn_file._align(current_offset, 16)
    
    # Skip prefab infos table (No userdata in SCN.19)
    current_offset += len(scn_file.prefab_infos) * 8
    
    current_offset = scn_file._align(current_offset, 16)
    
    resource_strings_offset = current_offset
    
    for ri in scn_file.resource_infos:
        resource_string = scn_file._resource_str_map.get(ri, "")
        if resource_string:
            new_resource_offsets[ri] = resource_strings_offset
            resource_strings_offset += len(resource_string.encode('utf-16-le')) + 2
        else:
            new_resource_offsets[ri] = 0
    
    prefab_strings_offset = resource_strings_offset
    new_prefab_offsets = {}
    
    for pi in scn_file.prefab_infos:
        prefab_string = scn_file._prefab_str_map.get(pi, "")
        if prefab_string:
            new_prefab_offsets[pi] = prefab_strings_offset
            prefab_strings_offset += len(prefab_string.encode('utf-16-le')) + 2
        else:
            new_prefab_offsets[pi] = 0
    
    for ri in scn_file.resource_infos:
        ri.string_offset = new_resource_offsets[ri]
        out += struct.pack("<II", ri.string_offset, ri.reserved)

    # 5) Align and write prefab infos with updated offsets (skip userdata for SCN.19)
    while len(out) % 16 != 0:
        out += b"\x00"
    prefab_info_tbl_offset = len(out)
    for pi in scn_file.prefab_infos:
        pi.string_offset = new_prefab_offsets[pi]
        out += struct.pack("<II", pi.string_offset, pi.parent_id)

    string_entries = []
    
    for ri, offset in new_resource_offsets.items():
        if offset:
            string_entries.append((offset, scn_file._resource_str_map.get(ri, "").encode("utf-16-le") + b"\x00\x00"))
            
    for pi, offset in new_prefab_offsets.items():
        if offset: 
            string_entries.append((offset, scn_file._prefab_str_map.get(pi, "").encode("utf-16-le") + b"\x00\x00"))
    
    string_entries.sort(key=lambda x: x[0])
    
    current_offset = string_entries[0][0] if string_entries else len(out)
    while len(out) < current_offset:
        out += b"\x00"
        
    for offset, string_data in string_entries:
        while len(out) < offset:
            out += b"\x00"
        out += string_data

    # Write RSZ header/tables/userdata
    if scn_file.rsz_header:
        if special_align_enabled:
            while len(out) % 16 != 0:
                out += b"\x00"
            
        rsz_start = len(out)
        scn_file.header.data_offset = rsz_start

        # Custom RSZ section build for SCN.19
        build_scn19_rsz_section(scn_file, out, special_align_enabled, rsz_start)

    # Before rewriting header, update all table offsets:
    scn_file.header.folder_tbl = folder_tbl_offset
    scn_file.header.resource_info_tbl = resource_info_tbl_offset
    scn_file.header.prefab_info_tbl = prefab_info_tbl_offset
    scn_file.header.userdata_info_tbl = 0  # No userdata in SCN.19

    header_bytes = struct.pack(
        "<4s5I5Q",
        scn_file.header.signature,
        scn_file.header.info_count,
        scn_file.header.resource_count,
        scn_file.header.folder_count,
        scn_file.header.userdata_count,  # userdata comes before prefab (value is 0)
        scn_file.header.prefab_count,
        scn_file.header.folder_tbl,
        scn_file.header.resource_info_tbl,
        scn_file.header.prefab_info_tbl,
        scn_file.header.userdata_info_tbl,  # Will be 0
        scn_file.header.data_offset
    )
    out[0:Scn19Header.SIZE] = header_bytes
    
    return bytes(out)

def build_embedded_rsz(rui, type_registry=None):
    """Build embedded RSZ data from parsed instances into binary format
    
    Args:
        rui: The Scn19RSZUserDataInfo object containing embedded RSZ data
        type_registry: Optional type registry for handling types
        
    Returns:
        bytes: The built binary data
    """
    if not hasattr(rui, 'embedded_rsz_header') or not rui.embedded_rsz_header:
        return rui.data
        
    if not hasattr(rui, 'embedded_instances') or not rui.embedded_instances:
        return rui.data
    
    from file_handlers.rsz.rsz_file import ScnFile
    mini_scn = ScnFile()
    
    # Copy all embedded RSZ information to the mini scn
    mini_scn.type_registry = type_registry
    mini_scn.rsz_header = rui.embedded_rsz_header
    mini_scn.object_table = rui.embedded_object_table if hasattr(rui, 'embedded_object_table') else []
    mini_scn.instance_infos = rui.embedded_instance_infos if hasattr(rui, 'embedded_instance_infos') else []
    mini_scn.parsed_elements = rui.embedded_instances.copy()
    mini_scn._rsz_userdata_dict = {}
    mini_scn._rsz_userdata_set = set()
    mini_scn.instance_hierarchy = getattr(rui, 'embedded_instance_hierarchy', {})
    
    mini_scn.rsz_userdata_infos = rui.embedded_userdata_infos if hasattr(rui, 'embedded_userdata_infos') else []
    mini_scn._rsz_userdata_str_map = {}
    
    for nested_rui in mini_scn.rsz_userdata_infos:
        if hasattr(nested_rui, 'embedded_instances') and nested_rui.embedded_instances:
            nested_rui.data = build_embedded_rsz(nested_rui, type_registry)
    
    out = bytearray()
    
    rsz_header_bytes = struct.pack(
        "<5I I Q Q Q",
        mini_scn.rsz_header.magic,
        mini_scn.rsz_header.version,
        mini_scn.rsz_header.object_count,
        len(mini_scn.instance_infos),
        len(mini_scn.rsz_userdata_infos),
        mini_scn.rsz_header.reserved,
        0,  # instance_offset - will update later
        0,  # data_offset - will update later 
        0   # userdata_offset - will update later
    )
    out += rsz_header_bytes
    
    for obj_id in mini_scn.object_table:
        out += struct.pack("<i", obj_id)
    
    instance_offset = len(out)
    for inst in mini_scn.instance_infos:
        out += struct.pack("<II", inst.type_id, inst.crc)
    
    while len(out) % 16 != 0:
        out += b"\x00"
    userdata_offset = len(out)
    
    userdata_entries_start = len(out)
    userdata_data_start = userdata_entries_start + len(mini_scn.rsz_userdata_infos) * Scn19RSZUserDataInfo.SIZE
    
    userdata_data_start = ((userdata_data_start + 15) & ~15)
    current_data_offset = userdata_data_start
    
    for nested_rui in mini_scn.rsz_userdata_infos:
        data_content = getattr(nested_rui, "data", b"")
        if data_content is None:
            data_content = b""
        
        rel_data_offset = current_data_offset - 0  
        
        out += struct.pack("<4IQ", 
            nested_rui.instance_id,
            getattr(nested_rui, "type_id", 0),
            getattr(nested_rui, "json_path_hash", 0),
            len(data_content),
            rel_data_offset
        )
        
        current_data_offset += ((len(data_content) + 15) & ~15)
    
    while len(out) < userdata_data_start:
        out += b"\x00"
    
    for nested_rui in mini_scn.rsz_userdata_infos:
        data_content = getattr(nested_rui, "data", b"")
        if data_content is None:
            data_content = b""
            
        out += data_content
        
        while len(out) % 16 != 0:
            out += b"\x00"
    
    data_offset = len(out)
    
    instance_data = mini_scn._write_instance_data()
    out += instance_data
    
    new_rsz_header = struct.pack(
        "<5I I Q Q Q",
        mini_scn.rsz_header.magic,
        mini_scn.rsz_header.version,
        mini_scn.rsz_header.object_count,
        len(mini_scn.instance_infos),
        len(mini_scn.rsz_userdata_infos),
        mini_scn.rsz_header.reserved,
        instance_offset,
        data_offset,
        userdata_offset
    )
    out[0:mini_scn.rsz_header.SIZE] = new_rsz_header
    
    return bytes(out)

def build_scn19_rsz_section(scn_file, out: bytearray, special_align_enabled: bool, rsz_start: int):
    """Build the RSZ section specifically for SCN.19 format"""
    rsz_header_bytes = struct.pack(
        "<5I I Q Q Q",
        scn_file.rsz_header.magic,
        scn_file.rsz_header.version,
        scn_file.rsz_header.object_count,
        len(scn_file.instance_infos),
        len(scn_file.rsz_userdata_infos),
        scn_file.rsz_header.reserved,
        0,  # instance_offset - will update later
        0,  # data_offset - will update later 
        0   # userdata_offset - will update later
    )
    out += rsz_header_bytes

    for obj_id in scn_file.object_table:
        out += struct.pack("<i", obj_id)

    new_instance_offset = len(out) - rsz_start
    for inst in scn_file.instance_infos:
        out += struct.pack("<II", inst.type_id, inst.crc)

    while len(out) % 16 != 0:
        out += b"\x00"
    new_userdata_offset = len(out) - rsz_start

    userdata_entries_start = len(out)
    userdata_data_start = userdata_entries_start + len(scn_file.rsz_userdata_infos) * Scn19RSZUserDataInfo.SIZE
    
    userdata_data_start = ((userdata_data_start + 15) & ~15)
    current_data_offset = userdata_data_start
    
    for rui in scn_file.rsz_userdata_infos:
        if hasattr(rui, 'embedded_instances') and rui.embedded_instances:
            rui.data = build_embedded_rsz(rui, scn_file.type_registry)
            
        data_content = getattr(rui, "data", b"")
        if data_content is None:
            data_content = b""
        
        rel_data_offset = current_data_offset - rsz_start
        
        out += struct.pack("<4IQ", 
            rui.instance_id,
            getattr(rui, "type_id", 0),
            getattr(rui, "json_path_hash", 0),
            len(data_content),  # Original data size
            rel_data_offset     # RSZ-relative offset (64-bit)
        )
        
        current_data_offset += ((len(data_content) + 15) & ~15)
    
    while len(out) < userdata_data_start:
        out += b"\x00"
    
    for rui in scn_file.rsz_userdata_infos:
        data_content = getattr(rui, "data", b"")
        if data_content is None:
            data_content = b""
            
        out += data_content
        
        while len(out) % 16 != 0:
            out += b"\x00"

    new_data_offset = len(out) - rsz_start
    
    instance_data = scn_file._write_instance_data()
    out += instance_data

    new_rsz_header = struct.pack(
        "<5I I Q Q Q",
        scn_file.rsz_header.magic,
        scn_file.rsz_header.version,
        scn_file.rsz_header.object_count,
        len(scn_file.instance_infos),
        len(scn_file.rsz_userdata_infos),
        scn_file.rsz_header.reserved,
        new_instance_offset,
        new_data_offset,
        new_userdata_offset
    )
    out[rsz_start:rsz_start + scn_file.rsz_header.SIZE] = new_rsz_header

def parse_scn19_rsz_userdata(scn_file, data):
    """Parse SCN.19 RSZ userdata entries (24 bytes each with embedded binary data)"""
    
    scn_file.rsz_userdata_infos = []
    rsz_base_offset = scn_file.header.data_offset
    current_offset = scn_file._current_offset
    
    for i in range(scn_file.rsz_header.userdata_count):
        rui = Scn19RSZUserDataInfo()
        current_offset = rui.parse(data, current_offset)
        scn_file.rsz_userdata_infos.append(rui)
    
    valid_blocks = 0
    failed_blocks = 0
    
    for i, rui in enumerate(scn_file.rsz_userdata_infos):
        try:
            if rui.rsz_offset <= 0 or rui.data_size <= 0:
                rui.data = b""
                scn_file.set_rsz_userdata_string(rui, "Empty UserData")
                continue
            
            abs_data_offset = rsz_base_offset + rui.rsz_offset
            
            magic = 0
            version = 0
            
            if abs_data_offset < rsz_base_offset or abs_data_offset >= len(data):
                rui.data = b""
                scn_file.set_rsz_userdata_string(rui, "Invalid UserData offset")
                failed_blocks += 1
                continue
                
            if abs_data_offset + rui.data_size <= len(data):
                rui.data = data[abs_data_offset:abs_data_offset + rui.data_size]
                
                if len(rui.data) >= 8:
                    magic, version = struct.unpack_from("<II", rui.data, 0)
                
                if len(rui.data) >= 48:
                    success = parse_embedded_rsz(rui, scn_file.type_registry)
                    
                    if success:
                        obj_count = len(rui.embedded_object_table)
                        inst_count = len(rui.embedded_instance_infos)
                        parsed_count = len(rui.embedded_instances)
                        
                        desc = f"Embedded RSZ: {obj_count} objects, {inst_count} instances, {parsed_count} parsed"
                        scn_file.set_rsz_userdata_string(rui, desc)
                        valid_blocks += 1
                    else:
                        # RSZ looked valid but failed to parse
                        scn_file.set_rsz_userdata_string(rui, f"RSZ parse error (magic: 0x{magic:08X}, ver: {version})")
                        failed_blocks += 1
                else:
                    # Not a valid RSZ block, too small
                    scn_file.set_rsz_userdata_string(rui, f"Not RSZ data - too small ({rui.data_size} bytes)")
                    failed_blocks += 1
            else:
                rui.data = b""
                scn_file.set_rsz_userdata_string(rui, "Invalid UserData (out of bounds)")
                failed_blocks += 1
        except Exception as e:
            rui.data = b""
            scn_file.set_rsz_userdata_string(rui, f"Error: {str(e)[:50]}...")
            failed_blocks += 1
    
    # Find the end of userdata section by finding the highest offset + size
    if scn_file.rsz_userdata_infos:
        try:
            max_end_offset = max(
                rsz_base_offset + rui.rsz_offset + rui.data_size 
                for rui in scn_file.rsz_userdata_infos
                if rui.rsz_offset > 0 and rui.data_size > 0
            )
            current_offset = scn_file._align(max_end_offset, 16)
        except ValueError:  
            current_offset = scn_file._align(current_offset, 16)
    else:
        current_offset = scn_file._align(current_offset, 16)
        
    return current_offset
