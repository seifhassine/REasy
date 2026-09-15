from __future__ import annotations

from dataclasses import dataclass

from ..profiles import DMC5_FULL_WRINKLE_NAMES


def _wrinkle_names(
    *groups: tuple[int, tuple[str, ...]],
) -> tuple[str, ...]:
    return tuple(
        f"head_cm{group}_color.head_wm{group}_{name}"
        for group, names in groups
        for name in names
    )


DMC5_MOB_WRINKLE_NAMES = _wrinkle_names(
    (1, (
        "browRaise_CL",
        "browRaise_CR",
        "browRaise_L",
        "browRaise_R",
        "cheekRaiser_DL",
        "cheekRaiser_DR",
        "neckStretcher_L",
        "neckStretcher_R",
        "noseWrinkler_L",
        "noseWrinkler_R",
        "purse_DL",
        "purse_DR",
        "purse_UL",
        "purse_UR",
    )),
    (2, (
        "browDown_L",
        "browDown_R",
        "browLateral_L",
        "browLateral_R",
        "chinRaiser_DL",
        "chinRaiser_DR",
        "smile_L",
        "smile_R",
        "squintInner_L",
        "squintInner_R",
    )),
)


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
    wrinkle_name_sets: tuple[tuple[str, ...], ...] = (
        DMC5_FULL_WRINKLE_NAMES,
        DMC5_MOB_WRINKLE_NAMES,
    )
    wrinkle_jaw_open_indices: tuple[int | None, ...] = (8, None)
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

    def wrinkle_names(
        self,
        blend_texture_type: int,
        *,
        legacy: bool,
    ) -> tuple[str, ...]:
        if legacy:
            raise ValueError("legacy DMC5 wrinkle naming is not established")
        if not 0 <= blend_texture_type < len(self.wrinkle_name_sets):
            raise ValueError(
                f"unknown DMC5 blend-texture type {blend_texture_type}"
            )
        return self.wrinkle_name_sets[blend_texture_type]

    def jaw_open_index(self, blend_texture_type: int) -> int:
        if not 0 <= blend_texture_type < len(self.wrinkle_jaw_open_indices):
            raise ValueError(
                f"unknown DMC5 blend-texture type {blend_texture_type}"
            )
        index = self.wrinkle_jaw_open_indices[blend_texture_type]
        if index is None:
            raise ValueError(
                f"DMC5 blend-texture type {blend_texture_type} has no "
                "established jaw-open input"
            )
        return index


DMC5_RUNTIME_PROFILE = Dmc5MotionRuntimeProfile()
