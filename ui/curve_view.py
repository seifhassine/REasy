"""See the Views sections of docs/internals/motion-editor.md and clip-editor.md, and docs/internals/jcns-editor.md."""

from __future__ import annotations

import itertools
import math

lazy import numpy as np
from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QWidget

lazy from file_handlers.clip.editor.lanes import tick_step, ticks, tone

COMPONENT_COLORS = ("#e06c6c", "#6cc46c", "#6c9ce0", "#b8bbc2")
CURVE_COLORS = ("#e0a050", "#5c9cf0", "#6fbf5f", "#e0625c", "#b48ee0", "#4fb0c6")
RADIUS = 4
_drag_ids = itertools.count(1)


class CurveSource(QObject):
    changed = Signal(bool)
    moved = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.picks: set[tuple[int, int]] = set()

    def reload(self, fit: bool = False) -> None:
        """The curves changed; ``fit``: they are others, so the picks go and the view fits them."""
        if fit:
            self.picks = set()
        self.changed.emit(fit)

    def picked(self) -> set[tuple[int, int]]:
        return self.picks

    def pick(self, picks: set[tuple[int, int]]) -> None:
        self.picks = picks

    def x_range(self, width: int) -> tuple[float, float] | None:
        """The x range a view ``width`` pixels wide shows when the source sets it; None: the view's own."""
        return None

    def set_x_range(self, low: float, high: float, width: int) -> bool:
        """Takes the view's zoomed or panned x range; False leaves it to the view."""
        return False

    def marker(self) -> float | None:
        return None

    def points(self) -> list[tuple[float, float, str]]:
        """Live (x, y, colour) points over the curves."""
        return []

    def set_marker(self, x: float) -> None:
        pass


class CurveView(QWidget):
    def __init__(self, source: CurveSource, parent: QWidget | None = None):
        super().__init__(parent)
        self.source = source
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(120)
        self._x = (0.0, 1.0)
        self._values = (-1.0, 1.0)
        self._shown: list = []
        self._drag: dict | None = None
        # The grid, curves and keys; the marker and points are drawn over it.
        self._layer: QPixmap | None = None
        source.changed.connect(self._reload)
        source.moved.connect(self.update)

    @property
    def _frames(self) -> tuple[float, float]:
        return self.source.x_range(self.width()) or self._x

    def _set_frames(self, low: float, high: float) -> None:
        if 1e-6 < high - low < 1e300 and not self.source.set_x_range(low, high, self.width()):
            self._x = (low, high)
            self._redraw()

    def _redraw(self) -> None:
        self._layer = None
        self.update()

    def _reload(self, fit: bool = False) -> None:
        self._shown = self.source.curves()
        picks = self.source.picked()
        if (kept := {(track, key) for track, key in picks
                     if track < len(self._shown) and key < len(self._shown[track].frames)}) != picks:
            self.source.pick(kept)
        if fit:
            self.fit()
        self._redraw()

    def fit(self) -> None:
        if self.source.x_range(self.width()) is None:
            low, high = self.source.extent()
            for curve in self._shown:
                if len(curve.frames):
                    low, high = min(low, float(curve.frames.min())), max(high, float(curve.frames.max()))
            span = max(high - low, 1e-6)
            self._x = (low - span * 0.02, high + span * 0.02)
        values = np.concatenate([np.ravel(curve.values) for curve in self._shown] or [np.zeros(0)])
        values = np.clip(values[np.isfinite(values)], -1e300, 1e300)
        bottom, top = (float(values.min()), float(values.max())) if len(values) else (-1.0, 1.0)
        pad = (top - bottom) * 0.1 or max(abs(top) * 0.1, 1.0)
        self._values = (bottom - pad, top + pad)
        self._redraw()

    def x_of(self, frame):
        low, high = self._frames
        return (frame - low) / (high - low) * self.width()

    def frame_of(self, x):
        low, high = self._frames
        return low + x / max(self.width(), 1) * (high - low)

    def y_of(self, value):
        low, high = self._values
        return self.height() - (value - low) / (high - low) * self.height()

    def value_of(self, y):
        low, high = self._values
        return low + (self.height() - y) / max(self.height(), 1) * (high - low)

    def _dots(self, curve) -> np.ndarray:
        low, high = self._frames
        shown = np.flatnonzero((curve.frames >= low) & (curve.frames <= high))
        return shown if len(shown) * (2 * RADIUS + 2) <= self.width() else shown[:0]

    def resizeEvent(self, event) -> None:
        self._layer = None
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        if self._layer is None:
            self._layer = self._draw()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._layer)
        if (marker := self.source.marker()) is not None:
            painter.setPen(QPen(tone("accent"), 1))
            x = self.x_of(marker)
            painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        for x, y, colour in self.source.points():
            painter.setPen(QPen(tone("canvas_bg"), 1))
            painter.setBrush(QColor(colour))
            painter.drawEllipse(QPointF(self.x_of(x), self.y_of(y)), RADIUS + 1, RADIUS + 1)
        painter.end()

    def _draw(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        layer = QPixmap(max(round(self.width() * ratio), 1), max(round(self.height() * ratio), 1))
        layer.setDevicePixelRatio(ratio)
        background, accent, minor, major, subtle, muted, bright = (tone(name) for name in (
            "canvas_bg", "accent", "canvas_grid_minor", "canvas_grid_major", "text_subtle", "text_muted",
            "text_bright"))
        layer.fill(background)
        painter = QPainter(layer)
        painter.setRenderHint(QPainter.Antialiasing)
        low, high = self._frames
        for frame in ticks(low, high, tick_step(self.width() / (high - low))):
            painter.setPen(QPen(minor, 1))
            painter.drawLine(QPointF(self.x_of(frame), 0), QPointF(self.x_of(frame), self.height()))
            painter.setPen(subtle)
            painter.drawText(QPointF(self.x_of(frame) + 2, self.height() - 4), f"{frame:g}")
        low, high = self._values
        for value in ticks(low, high, tick_step(self.height() / (high - low), 40)):
            painter.setPen(QPen(major if value == 0 else minor, 1))
            painter.drawLine(QPointF(0, self.y_of(value)), QPointF(self.width(), self.y_of(value)))
            painter.setPen(subtle)
            painter.drawText(QPointF(4, self.y_of(value) - 2), f"{value:g}")
        columns = np.arange(0.0, self.width() + 3.0, 3.0)
        picks = self.source.picked()
        for track, curve in enumerate(self._shown):
            samples = self.y_of(curve.sample_many(self.frame_of(columns)).astype(np.float64))
            dots = self._dots(curve)
            for component in range(samples.shape[1]):
                colors = getattr(curve, "colors", COMPONENT_COLORS)
                color = QColor(colors[component % len(colors)])
                # A cosmetic pen: wider antialiased curves draw some twenty times slower.
                painter.setPen(QPen(color, 0))
                painter.setBrush(Qt.NoBrush)
                line, finite = samples[:, component], np.isfinite(samples[:, component])
                for run in np.split(np.arange(len(line)), np.flatnonzero(np.diff(finite)) + 1):
                    if finite[run[0]]:
                        painter.drawPolyline(QPolygonF(list(map(QPointF, columns[run].tolist(), line[run].tolist()))))
                for key in dots.tolist():
                    at = QPointF(self.x_of(float(curve.frames[key])), self.y_of(float(curve.values[key, component])))
                    if not math.isfinite(at.y()):
                        continue
                    if (track, key) in picks and hasattr(curve, "handles"):
                        for anchor_x, anchor_y, x, y in curve.handles(key):
                            end = QPointF(self.x_of(x), self.y_of(y))
                            painter.setPen(QPen(muted, 0))
                            painter.drawLine(QPointF(self.x_of(anchor_x), self.y_of(anchor_y)), end)
                            painter.setBrush(bright)
                            painter.drawEllipse(end, RADIUS - 1, RADIUS - 1)
                    painter.setPen(QPen(background, 1))
                    painter.setBrush(accent if (track, key) in picks else color)
                    painter.drawEllipse(at, RADIUS, RADIUS)
        painter.end()
        return layer

    def _hit(self, point: QPointF) -> tuple[int, int, int] | None:
        for track, curve in enumerate(self._shown):
            dots = self._dots(curve)
            near = (np.abs(self.x_of(curve.frames[dots]) - point.x()) <= RADIUS + 2)[:, None] & \
                (np.abs(self.y_of(curve.values[dots]) - point.y()) <= RADIUS + 2)
            if near.any():
                key, component = np.argwhere(near)[0]
                return track, int(dots[key]), int(component)
        return None

    def _handle(self, point: QPointF) -> tuple[int, int, int] | None:
        for track, key in self.source.picked():
            curve = self._shown[track]
            if not hasattr(curve, "handles") or key not in self._dots(curve):
                continue
            for handle, (_x, _y, x, y) in enumerate(curve.handles(key)):
                if abs(self.x_of(x) - point.x()) + abs(self.y_of(y) - point.y()) <= RADIUS + 2:
                    return track, key, handle
        return None

    def mousePressEvent(self, event) -> None:
        point = event.position()
        if event.button() == Qt.MiddleButton:
            self._drag = {"pan": (point, self._frames, self._values)}
            return
        if event.button() != Qt.LeftButton:
            return
        if (handle := self._handle(point)) is not None:
            self._drag = {"handle": handle, "start": point, "merge": next(_drag_ids)}
            return
        hit, picks = self._hit(point), self.source.picked()
        if hit is None:
            self.source.pick(set())
            self.source.set_marker(self.frame_of(point.x()))
            self._redraw()
            return
        track, key, component = hit
        if event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier):
            self.source.pick(picks ^ {(track, key)})
        elif (track, key) not in picks:
            self.source.pick({(track, key)})
        if (track, key) in (picks := self.source.picked()):
            curve = self._shown[track]
            self._drag = {"track": track, "component": component, "start": point, "merge": next(_drag_ids),
                          "keys": sorted(index for picked, index in picks if picked == track),
                          "frames": curve.frames.copy(), "values": curve.values.copy()}
        self._redraw()

    def mouseMoveEvent(self, event) -> None:
        drag, point = self._drag, event.position()
        if drag is None:
            return
        if "pan" in drag:
            start, frames, values = drag["pan"]
            dx = self.frame_of(start.x()) - self.frame_of(point.x())
            dy = self.value_of(start.y()) - self.value_of(point.y())
            self._values = (values[0] + dy, values[1] + dy)
            self._set_frames(frames[0] + dx, frames[1] + dx)
            self._redraw()
            return
        if not drag.get("moving"):
            if (point - drag["start"]).manhattanLength() < QApplication.startDragDistance():
                return
            drag["moving"] = True
        if "handle" in drag:
            self.source.move_handle(*drag["handle"], self.frame_of(point.x()), self.value_of(point.y()), drag["merge"])
            return
        keys = self.source.move_keys(drag["track"], drag["keys"], drag["frames"], drag["values"],
                                     self.frame_of(point.x()) - self.frame_of(drag["start"].x()),
                                     self.value_of(point.y()) - self.value_of(drag["start"].y()), drag["component"],
                                     drag["merge"])
        if keys is not None:
            self.source.pick({(drag["track"], key) for key in keys})

    def mouseReleaseEvent(self, event) -> None:
        self._drag = None

    def mouseDoubleClickEvent(self, event) -> None:
        point = event.position()
        if not self._shown or event.button() != Qt.LeftButton:
            return
        frame = self.frame_of(point.x())
        distances = [np.fmin.reduce(np.abs(self.y_of(curve.sample_many([frame])[0]) - point.y()), initial=np.inf)
                     for curve in self._shown]
        self.source.add_key(int(np.argmin(distances)), frame)

    def wheelEvent(self, event) -> None:
        factor, point = 1.15 ** (-event.angleDelta().y() / 120), event.position()
        if bool(event.modifiers() & Qt.ControlModifier) == (self.source.x_range(self.width()) is not None):
            anchor, (low, high) = self.frame_of(point.x()), self._frames
            self._set_frames(anchor + (low - anchor) * factor, anchor + (high - anchor) * factor)
        else:
            anchor, (low, high) = self.value_of(point.y()), self._values
            low, high = anchor + (low - anchor) * factor, anchor + (high - anchor) * factor
            if 1e-6 < high - low < 1e300:
                self._values = (low, high)
                self._redraw()
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_F:
            self.fit()
        elif event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and (picks := self.source.picked()):
            picked = {track: sorted(key for chosen, key in picks if chosen == track) for track, _ in picks}
            self.source.delete_keys({track: keys for track, keys in picked.items()
                                     if len(keys) < len(self._shown[track].frames)})
            self.source.pick(set())
            self._redraw()
        else:
            super().keyPressEvent(event)
