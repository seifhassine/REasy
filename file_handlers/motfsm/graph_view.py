"""Qt scene items for a focused BHVT graph, with editor-only node positions."""
import math

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath,
                          QPainterPathStroker, QPen, QPolygonF)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsView


EDGE_COLORS = {'child': '#697581', 'start': '#e4a951', 'state': '#69abe0', 'all': '#b18bd5'}
EDGE_NAMES = {'child': 'Hierarchy', 'start': 'Initial state', 'state': 'Transition', 'all': 'All-state transition'}


class NodeItem(QGraphicsItem):
    WIDTH, HEIGHT = 234, 84

    def __init__(self, view, model, index, focus):
        super().__init__()
        self.view, self.model, self.index, self.focus = view, model, index, focus
        self.setFlags(QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemIsMovable |
                      QGraphicsItem.ItemSendsGeometryChanges)
        self.setZValue(1)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        node = model.nodes[index]
        self.setToolTip(f'{model.path(index)}\nNode [{index}] · 0x{node.id_hash:08X}, ex={node.ex_id}' +
                       ('\n'+'\n'.join(model.issues[index]) if model.issues[index] else ''))

    def boundingRect(self):
        return QRectF(-2, -2, self.WIDTH+4, self.HEIGHT+4)

    def paint(self, painter, option, widget=None):
        node = self.model.nodes[self.index]
        selected = self.isSelected()
        border = '#efc26b' if selected else '#70add9' if self.focus else '#66717d'
        painter.setPen(QPen(QColor(border), 2.2 if selected else 1.2))
        painter.setBrush(QColor('#344956' if self.focus else '#30373f'))
        painter.drawRoundedRect(QRectF(0, 0, self.WIDTH, self.HEIGHT), 7, 7)
        font = QFont(self.view.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor('#eef0f2'))
        title = QFontMetrics(font).elidedText(node.name or '(unnamed)', Qt.ElideRight, self.WIDTH-22)
        painter.drawText(QRectF(11, 8, self.WIDTH-22, 23), Qt.AlignVCenter, title)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor('#b1bec9'))
        painter.drawText(QRectF(11, 33, self.WIDTH-22, 18), Qt.AlignVCenter,
                         f'[{self.index}]  0x{node.id_hash:08X} : {node.ex_id}')
        links = sum(edge.kind != 'child' for edge in self.model.outgoing[self.index])
        painter.drawText(QRectF(11, 57, self.WIDTH-22, 18), Qt.AlignVCenter,
                         f'Actions {len(node.actions)}   Children {len(node.children)}   Links {links}')
        if self.model.issues[self.index]:
            painter.setPen(QColor('#f0ad65'))
            painter.drawText(QRectF(self.WIDTH-26, 8, 16, 23), Qt.AlignCenter, '!')

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.view.node_position_changed(self.index)
        return result

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.view.node_activated.emit(self.index)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class EdgeItem(QGraphicsPathItem):
    def __init__(self, edge, source, target, ordinal=0):
        super().__init__()
        self.edge, self.source, self.target, self.ordinal = edge, source, target, ordinal
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.hovered = False
        self.arrow = QPolygonF()
        description = EDGE_NAMES[edge.kind]+f' [{edge.position}]'
        if edge.condition & 0xFFFFFFFF != 0xFFFFFFFF:
            description += f'\nCondition reference: 0x{edge.condition & 0xFFFFFFFF:08X}'
        if edge.error:
            description += '\n'+edge.error
        self.setToolTip(description)
        self.route()

    def route(self):
        start = self.source.pos()+QPointF(NodeItem.WIDTH, 32+self.ordinal*7 % 36)
        end = self.target.pos()+QPointF(0, 42) if self.target is not None else start+QPointF(85, -40)
        path = QPainterPath(start)
        if self.target is self.source:
            end = self.source.pos()+QPointF(NodeItem.WIDTH, 66)
            path.cubicTo(start+QPointF(90, -45), end+QPointF(90, 45), end)
        elif end.x() <= start.x():
            top = min(start.y(), end.y())-65-self.ordinal*12
            path.cubicTo(start+QPointF(65, 0), QPointF(start.x()+65, top), QPointF((start.x()+end.x())/2, top))
            path.cubicTo(QPointF(end.x()-65, top), end-QPointF(65, 0), end)
        else:
            bend = max(40, (end.x()-start.x())*.45)
            path.cubicTo(start+QPointF(bend, 0), end-QPointF(bend, 0), end)
        self.setPath(path)
        before = path.pointAtPercent(.985)
        angle = math.atan2(end.y()-before.y(), end.x()-before.x())
        self.arrow = QPolygonF([end, end-QPointF(math.cos(angle-.5)*11, math.sin(angle-.5)*11),
                               end-QPointF(math.cos(angle+.5)*11, math.sin(angle+.5)*11)])
        self.update()

    def shape(self):
        stroke = QPainterPathStroker()
        stroke.setWidth(14)
        return stroke.createStroke(self.path())

    def boundingRect(self):
        return self.path().boundingRect().adjusted(-14, -14, 14, 14)

    def paint(self, painter, option, widget=None):
        color = QColor('#f2c775' if self.isSelected() else EDGE_COLORS[self.edge.kind])
        pen = QPen(color, 2.6 if self.isSelected() or self.hovered else 1.3)
        pen.setCosmetic(True)
        if self.edge.kind == 'child':
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(self.arrow)
        if self.target is None:
            painter.setPen(QColor('#efad6b'))
            painter.drawText(self.path().pointAtPercent(1)+QPointF(6, 0), '?')

    def hoverEnterEvent(self, event):
        self.hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.hovered = False
        self.update()
        super().hoverLeaveEvent(event)


class MotfsmGraphView(QGraphicsView):
    node_selected = Signal(int)
    node_activated = Signal(int)
    edge_selected = Signal(object)
    node_moved = Signal(int, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QBrush(QColor('#20262d')))
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self._building = False
        self.node_items = {}
        self.edge_items = []
        self._attached = {}
        self._focus = None
        self.scene().selectionChanged.connect(self._selection_changed)

    def clear_graph(self, message):
        self._building = True
        self.node_items, self.edge_items, self._attached = {}, [], {}
        self.scene().clear()
        text = self.scene().addText(message)
        text.setDefaultTextColor(QColor('#c4cbd2'))
        self.scene().setSceneRect(text.boundingRect().adjusted(-20, -20, 20, 20))
        self._focus = None
        self._building = False

    def set_graph(self, model, focus, mode, positions):
        self._building = True
        try:
            self.node_items = {}
            self.edge_items = []
            self._attached = {}
            self.scene().clear()
            self._focus = focus
            visible, edges = model.subgraph(focus, mode)
            if not isinstance(positions, dict):
                positions = {}
            columns = model.columns(focus, visible, edges, mode)
            grouped = {}
            for index in visible:
                grouped.setdefault(columns[index], []).append(index)
            defaults = {}
            x = 0
            for _, indexes in sorted(grouped.items()):
                for row, index in enumerate(indexes):
                    defaults[index] = (x+(row//16)*300, (row % 16)*115)
                x += max(1, math.ceil(len(indexes)/16))*300
            if mode == 'neighbors':
                height = min(16, max(map(len, grouped.values())))
                defaults[focus] = (defaults[focus][0], (height-1)*115/2)
            for index in visible:
                item = NodeItem(self, model, index, index == focus)
                self.scene().addItem(item)
                point = positions.get(model.identity(index), defaults[index])
                if not isinstance(point, (list, tuple)) or len(point) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in point):
                    point = defaults[index]
                item.setPos(*point)
                self.node_items[index] = item
            parallels = {}
            for edge in edges:
                pair = edge.source, edge.target
                ordinal = parallels.get(pair, 0)
                parallels[pair] = ordinal+1
                item = EdgeItem(edge, self.node_items[edge.source], self.node_items.get(edge.target), ordinal)
                self.scene().addItem(item)
                self.edge_items.append(item)
                for index in {edge.source, edge.target}-{None}:
                    self._attached.setdefault(index, []).append(item)
            self.scene().setSceneRect(self.scene().itemsBoundingRect().adjusted(-150, -150, 150, 150))
            self.resetTransform()
            self.scale(.9, .9)
            self.centerOn(self.node_items[focus])
            self.node_items[focus].setSelected(True)
        finally:
            self._building = False
        return len(visible), len(edges)

    def node_position_changed(self, index):
        if self._building or index not in self.node_items:
            return
        for edge in self._attached.get(index, ()):
            edge.route()
        item = self.node_items[index]
        self.scene().setSceneRect(self.sceneRect().united(item.sceneBoundingRect().adjusted(-120, -120, 120, 120)))
        self.node_moved.emit(index, item.pos().x(), item.pos().y())

    def _selection_changed(self):
        if self._building:
            return
        for item in self.scene().selectedItems():
            if isinstance(item, NodeItem):
                self.node_selected.emit(item.index)
                return
            if isinstance(item, EdgeItem):
                self.edge_selected.emit(item.edge)
                return

    def fit_graph(self):
        if self.node_items:
            self.fitInView(self.scene().itemsBoundingRect().adjusted(-30, -30, 30, 30), Qt.KeepAspectRatio)
            if self.transform().m11() > 1.3:
                self.resetTransform()
                self.scale(1.3, 1.3)
                self.centerOn(self.node_items[self._focus])

    def wheelEvent(self, event):
        factor = 1.18 ** (event.angleDelta().y()/120)
        scale = self.transform().m11()
        target = min(2.5, max(.035, scale*factor))
        self.scale(target/scale, target/scale)
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F:
            self.fit_graph()
            event.accept()
        else:
            super().keyPressEvent(event)
