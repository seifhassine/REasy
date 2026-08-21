"""Reusable graph view for Wwise event and HIRC relationships."""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QSizePolicy


class EventFlowGraph(QGraphicsView):
    """Small Wwise-style Event → Action → object → source graph."""

    # Wwise ShortIDs are unsigned 32-bit values. PySide's ``int`` signal type
    # is signed 32-bit and overflows for IDs above 0x7FFFFFFF.
    node_selected = Signal(str, object)
    node_activated = Signal(str, object)
    _WIDTH, _HEIGHT, _X_GAP, _Y_GAP = 158, 68, 44, 12
    _COLORS = {
        "event": ("#67459b", "#a78ad0"),
        "action": ("#1769aa", "#69afe5"),
        "object": ("#455a64", "#90a4ae"),
        "selected": ("#755b19", "#f0c85a"),
        "any": ("#3b4252", "#7b8496"),
        "ready": ("#2e7d4f", "#72c095"),
        "partial": ("#9a6200", "#e0ad4d"),
        "missing": ("#8f3232", "#dc7373"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QBrush(QColor("#202226")))
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

    def clear_message(self, message: str):
        self._reset_items()
        self.scene().clear()
        item = self.scene().addText(message)
        item.setDefaultTextColor(QColor("#c1c5cc"))
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
        edge_pen = QPen(QColor("#777e88"), 1.3)
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
        for node in nodes:
            x, y = positions[node["key"]]
            fill, outline = self._COLORS.get(node["tone"], self._COLORS["object"])
            tag = (node["kind"], int(node["object_id"]))
            width = 2.4 if node.get("selected") else 1.4
            rect = scene.addRect(x, y, self._WIDTH, self._HEIGHT, QPen(QColor(outline), width), QBrush(QColor(fill)))
            rect.setZValue(1)
            rect.setData(0, tag)
            rect.setToolTip(node["detail"])
            rect.setData(1, outline)
            rect.setData(2, width)
            self._node_rects[tag] = rect
            if node.get("selected"):
                self._center_point = (x + self._WIDTH / 2, y + self._HEIGHT / 2)
            title = scene.addText(node["title"], title_font)
            title.setZValue(2)
            title.setDefaultTextColor(QColor("white"))
            title.setTextWidth(self._WIDTH - 16)
            title.setPos(x + 7, y + 4)
            title.setData(0, tag)
            detail = scene.addText(node["detail"])
            detail.setZValue(2)
            detail.setDefaultTextColor(QColor("#e1e4e8"))
            detail.setTextWidth(self._WIDTH - 16)
            detail.setPos(x + 7, y + 31)
            detail.setData(0, tag)

        width = max(x + self._WIDTH for x, _y in positions.values()) + 20
        scene.setSceneRect(0, 0, width, content_height + 40)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._has_graph = True
        QTimer.singleShot(0, self, self.fit_graph)

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
            self._node_rects[tag].setPen(QPen(QColor("#ffffff"), 3.0))
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
        normal = QPen(QColor("#777e88"), 1.3)
        dim = QPen(QColor("#3f444c"), 1.0)
        focus = QPen(QColor("#ffd166"), 2.5)
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
        """Zoom around the pointer while keeping the graph usable."""

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
