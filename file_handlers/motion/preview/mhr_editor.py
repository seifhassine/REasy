from __future__ import annotations

import math
from enum import IntEnum

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, Signal, QSignalBlocker, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QSplitter, QTreeView,
                               QMessageBox, QPushButton, QComboBox, QFileDialog,
                               QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QSpinBox, QApplication)

from ..mhr_storage import Field, Group
from ..mhr_editing import duplicate_slot, next_motion_id
from ..mhr_import import default_motion_name
from .resolution import MotionListDocument
from .widget import MotListPreviewWidget
from .assembly_renderer import MotionAssemblyRenderer
from .clip_timeline import ClipTimeline
from .mhr_assets import MhrAssetLoader, WEAPON_PRESETS, weapon_family
from .weapon_motion import select_weapon_motion


def display_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (tuple, list)):
        return ', '.join(format(v, '.9g') for v in value)
    return format(value, '.9g') if isinstance(value, float) else str(value)


def parse_value(text, current):
    text = str(text).strip()
    if isinstance(current, bool):
        if text.lower() not in ('true', 'false', '0', '1'):
            raise ValueError('Enter true or false')
        return text.lower() in ('true', '1')
    if isinstance(current, (tuple, list)):
        result = tuple(float(part.strip()) for part in text.split(','))
        if len(result) != len(current) or not all(math.isfinite(v) for v in result):
            raise ValueError(f'Enter {len(current)} finite components separated by commas')
        return result
    if isinstance(current, float):
        result = float(text)
        if not math.isfinite(result):
            raise ValueError('Enter a finite number')
        return result
    if isinstance(current, int):
        value = int(text, 16 if text.lower().startswith(('0x', '-0x')) else 10)
        return type(current)(value) if isinstance(current, IntEnum) else value
    return str(text)


class FieldTreeModel(QAbstractItemModel):
    """Lazily materialized Qt indexes over parsed groups, without duplicating keys."""

    def __init__(self, groups, commit, parent=None):
        super().__init__(parent)
        self.root = Group('', groups)
        self.commit = commit
        self.parents = {}
        self.rows = {}

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid() and parent.column() != 0:
            return 0
        item = parent.internalPointer() if parent.isValid() else self.root
        return len(item.children) if isinstance(item, Group) else 0

    def columnCount(self, parent=QModelIndex()):
        return 3

    def index(self, row, column, parent=QModelIndex()):
        owner = parent.internalPointer() if parent.isValid() else self.root
        if not isinstance(owner, Group) or not 0 <= row < len(owner.children):
            return QModelIndex()
        item = owner.children[row]
        self.parents[id(item)] = owner
        self.rows[id(item)] = row
        return self.createIndex(row, column, item)

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        owner = self.parents[id(index.internalPointer())]
        if owner is self.root:
            return QModelIndex()
        return self.createIndex(self.rows[id(owner)], 0, owner)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = index.internalPointer()
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.name
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return None
        if index.column() == 0:
            return item.name
        if index.column() == 1:
            if isinstance(item, Field):
                if item.format.startswith('hash'):
                    return f'0x{int.from_bytes(item.encode(), "little"):X}'
                return display_value(item.get())
            return len(item.children)
        if isinstance(item, Field):
            return f'0x{item.offset:X}'
        return ''

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ('Field', 'Value', 'Source offset')[section]
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() == 1:
            item = index.internalPointer()
            if isinstance(item, Field) and item.editable:
                flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        item = index.internalPointer()
        if not isinstance(item, Field) or not item.editable or index.column() != 1:
            return False
        old = item.get()
        try:
            new = parse_value(value, old)
            item.set(new)
            item.encode()
            self.commit()
        except (ValueError, OverflowError) as exc:
            item.set(old)
            QMessageBox.warning(self.parent_widget, 'MOTLIST edit', str(exc))
            return False
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole])
        return True


class MhrMotListEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler, *, viewport_factory=None, read_only=False, compact=False, assets=None):
        super().__init__()
        self.handler = handler
        self.read_only = read_only
        self._emitting_edit = False
        self.document = handler.model
        self._assets = assets
        self._asset_loader = None
        self._pending_resource_directory = ''
        self._cleaned = False
        self._weapon_motion_controls = {}
        self._index_fields()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        kwargs = {} if viewport_factory is None else {'viewport_factory': viewport_factory}
        self.preview = MotListPreviewWidget(handler, **kwargs)
        self.duplicate_button = QPushButton(self.tr('Duplicate animation…'), self)
        self.duplicate_button.clicked.connect(self._duplicate_animation)
        self.preview.animation_pane.add_widget(self.duplicate_button)
        self.preview._scene_renderer = MotionAssemblyRenderer(self.preview.viewport)
        self.preview.motion_changed.connect(self._set_preview_motion)
        if self.preview.current_motion is not None:
            self._set_preview_motion(self.preview.current_motion)
        preview_page = QSplitter(Qt.Orientation.Vertical, self)
        preview_page.addWidget(self.preview)
        timeline_page = QSplitter(Qt.Orientation.Horizontal, self)
        self.timeline = ClipTimeline(self._commit_timeline, self)
        self.timeline.view.read_only = read_only
        timeline_page.addWidget(self.timeline)
        self.timeline_inspector = QTreeView(self)
        self.timeline_inspector.setMinimumWidth(260)
        self.timeline_inspector.setMaximumWidth(420)
        self.timeline_inspector.setUniformRowHeights(True)
        timeline_page.addWidget(self.timeline_inspector)
        timeline_page.setSizes([1050, 340])
        preview_page.addWidget(timeline_page)
        preview_page.setSizes([620, 300])
        layout.addWidget(preview_page)
        self.timeline.frame_requested.connect(self.preview.playback._on_frame_changed)
        self.timeline.selection_changed.connect(self._inspect_event)
        self.preview.playback.render_requested.connect(
            lambda _: self.timeline.view.set_playhead(self.preview.controller.current_frame))
        self.preview.motion_browser.selection_changed.connect(self._sync_timeline)
        self.weapon_combo = QComboBox()
        self.weapon_combo.addItem(self.tr('Auto weapon'), None)
        self.weapon_combo.addItem(self.tr('No weapon'), '')
        for family in WEAPON_PRESETS: self.weapon_combo.addItem(family, family)
        self.preview.rig_pane.add_widget(self.weapon_combo)
        self.default_rig_button = QPushButton(self.tr('Default hunter / weapon'))
        self.default_rig_button.clicked.connect(lambda: self._load_default_target())
        self.preview.rig_pane.add_widget(self.default_rig_button)
        self.resource_directory_button = QPushButton(self.tr('Game resource directory…'))
        self.resource_directory_button.clicked.connect(self._choose_resource_directory)
        self.preview.rig_pane.add_widget(self.resource_directory_button)
        self.weapon_combo.currentIndexChanged.connect(lambda _: self._load_default_target())
        self._sync_timeline()
        if read_only:
            self.duplicate_button.hide()
            self.timeline_inspector.setEditTriggers(QTreeView.NoEditTriggers)
        if compact:
            self.preview.animation_pane.hide()
            self.preview.rig_pane.hide()
            self.preview.workspace.top_bar.hide()
            self.preview.viewport_pane.add_widget(self.preview.playback)
            self.timeline_inspector.hide()
            preview_page.setSizes([280, 150])
        handler.document_changed.connect(self._external_document_changed)
        QTimer.singleShot(0, self._load_default_target)

    def _external_document_changed(self):
        if self._emitting_edit or self._cleaned:
            return
        frame = self.preview.controller.current_frame
        self.document = self.handler.model
        self._index_fields()
        root = self.preview._catalog.root
        self.preview._catalog.root = MotionListDocument(root.path, self.document)
        self.preview.playback.stop()
        self.preview._populate_motions(reset_camera=False)
        self.preview.playback._on_frame_changed(frame)
        self._sync_timeline()

    def _index_fields(self):
        self._fields_by_owner = {}
        for field in self.document.fields:
            if field.owner is not None:
                self._fields_by_owner.setdefault(id(field.owner), []).append(field)

    def _duplicate_animation(self):
        try:
            suggested_id = next_motion_id(self.document)
        except ValueError as exc:
            QMessageBox.warning(self, self.tr('Duplicate animation'), str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr('Duplicate animation'))
        dialog.setMinimumWidth(460)
        form = QFormLayout(dialog)
        motion_id = QSpinBox(dialog)
        motion_id.setObjectName('newMotionId')
        motion_id.setRange(0, 0xFFFF)
        motion_id.setValue(suggested_id)
        native_name = default_motion_name(self.document, suggested_id) or self.preview.current_motion.name
        name = QLineEdit(native_name, dialog)
        name.setObjectName('newMotionName')
        form.addRow(self.tr('Motion ID'), motion_id)
        form.addRow(self.tr('Name'), name)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, parent=dialog)
        form.addRow(buttons)
        buttons.rejected.connect(dialog.reject)
        def accept():
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.duplicate_current_motion(motion_id.value(), name.text().strip())
            except ValueError as exc:
                QMessageBox.warning(dialog, self.tr('Duplicate animation'), str(exc))
                return
            finally:
                QApplication.restoreOverrideCursor()
            dialog.accept()
        buttons.accepted.connect(accept)
        dialog.exec()
        dialog.deleteLater()

    def duplicate_current_motion(self, motion_id, name):
        entry = self.preview.current_entry
        if entry is None or entry.source_list_name != self.document.name or self.document.slots[entry.slot_index].payload is None:
            raise ValueError('Select an embedded animation to duplicate.')
        if not name or '\0' in name:
            raise ValueError('Enter a nonempty animation name without null characters.')
        result = duplicate_slot(self.document, entry.slot_index, motion_id, name=name)
        self.document = result
        self._index_fields()
        self._accept_document_edit(result, selected_slot=len(result.slots)-1)

    def _set_preview_motion(self, motion):
        entry = self.preview.current_entry
        motion_id = entry.motion_id if entry is not None else None
        self.preview._scene_renderer.set_motion(motion, motion_id)
        for part, combo in self._weapon_motion_controls.values():
            choice = select_weapon_motion(part.weapon_motions, motion_id)
            combo.setItemText(0, self.tr('Auto · ')+(choice.label if choice is not None else self.tr('Rest pose')))

    def _configure_weapon_motion_controls(self, target):
        for part, combo in self._weapon_motion_controls.values():
            combo.deleteLater()
        self._weapon_motion_controls.clear()
        self.preview._scene_renderer.weapon_selections.clear()
        for part in target.parts:
            if not part.weapon_motions:
                continue
            combo = QComboBox(self)
            combo.setToolTip(part.key)
            combo.setMinimumContentsLength(15)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.addItem(self.tr('Auto weapon motion'), -1)
            combo.addItem(self.tr('Weapon rest pose'), -2)
            for index, choice in enumerate(part.weapon_motions):
                combo.addItem(choice.label, index)
            combo.currentIndexChanged.connect(lambda _, key=part.key, widget=combo: self._select_weapon_motion(key, widget.currentData()))
            self.preview.rig_pane.add_widget(combo)
            self._weapon_motion_controls[part.key] = part, combo

    def _select_weapon_motion(self, part_key, selection):
        self.preview._scene_renderer.set_weapon_selection(part_key, selection)
        self.preview._render()

    def _sync_timeline(self, *_):
        entry = self.preview.current_entry
        slot = self.document.slots[entry.slot_index] if entry is not None and entry.source_list_name == self.document.name else None
        self.duplicate_button.setEnabled(slot is not None and slot.payload is not None)
        motion = slot.payload.value if slot is not None and slot.payload is not None else None
        if self.timeline.motion is not motion:
            old = self.timeline_inspector.model()
            self.timeline_inspector.setModel(None)
            if old is not None: old.deleteLater()
        self.timeline.set_motion(motion, slot.overrides if slot is not None else ())
        self.timeline.view.set_playhead(self.preview.controller.current_frame)

    def _inspect_event(self, lane, key):
        groups = [Group(lane.name, self._fields_by_owner.get(id(lane.prop), []))]
        if key is not None:
            groups.insert(0, Group(self.tr('Key'), self._fields_by_owner.get(id(key), [])))
        model = FieldTreeModel(groups, self._commit_timeline, self.timeline_inspector)
        model.parent_widget = self
        old = self.timeline_inspector.model()
        self.timeline_inspector.setModel(model)
        self.timeline_inspector.setColumnHidden(2, True)
        self.timeline_inspector.setColumnWidth(0, 145)
        self.timeline_inspector.expandAll()
        if old is not None: old.deleteLater()

    def _load_default_target(self, directory=''):
        if self._cleaned or (self._asset_loader is not None and self._asset_loader.isRunning()):
            return
        family = self.weapon_combo.currentData()
        weapon_only = self.document.name.casefold().startswith('wpg_')
        if weapon_only and family == '':
            self.preview.use_source_rig()
            return
        if family is None: family = weapon_family(self.document.name)
        self.default_rig_button.setEnabled(False)
        self.weapon_combo.setEnabled(False)
        self.preview.rig_label.setText(self.tr('Loading hunter and weapon…'))
        self._pending_resource_directory = directory
        loader = MhrAssetLoader(self.handler, family, assets=None if directory else self._assets,
                                directory=directory, weapon_only=weapon_only, parent=self)
        self._asset_loader = loader
        loader.loaded.connect(self._default_target_loaded)
        loader.failed.connect(self._default_target_failed)
        loader.finished.connect(lambda: self._release_asset_loader(loader))
        loader.start()

    def _release_asset_loader(self, loader):
        if self._asset_loader is loader:
            self._asset_loader = None
        loader.deleteLater()

    def _default_target_loaded(self, assets, target):
        if self._cleaned: return
        self._assets = assets
        self.handler.resource_context = assets.context
        self.preview._catalog.resources.resource_context = assets.context
        self.preview._catalog.resources.errors.clear()
        if self._pending_resource_directory and self.handler.app is not None:
            self.handler.app.settings['mhr_preview_game_directory'] = self._pending_resource_directory
        self._pending_resource_directory = ''
        self.default_rig_button.setEnabled(True);self.weapon_combo.setEnabled(True)
        if self.document.base_motion_list_path:
            self.preview._populate_motions()
        self._configure_weapon_motion_controls(target)
        self.preview.set_target(target)

    def _default_target_failed(self, message):
        if self._cleaned: return
        self.default_rig_button.setEnabled(True);self.weapon_combo.setEnabled(True)
        self._pending_resource_directory = ''
        self.preview.rig_label.setText(message)

    def _choose_resource_directory(self):
        directory = QFileDialog.getExistingDirectory(self, self.tr('Monster Hunter Rise resources'))
        if directory:
            self._load_default_target(directory)

    def _commit_timeline(self):
        # Timeline edits already operate on typed, bounded semantic fields.
        # Full binary validation stays at MotListHandler.rebuild on save.
        self._accept_document_edit(self.document)

    def _accept_document_edit(self, preview_model, *, selected_slot=None):
        self.handler.motlist_file.model = self.document
        self.handler.modified = True
        root = self.preview._catalog.root
        frame = self.preview.controller.current_frame
        index = self.preview.motion_browser.current_index
        self.preview.playback.stop()
        self.preview._catalog.root = MotionListDocument(root.path, preview_model)
        resolution = self.preview._catalog.refresh()
        self.preview._motions = list(resolution.entries)
        if selected_slot is not None:
            index = next(i for i, entry in enumerate(self.preview._motions)
                         if entry.source_list_name == self.document.name and entry.slot_index == selected_slot)
        with QSignalBlocker(self.preview.motion_browser):
            if selected_slot is not None:
                self.preview.motion_browser.filter_edit.clear()
            self.preview.motion_browser.set_entries(self.preview._motions, max(0, index))
        self.preview._load_current_motion(reset_camera=False)
        self.preview.playback._on_frame_changed(frame)
        self._sync_timeline()
        self.modified_changed.emit(True)
        self._emitting_edit = True
        try:
            self.handler.document_changed.emit()
        finally:
            self._emitting_edit = False

    def cleanup(self):
        self._cleaned = True
        if self._asset_loader is not None and self._asset_loader.isRunning():
            self._asset_loader.wait()
        self.preview.cleanup()

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)
