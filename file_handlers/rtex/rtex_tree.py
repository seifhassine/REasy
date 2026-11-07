from __future__ import annotations

lazy from PySide6.QtCore import QCoreApplication
lazy from PySide6.QtGui import QStandardItem, QStandardItemModel

from .rtex_file import RtexFile, field_kinds, value_names


def build_rtex_tree_model(rtex: RtexFile, game: str):
    translate = QCoreApplication.translate
    enums, flags = field_kinds()
    names = value_names(game)
    rows = [("Version", str(rtex.version))]
    for name, value in rtex.fields.items():
        if name in flags:
            text = translate("RtexTree", "On") if value else translate("RtexTree", "Off")
            text += "" if value in (0, 1) else f" ({value})"
        elif name in enums and value in names.get(name, {}):
            text = f"{names[name][value]} ({value})"
        else:
            text = f"{value:g}" if isinstance(value, float) else str(value)
        rows.append((name, text))
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels([translate("RtexTree", "Field"), translate("RtexTree", "Value")])
    for row in rows:
        items = [QStandardItem(text) for text in row]
        for item in items:
            item.setEditable(False)
        model.appendRow(items)
    return model
