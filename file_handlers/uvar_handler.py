import struct
import uuid
import re

from collections import defaultdict

from PySide6.QtWidgets import (
    QMenu, QInputDialog, QMessageBox, 
    QTreeWidget, QTreeWidgetItem, QTreeView  
)
from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtGui import QClipboard

from file_handlers.base_handler import FileHandler
from utils.hash_util import murmur3_hash
from utils.hex_util import (
    available, align, read_null_terminated_wstring
)

from .uvar.uvar_treeview import LazyTreeModel

class VariableEntry:
    def __init__(self):
        self.guid = None
        self.name_offset = 0
        self.floatOffset = 0
        self.uknOffset = 0
        self.typeVal = 0
        self.numBits = 0
        self.nameHash = 0
        self.nameString = ""
        self.nameMaxWchars = 0
        self.offset_typeVal = None
        self.offset_nameHash = None
        self.sharedStringOffset = None
        self.sharedStringItemID = None

class UvarFile:
    def __init__(self, raw_data=None):
        self.raw_data = raw_data
        self.start_pos = 0
        self.version = 0
        self.magic = 0
        self.stringsOffset = 0
        self.dataOffset = 0
        self.embedsInfoOffset = 0
        self.hashInfoOffset = 0
        self.UVARhash = 0
        self.variableCount = 0
        self.embedCount = 0
        self.unkn64 = 0
        self.offset_version = None
        self.offset_magic = None
        self.variables = []
        self.strings = []
        self.stringOffsets = []
        self.embedOffsets = []
        self.embeddedUvars = []
        self.hashDataOffsets = [0, 0, 0, 0]
        self.guids = []
        self.guidMap = []
        self.nameHashes = []
        self.nameHashMap = []
        self.offsetLinks = defaultdict(list)
        self.parent = None
        self.values = []

    def update_strings(self):
        title = self.strings[0] if self.strings else ""
        self.strings = [title] + [var.nameString for var in self.variables]
        for child in self.embeddedUvars:
            child.update_strings()

    def read(self, data: bytes, start_pos=0):
        self.raw_data = data
        self.start_pos = start_pos
        offset = start_pos

        if available(data, offset, 8):
            self.offset_version = offset
            self.offset_magic = offset + 4
            self.version, self.magic = struct.unpack_from("<II", data, offset)
            offset += 8
        else:
            return

        if not available(data, offset, 32):
            return
        self.stringsOffset, self.dataOffset, self.embedsInfoOffset, self.hashInfoOffset = struct.unpack_from("<QQQQ", data, offset)
        offset += 32

        if self.version < 3 and available(data, offset, 8):
            self.unkn64 = struct.unpack_from("<Q", data, offset)[0]
            offset += 8

        if available(data, offset, 8):
            self.UVARhash, self.variableCount, self.embedCount = struct.unpack_from("<IHH", data, offset)
            offset += 8

        if self.variableCount > 0 and self.dataOffset < len(data):
            self._read_variables(data, self.start_pos + self.dataOffset)

        if self.stringsOffset != 0:
            self._read_strings(data, self.start_pos + self.stringsOffset, self.variableCount + 1)

        if self.variableCount > 0:
            self._read_values(data)

        if self.embedCount > 0 and self.embedsInfoOffset < len(data):
            self._read_embed_offsets(data)

        self.embeddedUvars = []
        for eoff in self.embedOffsets:
            if eoff < len(data):
                child = UvarFile(data)
                child.parent = self
                child.read(data, eoff)
                self.embeddedUvars.append(child)

        if self.hashInfoOffset != 0 and self.variableCount > 0:
            self._read_hash_data(data)

        self._unify_variables_with_strings()

    def _read_variables(self, data: bytes, var_data_start: int):
        offset = var_data_start
        self.variables = []
        for i in range(self.variableCount):
            if not available(data, offset, 48):
                break
            v = VariableEntry()
            guid_bytes = data[offset:offset+16]
            v.guid = str(uuid.UUID(bytes=bytes(guid_bytes)))
            offset += 16

            v.name_offset = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
            if 0 < v.name_offset < len(data):
                nm, new_off, cnt = read_null_terminated_wstring(data, self.start_pos + v.name_offset)
                v.nameString = nm
                v.nameMaxWchars = cnt

            v.floatOffset, v.uknOffset = struct.unpack_from("<QQ", data, offset)
            offset += 16

            v.offset_typeVal = offset
            combined = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            v.typeVal = combined & 0xFFFFFF
            v.numBits = (combined >> 24) & 0xFF

            v.offset_nameHash = offset
            v.nameHash = struct.unpack_from("<I", data, offset)[0]
            offset += 4

            self.variables.append(v)

    def _read_strings(self, data: bytes, strings_start: int, count: int):
        self.strings = []
        self.stringOffsets = []
        offset = strings_start
        for _ in range(count):
            if offset >= len(data):
                break
            str_start = offset
            s, new_off, _ = read_null_terminated_wstring(data, offset)
            self.strings.append(s)
            self.stringOffsets.append((str_start, s))
            offset = new_off

    def _read_values(self, data: bytes):
        self.values = []
        values_start = self.start_pos + self.dataOffset + self.variableCount * 48
        for i in range(self.variableCount):
            pos = values_start + i * 4
            if available(data, pos, 4):
                self.values.append(struct.unpack_from("<f", data, pos)[0])
            else:
                self.values.append(0.0)

    def _read_embed_offsets(self, data: bytes):
        base = self.start_pos + self.embedsInfoOffset
        cur = base
        self.embedOffsets = []
        for _ in range(self.embedCount):
            if available(data, cur, 8):
                eoff = struct.unpack_from("<Q", data, cur)[0]
                self.embedOffsets.append(eoff)
                cur += 8

    def _read_hash_data(self, data: bytes):
        base = self.start_pos + self.hashInfoOffset
        if not available(data, base, 32):
            return
        self.hashDataOffsets = list(struct.unpack_from("<QQQQ", data, base))
        guid_array_off = self.start_pos + self.hashDataOffsets[0]
        guid_map_off   = self.start_pos + self.hashDataOffsets[1]
        name_hashes_off = self.start_pos + self.hashDataOffsets[2]
        name_hashmap_off = self.start_pos + self.hashDataOffsets[3]

        self.guids = []
        for i in range(self.variableCount):
            pos = guid_array_off + i * 16
            if available(data, pos, 16):
                raw_g = data[pos:pos+16]
                self.guids.append(str(uuid.UUID(bytes=bytes(raw_g))))

        self.guidMap = []
        for i in range(self.variableCount):
            pos = guid_map_off + i * 4
            if available(data, pos, 4):
                val = struct.unpack_from("<I", data, pos)[0]
                self.guidMap.append(val)

        self.nameHashes = []
        for i in range(self.variableCount):
            pos = name_hashes_off + i * 4
            if available(data, pos, 4):
                val = struct.unpack_from("<I", data, pos)[0]
                self.nameHashes.append(val)

        self.nameHashMap = []
        for i in range(self.variableCount):
            pos = name_hashmap_off + i * 4
            if available(data, pos, 4):
                val = struct.unpack_from("<I", data, pos)[0]
                self.nameHashMap.append(val)

    def _unify_variables_with_strings(self):
        if not self.stringOffsets:
            return
        knownOffs = {off for off, s in self.stringOffsets}
        for v in self.variables:
            if v.name_offset in knownOffs:
                v.sharedStringOffset = v.name_offset
            else:
                v.sharedStringOffset = None

    def patch_header_field_in_place(self, fieldname, new_val):
        if fieldname == "version":
            if self.offset_version is None or not available(self.raw_data, self.offset_version, 4):
                return (False, "No valid offset for version.")
            struct.pack_into("<I", self.raw_data, self.offset_version, new_val)
            self.version = new_val
            return (True, f"version updated to {new_val}")

        elif fieldname == "magic":
            if self.offset_magic is None or not available(self.raw_data, self.offset_magic, 4):
                return (False, "No valid offset for magic.")
            struct.pack_into("<I", self.raw_data, self.offset_magic, new_val)
            self.magic = new_val
            return (True, f"magic updated to {new_val}")

        return (False, "Unsupported header field")

    def patch_typeVal_in_place(self, var_index, new_typeVal):
        if not (0 <= var_index < len(self.variables)):
            return (False, "Invalid variable index.")
        v = self.variables[var_index]
        off = v.offset_typeVal
        if off is None or not available(self.raw_data, off, 4):
            return (False, "Offset out of range for typeVal.")
        combined = (new_typeVal & 0xFFFFFF) | (v.numBits << 24)
        struct.pack_into("<I", self.raw_data, off, combined)
        v.typeVal = new_typeVal
        return (True, f"typeVal updated to {new_typeVal}")

    def patch_nameHash_in_place(self, var_index, new_hash):
        if not (0 <= var_index < len(self.variables)):
            return (False, "Invalid variable index.")
        v = self.variables[var_index]
        off = v.offset_nameHash
        if off is None or not available(self.raw_data, off, 4):
            return (False, "Offset out of range for nameHash.")
        struct.pack_into("<I", self.raw_data, off, new_hash)
        v.nameHash = new_hash
        return (True, f"nameHash updated to {new_hash}")


    def rename_variable_in_place(self, var_index, new_name):
        if not (0 <= var_index < len(self.variables)):
            return (False, "Invalid variable index.")
        var = self.variables[var_index]
        var.nameString = new_name
        var.nameMaxWchars = (len(new_name.encode("utf-16le")) + 2) // 2

        new_hash = murmur3_hash(new_name.encode("utf-16le"))
        var.nameHash = new_hash

        canonical_index = None
        for i, mapping in enumerate(self.nameHashMap):
            if mapping == var_index:
                canonical_index = i
                break
        if canonical_index is not None:
            self.nameHashes[canonical_index] = new_hash

        return (True, f"Name updated to {new_name}. New hash: {new_hash}")

    def _build_data_block(self):
        header_size = 48
        count = len(self.variables)
        data_block = bytearray()
        for i, var in enumerate(self.variables):
            data_block.extend(uuid.UUID(var.guid).bytes)
            data_block.extend(struct.pack("<Q", 0))

            float_offset = header_size + count * 48 + i * 4
            var.floatOffset = float_offset
            data_block.extend(struct.pack("<Q", float_offset))
            data_block.extend(struct.pack("<Q", 0))

            combined = (var.typeVal & 0xFFFFFF) | ((var.numBits & 0xFF) << 24)
            data_block.extend(struct.pack("<I", combined))
            data_block.extend(struct.pack("<I", var.nameHash))
        return data_block

    def _build_values_block(self):
        count = len(self.variables)
        values_block = bytearray()
        for i in range(count):
            f_val = float(self.values[i])
            values_block.extend(struct.pack("<f", f_val))
        return values_block

    def _build_strings_block(self):
        strings_block = bytearray()
        file_title = self.strings[0] if self.strings else ""
        strings_block.extend(file_title.encode("utf-16le") + b"\x00\x00")

        relative_string_offsets = []
        for i, var in enumerate(self.variables):
            s = self.strings[i+1] if len(self.strings) > i+1 else var.nameString
            relative_string_offsets.append(len(strings_block))
            s_bytes = s.encode("utf-16le") + b"\x00\x00"
            strings_block.extend(s_bytes)
            var.nameMaxWchars = len(s_bytes) // 2

        return strings_block, relative_string_offsets

    def _build_embed_blocks(self, strings_offset, strings_block_length):
        embed_count = len(self.embeddedUvars)
        if embed_count == 0:
            return 0, bytearray(), bytearray()

        embed_info_offset = align(strings_offset + strings_block_length, 16)
        embed_info_block = bytearray()
        embedded_block = bytearray()

        embed_start = embed_info_offset + embed_count * 8
        for child in self.embeddedUvars:
            child_offset = embed_start + len(embedded_block)
            embed_info_block.extend(struct.pack("<Q", child_offset))
            child_data = child.rebuild()
            embedded_block.extend(child_data)
            pad_len = (16 - (len(embedded_block) % 16)) % 16
            embedded_block.extend(bytearray(b"\x00" * pad_len))

        return embed_info_offset, embed_info_block, embedded_block

    def _build_hashdata_block(self, hash_info_offset):
        count = len(self.variables)
        hashdata_block = bytearray()
        if count > 0:
            hd_header_size = 32
            guids_offset = hash_info_offset + hd_header_size
            guid_map_offset = guids_offset + count * 16
            name_hashes_offset = guid_map_offset + count * 4
            name_hashmap_offset = name_hashes_offset + count * 4

            hashdata_block.extend(struct.pack("<QQQQ",
                                            guids_offset,
                                            guid_map_offset,
                                            name_hashes_offset,
                                            name_hashmap_offset))
            sorted_guid_pairs = sorted(
                ((self.guids[i], self.guidMap[i]) for i in range(count)),
                key=lambda pair: uuid.UUID(pair[0]).bytes_le
            )
            sorted_guids = [pair[0] for pair in sorted_guid_pairs]
            sorted_guidMap = [pair[1] for pair in sorted_guid_pairs]
            self.guids = sorted_guids
            self.guidMap = sorted_guidMap

            for guid_str in self.guids:
                hashdata_block.extend(uuid.UUID(guid_str).bytes)
            for idx in self.guidMap:
                hashdata_block.extend(struct.pack("<I", idx))

            sorted_pairs = sorted(
                ((self.nameHashes[i], self.nameHashMap[i]) for i in range(count)),
                key=lambda pair: pair[0]
            )
            sorted_nameHashes = [pair[0] for pair in sorted_pairs]
            sorted_nameHashMap = [pair[1] for pair in sorted_pairs]
            self.nameHashes = sorted_nameHashes
            self.nameHashMap = sorted_nameHashMap

            for nh in self.nameHashes:
                hashdata_block.extend(struct.pack("<I", nh))
            for nhm in self.nameHashMap:
                hashdata_block.extend(struct.pack("<I", nhm))
        return hashdata_block

    def rebuild(self) -> bytes:
        self.update_strings()

        header_size = 48
        count = len(self.variables)
        embed_count = len(self.embeddedUvars)

        data_block = self._build_data_block()
        values_block = self._build_values_block()

        end_of_values = header_size + len(data_block) + len(values_block)
        strings_offset = align(end_of_values, 16)
        pad_after_values = bytearray(b"\x00" * (strings_offset - end_of_values))

        strings_block, relative_string_offsets = self._build_strings_block()

        self.stringOffsets = []
        self.stringOffsets.append((strings_offset, self.strings[0] if self.strings else ""))
        for i, rel_off in enumerate(relative_string_offsets):
            abs_off = strings_offset + rel_off
            s = self.strings[i+1] if i+1 < len(self.strings) else ""
            self.stringOffsets.append((abs_off, s))

        for i in range(count):
            abs_off = strings_offset + relative_string_offsets[i]
            struct.pack_into("<Q", data_block, i * 48 + 16, abs_off)
            self.variables[i].name_offset = abs_off

        if embed_count > 0:
            embed_info_offset, embed_info_block, embedded_block = self._build_embed_blocks(strings_offset, len(strings_block))
            hash_info_offset = embed_info_offset + len(embed_info_block) + len(embedded_block)
        else:
            embed_info_offset = 0
            embed_info_block = bytearray()
            embedded_block = bytearray()
            hash_info_offset = align(strings_offset + len(strings_block), 16)

        hash_info_offset = align(hash_info_offset, 16)
        hashdata_block = self._build_hashdata_block(hash_info_offset)

        header = bytearray()
        header.extend(struct.pack("<I", self.version))
        header.extend(struct.pack("<I", self.magic))
        header.extend(struct.pack("<Q", strings_offset))
        header.extend(struct.pack("<Q", header_size))
        header.extend(struct.pack("<Q", embed_info_offset))
        header.extend(struct.pack("<Q", hash_info_offset))
        header.extend(struct.pack("<I", self.UVARhash))
        header.extend(struct.pack("<H", count))
        header.extend(struct.pack("<H", embed_count))

        final_file = bytearray()
        final_file.extend(header)
        final_file.extend(data_block)
        final_file.extend(values_block)
        final_file.extend(pad_after_values)
        final_file.extend(strings_block)

        if embed_count > 0:
            current_end = strings_offset + len(strings_block)
            pad = embed_info_offset - current_end
            final_file.extend(bytearray(b"\x00" * pad))
            final_file.extend(embed_info_block)
            final_file.extend(embedded_block)

        if len(final_file) < hash_info_offset:
            final_file.extend(bytearray(b"\x00" * (hash_info_offset - len(final_file))))
        final_file.extend(hashdata_block)

        self.raw_data = final_file
        return final_file

class UvarHandler(FileHandler):
    def __init__(self):
        self.uvar = UvarFile()
        self.refresh_tree_callback = None
        self.app = None
        self.current_tree = None
        self._last_model = None
        self.metadata_map = {}

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 12:
            return False
        signature = data[4:8].decode('ascii', errors='ignore')
        return signature == 'uvar'

    def read(self, data: bytes):
        self.uvar.read(data, 0)

    def rebuild(self) -> bytes:
        return self.uvar.rebuild()

    def populate_treeview(self, tree: QTreeView, parent_item=None, metadata_map: dict = None):
        root_data = {
            "text": "UVAR_File",
            "data": ["Name", "Value"],
            "columns": ["Name", "Value"],
            "children": [],
            "meta": {"type": "uvarFile", "object": self.uvar}
        }
        uvar_node = self.build_lazy_tree(self.uvar, "UVAR_File")
        root_data["children"].append(uvar_node)
        
        model = LazyTreeModel(root_data)
        tree.setModel(model)
        
        header = tree.header()
        header.setMinimumSectionSize(150)
        header.setStretchLastSection(True)
        header.resizeSection(0, 200)
        
        root_index = model.index(0, 0, QModelIndex())
        tree.expand(root_index)

    def refresh_tree(self, tree_view: QTreeView, metadata_map: dict):
        tree_view.setHeaderHidden(False)
        self.current_tree = tree_view
        self.populate_treeview(tree_view, None, metadata_map)

    def build_lazy_tree(self, uvar, label: str) -> dict:
        node = {
            "text": label,
            "data": [label, ""],
            "children": [],
            "meta": {"type": "uvarFile", "object": uvar}
        }
        header = {"text": "Header", "data": ["Header", ""], "children": []}
        for field in ["version", "magic", "stringsOffset", "dataOffset", "embedsInfoOffset", "hashInfoOffset"]:
            header["children"].append({
                "text": field,
                "data": [field, str(getattr(uvar, field, ""))]
            })
        if uvar.version < 3:
            header["children"].append({
                "text": "unkn64",
                "data": ["unkn64", str(uvar.unkn64)]
            })
        for field in ["UVARhash", "variableCount", "embedCount"]:
            header["children"].append({
                "text": field,
                "data": [field, str(getattr(uvar, field, ""))]
            })
        node["children"].append(header)
        
        data_node = {"text": "Data (Variables)", "data": ["Data (Variables)", ""], "children": []}
        for i, var in enumerate(uvar.variables):
            var_node = {
                "text": f"Variable[{i}]",
                "data": [f"Variable[{i}]", ""],
                "children": [],
                "meta": {"type": "variable", "varIndex": i, "object": uvar}
            }
            var_node["children"].append({"text": "GUID", "data": ["GUID", var.guid]})
            var_node["children"].append({"text": "nameOffset", "data": ["nameOffset", str(var.name_offset)]})
            var_node["children"].append({"text": "nameString", "data": ["nameString", var.nameString]})
            var_node["children"].append({"text": "floatOffset", "data": ["floatOffset", str(var.floatOffset)]})
            var_node["children"].append({"text": "uknOffset", "data": ["uknOffset", str(var.uknOffset)]})
            var_node["children"].append({"text": "typeVal", "data": ["typeVal", str(var.typeVal)]})
            var_node["children"].append({"text": "numBits", "data": ["numBits", str(var.numBits)]})
            var_node["children"].append({"text": "nameHash", "data": ["nameHash", str(var.nameHash)]})
            if i < len(uvar.values):
                f_val = uvar.values[i]
            else:
                f_val = 0.0
            i_val = struct.unpack("<I", struct.pack("<f", f_val))[0]
            var_node["children"].append({"text": "Value (float)", "data": ["Value (float)", f"{f_val:.4f}"]})
            var_node["children"].append({"text": "Value (int)", "data": ["Value (int)", str(i_val)]})
            data_node["children"].append(var_node)
        node["children"].append(data_node)
        
        hash_node = {"text": "HashData", "data": ["HashData", ""], "children": []}
        hash_node["children"].append({"text": "HashDataOffsets", "data": ["HashDataOffsets", str(uvar.hashDataOffsets)]})
        guids_node = {"text": f"Guids[{len(uvar.guids)}]", "data": [f"Guids[{len(uvar.guids)}]", ""], "children": []}
        for i, g in enumerate(uvar.guids):
            guids_node["children"].append({"text": f"[{i}]", "data": [f"[{i}]", g]})
        hash_node["children"].append(guids_node)
        guidmap_node = {"text": f"GuidMap[{len(uvar.guidMap)}]", "data": [f"GuidMap[{len(uvar.guidMap)}]", ""], "children": []}
        for i, gm in enumerate(uvar.guidMap):
            guidmap_node["children"].append({"text": f"[{i}]", "data": [f"[{i}]", str(gm)]})
        hash_node["children"].append(guidmap_node)
        nh_node = {"text": f"nameHashes[{len(uvar.nameHashes)}]", "data": [f"nameHashes[{len(uvar.nameHashes)}]", ""], "children": []}
        for i, nh in enumerate(uvar.nameHashes):
            nh_node["children"].append({"text": f"[{i}]", "data": [f"[{i}]", str(nh)]})
        hash_node["children"].append(nh_node)
        nhm_node = {"text": f"nameHashMap[{len(uvar.nameHashMap)}]", "data": [f"nameHashMap[{len(uvar.nameHashMap)}]", ""], "children": []}
        for i, nhm in enumerate(uvar.nameHashMap):
            nhm_node["children"].append({"text": f"[{i}]", "data": [f"[{i}]", str(nhm)]})
        hash_node["children"].append(nhm_node)
        e_offsets_node = {"text": f"embedOffsets[{len(uvar.embedOffsets)}]", "data": [f"embedOffsets[{len(uvar.embedOffsets)}]", ""], "children": []}
        for i, eoff in enumerate(uvar.embedOffsets):
            e_offsets_node["children"].append({"text": f"[{i}]", "data": [f"[{i}]", str(eoff)]})
        hash_node["children"].append(e_offsets_node)
        node["children"].append(hash_node)
        
        if uvar.embeddedUvars:
            embedded_node = {"text": f"Embedded UVARs [{len(uvar.embeddedUvars)}]", "data": ["Embedded UVARs", ""], "children": []}
            for i, child in enumerate(uvar.embeddedUvars):
                child_node = self.build_lazy_tree(child, f"UVAR_File[{i}]")
                embedded_node["children"].append(child_node)
            node["children"].append(embedded_node)
        
        return node

    def get_context_menu(self, tree: QTreeWidget, item: QTreeWidgetItem, meta: dict) -> QMenu:
        menu = QMenu(tree)
        if meta is None:
            return menu

        if meta.get("type") == "variable" or "varIndex" in meta:
            delete_action = menu.addAction("Delete Variable")
            delete_action.triggered.connect(
                lambda: self._delete_variable(meta["object"], meta["varIndex"], tree)
            )

        if meta.get("type") == "uvarFile":
            uvar_obj = meta.get("object")
            if uvar_obj:
                add_vars_action = menu.addAction("Add Variables...")
                add_vars_action.triggered.connect(
                    lambda: self._open_add_variables_dialog(uvar_obj, tree)
                )

        copy_action = menu.addAction("Copy")
        copy_action.triggered.connect(lambda: self._copy_field(tree, item))

        return menu

    def refresh_ui(self):
        if self.refresh_tree_callback:
            self.refresh_tree_callback()
  
  
    def _open_add_variables_dialog(self, target_uvar, parent_widget):
        if self.app is None:
            QMessageBox.critical(parent_widget, "Error", "Internal error: app reference is missing.")
            return

        prefix, ok = QInputDialog.getText(parent_widget, "Naming Pattern",
                                          "Enter naming prefix for new variables (optional):")
        if not ok:
            return

        count, ok2 = QInputDialog.getInt(
            parent_widget,
            "Add Variables",
            "Enter number of variables to add:",
            1,
            1,
            65535,
            1
        )
        if not ok2 or count < 1:
            return

        try:
            state = self.save_tree_state(parent_widget)

            self.add_variables(target_uvar, prefix, count)

            current = target_uvar
            while current.parent:
                current = current.parent
            rebuilt_data = current.rebuild()
            current.read(rebuilt_data, 0)

            self.refresh_ui()

            self.restore_tree_state(parent_widget, state)

        except Exception as e:
            QMessageBox.critical(parent_widget, "Error", f"Failed to add variables: {str(e)}")

    def _copy_field(self, tree: QTreeWidget, item: QTreeWidgetItem):
        value = item.text(1)
        if value:
            QClipboard().setText(value)

    def handle_edit(self, meta: dict, new_val, old_val, item=None, parent_widget=None):
        if parent_widget is None:
            parent_widget = item.treeWidget()

        try:
            target = meta.get("object", self.uvar)
            typ = meta.get("type")

            if typ == "value_float":
                var_index = meta.get("varIndex")
                target.values[var_index] = float(new_val)

            elif typ == "value_int":
                var_index = meta.get("varIndex")
                int_val = int(new_val)
                target.values[var_index] = struct.unpack("<f", struct.pack("<I", int_val))[0]

            elif typ == "nameString":
                var_index = meta.get("varIndex")
                ok, msg = target.rename_variable_in_place(var_index, new_val)
                if not ok:
                    QMessageBox.critical(parent_widget, "Error", msg)
                    return
                else:
                    QMessageBox.information(parent_widget, "Success", msg)

            elif typ == "headerInt":
                ival = int(new_val)
                field_name = meta.get("field")
                ok, msg = target.patch_header_field_in_place(field_name, ival)
                if not ok:
                    QMessageBox.critical(parent_widget, "Error", msg)
                    return
                else:
                    QMessageBox.information(parent_widget, "Success", msg)

            elif typ == "varTypeVal":
                var_index = meta.get("varIndex")
                ival = int(new_val)
                ok, msg = target.patch_typeVal_in_place(var_index, ival)
                if not ok:
                    QMessageBox.critical(parent_widget, "Error", msg)
                    return
                else:
                    QMessageBox.information(parent_widget, "Success", msg)

            elif typ == "varNameHash":
                var_index = meta.get("varIndex")
                new_key = int(new_val)
                ok, msg = target.patch_nameHash_in_place(var_index, new_key)
                if not ok:
                    QMessageBox.critical(parent_widget, "Error", msg)
                    return
                canonical_index = None
                for i, mapping in enumerate(target.nameHashMap):
                    if mapping == var_index:
                        canonical_index = i
                        break
                if canonical_index is None:
                    QMessageBox.critical(parent_widget, "Error", "No canonical mapping found for this variable.")
                    return
                target.nameHashes[canonical_index] = new_key

            elif typ == "guid":
                try:
                    new_guid = str(uuid.UUID(new_val.strip()))
                except Exception as e:
                    QMessageBox.critical(parent_widget, "Error", f"Invalid GUID: {new_val}\n{e}")
                    return
                var_index = meta.get("varIndex")
                target.variables[var_index].guid = new_guid

            target.guids = [v.guid for v in target.variables]
            target.guidMap = list(range(len(target.variables)))
            target.nameHashes = [v.nameHash for v in target.variables]
            target.nameHashMap = list(range(len(target.variables)))

            target.update_strings()

            current = target
            while current.parent:
                current = current.parent
            rebuilt_data = current.rebuild()
            current.read(rebuilt_data, 0)

            self.refresh_ui()

        except Exception as e:
            QMessageBox.critical(parent_widget, "Error", f"An exception occurred: {e}")
            return
        
    def save_tree_state(self, tree_view: QTreeView) -> dict:
        state = {}
        state["scrollValue"] = tree_view.verticalScrollBar().value()
        
        model = tree_view.model()
        if not model:
            return state
            
        def store_expanded(index, expanded):
            if tree_view.isExpanded(index):
                expanded.append(self._get_path_from_index(index))
            for row in range(model.rowCount(index)):
                child_index = model.index(row, 0, index)
                store_expanded(child_index, expanded)
                
        expanded_paths = []
        root_index = QModelIndex()
        for row in range(model.rowCount(root_index)):
            index = model.index(row, 0, root_index)
            store_expanded(index, expanded_paths)
            
        state["expanded"] = expanded_paths
        return state

    def restore_tree_state(self, tree_view: QTreeView, state: dict):
        if not state:
            return
            
        model = tree_view.model()
        if not model:
            return
            
        expanded = state.get("expanded", [])
        for path in expanded:
            index = self._get_index_from_path(model, path)
            if index.isValid():
                tree_view.setExpanded(index, True)
                
        if "scrollValue" in state:
            tree_view.verticalScrollBar().setValue(state["scrollValue"])

    def _get_path_from_index(self, index):
        path = []
        while index.isValid():
            path.append(index.data(Qt.DisplayRole))
            index = index.parent()
        return list(reversed(path))

    def _get_index_from_path(self, model, path):
        index = QModelIndex()
        for text in path:
            found = False
            for row in range(model.rowCount(index)):
                child = model.index(row, 0, index)
                if child.data(Qt.DisplayRole) == text:
                    index = child
                    found = True
                    break
            if not found:
                return QModelIndex()
        return index

    def _delete_variable(self, target_uvar, var_index, parent_widget):
        ret = QMessageBox.question(
            parent_widget, 
            "Confirm Deletion", 
            f"Are you sure you want to delete variable at index {var_index}?",
            QMessageBox.Yes | QMessageBox.No
        )
        if ret != QMessageBox.Yes:
            return

        try:
            del target_uvar.variables[var_index]
            target_uvar.variableCount = len(target_uvar.variables)

            title = target_uvar.strings[0] if target_uvar.strings else ""
            target_uvar.strings = [title] + [v.nameString for v in target_uvar.variables]

            target_uvar.guids = [v.guid for v in target_uvar.variables]
            target_uvar.guidMap = list(range(target_uvar.variableCount))
            target_uvar.nameHashes = [v.nameHash for v in target_uvar.variables]
            target_uvar.nameHashMap = list(range(target_uvar.variableCount))

            if len(target_uvar.values) > target_uvar.variableCount:
                target_uvar.values = target_uvar.values[:target_uvar.variableCount]
            while len(target_uvar.values) < target_uvar.variableCount:
                target_uvar.values.append(0.0)

            current = target_uvar
            while current.parent:
                current = current.parent
            current.rebuild()

            self.refresh_ui()

        except Exception as e:
            QMessageBox.critical(parent_widget, "Error", f"Failed to delete variable: {e}")

    def add_variables(self, target_uvar, prefix: str, count: int):
        MAX_VARIABLES = 65535
        if target_uvar.variableCount + count > MAX_VARIABLES:
            raise ValueError(f"Cannot add variables: adding {count} would exceed max {MAX_VARIABLES}.")

        if prefix:
            base_prefix = prefix
            start = 1
            width = 0
        elif target_uvar.variables:
            last_name = target_uvar.variables[-1].nameString.strip()
            m = re.search(r'(.*?)(\d+)$', last_name)
            if m:
                base_prefix = m.group(1)
                start = int(m.group(2)) + 1
                width = len(m.group(2))
            else:
                base_prefix = last_name + "_"
                start = 1
                width = 0
        else:
            base_prefix = "Variable"
            start = 1
            width = 0

        new_vars = []
        for i in range(count):
            var = VariableEntry()
            var.guid = str(uuid.uuid4())
            num_str = f"{start + i:0{width}d}" if width else str(start + i)
            new_name = f"{base_prefix}{num_str}"
            var.nameString = new_name
            var.nameHash = murmur3_hash(new_name.encode("utf-16le"))
            var.typeVal = 2
            var.numBits = 0
            new_vars.append(var)

        original_count = target_uvar.variableCount
        target_uvar.variables.extend(new_vars)
        target_uvar.variableCount += count

        title = target_uvar.strings[0] if target_uvar.strings else ""
        target_uvar.strings = [title] + [v.nameString for v in target_uvar.variables]

        target_uvar.guids.extend([v.guid for v in new_vars])
        target_uvar.guidMap.extend(range(original_count, original_count + count))
        target_uvar.nameHashes.extend([v.nameHash for v in new_vars])
        target_uvar.nameHashMap.extend(range(original_count, original_count + count))
        target_uvar.values.extend([0.0] * count)

        current = target_uvar
        while current.parent:
            current = current.parent
        current.rebuild()

    def update_strings(self):
        self.uvar.update_strings()

    def validate_edit(self, meta: dict, new_val: str, old_val: str = None) -> bool:
        try:
            typ = meta.get("type")

            if typ == "value_float":
                float(new_val)
            elif typ == "value_int":
                int(new_val)
            elif typ == "nameString":
                if not new_val or len(new_val) > 255:
                    return False
            elif typ == "headerInt":
                int(new_val)
            elif typ == "varTypeVal":
                val = int(new_val)
                if not (0 <= val <= 0xFFFFFF):
                    return False
            elif typ == "varNameHash":
                val = int(new_val)
                if not (0 <= val <= 0xFFFFFFFF):
                    return False
            elif typ == "guid":
                uuid.UUID(new_val.strip())
            else:
                return False

            return True

        except (ValueError, TypeError, uuid.UUID.Error):
            return False






