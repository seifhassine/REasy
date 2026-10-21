from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout


class _Signals(QObject):
    finished = Signal(int, object, str)


class _Load(QRunnable):
    def __init__(self, token, operation):
        super().__init__()
        self.token, self.operation, self.signals = token, operation, _Signals()

    def run(self):
        try:
            result = self.operation()
        except Exception as error:
            self.signals.finished.emit(self.token, None, str(error) or type(error).__name__)
        else:
            self.signals.finished.emit(self.token, result, "")


class BackgroundJobs(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._closed = False
        self._token, self._jobs = 0, {}

    def start(self, operation, done, failed) -> None:
        if self._closed:
            return
        self._token += 1
        task = _Load(self._token, operation)
        self._jobs[self._token] = task, done, failed
        task.signals.finished.connect(self._finished)
        QThreadPool.globalInstance().start(task)

    def close(self) -> None:
        self._closed = True
        self._token += 1
        self._jobs = {token: (task, None, None) for token, (task, _done, _failed) in self._jobs.items()}

    @Slot(int, object, str)
    def _finished(self, token, result, error):
        _task, done, failed = self._jobs.pop(token)
        if self._closed or token != self._token:
            return
        if error:
            failed(error)
        else:
            done(result)


class _Job(QThread):
    progress = Signal(int, int)

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.operation(self.progress.emit, self.isInterruptionRequested)
        except Exception as error:
            self.error = error


class _JobDialog(QDialog):
    def __init__(self, parent, title, operation, status=None, describe=None, cancellable=False):
        super().__init__(parent)
        self.describe = describe
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        self.status = QLabel(status or self.tr("Preparing…"))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.cancel_button = None
        if cancellable:
            self.cancel_button = QPushButton(self.tr("Cancel"))
            self.cancel_button.clicked.connect(self.reject)
            buttons = QHBoxLayout()
            buttons.setContentsMargins(0, 4, 0, 0)
            buttons.addStretch()
            buttons.addWidget(self.cancel_button)
            layout.addLayout(buttons)
        self.job = _Job(operation, self)
        self.job.progress.connect(self._progress)
        self.job.finished.connect(self.accept)

    def _progress(self, current, total):
        self.progress.setRange(0, total)
        self.progress.setValue(current)
        if self.describe is not None and not self.job.isInterruptionRequested():
            self.status.setText(self.describe(current, total))

    def reject(self):
        if not self.job.isRunning():
            super().reject()
        elif self.cancel_button is not None:
            self.job.requestInterruption()
            self.cancel_button.setEnabled(False)
            self.status.setText(self.tr("Cancelling…"))

    def closeEvent(self, event):
        if self.job.isRunning():
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)


def run_job(parent, title, operation, *, status=None, describe=None, cancellable=False, cancel_error=None):
    """``operation(progress, cancelled)`` runs on a thread: ``progress(current, total)`` fills the bar and
    ``describe(current, total)`` gives its text. On cancel it returns what the operation returned, or raises
    ``cancel_error()`` when given."""
    dialog = _JobDialog(parent, title, operation, status, describe, cancellable or cancel_error is not None)
    dialog.job.start()
    dialog.exec()
    job = dialog.job
    job.wait()
    error, result, interrupted = job.error, job.result, job.isInterruptionRequested()
    # The dialog is ``parent``'s child until Qt deletes it, so what it and the job hold of the caller (and an error's
    # traceback) would form a cycle; the GC would then free Qt objects Qt already deleted.
    job.operation = job.error = job.result = dialog.describe = None
    dialog.deleteLater()
    if error:
        raise error
    if interrupted and cancel_error is not None:
        raise cancel_error()
    return result


__all__ = ["BackgroundJobs", "run_job"]
