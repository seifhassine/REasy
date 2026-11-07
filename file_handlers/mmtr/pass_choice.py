"""See docs/internals/mesh-preview.md, Program choice."""
from __future__ import annotations

lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.mdf.mdf_file import BASE_ALPHA_TEST_ENABLE, BASE_TWO_SIDE_ENABLE
lazy from file_handlers.sdf.sdf_file import COMPARE_EQUAL

from .material_preview import TranslatedError
lazy from .effect_meshes import effect_mesh_catalog_for
lazy from .pass_purpose import pass_purpose

# D3D12_DEPTH_WRITE_MASK_ZERO.
_DEPTH_WRITE_ZERO = 0
GUI_MESH = "GUIMesh"
# Eye passes masters draw over the lit image; the GUI's pass of meshes, onto the tonemapped image.
MESH_PROGRAMS = ("EyeballCopyGBuffer", "EyeballCopyHDR", "EyeballCaustic", "EyeballPostProcess", GUI_MESH)
# SpeedTree clusters' route: this prefix plus the stem their program names share, marker included.
SPEEDTREE_ROUTE = "SpeedTree:"
_SPEEDTREE_PASSES = {"ZPrePass": "DepthWrite"}


class PassChoiceError(TranslatedError):
    context = "PassChoice"


def speedtree_route(stem: str) -> str:
    return SPEEDTREE_ROUTE + stem


def _speedtree_stem(route: str) -> str | None:
    return route[len(SPEEDTREE_ROUTE):] if route.startswith(SPEEDTREE_ROUTE) else None


def _marker(material_flags: int) -> str:
    return (("A" if material_flags & BASE_ALPHA_TEST_ENABLE else "")
            + ("TS" if material_flags & BASE_TWO_SIDE_ENABLE else ""))


def gbuffer_program_name(material_flags: int, route: str) -> str:
    stem = _speedtree_stem(route)
    return stem if stem is not None else _marker(material_flags) + route


def _named_program(document, target: int, name: str, purpose: str, wrong_purpose: str) -> int:
    found = document.sdf.targets[target].program_indices.get(name, ())
    if len(found) != 1:
        raise PassChoiceError(QT_TRANSLATE_NOOP("PassChoice", "The master has {count} programs named {name}."),
                              {"count": len(found), "name": name})
    index = found[0]
    if pass_purpose(document, document.profile.shader_graph_profile, target, index) != purpose:
        raise PassChoiceError(wrong_purpose, {"name": name})
    semantics = document.program(target, index)
    if bool(semantics.skinning_influence_count) != (semantics.descriptor.geometry == "skinned"):
        raise PassChoiceError(QT_TRANSLATE_NOOP(
            "PassChoice", "Program {name} is named for another kind of geometry than its vertex inputs take."),
            {"name": name})
    return index


def route_core(route: str) -> str:
    return route.removeprefix("Deferred")


def own_program_name(material_flags: int, name: str, route: str) -> str:
    """``name``: one of ``MESH_PROGRAMS``, ``Forward``, ``ZPrePass`` or ``DepthWrite``. A GUI mesh is drawn as an
    element without mask, add blend or stencil: the plain ``Forward..._GUIMesh`` program."""
    stem = _speedtree_stem(route)
    if stem is not None:
        return stem + _SPEEDTREE_PASSES.get(name, name)
    core = route_core(route)
    if name == GUI_MESH:
        return f"{_marker(material_flags)}Forward{core}_{GUI_MESH}"
    return _marker(material_flags) + name + core


def forward_program_name(material_flags: int, route: str) -> str:
    return own_program_name(material_flags, "Forward", route)


def gbuffer_program(document, target: int, material_flags: int, route: str) -> int:
    return _named_program(document, target, gbuffer_program_name(material_flags, route), "gbuffer",
                          QT_TRANSLATE_NOOP("PassChoice", "Program {name} does not draw into the G-buffer."))


def scene_program(document, target: int, material_flags: int, route: str) -> tuple[str, int]:
    if gbuffer_program_name(material_flags, route) in document.sdf.targets[target].program_indices:
        return "gbuffer", gbuffer_program(document, target, material_flags, route)
    return "forward", _named_program(document, target, forward_program_name(material_flags, route), "forward",
                                     QT_TRANSLATE_NOOP("PassChoice", "Program {name} does not draw forward."))


def effect_program(document, target: int, material_flags: int, route: str) -> int | None:
    catalog = effect_mesh_catalog_for(document.profile)
    indices = document.sdf.targets[target].program_indices
    if catalog is None or catalog.program not in indices or route not in catalog.routes:
        return None
    index = _named_program(document, target, _marker(material_flags) + catalog.program, "colour",
                           QT_TRANSLATE_NOOP("PassChoice", "Program {name} does not draw colour."))
    program = document.sdf.targets[target].programs[index]
    contract = document.profile.shader_graph_profile.shader_contract
    read = next((resource.name for shader in program.shaders if shader.present
                 for resource in contract(document, program, shader.role).resources
                 if resource.name in catalog.dynamic), None)
    if read is not None:
        raise PassChoiceError(QT_TRANSLATE_NOOP(
            "PassChoice", "The effect system binds {name} as it draws, which REasy does not show."), {"name": read})
    return index


def water_program(document, target: int, catalog, route: str, occlusion: bool) -> int:
    return _named_program(document, target, catalog.program_name(route, occlusion), "gbuffer",
                          QT_TRANSLATE_NOOP("PassChoice", "Program {name} does not draw into the G-buffer."))


def drawn_program(document, target: int, material_flags: int, route: str) -> tuple[str, int]:
    if not has_scene_program(document, target, material_flags, route):
        effect = effect_program(document, target, material_flags, route)
        if effect is not None:
            return "forward", effect
    return scene_program(document, target, material_flags, route)


def has_scene_program(document, target: int, material_flags: int, route: str) -> bool:
    indices = document.sdf.targets[target].program_indices
    return (gbuffer_program_name(material_flags, route) in indices
            or forward_program_name(material_flags, route) in indices)


def own_passes(document, target: int, material_flags: int, route: str) -> tuple[str, ...]:
    indices = document.sdf.targets[target].program_indices
    return tuple(sorted(name for name in MESH_PROGRAMS if own_program_name(material_flags, name, route) in indices))


def own_program(document, target: int, material_flags: int, name: str, route: str) -> int:
    return _named_program(document, target, own_program_name(material_flags, name, route), "colour",
                          QT_TRANSLATE_NOOP("PassChoice", "Program {name} does not draw colour in a pass of its own."))


def _pass_program(document, target: int, material_flags: int, name: str, route: str, purpose: str,
                  wrong_purpose: str) -> int | None:
    name = own_program_name(material_flags, name, route.removesuffix("BasePose"))
    if name not in document.sdf.targets[target].program_indices:
        return None
    return _named_program(document, target, name, purpose, wrong_purpose)


def zprepass_program(document, target: int, material_flags: int, route: str) -> int | None:
    return _pass_program(document, target, material_flags, "ZPrePass", route, "depth-prepass",
                         QT_TRANSLATE_NOOP("PassChoice", "Program {name} is not a depth pre-pass."))


def depth_only_program(document, target: int, material_flags: int, name: str, route: str) -> int | None:
    return _pass_program(document, target, material_flags, name, route, "depth",
                         QT_TRANSLATE_NOOP("PassChoice", "Program {name} draws more than depth."))


def depth_prepass_program(document, target: int, material_flags: int, route: str) -> int | None:
    depth = document.sdf.targets[target].programs[drawn_program(
        document, target, material_flags, route)[1]].depth_stencil_state
    if depth is None or not (depth.depth_enable and depth.depth_write_mask_id == _DEPTH_WRITE_ZERO
                             and depth.depth_function_id == COMPARE_EQUAL):
        return None
    name = own_program_name(material_flags, "ZPrePass", route.removesuffix("BasePose"))
    return _named_program(document, target, name, "depth-prepass",
                          QT_TRANSLATE_NOOP("PassChoice", "Program {name} is not a depth pre-pass."))


__all__ = ["GUI_MESH", "MESH_PROGRAMS", "PassChoiceError", "SPEEDTREE_ROUTE", "depth_only_program",
           "depth_prepass_program", "drawn_program", "effect_program", "forward_program_name", "gbuffer_program",
           "gbuffer_program_name", "has_scene_program", "own_passes", "own_program", "own_program_name", "route_core",
           "scene_program", "speedtree_route", "water_program", "zprepass_program"]
