"""Waveform analysis, channel extraction, and rendering for sound previews."""

from __future__ import annotations

import wave

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

def analyze_wave_activity(wav_path, width, sensitivity=0.52):
    """Return normalized peaks and merged activity ranges for PCM16 WAV audio."""

    try:
        with wave.open(str(wav_path), "rb") as source:
            channels, rate, frames = (
                source.getnchannels(),
                source.getframerate(),
                source.getnframes(),
            )
            if source.getsampwidth() != 2 or min(channels, rate, frames) <= 0:
                return None
            pcm = np.frombuffer(source.readframes(frames), dtype=np.int16)
    except (OSError, wave.Error):
        return None

    pcm = pcm[: pcm.size // channels * channels]
    if not pcm.size:
        return None
    mono = np.abs(pcm.reshape(-1, channels).astype(np.float32)).mean(axis=1) / 32768.0
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


def write_wave_channel(source_path, output_path, channel_index):
    """Write one zero-based channel from a decoded PCM16 WAV as mono."""

    with wave.open(str(source_path), "rb") as source:
        channels, sample_width, rate, frames = (
            source.getnchannels(), source.getsampwidth(),
            source.getframerate(), source.getnframes(),
        )
        if sample_width != 2 or not 0 <= channel_index < channels:
            raise ValueError("Decoded WAV has an unsupported channel layout.")
        samples = np.frombuffer(source.readframes(frames), dtype="<i2")
    with wave.open(str(output_path), "wb") as output:
        output.setparams((1, 2, rate, frames, "NONE", ""))
        output.writeframes(samples[channel_index::channels].tobytes())


def write_wave_segment(source_path, output_path, start_ms, end_ms):
    """Copy one time interval from a decoded PCM WAV without reencoding it."""

    with wave.open(str(source_path), "rb") as source:
        channels, sample_width, rate, frames = (
            source.getnchannels(),
            source.getsampwidth(),
            source.getframerate(),
            source.getnframes(),
        )
        start = max(0, min(frames, round(int(start_ms) * rate / 1000)))
        end = max(start, min(frames, round(int(end_ms) * rate / 1000)))
        if end <= start:
            raise ValueError(
                "The referenced message interval is outside the decoded audio."
            )
        source.setpos(start)
        payload = source.readframes(end - start)
        compression = source.getcomptype(), source.getcompname()
    with wave.open(str(output_path), "wb") as output:
        output.setparams(
            (
                channels,
                sample_width,
                rate,
                end - start,
                compression[0],
                compression[1],
            )
        )
        output.writeframes(payload)


class WaveformWidget(QWidget):
    seek_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._peaks, self._ranges, self._position = [], [], 0.0
        self.setFixedHeight(60)
        self.setToolTip(
            self.tr("Blue: waveform · Green: activity · Dark: silence · Yellow: play position")
        )

    def set_data(self, peaks, ranges):
        self._peaks, self._ranges = peaks, ranges
        self.update()

    def set_position(self, ratio):
        self._position = max(0.0, min(1.0, ratio))
        self.update()

    def clear(self):
        self._peaks, self._ranges, self._position = [], [], 0.0
        self.update()

    def mousePressEvent(self, event):
        if self._peaks and event.button() == Qt.LeftButton:
            ratio = event.position().x() / max(1, self.width())
            self.seek_requested.emit(round(max(0.0, min(1.0, ratio)) * 1000))
            return
        super().mousePressEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        bounds = self.rect().adjusted(0, 0, -1, -1)
        painter.fillRect(bounds, QColor("#1e1e1e"))
        painter.setPen(QPen(QColor("#3d3d3d")))
        painter.drawRect(bounds)
        if not self._peaks:
            painter.setPen(QPen(QColor("#8a8a8a")))
            painter.drawText(bounds, Qt.AlignCenter, self.tr("Waveform appears after preview"))
            return
        width, middle = max(1, bounds.width()), bounds.center().y()
        for start, end in self._ranges:
            left, right = round(start * width), round(end * width)
            painter.fillRect(left, 1, max(1, right - left), bounds.height() - 1, QColor(39, 76, 49, 150))
        painter.setPen(QPen(QColor("#7ab6ff")))
        scale = max(1, len(self._peaks) - 1)
        for index, peak in enumerate(self._peaks):
            x, amplitude = round(index * width / scale), round((bounds.height() / 2 - 2) * peak)
            painter.drawLine(x, middle - amplitude, x, middle + amplitude)
        cursor = round(self._position * width)
        painter.setPen(QPen(QColor("#f9d66b")))
        painter.drawLine(cursor, 1, cursor, bounds.bottom() - 1)
