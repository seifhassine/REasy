"""Selected FSM Action playback, sharing MOTLIST documents with open editors."""
import os

from PySide6.QtCore import Qt, QSignalBlocker, Signal, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLabel, QPushButton, QFileDialog

from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.preview.mhr_editor import MhrMotListEditor
from .motion_link import MotionLinkResolver, linked_context, default_bank_path, node_motions, action_motion, motion_entry_index


class FsmMotionPreview(QWidget):
    content_changed = Signal(bool)

    def __init__(self, handler, parent=None, *, viewport_factory=None):
        super().__init__(parent)
        self.handler = handler
        self.viewport_factory = viewport_factory
        self.resolver = MotionLinkResolver(linked_context(handler), default_bank_path(handler.filepath))
        self.node_index = None
        self.editor = None
        self._assets = None
        self.source_path = ''
        self.resource_path = ''
        self._source_stamp = None
        self._shared_document = False
        self._has_content = False
        self._closed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.actions = QComboBox(self)
        self.actions.setMinimumContentsLength(18)
        self.actions.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        row.addWidget(self.actions, 1)
        self.bank_button = QPushButton(self.tr('MOTBANK…'), self)
        self.bank_button.setToolTip(self.resolver.bank_path)
        self.bank_button.clicked.connect(self._choose_bank)
        row.addWidget(self.bank_button)
        self.open_button = QPushButton(self.tr('Open MOTLIST'), self)
        self.open_button.clicked.connect(self._open_motion)
        self.open_button.setEnabled(False)
        row.addWidget(self.open_button)
        layout.addLayout(row)
        self.status = QLabel(self.tr('Select a playback node.'), self)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.actions.currentIndexChanged.connect(self._load_selected)

    def set_node(self, index):
        self.node_index = index
        previous = self.actions.currentData()
        preferred = (previous.action_id, previous.ex_id) if previous is not None else None
        try:
            choices = node_motions(self.handler.motfsm, index) if index is not None else ()
        except (ValueError, IndexError) as exc:
            with QSignalBlocker(self.actions):
                self.actions.clear()
            self._clear(str(exc))
            return
        self._set_choices(choices, preferred)

    def set_action(self, id_hash, ex_id, node_index=None):
        self.node_index = node_index
        try:
            motion = action_motion(self.handler.motfsm, id_hash, ex_id)
            choices = node_motions(self.handler.motfsm, node_index) if motion is not None and node_index is not None else (
                (motion,) if motion is not None else ())
        except (ValueError, IndexError) as exc:
            with QSignalBlocker(self.actions):
                self.actions.clear()
            self._clear(str(exc))
            return
        self._set_choices(choices, (id_hash, ex_id))

    def _set_choices(self, choices, preferred):
        with QSignalBlocker(self.actions):
            self.actions.clear()
            for action in choices:
                label = f'Action {action.action_id}:{action.ex_id} · {action.bank_id}:{action.motion_id}'
                if not action.enabled:
                    label += self.tr(' · Disabled')
                self.actions.addItem(label, action)
            if preferred is not None:
                selected = next((i for i, action in enumerate(choices)
                                 if (action.action_id, action.ex_id) == preferred), 0)
                self.actions.setCurrentIndex(selected)
        self._load_selected()

    def _clear(self, message):
        self.status.setText(message)
        self.open_button.setEnabled(False)
        if self.editor is not None:
            self.editor.preview.playback.stop()
            self.editor.hide()
        self._set_content(False)

    def _set_content(self, available):
        self.setMaximumHeight(16777215 if available else self.sizeHint().height() if self.editor is None else 90)
        if available != self._has_content:
            self._has_content = available
            self.content_changed.emit(available)

    def _open_tab(self, path):
        app = self.handler.app
        workspace = getattr(app, 'project_workspace', None)
        if workspace is None:
            return None
        key = os.path.normcase(os.path.abspath(path))
        return next((tab for tab in workspace.sessions.active_tabs()
                     if isinstance(tab.handler, MotListHandler) and tab.filename
                     and os.path.normcase(os.path.abspath(tab.filename)) == key), None)

    def _load_selected(self, *_):
        if self._closed:
            return
        action = self.actions.currentData()
        if action is None:
            self._clear(self.tr('No PlayerPlayMotion2 playback in this selection.'))
            return
        try:
            resource = self.resolver.motion_path(action)
            source, data = self.resolver.load_resource(resource)
            stat = os.stat(source) if os.path.isfile(source) else None
            stamp = (stat.st_mtime_ns, stat.st_size) if stat is not None else None
            tab = self._open_tab(source)
            motion_handler = tab.handler if tab is not None else None
            if (self.editor is None or self.source_path != source
                    or (motion_handler is not None and self.editor.handler is not motion_handler)
                    or (motion_handler is None and (self._shared_document or stamp != self._source_stamp))):
                if motion_handler is None:
                    motion_handler = MotListHandler()
                    motion_handler.filepath = source
                    motion_handler.app = self.handler.app
                    motion_handler.resource_context = self.resolver.context
                    motion_handler.read(data)
                self._dispose_editor()
                self.editor = MhrMotListEditor(motion_handler, viewport_factory=self.viewport_factory,
                                              read_only=True, compact=True, assets=self._assets)
                motion_handler.document_changed.connect(self._linked_document_changed)
                motion_handler.destroyed.connect(self._source_destroyed)
                self.layout().addWidget(self.editor, 1)
                self.source_path = source
                self._source_stamp = stamp
                self._shared_document = tab is not None
            self.resource_path = resource
            self._select_motion(action.motion_id)
            self.editor.show()
            self._set_content(True)
            self.open_button.setEnabled(callable(getattr(self.handler.app, 'add_tab', None)))
            self.status.setText(f'{action.bank_id}:{action.motion_id} · {resource}')
            self.status.setToolTip(source)
        except (OSError, ValueError, IndexError) as exc:
            self._clear(str(exc))

    def _select_motion(self, motion_id):
        preview = self.editor.preview
        index = motion_entry_index(preview._motions, motion_id)
        if preview.motion_browser.current_index != index:
            with QSignalBlocker(preview.motion_browser):
                preview.motion_browser.animation_list.setCurrentRow(index)
            preview.playback.stop()
            preview._load_current_motion(reset_camera=False)
            self.editor._sync_timeline()

    def _linked_document_changed(self):
        self._load_selected()

    def _choose_bank(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr('Player MOTBANK'), '', 'MOTBANK (*.motbank.*)')
        if path:
            self.resolver = MotionLinkResolver(self.resolver.context, path)
            self.bank_button.setToolTip(path)
            self._load_selected()

    def _open_motion(self):
        if self.editor is None:
            return
        app = self.handler.app
        tab = self._open_tab(self.source_path)
        if tab is None:
            source, data = self.resolver.load_resource(self.resource_path)
            tab = app.add_tab(source, data, resource_context=self.resolver.context,
                              pak_source_path=None if os.path.isfile(source) else source,
                              pak_project_dir=self.resolver.context.project_dir or None)
            if tab is not None and isinstance(tab.viewer, MhrMotListEditor):
                tab.viewer._assets = self.editor._assets
        else:
            notebook = app.project_workspace.sessions.notebook_for(tab.notebook_widget)
            if notebook is not None:
                notebook.setCurrentWidget(tab.notebook_widget)
                app.editor_groups.activate_page(tab.notebook_widget)
            else:
                for window in app.project_workspace.sessions.windows_for([tab]):
                    window.show(); window.raise_(); window.activateWindow()
        if tab is not None:
            self._load_selected()
            action = self.actions.currentData()
            if isinstance(tab.viewer, MhrMotListEditor) and action is not None:
                index = motion_entry_index(tab.viewer.preview._motions, action.motion_id)
                tab.viewer.preview.motion_browser.filter_edit.clear()
                tab.viewer.preview.motion_browser.animation_list.setCurrentRow(index)

    def _source_destroyed(self):
        self._dispose_editor(source_destroyed=True)
        if not self._closed:
            QTimer.singleShot(0, self._load_selected)

    def _dispose_editor(self, *, source_destroyed=False):
        if self.editor is not None:
            if self.editor._assets is not None:
                self._assets = self.editor._assets
            if not source_destroyed:
                self.editor.handler.document_changed.disconnect(self._linked_document_changed)
                self.editor.handler.destroyed.disconnect(self._source_destroyed)
            self.editor.cleanup()
            self.layout().removeWidget(self.editor)
            self.editor.hide()
            self.editor.deleteLater()
            self.editor = None

    def hideEvent(self, event):
        if self.editor is not None:
            self.editor.preview.playback.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self.node_index is not None:
            self._load_selected()

    def cleanup(self):
        self._closed = True
        self._dispose_editor()
