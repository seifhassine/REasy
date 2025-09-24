import struct
import traceback
from file_handlers.rsz.rsz_data_types import (
    ArrayData,
    StringData,
    ResourceData,
    is_reference_type,
    is_array_type
)
from utils.hex_util import align
from utils.id_manager import EmbeddedIdManager
from utils.hash_util import murmur3_hash  # Added import for murmur3_hash

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
         self.userdata_info_tbl,
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
        self.original_data = None  # Store the original binary data for preservation
        self.modified = False   # Track if this embedded structure was modified
        self.value = ""         
        
        self.parent_userdata_rui = None
        
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

    def mark_modified(self):
        """Mark this structure as modified"""
        self.modified = True

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

def parse_embedded_rsz(rui: Scn19RSZUserDataInfo, type_registry=None, recursion_depth=0, skip_data = False) -> bool:
    """Parse an embedded RSZ block within SCN.19 userdata, with support for recursive embedded structures
    
    Args:
        rui: The Scn19RSZUserDataInfo object containing the data to parse
        type_registry: Optional type registry for parsing instance data
        recursion_depth: Current recursion depth to prevent infinite loops
        visited_blocks: Set of already visited block hashes to prevent cycles
        
    Returns:
        bool: True if parsing was successful
    """
    
    
    # Skip if no data or already visited this exact data block (avoid cycles)
    if not rui.data or len(rui.data) < EmbeddedRSZHeader.SIZE:
        return False
        
    # Store the original data for preservation
    rui.original_data = rui.data
    rui.modified = False
    
    embedded_header = EmbeddedRSZHeader()
    current_offset = embedded_header.parse(rui.data, 0)
    rui.embedded_rsz_header = embedded_header
    
    # Create an embedded ID manager for this structure
    rui.id_manager = EmbeddedIdManager(rui.instance_id)
    
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
        #A complete mini RszFile-like object with the needed fields for _parse_instances
        from file_handlers.rsz.rsz_file import RszFile
        mini_scn = RszFile()
        
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
        
        mini_scn._warm_type_registry_cache()

        # Parse all instances at once using the proper method
        mini_scn._parse_instances(mini_scn.data, skip_data)
        
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
                skip_data
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

def build_scn_19(rsz_file, special_align_enabled = False) -> bytes:
    """Build function for SCN.19 files with modified header structure"""
    rsz_file.header.info_count = len(rsz_file.gameobjects)
    rsz_file.header.folder_count = len(rsz_file.folder_infos)
    rsz_file.header.resource_count = len(rsz_file.resource_infos)
    rsz_file.header.userdata_count = 0  # SCN.19 doesn't use userdata_infos
    rsz_file.header.prefab_count = len(rsz_file.prefab_infos)
    
    if (rsz_file.rsz_header):
        rsz_file.rsz_header.object_count = len(rsz_file.object_table)
        rsz_file.rsz_header.instance_count = len(rsz_file.instance_infos)
        rsz_file.rsz_header.userdata_count = len(rsz_file.rsz_userdata_infos)
        
    out = bytearray()
    
    # 1) Write header - note the changed order of userdata_count and prefab_count
    out += struct.pack(
        "<4s5I5Q",
        rsz_file.header.signature,
        rsz_file.header.info_count,
        rsz_file.header.resource_count,
        rsz_file.header.folder_count,
        rsz_file.header.userdata_count,  # userdata before prefab in SCN.19 (value is 0)
        rsz_file.header.prefab_count,
        0,  # folder_tbl - placeholder
        0,  # resource_info_tbl - placeholder
        0,  # prefab_info_tbl - placeholder
        0,  # userdata_info_tbl - placeholder (will remain 0)
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

    while len(out) % 16 != 0:
        out += b"\x00"
    folder_tbl_offset = len(out)
    for fi in rsz_file.folder_infos:
        out += struct.pack("<ii", fi.id, fi.parent_id)

    while len(out) % 16 != 0:
        out += b"\x00"
    resource_info_tbl_offset = len(out)
    
    for ri in rsz_file.resource_infos:
        out += struct.pack("<II", 0, ri.reserved)
        
    while len(out) % 16 != 0:
        out += b"\x00"
        
    prefab_info_tbl_offset = len(out)
    
    for pi in rsz_file.prefab_infos:
        out += struct.pack("<II", 0, pi.parent_id)
    
    strings_start_offset = len(out)
    current_offset = strings_start_offset
    
    new_resource_offsets = {}
    for ri in rsz_file.resource_infos:
        resource_string = rsz_file._resource_str_map.get(ri, "")
        if resource_string:
            new_resource_offsets[ri] = current_offset
            current_offset += len(resource_string.encode('utf-16-le')) + 2
        else:
            new_resource_offsets[ri] = 0
    
    new_prefab_offsets = {}
    for pi in rsz_file.prefab_infos:
        prefab_string = rsz_file._prefab_str_map.get(pi, "")
        if prefab_string:
            new_prefab_offsets[pi] = current_offset
            current_offset += len(prefab_string.encode('utf-16-le')) + 2
        else:
            new_prefab_offsets[pi] = 0
    
    for i, ri in enumerate(rsz_file.resource_infos):
        ri.string_offset = new_resource_offsets[ri]
        offset = resource_info_tbl_offset + (i * 8)
        struct.pack_into("<I", out, offset, ri.string_offset)
    
    for i, pi in enumerate(rsz_file.prefab_infos):
        pi.string_offset = new_prefab_offsets[pi]
        offset = prefab_info_tbl_offset + (i * 8)
        struct.pack_into("<I", out, offset, pi.string_offset)

    string_entries = []
    
    for ri, offset in new_resource_offsets.items():
        if offset: 
            string_entries.append((offset, rsz_file._resource_str_map.get(ri, "").encode("utf-16-le") + b"\x00\x00"))
            
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

    # Write RSZ header/tables/userdata
    if rsz_file.rsz_header:
        if special_align_enabled:
            while len(out) % 16 != 0:
                out += b"\x00"
            
        rsz_start = len(out)
        rsz_file.header.data_offset = rsz_start

        # Custom RSZ section build for SCN.19
        build_scn19_rsz_section(rsz_file, out, rsz_start)

    # Before rewriting header, update all table offsets:
    rsz_file.header.folder_tbl = folder_tbl_offset
    rsz_file.header.resource_info_tbl = resource_info_tbl_offset
    rsz_file.header.prefab_info_tbl = prefab_info_tbl_offset
    rsz_file.header.userdata_info_tbl = 0  # No userdata in SCN.19

    header_bytes = struct.pack(
        "<4s5I5Q",
        rsz_file.header.signature,
        rsz_file.header.info_count,
        rsz_file.header.resource_count,
        rsz_file.header.folder_count,
        rsz_file.header.userdata_count,  # userdata comes before prefab (value is 0)
        rsz_file.header.prefab_count,
        rsz_file.header.folder_tbl,
        rsz_file.header.resource_info_tbl,
        rsz_file.header.prefab_info_tbl,
        rsz_file.header.userdata_info_tbl,  # Will be 0
        rsz_file.header.data_offset
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
    if hasattr(rui, 'embedded_instances') and rui.embedded_instances:
        recalculate_json_path_hash(rui)
        from file_handlers.rsz.rsz_file import RszFile
        mini_scn = RszFile()
        
        mini_scn.type_registry = type_registry
        mini_scn.rsz_header = rui.embedded_rsz_header
        
        filtered_instance_infos = []
        old_to_new_idx = {}  # Map old indices to new indices
        
        valid_instance_ids = set(rui.embedded_instances.keys())
        
        if hasattr(rui, 'embedded_instance_infos'):
            for old_idx, info in enumerate(rui.embedded_instance_infos):
                # Only keep instance infos that are not None AND have a corresponding entry in embedded_instances
                # This ensures that deleted elements are completely removed
                if info is not None and old_idx in valid_instance_ids:
                    old_to_new_idx[old_idx] = len(filtered_instance_infos)
                    filtered_instance_infos.append(info)
                elif old_idx == 0:  # Always keep the null instance (index 0)
                    old_to_new_idx[0] = 0
                    filtered_instance_infos.append(info)
        
        mini_scn.instance_infos = filtered_instance_infos
        
        updated_object_table = []
        if hasattr(rui, 'embedded_object_table'):
            for ref in rui.embedded_object_table:
                if ref in old_to_new_idx:
                    updated_object_table.append(old_to_new_idx[ref])
                else:
                    updated_object_table.append(0) 
        
        mini_scn.object_table = updated_object_table
        
        updated_elements = {}
        if hasattr(rui, 'embedded_instances'):
            for old_id, fields in rui.embedded_instances.items():
                if old_id in old_to_new_idx:
                    new_id = old_to_new_idx[old_id]
                    updated_elements[new_id] = fields
                elif isinstance(old_id, int) and old_id < len(filtered_instance_infos):
                    updated_elements[old_id] = fields
        
        mini_scn.parsed_elements = updated_elements
        
        mini_scn._array_counters = {}
        
        for instance_id, fields in mini_scn.parsed_elements.items():
            if isinstance(fields, dict):
                for field_name, field_data in fields.items():
                    _update_field_references(field_data, old_to_new_idx)
                    if isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                        array_id = id(field_data)
                        mini_scn._array_counters[array_id] = len(field_data.values)
        
        mini_scn._rsz_userdata_dict = {}
        mini_scn._rsz_userdata_set = set()
        mini_scn.instance_hierarchy = getattr(rui, 'embedded_instance_hierarchy', {})
        
        all_embedded_userdata = rui.embedded_userdata_infos if hasattr(rui, 'embedded_userdata_infos') else []
        non_empty_embedded_userdata = []
        
        for nested_rui in all_embedded_userdata:
            if nested_rui.instance_id in old_to_new_idx:
                nested_rui.instance_id = old_to_new_idx[nested_rui.instance_id]
            
            if hasattr(nested_rui, 'embedded_instances') and nested_rui.embedded_instances:
                nested_rui.data = build_embedded_rsz(nested_rui, type_registry)
            
            has_data = False
            if hasattr(nested_rui, 'data') and nested_rui.data and len(nested_rui.data) > 0:
                has_data = True
            elif hasattr(nested_rui, 'embedded_instances') and nested_rui.embedded_instances:
                has_data = True
            
            if has_data:
                non_empty_embedded_userdata.append(nested_rui)
        
        mini_scn.rsz_userdata_infos = non_empty_embedded_userdata
        mini_scn._rsz_userdata_str_map = {}
        
        out = bytearray()
        
        mini_scn.rsz_header.object_count = len(mini_scn.object_table)
        mini_scn.rsz_header.instance_count = len(mini_scn.instance_infos)
        mini_scn.rsz_header.userdata_count = len(mini_scn.rsz_userdata_infos)
        
        # Build header based on version
        if mini_scn.rsz_header.version >= 4:
            # Version 4+ has userdata_count and reserved
            rsz_header_bytes = struct.pack(
                "<5I I Q Q Q",
                mini_scn.rsz_header.magic,
                mini_scn.rsz_header.version,
                mini_scn.rsz_header.object_count,
                mini_scn.rsz_header.instance_count,
                mini_scn.rsz_header.userdata_count,
                getattr(mini_scn.rsz_header, 'reserved', 0),
                0,  # instance_offset - will update later
                0,  # data_offset - will update later 
                0   # userdata_offset - will update later
            )
        else:
            rsz_header_bytes = struct.pack(
                "<4I Q Q",
                mini_scn.rsz_header.magic,
                mini_scn.rsz_header.version,
                mini_scn.rsz_header.object_count,
                mini_scn.rsz_header.instance_count,
                0,
                0
            )
        out += rsz_header_bytes
        
        # Write object table
        for obj_id in mini_scn.object_table:
            out += struct.pack("<i", obj_id)
        
        # Write instance infos
        instance_offset = len(out)
        for inst in mini_scn.instance_infos:
            out += struct.pack("<II", inst.type_id, inst.crc)
        
        # Align to 16 bytes
        while len(out) % 16 != 0:
            out += b"\x00"
        userdata_offset = len(out)
        
        # Calculate userdata offsets
        userdata_entries_start = len(out)
        
        sorted_rsz_userdata_infos = sorted(mini_scn.rsz_userdata_infos, key=lambda rui: rui.instance_id)
        userdata_data_start = userdata_entries_start + len(sorted_rsz_userdata_infos) * Scn19RSZUserDataInfo.SIZE
        
        userdata_data_start = ((userdata_data_start + 15) & ~15)
        current_data_offset = userdata_data_start
        
        # Write userdata table entries
        for nested_rui in sorted_rsz_userdata_infos:
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
            
            # Align each userdata block to 16 bytes
            current_data_offset += ((len(data_content) + 15) & ~15)
        
        # Pad to start of userdata data
        while len(out) < userdata_data_start:
            out += b"\x00"
        
        # Write userdata content
        for nested_rui in sorted_rsz_userdata_infos:
            data_content = getattr(nested_rui, "data", b"")
            if data_content is None:
                data_content = b""
                
            out += data_content
            
            # Align to 16 bytes
            while len(out) % 16 != 0:
                out += b"\x00"
        
        # Write instance data
        data_offset = len(out)
        try:
            for instance_id, fields in mini_scn.parsed_elements.items():
                if isinstance(fields, dict):
                    for field_name, field_data in fields.items():
                        if isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                            array_id = id(field_data)
                            if array_id in mini_scn._array_counters:
                                expected_count = mini_scn._array_counters[array_id]
                                if expected_count != len(field_data.values):
                                    print(f"Warning: Array count mismatch in {field_name} - expected {expected_count}, got {len(field_data.values)}")
                                    # Force array length to match counter to prevent data corruption
                                    while len(field_data.values) < expected_count:
                                        # Add padding elements if needed
                                        field_data.values.append(field_data.element_class())
            
            instance_data = mini_scn._write_instance_data()
            out += instance_data
        except Exception as e:
            print(f"Error in _write_instance_data: {e}")
            import traceback
            traceback.print_exc()
            return b''
        
        # Update RSZ header with correct offsets
        if mini_scn.rsz_header.version >= 4:
            new_rsz_header = struct.pack(
                "<5I I Q Q Q",
                mini_scn.rsz_header.magic,
                mini_scn.rsz_header.version,
                mini_scn.rsz_header.object_count,
                mini_scn.rsz_header.instance_count,
                mini_scn.rsz_header.userdata_count,
                getattr(mini_scn.rsz_header, 'reserved', 0),
                instance_offset,
                data_offset,
                userdata_offset
            )
            header_size = 48 
        else:
            new_rsz_header = struct.pack(
                "<4I Q Q",
                mini_scn.rsz_header.magic,
                mini_scn.rsz_header.version,
                mini_scn.rsz_header.object_count,
                mini_scn.rsz_header.instance_count,
                instance_offset,
                data_offset
            )
            header_size = 32 
        out[0:header_size] = new_rsz_header
        
        rui.modified = False
        
        return bytes(out)
    
    # Fallback to original data if no instances or structure to rebuild
    return rui.data if hasattr(rui, 'data') and rui.data else b""

def _update_field_references(field_data, old_to_new_idx):
    """Update references in fields to match new indices"""
    if is_reference_type(field_data):
        if field_data.value in old_to_new_idx:
            field_data.value = old_to_new_idx[field_data.value]
    elif is_array_type(field_data):
        field_data.modified = True  
        for element in field_data.values:
            _update_field_references(element, old_to_new_idx)

def build_scn19_rsz_section(rsz_file, out: bytearray, rsz_start: int):
    """Build the RSZ section specifically for SCN.19 format"""
    
    non_empty_userdata_infos = []
    for rui in rsz_file.rsz_userdata_infos:
        # Check if userdata has actual data
        has_data = False
        if hasattr(rui, 'data') and rui.data and len(rui.data) > 0:
            has_data = True
        elif hasattr(rui, 'embedded_instances') and rui.embedded_instances:
            has_data = True
        
        if has_data:
            non_empty_userdata_infos.append(rui)
    
    if rsz_file.rsz_header.version > 3:
        rsz_header_bytes = struct.pack(
            "<5I I Q Q Q",
            rsz_file.rsz_header.magic,
            rsz_file.rsz_header.version,
            rsz_file.rsz_header.object_count,
            len(rsz_file.instance_infos),
            len(non_empty_userdata_infos),  # Use filtered count
            rsz_file.rsz_header.reserved,
            0,  # instance_offset - will update later
            0,  # data_offset - will update later 
            0   # userdata_offset - will update later
        )
        out += rsz_header_bytes
    else:
        rsz_header_bytes = struct.pack(
            "<4I Q Q",
            rsz_file.rsz_header.magic,
            rsz_file.rsz_header.version,
            rsz_file.rsz_header.object_count,
            len(rsz_file.instance_infos),
            0, 
            0,
        )
        out += rsz_header_bytes

    for obj_id in rsz_file.object_table:
        out += struct.pack("<i", obj_id)

    new_instance_offset = len(out) - rsz_start
        
    if rsz_file.rsz_header.version < 4:
        for idx, inst in enumerate(rsz_file.instance_infos):
            out += struct.pack("<II", inst.type_id, inst.crc)

            h = 0

            fields = rsz_file.parsed_elements.get(idx, {})
            if fields:
                first_field_obj = next(iter(fields.values()))
                if isinstance(first_field_obj, StringData) or isinstance(first_field_obj, ResourceData):
                    raw = first_field_obj.value or ""
                    try:
                        if raw.startswith("assets:/"):
                            value_str = raw.strip("\x00")
                            h = murmur3_hash(value_str.encode("utf-16le"))
                    except Exception as e:
                        print(f"Error hashing string data: {e}")
                        h = 0

            out += struct.pack("<I", h)
            out += b"\x00" * 4
    else:
        for inst in rsz_file.instance_infos:
            out += struct.pack("<II", inst.type_id, inst.crc)
        while len(out) % 16 != 0:
            out += b"\x00"

    new_userdata_offset = len(out) - rsz_start

    userdata_entries_start = len(out)
    
    sorted_rsz_userdata_infos = sorted(non_empty_userdata_infos, key=lambda rui: rui.instance_id)
    userdata_data_start = userdata_entries_start + len(sorted_rsz_userdata_infos) * Scn19RSZUserDataInfo.SIZE
    
    userdata_data_start = ((userdata_data_start + 15) & ~15)
    current_data_offset = userdata_data_start

    for rui in sorted_rsz_userdata_infos:
        # Always check for embedded instances and rebuild if needed
        if hasattr(rui, 'embedded_instances') and rui.embedded_instances:
            try:
                rui.data = build_embedded_rsz(rui, rsz_file.type_registry)
            except Exception as e:
                print(f"Error rebuilding embedded RSZ: {str(e)}")
                traceback.print_exc()
                            
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
    
    if rsz_file.rsz_header.version > 3:
        while len(out) < userdata_data_start:
            out += b"\x00"

    for rui in sorted_rsz_userdata_infos:
        data_content = getattr(rui, "data", b"")
        if data_content is None:
            data_content = b""
            
        out += data_content
        
        while len(out) % 16 != 0:
            out += b"\x00"

    new_data_offset = len(out) - rsz_start
    
    if rsz_file.rsz_header.version < 4:
        while len(out) % 4 != 0:
            out += b'\x00'
        rsz_file._write_base_mod = (16 - (len(out) % 16)) % 16

    out += rsz_file._write_instance_data()


    if rsz_file.rsz_header.version > 3:
        new_rsz_header = struct.pack(
            "<5I I Q Q Q",
            rsz_file.rsz_header.magic,
            rsz_file.rsz_header.version,
            rsz_file.rsz_header.object_count,
            len(rsz_file.instance_infos),
            len(non_empty_userdata_infos),
            rsz_file.rsz_header.reserved,
            new_instance_offset,
            new_data_offset,
            new_userdata_offset
        )
        out[rsz_start:rsz_start + rsz_file.rsz_header.SIZE] = new_rsz_header
    else:
        new_rsz_header = struct.pack(
            "<4I Q Q",
            rsz_file.rsz_header.magic,
            rsz_file.rsz_header.version,
            rsz_file.rsz_header.object_count,
            len(rsz_file.instance_infos),
            new_instance_offset,
            new_data_offset,
        )
        out[rsz_start:rsz_start + rsz_file.rsz_header.SIZE] = new_rsz_header

def parse_scn19_rsz_userdata(rsz_file, data, skip_data = False):
    """Parse SCN.19 RSZ userdata entries (24 bytes each with embedded binary data)"""
    
    rsz_file.rsz_userdata_infos = []
    rsz_base_offset = rsz_file.header.data_offset
    current_offset = rsz_file._current_offset
    
    for i in range(rsz_file.rsz_header.userdata_count):
        rui = Scn19RSZUserDataInfo()
        current_offset = rui.parse(data, current_offset)
        rsz_file.rsz_userdata_infos.append(rui)
    
    for i, rui in enumerate(rsz_file.rsz_userdata_infos):
        try:
            if rui.rsz_offset <= 0 or rui.data_size <= 0:
                rui.data = b""
                rsz_file.set_rsz_userdata_string(rui, "Empty UserData")
                continue
            
            abs_data_offset = rsz_base_offset + rui.rsz_offset
            
            magic = 0
            version = 0
            
            if abs_data_offset < rsz_base_offset or abs_data_offset >= len(data):
                rui.data = b""
                rsz_file.set_rsz_userdata_string(rui, "Invalid UserData offset")
                continue
                
            if abs_data_offset + rui.data_size <= len(data):
                rui.data = data[abs_data_offset:abs_data_offset + rui.data_size]
                
                if len(rui.data) >= 8:
                    magic, version = struct.unpack_from("<II", rui.data, 0)
                
                if len(rui.data) >= 48:
                    success = parse_embedded_rsz(rui, rsz_file.type_registry, skip_data)
                    
                    if success:
                        obj_count = len(rui.embedded_object_table)
                        inst_count = len(rui.embedded_instance_infos)
                        parsed_count = len(rui.embedded_instances)
                        
                        desc = f"Embedded RSZ: {obj_count} objects, {inst_count} instances, {parsed_count} parsed"
                        rsz_file.set_rsz_userdata_string(rui, desc)
                    else:
                        # RSZ looked valid but failed to parse
                        rsz_file.set_rsz_userdata_string(rui, f"RSZ parse error (magic: 0x{magic:08X}, ver: {version})")
                else:
                    # Not a valid RSZ block, too small
                    rsz_file.set_rsz_userdata_string(rui, f"Not RSZ data - too small ({rui.data_size} bytes)")
            else:
                rui.data = b""
                rsz_file.set_rsz_userdata_string(rui, "Invalid UserData (out of bounds)")
        except Exception as e:
            rui.data = b""
            rsz_file.set_rsz_userdata_string(rui, f"Error: {str(e)[:50]}...")
    
    # Find the end of userdata section by finding the highest offset + size
    if rsz_file.rsz_userdata_infos:
        try:
            max_end_offset = max(
                rsz_base_offset + rui.rsz_offset + rui.data_size 
                for rui in rsz_file.rsz_userdata_infos
                if rui.rsz_offset > 0 and rui.data_size > 0
            )
            current_offset = align(max_end_offset, 16)
        except ValueError:  
            current_offset = align(current_offset, 16)
    else:
        current_offset = align(current_offset, 16)
        
    return current_offset

def recalculate_json_path_hash(rui):
    if not hasattr(rui, 'embedded_instances') or not rui.embedded_instances:
        return
    root_id = None
    if hasattr(rui, 'embedded_object_table') and rui.embedded_object_table:
        root_id = rui.embedded_object_table[0]
    if root_id is not None and root_id in rui.embedded_instances:
        root_instance = rui.embedded_instances[root_id]
        
        for field_name, field_value in root_instance.items():
            if hasattr(field_value, 'value') and isinstance(field_value.value, str):
                try:
                    value_str = field_value.value.strip("\x00")
                    if value_str:
                        rui.json_path_hash = murmur3_hash(value_str.encode("utf-16le"))
                        return
                except Exception as e:
                    print(f"Error calculating JSON path hash for field '{field_name}': {e}")

