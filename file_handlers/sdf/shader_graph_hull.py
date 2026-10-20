from .shader_graph import (GeneratedShaderSource, ShaderGraphError, ShaderGraphNodeExtension, ShaderPin,
    register_shader_graph_node_extension, _graph_numeric_features, _identifier, generate_graph_function,
    signature_declaration, signature_member, validate_graph)
lazy from .dxil_structured_views import structured_view_spaces
lazy from .shader_stage_execution import patch_field_members, patch_struct
from PySide6.QtCore import QT_TRANSLATE_NOOP


def patch_output_pin(field):
    return 'patch_' + field.pin_name


def output_control_point_pins(node, contract):
    key = str(node.properties.get('semantic', '')).casefold(), node.properties.get('semantic_index', 0)
    field = next((field for field in contract.outputs if field.key == key), None) if contract else None
    if contract is None or contract.stage != 'hull' or contract.primitive_execution is None or field is None:
        raise ShaderGraphError('Output Control Point requires a declared hull output attribute.')
    return (ShaderPin('index', 'Control Point Index', 'uint', 'input', True),
            ShaderPin('value', field.display_name, field.value_type, 'output'))


def hull_templates(contract):
    if contract is None or contract.stage != 'hull':
        return ()
    return tuple(('output_control_point', 'Output Control Point: ' + field.display_name,
        dict(semantic=field.semantic_name, semantic_index=field.semantic_index)) for field in contract.outputs)


def _signature_structure(name, fields, prefix):
    lines = [f'struct {name}', '{']
    for field in fields:
        if field.register == 0xffffffff:
            continue
        lines.append(signature_declaration(field, signature_member(prefix, field)))
    return (*lines, '};', '')


def generate_hull_source(graph, contract):
    from .shader_graph import MAX_SOURCE_LENGTH
    errors = [item.message for item in validate_graph(graph, contract) if item.severity == 'error']
    if errors:
        raise ShaderGraphError('\n'.join(errors))
    execution = contract.primitive_execution
    if execution is None:
        raise ShaderGraphError('Hull behavior requires its tessellator declaration.')
    execution.validate_stage('hull')
    output = next(node for node in graph.nodes if node.kind == 'output')
    links = {(link.target_node, link.target_pin): link for link in graph.links}
    phases = []
    for patch in (False, True):
        fields = contract.patch_constants if patch else contract.outputs
        members = {field.key: member for field, _, _, member in patch_field_members(fields)} if patch else {
            field.key: signature_member('out', field) for field in fields}
        result_type = 'REasyPatchConstants' if patch else 'REasyStageOutput'
        def resolve(node, pin, incoming):
            if patch and node.kind == 'invocation_input' and node.properties['meaning'] == 'output_control_point_id':
                raise ShaderGraphError('Output Control Point ID is per-control-point, not a per-patch input.')
            if not patch and node.kind == 'output_control_point':
                raise ShaderGraphError('Output control points are available only after the control-point phase.')
            return None
        def body(expression, incoming):
            lines = [f'    {result_type} result;']
            execution_pin = 'patch_execution' if patch else 'execution'
            if (output.node_id, execution_pin) in links:
                incoming(output, execution_pin, 'execution')
            for field in fields:
                pin = patch_output_pin(field) if patch else field.pin_name
                lines.append(f'    result.{members[field.key]} = {incoming(output, pin, field.value_type)};')
            return [*lines, '    return result;']
        generated = generate_graph_function(graph, contract, body, value_resolver=resolve,
            memoize_values=True, variable_prefix='reasyPatch' if patch else 'reasyControlPoint', entry=True)
        phases.append(generated)
    declarations = list(dict.fromkeys(block for phase in phases for block in phase.declarations))
    declarations.extend(_signature_structure('REasyStageInput', contract.inputs, 'in'))
    declarations.extend(_signature_structure('REasyStageOutput', contract.outputs, 'out'))
    declarations.extend(patch_struct(contract.patch_constants))
    for patch, phase in ((True, phases[1]), (False, phases[0])):
        parameters = [f'const InputPatch<REasyStageInput, {execution.input_control_points}> input']
        if patch:
            parameters.append(f'const OutputPatch<REasyStageOutput, {execution.output_control_points}> control_points')
        parameters.extend(f'{domain} {name} : {semantic}' for name, domain, semantic in phase.entry_parameters)
        if not patch:
            declarations.extend((f'[domain("{execution.domain}")]', f'[partitioning("{execution.partitioning}")]',
                f'[outputtopology("{execution.tessellation_primitive}")]', f'[outputcontrolpoints({execution.output_control_points})]',
                '[patchconstantfunc("REasyPatchMain")]',
                # FXC turns a declared maximum into min(max, factor) on every factor.
                *((f'[maxtessfactor({execution.max_tessellation_factor})]',) if execution.max_tessellation_factor is not None else ())))
        declarations.extend((f'{"REasyPatchConstants REasyPatchMain" if patch else "REasyStageOutput main"}({", ".join(parameters)})',
                             '{', *phase.body, '}', ''))
    source = '\n'.join(declarations)
    if len(source.encode('utf-8')) > MAX_SOURCE_LENGTH:
        raise ShaderGraphError('Generated hull HLSL exceeds the source size budget.')
    half, unassigned, signed_zero, word_views = _graph_numeric_features(graph)
    return GeneratedShaderSource(source, 'main', contract.target,
        native_16bit=half, unassigned_values=unassigned, signed_zero_constants=signed_zero,
        structured_view_spaces=structured_view_spaces(contract) if word_views else ())


register_shader_graph_node_extension(ShaderGraphNodeExtension(kind='output_control_point', title='Output Control Point',
    pins_provider=output_control_point_pins,
    expression=lambda node, pin, incoming: f"control_points[{incoming('index', 'uint')}]." +
        _identifier(f"out_{node.properties['semantic']}_{node.properties.get('semantic_index', 0)}"),
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'An already computed control-point attribute, available to per-patch calculations.')))
