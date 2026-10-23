import json
from dataclasses import asdict
from pathlib import Path

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton,
                              QInputDialog, QMessageBox, QFileDialog, QDialogButtonBox)
from PySide6.QtCore import Qt

from .sequence_project import SequenceProject


class LinkedUvsDialog(QDialog):
    def __init__(self, document, uvs, resource, resolver, parent=None):
        super().__init__(parent); self.setWindowTitle('Linked UVS'); self.resize(820, 600)
        self.project = SequenceProject(document, uvs, resource); self.resolver = resolver
        layout = QVBoxLayout(self); self.tree = QTreeWidget(); self.tree.setHeaderLabels(['Sequence / frame / texture', 'Value']); layout.addWidget(self.tree)
        controls = QHBoxLayout()
        for label, callback in [('Add', self.add), ('Edit', self.edit), ('Duplicate', self.duplicate), ('Delete', self.delete),
                                ('Up', lambda: self.move(-1)), ('Down', lambda: self.move(1)), ('Undo', self.undo), ('Redo', self.redo)]:
            button = QPushButton(label); button.clicked.connect(callback); controls.addWidget(button)
        layout.addLayout(controls); buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject); layout.addWidget(buttons); self.refresh()

    def refresh(self):
        self.tree.clear(); sequences = QTreeWidgetItem(self.tree, ['Sequences']); sequences.setData(0, Qt.UserRole, ('sequence',))
        for i, sequence in enumerate(self.project.uvs.sequences):
            row = QTreeWidgetItem(sequences, [str(i), f'{len(sequence.patterns)} frames']); row.setData(0, Qt.UserRole, ('sequence', i))
            for fi, pattern in enumerate(sequence.patterns):
                child = QTreeWidgetItem(row, [str(fi), f'Texture {pattern.texture_index}: {pattern.left:g}, {pattern.top:g}, {pattern.right:g}, {pattern.bottom:g}'])
                child.setData(0, Qt.UserRole, ('frame', i, fi))
        textures = QTreeWidgetItem(self.tree, ['Textures']); textures.setData(0, Qt.UserRole, ('texture',))
        for i, texture in enumerate(self.project.uvs.textures):
            row = QTreeWidgetItem(textures, [str(i), texture.path]); row.setData(0, Qt.UserRole, ('texture', i))
        self.tree.expandToDepth(0); self.tree.setColumnWidth(0, 250)

    def selected(self):
        row = self.tree.currentItem()
        return row.data(0, Qt.UserRole) if row else None

    def run(self, op):
        try:
            self.project.apply(op); self.refresh()
        except (ValueError, KeyError, TypeError, IndexError) as error:
            QMessageBox.warning(self, 'UVS edit failed', str(error))

    def _op(self, action):
        selected = self.selected()
        if not selected or len(selected) == 1:
            return None
        op = {'op': selected[0] + '-' + action, 'index': selected[-1]}
        if selected[0] == 'frame':
            op['sequence'] = selected[1]
        return op

    def delete(self):
        op = self._op('delete')
        if op: self.run(op)

    def duplicate(self):
        op = self._op('copy')
        if op: op['to'] = op['index']+1; self.run(op)

    def move(self, delta):
        op = self._op('move')
        if op: op['to'] = op['index']+delta; self.run(op)

    def undo(self):
        if self.project.undo(): self.refresh()

    def redo(self):
        if self.project.redo(): self.refresh()

    def add(self):
        self._value('insert')

    def edit(self):
        self._value('set')

    def _value(self, action):
        selected = self.selected()
        if not selected:
            return
        category = selected[0]
        pattern = {'flags': 0, 'left': 0., 'top': 0., 'right': 1., 'bottom': 1., 'texture_index': 0, 'cutout_uvs': []}
        if action == 'set':
            op = self._op(action)
            if not op: return
            collection = self.project.uvs.sequences if category == 'sequence' else self.project.uvs.textures if category == 'texture' else self.project.uvs.sequences[selected[1]].patterns
            value = asdict(collection[selected[-1]])
            if category == 'sequence':
                value = {'patterns': [{k:v for k,v in p.items() if k != 'cutout_uv_count'} for p in value['patterns']]}
            elif category == 'frame':
                value.pop('cutout_uv_count')
        else:
            op = {'op': category+'-insert'}
            value = {'patterns': [pattern]} if category == 'sequence' else {'path': ''} if category == 'texture' else pattern
            if category == 'frame': op['sequence'] = selected[1]
        text, ok = QInputDialog.getMultiLineText(self, action.title(), category.title(), json.dumps(value, indent=2))
        if ok:
            try: op['value'] = json.loads(text)
            except ValueError as error: QMessageBox.warning(self, 'Invalid JSON', str(error)); return
            self.run(op)

    def save(self):
        base, ok = QInputDialog.getText(self, 'Export linked bundle', 'EFX resource base', text='vfx/custom/effect')
        if not ok: return
        parent = QFileDialog.getExistingDirectory(self, 'Bundle parent directory')
        if not parent: return
        name, ok = QInputDialog.getText(self, 'Export linked bundle', 'New directory name', text='linked_uvs_bundle')
        if not ok or not name: return
        try:
            if Path(name).name != name or name in ('.', '..'):
                raise ValueError('Enter a directory name without a path')
            bundle = self.project.bundle(base); bundle.include_dependencies(self.resolver); self.output_path = bundle.publish(Path(parent) / name)
        except (OSError, ValueError, KeyError) as error:
            QMessageBox.warning(self, 'Export failed', str(error)); return
        self.accept()
