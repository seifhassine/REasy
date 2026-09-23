"""Frame-range selection for the current native animation slot."""
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QComboBox, QDoubleSpinBox, QPushButton, QDialogButtonBox,
                               QHeaderView, QMessageBox, QApplication, QSpinBox, QCheckBox, QLabel)
from PySide6.QtCore import Qt

from ..mhr_bake import MotionSegment


class MotionBakeDialog(QDialog):
    def __init__(self, document, motion_id, commit, parent=None):
        super().__init__(parent)
        self.document, self.motion_id, self.commit = document, motion_id, commit
        self.setWindowTitle(self.tr('Bake segments into Motion {0}').format(motion_id))
        self.resize(960, 300)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels([self.tr('Animation'), self.tr('Start frame'),
                                             self.tr('End frame'), self.tr('Speed ×'), self.tr('Root transform'),
                                             self.tr('Root travel ×')])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        add = QPushButton(self.tr('Add segment'), self)
        remove = QPushButton(self.tr('Remove segment'), self)
        add.clicked.connect(lambda: self.add_segment())
        remove.clicked.connect(self.remove_segment)
        actions.addWidget(add)
        actions.addWidget(remove)
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
        for slot in self.document.slots:
            if slot.payload is not None:
                source.addItem(f'{slot.motion_id}: {slot.payload.value.name}', slot.motion_id)
        source.setCurrentIndex(source.findData(self.motion_id))
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
            motion = next(s.payload.value for s in self.document.slots if s.motion_id == source.currentData())
            start.setRange(0, motion.end_frame)
            end.setRange(0, motion.end_frame)
            start.setValue(0)
            end.setValue(motion.end_frame)

        source.currentIndexChanged.connect(update_range)
        update_range()
        for column, widget in enumerate((source, start, end, speed, root, travel)):
            self.table.setCellWidget(row, column, widget)
        self.table.setCurrentCell(row, 0)

    def remove_segment(self):
        row = self.table.currentRow()
        if row >= 0 and self.table.rowCount() > 1:
            self.table.removeRow(row)

    def segments(self):
        return [MotionSegment(self.table.cellWidget(row, 0).currentData(),
                              *(self.table.cellWidget(row, col).value() for col in (1, 2, 3)),
                              self.table.cellWidget(row, 4).currentData(), self.table.cellWidget(row, 5).value())
                for row in range(self.table.rowCount())]

    def bake(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.commit(self.segments(), align_waist=self.align_waist.isChecked(), blend_frames=self.blend_frames.value())
        except ValueError as exc:
            QMessageBox.warning(self, self.tr('Bake segments'), str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.accept()
