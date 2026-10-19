from __future__ import annotations

from dataclasses import replace

from file_handlers.sdf.profiles import DMC5_SDF_PROFILE, DMC5_SDF_RESOURCE_VERSION

from ..models import MmtrNameSemantics, MmtrProgramContext, MmtrSemanticNote
from .base import MmtrGameProfile, register_mmtr_profile, surface_prefix
from .re_engine_shader_graph import re_engine_shader_graph_profile


DMC5_SHADER_TYPES = {
    0: "GBuffer",
    1: "GBufferInstancing",
    2: "ZPrePassedGBuffer",
    3: "ZPrePassedGBufferInstancing",
    4: "Shadow",
    5: "ShadowInstancing",
    6: "Pick",
    7: "PickInstancing",
    8: "Forward",
    9: "ForwardInstancing",
    10: "ForwardDebug",
    11: "DepthWrite",
    12: "DepthWriteInstancing",
    13: "ZPrePass",
    14: "ZPrePassInstancing",
    15: "Water",
    16: "WaterZ",
    17: "WaterWzOcc",
    18: "WaterPartial",
    19: "WaterZPartial",
    20: "WaterPartialWzOcc",
    21: "WaterPartialCheap",
    22: "WaterZPartialCheap",
    23: "WaterPartialCheapWzOcc",
    24: "WaterSurfaceDeferred",
    25: "WaterSurfaceForward",
    26: "WaterSurfaceForwardPreZ",
    27: "WaterSurfacePreZ",
    28: "PreTransform",
    29: "PreTransformWithNorm",
}

DMC5_INPUT_TYPES = {
    0: "Static",
    1: "Dynamic",
    2: "PreTransformStatic",
    3: "PreTransformDynamic",
}

_PREFIX_ROOTS = (
    "Deferred",
    "DirectDeferred",
    "DirectForward",
    "Forward",
    "Shadow",
    "Pick",
    "DepthWrite",
    "ZPrePass",
    "HeightField",
    "EmissiveOnly",
)


def _water_shader_type(core: str) -> int | None:
    exact = {
        "WaterSurfaceDeferred": 24,
        "WaterSurfaceForward": 25,
        "WaterSurfaceForwardPreZ": 26,
        "WaterSurfacePreZ": 27,
    }
    if core in exact:
        return exact[core]
    if not core.startswith("HeightFieldGBufferBlendDepth"):
        return None
    if "OnePassCheapTile" in core:
        return 22
    if "CheapTileWzOcc" in core:
        return 23
    if "CheapTile" in core:
        return 21
    if "OnePassPartial" in core:
        return 19
    if "PartialWzOcc" in core:
        return 20
    if "Partial" in core:
        return 18
    if "OnePassWzOcc" in core:
        return 17
    if "OnePass" in core:
        return 16
    if "WzOcc" in core:
        return 17
    return 15


def _gbuffer_shader_type(instanced: bool, z_prepassed: bool) -> int:
    return (3 if instanced else 2) if z_prepassed else (1 if instanced else 0)


def _gui_features(name: str) -> tuple[str, ...]:
    features = []
    for token, label in (
        ("Glyph", "glyph"),
        ("Poly", "polygon"),
        ("Glow", "glow"),
        ("Pers", "perspective"),
        ("2D", "2d"),
        ("3D", "3d"),
        ("Add", "additive"),
        ("Mask", "mask"),
        ("Reverse", "mask-reverse"),
        ("Write", "mask-write"),
        ("Test", "mask-test"),
        ("Blend2", "blend-variant-2"),
        ("Blend3", "blend-variant-3"),
        ("Detone", "detone"),
    ):
        if token in name:
            features.append(label)
    return tuple(features)


def classify_dmc5_mmtr_program(name: str) -> MmtrNameSemantics:
    core, alpha_test, two_sided = surface_prefix(str(name), _PREFIX_ROOTS)
    instanced = "Instancing" in core
    clip_distance = core.endswith("Clip")
    direct = "DirectDeferred" in core
    secondary = "Static2" in core or "Instancing2" in core
    with_normals = "WithNorm" in core
    notes: list[MmtrSemanticNote] = []
    features: list[str] = []

    if alpha_test:
        features.append("alpha-test")
    if two_sided:
        features.append("two-sided")
    if instanced:
        features.append("instanced")
    if clip_distance:
        features.append("user-clip-plane")
    if direct:
        features.append("direct-gbuffer")
    if secondary:
        features.append("alternate-world-source")
    if with_normals:
        features.append("pretransform-with-normals")

    render_output = "default"
    if "TLW" in core:
        render_output = "transparent-lightweight"
        features.append(render_output)
        notes.append(
            MmtrSemanticNote(
                "program.render_output",
                (
                    "TLW is the transparent LightWeight routing alias; all 40 "
                    "corpus families retain it even where its pipeline record is "
                    "byte-equivalent to the normal transparent route"
                ),
                "inferred",
            )
        )
    elif core.endswith(("LW", "LWClip")):
        render_output = "lightweight"
        features.append(render_output)

    geometry = "specialized"
    input_type_id = None
    input_type = "not-applicable"
    if "Skinning" in core:
        geometry = "skinned"
        input_type_id = 1
        input_type = DMC5_INPUT_TYPES[input_type_id]
    elif "Static" in core:
        geometry = "static"
        input_type_id = 0
        input_type = DMC5_INPUT_TYPES[input_type_id]
    elif "PreTransform" in core:
        geometry = "pre-transformed"
        input_type = "PreTransformStatic/PreTransformDynamic shared draw input"
    elif core.startswith("GUI"):
        geometry = "gui"
    elif core.startswith("HeightField"):
        geometry = "height-field"
    elif core.startswith("WaterSurface"):
        geometry = "water-surface"
    if core.endswith("_GUIMesh"):
        geometry = "gui-mesh"
        features.append("gui-mesh")

    shader_type_id = None
    family = "unclassified"
    if core.startswith("GUI"):
        family = "gui"
        features.extend(_gui_features(core))
    elif core.startswith("HeightField"):
        family = "height-field"
        shader_type_id = _water_shader_type(core)
        for token, label in (
            ("DepthOnly", "depth-only"),
            ("GBufferBlendDepth", "gbuffer-blend-depth"),
            ("OnePass", "one-pass"),
            ("Partial", "partial"),
            ("CheapTile", "cheap-tile"),
            ("WzOcc", "with-z-occlusion"),
        ):
            if token in core:
                features.append(label)
    elif core.startswith("WaterSurface"):
        family = "water-surface"
        shader_type_id = _water_shader_type(core)
    elif core.startswith("PreTransform") and core.endswith("CS"):
        family = "pre-transform-compute"
        shader_type_id = 29 if with_normals else 28
    elif "Deferred" in core:
        family = "deferred-gbuffer"
        z_prepassed = alpha_test and not direct
        shader_type_id = _gbuffer_shader_type(instanced, z_prepassed)
        if z_prepassed:
            features.append("z-prepassed")
    elif "Shadow" in core:
        family = "shadow"
        shader_type_id = 5 if instanced else 4
    elif "Pick" in core:
        family = "pick"
        shader_type_id = 7 if instanced else 6
    elif "Forward" in core:
        family = "forward"
        shader_type_id = 9 if instanced else 8
    elif "DepthWrite" in core:
        family = "depth-write"
        shader_type_id = 12 if instanced else 11
    elif "ZPrePass" in core:
        family = "z-prepass"
        shader_type_id = 14 if instanced else 13
    elif "EmissiveOnly" in core:
        family = "emissive-only"
        for token, label in (
            ("Overdraw", "overdraw"),
            ("DistortionDepth", "distortion-depth"),
        ):
            if token in core:
                features.append(label)
    else:
        notes.append(
            MmtrSemanticNote(
                "program.name",
                f"unclassified DMC5 MMTR program name {name!r}",
            )
        )

    shader_type = (
        DMC5_SHADER_TYPES[shader_type_id]
        if shader_type_id is not None
        else "not-applicable"
    )
    return MmtrNameSemantics(
        family=family,
        shader_type_id=shader_type_id,
        shader_type=shader_type,
        input_type_id=input_type_id,
        input_type=input_type,
        geometry=geometry,
        alpha_test=alpha_test,
        two_sided=two_sided,
        instanced=instanced,
        clip_distance=clip_distance,
        direct=direct,
        secondary_world_source=secondary,
        with_normals=with_normals,
        render_output=render_output,
        features=tuple(dict.fromkeys(features)),
        notes=tuple(notes),
    )


def refine_dmc5_mmtr_program(
    descriptor: MmtrNameSemantics,
    context: MmtrProgramContext,
) -> MmtrNameSemantics:
    if descriptor.family != "deferred-gbuffer":
        return descriptor
    if descriptor.direct:
        z_prepassed = False
    elif context.depth_writes is not None:
        z_prepassed = not context.depth_writes
    elif context.program_count == 36:
        # Compact VFX masters have no Z-prepassed route; their ADeferred aliases write depth and use GBuffer IDs 0/1.
        z_prepassed = False
    else:
        z_prepassed = "z-prepassed" in descriptor.features
    shader_type_id = _gbuffer_shader_type(descriptor.instanced, z_prepassed)
    features = tuple(
        feature for feature in descriptor.features if feature != "z-prepassed"
    )
    if z_prepassed:
        features += ("z-prepassed",)
    return replace(
        descriptor,
        shader_type_id=shader_type_id,
        shader_type=DMC5_SHADER_TYPES[shader_type_id],
        features=features,
    )


def classify_dmc5_mmtr_document(
    programs: tuple[MmtrNameSemantics, ...],
) -> str:
    families = {program.family for program in programs}
    if "gui" in families or any("gui-mesh" in program.features for program in programs):
        return "GUI master material"
    if families & {"height-field", "water-surface"}:
        return "height-field / water master material"
    if "deferred-gbuffer" in families:
        return "standard G-buffer master material"
    if "emissive-only" in families:
        return "emissive-only master material"
    if "forward" in families:
        return "forward / transparent master material"
    if families == {"unclassified"}:
        return "unclassified master material"
    return "specialized master material"


DMC5_MMTR_PROFILE = register_mmtr_profile(
    MmtrGameProfile(
        game="DMC5",
        name="DMC5 MMTR 1808168797",
        display_name="Devil May Cry 5 Master Material",
        aliases=("Devil May Cry V",),
        resource_versions=frozenset({DMC5_SDF_RESOURCE_VERSION}),
        sdf_profile=DMC5_SDF_PROFILE,
        program_classifier=classify_dmc5_mmtr_program,
        document_classifier=classify_dmc5_mmtr_document,
        program_refiner=refine_dmc5_mmtr_program,
        reflected_material_variable_prefixes=("VAR_",),
        shader_graph_profile=re_engine_shader_graph_profile(
            "DMC5", instanced_material_buffer="UserMaterialInstances",
        ),
        default_for_version=True,
    )
)


__all__ = [
    "DMC5_INPUT_TYPES",
    "DMC5_MMTR_PROFILE",
    "DMC5_SHADER_TYPES",
    "classify_dmc5_mmtr_document",
    "classify_dmc5_mmtr_program",
    "refine_dmc5_mmtr_program",
]
