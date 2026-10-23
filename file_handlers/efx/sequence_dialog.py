"""Interactive flipbook input, frame order, preview and bundle export."""
from pathlib import Path

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton,
                              QListWidget, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox,
                              QCheckBox, QFileDialog, QInputDialog, QMessageBox, QDialogButtonBox, QSplitter)

from .editing import locate
from .preview import PreviewPanel
from .sequence_assets import natural_order, load_rgba, slice_atlas, build_sequence_bundle


class SequenceDialog(QDialog):
    def __init__(self, document, entry_path, parent=None, resolver=None):
        super().__init__(parent); self.setWindowTitle('Sequence authoring'); self.resize(1000, 760)
        self.document, self.entry_path = document, entry_path
        self.resolver = resolver
        self.frames = []; self.bundle = None; self.output_path = None
        layout = QVBoxLayout(self); split = QSplitter(); layout.addWidget(split)
        self.frame_list = QListWidget(); self.frame_list.setMaximumWidth(280); split.addWidget(self.frame_list)
        self.preview = PreviewPanel(locate(document, entry_path).value); split.addWidget(self.preview)
        self.preview.mode.setCurrentIndex(0)
        controls = QHBoxLayout()
        for label, callback in [('Add PNGs', self.load_pngs), ('Slice atlas', self.load_atlas), ('Up', lambda: self.move(-1)), ('Down', lambda: self.move(1)), ('Remove frame', self.remove)]:
            button = QPushButton(label); button.clicked.connect(callback); controls.addWidget(button)
        layout.addLayout(controls)
        form = QFormLayout(); layout.addLayout(form)
        self.base = QLineEdit('vfx/custom/sequence'); form.addRow('Resource base', self.base)
        self.fps = QDoubleSpinBox(); self.fps.setRange(.01, 1000.); self.fps.setValue(60); form.addRow('Frames / second', self.fps)
        self.padding = QSpinBox(); self.padding.setRange(0, 1024); self.padding.setValue(4); form.addRow('Padding', self.padding)
        self.compression = QComboBox(); self.compression.addItems(['bc7', 'rgba8']); form.addRow('TEX encoding', self.compression)
        self.srgb = QCheckBox('sRGB'); self.srgb.setChecked(True); form.addRow(self.srgb)
        self.mipmaps = QCheckBox('Mipmaps'); self.mipmaps.setChecked(True); form.addRow(self.mipmaps)
        self.dependencies = QCheckBox('Include resolved EFX / UVS dependencies'); self.dependencies.setEnabled(resolver is not None); form.addRow(self.dependencies)
        self.output = QLineEdit(); line = QHBoxLayout(); line.addWidget(self.output)
        browse = QPushButton('…'); browse.clicked.connect(self.browse); line.addWidget(browse); form.addRow('New bundle directory', line)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.export); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.fps.valueChanged.connect(self._fps)
        self.frame_list.currentRowChanged.connect(self._frame)

    def _fps(self, value):
        self.preview.canvas.fps = value; self.preview.canvas.update()

    def _frame(self, row):
        if row >= 0 and self.frames:
            self.preview.play.setChecked(False)
            self.preview.mode.setCurrentIndex(0)
            self.preview.timeline.setValue(round(row * 60 / self.fps.value()))
            self.preview.canvas.frame = row * 60 / self.fps.value(); self.preview.canvas.update()

    def _refresh(self):
        self.preview.canvas.set_frames(self.frames)

    def load_pngs(self):
        files, _ = QFileDialog.getOpenFileNames(self, 'PNG frames', '', 'PNG (*.png)')
        try:
            loaded = [(p, load_rgba(p)) for p in natural_order(files)]
        except ValueError as error:
            QMessageBox.warning(self, 'Image load failed', str(error)); return
        for path, frame in loaded:
            self.frames.append(frame); self.frame_list.addItem(Path(path).name)
        self._refresh()

    def load_atlas(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Atlas', '', 'PNG (*.png)')
        if not path:
            return
        columns, ok = QInputDialog.getInt(self, 'Slice atlas', 'Columns', 8, 1, 512)
        if not ok:
            return
        rows, ok = QInputDialog.getInt(self, 'Slice atlas', 'Rows', 8, 1, 512)
        if not ok:
            return
        try:
            image = load_rgba(path); h, w = image.shape[:2]
            if w % columns or h % rows:
                raise ValueError('Atlas dimensions must divide evenly into this grid')
            fw, fh = w//columns, h//rows
            frames = slice_atlas(path, [[x*fw, y*fh, fw, fh] for y in range(rows) for x in range(columns)])
            for i, frame in enumerate(frames):
                self.frames.append(frame); self.frame_list.addItem(f'{Path(path).name}: {i}')
            self._refresh()
        except ValueError as error:
            QMessageBox.warning(self, 'Atlas load failed', str(error))

    def move(self, delta):
        row = self.frame_list.currentRow(); target = row + delta
        if 0 <= row < len(self.frames) and 0 <= target < len(self.frames):
            self.frames.insert(target, self.frames.pop(row)); item = self.frame_list.takeItem(row)
            self.frame_list.insertItem(target, item); self.frame_list.setCurrentRow(target); self._refresh()

    def remove(self):
        row = self.frame_list.currentRow()
        if row >= 0:
            self.frames.pop(row); self.frame_list.takeItem(row); self._refresh()

    def browse(self):
        parent = QFileDialog.getExistingDirectory(self, 'Bundle parent directory')
        if parent:
            self.output.setText(str(Path(parent) / 'sequence_bundle'))

    def export(self):
        try:
            if not self.output.text().strip():
                raise ValueError('Select a new bundle directory')
            self.bundle = build_sequence_bundle(self.document, self.entry_path, self.frames, self.base.text(), fps=self.fps.value(),
                                               padding=self.padding.value(), compression=self.compression.currentText(), srgb=self.srgb.isChecked(), mipmaps=self.mipmaps.isChecked())
            if self.dependencies.isChecked():
                self.bundle.include_dependencies(self.resolver)
            self.output_path = self.bundle.publish(self.output.text())
        except (ValueError, OSError, KeyError) as error:
            QMessageBox.warning(self, 'Sequence export failed', str(error)); return
        self.accept()
