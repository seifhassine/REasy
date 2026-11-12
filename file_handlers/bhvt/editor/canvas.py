"""See docs/internals/bhvt-editor.md, Canvas."""

from __future__ import annotations

import math
from collections import Counter, deque
from functools import partial

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetricsF, QPainter, QPainterPath, QPainterPathStroker, QPen, QPolygonF
from PySide6.QtWidgets import (QApplication, QGraphicsItem, QGraphicsObject, QGraphicsPathItem, QGraphicsScene,
                               QLineEdit, QMenu)

from file_handlers.sdf.shader_graph_editor import _ShaderGraphView, connection_path, graph_palette, paint_node_card

from .. import clipboard, edits, graph_edits
from ..references import slot_base
from ..structure import Transition
from ..summaries import short_name
from .layout import state_layout, tree_layout
from .pickers import class_entries

_WIDTH, _HEADER, _ROW, _PORT, _SHOWN, _BUDGET = 210.0, 24.0, 16.0, 5.0, 6, 24
_LABEL, _SMALLEST, _CLEAR = 150.0, 0.75, 6.0
_BENDS, _RISES = (30.0, 60.0, 100.0), (36.0, 64.0, 100.0, 150.0, 220.0, 320.0, 460.0, 660.0)
_FLAG = QGraphicsItem.GraphicsItemFlag
_STROKER = QPainterPathStroker()
_STROKER.setWidth(4.0)


class _Box(QGraphicsObject):
    moved = Signal()

    def __init__(self, key, node, port: bool):
        super().__init__()
        self.key, self.node, self.port, self.hover = key, node, port, False
        self.width, self.height = _WIDTH, _HEADER
        self.setFlags(_FLAG.ItemIsMovable | _FLAG.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)

    def boundingRect(self) -> QRectF:
        return QRectF(-2, -2, self.width + _PORT + 4, self.height + 4)

    def frame(self) -> QRectF:
        return QRectF(self.pos().x(), self.pos().y(), self.width, self.height)

    def out(self) -> QPointF:
        return QPointF(self.width, _HEADER / 2)

    def at_port(self, point: QPointF) -> bool:
        return self.port and (self.mapFromScene(point) - self.out()).manhattanLength() <= 3 * _PORT

    def paint_port(self, painter: QPainter, palette: dict) -> None:
        if self.port and (self.hover or self.isSelected()):
            painter.setPen(QPen(palette["node_bg"], 1.0))
            painter.setBrush(palette["accent"])
            painter.drawEllipse(self.out(), _PORT, _PORT)

    def hoverEnterEvent(self, event) -> None:
        self.hover = True
        self.update()

    def hoverLeaveEvent(self, event) -> None:
        self.hover = False
        self.update()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.moved.emit()
        return super().itemChange(change, value)


class _Card(_Box):
    def __init__(self, node, port: bool):
        super().__init__(id(node), node, port)
        self.title, self.note, self.rows, self.entry, self.folded = "", "", [], False, None
        self.setFlag(_FLAG.ItemIsSelectable)

    def set_text(self, title: str, note: str, rows: list[str], entry: bool, folded: bool | None) -> None:
        self.prepareGeometryChange()
        self.title, self.note, self.rows, self.entry, self.folded = title, note, rows, entry, folded
        self.height = _HEADER + (_ROW * len(rows) + 6 if rows else 0)
        self.update()

    def toggle(self) -> QRectF:
        return QRectF(self.width - 18, (_HEADER - 10) / 2, 10, 10)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        palette, metrics = graph_palette(), painter.fontMetrics()
        paint_node_card(painter, QRectF(0, 0, self.width, self.height), _HEADER,
                        band=palette["accent"] if self.entry else None,
                        outline=QPen(palette["accent"], 2.0) if self.isSelected() else None)
        right = self.width - (26 if self.folded is not None else 8)
        note = metrics.horizontalAdvance(self.note) + 8 if self.note else 0
        painter.setPen(palette["text_bright"])
        painter.drawText(QRectF(8, 0, right - 8 - note, _HEADER), Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(self.title, Qt.TextElideMode.ElideRight, int(right - 8 - note)))
        painter.setPen(palette["text_muted"])
        painter.drawText(QRectF(8, 0, right - 8, _HEADER), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                         self.note)
        for row, text in enumerate(self.rows):
            painter.drawText(QRectF(8, _HEADER + 3 + _ROW * row, self.width - 16, _ROW),
                             Qt.AlignmentFlag.AlignVCenter,
                             metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(self.width - 16)))
        if self.folded is not None:
            box, middle = self.toggle(), self.toggle().center()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(box)
            painter.drawLine(QPointF(box.left() + 2, middle.y()), QPointF(box.right() - 2, middle.y()))
            if self.folded:
                painter.drawLine(QPointF(middle.x(), box.top() + 2), QPointF(middle.x(), box.bottom() - 2))
        self.paint_port(painter, palette)


class _Chip(_Box):
    def __init__(self, key, text: str, port: bool, node=None):
        super().__init__(key, node, port)
        self.text = text
        self.width = QFontMetricsF(QApplication.font()).horizontalAdvance(text) + 22

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        palette = graph_palette()
        pen = QPen(palette["node_border"], 1.0)
        pen.setStyle(Qt.PenStyle.SolidLine if self.node is None else Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(palette["node_header"] if self.node is None else palette["canvas_bg"])
        painter.drawRoundedRect(QRectF(0, 0, self.width, self.height), 4.0, 4.0)
        painter.setPen(palette["text_bright"] if self.node is None else palette["text_muted"])
        painter.drawText(QRectF(0, 0, self.width, self.height), Qt.AlignmentFlag.AlignCenter, self.text)
        self.paint_port(painter, palette)


def _edge_of(frame: QRectF, toward: QPointF) -> QPointF:
    center, reach = frame.center(), toward - frame.center()
    if reach.isNull() or frame.isEmpty():
        return center
    return center + reach * min(frame.width() / 2 / abs(reach.x()) if reach.x() else math.inf,
                                frame.height() / 2 / abs(reach.y()) if reach.y() else math.inf)


def _routes(start: QRectF, goal: QRectF, lane: int):
    across = goal.center() - start.center()
    length = math.hypot(across.x(), across.y()) or 1.0
    normal, base = QPointF(-across.y(), across.x()) / length, 14 + 16 * lane
    for offset in (base, *(side * (base + more) for more in _BENDS for side in (1, -1))):
        bend = start.center() + across / 2 + normal * offset
        first, tip = _edge_of(start, bend), _edge_of(goal, bend)
        path = QPainterPath(first)
        path.quadTo(bend, tip)
        aim = tip - bend
        yield path, tip, aim / (math.hypot(aim.x(), aim.y()) or 1.0), first * 0.25 + bend * 0.5 + tip * 0.25
    flat = abs(across.x()) >= abs(across.y())
    for rise in _RISES:
        for side in (-1, 1):
            if flat:
                ends = [QPointF(frame.center().x(), frame.top() if side < 0 else frame.bottom())
                        for frame in (start, goal)]
                level = (min if side < 0 else max)(end.y() for end in ends) + side * (rise + 14 * lane)
                bends, heading = [QPointF(end.x(), level) for end in ends], QPointF(0, -side)
            else:
                ends = [QPointF(frame.left() if side < 0 else frame.right(), frame.center().y())
                        for frame in (start, goal)]
                level = (min if side < 0 else max)(end.x() for end in ends) + side * (rise + 14 * lane)
                bends, heading = [QPointF(level, end.y()) for end in ends], QPointF(-side, 0)
            path = QPainterPath(ends[0])
            path.cubicTo(bends[0], bends[1], ends[1])
            yield path, ends[1], heading, (ends[0] + (bends[0] + bends[1]) * 3 + ends[1]) / 8


def _blocked(path: QPainterPath, frames: list[QRectF]) -> bool:
    bounds = path.controlPointRect()
    near = [frame for frame in frames if frame.intersects(bounds)]
    if not near:
        return False
    stroke = _STROKER.createStroke(path)
    return any(stroke.intersects(frame) for frame in near)


class _Edge(QGraphicsPathItem):
    def __init__(self, source: _Box, target: _Box | None, transition: Transition | None = None, lane: int = 0,
                 link: tuple | None = None):
        super().__init__()
        self.source, self.target, self.transition, self.lane, self.link = source, target, transition, lane, link
        self.around: list = []
        self.text, self.shown, self.label, self.tip, self.heading = "", "", QRectF(), QPointF(), QPointF(1, 0)
        self.setFlag(_FLAG.ItemIsSelectable, transition is not None)
        self.setZValue(-1)

    def place(self, free: QPointF | None = None) -> None:
        self.prepareGeometryChange()
        start, metrics = self.source.frame(), QFontMetricsF(QApplication.font())
        self.shown = metrics.elidedText(self.text, Qt.TextElideMode.ElideMiddle, _LABEL)
        self.setToolTip(self.text)
        size = QPointF(metrics.horizontalAdvance(self.shown) + 8, metrics.height() + 2) if self.text else QPointF()
        if self.link is not None:
            first = QPointF(start.right(), start.top() + _HEADER / 2)
            end = QPointF(self.target.frame().left(), self.target.frame().top() + _HEADER / 2)
            self.setPath(connection_path(first, end, 30.0))
            self.label = QRectF(end.x() - size.x() - 6, end.y() - size.y() - 1, size.x(), size.y())
            return
        if free is None and self.target is self.source:
            corner = start.topRight()
            path = QPainterPath(corner + QPointF(-46, 0))
            path.cubicTo(corner + QPointF(-46, -44 - 10 * self.lane), corner + QPointF(26 + 10 * self.lane, -44),
                         corner + QPointF(0, 14))
            self.tip, self.heading, middle = corner + QPointF(0, 14), QPointF(-0.8, 0.6), corner + QPointF(-10, -36)
        else:
            routes = _routes(start, QRectF(free, free) if free is not None else self.target.frame(), self.lane)
            route = next(routes)
            others = [] if free is not None else [frame for box, frame in self.around
                                                  if box is not self.source and box is not self.target]
            if others and _blocked(route[0], others):
                route = next((other for other in routes if not _blocked(other[0], others)), route)
            path, self.tip, self.heading, middle = route
        self.setPath(path)
        self.label = QRectF(middle.x() - size.x() / 2, middle.y() - size.y() / 2, size.x(), size.y())

    def tail(self) -> QPointF:
        return self.path().pointAtPercent(0)

    def boundingRect(self) -> QRectF:
        return self.path().boundingRect().united(self.label).adjusted(-12, -12, 12, 12)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(10.0)
        shape = stroker.createStroke(self.path())
        shape.addRect(self.label)
        return shape

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        palette = graph_palette()
        chosen = self.isSelected() or self.target is None
        color = palette["accent"] if chosen else palette["text_muted"]
        pen = QPen(color, 2.0 if chosen else 1.3)
        if self.transition is None and self.link is None and self.target is not None:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())
        if self.link is None:
            side = QPointF(-self.heading.y(), self.heading.x())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([self.tip, self.tip - self.heading * 9 + side * 4,
                                           self.tip - self.heading * 9 - side * 4]))
            for point in (self.tip, self.tail()) if self.isSelected() else ():
                painter.drawRect(QRectF(point.x() - 4, point.y() - 4, 8, 8))
        if self.text:
            painter.fillRect(self.label, palette["canvas_bg"])
            painter.setPen(palette["text_bright"] if chosen else palette["text_muted"])
            painter.drawText(self.label, Qt.AlignmentFlag.AlignCenter, self.shown)


class _NameEdit(QLineEdit):
    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.setText(self.placeholderText())
            self.editingFinished.emit()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.editingFinished.emit()


class GraphCanvas(_ShaderGraphView):
    link_requested = Signal()

    def __init__(self, state, popup, parent=None):
        super().__init__(parent)
        self.state, self.popup = state, popup
        self.places: dict = {}
        self.folded: set[int] = set()
        self.opened: set[int] = set()
        self.boxes: dict = {}
        self.edges: list[_Edge] = []
        self._open: set[int] = set()
        self._drag: tuple | None = None
        self._editor: _NameEdit | None = None
        self._renamed_node = None
        self._menu_at, self._fit, self._floor, self._syncing = QPointF(), True, _SMALLEST, False
        self._around: list = []
        self._routing = QTimer(self)
        self._routing.setSingleShot(True)
        self._routing.timeout.connect(self._route)
        self.setScene(QGraphicsScene(self))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.scene().selectionChanged.connect(self._picked)
        state.changed.connect(self._changed)
        state.scope_changed.connect(self._scoped)
        state.selection_changed.connect(self._select)
        self.rebuild()

    def _changed(self, reshaped: bool) -> None:
        if reshaped:
            self.rebuild()
            return
        structure = self.state.structure
        for box in self.boxes.values():
            if isinstance(box, _Card):
                box.set_text(*self._card_text(structure, box.node))
        for edge in self.edges:
            edge.text = self._label(structure, edge)
        self._route()

    def _route(self) -> None:
        self._around[:] = [(box, box.frame().adjusted(-_CLEAR, -_CLEAR, _CLEAR, _CLEAR))
                           for box in self.boxes.values()]
        for edge in self.edges:
            edge.place()

    def _scoped(self) -> None:
        self._fit, self._floor = True, _SMALLEST
        self.rebuild()

    def rebuild(self) -> None:
        scene = self.scene()
        scene.blockSignals(True)
        scene.clear()
        scene.blockSignals(False)
        self.boxes, self.edges, self._drag = {}, [], None
        structure, scope = self.state.structure, self.state.scope
        places = (self._states if structure.is_fsm(scope) else self._tree)(structure, scope)
        for key, box in self.boxes.items():
            box.setPos(QPointF(*self.places.get(key, places[key])))
            box.moved.connect(self._moved)
            scene.addItem(box)
        for edge in self.edges:
            edge.text, edge.around = self._label(structure, edge), self._around
            scene.addItem(edge)
        self._route()
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-600, -400, 600, 400))
        self._select()
        if self._fit:
            self._frame()

    def _card(self, structure, node, port: bool) -> _Card:
        self.boxes[id(node)] = card = _Card(node, port)
        card.set_text(*self._card_text(structure, node))
        return card

    def _card_text(self, structure, node) -> tuple[str, str, list[str], bool, bool | None]:
        summaries, scope = self.state.summaries, self.state.scope
        states, fsm, kids = structure.is_fsm(scope), structure.is_fsm(node), len(node.children)
        refs = structure.actions(node)
        rows = [self.tr("Missing object") if ref is None else summaries.object_text(*ref) for ref in refs[:_SHOWN]]
        if len(refs) > _SHOWN:
            rows[-1] = self.tr("+{0} more").format(len(refs) - _SHOWN + 1)
        selector = None if fsm else structure.selector(node)
        if selector is not None:
            rows.insert(0, short_name(summaries.type_name(*selector)))
        if vars(node).get("reference_tree"):
            rows.insert(0, node.reference_tree.rsplit("/", 1)[-1])
        if states:
            entry = node is structure.by_key.get(scope.default_start_child_id) if structure.hfsm else any(
                structure.target(Transition("start", scope, index)) is node
                for index in range(len(structure.rows("start", scope))))
        else:
            entry = node is scope
        folded = None if states or fsm or not kids else id(node) not in self._open
        return (node.name, self.tr("States: {0}").format(kids) if fsm and kids else str(kids) if folded else "", rows,
                entry, folded)

    def _label(self, structure, edge: _Edge) -> str:
        summaries, item = self.state.summaries, edge.transition
        if edge.link is not None:
            return summaries.condition_text(structure.child_condition(*edge.link))
        if item is None:
            return "" if edge.target is None else self.tr("default")
        text = summaries.condition_text(structure.condition(item), structure.condition_record(item))
        if not text and structure.hfsm:
            text = self.tr("never")
        return f"{item.index + 1}. {text}" if item.kind == "start" and len(
            structure.rows("start", item.owner)) > 1 else text

    def _states(self, structure, scope) -> dict:
        self._open = set()
        chips = {}
        for kind, text, tip in (
                ("start", self.tr("Entry"), self.tr("Where this state machine starts: a wire from here goes to a "
                                                    "state it can start in")),
                ("any", self.tr("Any state"), self.tr("Stands for every state of this machine: a wire from here is a "
                                                      "transition it can take whichever state it is in"))):
            if structure.hfsm or structure.holds(scope, structure.kinds[kind]["target"]):
                chips[kind] = self.boxes[kind, id(scope)] = _Chip((kind, id(scope)), text, True)
                chips[kind].setToolTip(tip)
        for child in structure.children(scope):
            self._card(structure, child, structure.is_fsm(child))
        lanes, pairs = Counter(), []
        for item in structure.transitions(scope):
            start = chips.get(item.kind) or self.boxes.get(id(structure.source(item)))
            target = structure.target(item)
            if start is None or target is None:
                continue
            key = ("portal", id(scope), id(target))
            end = self.boxes.get(id(target)) or self.boxes.get(key)
            if end is None:
                end = self.boxes[key] = _Chip(key, target.name, False, target)
            self.edges.append(_Edge(start, end, item, lanes[start.key, end.key]))
            lanes[start.key, end.key] += 1
            pairs.append((start.key, end.key))
        default = self.boxes.get(id(structure.by_key.get(scope.default_start_child_id))) if structure.hfsm else None
        if default is not None:
            self.edges.append(_Edge(chips["start"], default, None, lanes[chips["start"].key, default.key]))
            pairs.append((chips["start"].key, default.key))
        return state_layout({key: (box.width, box.height) for key, box in self.boxes.items()}, pairs,
                            [chip.key for chip in chips.values()])

    def _tree(self, structure, scope) -> dict:
        self._open, queue, children, count = set(), deque([scope]), {}, 1
        while queue:
            node = queue.popleft()
            kids = structure.children(node)
            if kids and not structure.is_fsm(node) and id(node) not in self.folded and (
                    node is scope or id(node) in self.opened or count + len(kids) <= _BUDGET):
                self._open.add(id(node))
                children[id(node)] = [id(kid) for kid in kids]
                count += len(kids)
                queue.extend(kids)
        self._card(structure, scope, False)
        stack = [scope]
        while stack:
            node = stack.pop()
            for index, kid in enumerate(structure.children(node) if id(node) in self._open else ()):
                self.edges.append(_Edge(self.boxes[id(node)], self._card(structure, kid, False), None, 0,
                                        (node, index)))
                stack.append(kid)
        return tree_layout({key: (box.width, box.height) for key, box in self.boxes.items()}, children, id(scope))

    def fit(self) -> None:
        self._fit, self._floor = True, 0.0
        self._frame()

    def _frame(self) -> None:
        bounds, view = self.scene().itemsBoundingRect(), self.maximumViewportSize()
        if not self.boxes or min(view.width(), view.height()) <= 60:
            return
        scale = min(1.0, (view.width() - 60) / bounds.width(), (view.height() - 60) / bounds.height())
        self.resetTransform()
        self.scale(max(scale, self._floor), max(scale, self._floor))
        chosen = next(iter(self.scene().selectedItems()), None)
        first = next(iter(self.boxes.values())).frame()
        self.centerOn(bounds.center() if scale >= self._floor else chosen.sceneBoundingRect().center()
                      if chosen is not None else
                      QPointF(first.left() - 40 + view.width() / self._floor / 2, first.center().y()))

    def arrange(self) -> None:
        for key in self.boxes:
            self.places.pop(key, None)
        self._fit, self._floor = True, _SMALLEST
        self.rebuild()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._fit:
            self._frame()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit:
            self._frame()

    def wheelEvent(self, event) -> None:
        self._fit = False
        super().wheelEvent(event)

    def _moved(self) -> None:
        box = self.sender()
        self.places[box.key] = (box.pos().x(), box.pos().y())
        for edge in self.edges:
            if edge.source is box or edge.target is box:
                edge.place()
        self._routing.start(150 if QApplication.mouseButtons() != Qt.MouseButton.NoButton else 0)

    def _picked(self) -> None:
        if not self._syncing:
            self.state.select([item.node if isinstance(item, _Card) else item.transition
                               for item in self.scene().selectedItems()])

    def _select(self) -> None:
        structure, chosen = self.state.structure, None
        above = {id(item) for node in self.state.nodes() if id(node) not in self.boxes and self.state.shows(node)
                 for item in structure.path(node)[:-1]}
        if above - self.opened or above & self.folded:
            self.folded -= above
            self.opened |= above
            self.rebuild()
            return
        self._syncing = True
        self.scene().clearSelection()
        for item in self.state.selection:
            found = next((edge for edge in self.edges if edge.transition == item), None) \
                if isinstance(item, Transition) else self.boxes.get(id(item))
            if found is not None:
                found.setSelected(True)
                chosen = found
        self._syncing = False
        if chosen is not None:
            self.ensureVisible(chosen, 40, 40)

    def _box_at(self, point: QPointF, kind=_Box):
        return next((item for item in self.scene().items(point) if isinstance(item, kind)), None)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._grab(self.mapToScene(event.position().toPoint())):
            super().mousePressEvent(event)

    def _grab(self, point: QPointF) -> bool:
        edge, box = next((edge for edge in self.edges if edge.isSelected()), None), self._box_at(point)
        if edge is not None and (edge.tip - point).manhattanLength() <= 9:
            self._drag = (graph_edits.set_target, edge, edge)
        elif edge is not None and isinstance(edge.source, _Card) and (edge.tail() - point).manhattanLength() <= 9:
            self._drag = (graph_edits.set_source, edge, edge)
        elif box is not None and box.at_port(point):
            wire = _Edge(box, None)
            self.scene().addItem(wire)
            wire.place(point)
            self._drag = (None, box, wire)
        elif isinstance(box, _Card) and box.folded is not None and box.toggle().contains(box.mapFromScene(point)):
            self._fold(box.node, not box.folded)
        else:
            return False
        return True

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            self._fit &= not event.buttons()
            super().mouseMoveEvent(event)
            return
        change, _source, wire = self._drag
        point = self.mapToScene(event.position().toPoint())
        if change is graph_edits.set_source:
            wire.prepareGeometryChange()
            path = QPainterPath(point)
            path.lineTo(wire.tip)
            wire.setPath(path)
        else:
            wire.place(point)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is None:
            super().mouseReleaseEvent(event)
            return
        (change, source, wire), self._drag = self._drag, None
        state, point = self.state, self.mapToScene(event.position().toPoint())
        card = self._box_at(point, _Card)
        if change is None:
            self.scene().removeItem(wire)
            if card is not None or self._box_at(point) is None:
                self._connect(source, card, point, event.globalPosition().toPoint())
            return
        wire.place()
        end = state.structure.target if change is graph_edits.set_target else state.structure.source
        if card is not None and card.node is not end(wire.transition):
            moved = state.attempt(change, state.document, state.model, wire.transition, card.node)
            state.select([moved or wire.transition])

    def _connect(self, source: _Box, card: _Card | None, point: QPointF, at) -> None:
        state = self.state
        transition = state.attempt(self._link, source, card, point)
        if transition is None:
            return
        state.select([transition])
        structure, model = state.structure, state.model
        slot = (structure.graph["objects"]["instance"] if structure.hfsm else
                structure.graph["indexes"][structure.kinds[transition.kind]["condition"]])[0]
        self.popup.pick(class_entries(model, slot, slot_base(model, slot)), at, self.tr("Condition: search classes"),
                        partial(graph_edits.set_condition, state.document, model, transition))

    def _link(self, source: _Box, card: _Card | None, point: QPointF) -> Transition:
        document, model, scope = self.state.document, self.state.model, self.state.scope
        with document.macro(self.tr("Add transition")):
            if card is None:
                node = graph_edits.add_child(document, model, scope)
                self.places[id(node)] = (point.x(), point.y())
            return graph_edits.add_transition(document, model, scope, source.node or source.key[0],
                                              node if card is None else card.node)

    def _fold(self, node, fold: bool) -> None:
        (self.folded.add if fold else self.folded.discard)(id(node))
        (self.opened.discard if fold else self.opened.add)(id(node))
        self.rebuild()

    def mouseDoubleClickEvent(self, event) -> None:
        point = self.mapToScene(event.position().toPoint())
        box = self._box_at(point)
        if box is None:
            self.add_child(point)
        elif isinstance(box, _Chip):
            if box.node is not None:
                self.state.reveal(box.node)
        elif box.node.children and box.node is not self.state.scope:
            self.state.show(box.node)
        else:
            self.rename(box.node)

    def add_child(self, point: QPointF | None = None, parent=None) -> None:
        state = self.state
        parent = parent or self._parent()
        node = state.attempt(graph_edits.add_child, state.document, state.model, parent)
        if node is not None:
            if point is not None and parent is state.scope and state.structure.is_fsm(parent):
                self.places[id(node)] = (point.x(), point.y())
            state.reveal(node)

    def rename(self, node) -> None:
        card = self.boxes.get(id(node))
        if card is None or self._editor is not None:
            return
        header = self.mapFromScene(QRectF(card.pos().x(), card.pos().y(), card.width, _HEADER)).boundingRect()
        self._editor = editor = _NameEdit(node.name, self.viewport())
        self._renamed_node = node
        editor.setPlaceholderText(node.name)
        editor.setGeometry(header.adjusted(0, 0, max(0, 140 - header.width()), max(0, 22 - header.height())))
        editor.selectAll()
        editor.show()
        editor.setFocus()
        editor.editingFinished.connect(self._renamed)

    def _renamed(self) -> None:
        editor, self._editor = self._editor, None
        if editor is None:
            return
        node, name = self._renamed_node, editor.text()
        editor.hide()
        editor.deleteLater()
        if name != node.name and "\0" not in name:
            edits.set_fields(self.state.document, node, self.tr("Rename"), name=name)

    def delete(self) -> None:
        state = self.state
        if state.nodes() or state.transitions():
            state.attempt(graph_edits.remove_items, state.document, state.model, state.nodes(), state.transitions())

    def set_entry(self) -> None:
        state = self.state
        node = state.nodes()[-1]
        state.attempt(graph_edits.set_entry, state.document, state.model, state.structure.parent[id(node)], node)

    def keyPressEvent(self, event) -> None:
        key, nodes = event.key(), self.state.nodes()
        if key == Qt.Key.Key_Escape and self._drag is not None:
            drag, self._drag = self._drag, None
            if drag[0] is None:
                self.scene().removeItem(drag[2])
            else:
                drag[2].place()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete()
        elif key == Qt.Key.Key_F2 and nodes:
            self.rename(nodes[-1])
        elif key == Qt.Key.Key_F:
            self.fit()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and nodes and nodes[-1].children:
            self.state.show(nodes[-1])
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        point = self.mapToScene(event.pos())
        box = self._box_at(point, _Card)
        if box is None:
            self.state.select([])
        elif not box.isSelected():
            self.state.select([box.node])
        self.show_menu(event.globalPos(), point)

    def show_menu(self, at, point: QPointF | None = None) -> None:
        state, self._menu_at = self.state, point
        structure, nodes = state.structure, state.nodes()
        node = nodes[-1] if nodes else None
        inside = node is not None and structure.is_state(node)
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        for text, slot, shown in (
                (self.tr("Open"), self._enter, node is not None and bool(node.children)),
                (self.tr("Rename"), self._rename, node is not None),
                (self.tr("Add child") if node is not None else self.tr("Add state here")
                 if structure.is_fsm(state.scope) else self.tr("Add node"), self._add_here, True),
                (self.tr("Add transition..."), self.link_requested, inside),
                (self.tr("Set as entry state"), self.set_entry, inside),
                (self.tr("Copy"), self.copy, node is not None),
                (self.tr("Paste"), self.paste, clipboard.accepts(state.model, "nodes")),
                (self.tr("Duplicate"), self.duplicate, node is not None and node is not structure.root),
                (self.tr("Delete"), self.delete, bool(state.transitions()) or
                 any(item is not structure.root for item in nodes)),
                (self.tr("Arrange"), self.arrange, point is not None), (self.tr("Fit"), self.fit, point is not None)):
            if shown:
                menu.addAction(text).triggered.connect(slot)
        menu.popup(at)

    def _enter(self) -> None:
        self.state.show(self.state.nodes()[-1])

    def _rename(self) -> None:
        self.rename(self.state.nodes()[-1])

    def _add_here(self) -> None:
        self.add_child(self._menu_at, self.state.nodes()[-1] if self.state.nodes() else None)

    def copy(self) -> None:
        if self.state.nodes():
            clipboard.copy_nodes(self.state.document, self.state.model, self.state.nodes())
            self.state.copied.emit()

    def _parent(self):
        state = self.state
        return state.scope if state.structure.is_fsm(state.scope) or not state.nodes() else state.nodes()[-1]

    def paste(self) -> None:
        state = self.state
        pasted = state.attempt(clipboard.paste_nodes, state.document, state.model, self._parent())
        if pasted:
            state.reveal(pasted[-1])
            state.select(pasted)

    def duplicate(self) -> None:
        state = self.state
        if state.nodes() and (pasted := state.attempt(clipboard.duplicate_nodes, state.document, state.model,
                                                      state.nodes())):
            state.select(pasted)
