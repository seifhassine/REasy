from uuid import uuid4

from file_handlers.sdf.shader_graph import ShaderGraph, ShaderGraphError, ShaderPin, ShaderNodeEvaluation, ShaderGraphNodeExtension, register_shader_graph_node_extension
from file_handlers.sdf.shader_graph_math import NUMERIC_VALUE_TYPES
from .material_function import (
    MaterialFunctionAsset, MaterialFunctionPin,
    _compiler_function_document, _read_compiler_function,
)
from .material_region_read import read_region, publish_region_properties
from .function_region import region_helpers, region_parameters, region_result
from PySide6.QtCore import QT_TRANSLATE_NOOP


def _parse_loop(properties):
    try:
        body = _read_compiler_function(properties['body'])
        inputs, outputs = {p.pin_id: p for p in body.inputs}, {p.pin_id: p for p in body.outputs}
        states = tuple((row['input'], row['output']) for row in properties['states'])
        continuation = properties['continuation']
        if continuation not in outputs or outputs[continuation].value_type != 'bool':
            raise ShaderGraphError('A repeat region needs an explicit boolean continuation result.')
        if len({source for source, _ in states}) != len(states):
            raise ShaderGraphError('A loop-carried value cannot have two updates.')
        for source, target in states:
            if (source not in inputs or target not in outputs
                    or inputs[source].value_type != outputs[target].value_type or inputs[source].value_type not in (*NUMERIC_VALUE_TYPES, 'execution')):
                raise ShaderGraphError('Loop-carried values need matching numeric input/update types.')
        if any(p.value_type not in (*NUMERIC_VALUE_TYPES, 'execution') for p in body.outputs):
            raise ShaderGraphError('Repeat results must be numeric values.')
        if any(p.default is not None for p in body.inputs):
            raise ShaderGraphError('Repeat inputs are explicit caller values; defaults belong outside the loop.')
        if len(outputs) < 2:
            raise ShaderGraphError('An effect-only loop needs explicit execution ownership, not an unused value node.')
        return body, states, continuation
    except (KeyError, TypeError, ValueError) as exc:
        raise ShaderGraphError(f'Invalid repeat region: {exc}') from exc


def loop_body(node):
    return read_region('repeat', node.properties)


def loop_properties(body, states, continuation):
    properties = publish_region_properties(dict(body=_compiler_function_document(body),
        states=[dict(input=a, output=b) for a, b in states], continuation=continuation))
    read_region('repeat', properties)
    return properties


def loop_pins(node, contract):
    body, states, continuation = loop_body(node)
    carried = {source for source, _ in states}
    return (*(ShaderPin(p.pin_id, ('Initial ' if p.pin_id in carried else '') + p.name, p.value_type, 'input', True) for p in body.inputs),
            *(ShaderPin(p.pin_id, p.name, p.value_type, 'output') for p in body.outputs if p.pin_id != continuation))


def new_loop_properties():
    graph = ShaderGraph('material_function', '')
    source = graph.add_node('function_input', properties=dict(pin_id='state', display_name='Value', value_type='float'))
    result = graph.add_node('function_output', properties=dict(pin_id='updated', display_name='Updated Value', value_type='float'))
    stop = graph.add_node('constant', properties=dict(value_type='bool', values=[False]))
    continuation = graph.add_node('function_output', properties=dict(pin_id='continue_loop', display_name='Continue', value_type='bool'))
    graph.connect(source.node_id, 'value', result.node_id, 'value')
    graph.connect(stop.node_id, 'value', continuation.node_id, 'value')
    body = MaterialFunctionAsset('repeat.' + uuid4().hex, 'Repeat Step', 'Control Flow', '',
        (MaterialFunctionPin('state', 'Value', 'float'),),
        (MaterialFunctionPin('updated', 'Updated Value', 'float', False), MaterialFunctionPin('continue_loop', 'Continue', 'bool', False)), graph)
    return loop_properties(body, [('state', 'updated')], 'continue_loop')


def _name(node):
    from .region_codegen import region_codegen_key
    return 'REasyRepeat_' + region_codegen_key(node)[:24]


def loop_evaluation(node, incoming):
    body, _, continuation = loop_body(node)
    name = _name(node)
    return ShaderNodeEvaluation(f"{name}({', '.join(incoming(p.pin_id, p.value_type) for p in body.inputs)})", name + '_Result',
        tuple((p.pin_id, f'field{i}') for i, p in enumerate(body.outputs) if p.pin_id != continuation))


def loop_helpers(node, contract):
    body, states, continuation = loop_body(node)
    name = _name(node)
    result_type = name + '_Result'
    names, parameters = region_parameters(body)
    output_fields = {pin.pin_id: f'field{i}' for i, pin in enumerate(body.outputs)}
    declarations = [region_result(body, result_type), *region_helpers(body, name + '_Step', result_type, contract)]
    declarations.extend((f'{result_type} {name}({parameters})', '{', f'    {result_type} step;', '    [loop] while (true)', '    {',
        f"        step = {name}_Step({', '.join(names.values())});",
        f'        if (!step.{output_fields[continuation]}) break;'))
    declarations.extend(f'        {names[source]} = step.{output_fields[target]};' for source, target in states)
    declarations.extend(('    }', '    return step;', '}'))
    return '\n'.join((f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED', *declarations, '#endif'))


register_shader_graph_node_extension(ShaderGraphNodeExtension(kind='repeat', title='Repeat', pins_provider=loop_pins,
    evaluation=loop_evaluation, hlsl_helpers_provider=loop_helpers,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Repeatedly evaluate editable calculations, carrying explicit state until Continue becomes false.')))
