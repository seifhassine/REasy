import struct
from utils.hex_util import *


########################################
# UserData Block Parsing Classes
########################################


class RcolUserDataRSZHeader:
    HEADER_SIZE = 48

    def __init__(self):
        self.magic = 0
        self.version = 0
        self.objectCount = 0
        self.instanceCount = 0
        self.userDataCount = 0
        self.reserved = 0
        self.instanceOffset = 0
        self.dataOffset = 0
        self.userDataOffset = 0

    def parse(self, data: bytes):
        (
            self.magic,
            self.version,
            self.objectCount,
            self.instanceCount,
            self.userDataCount,
            self.reserved,
            self.instanceOffset,
            self.dataOffset,
            self.userDataOffset,
        ) = struct.unpack_from("<IIIIIIQQQ", data, 0)


class RcolUserDataInstanceInfo:
    SIZE = 8

    def __init__(self):
        self.type_id = 0
        self.crc = 0
        self.extra_info = None

    def parse(self, data: bytes, offset: int) -> int:
        self.type_id, self.crc = struct.unpack_from("<II", data, offset)
        return offset + self.SIZE


class RcolUserDataFull:
    """
    Parses the complete userData block:
      1) RSZHeader (48 bytes)
      2) ObjectTable: objectCount entries (4 bytes each)
      3) InstanceInfos: instanceCount entries (8 bytes each; first forced null)
      4) Data: exactly userDataSize bytes.
      Then groups InstanceInfos not referenced in ObjectTable.
    """

    def __init__(self):
        self.header = None
        self.object_table = []
        self.instance_infos = []
        self.data = b""
        self.data_groups = []  # list of tuples: (main_index, [child indices])
        # Reworked: data_group_bytes is now a list of 5-tuples:
        # (main_index, [child indices], [bytes consumed per child], raw_bytes, parsed_fields_per_child)
        self.data_group_bytes = []
        self.userDataSize = 0
        self.type_registry = None  # This is set externally by the handler.

    def parse(self, data: bytes, offset: int, userDataSize: int):
        self.userDataSize = userDataSize
        block = data[offset:]
        if len(block) < RcolUserDataRSZHeader.HEADER_SIZE:
            raise ValueError("UserData block too short for RSZHeader")
        self.header = RcolUserDataRSZHeader()
        self.header.parse(block)
        off = RcolUserDataRSZHeader.HEADER_SIZE
        # Parse ObjectTable.
        count_obj = self.header.objectCount
        need_obj = off + count_obj * 4
        if need_obj > len(block):
            raise ValueError("ObjectTable out of range in userData block")
        self.object_table = list(struct.unpack_from(f"<{count_obj}i", block, off))
        off = need_obj
        # Parse InstanceInfos.
        count_inst = self.header.instanceCount
        need_inst = off + count_inst * RcolUserDataInstanceInfo.SIZE
        if need_inst > len(block):
            raise ValueError("InstanceInfos out of range in userData block")
        self.instance_infos = []
        for i in range(count_inst):
            inst = RcolUserDataInstanceInfo()
            off = inst.parse(block, off)
            if i == 0:
                inst.type_id = 0
                inst.crc = 0
            self.instance_infos.append(inst)
        # Parse Data.
        # The header.userDataSize covers the entire RSZUserData Components,
        # so the Data field is userDataSize - header.dataOffset bytes.
        data_start = offset + self.header.dataOffset
        data_length = userDataSize - self.header.dataOffset
        if data_start + data_length > len(data):
            raise ValueError("Not enough data for the Data field in userData block")
        self.data = data[data_start : data_start + data_length]
        # Group InstanceInfos not referenced in ObjectTable.
        self.group_data()

    def group_data(self, debug=False):
        # Build groups as before using the ObjectTable.
        if self.type_registry is None:
            raise ValueError("Type registry not set in RcolUserDataFull")
        child_set = set(self.object_table)
        main_indices = [
            i for i in range(1, len(self.instance_infos)) if i not in child_set
        ]
        main_indices.sort()
        groups = []
        for idx, main in enumerate(main_indices):
            group = []
            next_main = (
                main_indices[idx + 1]
                if idx + 1 < len(main_indices)
                else len(self.instance_infos)
            )
            for i in range(main + 1, next_main):
                if i in child_set:
                    group.append(i)
            groups.append((main, group))
        self.data_groups = groups
        if debug:
            print("DEBUG: Data Groups (main, children):")
            for g in groups:
                print("  Group:", g)

        # Flatten children order.
        all_children = []
        for main, children in groups:
            all_children.extend(children)
        if debug:
            print("DEBUG: Flat list of all children:", all_children)

        # Process the Data block sequentially for each child.
        child_consumed = {}  # child index -> bytes consumed (including alignment)
        parsed_fields_dict = {}  # child index -> parsed fields list
        consumed_slices = {}  # child index -> raw bytes consumed (including alignment)
        data_off = 0
        for child in all_children:
            if debug:
                print(
                    f"DEBUG: Parsing child {child} starting at data_off {data_off:#x}"
                )
            inst = self.instance_infos[child]
            try:
                tid = int(inst.type_id)
            except Exception:
                tid = 0
            info = self.type_registry.get_type_info(tid)
            if info is None or "fields" not in info:
                child_consumed[child] = 0
                parsed_fields_dict[child] = []
                consumed_slices[child] = b""
                if debug:
                    print(f"DEBUG: No field definitions for child {child}")
            else:
                start = data_off
                parsed_fields, new_off = parse_instance_fields(
                    self.data, data_off, info["fields"], debug=debug
                )
                # Determine required alignment for this element from the last field, default to 4.
                required_align = 4
                if info["fields"]:
                    last_field = info["fields"][-1]
                    if "align" in last_field:
                        try:
                            required_align = int(last_field["align"])
                        except Exception:
                            required_align = 4
                # Align new_off to the next multiple of required_align.
                aligned_off = align_offset(new_off, required_align)
                if debug:
                    print(
                        f"DEBUG: Child {child} parsed from {start:#x} to {new_off:#x} ({new_off - start} bytes)"
                    )
                    if aligned_off != new_off:
                        print(
                            f"DEBUG: Aligning child {child} from {new_off:#x} to {aligned_off:#x} (alignment {required_align})"
                        )
                consumed = aligned_off - start
                child_consumed[child] = consumed
                parsed_fields_dict[child] = parsed_fields
                consumed_slices[child] = self.data[start:aligned_off]
                data_off = aligned_off
        if debug:
            print(
                f"DEBUG: Finished parsing all children; total consumed data: {data_off} of {len(self.data)} bytes"
            )

        # Reassemble per group.
        self.data_group_bytes = []
        for main, children in groups:
            group_raw = b"".join(consumed_slices.get(child, b"") for child in children)
            child_sizes = [child_consumed.get(child, 0) for child in children]
            parsed_list = [
                (child, parsed_fields_dict.get(child, [])) for child in children
            ]
            self.data_group_bytes.append(
                (main, children, child_sizes, group_raw, parsed_list)
            )
            if debug:
                print(
                    f"DEBUG: Group for main {main}: children {children}, sizes {child_sizes}"
                )
        if data_off != len(self.data):
            print(
                f"Warning: total consumed data ({data_off}) does not equal Data length ({len(self.data)})"
            )

    def group_data(self, debug=False):
        # Build groups
        child_set = set(self.object_table)
        main_indices = [
            i for i in range(1, len(self.instance_infos)) if i not in child_set
        ]
        main_indices.sort()
        groups = []
        for idx, main in enumerate(main_indices):
            group = []
            next_main = (
                main_indices[idx + 1]
                if idx + 1 < len(main_indices)
                else len(self.instance_infos)
            )
            for i in range(main + 1, next_main):
                if i in child_set:
                    group.append(i)
            groups.append((main, group))
        self.data_groups = groups
        if debug:
            print("DEBUG: Data Groups (main, children):")
            for g in groups:
                print("  Group:", g)

        # Now process the Data block sequentially for all children in group order.
        child_consumed = {}  # child index -> bytes consumed (including alignment)
        parsed_fields_dict = {}  # child index -> parsed fields list
        consumed_slices = {}  # child index -> raw bytes consumed (including alignment)
        data_off = 0
        # Flatten all children in order.
        all_children = []
        for main, children in groups:
            all_children.extend(children)
        if debug:
            print("DEBUG: Flat list of all children:", all_children)
        for child in all_children:
            if debug:
                print(
                    f"DEBUG: Parsing child {child} starting at data_off {data_off:#x}"
                )
            inst = self.instance_infos[child]
            try:
                tid = int(inst.type_id)
            except Exception:
                tid = 0
            info = self.type_registry.get_type_info(tid)
            if info is None or "fields" not in info:
                child_consumed[child] = 0
                parsed_fields_dict[child] = []
                consumed_slices[child] = b""
                if debug:
                    print(f"DEBUG: No field definitions for child {child}")
            else:
                start = data_off
                parsed_fields, new_off = parse_instance_fields(
                    self.data, data_off, info["fields"], debug=debug
                )
                consumed = new_off - start
                # Check if an alignment is required for this child element.
                # We'll assume that each instance must be aligned to 4 bytes by default.
                aligned_off = align_offset(new_off, 4)
                if debug and aligned_off != new_off:
                    print(
                        f"DEBUG: Aligning child {child} from {new_off:#x} to {aligned_off:#x}"
                    )
                consumed = aligned_off - start
                child_consumed[child] = consumed
                parsed_fields_dict[child] = parsed_fields
                consumed_slices[child] = self.data[start:aligned_off]
                data_off = aligned_off
                if debug:
                    print(
                        f"DEBUG: Child {child} parsed; consumed {consumed} bytes; new data_off = {data_off:#x}"
                    )
        if debug:
            print(
                f"DEBUG: Finished parsing all children; total consumed data: {data_off} of {len(self.data)} bytes"
            )

        # Reassemble per group.
        self.data_group_bytes = []
        for main, children in groups:
            group_raw = b"".join(consumed_slices.get(child, b"") for child in children)
            child_sizes = [child_consumed.get(child, 0) for child in children]
            parsed_list = [
                (child, parsed_fields_dict.get(child, [])) for child in children
            ]
            self.data_group_bytes.append(
                (main, children, child_sizes, group_raw, parsed_list)
            )
            if debug:
                print(
                    f"DEBUG: Group for main {main}: children {children}, sizes {child_sizes}"
                )
        if data_off != len(self.data):
            print(
                f"Warning: total consumed data ({data_off}) does not equal Data length ({len(self.data)})"
            )


########################################
# Parsing Instance Fields from JSON
########################################
def align_offset(offset: int, alignment: int) -> int:
    if alignment > 0:
        remainder = offset % alignment
        if remainder:
            return offset + (alignment - remainder)
    return offset


def parse_instance_fields(raw: bytes, offset: int, fields_def: list, debug=True):
    results = []
    pos = offset
    for index, field in enumerate(fields_def):
        field_name = field.get("name", "<unnamed>")
        ftype = field.get("type", "Unknown").lower()
        fsize = field.get("size", 4)

        # === Pre-read Alignment ===
        if "align" in field:
            align_val = int(field["align"])
            new_pos = align_offset(pos, align_val)
            if debug and new_pos != pos:
                print(
                    f"DEBUG: Pre-aligning field '{field_name}' from {pos:#x} to {new_pos:#x} (alignment {align_val})"
                )
            pos = new_pos

        if debug:
            print(
                f"DEBUG: Reading field '{field_name}' (type: {ftype}) at pos {pos:#x}"
            )

        # === Field Reading ===
        if ftype == "bool":
            if pos + 1 > len(raw):
                value = "N/A"
            else:
                value = "True" if raw[pos] != 0 else "False"
                if debug:
                    print(f"DEBUG: Raw bool byte: {raw[pos:pos+1].hex()} -> {value}")
            pos += 1
            subresults = []
        elif ftype in ("s32", "int"):
            if pos + 4 > len(raw):
                value = "N/A"
            else:
                raw_bytes = raw[pos : pos + 4]
                value = struct.unpack_from("<i", raw, pos)[0]
                if debug:
                    print(f"DEBUG: Raw int bytes: {raw_bytes.hex()} -> {value}")
            pos += 4
            subresults = []
        elif ftype in ("u32", "uint"):
            if pos + 4 > len(raw):
                value = "N/A"
            else:
                raw_bytes = raw[pos : pos + 4]
                value = struct.unpack_from("<I", raw, pos)[0]
                if debug:
                    print(f"DEBUG: Raw uint bytes: {raw_bytes.hex()} -> {value}")
            pos += 4
            subresults = []
        elif ftype in ("f32", "float"):
            if pos + 4 > len(raw):
                value = "N/A"
            else:
                raw_bytes = raw[pos : pos + 4]
                value = struct.unpack_from("<f", raw, pos)[0]
                if debug:
                    print(f"DEBUG: Raw float bytes: {raw_bytes.hex()} -> {value}")
            pos += 4
            subresults = []
        elif ftype == "string":
            
            size_field_bytes = field.get("size", 4)
            if debug:
                print(
                    f"DEBUG: Field '{field_name}': expecting size field of {size_field_bytes} bytes at pos {pos:#x}"
                )
            if pos + size_field_bytes > len(raw):
                value = "N/A"
                pos += size_field_bytes
            else:
                size_field_data = raw[pos : pos + size_field_bytes]
                
                size_val = struct.unpack_from("<I", raw, pos)[0]
                str_byte_count = size_val * 2  # UTF-16-LE encoding
                if debug:
                    print(
                        f"DEBUG: Field '{field_name}': size field raw: {size_field_data.hex()} -> {size_val} characters -> {str_byte_count} bytes"
                    )
                pos += size_field_bytes
                if pos + str_byte_count > len(raw):
                    value = "Truncated String"
                    pos = len(raw)
                else:
                    str_data = raw[pos : pos + str_byte_count]
                    if debug:
                        print(
                            f"DEBUG: Field '{field_name}': raw string data: {str_data.hex()}"
                        )
                    value = str_data.decode("utf-16-le", errors="replace")
                    pos += str_byte_count
            subresults = []
        else:
            # Fallback: read fsize bytes and show their hex representation.
            if pos + fsize > len(raw):
                value = "N/A"
            else:
                raw_data = raw[pos : pos + fsize]
                value = raw_data.hex()
                if debug:
                    print(f"DEBUG: Field '{field_name}' raw data: {raw_data.hex()}")
            pos += fsize
            subresults = []

        # Recursively parse any nested fields.
        if "fields" in field and field["fields"]:
            subresults, pos = parse_instance_fields(
                raw, pos, field["fields"], debug=debug
            )

        results.append({"name": field_name, "value": value, "subfields": subresults})

    #  force the entire instance block to align to 4 bytes.
    new_pos = align_offset(pos, 4)
    if debug and new_pos != pos:
        print(f"DEBUG: Final instance alignment: moving from {pos:#x} to {new_pos:#x}")
    pos = new_pos
    return results, pos


########################################
# RcolShape (160 bytes)
########################################


class RcolShape:
    SIZE = 160

    def __init__(self):
        self.guid = b"\x00" * 16
        self.name_offset = 0
        self.name = ""
        self.name_hash = 0
        self.user_data_index = 0
        self.layer_index = 0
        self.attribute = 0
        self.skip_id_bits = 0
        self.ignore_tag_bits = 0
        self.primary_joint_name_offset = 0
        self.primary_joint_name = ""
        self.secondary_joint_name_offset = 0
        self.secondary_joint_name = ""
        self.primary_joint_name_hash = 0
        self.secondary_joint_name_hash = 0
        self.shape_type = 0
        self.parameters = []

    def parse(self, data: bytes, offset: int) -> int:
        if offset + self.SIZE > len(data):
            raise ValueError(f"Truncated shape at 0x{offset:X}")
        self.guid = data[offset : offset + 16]
        offset += 16
        self.name_offset = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        (self.name_hash, self.user_data_index, self.layer_index, self.attribute) = (
            struct.unpack_from("<iiii", data, offset)
        )
        offset += 16
        self.skip_id_bits, self.ignore_tag_bits = struct.unpack_from(
            "<ii", data, offset
        )
        offset += 8
        self.primary_joint_name_offset = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        self.secondary_joint_name_offset = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        self.primary_joint_name_hash, self.secondary_joint_name_hash = (
            struct.unpack_from("<II", data, offset)
        )
        offset += 8
        self.shape_type = struct.unpack_from("<i", data, offset)[0]
        offset += 4
        offset += 4  # skip padding
        self.parameters = list(struct.unpack_from("<20f", data, offset))
        offset += 80
        if 0 < self.name_offset < len(data):
            self.name, _ = read_wstring(data, self.name_offset, 100)
        else:
            self.name = ""
        if 0 < self.primary_joint_name_offset < len(data):
            self.primary_joint_name, _ = read_wstring(
                data, self.primary_joint_name_offset, 100
            )
        else:
            self.primary_joint_name = ""
        if 0 < self.secondary_joint_name_offset < len(data):
            self.secondary_joint_name, _ = read_wstring(
                data, self.secondary_joint_name_offset, 100
            )
        else:
            self.secondary_joint_name = ""
        return offset

    @property
    def guid_str(self):
        return guid_le_to_str(self.guid)


########################################
# RcolGroup (80 bytes)
########################################


class RcolGroup:
    SIZE = 80

    def __init__(self):
        self.group_guid = b"\x00" * 16
        self.name_offset = 0
        self.name_hash = 0
        self.num_shapes = 0
        self.user_data_index = 0
        self.num_mask_guids = 0
        self.shapes_tbl = 0
        self.layer_index = 0
        self.mask_bits = 0
        self.mask_guids_offset = 0
        self.layer_guid = b"\x00" * 16
        self.name = ""
        self.mask_guids = []
        self.shapes = []

    def parse(self, data: bytes, offset: int) -> int:
        end_needed = offset + self.SIZE
        if end_needed > len(data):
            raise ValueError(f"Not enough data to parse group at 0x{offset:X}")
        self.group_guid = data[offset : offset + 16]
        offset += 16
        self.name_offset = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        (self.name_hash, self.num_shapes, self.user_data_index, self.num_mask_guids) = (
            struct.unpack_from("<IIII", data, offset)
        )
        offset += 16
        self.shapes_tbl = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        (self.layer_index, self.mask_bits) = struct.unpack_from("<II", data, offset)
        offset += 8
        self.mask_guids_offset = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        self.layer_guid = data[offset : offset + 16]
        offset += 16
        self._parse_name(data)
        self._parse_mask_guids(data)
        return offset

    def _parse_name(self, data: bytes):
        if 0 < self.name_offset < len(data):
            self.name, _ = read_wstring(data, self.name_offset, 100)
        else:
            self.name = ""

    def _parse_mask_guids(self, data: bytes):
        if self.num_mask_guids <= 0:
            return
        start = self.mask_guids_offset
        end = start + (16 * self.num_mask_guids)
        if end > len(data):
            print(
                f"Warning: mask_guids_offset out of range: 0x{self.mask_guids_offset:X}"
            )
            return
        self.mask_guids = [data[pos : pos + 16] for pos in range(start, end, 16)]

    def _parse_shapes(self, data: bytes):
        if self.num_shapes <= 0:
            return
        off = self.shapes_tbl
        shape_size = RcolShape.SIZE
        needed = off + shape_size * self.num_shapes
        if off >= len(data):
            print(f"Warning: shapes_tbl=0x{self.shapes_tbl:X} out of file bounds.")
            self.num_shapes = 0
            return
        if needed > len(data):
            can_fit = (len(data) - off) // shape_size
            if can_fit < self.num_shapes:
                print(
                    f"Warning: group has {self.num_shapes} shapes but only {can_fit} fit. Clamping."
                )
                self.num_shapes = can_fit
        shapes_list = []
        for _ in range(self.num_shapes):
            shape = RcolShape()
            off = shape.parse(data, off)
            shapes_list.append(shape)
        self.shapes = shapes_list

    @property
    def guid_str(self):
        return guid_le_to_str(self.group_guid)


########################################
# RcolRequestSet (64 bytes)
########################################


class RcolRequestSet:
    SIZE = 48  # New size as specified

    def __init__(self):
        self.req_id = 0
        self.group_index = 0
        self.shape_offset = 0
        self.status = 0
        self.uknA = 0
        self.uknB = 0
        self.name_offset = 0
        self.keyname_offset = 0
        self.keyhash = 0
        self.keyhash2 = 0
        # We'll also save the request’s index (its order)
        self.index = 0
        # For display purposes (you might later resolve the offsets to strings)
        self.name = ""
        self.keyname = ""

    def parse(self, data: bytes, offset: int, index: int) -> int:
        self.index = index
        (
            self.req_id,
            self.group_index,
            self.shape_offset,
            self.status,
            self.uknA,
            self.uknB,
        ) = struct.unpack_from("<iiiiii", data, offset)
        offset += 24
        self.name_offset = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        self.keyname_offset = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        self.keyhash, self.keyhash2 = struct.unpack_from("<II", data, offset)
        offset += 8
        
        self.name = f"0x{self.name_offset:X}"
        self.keyname = f"0x{self.keyname_offset:X}"
        return offset


########################################
# RcolFile: Main parser for .rcol file
########################################


class RcolFile:
    HEADER_SIZE = 88

    def __init__(self):
        self.signature = ""
        self.numGroups = 0
        self.numShapes = 0
        self.numUserData = 0
        self.numRequestSets = 0
        self.maxRequestSetId = 0
        self.numIgnoreTags = 0
        self.numAutoGenerateJoints = 0
        self.userDataSize = 0
        self.status = 0
        self.ukn = 0

        self.groupsPtrTbl = 0
        self.userDataStreamPtr = 0
        self.requestSetTbl = 0
        self.ignoreTagTbl = 0
        self.autoGenerateJointDescTbl = 0

        self.groups = []
        self.request_sets = []
        self.user_data_full = None
        self.tags_data = b""

    def read(self, data: bytes):
        if len(data) < self.HEADER_SIZE:
            raise ValueError("Not enough data for the RCOL header")
        self._parse_header(data)
        self._parse_groups(data)
        self._parse_request_sets(data)
        self._parse_user_data(data)
        self._parse_tags(data)
        self._parse_ignore_tags(data)

    def _parse_header(self, data: bytes):
        sig = struct.unpack_from("<4s", data, 0)[0]
        self.signature = sig.decode("ascii", errors="replace")
        (
            self.numGroups,
            self.numShapes,
            self.numUserData,
            self.numRequestSets,
            self.maxRequestSetId,
            self.numIgnoreTags,
            self.numAutoGenerateJoints,
            self.userDataSize,
            self.status,
        ) = struct.unpack_from("<9I", data, 4)
        self.ukn = struct.unpack_from("<Q", data, 0x28)[0]
        self.groupsPtrTbl = struct.unpack_from("<Q", data, 0x30)[0]
        self.userDataStreamPtr = struct.unpack_from("<Q", data, 0x38)[0]
        self.requestSetTbl = struct.unpack_from("<Q", data, 0x40)[0]
        self.ignoreTagTbl = struct.unpack_from("<Q", data, 0x48)[0]
        self.autoGenerateJointDescTbl = struct.unpack_from("<Q", data, 0x50)[0]

    def _parse_groups(self, data: bytes):
        if self.groupsPtrTbl < len(data):
            off = self.groupsPtrTbl
            group_size = RcolGroup.SIZE
            total = off + group_size * self.numGroups
            if total > len(data):
                raise ValueError("Groups out of file bounds")
            for _ in range(self.numGroups):
                grp = RcolGroup()
                off = grp.parse(data, off)
                self.groups.append(grp)
        else:
            if self.numGroups > 0:
                print(f"Warning: invalid groupsPtrTbl=0x{self.groupsPtrTbl:X}")
        for grp in self.groups:
            grp._parse_shapes(data)

    def _parse_request_sets(self, data: bytes):
        if self.requestSetTbl < len(data):
            off = self.requestSetTbl
            total = off + (RcolRequestSet.SIZE * self.numRequestSets)
            if total > len(data):
                raise ValueError("RequestSets out of file bounds")
            self.request_sets = []
            for i in range(self.numRequestSets):
                rs = RcolRequestSet()
                off = rs.parse(data, off, i)
                self.request_sets.append(rs)
        else:
            if self.numRequestSets > 0:
                print(f"Warning: invalid requestSetTbl=0x{self.requestSetTbl:X}")

    def _parse_user_data(self, data: bytes):
        if self.userDataSize > 0:
            if self.userDataStreamPtr < len(data):
                self.user_data_full = RcolUserDataFull()
                self.user_data_full.type_registry = self.type_registry
                self.user_data_full.parse(
                    data, self.userDataStreamPtr, self.userDataSize
                )
            else:
                print(
                    f"Warning: userDataStreamPtr=0x{self.userDataStreamPtr:X} invalid"
                )

    def _parse_tags(self, data: bytes):
        if 0 <= self.autoGenerateJointDescTbl < len(data):
            self.tags_data = data[self.autoGenerateJointDescTbl :]
        else:
            self.tags_data = b""

    def _parse_ignore_tags(self, data: bytes):
        if self.ignoreTagTbl < len(data):
            off = self.ignoreTagTbl
            self.ignore_tags = []
            for i in range(self.numIgnoreTags):
                tag, off = read_wstring(data, off, 100)  # TODO
                self.ignore_tags.append(tag)
        else:
            self.ignore_tags = []
