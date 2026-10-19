from .shader_graph_math import NUMERIC_VALUE_TYPES
from .shader_graph import (
    ShaderGraphError, ShaderGraphNodeExtension, ShaderPin,
    register_shader_graph_node_extension,
)
from PySide6.QtCore import QT_TRANSLATE_NOOP


MAX_INDEXED_VALUES = 4096


def indexed_shape(node):
    domain, count = node.properties.get('value_type', 'float'), node.properties.get('count', 4)
    if domain not in NUMERIC_VALUE_TYPES or type(count) is not int or not 1 <= count <= MAX_INDEXED_VALUES:
        raise ShaderGraphError('Indexed selection needs a numeric type and a positive bounded choice count.')
    if node.kind == 'indexed_select' and type(node.properties.get('has_default', True)) is not bool:
        raise ShaderGraphError('The default-value policy must be enabled or disabled.')
    return domain, count


def indexed_pins(node, _contract):
    domain, count = indexed_shape(node)
    return (ShaderPin('index', 'Index', 'uint', 'input', True),
            *(ShaderPin(f'item{i}', f'Value {i}', domain, 'input', True) for i in range(count)),
            *((ShaderPin('fallback', 'Out of range', domain, 'input', True),) if node.kind == 'indexed_select' and node.properties.get('has_default', True) else ()),
            ShaderPin('value', 'Selected value', domain, 'output'))


def _name(node):
    domain, count = indexed_shape(node)
    prefix = ('REasyIndexed' if node.properties.get('has_default', True) else 'REasyCases') if node.kind == 'indexed_select' else 'REasyArray'
    return f'{prefix}_{domain}_{count}'


def indexed_helpers(node, _contract):
    domain, count = indexed_shape(node)
    name = _name(node)
    parameters = ', '.join(f'{domain} item{i}' for i in range(count))
    values = ', '.join(f'item{i}' for i in range(count))
    if node.kind == 'indexed_select' and not node.properties.get('has_default', True):
        return '\n'.join((f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED',
            f'{domain} {name}(uint index, {parameters}) {{', f'    {domain} REasyUnassignedCaseResult;',
            '    switch (index) {', *(f'    case {i}u: REasyUnassignedCaseResult = item{i}; break;' for i in range(count)),
            '    } return REasyUnassignedCaseResult;', '}', '#endif'))
    if node.kind == 'indexed_select':
        return '\n'.join((f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED',
            f'{domain} {name}(uint index, {parameters}, {domain} fallback) {{',
            '    switch (index) {', *(f'    case {i}u: return item{i};' for i in range(count)),
            '    default: return fallback;', '    }', '}', '#endif'))
    return '\n'.join((f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED',
        f'{domain} {name}(uint index, {parameters}) {{',
        f'    {domain} items[{count}] = {{ {values} }};',
        '    return items[index];', '}', '#endif'))


def indexed_expression(node, pin, incoming):
    domain, count = indexed_shape(node)
    if pin != 'value':
        raise ShaderGraphError('Indexed selection has only one value output.')
    values = [incoming('index', 'uint'), *(incoming(f'item{i}', domain) for i in range(count))]
    if node.kind == 'indexed_select' and node.properties.get('has_default', True):
        values.append(incoming('fallback', domain))
    return f"{_name(node)}({', '.join(values)})"


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind='indexed_select', title='Select by Index', pins_provider=indexed_pins,
    expression=indexed_expression, hlsl_helpers_provider=indexed_helpers,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Choose a value by its zero-based index. All inputs are evaluated. Enable a default to define out-of-range results; otherwise those results are unspecified.'),
))

register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind='array_read', title='Array Element', pins_provider=indexed_pins,
    expression=indexed_expression, hlsl_helpers_provider=indexed_helpers,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Read one element of a value array. The index must be within its declared extent; no clamping or fallback is applied.'),
))


_TABLE_READS = {'float': 'asfloat', 'int': 'asint', 'uint': ''}


def table_shape(node):
    domain, words = node.properties.get('value_type'), node.properties.get('words')
    words = None if isinstance(words, (str, bytes, dict)) or not hasattr(words, '__iter__') else list(words)
    if (domain not in _TABLE_READS or words is None or not 1 <= len(words) <= MAX_INDEXED_VALUES
            or any(type(word) is not int or not 0 <= word <= 0xFFFFFFFF for word in words)
            or type(node.properties.get('has_default', True)) is not bool):
        raise ShaderGraphError('A constant table needs a 32-bit numeric type and its exact element words.')
    return domain, words, node.properties.get('has_default', True)


def table_pins(node, _contract):
    domain, _words, has_default = table_shape(node)
    return (ShaderPin('index', 'Index', 'uint', 'input', True),
            *((ShaderPin('fallback', 'Out of range', domain, 'input', True),) if has_default else ()),
            ShaderPin('value', 'Selected value', domain, 'output'))


def _table_name(node):
    import hashlib
    domain, words, has_default = table_shape(node)
    identity = f"{domain}:{int(has_default)}:{','.join(map(str, words))}".encode('ascii')
    return f'REasyTable_{hashlib.sha256(identity).hexdigest()[:20]}'


def table_helpers(node, _contract):
    domain, words, has_default = table_shape(node)
    name = _table_name(node)
    read = _TABLE_READS[domain]
    element = f'{read}({name}Words[index])' if read else f'{name}Words[index]'
    rows = [', '.join(f'0x{word:08x}u' for word in words[i:i + 8]) for i in range(0, len(words), 8)]
    body = (f'    return index < {len(words)}u ? {element} : fallback;' if has_default else f'    return {element};')
    return '\n'.join((f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED',
        f'static const uint {name}Words[{len(words)}] = {{', *(f'    {row},' for row in rows), '};',
        f"{domain} {name}(uint index{f', {domain} fallback' if has_default else ''}) {{", body, '}', '#endif'))


def table_expression(node, pin, incoming):
    _domain, _words, has_default = table_shape(node)
    if pin != 'value':
        raise ShaderGraphError('A constant table has only one value output.')
    arguments = [incoming('index', 'uint'), *((incoming('fallback', node.properties['value_type']),) if has_default else ())]
    return f"{_table_name(node)}({', '.join(arguments)})"


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind='constant_table', title='Constant Table', pins_provider=table_pins,
    expression=table_expression, hlsl_helpers_provider=table_helpers,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Read a fixed table of values by index; the words are stored exactly. With a default, an index '
                'past the table selects it; without one, the index must be within the table.'),
))
