from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QWidget

lazy from ui.editor_widgets import tool_button, toolbar_separator
lazy from ui.icons import line_icon
lazy from ui.styles import set_style_role

from .state import EditorState


class Transport(QWidget):
    def __init__(self, state: EditorState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self.setAttribute(Qt.WA_StyledBackground, True)
        set_style_role(self, surface="footer")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 3, 8, 3)
        row.setSpacing(4)
        row.addWidget(tool_button("skip_back", self.tr("Go to start (Home)"), lambda: state.set_frame(0.0)))
        self.play_button = tool_button("play", self.tr("Play (Space)"), self.toggle)
        row.addWidget(self.play_button)
        row.addWidget(tool_button("skip_forward", self.tr("Go to end (End)"), self.go_to_end))
        self.loop_button = tool_button("refresh", self.tr("Loop"))
        self.loop_button.setCheckable(True)
        self.loop_button.setChecked(True)
        row.addWidget(self.loop_button)
        row.addWidget(toolbar_separator())
        row.addWidget(QLabel(self.tr("Frame")))
        self.frame_spin = QDoubleSpinBox()
        self.frame_spin.setDecimals(2)
        self.frame_spin.setRange(0.0, 1e7)
        self.frame_spin.setProperty("scrub", True)
        self.frame_spin.setMaximumWidth(90)
        self.frame_spin.valueChanged.connect(state.set_frame)
        row.addWidget(self.frame_spin)
        self.total_label = QLabel()
        row.addWidget(self.total_label)
        row.addWidget(toolbar_separator())
        row.addWidget(QLabel(self.tr("Frames/s")))
        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(1.0, 240.0)
        self.rate_spin.setValue(60.0)
        self.rate_spin.setDecimals(0)
        self.rate_spin.setProperty("scrub", True)
        self.rate_spin.setMaximumWidth(60)
        row.addWidget(self.rate_spin)
        row.addStretch(1)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._clock = QElapsedTimer()
        state.frame_changed.connect(self._show_frame)
        state.document.changed.connect(self._show_total)
        self._show_total()

    @property
    def playing(self) -> bool:
        return self._timer.isActive()

    def toggle(self) -> None:
        if self.playing:
            self._timer.stop()
        else:
            self._clock.start()
            self._timer.start()
        self.play_button.setIcon(line_icon("pause" if self.playing else "play"))

    def stop(self) -> None:
        if self.playing:
            self.toggle()

    def go_to_end(self) -> None:
        self.state.set_frame(self.state.document.timeline.total_frame)

    def _tick(self) -> None:
        elapsed = self._clock.restart() / 1000.0
        end = self.state.document.timeline.total_frame
        frame = self.state.frame + elapsed * self.rate_spin.value()
        if frame > end:
            if not self.loop_button.isChecked():
                self.state.set_frame(end)
                self.stop()
                return
            frame = frame - end if end > 0 else 0.0
        self.state.set_frame(frame)

    def _show_frame(self, frame: float) -> None:
        with QSignalBlocker(self.frame_spin):
            self.frame_spin.setValue(frame)

    def _show_total(self, *_change) -> None:
        self.total_label.setText(f"/ {self.state.document.timeline.total_frame:g}")

    def hideEvent(self, event) -> None:
        self.stop()
        super().hideEvent(event)
