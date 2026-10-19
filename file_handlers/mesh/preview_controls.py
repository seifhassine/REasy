from math import acos, cos, tau

from PySide6.QtCore import QEvent, QElapsedTimer, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QHBoxLayout, QLabel, QPushButton, QSlider, QSpinBox, QToolButton, QWidget,
)


def make_searchable(combo, placeholder):
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    combo.setMinimumContentsLength(16)
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.lineEdit().setPlaceholderText(placeholder)
    combo.lineEdit().setClearButtonEnabled(True)
    combo.completer().setCaseSensitivity(Qt.CaseInsensitive)
    combo.completer().setFilterMode(Qt.MatchContains)
    combo.completer().setCompletionMode(QCompleter.PopupCompletion)

    def finish():
        if combo.currentIndex() >= 0 and combo.currentText() == combo.itemText(combo.currentIndex()):
            return
        index = combo.findText(combo.currentText(), Qt.MatchFixedString)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.currentIndex() >= 0:
            combo.setEditText(combo.itemText(combo.currentIndex()))

    combo.lineEdit().editingFinished.connect(finish)
    combo.currentIndexChanged.connect(lambda: combo.lineEdit().setCursorPosition(0))


def find_combo_data(combo, value, default=-1):
    # Qt's findData cannot compare Python tuples stored in QVariants.
    return next((i for i in range(combo.count()) if combo.itemData(i) == value), default)


class ComboStepper(QWidget):

    def __init__(self, combo, label, parent=None, *, on_step=None):
        super().__init__(parent)
        self.combo = combo
        self.setFocusProxy(combo)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.previous = QToolButton(self)
        self.next = QToolButton(self)
        for button, direction, text, arrow in (
            (self.previous, -1, self.tr("Previous {item}"), Qt.LeftArrow),
            (self.next, 1, self.tr("Next {item}"), Qt.RightArrow),
        ):
            button.setArrowType(arrow)
            button.setAccessibleName(text.format(item=label))
            button.setToolTip(text.format(item=label))
            button.clicked.connect(lambda _checked=False, step=direction: (on_step or self.step)(step))
        row.addWidget(self.previous)
        row.addWidget(combo, 1)
        row.addWidget(self.next)
        combo.currentIndexChanged.connect(self.refresh_enabled)
        for signal in (combo.model().rowsInserted, combo.model().rowsRemoved, combo.model().modelReset, combo.model().dataChanged):
            signal.connect(self.refresh_enabled)
        combo.installEventFilter(self)
        self.refresh_enabled()

    def target(self, direction):
        index = self.combo.currentIndex()
        for candidate in range(index + direction, self.combo.count() if direction > 0 else -1, direction):
            if self.combo.model().flags(self.combo.model().index(candidate, 0)) & Qt.ItemIsEnabled:
                return candidate
        return index

    def can_step(self, direction):
        return self.combo.isEnabled() and self.target(direction) != self.combo.currentIndex()

    def step(self, direction):
        target = self.target(direction)
        if not self.combo.isEnabled() or target == self.combo.currentIndex():
            return False
        self.combo.setCurrentIndex(target)
        return True

    def refresh_enabled(self, *_args):
        for button, direction in ((self.previous, -1), (self.next, 1)):
            button.setEnabled(self.can_step(direction))

    def eventFilter(self, watched, event):
        if event.type() == QEvent.EnabledChange:
            self.refresh_enabled()
        return super().eventFilter(watched, event)


class MorphControls(QWidget):
    changed = Signal()

    def __init__(self, parent=None, *, frame_interval=None):
        super().__init__(parent)
        self._frame_interval = frame_interval or (lambda: 16)
        self.weight = 1.0
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(self.tr("Morph"), self)
        self.combo = QComboBox(self)
        make_searchable(self.combo, self.tr("Search morphs…"))
        self.combo.setAccessibleName(self.tr("Preview blendshape"))
        self.combo.setToolTip(self.tr("Type part of a morph name to find it."))
        self.steps = ComboStepper(self.combo, self.tr("morph"), self, on_step=self.step)
        self.steps.setMaximumWidth(420)
        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setRange(0, 100)
        self.slider.setMinimumWidth(100)
        self.slider.setMaximumWidth(220)
        self.slider.setValue(100)
        self.slider.setAccessibleName(self.tr("Morph strength"))
        self.slider.setToolTip(self.tr("Blend from the base pose (0%) to the full morph (100%)."))
        self.percent = QSpinBox(self)
        self.percent.setRange(0, 100)
        self.percent.setSuffix("%")
        self.percent.setValue(100)
        self.percent.setKeyboardTracking(False)
        self.percent.setAccessibleName(self.tr("Morph strength percentage"))
        self.percent.setFixedWidth(self.percent.sizeHint().width())
        self.play_button = QPushButton(self.tr("Play"), self)
        self.play_button.setCheckable(True)
        self.play_button.setToolTip(self.tr("Loop smoothly between base pose and the selected morph."))
        self.reset_button = QPushButton(self.tr("Base pose"), self)
        self.reset_button.setToolTip(self.tr("Stop playback and return to the undeformed mesh."))
        for widget, stretch in ((self.label, 0), (self.steps, 3), (self.slider, 3),
                                (self.percent, 0), (self.play_button, 0), (self.reset_button, 0)):
            row.addWidget(widget, stretch)
        row.addStretch()
        for button in (self.play_button, self.reset_button):
            button.setProperty("compact", True)
            button.setAutoDefault(False)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._elapsed = QElapsedTimer()
        self._phase = 0.0
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(16)
        self._pending.timeout.connect(self.changed.emit)
        self.slider.valueChanged.connect(self._weight_changed)
        self.slider.actionTriggered.connect(self.pause)
        self.slider.sliderPressed.connect(self.pause)
        self.slider.sliderReleased.connect(self._flush)
        self.percent.valueChanged.connect(self._typed_weight)
        self.combo.currentIndexChanged.connect(self._channel_changed)
        self.play_button.toggled.connect(self._play)
        self.reset_button.clicked.connect(self.reset)
        self.refresh_enabled()

    def refresh_enabled(self):
        self.combo.setEnabled(self.combo.count() > 1)
        active = self.combo.isEnabled() and self.combo.currentData() is not None
        for widget in (self.slider, self.percent, self.play_button, self.reset_button):
            widget.setEnabled(active)
        if not active:
            self.pause()
        self.steps.refresh_enabled()

    def step(self, direction):
        playing = self.play_button.isChecked()
        if self.steps.step(direction) and playing and self.combo.currentData() is not None:
            self.play_button.setChecked(True)

    def _channel_changed(self):
        self.combo.setToolTip(self.combo.currentText() + "\n" + self.tr("Type part of a morph name to find it."))
        self.pause()
        self.refresh_enabled()
        self._flush()

    def _weight_changed(self, value):
        self.weight = value / 100.0
        with QSignalBlocker(self.percent):
            self.percent.setValue(value)
        if self.slider.isSliderDown():
            if not self._pending.isActive():
                self._pending.start()
        else:
            self._flush()

    def _typed_weight(self, value):
        self.pause()
        self.slider.setValue(value)

    def _flush(self):
        self._pending.stop()
        self.changed.emit()

    def _play(self, checked):
        self._timer.stop()
        self.play_button.setText(self.tr("Pause") if checked else self.tr("Play"))
        if checked:
            self._sync_interval()
            self._phase = acos(1.0 - 2.0 * self.weight) / tau
            self._elapsed.start()
            self._timer.start()

    def _sync_interval(self):
        interval = max(16, self._frame_interval())
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)

    def _tick(self):
        self._sync_interval()
        phase = self._phase + self._elapsed.elapsed() / 4000.0
        self.weight = 0.5 * (1.0 - cos(tau * phase))
        with QSignalBlocker(self.slider), QSignalBlocker(self.percent):
            self.slider.setValue(round(self.weight * 100))
            self.percent.setValue(self.slider.value())
        self._flush()

    def pause(self, *_args):
        self.play_button.setChecked(False)

    def reset(self):
        self.pause()
        self.weight = 1.0
        with QSignalBlocker(self.combo):
            self.combo.setCurrentIndex(0)
        with QSignalBlocker(self.slider):
            self.slider.setValue(100)
        self.percent.setValue(100)
        self.refresh_enabled()
        self._flush()

    def stop(self):
        self.pause()
        self._pending.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._flush()

    def hideEvent(self, event):
        self.stop()
        super().hideEvent(event)
