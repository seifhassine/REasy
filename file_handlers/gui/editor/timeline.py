"""See docs/internals/gui-editor.md, Timeline."""

from __future__ import annotations

import copy
from dataclasses import replace
from functools import partial

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QDoubleSpinBox, QFormLayout, QSplitter, QStackedWidget, QTreeView,
                               QVBoxLayout, QWidget)

lazy from file_handlers.clip import edits as clip_edits
lazy from file_handlers.clip.rules import PropertyType, display_names, interpolation, property_type
lazy from ui.editor_widgets import EmbeddedPopupComboBox, editor_strip, styled_label, tool_button, toolbar_separator
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, qcolor, set_style_role

from file_handlers.clip.editor.curves import ClipCurves, PropertyCurves
from file_handlers.clip.editor.lanes import LaneView
from file_handlers.clip.editor.state import EditorState as KeyState
from file_handlers.clip.model import Key, Node, Property, walk_properties
from ui.outline_model import OutlineModel
from ui.search_popup import below

from .. import state_edits
from ..edits import checked
from ..errors import GuiEditError
from ..rules import gui_layout
from ..storage import nodes
from .navigator import short
from .pickers import picker
from .values import EnumEdit, NumberEdit, VectorEdit, value_edit


def _frames(items: list):
    for item in items:
        if isinstance(item, Key):
            yield item.frame
        else:
            for track in walk_properties(item.properties if isinstance(item, Node) else [item]):
                yield from (key.frame for key in track.keys)


class Tracks(OutlineModel):
    def __init__(self, view: StateView):
        super().__init__(view.animation, view)
        self.view = view
        view.clip.changed.connect(self._changed)

    def children_of(self, item) -> list:
        if item is self.view.animation:
            return [node for node in nodes(item) if node.properties]
        return item.properties if isinstance(item, Node) else item.children

    def owner_of(self, item):
        found = self.view.clip.parent(item)
        return None if found is None else self.view.animation if isinstance(item, Node) else found[0]

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return (self.tr("Track"), self.tr("Type"))[section]
        return None

    def display(self, item, row: int, column: int, role: int):
        if role == Qt.ItemDataRole.DisplayRole:
            return item.name if column == 0 else self.view.kind(item)
        if role == Qt.ItemDataRole.DecorationRole and column == 0:
            return line_icon("box" if isinstance(item, Node) else "list" if item.children else "variable")
        if role == Qt.ItemDataRole.ForegroundRole and column == 1:
            return qcolor(current_colors()["text_muted"])
        return None

    def _changed(self, change) -> None:
        if change.structural:
            self.reset(self.view.animation)


class Lanes(LaneView):
    def __init__(self, view: StateView):
        super().__init__(view.keys, view.tree, view)
        self.view = view

    def hit(self, pos):
        found = super().hit(pos)
        return replace(found, part="body") if found is not None and found.part in ("start", "end") else found

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        if not self.view.editable and self._drag is not None and "targets" in self._drag:
            self._drag["targets"] = []

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        drag = self._drag
        if drag is not None and drag.get("targets") and (low := min(_frames(drag["targets"]), default=0.0)) < 0.0:
            clip_edits.shift(self.state.document, drag["targets"], -low, drag["merge"])
            drag["applied"] -= low

    def mouseDoubleClickEvent(self, event) -> None:
        if self.view.editable:
            super().mouseDoubleClickEvent(event)


class Curves(PropertyCurves):
    def __init__(self, view: StateView):
        super().__init__(view.keys)
        self.view = view

    def value(self, prop: Property, number: float):
        return checked(self.view.state.document, PropertyType(prop.type), super().value(prop, number), prop.name)

    def move_keys(self, track: int, keys: list[int], frames, values, dx: float, dy: float, component: int,
                  merge) -> None:
        if self.view.editable:
            self.view.state.attempt(PropertyCurves.move_keys, self, track, keys, frames, values,
                                    max(dx, -min(float(frames[at]) for at in keys)), dy, component, merge)

    def move_handle(self, track: int, at: int, handle: int, x: float, y: float, merge) -> None:
        if self.view.editable:
            super().move_handle(track, at, handle, x, y, merge)

    def add_key(self, track: int, x: float) -> None:
        if self.view.editable:
            self.view.add_keys([self.properties[track]], self.state.snapped(x))

    def delete_keys(self, picked: dict[int, list[int]]) -> None:
        view = self.view
        if view.editable:
            view.state.attempt(state_edits.remove_items, view.state.document, view.animation,
                               [self.properties[track].keys[at] for track, keys in picked.items() for at in keys])


class KeyPane(QWidget):
    def __init__(self, view: StateView):
        super().__init__(view)
        self.view = view
        self._signature, self.rows, self.body = None, [], None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        set_style_role(self, surface="view")
        self.setMinimumWidth(210)
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.column.setSpacing(0)
        self.title = styled_label("", self, heading="panel")
        self.title.setContentsMargins(8, 6, 8, 2)
        self.column.addWidget(self.title)
        self.column.addStretch(1)
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self.sync)
        view.clip.changed.connect(self._schedule)
        self.sync()

    def _schedule(self, _change) -> None:
        self._refresh.start()

    def _mode(self, keys: list[Key], names: dict[int, str]) -> str:
        found = {key.interpolation for key in keys}
        return names[next(iter(found))] if len(found) == 1 else self.tr("(mixed)")

    def _set_mode(self, keys: list[Key], names: dict[int, str], name: str) -> None:
        state, code = self.view.state, next(code for code, held in names.items() if held == name)
        state.attempt(state_edits.set_interpolation, state.document, self.view.animation, keys, code)

    def _add(self, form: QFormLayout, label: str, edit: QWidget, tip: str = "") -> None:
        edit.setEnabled(self.view.editable)
        edit.setToolTip(tip)
        form.addRow(label, edit)
        self.rows.append(edit)

    def sync(self) -> None:
        view = self.view
        keys = [item for item in view.keys.selection if isinstance(item, Key)]
        one = keys[0] if len(keys) == 1 else None
        mode = interpolation(one.interpolation) if one is not None else None
        curved = one is not None and mode is not None and bool(mode.control) and one.control is not None \
            and len(one.control) == 4
        names = state_edits.interpolations(view.state.document, view.animation, keys) if keys else {}
        signature = (tuple(map(id, keys)), curved, tuple(names))
        if signature == self._signature:
            for row in self.rows:
                row.refresh()
            return
        if self.body is not None:
            for row in self.rows:
                row.live = False
            self.body.hide()
            self.body.deleteLater()
        self._signature, self.rows = signature, []
        self.title.setText(self.tr("Key") if one is not None else self.tr("{0} keys").format(len(keys)) if keys
                           else self.tr("No key selected"))
        self.body = body = QWidget(self)
        form = QFormLayout(body)
        form.setContentsMargins(8, 4, 8, 8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.column.insertWidget(1, body)
        if not keys:
            hint = styled_label(self.tr("Select a key to edit its frame, value and the way the value moves to the "
                                        "next key. Double-click a track's lane to add a key."), body, tone="muted")
            hint.setWordWrap(True)
            form.addRow(hint)
            return
        if one is not None:
            track = view.clip.owner(one, Property)
            target, field = view.target(track)
            whole = field is not None and int(field.type) == track.type
            self._add(form, self.tr("Frame"), NumberEdit(False, partial(getattr, one, "frame"),
                                                         partial(view.set_frame, one), body))
            self._add(form, self.tr("Value"), value_edit(
                gui_layout(view.state.document.gui.version), PropertyType(track.type),
                view.state.labels(view.resource, target, field) if whole else (), partial(getattr, one, "value"),
                partial(view.set_value, track, one), body,
                " ".join(f".{name}" for name in field.asset) if whole else "", whole and field.lines,
                self.tr("(first state)"), picker(view.state, view.popup, target.type_name, field, partial(
                    state_edits.set_key_value, view.state.document, view.animation, track, one)) if whole else None))
        self._add(form, self.tr("Interpolation"), EnumEdit(
            tuple(names.values()), partial(self._mode, keys, names), partial(self._set_mode, keys, names), body),
            self.tr("How the value moves to the next key: Discrete holds it, Linear moves evenly, Hermite follows "
                    "the curve its tangents set, Event applies the key once when the playhead reaches it"))
        if curved:
            for at, label, tip in ((0, self.tr("Out tangent"), self.tr("The curve's direction as it leaves this key")),
                                   (2, self.tr("In tangent"), self.tr("The curve's direction as it reaches the next "
                                                                      "key"))):
                self._add(form, label, VectorEdit(["x", "y"], False, partial(view.control, one, at),
                                                  partial(view.set_control, one, at), body), tip)


class StateView(QSplitter):
    changed = Signal()

    def __init__(self, state, animation, held, editable: bool, parent: Timeline):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.state, self.animation, self.editable, self.popup = state, animation, editable, parent.popup
        self.symbol, self.resource, self._fitted = held.symbol, held.resource, False
        self.clip = state.document.clip(animation)
        self.keys = KeyState(self.clip, self)
        self.tree = QTreeView(self)
        self.tracks = Tracks(self)
        self.tree.setModel(self.tracks)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 170)
        self.lanes = Lanes(self)
        self.curves = ClipCurves(self.keys, self, Curves(self))
        self.views = QStackedWidget(self)
        self.views.addWidget(self.lanes)
        self.views.addWidget(self.curves)
        self.pane = KeyPane(self)
        for widget in (self.tree, self.views, self.pane):
            self.addWidget(widget)
        self.setStretchFactor(1, 1)
        self.setSizes([250, 600, 250])
        self.tree.expandAll()
        self.tracks.modelReset.connect(self._reset)
        self.tree.selectionModel().selectionChanged.connect(self._tree_selected)
        self.keys.selection_changed.connect(self._selected)
        self.keys.frame_changed.connect(state.set_frame)
        self.lanes.add_key_requested.connect(self._key_requested)
        state.frame_changed.connect(self._frame)
        self._frame()

    def kind(self, item) -> str:
        if isinstance(item, Node):
            held = next((obj for obj in self.symbol.objects if obj.name == item.name), None)
            return short(held.type_name) if held is not None else ""
        return display_names("PropertyType", self.clip.timeline.version).get(item.type, f"0x{item.type:X}")

    def target(self, track: Property) -> tuple:
        owner = self.clip.parent(track)[0]
        top = track if isinstance(owner, Node) else owner
        name = self.clip.parent(top)[0].name
        held = next((obj for obj in self.symbol.objects if obj.name == name), None)
        return held, held and self.state.document.authoring.field(held.type_name, top.name)

    def leaves(self, items: list) -> list[Property]:
        found = []
        for item in items:
            if isinstance(item, Key):
                item = self.clip.owner(item, Property)
            for track in walk_properties(item.properties if isinstance(item, Node) else [item]):
                if not property_type(track.type).members and track not in found:
                    found.append(track)
        return found

    def fit(self) -> None:
        self.lanes.fit()
        self.curves.fit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._fitted and self.lanes.width() > 100:
            self._fitted = True
            self.fit()

    def _frame(self) -> None:
        self.keys.set_frame(self.state.frame)

    def _reset(self) -> None:
        self.tree.expandAll()
        self._selected()

    def _tree_selected(self, *_changes) -> None:
        items = [self.tracks.item(index) for index in self.tree.selectionModel().selectedRows()]
        self.keys.select(items or [item for item in self.keys.selection if isinstance(item, Key)])

    def _selected(self) -> None:
        chosen = QItemSelection()
        for item in self.keys.selection:
            if not isinstance(item, Key) and (index := self.tracks.index_of(item)).isValid():
                chosen.select(index, index.siblingAtColumn(1))
        with QSignalBlocker(self.tree.selectionModel()):
            self.tree.selectionModel().select(chosen, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.tree.viewport().update()
        self.pane.sync()
        self.changed.emit()

    def _key_requested(self, track: Property, frame: float) -> None:
        self.add_keys(self.leaves([track]), frame)

    def add_keys(self, tracks: list[Property], frame: float) -> None:
        state = self.state
        made = state.attempt(state_edits.add_keys, state.document, self.animation, tracks, max(0.0, frame))
        if made:
            self.keys.select(made)

    def remove(self) -> None:
        state, chosen = self.state, list(self.keys.selection)
        if chosen:
            state.attempt(state_edits.remove_items, state.document, self.animation, chosen)

    def set_frame(self, key: Key, frame: float) -> None:
        state = self.state
        state.attempt(state_edits.set_key_frame, state.document, self.animation, key, frame)

    def set_value(self, track: Property, key: Key, value) -> None:
        state = self.state
        state.attempt(state_edits.set_key_value, state.document, self.animation, track, key, value)

    def control(self, key: Key, at: int) -> list[float]:
        return list(key.control[at:at + 2])

    def set_control(self, key: Key, at: int, pair: list[float]) -> None:
        state, held = self.state, list(key.control)
        held[at:at + 2] = [float(part) for part in pair]
        state.attempt(state_edits.set_key_control, state.document, self.animation, key, tuple(held))

    def copied(self) -> list[Key]:
        keys = [item for item in self.keys.selection if isinstance(item, Key)]
        first = min((key.frame for key in keys), default=0.0)
        found = [copy.copy(key) for key in keys]
        for key in found:
            key.frame -= first
        return found

    def copied_tracks(self) -> list[tuple[str, Property]]:
        wanted: dict[int, tuple[str, Property, set[str]]] = {}
        for item in self.keys.selection:
            for track in item.properties if isinstance(item, Node) else [item] if isinstance(item, Property) else []:
                owner = self.clip.parent(track)[0]
                top = track if isinstance(owner, Node) else owner
                names = wanted.setdefault(id(top), (self.clip.parent(top)[0].name, top, set()))[2]
                names.update([track.name] if top is not track else [child.name for child in top.children])
        found = []
        for name, top, names in wanted.values():
            made = copy.deepcopy(top)
            made.children = [child for child in made.children if child.name in names]
            found.append((name, made))
        return found

    def _paste_tracks(self, items: list[tuple[str, Property]], target) -> list[Property]:
        document, names, made = self.state.document, list(dict.fromkeys(name for name, _track in items)), []
        with document.macro(self.tr("Paste Tracks")):
            for name in names:
                obj = target if len(names) == 1 and target is not None else next(
                    (item for item in self.symbol.objects if item.name == name), None)
                if obj is None:
                    raise GuiEditError(self.tr("This state's symbol holds no object named {0}: select the object "
                                               "the tracks go to.").format(name))
                made += state_edits.paste_tracks(document, self.animation, obj,
                                                 [track for held, track in items if held == name])
        return made

    def paste(self, items: list, target) -> None:
        state, tracks = self.state, self.leaves(self.keys.selection)
        if items and isinstance(items[0], tuple):
            made = state.attempt(self._paste_tracks, items, target)
        elif items and len(tracks) == 1:
            made = state.attempt(state_edits.paste_keys, state.document, self.animation, tracks[0], items, state.frame)
        else:
            return
        if made:
            self.keys.select(made)


class Timeline(QWidget):
    def __init__(self, state, popup, parent: QWidget | None = None):
        super().__init__(parent)
        self.state, self.popup = state, popup
        self.view: StateView | None = None
        self._copied: list = []
        self._states: tuple = ()
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        strip, row = editor_strip()
        self.owner = tool_button("layers", self.tr("The control whose states are listed. Click to select it: its "
                                                   "states are renamed, ordered and removed in the inspector."),
                                 self._select_owner, text=" ", parent=self)
        self.states = EmbeddedPopupComboBox(self)
        self.states.setMinimumWidth(150)
        self.states.setToolTip(self.tr("The state shown: one animation of the control's objects. Choosing a state "
                                       "starts it from its first frame, as a script does in the game."))
        self.new_state = tool_button("add", self.tr("Add a state to the control's symbol"), self._add_state,
                                     parent=self)
        self.rewind = tool_button("skip_back", self.tr("Back to the first frame"), self._rewind, parent=self)
        self.play = tool_button("play", self.tr("Play: every control runs its state, as in the game"), self._toggle,
                                parent=self)
        self.frame = QDoubleSpinBox(self)
        self.frame.setDecimals(2)
        self.frame.setRange(0.0, 1e7)
        self.frame.setKeyboardTracking(False)
        self.frame.setMaximumWidth(90)
        self.frame.setToolTip(self.tr("The frame shown"))
        self.length = styled_label("", self, tone="muted")
        self.add = tool_button("add", "", self._add_track, text=self.tr("Track"), parent=self)
        self.key = tool_button("key", self.tr("Add a key at the playhead on the selected tracks"), self._add_key,
                               text=self.tr("Key"), parent=self)
        self.remove = tool_button("delete", self.tr("Delete the selected keys or tracks (Del)"), self._delete,
                                  parent=self)
        self.curve = tool_button("graph", self.tr("Curves: the selected tracks' values over the frames. Keys drag in "
                                                  "frame and value, a selected curved key shows its tangents to "
                                                  "drag, a double click adds a key."), self._curved,
                                 text=self.tr("Curves"), parent=self)
        self.curve.setCheckable(True)
        self.fit = tool_button("fit", self.tr("Show the whole state"), self._fit, parent=self)
        for widget in (self.owner, self.states, self.new_state, toolbar_separator(), self.rewind, self.play,
                       self.frame, self.length, toolbar_separator(), self.add, self.key, self.remove):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(self.curve)
        row.addWidget(self.fit)
        self.hint = styled_label("", self, tone="muted")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        column.addWidget(strip)
        column.addWidget(self.hint, 1)
        for keys, slot in ((QKeySequence.StandardKey.Delete, self._delete), (QKeySequence.StandardKey.Copy, self._copy),
                           (QKeySequence.StandardKey.Paste, self._paste)):
            QShortcut(keys, self, slot).setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.states.activated.connect(self._chosen)
        self.frame.valueChanged.connect(state.set_frame)
        state.shown_changed.connect(self._shown)
        state.frame_changed.connect(self._frame)
        state.played.connect(self._played)
        for signal in (state.changed, state.selection_changed):
            signal.connect(self._update)
        self._shown()

    def hideEvent(self, event) -> None:
        self.state.play(False)
        super().hideEvent(event)

    def _symbol(self):
        held = self.state.context.prototype
        return held.symbol if held is not None else None

    def _target(self):
        node = self.state.current
        return node.object if node is not None and node.parent is self.state.context else None

    def _shown(self) -> None:
        state, view = self.state, self.view
        wanted = state.shown, state.shown is not None and state.home(state.context) is not None
        if view is None or (view.animation, view.editable) != wanted:
            if view is not None:
                for row in view.pane.rows:
                    row.live = False
                view.hide()
                view.deleteLater()
            self.view = None
            if state.shown is not None:
                self.view = StateView(state, state.shown, state.context.prototype, wanted[1], self)
                self.view.views.setCurrentIndex(int(self.curve.isChecked()))
                self.layout().insertWidget(1, self.view, 1)
                self.view.changed.connect(self._update)
        self._update()
        self._frame()

    def _update(self, *_changes) -> None:
        state, view, symbol = self.state, self.view, self._symbol()
        held = symbol.animations if symbol is not None else []
        local, target = state.home(state.context) is not None, self._target()
        signature = tuple((id(item), item.name) for item in held)
        with QSignalBlocker(self.states):
            if signature != self._states:
                self._states = signature
                self.states.clear()
                self.states.addItems([item.name for item in held])
            self.states.setCurrentIndex(next((at for at, item in enumerate(held) if item is state.shown), -1))
        self.states.setEnabled(bool(held))
        self.owner.setText(state.context.object.name)
        self.new_state.setEnabled(local)
        able = view is not None and view.editable
        self.add.setEnabled(able and target is not None)
        self.add.setToolTip(self.tr("Animate a property of {0} in this state").format(target.name) if target is not None
                            else self.tr("Select an object of {0} to animate one of its properties")
                            .format(state.context.object.name))
        self.key.setEnabled(able and bool(view.leaves(view.keys.selection)))
        self.remove.setEnabled(able and bool(view.keys.selection))
        for widget in (self.rewind, self.frame, self.fit, self.curve):
            widget.setEnabled(view is not None)
        total = max(0.0, state.shown.clip.total_frame) if state.shown is not None else 0.0
        self.length.setText(f"/ {total:g}")
        self.hint.setVisible(view is None)
        self.hint.setText((self.tr("{0} has no states. A state is an animation of the objects of its symbol: add one "
                                   "with +.") if local else self.tr("{0} has no states."))
                          .format(state.context.object.name))

    def _frame(self) -> None:
        with QSignalBlocker(self.frame):
            self.frame.setValue(self.state.frame)

    def _select_owner(self) -> None:
        self.state.select([self.state.context])

    def _chosen(self, index: int) -> None:
        self.state.show(self.state.context, self._symbol().animations[index])

    def _add_state(self) -> None:
        state = self.state
        made = state.attempt(state_edits.add_state, state.document, self._symbol())
        if made is not None:
            state.settle()
            state.show(state.context, made)

    def _rewind(self) -> None:
        self.state.set_frame(0.0)

    def _played(self, on: bool) -> None:
        self.play.setIcon(line_icon("pause" if on else "play"))

    def _toggle(self) -> None:
        state, shown = self.state, self.state.shown
        ended = shown is not None and not shown.loop and shown.transition is None \
            and state.frame >= shown.clip.total_frame
        if not state.playing and ended:
            state.set_frame(0.0)
        state.play(not state.playing)

    def _add_track(self) -> None:
        state, obj, animation = self.state, self._target(), self.state.shown
        document, kinds = state.document, display_names("PropertyType")
        held = {track.name: track for track in state_edits.track_node(document, animation, obj).properties}
        entries = []
        for field in document.authoring.fields(obj.type_name):
            if not state_edits.can_animate(document, obj, field.name):
                continue
            if field.name not in held:
                entries.append((field.name, kinds[int(field.type)], (field.name, None)))
            else:
                entries += [(f"{field.name}.{member}", kinds[kind], (field.name, member))
                            for member, kind in clip_edits.missing_members(held[field.name])]
        self.popup.pick(entries, below(self.add), self.tr("Animate a property of {0}").format(obj.name),
                        partial(self._animate, obj, animation, held))

    def _animate(self, obj, animation, held: dict, choice: tuple) -> None:
        document, (name, member) = self.state.document, choice
        made = state_edits.add_track(document, animation, obj, name) if member is None else \
            state_edits.add_member(document, animation, obj, held[name], member)
        self.view.keys.select([made])

    def _add_key(self) -> None:
        view = self.view
        if view is not None and view.editable:
            view.add_keys(view.leaves(view.keys.selection), self.state.frame)

    def _delete(self) -> None:
        if self.view is not None and self.view.editable:
            self.view.remove()

    def _copy(self) -> None:
        if self.view is not None and (found := self.view.copied() or self.view.copied_tracks()):
            self._copied = found

    def _paste(self) -> None:
        if self.view is not None and self.view.editable:
            self.view.paste(self._copied, self._target())

    def _curved(self, on: bool) -> None:
        self.view.views.setCurrentIndex(int(on))

    def _fit(self) -> None:
        if self.view is not None:
            self.view.fit()
