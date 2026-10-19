"""See docs/internals/textures-view.md, States."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.sdf.sdf_file import CULL_BACK, CULL_NONE

from .material_preview import _TRIANGLE_LIST, MaterialPreviewError, PreviewMaterial
from .mesh_draw import program_states
from .mesh_render import _written, draw_purpose
from .pass_choice import depth_prepass_program, has_scene_program, own_passes
from .surface_meaning import GBUFFER_PURPOSE, surface_output_catalog_for
from .textures_view import BLENDED, DrawStates

# A via.render.layer.GBufferLayout name.
BASE_COLOUR_SLOT = "BaseColorMetallicTranslucency"
# The renderer's draw order; "blending" holds the G-buffer programs that blend.
ORDER = (GBUFFER_PURPOSE, "blending", "forward")
_SOLID = 3
_RED_GREEN_BLUE = 0x7
# D3D12_BLEND's factors of a second colour the pixel shader would give.
_SECOND_SOURCE = frozenset({16, 17, 18, 19})


@dataclass(frozen=True, slots=True)
class ShownDraw:
    """``purpose`` is one of ``ORDER``; ``prepass`` the states of a depth pass run first."""
    purpose: str
    states: DrawStates
    prepass: DrawStates | None = None


def _states(program, target: int | None) -> DrawStates:
    count = 0 if program.blend_state is None else len(program.blend_state.render_targets)
    given = program_states(program, count, 0)
    fill, cull, counter_clockwise, bias, clamp, slope, clip = given["rasterizer"]
    if fill != _SOLID or given["topology"] != _TRIANGLE_LIST:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The program {name} does not draw filled triangles from a list."),
            {"name": program.name})
    tested, written, function = given["depth_stencil"][:3]
    blend, channels = None, 0
    if target is not None:
        _coverage, independent, targets = given["blend"]
        enabled, source, destination, operation, *_alpha, mask = targets[target if independent else 0]
        if enabled and _SECOND_SOURCE & {source, destination}:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The program {name} blends with a second colour of its pixel shader's."),
                {"name": program.name})
        blend, channels = (source, destination, operation) if enabled else None, mask & _RED_GREEN_BLUE
    return DrawStates(cull, bool(counter_clockwise), function if tested else None, bool(tested and written), blend,
                      channels, (bias, clamp, slope), bool(clip))


def program_draw(document, material: PreviewMaterial, shader_target: int, route: str) -> ShownDraw | None:
    flags = material.material_flags
    if own_passes(document, shader_target, flags, route) and not has_scene_program(
            document, shader_target, flags, route):
        return None
    purpose, program = draw_purpose(document, material, shader_target, route)
    catalog = surface_output_catalog_for(document.profile)
    target = 0 if purpose == "forward" else next(
        (item.target for item in catalog.targets
         if item.purpose == GBUFFER_PURPOSE and item.slot == BASE_COLOUR_SLOT), None)
    if target is None or target not in _written(document, program):
        return None
    states = _states(program, target)
    if not states.channels:
        return None
    prepass = depth_prepass_program(document, shader_target, flags, route)
    return ShownDraw(purpose, states, None if prepass is None else _states(
        document.sdf.targets[shader_target].programs[prepass], None))


def plain_draw(two_sided: bool, transparent: bool) -> ShownDraw:
    cull = CULL_NONE if two_sided else CULL_BACK
    if transparent:
        return ShownDraw("forward", DrawStates(cull, depth_write=False, blend=BLENDED))
    return ShownDraw(GBUFFER_PURPOSE, DrawStates(cull))


def ordered(draws: list[tuple[ShownDraw, object]]) -> list[tuple[DrawStates, object]]:
    steps = [(draw.prepass, item) for draw, item in draws if draw.prepass is not None and draw.purpose != "forward"]
    for purpose in ORDER:
        if purpose == "forward":
            steps += [(draw.prepass, item) for draw, item in draws
                      if draw.prepass is not None and draw.purpose == purpose]
        steps += [(draw.states, item) for draw, item in draws if draw.purpose == purpose]
    return steps


__all__ = ["BASE_COLOUR_SLOT", "ORDER", "ShownDraw", "ordered", "plain_draw", "program_draw"]
