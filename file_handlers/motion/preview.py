"""See docs/internals/motion-editor.md, Preview."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QLineEdit,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.scene.scene_features import ScnSceneFeatures

lazy from file_handlers.clip.editor.transport import Playback, Transport
lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.rsz.rsz_handler import RszHandler
lazy from file_handlers.rsz.scn_scene_graph import normalize_document_id
lazy from file_handlers.rsz.scn_scene_loader import ScnSceneSource
lazy from file_handlers.rsz.scn_scene_preview import ScnScenePreviewWidget
lazy from file_handlers.rsz.spawn_rules import holds_spawner, spawn_choice_key
lazy from file_handlers.strands.chain_catalog import strand_catalog_for
lazy from file_handlers.strands.motion import MotionStrandsDialog
lazy from settings import app_settings
lazy from ui.editor_widgets import editor_strip, search_field, styled_label, tool_button
lazy from ui.project_manager.catalogue_panel import CataloguePanel
lazy from ui.scene.scene_view import ScenePose
lazy from ui.styles import set_style_role
lazy from utils.resource_file_utils import nearby_resource_paths, resolve_handler_resource_data, resource_key

from .banks import (
    bank_references, entry_mirrored, entry_motion, entry_reversed, entry_sequences, handler_reader, list_paths,
    names_motions, read_list)
from .model import Motion, MotionList, MotionTree
from .pose import MotionSampler, Rig
from .preview_tracks import MotionPreviewTracks
from .scene_poser import ScenePoser
from .sequence_tracks import TrackPlayer
from .tree_sampler import TreeSampler, tree_variables


def read_prefab(owner, path: str, data: bytes) -> RszHandler:
    handler = RszHandler()
    handler.app, handler.filepath, handler.resource_context = owner.app, path, owner.resource_context
    handler.set_game_version(owner.app.settings.get("game_version", "RE4") if owner.app is not None else "RE4")
    handler.read(data)
    return handler


def catalogue_context(handler) -> tuple[str, object]:
    return (handler.app.settings.get("game_version", "RE4") if handler.app is not None else "RE4",
            partial(resolve_handler_resource_data, handler, allow_selection_dialog=False))


class MotionPreview(ScnSceneFeatures, QWidget, MotionPreviewTracks):
    played = Signal()

    def __init__(self, parent: QWidget | None = None, picker=None, settings: dict | None = None):
        super().__init__(parent)
        self.prefab: RszHandler | None = None
        self.lock_root = True
        self._sampler: MotionSampler | TreeSampler | None = None
        self._item: Motion | MotionTree | None = None
        self._row: tuple[bool, bool, list | None] = (False, False, None)
        self._runtime_parts: dict = {}
        self._packages: dict | None = None
        self._lists: dict[str, MotionList | None] = {}
        self.trees: dict[int, tuple[list, dict]] = {}
        self._played: tuple[tuple, int | None, str | None] = ((), None, None)
        self.list_path: str | None = None
        self._listed: tuple[tuple, int | None, str | None] = ((), None, None)
        self._frame = 0.0
        self._poser: ScenePoser | None = None
        self._tracks: TrackPlayer | None = None
        self.pins: list[tuple] = []
        self.edited: tuple[str, object] | None = None
        self._strands: MotionStrandsDialog | None = None
        self._catalogue: QFrame | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar, row = editor_strip()
        self.picker = None if picker is None else CharacterPicker(picker, bar)
        if self.picker is not None:
            self.picker.chosen.connect(self.set_prefab)
            row.addWidget(self.picker, 1)
            self.catalogue_button = tool_button("user", self.tr("Pick a character from the game's catalogs"),
                                                self.open_catalogue, text=self.tr("Catalogue"))
            row.addWidget(self.catalogue_button)
        lock = QCheckBox(self.tr("Lock root"))
        lock.setToolTip(self.tr("Keep root joints at their base position, without the motion's root motion"))
        lock.setChecked(True)
        lock.toggled.connect(self.set_lock_root)
        row.addWidget(lock)
        self.layer_box, self._layer = QComboBox(bar), None
        self.layer_box.setToolTip(self.tr("The layer of its holder's Motion the motion plays on. Game code picks it "
                                          "at run time; by default the layer set to the motion's bank, else the first"))
        self.layer_box.activated.connect(self._pick_layer)
        row.addWidget(self.layer_box)
        self.strands_button = tool_button("wand", self.tr("Strands over the posed prefab"), self.open_strands,
                                          text=self.tr("Strands"))
        self.strands_button.setEnabled(False)
        row.addWidget(self.strands_button)
        if picker is None:
            row.addStretch(1)
        layout.addWidget(bar)
        self.unlisted = styled_label(self.tr(
            "No Motion of this prefab lists this file, so its motions replace the pose here and in Blender exports: "
            "additive ones, such as face expressions, look wrong. Preview on a prefab that uses this list."), self,
            banner="warning")
        self.unlisted.setWordWrap(True)
        self.unlisted.hide()
        layout.addWidget(self.unlisted)
        self.scene = ScnScenePreviewWidget(self, sources_getter=self._sources, settings=settings,
                                           graphs_changed_callback=self._show_features)
        self.scene.constraint_materials = True
        self.scene.renderables_changed.connect(self._bind)
        layout.addWidget(self.scene, 1)
        self.scene.status_label.setContentsMargins(6, 3, 6, 3)
        set_style_role(self.scene.status_label, tone="muted")
        layout.addWidget(self.scene.status_label)
        self._build_features_panel()
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.timeout.connect(self.scene.ensure_loaded)

    @property
    def preview(self) -> ScnScenePreviewWidget:
        return self.scene

    def set_prefab(self, prefab: RszHandler | None) -> None:
        self.prefab, self._poser, self.pins = prefab, None, []
        self.strands_button.setEnabled(
            prefab is not None and strand_catalog_for(mmtr_profile_for_game(prefab.game_version)) is not None)
        self.scene.preview.set_shading("game")
        self.scene.request_refresh()

    def set_motion(self, motion: Motion | MotionTree | None, holders: tuple = (), bank: int | None = None,
                   path: str | None = None, mirrored: bool = False, reverse: bool = False,
                   sequences: list | None = None, mask: tuple | None = None) -> None:
        self._item, self._row = motion, (mirrored, reverse, sequences, mask)
        if isinstance(motion, MotionTree):
            self._sampler = self._tree_sampler(motion, holders or self._listed[0])
        else:
            self._sampler = None if motion is None else MotionSampler(
                motion, self._sampler if isinstance(self._sampler, MotionSampler) else None)
        self._played = holders, bank, path
        layers = () if self._poser is None else self._poser.layers((holders or self._listed[0] or (None,))[0])
        self._layer = self._layer if self._layer is not None and self._layer < len(layers) else None
        self.layer_box.clear()
        self.layer_box.addItems([self.tr("Bank's layer"), *(self.tr("Layer {0}").format(at) for at in range(len(
            layers)))])
        self.layer_box.setCurrentIndex(0 if self._layer is None else self._layer + 1)
        runtime = None if holders or self._listed[0] or self._poser is None else self._poser.runtime_holder(
            self._sampler)
        self._tracks = self._track_player(motion, holders or self._listed[0] or (() if runtime is None else (runtime,)))
        changes = {} if self._tracks is None else self._tracks.changes()
        for holder, at, pinned, motion_id in self.pins:
            changes.setdefault(holder, {}).setdefault(at, [(0.0, pinned, motion_id, 0.0, 0, 0)])
        if self._poser is not None:
            self._poser.bind(self._sampler, *(self._played if holders else self._listed), mirrored, reverse, changes,
                             *((None, None) if self._tracks is None else (self._tracks.joints, self._tracks.masks())),
                             self._layer, mask, None if self._tracks is None else self._tracks.variables)
        self._show(everything=True)
        self.played.emit()

    def _pick_layer(self, index: int) -> None:
        self._layer = None if index <= 0 else index - 1
        self.set_motion(self._item, *self._played, *self._row)

    def pin(self, motion_id: int) -> None:
        holders, bank, _path = self._played
        if not holders or self._poser is None:
            return
        at = self._played_layer(holders[0])
        self.pins = [pin for pin in self.pins if pin[:2] != (holders[0], at)] + [(holders[0], at, bank, motion_id)]
        self.set_motion(self._item, *self._played, *self._row)

    def _played_layer(self, holder) -> int:
        layers = () if self._poser is None else self._poser.layers(holder)
        return self._layer if self._layer is not None else next(
            (at for at, layer in enumerate(layers) if layer.bank == self._played[1]), 0)

    def unpin(self) -> None:
        self.pins = []
        self.set_motion(self._item, *self._played, *self._row)

    def _tree_sampler(self, tree: MotionTree, holders: tuple) -> TreeSampler | None:
        if self._poser is None or self.prefab is None:
            return None
        if id(tree) not in self.trees:
            rows = tree_variables(handler_reader(self.prefab), tree)
            self.trees[id(tree)] = rows, {guid: value for guid, _name, value in rows}
        return TreeSampler(tree, partial(self._poser.motion, holders), self.trees[id(tree)][1],
                           mirror_of=partial(self._poser.mirrored, holders),
                           reverse_of=partial(self._poser.reversed, holders),
                           mask_of=partial(self._poser.masked, holders))

    def tree_variables(self) -> tuple[list[tuple[bytes, str, object]], dict] | None:
        return self.trees.get(id(self._item)) if isinstance(self._item, MotionTree) else None

    def refresh_tree(self) -> None:
        if isinstance(self._sampler, TreeSampler):
            self._sampler.refresh()
            self._show()

    @property
    def sampler(self) -> MotionSampler | TreeSampler | None:
        return self._sampler

    def set_frame(self, frame: float) -> None:
        self._frame = frame
        self._show()

    def set_lock_root(self, locked: bool) -> None:
        self.lock_root = locked
        self._show()

    def rig(self) -> Rig | None:
        rigs = [] if self._poser is None else self._poser.rigs
        keyed = set() if self._sampler is None else set(self._sampler.hashes)
        return next((entry.rig for entry in rigs if entry.binding is not None), None) or max(
            (entry.rig for entry in rigs if entry.owner is None), key=lambda rig: len(keyed.intersection(rig.hashes)),
            default=None)

    def adds(self) -> bool:
        rigs = [] if self._poser is None else self._poser.rigs
        return next((entry.layer.add for entry in rigs if entry.binding is not None), False)

    def open_strands(self) -> None:
        if self._strands is None:
            self._strands = MotionStrandsDialog(self, self.prefab)
        self._strands.show()
        self._strands.raise_()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._load_timer.start()

    def cleanup(self) -> None:
        if self._strands is not None:
            self._strands.cleanup()
        self.scene.cleanup()
        self.prefab = self._sampler = self._strands = self._poser = None

    def _sources(self) -> list[ScnSceneSource]:
        if self.prefab is None:
            return []
        return [ScnSceneSource(self.prefab.filepath, self.prefab, Path(self.prefab.filepath).name,
                               game_version=self.prefab.game_version)]

    def show_spawned(self, prefab: RszHandler, instance: int | None, picks) -> None:
        if prefab is not self.prefab:
            self.set_prefab(prefab)
            if self.picker is not None:
                self.picker.set_path(prefab.filepath)
        key = partial(spawn_choice_key, normalize_document_id(prefab.filepath), instance)
        kept = {name: pick for name, pick in self.scene.spawn_choices.items() if not name.startswith(key(""))}
        self.scene.set_spawn_choices(kept | {key(level): pick for level, pick in enumerate(picks)})

    def open_catalogue(self) -> None:
        if self._catalogue is None:
            self._catalogue = QFrame(self, Qt.Popup)
            self._catalogue.setFrameShape(QFrame.StyledPanel)
            panel = CataloguePanel(partial(catalogue_context, self.picker.handler), self._catalogue)
            panel.open_requested.connect(self._open_catalogued)
            column = QVBoxLayout(self._catalogue)
            column.setContentsMargins(0, 0, 0, 0)
            column.addWidget(panel)
            self._catalogue.resize(380, 460)
        self._catalogue.move(self.catalogue_button.mapToGlobal(self.catalogue_button.rect().bottomLeft()))
        self._catalogue.show()

    def _open_catalogued(self, path: str, instance: int | None, picks) -> None:
        self._catalogue.hide()
        if (hit := resolve_handler_resource_data(self.picker.handler, path, self)) is not None:
            self.show_spawned(read_prefab(self.picker.handler, *hit), instance, picks)

    @property
    def poser(self) -> ScenePoser | None:
        return self._poser

    def rebind(self) -> None:
        if self._poser is not None:
            self._bind()

    def _bind(self) -> None:
        read = None if self.prefab is None else handler_reader(self.prefab)
        self._runtime_parts = {}
        self._poser = ScenePoser(self.scene.graphs, self.scene.loaded_meshes(),
                                 "" if self.prefab is None else self.prefab.game_version, self.edited, read)
        key = None if self.list_path is None or read is None else resource_key(self.list_path)
        references = [] if key is None else bank_references(self.scene.graphs)
        self._listed = next(((reference.holders, bank, reference.path) for reference in references
                             for bank, listed in list_paths(read, reference.path) if resource_key(listed) == key),
                            ((), None, None))
        self.unlisted.setVisible(key is not None and not self._listed[0])
        self.set_motion(self._item, *self._played, *self._row)

    def _show(self, everything: bool = False) -> None:
        if self._poser is None:
            return
        preview = self.scene.preview
        if self._sampler is None:
            for skinned in self._poser.skinned if everything else ():
                preview.set_mesh_pose(skinned.key, None if skinned.start is None else ScenePose(skinned.start))
            self.scene.set_mesh_materials({}, {})
            self.scene.set_track_state({}, {})
            preview.set_time_buffers({})
            preview.set_strands_gravity(None)
            return
        self._poser.attach_materials(self.scene.material_surfaces)
        state = None if self._tracks is None else self._tracks.at(self._frame)
        for key, bones, morphs in self._poser.poses(
                self._sampler, self._frame, self.lock_root, everything,
                *((None, None) if state is None else (state.scales, state.shapes))):
            preview.set_mesh_pose(key, ScenePose(bones, morphs))
        materials = self._poser.material_values()
        self.scene.set_mesh_materials(*(({}, {}) if state is None else (state.meshes, state.flags)))
        self._track_materials(materials, {} if state is None else state.materials)
        self.scene.set_pose_materials(materials)
        self.scene.set_track_state(*(({}, {}) if state is None else (state.drawn, state.parts)))
        preview.set_time_buffers({} if state is None else state.buffers)
        preview.set_strands_gravity(None if state is None else state.gravity)


class CharacterPicker(QComboBox):
    chosen = Signal(object)

    def __init__(self, handler, parent: QWidget | None = None):
        super().__init__(parent)
        self.handler = handler
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMinimumContentsLength(24)
        self.lineEdit().setPlaceholderText(self.tr("Prefab, character file or scene to preview on"))
        self.addItems(nearby_resource_paths(handler, "pfb", parent_levels=2, child_levels=2))
        self.setCurrentIndex(-1)
        self._path = ""
        self.activated.connect(self._load)
        self.lineEdit().returnPressed.connect(self._load)

    def set_path(self, path: str) -> None:
        self._path = path
        self.setEditText(path)

    def _load(self, *_args) -> None:
        path = self.currentText().strip()
        if not path or path == self._path:
            return
        self._path = path
        if (hit := resolve_handler_resource_data(self.handler, path, self)) is not None:
            self.chosen.emit(read_prefab(self.handler, *hit))


class CharacterMotions(QSplitter):
    def __init__(self, handler, parent: QWidget | None = None, own: bool = True):
        super().__init__(Qt.Horizontal, parent)
        self.handler = handler
        self.playback = Playback(self)
        browser = QWidget(self)
        column = QVBoxLayout(browser)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        bar, row = editor_strip()
        self.search = search_field(QLineEdit())
        self.search.setPlaceholderText(self.tr("Filter opened lists"))
        self.search.textChanged.connect(self._filter)
        row.addWidget(self.search, 1)
        self.pin_button = tool_button("layers", self.tr(
            "Keep this motion playing on its layer while you pick another for a different layer"), self._pin,
            text=self.tr("Pin"))
        self.unpin_button = tool_button("close", self.tr("Stop the pinned motions"), self._unpin, text=self.tr("Unpin"))
        self.unpin_button.setEnabled(False)
        row.addWidget(self.pin_button)
        row.addWidget(self.unpin_button)
        column.addWidget(bar)
        self.motions = QTreeWidget(browser)
        self.motions.setHeaderLabels([self.tr("Motion"), self.tr("Frames")])
        self.motions.itemExpanded.connect(self._open_list)
        self.motions.currentItemChanged.connect(self._chosen)
        column.addWidget(self.motions, 1)
        self.addWidget(browser)
        shown = QWidget(self)
        layout = QVBoxLayout(shown)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.preview = MotionPreview(shown, picker=handler, settings=app_settings(handler.app))
        if own:
            self.preview.set_prefab(handler)
            self.preview.picker.set_path(handler.filepath)
        self.preview.scene.renderables_changed.connect(self._list_banks)
        layout.addWidget(self.preview, 1)
        self.transport = Transport(self.playback, shown, self.preview.scene.preview)
        layout.addWidget(self.transport)
        self.preview.scene.preview.set_fullscreen_content(shown)
        self.addWidget(shown)
        self.setStretchFactor(1, 1)
        self.playback.frame_changed.connect(self.preview.set_frame)
        self._lists: dict = {}
        self._listed = None

    def pick_spawned(self, instance: int | None, picks) -> None:
        self.preview.show_spawned(self.handler, instance, picks)

    def _list_banks(self) -> None:
        self.unpin_button.setEnabled(bool(self.preview.pins))
        top = self.motions.topLevelItem(0)
        if self._listed is self.preview.prefab and top is not None and top.data(0, Qt.UserRole) is not None:
            return
        self._listed = self.preview.prefab
        self.motions.clear()
        for reference in bank_references(self.preview.scene.graphs):
            for bank_id, path in list_paths(handler_reader(self.handler), reference.path):
                item = QTreeWidgetItem([f"{reference.owner} · {bank_id} · {Path(path).name}", ""])
                item.setToolTip(0, path)
                item.setData(0, Qt.UserRole, path)
                item.setData(0, Qt.UserRole + 1, (reference.holders, bank_id, reference.path))
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                self.motions.addTopLevelItem(item)
        if not self.motions.topLevelItemCount():
            self.motions.addTopLevelItem(QTreeWidgetItem([self.tr("No motion banks found"), ""]))

    def _open_list(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, Qt.UserRole)
        if item.childCount() or not isinstance(path, str):
            return
        read = handler_reader(self.handler)
        motion_list = read_list(read, path)
        for entry in () if motion_list is None else motion_list.entries:
            if (motion := entry_motion(read, motion_list, entry, self._lists)) is not None:
                row = (entry_mirrored(read, motion_list, entry, self._lists),
                       entry_reversed(read, motion_list, entry, self._lists),
                       entry_sequences(read, motion_list, entry, self._lists),
                       (entry.joint_mask, motion_list.joint_mask_mode))
                child = QTreeWidgetItem([f"{entry.motion_id}  {motion.name}" + "".join(
                    "  " + text for text, shown in ((self.tr("(mirrored)"), row[0]), (self.tr("(reversed)"), row[1]))
                    if shown), f"{motion.end_frame:g}"])
                child.setData(0, Qt.UserRole, motion)
                child.setData(0, Qt.UserRole + 1, row)
                child.setData(0, Qt.UserRole + 2, entry.motion_id)
                if motion is not entry.item:
                    child.setToolTip(0, self.tr("Not held by this list: from its base list or a referenced file"))
                item.addChild(child)
        item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicatorWhenChildless)
        self._filter(self.search.text())

    def _filter(self, text: str) -> None:
        needle = text.casefold()
        for index in range(self.motions.topLevelItemCount()):
            top = self.motions.topLevelItem(index)
            for row in range(top.childCount()):
                child = top.child(row)
                child.setHidden(bool(needle) and needle not in child.text(0).casefold())

    def _chosen(self, item: QTreeWidgetItem | None, _previous) -> None:
        motion = None if item is None else item.data(0, Qt.UserRole)
        if isinstance(motion, Motion):
            self.transport.rate_spin.setValue(motion.fps or 60)
            self.playback.set_end_frame(motion.end_frame)
            self.playback.set_frame(0.0)
            self.preview.set_motion(motion, *item.parent().data(0, Qt.UserRole + 1), *item.data(0, Qt.UserRole + 1))

    def _pin(self) -> None:
        item = self.motions.currentItem()
        if item is not None and isinstance(item.data(0, Qt.UserRole), Motion):
            self.preview.pin(item.data(0, Qt.UserRole + 2))
            self.unpin_button.setEnabled(bool(self.preview.pins))

    def _unpin(self) -> None:
        self.preview.unpin()
        self.unpin_button.setEnabled(False)

    def cleanup(self) -> None:
        self.transport.stop()
        self.preview.cleanup()


def create_motion_preview(handler) -> CharacterMotions | None:
    rsz_file = handler.rsz_file
    return CharacterMotions(handler) if rsz_file.is_pfb and names_motions(rsz_file) or holds_spawner(rsz_file) else None
