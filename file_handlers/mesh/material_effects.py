from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol


BASE_METAL_MAP = "BaseMetalMap"
NORMAL_ROUGHNESS_MAP = "NormalRoughnessMap"
ATLAS_WRINKLE_MASK_MAP = "AtlasWrinkleMaskMap"
ALPHA_TRANSLUCENT_OCCLUSION_SSS_MAP = "AlphaTranslucentOcclusionSSSMap"
BLEND_ATOS = "BlendATOS"
WRINKLE_DIFFUSE_MAPS = tuple(
    f"WrinkleDiffuseMap{index}"
    for index in range(1, 4)
)
WRINKLE_NORMAL_MAPS = tuple(
    f"WrinkleNormalMap{index}"
    for index in range(1, 4)
)


class _TextureProfile(Protocol):
    texture_type: str
    texture_path: str


class _SurfaceProfile(Protocol):
    game_version: str
    mmtr_path: str
    textures: tuple[_TextureProfile, ...]
    parameter_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RigLogicMaterialEffect:
    name: str
    game_versions: frozenset[str]
    templates: frozenset[str]
    weight_count: int
    texture_types: tuple[str, ...]

    def matches(self, surface: _SurfaceProfile) -> bool:
        return (
            surface.game_version.upper() in self.game_versions
            and _template_name(surface.mmtr_path) in self.templates
        )

    def validate(self, surface: _SurfaceProfile) -> None:
        _require_unique(
            self.texture_types,
            (texture.texture_type for texture in surface.textures),
            f"{self.name} texture role",
        )
        _require_unique(
            self.weight_names,
            surface.parameter_names,
            f"{self.name} parameter",
        )
        expected_weights = set(self.weight_names)
        unexpected_weights = sorted(
            name
            for name in surface.parameter_names
            if name.startswith("Weight")
            and name[6:].isdigit()
            and name not in expected_weights
        )
        if unexpected_weights:
            raise ValueError(
                f"{self.name} material has unexpected "
                f"{unexpected_weights[0]}"
            )

    @property
    def weight_names(self) -> tuple[str, ...]:
        return tuple(
            f"Weight{index}"
            for index in range(1, self.weight_count + 1)
        )


DMC5_FULL_WRINKLE_EFFECT = RigLogicMaterialEffect(
    name="DMC5 RigLogic 41-weight wrinkles",
    game_versions=frozenset({"DMC5"}),
    templates=frozenset({
        "blendtexture_riglogic.mmtr",
        "blendtexture_riglogic_astral.mmtr",
        "blendtexture_riglogic_pl0200.mmtr",
        "blendtexture_riglogic_pl0200_wet.mmtr",
        "blendtexture_riglogic_trans.mmtr",
        "blendtexture_riglogic_wet.mmtr",
        "shader_03_cs_blendtexture_riglogic_wet_00_000.mmtr",
    }),
    weight_count=41,
    texture_types=(
        BASE_METAL_MAP,
        *WRINKLE_DIFFUSE_MAPS,
        NORMAL_ROUGHNESS_MAP,
        *WRINKLE_NORMAL_MAPS,
        ATLAS_WRINKLE_MASK_MAP,
    ),
)

DMC5_PEOPLE_WRINKLE_EFFECT = RigLogicMaterialEffect(
    name="DMC5 RigLogic 24-weight wrinkles",
    game_versions=frozenset({"DMC5"}),
    templates=frozenset({
        "blendtexture_riglogic_people.mmtr",
        "blendtexture_riglogic_people_wet.mmtr",
    }),
    weight_count=24,
    texture_types=(
        BASE_METAL_MAP,
        NORMAL_ROUGHNESS_MAP,
        WRINKLE_NORMAL_MAPS[0],
        WRINKLE_NORMAL_MAPS[1],
        ATLAS_WRINKLE_MASK_MAP,
    ),
)

DMC5_TEETH_OCCLUSION_EFFECT = RigLogicMaterialEffect(
    name="DMC5 RigLogic teeth occlusion",
    game_versions=frozenset({"DMC5"}),
    templates=frozenset({
        "blendtexture_riglogic_teeth.mmtr",
        "blendtexture_riglogic_teeth_trans.mmtr",
    }),
    weight_count=1,
    texture_types=(
        BASE_METAL_MAP,
        NORMAL_ROUGHNESS_MAP,
        ALPHA_TRANSLUCENT_OCCLUSION_SSS_MAP,
        BLEND_ATOS,
    ),
)

DMC5_RIGLOGIC_EFFECTS = (
    DMC5_FULL_WRINKLE_EFFECT,
    DMC5_PEOPLE_WRINKLE_EFFECT,
    DMC5_TEETH_OCCLUSION_EFFECT,
)


def riglogic_material_effect(
    surface: _SurfaceProfile | None,
) -> RigLogicMaterialEffect | None:
    if surface is None:
        return None
    for effect in DMC5_RIGLOGIC_EFFECTS:
        if effect.matches(surface):
            effect.validate(surface)
            return effect
    return None


def material_texture_key(
    material_key: str,
    texture_type: str,
) -> str:
    if texture_type == BASE_METAL_MAP:
        return material_key
    return f"{material_key}\x1f{texture_type}"


def surface_texture_paths(
    surface: _SurfaceProfile,
) -> dict[str, str]:
    return {
        texture.texture_type: texture.texture_path
        for texture in surface.textures
    }


def _template_name(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").lower()
    return PurePosixPath(normalized).name


def _require_unique(required, available, label: str) -> None:
    values = tuple(available)
    for name in required:
        count = values.count(name)
        if count != 1:
            detail = "is missing" if count == 0 else f"appears {count} times"
            raise ValueError(f"{label} {name} {detail}")
