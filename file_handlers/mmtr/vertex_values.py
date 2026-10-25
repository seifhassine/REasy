"""See docs/internals/surface-data.md, Values added per vertex."""
from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.sdf.shader_graph import (
    ShaderGraphError, ShaderGraphNodeExtension, ShaderPin, ShaderSignatureField, register_shader_graph_node_extension,
)

lazy from .profiles.renderer_inputs import (
    WHERE_AVAILABLE_KIND, add_engine_buffers, add_engine_resources, engine_input_nodes, engine_inputs_available,
    graph_renderer_buffers, graph_renderer_resources,
)
lazy from .material_region_read import per_build_of
lazy from .surface_data import program_surface_data, program_vertex_inputs, surface_data_keys

PER_VERTEX_KIND = "re_engine_per_vertex"
VALUE_SEMANTIC = "REASYVALUE"
# Direct3D 11 and 12 link at most 32 vertex output registers.
MAX_REGISTERS = 32
_COMPONENT_TYPES = {"float": "float32", "uint": "uint32", "int": "sint32"}


def _per_vertex_pins(node, _contract):
    value_type = str(node.properties.get("value_type", "float"))
    return (ShaderPin("value", "Value", value_type, "input", True),
            ShaderPin("result", "Result", value_type, "output"))


def _per_vertex_expression(_node, _pin, _incoming):
    raise ShaderGraphError("Computed per Vertex works in the material graph itself, not inside functions or regions.")


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind=PER_VERTEX_KIND, title="Computed per Vertex", pins_provider=_per_vertex_pins,
    expression=_per_vertex_expression,
    description=QT_TRANSLATE_NOOP(
        "ShaderGraphNodes",
        "Computes Value in the vertex shader, once per vertex, and hands it to the pixel shader interpolated across "
        "each triangle. Value can read the vertex's own attributes, such as its position as the mesh stores it."),
))


@dataclass(frozen=True, slots=True)
class VertexValue:
    # "node:<id>" of a Computed per Vertex node, or "datum:<key>" of Surface Data carried from the vertex shader.
    source: str
    value_type: str
    register: int
    index: int

    @property
    def scalar(self) -> str:
        return self.value_type.rstrip("234")

    @property
    def width(self) -> int:
        return int(self.value_type[-1]) if self.value_type[-1].isdigit() else 1

    def field(self, stage: str) -> ShaderSignatureField:
        return ShaderSignatureField(
            VALUE_SEMANTIC, self.index, self.register, _COMPONENT_TYPES[self.scalar], 0xF,
            (1 << self.width) - 1 if stage == "pixel" else 0, "undefined",
            interpolation="linear" if self.scalar == "float" else "constant")

    def pixel_expression(self) -> str:
        """Recovered masters name pixel input registers ``v<register>``."""
        value = f"v{self.register}.{'xyzw'[:self.width]}"
        return value if self.scalar == "float" else f"as{self.scalar}({value})"

    def stored_expression(self, expression: str) -> str:
        zero = {"float": "0.0", "uint": "0u", "int": "0"}[self.scalar]
        return f"{self.scalar}4({', '.join((expression, *(zero,) * (4 - self.width)))})"


@dataclass(frozen=True, slots=True)
class _VertexStage:
    target_index: int
    program_index: int
    shader_role: str = "vertex"


def _system(field) -> bool:
    return field.system_value not in {"", "undefined"}


def linkage_problem(document, profile, program) -> str:
    roles = {shader.role for shader in program.shaders if shader.present}
    if not {"vertex", "pixel"} <= roles:
        return "It has no vertex shader."
    if roles & {"geometry", "hull", "domain"}:
        return "It draws through a geometry or tessellation stage."
    if "vertex" not in profile.authoring_roles:
        return "REasy rebuilds only pixel shaders for this game."
    vertex = profile.shader_contract(document, program, "vertex")
    pixel = profile.shader_contract(document, program, "pixel")
    outputs = {item.key: item for item in vertex.outputs if not _system(item)}
    for item in pixel.inputs:
        if _system(item):
            continue
        source = outputs.get(item.key)
        if source is None or source.register != item.register or item.mask & ~source.mask:
            return "Its pixel shader reads a value its vertex shader does not hand it at the same place."
    if any(item.key[0] == VALUE_SEMANTIC.casefold() for item in (*vertex.outputs, *pixel.inputs)):
        return "It already carries values REasy added."
    return ""


def _dxbc(program) -> bool:
    shader = program.shader("pixel")
    return shader.spirv is None and int(shader.reflection.model_major) < 6


def _generated(vertex, pixel) -> list:
    handed = {item.key for item in vertex.outputs}
    return [item for item in pixel.inputs if item.key not in handed and item.register != 0xFFFFFFFF]


def first_register(document, profile, program) -> int:
    vertex = profile.shader_contract(document, program, "vertex")
    pixel = profile.shader_contract(document, program, "pixel")
    generated = {item.key for item in _generated(vertex, pixel)} if _dxbc(program) else set()
    fields = (*vertex.outputs, *(item for item in pixel.inputs if item.key not in generated))
    return 1 + max((int(item.register) for item in fields if item.register != 0xFFFFFFFF), default=-1)


def upstream(graph, links, roots, *, through_per_vertex: bool, provides=None):
    """With ``provides`` (producers, per-vertex nodes -> bool), a Where Available counts by the input it takes."""
    sources: dict[str, list[str]] = {}
    for (target, _pin), link in links.items():
        sources.setdefault(target, []).append(link.source_node)
    nodes = {}
    for item in graph.nodes:
        nodes.setdefault(item.node_id, item)
    pending, seen, found, per_vertex = list(roots), set(), [], []
    while pending:
        node_id = pending.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        node = nodes.get(node_id)
        if node is None:
            raise ShaderGraphError("Recovered material connection is dangling.")
        found.append(node)
        if node.kind == PER_VERTEX_KIND:
            per_vertex.append(node)
            if not through_per_vertex:
                continue
        if provides is not None and node.kind == WHERE_AVAILABLE_KIND:
            taken = links.get((node_id, where_available_input(graph, links, node, through_per_vertex, provides)))
            pending.extend((taken.source_node,) if taken else ())
            continue
        pending.extend(sources.get(node_id, ()))
    return found, per_vertex


def where_available_input(graph, links, node, through_per_vertex: bool, provides) -> str:
    value = links.get((node.node_id, "value"))
    return "value" if provides(*upstream(graph, links, [value.source_node] if value else [],
                                         through_per_vertex=through_per_vertex, provides=provides)) else "otherwise"


def _pixel_reads(graph, roots) -> tuple[dict, list, set]:
    """The graph's links by target, and what the pixel stage behind ``roots`` reads: its Computed per Vertex nodes
    and the Surface Data its nodes want."""
    links = {(link.target_node, link.target_pin): link for link in graph.links}
    pixel_nodes, per_vertex = upstream(graph, links, roots, through_per_vertex=False)
    return links, per_vertex, {*surface_data_keys(node) for node in pixel_nodes}


def vertex_value_plan(document, profile, graph, target_index: int, program_index: int, roots):
    links, per_vertex, wanted = per_build_of("pixel_reads", (graph,), tuple(roots), lambda: _pixel_reads(graph, roots))
    if not (per_vertex or wanted):
        return (), ()
    program = document.sdf.targets[int(target_index)].programs[int(program_index)]
    problem = linkage_problem(document, profile, program)
    if problem:
        return (), tuple((f"node:{node.node_id}", problem) for node in per_vertex)
    handed = {(datum.key, datum.value_type) for datum in program_surface_data(document, profile, target_index,
                                                                             program_index)}
    inputs = {(datum.key, datum.value_type) for datum in program_vertex_inputs(document, profile, target_index,
                                                                              program_index)}
    sources = {f"datum:{key}": value_type for key, value_type in wanted - handed if (key, value_type) in inputs}
    refusals = []
    stage = _VertexStage(int(target_index), int(program_index))
    contract = profile.shader_contract(document, program, "vertex")
    contract = add_engine_buffers(document, contract, stage, graph_renderer_buffers(graph))
    contract = add_engine_resources(document, contract, stage, graph_renderer_resources(graph))

    def provides(producers, _per_vertex):
        return (engine_inputs_available(contract, [*engine_input_nodes(producer) for producer in producers])
                and {*surface_data_keys(producer) for producer in producers} <= inputs)

    for node in per_vertex:
        behind, _nested = upstream(graph, links, [link.source_node for (target, _pin), link in links.items()
                                                  if target == node.node_id], through_per_vertex=True,
                                   provides=provides)
        if not engine_inputs_available(contract, [*engine_input_nodes(producer) for producer in behind]):
            refusals.append((f"node:{node.node_id}", "Its vertex shader cannot read an engine input Value reads."))
        elif not {*surface_data_keys(producer) for producer in behind} <= inputs:
            refusals.append((f"node:{node.node_id}", "Its vertex shader does not read Surface Data that Value reads."))
        else:
            sources[f"node:{node.node_id}"] = str(node.properties.get("value_type", "float"))
    start = first_register(document, profile, program)
    generated = len(_generated(profile.shader_contract(document, program, "vertex"),
                               profile.shader_contract(document, program, "pixel"))) if _dxbc(program) else 0
    if start + len(sources) + generated > MAX_REGISTERS:
        return (), tuple((source, f"Its vertex shader has no free output register for {len(sources)} more values.")
                         for source in sorted(sources) if source.startswith("node:")) + tuple(refusals)
    return (tuple(VertexValue(source, sources[source], start + index, index)
                  for index, source in enumerate(sorted(sources))), tuple(refusals))


def add_vertex_values(document, profile, contract, semantics):
    values = semantics.vertex_values
    if not values:
        return contract
    if contract.stage == "vertex":
        return replace(contract, outputs=(*contract.outputs, *(value.field("vertex") for value in values)))
    program = document.sdf.targets[semantics.target_index].programs[semantics.program_index]
    vertex = profile.shader_contract(document, program, "vertex")
    present = {item.key for item in contract.inputs}
    dxbc = _dxbc(program)
    if program.shader("pixel").spirv is not None:
        lacking = ()
    else:
        lacking = tuple(replace(item, read_write_mask=0) for item in vertex.outputs
                        if item.key not in present and (dxbc or not _system(item)))
    inputs = contract.inputs
    if dxbc:
        # DXBC places rasterizer-generated values after every handed-on value, each in its own register.
        moved = {item.key: replace(item, register=values[-1].register + 1 + index)
                 for index, item in enumerate(sorted(_generated(vertex, contract), key=lambda item: int(item.register)))}
        inputs = tuple(moved.get(item.key, item) for item in inputs)
    # In register order, the order the DXBC compiler places inputs in; system values without a register go last.
    inputs = sorted((*inputs, *lacking, *(value.field("pixel") for value in values)),
                    key=lambda item: (int(item.register), (item.mask & -item.mask).bit_length()))
    return replace(contract, inputs=tuple(inputs))


def carried_value(semantics, key: str, value_type: str) -> VertexValue | None:
    return next((value for value in semantics.vertex_values
                 if value.source == f"datum:{key}" and value.value_type == value_type), None)


def per_vertex_value(semantics, node) -> VertexValue:
    source = f"node:{node.node_id}"
    value = next((item for item in semantics.vertex_values if item.source == source), None)
    if value is not None:
        return value
    reason = dict(semantics.vertex_value_refusals).get(source, "")
    raise ShaderGraphError(f"{semantics.name} cannot compute this value per vertex. {reason} Use Where Available or "
                           "Drawing For for the passes that cannot.".replace("  ", " "))


__all__ = [
    "MAX_REGISTERS", "PER_VERTEX_KIND", "VALUE_SEMANTIC", "VertexValue", "add_vertex_values", "carried_value",
    "first_register", "linkage_problem", "per_vertex_value", "upstream", "vertex_value_plan",
]
