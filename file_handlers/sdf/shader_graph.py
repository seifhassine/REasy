from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
import bisect
import hashlib
import importlib
import json
import math
import re
from typing import Any, Callable, Hashable
from uuid import uuid4

from ..material_names import resolve_material_name
from .dxbc import DxbcShader, parse_dxbc
from .graph_snapshot import IMMUTABLE_JSON, _INVALID, _checked_properties
from .models import shader_target
from .shader_graph_math import (BASIC_MATH_KINDS, UNARY_FUNCTIONS, unary_expression, comparison_expression, remap_expression, NUMERIC_VALUE_TYPES,
    TYPED_MATH_NODES, typed_math_signature, typed_math_expression, typed_math_helpers, math_context_error, value_width)
from .shader_graph_texture import (TEXTURE_COORDINATE_TYPES, TEXTURE_OBJECT_TYPES, TEXTURE_SPATIAL_WIDTHS,
    TextureOperation, texture_object_type, texture_result_pins)
from .shader_graph_buffer import BufferRead, structured_buffer_type, structured_storage
from .shader_stage_execution import ShaderPrimitiveExecution, ShaderMeshExecution
from .spirv_execution import ShaderVulkanExecution


GRAPH_SCHEMA = "reasy.shader-graph"
GRAPH_SCHEMA_VERSION = 1
SHADER_GRAPH_STAGES = frozenset({'vertex','pixel','compute','geometry','domain','hull','mesh','amplification'})
MAX_GRAPH_NODES = 32768
MAX_GRAPH_LINKS = 131072
# Editable DD2 skin regions occupy ~35 MiB.
MAX_PROJECT_LENGTH = 64 * 1024 * 1024
MAX_SOURCE_LENGTH = MAX_PROJECT_LENGTH

_FLOAT_TYPES = ("float", "float2", "float3", "float4")
_VALUE_TYPES = frozenset(NUMERIC_VALUE_TYPES)
_ORDERED_INPUT_KINDS = frozenset({'branch', 'repeat', 'indexed_select', 'array_read', 'constant_table'})
# Selections are computed once into a variable; inlined, DXC duplicates the switch per use.
_MATERIALIZED_EXTENSIONS = frozenset({'indexed_select', 'array_read', 'constant_table'})
_NODE_KINDS = frozenset(
    {
        "input",
        "constant",
        "parameter",
        "structured_parameter",
        "texture_sample",
        "shader_resource",
        "material_parameter",
        "material_texture",
        "material_surface",
        "material_function",
        "function_input",
        "function_output",
        "texture_coordinates",
        "sample_texture",
        "texture_load",
        "buffer_load",
        *BASIC_MATH_KINDS,
        "output",
    }
).union(UNARY_FUNCTIONS, TYPED_MATH_NODES, ("multiply_add", "compare", "select"))
_IDENTIFIER = re.compile(r"[^A-Za-z0-9_]")
HLSL_RESERVED_WORDS = frozenset("""
AppendStructuredBuffer BlendState Buffer ByteAddressBuffer ConstantBuffer ConsumeStructuredBuffer
DepthStencilState DepthStencilView InputPatch LineStream OutputPatch PixelShader PointStream
RasterizerState RenderTargetView RWBuffer RWByteAddressBuffer RWStructuredBuffer RWTexture1D
RWTexture1DArray RWTexture2D RWTexture2DArray RWTexture3D SamplerComparisonState SamplerState
StructuredBuffer Texture1D Texture1DArray Texture2D Texture2DArray Texture2DMS Texture2DMSArray
Texture3D TextureCube TextureCubeArray TriangleStream VertexShader asm asm_fragment auto bool
break case catch cbuffer centroid char class column_major compile compile_fragment const
const_cast continue default delete discard do double dword dynamic_cast else enum explicit export
extern false float for friend fxgroup goto groupshared half if in inline inout int interface line
lineadj linear long matrix min10float min12int min16float min16int min16uint mutable namespace new
nointerpolation noperspective operator out packoffset pass pixelfragment point precise private
protected public register reinterpret_cast return row_major sample sampler sampler1D sampler2D
sampler3D samplerCUBE shared short signed sizeof snorm stateblock stateblock_state static
static_cast string struct switch tbuffer technique technique10 technique11 template texture
texture1D texture2D texture3D textureCUBE this throw triangle triangleadj true try typedef typename
uint uniform union unorm unsigned using vector vertexfragment virtual void volatile while
""".split())


class ShaderGraphError(ValueError):
    pass


def _identifier(value: str, *, prefix: str = "value") -> str:
    result = _IDENTIFIER.sub("_", str(value or ""))
    if not result:
        result = prefix
    if result[0].isdigit():
        result = f"_{result}"
    if result in HLSL_RESERVED_WORDS:
        result += "_"
    return result


@dataclass(frozen=True, slots=True)
class ShaderExpression:
    value: str
    value_type: str
    dependencies: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ShaderNodeEvaluation:
    value: str
    value_type: str
    members: tuple[tuple[str, str], ...]
    direct: tuple[str, ...] = ()
    forwarded: tuple[tuple[str, str], ...] = ()


class ShaderExpressionCompiler:
    def __init__(
        self,
        *,
        variable_prefix: str = "reasyGraph",
        define_unassigned: bool = False,
    ) -> None:
        # FXC rejects any read of an uninitialized temp, so SM5 targets refine unspecified values to zero.
        self.define_unassigned = bool(define_unassigned)
        self.shared_scope = "shared"
        self.variable_prefix = _identifier(variable_prefix, prefix="graphValue")
        self._statements: dict[str, list[str]] = {}
        self._shared: dict[Hashable, ShaderExpression] = {}
        self._scoped: dict[tuple[str, Hashable], ShaderExpression] = {}
        self._counts: dict[str, int] = {}

    def cached(self, key: Hashable, scope: str) -> ShaderExpression | None:
        return self._shared.get(key) or self._scoped.get((str(scope), key))

    def emit(
        self,
        key: Hashable,
        scope: str,
        expression: ShaderExpression,
        *,
        materialize: bool = True,
        initialize: bool = True,
        precise: bool = False,
    ) -> ShaderExpression:
        scope = str(scope)
        dependencies = expression.dependencies
        if dependencies and dependencies != frozenset((scope,)):
            raise ShaderGraphError(
                "shader expression mixes incompatible evaluation scopes"
            )

        bucket = scope if dependencies else self.shared_scope
        result = expression
        if materialize:
            index = self._counts.get(bucket, 0)
            self._counts[bucket] = index + 1
            suffix = _IDENTIFIER.sub("_", bucket) or "scope"
            name = f"{self.variable_prefix}{suffix.title()}{index}"
            if initialize:
                declaration = f" = {expression.value};"
            elif self.define_unassigned:
                declaration = f" = ({expression.value_type})0;"
            else:
                declaration = ";"
            self._statements.setdefault(bucket, []).append(
                ('precise ' if precise else '') + f"{expression.value_type} {name}" + declaration
            )
            result = ShaderExpression(name, expression.value_type, dependencies)

        if dependencies:
            self._scoped[(scope, key)] = result
        else:
            self._shared[key] = result
        return result

    def statements(self, scope: str) -> tuple[str, ...]:
        return tuple(self._statements.get(str(scope), ()))

    def append_statements(self, scope: str, statements) -> None:
        self._statements.setdefault(str(scope), []).extend(statements)


def _type_width(value_type: str) -> int:
    from .shader_graph_math import NUMERIC_BASE_TYPES
    match = re.fullmatch(r"(?:" + '|'.join(NUMERIC_BASE_TYPES) + r")([1-4]?)", str(value_type))
    if match is None:
        return 0
    return int(match.group(1) or 1)


def _type_base(value_type: str) -> str:
    return re.sub(r"[1-4]$", "", str(value_type))


def _signature_value_type(component_type: str, mask: int) -> str:
    base = {
        "float32": "float",
        "sint32": "int",
        "uint32": "uint",
    }.get(str(component_type), "float")
    width = max(1, min(4, int(mask).bit_count() or 4))
    return base if width == 1 else f"{base}{width}"


def _semantic_key(name: str, index: int) -> tuple[str, int]:
    return str(name).casefold(), int(index)


def _semantic_token(name: str, index: int) -> str:
    source = _identifier(str(name or "SEMANTIC"), prefix="SEMANTIC")
    if int(index) == 0 and source.casefold().startswith("sv_"):
        return source
    return f"{source}{int(index)}"


@dataclass(frozen=True, slots=True)
class ShaderSignatureField:
    semantic_name: str
    semantic_index: int
    register: int
    component_type: str
    mask: int
    read_write_mask: int
    system_value: str
    stream: int = 0
    array_length: int = 1
    interpolation: str = ""

    def __post_init__(self):
        if not isinstance(self.array_length, int) or not 1 <= self.array_length <= 65536:
            raise ShaderGraphError("Shader interface has an invalid array extent")
        if self.interpolation not in {"", "constant", "linear", "linear centroid", "linear noperspective",
                                      "linear noperspective centroid", "linear sample", "linear noperspective sample"}:
            raise ShaderGraphError("Shader interface has an invalid interpolation mode")

    @property
    def key(self) -> tuple[str, int]:
        return _semantic_key(self.semantic_name, self.semantic_index)

    @property
    def stream_key(self) -> tuple[int, str, int]:
        return (self.stream, *self.key)

    @property
    def primitive_attribute(self) -> bool:
        return self.register != 0xffffffff or self.system_value in {
            'position', 'point-size', 'clip-distance', 'cull-distance',
            'render-target-array-index', 'viewport-array-index',
        }

    @property
    def value_type(self) -> str:
        if self.system_value == 'cull-primitive':
            if self.component_type != 'uint32' or self.mask != 1:
                raise ShaderGraphError('Primitive culling requires its scalar Boolean system value.')
            return 'bool'
        return _signature_value_type(self.component_type, self.mask)

    @property
    def pin_name(self) -> str:
        return f"semantic:{self.semantic_name.casefold()}:{self.semantic_index}"

    @property
    def display_name(self) -> str:
        return _semantic_token(self.semantic_name, self.semantic_index)


@dataclass(frozen=True, slots=True)
class ShaderContractVariable:
    name: str
    byte_offset: int
    byte_size: int
    hlsl_declaration: str | None
    graph_type: str | None
    array_length: int = 1
    matrix: bool = False
    struct_name: str = ""
    members: tuple["ShaderContractVariable", ...] = ()


@dataclass(frozen=True, slots=True)
class ShaderContractBuffer:
    name: str
    byte_size: int
    bind_point: int
    bind_count: int
    variables: tuple[ShaderContractVariable, ...]
    register_space: int = 0


@dataclass(frozen=True, slots=True)
class ShaderContractResource:
    name: str
    input_type: str
    return_type: str
    dimension: str
    sample_count: int
    bind_point: int
    bind_count: int
    flags: int
    register_space: int = 0
    resource_id: int = 0

    @property
    def register_class(self) -> str:
        if self.input_type == "sampler":
            return "s"
        if self.input_type == "constant-buffer":
            return "b"
        if self.input_type.startswith("rw-") or self.input_type in {
            "append-structured-buffer",
            "consume-structured-buffer",
        }:
            return "u"
        return "t"

    @property
    def sampleable(self) -> bool:
        return bool(
            self.input_type == "texture"
            and self.dimension
            in {
                "texture1d",
                "texture1d-array",
                "texture2d",
                "texture2d-array",
                "texture3d",
                "texture-cube",
                "texture-cube-array",
            }
        )

    @property
    def coordinate_type(self) -> str:
        return TEXTURE_COORDINATE_TYPES.get(self.dimension.replace('-', ''), "float2")

    @property
    def texture_value_type(self) -> str | None:
        base = {'float': 'float', 'sint': 'int', 'uint': 'uint', 'sint64': 'int64_t', 'uint64': 'uint64_t'}.get(self.return_type)
        if self.input_type not in {'texture', 'rw-typed'} or self.dimension.replace('-', '') not in TEXTURE_SPATIAL_WIDTHS or base is None:
            return None
        # D3D_SIF_TEXTURE_COMPONENT_0/1 encode the declared vector width minus one.
        width = ((self.flags >> 2) & 3) + 1
        return base + (str(width) if width > 1 else '')

    @property
    def texture_samples(self) -> int:
        return self.sample_count if 'ms' in self.dimension and self.sample_count != 0xffffffff else 0

    @property
    def object_type(self) -> str:
        if self.input_type == 'constant-buffer' and self.sample_count:
            from .shader_graph_constant_buffer import constant_buffer_type
            return constant_buffer_type(self.sample_count)
        if self.input_type == "byte-address-buffer":
            return "byte_address_buffer"
        if self.input_type == 'rw-byte-address-buffer':
            return 'rw_byte_address_buffer'
        if self.input_type in {'structured-buffer', 'rw-structured-buffer'}:
            return structured_buffer_type(self.sample_count, writable=self.input_type == 'rw-structured-buffer')
        dimension = self.dimension.replace('-', '')
        if self.input_type == 'rw-typed':
            from .shader_graph_texture import WRITABLE_TEXTURE_DIMENSIONS
            if dimension not in WRITABLE_TEXTURE_DIMENSIONS or not self.texture_value_type:
                raise ShaderGraphError('This writable typed resource requires a different dimension adapter.')
            return 'rw_' + texture_object_type(dimension, self.texture_value_type)
        return texture_object_type(dimension, self.texture_value_type, self.texture_samples) if self.texture_value_type else dimension


def _dxbc_variable_declaration(variable_type) -> tuple[str | None, str | None, int, bool]:
    base = {
        "float": "float",
        "int": "int",
        "uint": "uint",
        "bool": "bool",
        "double": "double",
        "int64": "int64_t",
        "uint64": "uint64_t",
        "min8float": "min8float",
        "min10float": "min10float",
        "min16float": "min16float",
        "min12int": "min12int",
        "min16int": "min16int",
        "min16uint": "min16uint",
    }.get(str(variable_type.variable_type))
    array_length = max(1, int(variable_type.element_count or 0))
    variable_class = str(variable_type.variable_class)
    rows = max(1, int(variable_type.rows or 0))
    columns = max(1, int(variable_type.columns or 0))
    matrix = variable_class in {"matrix-rows", "matrix-columns"}
    if base is None or variable_class not in {
        "scalar",
        "vector",
        "matrix-rows",
        "matrix-columns",
    }:
        return None, None, array_length, matrix
    if variable_class == "scalar":
        declaration = base
        graph_type = base if base in {"float", "int", "uint"} else None
    elif variable_class == "vector":
        width = max(rows, columns)
        declaration = base if width == 1 else f"vector<{base}, {width}>" if base in {"int64_t", "uint64_t"} else f"{base}{width}"
        graph_type = declaration if declaration in _VALUE_TYPES else None
    else:
        orientation = "row_major" if variable_class == "matrix-rows" else "column_major"
        declaration = f"{orientation} {base}{rows}x{columns}"
        graph_type = None
    if array_length > 1:
        graph_type = None
    return declaration, graph_type, array_length, matrix


@dataclass(frozen=True, slots=True)
class ShaderContract:
    stage: str
    model_major: int
    model_minor: int
    inputs: tuple[ShaderSignatureField, ...]
    outputs: tuple[ShaderSignatureField, ...]
    resources: tuple[ShaderContractResource, ...]
    constant_buffers: tuple[ShaderContractBuffer, ...]
    source_sha256: str = ""
    structured_buffers: tuple[ShaderContractBuffer, ...] = ()
    patch_constants: tuple[ShaderSignatureField, ...] = ()
    thread_group_size: tuple[int, int, int] | None = None
    primitive_execution: ShaderPrimitiveExecution | None = None
    mesh_execution: 'ShaderMeshExecution | None' = None
    vulkan_execution: 'ShaderVulkanExecution | None' = None
    _fingerprint: str = field(default="", init=False, compare=False, repr=False)

    @classmethod
    def from_shader(cls, shader: DxbcShader) -> "ShaderContract":
        def signature(items) -> tuple[ShaderSignatureField, ...]:
            return tuple(
                ShaderSignatureField(
                    semantic_name=item.semantic_name,
                    semantic_index=item.semantic_index,
                    register=item.register,
                    component_type=item.component_type,
                    mask=item.mask,
                    read_write_mask=item.read_write_mask,
                    system_value=item.system_value,
                    stream=item.stream,
                )
                for item in items
            )

        def contract_variable(
            name: str,
            byte_offset: int,
            byte_size: int,
            variable_type,
        ) -> ShaderContractVariable:
            declaration, graph_type, array_length, matrix = (
                _dxbc_variable_declaration(variable_type)
            )
            struct_name = ""
            members: tuple[ShaderContractVariable, ...] = ()
            if str(variable_type.variable_class) == "struct":
                struct_name = _identifier(
                    str(variable_type.name or name),
                    prefix="REasyStruct",
                )
                declaration = struct_name
                graph_type = None
                matrix = False
                array_length = max(1, int(variable_type.element_count or 0))
                element_size = (
                    int(byte_size) // array_length
                    if int(byte_size) > 0 and int(byte_size) % array_length == 0
                    else int(byte_size)
                )
                ordered = sorted(
                    variable_type.members,
                    key=lambda item: int(item.byte_offset),
                )
                decoded_members = []
                for member_index, member in enumerate(ordered):
                    next_offset = (
                        int(ordered[member_index + 1].byte_offset)
                        if member_index + 1 < len(ordered)
                        else element_size
                    )
                    member_size = max(0, next_offset - int(member.byte_offset))
                    decoded_members.append(
                        contract_variable(
                            str(member.name),
                            int(member.byte_offset),
                            member_size,
                            member.variable_type,
                        )
                    )
                members = tuple(decoded_members)
            return ShaderContractVariable(
                name=str(name),
                byte_offset=int(byte_offset),
                byte_size=int(byte_size),
                hlsl_declaration=declaration,
                graph_type=graph_type,
                array_length=array_length,
                matrix=matrix,
                struct_name=struct_name,
                members=members,
            )

        reflection = shader.reflection
        resources = tuple(
            ShaderContractResource(
                name=item.name,
                input_type=item.input_type,
                return_type=item.return_type,
                dimension=item.dimension,
                sample_count=item.sample_count,
                bind_point=item.bind_point,
                bind_count=item.bind_count,
                flags=item.flags,
                register_space=item.register_space,
                resource_id=item.resource_id,
            )
            for item in (reflection.resources if reflection is not None else ())
        )
        buffers = []
        structured_buffers = []
        for buffer in reflection.constant_buffers if reflection is not None else ():
            binding = resolve_material_name(buffer.name, resources, key=_item_name)
            if binding is None:
                continue
            if binding.input_type in {"structured-buffer", "rw-structured-buffer", "rw-structured-buffer-counter"}:
                element = next(
                    (
                        variable
                        for variable in buffer.variables
                        if variable.variable_type.members
                    ),
                    None,
                )
                if element is None:
                    if len(buffer.variables) != 1:
                        continue
                    element = buffer.variables[0]
                    if element.variable_type.variable_class not in {
                        "scalar", "vector", "matrix-rows", "matrix-columns",
                    }:
                        continue
                members = []
                if not element.variable_type.members:
                    members.append(contract_variable(
                        element.name, element.start_offset, element.size, element.variable_type,
                    ))
                ordered = sorted(
                    element.variable_type.members,
                    key=lambda item: item.byte_offset,
                )
                for member_index, member in enumerate(ordered):
                    next_offset = (
                        ordered[member_index + 1].byte_offset
                        if member_index + 1 < len(ordered)
                        else binding.sample_count
                    )
                    byte_size = max(0, next_offset - member.byte_offset)
                    declaration, graph_type, _array_length, _matrix = (
                        _dxbc_variable_declaration(member.variable_type)
                    )
                    expected_width = _type_width(graph_type or "")
                    if expected_width:
                        byte_size = expected_width * 4
                    members.append(
                        contract_variable(
                            member.name,
                            member.byte_offset,
                            byte_size,
                            member.variable_type,
                        )
                    )
                structured_buffers.append(
                    ShaderContractBuffer(
                        name=buffer.name,
                        byte_size=binding.sample_count,
                        bind_point=binding.bind_point,
                        bind_count=binding.bind_count,
                        variables=tuple(members),
                        register_space=binding.register_space,
                    )
                )
                continue
            if binding.input_type != "constant-buffer":
                continue
            variables = []
            for variable in buffer.variables:
                variables.append(
                    contract_variable(
                        variable.name,
                        variable.start_offset,
                        variable.size,
                        variable.variable_type,
                    )
                )
            buffers.append(
                ShaderContractBuffer(
                    name=buffer.name,
                    byte_size=buffer.byte_size,
                    bind_point=binding.bind_point,
                    bind_count=binding.bind_count,
                    variables=tuple(variables),
                    register_space=binding.register_space,
                )
            )
        return cls(
            stage=shader.stage,
            model_major=shader.model_major,
            model_minor=shader.model_minor,
            inputs=signature(shader.input_signature),
            outputs=signature(shader.output_signature),
            resources=resources,
            constant_buffers=tuple(buffers),
            source_sha256=shader.sha256,
            structured_buffers=tuple(structured_buffers),
            patch_constants=signature(shader.patch_signature),
            thread_group_size=reflection.thread_group_size if reflection is not None and shader.stage == 'compute' else None,
        )

    @property
    def target(self) -> str:
        target = shader_target(self.stage, self.model_major, self.model_minor)
        if not target:
            raise ShaderGraphError(f"unsupported shader stage {self.stage!r}")
        return target

    @property
    def fingerprint(self) -> str:
        if self._fingerprint:
            return self._fingerprint
        payload = asdict(self)
        payload.pop("source_sha256")
        payload.pop("_fingerprint")
        for name in ('primitive_execution', 'mesh_execution', 'vulkan_execution'):
            if getattr(self, name) is None:
                payload.pop(name)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        fingerprint = hashlib.sha256(raw).hexdigest()
        object.__setattr__(self, "_fingerprint", fingerprint)
        return fingerprint

    def input(self, semantic_name: str, semantic_index: int) -> ShaderSignatureField | None:
        key = _semantic_key(semantic_name, semantic_index)
        return next((item for item in self.inputs if item.key == key), None)

    def buffer(self, name: str) -> ShaderContractBuffer | None:
        return resolve_material_name(name, self.constant_buffers, key=_item_name)

    def structured_buffer(self, name: str) -> ShaderContractBuffer | None:
        return resolve_material_name(name, self.structured_buffers, key=_item_name)

    def resource(self, name: str) -> ShaderContractResource | None:
        return resolve_material_name(name, self.resources, key=_item_name)


@dataclass(frozen=True, slots=True)
class ShaderContractIssue:
    severity: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ShaderContractReport:
    issues: tuple[ShaderContractIssue, ...]

    @property
    def errors(self) -> tuple[ShaderContractIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "error")

    @property
    def warnings(self) -> tuple[ShaderContractIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "warning")

    @property
    def compatible(self) -> bool:
        return not self.errors


def _compare_signature_field(
    reference: ShaderSignatureField,
    candidate: ShaderSignatureField,
    kind: str,
) -> list[ShaderContractIssue]:
    issues = []
    label = reference.display_name
    # Stage linkage is direct (oN.lane feeds vN.lane, D3D11 spec 4.4.3.2); live inputs keep their register and lanes.
    comparisons = [
        ("component type", reference.component_type, candidate.component_type),
        ("stream", reference.stream, candidate.stream),
        ("array length", reference.array_length, candidate.array_length),
    ]
    if kind == "input":
        comparisons.append(
            (
                "component count",
                reference.mask.bit_count(),
                candidate.mask.bit_count(),
            )
        )
    if kind != "input" or candidate.read_write_mask:
        comparisons.extend(
            (
                ("register", reference.register, candidate.register),
                ("component mask", reference.mask, candidate.mask),
            )
        )
    for field_name, expected, actual in comparisons:
        if expected != actual:
            issues.append(
                ShaderContractIssue(
                    "error",
                    f"{kind}-{field_name.replace(' ', '-')}",
                    f"{kind.title()} {label} changed {field_name} from {expected!r} to {actual!r}.",
                )
            )
    return issues


def validate_shader_contract(
    reference: ShaderContract,
    candidate: ShaderContract | DxbcShader | bytes,
) -> ShaderContractReport:
    if isinstance(candidate, (bytes, bytearray, memoryview)):
        bytecode = bytes(candidate)
        candidate = ShaderContract.from_shader(parse_dxbc(bytecode))
        if reference.primitive_execution is not None or reference.mesh_execution is not None:
            from file_handlers.mmtr.material_implementation import implementation_contract
            candidate = implementation_contract(candidate,bytecode)
        if candidate.stage == 'compute':
            if candidate.model_major >= 6:
                from .dxc import enrich_dxil_shader
                candidate = ShaderContract.from_shader(enrich_dxil_shader(parse_dxbc(bytecode), bytecode))
            else:
                from .hlsl import dxbc_thread_group_size
                candidate = replace(candidate, thread_group_size=dxbc_thread_group_size(bytecode))
    elif isinstance(candidate, DxbcShader):
        candidate = ShaderContract.from_shader(candidate)
    if not isinstance(candidate, ShaderContract):
        raise TypeError("candidate must be a shader contract, parsed DXBC, or DXBC bytes")

    issues: list[ShaderContractIssue] = []
    if reference.vulkan_execution is not None:
        from .spirv_implementation import validate_vulkan_linkage
        for message in validate_vulkan_linkage(reference.vulkan_execution, candidate.vulkan_execution):
            issues.append(ShaderContractIssue('error', 'vulkan-linkage', message))
    if reference.thread_group_size is not None and candidate.thread_group_size != reference.thread_group_size:
        issues.append(ShaderContractIssue('error', 'thread-group-size', 'Compiled thread-group dimensions differ from the renderer contract.'))
    if reference.primitive_execution is not None and candidate.primitive_execution != reference.primitive_execution:
        issues.append(ShaderContractIssue('error', 'primitive-execution', 'Compiled primitive execution differs from the renderer contract.'))
    if reference.mesh_execution is not None and candidate.mesh_execution != reference.mesh_execution:
        issues.append(ShaderContractIssue('error', 'mesh-execution', 'Compiled mesh/amplification execution differs from the renderer contract.'))
    if candidate.stage != reference.stage:
        issues.append(
            ShaderContractIssue(
                "error",
                "stage",
                f"Compiled stage is {candidate.stage}; selected pass expects {reference.stage}.",
            )
        )
    if (candidate.model_major, candidate.model_minor) != (
        reference.model_major,
        reference.model_minor,
    ):
        issues.append(
            ShaderContractIssue(
                "error",
                "shader-model",
                "Compiled shader model does not match the selected pass.",
            )
        )

    reference_inputs = {item.key: item for item in reference.inputs}
    candidate_inputs = {item.key: item for item in candidate.inputs}
    for key, item in candidate_inputs.items():
        expected = reference_inputs.get(key)
        if expected is None:
            issues.append(
                ShaderContractIssue(
                    "error",
                    "unexpected-input",
                    f"Shader requests input {item.display_name}, which the selected pass does not provide.",
                )
            )
        else:
            issues.extend(_compare_signature_field(expected, item, "input"))
    for key, item in reference_inputs.items():
        if key not in candidate_inputs:
            issues.append(
                ShaderContractIssue(
                    "info",
                    "unused-input",
                    f"Input {item.display_name} is no longer consumed.",
                )
            )

    reference_outputs = {item.stream_key: item for item in reference.outputs}
    candidate_outputs = {item.stream_key: item for item in candidate.outputs}
    for key, expected in reference_outputs.items():
        actual = candidate_outputs.get(key)
        if actual is None:
            issues.append(
                ShaderContractIssue(
                    "error",
                    "missing-output",
                    f"Required output {expected.display_name} is missing.",
                )
            )
        else:
            issues.extend(_compare_signature_field(expected, actual, "output"))
    for key, item in candidate_outputs.items():
        if key not in reference_outputs:
            issues.append(
                ShaderContractIssue(
                    "error",
                    "unexpected-output",
                    f"Shader writes output {item.display_name}, which is absent from the selected pass.",
                )
            )

    reference_patch = {item.key: item for item in reference.patch_constants}
    candidate_patch = {item.key: item for item in candidate.patch_constants}
    if reference_patch.keys() != candidate_patch.keys():
        issues.append(ShaderContractIssue("error", "patch-constants", "Tessellation patch-constant interface changed."))
    for key in reference_patch.keys() & candidate_patch.keys():
        issues.extend(_compare_signature_field(reference_patch[key], candidate_patch[key], "patch constant"))

    reference_resources = {
        (item.register_class, item.register_space, item.bind_point): item
        for item in reference.resources
    }
    candidate_resources = {
        (item.register_class, item.register_space, item.bind_point): item
        for item in candidate.resources
    }
    for slot, item in candidate_resources.items():
        expected = reference_resources.get(slot)
        register = f"{slot[0]}{slot[2]}"
        if slot[1]:
            register += f", space{slot[1]}"
        if expected is None:
            issues.append(
                ShaderContractIssue(
                    "error",
                    "unexpected-binding",
                    f"Shader requests {item.name} at {register}, which the pass never binds.",
                )
            )
            continue
        for field_name, wanted, actual in (
            ("resource class", expected.input_type, item.input_type),
            ("dimension", expected.dimension, item.dimension),
            ("array count", expected.bind_count, item.bind_count),
            ("return type", expected.return_type, item.return_type),
            ("sample count", expected.sample_count, item.sample_count),
        ):
            if wanted != actual:
                issues.append(
                    ShaderContractIssue(
                        "error",
                        "binding-contract",
                        f"Binding {register} changed {field_name} from {wanted!r} to {actual!r}.",
                    )
                )
        if (
            expected.input_type == "sampler"
            and bool(expected.flags & 0x2) != bool(item.flags & 0x2)
        ):
            issues.append(
                ShaderContractIssue(
                    "error",
                    "sampler-contract",
                    f"Binding {register} changed between regular and comparison sampling.",
                )
            )
        if expected.name.casefold() != item.name.casefold():
            issues.append(
                ShaderContractIssue(
                    "warning",
                    "binding-name",
                    f"Binding {register} is named {item.name}; the game metadata calls it {expected.name}.",
                )
            )
    for slot, item in reference_resources.items():
        if slot not in candidate_resources:
            issues.append(
                ShaderContractIssue(
                    "info",
                    "unused-binding",
                f"Binding {item.register_class}{item.bind_point}"
                f"{f', space{item.register_space}' if item.register_space else ''} "
                f"({item.name}) is no longer consumed.",
                )
            )

    reference_buffers = {
        (item.register_space, item.bind_point): item
        for item in reference.constant_buffers
    }
    candidate_buffers = {
        (item.register_space, item.bind_point): item
        for item in candidate.constant_buffers
    }

    def variable_shape(variable: ShaderContractVariable) -> tuple:
        return (
            int(variable.byte_offset),
            int(variable.byte_size),
            variable.hlsl_declaration,
            variable.graph_type,
            int(variable.array_length),
            bool(variable.matrix),
            str(variable.struct_name),
            tuple(
                (
                    member.name.casefold(),
                    variable_shape(member),
                )
                for member in variable.members
            ),
        )

    for binding_key, buffer in candidate_buffers.items():
        expected = reference_buffers.get(binding_key)
        if expected is None:
            continue
        register_space, bind_point = binding_key
        register = f"b{bind_point}"
        if register_space:
            register += f", space{register_space}"
        if buffer.byte_size > expected.byte_size:
            issues.append(
                ShaderContractIssue(
                    "error",
                    "constant-buffer-size",
                    f"{register} reads {buffer.byte_size} bytes; the pass supplies only {expected.byte_size}.",
                )
            )
        expected_ranges = {}
        for item in expected.variables:
            expected_ranges.setdefault((int(item.byte_offset), int(item.byte_size)), []).append(item)
        for variable in buffer.variables:
            start, stop = variable.byte_offset, variable.byte_offset + variable.byte_size
            if start < 0 or stop > expected.byte_size:
                issues.append(
                    ShaderContractIssue(
                        "error",
                        "constant-buffer-layout",
                        f"{register} variable {variable.name} reads bytes {start}..{stop - 1} outside the supplied buffer.",
                    )
                )
                continue
            if variable.name.startswith(("REasyOpaque_", "REasyReserved_")):
                continue
            physical_view = re.fullmatch(r"REasyCB(\d+)", variable.name)
            if physical_view is not None:
                registers = (int(expected.byte_size) + 15) // 16
                valid_physical_view = (
                    int(physical_view.group(1)) == int(bind_point)
                    and int(expected.byte_size) > 0
                    and int(expected.byte_size) % 16 == 0
                    and int(variable.byte_offset) == 0
                    and int(variable.byte_size) == int(expected.byte_size)
                    and variable.hlsl_declaration in {"uint4", "float4"}
                    and (
                        variable.graph_type is None
                        or (registers == 1 and variable.graph_type == variable.hlsl_declaration)
                    )
                    and int(variable.array_length) == registers
                    and not variable.matrix
                    and not variable.struct_name
                    and not variable.members
                )
                if not valid_physical_view:
                    issues.append(
                        ShaderContractIssue(
                            "error",
                            "constant-buffer-physical-register-view",
                            f"{register} variable {variable.name} is not an exact typed "
                            "32-bit register view of the supplied buffer "
                            f"(offset={variable.byte_offset}, size={variable.byte_size}, "
                            f"declaration={variable.hlsl_declaration!r}, "
                            f"array_length={variable.array_length}, "
                            f"expected_size={expected.byte_size}, "
                            f"expected_registers={registers}).",
                        )
                    )
                continue
            exact = expected_ranges.get((int(variable.byte_offset), int(variable.byte_size)), ())
            named = [
                item
                for item in exact
                if item.name.casefold() == variable.name.casefold()
            ]
            match = named[0] if len(named) == 1 else exact[0] if len(exact) == 1 else None
            if match is None:
                issues.append(
                    ShaderContractIssue(
                        "error",
                        "constant-buffer-reinterpretation",
                        f"{register} variable {variable.name} does not match one semantic field at bytes {start}..{stop - 1}.",
                    )
                )
                continue
            if variable_shape(variable) != variable_shape(match):
                issues.append(
                    ShaderContractIssue(
                        "error",
                        "constant-buffer-variable-contract",
                        f"{register} variable {variable.name} changed its scalar, array, matrix, or nested-structure layout.",
                    )
                )
            elif variable.name.casefold() != match.name.casefold():
                issues.append(
                    ShaderContractIssue(
                        "warning",
                        "constant-buffer-variable-name",
                        f"{register} variable {variable.name} is named {match.name} by the native contract.",
                    )
                )

    if not any(item.severity in {"error", "warning"} for item in issues):
        issues.insert(
            0,
            ShaderContractIssue(
                "info",
                "compatible",
                "Compiled DXBC matches the selected pass contract.",
            ),
        )
    return ShaderContractReport(tuple(issues))


@dataclass(slots=True)
class ShaderGraphNode:
    node_id: str
    kind: str
    x: float = 0.0
    y: float = 0.0
    properties: dict[str, Any] = field(default_factory=dict)

    def __deepcopy__(self, memo):
        result = type(self)(self.node_id, self.kind, self.x, self.y)
        memo[id(self)] = result
        pending = []

        def clone(value):
            if id(value) in memo:
                return memo[id(value)]
            if type(value) in (dict, list):
                copied = {} if isinstance(value, dict) else []
                memo[id(value)] = copied
                pending.append((value, copied))
                return copied
            return deepcopy(value, memo)

        result.properties = clone(self.properties)
        while pending:
            original, copied = pending.pop()
            if isinstance(original, dict):
                copied.update((deepcopy(key, memo), clone(value)) for key, value in original.items())
            else:
                copied.extend(clone(value) for value in original)
        return result


@dataclass(frozen=True, slots=True)
class ShaderGraphLink:
    source_node: str
    source_pin: str
    target_node: str
    target_pin: str


@dataclass(frozen=True, slots=True)
class ShaderGraphDiagnostic:
    severity: str
    code: str
    message: str
    node_id: str = ""


@dataclass(frozen=True, slots=True)
class ShaderPin:
    name: str
    label: str
    value_type: str
    direction: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class ShaderGraphNodeExtension:
    kind: str
    title: str
    pins_provider: Callable[
        [ShaderGraphNode, ShaderContract],
        tuple[ShaderPin, ...],
    ]
    expression: Callable[
        [ShaderGraphNode, str, Callable[[str, str], str]],
        str,
    ] | None = None
    hlsl_helpers_provider: Callable[
        [ShaderGraphNode, ShaderContract],
        str,
    ] | None = None
    required_buffers_provider: Callable[
        [ShaderGraphNode, ShaderContract],
        tuple[str, ...],
    ] | None = None
    description: str = ""
    evaluation: Callable[[ShaderGraphNode, Callable[[str, str], str]], ShaderNodeEvaluation] | None = None
    entry_parameters_provider: Callable[[ShaderGraphNode, ShaderContract], tuple[tuple[str, str, str], ...]] | None = None


_NODE_EXTENSIONS: dict[str, ShaderGraphNodeExtension] = {}
EXTENSION_MODULES = {
    **dict.fromkeys(('domain_coordinates', 'patch_input', 'output_control_point'),
                    ('file_handlers.sdf.shader_graph_tessellation', 'file_handlers.sdf.shader_graph_hull')),
    **dict.fromkeys(('constant_buffer_load', 'constant_buffer_row', 'parameter_element'),
                    ('file_handlers.sdf.shader_graph_constant_buffer',)),
    **dict.fromkeys(('primitive_input', 'emit_vertex', 'end_primitive'), ('file_handlers.sdf.shader_graph_geometry',)),
    **dict.fromkeys(('mesh_output_counts', 'mesh_indices', 'mesh_vertex', 'mesh_primitive'),
                    ('file_handlers.sdf.shader_graph_mesh',)),
    **dict.fromkeys(('group_read', 'group_write', 'dispatch_mesh'), ('file_handlers.sdf.shader_graph_storage',)),
    'invocation_input': ('file_handlers.sdf.shader_graph_invocation',),
    'branch': ('file_handlers.mmtr.material_branch',),
    'repeat': ('file_handlers.mmtr.material_loop',),
    **dict.fromkeys(('resource_index', 'descriptor_heap'), ('file_handlers.sdf.shader_graph_resource',)),
    'resource_dimensions': ('file_handlers.sdf.shader_graph_dimensions',),
    **dict.fromkeys(('indexed_select', 'array_read', 'constant_table'),
                    ('file_handlers.sdf.shader_graph_indexed_value',)),
    'unassigned_value': ('file_handlers.sdf.shader_graph_unassigned',),
    'code': ('file_handlers.sdf.shader_graph_code',),
    **dict.fromkeys(('execution_begin', 'capture_value', 'discard_pixel', 'memory_load', 'memory_store',
                     'memory_atomic', 'memory_barrier', 'image_load', 'image_store', 'image_atomic'),
                    ('file_handlers.sdf.shader_graph_execution',)),
}


def register_shader_graph_node_extension(
    extension: ShaderGraphNodeExtension,
) -> ShaderGraphNodeExtension:
    kind = str(extension.kind).strip().lower()
    if not kind or kind in _NODE_KINDS or kind in _NODE_EXTENSIONS:
        raise ValueError(f"shader graph node kind {extension.kind!r} is already registered")
    if extension.expression is None and extension.evaluation is None:
        raise ValueError(f"shader graph node kind {kind!r} has no expression")
    _NODE_EXTENSIONS[kind] = extension
    return extension


def shader_graph_node_extension(kind: str) -> ShaderGraphNodeExtension | None:
    kind = str(kind).strip().lower()
    if kind in _NODE_EXTENSIONS:
        return _NODE_EXTENSIONS[kind]
    for module in EXTENSION_MODULES.get(kind, ()):
        importlib.import_module(module)
    return _NODE_EXTENSIONS.get(kind)


_property_validation = ContextVar('shader_property_validation', default=None)


@contextmanager
def immutable_property_validation(validated=None):
    token = _property_validation.set({} if validated is None else validated)
    try:
        yield
    finally:
        _property_validation.reset(token)


def _json_properties(value: Any) -> bool:
    return _checked_properties(value, None) is not _INVALID


def _new_graph_node(kind, x, y, properties, node_id):
    key = str(kind).strip().lower()
    if key not in _NODE_KINDS and shader_graph_node_extension(key) is None:
        raise ShaderGraphError(f"unknown shader graph node kind {kind!r}")
    identity = str(node_id or uuid4().hex)
    if not identity or len(identity) > 256:
        raise ShaderGraphError(f"duplicate or empty shader graph node id {identity!r}")
    values = dict(properties or {})
    checked_source = properties if (type(properties) is dict or
        isinstance(properties, dict) and isinstance(properties, IMMUTABLE_JSON)) else values
    if not _json_properties(checked_source):
        raise ShaderGraphError("Node properties must contain finite JSON values within their declared size limits.")
    return ShaderGraphNode(identity, key, float(x), float(y), values)


def _check_graph_tables(nodes, links):
    if not isinstance(nodes, list) or len(nodes) > MAX_GRAPH_NODES:
        raise ShaderGraphError("shader graph node table is invalid or too large")
    if not isinstance(links, list) or len(links) > MAX_GRAPH_LINKS:
        raise ShaderGraphError("shader graph link table is invalid or too large")


def _check_graph_header(stage, contract_fingerprint):
    if len(stage) > 32 or len(contract_fingerprint) > 128:
        raise ShaderGraphError("shader graph stage or contract fingerprint is invalid")


def _checked_graph_node(kind, x, y, properties, node_id, identities):
    x, y = float(x), float(y)
    if not math.isfinite(x) or not math.isfinite(y):
        raise ShaderGraphError("shader graph node position must be finite")
    node = _new_graph_node(kind, x, y, properties, node_id)
    if node.node_id in identities:
        raise ShaderGraphError(f"duplicate or empty shader graph node id {node.node_id!r}")
    identities.add(node.node_id)
    return node


def _check_graph_link_fields(fields):
    if any(not part or len(part) > 256 for part in fields):
        raise ShaderGraphError("shader graph link endpoint is empty or too long")


def _checked_graph_link(source_node, source_pin, target_node, target_pin):
    fields = tuple(str(part) for part in (source_node, source_pin, target_node, target_pin))
    _check_graph_link_fields(fields)
    return ShaderGraphLink(*fields)


@dataclass(slots=True)
class ShaderGraph:
    stage: str
    contract_fingerprint: str
    nodes: list[ShaderGraphNode] = field(default_factory=list)
    links: list[ShaderGraphLink] = field(default_factory=list)

    def node(self, node_id: str) -> ShaderGraphNode | None:
        return next((item for item in self.nodes if item.node_id == node_id), None)

    def add_node(
        self,
        kind: str,
        *,
        x: float = 0.0,
        y: float = 0.0,
        properties: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> ShaderGraphNode:
        if len(self.nodes) >= MAX_GRAPH_NODES:
            raise ShaderGraphError(f"shader graph is limited to {MAX_GRAPH_NODES} nodes")
        node = _new_graph_node(kind, x, y, properties, node_id)
        if self.node(node.node_id) is not None:
            raise ShaderGraphError(f"duplicate or empty shader graph node id {node.node_id!r}")
        self.nodes.append(node)
        return node

    def remove_node(self, node_id: str) -> None:
        node = self.node(node_id)
        if node is None:
            return
        if node.kind in {"output", "material_surface"} or bool(
            node.properties.get("semantic_reconstruction_boundary", False)
        ):
            raise ShaderGraphError("the material output or recovered master function cannot be removed")
        self.nodes.remove(node)
        self.links[:] = [
            link
            for link in self.links
            if link.source_node != node_id and link.target_node != node_id
        ]

    def upstream_nodes(self, roots) -> set[str]:
        incoming = {node.node_id: set() for node in self.nodes}
        for link in self.links:
            if link.target_node in incoming and link.source_node in incoming:
                incoming[link.target_node].add(link.source_node)
        live = set(roots) & incoming.keys()
        pending = list(live)
        while pending:
            for source in incoming[pending.pop()] - live:
                live.add(source)
                pending.append(source)
        return live

    def connect(
        self,
        source_node: str,
        source_pin: str,
        target_node: str,
        target_pin: str,
    ) -> ShaderGraphLink:
        if len(self.links) >= MAX_GRAPH_LINKS:
            raise ShaderGraphError(f"shader graph is limited to {MAX_GRAPH_LINKS} links")
        if source_node == target_node:
            raise ShaderGraphError("a node cannot connect to itself")
        self.disconnect_target(target_node, target_pin)
        link = ShaderGraphLink(
            str(source_node), str(source_pin), str(target_node), str(target_pin)
        )
        self.links.append(link)
        return link

    def disconnect_target(self, target_node: str, target_pin: str) -> None:
        self.links[:] = [
            link
            for link in self.links
            if not (link.target_node == target_node and link.target_pin == target_pin)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "contract_fingerprint": self.contract_fingerprint,
            "nodes": [
                {
                    "id": item.node_id,
                    "kind": item.kind,
                    "position": [item.x, item.y],
                    "properties": item.properties,
                }
                for item in self.nodes
            ],
            "links": [
                {
                    "source": [item.source_node, item.source_pin],
                    "target": [item.target_node, item.target_pin],
                }
                for item in self.links
            ],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ShaderGraph":
        if not isinstance(value, dict):
            raise ShaderGraphError("shader graph must be a JSON object")
        nodes_value = value.get("nodes", [])
        links_value = value.get("links", [])
        _check_graph_tables(nodes_value, links_value)
        graph = cls(
            stage=str(value.get("stage", "")),
            contract_fingerprint=str(value.get("contract_fingerprint", "")),
        )
        _check_graph_header(graph.stage, graph.contract_fingerprint)
        identities = set()
        for item in nodes_value:
            if not isinstance(item, dict):
                raise ShaderGraphError("shader graph node must be an object")
            position = item.get("position", [0.0, 0.0])
            if not isinstance(position, list) or len(position) != 2:
                raise ShaderGraphError("shader graph node position must contain x and y")
            node = _checked_graph_node(str(item.get('kind', '')), position[0], position[1],
                                       item.get('properties', {}), str(item.get('id', '')), identities)
            graph.nodes.append(node)
        for item in links_value:
            if not isinstance(item, dict):
                raise ShaderGraphError("shader graph link must be an object")
            source, target = item.get("source"), item.get("target")
            if not isinstance(source, list) or not isinstance(target, list) or len(source) != 2 or len(target) != 2:
                raise ShaderGraphError("shader graph link endpoints are invalid")
            graph.links.append(_checked_graph_link(*source, *target))
        return graph


def _operation_type(node: ShaderGraphNode) -> str:
    value = str(node.properties.get("value_type", "float4"))
    return value if value in _VALUE_TYPES else "float4"


def node_title(node: ShaderGraphNode) -> str:
    extension = shader_graph_node_extension(node.kind)
    if extension is not None:
        return str(node.properties.get("display_name") or extension.title)
    if node.kind in {"material_parameter", "material_texture"}:
        return str(node.properties.get("display_name") or node.properties.get("name") or "Material Input")
    if node.kind == "material_function":
        function = node.properties.get("function", {})
        return str(
            node.properties.get("display_name")
            or (function.get("name") if isinstance(function, dict) else "")
            or "Material Function"
        )
    if node.kind in {"function_input", "function_output"}:
        return str(
            node.properties.get("display_name")
            or node.properties.get("name")
            or ("Function Input" if node.kind == "function_input" else "Function Output")
        )
    return {
        "input": str(node.properties.get("display_name") or "Stage Input"),
        "constant": "Constant",
        "parameter": "Material / Scene Value",
        "structured_parameter": "Instanced Material Value",
        "texture_sample": "Texture Sample",
        "material_surface": "Material Output",
        "texture_coordinates": "Texture Coordinates",
        "sample_texture": "Sample Texture",
        "texture_load": "Read Texel",
        "add": "Add",
        "subtract": "Subtract",
        "multiply": "Multiply",
        "divide": "Divide",
        "lerp": "Linear Interpolate",
        "power": "Power",
        "minimum": "Minimum",
        "maximum": "Maximum",
        "clamp": "Clamp",
        "saturate": "Saturate",
        "normalize": "Normalize",
        "dot": "Dot Product",
        "one_minus": "One Minus",
        "absolute": "Absolute",
        "sine": "Sine",
        "cosine": "Cosine",
        "floor": "Floor",
        "ceiling": "Ceiling",
        "fraction": "Fraction",
        "square_root": "Square Root",
        "smoothstep": "Smoothstep",
        "distance": "Distance",
        "remap": "Remap",
        "normal_unpack": "Unpack Normal Map",
        "swizzle": "Component Mask",
        "compose": "Compose Vector",
        "output": "Stage Output",
    }.get(node.kind, node.kind.replace("_", " ").title())


def _item_name(item) -> str:
    return item.name


def swizzle_mask(node: ShaderGraphNode) -> str:
    source_type = str(node.properties.get("source_type", "float4"))
    width = _type_width(source_type)
    mask = str(node.properties.get("mask", "rgb")).casefold()
    if not 1 <= len(mask) <= 4 or any(char not in "xyzwrgba" for char in mask):
        raise ShaderGraphError("Component Mask needs 1–4 components from XYZW or RGBA.")
    mask = mask.translate(str.maketrans("rgba", "xyzw"))
    if not width or any("xyzw".index(char) >= width for char in mask):
        raise ShaderGraphError(f"Component Mask selects a component absent from {source_type}.")
    return mask


def node_property_diagnostics(node: ShaderGraphNode) -> tuple[ShaderGraphDiagnostic, ...]:
    if 'precise' in node.properties:
        from .shader_graph_precision import PRECISION_OPERATIONS
        if node.kind not in PRECISION_OPERATIONS or type(node.properties['precise']) is not bool:
            return (ShaderGraphDiagnostic('error', 'calculation-order', 'Invalid per-operation calculation-order constraint.', node.node_id),)
    if node.kind == 'unassigned_value':
        return (ShaderGraphDiagnostic('warning', 'unassigned-value', 'No value is assigned on this path. Replace this node with a defined value to specify its result.', node.node_id),)
    if node.kind in {'indexed_select', 'constant_table'} and node.properties.get('has_default', True) is False:
        return (ShaderGraphDiagnostic('warning', 'unspecified-case-default', 'This selector has no default: out-of-range results are unspecified. Enable and connect a default to define them.', node.node_id),)
    if node.kind in {'resource_index', 'descriptor_heap'}:
        from .shader_graph_resource import resource_properties
        try:
            resource_properties(node)
        except ValueError as exc:
            return (ShaderGraphDiagnostic('error', 'descriptor', str(exc), node.node_id),)
    if node.kind == "buffer_load":
        try:
            BufferRead.from_properties(node.properties)
        except ValueError as exc:
            return (ShaderGraphDiagnostic("error", "buffer-read", str(exc), node.node_id),)
    if node.kind in {'sample_texture', 'texture_load'}:
        try:
            TextureOperation.from_properties(node.kind, node.properties)
        except ValueError as exc:
            return (ShaderGraphDiagnostic('error', 'texture-operation', str(exc), node.node_id),)
    if node.kind in TYPED_MATH_NODES:
        try:
            typed_math_signature(node.kind, node.properties)
        except ValueError as exc:
            return (ShaderGraphDiagnostic('error', 'value-type', str(exc), node.node_id),)
    if node.kind == "swizzle":
        try:
            swizzle_mask(node)
        except ShaderGraphError as exc:
            return (ShaderGraphDiagnostic("error", "component-mask", str(exc), node.node_id),)
    if node.kind == "compare":
        try:
            comparison_expression(node.properties, "a", "b")
        except ValueError as exc:
            return (ShaderGraphDiagnostic("error", "comparison", str(exc), node.node_id),)
    return ()


def validate_node_update(graph, node, properties, contract):
    candidate = replace(node, properties=properties)
    errors = node_property_diagnostics(candidate)
    if errors:
        raise ShaderGraphError(errors[0].message)
    for link in graph.links:
        if node.node_id not in (link.source_node, link.target_node):
            continue
        source = candidate if link.source_node == node.node_id else graph.node(link.source_node)
        target = candidate if link.target_node == node.node_id else graph.node(link.target_node)
        output = next((p for p in node_pins(source, contract) if p.direction == 'output' and p.name == link.source_pin), None)
        input_pin = next((p for p in node_pins(target, contract) if p.direction == 'input' and p.name == link.target_pin), None)
        if output is None or input_pin is None or not connection_compatible(output.value_type, input_pin.value_type):
            raise ShaderGraphError('Disconnect incompatible ports before changing this setting.')


def node_pins(
    node: ShaderGraphNode,
    contract: ShaderContract,
) -> tuple[ShaderPin, ...]:
    inputs: list[ShaderPin] = []
    outputs: list[ShaderPin] = []
    kind = node.kind
    extension = shader_graph_node_extension(kind)
    if extension is not None:
        return extension.pins_provider(node, contract)
    if kind == "input":
        field = contract.input(
            str(node.properties.get("semantic", "")),
            int(node.properties.get("semantic_index", 0)),
        )
        if field is not None:
            outputs.append(ShaderPin("value", field.display_name, field.value_type, "output"))
    elif kind == "constant":
        value_type = _operation_type(node)
        outputs.append(ShaderPin("value", "Value", value_type, "output"))
    elif kind == "parameter":
        buffer = contract.buffer(str(node.properties.get("buffer", "")))
        variable_name = str(node.properties.get("variable", ""))
        variable = resolve_material_name(variable_name, buffer.variables if buffer is not None else (), key=_item_name)
        if variable is not None and "field_path" in node.properties:
            from .aggregate_values import constant_value_fields
            field = next((item for item in constant_value_fields(variable) if item.path == node.properties["field_path"]), None)
            if field is not None:
                outputs.append(ShaderPin("value", variable.name + field.path, field.value_type, "output"))
        elif variable is not None and variable.graph_type is not None:
            outputs.append(ShaderPin("value", variable.name, variable.graph_type, "output"))
    elif kind == "shader_resource":
        resource = contract.resource(str(node.properties.get("resource", ""))) if contract else None
        if resource is not None and (resource.texture_value_type or resource.input_type in {'sampler', 'byte-address-buffer', 'rw-byte-address-buffer', 'structured-buffer', 'rw-structured-buffer'}):
            from .shader_graph_resource import resource_value_type
            outputs.append(ShaderPin('value', resource.name, resource_value_type(resource,
                globally_coherent=node.properties.get('globally_coherent', False), word_bits=node.properties.get('word_bits',32)), 'output'))
        elif resource is not None and resource.input_type == 'constant-buffer' and not resource.sample_count:
            buffer = contract.buffer(resource.name)
            if buffer is not None and resource.bind_count == 1:
                from .shader_graph_constant_buffer import constant_buffer_type, row_view_size
                outputs.append(ShaderPin('value', resource.name, constant_buffer_type(row_view_size(buffer.byte_size)), 'output'))
    elif kind == "structured_parameter":
        buffer = contract.structured_buffer(
            str(node.properties.get("resource", ""))
        )
        variable_name = str(node.properties.get("variable", ""))
        variable = resolve_material_name(variable_name, buffer.variables if buffer is not None else (), key=_item_name)
        if variable is not None and variable.graph_type is not None:
            outputs.append(
                ShaderPin("value", variable.name, variable.graph_type, "output")
            )
    elif kind == "material_parameter":
        value_type = str(node.properties.get("value_type", "float"))
        if value_type not in _FLOAT_TYPES:
            value_type = "float"
        outputs.append(ShaderPin("value", "Value", value_type, "output"))
    elif kind == "material_texture":
        if node.properties.get("dimension", "texture2d") is None:
            return (ShaderPin("texture_object", "Descriptor", "texture", "output"),)
        dimension = str(node.properties.get("dimension", "texture2d")).casefold()
        coordinate_type = TEXTURE_COORDINATE_TYPES.get(dimension, "float2")
        inputs.append(ShaderPin("coordinates", "Coordinates", coordinate_type, "input", False))
        outputs.extend(
            (
                ShaderPin(
                    "texture_object",
                    "Texture",
                    dimension,
                    "output",
                ),
                ShaderPin("sampler_object", "Material Sampler", "sampler", "output"),
                ShaderPin("rgba", "RGBA", "float4", "output"),
                ShaderPin("rgb", "RGB", "float3", "output"),
                ShaderPin("r", "R", "float", "output"),
                ShaderPin("g", "G", "float", "output"),
                ShaderPin("b", "B", "float", "output"),
                ShaderPin("a", "A", "float", "output"),
            )
        )
    elif kind == "material_function":
        function = node.properties.get("function", {})
        if isinstance(function, dict):
            for direction, key in (("input", "inputs"), ("output", "outputs")):
                values = function.get(key, [])
                if not isinstance(values, list):
                    continue
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    pin_id = str(value.get("id", ""))
                    value_type = str(value.get("type", ""))
                    if pin_id and value_type:
                        inputs_or_outputs = inputs if direction == "input" else outputs
                        inputs_or_outputs.append(
                            ShaderPin(
                                pin_id,
                                str(value.get("name") or pin_id),
                                value_type,
                                direction,
                                bool(value.get("required", direction == "input")),
                            )
                        )
    elif kind in {"function_input", "function_output"}:
        pin_id = str(node.properties.get("pin_id", ""))
        value_type = str(node.properties.get("value_type", ""))
        label = str(node.properties.get("display_name") or pin_id or "Value")
        if pin_id and value_type:
            (outputs if kind == "function_input" else inputs).append(
                ShaderPin(
                    "value",
                    label,
                    value_type,
                    "output" if kind == "function_input" else "input",
                    kind == "function_output",
                )
            )
    elif kind == "texture_coordinates":
        outputs.append(ShaderPin("value", "UV", "float2", "output"))
    elif kind == "buffer_load":
        try:
            operation = BufferRead.from_properties(node.properties)
        except ValueError:
            return ()
        return (*(ShaderPin(name, name.replace('_', ' ').title(), kind, "input", True) for name, kind in operation.inputs),
                ShaderPin("value", "Word" if operation.words == 1 else "Words", operation.value_type, "output"))
    elif kind in {'sample_texture', 'texture_load'}:
        try:
            operation = TextureOperation.from_properties(kind, node.properties)
        except ValueError:
            return ()
        inputs.extend(ShaderPin(name, name.replace('_', ' ').title(), value_type, 'input', True) for name, value_type in operation.inputs)
        outputs.extend(ShaderPin(name, name.upper() if name != 'value' else 'Value', value_type, 'output')
                       for name, (_, value_type) in operation.outputs.items())
    elif kind == "material_surface":
        if bool(node.properties.get("accepts_recovered_material", False)):
            from file_handlers.mmtr.profiles.material_graph_expression import surface_channel_pins
            inputs.append(
                ShaderPin(
                    "recovered_material",
                    "Imported Shading",
                    "material",
                    "input",
                    True,
                )
            )
            inputs.extend(surface_channel_pins(node, "input"))
            return tuple(inputs)
        inputs.extend(
            (
                ShaderPin("base_color", "Base Color", "float3", "input", False),
                ShaderPin("emission", "Emissive Color", "float3", "input", False),
                ShaderPin("world_normal", "Normal", "float3", "input", False),
                ShaderPin("roughness", "Roughness", "float", "input", False),
                ShaderPin("metalness", "Metallic", "float", "input", False),
                ShaderPin("specular", "Specular", "float", "input", False),
                ShaderPin("translucency", "Translucency", "float", "input", False),
                ShaderPin(
                    "ambient_occlusion",
                    "Ambient Occlusion",
                    "float",
                    "input",
                    False,
                ),
                ShaderPin("opacity", "Opacity", "float", "input", False),
                ShaderPin("opacity_mask", "Opacity Mask", "float", "input", False),
                ShaderPin("dissolve", "Dissolve Amount", "float", "input", False),
                ShaderPin("glow", "Glow / Bloom", "float", "input", False),
            )
        )
    elif kind == "texture_sample":
        resource = contract.resource(str(node.properties.get("texture", "")))
        if resource is not None and resource.sampleable:
            inputs.append(ShaderPin("coordinates", "Coordinates", resource.coordinate_type, "input", True))
            object_type = resource.object_type
            outputs.extend(
                (
                    ShaderPin(
                        "texture_object",
                        "Texture",
                        object_type,
                        "output",
                    ),
                    ShaderPin(
                        "sampler_object",
                        "Sampler",
                        "sampler",
                        "output",
                    ),
                    *(ShaderPin(name, name.upper() if name != 'value' else 'Value', value_type, 'output')
                      for name, (_, value_type) in texture_result_pins(resource.texture_value_type or 'float4').items()),
                )
            )
    elif kind in {"add", "subtract", "multiply", "divide", "power", "minimum", "maximum"}:
        value_type = _operation_type(node)
        inputs.extend(
            (
                ShaderPin("a", "A", value_type, "input", True),
                ShaderPin("b", "B", value_type, "input", True),
            )
        )
        outputs.append(ShaderPin("value", "Result", value_type, "output"))
    elif kind == "lerp":
        value_type = _operation_type(node)
        inputs.extend(
            (
                ShaderPin("a", "A", value_type, "input", True),
                ShaderPin("b", "B", value_type, "input", True),
                ShaderPin("factor", "Factor", "float", "input", True),
            )
        )
        outputs.append(ShaderPin("value", "Result", value_type, "output"))
    elif kind in TYPED_MATH_NODES:
        try:
            arguments, result_type = typed_math_signature(kind, node.properties)
        except ValueError:
            return ()
        inputs.extend(ShaderPin(key, key.title(), value_type, 'input', True) for key, value_type in arguments)
        outputs.append(ShaderPin('result', 'Result', result_type, 'output'))
    elif kind == "compare":
        inputs.extend(ShaderPin(key, key.upper(), node.properties.get('value_type', 'float'), "input", True) for key in ("a", "b"))
        outputs.append(ShaderPin("result", "Condition", "bool", "output"))
    elif kind == "select":
        value_type = _operation_type(node)
        inputs.append(ShaderPin("condition", "Condition", "bool", "input", True))
        inputs.extend(ShaderPin(key, key.title(), value_type, "input", True) for key in ("true", "false"))
        outputs.append(ShaderPin("result", "Result", value_type, "output"))
    elif kind == "multiply_add":
        value_type = _operation_type(node)
        inputs.extend(ShaderPin(key, key.upper(), value_type, "input", True) for key in ("a", "b", "c"))
        outputs.append(ShaderPin("result", "Result", value_type, "output"))
    elif kind == "clamp":
        value_type = _operation_type(node)
        inputs.extend(
            (
                ShaderPin("value", "Value", value_type, "input", True),
                ShaderPin("minimum", "Minimum", value_type, "input", True),
                ShaderPin("maximum", "Maximum", value_type, "input", True),
            )
        )
        outputs.append(ShaderPin("result", "Result", value_type, "output"))
    elif kind in {"saturate", "normalize", "one_minus"} or kind in UNARY_FUNCTIONS:
        value_type = _operation_type(node)
        inputs.append(ShaderPin("value", "Value", value_type, "input", True))
        outputs.append(ShaderPin("result", "Result", value_type, "output"))
    elif kind == "normal_unpack":
        inputs.append(ShaderPin("value", "Texture RGB", "float3", "input", True))
        outputs.append(ShaderPin("result", "Normal", "float3", "output"))
    elif kind == "smoothstep":
        value_type = _operation_type(node)
        inputs.extend(
            (
                ShaderPin("minimum", "Minimum", value_type, "input", True),
                ShaderPin("maximum", "Maximum", value_type, "input", True),
                ShaderPin("value", "Value", value_type, "input", True),
            )
        )
        outputs.append(ShaderPin("result", "Result", value_type, "output"))
    elif kind == "distance":
        value_type = _operation_type(node)
        inputs.extend(
            (
                ShaderPin("a", "A", value_type, "input", True),
                ShaderPin("b", "B", value_type, "input", True),
            )
        )
        outputs.append(ShaderPin("value", "Distance", "float", "output"))
    elif kind == "remap":
        value_type = _operation_type(node)
        inputs.extend(
            (
                ShaderPin("value", "Value", value_type, "input", True),
                ShaderPin("input_min", "Input Minimum", value_type, "input", True),
                ShaderPin("input_max", "Input Maximum", value_type, "input", True),
                ShaderPin("output_min", "Output Minimum", value_type, "input", True),
                ShaderPin("output_max", "Output Maximum", value_type, "input", True),
            )
        )
        outputs.append(ShaderPin("result", "Result", value_type, "output"))
    elif kind == "dot":
        value_type = _operation_type(node)
        inputs.extend(
            (
                ShaderPin("a", "A", value_type, "input", True),
                ShaderPin("b", "B", value_type, "input", True),
            )
        )
        outputs.append(ShaderPin("value", "Result", _type_base(value_type), "output"))
    elif kind == "swizzle":
        source_type = str(node.properties.get("source_type", "float4"))
        inputs.append(ShaderPin("value", "Value", source_type, "input", True))
        try:
            mask = swizzle_mask(node)
        except ShaderGraphError:
            return tuple(inputs)
        result_type = _type_base(source_type) + (str(len(mask)) if len(mask)>1 else "")
        outputs.append(ShaderPin("result", str(node.properties.get("mask", "rgb")).upper(), result_type, "output"))
    elif kind == "compose":
        value_type = _operation_type(node)
        width = _type_width(value_type)
        for name in "XYZW"[:width]:
            inputs.append(ShaderPin(name.casefold(), name, _type_base(value_type), "input", True))
        outputs.append(ShaderPin("value", "Vector", value_type, "output"))
    elif kind == "output":
        inputs.extend(
            ShaderPin(item.pin_name, item.display_name, item.value_type, "input", True)
            for item in contract.outputs if contract.stage not in {'geometry','mesh'}
        )
        if contract.stage == "pixel":
            inputs.append(ShaderPin("clip", "Opacity clip", "float", "input", False))
        inputs.append(ShaderPin('execution', 'Execution', 'execution', 'input', contract.stage in {'geometry','mesh'}))
        if contract.stage == 'hull':
            from .shader_graph_hull import patch_output_pin
            inputs.extend(ShaderPin(patch_output_pin(field), 'Patch: ' + field.display_name, field.value_type, 'input', True)
                          for field in contract.patch_constants)
            inputs.append(ShaderPin('patch_execution', 'Patch Execution', 'execution', 'input', False))
    return tuple(inputs + outputs)


def mask_components(mask: int) -> str:
    return "".join(component for bit, component in enumerate("xyzw") if int(mask) & (1 << bit))


def signature_member(prefix: str, field) -> str:
    return _identifier(f"{prefix}_{field.semantic_name}_{field.semantic_index}")


def interpolation_modifier(mode: str) -> str:
    """Linear is HLSL's default, so it is left out."""
    return "nointerpolation" if mode == "constant" else mode.replace("linear", "").strip()


def signature_declaration(field, member: str, value_type: str | None = None, attribute: str = "", *,
                          interpolation: str | None = None) -> str:
    modifier = interpolation_modifier(field.interpolation if interpolation is None else interpolation)
    return (f"    {attribute}{modifier + ' ' if modifier else ''}{field.value_type if value_type is None else value_type} "
            f"{member} : {_semantic_token(field.semantic_name, field.semantic_index)};")


def node_pin(node: ShaderGraphNode, contract: ShaderContract | None, name: str, direction: str) -> ShaderPin | None:
    return next(
        (
            item
            for item in node_pins(node, contract)
            if item.name == name and item.direction == direction
        ),
        None,
    )


def connection_compatible(source_type: str, target_type: str) -> bool:
    if source_type == target_type:
        return True
    if target_type == "texture" and source_type in TEXTURE_OBJECT_TYPES:
        return True
    source_base = _type_base(source_type)
    target_base = _type_base(target_type)
    source_width = _type_width(source_type)
    target_width = _type_width(target_type)
    if (
        source_base in {"float", "int", "uint"}
        and target_base in {"float", "int", "uint"}
        and source_width == target_width == 1
    ):
        return True
    return bool(
        source_base == "float"
        and target_base == "float"
        and source_width == 1
    )


def connection_adapter(source_type: str, target_type: str) -> tuple[str, dict, str, str] | None:
    numbers = {"float", "int", "uint"}
    source_base, target_base = _type_base(source_type), _type_base(target_type)
    source_width, target_width = _type_width(source_type), _type_width(target_type)
    if source_base not in numbers or target_base not in numbers:
        return None
    if source_base == target_base and target_width < source_width:
        return "swizzle", {"source_type": source_type, "mask": "xyzw"[:target_width]}, "value", "result"
    if source_base != target_base and source_width == target_width:
        return "convert", {"value_type": target_type, "source_type": source_type}, "value", "result"
    return None


def validate_graph(graph: ShaderGraph, contract: ShaderContract) -> tuple[ShaderGraphDiagnostic, ...]:
    from file_handlers.mmtr.material_region_read import region_read_session
    with region_read_session():
        return _validate_graph(graph, contract)


def _validate_graph(graph: ShaderGraph, contract: ShaderContract) -> tuple[ShaderGraphDiagnostic, ...]:
    diagnostics: list[ShaderGraphDiagnostic] = []
    if graph.stage != contract.stage:
        diagnostics.append(
            ShaderGraphDiagnostic("error", "stage", "Graph stage does not match the selected shader.")
        )
    if graph.contract_fingerprint and graph.contract_fingerprint != contract.fingerprint:
        diagnostics.append(
            ShaderGraphDiagnostic(
                "warning",
                "contract-changed",
                "The graph was created for a different reflected contract; reconnect invalid pins before compiling.",
            )
        )
    identities = [item.node_id for item in graph.nodes]
    if len(identities) != len(set(identities)):
        diagnostics.append(ShaderGraphDiagnostic("error", "duplicate-node", "Graph contains duplicate node IDs."))
    outputs = [item for item in graph.nodes if item.kind == "output"]
    if len(outputs) != 1:
        diagnostics.append(
            ShaderGraphDiagnostic("error", "output-count", "Graph needs exactly one stage output node.")
        )
    node_map = {item.node_id: item for item in graph.nodes}
    resolved_pins = {}
    def pins(node):
        identity = id(node)
        if identity not in resolved_pins:
            resolved_pins[identity] = node_pins(node, contract)
        return resolved_pins[identity]
    for node in graph.nodes:
        diagnostics.extend(node_property_diagnostics(node))
    adjacency: dict[str, set[str]] = {item.node_id: set() for item in graph.nodes}
    reverse_adjacency: dict[str, set[str]] = {
        item.node_id: set() for item in graph.nodes
    }
    incoming: set[tuple[str, str]] = set()
    for link in graph.links:
        source, target = node_map.get(link.source_node), node_map.get(link.target_node)
        if source is None or target is None:
            diagnostics.append(ShaderGraphDiagnostic("error", "dangling-link", "Graph contains a link to a missing node."))
            continue
        source_pin = next((p for p in pins(source) if p.name == link.source_pin and p.direction == 'output'), None)
        target_pin = next((p for p in pins(target) if p.name == link.target_pin and p.direction == 'input'), None)
        if source_pin is None or target_pin is None:
            diagnostics.append(
                ShaderGraphDiagnostic("error", "missing-pin", "Graph contains a link to a missing pin.", target.node_id)
            )
            continue
        target_key = (target.node_id, target_pin.name)
        if target_key in incoming:
            diagnostics.append(
                ShaderGraphDiagnostic("error", "multiple-inputs", f"{target_pin.label} has more than one incoming link.", target.node_id)
            )
        incoming.add(target_key)
        if not connection_compatible(source_pin.value_type, target_pin.value_type):
            diagnostics.append(
                ShaderGraphDiagnostic(
                    "error",
                    "type-mismatch",
                    f"Cannot connect {source_pin.value_type} to {target_pin.value_type}.",
                    target.node_id,
                )
            )
        adjacency[source.node_id].add(target.node_id)
        reverse_adjacency[target.node_id].add(source.node_id)

    indegree = {identity: 0 for identity in adjacency}
    for targets in adjacency.values():
        for target in targets:
            indegree[target] += 1
    queue = [identity for identity, count in indegree.items() if count == 0]
    visited = 0
    while queue:
        identity = queue.pop()
        visited += 1
        for target in adjacency[identity]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(adjacency):
        diagnostics.append(ShaderGraphDiagnostic("error", "cycle", "Shader graphs cannot contain cycles."))

    reachable = {item.node_id for item in outputs}
    pending = list(reachable)
    while pending:
        target = pending.pop()
        for source in reverse_adjacency.get(target, ()):
            if source not in reachable:
                reachable.add(source)
                pending.append(source)
    for node in graph.nodes:
        ports = pins(node)
        if node.node_id in reachable and (context_error := math_context_error(node.kind, contract)):
            diagnostics.append(ShaderGraphDiagnostic('error', 'shader-model', context_error, node.node_id))
        if (
            node.node_id in reachable
            and node.kind != "output"
            and not any(item.direction == "output" for item in ports)
        ):
            diagnostics.append(
                ShaderGraphDiagnostic("error", "invalid-node", f"{node_title(node)} no longer resolves against this pass.", node.node_id)
            )
        for pin in ports:
            if (
                node.node_id in reachable
                and pin.direction == "input"
                and pin.required
                and (node.node_id, pin.name) not in incoming
            ):
                diagnostics.append(
                    ShaderGraphDiagnostic("error", "unconnected", f"{node_title(node)}: {pin.label} is not connected.", node.node_id)
                )
    if outputs and not contract.outputs and contract.stage == "pixel" and not any((outputs[0].node_id, pin) in incoming for pin in ('clip', 'execution')):
        diagnostics.append(
            ShaderGraphDiagnostic(
                "error",
                "empty-depth-pass",
                "A pixel pass with no color outputs must connect Opacity clip; an empty shader would change visibility behavior.",
                outputs[0].node_id,
            )
        )
    from .shader_graph_execution import execution_diagnostics
    diagnostics.extend(execution_diagnostics(graph, pins))
    return tuple(diagnostics)


def new_shader_graph(contract: ShaderContract) -> ShaderGraph:
    graph = ShaderGraph(contract.stage, contract.fingerprint)
    y = 40.0
    for item in contract.inputs:
        if contract.stage in {'geometry','domain','hull'} and not item.primitive_attribute:
            continue
        graph.add_node(
            'primitive_input' if contract.stage in {'geometry','domain','hull'} else 'input',
            x=40.0,
            y=y,
            properties={"semantic": item.semantic_name, "semantic_index": item.semantic_index},
        )
        y += 125.0
    graph.add_node("output", x=900.0, y=max(80.0, y * 0.35))
    return graph


def shader_value_literal(value_type, values):
    width = _type_width(value_type)
    if not width:
        raise ShaderGraphError(f"Cannot construct a constant of type {value_type}")
    values = (list(values) + [0] * width)[:width]
    rendered = []
    for value in values:
        from .shader_graph_math import is_float_type
        if is_float_type(value_type):
            rendered.append(_float_literal(value))
        elif value_type.startswith("bool"):
            if value not in (0, 1, False, True):
                raise ShaderGraphError("Boolean constants must be false or true")
            rendered.append("true" if value else "false")
        else:
            number = int(value)
            base = value_type.rstrip('234')
            unsigned = base in {'uint', 'min16uint', 'uint64_t'}
            bits = 64 if base.endswith('64_t') else 32
            low, high = (0, (1 << bits)-1) if unsigned else (-(1 << (bits-1)), (1 << (bits-1))-1)
            if number != value or not low <= number <= high:
                raise ShaderGraphError(f"Integer constant is outside its declared {bits}-bit type")
            suffix = 'ull' if unsigned and bits == 64 else 'll' if bits == 64 else 'u' if unsigned else ''
            rendered.append(f'(-{high}{suffix} - 1{suffix})' if not unsigned and number == low else f'{number}{suffix}')
    return rendered[0] if width == 1 and value_type in {'float', 'int', 'uint', 'bool'} else f"{value_type}({', '.join(rendered)})"


def _float_literal(value: Any) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ShaderGraphError("constant values must be finite")
    text = format(number, ".9g")
    if "e" not in text.casefold() and "." not in text:
        text += ".0"
    return text + "f"


def splat_expression(value_type: str, expression: str) -> str:
    width = value_width(value_type)
    if width == 1:
        return expression
    return f"{value_type}({', '.join(expression for _ in range(width))})"


def math_node_expression(node: ShaderGraphNode, value_type: str, incoming) -> tuple[str, str] | None:
    kind = node.kind
    if kind in {"add", "subtract", "multiply", "divide"}:
        operator = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}[kind]
        return f"({incoming('a', value_type)} {operator} {incoming('b', value_type)})", value_type
    if kind in {"power", "minimum", "maximum"}:
        function = {"power": "pow", "minimum": "min", "maximum": "max"}[kind]
        return f"{function}({incoming('a', value_type)}, {incoming('b', value_type)})", value_type
    if kind == "lerp":
        return f"lerp({incoming('a', value_type)}, {incoming('b', value_type)}, {incoming('factor', 'float')})", value_type
    if kind == "multiply_add":
        return f"mad({incoming('a', value_type)}, {incoming('b', value_type)}, {incoming('c', value_type)})", value_type
    if kind == "compare":
        try:
            comparison_type = node.properties.get('value_type', 'float')
            return comparison_expression(node.properties, incoming("a", comparison_type), incoming("b", comparison_type)), "bool"
        except ValueError as exc:
            raise ShaderGraphError(str(exc)) from exc
    if kind == "clamp":
        return (f"clamp({incoming('value', value_type)}, {incoming('minimum', value_type)}, "
                f"{incoming('maximum', value_type)})", value_type)
    if kind in {"saturate", "normalize"}:
        return f"{kind}({incoming('value', value_type)})", value_type
    if kind == "one_minus":
        return f"({splat_expression(value_type, '1.0')} - {incoming('value', value_type)})", value_type
    if kind in UNARY_FUNCTIONS:
        return unary_expression(kind, incoming('value', value_type), value_type), value_type
    if kind == "normal_unpack":
        return f"normalize({incoming('value', 'float3')} * 2.0 - 1.0)", "float3"
    if kind == "smoothstep":
        return (f"smoothstep({incoming('minimum', value_type)}, {incoming('maximum', value_type)}, "
                f"{incoming('value', value_type)})", value_type)
    if kind == "distance":
        return f"distance({incoming('a', value_type)}, {incoming('b', value_type)})", "float"
    if kind == "remap":
        return remap_expression(value_type, *(incoming(pin, value_type)
            for pin in ("value", "input_min", "input_max", "output_min", "output_max"))), value_type
    if kind == "dot":
        return f"dot({incoming('a', value_type)}, {incoming('b', value_type)})", value_type.rstrip('234')
    if kind == "swizzle":
        source_type = str(node.properties.get("source_type", "float4"))
        mask = swizzle_mask(node)
        return (f"({incoming('value', source_type)}).{mask}",
                source_type.rstrip("1234") + (str(len(mask)) if len(mask) > 1 else ""))
    if kind == "compose":
        components = ", ".join(incoming(name.casefold(), value_type.rstrip("1234")) for name in "XYZW"[:value_width(value_type)])
        return f"{value_type}({components})", value_type
    return None


def _coerce(expression: str, source_type: str, target_type: str) -> str:
    if source_type == target_type:
        return expression
    if connection_compatible(source_type, target_type):
        width = _type_width(target_type)
        if width == 1:
            return f"{target_type}({expression})"
        return f"{target_type}({', '.join(expression for _ in range(width))})"
    raise ShaderGraphError(f"cannot convert {source_type} to {target_type}")


def texture_element_type(return_type: str, component_count: int = 4) -> str:
    element = {
        "unorm": "unorm float4",
        "snorm": "snorm float4",
        "sint": "int4",
        "int": "int4",
        "uint": "uint4",
        "sint64": "int64_t4",
        "uint64": "uint64_t4",
        "int64_t": "int64_t4",
        "uint64_t": "uint64_t4",
        "double": "double4",
    }.get(return_type, "float4")
    if component_count not in {1, 2, 3, 4}:
        raise ShaderGraphError('Texture element width must be between one and four.')
    return element.removesuffix('4') + (str(component_count) if component_count > 1 else '')


def texture_hlsl_type(dimension: str, return_type: str = "float", component_count: int = 4) -> str:
    element = texture_element_type(return_type, component_count)
    object_type = {
        "texture1d": "Texture1D",
        "texture1darray": "Texture1DArray",
        "texture2d": "Texture2D",
        "texture2darray": "Texture2DArray",
        "texture2dms": "Texture2DMS",
        "texture2dmsarray": "Texture2DMSArray",
        "texture3d": "Texture3D",
        "texturecube": "TextureCube",
        "texturecubearray": "TextureCubeArray",
    }.get(dimension.replace('-', ''))
    if object_type is None:
        raise ShaderGraphError(f"Texture dimension {dimension!r} is not sampleable by the graph")
    return f"{object_type}<{element}>"


def _resource_hlsl_type(resource: ShaderContractResource) -> str:
    declaration = texture_hlsl_type(resource.dimension, resource.return_type, ((resource.flags >> 2) & 3) + 1)
    if resource.texture_samples > 0:
        declaration = declaration[:-1] + f', {resource.texture_samples}>'
    return declaration


def _packoffset(byte_offset: int) -> str:
    register, component = divmod(int(byte_offset), 16)
    component //= 4
    return f"c{register}.{('x', 'y', 'z', 'w')[component]}"


def _hlsl_register(register_class: str, bind_point: int, register_space: int) -> str:
    suffix = f", space{int(register_space)}" if int(register_space) else ""
    return f"register({register_class}{int(bind_point)}{suffix})"


def _hlsl_variable_parts(variable) -> tuple[str, str]:
    declaration = str(variable.hlsl_declaration or "").strip()
    match = re.fullmatch(r"([^\[\]]+)((?:\[[1-9][0-9]*\])*)", declaration)
    if match is None:
        raise ShaderGraphError(f"Invalid reflected HLSL type {declaration!r}")
    outer = f"[{int(variable.array_length)}]" if variable.array_length > 1 else ""
    return match[1], outer + match[2]


def _hlsl_variable_declaration(variable, identifier: str) -> str:
    declaration, suffix = _hlsl_variable_parts(variable)
    return f"{declaration} {identifier}{suffix}"


def _is_graph_padding(buffer, variable):
    return str(variable.name) == f"REasyOpaque_{_identifier(buffer.name, prefix='Buffer')}_{variable.byte_offset}" and (
        variable.hlsl_declaration == 'uint' and variable.byte_size == 4 and variable.array_length == 1
        and not variable.matrix and not variable.members)


def _constant_buffer_identifiers(buffers, *, reserved=()) -> dict[tuple[str, str], str]:
    buffers = tuple(buffers)
    candidates = []
    for buffer in buffers:
        prefix = _identifier(f"REasy_{buffer.name}_")
        for variable in buffer.variables:
            name = str(variable.name)
            padding = _is_graph_padding(buffer, variable)
            retained = re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) is not None and (name.startswith(prefix) or padding)
            candidate = name if retained else _identifier(f"REasy_{buffer.name}_{name}")
            candidates.append((not retained, buffer.register_space, buffer.bind_point, buffer.name,
                               variable.byte_offset, name, candidate))
    occupied = {_identifier(buffer.name, prefix='Buffer') for buffer in buffers} | {_identifier(name) for name in reserved}
    result = {}
    for _fresh, space, slot, buffer_name, offset, name, candidate in sorted(candidates):
        identifier = candidate
        if identifier in occupied:
            identifier += f'_s{space}_b{slot}_o{offset}'
            while identifier in occupied:
                identifier += '_'
        occupied.add(identifier)
        key = buffer_name, name
        if key in result:
            raise ShaderGraphError(f'Constant-buffer field {buffer_name}.{name} is ambiguous.')
        result[key] = identifier
    return result


def _emit_cbuffer(
    buffer: ShaderContractBuffer,
    identifiers: dict[tuple[str, str], str] | None = None,
) -> tuple[str, dict[str, str]]:
    if buffer.bind_point < 0:
        raise ShaderGraphError(f"constant buffer {buffer.name} has no reflected register")
    buffer_id = _identifier(buffer.name, prefix="Buffer")
    lines = [
        f"cbuffer {buffer_id} : "
        f"{_hlsl_register('b', buffer.bind_point, buffer.register_space)}",
        "{",
    ]
    occupied = [False] * max(0, (buffer.byte_size + 3) // 4)
    if identifiers is None:
        identifiers = _constant_buffer_identifiers((buffer,))
    names: dict[str, str] = {}
    for index, variable in enumerate(sorted(buffer.variables, key=lambda item: item.byte_offset)):
        declaration = variable.hlsl_declaration
        can_declare = bool(
            declaration
            and variable.byte_offset % 4 == 0
            and variable.byte_size > 0
            and (not (variable.matrix or variable.array_length > 1) or variable.byte_offset % 16 == 0)
        )
        if not can_declare:
            continue
        identifier = identifiers[buffer.name, variable.name]
        if _is_graph_padding(buffer, variable) and identifier == variable.name:
            continue
        lines.append(
            f"    {_hlsl_variable_declaration(variable, identifier)} : packoffset({_packoffset(variable.byte_offset)});"
        )
        names[variable.name] = identifier
        first = variable.byte_offset // 4
        stop = min(len(occupied), (variable.byte_offset + variable.byte_size + 3) // 4)
        for word in range(first, stop):
            occupied[word] = True
    for word, used in enumerate(occupied):
        if used:
            continue
        byte_offset = word * 4
        # Cbuffer members share one scope, so padding is keyed by buffer, not register.
        lines.append(
            f"    uint REasyOpaque_{buffer_id}_{byte_offset} : packoffset({_packoffset(byte_offset)});"
        )
    lines.extend(("};", ""))
    return "\n".join(lines), names


def _emit_structured_buffer(
    buffer: ShaderContractBuffer,
    resource: ShaderContractResource,
    identifier: str,
    binding_attribute: str = '',
) -> str:
    if resource.input_type != "structured-buffer":
        raise ShaderGraphError(
            f"resource {resource.name} is not a read-only structured buffer"
        )
    if buffer.byte_size <= 0 or buffer.byte_size % 4:
        raise ShaderGraphError(
            f"structured material buffer {buffer.name} has an invalid {buffer.byte_size}-byte stride"
        )
    if resource.sample_count != buffer.byte_size:
        raise ShaderGraphError(
            f"structured material buffer {buffer.name} layout is {buffer.byte_size} bytes, "
            f"but the shader contract declares a {resource.sample_count}-byte stride"
        )
    element_type, declaration = structured_storage(buffer.byte_size)
    lines = [declaration]
    lines.extend(
        (
            f"{binding_attribute}StructuredBuffer<{element_type}> {identifier} : "
            f"{_hlsl_register('t', resource.bind_point, resource.register_space)};",
            "",
        )
    )
    return "\n".join(lines)


def _structured_parameter_expression(
    buffer: ShaderContractBuffer,
    variable: ShaderContractVariable,
    resource_identifier: str,
    index_expression: str,
) -> str:
    value_type = str(variable.graph_type or "")
    width = _type_width(value_type)
    base = _type_base(value_type)
    if (
        width <= 0
        or base not in {"float", "int", "uint"}
        or variable.byte_offset < 0
        or variable.byte_offset % 4
        or variable.byte_size != width * 4
        or variable.byte_offset + variable.byte_size > buffer.byte_size
    ):
        raise ShaderGraphError(
            f"structured material value {variable.name} has an unsupported layout"
        )
    components = []
    first_word = variable.byte_offset // 4
    for component_index in range(width):
        from .shader_graph_buffer import structured_word_member
        raw = (
            f"{resource_identifier}[{index_expression}]."
            f"{structured_word_member(buffer.byte_size, first_word + component_index)}"
        )
        components.append(
            raw
            if base == "uint"
            else f"as{base}({raw})"
        )
    if width == 1:
        return components[0]
    return f"{value_type}({', '.join(components)})"


@dataclass(frozen=True, slots=True)
class GeneratedShaderSource:
    source: str
    entry_point: str
    target: str
    ieee_strict: bool = False
    value_statements: tuple[str, ...] = ()
    native_16bit: bool = False
    unwritten_outputs: tuple[tuple[str, int, int], ...] = ()
    unassigned_values: bool = False
    # FXC's constant propagation loses the sign of stored zero even with /Gis and precise.
    signed_zero_constants: bool = False
    # Original/private register-space pairs for compiler-only structured views.
    structured_view_spaces: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class GeneratedGraphFunction:
    declarations: tuple[str, ...]
    body: tuple[str, ...]
    entry_parameters: tuple[tuple[str, str, str], ...] = ()


def generate_graph_function(graph, contract, build_body, *, value_resolver=None, memoize_values=False,
                            inline_regions=None, variable_prefix='reasyGraph', entry=False):
    from file_handlers.mmtr.material_region_read import region_read_session
    with region_read_session():
        return _generate_graph_source(graph, contract, build_body=build_body, value_resolver=value_resolver,
                                      memoize_values=memoize_values, inline_regions=inline_regions, variable_prefix=variable_prefix,
                                      entry=entry)


def generate_graph_hlsl(graph: ShaderGraph, contract: ShaderContract, *, early_depth_stencil=False) -> GeneratedShaderSource:
    if contract.stage == 'hull':
        if early_depth_stencil:
            raise ShaderGraphError('Early depth/stencil execution is only valid for pixel implementations.')
        from .shader_graph_hull import generate_hull_source
        return generate_hull_source(graph, contract)
    from file_handlers.mmtr.material_region_read import region_read_session
    with region_read_session():
        return _generate_graph_source(graph, contract, early_depth_stencil=early_depth_stencil)


_SAMPLING = re.compile(r'\.(?:Sample\w*|Gather\w*)\(')
_DECLARATION = re.compile(r'^(\s*)(?:precise\s+)?\w+(?:<[\w, ]*>)?\s+(\w+)\s*=\s*(.*);\s*$')
_NAME_TOKEN = re.compile(r'\b[A-Za-z_]\w*\b')
_ASSIGNED = re.compile(r'(?<![\w.])([A-Za-z_]\w*)(?:\.\w+|\[[^\]]*\])*\s*(?:[-+*/%&|^]|<<|>>)?=(?!=)'
                       r'|(?:\+\+|--)\s*([A-Za-z_]\w*)\b|\b([A-Za-z_]\w*)\s*(?:\+\+|--)')
_OUT_ARGUMENTS = re.compile(r'\b(?:sincos|modf|frexp|GetDimensions|Interlocked\w*)\s*\(')
_PURE_COPY = re.compile(r'\(?\s*(\w+)\s*\)?(?:\.[xyzw]{1,4})?')


def _line_names(line, cache):
    names = cache.get(line)
    if names is None:
        reads = frozenset(_NAME_TOKEN.findall(line))
        writes = reads if _OUT_ARGUMENTS.search(line) else frozenset(
            name for groups in _ASSIGNED.findall(line) for name in groups if name)
        names = cache[line] = (reads, writes)
    return names


class _SinkText:
    __slots__ = ("lines", "depths", "sampled", "names")

    def __init__(self, lines):
        self.lines = lines
        self.depths = [len(line) - len(line.lstrip()) if line.strip() else -1 for line in lines]
        self.sampled = [0]
        for line in lines:
            self.sampled.append(self.sampled[-1] + bool(_SAMPLING.search(line)))
        self.names = {}

    def samples(self, start, end):
        return self.sampled[end] > self.sampled[start]


def _sink_texture_reads(lines):
    text = _SinkText(list(lines))
    if not text.samples(0, len(text.lines)):
        return text.lines
    return _sink_block(text, 0, len(text.lines))


def _sink_block(text, start, end):
    if not text.samples(start, end):
        return text.lines[start:end]
    out, depths = [], text.depths
    while start < end:
        first = next((i for i in range(start, end) if depths[i] >= 0), None)
        if first is None:
            out.extend(text.lines[start:end])
            break
        indent, stop = depths[first], first + 1
        while stop < end and (depths[stop] < 0 or depths[stop] >= indent):
            stop += 1
        out.extend(text.lines[start:first])
        out.extend(_sink_statements(text, first, stop, indent))
        start = stop
    return out


def _sink_statements(text, start, end, indent):
    lines, depths = text.lines, text.depths
    if not text.samples(start, end):
        return lines[start:end]
    statements, cursor = [], start
    while cursor < end:
        stop = cursor + 1
        while stop < end and (depths[stop] < 0 or depths[stop] > indent or lines[stop].strip() in {'{', '}', '};'}
                              or lines[stop].lstrip().startswith('else')):
            stop += 1
        statements.append((cursor, stop))
        cursor = stop
    texts = []
    for begin, stop in statements:
        if not text.samples(begin + 1, stop):
            texts.append(lines[begin:stop])
            continue
        body, run = [lines[begin]], None
        for position in range(begin + 1, stop):
            if 0 <= depths[position] <= indent:
                if run is not None:
                    body.extend(_sink_block(text, run, position))
                    run = None
                body.append(lines[position])
            elif run is None and depths[position] >= 0:
                run = position
            elif run is None:
                body.append(lines[position])
        if run is not None:
            body.extend(_sink_block(text, run, stop))
        texts.append(body)
    declarations = [None] * len(statements)
    for number, (begin, stop) in enumerate(statements):
        if stop == begin + 1 and text.samples(begin, stop):
            declarations[number] = _DECLARATION.match(lines[begin])
            follower = number + 1
            while declarations[number] and follower < len(statements):
                begin, stop = statements[follower]
                copy = _DECLARATION.match(lines[begin]) if stop == begin + 1 else None
                if copy is None or not _PURE_COPY.fullmatch(copy.group(3).strip()):
                    break
                declarations[follower] = copy
                follower += 1
    if not any(match and _SAMPLING.search(match.group(3)) for match in declarations):
        return [line for body in texts for line in body]
    readers, writers = {}, {}
    for number, (begin, stop) in enumerate(statements):
        for position in range(begin, stop):
            reads, writes = _line_names(lines[position], text.names)
            for table, names in ((readers, reads), (writers, writes)):
                for name in names:
                    found = table.setdefault(name, [])
                    if not found or found[-1] != number:
                        found.append(number)

    def following(table, name, after):
        found = table.get(name, ())
        at = bisect.bisect_right(found, after)
        return found[at] if at < len(found) else len(statements)

    pending, moved, number = {}, set(), 0
    while number < len(statements):
        match = declarations[number]
        if match is None or not _SAMPLING.search(match.group(3)):
            number += 1
            continue
        names, stop = [match.group(2)], number + 1
        while stop < len(statements):
            copy = declarations[stop]
            source = copy and _PURE_COPY.fullmatch(copy.group(3).strip())
            if not source or source.group(1) not in names:
                break
            names.append(copy.group(2))
            stop += 1
        use = min(following(readers, name, stop - 1) for name in names)
        write = min((following(writers, name, stop - 1) for name in set(_NAME_TOKEN.findall(match.group(3)))),
                    default=len(statements))
        target = min(use, write)
        target = next((k for k in range(stop, target) if statements[k][1] - statements[k][0] > 1), target)
        if use < len(statements) and target > stop:
            pending.setdefault(target, []).extend(range(number, stop))
            moved.update(range(number, stop))
        number = stop
    result = []
    for number, body in enumerate(texts):
        for other in pending.get(number, ()):
            result.extend(texts[other])
        if number not in moved:
            result.extend(body)
    return result


class _NeedGraphExpression(Exception):
    def __init__(self, context, node, pin):
        self.context, self.node, self.pin = context, node, pin


class _GraphExpressionContext:
    __slots__ = ("_incoming", "_expression", "_expression_value")

    def __init__(self, incoming, expression, expression_value):
        self._incoming = incoming
        self._expression = expression
        self._expression_value = expression_value

    def incoming(self, node, pin_name, target_type):
        return self._incoming(self, node, pin_name, target_type)

    def expression(self, node, pin_name):
        return self._expression(self, node, pin_name)


# GLSL FMin/FMax leave NaN to the driver; HLSL min/max compile to NMin/NMax.
DRIVER_MIN_MAX_HELPERS = "\n".join(
    ("#ifndef REasyDriverMinMax_DEFINED", "#define REasyDriverMinMax_DEFINED", "#ifdef __spirv__")
    + tuple(f'[[vk::ext_instruction({code}, "GLSL.std.450")]] {t} REasyF{name}({t} a, {t} b);'
            for code, name in ((37, "Min"), (40, "Max")) for t in ("float", "float2", "float3", "float4"))
    + ("#else",)
    + tuple(f"{t} REasyF{name}({t} a, {t} b) {{ return {name.lower()}(a, b); }}"
            for name in ("Min", "Max") for t in ("float", "float2", "float3", "float4"))
    + ("#endif", "#endif"))


def _fxc_target(contract) -> bool:
    return contract.model_major < 6 and contract.vulkan_execution is None


def dxil_target(contract) -> bool:
    return contract.model_major >= 6 and contract.vulkan_execution is None



def _entry_parameters(extension_nodes: dict, contract: ShaderContract) -> dict[str, tuple[str, str, str]]:
    parameters = {}
    for node in extension_nodes.values():
        provider = shader_graph_node_extension(node.kind).entry_parameters_provider
        for name, domain, semantic in provider(node, contract) if provider else ():
            if name in parameters and parameters[name] != (name, domain, semantic):
                raise ShaderGraphError('Entry inputs with the same name have different meanings.')
            parameters[name] = name, domain, semantic
    return parameters


def _generate_graph_source(graph, contract, *, build_body=None, value_resolver=None, early_depth_stencil=False,
                           memoize_values=False, inline_regions=None, variable_prefix='reasyGraph', entry=False):
    if build_body is None and contract.stage not in SHADER_GRAPH_STAGES:
        raise ShaderGraphError(
            "This stage needs a primitive or dispatch execution adapter."
        )
    diagnostics = validate_graph(graph, contract) if build_body is None else ()
    errors = [item.message for item in diagnostics if item.severity == "error"]
    if errors:
        raise ShaderGraphError("\n".join(errors))
    automatic_regions = inline_regions is None
    if automatic_regions:
        inline_regions = contract.stage in {'geometry','mesh','amplification'} or contract.model_major < 6 and _graph_numeric_features(graph)[1]
    if any(node.kind in {'branch', 'repeat'} for node in graph.nodes):
        from file_handlers.mmtr.region_dependencies import specialize_regions
        graph = specialize_regions(graph)
        if automatic_regions and not inline_regions and build_body is None:
            from file_handlers.mmtr.region_inline_policy import inline_recovered_regions
            inline_regions = inline_recovered_regions(graph, contract)
    nodes = {item.node_id: item for item in graph.nodes}
    links = {(item.target_node, item.target_pin): item for item in graph.links}
    output = next((item for item in graph.nodes if item.kind == "output"), None)
    input_fields = {item.key: item for item in contract.inputs}
    field_names = {
        item.key: signature_member("in", item)
        for item in contract.inputs
    }
    output_names = {
        item.key: signature_member("out", item)
        for item in contract.outputs
    }
    used_buffers: set[str] = set()
    used_structured_buffers: set[str] = set()
    used_byte_address_buffers: set[str] = set()
    used_record_buffers: set[str] = set()
    used_textures: set[str] = set()
    used_samplers: set[str] = set()
    used_constant_views: set[str] = set()
    used_extension_nodes: dict[str, ShaderGraphNode] = {}
    inline_declarations = []
    inlined_nodes = set()
    inline_aliases = {}
    resource_coherence_policies: dict[str, bool] = {}
    resource_word_layouts: dict[str, set[int]] = {}
    from .dxil_structured_views import structured_view_spaces
    view_spaces = dict(structured_view_spaces(contract))
    buffer_helpers: dict[int, str] = {}
    node_evaluations: dict[str, ShaderNodeEvaluation] = {}
    storage_evaluations = {}
    resolved_ports = {}

    def ports(node):
        if node.node_id not in resolved_ports:
            resolved_ports[node.node_id] = {(pin.direction, pin.name): pin for pin in node_pins(node, contract)}
        return resolved_ports[node.node_id]
    parameter_identifiers = _constant_buffer_identifiers(contract.constant_buffers, reserved=(item.name for item in contract.resources))
    resource_identifiers: dict[str, str] = {}
    sampler_identifiers: dict[str, str] = {}
    stack: set[tuple[str, str]] = set()
    resolver_reads: set[str] = set()
    value_compiler = ShaderExpressionCompiler(variable_prefix=variable_prefix, define_unassigned=_fxc_target(contract)
                                              ) if build_body is None or memoize_values else None
    body: list[str] = []
    unwritten_outputs = []
    scheduling = False

    def incoming(context, node: ShaderGraphNode, pin_name: str, target_type: str) -> str:
        link = links.get((node.node_id, pin_name))
        if link is None:
            raise ShaderGraphError(f"{node_title(node)}: {pin_name} is unconnected")
        source = nodes[link.source_node]
        source_pin = ports(source).get(('output', link.source_pin))
        if source_pin is None:
            raise ShaderGraphError("a linked output pin no longer exists")
        return _coerce(context._expression(context, source, link.source_pin), source_pin.value_type, target_type)

    def expression(context, node: ShaderGraphNode, pin_name: str) -> str:
        nonlocal scheduling
        key = (node.node_id, pin_name)
        if value_compiler is None:
            return context._expression_value(context, node, pin_name)
        cached = value_compiler.cached(key, "shared")
        if cached is not None:
            return cached.value
        if scheduling:
            raise _NeedGraphExpression(context, node, pin_name)
        # Explicit stack: recursive evaluation exhausted Python's stack on long graphs.
        pending, active = [(node, pin_name)], {key}
        scheduling = True
        try:
            while pending:
                current, current_pin = pending[-1]
                current_key = current.node_id, current_pin
                if current_key not in active and value_compiler.cached(current_key, 'shared') is not None:
                    pending.pop()
                    continue
                active.add(current_key)
                try:
                    text = context._expression_value(context, current, current_pin)
                except _NeedGraphExpression as need:
                    if need.context is not context:
                        raise
                    dependency = need.node.node_id, need.pin
                    if dependency in active:
                        raise ShaderGraphError('shader graph contains a cycle')
                    if current.kind in _ORDERED_INPUT_KINDS and current.node_id not in resolver_reads:
                        rest = []
                        for port in ports(current).values():
                            link = links.get((current.node_id, port.name)) if port.direction == 'input' else None
                            if link is None:
                                continue
                            queued = link.source_node, link.source_pin
                            if queued != dependency and queued not in active \
                                    and value_compiler.cached(queued, 'shared') is None:
                                rest.append((nodes[link.source_node], link.source_pin))
                        pending.extend(reversed(rest))
                    active.add(dependency)
                    pending.append((need.node, need.pin))
                    continue
                pin = ports(current).get(('output', current_pin))
                if pin is None:
                    raise ShaderGraphError('An expression has no typed output.')
                materialize = (pin.value_type in _VALUE_TYPES
                    and current.kind not in {'input', 'function_input', 'unassigned_value', 'constant', 'parameter', 'shader_resource'}
                    and (shader_graph_node_extension(current.kind) is None or current.kind in _MATERIALIZED_EXTENSIONS))
                value_compiler.emit(current_key, 'shared', ShaderExpression(text, pin.value_type), materialize=materialize,
                                    precise=current.kind == 'precise')
                if materialize:
                    body.append('    ' + value_compiler.statements('shared')[-1])
                pending.pop()
                active.remove(current_key)
            return value_compiler.cached(key, 'shared').value
        finally:
            scheduling = False

    def expression_value(context, node: ShaderGraphNode, pin_name: str) -> str:
        if node.properties.get('precise'):
            from .shader_graph_precision import precise_operation
            value, _domain, helper = precise_operation(node, pin_name, contract,
                lambda pin, domain: context._incoming(context, node, pin, domain))
            inline_declarations.append(helper)
            return value
        if node.kind == 'precise' and value_compiler is None:
            raise ShaderGraphError('Calculation-order constraints require statement-owned expression generation.')
        recursion_key = (node.node_id, pin_name)
        if recursion_key in stack:
            raise ShaderGraphError("shader graph contains a cycle")
        stack.add(recursion_key)
        try:
            if node.kind == 'unassigned_value' and value_compiler is not None:
                from .shader_graph_unassigned import unassigned_type
                result = value_compiler.emit(('unassigned', node.node_id), 'shared',
                    ShaderExpression('', unassigned_type(node)), initialize=False)
                body.append('    ' + value_compiler.statements('shared')[-1])
                return result.value
            if value_resolver is not None:
                def resolver_read(name, target_type):
                    resolver_reads.add(node.node_id)
                    return context._incoming(context, node, name, target_type)
                resolved = value_resolver(node, pin_name, resolver_read)
                if resolved is not None:
                    return resolved
            kind = node.kind
            if kind in {'group_read','group_write','dispatch_mesh'}:
                from .shader_graph_storage import storage_statements
                if value_compiler is None:
                    raise ShaderGraphError('Shared data requires invocation-owned statement generation.')
                if node.node_id not in storage_evaluations:
                    variable = variable_prefix + 'Storage' + str(len(body))
                    statements, results = storage_statements(node, contract, lambda name,domain: context._incoming(context, node,name,domain), variable)
                    body.extend('    '+line for line in statements)
                    storage_evaluations[node.node_id] = results
                return storage_evaluations[node.node_id][pin_name]
            if kind in {'emit_vertex','end_primitive','mesh_output_counts','mesh_indices','mesh_vertex','mesh_primitive'}:
                from .shader_graph_geometry import geometry_statements
                from .shader_graph_mesh import MESH_EFFECTS, mesh_statements
                if value_compiler is None:
                    raise ShaderGraphError('Geometry effects require invocation-owned statement generation.')
                variable = variable_prefix + 'Vertex' + str(len(body))
                statements, result = (mesh_statements if kind in MESH_EFFECTS else geometry_statements)(node, contract,
                    lambda name, domain: context._incoming(context, node,name,domain), variable)
                body.extend('    '+line for line in statements)
                return result
            if kind == "input":
                key = _semantic_key(
                    str(node.properties.get("semantic", "")),
                    int(node.properties.get("semantic_index", 0)),
                )
                if key not in input_fields:
                    raise ShaderGraphError("stage input is absent from the selected contract")
                expression = f"input.{field_names[key]}"
                if contract.vulkan_execution is not None and input_fields[key].system_value == 'front-face':
                    expression = f"uint({expression})"
                return expression
            if kind == "constant":
                value_type = _operation_type(node)
                width = _type_width(value_type)
                values = list(node.properties.get("values", [0.0] * width))
                values = (values + [0.0] * width)[:width]
                return shader_value_literal(value_type, values)
            if kind == "parameter":
                buffer_name = str(node.properties.get("buffer", ""))
                variable_name = str(node.properties.get("variable", ""))
                buffer = contract.buffer(buffer_name)
                variable = resolve_material_name(variable_name, buffer.variables if buffer else (), key=_item_name)
                if buffer is None or variable is None:
                    raise ShaderGraphError('The parameter has no uniquely identified renderer field.')
                buffer_name, variable_name = buffer.name, variable.name
                used_buffers.add(buffer_name)
                identifier = parameter_identifiers[buffer_name, variable_name]
                if "field_path" in node.properties:
                    from .aggregate_values import constant_value_fields
                    buffer = contract.buffer(buffer_name)
                    variable = resolve_material_name(variable_name, buffer.variables if buffer else (), key=_item_name)
                    path = node.properties["field_path"]
                    if variable is None or not any(field.path == path for field in constant_value_fields(variable)):
                        raise ShaderGraphError("The parameter projection is outside its declared logical fields.")
                    identifier += path
                return identifier
            if kind == "shader_resource":
                resource = contract.resource(str(node.properties.get("resource", "")))
                if resource is None:
                    raise ShaderGraphError("The graph resource has no renderer binding.")
                coherent = node.properties.get('globally_coherent', False)
                key = resource.name
                if key in resource_coherence_policies and resource_coherence_policies[key] != coherent:
                    raise ShaderGraphError('A bound resource cannot have conflicting global coherence declarations.')
                resource_coherence_policies[key] = coherent
                if resource.input_type == 'constant-buffer' and not resource.sample_count:
                    if not dxil_target(contract):
                        raise ShaderGraphError('A constant-buffer row view needs a Shader Model 6 DXIL target.')
                    from .shader_graph_constant_buffer import constant_view_identifier
                    used_constant_views.add(key)
                    return constant_view_identifier(resource)
                if resource.texture_value_type:
                    used_textures.add(key)
                    return resource_identifiers.setdefault(key, _identifier(resource.name, prefix="Texture"))
                if resource.input_type == "sampler":
                    used_samplers.add(key)
                    return sampler_identifiers.setdefault(key, _identifier(resource.name, prefix="Sampler"))
                if resource.input_type in {'byte-address-buffer', 'rw-byte-address-buffer'}:
                    used_byte_address_buffers.add(key)
                    return resource_identifiers.setdefault(key, _identifier(resource.name, prefix="Buffer"))
                if resource.input_type in {'structured-buffer', 'rw-structured-buffer'}:
                    word_bits = node.properties.get('word_bits',32)
                    resource_word_layouts.setdefault(key, set()).add(word_bits)
                    used_record_buffers.add(key)
                    if word_bits == 64 and view_spaces:
                        return resource_identifiers.setdefault((key,64), _identifier(resource.name, prefix="Buffer") + '_REasyWords64')
                    return resource_identifiers.setdefault(key, _identifier(resource.name, prefix="Buffer"))
                raise ShaderGraphError("This resource needs a different typed renderer adapter.")
            if kind == "structured_parameter":
                resource_name = str(node.properties.get("resource", ""))
                variable_name = str(node.properties.get("variable", ""))
                index_semantic = str(
                    node.properties.get("index_semantic", "NOINTERPOLATOR")
                )
                index_semantic_index = int(
                    node.properties.get("index_semantic_index", 0)
                )
                index_component = str(
                    node.properties.get("index_component", "")
                ).casefold()
                buffer = contract.structured_buffer(resource_name)
                resource = contract.resource(resource_name)
                variable = resolve_material_name(variable_name, buffer.variables if buffer is not None else (), key=_item_name)
                index_key = _semantic_key(
                    index_semantic,
                    index_semantic_index,
                )
                if buffer is None or resource is None or variable is None:
                    raise ShaderGraphError(
                        f"instanced material value {resource_name}.{variable_name} is absent from the selected contract"
                    )
                if index_key not in input_fields:
                    raise ShaderGraphError(
                        f"instanced material index {index_semantic}{index_semantic_index} is absent from the selected contract"
                    )
                index_field = f"input.{field_names[index_key]}"
                if index_component:
                    if index_component not in {"x", "y", "z", "w"}:
                        raise ShaderGraphError(
                            "instanced material index uses an invalid input component"
                        )
                    index_field = f"({index_field}).{index_component}"
                elif _type_width(input_fields[index_key].value_type) != 1:
                    raise ShaderGraphError(
                        "instanced material index needs an explicit input component"
                    )
                resource_name = resource.name
                used_structured_buffers.add(resource_name)
                resource_identifier = resource_identifiers.setdefault(
                    resource_name,
                    _identifier(resource_name, prefix="MaterialInstances"),
                )
                return _structured_parameter_expression(
                    buffer,
                    variable,
                    resource_identifier,
                    f"uint({index_field})",
                )
            if kind == "texture_sample":
                texture_name = str(node.properties.get("texture", ""))
                sampler_name = str(node.properties.get("sampler", ""))
                resource = contract.resource(texture_name)
                if resource is None or not resource.sampleable:
                    raise ShaderGraphError(f"texture {texture_name!r} is not sampleable")
                sampler = contract.resource(sampler_name)
                if sampler is None or sampler.input_type != "sampler":
                    raise ShaderGraphError(f"sampler {sampler_name!r} is not bound by the pass")
                if sampler.flags & 0x2:
                    raise ShaderGraphError(
                        f"sampler {sampler_name!r} needs a high-level comparison-sampling node that is not implemented yet"
                    )
                texture_name, sampler_name = resource.name, sampler.name
                used_textures.add(texture_name)
                used_samplers.add(sampler_name)
                texture_id = resource_identifiers.setdefault(
                    texture_name, _identifier(texture_name, prefix="Texture")
                )
                sampler_id = sampler_identifiers.setdefault(
                    sampler_name, _identifier(sampler_name, prefix="Sampler")
                )
                if pin_name == "texture_object":
                    return texture_id
                if pin_name == "sampler_object":
                    return sampler_id
                sample = f"{texture_id}.Sample({sampler_id}, {context._incoming(context, node, 'coordinates', resource.coordinate_type)})"
                suffix, _ = texture_result_pins(resource.texture_value_type or 'float4')[pin_name]
                return f'({sample}){suffix}'
            if kind == "buffer_load":
                operation = BufferRead.from_properties(node.properties)
                if operation.dynamic_offset:
                    buffer_helpers.setdefault(operation.stride, operation.helpers())
                return operation.expression(lambda name, kind: context._incoming(context, node, name, kind))
            if kind in {'sample_texture', 'texture_load'}:
                try:
                    operation = TextureOperation.from_properties(kind, node.properties)
                    operation.validate_stage(contract.stage)
                    sample = operation.expression(lambda name, value_type: context._incoming(context, node, name, value_type))
                    suffix, _ = operation.outputs[pin_name]
                    return f'({sample}){suffix}'
                except ValueError as exc:
                    raise ShaderGraphError(str(exc)) from exc
            value_type = _operation_type(node)
            if kind in TYPED_MATH_NODES:
                try:
                    if context_error := math_context_error(kind, contract):
                        raise ValueError(context_error)
                    if helper := typed_math_helpers(kind, node.properties):
                        inline_declarations.append(helper)
                    return typed_math_expression(kind, node.properties, lambda name, value_type: context._incoming(context, node, name, value_type))[0]
                except ValueError as exc:
                    raise ShaderGraphError(str(exc)) from exc
            if kind in {"minimum", "maximum"} and node.properties.get("nan_policy") == "driver":
                function = "REasyFMin" if kind == "minimum" else "REasyFMax"
                return f"{function}({context._incoming(context, node, 'a', value_type)}, {context._incoming(context, node, 'b', value_type)})"
            if kind == "select":
                missing = {pin for pin in ('true','false')
                           if nodes[links[node.node_id,pin].source_node].kind=='unassigned_value'}
                if missing and value_compiler is not None:
                    # FXC rejects reading a definitely uninitialized local in a ternary but accepts a conditional assignment.
                    predicate = context._incoming(context, node,'condition','bool')
                    choices = {pin:context._incoming(context, node,pin,value_type) for pin in ('true','false') if pin not in missing}
                    result = value_compiler.emit(('partial_select',node.node_id),'shared',ShaderExpression('',value_type),initialize=False)
                    body.append('    '+value_compiler.statements('shared')[-1])
                    statements = tuple(f'if ({predicate if pin=="true" else "!("+predicate+")"}) {result.value} = {text};'
                                       for pin,text in choices.items())
                    value_compiler.append_statements('shared',statements)
                    body.extend('    '+line for line in statements)
                    return result.value
                return f"({context._incoming(context, node, 'condition', 'bool')} ? {context._incoming(context, node, 'true', value_type)} : {context._incoming(context, node, 'false', value_type)})"
            if (math := math_node_expression(node, value_type, lambda name, target_type: context._incoming(context, node, name, target_type))) is not None:
                return math[0]
            if kind == 'indexed_select' and value_compiler is not None:
                from .shader_graph_indexed_value import indexed_shape
                domain, count = indexed_shape(node)
                choices = [f'item{i}' for i in range(count)]
                if node.properties.get('has_default', True):
                    choices.append('fallback')
                missing = {pin for pin in choices if nodes[links[node.node_id, pin].source_node].kind == 'unassigned_value'}
                if missing:
                    index = context._incoming(context, node, 'index', 'uint')
                    values = {pin: context._incoming(context, node, pin, domain) for pin in choices if pin not in missing}
                    result = value_compiler.emit(('partial_indexed_select', node.node_id), 'shared',
                        ShaderExpression('', domain), initialize=False)
                    body.append('    ' + value_compiler.statements('shared')[-1])
                    statements = (f'switch ({index}) {{',
                        *(f'case {i}u: ' + (f'{result.value} = {values[pin]}; ' if pin in values else '') + 'break;'
                          for i, pin in enumerate(choices[:count])),
                        *((f'default: {result.value} = {values["fallback"]}; break;',) if 'fallback' in values else ()), '}')
                    value_compiler.append_statements('shared', statements)
                    body.extend('    ' + line for line in statements)
                    return result.value
            extension = shader_graph_node_extension(kind)
            if extension is not None:
                used_extension_nodes[node.node_id] = node
                if extension.evaluation is not None:
                    evaluation_key = (node.node_id, "evaluation")
                    cached = value_compiler.cached(evaluation_key, "shared") if value_compiler else None
                    if cached is None:
                        if kind in {'branch', 'repeat'} and value_compiler is not None:
                            for port in ports(node).values():
                                if port.direction == 'input':
                                    context._incoming(context, node, port.name, port.value_type)
                        if inline_regions and kind in {'branch', 'repeat'} and value_compiler is not None:
                            from file_handlers.mmtr.function_region import inline_region
                            prefix = 'reasyRegion_' + hashlib.sha256((variable_prefix + ':' + node.node_id).encode()).hexdigest()[:24]
                            lowered, evaluated, aliases = inline_region(node, contract,
                                lambda name, target_type: context._incoming(context, node, name, target_type), prefix,
                                unassigned_inputs={port.name for port in ports(node).values() if port.direction=='input'
                                    and nodes[links[node.node_id,port.name].source_node].kind=='unassigned_value'})
                            inline_aliases[node.node_id] = aliases
                            inline_declarations.extend(lowered.declarations)
                            value_compiler.append_statements('shared', lowered.body)
                            body.extend('    ' + line for line in lowered.body)
                            inlined_nodes.add(node.node_id)
                        else:
                            evaluated = extension.evaluation(node, lambda name, target_type: context._incoming(context, node, name, target_type))
                        cached = ShaderExpression(evaluated.value, evaluated.value_type)
                        if value_compiler is not None:
                            cached = value_compiler.emit(evaluation_key, "shared", cached, materialize=node.node_id not in inlined_nodes)
                            if node.node_id not in inlined_nodes:
                                body.append("    " + value_compiler.statements("shared")[-1])
                        node_evaluations[node.node_id] = evaluated
                    else:
                        evaluated = node_evaluations[node.node_id]
                    if pin_name in inline_aliases.get(node.node_id, {}):
                        return inline_aliases[node.node_id][pin_name]
                    forwarded = dict(evaluated.forwarded)
                    if pin_name in forwarded:
                        return forwarded[pin_name]
                    if pin_name in evaluated.direct:
                        return f"({cached.value})"
                    return f"({cached.value}).{dict(evaluated.members)[pin_name]}"
                return extension.expression(
                    node,
                    pin_name,
                    lambda name, target_type: context._incoming(context, node, name, target_type),
                )
            raise ShaderGraphError(f"node {node_title(node)} does not produce a value")
        finally:
            stack.remove(recursion_key)

    expression_context = _GraphExpressionContext(incoming, expression, expression_value)
    incoming = expression_context.incoming
    expression = expression_context.expression

    if build_body is not None:
        body.extend(build_body(expression, incoming))
    elif contract.outputs and contract.stage not in {'geometry','mesh'}:
        body.append("    REasyStageOutput output;")

        def unassigned_lane(node_id, lane):
            # Iterative: a self-referencing closure would keep the graph alive until a GC sweep.
            for depth in range(66):
                node = nodes[node_id]
                if node.kind == 'unassigned_value':
                    return True
                if depth > 64:
                    return False
                if node.kind == 'compose':
                    link = links.get((node_id, 'xyzw'[lane]))
                    if link is None:
                        return False
                    node_id, lane = link.source_node, 0
                elif node.kind == 'swizzle':
                    try:
                        mask = swizzle_mask(node)
                    except ShaderGraphError:
                        return False
                    link = links.get((node_id, 'value'))
                    if link is None or lane >= len(mask):
                        return False
                    node_id, lane = link.source_node, 'xyzw'.index(mask[lane])
                else:
                    return False
            return False

        for field in contract.outputs:
            target_type = field.value_type
            link = links[(output.node_id, field.pin_name)]
            source = nodes[link.source_node]
            if source.kind == 'unassigned_value':
                unwritten_outputs.append((field.semantic_name, field.semantic_index, field.mask))
                continue
            if source.kind in ('compose', 'swizzle'):
                components = 'xyzw'[:_type_width(target_type)]
                omitted = {index for index in range(len(components)) if unassigned_lane(source.node_id, index)}
                if len(omitted) == len(components):
                    unwritten_outputs.append((field.semantic_name, field.semantic_index, field.mask))
                    continue
                if omitted:
                    lanes = [lane for lane in range(4) if field.mask & (1 << lane)]
                    unwritten_outputs.append((field.semantic_name, field.semantic_index, sum(1 << lanes[index] for index in omitted)))
                    whole = incoming(output, field.pin_name, target_type) if source.kind == 'swizzle' else None
                    for index, pin in enumerate(components):
                        if index not in omitted:
                            value = f"({whole}).{pin}" if whole is not None else incoming(source, pin, _type_base(target_type))
                            body.append(f"    output.{output_names[field.key]}.{pin} = {value};")
                    continue
            body.append(
                f"    output.{output_names[field.key]} = {incoming(output, field.pin_name, target_type)};"
            )
    clip_link = links.get((output.node_id, "clip")) if output is not None else None
    if build_body is None and output is not None and (output.node_id, 'execution') in links:
        incoming(output, 'execution', 'execution')
    if build_body is None and contract.stage == "pixel" and clip_link is not None:
        body.append(f"    clip({incoming(output, 'clip', 'float')});")
    if build_body is None and contract.outputs and contract.stage not in {'geometry','mesh'}:
        body.append("    return output;")

    for extension_node in sorted(
        used_extension_nodes.values(),
        key=lambda item: (item.kind, item.node_id),
    ):
        extension = shader_graph_node_extension(extension_node.kind)
        dynamic_buffers = (
            extension.required_buffers_provider(extension_node, contract)
            if extension.required_buffers_provider is not None
            else ()
        )
        for buffer_name in dynamic_buffers:
            buffer = contract.buffer(buffer_name)
            if buffer is None:
                raise ShaderGraphError(
                    f"{extension.title} requires constant buffer {buffer_name}, which is absent from this pass"
                )
            used_buffers.add(buffer.name)

    if contract.vulkan_execution is not None and (build_body is None or entry):
        # Declare every contract binding so the compiled Vulkan module keeps the native descriptor layout.
        declared = (used_buffers | used_structured_buffers | used_record_buffers | used_textures
                    | used_samplers | used_byte_address_buffers)
        for resource in contract.resources:
            key = resource.name
            if key in declared:
                continue
            if resource.input_type == 'constant-buffer':
                if contract.buffer(key) is not None:
                    used_buffers.add(key)
            elif resource.texture_value_type:
                used_textures.add(key)
                resource_identifiers.setdefault(key, _identifier(resource.name, prefix="Texture"))
            elif resource.input_type == 'sampler':
                used_samplers.add(key)
                sampler_identifiers.setdefault(key, _identifier(resource.name, prefix="Sampler"))
            elif resource.input_type in {'byte-address-buffer', 'rw-byte-address-buffer'}:
                used_byte_address_buffers.add(key)
                resource_identifiers.setdefault(key, _identifier(resource.name, prefix="Buffer"))
            elif resource.input_type in {'structured-buffer', 'rw-structured-buffer'}:
                resource_word_layouts.setdefault(key, set()).add(32)
                used_record_buffers.add(key)
                resource_identifiers.setdefault(key, _identifier(resource.name, prefix="Buffer"))

    def binding_attribute(name):
        if contract.vulkan_execution is None:
            return ''
        binding = next((b for b in contract.vulkan_execution.bindings if b.name == name), None)
        resource = contract.resource(name)
        if binding is None or resource is None or (binding.register_class, binding.slot, binding.space) != (
                resource.register_class, resource.bind_point, resource.register_space):
            raise ShaderGraphError(f'{name} needs an explicit matching Vulkan descriptor binding.')
        return f'[[vk::binding({binding.binding}, {binding.descriptor_set})]] '

    declarations: list[str] = [
        "// Generated by REasy's reflected shader graph.",
        "// The selected pass contract is the source of all signatures and registers.",
        "",
    ]
    from .hlsl_contract import contract_structure_blocks, resource_array_suffix, resource_declaration
    declarations.extend(contract_structure_blocks(*(buffer for buffer in contract.constant_buffers if buffer.name in used_buffers)).values())
    for buffer in contract.constant_buffers:
        if buffer.name not in used_buffers:
            continue
        block = _emit_cbuffer(buffer, parameter_identifiers)[0]
        guard = _identifier(f'REasyCBuffer_{buffer.name}') + '_DEFINED'
        declarations.append(f'#ifndef {guard}' + chr(10) + f'#define {guard}' + chr(10)
                            + binding_attribute(buffer.name) + block + chr(10) + '#endif')
    if used_constant_views:
        from .shader_graph_constant_buffer import constant_view_declaration, constant_view_space
        view_space = constant_view_space(contract)
        for key in sorted(used_constant_views):
            declarations.append(constant_view_declaration(contract.resource(key), contract.buffer(key), view_space))
    for buffer in contract.structured_buffers:
        key = buffer.name
        if key not in used_structured_buffers:
            continue
        resource = contract.resource(buffer.name)
        if resource is None:
            raise ShaderGraphError(
                f"structured material buffer {buffer.name} has no reflected resource binding"
            )
        declarations.append(
            _emit_structured_buffer(
                buffer,
                resource,
                resource_identifiers[key],
                binding_attribute(key),
            )
        )
    for resource in contract.resources:
        key = resource.name
        if key not in used_record_buffers | used_textures | used_samplers | used_byte_address_buffers:
            continue
        array_suffix = resource_array_suffix(resource)
        if key in used_record_buffers and key not in used_structured_buffers:
            from .shader_graph_resource import object_declaration, resource_value_type
            for word_bits in sorted(resource_word_layouts.get(key,{32})):
                _name, declaration = structured_storage(resource.sample_count, word_bits=word_bits)
                private_view = word_bits == 64 and view_spaces
                identifier = resource_identifiers[(key,64) if private_view else key]
                space = view_spaces[resource.register_space] if private_view else resource.register_space
                typed = resource_value_type(resource, globally_coherent=resource_coherence_policies.get(key, False), word_bits=word_bits)
                declarations.extend((declaration, f"{binding_attribute(key)}{object_declaration(typed, identifier)} : "
                    f"{_hlsl_register(resource.register_class, resource.bind_point, space)};"))
        elif key in used_textures and resource.input_type == 'rw-typed':
            declarations.append(
                f"{binding_attribute(key)}{'globallycoherent ' if resource_coherence_policies.get(key, False) else ''}RW{_resource_hlsl_type(resource)} {resource_identifiers[key]}{array_suffix} : "
                f"{_hlsl_register(resource.register_class, resource.bind_point, resource.register_space)};"
            )
        elif key in used_textures or key in used_samplers or key in used_byte_address_buffers:
            identifier = sampler_identifiers[key] if key in used_samplers else resource_identifiers[key]
            declarations.append(resource_declaration(resource, identifier=identifier,
                                                     coherent=resource_coherence_policies.get(key, False),
                                                     attribute=binding_attribute(key)))
    if used_textures or used_samplers or used_byte_address_buffers:
        declarations.append("")

    storage_parameters = []
    if build_body is None and contract.stage in {'compute','mesh','amplification'}:
        from .shader_graph_storage import storage_environment
        storage_declarations, storage_parameters = storage_environment(graph,contract)
        declarations.extend(storage_declarations)
    declarations.extend(buffer_helpers.values())
    declarations.extend(dict.fromkeys(inline_declarations))
    emitted_helpers: set[str] = set()
    for extension_node in sorted(
        used_extension_nodes.values(),
        key=lambda item: (item.kind, item.node_id),
    ):
        extension = shader_graph_node_extension(extension_node.kind)
        dynamic_helpers = ''
        if extension.hlsl_helpers_provider is not None and extension_node.node_id not in inlined_nodes:
            if extension_node.kind in {'branch', 'repeat'}:
                from file_handlers.mmtr.function_region import compiled_region_helpers
                dynamic_helpers = compiled_region_helpers(extension_node, contract)
            else:
                dynamic_helpers = extension.hlsl_helpers_provider(extension_node, contract)
        if dynamic_helpers.strip() and dynamic_helpers not in emitted_helpers:
            declarations.append(dynamic_helpers.strip())
            declarations.append("")
            emitted_helpers.add(dynamic_helpers)

    if any(node.kind in {'minimum', 'maximum'} and node.properties.get('nan_policy') == 'driver'
           for node in nodes.values()):
        declarations.append(DRIVER_MIN_MAX_HELPERS)
        declarations.append("")

    if build_body is not None:
        return GeneratedGraphFunction(tuple(declarations), tuple(_sink_texture_reads(body)),
                                      tuple(_entry_parameters(used_extension_nodes, contract).values()))
    if contract.inputs or contract.stage in {'geometry','domain'}:
        declarations.extend(("struct REasyStageInput", "{"))
        for field in contract.inputs:
            if contract.stage in {'geometry','domain'} and not field.primitive_attribute:
                continue
            # Vulkan FrontFacing is bool; the renderer graph uses the uint carrier.
            value_type = 'bool' if contract.vulkan_execution is not None and field.system_value == 'front-face' else field.value_type
            builtin = {'base-vertex':'BaseVertex','base-instance':'BaseInstance','draw-index':'DrawIndex'}.get(field.system_value)
            attribute = f'[[vk::builtin("{builtin}")]] ' if contract.vulkan_execution is not None and builtin else ''
            declarations.append(signature_declaration(field, field_names[field.key], value_type, attribute))
        declarations.extend(("};", ""))
    primitive_attributes = []
    primitive_parameters = []
    if contract.stage == 'geometry':
        from .shader_graph_geometry import stage_declarations
        geometry_declarations, primitive_parameters, primitive_attributes = stage_declarations(contract)
        declarations.extend(geometry_declarations)
    if contract.stage=='domain':
        from .shader_graph_tessellation import domain_declarations
        patch_declarations,primitive_parameters,primitive_attributes = domain_declarations(contract)
        declarations.extend(patch_declarations)
    if contract.stage == 'mesh':
        from .shader_graph_mesh import mesh_declarations
        mesh_types, primitive_parameters, primitive_attributes = mesh_declarations(contract)
        declarations.extend(mesh_types)
    if contract.stage not in {'geometry','mesh'} and contract.outputs:
        declarations.extend(("struct REasyStageOutput", "{"))
        for field in contract.outputs:
            declarations.append(signature_declaration(field, output_names[field.key]))
        declarations.extend(("};", ""))
    return_type = "REasyStageOutput" if contract.outputs and contract.stage not in {'geometry','mesh'} else "void"
    parameters = primitive_parameters if contract.stage in {'geometry','domain','mesh'} else ['REasyStageInput input'] if contract.inputs else []
    entry_parameters = _entry_parameters(used_extension_nodes, contract)
    parameters.extend('{1} {0} : {2}'.format(*entry_parameters[name]) for name in sorted(entry_parameters))
    parameters.extend(storage_parameters)
    parameter = ', '.join(parameters)
    if contract.stage in {'compute','mesh','amplification'}:
        dimensions = contract.thread_group_size
        if dimensions is None or len(dimensions) != 3 or any(type(n) is not int or n < 1 for n in dimensions) or math.prod(dimensions) > (128 if contract.stage in {'mesh','amplification'} else 1024):
            raise ShaderGraphError('Dispatch behavior needs valid native thread-group dimensions.')
        invocation_inputs = (contract.stage == 'compute' and contract.vulkan_execution is not None and all(
            field.system_value in {'group-id', 'group-thread-id', 'dispatch-thread-id', 'group-index'}
            and field.register == 0xffffffff and field.component_type == 'uint32' and field.array_length == 1
            and field.mask == (1 if field.system_value == 'group-index' else 7) for field in contract.inputs))
        if contract.stage in {'compute','amplification'} and (contract.outputs or contract.inputs and not invocation_inputs):
            raise ShaderGraphError('Compute entries use invocation inputs and ordered effects, not raster signatures.')
        declarations.append('[numthreads(' + ', '.join(map(str, dimensions)) + ')]')
    if early_depth_stencil:
        if contract.stage != "pixel":
            raise ShaderGraphError("Early depth/stencil execution is only valid for pixel implementations.")
        declarations.append("[earlydepthstencil]")
    declarations.extend(primitive_attributes)
    declarations.append(f"{return_type} main({parameter})")
    declarations.append("{")
    declarations.extend(_sink_texture_reads(body))
    declarations.append("}")
    declarations.append("")
    source = "\n".join(declarations)
    source_bytes = len(source.encode('utf-8'))
    if source_bytes > MAX_SOURCE_LENGTH:
        raise ShaderGraphError(f'generated HLSL is {source_bytes} bytes, exceeding the {MAX_SOURCE_LENGTH}-byte source budget')
    native_16bit, unassigned_values, signed_zero, word_views = _graph_numeric_features(graph)
    return GeneratedShaderSource(source, "main", contract.target,
        value_statements=value_compiler.statements("shared") if value_compiler is not None else (),
        native_16bit=native_16bit, unwritten_outputs=tuple(unwritten_outputs),
        unassigned_values=unassigned_values, signed_zero_constants=signed_zero,
        structured_view_spaces=tuple(view_spaces.items()) if word_views else ())


def _graph_numeric_features(graph):
    pending = [graph.to_dict()]
    native_half, unassigned, signed_zero, word_views = False, False, False, False
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            word_views |= value.get('kind') == 'shader_resource' and value.get('properties', {}).get('word_bits') == 64
            if any(str(value.get(key, '')).rstrip('234') == 'half' for key in ('value_type', 'source_type')):
                native_half = True
            unassigned |= value.get('kind') == 'unassigned_value'
            if 'nodes' in value and 'links' in value:
                nodes = {node['id']: node for node in value['nodes']}
                negative_zero = {identity for identity, node in nodes.items() if node['kind'] == 'constant'
                    and any(isinstance(number, float) and number == 0 and math.copysign(1, number) < 0
                            for number in node.get('properties', {}).get('values', ()))}
                # IEEE comparisons treat +0 and -0 identically.
                signed_zero |= any(link['source'][0] in negative_zero
                    and nodes[link['target'][0]]['kind'] != 'compare' for link in value['links'])
            pending.extend(value.values())
        elif isinstance(value, (tuple, list)):
            pending.extend(value)
    return native_half, unassigned, signed_zero, word_views


@dataclass(slots=True)
class ShaderAuthoringProject:
    graph: ShaderGraph
    source_override: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": GRAPH_SCHEMA,
            "version": GRAPH_SCHEMA_VERSION,
            "graph": self.graph.to_dict(),
            "source_override": self.source_override,
        }

    def to_json(self) -> str:
        payload = json.dumps(self.to_dict(), separators=(',', ':'), sort_keys=True)
        if len(payload.encode('utf-8')) > MAX_PROJECT_LENGTH:
            raise ShaderGraphError('shader graph project is too large')
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "ShaderAuthoringProject":
        if not isinstance(value, dict) or value.get("schema") != GRAPH_SCHEMA:
            raise ShaderGraphError("file is not a REasy shader graph project")
        if int(value.get("version", -1)) != GRAPH_SCHEMA_VERSION:
            raise ShaderGraphError(
                f"unsupported shader graph project version {value.get('version')!r}"
            )
        source = value.get("source_override")
        if source is not None:
            if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_SOURCE_LENGTH:
                raise ShaderGraphError("shader source override is invalid or too large")
        return cls(ShaderGraph.from_dict(value.get("graph")), source)

    @classmethod
    def from_json(cls, text: str) -> "ShaderAuthoringProject":
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_PROJECT_LENGTH:
            raise ShaderGraphError("shader graph project is too large")
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ShaderGraphError(f"invalid shader graph JSON: {exc}") from exc
        return cls.from_dict(value)


__all__ = [
    "GeneratedShaderSource",
    "ShaderAuthoringProject",
    "ShaderContract",
    "ShaderContractBuffer",
    "ShaderContractIssue",
    "ShaderContractReport",
    "ShaderContractResource",
    "ShaderContractVariable",
    "ShaderExpression",
    "ShaderExpressionCompiler",
    "ShaderGraph",
    "ShaderGraphDiagnostic",
    "ShaderGraphError",
    "ShaderGraphLink",
    "ShaderGraphNode",
    "ShaderGraphNodeExtension",
    "ShaderPin",
    "connection_adapter",
    "connection_compatible",
    "generate_graph_hlsl",
    "new_shader_graph",
    "node_pins",
    "node_property_diagnostics",
    "node_title",
    "register_shader_graph_node_extension",
    "shader_graph_node_extension",
    "swizzle_mask",
    "texture_element_type",
    "texture_hlsl_type",
    "validate_graph",
    "validate_shader_contract",
]
