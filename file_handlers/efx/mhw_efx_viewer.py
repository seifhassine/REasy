"""Read-only World EFX attribute and timeline inspector."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem


class MhwEfxViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Field", "Value", "Type"])
        layout = QVBoxLayout(self)
        layout.addWidget(self.tree)
        document = handler.efx.document
        self._value(self.tree.invisibleRootItem(), "Format", document["format"])
        self._value(self.tree.invisibleRootItem(), "Version", document["version"])
        for name, value in document.items():
            if name not in ("format", "version", "roundtrip_exact"):
                self._value(self.tree.invisibleRootItem(), name, value)
        self.tree.expandToDepth(0)
        self.tree.resizeColumnToContents(0)

    def _value(self, parent, name, value):
        if isinstance(value, dict):
            title = f"{name}: {value['type']}" if "type" in value else str(name)
            if "name" in value and "index" in value:
                title = f"{value['index']}: {value['name']}"
            row = QTreeWidgetItem(parent, [title, "", "Object"])
            for key, child in value.items():
                self._value(row, key, child)
        elif isinstance(value, list):
            row = QTreeWidgetItem(parent, [str(name), str(len(value)), "Array"])
            for index, child in enumerate(value):
                self._value(row, index, child)
        else:
            text = str(value).replace("\0", "\\0") if value is not None else ""
            QTreeWidgetItem(parent, [str(name), text, type(value).__name__])
