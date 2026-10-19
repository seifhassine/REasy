from __future__ import annotations

from dataclasses import replace

from utils.hash_util import murmur3_hash_ascii

from ..models import STAGE_KEYS
from .base import (
    SdfGameProfile,
    SdfProgramLayout,
    SdfStageLayout,
    SdfTargetLayout,
    register_sdf_profile,
)
from .enum_tables import (
    binner_control_names, component_type_names, input_formats, system_value_names, topology_names,
)


DMC5_SDF_RESOURCE_VERSION = 1808168797


PROGRAM_LAYOUT_0X108 = SdfProgramLayout(
    name_pointer_offset=0x00,
    input_signature_pointer_offset=0x08,
    input_layout_pointer_offset=0x40,
    rasterizer_state_pointer_offset=0x48,
    blend_state_pointer_offset=0x50,
    depth_stencil_state_pointer_offset=0x58,
    compact_declaration_pointer_offset=0x60,
    resource_binding_pointer_offsets=(0x68, 0x78, 0x88, 0x98, 0xA8),
    resource_metadata_pointer_offsets=(0x70, 0x80, 0x90, 0xA0, 0xB0),
    input_signature_size_offset=0xB8,
    total_stage_binding_count_offset=0xD4,
    per_stage_binding_counts_offset=0xD8,
    distinct_resource_count_offsets=(0xF6, 0xF7, 0xFC, 0xFD, 0xFE),
    compact_declaration_count_offset=0xF8,
    reserved_tail_offset=0xFF,
    compute_group_size_offset=0x100,
    primitive_topology_offset=0x106,
    input_layout_header_size=0x08,
    input_element_size=0x08,
    compact_declaration_size=0x04,
    binding_descriptor_size=0x08,
    constant_buffer_metadata_size=0x20,
    constant_variable_size=0x10,
    named_resource_metadata_size=0x10,
)


PROGRAM_LAYOUT_0X110 = replace(
    PROGRAM_LAYOUT_0X108,
    compute_group_size_offset=0x108,
    primitive_topology_offset=0x10E,
)


STAGES = (
    SdfStageLayout("vertex", 0x10, 0xBC, 0x01),
    SdfStageLayout("hull", 0x18, 0xC0, 0x02),
    SdfStageLayout("domain", 0x20, 0xC4, 0x04),
    SdfStageLayout("geometry", 0x28, 0xC8, 0x08),
    SdfStageLayout("pixel", 0x30, 0xCC, 0x10),
    SdfStageLayout("compute", 0x38, 0xD0, 0x20),
)


# Remap tables applied before creating D3D11 state objects.
FILL_MODE_TABLE = (0, 2, 3, 0)
CULL_MODE_TABLE = (0, 1, 2, 3)
BLEND_FACTOR_TABLE = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 19)
BLEND_OPERATION_TABLE = (0, 1, 2, 3, 4, 5, 0, 1)
DEPTH_WRITE_MASK_TABLE = (0, 1, 0, 0)
COMPARISON_FUNCTION_TABLE = (0, 1, 2, 3, 4, 5, 6, 7, 8, 0, 0, 0, 0, 0, 0, 0)
STENCIL_OPERATION_TABLE = (0, 1, 2, 3, 4, 5, 6, 7, 8, 0, 0, 0, 0, 0, 0, 0)


SEMANTIC_NAMES = {
    0: "POSITION",
    1: "NORMAL",
    2: "BINORMAL",
    3: "TANGENT",
    4: "TEXCOORD",
    5: "INDEX",
    6: "WEIGHT",
    7: "COLOR",
    8: "SV_VERTEXID",
    9: "GENERIC",
    10: "SV_INSTANCEID",
    11: "UNIQUE_UV",
    12: "TESSPARAM",
    13: "GROUPID",
    14: "P",
    15: "S",
    16: "T",
    17: "Q",
}


RESOURCE_TYPE_NAMES = {
    "constant_buffer": {0xFF: "constant-buffer"},
    "sampler": {0x00: "sampler"},
    "shader_resource": {
        0x02: "texture2d-family-srv",
        0x03: "texture3d-srv",
        0x04: "texture-cube-srv",
        0x05: "texture1d-array-srv",
        0x06: "texture2d-array-srv",
        0x80: "buffer-srv",
    },
    "unordered_access": {
        0x0D: "texture2d-uav",
        0x0E: "texture3d-uav",
        0x0F: "texture1d-array-uav",
        0x10: "texture2d-array-uav",
        0x80: "buffer-uav",
    },
    "optional_resource": {},
}


def _targets(count: int) -> tuple[SdfTargetLayout, ...]:
    names = (
        ("baseline", "Baseline", None),
        ("gen2", "GraphicsFeatures::Gen2", 28),
        ("gen3", "GraphicsFeatures::Gen3", 29),
        ("gen4", "GraphicsFeatures::Gen4", 30),
        ("gen5", "GraphicsFeatures::Gen5", 31),
    )
    return tuple(SdfTargetLayout(*entry) for entry in names[:count])


_BINDLESS = 0x02
# Bit 0x04 is VRS in Rise and doubles from RE4 on; see docs/internals/sdf-profiles.md, Requirement flags.
_MHR_VRS = 0x04
_DOUBLES = 0x04
_MINIMUM_PRECISION = 0x08
_TYPED_UAV_LOAD_ADDITIONAL_FORMATS = 0x10
_LATE_VRS = 0x80
_STENCIL_REFERENCE = 0x40
_TEXTURE1D_UAV = {"unordered_access": {0x0C: "texture1d-uav"}}
_RT_TEXTURE1D_TYPES = {
    "shader_resource": {0x00: "raytracing-acceleration-structure-srv", 0x01: "texture1d-srv"},
    **_TEXTURE1D_UAV,
}


def _requirements(*, rise_vrs: bool = False, late: bool = False, typed_uav: bool = False, stencil: bool = False,
                  doubles: bool = False):
    result = {_BINDLESS: "bindless-resource-indexing"}
    if rise_vrs:
        result[_MHR_VRS] = "variable-rate-shading"
    if late:
        result[_MINIMUM_PRECISION] = "minimum-precision"
        result[_LATE_VRS] = "variable-rate-shading"
    if typed_uav:
        result[_TYPED_UAV_LOAD_ADDITIONAL_FORMATS] = "typed-UAV-load-additional-formats"
    if stencil:
        result[_STENCIL_REFERENCE] = "stencil-reference"
    if doubles:
        result[_DOUBLES] = "doubles"
    return result


def _game_tables(game: str) -> dict:
    return dict(
        input_formats=input_formats(game), system_value_names=system_value_names(game),
        component_type_names=component_type_names(game), primitive_topology_names=topology_names(game),
        rasterizer_binner_control_names=binner_control_names(game),
    )


def _merged_types(base: dict, extra) -> dict:
    return {kind: {**names, **(extra or {}).get(kind, {})} for kind, names in base.items()}


def _slot_bindings(pairs) -> frozenset:
    return frozenset((role, name, *_RENDERER_SLOTS[name]) for role, names in pairs for name in names.split())


# Renderer root-signature slots: name -> (register class, bind point, space).
_RENDERER_SLOTS = {
    "PointWrap": ("s", 0, 32), "PointClamp": ("s", 1, 32), "PointBorder": ("s", 2, 32),
    "PointMirror": ("s", 3, 32), "BilinearWrap": ("s", 4, 32), "BilinearClamp": ("s", 5, 32),
    "BilinearBorder": ("s", 6, 32), "BilinearMirror": ("s", 7, 32), "TrilinearWrap": ("s", 8, 32),
    "TrilinearClamp": ("s", 9, 32), "TrilinearBorder": ("s", 10, 32), "TrilinearMirror": ("s", 11, 32),
    "LinearCompare": ("s", 12, 32), "RootConstant": ("b", 0, 32),
    "bindlessBuffer": ("t", 0, 1), "bindlessTexture1D": ("t", 0, 2), "bindlessTexture2D": ("t", 0, 4),
    "bindlessTexture2DArray": ("t", 0, 5), "bindlessTexture3D": ("t", 0, 6), "bindlessTextureCube": ("t", 0, 7),
    "EventBlood_MaskMap": ("t", 32, 0), "RainAddressBuffer": ("t", 33, 0), "CharacterFadeBuffer": ("t", 34, 0),
    "g_NvidiaExt": ("u", 0, 0xE7AB1E),
}


def _profile(
    *,
    game: str,
    display_name: str,
    aliases: tuple[str, ...],
    versions: tuple[int, ...],
    target_count: int,
    record_size: int = 0x108,
    header_layout: str = "uniform",
    rasterizer_binner_control: int = 0,
    requirements: dict[int, str] | None = None,
    spirv_target_indices: tuple[int, ...] = (),
    resource_types: dict[str, dict[int, str]] | None = None,
    meshlet_stages: str = "",
    root_slots: str = "",
    implicit_resource_stages: tuple[str, ...] = STAGE_KEYS,
    stage_slots: dict[str, str] | None = None,
    variant_of: str = "",
    constant_variable_size_64k: bool = False,
    conservative_rasterization_supported: bool = False,
    raytracing_libraries: bool = False,
    raytracing_export_layout: str = "standard",
    runtime_cache_keys: bool = False,
) -> SdfGameProfile:
    return register_sdf_profile(
        SdfGameProfile(
            game=game,
            variant_of=variant_of,
            constant_variable_size_64k=constant_variable_size_64k,
            conservative_rasterization_supported=conservative_rasterization_supported,
            raytracing_libraries=raytracing_libraries,
            raytracing_export_layout=raytracing_export_layout,
            runtime_cache_keys=runtime_cache_keys,
            display_name=display_name,
            aliases=aliases,
            resource_versions=frozenset(versions),
            record_size=record_size,
            program_layout=(
                PROGRAM_LAYOUT_0X110 if record_size == 0x110 else PROGRAM_LAYOUT_0X108
            ),
            stages=STAGES,
            targets=_targets(target_count),
            semantic_names=dict(SEMANTIC_NAMES),
            **_game_tables(game),
            resource_type_names=_merged_types(RESOURCE_TYPE_NAMES, resource_types),
            binary_editing=True,
            metadata_name_hash=murmur3_hash_ascii,
            fill_mode_table=FILL_MODE_TABLE,
            cull_mode_table=CULL_MODE_TABLE,
            blend_factor_table=BLEND_FACTOR_TABLE,
            blend_operation_table=BLEND_OPERATION_TABLE,
            depth_write_mask_table=DEPTH_WRITE_MASK_TABLE,
            comparison_function_table=COMPARISON_FUNCTION_TABLE,
            stencil_operation_table=STENCIL_OPERATION_TABLE,
            header_layout=header_layout,
            reserved_record_ranges=((0x100, 8),) if record_size == 0x110 else (),
            rasterizer_binner_control=rasterizer_binner_control,
            program_requirement_names=dict(requirements or {}),
            spirv_target_indices=frozenset(spirv_target_indices),
            shader_role_stages=(
                {
                    "hull": frozenset(("hull", "mesh")),
                    **(
                        {"domain": frozenset(("domain", "amplification"))}
                        if meshlet_stages == "mesh-amplification"
                        else {}
                    ),
                }
                if meshlet_stages
                else {}
            ),
            implicit_resource_bindings=_slot_bindings(
                (*((role, root_slots) for role in implicit_resource_stages), *(stage_slots or {}).items())),
            default_for_version=True,
        )
    )


DMC5_SDF_PROFILE = _profile(
    game="DMC5",
    display_name="Devil May Cry 5",
    aliases=("Devil May Cry V",),
    versions=(DMC5_SDF_RESOURCE_VERSION,),
    target_count=4,
)
RE2_SDF_PROFILE = _profile(
    game="RE2",
    display_name="Resident Evil 2",
    aliases=("Resident Evil 2 / Biohazard RE:2",),
    versions=(1808160001,),
    target_count=4,
)
RE3_SDF_PROFILE = _profile(
    game="RE3",
    display_name="Resident Evil 3",
    aliases=(),
    versions=(1905100741,),
    target_count=4,
    # Resistance NormalOffsetTexture reflects Texture1D (compact SRV type 0x01).
    resource_types={"shader_resource": {0x01: "texture1d-srv"}},
)
RE_RT_SDF_PROFILE = _profile(
    game="RE2RT",
    display_name="Resident Evil 2 Ray Tracing",
    # Earlier names, from when RE3 RT shared this profile.
    aliases=("RE2_RE3_RT", "RE_RT", "RE2 / RE3 RT", "Resident Evil 2 / 3 Ray Tracing"),
    versions=(2109101635,),
    target_count=5,
    raytracing_libraries=True,
    raytracing_export_layout="legacy-nine",
    rasterizer_binner_control=1,
    requirements=_requirements(),
    spirv_target_indices=(4,),
    resource_types={
        # BVH is an acceleration structure in native compute/DXR targets; the Vulkan
        # target types Texture2DMS composites as 0x08.
        "shader_resource": {
            0x00: "raytracing-acceleration-structure-srv",
            0x01: "texture1d-srv",
            0x08: "texture2d-ms-srv",
        },
        **_TEXTURE1D_UAV,
        "sampler": {0x19: "gl-family-sampler"},
        "optional_resource": {0x00: "gl-family-buffer"},
    },
    stage_slots={
        "vertex": "BilinearWrap BilinearBorder PointWrap bindlessTexture3D",
        "pixel": (
            "BilinearWrap BilinearBorder PointWrap PointClamp PointBorder TrilinearClamp "
            "bindlessTexture2DArray bindlessTexture3D bindlessTextureCube"
        ),
        "compute": "BilinearWrap BilinearBorder PointWrap bindlessTexture3D",
    },
    root_slots=(
        "BilinearClamp BilinearMirror LinearCompare RootConstant TrilinearMirror "
        "bindlessTexture2D"
    ),
)
RE8_SDF_PROFILE = _profile(
    game="RE8",
    display_name="Resident Evil Village",
    aliases=("Resident Evil 8", "Resident Evil Village / Biohazard Village"),
    versions=(2102188797,),
    target_count=5,
    # Tag, six export names (no VFX pair), then DXIL at 0xE0.
    raytracing_libraries=True,
    raytracing_export_layout="legacy-six",
    rasterizer_binner_control=1,
    requirements=_requirements(),
    spirv_target_indices=(4,),
    resource_types={
        "shader_resource": {
            0x00: "raytracing-acceleration-structure-srv",
            0x01: "texture1d-srv",
            0x08: "texture2d-ms-srv",
        },
        **_TEXTURE1D_UAV,
        "sampler": {0x19: "gl-family-sampler"},
        "optional_resource": {0x00: "gl-family-buffer"},
    },
    # Renderer descriptor arrays declared outside the space-0 SDF bindings.
    stage_slots={
        "vertex": "bindlessTexture2D bindlessTexture3D",
        "pixel": "bindlessTexture2D bindlessTexture2DArray bindlessTextureCube",
        "compute": "bindlessTexture2D bindlessTexture3D",
        "domain": "bindlessTexture2D",
    },
    root_slots="RootConstant",
)
MHRISE_SDF_PROFILE = _profile(
    game="MHRISE",
    display_name="Monster Hunter Rise",
    aliases=("MHR", "MonsterHunterRise"),
    versions=(220427553,),
    target_count=3,
    raytracing_libraries=True,
    raytracing_export_layout="legacy-nine",
    rasterizer_binner_control=1,
    requirements=_requirements(rise_vrs=True),
    resource_types=_RT_TEXTURE1D_TYPES,
    stage_slots={
        "pixel": "TrilinearClamp PointClamp PointBorder BilinearBorder",
        "vertex": "BilinearBorder PointWrap",
        "compute": "BilinearBorder PointWrap",
    },
    root_slots=(
        "RootConstant BilinearClamp BilinearMirror BilinearWrap LinearCompare "
        "TrilinearMirror TrilinearWrap bindlessTexture2D bindlessTextureCube"
    ),
)
RE4_SDF_PROFILE = _profile(
    runtime_cache_keys=True,
    game="RE4",
    display_name="Resident Evil 4",
    aliases=("Resident Evil 4 Remake",),
    versions=(221007879,),
    target_count=3,
    rasterizer_binner_control=1,
    requirements=_requirements(late=True, typed_uav=True, doubles=True),
    constant_variable_size_64k=True,
    conservative_rasterization_supported=True,
    resource_types=_RT_TEXTURE1D_TYPES,
    stage_slots={
        "pixel": "TrilinearClamp PointClamp PointBorder TrilinearWrap PointWrap BilinearBorder",
        "vertex": "bindlessTexture3D TrilinearWrap",
        # TrilinearClamp: strand.sdf's fine rasters (fineRaster*, preTransformedFineRaster*).
        "compute": "bindlessTexture3D LinearCompare PointWrap TrilinearClamp",
        "domain": "PointWrap",
    },
    meshlet_stages="mesh",
    root_slots=(
        "BilinearClamp BilinearMirror BilinearWrap RootConstant TrilinearMirror "
        "bindlessBuffer bindlessTexture2D bindlessTexture2DArray bindlessTextureCube"
    ),
)
SF6_SDF_PROFILE = _profile(
    runtime_cache_keys=True,
    game="SF6",
    display_name="Street Fighter 6",
    aliases=("StreetFighter6",),
    versions=(221102761, 250109895),
    target_count=3,
    rasterizer_binner_control=1,
    requirements=_requirements(late=True, typed_uav=True, doubles=True),
    constant_variable_size_64k=True,
    conservative_rasterization_supported=True,
    resource_types=_RT_TEXTURE1D_TYPES,
    stage_slots={
        "pixel": (
            "BilinearBorder TrilinearClamp TrilinearWrap PointClamp PointBorder PointMirror "
            "bindlessTexture2DArray"
        ),
        "vertex": "TrilinearWrap TrilinearClamp PointClamp",
        "compute": "TrilinearWrap TrilinearClamp",
    },
    meshlet_stages="mesh",
    root_slots=(
        "BilinearClamp BilinearMirror BilinearWrap LinearCompare PointWrap RootConstant "
        "TrilinearMirror bindlessBuffer bindlessTexture2D bindlessTextureCube"
    ),
)
DD2_OLD_SDF_PROFILE = _profile(
    runtime_cache_keys=True,
    game="DD2OLD",
    variant_of="DD2",
    implicit_resource_stages=("pixel",),
    display_name="Dragon's Dogma 2 (legacy)",
    aliases=("DD2 Old",),
    versions=(240112003, 240423143),
    target_count=3,
    rasterizer_binner_control=3,
    requirements=_requirements(late=True),
    meshlet_stages="mesh",
    # DD2 lightweight-VFX pixel programs bind EventBlood_MaskMap, RainAddressBuffer and CharacterFadeBuffer as root slots.
    root_slots=(
        "EventBlood_MaskMap RainAddressBuffer CharacterFadeBuffer BilinearClamp "
        "BilinearMirror BilinearWrap PointMirror TrilinearMirror bindlessTexture2D "
        "bindlessTextureCube"
    ),
)
MHWS_SDF_PROFILE = _profile(
    runtime_cache_keys=True,
    game="MHWS",
    display_name="Monster Hunter Wilds",
    aliases=("MHWILDS", "MonsterHunterWilds"),
    versions=(240906211, 250206176),
    target_count=4,
    record_size=0x110,
    rasterizer_binner_control=3,
    requirements=_requirements(late=True, typed_uav=True, stencil=True),
    constant_variable_size_64k=True,
    conservative_rasterization_supported=True,
    resource_types=_RT_TEXTURE1D_TYPES,
    meshlet_stages="mesh",
    # 240906211 GUI mask shaders omit renderer samplers (PointClamp s1, BilinearBorder) from SDF metadata.
    stage_slots={
        "pixel": (
            "BilinearBorder PointBorder PointClamp TrilinearClamp TrilinearBorder "
            "TrilinearWrap bindlessTexture1D bindlessTexture2DArray bindlessTexture3D "
            "bindlessTextureCube"
        ),
        "compute": (
            "PointClamp BilinearBorder TrilinearClamp TrilinearWrap bindlessTexture1D "
            "bindlessTexture3D"
        ),
        "vertex": "BilinearBorder TrilinearWrap bindlessTexture1D bindlessTexture3D",
        "hull": "BilinearBorder TrilinearWrap",
    },
    root_slots=(
        "BilinearClamp BilinearMirror BilinearWrap PointWrap RootConstant TrilinearMirror "
        "bindlessBuffer bindlessTexture2D"
    ),
)
RE9_SDF_PROFILE = _profile(
    runtime_cache_keys=True,
    game="RE9",
    display_name="Resident Evil Requiem",
    aliases=("Resident Evil 9",),
    versions=(251112994,),
    target_count=4,
    record_size=0x110,
    header_layout="per-target",
    rasterizer_binner_control=3,
    requirements=_requirements(late=True, typed_uav=True, stencil=True),
    constant_variable_size_64k=True,
    conservative_rasterization_supported=True,
    resource_types=_RT_TEXTURE1D_TYPES,
    meshlet_stages="mesh-amplification",
    # Strands CS uses LinearCompare without an SDF row.
    root_slots=(
        "BilinearBorder BilinearClamp BilinearMirror BilinearWrap LinearCompare "
        "PointBorder PointClamp PointWrap RootConstant TrilinearClamp TrilinearMirror "
        "TrilinearWrap bindlessTexture1D bindlessTexture2D bindlessTexture2DArray "
        "bindlessTexture3D bindlessTextureCube bindlessBuffer"
    ),
)
PRAGMATA_SDF_PROFILE = _profile(
    runtime_cache_keys=True,
    game="PRAGMATA",
    display_name="Pragmata (retail)",
    aliases=(),
    versions=(251112995,),
    target_count=4,
    record_size=0x110,
    header_layout="per-target",
    rasterizer_binner_control=3,
    requirements=_requirements(late=True, typed_uav=True, stencil=True),
    constant_variable_size_64k=True,
    conservative_rasterization_supported=True,
    stage_slots={
        "pixel": "BilinearBorder PointWrap PointBorder TrilinearClamp bindlessTexture2DArray",
        "vertex": "PointWrap",
        "compute": "PointWrap PointBorder TrilinearClamp LinearCompare",
        "domain": "PointWrap",
        "hull": "PointWrap",
    },
    resource_types=_RT_TEXTURE1D_TYPES,
    meshlet_stages="mesh-amplification",
    root_slots=(
        "BilinearClamp BilinearMirror BilinearWrap PointClamp RootConstant TrilinearMirror "
        "TrilinearWrap bindlessBuffer bindlessTexture1D bindlessTexture2D "
        "bindlessTexture3D bindlessTextureCube"
    ),
)
ONIMUSHA_WOTS_DEMO_SDF_PROFILE = _profile(
    runtime_cache_keys=True,
    game="ONIMUSHAWOTSDEMO",
    variant_of="ONIMUSHAWOTS",
    implicit_resource_stages=("pixel",),
    display_name="Onimusha: Way of the Sword (demo)",
    aliases=("WOTSDEMO", "OnimushaWotSDemo"),
    versions=(251112814,),
    target_count=4,
    record_size=0x110,
    header_layout="per-target",
    rasterizer_binner_control=3,
    requirements=_requirements(late=True),
    resource_types={"shader_resource": {0x01: "texture1d-srv"}},
    meshlet_stages="mesh-amplification",
    root_slots=(
        "BilinearClamp BilinearMirror BilinearWrap PointClamp PointWrap RootConstant "
        "TrilinearMirror TrilinearWrap bindlessBuffer bindlessTexture1D bindlessTexture2D "
        "bindlessTexture3D bindlessTextureCube"
    ),
)


DD2_SDF_PROFILE = _profile(
    runtime_cache_keys=True,
    game="DD2",
    display_name="Dragon's Dogma 2",
    aliases=("Dragons Dogma 2",),
    versions=(260511270,),
    target_count=4,
    constant_variable_size_64k=True,
    conservative_rasterization_supported=True,
    raytracing_libraries=True,
    record_size=0x110,
    header_layout="per-target",
    rasterizer_binner_control=3,
    requirements=_requirements(late=True, typed_uav=True, stencil=True),
    resource_types=_RT_TEXTURE1D_TYPES,
    meshlet_stages="mesh-amplification",
    # g_NvidiaExt is the native NVAPI extension UAV, outside the ordinary SDF descriptor table.
    stage_slots={
        "compute": (
            "PointBorder g_NvidiaExt BilinearClamp BilinearWrap PointClamp PointWrap "
            "RootConstant TrilinearClamp TrilinearWrap bindlessBuffer bindlessTexture2D "
            "bindlessTexture3D BilinearBorder BilinearMirror LinearCompare PointMirror "
            "TrilinearMirror bindlessTexture2DArray bindlessTextureCube"
        ),
        "domain": "BilinearWrap RootConstant BilinearClamp",
        "hull": "BilinearWrap RootConstant BilinearClamp PointWrap TrilinearWrap",
        "pixel": (
            "BilinearClamp BilinearMirror BilinearWrap PointClamp PointMirror PointWrap "
            "RootConstant TrilinearClamp TrilinearMirror TrilinearWrap bindlessBuffer "
            "bindlessTexture1D bindlessTexture2D bindlessTexture2DArray bindlessTexture3D "
            "bindlessTextureCube BilinearBorder LinearCompare PointBorder"
        ),
        "vertex": (
            "BilinearWrap RootConstant bindlessBuffer bindlessTexture2D bindlessTexture3D "
            "BilinearClamp PointClamp PointWrap TrilinearWrap bindlessTexture2DArray"
        ),
    },
)
ONIMUSHA_WOTS_SDF_PROFILE = _profile(
    runtime_cache_keys=True,
    game="ONIMUSHAWOTS",
    display_name="Onimusha: Way of the Sword",
    aliases=("WOTS", "OnimushaWotS"),
    versions=(260605155,),
    target_count=4,
    constant_variable_size_64k=True,
    conservative_rasterization_supported=True,
    record_size=0x110,
    header_layout="per-target",
    rasterizer_binner_control=3,
    requirements=_requirements(late=True, typed_uav=True, stencil=True),
    resource_types=_RT_TEXTURE1D_TYPES,
    meshlet_stages="mesh-amplification",
    stage_slots={
        "compute": (
            "BilinearBorder BilinearMirror LinearCompare PointBorder PointMirror "
            "TrilinearClamp TrilinearMirror bindlessTextureCube BilinearClamp BilinearWrap "
            "PointClamp RootConstant TrilinearWrap bindlessBuffer bindlessTexture2D "
            "bindlessTexture3D PointWrap"
        ),
        "domain": "BilinearWrap RootConstant BilinearClamp TrilinearWrap bindlessTexture2D",
        "vertex": (
            "PointClamp PointWrap bindlessTexture2DArray BilinearClamp BilinearWrap "
            "RootConstant TrilinearWrap bindlessBuffer bindlessTexture2D bindlessTexture3D"
        ),
        "hull": "BilinearClamp RootConstant TrilinearWrap BilinearWrap",
        "pixel": (
            "BilinearClamp BilinearMirror BilinearWrap PointClamp PointWrap RootConstant "
            "TrilinearClamp TrilinearMirror TrilinearWrap bindlessBuffer bindlessTexture1D "
            "bindlessTexture2D bindlessTexture2DArray bindlessTexture3D bindlessTextureCube "
            "BilinearBorder LinearCompare PointBorder TrilinearBorder"
        ),
    },
)


def _enum_variant(base, game, display_name, aliases):
    return register_sdf_profile(replace(
        base, game=game, display_name=display_name, aliases=aliases,
        default_for_version=False, **_game_tables(game),
    ))


def _layout_sibling(base, game, display_name, aliases, versions, *, stage_slots=None, **changes):
    return register_sdf_profile(replace(
        base, game=game, display_name=display_name, aliases=aliases, variant_of="",
        resource_versions=frozenset(versions), default_for_version=True,
        implicit_resource_bindings=base.implicit_resource_bindings | _slot_bindings((stage_slots or {}).items()),
        **_game_tables(game), **changes,
    ))


RERESISTANCE_SDF_PROFILE = _enum_variant(
    RE3_SDF_PROFILE, "RERESISTANCE", "Resident Evil Resistance",
    ("RE3_RESISTANCE", "Resident Evil Resistance"),
)
RE3RT_SDF_PROFILE = _enum_variant(
    RE_RT_SDF_PROFILE, "RE3RT", "Resident Evil 3 Ray Tracing", ("RE3 RT",),
)

KUNITSUGAMI_SDF_PROFILE = _layout_sibling(
    MHWS_SDF_PROFILE, "KUNITSUGAMI", "Kunitsu-Gami: Path of the Goddess", ("KunitsuGami",), (240312225,),
    stage_slots={"pixel": "LinearCompare", "vertex": "TrilinearClamp"},
)
DRDR_SDF_PROFILE = _layout_sibling(
    MHWS_SDF_PROFILE, "DRDR", "Dead Rising Deluxe Remaster", ("Dead Rising Deluxe Remaster",), (240405143,),
)
RE7RT_SDF_PROFILE = _layout_sibling(
    RE_RT_SDF_PROFILE, "RE7RT", "Resident Evil 7 Ray Tracing", ("RE7 RT",), (220128762,),
    stage_slots={"pixel": "TrilinearWrap"},
)
MHST3_SDF_PROFILE = _layout_sibling(
    DD2_SDF_PROFILE, "MHST3", "Monster Hunter Stories 3: Twisted Reflection", ("MHSTORIES3",), (250905804,),
    stage_slots={"pixel": "TrilinearBorder", "vertex": "TrilinearClamp bindlessTexture1D", "hull": "TrilinearClamp",
                 "domain": "bindlessBuffer bindlessTexture2D TrilinearWrap"},
)
O2_SDF_PROFILE = _layout_sibling(
    MHWS_SDF_PROFILE, "O2", "Onimusha 2: Samurai's Destiny", ("Onimusha2",), (240909130,),
    # Forward pre-transform, vertex and compute programs reflect these renderer samplers without SDF rows.
    stage_slots={"pixel": "LinearCompare", "vertex": "PointClamp", "compute": "PointBorder"},
)


__all__ = [
    "DMC5_SDF_PROFILE",
    "DMC5_SDF_RESOURCE_VERSION",
    "DD2_SDF_PROFILE",
    "DRDR_SDF_PROFILE",
    "KUNITSUGAMI_SDF_PROFILE",
    "MHST3_SDF_PROFILE",
    "O2_SDF_PROFILE",
    "DD2_OLD_SDF_PROFILE",
    "MHRISE_SDF_PROFILE",
    "MHWS_SDF_PROFILE",
    "ONIMUSHA_WOTS_SDF_PROFILE",
    "ONIMUSHA_WOTS_DEMO_SDF_PROFILE",
    "PRAGMATA_SDF_PROFILE",
    "RE2_SDF_PROFILE",
    "RE3_SDF_PROFILE",
    "RE4_SDF_PROFILE",
    "RE8_SDF_PROFILE",
    "RE9_SDF_PROFILE",
    "RE_RT_SDF_PROFILE",
    "RERESISTANCE_SDF_PROFILE",
    "RE3RT_SDF_PROFILE",
    "RE7RT_SDF_PROFILE",
    "SF6_SDF_PROFILE",
]
