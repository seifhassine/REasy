from .shader_graph import (
    ShaderGraphError, ShaderGraphNodeExtension, ShaderPin,
    register_shader_graph_node_extension,
)
from .shader_graph_math import NUMERIC_VALUE_TYPES
from PySide6.QtCore import QT_TRANSLATE_NOOP


def unassigned_type(node):
    domain = node.properties.get('value_type', 'float')
    if domain not in NUMERIC_VALUE_TYPES:
        raise ShaderGraphError('An unassigned value must have an explicit numeric type.')
    return domain


def unassigned_helpers(node, contract):
    from .shader_graph import _fxc_target
    domain = unassigned_type(node)
    name = f'REasyUnassigned_{domain}'
    # FXC rejects reads of uninitialized temps, so SM5 uses zero; other backends keep undef.
    value = f' = ({domain})0' if contract is not None and _fxc_target(contract) else ''
    return '\n'.join((f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED',
        f'{domain} {name}() {{', f'    {domain} REasyUnassignedValue{value};',
        '    return REasyUnassignedValue;', '}', '#endif'))


def unassigned_expression(node, pin, _incoming):
    if pin != 'value':
        raise ShaderGraphError('An unassigned result has only one value output.')
    return f'REasyUnassigned_{unassigned_type(node)}()'


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind='unassigned_value', title='Unassigned Value', 
    pins_provider=lambda node, _contract: (ShaderPin('value', 'Unassigned', unassigned_type(node), 'output'),),
    expression=unassigned_expression, hlsl_helpers_provider=unassigned_helpers,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'No value is assigned on this path. Replace this node with a defined value to specify its result; it does not mean zero.'),
))
