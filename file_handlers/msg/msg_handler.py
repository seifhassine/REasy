import struct
import uuid
import logging
import json
import os
from typing import Any, Dict, List, Optional

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
            
        cls._LANGUAGE_NAMES = {
            0: "Japanese", 1: "English", 2: "French", 3: "Italian", 4: "German",
            5: "Spanish", 6: "Russian", 7: "Polish", 8: "Dutch", 9: "Portuguese",
            10: "PortugueseBr", 11: "Korean", 12: "TransitionalChinese", 
            13: "SimplifiedChinese", 14: "Finnish", 15: "Swedish", 16: "Danish",
            17: "Norwegian", 18: "Czech", 19: "Hungarian", 20: "Slovak",
            21: "Arabic", 22: "Turkish", 23: "Bulgarian", 24: "Greek",
            25: "Romanian", 26: "Thai"
        }
            
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

    def __init__(self):
        super().__init__()
        self.__class__._load_language_names()
        self.header: Dict[str, Any] = {}
        self.messageTbl: List[int] = []
        self.entries: List[Dict[str, Any]] = []
        self.useLanguages: List[int] = []
        self.userParamTypes: List[int] = []
        self.userParamNames: List[str] = []
        self._pool: Optional[bytes] = None
        self.raw_data: bytes | bytearray = b""
        self.is_encrypted = False

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return len(data) >= 8 and data[4:8] == b"GMSG"

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes):
        if len(data) < 0x20:
            raise ValueError("File too small to be MSG")

        self.raw_data = data
        self.header = self._parse_header()
        self.is_encrypted = self._is_encrypted(self.header["version"])
        if self.is_encrypted:
            self._decrypt_string_pool()
        self._parse_content()
        self._pool = None
        
    def rebuild(self) -> bytes:
        return self._rebuild_structure()
    
    def _rebuild_structure(self) -> bytes:
        self.raw_data = bytearray(self.raw_data)
        struct.pack_into("<I", self.raw_data, 16, len(self.entries))
        struct.pack_into("<I", self.raw_data, 20, len(self.userParamTypes))
        self.header["messageCount"] = len(self.entries)
        self.header["userParamCount"] = len(self.userParamTypes)
        
        if self._by_hash(self.header["version"]):
            for entry in self.entries:
                if entry.get("name"):
                    name_bytes = entry["name"].encode("utf-16le")
                    entry["nameHash"] = murmur3_hash(name_bytes)
                else:
                    entry["nameHash"] = 0
        
        if self._by_hash(self.header["version"]):
            sorted_entries = sorted(self.entries, key=lambda e: e.get("name", "").lower())
        else:
            sorted_entries = sorted(self.entries, key=lambda e: e.get("index", 0))
            for i, entry in enumerate(sorted_entries):
                entry["index"] = i
        
        is_v12 = not self.is_encrypted
        entry_table_start = 64 if is_v12 else 72
        entry_offsets_size = len(sorted_entries) * 8
        entry_offsets_end = entry_table_start + entry_offsets_size

        metadata_start = entry_offsets_end
        unkn_zero_off = metadata_start
        lang_off = unkn_zero_off + 8

        after_langs = lang_off + len(self.useLanguages) * 4
        pad1 = (8 - (after_langs % 8)) % 8
        
        attr_off = after_langs + pad1
        if self.userParamTypes:
            after_attr_types = attr_off + len(self.userParamTypes) * 4
            pad2 = (8 - (after_attr_types % 8)) % 8
            attr_name_off = after_attr_types + pad2
            after_attr_names = attr_name_off + len(self.userParamTypes) * 8
            metadata_size = after_attr_names - metadata_start
        else:
            attr_name_off = attr_off
            after_attr_names = attr_name_off
            metadata_size = after_attr_names - metadata_start

        entry_data_start = metadata_start + metadata_size
        if entry_data_start % 8:
            entry_data_start += 8 - (entry_data_start % 8)

        entry_base_size = 40
        entry_size = entry_base_size + len(self.useLanguages) * 8

        if self.userParamTypes:
            all_entries_size = len(sorted_entries) * entry_size
            attr_data_start = entry_data_start + all_entries_size
            if attr_data_start % 8:
                attr_data_start += 8 - (attr_data_start % 8)
        else:
            attr_data_start = entry_data_start + len(sorted_entries) * entry_size

        if self.userParamTypes:
            string_data_start = attr_data_start + len(sorted_entries) * len(self.userParamTypes) * 8
        else:
            string_data_start = attr_data_start
        if string_data_start % 8:
            string_data_start += 8 - (string_data_start % 8)

        file_size_guess = max(
            string_data_start,
            after_attr_names,
            attr_name_off + (len(self.userParamTypes) * 8 if self.userParamTypes else 0),
            attr_off + (len(self.userParamTypes) * 4 + pad2 if self.userParamTypes else 0),
            lang_off + len(self.useLanguages) * 4 + pad1,
            entry_data_start + len(sorted_entries) * entry_size
        ) + 0x1000
        new_file = bytearray(file_size_guess)
        
        header_size = 64 if is_v12 else 72
        new_file[:header_size] = self.raw_data[:header_size]

        new_entry_offsets = []
        current_entry_pos = entry_data_start
        for i in range(len(sorted_entries)):
            struct.pack_into("<Q", new_file, entry_table_start + i * 8, current_entry_pos)
            new_entry_offsets.append(current_entry_pos)
            current_entry_pos += entry_size

        struct.pack_into("<Q", new_file, unkn_zero_off, 0)
        for i, lang in enumerate(self.useLanguages):
            struct.pack_into("<I", new_file, lang_off + i * 4, lang)
        for i in range(pad1):
            new_file[after_langs + i] = 0
        if self.userParamTypes:
            for i, attr_type in enumerate(self.userParamTypes):
                struct.pack_into("<i", new_file, attr_off + i * 4, attr_type)
            for i in range(pad2):
                new_file[after_attr_types + i] = 0
            for i in range(len(self.userParamTypes)):
                struct.pack_into("<Q", new_file, attr_name_off + i * 8, 0)
        
        if is_v12:
            new_file = new_file[:string_data_start]
        
        string_cache = {}
        
        def write_string(text):
            if not text:
                if "" not in string_cache:
                    offset = string_data_start + len(new_file[string_data_start:])
                    new_file.extend(b"\x00\x00")
                    string_cache[""] = offset
                return string_cache[""]
            
            if text in string_cache:
                return string_cache[text]
            
            offset = string_data_start + len(new_file[string_data_start:])
            string_data = text.encode("utf-16le") + b"\x00\x00"
            new_file.extend(string_data)
            string_cache[text] = offset
            return offset
        
        attr_offsets = []
        current_attr_pos = attr_data_start
        for i, entry in enumerate(sorted_entries):
            eoff = new_entry_offsets[i]
            new_file[eoff:eoff+16] = self._guid_to_bytes(entry["uuid"])
            pos = eoff + 16
            struct.pack_into("<I", new_file, pos, entry.get("SoundID", 0))
            pos += 4
            if self._by_hash(self.header["version"]):
                struct.pack_into("<I", new_file, pos, entry.get("nameHash", 0))
            else:
                struct.pack_into("<I", new_file, pos, entry.get("index", i))
            pos += 4

            name_offset = write_string(entry["name"])
            struct.pack_into("<Q", new_file, pos, name_offset)
            pos += 8

            if self.userParamTypes:
                struct.pack_into("<Q", new_file, pos, current_attr_pos)
                attr_offsets.append(current_attr_pos)
                current_attr_pos += len(self.userParamTypes) * 8
            else:
                attr_offset_val = write_string("")
                struct.pack_into("<Q", new_file, pos, attr_offset_val)
                attr_offsets.append(attr_offset_val)
            pos += 8

            for content in entry["content"]:
                content_offset = write_string(content)
                struct.pack_into("<Q", new_file, pos, content_offset)
                pos += 8

        if self.userParamTypes:
            for i, entry in enumerate(sorted_entries):
                attr_pos = attr_offsets[i]
                for j, (atype, aval) in enumerate(zip(self.userParamTypes, entry["attributes"])):
                    apos = attr_pos + j * 8
                    if atype in (-1, 2):
                        attr_string_offset = write_string(str(aval) if aval else "")
                        struct.pack_into("<Q", new_file, apos, attr_string_offset)
                    elif atype == 0:
                        struct.pack_into("<q", new_file, apos, int(aval) if aval else 0)
                    elif atype == 1:
                        struct.pack_into("<d", new_file, apos, float(aval) if aval else 0.0)
                    else:
                        struct.pack_into("<Q", new_file, apos, 0)

        if is_v12 and self.userParamNames:
            for i, name in enumerate(self.userParamNames):
                name_str_offset = write_string(name)
                struct.pack_into("<Q", new_file, attr_name_off + i * 8, name_str_offset)
        
        if not is_v12:
            new_file = new_file[:string_data_start]

        struct.pack_into("<Q", new_file, 8, 16)
        
        if is_v12:
            struct.pack_into("<Q", new_file, 32, unkn_zero_off)
            struct.pack_into("<Q", new_file, 40, lang_off)
            struct.pack_into("<Q", new_file, 48, attr_off)
            struct.pack_into("<Q", new_file, 56, attr_name_off)
        else:
            struct.pack_into("<Q", new_file, 32, string_data_start)
            struct.pack_into("<Q", new_file, 40, unkn_zero_off)
            struct.pack_into("<Q", new_file, 48, lang_off)
            struct.pack_into("<Q", new_file, 56, attr_off)
            struct.pack_into("<Q", new_file, 64, attr_name_off)
        
        self.entries = sorted_entries
        self.raw_data = new_file
        self.messageTbl = new_entry_offsets
        
        if is_v12:
            self.header.update({
                "header_offset": 16,
                "data_offset": None,
                "unknown_offset": unkn_zero_off,
                "lang_offset": lang_off,
                "attribute_offset": attr_off,
                "attribute_name_offset": attr_name_off
            })
        else:
            self.header.update({
                "header_offset": 16,
                "data_offset": string_data_start,
                "unknown_offset": unkn_zero_off,
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
        ftype = meta.get("field_type")
        
        if ftype == "attribute_name":
            return bool(new)
            
        if idx is None or idx >= len(self.entries):
            return False
            
        if ftype == "uuid":
            try:
                uuid.UUID(new)
                return True
            except ValueError:
                return False
        if ftype == "name":
            return bool(new)
        if ftype == "SoundID":
            try:
                int(new)
                return True
            except ValueError:
                return False
        if ftype == "attribute":
            aidx = meta.get("attr_index", -1)
            if not (0 <= aidx < len(self.userParamTypes)):
                return False
            atype = self.userParamTypes[aidx]
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
                    entry["nameHash"] = murmur3_hash(name_bytes)
                else:
                    entry["nameHash"] = 0
        elif ftype == "SoundID":
            entry["SoundID"] = int(new) if new else 0
        elif ftype == "content":
            entry["content"][meta.get("lang_index", 0)] = new
        elif ftype == "attribute":
            aidx = meta["attr_index"]
            atype = self.userParamTypes[aidx]
            if atype == 0:
                entry["attributes"][aidx] = int(new)
            elif atype == 1:
                entry["attributes"][aidx] = float(new)
            else:
                entry["attributes"][aidx] = new
        elif ftype == "attribute_name":
            aidx = meta.get("attr_index")
            if aidx is not None and 0 <= aidx < len(self.userParamNames):
                self.userParamNames[aidx] = new

    def _parse_header(self) -> Dict[str, Any]:
        r = self.raw_data
        ver, magic = struct.unpack_from("<I4s", r, 0)
        if magic != b"GMSG":
            raise ValueError("Missing GMSG magic")
        
        is_v12 = not self._is_encrypted(ver)
        
        hdr = {
            "version": ver,
            "header_offset": struct.unpack_from("<Q", r, 8)[0],
            "messageCount": struct.unpack_from("<I", r, 16)[0],
            "userParamCount": struct.unpack_from("<I", r, 20)[0],
            "languageDataCount": struct.unpack_from("<I", r, 24)[0],
        }
        
        if is_v12:
            hdr["data_offset"] = None
            hdr["unknown_offset"] = struct.unpack_from("<Q", r, 32)[0]
            hdr["lang_offset"] = struct.unpack_from("<Q", r, 40)[0]
            hdr["attribute_offset"] = struct.unpack_from("<Q", r, 48)[0]
            hdr["attribute_name_offset"] = struct.unpack_from("<Q", r, 56)[0]
        else:
            hdr["data_offset"] = struct.unpack_from("<Q", r, 32)[0]
            hdr["unknown_offset"] = struct.unpack_from("<Q", r, 40)[0]
            hdr["lang_offset"] = struct.unpack_from("<Q", r, 48)[0]
            hdr["attribute_offset"] = struct.unpack_from("<Q", r, 56)[0]
            hdr["attribute_name_offset"] = struct.unpack_from("<Q", r, 64)[0]
        
        return hdr

    @staticmethod
    def _is_encrypted(version: int) -> bool:
        return version > 12 and version != 0x2022033D

    @staticmethod
    def _by_hash(version: int) -> bool:
        return version > 15 and version != 0x2022033D

    def _decrypt_string_pool(self):
        if not self.is_encrypted or self.header["data_offset"] is None:
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
        if not self.is_encrypted or self.header["data_offset"] is None:
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
        if self._pool and self.header["data_offset"] is not None and abs_off >= self.header["data_offset"]:
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
        self.useLanguages = list(struct.unpack_from(f"<{hdr['languageDataCount']}I", r, hdr["lang_offset"]))
        if hdr['userParamCount'] > 0 and hdr["attribute_offset"] > 0:
            self.userParamTypes = list(struct.unpack_from(f"<{hdr['userParamCount']}i", r, hdr["attribute_offset"]))
        else:
            self.userParamTypes = []
            
        if hdr['userParamCount'] > 0 and hdr["attribute_name_offset"] > 0:
            name_offs = struct.unpack_from(f"<{hdr['userParamCount']}Q", r, hdr["attribute_name_offset"])
            self.userParamNames = [self._read_wstr(o) for o in name_offs]
        else:
            self.userParamNames = []
            
        is_v12 = not self.is_encrypted
        base = 64 if is_v12 else 72
        self.messageTbl = list(struct.unpack_from(f"<{hdr['messageCount']}Q", r, base))
        for eoff in self.messageTbl:
            cur = {}
            cur_off = eoff
            uuid_bytes = r[cur_off:cur_off + 16]
            cur["uuid"] = self._format_guid(uuid_bytes)
            cur_off += 16
            cur["SoundID"], cur_off = struct.unpack_from("<I", r, cur_off)[0], cur_off + 4
            if self._by_hash(hdr["version"]):
                cur["nameHash"] = struct.unpack_from("<I", r, cur_off)[0]
            else:
                cur["index"] = struct.unpack_from("<I", r, cur_off)[0]
            cur_off += 4
            name_ptr, attr_ptr = struct.unpack_from("<QQ", r, cur_off)
            cur_off += 16
            lang_ptrs = struct.unpack_from(f"<{hdr['languageDataCount']}Q", r, cur_off)
            cur["name"] = self._read_wstr(name_ptr)
            cur["content"] = [self._read_wstr(p) for p in lang_ptrs]
            cur["attributes"] = self._parse_attributes(attr_ptr)
            self.entries.append(cur)

    def _parse_attributes(self, ptr: int) -> List[Any]:
        if ptr == 0:
            return ["" for _ in self.userParamTypes]
        vals: List[Any] = []
        for atype in self.userParamTypes:
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
        contents = contents or ["" for _ in self.useLanguages]
        attrs    = attrs    or ["" for _ in self.userParamTypes]
        
        new = {"uuid": uuid_str, "SoundID": 0,  
               "name": name, "content": contents, "attributes": attrs}
               
        if self._by_hash(self.header["version"]):
            if name:
                name_bytes = name.encode("utf-16le")
                new["nameHash"] = murmur3_hash(name_bytes)
            else:
                new["nameHash"] = 0
        else:
            new["index"] = len(self.entries)
            
        self.entries.append(new)

    def remove_entry(self, idx: int):
        if 0 <= idx < len(self.entries):
            self.entries.pop(idx)
            if not self._by_hash(self.header["version"]):
                for i, entry in enumerate(self.entries):
                    entry["index"] = i
    
    def add_user_param(self, name: str = "NewParam", param_type: int = 2):
        """Add a new user parameter (attribute)
        
        Args:
            name: Name of the parameter
            param_type: Type of parameter (-1 or 2 for string, 0 for int, 1 for float)
        """
        self.userParamNames.append(name)
        self.userParamTypes.append(param_type)
        
        default_value = "" if param_type in (-1, 2) else (0 if param_type == 0 else 0.0)
        for entry in self.entries:
            entry["attributes"].append(default_value)
    
    def remove_user_param(self, idx: int):
        """Remove a user parameter at the given index
        
        Args:
            idx: Index of the parameter to remove
        """
        if 0 <= idx < len(self.userParamTypes):
            self.userParamNames.pop(idx)
            self.userParamTypes.pop(idx)
            
            for entry in self.entries:
                if idx < len(entry["attributes"]):
                    entry["attributes"].pop(idx)

    def _update_strings(self):
        pool_base = self.header["data_offset"]
        
        if pool_base is None:
            return
            
        self.raw_data = bytearray(self.raw_data)
        
        new_pool = bytearray(b"\x00\x00")
        
        string_refs = []
        
        if self.userParamNames:
            for i, txt in enumerate(self.userParamNames):
                ptr_pos = self.header["attribute_name_offset"] + i * 8
                if txt:
                    original_ptr = struct.unpack_from("<Q", self.raw_data, ptr_pos)[0]
                    original_offset = original_ptr - pool_base if original_ptr > 0 else 0
                    string_refs.append((original_offset, txt, ptr_pos))
                else:
                    struct.pack_into("<Q", self.raw_data, ptr_pos, pool_base)

        for eoff, ent in zip(self.messageTbl, self.entries):
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
                        zip(self.userParamTypes, ent["attributes"])):
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
