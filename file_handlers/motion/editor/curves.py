from __future__ import annotations

import itertools

lazy import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QWidget

lazy from file_handlers.clip.editor.lanes import tick_step, ticks, tone

from file_handlers.document_commands import Change

from .. import edits
from .state import EditorState

COMPONENT_COLORS = ("#e06c6c", "#6cc46c", "#6c9ce0", "#b8bbc2")
RADIUS = 4
_drag_ids = itertools.count(1)


class TrackCurves(QWidget):
    def __init__(self, state: EditorState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(120)
        self._frames = (0.0, 1.0)
        self._values = (-1.0, 1.0)
        self._shown: list = []
        self._picked: set[tuple[int, int]] = set()
        self._drag: dict | None = None
        # The grid, curves and keys; frames only move the playhead drawn over it.
        self._layer: QPixmap | None = None
        state.selection_changed.connect(lambda: self._reload(fit=True))
        state.frame_changed.connect(self.update)
        state.document.changed.connect(self._changed)

    def _redraw(self) -> None:
        self._layer = None
        self.update()

    def _reload(self, fit: bool = False) -> None:
        document = self.state.document
        self._shown = [(owner, field, edits.curve(document, owner, field)) for owner, field in self.state.tracks()]
        if fit:
            self._picked.clear()
            self.fit()
        self._redraw()

    def _changed(self, change: Change) -> None:
        if change.structural or any(change.target is owner for owner, _field, _curve in self._shown):
            self._reload()

    def fit(self) -> None:
        end = max(self.state.end_frame, *(float(curve.frames[-1]) for _o, _f, curve in self._shown), 1.0)
        values = [curve.values for _o, _f, curve in self._shown if len(curve.values)]
        low, high = (float(min(v.min() for v in values)), float(max(v.max() for v in values))) if values else (-1, 1)
        pad = max((high - low) * 0.1, 0.05)
        self._frames, self._values = (-end * 0.02, end * 1.02), (low - pad, high + pad)
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
        painter.setPen(QPen(tone("accent"), 1))
        x = self.x_of(self.state.frame)
        painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        painter.end()

    def _draw(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        layer = QPixmap(max(round(self.width() * ratio), 1), max(round(self.height() * ratio), 1))
        layer.setDevicePixelRatio(ratio)
        background, accent, minor, major, subtle = (tone(name) for name in (
            "canvas_bg", "accent", "canvas_grid_minor", "canvas_grid_major", "text_subtle"))
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
        columns = np.arange(0.0, self.width() + 3.0, 3.0)
        for track, (_owner, _field, curve) in enumerate(self._shown):
            samples = self.y_of(curve.sample_many(self.frame_of(columns)).astype(np.float64))
            dots = self._dots(curve)
            for component in range(samples.shape[1]):
                color = QColor(COMPONENT_COLORS[component])
                # A cosmetic pen: wider antialiased curves draw some twenty times slower.
                painter.setPen(QPen(color, 0))
                painter.setBrush(Qt.NoBrush)
                painter.drawPolyline(QPolygonF(list(map(QPointF, columns.tolist(), samples[:, component].tolist()))))
                painter.setPen(QPen(background, 1))
                for key in dots.tolist():
                    painter.setBrush(accent if (track, key) in self._picked else color)
                    painter.drawEllipse(QPointF(self.x_of(float(curve.frames[key])),
                                                self.y_of(float(curve.values[key, component]))), RADIUS, RADIUS)
        painter.end()
        return layer

    def _hit(self, point: QPointF) -> tuple[int, int, int] | None:
        for track, (_owner, _field, curve) in enumerate(self._shown):
            dots = self._dots(curve)
            near = (np.abs(self.x_of(curve.frames[dots]) - point.x()) <= RADIUS + 2)[:, None] & \
                (np.abs(self.y_of(curve.values[dots]) - point.y()) <= RADIUS + 2)
            if near.any():
                key, component = np.argwhere(near)[0]
                return track, int(dots[key]), int(component)
        return None

    def mousePressEvent(self, event) -> None:
        point = event.position()
        if event.button() == Qt.MiddleButton:
            self._drag = {"pan": (point, self._frames, self._values)}
            return
        if event.button() != Qt.LeftButton:
            return
        hit = self._hit(point)
        if hit is None:
            self._picked.clear()
            self.state.set_frame(max(0.0, round(self.frame_of(point.x()))))
            self._redraw()
            return
        track, key, component = hit
        if event.modifiers() & Qt.ShiftModifier:
            self._picked ^= {(track, key)}
        elif (track, key) not in self._picked:
            self._picked = {(track, key)}
        if (track, key) in self._picked:
            owner, field, curve = self._shown[track]
            keys = sorted(index for picked, index in self._picked if picked == track)
            self._drag = {"track": track, "component": component, "keys": keys, "start": point, "owner": owner,
                          "field": field, "frames": curve.frames.astype(np.int64), "values": curve.values.copy(),
                          "merge": next(_drag_ids)}
        self._redraw()

    def mouseMoveEvent(self, event) -> None:
        drag, point = self._drag, event.position()
        if drag is None:
            return
        if "pan" in drag:
            start, frames, values = drag["pan"]
            shift = (self.frame_of(start.x()) - self.frame_of(point.x()), self.value_of(start.y()) - self.value_of(
                point.y()))
            self._frames = (frames[0] + shift[0], frames[1] + shift[0])
            self._values = (values[0] + shift[1], values[1] + shift[1])
            self._redraw()
            return
        if not drag.get("moving"):
            if (point - drag["start"]).manhattanLength() < QApplication.startDragDistance():
                return
            drag["moving"] = True
        moved = round(self.frame_of(point.x()) - self.frame_of(drag["start"].x()))
        change = self.value_of(point.y()) - self.value_of(drag["start"].y())
        frames, values = drag["frames"].copy(), drag["values"].copy()
        keys = np.array(drag["keys"], np.int64)
        frames[keys] = np.maximum(frames[keys] + moved, 0)
        values[keys, drag["component"]] += change
        kept = np.ones(len(frames), bool)
        kept[[index for index in range(len(frames)) if index not in drag["keys"] and frames[index] in frames[keys]]] = False
        edits.set_keys(self.state.document, drag["owner"], drag["field"], frames[kept], values[kept],
                       self.tr("Move Keys"), drag["merge"])
        moved_frames = set(frames[keys].tolist())
        curve = self._shown[drag["track"]][2]
        self._picked = {(drag["track"], index) for index, frame in enumerate(curve.frames.tolist())
                        if frame in moved_frames}

    def mouseReleaseEvent(self, event) -> None:
        self._drag = None

    def mouseDoubleClickEvent(self, event) -> None:
        point = event.position()
        if not self._shown or event.button() != Qt.LeftButton:
            return
        frame = max(0, round(self.frame_of(point.x())))
        distances = [min(abs(self.y_of(float(value)) - point.y()) for value in curve.sample(frame))
                     for _owner, _field, curve in self._shown]
        owner, field, _curve = self._shown[int(np.argmin(distances))]
        edits.add_key(self.state.document, owner, field, frame)

    def wheelEvent(self, event) -> None:
        factor = 0.85 if event.angleDelta().y() > 0 else 1 / 0.85
        point = event.position()
        if event.modifiers() & Qt.ControlModifier:
            anchor, (low, high) = self.value_of(point.y()), self._values
            self._values = (anchor + (low - anchor) * factor, anchor + (high - anchor) * factor)
        else:
            anchor, (low, high) = self.frame_of(point.x()), self._frames
            self._frames = (anchor + (low - anchor) * factor, anchor + (high - anchor) * factor)
        self._redraw()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_F:
            self.fit()
        elif event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self._picked:
            picked = {track: [key for chosen, key in self._picked if chosen == track] for track, _key in self._picked}
            deletions = [(track, keys) for track, keys in sorted(picked.items())
                         if len(keys) < len(self._shown[track][2].frames)]
            if deletions:
                with self.state.document.macro(self.tr("Delete Keys")):
                    for track, keys in deletions:
                        owner, field, _curve = self._shown[track]
                        edits.delete_keys(self.state.document, owner, field, keys)
            self._picked.clear()
            self._redraw()
        else:
            super().keyPressEvent(event)
