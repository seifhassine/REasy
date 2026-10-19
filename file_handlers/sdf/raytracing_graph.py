from dataclasses import asdict, dataclass, field
import hashlib
from itertools import islice
import json
import re

from .shader_graph import (ShaderContract, ShaderGraph, ShaderGraphError, ShaderGraphNodeExtension,
                           ShaderPin, connection_compatible, node_pins, register_shader_graph_node_extension,
                           generate_graph_function, validate_shader_contract, MAX_PROJECT_LENGTH, MAX_SOURCE_LENGTH,
                           _identifier)
from .aggregate_values import constant_value_fields
lazy from .shader_contract_storage import contract_from_storage, contract_to_storage
lazy from .hlsl_contract import contract_structures, contract_cbuffer, resource_declaration, resource_identifier
from .dxc import compile_hlsl_dxc, validate_dxil
lazy from . import dxil_structured_views, raytracing_abi
lazy from .models import SdfRayTracingLibrary
from .raytracing import (
    DXR_EXPORT_STAGES, PAYLOAD_ACCESS_STAGES, raytracing_export_fields, validate_raytracing_library,
)
from PySide6.QtCore import QT_TRANSLATE_NOOP


RAY_STAGES = ('raygeneration', 'closesthit', 'miss', 'anyhit', 'intersection', 'callable')
_IDENTIFIER = re.compile(r'[A-Za-z_]\w*', re.ASCII)
_SCALAR_VECTOR = re.compile(r'(float|uint|int|bool)([1-4])?')


def _name(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or len(value) > 128:
        raise ShaderGraphError(f'Invalid ray-tracing identifier: {value!r}')
    return value


@dataclass(frozen=True)
class RayField:
    name: str
    value_type: str
    dimensions: tuple[int, ...] = ()
    read_access: tuple[str, ...] | None = None
    write_access: tuple[str, ...] | None = None

    def __post_init__(self):
        _name(self.name)
        _name(self.value_type)
        object.__setattr__(self, 'dimensions', tuple(self.dimensions))
        if len(self.dimensions) > 8 or any(not isinstance(n, int) or not 1 <= n <= 65536 for n in self.dimensions):
            raise ShaderGraphError('Ray-tracing field has an invalid declared array dimension')
        for field in ('read_access','write_access'):
            value = getattr(self,field)
            if value is None: continue
            if len(set(value))!=len(value) or set(value)-set(PAYLOAD_ACCESS_STAGES):
                raise ShaderGraphError('Invalid declared payload-access stage')
            object.__setattr__(self,field,tuple(s for s in PAYLOAD_ACCESS_STAGES if s in value))
        if (self.read_access is None)!=(self.write_access is None):
            raise ShaderGraphError('Payload fields must declare both read and write access')

    @property
    def declaration(self):
        return f'{self.value_type} {self.name}' + ''.join(f'[{count}]' for count in self.dimensions)


@dataclass(frozen=True)
class RayStructure:
    name: str
    fields: tuple[RayField, ...]

    def __post_init__(self):
        _name(self.name)
        object.__setattr__(self, 'fields', tuple(self.fields))
        if len(self.fields) > 1024 or len({f.name for f in self.fields}) != len(self.fields):
            raise ShaderGraphError('Ray-tracing structure needs distinct declared fields')


@dataclass
class RayExport:
    name: str
    stage: str
    graph: ShaderGraph
    payload_type: str = ''
    attribute_type: str = ''
    callable_type: str = ''

    def __post_init__(self):
        _name(self.name)
        if self.stage not in RAY_STAGES or self.graph.stage != self.stage:
            raise ShaderGraphError('Ray-tracing export has an invalid graph stage')


@dataclass
class RayTracingGraphProject:
    contract: ShaderContract
    structures: list[RayStructure] = field(default_factory=list)
    exports: list[RayExport] = field(default_factory=list)
    bindings: dict[str, str] = field(default_factory=dict)
    export_layout: str = 'standard'

    @property
    def explicit_payload_access(self):
        return any(member.read_access is not None for structure in self.structures for member in structure.fields)

    def structure(self, name):
        return next((item for item in self.structures if item.name == name), None)

    def add_export(self, name, stage, *, payload_type='', attribute_type='', callable_type=''):
        if any(export.name == name for export in self.exports):
            raise ShaderGraphError(f'Duplicate ray-tracing export {name}')
        graph = ShaderGraph(stage, self.contract.fingerprint)
        entry = graph.add_node('ray_entry', x=20, y=40)
        end = graph.add_node('ray_return', x=450, y=40)
        graph.connect(entry.node_id, 'next', end.node_id, 'exec')
        export = RayExport(name, stage, graph, payload_type, attribute_type, callable_type)
        self.exports.append(export)
        return export

    def fields(self, name):
        def visit(value_type, dimensions, path, stack):
            if len(stack) > 16 or value_type in stack:
                raise ShaderGraphError('Ray-tracing value types contain recursive nesting')
            if dimensions:
                for index in range(dimensions[0]):
                    yield from visit(value_type, dimensions[1:], path + f'[{index}]', stack)
            elif _SCALAR_VECTOR.fullmatch(value_type):
                yield path, value_type
            else:
                structure = self.structure(value_type)
                if structure is None:
                    raise ShaderGraphError(f'Unknown ray-tracing value type {value_type}')
                for member in structure.fields:
                    yield from visit(member.value_type, member.dimensions, path + '.' + member.name, (*stack, value_type))
        fields = tuple(islice(visit(name, (), '', ()),65537))
        if len(fields)>65536: raise ShaderGraphError('Ray-tracing type has too many declared leaf fields')
        return fields

    def type_declarations(self):
        emitted, visiting, lines = set(), set(), []
        payloads = {e.payload_type for e in self.exports if e.payload_type}
        annotated = {s.name for s in self.structures if any(f.read_access is not None for f in s.fields)}
        if self.contract.model_minor>=7 or self.explicit_payload_access: annotated.update(payloads)
        def emit(name):
            if _SCALAR_VECTOR.fullmatch(name) or name in emitted:
                return
            if name in visiting:
                raise ShaderGraphError('Recursive ray-tracing value structure')
            structure = self.structure(name)
            if structure is None:
                raise ShaderGraphError(f'Unknown ray-tracing structure {name}')
            visiting.add(name)
            for member in structure.fields: emit(member.value_type)
            fields = []
            for member in structure.fields:
                declaration = member.declaration
                if name in annotated and member.value_type not in annotated:
                    reads = PAYLOAD_ACCESS_STAGES if member.read_access is None else member.read_access
                    writes = PAYLOAD_ACCESS_STAGES if member.write_access is None else member.write_access
                    declaration += f' : read({",".join(reads)}) : write({",".join(writes)})'
                elif name in annotated and member.read_access not in (None,()):
                    raise ShaderGraphError('Nested annotated payload fields inherit access from their declared type')
                fields.append('    '+declaration+';')
            qualifier = '[raypayload] ' if name in annotated else ''
            lines.append(f'struct {qualifier}{name}\n{{\n' + '\n'.join(fields) + '\n};')
            visiting.remove(name)
            emitted.add(name)
        for structure in self.structures: emit(structure.name)
        return tuple(lines)

    def to_bytes(self):
        data = dict(schema='reasy.raytracing-graph', version=1, contract=contract_to_storage(self.contract),
                    structures=[asdict(item) for item in self.structures], bindings=dict(self.bindings),
                    export_layout=self.export_layout,
                    exports=[dict(name=e.name, stage=e.stage, payload_type=e.payload_type, attribute_type=e.attribute_type,
                                  callable_type=e.callable_type, graph=e.graph.to_dict()) for e in self.exports])
        raw = json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if len(raw) > MAX_PROJECT_LENGTH: raise ShaderGraphError('Ray-tracing graph project is too large')
        return raw

    @classmethod
    def from_bytes(cls, raw):
        if len(raw) > MAX_PROJECT_LENGTH: raise ShaderGraphError('Ray-tracing graph project is too large')
        data = json.loads(raw)
        if data.get('schema') != 'reasy.raytracing-graph' or data.get('version') != 1:
            raise ShaderGraphError('Invalid ray-tracing graph project')
        structures = [RayStructure(row['name'], tuple(RayField(**member) for member in row['fields'])) for row in data['structures']]
        exports = [RayExport(row['name'], row['stage'], ShaderGraph.from_dict(row['graph']), row.get('payload_type',''),
                             row.get('attribute_type',''), row.get('callable_type','')) for row in data['exports']]
        if len(structures) > 1024 or not 1 <= len(exports) <= 1024:
            raise ShaderGraphError('Ray-tracing project has an invalid type/export count')
        result = cls(contract_from_storage(data['contract'], ''), structures, exports, dict(data['bindings']),
                     data.get('export_layout', 'standard'))
        result.validate()
        return result

    def validate(self):
        if self.contract.stage != 'library' or self.contract.model_major != 6 or self.contract.model_minor < 3:
            raise ShaderGraphError('DXR graphs require a Shader Model 6.3+ library contract')
        if self.explicit_payload_access and self.contract.model_minor<6:
            raise ShaderGraphError('Payload-access qualifiers require Shader Model 6.6 or newer')
        if len(self.structures)>1024 or len(self.exports)>1024:
            raise ShaderGraphError('Ray-tracing project exceeds its type/export limit')
        if len({item.name for item in self.structures}) != len(self.structures):
            raise ShaderGraphError('Duplicate ray-tracing structure names')
        if not self.exports or len({item.name for item in self.exports}) != len(self.exports):
            raise ShaderGraphError('Ray-tracing graphs need distinct exports')
        self.type_declarations()
        try:
            export_fields = raytracing_export_fields(self.export_layout)
        except ValueError as exc:
            raise ShaderGraphError(str(exc)) from exc
        if set(self.bindings) - set(export_fields) or not self.bindings.get('raygen'):
            raise ShaderGraphError('DXR envelope needs a ray-generation export and only roles supported by its layout')
        for role, name in self.bindings.items():
            if not name: continue
            export = next((item for item in self.exports if item.name == name), None)
            if export is None or export.stage != DXR_EXPORT_STAGES[role] or len(name.encode('ascii')) >= 32:
                raise ShaderGraphError(f'DXR envelope {role} has an absent, wrong-stage or overlong export')
        for export in self.exports:
            if export.stage not in RAY_STAGES or export.graph.stage != export.stage:
                raise ShaderGraphError('Ray-tracing graph stage disagrees with its export')
            _name(export.name)
            for name in (export.payload_type, export.attribute_type, export.callable_type):
                if name: self.fields(name)
            if export.stage in {'closesthit','miss','anyhit'} and not export.payload_type:
                raise ShaderGraphError(f'{export.name} requires an explicit payload type')
            if export.stage in {'closesthit','anyhit'} and not export.attribute_type:
                raise ShaderGraphError(f'{export.name} requires an explicit attribute type')
            if export.stage == 'callable' and not export.callable_type:
                raise ShaderGraphError(f'{export.name} requires an explicit callable-data type')


RAY_BUILTINS = {
    'DispatchRaysIndex': 'uint3', 'DispatchRaysDimensions': 'uint3',
    'WorldRayOrigin': 'float3', 'WorldRayDirection': 'float3',
    'ObjectRayOrigin': 'float3', 'ObjectRayDirection': 'float3',
    'RayTMin': 'float', 'RayTCurrent': 'float', 'InstanceIndex': 'uint',
    'InstanceID': 'uint', 'PrimitiveIndex': 'uint', 'HitKind': 'uint',
    'RayFlags': 'uint', 'GeometryIndex': 'uint',
}
_FLOW = frozenset({'ray_entry','ray_return','ray_if','ray_loop','ray_break','ray_continue',
                   'ray_set','ray_trace','ray_call','ray_report_hit','ray_store','ray_ignore_hit','ray_accept_hit'})
_TERMINATORS = frozenset({'ray_return','ray_break','ray_continue','ray_ignore_hit','ray_accept_hit'})
_PURE = frozenset({'ray_builtin','ray_state','ray_constant','ray_loop_index','ray_compare','ray_cast','ray_binary','ray_read','ray_sample'})


def _numeric_type(value):
    if not isinstance(value, str) or not _SCALAR_VECTOR.fullmatch(value):
        raise ShaderGraphError('Ray graph socket requires a declared scalar/vector type')
    return value


def _resource(contract, node):
    resource = contract.resource(str(node.properties.get('resource','')))
    if resource is None: raise ShaderGraphError('Ray graph refers to an unbound shader resource')
    return resource


def _resource_coordinate(resource):
    return {'buffer':'uint','texture1d':'uint','texture1d-array':'uint2','texture2d':'uint2',
            'texture2d-array':'uint3','texture3d':'uint3'}.get(resource.dimension, 'uint')


def _ray_pins(node, contract):
    kind, properties = node.kind, node.properties
    pins = []
    def socket(name, value_type, direction='input'):
        pins.append(ShaderPin(name, name.replace('_',' ').title(), value_type, direction, direction == 'input'))
    if kind in _FLOW:
        if kind != 'ray_entry': socket('exec', 'execution')
        if kind not in _TERMINATORS: socket('next', 'execution', 'output')
    if kind == 'ray_if':
        socket('condition','bool'); socket('true','execution','output'); socket('false','execution','output')
    elif kind == 'ray_loop':
        socket('count','uint'); socket('body','execution','output')
    elif kind in {'ray_set','ray_state','ray_constant'}:
        socket('value', _numeric_type(properties.get('value_type','float')), 'input' if kind == 'ray_set' else 'output')
    elif kind == 'ray_builtin':
        value_type = RAY_BUILTINS.get(properties.get('builtin'))
        if value_type is None: raise ShaderGraphError('Unknown DXR intrinsic')
        mask = str(properties.get('mask',''))
        if mask:
            width = int(value_type[-1]) if value_type[-1].isdigit() else 1
            if not 1 <= len(mask) <= 4 or any(c not in 'xyzw'[:width] for c in mask):
                raise ShaderGraphError('Invalid DXR intrinsic component selection')
            value_type = value_type.rstrip('1234') + (str(len(mask)) if len(mask)>1 else '')
        socket('value',value_type,'output')
    elif kind == 'ray_loop_index': socket('value','uint','output')
    elif kind in {'ray_compare','ray_binary'}:
        value_type = _numeric_type(properties.get('value_type','uint'))
        socket('a',value_type); socket('b',value_type)
        socket('value','bool' if kind == 'ray_compare' else value_type,'output')
    elif kind == 'ray_cast':
        socket('value',_numeric_type(properties.get('source_type','float')))
        socket('result',_numeric_type(properties.get('value_type','uint')),'output')
    elif kind == 'ray_trace':
        socket('origin','float3'); socket('direction','float3'); socket('t_min','float'); socket('t_max','float')
        for name in ('flags','mask','hit_group','geometry_stride','miss_index'): socket(name,'uint')
        if _resource(contract,node).bind_count != 1: socket('descriptor','uint')
    elif kind == 'ray_call': socket('record_index','uint')
    elif kind == 'ray_report_hit':
        socket('t','float'); socket('hit_kind','uint'); socket('accepted','bool','output')
    elif kind in {'ray_read','ray_store'}:
        resource = _resource(contract,node)
        socket('index',_resource_coordinate(resource))
        value_type = _numeric_type(properties.get('value_type','uint4' if 'byte-address' in resource.input_type else 'float4'))
        socket('value',value_type,'output' if kind == 'ray_read' else 'input')
        if resource.input_type == 'texture': socket('mip','uint')
        if resource.bind_count != 1: socket('descriptor','uint')
    elif kind == 'ray_sample':
        resource = _resource(contract,node)
        if not resource.sampleable: raise ShaderGraphError('Ray sample needs a texture resource')
        socket('coordinates',resource.coordinate_type); socket('level','float'); socket('value','float4','output')
        if resource.bind_count != 1: socket('descriptor','uint')
    return tuple(pins)


def _ray_expression_unbound(_node, _pin, _incoming):
    raise ShaderGraphError('Ray nodes require their export/payload context')


for _kind in sorted(_FLOW | _PURE):
    register_shader_graph_node_extension(ShaderGraphNodeExtension(
        kind=_kind, title=_kind.removeprefix('ray_').replace('_',' ').title(), pins_provider=_ray_pins,
        expression=_ray_expression_unbound, description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Typed DXR export graph operation.'),
    ))


class _ExportCompiler:
    def __init__(self, project, export):
        self.project, self.export, self.contract, self.graph = project, export, project.contract, export.graph
        self.incoming = {(link.target_node,link.target_pin):link for link in self.graph.links}
        self.flow, self.executed, self.active_loops, self.available_results = {}, set(), {}, set()
        self.used_resources, self.used_buffers = set(), set()
        self.names = {node.node_id:f'REasyRayNode{index}' for index,node in enumerate(self.graph.nodes)}
        for link in self.graph.links:
            source, target = self.graph.node(link.source_node), self.graph.node(link.target_node)
            if source is None or target is None: raise ShaderGraphError('Ray graph has a dangling connection')
            out_pin = next((pin for pin in node_pins(source,self.contract) if pin.direction=='output' and pin.name==link.source_pin),None)
            in_pin = next((pin for pin in node_pins(target,self.contract) if pin.direction=='input' and pin.name==link.target_pin),None)
            if out_pin is None or in_pin is None or not connection_compatible(out_pin.value_type,in_pin.value_type):
                raise ShaderGraphError(f'{source.kind}.{link.source_pin} -> {target.kind}.{link.target_pin}: '
                                       f'absent or mismatched typed socket ({out_pin} -> {in_pin})')
            if out_pin.value_type == 'execution':
                key = source.node_id, out_pin.name
                if key in self.flow: raise ShaderGraphError('Execution outputs cannot fork implicitly; use an If node')
                self.flow[key] = target
        if len(self.incoming) != len(self.graph.links): raise ShaderGraphError('Ray input has multiple connections')

    def state(self,node, *, write=False):
        domain = node.properties.get('domain','payload')
        type_name, identifier = {'payload':(self.export.payload_type,'payload'),
                                 'attributes':(self.export.attribute_type,'attributes'),
                                 'callable':(self.export.callable_type,'callableData')}.get(domain, ('',''))
        if not type_name: raise ShaderGraphError(f'{self.export.name} has no {domain} value')
        if write and domain == 'attributes' and self.export.stage in {'closesthit','anyhit'}:
            raise ShaderGraphError('Hit-shader intersection attributes are read-only')
        path = str(node.properties.get('field',''))
        fields = dict(self.project.fields(type_name))
        if path not in fields or fields[path] != node.properties.get('value_type','float'):
            raise ShaderGraphError(f'{domain}{path} does not match the declared payload field/type')
        return identifier + path

    def resource(self,node,incoming):
        resource = _resource(self.contract,node)
        self.used_resources.add(resource.name)
        name = resource_identifier(resource)
        if resource.bind_count != 1:
            name += f"[NonUniformResourceIndex({incoming('descriptor','uint')})]"
        return resource, name

    def resource_value_type(self,node,resource):
        value_type = _numeric_type(node.properties.get('value_type','uint4' if 'byte-address' in resource.input_type else 'float4'))
        if 'byte-address' in resource.input_type:
            if not value_type.startswith('uint'): raise ShaderGraphError('Raw resource loads/stores expose uint words; use a bit-cast node')
        elif resource.input_type in {'structured-buffer','rw-structured-buffer'}:
            layout = self.contract.structured_buffer(resource.name)
            field_name = node.properties.get('field','')
            variable = next((v for v in layout.variables if v.name == field_name),None) if layout else None
            if variable is None or variable.graph_type != value_type:
                raise ShaderGraphError('Structured resource access must name a reflected scalar/vector field')
        else:
            expected = {'uint':'uint4','sint':'int4'}.get(resource.return_type,'float4')
            if value_type != expected: raise ShaderGraphError(f'Typed resource expects {expected}, not {value_type}')
        return value_type

    def expression(self,node,pin,incoming):
        kind, properties = node.kind, node.properties
        if kind in {'ray_constant','parameter'}:
            buffer = self.contract.buffer(str(properties.get('buffer','')))
            variable = next((v for v in buffer.variables if v.name==properties.get('variable')),None) if buffer else None
            path = str(properties.get('field',''))
            fields = constant_value_fields(variable) if variable else ()
            declared = next((f for f in fields if f.path==path),None)
            if declared is None or declared.value_type != properties.get('value_type',variable.graph_type if variable else ''):
                raise ShaderGraphError('Ray constant does not match a reflected buffer field/type')
            self.used_buffers.add(buffer.name)
            return _constant_identifier(buffer,variable) + path
        if kind == 'ray_builtin':
            _ray_pins(node,self.contract)
            name, mask = properties['builtin'], str(properties.get('mask',''))
            return name + '()' + ('.'+mask if mask else '')
        if kind == 'ray_state': return self.state(node)
        if kind == 'ray_loop_index':
            name = self.active_loops.get(properties.get('loop'))
            if name is None: raise ShaderGraphError('Loop index is used outside its owning loop body')
            return name
        if kind == 'ray_report_hit' and pin == 'accepted':
            if node.node_id not in self.available_results: raise ShaderGraphError('ReportHit result is used before execution or outside its branch')
            return self.names[node.node_id] + '_accepted'
        if kind in {'ray_compare','ray_binary'}:
            value_type = properties.get('value_type','uint')
            operator = properties.get('operator','==' if kind=='ray_compare' else '&')
            valid = {'==','!=','<','<=','>','>='} if kind=='ray_compare' else {'&','|','^','<<','>>','%'}
            if operator not in valid: raise ShaderGraphError('Invalid ray graph operator')
            if kind=='ray_compare' and value_type[-1].isdigit(): raise ShaderGraphError('Flow comparisons require scalar operands')
            if kind=='ray_binary' and not value_type.startswith(('int','uint')): raise ShaderGraphError('Bit operations need integer operands')
            return f"({incoming('a',value_type)} {operator} {incoming('b',value_type)})"
        if kind == 'ray_cast':
            source_type, target_type = properties.get('source_type','float'), properties.get('value_type','uint')
            value = incoming('value',source_type)
            if properties.get('bitcast',False):
                source = _SCALAR_VECTOR.fullmatch(source_type); target = _SCALAR_VECTOR.fullmatch(target_type)
                if not source or not target or source[1]=='bool' or target[1]=='bool' or source[2]!=target[2]:
                    raise ShaderGraphError('Bit casts require equal-width 32-bit numeric vectors')
                return f'as{target[1]}({value})'
            return f'{target_type}({value})'
        if kind == 'ray_read':
            resource, name = self.resource(node,incoming)
            value_type = self.resource_value_type(node,resource)
            index = incoming('index',_resource_coordinate(resource))
            if 'byte-address' in resource.input_type:
                width = value_type[-1] if value_type[-1].isdigit() else ''
                return f'{name}.Load{width}({index})'
            if resource.input_type in {'structured-buffer','rw-structured-buffer'}:
                return f"{name}[{index}].{_name(properties['field'])}"
            if resource.input_type == 'texture':
                coordinate = _resource_coordinate(resource)
                width = int(coordinate[-1]) if coordinate[-1].isdigit() else 1
                return f"{name}.Load(int{width+1}({index}, {incoming('mip','uint')}))"
            if resource.input_type in {'rw-typed','texture-buffer'}: return f'{name}[{index}]'
            raise ShaderGraphError('Ray graph resource has no typed load operation')
        if kind == 'ray_sample':
            resource, name = self.resource(node,incoming)
            sampler = self.contract.resource(str(properties.get('sampler','')))
            if sampler is None or sampler.input_type != 'sampler' or sampler.flags & 2 or sampler.bind_count != 1:
                raise ShaderGraphError('Ray sample requires a bound ordinary sampler')
            self.used_resources.add(sampler.name)
            return f"{name}.SampleLevel({resource_identifier(sampler)}, {incoming('coordinates',resource.coordinate_type)}, {incoming('level','float')})"
        return None

    def body(self,_expression,incoming):
        entries = [node for node in self.graph.nodes if node.kind == 'ray_entry']
        if len(entries) != 1: raise ShaderGraphError('An export graph requires exactly one execution entry')
        lines = []
        parameter_domains = ({'payload','attributes'} if self.export.stage in {'closesthit','anyhit'} else
                             {'payload'} if self.export.stage=='miss' else {'callable'} if self.export.stage=='callable' else set())
        for domain, value_type, identifier in (('payload',self.export.payload_type,'payload'),
                                               ('attributes',self.export.attribute_type,'attributes'),
                                               ('callable',self.export.callable_type,'callableData')):
            if value_type and domain not in parameter_domains: lines.append(f'    {value_type} {identifier} = ({value_type})0;')
        def emit_chain(node,depth):
            while node is not None:
                if node.node_id in self.executed: raise ShaderGraphError('Execution regions overlap or contain a cycle; use structured If/Loop nodes')
                self.executed.add(node.node_id)
                prefix = '    '*depth
                read = lambda name,kind: incoming(node,name,kind)
                kind, name = node.kind, self.names[node.node_id]
                if kind not in _FLOW: raise ShaderGraphError('A value node cannot execute as a statement')
                if kind == 'ray_entry': pass
                elif kind == 'ray_return': lines.append(prefix+'return;')
                elif kind in {'ray_break','ray_continue'}:
                    if not self.active_loops: raise ShaderGraphError('Break/Continue requires an enclosing loop')
                    lines.append(prefix+('break;' if kind=='ray_break' else 'continue;'))
                elif kind in {'ray_ignore_hit','ray_accept_hit'}:
                    if self.export.stage != 'anyhit': raise ShaderGraphError('Hit-acceptance operations require an any-hit export')
                    lines.append(prefix+('IgnoreHit();' if kind=='ray_ignore_hit' else 'AcceptHitAndEndSearch();'))
                elif kind == 'ray_set': lines.append(prefix+f"{self.state(node,write=True)} = {read('value',node.properties.get('value_type','float'))};")
                elif kind == 'ray_if':
                    condition = read('condition','bool')
                    for branch,label in (('true',f'if ({condition})'),('false','else')):
                        lines.extend((prefix+label,prefix+'{'))
                        saved = set(self.available_results)
                        emit_chain(self.flow.get((node.node_id,branch)),depth+1)
                        self.available_results = saved
                        lines.append(prefix+'}')
                elif kind == 'ray_loop':
                    lines.append(prefix+f"uint {name}_count = {read('count','uint')};")
                    lines.extend((prefix+f'for (uint {name}_index=0; {name}_index<{name}_count; ++{name}_index)',prefix+'{'))
                    self.active_loops[node.node_id] = name+'_index'
                    saved = set(self.available_results)
                    emit_chain(self.flow.get((node.node_id,'body')),depth+1)
                    self.available_results = saved
                    self.active_loops.pop(node.node_id)
                    lines.append(prefix+'}')
                elif kind == 'ray_trace':
                    if not self.export.payload_type: raise ShaderGraphError('TraceRay needs a declared payload type')
                    resource, identifier = self.resource(node,read)
                    if resource.input_type != 'raytracing-acceleration-structure': raise ShaderGraphError('TraceRay requires an acceleration structure')
                    lines.extend((prefix+f'RayDesc {name}_ray;', prefix+f"{name}_ray.Origin = {read('origin','float3')};",
                                  prefix+f"{name}_ray.Direction = {read('direction','float3')};",prefix+f"{name}_ray.TMin = {read('t_min','float')};",
                                  prefix+f"{name}_ray.TMax = {read('t_max','float')};"))
                    arguments = ', '.join(read(pin,'uint') for pin in ('flags','mask','hit_group','geometry_stride','miss_index'))
                    lines.append(prefix+f'TraceRay({identifier}, {arguments}, {name}_ray, payload);')
                elif kind == 'ray_call':
                    if not self.export.callable_type: raise ShaderGraphError('CallShader requires a declared callable-data type')
                    lines.append(prefix+f"CallShader({read('record_index','uint')}, callableData);")
                elif kind == 'ray_report_hit':
                    if self.export.stage != 'intersection' or not self.export.attribute_type:
                        raise ShaderGraphError('ReportHit requires an intersection export with declared attributes')
                    lines.append(prefix+f"bool {name}_accepted = ReportHit({read('t','float')}, {read('hit_kind','uint')}, attributes);")
                    self.available_results.add(node.node_id)
                elif kind == 'ray_store':
                    resource, identifier = self.resource(node,read)
                    if resource.register_class != 'u': raise ShaderGraphError('Store requires a writable resource')
                    value_type = self.resource_value_type(node,resource)
                    index, value = read('index',_resource_coordinate(resource)), read('value',value_type)
                    if resource.input_type == 'rw-byte-address-buffer':
                        width = value_type[-1] if value_type[-1].isdigit() else ''
                        line = f'{identifier}.Store{width}({index}, {value});'
                    else:
                        member = '.'+_name(node.properties['field']) if resource.input_type=='rw-structured-buffer' else ''
                        line = f'{identifier}[{index}]{member} = {value};'
                    lines.append(prefix+line)
                if kind in _TERMINATORS: break
                node = self.flow.get((node.node_id,'next'))
        emit_chain(entries[0],1)
        return lines


def generate_raytracing_library(project):
    project.validate()
    globals_, functions, resources, buffers = [], [], set(), set()
    for export in project.exports:
        compiler = _ExportCompiler(project,export)
        result = generate_graph_function(export.graph,project.contract,compiler.body,value_resolver=compiler.expression)
        globals_.extend(result.declarations)
        resources.update(compiler.used_resources)
        buffers.update(compiler.used_buffers)
        parameters = []
        if export.stage in {'closesthit','anyhit','miss'}: parameters.append(f'inout {export.payload_type} payload')
        if export.stage in {'closesthit','anyhit'}: parameters.append(f'in {export.attribute_type} attributes')
        if export.stage=='callable': parameters.append(f'inout {export.callable_type} callableData')
        functions.append(f'[shader("{export.stage}")] void {export.name}({", ".join(parameters)})\n{{\n'+ '\n'.join(result.body)+'\n}')
    declarations = [contract_structures(*project.contract.constant_buffers,*project.contract.structured_buffers),*project.type_declarations()]
    declarations.extend(resource_declaration(resource,project.contract.structured_buffer(resource.name))
                        for resource in project.contract.resources if resource.name in resources and resource.register_class!='b')
    declarations.extend(contract_cbuffer(buffer,_constant_identifier)[0]
                        for buffer in project.contract.constant_buffers if buffer.name in buffers)
    declarations.extend(dict.fromkeys(globals_))
    source = '\n\n'.join((*declarations,*functions))
    if len(source.encode()) > MAX_SOURCE_LENGTH: raise ShaderGraphError('Ray-tracing graph source is too large')
    return source


def build_raytracing_graph(project, *, diagnostics=None):
    source = generate_raytracing_library(project)
    arguments = ['-WX','-Wno-error=payload-access-perf']
    if project.contract.model_minor==6 and project.explicit_payload_access:
        arguments.append('-enable-payload-qualifiers')
    result = compile_hlsl_dxc(source,entry_point='',target=project.contract.target,extra_arguments=tuple(arguments))
    if diagnostics is not None and result.diagnostics.strip('\0\n\r '):
        diagnostics.append(result.diagnostics.strip('\0\n\r '))
    bytecode = dxil_structured_views.coalesce_structured_views(
        result.bytecode, project.contract, dxil_structured_views.structured_view_spaces(project.contract))
    bytecode = validate_dxil(bytecode)
    library = SdfRayTracingLibrary(
        **{role:project.bindings.get(role,'') for role in raytracing_export_fields(project.export_layout)},
        bytecode=bytecode, export_layout=project.export_layout)
    reflected = validate_raytracing_library(library,reflect=True)
    report = validate_shader_contract(project.contract,ShaderContract.from_shader(reflected))
    if report.errors:
        raise ShaderGraphError('\n'.join(issue.message for issue in report.errors))
    functions = {function.name:function.stage for function in reflected.reflection.library_functions if function.stage in RAY_STAGES}
    if functions != {export.name:export.stage for export in project.exports}:
        raise ShaderGraphError('Compiled DXR library does not contain exactly the declared graph exports')
    return library


def _constant_identifier(buffer,variable):
    return _identifier(f'REasyRay_b{buffer.bind_point}_s{buffer.register_space}_{variable.name}')


def new_raytracing_graph(library):
    shader = validate_raytracing_library(library,reflect=True)
    project = RayTracingGraphProject(ShaderContract.from_shader(shader),
        bindings={role:getattr(library,role) for role in raytracing_export_fields(library.export_layout)},
        export_layout=library.export_layout)
    types = {}
    def declare(shape):
        if shape.kind in {'float','i32','i1'}:
            return {'float':'float','i32':'uint','i1':'bool'}[shape.kind], ()
        if shape.kind == 'vector':
            return declare(shape.children[0])[0] + str(shape.count), ()
        if shape.kind == 'array':
            value_type, dimensions = declare(shape.children[0])
            return value_type, (shape.count,*dimensions)
        identity = repr(shape)  # Includes PAQs as well as the declared shape.
        if identity in types: return types[identity], ()
        name = 'RayValue_' + hashlib.sha256(identity.encode()).hexdigest()[:16]
        types[identity] = name
        fields = []
        for index,child in enumerate(shape.children):
            value_type, dimensions = declare(child)
            access = shape.access[index] if shape.access else (None,None)
            fields.append(RayField(f'field{index}',value_type,dimensions,*access))
        project.structures.append(RayStructure(name,tuple(fields)))
        return name, ()
    abis = raytracing_abi.library_abi(library, shader).exports
    defaults = {}
    for domain in ('payload','attributes','callable_data'):
        shape = next((getattr(abi,domain) for abi in abis.values() if getattr(abi,domain) is not None),None)
        defaults[domain] = declare(shape)[0] if shape is not None else ''
    for abi in abis.values():
        def parameter(domain):
            shape = getattr(abi,domain)
            if shape is None: return defaults[domain]
            value_type, dimensions = declare(shape)
            if dimensions: raise ShaderGraphError('DXR export parameter must be a declared structure, not a bare array')
            return value_type
        project.add_export(abi.name,abi.stage,payload_type=parameter('payload'),
                           attribute_type=parameter('attributes'),callable_type=parameter('callable_data'))
    project.validate()
    return project
