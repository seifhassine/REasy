import hashlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

from file_handlers.sdf.dxc import enrich_dxil_shader
lazy from file_handlers.material_names import is_private_material_parameter
from file_handlers.sdf.aggregate_values import (
    AggregateField, aggregate_fields, aggregate_type, aggregate_helpers, constant_value_fields,
)

from file_handlers.sdf.shader_graph import (
    ShaderContractResource, ShaderGraphError, ShaderGraphNodeExtension, ShaderPin,
    register_shader_graph_node_extension,
)
from PySide6.QtCore import QT_TRANSLATE_NOOP
lazy from file_handlers.mmtr.engine_inputs import engine_input_catalog_for, resource_from_dict, resource_to_dict
lazy from file_handlers.mmtr.material_region_read import per_build, region_derived_cache
lazy from file_handlers.mmtr.pass_purpose import pass_purpose
lazy from file_handlers.sdf.hlsl_contract import resource_identifier
lazy from file_handlers.sdf.shader_graph_resource import resource_value_type


RENDERER_VALUE_KIND = "re_engine_renderer_value"
RENDERER_RESOURCE_KIND = "re_engine_renderer_resource"
WHERE_AVAILABLE_KIND = "re_engine_where_available"


def _pins(node, _contract):
    return (ShaderPin("value", "Value", node.properties["value_type"], "output"),)


def _expression(_node, _pin, _incoming):
    raise ShaderGraphError("Renderer values require the native material contract.")


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind=RENDERER_VALUE_KIND, title="Renderer Value", pins_provider=_pins,
    expression=_expression,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", "Reads a named renderer constant without adding an MDF parameter."),
))


def _resource_pins(node, _contract):
    return (ShaderPin("value", "Resource", node.properties["value_type"], "output"),)


def _resource_expression(_node, _pin, _incoming):
    raise ShaderGraphError("Engine resources require the native material contract.")


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind=RENDERER_RESOURCE_KIND, title="Engine Resource", pins_provider=_resource_pins,
    expression=_resource_expression,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", "A texture, buffer or sampler the engine binds by name."),
))


def _where_available_pins(node, _contract):
    value_type = str(node.properties.get("value_type", "float"))
    return (ShaderPin("value", "Value", value_type, "input", True),
            ShaderPin("otherwise", "Otherwise", value_type, "input", True),
            ShaderPin("result", "Result", value_type, "output"))


def _where_available_expression(_node, _pin, _incoming):
    raise ShaderGraphError("Where Available requires the native material contract.")


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind=WHERE_AVAILABLE_KIND, title="Where Available", pins_provider=_where_available_pins,
    expression=_where_available_expression,
    description=QT_TRANSLATE_NOOP(
        "ShaderGraphNodes",
        "Uses Value in the rendering passes that provide every engine input and surface data it reads, and "
        "Otherwise in the rest."),
))


def renderer_only(document, profile, project=None):
    return (not (project is not None and (project.parameters or project.textures))
            and not any(not is_private_material_parameter(p.name) for p in document.material_interface.parameters)
            and not profile.editable_textures(document))


class RendererValueUnavailable(ShaderGraphError):
    def __init__(self, label, buffer):
        super().__init__(f"Renderer value {label} is absent from this shader.")
        self.label = label
        self.buffer = buffer


class RendererResourceUnavailable(ShaderGraphError):
    def __init__(self, properties):
        self.label = str(properties["label"])
        super().__init__(f"Engine resource {self.label} is absent from this shader.")
        self.properties = properties


@dataclass(frozen=True)
class EngineInput:
    pin: Mapping[str, Any]
    passes: int
    total_passes: int
    materials: int = 0
    total_materials: int = 0
    # Pass purpose -> (passes that can read it, passes of that purpose).
    purposes: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    # (target, program, shader role) of each pass that can read it.
    routes: frozenset = frozenset()


# D3D11/12 register counts per stage.
_REGISTERS = {"b": 14, "t": 128, "s": 16}


def _renderer_pin(buffer, variable, cache):
    cache_key = (buffer.name, variable)
    if cache_key not in cache:
        layout = constant_value_fields(variable)
        aggregate = len(layout) != 1 or layout[0].path != ""
        value_type = aggregate_type(layout) if aggregate else layout[0].value_type
        key = (buffer.name, variable.name, variable.byte_offset, value_type,
               tuple((field.path, field.value_type, field.offsets) for field in layout))
        identity = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:24]
        cache[cache_key] = key, dict(
            pin="renderer_" + identity,
            label=buffer.name + "." + variable.name,
            value_type=value_type, input_kind="renderer_value",
            source_buffer=buffer.name, source_name=variable.name,
            source_byte_offset=variable.byte_offset,
            **({"value_layout": [field.to_dict() for field in layout]} if aggregate else {}),
        )
    return cache[cache_key]


def _buffer_pins(buffer, cache):
    return [_renderer_pin(buffer, variable, cache) for variable in buffer.variables
            if not variable.name.startswith(("REasyReserved_", "REasyOpaque_"))]


def renderer_input_pins(document, profile):
    fields, seen, cache = {}, set(), {}
    for target, program, role in profile.material_program_routes(document):
        source_program = document.sdf.targets[target].programs[program]
        shader = source_program.shader(role)
        contract = profile.shader_contract(document, source_program, role)
        identity = (contract.source_sha256, contract.fingerprint)
        if identity in seen:
            continue
        seen.add(identity)
        used = None
        if shader.reflection is not None:
            reflected = shader.reflection
            if reflected.reflection is None:
                reflected = enrich_dxil_shader(reflected, shader.payload)
            if reflected.reflection is not None:
                used = {(b.name, v.name) for b in reflected.reflection.constant_buffers
                        for v in b.variables if v.flags & 2}
        for buffer in contract.constant_buffers:
            for key, pin in _buffer_pins(buffer, cache):
                if used is None or (buffer.name, pin["source_name"]) in used:
                    fields[key] = pin
    return tuple(fields[key] for key in sorted(fields))


def engine_buffer_layout(document, program, stage, name):
    return _engine_input_layout(document, program, stage, name, resources=False)


def _engine_input_layout(document, program, stage, name, *, resources: bool):
    shader = program.shader(stage)
    if shader is None or not shader.present:
        return None, f"{program.name} has no {stage} shader."
    catalog = engine_input_catalog_for(document.profile)
    if catalog is None:
        return None, (f"No engine input catalog exists for {document.profile.game} build "
                      f"{document.profile.build_resource_version}; generate it with tools/engine_input_catalog.py.")
    backend = "Vulkan " if shader.binary_format == "spirv-module" else ""
    passes = catalog.pass_resources if resources else catalog.pass_buffers
    found = passes(shader.binary_format, program.name, stage).get(name, ())
    if not found:
        return None, f"The game never binds {name} in {backend}{program.name} {stage}."
    if len(found) > 1:
        return None, (f"The game binds {name} with {len(found)} different {'declarations' if resources else 'layouts'}"
                      f" in {backend}{program.name} {stage}.")
    layout = (catalog.resources if resources else catalog.layouts)[found[0]]
    if (layout.resource if resources else layout.buffer).register_space != 0:
        return None, f"The game binds {name} outside register space 0."
    return layout, ""


def free_register(document, contract, stage, register_class):
    occupied = {slot for resource in contract.resources
                if resource.register_class == register_class and resource.register_space == 0
                for slot in range(resource.bind_point, resource.bind_point + max(1, resource.bind_count))}
    if register_class == "b":
        occupied.update(buffer.bind_point for buffer in contract.constant_buffers if buffer.register_space == 0)
    occupied.update(slot for role, _name, kind, slot, space in document.profile.sdf_profile.implicit_resource_bindings
                    if role == stage and kind == register_class and space == 0)
    return next((slot for slot in range(_REGISTERS[register_class]) if slot not in occupied), None)


def _next_resource_id(program, stage, contract, register_class):
    """DXBC and SPIR-V do not number bindings."""
    if program.shader(stage).binary_format != "dxbc-container" or contract.model_major < 6:
        return 0
    return 1 + max((item.resource_id for item in contract.resources if item.register_class == register_class), default=-1)


def add_engine_buffers(document, contract, semantics, names):
    program = document.sdf.targets[semantics.target_index].programs[semantics.program_index]
    stage = semantics.shader_role
    for name in sorted(names):
        if contract.buffer(name) is not None:
            continue
        layout, _reason = engine_buffer_layout(document, program, stage, name)
        slot = free_register(document, contract, stage, "b") if layout is not None else None
        if slot is None:
            continue
        flags = {item.flags for item in contract.resources if item.input_type == "constant-buffer"}
        resource = ShaderContractResource(
            name, "constant-buffer", "unknown", "unknown", 0, slot, 1, flags.pop() if len(flags) == 1 else 0, 0,
            _next_resource_id(program, stage, contract, "b"),
        )
        contract = replace(
            contract,
            resources=(*contract.resources, resource),
            constant_buffers=tuple(sorted((*contract.constant_buffers, replace(layout.buffer, bind_point=slot)),
                                          key=lambda item: (item.register_space, item.bind_point))),
        )
    return contract


def engine_buffer_unavailable_reason(document, program, stage, contract, name):
    if contract.buffer(name) is not None:
        return f"This shader binds {name} with a layout that lacks the value."
    layout, reason = engine_buffer_layout(document, program, stage, name)
    if layout is None:
        return reason
    if free_register(document, contract, stage, "b") is None:
        return f"This shader has no free constant buffer register for {name}."
    return f"The game's {name} layout in {program.name} {stage} lacks the value."


def _nested_nodes(node, kinds):
    cache, key = region_derived_cache("nested_engine_nodes"), (id(node), frozenset(kinds))
    if cache is not None and key in cache:
        return cache[key][1]
    found = [(node.kind, node.properties)] if node.kind in kinds else []

    def visit(value):
        if isinstance(value, dict):
            if value.get("kind") in kinds:
                found.append((value["kind"], value.get("properties", {})))
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(node.properties)
    if cache is not None:
        cache[key] = node, tuple(found)
    return tuple(found)


def _graph_names(graph, kind, key):
    return per_build("graph_engine_names", (id(graph), kind, key), lambda: frozenset(
        str(properties.get(key, "")) for node in graph.nodes for _kind, properties in _nested_nodes(node, {kind})) - {""})


def graph_renderer_buffers(graph):
    return _graph_names(graph, RENDERER_VALUE_KIND, "source_buffer")


def graph_renderer_resources(graph):
    return _graph_names(graph, RENDERER_RESOURCE_KIND, "source_resource")


def engine_input_plan(document, program, stage, buffers, resources):
    """The game binds engine inputs per pass, so passes with equal shaders can differ."""
    if not (buffers or resources):
        return ()
    catalog = engine_input_catalog_for(document.profile)
    shader = program.shader(stage)
    if catalog is None or shader is None:
        return ()
    backend = shader.binary_format
    bound_buffers = catalog.pass_buffers(backend, program.name, stage)
    bound_resources = catalog.pass_resources(backend, program.name, stage)
    return (tuple((name, bound_buffers.get(name, ())) for name in sorted(buffers)),
            tuple((name, bound_resources.get(name, ())) for name in sorted(resources)))


def engine_resource_value_type(resource):
    if resource.register_class not in {"t", "s"}:
        return None
    try:
        return resource_value_type(resource)
    except ValueError:
        return None


# D3D_SIF_TEXTURE_COMPONENT_0/1: a texture's declared vector width minus one.
_TEXTURE_COMPONENTS = 0b1100


def texel_components(resource):
    return ((resource.flags & _TEXTURE_COMPONENTS) >> 2) + 1 if resource.input_type == "texture" else 0


@lru_cache(maxsize=4096)
def _shape(resource):
    """Shaders reading one texture vary in its declared component count."""
    flags = resource.flags & ~_TEXTURE_COMPONENTS if resource.input_type == "texture" else resource.flags
    return replace(resource, bind_point=0, resource_id=0, flags=flags)


def _supplies(resource, declaration):
    return _shape(resource) == _shape(declaration) and texel_components(resource) >= texel_components(declaration)


def engine_resource_declaration(catalog, name):
    found = [item.resource for item in catalog.resources if item.name == name]
    shapes = {_shape(item) for item in found}
    if len(shapes) != 1:
        return None
    declaration = shapes.pop()
    if declaration.input_type == "texture":
        declaration = replace(declaration, flags=declaration.flags | (min(map(texel_components, found)) - 1) << 2)
    return declaration if engine_resource_value_type(declaration) is not None else None


def engine_resource_pin(declaration):
    signature = resource_to_dict(declaration)
    identity = hashlib.sha256(repr(sorted(signature.items())).encode("utf-8")).hexdigest()[:24]
    return dict(pin="renderer_resource_" + identity, label=declaration.name,
                value_type=engine_resource_value_type(declaration), input_kind="renderer_resource",
                source_resource=declaration.name, resource=signature)


def engine_resource_layout(document, program, stage, name):
    return _engine_input_layout(document, program, stage, name, resources=True)


def _engine_resource_signature(properties):
    return resource_from_dict(properties["resource"])


def _bound_resource(contract, properties):
    resource = contract.resource(str(properties["source_resource"]))
    return resource if resource is not None and _supplies(resource, _engine_resource_signature(properties)) else None


def add_engine_resources(document, contract, semantics, names):
    program = document.sdf.targets[semantics.target_index].programs[semantics.program_index]
    stage = semantics.shader_role
    for name in sorted(names):
        if contract.resource(name) is not None:
            continue
        layout, _reason = engine_resource_layout(document, program, stage, name)
        if layout is None:
            continue
        register_class = layout.resource.register_class
        slot = free_register(document, contract, stage, register_class)
        if slot is None:
            continue
        resource = replace(layout.resource, bind_point=slot,
                           resource_id=_next_resource_id(program, stage, contract, register_class))
        contract = replace(contract, resources=(*contract.resources, resource))
    return contract


def engine_resource_unavailable_reason(document, program, stage, contract, properties):
    name = str(properties["source_resource"])
    if contract.resource(name) is not None:
        return f"This shader declares {name} differently."
    layout, reason = engine_resource_layout(document, program, stage, name)
    if layout is None:
        return reason
    if not _supplies(layout.resource, _engine_resource_signature(properties)):
        return f"The game declares {name} differently in {program.name} {stage}."
    return f"This shader has no free {layout.resource.register_class} register for {name}."


def engine_inputs_available(contract, nodes):
    for kind, properties in nodes:
        if kind == RENDERER_VALUE_KIND and renderer_buffer(contract, properties) is None:
            return False
        if kind == RENDERER_RESOURCE_KIND and _bound_resource(contract, properties) is None:
            return False
    return True


def engine_input_nodes(node):
    return _nested_nodes(node, {RENDERER_VALUE_KIND, RENDERER_RESOURCE_KIND})


def renderer_resource_expression(contract, properties):
    resource = _bound_resource(contract, properties)
    if resource is None:
        raise RendererResourceUnavailable(properties)
    return resource_identifier(resource), resource


def recovered_structured_word(identifier, stride, index, byte_offset):
    """Rebuilt shaders declare the buffer's records as uint slots."""
    words = int(stride) // 4
    slot, component = divmod(int(byte_offset) // 4, 4)
    width = min(4, words - slot * 4)
    return f"{identifier}[{index}].slot{slot}" + (f".{'xyzw'[component]}" if width > 1 else "")


def recovered_structured_slot(identifier, stride, index, byte_offset):
    return f"{identifier}[{index}].slot{int(byte_offset) // 16}"


def _engine_resource_entries(document, program, role, contract, catalog, declarations):
    readable, free = {}, {}
    for name in sorted(catalog.pass_resources(program.shader(role).binary_format, program.name, role)):
        if name not in declarations:
            declaration = engine_resource_declaration(catalog, name)
            declarations[name] = (declaration, engine_resource_pin(declaration)) if declaration is not None else None
        if declarations[name] is None:
            continue
        declaration, pin = declarations[name]
        bound = contract.resource(name)
        if bound is not None:
            usable = _supplies(bound, declaration)
        else:
            layout, _reason = engine_resource_layout(document, program, role, name)
            register_class = layout.resource.register_class if layout is not None else None
            if register_class not in free and layout is not None:
                free[register_class] = free_register(document, contract, role, register_class)
            usable = layout is not None and _supplies(layout.resource, declaration) and free[register_class] is not None
        if usable:
            readable[("resource", pin["pin"])] = pin
    return readable


def material_engine_inputs(document, profile):
    catalog = engine_input_catalog_for(document.profile)
    user_buffers = {name.casefold() for name in document.profile.user_material_buffer_names}
    routes = profile.material_program_routes(document)
    cache, bound_by_shader, readable_by_pass, declarations = {}, {}, {}, {}
    fields, counts, by_purpose, purpose_totals, readers = {}, {}, {}, Counter(), {}
    for target, index, role in routes:
        program = document.sdf.targets[target].programs[index]
        purpose = pass_purpose(document, profile, target, index)
        purpose_totals[purpose] += 1
        contract = profile.shader_contract(document, program, role)
        identity = (contract.source_sha256, contract.fingerprint)
        if identity not in bound_by_shader:
            bound_by_shader[identity] = {key: pin for buffer in contract.constant_buffers
                                         if buffer.name.casefold() not in user_buffers
                                         for key, pin in _buffer_pins(buffer, cache)}
        pass_key = (identity, program.name, role)
        if pass_key not in readable_by_pass:
            readable, free_buffer = dict(bound_by_shader[identity]), None
            if catalog is not None:
                for name in catalog.pass_buffers(program.shader(role).binary_format, program.name, role):
                    if name.casefold() in user_buffers or contract.buffer(name) is not None:
                        continue
                    layout, _reason = engine_buffer_layout(document, program, role, name)
                    if layout is None:
                        continue
                    if free_buffer is None:
                        free_buffer = (free_register(document, contract, role, "b"),)
                    if free_buffer[0] is not None:
                        readable.update(_buffer_pins(layout.buffer, cache))
                readable.update(_engine_resource_entries(document, program, role, contract, catalog, declarations))
            readable_by_pass[pass_key] = readable
        for key, pin in readable_by_pass[pass_key].items():
            fields[key] = pin
            counts[key] = counts.get(key, 0) + 1
            readers.setdefault(key, set()).add((target, index, role))
            by_purpose.setdefault(key, Counter())[purpose] += 1
    def materials(pin):
        if catalog is None:
            return 0
        if pin["input_kind"] == "renderer_resource":
            return catalog.resource_materials.get(pin["source_resource"], 0)
        return catalog.buffer_materials.get(pin["source_buffer"], 0)

    return tuple(EngineInput(fields[key], counts[key], len(routes), materials(fields[key]),
                             catalog.materials if catalog is not None else 0,
                             {purpose: (by_purpose[key][purpose], total) for purpose, total in purpose_totals.items()},
                             frozenset(readers[key]))
                 for key in sorted(fields))


def renderer_buffer(contract, properties):
    buffer = contract.buffer(str(properties["source_buffer"]))
    if buffer is None or buffer.register_space and contract.model_major < 6:
        return None
    fields = renderer_fields(properties)
    return buffer if any(v.name == properties["source_name"] and v.byte_offset == properties["source_byte_offset"]
                         and constant_value_fields(v) == fields for v in buffer.variables) else None


def renderer_fields(properties):
    if "value_layout" in properties:
        fields = aggregate_fields(properties)
        if properties["value_type"] != aggregate_type(fields):
            raise ShaderGraphError("Renderer aggregate type does not match its declared fields")
        return fields
    value_type = properties["value_type"]
    width = int(value_type[-1]) if value_type[-1].isdigit() else 1
    offset = int(properties["source_byte_offset"])
    return (AggregateField("", value_type, tuple(offset + index * 4 for index in range(width))),)


def renderer_field_expression(contract, properties, field):
    buffer = renderer_buffer(contract, properties)
    if buffer is None:
        raise RendererValueUnavailable(str(properties["label"]), str(properties["source_buffer"]))
    if contract.model_major >= 6:
        return str(properties["source_name"]) + field.path
    words = [f"REasyCB{buffer.bind_point}[{offset // 16}].{'xyzw'[offset % 16 // 4]}" for offset in field.offsets]
    if field.value_type.startswith("float"):
        words = [f"asfloat({word})" for word in words]
    elif field.value_type.startswith("int"):
        words = [f"asint({word})" for word in words]
    elif field.value_type.startswith("bool"):
        words = [f"({word} != 0u)" for word in words]
    return words[0] if len(words) == 1 else f"{field.value_type}({', '.join(words)})"


def renderer_value_expression(contract, properties, helpers):
    fields = renderer_fields(properties)
    values = [renderer_field_expression(contract, properties, field) for field in fields]
    if "value_layout" not in properties:
        return values[0]
    value_type = aggregate_type(fields)
    helpers.setdefault(value_type, aggregate_helpers(fields))
    return f"{value_type}_Make({', '.join(values)})"
