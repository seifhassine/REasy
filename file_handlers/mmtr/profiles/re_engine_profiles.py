from __future__ import annotations

from dataclasses import replace

from file_handlers.sdf.profiles import (
    DD2_SDF_PROFILE,
    DD2_OLD_SDF_PROFILE,
    DRDR_SDF_PROFILE,
    KUNITSUGAMI_SDF_PROFILE,
    MHRISE_SDF_PROFILE,
    MHST3_SDF_PROFILE,
    MHWS_SDF_PROFILE,
    O2_SDF_PROFILE,
    ONIMUSHA_WOTS_SDF_PROFILE,
    ONIMUSHA_WOTS_DEMO_SDF_PROFILE,
    PRAGMATA_SDF_PROFILE,
    RE2_SDF_PROFILE,
    RE3_SDF_PROFILE,
    RE3RT_SDF_PROFILE,
    RE7RT_SDF_PROFILE,
    RERESISTANCE_SDF_PROFILE,
    RE4_SDF_PROFILE,
    RE8_SDF_PROFILE,
    RE9_SDF_PROFILE,
    RE_RT_SDF_PROFILE,
    SF6_SDF_PROFILE,
)

from ..models import MmtrNameSemantics, MmtrProgramContext
from .base import MmtrGameProfile, register_mmtr_profile, surface_prefix
from .re_engine_shader_graph import re_engine_shader_graph_profile
from .speedtree_names import speedtree_name


_SURFACE_ROOTS = (
    "Deferred",
    "DirectDeferred",
    "DirectForward",
    "Forward",
    "Shadow",
    "Pick",
    "DepthWrite",
    "ZPrePass",
    "StencilWrite",
    "Velocity",
    "GBuffer",
    "HeightField",
    "Water",
)


def classify_re_engine_mmtr_program(name: str) -> MmtrNameSemantics:
    source = str(name)
    speedtree = speedtree_name(source)
    if speedtree is not None:
        features = tuple(label for enabled, label in ((speedtree.alpha_test, "alpha-test"),
                                                      (speedtree.two_sided, "two-sided"),
                                                      (speedtree.foliage, "instanced")) if enabled)
        return MmtrNameSemantics(family=speedtree.family, shader_type_id=None,
                                 shader_type=speedtree.family + " speedtree", input_type_id=None,
                                 input_type="renderer-owned", geometry="speedtree", alpha_test=speedtree.alpha_test,
                                 two_sided=speedtree.two_sided, instanced=speedtree.foliage, features=features)
    core, alpha_test, two_sided = surface_prefix(source, _SURFACE_ROOTS)
    lowered = core.casefold()
    instanced = "instancing" in lowered
    clip_distance = lowered.endswith("clip") or "clipdistance" in lowered
    direct = "directdeferred" in lowered or lowered.startswith("directforward")
    secondary = "static2" in lowered or "instancing2" in lowered
    with_normals = "withnorm" in lowered

    families = (
        ("pre-transform-compute", "pretransform", "cs"),
        ("deferred-gbuffer", "deferred", ""),
        ("shadow", "shadow", ""),
        ("forward", "forward", ""),
        ("depth-write", "depthwrite", ""),
        ("z-prepass", "zprepass", ""),
        ("stencil-write", "stencilwrite", ""),
        ("velocity", "velocity", ""),
        ("pick", "pick", ""),
        ("height-field", "heightfield", ""),
        ("water", "water", ""),
        ("gui", "gui", ""),
        ("emissive-only", "emissiveonly", ""),
    )
    family = "named-engine-pass"
    for candidate, token, required_suffix in families:
        if token in lowered and (not required_suffix or lowered.endswith(required_suffix)):
            family = candidate
            break

    if "skinning" in lowered:
        geometry = "skinned"
    elif "pretransform" in lowered:
        geometry = "pre-transformed"
    elif "static" in lowered:
        geometry = "static"
    elif "heightfield" in lowered:
        geometry = "height-field"
    elif "water" in lowered:
        geometry = "water-surface"
    elif "gui" in lowered:
        geometry = "gui"
    else:
        geometry = "renderer-generated"

    features = []
    for enabled, label in (
        (alpha_test, "alpha-test"),
        (two_sided, "two-sided"),
        (instanced, "instanced"),
        (clip_distance, "user-clip-plane"),
        (direct, "direct-route"),
        (secondary, "alternate-world-source"),
        (with_normals, "pretransform-with-normals"),
        ("basepose" in lowered, "base-pose"),
        ("vfx" in lowered, "vfx"),
    ):
        if enabled:
            features.append(label)

    render_output = "default"
    if "tlw" in lowered:
        render_output = "transparent-lightweight"
    elif lowered.endswith(("lw", "lwclip")):
        render_output = "lightweight"
    if render_output != "default":
        features.append(render_output)

    input_type = {
        "skinned": "dynamic-skinned",
        "static": "static",
        "pre-transformed": "pre-transformed",
    }.get(geometry, "renderer-owned")
    shader_type = family + (" instanced" if instanced else "")
    return MmtrNameSemantics(
        family=family,
        shader_type_id=None,
        shader_type=shader_type,
        input_type_id=None,
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
        features=tuple(features),
    )


def refine_re_engine_mmtr_program(
    descriptor: MmtrNameSemantics,
    context: MmtrProgramContext,
) -> MmtrNameSemantics:
    program = context.program
    compute = program.shader("compute")
    graphics = any(
        shader.present for shader in program.shaders if shader.role != "compute"
    )
    if compute is not None and compute.present and not graphics:
        if descriptor.family == "named-engine-pass":
            descriptor = replace(
                descriptor,
                family="compute",
                shader_type="compute",
                geometry="renderer-generated",
                input_type="renderer-owned",
            )
    vertex = program.shader("vertex")
    if vertex is not None and vertex.reflection is not None:
        has_clip_distance = any(
            parameter.system_value == "clip-distance"
            or parameter.semantic_name.upper() == "SV_CLIPDISTANCE"
            for parameter in vertex.reflection.output_signature
        )
        if has_clip_distance != descriptor.clip_distance:
            features = tuple(
                feature
                for feature in descriptor.features
                if feature != "user-clip-plane"
            )
            if has_clip_distance:
                features += ("user-clip-plane",)
            descriptor = replace(
                descriptor,
                clip_distance=has_clip_distance,
                features=features,
            )
    return descriptor


def classify_re_engine_mmtr_document(
    programs: tuple[MmtrNameSemantics, ...],
) -> str:
    families = {program.family for program in programs}
    if "deferred-gbuffer" in families:
        return "surface master material"
    if families & {"forward", "water", "height-field"}:
        return "forward / specialized surface master material"
    if "gui" in families:
        return "GUI master material"
    if families <= {"compute", "pre-transform-compute", "named-engine-pass"}:
        return "renderer compute master material"
    return "specialized master material"


def _profile(
    *,
    game: str,
    display_name: str,
    aliases: tuple[str, ...],
    sdf_profile,
    current_resource_version: int | None = None,
    bindless_materials: bool = False,
    material_record_prefix_size: int = 0,
    full_authoring: bool = True,
    shader_graph_id: str = "",
) -> MmtrGameProfile:
    return register_mmtr_profile(
        MmtrGameProfile(
            game=game,
            name=f"{display_name} MMTR",
            display_name=display_name,
            aliases=aliases,
            resource_versions=frozenset(sdf_profile.resource_versions),
            sdf_profile=sdf_profile,
            program_classifier=classify_re_engine_mmtr_program,
            document_classifier=classify_re_engine_mmtr_document,
            program_refiner=refine_re_engine_mmtr_program,
            reflected_material_variable_prefixes=("VAR_",),
            shader_graph_profile=re_engine_shader_graph_profile(
                shader_graph_id or game,
                bindless_materials=bindless_materials,
                material_record_prefix_size=material_record_prefix_size,
                full_authoring=full_authoring,
            ),
            default_for_version=True,
            current_resource_version=current_resource_version,
        )
    )


RE2_MMTR_PROFILE = _profile(
    game="RE2",
    display_name="Resident Evil 2 Master Material",
    aliases=("Resident Evil 2 / Biohazard RE:2",),
    sdf_profile=RE2_SDF_PROFILE,
)
RE3_MMTR_PROFILE = _profile(
    game="RE3",
    display_name="Resident Evil 3 Master Material",
    aliases=(),
    sdf_profile=RE3_SDF_PROFILE,
)
RE_RT_MMTR_PROFILE = _profile(
    game="RE2RT",
    display_name="Resident Evil 2 Ray Tracing Master Material",
    # Earlier names, from when RE3 RT shared this profile.
    aliases=("RE2_RE3_RT", "RE_RT", "RE2 / RE3 RT", "Resident Evil 2 / 3 Ray Tracing Master Material"),
    sdf_profile=RE_RT_SDF_PROFILE,
    # Shared with the RE3 RT variant and recorded in saved material projects.
    shader_graph_id="RE2_RE3_RT",
    bindless_materials=True,
)
RE8_MMTR_PROFILE = _profile(
    game="RE8",
    display_name="Resident Evil Village Master Material",
    aliases=("Resident Evil 8", "Resident Evil Village / Biohazard Village"),
    sdf_profile=RE8_SDF_PROFILE,
    bindless_materials=True,
)
MHRISE_MMTR_PROFILE = _profile(
    game="MHRISE",
    display_name="Monster Hunter Rise Master Material",
    aliases=("MHR", "MonsterHunterRise"),
    sdf_profile=MHRISE_SDF_PROFILE,
)
RE4_MMTR_PROFILE = _profile(
    game="RE4",
    display_name="Resident Evil 4 Master Material",
    aliases=("Resident Evil 4 Remake",),
    sdf_profile=RE4_SDF_PROFILE,
    bindless_materials=True,
)
SF6_MMTR_PROFILE = _profile(
    game="SF6",
    display_name="Street Fighter 6 Master Material",
    aliases=("StreetFighter6",),
    sdf_profile=SF6_SDF_PROFILE,
    current_resource_version=250109895,
    bindless_materials=True,
)
DD2_MMTR_PROFILE = _profile(
    game="DD2",
    display_name="Dragon's Dogma 2 Master Material",
    aliases=("Dragons Dogma 2",),
    sdf_profile=DD2_SDF_PROFILE,
    bindless_materials=True,
)
DD2_OLD_MMTR_PROFILE = _profile(
    game="DD2OLD",
    display_name="Dragon's Dogma 2 Master Material (legacy)",
    aliases=("DD2 Old",),
    sdf_profile=DD2_OLD_SDF_PROFILE,
    full_authoring=False,
)
MHWS_MMTR_PROFILE = _profile(
    game="MHWS",
    display_name="Monster Hunter Wilds Master Material",
    aliases=("MHWILDS", "MonsterHunterWilds"),
    sdf_profile=MHWS_SDF_PROFILE,
    # The version the game's executable registers.
    current_resource_version=240906211,
    bindless_materials=True,
)
RE9_MMTR_PROFILE = _profile(
    game="RE9",
    display_name="Resident Evil Requiem Master Material",
    aliases=("Resident Evil 9",),
    sdf_profile=RE9_SDF_PROFILE,
    bindless_materials=True,
)
PRAGMATA_MMTR_PROFILE = _profile(
    game="PRAGMATA",
    display_name="Pragmata Retail Master Material",
    aliases=(),
    sdf_profile=PRAGMATA_SDF_PROFILE,
    bindless_materials=True,
)
ONIMUSHA_WOTS_MMTR_PROFILE = _profile(
    game="ONIMUSHAWOTS",
    display_name="Onimusha: Way of the Sword Master Material",
    aliases=("WOTS", "OnimushaWotS"),
    sdf_profile=ONIMUSHA_WOTS_SDF_PROFILE,
    bindless_materials=True,
    material_record_prefix_size=16,
)
ONIMUSHA_WOTS_DEMO_MMTR_PROFILE = _profile(
    game="ONIMUSHAWOTSDEMO",
    display_name="Onimusha: Way of the Sword Master Material (demo)",
    aliases=("WOTSDEMO", "OnimushaWotSDemo"),
    sdf_profile=ONIMUSHA_WOTS_DEMO_SDF_PROFILE,
    full_authoring=False,
)

KUNITSUGAMI_MMTR_PROFILE = _profile(
    game="KUNITSUGAMI",
    display_name="Kunitsu-Gami Master Material",
    aliases=KUNITSUGAMI_SDF_PROFILE.aliases,
    sdf_profile=KUNITSUGAMI_SDF_PROFILE,
    bindless_materials=True,
)
DRDR_MMTR_PROFILE = _profile(
    game="DRDR",
    display_name="Dead Rising Deluxe Remaster Master Material",
    aliases=DRDR_SDF_PROFILE.aliases,
    sdf_profile=DRDR_SDF_PROFILE,
    bindless_materials=True,
)
RE7RT_MMTR_PROFILE = _profile(
    game="RE7RT",
    display_name="Resident Evil 7 Ray Tracing Master Material",
    aliases=RE7RT_SDF_PROFILE.aliases,
    sdf_profile=RE7RT_SDF_PROFILE,
    bindless_materials=True,
)
MHST3_MMTR_PROFILE = _profile(
    game="MHST3",
    display_name="Monster Hunter Stories 3 Master Material",
    aliases=MHST3_SDF_PROFILE.aliases,
    sdf_profile=MHST3_SDF_PROFILE,
    bindless_materials=True,
)
O2_MMTR_PROFILE = _profile(
    game="O2",
    display_name="Onimusha 2 Master Material",
    aliases=O2_SDF_PROFILE.aliases,
    sdf_profile=O2_SDF_PROFILE,
    bindless_materials=True,
)


def _enum_variant(base, sdf_profile):
    return register_mmtr_profile(replace(
        base, game=sdf_profile.game, name=f"{sdf_profile.display_name} MMTR",
        display_name=f"{sdf_profile.display_name} Master Material", aliases=sdf_profile.aliases,
        sdf_profile=sdf_profile, default_for_version=False,
    ))


RERESISTANCE_MMTR_PROFILE = _enum_variant(RE3_MMTR_PROFILE, RERESISTANCE_SDF_PROFILE)
RE3RT_MMTR_PROFILE = _enum_variant(RE_RT_MMTR_PROFILE, RE3RT_SDF_PROFILE)


ALL_RE_ENGINE_MMTR_PROFILES = (
    RE2_MMTR_PROFILE,
    RE3_MMTR_PROFILE,
    RE_RT_MMTR_PROFILE,
    RERESISTANCE_MMTR_PROFILE,
    RE3RT_MMTR_PROFILE,
    RE8_MMTR_PROFILE,
    MHRISE_MMTR_PROFILE,
    RE4_MMTR_PROFILE,
    SF6_MMTR_PROFILE,
    DD2_MMTR_PROFILE,
    DD2_OLD_MMTR_PROFILE,
    MHWS_MMTR_PROFILE,
    RE9_MMTR_PROFILE,
    PRAGMATA_MMTR_PROFILE,
    ONIMUSHA_WOTS_MMTR_PROFILE,
    ONIMUSHA_WOTS_DEMO_MMTR_PROFILE,
    KUNITSUGAMI_MMTR_PROFILE,
    DRDR_MMTR_PROFILE,
    O2_MMTR_PROFILE,
    RE7RT_MMTR_PROFILE,
    MHST3_MMTR_PROFILE,
)


__all__ = [
    "ALL_RE_ENGINE_MMTR_PROFILES",
    "DD2_MMTR_PROFILE",
    "DD2_OLD_MMTR_PROFILE",
    "DRDR_MMTR_PROFILE",
    "KUNITSUGAMI_MMTR_PROFILE",
    "MHST3_MMTR_PROFILE",
    "O2_MMTR_PROFILE",
    "RE7RT_MMTR_PROFILE",
    "MHRISE_MMTR_PROFILE",
    "MHWS_MMTR_PROFILE",
    "ONIMUSHA_WOTS_MMTR_PROFILE",
    "ONIMUSHA_WOTS_DEMO_MMTR_PROFILE",
    "PRAGMATA_MMTR_PROFILE",
    "RE2_MMTR_PROFILE",
    "RE3_MMTR_PROFILE",
    "RE4_MMTR_PROFILE",
    "RE8_MMTR_PROFILE",
    "RE9_MMTR_PROFILE",
    "RE_RT_MMTR_PROFILE",
    "RERESISTANCE_MMTR_PROFILE",
    "RE3RT_MMTR_PROFILE",
    "SF6_MMTR_PROFILE",
    "classify_re_engine_mmtr_document",
    "classify_re_engine_mmtr_program",
    "refine_re_engine_mmtr_program",
]
