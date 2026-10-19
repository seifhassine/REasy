from .shader_graph import ShaderGraphError, ShaderGraphNodeExtension, ShaderPin, register_shader_graph_node_extension
from PySide6.QtCore import QT_TRANSLATE_NOOP


INVOCATION_INPUTS = {
    'dispatch_thread_id': (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Dispatch Thread ID'), 'uint3', 'SV_DispatchThreadID'),
    'group_id': (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Group ID'), 'uint3', 'SV_GroupID'),
    'group_thread_id': (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Thread ID in Group'), 'uint3', 'SV_GroupThreadID'),
    'group_index': (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Flattened Thread Index'), 'uint', 'SV_GroupIndex'),
    'primitive_id': (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Primitive ID'), 'uint', 'SV_PrimitiveID'),
    'geometry_instance_id': (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Geometry Instance ID'), 'uint', 'SV_GSInstanceID'),
    'output_control_point_id': (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Output Control Point ID'), 'uint', 'SV_OutputControlPointID'),
}


def invocation_stages(meaning):
    if meaning == 'output_control_point_id':
        return ('hull',)
    if meaning=='primitive_id':
        return ('geometry','domain','hull')
    return ('geometry',) if meaning=='geometry_instance_id' else ('compute','mesh','amplification') if meaning in INVOCATION_INPUTS else ()


def invocation_shape(node):
    meaning = node.properties.get('meaning')
    if meaning not in INVOCATION_INPUTS:
        raise ShaderGraphError('Choose an identified invocation input.')
    return INVOCATION_INPUTS[meaning]


def invocation_parameters(node, contract):
    _, domain, semantic = invocation_shape(node)
    if contract.stage not in invocation_stages(node.properties['meaning']):
        raise ShaderGraphError('This invocation input belongs to a different shader stage.')
    return (('reasy_' + node.properties['meaning'], domain, semantic),)


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind='invocation_input', title='Invocation Input', 
    pins_provider=lambda node, contract: (ShaderPin('value', invocation_shape(node)[0], invocation_shape(node)[1], 'output'),),
    expression=lambda node, pin, incoming: 'reasy_' + node.properties['meaning'],
    entry_parameters_provider=invocation_parameters,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'The current dispatched thread or group identity supplied by the renderer.'),
))
