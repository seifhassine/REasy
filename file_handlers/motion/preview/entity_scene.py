from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from file_handlers.rsz.scn_scene_attachments import (
    same_joints_constraint_renderables,
    same_joints_descendant_depth,
)
from file_handlers.rsz.scn_scene_graph import (
    ScnRenderableMesh,
    ScnSceneGraph,
    normalize_document_id,
)

from ..evaluation import Rig
from ..evaluation.binding import (
    JointBindingStrategy,
    bind_motion,
    binding_reaches_dominant_branch,
)
from ..mot import Motion
from .entity_session import EntityMotionSession, ResolvedMotionTarget
from .shared_pose import resolve_motion_target_renderable
from .target import RigPreviewTarget

if TYPE_CHECKING:
    from file_handlers.rsz.scn_scene_preview import ScnLoadedMesh


@dataclass(frozen=True, slots=True)
class MeshBinding:
    asset: ScnLoadedMesh
    target: RigPreviewTarget


class EntitySceneCoordinator:
    """Resolve runtime motion targets onto compatible scene meshes."""

    def __init__(
        self,
        target_factory: Callable[[object], RigPreviewTarget],
        joint_binding: JointBindingStrategy,
    ) -> None:
        self._target_factory = target_factory
        self._joint_binding = joint_binding
        self.bindings: list[MeshBinding] = []
        self.binding_errors: dict[str, str] = {}
        self._selected_variants: dict[object, str] = {}
        self._motion_joint_keys: dict[object, frozenset[object]] = {}

    def reset_session(self) -> None:
        self._selected_variants.clear()
        self._motion_joint_keys.clear()
        self.binding_errors.clear()

    def bind_assets(self, assets: Iterable[object]) -> tuple[str, ...]:
        bindings = []
        errors = []
        self.binding_errors.clear()
        for asset in assets:
            try:
                bindings.append(MeshBinding(asset, self._target_factory(asset)))
            except ValueError as exc:
                message = f"{asset.renderable.mesh_path}: {exc}"
                self.binding_errors[asset.key] = message
                errors.append(message)
        self.bindings = bindings
        return tuple(errors)

    def preferred_variant(
        self,
        target: ResolvedMotionTarget | None,
        fallback: str = "",
    ) -> str:
        return (
            self._selected_variants.get(target.definition.id, fallback)
            if target is not None
            else fallback
        )

    def select_variant(self, target: ResolvedMotionTarget, key: str) -> None:
        self._selected_variants[target.definition.id] = key

    def default_index(
        self,
        session: EntityMotionSession | None,
        graphs: Sequence[object],
        target: ResolvedMotionTarget | None,
        previous_key: str = "",
        motion: Motion | None = None,
    ) -> int | None:
        if not self.bindings:
            return None
        if target is not None:
            match = self.matching_index(session, graphs, target, motion)
            if match is not None:
                return match
        if previous_key:
            match = next(
                (
                    index
                    for index, binding in enumerate(self.bindings)
                    if binding.asset.key == previous_key
                ),
                None,
            )
            if match is not None:
                return match
        return next(
            (
                index
                for index, binding in enumerate(self.bindings)
                if getattr(binding.asset.renderable, "visible_by_default", True)
            ),
            0,
        )

    def compatible_indices(
        self,
        graphs: Sequence[object],
        target: ResolvedMotionTarget,
        reference: int,
        motion: Motion | None = None,
    ) -> tuple[int, ...]:
        owner = self.bindings[reference]
        _graph, group = self.same_joints_group(
            graphs,
            owner.asset.renderable,
        )
        group_keys = {renderable.key for renderable in group}
        source_keys = (
            self._motion_joint_keys_for(motion)
            if motion is not None
            else self.motion_joint_keys(target)
        )
        owner_signature = self._rig_motion_signature(
            owner.target.rig,
            source_keys,
        )
        if not owner_signature:
            return (
                (reference,)
                if motion is None
                or self._motion_applies_to_rig(motion, owner.target.rig)
                else ()
            )
        return tuple(
            index
            for index, binding in enumerate(self.bindings)
            if binding.asset.key in group_keys
            and self._rig_motion_signature(binding.target.rig, source_keys)
            == owner_signature
            and (
                motion is None
                or self._motion_applies_to_rig(motion, binding.target.rig)
            )
        )

    def matching_index(
        self,
        session: EntityMotionSession | None,
        graphs: Sequence[object],
        target: ResolvedMotionTarget,
        motion: Motion | None = None,
    ) -> int | None:
        document_id = normalize_document_id(session.source_path) if session else ""
        graph = next(
            (
                item
                for item in graphs
                if getattr(item, "root_document_id", "") == document_id
            ),
            None,
        )
        renderable = (
            resolve_motion_target_renderable(
                session,
                graph,
                tuple(binding.asset.renderable for binding in self.bindings),
                target.definition,
            )
            if graph is not None
            else None
        )
        if renderable is not None:
            return next(
                (
                    index
                    for index, binding in enumerate(self.bindings)
                    if binding.asset.renderable is renderable
                ),
                None,
            )
        document = graph.documents.get(document_id) if graph is not None else None
        if document is not None:
            source_keys = (
                self._motion_joint_keys_for(motion)
                if motion is not None
                else self.motion_joint_keys(target)
            )
            candidates = []
            for index, binding in enumerate(self.bindings):
                candidate = binding.asset.renderable
                if candidate.source_object_id.document_id != document_id:
                    continue
                depth = same_joints_descendant_depth(
                    document,
                    candidate.source_object_id,
                    target.definition.id.object_id,
                )
                if depth is None:
                    continue
                candidates.append(
                    (
                        len(source_keys & self._rig_joint_keys(binding.target.rig)),
                        bool(candidate.visible_by_default),
                        -depth,
                        len(binding.target.rig.joints),
                        -index,
                        index,
                    )
                )
            if candidates:
                return max(candidates)[-1]
        return self._index_for_object(session, target.definition.id.object_id)

    def motion_joint_keys(
        self,
        target: ResolvedMotionTarget,
    ) -> frozenset[object]:
        target_id = target.definition.id
        cached = self._motion_joint_keys.get(target_id)
        if cached is not None:
            return cached
        loaded = tuple(
            entry.loaded_motion
            for entry in target.motions
            if entry.loaded_motion is not None
        )
        result = frozenset(
            self._joint_binding.motion_key(joint)
            for motion in loaded
            if motion.skeleton is not None
            for joint in motion.skeleton.joints
        )
        if len(loaded) == len(target.motions):
            self._motion_joint_keys[target_id] = result
        return result

    def _motion_joint_keys_for(self, motion: Motion) -> frozenset[object]:
        return frozenset(
            self._joint_binding.motion_key(joint)
            for joint in (motion.skeleton.joints if motion.skeleton else ())
        )

    def _motion_applies_to_rig(self, motion: Motion, rig: Rig) -> bool:
        binding = bind_motion(motion, rig, self._joint_binding)
        return not binding.has_errors and binding_reaches_dominant_branch(
            binding
        )

    def choice_label(
        self,
        graphs: Sequence[object],
        binding: MeshBinding,
    ) -> str:
        renderable = binding.asset.renderable
        for graph in graphs:
            document = getattr(graph, "documents", {}).get(
                renderable.source_object_id.document_id
            )
            scene_object = (
                document.objects.get(renderable.source_object_id)
                if document is not None
                else None
            )
            if scene_object is not None:
                return scene_object.name or binding.target.label
        return binding.target.label

    @staticmethod
    def same_joints_group(
        graphs: Sequence[object],
        source: ScnRenderableMesh,
    ) -> tuple[ScnSceneGraph, tuple[ScnRenderableMesh, ...]]:
        graph = next(
            (
                item
                for item in graphs
                if any(
                    renderable is source
                    for renderable in getattr(item, "renderables", ())
                )
            ),
            None,
        )
        if graph is None:
            raise ValueError(
                f"scene mesh {source.key!r} is not part of a loaded graph"
            )
        return (
            graph,
            same_joints_constraint_renderables(graph, source),
        )

    def _index_for_object(
        self,
        session: EntityMotionSession | None,
        object_id: int,
    ) -> int | None:
        document_id = normalize_document_id(session.source_path) if session else ""
        return next(
            (
                index
                for index, binding in enumerate(self.bindings)
                if binding.asset.renderable.source_object_id.document_id
                == document_id
                and binding.asset.renderable.source_object_id.local_object_id
                == object_id
            ),
            None,
        )

    def _rig_joint_keys(self, rig: Rig) -> frozenset[object]:
        return frozenset(
            self._joint_binding.rig_key(joint) for joint in rig.joints
        )

    def _rig_motion_signature(
        self,
        rig: Rig,
        source_keys: frozenset[object],
    ) -> frozenset[tuple[object, object | None]]:
        keys = tuple(self._joint_binding.rig_key(joint) for joint in rig.joints)
        bound_keys = tuple(key for key in keys if key in source_keys)
        if len(bound_keys) != len(set(bound_keys)):
            return frozenset()
        return frozenset(
            (
                key,
                keys[joint.parent_index]
                if joint.parent_index is not None
                else None,
            )
            for key, joint in zip(keys, rig.joints, strict=True)
            if key in source_keys
        )
