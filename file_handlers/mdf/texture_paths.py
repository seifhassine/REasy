from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtWidgets import QCompleter, QLineEdit, QStyledItemDelegate

lazy from utils.resource_file_utils import listed_resource_paths


class TexturePathCompleter(QCompleter):
    def __init__(self, handler, parent):
        super().__init__(parent)
        self._handler = handler
        self.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterMode(Qt.MatchFlag.MatchContains)
        self.popup().setUniformItemSizes(True)

    def attach(self, editor: QLineEdit) -> None:
        if self.model() is None:
            self.setModel(QStringListModel(listed_resource_paths(self._handler, "tex"), self))
        editor.setCompleter(self)


class TexturePathDelegate(QStyledItemDelegate):
    def __init__(self, completer: TexturePathCompleter, parent):
        super().__init__(parent)
        self._completer = completer

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        self._completer.attach(editor)
        return editor
