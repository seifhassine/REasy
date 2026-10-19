from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from file_handlers.sdf.shader_graph import (
    ShaderGraphError, ShaderGraphNodeExtension, ShaderPin, register_shader_graph_node_extension,
)

lazy from .profiles.re_engine_shader_graph import RE_ENGINE_RECOVERED_MASTER_KIND


_NAMED = {
    "deferred-gbuffer": ("gbuffer", {"colour"}),
    "forward": ("forward", {"colour"}),
    "shadow": ("shadow", {"depth"}),
    "z-prepass": ("depth-prepass", {"depth"}),
    "velocity": ("velocity", {"colour"}),
    "pick": ("pick", {"depth", "stencil", "other"}),
}
PURPOSES = ("gbuffer", "forward", "shadow", "depth-prepass", "velocity", "pick",
            "colour", "depth", "stencil", "compute", "other")
VISIBLE = frozenset({"gbuffer", "forward"})


DRAWING_FOR_KIND = "re_engine_drawing_for"


def _drawing_for_pins(_node, _contract):
    return (ShaderPin("active", "Drawing For It", "bool", "output"),)


def _drawing_for_expression(_node, _pin, _incoming):
    raise ShaderGraphError("Drawing For needs the pass being built.")


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind=DRAWING_FOR_KIND, title="Drawing For", pins_provider=_drawing_for_pins,
    expression=_drawing_for_expression,
    description=QT_TRANSLATE_NOOP(
        "ShaderGraphNodes",
        "True in the passes drawing for the chosen purposes, false in the rest; each pass sees a constant."),
))


def drawing_for(node, purpose: str) -> bool:
    purposes = node.properties.get("purposes", ())
    if not isinstance(purposes, (list, tuple)) or any(item not in PURPOSES for item in purposes):
        raise ShaderGraphError("Drawing For names an unknown purpose.")
    return purpose in purposes


def graph_draws_for(graph) -> bool:
    def found(value) -> bool:
        if isinstance(value, dict):
            return value.get("kind") == DRAWING_FOR_KIND or any(found(item) for item in value.values())
        return isinstance(value, (list, tuple)) and any(found(item) for item in value)

    # The recovered master's properties are its programs, which nest no graph.
    return any(node.kind == DRAWING_FOR_KIND or node.kind != RE_ENGINE_RECOVERED_MASTER_KIND and found(node.properties)
               for node in graph.nodes)


translate = QCoreApplication.translate

_TITLES = {
    "gbuffer": QT_TRANSLATE_NOOP("PassPurpose", "Visible colour (G-buffer)"),
    "forward": QT_TRANSLATE_NOOP("PassPurpose", "Visible colour (forward)"),
    "shadow": QT_TRANSLATE_NOOP("PassPurpose", "Shadows"),
    "depth-prepass": QT_TRANSLATE_NOOP("PassPurpose", "Depth pre-pass"),
    "velocity": QT_TRANSLATE_NOOP("PassPurpose", "Motion vectors"),
    "pick": QT_TRANSLATE_NOOP("PassPurpose", "Selection (pick)"),
    "colour": QT_TRANSLATE_NOOP("PassPurpose", "Other colour pass"),
    "depth": QT_TRANSLATE_NOOP("PassPurpose", "Depth only"),
    "stencil": QT_TRANSLATE_NOOP("PassPurpose", "Stencil only"),
    "compute": QT_TRANSLATE_NOOP("PassPurpose", "Compute"),
    "other": QT_TRANSLATE_NOOP("PassPurpose", "Other"),
}


def purpose_title(purpose: str) -> str:
    return translate("PassPurpose", _TITLES[purpose])


def structural_class(document, profile, target_index: int, program_index: int) -> str:
    program = document.sdf.targets[target_index].programs[program_index]
    roles = {shader.role for shader in program.shaders if shader.present}
    if roles == {"compute"}:
        return "compute"
    blend, depth = program.blend_state, program.depth_stencil_state
    colour = False
    if "pixel" in roles:
        try:
            outputs = profile.shader_contract(document, program, "pixel").outputs
        except ValueError:
            outputs = ()
        targets = any(field.semantic_name.casefold() == "sv_target" for field in outputs)
        colour = targets and (blend is None or any(target.write_mask for target in blend.render_targets))
    if colour:
        return "colour"
    if depth is not None and depth.depth_enable and depth.depth_write_mask != "zero":
        return "depth"
    if depth is not None and depth.stencil_enable and depth.stencil_write_mask:
        return "stencil"
    return "other"


def pass_purpose(document, profile, target_index: int, program_index: int) -> str:
    cache = document._pass_purpose_cache
    key = (target_index, program_index)
    if key not in cache:
        try:
            semantics = document.program(*key)
        except KeyError:
            semantics = None
        structure = structural_class(document, profile, target_index, program_index)
        named = _NAMED.get(semantics.descriptor.family) if semantics is not None else None
        cache[key] = named[0] if named is not None and structure in named[1] else structure
    return cache[key]


__all__ = ["DRAWING_FOR_KIND", "PURPOSES", "VISIBLE", "drawing_for", "graph_draws_for", "pass_purpose",
           "purpose_title", "structural_class"]
