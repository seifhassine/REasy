from __future__ import annotations
from typing   import Callable
import threading

from PySide6.QtCore    import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QProgressBar
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
        self._finished = False

        lay = QVBoxLayout(self)
        self._lbl  = QLabel(self.tr("Starting…"), self)
        self._prog = QProgressBar(self)
        self._prog.setRange(0, 100)
        self._prog.setValue(0)

        lay.addWidget(self._lbl)
        lay.addWidget(self._prog)

        self._bridge = _Bridge()
        self._bridge.prog.connect(self._prog.setValue)
        self._bridge.text.connect(self._lbl.setText)
        self._bridge.done.connect(self._on_done)

    @property
    def bridge(self) -> _Bridge:
        return self._bridge

    def _on_done(self):
        self._finished = True
        self.accept()

    def reject(self):
        if self._finished:  # Do not return to the caller while installation is running.
            super().reject()


def run_with_progress(parent, title: str,
                      download_fn: Callable[[_Bridge], None]) -> None:
    dlg = DownloadStatusDialog(title, parent)
    err: list[Exception] = []

    def _runner():
        try:
            download_fn(dlg.bridge)
        except Exception as e:
            err.append(e)
        finally:
            dlg.bridge.done.emit()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    dlg.exec()
    if err:
        raise err[0]
