"""See docs/internals/motion-editor.md, Preview."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

lazy import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLineEdit,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

lazy from file_handlers.clip.editor.transport import Playback, Transport
lazy from file_handlers.engine_math import transform_product
lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.rsz.rsz_handler import RszHandler
lazy from file_handlers.rsz.scn_scene_graph import mesh_joints
lazy from file_handlers.rsz.scn_scene_loader import ScnSceneSource
lazy from file_handlers.rsz.scn_scene_preview import ScnScenePreviewWidget
lazy from file_handlers.strands.chain_catalog import strand_catalog_for
lazy from file_handlers.strands.motion import MotionStrandsDialog
lazy from settings import app_settings
lazy from ui.editor_widgets import editor_strip, search_field, tool_button
lazy from ui.scene.scene_view import ScenePose
lazy from ui.styles import set_style_role
lazy from utils.hash_util import murmur3_hash_utf16le
lazy from utils.resource_file_utils import nearby_resource_paths, resolve_handler_resource_data

from .banks import bank_references, entry_motion, list_paths, names_motions, read_list
from .model import Motion
from .pose import MotionSampler, Rig


@dataclass(slots=True)
class _Rig:
    rig: Rig
    owner: int | None
    # Rows following the owner's joints, and the owner's rows they follow.
    follows: tuple[np.ndarray, np.ndarray]
    binding: tuple[np.ndarray, np.ndarray] | None = None
    moves: bool = False
    rest: np.ndarray | None = None


@dataclass(slots=True)
class _Skinned:
    key: str
    rig: int
    joints: np.ndarray
    valid: np.ndarray
    inverse_binds: np.ndarray
    morphs: tuple[tuple[str, int], ...]
    start: np.ndarray | None
    moves: bool = False


def read_prefab(owner, path: str, data: bytes) -> RszHandler:
    handler = RszHandler()
    handler.app, handler.filepath, handler.resource_context = owner.app, path, owner.resource_context
    handler.set_game_version(owner.app.settings.get("game_version", "RE4") if owner.app is not None else "RE4")
    handler.read(data)
    return handler


class ScenePoser:
    def __init__(self, graphs, loaded_meshes):
        loaded = {mesh.key: mesh for mesh in loaded_meshes}
        meshes = {}
        for graph in graphs:
            for renderable in graph.renderables:
                if renderable.key in loaded:
                    meshes.setdefault(renderable.source_object_id, getattr(loaded[renderable.key].handler, "mesh", None))
        built: dict[int, object] = {}
        rows: dict[int, int] = {}
        self.rigs: list[_Rig] = []
        self.skinned: list[_Skinned] = []

        def joints_of(document, item):
            if (found := document.joints.get(item.id)) is not None or (mesh := meshes.get(item.id)) is None:
                return found
            if id(mesh) not in built:
                built[id(mesh)] = mesh_joints(mesh)
            return built[id(mesh)]

        def rig_of(document, item) -> int | None:
            joints = joints_of(document, item)
            if joints is None:
                return None
            if id(joints) not in rows:
                owner = None
                if item.transform is not None and item.transform.same_joints_constraint:
                    parent = document.objects.get(document.object_by_local_id.get(item.parent_id))
                    owner = None if parent is None else rig_of(document, parent)
                rig = Rig.of_joints(joints)
                followed = {} if owner is None else {value: row for row, value in enumerate(self.rigs[owner].rig.hashes)}
                pairs = [(row, followed[value]) for row, value in enumerate(rig.hashes) if value in followed]
                rows[id(joints)] = len(self.rigs)
                self.rigs.append(_Rig(rig, owner, (np.array([row for row, _ in pairs], np.int64),
                                                   np.array([row for _, row in pairs], np.int64))))
            return rows[id(joints)]

        for graph in graphs:
            for renderable in graph.renderables:
                document = graph.documents.get(renderable.source_object_id.document_id)
                item = None if document is None else document.objects.get(renderable.source_object_id)
                if item is None or renderable.key not in loaded:
                    continue
                joints = joints_of(document, item)
                if joints is None or joints.inverse_binds is None or not joints.remap:
                    continue
                index = rig_of(document, item)
                remap = np.array(joints.remap, np.int64)
                valid = (remap >= 0) & (remap < len(joints.names))
                remap = np.where(valid, remap, 0)
                inverse = np.asarray(joints.inverse_binds, np.float32)[remap].transpose(0, 2, 1)
                self.skinned.append(_Skinned(renderable.key, index, remap, valid, inverse,
                                             _morphs(loaded[renderable.key].handler), renderable.start_pose))

    def bind(self, sampler: MotionSampler | None) -> None:
        for entry in self.rigs:
            entry.binding = None if sampler is None or entry.owner is not None else entry.rig.bind(sampler.hashes)
            entry.moves = bool(len(entry.binding[0])) if entry.binding is not None else (
                entry.owner is not None and self.rigs[entry.owner].moves)
        properties = () if sampler is None else sampler.properties
        for skinned in self.skinned:
            skinned.moves = self.rigs[skinned.rig].moves or any(value in properties for _name, value in skinned.morphs)

    def poses(self, sampler: MotionSampler | None, frame: float, lock_root: bool, everything: bool = False):
        """(mesh key, skin matrices 3 x 4 in bone remap order, blend-shape weights by name) per skinned mesh the
        motion moves, or per skinned mesh with ``everything``."""
        shown = [skinned for skinned in self.skinned if everything or skinned.moves]
        if not shown:
            return
        sample = None if sampler is None else sampler.sample(frame)
        values = {} if sampler is None else sampler.property_values(frame)
        models: list[np.ndarray | None] = [None] * len(self.rigs)
        identity = np.eye(3, 4, dtype=np.float32)
        for skinned in shown:
            bones = transform_product(self._models(skinned.rig, sample, models, lock_root)[skinned.joints],
                                      skinned.inverse_binds)[:, :3]
            bones[~skinned.valid] = identity
            yield skinned.key, np.ascontiguousarray(bones), tuple(
                (name, values[value]) for name, value in skinned.morphs if value in values)

    def _models(self, index: int, sample, models: list, lock_root: bool) -> np.ndarray:
        if models[index] is None:
            entry = self.rigs[index]
            rig = entry.rig
            if not entry.moves or sample is None:
                if entry.rest is None:
                    entry.rest = rig.models(rig.base_locals) if entry.owner is None else rig.models(
                        rig.base_locals, (entry.follows[0], self._models(entry.owner, None, models, lock_root)[
                            entry.follows[1]]))
                models[index] = entry.rest
            elif entry.owner is None:
                rows, slots = entry.binding
                keyed = sample[1][0][slots] | sample[1][1][slots] | sample[1][2][slots]
                models[index] = rig.models(rig.locals(rig.posed(sample, entry.binding, lock_root), rows[keyed]))
            else:
                rows, followed = entry.follows
                models[index] = rig.models(rig.base_locals, (rows, self._models(entry.owner, sample, models,
                                                                                 lock_root)[followed]))
        return models[index]


class MotionPreview(QWidget):
    """``picker``: the file's handler, to choose a prefab near the file."""

    def __init__(self, parent: QWidget | None = None, picker=None, settings: dict | None = None):
        super().__init__(parent)
        self.prefab: RszHandler | None = None
        self.lock_root = True
        self._sampler: MotionSampler | None = None
        self._frame = 0.0
        self._poser: ScenePoser | None = None
        self._strands: MotionStrandsDialog | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar, row = editor_strip()
        if picker is not None:
            chooser = PrefabPicker(picker, bar)
            chooser.chosen.connect(self.set_prefab)
            row.addWidget(chooser, 1)
        lock = QCheckBox(self.tr("Lock root"))
        lock.setToolTip(self.tr("Keep root joints at their base position, without the motion's root motion"))
        lock.setChecked(True)
        lock.toggled.connect(self.set_lock_root)
        row.addWidget(lock)
        self.strands_button = tool_button("wand", self.tr("Strands over the posed prefab"), self.open_strands,
                                          text=self.tr("Strands"))
        self.strands_button.setEnabled(False)
        row.addWidget(self.strands_button)
        if picker is None:
            row.addStretch(1)
        layout.addWidget(bar)
        self.scene = ScnScenePreviewWidget(self, sources_getter=self._sources, settings=settings)
        self.scene.renderables_changed.connect(self._bind)
        layout.addWidget(self.scene, 1)
        self.scene.status_label.setContentsMargins(6, 3, 6, 3)
        set_style_role(self.scene.status_label, tone="muted")
        layout.addWidget(self.scene.status_label)
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.timeout.connect(self.scene.ensure_loaded)

    def set_prefab(self, prefab: RszHandler | None) -> None:
        self.prefab, self._poser = prefab, None
        self.strands_button.setEnabled(
            prefab is not None and strand_catalog_for(mmtr_profile_for_game(prefab.game_version)) is not None)
        self.scene.preview.set_shading("game")
        self.scene.request_refresh()

    def set_motion(self, motion: Motion | None) -> None:
        self._sampler = None if motion is None else MotionSampler(motion, self._sampler)
        if self._poser is not None:
            self._poser.bind(self._sampler)
        self._show(everything=True)

    def set_frame(self, frame: float) -> None:
        self._frame = frame
        self._show()

    def set_lock_root(self, locked: bool) -> None:
        self.lock_root = locked
        self._show()

    def rig(self) -> Rig | None:
        owners = [] if self._poser is None else [entry.rig for entry in self._poser.rigs if entry.owner is None]
        keyed = set() if self._sampler is None else set(self._sampler.hashes)
        return max(owners, key=lambda rig: len(keyed.intersection(rig.hashes)), default=None)

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
        return [ScnSceneSource(self.prefab.filepath, self.prefab, Path(self.prefab.filepath).name)]

    def _bind(self) -> None:
        self._poser = ScenePoser(self.scene.graphs, self.scene.loaded_meshes())
        self.set_motion(None if self._sampler is None else self._sampler.motion)

    def _show(self, everything: bool = False) -> None:
        if self._poser is None:
            return
        preview = self.scene.preview
        if self._sampler is None:
            for skinned in self._poser.skinned if everything else ():
                preview.set_mesh_pose(skinned.key, None if skinned.start is None else ScenePose(skinned.start))
            return
        for key, bones, morphs in self._poser.poses(self._sampler, self._frame, self.lock_root, everything):
            preview.set_mesh_pose(key, ScenePose(bones, morphs))


def _morphs(handler) -> tuple[tuple[str, int], ...]:
    blend = getattr(getattr(handler, "mesh", None), "blend_shape_data", None)
    names = {channel.name for body in (blend.bodies if blend is not None else ()) for channel in body.channels
             if channel.name}
    return tuple((name, murmur3_hash_utf16le(name)) for name in sorted(names))


class PrefabPicker(QComboBox):
    """``chosen`` gives the prefab read."""
    chosen = Signal(object)

    def __init__(self, handler, parent: QWidget | None = None):
        super().__init__(parent)
        self.handler = handler
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMinimumContentsLength(24)
        self.lineEdit().setPlaceholderText(self.tr("Prefab to preview on (.pfb path)"))
        self.addItems(nearby_resource_paths(handler, "pfb", parent_levels=2, child_levels=2))
        self.setCurrentIndex(-1)
        self._path = ""
        self.activated.connect(self._load)
        self.lineEdit().returnPressed.connect(self._load)

    def _load(self, *_args) -> None:
        path = self.currentText().strip()
        if not path or path == self._path:
            return
        self._path = path
        if (hit := resolve_handler_resource_data(self.handler, path, self)) is not None:
            self.chosen.emit(read_prefab(self.handler, *hit))


class PrefabMotions(QSplitter):
    def __init__(self, handler, parent: QWidget | None = None):
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
        self.preview = MotionPreview(shown, settings=app_settings(handler.app))
        self.preview.set_prefab(handler)
        self.preview.scene.renderables_changed.connect(self._list_banks)
        layout.addWidget(self.preview, 1)
        self.transport = Transport(self.playback, shown, self.preview.scene.preview)
        layout.addWidget(self.transport)
        self.preview.scene.preview.set_fullscreen_content(shown)
        self.addWidget(shown)
        self.setStretchFactor(1, 1)
        self.playback.frame_changed.connect(self.preview.set_frame)
        self._lists: dict = {}

    def _list_banks(self) -> None:
        if self.motions.topLevelItemCount() and self.motions.topLevelItem(0).data(0, Qt.UserRole) is not None:
            return
        self.motions.clear()
        for reference in bank_references(self.preview.scene.graphs):
            for bank_id, path in list_paths(self.handler, reference.path):
                item = QTreeWidgetItem([f"{reference.owner} · {bank_id} · {Path(path).name}", ""])
                item.setToolTip(0, path)
                item.setData(0, Qt.UserRole, path)
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                self.motions.addTopLevelItem(item)
        if not self.motions.topLevelItemCount():
            self.motions.addTopLevelItem(QTreeWidgetItem([self.tr("No motion banks found"), ""]))

    def _open_list(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, Qt.UserRole)
        if item.childCount() or not isinstance(path, str):
            return
        motion_list = read_list(self.handler, path)
        for entry in () if motion_list is None else motion_list.entries:
            if (motion := entry_motion(self.handler, motion_list, entry, self._lists)) is not None:
                child = QTreeWidgetItem([f"{entry.motion_id}  {motion.name}", f"{motion.end_frame:g}"])
                child.setData(0, Qt.UserRole, motion)
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
            self.preview.set_motion(motion)

    def cleanup(self) -> None:
        self.transport.stop()
        self.preview.cleanup()


def create_prefab_motion_preview(handler) -> PrefabMotions | None:
    return PrefabMotions(handler) if handler.rsz_file.is_pfb and names_motions(handler.rsz_file) else None
