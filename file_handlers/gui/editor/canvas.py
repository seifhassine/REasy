"""See docs/internals/gui-editor.md, Canvas."""

from __future__ import annotations

from PySide6.QtCore import QLineF, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

lazy from ui.styles import current_colors

from .state import EditorState

_MARGIN, _STAGE, _GRIP = 10.0, QColor(2, 4, 6), 4.0
_STEPS = {Qt.Key.Key_Left: (-1.0, 0.0), Qt.Key.Key_Right: (1.0, 0.0), Qt.Key.Key_Up: (0.0, -1.0),
          Qt.Key.Key_Down: (0.0, 1.0)}


class Canvas(QWidget):
    menu_requested = Signal(QPoint)
    drawn = Signal()

    def __init__(self, state: EditorState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self.zoom, self.pan, self.guides, self.live = 1.0, QPointF(), False, False
        self._gaps: list | None = None
        self._drag: dict | None = None
        self._grab: QPointF | None = None
        self.setMinimumSize(320, 200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        for signal in (state.changed, state.selection_changed, state.frame_changed, state.shown_changed):
            signal.connect(self.update)

    def fit(self) -> None:
        self.zoom, self.pan = 1.0, QPointF()
        self.update()

    def set_guides(self, on: bool) -> None:
        self.guides = on
        self.update()

    def set_live(self, on: bool) -> None:
        self.live, self._drag = on, None
        self.unsetCursor()
        self.update()

    def frame(self) -> tuple[QRectF, float]:
        width, height = self.state.stage.size
        room = QRectF(_MARGIN, _MARGIN, max(1.0, self.width() - 2 * _MARGIN), max(1.0, self.height() - 2 * _MARGIN))
        scale = min(room.width() / width, room.height() / height) * self.zoom
        origin = room.center() - QPointF(width * scale / 2, height * scale / 2) + self.pan
        return QRectF(origin.x(), origin.y(), width * scale, height * scale), scale

    def point(self, position: QPointF) -> QPointF:
        frame, scale = self.frame()
        return QPointF((position.x() - frame.left()) / scale, (position.y() - frame.top()) / scale)

    def paintEvent(self, _event) -> None:
        colors, stage = current_colors(), self.state.stage
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor(colors["canvas_bg"]))
        frame, scale = self.frame()
        painter.fillRect(frame, _STAGE)
        image, (width, height) = stage.image(), stage.size
        if stage.gaps is not self._gaps:
            self._gaps = stage.gaps
            self.drawn.emit()
        painter.save()
        painter.setClipRect(frame)
        painter.translate(frame.topLeft())
        painter.scale(scale, scale)
        if not image.isNull():
            painter.drawImage(QRectF(0.0, 0.0, width, height), image)
        scene, chosen = self.state.scene, {id(node) for node in self.state.selection}
        copies = {id(node) for node in scene.draw_nodes if node.source is not None and id(node.origin()) in chosen}
        painter.setBrush(Qt.BrushStyle.NoBrush)
        quiet, copied = QColor(colors["text_muted"]), QColor(colors["accent"])
        quiet.setAlpha(90)
        copied.setAlpha(140)
        for node in scene.draw_nodes if self.guides else () if self.live else [
                *self.state.selection, *(node for node in scene.draw_nodes if id(node) in copies)]:
            if node is scene.root:
                continue
            pen = QPen(QColor(colors["accent"]) if id(node) in chosen else copied if id(node) in copies else quiet, 0.0)
            if not node.effective_visible:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawPath(stage.outline(node))
        painter.restore()
        painter.setPen(QPen(QColor(colors["accent"]), 1.0))
        painter.setBrush(QColor(colors["canvas_bg"]))
        for spot in self._grips().values():
            painter.drawRect(QRectF(spot.x() - _GRIP, spot.y() - _GRIP, 2 * _GRIP, 2 * _GRIP))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(colors["border_strong"]), 1.0))
        painter.drawRect(frame)
        if image.isNull():
            painter.setPen(QColor(colors["text_muted"]))
            painter.drawText(frame.adjusted(12, 12, -12, -12),
                             int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                             self.tr("No preview: {0}").format("; ".join(
                                 reason for path, reason in stage.gaps if path == scene.root.path)))
        if self.height() - frame.bottom() >= 18:
            painter.setPen(QColor(colors["text_subtle"]))
            painter.drawText(QRectF(_MARGIN, frame.bottom() + 2.0, self.width() - 2 * _MARGIN, 16.0),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             self.tr("{0}% · Ctrl+Wheel zooms · Middle-drag pans").format(round(self.zoom * 100)))

    def _grips(self) -> dict[tuple[int, int], QPointF]:
        state = self.state
        if self.live or len(state.selection) != 1 or state.mode(state.current) == "locked":
            return {}
        frame, scale = self.frame()
        spots = {key: frame.topLeft() + point * scale for key, point in state.stage.handles(state.current).items()}
        sides = [QLineF(spots[low], spots[high]).length() for low, high in (((-1, 0), (1, 0)), ((0, -1), (0, 1)))
                 if spots]
        return {key: spot for key, spot in spots.items()
                if all(sides[at] >= 3 * _GRIP if part else not 3 * _GRIP <= sides[at] < 6 * _GRIP
                       for at, part in enumerate(key))}

    def _grip(self, at: QPointF) -> tuple[int, int] | None:
        return next((key for key, spot in self._grips().items()
                     if max(abs(spot.x() - at.x()), abs(spot.y() - at.y())) <= _GRIP + 1.0), None)

    def _menu(self, at: QPoint) -> None:
        if not self.live:
            self.menu_requested.emit(self.mapToGlobal(at))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._grab = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return
        state, at = self.state, self.point(event.position())
        if self.live:
            if event.button() == Qt.MouseButton.LeftButton:
                state.stage.press(at.x(), at.y())
                self.update()
            return
        grip = self._grip(event.position()) if event.button() == Qt.MouseButton.LeftButton else None
        if grip is not None:
            self._drag = {"node": state.current, "grip": grip, "sized": None}
            return
        found = state.stage.pick(at.x(), at.y())
        held = state.current
        if event.button() == Qt.MouseButton.RightButton:
            if found and not any(node in state.selection for node in found):
                state.select(found[:1])
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if found:
                state.select([item for item in state.selection if item is not found[0]]
                             if found[0] in state.selection else [*state.selection, found[0]])
            return
        if event.modifiers() & Qt.KeyboardModifier.AltModifier and held in found:
            node = found[(found.index(held) + 1) % len(found)]
        else:
            node = held if held in found else found[0] if found else None
        front = found[0] if node is held and found and found[0] is not held else None
        state.select([node] if node is not None else [])
        if node is not None and state.mode(node) != "locked":
            self._drag = {"node": node, "anchor": state.stage.anchor(node), "start": at, "local": None,
                          "front": front}
        elif front is not None:
            state.select([front])

    def mouseMoveEvent(self, event) -> None:
        if self._grab is not None:
            self.pan += event.position() - self._grab
            self._grab = event.position()
            self.update()
            return
        drag = self._drag
        stage, at = self.state.stage, self.point(event.position())
        if self.live:
            self.setCursor(Qt.CursorShape.PointingHandCursor if stage.hover(at.x(), at.y())
                           else Qt.CursorShape.ArrowCursor)
            self.update()
            return
        if drag is None:
            grip = self._grip(event.position())
            if grip is None:
                self.unsetCursor()
            else:
                self.setCursor(Qt.CursorShape.SizeVerCursor if not grip[0] else Qt.CursorShape.SizeHorCursor
                               if not grip[1] else Qt.CursorShape.SizeFDiagCursor if grip[0] == grip[1]
                               else Qt.CursorShape.SizeBDiagCursor)
            return
        if "grip" in drag:
            drag["sized"] = stage.resize(drag["node"], drag["grip"], at.x(), at.y())
            stage.layout((drag["node"], *drag["sized"][1], drag["sized"][0]))
            self.update()
            return
        moved = at - drag["start"]
        if drag["local"] is None and abs(moved.x()) + abs(moved.y()) < 2.0 / max(self.frame()[1], 1e-6):
            return
        found = self.state.attempt(stage.local, drag["node"], drag["anchor"][0] + moved.x(),
                                   drag["anchor"][1] + moved.y())
        if found is None:
            self._drag = None
            stage.layout()
            self.update()
            return
        drag["local"] = tuple(float(round(part)) for part in found)
        stage.layout((drag["node"], *drag["local"]))
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._grab = None
            self.unsetCursor()
            return
        drag, self._drag = self._drag, None
        if event.button() != Qt.MouseButton.LeftButton or drag is None:
            return
        if "grip" in drag:
            if drag["sized"] is not None:
                self.state.stage.layout()
                self.state.attempt(self.state.resize, drag["node"], *drag["sized"])
                self.update()
            return
        if drag["local"] is not None:
            self._place(drag["node"], *drag["local"])
        elif drag["front"] is not None:
            self.state.select([drag["front"]])

    def _place(self, node, x: float, y: float) -> None:
        state = self.state
        name = state.stage.position_field
        state.stage.layout()
        state.attempt(state.set_value, node, name, [x, y, *state.value(node, name)[2:]])
        self.update()

    def keyPressEvent(self, event) -> None:
        state, step = self.state, _STEPS.get(event.key())
        if self.live:
            if state.stage.key(event):
                self.update()
            else:
                super().keyPressEvent(event)
            return
        if event.key() == Qt.Key.Key_Escape and self._drag is not None:
            self._drag = None
            state.stage.layout()
            self.update()
            return
        if step is None or not state.selection:
            super().keyPressEvent(event)
            return
        times = 10.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
        state.attempt(self._nudge, step[0] * times, step[1] * times)

    def keyReleaseEvent(self, event) -> None:
        self.state.stage.release(event)
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:
        self.state.stage.release()
        super().focusOutEvent(event)

    def _nudge(self, x: float, y: float) -> None:
        state, name = self.state, self.state.stage.position_field
        with state.document.macro(self.tr("Move")):
            for node in state.selection:
                held = state.value(node, name)
                state.set_value(node, name, [held[0] + x, held[1] + y, *held[2:]])

    def wheelEvent(self, event) -> None:
        if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            super().wheelEvent(event)
            return
        before = self.point(event.position())
        self.zoom = min(16.0, max(0.1, self.zoom * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15)))
        frame, scale = self.frame()
        self.pan += event.position() - QPointF(frame.left() + before.x() * scale, frame.top() + before.y() * scale)
        self.update()
        event.accept()
