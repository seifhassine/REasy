"""Reusable graph view for Wwise event and HIRC relationships."""

from __future__ import annotations

import math
from collections import Counter

from PySide6.QtCore import QLineF, QRectF, Qt, QTimer, Signal
lazy from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QSizePolicy

lazy from ui.styles import current_colors


class EventFlowGraph(QGraphicsView):
    """Small Wwise-style Event → Action → object → source graph."""

    # Wwise ShortIDs are unsigned 32-bit values. PySide's ``int`` signal type
    # is signed 32-bit and overflows for IDs above 0x7FFFFFFF.
    node_selected = Signal(str, object)
    node_activated = Signal(str, object)
    _WIDTH, _HEIGHT, _X_GAP, _Y_GAP = 158, 68, 44, 12
    _HEADER, _GRID = 22, 16
    _COLORS = {
        "event": "#67459b",
        "action": "#1769aa",
        "object": "#455a64",
        "any": "#3b4252",
        "ready": "#2e7d4f",
        "partial": "#9a6200",
        "missing": "#8f3232",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        self.viewport().setMouseTracking(True)
        self._pending_node = None
        self._node_timer = QTimer(self)
        self._node_timer.setSingleShot(True)
        self._node_timer.timeout.connect(self._emit_pending_node)
        # Deferred until the nodes are laid out; a child timer dies with the view.
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self.fit_graph)
        self._has_graph = False
        self._pan_start = None
        self._pan_scroll = None
        self._pan_tag = None
        self._pan_moved = False
        self.clear_message(self.tr("Choose an event to see its playback flow."))

    def _reset_items(self):
        self._node_rects = {}
        self._edge_items = []
        self._edge_focus_tag = self._center_point = self._selected_tag = None

    @staticmethod
    def _tag(item):
        while item is not None:
            tag = item.data(0)
            if isinstance(tag, tuple) and len(tag) == 2:
                return tag
            item = item.parentItem()
        return None

    def drawBackground(self, painter, rect):
        colors = current_colors()
        painter.fillRect(rect, QColor(colors["canvas_bg"]))
        scale = self.transform().m11()
        step = self._GRID
        if step * scale < 6:
            step *= 4
        if step * scale < 6:
            return
        major_step = self._GRID * 4
        minor_lines, major_lines = [], []
        x = math.floor(rect.left() / step) * step
        while x <= rect.right():
            (major_lines if x % major_step == 0 else minor_lines).append(
                QLineF(x, rect.top(), x, rect.bottom())
            )
            x += step
        y = math.floor(rect.top() / step) * step
        while y <= rect.bottom():
            (major_lines if y % major_step == 0 else minor_lines).append(
                QLineF(rect.left(), y, rect.right(), y)
            )
            y += step
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(QPen(QColor(colors["canvas_grid_minor"]), 0))
        painter.drawLines(minor_lines)
        painter.setPen(QPen(QColor(colors["canvas_grid_major"]), 0))
        painter.drawLines(major_lines)
        painter.restore()

    def clear_message(self, message: str):
        self._reset_items()
        self.scene().clear()
        item = self.scene().addText(message)
        item.setDefaultTextColor(QColor(current_colors()["text_muted"]))
        item.setPos(16, 16)
        self.scene().setSceneRect(0, 0, 600, 180)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.resetTransform()
        self._has_graph = False

    def set_graph(self, nodes, edges):
        if not nodes:
            self.clear_message(self.tr("No playback route was resolved."))
            return
        scene = self.scene()
        self._reset_items()
        scene.clear()
        columns: dict[int, list[dict]] = {}
        for node in nodes:
            columns.setdefault(node["depth"], []).append(node)
        rows = max(map(len, columns.values()))
        content_height = rows * (self._HEIGHT + self._Y_GAP) - self._Y_GAP
        positions = {}
        for depth, column in sorted(columns.items()):
            top = 20 + (content_height - (len(column) * (self._HEIGHT + self._Y_GAP) - self._Y_GAP)) / 2
            for row, node in enumerate(column):
                positions[node["key"]] = (
                    20 + depth * (self._WIDTH + self._X_GAP),
                    top + row * (self._HEIGHT + self._Y_GAP),
                )

        valid_edges = [
            (source, target) for source, target in edges
            if source in positions and target in positions
        ]
        outgoing = Counter(source for source, _target in valid_edges)
        incoming = Counter(target for _source, target in valid_edges)
        outgoing_index, incoming_index = Counter(), Counter()
        tags = {
            node["key"]: (node["kind"], int(node["object_id"]))
            for node in nodes
        }
        titles = {node["key"]: node["title"] for node in nodes}
        colors = current_colors()
        edge_pen = QPen(QColor(colors["wire"]), 1.3)
        for source, target in valid_edges:
            x1, y1 = positions[source]
            x2, y2 = positions[target]
            x1 += self._WIDTH
            y1 += (outgoing_index[source] + 1) * self._HEIGHT / (outgoing[source] + 1)
            y2 += (incoming_index[target] + 1) * self._HEIGHT / (incoming[target] + 1)
            outgoing_index[source] += 1
            incoming_index[target] += 1
            bend = max(24, (x2 - x1) * 0.4)
            path = QPainterPath()
            path.moveTo(x1, y1)
            path.cubicTo(x1 + bend, y1, x2 - bend, y2, x2, y2)
            item = scene.addPath(path, edge_pen)
            item.setZValue(-1)
            item.setToolTip(f"{titles[source]} → {titles[target]}")
            self._edge_items.append((item, tags[source], tags[target]))

        title_font = QFont(self.font())
        title_font.setBold(True)
        title_metrics = QFontMetrics(title_font)
        body_brush = QBrush(QColor(colors["node_bg"]))
        border = colors["node_border"]
        accent = colors["accent"]
        title_color, selected_title_color = QColor(colors["text_bright"]), QColor(colors["accent_text"])
        detail_color = QColor(colors["text_muted"])
        title_y = (self._HEADER - title_metrics.height()) / 2 + 1
        for node in nodes:
            x, y = positions[node["key"]]
            tone = node["tone"]
            header_color = accent if tone == "selected" else self._COLORS.get(tone, self._COLORS["object"])
            tag = (node["kind"], int(node["object_id"]))
            outline, width = (accent, 2.0) if node.get("selected") else (border, 1.0)
            body_path = QPainterPath()
            body_path.addRoundedRect(QRectF(x, y, self._WIDTH, self._HEIGHT), 3, 3)
            rect = scene.addPath(body_path, QPen(QColor(outline), width), body_brush)
            rect.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape, True)
            rect.setZValue(1)
            rect.setData(0, tag)
            rect.setToolTip(node["detail"])
            rect.setData(1, outline)
            rect.setData(2, width)
            self._node_rects[tag] = rect
            if node.get("selected"):
                self._center_point = (x + self._WIDTH / 2, y + self._HEIGHT / 2)
            header_path = QPainterPath()
            header_path.addRoundedRect(QRectF(x, y, self._WIDTH, self._HEADER), 3, 3)
            square = QPainterPath()
            square.addRect(QRectF(x, y + self._HEADER / 2, self._WIDTH, self._HEADER / 2))
            # Children are added through the scene, which owns them; a parented wrapper may not.
            header = scene.addPath(header_path.united(square), Qt.PenStyle.NoPen, QColor(header_color))
            header.setParentItem(rect)
            header.setData(0, tag)
            header.setToolTip(node["detail"])
            title = scene.addSimpleText(
                title_metrics.elidedText(node["title"], Qt.TextElideMode.ElideRight, self._WIDTH - 16),
                title_font,
            )
            title.setParentItem(rect)
            title.setBrush(selected_title_color if tone == "selected" else title_color)
            title.setPos(x + 8, y + title_y)
            title.setData(0, tag)
            title.setToolTip(f"{node['title']}\n{node['detail']}")
            detail = scene.addText(node["detail"])
            detail.setParentItem(rect)
            detail.setDefaultTextColor(detail_color)
            detail.setTextWidth(self._WIDTH - 8)
            detail.setPos(x + 4, y + self._HEADER + 2)
            detail.setData(0, tag)

        width = max(x + self._WIDTH for x, _y in positions.values()) + 20
        scene.setSceneRect(0, 0, width, content_height + 40)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._has_graph = True
        self._fit_timer.start()

    def cleanup(self):
        self._node_timer.stop()
        self._pending_node = None
        self._has_graph = False
        self._reset_items()
        self.scene().clear()

    def select_node(self, kind, object_id):
        tag = (kind, int(object_id))
        if self._selected_tag in self._node_rects:
            old = self._node_rects[self._selected_tag]
            old.setPen(QPen(QColor(old.data(1)), float(old.data(2))))
        self._selected_tag = tag if tag in self._node_rects else None
        if self._selected_tag:
            self._node_rects[tag].setPen(QPen(QColor(current_colors()["accent"]), 2.0))
        self._focus_edges(self._selected_tag)

    def _queue_node(self, tag, activate=False):
        self.select_node(*tag)
        self._pending_node = tag, activate
        # Selection handlers rebuild the scene; let the current mouse event finish first.
        self._node_timer.start(0)

    def _emit_pending_node(self):
        if not self._pending_node:
            return
        tag, activate = self._pending_node
        self._pending_node = None
        self.node_selected.emit(tag[0], int(tag[1]))
        if activate:
            self.node_activated.emit(tag[0], int(tag[1]))

    def _focus_edges(self, tag):
        if tag == self._edge_focus_tag:
            return
        self._edge_focus_tag = tag
        colors = current_colors()
        normal = QPen(QColor(colors["wire"]), 1.3)
        dim = QPen(QColor(colors["border"]), 1.0)
        focus = QPen(QColor(colors["accent"]), 2.5)
        for item, source, target in self._edge_items:
            connected = tag is not None and tag in (source, target)
            item.setPen(focus if connected else dim if tag is not None else normal)
            item.setZValue(0 if connected else -1)

    def fit_graph(self):
        if not self._has_graph:
            return
        self.resetTransform()
        scale = min(
            1.0,
            max(
                0.5 if self._center_point else 0.25,
                min(
                    (self.viewport().width() - 16) / max(1, self.scene().width()),
                    (self.viewport().height() - 16) / max(1, self.scene().height()),
                ),
            ),
        )
        self.scale(scale, scale)
        if self._center_point:
            self.centerOn(*self._center_point)
        else:
            self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)

    def zoom_by(self, factor: float):
        current = self.transform().m11()
        target = min(4.0, max(0.2, current * factor))
        if current:
            self.scale(target / current, target / current)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self.zoom_by(1.18 if delta > 0 else 1 / 1.18)
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pan_start = event.position().toPoint()
            self._pan_scroll = (
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value(),
            )
            self._pan_tag = self._tag(self.itemAt(self._pan_start))
            self._pan_moved = False
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.position().toPoint() - self._pan_start
            self._pan_moved |= delta.manhattanLength() > 3
            self.horizontalScrollBar().setValue(self._pan_scroll[0] - delta.x())
            self.verticalScrollBar().setValue(self._pan_scroll[1] - delta.y())
            event.accept()
            return
        self._focus_edges(
            self._tag(self.itemAt(event.position().toPoint())) or self._selected_tag
        )
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._focus_edges(self._selected_tag)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pan_start is not None:
            tag = self._tag(self.itemAt(event.position().toPoint()))
            if not self._pan_moved and tag and tag == self._pan_tag:
                self._queue_node(tag)
            self._pan_start = self._pan_scroll = self._pan_tag = None
            self._pan_moved = False
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        tag = self._tag(self.itemAt(event.position().toPoint()))
        if tag:
            self._queue_node(tag, activate=True)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
