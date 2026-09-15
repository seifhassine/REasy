from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

import numpy as np

from .scn_scene_graph import (
    ScnObjectId,
    ScnRenderableMesh,
    ScnSceneDocument,
    ScnSceneGraph,
    ScnSceneObject,
    compose_parented_transform,
)


@dataclass(frozen=True, slots=True)
class ScnAnimatedRigPose:
    """Animated object-local joint matrices for one instantiated scene object."""

    document_instance_id: str
    source_object_id: ScnObjectId
    joint_matrices: Mapping[str, np.ndarray]


@dataclass(frozen=True, slots=True)
class ScnJointAttachmentResult:
    matrices: dict[str, np.ndarray]


def same_joints_constraint_renderables(
    graph: ScnSceneGraph,
    owner: ScnRenderableMesh,
) -> tuple[ScnRenderableMesh, ...]:
    """Return the renderables in one authored same-joints hierarchy."""
    document = graph.documents.get(owner.source_object_id.document_id)
    source_object = (
        document.objects.get(owner.source_object_id) if document else None
    )
    transform = source_object.transform if source_object else None
    if source_object is None:
        raise ValueError(
            f"same-joints owner {owner.key!r} has no source scene object"
        )
    if transform is None:
        raise ValueError(
            f"same-joints owner {source_object.name!r} has no transform"
        )
    if transform.parent_joint:
        return (owner,)

    anchor = _same_joints_anchor(document, source_object)
    anchor_local_id = anchor.id.local_object_id
    result = [
        renderable
        for renderable in graph.renderables
        if renderable.document_instance_id == owner.document_instance_id
        and renderable.source_object_id.document_id
        == owner.source_object_id.document_id
        and same_joints_descendant_depth(
            document,
            renderable.source_object_id,
            anchor_local_id,
        )
        is not None
    ]
    if not result:
        raise ValueError(
            f"same-joints owner {source_object.name!r} is outside its "
            "resolved constraint hierarchy"
        )
    return tuple(result)


def same_joints_pose_space_matrix(
    graph: ScnSceneGraph,
    owner: ScnRenderableMesh,
) -> np.ndarray:
    """Return the scene matrix of the pose space shared by a constrained rig."""
    document = graph.documents.get(owner.source_object_id.document_id)
    source_object = (
        document.objects.get(owner.source_object_id) if document else None
    )
    if source_object is None:
        raise ValueError(
            f"same-joints owner {owner.key!r} has no source scene object"
        )
    transform = source_object.transform
    if transform is None:
        raise ValueError(
            f"same-joints owner {source_object.name!r} has no transform"
        )
    if transform.parent_joint or not transform.same_joints_constraint:
        return np.asarray(owner.world_matrix, dtype=np.float32).reshape(4, 4).copy()

    anchor = _same_joints_anchor(document, source_object)
    instance = graph.document_instances.get(owner.document_instance_id)
    if instance is None:
        raise ValueError(
            f"same-joints owner {source_object.name!r} belongs to missing "
            f"document instance {owner.document_instance_id!r}"
        )
    if instance.document_id != document.document_id:
        raise ValueError(
            f"same-joints owner {source_object.name!r} belongs to document "
            f"instance {owner.document_instance_id!r} for "
            f"{instance.document_id!r}, not {document.document_id!r}"
        )
    return (
        np.asarray(instance.base_world_matrix, dtype=np.float32).reshape(4, 4)
        @ np.asarray(anchor.document_world_matrix, dtype=np.float32).reshape(4, 4)
    ).astype(np.float32)


def _same_joints_anchor(
    document: ScnSceneDocument,
    source_object: ScnSceneObject,
) -> ScnSceneObject:
    current = source_object
    visited = set()
    while (
        current.transform is not None
        and current.transform.same_joints_constraint
        and not current.transform.parent_joint
    ):
        if current.id in visited:
            raise ValueError(
                f"same-joints hierarchy contains a cycle at {current.name!r}"
            )
        visited.add(current.id)
        parent_id = document.object_by_local_id.get(current.parent_id)
        if parent_id is None or parent_id not in document.objects:
            raise ValueError(
                f"same-joints object {current.name!r} references missing "
                f"parent {current.parent_id}"
            )
        current = document.objects[parent_id]
    if current.transform is None:
        raise ValueError(
            f"same-joints hierarchy anchor {current.name!r} has no transform"
        )
    return current


def same_joints_descendant_depth(
    document: ScnSceneDocument,
    object_id: ScnObjectId,
    ancestor_local_id: int,
) -> int | None:
    """Return depth below an ancestor when every intervening edge shares joints."""
    current = document.objects.get(object_id)
    if current is not None and current.id.local_object_id == ancestor_local_id:
        return 0
    depth = 0
    visited = set()
    while current is not None:
        if current.id in visited:
            return None
        visited.add(current.id)
        transform = current.transform
        if (
            transform is None
            or not transform.same_joints_constraint
            or transform.parent_joint
        ):
            return None
        depth += 1
        if current.parent_id == ancestor_local_id:
            return depth
        parent_id = document.object_by_local_id.get(current.parent_id)
        current = document.objects.get(parent_id) if parent_id is not None else None
    return None


def resolve_joint_attachments(
    graph: ScnSceneGraph,
    poses: Iterable[ScnAnimatedRigPose],
) -> ScnJointAttachmentResult:
    """Resolve renderables affected by via.Transform.ParentJoint relationships."""
    pose_by_object = {
        (pose.document_instance_id, pose.source_object_id): pose
        for pose in poses
    }
    if not pose_by_object:
        return ScnJointAttachmentResult({})

    worlds: dict[tuple[str, ScnObjectId], tuple[np.ndarray, bool]] = {}
    visiting: set[tuple[str, ScnObjectId]] = set()

    def resolve(
        instance_id: str,
        object_id: ScnObjectId,
    ) -> tuple[np.ndarray, bool]:
        key = instance_id, object_id
        cached = worlds.get(key)
        if cached is not None:
            return cached

        instance = graph.document_instances.get(instance_id)
        document = graph.documents.get(object_id.document_id)
        scene_object = document.objects.get(object_id) if document else None
        if instance is None or document is None or scene_object is None:
            raise ValueError("joint attachment references an unknown scene object")
        if key in visiting:
            raise ValueError(
                f"joint attachment hierarchy contains a cycle at "
                f"{scene_object.name!r}"
            )

        visiting.add(key)
        try:
            transform = scene_object.transform
            parent_id = document.object_by_local_id.get(scene_object.parent_id)
            if parent_id is None or parent_id not in document.objects:
                parent_world = instance.base_world_matrix
                parent_dynamic = False
            else:
                parent_world, parent_dynamic = resolve(
                    instance_id,
                    parent_id,
                )

            dynamic = parent_dynamic
            joint_matrix = None
            if transform is not None and transform.parent_joint:
                if parent_id is None:
                    raise ValueError(
                        f"joint-attached scene object {scene_object.name!r} "
                        "has no parent object"
                    )
                parent_pose = pose_by_object.get((instance_id, parent_id))
                if parent_pose is not None:
                    joint_matrix = parent_pose.joint_matrices.get(
                        transform.parent_joint
                    )
                    if joint_matrix is None:
                        raise ValueError(
                            f"parent joint {transform.parent_joint!r} was not "
                            f"found for scene object {scene_object.name!r}"
                        )
                    dynamic = True
                elif parent_dynamic:
                    raise ValueError(
                        f"animated parent of scene object "
                        f"{scene_object.name!r} has no rig pose for joint "
                        f"{transform.parent_joint!r}"
                    )

            if transform is None:
                world = parent_world.copy()
            else:
                try:
                    world = compose_parented_transform(
                        parent_world,
                        transform,
                        joint_matrix,
                    )
                except (ValueError, np.linalg.LinAlgError) as exc:
                    raise ValueError(
                        f"unable to compose joint attachment for "
                        f"{scene_object.name!r}: {exc}"
                    ) from exc
            if not np.isfinite(world).all():
                raise ValueError(
                    f"joint attachment for {scene_object.name!r} produced "
                    "a non-finite transform"
                )

            resolved = world, dynamic
            worlds[key] = resolved
            return resolved
        finally:
            visiting.discard(key)

    matrices: dict[str, np.ndarray] = {}
    for renderable in graph.renderables:
        key = renderable.document_instance_id, renderable.source_object_id
        object_world, dynamic = resolve(*key)
        if not dynamic:
            continue
        document = graph.documents.get(renderable.source_object_id.document_id)
        scene_object = (
            document.objects.get(renderable.source_object_id) if document else None
        )
        if scene_object is None:
            raise ValueError(
                f"joint attachment renderable {renderable.key!r} has no "
                "source scene object"
            )
        try:
            renderable_local = (
                np.linalg.inv(scene_object.document_world_matrix)
                @ renderable.document_world_matrix
            )
        except np.linalg.LinAlgError as exc:
            raise ValueError(
                f"scene object {scene_object.name!r} has a singular static "
                "attachment transform"
            ) from exc
        if not np.isfinite(renderable_local).all():
            raise ValueError(
                f"scene object {scene_object.name!r} has a non-finite static "
                "attachment transform"
            )
        matrices[renderable.key] = (
            object_world @ renderable_local
        ).astype(np.float32)

    return ScnJointAttachmentResult(matrices)
