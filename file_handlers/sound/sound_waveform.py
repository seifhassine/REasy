"""Waveform analysis, channel extraction, and rendering for sound previews."""

from __future__ import annotations

import wave

lazy import numpy as np
from PySide6.QtCore import QCoreApplication, QPoint, Qt, Signal
lazy from PySide6.QtGui import QColor, QPainter, QPen
lazy from PySide6.QtMultimedia import QMediaDevices
from PySide6.QtWidgets import QSizePolicy, QWidget

lazy from ui.styles import current_colors


def playback_unavailable(player) -> str:
    if not player.isAvailable():
        return QCoreApplication.translate(
            "SoundPlayback",
            "Windows Media Foundation is missing, so audio cannot play. "
            "Windows N editions need the Media Feature Pack.",
        )
    if not QMediaDevices.audioOutputs():
        return QCoreApplication.translate("SoundPlayback", "Windows has no audio output device to play to.")
    return ""

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


def write_wave_preview(source_path, output_path, channel=-1, normalize=False):
    """Write one zero-based channel (all when negative) of a PCM16 WAV, optionally
    peak-normalized; return the applied gain factor."""

    with wave.open(str(source_path), "rb") as source:
        params = source.getparams()
        if params.sampwidth != 2 or channel >= params.nchannels:
            raise ValueError("Decoded WAV has an unsupported channel layout.")
        samples = np.frombuffer(source.readframes(params.nframes), dtype="<i2")
    samples = samples.reshape(-1, params.nchannels)
    if channel >= 0:
        samples = samples[:, channel:channel + 1]
    peak = int(np.abs(samples.astype(np.int32)).max(initial=0))
    gain = 32767 / peak if normalize and peak else 1.0
    if gain != 1.0:
        samples = np.round(samples * gain).clip(-32768, 32767).astype("<i2")
    with wave.open(str(output_path), "wb") as output:
        output.setparams(params._replace(nchannels=samples.shape[1]))
        output.writeframes(np.ascontiguousarray(samples).tobytes())
    return gain


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


_WAVE_EDITS = {
    "silence": lambda data, first, last: data[:first] + bytes(last - first) + data[last:],
    "trim": lambda data, first, last: data[:first] + data[last:],
    "crop": lambda data, first, last: data[first:last],
}


def write_wave_edit(source_path, output_path, operation, start, end):
    
    with wave.open(str(source_path), "rb") as source:
        params = source.getparams()
        payload = source.readframes(params.nframes)
    if params.sampwidth < 2:
        raise ValueError("Only 16-bit or wider PCM can be edited.")
    frame = params.nchannels * params.sampwidth
    first, last = (
        round(max(0.0, min(1.0, value)) * params.nframes) * frame for value in (start, end)
    )
    if last <= first:
        raise ValueError("Select part of the waveform first.")
    payload = _WAVE_EDITS[operation](payload, first, last)
    if not payload:
        raise ValueError("The edit would remove all audio.")
    with wave.open(str(output_path), "wb") as output:
        output.setparams(params)
        output.writeframes(payload)


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
