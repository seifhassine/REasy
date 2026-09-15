from __future__ import annotations


from file_handlers.base_handler import BaseFileHandler
from utils.resource_file_utils import resolve_handler_resource_data

from .gcf_file import GcfFile
from .model import GcfData

class GcfHandler(BaseFileHandler):
    def __init__(self) -> None:
        super().__init__()
        self.gcf: GcfFile | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return GcfFile.can_handle(data)

    def read(self, data: bytes) -> None:
        self.gcf = GcfFile.from_bytes(data)
        self.modified = False

    def rebuild(self) -> bytes:
        if self.gcf is None:
            raise ValueError("no GCF file is loaded")
        result = self.gcf.write()
        self.modified = False
        return result

    def supports_editing(self) -> bool:
        return True

    def resolve_resource(self, resource_path: str) -> tuple[str, bytes] | None:
        return resolve_handler_resource_data(
            self,
            resource_path,
            allow_selection_dialog=False,
        )

    @property
    def gcf_data(self) -> GcfData:
        if self.gcf is None:
            raise ValueError("no GCF file is loaded")
        return self.gcf.require_model()

    def populate_treeview(self, tree, _parent_item, _metadata_map: dict) -> None:
        if self.gcf is None:
            return
        from PySide6.QtGui import QStandardItem, QStandardItemModel

        data = self.gcf_data
        model = QStandardItemModel()
        model.setHorizontalHeaderLabels(["Field", "Value"])

        def add(name: str, value: object, parent=None):
            row = [QStandardItem(name), QStandardItem(str(value))]
            (parent or model).appendRow(row)
            return row[0]

        add("version", data.version)
        add("delayLanguageFontLoad", data.delay_language_font_load)
        add("defaultRubySizeRatio", data.default_ruby_size_ratio)
        add("rootReserved", f"0x{data.root_reserved:08X}")
        add("iconFontAssetPath", data.icon_font_asset_path or "")

        dimensions = add("fontDimensions", "")
        add("languages", data.language_count, dimensions)
        add("slots", data.font_slot_count, dimensions)
        add("pathsPerSlot", data.font_asset_path_count, dimensions)

        fonts = add("fontSlots", len(data.font_slots))
        for mapping in data.font_slots:
            item = add(
                f"{mapping.language_name}/{mapping.slot_name}",
                "",
                fonts,
            )
            for index, path in enumerate(mapping.asset_paths):
                add(f"path[{index}]", path or "", item)
            add("adjustScale", mapping.adjust_scale, item)

        messages = add("messageAssets", len(data.message_asset_paths))
        add("reserved", f"0x{data.message_section_reserved:08X}", messages)
        for index, path in enumerate(data.message_asset_paths):
            add(f"[{index}]", path or "", messages)

        triplets = add("assetLanguageTriplets", len(data.asset_language_triplets))
        for triplet in data.asset_language_triplets:
            add(
                triplet.asset_language_name,
                (triplet.value_0, triplet.value_1, triplet.value_2),
                triplets,
            )

        localize = add("localizeAssets", len(data.localize_assets))
        add("reserved", f"0x{data.localize_section_reserved:08X}", localize)
        for index, asset in enumerate(data.localize_assets):
            item = add(f"[{index}] {asset.slot_name}", asset.path or "", localize)
            add("slot", asset.slot, item)
            add("reserved", f"0x{asset.reserved:08X}", item)

        if self.gcf.layout is not None:
            layout = add("sourceLayout", "")
            add("rootOffset", f"0x{self.gcf.layout.root_offset:X}", layout)
            add(
                "messageAssetsOffset",
                f"0x{self.gcf.layout.message_assets_offset:X}",
                layout,
            )
            add(
                "assetLanguageTripletsOffset",
                f"0x{self.gcf.layout.asset_language_triplets_offset:X}",
                layout,
            )
            add(
                "localizeAssetsOffset",
                f"0x{self.gcf.layout.localize_assets_offset:X}",
                layout,
            )

        tree.setModel(model)
        tree.expandToDepth(1)

    def create_viewer(self):
        return None
