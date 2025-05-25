import struct
import uuid
import logging
import json
import os
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu

from file_handlers.base_handler import BaseFileHandler
from file_handlers.msg.msg_viewer import MsgViewer
from utils.hash_util import murmur3_hash

logger = logging.getLogger(__name__)


class MsgHandler(BaseFileHandler):

    _KEY = bytes([
        0xCF, 0xCE, 0xFB, 0xF8, 0xEC, 0x0A, 0x33, 0x66,
        0x93, 0xA9, 0x1D, 0x93, 0x50, 0x39, 0x5F, 0x09,
    ])

    _LANGUAGE_NAMES = {}

    @classmethod
    def _load_language_names(cls):
        if cls._LANGUAGE_NAMES:
            return
            
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            json_path = os.path.join(current_dir, "..", "..", "resources", "data", "enums", "shared_sdk.json")
            json_path = os.path.normpath(json_path)
            
            if os.path.exists(json_path):
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                if "via.Language" in data:
                    for item in data["via.Language"]:
                        cls._LANGUAGE_NAMES[item["value"]] = item["name"]
                        
        except Exception as e:
            logger.warning(f"Could not load language names from JSON: {e}")
            cls._LANGUAGE_NAMES = {
                0: "Japanese", 1: "English", 2: "French", 3: "Italian", 4: "German",
                5: "Spanish", 6: "Russian", 7: "Polish", 8: "Dutch", 9: "Portuguese",
                10: "PortugueseBr", 11: "Korean", 12: "TransitionalChinese", 
                13: "SimplifiedChinese", 14: "Finnish", 15: "Swedish", 16: "Danish",
                17: "Norwegian", 18: "Czech", 19: "Hungarian", 20: "Slovak",
                21: "Arabic", 22: "Turkish", 23: "Bulgarian", 24: "Greek",
                25: "Romanian", 26: "Thai"
            }

    def __init__(self):
        super().__init__()
        self.__class__._load_language_names()
        self.header: Dict[str, Any] = {}
        self.entry_offsets: List[int] = []
        self.entries: List[Dict[str, Any]] = []
        self.languages: List[int] = []
        self.attribute_value_types: List[int] = []
        self.attribute_names: List[str] = []
        self._pool: Optional[bytes] = None
        self.raw_data: bytes | bytearray = b""
        self.is_encrypted = False

    @staticmethod
    def can_handle(data: bytes) -> bool:
        return len(data) >= 8 and data[4:8] == b"GMSG"

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes):
        if len(data) < 0x20:
            raise ValueError("File too small to be MSG")

        self.raw_data = data
        self.header = self._parse_header()
        self.is_encrypted = self._is_encrypted(self.header["version"])
        self._decrypt_string_pool()
        self._parse_content()
        self._pool = None
        
    def rebuild(self) -> bytes:
        return self._rebuild_structure()
    
    def _rebuild_structure(self) -> bytes:
        self.raw_data = bytearray(self.raw_data)
        struct.pack_into("<I", self.raw_data, 16, len(self.entries))
        struct.pack_into("<I", self.raw_data, 20, len(self.attribute_value_types))
        self.header["entry_count"] = len(self.entries)
        self.header["attribute_count"] = len(self.attribute_value_types)
        
        if self._by_hash(self.header["version"]):
            for entry in self.entries:
                if entry.get("name"):
                    name_bytes = entry["name"].encode("utf-16le")
                    entry["hash"] = murmur3_hash(name_bytes)
                else:
                    entry["hash"] = 0
        
        sorted_entries = sorted(self.entries, key=lambda e: e.get("name", "").lower())
        
        entry_table_start = 72
        entry_offsets_size = len(sorted_entries) * 8
        entry_offsets_end = entry_table_start + entry_offsets_size

        metadata_start = entry_offsets_end
        unkn_zero_off = metadata_start
        lang_off = unkn_zero_off + 8

        after_langs = lang_off + len(self.languages) * 4
        pad1 = (8 - (after_langs % 8)) % 8
        
        attr_off = after_langs + pad1
        if self.attribute_value_types:
            after_attr_types = attr_off + len(self.attribute_value_types) * 4
            pad2 = (8 - (after_attr_types % 8)) % 8
            attr_name_off = after_attr_types + pad2
            after_attr_names = attr_name_off + len(self.attribute_value_types) * 8
            metadata_size = after_attr_names - metadata_start
        else:
            attr_name_off = attr_off
            after_attr_names = attr_name_off
            metadata_size = after_attr_names - metadata_start

        entry_data_start = metadata_start + metadata_size
        if entry_data_start % 8:
            entry_data_start += 8 - (entry_data_start % 8)

        entry_base_size = 40
        entry_size = entry_base_size + len(self.languages) * 8

        if self.attribute_value_types:
            all_entries_size = len(sorted_entries) * entry_size
            attr_data_start = entry_data_start + all_entries_size
            if attr_data_start % 8:
                attr_data_start += 8 - (attr_data_start % 8)
        else:
            attr_data_start = entry_data_start + len(sorted_entries) * entry_size

        if self.attribute_value_types:
            string_data_start = attr_data_start + len(sorted_entries) * len(self.attribute_value_types) * 8
        else:
            string_data_start = attr_data_start
        if string_data_start % 8:
            string_data_start += 8 - (string_data_start % 8)

        file_size_guess = max(
            string_data_start,
            after_attr_names,
            attr_name_off + (len(self.attribute_value_types) * 8 if self.attribute_value_types else 0),
            attr_off + (len(self.attribute_value_types) * 4 + pad2 if self.attribute_value_types else 0),
            lang_off + len(self.languages) * 4 + pad1,
            entry_data_start + len(sorted_entries) * entry_size
        ) + 0x1000
        new_file = bytearray(file_size_guess)
        new_file[:72] = self.raw_data[:72]

        new_entry_offsets = []
        current_entry_pos = entry_data_start
        for i in range(len(sorted_entries)):
            struct.pack_into("<Q", new_file, entry_table_start + i * 8, current_entry_pos)
            new_entry_offsets.append(current_entry_pos)
            current_entry_pos += entry_size

        struct.pack_into("<Q", new_file, unkn_zero_off, 0)
        for i, lang in enumerate(self.languages):
            struct.pack_into("<I", new_file, lang_off + i * 4, lang)
        for i in range(pad1):
            new_file[after_langs + i] = 0
        if self.attribute_value_types:
            for i, attr_type in enumerate(self.attribute_value_types):
                struct.pack_into("<i", new_file, attr_off + i * 4, attr_type)
            for i in range(pad2):
                new_file[after_attr_types + i] = 0
            for i in range(len(self.attribute_value_types)):
                struct.pack_into("<Q", new_file, attr_name_off + i * 8, 0)
        
        shared_offset = 0xA98
        attr_offsets = []
        current_attr_pos = attr_data_start
        for i, entry in enumerate(sorted_entries):
            eoff = new_entry_offsets[i]
            new_file[eoff:eoff+16] = self._guid_to_bytes(entry["uuid"])
            pos = eoff + 16
            struct.pack_into("<I", new_file, pos, entry.get("unknown", 0))
            pos += 4
            if self._by_hash(self.header["version"]):
                struct.pack_into("<I", new_file, pos, entry.get("hash", 0))
            else:
                struct.pack_into("<I", new_file, pos, entry.get("index", i))
            pos += 4

            if entry["name"]:
                name_offset = string_data_start + len(new_file[string_data_start:])
                name_data = entry["name"].encode("utf-16le") + b"\x00\x00"
                new_file.extend(name_data)
            else:
                name_offset = 0
            struct.pack_into("<Q", new_file, pos, name_offset)
            pos += 8

            if self.attribute_value_types:
                struct.pack_into("<Q", new_file, pos, current_attr_pos)
                attr_offsets.append(current_attr_pos)
                current_attr_pos += len(self.attribute_value_types) * 8
            else:
                struct.pack_into("<Q", new_file, pos, shared_offset)
                attr_offsets.append(shared_offset)
            pos += 8

            for content in entry["content"]:
                if content:
                    content_offset = string_data_start + len(new_file[string_data_start:])
                    content_data = content.encode("utf-16le") + b"\x00\x00"
                    new_file.extend(content_data)
                else:
                    content_offset = shared_offset
                struct.pack_into("<Q", new_file, pos, content_offset)
                pos += 8

        if self.attribute_value_types:
            for i, entry in enumerate(sorted_entries):
                attr_pos = attr_offsets[i]
                for j, (atype, aval) in enumerate(zip(self.attribute_value_types, entry["attributes"])):
                    apos = attr_pos + j * 8
                    if atype in (-1, 2):
                        if aval:
                            attr_string_offset = string_data_start + len(new_file[string_data_start:])
                            attr_string_data = aval.encode("utf-16le") + b"\x00\x00"
                            new_file.extend(attr_string_data)
                            struct.pack_into("<Q", new_file, apos, attr_string_offset)
                        else:
                            struct.pack_into("<Q", new_file, apos, 0)
                    elif atype == 0:
                        struct.pack_into("<q", new_file, apos, int(aval) if aval else 0)
                    elif atype == 1:
                        struct.pack_into("<d", new_file, apos, float(aval) if aval else 0.0)
                    else:
                        struct.pack_into("<Q", new_file, apos, 0)

        new_file = new_file[:string_data_start]

        struct.pack_into("<Q", new_file, 8, 16)
        struct.pack_into("<Q", new_file, 32, string_data_start)
        struct.pack_into("<Q", new_file, 40, unkn_zero_off)
        struct.pack_into("<Q", new_file, 48, lang_off)
        struct.pack_into("<Q", new_file, 56, attr_off)
        struct.pack_into("<Q", new_file, 64, attr_name_off)
        
        self.entries = sorted_entries
        self.raw_data = new_file
        self.entry_offsets = new_entry_offsets
        self.header.update({
            "header_offset": 16,
            "data_offset": string_data_start,
            "lang_offset": lang_off,
            "attribute_offset": attr_off,
            "attribute_name_offset": attr_name_off
        })

        self._update_strings()
        return self._encrypt()

    def create_viewer(self) -> Optional[MsgViewer]:
        try:
            return MsgViewer(self)
        except Exception as exc:
            logger.error("Viewer create failed: %s", exc)
            return None

    def validate_edit(self, meta: Dict[str, Any], new: str, _old: str = "") -> bool:
        idx = meta.get("entry_index")
        if idx is None or idx >= len(self.entries):
            return False
        ftype = meta.get("field_type")
        if ftype == "uuid":
            try:
                uuid.UUID(new)
                return True
            except ValueError:
                return False
        if ftype == "name":
            return bool(new)
        if ftype == "attribute":
            aidx = meta.get("attr_index", -1)
            if not (0 <= aidx < len(self.attribute_value_types)):
                return False
            atype = self.attribute_value_types[aidx]
            try:
                return atype == 0 and int(new) or atype == 1 and float(new) or True
            except ValueError:
                return False
        return True

    def handle_edit(self, meta: Dict[str, Any], new: str, _old: str, *_):
        idx = meta["entry_index"]
        entry = self.entries[idx]
        ftype = meta["field_type"]
        if ftype == "uuid":
            entry["uuid"] = new.lower()
        elif ftype == "name":
            entry["name"] = new
            if self._by_hash(self.header["version"]):
                if new:
                    name_bytes = new.encode("utf-16le")
                    entry["hash"] = murmur3_hash(name_bytes)
                else:
                    entry["hash"] = 0
        elif ftype == "content":
            entry["content"][meta.get("lang_index", 0)] = new
        elif ftype == "attribute":
            aidx = meta["attr_index"]
            atype = self.attribute_value_types[aidx]
            entry["attributes"][aidx] = int(new) if atype == 0 else float(new) if atype == 1 else new

    def _parse_header(self) -> Dict[str, Any]:
        r = self.raw_data
        ver, magic = struct.unpack_from("<I4s", r, 0)
        if magic != b"GMSG":
            raise ValueError("Missing GMSG magic")
        if not self._is_encrypted(ver):
            raise ValueError("Only encrypted >v12 supported for editing")
        hdr = {
            "version": ver,
            "header_offset": struct.unpack_from("<Q", r, 8)[0],
            "entry_count": struct.unpack_from("<I", r, 16)[0],
            "attribute_count": struct.unpack_from("<I", r, 20)[0],
            "lang_count": struct.unpack_from("<I", r, 24)[0],
            "data_offset": struct.unpack_from("<Q", r, 32)[0],
            "lang_offset": struct.unpack_from("<Q", r, 48)[0],
            "attribute_offset": struct.unpack_from("<Q", r, 56)[0],
            "attribute_name_offset": struct.unpack_from("<Q", r, 64)[0],
        }
        return hdr

    @staticmethod
    def _is_encrypted(version: int) -> bool:
        return version > 12 and version != 0x2022033D

    @staticmethod
    def _by_hash(version: int) -> bool:
        return version > 15 and version != 0x2022033D

    def _decrypt_string_pool(self):
        if not self.is_encrypted:
            return
        off = self.header["data_offset"]
        enc = self.raw_data[off:]
        dec = bytearray(len(enc))
        prev = 0
        for i, c in enumerate(enc):
            dec[i] = c ^ prev ^ self._KEY[i & 0xF]
            prev = c
        self._pool = bytes(dec)

    def _encrypt(self) -> bytes:
        if not self.is_encrypted:
            return bytes(self.raw_data)
        off = self.header["data_offset"]
        plain = self.raw_data[off:]
        enc = bytearray(len(plain))
        prev = 0
        for i, c in enumerate(plain):
            enc[i] = c ^ prev ^ self._KEY[i & 0xF]
            prev = enc[i]
        return bytes(self.raw_data[:off] + enc)

    def _read_wstr(self, abs_off: int) -> str:
        if abs_off == 0:
            return ""
        if self._pool and abs_off >= self.header["data_offset"]:
            data = self._pool[abs_off - self.header["data_offset"]:]
        else:
            data = self.raw_data[abs_off:]
        for i in range(0, len(data) - 1, 2):
            if data[i] == 0 and data[i + 1] == 0:
                return data[:i].decode("utf-16le", "ignore")
        return data.decode("utf-16le", "ignore")

    def _parse_content(self):
        r = self.raw_data
        hdr = self.header
        self.languages = list(struct.unpack_from(f"<{hdr['lang_count']}I", r, hdr["lang_offset"]))
        
        if hdr['attribute_count'] > 0 and hdr["attribute_offset"] > 0:
            self.attribute_value_types = list(struct.unpack_from(f"<{hdr['attribute_count']}i", r, hdr["attribute_offset"]))
        else:
            self.attribute_value_types = []
            
        if hdr['attribute_count'] > 0 and hdr["attribute_name_offset"] > 0:
            name_offs = struct.unpack_from(f"<{hdr['attribute_count']}Q", r, hdr["attribute_name_offset"])
            self.attribute_names = [self._read_wstr(o) for o in name_offs]
        else:
            self.attribute_names = []
            
        base = 72
        self.entry_offsets = list(struct.unpack_from(f"<{hdr['entry_count']}Q", r, base))
        for eoff in self.entry_offsets:
            cur = {}
            cur_off = eoff
            uuid_bytes = r[cur_off:cur_off + 16]
            cur["uuid"] = self._format_guid(uuid_bytes)
            cur_off += 16
            cur["unknown"], cur_off = struct.unpack_from("<I", r, cur_off)[0], cur_off + 4
            if self._by_hash(hdr["version"]):
                cur["hash"] = struct.unpack_from("<I", r, cur_off)[0]
            else:
                cur["index"] = struct.unpack_from("<I", r, cur_off)[0]
            cur_off += 4
            name_ptr, attr_ptr = struct.unpack_from("<QQ", r, cur_off)
            cur_off += 16
            lang_ptrs = struct.unpack_from(f"<{hdr['lang_count']}Q", r, cur_off)
            cur["name"] = self._read_wstr(name_ptr)
            cur["content"] = [self._read_wstr(p) for p in lang_ptrs]
            cur["attributes"] = self._parse_attributes(attr_ptr)
            self.entries.append(cur)

    def _parse_attributes(self, ptr: int) -> List[Any]:
        if ptr == 0:
            return ["" for _ in self.attribute_value_types]
        vals: List[Any] = []
        for atype in self.attribute_value_types:
            if atype in (-1, 2):
                vals.append(self._read_wstr(struct.unpack_from("<Q", self.raw_data, ptr)[0]))
            elif atype == 0:
                vals.append(struct.unpack_from("<q", self.raw_data, ptr)[0])
            elif atype == 1:
                vals.append(struct.unpack_from("<d", self.raw_data, ptr)[0])
            else:
                vals.append(None)
            ptr += 8
        return vals

    def add_entry(self,
                  uuid_str: Optional[str] = None,
                  name: str = "",
                  contents: Optional[List[str]] = None,
                  attrs: Optional[List[Any]] = None):

        uuid_str = uuid_str or str(uuid.uuid4())
        contents = contents or ["" for _ in self.languages]
        attrs    = attrs    or ["" for _ in self.attribute_value_types]
        
        new = {"uuid": uuid_str, "unknown": 0,  
               "name": name, "content": contents, "attributes": attrs}
               
        if self._by_hash(self.header["version"]):
            if name:
                name_bytes = name.encode("utf-16le")
                new["hash"] = murmur3_hash(name_bytes)
            else:
                new["hash"] = 0
        else:
            new["index"] = len(self.entries)
            
        self.entries.append(new)

    def remove_entry(self, idx: int):
        if 0 <= idx < len(self.entries):
            self.entries.pop(idx)
            if not self._by_hash(self.header["version"]):
                for i, entry in enumerate(self.entries):
                    entry["index"] = i

    def _update_strings(self):
        pool_base = self.header["data_offset"]
        self.raw_data = bytearray(self.raw_data)
        
        new_pool = bytearray(b"\x00\x00")
        
        string_refs = []
        
        if self.attribute_names:
            for i, txt in enumerate(self.attribute_names):
                ptr_pos = self.header["attribute_name_offset"] + i * 8
                if txt:
                    original_ptr = struct.unpack_from("<Q", self.raw_data, ptr_pos)[0]
                    original_offset = original_ptr - pool_base if original_ptr > 0 else 0
                    string_refs.append((original_offset, txt, ptr_pos))
                else:
                    struct.pack_into("<Q", self.raw_data, ptr_pos, pool_base)

        for eoff, ent in zip(self.entry_offsets, self.entries):
            name_ptr_pos = eoff + 24
            if ent["name"]:
                original_ptr = struct.unpack_from("<Q", self.raw_data, name_ptr_pos)[0]
                original_offset = original_ptr - pool_base if original_ptr > 0 else 0
                string_refs.append((original_offset, ent["name"], name_ptr_pos))
            else:
                struct.pack_into("<Q", self.raw_data, name_ptr_pos, pool_base)

            for li, txt in enumerate(ent["content"]):
                pos = eoff + 40 + li * 8
                if txt:
                    original_ptr = struct.unpack_from("<Q", self.raw_data, pos)[0]
                    original_offset = original_ptr - pool_base if original_ptr > 0 else 0
                    string_refs.append((original_offset, txt, pos))
                else:
                    struct.pack_into("<Q", self.raw_data, pos, pool_base)

            attr_ptr = struct.unpack_from("<Q", self.raw_data, eoff + 32)[0]
            if attr_ptr:
                for ai, (atype, aval) in enumerate(
                        zip(self.attribute_value_types, ent["attributes"])):
                    if atype in (-1, 2):
                        str_off_pos = attr_ptr + ai * 8
                        if aval:
                            original_ptr = struct.unpack_from("<Q", self.raw_data, str_off_pos)[0]
                            original_offset = original_ptr - pool_base if original_ptr > 0 else 0
                            string_refs.append((original_offset, str(aval), str_off_pos))
                        else:
                            struct.pack_into("<Q", self.raw_data, str_off_pos, pool_base)

        string_refs.sort(key=lambda x: x[0])
        
        for original_offset, text, ptr_pos in string_refs:
            if text:
                off = pool_base + len(new_pool)
                new_pool += text.encode("utf-16le") + b"\x00\x00"
                struct.pack_into("<Q", self.raw_data, ptr_pos, off)

        self.raw_data = self.raw_data[:pool_base] + new_pool

    @staticmethod
    def _format_guid(b: bytes) -> str:
        d1, d2, d3, d4 = struct.unpack("<IHH8s", b)
        return f"{d1:08x}-{d2:04x}-{d3:04x}-{d4[:2].hex()}-{d4[2:].hex()}"

    @staticmethod
    def _guid_to_bytes(guid: str) -> bytes:
        u = uuid.UUID(guid)
        b = u.bytes
        return struct.pack("<IHH8s", *struct.unpack_from(">IHH8s", b, 0))

    def get_language_name(self, lang_code: int) -> str:
        return self._LANGUAGE_NAMES.get(lang_code, f"Unknown({lang_code})")
