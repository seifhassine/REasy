"""See docs/internals/clip-editor.md."""

from __future__ import annotations

import copy
import math

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QPoint, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMenu,
    QScrollBar,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

lazy from ui.editor_widgets import editor_strip, icon_action, styled_label, tool_button, toolbar_separator
lazy from ui.icons import line_icon

from .. import edits
from ..document import ClipDocument
from ..model import Clip, Key, Node, Property, SpeedPoint, Timeline, Track
from ..rules import display_names, property_type
from .curves import CurveView
from .inspector import Inspector
from .lanes import LaneView
from .outline import OutlineModel
from .state import EditorState
from .transport import Transport


def _tenths(frames: float) -> int:
    return int(min(max(frames * 10.0, -2.0 ** 31), 2.0 ** 31 - 1)) if math.isfinite(frames) else 0


class ClipEditor(QWidget):
    selection_changed = Signal(list)
    current_frame_changed = Signal(float)
    modified_changed = Signal(bool)

    def __init__(self, document: ClipDocument, parent: QWidget | None = None):
        super().__init__(parent)
        self.document = document
        self.state = EditorState(document, self)
        self._clipboard: list[Key] = []
        self._build()
        self._shortcuts()
        self.state.selection_changed.connect(self._selection_changed)
        self.state.frame_changed.connect(self.current_frame_changed)
        self.state.view_changed.connect(self._sync_scrollbar)
        document.undo_stack.cleanChanged.connect(self._clean_changed)
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self._update_actions)
        document.changed.connect(self._document_changed)
        self._update_actions()

    def set_current_frame(self, frame: float) -> None:
        self.state.set_frame(frame)

    def selected_items(self) -> list:
        return list(self.state.selection)

    def select(self, items: list) -> None:
        self.state.select(items)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar, row = editor_strip()
        self.add_track_button = tool_button("add", self.tr("Add track"), self._add_track, text=self.tr("Track"))
        row.addWidget(self.add_track_button)
        self.add_clip_button = tool_button("crop", self.tr("Add clip to the track"), self._add_clip,
                                           text=self.tr("Clip"))
        row.addWidget(self.add_clip_button)
        self.add_node_button = self._menu_button("gameobject", self.tr("Node"), self.tr("Add node"), self._node_menu)
        row.addWidget(self.add_node_button)
        self.add_property_button = self._menu_button("variable", self.tr("Property"), self.tr("Add property"),
                                                     self._property_menu)
        row.addWidget(self.add_property_button)
        self.add_key_button = tool_button("key", self.tr("Add key at the playhead (K)"), self._add_key_at_playhead,
                                          text=self.tr("Key"))
        row.addWidget(self.add_key_button)
        row.addWidget(toolbar_separator())
        self.duplicate_button = tool_button("copy", self.tr("Duplicate (Ctrl+D)"), self._duplicate)
        row.addWidget(self.duplicate_button)
        self.delete_button = tool_button("delete", self.tr("Delete (Del)"), self._delete)
        row.addWidget(self.delete_button)
        row.addWidget(toolbar_separator())
        self.undo_button = tool_button("undo", self.tr("Undo (Ctrl+Z)"), self.document.undo_stack.undo)
        self.redo_button = tool_button("redo", self.tr("Redo (Ctrl+Y)"), self.document.undo_stack.redo)
        self.document.undo_stack.canUndoChanged.connect(self.undo_button.setEnabled)
        self.document.undo_stack.canRedoChanged.connect(self.redo_button.setEnabled)
        row.addWidget(self.undo_button)
        row.addWidget(self.redo_button)
        row.addWidget(toolbar_separator())
        self.curves_button = tool_button("graph", self.tr("Curves"), text=self.tr("Curves"))
        self.curves_button.setCheckable(True)
        self.curves_button.toggled.connect(lambda shown: self.views.setCurrentIndex(int(shown)))
        row.addWidget(self.curves_button)
        self.snap_button = tool_button("grid", self.tr("Snap to whole frames"))
        self.snap_button.setCheckable(True)
        self.snap_button.setChecked(True)
        self.snap_button.toggled.connect(lambda snap: setattr(self.state, "snap", snap))
        row.addWidget(self.snap_button)
        row.addWidget(tool_button("fit", self.tr("Frame all (F)"), self._fit))
        row.addStretch(1)
        self.issues_button = QToolButton()
        self.issues_button.setAutoRaise(True)
        self.issues_button.setIcon(line_icon("warning"))
        self.issues_button.clicked.connect(self._show_issue)
        row.addWidget(self.issues_button)
        self.version_label = styled_label("", tone="muted")
        row.addWidget(self.version_label)
        layout.addWidget(bar)

        self.tree = QTreeView()
        self.tree.setModel(OutlineModel(self.document, self.tree))
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        self.tree.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 220)
        self.tree.selectionModel().selectionChanged.connect(self._tree_selection)
        self.tree.model().modelReset.connect(self._restore_tree)
        self._expanded: list = []
        self.tree.expanded.connect(lambda index: self._expanded.append(self.tree.model().item(index)))
        self.tree.collapsed.connect(lambda index: self._forget_expanded(self.tree.model().item(index)))

        self.lanes = LaneView(self.state, self.tree)
        self.lanes.add_key_requested.connect(lambda prop, frame: self.state.select([edits.add_key(self.document,
                                                                                                    prop, frame)]))
        self.lanes.context_requested.connect(self._lane_menu)
        self.curves = CurveView(self.state)
        self.views = QStackedWidget()
        self.views.addWidget(self.lanes)
        self.views.addWidget(self.curves)
        self.time_bar = QScrollBar(Qt.Horizontal)
        self.time_bar.valueChanged.connect(lambda value: self.state.set_view(value / 10.0, self.state.view_scale))
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self.views, 1)
        right_layout.addWidget(self.time_bar)
        self.inspector = Inspector(self.state)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(right)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 700, 300])
        layout.addWidget(self.splitter, 1)
        self.transport = Transport(self.state)
        layout.addWidget(self.transport)
        self.add_clip_button.setVisible("clip" in self.document.rules.records)
        profile = self.document.profile
        self.version_label.setText(f"CLIP v{profile.version}"
                                   + ("" if profile.checked else " " + self.tr("(unchecked)")))

    def _menu_button(self, icon: str, text: str, tooltip: str, fill) -> QToolButton:
        button = tool_button(icon, tooltip, text=text)
        menu = QMenu(button)
        menu.aboutToShow.connect(lambda: fill(menu))
        button.setMenu(menu)
        button.setPopupMode(QToolButton.InstantPopup)
        return button

    def _shortcuts(self) -> None:
        stack = self.document.undo_stack
        editor = [
            (QKeySequence.Delete, self._delete), ("Ctrl+D", self._duplicate), (QKeySequence.Undo, stack.undo),
            (QKeySequence.Redo, stack.redo), ("Ctrl+Shift+Z", stack.redo),
            (QKeySequence.Copy, self._copy_keys), (QKeySequence.Paste, self._paste_keys),
            ("Alt+Up", lambda: self._move(-1)), ("Alt+Down", lambda: self._move(1)),
        ]
        views = [
            ("Space", self.transport.toggle), ("Home", lambda: self.state.set_frame(0.0)),
            ("End", self.transport.go_to_end), ("F", self._fit), ("K", self._add_key_at_playhead),
            ("Left", lambda: self._nudge(-1.0)), ("Right", lambda: self._nudge(1.0)),
        ]
        for parent, bindings in ((self, editor), (self.views, views)):
            for sequence, slot in bindings:
                shortcut = QShortcut(QKeySequence(sequence), parent)
                shortcut.setContext(Qt.WidgetWithChildrenShortcut)
                shortcut.activated.connect(slot)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.state.view_scale == 4.0 and self.lanes.width() > 100:
            self.lanes.fit()

    def _tree_selection(self, *_args) -> None:
        model = self.tree.model()
        items = [model.item(index) for index in self.tree.selectionModel().selectedRows()]
        keys = [item for item in self.state.selection if isinstance(item, (Key, SpeedPoint))]
        self.state.select(items if items else keys)

    def _selection_changed(self) -> None:
        model, selection = self.tree.model(), QItemSelection()
        for item in self.state.selection:
            if isinstance(item, (Track, Clip, Node, Property)):
                index = model.index_of(item)
                if index.isValid():
                    selection.select(index, index.siblingAtColumn(1))
        with QSignalBlocker(self.tree.selectionModel()):
            self.tree.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect)
        current = self.state.current
        if isinstance(current, (Key, SpeedPoint)):
            current = self.document.owner(current, Property)
        if isinstance(current, (Track, Clip, Node, Property)):
            index = model.index_of(current)
            if index.isValid():
                self.tree.scrollTo(index)
        self.tree.viewport().update()
        self._update_actions()
        self.selection_changed.emit(list(self.state.selection))

    def _forget_expanded(self, item) -> None:
        self._expanded = [held for held in self._expanded if held is not item]

    def _restore_tree(self) -> None:
        model = self.tree.model()
        expanded, self._expanded = self._expanded, []
        for item in expanded:
            index = model.index_of(item)
            if index.isValid():
                self.tree.expand(index)
        self._selection_changed()

    def _clean_changed(self, clean: bool) -> None:
        self.modified_changed.emit(not clean)

    def _document_changed(self, _change) -> None:
        self._refresh.start()

    def _update_actions(self) -> None:
        current = self.state.current
        issues = [issue for issue in self.document.issues() if issue.error]
        self.issues_button.setVisible(bool(issues))
        self.issues_button.setText(self.tr("{0} problems").format(len(issues)) if len(issues) != 1 else
                                   self.tr("1 problem"))
        self.issues_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        has_rows = isinstance(current, (Track, Clip, Node, Property, Key, SpeedPoint))
        self.delete_button.setEnabled(has_rows)
        self.duplicate_button.setEnabled(has_rows)
        self.add_key_button.setEnabled(bool(self._keyed_targets()))
        self.add_clip_button.setEnabled(isinstance(self._owner(Track), Track))

    def _show_issue(self) -> None:
        issues = [issue for issue in self.document.issues() if issue.error]
        if issues:
            target = issues[0].target
            self.state.select([target] if not isinstance(target, Timeline) else [])

    def _owner(self, kind: type):
        current = self.state.current
        if isinstance(current, kind):
            return current
        return self.document.owner(current, kind)

    def _keyed_targets(self) -> list[Property]:
        version = self.document.timeline.version
        props = []
        for item in self.state.selection:
            if isinstance(item, (Key, SpeedPoint)):
                item = self.document.owner(item, Property)
            if isinstance(item, Property) and property_type(item.type) is not None and \
                    property_type(item.type).owns_keys(version) and item not in props:
                props.append(item)
        return props

    def _add_track(self) -> None:
        self.state.select([edits.add_track(self.document)])

    def _add_clip(self) -> None:
        track = self._owner(Track)
        if isinstance(track, Track):
            self.state.select([edits.add_clip(self.document, track)])

    def _node_parent(self):
        current = self.state.current
        if isinstance(current, (Key, SpeedPoint, Property)):
            current = self.document.owner(current, Node)
        if isinstance(current, Node):
            return current
        if self.document.rules.clip_roots:
            clip = current if isinstance(current, Clip) else None
            if clip is None and isinstance(current, Track) and current.clips:
                clip = current.clips[0]
            return clip
        return current if isinstance(current, Track) else self.document.owner(current, Track)

    def _node_menu(self, menu: QMenu) -> None:
        menu.clear()
        parent = self._node_parent()
        for value, name in display_names("NodeType", self.document.timeline.version).items():
            action = menu.addAction(name)
            action.setEnabled(parent is not None)
            action.triggered.connect(lambda _checked=False, value=value, parent=parent:
                                     self.state.select([edits.add_node(self.document, parent, value)]))

    def _property_menu(self, menu: QMenu) -> None:
        menu.clear()
        current = self.state.current
        if isinstance(current, (Key, SpeedPoint)):
            current = self.document.owner(current, Property)
        info = property_type(current.type) if isinstance(current, Property) else None
        kinds = display_names("PropertyType", self.document.timeline.version)
        if info is not None and not info.owns_keys(self.document.timeline.version):
            if info.children == "members":
                for name, kind in edits.missing_members(current):
                    action = menu.addAction(f"{name} ({kinds.get(kind, kind)})")
                    action.triggered.connect(lambda _checked=False, name=name:
                                             self.state.select([edits.add_member(self.document, current, name)]))
                return
            if info.children == "elements":
                menu.addAction(self.tr("Element...")).triggered.connect(lambda: self._ask_property(current, True))
                return
            menu.addAction(self.tr("Field...")).triggered.connect(lambda: self._ask_property(current, False))
            return
        node = current if isinstance(current, Node) else self.document.owner(current, Node)
        action = menu.addAction(self.tr("Property..."))
        action.setEnabled(isinstance(node, Node))
        action.triggered.connect(lambda: self._ask_property(node, False))

    def _ask_property(self, owner: Node | Property, element: bool) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr("Add Property"))
        form = QFormLayout(dialog)
        name = QLineEdit()
        kinds = QComboBox()
        for value, label in display_names("PropertyType", self.document.timeline.version).items():
            kinds.addItem(label, value)
        if element:
            name.setEnabled(False)
            name.setPlaceholderText(self.tr("Named by its index"))
        else:
            form.addRow(self.tr("Name"), name)
        form.addRow(self.tr("Type"), kinds)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted or (not element and not name.text()):
            return
        if element:
            prop = edits.add_element(self.document, owner, kinds.currentData())
        else:
            prop = edits.add_property(self.document, owner, name.text(), kinds.currentData())
        self.state.select([prop])

    def _add_key_at_playhead(self) -> None:
        added = []
        for prop in self._keyed_targets():
            added.append(edits.add_key(self.document, prop, self.state.frame - self.document.time_offset(prop)))
        if added:
            self.state.select(added)

    def _delete(self) -> None:
        selection = list(self.state.selection)
        if selection:
            owners = [self.document.owner(item, Property) for item in self.state.keys()]
            edits.remove(self.document, selection)
            self.state.select(owners)

    def _duplicate(self) -> None:
        copies = edits.duplicate(self.document, list(self.state.selection))
        if copies:
            self.state.select(copies)

    def _move(self, delta: int) -> None:
        if isinstance(self.state.current, (Track, Clip, Node, Property)):
            edits.move(self.document, self.state.current, delta)

    def _nudge(self, delta: float) -> None:
        keys = self.state.keys()
        if keys:
            edits.shift(self.document, keys, delta)

    def _fit(self) -> None:
        self.lanes.fit()
        self.curves.fit_values()

    def _copy_keys(self) -> None:
        keys = [key for key in self.state.keys() if isinstance(key, Key)]
        if keys:
            base = min(key.frame for key in keys)
            self._clipboard = [copy.copy(key) for key in keys]
            for key in self._clipboard:
                key.frame -= base

    def _paste_keys(self) -> None:
        targets = self._keyed_targets()
        if self._clipboard and len(targets) == 1:
            prop = targets[0]
            pasted = edits.paste_keys(self.document, prop, self._clipboard,
                                      self.state.frame - self.document.time_offset(prop))
            if pasted:
                self.state.select(pasted)

    def _tree_menu(self, position: QPoint) -> None:
        index = self.tree.indexAt(position)
        if index.isValid() and self.tree.model().item(index) not in self.state.selection:
            self.state.select([self.tree.model().item(index)])
        self._item_menu(self.tree.viewport().mapToGlobal(position), None)

    def _lane_menu(self, hit, position: QPoint) -> None:
        frame = None
        if hit is not None:
            target = hit.item if hit.item is not None else hit.row_item
            if target not in self.state.selection:
                self.state.select([target])
            frame = self.state.snapped(self.lanes.frame_of(self.lanes.mapFromGlobal(position).x()))
        self._item_menu(position, frame)

    def _item_menu(self, position: QPoint, frame: float | None) -> None:
        menu = QMenu(self)
        targets = self._keyed_targets()
        if frame is not None and targets:
            prop = targets[0]
            icon_action(menu, "key", self.tr("Add Key Here")).triggered.connect(
                lambda: self.state.select([edits.add_key(self.document, prop,
                                                         frame - self.document.time_offset(prop))]))
        keys = self.state.keys()
        if keys:
            modes = menu.addMenu(self.tr("Interpolation"))
            for value, name in edits.interpolations_for(self.document, keys).items():
                modes.addAction(name).triggered.connect(
                    lambda _checked=False, value=value: edits.set_interpolation(self.document, keys, value))
        if len(targets) == 1:
            prop = targets[0]
            rules = self.document.rules
            slots = display_names("ExtraKeyType")
            free = [slot for slot in range(1 if rules.last_key_table else rules.extra_key_slots)
                    if slot not in prop.extra_keys]
            if free:
                extra = menu.addMenu(self.tr("Add Extra Key"))
                for slot in free:
                    extra.addAction(slots.get(slot, str(slot))).triggered.connect(
                        lambda _checked=False, slot=slot: self.state.select([edits.set_extra_key(self.document, prop,
                                                                                                  slot)]))
            if property_type(prop.type).speed_points:
                icon_action(menu, "clock", self.tr("Add Speed Point")).triggered.connect(
                    lambda: self.state.select([edits.add_speed_point(
                        self.document, prop,
                        (self.state.frame if frame is None else frame) - self.document.time_offset(prop))]))
        if self.state.selection:
            menu.addSeparator()
            icon_action(menu, "copy", self.tr("Duplicate")).triggered.connect(self._duplicate)
            icon_action(menu, "delete", self.tr("Delete"), danger=True).triggered.connect(self._delete)
        if not menu.isEmpty():
            menu.exec(position)

    def _sync_scrollbar(self) -> None:
        start, visible = self.state.view_start, self.lanes.width() / self.state.view_scale
        with QSignalBlocker(self.time_bar):
            self.time_bar.setRange(_tenths(min(0.0, start)),
                                   _tenths(max(self.document.timeline.total_frame - visible * 0.5, 0.0)))
            self.time_bar.setPageStep(_tenths(visible))
            self.time_bar.setValue(_tenths(start))
