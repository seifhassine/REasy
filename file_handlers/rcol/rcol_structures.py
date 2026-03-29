import uuid
from typing import List, Optional, Any
from .base_model import BaseModel, FileHandler
from .shape_types import ShapeType, read_shape, write_shape
from utils.hash_util import murmur3_hash

def calc_hash(text: str) -> int:
    return murmur3_hash((text or "").encode('utf-16le'))

RCOL_MAGIC = 0x4C4F4352  # 'RCOL'

class Header(BaseModel):
    """RCOL file header"""
    def __init__(self):
        super().__init__()
        self.magic = RCOL_MAGIC
        self.num_groups = 0
        self.num_shapes = 0
        self.ukn_count = 0
        self.num_user_data = 0
        self.num_request_sets = 0
        self.max_request_set_id = 0
        
        self.num_ignore_tags = 0
        self.num_auto_generate_joints = 0
        
        self.user_data_size = 0
        self.status = 0 #via.physics.RequestSetColliderResource.State
        
        self.ukn_re3_a = 0
        self.ukn_re3_b = 0
        
        self.numResourceInfos = 0
        self.numUserDataInfos = 0
        
        self.groups_ptr_offset = 0
        self.data_offset = 0
        self.request_set_offset = 0
        self.ignore_tag_offset = 0
        self.request_set_id_lookups_offset = 0  # rcol.2
        self.auto_generate_joint_desc_offset = 0
        self.resourceInfoTbl = 0
        self.userDataInfoTbl = 0
        self.ukn_re3_tbl = 0
        
    def do_read(self, handler: FileHandler) -> bool:
        self.magic = handler.read_uint32()
        self.num_groups = handler.read_int32()
        if handler.file_version >= 25:
            self.num_user_data = handler.read_int32()
            self.ukn_count = handler.read_int32() # TODO might actually be numuserdata despite tests failing, maybe different meaning
        else:
            self.num_shapes = handler.read_int32()
            self.num_user_data = handler.read_int32()
        self.num_request_sets = handler.read_int32()
        self.max_request_set_id = handler.read_uint32()
        
        if handler.file_version > 11:
            self.num_ignore_tags = handler.read_int32()
            self.num_auto_generate_joints = handler.read_int32()
            
        if handler.file_version == 11:
            handler.read_int64()
            handler.read_int64() #padding
        self.user_data_size = handler.read_int32()
        self.status = handler.read_int32()
        if handler.file_version >= 20:
            self.numResourceInfos = handler.read_uint32()
            self.numUserDataInfos = handler.read_uint32()
            
        self.groups_ptr_offset = handler.read_int64()
        self.data_offset = handler.read_int64()
        self.request_set_offset = handler.read_int64()
        
        if handler.file_version > 11:
            self.ignore_tag_offset = handler.read_int64()
            self.auto_generate_joint_desc_offset = handler.read_int64()
        elif handler.file_version == 11:
            self.ukn_re3_tbl = handler.read_int64()
            
        if handler.file_version >= 20:
            self.resourceInfoTbl = handler.read_int64()
            self.userDataInfoTbl = handler.read_int64()
            
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        handler.write_uint32(self.magic)
        handler.write_int32(self.num_groups)
        if handler.file_version >= 25:
            handler.write_int32(self.num_user_data)
            handler.write_int32(self.ukn_count)
        else:
            handler.write_int32(self.num_shapes)
            handler.write_int32(self.num_user_data)
        handler.write_int32(self.num_request_sets)
        if(self.num_request_sets == 0 and handler.file_version < 18):
            handler.write_int32(-1)
        else: handler.write_uint32(self.max_request_set_id)
        
        if handler.file_version > 11:
            handler.write_int32(self.num_ignore_tags)
            handler.write_int32(self.num_auto_generate_joints)
            
        if handler.file_version == 11:
            handler.write_int64(self.ukn_re3_a)
            handler.write_int64(self.ukn_re3_b)
        handler.write_int32(self.user_data_size)
        handler.write_int32(self.status)
        if handler.file_version >= 20:
            handler.write_int32(self.numResourceInfos)
            handler.write_int32(self.numUserDataInfos)
            
        handler.write_int64(self.groups_ptr_offset)
        handler.write_int64(self.data_offset)
        handler.write_int64(self.request_set_offset)
        
        if handler.file_version > 11:
            handler.write_int64(self.ignore_tag_offset)
            handler.write_int64(self.auto_generate_joint_desc_offset)
        elif handler.file_version == 11:
            handler.write_int64(self.ukn_re3_tbl)
            
        if handler.file_version >= 20:
            handler.write_int64(self.resourceInfoTbl)
            handler.write_int64(self.userDataInfoTbl)
            
        return True

class GroupInfo(BaseModel):
    """RCOL group information"""
    def __init__(self):
        super().__init__()
        self.guid = uuid.UUID(int=0)
        self.name = ""
        self.name_hash = 0
        
        self.rsz_chunk = 0
        self.num_mirror_shapes = 0
        self.num_shapes = 0
        
        self.num_mask_guids = 0
        self.shapes_offset = 0
        self.layer_index = 0
        self.mask_bits = 0
        self.mask_guids_offset = 0
        self.layer_guid = uuid.UUID(int=0)        
        self.mask_guids_offset_start = 0
        
        self.mask_guids: Optional[List[uuid.UUID]] = None
        self.shapes_offset_start = 0
        
    def do_read(self, handler: FileHandler) -> bool:
        if self.mask_guids:
            self.mask_guids.clear()
        
        self.guid = handler.read_guid()
        
        self.name = handler.read_offset_wstring()
            
        self.name_hash = handler.read_uint32()
        
        if handler.file_version >= 25:
            self.num_shapes = handler.read_int32()
            self.num_mirror_shapes = handler.read_int32()
            self.num_mask_guids = handler.read_int32()
        else:
            self.rsz_chunk = handler.read_int32() # this refers to the entire RSZ chunk id. Should always be 0.
            if(self.rsz_chunk != 0):
                raise  RuntimeError("Currently only groups with RSZ chunk 0 are supported.")
            self.num_shapes = handler.read_int32()
            self.num_mask_guids = handler.read_int32()
            
        self.shapes_offset = handler.read_int64()
        self.layer_index = handler.read_int32()
        self.mask_bits = handler.read_uint32()
        
        self.mask_guids_offset = handler.read_int64()
        self.layer_guid = handler.read_guid()
        if self.mask_guids_offset > 0 and self.num_mask_guids > 0:
            with handler.seek_jump_back(self.mask_guids_offset):
                self.mask_guids = self.mask_guids or []
                handler.read_list(self.mask_guids, self.num_mask_guids)
                
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        handler.write_guid(self.guid)
        
        handler.write_offset_wstring(self.name)
            
        self.name_hash = calc_hash(self.name)
        handler.write_uint32(self.name_hash)
        
        self.num_mask_guids = len(self.mask_guids) if self.mask_guids else 0
        
        if handler.file_version >= 25:
            handler.write_int32(self.num_shapes)
            handler.write_int32(self.num_mirror_shapes)
            handler.write_int32(self.num_mask_guids)
        else:
            handler.write_int32(self.rsz_chunk)
            handler.write_int32(self.num_shapes)
            handler.write_int32(self.num_mask_guids)
            
        self.shapes_offset_start = handler.tell
        handler.write_int64(self.shapes_offset)
        handler.write_int32(self.layer_index)
        handler.write_uint32(self.mask_bits)
        
        self.mask_guids_offset_start = handler.tell
        handler.write_int64(self.mask_guids_offset)
        handler.write_guid(self.layer_guid)
            
        return True
        
    def __str__(self):
        return self.name

class RcolShapeInfo(BaseModel):
    """RCOL shape information"""
    def __init__(self):
        super().__init__()
        self.guid = uuid.UUID(int=0)
        self.name = ""
        self.name_hash = 0
        self.user_data_index = 0
        self.status = 0
        self.layer_index = 0
        self.attribute = 0
        self.skip_id_bits = 0
        self.ignore_tag_bits = 0
        self.primary_joint_name_str = ""
        self.secondary_joint_name_str = ""
        self.primary_joint_name_hash = 0
        self.secondary_joint_name_hash = 0
        self.cmat_path: Optional[str] = None
        self.shape_type = ShapeType.Invalid
        
    def do_read(self, handler: FileHandler) -> bool:
        self.guid = handler.read_guid()
        
        self.name = handler.read_offset_wstring()
        self.name_hash = handler.read_uint32()
        # ????  via.physics.RequestSetColliderResource.ShapeState
        if handler.file_version >= 25:
            self.status = handler.read_int32()
        else: self.user_data_index = handler.read_int32()
        self.layer_index = handler.read_int32()
        self.attribute = handler.read_int32()
        
        if handler.file_version >= 27:
            self.skip_id_bits = handler.read_uint32()
            self.shape_type = handler.read_int32()
            self.ignore_tag_bits = handler.read_uint64()
            self.primary_joint_name_str = handler.read_offset_wstring()
            self.secondary_joint_name_str = handler.read_offset_wstring()
            self.primary_joint_name_hash = handler.read_uint32()
            self.secondary_joint_name_hash = handler.read_uint32()
            if handler.file_version >= 28:
                cmat_offset = handler.read_uint64()
                if cmat_offset > 0:
                    with handler.seek_jump_back(cmat_offset):
                        self.cmat_path = handler.read_wstring()
                else:
                    self.cmat_path = None
                handler.skip(8)
        else:
            self.skip_id_bits = handler.read_uint32()
            self.ignore_tag_bits = handler.read_uint32()
            self.primary_joint_name_str = handler.read_offset_wstring()
            self.secondary_joint_name_str = handler.read_offset_wstring()
            self.primary_joint_name_hash = handler.read_uint32()
            self.secondary_joint_name_hash = handler.read_uint32()
            self.shape_type = handler.read_int32()
            handler.skip(4)
            
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        self.primary_joint_name_hash = calc_hash(self.primary_joint_name_str)
        self.secondary_joint_name_hash = calc_hash(self.secondary_joint_name_str)
        self.name_hash = calc_hash(self.name)
        
        handler.write_guid(self.guid)
        
        handler.write_offset_wstring(self.name or "")
            
        handler.write_uint32(self.name_hash)
        if handler.file_version >= 25:
            handler.write_int32(self.status)
        else: handler.write_int32(self.user_data_index)
        
        handler.write_int32(self.layer_index)
        handler.write_int32(self.attribute)
        
        if handler.file_version >= 27:
            handler.write_uint32(self.skip_id_bits)
            handler.write_int32(self.shape_type)
            handler.write_uint64(self.ignore_tag_bits)
            handler.write_offset_wstring(self.primary_joint_name_str or "", context="joint_name")
            handler.write_offset_wstring(self.secondary_joint_name_str or "", context="joint_name")
            handler.write_uint32(self.primary_joint_name_hash)
            handler.write_uint32(self.secondary_joint_name_hash)
            if handler.file_version >= 28:
                if self.cmat_path:
                    handler.write_offset_wstring(self.cmat_path, context="cmat")
                else:
                    handler.write_int64(0)
                handler.write_int64(0)
        else:
            handler.write_uint32(self.skip_id_bits)
            handler.write_uint32(self.ignore_tag_bits)
            handler.write_offset_wstring(self.primary_joint_name_str or "", context="joint_name")
            handler.write_offset_wstring(self.secondary_joint_name_str or "", context="joint_name")
            handler.write_uint32(self.primary_joint_name_hash)
            handler.write_uint32(self.secondary_joint_name_hash)
            handler.write_int32(self.shape_type)
            handler.write_int32(0)  # padding
            
        return True

class RcolShape(BaseModel):
    """RCOL shape with info and geometry data"""
    def __init__(self):
        super().__init__()
        self.info = RcolShapeInfo()
        self.shape = None
        self.instance = None  # RSZ instance
        
    def do_read(self, handler: FileHandler) -> bool:
        self.info.read(handler)
        
        tell = handler.tell
        self.shape = read_shape(handler, self.info.shape_type)
        
        # Shape payload block is always 80 bytes.
        bytes_read = handler.tell - tell
        remaining_bytes = 80 - bytes_read
        if remaining_bytes < 0:
            raise ValueError(
                f"Shape payload overrun for '{self.info.name}' ({self.info.shape_type}): "
                f"read {bytes_read} bytes, expected <= 80"
            )
        if remaining_bytes > 0:
            handler.skip(remaining_bytes)
        
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        self.info.write(handler)
        
        tell = handler.tell
        write_shape(handler, self.info.shape_type, self.shape)
        
        # Shape payload block is always 80 bytes.
        bytes_written = handler.tell - tell
        remaining_bytes = 80 - bytes_written
        if remaining_bytes < 0:
            raise ValueError(
                f"Shape payload overrun for '{self.info.name}' ({self.info.shape_type}): "
                f"wrote {bytes_written} bytes, expected <= 80"
            )
        if remaining_bytes > 0:
            handler.write_bytes(b'\x00' * remaining_bytes)
        
        return True
        
    def __str__(self):
        if self.shape is None:
            return f"RcolShape [{self.instance}]"
        else:
            return f"{self.shape} [{self.instance}]"

class RcolGroup(BaseModel):
    """RCOL group containing shapes"""
    def __init__(self):
        super().__init__()
        self.info = GroupInfo()
        self.shapes: List[RcolShape] = []
        self.extra_shapes: List[RcolShape] = []  # may be >= rcol.25 exclusive
        
    def read_info(self, handler: FileHandler) -> bool:
        return self.info.read(handler)
        
    def write_info(self, handler: FileHandler) -> bool:
        self.info.num_shapes = len(self.shapes)
        self.info.num_mirror_shapes = len(self.extra_shapes)
        return self.info.write(handler)
        
    def do_read(self, handler: FileHandler) -> bool:
        self.shapes.clear()
        total_shapes = self.info.num_shapes + self.info.num_mirror_shapes
        has_shape_stream = False
        if total_shapes > 0 and self.info.shapes_offset > 0:
            # Check if offset is valid
            if self.info.shapes_offset >= len(handler.data):
                print(f"Warning: shapes_offset {self.info.shapes_offset} is beyond file size {len(handler.data)}")
                return True
            
            handler.seek(self.info.shapes_offset)
            has_shape_stream = True
        if self.info.num_shapes > 0 and has_shape_stream:
            for i in range(self.info.num_shapes):
                shape = RcolShape()
                if not shape.read(handler):
                    raise ValueError(f"Failed to read shape {i} in group '{self.info.name}'")
                self.shapes.append(shape)
                
        self.extra_shapes.clear()
        if self.info.num_mirror_shapes > 0 and has_shape_stream:
            for i in range(self.info.num_mirror_shapes):
                shape = RcolShape()
                if not shape.read(handler):
                    raise ValueError(f"Failed to read extra shape {i} in group '{self.info.name}'")
                self.extra_shapes.append(shape)
                
        return True
        
    def do_write(self, handler: FileHandler) -> bool:
        has_shape_payload = self.info.num_shapes > 0 or self.info.num_mirror_shapes > 0
        if has_shape_payload:
            self.info.shapes_offset = handler.tell
            if self.info.shapes_offset_start > 0:
                handler.write_int64_at(self.info.shapes_offset_start, self.info.shapes_offset)
            else:
                raise ValueError("Should WriteInfo first")
                
        if self.info.num_shapes > 0:
            for shape in self.shapes:
                shape.write(handler)
                
        if self.info.num_mirror_shapes > 0:
            for shape in self.extra_shapes:
                shape.write(handler)
                
        return True
        
    def __str__(self):
        return self.info.name
