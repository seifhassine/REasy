from __future__ import annotations

import itertools
import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

from .. import edits
from ..document import Change
from ..evaluation import NUMERIC_TYPES, sample
from ..model import Clip, Key, Node, Property, SpeedPoint, Track, walk_nodes, walk_properties
from ..rules import InterpolationType, property_type
from ..values import coerce
from .lanes import MARGIN, tick_step, ticks, tone
from .state import EditorState

CURVE_COLORS = ("#e0a050", "#5c9cf0", "#6fbf5f", "#e0625c", "#b48ee0", "#4fb0c6")
LIMIT = 12
_drag_ids = itertools.count(1)


class CurveView(QWidget):
    def __init__(self, state: EditorState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._range = (0.0, 1.0)
        self._shown: list[Property] = []
        self._drag: dict | None = None
        state.frame_changed.connect(self.update)
        state.view_changed.connect(self.update)
        state.selection_changed.connect(self._selection_changed)
        state.document.changed.connect(self._document_changed)

    def properties(self) -> list[Property]:
        return self._shown

    def _selection_changed(self) -> None:
        selection, document = self.state.selection, self.state.document
        kept = [prop for prop in self._shown
                if prop.type in NUMERIC_TYPES and prop.keys and document.parent(prop) is not None]
        if selection and all(isinstance(item, (Key, SpeedPoint)) and document.owner(item, Property) in kept
                             for item in selection):
            found = kept
        else:
            found = self._collect(selection)
        if [id(prop) for prop in found] != [id(prop) for prop in self._shown]:
            self._shown = found
            self.fit_values()
        self.update()

    def _document_changed(self, change: Change) -> None:
        if change.structural or change.field == "type":
            self._selection_changed()
        else:
            self.update()

    def _collect(self, selection: list) -> list[Property]:
        found: list[Property] = []
        for item in selection:
            if isinstance(item, (Key, SpeedPoint)):
                item = self.state.document.owner(item, Property)
            if isinstance(item, (Track, Clip)):
                roots = item.roots if isinstance(item, Track) else item.nodes
                props = [prop for node in walk_nodes(roots) for prop in node.properties]
            elif isinstance(item, Node):
                props = [prop for node in walk_nodes([item]) for prop in node.properties]
            else:
                props = [item] if isinstance(item, Property) else []
            for prop in walk_properties(props):
                if prop.type in NUMERIC_TYPES and prop.keys and prop not in found:
                    found.append(prop)
                if len(found) >= LIMIT:
                    return found
        return found

    def fit_values(self) -> None:
        values = [min(max(value, -1e300), 1e300) for prop in self._shown for key in prop.keys
                  if math.isfinite(value := float(key.value))]
        low, high = (min(values), max(values)) if values else (0.0, 1.0)
        pad = (high - low) * 0.1 or max(abs(high) * 0.1, 1.0)
        self._range = (low - pad, high + pad)
        self.update()

    def x_of(self, frame: float) -> float:
        return (frame - self.state.view_start) * self.state.view_scale + MARGIN

    def frame_of(self, x: float) -> float:
        return (x - MARGIN) / self.state.view_scale + self.state.view_start

    def y_of(self, value: float) -> float:
        low, high = self._range
        return self.height() - 8 - (value - low) / (high - low) * (self.height() - 16)

    def value_of(self, y: float) -> float:
        low, high = self._range
        return low + (self.height() - 8 - y) / max(self.height() - 16, 1) * (high - low)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), tone("canvas_bg"))
        painter.setPen(QPen(tone("canvas_grid_minor"), 1))
        for frame in ticks(self.frame_of(0), self.frame_of(self.width()), tick_step(self.state.view_scale)):
            painter.drawLine(QPointF(self.x_of(frame), 0), QPointF(self.x_of(frame), self.height()))
        low, high = self._range
        for value in ticks(low, high, tick_step(max(self.height() - 16, 1) / (high - low), 40)):
            y = self.y_of(value)
            painter.setPen(QPen(tone("canvas_grid_major") if value == 0 else tone("canvas_grid_minor"), 1))
            painter.drawLine(QPointF(0, y), QPointF(self.width(), y))
            painter.setPen(tone("text_subtle"))
            painter.drawText(QPointF(4, y - 2), f"{value:g}")
        selected = {id(item) for item in self.state.selection}
        for index, prop in enumerate(self._shown):
            color = QColor(CURVE_COLORS[index % len(CURVE_COLORS)])
            offset = self.state.document.time_offset(prop)
            path, connected = QPainterPath(), False
            for x in range(0, self.width() + 2, 2):
                result = sample(prop, self.frame_of(x) - offset)
                if result is None:
                    continue
                y = self.y_of(float(result))
                if not math.isfinite(y):
                    connected = False
                elif connected:
                    path.lineTo(x, y)
                else:
                    path.moveTo(x, y)
                    connected = True
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            for position, key in enumerate(prop.keys):
                center = QPointF(self.x_of(key.frame + offset), self.y_of(float(key.value)))
                if not math.isfinite(center.y()):
                    continue
                chosen = id(key) in selected
                if chosen:
                    for handle in self._handles(prop, position, offset):
                        painter.setPen(QPen(tone("text_muted"), 1))
                        painter.drawLine(handle[1], handle[2])
                        painter.setBrush(tone("text_bright"))
                        painter.drawEllipse(handle[2], 3, 3)
                painter.setPen(QPen(tone("canvas_bg"), 1))
                painter.setBrush(tone("accent") if chosen else color)
                painter.drawEllipse(center, 4, 4)
        x = self.x_of(self.state.frame)
        painter.setPen(QPen(tone("accent"), 1))
        painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        painter.end()

    def _handles(self, prop: Property, position: int, offset: float) -> list[tuple[int, QPointF, QPointF]]:
        keys = prop.keys
        key = keys[position]
        if key.control is None or position + 1 >= len(keys) or key.interpolation not in (InterpolationType.HERMITE,
                                                                                            InterpolationType.BEZIER):
            return []
        nxt = keys[position + 1]
        if not (math.isfinite(float(key.value)) and math.isfinite(float(nxt.value))):
            return []
        out_x, out_y, in_x, in_y = key.control[:4]
        start = QPointF(self.x_of(key.frame + offset), self.y_of(float(key.value)))
        end = QPointF(self.x_of(nxt.frame + offset), self.y_of(float(nxt.value)))
        if key.interpolation == InterpolationType.HERMITE:
            first = QPointF(self.x_of(key.frame + offset + out_x / 3), self.y_of(float(key.value) + out_y / 3))
            second = QPointF(self.x_of(nxt.frame + offset + in_x / 3), self.y_of(float(nxt.value) + in_y / 3))
        else:
            span = (nxt.frame - key.frame) / 3
            first = QPointF(self.x_of(key.frame + offset + span), self.y_of(float(key.value) + out_y))
            second = QPointF(self.x_of(nxt.frame + offset - span), self.y_of(float(nxt.value) - in_y))
        return [(0, start, first), (2, end, second)]

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        for prop in self._shown:
            offset = self.state.document.time_offset(prop)
            for position, key in enumerate(prop.keys):
                if key in self.state.selection:
                    for slot, _anchor, handle in self._handles(prop, position, offset):
                        if (handle - pos).manhattanLength() <= 6:
                            self._drag = {"handle": slot, "key": key, "prop": prop, "position": position, "press": pos,
                                          "merge": ("curve", next(_drag_ids))}
                            return
                center = QPointF(self.x_of(key.frame + offset), self.y_of(float(key.value)))
                if (center - pos).manhattanLength() <= 7:
                    mode = "toggle" if event.modifiers() & Qt.ControlModifier else "replace"
                    if key not in self.state.selection or mode != "replace":
                        self.state.select([key], mode)
                    self._drag = {"key": key, "prop": prop, "press": pos, "frame": key.frame,
                                  "value": float(key.value), "merge": ("curve", next(_drag_ids))}
                    return
        self.state.select(list(self._shown))

    def mouseMoveEvent(self, event) -> None:
        drag = self._drag
        if drag is None:
            return
        pos, key, prop = event.position(), drag["key"], drag["prop"]
        if not drag.get("moving"):
            if (pos - drag["press"]).manhattanLength() < QApplication.startDragDistance():
                return
            drag["moving"] = True
        document = self.state.document
        if "handle" in drag:
            nxt = prop.keys[drag["position"] + 1]
            control = list(key.control)
            if key.interpolation == InterpolationType.HERMITE:
                anchor = key if drag["handle"] == 0 else nxt
                offset = document.time_offset(prop)
                control[drag["handle"]] = (self.frame_of(pos.x()) - offset - anchor.frame) * 3
                control[drag["handle"] + 1] = (self.value_of(pos.y()) - float(anchor.value)) * 3
            else:
                sign = 1.0 if drag["handle"] == 0 else -1.0
                anchor = key if drag["handle"] == 0 else nxt
                control[drag["handle"] + 1] = (self.value_of(pos.y()) - float(anchor.value)) * sign
            edits.set_fields(document, key, "Edit Curve", drag["merge"],
                             control=tuple(float(value) for value in control))
            return
        press = drag["press"]
        frame = drag["frame"] + self.state.snapped(self.frame_of(pos.x()) - self.frame_of(press.x()))
        value = drag["value"] + self.value_of(pos.y()) - self.value_of(press.y())
        edits.set_fields(document, key, "Edit Curve", drag["merge"], frame=float(frame),
                         value=coerce(property_type(prop.type).value, value))

    def mouseReleaseEvent(self, event) -> None:
        self._drag = None

    def wheelEvent(self, event) -> None:
        state, delta = self.state, event.angleDelta()
        if event.modifiers() & Qt.ControlModifier:
            anchor = self.frame_of(event.position().x())
            scale = state.view_scale * 1.15 ** (delta.y() / 120)
            state.set_view(anchor - (event.position().x() - MARGIN) / max(min(scale, 400.0), 0.02), scale)
        else:
            low, high = self._range
            factor = 1.15 ** (-delta.y() / 120)
            center = self.value_of(event.position().y())
            low, high = center - (center - low) * factor, center + (high - center) * factor
            if 1e-6 < high - low < 1e300:
                self._range = (low, high)
                self.update()
        event.accept()
