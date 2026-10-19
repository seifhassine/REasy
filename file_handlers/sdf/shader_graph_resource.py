import hashlib
import re

from .shader_graph_texture import TEXTURE_OBJECT_TYPES, WRITABLE_TEXTURE_OBJECT_TYPES, texture_object_parts, texture_object_sample_count
from .shader_graph_buffer import structured_buffer_stride, structured_storage, structured_buffer_type, structured_word_bits
from PySide6.QtCore import QT_TRANSLATE_NOOP


RESOURCE_TEMPLATES = (('resource_index', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Descriptor Table Element'), {'element_type': 'texture2d', 'count': 0, 'non_uniform': False}),
                      ('descriptor_heap', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Descriptor Heap Resource'), {'element_type': 'texture2d', 'non_uniform': False}),
                      ('resource_dimensions', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Resource Dimensions'), {'resource_type': 'texture2d'}),
                      ('constant_buffer_load',QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Read Constant Buffer Word'),{'byte_size':16}))


def is_resource_type(value_type):
    from .shader_graph_constant_buffer import constant_buffer_size
    return value_type in TEXTURE_OBJECT_TYPES or value_type in WRITABLE_TEXTURE_OBJECT_TYPES or value_type in {'sampler', 'comparison_sampler', 'byte_address_buffer', 'rw_byte_address_buffer'} or structured_buffer_stride(value_type) is not None or constant_buffer_size(value_type) is not None or coherent_resource_element(value_type) is not None


def _writable_element(element):
    return (element in WRITABLE_TEXTURE_OBJECT_TYPES or element == 'rw_byte_address_buffer'
            or element.startswith('rw_') and structured_buffer_stride(element) is not None)


def is_writable_resource_type(value_type):
    return _writable_element(coherent_resource_element(value_type) or value_type)


def coherent_resource_element(value_type):
    if not str(value_type).startswith('globally_coherent<'):
        return None
    element = value_type[len('globally_coherent<'):-1]
    if not value_type.endswith('>') or not _writable_element(element):
        raise ValueError('Global coherence requires a supported writable resource type.')
    return element


def resource_coherence(value_type, enabled):
    if type(enabled) is not bool:
        raise ValueError('Global resource coherence must be enabled or disabled.')
    if not enabled:
        return value_type
    result = f'globally_coherent<{value_type}>'
    coherent_resource_element(result)
    return result


def resource_array_type(element, count):
    if not is_resource_type(element) or type(count) is not int or not 0 <= count <= 1_048_576:
        raise ValueError('A descriptor table needs a supported element type and a declared extent (0 means unbounded).')
    return f'resource_array<{element},{count}>'


def resource_array_parts(value_type):
    if not str(value_type).startswith('resource_array<'):
        return None
    match = re.fullmatch(r'resource_array<(.+),(\d+)>', value_type)
    if match is None or resource_array_type(match[1], int(match[2])) != value_type:
        raise ValueError('Invalid descriptor-table type.')
    return match[1], int(match[2])


def resource_value_type(resource, *, globally_coherent=False, word_bits=32):
    scalar = ('comparison_sampler' if resource.flags & 2 else 'sampler') if resource.input_type == 'sampler' else resource.object_type
    if word_bits != 32:
        if resource.input_type not in {'structured-buffer', 'rw-structured-buffer'}:
            raise ValueError('A structured word layout requires a structured resource.')
        scalar = structured_buffer_type(resource.sample_count, writable=resource.input_type == 'rw-structured-buffer', word_bits=word_bits)
    if not is_resource_type(scalar):
        raise ValueError('This renderer resource needs a different typed adapter.')
    scalar = resource_coherence(scalar, globally_coherent)
    return scalar if resource.bind_count == 1 else resource_array_type(scalar, 0 if resource.bind_count == 0xffffffff else resource.bind_count)


def object_declaration(value_type, name):
    array = resource_array_parts(value_type)
    if array:
        element, count = array
        return object_declaration(element, name + f'[{count or ""}]')
    coherent = coherent_resource_element(value_type)
    if coherent is not None:
        return 'globallycoherent ' + object_declaration(coherent, name)
    from .shader_graph import texture_hlsl_type
    from .shader_graph_constant_buffer import constant_buffer_size, constant_storage
    if value_type in {'sampler', 'comparison_sampler', 'byte_address_buffer', 'rw_byte_address_buffer', 'execution'}:
        spelling = {'sampler': 'SamplerState', 'comparison_sampler': 'SamplerComparisonState', 'byte_address_buffer': 'ByteAddressBuffer',
                    'rw_byte_address_buffer': 'RWByteAddressBuffer', 'execution': 'execution'}[value_type]
    elif (stride := structured_buffer_stride(value_type)) is not None:
        spelling = f'{"RW" if value_type.startswith("rw_") else ""}StructuredBuffer<{structured_storage(stride, word_bits=structured_word_bits(value_type))[0]}>'
    elif (size := constant_buffer_size(value_type)) is not None:
        spelling = f'ConstantBuffer<{constant_storage(size)[0]}>'
    else:
        writable = value_type in WRITABLE_TEXTURE_OBJECT_TYPES
        dimension, element = texture_object_parts(value_type[3:] if writable else value_type)
        width = int(element[-1]) if element[-1].isdigit() else 1
        spelling = texture_hlsl_type(dimension, element.rstrip('1234'), width)
        spelling = ('RW' if writable else '') + spelling
        count = texture_object_sample_count(value_type[3:] if writable else value_type)
        if count:
            spelling = spelling[:-1] + f', {count}>'
    return spelling + (' ' + name if name else '')


def resource_type_declarations(value_type):
    from .shader_graph_constant_buffer import constant_buffer_size, constant_storage
    element = (resource_array_parts(value_type) or (value_type,))[0]
    element = coherent_resource_element(element) or element
    stride = structured_buffer_stride(element)
    size = constant_buffer_size(element)
    return (structured_storage(stride, word_bits=structured_word_bits(element))[1],) if stride is not None else (constant_storage(size)[1],) if size is not None else ()


def resource_properties(node):
    properties = node.properties
    element = properties.get('element_type', 'texture2d')
    if not is_resource_type(element) or type(properties.get('non_uniform', False)) is not bool:
        raise ValueError('Descriptor selection needs a valid object type and a boolean nonuniform policy.')
    array_type = resource_array_type(element, properties.get('count', 0)) if node.kind == 'resource_index' else None
    return element, array_type


def resource_pins(node, contract):
    from .shader_graph import ShaderPin
    element, array = resource_properties(node)
    return ((*((ShaderPin('table', 'Descriptor Table', array, 'input', True),) if array else ()),
             ShaderPin('index', 'Descriptor Index', 'uint', 'input', True),
             ShaderPin('value', 'Resource', element, 'output')))


def _heap_name(node):
    element, _ = resource_properties(node)
    return 'REasyHeap_' + hashlib.sha256((element + str(node.properties.get('non_uniform', False))).encode()).hexdigest()[:20]


def resource_expression(node, _pin, incoming):
    _, array = resource_properties(node)
    index = incoming('index', 'uint')
    if array:
        if node.properties.get('non_uniform', False):
            index = f'NonUniformResourceIndex({index})'
        return f"{incoming('table', array)}[{index}]"
    return f'{_heap_name(node)}({index})'


def resource_helpers(node, contract):
    from .shader_graph import ShaderGraphError
    element, array = resource_properties(node)
    if array:
        legacy = (contract.model_major, contract.model_minor) < (5, 1)
        if contract.model_major < 5 or legacy and (not node.properties.get('count') or node.properties.get('non_uniform', False)):
            raise ShaderGraphError('Descriptor-table indexing requires Shader Model 5.1 or later.')
        # SM5.0 allows statically selected elements of a descriptor array, not dynamic indices.
        return ''
    if (contract.model_major, contract.model_minor) < (6, 6):
        raise ShaderGraphError('Direct descriptor heaps require Shader Model 6.6 or later.')
    index = 'NonUniformResourceIndex(index)' if node.properties.get('non_uniform', False) else 'index'
    heap = 'SamplerDescriptorHeap' if element in {'sampler', 'comparison_sampler'} else 'ResourceDescriptorHeap'
    name = _heap_name(node)
    # DXC requires a typed object assignment for heap descriptors.
    declarations = resource_type_declarations(element)
    return '\n'.join((f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED', *declarations,
        f'{object_declaration(element, name)}(uint index) {{',
        f'    {object_declaration(element, "resource")} = {heap}[{index}];',
        '    return resource;', '}', '#endif'))


from .shader_graph import ShaderGraphNodeExtension, register_shader_graph_node_extension

for _kind, _title in (('resource_index', 'Descriptor Table Element'), ('descriptor_heap', 'Descriptor Heap Resource')):
    register_shader_graph_node_extension(ShaderGraphNodeExtension(
        kind=_kind, title=_title, pins_provider=resource_pins,
        expression=resource_expression, hlsl_helpers_provider=resource_helpers,
        description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Select a typed resource using an editable descriptor index.'),
    ))
