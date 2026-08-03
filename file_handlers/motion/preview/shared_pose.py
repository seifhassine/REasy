from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

from file_handlers.rsz.scn_scene_graph import (
    ScnRenderableMesh,
    ScnSceneGraph,
    normalize_document_id,
)
from file_handlers.rsz.scn_scene_attachments import (
    same_joints_descendant_depth,
)
from utils.resource_file_utils import resource_path_key

from ..runtime import MotionTargetDefinition
from .entity_session import EntityMotionSession


def resolve_motion_target_renderable(
    session: EntityMotionSession | None,
    graph: ScnSceneGraph,
    renderables: Sequence[ScnRenderableMesh],
    target: MotionTargetDefinition,
) -> ScnRenderableMesh | None:
    """Resolve direct or explicitly bank-linked child mesh ownership."""
    if session is None:
        return None
    document_id = normalize_document_id(session.source_path)
    candidates = tuple(
        renderable
        for renderable in renderables
        if renderable.source_object_id.document_id == document_id
    )
    direct = tuple(
        renderable
        for renderable in candidates
        if renderable.source_object_id.local_object_id == target.id.object_id
    )
    if direct:
        return direct[0]

    bank_path = resource_path_key(target.motion_bank_path)
    if not bank_path:
        return None
    controlled_children = {
        definition.id.object_id
        for definition in session.definition.targets
        if definition.enabled
        and resource_path_key(definition.motion_bank_path) == bank_path
    }
    document = graph.documents.get(document_id)
    if document is None:
        return None
    linked = []
    for renderable in candidates:
        source = document.objects.get(renderable.source_object_id)
        if (
            renderable.source_object_id.local_object_id
            in controlled_children
            and source is not None
            and source.parent_id == target.id.object_id
            and source.transform is not None
            and source.transform.same_joints_constraint
        ):
            linked.append(renderable)
    if len(linked) == 1:
        return linked[0]

    descendants = []
    for renderable in candidates:
        depth = same_joints_descendant_depth(
            document,
            renderable.source_object_id,
            target.id.object_id,
        )
        if depth is not None and depth > 0:
            descendants.append((depth, renderable))
    if not descendants:
        return None
    nearest_depth = min(depth for depth, _renderable in descendants)
    nearest = tuple(
        renderable
        for depth, renderable in descendants
        if depth == nearest_depth
    )
    visible = tuple(
        renderable for renderable in nearest if renderable.visible_by_default
    )
    if len(visible) == 1:
        return visible[0]
    return nearest[0] if len(nearest) == 1 else None


def resolve_motion_variant_visibility(
    graph: ScnSceneGraph,
    renderables: Sequence[ScnRenderableMesh],
    source: ScnRenderableMesh,
) -> dict[str, bool]:
    """Temporarily isolate an authored-hidden variant and its visible children."""
    if source.visible_by_default:
        return {}
    document = graph.documents.get(source.source_object_id.document_id)
    branch = {
        renderable.key
        for renderable in renderables
        if document is not None
        and same_joints_descendant_depth(
            document,
            renderable.source_object_id,
            source.source_object_id.local_object_id,
        )
        is not None
    }
    return {
        renderable.key: (
            renderable.key == source.key
            or renderable.key in branch
            and renderable.visible_by_default
        )
        for renderable in renderables
    }


def resolve_motion_display_scope(
    renderables: Sequence[ScnRenderableMesh],
    runtime_overrides: Mapping[str, bool],
) -> frozenset[str]:
    """Resolve the selected runtime variant independently of user visibility."""
    return frozenset(
        renderable.key
        for renderable in renderables
        if runtime_overrides.get(
            renderable.key,
            renderable.visible_by_default,
        )
    )


def resolve_same_joints_pose_targets(
    session: EntityMotionSession,
    graph: ScnSceneGraph,
    renderables: Sequence[ScnRenderableMesh],
    source: ScnRenderableMesh,
    *,
    runtime_target: MotionTargetDefinition,
) -> tuple[ScnRenderableMesh, ...]:
    """Resolve the meshes driven by one controller in a same-joints group."""
    if not renderables:
        raise ValueError("same-joints pose routing requires at least one mesh")
    document = graph.documents.get(source.source_object_id.document_id)
    if document is None:
        raise ValueError(
            f"same-joints source document "
            f"{source.source_object_id.document_id!r} is not loaded"
        )
    source_object = document.objects.get(source.source_object_id)
    if source_object is None:
        raise ValueError(
            f"same-joints source {source.key!r} has no scene object"
        )
    source_id = source.source_object_id.local_object_id

    if runtime_target.id.object_id != source_id:
        descendants = tuple(
            renderable
            for renderable in renderables
            if same_joints_descendant_depth(
                document,
                renderable.source_object_id,
                runtime_target.id.object_id,
            )
            is not None
        )
        if not any(renderable.key == source.key for renderable in descendants):
            raise ValueError(
                f"Motion target {runtime_target.id.object_id} does not own "
                f"same-joints source {source_object.name!r}"
            )
        return descendants

    banks_by_object: dict[int, set[str]] = defaultdict(set)
    for target in session.targets:
        definition = target.definition
        path = resource_path_key(definition.motion_bank_path)
        if definition.enabled and path:
            banks_by_object[definition.id.object_id].add(path)

    member_ids = frozenset(
        renderable.source_object_id.local_object_id
        for renderable in renderables
    )
    if source_id not in member_ids:
        raise ValueError(
            f"same-joints source {source_object.name!r} is outside its group"
        )
    controllers = frozenset(
        object_id for object_id in member_ids if banks_by_object.get(object_id)
    )
    parent_banks = banks_by_object.get(source_object.parent_id, set())
    if parent_banks:
        primary_candidates = {
            object_id
            for object_id in controllers
            if banks_by_object.get(object_id, set()) & parent_banks
        }
    else:
        primary_candidates = set(controllers)
    if len(primary_candidates) != 1:
        raise ValueError(
            "same-joints motion routing requires exactly one primary "
            f"controller; resolved {sorted(primary_candidates)}"
        )

    primary = next(iter(primary_candidates))
    if source_id == primary:
        target_ids = member_ids
    elif source_id in controllers:
        target_ids = (member_ids - controllers) | {source_id}
    else:
        target_ids = {source_id}
    return tuple(
        renderable
        for renderable in renderables
        if renderable.source_object_id.local_object_id in target_ids
    )


