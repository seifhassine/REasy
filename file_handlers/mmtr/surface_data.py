"""See docs/internals/surface-data.md."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import re

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from file_handlers.bounded_cache import BoundedCache
from file_handlers.sdf.shader_graph import (
    ShaderGraphError, ShaderGraphNodeExtension, ShaderPin, register_shader_graph_node_extension,
)
lazy from file_handlers.sdf.spirv import reflect_spirv
lazy from file_handlers.sdf.spirv_contract import patch_interface_ids, spirv_interface_rows

lazy from .surface_flow import (
    SCALAR_TYPES, ChannelSplit, FlowStore, NativeFlow, SurfaceChannel, _vector, recovered_surface_flow,
)
lazy from .pass_purpose import pass_purpose
lazy from .profiles.re_engine_bindless_material import bound_resource
lazy from .profiles.re_engine_dxil_signatures import DxilEntryMetadata
lazy from .profiles.re_engine_shader_graph import RE_ENGINE_RECOVERED_MASTER_KIND
lazy from .profiles.re_engine_spirv_authoring import _Module
lazy from .surface_outputs import _recover_ir, _shader_bytes
lazy from .vertex_values import MAX_REGISTERS, first_register, linkage_problem, carried_value

SURFACE_DATA_KIND = "re_engine_surface_data"
MATERIAL_COORDINATES = "coordinates"


def _surface_data_pins(node, _contract):
    return (ShaderPin("value", str(node.properties.get("label", "Value")),
                      str(node.properties.get("value_type", "float")), "output"),)


def _surface_data_expression(_node, _pin, _incoming):
    raise ShaderGraphError("Surface data needs the pass being built.")


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind=SURFACE_DATA_KIND, title="Surface Data", pins_provider=_surface_data_pins,
    expression=_surface_data_expression,
    description=QT_TRANSLATE_NOOP(
        "ShaderGraphNodes",
        "A value the pass hands its pixel shader, such as a UV set, the normal or the position, read in every pass "
        "that has it."),
))
_CACHE_LIMIT = 1024
_CACHE = BoundedCache()


translate = QCoreApplication.translate


@dataclass(frozen=True, slots=True)
class SurfaceDatum:
    key: str
    label: str
    value_type: str
    # The pixel input lanes carrying it: (semantic, index, component).
    lanes: tuple[tuple[str, int, int], ...]
    # Has this key only because the pass transforms nothing.
    vacuous: bool = False


def _attribute(field, component: int) -> tuple[str, int]:
    return f"{field.semantic_name}{field.semantic_index}", (field.mask & ((1 << component) - 1)).bit_count()


def _dxbc_sources(ir, contract, flow: NativeFlow) -> dict[int, tuple]:
    buffers = {item.bind_point: item.name for item in contract.constant_buffers if item.register_space == 0}
    resources = {(item.register_class, item.bind_point): item.name for item in contract.resources
                 if item.register_space == 0}
    result = {}
    for index, value in enumerate(flow.values):
        origin = value.origin
        if value.kind != "source" or not isinstance(origin, tuple):
            continue
        if len(origin) == 2 and isinstance(origin[0], str) and re.fullmatch(r"v\d+", origin[0]):
            register, component = int(origin[0][1:]), int(origin[1])
            field = next((item for item in contract.inputs if item.register == register
                          and item.mask & (1 << component)), None)
            if field is not None:
                result[index] = ("attribute", *_attribute(field, component))
        elif len(origin) == 4 and origin[0] == "cb" and origin[1] in buffers:
            result[index] = ("data", buffers[origin[1]])
        elif len(origin) == 2 and isinstance(origin[0], str) and (match := re.match(r"cb(\d+)\[", origin[0])):
            if int(match[1]) in buffers:
                result[index] = ("data", buffers[int(match[1])])
        elif len(origin) == 3 and isinstance(origin[0], int):
            operands = ir.instructions[origin[0]].operands
            for text in operands:
                match = re.match(r"\s*([tu])(\d+)\b", text)
                if match and (match[1], int(match[2])) in resources:
                    result[index] = ("data", resources[match[1], int(match[2])])
                    break
    return result


_DXIL_LINE = re.compile(r"\s*(%[\w.]+) = (.*)")
_DXIL_INPUT = re.compile(r"@dx\.op\.loadInput\.\w+\(i32 4, i32 (\d+), i32 (\d+), i8 (\d+)")
_DXIL_HEAP = re.compile(r"@dx\.op\.createHandleFromHeap\(i32 218, [^,]+, i1 (false|true)")
_DXIL_ANNOTATE = re.compile(r"@dx\.op\.annotateHandle\(i32 216, %dx\.types\.Handle (%[\w.]+)")
_DXIL_VALUE = re.compile(r"%[\w.]+")


def _dxil_sources(ir, contract, flow: NativeFlow) -> dict[int, tuple]:
    lines = ir.to_assembly().splitlines()
    rows = {row: key for key, row in DxilEntryMetadata.parse(lines).input_rows().items()}
    fields = {(item.semantic_name.casefold(), item.semantic_index): item for item in contract.inputs}
    by_id = {(item.register_class, item.resource_id): item for item in contract.resources}
    by_binding = {(item.register_class, item.register_space, item.bind_point): item for item in contract.resources}
    handles, names = {}, {}
    for line in lines:
        match = _DXIL_LINE.match(line)
        if match is None:
            continue
        result, body = match.groups()
        if (found := _DXIL_INPUT.search(body)) is not None:
            field = fields.get(rows.get((int(found[1]), int(found[2]))))
            if field is not None:
                names[result] = ("attribute", f"{field.semantic_name}{field.semantic_index}", int(found[3]))
        elif (resource := bound_resource(body, by_id, by_binding)) is not None:
            handles[result] = resource.name
        elif (found := _DXIL_HEAP.search(body)) is not None:
            handles[result] = "SamplerDescriptorHeap" if found[1] == "true" else "ResourceDescriptorHeap"
        elif (found := _DXIL_ANNOTATE.search(body)) is not None:
            if found[1] in handles:
                handles[result] = handles[found[1]]
        elif body.startswith("call") and (handle := next((handles[item] for item in _DXIL_VALUE.findall(body)
                                                         if item in handles), None)) is not None:
            names[result] = ("data", handle)
        elif body.startswith("extractvalue") and (source := next(
                (names[item] for item in _DXIL_VALUE.findall(body) if names.get(item, ("",))[0] == "data"),
                None)) is not None:
            names[result] = source
    return {index: names[value.origin] for index, value in enumerate(flow.values)
            if value.kind == "source" and value.origin in names}


def _spirv_sources(ir, contract, flow: NativeFlow) -> dict[int, tuple]:
    native = reflect_spirv(ir.write())
    module = _Module(ir)
    inputs, _outputs, _undeclared = spirv_interface_rows(native, contract,
                                                         patch_interface_ids(native, module.decorations))
    descriptors = {item.variable_id: item.name for item in native.descriptors}
    pointer_names = {"OpAccessChain", "OpInBoundsAccessChain", "OpPtrAccessChain", "OpCopyObject"}
    roots, loads = {}, {}
    for row in ir.instructions:
        references = [operand.value for operand in row.operands if operand.kind == "IdRef"]
        if row.name in pointer_names and references:
            root, path = roots.get(references[0], (references[0], ()))
            roots[row.result] = (root, (*path, *references[1:]))
        elif row.name == "OpLoad" and references:
            loads[row.result] = roots.get(references[0], (references[0], ()))
    result = {}
    for index, value in enumerate(flow.values):
        if value.kind != "source" or not isinstance(value.origin, tuple) or value.origin[0] not in loads:
            continue
        root, path = loads[value.origin[0]]
        if root in inputs and len(inputs[root]) == 1:
            constants = [module.constant(item) for item in path]
            if None not in constants and len(constants) <= 1:
                field = inputs[root][0]
                result[index] = ("attribute", f"{field.semantic_name}{field.semantic_index}",
                                 constants[0] if constants else value.origin[1])
        elif root in descriptors:
            result[index] = ("data", descriptors[root])
        elif module.names.get(root) and module.definitions[module.definitions[root].type_id].values[1] == 9:
            result[index] = ("data", module.names[root])  # Storage class 9: a push-constant block.
    return result


def _named_sources(ir_kind: str, ir, contract, flow: NativeFlow) -> dict[int, tuple]:
    if ir_kind == "dxbc-sm5":
        return _dxbc_sources(ir, contract, flow)
    if ir_kind == "dxil":
        return _dxil_sources(ir, contract, flow)
    return _spirv_sources(ir, contract, flow)


def _closure(flow: NativeFlow, roots) -> set[int]:
    seen, pending = set(), list(roots)
    while pending:
        index = pending.pop()
        if index not in seen:
            seen.add(index)
            pending.extend(flow.values[index].operands)
    return seen


def vertex_data(ir_kind: str, ir, contract) -> dict[str, tuple]:
    """key -> (output lanes, label, vacuous)."""
    return _vertex_analysis(ir_kind, ir, contract, recovered_surface_flow(ir_kind, ir, contract))[0]


def projected_position(ir_kind: str, ir, contract, flow: NativeFlow) -> ChannelSplit | None:
    values = _vertex_analysis(ir_kind, ir, contract, flow)[1]
    if values is None or any(flow.values[value].scalar not in {"", "float"} for value in values):
        return None
    channel = SurfaceChannel("vertex", "position", "position", 0, tuple(range(len(values))),
                             f"float{len(values)}", f"float{len(values)}")
    return ChannelSplit(channel, tuple(FlowStore("position", 0, lane, value, scope="position")
                                       for lane, value in enumerate(values)))


def _vertex_analysis(ir_kind: str, ir, contract, flow: NativeFlow) -> tuple[dict[str, tuple], tuple | None]:
    named = _named_sources(ir_kind, ir, contract, flow)
    outputs = {(item.semantic_name.casefold(), item.semantic_index): item for item in contract.outputs}
    system = {key: item.system_value for key, item in outputs.items()}
    stores = [store for store in flow.stores if store.scope == "output" and store.value is not None]
    if ir_kind != "dxbc-sm5":
        # DXIL and SPIR-V number lanes within a signature element; DXBC by register component.
        stores = [replace(store, lane=[bit for bit in range(4) if outputs[store.semantic, store.semantic_index].mask
                                       & (1 << bit)][store.lane])
                  for store in stores if (store.semantic, store.semantic_index) in outputs]
    position = [store.value for store in stores if system.get((store.semantic, store.semantic_index)) == "position"]
    reach = {}
    for store in stores:
        if system.get((store.semantic, store.semantic_index), "undefined") != "undefined":
            continue
        sources = {named[index] for index in _closure(flow, (store.value,)) if index in named}
        attributes = sorted({source[1] for source in sources if source[0] == "attribute"})
        data = tuple(sorted({source[1] for source in sources if source[0] == "data"}))
        copy = named.get(store.value) if named.get(store.value, ("",))[0] == "attribute" else None
        reach[(store.semantic, store.semantic_index, store.lane)] = (store.value, tuple(attributes), data, copy)
    lanes: dict[str, list] = {}
    labels: dict[str, str] = {}
    copies: dict[str, dict[int, tuple]] = {}
    for where, (_value, attributes, data, copy) in reach.items():
        if copy is not None:
            copies.setdefault(copy[1], {}).setdefault(copy[2], where)
        else:
            key = f"derived:{'+'.join(attributes)}|{'+'.join(data)}"
            lanes.setdefault(key, []).append(where)
            labels[key] = translate("SurfaceData", "{attributes} via {data}").format(
                attributes=", ".join(attributes) or translate("SurfaceData", "no attribute"),
                data=", ".join(data) or translate("SurfaceData", "arithmetic"))
    for attribute, by_lane in copies.items():
        name = f"{attribute}.{''.join('xyzw'[lane] for lane in sorted(by_lane))}"
        lanes[f"attribute:{name}"] = [by_lane[lane] for lane in sorted(by_lane)]
        labels[f"attribute:{name}"] = translate("SurfaceData", "Vertex {attribute}").format(attribute=name)
    groups = {key: value for key, value in lanes.items() if len(value) <= 4}
    result = {key: (tuple(where), labels[key], False) for key, where in groups.items()}
    values = _projection(ir_kind, ir, flow, position,
                         {index for index, source in named.items() if source[0] == "attribute"})
    if values is None:
        return result, None
    sources = {named[index] for index in _closure(flow, values) if index in named}
    space = tuple(sorted({source[1] for source in sources if source[0] == "data"}))
    # Attributes that place the position place everything computed like it.
    placed = [set(reach[item][1]) for where in groups.values() for item in where
              if set(reach[item][2]) <= set(space) and bool(reach[item][2]) == bool(space)]
    placing = {source[1] for source in sources if source[0] == "attribute"}
    if len({frozenset(item) for item in placed}) > 1:
        placing &= set.intersection(*placed)
    held = next((key for key, where in groups.items() if {reach[item][0] for item in where} == set(values)
                 and len(where) == len(values)), None)
    if held is not None:
        order = {value: index for index, value in enumerate(values)}
        result["position"] = (tuple(sorted(groups[held], key=lambda item: order[reach[item][0]])),
                              translate("SurfaceData", "Position the pass projects"), False)
        if held.startswith("derived:"):
            del result[held]
    for key, where in groups.items():
        attributes = {tuple(sorted(set(reach[item][1]) - placing)) for item in where}
        if (key != held and len(attributes) == 1 and len(next(iter(attributes))) == 1
                and all(SCALAR_TYPES.get(outputs[item[:2]].component_type) == "float" for item in where)
                and all(set(reach[item][2]) <= set(space) and bool(reach[item][2]) == bool(space)
                        for item in where)):
            attribute = next(iter(attributes))[0]
            # Without engine data the value is stored as is, so the key is vacuous.
            result[f"space:{attribute}"] = (tuple(where), translate(
                "SurfaceData", "{attribute} placed like the position").format(attribute=attribute), not space)
            if key.startswith("derived:"):
                del result[key]
    return result, values


def _projection(ir_kind: str, ir, flow: NativeFlow, position, attributes) -> tuple[int, ...] | None:
    if not position:
        return None
    closures = [_closure(flow, (value,)) for value in position]
    shared, projecting = set.intersection(*closures), set.union(*closures)
    consumers: dict[int, set[int]] = {}
    for index, value in enumerate(flow.values):
        for operand in value.operands:
            consumers.setdefault(operand, set()).add(index)
    projection = projecting - shared
    frontier = [value for value in sorted(shared) if consumers.get(value, set()) & projection
                and _closure(flow, (value,)) & attributes]
    if len(frontier) != 3 or any(flow.values[value].scalar not in {"", "float"} for value in frontier):
        return None
    words = _words(ir_kind, ir, flow)
    keys = []
    for value in frontier:
        # Each component multiplies one row or column of the projection.
        found = {words[operand] for consumer in consumers[value] & projection
                 for operand in flow.values[consumer].operands if operand in words}
        rows, lanes = {word[:2] for word in found}, {word[2] for word in found}
        if len(found) >= 2 and (len(rows) == 1) != (len(lanes) == 1):
            keys.append(("row", *next(iter(rows))) if len(rows) == 1 else ("lane", next(iter(lanes))))
    if (len(keys) == 3 and len({key[0] for key in keys}) == 1 and len(set(keys)) == 3
            and (keys[0][0] == "lane" or len({key[1] for key in keys}) == 1)):
        return tuple(value for _key, value in sorted(zip(keys, frontier)))
    # Row dot products read every component: the vector's own lanes order them.
    origins = [flow.values[value].origin for value in frontier]
    if ir_kind == "dxbc-sm5":
        # A value joined from branches lives in its register from the join on.
        origins = [(origin[1], f"r{origin[2][1]}", origin[2][2]) if origin[0] == "join" and origin[2][0] == "r"
                   else origin for origin in origins]
        homes = [origin[-2:] for origin in origins if isinstance(origin, tuple) and len(origin) in {2, 3}]
    else:
        homes = [origin for origin in origins if isinstance(origin, tuple) and len(origin) == 2]
    if len(homes) == 3 and len({home[0] for home in homes}) == 1 and len({home[1] for home in homes}) == 3:
        return tuple(value for _home, value in sorted(zip(homes, frontier), key=lambda item: item[0][1]))
    return None


_DXIL_CONSTANT_ROW = re.compile(r"@dx\.op\.cbufferLoadLegacy\.\w+\(i32 59, %dx\.types\.Handle (%[\w.]+), i32 (\d+)\)")
_DXIL_EXTRACT = re.compile(r"extractvalue %dx\.types\.CBufRet\.\w+ (%[\w.]+), (\d+)")


def _words(ir_kind: str, ir, flow: NativeFlow) -> dict[int, tuple]:
    """Flow value -> (buffer, register, lane) of the constant buffer word it reads."""
    if ir_kind == "dxbc-sm5":
        return {index: value.origin[1:] for index, value in enumerate(flow.values)
                if value.kind == "source" and isinstance(value.origin, tuple) and len(value.origin) == 4
                and value.origin[0] == "cb"}
    if ir_kind != "dxil":
        return {}
    rows, words = {}, {}
    for line in ir.to_assembly().splitlines():
        match = _DXIL_LINE.match(line)
        if match is None:
            continue
        result, body = match.groups()
        if (found := _DXIL_CONSTANT_ROW.search(body)) is not None:
            rows[result] = (found[1], int(found[2]))
        elif (found := _DXIL_EXTRACT.search(body)) is not None and found[1] in rows:
            words[result] = (*rows[found[1]], int(found[2]))
    return {index: words[value.origin] for index, value in enumerate(flow.values) if value.origin in words}


def texture_read_inputs(ir_kind: str, ir, contract) -> dict[str, frozenset]:
    flow = recovered_surface_flow(ir_kind, ir, contract)
    named = _named_sources(ir_kind, ir, contract, flow)
    fields = {f"{item.semantic_name}{item.semantic_index}": item for item in contract.inputs}
    result: dict[str, set] = {}
    for store in flow.stores:
        if store.scope != "read" or store.value is None:
            continue
        lanes = result.setdefault(store.semantic, set())
        for index in _closure(flow, (store.value,)):
            source = named.get(index)
            if source is not None and source[0] == "attribute" and source[1] in fields:
                field = fields[source[1]]
                physical = [bit for bit in range(4) if field.mask & (1 << bit)]
                if source[2] < len(physical):
                    lanes.add((field.semantic_name, field.semantic_index, physical[source[2]]))
    return {name: frozenset(lanes) for name, lanes in result.items()}


def _native(compute, document, profile, program, role: str):
    kind, data, contract, digest = _shader_bytes(document, profile, program, role)
    key = compute.__name__, f"{kind}:{digest}", contract.fingerprint
    found = _CACHE.get(key)
    if found is None:
        found = compute(kind, _recover_ir(kind, data), contract)
        _CACHE.put(key, found, 0, _CACHE_LIMIT, 0)
    return found


def _material_coordinates(document, profile, program, data) -> SurfaceDatum | None:
    reads = _native(texture_read_inputs, document, profile, program, "pixel").values()
    candidates = {datum.lanes for datum in data if datum.value_type == "float2" and not datum.key.startswith("system:")}
    # A read from two UV sets doesn't tell which is the main one.
    votes = Counter(found[0] for read in reads
                    if len(found := [lanes for lanes in candidates if set(lanes) <= read]) == 1)
    ranked = votes.most_common(2)
    if not ranked or len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return SurfaceDatum(MATERIAL_COORDINATES, translate("SurfaceData", "UV the material's textures use"), "float2",
                        ranked[0][0])


def program_surface_data(document, profile, target_index: int, program_index: int) -> tuple[SurfaceDatum, ...]:
    program = document.sdf.targets[int(target_index)].programs[int(program_index)]
    pixel = program.shader("pixel")
    if pixel is None or not pixel.present:
        return ()
    contract = profile.shader_contract(document, program, "pixel")
    fields = {(item.semantic_name.casefold(), item.semantic_index): item for item in contract.inputs}
    result = []
    for field in contract.inputs:
        if field.system_value not in {"", "undefined"} and field.component_type in SCALAR_TYPES:
            width = field.mask.bit_count()
            result.append(SurfaceDatum(
                f"system:{field.system_value}", field.semantic_name, _vector(SCALAR_TYPES[field.component_type], width),
                tuple((field.semantic_name, field.semantic_index, component) for component in range(4)
                      if field.mask & (1 << component))))
    stages = {shader.role for shader in program.shaders if shader.present}
    if stages & {"geometry", "hull", "domain"} or "vertex" not in stages:
        return tuple(result)
    for key, (where, label, vacuous) in _native(vertex_data, document, profile, program, "vertex").items():
        picked = []
        for semantic, index, component in where:
            field = fields.get((semantic, index))
            if field is None or not field.mask & (1 << component) or field.component_type not in SCALAR_TYPES:
                break
            picked.append((field.semantic_name, field.semantic_index, component, SCALAR_TYPES[field.component_type]))
        else:
            if len({item[3] for item in picked}) == 1:
                result.append(SurfaceDatum(key, label, _vector(picked[0][3], len(picked)),
                                           tuple(item[:3] for item in picked), vacuous))
    coordinates = _material_coordinates(document, profile, program, result)
    return tuple(result) + ((coordinates,) if coordinates is not None else ())


def program_vertex_inputs(document, profile, target_index: int, program_index: int) -> tuple[SurfaceDatum, ...]:
    program = document.sdf.targets[int(target_index)].programs[int(program_index)]
    vertex = program.shader("vertex")
    if vertex is None or not vertex.present:
        return ()
    result = []
    for field in profile.shader_contract(document, program, "vertex").inputs:
        if field.component_type not in SCALAR_TYPES:
            continue
        lanes = tuple((field.semantic_name, field.semantic_index, component) for component in range(4)
                      if field.mask & (1 << component))
        value_type = _vector(SCALAR_TYPES[field.component_type], len(lanes))
        if field.system_value not in {"", "undefined"}:
            result.append(SurfaceDatum(f"system:{field.system_value}", field.semantic_name, value_type, lanes))
            continue
        name = f"{field.semantic_name}{field.semantic_index}.{''.join('xyzw'[lane[2]] for lane in lanes)}"
        result.append(SurfaceDatum(f"attribute:{name}", translate("SurfaceData", "Vertex {attribute}").format(
            attribute=name), value_type, lanes))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SurfaceDataOffer:
    key: str
    label: str
    value_type: str
    # Purpose: (passes with it, passes with a pixel shader).
    purposes: dict
    # Purpose: passes that hand it on only when a graph reads it.
    carried: dict


def document_surface_data(document, profile) -> tuple[SurfaceDataOffer, ...]:
    totals, found, carried, labels, real = {}, {}, {}, {}, set()
    for target in document.sdf.targets:
        for program in target.programs:
            pixel = program.shader("pixel")
            if pixel is None or not pixel.present:
                continue
            purpose = pass_purpose(document, profile, target.index, program.program_index)
            totals[purpose] = totals.get(purpose, 0) + 1
            try:
                data = program_surface_data(document, profile, target.index, program.program_index)
                inputs = (program_vertex_inputs(document, profile, target.index, program.program_index)
                          if not linkage_problem(document, profile, program)
                          and first_register(document, profile, program) < MAX_REGISTERS else ())
            except ShaderGraphError:
                continue
            handed = {(datum.key, datum.value_type) for datum in data}
            for datum in (*data, *(item for item in inputs if (item.key, item.value_type) not in handed)):
                key = (datum.key, datum.value_type)
                found.setdefault(key, {}).setdefault(purpose, 0)
                found[key][purpose] += 1
                if key not in handed:
                    carried.setdefault(key, {}).setdefault(purpose, 0)
                    carried[key][purpose] += 1
                labels.setdefault(key, datum.label)
                if not datum.vacuous:
                    real.add(key)
    order = ("coordinates", "position", "space:", "attribute:", "system:", "derived:")
    return tuple(SurfaceDataOffer(key, labels[key, value_type], value_type,
                                  {purpose: (count, totals[purpose])
                                   for purpose, count in found[key, value_type].items()},
                                  carried.get((key, value_type), {}))
                 for key, value_type in sorted(real, key=lambda item: (
                     next(index for index, prefix in enumerate(order) if item[0].startswith(prefix)), item)))


def _node_datum(kind, properties):
    if kind == SURFACE_DATA_KIND:
        return str(properties.get("datum", "")), str(properties.get("value_type", ""))
    return (MATERIAL_COORDINATES, "float2") if kind == "texture_coordinates" else None


def surface_data_keys(node) -> list[tuple[str, str]]:
    """(key, value type) pairs."""
    if node.kind == RE_ENGINE_RECOVERED_MASTER_KIND:
        return []  # Its properties are the recovered programs, which nest no graph.
    found = []

    def visit(value):
        if isinstance(value, dict):
            datum = _node_datum(value.get("kind"), value.get("properties", {}))
            if datum is not None:
                found.append(datum)
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    datum = _node_datum(node.kind, node.properties)
    if datum is not None:
        found.append(datum)
    visit(node.properties)
    return found


def graph_reads_surface_data(graph) -> bool:
    return any(surface_data_keys(node) for node in graph.nodes)


def surface_data_available(semantics, keys) -> bool:
    present = {(datum.key, datum.value_type) for datum in semantics.surface_data}
    return all(key in present or semantics.shader_role == "pixel" and carried_value(semantics, *key) is not None
               for key in keys)


def surface_data_value(semantics, contract, properties) -> str:
    key = str(properties.get("datum", ""))
    if contract.stage not in {"vertex", "pixel"}:
        raise ShaderGraphError("Surface data is read by vertex and pixel shaders only.")
    datum = next((item for item in semantics.surface_data if item.key == key), None)
    if datum is None and contract.stage == "vertex":
        raise ShaderGraphError(f"{properties.get('label', key)} is not an input of the vertex shader of "
                               f"{semantics.name}. Use Where Available or Drawing For for passes without it.")
    if datum is None:
        carried = carried_value(semantics, key, str(properties.get("value_type", "")))
        if carried is not None:
            return carried.pixel_expression()
        if key == MATERIAL_COORDINATES:
            raise ShaderGraphError(f"{semantics.name} does not read its textures with one UV set, so it has no "
                                   "texture coordinates of its own. Connect a UV set from Surface Data, or use Where "
                                   "Available.")
        raise ShaderGraphError(f"{properties.get('label', key)} is not handed to the pixel shader of "
                               f"{semantics.name}. Use Where Available or Drawing For for passes without it.")
    if datum.value_type != properties.get("value_type"):
        raise ShaderGraphError(f"{datum.label} is {datum.value_type} in {semantics.name}, "
                               f"not {properties.get('value_type')}.")
    return surface_datum_expression(contract, datum)


def surface_datum_expression(contract, datum: SurfaceDatum) -> str:
    parts = []
    for semantic, index, component in datum.lanes:
        field = contract.input(semantic, index)
        if field is None or not field.mask & (1 << component):
            raise ShaderGraphError(f"{datum.label} is not an input of this {contract.stage} shader.")
        part = f"v{int(field.register)}.{'xyzw'[component]}"
        if field.component_type in {"uint32", "sint32"}:
            part = f"as{'uint' if field.component_type == 'uint32' else 'int'}({part})"
        parts.append(part)
    return parts[0] if len(parts) == 1 else f"{datum.value_type}({', '.join(parts)})"


__all__ = [
    "MATERIAL_COORDINATES", "SURFACE_DATA_KIND", "SurfaceDataOffer", "SurfaceDatum", "document_surface_data",
    "graph_reads_surface_data", "program_surface_data", "program_vertex_inputs", "projected_position",
    "surface_data_available",
    "surface_data_keys", "surface_data_value", "surface_datum_expression", "texture_read_inputs", "vertex_data",
]
