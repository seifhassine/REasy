from __future__ import annotations

import enum

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

lazy from utils.native_build import ensure_fastaudio

_idle_timer = None


class PlaybackState(enum.Enum):
    StoppedState = 0
    PlayingState = 1
    PausedState = 2


def playback_unavailable() -> str:
    if ensure_fastaudio().available():
        return ""
    return QCoreApplication.translate("SoundPlayback", "Windows has no audio output device to play to.")


def wake_output():
    ensure_fastaudio().wake()
    _watch_idle()


def _watch_idle():
    global _idle_timer
    if _idle_timer is None:
        _idle_timer = QTimer(QCoreApplication.instance())
        _idle_timer.timeout.connect(lambda: ensure_fastaudio().suspend_if_idle(300))
        _idle_timer.start(15_000)


class AudioPlayer(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)
    finished = Signal()
    errorOccurred = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sound = []
        self.destroyed.connect(self._sound.clear)
        self._source = None
        self._state = PlaybackState.StoppedState
        self._volume, self._rate, self._loops = 1.0, 1.0, False
        self._poll = QTimer(self)
        self._poll.setInterval(30)
        self._poll.timeout.connect(self._tick)

    def source(self):
        return self._source

    def setSource(self, path):
        self.stop()
        self._sound.clear()
        self._source = path or None
        if not path:
            return
        try:
            self._sound.append(ensure_fastaudio().load(str(path)))
        except OSError as exc:
            self.errorOccurred.emit(str(exc))
            return
        self._apply()
        self.durationChanged.emit(self.duration())

    def play(self):
        if not self._sound:
            return
        ensure_fastaudio().play(self._sound[0], True)
        _watch_idle()
        self._poll.start()
        self._set_state(PlaybackState.PlayingState)

    def pause(self):
        if self._sound and self._state is PlaybackState.PlayingState:
            ensure_fastaudio().pause(self._sound[0])
            self._poll.stop()
            self._set_state(PlaybackState.PausedState)

    def stop(self):
        if self._sound:
            audio = ensure_fastaudio()
            audio.pause(self._sound[0])
            audio.seek(self._sound[0], 0)
        self._poll.stop()
        self._set_state(PlaybackState.StoppedState)

    def setPosition(self, milliseconds):
        if self._sound:
            ensure_fastaudio().seek(self._sound[0], milliseconds)
            self.positionChanged.emit(int(milliseconds))

    def position(self):
        return round(ensure_fastaudio().position(self._sound[0])) if self._sound else 0

    def duration(self):
        return round(ensure_fastaudio().duration(self._sound[0])) if self._sound else 0

    def playbackState(self):
        return self._state

    def setVolume(self, volume):
        self._volume = volume
        self._apply()

    def setPlaybackRate(self, rate):
        self._rate = rate
        self._apply()

    def setLoops(self, loops):
        self._loops = loops
        self._apply()

    def _apply(self):
        if self._sound:
            ensure_fastaudio().configure(self._sound[0], self._volume, self._rate, self._loops)

    def _set_state(self, state):
        if state is not self._state:
            self._state = state
            self.playbackStateChanged.emit(state)

    def _tick(self):
        self.positionChanged.emit(self.position())
        if not ensure_fastaudio().playing(self._sound[0]):
            self._poll.stop()
            self._set_state(PlaybackState.StoppedState)
            self.finished.emit()
