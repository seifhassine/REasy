"""See docs/internals/gui-editor.md, Inspector."""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout,
                               QWidget)

lazy from ui.editor_widgets import DisclosureButton, EmbeddedPopupComboBox, styled_label, tool_button
lazy from ui.icons import line_icon
lazy from ui.styles import set_style_role

from ui.search_popup import below

from .. import edits, state_edits
from ..model import ZERO_GUID
from ..rules import gui_layout
from ..scene import GuiSceneNode
from ..storage import nodes
from .navigator import short
from .payloads import payload_editor
from .pickers import picker
from .values import LineEdit, ValueEdit, value_edit


class Properties(QWidget):
    def __init__(self, inspector: Inspector, node: GuiSceneNode, fields: list, parent: QWidget):
        super().__init__(parent)
        self.inspector, self.node = inspector, node
        state = inspector.state
        layout = gui_layout(state.document.gui.version)
        grid = QGridLayout(self)
        grid.setContentsMargins(14, 2, 6, 6)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(0, inspector.label_width)
        self.rows = []
        for at, field in enumerate(fields):
            label = QLabel(field.name, self)
            label.setToolTip(self._tip(field))
            edit = value_edit(layout, field.type, state.labels(node.resource, node.object, field),
                              partial(inspector.value, node, field.name),
                              partial(inspector.set_value, node, field.name),
                              self, " ".join(f".{kind}" for kind in field.asset), field.lines,
                              self.tr("(first state)"), picker(state, inspector.popup, node.object.type_name, field,
                                                               partial(state.set_value, node, field.name)))
            edit.setEnabled(inspector.editable or inspector.scripting)
            reset = tool_button("reset", self.tr("Remove the script value: the file's value shows again")
                                if inspector.scripting else
                                self.tr("Remove this placement's own value: the symbol's value shows again"),
                                partial(inspector.revert, node, field.name), parent=self)
            keyed = tool_button("clock", "", partial(inspector.key, node, field.name), parent=self)
            keyed.setCheckable(True)
            grid.addWidget(label, at, 0)
            grid.addWidget(edit, at, 1)
            grid.addWidget(reset, at, 2)
            grid.addWidget(keyed, at, 3)
            self.rows.append((field, label, edit, reset, keyed))
        self.refresh()

    def _tip(self, field) -> str:
        parts = [field.type.name.capitalize() if not field.enum else short(field.enum)]
        if field.when is not None:
            parts.append(self.tr("stored while {0} is {1}").format(*field.when))
        if field.unread:
            parts.append(self.tr("the game's class does not read it"))
        return ", ".join(parts)

    def refresh(self) -> None:
        state, node, animated = self.inspector.state, self.node, self.inspector.animated
        for field, label, edit, reset, keyed in self.rows:
            edit.refresh()
            held = state.scripted(node, field.name) if self.inspector.scripting else \
                state.binding(node, field.name) is not None
            reset.setVisible(held)
            if bool(label.property("tone")) != held:
                set_style_role(label, tone="accent" if held else "")
            found, track = animated.get(field.name, ()), state.keyed(node, field.name)
            here = track is not None or state.can_key(node, field.name)
            keyed.setVisible(here or bool(found))
            keyed.setIcon(line_icon("key" if here else "clock"))
            keyed.setChecked(track is not None and state.on_key(track))
            keyed.setToolTip(
                self.tr("Animated in {0}: the value shown is the state's at the playhead, and a change keys it "
                        "there. Click to add a key at frame {1:g} that holds it.").format(state.shown.name, state.frame)
                if track is not None else
                self.tr("Animate in {0}: the value becomes the first key at frame 0, and a change at another frame "
                        "keys it there.").format(state.shown.name) if here else
                self.tr("Animated in {0}: while such a state plays, its keys set the value. Click to show the "
                        "state.").format(", ".join(item.name for item in found)))


class Section(QWidget):
    def __init__(self, inspector: Inspector, title: str, body: QWidget, parent: QWidget):
        super().__init__(parent)
        self.inspector, self.title, self.body = inspector, title, body
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.button = DisclosureButton(title, self)
        self.button.setChecked(title not in inspector.folded)
        body.setParent(self)
        body.setVisible(self.button.isChecked())
        column.addWidget(self.button)
        column.addWidget(body)
        self.button.toggled.connect(self._toggled)

    def _toggled(self, on: bool) -> None:
        (self.inspector.folded.discard if on else self.inspector.folded.add)(self.title)
        self.body.setVisible(on)

    def refresh(self) -> None:
        self.body.refresh()


class Header(QWidget):
    def __init__(self, inspector: Inspector, node: GuiSceneNode, parent: QWidget):
        super().__init__(parent)
        self.inspector, self.node = inspector, node
        state = inspector.state
        form = QFormLayout(self)
        form.setContentsMargins(8, 4, 8, 4)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.name = LineEdit(self._name, self._rename, self)
        self.name.setEnabled(state.local(node) or state.stand(node))
        form.addRow(self.tr("Name"), self.name)
        form.addRow(self.tr("Class"), styled_label(short(node.object.type_name), self, tone="muted"))
        self.symbol = self.open = self.unique = None
        if state.document.authoring.is_control(node.object.type_name) and node.parent is not None:
            holder = QWidget(self)
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            self.symbol = tool_button("layers", self.tr("Choose the symbol this control places"), self._pick,
                                      text=" ", parent=holder)
            self.symbol.setEnabled(state.local(node))
            self.open = tool_button("external", self.tr("Open the symbol alone"), self._open, parent=holder)
            self.unique = tool_button("split", self.tr("Make unique: give this control a copy of the symbol, kept "
                                                       "in this file, so changes stay here"), self._unique,
                                      parent=holder)
            row.addWidget(self.symbol, 1)
            row.addWidget(self.open)
            row.addWidget(self.unique)
            form.addRow(self.tr("Symbol"), holder)
        self.note = styled_label("", self, tone="muted")
        self.note.setWordWrap(True)
        form.addRow(self.note)
        self.refresh()

    def _name(self) -> str:
        return self.inspector.state.scope.name if self.inspector.state.stand(self.node) else self.node.object.name

    def _rename(self, name: str) -> None:
        state = self.inspector.state
        if state.stand(self.node):
            state.attempt(edits.rename_symbol, state.document, state.scope, name)
        else:
            state.attempt(edits.rename_object, state.document, self.node.object, name)

    def _symbols(self) -> list[tuple]:
        state, node = self.inspector.state, self.node
        found = [(symbol.name, self.tr("this file"), symbol) for symbol in state.document.gui.symbols[1:]]
        found += [(symbol.name, key, symbol) for key, resource in state.workspace.resources.items()
                  if key != state.workspace.root_key for symbol in resource.document.symbols]
        return [(self.tr("No symbol"), "", None),
                *(entry for entry in found if entry[2].type_name == node.object.type_name)]

    def _pick(self) -> None:
        state = self.inspector.state
        self.inspector.popup.pick(self._symbols(), below(self.symbol), self.tr("Search symbols"),
                                  lambda symbol: edits.set_symbol(state.document, self.node.object, symbol))

    def _open(self) -> None:
        self.inspector.state.open_symbol(self.inspector.state.home(self.node))

    def _unique(self) -> None:
        state = self.inspector.state
        state.attempt(edits.make_unique, state.document, self.node.object, self.node.prototype.symbol)

    def refresh(self) -> None:
        state, node = self.inspector.state, self.node
        self.name.refresh()
        if self.symbol is not None:
            held, home = node.prototype, state.home(node)
            missing = held is None and node.object.prototype_guid != ZERO_GUID
            self.symbol.setText(self.tr("Not found") if missing else held.symbol.name if held is not None
                                else self.tr("None"))
            self.open.setEnabled(home is not None)
            shared = home is not None and len(state.document.structure.uses.get(home.guid, ())) > 1
            self.unique.setEnabled(state.local(node) and held is not None and (home is None or shared))
        mode = state.mode(node)
        self.note.setText(
            self.tr("The symbol itself. Its objects and states are shared by every control that places it.")
            if state.stand(node) else "" if mode == "definition" else state.locked(node) if mode == "locked" else
            self.tr("Placed {0} times through a shared symbol. A value set here is kept for this placement only; "
                    "open the symbol to change every placement.").format(state.placed(node)) if state.local(node)
            else self.tr("From {0}. A value set here is kept in this file, for this placement only.")
            .format(node.resource.key))
        self.note.setVisible(bool(self.note.text()))


class States(QWidget):
    def __init__(self, inspector: Inspector, node: GuiSceneNode, parent: QWidget):
        super().__init__(parent)
        self.inspector, self.node = inspector, node
        state, symbol = inspector.state, node.prototype.symbol
        self.editable = state.home(node) is not None
        grid = QGridLayout(self)
        grid.setContentsMargins(14, 2, 6, 6)
        grid.setHorizontalSpacing(2)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(1, 1)
        self.rows = []
        for at, animation in enumerate(symbol.animations):
            show = tool_button("eye", self.tr("Show this state in the timeline"), partial(self._show, animation),
                               parent=self)
            show.setCheckable(True)
            name = LineEdit(partial(getattr, animation, "name"), partial(self._edit, state_edits.rename_state,
                                                                         animation), self)
            loop = QCheckBox(self.tr("Loop"), self)
            loop.setToolTip(self.tr("Start again from frame 0 when the last frame is reached"))
            loop.clicked.connect(partial(self._loop, animation))
            after = EmbeddedPopupComboBox(self)
            after.setToolTip(self.tr("The state that plays once this one ends"))
            after.addItem(self.tr("(stays)"))
            after.addItems([item.name for item in symbol.animations])
            after.activated.connect(partial(self._next, animation))
            buttons = [tool_button(icon, tip, slot, parent=self) for icon, tip, slot in (
                ("arrow_up", self.tr("Move up: the first state plays when the GUI opens"),
                 partial(self._edit, state_edits.move_state, animation, at - 1)),
                ("arrow_down", self.tr("Move down"), partial(self._edit, state_edits.move_state, animation, at + 1)),
                ("remove", self.tr("Remove the state"), partial(self._remove, animation)))]
            for column, widget in enumerate((show, name, loop, after, *buttons)):
                widget.setEnabled(self.editable or widget is show)
                grid.addWidget(widget, at, column)
            self.rows.append((animation, show, name, loop, after))
        self.add = tool_button("add", self.tr("Add a state: an animation of this symbol's objects"), self._add,
                               text=self.tr("State"), parent=self)
        self.add.setEnabled(self.editable)
        grid.addWidget(self.add, len(self.rows), 0, 1, 4, Qt.AlignmentFlag.AlignLeft)
        self.refresh()

    def _edit(self, edit, animation, *arguments) -> None:
        state = self.inspector.state
        state.attempt(edit, state.document, animation, *arguments)

    def _loop(self, animation) -> None:
        self._edit(state_edits.set_loop, animation, not animation.loop)

    def _next(self, animation, index: int) -> None:
        held = self.node.prototype.symbol.animations
        self._edit(state_edits.set_transition, animation, held[index - 1] if index else None)

    def _show(self, animation) -> None:
        self.inspector.state.show(self.node, animation)

    def _remove(self, animation) -> None:
        state = self.inspector.state
        state.attempt(state_edits.remove_states, state.document, [animation])

    def _add(self) -> None:
        state = self.inspector.state
        made = state.attempt(state_edits.add_state, state.document, self.node.prototype.symbol)
        if made is not None:
            state.settle()
            state.show(self.node, made)

    def refresh(self) -> None:
        state, held = self.inspector.state, self.node.prototype.symbol.animations
        for animation, show, name, loop, after in self.rows:
            name.refresh()
            with QSignalBlocker(show), QSignalBlocker(loop), QSignalBlocker(after):
                show.setChecked(state.context is self.node and state.shown is animation)
                loop.setChecked(animation.loop)
                after.setCurrentIndex(next((at + 1 for at, item in enumerate(held) if item is animation.transition), 0))


class Inspector(QScrollArea):
    def __init__(self, state, popup, parent: QWidget | None = None):
        super().__init__(parent)
        self.state, self.popup = state, popup
        self.folded: set[str] = set()
        self.animated: dict[str, list] = {}
        self.editable, self.scripting, self.label_width = False, False, 0
        self._signature, self._scroll = None, 0
        self.parts: list[QWidget] = []
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(self)
        set_style_role(body, surface="view")
        self.setWidget(body)
        self._column = QVBoxLayout(body)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(0)
        self.title = styled_label("", body, heading="panel")
        self.title.setContentsMargins(8, 6, 8, 2)
        self.script = tool_button("code", self.tr("Script values: while on, a change is kept for the preview only, as "
                                                  "a value the game's script sets when the GUI runs. The file is not "
                                                  "changed."), self._scripting, text=" ", parent=body)
        self.script.setCheckable(True)
        self.unscript = tool_button("reset", self.tr("Remove every script value"), self._unscript, parent=body)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 6, 0)
        for widget, stretch in ((self.title, 1), (self.script, 0), (self.unscript, 0)):
            head.addWidget(widget, stretch)
        self._column.addLayout(head)
        self._column.addStretch(1)
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self.sync)
        self._restore = QTimer(self)
        self._restore.setSingleShot(True)
        self._restore.timeout.connect(self._scrolled)
        for signal in (state.selection_changed, state.shown_changed, state.changed, state.frame_changed):
            signal.connect(self._schedule)
        self.sync()

    def _schedule(self, *_changes) -> None:
        self._refresh.start()

    def _scrolled(self) -> None:
        self.verticalScrollBar().setValue(self._scroll)

    def _scripting(self) -> None:
        self.scripting = self.script.isChecked()
        self.sync()

    def _unscript(self) -> None:
        self.state.clear_script()

    def value(self, node: GuiSceneNode, name: str):
        return self.state.shown_value(node, name) if self.scripting else self.state.value(node, name)

    def set_value(self, node: GuiSceneNode, name: str, value) -> None:
        state = self.state
        state.attempt(state.set_script if self.scripting else state.set_value, node, name, value)

    def revert(self, node: GuiSceneNode, name: str) -> None:
        state = self.state
        if self.scripting:
            state.clear_script(node, name)
        elif (binding := state.binding(node, name)) is not None:
            state.attempt(edits.remove_overrides, state.document, [binding])

    def key(self, node: GuiSceneNode, name: str) -> None:
        state = self.state
        if state.keyed(node, name) is not None or state.can_key(node, name):
            state.attempt(state.add_key, node, name)
        else:
            state.show(node.parent, self.animated[name][0])

    def fields(self, node: GuiSceneNode) -> list:
        authoring, obj = self.state.document.authoring, node.object
        active = {field.name for field in authoring.stored(obj.type_name, obj.values())}
        return [field for field in authoring.fields(obj.type_name)
                if obj.property(field.name) is not None or field.name in active and not field.unread]

    def _animated(self, node: GuiSceneNode) -> dict[str, list]:
        found: dict[str, list] = {}
        owner = node.parent.prototype.symbol if node.parent is not None and node.parent.prototype is not None else None
        first = next((item for item in owner.objects if item.name == node.object.name), None) if owner else None
        for animation in owner.animations if first is node.object else ():
            for held in nodes(animation):
                if held.name == node.object.name:
                    for track in held.properties:
                        found.setdefault(track.name, []).append(animation)
        return found

    def sync(self) -> None:
        state = self.state
        if state.settling:
            self._refresh.start()
            return
        node = state.current
        fields = self.fields(node) if node is not None and not state.stand(node) else []
        self.animated = self._animated(node) if node is not None else {}
        self.editable = node is not None and state.mode(node) != "locked"
        special = node.object.special_data if node is not None else None
        count = sum(map(len, state.script.values()))
        self.script.setText(f" {count}" if count else " ")
        self.unscript.setVisible(count > 0)
        signature = node is not None and (
            state.key(node), self.editable, self.scripting, state.local(node), tuple(field.name for field in fields),
            type(special).__name__, len(getattr(special, "entries", ())),
            tuple((id(item), item.name) for item in node.prototype.symbol.animations)
            if node.prototype is not None else None, id(node))
        if signature == self._signature:
            for part in self.parts:
                part.refresh()
            return
        self._scroll = self.verticalScrollBar().value() if signature and self._signature \
            and signature[0] == self._signature[0] else 0
        for part in self.parts:
            for edit in part.findChildren(ValueEdit):
                edit.live = False
            part.hide()
            part.deleteLater()
        body, self.parts, self._signature = self.widget(), [], signature
        self.label_width = max((self.fontMetrics().horizontalAdvance(field.name) for field in fields), default=0)
        self.title.setText(self.tr("Nothing selected") if node is None else node.object.name)
        if node is not None:
            self.parts.append(Header(self, node, body))
            groups: dict[str, list] = {}
            for field in fields:
                groups.setdefault(self.tr("Stored, not read by the game") if field.unread else short(field.owner),
                                  []).append(field)
            self.parts += [Section(self, title, Properties(self, node, held, body), body)
                           for title, held in groups.items()]
            if special is not None:
                self.parts.append(Section(self, self.tr("Content"), payload_editor(self, node, body), body))
            if node.prototype is not None:
                self.parts.append(Section(self, self.tr("States"), States(self, node, body), body))
        for at, part in enumerate(self.parts, 1):
            self._column.insertWidget(at, part)
        self._restore.start()
