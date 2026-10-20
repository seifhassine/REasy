from dataclasses import dataclass
import hashlib

from .shader_graph import ShaderGraphError, ShaderGraphNodeExtension, ShaderNodeEvaluation, ShaderPin, register_shader_graph_node_extension
from .shader_graph_buffer import structured_buffer_stride
from .shader_graph_resource import coherent_resource_element, object_declaration, resource_type_declarations
from .shader_graph_texture import TEXTURE_OBJECT_TYPES, WRITABLE_TEXTURE_OBJECT_TYPES, texture_object_parts
from PySide6.QtCore import QT_TRANSLATE_NOOP


@dataclass(frozen=True)
class ResourceDimensions:
    resource_type: str
    outputs: tuple[str, ...]
    mipmapped: bool
    stride: int | None = None

    @classmethod
    def from_type(cls, resource_type):
        scalar = coherent_resource_element(resource_type) or resource_type
        stride = structured_buffer_stride(scalar)
        if stride is not None:
            return cls(resource_type, ('elements', 'stride'), False, stride)
        if scalar in {'byte_address_buffer', 'rw_byte_address_buffer'}:
            return cls(resource_type, ('bytes',), False)
        writable = scalar in WRITABLE_TEXTURE_OBJECT_TYPES
        if not writable and scalar not in TEXTURE_OBJECT_TYPES:
            raise ShaderGraphError('Dimensions require a single texture or buffer, not a sampler or descriptor table.')
        dimension, _ = texture_object_parts(scalar[3:] if writable else scalar)
        outputs = ['width']
        if not dimension.startswith('texture1d'):
            outputs.append('height')
        if dimension == 'texture3d':
            outputs.append('depth')
        elif dimension.endswith('array'):
            outputs.append('cubes' if 'cube' in dimension else 'layers')
        mipmapped = not writable and 'ms' not in dimension
        if mipmapped:
            outputs.append('mips')
        elif 'ms' in dimension:
            outputs.append('samples')
        return cls(resource_type, tuple(outputs), mipmapped)

    @property
    def native_lanes(self):
        spatial = tuple(name for name in self.outputs if name not in {'mips', 'samples', 'stride'})
        return (*spatial, *((None,) * (3-len(spatial))), next((n for n in ('mips','samples') if n in self.outputs), None))


def dimensions_shape(node):
    return ResourceDimensions.from_type(node.properties.get('resource_type', 'texture2d'))


def dimensions_pins(node, _contract):
    shape = dimensions_shape(node)
    return (ShaderPin('resource', 'Resource', shape.resource_type, 'input', True),
            *((ShaderPin('mip', 'Mip level', 'uint', 'input', True),) if shape.mipmapped else ()),
            *(ShaderPin(name, {'mips':'Mip levels', 'stride':'Record stride (bytes)', 'bytes':'Size (bytes)'}.get(name, name.title()), 'uint', 'output') for name in shape.outputs))


def _name(node):
    return 'REasyDimensions_' + hashlib.sha256(dimensions_shape(node).resource_type.encode()).hexdigest()[:20]


def dimensions_evaluation(node, incoming):
    shape, name = dimensions_shape(node), _name(node)
    arguments = [incoming('resource', shape.resource_type)]
    if shape.mipmapped:
        arguments.append(incoming('mip', 'uint'))
    return ShaderNodeEvaluation(f"{name}({', '.join(arguments)})", name + '_Result', tuple((pin, pin) for pin in shape.outputs))


def dimensions_helpers(node, _contract):
    shape, name = dimensions_shape(node), _name(node)
    parameters = object_declaration(shape.resource_type, 'resource') + (', uint mip' if shape.mipmapped else '')
    arguments = (['mip'] if shape.mipmapped else []) + ['result.' + pin for pin in shape.outputs]
    return '\n'.join((f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED',
        *resource_type_declarations(shape.resource_type),
        f'struct {name}_Result {{ ' + ' '.join('uint ' + pin + ';' for pin in shape.outputs) + ' };',
        f'{name}_Result {name}({parameters}) {{', f'    {name}_Result result;',
        f"    resource.GetDimensions({', '.join(arguments)});", '    return result;', '}', '#endif'))


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind='resource_dimensions', title='Resource Dimensions', pins_provider=dimensions_pins,
    evaluation=dimensions_evaluation, hlsl_helpers_provider=dimensions_helpers,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Query the bound resource at runtime. Array layers, cube count, texel dimensions and mip/sample counts have separate outputs.'),
))
