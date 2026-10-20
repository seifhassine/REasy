import hashlib
import re
from PySide6.QtCore import QT_TRANSLATE_NOOP
from utils.hex_util import align

from ..material_names import resolve_material_name
from .shader_graph import (
    ShaderGraphNodeExtension, ShaderPin, _constant_buffer_identifiers, _hlsl_variable_parts, _item_name,
    register_shader_graph_node_extension,
)


def constant_buffer_type(byte_size):
    if type(byte_size) is not int or not 4 <= byte_size <= 65536 or byte_size % 4:
        raise ValueError('Constant-buffer word storage needs a four-byte-aligned extent up to 64 KiB.')
    return f'constant_buffer<{byte_size}>'


def constant_buffer_size(value_type):
    match = re.fullmatch(r'constant_buffer<(\d+)>',str(value_type))
    if match is None:
        return None
    size = int(match[1])
    if constant_buffer_type(size) != value_type:
        raise ValueError('Invalid constant-buffer descriptor type.')
    return size


def constant_storage(byte_size):
    constant_buffer_type(byte_size)
    rows, tail = divmod(byte_size//4,4)
    name = f'REasyConstantWords{byte_size}'
    fields = ([f'    uint4 rows[{rows}];'] if rows else [])
    if tail:
        fields.append(f'    uint{tail if tail>1 else ""} tail;')
    declaration = '\n'.join((f'#ifndef {name}_DEFINED',f'#define {name}_DEFINED',f'struct {name}','{',*fields,'};','#endif'))
    return name, declaration


def _size(node):
    size = node.properties.get('byte_size')
    constant_buffer_type(size)
    return size


def constant_load_helpers(node, _contract):
    size = _size(node)
    name, declaration = constant_storage(size)
    rows, tail = divmod(size//4,4)
    lines = [declaration,f'#ifndef REasyRead{name}_DEFINED',f'#define REasyRead{name}_DEFINED',
             f'uint REasyRead{name}(ConstantBuffer<{name}> buffer, uint byteOffset) {{',
             '    uint row = byteOffset >> 4; uint component = (byteOffset >> 2) & 3u;']
    if tail:
        value = 'buffer.tail' + ('[component]' if tail>1 else '')
        lines.append(f'    {f"if (row == {rows}u) " if rows else ""}return {value};')
    if rows:
        lines.append('    return buffer.rows[row][component];')
    lines.extend(('}','#endif'))
    return '\n'.join(lines)


def constant_load_expression(node, _pin, incoming):
    size = _size(node)
    return f"REasyReadREasyConstantWords{size}({incoming('buffer',constant_buffer_type(size))}, {incoming('byte_offset','uint')})"


def row_view_size(byte_size):
    return align(int(byte_size), 16)


_VIEW_DECLARATION = re.compile(r'ConstantBuffer<REasyConstantWords\d+> REasyConstantView_s(\d+)_b(\d+) : register\(b(\d+), space(\d+)\);')


def constant_view_identifier(resource):
    return f'REasyConstantView_s{int(resource.register_space)}_b{int(resource.bind_point)}'


def constant_view_space(contract):
    from .dxil_structured_views import structured_view_spaces
    occupied = ({int(r.register_space) for r in contract.resources} | {int(b.register_space) for b in contract.constant_buffers}
                | {private for _original, private in structured_view_spaces(contract)})
    space = 0
    while space in occupied:
        space += 1
    return space


def constant_view_declaration(resource, buffer, space):
    name, declaration = constant_storage(row_view_size(buffer.byte_size))
    return declaration + '\n' + (f'ConstantBuffer<{name}> {constant_view_identifier(resource)} : '
                                  f'register(b{int(resource.bind_point)}, space{int(space)});')


def constant_view_overrides(source):
    return {(int(space), int(slot)): (int(original), int(slot))
            for original, slot, _register, space in _VIEW_DECLARATION.findall(source)}


def _row_component(node):
    component = node.properties.get('component')
    if type(component) is not int or not 0 <= component < 4:
        raise ValueError('A constant-buffer row read selects one of four components.')
    return component


def constant_row_pins(node, contract):
    size = _size(node)
    if size % 16:
        raise ValueError('A constant-buffer row view spans whole 16-byte registers.')
    return (ShaderPin('buffer', 'Constant Buffer', constant_buffer_type(size), 'input', True),
            ShaderPin('row', 'Register Row', 'uint', 'input', True), ShaderPin('value', 'Word', 'uint', 'output'))


def constant_row_helpers(node, _contract):
    return constant_storage(_size(node))[1]


def constant_row_expression(node, _pin, incoming):
    size = _size(node)
    return (f"{incoming('buffer', constant_buffer_type(size))}.rows[{incoming('row', 'uint')}]"
            f".{'xyzw'[_row_component(node)]}")


register_shader_graph_node_extension(ShaderGraphNodeExtension(kind='constant_buffer_load',title='Read Constant Buffer Word',
    pins_provider=lambda node,contract:(ShaderPin('buffer','Constant Buffer',constant_buffer_type(_size(node)),'input',True),
        ShaderPin('byte_offset','Word Byte Offset','uint','input',True),ShaderPin('value','Word','uint','output')),
    expression=constant_load_expression,hlsl_helpers_provider=constant_load_helpers,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Read a word from the descriptor-selected constant buffer. The byte address must be aligned and inside its declared extent.')))

register_shader_graph_node_extension(ShaderGraphNodeExtension(kind='constant_buffer_row', title='Read Constant Buffer Row',
    pins_provider=constant_row_pins, expression=constant_row_expression, hlsl_helpers_provider=constant_row_helpers,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Read one component of a 16-byte register row of a constant buffer, with the row chosen at run time. '
                "This is the renderer's own indexed constant read: a row outside the bound buffer reads zero.")))


def array_element_shape(buffer, variable_name):
    variable = resolve_material_name(variable_name, buffer.variables if buffer is not None else (), key=_item_name)
    if variable is None or variable.array_length < 2 or variable.byte_offset % 16:
        return None
    declaration, axes = _hlsl_variable_parts(variable)
    element = re.fullmatch(r'(float|int|uint)([1-4]?)', declaration.strip())
    if element is None or axes != f'[{int(variable.array_length)}]':
        return None
    width = int(element[2] or 1)
    if variable.byte_size != (variable.array_length - 1) * 16 + width * 4:
        return None
    return element[1], width


def _element_properties(node, contract):
    buffer = contract.buffer(str(node.properties.get('buffer', '')))
    shape = array_element_shape(buffer, str(node.properties.get('variable', '')))
    component = node.properties.get('component')
    if shape is None or type(component) is not int or not -1 <= component < shape[1]:
        raise ValueError('An array element read needs a row-per-element array parameter and one of its components.')
    return buffer, shape, component


def _element_type(scalar, width, component):
    return scalar if component >= 0 or width == 1 else f'{scalar}{width}'


def parameter_element_pins(node, contract):
    value_type = node.properties.get('value_type')
    if str(value_type).rstrip('234') not in {'float', 'int', 'uint'}:
        raise ValueError('An array element read declares its value type.')
    if contract is not None:
        _buffer, (scalar, width), component = _element_properties(node, contract)
        if _element_type(scalar, width, component) != value_type:
            raise ValueError('The array element type differs from its declared parameter.')
    return (ShaderPin('index', 'Element', 'uint', 'input', True), ShaderPin('value', 'Value', value_type, 'output'))


def parameter_element_buffers(node, contract):
    return (_element_properties(node, contract)[0].name,)


def _element_function(node):
    identity = f"{node.properties.get('buffer', '')}\0{node.properties.get('variable', '')}".encode('utf-8')
    component = int(node.properties.get('component', 0))
    return f"REasyElement_{hashlib.sha256(identity).hexdigest()[:16]}_{component if component >= 0 else 'all'}"


def parameter_element_helpers(node, contract):
    buffer, (scalar, width), component = _element_properties(node, contract)
    identifier = _constant_buffer_identifiers(contract.constant_buffers, reserved=(item.name for item in contract.resources))[
        buffer.name, str(node.properties['variable'])]
    name = _element_function(node)
    swizzle = f".{'xyzw'[component]}" if width > 1 and component >= 0 else ''
    return '\n'.join((f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED',
                      f'{_element_type(scalar, width, component)} {name}(uint index) {{ return {identifier}[index]{swizzle}; }}',
                      '#endif'))


def parameter_element_expression(node, _pin, incoming):
    return f"{_element_function(node)}({incoming('index', 'uint')})"


register_shader_graph_node_extension(ShaderGraphNodeExtension(kind='parameter_element', title='Read Array Parameter Element', 
    pins_provider=parameter_element_pins, expression=parameter_element_expression,
    hlsl_helpers_provider=parameter_element_helpers, required_buffers_provider=parameter_element_buffers,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Read one component of an array parameter element chosen at run time. An index past the array '
                'reads the registers that follow it, as the native indexed constant read does.')))
