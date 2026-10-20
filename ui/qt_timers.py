from __future__ import annotations

lazy from PySide6.QtCore import QObject, QTimer


def call_later(owner: QObject, msec: int, callback) -> None:
    """QTimer.singleShot is buggy on PySide 6.11: without a context it still calls a lambda whose widget is gone,
    and with one at 0 ms it skips the call but keeps the callback alive forever. For a method requested repeatedly
    before it runs, prefer a persistent child QTimer."""
    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.timeout.connect(callback)
    timer.timeout.connect(timer.deleteLater)
    timer.start(msec)
