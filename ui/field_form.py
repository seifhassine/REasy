"""See docs/internals/clip-editor.md."""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QCompleter,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

lazy from ui.editor_widgets import DisclosureButton, EmbeddedPopupComboBox, set_invalid, styled_label
lazy from ui.styles import set_style_role
lazy from utils.number_format import format_full_float


@dataclass(slots=True)
class Field:
    label: str
    kind: str
    get: Callable[[], object]
    set: Callable[[object], None] | None = None
    options: dict | None = None
    tooltip: str = ""


def _parse(kind: str, text: str):
    if kind == "float":
        value = float(text)
        if not math.isfinite(value):
            raise ValueError(text)
        return value
    if kind == "int":
        return int(text.strip(), 0)
    if kind == "hex":
        return int(text, 16)
    if kind == "guid":
        return uuid.UUID(text.strip()).bytes_le
    return text


def _format(kind: str, value) -> str:
    if kind == "float":
        return format_full_float(value)
    if kind == "hex":
        return f"{value:08X}"
    if kind == "guid":
        return str(uuid.UUID(bytes_le=value))
    return str(value)


def _choose(field: Field, value) -> None:
    if value != field.get():
        field.set(value)


def _quiet(widget: QWidget, setter, value) -> None:
    with QSignalBlocker(widget):
        setter(value)


def shape(fields: list[Field]) -> tuple:
    return tuple((field.label, field.kind, shape(field.options["fields"]) if field.kind == "row" else
                  (tuple(field.options.items()), field.get() in field.options) if field.kind in ("enum", "choice")
                  else None)
                 for field in fields)


class FieldForm(QWidget):
    def __init__(self, fields: list[Field], parent: QWidget | None = None):
        super().__init__(parent)
        self._updaters: list[Callable[[], None]] = []
        self._live = True
        form = QFormLayout(self)
        form.setContentsMargins(8, 4, 8, 8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for field in fields:
            if field.kind == "section":
                form.addRow(styled_label(field.label, heading="caption"))
                continue
            if field.kind == "toggle":
                button = DisclosureButton(field.label)
                button.setChecked(bool(field.get()))
                button.toggled.connect(field.set)
                form.addRow(button)
                continue
            widget = self._widget(field)
            label = QLabel(field.label)
            if field.tooltip:
                label.setToolTip(field.tooltip)
                widget.setToolTip(field.tooltip)
            form.addRow(label, widget)

    def refresh(self) -> None:
        for update in self._updaters:
            update()

    def retire(self) -> None:
        self._live = False

    def _widget(self, field: Field) -> QWidget:
        if field.kind == "readonly":
            widget = styled_label(str(field.get()), tone="muted")
            widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._updaters.append(lambda: widget.setText(str(field.get())))
            return widget
        if field.kind == "bool":
            widget = QCheckBox()
            widget.setChecked(bool(field.get()))
            widget.toggled.connect(lambda checked: field.set(bool(checked)))
            self._updaters.append(lambda: _quiet(widget, widget.setChecked, bool(field.get())))
            return widget
        if field.kind == "enum":
            widget = EmbeddedPopupComboBox()
            for value, name in field.options.items():
                widget.addItem(name, value)
            current = int(field.get())
            if widget.findData(current) < 0:
                widget.addItem(f"0x{current:X}", current)
            widget.setCurrentIndex(widget.findData(current))
            widget.activated.connect(lambda _index: _choose(field, widget.currentData()))
            self._updaters.append(lambda: _quiet(widget, widget.setCurrentIndex, widget.findData(int(field.get()))))
            return widget
        if field.kind == "choice":
            return self._choice(field)
        if field.kind == "button":
            widget = QPushButton(str(field.get()))
            set_style_role(widget, compact=True)
            widget.clicked.connect(lambda: field.set(None))
            return widget
        if field.kind == "row":
            return self._row(field)
        widget = QLineEdit(_format(field.kind, field.get()))
        if field.set is None:
            widget.setReadOnly(True)
        else:
            widget.editingFinished.connect(lambda: self._commit(widget, field))
        self._updaters.append(lambda: _quiet(widget, widget.setText, _format(field.kind, field.get())))
        return widget

    def _choice(self, field: Field) -> QLineEdit:
        """A name typed or completed from ``options`` ({value: name}), for lists too long for a combo box."""
        widget = QLineEdit(field.options.get(field.get(), ""))
        completer = QCompleter(list(field.options.values()), widget)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        widget.setCompleter(completer)
        values = {name.casefold(): value for value, name in field.options.items()}

        def chosen() -> None:
            value = values.get(widget.text().strip().casefold())
            set_invalid(widget, value is None)
            if self._live and value is not None:
                _choose(field, value)
        widget.editingFinished.connect(chosen)
        self._updaters.append(lambda: _quiet(widget, widget.setText, field.options.get(field.get(), "")))
        return widget

    def _row(self, field: Field) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        kinds = {sub.kind for sub in field.options["fields"]}
        for sub in field.options["fields"]:
            widget = self._widget(sub)
            if sub.label:
                row.addWidget(styled_label(sub.label, tone="subtle"))
            # A button beside other fields keeps its own width.
            row.addWidget(widget, 0 if sub.kind == "button" and kinds != {"button"} else 1)
        return holder

    def _commit(self, widget: QLineEdit, field: Field) -> None:
        if not self._live:
            return
        text = widget.text()
        if text == _format(field.kind, field.get()):
            set_invalid(widget, False)
            return
        try:
            field.set(_parse(field.kind, text))
        except ValueError:
            set_invalid(widget, True)
            return
        set_invalid(widget, False)


class FieldPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        set_style_role(self, surface="view")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.title = styled_label("", heading="panel")
        self.title.setContentsMargins(8, 6, 8, 2)
        layout.addWidget(self.title)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        layout.addWidget(self.scroll, 1)
        self._signature: tuple = ()

    def show_fields(self, key, title: str, fields: list[Field]) -> None:
        if (signature := (key, shape(fields))) == self._signature:
            self.scroll.widget().refresh()
            return
        self._signature = signature
        if (old := self.scroll.widget()) is not None:
            old.retire()
            self.scroll.takeWidget().deleteLater()
        self.scroll.setWidget(FieldForm(fields))
        self.title.setText(title)
