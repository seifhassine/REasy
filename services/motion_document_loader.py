"""Parse MOTLIST documents off the UI thread; only the newest request is delivered."""
from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal, Slot


def parse_motion_document(data, label):
    from file_handlers.motion.motlist_file import MotListFile
    document = MotListFile()
    document.read(data, label=label)
    return document


class _ParseJob(QThread):
    ready = Signal(object)
    failed = Signal(str)
    _running = set()
    _shutdown_connected = False

    def __init__(self, data, label, parser):
        super().__init__()
        self.data, self.label, self.parser = data, label, parser
        if not self._shutdown_connected:
            QCoreApplication.instance().aboutToQuit.connect(self.shutdown)
            type(self)._shutdown_connected = True
        self._running.add(self)
        self.finished.connect(self._retire)

    def run(self):
        try:
            document = self.parser(self.data, self.label)
            if not self.isInterruptionRequested():
                self.ready.emit(document)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))

    @Slot()
    def _retire(self):
        self._running.discard(self)
        self.deleteLater()

    @classmethod
    def shutdown(cls):
        for job in tuple(cls._running):
            job.requestInterruption()
            job.wait()


class MotionDocumentLoader(QObject):
    def __init__(self, parent, loaded, failed, *, parser=parse_motion_document):
        super().__init__(parent)
        self._loaded, self._failed, self._parser = loaded, failed, parser
        self._job = None
        self._pending = None
        self._closed = False
        self._revision = 0

    def submit(self, data, label, context):
        self._revision += 1
        self._pending = (data, label, context)
        if self._job is None:
            self._start()
        else:
            self._job.requestInterruption()

    def _start(self):
        self._job_revision = self._revision
        data, label, self._context = self._pending
        self._pending = None
        self._job = _ParseJob(data, label, self._parser)
        self._job.ready.connect(self._ready)
        self._job.failed.connect(self._error)
        self._job.finished.connect(self._finished)
        self._job.start()

    @Slot(object)
    def _ready(self, document):
        if not self._closed and self._job_revision == self._revision:
            self._loaded(document, self._job.data, self._context)

    @Slot(str)
    def _error(self, message):
        if not self._closed and self._job_revision == self._revision:
            self._failed(message, self._context)

    @Slot()
    def _finished(self):
        self._job = None
        if not self._closed and self._pending is not None:
            self._start()

    def close(self):
        self._closed = True
        self.cancel()

    def cancel(self):
        self._revision += 1
        self._pending = None
        if self._job is not None:
            self._job.requestInterruption()
