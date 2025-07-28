# ui/project_manager/proxy.py

from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtWidgets import QIdentityProxyModel

class ActionsProxyModel(QIdentityProxyModel):
    """
    Wraps any QAbstractItemModel and inserts a fake column 0 for actions,
    shifting the real columns one to the right.
    """

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return super().columnCount(parent) + 1

    def mapToSource(self, proxyIndex: QModelIndex) -> QModelIndex:
        if not proxyIndex.isValid() or proxyIndex.column() == 0:
            return QModelIndex()
        return self.sourceModel().index(
            proxyIndex.row(),
            proxyIndex.column() - 1,
            self.mapToSource(proxyIndex.parent())
        )

    def mapFromSource(self, sourceIndex: QModelIndex) -> QModelIndex:
        if not sourceIndex.isValid():
            return QModelIndex()
        parent = self.mapFromSource(sourceIndex.parent())
        return self.index(
            sourceIndex.row(),
            sourceIndex.column() + 1,
            parent
        )

    def data(self, proxyIndex: QModelIndex, role: int = Qt.DisplayRole):
        if not proxyIndex.isValid():
            return None

        if proxyIndex.column() == 0:
            if role == Qt.DisplayRole:
                return ""
            return None

        return self.sourceModel().data(self.mapToSource(proxyIndex), role)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            if section == 0:
                return "" 
            return self.sourceModel().headerData(section - 1, orientation, role)
        return super().headerData(section, orientation, role)
