import struct
from typing import Optional, Dict, Any, List

from file_handlers.base_handler import BaseFileHandler

from .tex_file import TexFile, TEX_MAGIC
from .dds import build_dds_dx10


class TexHandler(BaseFileHandler):

    def __init__(self):
        super().__init__()
        self.tex: Optional[TexFile] = None
        self.raw_data: bytes | bytearray = b""

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        magic = struct.unpack_from('<I', data, 0)[0]
        return magic == TEX_MAGIC

    def supports_editing(self) -> bool:
        return False

    def read(self, data: bytes):
        self.raw_data = data
        tex = TexFile()
        if not tex.read(data):
            raise ValueError("Failed to parse TEX file")
        self.tex = tex
        self.modified = False

    def rebuild(self) -> bytes:
        return bytes(self.raw_data)

    def populate_treeview(self, tree, parent_item, metadata_map: dict):
        return

    def get_context_menu(self, tree, item, meta: dict):
        return None

    def handle_edit(self, meta: Dict[str, Any], new_val, old_val, item):
        pass

    def add_variables(self, target, prefix: str, count: int):
        pass

    def update_strings(self):
        pass

    def create_viewer(self):
        try:
            from .tex_viewer import TexViewer
            v = TexViewer(self)
            v.modified_changed.connect(self.modified_changed.emit)
            return v
        except Exception:
            return None

    def build_dds_bytes(self, image_index: int = 0) -> bytes:
        if not self.tex:
            return b""
        header = self.tex.header
        mip_bytes: List[bytes] = []

        if header.format_is_block_compressed() and not self.tex.header_is_power_of_two():
            for level in range(header.mip_count):
                data, w, h = self.tex.read_non_pot_level(level, image_index)
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

