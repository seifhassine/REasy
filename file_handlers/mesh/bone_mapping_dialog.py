from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
lazy from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QStyledItemDelegate, QTableView, QVBoxLayout,
)

lazy from .blender.mapping import ACTIONS, ADD, DROP, EXACT, LOADED, MANUAL, MERGE, NAME, POSITION, ROLE

SOURCE, VERTICES, TARGET, UNMAPPED, FOUND, OFFSET = range(6)
UNMAPPED_TEXT = "—"


def offset_limit(mapping):
    """In metres."""
    return max(0.02, 0.02 * mapping.skeleton_size())


class BoneMappingModel(QAbstractTableModel):
    def __init__(self, mapping, parent=None):
        super().__init__(parent)
        self.mapping = mapping
        self.limit = offset_limit(mapping)
        self.depth = {row.source: sum(1 for _ in mapping.ancestors(row.source)) for row in mapping.rows}

    def headers(self):
        return [self.tr("Source bone or group"), self.tr("Vertices"), self.tr("Reference joint"),
                self.tr("If unmapped"), self.tr("Found by"), self.tr("Rest offset")]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.mapping.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 6

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.headers()[section]
        return None

    def action_text(self, row, action=None):
        action = action or row.action
        if action == ADD:
            return self.tr("Add as a new joint")
        if action == DROP:
            return self.tr("Drop its weights")
        target = self.mapping.merge_target(row.source)
        return self.tr("Merge into {joint}").format(joint=target) if target else self.tr("Merge")

    def origin_text(self, origin):
        return {EXACT: self.tr("Same name"), ROLE: self.tr("Body part"), NAME: self.tr("Similar name"),
                POSITION: self.tr("Position"),
                MANUAL: self.tr("You"), LOADED: self.tr("Saved mapping")}.get(origin, "")

    def needs_review(self, row):
        offset = self.mapping.rest_offset(row)
        return (row.origin == POSITION or (not row.mapped and row.weighted > 0)
                or (offset is not None and offset > self.limit))

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.mapping.rows[index.row()]
        column = index.column()
        offset = self.mapping.rest_offset(row)
        if role == Qt.DisplayRole:
            if column == SOURCE:
                return "    " * self.depth[row.source] + row.source
            if column == VERTICES:
                return f"{row.weighted:,}" if row.weighted else ""
            if column == TARGET:
                return row.target or UNMAPPED_TEXT
            if column == UNMAPPED:
                return "" if row.mapped else self.action_text(row)
            if column == FOUND:
                return self.origin_text(row.origin)
            if column == OFFSET:
                return "" if offset is None else f"{offset * 100:.1f} cm"
        if role == Qt.EditRole:
            if column == TARGET:
                return row.target
            if column == UNMAPPED:
                return row.action
        if role == Qt.ForegroundRole:
            if column == OFFSET and offset is not None and offset > self.limit:
                return QBrush(QColor("#d9534f"))
            if column == TARGET and not row.mapped and row.weighted:
                return QBrush(QColor("#e0a030"))
        if role == Qt.ToolTipRole and column == OFFSET and offset is not None and offset > self.limit:
            return self.tr("The model's bone sits far from this joint: the reference may be the wrong one, "
                           "or the model is not fitted to its skeleton.")
        if role == Qt.UserRole:
            return self.needs_review(row)
        return None

    def flags(self, index):
        flags = super().flags(index)
        row = self.mapping.rows[index.row()]
        if index.column() == TARGET or (index.column() == UNMAPPED and not row.mapped):
            flags |= Qt.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        row = self.mapping.rows[index.row()]
        if index.column() == TARGET:
            value = "" if value in ("", UNMAPPED_TEXT) else value
            if value and value not in self.mapping.target:
                return False
            self.mapping.set_target(row.source, value, MANUAL)
        elif index.column() == UNMAPPED and value in ACTIONS:
            row.action = value
        else:
            return False
        # Whole table: descendants' merge targets follow their ancestors' mapping.
        self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, self.columnCount() - 1))
        return True

    def reset(self):
        self.beginResetModel()
        self.endResetModel()


class _ReviewFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.only_review = False
        self.text = ""

    def set_filter(self, only_review, text):
        self.only_review, self.text = only_review, text.strip().casefold()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        row = model.mapping.rows[source_row]
        if self.only_review and not model.data(model.index(source_row, 0), Qt.UserRole):
            return False
        return not self.text or self.text in row.source.casefold() or self.text in row.target.casefold()


class _Delegate(QStyledItemDelegate):
    def __init__(self, mapping, parent=None):
        super().__init__(parent)
        self.mapping = mapping

    def createEditor(self, parent, option, index):
        editor = QComboBox(parent)
        if index.column() == TARGET:
            editor.setEditable(True)
            editor.setInsertPolicy(QComboBox.NoInsert)
            for name in ("", *self.mapping.target):
                editor.addItem(name or UNMAPPED_TEXT, name)
            completer = QCompleter([UNMAPPED_TEXT, *self.mapping.target], editor)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchContains)
            editor.setCompleter(completer)
        else:
            row = self.mapping.rows[index.model().mapToSource(index).row()]
            for action in ACTIONS:
                editor.addItem(index.model().sourceModel().action_text(row, action), action)
        return editor

    def setEditorData(self, editor, index):
        editor.setCurrentIndex(max(editor.findData(index.data(Qt.EditRole) or ""), 0))

    def setModelData(self, editor, model, index):
        position = editor.findText(editor.currentText().strip(), Qt.MatchFixedString)
        if position >= 0:
            model.setData(index, editor.itemData(position), Qt.EditRole)


class BoneMappingDialog(QDialog):
    def __init__(self, mapping, reference_label="", parent=None):
        super().__init__(parent)
        self.mapping = mapping
        self.setWindowTitle(self.tr("Map the model's bones"))
        self.resize(980, 640)
        layout = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "Each bone or vertex group of the model sends its weights to a joint of {reference}. "
            "Rows needing a look: guesses by position, weighted bones without a joint, and bones far from "
            "their joint's rest position.").format(reference=reference_label or self.tr("the reference")))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("Filter by bone or joint name"))
        self.review_only = QCheckBox(self.tr("Only rows to review"))
        controls.addWidget(self.search, 1)
        controls.addWidget(self.review_only)
        layout.addLayout(controls)

        self.model = BoneMappingModel(mapping, self)
        self.proxy = _ReviewFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setItemDelegate(_Delegate(mapping, self.table))
        self.table.setEditTriggers(QAbstractItemView.AllEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(SOURCE, QHeaderView.Stretch)
        for column in (VERTICES, FOUND, OFFSET):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.resizeSection(TARGET, 220)
        header.resizeSection(UNMAPPED, 230)
        layout.addWidget(self.table, 1)

        files = QHBoxLayout()
        load_button = QPushButton(self.tr("Load mapping…"))
        save_button = QPushButton(self.tr("Save mapping…"))
        files.addWidget(load_button)
        files.addWidget(save_button)
        files.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(self.tr("Import with this mapping"))
        files.addWidget(buttons)
        layout.addLayout(files)

        self.search.textChanged.connect(self._filter)
        self.review_only.toggled.connect(self._filter)
        self.model.dataChanged.connect(self._summarize)
        self.model.modelReset.connect(self._summarize)
        load_button.clicked.connect(self._load)
        save_button.clicked.connect(self._save)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._summarize()

    def _filter(self):
        self.proxy.set_filter(self.review_only.isChecked(), self.search.text())

    def _summarize(self):
        rows = self.mapping.rows
        mapped = sum(row.mapped for row in rows)
        guessed = sum(row.origin == POSITION for row in rows)
        weighted = [row for row in rows if not row.mapped and row.weighted]
        merged = sum(row.action == MERGE for row in weighted)
        added = sum(row.action == ADD for row in weighted)
        dropped = sum(row.action == DROP for row in weighted)
        far = sum(1 for row in rows if (self.mapping.rest_offset(row) or 0) > self.model.limit)
        parts = [self.tr("{mapped} of {total} mapped").format(mapped=mapped, total=len(rows))]
        if self.mapping.families:
            parts.append(self.tr("body parts: {source} onto {target}").format(source=self.mapping.families[0],
                                                                           target=self.mapping.families[1]))
        if guessed:
            parts.append(self.tr("{count} by position").format(count=guessed))
        if weighted:
            parts.append(self.tr("{count} weighted unmapped ({merged} merged, {added} added, {dropped} dropped)")
                         .format(count=len(weighted), merged=merged, added=added, dropped=dropped))
        if far:
            parts.append(self.tr("{count} far from their joint").format(count=far))
        self.summary.setText(" · ".join(parts))

    def _load(self):
        path, _filter = QFileDialog.getOpenFileName(self, self.tr("Load bone mapping"), "",
                                                    self.tr("Bone mappings (*.json)"))
        if not path:
            return
        try:
            applied = self.mapping.apply_json(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Bone mapping"), str(exc))
            return
        self.model.reset()
        QMessageBox.information(self, self.tr("Bone mapping"),
                                self.tr("Applied the saved mapping to {count} rows.").format(count=applied))

    def _save(self):
        path, _filter = QFileDialog.getSaveFileName(self, self.tr("Save bone mapping"), "bone_mapping.json",
                                                    self.tr("Bone mappings (*.json)"))
        if not path:
            return
        try:
            Path(path).write_text(self.mapping.to_json(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, self.tr("Bone mapping"), str(exc))


__all__ = ["BoneMappingDialog", "BoneMappingModel", "offset_limit"]
