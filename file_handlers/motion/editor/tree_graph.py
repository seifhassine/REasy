"""See docs/internals/motion-editor.md, Trees."""

from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPainterPathStroker, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsPathItem, QGraphicsScene, QMenu

from file_handlers.document_commands import Change
from file_handlers.sdf.shader_graph_editor import _ShaderGraphView, connection_path, graph_palette, paint_node_card

from .. import edits
from ..model import MotionTree, TreeLink, TreeNode, TreeParam
from ..rules import tree_classes
from .state import EditorState

_WIDTH, _HEADER, _ROW, _COLUMN, _GAP, _PORT = 210.0, 22.0, 17.0, 270.0, 24.0, 5.0
_MOTION, _PARAM = 1, 2  # via.motion.tree.NodeType and LinkType


class _Card(QGraphicsObject):
    def __init__(self, graph: TreeGraph, node: TreeNode, pins: list[tuple[int, int]],
                 links: dict[tuple[int, int], TreeLink]):
        super().__init__()
        self.graph, self.node, self.pins, self.links = graph, node, pins, links
        self.weight: float | None = None
        self.height = _HEADER + _ROW * (1 + len(pins)) + 4
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemSendsGeometryChanges)

    def boundingRect(self) -> QRectF:
        return QRectF(-_PORT, 0, _WIDTH + 2 * _PORT, self.height)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        palette, graph = graph_palette(), self.graph
        paint_node_card(painter, QRectF(0, 0, _WIDTH, self.height), _HEADER,
                        band=palette["accent"] if graph.tree.root is self.node else None,
                        outline=QPen(palette["accent"], 2.0) if self.isSelected() else None)
        if self.weight is not None:
            painter.fillRect(QRectF(0, _HEADER - 3, _WIDTH * min(max(self.weight, 0.0), 1.0), 3), palette["accent"])
        rows = [(palette["text_bright"], self.node.class_name, _HEADER),
                (palette["text_muted"], graph.caption(self.node), _ROW)]
        for kind, pin in self.pins:
            link = self.links.get((kind, pin))
            label = (graph.tr("Motion {0}") if kind == _MOTION else graph.tr("Param {0}")).format(pin)
            variable = None if link is None or link.child.class_name != "VariablesNode" else \
                graph.state.variables_of(link.child).get(link.guid)
            rows.append((palette["text_muted"], label if variable is None else f"{label}: {variable[0]}", _ROW))
        top, metrics = 0.0, painter.fontMetrics()
        for color, text, height in rows:
            painter.setPen(color)
            painter.drawText(QRectF(10, top, _WIDTH - 20, height), Qt.AlignVCenter | Qt.AlignLeft,
                             metrics.elidedText(text, Qt.ElideRight, int(_WIDTH - 20)))
            top += height
        painter.setPen(QPen(palette["node_bg"], 1.0))
        for row, (kind, _pin) in enumerate(self.pins):
            painter.setBrush(palette["accent"] if kind == _MOTION else palette["text_muted"])
            painter.drawEllipse(QPointF(0, _HEADER + _ROW * (1.5 + row)), _PORT, _PORT)
        painter.setBrush(palette["accent"] if self.node.node_type != _PARAM else palette["text_muted"])
        painter.drawEllipse(QPointF(_WIDTH, _HEADER / 2), _PORT, _PORT)

    def output(self) -> QPointF:
        return self.mapToScene(QPointF(_WIDTH, _HEADER / 2))

    def pin_point(self, kind: int, pin: int) -> QPointF:
        return self.mapToScene(QPointF(0, _HEADER + _ROW * (1.5 + self.pins.index((kind, pin)))))

    def pin_at(self, point: QPointF) -> tuple[int, int] | None:
        local = self.mapFromScene(point)
        row = int((local.y() - _HEADER) // _ROW) - 1
        return self.pins[row] if 0 <= row < len(self.pins) and -3 * _PORT <= local.x() <= _WIDTH * 0.6 else None

    def at_output(self, point: QPointF) -> bool:
        return (point - self.output()).manhattanLength() <= 3 * _PORT

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.graph.moved(self)
        return super().itemChange(change, value)


class _Wire(QGraphicsPathItem):
    def __init__(self, link: TreeLink | None):
        super().__init__()
        self.link = link
        self.setPen(QPen(Qt.black, 3.0))
        self.setFlag(QGraphicsItem.ItemIsSelectable, link is not None)
        self.setZValue(-1)

    def place(self, start: QPointF, end: QPointF) -> None:
        self.setPath(connection_path(start, end, 40.0))

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(8.0)
        return stroker.createStroke(self.path())

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        palette = graph_palette()
        pen = QPen(palette["accent"] if self.isSelected() or self.link is None else palette["text_muted"],
                   2.2 if self.isSelected() else 1.4)
        pen.setStyle(Qt.DashLine if self.link is not None and self.link.type == _PARAM else Qt.SolidLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())


class TreeGraph(_ShaderGraphView):
    picked = Signal(list)

    def __init__(self, state: EditorState, parent=None):
        super().__init__(parent)
        self.state, self.tree = state, None
        self.cards: dict[int, _Card] = {}
        self.wires: dict[int, list[_Wire]] = defaultdict(list)
        self.places: dict[int, QPointF] = {}
        self._runtime = None
        self._drag: tuple[_Card, _Wire] | None = None
        self._fit = False
        # Rebuilt once an edit's changes are all in (a macro's middle can hold links to a removed node).
        self._stale = QTimer(self)
        self._stale.setSingleShot(True)
        self._stale.timeout.connect(self._rebuild)
        self.setScene(QGraphicsScene(self))
        self.scene().selectionChanged.connect(self._selected)
        state.document.changed.connect(self._changed)

    def show_tree(self, tree: MotionTree | None) -> None:
        if tree is not self.tree:
            self.tree, self._fit = tree, True
            self._stale.stop()
            self._rebuild()
            self._fit_shown()

    def show_weights(self, runtime) -> None:
        self._runtime = runtime
        for card in self.cards.values():
            card.weight = None if runtime is None else getattr(runtime.nodes.get(id(card.node)), "weight", None)
        self.scene().update()

    def caption(self, node: TreeNode) -> str:
        motion = None if self._runtime is None else getattr(self._runtime.nodes.get(id(node)), "motion", None)
        return node.name or (motion.name if motion is not None else "")

    def _fit_shown(self) -> None:
        if self._fit and self.isVisible() and self.viewport().width() > 1:
            self._fit = False
            self.fit_scene()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_shown()

    def _changed(self, change: Change) -> None:
        if self.tree is not None and self.state.tree_of(change.target) is self.tree:
            if isinstance(change.target, TreeParam):
                self.scene().update()
            else:
                self._stale.start()

    def _rebuild(self) -> None:
        scene, tree = self.scene(), self.tree
        scene.blockSignals(True)
        scene.clear()
        scene.blockSignals(False)
        self.cards, self.wires = {}, defaultdict(list)
        if tree is None:
            return
        classes, used = tree_classes(tree.version), defaultdict(dict)
        for link in tree.links:
            used[id(link.parent)][link.type, link.pin] = link
        for node in tree.nodes:
            spec = classes.get(node.class_name)
            counts = list(spec["pins"]) if spec else [int(node.node_type != _PARAM), 1]
            for kind, pin in used[id(node)]:
                counts[kind - 1] = max(counts[kind - 1], pin + 1)
            pins = [(kind, pin) for kind in (_MOTION, _PARAM) for pin in range(counts[kind - 1])]
            self.cards[id(node)] = _Card(self, node, pins, used[id(node)])
        self._lay_out(tree)
        for node in tree.nodes:
            card = self.cards[id(node)]
            card.setPos(self.places[id(node)])
            scene.addItem(card)
        for link in tree.links:
            wire = _Wire(link)
            scene.addItem(wire)
            self.wires[id(link.parent)].append(wire)
            self.wires[id(link.child)].append(wire)
            self._place(wire)
        self.show_weights(self._runtime)
        self.select(self.state.selection)

    def _lay_out(self, tree: MotionTree) -> None:
        children = defaultdict(list)
        for link in sorted(tree.links, key=lambda link: (link.type, link.pin)):
            children[id(link.parent)].append(link.child)
        bottom, found = defaultdict(float), {}
        stack = [] if tree.root is None else [(tree.root, 0, 0.0)]
        while stack:
            node, level, top = stack.pop()
            if id(node) not in found:
                found[id(node)] = place = QPointF(-level * _COLUMN, max(bottom[level], top))
                bottom[level] = place.y() + self.cards[id(node)].height + _GAP
                stack.extend((child, level + 1, place.y()) for child in reversed(children[id(node)]))
        level = max(bottom, default=-1) + 1
        for node in tree.nodes:
            if id(node) not in found:
                found[id(node)] = QPointF(-level * _COLUMN, bottom[level])
                bottom[level] += self.cards[id(node)].height + _GAP
        for key, place in found.items():
            self.places.setdefault(key, place)

    def _place(self, wire: _Wire) -> None:
        link = wire.link
        wire.place(self.cards[id(link.child)].output(), self.cards[id(link.parent)].pin_point(link.type, link.pin))

    def moved(self, card: _Card) -> None:
        self.places[id(card.node)] = card.pos()
        for wire in self.wires.get(id(card.node), ()):
            self._place(wire)

    def _selected(self) -> None:
        self.picked.emit([item.node if isinstance(item, _Card) else item.link for item in self.scene().selectedItems()
                          if isinstance(item, (_Card, _Wire))])

    def select(self, items: list) -> None:
        scene, chosen = self.scene(), None
        by_link = {id(wire.link): wire for wires in self.wires.values() for wire in wires}
        scene.blockSignals(True)
        scene.clearSelection()
        for item in items:
            item = self.state.document.parent(item) if isinstance(item, TreeParam) else item
            if (found := self.cards.get(id(item)) or by_link.get(id(item))) is not None:
                found.setSelected(True)
                chosen = found
        scene.blockSignals(False)
        if chosen is not None:
            self.ensureVisible(chosen, 40, 40)

    def _card_at(self, point: QPointF, skip: _Card | None = None) -> _Card | None:
        return next((item for item in self.scene().items(point) if isinstance(item, _Card) and item is not skip), None)

    def mousePressEvent(self, event) -> None:
        point = self.mapToScene(event.position().toPoint())
        card = self._card_at(point)
        if event.button() == Qt.LeftButton and card is not None and card.at_output(point):
            wire = _Wire(None)
            self.scene().addItem(wire)
            wire.place(card.output(), point)
            self._drag = card, wire
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag is not None:
            self._drag[1].place(self._drag[0].output(), self.mapToScene(event.position().toPoint()))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is None:
            super().mouseReleaseEvent(event)
            return
        (child, wire), self._drag = self._drag, None
        self.scene().removeItem(wire)
        point = self.mapToScene(event.position().toPoint())
        parent = self._card_at(point, child)
        found = None if parent is None else parent.pin_at(point)
        kind = _PARAM if child.node.node_type == _PARAM else _MOTION
        if found is not None and found[0] == kind:
            self._connect(parent.node, found[1], child.node, kind, event.globalPosition().toPoint())

    def _connect(self, parent: TreeNode, pin: int, child: TreeNode, kind: int, at) -> None:
        guid = None
        if child.class_name == "VariablesNode":
            variables, menu = list(self.state.variables_of(child).items()), QMenu(self)
            for index, (_key, (name, _value)) in enumerate(variables):
                menu.addAction(name).setData(index)
            chosen = menu.exec(at) if variables else None
            if variables and chosen is None:
                return
            guid = bytes(16) if chosen is None else variables[chosen.data()][0]
        self.picked.emit([edits.connect(self.state.document, self.tree, parent, pin, child, kind, guid)])

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.scene().selectedItems():
            self._delete()
            return
        if event.key() == Qt.Key_F:
            self.fit_scene()
            return
        super().keyPressEvent(event)

    def _delete(self) -> None:
        edits.delete(self.state.document, [item.node if isinstance(item, _Card) else item.link
                                           for item in self.scene().selectedItems()
                                           if isinstance(item, (_Card, _Wire))])

    def contextMenuEvent(self, event) -> None:
        if self.tree is None:
            return
        point, menu = self.mapToScene(event.pos()), QMenu(self)
        card = self._card_at(point)
        if card is not None:
            if not card.isSelected():
                self.select([card.node])
                self._selected()
            root = menu.addAction(self.tr("Make Root"), lambda: edits.set_fields(
                self.state.document, self.tree, self.tr("Make Root"), root=card.node))
            root.setEnabled(self.tree.root is not card.node)
        else:
            groups = {_MOTION: menu.addMenu(self.tr("Add Motion Node")),
                      _PARAM: menu.addMenu(self.tr("Add Parameter Node"))}
            for name, spec in sorted(tree_classes(self.tree.version).items()):
                groups.get(spec["type"], groups[_MOTION]).addAction(name, lambda name=name: self.add(name, point))
        if self.scene().selectedItems():
            menu.addAction(self.tr("Delete"), self._delete)
        menu.addAction(self.tr("Fit (F)"), self.fit_scene)
        menu.exec(event.globalPos())

    def add(self, class_name: str, point: QPointF) -> TreeNode:
        node = edits.add_node(self.state.document, self.tree, class_name)
        self.places[id(node)] = point
        self.picked.emit([node])
        return node
