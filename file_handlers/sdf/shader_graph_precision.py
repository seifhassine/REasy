import hashlib
import json

from .shader_graph_math import TYPED_MATH_NODES, UNARY_FUNCTIONS


PRECISION_OPERATIONS = (TYPED_MATH_NODES - {'precise'}) | UNARY_FUNCTIONS.keys() | {
    'add', 'subtract', 'multiply', 'divide', 'multiply_add', 'minimum', 'maximum',
    'power', 'dot', 'compare', 'select', 'swizzle', 'compose', 'clamp', 'saturate',
    'normalize', 'lerp', 'one_minus', 'smoothstep', 'distance', 'remap', 'normal_unpack',
}


def precise_operation(node, output_pin, contract, incoming):
    from .shader_graph import ShaderGraph, ShaderGraphError, generate_graph_function, node_pins
    if node.kind not in PRECISION_OPERATIONS or type(node.properties.get('precise')) is not bool:
        raise ShaderGraphError('This node has no per-operation calculation-order constraint.')
    properties = {key:value for key,value in node.properties.items() if key != 'precise'}
    ports = node_pins(node, contract)
    inputs = tuple(pin for pin in ports if pin.direction == 'input')
    output = next(pin for pin in ports if pin.direction == 'output' and pin.name == output_pin)
    arguments, declarations, parameters = [], [], {}
    for index, pin in enumerate(inputs):
        argument = incoming(pin.name, pin.value_type)
        width = int(pin.value_type[-1]) if pin.value_type[-1:] in '234' else 1
        scalar = pin.value_type.rstrip('234')
        components = []
        for lane in range(width):
            parameter = f'argument{index}_{lane}'
            components.append(parameter)
            declarations.append(f'{scalar} {parameter}')
            arguments.append(f'({argument}).{"xyzw"[lane]}' if width > 1 else argument)
        parameters[pin.name] = f"{pin.value_type}({', '.join(components)})" if width > 1 else components[0]
    descriptor = json.dumps((node.kind, properties, output_pin), sort_keys=True, separators=(',', ':'))
    name = 'REasyPrecise_' + hashlib.sha256(descriptor.encode()).hexdigest()[:24]
    graph = ShaderGraph('material_function', '')
    operation = graph.add_node(node.kind, node_id='operation', properties=properties)
    for index, pin in enumerate(inputs):
        source = graph.add_node('function_input', properties=dict(pin_id=pin.name, value_type=pin.value_type))
        graph.connect(source.node_id,'value',operation.node_id,pin.name)
    width = int(output.value_type[-1]) if output.value_type[-1:] in '234' else 1
    scalar = output.value_type.rstrip('234')
    attributes = ('[noinline]',) if contract.model_major >= 6 else ()
    helpers, calls = [], []
    for lane in range(width):
        target, pin_name = operation, output_pin
        if width > 1:
            target = graph.add_node('swizzle', properties=dict(source_type=output.value_type, mask='xyzw'[lane]))
            graph.connect(operation.node_id,output_pin,target.node_id,'value')
            pin_name = 'result'
        generated = generate_graph_function(graph, contract,
            lambda expression, read: [f'    precise {scalar} constrained = {expression(target,pin_name)};', '    return constrained;'],
            value_resolver=lambda item, _pin, _read: parameters[item.properties['pin_id']] if item.kind == 'function_input' else None,
            memoize_values=True)
        helper_name = f'{name}_{lane}'
        helpers.extend((*generated.declarations, *attributes,
            f"{scalar} {helper_name}({', '.join(declarations)})", '{', *generated.body, '}'))
        calls.append(f"{helper_name}({', '.join(arguments)})")
    helper = '\n'.join((f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED', *helpers, '#endif'))
    expression = f"{output.value_type}({', '.join(calls)})" if width > 1 else calls[0]
    return expression, output.value_type, helper
