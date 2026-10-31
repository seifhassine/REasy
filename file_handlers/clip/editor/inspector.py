from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
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

from .. import edits
from ..model import Clip, Key, Node, Property, SpeedPoint, Timeline, Track
from ..rules import display_names, interpolation, property_type
from ..values import value_problem
from .state import EditorState

AUTHORED_FLAGS = ("set_after_end_frame", "set_delegate_enable")
ENGINE_FLAGS = ("enum_closed", "restoration", "prev_diff_frame_set", "next_diff_frame_set", "prev_key_value_set",
                "delayed_execution", "array_count_set")


@dataclass(slots=True)
class Field:
    label: str
    kind: str
    get: Callable[[], object]
    set: Callable[[object], None] | None = None
    options: dict | None = None
    tooltip: str = ""


def _float_text(value: float) -> str:
    return format_full_float(value)


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
        return _float_text(value)
    if kind == "hex":
        return f"{value:08X}"
    if kind == "guid":
        return str(uuid.UUID(bytes_le=value))
    return str(value)


def _shape(fields: list[Field]) -> tuple:
    return tuple((field.label, field.kind, _shape(field.options["fields"]) if field.kind == "row" else
                  (tuple(field.options.items()), field.get() in field.options) if field.kind == "enum" else None)
                 for field in fields)


class Inspector(QWidget):
    def __init__(self, state: EditorState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self.setAttribute(Qt.WA_StyledBackground, True)
        set_style_role(self, surface="view")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.title = styled_label("", heading="panel")
        self.title.setContentsMargins(8, 6, 8, 2)
        layout.addWidget(self.title)
        self.banner = styled_label("", banner="danger")
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        layout.addWidget(self.scroll, 1)
        self._signature: tuple = ()
        self._updaters: list[Callable[[], None]] = []
        self._show_engine = False
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self._sync)
        state.selection_changed.connect(self._schedule)
        state.document.changed.connect(self._schedule)
        self.rebuild()

    def rebuild(self) -> None:
        item = self.state.current
        keys = self.state.keys()
        fields = self._multi_key_fields(keys) if len(keys) > 1 else self._fields(item)
        self._signature = (id(item), len(keys), _shape(fields))
        self._updaters = []
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(8, 4, 8, 8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for field in fields:
            if field.kind == "section":
                form.addRow(styled_label(field.label, heading="caption"))
                continue
            if field.kind == "toggle":
                button = DisclosureButton(field.label)
                button.setChecked(self._show_engine)
                button.toggled.connect(self._toggle_engine)
                form.addRow(button)
                continue
            widget = self._widget(field)
            label = QLabel(field.label)
            if field.tooltip:
                label.setToolTip(field.tooltip)
                widget.setToolTip(field.tooltip)
            form.addRow(label, widget)
        if self.scroll.widget() is not None:
            self.scroll.takeWidget().deleteLater()
        self.scroll.setWidget(body)
        self.title.setText(self._title(item, keys))
        self._update_banner(item)

    def _toggle_engine(self, shown: bool) -> None:
        self._show_engine = shown
        self.rebuild()

    def _title(self, item, keys: list) -> str:
        if len(keys) > 1:
            return self.tr("{0} keys").format(len(keys))
        names = {Timeline: self.tr("Timeline"), Track: self.tr("Track"), Clip: self.tr("Clip"), Node: self.tr("Node"),
                 Property: self.tr("Property"), Key: self.tr("Key"), SpeedPoint: self.tr("Speed Point")}
        return names.get(type(item), "")

    def _update_banner(self, item) -> None:
        messages = [issue.message for issue in self.state.document.issues() if issue.target is item]
        self.banner.setText("\n".join(messages))
        self.banner.setVisible(bool(messages))

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
            self._updaters.append(lambda: self._quiet(widget, widget.setChecked, bool(field.get())))
            return widget
        if field.kind == "enum":
            widget = EmbeddedPopupComboBox()
            for value, name in field.options.items():
                widget.addItem(name, value)
            current = int(field.get())
            if widget.findData(current) < 0:
                widget.addItem(f"0x{current:X}", current)
            widget.setCurrentIndex(widget.findData(current))
            widget.activated.connect(lambda _index: self._choose(field, widget.currentData()))
            self._updaters.append(lambda: self._quiet(widget, widget.setCurrentIndex,
                                                      widget.findData(int(field.get()))))
            return widget
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
        self._updaters.append(lambda: self._quiet(widget, widget.setText, _format(field.kind, field.get())))
        return widget

    def _row(self, field: Field) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for sub in field.options["fields"]:
            widget = self._widget(sub)
            if sub.label:
                row.addWidget(styled_label(sub.label, tone="subtle"))
            row.addWidget(widget, 1)
        return holder

    def _commit(self, widget: QLineEdit, field: Field) -> None:
        body = self.scroll.widget()
        if body is None or not body.isAncestorOf(widget):
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

    @staticmethod
    def _choose(field: Field, value) -> None:
        if value != field.get():
            field.set(value)

    @staticmethod
    def _quiet(widget: QWidget, setter, value) -> None:
        with QSignalBlocker(widget):
            setter(value)

    def _schedule(self, *_args) -> None:
        self._refresh.start()

    def _sync(self) -> None:
        item = self.state.current
        keys = self.state.keys()
        fields = self._multi_key_fields(keys) if len(keys) > 1 else self._fields(item)
        if (id(item), len(keys), _shape(fields)) != self._signature:
            self.rebuild()
            return
        for update in self._updaters:
            update()
        self._update_banner(item)

    def _set(self, target, text: str) -> Callable[[str], Callable[[object], None]]:
        document = self.state.document

        def setter(name: str) -> Callable[[object], None]:
            return lambda value: edits.set_fields(document, target, text, **{name: value})

        return setter

    def _stored(self, record: str, name: str) -> bool:
        return edits.stores(self.state.document, record, name)

    def _fields(self, item) -> list[Field]:
        if isinstance(item, Track):
            return self._track_fields(item)
        if isinstance(item, Clip):
            return self._clip_fields(item)
        if isinstance(item, Node):
            return self._node_fields(item)
        if isinstance(item, Property):
            return self._property_fields(item)
        if isinstance(item, (Key, SpeedPoint)):
            return self._key_fields(item)
        return self._timeline_fields(item)

    def _timeline_fields(self, timeline: Timeline) -> list[Field]:
        document = self.state.document
        profile = document.profile
        fields = [
            Field(self.tr("Version"), "readonly", lambda: f"{timeline.version} ({', '.join(profile.games)})"
                  + ("" if profile.checked else self.tr(", not checked against an executable"))),
            Field(self.tr("Resource"), "readonly", lambda: timeline.kind.value),
            Field(self.tr("Total Frame"), "float", lambda: timeline.total_frame,
                  lambda value: edits.set_fields(document, timeline, self.tr("Total Frame"), total_frame=value)),
        ]
        if "header_guid" in document.rules.header.names:
            fields.append(Field(self.tr("Header GUID"), "guid", lambda: timeline.header_guid,
                                lambda value: edits.set_fields(document, timeline, "GUID", header_guid=value),
                                tooltip=self.tr("Stored by the authoring tool; the game does not read it.")))
        if timeline.root_order is not None:
            fields.append(Field(self.tr("Root Order"), "readonly", lambda: self.tr("Differs from the track order")))
        return fields

    def _track_fields(self, track: Track) -> list[Field]:
        setter = self._set(track, self.tr("Edit Track"))
        fields = [
            Field(self.tr("Name"), "text", lambda: track.name,
                  lambda value: edits.rename(self.state.document, track, value)),
            Field(self.tr("Group"), "text", lambda: track.group, setter("group")),
            Field(self.tr("Enabled"), "bool", lambda: track.enabled, setter("enabled")),
        ]
        if self._stored("track", "tool_flag"):
            fields.append(Field(self.tr("Tool Flag"), "int", lambda: track.tool_flag, setter("tool_flag"),
                                tooltip=self.tr("Stored by the authoring tool; the game does not read it.")))
        return fields

    def _clip_fields(self, clip: Clip) -> list[Field]:
        setter = self._set(clip, self.tr("Edit Clip"))
        return [
            Field(self.tr("Name"), "text", lambda: clip.name, setter("name")),
            Field(self.tr("Frame In"), "float", lambda: clip.frame_in, setter("frame_in")),
            Field(self.tr("Frame Out"), "float", lambda: clip.frame_out, setter("frame_out")),
            Field(self.tr("Source In"), "float", lambda: clip.source_in, setter("source_in")),
            Field(self.tr("Source Out"), "float", lambda: clip.source_out, setter("source_out")),
        ]

    def _node_fields(self, node: Node) -> list[Field]:
        setter = self._set(node, self.tr("Edit Node"))
        fields = [
            Field(self.tr("Name"), "text", lambda: node.name,
                  lambda value: edits.rename(self.state.document, node, value)),
            Field(self.tr("Type"), "enum", lambda: node.node_type, setter("node_type"),
                  display_names("NodeType", self.state.document.timeline.version)),
            Field(self.tr("Begin"), "float", lambda: node.begin_frame, setter("begin_frame")),
            Field(self.tr("End"), "float", lambda: node.end_frame, setter("end_frame")),
            Field(self.tr("GUID"), "guid", lambda: node.guid, setter("guid"),
                  tooltip=self.tr("The GameObject a fixed bind finds this node's object by.")),
        ]
        if self._stored("node", "node_id"):
            fields.append(Field(self.tr("Node ID"), "guid", lambda: node.node_id, setter("node_id")))
        if self._stored("node", "tag"):
            fields.append(Field(self.tr("Tag"), "text", lambda: node.tag, setter("tag")))
        if self._stored("node", "editor_id"):
            fields.append(Field(self.tr("Editor ID"), "hex", lambda: node.editor_id, setter("editor_id"),
                                tooltip=self.tr("The authoring tool's ID; the game does not read it.")))
        if self._stored("node", "type_hash"):
            fields.append(Field(self.tr("Type Hash"), "hex", lambda: node.type_hash, setter("type_hash"),
                                tooltip=self.tr("Hash of the object's type name the game looks the type up by.")))
        return fields

    def _property_fields(self, prop: Property) -> list[Field]:
        document = self.state.document
        setter = self._set(prop, self.tr("Edit Property"))
        info = property_type(prop.type)
        keyed = info is not None and info.owns_keys(document.timeline.version)
        fields = [
            Field(self.tr("Name"), "text", lambda: prop.name, lambda value: edits.rename(document, prop, value)),
            Field(self.tr("Type"), "enum", lambda: prop.type, lambda value: edits.retarget(document, prop, value),
                  display_names("PropertyType", document.timeline.version)),
            Field(self.tr("Begin"), "float", lambda: prop.begin_frame, setter("begin_frame")),
            Field(self.tr("End"), "float", lambda: prop.end_frame, setter("end_frame")),
        ]
        parent = document.parent(prop)
        owner = parent[0] if parent else None
        if isinstance(owner, Property) and property_type(owner.type) is not None and \
                property_type(owner.type).children == "elements":
            fields.append(Field(self.tr("Array Index"), "int", lambda: prop.array_index, setter("array_index")))
        if self._stored("property", "editor_id"):
            fields.append(Field(self.tr("Editor ID"), "hex", lambda: prop.editor_id, setter("editor_id"),
                                tooltip=self.tr("The authoring tool's ID; the game does not read it.")))
        if keyed and document.rules.compact_keys and not info.key_storage:
            kinds = display_names("KeyType", document.timeline.version)
            compact = document.rules.key_tables.index(document.rules.compact_key_table)
            fields.append(Field(self.tr("Key Type"), "enum", lambda: compact if prop.compact_keys else 0,
                                lambda value: edits.set_key_storage(document, prop, value == compact),
                                {0: kinds[0], compact: kinds[compact]},
                                self.tr("{0} keys hold no rate or control points.").format(kinds[compact])))
        for flag in AUTHORED_FLAGS:
            if self._stored("property", flag):
                fields.append(Field(flag.replace("_", " ").capitalize(), "bool",
                                    lambda flag=flag: getattr(prop, flag), setter(flag)))
        engine = [flag for flag in ENGINE_FLAGS if self._stored("property", flag)]
        if engine:
            fields.append(Field(self.tr("Set by the game at load"), "toggle", lambda: None))
            if self._show_engine:
                fields += [Field(flag.replace("_", " ").capitalize(), "bool", lambda flag=flag: getattr(prop, flag),
                                 setter(flag), tooltip=self.tr("The game sets this from the property's attributes; "
                                                               "a set bit forces it on."))
                           for flag in engine]
        if keyed:
            fields.append(Field(self.tr("Keys"), "readonly", lambda: len(prop.keys)))
            slots = display_names("ExtraKeyType")
            count = 1 if document.rules.last_key_table else document.rules.extra_key_slots
            for slot in range(count):
                if slot not in prop.extra_keys:
                    fields.append(Field(slots.get(slot, str(slot)), "button", lambda: self.tr("Add"),
                                        lambda _value, slot=slot: self.state.select(
                                            [edits.set_extra_key(document, prop, slot)])))
            if info.speed_points:
                fields.append(Field(self.tr("Speed Points"), "button", lambda: self.tr("Add at playhead"),
                                    lambda _value: self.state.select([edits.add_speed_point(
                                        document, prop, self.state.frame - document.time_offset(prop))])))
        return fields

    def _key_fields(self, item: Key | SpeedPoint) -> list[Field]:
        document = self.state.document
        setter = self._set(item, self.tr("Edit Key"))
        prop = document.owner(item, Property)
        fields = [Field(self.tr("Frame"), "float", lambda: item.frame, setter("frame"))]
        if isinstance(item, Key) and prop is not None:
            fields += self._value_fields(item, prop)
        fields.append(Field(self.tr("Interpolation"), "enum", lambda: item.interpolation,
                            lambda value: edits.set_interpolation(document, [item], value),
                            edits.interpolations_for(document, [item])))
        record = document.rules.records["speed_point"] if isinstance(item, SpeedPoint) else \
            edits.key_record(document, prop, item)
        names = record.names
        if "width" in names:
            fields.append(Field(self.tr("Width"), "int", lambda: item.width, setter("width"),
                                tooltip=self.tr("The frame span RangeV2 keys use.")))
        if "rate" in names:
            fields.append(Field(self.tr("Rate"), "float", lambda: item.rate, setter("rate")))
        if "instance_value" in names:
            fields.append(Field(self.tr("Instance Value"), "bool", lambda: item.instance_value,
                                setter("instance_value")))
        info = interpolation(item.interpolation)
        if info is not None and info.control and item.control is not None:
            width = len(item.control)
            labels = ("start x", "start y", "end x", "end y") if width == 4 else \
                ("start x", "start y", "start z", "start w", "end x", "end y", "end z", "end w")

            def control_setter(position: int) -> Callable[[object], None]:
                return lambda value: edits.set_fields(
                    document, item, self.tr("Edit Control"),
                    control=tuple(value if index == position else old for index, old in enumerate(item.control)))

            fields.append(Field(self.tr("Control"), "row", lambda: None, options={"fields": [
                Field(labels[index], "float", lambda index=index: item.control[index], control_setter(index))
                for index in range(width)]}))
        return fields

    def _value_fields(self, key: Key, prop: Property) -> list[Field]:
        document = self.state.document
        codec = property_type(prop.type).value

        def apply(value) -> None:
            if problem := value_problem(codec, value):
                raise ValueError(problem)
            edits.set_fields(document, key, self.tr("Edit Value"), value=value)

        if codec.kind == "bool":
            return [Field(self.tr("Value"), "bool", lambda: key.value, apply)]
        if codec.kind in ("int", "float"):
            return [Field(self.tr("Value"), codec.kind, lambda: key.value, apply)]
        if codec.kind == "string":
            return [Field(self.tr("Value"), "text", lambda: "" if key.value is None else key.value, apply),
                    Field(self.tr("Null"), "bool", lambda: key.value is None,
                          lambda null: apply(None if null else ""),
                          tooltip=self.tr("No string: the file stores offset 0."))]
        if codec.kind == "row" and codec.table == "path_points":
            def axis(position: int) -> Callable[[object], None]:
                return lambda value: apply(tuple(float(value) if index == position else old
                                                 for index, old in enumerate(key.value)))

            return [Field(self.tr("Value"), "row", lambda: None, options={"fields": [
                Field(name, "float", lambda index=index: key.value[index], axis(index))
                for index, name in enumerate("xyz")]})]
        if codec.kind == "row":
            return [Field(self.tr("Asset Type"), "text", lambda: key.value[0],
                          lambda text: apply((text, key.value[1]))),
                    Field(self.tr("Asset Path"), "text", lambda: key.value[1],
                          lambda text: apply((key.value[0], text)))]
        return [Field(self.tr("Value"), "readonly", lambda: self.tr("Trigger"))]

    def _multi_key_fields(self, keys: list) -> list[Field]:
        document = self.state.document

        def current() -> int:
            modes = {key.interpolation for key in keys}
            return next(iter(modes)) if len(modes) == 1 else -1

        def apply(value: int) -> None:
            if value >= 0:
                edits.set_interpolation(document, keys, value)

        mixed = {-1: self.tr("(mixed)")} if current() < 0 else {}
        return [Field(self.tr("Interpolation"), "enum", current, apply,
                      {**mixed, **edits.interpolations_for(document, keys)})]
