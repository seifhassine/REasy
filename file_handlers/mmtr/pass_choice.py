"""See docs/internals/mesh-preview.md, Program choice."""
from __future__ import annotations

lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.mdf.mdf_file import BASE_ALPHA_TEST_ENABLE, BASE_TWO_SIDE_ENABLE
lazy from file_handlers.sdf.sdf_file import COMPARE_EQUAL

from .material_preview import TranslatedError
lazy from .pass_purpose import pass_purpose

# D3D12_DEPTH_WRITE_MASK_ZERO.
_DEPTH_WRITE_ZERO = 0
# Eye passes masters draw over the lit image.
MESH_PROGRAMS = ("EyeballCopyGBuffer", "EyeballCopyHDR", "EyeballCaustic", "EyeballPostProcess")
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


def own_program_name(material_flags: int, name: str, route: str) -> str:
    """``name``: one of ``MESH_PROGRAMS``, ``Forward`` or ``ZPrePass``."""
    stem = _speedtree_stem(route)
    if stem is not None:
        return stem + _SPEEDTREE_PASSES.get(name, name)
    return _marker(material_flags) + name + route.removeprefix("Deferred")


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


def depth_prepass_program(document, target: int, material_flags: int, route: str) -> int | None:
    depth = document.sdf.targets[target].programs[scene_program(
        document, target, material_flags, route)[1]].depth_stencil_state
    if depth is None or not (depth.depth_enable and depth.depth_write_mask_id == _DEPTH_WRITE_ZERO
                             and depth.depth_function_id == COMPARE_EQUAL):
        return None
    name = own_program_name(material_flags, "ZPrePass", route.removesuffix("BasePose"))
    return _named_program(document, target, name, "depth-prepass",
                          QT_TRANSLATE_NOOP("PassChoice", "Program {name} is not a depth pre-pass."))


__all__ = ["MESH_PROGRAMS", "PassChoiceError",
           "depth_prepass_program", "forward_program_name", "gbuffer_program", "gbuffer_program_name",
           "has_scene_program", "own_passes", "own_program", "own_program_name", "scene_program", "SPEEDTREE_ROUTE",
           "speedtree_route"]
