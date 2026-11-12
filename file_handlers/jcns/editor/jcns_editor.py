"""See docs/internals/jcns-editor.md."""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

lazy from ui.editor_widgets import editor_strip, tool_button, toolbar_separator

from ui.curve_view import CurveView

from ..document import Table
from ..edits import JointEdits
from ..joints import JointView
from ..model import Record
from ..rules import Enums, version_game
from ..viewport import JcnsTool
from .curves import DriverCurves
from .joints import JointCards, JointTree
from .preview import JcnsPreview
from .records import JcnsInspector, JcnsOutline


class JcnsEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler, parent: QWidget | None = None):
        super().__init__(parent)
        self.handler = handler
        self.document = document = handler.document
        document.modified_changed.connect(self.modified_changed)
        game = handler.app.settings.get("game_version", "RE4") if handler.app is not None else "RE4"
        self.enums = Enums.of_game(version_game(document.model.version, game))
        self.edits = JointEdits(document, self.enums)
        self.view = JointView(document.model, self.enums)
        self.names: dict[int, str] = {}
        self.target: tuple | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar, row = editor_strip()
        self.tabs = QTabBar(bar)
        self.tabs.addTab(self.tr("Joints"))
        self.tabs.addTab(self.tr("Records"))
        row.addWidget(self.tabs)
        undo = tool_button("undo", self.tr("Undo (Ctrl+Z)"), document.undo_stack.undo)
        redo = tool_button("redo", self.tr("Redo (Ctrl+Y)"), document.undo_stack.redo)
        document.undo_stack.canUndoChanged.connect(undo.setEnabled)
        document.undo_stack.canRedoChanged.connect(redo.setEnabled)
        undo.setEnabled(False)
        redo.setEnabled(False)
        self.record_buttons = [toolbar_separator(),
                               tool_button("add", self.tr("Add a record to the selected table (Ins)"), self._add),
                               tool_button("copy", self.tr("Duplicate (Ctrl+D)"), self._duplicate),
                               tool_button("delete", self.tr("Delete (Del)"), self._delete)]
        for widget in (toolbar_separator(), undo, redo, *self.record_buttons):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(QLabel(self.tr("Version {0}").format(document.model.version), bar))
        layout.addWidget(bar)
        columns = QSplitter(Qt.Horizontal, self)
        self.left, self.right = QStackedWidget(columns), QStackedWidget(columns)
        self.joint_tree = JointTree(self.left)
        self.joint_tree.chosen.connect(self._show_target)
        self.left.addWidget(self.joint_tree)
        self.tree = QTreeView(self.left)
        self.tree.setModel(JcnsOutline(document, self.names, self.tree))
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.selectionModel().selectionChanged.connect(self._selected)
        self.tree.expand(self.tree.model().index(0, 0))
        self.left.addWidget(self.tree)
        columns.addWidget(self.left)
        self.center = QSplitter(Qt.Vertical, columns)
        self.preview = JcnsPreview(handler, self.center)
        self.preview.names_changed.connect(self._names_changed)
        self.driver_curves = DriverCurves(self.edits, self)
        self.curves = CurveView(self.driver_curves, self.center)
        self.center.setStretchFactor(0, 1)
        self.tool = JcnsTool(self.preview.viewport(), self)
        self.tool.picked.connect(lambda joint: self.joint_tree.select(("joint", joint)))
        self.preview.motions.preview.played.connect(self._live)
        self.preview.motions.playback.frame_changed.connect(self._live)
        columns.addWidget(self.center)
        self.cards = JointCards(self.edits, self.right)
        self.cards.pick_requested.connect(self.tool.arm)
        self.tool.picking.connect(self.cards.picking)
        self.right.addWidget(self.cards)
        self.inspector = JcnsInspector(self.edits, self.right)
        self.right.addWidget(self.inspector)
        columns.addWidget(self.right)
        columns.setStretchFactor(1, 1)
        columns.setSizes([300, 800, 360])
        layout.addWidget(columns, 1)
        self.tabs.currentChanged.connect(self._tab)
        # A macro's many changes refresh the views and the preview once.
        self._edited = QTimer(self)
        self._edited.setSingleShot(True)
        self._edited.timeout.connect(self._refresh)
        document.changed.connect(self._edited.start)
        for pane in (self.left, self.center, self.right):
            for keys, slot in ((QKeySequence.Undo, document.undo_stack.undo),
                               (QKeySequence.Redo, document.undo_stack.redo), ("Ctrl+D", self._duplicate)):
                QShortcut(keys, pane, slot, context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence.Delete, self.tree, self._delete, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence("Ins"), self.tree, self._add, context=Qt.WidgetShortcut)
        self._tab(0)
        self._show_joints()

    def joints(self) -> list[int]:
        rig = self.preview.rig()
        return sorted(self.view.joint_hashes() | set(() if rig is None else rig.hashes),
                      key=lambda joint: self.view.name(joint, self.names).casefold())

    def _tab(self, index: int) -> None:
        self.left.setCurrentIndex(index)
        self.right.setCurrentIndex(index)
        self.tool.arm(None)
        self.preview.viewport().set_edit_tool(self.tool if index == 0 else None)
        for widget in self.record_buttons:
            widget.setVisible(index == 1)

    def _show_joints(self) -> None:
        self.joint_tree.show_view(self.view, self.preview.rig(), self.names)

    def _show_target(self, target: tuple | None) -> None:
        self.cards.show_target(self.view, target, self.names, self.joints())
        self.driver_curves.show(self.view, target, fit=target != self.target)
        self.target = target
        data, chosen = self.view.data, target[1] if target and target[0] == "joint" else None
        links = [(joint, tuple(data["cards"][card.kind]["colour"]), weight)
                 for card in self.view.by_target.get(target, ()) for joint, weight in card.reads if joint is not None]
        links += [(listed[1], tuple(data["drives_colour"]), None) for card in self.view.readers.get(chosen, ())
                  for listed in card.targets if listed[0] == "joint"]
        self.tool.arm(None)
        self.tool.set_links(chosen, links, self.view.guides(target) if chosen is not None else ())

    def _live(self, *_args) -> None:
        poser, index, evaluation = self.preview.constrained() or (None, None, None)
        self.driver_curves.set_live(([], []) if evaluation is None else evaluation.program.driver_values)
        if evaluation is None:
            self.tool.set_pose((), None, self.names)
        else:
            self.tool.set_pose(poser.rigs[index].rig.hashes, poser.joint_worlds(index), self.names,
                               evaluation.program.guides)

    def _refresh(self) -> None:
        self.view = JointView(self.document.model, self.enums)
        self._show_joints()
        self.preview.refresh()

    def selected(self) -> list:
        model = self.tree.model()
        return [model.item(index) for index in self.tree.selectionModel().selectedRows()]

    def _selected(self, *_args) -> None:
        records = [item for item in self.selected() if isinstance(item, Record)]
        self.inspector.show_record(records[0] if len(records) == 1 else None)

    def _add(self) -> None:
        current = next(iter(self.selected()), None)
        table = current if isinstance(current, Table) else self.document.parent(current)
        if not isinstance(table, Table):
            return
        rows = table.owner.links.get(table.name) or []
        record = self.edits.insert(table, rows.index(current) + 1 if current in rows else len(rows))
        self._select(record)

    def _duplicate(self) -> None:
        records = [item for item in self.selected() if isinstance(item, Record) and item is not self.document.root]
        if len(records) == 1 and (copied := self.edits.duplicate(records[0])) is not None:
            self._select(copied)

    def _delete(self) -> None:
        records = [item for item in self.selected() if isinstance(item, Record) and item is not self.document.root]
        if records:
            self.edits.remove(records)

    def _select(self, item) -> None:
        index = self.tree.model().index_of(item)
        if index.isValid():
            self.tree.setCurrentIndex(index)
            self.tree.scrollTo(index)

    def _names_changed(self) -> None:
        if (names := self.preview.names()) == self.names:
            return
        self.names.clear()
        self.names.update(names)
        model = self.tree.model()
        with QSignalBlocker(self.tree.selectionModel()):
            model.reset(self.document.root)
        self.tree.expand(model.index(0, 0))
        self._show_joints()

    def cleanup(self) -> None:
        self.preview.cleanup()
