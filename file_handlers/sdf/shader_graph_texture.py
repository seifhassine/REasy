from dataclasses import dataclass
from PySide6.QtCore import QT_TRANSLATE_NOOP

from .shader_graph_math import value_width


TEXTURE_COORDINATE_TYPES = {
    'texture1d': 'float', 'texture1darray': 'float2', 'texture2d': 'float2',
    'texture2darray': 'float3', 'texture3d': 'float3', 'texturecube': 'float3', 'texturecubearray': 'float4',
}
TEXTURE_LOAD_COORDINATES = {
    'texture1d': 'int', 'texture1darray': 'int2', 'texture2d': 'int2',
    'texture2darray': 'int3', 'texture3d': 'int3', 'texture2dms': 'int2', 'texture2dmsarray': 'int3',
}
TEXTURE_SPATIAL_WIDTHS = {'texture1d': 1, 'texture1darray': 1, 'texture2d': 2, 'texture2darray': 2,
    'texture3d': 3, 'texturecube': 3, 'texturecubearray': 3, 'texture2dms': 2, 'texture2dmsarray': 2}
TEXTURE_ELEMENT_TYPES = tuple(base + suffix for base in ('float', 'int', 'uint') for suffix in ('', '2', '3', '4')) + ('int64_t', 'uint64_t')
WRITABLE_TEXTURE_DIMENSIONS = ('texture1d', 'texture1darray', 'texture2d', 'texture2darray', 'texture3d')
TEXTURE_MODES = {'implicit': 'Automatic mip', 'level': 'Mip level', 'bias': 'Mip bias',
    'gradients': 'UV gradients', 'compare': 'Depth comparison', 'compare_level_zero': 'Depth comparison (mip 0)',
    'gather': 'Gather four texels', 'gather_compare': 'Gather four depth comparisons'}
GATHER_DIMENSIONS = frozenset({'texture2d', 'texture2darray', 'texturecube', 'texturecubearray'})
TEXTURE_TEMPLATES = (('sample_texture',QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Sample Texture'),{'dimension':'texture2d'}),
    ('texture_load',QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Read Texel'),{'dimension':'texture2d'}),
    ('sample_texture',QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Gather Four Texels'),{'dimension':'texture2d','mode':'gather'}))


def texture_coordinate_width(dimension, *, load=False):
    """HLSL Load's location also carries the mip level; this width leaves it out."""
    value_type = (TEXTURE_LOAD_COORDINATES if load else TEXTURE_COORDINATE_TYPES).get(dimension.replace('-', ''))
    return None if value_type is None else value_width(value_type)


def texture_property_update(properties, key, value):
    result = {**properties,key:value}
    if key == 'dimension' and 'ms' not in str(value):
        result.pop('sample_count',None)
    if key == 'mode':
        if value not in {'gather','gather_compare'}:
            result.pop('channel',None)
            result.pop('dynamic_offset',None)
        if value not in {'implicit','bias','gradients','compare'}:
            result.pop('minimum_lod',None)
    return result


def texture_object_type(dimension, element='float4', sample_count=0):
    if not isinstance(dimension, str) or not isinstance(element, str) or dimension not in TEXTURE_SPATIAL_WIDTHS or element not in TEXTURE_ELEMENT_TYPES:
        raise ValueError('This texture dimension or element type has no typed graph representation.')
    if type(sample_count) is not int or not 0 <= sample_count <= 32 or sample_count and 'ms' not in dimension:
        raise ValueError('Only multisample textures have a sample count (0 means runtime-sized, otherwise 1–32).')
    if sample_count:
        return f'{dimension}<{element},{sample_count}>'
    return dimension if element == 'float4' else f'{dimension}<{element}>'


TEXTURE_OBJECT_TYPES = frozenset(texture_object_type(dimension, element)
    for dimension in TEXTURE_SPATIAL_WIDTHS for element in TEXTURE_ELEMENT_TYPES) | frozenset(
        texture_object_type(dimension, element, count) for dimension in ('texture2dms', 'texture2dmsarray')
        for element in TEXTURE_ELEMENT_TYPES for count in range(1, 33))
WRITABLE_TEXTURE_OBJECT_TYPES = frozenset('rw_' + texture_object_type(dimension, element)
    for dimension in WRITABLE_TEXTURE_DIMENSIONS for element in TEXTURE_ELEMENT_TYPES)


def texture_object_parts(value_type):
    if value_type not in TEXTURE_OBJECT_TYPES:
        raise ValueError('Not a typed texture object.')
    dimension, separator, element = value_type.partition('<')
    return dimension, element[:-1].split(',')[0] if separator else 'float4'


def texture_object_sample_count(value_type):
    texture_object_parts(value_type)
    return int(value_type.rsplit(',', 1)[1][:-1]) if ',' in value_type else 0


def texture_result_pins(value_type):
    base = value_type.rstrip('234')
    width = value_width(value_type)
    result = {}
    for count, name in ((4, 'rgba'), (3, 'rgb'), (2, 'rg')):
        if width >= count:
            result[name] = ('' if width == count else '.' + name, base + str(count))
    for name in 'rgba'[:width]:
        result[name] = ('' if width == 1 else '.' + name, base)
    return result


@dataclass(frozen=True)
class TextureOperation:
    kind: str
    dimension: str
    element: str
    mode: str
    offset: tuple[int, ...]
    minimum_lod: bool
    sample_count: int = 0
    channel: int = 0
    dynamic_offset: bool = False

    @classmethod
    def from_properties(cls, kind, properties):
        dimension = properties.get('dimension', 'texture2d')
        element = properties.get('element_type', 'float4')
        sample_count = properties.get('sample_count', 0)
        texture_object_type(dimension, element, sample_count)
        mode = properties.get('mode', 'implicit')
        if not isinstance(mode, str):
            raise ValueError('Sampling mode must name a supported texture operation.')
        gather = mode in {'gather', 'gather_compare'}
        channel = properties.get('channel', 0)
        dynamic_offset = properties.get('dynamic_offset', False)
        if type(channel) is not int or not 0 <= channel < 4 or channel and not gather:
            raise ValueError('A gather channel must be red, green, blue or alpha.')
        if type(dynamic_offset) is not bool or dynamic_offset and (not gather or 'cube' in dimension):
            raise ValueError('Programmable texel offsets require a 2D texture gather.')
        minimum_lod = properties.get('minimum_lod', False)
        if type(minimum_lod) is not bool:
            raise ValueError('Minimum mip clamp must be enabled or disabled.')
        offset = properties.get('offset', ())
        if not isinstance(offset, (tuple, list)) or any(type(v) is not int for v in offset):
            raise ValueError('Texel offsets must be constant integers.')
        spatial = TEXTURE_SPATIAL_WIDTHS[dimension]
        if offset and (len(offset) != spatial or 'cube' in dimension):
            raise ValueError('Texel offset dimensions do not match this texture.')
        lower, upper = (-32, 31) if gather else (-8, 7)
        if any(not lower <= v <= upper for v in offset) or offset and dynamic_offset:
            raise ValueError(f'Texel offsets must be between {lower} and {upper}, with only one offset source.')
        if kind == 'texture_load':
            if dimension not in TEXTURE_LOAD_COORDINATES or mode != 'implicit' or minimum_lod:
                raise ValueError('Texel reads use integer coordinates, not filtered sampling settings.')
        elif kind == 'sample_texture':
            if dimension not in TEXTURE_COORDINATE_TYPES or (not gather and not element.startswith('float')) or mode not in TEXTURE_MODES:
                raise ValueError('Filtered sampling requires a float texture and a supported sampling mode.')
            if gather and (dimension not in GATHER_DIMENSIONS or element in {'int64_t','uint64_t'}):
                raise ValueError('Gather requires a 2D or cube texture with 32-bit texel components.')
            if mode == 'gather_compare' and not element.startswith('float'):
                raise ValueError('Gather comparisons require a floating-point texture.')
            if mode.startswith('compare') and (not element.startswith('float') or dimension == 'texture3d'):
                raise ValueError('Depth comparison requires a floating-point depth texture (not 3D).')
            if minimum_lod and mode not in {'implicit', 'bias', 'gradients', 'compare'}:
                raise ValueError('A minimum mip clamp is not part of this sampling operation.')
        else:
            raise ValueError('Unknown texture operation.')
        return cls(kind, dimension, element, mode, tuple(offset), minimum_lod, sample_count, channel, dynamic_offset)

    @property
    def comparison(self):
        return self.mode.startswith('compare') or self.mode == 'gather_compare'

    @property
    def gather(self):
        return self.mode in {'gather', 'gather_compare'}

    @property
    def inputs(self):
        ports = [('texture', texture_object_type(self.dimension, self.element, self.sample_count))]
        if self.kind == 'texture_load':
            ports.append(('coordinates', TEXTURE_LOAD_COORDINATES[self.dimension]))
            ports.append(('sample_index' if 'ms' in self.dimension else 'mip_level', 'int'))
        else:
            ports.extend((('sampler', 'comparison_sampler' if self.comparison else 'sampler'),
                          ('coordinates', TEXTURE_COORDINATE_TYPES[self.dimension])))
            if self.mode == 'level': ports.append(('mip_level', 'float'))
            if self.mode == 'bias': ports.append(('mip_bias', 'float'))
            if self.mode == 'gradients':
                gradient = 'float' + (str(TEXTURE_SPATIAL_WIDTHS[self.dimension]) if TEXTURE_SPATIAL_WIDTHS[self.dimension] > 1 else '')
                ports.extend((('gradient_x', gradient), ('gradient_y', gradient)))
            if self.comparison: ports.append(('comparison', 'float'))
            if self.minimum_lod: ports.append(('minimum_lod', 'float'))
            if self.dynamic_offset: ports.append(('texel_offset', 'int2'))
        return tuple(ports)

    @property
    def result_type(self):
        if self.gather:
            return ('float' if self.comparison else self.element.rstrip('234')) + '4'
        return 'float' if self.comparison else self.element

    @property
    def outputs(self):
        return texture_result_pins(self.result_type)

    @property
    def result_pin(self):
        return next(name for name, (suffix, _) in self.outputs.items() if not suffix)

    def expression(self, incoming):
        values = {name: incoming(name, value_type) for name, value_type in self.inputs}
        coordinate = values['coordinates']
        if self.kind == 'texture_load':
            method = 'Load'
            if 'ms' in self.dimension:
                args = [coordinate, values['sample_index']]
            else:
                coordinate_type = TEXTURE_LOAD_COORDINATES[self.dimension]
                width = value_width(coordinate_type)
                args = [f"int{width+1}({coordinate}, {values['mip_level']})"]
        else:
            method = ('Gather' + ('Cmp' if self.comparison else '') + ('Red','Green','Blue','Alpha')[self.channel]) if self.gather else {
                'implicit': 'Sample', 'level': 'SampleLevel', 'bias': 'SampleBias',
                'gradients': 'SampleGrad', 'compare': 'SampleCmp', 'compare_level_zero': 'SampleCmpLevelZero'}[self.mode]
            args = [values['sampler'], coordinate]
            args.extend(values[name] for name in ('mip_level', 'mip_bias', 'gradient_x', 'gradient_y', 'comparison') if name in values)
        # HLSL clamp overloads have a preceding offset argument for non-cubes.
        offset = self.offset or ((0,) * TEXTURE_SPATIAL_WIDTHS[self.dimension] if self.minimum_lod and 'cube' not in self.dimension else ())
        if offset:
            args.append(str(offset[0]) if len(offset) == 1 else f"int{len(offset)}({', '.join(map(str, offset))})")
        if self.dynamic_offset:
            args.append(values['texel_offset'])
        if self.minimum_lod:
            args.append(values['minimum_lod'])
        return f"{values['texture']}.{method}({', '.join(args)})"

    def validate_stage(self, stage):
        if self.kind == 'sample_texture' and self.mode in {'implicit', 'bias', 'compare'} and stage != 'pixel':
            raise ValueError('Automatic mip sampling needs pixel derivatives; use a mip level or UV gradients in this stage.')
