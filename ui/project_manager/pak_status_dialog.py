from __future__ import annotations
from typing   import Callable

from PySide6.QtCore    import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QProgressBar, QPushButton
)


class _Bridge(QObject):
    prog = Signal(int)    
    text = Signal(str)
    done = Signal()


class DownloadStatusDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.resize(360, 120)

        lay = QVBoxLayout(self)
        self._lbl  = QLabel("Starting…", self)
        self._prog = QProgressBar(self)
        self._prog.setRange(0, 100)
        self._prog.setValue(0)

        self._close_btn = QPushButton("Close", self, enabled=False)
        self._close_btn.clicked.connect(self.accept)

        lay.addWidget(self._lbl)
        lay.addWidget(self._prog)
        lay.addWidget(self._close_btn)

        self._bridge = _Bridge()
        self._bridge.prog.connect(self._prog.setValue)
        self._bridge.text.connect(self._lbl.setText)
        self._bridge.done.connect(self._on_done)

    @property
    def bridge(self) -> _Bridge:
        """Return the bridge so worker threads can emit signals."""
        return self._bridge

    def _on_done(self):
        self._close_btn.setEnabled(True)
        self._lbl.setText(self._lbl.text() + " – finished.")


def run_with_progress(parent, title: str,
                      download_fn: Callable[[_Bridge], None]) -> None:
    dlg = DownloadStatusDialog(title, parent)
    try:
        download_fn(dlg.bridge)
    finally:
        dlg.bridge.done.emit()
    dlg.exec()
