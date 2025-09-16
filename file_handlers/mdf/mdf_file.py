from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

from utils.binary_handler import BinaryHandler
from utils.hash_util import murmur3_hash_utf16le, murmur3_hash_ascii


MDF_MAGIC = 0x0046444D


@dataclass
class MdfHeader:
    magic: int = MDF_MAGIC
    version: int = 0
    material_count: int = 0
    ukn0: int = 0

    def read(self, h: BinaryHandler):
        self.magic = h.read_uint32()
        self.version = h.read_int16()
        self.material_count = h.read_int16()
        self.ukn0 = h.read_int32()

    def write(self, h: BinaryHandler):
        h.write_uint32(self.magic)
        h.write_int16(self.version)
        h.write_int16(self.material_count)
        h.write_int32(self.ukn0)


@dataclass
class MatHeader:
    mat_name: str = ""
    mat_name_hash: int = 0
    ukn_re7: int = 0  # version == 6
    params_size: int = 0
    param_count: int = 0
    tex_count: int = 0
    gpbf_name_count: int = 0  # >= 19
    gpbf_data_count: int = 0  # >= 19
    shader_type: int = 0
    ukn: int = 0  # >= 31
    alpha_flags: int = 0
    ukn1: int = 0  # >= 31 (uint32)
    texID_count: int = 0  # >= 31 (uint32)
    param_header_offset: int = 0
    tex_header_offset: int = 0
    gpbf_offset: int = 0  # >= 19
    params_offset: int = 0
    mmtr_path: str = ""
    tex_ids_offset: int = 0  # >= 31

    _pos: int = field(default=0, init=False, repr=False)
    _orig_param_count: int = field(default=0, init=False, repr=False)
    _orig_params_size: int = field(default=0, init=False, repr=False)

    def get_flags1(self) -> int:
        return self.alpha_flags & 0x03FF

    def set_flags1(self, value: int):
        self.alpha_flags = (self.alpha_flags & ~0x03FF) | (value & 0x03FF)

    def get_tessellation(self) -> int:
        return (self.alpha_flags >> 10) & 0x3F

    def set_tessellation(self, value: int):
        self.alpha_flags = (self.alpha_flags & ~0xFC00) | ((value & 0x3F) << 10)

    def get_phong(self) -> int:
        return (self.alpha_flags >> 16) & 0xFF

    def set_phong(self, value: int):
        self.alpha_flags = (self.alpha_flags & ~0xFF0000) | ((value & 0xFF) << 16)

    def get_flags2(self) -> int:
        return (self.alpha_flags >> 24) & 0xFF

    def set_flags2(self, value: int):
        self.alpha_flags = (self.alpha_flags & ~0xFF000000) | ((value & 0xFF) << 24)

    def read(self, h: BinaryHandler, version: int):
        self._pos = h.tell
        name_off = h.read_int64()
        self.mat_name_hash = h.read_uint32()
        if version == 6:
            self.ukn_re7 = h.read_uint64()
        self.params_size = h.read_int32()
        self.param_count = h.read_int32()
        self.tex_count = h.read_int32()
        self._orig_param_count = self.param_count
        self._orig_params_size = self.params_size
        if version >= 19:
            self.gpbf_name_count = h.read_int32()
            self.gpbf_data_count = h.read_int32()
        self.shader_type = h.read_int32()
        if version >= 31:
            self.ukn = h.read_uint32()
        self.alpha_flags = h.read_uint32()
        if version >= 31:
            self.ukn1 = h.read_uint32()
            self.texID_count = h.read_uint32()
        self.param_header_offset = h.read_int64()
        self.tex_header_offset = h.read_int64()
        if version >= 19:
            self.gpbf_offset = h.read_int64()
        self.params_offset = h.read_int64()
        mmtr_off = h.read_int64()
        if version >= 31:
            self.tex_ids_offset = h.read_int64()

        self.mat_name = ""
        if name_off:
            with h.seek_jump_back(name_off):
                self.mat_name = h.read_wstring()
        self.mmtr_path = ""
        if mmtr_off:
            with h.seek_jump_back(mmtr_off):
                self.mmtr_path = h.read_wstring()

    def write(self, h: BinaryHandler, version: int):
        self._pos = h.tell
        h.write_offset_wstring(self.mat_name or "")
        self.mat_name_hash = murmur3_hash_utf16le(self.mat_name or "")
        h.write_uint32(self.mat_name_hash)
        if version == 6:
            h.write_uint64(self.ukn_re7)
        h.write_int32(self.params_size)
        h.write_int32(self.param_count)
        h.write_int32(self.tex_count)
        if version >= 19:
            h.write_int32(self.gpbf_name_count)
            h.write_int32(self.gpbf_data_count)
        h.write_int32(self.shader_type)
        if version >= 31:
            h.write_uint32(self.ukn)
        h.write_uint32(self.alpha_flags)
        if version >= 31:
            h.write_uint32(self.ukn1)
            h.write_uint32(self.texID_count)
        h.write_int64(self.param_header_offset)
        h.write_int64(self.tex_header_offset)
        if version >= 19:
            h.write_int64(self.gpbf_offset)
        h.write_int64(self.params_offset)
        h.write_offset_wstring(self.mmtr_path or "")
        if version >= 31:
            h.write_int64(self.tex_ids_offset)

    def rewrite(self, h: BinaryHandler, version: int):
        cur = self._pos
        cur += 8  # name off
        cur += 4  # name hash
        if version == 6:
            cur += 8
        # params_size, param_count, tex_count
        h.write_at(cur, '<i', self.params_size)
        cur += 4
        h.write_at(cur, '<i', self.param_count)
        cur += 4
        h.write_at(cur, '<i', self.tex_count)
        cur += 4
        # gpbf counts (>=19)
        if version >= 19:
            h.write_at(cur, '<i', self.gpbf_name_count)
            cur += 4
            h.write_at(cur, '<i', self.gpbf_data_count)
            cur += 4
        # skip shader_type
        cur += 4
        # skip ukn (>=31)
        if version >= 31:
            cur += 4
        # skip alpha_flags
        cur += 4
        # ukn1 and texID_count (>=31)
        if version >= 31:
            cur += 8
        # param_header_offset, tex_header_offset
        h.write_at(cur, '<q', self.param_header_offset)
        cur += 8
        h.write_at(cur, '<q', self.tex_header_offset)
        cur += 8
        if version >= 19:
            h.write_at(cur, '<q', self.gpbf_offset)
            cur += 8
        h.write_at(cur, '<q', self.params_offset)
        cur += 8
        # mmtr offset (string)
        if version >= 31:
            cur += 8  # mmtr offset value
            h.write_at(cur, '<q', self.tex_ids_offset)
            cur += 8


@dataclass
class TexHeader:
    tex_type: str = ""
    hash: int = 0
    ascii_hash: int = 0
    tex_path: str = ""

    def read(self, h: BinaryHandler, version: int):
        type_off = h.read_int64()
        self.hash = h.read_uint32()
        self.ascii_hash = h.read_uint32()
        path_off = h.read_int64()
        self.tex_type = ""
        if type_off:
            with h.seek_jump_back(type_off):
                self.tex_type = h.read_wstring()
        self.tex_path = ""
        if path_off:
            with h.seek_jump_back(path_off):
                self.tex_path = h.read_wstring()
        if version >= 13:
            try:
                data_len = len(h.data)
            except Exception:
                data_len = None
            if data_len is None or (h.tell + 8) <= data_len:
                h.skip(8)

    def write(self, h: BinaryHandler, version: int):
        if self.tex_type:
            h.write_offset_wstring(self.tex_type)
            self.hash = murmur3_hash_utf16le(self.tex_type)
            self.ascii_hash = murmur3_hash_ascii(self.tex_type)
        else:
            h.write_uint64(0)
            self.hash = 0
            self.ascii_hash = 0
        h.write_uint32(self.hash)
        h.write_uint32(self.ascii_hash)
        if self.tex_path:
            h.write_offset_wstring(self.tex_path)
        else:
            h.write_uint64(0)
        if version >= 13:
            h.write_bytes(b"\x00" * 8)


@dataclass
class ParamHeader:
    name: str = ""
    hash: int = 0
    ascii_hash: int = 0
    component_count: int = 4
    component_ukn: int = 0
    param_rel_offset: int = 0
    param_abs_offset: int = 0
    gap_size: int = 0
    parameter: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    _pos: int = field(default=0, init=False, repr=False)
    _orig_rel_offset: int = field(default=0, init=False, repr=False)
    _orig_component_count: int = field(default=0, init=False, repr=False)

    def read(self, h: BinaryHandler, version: int):
        self._pos = h.tell
        name_off = h.read_int64()
        self.hash = h.read_uint32()
        self.ascii_hash = h.read_uint32()
        if version >= 31:
            self.param_rel_offset = h.read_int32()
            comp = h.read_uint32()
            self.component_count = comp & 0xFFFF
            self.component_ukn = (comp >> 16) & 0xFFFF
        elif version >= 13:
            self.param_rel_offset = h.read_int32()
            self.component_count = h.read_int32()
            self.component_ukn = 0
        else:
            self.component_count = h.read_int32()
            self.component_ukn = 0
            self.param_rel_offset = h.read_int32()
        self._orig_rel_offset = self.param_rel_offset
        self._orig_component_count = self.component_count
        self.name = ""
        if name_off:
            with h.seek_jump_back(name_off):
                self.name = h.read_wstring()

    def write(self, h: BinaryHandler, version: int):
        self._pos = h.tell
        h.write_offset_wstring(self.name or "")
        self.hash = murmur3_hash_utf16le(self.name or "")
        self.ascii_hash = murmur3_hash_ascii(self.name or "")
        h.write_uint32(self.hash)
        h.write_uint32(self.ascii_hash)
        if version >= 31:
            h.write_int32(self.param_rel_offset)
            comp = ((self.component_ukn & 0xFFFF) << 16) | (self.component_count & 0xFFFF)
            h.write_uint32(comp)
        elif version >= 13:
            h.write_int32(self.param_rel_offset)
            h.write_int32(self.component_count)
        else:
            h.write_int32(self.component_count)
            h.write_int32(self.param_rel_offset)

    def rewrite_rel_offset(self, h: BinaryHandler, version: int):
        base = self._pos + 8 + 4 + 4
        if version >= 13:
            h.write_at(base, '<i', self.param_rel_offset)
        else:
            base += 4
            h.write_at(base, '<i', self.param_rel_offset)


@dataclass
class GpbfHeader:
    name: str = ""
    utf16_hash: int = 0
    ascii_hash: int = 0

    def read(self, h: BinaryHandler):
        name_off = h.read_int64()
        self.utf16_hash = h.read_uint32()
        self.ascii_hash = h.read_uint32()
        self.name = ""
        if name_off:
            with h.seek_jump_back(name_off):
                self.name = h.read_wstring()

    def write(self, h: BinaryHandler, is_value: bool = False):
        h.write_offset_wstring(self.name or "")
        if is_value:
            self.utf16_hash = 0
            self.ascii_hash = 1
            h.write_uint32(0)
            h.write_uint32(1)
        else:
            self.utf16_hash = murmur3_hash_utf16le(self.name or "")
            self.ascii_hash = murmur3_hash_ascii(self.name or "")
            h.write_uint32(self.utf16_hash)
            h.write_uint32(self.ascii_hash)


@dataclass
class MatData:
    header: MatHeader = field(default_factory=MatHeader)
    textures: List[TexHeader] = field(default_factory=list)
    parameters: List[ParamHeader] = field(default_factory=list)
    gpu_buffers: List[Tuple[GpbfHeader, GpbfHeader]] = field(default_factory=list)
    tex_id_arrays: List[Tuple[List[int], List[int]]] = field(default_factory=list)


class MdfFile:
    EXTENSION = ".mdf2"

    def __init__(self):
        self.header = MdfHeader()
        self.materials: List[MatData] = []
        self.file_version: int = 13

    @staticmethod
    def can_handle(data: bytes) -> bool:
        if len(data) < 8:
            return False
        try:
            magic = int.from_bytes(data[0:4], 'little', signed=False)
            return magic == MDF_MAGIC
        except Exception:
            return False

    def read(self, data: bytes, file_path: str = "") -> bool:
        h = BinaryHandler(bytearray(data))
        self.header.read(h)
        if self.header.magic != MDF_MAGIC:
            raise ValueError("Not an MDF file")

        if file_path:
            lower = file_path.lower()
            matched = False
            idx = lower.rfind('.')
            if idx != -1:
                ver_str = lower[idx + 1:]
                if ver_str.isdigit():
                    self.file_version = int(ver_str)
                    matched = True
            if not matched:
                raise ValueError("Could not determine MDF version from file name (expected .mdf2.<ver>)")
        else:
            raise ValueError("File path required to determine MDF version (expected .mdf2.<ver>)")

        version = self.file_version

        h.align(16)
        self.materials = []
        for _ in range(self.header.material_count):
            mh = MatHeader()
            mh.read(h, version)
            self.materials.append(MatData(header=mh))

        for mat in self.materials:
            if mat.header.tex_header_offset:
                try:
                    data_len = len(h.data)
                except Exception:
                    data_len = None
                header_size = 32 if version >= 13 else 24
                expected_end = mat.header.tex_header_offset + header_size * mat.header.tex_count
                if data_len is not None and expected_end > data_len:
                    mat.textures = []
                else:
                    with h.seek_jump_back(mat.header.tex_header_offset):
                        mat.textures = []
                        for _ in range(mat.header.tex_count):
                            th = TexHeader()
                            th.read(h, version)
                            mat.textures.append(th)

        for mat in self.materials:
            if mat.header.param_header_offset:
                with h.seek_jump_back(mat.header.param_header_offset):
                    mat.parameters = []
                    for i in range(mat.header.param_count):
                        ph = ParamHeader()
                        ph.read(h, version)
                        ph.param_abs_offset = mat.header.params_offset + ph.param_rel_offset
                        if i == 0:
                            ph.gap_size = ph.param_rel_offset
                        else:
                            prev = mat.parameters[i - 1]
                            ph.gap_size = int(ph.param_abs_offset - (prev.param_abs_offset + prev.component_count * 4))
                        if ph.component_count == 4:
                            x, y, z, w = h.read_at(ph.param_abs_offset, '<ffff')
                            ph.parameter = (x, y, z, w)
                        else:
                            x = h.read_at(ph.param_abs_offset, '<f')
                            ph.parameter = (x, 0.0, 0.0, 0.0)
                        mat.parameters.append(ph)

            mat.gpu_buffers = []
            if version >= 19 and mat.header.gpbf_offset:
                tell = h.tell
                with h.seek_jump_back(mat.header.gpbf_offset):
                    for _ in range(mat.header.gpbf_name_count):
                        n = GpbfHeader()
                        d = GpbfHeader()
                        n.read(h)
		                d.read(h)
                        mat.gpu_buffers.append((n, d))
                h.seek(tell)

            # Read tex ID arrays table and arrays (>=31)
            mat.tex_id_arrays = []
            if version >= 31 and mat.header.tex_ids_offset and mat.header.texID_count > 0:
                with h.seek_jump_back(mat.header.tex_ids_offset):
                    offs = [h.read_int64() for _ in range(mat.header.texID_count * 2)]
                for i in range(mat.header.texID_count):
                    counts_off = offs[i * 2]
                    elems_off = offs[i * 2 + 1]
                    counts: List[int] = []
                    elems: List[int] = []
                    if counts_off:
                        with h.seek_jump_back(counts_off):
                            c = h.read_int32()
                            counts = [h.read_int32() for _ in range(c)] if c > 0 else []
                    if elems_off:
                        with h.seek_jump_back(elems_off):
                            c = h.read_int32()
                            elems = [h.read_int32() for _ in range(c)] if c > 0 else []
                    mat.tex_id_arrays.append((counts, elems))

        return True

    def write(self) -> bytes:
        h = BinaryHandler(bytearray())
        version = self.file_version
        self.header.material_count = len(self.materials)

        self.header.write(h)

        h.align_write(16)
        for mat in self.materials:
            mat.header.param_count = len(mat.parameters)
            mat.header.tex_count = len(mat.textures)
            mat.header.gpbf_name_count = mat.header.gpbf_data_count = len(mat.gpu_buffers)
            if version >= 31 and mat.header.texID_count <= 0:
                mat.header.tex_ids_offset = 0
            mat.header.write(h, version)

        for mat in self.materials:
            mat.header.tex_header_offset = h.tell
            for th in mat.textures:
                th.write(h, version)

        for mat in self.materials:
            mat.header.param_header_offset = h.tell
            for ph in mat.parameters:
                ph.write(h, version)

        for mat in self.materials:
            mat.header.gpbf_offset = h.tell
            mat.header.gpbf_name_count = mat.header.gpbf_data_count = len(mat.gpu_buffers)
            for name_hdr, data_hdr in mat.gpu_buffers:
                name_hdr.write(h, is_value=False)
                data_hdr.write(h, is_value=True)

        h.string_table_flush()

        for mat in self.materials:
            mat.header.params_offset = h.tell
            size_accum = 0
            prev_color_index = None
            for idx, ph in enumerate(mat.parameters):
                gap = ph.gap_size if ph.gap_size > 0 else 0
                if gap:
                    h.write_bytes(b"\x00" * gap)
                    size_accum += gap
                    prev_color_index = None
                else:
                    name_l = (ph.name or "").lower()
                    color_idx = None
                    if name_l.startswith("layercolor_"):
                        if "red" in name_l:
                            color_idx = 0
                        elif "green" in name_l:
                            color_idx = 1
                        elif "blue" in name_l:
                            color_idx = 2
                    if prev_color_index is not None and color_idx is not None and color_idx > prev_color_index:
                        missing = color_idx - prev_color_index - 1
                        if missing > 0:
                            inferred = missing * 4
                            h.write_bytes(b"\x00" * inferred)
                            size_accum += inferred
                    prev_color_index = color_idx if color_idx is not None else None

                ph.param_rel_offset = h.tell - mat.header.params_offset
                if ph.component_count == 4:
                    x, y, z, w = ph.parameter
                    h.write_vec4(x, y, z, w)
                else:
                    h.write_float(ph.parameter[0])
                ph.rewrite_rel_offset(h, version)
                size_accum += max(0, ph.component_count) * 4
            if version == 6: # Dunno what kind of unholy optimization Capcom was doing here
                pad_end = 4 if (size_accum % 16) != 0 else 0
                if pad_end:
                    h.write_bytes(b"\x00" * pad_end)
            else:
                pad_end = (16 - (size_accum % 16)) % 16
                if pad_end:
                    h.write_bytes(b"\x00" * pad_end)
            mat.header.params_size = size_accum + pad_end
            mat.header.rewrite(h, version)

        if version >= 31:
            for mat in self.materials:
                if mat.header.texID_count > 0:
                    mat.header.tex_ids_offset = h.tell
                    table_pos = h.tell
                    table_count = mat.header.texID_count * 2
                    for _ in range(table_count):
                        h.write_int64(0)
                    offsets: List[int] = []
                    for i in range(mat.header.texID_count):
                        counts, elems = (mat.tex_id_arrays[i] if i < len(mat.tex_id_arrays) else ([], []))
                        # counts array: size then 4-byte IDs
                        cofs = h.tell
                        h.write_int32(len(counts))
                        for v in counts:
                            h.write_int32(int(v))
                        offsets.append(cofs)
                        # elems array: size then 4-byte IDs
                        eofs = h.tell
                        h.write_int32(len(elems))
                        for v in elems:
                            h.write_int32(int(v))
                        offsets.append(eofs)
                    # Patch offsets table
                    for j, ofs in enumerate(offsets):
                        h.write_at(table_pos + j * 8, '<q', ofs)
                    # Persist tex_ids_offset in header
                    mat.header.rewrite(h, version)
                else:
                    # No tex IDs: ensure header field is zeroed
                    mat.header.tex_ids_offset = 0
                    mat.header.rewrite(h, version)

        return h.get_all_bytes()

    @staticmethod
    def _in_range(val: int, size: int) -> bool:
        return 0 <= val < size

    def _mat_header_is_plausible(self, mh: MatHeader, version: int, size: int) -> bool:
        if version >= 19 and mh.gpbf_name_count != mh.gpbf_data_count:
            return False
        for off in [mh.tex_header_offset, mh.param_header_offset, mh.gpbf_offset if version >= 19 else 0]:
            if off != 0 and not self._in_range(off, size):
                return False
        if mh.tex_header_offset and mh.tex_count >= 0:
            per = 32 if version >= 13 else 24
            end = mh.tex_header_offset + per * mh.tex_count
            if end > size:
                return False
        if mh.param_header_offset and mh.param_count >= 0:
            per = 24
            end = mh.param_header_offset + per * mh.param_count
            if end > size:
                return False
        if mh.params_size >= 0:
            if mh.param_count == 0 and mh.params_size == 0:
                if not (0 <= mh.params_offset <= size):
                    return False
            else:
                if not self._in_range(mh.params_offset, size):
                    return False
                end = mh.params_offset + mh.params_size
                if end > size:
                    return False
        if mh.mat_name and mh.mat_name_hash == 0:
            return False
        return True

