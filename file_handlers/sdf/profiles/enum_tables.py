from collections.abc import Mapping
from functools import cached_property

lazy from utils.enum_manager import EnumDefinitionError, game_enum_definitions


class EngineEnumTable(Mapping):
    def __init__(self, game, enum_name, meanings, *, maximum):
        self.game = game
        self.enum_name = enum_name
        self.meanings = meanings
        self.maximum = maximum

    @cached_property
    def _values(self):
        label = f"{self.game} enum JSON: {self.enum_name}"
        members = game_enum_definitions(self.game, namespace="via.render.").get(self.enum_name)
        if not isinstance(members, list) or not members:
            raise EnumDefinitionError(f"{label} is missing or is not a non-empty member list")
        result, names = {}, set()
        prefix = self.enum_name.rsplit('.', 1)[-1] + '_'
        for member in members:
            if not isinstance(member, dict) or not isinstance(member.get('name'), str):
                raise EnumDefinitionError(f"{label} has an invalid member {member!r}")
            name = member['name'].removeprefix(prefix)
            value = member.get('value')
            if type(value) is not int or not 0 <= value <= self.maximum:
                raise EnumDefinitionError(f"{label}.{name} ID {value!r} does not fit its serialized field")
            if name not in self.meanings:
                raise EnumDefinitionError(f"{label}.{name} has no implemented meaning")
            meaning = self.meanings[name]
            if name in names or (value in result and result[value] != meaning):
                raise EnumDefinitionError(f"{label} has a duplicate/conflicting member {name}={value}")
            names.add(name)
            result[value] = meaning
        return frozendict(result)

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def items(self):
        return self._values.items()


def binner_control_names(game):
    return EngineEnumTable(game, 'via.render.BinnerControl', {
        'Automatic': 'Automatic', 'ForceOn': 'Force on', 'ForceOff': 'Force off',
        'Num': 'Count (not a state)',
    }, maximum=0xFFFFFFFF)


_TOPOLOGIES = {
    'Undefined': 'undefined', 'PointList': 'point-list',
    'LineList': 'line-list', 'LineStrip': 'line-strip',
    'TriangleList': 'triangle-list', 'TriangleStrip': 'triangle-strip',
    'LineListAdj': 'line-list-adjacency', 'LineStripAdj': 'line-strip-adjacency',
    'TriangleListAdj': 'triangle-list-adjacency', 'TriangleStripAdj': 'triangle-strip-adjacency',
    **{f'PatchList_ControlPoint{count}': f'patch-list-{count}' for count in range(1, 33)},
}


def topology_names(game):
    return EngineEnumTable(game, 'via.render.Topology', _TOPOLOGIES, maximum=0xFFFF)


_SYSTEM_VALUES = {
    'Undefined': 'undefined', 'Position': 'position',
    'Clip_Distance': 'clip-distance', 'Cull_Distance': 'cull-distance',
    'Render_Target_Array_Index': 'render-target-array-index',
    'Viewport_Array_Index': 'viewport-array-index',
    'Vertex_Id': 'vertex-id', 'Primitive_Id': 'primitive-id', 'Instance_Id': 'instance-id',
    'Is_Front_Face': 'front-face', 'Sample_Index': 'sample-index',
    'Final_Quad_Edge_Tessfactor': 'final-quad-edge-tessfactor',
    'Final_Quad_Inside_Tessfactor': 'final-quad-inside-tessfactor',
    'Final_Tri_Edge_Tessfactor': 'final-tri-edge-tessfactor',
    'Final_Tri_Inside_Tessfactor': 'final-tri-inside-tessfactor',
    'Final_Line_Detail_Tessfactor': 'final-line-detail-tessfactor',
    'Final_Line_Density_Tessfactor': 'final-line-density-tessfactor',
    'Target': 'target', 'Depth': 'depth', 'Coverage': 'coverage',
    'Depth_Greater_Equal': 'depth-greater-equal', 'Depth_Less_Equal': 'depth-less-equal',
}
_COMPONENT_TYPES = {'Unknown': 'unknown', 'Uint32': 'uint32', 'Sint32': 'sint32', 'Float32': 'float32'}


def system_value_names(game):
    return EngineEnumTable(game, 'via.render.SysValSemantic', _SYSTEM_VALUES, maximum=0x1F)


def component_type_names(game):
    return EngineEnumTable(game, 'via.render.RegisterComponent', _COMPONENT_TYPES, maximum=0x07)


_DXGI_BY_MEANING = {
    'Float1': (41, 'R32_FLOAT'), 'Float2': (16, 'R32G32_FLOAT'),
    'Float3': (6, 'R32G32B32_FLOAT'), 'Float4': (2, 'R32G32B32A32_FLOAT'),
    'Half2': (34, 'R16G16_FLOAT'), 'Half4': (10, 'R16G16B16A16_FLOAT'),
    'UByte4': (30, 'R8G8B8A8_UINT'), 'Byte4': (32, 'R8G8B8A8_SINT'),
    'NormUByte4': (28, 'R8G8B8A8_UNORM'), 'NormByte4': (31, 'R8G8B8A8_SNORM'),
    'UShort4': (12, 'R16G16B16A16_UINT'), 'Short4': (14, 'R16G16B16A16_SINT'),
    'UShort2': (36, 'R16G16_UINT'), 'Short2': (38, 'R16G16_SINT'),
    'NormUShort2': (35, 'R16G16_UNORM'), 'NormUDec3': (24, 'R10G10B10A2_UNORM'),
    'UDec3': (25, 'R10G10B10A2_UINT'),
    'Uint1': (42, 'R32_UINT'), 'Uint2': (17, 'R32G32_UINT'),
    'Uint3': (7, 'R32G32B32_UINT'), 'Uint4': (3, 'R32G32B32A32_UINT'),
    'Int1': (43, 'R32_SINT'), 'Int2': (18, 'R32G32_SINT'),
    'Int3': (8, 'R32G32B32_SINT'), 'Int4': (4, 'R32G32B32A32_SINT'),
}


def input_formats(game):
    return EngineEnumTable(game, 'via.render.InputElementFormat', _DXGI_BY_MEANING, maximum=0xFF)
