"""See docs/internals/skeleton-editor.md, Preview."""
from __future__ import annotations

import time
from dataclasses import replace
from functools import partial

lazy import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHeaderView, QVBoxLayout, QWidget

lazy from file_handlers.motion.preview import CharacterMotions
lazy from file_handlers.rsz.scene_joints import skeleton_joint_descs
lazy from file_handlers.rsz.scn_scene_graph import ScnJoints, instance_world
lazy from ui.editor_widgets import styled_label
lazy from ui.scene.scene_model import SceneDrawMesh
lazy from ui.scene.scene_view import ScenePose
lazy from utils.resource_file_utils import resource_key

from .pose import rules
from .skeleton_file import write_skeleton
from .viewport import FACES, bones, octahedra, widest

_SOLID = (0.56, 0.6, 0.66)


def solid(document) -> list[SceneDrawMesh]:
    joints, rows = document.joints, document.skeleton.rows()
    points = document.worlds()[:, :3, 3].astype(np.float64)
    reach = np.array([[0.0, widest(points), 0.0]])
    corners = np.concatenate((
        bones(points, np.array([rows[id(joint.parent)] for joint in joints]), np.arange(len(joints))),
        octahedra(points - reach, points + reach, 0.5, np.full((len(joints), 1), reach[0, 1]))))
    return [SceneDrawMesh("skeleton", corners.reshape(-1, 3).astype(np.float32), (
        FACES + 6 * np.arange(len(corners))[:, None, None]).reshape(-1).astype(np.uint32), _SOLID)]


class SkeletonPreview(QWidget):

    placed = Signal()

    def __init__(self, handler, parent: QWidget | None = None):
        super().__init__(parent)
        self.document, self.key, self.place = handler.document, resource_key(handler.filepath), np.eye(4)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.hint = styled_label(self.tr(
            "Pick a prefab or character whose CustomSkeleton or DummySkeleton names this file: its meshes then follow "
            "the joints as you edit them."), self, tone="muted")
        self.unused = styled_label("", self, banner="warning")
        for label in (self.hint, self.unused):
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignCenter)
            label.setContentsMargins(8, 4, 8, 4)
            layout.addWidget(label)
        self.unused.hide()
        self.motions = CharacterMotions(handler, self, own=False)
        # The motion list, which the editor shows under its joint outline.
        self.browser = self.motions.widget(0)
        header = self.motions.motions.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.shown, self.scene = self.motions.preview, self.motions.preview.scene
        self.scene.loader.edited[self.key] = partial(write_skeleton, self.document.skeleton)
        self.scene.idle_scene = partial(solid, self.document)
        self._took = 0.0
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.timeout.connect(self.refresh)
        self.shown.picker.chosen.connect(self.hint.hide)
        self.shown.played.connect(self._pose)
        self.scene.renderables_changed.connect(self._placed)
        layout.addWidget(self.motions, 1)

    def viewport(self):
        return self.scene.preview

    def resting(self) -> bool:
        return self.shown.sampler is None

    def rest(self) -> None:
        self.motions.transport.stop()
        self.motions.motions.setCurrentItem(None)
        self.shown.set_motion(None)

    def follow(self) -> None:
        rule = rules()["follow"]
        self._pending.start(0 if self._took < rule["live"] else round(rule["pause"] * 1000))

    def refresh(self) -> None:
        started = time.perf_counter()
        if not self.scene.loaded_meshes():
            self.viewport().set_scene(solid(self.document), reset_camera=False)
        made = None
        for document, item, _instance in self._users():
            if made is None:
                *descs, symmetry = skeleton_joint_descs(write_skeleton(self.document.skeleton))
                made = replace(ScnJoints.of_descs(*descs, symmetry=symmetry), source=document.joints[item].source)
            document.joints[item] = made
        if made is not None:
            self.shown.rebind()
        self._took = time.perf_counter() - started

    def cleanup(self) -> None:
        self.motions.cleanup()

    def _users(self) -> list[tuple]:
        return [(graph.documents[instance.document_id], item, instance)
                for graph in self.scene.graphs for instance in graph.document_instances.values()
                for item, joints in graph.documents[instance.document_id].joints.items()
                if joints is not None and resource_key(joints.source) == self.key]

    def _placed(self) -> None:
        users = self._users()
        read = sorted({joints.source for graph in self.scene.graphs for document in graph.documents.values()
                       for joints in document.joints.values() if joints is not None and joints.source})
        self.unused.setText(self.tr(
            "No object shown takes its joints from this file, so the meshes do not follow your edits. The objects "
            "shown read {0}.").format(", ".join(read)) if read else self.tr(
            "No object shown takes its joints from this file, so the meshes do not follow your edits. An object takes "
            "them from a DummySkeleton, or a CustomSkeleton with Auto Apply, that names the file."))
        self.unused.setVisible(bool(self.scene.graphs) and not users)
        self.place = np.eye(4) if not users else instance_world(
            users[0][2], users[0][0].objects[users[0][1]]).astype(np.float64)
        self.placed.emit()

    def _pose(self) -> None:
        if self.resting() and self.shown.poser is not None and self._users():
            for key, bones_, morphs in self.shown.poser.poses(None, 0.0, True, True):
                self.viewport().set_mesh_pose(key, ScenePose(bones_, morphs))
