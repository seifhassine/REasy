from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import (
    QElapsedTimer,
    QSignalBlocker,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.editor_widgets import EmbeddedPopupComboBox

from .controller import MotionPreviewController
from .model import PreviewLoopMode, RootDisplayMode


_FRAME_SLIDER_SCALE = 100
_MAX_TICK_SECONDS = 0.25
_TIMELINE_REFRESH_INTERVAL_MS = 50
_NUMERIC_REFRESH_INTERVAL_MS = 100


def _format_frame(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


class _MotionTimeline(QSlider):
    """Native slider that repaints only for a visible handle movement."""

    def __init__(self) -> None:
        super().__init__(Qt.Orientation.Horizontal)
        self._painted_pixel = -1

    def set_playback_value(self, value: int, *, force: bool = False) -> None:
        value = max(self.minimum(), min(self.maximum(), int(value)))
        span = self.maximum() - self.minimum()
        pixel = (
            round((value - self.minimum()) * max(1, self.width() - 1) / span)
            if span
            else 0
        )
        if not force and pixel == self._painted_pixel:
            return
        self._painted_pixel = pixel
        with QSignalBlocker(self):
            self.setValue(value)

    def setRange(self, minimum: int, maximum: int) -> None:
        self._painted_pixel = -1
        super().setRange(minimum, maximum)

    def resizeEvent(self, event) -> None:
        self._painted_pixel = -1
        super().resizeEvent(event)


class MotionPlaybackControls(QWidget):
    """Shared timeline and clock for every motion preview surface."""

    render_requested = Signal(bool)

    def __init__(
        self,
        controller: MotionPreviewController,
        *,
        frame_driver: Callable[[Callable[[], None] | None], None] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.controller = controller
        self._frame_driver = frame_driver
        self._elapsed = QElapsedTimer()
        self._ui_elapsed = QElapsedTimer()
        self._ui_timer = QTimer(self)
        self._ui_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._ui_timer.setInterval(_TIMELINE_REFRESH_INTERVAL_MS)
        self._ui_timer.timeout.connect(self._refresh_playback_ui)
        self._stop_on_hide = True
        self._build_ui()
        self.clear()

    def _build_ui(self) -> None:
        self.setObjectName("motionPlaybackControls")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        timeline = QHBoxLayout()
        timeline.setSpacing(5)
        self.frame_slider = _MotionTimeline()
        self.frame_slider.valueChanged.connect(self._on_slider_changed)
        timeline.addWidget(self.frame_slider, 1)
        self.frame_spin = QDoubleSpinBox()
        self.frame_spin.setObjectName("motionFrameSpin")
        self.frame_spin.setDecimals(2)
        self.frame_spin.setSingleStep(0.25)
        self.frame_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.frame_spin.setMaximumWidth(60)
        self.frame_spin.valueChanged.connect(self._on_frame_changed)
        timeline.addWidget(self.frame_spin)
        self.frame_total_label = QLabel("/ 0")
        self.frame_total_label.setObjectName("motionFrameTotal")
        timeline.addWidget(self.frame_total_label)
        root.addLayout(timeline)

        playback = QHBoxLayout()
        playback.setSpacing(5)
        self.restart_button = QToolButton()
        self.restart_button.setObjectName("motionRestartButton")
        self.restart_button.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_MediaSkipBackward
            )
        )
        self.restart_button.setToolTip(self.tr("Restart animation"))
        self.restart_button.setFixedSize(24, 22)
        self.restart_button.clicked.connect(self.restart)
        playback.addWidget(self.restart_button)
        self.play_button = QToolButton()
        self.play_button.setObjectName("motionPlayButton")
        self.play_button.setFixedSize(24, 22)
        self.play_button.clicked.connect(self.toggle)
        playback.addWidget(self.play_button)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.01, 100.0)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setSuffix("×")
        self.speed_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.speed_spin.setMaximumWidth(58)
        self.speed_spin.setToolTip(self.tr("Playback speed"))
        self.speed_spin.valueChanged.connect(self.controller.set_speed)
        playback.addWidget(self.speed_spin)

        playback.addStretch(1)
        root.addLayout(playback)

        options = QHBoxLayout()
        options.setSpacing(5)
        options.addWidget(QLabel(self.tr("Loop")))
        self.loop_combo = EmbeddedPopupComboBox()
        for label, mode in (
            (self.tr("Authored"), PreviewLoopMode.SOURCE),
            (self.tr("Loop"), PreviewLoopMode.LOOP),
            (self.tr("Once"), PreviewLoopMode.ONCE),
        ):
            self.loop_combo.addItem(label, mode)
        self.loop_combo.currentIndexChanged.connect(self._on_loop_mode_changed)
        options.addWidget(self.loop_combo, 1)

        options.addWidget(QLabel(self.tr("Root")))
        self.root_combo = EmbeddedPopupComboBox()
        self.root_combo.addItem(self.tr("Authored"), RootDisplayMode.AUTHORED)
        self.root_combo.addItem(
            self.tr("Lock translation"),
            RootDisplayMode.LOCK_TRANSLATION,
        )
        self.root_combo.currentIndexChanged.connect(self._on_root_mode_changed)
        options.addWidget(self.root_combo, 1)
        root.addLayout(options)
        self._set_playing_ui(False)

    def set_frame_driver(
        self,
        frame_driver: Callable[[Callable[[], None] | None], None],
    ) -> None:
        if self.controller.playing:
            raise RuntimeError("cannot replace the viewport frame driver during playback")
        self._frame_driver = frame_driver

    def configure(self) -> None:
        end = max(0.0, self.controller.end_frame)
        self.frame_slider.setRange(0, round(end * _FRAME_SLIDER_SCALE))
        self.frame_spin.setRange(0.0, end)
        self.frame_total_label.setText(f"/ {_format_frame(end)}")
        self._set_enabled(True)
        self._sync_frame_controls()

    def clear(self) -> None:
        self.stop()
        self._set_enabled(False)
        with QSignalBlocker(self.frame_slider), QSignalBlocker(self.frame_spin):
            self.frame_slider.setRange(0, 0)
            self.frame_spin.setRange(0.0, 0.0)
            self.frame_slider.setValue(0)
            self.frame_spin.setValue(0.0)
        self.frame_total_label.setText("/ 0")

    def toggle(self) -> None:
        if not self.controller.ready:
            return
        if self.controller.playing:
            self.stop()
            return
        if self.controller.current_frame >= self.controller.end_frame:
            self.controller.set_frame(0.0)
        if self._frame_driver is None:
            raise RuntimeError("motion playback requires a viewport frame driver")
        self.controller.playing = True
        self._elapsed.start()
        self._ui_elapsed.start()
        self._ui_timer.start()
        self._frame_driver(self._on_tick)
        self._set_playing_ui(True)

    def stop(self) -> None:
        self.controller.playing = False
        self._ui_timer.stop()
        if self._frame_driver is not None:
            self._frame_driver(None)
        self._sync_frame_controls()
        self._set_playing_ui(False)

    def set_authored_defaults(
        self,
        *,
        speed: float = 1.0,
        stop_at_motion_end: bool = False,
    ) -> None:
        self.controller.set_speed(speed)
        mode = (
            PreviewLoopMode.ONCE
            if stop_at_motion_end
            else PreviewLoopMode.SOURCE
        )
        with QSignalBlocker(self.speed_spin), QSignalBlocker(self.loop_combo):
            self.speed_spin.setValue(speed)
            self.loop_combo.setCurrentIndex(self.loop_combo.findData(mode))
        self.controller.loop_mode = mode
        self._set_playing_ui(False)

    def restart(self) -> None:
        self.stop()
        self.controller.restart()
        self._sync_frame_controls()
        self.render_requested.emit(False)

    def cleanup(self) -> None:
        self.stop()

    def set_stop_on_hide(self, enabled: bool) -> None:
        self._stop_on_hide = bool(enabled)

    def hideEvent(self, event) -> None:
        if self._stop_on_hide:
            self.stop()
        super().hideEvent(event)

    def _set_enabled(self, enabled: bool) -> None:
        for control in (
            self.play_button,
            self.restart_button,
            self.speed_spin,
            self.loop_combo,
            self.root_combo,
            self.frame_slider,
            self.frame_spin,
        ):
            control.setEnabled(enabled)

    def _on_slider_changed(self, value: int) -> None:
        self.controller.set_frame(value / _FRAME_SLIDER_SCALE)
        self._sync_frame_controls()
        self.render_requested.emit(False)

    def _on_frame_changed(self, value: float) -> None:
        self.controller.set_frame(value)
        self._sync_frame_controls()
        self.render_requested.emit(False)

    def _sync_frame_controls(self) -> None:
        self._sync_frame_slider(force=True)
        self._sync_frame_number()

    def _sync_frame_number(self) -> None:
        with QSignalBlocker(self.frame_spin):
            self.frame_spin.setValue(self.controller.current_frame)

    def _sync_frame_slider(self, *, force: bool = False) -> None:
        self.frame_slider.set_playback_value(
            round(self.controller.current_frame * _FRAME_SLIDER_SCALE),
            force=force,
        )

    def _refresh_playback_ui(self) -> None:
        if not self.controller.playing:
            return
        self._sync_frame_slider()
        if self._ui_elapsed.elapsed() >= _NUMERIC_REFRESH_INTERVAL_MS:
            self._ui_elapsed.restart()
            self._sync_frame_number()

    def _on_tick(self) -> None:
        if not self.controller.playing:
            return
        elapsed_seconds = max(0, self._elapsed.restart()) / 1000.0
        self.controller.advance(min(elapsed_seconds, _MAX_TICK_SECONDS))
        self.render_requested.emit(False)
        if not self.controller.playing:
            self.stop()

    def _set_playing_ui(self, playing: bool) -> None:
        self.play_button.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_MediaPause
                if playing
                else QStyle.StandardPixmap.SP_MediaPlay
            )
        )
        label = self.tr("Pause") if playing else self.tr("Play")
        self.play_button.setToolTip(label)
        self.play_button.setAccessibleName(label)

    def _on_loop_mode_changed(self, _index: int) -> None:
        mode = self.loop_combo.currentData()
        if isinstance(mode, PreviewLoopMode):
            self.controller.loop_mode = mode

    def _on_root_mode_changed(self, _index: int) -> None:
        mode = self.root_combo.currentData()
        if isinstance(mode, RootDisplayMode):
            self.controller.root_display_mode = mode
            self.render_requested.emit(False)

