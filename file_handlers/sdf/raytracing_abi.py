from dataclasses import dataclass, field
import re

from .dxc import disassemble_dxil
lazy from .dxil_metadata import metadata_definitions, metadata_operands
from .raytracing import PAYLOAD_ACCESS_STAGES, raytracing_export_fields, validate_raytracing_library
lazy from .shader_graph import ShaderContract, ShaderGraphError, validate_shader_contract


_STAGES = {7:'raygeneration', 8:'intersection', 9:'anyhit', 10:'closesthit', 11:'miss', 12:'callable'}
_TOKEN = r'%(?:"(?:[^"\\]|\\.)*"|[-$._A-Za-z0-9]+)'
_FUNCTION = re.compile(r'@("(?:[^"\\]|\\.)*"|[-$._A-Za-z0-9]+)')


@dataclass(frozen=True)
class RayValueType:
    kind: str
    children: tuple['RayValueType', ...] = ()
    count: int = 1
    access: tuple = field(default=(),compare=False)


@dataclass(frozen=True)
class RayExportAbi:
    name: str
    stage: str
    payload: RayValueType | None = None
    attributes: RayValueType | None = None
    callable_data: RayValueType | None = None
    payload_size: int = 0
    attribute_size: int = 0


@dataclass(frozen=True)
class RayLibraryAbi:
    exports: dict[str, RayExportAbi]
    trace_payloads: tuple[RayValueType, ...]
    reported_attributes: tuple[RayValueType, ...]
    callable_arguments: tuple[RayValueType, ...]


def library_abi(library, shader=None):
    if shader is None:
        shader = validate_raytracing_library(library, reflect=True)
    lines = disassemble_dxil(library.bytecode).splitlines()
    definitions = metadata_definitions(lines)
    types = {}
    for line in lines:
        match = re.fullmatch(r'(' + _TOKEN + r') = type (.+)', line.strip())
        if match: types[match[1]] = match[2]
    annotations = {}
    for reference in definitions.get('!dx.dxrPayloadAnnotations', ()):
        row = definitions.get(reference, ())
        if not row or row[0]!='i32 0' or len(row)%2!=1:
            raise ShaderGraphError('Invalid DXR payload annotation structure')
        for name,fields in zip(row[1::2],row[2::2],strict=True):
            match = re.fullmatch('('+_TOKEN+') undef',name)
            if not match: raise ShaderGraphError('Payload annotation has no declared type')
            qualifiers = []
            for item in definitions.get(fields, ()):
                flags = definitions.get(item, ())
                if len(flags)!=2 or flags[0]!='i32 0' or not re.fullmatch(r'i32 \d+',flags[1]):
                    raise ShaderGraphError('Invalid DXR payload access qualifier')
                value = int(flags[1][4:])
                if value & ~0x3333: raise ShaderGraphError('Unknown DXR payload access qualifier bits')
                qualifiers.append((tuple(s for i,s in enumerate(PAYLOAD_ACCESS_STAGES) if value & (1<<(i*4))),
                                   tuple(s for i,s in enumerate(PAYLOAD_ACCESS_STAGES) if value & (2<<(i*4)))))
            annotations[match[1]] = tuple(qualifiers)

    def value_type(text, stack=()):
        if len(stack) > 32 or text in stack:
            raise ShaderGraphError('Recursive DXR payload type declaration')
        if text in {'float','i32','i1'}: return RayValueType(text)
        if text in types:
            shape = value_type(types[text], (*stack,text))
            if text in annotations:
                if shape.kind!='struct' or len(shape.children)!=len(annotations[text]):
                    raise ShaderGraphError('Payload qualifiers disagree with the declared member count')
                shape = RayValueType(shape.kind,shape.children,shape.count,annotations[text])
            return shape
        match = re.fullmatch(r'<([1-4]) x (float|i32|i1)>', text)
        if match: return RayValueType('vector', (RayValueType(match[2]),), int(match[1]))
        match = re.fullmatch(r'\[(\d+) x (.+)\]', text)
        if match and 1 <= int(match[1]) <= 65536:
            return RayValueType('array', (value_type(match[2],stack),), int(match[1]))
        if text.startswith('{') and text.endswith('}'):
            return RayValueType('struct', tuple(value_type(part,stack) for part in metadata_operands('!'+text)))
        raise ShaderGraphError(f'DXR payload has an unsupported declared value type: {text}')

    functions = {}
    for line in lines:
        if not line.startswith('define '): continue
        match = _FUNCTION.search(line)
        if not match: continue
        begin = match.end()
        if line[begin:begin+1] != '(': raise ShaderGraphError('Invalid DXR function declaration')
        end = line.rfind(')')
        functions[match[1]] = metadata_operands('!{' + line[begin+1:end] + '}')
    native = {function.name:function for function in shader.reflection.library_functions}
    result = {}
    for reference in definitions.get('!dx.entryPoints', ()):
        entry = definitions.get(reference, ())
        if len(entry) != 5: raise ShaderGraphError('Invalid DXR entry metadata')
        if entry[0] == 'null': continue  # Declared library resource root.
        function = _FUNCTION.search(entry[0])
        if not function or function[1] not in functions: raise ShaderGraphError('DXR entry has no declared function')
        properties = definitions.get(entry[4], ())
        if len(properties) % 2: raise ShaderGraphError('Invalid DXR entry property pairs')
        properties = dict(zip(properties[::2], properties[1::2], strict=True))
        def integer(tag, default=0):
            raw = properties.get(f'i32 {tag}', f'i32 {default}')
            if not re.fullmatch(r'i32 \d+', raw): raise ShaderGraphError('Invalid DXR entry integer property')
            return int(raw[4:])
        stage = _STAGES.get(integer(8))
        if stage is None: continue  # A library can also expose ordinary helper functions.
        linkage = function[1].strip('"').replace('\\01','\x01')
        reflected = next((item for item in native.values() if item.linkage_name.lstrip('\x01') == linkage.lstrip('\x01')),None)
        if reflected is None or reflected.stage != stage: raise ShaderGraphError('DXR metadata and native export reflection disagree')
        roles = {'closesthit':('payload','attributes'), 'anyhit':('payload','attributes'),
                 'miss':('payload',), 'callable':('callable_data',)}.get(stage, ())
        arguments = functions[function[1]]
        if len(arguments) != len(roles): raise ShaderGraphError('DXR export has an invalid native parameter count')
        values = {}
        for role, argument in zip(roles, arguments, strict=True):
            match = re.match(r'(' + _TOKEN + r'|float|i32|i1)\*\s', argument)
            if not match: raise ShaderGraphError('DXR export parameter is not a declared typed pointer')
            values[role] = value_type(match[1])
        result[reflected.name] = RayExportAbi(reflected.name, stage, **values,
            payload_size=integer(6), attribute_size=integer(7))
    if set(result) != {name for name,item in native.items() if item.stage in _STAGES.values()}:
        raise ShaderGraphError('DXR entry metadata does not cover the reflected exports')
    intrinsic_values = {name:[] for name in ('traceRay','reportHit','callShader')}
    for line in lines:
        match = re.match(r'declare (?:void|i1) @dx\.op\.(traceRay|reportHit|callShader)[^(]*\((.*)\)',line)
        if not match: continue
        parameters = metadata_operands('!{'+match[2]+'}')
        if not parameters or not parameters[-1].endswith('*'):
            raise ShaderGraphError('DXR operation lacks its declared payload/attribute pointer')
        shape = value_type(parameters[-1][:-1])
        if shape not in intrinsic_values[match[1]]: intrinsic_values[match[1]].append(shape)
    return RayLibraryAbi(result,tuple(intrinsic_values['traceRay']),tuple(intrinsic_values['reportHit']),
                         tuple(intrinsic_values['callShader']))


def validate_raytracing_replacement(reference, candidate):
    if reference.export_layout != candidate.export_layout:
        raise ShaderGraphError('DXR replacement cannot change the runtime export layout')
    original = validate_raytracing_library(reference, reflect=True)
    rebuilt = validate_raytracing_library(candidate, reflect=True)
    report = validate_shader_contract(ShaderContract.from_shader(original), ShaderContract.from_shader(rebuilt))
    errors = [issue.message for issue in report.issues if issue.severity == 'error']
    if errors: raise ShaderGraphError('\n'.join(errors))
    before_abi, after_abi = library_abi(reference, original), library_abi(candidate, rebuilt)
    before, after = before_abi.exports, after_abi.exports
    for role in raytracing_export_fields(reference.export_layout):
        old_name, new_name = getattr(reference,role), getattr(candidate,role)
        if bool(old_name) != bool(new_name):
            raise ShaderGraphError(f'DXR replacement cannot change the runtime-bound {role} role')
        if not old_name: continue
        old, new = before[old_name], after[new_name]
        for field in ('stage','payload','attributes','payload_size','attribute_size'):
            if getattr(old,field) != getattr(new,field):
                raise ShaderGraphError(f'DXR {role} replacement changes its native {field} contract')
    for field in ('payload_size','attribute_size'):
        limit = max((getattr(item,field) for item in before.values()),default=0)
        if any(getattr(item,field)>limit for item in after.values()):
            raise ShaderGraphError(f'DXR replacement exceeds the existing library {field} limit')
    for field,parameter in (('trace_payloads','payload'),('reported_attributes','attributes'),('callable_arguments','callable_data')):
        declared = (*getattr(before_abi,field),*(getattr(export,parameter) for export in before.values()))
        if any(shape not in declared for shape in getattr(after_abi,field)):
            raise ShaderGraphError(f'DXR replacement introduces a {field} type outside the existing runtime ABI')
    return report
