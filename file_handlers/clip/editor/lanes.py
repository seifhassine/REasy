from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QTreeView, QWidget

lazy from ui.styles import current_colors, qcolor

from .. import edits
from ..commands import SetFields
from ..model import Clip, Key, Node, Property, Track
from ..rules import InterpolationType, interpolation, owns_keys
from .state import EditorState

MARGIN = 12
EDGE = 4
KEY_RADIUS = 5
NODE_COLORS = {0: "#6b7280", 1: "#3f9f72", 2: "#477fc2", 3: "#9b7d33"}
KEY_COLORS = {"curve": "#e0a050", "step": "#9aa7b8", "event": "#c77dd8"}
EXTRA_KEY_COLOR = "#d86a6a"
SPEED_COLOR = "#4fb0c6"
_STEPS = [1, 2, 5]
_EVENTS = {InterpolationType.EVENT, InterpolationType.PASS_EVENT}
_drag_ids = itertools.count(1)


def tone(token: str) -> QColor:
    return qcolor(current_colors()[token])


@dataclass(frozen=True, slots=True)
class Hit:
    item: object
    part: str
    row_item: object


def key_family(code: int) -> str:
    if code in _EVENTS:
        return "event"
    info = interpolation(code)
    return "curve" if info is not None and info.numeric else "step"


def tick_step(scale: float, spacing: float = 60.0) -> float:
    span = spacing / scale if 0 < scale < math.inf else math.inf
    if not math.isfinite(span):
        return math.inf
    power = max(0, math.floor(math.log10(span)))
    return next(step * 10.0 ** exponent for exponent in (power, power + 1) for step in _STEPS
                if step * 10.0 ** exponent >= span)


def ticks(low: float, high: float, step: float) -> list[float]:
    if not (math.isfinite(low) and math.isfinite(high) and math.isfinite(step)):
        return []
    first = math.floor(low / step)
    return [index * step for index in range(first, min(math.floor(high / step), first + 1000) + 1)]


class LaneView(QWidget):
    add_key_requested = Signal(object, float)
    context_requested = Signal(object, QPoint)

    def __init__(self, state: EditorState, tree: QTreeView, parent: QWidget | None = None):
        super().__init__(parent)
        self.state, self.tree = state, tree
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumWidth(200)
        self._drag: dict | None = None
        self._band: QRectF | None = None
        tree.verticalScrollBar().valueChanged.connect(self.update)
        tree.expanded.connect(self.update)
        tree.collapsed.connect(self.update)
        model = tree.model()
        for signal in (model.modelReset, model.layoutChanged, model.dataChanged, model.rowsInserted):
            signal.connect(self.update)
        state.selection_changed.connect(self.update)
        state.frame_changed.connect(self.update)
        state.view_changed.connect(self.update)
        state.document.changed.connect(self.update)

    def ruler_height(self) -> int:
        return self.tree.header().height()

    def x_of(self, frame: float) -> float:
        return (frame - self.state.view_start) * self.state.view_scale + MARGIN

    def frame_of(self, x: float) -> float:
        return (x - MARGIN) / self.state.view_scale + self.state.view_start

    def rows(self) -> list[tuple[object, int, int]]:
        model, viewport = self.tree.model(), self.tree.viewport()
        rows, index, top = [], self.tree.indexAt(QPoint(4, 1)), self.ruler_height()
        while index.isValid():
            rect = self.tree.visualRect(index)
            if rect.top() > viewport.height():
                break
            rows.append((model.item(index), rect.top() + top, rect.height()))
            index = self.tree.indexBelow(index)
        return rows

    def fit(self) -> None:
        end = max(self.state.document.timeline.total_frame, 1.0)
        width = max(self.width() - 2 * MARGIN, 50)
        self.state.set_view(0.0, width / end)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), tone("canvas_bg"))
        frames = ticks(self.frame_of(0), self.frame_of(self.width()), tick_step(self.state.view_scale))
        painter.setPen(QPen(tone("canvas_grid_minor"), 1))
        for frame in frames:
            x = self.x_of(frame)
            painter.drawLine(QPointF(x, self.ruler_height()), QPointF(x, self.height()))
        selected = {id(item) for item in self.state.selection}
        for item, top, height in self.rows():
            if any(item is chosen for chosen in self.state.selection
                   if isinstance(chosen, (Track, Clip, Node, Property))):
                painter.fillRect(QRectF(0, top, self.width(), height), tone("selection_inactive"))
            self._paint_row(painter, item, top, height, selected)
        self._paint_end(painter)
        self._paint_ruler(painter, frames)
        if self._band is not None:
            painter.setPen(QPen(tone("accent"), 1))
            painter.setBrush(tone("accent_soft"))
            painter.drawRect(self._band)
        x = self.x_of(self.state.frame)
        painter.setPen(QPen(tone("accent"), 1))
        painter.drawLine(QPointF(x, self.ruler_height() - 6), QPointF(x, self.height()))
        painter.setBrush(tone("accent"))
        top = self.ruler_height()
        painter.drawPolygon(QPolygonF([QPointF(x - 5, top - 10), QPointF(x + 5, top - 10), QPointF(x, top - 3)]))
        painter.end()

    def _paint_end(self, painter: QPainter) -> None:
        end = self.x_of(self.state.document.timeline.total_frame)
        if end < self.width():
            painter.fillRect(QRectF(end, self.ruler_height(), self.width() - end, self.height()),
                             tone("canvas_grid_minor"))

    def _paint_ruler(self, painter: QPainter, frames: list[float]) -> None:
        height = self.ruler_height()
        painter.fillRect(QRectF(0, 0, self.width(), height), tone("header_bg"))
        painter.setPen(QPen(tone("border"), 1))
        painter.drawLine(QPointF(0, height - 0.5), QPointF(self.width(), height - 0.5))
        painter.setPen(tone("text_muted"))
        for frame in frames:
            x = self.x_of(frame)
            painter.drawLine(QPointF(x, height - 6), QPointF(x, height - 1))
            painter.drawText(QPointF(x + 3, height - 8), f"{frame:g}")

    def _paint_row(self, painter: QPainter, item, top: int, height: int, selected: set[int]) -> None:
        middle = top + height / 2
        if isinstance(item, Track):
            for clip in item.clips:
                self._paint_block(painter, clip, top, height, id(clip) in selected)
        elif isinstance(item, Clip):
            self._paint_block(painter, item, top, height, id(item) in selected)
        elif isinstance(item, (Node, Property)):
            offset = self.state.document.time_offset(item)
            color = QColor(NODE_COLORS.get(item.node_type, NODE_COLORS[0])) if isinstance(item, Node) \
                else tone("text_subtle")
            thickness = 8 if isinstance(item, Node) else 4
            rect = QRectF(self.x_of(item.begin_frame + offset), middle - thickness / 2,
                          max(self.x_of(item.end_frame + offset) - self.x_of(item.begin_frame + offset), 1), thickness)
            painter.setPen(QPen(tone("accent"), 1.5) if id(item) in selected else Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 2, 2)
            if isinstance(item, Property):
                for point in item.speed_points:
                    self._paint_speed_point(painter, self.x_of(point.frame + offset), top + height - 5,
                                            id(point) in selected)
                for key in item.keys:
                    self._paint_key(painter, self.x_of(key.frame + offset), middle, key, id(key) in selected)
                for slot, key in item.extra_keys.items():
                    self._paint_extra_key(painter, self.x_of(key.frame + offset), middle, slot, id(key) in selected)

    def _paint_block(self, painter: QPainter, clip: Clip, top: int, height: int, chosen: bool) -> None:
        rect = QRectF(self.x_of(clip.frame_in), top + 2, max(self.x_of(clip.frame_out) - self.x_of(clip.frame_in), 2),
                      height - 4)
        painter.setPen(QPen(tone("accent") if chosen else tone("border_strong"), 1.5 if chosen else 1))
        painter.setBrush(tone("surface_alt"))
        painter.drawRoundedRect(rect, 3, 3)
        if clip.name and rect.width() > 30:
            painter.setPen(tone("text"))
            painter.drawText(rect.adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, clip.name)

    def _paint_key(self, painter: QPainter, x: float, y: float, key: Key, chosen: bool) -> None:
        r = KEY_RADIUS
        path = QPainterPath()
        path.addPolygon(QPolygonF([QPointF(x, y - r), QPointF(x + r, y), QPointF(x, y + r), QPointF(x - r, y)]))
        path.closeSubpath()
        painter.setPen(QPen(tone("canvas_bg"), 1))
        painter.setBrush(tone("accent") if chosen else QColor(KEY_COLORS[key_family(key.interpolation)]))
        painter.drawPath(path)

    def _paint_extra_key(self, painter: QPainter, x: float, y: float, slot: int, chosen: bool) -> None:
        r = KEY_RADIUS + 1
        painter.setPen(QPen(tone("canvas_bg"), 1))
        painter.setBrush(tone("accent") if chosen else QColor(EXTRA_KEY_COLOR))
        painter.drawPolygon(QPolygonF([QPointF(x - r, y - r), QPointF(x + r, y - r), QPointF(x, y + r)]))
        painter.setPen(tone("text_bright"))
        painter.drawText(QRectF(x - r, y - r - 12, 2 * r, 12), Qt.AlignCenter, "L" if slot == 0 else str(slot))

    def _paint_speed_point(self, painter: QPainter, x: float, y: float, chosen: bool) -> None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(tone("accent") if chosen else QColor(SPEED_COLOR))
        painter.drawEllipse(QPointF(x, y), 3, 3)

    def hit(self, pos: QPointF) -> Hit | None:
        for item, top, height in self.rows():
            if not top <= pos.y() < top + height:
                continue
            middle = top + height / 2
            if isinstance(item, Property):
                offset = self.state.document.time_offset(item)
                for key in [*item.keys, *item.extra_keys.values()]:
                    if abs(self.x_of(key.frame + offset) - pos.x()) <= KEY_RADIUS + 1 and abs(pos.y() - middle) <= 7:
                        return Hit(key, "key", item)
                for point in item.speed_points:
                    if abs(self.x_of(point.frame + offset) - pos.x()) <= 4 and pos.y() > middle:
                        return Hit(point, "key", item)
            for block, start, end in self._blocks(item):
                left, right = self.x_of(start), self.x_of(end)
                if abs(pos.x() - left) <= EDGE:
                    return Hit(block, "start", item)
                if abs(pos.x() - right) <= EDGE:
                    return Hit(block, "end", item)
                if left < pos.x() < right:
                    return Hit(block, "body", item)
            return Hit(None, "empty", item)
        return None

    def _blocks(self, item) -> list[tuple[object, float, float]]:
        if isinstance(item, Track):
            return [(clip, clip.frame_in, clip.frame_out) for clip in item.clips]
        if isinstance(item, Clip):
            return [(item, item.frame_in, item.frame_out)]
        if isinstance(item, (Node, Property)):
            offset = self.state.document.time_offset(item)
            return [(item, item.begin_frame + offset, item.end_frame + offset)]
        return []

    def mousePressEvent(self, event) -> None:
        pos, state = event.position(), self.state
        if event.button() == Qt.RightButton:
            hit = self.hit(pos)
            self.context_requested.emit(hit, event.globalPosition().toPoint())
            return
        if event.button() != Qt.LeftButton:
            return
        if pos.y() < self.ruler_height():
            self._drag = {"mode": "scrub"}
            state.set_frame(state.snapped(self.frame_of(pos.x())))
            return
        hit = self.hit(pos)
        modifiers = event.modifiers()
        mode = "toggle" if modifiers & Qt.ControlModifier else "add" if modifiers & Qt.ShiftModifier else "replace"
        if hit is None or hit.item is None:
            if hit is not None and mode == "replace":
                state.select([hit.row_item])
            self._drag = {"mode": "band", "origin": pos, "select": mode}
            return
        if hit.item not in state.selection or mode != "replace":
            state.select([hit.item], mode)
        if hit.part == "key":
            targets = state.keys() if hit.item in state.selection else []
            drag = "move"
        elif hit.part == "body":
            targets, drag = [hit.item], "slip" if modifiers & Qt.AltModifier and isinstance(hit.item, Clip) else "move"
        else:
            targets, drag = [hit.item], hit.part
        self._drag = {"mode": drag, "targets": targets, "origin": self.frame_of(pos.x()), "applied": 0.0,
                      "merge": ("lanes", next(_drag_ids))}

    def mouseMoveEvent(self, event) -> None:
        drag, pos = self._drag, event.position()
        if drag is None:
            hit = self.hit(pos)
            self.setCursor(Qt.SizeHorCursor if hit is not None and hit.part in ("start", "end") else Qt.ArrowCursor)
            return
        if drag["mode"] == "scrub":
            self.state.set_frame(self.state.snapped(self.frame_of(pos.x())))
            return
        if drag["mode"] == "band":
            self._band = QRectF(drag["origin"], pos).normalized()
            self.update()
            return
        total = self.state.snapped(self.frame_of(pos.x()) - drag["origin"]) if self.state.snap else \
            self.frame_of(pos.x()) - drag["origin"]
        step = total - drag["applied"]
        if not step or not drag["targets"]:
            return
        document = self.state.document
        if drag["mode"] == "move":
            edits.shift(document, drag["targets"], step, drag["merge"])
        elif drag["mode"] == "slip":
            edits.slip(document, drag["targets"][0], step, drag["merge"])
        else:
            edits.trim(document, drag["targets"][0], drag["mode"] == "end", step, drag["merge"])
        drag["applied"] = total

    def mouseReleaseEvent(self, event) -> None:
        drag, self._drag = self._drag, None
        if drag is not None and drag["mode"] == "band" and self._band is not None:
            band, self._band = self._band, None
            found = [key for key, x, y in self._key_points() if band.contains(QPointF(x, y))]
            self.state.select(found, "replace" if drag["select"] == "replace" else "add")
            self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        hit, document = self.hit(event.position()), self.state.document
        if hit is not None and isinstance(hit.row_item, Property) and hit.part in ("empty", "body") and \
                owns_keys(hit.row_item.type, document.timeline.version):
            offset = document.time_offset(hit.row_item)
            self.add_key_requested.emit(hit.row_item, self.state.snapped(self.frame_of(event.position().x()) - offset))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape and self._drag is not None:
            drag, self._drag = self._drag, None
            stack = self.state.document.undo_stack
            top = stack.command(stack.index() - 1)
            if "merge" in drag and isinstance(top, SetFields) and top.merge_key == drag["merge"]:
                top.undo()
                top.setObsolete(True)
                stack.undo()
            self._band = None
            self.update()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        state, delta = self.state, event.angleDelta()
        if event.modifiers() & Qt.ControlModifier:
            anchor = self.frame_of(event.position().x())
            scale = state.view_scale * 1.15 ** (delta.y() / 120)
            state.set_view(anchor - (event.position().x() - MARGIN) / max(min(scale, 400.0), 0.02), scale)
        elif event.modifiers() & Qt.ShiftModifier or delta.x():
            amount = delta.x() or delta.y()
            state.set_view(state.view_start - amount / 120 * 40 / state.view_scale, state.view_scale)
        else:
            bar = self.tree.verticalScrollBar()
            bar.setValue(bar.value() - delta.y() // 40)
        event.accept()

    def _key_points(self):
        for item, top, height in self.rows():
            if isinstance(item, Property):
                offset, middle = self.state.document.time_offset(item), top + height / 2
                for key in [*item.keys, *item.extra_keys.values()]:
                    yield key, self.x_of(key.frame + offset), middle
                for point in item.speed_points:
                    yield point, self.x_of(point.frame + offset), top + height - 5
