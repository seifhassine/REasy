from __future__ import annotations

import re

lazy from file_handlers.sdf.hlsl_contract import resource_identifier
from file_handlers.sdf.shader_graph import (
    ShaderContract,
    ShaderExpression,
    ShaderExpressionCompiler,
    ShaderGraphError,
    ShaderGraphNode,
    ShaderPin,
    mask_components,
    math_node_expression,
    node_pins,
    TEXTURE_COORDINATE_TYPES,
    shader_value_literal,
    splat_expression,
)
from file_handlers.sdf.shader_graph_math import value_width
lazy from file_handlers.sdf.aggregate_values import (
    aggregate_expression, aggregate_fields, aggregate_helpers, aggregate_pins, aggregate_projection, aggregate_type,
)
lazy from file_handlers.sdf.shader_graph_buffer import BufferRead
lazy from file_handlers.sdf.shader_graph_code import code_expression, code_helpers
lazy from file_handlers.sdf.shader_graph_execution import EXECUTION_KINDS
lazy from file_handlers.sdf.shader_graph_indexed_value import indexed_expression, indexed_helpers, indexed_shape
lazy from file_handlers.sdf.shader_graph_math import TYPED_MATH_NODES, math_context_error, typed_math_expression, typed_math_helpers
lazy from file_handlers.sdf.shader_graph_precision import precise_operation
lazy from file_handlers.sdf.shader_graph_resource import resource_expression, resource_helpers, resource_properties
lazy from file_handlers.sdf.shader_graph_texture import TextureOperation
lazy from file_handlers.sdf.shader_graph_unassigned import unassigned_expression, unassigned_helpers, unassigned_type
lazy from ..pass_purpose import drawing_for
lazy from ..surface_data import MATERIAL_COORDINATES, surface_data_available, surface_data_keys, surface_data_value
lazy from ..surface_outputs import validate_output_pin
lazy from ..vertex_values import per_vertex_value, upstream as upstream_nodes
lazy from .renderer_inputs import (
    engine_input_nodes, engine_inputs_available, recovered_structured_slot, recovered_structured_word,
    renderer_field_expression, renderer_fields, renderer_resource_expression, renderer_value_expression,
)


def recovered_master_pins(
    node: ShaderGraphNode,
    _contract: ShaderContract,
) -> tuple[ShaderPin, ...]:
    result: list[ShaderPin] = []
    values = node.properties.get("input_pins", [])
    if not isinstance(values, list):
        raise ShaderGraphError("Recovered master-material inputs are invalid.")
    for value in values:
        if not isinstance(value, dict):
            raise ShaderGraphError("Recovered master-material input is invalid.")
        pin_name = str(value.get("pin", ""))
        label = str(value.get("label", "Material Input"))
        value_type = str(value.get("value_type", "float"))
        renderer_type = False
        if value.get("input_kind") == "renderer_value":
            renderer_fields(value)
            renderer_type = bool(re.fullmatch(r"(?:float|int|uint|bool)[1-4]?", value_type) or "value_layout" in value)
        if (
            not pin_name
            or len(pin_name) > 256
            or not renderer_type and value_type
            not in {
                "float",
                "float2",
                "float3",
                "float4",
                *TEXTURE_COORDINATE_TYPES,
                "texture",
            }
        ):
            raise ShaderGraphError("Recovered master-material input is invalid.")
        result.append(
            ShaderPin(
                pin_name,
                label,
                value_type,
                "input",
                bool(value.get("required", True)),
            )
        )
    result.append(
        ShaderPin(
            "material",
            "Material",
            "material",
            "output",
        )
    )
    result.extend(surface_channel_pins(node, "output"))
    return tuple(result)


def surface_channel_pins(node: ShaderGraphNode, direction: str) -> tuple[ShaderPin, ...]:
    records = node.properties.get("output_pins" if direction == "output" else "channel_pins", [])
    if not isinstance(records, list):
        raise ShaderGraphError("Recovered master-material outputs are invalid.")
    result = []
    for record in records:
        validate_output_pin(record)
        result.append(ShaderPin(str(record["pin"]), str(record.get("label", record["pin"])),
                                str(record["value_type"]), direction, False))
    return tuple(result)


def _parameter_storage_expression(
    profile,
    project,
    definition,
    contract: ShaderContract,
) -> str:
    expression = profile.material_parameter_storage_expression(project, definition, contract)
    if expression is not None:
        return str(expression)
    target_name = profile.material_parameter_name(
        definition,
        contract,
        project.source_profile,
    )
    shader_name = profile.shader_parameter_name(target_name)
    buffer = contract.buffer(profile.user_buffer_name)
    instances = None
    if buffer is None and profile.instanced_material_buffer:
        buffer = contract.structured_buffer(profile.instanced_material_buffer)
        instances = contract.resource(profile.instanced_material_buffer) if buffer is not None else None
    variable = next(
        (
            item
            for item in (buffer.variables if buffer is not None else ())
            if item.name == shader_name
        ),
        None,
    )
    if variable is None:
        folded = [
            item
            for item in (buffer.variables if buffer is not None else ())
            if item.name.casefold() == shader_name.casefold()
        ]
        variable = folded[0] if len(folded) == 1 else None
    if variable is None:
        if buffer is not None:
            raise ShaderGraphError(f"Material parameter {definition.name} has no native field.")
        return shader_value_literal(definition.value_type, definition.default)

    width = value_width(definition.value_type)
    if instances is not None:
        # The DXBC generator substitutes the native element index for the placeholder.
        index = f"REasyStructuredElementIndex_{instances.bind_point}"
        parts = [
            f"asfloat(REasyLoadStructured_{instances.bind_point}({index}, {variable.byte_offset + component * 4}u))"
            for component in range(width)
        ]
        return parts[0] if width == 1 else f"{definition.value_type}({', '.join(parts)})"
    declaration = str(variable.hlsl_declaration or "")
    declaration_match = re.fullmatch(
        r"float(?P<width>[1-4]?)",
        declaration,
        re.IGNORECASE,
    )
    if (
        declaration_match is None
        or int(variable.array_length) != 1
        or variable.matrix
        or variable.members
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable.name)
    ):
        raise ShaderGraphError(
            f"Material parameter {definition.name} has no scalar/vector "
            "semantic storage declaration."
        )
    stored_width = int(declaration_match.group("width") or "1")
    if stored_width < width:
        raise ShaderGraphError(
            f"Material parameter {definition.name} exceeds its semantic "
            "storage declaration."
        )
    parts = [
        variable.name
        if stored_width == 1
        else f"{variable.name}.{'xyzw'[component]}"
        for component in range(width)
    ]
    return parts[0] if width == 1 else f"{definition.value_type}({', '.join(parts)})"


def _engine_structured_read(operation, incoming, helpers):
    """Rebuilt shaders declare engine structured buffers as records of uint slots."""
    identifier = incoming('buffer', operation.object_type)
    index = incoming('index', 'uint')
    if not operation.dynamic_offset:
        read = recovered_structured_slot if operation.words > 1 else recovered_structured_word
        return read(identifier, operation.stride, index, operation.byte_offset), operation.value_type
    words = operation.stride // 4
    name = f"REasyEngineWord_{identifier}"
    cases = []
    for slot in range((words + 3) // 4):
        width = min(4, words - slot * 4)
        component = "[word & 3u]" if width > 1 else ""
        guard = f"(word & 3u) < {width}u ? " if width not in {1, 4} else "(word & 3u) == 0u ? " if width == 1 else ""
        value = f"{identifier}[index].slot{slot}{component}"
        cases.append(f"        case {slot}u: return {guard}{value}{' : 0u' if guard else ''};")
    helpers.setdefault(('engine_structured_word', identifier), "\n".join((
        f"uint {name}(uint index, uint byteOffset)", "{",
        "    uint word = byteOffset >> 2;",
        "    switch (word >> 2)", "    {", *cases, "        default: return 0u;", "    }", "}",
    )))
    return f"{name}({index}, {incoming('record_byte_offset', 'uint')})", 'uint'


def recovered_graph_expression(
    profile,
    project,
    contract: ShaderContract,
    program_semantics,
    source_node: ShaderGraphNode,
    source_pin: str,
    links,
    stack: set[tuple[str, str]],
    helpers: dict[str, str],
    scope: str = "material",
    compiler: ShaderExpressionCompiler | None = None,
    texture_resolver=None,
    channel_resolver=None,
) -> ShaderExpression:
    key = (source_node.node_id, source_pin)
    if compiler is not None:
        cached = compiler.cached(key, scope)
        if cached is not None:
            return cached
    if key in stack:
        raise ShaderGraphError("Recovered master-material inputs contain a cycle.")
    stack.add(key)
    try:
        node = source_node
        kind = node.kind
        if kind == 'precise' and compiler is None:
            raise ShaderGraphError('Calculation-order constraints require statement-owned expression generation.')
        dependencies: set[str] = set()

        def finish(
            value: str,
            value_type: str,
            *,
            own_dependencies: tuple[str, ...] = (),
            materialize: bool = True,
        ) -> ShaderExpression:
            result = ShaderExpression(
                value,
                value_type,
                frozenset((*dependencies, *own_dependencies)),
            )
            return (
                compiler.emit(
                    key,
                    scope,
                    result,
                    materialize=materialize,
                    precise=kind == 'precise',
                )
                if compiler is not None
                else result
            )

        def incoming(pin_name: str, expected_type: str) -> str:
            link = links.get((node.node_id, pin_name))
            if link is None:
                raise ShaderGraphError(
                    f"{node.properties.get('display_name') or kind}: "
                    f"{pin_name} is unconnected."
                )
            source = project.graph.node(link.source_node)
            if source is None:
                raise ShaderGraphError("Recovered material connection is dangling.")
            incoming_value = recovered_graph_expression(
                profile,
                project,
                contract,
                program_semantics,
                source,
                link.source_pin,
                links,
                stack,
                helpers,
                scope,
                compiler,
                texture_resolver,
                channel_resolver,
            )
            dependencies.update(incoming_value.dependencies)
            expression = incoming_value.value
            actual_type = incoming_value.value_type
            if actual_type == expected_type:
                return expression
            if actual_type in {"float", "int", "uint"} and expected_type in {"float", "int", "uint"}:
                return f"{expected_type}({expression})"
            if actual_type == "float" and expected_type.startswith("float"):
                return splat_expression(expected_type, expression)
            raise ShaderGraphError(
                f"Recovered material cannot convert {actual_type} to {expected_type}."
            )

        if kind in {'resource_index', 'descriptor_heap'}:
            helpers.setdefault(('resource', repr(node.properties)), resource_helpers(node, contract))
            return finish(resource_expression(node, source_pin, incoming), resource_properties(node)[0], materialize=False)
        if node.properties.get('precise'):
            value, domain, helper = precise_operation(node, source_pin, contract, incoming)
            helpers.setdefault(('precise', helper), helper)
            return finish(value, domain)
        if kind in {'indexed_select', 'array_read'}:
            domain, count = indexed_shape(node)
            helpers.setdefault(('indexed_value', kind, domain, count, node.properties.get('has_default', True)), indexed_helpers(node, contract))
            return finish(indexed_expression(node, source_pin, incoming), domain)
        if kind == 'unassigned_value':
            domain = unassigned_type(node)
            if compiler is not None:
                return compiler.emit(key, scope, ShaderExpression('', domain, frozenset((scope,))), initialize=False)
            helpers.setdefault(('unassigned_value', domain), unassigned_helpers(node, contract))
            return finish(unassigned_expression(node, source_pin, incoming), domain)
        if kind in {'branch', 'repeat', 'resource_dimensions'} or kind in EXECUTION_KINDS:
            if kind == 'branch':
                from ..material_branch import branch_evaluation as evaluate_region, branch_helpers as region_helpers
            elif kind == 'repeat':
                from ..material_loop import loop_evaluation as evaluate_region, loop_helpers as region_helpers
            elif kind == 'resource_dimensions':
                from file_handlers.sdf.shader_graph_dimensions import dimensions_evaluation as evaluate_region, dimensions_helpers as region_helpers
            else:
                from file_handlers.sdf.shader_graph_execution import execution_evaluation as evaluate_region, execution_helpers as region_helpers
                if compiler is None:
                    raise ShaderGraphError('Ordered operations require a scope-owned expression compiler.')
                dependencies.add(scope)
            evaluation_key = (node.node_id, "evaluation")
            result = compiler.cached(evaluation_key, scope) if compiler else None
            if result is None:
                evaluated = evaluate_region(node, incoming)
                helpers.setdefault(evaluated.value_type, region_helpers(node, contract))
                result = ShaderExpression(evaluated.value, evaluated.value_type, frozenset(dependencies))
                if compiler:
                    result = compiler.emit(evaluation_key, scope, result)
            else:
                evaluated = evaluate_region(node, lambda _name, _type: "")
            dependencies.update(result.dependencies)
            return finish(f"({result.value}).{dict(evaluated.members)[source_pin]}",
                next(pin.value_type for pin in node_pins(node, contract) if pin.name == source_pin and pin.direction == "output"),
                materialize=False)
        if kind == "re_engine_recovered_master_material":
            record = next((item for item in node.properties.get("output_pins", ()) if item.get("pin") == source_pin), None)
            if record is None or channel_resolver is None:
                raise ShaderGraphError("Only surface channels can read the master material's outputs.")
            return finish(channel_resolver(source_pin), str(record["value_type"]), materialize=False)
        if kind == "re_engine_renderer_value":
            return finish(renderer_value_expression(contract, node.properties, helpers),
                          node.properties["value_type"], materialize=False)
        if kind == "re_engine_renderer_resource":
            identifier, _resource = renderer_resource_expression(contract, node.properties)
            return finish(identifier, node.properties["value_type"], materialize=False)
        if kind == "re_engine_drawing_for":
            if not program_semantics.purpose:
                raise ShaderGraphError("Drawing For needs the pass being built.")
            return finish("true" if drawing_for(node, program_semantics.purpose) else "false", "bool", materialize=False)
        if kind == "code":
            text = code_helpers(node)
            helpers.setdefault(("code", text), text)
            value_type = next(pin.value_type for pin in node_pins(node, contract)
                              if pin.direction == "output" and pin.name == source_pin)
            return finish(code_expression(node, source_pin, incoming), value_type)
        if kind == "re_engine_surface_data":
            return finish(surface_data_value(program_semantics, contract, node.properties),
                          str(node.properties["value_type"]), materialize=False)
        if kind == "re_engine_per_vertex":
            value_type = str(node.properties.get("value_type", "float"))
            if contract.stage == "pixel":
                return finish(per_vertex_value(program_semantics, node).pixel_expression(), value_type,
                              materialize=False)
            if contract.stage != "vertex":
                raise ShaderGraphError("Computed per Vertex is read by vertex and pixel shaders only.")
            return finish(incoming("value", value_type), value_type, materialize=False)
        if kind == "re_engine_where_available":
            # A pixel shader reads what a Computed per Vertex node computes, not what the node reads.
            first = links.get((node.node_id, "value"))
            producers, per_vertex = upstream_nodes(project.graph, links, [first.source_node] if first else [],
                                                   through_per_vertex=contract.stage != "pixel")
            producers = [producer for producer in producers if producer not in per_vertex]
            upstream = [*engine_input_nodes(producer) for producer in producers]
            value_type = str(node.properties.get("value_type", "float"))
            computed = {value.source for value in program_semantics.vertex_values}
            chosen = ("value" if engine_inputs_available(contract, upstream) and surface_data_available(
                program_semantics, [*surface_data_keys(producer) for producer in producers])
                and (contract.stage != "pixel" or all(f"node:{item.node_id}" in computed for item in per_vertex))
                else "otherwise")
            return finish(incoming(chosen, value_type), value_type, materialize=False)
        if kind in {"aggregate_construct", "aggregate_extract", "aggregate_replace"}:
            fields = aggregate_fields(node.properties)
            value_type = next(pin.value_type for pin in aggregate_pins(node, contract) if pin.direction == "output")
            if kind == "aggregate_extract":
                link = links.get((node.node_id,"aggregate"))
                producer = project.graph.node(link.source_node) if link else None
                if producer is None:
                    raise ShaderGraphError("Aggregate input is unconnected")
                producer, socket, index = aggregate_projection(project.graph,producer,link.source_pin,
                    int(node.properties.get("field_index",0)),links)
                if producer.kind == "re_engine_renderer_value" and index is not None:
                    if producer.properties["value_type"] != aggregate_type(fields):
                        raise ShaderGraphError("Aggregate input changes its logical field layout")
                    field = renderer_fields(producer.properties)[index]
                    return finish(renderer_field_expression(contract,producer.properties,field),field.value_type,materialize=False)
                result = recovered_graph_expression(profile,project,contract,program_semantics,producer,socket,
                                                       links,stack,helpers,scope,compiler,texture_resolver,
                                                       channel_resolver)
                dependencies.update(result.dependencies)
                if index is None:
                    if result.value_type != value_type:
                        raise ShaderGraphError("Extracted aggregate field has the wrong type")
                    return finish(result.value,value_type,materialize=False)
                if result.value_type != aggregate_type(fields):
                    raise ShaderGraphError("Aggregate input changes its logical field layout")
                return finish(f"({result.value}).field{index}",value_type,materialize=False)
            helpers.setdefault(aggregate_type(fields), aggregate_helpers(fields))
            return finish(aggregate_expression(node, source_pin, incoming), value_type, materialize=False)
        if kind == "material_parameter":
            definition = project.parameter(
                str(node.properties.get("parameter_id", ""))
            )
            if definition is None:
                raise ShaderGraphError("Recovered material uses a deleted parameter.")
            return finish(
                _parameter_storage_expression(
                    profile,
                    project,
                    definition,
                    contract,
                ),
                definition.value_type,
                materialize=False,
            )
        if kind == "texture_coordinates":
            return finish(surface_data_value(program_semantics, contract, {
                "datum": MATERIAL_COORDINATES, "label": "Texture Coordinates", "value_type": "float2"}),
                "float2", materialize=False)
        if kind == "input":
            semantic_name = str(node.properties.get("semantic", ""))
            semantic_index = int(node.properties.get("semantic_index", 0))
            field = contract.input(semantic_name, semantic_index)
            if field is None:
                raise ShaderGraphError(
                    f"Stage input {semantic_name}{semantic_index} is unavailable."
                )
            expression = f"v{int(field.register)}"
            element = node.properties.get("element_index")
            if element is not None and (contract.stage in {"geometry", "hull", "domain"} or field.array_length > 1):
                if isinstance(element, bool) or not isinstance(element, int) or element < 0:
                    raise ShaderGraphError("Stage input element index must be a non-negative integer")
                expression += f"[{element}]"
            selected = mask_components(field.mask)
            if len(selected) < 4:
                expression += "." + "".join(selected)
            if field.component_type in {"uint32", "sint32"}:
                expression = f"as{'uint' if field.component_type == 'uint32' else 'int'}({expression})"
            return finish(
                expression,
                field.value_type,
                materialize=False,
            )
        if kind == "material_texture":
            definition = project.texture(
                str(node.properties.get("texture_id", ""))
            )
            if definition is None:
                raise ShaderGraphError("Recovered material uses a deleted texture.")

            def material_sampler() -> str:
                sampler = contract.resource(profile.material_sampler(contract))
                if sampler is None or sampler.input_type != "sampler":
                    raise ShaderGraphError(f"{definition.name} cannot find a compatible material sampler.")
                return resource_identifier(sampler)
            if source_pin == "sampler_object":
                return finish(material_sampler(), "sampler", materialize=False)
            resource_name = profile.material_texture_resource(definition, contract)
            resource = contract.resource(resource_name) if resource_name else None
            if resource is not None and resource.sampleable:
                texture = resource_identifier(resource)
            elif texture_resolver is not None:
                texture = texture_resolver(definition)
            else:
                raise ShaderGraphError(
                    f"Recovered material texture {definition.name} has no shader binding."
                )
            if source_pin == "texture_object":
                return finish(texture, definition.dimension, materialize=False)
            outputs = {
                "rgba": ("", "float4"),
                "rgb": (".rgb", "float3"),
                "r": (".r", "float"),
                "g": (".g", "float"),
                "b": (".b", "float"),
                "a": (".a", "float"),
            }
            if source_pin not in outputs:
                raise ShaderGraphError(
                    f"{definition.name} has no {source_pin!r} output."
                )
            coordinate_type = TEXTURE_COORDINATE_TYPES.get(definition.dimension, "float2")
            sample = (
                f"{texture}.Sample({material_sampler()}, "
                f"{incoming('coordinates', coordinate_type)})"
            )
            suffix, output_type = outputs[source_pin]
            if compiler is None:
                return finish(f"({sample}){suffix}", output_type)
            sample_key = (node.node_id, "__texture_sample__")
            sampled = compiler.cached(sample_key, scope)
            if sampled is None:
                sampled = compiler.emit(
                    sample_key,
                    scope,
                    ShaderExpression(
                        sample,
                        "float4",
                        frozenset(dependencies),
                    ),
                    materialize=True,
                )
            dependencies.update(sampled.dependencies)
            return finish(
                f"({sampled.value}){suffix}",
                output_type,
                materialize=False,
            )
        if kind in {'sample_texture', 'texture_load'}:
            try:
                operation = TextureOperation.from_properties(kind, node.properties)
                operation.validate_stage(contract.stage)
                sample = operation.expression(incoming)
            except ValueError as exc:
                raise ShaderGraphError(str(exc)) from exc
            link = links.get((node.node_id, 'texture'))
            producer = project.graph.node(link.source_node) if link is not None else None
            if producer is not None and producer.kind == "re_engine_renderer_resource" and not (
                    operation.gather or operation.comparison) and value_width(operation.element) < 4:
                # Shaders may declare an engine texture with more components than the node uses.
                sample = f"({sample}).{'xyzw'[:value_width(operation.element)]}"
            outputs = operation.outputs
            if source_pin not in outputs:
                raise ShaderGraphError(
                    f"Sample Texture has no {source_pin!r} output."
                )
            suffix, output_type = outputs[source_pin]
            if compiler is None:
                return finish(f"({sample}){suffix}", output_type)
            sample_key = (node.node_id, "__texture_sample__")
            sampled = compiler.cached(sample_key, scope)
            if sampled is None:
                sampled = compiler.emit(
                    sample_key,
                    scope,
                    ShaderExpression(
                        sample,
                        operation.result_type,
                        frozenset(dependencies),
                    ),
                    materialize=True,
                )
            dependencies.update(sampled.dependencies)
            return finish(
                f"({sampled.value}){suffix}",
                output_type,
                materialize=False,
            )
        if kind == "constant":
            value_type = str(node.properties.get("value_type", "float4"))
            return finish(
                shader_value_literal(
                    value_type,
                    node.properties.get(
                        "values",
                        [0.0] * value_width(value_type),
                    ),
                ),
                value_type,
                materialize=False,
            )

        value_type = str(node.properties.get("value_type", "float4"))

        if kind in TYPED_MATH_NODES:
            try:
                if context_error := math_context_error(kind, contract):
                    raise ValueError(context_error)
                if helper := typed_math_helpers(kind, node.properties):
                    helpers.setdefault(('typed_math', helper), helper)
                expression, result_type = typed_math_expression(kind, node.properties, incoming)
                return finish(expression, result_type)
            except ValueError as exc:
                raise ShaderGraphError(str(exc)) from exc

        if (math := math_node_expression(node, value_type, incoming)) is not None:
            return finish(*math)
        if kind == "select":
            condition = incoming('condition', 'bool')
            if condition in {"true", "false"}:
                return finish(incoming(condition, value_type), value_type, materialize=False)
            return finish(f"({condition} ? {incoming('true', value_type)} : {incoming('false', value_type)})", value_type)
        if kind == 'buffer_load':
            operation = BufferRead.from_properties(node.properties)
            link = links.get((node.node_id, 'buffer'))
            producer = project.graph.node(link.source_node) if link is not None else None
            if operation.stride and producer is not None and producer.kind == "re_engine_renderer_resource":
                return finish(*_engine_structured_read(operation, incoming, helpers))
            if operation.dynamic_offset:
                helpers.setdefault(('buffer_word', operation.stride), operation.helpers())
            return finish(operation.expression(incoming), 'uint')

        raise ShaderGraphError(
            f"{kind.replace('_', ' ').title()} cannot feed a recovered master-material input."
        )
    finally:
        stack.remove(key)


__all__ = [
    "recovered_graph_expression",
    "recovered_master_pins",
]
