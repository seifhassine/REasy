from __future__ import annotations
import os
import weakref
from bisect import bisect_left
from dataclasses import dataclass
from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtWidgets import QTreeView, QAbstractItemView, QMessageBox

@dataclass(slots=True)
class _PakNode:
    path: str = ''
    parent: object = None
    row: int = 0
    start: int = 0
    end: int = 0
    children: list | None = None


class PakPathModel(QAbstractItemModel):
    """``paths`` must be sorted."""
    def __init__(self, paths, directory_icon, file_icon, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.directory_icon, self.file_icon = directory_icon, file_icon
        self.root = _PakNode(self._native_prefix(paths), end=len(paths))

    @staticmethod
    def _native_prefix(paths) -> str:
        if not paths or not paths[0].startswith('natives/'):
            return ''
        prefix = paths[0][:paths[0].find('/', len('natives/')) + 1]
        return prefix if prefix and paths[-1].startswith(prefix) else ''

    def display_parts(self, path: str) -> list[str]:
        """Row names from the top level down to ``path``."""
        if path.startswith(self.root.path):
            path = path[len(self.root.path):]
        return [part for part in path.rstrip('/').split('/') if part]

    def _children(self, node):
        if node.children is None:
            node.children = []
            first = node.start
            while first < node.end:
                name, separator, _ = self.paths[first][len(node.path):].partition('/')
                path = node.path + name + separator
                # '0' follows '/' in lexical order, so it bounds this subtree.
                end = bisect_left(self.paths, node.path + name + '0', first, node.end) if separator else first + 1
                child = _PakNode(path, node, len(node.children), first, end,
                                 None if separator else [])
                node.children.append(child)
                first = end
        return node.children

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid() and parent.column() != 0:
            return 0
        return len(self._children(parent.internalPointer() if parent.isValid() else self.root))

    def hasChildren(self, parent=QModelIndex()):
        if parent.isValid() and parent.column() != 0:
            return False
        node = parent.internalPointer() if parent.isValid() else self.root
        return node.start < node.end and (node is self.root or node.path.endswith('/'))

    def columnCount(self, parent=QModelIndex()):
        return 1

    def index(self, row, column, parent=QModelIndex()):
        if column != 0 or row < 0 or (parent.isValid() and parent.column() != 0):
            return QModelIndex()
        children = self._children(parent.internalPointer() if parent.isValid() else self.root)
        return self.createIndex(row, column, children[row]) if row < len(children) else QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer().parent
        return QModelIndex() if node is self.root else self.createIndex(node.row, 0, node)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        path = index.internalPointer().path
        directory = path.endswith('/')
        if role == Qt.DisplayRole:
            return path.rstrip('/').rsplit('/', 1)[-1]
        if role == Qt.DecorationRole:
            return self.directory_icon if directory else self.file_icon
        if role == Qt.UserRole + (2 if directory else 1):
            return path
        return None

    def flags(self, index):
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable if index.isValid() else Qt.NoItemFlags

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        return self.tr('Paths') if section == 0 and orientation == Qt.Horizontal and role == Qt.DisplayRole else None


class _UrlDragTree(QTreeView):
    @staticmethod
    def _handle_url_drag(event, fallback):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            fallback(event)

    def dragEnterEvent(self, event):
        self._handle_url_drag(event, super().dragEnterEvent)

    def dragMoveEvent(self, event):
        self._handle_url_drag(event, super().dragMoveEvent)


class _DndTree(_UrlDragTree):
    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setAcceptDrops(False)
        self.setDragDropMode(QAbstractItemView.DragOnly)


class _DropTree(_UrlDragTree):
    """Project‑Files tree – accepts all URL drops, but only copies in‑folder items."""
    def __init__(self, mgr):
        super().__init__()
        # Weak: the manager holds this tree, so a strong reference would make a cycle for the garbage collector.
        self._mgr = weakref.ref(mgr)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setDefaultDropAction(Qt.CopyAction)

    @property
    def mgr(self):
        return self._mgr()

    def _inside_unpacked(self, path: str) -> bool:
        up = os.path.abspath(self.mgr.unpacked_dir or "")
        return up and os.path.abspath(path).startswith(up + os.sep)

    def dropEvent(self, e):
        if not e.mimeData().hasUrls():
            return super().dropEvent(e)
        e.acceptProposedAction()

        blocked = []
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if self._inside_unpacked(p):
                self.mgr._copy_to_project(p)
            else:
                blocked.append(p)

        if blocked:
            QMessageBox.warning(
                self, self.tr("Add Blocked"),
                self.tr("These items are outside the selected unpacked game folder and were ignored:")
                + "\n" + "\n".join(blocked)
            )
