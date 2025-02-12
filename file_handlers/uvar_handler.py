import os
import sys
import json
import struct
import uuid
import re
import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from collections import defaultdict

from file_handlers.base_handler import FileHandler
from utils.hash_util import murmur3_hash 
from utils.hex_util import * 

# ---------- Data Structures ----------

class VariableEntry:
    def __init__(self):
        self.guid = None
        self.nameOffset = 0
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
            v.nameOffset = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
            if 0 < v.nameOffset < len(data):
                nm, new_off, cnt = read_null_terminated_wstring(data, self.start_pos + v.nameOffset)
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
            v.sharedStringOffset = v.nameOffset if v.nameOffset in knownOffs else None

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
        # Update the string and its storage requirements.
        var.nameString = new_name
        var.nameMaxWchars = (len(new_name.encode("utf-16le")) + 2) // 2

        # Regenerate the nameHash using murmur3.
        new_hash = murmur3_hash(new_name.encode("utf-16le"))
        var.nameHash = new_hash

        # Now update the global nameHashes array.
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
            data_block.extend(struct.pack("<Q", 0))  # placeholder for nameOffset
            float_offset = header_size + count * 48 + i * 4
            var.floatOffset = float_offset
            data_block.extend(struct.pack("<Q", float_offset))
            data_block.extend(struct.pack("<Q", 0))  # uknOffset placeholder
            combined = (var.typeVal & 0xFFFFFF) | ((var.numBits & 0xFF) << 24)
            data_block.extend(struct.pack("<I", combined))
            data_block.extend(struct.pack("<I", var.nameHash))
        return data_block

    def _build_values_block(self):
        count = len(self.variables)
        values_block = bytearray()
        for i in range(count):
            try:
                f_val = float(self.values[i])
            except IndexError:
                f_val = 0.0
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
            hashdata_block.extend(struct.pack("<QQQQ", guids_offset, guid_map_offset, name_hashes_offset, name_hashmap_offset))
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
            for nh in sorted_nameHashes:
                hashdata_block.extend(struct.pack("<I", nh))
            for nhm in sorted_nameHashMap:
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
            self.variables[i].nameOffset = abs_off

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

def populate_treeview(tree, parent_id, uvar, metadata_map, label="UVAR_File"):
    this_id = tree.insert(parent_id, "end", text=label, values=("",))
    metadata_map[this_id] = {"type": "uvarFile", "object": uvar}

    # Header Section
    hdr_id = tree.insert(this_id, "end", text="Header", values=("",))
    for field in ["version", "magic", "stringsOffset", "dataOffset", "embedsInfoOffset", "hashInfoOffset"]:
        val = getattr(uvar, field)
        node_id = tree.insert(hdr_id, "end", text=field, values=(val,))
        metadata_map[node_id] = {"type": "headerInt", "field": field, "object": uvar}
    if uvar.version < 3:
        node_id = tree.insert(hdr_id, "end", text="unkn64", values=(uvar.unkn64,))
        metadata_map[node_id] = {"type": "headerInt", "field": "unkn64", "object": uvar}
    for field in ["UVARhash", "variableCount", "embedCount"]:
        val = getattr(uvar, field)
        node_id = tree.insert(hdr_id, "end", text=field, values=(val,))
        metadata_map[node_id] = {"type": "headerInt", "field": field, "object": uvar}

    # Data (Variables) Section
    data_id = tree.insert(this_id, "end", text="Data (Variables)", values=("",))
    for i, var in enumerate(uvar.variables):
        var_id = tree.insert(data_id, "end", text=f"Variable[{i}]", values=("",))
        guid_id = tree.insert(var_id, "end", text="GUID", values=(var.guid,))
        metadata_map[guid_id] = {"type": "guid", "varIndex": i, "object": uvar}
        tree.insert(var_id, "end", text="nameOffset", values=(var.nameOffset,))
        name_string_id = tree.insert(var_id, "end", text="nameString", values=(var.nameString,))
        metadata_map[name_string_id] = {"type": "nameString", "varIndex": i, "object": uvar}
        tree.insert(var_id, "end", text="floatOffset", values=(var.floatOffset,))
        tree.insert(var_id, "end", text="uknOffset", values=(var.uknOffset,))
        type_val_id = tree.insert(var_id, "end", text="typeVal", values=(var.typeVal,))
        metadata_map[type_val_id] = {"type": "varTypeVal", "varIndex": i, "object": uvar}
        tree.insert(var_id, "end", text="numBits", values=(var.numBits,))
        name_hash_id = tree.insert(var_id, "end", text="nameHash", values=(var.nameHash,))
        metadata_map[name_hash_id] = {"type": "varNameHash", "varIndex": i, "object": uvar}
        value_text = ""
        if i < len(uvar.values):
            f_val = uvar.values[i]
            i_val = struct.unpack("<I", struct.pack("<f", f_val))[0]
            value_text = f"{f_val:.4f} ({i_val})"
        value_id = tree.insert(var_id, "end", text="Value", values=(value_text,))
        metadata_map[value_id] = {"type": "value", "varIndex": i, "object": uvar}

    # Strings Section
    str_id = tree.insert(this_id, "end", text="Strings", values=("",))
    for i, (off, s) in enumerate(uvar.stringOffsets):
        tree.insert(str_id, "end", text=f"str[{i}]", values=(s,))

    # HashData Section
    hd_id = tree.insert(this_id, "end", text="HashData", values=("",))
    hash_data_offsets_id = tree.insert(hd_id, "end", text="HashDataOffsets", values=(str(uvar.hashDataOffsets),))
    metadata_map[hash_data_offsets_id] = {"type": "headerInt", "field": "hashDataOffsets", "object": uvar}
    guids_id = tree.insert(hd_id, "end", text=f"Guids[{len(uvar.guids)}]", values=("",))
    for i, g in enumerate(uvar.guids):
        tree.insert(guids_id, "end", text=f"[{i}]", values=(g,))
    gm_id = tree.insert(hd_id, "end", text=f"GuidMap[{len(uvar.guidMap)}]", values=("",))
    for i, gm in enumerate(uvar.guidMap):
        tree.insert(gm_id, "end", text=f"[{i}]", values=(gm,))
    nh_id = tree.insert(hd_id, "end", text=f"nameHashes[{len(uvar.nameHashes)}]", values=("",))
    for i, nh in enumerate(uvar.nameHashes):
        tree.insert(nh_id, "end", text=f"[{i}]", values=(nh,))
    nhm_id = tree.insert(hd_id, "end", text=f"nameHashMap[{len(uvar.nameHashMap)}]", values=("",))
    for i, nhm in enumerate(uvar.nameHashMap):
        tree.insert(nhm_id, "end", text=f"[{i}]", values=(nhm,))
    eo_id = tree.insert(this_id, "end", text=f"embedOffsets[{len(uvar.embedOffsets)}]", values=("",))
    for i, eoff in enumerate(uvar.embedOffsets):
        tree.insert(eo_id, "end", text=f"[{i}]", values=(eoff,))
    emb_id = tree.insert(this_id, "end", text=f"Embedded_UVARs[{len(uvar.embeddedUvars)}]", values=("",))
    for i, child in enumerate(uvar.embeddedUvars):
        populate_treeview(tree, emb_id, child, metadata_map, label=f"UVAR_File[{i}]")

class UvarHandler(FileHandler):
    def __init__(self):
        self.uvar = UvarFile()
        self.refresh_tree_callback = None
        self.app = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 48:
            return False
        try:
            struct.unpack_from("<II", data, 0)
            return True
        except Exception:
            return False

    def read(self, data: bytes):
        self.uvar.read(data, 0)

    def rebuild(self) -> bytes:
        return self.uvar.rebuild()

    def populate_treeview(self, tree: ttk.Treeview, parent_id, metadata_map: dict):
        populate_treeview(tree, parent_id, self.uvar, metadata_map, label="UVAR_File")

    def get_context_menu(self, tree: tk.Widget, row_id, meta: dict) -> tk.Menu:
        menu = tk.Menu(tree, tearoff=0)
        if meta is None:
            return menu
        if meta.get("type") == "uvarFile":
            uvar_obj = meta.get("object")
            if uvar_obj:
                if uvar_obj.parent is None:
                    menu.add_command(
                        label="Add Variables...",
                        command=lambda: messagebox.showerror("Error", "Cannot add variables to top-level UVAR.", parent=tree.winfo_toplevel())
                    )
                else:
                    menu.add_command(
                        label="Add Variables...",
                        command=lambda: self._open_add_variables_dialog(uvar_obj, tree.winfo_toplevel())
                    )
        menu.add_command(
            label="Copy",
            command=lambda: self._copy_field(tree, row_id)
        )
        return menu

    def _open_add_variables_dialog(self, target_uvar, parent):
        if target_uvar.parent is None:
            messagebox.showerror("Error", "Cannot add variables to top-level UVAR.", parent=parent)
            return
        if self.app is None:
            messagebox.showerror("Error", "Internal error: app reference is missing.", parent=parent)
            return
        prefix = simpledialog.askstring("Naming Pattern",
                                        "Enter naming prefix for new variables (optional):",
                                        parent=parent)
        count = simpledialog.askinteger("Add Variables",
                                        "Enter number of variables to add:",
                                        parent=parent,
                                        minvalue=1)
        if not count:
            return
        try:
            state = self.app.save_tree_state()
            self.add_variables(target_uvar, prefix, count)
            current = target_uvar
            while current.parent:
                current = current.parent
            rebuilt_data = current.rebuild()
            current.read(rebuilt_data, 0)
            self.app.refresh_tree()
            self.app.restore_tree_state(state)
            parent.update_idletasks()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to add variables: {str(e)}", parent=parent)

    def _copy_field(self, tree: tk.Widget, row_id):
        value = tree.set(row_id, "value")
        if value:
            tree.clipboard_clear()
            tree.clipboard_append(value)

    def handle_edit(self, meta: dict, new_val, old_val, row_id):
        try:
            target = meta.get("object", self.uvar)
            typ = meta.get("type")
            if typ == "value":
                var_index = meta.get("varIndex")
                new_number = float(new_val) if ('.' in new_val or 'e' in new_val.lower()) else int(new_val)
                target.values[var_index] = float(new_number)
            elif typ == "nameString":
                var_index = meta.get("varIndex")
                ok, msg = target.rename_variable_in_place(var_index, new_val)
                if ok:
                    messagebox.showinfo("Success", msg)
                else:
                    messagebox.showerror("Error", msg)
                    return
            elif typ == "headerInt":
                ival = int(new_val)
                field_name = meta.get("field")
                ok, msg = target.patch_header_field_in_place(field_name, ival)
                if ok:
                    messagebox.showinfo("Success", msg)
                else:
                    messagebox.showerror("Error", msg)
                    return
            elif typ == "varTypeVal":
                ival = int(new_val)
                var_index = meta.get("varIndex")
                ok, msg = target.patch_typeVal_in_place(var_index, ival)
                if ok:
                    messagebox.showinfo("Success", msg)
                else:
                    messagebox.showerror("Error", msg)
                    return
            elif typ == "varNameHash":
                var_index = meta.get("varIndex")
                new_key = int(new_val)
                ok, msg = target.patch_nameHash_in_place(var_index, new_key)
                if not ok:
                    messagebox.showerror("Error", msg)
                    return
                canonical_index = None
                for i, mapping in enumerate(target.nameHashMap):
                    if mapping == var_index:
                        canonical_index = i
                        break
                if canonical_index is None:
                    messagebox.showerror("Error", "No canonical mapping found for this variable.")
                    return
                target.nameHashes[canonical_index] = new_key
            elif typ == "nameHashes":
                canonical_index = meta.get("varIndex")
                new_key = int(new_val)
                if any(i != canonical_index and nh == new_key for i, nh in enumerate(target.nameHashes)):
                    messagebox.showerror("Error", "This nameHash value is already assigned to another variable.")
                    return
                target.nameHashes[canonical_index] = new_key
                var_index = target.nameHashMap[canonical_index]
                ok, msg = target.patch_nameHash_in_place(var_index, new_key)
                if not ok:
                    messagebox.showerror("Error", msg)
                    return
            elif typ == "guid":
                try:
                    new_guid = str(uuid.UUID(new_val.strip()))
                except Exception as e:
                    messagebox.showerror("Error", f"Invalid GUID: {new_val}\n{e}")
                    return
                var_index = meta.get("varIndex")
                target.variables[var_index].guid = new_guid
            self.uvar.rebuild()
        except Exception as e:
            messagebox.showerror("Error", f"An exception occurred: {e}")
            return

    def add_variables(self, target, prefix: str, count: int):
        if prefix:
            base_prefix = prefix
            start = 0  # start numbering at 0 
            width = 0  # no zero-padding 
        #  otherwise, if there are existing variables, try to parse the last variable's name.
        elif target.variables:
            last_name = target.variables[-1].nameString.strip()
            m = re.search(r'(.*?)(\d+)$', last_name)
            if m:
                base_prefix = m.group(1)
                start = int(m.group(2)) + 1
                width = len(m.group(2))
            else:
                base_prefix = "Variable"
                start = 0
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

        original_count = target.variableCount
        target.variables.extend(new_vars)
        target.variableCount += count
        title = target.strings[0] if target.strings else ""
        target.strings = [title] + [var.nameString for var in target.variables]
        
        target.guids.extend([v.guid for v in new_vars])
        target.guidMap.extend(range(original_count, original_count + count))
        target.nameHashes.extend([v.nameHash for v in new_vars])
        target.nameHashMap.extend(range(original_count, original_count + count))
        target.values.extend([0.0] * count)

        # Rebuild from top-level UVAR.
        current = target
        while current.parent:
            current = current.parent
        current.rebuild()

    def update_strings(self):
        self.uvar.update_strings()