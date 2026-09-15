from __future__ import annotations

from dataclasses import dataclass, field

from utils.binary_handler import BinaryHandler


UVS_MAGIC = 0x5556532E


@dataclass
class UvsHeader:
    magic: int = UVS_MAGIC
    texture_count: int = 0
    sequence_count: int = 0
    pattern_count: int = 0
    attributes: int = 0
    tex_offset: int = 0
    sequence_offset: int = 0
    pattern_offset: int = 0
    string_offset: int = 0


@dataclass
class UvsTexture:
    state_holder: int = 0
    path_string_offset: int = 0
    tex_handle_1: int = 0
    tex_handle_2: int = 0
    tex_handle_3: int = 0
    path: str = ""
    normal_path: str = ""
    specular_path: str = ""
    alpha_path: str = ""


@dataclass
class UvsPattern:
    flags: int = 0
    left: float = 0.0
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0
    texture_index: int = 0
    cutout_uv_count: int = -1
    cutout_uvs: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class UvsSequence:
    pattern_count: int = 0
    pattern_table_offset: int = 0
    patterns: list[UvsPattern] = field(default_factory=list)


class UvsFile:
    def __init__(self):
        self.version = 0
        self.header = UvsHeader()
        self.textures: list[UvsTexture] = []
        self.sequences: list[UvsSequence] = []

    def read(self, data: bytes, version: int = 0) -> bool:
        if len(data) < 4:
            raise ValueError("File too small for UVS")

        h = BinaryHandler(bytearray(data), file_version=version)
        self.version = version
        hdr = self.header = UvsHeader()

        hdr.magic = h.read_uint32()
        if hdr.magic != UVS_MAGIC:
            raise ValueError("Invalid UVS magic")

        hdr.texture_count = h.read_int32()
        hdr.sequence_count = h.read_int32()
        hdr.pattern_count = h.read_int32()

        if version >= 7:
            hdr.attributes = h.read_int32()
            h.skip(4)

        hdr.tex_offset = h.read_int64()
        hdr.sequence_offset = h.read_int64()
        hdr.pattern_offset = h.read_int64()
        hdr.string_offset = h.read_int64()

        h.seek(hdr.tex_offset)
        self.textures = [self._read_texture(h) for _ in range(hdr.texture_count)]

        string_table_end = self._find_string_table_end(data)
        for tex in self.textures:
            tex.path, tex.normal_path, tex.specular_path, tex.alpha_path = (
                self._read_string_at_offset(data, offset, string_table_end)
                for offset in (tex.path_string_offset, tex.tex_handle_1, tex.tex_handle_2, tex.tex_handle_3)
            )

        h.seek(hdr.sequence_offset)
        self.sequences = [self._read_sequence(h) for _ in range(hdr.sequence_count)]

        h.seek(hdr.pattern_offset)
        for seq in self.sequences:
            seq.patterns = [self._read_pattern(h) for _ in range(seq.pattern_count)]

        return True

    @staticmethod
    def _read_texture(h: BinaryHandler) -> UvsTexture:
        return UvsTexture(
            state_holder=h.read_int64(),
            path_string_offset=h.read_int64(),
            tex_handle_1=h.read_int64(),
            tex_handle_2=h.read_int64(),
            tex_handle_3=h.read_int64(),
        )

    @staticmethod
    def _read_sequence(h: BinaryHandler) -> UvsSequence:
        return UvsSequence(pattern_count=h.read_int32(), pattern_table_offset=h.read_int32())

    def _find_string_table_end(self, data: bytes) -> int:
        start = int(self.header.string_offset)
        if start < 0 or start >= len(data):
            return start

        table = data[start:]
        pos = 0
        table_len = len(table)

        while pos + 1 < table_len:
            if table[pos:pos + 2] == b"\x00\x00" and not any(table[pos:]):
                return start + pos
            pos += 2

        return len(data)

    def _read_string_at_offset(self, data: bytes, char_offset: int, string_table_end: int) -> str:
        if char_offset < 0:
            return ""
        
        pos = int(self.header.string_offset) + (int(char_offset) * 2)
        if pos < self.header.string_offset or pos >= string_table_end:
            return ""

        end = pos
        while end + 1 < string_table_end and data[end:end + 2] != b"\x00\x00":
            end += 2

        try:
            return data[pos:end].decode("utf-16le")
        except UnicodeDecodeError:
            return ""


    @staticmethod
    def _read_pattern(h: BinaryHandler) -> UvsPattern:
        pat = UvsPattern(
            flags=h.read_int64(),
            left=h.read_float(),
            top=h.read_float(),
            right=h.read_float(),
            bottom=h.read_float(),
            texture_index=h.read_int32(),
            cutout_uv_count=h.read_int32(),
        )
        pat.cutout_uvs = [(h.read_float(), h.read_float()) for _ in range(pat.cutout_uv_count)] if pat.cutout_uv_count > 0 else []
        return pat



    def write(self) -> bytes:
        h = BinaryHandler(bytearray(), file_version=self.version)
        hdr = self.header
        hdr.magic = UVS_MAGIC
        hdr.texture_count = len(self.textures)
        hdr.sequence_count = len(self.sequences)

        self._write_header(h, final=False)

        hdr.tex_offset = h.tell
        strings, char_len = [], 0

        def append_path(path: str) -> int:
            nonlocal char_len
            p = path or ""
            if not p:
                return -1
            offset = char_len
            strings.append(p)
            char_len += len(p) + 1
            return offset

        for tex in self.textures:
            tex.path_string_offset, tex.tex_handle_1, tex.tex_handle_2, tex.tex_handle_3 = (
                append_path(p) for p in (tex.path, tex.normal_path, tex.specular_path, tex.alpha_path)
            )
            self._write_texture(h, tex)

        hdr.sequence_offset = h.tell
        hdr.pattern_count = 0
        for seq in self.sequences:
            seq.pattern_table_offset = hdr.pattern_count
            seq.pattern_count = len(seq.patterns)
            hdr.pattern_count += seq.pattern_count
            h.write_int32(seq.pattern_count)
            h.write_int32(seq.pattern_table_offset)

        hdr.pattern_offset = h.tell
        for seq in self.sequences:
            for pat in seq.patterns:
                self._write_pattern(h, pat)

        hdr.string_offset = h.tell
        if strings:
            h.write_wstring("\0".join(strings) + "\0")
        
        if (string_table_size := h.tell - hdr.string_offset) > 0:
            target_blocks = 1 << ((string_table_size + 255) // 256 - 1).bit_length()
            if padding := (target_blocks * 256) - string_table_size:
                h.write_bytes(b"\x00" * padding)

        self._write_header(h, final=True)
        return bytes(h.data)

    @staticmethod
    def _write_texture(h: BinaryHandler, tex: UvsTexture):
        h.write_int64(tex.state_holder)
        h.write_int64(tex.path_string_offset)
        h.write_int64(tex.tex_handle_1)
        h.write_int64(tex.tex_handle_2)
        h.write_int64(tex.tex_handle_3)

    @staticmethod
    def _write_pattern(h: BinaryHandler, pat: UvsPattern):
        if pat.cutout_uvs:
            pat.cutout_uv_count = len(pat.cutout_uvs)
        elif pat.cutout_uv_count > 0:
            pat.cutout_uv_count = 0
        elif pat.cutout_uv_count not in (-1, 0):
            pat.cutout_uv_count = -1

        h.write_int64(pat.flags)
        h.write_float(pat.left)
        h.write_float(pat.top)
        h.write_float(pat.right)
        h.write_float(pat.bottom)
        h.write_int32(pat.texture_index)
        h.write_int32(pat.cutout_uv_count)
        
        if pat.cutout_uv_count > 0:
            for u, v in pat.cutout_uvs:
                h.write_float(u)
                h.write_float(v)

    def _write_header(self, h: BinaryHandler, final: bool):
        if final:
            h.seek(0)
        h.write_uint32(self.header.magic)
        h.write_int32(self.header.texture_count)
        h.write_int32(self.header.sequence_count)
        h.write_int32(self.header.pattern_count)
        if self.version >= 7:
            h.write_int32(self.header.attributes)
            h.write_int32(0)
        h.write_int64(self.header.tex_offset)
        h.write_int64(self.header.sequence_offset)
        h.write_int64(self.header.pattern_offset)
        h.write_int64(self.header.string_offset)
