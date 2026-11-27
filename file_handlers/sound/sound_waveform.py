"""Waveform analysis, channel extraction, and rendering for sound previews."""

from __future__ import annotations

lazy import numpy as np
from PySide6.QtCore import QPoint, Qt, Signal
lazy from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

lazy from ui.styles import current_colors

from .pcm import Pcm


def analyze_wave_activity(audio, width, sensitivity=0.52):
    """Return normalized peaks and merged activity ranges for decoded audio."""

    rate = audio.rate
    full_scale = 32768.0 if audio.samples.dtype == np.int16 else 1.0
    mono = np.abs(audio.samples.astype(np.float32)).mean(axis=1) / full_scale
    frame_count = mono.size

    peak_step = max(1, (frame_count + max(1, width) - 1) // max(1, width))
    peak_starts = np.arange(0, frame_count, peak_step)
    peaks = np.maximum.reduceat(mono, peak_starts)
    peaks = (peaks / max(float(peaks.max()), 1e-9)).tolist()

    window = max(1, round(rate * 0.02))
    starts = np.arange(0, frame_count, window)
    energy = np.sqrt(
        np.add.reduceat(mono * mono, starts)
        / np.minimum(window, frame_count - starts)
    )
    levels = 20 * np.log10(np.maximum(energy, 1e-7))
    peak_level = float(np.quantile(levels, 0.99))
    if peak_level < -90:
        return {"peaks": peaks, "ranges": [], "active_ms": []}
    dynamic_range = 28 + 24 * max(0.0, min(1.0, sensitivity))
    open_level = max(peak_level - dynamic_range, -90.0)
    close_level = max(open_level - 6, -96.0)

    ranges, start, last_active = [], None, None
    hangover = round(0.18 / 0.02)
    for index, level in enumerate(levels):
        if start is None and level >= open_level:
            start = max(0, index - 2)
            last_active = index
        elif start is not None:
            if level >= close_level:
                last_active = index
            elif index - last_active > hangover:
                ranges.append((start * window, min(frame_count, (last_active + 3) * window)))
                start = last_active = None
    if start is not None:
        ranges.append((start * window, min(frame_count, (last_active + 3) * window)))
    ranges = [value for value in ranges if value[1] - value[0] >= rate * 0.12]
    if not ranges and np.any(levels >= open_level):
        center, half = int(np.argmax(mono)), round(rate * 0.35)
        ranges = [(max(0, center - half), min(frame_count, center + half))]
    return {
        "peaks": peaks,
        "ranges": [(start / frame_count, end / frame_count) for start, end in ranges],
        "active_ms": [(round(start * 1000 / rate), round(end * 1000 / rate)) for start, end in ranges],
    }


def wave_preview(audio, channel=-1, normalize=False):
    """Return one zero-based channel (all when negative), optionally peak-normalized,
    and the applied gain factor."""

    samples = audio.samples if channel < 0 else audio.samples[:, [channel]]
    integer = samples.dtype == np.int16
    peak = max(float(samples.max()), -float(samples.min()))
    gain = (32767 if integer else 1.0) / peak if normalize and peak else 1.0
    if gain != 1:
        scaled = samples * gain
        samples = (np.round(scaled).clip(-32768, 32767) if integer else scaled).astype(samples.dtype, copy=False)
    return Pcm(samples, audio.rate), gain


def wave_segment(audio, start_ms, end_ms):
    """Return one time interval of decoded audio."""

    frames = len(audio.samples)
    start = max(0, min(frames, round(int(start_ms) * audio.rate / 1000)))
    end = max(start, min(frames, round(int(end_ms) * audio.rate / 1000)))
    if end <= start:
        raise ValueError(
            "The referenced message interval is outside the decoded audio."
        )
    return Pcm(audio.samples[start:end], audio.rate)


_WAVE_EDITS = {
    "silence": lambda samples, first, last: np.concatenate(
        (samples[:first], np.zeros_like(samples[first:last]), samples[last:])
    ),
    "trim": lambda samples, first, last: np.concatenate((samples[:first], samples[last:])),
    "crop": lambda samples, first, last: samples[first:last],
}


def wave_edit(audio, operation, start, end):
    frames = len(audio.samples)
    first, last = (round(max(0.0, min(1.0, value)) * frames) for value in (start, end))
    if last <= first:
        raise ValueError("Select part of the waveform first.")
    samples = _WAVE_EDITS[operation](audio.samples, first, last)
    if not len(samples):
        raise ValueError("The edit would remove all audio.")
    return Pcm(samples, audio.rate)


class WaveformWidget(QWidget):
    seek_requested = Signal(int)
    selection_changed = Signal()

    def __init__(self, parent=None, *, selectable=False):
        super().__init__(parent)
        self._peaks, self._ranges, self._position = [], [], 0.0
        self._selectable, self._anchor, self._selection = selectable, None, None
        self.setFixedHeight(76)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        legend = self.tr("Blue: waveform · Green: activity · Dark: silence · Yellow: play position")
        if selectable:
            legend += "\n" + self.tr("Click to seek · Drag to select a range")
        self.setToolTip(legend)

    def set_data(self, peaks, ranges):
        self._peaks, self._ranges = peaks, ranges
        self.update()

    def set_position(self, ratio):
        self._position = max(0.0, min(1.0, ratio))
        self.update()

    def selection(self):
        return self._selection

    def clear(self):
        self._peaks, self._ranges, self._position = [], [], 0.0
        self._set_selection(None)
        self.update()

    def _set_selection(self, value):
        if value != self._selection:
            self._selection = value
            self.selection_changed.emit()
            self.update()

    def _ratio(self, event):
        return max(0.0, min(1.0, event.position().x() / max(1, self.width())))

    def mousePressEvent(self, event):
        if self._peaks and event.button() == Qt.LeftButton:
            self._anchor = self._ratio(event)
            self._set_selection(None)
            self.seek_requested.emit(round(self._anchor * 1000))
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._selectable and self._anchor is not None and event.buttons() & Qt.LeftButton:
            ratio = self._ratio(event)
            dragged = abs(ratio - self._anchor) * self.width() >= 3
            self._set_selection(tuple(sorted((self._anchor, ratio))) if dragged else None)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._anchor = None
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event):
        colors = current_colors()
        painter = QPainter(self)
        bounds = self.rect().adjusted(0, 0, -1, -1)
        painter.fillRect(bounds, QColor(colors["input_bg"]))
        if not self._peaks:
            painter.setPen(QPen(QColor(colors["border_subtle"])))
            painter.drawRect(bounds)
            painter.setPen(QPen(QColor(colors["text_subtle"])))
            painter.drawText(bounds, Qt.AlignCenter, self.tr("Waveform appears after preview"))
            return
        width, middle = max(1, bounds.width()), bounds.center().y()
        for start, end in self._ranges:
            left, right = round(start * width), round(end * width)
            painter.fillRect(left, 1, max(1, right - left), bounds.height() - 1, QColor(39, 76, 49, 150))
        selection = None
        if self._selection:
            left, right = (round(value * width) for value in self._selection)
            selection = left, max(left + 1, right)
            fill = QColor(colors["accent"])
            fill.setAlpha(56)
            painter.fillRect(left, 1, selection[1] - left, bounds.height() - 1, fill)
        painter.setPen(QPen(QColor(colors["border_subtle"])))
        painter.drawLine(1, middle, bounds.right() - 1, middle)
        painter.setPen(QPen(QColor("#7ab6ff")))
        scale = max(1, len(self._peaks) - 1)
        for index, peak in enumerate(self._peaks):
            x, amplitude = round(index * width / scale), round((bounds.height() / 2 - 2) * peak)
            painter.drawLine(x, middle - amplitude, x, middle + amplitude)
        if selection:
            painter.setPen(QPen(QColor(colors["accent"])))
            for x in selection:
                painter.drawLine(x, 1, x, bounds.bottom() - 1)
        painter.setPen(QPen(QColor(colors["border_subtle"])))
        painter.drawRect(bounds)
        cursor = round(self._position * width)
        playhead = QColor("#f9d66b")
        painter.setPen(QPen(playhead))
        painter.drawLine(cursor, 1, cursor, bounds.bottom() - 1)
        # A cap tells the playhead apart from the selection edges.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(playhead)
        painter.drawPolygon([QPoint(cursor - 4, 1), QPoint(cursor + 4, 1), QPoint(cursor, 5)])
