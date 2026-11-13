"""See docs/internals/motion-editor.md."""

from __future__ import annotations

import functools
from pathlib import Path

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHeaderView,
    QInputDialog,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

lazy from file_handlers.clip.editor import ClipEditor
lazy from file_handlers.clip.editor.transport import Transport
lazy from file_handlers.mesh.blender.mapping import suggest
lazy from file_handlers.mesh.blender_ui import require_blender, review_mapping, run_busy
lazy from settings import app_settings
lazy from ui.editor_widgets import editor_strip, tool_button, toolbar_separator
lazy from utils.hash_util import murmur3_hash_utf16le

from file_handlers.document_commands import Change

from .. import edits
from ..banks import entry_mirrored, entry_motion, entry_sequences, handler_reader, stop_row
from ..blender import action_bones, action_tracks, export_blend, read_actions
from ..model import MotionList, MotionListEntry, MotionTree, Sequence
from ..pose import Rig
from ..preview import MotionPreview
from ..retarget import bones_of
from ..tree_runtime import node_params
from ..tree_sampler import TreeSampler, uvar_variables
from .curves import TrackCurves
from .inspector import Inspector
from .outline import MotionOutline
from .state import EditorState
from .tree_variables import TreeVariables

lazy from .tree_graph import TreeGraph


class MotionEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler, parent: QWidget | None = None):
        super().__init__(parent)
        self.handler = handler
        self.document = document = handler.document
        document.modified_changed.connect(self.modified_changed)
        self.state = EditorState(document, self)
        lists: dict = {}
        read = handler_reader(handler)
        self.state.inherited = lambda entry: entry_motion(read, document.root, entry, lists)
        self.state.mirrored_of = lambda entry: entry_mirrored(read, document.root, entry, lists)
        self.state.sequences_of = lambda entry: entry_sequences(read, document.root, entry, lists)
        if isinstance(document.root, MotionList):
            self.state.stop_of = lambda: stop_row(read, document.root, lists)

        @functools.cache
        def uvar(path: str) -> dict:
            return uvar_variables(read(path) if path else None)

        self.state.variables_of = lambda node: uvar(node_params(node).get("Asset") or "")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar, row = editor_strip()
        undo = tool_button("undo", self.tr("Undo (Ctrl+Z)"), document.undo_stack.undo)
        redo = tool_button("redo", self.tr("Redo (Ctrl+Y)"), document.undo_stack.redo)
        document.undo_stack.canUndoChanged.connect(undo.setEnabled)
        document.undo_stack.canRedoChanged.connect(redo.setEnabled)
        undo.setEnabled(False)
        redo.setEnabled(False)
        for widget in (undo, redo, toolbar_separator(),
                       tool_button("copy", self.tr("Duplicate (Ctrl+D)"), self._duplicate),
                       tool_button("delete", self.tr("Delete (Del)"), self._delete), toolbar_separator(),
                       tool_button("export", self.tr("Export the motions to a .blend, one action each"),
                                   self._export_blend, text=self.tr("Blender")),
                       tool_button("import", self.tr("Replace the motion's keys with an action from a .blend"),
                                   self._import_blend)):
            row.addWidget(widget)
        row.addStretch(1)
        layout.addWidget(bar)
        columns = QSplitter(Qt.Horizontal, self)
        self.tree = QTreeView(columns)
        self.tree.setModel(MotionOutline(document, self.tree))
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.selectionModel().selectionChanged.connect(self._tree_selection)
        self.tree.model().modelReset.connect(self._sync_tree)
        columns.addWidget(self.tree)
        center = QSplitter(Qt.Vertical, columns)
        self.preview = MotionPreview(center, picker=handler, settings=app_settings(handler.app))
        if isinstance(document.root, MotionList):
            self.preview.list_path = handler.filepath
        self.lower = QStackedWidget(center)
        self.curves = TrackCurves(self.state, self.lower)
        self.lower.addWidget(self.curves)
        self.trees = QSplitter(Qt.Horizontal, self.lower)
        self.graph: TreeGraph | None = None
        self.variables = TreeVariables(self.trees)
        self.variables.changed.connect(self.preview.refresh_tree)
        self.variables.changed.connect(self._show_weights)
        self.preview.played.connect(self._played)
        self.trees.addWidget(self.variables)
        self.lower.addWidget(self.trees)
        self._clip_editor: ClipEditor | None = None
        columns.addWidget(center)
        self.inspector = Inspector(self.state, columns)
        columns.addWidget(self.inspector)
        columns.setStretchFactor(1, 1)
        columns.setSizes([280, 900, 320])
        layout.addWidget(columns, 1)
        self.transport = Transport(self.state, self, self.preview.scene.preview)
        layout.addWidget(self.transport)
        self.state.motion_changed.connect(self._motion_changed)
        self.state.frame_changed.connect(self.preview.set_frame)
        self.state.frame_changed.connect(self._show_weights)
        self.state.selection_changed.connect(self._selection_changed)
        # Edits of the shown motion pose it again once, after the edit's last change.
        self._edited = QTimer(self)
        self._edited.setSingleShot(True)
        self._edited.timeout.connect(self._repose)
        document.changed.connect(self._document_changed)
        for pane in (self.tree, self.preview, self.curves, self.trees, self.inspector, self.transport):
            for keys, slot in ((QKeySequence.Undo, document.undo_stack.undo),
                               (QKeySequence.Redo, document.undo_stack.redo), ("Ctrl+D", self._duplicate)):
                QShortcut(keys, pane, slot).setContext(Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence.Delete, self.tree, self._delete).setContext(Qt.WidgetShortcut)
        self.preview.scene.preview.set_fullscreen_content(self)
        self._motion_changed()

    def _tree_selection(self, *_args) -> None:
        model = self.tree.model()
        self.state.select([model.item(index) for index in self.tree.selectionModel().selectedRows()])

    def _selection_changed(self) -> None:
        current = self.state.current
        if isinstance(current, Sequence):
            clip = self.document.clip_document(current)
            if self._clip_editor is None or self._clip_editor.document is not clip:
                if self._clip_editor is not None:
                    self.lower.removeWidget(self._clip_editor)
                    self._clip_editor.deleteLater()
                self._clip_editor = ClipEditor(clip, self.lower)
                self.lower.addWidget(self._clip_editor)
            self.lower.setCurrentWidget(self._clip_editor)
        elif (tree := self.state.tree_of(current) or (
                self.state.played if isinstance(self.state.played, MotionTree) else None)) is not None:
            if self.graph is None:
                self.graph = TreeGraph(self.state, self.trees)
                self.graph.picked.connect(self._graph_picked)
                self.trees.insertWidget(0, self.graph)
                self.trees.setStretchFactor(0, 1)
            self.graph.show_tree(tree)
            self.graph.select(self.state.selection)
            self.lower.setCurrentWidget(self.trees)
        else:
            self.lower.setCurrentWidget(self.curves)
        self._sync_tree()

    def _graph_picked(self, items: list) -> None:
        tree, root = self.graph.tree, self.document.root
        self.state.select(items or [next((entry for entry in getattr(root, "entries", ()) if entry.item is tree),
                                         root)])

    def _show_weights(self, *_args) -> None:
        if self.graph is not None:
            sampler = self.preview.sampler
            self.graph.show_weights(sampler.runtime if isinstance(sampler, TreeSampler) and
                                    sampler.tree is self.graph.tree else None)

    def _sync_tree(self) -> None:
        model, selection = self.tree.model(), QItemSelection()
        for index in map(model.index_of, self.state.selection):
            if index.isValid():
                selection.select(index, index.siblingAtColumn(1))
        with QSignalBlocker(self.tree.selectionModel()):
            self.tree.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect)
        if (index := model.index_of(self.state.current)).isValid():
            self.tree.scrollTo(index)
        self.tree.viewport().update()

    def _motion_changed(self) -> None:
        motion = self.state.motion
        if motion is not None:
            self.transport.rate_spin.setValue(motion.fps or 60)
        self._repose()
        self.preview.set_frame(self.state.frame)

    def _document_changed(self, change: Change) -> None:
        motion, played = self.state.motion, self.state.played
        if isinstance(change.target, MotionListEntry) and change.field == "flags":
            self.state.select(self.state.selection)
        elif motion is not None and self.document.motion_of(change.target) is motion:
            if change.target is motion and change.field == "fps":
                self.transport.rate_spin.setValue(motion.fps or 60)
            self._edited.start()
        elif isinstance(played, MotionTree) and self.state.tree_of(change.target) is played:
            self._edited.start()

    def _repose(self) -> None:
        self.preview.set_motion(self.state.played, mirrored=self.state.mirrored, sequences=self.state.sequences,
                                mask=self.state.mask)
        self._selection_changed()

    def _played(self) -> None:
        if isinstance(self.state.played, MotionTree):
            sampler = self.preview.sampler
            self.state.set_end_frame(sampler.end_frame if isinstance(sampler, TreeSampler) else 0.0)
            self.variables.show_variables(self.preview.tree_variables())
            self._show_weights()

    def _delete(self) -> None:
        edits.delete(self.document, list(self.state.selection))

    def _skeletons(self) -> list:
        motion = self.state.motion
        return sorted((m.skeleton for m in self.document.motions() if m.skeleton is not None),
                      key=lambda skeleton: motion is None or skeleton is not motion.skeleton)

    def _rig(self) -> Rig | None:
        rig, motion = self.preview.rig(), self.state.motion
        if rig is not None and motion is not None and set(rig.hashes) & {c.name_hash for c in motion.channels}:
            return rig
        skeletons = self._skeletons()
        return Rig.of_skeleton(*skeletons) if skeletons else rig

    def _export_blend(self) -> None:
        title = self.tr("Export to Blender")
        motions = self.document.motions() or ([self.state.motion] if self.state.motion is not None else [])
        rig = self._rig()
        if not motions or rig is None:
            QMessageBox.information(self, title, self.tr("Exporting needs a motion and a skeleton: this file holds no "
                                                         "skeleton, and no prefab is previewed."))
            return
        name = Path(self.handler.filepath or "motion").name.split(".")[0]
        path, _filter = QFileDialog.getSaveFileName(self, title, name + ".blend", "Blender (*.blend)")
        blender = require_blender(self, self.handler.app) if path else None
        add = rig is self.preview.rig() and self.preview.adds()
        if blender is not None:
            _done, error = run_busy(self, self.tr("Writing the .blend…"),
                                    lambda: export_blend(blender, rig, motions, path, name, add))
            if error:
                QMessageBox.warning(self, title, error)

    def _import_blend(self) -> None:
        title = self.tr("Import from Blender")
        motion, rig = self.state.motion, self._rig()
        if motion not in self.document.motions() or rig is None:
            QMessageBox.information(self, title, self.tr("Select a motion of this file first; its joints come from "
                                                         "the file's skeleton or the previewed prefab."))
            return
        path, _filter = QFileDialog.getOpenFileName(self, title, "", "Blender (*.blend)")
        blender = require_blender(self, self.handler.app) if path else None
        if blender is None:
            return
        read, error = run_busy(self, self.tr("Reading the actions…"), lambda: read_actions(blender, path))
        if error:
            QMessageBox.warning(self, title, error)
            return
        manifest, arrays = read
        names = [action["name"] for action in manifest["actions"]]
        if not names:
            QMessageBox.information(self, title, self.tr("The .blend has no action keying its armature's bones."))
            return
        number = 0
        if len(names) > 1:
            chosen, accepted = QInputDialog.getItem(self, title, self.tr("Action"), names,
                                                    names.index(motion.name) if motion.name in names else 0, False)
            if not accepted:
                return
            number = names.index(chosen)
        pairs = self._pairs(manifest, number, rig, motion)
        if pairs is None:
            return
        tracks, end = action_tracks(manifest, arrays, number, rig, motion, pairs,
                                    rig is self.preview.rig() and self.preview.adds())
        if not tracks:
            QMessageBox.information(self, title, self.tr("The action keys none of the joints its bones map onto."))
            return
        joint_ids = {murmur3_hash_utf16le(joint.name): joint.joint_id
                     for skeleton in reversed(self._skeletons()) for joint in skeleton.joints}
        edits.replace_tracks(self.document, motion, tracks, end, joint_ids, title)

    def _pairs(self, manifest: dict, number: int, rig: Rig, motion) -> list | None:
        mapping = suggest(action_bones(manifest), bones_of(rig.names, rig.parents, rig.models(rig.base_locals)),
                          dict.fromkeys(manifest["actions"][number]["bones"], 1), positions=False)
        if any(row.target != row.source for row in mapping.rows if row.mapped) and not review_mapping(
                self, self.handler.app, mapping, rig.names, motion.name, motion=True):
            return None
        return [(row.source, row.target) for row in mapping.rows if row.mapped]

    def _duplicate(self) -> None:
        if copies := edits.duplicate(self.document, list(self.state.selection)):
            self.state.select(copies)

    def cleanup(self) -> None:
        self.transport.stop()
        self.preview.scene.preview.set_fullscreen_content(None)
        self.preview.cleanup()
