"""See docs/internals/bhvt-editor.md, Object fields."""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from file_handlers.pyside.value_widgets import StringInput

lazy from file_handlers.pyside.tree_widget_factory import TreeWidgetFactory
lazy from file_handlers.pyside.value_widgets import EnumInput, FlagEnumInput
lazy from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, StructData, UserDataData
lazy from file_handlers.rsz.utils.rsz_field_utils import INTEGER_BOUNDS, is_type_assignable
lazy from ui.editor_widgets import DisclosureButton, ElidedLabel, styled_label, tool_button
lazy from ui.styles import set_style_role
lazy from utils.enum_manager import FlagMembers
lazy from utils.hex_util import guid_le_to_str

from .. import object_edits
from ..edits import Embedded
from ..references import base_name
from ..summaries import label
from ui.search_popup import below

from .pickers import type_entries, user_data_entries, variable_entries


class Fold(QWidget):
    toggled = Signal(bool)

    def __init__(self, parent: QWidget, *trailing: QWidget):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        row = QHBoxLayout()
        column.addLayout(row)
        row.setSpacing(2)
        self.button = DisclosureButton("", self)
        self.button.setMinimumWidth(40)
        self.detail = ElidedLabel(self)
        set_style_role(self.detail, tone="muted")
        self.note = QLabel(self)
        set_style_role(self.note, tone="info")
        self.note.hide()
        row.addWidget(self.button)
        row.addWidget(self.detail, 1)
        row.addWidget(self.note)
        for widget in trailing:
            row.addWidget(widget)
        self.body: QWidget | None = None
        self.button.toggled.connect(self._toggled)

    def set_text(self, text: str, detail: str = "", note: str = "", tip: str = "") -> None:
        self.button.setText(text)
        self.set_detail(detail, note, tip)

    def set_detail(self, detail: str, note: str = "", tip: str = "") -> None:
        self.detail.setText(detail)
        self.note.setText(note)
        self.note.setToolTip(tip)
        self.note.setVisible(bool(note))

    def set_body(self, body: QWidget) -> None:
        self.body = body
        self.layout().addWidget(body)

    def _toggled(self, on: bool) -> None:
        if self.body is not None:
            self.body.setVisible(on)
        self.toggled.emit(on)


class Text(StringInput):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.layout.setAlignment(Qt.AlignmentFlag(0))

    def _fit_width(self, text: str) -> None:
        self.line_edit.setMinimumWidth(self.minimum_width)


def _body(parent: QWidget) -> QFormLayout:
    form = QFormLayout(parent)
    form.setContentsMargins(14, 2, 0, 4)
    form.setVerticalSpacing(3)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    return form


class ObjectForm(QWidget):
    def __init__(self, state, popup, expanded: set, rsz, info: dict, fields: dict, parent: QWidget,
                 skip=frozenset(), fixed=frozenset()):
        super().__init__(parent)
        self.state, self.popup, self.expanded, self.rsz, self.info = state, popup, expanded, rsz, info
        self.fields = fields
        self._bound: dict = {}
        self._targets: dict = {}
        self._forms: list[ObjectForm] = []
        self._loading = False
        form = _body(self)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        summaries = state.summaries
        self._picks = {rule[1]: pick for rule, pick in ((summaries.variable, self._pick_variable),
                                                        (summaries.timeline, self._pick_timeline),
                                                        (summaries.hold_point, self._pick_hold_point))
                       if rule is not None and is_type_assignable(rsz.type_registry, info["name"], rule[0])}
        for key, field in fields.items():
            if base_name(key) not in skip:
                self._add(form, label(key), key, field, base_name(key) in fixed)

    @classmethod
    def of(cls, state, popup, expanded: set, rsz, index: int, parent: QWidget, skip=frozenset(),
           fixed=frozenset()) -> ObjectForm:
        return cls(state, popup, expanded, rsz, rsz.type_registry.get_type_info(rsz.instance_infos[index].type_id),
                   rsz.parsed_elements[index], parent, skip, fixed)

    def refresh(self) -> None:
        for widget, before in self._bound.items():
            if object_edits.snapshot(widget.get_data()) != before:
                self._load(widget, widget.get_data())
        for target, held in self._targets.items():
            if isinstance(target, QPushButton):
                self._show_named(target, held)
            elif isinstance(target, Fold):
                target.set_detail(self._detail(held[1]), *self._note(held[1]))
        for form in self._forms:
            form.refresh()

    def _detail(self, field) -> str:
        if isinstance(field, dict):
            return ", ".join(self.state.summaries.value_text(value, self.rsz) for value in field.values())
        if isinstance(field, (ArrayData, StructData)):
            return self.tr("{0} elements").format(len(field.values))
        return self.state.summaries.value_text(field, self.rsz)

    def _note(self, field) -> tuple[str, str]:
        count = self.state.uses(self.rsz)[field.value] if isinstance(field, UserDataData) and field.value else 1
        return (self.tr("shared by {0}").format(count), self.tr(
            "{0} fields hold this user data: an edit here is an edit for all of them").format(count)) \
            if count > 1 else ("", "")

    def _add(self, form: QFormLayout, text: str, key, field, fixed: bool = False, *trailing: QWidget) -> None:
        if isinstance(field, (ArrayData, StructData, dict)) or object_edits.classed(self.rsz, field):
            buttons = [] if isinstance(field, dict) else \
                [tool_button("add", self.tr("Add an element"), self._add_element, parent=self)] \
                if isinstance(field, (ArrayData, StructData)) else \
                [tool_button("edit", self.tr("Choose the user data") if isinstance(field, UserDataData) else
                             self.tr("Choose the class"), self._pick_class, parent=self),
                 tool_button("close", self.tr("Clear"), self._clear_object, parent=self)]
            fold = Fold(self, *buttons, *trailing)
            self._targets.update(dict.fromkeys((fold, *buttons), (key, field)))
            fold.set_text(text, self._detail(field), *self._note(field))
            filled = bool(field if isinstance(field, dict) else field.values if isinstance(
                field, (ArrayData, StructData)) else field.value)
            fold.button.setEnabled(filled)
            fold.toggled.connect(self._fold_toggled)
            fold.button.setChecked(filled and (id(self.fields), key) in self.expanded)
            form.addRow(fold)
            return
        editor = self._editor(key, field, fixed)
        if trailing:
            holder = QWidget(self)
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            for widget in (editor, *trailing):
                row.addWidget(widget)
            row.addStretch(1)
        form.addRow(text, holder if trailing else editor)

    def _fold_toggled(self, on: bool) -> None:
        fold = self.sender()
        key, field = self._targets[fold]
        (self.expanded.add if on else self.expanded.discard)((id(self.fields), key))
        if not on or fold.body is not None:
            return
        if isinstance(field, (ArrayData, StructData)):
            body = QWidget(fold)
            form = _body(body)
            for index, element in enumerate(field.values):
                remove = tool_button("remove", self.tr("Remove the element"), self._remove_element, parent=self)
                self._targets[remove] = (field, index)
                self._add(form, str(index), (key, index), element, False, remove)
        else:
            args = self.state, self.popup, self.expanded
            if isinstance(field, dict):
                body = ObjectForm(*args, self.rsz, self.rsz.type_registry.find_type_by_name(
                    self.fields[key[0]].orig_type)[0], field, fold)
            elif isinstance(field, ObjectData):
                body = ObjectForm.of(*args, self.rsz, field.value, fold)
            else:
                inner = Embedded(self.rsz, self.rsz._rsz_userdata_dict[field.value])
                body = ObjectForm.of(*args, inner, inner.object_table[0], fold)
            self._forms.append(body)
        fold.set_body(body)

    def _editor(self, key, field, fixed: bool) -> QWidget:
        summaries = self.state.summaries
        if isinstance(field, UserDataData) and not fixed:
            edit = QLineEdit(field.string.rstrip("\0"), self)
            edit.editingFinished.connect(self._userdata_edited)
            self._targets[edit] = field
            return edit
        if fixed:
            return styled_label(summaries.value_text(field, self.rsz), self, tone="muted")
        if (name := base_name(key if isinstance(key, str) else key[0])) in self._picks:
            button = QPushButton(self)
            set_style_role(button, compact=True)
            button.clicked.connect(self._picks[name])
            self._targets[button] = field
            self._show_named(button, field)
            return button
        members = summaries.enums.get(field.orig_type) if type(field).__name__ in INTEGER_BOUNDS else None
        kind = (FlagEnumInput if isinstance(members, FlagMembers) else EnumInput) if members else \
            TreeWidgetFactory.WIDGET_TYPES.get(type(field).__name__)
        if kind is None:
            return styled_label(summaries.value_text(field, self.rsz), self, tone="muted")
        widget = (Text if kind is StringInput else kind)(parent=self)
        self._load(widget, field)
        if members:
            widget.set_enum_values(members)
        widget.modified_changed.connect(self._edited)
        return widget

    def _load(self, widget, field) -> None:
        before = object_edits.snapshot(field)
        self._loading = True
        widget.set_data(field)
        self._loading = False
        object_edits.restore(before)
        self._bound[widget] = before

    def _edited(self, _modified: bool) -> None:
        widget = self.sender()
        if self._loading:
            return
        data = widget.get_data()
        if object_edits.commit(self.state.document, data, self._bound[widget], self.tr("Edit field"),
                               ("field", id(data))):
            self._bound[widget] = object_edits.snapshot(data)

    def _show_named(self, button: QPushButton, field) -> None:
        button.setText(self.state.summaries.named(field.raw_bytes) or (
            field.guid_str if any(field.raw_bytes) else self.tr("Choose...")))

    def _pick_variable(self) -> None:
        self.popup.pick(variable_entries(self.state.document, self.state.model, self.state.summaries),
                        below(self.sender()), self.tr("Variable"),
                        partial(self._set_guid, self._targets[self.sender()], self.tr("Set variable")))

    def _pick_timeline(self) -> None:
        self.popup.pick([(name, "", guid) for guid, name in self.state.summaries.timelines.items()],
                        below(self.sender()), self.tr("Timeline"),
                        partial(self._set_guid, self._targets[self.sender()], self.tr("Set timeline")))

    def _pick_hold_point(self) -> None:
        self.popup.pick([(name, "", guid) for guid, name in self.state.summaries.holds.items()],
                        below(self.sender()), self.tr("Hold point"),
                        partial(self._set_guid, self._targets[self.sender()], self.tr("Set hold point")))

    def _set_guid(self, field, text: str, guid: bytes) -> None:
        before = object_edits.snapshot(field)
        field.raw_bytes, field.guid_str = guid, guid_le_to_str(guid)
        object_edits.commit(self.state.document, field, before, text)

    def _userdata_edited(self) -> None:
        field = self._targets[self.sender()]
        if self.sender().text() != field.string.rstrip("\0"):
            object_edits.set_userdata(self.state.document, self.state.model, self.rsz, field,
                                      self.sender().text(), self.tr("Set user data"))

    def _pick(self, field, receiver) -> None:
        array = isinstance(field, ArrayData)
        users = (field.element_class if array else type(field)) is UserDataData
        held = user_data_entries(self.rsz, field.orig_type, self.state.summaries.user_path,
                                 0 if array else field.value) if users else []
        self.popup.pick([*held, *type_entries(self.rsz, field.orig_type)], below(self.sender()),
                        self.tr("User data") if users else self.tr("Class"), receiver)

    def _pick_class(self) -> None:
        field = self._targets[self.sender()][1]
        self._pick(field, partial(self._set_object, field))

    def _set_object(self, field, picked) -> None:
        object_edits.set_object(self.state.document, self.state.model, self.rsz, field, picked,
                                self.tr("Set object"))

    def _clear_object(self) -> None:
        if (field := self._targets[self.sender()][1]).value:
            self._set_object(field, None)

    def _add_element(self) -> None:
        key, array = self._targets[self.sender()]
        if object_edits.classed(self.rsz, array):
            self._pick(array, partial(self._append, key, array))
        else:
            self._append(key, array)

    def _append(self, key: str, array, picked=None) -> None:
        size = next(item["size"] for item in self.info["fields"] if item["name"] == key)
        element, staged = object_edits.new_element(self.rsz, array, size, picked)
        self.expanded.add((id(self.fields), key))
        object_edits.set_elements(self.state.document, self.state.model, self.rsz, array,
                                  [*array.values, element], self.tr("Add element"), staged)

    def _remove_element(self) -> None:
        array, index = self._targets[self.sender()]
        object_edits.set_elements(self.state.document, self.state.model, self.rsz, array,
                                  array.values[:index] + array.values[index + 1:], self.tr("Remove element"))
