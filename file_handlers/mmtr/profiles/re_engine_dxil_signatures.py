from dataclasses import dataclass
import re

from file_handlers.sdf.shader_graph import ShaderGraphError


from file_handlers.sdf.dxil_metadata import metadata_definitions
lazy from file_handlers.sdf.dxil_metadata import native_float_literal
lazy from file_handlers.sdf.shader_stage_execution import ShaderMeshExecution, ShaderPrimitiveExecution
lazy from file_handlers.sdf.shader_storage import native_storage_type


_STORAGE_TYPE = re.compile(r'(%[-\w.$]+) = type (.+)')
_MESH_PAYLOAD = re.compile(r'(?:call|declare) (%[-\w.$]+)\* @dx\.op\.getMeshPayload\.')
_DISPATCH_PAYLOAD = re.compile(r'@dx\.op\.dispatchMesh\.[^(]+\(.*?, (%[-\w.$]+) addrspace\(3\)\*')
_INTERPOLATION_MODES = ("", "constant", "linear", "linear centroid", "linear noperspective",
                        "linear noperspective centroid", "linear sample", "linear noperspective sample")
_COMPONENT_CODES = {"float32": 9, "uint32": 5, "sint32": 4}


@dataclass(frozen=True)
class DxilEntryMetadata:
    reference: str
    operands: tuple[str, ...]
    definitions: dict[str, tuple[str, ...]]
    storage_types: dict[str, str]
    payload_type: str

    @classmethod
    def parse_entries(cls, lines: list[str]) -> tuple["DxilEntryMetadata", ...]:
        definitions = metadata_definitions(lines)
        entries = definitions.get("!dx.entryPoints", ())
        if not entries or any(len(definitions.get(entry, ())) != 5 for entry in entries):
            raise ShaderGraphError("DXIL has invalid entry metadata records.")
        types = {match[1]:match[2] for line in lines
                 if line.startswith('%') and (match := _STORAGE_TYPE.fullmatch(line))}
        payloads = set()
        for line in lines:
            if '@dx.op.getMeshPayload.' in line and (match := _MESH_PAYLOAD.search(line)):
                payloads.add(match[1])
            if '@dx.op.dispatchMesh.' in line and (match := _DISPATCH_PAYLOAD.search(line)):
                payloads.add(match[1])
        if len(payloads) > 1:
            raise ShaderGraphError('One mesh/amplification entry cannot declare conflicting payload types.')
        payload = next(iter(payloads), '')
        return tuple(cls(entry, definitions[entry], definitions, types, payload) for entry in entries)

    @classmethod
    def parse(cls, lines: list[str]) -> "DxilEntryMetadata":
        entries = cls.parse_entries(lines)
        if len(entries) != 1:
            raise ShaderGraphError("Material DXIL needs one entry metadata record.")
        return entries[0]

    @property
    def function_name(self) -> str:
        return self.function_reference_name(self.operands[0])

    @staticmethod
    def function_reference_name(operand):
        match = re.search(r'@(?P<name>"(?:[^"\\]|\\.)*"|[-$._A-Za-z0-9]+)', operand)
        if match is None:
            raise ShaderGraphError("Recovered DXIL entry function is invalid.")
        name = match['name']
        return name[1:-1] if name.startswith('"') else name

    def hull_patch_function_name(self):
        if self.stage != 'hull':
            raise ShaderGraphError('Only a hull entry has a patch-constant function.')
        reference = self._stage_state(3,7)[0]
        if not reference.startswith('void ()* @'):
            raise ShaderGraphError('The hull patch-constant entry must be void with no formal arguments.')
        return self.function_reference_name(reference)

    @property
    def stage(self) -> str:
        references = self.definitions.get("!dx.shaderModel", ())
        model = self.definitions.get(references[0], ()) if len(references) == 1 else ()
        stages = {'!"vs"': "vertex", '!"ps"': "pixel", '!"cs"': "compute", '!"gs"': "geometry",
                  '!"hs"': "hull", '!"ds"': "domain", '!"ms"': "mesh", '!"as"': "amplification"}
        if (len(model) != 3 or model[0] not in stages
                or any(not re.fullmatch(r"i32 \d+", value) for value in model[1:])):
            raise ShaderGraphError("DXIL entry shader model is invalid.")
        return stages[model[0]]

    def _signature_rows(self, list_index, *, streams=False):
        signature = self.definitions.get(self.operands[2], ())
        if not signature:
            return {}
        if len(signature) != 3:
            raise ShaderGraphError("DXIL entry signature must have input/output/patch lists.")
        result = {}
        for reference in self.definitions.get(signature[list_index], ()):
            row = self.definitions.get(reference, ())
            if len(row) != 11 or not re.fullmatch(r'i32 \d+', row[0]) or not re.fullmatch(r'!"[\w.]+"', row[1]):
                raise ShaderGraphError("DXIL signature row has an unsupported schema.")
            identity, name = int(row[0][4:]), row[1][2:-1].casefold()
            if not re.fullmatch(r"i8 [0-7]", row[5]):
                raise ShaderGraphError("DXIL signature interpolation mode is unsupported.")
            mode = _INTERPOLATION_MODES[int(row[5][3:])]
            extensions = self.definitions.get(row[10],())
            if len(extensions)%2:
                raise ShaderGraphError('DXIL signature properties must be tag/value pairs.')
            stream_values = [value for tag,value in zip(extensions[::2],extensions[1::2]) if tag=='i32 0']
            if len(stream_values)>1 or stream_values and not re.fullmatch(r'i32 [0-3]',stream_values[0]):
                raise ShaderGraphError('DXIL signature output stream is invalid.')
            stream = int(stream_values[0][4:]) if stream_values else 0
            if stream and list_index != 1:
                raise ShaderGraphError('Only output signatures declare geometry streams.')
            indices = self.definitions.get(row[4], ())
            if row[6] != f"i32 {len(indices)}":
                raise ShaderGraphError("DXIL signature row count disagrees with semantic indices.")
            for offset, index in enumerate(indices):
                if not re.fullmatch(r"i32 \d+", index):
                    raise ShaderGraphError("DXIL signature semantic index is not an integer.")
                key = (stream,name,int(index[4:])) if streams else (name, int(index[4:]))
                if key in result:
                    raise ShaderGraphError("DXIL signature semantic row is ambiguous.")
                result[key] = (identity, offset, mode)
        return result

    def input_rows(self) -> dict[tuple[str, int], tuple[int, int]]:
        return {key: value[:2] for key, value in self._signature_rows(0).items()}

    def patch_rows(self):
        return {key:value[:2] for key,value in self._signature_rows(2).items()}

    def output_rows(self, *, streams=False):
        return {key: value[:2] for key, value in self._signature_rows(1,streams=streams).items()}

    def input_interpolation(self) -> dict[tuple[str, int], str]:
        return {key: value[2] for key, value in self._signature_rows(0).items()}

    def output_interpolation(self, *, streams=False):
        return {key: value[2] for key, value in self._signature_rows(1,streams=streams).items()}

    def patch_interpolation(self):
        return {key: value[2] for key, value in self._signature_rows(2).items()}

    def _properties(self, tag: int) -> list[str]:
        properties = self.definitions.get(self.operands[4], ())
        if len(properties) % 2:
            raise ShaderGraphError("DXIL entry properties must be tag/value pairs.")
        return [value for key, value in zip(properties[::2], properties[1::2]) if key == f"i32 {tag}"]

    def shader_flags(self) -> int:
        flags = self._properties(0)
        if not flags:
            return 0
        if len(flags) != 1 or not re.fullmatch(r"i64 \d+", flags[0]):
            raise ShaderGraphError("DXIL entry shader flags are invalid.")
        value = int(flags[0][4:])
        if value >= 1 << 64:
            raise ShaderGraphError("DXIL entry shader flags exceed their declared width.")
        return value

    def thread_group_size(self) -> tuple[int, int, int] | None:
        groups = self._properties(4)
        if self.stage in {'mesh', 'amplification'}:
            state = self._stage_state(9, 5) if self.stage == 'mesh' else self._stage_state(10, 2)
            if groups:
                raise ShaderGraphError('Mesh/amplification threads belong to their stage state, not NumThreads tag 4.')
            groups = [state[0]]
        if not groups:
            return None
        dimensions = self.definitions.get(groups[0], ())
        if len(groups) != 1 or len(dimensions) != 3 or any(
            not re.fullmatch(r"i32 [1-9][0-9]*", item) for item in dimensions
        ):
            raise ShaderGraphError("DXIL NumThreads declaration is invalid.")
        return tuple(int(item[4:]) for item in dimensions)

    def mesh_execution(self):
        if self.stage not in {'mesh', 'amplification'}:
            return None
        state = self._stage_state(9, 5) if self.stage == 'mesh' else self._stage_state(10, 2)
        if any(not re.fullmatch(r'i32 \d+', item) for item in state[1:]):
            raise ShaderGraphError('DXIL mesh/amplification execution state is invalid.')
        numbers = tuple(int(item[4:]) for item in state[1:])
        layout = native_storage_type(self.payload_type,self.storage_types) if self.payload_type else None
        result = (ShaderMeshExecution(numbers[0], numbers[1], {1:'line', 2:'triangle'}.get(numbers[2], ''), numbers[3], layout)
                  if self.stage == 'mesh' else ShaderMeshExecution(payload_bytes=numbers[0],payload_layout=layout))
        result.validate_stage(self.stage)
        return result

    def _stage_state(self,tag,size):
        states = [self.definitions.get(value, ()) for value in self._properties(tag)]
        if len(states)!=1 or len(states[0])!=size:
            raise ShaderGraphError('Native primitive/control-point declaration is invalid')
        return states[0]

    def geometry_execution(self):
        state = self._stage_state(1,5)
        if self.stage!='geometry' or any(not re.fullmatch(r'i32 \d+',item) for item in state):
            raise ShaderGraphError('DXIL geometry execution state is invalid.')
        primitive,vertices,mask,topology,instances = (int(item[4:]) for item in state)
        if not 1<=mask<=15:
            raise ShaderGraphError('DXIL geometry active-stream mask is invalid.')
        result = ShaderPrimitiveExecution(input_primitive={1:'point',2:'line',3:'triangle',6:'lineadj',7:'triangleadj'}.get(primitive,''),
            max_output_vertices=vertices,output_streams=tuple(i for i in range(4) if mask&(1<<i)),
            output_topology={1:'pointlist',3:'linestrip',5:'trianglestrip'}.get(topology,''),geometry_instances=instances)
        result.validate_stage('geometry')
        return result

    def primitive_execution(self):
        if self.stage=='geometry':
            return self.geometry_execution()
        if self.stage=='hull':
            state = self._stage_state(3,7)
            if any(not re.fullmatch(r'i32 \d+',value) for value in state[1:6]) or not state[6].startswith('float '):
                raise ShaderGraphError('DXIL hull execution state is invalid.')
            result = ShaderPrimitiveExecution(input_control_points=int(state[1][4:]), output_control_points=int(state[2][4:]),
                domain={1:'isoline',2:'tri',3:'quad'}.get(int(state[3][4:]),''),
                partitioning={1:'integer',2:'pow2',3:'fractional_odd',4:'fractional_even'}.get(int(state[4][4:]),''),
                tessellation_primitive={1:'point',2:'line',3:'triangle_cw',4:'triangle_ccw'}.get(int(state[5][4:]),''),
                max_tessellation_factor=native_float_literal(state[6][6:]))
            result.validate_stage('hull')
            return result
        if self.stage=='domain':
            state = self._stage_state(2,2)
            if any(not re.fullmatch(r'i32 \d+',value) for value in state):
                raise ShaderGraphError('DXIL domain execution state is invalid.')
            result = ShaderPrimitiveExecution(domain={1:'isoline',2:'tri',3:'quad'}.get(int(state[0][4:]),''),
                input_control_points=int(state[1][4:]))
            result.validate_stage('domain')
            return result
        return None

    def geometry_stream_declaration(self, lines, streams):
        actual = self.geometry_execution().output_streams
        wanted = tuple(streams)
        removed = set(actual)-set(wanted)
        if wanted == actual:
            return lines
        if not set(wanted)<=set(actual) or not wanted or any(key[0] in removed for key in self.output_rows(streams=True)):
            raise ShaderGraphError('Geometry compilation changed a live output stream.')
        for line in lines:
            match = re.search(r'@dx\.op\.(?:emitStream|cutStream|emitThenCutStream)\(i32 \d+, i8 (\d+)\)',line)
            if match and int(match[1]) in removed:
                raise ShaderGraphError('Geometry compilation emits into an undeclared stream.')
        reference = self._properties(1)[0]
        state = list(self._stage_state(1,5))
        state[2] = f'i32 {sum(1<<stream for stream in wanted)}'
        replacement = reference + ' = !{' + ', '.join(state) + '}'
        return [replacement if line.startswith(reference+' = ') else line for line in lines]

    def primitive_input_count(self, stage):
        tag, count_index, size = {"geometry": (1, 0, 5), "domain": (2, 1, 2), "hull": (3, 1, 7)}[stage]
        state = self._stage_state(tag,size)
        if not re.fullmatch(r"i32 \d+", state[count_index]):
            raise ShaderGraphError("Native primitive/control-point declaration is invalid")
        value = int(state[count_index][4:])
        if stage == "geometry":
            # D3D_PRIMITIVE: point, line, triangle, line-adj, triangle-adj.
            value = {1: 1, 2: 2, 3: 3, 6: 4, 7: 6}.get(value, 0)
        if not 1 <= value <= 32:
            raise ShaderGraphError("Unsupported native primitive/control-point count")
        return value


def with_signature_rows(lines: list[str], metadata: DxilEntryMetadata, *, inputs=(), outputs=()) -> list[str]:
    signature_reference = metadata.operands[2]
    signature = list(metadata.definitions.get(signature_reference, ()))
    if len(signature) != 3:
        raise ShaderGraphError("DXIL entry signature must have input/output/patch lists.")
    next_id = 1 + max(int(match[1]) for line in lines if (match := re.match(r"!(\d+) = ", line)))
    added, replaced = [], {}
    for list_index, fields in ((0, inputs), (1, outputs)):
        if not fields:
            continue
        reference = signature[list_index]
        rows = list(metadata.definitions.get(reference, ())) if reference != "null" else []
        for item in fields:
            if item.system_value not in {"", "undefined"} or item.component_type not in _COMPONENT_CODES:
                raise ShaderGraphError(f"REasy cannot add {item.semantic_name} to a DXIL signature.")
            usage = item.mask if list_index == 1 else item.read_write_mask
            mode = item.interpolation or ("linear" if item.component_type == "float32" else "constant")
            lowest = (item.mask & -item.mask).bit_length() - 1
            indices, row, properties = f"!{next_id}", f"!{next_id + 1}", f"!{next_id + 2}" if usage else "null"
            next_id += 3
            added.extend((f"{indices} = !{{i32 {item.semantic_index}}}",
                          *((f"{properties} = !{{i32 3, i32 {usage}}}",) if usage else ()),
                          f'{row} = !{{i32 {len(rows)}, !"{item.semantic_name}", i8 '
                          f'{_COMPONENT_CODES[item.component_type]}, i8 0, {indices}, i8 '
                          f'{_INTERPOLATION_MODES.index(mode)}, i32 1, i8 {item.mask.bit_count()}, i32 '
                          f'{item.register}, i8 {lowest}, {properties}}}'))
            rows.append(row)
        if reference == "null":
            reference = signature[list_index] = f"!{next_id}"
            next_id += 1
            added.append(f"{reference} = !{{{', '.join(rows)}}}")
        else:
            replaced[reference] = f"{reference} = !{{{', '.join(rows)}}}"
    replaced[signature_reference] = f"{signature_reference} = !{{{', '.join(signature)}}}"
    return [replaced.get(line.split(" = ", 1)[0], line) for line in lines] + added


def map_carrier_inputs(lines: list[str], source: DxilEntryMetadata, carrier: DxilEntryMetadata, indexed_reads=()) -> list[str]:
    source_rows = source.input_rows()
    indexed = {read.carrier.key: read for read in indexed_reads}
    mapping = {}
    for semantic, location in carrier.input_rows().items():
        read = indexed.get(semantic)
        semantic = read.native.key if read else semantic
        if semantic not in source_rows:
            raise ShaderGraphError(f"Carrier requests absent DXIL semantic {semantic!r}.")
        mapping[location] = (*source_rows[semantic], read.element if read else None)
    operation = re.compile(
        r"(@dx\.op\.(?:loadInput|evalCentroid|evalSampleIndex|evalSnapped)\.[\w]+"
        r"\(i32 \d+, i32 )(\d+)(, i32 )(\d+)(, i8 \d+, i32 [\w%.-]+)?"
    )

    def replace(match):
        location = (int(match[2]), int(match[4]))
        if location not in mapping:
            raise ShaderGraphError("Carrier input operation has no native semantic row.")
        identity, row, element = mapping[location]
        tail = match[5] or ""
        if element is not None:
            if not tail or "loadInput" not in match[1]:
                raise ShaderGraphError("Indexed primitive input must be a native LoadInput operation")
            tail = re.sub(r"i32 [\w%.-]+$", f"i32 {element}", tail)
        return f"{match[1]}{identity}{match[3]}{row}{tail}"

    return [operation.sub(replace, line) for line in lines]
