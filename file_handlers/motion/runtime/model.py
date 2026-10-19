from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import TypeAlias

import numpy as np


class MotionRuntimeContextError(ValueError):
    pass


class OpenIntEnum(IntEnum):
    """Keep unrecognized engine enum values inspectable instead of guessing."""

    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, int):
            return None
        member = int.__new__(cls, value)
        member._name_ = f"UNKNOWN_{value}"
        member._value_ = value
        cls._value2member_map_[value] = member
        return member

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").title()


class RuntimeBlendMode(OpenIntEnum):
    OVERWRITE = 0
    ADD_BLEND = 1
    PRIVATE = 2


class RuntimeInterpolationMode(OpenIntEnum):
    NONE = 0
    FRONT_FADE = 1
    CROSS_FADE = 2
    SYNC_CROSS_FADE = 3
    SYNC_POINT_CROSS_FADE = 4


class RuntimeInterpolationCurve(OpenIntEnum):
    LINEAR = 0
    SMOOTH = 1
    EASE_IN = 2
    EASE_OUT = 3


class RuntimeWrapMode(OpenIntEnum):
    DEFAULT = 0
    ONCE = 1
    LOOP = 2
    TURN_BACK = 3
    LOOP_TURN_BACK = 4

    @property
    def looping(self) -> bool | None:
        if self is RuntimeWrapMode.DEFAULT:
            return None
        if self is RuntimeWrapMode.ONCE:
            return False
        if self is RuntimeWrapMode.LOOP:
            return True
        return None


class RuntimeRootMotionMode(OpenIntEnum):
    NONE = 0
    FIXED = 1
    CONTINUANCE = 2
    JOINT = 3
    FIXED_WITH_SCALE = 4


class MotionChannelKind(Enum):
    AUTHORED_SOURCE = "authored_source"
    FACE_EXPRESSION = "face_expression"
    LIP_SYNC = "lip_sync"
    BLINK = "blink"
    MOTION_FSM = "motion_fsm"
    ACTOR_MOTION = "actor_motion"


class MotionChannelActivation(Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    RUNTIME_CONTROLLED = "runtime_controlled"

    @property
    def label(self) -> str:
        return {
            self.ACTIVE: "Enabled in PFB",
            self.INACTIVE: "Disabled in PFB",
            self.RUNTIME_CONTROLLED: "Triggered by game",
        }[self]


RuntimePropertyValue: TypeAlias = bool | int | float | str


@dataclass(frozen=True, slots=True, order=True)
class MotionTargetId:
    object_id: int
    component_instance_id: int


@dataclass(frozen=True, slots=True)
class MotionBankReference:
    bank_id: int
    path: str
    bank_type: int = 0
    bank_type_mask_bits: int = 0


@dataclass(frozen=True, slots=True)
class MotionBankDefinition:
    version: int
    references: tuple[MotionBankReference, ...]
    uvar_path: str = ""
    joint_map_path: str = ""


@dataclass(frozen=True, slots=True)
class TreeLayerSource:
    resource_path: str = ""
    bank_id: int = 0
    motion_id: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.resource_path) or self.motion_id is not None


@dataclass(frozen=True, slots=True)
class RuntimeLayerDefinition:
    blend_mode: RuntimeBlendMode
    weight: float
    # Serialized as BaseLayerNo; consulted by explicit normalized-time sync.
    normalized_time_source_index: int
    joint_mask_id: int
    wrap_mode: RuntimeWrapMode
    frame: float
    speed: float
    interpolation_mode: RuntimeInterpolationMode
    interpolation_curve: RuntimeInterpolationCurve
    interpolation_frames: float
    source: TreeLayerSource = TreeLayerSource()
    world_rotation_blend: bool = False
    mirror_symmetry: bool = False
    show_integer_frame: bool = False
    ignore_first_interpolation: bool = False
    unknown_flags: tuple[str, ...] = ()

    @property
    def wrap_looping(self) -> bool | None:
        return self.wrap_mode.looping


@dataclass(frozen=True, slots=True)
class MotionLayerSlot:
    index: int
    instance_id: int | None
    type_name: str
    definition: RuntimeLayerDefinition | None
    diagnostic: str = ""


@dataclass(frozen=True, slots=True)
class MotionTargetDefinition:
    id: MotionTargetId
    name: str
    enabled: bool
    motion_bank_path: str
    joint_map_path: str
    layers: tuple[MotionLayerSlot, ...]
    play_speed: float = 1.0
    root_motion_mode: RuntimeRootMotionMode = RuntimeRootMotionMode.NONE
    stop_at_motion_end: bool = False
    joint_map_expression_enabled: bool = False
    after_parent_animation: bool = False

    def layer(self, index: int) -> MotionLayerSlot | None:
        return next((item for item in self.layers if item.index == index), None)


@dataclass(frozen=True, slots=True)
class MotionChoiceReference:
    key: str
    label: str
    motion_id: int | None


@dataclass(frozen=True, slots=True)
class RuntimeProperty:
    name: str
    value: RuntimePropertyValue


@dataclass(frozen=True, slots=True)
class MotionMaterialParameter:
    source_name: str
    material_name: str
    parameter_name: str

    def __post_init__(self) -> None:
        if (
            not self.source_name
            or not self.material_name
            or not self.parameter_name
        ):
            raise ValueError("material animation names cannot be empty")

    @staticmethod
    def bounded(value: float) -> float:
        """Clamped with the controller's comparisons, so NaN gives 0."""
        value = value if value >= 0.0 else 0.0
        return value if value <= 1.0 else 1.0


@dataclass(frozen=True, slots=True)
class MotionValueCorrection:
    """``apply`` follows the game's ``powf``."""

    source_name: str
    maximum: float
    exponent: float

    def apply(self, value: float) -> float:
        with np.errstate(all="ignore"):
            raised = float(np.power(np.float32(value), np.float32(self.exponent)))
        return raised if raised <= self.maximum else self.maximum


@dataclass(frozen=True, slots=True)
class MotionMaterialController:
    """Updates its parameters each frame, or with ``held_by_channels`` only when the mesh's blend channel values change
    and never for a mesh without channels (DMC5's blend-texture controller)."""

    provider_type: str
    provider_instance_id: int
    provider_object_id: int
    parameters: tuple[MotionMaterialParameter, ...]
    corrections: tuple[MotionValueCorrection, ...] = ()
    held_by_channels: bool = False

    def __post_init__(self) -> None:
        if self.provider_instance_id <= 0 or self.provider_object_id < 0:
            raise ValueError("material controller has an invalid runtime identity")
        targets = [
            (item.material_name, item.parameter_name)
            for item in self.parameters
        ]
        if len(set(targets)) != len(targets):
            raise ValueError("material controller has duplicate parameter targets")

    @property
    def source_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.source_name for item in self.parameters))

    def values(self, sources: Mapping[str, float]) -> dict[tuple[str, str], float]:
        corrected = {name: float(sources.get(name, 0.0)) for name in self.source_names}
        for correction in self.corrections:
            if correction.source_name in corrected:
                corrected[correction.source_name] = correction.apply(corrected[correction.source_name])
        return {(item.material_name, item.parameter_name): item.bounded(corrected[item.source_name])
                for item in self.parameters}


@dataclass(frozen=True, slots=True)
class MotionScenePartState:
    part_index: int
    enabled_when_active: bool
    enabled_when_inactive: bool

    def __post_init__(self) -> None:
        if self.part_index < 0:
            raise ValueError("scene-state mesh part index cannot be negative")


@dataclass(frozen=True, slots=True)
class MotionSceneObjectState:
    object_id: int
    visible_when_active: bool | None = None
    visible_when_inactive: bool | None = None
    parts: tuple[MotionScenePartState, ...] = ()

    def __post_init__(self) -> None:
        if self.object_id < 0:
            raise ValueError("scene-state GameObject id cannot be negative")
        indices = [part.part_index for part in self.parts]
        if len(set(indices)) != len(indices):
            raise ValueError("scene-state mesh part indices must be unique")


@dataclass(frozen=True, slots=True)
class MotionSceneStateBinding:
    """A compact MotClip property driving PFB object/mesh display state."""

    provider_type: str
    provider_instance_id: int
    source_node_type: str
    source_property: str
    default_value: RuntimePropertyValue
    active_values: tuple[RuntimePropertyValue, ...]
    targets: tuple[MotionSceneObjectState, ...]

    def __post_init__(self) -> None:
        if (
            not self.provider_type
            or self.provider_instance_id <= 0
            or not self.source_node_type
            or not self.source_property
        ):
            raise ValueError("scene-state binding has an invalid source")
        if not self.active_values or not self.targets:
            raise ValueError("scene-state binding must have values and targets")


@dataclass(frozen=True, slots=True)
class MotionChannelDefinition:
    key: str
    label: str
    kind: MotionChannelKind
    activation: MotionChannelActivation
    target_id: MotionTargetId | None
    layer_index: int | None
    bank_id: int | None
    choices: tuple[MotionChoiceReference, ...] = ()
    default_choice_key: str | None = None
    fade_in_frames: float = 0.0
    fade_out_frames: float = 0.0
    source_bank_target_id: MotionTargetId | None = None
    resource_path: str = ""
    provider_type: str = ""
    provider_instance_id: int | None = None
    provider_object_id: int | None = None
    properties: tuple[RuntimeProperty, ...] = ()
    unresolved_reason: str = ""
    # For a channel the game's script replays by itself: frames between motion starts (``LayerRepeat``); 0 otherwise.
    repeat_period_frames: float = 0.0


@dataclass(frozen=True, slots=True)
class MotionLayerObserver:
    label: str
    target_id: MotionTargetId | None
    layer_index: int | None
    provider_type: str
    provider_instance_id: int
    provider_object_id: int | None = None
    unresolved_reason: str = ""


@dataclass(frozen=True, slots=True)
class MotionRuntimeScene:
    targets: tuple[MotionTargetDefinition, ...]
    channels: tuple[MotionChannelDefinition, ...] = ()
    observers: tuple[MotionLayerObserver, ...] = ()
    diagnostics: tuple[str, ...] = ()
    material_controllers: tuple[MotionMaterialController, ...] = ()
    scene_state_bindings: tuple[MotionSceneStateBinding, ...] = ()

    def target(self, target_id: MotionTargetId | None) -> MotionTargetDefinition | None:
        if target_id is None:
            return None
        return next((item for item in self.targets if item.id == target_id), None)

    def observers_for(
        self,
        target_id: MotionTargetId,
    ) -> tuple[MotionLayerObserver, ...]:
        return tuple(item for item in self.observers if item.target_id == target_id)
