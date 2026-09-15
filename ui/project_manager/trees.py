from __future__ import annotations
import os
from PySide6.QtCore   import Qt
from PySide6.QtWidgets import QTreeView, QAbstractItemView, QMessageBox

class _UrlDragTree(QTreeView):
    """Accept URL drag events and defer all other events to Qt."""

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
    """Readonly system‑files view – drag‑only."""
    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setAcceptDrops(False)
        self.setDragDropMode(QAbstractItemView.DragOnly)


class _DropTree(_UrlDragTree):
    """Project‑Files tree – accepts all URL drops, but only copies in‑folder items."""
    def __init__(self, mgr):
        super().__init__()
        self.mgr = mgr
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setDefaultDropAction(Qt.CopyAction)

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
                self, "Add Blocked",
                "These items are outside the selected unpacked game folder and were ignored:\n"
                + "\n".join(blocked)
            )
