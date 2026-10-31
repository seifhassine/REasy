"""Wwise-style property editor for compiled Wwise objects and bank chunks. See docs/internals/wwise-property-pages.md."""

from __future__ import annotations

import math
import re
import struct

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyledItemDelegate,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

lazy from .bnk_parser import (
    HircObject,
    HircReference,
    compatible_hirc_reference_types,
    wwise_id_from_name,
    _decode_one_hirc_object,
    _resolve_fx_state_property_names,
)
from .sound_metadata import SoundMetadata
from .wwise_pages import PageControl, chunk_pages, keyed_path, object_pages, split_unit
from .wwise_schema import (
    BNK_CURVE_INTERPOLATION,
    BNK_FX_ENUMS,
    BNK_PLUGIN_NAMES,
    action_types,
    integer_properties,
    property_default,
    property_names,
)
lazy from .wwise_v132 import (
    WwiseDecisionPath,
    decision_tree,
    parse_structured_chunk,
    parse_structured_object,
    reorder_wwise_list,
    set_decision_tree,
    set_wwise_fields,
    set_wwise_keyed_values,
    wwise_lists,
)
lazy from ui.editor_widgets import styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, qcolor, set_style_role


_RANGES = {
    "u8": (0, 0xFF), "i8": (-0x80, 0x7F), "u16": (0, 0xFFFF), "i16": (-0x8000, 0x7FFF),
    "u32": (0, 0xFFFFFFFF), "i32": (-0x80000000, 0x7FFFFFFF), "var": (0, 0xFFFFFFFF), "bool": (0, 1),
}
_SYNC_KINDS = frozenset({
    "state_group", "state_value", "switch_group", "switch_value", "game_parameter", "trigger", "bank",
    "plugin", "language", "midi_parameter", "acoustic_texture", "cue",
})
_CHILD_ROLES = frozenset({
    "playlist item", "switch assignment", "switch child", "layer child", "decision leaf", "playlist segment",
})
_ENTRY_CUE, _EXIT_CUE = 43_573_010, 1_539_036_744
_POINT_FROM = re.compile(r"^(.*)/Points/\d+/From$")
_DIGITS = re.compile(r"\s*\d+$")
_STATE_VALUES = re.compile(r"/State \d+/Property count$")
_PROPERTY_CHOICES = {
    "Fade In Curve": BNK_CURVE_INTERPOLATION, "Fade Out Curve": BNK_CURVE_INTERPOLATION,
    "Crossfade Up Curve": BNK_CURVE_INTERPOLATION, "Crossfade Down Curve": BNK_CURVE_INTERPOLATION,
    "LFO: waveform": dict(enumerate(BNK_FX_ENUMS["waveform"])),
    "Scope": {0: "Voice", 1: "Note or event", 2: "Game object", 3: "Global"},
}
_PROPERTY_BOOLS = frozenset({
    "Envelope: stop playback", "LFO: retrigger", "Bypass All FX", "Bypass All Metadata", "Attenuation Enabled",
})
_PROPERTY_PICKERS = {
    "Attenuation ShortID": "attenuation", "MIDI Target Node": "MIDI target", "Attached Plug-in FX": "attached effect",
}
_SECONDS = frozenset({"Delay Time (ms)", "Transition Time (ms)"})
_PROPERTY_UNITS = {
    **dict.fromkeys((
        "Output Bus Volume", "Game Aux Send", "Game Aux Send Volume", "User Aux Send", "User Aux Send Volume",
        "Reflections Bus Volume", "HDR Bus Threshold", "HDR Active Range", "Feedback Volume", "Cone Attenuation",
    ), "dB"),
    "HDR Bus Release Time": "s",
}
_CHANNEL_PRESETS = {
    0: "Same as parent", 3584: "Same as main mix", 3840: "Same as passthrough mix", 768: "Audio objects",
    16641: "1.0", 12546: "2.0", 45315: "2.1", 28931: "3.0", 6304004: "4.0", 6353158: "5.1", 6549768: "7.1",
    90239240: "5.1.2", 90435850: "7.1.2", 761524492: "7.1.4", 516: "Ambisonics 1st order",
    521: "Ambisonics 2nd order", 528: "Ambisonics 3rd order", 537: "Ambisonics 4th order",
    548: "Ambisonics 5th order", 761327882: "Auro 9.1", 769716491: "Auro 10.1", 803270924: "Auro 11.1",
    803467534: "Auro 13.1", 33025: "LFE",
}
_CURVE_SHAPES = {
    0: lambda v: v ** 3, 1: lambda v: math.sin(v * math.pi / 2), 2: lambda v: v ** 1.41,
    3: lambda v: 0.5 + math.asin(2 * v - 1) / math.pi, 4: lambda v: v, 5: lambda v: (1 - math.cos(v * math.pi)) / 2,
    6: lambda v: 1 - (1 - v) ** 1.41, 7: lambda v: 1 - math.cos(v * math.pi / 2), 8: lambda v: 1 - (1 - v) ** 3,
    9: lambda v: 0.0,
}


def _join(*parts):
    return "/".join(part for part in parts if part)


def _float_text(value, storage="f32"):
    if storage == "f32":
        packed = struct.pack("<f", value)
        value = next(
            float(text) for text in (f"{value:.{digits}g}" for digits in range(1, 10))
            if struct.pack("<f", float(text)) == packed
        )
    return repr(float(value)).removesuffix(".0")


def _inside(path, root):
    return path == root or path.startswith(root + "/")


def _button(icon, tooltip, parent):
    button = QToolButton(parent)
    button.setIcon(line_icon(icon))
    button.setToolTip(tooltip)
    button.setAutoRaise(True)
    return button


def _is_bool(choices):
    return bool(choices) and 1 in choices and set(choices.values()) <= {"No", "Yes", "Yes (0xFF encoding)"}


def _choices(field, hints):
    choices = hints.get("choices")
    return choices if choices is not None else dict(field.enum) if field.enum else None


def _bundle(fields, section, kind="Properties"):
    prefix = _join(section, kind) + "/"
    ids, values = [], []
    for field in fields:
        if field.path.startswith(prefix) and not field.label.endswith("count"):
            (ids if field.label.endswith(" type") else values).append(field.value)
    if kind == "Randomizers":
        values = list(zip(values[::2], values[1::2]))
    return dict(zip((int(value) for value in ids), values))


class _Context:
    def __init__(self, metadata, objects, version, owner):
        self.metadata, self.objects, self.version, self.owner = metadata, tuple(objects), version, owner
        self.by_id = {}
        for obj in self.objects:
            self.by_id.setdefault(obj.object_id, []).append(obj)
        self._known = {}

    def known(self, kind):
        if kind not in self._known:
            values = set()
            for obj in self.objects:
                for field in obj.structure.fields if obj.structure is not None else ():
                    if field.id_kind == kind and int(field.value):
                        values.add(int(field.value))
            self._known[kind] = sorted(values)
        return self._known[kind]

    def object_label(self, object_id, none_label="None"):
        if not object_id or object_id == 0xFFFFFFFF and none_label != "Any":
            return none_label
        if object_id == 0xFFFFFFFF:
            return "Any"
        local = self.by_id.get(object_id, ())
        if not local:
            return self.metadata.external_object_label(object_id)
        names = self.metadata.event_names(object_id) if any(obj.type_id == 0x04 for obj in local) else ()
        kinds = " / ".join(dict.fromkeys(obj.type_name for obj in local))
        return f"{kinds} {object_id}" + (f" · {' / '.join(names)}" if names else "")

    def targets(self, role, children=False):
        owner = self.owner
        if owner is None:
            return []
        if children and role in _CHILD_ROLES:
            return [self.by_id[value][0] for value in owner.child_ids if value in self.by_id]
        roles = ("output bus", "MIDI target") if role == "exception" else (role,)
        allowed = frozenset().union(*(compatible_hirc_reference_types(owner, HircReference(0, 0, item)) for item in roles))
        return [
            obj for obj in self.objects
            if obj.type_id in allowed and len(self.by_id[obj.object_id]) == 1
            and (obj.object_id, obj.type_id) != (owner.object_id, owner.type_id)
        ]


class _Number(QDoubleSpinBox):
    committed = Signal()

    def __init__(self, value, storage, parent=None, *, suffix="", scale=1.0, maximum=None):
        super().__init__(parent)
        self._scale, self._integral, self._storage = scale, storage in _RANGES or storage == "bit", storage
        low, high = _RANGES.get(storage, (0, maximum) if storage == "bit" else (-3.4e38, 3.4e38))
        self.setDecimals(3 if scale != 1 else 0 if self._integral else 6)
        self.setRange(low * scale, high * scale)
        self.setSuffix(f" {suffix}" if suffix else "")
        self.setKeyboardTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(90)
        self.set_raw(value)
        self.valueChanged.connect(self._changed)

    def textFromValue(self, value):
        if self._integral or self._scale != 1:
            text = f"{value:.{self.decimals()}f}"
            return text.rstrip("0").rstrip(".") if "." in text else text
        return _float_text(value, self._storage)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def set_raw(self, value):
        self.blockSignals(True)
        self.setValue(float(value) * self._scale)
        self.blockSignals(False)
        self._shown = self.value()

    def raw(self):
        value = self.value() / self._scale
        return int(round(value)) if self._integral else value

    def _changed(self, _value):
        if self.value() != self._shown:
            self._shown = self.value()
            self.committed.emit()


class _Choice(QComboBox):
    committed = Signal()

    def __init__(self, choices, value, parent=None):
        super().__init__(parent)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(10)
        for number, label in choices.items():
            self.addItem(self.tr(label), int(number))
        self.set_raw(value)
        self.activated.connect(self._activated)

    def set_raw(self, value):
        if self.findData(int(value)) < 0:
            self.addItem(self.tr("Unknown ({value})").format(value=int(value)), int(value))
        self.setCurrentIndex(self.findData(int(value)))
        self._shown = int(value)

    def raw(self):
        return int(self.currentData())

    def _activated(self, _index):
        if self.raw() != self._shown:
            self._shown = self.raw()
            self.committed.emit()


class _Radio(QWidget):
    committed = Signal()

    def __init__(self, choices, value, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.group = QButtonGroup(self)
        for number, label in choices.items():
            button = QRadioButton(self.tr(label), self)
            self.group.addButton(button, int(number))
            row.addWidget(button)
        row.addStretch()
        self.set_raw(value)
        self.group.idClicked.connect(self._clicked)

    def set_raw(self, value):
        button = self.group.button(int(value))
        if button is not None:
            button.setChecked(True)
        self._shown = int(value)

    def raw(self):
        return self.group.checkedId()

    def _clicked(self, _value):
        if self.raw() != self._shown:
            self._shown = self.raw()
            self.committed.emit()


class _Check(QCheckBox):
    committed = Signal()

    def __init__(self, label, value, parent=None):
        super().__init__(label, parent)
        self.set_raw(value)
        self.clicked.connect(self.committed)

    def set_raw(self, value):
        self.setChecked(bool(int(value)))

    def raw(self):
        return int(self.isChecked())


class _Text(QLineEdit):
    committed = Signal()

    def __init__(self, value, parent=None, *, mono=False):
        super().__init__(parent)
        if mono:
            set_style_role(self, mono=True)
        self.set_raw(value)
        self.editingFinished.connect(self._finished)

    def set_raw(self, value):
        self.setText(str(value))
        self._shown = self.text()

    def raw(self):
        return self.text()

    def _finished(self):
        if self.text() != self._shown:
            self._shown = self.text()
            self.committed.emit()


class _ReadOnly(QLabel):
    committed = Signal()

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        set_style_role(self, tone="muted")

    def set_raw(self, _value):
        pass


class _Picker(QComboBox):
    committed = Signal()

    def __init__(self, value, items, label, resolve, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)
        self._items, self._label, self._resolve, self._filled = items, label, resolve, False
        self.set_raw(value)
        self.activated.connect(self._finish)
        self.lineEdit().editingFinished.connect(self._finish)

    def showPopup(self):
        if not self._filled:
            self._filled = True
            text = self.currentText()
            self.blockSignals(True)
            self.clear()
            for value in dict.fromkeys(self._items()):
                self.addItem(self._label(value), int(value))
            self.setEditText(text)
            self.blockSignals(False)
        super().showPopup()

    def set_raw(self, value):
        self._shown = int(value)
        self.blockSignals(True)
        self.setEditText(self._label(int(value)))
        self.blockSignals(False)

    def raw(self):
        text = self.currentText().strip()
        if text == self._label(self._shown):
            return self._shown
        index = self.findText(text)
        return int(self.itemData(index)) if index >= 0 else self._resolve(text)

    def _finish(self, *_args):
        try:
            value = self.raw()
        except ValueError:
            set_style_role(self, invalid=True)
            return
        set_style_role(self, invalid=False)
        if value != self._shown:
            self._shown = value
            self.committed.emit()
        self.set_raw(value)


def _number_id(text):
    return int(text, 0) & 0xFFFFFFFF


def _object_picker(value, role, hints, ctx, parent):
    none_label = hints.get("none_label", "Any" if role in {"transition source", "transition destination"} else "None")
    return _Picker(
        value,
        lambda: [0, *(obj.object_id for obj in ctx.targets(role, hints.get("children", False)))],
        lambda object_id: ctx.object_label(object_id, none_label),
        _number_id, parent,
    )


def _sync_picker(value, kind, ctx, parent, choices=None):
    choices = dict(choices or {})

    def items():
        if kind == "plugin":
            return [key for key in BNK_PLUGIN_NAMES if key & 0x0F == value & 0x0F]
        return [*choices, *ctx.known(kind)]

    def label(number):
        if number in choices:
            return choices[number]
        return BNK_PLUGIN_NAMES.get(number) or str(number) if kind == "plugin" else ctx.metadata.id_label(kind, number)

    def resolve(text):
        named = next((number for number, name in choices.items() if name == text), None)
        return named if named is not None else ctx.metadata.resolve_id(kind, text)

    return _Picker(value, items, label, resolve, parent)


def _display(field, ctx, hints):
    value = field.value
    choices = _choices(field, hints)
    if field.id_kind in {"hirc", "event"} and field.reference_role:
        roles = {"transition source", "transition destination"}
        return ctx.object_label(int(value), hints.get("none_label", "Any" if field.reference_role in roles else "None"))
    if field.id_kind == "plugin":
        return BNK_PLUGIN_NAMES.get(int(value)) or f"0x{int(value):08X}"
    if field.id_kind in _SYNC_KINDS:
        return (choices or {}).get(int(value)) or ctx.metadata.id_label(field.id_kind, int(value))
    if choices and isinstance(value, int) and value in choices:
        return choices[value]
    if isinstance(value, str):
        return value
    unit = hints.get("suffix", split_unit(field.label)[1])
    scale = hints.get("scale", 1.0)
    if scale != 1:
        text = f"{value * scale:.3f}".rstrip("0").rstrip(".")
    else:
        text = _float_text(value, field.storage) if isinstance(value, float) else str(value)
    return f"{text} {unit}".strip()


def _input(field, hints, ctx, parent, label=""):
    choices = _choices(field, hints)
    if not field.editable or hints.get("readonly"):
        return _ReadOnly(_display(field, ctx, hints), parent), False
    if field.storage == "string":
        return _Text(field.value, parent), False
    if field.storage == "bytes":
        return _Text(field.value, parent, mono=True), False
    if field.id_kind in {"hirc", "event"} and field.reference_role:
        return _object_picker(int(field.value), field.reference_role, hints, ctx, parent), False
    if field.id_kind in _SYNC_KINDS:
        return _sync_picker(int(field.value), field.id_kind, ctx, parent, choices), False
    if hints.get("radio") and choices:
        return _Radio(choices, field.value, parent), False
    if _is_bool(choices) and "choices" not in hints:
        return _Check(label, field.value, parent), True
    if choices:
        return _Choice(choices, field.value, parent), False
    return _Number(
        field.value, field.storage, parent, suffix=hints.get("suffix", split_unit(field.label)[1]),
        scale=hints.get("scale", 1.0), maximum=field.mask >> field.shift if field.storage == "bit" else None,
    ), False


class _Randomizer(QToolButton):
    committed = Signal()

    def __init__(self, storage, parent=None, *, scale=1.0, suffix=""):
        super().__init__(parent)
        self.setIcon(line_icon("dice"))
        self.setAutoRaise(True)
        self.setCheckable(True)
        self.setToolTip(self.tr("Randomizer"))
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setStyleSheet("QToolButton { padding-right: 3px; } QToolButton::menu-indicator { image: none; width: 0px; }")
        menu = QMenu(self)
        box = QWidget(menu)
        form = QFormLayout(box)
        form.setContentsMargins(8, 6, 8, 6)
        self.enabled = QCheckBox(self.tr("Enabled"), box)
        self.minimum = _Number(0, storage, box, suffix=suffix, scale=scale)
        self.maximum = _Number(0, storage, box, suffix=suffix, scale=scale)
        form.addRow(self.enabled)
        form.addRow(self.tr("Minimum offset"), self.minimum)
        form.addRow(self.tr("Maximum offset"), self.maximum)
        action = QWidgetAction(menu)
        action.setDefaultWidget(box)
        menu.addAction(action)
        menu.aboutToHide.connect(self._closed)
        self.setMenu(menu)
        self._shown = None

    def set_range(self, value):
        self._shown = value
        self.enabled.setChecked(value is not None)
        self.setChecked(value is not None)
        self.minimum.set_raw(value[0] if value else 0)
        self.maximum.set_raw(value[1] if value else 0)

    def raw(self):
        return (self.minimum.raw(), self.maximum.raw()) if self.enabled.isChecked() else None

    def _closed(self):
        if self.raw() != self._shown:
            self._shown = self.raw()
            self.setChecked(self._shown is not None)
            self.committed.emit()


class _PropertyRow(QWidget):
    edit = Signal(object)

    def __init__(self, section, prop_id, name, kind, version, ranged, ctx, parent=None):
        super().__init__(parent)
        self.section, self.prop_id = section, prop_id
        self.default = property_default(kind, version, prop_id)
        self.integer = prop_id in integer_properties(kind, version)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        unit = "s" if name in _SECONDS else _PROPERTY_UNITS.get(_DIGITS.sub("", name), split_unit(name)[1])
        scale = 0.001 if name in _SECONDS else 1.0
        storage = "i32" if self.integer else "f32"
        if name in _PROPERTY_PICKERS:
            self.input = _object_picker(0, _PROPERTY_PICKERS[name], {}, ctx, self)
        elif name == "HDR Bus Game Parameter":
            self.input = _sync_picker(0, "game_parameter", ctx, self)
        elif name in _PROPERTY_CHOICES:
            self.input = _Choice(_PROPERTY_CHOICES[name], self.default or 0, self)
        elif name in _PROPERTY_BOOLS:
            self.input = _Check("", 0, self)
        else:
            self.input = _Number(0, storage, self, suffix=unit, scale=scale)
        self.input.committed.connect(self._value_committed)
        row.addWidget(self.input, 1)
        self.random = None
        if ranged:
            self.random = _Randomizer(storage, self, scale=scale, suffix=unit)
            self.random.committed.connect(self._range_committed)
            row.addWidget(self.random)
        self.reset = _button("reset", self.tr("Reset to the default value"), self)
        self.reset.clicked.connect(self._reset)
        row.addWidget(self.reset)

    def _shown(self, value):
        return value - 0x100000000 if self.integer and isinstance(self.input, _Number) and value >= 0x80000000 else value

    def sync(self, values, ranges):
        value = values.get(self.prop_id, self.default)
        self.input.set_raw(self._shown(value if value is not None else 0))
        self.reset.setVisible(self.prop_id in values or self.prop_id in ranges)
        if self.random is not None:
            current = ranges.get(self.prop_id)
            self.random.set_range(tuple(map(self._shown, current)) if current else None)

    def _stored(self, value):
        return value & 0xFFFFFFFF if self.integer else value

    def _value_committed(self):
        self.edit.emit(("props", self.section, {self.prop_id: self._stored(self.input.raw())}))

    def _range_committed(self):
        value = self.random.raw()
        self.edit.emit(("ranges", self.section, {self.prop_id: tuple(map(self._stored, value)) if value else None}))

    def _reset(self):
        self.edit.emit(("props", self.section, {self.prop_id: None}))


class _CurveView(QWidget):
    moved = Signal(int, float, float)
    added = Signal(float, float)
    removed = Signal(int)
    shaped = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(260, 170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.points, self._drag = [], None

    def set_points(self, points):
        self.points = [tuple(point) for point in points]
        self.update()

    def _bounds(self):
        xs = [point[0] for point in self.points] or [0.0, 1.0]
        ys = [point[1] for point in self.points] or [0.0, 1.0]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        if x1 - x0 < 1e-9:
            x0, x1 = x0 - 1, x1 + 1
        if y1 - y0 < 1e-9:
            y0, y1 = y0 - 1, y1 + 1
        pad = (y1 - y0) * 0.08
        return x0, x1, y0 - pad, y1 + pad

    def _area(self):
        return QRectF(self.rect()).adjusted(48, 10, -12, -22)

    def _screen(self, x, y, bounds):
        x0, x1, y0, y1 = bounds
        area = self._area()
        return QPointF(area.left() + (x - x0) / (x1 - x0) * area.width(), area.bottom() - (y - y0) / (y1 - y0) * area.height())

    def _value(self, point, bounds):
        x0, x1, y0, y1 = bounds
        area = self._area()
        return (
            x0 + (point.x() - area.left()) / max(area.width(), 1) * (x1 - x0),
            y0 + (area.bottom() - point.y()) / max(area.height(), 1) * (y1 - y0),
        )

    def paintEvent(self, _event):
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), qcolor(colors["canvas_bg"]))
        area, bounds = self._area(), self._bounds()
        painter.setPen(QPen(qcolor(colors["canvas_grid_minor"]), 1))
        for step in range(1, 4):
            y, x = area.top() + area.height() * step / 4, area.left() + area.width() * step / 4
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
        painter.setPen(QPen(qcolor(colors["canvas_grid_major"]), 1))
        painter.drawRect(area)
        painter.setPen(qcolor(colors["text_muted"]))
        x0, x1, _y0, _y1 = bounds
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        ys = [point[1] for point in self.points] or [0.0]
        for value in {min(ys), max(ys)}:
            painter.drawText(QRectF(0, self._screen(x0, value, bounds).y() - 8, 44, 16), right, f"{value:.4g}")
        painter.drawText(QRectF(area.left(), area.bottom() + 4, 80, 16), Qt.AlignmentFlag.AlignLeft, f"{x0:.4g}")
        painter.drawText(QRectF(area.right() - 80, area.bottom() + 4, 80, 16), Qt.AlignmentFlag.AlignRight, f"{x1:.4g}")
        if not self.points:
            return
        path = QPainterPath(self._screen(*self.points[0][:2], bounds))
        for (xa, ya, shape), (xb, yb, _next) in zip(self.points, self.points[1:]):
            curve = _CURVE_SHAPES.get(shape, _CURVE_SHAPES[4])
            for step in range(1, 25):
                t = step / 24
                path.lineTo(self._screen(xa + (xb - xa) * t, ya + (yb - ya) * curve(t), bounds))
        painter.setPen(QPen(qcolor(colors["accent"]), 2))
        painter.drawPath(path)
        painter.setPen(QPen(qcolor(colors["text_bright"]), 1))
        painter.setBrush(qcolor(colors["accent"]))
        for x, y, _shape in self.points:
            painter.drawEllipse(self._screen(x, y, bounds), 4, 4)

    def _hit(self, position):
        bounds = self._bounds()
        for index, (x, y, _shape) in enumerate(self.points):
            point = self._screen(x, y, bounds)
            if abs(point.x() - position.x()) <= 6 and abs(point.y() - position.y()) <= 6:
                return index
        return None

    def mousePressEvent(self, event):
        index = self._hit(event.position())
        if index is None:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = (index, self._bounds())
            return
        menu = QMenu(self)
        shapes = menu.addMenu(self.tr("Interpolation to next point"))
        for value, label in BNK_CURVE_INTERPOLATION.items():
            action = shapes.addAction(self.tr(label))
            action.setData(value)
            action.setCheckable(True)
            action.setChecked(self.points[index][2] == value)
        delete = menu.addAction(line_icon("delete"), self.tr("Delete point"))
        delete.setEnabled(len(self.points) > 1)
        chosen = menu.exec(event.globalPosition().toPoint())
        menu.deleteLater()
        if chosen is delete:
            self.removed.emit(index)
        elif chosen is not None and chosen.data() is not None:
            self.shaped.emit(index, int(chosen.data()))

    def mouseMoveEvent(self, event):
        if self._drag is None:
            hit = self._hit(event.position()) is not None
            self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)
            return
        index, bounds = self._drag
        self.points[index] = (*self._value(event.position(), bounds), self.points[index][2])
        self.update()

    def mouseReleaseEvent(self, _event):
        if self._drag is not None:
            index, _bounds = self._drag
            self._drag = None
            self.moved.emit(index, *self.points[index][:2])

    def mouseDoubleClickEvent(self, event):
        if self._hit(event.position()) is None and self._area().contains(event.position()):
            self.added.emit(*self._value(event.position(), self._bounds()))


class _Delegate(QStyledItemDelegate):
    def createEditor(self, parent, _option, index):
        table = self.parent()
        field, hints = table.cell(index.row(), index.column())
        if field is None or isinstance(field, str) or not field.editable or hints.get("readonly"):
            return None
        editor, own = _input(field, hints, table.ctx, parent)
        if own:
            editor.deleteLater()
            return None
        editor.setProperty("wwise_path", field.path)
        editor.committed.connect(table.cell_committed)
        return editor

    def setEditorData(self, _editor, _index):
        pass

    def setModelData(self, _editor, _model, _index):
        pass


class _Table(QTableWidget):
    edit = Signal(object)
    current = Signal(int)

    def __init__(self, columns, rows, ctx, parent=None):
        super().__init__(0, len(columns), parent)
        self.ctx, self.columns = ctx, columns
        self.setHorizontalHeaderLabels([self.tr(split_unit(label)[0]) for label, _hints in columns])
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setStretchLastSection(True)
        self.verticalHeader().setDefaultSectionSize(24)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.setItemDelegate(_Delegate(self))
        self.fill(rows)
        self.resizeColumnsToContents()
        self.setMinimumHeight(min(60 + 24 * len(rows), 340) if rows else 56)
        self.setMaximumHeight(16777215 if rows else 72)
        self.itemChanged.connect(self._item_changed)
        self.currentCellChanged.connect(self._current_changed)

    def cell(self, row, column):
        label, hints = self.columns[column]
        return self.rows[row].get(label), hints

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.rowCount():
            painter = QPainter(self.viewport())
            painter.setPen(qcolor(current_colors()["text_muted"]))
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, self.tr("Empty. Add an item with +."))

    def fill(self, rows):
        self.rows = rows
        self.blockSignals(True)
        self.setRowCount(len(rows))
        enabled = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        for row in range(len(rows)):
            for column in range(len(self.columns)):
                field, hints = self.cell(row, column)
                item = QTableWidgetItem()
                editable = field is not None and not isinstance(field, str) and field.editable and not hints.get("readonly")
                if isinstance(field, str) or field is None:
                    item.setText(field or "")
                    item.setFlags(enabled)
                elif _is_bool(_choices(field, hints)) and "choices" not in hints:
                    item.setFlags(enabled | (Qt.ItemFlag.ItemIsUserCheckable if editable else Qt.ItemFlag.NoItemFlags))
                    item.setCheckState(Qt.CheckState.Checked if int(field.value) else Qt.CheckState.Unchecked)
                else:
                    item.setText(_display(field, self.ctx, hints))
                    item.setFlags(enabled | (Qt.ItemFlag.ItemIsEditable if editable else Qt.ItemFlag.NoItemFlags))
                self.setItem(row, column, item)
        self.blockSignals(False)

    def cell_committed(self):
        editor = self.sender()
        try:
            value = editor.raw()
        except ValueError:
            return
        self.edit.emit(("set", {editor.property("wwise_path"): value}))

    def _item_changed(self, item):
        field, _hints = self.cell(item.row(), item.column())
        if field is not None and not isinstance(field, str) and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            self.edit.emit(("set", {field.path: int(item.checkState() == Qt.CheckState.Checked)}))

    def _current_changed(self, row, _column, previous, _previous_column):
        if row != previous:
            self.current.emit(row)


def _item_fields(state, entry, index):
    roots = [lane[index] for lane in entry.lanes if index < len(lane) and lane[index]]
    nested = [count for count in state.lists if count != entry.count_path and any(count.startswith(root + "/") for root in roots)]
    nested_roots = {count: [root for lane in state.lists[count].lanes for root in lane if root] for count in nested}
    direct = [
        count for count in nested
        if not any(other != count and any(_inside(count, root) for root in nested_roots[other]) for other in nested)
    ]
    inner = [root for roots_of in nested_roots.values() for root in roots_of]
    result = {}
    for field in state.layout.fields:
        if not field.visible or field.path in state.lists:
            continue
        root = next((root for root in roots if _inside(field.path, root)), None)
        if root is None or any(_inside(field.path, other) for other in inner):
            continue
        result[_DIGITS.sub("", field.label) if field.path == root else field.path[len(root) + 1:]] = field
    return result, direct


class _ListEditor(QWidget):
    edit = Signal(object)

    def __init__(self, count_path, hints, state, parent=None):
        super().__init__(parent)
        self.count_path, self.hints = count_path, hints
        self.entry = state.lists[count_path]
        rows, nested = self._rows(state)
        present = list(dict.fromkeys(label for fields in rows for label in fields))
        columns = [label for label in hints.get("columns", ()) if label in present or not rows] or present
        self.curve = bool(rows) and set(columns) == {"From", "To", "Interpolation"}
        self.detail_mode = not self.curve and (any(nested) or len(columns) > 7 or hints.get("master"))
        self.column_hints = {label: self._column_hints(label) for label in columns}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.view = None
        self._detail_inputs, self._detail_lists = {}, []
        if self.detail_mode:
            summary = hints.get("summary")
            self.summary = ["Rule"] if summary == "rule" else [
                label for label in summary or [label for label in columns if not label.endswith("count")][:2] if label in columns
            ]
            self.table = _Table(
                [(label, {**self.column_hints.get(label, {}), "readonly": True}) for label in self.summary],
                self._summaries(state, rows), state.ctx, self,
            )
            splitter = QSplitter(Qt.Orientation.Horizontal, self)
            splitter.addWidget(self.table)
            self.detail = QWidget(splitter)
            self.detail_layout = QVBoxLayout(self.detail)
            self.detail_layout.setContentsMargins(6, 0, 0, 0)
            splitter.addWidget(self.detail)
            splitter.setStretchFactor(1, 2)
            width = self.table.verticalHeader().sizeHint().width() + sum(
                self.table.columnWidth(column) for column in range(self.table.columnCount())
            ) + 24
            splitter.setSizes([min(max(width, 200), 440), 520])
            layout.addWidget(splitter, 1)
        else:
            self.table = _Table([(label, self.column_hints[label]) for label in columns], rows, state.ctx, self)
            self.table.edit.connect(self.edit)
            if self.curve:
                self.view = _CurveView(self)
                self.view.moved.connect(self._moved)
                self.view.added.connect(self._added)
                self.view.removed.connect(self._removed)
                self.view.shaped.connect(self._shaped)
                splitter = QSplitter(Qt.Orientation.Horizontal, self)
                splitter.addWidget(self.view)
                splitter.addWidget(self.table)
                splitter.setSizes([440, 300])
                layout.addWidget(splitter, 1)
                self._set_points(rows)
            else:
                layout.addWidget(self.table, 1)
        if not hints.get("readonly"):
            tools = QHBoxLayout()
            tools.setSpacing(2)
            for icon, text, slot in (
                ("add", self.tr("Add"), self._add), ("copy", self.tr("Duplicate"), self._duplicate),
                ("remove", self.tr("Remove"), self._remove), ("arrow_up", self.tr("Move up"), self._up),
                ("arrow_down", self.tr("Move down"), self._down),
            ):
                if self.curve and icon not in {"add", "remove"}:
                    continue
                button = _button(icon, text, self)
                button.clicked.connect(slot)
                tools.addWidget(button)
            tools.addStretch()
            layout.addLayout(tools)
        self.count_label = styled_label(self.tr("{count} item(s)").format(count=len(rows)), self, tone="muted")
        layout.addWidget(self.count_label)
        self._state = state
        if self.detail_mode:
            self.table.current.connect(self._show_detail)
        if rows:
            self.table.selectRow(min(state.selected.get(count_path, 0), len(rows) - 1))

    def _rows(self, state):
        rows, nested = [], []
        for index in range(len(self.entry.items)):
            fields, inner = _item_fields(state, self.entry, index)
            base = self.hints.get("bypass_base")
            if base and "Slot" in fields and state.fields.get(f"{base}/Bypass slot {int(fields['Slot'].value)}") is not None:
                fields["Bypassed"] = state.fields[f"{base}/Bypass slot {int(fields['Slot'].value)}"]
            rows.append(fields)
            nested.append(inner)
        return rows, nested

    def _summaries(self, state, rows):
        if self.hints.get("summary") != "rule":
            return rows
        result = []
        for index in range(len(rows)):
            root = self.entry.lanes[0][index]
            sides = []
            for side in ("Source", "Destination"):
                count = int(state.value(f"{root}/{side} count"))
                sides.append(", ".join(state.ctx.object_label(int(state.value(f"{root}/{side} {item}")), "Any") for item in range(1, count + 1)))
            result.append({"Rule": f"{sides[0]}  →  {sides[1]}"})
        return result

    def _column_hints(self, label):
        result = dict(self.hints.get("column_hints", {}).get(label, {}))
        if label == "Weight" and self.count_path == "Playlist/Item count":
            result["scale"] = 0.001
        for key in ("readonly", "children"):
            if self.hints.get(key):
                result[key] = True
        return result

    def _set_points(self, rows):
        self.view.set_points([(row["From"].value, row["To"].value, int(row["Interpolation"].value)) for row in rows if "From" in row])

    def sync(self, state):
        self._state = state
        self.entry = state.lists.get(self.count_path, self.entry)
        rows, _nested = self._rows(state)
        if self.detail_mode:
            self.table.fill(self._summaries(state, rows))
            for path, widget in self._detail_inputs.items():
                if path in state.fields:
                    widget.set_raw(state.fields[path].value)
            for child in self._detail_lists:
                child.sync(state)
        else:
            self.table.fill(rows)
            if self.view is not None:
                self._set_points(rows)

    def _show_detail(self, row):
        state = self._state
        rows, nested = self._rows(state)
        if not 0 <= row < len(rows):
            return
        state.selected[self.count_path] = row
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._detail_inputs, self._detail_lists = {}, []
        form_box = QWidget(self.detail)
        form = QFormLayout(form_box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.detail_layout.addWidget(form_box)
        for label, field in rows[row].items():
            text = self.tr(split_unit(label)[0])
            widget, own = _input(field, self.column_hints.get(label, self._column_hints(label)), state.ctx, form_box, text)
            widget.setProperty("wwise_path", field.path)
            widget.committed.connect(self._detail_committed)
            self._detail_inputs[field.path] = widget
            if own:
                form.addRow(widget)
            else:
                form.addRow(text, widget)
        for count in nested[row]:
            title = count.rsplit("/", 1)[-1].replace(" count", "s")
            box = QGroupBox(self.tr(title[:1].upper() + title[1:]), self.detail)
            box_layout = QVBoxLayout(box)
            if any(None in lane for lane in state.lists[count].lanes) or _STATE_VALUES.search(count):
                child = _KeyedEditor(PageControl("keyed", count), state, box)
            else:
                child = _ListEditor(count, {"children": self.hints.get("children")}, state, box)
            child.edit.connect(self.edit)
            box_layout.addWidget(child)
            self._detail_lists.append(child)
            self.detail_layout.addWidget(box)
        self.detail_layout.addStretch()

    def _detail_committed(self):
        widget = self.sender()
        try:
            value = widget.raw()
        except ValueError:
            return
        self.edit.emit(("set", {widget.property("wwise_path"): value}))

    def _order(self):
        return list(range(len(self.entry.items)))

    def _add(self):
        order = self._order()
        if self.curve and order and self.view.points:
            x, y, _shape = self.view.points[-1]
            self._added(x + 1.0, y)
            return
        self._state.selected[self.count_path] = len(order)
        self.edit.emit(("list", self.count_path, [*order, None]))

    def _duplicate(self):
        row, order = self.table.currentRow(), self._order()
        if row >= 0:
            self._state.selected[self.count_path] = row + 1
            self.edit.emit(("list", self.count_path, [*order[:row + 1], row, *order[row + 1:]]))

    def _remove(self):
        row, order = self.table.currentRow(), self._order()
        if row >= 0:
            self.edit.emit(("list", self.count_path, [*order[:row], *order[row + 1:]]))

    def _move(self, step):
        row, order = self.table.currentRow(), self._order()
        if 0 <= row and 0 <= row + step < len(order):
            order[row], order[row + step] = order[row + step], order[row]
            self._state.selected[self.count_path] = row + step
            self.edit.emit(("list", self.count_path, order))

    def _up(self):
        self._move(-1)

    def _down(self):
        self._move(1)

    def _moved(self, index, x, y):
        self.edit.emit(("set", {f"{self.entry.lanes[0][index]}/From": x, f"{self.entry.lanes[0][index]}/To": y}))

    def _added(self, x, y):
        position = sum(point[0] <= x for point in self.view.points)
        order = self._order()
        order.insert(position, None)
        root = f"{self.entry.path}/{position + 1}"
        self.edit.emit(("seq", [("list", self.count_path, order), ("set", {f"{root}/From": x, f"{root}/To": y})]))

    def _removed(self, index):
        order = self._order()
        del order[index]
        self.edit.emit(("list", self.count_path, order))

    def _shaped(self, index, shape):
        self.edit.emit(("set", {f"{self.entry.lanes[0][index]}/Interpolation": shape}))


class _Composite(QWidget):
    edit = Signal(object)

    def __init__(self, control, state, parent=None):
        super().__init__(parent)
        self.key, self.hints = control.key, control.hints
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(4)
        self.build(state)

    def build(self, state):
        pass

    def sync(self, state):
        pass


class _ListComposite(_Composite):
    def build(self, state):
        self.list = _ListEditor(self.key, self.hints, state, self)
        self.list.edit.connect(self._edited)
        self.row.addWidget(self.list, 1)

    def _edited(self, op):
        self.edit.emit(op)

    def sync(self, state):
        self.list.sync(state)


class _EffectsEditor(_ListComposite):
    def _edited(self, op):
        if op[0] == "list" and None in op[2]:
            used = {int(row["Slot"].value) for row in self.list.table.rows if "Slot" in row}
            root = f"{self.list.entry.path}/{op[2].index(None) + 1}"
            op = ("seq", [op, ("set", {f"{root}/Slot": next(slot for slot in range(256) if slot not in used)})])
        self.edit.emit(op)


class _LoopEditor(_Composite):
    def build(self, state):
        self.loop = QCheckBox(self.tr("Loop"), self)
        self.infinite = QRadioButton(self.tr("Infinite"), self)
        self.counted = QRadioButton(self.tr("No. of loops"), self)
        self.count = _Number(2, "u16", self)
        self.count.setRange(2, 32767)
        group = QButtonGroup(self)
        group.addButton(self.infinite)
        group.addButton(self.counted)
        for widget in (self.loop, self.infinite, self.counted, self.count):
            self.row.addWidget(widget)
        self.random = None
        if self.hints.get("random"):
            self.random = _Randomizer(state.fields[self.hints["random"][0]].storage, self)
            self.random.committed.connect(self._random)
            self.row.addWidget(self.random)
        self.row.addStretch()
        for widget in (self.loop, self.infinite, self.counted):
            widget.clicked.connect(self._commit)
        self.count.committed.connect(self._commit)
        self.sync(state)

    def _field(self):
        return "id" not in self.hints

    def sync(self, state):
        value = int(state.value(self.key, 1) if self._field() else _bundle(state.layout.fields, self.key).get(self.hints["id"], 1))
        self.loop.setChecked(value != 1)
        self.infinite.setChecked(value == 0)
        self.counted.setChecked(value > 1)
        self.count.set_raw(max(value, 2))
        for widget in (self.infinite, self.counted, self.count):
            widget.setEnabled(value != 1)
        if self.random is not None:
            low, high = (state.value(path) for path in self.hints["random"])
            self.random.set_range((low, high) if low or high else None)

    def _commit(self, *_args):
        sender = self.sender()
        if not self.loop.isChecked():
            value = 1
        elif sender is self.loop or sender is self.infinite:
            value = 0
        else:
            value = self.count.raw()
        if self._field():
            self.edit.emit(("set", {self.key: value}))
        else:
            self.edit.emit(("props", self.key, {self.hints["id"]: None if value == 1 else value}))

    def _random(self):
        low, high = self.random.raw() or (0, 0)
        self.edit.emit(("set", dict(zip(self.hints["random"], (low, high)))))


class _StorageEditor(_Composite):
    def build(self, state):
        self.stream = QCheckBox(self.tr("Stream"), self)
        self.zero = QCheckBox(self.tr("Zero latency"), self)
        self.row.addWidget(self.stream)
        self.row.addWidget(self.zero)
        self.row.addStretch()
        self.stream.clicked.connect(self._commit)
        self.zero.clicked.connect(self._commit)
        self.sync(state)

    def sync(self, state):
        value = int(state.value(self.key))
        self.stream.setChecked(value in {1, 2})
        self.zero.setChecked(value == 1)
        self.zero.setEnabled(value in {1, 2})

    def _commit(self):
        self.edit.emit(("set", {self.key: 0 if not self.stream.isChecked() else 1 if self.zero.isChecked() else 2}))


class _LimitEditor(_Composite):
    def build(self, state):
        self.limit = QCheckBox(self.tr("Limit sound instances to"), self)
        self.count = _Number(50, "u16", self)
        self.count.setMinimum(1)
        self.row.addWidget(self.limit)
        self.row.addWidget(self.count)
        self.row.addStretch()
        self.limit.clicked.connect(self._commit)
        self.count.committed.connect(self._commit)
        self.sync(state)

    def sync(self, state):
        value = int(state.value(self.key))
        self.limit.setChecked(bool(value))
        self.count.set_raw(value or 50)
        self.count.setEnabled(bool(value))

    def _commit(self):
        self.edit.emit(("set", {self.key: self.count.raw() if self.limit.isChecked() else 0}))


class _ChannelsEditor(_Composite):
    _PATHS = ("Bus playback/Channel count", "Bus playback/Configuration type", "Bus playback/Channel mask")

    def build(self, state):
        self.preset = _Choice(_CHANNEL_PRESETS, 0, self)
        self.row.addWidget(self.preset, 1)
        self.preset.committed.connect(self._commit)
        self.sync(state)

    def sync(self, state):
        count, kind, mask = (int(state.value(path)) for path in self._PATHS)
        self.preset.set_raw(count | kind << 8 | mask << 12)

    def _commit(self):
        packed = self.preset.raw()
        self.edit.emit(("set", dict(zip(self._PATHS, (packed & 0xFF, packed >> 8 & 0xF, packed >> 12)))))


class _ActionTypeEditor(_Composite):
    def build(self, state):
        self.types = action_types(state.version)
        self.family = _Choice({family: name for family, (name, _variants) in self.types.items()}, 0, self)
        self.target = _Choice({2: "Object", 4: "All", 8: "All except"}, 2, self)
        self.scope = _Choice({0: "Global", 1: "Game object"}, 0, self)
        for widget, stretch in ((self.family, 2), (self.target, 1), (self.scope, 1)):
            self.row.addWidget(widget, stretch)
            widget.committed.connect(self._commit)
        self.sync(state)

    def sync(self, state):
        value = int(state.value(self.key))
        family, variant = value & 0xFF00, value & 0xFF
        self.family.set_raw(family)
        variants = self.types.get(family, ("", (variant,)))[1]
        self.target.set_raw(variant & 0x0E or 2)
        self.target.setVisible(len({item & 0x0E for item in variants}) > 1)
        self.scope.set_raw(variant & 1)
        self.scope.setVisible(len({item & 1 for item in variants}) > 1)

    def _commit(self):
        family = self.family.raw()
        variants = self.types.get(family, ("", (0,)))[1]
        wanted = self.target.raw() | self.scope.raw()
        variant = wanted if wanted in variants else next((item for item in variants if item & 1 == self.scope.raw()), variants[0])
        self.edit.emit(("set", {self.key: family | variant}))


class _SendsEditor(_Composite):
    def build(self, state):
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        self.row.addLayout(grid, 1)
        for column, title in enumerate(("Auxiliary bus", "Volume", "Low-pass filter", "High-pass filter")):
            grid.addWidget(styled_label(self.tr(title), self, heading="caption"), 0, column)
        names = property_names("object", state.version)
        self.buses, self.props = [], []
        for slot in range(4):
            picker = _object_picker(0, "auxiliary bus", {}, state.ctx, self)
            picker.setProperty("slot", slot)
            picker.committed.connect(self._bus)
            grid.addWidget(picker, slot + 1, 0)
            self.buses.append(picker)
            for column, prop_id in enumerate(self.hints["slots"][slot], 1):
                if prop_id is not None:
                    row = _PropertyRow(self.hints["section"], prop_id, names[prop_id], "object", state.version, False, state.ctx, self)
                    row.edit.connect(self.edit)
                    grid.addWidget(row, slot + 1, column)
                    self.props.append(row)
        self.sync(state)

    def sync(self, state):
        for slot, picker in enumerate(self.buses):
            picker.set_raw(int(state.value(f"{self.key}/User send {slot + 1} bus")))
        values = _bundle(state.layout.fields, self.hints["section"])
        for row in self.props:
            row.sync(values, {})

    def _bus(self):
        buses = [picker._shown for picker in self.buses]
        has = f"{self.key}/Has user-defined sends"
        if any(buses):
            changes = {f"{self.key}/User send {slot + 1} bus": value for slot, value in enumerate(buses)}
            self.edit.emit(("seq", [("set", {has: 1}), ("set", changes)]))
        else:
            self.edit.emit(("set", {has: 0}))


class _KeyedEditor(_Composite):
    def build(self, state):
        column = QVBoxLayout()
        self.row.addLayout(column, 1)
        self.names = property_names("state", state.version)
        self.values = self._read(state)
        self.table = QTableWidget(len(self.values), 2, self)
        self.table.setHorizontalHeaderLabels((self.tr("Property"), self.tr("Value")))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(26)
        for row, (prop_id, value) in enumerate(self.values.items()):
            choice = _Choice(self.names, prop_id, self.table)
            choice.setProperty("row", row)
            choice.committed.connect(self._renamed)
            self.table.setCellWidget(row, 0, choice)
            number = _Number(value, "f32", self.table)
            number.setProperty("row", row)
            number.committed.connect(self._valued)
            self.table.setCellWidget(row, 1, number)
        self.table.resizeColumnsToContents()
        self.table.setMinimumHeight(min(70 + 26 * max(len(self.values), 2), 320))
        column.addWidget(self.table)
        tools = QHBoxLayout()
        for icon, text, slot in (("add", self.tr("Add"), self._add), ("remove", self.tr("Remove"), self._remove)):
            button = _button(icon, text, self)
            button.clicked.connect(slot)
            tools.addWidget(button)
        tools.addStretch()
        column.addLayout(tools)

    def _read(self, state):
        prefix = self.key.rsplit("/", 1)[0] + "/"
        ids, values = [], []
        for field in state.layout.fields:
            if field.path.startswith(prefix) and "/" not in field.path[len(prefix):]:
                if field.label.endswith((" type", " property")):
                    ids.append(int(field.value))
                elif field.label.endswith(" value"):
                    values.append(field.value)
        return dict(zip(ids, values))

    def sync(self, state):
        values = self._read(state)
        if list(values) == list(self.values):
            self.values = values
            for row, value in enumerate(values.values()):
                self.table.cellWidget(row, 1).set_raw(value)

    def _emit(self, values):
        self.edit.emit(("keyed", self.key, values))

    def _renamed(self):
        items = list(self.values.items())
        row, new_id = self.sender().property("row"), self.sender().raw()
        if new_id not in self.values:
            items[row] = (new_id, items[row][1])
            self._emit(dict(items))

    def _valued(self):
        items = list(self.values.items())
        row = self.sender().property("row")
        items[row] = (items[row][0], self.sender().raw())
        self._emit(dict(items))

    def _add(self):
        free = next((prop_id for prop_id in sorted(self.names) if prop_id not in self.values), None)
        if free is not None:
            self._emit({**self.values, free: 0.0})

    def _remove(self):
        row = self.table.currentRow()
        if row >= 0:
            items = list(self.values.items())
            del items[row]
            self._emit(dict(items))


def _with_keys(path, keys):
    return WwiseDecisionPath(tuple(keys), path.target, path.weight, path.probability)


class _DecisionEditor(_Composite):
    def build(self, state):
        column = QVBoxLayout()
        self.row.addLayout(column, 1)
        self.arguments, self.paths = decision_tree(state.layout)
        ctx = state.ctx
        music = ctx.owner is not None and ctx.owner.type_id == 0x0C
        column.addWidget(styled_label(self.tr("Arguments"), self, heading="caption"))
        self.args = QTableWidget(len(self.arguments), 2, self)
        self.args.setHorizontalHeaderLabels((self.tr("Type"), self.tr("Group")))
        self.args.horizontalHeader().setStretchLastSection(True)
        self.args.verticalHeader().setDefaultSectionSize(26)
        for row, (group, kind) in enumerate(self.arguments):
            choice = _Choice({0: "Switch", 1: "State"}, kind, self.args)
            picker = _sync_picker(group, "state_group" if kind else "switch_group", ctx, self.args)
            for column_index, widget in enumerate((choice, picker)):
                widget.setProperty("row", row)
                widget.committed.connect(self._argument)
                self.args.setCellWidget(row, column_index, widget)
        self.args.setMinimumHeight(min(60 + 26 * max(len(self.arguments), 1), 200))
        column.addWidget(self.args)
        column.addLayout(self._tools(("Add argument", self._add_argument), ("Remove argument", self._remove_argument)))
        column.addWidget(styled_label(self.tr("Paths"), self, heading="caption"))
        headers = [*(self.tr("Argument {number}").format(number=index + 1) for index in range(len(self.arguments))),
                   self.tr("Object"), self.tr("Weight"), self.tr("Probability")]
        self.table = QTableWidget(len(self.paths), len(headers), self)
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setMinimumHeight(min(70 + 26 * max(len(self.paths), 2), 420))
        self.widgets = []
        for row, path in enumerate(self.paths):
            widgets = [
                _sync_picker(key, "state_value" if self.arguments[level][1] else "switch_value", ctx, self.table, {0: "*"})
                for level, key in enumerate(path.keys)
            ]
            widgets.append(_object_picker(path.target, "decision leaf" if music else "dialogue leaf", {"children": music}, ctx, self.table))
            widgets.append(_Number(path.weight, "u16", self.table))
            widgets.append(_Number(path.probability, "u16", self.table))
            for column_index, widget in enumerate(widgets):
                widget.committed.connect(self._commit)
                self.table.setCellWidget(row, column_index, widget)
            self.widgets.append(widgets)
        self.table.resizeColumnsToContents()
        column.addWidget(self.table)
        column.addLayout(self._tools(("Add path", self._add_path), ("Remove path", self._remove_path)))

    def _tools(self, *buttons):
        tools = QHBoxLayout()
        for (text, slot), icon in zip(buttons, ("add", "remove")):
            button = _button(icon, self.tr(text), self)
            button.clicked.connect(slot)
            tools.addWidget(button)
        tools.addStretch()
        return tools

    def _current(self):
        depth = len(self.arguments)
        return [
            WwiseDecisionPath(tuple(widget.raw() for widget in widgets[:depth]), *(widget.raw() for widget in widgets[depth:]))
            for widgets in self.widgets
        ]

    def sync(self, state):
        arguments, paths = decision_tree(state.layout)
        if arguments == self.arguments and len(paths) == len(self.widgets):
            for widgets, path in zip(self.widgets, paths):
                for widget, value in zip(widgets, (*path.keys, path.target, path.weight, path.probability)):
                    widget.set_raw(value)

    def _emit(self, arguments, paths):
        self.edit.emit(("decision", tuple(arguments), tuple(paths)))

    def _commit(self):
        self._emit(self.arguments, self._current())

    def _argument(self):
        row = self.sender().property("row")
        kind = self.args.cellWidget(row, 0).raw()
        arguments = list(self.arguments)
        arguments[row] = (self.args.cellWidget(row, 1).raw() if kind == arguments[row][1] else 0, kind)
        self._emit(arguments, self._current())

    def _add_argument(self):
        self._emit([*self.arguments, (0, 0)], [_with_keys(path, path.keys + (0,)) for path in self._current()])

    def _remove_argument(self):
        if self.arguments:
            self._emit(self.arguments[:-1], [_with_keys(path, path.keys[:-1]) for path in self._current()])

    def _add_path(self):
        if self.arguments:
            self._emit(self.arguments, [*self._current(), WwiseDecisionPath((0,) * len(self.arguments), 0)])

    def _remove_path(self):
        row, paths = self.table.currentRow(), self._current()
        if 0 <= row < len(paths):
            del paths[row]
            self._emit(self.arguments, paths)


class _PlaylistEditor(_Composite):
    _LABELS = (
        "Segment", "Selection type", "Loop count", "Loop random minimum", "Loop random maximum", "Weight",
        "Avoid-repeat count", "Use weight", "Shuffle", "Playlist item ID",
    )

    def build(self, state):
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.row.addWidget(splitter, 1)
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget(left)
        self.tree.setHeaderLabels((self.tr("Item"), self.tr("Loop"), self.tr("Weight")))
        self.tree.setMinimumHeight(260)
        left_layout.addWidget(self.tree, 1)
        tools = QHBoxLayout()
        for icon, text, slot in (
            ("add", "Add segment", self._add_segment), ("folder", "Add group", self._add_group),
            ("remove", "Remove", self._remove), ("arrow_up", "Move up", self._up), ("arrow_down", "Move down", self._down),
        ):
            button = _button(icon, self.tr(text), left)
            button.clicked.connect(slot)
            tools.addWidget(button)
        tools.addStretch()
        left_layout.addLayout(tools)
        self.detail = QWidget(splitter)
        self.form = QFormLayout(self.detail)
        splitter.addWidget(left)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        self.state, self.inputs = state, {}
        if state.layout.field("Music playlist/Segment") is not None:
            self._node(self.tree.invisibleRootItem(), "Music playlist")
        self.tree.expandAll()
        self.tree.currentItemChanged.connect(self._selected)
        wanted = state.selected.get(self.key, "Music playlist")
        items = self.tree.findItems("", Qt.MatchFlag.MatchContains | Qt.MatchFlag.MatchRecursive)
        self.tree.setCurrentItem(next((item for item in items if item.data(0, Qt.ItemDataRole.UserRole) == wanted), self.tree.topLevelItem(0)))

    def _texts(self, root):
        value = self.state.value
        kind = int(value(f"{root}/Selection type"))
        if kind == 0xFFFFFFFF:
            title = self.state.ctx.object_label(int(value(f"{root}/Segment")))
        else:
            title = self.tr(dict(self.state.layout.field(f"{root}/Selection type").enum).get(kind, "Group"))
        loop = int(value(f"{root}/Loop count"))
        return title, self.tr("Infinite") if loop == 0 else str(loop), f"{int(value(f'{root}/Weight')) / 1000:g}"

    def _node(self, parent, root):
        item = QTreeWidgetItem(parent, self._texts(root))
        item.setData(0, Qt.ItemDataRole.UserRole, root)
        for index in range(int(self.state.value(f"{root}/Child count"))):
            self._node(item, f"{root}/{index + 1}")

    def sync(self, state):
        self.state = state
        for item in self.tree.findItems("", Qt.MatchFlag.MatchContains | Qt.MatchFlag.MatchRecursive):
            for column, text in enumerate(self._texts(item.data(0, Qt.ItemDataRole.UserRole))):
                item.setText(column, text)
        for path, widget in self.inputs.items():
            if path in state.fields:
                widget.set_raw(state.fields[path].value)

    def _selected(self, item, _previous):
        while self.form.rowCount():
            self.form.removeRow(0)
        self.inputs = {}
        if item is None:
            return
        root = item.data(0, Qt.ItemDataRole.UserRole)
        self.state.selected[self.key] = root
        for label in self._LABELS:
            field = self.state.layout.field(f"{root}/{label}")
            if field is None:
                continue
            hints = {"scale": 0.001} if label == "Weight" else {"readonly": True} if label == "Playlist item ID" else {"children": True}
            widget, own = _input(field, hints, self.state.ctx, self.detail, self.tr(label))
            widget.setProperty("wwise_path", field.path)
            widget.committed.connect(self._committed)
            self.inputs[field.path] = widget
            if own:
                self.form.addRow(widget)
            else:
                self.form.addRow(self.tr(label), widget)

    def _committed(self):
        widget = self.sender()
        self.edit.emit(("set", {widget.property("wwise_path"): widget.raw()}))

    def _current(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _insert(self, group):
        root = self._current()
        if root is None:
            return
        if int(self.state.value(f"{root}/Selection type")) == 0xFFFFFFFF and "/" in root:
            root = root.rsplit("/", 1)[0]
        count = int(self.state.value(f"{root}/Child count"))
        steps = [("list", f"{root}/Child count", [*range(count), None])]
        if group:
            steps.append(("set", {f"{root}/{count + 1}/Selection type": 0}))
        self.state.selected[self.key] = f"{root}/{count + 1}"
        self.edit.emit(("seq", steps))

    def _add_segment(self):
        self._insert(False)

    def _add_group(self):
        self._insert(True)

    def _sibling(self, step):
        root = self._current()
        if not root or "/" not in root:
            return
        parent, index = root.rsplit("/", 1)
        index = int(index) - 1
        order = list(range(int(self.state.value(f"{parent}/Child count"))))
        if step == 0:
            del order[index]
            self.state.selected[self.key] = parent
        elif 0 <= index + step < len(order):
            order[index], order[index + step] = order[index + step], order[index]
            self.state.selected[self.key] = f"{parent}/{index + step + 1}"
        else:
            return
        self.edit.emit(("list", f"{parent}/Child count", order))

    def _remove(self):
        self._sibling(0)

    def _up(self):
        self._sibling(-1)

    def _down(self):
        self._sibling(1)


class _AttenuationEditor(_Composite):
    def build(self, state):
        column = QVBoxLayout()
        self.row.addLayout(column, 1)
        curves = state.lists.get("Attenuation curves/Curve count")
        count = len(curves.items) if curves else 0
        assignments = [field for field in state.layout.fields if field.path.startswith("Curve assignments/")]
        choices = {
            -1: "None", **({-2: "Use project curve"} if state.version >= 142 else {}),
            **{index: f"Curve {index + 1}" for index in range(count)},
        }
        grid = QGridLayout()
        self.choices = {}
        for index, field in enumerate(assignments):
            grid.addWidget(QLabel(self.tr(field.label), self), index // 2, index % 2 * 2)
            choice = _Choice(choices, int(field.value), self)
            choice.setProperty("wwise_path", field.path)
            choice.committed.connect(self._assigned)
            grid.addWidget(choice, index // 2, index % 2 * 2 + 1)
            self.choices[field.path] = choice
        column.addLayout(grid)
        self._assignments = {field.path: int(field.value) for field in assignments}
        self.list = None
        if curves is not None:
            self.list = _ListEditor("Attenuation curves/Curve count", {"master": True, "summary": ("Scaling",)}, state, self)
            self.list.edit.connect(self._curve_op)
            column.addWidget(self.list, 1)

    def sync(self, state):
        self._assignments = {path: int(state.value(path)) for path in self.choices}
        for path, choice in self.choices.items():
            choice.set_raw(self._assignments[path])
        if self.list is not None:
            self.list.sync(state)

    def _assigned(self):
        widget = self.sender()
        self.edit.emit(("set", {widget.property("wwise_path"): widget.raw()}))

    def _curve_op(self, op):
        if op[0] != "list" or op[1] != "Attenuation curves/Curve count":
            self.edit.emit(op)
            return
        remap = {old: new for new, old in enumerate(op[2]) if old is not None}
        steps = [op, ("set", {path: value if value < 0 else remap.get(value, -1) for path, value in self._assignments.items()})]
        for position, old in enumerate(op[2]):
            if old is None:
                root = f"Attenuation curves/{position + 1}"
                steps += [("list", f"{root}/Point count", [None, None]), ("set", {f"{root}/Points/2/From": 100.0})]
        self.edit.emit(("seq", steps))


class _AddProperty(_Composite):
    def build(self, state):
        self.kind, self.version = state.kind, state.version
        self.choice = QComboBox(self)
        add = _button("add", self.tr("Add property"), self)
        add.clicked.connect(self._add)
        self.row.addWidget(self.choice, 1)
        self.row.addWidget(add)
        self.sync(state)

    def sync(self, state):
        present = _bundle(state.layout.fields, self.key)
        names = property_names(state.kind, state.version)
        self.choice.clear()
        for prop_id in sorted(self.hints["ids"], key=lambda item: names.get(item, "")):
            if prop_id not in present:
                self.choice.addItem(self.tr(names.get(prop_id, f"Property 0x{prop_id:02X}")), prop_id)

    def _add(self):
        prop_id = self.choice.currentData()
        if prop_id is not None:
            default = property_default(self.kind, self.version, prop_id)
            self.edit.emit(("props", self.key, {prop_id: default or 0}))


class _ReadOnlyTable(_Composite):
    def build(self, state):
        rows = {}
        for field in state.layout.fields:
            if field.path.startswith(self.key + "/"):
                index, _, label = field.path[len(self.key) + 1:].partition("/")
                rows.setdefault(index, {})[label] = field
        columns = list(dict.fromkeys(label for fields in rows.values() for label in fields))
        self.row.addWidget(_Table([(label, {"readonly": True}) for label in columns], list(rows.values()), state.ctx, self), 1)


_COMPOSITES = {
    "list": _ListComposite, "effects": _EffectsEditor, "loop": _LoopEditor, "loop_field": _LoopEditor,
    "storage": _StorageEditor, "limit": _LimitEditor, "channels": _ChannelsEditor, "action": _ActionTypeEditor,
    "sends": _SendsEditor, "keyed": _KeyedEditor, "decision": _DecisionEditor, "playlist": _PlaylistEditor,
    "attenuation": _AttenuationEditor, "add_property": _AddProperty, "readonly_table": _ReadOnlyTable,
}
_WIDE = frozenset({"list", "effects", "sends", "keyed", "decision", "playlist", "attenuation", "readonly_table"})


class _State:
    def __init__(self, version, kind):
        self.version, self.kind = version, kind
        self.layout = self.ctx = None
        self.lists, self.fields, self.selected = {}, {}, {}

    def value(self, path, default=0):
        field = self.fields.get(path)
        return field.value if field is not None else default


class WwisePropertyEditor(QWidget):
    edited = Signal()

    def __init__(self, payload, layout, *, type_name="", obj=None, objects=(), metadata=None, parent=None):
        super().__init__(parent)
        self._payload, self._source = bytes(payload), layout.source
        self._type_name, self._obj, self._objects = type_name, obj, tuple(objects)
        self._metadata = metadata or SoundMetadata()
        self._history, self._future = [], []
        kind = "modulator" if type_name in {"LFO", "Envelope", "Time Modulator"} else "state" if type_name == "State" else "object"
        self._state = _State(self._source[2], kind)
        self._signature = None
        self._tab_pages, self._built = [], set()
        self._inputs, self._rows, self._composites, self._rules = {}, [], [], {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QFrame(self)
        set_style_role(header, surface="header")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(10, 6, 6, 6)
        self.title = styled_label("", header, heading="panel")
        self.subtitle = styled_label("", header, tone="muted")
        header_row.addWidget(self.title)
        header_row.addWidget(self.subtitle, 1)
        self.undo_button = _button("undo", self.tr("Undo (Ctrl+Z)"), header)
        self.redo_button = _button("redo", self.tr("Redo (Ctrl+Y)"), header)
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        header_row.addWidget(self.undo_button)
        header_row.addWidget(self.redo_button)
        root.addWidget(header)
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)
        self.status = styled_label("", self, banner="danger")
        self.status.setWordWrap(True)
        self.status.hide()
        root.addWidget(self.status)
        QShortcut(QKeySequence.StandardKey.Undo, self, self.undo)
        QShortcut(QKeySequence.StandardKey.Redo, self, self.redo)
        self._load(self._payload)

    def payload(self):
        return self._payload

    def undo(self):
        if self._history:
            self._future.append(self._payload)
            self._load(self._history.pop())
            self.edited.emit()

    def redo(self):
        if self._future:
            self._history.append(self._payload)
            self._load(self._future.pop())
            self.edited.emit()

    def _parse(self, payload):
        if self._source[0] == "hirc":
            return parse_structured_object(self._source[1], payload, self._source[2], feedback=self._source[3])
        return parse_structured_chunk(self._source[1], payload, self._source[2]).structure

    def _load(self, payload):
        self._payload = payload
        state = self._state
        state.layout = self._parse(payload)
        state.fields = {field.path: field for field in state.layout.fields}
        state.lists = {} if self._source[1] == "DIDX" else {entry.count_path: entry for entry in wwise_lists(payload, state.layout)}
        owner = self._obj
        if owner is not None:
            owner = HircObject(
                owner.index, owner.type_id, owner.type_name, owner.object_id, payload, owner.payload_offset,
                bank_version=owner.bank_version,
            )
            others = [item for item in self._objects if item.index != owner.index]
            _decode_one_hirc_object(owner, [*others, owner])
            _resolve_fx_state_property_names([*(item for item in others if item.fx_plugin), owner])
        state.ctx = _Context(self._metadata, self._objects, state.version, owner)
        self.undo_button.setEnabled(bool(self._history))
        self.redo_button.setEnabled(bool(self._future))
        self._header()
        lists = tuple(state.lists.values())
        if self._source[0] == "chunk":
            pages = chunk_pages(self._source[1], state.layout, lists, state.version)
        else:
            pages = object_pages(self._type_name, state.layout, lists, state.version)
        signature = (
            tuple(
                (field.path, field.storage, field.id_kind, field.reference_role)
                for field in state.layout.fields if field.visible and not keyed_path(field.path)
            ),
            tuple(
                (tab.title, group.title, control.kind, control.key, control.hints.get("id"))
                for tab in pages for group in tab.groups for control in group.controls
            ),
        )
        if signature != self._signature:
            self._signature, self._tab_pages = signature, pages
            self._rebuild()
        else:
            self._sync()

    def _header(self):
        state = self._state
        if self._source[0] == "chunk":
            self.title.setText(self.tr(self._type_name or self._source[1]))
            self.subtitle.setText(self._source[1])
            return
        object_id = int(state.value("Object ID"))
        names = self._metadata.event_names(object_id) if self._source[1] == 0x04 else ()
        self.title.setText(f"{self.tr(self._type_name)}  {object_id}" + (f"  ·  {' / '.join(names)}" if names else ""))
        detail = [f"0x{object_id:08X}", self.tr("bank v{version}").format(version=state.version)]
        if int(state.value("Node/Parent")):
            detail.append(self.tr("parent {parent}").format(parent=state.ctx.object_label(int(state.value("Node/Parent")))))
        self.subtitle.setText("   ·   ".join(detail))

    def _rebuild(self):
        current = self.tabs.tabText(self.tabs.currentIndex()) if self.tabs.count() else ""
        page = self.tabs.currentWidget()
        scroll = page.verticalScrollBar().value() if isinstance(page, QScrollArea) else 0
        self.tabs.blockSignals(True)
        while self.tabs.count():
            widget = self.tabs.widget(0)
            self.tabs.removeTab(0)
            widget.deleteLater()
        self._built, self._inputs, self._rows, self._composites, self._rules = set(), {}, [], [], {}
        for tab in self._tab_pages:
            area = QScrollArea(self.tabs)
            area.setWidgetResizable(True)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            area.setFrameShape(QFrame.Shape.NoFrame)
            self.tabs.addTab(area, self.tr(tab.title))
        if not self._tab_pages:
            self.tabs.addTab(styled_label(self.tr("Nothing here can be edited."), self.tabs, emptyState=True), self.tr("Settings"))
        titles = [tab.title for tab in self._tab_pages]
        self.tabs.setCurrentIndex(titles.index(current) if current in titles else 0)
        self.tabs.blockSignals(False)
        self._tab_changed(self.tabs.currentIndex())
        area = self.tabs.currentWidget()
        if scroll and isinstance(area, QScrollArea):
            QTimer.singleShot(0, area, lambda: area.verticalScrollBar().setValue(scroll))

    def _tab_changed(self, index):
        if not 0 <= index < len(self._tab_pages) or index in self._built:
            return
        self._built.add(index)
        area = self.tabs.widget(index)
        content = QWidget(area)
        grid = QGridLayout(content)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(8)
        heights, wide, shown = [0, 0], [], set()
        for group in self._tab_pages[index].groups:
            if not group.controls:
                continue
            box = self._group(group, content, shown)
            if any(control.kind in _WIDE for control in group.controls):
                wide.append(box)
                continue
            column = 0 if heights[0] <= heights[1] else 1
            grid.addWidget(box, heights[column], column)
            heights[column] += 1
        row = max(heights)
        for box in wide:
            grid.addWidget(box, row, 0, 1, 2)
            row += 1
        grid.setRowStretch(row, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        area.setWidget(content)
        self._sync()

    def _group(self, group, parent, shown):
        box = QGroupBox(self.tr(group.title), parent)
        form = QFormLayout(box)
        form.setContentsMargins(8, 6, 8, 8)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(5)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        override = group.override
        if override and override not in shown:
            shown.add(override)
            check = _Check(self.tr("Override parent"), self._state.value(override), box)
            self._register(check, override)
            form.addRow(check)
        for control in group.controls:
            rules = [rule for rule in ((override, (1,)) if override else None, control.hints.get("enabled_by")) if rule]
            for widget in self._control(control, box, form):
                if rules:
                    self._rules[widget] = rules
        return box

    def _register(self, widget, path):
        widget.setProperty("wwise_path", path)
        widget.committed.connect(self._field_committed)
        self._inputs.setdefault(path, []).append(widget)

    def _control(self, control, box, form):
        state = self._state
        label = self.tr(control.label) if control.label else ""
        if control.kind == "field":
            field = state.fields[control.key]
            text = label or self.tr(split_unit(field.label)[0])
            widget, own = _input(field, control.hints, state.ctx, box, text)
            self._register(widget, control.key)
            if not control.hints.get("random"):
                form.addRow(widget) if own else form.addRow(text, widget)
                return [widget]
            row = QWidget(box)
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(widget, 1)
            widget.setParent(row)
            random = _Randomizer(field.storage, row, suffix=split_unit(field.label)[1])
            random.setProperty("wwise_range", control.hints["random"])
            random.committed.connect(self._range_committed)
            line.addWidget(random)
            self._composites.append(random)
            form.addRow(text, row)
            return [widget, random]
        if control.kind == "property":
            prop_id = control.hints["id"]
            name = property_names(state.kind, state.version).get(prop_id, f"Property 0x{prop_id:02X}")
            ranged = _join(control.key, "Randomizers/Randomizer count") in state.fields
            row = _PropertyRow(control.key, prop_id, name, state.kind, state.version, ranged, state.ctx, box)
            row.edit.connect(self._apply)
            self._rows.append(row)
            form.addRow(label or self.tr(split_unit(name)[0]), row)
            return [row]
        widget = _COMPOSITES[control.kind](control, state, box)
        widget.edit.connect(self._apply)
        self._composites.append(widget)
        if control.kind in _WIDE or not label:
            form.addRow(widget)
        else:
            form.addRow(label, widget)
        return [widget]

    def _sync(self):
        state = self._state
        for path, widgets in self._inputs.items():
            if path in state.fields:
                for widget in widgets:
                    widget.set_raw(state.fields[path].value)
        bundles = {}
        for row in self._rows:
            if row.section not in bundles:
                bundles[row.section] = (_bundle(state.layout.fields, row.section), _bundle(state.layout.fields, row.section, "Randomizers"))
            row.sync(*bundles[row.section])
        for widget in self._composites:
            if isinstance(widget, _Randomizer):
                low, high = (state.value(path) for path in widget.property("wwise_range"))
                widget.set_range((low, high) if low or high else None)
            else:
                widget.sync(state)
        for widget, rules in self._rules.items():
            widget.setEnabled(all(int(state.value(path)) in values for path, values in rules))

    def _field_committed(self):
        widget = self.sender()
        try:
            value = widget.raw()
        except ValueError as exc:
            self._error(str(exc))
            return
        self._apply(("set", {widget.property("wwise_path"): value}))

    def _range_committed(self):
        widget = self.sender()
        low, high = widget.raw() or (0, 0)
        self._apply(("set", dict(zip(widget.property("wwise_range"), (low, high)))))

    def _apply(self, op):
        try:
            payload = self._payload
            for step in op[1] if op[0] == "seq" else (op,):
                payload = self._step(payload, step)
        except (ValueError, OverflowError, KeyError, StopIteration) as exc:
            self._error(str(exc) or type(exc).__name__)
            self._sync()
            return
        self.status.hide()
        if payload == self._payload:
            return
        self._history.append(self._payload)
        self._future.clear()
        self._load(payload)
        self.edited.emit()

    def _error(self, message):
        self.status.setText(message)
        self.status.show()

    def _step(self, payload, step):
        layout = self._parse(payload)
        kind = step[0]
        if kind == "list":
            return reorder_wwise_list(payload, layout, step[1], step[2])
        if kind == "keyed":
            return set_wwise_keyed_values(payload, layout, step[1], step[2])
        if kind == "decision":
            return set_decision_tree(payload, layout, step[1], step[2])
        if kind in {"props", "ranges"}:
            return self._properties(payload, layout, kind, step[1], step[2])
        changes = dict(step[1])
        fields = {field.path: field for field in layout.fields}
        if self._source[:2] == ("hirc", 0x03):
            changes.update({"Target": value for path, value in step[1].items() if path in {"State", "Switch"}})
        for path, value in step[1].items():
            if path.startswith("Cues/") and path.endswith("/Name"):
                cue = path.rsplit("/", 1)[0] + "/Cue ID"
                if int(fields[cue].value) not in {_ENTRY_CUE, _EXIT_CUE}:
                    changes[cue] = wwise_id_from_name(value) if value else 0
        payload = set_wwise_fields(payload, layout, changes)
        for prefix, count_path in (("Cues/", "Cues/Cue count"), ("Clips/", "Clips/Clip count")):
            if any(path.startswith(prefix) for path in changes):
                layout = self._parse(payload)
                payload = reorder_wwise_list(payload, layout, count_path, range(int(layout.field(count_path).value)))
        for base in {match[1] for match in map(_POINT_FROM.match, changes) if match}:
            layout = self._parse(payload)
            values = {field.path: field.value for field in layout.fields}
            order = sorted(range(int(values[f"{base}/Point count"])), key=lambda index: values[f"{base}/Points/{index + 1}/From"])
            if order != sorted(order):
                payload = reorder_wwise_list(payload, layout, f"{base}/Point count", order)
        return payload

    def _properties(self, payload, layout, kind, section, changes):
        group, count = ("Properties", "Properties/Property count") if kind == "props" else ("Randomizers", "Randomizers/Randomizer count")
        current = _bundle(layout.fields, section, group)
        for prop_id, value in changes.items():
            if value is None:
                current.pop(prop_id, None)
            else:
                current[prop_id] = value
        payload = set_wwise_keyed_values(payload, layout, _join(section, count), current)
        removed = [prop_id for prop_id, value in changes.items() if value is None]
        randomizers = _join(section, "Randomizers/Randomizer count")
        if kind == "props" and removed:
            layout = self._parse(payload)
            ranges = _bundle(layout.fields, section, "Randomizers")
            if layout.field(randomizers) is not None and any(prop_id in ranges for prop_id in removed):
                payload = set_wwise_keyed_values(payload, layout, randomizers, {
                    prop_id: value for prop_id, value in ranges.items() if prop_id not in removed
                })
        return payload


def object_editor(obj, objects=(), metadata=None, parent=None):
    return WwisePropertyEditor(
        obj.payload, obj.structure, type_name=obj.type_name, obj=obj, objects=objects, metadata=metadata, parent=parent,
    )


def chunk_editor(chunk, objects=(), metadata=None, parent=None):
    return WwisePropertyEditor(
        chunk.payload, chunk.structure, type_name=chunk.title, objects=objects, metadata=metadata, parent=parent,
    )
