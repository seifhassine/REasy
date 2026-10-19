from __future__ import annotations

import threading
from collections.abc import Callable
from ctypes import wintypes

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QWidget

# Also the strand step rate when uncapped: each step is an image, so steps need a rate to pace them.
LIMIT_MOST = 240

_WM_NCHITTEST, _WM_DESTROY, _HTTRANSPARENT = 0x0084, 0x0002, -1


class FrameWindow(QWidget):
    window_changed = Signal(int)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        for attribute in (Qt.WA_NativeWindow, Qt.WA_PaintOnScreen, Qt.WA_NoSystemBackground,
                          Qt.WA_TransparentForMouseEvents, Qt.WA_DontCreateNativeAncestors):
            self.setAttribute(attribute)
        self.setFocusPolicy(Qt.NoFocus)

    def paintEngine(self):
        return None

    def pixels(self) -> tuple[int, int]:
        size = self.size() * self.devicePixelRatioF()
        return size.width(), size.height()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.WinIdChange:
            self.window_changed.emit(int(self.effectiveWinId() or 0))
        return super().event(event)

    def nativeEvent(self, kind, message):
        if bytes(kind) == b"windows_generic_MSG":
            sent = wintypes.MSG.from_address(int(message))
            if sent.message == _WM_NCHITTEST:
                return True, _HTTRANSPARENT
            if sent.message == _WM_DESTROY:
                self.window_changed.emit(0)
        return super().nativeEvent(kind, message)


class Presenter:
    def __init__(self):
        self._lock = threading.Lock()
        self._window: int | None = None
        self._showing: tuple[object, int] | None = None

    def place(self, window: int | None) -> None:
        with self._lock:
            if window != self._window:
                self._hide()
                self._window = window

    def show(self, scene, image) -> None:
        """``image`` is a texture of the device's ``scene``."""
        with self._lock:
            if self._window is None:
                return
            if self._showing != (scene, self._window):
                self._hide()
            scene.show(self._window, image)
            self._showing = (scene, self._window)

    def hide(self) -> None:
        """Needed before a shown scene leaves the device."""
        with self._lock:
            self._hide()

    def _hide(self) -> None:
        if self._showing is not None:
            scene, window = self._showing
            scene.hide(window)
            self._showing = None


class RenderThread(threading.Thread):
    def __init__(self, drawer, finished: Callable[[tuple, object], None]):
        super().__init__(name="Viewport frames", daemon=True)
        self._drawer, self._finished = drawer, finished
        self._condition = threading.Condition()
        self._wanted: tuple | None = None
        self._stopping = False

    def ask(self, request: tuple) -> None:
        with self._condition:
            self._wanted = request
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify()

    def run(self) -> None:
        while True:
            with self._condition:
                while self._wanted is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    break
                request, self._wanted = self._wanted, None
            try:
                result = self._drawer.draw(*request)
                self._drawer.show()
            except Exception as exc:  # noqa: BLE001 - the viewer tells why nothing is drawn
                result = exc
            # Once stopped, the receiver may be gone.
            with self._condition:
                if self._stopping:
                    break
                self._finished(request, result)
        self._drawer.close()


__all__ = ["LIMIT_MOST", "FrameWindow", "Presenter", "RenderThread"]
