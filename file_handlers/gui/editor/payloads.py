"""See docs/internals/gui-editor.md, Inspector."""

from __future__ import annotations

from dataclasses import replace
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QWidget

lazy from ui.editor_widgets import styled_label, tool_button

from .. import edits
from ..model import GuiNineSlice, GuiTextureEntry, GuiTextureSet
from ..scene import GuiSceneNode
from .values import NumberEdit


class _Content(QWidget):
    def __init__(self, inspector, node: GuiSceneNode, parent: QWidget):
        super().__init__(parent)
        self.inspector, self.node = inspector, node
        self.editable = inspector.state.mode(node) == "definition"
        self.edits: list[NumberEdit] = []
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(14, 2, 6, 6)
        self.grid.setHorizontalSpacing(4)
        self.grid.setVerticalSpacing(3)

    @property
    def held(self):
        return self.node.object.special_data

    def put(self, payload) -> None:
        state = self.inspector.state
        state.attempt(edits.set_payload, state.document, self.node.object, payload)

    def heads(self, titles: list[tuple[str, str]], row: int = 0, column: int = 0) -> None:
        for at, (title, tip) in enumerate(titles, column):
            label = styled_label(title, self, tone="subtle")
            label.setToolTip(tip)
            self.grid.addWidget(label, row, at)

    def number(self, integer: bool, get, apply, row: int, column: int) -> None:
        edit = NumberEdit(integer, get, apply, self)
        edit.setEnabled(self.editable)
        self.grid.addWidget(edit, row, column)
        self.edits.append(edit)

    def note(self, row: int) -> None:
        if not self.editable:
            label = styled_label(self.tr("The content belongs to the object itself, not to a placement: open its "
                                         "symbol alone to change it."), self, tone="muted")
            label.setWordWrap(True)
            self.grid.addWidget(label, row, 0, 1, self.grid.columnCount())

    def refresh(self) -> None:
        for edit in self.edits:
            edit.refresh()

    def _cell_heads(self) -> list[tuple[str, str]]:
        return [(self.tr("Sequence"), self.tr("The pattern list of the object's UV sequence file (UVSequence) the "
                                              "picture is taken from")),
                (self.tr("Pattern"), self.tr("The pattern of that list: one rectangle of a texture"))]


class TextureSetEdit(_Content):
    def __init__(self, inspector, node: GuiSceneNode, parent: QWidget):
        super().__init__(inspector, node, parent)
        sides = (self.tr("Left"), self.tr("Top"), self.tr("Right"), self.tr("Bottom"))
        tip = self.tr("The rectangle the pattern is drawn in, in the object's own space, before its Scale")
        self.heads([*self._cell_heads(), *((side, tip) for side in sides)])
        for at in range(len(self.held.entries)):
            for column, name in enumerate(("sequence", "pattern")):
                self.number(True, partial(self._part, at, name), partial(self._set, at, name), at + 1, column)
            for side in range(4):
                self.number(False, partial(self._side, at, side), partial(self._set_side, at, side), at + 1, side + 2)
            remove = tool_button("remove", self.tr("Remove the entry"), partial(self._remove, at), parent=self)
            remove.setEnabled(self.editable)
            self.grid.addWidget(remove, at + 1, 6)
        rows = len(self.held.entries) + 1
        add = tool_button("add", self.tr("Add an entry: one pattern of the UV sequence file, drawn in a rectangle of "
                                         "its own"), self._add, text=self.tr("Entry"), parent=self)
        add.setEnabled(self.editable)
        self.grid.addWidget(add, rows, 0, 1, 7, Qt.AlignmentFlag.AlignLeft)
        self.note(rows + 1)

    def _part(self, at: int, name: str) -> int:
        return getattr(self.held.entries[at], name)

    def _side(self, at: int, side: int) -> float:
        return self.held.entries[at].bounds[side]

    def _replace(self, at: int, entry: GuiTextureEntry) -> None:
        self.put(GuiTextureSet([entry if place == at else held for place, held in enumerate(self.held.entries)]))

    def _set(self, at: int, name: str, value: int) -> None:
        self._replace(at, replace(self.held.entries[at], **{name: value}))

    def _set_side(self, at: int, side: int, value: float) -> None:
        held = self.held.entries[at]
        self._replace(at, replace(held, bounds=tuple(value if place == side else old
                                                     for place, old in enumerate(held.bounds))))

    def _remove(self, at: int) -> None:
        self.put(GuiTextureSet([held for place, held in enumerate(self.held.entries) if place != at]))

    def _add(self) -> None:
        tagged = self.inspector.state.document.authoring.payloads[self.node.object.type_name]["tagged"]
        self.put(GuiTextureSet([*self.held.entries, GuiTextureEntry(0, 0, (0.0, 0.0, 0.0, 0.0), tagged)]))


class NineSliceEdit(_Content):
    def __init__(self, inspector, node: GuiSceneNode, parent: QWidget):
        super().__init__(inspector, node, parent)
        sides = (self.tr("Left"), self.tr("Top"), self.tr("Right"), self.tr("Bottom"))
        tip = self.tr("The width of the border on that side: corners keep their size, edges stretch along one "
                      "axis, the centre along both")
        self.heads([(side, tip) for side in sides], 0, 1)
        self.grid.addWidget(styled_label(self.tr("Border"), self), 1, 0)
        for side in range(4):
            self.number(False, partial(self._border, side), partial(self._set_border, side), 1, side + 1)
        repeat = self.tr("Stored per cell; every file of the game holds 0 and the preview does not use it")
        self.heads([(self.tr("Cell"), self.tr("The nine parts of the frame, row by row")), *self._cell_heads(),
                    (self.tr("Repeat W"), repeat), (self.tr("Repeat H"), repeat)], 2)
        names = (self.tr("Top left"), self.tr("Top"), self.tr("Top right"), self.tr("Left"), self.tr("Centre"),
                 self.tr("Right"), self.tr("Bottom left"), self.tr("Bottom"), self.tr("Bottom right"))
        for at, name in enumerate(names):
            self.grid.addWidget(styled_label(name, self), at + 3, 0)
            for column, part in enumerate(("sequence", "pattern")):
                self.number(True, partial(self._part, at, part), partial(self._set, at, part), at + 3, column + 1)
            for side in range(2):
                self.number(False, partial(self._repeat, at, side), partial(self._set_repeat, at, side), at + 3,
                            side + 3)
        self.note(len(names) + 3)

    def _border(self, side: int) -> float:
        return self.held.borders[side]

    def _set_border(self, side: int, value: float) -> None:
        held = self.held
        self.put(GuiNineSlice(tuple(value if place == side else old for place, old in enumerate(held.borders)),
                              held.cells))

    def _part(self, at: int, name: str) -> int:
        return getattr(self.held.cells[at], name)

    def _repeat(self, at: int, side: int) -> float:
        return self.held.cells[at].repeat_size[side]

    def _replace(self, at: int, cell) -> None:
        held = self.held
        self.put(GuiNineSlice(held.borders, [cell if place == at else old for place, old in enumerate(held.cells)]))

    def _set(self, at: int, name: str, value: int) -> None:
        self._replace(at, replace(self.held.cells[at], **{name: value}))

    def _set_repeat(self, at: int, side: int, value: float) -> None:
        held = self.held.cells[at]
        self._replace(at, replace(held, repeat_size=tuple(value if place == side else old
                                                          for place, old in enumerate(held.repeat_size))))


def payload_editor(inspector, node: GuiSceneNode, parent: QWidget) -> QWidget:
    kind = TextureSetEdit if isinstance(node.object.special_data, GuiTextureSet) else NineSliceEdit
    return kind(inspector, node, parent)
