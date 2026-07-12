import struct
from typing import Optional

from .tex_file import TexFile, TEX_MAGIC
from .dds import build_dds_dx10
from .texture_handler import TextureViewerHandler


class TexHandler(TextureViewerHandler):

    def __init__(self):
        super().__init__()
        self.tex: Optional[TexFile] = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        magic = struct.unpack_from('<I', data, 0)[0]
        return magic == TEX_MAGIC

    def read(self, data: bytes):
        self.raw_data = data
        tex = TexFile()
        file_version = 0
        filepath = getattr(self, "filepath", "") or ""
        try:
            lowered = filepath.lower()
            if ".tex." in lowered:
                file_version = int(lowered.rsplit(".tex.", 1)[1])
        except (TypeError, ValueError, IndexError):
            file_version = 0

        ok = tex.read(data, file_version=file_version)

        if not ok:
            raise ValueError("Failed to parse TEX file")
        self.tex = tex
        self.modified = False

    def build_dds_bytes(self, image_index: int = 0) -> bytes:
        if not self.tex:
            return b""
        header = self.tex.header
        mip_bytes: list[bytes] = []

        if header.format_is_block_compressed() and not self.tex.header_is_power_of_two():
            for level in range(header.mip_count):
                data, _, _ = self.tex.read_non_pot_level(level, image_index)
                mip_bytes.append(data)
        else:
            for level in range(header.mip_count):
                m = self.tex.get_mip_map_data(level, image_index)
                mip_bytes.append(m.data)

        dds_header = build_dds_dx10(
            width=header.width,
            height=header.height,
            mip_count=header.mip_count,
            dxgi_format=header.format,
            array_size=max(1, getattr(header, 'image_count', 1)),
        )
        return dds_header + b"".join(mip_bytes)

