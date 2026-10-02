"""Frame-range selection for the current native animation slot."""
from pathlib import Path
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QComboBox, QDoubleSpinBox, QPushButton, QDialogButtonBox,
                               QHeaderView, QMessageBox, QApplication, QSpinBox, QCheckBox, QLabel)
from PySide6.QtCore import Qt

from ..mhr_bake import MotionSegment


class MotionBakeDialog(QDialog):
    def __init__(self, document, motion_id, commit, parent=None, *, sources=None):
        super().__init__(parent)
        self.document, self.motion_id, self.commit = document, motion_id, commit
        self.sources = sources
        self.column_offset = int(sources is not None)
        self.setWindowTitle(self.tr('Bake segments into Motion {0}').format(motion_id))
        self.resize(960, 300)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 6+self.column_offset, self)
        self.table.setHorizontalHeaderLabels(([self.tr('LMT file')] if self.sources is not None else []) + [self.tr('Animation'), self.tr('Start frame'),
                                             self.tr('End frame'), self.tr('Speed ×'), self.tr('Root transform'),
                                             self.tr('Root travel ×')])
        self.table.horizontalHeader().setSectionResizeMode(self.column_offset, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        add = QPushButton(self.tr('Add segment'), self)
        remove = QPushButton(self.tr('Remove segment'), self)
        add.clicked.connect(lambda: self.add_segment())
        remove.clicked.connect(self.remove_segment)
        actions.addWidget(add)
        actions.addWidget(remove)
        up = QPushButton(self.tr('Move up'), self)
        down = QPushButton(self.tr('Move down'), self)
        up.clicked.connect(lambda: self.move_segment(-1))
        down.clicked.connect(lambda: self.move_segment(1))
        actions.addWidget(up)
        actions.addWidget(down)
        actions.addStretch()
        layout.addLayout(actions)
        options = QHBoxLayout()
        self.align_waist = QCheckBox(self.tr('Align waist rotation'), self)
        self.align_waist.setChecked(True)
        self.blend_frames = QSpinBox(self)
        self.blend_frames.setRange(0, 1000)
        self.blend_frames.setValue(3)
        options.addWidget(self.align_waist)
        options.addWidget(QLabel(self.tr('Blend frames per side'), self))
        options.addWidget(self.blend_frames)
        options.addStretch()
        layout.addLayout(options)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.bake)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.add_segment()
        self.add_segment()

    def add_segment(self):
        row = self.table.rowCount()
        self.table.insertRow(row)
        source = QComboBox(self.table)
        file_choice = None
        if self.sources is not None:
            file_choice = QComboBox(self.table)
            for path in self.sources:
                file_choice.addItem(Path(path).name, path)
                file_choice.setItemData(file_choice.count()-1, path, Qt.ItemDataRole.ToolTipRole)
        start, end, speed = (QDoubleSpinBox(self.table) for _ in range(3))
        for field in (start, end):
            field.setDecimals(3)
        speed.setDecimals(4)
        speed.setRange(0.0001, 10000)
        speed.setValue(1)
        root = QComboBox(self.table)
        for label, value in ((self.tr('Relative'), 'relative'), (self.tr('Identity'), 'identity'),
                             (self.tr('Authored'), 'authored')):
            root.addItem(label, value)
        travel = QDoubleSpinBox(self.table)
        travel.setDecimals(4)
        travel.setRange(0, 10000)
        travel.setValue(1)

        def update_range():
            document = self.sources[file_choice.currentData()] if file_choice is not None else self.document
            motion = next(s.payload.value for s in document.slots if s.motion_id == source.currentData())
            start.setRange(0, motion.end_frame)
            end.setRange(0, motion.end_frame)
            start.setValue(0)
            end.setValue(motion.end_frame)

        def update_source():
            document = self.sources[file_choice.currentData()] if file_choice is not None else self.document
            source.blockSignals(True)
            source.clear()
            for slot in document.slots:
                if slot.payload is not None:
                    source.addItem(f'{slot.motion_id}: {slot.payload.value.name}', slot.motion_id)
            selected = source.findData(self.motion_id)
            source.setCurrentIndex(selected if selected >= 0 else 0)
            source.blockSignals(False)
            update_range()

        source.currentIndexChanged.connect(update_range)
        if file_choice is not None:
            file_choice.currentIndexChanged.connect(update_source)
        update_source()
        widgets = ((file_choice,) if file_choice is not None else ()) + (source, start, end, speed, root, travel)
        for column, widget in enumerate(widgets):
            self.table.setCellWidget(row, column, widget)
        self.table.setCurrentCell(row, 0)

    def remove_segment(self):
        row = self.table.currentRow()
        if row >= 0 and self.table.rowCount() > 1:
            self.table.removeRow(row)

    def segments(self):
        offset = self.column_offset
        return [MotionSegment(self.table.cellWidget(row, offset).currentData(),
                              *(self.table.cellWidget(row, col+offset).value() for col in (1, 2, 3)),
                              self.table.cellWidget(row, 4+offset).currentData(), self.table.cellWidget(row, 5+offset).value(),
                              self.table.cellWidget(row, 0).currentData() if offset else None)
                for row in range(self.table.rowCount())]

    def move_segment(self, direction):
        row = self.table.currentRow()
        target = row+direction
        if row < 0 or not 0 <= target < self.table.rowCount():
            return
        segments = self.segments()
        segments[row], segments[target] = segments[target], segments[row]
        self.table.setRowCount(0)
        offset = self.column_offset
        for row, segment in enumerate(segments):
            self.add_segment()
            if offset:
                combo = self.table.cellWidget(row, 0)
                combo.setCurrentIndex(combo.findData(segment.source))
            combo = self.table.cellWidget(row, offset)
            combo.setCurrentIndex(combo.findData(segment.motion_id))
            for column, value in ((1, segment.start), (2, segment.end), (3, segment.speed), (5, segment.root_translation_scale)):
                self.table.cellWidget(row, column+offset).setValue(value)
            combo = self.table.cellWidget(row, 4+offset)
            combo.setCurrentIndex(combo.findData(segment.root_transform))
        self.table.setCurrentCell(target, 0)

    def bake(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.commit(self.segments(), align_waist=self.align_waist.isChecked(), blend_frames=self.blend_frames.value())
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, self.tr('Bake segments'), str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.accept()
