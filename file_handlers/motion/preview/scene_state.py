from __future__ import annotations

from collections.abc import Iterable

from ..mot.model import Motion
from ..mot_clip.model import ClipInterpolation, ClipNode, ClipProperty
from ..runtime.model import MotionSceneStateBinding, RuntimePropertyValue
from ..sequence.model import SequenceCategory
from .model import MotionPreviewError


_DISCRETE = frozenset((
    ClipInterpolation.DISCRETE,
    ClipInterpolation.DISCRETE_TO_END,
))


def evaluate_motion_scene_state(
    motion: Motion,
    bindings: Iterable[MotionSceneStateBinding],
    frame: float,
) -> tuple[dict[int, bool], dict[int, dict[int, bool]]]:
    """Evaluate sequence-driven object visibility and mesh-part state."""

    visibility: dict[int, bool] = {}
    parts: dict[int, dict[int, bool]] = {}
    for binding in bindings:
        value = _source_value(motion, binding, frame)
        active = any(
            type(value) is type(candidate) and value == candidate
            for candidate in binding.active_values
        )
        for target in binding.targets:
            visible = (
                target.visible_when_active
                if active
                else target.visible_when_inactive
            )
            if visible is not None:
                _set_unique(
                    visibility,
                    target.object_id,
                    visible,
                    "GameObject visibility",
                )
            target_parts = parts.setdefault(target.object_id, {})
            for part in target.parts:
                enabled = (
                    part.enabled_when_active
                    if active
                    else part.enabled_when_inactive
                )
                _set_unique(
                    target_parts,
                    part.part_index,
                    enabled,
                    f"GameObject {target.object_id} mesh-part state",
                )
            if not target_parts:
                parts.pop(target.object_id, None)
    return visibility, parts


def resolve_renderable_scene_state(
    motion: Motion,
    bindings: Iterable[MotionSceneStateBinding],
    frame: float,
    document_id: str,
    renderables: Iterable[object],
) -> tuple[dict[str, bool], dict[str, tuple[bool, ...]]]:
    """Map semantic object state onto the loaded renderable instances."""
    visibility, part_states = evaluate_motion_scene_state(
        motion,
        bindings,
        frame,
    )
    visibility_overrides = {}
    part_overrides = {}
    for renderable in renderables:
        if renderable.source_object_id.document_id != document_id:
            continue
        object_id = renderable.source_object_id.local_object_id
        if object_id in visibility:
            visibility_overrides[renderable.key] = visibility[object_id]
        changes = part_states.get(object_id)
        if not changes:
            continue
        enabled = list(renderable.enabled_parts or ())
        enabled.extend(True for _ in range(max(changes) + 1 - len(enabled)))
        for part_index, value in changes.items():
            enabled[part_index] = value
        part_overrides[renderable.key] = tuple(enabled)
    return visibility_overrides, part_overrides


def _source_value(
    motion: Motion,
    binding: MotionSceneStateBinding,
    frame: float,
) -> RuntimePropertyValue:
    values = [
        value
        for sequence in motion.sequences
        if sequence.category is SequenceCategory.GAME
        for node in _nodes(sequence.clip.root)
        if node.name == binding.source_node_type
        for prop in _properties(node.properties)
        if prop.name == binding.source_property
        if (value := _sample_property(prop, frame)) is not _UNSET
    ]
    if not values:
        return binding.default_value
    first = values[0]
    if any(type(value) is not type(first) or value != first for value in values[1:]):
        raise MotionPreviewError(
            f"{binding.source_node_type}.{binding.source_property} has "
            f"conflicting values at frame {frame:g}"
        )
    return first


_UNSET = object()


def _sample_property(
    prop: ClipProperty,
    frame: float,
):
    if prop.start_frame < 0.0 or not prop.keys:
        return _UNSET
    after_end = prop.end_frame >= 0.0 and frame > prop.end_frame
    if frame < prop.start_frame or (
        after_end and (prop.restoration or not prop.set_after_end_frame)
    ):
        return _UNSET
    keys = [*prop.keys, *([prop.last_key] if prop.last_key is not None else [])]
    key = max((item for item in keys if item.frame <= frame), key=lambda item: item.frame, default=None)
    if key is None:
        return _UNSET
    if key.interpolation not in _DISCRETE:
        raise MotionPreviewError(
            f"scene-state property {prop.name!r} uses unsupported "
            f"{key.interpolation.name.lower()} interpolation"
        )
    if not isinstance(key.value, (bool, int, float, str)):
        raise MotionPreviewError(
            f"scene-state property {prop.name!r} is not scalar"
        )
    return key.value


def _nodes(root: ClipNode):
    for node in root.children:
        yield node
        yield from _nodes(node)


def _properties(properties: Iterable[ClipProperty]):
    for prop in properties:
        yield prop
        yield from _properties(prop.children)


def _set_unique(mapping: dict, key, value, label: str) -> None:
    previous = mapping.get(key, _UNSET)
    if previous is not _UNSET and previous != value:
        raise MotionPreviewError(f"{label} has conflicting runtime values")
    mapping[key] = value
