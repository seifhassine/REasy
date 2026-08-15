"""Focused structured editors for supported Wwise HIRC schemas."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .bnk_parser import (
    BnkAttenuationCurve,
    BnkGraphPoint,
    HircReference,
    BnkMusicClip,
    BnkMusicMarker,
    BnkPlaylistItem,
    BnkPropertyRange,
    BnkPropertyValue,
    BnkSwitchMapping,
    BnkSwitchParam,
    compatible_hirc_reference_targets,
    bnk_property_name,
    can_edit_hirc_children,
    format_bnk_property_value,
    parse_bnk_property_value,
    set_action_fields,
    set_action_specific,
    set_attenuation,
    set_bank_sources,
    set_fx_parameters,
    set_hirc_children,
    set_music_segment,
    set_music_track_clips,
    set_property_bundle,
    set_random_sequence,
    set_silence_source,
    set_switch_container,
)
from .sound_metadata import SoundMetadata
from .wwise_schema import (
    BNK_CURVE_INTERPOLATION,
    BNK_CURVE_SCALING,
    BNK_FX_ENUMS,
    attenuation_targets,
    property_names,
)
from .wwise_v132 import set_v132_fields


_SOURCE_HEADER_FIELDS = (
    "Codec / source plug-in", "Storage type", "Source ID",
    "In-memory media bytes", "Source flags", "Plug-in parameter bytes",
)


class ActionPickerDialog(QDialog):
    """Order-preserving Event Action picker."""

    def __init__(self, actions, objects, selected_ids=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Choose Event Actions"))
        self.resize(760, 430)
        self._actions = {action.object_id: action for action in actions}
        self._objects = {obj.object_id: obj for obj in objects}
        self._metadata = getattr(parent, "_sound_metadata", SoundMetadata())

        layout = QVBoxLayout(self)
        hint = QLabel(self.tr("Actions run from top to bottom. Add existing Actions, then reorder them."))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        columns = QHBoxLayout()
        available = QVBoxLayout()
        available.addWidget(QLabel(self.tr("Available")))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(self.tr("Filter actions"))
        available.addWidget(self.filter_edit)
        self.available_list = QListWidget()
        self.available_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        available.addWidget(self.available_list)
        columns.addLayout(available, 1)

        moves = QVBoxLayout()
        moves.addStretch()
        self.add_button = QPushButton(self.tr("Add →"))
        self.remove_button = QPushButton(self.tr("← Remove"))
        moves.addWidget(self.add_button)
        moves.addWidget(self.remove_button)
        moves.addStretch()
        columns.addLayout(moves)

        selected = QVBoxLayout()
        selected.addWidget(QLabel(self.tr("Used by event")))
        self.selected_list = QListWidget()
        self.selected_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        selected.addWidget(self.selected_list)
        order = QHBoxLayout()
        self.up_button = QPushButton(self.tr("Move Up"))
        self.down_button = QPushButton(self.tr("Move Down"))
        order.addWidget(self.up_button)
        order.addWidget(self.down_button)
        selected.addLayout(order)
        columns.addLayout(selected, 1)
        layout.addLayout(columns)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for action_id in selected_ids:
            self.selected_list.addItem(self._item(action_id))
        self._refill_available()
        self.filter_edit.textChanged.connect(self._refill_available)
        self.add_button.clicked.connect(self._add_selected)
        self.remove_button.clicked.connect(self._remove_selected)
        self.up_button.clicked.connect(lambda: self._move_selected(-1))
        self.down_button.clicked.connect(lambda: self._move_selected(1))
        self.available_list.itemDoubleClicked.connect(lambda _item: self._add_selected())
        self.selected_list.itemDoubleClicked.connect(lambda _item: self._remove_selected())

    def _label(self, action_id: int) -> str:
        action = self._actions.get(action_id)
        if action is None:
            return self.tr("Missing Action {id}").format(id=action_id)
        settings = action.settings
        if action.target_kind in {"state", "switch"} and settings:
            group_kind = f"{action.target_kind}_group"
            value_kind = f"{action.target_kind}_value"
            target = (
                f"{self._metadata.label(group_kind, settings.group_id or 0)} → "
                f"{self._metadata.label(value_kind, settings.value_id or 0)}"
            )
        elif action.target_kind == "game_parameter":
            target = self._metadata.id_label("game_parameter", action.raw_id)
        elif action.target_kind == "trigger":
            target = self._metadata.id_label("trigger", action.raw_id)
        elif action.target_kind == "event":
            target = self._metadata.id_label("event", action.target_id, event=True)
        else:
            obj = self._objects.get(action.target_id)
            target = (
                f"{obj.type_name} {action.target_id}" if obj else
                self._metadata.external_object_label(action.target_id)
            )
        return self.tr("{name} → {target}  (Action {action_id})").format(
            name=action.action_name,
            target=target,
            action_id=action.object_id,
        )

    def _item(self, action_id: int) -> QListWidgetItem:
        item = QListWidgetItem(self._label(action_id))
        item.setData(Qt.UserRole, int(action_id))
        return item

    def selected_action_ids(self) -> tuple[int, ...]:
        return tuple(
            int(self.selected_list.item(row).data(Qt.UserRole))
            for row in range(self.selected_list.count())
        )

    def _refill_available(self):
        used = set(self.selected_action_ids())
        needle = self.filter_edit.text().strip().casefold()
        self.available_list.clear()
        for action_id in self._actions:
            item = self._item(action_id)
            if action_id not in used and (not needle or needle in item.text().casefold()):
                self.available_list.addItem(item)

    @staticmethod
    def _selected_rows(widget: QListWidget) -> list[int]:
        rows = sorted({widget.row(item) for item in widget.selectedItems()})
        return rows or ([widget.currentRow()] if widget.currentRow() >= 0 else [])

    def _add_selected(self):
        for row in self._selected_rows(self.available_list):
            self.selected_list.addItem(self._item(self.available_list.item(row).data(Qt.UserRole)))
        self._refill_available()

    def _remove_selected(self):
        for row in reversed(self._selected_rows(self.selected_list)):
            self.selected_list.takeItem(row)
        self._refill_available()

    def _move_selected(self, direction: int):
        rows = self._selected_rows(self.selected_list)
        if not rows or rows[0] + direction < 0 or rows[-1] + direction >= self.selected_list.count():
            return
        ids = list(self.selected_action_ids())
        for row in rows if direction < 0 else reversed(rows):
            ids[row], ids[row + direction] = ids[row + direction], ids[row]
        self.selected_list.clear()
        for action_id in ids:
            self.selected_list.addItem(self._item(action_id))
        for row in (value + direction for value in rows):
            self.selected_list.item(row).setSelected(True)
        self.selected_list.setCurrentRow(rows[0] + direction)


def _number(text, label, *, bits=32, allow_zero=True):
    value = str(text).strip()
    try:
        parsed = int(value, 0)
    except ValueError:
        parsed = int(value, 10)
    limit = (1 << bits) - 1
    if parsed < 0 or parsed > limit or (not allow_zero and parsed == 0):
        raise ValueError(f"{label} must be between {0 if allow_zero else 1} and {limit}.")
    return parsed


def _id_list(text, label="Child IDs"):
    values = str(text).replace(";", ",").split(",")
    return tuple(
        _number(value, label, allow_zero=False)
        for value in values
        if value.strip()
    )


class RowsEditor(QWidget):
    """Compact add/remove table shared by the structured property dialogs."""

    def __init__(self, headers, rows=(), defaults=(), parent=None, *, fixed=False):
        super().__init__(parent)
        self.defaults = tuple(map(str, defaults))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)
        if not fixed:
            buttons = QHBoxLayout()
            add = QPushButton(self.tr("Add Row"))
            remove = QPushButton(self.tr("Remove Row"))
            up, down = QPushButton(self.tr("Move Up")), QPushButton(self.tr("Move Down"))
            add.clicked.connect(lambda: self.add_row(self.defaults))
            remove.clicked.connect(self.remove_rows)
            up.clicked.connect(lambda: self.move_row(-1))
            down.clicked.connect(lambda: self.move_row(1))
            buttons.addWidget(add)
            buttons.addWidget(remove)
            buttons.addWidget(up)
            buttons.addWidget(down)
            buttons.addStretch()
            layout.addLayout(buttons)
        for row in rows:
            self.add_row(row)

    def add_row(self, values=()):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column in range(self.table.columnCount()):
            value = values[column] if column < len(values) else ""
            self.table.setItem(row, column, QTableWidgetItem(str(value)))

    def remove_rows(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for row in rows:
            self.table.removeRow(row)

    def move_row(self, direction):
        row, target = self.table.currentRow(), self.table.currentRow() + direction
        if row < 0 or target < 0 or target >= self.table.rowCount():
            return
        for column in range(self.table.columnCount()):
            first, second = self.table.takeItem(row, column), self.table.takeItem(target, column)
            first_widget, second_widget = self.table.cellWidget(row, column), self.table.cellWidget(target, column)
            if first_widget:
                self.table.removeCellWidget(row, column)
            if second_widget:
                self.table.removeCellWidget(target, column)
            if second:
                self.table.setItem(row, column, second)
            if first:
                self.table.setItem(target, column, first)
            if second_widget:
                self.table.setCellWidget(row, column, second_widget)
            if first_widget:
                self.table.setCellWidget(target, column, first_widget)
        self.table.selectRow(target)

    def values(self):
        return [
            [
                self.table.item(row, column).text().strip()
                if self.table.item(row, column)
                else ""
                for column in range(self.table.columnCount())
            ]
            for row in range(self.table.rowCount())
        ]


class ObjectIdCombo(QComboBox):
    """HIRC target picker limited to validated objects supplied by the caller."""

    def __init__(self, objects, current=0, parent=None, *, metadata=None):
        super().__init__(parent)
        self.setEditable(False)
        ordered = sorted(objects, key=lambda item: (item.type_name, item.object_id))
        if current and all(item.object_id != current for item in ordered):
            label = (
                metadata.external_object_label(current)
                if metadata is not None else
                self.tr("External / unavailable Wwise object {id}").format(id=current)
            )
            self.addItem(label, int(current))
        elif not current:
            self.addItem(self.tr("No target (preserved)"), 0)
        for obj in ordered:
            self.addItem(f"{obj.type_name} · {obj.object_id}", obj.object_id)
        index = self.findData(int(current))
        if index >= 0:
            self.setCurrentIndex(index)
        elif current:
            self.setEditText(str(current))

    def object_id(self):
        return int(self.currentData())


class NamedIdEdit(QWidget):
    """Numeric/name input with the active game's recovered label kept visible."""

    def __init__(self, metadata, category, current=0, parent=None, *, event=False):
        super().__init__(parent)
        self.metadata, self.category, self.is_event_id = metadata, category, event
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(str(int(current or 0)))
        self.edit.setPlaceholderText(self.tr("name or numeric ID"))
        self.name = QLabel()
        self.name.setStyleSheet("color: #b8bdc7;")
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.name, 1)
        self.edit.textChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        try:
            object_id = self.metadata.resolve_id(
                self.category, self.edit.text(), event=self.is_event_id
            )
            names = (
                self.metadata.event_names(object_id)
                if self.is_event_id else self.metadata.names(self.category, object_id)
            )
            self.name.setText(" / ".join(names) or self.tr("unresolved name"))
            bindings = self.metadata.bindings(self.category, object_id)
            self.setToolTip("\n".join(bindings))
        except ValueError:
            self.name.setText(self.tr("invalid or ambiguous"))

    def object_id(self, label="ID"):
        try:
            return self.metadata.resolve_id(
                self.category, self.edit.text(), event=self.is_event_id
            )
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from exc

    def set_category(self, category):
        self.category = category
        self._refresh()


class PropertyRowsEditor(QWidget):
    """Friendly editor for an AkPropValue or randomizer table."""

    def __init__(
        self, rows=(), parent=None, *, ranged=False, property_kind="object",
        bank_version=125,
    ):
        super().__init__(parent)
        self.ranged = ranged
        self.property_kind = property_kind
        self.bank_version = bank_version
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        headers = [self.tr("Property"), self.tr("Minimum"), self.tr("Maximum")] if ranged else [self.tr("Property"), self.tr("Value")]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumHeight(145)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        add, remove = QPushButton(self.tr("Add Property")), QPushButton(self.tr("Remove"))
        add.clicked.connect(lambda: self.add_row())
        remove.clicked.connect(self.remove_rows)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        layout.addLayout(buttons)
        for row in rows:
            self.add_row(row)

    def add_row(self, value=None):
        row = self.table.rowCount()
        self.table.insertRow(row)
        combo = QComboBox()
        combo.setMinimumContentsLength(24)
        names = property_names(self.property_kind, self.bank_version)
        ids = sorted(names)
        used = {
            int(self.table.cellWidget(index, 0).currentData())
            for index in range(row)
            if self.table.cellWidget(index, 0)
        }
        property_id = (
            int(value.property_id) if value is not None
            else next((item_id for item_id in ids if item_id not in used), ids[0])
        )
        if property_id not in ids:
            ids.append(property_id)
        for item_id in ids:
            combo.addItem(
                f"0x{item_id:02X} — "
                f"{bnk_property_name(item_id, self.property_kind, self.bank_version)}",
                item_id,
            )
        combo.setCurrentIndex(combo.findData(property_id))
        combo.setProperty("previous_id", property_id)
        combo.currentIndexChanged.connect(lambda _index, combo=combo: self._property_changed(combo))
        self.table.setCellWidget(row, 0, combo)
        bits = (
            (value.minimum_bits, value.maximum_bits)
            if self.ranged and value is not None
            else (0, 0)
            if self.ranged
            else (value.value_bits,)
            if value is not None
            else (0,)
        )
        for column, raw in enumerate(bits, 1):
            self.table.setItem(row, column, QTableWidgetItem(
                format_bnk_property_value(
                    property_id, raw, self.property_kind, self.bank_version
                )
            ))

    def _property_changed(self, combo):
        row = next(
            (index for index in range(self.table.rowCount()) if self.table.cellWidget(index, 0) is combo),
            -1,
        )
        if row < 0:
            return
        old_id, new_id = int(combo.property("previous_id")), int(combo.currentData())
        for column in range(1, self.table.columnCount()):
            item = self.table.item(row, column)
            try:
                bits = parse_bnk_property_value(
                    old_id, item.text() if item else "0",
                    self.property_kind, self.bank_version,
                )
            except (TypeError, ValueError):
                bits = 0
            if item:
                item.setText(format_bnk_property_value(
                    new_id, bits, self.property_kind, self.bank_version
                ))
        combo.setProperty("previous_id", new_id)

    def remove_rows(self):
        rows = sorted({item.row() for item in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for row in rows:
            self.table.removeRow(row)

    def entries(self):
        result = []
        for row in range(self.table.rowCount()):
            property_id = int(self.table.cellWidget(row, 0).currentData())
            values = [
                parse_bnk_property_value(
                    property_id,
                    self.table.item(row, column).text() if self.table.item(row, column) else "0",
                    self.property_kind,
                    self.bank_version,
                )
                for column in range(1, self.table.columnCount())
            ]
            result.append(
                BnkPropertyRange(property_id, *values)
                if self.ranged else BnkPropertyValue(property_id, values[0])
            )
        return tuple(result)


class PropertyBundleEditor(QTabWidget):
    def __init__(self, bundle, parent=None):
        super().__init__(parent)
        options = {"property_kind": bundle.kind, "bank_version": bundle.bank_version}
        self.values = PropertyRowsEditor(bundle.values, **options)
        self.ranges = (
            PropertyRowsEditor(bundle.ranges, ranged=True, **options)
            if bundle.has_ranges else None
        )
        self.addTab(self.values, self.tr("Values"))
        if self.ranges:
            self.addTab(self.ranges, self.tr("Random ranges"))
        self.setToolTip(
            self.tr("Properties absent from this object inherit/default in Wwise. Add one to override it here.")
        )


class CurvePointsEditor(RowsEditor):
    """Distance/value points with named Wwise interpolation choices."""

    def __init__(self, points=(), parent=None):
        super().__init__(
            [self.tr("Distance"), self.tr("Value"), self.tr("Interpolation")],
            defaults=(0, 0, 4),
            parent=parent,
        )
        for point in points:
            self.add_row((point.x, point.y, point.interpolation))

    def add_row(self, values=(0, 0, 4)):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(values[0])))
        self.table.setItem(row, 1, QTableWidgetItem(str(values[1])))
        choice = QComboBox()
        for value, label in BNK_CURVE_INTERPOLATION.items():
            choice.addItem(label, value)
        interpolation = int(values[2])
        if choice.findData(interpolation) < 0:
            choice.addItem(self.tr("Unknown ({value})").format(value=interpolation), interpolation)
        choice.setCurrentIndex(choice.findData(interpolation))
        self.table.setCellWidget(row, 2, choice)

    def entries(self):
        return tuple(
            BnkGraphPoint(
                float(self.table.item(row, 0).text()),
                float(self.table.item(row, 1).text()),
                int(self.table.cellWidget(row, 2).currentData()),
            )
            for row in range(self.table.rowCount())
        )


class FxParametersEditor(QWidget):
    """Searchable typed editor for the active Wwise schema's built-in plug-ins."""

    def __init__(self, fx, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Filter plug-in settings"))
        layout.addWidget(search)
        self.table = QTableWidget(len(fx.parameters), 2)
        self.table.setHorizontalHeaderLabels((self.tr("Setting"), self.tr("Value")))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setMinimumHeight(390)
        self.rows = []
        for row, parameter in enumerate(fx.parameters):
            name = QTableWidgetItem(self.tr(parameter.name))
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, name)
            if parameter.storage == "bool":
                editor = QCheckBox(self.tr("Enabled"))
                editor.setChecked(bool(parameter.value))
            elif parameter.enum_name:
                editor = QComboBox()
                choices = BNK_FX_ENUMS[parameter.enum_name]
                for value, label in (
                    choices.items() if isinstance(choices, dict) else enumerate(choices)
                ):
                    editor.addItem(self.tr(label), value)
                if editor.findData(int(parameter.value)) < 0:
                    editor.addItem(
                        self.tr("Unknown ({value})").format(value=parameter.value),
                        int(parameter.value),
                    )
                editor.setCurrentIndex(editor.findData(int(parameter.value)))
            elif parameter.storage == "f32":
                editor = QDoubleSpinBox()
                editor.setRange(-1_000_000_000.0, 1_000_000_000.0)
                editor.setDecimals(6)
                editor.setSingleStep(0.1)
                editor.setValue(float(parameter.value))
                editor.setProperty("exact_value", float(parameter.value))
                editor.setProperty("initial_display_value", editor.value())
            else:
                editor = QLineEdit(str(int(parameter.value)))
            self.table.setCellWidget(row, 1, editor)
            self.rows.append((parameter, editor))
        layout.addWidget(self.table)
        search.textChanged.connect(self._filter)

    def _filter(self, text):
        needle = text.strip().casefold()
        for row in range(self.table.rowCount()):
            self.table.setRowHidden(row, bool(needle and needle not in self.table.item(row, 0).text().casefold()))

    def parameters(self):
        result = []
        for parameter, editor in self.rows:
            if parameter.storage == "bool":
                value = int(editor.isChecked())
            elif parameter.enum_name:
                value = int(editor.currentData())
            elif parameter.storage == "f32":
                value = (
                    float(editor.property("exact_value"))
                    if editor.value() == editor.property("initial_display_value") else editor.value()
                )
            else:
                value = _number(editor.text(), parameter.name)
            result.append(replace(parameter, value=value))
        return tuple(result)


class AttenuationEditor(QWidget):
    """Named curve assignments and editable Wwise attenuation graph points."""

    def __init__(self, attenuation, bank_version=125, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        targets = attenuation_targets(bank_version)
        self.assignments = QTableWidget(len(targets), 2)
        self.assignments.setHorizontalHeaderLabels((self.tr("Affects"), self.tr("Curve")))
        self.assignments.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.assignment_choices = []
        for row, target in enumerate(targets):
            item = QTableWidgetItem(self.tr(target))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.assignments.setItem(row, 0, item)
            choice = QComboBox()
            self.assignments.setCellWidget(row, 1, choice)
            self.assignment_choices.append(choice)
        layout.addWidget(self.assignments)

        self.curves = QTabWidget()
        self.curve_editors = []
        layout.addWidget(self.curves, 1)
        controls = QHBoxLayout()
        add, remove = QPushButton(self.tr("Add Curve")), QPushButton(self.tr("Remove Curve"))
        add.clicked.connect(lambda: self._add_curve())
        remove.clicked.connect(self._remove_curve)
        controls.addWidget(add)
        controls.addWidget(remove)
        controls.addStretch()
        layout.addLayout(controls)
        for curve in attenuation.curves:
            self._add_curve(curve, refresh=False)
        self._refresh_assignments(attenuation.assignments)

    def _add_curve(self, curve=None, *, refresh=True):
        curve = curve or BnkAttenuationCurve(
            0, (BnkGraphPoint(0.0, 0.0, 4), BnkGraphPoint(100.0, 0.0, 4))
        )
        page, form = QWidget(), QFormLayout()
        page.setLayout(form)
        scaling = QComboBox()
        for value, label in BNK_CURVE_SCALING.items():
            scaling.addItem(self.tr(label), value)
        if scaling.findData(curve.scaling) < 0:
            scaling.addItem(self.tr("Unknown ({value})").format(value=curve.scaling), curve.scaling)
        scaling.setCurrentIndex(scaling.findData(curve.scaling))
        points = CurvePointsEditor(curve.points)
        form.addRow(self.tr("Value scaling"), scaling)
        form.addRow(self.tr("Curve points"), points)
        self.curve_editors.append((scaling, points))
        self.curves.addTab(page, self.tr("Curve {number}").format(number=len(self.curve_editors)))
        self.curves.setCurrentWidget(page)
        if refresh:
            self._refresh_assignments()

    def _remove_curve(self):
        removed = self.curves.currentIndex()
        if removed < 0:
            return
        values = [int(choice.currentData()) for choice in self.assignment_choices]
        self.curves.removeTab(removed)
        del self.curve_editors[removed]
        values = [-1 if value == removed else value - 1 if value > removed else value for value in values]
        for index in range(self.curves.count()):
            self.curves.setTabText(index, self.tr("Curve {number}").format(number=index + 1))
        self._refresh_assignments(values)

    def _refresh_assignments(self, values=None):
        values = values or [int(choice.currentData()) for choice in self.assignment_choices]
        for choice, value in zip(self.assignment_choices, values):
            choice.blockSignals(True)
            choice.clear()
            choice.addItem(self.tr("None"), -1)
            for index in range(len(self.curve_editors)):
                choice.addItem(self.tr("Curve {number}").format(number=index + 1), index)
            choice.setCurrentIndex(max(0, choice.findData(int(value))))
            choice.blockSignals(False)

    def values(self):
        assignments = tuple(int(choice.currentData()) for choice in self.assignment_choices)
        curves = tuple(
            BnkAttenuationCurve(int(scaling.currentData()), points.entries())
            for scaling, points in self.curve_editors
        )
        return assignments, curves


class WwiseFieldsEditor(QWidget):
    """Compact tree for the complete field-level Wwise representation."""

    def __init__(
        self, layout, metadata, hidden_prefixes=(), parent=None, *, objects=()
    ):
        super().__init__(parent)
        self.layout, self.metadata = layout, metadata
        self._objects = {}
        for obj in objects:
            self._objects.setdefault(obj.object_id, []).append(obj)
        self._items = {}
        tree = self.tree = QTreeWidget()
        tree.setColumnCount(3)
        tree.setHeaderLabels((self.tr("Setting"), self.tr("Value"), self.tr("Meaning")))
        tree.setRootIsDecorated(True)
        tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree.itemDoubleClicked.connect(self._edit_item)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tree.header().setStretchLastSection(True)
        roots = {}
        for field in layout.fields:
            if not field.visible or any(
                field.path == prefix or field.path.startswith(prefix + "/")
                for prefix in hidden_prefixes
            ):
                continue
            parts = field.path.split("/")
            parent_item = tree.invisibleRootItem()
            prefix = []
            for part in parts[:-1]:
                prefix.append(part)
                key = "/".join(prefix)
                item = roots.get(key)
                if item is None:
                    item = QTreeWidgetItem(parent_item, (part, "", ""))
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    roots[key] = item
                parent_item = item
            value = self._display(field)
            meaning = field.enum_label()
            if field.id_kind and int(field.value):
                object_id = int(field.value)
                names = metadata.names(field.id_kind, object_id)
                if names:
                    meaning = " / ".join(names)
                elif field.id_kind == "hirc":
                    local = self._objects.get(object_id, ())
                    if local:
                        meaning = " / ".join(dict.fromkeys(
                            obj.type_name for obj in local
                        ))
                    elif metadata.external_object(object_id):
                        meaning = metadata.external_object_label(object_id)
            item = QTreeWidgetItem(parent_item, (parts[-1], value, meaning))
            item.setData(0, Qt.ItemDataRole.UserRole, field.path)
            item.setData(1, Qt.ItemDataRole.UserRole, value)
            item.setToolTip(
                0,
                self.tr("Payload offset 0x{offset:X}, {size} byte(s), {storage}").format(
                    offset=field.offset, size=field.size, storage=field.storage,
                ) + (
                    self.tr("\nReference targets are changed through a type-safe picker.")
                    if field.reference_role else ""
                ),
            )
            if field.editable and not field.reference_role:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setForeground(1, tree.palette().brush(tree.foregroundRole()))
            self._items[field.path] = item
        tree.expandToDepth(0)
        layout_box = QVBoxLayout(self)
        layout_box.setContentsMargins(0, 0, 0, 0)
        layout_box.addWidget(tree)

    def _edit_item(self, item, column):
        path = item.data(0, Qt.ItemDataRole.UserRole)
        field = next((value for value in self.layout.fields if value.path == path), None)
        if (
            column == 1 and field is not None
            and field.editable and not field.reference_role
        ):
            self.tree.editItem(item, 1)

    @staticmethod
    def _display(field):
        if field.storage.startswith("f"):
            return repr(float(field.value))
        return str(field.value)

    def changes(self):
        changes = {}
        fields = {field.path: field for field in self.layout.fields}
        for path, item in self._items.items():
            original = item.data(1, Qt.ItemDataRole.UserRole)
            text = item.text(1).strip()
            if text == original:
                continue
            field = fields[path]
            enum = {label.casefold(): value for value, label in field.enum}
            if text.casefold() in enum:
                changes[path] = enum[text.casefold()]
            elif field.storage.startswith("f"):
                changes[path] = float(text)
            else:
                changes[path] = int(text, 0)
        return changes


class HircPropertiesDialog(QDialog):
    """Typed properties for supported schemas; opaque bytes stay read-only."""

    def __init__(self, obj, parent=None, objects=()):
        super().__init__(parent)
        self.obj, self._payload = obj, None
        if not objects and parent is not None and getattr(parent, "_parse_result", None):
            objects = parent._parse_result.objects
        self.objects = tuple(objects)
        self.metadata = getattr(parent, "_sound_metadata", SoundMetadata())
        self.setWindowTitle(self.tr("Edit {type} {id}").format(type=obj.type_name, id=obj.object_id))
        self.resize(780, 610)
        layout = QVBoxLayout(self)
        hint = QLabel(self.tr("Only the fields shown below are rebuilt; every other byte in this HIRC object is preserved."))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #b8bdc7;")
        layout.addWidget(hint)
        form_content = QWidget()
        self.form = QFormLayout(form_content)
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form_scroll.setWidget(form_content)
        layout.addWidget(self.form_scroll, 1)
        self.kind = "read_only"
        self._build()
        self.property_editor = None
        if obj.property_bundle is not None:
            if self.kind == "read_only":
                self.form.removeRow(self.read_only)
                self.kind = "properties"
            self.property_editor = PropertyBundleEditor(obj.property_bundle)
            self.form.addRow(self.tr("Playback properties"), self.property_editor)
        self.structure_editor = None
        if obj.structure and obj.structure.complete:
            self.structure_editor = WwiseFieldsEditor(
                obj.structure, self.metadata, self._covered_structure_fields(),
                objects=self.objects,
            )
            complete = QGroupBox(self.tr("All compiled Wwise settings"))
            complete.setCheckable(True)
            complete.setChecked(
                self.kind in {"read_only", "structure", "children", "properties"}
                or (self.kind == "fx" and not obj.fx_plugin.parameters)
            )
            complete_layout = QVBoxLayout(complete)
            complete_layout.addWidget(self.structure_editor)
            self.structure_editor.setVisible(complete.isChecked())
            complete.toggled.connect(self.structure_editor.setVisible)
            self.form.addRow(complete)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _spin(value, maximum=0xFFFF):
        widget = QSpinBox()
        widget.setRange(0, maximum)
        widget.setValue(int(value))
        return widget

    @staticmethod
    def _time(value):
        widget = QDoubleSpinBox()
        widget.setRange(-86_400_000.0, 86_400_000.0)
        widget.setDecimals(4)
        widget.setValue(float(value))
        widget.setProperty("exact_value", float(value))
        widget.setProperty("initial_display_value", widget.value())
        widget.setSuffix(" ms")
        return widget

    @staticmethod
    def _decimal(value, suffix=""):
        widget = QDoubleSpinBox()
        widget.setRange(-1_000_000_000.0, 1_000_000_000.0)
        widget.setDecimals(6)
        widget.setValue(float(value))
        widget.setProperty("exact_value", float(value))
        widget.setProperty("initial_display_value", widget.value())
        widget.setSuffix(suffix)
        return widget

    @staticmethod
    def _time_value(widget):
        return (
            float(widget.property("exact_value"))
            if widget.value() == widget.property("initial_display_value")
            else widget.value()
        )

    _decimal_value = _time_value

    @staticmethod
    def _line(value):
        return QLineEdit(str(value))

    @staticmethod
    def _choice(value, choices):
        widget = QComboBox()
        for label, number in choices:
            widget.addItem(label, number)
        if widget.findData(int(value)) < 0:
            widget.addItem(f"Unknown ({int(value)})", int(value))
        widget.setCurrentIndex(widget.findData(int(value)))
        return widget

    def _build_action(self, obj):
        self.kind = "action"
        self.action_type = self._line(f"0x{obj.action_type:04X}")
        self.action_target_kind = obj.action_target_kind
        self.target = self.named_target = None
        self.form.addRow(self.tr("Action"), QLabel(obj.action_name or self.tr("Unknown")))
        self.form.addRow(self.tr("Variant code (advanced)"), self.action_type)

        settings = obj.action_settings
        raw_id = obj.action_raw_id or 0
        if obj.action_target_kind == "object":
            field = next((
                item for item in obj.reference_fields
                if item.role == "action target"
            ), HircReference(6, obj.action_target_id or 0, "action target"))
            targets = compatible_hirc_reference_targets(
                obj, field, self.objects
            )
            self.target = ObjectIdCombo(
                targets, obj.action_target_id or 0, metadata=self.metadata
            )
            self.target_bus = QCheckBox(self.tr("Target is an Audio Bus"))
            self.target_bus.setChecked(obj.action_target_is_bus)
            self.target_bus.setEnabled(False)
            self.target_bus.setToolTip(self.tr(
                "Determined by the compiled Action variant; only compatible targets are listed."
            ))
            self.form.addRow(self.tr("Target object"), self.target)
            self.form.addRow("", self.target_bus)
        elif obj.action_target_kind == "event":
            self.named_target = NamedIdEdit(self.metadata, "event", raw_id, event=True)
            self.form.addRow(self.tr("Target Event"), self.named_target)
        elif obj.action_target_kind == "trigger":
            self.named_target = NamedIdEdit(self.metadata, "trigger", raw_id)
            self.form.addRow(self.tr("Trigger"), self.named_target)
        elif obj.action_target_kind == "game_parameter":
            value = settings.parameter_id if settings else raw_id
            self.named_target = NamedIdEdit(self.metadata, "game_parameter", value)
            self.form.addRow(self.tr("Game Parameter"), self.named_target)
        elif obj.action_target_kind in {"state", "switch"} and settings:
            prefix = obj.action_target_kind
            self.action_group = NamedIdEdit(
                self.metadata, f"{prefix}_group", settings.group_id or 0
            )
            self.action_value = NamedIdEdit(
                self.metadata, f"{prefix}_value", settings.value_id or 0
            )
            self.form.addRow(
                self.tr("State Group") if prefix == "state" else self.tr("Switch Group"),
                self.action_group,
            )
            self.form.addRow(
                self.tr("State") if prefix == "state" else self.tr("Switch"),
                self.action_value,
            )
        else:
            self.raw_target = self._line(raw_id)
            self.form.addRow(self.tr("External ID"), self.raw_target)

        self.fade_curve = self.action_bank_id = self.action_value_meaning = None
        self.action_value_base = self.action_value_min = self.action_value_max = None
        self.action_bypass = self.action_primary = self.apply_states = self.apply_sequences = None
        self.action_target_mask = self.action_relative = self.action_snap = None
        self.action_exceptions = None
        if not settings:
            return
        if settings.fade_curve is not None:
            self.fade_curve = self._choice(settings.fade_curve, (
                (self.tr("Logarithmic 3"), 0), (self.tr("Sine"), 1),
                (self.tr("Logarithmic 1"), 2), (self.tr("Inverse S-curve"), 3),
                (self.tr("Linear"), 4), (self.tr("S-curve"), 5),
                (self.tr("Exponential 1"), 6), (self.tr("Reciprocal sine"), 7),
                (self.tr("Exponential 3"), 8), (self.tr("Constant"), 9),
            ))
            self.form.addRow(self.tr("Fade curve"), self.fade_curve)
        if settings.kind == "play":
            self.action_bank_id = NamedIdEdit(
                self.metadata, "bank", settings.bank_id or 0
            )
            self.form.addRow(self.tr("Required bank ShortID"), self.action_bank_id)
        if settings.kind in {"stop", "pause", "resume"}:
            flags = settings.stop_flags or 0
            if settings.kind in {"pause", "resume"}:
                label = (
                    self.tr("Include pending Resume actions")
                    if settings.kind == "pause" else self.tr("Master resume")
                )
                self.action_primary = QCheckBox(label)
                self.action_primary.setChecked(bool(flags & 1))
                self.form.addRow("", self.action_primary)
            self.apply_states = QCheckBox(self.tr("Apply to State transitions"))
            self.apply_states.setChecked(bool(flags & 0x02))
            self.apply_sequences = QCheckBox(self.tr("Apply to Dynamic Sequences"))
            self.apply_sequences.setChecked(bool(flags & 0x04))
            self.form.addRow("", self.apply_states)
            self.form.addRow("", self.apply_sequences)
        if settings.kind in {"set_value", "game_parameter"}:
            if settings.kind == "game_parameter":
                self.action_bypass = QCheckBox(self.tr("Bypass transition time"))
                self.action_bypass.setChecked(bool(settings.bypass_transition))
                self.form.addRow("", self.action_bypass)
            self.action_value_meaning = self._choice(settings.value_meaning or 0, (
                (self.tr("Absolute / default"), 0),
                (self.tr("Independent"), 1),
                (self.tr("Offset"), 2),
            ))
            self.action_value_base = self._decimal(
                0.0 if settings.value is None else settings.value
            )
            self.action_value_min = self._decimal(
                0.0 if settings.minimum is None else settings.minimum
            )
            self.action_value_max = self._decimal(
                0.0 if settings.maximum is None else settings.maximum
            )
            self.form.addRow(self.tr("Value meaning"), self.action_value_meaning)
            self.form.addRow(self.tr("Base value"), self.action_value_base)
            self.form.addRow(self.tr("Random minimum"), self.action_value_min)
            self.form.addRow(self.tr("Random maximum"), self.action_value_max)
        if settings.kind == "bypass_fx":
            self.action_bypass = QCheckBox(self.tr("Bypass selected effect slots"))
            self.action_bypass.setChecked(bool(settings.bypass))
            self.action_target_mask = self._spin(settings.target_mask or 0, 0xFF)
            self.form.addRow("", self.action_bypass)
            self.form.addRow(self.tr("Effect-slot mask"), self.action_target_mask)
        if settings.kind == "seek":
            self.action_relative = QCheckBox(self.tr("Position is relative to duration"))
            self.action_relative.setChecked(bool(settings.relative_to_duration))
            self.action_snap = QCheckBox(self.tr("Snap to nearest marker"))
            self.action_snap.setChecked(bool(settings.snap_to_marker))
            self.action_value_base = self._decimal(settings.value or 0.0)
            self.action_value_min = self._decimal(settings.minimum or 0.0)
            self.action_value_max = self._decimal(settings.maximum or 0.0)
            self.form.addRow("", self.action_relative)
            self.form.addRow(self.tr("Seek value"), self.action_value_base)
            self.form.addRow(self.tr("Random minimum"), self.action_value_min)
            self.form.addRow(self.tr("Random maximum"), self.action_value_max)
            self.form.addRow("", self.action_snap)
        if settings.kind not in {"play", "state", "switch"}:
            self.action_exceptions = RowsEditor(
                [self.tr("Except object ShortID"), self.tr("Is Bus (0/1)")],
                ((object_id, int(is_bus)) for object_id, is_bus in settings.exceptions),
                ("0", "0"),
            )
            self.form.addRow(self.tr("Exceptions"), self.action_exceptions)

    def _action_settings(self, settings):
        updates = {}
        if self.fade_curve is not None:
            updates["fade_curve"] = int(self.fade_curve.currentData())
        if self.action_bank_id is not None:
            updates["bank_id"] = self.action_bank_id.object_id("Bank ShortID")
        if settings.kind in {"stop", "pause", "resume"}:
            flags = settings.stop_flags or 0
            flags = (flags & ~0x07) | (int(bool(self.action_primary and self.action_primary.isChecked())))
            flags |= int(self.apply_states.isChecked()) << 1
            flags |= int(self.apply_sequences.isChecked()) << 2
            updates["stop_flags"] = flags
        if settings.kind in {"set_value", "game_parameter"}:
            updates.update(
                value_meaning=int(self.action_value_meaning.currentData()),
                value=self._decimal_value(self.action_value_base),
                minimum=self._decimal_value(self.action_value_min),
                maximum=self._decimal_value(self.action_value_max),
            )
        if settings.kind == "game_parameter":
            updates.update(
                parameter_id=self.named_target.object_id(self.tr("Game Parameter")),
                bypass_transition=self.action_bypass.isChecked(),
            )
        if settings.kind == "bypass_fx":
            updates.update(
                bypass=self.action_bypass.isChecked(),
                target_mask=self.action_target_mask.value(),
            )
        if settings.kind == "seek":
            updates.update(
                relative_to_duration=self.action_relative.isChecked(),
                value=self._decimal_value(self.action_value_base),
                minimum=self._decimal_value(self.action_value_min),
                maximum=self._decimal_value(self.action_value_max),
                snap_to_marker=self.action_snap.isChecked(),
            )
        if settings.kind in {"state", "switch"}:
            updates.update(
                group_id=self.action_group.object_id(self.tr("Group")),
                value_id=self.action_value.object_id(self.tr("Value")),
            )
        if self.action_exceptions is not None:
            updates["exceptions"] = tuple(
                (
                    _number(row[0], "Exception ShortID"),
                    bool(_number(row[1], "Exception Is Bus", bits=8)),
                )
                for row in self.action_exceptions.values()
            )
        return replace(settings, **updates)

    def _build(self):
        obj = self.obj
        if obj.type_id == 0x03 and obj.action_type is not None:
            self._build_action(obj)
            return
        if obj.type_id == 0x02 and obj.sources:
            self.kind = "sound"
            source = obj.sources[0]
            self.source_id = self._line(source.source_id)
            self.stream_type = QComboBox()
            for label, value in (
                (self.tr("In this BNK"), 0),
                (self.tr("Prefetch in BNK + full audio in PCK"), 1),
                (self.tr("Streamed from PCK"), 2),
            ):
                self.stream_type.addItem(label, value)
            if self.stream_type.findData(source.stream_type) < 0:
                self.stream_type.addItem(
                    self.tr("Unknown ({value})").format(value=source.stream_type),
                    source.stream_type,
                )
            self.stream_type.setCurrentIndex(self.stream_type.findData(source.stream_type))
            self.memory_size = self._spin(source.in_memory_size, 0x7FFFFFFF)
            self.source_flags = self._spin(source.source_bits, 0xFF)
            self.form.addRow(self.tr("Source ID"), self.source_id)
            self.form.addRow(self.tr("Media storage"), self.stream_type)
            self.form.addRow(self.tr("In-memory bytes"), self.memory_size)
            self.form.addRow(self.tr("Source flags"), self.source_flags)
            self.form.addRow(self.tr("Codec plugin"), QLabel(f"0x{source.plugin_id:08X}"))
            return
        if obj.random_sequence:
            self.kind, data = "random", obj.random_sequence
            self.loop = self._spin(data.loop_count)
            self.loop_min, self.loop_max = self._spin(data.loop_min), self._spin(data.loop_max)
            self.transition = self._time(data.transition_ms)
            self.transition_min, self.transition_max = self._time(data.transition_min_ms), self._time(data.transition_max_ms)
            self.avoid = self._spin(data.avoid_repeat)
            self.transition_mode = self._choice(data.transition_mode, (
                (self.tr("Disabled"), 0), (self.tr("Crossfade (amplitude)"), 1),
                (self.tr("Crossfade (power)"), 2), (self.tr("Delay"), 3),
                (self.tr("Sample accurate"), 4), (self.tr("Trigger rate"), 5),
            ))
            self.random_mode = self._choice(data.random_mode, (
                (self.tr("Normal"), 0), (self.tr("Shuffle"), 1),
            ))
            self.container_mode = self._choice(data.mode, (
                (self.tr("Random"), 0), (self.tr("Sequence"), 1),
            ))
            for label, field in (
                ("Loop count", self.loop), ("Loop random minimum", self.loop_min),
                ("Loop random maximum", self.loop_max), ("Transition", self.transition),
                ("Transition random minimum", self.transition_min),
                ("Transition random maximum", self.transition_max),
                ("Avoid repeats", self.avoid), ("Transition mode", self.transition_mode),
                ("Random mode", self.random_mode), ("Container mode", self.container_mode),
            ):
                self.form.addRow(self.tr(label), field)
            self.random_flags = []
            for label, mask in (
                ("Reset playlist each play", 0x02), ("Restart sequence backwards", 0x04),
                ("Continuous playback", 0x08), ("Use one global playlist", 0x10),
            ):
                flag = QCheckBox(self.tr(label))
                flag.setChecked(bool(data.flags & mask))
                self.random_flags.append((flag, mask))
                self.form.addRow("", flag)
            self.rows = RowsEditor(
                [self.tr("Child ShortID"), self.tr("Weight")],
                ((item.object_id, item.weight) for item in data.playlist),
                ("", "50000"),
            )
            self.form.addRow(self.tr("Playlist (in order)"), self.rows)
            return
        if obj.switch_container:
            self.kind, data = "switch", obj.switch_container
            self.group_type = self._choice(data.group_type, (
                (self.tr("Switch Group"), 0), (self.tr("State Group"), 1),
            ))
            prefix = "switch" if data.group_type == 0 else "state"
            self.group_id = NamedIdEdit(self.metadata, f"{prefix}_group", data.group_id)
            self.default_id = NamedIdEdit(self.metadata, f"{prefix}_value", data.default_value_id)
            self.group_type.currentIndexChanged.connect(
                lambda _index: (
                    self.group_id.set_category(
                        "switch_group" if self.group_type.currentData() == 0 else "state_group"
                    ),
                    self.default_id.set_category(
                        "switch_value" if self.group_type.currentData() == 0 else "state_value"
                    ),
                )
            )
            self.continuous = QCheckBox(self.tr("Continuous validation"))
            self.continuous.setChecked(data.continuous_validation)
            self.children = self._line(", ".join(map(str, obj.child_ids)))
            self.form.addRow(self.tr("Group type"), self.group_type)
            self.form.addRow(self.tr("Group ShortID"), self.group_id)
            self.form.addRow(self.tr("Default Switch/State ShortID"), self.default_id)
            self.form.addRow("", self.continuous)
            self.form.addRow(self.tr("Children"), self.children)
            self.mappings = RowsEditor(
                [self.tr("Switch/State ShortID"), self.tr("Child ShortIDs (comma-separated)")],
                ((
                    (self.metadata.names(f"{prefix}_value", item.value_id) or (str(item.value_id),))[0],
                    ", ".join(map(str, item.object_ids)),
                ) for item in data.mappings),
                ("", ""),
            )
            self.params = RowsEditor(
                [self.tr("Child ShortID"), self.tr("Flags"), self.tr("Mode"), self.tr("Fade out ms"), self.tr("Fade in ms")],
                ((item.object_id, item.flags, item.mode, item.fade_out_ms, item.fade_in_ms) for item in data.params),
                ("", "0", "0", "0", "0"),
            )
            self.form.addRow(self.tr("Value mappings"), self.mappings)
            self.form.addRow(self.tr("Transitions"), self.params)
            return
        if obj.music_segment:
            self.kind, data = "segment", obj.music_segment
            self.duration = self._time(data.duration_ms)
            self.rows = RowsEditor(
                [self.tr("Cue ShortID"), self.tr("Position ms"), self.tr("Name")],
                ((item.marker_id, item.position_ms, item.name) for item in data.markers),
                ("0", "0", ""),
            )
            self.form.addRow(self.tr("Duration"), self.duration)
            self.form.addRow(self.tr("Music cues"), self.rows)
            return
        if obj.music_track:
            self.kind, data = "track", obj.music_track
            self.source_rows = RowsEditor(
                [self.tr("Plugin"), self.tr("Stream type"), self.tr("Source ID"), self.tr("In-memory bytes"), self.tr("Flags")],
                ((f"0x{item.plugin_id:08X}", item.stream_type, item.source_id, item.in_memory_size, item.source_bits) for item in obj.sources),
                fixed=True,
            )
            self.subtracks = self._spin(data.subtrack_count, 0x7FFFFFFF)
            self.rows = RowsEditor(
                [self.tr("Track"), self.tr("Source ID"), self.tr("Event ID"), self.tr("Play at ms"), self.tr("Begin trim ms"), self.tr("End trim ms"), self.tr("Source duration ms")],
                ((item.track_id, item.source_id, item.event_id, item.play_at_ms, item.begin_trim_ms, item.end_trim_ms, item.source_duration_ms) for item in data.clips),
                ("0", "", "0", "0", "0", "0", "0"),
            )
            self.form.addRow(self.tr("Sources"), self.source_rows)
            self.form.addRow(self.tr("Subtrack count"), self.subtracks)
            self.form.addRow(self.tr("Clips"), self.rows)
            return
        if obj.fx_plugin:
            self.kind, data = "fx", obj.fx_plugin
            self.resize(900, 720)
            self.form.addRow(
                self.tr("Wwise plug-in"), QLabel(f"{data.name} · 0x{data.plugin_id:08X}")
            )
            self.fx_editor = FxParametersEditor(data) if data.parameters else None
            if self.fx_editor is not None:
                self.form.addRow(self.tr("Plug-in settings"), self.fx_editor)
            else:
                self.form.addRow(
                    self.tr("Plug-in settings"),
                    QLabel(self.tr("Edit the structured settings and curves below.")),
                )
            return
        if obj.silence_source:
            self.kind, data = "silence", obj.silence_source
            self.silence_duration = self._decimal(data.duration_seconds, " s")
            self.silence_minus = self._decimal(data.random_minus_seconds, " s")
            self.silence_plus = self._decimal(data.random_plus_seconds, " s")
            self.form.addRow(self.tr("Base silence duration"), self.silence_duration)
            self.form.addRow(self.tr("Random minimum offset"), self.silence_minus)
            self.form.addRow(self.tr("Random maximum offset"), self.silence_plus)
            return
        if obj.attenuation:
            self.kind, data = "attenuation", obj.attenuation
            self.resize(900, 820)
            self.cone_enabled = QCheckBox(self.tr("Enable directional cone"))
            self.cone_enabled.setChecked(data.cone is not None)
            self.form.addRow("", self.cone_enabled)
            values = data.cone or (0.0,) * 5
            self.cone_fields = tuple(
                self._decimal(value, suffix)
                for value, suffix in zip(values, ("°", "°", " dB", " %", " %"))
            )
            for label, field in zip(
                ("Inside angle", "Outside angle", "Outside volume", "Outside LPF", "Outside HPF"),
                self.cone_fields,
            ):
                field.setEnabled(data.cone is not None)
                self.cone_enabled.toggled.connect(field.setEnabled)
                self.form.addRow(self.tr(label), field)
            self.attenuation_editor = AttenuationEditor(data, obj.bank_version)
            self.form.addRow(self.tr("Distance attenuation"), self.attenuation_editor)
            return
        if can_edit_hirc_children(obj):
            self.kind = "children"
            self.children = self._line(", ".join(map(str, obj.child_ids)))
            self.form.addRow(self.tr("Children (ordered ShortIDs)"), self.children)
            return
        if obj.structure and obj.structure.complete:
            self.kind = "structure"
            return
        self.kind = "read_only"
        self.read_only = QLabel(self.tr(
            "This object layout is not decoded. Its payload will be preserved unchanged."
        ))
        self.read_only.setWordWrap(True)
        self.form.addRow(self.tr("Read-only"), self.read_only)

    def payload(self):
        obj = self.obj
        if self.structure_editor is not None:
            obj = replace(
                obj,
                payload=set_v132_fields(
                    obj.payload, obj.structure, self.structure_editor.changes()
                ),
            )
        if self.kind == "action":
            if self.target is not None:
                target_id = self.target.object_id()
                target_is_bus = self.target_bus.isChecked()
            elif self.named_target is not None:
                target_id = self.named_target.object_id(self.tr("Action target"))
                target_is_bus = obj.action_target_is_bus
            elif obj.action_settings and obj.action_settings.kind in {"state", "switch"}:
                target_id = self.action_value.object_id(self.tr("Value"))
                target_is_bus = obj.action_target_is_bus
            else:
                target_id = _number(self.raw_target.text(), "External ID")
                target_is_bus = obj.action_target_is_bus
            payload = self._with_properties(set_action_fields(
                obj,
                _number(self.action_type.text(), "Action type", bits=16),
                target_id,
                target_is_bus,
            ))
            if obj.action_settings:
                delta = len(payload) - len(obj.payload)
                shifted_settings = replace(
                    obj.action_settings,
                    offset=obj.action_settings.offset + delta,
                    end=(
                        obj.action_settings.end + delta
                        if obj.action_settings.end is not None else None
                    ),
                )
                payload = set_action_specific(
                    replace(obj, payload=payload, action_settings=shifted_settings),
                    self._action_settings(shifted_settings),
                )
            return payload
        if self.kind == "sound":
            source = obj.sources[0]
            return self._with_properties(set_bank_sources(
                obj,
                (
                    replace(
                        source,
                        source_id=_number(self.source_id.text(), "Source ID", allow_zero=False),
                        stream_type=int(self.stream_type.currentData()),
                        in_memory_size=self.memory_size.value(),
                        source_bits=self.source_flags.value(),
                    ),
                ),
            ))
        if self.kind == "random":
            playlist = tuple(
                BnkPlaylistItem(_number(row[0], "Child ShortID", allow_zero=False), int(row[1]))
                for row in self.rows.values()
            )
            flags = obj.random_sequence.flags & ~0x1E
            flags |= sum(mask for checkbox, mask in self.random_flags if checkbox.isChecked())
            data = replace(
                obj.random_sequence,
                loop_count=self.loop.value(), loop_min=self.loop_min.value(),
                loop_max=self.loop_max.value(), transition_ms=self._time_value(self.transition),
                transition_min_ms=self._time_value(self.transition_min), transition_max_ms=self._time_value(self.transition_max),
                avoid_repeat=self.avoid.value(), transition_mode=int(self.transition_mode.currentData()),
                random_mode=int(self.random_mode.currentData()), mode=int(self.container_mode.currentData()),
                flags=flags,
            )
            playlist_ids = tuple(item.object_id for item in playlist)
            original_playlist_ids = {
                item.object_id for item in obj.random_sequence.playlist
            }
            children = tuple(value for value in obj.child_ids if value in playlist_ids) + tuple(
                value for value in dict.fromkeys(playlist_ids)
                if value not in obj.child_ids and value not in original_playlist_ids
            )
            return self._with_properties(set_random_sequence(obj, data, children, playlist))
        if self.kind == "switch":
            children = _id_list(self.children.text())
            prefix = "switch" if self.group_type.currentData() == 0 else "state"
            mappings = tuple(
                BnkSwitchMapping(
                    self.metadata.resolve_id(f"{prefix}_value", row[0]),
                    _id_list(row[1]),
                )
                for row in self.mappings.values()
            )
            params = tuple(
                BnkSwitchParam(
                    _number(row[0], "Child ShortID", allow_zero=False),
                    _number(row[1], "Flags", bits=8),
                    _number(row[2], "Mode", bits=8), int(row[3]), int(row[4]),
                )
                for row in self.params.values()
            )
            data = replace(
                obj.switch_container,
                group_type=int(self.group_type.currentData()),
                group_id=self.group_id.object_id(self.tr("Group")),
                default_value_id=self.default_id.object_id(self.tr("Default value")),
                continuous_validation=self.continuous.isChecked(),
            )
            return self._with_properties(set_switch_container(obj, data, children, mappings, params))
        if self.kind == "segment":
            markers = tuple(
                BnkMusicMarker(
                    _number(row[0], "Cue ShortID"), float(row[1]), row[2]
                )
                for row in self.rows.values()
            )
            return self._with_properties(set_music_segment(obj, self._time_value(self.duration), markers))
        if self.kind == "track":
            sources = tuple(
                replace(
                    source,
                    plugin_id=_number(row[0], "Plugin ID"),
                    stream_type=_number(row[1], "Stream type", bits=8),
                    source_id=_number(row[2], "Source ID", allow_zero=False),
                    in_memory_size=_number(row[3], "In-memory bytes"),
                    source_bits=_number(row[4], "Source flags", bits=8),
                )
                for source, row in zip(obj.sources, self.source_rows.values())
            )
            clips = tuple(
                BnkMusicClip(
                    _number(row[0], "Track index"),
                    _number(row[1], "Source ID", allow_zero=False),
                    *map(float, row[3:]),
                    _number(row[2], "Event ID"),
                )
                for row in self.rows.values()
            )
            with_properties = self._with_properties(obj.payload)
            source_payload = set_bank_sources(replace(obj, payload=with_properties), sources)
            return set_music_track_clips(
                replace(obj, payload=source_payload), clips, self.subtracks.value()
            )
        if self.kind == "attenuation":
            assignments, curves = self.attenuation_editor.values()
            cone = (
                tuple(self._decimal_value(field) for field in self.cone_fields)
                if self.cone_enabled.isChecked() else None
            )
            return set_attenuation(
                obj, replace(obj.attenuation, cone=cone, assignments=assignments, curves=curves)
            )
        if self.kind == "silence":
            return set_silence_source(
                obj,
                replace(
                    obj.silence_source,
                    duration_seconds=self._decimal_value(self.silence_duration),
                    random_minus_seconds=self._decimal_value(self.silence_minus),
                    random_plus_seconds=self._decimal_value(self.silence_plus),
                ),
            )
        if self.kind == "fx":
            return set_fx_parameters(
                obj, self.fx_editor.parameters() if self.fx_editor is not None else ()
            )
        if self.kind == "children":
            return self._with_properties(set_hirc_children(obj, _id_list(self.children.text())))
        if self.kind == "properties":
            return self._with_properties(obj.payload)
        if self.kind == "structure":
            return self._with_properties(obj.payload)
        return obj.payload

    def _with_properties(self, payload):
        if self.property_editor is None:
            return payload
        return set_property_bundle(
            replace(self.obj, payload=payload),
            self.property_editor.values.entries(),
            self.property_editor.ranges.entries() if self.property_editor.ranges else (),
        )

    def _covered_structure_fields(self):
        common_properties = (
            "Node/Properties", "Node/Randomizers", "Properties", "Randomizers",
        ) if self.property_editor is not None else ()
        focused = {
            "action": ("Action type", "Target", "Target flags", "Properties", "Randomizers", "Play fade", "Fade", "Scope", "Exceptions", "Value", "State", "Switch"),
            "sound": tuple(f"Source/{name}" for name in _SOURCE_HEADER_FIELDS),
            "random": ("Playlist behavior", "Children", "Playlist"),
            "switch": ("Group type", "Group", "Default value", "Continuous validation", "Children", "Value assignments", "Switch transitions"),
            "segment": ("Duration (ms)", "Cues"),
            "track": ("Source count", "Clips"),
            "fx": (
                ("Plug-in parameters",)
                if self.obj.fx_plugin and self.obj.fx_plugin.parameters else ()
            ),
            "silence": ("Plug-in parameters",),
            "attenuation": ("Directional cone enabled", "Directional cone", "Curve assignments", "Attenuation curves"),
            "children": ("Children",),
        }.get(self.kind, ())
        if self.kind == "track" and self.obj.structure:
            source_roots = dict.fromkeys(
                field.path.split("/", 1)[0]
                for field in self.obj.structure.fields
                if field.path.startswith("Source ")
            )
            focused = (*focused, *(
                f"{root}/{name}"
                for root in source_roots for name in _SOURCE_HEADER_FIELDS
            ))
        return (*common_properties, *focused)

    def accept(self):
        try:
            self._payload = self.payload()
        except (ValueError, OverflowError) as exc:
            QMessageBox.warning(self, self.tr("Invalid Properties"), str(exc))
            return
        super().accept()

    def edited_payload(self):
        return self._payload
