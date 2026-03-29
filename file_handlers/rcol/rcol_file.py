from typing import List, Optional, Union
import struct
from .base_model import FileHandler
from .rcol_structures import Header, RcolGroup, RCOL_MAGIC, calc_hash
from .request_set import RequestSet, RequestSetInfo, IgnoreTag
from file_handlers.rsz.rsz_file import RszFile as RSZFile
from io import BytesIO

class RcolFile:
    """Main RCOL file handler"""
    
    MAGIC = RCOL_MAGIC
    EXTENSION = ".rcol"
    REQUEST_SET_INFO_SIZE = 48
    GUID_SIZE = 16
    GROUP_INFO_SIZE = 80
    SHAPE_RECORD_SIZE = 160
    
    def __init__(self, option=None, file_handler: Optional[FileHandler] = None):
        self.header = Header()
        self.rsz = None
        self.type_registry = None
        self.user_data_bytes = b''
        self.groups: List[RcolGroup] = []
        self.request_sets: List[RequestSet] = []
        self.ignore_tags: Optional[List[IgnoreTag]] = None
        self.auto_generate_joint_descs: Optional[List[str]] = None
        self.auto_generate_joint_entry_meta: List[dict] = []
        self.auto_generate_joint_entry_size = 8
        self.option = option
        self.file_handler = file_handler
        self.file_version = 25
        self._source_file_size = 0
        
    def get_rsz(self) -> Optional[RSZFile]:
        """Get the RSZ file instance"""
        return self.rsz
    

        
    def read(self, data: Union[bytes, bytearray], file_version: int = 0, file_path: str = "") -> bool:
        """Read RCOL file from bytes"""
        self.file_version = file_version if file_version > 0 else 25
        if self.file_version < 10 or self.file_version == 38:
            raise ValueError(f"Unsupported RCOL version {self.file_version}; rcol 2 and 38 are not supported")
        handler = FileHandler(data, file_version=self.file_version, file_path=file_path)
        return self.do_read(handler)
        
    def do_read(self, handler: FileHandler) -> bool:
        """Read RCOL file"""
        self.groups.clear()
        self.request_sets.clear()
        if self.ignore_tags:
            self.ignore_tags.clear()
        if self.auto_generate_joint_descs:
            self.auto_generate_joint_descs.clear()
        self.auto_generate_joint_entry_meta.clear()
        self.auto_generate_joint_entry_size = 8
        self._source_file_size = len(handler.data)
            
        if len(handler.data) < 100:
            raise ValueError(f"File too small ({len(handler.data)} bytes) to be a valid RCOL file")
            
        header = self.header
        if not header.read(handler):
            return False
            
        if header.magic != self.MAGIC:
            raise ValueError(f"{handler.file_path} Not a RCOL file (magic: 0x{header.magic:08X}, expected: 0x{self.MAGIC:08X})")
            
        if header.num_groups > 0:
            read_groups = False
            
            if header.groups_ptr_offset > 0:
                if header.groups_ptr_offset >= len(handler.data):
                    print(f"groups_ptr_offset ({header.groups_ptr_offset}) is beyond file size ({len(handler.data)})")
                else:
                    handler.seek(header.groups_ptr_offset)
                    read_groups = True
            else:
                print(f"Have {header.num_groups} groups but offset is 0 (version {handler.file_version})")
                
            if read_groups:
                for i in range(header.num_groups):
                    group = RcolGroup()
                    group.read_info(handler)
                    self.groups.append(group)
                for group in self.groups:
                    group.read(handler)
                    
        self.read_rsz(handler, header.data_offset)
        
        # Read request sets
        if header.num_request_sets > 0 and header.request_set_offset > 0:
            if header.request_set_offset >= len(handler.data):
                print(f"request_set_offset ({header.request_set_offset}) is beyond file size")
            else:
                handler.seek(header.request_set_offset)
                for i in range(header.num_request_sets):
                    try:
                        request_set_info = RequestSetInfo()
                        if not request_set_info.read(handler):
                            print(f"Failed to read request set {i}")
                            break
                            
                        request_set = RequestSet(i, request_set_info)
                        self.request_sets.append(request_set)
                    except Exception as e:
                        print(f"Error reading request set {i}: {e}")
                        break
                
                # For v10+, read shape userdata GUIDs AFTER all RequestSetInfo structures
                # The GUIDs come as a block after all the RequestSetInfo, not interleaved
                if handler.file_version >= 10 and header.num_request_sets > 0:
                    request_set_info_end, guid_block_end = self._get_request_set_guid_block_bounds(handler, header)
                    guid_count = max(0, (guid_block_end - request_set_info_end) // self.GUID_SIZE)
                    all_guids = []
                    if guid_count > 0:
                        handler.seek(request_set_info_end)
                        for _ in range(guid_count):
                            all_guids.append(handler.read_bytes(self.GUID_SIZE))
                    
                    #  Distribute the GUIDs among request sets (round-robin distribution)
                    for i, guid_bytes in enumerate(all_guids):
                        req_idx = i % len(self.request_sets)
                        self.request_sets[req_idx].shape_userdata.append(guid_bytes)
                    
        elif header.num_request_sets > 0:
            print(f"Have {header.num_request_sets} request sets but offset is 0")
                
        # Read ignore tags
        if header.num_ignore_tags > 0 and header.ignore_tag_offset > 0:
            if header.ignore_tag_offset >= len(handler.data):
                print(f"ignore_tag_offset ({header.ignore_tag_offset}) is beyond file size")
            else:
                handler.seek(header.ignore_tag_offset)
                self.ignore_tags = self.ignore_tags or []
                for i in range(header.num_ignore_tags):
                    try:
                        ignore_tag = IgnoreTag()
                        ignore_tag.read(handler)
                        self.ignore_tags.append(ignore_tag)
                    except Exception as e:
                        print(f"Error reading ignore tag {i}: {e}")
                        break
                
        # Read auto generate joints
        if header.num_auto_generate_joints > 0 and header.auto_generate_joint_desc_offset > 0:
            if header.auto_generate_joint_desc_offset >= len(handler.data):
                print(f"auto_generate_joint_desc_offset ({header.auto_generate_joint_desc_offset}) is beyond file size")
            else:
                self.auto_generate_joint_descs = self.auto_generate_joint_descs or []
                # rcol.20+ stores auto-generate-joint entries as fixed 0x40-byte structs.
                # Older variants use a compact pointer entry.
                self.auto_generate_joint_entry_size = 64 if handler.file_version >= 20 else 8
                for i in range(header.num_auto_generate_joints):
                    try:
                        entry_offset = header.auto_generate_joint_desc_offset + (i * self.auto_generate_joint_entry_size)
                        handler.seek(entry_offset)
                        joint_name = handler.read_offset_wstring()
                        if self.auto_generate_joint_entry_size == 64:
                            entry_meta = {
                                "name_hash": handler.read_uint32(),
                                "parent_index": handler.read_int16(),
                                "symmetry_index": handler.read_int16(),
                                "quaternion": handler.read('<4f'),
                                "position": handler.read('<3f'),
                                "scale": handler.read('<3f'),
                                "segment_scale": handler.read_uint32(),
                                "padding": handler.read_uint32(),
                            }
                            self.auto_generate_joint_entry_meta.append(entry_meta)
                        else:
                            self.auto_generate_joint_entry_meta.append({})
                        self.auto_generate_joint_descs.append(joint_name)
                    except Exception as e:
                        print(f"Error reading auto generate joint {i}: {e}")
                        break
                
        self.setup_references(handler.file_version)
        
        return True

    def _get_request_set_guid_block_bounds(self, handler: FileHandler, header: Header) -> tuple[int, int]:
        """Return (start, end) bounds for the request-set GUID block."""
        request_set_info_end = header.request_set_offset + (header.num_request_sets * self.REQUEST_SET_INFO_SIZE)
        data = handler.data
        data_len = len(data)
        guid_block_end = data_len

        def read_i64(offset: int) -> Optional[int]:
            if offset < 0 or offset + 8 > data_len:
                return None
            return struct.unpack_from('<q', data, offset)[0]

        def add_candidate(offset: int):
            nonlocal guid_block_end
            if offset is None:
                return
            if request_set_info_end <= offset < guid_block_end:
                guid_block_end = offset

        # Header-level section starts that can follow the GUID block.
        add_candidate(header.ignore_tag_offset)
        add_candidate(header.auto_generate_joint_desc_offset)
        add_candidate(header.resourceInfoTbl)
        add_candidate(header.userDataInfoTbl)

        # Group and shape string pointers.
        for group_index in range(header.num_groups):
            if guid_block_end == request_set_info_end:
                break
            group_info_offset = header.groups_ptr_offset + (group_index * self.GROUP_INFO_SIZE)            
            add_candidate(read_i64(group_info_offset + 16))   # group name pointer
            add_candidate(read_i64(group_info_offset + 56))   # group mask GUID payload pointer

            num_shapes_field_offset = group_info_offset + (28 if handler.file_version >= 25 else 32)
            num_shapes = (
                struct.unpack_from('<i', data, num_shapes_field_offset)[0]
                if num_shapes_field_offset + 4 <= data_len else 0
            )
            shapes_offset = read_i64(group_info_offset + 40) or 0
            for shape_index in range(max(0, num_shapes)):
                if guid_block_end == request_set_info_end:
                    break
                shape_info_offset = shapes_offset + (shape_index * self.SHAPE_RECORD_SIZE)
                add_candidate(read_i64(shape_info_offset + 16))
                add_candidate(read_i64(shape_info_offset + 48))
                add_candidate(read_i64(shape_info_offset + 56))

        # Request set string pointers.
        request_name_rel = 24 if handler.file_version >= 25 else 16
        for request_index in range(header.num_request_sets):
            if guid_block_end == request_set_info_end:
                break
            request_info_offset = header.request_set_offset + (request_index * self.REQUEST_SET_INFO_SIZE)
            for relative in (request_name_rel, 32):
                add_candidate(read_i64(request_info_offset + relative))

        if guid_block_end < request_set_info_end:
            guid_block_end = request_set_info_end

        guid_block_size = guid_block_end - request_set_info_end
        guid_block_end -= (guid_block_size % self.GUID_SIZE)
        return request_set_info_end, guid_block_end
        
    def do_write(self, handler: FileHandler) -> bool:
        """Write RCOL file"""
        handler.clear()
        
        header = Header()
        
        header.status = self.header.status
        header.ukn_re3_a = self.header.ukn_re3_a
        header.ukn_re3_b = self.header.ukn_re3_b
        header.numResourceInfos = self.header.numResourceInfos
        header.numUserDataInfos = self.header.numUserDataInfos
        header.num_user_data = self.header.num_user_data
        header.ukn_re3_tbl = self.header.ukn_re3_tbl
        header.ukn_count = self.header.ukn_count
            
        header.num_request_sets = len(self.request_sets)
        header.num_groups = len(self.groups)
        header.num_ignore_tags = len(self.ignore_tags) if self.ignore_tags else 0
            
        header.num_shapes = 0
        
        # For v25, this field might be repurposed for something else
        if self.request_sets:
            header.max_request_set_id = (
                max(s.info.field0 for s in self.request_sets)
                if handler.file_version >= 20
                else max(s.info.id for s in self.request_sets)
            )
        else:
            header.max_request_set_id = 0 if handler.file_version >= 18 else -1
        
        # For older than v25, num_user_data doesn't seem to matter at all for runtime. 
        # Haven't checked for v25+.
        if handler.file_version >= 25:
            header.num_user_data = sum(len(self.groups[rs.info.group_index].shapes) for rs in self.request_sets)
            
        header.write(handler)
        handler.align(16)
        

        # v10 preserves insertion-order string table emission (no sorting by offset slot).
        use_custom_string_flush = handler.file_version >= 25
        original_string_table_flush = handler.string_table_flush
        preseed_first_string_offsets = {}

        if handler.file_version < 25:
            legacy_strings = []

            for group in self.groups:
                if group.info.name is not None:
                    legacy_strings.append(group.info.name)
                for shape in group.shapes:
                    if shape.info.name is not None:
                        legacy_strings.append(shape.info.name)
                    legacy_strings.append(shape.info.primary_joint_name_str or "")
                    legacy_strings.append(shape.info.secondary_joint_name_str or "")

            for req in self.request_sets:
                if req.info.name is not None:
                    legacy_strings.append(req.info.name)
                if req.info.key_name is not None:
                    legacy_strings.append(req.info.key_name or "")

            def legacy_string_flush():
                string_refs = {}
                for offset_pos, string in handler.string_table_offsets:
                    if string not in string_refs:
                        string_refs[string] = []
                    string_refs[string].append(offset_pos)

                first_offset_by_string = dict(preseed_first_string_offsets)
                for string in legacy_strings:
                    write_pos_for_this_string = handler.tell
                    handler.write_wstring(string)
                    if string not in first_offset_by_string:
                        first_offset_by_string[string] = write_pos_for_this_string

                # Update references for strings emitted by the v10 ordering.
                for string, offset in first_offset_by_string.items():
                    if string in string_refs:
                        for offset_pos in string_refs[string]:
                            handler.write_at(offset_pos, '<q', offset)

                preseed_first_string_offsets.clear()
                preseed_first_string_offsets.update(first_offset_by_string)
                handler.string_table_offsets.clear()
                handler.string_table_contexts.clear()

            handler.string_table_flush = legacy_string_flush
        elif use_custom_string_flush:
            # custom handler that collects strings in the right order
            
            # 1: Collect strings with their context
            # This helps us know which strings are shape names vs joint names
            class StringOrderCollector:
                def __init__(self):
                    self.strings = []  # List of (context, string) tuples
                    self.buffer = BytesIO()
                    
                def collect_string(self, s, context='other'):
                    """Collect a string with its context"""
                    if s is not None:
                        self.strings.append((context, s))
            
            collector = StringOrderCollector()
            
            # Collect strings by simulating writes in the desired order
            # First collect group and shape names
            for group in self.groups:
                # Collect group strings
                if group.info.name is not None:
                    collector.collect_string(group.info.name, 'group_name')
                
                # Collect regular shape names for this group
                for shape in group.shapes:
                    if shape.info.name is not None:
                        collector.collect_string(shape.info.name, 'shape_name')
                                        
                # And mirror shape names
                for shape in group.extra_shapes:
                    if shape.info.name is not None:
                        collector.collect_string(shape.info.name, 'mirror_shape_name')
            
            # 2: collect cmat paths (v28+) before joint names
            for group in self.groups:
                for shape in [*group.shapes, *group.extra_shapes]:
                    if shape.info.cmat_path:
                        collector.collect_string(shape.info.cmat_path, 'cmat')

            # 3: collect joint names
            for group in self.groups:
                for shape in [*group.shapes, *group.extra_shapes]:
                    collector.collect_string(shape.info.primary_joint_name_str or "", 'joint_name')
                    collector.collect_string(shape.info.secondary_joint_name_str or "", 'joint_name')
            
            # 4: request set strings
            for req in self.request_sets:
                if req.info.name is not None:
                    collector.collect_string(req.info.name, 'request_name')
                if req.info.key_name is not None:
                    collector.collect_string(req.info.key_name or "", 'request_key')
            
            # 5: build string priority map
            string_priority = {}
            for i, (context, s) in enumerate(collector.strings):
                if s not in string_priority:
                    string_priority[s] = i
            
            # 6: override the handler's string comparison to use our priority
            def custom_string_flush():
                # Build a map of string -> list of offset positions that reference it
                string_refs = {}
                use_joint_empty_override = handler.file_version >= 27
                use_joint_name_context_override = handler.file_version >= 27
                joint_empty_ref_positions = []
                request_key_empty_ref_positions = []
                joint_ref_positions_by_string = {}
                for offset_pos, string in handler.string_table_offsets:
                    if string not in string_refs:
                        string_refs[string] = []
                    string_refs[string].append(offset_pos)
                    context = handler.string_table_contexts.get(offset_pos)
                    if use_joint_name_context_override and context == "joint_name":
                        if string not in joint_ref_positions_by_string:
                            joint_ref_positions_by_string[string] = []
                        joint_ref_positions_by_string[string].append(offset_pos)
                    if use_joint_empty_override and string == "":
                        if context == "joint_name":
                            joint_empty_ref_positions.append(offset_pos)
                        elif context == "request_key":
                            request_key_empty_ref_positions.append(offset_pos)
                
                # Track first occurrence of each string for offset sharing.
                # For empty strings, always use the first empty emitted by this flush
                # (e.g. primary/secondary joint names), not any preseeded empty offset.
                string_first_offset = dict(preseed_first_string_offsets)
                
                # Track which strings have been written in joint context for deduplication
                joint_names_written = set()  # Tracks strings written in joint context
                # Track empty strings for joint_name context
                joint_empty_written = False
                joint_empty_offset = None
                first_empty_offset_by_context = {}
                # Note: request_key strings are NOT deduplicated, so no tracking needed
                
                # Write strings in the order collected
                for context, string in collector.strings:
                    if string == "":
                        if context == 'shape_name':
                            # Empty shape names are written - track the first one
                            if "" not in string_first_offset:
                                string_first_offset[""] = handler.tell
                            handler.write_wstring(string)
                        elif context == 'joint_name':
                            # Write empty joint name only once (deduplicate within joint context)
                            if not joint_empty_written:
                                joint_empty_written = True
                                joint_empty_offset = handler.tell
                                if "" not in string_first_offset:
                                    string_first_offset[""] = handler.tell
                                handler.write_wstring(string)
                        elif context == 'request_key':
                            # Request keys are NOT deduplicated - write every empty occurrence
                            offset = handler.tell
                            handler.write_wstring(string)
                            # Update string_first_offset only if not already set
                            if "" not in string_first_offset:
                                string_first_offset[""] = offset
                            if "request_key" not in first_empty_offset_by_context:
                                first_empty_offset_by_context["request_key"] = offset
                        elif context == 'request_name':
                            # Request names are NOT deduplicated - write every empty occurrence
                            offset = handler.tell
                            handler.write_wstring(string)
                            # Update string_first_offset only if not already set
                            if "" not in string_first_offset:
                                string_first_offset[""] = offset
                        # Skip other empty strings (group names, etc.)
                    elif context == 'joint_name':
                        # Non-empty joint names SHOULD be deduplicated within joint context only
                        if string not in joint_names_written:
                            # First occurrence of this joint name - write it
                            joint_names_written.add(string)
                            offset = handler.tell
                            handler.write_wstring(string)
                            if use_joint_name_context_override:
                                # Keep the global first-occurrence mapping for non-joint contexts.
                                if string not in string_first_offset:
                                    string_first_offset[string] = offset
                                # Joint-name references should resolve to the first joint-context occurrence,
                                # not to an earlier occurrence from other contexts
                                for offset_pos in joint_ref_positions_by_string.get(string, []):
                                    handler.write_at(offset_pos, '<q', offset)
                            else:
                                # Legacy behavior for < v27
                                if string not in string_first_offset:
                                    string_first_offset[string] = offset
                                    if string in string_refs:
                                        for offset_pos in string_refs[string]:
                                            handler.write_at(offset_pos, '<q', offset)
                        # Skip subsequent occurrences of non-empty joint names - they're deduplicated within context
                    elif context == 'shape_name':
                        # Shape names (including empty ones) should NOT be deduplicated - write every occurrence
                        if string not in string_first_offset:
                            # First occurrence - remember offset for references
                            string_first_offset[string] = handler.tell
                            handler.write_wstring(string)
                            # Update all references to point to this first occurrence
                            if string in string_refs:
                                for offset_pos in string_refs[string]:
                                    handler.write_at(offset_pos, '<q', string_first_offset[string])
                        else:
                            # Subsequent occurrence - write it again (no deduplication)
                            handler.write_wstring(string)
                            # References already point to the first occurrence
                    elif context == 'request_key':
                        # Request keys are NOT deduplicated - write every occurrence
                        offset = handler.tell
                        handler.write_wstring(string)
                        # Update references only if this is the first time we see this string globally
                        if string not in string_first_offset:
                            string_first_offset[string] = offset
                            if string in string_refs:
                                for offset_pos in string_refs[string]:
                                    handler.write_at(offset_pos, '<q', offset)
                    elif context == 'cmat':
                        # cmat paths are deduplicated like default offset strings.
                        if string not in string_first_offset:
                            offset = handler.tell
                            string_first_offset[string] = offset
                            handler.write_wstring(string)
                            if string in string_refs:
                                for offset_pos in string_refs[string]:
                                    handler.write_at(offset_pos, '<q', offset)
                    elif context == 'mirror_shape_name':
                        if string not in string_first_offset:
                            offset = handler.tell
                            string_first_offset[string] = offset
                            handler.write_wstring(string)
                        if string in string_refs:
                            for offset_pos in string_refs[string]:
                                handler.write_at(offset_pos, '<q', string_first_offset[string])
                    else:
                        # Other strings (group names, request names, etc.)
                        # Default behavior: write each occurrence (no deduplication)
                        if string not in string_first_offset:
                            # First occurrence - write it and remember offset
                            string_first_offset[string] = handler.tell
                            handler.write_wstring(string)
                            # Update all references to point to this first occurrence
                            if string in string_refs:
                                for offset_pos in string_refs[string]:
                                    handler.write_at(offset_pos, '<q', string_first_offset[string])
                        else:
                            # Subsequent occurrence - write it again (no deduplication)
                            handler.write_wstring(string)
                            # References still point to the first occurrence
                
                # Ensure empty-string references use the first known empty string offset
                if "" in string_refs and "" in string_first_offset:
                    for offset_pos in string_refs[""]:
                        handler.write_at(offset_pos, '<q', string_first_offset[""])

                if use_joint_empty_override and joint_empty_ref_positions and joint_empty_offset is not None:
                    for offset_pos in joint_empty_ref_positions:
                        handler.write_at(offset_pos, '<q', joint_empty_offset)
                request_key_empty_offset = first_empty_offset_by_context.get("request_key")
                if use_joint_empty_override and request_key_empty_ref_positions and request_key_empty_offset is not None:
                    for offset_pos in request_key_empty_ref_positions:
                        current_offset = handler.read_at(offset_pos, '<q')
                        if current_offset > request_key_empty_offset:
                            handler.write_at(offset_pos, '<q', request_key_empty_offset)
                
                preseed_first_string_offsets.clear()
                preseed_first_string_offsets.update(string_first_offset)
                handler.string_table_offsets.clear()
                handler.string_table_contexts.clear()
            
            handler.string_table_flush = custom_string_flush
        
        # Write groups
        # For v25, groups are written directly at groups_ptr_offset
        header.groups_ptr_offset = handler.tell
        
        # Write GroupInfo structures (they must be sequential)
        for group in self.groups:
            group.write_info(handler)
        
        # Write group shape data (also sequential)
        for group in self.groups:
            group.write(handler)
            header.num_shapes += len(group.shapes)
            
        # Write RSZ data
        handler.align(16)
        header.data_offset = handler.tell
        self.write_rsz(handler)
        if self.user_data_bytes:
            header.user_data_size = len(self.user_data_bytes)
        elif self.rsz:
            header.user_data_size = len(self.rsz.full_data)
        else:
            header.user_data_size = 0
            
        # Write request sets
        handler.align(16)
        header.num_request_sets = len(self.request_sets)
        header.request_set_offset = handler.tell
        for item in self.request_sets:
            item.info.write(handler)
            # Only update max_request_set_id for non-v25 files
            if handler.file_version < 25 and item.info.id > header.max_request_set_id:
                header.max_request_set_id = item.info.id
        
        # For v10+, write all shape userdata GUIDs AFTER all RequestSetInfo structures
        request_guid_block_start = handler.tell
        request_guid_blob = b""
        if handler.file_version >= 10:
            # Collect all GUIDs and write them in the correct order
            # We need to reverse the round-robin distribution to get sequential order
            all_guids = []
            max_guids_per_rs = max(
                (len(rs.shape_userdata) for rs in self.request_sets),
                default=0,
            )
            
            # Reconstruct the original order from round-robin distribution
            for round_num in range(max_guids_per_rs):
                for rs in self.request_sets:
                    if round_num < len(rs.shape_userdata):
                        all_guids.append(rs.shape_userdata[round_num])
            
            # Write all GUIDs in sequential order
            guid_blob = bytearray()
            for guid in all_guids:
                if isinstance(guid, bytes) and len(guid) == 16:
                    handler.write_bytes(guid)
                    guid_blob.extend(guid)
            request_guid_blob = bytes(guid_blob)

        # Group mask GUID blobs can exist with or without request sets.
        for group in self.groups:
            mask_count = max(0, group.info.num_mask_guids)
            if mask_count == 0:
                continue

            mask_guids = (group.info.mask_guids or [])[:mask_count]
            payload_bytes = b"".join(mask_guids)
            if len(payload_bytes) != len(mask_guids) * self.GUID_SIZE:
                continue

            # Prefer dynamic reuse inside the freshly-written request GUID block
            # when this mask payload exists there.
            target_offset = None
            if request_guid_blob:
                idx = request_guid_blob.find(payload_bytes)
                if idx != -1:
                    target_offset = request_guid_block_start + idx

            # Fallback: emit payload at current cursor.
            if target_offset is None:
                target_offset = handler.tell
                handler.write_bytes(payload_bytes)

            group.info.mask_guids_offset = target_offset
            if group.info.mask_guids_offset_start > 0:
                handler.write_int64_at(group.info.mask_guids_offset_start, target_offset)
                            
        # Prepare ignore tags
        # They will be written with their own string table after the main string table
        handler.align(16)
        header.num_ignore_tags = len(self.ignore_tags) if self.ignore_tags else 0
        ignore_tag_data_offset = handler.tell
        
        ignore_tag_string_offsets = []
        auto_joint_string_offsets = []
        if self.ignore_tags:
            for tag in self.ignore_tags:
                offset_pos = handler.tell
                handler.write_int64(0)  # Placeholder for string offset
                ignore_tag_string_offsets.append((offset_pos, tag.tag))
                handler.write_uint32(tag.hash)
                handler.write_int32(0)
                
        # Write auto generate joints
        header.num_auto_generate_joints = len(self.auto_generate_joint_descs) if self.auto_generate_joint_descs else 0
        if self.auto_generate_joint_descs:
            target_auto_offset = handler.tell
            header.auto_generate_joint_desc_offset = target_auto_offset

            if target_auto_offset > len(handler.data):
                handler.write_bytes(b"\x00" * (target_auto_offset - len(handler.data)))

            with handler.seek_temp(target_auto_offset):
                for index, item in enumerate(self.auto_generate_joint_descs):
                    offset_pos = handler.tell
                    handler.write_int64(0)
                    auto_joint_string_offsets.append((offset_pos, item))

                    if self.auto_generate_joint_entry_size == 64 and index < len(self.auto_generate_joint_entry_meta):
                        entry_meta = self.auto_generate_joint_entry_meta[index] or {}
                        handler.write_uint32(calc_hash(item or ""))
                        handler.write_int16(entry_meta.get("parent_index", -1))
                        handler.write_int16(entry_meta.get("symmetry_index", -1))
                        handler.write('<4f', *(entry_meta.get("quaternion") or (0.0, 0.0, 0.0, 1.0)))
                        handler.write('<3f', *(entry_meta.get("position") or (0.0, 0.0, 0.0)))
                        handler.write('<3f', *(entry_meta.get("scale") or (1.0, 1.0, 1.0)))
                        handler.write_uint32(entry_meta.get("segment_scale", 0))
                        handler.write_uint32(entry_meta.get("padding", 0))

            total_auto_size = len(self.auto_generate_joint_descs) * max(8, self.auto_generate_joint_entry_size)
            handler.seek(max(handler.tell, target_auto_offset + total_auto_size))
        else:
            # When no auto generate joints, these offsets should point to where string table will be
            # We'll update them after string table flush
            header.auto_generate_joint_desc_offset = 0
            
        header.resourceInfoTbl = 0 
        header.userDataInfoTbl = 0 
                
        # Auto-joint strings should be emitted before the main string table.
        auto_joint_strings_start = None
        if auto_joint_string_offsets:
            auto_joint_strings_start = handler.tell
            for offset_pos, joint_string in auto_joint_string_offsets:
                normalized_joint_string = joint_string or ""
                string_offset = handler.tell
                handler.write_wstring(normalized_joint_string)

                # Always point references to the first occurrence, including empty strings.
                ref_offset = preseed_first_string_offsets.setdefault(
                    normalized_joint_string,
                    string_offset,
                )
                handler.write_at(offset_pos, '<q', ref_offset)
                
        string_table_start = handler.tell
        first_string_section_start = auto_joint_strings_start if auto_joint_strings_start is not None else string_table_start
        handler.string_table_flush()
        handler.offset_content_table_flush()
        
        handler.string_table_flush = original_string_table_flush
        
        # Write ignore tag string table if there are ignore tags
        if self.ignore_tags and ignore_tag_string_offsets:
            header.ignore_tag_offset = ignore_tag_data_offset
            
            for offset_pos, tag_string in ignore_tag_string_offsets:
                string_offset = handler.tell
                handler.write_wstring(tag_string)
                if tag_string not in preseed_first_string_offsets:
                    preseed_first_string_offsets[tag_string] = string_offset
                ref_offset = preseed_first_string_offsets[tag_string]
                handler.write_at(offset_pos, '<q', ref_offset)
        else:
            header.ignore_tag_offset = ignore_tag_data_offset
        
        if header.auto_generate_joint_desc_offset == 0:
            header.auto_generate_joint_desc_offset = first_string_section_start
        header.resourceInfoTbl = first_string_section_start
        header.userDataInfoTbl = first_string_section_start
        header.ukn_re3_tbl = first_string_section_start
        
        end_position = handler.tell
        
        header.magic = self.MAGIC
        header.write(handler, 0)
        
        handler.seek(end_position)
        
        return True
        
    def write(self, file_version: int = 0) -> bytes:
        """Write RCOL file to bytes"""
        if 0 < file_version < 10:
            raise ValueError(f"Unsupported RCOL version {file_version}; only rcol.10+ is supported")
        handler = FileHandler(bytearray(), file_version=file_version)
        self.do_write(handler)
        return handler.get_bytes()
        
    def read_rsz(self, handler: FileHandler, offset: int):
        """Read user data from RCOL file"""
        if offset == 0 or self.header.user_data_size == 0:
            return
            
        if offset >= len(handler.data):
            print(f"User data offset ({offset}) is beyond file size")
            return
            
        handler.seek(offset)
        user_data_size = self.header.user_data_size
        if handler.tell + user_data_size > len(handler.data):
            user_data_size = len(handler.data) - handler.tell
            print(f"User data size adjusted to {user_data_size}")
        
        if user_data_size > 0:
            self.user_data_bytes = handler.read_bytes(user_data_size)
            if self.user_data_bytes.startswith(b'RSZ\x00'):
                self.rsz = RSZFile()
                self.rsz.type_registry = self.type_registry
                self.rsz.filepath = handler.file_path
                self.rsz.read_headless(self.user_data_bytes)
            else:
                raise RuntimeError("RSZ magic not found in RSZ section.")
        else:
            self.user_data_bytes = b''
            self.rsz = None
            
    def write_rsz(self, handler: FileHandler):
        rsz_bytes = self.rsz.build_headless()
        self.user_data_bytes = rsz_bytes
        handler.write_bytes(rsz_bytes)
    def setup_references(self, file_version: int):
        """Setup references between RCOL components and RSZ instances"""
        if not self.rsz:
            return
        
        object_owner_by_index = {}

        def claim_object_index(index: int, owner: str):
            if index in object_owner_by_index:
                existing_owner = object_owner_by_index[index]
                raise ValueError(
                    f"RSZ object index {index} is referenced by both '{existing_owner}' and '{owner}'. "
                    f"Each RSZ object index must be owned by exactly one request-set entity."
                )
            object_owner_by_index[index] = owner
            
        # Setup group/shape base references first
        if file_version < 25:  # user_data_index is available ONLY before v25
            for group_index, group in enumerate(self.groups):
                for shape_index, shape in enumerate(group.shapes):
                    shape_idx = shape.info.user_data_index
                    if not isinstance(shape_idx, int) or shape_idx < 0:
                        continue
                    if shape_idx < len(self.rsz.object_table):
                        claim_object_index(
                            shape_idx,
                            f"group[{group_index}] shape[{shape_index}] base",
                        )
                        shape.instance = self.rsz.object_table[shape_idx]
                        
        # Setup request set references
        for i, request_set in enumerate(self.request_sets):            
            if not (0 <= request_set.info.group_index < len(self.groups)):
                request_set.group = None
                continue
            request_set.group = self.groups[request_set.info.group_index]
            
            if file_version >= 25:
                request_root_index = request_set.info.request_set_userdata_index
                if 0 <= request_root_index < len(self.rsz.object_table):
                    claim_object_index(
                        request_root_index,
                        f"request_set[{i}] root",
                    )
                    request_set.instance = self.rsz.object_table[request_root_index]
                else:
                    request_set.instance = None

                preserved_shape_entries = [
                    item for item in (request_set.shape_userdata or [])
                    if not isinstance(item, int)
                ]
                resolved_shape_instances = []
                for k in range(len(request_set.group.shapes)):
                    idx = request_set.info.group_userdata_index_start + k
                    if 0 <= idx < len(self.rsz.object_table):
                        claim_object_index(
                            idx,
                            f"request_set[{i}] shape[{k}]",
                        )
                        resolved_shape_instances.append(self.rsz.object_table[idx])
                request_set.shape_userdata = preserved_shape_entries + resolved_shape_instances
            else:
                request_obj_index = i
                if (
                    isinstance(request_obj_index, int)
                    and 0 <= request_obj_index < len(self.rsz.object_table)
                ):
                    claim_object_index(
                        request_obj_index,
                        f"request_set[{i}] root",
                    )
                    request_set.instance = self.rsz.object_table[request_obj_index]
                else:
                    request_set.instance = None
                
                preserved_shape_entries = [
                    item for item in (request_set.shape_userdata or [])
                    if not isinstance(item, int)
                ]
                resolved_shape_instances = []
                group_request_sets = [
                    rs for rs in self.request_sets
                    if rs.info.group_index == request_set.info.group_index
                ]
                is_primary_group_request = (
                    len(group_request_sets) > 0 and group_request_sets[0] is request_set
                )
                
                for k in range(len(request_set.group.shapes)):
                    shape = request_set.group.shapes[k]
                    if is_primary_group_request:
                        idx = shape.info.user_data_index
                    else:
                        idx = shape.info.user_data_index + request_set.info.shape_offset
                    if 0 <= idx < len(self.rsz.object_table):
                        if not is_primary_group_request:
                            claim_object_index(
                                idx,
                                f"request_set[{i}] shape[{k}]",
                            )
                        instance_id = self.rsz.object_table[idx]
                        if 0 <= instance_id < len(self.rsz.instance_infos):
                            resolved_shape_instances.append(self.rsz.instance_infos[instance_id])
                request_set.shape_userdata = preserved_shape_entries + resolved_shape_instances
