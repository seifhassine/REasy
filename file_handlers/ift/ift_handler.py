from __future__ import annotations

from file_handlers.base_handler import BaseFileHandler
from file_handlers.uvs.uvs_file import UvsFile
from utils.resource_file_utils import (
    resolve_handler_resource_data,
    resource_version_from_path,
)

from .ift_file import IftFile
from .model import IconGlyph


class IftHandler(BaseFileHandler):
    def __init__(self) -> None:
        super().__init__()
        self.ift: IftFile | None = None
        self.companion_uvs: UvsFile | None = None
        self.companion_uvs_version: int | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return IftFile.can_handle(data)

    def read(self, data: bytes) -> None:
        self.ift = IftFile.from_bytes(data)
        self.companion_uvs = None
        self.modified = False

    def rebuild(self) -> bytes:
        if self.ift is None:
            raise ValueError("no IFT file is loaded")
        result = self.ift.write()
        self.modified = False
        return result

    def supports_editing(self) -> bool:
        return True

    def load_companion_uvs(self) -> UvsFile:
        if self.companion_uvs is not None:
            return self.companion_uvs
        if self.ift is None:
            raise ValueError("no IFT file is loaded")
        reference = self.ift.require_model().uv_sequence_path
        resolved = resolve_handler_resource_data(
            self, reference, allow_selection_dialog=False
        )
        if resolved is None:
            raise FileNotFoundError(f"unable to resolve IFT UVS dependency: {reference}")
        path, data = resolved
        version = (
            resource_version_from_path(path, "uvs")
            or resource_version_from_path(reference, "uvs")
            or self.companion_uvs_version
        )
        if version is None:
            raise ValueError(
                "cannot determine companion UVS version from its path; set "
                "companion_uvs_version explicitly"
            )
        uvs = UvsFile()
        uvs.read(data, version=version)
        self.companion_uvs = uvs
        return uvs

    def resolve_icon(
        self, name: str, *, load_uvs: bool = True
    ) -> IconGlyph | None:
        if self.ift is None:
            return None
        return self.ift.resolve(name, self.load_companion_uvs() if load_uvs else None)

    def populate_treeview(self, tree, _parent_item, _metadata_map: dict) -> None:
        if self.ift is None:
            return
        from PySide6.QtGui import QStandardItem, QStandardItemModel

        data = self.ift.require_model()
        model = QStandardItemModel()
        model.setHorizontalHeaderLabels(["Field", "Value"])

        def add(name: str, value: object, parent=None):
            row = [QStandardItem(name), QStandardItem(str(value))]
            (parent or model).appendRow(row)
            return row[0]

        add("version", data.version)
        add("descent", data.descent)
        add("fontSize", data.font_size)
        add("reserved", f"0x{data.reserved:08X}")
        add("uvSequencePath", data.uv_sequence_path)
        entries = add("entries", len(data.entries))
        for index, entry in enumerate(data.entries):
            item = add(f"[{index}] {entry.name}", "", entries)
            add("uvSequenceNo", entry.uv_sequence_no, item)
            add("uvPatternNo", entry.uv_pattern_no, item)
            add("width", entry.width, item)
            add("height", entry.height, item)
            add("sourceOffset", f"0x{entry.source_offset:X}", item)
            add("nameOffset", f"0x{entry.name_offset:X}", item)
        tree.setModel(model)
        tree.expandToDepth(1)

    def create_viewer(self):
        return None
