"""See docs/internals/bhvt-editor.md, Inspector."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from PySide6.QtWidgets import QHBoxLayout, QToolButton, QVBoxLayout, QWidget

lazy from ui.editor_widgets import ElidedLabel, styled_label, tool_button
lazy from ui.styles import set_style_role

from .object_form import Fold, ObjectForm


@dataclass(slots=True)
class Row:
    text: str
    detail: str = ""
    ref: tuple[str, int] | None = None
    skip: frozenset = frozenset()
    fixed: frozenset = frozenset()


class Rows(QWidget):
    def __init__(self, state, popup, expanded: set, title: str, rows: list[Row], parent: QWidget, empty: str = "",
                 **ops):
        super().__init__(parent)
        self.state, self.popup, self.expanded, self.title = state, popup, expanded, title
        self.ops = SimpleNamespace(**{name: ops.get(name) for name in ("add", "paste", "open", "remove", "move",
                                                                       "copy")})
        self._targets: dict = {}
        self._shown: list[tuple[QWidget, ElidedLabel | None]] = []
        self._forms: list[ObjectForm] = []
        column = QVBoxLayout(self)
        column.setContentsMargins(8, 6, 8, 2)
        column.setSpacing(1)
        header = QHBoxLayout()
        column.addLayout(header)
        header.addWidget(styled_label(title, self, heading="caption"), 1)
        for icon, tip, op, slot in (("paste", self.tr("Paste"), self.ops.paste, self._paste),
                                    ("add", self.tr("Add"), self.ops.add, self._add)):
            if op is not None:
                header.addWidget(tool_button(icon, f"{tip}: {title}", slot, parent=self))
        if not rows and empty:
            column.addWidget(styled_label(empty, self, tone="subtle"))
        for index, row in enumerate(rows):
            column.addWidget(self._row(index, row, len(rows)))

    def refresh(self, rows: list[Row]) -> None:
        for (widget, detail), row in zip(self._shown, rows):
            if detail is None:
                widget.set_text(row.text, row.detail)
            else:
                widget.setText(row.text)
                detail.setText(row.detail)
        for form in self._forms:
            form.refresh()

    def _row(self, index: int, row: Row, count: int) -> QWidget:
        buttons = [tool_button(icon, tip, slot, parent=self) for icon, tip, slot, shown in (
            ("arrow_up", self.tr("Move up"), self._up, self.ops.move is not None and index > 0),
            ("arrow_down", self.tr("Move down"), self._down, self.ops.move is not None and index < count - 1),
            ("copy", self.tr("Copy"), self._copy, self.ops.copy is not None and row.ref is not None),
            ("remove", self.tr("Remove"), self._remove, self.ops.remove is not None)) if shown]
        self._targets.update(dict.fromkeys(buttons, index))
        if row.ref is not None:
            fold = Fold(self, *buttons)
            fold.set_text(row.text, row.detail)
            self._targets[fold] = row
            fold.toggled.connect(self._unfold)
            fold.button.setChecked(id(self.state.summaries.fields(*row.ref)) in self.expanded)
            self._shown.append((fold, None))
            return fold
        holder = QWidget(self)
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(2)
        button = QToolButton(holder)
        button.setAutoRaise(True)
        button.setText(row.text)
        button.setMinimumWidth(40)
        button.setEnabled(self.ops.open is not None)
        button.clicked.connect(self._open)
        detail = ElidedLabel(holder)
        detail.setText(row.detail)
        set_style_role(detail, tone="muted")
        self._targets[button] = index
        line.addWidget(button)
        line.addWidget(detail, 1)
        for widget in buttons:
            line.addWidget(widget)
        self._shown.append((button, detail))
        return holder

    def _unfold(self, on: bool) -> None:
        fold = self.sender()
        row = self._targets[fold]
        fields = self.state.summaries.fields(*row.ref)
        (self.expanded.add if on else self.expanded.discard)(id(fields))
        if on and fold.body is None:
            rsz = self.state.model.blocks[row.ref[0]]
            form = ObjectForm.of(self.state, self.popup, self.expanded, rsz, rsz.object_table[row.ref[1]], fold,
                                 row.skip, row.fixed)
            self._forms.append(form)
            fold.set_body(form)

    def _add(self) -> None:
        self.state.attempt(self.ops.add)

    def _paste(self) -> None:
        self.state.attempt(self.ops.paste)

    def _open(self) -> None:
        self.state.attempt(self.ops.open, self._targets[self.sender()])

    def _up(self) -> None:
        self.state.attempt(self.ops.move, index := self._targets[self.sender()], index - 1)

    def _down(self) -> None:
        self.state.attempt(self.ops.move, index := self._targets[self.sender()], index + 1)

    def _copy(self) -> None:
        self.state.attempt(self.ops.copy, self._targets[self.sender()])

    def _remove(self) -> None:
        self.state.attempt(self.ops.remove, self._targets[self.sender()])
