"""Reusable graph view for Wwise event and HIRC relationships."""

from __future__ import annotations

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
        self._has_graph = False
        self._node_rects = {}
        self._selected_tag = None
        self._pan_start = None
        self._pan_scroll = None
        self._pan_tag = None
        self._pan_moved = False
        self.clear_message(self.tr("Choose an event to see its playback flow."))

    @staticmethod
    def _tag(item):
        while item is not None:
            tag = item.data(0)
            if isinstance(tag, tuple) and len(tag) == 2:
                return tag
            item = item.parentItem()
        return None

    def clear_message(self, message: str):
        self.scene().clear()
        self._node_rects = {}
        self._selected_tag = None
        item = self.scene().addText(message)
        item.setDefaultTextColor(QColor("#c1c5cc"))
        item.setPos(16, 16)
        self.scene().setSceneRect(0, 0, 600, 180)
        self.resetTransform()
        self._has_graph = False

    def set_graph(self, nodes, edges):
        if not nodes:
            self.clear_message(self.tr("No playback route was resolved."))
            return
        scene = self.scene()
        scene.clear()
        self._node_rects = {}
        self._selected_tag = None
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

        edge_pen = QPen(QColor("#858b94"), 1.4)
        for source, target in edges:
            if source not in positions or target not in positions:
                continue
            x1, y1 = positions[source]
            x2, y2 = positions[target]
            x1, y1 = x1 + self._WIDTH, y1 + self._HEIGHT / 2
            y2 += self._HEIGHT / 2
            bend = max(24, (x2 - x1) * 0.4)
            path = QPainterPath()
            path.moveTo(x1, y1)
            path.cubicTo(x1 + bend, y1, x2 - bend, y2, x2, y2)
            scene.addPath(path, edge_pen)

        title_font = QFont(self.font())
        title_font.setBold(True)
        for node in nodes:
            x, y = positions[node["key"]]
            fill, outline = self._COLORS.get(node["tone"], self._COLORS["object"])
            tag = (node["kind"], int(node["object_id"]))
            rect = scene.addRect(x, y, self._WIDTH, self._HEIGHT, QPen(QColor(outline), 1.4), QBrush(QColor(fill)))
            rect.setData(0, tag)
            rect.setToolTip(node["detail"])
            rect.setData(1, outline)
            self._node_rects[tag] = rect
            title = scene.addText(node["title"], title_font)
            title.setDefaultTextColor(QColor("white"))
            title.setTextWidth(self._WIDTH - 16)
            title.setPos(x + 7, y + 4)
            title.setData(0, tag)
            detail = scene.addText(node["detail"])
            detail.setDefaultTextColor(QColor("#e1e4e8"))
            detail.setTextWidth(self._WIDTH - 16)
            detail.setPos(x + 7, y + 31)
            detail.setData(0, tag)

        width = max(x + self._WIDTH for x, _y in positions.values()) + 20
        scene.setSceneRect(0, 0, width, content_height + 40)
        self._has_graph = True
        QTimer.singleShot(0, self.fit_graph)

    def select_node(self, kind, object_id):
        tag = (kind, int(object_id))
        if self._selected_tag in self._node_rects:
            old = self._node_rects[self._selected_tag]
            old.setPen(QPen(QColor(old.data(1)), 1.4))
        self._selected_tag = tag if tag in self._node_rects else None
        if self._selected_tag:
            self._node_rects[tag].setPen(QPen(QColor("#ffffff"), 3.0))

    def fit_graph(self):
        if not self._has_graph:
            return
        self.resetTransform()
        scale = min(
            1.0,
            max(
                0.25,
                min(
                    (self.viewport().width() - 16) / max(1, self.scene().width()),
                    (self.viewport().height() - 16) / max(1, self.scene().height()),
                ),
            ),
        )
        self.scale(scale, scale)
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(0)

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
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pan_start is not None:
            tag = self._tag(self.itemAt(event.position().toPoint()))
            if not self._pan_moved and tag and tag == self._pan_tag:
                self.select_node(*tag)
                self.node_selected.emit(tag[0], int(tag[1]))
            self._pan_start = self._pan_scroll = self._pan_tag = None
            self._pan_moved = False
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        tag = self._tag(self.itemAt(event.position().toPoint()))
        if tag:
            self.select_node(*tag)
            self.node_selected.emit(tag[0], int(tag[1]))
            self.node_activated.emit(tag[0], int(tag[1]))
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
