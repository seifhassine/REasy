"""See docs/internals/gui-editor.md, Value editors."""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from functools import partial

import numpy as np
from PySide6.QtCore import QSignalBlocker, QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QSizePolicy,
                               QToolButton, QWidget)

lazy from file_handlers.clip.rules import PropertyType, property_type
lazy from ui.editor_widgets import EmbeddedPopupComboBox, set_invalid, styled_label, tool_button
lazy from utils.number_format import format_full_float

from ..rules import STRINGS, GuiLayout

_LINES = 6


class ValueEdit(QWidget):
    def __init__(self, get: Callable[[], object], apply: Callable[[object], None], parent: QWidget | None = None):
        super().__init__(parent)
        self._get, self._apply, self.live = get, apply, True
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(4)

    def refresh(self) -> None:
        self.show_value(self._get())

    def show_value(self, value) -> None:
        raise NotImplementedError

    def commit(self, value, widget: QWidget | None = None) -> None:
        if not self.live:
            return
        if value != self._get():
            self._apply(value)
        if widget is not None:
            set_invalid(widget, False)
        self.refresh()


class BoolEdit(ValueEdit):
    def __init__(self, get, apply, parent=None):
        super().__init__(get, apply, parent)
        self.box = QCheckBox(self)
        self.row.addWidget(self.box, 1)
        self.box.clicked.connect(self._clicked)
        self.refresh()

    def show_value(self, value) -> None:
        with QSignalBlocker(self.box):
            self.box.setChecked(bool(value))

    def _clicked(self, on: bool) -> None:
        self.commit(bool(on))


class _Line(QLineEdit):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setMinimumWidth(36)

    def put(self, text: str) -> None:
        if text != self.text():
            with QSignalBlocker(self):
                self.setText(text)
                self.setCursorPosition(0)


def _number(text: str, integer: bool):
    if integer:
        return int(text.strip(), 0)
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(text)
    return value


def _shown(value, integer: bool) -> str:
    if integer:
        return str(value)
    single = np.float32(value)
    return np.format_float_positional(single, trim="-") if float(single) == value else format_full_float(value)


class NumberEdit(ValueEdit):
    def __init__(self, integer: bool, get, apply, parent=None):
        super().__init__(get, apply, parent)
        self.integer, self.line = integer, _Line(self)
        self.row.addWidget(self.line, 1)
        self.line.editingFinished.connect(self._edited)
        self.refresh()

    def show_value(self, value) -> None:
        self.line.put(_shown(value, self.integer))

    def _edited(self) -> None:
        try:
            value = _number(self.line.text(), self.integer)
        except ValueError:
            set_invalid(self.line, True)
            return
        self.commit(value, self.line)


class VectorEdit(ValueEdit):
    def __init__(self, names: list[str], integer: bool, get, apply, parent=None):
        super().__init__(get, apply, parent)
        self.integer, self.lines = integer, []
        for name in names:
            self.row.addWidget(styled_label(name, self, tone="subtle"))
            line = _Line(self)
            line.editingFinished.connect(self._edited)
            self.lines.append(line)
            self.row.addWidget(line, 1)
        self.refresh()

    def show_value(self, value) -> None:
        for line, part in zip(self.lines, value):
            line.put(_shown(part, self.integer))

    def _edited(self) -> None:
        line = self.sender()
        if not self.live:
            return
        try:
            part = _number(line.text(), self.integer)
        except ValueError:
            set_invalid(line, True)
            return
        at = self.lines.index(line)
        self.commit([part if place == at else held for place, held in enumerate(self._get())], line)


class ColorEdit(VectorEdit):
    swatch: QToolButton | None = None

    def __init__(self, names: list[str], get, apply, parent=None):
        super().__init__(names, True, get, apply, parent)
        self.swatch = QToolButton(self)
        self.swatch.setAutoRaise(True)
        self.swatch.setIconSize(QSize(16, 16))
        self.swatch.setToolTip(self.tr("Choose the colour"))
        self.swatch.clicked.connect(self._choose)
        self.row.insertWidget(0, self.swatch)
        self.refresh()

    def show_value(self, value) -> None:
        super().show_value(value)
        if self.swatch is not None and all(type(part) is int and 0 <= part <= 255 for part in value):
            pixmap = QPixmap(16, 16)
            pixmap.fill(QColor(*value[:3]))
            self.swatch.setIcon(pixmap)

    def _choose(self) -> None:
        held = self._get()
        color = QColorDialog.getColor(QColor(*held), self.window(), self.tr("Colour"),
                                      QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid():
            self.commit([color.red(), color.green(), color.blue(), color.alpha()])


class EnumEdit(ValueEdit):
    def __init__(self, labels: tuple[str, ...], get, apply, parent=None, blank: str = ""):
        super().__init__(get, apply, parent)
        self.labels, self.box = labels, EmbeddedPopupComboBox(self)
        self.box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for label in labels:
            self.box.addItem(label or blank, label)
        self.row.addWidget(self.box, 1)
        self.box.activated.connect(self._chosen)
        self.refresh()

    def show_value(self, value) -> None:
        with QSignalBlocker(self.box):
            while self.box.count() > len(self.labels):
                self.box.removeItem(self.box.count() - 1)
            if value not in self.labels:
                self.box.addItem(str(value), value)
            self.box.setCurrentIndex(self.box.findData(value))

    def _chosen(self, index: int) -> None:
        self.commit(self.box.itemData(index))


class LineEdit(ValueEdit):
    def __init__(self, get, apply, parent=None, hint: str = ""):
        super().__init__(get, apply, parent)
        self.line = _Line(self)
        self.line.setPlaceholderText(hint)
        self.row.addWidget(self.line, 1)
        self.line.editingFinished.connect(self._edited)
        self.refresh()

    def show_value(self, value) -> None:
        self.line.put("" if value is None else str(value))

    def parsed(self, text: str):
        return text

    def _edited(self) -> None:
        try:
            value = self.parsed(self.line.text())
        except ValueError:
            set_invalid(self.line, True)
            return
        self.commit(value, self.line)


class GuidEdit(LineEdit):
    def parsed(self, text: str):
        return uuid.UUID(text.strip())


class _Text(QPlainTextEdit):
    def __init__(self, owner: TextEdit):
        super().__init__(owner)
        self.owner = owner
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(3)

    def fit(self) -> None:
        lines = min(max(self.document().blockCount(), 1), _LINES)
        self.setFixedHeight(self.fontMetrics().lineSpacing() * lines + 10)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.owner.edited()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.owner.edited()
            return
        super().keyPressEvent(event)
        self.fit()


class TextEdit(ValueEdit):
    def __init__(self, get, apply, parent=None):
        super().__init__(get, apply, parent)
        self.text = _Text(self)
        self.row.addWidget(self.text, 1)
        self.refresh()

    def show_value(self, value) -> None:
        shown = ("" if value is None else str(value)).replace("\r\n", "\n")
        if shown != self.text.toPlainText():
            with QSignalBlocker(self.text):
                self.text.setPlainText(shown)
        self.text.fit()

    def edited(self) -> None:
        if not self.live:
            return
        held, text = self._get() or "", self.text.toPlainText()
        if text != held.replace("\r\n", "\n"):
            self.commit(text.replace("\n", "\r\n") if "\r\n" in held else text)


def _picking(edit: LineEdit, pick: tuple | None) -> LineEdit:
    if pick is not None:
        edit.pick = tool_button("search", pick[0], parent=edit)
        edit.pick.clicked.connect(partial(pick[1], edit.pick))
        edit.row.addWidget(edit.pick)
    return edit


def value_edit(layout: GuiLayout, kind: PropertyType, labels: tuple[str, ...], get, apply,
               parent: QWidget | None = None, hint: str = "", lines: bool = False, blank: str = "",
               pick: tuple | None = None) -> ValueEdit:
    storage = layout.values[kind]
    names = [name for name, _kind in property_type(kind).members]
    if storage == "?":
        return BoolEdit(get, apply, parent)
    if kind == PropertyType.ENUM or labels:
        return EnumEdit(labels, get, apply, parent, blank)
    if kind == PropertyType.GUID:
        return _picking(GuidEdit(get, apply, parent), pick)
    if kind == PropertyType.ASSET:
        return _picking(LineEdit(get, apply, parent, hint), pick)
    if storage in STRINGS:
        return TextEdit(get, apply, parent) if lines else LineEdit(get, apply, parent)
    if kind == PropertyType.COLOR:
        return ColorEdit(names, get, apply, parent)
    if names:
        return VectorEdit(names, layout.payloads[storage][-1] not in "fd", get, apply, parent)
    return NumberEdit(storage != "d", get, apply, parent)
