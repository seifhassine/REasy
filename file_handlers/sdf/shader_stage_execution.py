from dataclasses import dataclass
import re
from .shader_storage import ShaderStorageType


@dataclass(frozen=True, slots=True)
class ShaderMeshExecution:
    max_output_vertices: int = 0
    max_output_primitives: int = 0
    output_topology: str = ''
    payload_bytes: int = 0
    payload_layout: ShaderStorageType | None = None

    def __post_init__(self):
        if isinstance(self.payload_layout, dict):
            object.__setattr__(self, 'payload_layout', ShaderStorageType.from_data(self.payload_layout))

    def validate_stage(self, stage):
        from .shader_graph import ShaderGraphError
        if (any(type(n) is not int for n in (self.max_output_vertices, self.max_output_primitives, self.payload_bytes))
                or not 0 <= self.payload_bytes <= 16384
                or stage not in {'mesh', 'amplification'}):
            raise ShaderGraphError('Invalid mesh/amplification execution declaration.')
        if stage == 'mesh':
            valid = (0 <= self.max_output_vertices <= 256 and 0 <= self.max_output_primitives <= 256
                     and self.output_topology in {'line', 'triangle'})
        else:
            valid = not (self.max_output_vertices or self.max_output_primitives or self.output_topology)
        if not valid:
            raise ShaderGraphError('Mesh output declarations do not match their native stage.')
        if self.payload_layout is not None and (not isinstance(self.payload_layout, ShaderStorageType)
                or self.payload_layout.kind != 'struct' or self.payload_layout.byte_size != self.payload_bytes):
            raise ShaderGraphError('Typed amplification payload disagrees with its declared native extent.')


@dataclass(frozen=True, slots=True)
class ShaderPrimitiveExecution:
    input_control_points: int = 0
    output_control_points: int = 0
    domain: str = ''
    partitioning: str = ''
    tessellation_primitive: str = ''
    # None: the native hull declares no maximum (FXC then emits no factor clamp).
    max_tessellation_factor: float | None = 64.0
    input_primitive: str = ''
    output_topology: str = ''
    max_output_vertices: int = 0
    geometry_instances: int = 1
    output_streams: tuple[int, ...] = ()

    def __post_init__(self):
        from .shader_graph import ShaderGraphError
        object.__setattr__(self, 'output_streams', tuple(self.output_streams) or ((0,) if self.input_primitive else ()))
        integers = (self.input_control_points, self.output_control_points, self.max_output_vertices,
                    self.geometry_instances, *self.output_streams)
        if (any(type(n) is not int for n in integers)
                or not 0 <= self.input_control_points <= 32 or not 0 <= self.output_control_points <= 32
                or self.domain not in {'', 'tri', 'quad', 'isoline'}
                or self.partitioning not in {'', 'integer', 'pow2', 'fractional_even', 'fractional_odd'}
                or self.tessellation_primitive not in {'', 'point', 'line', 'triangle_cw', 'triangle_ccw'}
                or (self.max_tessellation_factor is not None and not 1 <= self.max_tessellation_factor <= 64)
                or self.input_primitive not in {'', 'point', 'line', 'triangle', 'lineadj', 'triangleadj'}
                or self.output_topology not in {'', 'pointlist', 'linestrip', 'trianglestrip'}
                or not 0 <= self.max_output_vertices <= 1024 or not 1 <= self.geometry_instances <= 32
                or self.output_streams != tuple(sorted(set(self.output_streams)))
                or any(stream not in range(4) for stream in self.output_streams)):
            raise ShaderGraphError('Invalid primitive/tessellation execution declaration')

    def validate_stage(self, stage):
        from .shader_graph import ShaderGraphError
        stage = {'geometry':'gs', 'hull':'hs', 'domain':'ds'}.get(stage, stage)
        if stage in {'ds', 'hs'} and (not self.input_control_points or not self.domain):
            raise ShaderGraphError('Tessellation requires a declared domain and control-point count')
        if stage == 'hs' and (not self.output_control_points or not self.partitioning or not self.tessellation_primitive):
            raise ShaderGraphError('Hull shader has incomplete tessellator declarations')
        if stage == 'gs' and (not self.input_primitive or not self.output_topology or not self.max_output_vertices):
            raise ShaderGraphError('Geometry shader has incomplete primitive declarations')
        if len(self.output_streams) > 1 and (stage != 'gs' or self.output_topology != 'pointlist'):
            raise ShaderGraphError('Multiple geometry streams require point-list output')
        if stage not in {'hs', 'ds', 'gs'} and self != ShaderPrimitiveExecution():
            raise ShaderGraphError('Primitive declarations occur in the wrong shader stage')

    @property
    def primitive_vertices(self):
        return self.input_control_points or {'point':1, 'line':2, 'triangle':3, 'lineadj':4, 'triangleadj':6}[self.input_primitive]


def patch_field_members(fields):
    from .shader_graph import ShaderGraphError
    result = []
    for field in fields:
        name = re.sub(r"[^A-Za-z0-9_]", "_", f"patch_{field.semantic_name}")
        peers = sorted((f for f in fields if f.semantic_name == field.semantic_name), key=lambda f: f.semantic_index)
        if len({p.semantic_index for p in peers}) != len(peers):
            raise ShaderGraphError('Patch-constant semantic identities are duplicated')
        array = (len(peers) > 1 and [p.semantic_index for p in peers] == list(range(len(peers)))
                 and all((p.mask, p.component_type) == (field.mask, field.component_type) for p in peers))
        if len(peers) > 1 and not array:
            name += f'_{field.semantic_index}'
        result.append((field, name, len(peers) if array else 1, f"{name}[{field.semantic_index}]" if array else name))
    return result


def patch_struct(fields):
    lines = ["struct REasyPatchConstants", "{"]
    for field, name, count, _ in patch_field_members(fields):
        if field.semantic_index == 0 or count == 1:
            semantic = field.semantic_name + (str(field.semantic_index) if field.semantic_index else "")
            lines.append(f"    {field.value_type} {name}{f'[{count}]' if count > 1 else ''} : {semantic};")
    return lines + ["};", ""]
