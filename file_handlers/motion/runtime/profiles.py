from __future__ import annotations

from dataclasses import dataclass

from ..profiles import DMC5_FULL_WRINKLE_NAMES, DMC5_LEGACY_WRINKLE_NAMES, DMC5_MOB_WRINKLE_NAMES


@dataclass(frozen=True, slots=True)
class LayerObserverProfile:
    base_type: str
    field_name: str
    label: str


@dataclass(frozen=True, slots=True)
class SceneStateTargetProfile:
    reference_name: str
    reference_property_id: int
    visible_when_active: bool | None = None
    visible_when_inactive: bool | None = None
    # part index, enabled while active, enabled while inactive
    parts: tuple[tuple[int, bool, bool], ...] = ()


@dataclass(frozen=True, slots=True)
class SequenceSceneStateProfile:
    provider_type: str
    source_node_type: str
    source_property: str
    default_value: bool | int | float | str
    active_values: tuple[bool | int | float | str, ...]
    targets: tuple[SceneStateTargetProfile, ...]


@dataclass(frozen=True, slots=True)
class Dmc5MotionRuntimeProfile:
    game_version: str = "DMC5"
    pfb_suffix: str = ".pfb.16"
    motion_type: str = "via.motion.Motion"
    tree_layer_type: str = "via.motion.TreeLayer"
    facial_controller_type: str = (
        "app.character.CharacterFacialMotionController"
    )
    player_facial_controller_type: str = (
        "app.player.PlayerFacialMotionController"
    )
    cerberus_facial_controller_type: str = "app.Em5700FacialMotionController"
    bake_blend_texture_controller_type: str = "app.BakeBlendTextureController"
    motion_fsm_type: str = "via.motion.MotionFsm2"
    actor_motion_type: str = "via.motion.ActorMotion"
    monitored_object_property_id: int = 0x50001
    face_object_property_id: int = 0x60007
    expression_names: tuple[str, ...] = (
        "Empty",
        "Default",
        "AttackDefault",
        "Damage",
        "Ex00",
        "Ex01",
        "Ex02",
        "Ex03",
        "Ex04",
        "Manual",
    )
    # The game's ``BlendTextureType`` (only ``Full`` applies corrections) and ``JawOpenIndex``.
    full_blend_texture_type: int = 0
    mob_blend_texture_type: int = 1
    full_wrinkle_names: tuple[str, ...] = DMC5_FULL_WRINKLE_NAMES
    mob_wrinkle_names: tuple[str, ...] = DMC5_MOB_WRINKLE_NAMES
    legacy_wrinkle_names: tuple[str, ...] = DMC5_LEGACY_WRINKLE_NAMES
    jaw_open_index: int = 8
    observers: tuple[LayerObserverProfile, ...] = (
        LayerObserverProfile(
            "via.motion.script.FootEffectController",
            "BaseLayer",
            "Foot effects",
        ),
        LayerObserverProfile(
            "app.AfterImageShell",
            "TargetLayer",
            "Afterimage",
        ),
        LayerObserverProfile(
            "via.wwise.WwiseMotionSequence",
            "LayerThinnedOutTrigger",
            "Motion audio",
        ),
    )
    scene_states: tuple[SequenceSceneStateProfile, ...] = (
        SequenceSceneStateProfile(
            provider_type="app.player.pl0300.PlayerVergil",
            source_node_type="app.player.pl0300.PlayerVergilTrack",
            source_property="ConstShortWeapon",
            default_value=-1,
            active_values=(0, 1),
            targets=(
                SceneStateTargetProfile(
                    "YamatoModel",
                    0xB000A,
                    visible_when_active=True,
                    visible_when_inactive=False,
                ),
                SceneStateTargetProfile(
                    "YamatoSheathModel",
                    0xB000B,
                    parts=((0, False, True), (1, False, True)),
                ),
            ),
        ),
    )

    def wrinkle_names(self, blend_texture_type: int, *, legacy: bool) -> tuple[str, ...]:
        """In head-weight order, as ``getRigLogicWrinkleWeightName`` picks them."""
        if blend_texture_type == self.full_blend_texture_type and legacy:
            return self.legacy_wrinkle_names
        if blend_texture_type == self.mob_blend_texture_type:
            return self.mob_wrinkle_names
        return self.full_wrinkle_names


DMC5_RUNTIME_PROFILE = Dmc5MotionRuntimeProfile()
