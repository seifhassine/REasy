from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from utils.resource_file_utils import resource_path_key

from ..evaluation import (
    LayerBlendMode,
    LayerInterpolationCurve,
    LayerTimePolicy,
    LayerTiming,
    MotionLayer,
)
from ..runtime.joint_map import JointMapDefinition, JointMaskGroup
from ..runtime.model import (
    MotionBankDefinition,
    MotionChannelDefinition,
    MotionChoiceReference,
    MotionLayerSlot,
    MotionRuntimeContextError,
    MotionRuntimeScene,
    MotionTargetDefinition,
    MotionTargetId,
    RuntimeBlendMode,
    RuntimeInterpolationCurve,
    RuntimeInterpolationMode,
    RuntimeWrapMode,
)
from .resolution import (
    MotionListDocument,
    MotionPreviewResolver,
    MotionResolutionDiagnostic,
    PreviewMotionEntry,
)
from .resources import MotionListResourceStore
from .support import EntityMotionSupport


ResourceDataLoader = Callable[[str], tuple[str, bytes] | None]
MotionListLoader = Callable[[str], MotionListDocument | None]
_MotionListEntries = tuple[
    tuple[PreviewMotionEntry, ...],
    tuple[MotionResolutionDiagnostic, ...],
]
_BankCacheValue = tuple[
    str,
    MotionBankDefinition | None,
    tuple[PreviewMotionEntry, ...],
    tuple[str, ...],
]
_JointMapCacheValue = tuple[
    str,
    JointMapDefinition | None,
    tuple[str, ...],
]


@dataclass(frozen=True, slots=True)
class PreviewChannelChoice:
    definition: MotionChoiceReference
    motion: PreviewMotionEntry

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def label(self) -> str:
        return self.definition.label


@dataclass(frozen=True, slots=True)
class PreviewMotionChannel:
    definition: MotionChannelDefinition
    layer: MotionLayerSlot | None
    choices: tuple[PreviewChannelChoice, ...]
    joint_mask: JointMaskGroup | None = None
    diagnostics: tuple[str, ...] = ()

    @property
    def preview_blocker(self) -> str:
        if self.definition.unresolved_reason:
            return self.definition.unresolved_reason
        return preview_layer_blocker(self.layer, self.joint_mask)

    @property
    def normalized_time_source(self) -> int | None:
        if self.layer is None or self.layer.definition is None:
            return None
        layer_index = self.definition.layer_index
        source_index = self.layer.definition.normalized_time_source_index
        return (
            source_index
            if layer_index is not None and layer_index > source_index
            else None
        )


@dataclass(frozen=True, slots=True)
class ResolvedMotionTarget:
    definition: MotionTargetDefinition
    motion_bank_path: str = ""
    motion_bank: MotionBankDefinition | None = None
    motions: tuple[PreviewMotionEntry, ...] = ()
    joint_map_path: str = ""
    joint_map: JointMapDefinition | None = None
    channels: tuple[PreviewMotionChannel, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntityMotionSession:
    source_path: str
    definition: MotionRuntimeScene
    targets: tuple[ResolvedMotionTarget, ...]
    diagnostics: tuple[str, ...] = ()

    def target(
        self,
        target_id: MotionTargetId | None,
    ) -> ResolvedMotionTarget | None:
        if target_id is None:
            return None
        return next(
            (item for item in self.targets if item.definition.id == target_id),
            None,
        )

    @property
    def motions(self) -> tuple[PreviewMotionEntry, ...]:
        return tuple(
            motion for target in self.targets for motion in target.motions
        )

    @property
    def channels(self) -> tuple[PreviewMotionChannel, ...]:
        return tuple(
            channel for target in self.targets for channel in target.channels
        )


class EntityMotionSessionResolver:
    """Resolve a semantic runtime scene and its referenced motion resources."""

    def __init__(
        self,
        load_resource_data: ResourceDataLoader,
        *,
        support: EntityMotionSupport,
        load_motion_list: MotionListLoader | None = None,
    ):
        self._load_resource_data = load_resource_data
        self._support = support
        self._resources = MotionListResourceStore(
            support.format_codec,
            resource_data_loader=load_resource_data,
            catalog_reader=support.catalog_reader,
        )
        self._use_catalog = (
            load_motion_list is None and support.catalog_reader is not None
        )
        self._load_motion_list = load_motion_list or self._resources.load
        self._motion_resolver = MotionPreviewResolver(
            self._load_motion_list,
            support.tree_references,
        )

    def load(self, path: str, parsed: object) -> EntityMotionSession:
        definition = self._support.backend.adapt(parsed)
        return self.resolve(path, definition)

    def resolve(
        self,
        path: str,
        definition: MotionRuntimeScene,
    ) -> EntityMotionSession:
        bank_cache: dict[str, _BankCacheValue] = {}
        joint_map_cache: dict[str, _JointMapCacheValue] = {}
        targets = [
            self._resolve_target_resources(
                target,
                bank_cache,
                joint_map_cache,
            )
            for target in definition.targets
        ]
        by_id = {item.definition.id: item for item in targets}
        channels_by_target: dict[MotionTargetId, list[PreviewMotionChannel]] = {
            item.definition.id: [] for item in targets
        }
        diagnostics = [*definition.diagnostics]
        for channel in definition.channels:
            resolved = self._resolve_channel(channel, by_id)
            diagnostics.extend(resolved.diagnostics)
            if channel.target_id in channels_by_target:
                channels_by_target[channel.target_id].append(resolved)
            elif channel.target_id is None:
                diagnostics.append(
                    f"{channel.label} from {channel.provider_type} has no target Motion."
                )

        targets = [
            replace(
                target,
                channels=tuple(channels_by_target[target.definition.id]),
            )
            for target in targets
        ]
        diagnostics.extend(self._resources.errors)
        diagnostics.extend(
            item
            for target in targets
            for item in target.diagnostics
        )
        return EntityMotionSession(
            path,
            definition,
            tuple(targets),
            tuple(dict.fromkeys(diagnostics)),
        )

    def _resolve_target_resources(
        self,
        target: MotionTargetDefinition,
        bank_cache: dict[str, _BankCacheValue],
        joint_map_cache: dict[str, _JointMapCacheValue],
    ) -> ResolvedMotionTarget:
        diagnostics = []
        bank_path = ""
        bank = None
        motions: tuple[PreviewMotionEntry, ...] = ()
        if target.motion_bank_path:
            key = resource_path_key(target.motion_bank_path)
            cached = bank_cache.get(key)
            if cached is None:
                cached = self._load_bank(target.motion_bank_path)
                bank_cache[key] = cached
            bank_path, bank, motions, messages = cached
            diagnostics.extend(f"{target.name}: {item}" for item in messages)

        joint_map_path = ""
        joint_map = None
        if target.joint_map_path:
            key = resource_path_key(target.joint_map_path)
            cached = joint_map_cache.get(key)
            if cached is None:
                cached = self._load_joint_map(target.joint_map_path)
                joint_map_cache[key] = cached
            joint_map_path, joint_map, messages = cached
            diagnostics.extend(f"{target.name}: {item}" for item in messages)

        return ResolvedMotionTarget(
            target,
            bank_path,
            bank,
            motions,
            joint_map_path,
            joint_map,
            diagnostics=tuple(diagnostics),
        )

    def _load_bank(
        self,
        path: str,
    ) -> _BankCacheValue:
        hit = self._load_resource_data(path)
        if hit is None:
            return "", None, (), (f"MOTBANK {path!r} could not be resolved.",)
        resolved_path, data = hit
        try:
            bank = self._support.backend.parse_motion_bank(data)
            motions, diagnostics = self._resolve_bank_motions(bank)
            return resolved_path, bank, motions, diagnostics
        except (OSError, ValueError) as exc:
            return (
                resolved_path,
                None,
                (),
                (f"could not parse MOTBANK {resolved_path!r}: {exc}",),
            )

    def _load_joint_map(
        self,
        path: str,
    ) -> _JointMapCacheValue:
        hit = self._load_resource_data(path)
        if hit is None:
            return "", None, (f"JMAP {path!r} could not be resolved.",)
        resolved_path, data = hit
        try:
            return (
                resolved_path,
                self._support.backend.parse_joint_map(
                    data,
                    label=resolved_path,
                ),
                (),
            )
        except ValueError as exc:
            return (
                resolved_path,
                None,
                (f"could not parse JMAP {resolved_path!r}: {exc}",),
            )

    def _resolve_bank_motions(
        self,
        bank: MotionBankDefinition,
    ) -> tuple[tuple[PreviewMotionEntry, ...], tuple[str, ...]]:
        result = []
        diagnostics = []
        for reference in bank.references:
            resolved = self._resolve_motion_list(reference.path)
            if resolved is None:
                diagnostics.append(
                    f"bank {reference.bank_id} MOTLIST "
                    f"{reference.path!r} could not be resolved."
                )
                continue
            entries, messages = resolved
            diagnostics.extend(
                item.message for item in messages
            )
            result.extend(
                replace(entry, bank_id=reference.bank_id)
                for entry in entries
            )
        return tuple(result), tuple(diagnostics)

    def _resolve_motion_list(self, path: str) -> _MotionListEntries | None:
        if self._use_catalog:
            return self._resources.motion_entries(path)
        document = self._load_motion_list(path)
        if document is None:
            return None
        resolution = self._motion_resolver.resolve(document)
        return resolution.entries, resolution.diagnostics

    def _resolve_channel(
        self,
        channel: MotionChannelDefinition,
        targets: dict[MotionTargetId, ResolvedMotionTarget],
    ) -> PreviewMotionChannel:
        target = targets.get(channel.target_id)
        layer = (
            target.definition.layer(channel.layer_index)
            if target is not None and channel.layer_index is not None
            else None
        )
        diagnostics = []
        source_target = targets.get(
            channel.source_bank_target_id or channel.target_id
        )
        if channel.resource_path and channel.choices:
            source_motions = self._direct_resource_motions(
                channel,
                diagnostics,
            )
        elif source_target is not None:
            source_motions = source_target.motions
        else:
            source_motions = ()

        joint_mask = None
        if layer is not None and layer.definition is not None:
            mask_id = layer.definition.joint_mask_id
            if target is not None and target.joint_map is not None:
                joint_mask = target.joint_map.mask_group(mask_id)
            if mask_id and (target is None or target.joint_map is None):
                diagnostics.append(
                    f"{channel.label} uses JMAP joint mask {mask_id}, "
                    "but the target JMAP is unresolved."
                )
            elif mask_id and joint_mask is None:
                diagnostics.append(
                    f"{channel.label} uses JMAP joint mask {mask_id}, "
                    "which is absent from the target JMAP."
                )

        choices = []
        for reference in channel.choices:
            matches = [
                item
                for item in source_motions
                if item.motion_id == reference.motion_id
                and (
                    channel.bank_id is None
                    or item.bank_id == channel.bank_id
                )
            ]
            if len(matches) != 1:
                diagnostics.append(
                    f"{channel.label} choice {reference.label!r} maps to "
                    f"bank {channel.bank_id}, MotionID {reference.motion_id}; "
                    f"{len(matches)} motions resolve."
                )
                continue
            choices.append(PreviewChannelChoice(reference, matches[0]))
        return PreviewMotionChannel(
            definition=channel,
            layer=layer,
            choices=tuple(choices),
            joint_mask=joint_mask,
            diagnostics=tuple(diagnostics),
        )

    def _direct_resource_motions(
        self,
        channel: MotionChannelDefinition,
        diagnostics: list[str],
    ) -> tuple[PreviewMotionEntry, ...]:
        resolved = self._resolve_motion_list(channel.resource_path)
        if resolved is None:
            diagnostics.append(
                f"{channel.label} resource {channel.resource_path!r} "
                "could not be resolved as a MOTLIST."
            )
            return ()
        entries, messages = resolved
        diagnostics.extend(item.message for item in messages)
        return tuple(
            replace(entry, bank_id=channel.bank_id)
            for entry in entries
        )


def preview_layer_blocker(
    layer: MotionLayerSlot | None,
    joint_mask: JointMaskGroup | None = None,
) -> str:
    if layer is None:
        return "target layer is unresolved"
    definition = layer.definition
    if definition is None:
        return layer.diagnostic or "target layer is unresolved"
    if definition.blend_mode not in (
        RuntimeBlendMode.OVERWRITE,
        RuntimeBlendMode.ADD_BLEND,
    ):
        return (
            f"{definition.blend_mode.label} evaluation is not semantically "
            "established"
        )
    if definition.joint_mask_id != 0 and joint_mask is None:
        return (
            f"JMAP joint mask {definition.joint_mask_id} is unresolved"
        )
    if definition.world_rotation_blend:
        return "world-rotation blending is not evaluated"
    if definition.mirror_symmetry:
        return "mirrored layer evaluation is not established"
    if definition.unknown_flags:
        return "unidentified TreeLayer flags are enabled"
    if definition.ignore_first_interpolation:
        return "ignore-first-interpolation behavior is not evaluated"
    if definition.interpolation_mode not in (
        RuntimeInterpolationMode.NONE,
        RuntimeInterpolationMode.FRONT_FADE,
    ):
        return (
            f"{definition.interpolation_mode.label} interpolation "
            "is not evaluated"
        )
    if definition.interpolation_curve not in (
        RuntimeInterpolationCurve.LINEAR,
        RuntimeInterpolationCurve.SMOOTH,
        RuntimeInterpolationCurve.EASE_IN,
        RuntimeInterpolationCurve.EASE_OUT,
    ):
        return (
            f"interpolation curve {definition.interpolation_curve.value} "
            "is not evaluated"
        )
    if definition.wrap_mode not in (
        RuntimeWrapMode.DEFAULT,
        RuntimeWrapMode.ONCE,
        RuntimeWrapMode.LOOP,
    ):
        return f"{definition.wrap_mode.label} wrapping is not evaluated"
    if not 0.0 <= definition.weight <= 1.0:
        return "layer blend rate is outside [0, 1]"
    if definition.speed < 0.0:
        return "layer speed is negative"
    if definition.interpolation_frames < 0.0:
        return "layer interpolation frame count is negative"
    return ""


def build_preview_motion_layer(
    channel: PreviewMotionChannel,
    choice: PreviewChannelChoice,
    *,
    synchronize_normalized_time: bool = False,
) -> MotionLayer:
    blocker = channel.preview_blocker
    if blocker:
        raise MotionRuntimeContextError(
            f"{channel.definition.label} cannot be previewed: {blocker}"
        )
    definition = channel.layer.definition
    curve = {
        RuntimeInterpolationCurve.LINEAR: LayerInterpolationCurve.LINEAR,
        RuntimeInterpolationCurve.SMOOTH: LayerInterpolationCurve.SMOOTH,
        RuntimeInterpolationCurve.EASE_IN: LayerInterpolationCurve.EASE_IN,
        RuntimeInterpolationCurve.EASE_OUT: LayerInterpolationCurve.EASE_OUT,
    }.get(definition.interpolation_curve)
    if curve is None:
        raise MotionRuntimeContextError(
            f"unsupported layer curve {definition.interpolation_curve.value}"
        )
    fade = (
        0.0
        if definition.interpolation_mode is RuntimeInterpolationMode.NONE
        else channel.definition.fade_in_frames
        or definition.interpolation_frames
    )
    source_index = (
        channel.normalized_time_source
        if synchronize_normalized_time
        else None
    )
    layer_index = channel.definition.layer_index
    return MotionLayer(
        choice.motion.resolve_motion(),
        blend_mode={
            RuntimeBlendMode.OVERWRITE: LayerBlendMode.OVERWRITE,
            RuntimeBlendMode.ADD_BLEND: LayerBlendMode.ADDITIVE,
        }[definition.blend_mode],
        weight=definition.weight,
        timing=LayerTiming(fade_in_frames=fade, curve=curve),
        speed=definition.speed,
        wrap_looping=definition.wrap_looping,
        layer_key=(
            layer_index
            if synchronize_normalized_time
            and layer_index is not None
            and layer_index != 0
            else None
        ),
        time_policy=(
            LayerTimePolicy.SYNCHRONIZED_NORMALIZED_TIME
            if source_index is not None
            else LayerTimePolicy.INDEPENDENT
        ),
        time_source_key=source_index,
        joint_channels=(
            tuple(
                (entry.joint_hash, entry.channels)
                for entry in channel.joint_mask.entries
            )
            if channel.joint_mask is not None
            else None
        ),
    )
