"""See docs/internals/bhvt-editor.md, Navigator."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

lazy from ui.editor_widgets import editor_strip, styled_label, tool_button
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors
lazy from utils.hex_util import guid_le_to_str

from .. import container_edits

_EDITABLE = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
_CHECKED = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable


class ContainerPane(QSplitter):
    opened = Signal(object)

    def __init__(self, state, parent: QWidget | None = None):
        super().__init__(Qt.Orientation.Vertical, parent)
        self.state, self._filling = state, False
        self._layers: list[list[bytes]] = []
        self._reached: dict[tuple[int, int], tuple] = {}
        self.timelines = self._list(self.tr("Name"), self.tr("Frames"))
        self.holds = self._list(self.tr("Waits for"), self.tr("Reached by"))
        self.holds.setRootIsDecorated(True)
        self._pane(self.timelines, "", (
            tool_button("add", self.tr("Add a timeline"), self._add_timeline, text=self.tr("Timeline")),
            tool_button("edit", self.tr("Rename the selected timeline (F2)"), self._rename),
            tool_button("delete", self.tr("Remove the selected timeline"), self._remove_timeline),
            tool_button("external", self.tr("Open the selected timeline in the clip editor"), self._open)))
        self._pane(self.holds, self.tr("Hold points"), (
            tool_button("add", self.tr("Add a hold point: the layers it waits for go on together, once each has "
                                       "reached it"), self._add_hold, text=self.tr("Hold point")),
            tool_button("delete", self.tr("Remove the selected hold point"), self._remove_hold)))
        self.setStretchFactor(0, 2)
        self.setStretchFactor(1, 1)
        self._refill = QTimer(self)
        self._refill.setSingleShot(True)
        self._refill.timeout.connect(self.fill)
        QShortcut("F2", self.timelines, self._rename, context=Qt.ShortcutContext.WidgetShortcut)
        self.timelines.itemActivated.connect(self._activated)
        self.timelines.itemChanged.connect(self._renamed)
        self.holds.itemChanged.connect(self._targets_changed)
        self.holds.itemActivated.connect(self._reveal)

    def _list(self, *labels: str) -> QTreeWidget:
        rows = QTreeWidget(self)
        rows.setHeaderLabels(labels)
        rows.setRootIsDecorated(False)
        rows.setUniformRowHeights(True)
        rows.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        rows.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        return rows

    def _pane(self, rows: QTreeWidget, title: str, buttons: tuple) -> None:
        page = QWidget(self)
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        strip, row = editor_strip()
        if title:
            row.addWidget(styled_label(title, strip, heading="panel"))
        row.addStretch(1)
        for button in buttons:
            row.addWidget(button)
        column.addWidget(strip)
        column.addWidget(rows)
        self.addWidget(page)

    @property
    def fsm(self):
        return self.state.document.resource.model

    def fill(self) -> None:
        self._filling = True
        self._fill_timelines()
        self._fill_holds()
        self._filling = False

    def _fill_timelines(self) -> None:
        rows, fsm = self.timelines, self.fsm
        at = rows.indexOfTopLevelItem(rows.currentItem())
        rows.clear()
        for entry in fsm.timelines:
            QTreeWidgetItem(rows, [entry.name, f"{entry.timeline.total_frame:g}"]).setFlags(_EDITABLE)
        if fsm.timelines and at >= 0:
            rows.setCurrentItem(rows.topLevelItem(min(at, len(fsm.timelines) - 1)))

    def _fill_holds(self) -> None:
        rows, fsm, state = self.holds, self.fsm, self.state
        current = rows.currentItem()
        place = None if current is None else (
            rows.indexOfTopLevelItem(current.parent() or current),
            current.parent().indexOfChild(current) if current.parent() else -1)
        reaching = [(item, list(structure.naming(structure.graph["hold_point"])))
                    for item, structure in ((item, state.structure_of(item.tree)) for item in fsm.trees)]
        names, warning = state.holds, line_icon("warning", current_colors()["warning"])
        known = [item.layer_id for item in fsm.trees]
        rows.clear()
        self._layers, self._reached = [], {}
        for number, hold in enumerate(fsm.hold_points):
            top = QTreeWidgetItem(rows, [names[hold.hold_point_id], ""])
            top.setToolTip(0, guid_le_to_str(hold.hold_point_id))
            self._layers.append([*known, *(target for target in hold.targets if target not in known)])
            for at, (item, found) in enumerate(reaching):
                nodes = [node for node, value in found if value == hold.hold_point_id]
                row = QTreeWidgetItem(top, [item.name, ", ".join(node.name for node in nodes)])
                waits = item.layer_id in hold.targets
                row.setFlags(_CHECKED)
                row.setCheckState(0, Qt.CheckState.Checked if waits else Qt.CheckState.Unchecked)
                if nodes:
                    self._reached[number, at] = (nodes[0], item.tree)
                elif waits:
                    row.setIcon(1, warning)
                    row.setToolTip(1, self.tr("No action of this layer reaches the hold point: the layers that "
                                              "reach it wait"))
            for target in self._layers[-1][len(known):]:
                row = QTreeWidgetItem(top, [guid_le_to_str(target), self.tr("not a layer of this file")])
                row.setFlags(_CHECKED)
                row.setCheckState(0, Qt.CheckState.Checked)
                row.setIcon(1, warning)
        rows.expandAll()
        if place is not None and 0 <= place[0] < rows.topLevelItemCount():
            top = rows.topLevelItem(place[0])
            rows.setCurrentItem(top.child(place[1]) if 0 <= place[1] < top.childCount() else top)

    def _entry(self):
        at = self.timelines.indexOfTopLevelItem(self.timelines.currentItem())
        return self.fsm.timelines[at] if at >= 0 else None

    def _hold(self):
        current = self.holds.currentItem()
        at = -1 if current is None else self.holds.indexOfTopLevelItem(current.parent() or current)
        return self.fsm.hold_points[at] if at >= 0 else None

    def _add_timeline(self) -> None:
        self.state.attempt(container_edits.add_timeline, self.state.document)
        self.fill()
        self.timelines.setCurrentItem(self.timelines.topLevelItem(self.timelines.topLevelItemCount() - 1))

    def _rename(self) -> None:
        if (item := self.timelines.currentItem()) is not None:
            self.timelines.editItem(item, 0)

    def _renamed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._filling or column:
            return
        entry = self.fsm.timelines[self.timelines.indexOfTopLevelItem(item)]
        self.state.attempt(container_edits.rename, self.state.document, entry, item.text(0))
        self._refill.start()

    def _remove_timeline(self) -> None:
        if (entry := self._entry()) is not None:
            self.state.attempt(container_edits.remove_timeline, self.state.document, entry)
            self.fill()

    def _open(self) -> None:
        if (entry := self._entry()) is not None:
            self.opened.emit(entry.timeline)

    def _activated(self, item: QTreeWidgetItem) -> None:
        self.opened.emit(self.fsm.timelines[self.timelines.indexOfTopLevelItem(item)].timeline)

    def _add_hold(self) -> None:
        self.state.attempt(container_edits.add_hold_point, self.state.document)
        self.fill()
        self.holds.setCurrentItem(self.holds.topLevelItem(self.holds.topLevelItemCount() - 1))

    def _remove_hold(self) -> None:
        if (hold := self._hold()) is not None:
            self.state.attempt(container_edits.remove_hold_point, self.state.document, hold)
            self.fill()

    def _targets_changed(self, item: QTreeWidgetItem, column: int) -> None:
        top = item.parent()
        if self._filling or top is None:
            return
        at = self.holds.indexOfTopLevelItem(top)
        targets = [layer for row, layer in enumerate(self._layers[at])
                   if top.child(row).checkState(0) == Qt.CheckState.Checked]
        self.state.attempt(container_edits.set_hold_targets, self.state.document, self.fsm.hold_points[at], targets)
        self._refill.start()

    def _reveal(self, item: QTreeWidgetItem) -> None:
        top = item.parent()
        if top is not None and (found := self._reached.get((self.holds.indexOfTopLevelItem(top),
                                                             top.indexOfChild(item)))) is not None:
            self.state.reveal(*found)
