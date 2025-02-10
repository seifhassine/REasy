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

# ---------- Utility Functions ----------

def align(offset, alignment=16):
    r = offset % alignment
    return offset if r == 0 else offset + (alignment - r)

def read_null_terminated_wstring(data, offset, max_chars=65535):
    chars = []
    pos = offset
    count = 0
    for _ in range(max_chars):
        if pos + 2 > len(data):
            break
        val = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        count += 1
        if val == 0:
            break
        chars.append(val)
    return "".join(chr(c) for c in chars), pos, count

def compute_namehash(s):
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h

def rotl32(x, r):
    return ((x << r) | (x >> (32 - r))) & 0xffffffff

def fmix(h):
    h ^= h >> 16
    h = (h * 0x85ebca6b) & 0xffffffff
    h ^= h >> 13
    h = (h * 0xc2b2ae35) & 0xffffffff
    h ^= h >> 16
    return h

# Credits: https://github.com/TrikzMe/RE-Engine-Hash-tool/

def murmur3_hash(data):
    c1 = 0xcc9e2d51
    c2 = 0x1b873593
    seed = 0xffffffff
    h1 = seed
    stream_length = 0
    i = 0
    n = len(data)
    while i < n:
        chunk = data[i:i+4]
        i += len(chunk)
        stream_length += len(chunk)
        if len(chunk) == 4:
            k1 = (chunk[0] | (chunk[1] << 8) | (chunk[2] << 16) | (chunk[3] << 24))
            k1 = (k1 * c1) & 0xffffffff
            k1 = rotl32(k1, 15)
            k1 = (k1 * c2) & 0xffffffff
            h1 ^= k1
            h1 = rotl32(h1, 13)
            h1 = (h1 * 5 + 0xe6546b64) & 0xffffffff
        elif len(chunk) == 3:
            k1 = (chunk[0] | (chunk[1] << 8) | (chunk[2] << 16))
            k1 = (k1 * c1) & 0xffffffff
            k1 = rotl32(k1, 15)
            k1 = (k1 * c2) & 0xffffffff
            h1 ^= k1
        elif len(chunk) == 2:
            k1 = (chunk[0] | (chunk[1] << 8))
            k1 = (k1 * c1) & 0xffffffff
            k1 = rotl32(k1, 15)
            k1 = (k1 * c2) & 0xffffffff
            h1 ^= k1
        elif len(chunk) == 1:
            k1 = chunk[0]
            k1 = (k1 * c1) & 0xffffffff
            k1 = rotl32(k1, 15)
            k1 = (k1 * c2) & 0xffffffff
            h1 ^= k1
    h1 ^= stream_length
    h1 = fmix(h1)
    return h1

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
        """
        Update the internal strings list from the current variables.
        Preserve the first element (the title) if it exists.
        Propagate the update recursively for embedded UVARs.
        """
        if self.strings:
            title = self.strings[0]
        else:
            title = ""
        self.strings = [title] + [var.nameString for var in self.variables]
        for child in self.embeddedUvars:
            child.update_strings()

    def read(self, data, start_pos=0):
        self.raw_data = data
        self.start_pos = start_pos
        offset = start_pos
        if offset + 8 <= len(data):
            self.offset_version = offset
            self.offset_magic = offset + 4
            self.version, self.magic = struct.unpack_from("<II", data, offset)
            offset += 8
        else:
            return
        if offset + 32 > len(data):
            return
        (self.stringsOffset, self.dataOffset,
         self.embedsInfoOffset, self.hashInfoOffset) = struct.unpack_from("<QQQQ", data, offset)
        offset += 32
        if self.version < 3 and offset + 8 <= len(data):
            self.unkn64 = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
        if offset + 8 <= len(data):
            self.UVARhash, self.variableCount, self.embedCount = struct.unpack_from("<IHH", data, offset)
            offset += 8
        if self.variableCount > 0 and self.dataOffset < len(data):
            var_data_start = self.start_pos + self.dataOffset
            self._read_variables(data, var_data_start)
        if self.stringsOffset != 0:
            self._read_strings(data, self.start_pos + self.stringsOffset, self.variableCount + 1)
        if self.variableCount > 0:
            values_start = self.start_pos + self.dataOffset + self.variableCount * 48
            self.values = []
            for i in range(self.variableCount):
                pos = values_start + i * 4
                if pos + 4 <= len(data):
                    val = struct.unpack_from("<f", data, pos)[0]
                    self.values.append(val)
                else:
                    self.values.append(0.0)
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

    def _read_variables(self, data, var_data_start):
        offset = var_data_start
        self.variables = []
        for i in range(self.variableCount):
            if offset + 48 > len(data):
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
            v.typeVal = (combined & 0xFFFFFF)
            v.numBits = (combined >> 24) & 0xFF
            v.offset_nameHash = offset
            v.nameHash = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            self.variables.append(v)

    def _read_strings(self, data, strings_start, count):
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

    def _read_embed_offsets(self, data):
        base = self.start_pos + self.embedsInfoOffset
        cur = base
        self.embedOffsets = []
        for _ in range(self.embedCount):
            if cur + 8 <= len(data):
                eoff = struct.unpack_from("<Q", data, cur)[0]
                self.embedOffsets.append(eoff)
                cur += 8

    def _read_hash_data(self, data):
        base = self.start_pos + self.hashInfoOffset
        if base + 32 > len(data):
            return
        self.hashDataOffsets = list(struct.unpack_from("<QQQQ", data, base))
        guid_array_off = self.start_pos + self.hashDataOffsets[0]
        guid_map_off   = self.start_pos + self.hashDataOffsets[1]
        name_hashes_off = self.start_pos + self.hashDataOffsets[2]
        name_hashmap_off = self.start_pos + self.hashDataOffsets[3]
        self.guids = []
        for i in range(self.variableCount):
            pos = guid_array_off + i * 16
            if pos + 16 <= len(data):
                raw_g = data[pos:pos+16]
                self.guids.append(str(uuid.UUID(bytes=bytes(raw_g))))
        self.guidMap = []
        for i in range(self.variableCount):
            pos = guid_map_off + i * 4
            if pos + 4 <= len(data):
                val = struct.unpack_from("<I", data, pos)[0]
                self.guidMap.append(val)
        self.nameHashes = []
        for i in range(self.variableCount):
            pos = name_hashes_off + i * 4
            if pos + 4 <= len(data):
                val = struct.unpack_from("<I", data, pos)[0]
                self.nameHashes.append(val)
        self.nameHashMap = []
        for i in range(self.variableCount):
            pos = name_hashmap_off + i * 4
            if pos + 4 <= len(data):
                val = struct.unpack_from("<I", data, pos)[0]
                self.nameHashMap.append(val)

    def _unify_variables_with_strings(self):
        if not self.stringOffsets:
            return
        knownOffs = {o for (o, s) in self.stringOffsets}
        for v in self.variables:
            v.sharedStringOffset = v.nameOffset if v.nameOffset in knownOffs else None

    def patch_header_field_in_place(self, fieldname, new_val):
        if fieldname == "version":
            if self.offset_version is None or self.offset_version + 4 > len(self.raw_data):
                return (False, "No valid offset for version.")
            struct.pack_into("<I", self.raw_data, self.offset_version, new_val)
            self.version = new_val
            return (True, f"version updated to {new_val}")
        elif fieldname == "magic":
            if self.offset_magic is None or self.offset_magic + 4 > len(self.raw_data):
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
        if off is None or off + 4 > len(self.raw_data):
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
        if off is None or off + 4 > len(self.raw_data):
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
        return (True, f"Name updated to {new_name}")

    def rebuild(self):
        # First, update strings from the current variables.
        self.update_strings()
        
        header_size = 48
        count = len(self.variables)
        embed_count = len(self.embeddedUvars)
        data_block = bytearray()
        
        # Build Data Block: one 48-byte block per variable.
        for i, var in enumerate(self.variables):
            data_block.extend(uuid.UUID(var.guid).bytes)
            # Placeholder for nameOffset (will be updated later)
            data_block.extend(struct.pack("<Q", 0))
            # Calculate floatOffset and update in-memory.
            float_offset = header_size + count * 48 + i * 4
            var.floatOffset = float_offset
            data_block.extend(struct.pack("<Q", float_offset))
            data_block.extend(struct.pack("<Q", 0))  # uknOffset (placeholder)
            combined = (var.typeVal & 0xFFFFFF) | ((var.numBits & 0xFF) << 24)
            data_block.extend(struct.pack("<I", combined))
            data_block.extend(struct.pack("<I", var.nameHash))
        
        # Build Values Block.
        values_block = bytearray()
        for i in range(count):
            try:
                f_val = float(self.values[i])
            except IndexError:
                f_val = 0.0
            values_block.extend(struct.pack("<f", f_val))
        
        # Determine the offset for the Strings Block.
        end_of_values = header_size + len(data_block) + len(values_block)
        strings_offset = align(end_of_values, 16)
        pad_after_values = bytearray(b"\x00" * (strings_offset - end_of_values))
        
        # Build Strings Block.
        strings_block = bytearray()
        file_title = self.strings[0] if self.strings else ""
        strings_block.extend(file_title.encode("utf-16le") + b"\x00\x00")
        # record the relative offsets (within strings_block) for each variable name.
        relative_string_offsets = []
        for i, var in enumerate(self.variables):
            # Use the updated name from self.strings (which is is already rebuilt in update_strings()).
            s = self.strings[i+1] if len(self.strings) > i+1 else var.nameString
            relative_string_offsets.append(len(strings_block))
            s_bytes = s.encode("utf-16le") + b"\x00\x00"
            strings_block.extend(s_bytes)
            var.nameMaxWchars = len(s_bytes) // 2

        # **Update self.stringOffsets** (which is used by populate_treeview)
        self.stringOffsets = []
        # The first string (title) is at offset strings_offset.
        self.stringOffsets.append((strings_offset, file_title))
        for i, rel_off in enumerate(relative_string_offsets):
            abs_off = strings_offset + rel_off
            s = self.strings[i+1] if i+1 < len(self.strings) else ""
            self.stringOffsets.append((abs_off, s))
        
        # Now, update each variable’s nameOffset in the data block.
        for i in range(count):
            abs_off = strings_offset + relative_string_offsets[i]
            struct.pack_into("<Q", data_block, i * 48 + 16, abs_off)
            self.variables[i].nameOffset = abs_off

        # Build embed info and embedded UVARs blocks.
        if embed_count > 0:
            embed_info_offset = align(strings_offset + len(strings_block), 16)
            embed_info_block = bytearray()
            embedded_block = bytearray()
            embed_start = embed_info_offset + embed_count * 8
            for child in self.embeddedUvars:
                child_offset = embed_start + len(embedded_block)
                embed_info_block.extend(struct.pack("<Q", child_offset))
                child_data = child.rebuild()  # recursively rebuild child
                embedded_block.extend(child_data)
                padding_needed = (16 - (len(embedded_block) % 16)) % 16
                embedded_block.extend(bytearray(b"\x00" * padding_needed))
        else:
            embed_info_offset = 0
            embed_info_block = bytearray()
            embedded_block = bytearray()

        if embed_count > 0:
            hash_info_offset = embed_info_offset + len(embed_info_block) + len(embedded_block)
        else:
            hash_info_offset = align(strings_offset + len(strings_block), 16)
        hash_info_offset = align(hash_info_offset, 16)
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
                name_hashmap_offset
            ))
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
        current_size = len(final_file)
        if current_size < hash_info_offset:
            final_file.extend(bytearray(b"\x00" * (hash_info_offset - current_size)))
        final_file.extend(hashdata_block)
        self.raw_data = final_file
        return final_file

# ---------- Treeview Population Function ----------

def populate_treeview(tree, parent_id, uvar: UvarFile, metadata_map, label="UVAR_File"):
    this_id = tree.insert(parent_id, "end", text=label, values=("",))
    metadata_map[this_id] = {"type": "uvarFile", "object": uvar}
    hdr_id = tree.insert(this_id, "end", text="Header", values=("",))
    tree.insert(hdr_id, "end", text="version", values=(uvar.version,))
    tree.insert(hdr_id, "end", text="magic", values=(uvar.magic,))
    tree.insert(hdr_id, "end", text="stringsOffset", values=(uvar.stringsOffset,))
    tree.insert(hdr_id, "end", text="dataOffset", values=(uvar.dataOffset,))
    tree.insert(hdr_id, "end", text="embedsInfoOffset", values=(uvar.embedsInfoOffset,))
    tree.insert(hdr_id, "end", text="hashInfoOffset", values=(uvar.hashInfoOffset,))
    if uvar.version < 3:
        tree.insert(hdr_id, "end", text="unkn64", values=(uvar.unkn64,))
    tree.insert(hdr_id, "end", text="UVARhash", values=(uvar.UVARhash,))
    tree.insert(hdr_id, "end", text="variableCount", values=(uvar.variableCount,))
    tree.insert(hdr_id, "end", text="embedCount", values=(uvar.embedCount,))
    data_id = tree.insert(this_id, "end", text="Data (Variables)", values=("",))
    for i, var in enumerate(uvar.variables):
        var_id = tree.insert(data_id, "end", text=f"Variable[{i}]", values=("",))
        tree.insert(var_id, "end", text="GUID", values=(var.guid,))
        tree.insert(var_id, "end", text="nameOffset", values=(var.nameOffset,))
        tree.insert(var_id, "end", text="nameString", values=(var.nameString,))
        tree.insert(var_id, "end", text="floatOffset", values=(var.floatOffset,))
        tree.insert(var_id, "end", text="uknOffset", values=(var.uknOffset,))
        tree.insert(var_id, "end", text="typeVal", values=(var.typeVal,))
        tree.insert(var_id, "end", text="numBits", values=(var.numBits,))
        tree.insert(var_id, "end", text="nameHash", values=(var.nameHash,))
        value_text = ""
        if i < len(uvar.values):
            f_val = uvar.values[i]
            i_val = struct.unpack("<I", struct.pack("<f", f_val))[0]
            value_text = f"{f_val:.4f} ({i_val})"
        tree.insert(var_id, "end", text="Value", values=(value_text,))
    str_id = tree.insert(this_id, "end", text="Strings", values=("",))
    for i, (off, s) in enumerate(uvar.stringOffsets):
        tree.insert(str_id, "end", text=f"str[{i}]", values=(s,))
    hd_id = tree.insert(this_id, "end", text="HashData", values=("",))
    tree.insert(hd_id, "end", text="HashDataOffsets", values=(str(uvar.hashDataOffsets),))
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

# ---------- UvarHandler Implementation ----------

class UvarHandler(FileHandler):
    def __init__(self):
        self.uvar = UvarFile()

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 48:
            return False
        try:
            version, magic = struct.unpack_from("<II", data, 0)
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
        return None

    def handle_edit(self, meta: dict, new_val, old_val, row_id):
        try:
            if meta.get("type") == "value":
                var_index = meta.get("varIndex")
                new_number = float(new_val) if ('.' in new_val or 'e' in new_val.lower()) else int(new_val)
                self.uvar.values[var_index] = float(new_number)
            elif meta.get("type") == "nameString":
                var_index = meta.get("varIndex")
                ok, msg = self.uvar.rename_variable_in_place(var_index, new_val)
                if ok:
                    messagebox.showinfo("Success", msg)
                else:
                    messagebox.showerror("Error", msg)
                    return
            elif meta.get("type") == "headerInt":
                ival = int(new_val)
                field_name = meta.get("field")
                ok, msg = self.uvar.patch_header_field_in_place(field_name, ival)
                if ok:
                    messagebox.showinfo("Success", msg)
                else:
                    messagebox.showerror("Error", msg)
                    return
            elif meta.get("type") == "varTypeVal":
                ival = int(new_val)
                var_index = meta.get("varIndex")
                ok, msg = self.uvar.patch_typeVal_in_place(var_index, ival)
                if ok:
                    messagebox.showinfo("Success", msg)
                else:
                    messagebox.showerror("Error", msg)
                    return
            elif meta.get("type") == "varNameHash":
                var_index = meta.get("varIndex")
                new_key = int(new_val)
                ok, msg = self.uvar.patch_nameHash_in_place(var_index, new_key)
                if not ok:
                    messagebox.showerror("Error", msg)
                    return
                canonical_index = None
                for i, mapping in enumerate(self.uvar.nameHashMap):
                    if mapping == var_index:
                        canonical_index = i
                        break
                if canonical_index is None:
                    messagebox.showerror("Error", "No canonical mapping found for this variable.")
                    return
                self.uvar.nameHashes[canonical_index] = new_key
            elif meta.get("type") == "nameHashes":
                canonical_index = meta.get("varIndex")
                new_key = int(new_val)
                if any(i != canonical_index and nh == new_key for i, nh in enumerate(self.uvar.nameHashes)):
                    messagebox.showerror("Error", "This nameHash value is already assigned to another variable.")
                    return
                self.uvar.nameHashes[canonical_index] = new_key
                var_index = self.uvar.nameHashMap[canonical_index]
                ok, msg = self.uvar.patch_nameHash_in_place(var_index, new_key)
                if not ok:
                    messagebox.showerror("Error", msg)
                    return
            elif meta.get("type") == "guid":
                try:
                    new_guid = str(uuid.UUID(new_val.strip()))
                except Exception as e:
                    messagebox.showerror("Error", f"Invalid GUID: {new_val}\n{e}")
                    return
                var_index = meta.get("varIndex")
                self.uvar.variables[var_index].guid = new_guid
            else:
                pass
            self.uvar.rebuild()
        except Exception as e:
            messagebox.showerror("Error", f"An exception occurred: {e}")
            return

    def add_variables(self, target, prefix: str, count: int):
        """
        Add 'count' new variables to the given target UvarFile using the provided prefix.
        This version updates the strings array completely (preserving the title as first element)
        and then rebuilds the top-level UvarFile.
        """
        if target.variables:
            last_name = target.variables[-1].nameString.strip()
            m = re.search(r'(.*?)(\d+)$', last_name)
            if m:
                base_prefix = m.group(1)
                start = int(m.group(2)) + 1
                # Use the width of the numeric part:
                width = len(m.group(2))
            else:
                base_prefix = prefix if prefix else "Variable"
                start = 1
                width = 0
        else:
            base_prefix = prefix if prefix else "Variable"
            start = 1
            width = 0

        new_vars = []
        for i in range(count):
            var = VariableEntry()
            var.guid = str(uuid.uuid4())
            if width:
                num_str = f"{start + i:0{width}d}"
            else:
                num_str = str(start + i)
            new_name = f"{base_prefix}{num_str}"
            var.nameString = new_name
            var.nameHash = murmur3_hash(new_name.encode("utf-16le"))
            var.typeVal = 2
            var.numBits = 0
            new_vars.append(var)

        original_count = target.variableCount
        target.variables.extend(new_vars)
        target.variableCount += count
        # Rebuild teh entire strings array:
        if target.strings:
            title = target.strings[0]
        else:
            title = ""
        target.strings = [title] + [var.nameString for var in target.variables]
        target.guids.extend([v.guid for v in new_vars])
        target.guidMap.extend(range(original_count, original_count + count))
        target.nameHashes.extend([v.nameHash for v in new_vars])
        target.nameHashMap.extend(range(original_count, original_count + count))
        target.values.extend([0.0] * count)
        # Rebuild from the top-level.
        current = target
        while current.parent:
            current = current.parent
        current.rebuild()