"""
RSZ Block Parser for MOTFSM files.
Based on verified parsing logic from test_conditions_1357.py and other tests.

RSZ (Resource Serialization) blocks contain serialized class instances with
field alignment based on ABSOLUTE position in the data stream.
"""
import struct
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from utils.binary_handler import BinaryHandler


RSZ_MAGIC = b'RSZ\x00'


def get_aligned_offset(pos: int, alignment: int) -> int:
    """
    Calculate aligned offset for field positioning.
    Uses ABSOLUTE alignment (verified in tests).
    """
    if alignment <= 1:
        return pos
    elif alignment == 2:
        return pos + (pos % 2)
    elif alignment == 4:
        return (pos + 3) & ~3
    elif alignment == 8:
        return (pos + 7) & ~7
    elif alignment == 16:
        return (pos + 15) & ~15
    else:
        if pos % alignment != 0:
            return pos + (alignment - (pos % alignment))
        return pos


@dataclass
class RSZHeader:
    """RSZ block header structure"""
    magic: bytes = b''
    version: int = 0
    object_count: int = 0
    instance_count: int = 0
    userdata_count: int = 0
    reserved: int = 0
    instance_offset: int = 0
    data_offset: int = 0
    userdata_offset: int = 0

    # Calculated absolute offsets
    base_offset: int = 0
    instance_info_start: int = 0
    data_start: int = 0
    userdata_info_start: int = 0


@dataclass
class RSZInstanceInfo:
    """Instance type information (8 bytes per instance)"""
    type_id: int = 0
    crc: int = 0


@dataclass
class RSZUserDataInfo:
    """UserData information (16 bytes per entry)"""
    instance_id: int = 0
    type_id: int = 0
    string_offset: int = 0


@dataclass
class RSZFieldValue:
    """Parsed field value with metadata"""
    name: str = ""
    type_name: str = ""
    value: Any = None
    offset: int = 0  # Absolute offset in the file
    size: int = 0
    is_array: bool = False
    array_count: int = 0
    _modified: bool = False  # Track if this field was modified by user

    def write_value_to_buffer(self, handler: BinaryHandler):
        """Write the field value back to the buffer at its original offset"""
        # CRITICAL: ONLY write if explicitly marked as modified
        if not self._modified:
            return

        if self.is_array:
            # Arrays are complex, skip for now
            return

        # Save current position
        current_pos = handler.tell
        handler.seek(self.offset)

        try:
            type_lower = self.type_name.lower()
            if type_lower == "u32":
                handler.write_uint32(int(self.value))
            elif type_lower == "s32":
                handler.write_int32(int(self.value))
            elif type_lower == "u16":
                handler.write_uint16(int(self.value))
            elif type_lower == "s16":
                handler.write_int16(int(self.value))
            elif type_lower == "u8":
                handler.write_uint8(int(self.value))
            elif type_lower == "s8":
                handler.write_int8(int(self.value))
            elif type_lower == "f32":
                handler.write_float(float(self.value))
            elif type_lower == "f64":
                handler.write_double(float(self.value))
            elif type_lower == "bool":
                handler.write_bool(bool(self.value))
            # String is more complex, skip for now
        finally:
            # Restore position
            handler.seek(current_pos)


@dataclass
class RSZInstance:
    """A parsed RSZ instance"""
    index: int = 0
    type_id: int = 0
    class_name: str = ""
    start_offset: int = 0
    end_offset: int = 0
    size: int = 0
    fields: List[RSZFieldValue] = field(default_factory=list)
    is_userdata: bool = False


class RSZBlock:
    """
    RSZ Block parser with lazy loading support.

    The RSZ block contains serialized class instances. Each instance's fields
    are aligned to their natural alignment based on ABSOLUTE position.
    """

    def __init__(self, data: bytes, block_offset: int, rsz_type_info: Dict = None):
        """
        Initialize RSZ block parser.

        Args:
            data: Full file data
            block_offset: Absolute offset where RSZ block starts
            rsz_type_info: Type information dictionary (from rszmhrise.json)
        """
        self.data = data
        self.block_offset = block_offset
        self.rsz_type_info = rsz_type_info or {}

        self.header: Optional[RSZHeader] = None
        self.instance_infos: List[RSZInstanceInfo] = []
        self.userdata_infos: List[RSZUserDataInfo] = []
        self.userdata_instance_ids: set = set()

        # Lazy loading cache
        self._instance_positions: Dict[int, int] = {}
        self._parsed_instances: Dict[int, RSZInstance] = {}
        self._positions_calculated: bool = False

        # Parse header immediately
        self._parse_header()

    def _parse_header(self):
        """Parse RSZ header and instance/userdata info arrays"""
        pos = self.block_offset

        # Verify magic
        magic = self.data[pos:pos+4]
        if magic != RSZ_MAGIC:
            raise ValueError(f"Invalid RSZ magic at 0x{pos:X}: {magic.hex()}")

        self.header = RSZHeader()
        self.header.magic = magic
        self.header.base_offset = pos

        pos += 4
        self.header.version = struct.unpack_from('<I', self.data, pos)[0]; pos += 4
        self.header.object_count = struct.unpack_from('<I', self.data, pos)[0]; pos += 4
        self.header.instance_count = struct.unpack_from('<I', self.data, pos)[0]; pos += 4
        self.header.userdata_count = struct.unpack_from('<I', self.data, pos)[0]; pos += 4
        self.header.reserved = struct.unpack_from('<I', self.data, pos)[0]; pos += 4
        self.header.instance_offset = struct.unpack_from('<Q', self.data, pos)[0]; pos += 8
        self.header.data_offset = struct.unpack_from('<Q', self.data, pos)[0]; pos += 8
        self.header.userdata_offset = struct.unpack_from('<Q', self.data, pos)[0]; pos += 8

        # Calculate absolute offsets
        self.header.instance_info_start = self.block_offset + self.header.instance_offset
        self.header.data_start = self.block_offset + self.header.data_offset
        self.header.userdata_info_start = self.block_offset + self.header.userdata_offset

        # Parse instance info array
        self.instance_infos = []
        for i in range(self.header.instance_count):
            info_pos = self.header.instance_info_start + i * 8
            info = RSZInstanceInfo(
                type_id=struct.unpack_from('<I', self.data, info_pos)[0],
                crc=struct.unpack_from('<I', self.data, info_pos + 4)[0]
            )
            self.instance_infos.append(info)

        # Parse userdata info array
        self.userdata_infos = []
        self.userdata_instance_ids = set()
        for i in range(self.header.userdata_count):
            ud_pos = self.header.userdata_info_start + i * 16
            ud_info = RSZUserDataInfo(
                instance_id=struct.unpack_from('<I', self.data, ud_pos)[0],
                type_id=struct.unpack_from('<I', self.data, ud_pos + 4)[0],
                string_offset=struct.unpack_from('<Q', self.data, ud_pos + 8)[0]
            )
            self.userdata_infos.append(ud_info)
            self.userdata_instance_ids.add(ud_info.instance_id)

    def _ensure_positions_calculated(self):
        """Calculate all instance positions (lazy)"""
        if self._positions_calculated:
            return

        pos = self.header.data_start
        self._instance_positions = {0: pos}  # Instance 0 is always at data_start (NULL)

        for i in range(1, self.header.instance_count):
            self._instance_positions[i] = pos

            if i in self.userdata_instance_ids:
                # UserData instances only occupy 1 byte (skipFileData)
                pos += 1
            else:
                type_id = self.instance_infos[i].type_id
                pos = self._calc_instance_end(pos, type_id)

        self._positions_calculated = True

    def _calc_instance_end(self, pos: int, type_id: int) -> int:
        """
        Calculate instance end position by traversing all fields.
        Uses ABSOLUTE alignment (verified in tests).
        """
        type_hex = f"{type_id:x}"
        if type_hex not in self.rsz_type_info:
            return pos

        fields = self.rsz_type_info[type_hex].get("fields", [])
        for field_def in fields:
            fsize = field_def.get("size", 0)
            falign = field_def.get("align", 1)
            farray = field_def.get("array", False)
            ftype = field_def.get("type", "")

            if farray:
                pos = get_aligned_offset(pos, 4)
                arr_count = struct.unpack_from('<i', self.data, pos)[0]
                pos += 4
                for _ in range(arr_count):
                    pos = get_aligned_offset(pos, falign)
                    pos += fsize
            elif ftype == "String":
                pos = get_aligned_offset(pos, 4)
                char_count = struct.unpack_from('<i', self.data, pos)[0]
                pos += 4
                if char_count > 0:
                    pos += char_count * 2 + 2  # UTF-16LE + null terminator
            else:
                pos = get_aligned_offset(pos, falign)
                pos += fsize

        return pos

    def get_instance(self, index: int) -> Optional[RSZInstance]:
        """
        Get parsed instance by index (lazy loading).

        Args:
            index: Instance index (0 is NULL, 1+ are actual instances)

        Returns:
            Parsed RSZInstance or None if out of range
        """
        if index < 0 or index >= self.header.instance_count:
            return None

        # Check cache first
        if index in self._parsed_instances:
            return self._parsed_instances[index]

        # Ensure positions are calculated
        self._ensure_positions_calculated()

        # Parse the instance
        instance = self._parse_instance(index)
        self._parsed_instances[index] = instance
        return instance

    def _parse_instance(self, index: int) -> RSZInstance:
        """Parse a single instance at the given index"""
        type_id = self.instance_infos[index].type_id
        start_pos = self._instance_positions[index]

        instance = RSZInstance(
            index=index,
            type_id=type_id,
            start_offset=start_pos,
            is_userdata=(index in self.userdata_instance_ids)
        )

        # Get class name
        type_hex = f"{type_id:x}"
        if type_hex in self.rsz_type_info:
            instance.class_name = self.rsz_type_info[type_hex].get("name", "Unknown")
        else:
            instance.class_name = f"Unknown_0x{type_id:08X}"

        # UserData instances only have 1 byte
        if instance.is_userdata:
            instance.end_offset = start_pos + 1
            instance.size = 1
            return instance

        # Parse fields
        pos = start_pos
        if type_hex in self.rsz_type_info:
            fields_def = self.rsz_type_info[type_hex].get("fields", [])
            for field_def in fields_def:
                field_val, pos = self._parse_field(pos, field_def)
                instance.fields.append(field_val)

        instance.end_offset = pos
        instance.size = pos - start_pos
        return instance

    def _parse_field(self, pos: int, field_def: dict) -> Tuple[RSZFieldValue, int]:
        """Parse a single field and return the field value and new position"""
        fname = field_def.get("name", "?")
        ftype = field_def.get("type", "?")
        fsize = field_def.get("size", 0)
        falign = field_def.get("align", 1)
        farray = field_def.get("array", False)

        field_val = RSZFieldValue(
            name=fname,
            type_name=ftype,
            is_array=farray
        )

        if farray:
            pos = get_aligned_offset(pos, 4)
            field_val.offset = pos
            arr_count = struct.unpack_from('<i', self.data, pos)[0]
            field_val.array_count = arr_count
            field_val.value = f"array[{arr_count}]"
            pos += 4

            # Skip array elements
            for _ in range(arr_count):
                pos = get_aligned_offset(pos, falign)
                pos += fsize

            field_val.size = pos - field_val.offset

        elif ftype == "String":
            pos = get_aligned_offset(pos, 4)
            field_val.offset = pos
            char_count = struct.unpack_from('<i', self.data, pos)[0]
            field_val.array_count = char_count
            pos += 4

            if char_count > 0:
                # Read UTF-16LE string
                string_data = self.data[pos:pos + char_count * 2]
                try:
                    field_val.value = string_data.decode('utf-16le')
                except:
                    field_val.value = f"<decode_error:{char_count}>"
                pos += char_count * 2 + 2  # Including null terminator
            else:
                field_val.value = ""

            field_val.size = pos - field_val.offset

        else:
            pos = get_aligned_offset(pos, falign)
            field_val.offset = pos
            field_val.size = fsize

            # Read value based on type
            if ftype == "Bool":
                field_val.value = self.data[pos] != 0
            elif ftype in ("U32", "Resource"):
                field_val.value = struct.unpack_from('<I', self.data, pos)[0]
            elif ftype == "S32":
                field_val.value = struct.unpack_from('<i', self.data, pos)[0]
            elif ftype == "F32":
                field_val.value = struct.unpack_from('<f', self.data, pos)[0]
            elif ftype == "U64":
                field_val.value = struct.unpack_from('<Q', self.data, pos)[0]
            elif ftype == "S64":
                field_val.value = struct.unpack_from('<q', self.data, pos)[0]
            elif ftype == "Guid":
                field_val.value = self.data[pos:pos+16].hex()
            elif ftype == "U16":
                field_val.value = struct.unpack_from('<H', self.data, pos)[0]
            elif ftype == "S16":
                field_val.value = struct.unpack_from('<h', self.data, pos)[0]
            elif ftype in ("U8", "S8"):
                field_val.value = self.data[pos]
            elif ftype == "Object":
                # Object reference (instance index)
                field_val.value = struct.unpack_from('<I', self.data, pos)[0]
            else:
                field_val.value = f"<{ftype}:{fsize}b>"

            pos += fsize

        return field_val, pos

    def get_instance_by_type(self, class_name: str) -> List[RSZInstance]:
        """Get all instances of a specific class type"""
        result = []
        for i in range(1, self.header.instance_count):
            type_id = self.instance_infos[i].type_id
            type_hex = f"{type_id:x}"
            if type_hex in self.rsz_type_info:
                name = self.rsz_type_info[type_hex].get("name", "")
                if name == class_name:
                    result.append(self.get_instance(i))
        return result

    def get_class_name(self, index: int) -> str:
        """Get class name for instance at index without full parsing"""
        if index < 0 or index >= self.header.instance_count:
            return "Invalid"

        type_id = self.instance_infos[index].type_id
        type_hex = f"{type_id:x}"
        if type_hex in self.rsz_type_info:
            return self.rsz_type_info[type_hex].get("name", f"Unknown_0x{type_id:08X}")
        return f"Unknown_0x{type_id:08X}"

    @property
    def object_count(self) -> int:
        return self.header.object_count if self.header else 0

    @property
    def instance_count(self) -> int:
        return self.header.instance_count if self.header else 0


class RSZBlockCollection:
    """
    Collection of RSZ blocks for a MOTFSM file.
    Provides unified access to all RSZ blocks with lazy loading.
    """

    def __init__(self, data: bytes, bhvt_base: int, offsets: dict, rsz_type_info: Dict = None):
        """
        Initialize RSZ block collection.

        Args:
            data: Full file data
            bhvt_base: BHVT structure base offset
            offsets: Dictionary of offset names to relative offsets
            rsz_type_info: Type information dictionary
        """
        self.data = data
        self.bhvt_base = bhvt_base
        self.offsets = offsets
        self.rsz_type_info = rsz_type_info or {}

        # Lazy-loaded RSZ blocks
        self._blocks: Dict[str, Optional[RSZBlock]] = {}

        # Block name to offset key mapping
        self._block_keys = {
            "actions": "action_offset",
            "selectors": "selector_offset",
            "selector_callers": "selector_caller_offset",
            "conditions": "conditions_offset",
            "transition_events": "transition_event_offset",
            "expression_tree_conditions": "expression_tree_conditions_offset",
            "static_actions": "static_action_offset",
            "static_selector_callers": "static_selector_caller_offset",
            "static_conditions": "static_conditions_offset",
            "static_transition_events": "static_transition_event_offset",
            "static_expression_tree_conditions": "static_expression_tree_conditions_offset",
        }

    def get_block(self, name: str) -> Optional[RSZBlock]:
        """
        Get RSZ block by name (lazy loading).

        Args:
            name: Block name (e.g., "actions", "conditions", "static_conditions")

        Returns:
            RSZBlock or None if not found
        """
        if name in self._blocks:
            return self._blocks[name]

        offset_key = self._block_keys.get(name)
        if not offset_key or offset_key not in self.offsets:
            return None

        relative_offset = self.offsets[offset_key]
        absolute_offset = self.bhvt_base + relative_offset

        try:
            block = RSZBlock(self.data, absolute_offset, self.rsz_type_info)
            self._blocks[name] = block
            return block
        except Exception as e:
            print(f"Failed to parse RSZ block '{name}' at 0x{absolute_offset:X}: {e}")
            self._blocks[name] = None
            return None

    @property
    def actions(self) -> Optional[RSZBlock]:
        return self.get_block("actions")

    @property
    def selectors(self) -> Optional[RSZBlock]:
        return self.get_block("selectors")

    @property
    def selector_callers(self) -> Optional[RSZBlock]:
        return self.get_block("selector_callers")

    @property
    def conditions(self) -> Optional[RSZBlock]:
        return self.get_block("conditions")

    @property
    def transition_events(self) -> Optional[RSZBlock]:
        return self.get_block("transition_events")

    @property
    def expression_tree_conditions(self) -> Optional[RSZBlock]:
        return self.get_block("expression_tree_conditions")

    @property
    def static_actions(self) -> Optional[RSZBlock]:
        return self.get_block("static_actions")

    @property
    def static_selector_callers(self) -> Optional[RSZBlock]:
        return self.get_block("static_selector_callers")

    @property
    def static_conditions(self) -> Optional[RSZBlock]:
        return self.get_block("static_conditions")

    @property
    def static_transition_events(self) -> Optional[RSZBlock]:
        return self.get_block("static_transition_events")

    @property
    def static_expression_tree_conditions(self) -> Optional[RSZBlock]:
        return self.get_block("static_expression_tree_conditions")
