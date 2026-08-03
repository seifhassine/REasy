from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import Mapping

from file_handlers.motbank.motbank_file import MotbankFile
from file_handlers.rsz.rsz_file import RszFile
from utils.type_registry import TypeRegistry

from .jmap import parse_dmc5_joint_map
from .joint_map import JointMapDefinition
from .model import (
    MotionBankDefinition,
    MotionBankReference,
    MotionChannelActivation,
    MotionChannelDefinition,
    MotionChannelKind,
    MotionChoiceReference,
    MotionLayerObserver,
    MotionLayerSlot,
    MotionMaterialController,
    MotionMaterialParameter,
    MotionRuntimeContextError,
    MotionRuntimeScene,
    MotionSceneObjectState,
    MotionScenePartState,
    MotionSceneStateBinding,
    MotionTargetDefinition,
    MotionTargetId,
    RuntimeBlendMode,
    RuntimeInterpolationCurve,
    RuntimeInterpolationMode,
    RuntimeLayerDefinition,
    RuntimeProperty,
    RuntimeRootMotionMode,
    RuntimeWrapMode,
    TreeLayerSource,
)
from .pfb_graph import PfbRuntimeGraph
from .profiles import DMC5_RUNTIME_PROFILE, Dmc5MotionRuntimeProfile
from .rsz_values import (
    bool_value as _bool,
    clean_path as _clean_path,
    finite_float as _finite_float,
    int_array as _int_array,
    int_value as _int,
    nonnegative_float as _nonnegative_float,
    string_value as _string,
)


_NO_MOTION_IDS = frozenset((-1, 0xFFFFFFFF))


def parse_dmc5_motion_bank(data: bytes) -> MotionBankDefinition:
    parsed = MotbankFile()
    try:
        parsed.read(data)
    except Exception as exc:
        raise MotionRuntimeContextError(
            f"could not parse DMC5 MOTBANK: {exc}"
        ) from exc
    if parsed.version != 1:
        raise MotionRuntimeContextError(
            f"DMC5 runtime context requires MOTBANK v1, got v{parsed.version}"
        )
    return MotionBankDefinition(
        version=parsed.version,
        references=tuple(
            MotionBankReference(
                item.bank_id,
                _clean_path(item.path),
                item.bank_type,
                item.bank_type_mask_bits,
            )
            for item in parsed.items
        ),
        uvar_path=_clean_path(parsed.uvar_path),
        joint_map_path=_clean_path(parsed.jmap_path),
    )


def parse_dmc5_motion_runtime_scene(
    data: bytes,
    *,
    path: str,
    registry_path: str | Path,
    profile: Dmc5MotionRuntimeProfile = DMC5_RUNTIME_PROFILE,
) -> MotionRuntimeScene:
    if not path.lower().endswith(profile.pfb_suffix):
        raise MotionRuntimeContextError(
            f"DMC5 runtime scene requires a {profile.pfb_suffix} resource"
        )
    if data[:4] != b"PFB\0":
        raise MotionRuntimeContextError("runtime scene resource is not a PFB")

    parsed = RszFile()
    parsed.filepath = path
    parsed.game_version = profile.game_version
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            parsed.type_registry = TypeRegistry(str(registry_path))
            parsed.read(data)
    except Exception as exc:
        raise MotionRuntimeContextError(
            f"could not parse DMC5 character PFB: {exc}"
        ) from exc
    return adapt_dmc5_motion_runtime_scene(parsed, profile=profile)


def adapt_dmc5_motion_runtime_scene(
    parsed: RszFile,
    *,
    profile: Dmc5MotionRuntimeProfile = DMC5_RUNTIME_PROFILE,
) -> MotionRuntimeScene:
    return _Dmc5RuntimeAdapter(parsed, profile).adapt()


def adapt_dmc5_material_controllers(
    parsed: RszFile,
    *,
    profile: Dmc5MotionRuntimeProfile = DMC5_RUNTIME_PROFILE,
) -> tuple[MotionMaterialController, ...]:
    """Extract material animation without rebuilding the entity-motion graph."""

    return _read_material_controllers(PfbRuntimeGraph(parsed), profile)


class _Dmc5RuntimeAdapter:
    def __init__(self, parsed: RszFile, profile: Dmc5MotionRuntimeProfile):
        self.graph = PfbRuntimeGraph(parsed)
        self.profile = profile
        self.targets: dict[MotionTargetId, MotionTargetDefinition] = {}
        self.targets_by_object: dict[int, list[MotionTargetId]] = {}
        self.channels: list[MotionChannelDefinition] = []
        self.observers: list[MotionLayerObserver] = []
        self.scene_state_bindings: list[MotionSceneStateBinding] = []
        self.diagnostics: list[str] = []

    def adapt(self) -> MotionRuntimeScene:
        self._read_targets()
        self._read_authored_sources()
        self._read_facial_controllers()
        self._read_motion_fsms()
        self._read_actor_motion()
        self._read_observers()
        self._read_scene_state_bindings()
        return MotionRuntimeScene(
            targets=tuple(self.targets.values()),
            channels=tuple(self.channels),
            observers=tuple(self.observers),
            diagnostics=tuple(dict.fromkeys(self.diagnostics)),
            material_controllers=_read_material_controllers(
                self.graph,
                self.profile,
            ),
            scene_state_bindings=tuple(self.scene_state_bindings),
        )

    def _read_targets(self) -> None:
        for instance_id in self.graph.instances_of(self.profile.motion_type):
            owner_id = self.graph.owner_of(instance_id)
            if owner_id is None:
                self.diagnostics.append(
                    f"Motion component {instance_id} has no owning GameObject."
                )
                continue
            fields = self.graph.fields(instance_id)
            target_id = MotionTargetId(owner_id, instance_id)
            slots = tuple(
                self._read_layer_slot(index, layer_id)
                for index, layer_id in enumerate(
                    _int_array(fields, "PrivateLayer", default=())
                )
            )
            target = MotionTargetDefinition(
                id=target_id,
                name=self.graph.object_names.get(owner_id, f"GameObject {owner_id}"),
                enabled=_bool(fields, "Enabled", default=True),
                motion_bank_path=_string(
                    fields,
                    "MotionBankAsset",
                    default="",
                ),
                joint_map_path=_string(fields, "JointMap", default=""),
                layers=slots,
                play_speed=_finite_float(fields, "PlaySpeed", default=1.0),
                root_motion_mode=RuntimeRootMotionMode(
                    _int(fields, "RootMotion", default=0)
                ),
                stop_at_motion_end=_bool(
                    fields,
                    "StopAtMotionEnd",
                    default=False,
                ),
                joint_map_expression_enabled=_bool(
                    fields,
                    "JointMapExpressionEnabled",
                    default=False,
                ),
            )
            self.targets[target_id] = target
            self.targets_by_object.setdefault(owner_id, []).append(target_id)

    def _read_layer_slot(self, index: int, instance_id: int) -> MotionLayerSlot:
        if instance_id <= 0:
            return MotionLayerSlot(
                index,
                None,
                "",
                None,
                "null private-layer reference",
            )
        type_name = self.graph.type_name(instance_id)
        if not self.graph.is_a(type_name, self.profile.tree_layer_type):
            return MotionLayerSlot(
                index,
                instance_id,
                type_name,
                None,
                f"expected {self.profile.tree_layer_type}, got {type_name or 'unknown'}",
            )
        try:
            definition = self._read_tree_layer(self.graph.fields(instance_id))
        except MotionRuntimeContextError as exc:
            return MotionLayerSlot(index, instance_id, type_name, None, str(exc))
        return MotionLayerSlot(index, instance_id, type_name, definition)

    def _read_tree_layer(
        self,
        fields: Mapping[str, object],
    ) -> RuntimeLayerDefinition:
        blend_mode = RuntimeBlendMode(_int(fields, "BlendMode"))
        interpolation_mode = RuntimeInterpolationMode(
            _int(fields, "InterpolationMode")
        )
        interpolation_curve = RuntimeInterpolationCurve(
            _int(fields, "InterpolationCurve")
        )
        wrap_mode = RuntimeWrapMode(_int(fields, "WrapMode"))
        unknown_flags = tuple(
            name
            for name in fields
            if name.lower().startswith("ukn") and _bool(fields, name, default=False)
        )
        motion_id = _int(fields, "MotionID")
        return RuntimeLayerDefinition(
            blend_mode=blend_mode,
            weight=_finite_float(fields, "BlendRate"),
            normalized_time_source_index=_int(fields, "BaseLayerNo"),
            joint_mask_id=_int(fields, "JointMaskID"),
            wrap_mode=wrap_mode,
            frame=_finite_float(fields, "Frame"),
            speed=_finite_float(fields, "Speed"),
            interpolation_mode=interpolation_mode,
            interpolation_curve=interpolation_curve,
            interpolation_frames=_finite_float(fields, "InterpolationFrame"),
            source=TreeLayerSource(
                _string(fields, "Resource", default=""),
                _int(fields, "MotionBankID"),
                None if motion_id in _NO_MOTION_IDS else motion_id,
            ),
            world_rotation_blend=_bool(
                fields,
                "WorldRotationBlend",
                default=False,
            ),
            mirror_symmetry=_bool(fields, "MirrorSymmetry", default=False),
            show_integer_frame=_bool(fields, "ShowIntFrame", default=False),
            ignore_first_interpolation=_bool(
                fields,
                "IgnoreFirstInterpolation(?)",
                default=False,
            ),
            unknown_flags=unknown_flags,
        )

    def _read_authored_sources(self) -> None:
        for target in self.targets.values():
            for slot in target.layers:
                layer = slot.definition
                if layer is None or not layer.source.configured:
                    continue
                source = layer.source
                choices = (
                    (
                        MotionChoiceReference(
                            "authored",
                            "Authored motion",
                            source.motion_id,
                        ),
                    )
                    if source.motion_id is not None
                    else ()
                )
                self.channels.append(
                    MotionChannelDefinition(
                        key=f"authored:{target.id.component_instance_id}:{slot.index}",
                        label="Authored source",
                        kind=MotionChannelKind.AUTHORED_SOURCE,
                        activation=(
                            MotionChannelActivation.ACTIVE
                            if target.enabled
                            else MotionChannelActivation.INACTIVE
                        ),
                        target_id=target.id,
                        layer_index=slot.index,
                        bank_id=source.bank_id,
                        choices=choices,
                        default_choice_key="authored" if choices else None,
                        source_bank_target_id=target.id,
                        resource_path=source.resource_path,
                        provider_type=slot.type_name,
                        provider_instance_id=slot.instance_id,
                        provider_object_id=target.id.object_id,
                    )
                )

    def _read_facial_controllers(self) -> None:
        for instance_id in self.graph.instances_of(
            self.profile.facial_controller_type
        ):
            fields = self.graph.fields(instance_id)
            type_name = self.graph.type_name(instance_id)
            owner_id = self.graph.owner_of(instance_id)
            face_object = self.graph.reference_target(
                instance_id,
                self.profile.face_object_property_id,
            )
            monitored_object = self.graph.reference_target(
                instance_id,
                self.profile.monitored_object_property_id,
            )
            if monitored_object is None:
                monitored_object = owner_id
            face_target = self._target_for_object(
                face_object,
                f"{type_name} FaceObject",
            )
            monitored_target = self._target_for_object(
                monitored_object,
                f"{type_name} monitored object",
            )
            controller_enabled = _bool(fields, "Enabled", default=True)
            face_activation = self._flag_activation(
                controller_enabled
                and _bool(fields, "IsFacialAnimationEnabled", default=False)
            )
            lip_activation = self._flag_activation(
                controller_enabled
                and _bool(fields, "IsLipSyncEnabled", default=False)
            )

            alternate_activation = (
                MotionChannelActivation.RUNTIME_CONTROLLED
                if controller_enabled
                else MotionChannelActivation.INACTIVE
            )
            work_bindings = (
                (
                    self._append_face_work,
                    "FaceData",
                    "Face expression",
                    "Body expression",
                    "BodyLayerNoForExpression",
                    face_activation,
                ),
                (
                    self._append_lip_work,
                    "LipSyncData",
                    "Lip sync",
                    "Body lip sync",
                    "BodyLayerNoForLipSync",
                    lip_activation,
                ),
                (
                    self._append_face_work,
                    "ExFaceData",
                    "Extended face expression",
                    "Extended body expression",
                    "BodyLayerNoForExpression",
                    alternate_activation,
                ),
                (
                    self._append_lip_work,
                    "ExLipSyncData",
                    "Extended lip sync",
                    "Extended body lip sync",
                    "BodyLayerNoForLipSync",
                    alternate_activation,
                ),
            )
            for (
                append,
                work_name,
                label,
                _body_label,
                _body_layer_field,
                activation,
            ) in work_bindings:
                append(
                    fields,
                    work_name,
                    label,
                    activation,
                    face_target,
                    face_target,
                    instance_id,
                    type_name,
                )

            if self.graph.is_a(
                type_name,
                self.profile.player_facial_controller_type,
            ):
                self._append_blink(
                    fields,
                    controller_enabled,
                    face_target,
                    instance_id,
                    type_name,
                )

            if self.graph.is_a(
                type_name,
                self.profile.cerberus_facial_controller_type,
            ):
                for (
                    append,
                    work_name,
                    _label,
                    body_label,
                    body_layer_field,
                    activation,
                ) in work_bindings:
                    append(
                        fields,
                        work_name,
                        body_label,
                        activation,
                        monitored_target,
                        monitored_target,
                        instance_id,
                        type_name,
                        layer_override=_int(
                            fields,
                            body_layer_field,
                            default=-1,
                        ),
                    )

    def _append_face_work(
        self,
        controller: Mapping[str, object],
        field_name: str,
        label: str,
        activation: MotionChannelActivation,
        target_id: MotionTargetId | None,
        source_target_id: MotionTargetId | None,
        provider_id: int,
        provider_type: str,
        *,
        layer_override: int | None = None,
    ) -> None:
        work = _referenced_fields(self.graph, controller, field_name)
        if work is None:
            return
        layer_index = (
            layer_override
            if layer_override is not None
            else _int(work, "Layer", default=-1)
        )
        layer_index = _optional_index(layer_index)
        use_extended_ids = _bool(work, "IsUseExFaceID", default=False)
        choices = (
            self._extended_face_choices(work)
            if use_extended_ids
            else self._face_list_choices(work)
        )
        default_expression = _int(
            work,
            "DefaultExpressionType",
            default=0,
        )
        default_key = next(
            (
                item.key
                for item in choices
                if item.key.startswith(f"expression:{default_expression}")
            ),
            None,
        )
        problem = self._layer_problem(target_id, layer_index)
        if use_extended_ids and not choices:
            problem = _join_problem(
                problem,
                "extended-expression mode contains no ExID choices",
            )
        self.channels.append(
            MotionChannelDefinition(
                key=f"{provider_id}:{field_name}:{label}",
                label=label,
                kind=MotionChannelKind.FACE_EXPRESSION,
                activation=activation,
                target_id=target_id,
                layer_index=layer_index,
                bank_id=_int(work, "BankID", default=0),
                choices=choices,
                default_choice_key=default_key,
                fade_in_frames=_nonnegative_float(
                    work,
                    "InterpolationFrame",
                    default=0.0,
                ),
                fade_out_frames=_nonnegative_float(
                    work,
                    "InterpolationOutFrame",
                    default=0.0,
                ),
                source_bank_target_id=source_target_id,
                provider_type=provider_type,
                provider_instance_id=provider_id,
                provider_object_id=self.graph.owner_of(provider_id),
                properties=(
                    RuntimeProperty("Work", field_name),
                    RuntimeProperty("Use extended IDs", use_extended_ids),
                    RuntimeProperty("Default expression", default_expression),
                ),
                unresolved_reason=problem,
            )
        )

    def _face_list_choices(
        self,
        work: Mapping[str, object],
    ) -> tuple[MotionChoiceReference, ...]:
        return tuple(
            MotionChoiceReference(
                f"expression:{expression}",
                self._expression_name(expression),
                motion_id,
            )
            for expression, motion_id in enumerate(
                _int_array(work, "FaceList", default=())
            )
            if motion_id not in _NO_MOTION_IDS
        )

    def _extended_face_choices(
        self,
        work: Mapping[str, object],
    ) -> tuple[MotionChoiceReference, ...]:
        choices = []
        for expression, reference in enumerate(
            _int_array(work, "ExID", default=())
        ):
            definition = self.graph.fields(reference) if reference > 0 else {}
            motion_ids = _int_array(definition, "RandomID", default=())
            for variation, motion_id in enumerate(motion_ids):
                if motion_id in _NO_MOTION_IDS:
                    continue
                choices.append(
                    MotionChoiceReference(
                        f"expression:{expression}:variation:{variation}",
                        f"{self._expression_name(expression)} · Variant {variation + 1}",
                        motion_id,
                    )
                )
        return tuple(choices)

    def _append_lip_work(
        self,
        controller: Mapping[str, object],
        field_name: str,
        label: str,
        activation: MotionChannelActivation,
        target_id: MotionTargetId | None,
        source_target_id: MotionTargetId | None,
        provider_id: int,
        provider_type: str,
        *,
        layer_override: int | None = None,
    ) -> None:
        work = _referenced_fields(self.graph, controller, field_name)
        if work is None:
            return
        layer_index = (
            layer_override
            if layer_override is not None
            else _int(work, "Layer", default=-1)
        )
        layer_index = _optional_index(layer_index)
        choices = []

        def add(key: str, choice_label: str, motion_id: int) -> None:
            if motion_id not in _NO_MOTION_IDS:
                choices.append(MotionChoiceReference(key, choice_label, motion_id))

        add(
            "reaction:s",
            "Reaction S",
            _int(work, "ReactionSMotionNo", default=-1),
        )
        add(
            "reaction:l",
            "Reaction L",
            _int(work, "ReactionLMotionNo", default=-1),
        )
        for size, field in (("S", "DialogSMotions"), ("L", "DialogLMotions")):
            for index, motion_id in enumerate(
                _int_array(work, field, default=())
            ):
                add(
                    f"dialog:{size.lower()}:{index}",
                    f"Dialog {size} {index + 1}",
                    motion_id,
                )
        problem = self._layer_problem(target_id, layer_index)
        if not choices:
            problem = _join_problem(problem, "PFB work contains no lip motions")
        self.channels.append(
            MotionChannelDefinition(
                key=f"{provider_id}:{field_name}:{label}",
                label=label,
                kind=MotionChannelKind.LIP_SYNC,
                activation=activation,
                target_id=target_id,
                layer_index=layer_index,
                bank_id=_int(work, "BankID", default=0),
                choices=tuple(choices),
                fade_in_frames=_nonnegative_float(
                    work,
                    "InterpolationFrame",
                    default=0.0,
                ),
                source_bank_target_id=source_target_id,
                provider_type=provider_type,
                provider_instance_id=provider_id,
                provider_object_id=self.graph.owner_of(provider_id),
                properties=(
                    RuntimeProperty("Work", field_name),
                    RuntimeProperty(
                        "Keep reaction frames",
                        _finite_float(
                            work,
                            "KeepReactionFrame",
                            default=0.0,
                        ),
                    ),
                    RuntimeProperty(
                        "Keep dialog frames",
                        _finite_float(
                            work,
                            "KeepDialogFrame",
                            default=0.0,
                        ),
                    ),
                    RuntimeProperty(
                        "Reaction frames",
                        _finite_float(work, "ReactionFrame", default=0.0),
                    ),
                    RuntimeProperty(
                        "Dialog frames",
                        _finite_float(work, "DialogFrame", default=0.0),
                    ),
                    RuntimeProperty(
                        "Use S motions for dialog L",
                        _bool(
                            work,
                            "IsUseSMotionsOnDialogL",
                            default=False,
                        ),
                    ),
                ),
                unresolved_reason=problem,
            )
        )

    def _append_blink(
        self,
        controller: Mapping[str, object],
        controller_enabled: bool,
        target_id: MotionTargetId | None,
        provider_id: int,
        provider_type: str,
    ) -> None:
        work = _referenced_fields(self.graph, controller, "BlinkData")
        if work is None:
            return
        layer_index = _optional_index(_int(work, "Layer", default=-1))
        motion_id = _int(work, "MotionID", default=-1)
        choices = (
            (MotionChoiceReference("blink", "Blink", motion_id),)
            if motion_id not in _NO_MOTION_IDS
            else ()
        )
        enabled = controller_enabled and _bool(
            controller,
            "IsBlinkEnabled",
            default=False,
        )
        problem = self._layer_problem(target_id, layer_index)
        if not choices:
            problem = _join_problem(problem, "PFB work contains no blink motion")
        self.channels.append(
            MotionChannelDefinition(
                key=f"{provider_id}:BlinkData",
                label="Blink",
                kind=MotionChannelKind.BLINK,
                activation=(
                    MotionChannelActivation.RUNTIME_CONTROLLED
                    if enabled
                    else MotionChannelActivation.INACTIVE
                ),
                target_id=target_id,
                layer_index=layer_index,
                bank_id=_int(work, "BankID", default=0),
                choices=choices,
                fade_in_frames=_nonnegative_float(
                    work,
                    "InterpolationFrame",
                    default=0.0,
                ),
                source_bank_target_id=target_id,
                provider_type=provider_type,
                provider_instance_id=provider_id,
                provider_object_id=self.graph.owner_of(provider_id),
                properties=(
                    RuntimeProperty(
                        "Interval",
                        _finite_float(work, "Interval", default=0.0),
                    ),
                    RuntimeProperty(
                        "Random interval",
                        _finite_float(work, "RandomInterval", default=0.0),
                    ),
                ),
                unresolved_reason=problem,
            )
        )

    def _read_motion_fsms(self) -> None:
        for provider_id in self.graph.instances_of(self.profile.motion_fsm_type):
            provider = self.graph.fields(provider_id)
            target_id = self._target_for_object(
                self.graph.owner_of(provider_id),
                f"{self.graph.type_name(provider_id)} owner",
            )
            provider_enabled = _bool(provider, "Enabled", default=True)
            for index, layer_id in enumerate(
                _int_array(provider, "v12_Layer", default=())
            ):
                fields = self.graph.fields(layer_id)
                if not fields:
                    self.diagnostics.append(
                        f"MotionFsm2 component {provider_id} layer {index} is unresolved."
                    )
                    continue
                layer_index = _optional_index(
                    _int(fields, "TargetMotionLayerNo", default=-1)
                )
                problem = self._layer_problem(target_id, layer_index)
                problem = _join_problem(
                    problem,
                    "active motion requires the external FSM/UVAR state",
                )
                self.channels.append(
                    MotionChannelDefinition(
                        key=f"{provider_id}:fsm:{index}",
                        label=f"Motion FSM {index + 1}",
                        kind=MotionChannelKind.MOTION_FSM,
                        activation=(
                            MotionChannelActivation.RUNTIME_CONTROLLED
                            if provider_enabled
                            and _bool(fields, "Enabled", default=True)
                            else MotionChannelActivation.INACTIVE
                        ),
                        target_id=target_id,
                        layer_index=layer_index,
                        bank_id=None,
                        resource_path=_string(
                            fields,
                            "MotionFsm2Resource",
                            default="",
                        ),
                        provider_type=self.graph.type_name(provider_id),
                        provider_instance_id=provider_id,
                        provider_object_id=self.graph.owner_of(provider_id),
                        properties=(
                            RuntimeProperty(
                                "Execute layer",
                                _int(
                                    fields,
                                    "ExecuteMotionLayerNo(?)",
                                    default=-1,
                                ),
                            ),
                            RuntimeProperty(
                                "Joint mask override",
                                _bool(
                                    fields,
                                    "OverwriteJointMaskID",
                                    default=False,
                                ),
                            ),
                            RuntimeProperty(
                                "Blend mode",
                                _int(fields, "BlendMode", default=0),
                            ),
                            RuntimeProperty(
                                "Blend rate",
                                _finite_float(
                                    fields,
                                    "BlendRate",
                                    default=1.0,
                                ),
                            ),
                        ),
                        unresolved_reason=problem,
                    )
                )

    def _read_actor_motion(self) -> None:
        for provider_id in self.graph.instances_of(self.profile.actor_motion_type):
            provider = self.graph.fields(provider_id)
            target_id = self._target_for_object(
                self.graph.owner_of(provider_id),
                f"{self.graph.type_name(provider_id)} owner",
            )
            provider_enabled = _bool(provider, "v0_Enabled", default=True)
            for index, layer_id in enumerate(
                _int_array(provider, "v4_Layer", default=())
            ):
                fields = self.graph.fields(layer_id)
                if not fields:
                    self.diagnostics.append(
                        f"ActorMotion component {provider_id} layer {index} is unresolved."
                    )
                    continue
                layer_index = _optional_index(
                    _int(fields, "v0_TargetLayerNo", default=-1)
                )
                motion_id = _int(fields, "v5_MotionID", default=-1)
                choices = (
                    (
                        MotionChoiceReference(
                            f"actor:{index}",
                            "Authored actor motion",
                            motion_id,
                        ),
                    )
                    if motion_id not in _NO_MOTION_IDS
                    else ()
                )
                problem = self._layer_problem(target_id, layer_index)
                if not choices:
                    problem = _join_problem(
                        problem,
                        "active motion is selected by event/timeline state",
                    )
                self.channels.append(
                    MotionChannelDefinition(
                        key=f"{provider_id}:actor:{index}",
                        label=f"Actor motion {index + 1}",
                        kind=MotionChannelKind.ACTOR_MOTION,
                        activation=(
                            MotionChannelActivation.RUNTIME_CONTROLLED
                            if provider_enabled
                            else MotionChannelActivation.INACTIVE
                        ),
                        target_id=target_id,
                        layer_index=layer_index,
                        bank_id=_int(fields, "v4_MotionBankID", default=0),
                        choices=choices,
                        fade_in_frames=_nonnegative_float(
                            fields,
                            "v10_InterpolationFrame",
                            default=0.0,
                        ),
                        source_bank_target_id=target_id,
                        resource_path=_string(
                            fields,
                            "v3_Resource",
                            default="",
                        ),
                        provider_type=self.graph.type_name(provider_id),
                        provider_instance_id=provider_id,
                        provider_object_id=self.graph.owner_of(provider_id),
                        properties=(
                            RuntimeProperty(
                                "Blend mode",
                                _int(fields, "v1_BlendMode", default=0),
                            ),
                            RuntimeProperty(
                                "Blend rate",
                                _finite_float(
                                    fields,
                                    "v2_BlendRate",
                                    default=1.0,
                                ),
                            ),
                            RuntimeProperty(
                                "Frame",
                                _finite_float(fields, "v6_Frame", default=0.0),
                            ),
                            RuntimeProperty(
                                "Speed",
                                _finite_float(fields, "v7_Speed", default=1.0),
                            ),
                        ),
                        unresolved_reason=problem,
                    )
                )

    def _read_observers(self) -> None:
        seen: set[tuple[int, str]] = set()
        for specification in self.profile.observers:
            for provider_id in self.graph.instances_of(specification.base_type):
                identity = provider_id, specification.field_name
                if identity in seen:
                    continue
                seen.add(identity)
                fields = self.graph.fields(provider_id)
                layer_index = _optional_index(
                    _int(fields, specification.field_name, default=-1)
                )
                target_id = self._target_for_object(
                    self.graph.owner_of(provider_id),
                    f"{self.graph.type_name(provider_id)} owner",
                )
                self.observers.append(
                    MotionLayerObserver(
                        specification.label,
                        target_id,
                        layer_index,
                        self.graph.type_name(provider_id),
                        provider_id,
                        self.graph.owner_of(provider_id),
                        self._layer_problem(target_id, layer_index),
                    )
                )

    def _read_scene_state_bindings(self) -> None:
        for specification in self.profile.scene_states:
            for provider_id in self.graph.instances_of(
                specification.provider_type
            ):
                targets = []
                for target in specification.targets:
                    object_id = self.graph.reference_target(
                        provider_id,
                        target.reference_property_id,
                    )
                    if object_id is None:
                        self.diagnostics.append(
                            f"{specification.provider_type} "
                            f"{target.reference_name} is unresolved."
                        )
                        targets = []
                        break
                    targets.append(MotionSceneObjectState(
                        object_id=object_id,
                        visible_when_active=target.visible_when_active,
                        visible_when_inactive=target.visible_when_inactive,
                        parts=tuple(
                            MotionScenePartState(*part)
                            for part in target.parts
                        ),
                    ))
                if targets:
                    self.scene_state_bindings.append(MotionSceneStateBinding(
                        provider_type=specification.provider_type,
                        provider_instance_id=provider_id,
                        source_node_type=specification.source_node_type,
                        source_property=specification.source_property,
                        default_value=specification.default_value,
                        active_values=specification.active_values,
                        targets=tuple(targets),
                    ))

    def _target_for_object(
        self,
        object_id: int | None,
        relationship: str,
    ) -> MotionTargetId | None:
        if object_id is None:
            self.diagnostics.append(f"{relationship} is unresolved.")
            return None
        candidates = self.targets_by_object.get(object_id, ())
        if len(candidates) != 1:
            self.diagnostics.append(
                f"{relationship} resolves to {len(candidates)} Motion components "
                f"on GameObject {object_id}."
            )
            return None
        return candidates[0]

    def _layer_problem(
        self,
        target_id: MotionTargetId | None,
        layer_index: int | None,
    ) -> str:
        target = self.targets.get(target_id) if target_id is not None else None
        if target is None:
            return "target Motion is unresolved"
        if layer_index is None:
            return "target layer is runtime-selected"
        slot = target.layer(layer_index)
        if slot is None:
            return (
                f"layer {layer_index} is outside the target's "
                f"{len(target.layers)} slots"
            )
        if slot.definition is None:
            return slot.diagnostic or f"layer {layer_index} is unresolved"
        return ""

    def _expression_name(self, expression: int) -> str:
        names = self.profile.expression_names
        return (
            names[expression]
            if 0 <= expression < len(names)
            else f"Expression {expression}"
        )

    @staticmethod
    def _flag_activation(active: bool) -> MotionChannelActivation:
        return (
            MotionChannelActivation.ACTIVE
            if active
            else MotionChannelActivation.INACTIVE
        )


def _read_material_controllers(
    graph: PfbRuntimeGraph,
    profile: Dmc5MotionRuntimeProfile,
) -> tuple[MotionMaterialController, ...]:
    controllers = []
    for instance_id in graph.instances_of(
        profile.bake_blend_texture_controller_type
    ):
        owner_id = graph.owner_of(instance_id)
        if owner_id is None:
            raise MotionRuntimeContextError(
                f"blend-texture controller {instance_id} has no owning GameObject"
            )
        fields = graph.fields(instance_id)
        blend_type = _int(fields, "_BlendTextureType")
        legacy = _bool(fields, "IsRegacyMotion", default=False)
        try:
            source_names = profile.wrinkle_names(
                blend_type,
                legacy=legacy,
            )
        except ValueError as exc:
            raise MotionRuntimeContextError(
                f"blend-texture controller {instance_id}: {exc}"
            ) from exc

        corrections: dict[int, tuple[float, float]] = {}
        if _bool(fields, "IsWrinkleWeightCorrection", default=False):
            for correction_id in _int_array(
                fields,
                "WrinkleWeightCorrectionDataList",
                default=(),
            ):
                correction = graph.fields(correction_id)
                part = _int(correction, "_Parts")
                if not 0 <= part < len(source_names):
                    raise MotionRuntimeContextError(
                        f"blend-texture correction part {part} exceeds "
                        f"type {blend_type}'s {len(source_names)} wrinkle inputs"
                    )
                if part in corrections:
                    raise MotionRuntimeContextError(
                        f"blend-texture correction part {part} is duplicated"
                    )
                maximum = _finite_float(correction, "_MaxWeight")
                exponent = _finite_float(correction, "_Ratio")
                if maximum <= 0.0 or exponent <= 0.0:
                    raise MotionRuntimeContextError(
                        f"blend-texture correction part {part} has a "
                        "nonpositive maximum or exponent"
                    )
                corrections[part] = maximum, exponent

        head_material = _string(fields, "BlendTextureMaterialName")
        if not head_material:
            raise MotionRuntimeContextError(
                f"blend-texture controller {instance_id} has no head material"
            )
        parameters = [
            MotionMaterialParameter(
                source_name,
                head_material,
                f"Weight{index + 1}",
                *corrections.get(index, (1.0, 1.0)),
            )
            for index, source_name in enumerate(source_names)
        ]
        teeth_material = _string(
            fields,
            "BlendTextureTeethMaterialName",
            default="",
        )
        if teeth_material:
            try:
                jaw = profile.jaw_open_index(blend_type)
            except ValueError as exc:
                raise MotionRuntimeContextError(
                    f"blend-texture controller {instance_id}: {exc}"
                ) from exc
            if not 0 <= jaw < len(source_names):
                raise MotionRuntimeContextError(
                    f"blend-texture jaw index {jaw} exceeds "
                    f"type {blend_type}'s {len(source_names)} wrinkle inputs"
                )

            parameters.append(
                MotionMaterialParameter(
                    source_names[jaw],
                    teeth_material,
                    "Weight1",
                )
            )
        controllers.append(MotionMaterialController(
            profile.bake_blend_texture_controller_type,
            instance_id,
            owner_id,
            tuple(parameters),
        ))
    return tuple(controllers)


def _referenced_fields(
    graph: PfbRuntimeGraph,
    fields: Mapping[str, object],
    name: str,
) -> Mapping[str, object] | None:
    instance_id = _int(fields, name, default=0)
    if instance_id <= 0:
        return None
    result = graph.fields(instance_id)
    return result or None


def _optional_index(value: int) -> int | None:
    return None if value in _NO_MOTION_IDS else value


def _join_problem(left: str, right: str) -> str:
    return "; ".join(item for item in (left, right) if item)


class Dmc5EntityMotionBackend:
    game_version = "DMC5"

    @staticmethod
    def adapt(parsed: object) -> MotionRuntimeScene:
        return adapt_dmc5_motion_runtime_scene(parsed)

    @staticmethod
    def parse_motion_bank(data: bytes) -> MotionBankDefinition:
        return parse_dmc5_motion_bank(data)

    @staticmethod
    def material_controllers(
        parsed: object,
    ) -> tuple[MotionMaterialController, ...]:
        return adapt_dmc5_material_controllers(parsed)

    @staticmethod
    def parse_joint_map(
        data: bytes,
        *,
        label: str,
    ) -> JointMapDefinition:
        return parse_dmc5_joint_map(data, label=label)


DMC5_ENTITY_MOTION_BACKEND = Dmc5EntityMotionBackend()
