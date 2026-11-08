from PySide6.QtCore import QT_TRANSLATE_NOOP, QAbstractTableModel, QCoreApplication, QModelIndex, Qt, Signal

lazy from ui.icons import line_icon
lazy from ui.widgets_utils import get_color_preview_brush
lazy from utils.number_format import format_display_value
lazy from .material_graph import is_color_parameter, unit_value

_COLUMNS = (QT_TRANSLATE_NOOP("MdfViewer", "Name"), QT_TRANSLATE_NOOP("MdfViewer", "Components"),
            QT_TRANSLATE_NOOP("MdfViewer", "Locked"), "X", "Y", "Z", "W", QT_TRANSLATE_NOOP("MdfViewer", "Color"))
_LOCKED_TIP = QT_TRANSLATE_NOOP(
    "MdfViewer", "Checked: the game can't change this parameter at runtime; setFloat() is skipped and the file's value stays.")
_SELECTABLE = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled


class MdfParameterModel(QAbstractTableModel):
    edited = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.material = None
        self._lock_icon = line_icon("lock")

    def set_material(self, material) -> None:
        self.beginResetModel()
        self.material = material
        self.endResetModel()

    def rows_changed(self, first: int = 0, last: int | None = None) -> None:
        last = self.rowCount() - 1 if last is None else last
        if last >= first:
            self.dataChanged.emit(self.index(first, 0), self.index(last, len(_COLUMNS) - 1))

    def insert(self, row: int, parameter) -> None:
        self.beginInsertRows(QModelIndex(), row, row)
        self.material.parameters.insert(row, parameter)
        self.endInsertRows()

    def rowCount(self, parent=QModelIndex()):
        return 0 if self.material is None or parent.isValid() else len(self.material.parameters)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return QCoreApplication.translate("MdfViewer", _COLUMNS[section])
            if section == 2 and role == Qt.ItemDataRole.DecorationRole:
                return self._lock_icon
            if section == 2 and role == Qt.ItemDataRole.ToolTipRole:
                return QCoreApplication.translate("MdfViewer", _LOCKED_TIP)
        return super().headerData(section, orientation, role)

    def flags(self, index):
        parameter, column = self.material.parameters[index.row()], index.column()
        if column == 2:
            return _SELECTABLE | Qt.ItemFlag.ItemIsUserCheckable
        if column == 7:
            return _SELECTABLE if is_color_parameter(parameter) else Qt.ItemFlag.ItemIsEnabled
        if column > 2 and column - 3 >= parameter.component_count:
            return Qt.ItemFlag.ItemIsEnabled
        return _SELECTABLE | Qt.ItemFlag.ItemIsEditable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        parameter, column = self.material.parameters[index.row()], index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if column == 0:
                return parameter.name
            if column == 1:
                return str(parameter.component_count)
            if column == 2:
                return None
            component = column - 3
            return format_display_value(parameter.parameter[component]) if column < 7 and component < parameter.component_count else ""
        if role == Qt.ItemDataRole.CheckStateRole and column == 2:
            return Qt.CheckState.Checked if parameter.component_locked else Qt.CheckState.Unchecked
        if column == 7 and role in (Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ToolTipRole) and is_color_parameter(parameter):
            values = parameter.parameter
            red, green, blue = (int(unit_value(value) * 255) for value in values[:3])
            alpha = int(unit_value(values[3] if parameter.component_count == 4 else 1.0) * 255)
            if role == Qt.ItemDataRole.BackgroundRole:
                return get_color_preview_brush(red, green, blue, alpha)
            return f"RGBA: {red},{green},{blue},{alpha}" if parameter.component_count == 4 else f"RGB: {red},{green},{blue}"
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        row, column = index.row(), index.column()
        parameter = self.material.parameters[row]
        if column == 2 and role == Qt.ItemDataRole.CheckStateRole:
            locked = int(Qt.CheckState(value) == Qt.CheckState.Checked)
            if locked == parameter.component_locked:
                return True
            parameter.component_locked = locked
        elif role == Qt.ItemDataRole.EditRole and column not in (2, 7):
            text = str(value)
            if text == self.data(index):
                return True
            try:
                if column == 0:
                    parameter.name = text
                elif column == 1:
                    count = max(1, min(4, int(text)))
                    parameter.component_count = count
                    parameter.parameter = tuple(v if i < count else 0.0 for i, v in enumerate(parameter.parameter))
                elif column - 3 < parameter.component_count:
                    parameter.parameter = tuple(float(text) if i == column - 3 else v
                                                for i, v in enumerate(parameter.parameter))
                else:
                    return False
            except ValueError:
                return False
        else:
            return False
        self.rows_changed(row, row)
        self.edited.emit(row)
        return True
