"""Choose LMT-only source segments and a destination slot in an open Rise list."""
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QComboBox, QSpinBox, QMessageBox
from PySide6.QtCore import Qt

from ..lmt_codec import LMT_MOTION_FORMAT_CODEC
from ..mhr_editing import next_motion_id
from .mhr_bake_dialog import MotionBakeDialog


def load_lmt_source(path):
    path = Path(path).resolve()
    document = LMT_MOTION_FORMAT_CODEC.parse(path.read_bytes(), label=str(path))
    if not document.slots:
        raise ValueError(f'{path.name}: no animations to bake')
    return str(path), document


class LmtBakeDialog(MotionBakeDialog):
    def __init__(self, target, selected_id, source_path, source, commit, parent=None):
        self.target_document = target
        self.selected_id = selected_id
        self.commit_lmt = commit
        super().__init__(source, source.slots[0].motion_id, self._commit, parent,
                         sources={source_path: source})
        self.setWindowTitle(self.tr('Bake LMT segments'))
        self.resize(1200, 380)
        self.table.setColumnWidth(0, 230)
        controls = QHBoxLayout()
        add_source = QPushButton(self.tr('Add LMT files…'), self)
        add_source.clicked.connect(self._choose_sources)
        controls.addWidget(add_source)
        controls.addStretch()
        self.mode = QComboBox(self)
        self.mode.addItem(self.tr('Add animation'), False)
        self.mode.addItem(self.tr('Replace animation'), True)
        self.target_id = QSpinBox(self)
        self.target_id.setRange(0, 65535)
        self.target_id.setValue(next_motion_id(target))
        self.mode.currentIndexChanged.connect(self._mode_changed)
        controls.addWidget(self.mode)
        controls.addWidget(QLabel(self.tr('Target MotionID'), self))
        controls.addWidget(self.target_id)
        self.layout().insertLayout(0, controls)

    def _mode_changed(self):
        self.target_id.setValue(self.selected_id if self.mode.currentData() else next_motion_id(self.target_document))

    def _choose_sources(self):
        paths, _ = QFileDialog.getOpenFileNames(self, self.tr('LMT animation sources'), '', 'LMT (*.lmt)')
        if paths:
            try:
                self.add_sources(paths)
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, self.tr('LMT animation sources'), str(exc))

    def add_sources(self, paths):
        loaded = dict(load_lmt_source(path) for path in paths)
        for path, document in loaded.items():
            if path in self.sources:
                continue
            self.sources[path] = document
            for row in range(self.table.rowCount()):
                combo = self.table.cellWidget(row, 0)
                combo.addItem(Path(path).name, path)
                combo.setItemData(combo.count()-1, path, Qt.ItemDataRole.ToolTipRole)

    def _commit(self, segments, **options):
        return self.commit_lmt(self.sources, segments, self.target_id.value(),
                               replace_existing=self.mode.currentData(), **options)
