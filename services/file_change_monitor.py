"""Observe local editor sources, including atomic replacements and recreation."""
import hashlib
import os

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal


def digest(data):
    return hashlib.sha256(data).digest()


class FileChangeMonitor(QObject):
    changed = Signal(bytes)
    failed = Signal(str)

    def __init__(self, parent=None, *, interval=200):
        super().__init__(parent)
        self.path = ''
        self._accepted = self._seen = None
        self._stamp = self._stable = None
        self._force = False
        self._watcher = QFileSystemWatcher(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(interval)
        self._timer.timeout.connect(self._check)
        self._watcher.fileChanged.connect(self._file_changed)
        self._watcher.directoryChanged.connect(self._directory_changed)

    def accept(self, path, data):
        path = os.path.abspath(path) if path else ''
        if path != self.path:
            self.close()
            self.path = path
        self._accepted = self._seen = digest(data)
        self._stamp = self._stat()
        self._stable = None
        self._arm()
        # A newer write may have arrived while this version was being parsed.
        self._file_changed()

    def _stat(self):
        try:
            stat = os.stat(self.path)
            return stat.st_mtime_ns, stat.st_size, stat.st_ino
        except FileNotFoundError:
            return None

    def _arm(self):
        if not self.path:
            return
        directory = os.path.dirname(self.path)
        if os.path.isdir(directory) and directory not in self._watcher.directories():
            self._watcher.addPath(directory)
        if os.path.isfile(self.path) and self.path not in self._watcher.files():
            self._watcher.addPath(self.path)

    def _file_changed(self, *_):
        self._force = True
        self._stable = None
        self._timer.start()

    def _directory_changed(self, *_):
        if not self._timer.isActive():
            self._timer.start()

    def _check(self):
        if not self.path:
            return
        self._arm()
        try:
            stamp = self._stat()
            if stamp is None:
                self._stamp = None
                return  # The directory watch will notice recreation.
            if stamp == self._stamp and not self._force:
                return
            if stamp != self._stable:
                self._stable = stamp
                self._timer.start()
                return
            with open(self.path, 'rb') as stream:
                data = stream.read()
            if self._stat() != stamp:
                self._stable = None
                self._timer.start()
                return
            self._stamp, self._force = stamp, False
            fingerprint = digest(data)
            if fingerprint != self._seen:
                self._seen = fingerprint
                self.changed.emit(data)
        except OSError as exc:
            self.failed.emit(str(exc))

    def differs_from_disk(self):
        if not self.path:
            return False
        try:
            with open(self.path, 'rb') as stream:
                return digest(stream.read()) != self._accepted
        except FileNotFoundError:
            return True

    def close(self):
        self._timer.stop()
        paths = self._watcher.files() + self._watcher.directories()
        if paths:
            self._watcher.removePaths(paths)
        self.path = ''
