from __future__ import annotations

from file_handlers.base_handler import BaseFileHandler
from utils.resource_file_utils import resource_version_from_path

from .codec import OFT_CODECS
from .oft_file import OftFile


class OftHandler(BaseFileHandler):
    def __init__(self) -> None:
        super().__init__()
        self.oft: OftFile | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return OftFile.can_handle(data)

    def read(self, data: bytes) -> None:
        version = resource_version_from_path(self.filepath, "oft")
        if version is None:
            if len(OFT_CODECS) != 1:
                raise ValueError(
                    "cannot determine OFT version from the filename; multiple "
                    "codecs are registered"
                )
            version = next(iter(OFT_CODECS))
        self.oft = OftFile.from_bytes(data, version=version)
        self.modified = False

    def rebuild(self) -> bytes:
        if self.oft is None:
            raise ValueError("no OFT file is loaded")
        result = self.oft.write()
        self.modified = False
        return result

    def supports_editing(self) -> bool:
        return True

    def populate_treeview(self, tree, _parent_item, _metadata_map: dict) -> None:
        if self.oft is None:
            return
        from PySide6.QtGui import QStandardItem, QStandardItemModel

        model = QStandardItemModel()
        model.setHorizontalHeaderLabels(["Field", "Value"])

        def add(name: str, value: object, parent=None):
            row = [QStandardItem(name), QStandardItem(str(value))]
            (parent or model).appendRow(row)
            return row[0]

        add("version", self.oft.version)
        add("wrapper", "FBFO")
        add("decodedSize", len(self.oft.sfnt_data))
        font = self.oft.font()
        font_item = add("sfnt", font.sfnt_version.hex(" "))
        add("familyName", font.family_name or "", font_item)
        add("fullName", font.full_name or "", font_item)
        add("unitsPerEm", font.units_per_em, font_item)
        add("glyphCount", font.glyph_count, font_item)
        add("faces", len(font.face_offsets), font_item)
        add("tables", ", ".join(font.table_tags), font_item)
        tree.setModel(model)
        tree.expandToDepth(1)

    def create_viewer(self):
        return None
