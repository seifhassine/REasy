from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields
from functools import partial
from pathlib import Path

lazy import numpy as np
from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP, Qt, Signal
lazy from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from file_handlers.clsp.clsp_file import CollisionShape, ShapeKind

lazy from .gpus import AnimationCurve, CurveKey, GpuStrandsPreset

_CONTEXT = "StrandForms"
translate = QCoreApplication.translate
_GROUPS = (
    (QT_TRANSLATE_NOOP("StrandForms", "Material"), ("radius", "density", "stretch_stiffness", "bend_stiffness",
                                                    "viscosity")),
    (QT_TRANSLATE_NOOP("StrandForms", "Weights"), ("use_deformation_weight", "use_relative_distance",
                                                   "strand_weight_curve", "use_responsive_curve", "responsive_curve")),
    (QT_TRANSLATE_NOOP("StrandForms", "Damping"), ("linear_damping", "angular_damping", "linear_ema", "angular_ema",
                                                   "use_damping_scaling_curve", "damping_scaling_curve")),
    (QT_TRANSLATE_NOOP("StrandForms", "Friction and collision"), ("static_friction", "kinetic_friction",
                                                                  "use_static_friction_curve",
                                                                  "static_friction_weights", "restitution",
                                                                  "collision_tag")),
    (QT_TRANSLATE_NOOP("StrandForms", "Limits"), ("use_swing_limit", "swing_limit", "swing_limit_stiffness",
                                                  "use_twist_limit", "twist_limit", "twist_limit_stiffness")),
    (QT_TRANSLATE_NOOP("StrandForms", "Volume"), ("use_volume_preservation", "repulsive_strength",
                                                  "use_inter_smoothing", "inter_smoothing_strength",
                                                  "density_cell_width", "velocity_cell_width")),
    (QT_TRANSLATE_NOOP("StrandForms", "Clumping"), ("clump_stray_amount", "clump_tightness", "clump_limit")),
)
_LABELS = {
    "radius": QT_TRANSLATE_NOOP("StrandForms", "Radius"),
    "density": QT_TRANSLATE_NOOP("StrandForms", "Density"),
    "stretch_stiffness": QT_TRANSLATE_NOOP("StrandForms", "Stretch stiffness"),
    "bend_stiffness": QT_TRANSLATE_NOOP("StrandForms", "Bend stiffness"),
    "viscosity": QT_TRANSLATE_NOOP("StrandForms", "Viscosity"),
    "use_deformation_weight": QT_TRANSLATE_NOOP("StrandForms", "Use deformation weight"),
    "use_relative_distance": QT_TRANSLATE_NOOP("StrandForms", "Use relative distance"),
    "strand_weight_curve": QT_TRANSLATE_NOOP("StrandForms", "Strand weight curve"),
    "use_responsive_curve": QT_TRANSLATE_NOOP("StrandForms", "Use responsive curve"),
    "responsive_curve": QT_TRANSLATE_NOOP("StrandForms", "Responsive curve"),
    "linear_damping": QT_TRANSLATE_NOOP("StrandForms", "Linear damping"),
    "angular_damping": QT_TRANSLATE_NOOP("StrandForms", "Angular damping"),
    "linear_ema": QT_TRANSLATE_NOOP("StrandForms", "Linear smoothing"),
    "angular_ema": QT_TRANSLATE_NOOP("StrandForms", "Angular smoothing"),
    "use_damping_scaling_curve": QT_TRANSLATE_NOOP("StrandForms", "Use damping scaling curve"),
    "damping_scaling_curve": QT_TRANSLATE_NOOP("StrandForms", "Damping scaling curve"),
    "static_friction": QT_TRANSLATE_NOOP("StrandForms", "Static friction"),
    "kinetic_friction": QT_TRANSLATE_NOOP("StrandForms", "Kinetic friction"),
    "use_static_friction_curve": QT_TRANSLATE_NOOP("StrandForms", "Use static friction weights"),
    "static_friction_weights": QT_TRANSLATE_NOOP("StrandForms", "Static friction weights"),
    "restitution": QT_TRANSLATE_NOOP("StrandForms", "Restitution"),
    "collision_tag": QT_TRANSLATE_NOOP("StrandForms", "Collision tag"),
    "use_swing_limit": QT_TRANSLATE_NOOP("StrandForms", "Use swing limit"),
    "swing_limit": QT_TRANSLATE_NOOP("StrandForms", "Swing limit"),
    "swing_limit_stiffness": QT_TRANSLATE_NOOP("StrandForms", "Swing limit stiffness"),
    "use_twist_limit": QT_TRANSLATE_NOOP("StrandForms", "Use twist limit"),
    "twist_limit": QT_TRANSLATE_NOOP("StrandForms", "Twist limit"),
    "twist_limit_stiffness": QT_TRANSLATE_NOOP("StrandForms", "Twist limit stiffness"),
    "use_volume_preservation": QT_TRANSLATE_NOOP("StrandForms", "Use volume preservation"),
    "repulsive_strength": QT_TRANSLATE_NOOP("StrandForms", "Repulsive strength"),
    "use_inter_smoothing": QT_TRANSLATE_NOOP("StrandForms", "Use inter-strand smoothing"),
    "inter_smoothing_strength": QT_TRANSLATE_NOOP("StrandForms", "Inter-strand smoothing"),
    "density_cell_width": QT_TRANSLATE_NOOP("StrandForms", "Density cell width"),
    "velocity_cell_width": QT_TRANSLATE_NOOP("StrandForms", "Velocity cell width"),
    "clump_stray_amount": QT_TRANSLATE_NOOP("StrandForms", "Clump stray amount"),
    "clump_tightness": QT_TRANSLATE_NOOP("StrandForms", "Clump tightness"),
    "clump_limit": QT_TRANSLATE_NOOP("StrandForms", "Clump limit"),
}
# Indexed by curve key kind, as ``AnimationCurve.at`` reads it.
_KINDS = (QT_TRANSLATE_NOOP("StrandForms", "Hermite"), QT_TRANSLATE_NOOP("StrandForms", "Linear"),
          QT_TRANSLATE_NOOP("StrandForms", "Constant"), QT_TRANSLATE_NOOP("StrandForms", "Hermite (3)"),
          QT_TRANSLATE_NOOP("StrandForms", "Hermite (4)"), QT_TRANSLATE_NOOP("StrandForms", "Weighted"),
          QT_TRANSLATE_NOOP("StrandForms", "Weighted (6)"))


def _tr(text: str) -> str:
    return QCoreApplication.translate(_CONTEXT, text)


def _shown(value: float) -> str:
    value = float(np.float32(value))
    return np.format_float_positional(value, trim="-") if 1e-4 <= abs(value) < 1e7 or value == 0 else \
        np.format_float_scientific(np.float32(value), trim="-")


def _value(text: str) -> float | None:
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def number_edit(get, put) -> QLineEdit:
    """``put`` raises ``ValueError`` to refuse a value."""
    edit = QLineEdit(_shown(get()))
    edit.setValidator(QDoubleValidator(edit))
    edit.validator().setNotation(QDoubleValidator.ScientificNotation)

    def commit():
        value = _value(edit.text())
        if value is not None and np.float32(value) != np.float32(get()):
            try:
                put(float(np.float32(value)))
            except ValueError as error:
                QMessageBox.warning(edit, translate("StrandForms", "Strands"), str(error))
        edit.setText(_shown(get()))
    edit.editingFinished.connect(commit)
    return edit


def _tag(text: str) -> int | None:
    try:
        value = int(text.strip().removeprefix("0x"), 16)
    except ValueError:
        return None
    return value if 0 <= value < 1 << 64 else None


def tag_edit(get, put) -> QLineEdit:
    edit = QLineEdit(f"{get():016X}")
    edit.setToolTip(translate("StrandForms", "The tag bits, in hexadecimal: a collider collides with the strands "
                                             "when its tag and their physics preset's share a bit."))

    def commit():
        value = _tag(edit.text())
        if value is not None and value != get():
            put(value)
        edit.setText(f"{get():016X}")
    edit.editingFinished.connect(commit)
    return edit


class JointField(QComboBox):
    chosen = Signal(str)

    def __init__(self, names: Sequence[str], current: str, free: bool = False, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.addItems(list(names))
        completer = self.completer()
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self._names, self._free, self._held = frozenset(names), free, current
        self.setCurrentText(current)
        self.lineEdit().editingFinished.connect(self._commit)
        self.activated.connect(lambda _index: self._commit())

    def name(self) -> str:
        return self.currentText().strip()

    def _commit(self) -> None:
        name = self.name()
        if not name or not (self._free or name in self._names):
            self.setCurrentText(self._held)
        elif name != self._held:
            self._held = name
            self.chosen.emit(name)


def _edit_button(slot) -> QPushButton:
    button = QPushButton(translate("StrandForms", "Edit…"))
    button.clicked.connect(slot)
    return button


def _form(parent: QWidget, title: str = "") -> QFormLayout:
    box = QGroupBox(title, parent) if title else parent
    form = QFormLayout(box)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    if title:
        parent.layout().addWidget(box)
    return form


class PresetForm(QWidget):
    changed = Signal()

    def __init__(self, preset: GpuStrandsPreset, parent=None):
        super().__init__(parent)
        self.preset = preset
        kinds = {f.name: f.metadata.get("kind") for f in fields(GpuStrandsPreset)}
        QVBoxLayout(self).setContentsMargins(0, 0, 0, 0)
        for title, names in _GROUPS:
            form = _form(self, _tr(title))
            for name in names:
                kind, label = kinds.get(name), _tr(_LABELS[name])
                if name == "static_friction_weights":
                    form.addRow(label, _edit_button(self._edit_weights))
                elif kind == "curve":
                    form.addRow(label, _edit_button(lambda _=False, name=name: self._edit_curve(name)))
                elif kind == "f":
                    form.addRow(label, number_edit(partial(getattr, preset, name), partial(self._set, name)))
                elif kind == "Q":
                    form.addRow(label, tag_edit(partial(getattr, preset, name), partial(self._set, name)))
                else:
                    form.addRow(self._switch(name, label))

    def _set(self, name: str, value) -> None:
        held = getattr(self.preset, name)
        setattr(self.preset, name, value)
        try:
            self.preset.validate()
        except ValueError:
            setattr(self.preset, name, held)
            raise
        self.changed.emit()

    def _switch(self, name: str, label: str) -> QWidget:
        value = int(getattr(self.preset, name))
        if value in (0, 1):
            box = QCheckBox(label)
            box.setChecked(bool(value))
            box.toggled.connect(lambda on: self._set(name, int(on)))
            return box
        spin = QSpinBox()
        spin.setRange(0, 65535)
        spin.setValue(value)
        spin.valueChanged.connect(lambda number: self._set(name, int(number)))
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(QLabel(label))
        line.addWidget(spin)
        return row

    def _edit_curve(self, name: str) -> None:
        curve = CurveDialog.edited(getattr(self.preset, name), _tr(_LABELS[name]), self)
        if curve is not None:
            self._set(name, curve)

    def _edit_weights(self) -> None:
        dialog = WeightsDialog(self.preset.static_friction_weights, self)
        if dialog.exec() == QDialog.Accepted:
            self._set("static_friction_weights", dialog.weights)


class LodForm(QWidget):
    changed = Signal(object)
    _FIELDS = ((QT_TRANSLATE_NOOP("StrandForms", "Screen share, most"), "screen_max", None),
               (QT_TRANSLATE_NOOP("StrandForms", "Screen share, least"), "screen_min", None),
               (QT_TRANSLATE_NOOP("StrandForms", "LOD 0 vertex ratio"), "ratios", 0),
               (QT_TRANSLATE_NOOP("StrandForms", "LOD 1 vertex ratio"), "ratios", 1),
               (QT_TRANSLATE_NOOP("StrandForms", "LOD 2 vertex ratio"), "ratios", 2),
               (QT_TRANSLATE_NOOP("StrandForms", "Factor offset"), "offset", None),
               (QT_TRANSLATE_NOOP("StrandForms", "Factor"), "factor", None))

    def __init__(self, record, parent=None):
        super().__init__(parent)
        self.record = record
        form = _form(self)
        form.setContentsMargins(0, 0, 0, 0)
        for label, name, index in self._FIELDS:
            form.addRow(_tr(label), number_edit(partial(self._get, name, index), partial(self._put, name, index)))
        form.addRow(translate("StrandForms", "Factor by screen share"), _edit_button(self._edit_curve))

    def _get(self, name: str, index: int | None) -> float:
        value = getattr(self.record, name)
        return value if index is None else value[index]

    def _put(self, name: str, index: int | None, value) -> None:
        if index is not None:
            held = list(self.record.ratios)
            held[index] = np.float32(value)
            value = tuple(held)
        elif name != "curve":
            value = np.float32(value)
        self.record = dataclasses.replace(self.record, **{name: value})
        self.changed.emit(self.record)

    def _edit_curve(self) -> None:
        curve = CurveDialog.edited(self.record.curve, translate("StrandForms", "Factor by screen share"), self)
        if curve is not None:
            self._put("curve", None, curve)


class ColliderForm(QWidget):
    """Values are in the shape's joints' space. ``rejointed`` gives (joint, end joint) hashes."""

    changed = Signal(object)
    rejointed = Signal(object, object)
    _AXES = "XYZ"
    # Each kind's fields: label, index of its first float, and float count.
    _FIELDS = {
        ShapeKind.SPHERE: ((QT_TRANSLATE_NOOP("StrandForms", "Centre {axis}"), 0, 3),
                           (QT_TRANSLATE_NOOP("StrandForms", "Radius"), 3, 1)),
        ShapeKind.CAPSULE: ((QT_TRANSLATE_NOOP("StrandForms", "Start {axis}"), 0, 3),
                            (QT_TRANSLATE_NOOP("StrandForms", "End {axis}"), 4, 3),
                            (QT_TRANSLATE_NOOP("StrandForms", "Radius"), 8, 1)),
        ShapeKind.TAPERED_CAPSULE: ((QT_TRANSLATE_NOOP("StrandForms", "Start {axis}"), 0, 3),
                                    (QT_TRANSLATE_NOOP("StrandForms", "Start radius"), 3, 1),
                                    (QT_TRANSLATE_NOOP("StrandForms", "End {axis}"), 4, 3),
                                    (QT_TRANSLATE_NOOP("StrandForms", "End radius"), 7, 1)),
        ShapeKind.BOX: ((QT_TRANSLATE_NOOP("StrandForms", "Centre {axis}"), 12, 3),
                        (QT_TRANSLATE_NOOP("StrandForms", "Half extent {axis}"), 16, 3)),
        ShapeKind.PLANE: ((QT_TRANSLATE_NOOP("StrandForms", "Normal {axis}"), 0, 3),
                          (QT_TRANSLATE_NOOP("StrandForms", "Distance"), 3, 1)),
    }

    _SIZES = ("Radius", "Start radius", "End radius", "Half extent {axis}")

    def __init__(self, shape: CollisionShape, joints: Mapping[int, str] | None = None, parent=None):
        super().__init__(parent)
        self.shape = shape
        form = _form(self)
        form.setContentsMargins(0, 0, 0, 0)
        if joints:
            keys = {name: key for key, name in joints.items()}
            capsule = shape.kind in (ShapeKind.CAPSULE, ShapeKind.TAPERED_CAPSULE)
            tip = translate("StrandForms",
                            "Choosing another joint keeps the shape in place and rewrites its values in that joint's "
                            "space.")
            for label, key, end in ((translate("StrandForms", "Joint"), shape.joint, False),
                                    (translate("StrandForms", "End joint"), shape.end_joint, True)):
                if end and not capsule:
                    continue
                field = JointField(list(joints.values()), joints.get(key, f"{key:08X}"))
                field.setToolTip(tip)
                field.chosen.connect(lambda name, end=end: self.rejointed.emit(
                    *((self.shape.joint, keys[name]) if end else (keys[name], self.shape.end_joint))))
                form.addRow(label, field)
        for label, first, count in self._FIELDS.get(ShapeKind(shape.kind), ()):
            for at in range(first, first + count):
                text = _tr(label).format(axis=self._AXES[at - first]) if count == 3 else _tr(label)
                form.addRow(text, number_edit(partial(self._get, at), partial(self._put, at)))
        form.addRow(translate("StrandForms", "Tag"), tag_edit(lambda: self.shape.tag, lambda tag: self._made(tag=tag)))

    def _get(self, at: int) -> float:
        return float(self.shape.values[at])

    def _put(self, at: int, value: float) -> None:
        sizes = {first + step for label, first, count in self._FIELDS[ShapeKind(self.shape.kind)]
                 if label in self._SIZES for step in range(count)}
        if value < 0 and at in sizes:
            raise ValueError(translate("StrandForms", "A radius or extent is not below 0."))
        values = np.array(self.shape.values, np.float32)
        values[at] = value
        self._made(values=values)

    def _made(self, **changes) -> None:
        self.shape = dataclasses.replace(self.shape, **changes)
        self.changed.emit(self.shape)


def _dialog_buttons(dialog: QDialog, accept) -> QDialogButtonBox:
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


class CurveDialog(QDialog):
    _COLUMNS = (QT_TRANSLATE_NOOP("StrandForms", "Time"), QT_TRANSLATE_NOOP("StrandForms", "Value"),
                QT_TRANSLATE_NOOP("StrandForms", "Interpolation"), QT_TRANSLATE_NOOP("StrandForms", "In Y"),
                QT_TRANSLATE_NOOP("StrandForms", "In X"), QT_TRANSLATE_NOOP("StrandForms", "Out Y"),
                QT_TRANSLATE_NOOP("StrandForms", "Out X"))

    @classmethod
    def edited(cls, curve: AnimationCurve, title: str, parent=None) -> AnimationCurve | None:
        dialog = cls(curve, title, parent)
        return dialog.curve if dialog.exec() == QDialog.Accepted else None

    def __init__(self, curve: AnimationCurve, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 360)
        self.held, self.curve = curve, None
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self._COLUMNS), self)
        self.table.setHorizontalHeaderLabels([_tr(text) for text in self._COLUMNS])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for key in curve.keys:
            self._add(key)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        add, remove = QPushButton(self.tr("Add key")), QPushButton(self.tr("Remove key"))
        add.clicked.connect(lambda: self._add(CurveKey(0.0, 1, self._last_time())))
        remove.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()))
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)
        bounds = QFormLayout()
        self.minimum, self.maximum = QLineEdit(_shown(curve.minimum)), QLineEdit(_shown(curve.maximum))
        self.clamped = QCheckBox(self.tr("Clamp the values between the bounds"))
        self.clamped.setChecked(bool(curve.flags & 1))
        bounds.addRow(self.tr("Least value"), self.minimum)
        bounds.addRow(self.tr("Greatest value"), self.maximum)
        bounds.addRow(self.clamped)
        layout.addLayout(bounds)
        layout.addWidget(_dialog_buttons(self, self._accept))

    def _last_time(self) -> float:
        times = [_value(self.table.item(row, 0).text()) for row in range(self.table.rowCount())]
        return max([time for time in times if time is not None], default=-0.1) + 0.1

    def _add(self, key: CurveKey) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate((key.time, key.value, None, key.in_y, key.in_x, key.out_y, key.out_x)):
            if column == 2:
                combo = QComboBox()
                combo.addItems([_tr(text) for text in _KINDS])
                combo.setCurrentIndex(int(key.kind))
                self.table.setCellWidget(row, column, combo)
            else:
                self.table.setItem(row, column, QTableWidgetItem(_shown(value)))

    def _accept(self) -> None:
        keys = []
        for row in range(self.table.rowCount()):
            numbers = [_value(self.table.item(row, column).text()) for column in (0, 1, 3, 4, 5, 6)]
            if any(number is None for number in numbers):
                QMessageBox.warning(self, self.windowTitle(), self.tr("Key {row} holds a value that is not a "
                                                                      "number.").format(row=row + 1))
                return
            time, value, in_y, in_x, out_y, out_x = numbers
            keys.append(CurveKey(value, self.table.cellWidget(row, 2).currentIndex(), time, in_y, in_x, out_y, out_x))
        keys.sort(key=lambda key: key.time)
        low, high = _value(self.minimum.text()), _value(self.maximum.text())
        if low is None or high is None:
            QMessageBox.warning(self, self.windowTitle(), self.tr("The bounds must be numbers."))
            return
        held = self.held
        curve = AnimationCurve(tuple(keys), held.loop_count, held.loop_start, held.loop_end, low, high, held.wrap,
                               (held.flags & ~1) | int(self.clamped.isChecked()))
        try:
            curve.validate()
        except ValueError as error:
            QMessageBox.warning(self, self.windowTitle(), str(error))
            return
        self.curve = curve
        self.accept()


class WeightsDialog(QDialog):
    def __init__(self, weights, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_tr(_LABELS["static_friction_weights"]))
        self.resize(260, 520)
        self.weights = None
        layout = QVBoxLayout(self)
        self.table = QTableWidget(len(weights), 1, self)
        self.table.setHorizontalHeaderLabels([self.tr("Weight")])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for row, weight in enumerate(weights):
            self.table.setItem(row, 0, QTableWidgetItem(_shown(weight)))
        layout.addWidget(self.table)
        layout.addWidget(_dialog_buttons(self, self._accept))

    def _accept(self) -> None:
        values = [_value(self.table.item(row, 0).text()) for row in range(self.table.rowCount())]
        if any(value is None for value in values):
            QMessageBox.warning(self, self.windowTitle(), self.tr("Every weight must be a number."))
            return
        self.weights = tuple(float(np.float32(value)) for value in values)
        self.accept()


class NewColliderDialog(QDialog):
    """``made`` holds (kind, path, joint, end joint, tag, size in m) once accepted."""

    _KINDS = ((ShapeKind.SPHERE, QT_TRANSLATE_NOOP("StrandForms", "Sphere")),
              (ShapeKind.CAPSULE, QT_TRANSLATE_NOOP("StrandForms", "Capsule")),
              (ShapeKind.TAPERED_CAPSULE, QT_TRANSLATE_NOOP("StrandForms", "Tapered capsule")),
              (ShapeKind.BOX, QT_TRANSLATE_NOOP("StrandForms", "Box")),
              (ShapeKind.PLANE, QT_TRANSLATE_NOOP("StrandForms", "Plane")))

    def __init__(self, files: Sequence[str], joints: Sequence[str], file: str, joint: str, tag: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("New collider"))
        self.made = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.kind = QComboBox()
        for kind, label in self._KINDS:
            self.kind.addItem(_tr(label), int(kind))
        self.file = QComboBox()
        for path in files:
            self.file.addItem(Path(path.replace("\\", "/")).name, path)
            self.file.setItemData(self.file.count() - 1, path, Qt.ToolTipRole)
        self.file.setCurrentIndex(max(self.file.findData(file), 0))
        self.joint, self.end_joint = JointField(joints, joint, True), JointField(joints, joint, True)
        tip = self.tr("A joint of the strands' bound mesh, or any typed joint name. Shapes on other joints are saved "
                      "but not drawn; the game hangs them on its character's joint of that name.")
        self.joint.setToolTip(tip)
        self.end_joint.setToolTip(tip)
        self._end_chosen = False
        self.joint.chosen.connect(lambda name: self._end_chosen or self.end_joint.setCurrentText(name))
        self.end_joint.chosen.connect(lambda _name: setattr(self, "_end_chosen", True))
        self.tag = QLineEdit(f"{tag:016X}")
        self.tag.setToolTip(translate("StrandForms", "The tag bits, in hexadecimal: a collider collides with the "
                                                     "strands when its tag and their physics preset's share a bit."))
        self.size = QDoubleSpinBox()
        self.size.setRange(0.1, 500.0)
        self.size.setDecimals(2)
        self.size.setValue(5.0)
        self.size.setSuffix(self.tr(" cm"))
        self.size.setToolTip(self.tr("Radius of a sphere or capsule, or half a box's side. The shape sits at its "
                                     "joint, in joint space; a one-joint capsule ends twice this far along the "
                                     "joint's x axis."))
        form.addRow(self.tr("Kind"), self.kind)
        form.addRow(self.tr("File"), self.file)
        form.addRow(self.tr("Joint"), self.joint)
        form.addRow(self.tr("End joint"), self.end_joint)
        form.addRow(self.tr("Tag"), self.tag)
        form.addRow(self.tr("Size"), self.size)
        layout.addLayout(form)
        layout.addWidget(_dialog_buttons(self, self._accept))
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._kind_changed()

    def _kind_changed(self, *_) -> None:
        kind = self.kind.currentData()
        self.end_joint.setEnabled(kind in (ShapeKind.CAPSULE, ShapeKind.TAPERED_CAPSULE))
        self.size.setEnabled(kind != ShapeKind.PLANE)

    def _accept(self) -> None:
        kind, joint, end_joint, tag = self.kind.currentData(), self.joint.name(), self.end_joint.name(), \
            _tag(self.tag.text())
        if not joint or (self.end_joint.isEnabled() and not end_joint):
            QMessageBox.warning(self, self.windowTitle(), self.tr("Name the joints the shape hangs on."))
            return
        if tag is None:
            QMessageBox.warning(self, self.windowTitle(), self.tr("The tag is 64 bits written in hexadecimal."))
            return
        self.made = (kind, self.file.currentData(), joint, end_joint if self.end_joint.isEnabled() else joint, tag,
                     self.size.value() / 100)
        self.accept()


class PresetImportDialog(QDialog):
    def __init__(self, name: str, kept: int, imported: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Import preset"))
        self.tag = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self.tr("{name} replaces the set's preset whole, saved at the set's preset path. Its "
                                        "collision tag:").format(name=name)))
        self.choices = QButtonGroup(self)
        self.typed = QLineEdit(f"{kept:016X}")
        for number, text in enumerate((self.tr("The set's, kept: {tag:016X}").format(tag=kept),
                                       self.tr("The preset's own: {tag:016X}").format(tag=imported),
                                       self.tr("This one:"))):
            button = QRadioButton(text)
            self.choices.addButton(button, number)
            layout.addWidget(button)
        layout.addWidget(self.typed)
        self.choices.button(0).setChecked(True)
        self.typed.setEnabled(False)
        self.choices.idToggled.connect(lambda number, on: number == 2 and self.typed.setEnabled(on))
        self._tags = (kept, imported)
        layout.addWidget(_dialog_buttons(self, self._accept))

    def _accept(self) -> None:
        chosen = self.choices.checkedId()
        tag = self._tags[chosen] if chosen < 2 else _tag(self.typed.text())
        if tag is None:
            QMessageBox.warning(self, self.windowTitle(), self.tr("The tag is 64 bits written in hexadecimal."))
            return
        self.tag = tag
        self.accept()


__all__ = ["ColliderForm", "CurveDialog", "JointField", "LodForm", "NewColliderDialog", "PresetForm",
           "PresetImportDialog", "WeightsDialog", "number_edit", "tag_edit"]
