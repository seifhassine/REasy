from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from file_handlers.rsz.scn_scene_attachments import (
    ScnAnimatedRigPose,
    ScnJointAttachmentResult,
    resolve_joint_attachments,
)
from file_handlers.rsz.scn_scene_graph import (
    ScnObjectId,
    ScnRenderableMesh,
    ScnSceneGraph,
)

from ..evaluation.model import Rig
from .model import MotionPreviewSnapshot


class MotionSceneAttachmentResolver:
    """Bridge row-vector evaluated poses to the generic SCN attachment resolver."""

    def __init__(
        self,
        graphs: Iterable[ScnSceneGraph],
        target: ScnRenderableMesh,
        rig: Rig,
        *,
        pose_source_object_id: ScnObjectId | None = None,
    ):
        self._graph = next(
            (
                graph
                for graph in graphs
                if any(
                    renderable is target or renderable.key == target.key
                    for renderable in getattr(graph, "renderables", ())
                )
            ),
            None,
        )
        if self._graph is None:
            raise ValueError(
                "motion attachment target is not part of a loaded scene graph"
            )
        self._target = target
        self._pose_source_object_id = (
            pose_source_object_id or target.source_object_id
        )
        self._rig = rig
        self._joint_names = tuple(joint.name for joint in rig.joints)
        self._joint_indices: dict[str, int] = {}
        for index, name in enumerate(self._joint_names):
            self._joint_indices.setdefault(name, index)
        if len(self._joint_indices) != len(self._joint_names):
            raise ValueError(
                "motion attachment target rig contains duplicate joint names"
            )
        self._root_by_joint = rig.root_indices

    def resolve(
        self,
        snapshot: MotionPreviewSnapshot,
    ) -> ScnJointAttachmentResult:
        if snapshot.joint_names != self._joint_names:
            raise ValueError("motion snapshot does not match the attachment target rig")

        matrices = np.asarray(
            snapshot.pose.world_matrices,
            dtype=np.float32,
        ).reshape(-1, 4, 4).copy()
        if (
            len(matrices) != len(self._rig.joints)
            or not np.isfinite(matrices).all()
        ):
            raise ValueError("motion snapshot has invalid joint world matrices")

        root_deltas = dict(snapshot.root_deltas)
        for joint_index, root_index in enumerate(self._root_by_joint):
            delta = root_deltas.get(root_index)
            if delta is not None:
                matrices[joint_index, 3, :3] -= np.asarray(
                    delta,
                    dtype=np.float32,
                )

        column_matrices = matrices.transpose(0, 2, 1)
        pose = ScnAnimatedRigPose(
            self._target.document_instance_id,
            self._pose_source_object_id,
            {
                name: column_matrices[index]
                for name, index in self._joint_indices.items()
            },
        )
        return resolve_joint_attachments(self._graph, (pose,))
