from __future__ import annotations

from .constant_words import ConstantWord, constant_word, word_tag, override_word, dynamic_word, OVERRIDE_PATTERN, normalize_word_expressions

from dataclasses import dataclass, replace
import pickle
import re
from typing import Mapping

from file_handlers.sdf.dxc import (
    assemble_dxil,
    compile_hlsl_dxc,
    disassemble_dxil,
    dxil_container_part,
    enrich_dxil_shader,
    optimize_dxil,
    validate_dxil,
)
from file_handlers.sdf.dxbc import parse_dxbc
from file_handlers.sdf import dxc, dxil_metadata as _dxil_metadata
from file_handlers.sdf.shader_graph import (
    ShaderContract,
    ShaderContractBuffer,
    ShaderContractResource,
    ShaderGraphError,
    ShaderSignatureField,
    validate_shader_contract,
)
from file_handlers.sdf.shader_graph import (
    HLSL_RESERVED_WORDS, _hlsl_variable_parts, _identifier, mask_components, signature_declaration, signature_member,
)

lazy from file_handlers.sdf.shader_graph_texture import texture_coordinate_width
lazy from file_handlers.sdf.hlsl_contract import (resource_identifier, resource_declaration as _resource_declaration, contract_structures as _contract_structures, contract_cbuffer as _contract_cbuffer)
from .re_engine_dxil_recovery import LLVM_NAME, ReEngineDxilSemanticIr
from .re_engine_bindless_material import material_record_accesses, relocate_material_record_words
from .re_engine_dxil_signatures import DxilEntryMetadata, carrier_input_rows, with_signature_rows
from .material_stage_inputs import stage_input_library
from ..native_build_cache import NativeBuildCache, native_build_key
from ..native_resource import DXIL_RESOURCE_CLASS_NUMBERS, DXIL_RESOURCE_CLASSES
from ..material_implementation import implementation_contract
lazy import fastdxiltext
lazy from file_handlers.bounded_cache import BoundedCache
lazy from ..native_resource import complete_typed_resources
lazy from .. import surface_splice_dxil
lazy from ..surface_splice_dxil import optimizer_passes, surface_helper_source
lazy from file_handlers.sdf.shader_graph_math import value_width
lazy from . import re_engine_dxil_worker as worker
lazy from ..worker_pool import shared_workers, sharing, worker_count


_SSA = re.compile('%' + LLVM_NAME)
_VALUE = re.compile(r'^(?P<indent>\s*)(?P<result>%' + LLVM_NAME + r')\s*=')

_CARRIER_CACHE_LIMIT = 128
_CARRIER_CACHE_BYTES = 8 * 1024 * 1024
_CARRIER_ANALYSIS_BYTES = 24 * 1024 * 1024
_CARRIER_ARGUMENTS = ("-Od",)
_DXIL_OPTIMIZER_PASSES = (
    "-hlsl-dxilload", "-always-inline", "-dxil-dfe",
    "-globaldce", "-strip-dead-prototypes",
    "-hlsl-dxilfinalize", "-hlsl-dxilemit",
)
_DXIL_BUILD_CONFIGURATION = ("material-dxil-v1", "REasyShaderGraph.hlsl", "-Ges", "-O3", "-WX", "-Qstrip_debug")
_NATIVE_BUILD_CACHE = NativeBuildCache()
_NATIVE_ACTIVITY = {"compile": 0, "validate": 0}
_LAST_NATIVE_BATCH = {}


def native_build_activity():
    return dict(_NATIVE_ACTIVITY)


def native_build_batch_info():
    return dict(_LAST_NATIVE_BATCH)


def _native_pipeline_functions():
    return (compile_hlsl_dxc, disassemble_dxil, optimize_dxil,
            assemble_dxil, validate_dxil, dxil_container_part, dxc._operation_result,
            enrich_dxil_shader, parse_dxbc, ShaderContract.from_shader.__func__,
            implementation_contract, complete_typed_resources,
            validate_shader_contract, _merge_entry, _attach_reflection,
            _constant_extracts, _execute_dxil_job,
            _prepare_dxil_job, _entry_name, _dxil_job_key, stage_input_library,
            DxilEntryMetadata.parse.__func__, DxilEntryMetadata._properties,
            DxilEntryMetadata.input_interpolation, DxilEntryMetadata.output_interpolation,
            DxilEntryMetadata.patch_interpolation, DxilEntryMetadata.primitive_execution,
            DxilEntryMetadata.mesh_execution, DxilEntryMetadata.primitive_input_count,
            DxilEntryMetadata.thread_group_size, DxilEntryMetadata.parse_entries.__func__,
            fastdxiltext.entry_tables, _dxil_metadata.metadata_operands, fastdxiltext.metadata_operands,
            fastdxiltext.function_spans, fastdxiltext.merge_entry, fastdxiltext.attach_reflection,
            fastdxiltext.constant_extracts,
            fastdxiltext.always_inline, carrier_input_rows,
            surface_splice_dxil.apply_surface_plan, surface_splice_dxil.place_surface_code,
            surface_splice_dxil.surface_helper_source,
            surface_splice_dxil.optimizer_passes)


def _native_build_runtime():
    return (dxc._library(), *_native_pipeline_functions())


def _bounded_region_cache(name: str, key, byte_limit: int, build, cost):
    from ..material_region_read import region_derived_cache

    cache = region_derived_cache(name)
    if cache is None:
        return build()
    entries = cache.get("entries") or cache.setdefault("entries", BoundedCache())
    cached = entries.get(key)
    if cached is None:
        cached = build()
        entries.put(key, cached, cost(cached), _CARRIER_CACHE_LIMIT, byte_limit)
    return cached


def _region_entry(name: str, key):
    from ..material_region_read import region_derived_cache

    cache = region_derived_cache(name)
    entries = cache.get("entries") if cache is not None else None
    return entries.get(key) if entries is not None else None


def _carrier_cost(source: str, result) -> int:
    return len(source.encode("utf-8")) + len(result.bytecode) + len(result.diagnostics.encode("utf-8"))


def _compile_carrier(source: str, *, entry_point: str, target: str, arguments=None):
    arguments = _CARRIER_ARGUMENTS if arguments is None else tuple(arguments)

    def build():
        _NATIVE_ACTIVITY["compile"] += 1
        return compile_hlsl_dxc(source, entry_point=entry_point, target=target, extra_arguments=arguments)
    return _bounded_region_cache("re_engine_dxil_carriers", (source, entry_point, target, arguments),
                                 _CARRIER_CACHE_BYTES, build, lambda result: _carrier_cost(source, result))


def carrier_blob(job) -> bytes | None:
    """The job's compiled carrier as this process cached it, pickled for the workers whose jobs share it; None when
    it is not cached or does not pickle. Its analysis, several times larger, is cheap to redo."""
    key = job.source, job.entry_point, job.carrier_contract.target, tuple(job.carrier_arguments)
    compiled = _region_entry("re_engine_dxil_carriers", key)
    if compiled is None:
        return None
    try:
        return pickle.dumps((key, compiled), protocol=pickle.HIGHEST_PROTOCOL)
    except (TypeError, ValueError, RecursionError, pickle.PickleError):
        return None


def remember_carrier(blob: bytes) -> None:
    """Caches a carrier another worker compiled, as compiling it here would have."""
    key, compiled = pickle.loads(blob)
    _bounded_region_cache("re_engine_dxil_carriers", key, _CARRIER_CACHE_BYTES, lambda: compiled,
                          lambda result: _carrier_cost(key[0], result))


@dataclass(frozen=True, slots=True)
class ReEngineDxilBuild:
    bytecode: bytes
    contract: ShaderContract


@dataclass(frozen=True)
class _PreparedCarrier:
    contract: ShaderContract
    assembly: str
    metadata: DxilEntryMetadata
    reflection: str
    reflection_metadata: DxilEntryMetadata


def _prepare_carrier(bytecode: bytes) -> _PreparedCarrier:
    return _bounded_region_cache(
        "re_engine_dxil_carrier_analysis", bytecode, _CARRIER_ANALYSIS_BYTES, lambda: _analysed_carrier(bytecode),
        lambda result: (len(bytecode) + len(repr(result.contract).encode("utf-8"))
                        + 3 * (len(result.assembly.encode("utf-8")) + len(result.reflection.encode("utf-8")))))


def _analysed_carrier(bytecode: bytes) -> _PreparedCarrier:
    shader = enrich_dxil_shader(parse_dxbc(bytecode), bytecode)
    assembly = disassemble_dxil(bytecode)
    metadata = DxilEntryMetadata.parse(assembly.splitlines())
    # Same 64-bit texel completion as the source contract; RDEF says "uint".
    contract = complete_typed_resources(ShaderContract.from_shader(shader), metadata, assembly.splitlines())
    part = dxil_container_part(bytecode, "STAT")
    if len(part) < 24 or part[8:12] != b"DXIL":
        raise ShaderGraphError("DXC carrier has no typed DXIL reflection module.")
    start = 8 + int.from_bytes(part[16:20], "little")
    size = int.from_bytes(part[20:24], "little")
    if start < 24 or start + size != len(part) or part[start:start + 4] != b"BC\xc0\xde":
        raise ShaderGraphError("DXC reflection bitcode has an invalid program boundary.")
    reflection = disassemble_dxil(part[start:start + size])
    reflected_metadata = DxilEntryMetadata.parse(reflection.splitlines())
    return _PreparedCarrier(contract, assembly, metadata, reflection, reflected_metadata)


def _entry_name(metadata: DxilEntryMetadata) -> str:
    result = metadata.function_name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result):
        raise ShaderGraphError(
            f"DXIL entry point {result!r} cannot be represented canonically in HLSL."
        )
    return result


def _resource_object(resource: ShaderContractResource) -> str:
    identifier = resource_identifier(resource)
    return identifier + ("[0]" if int(resource.bind_count) != 1 else "")


def _touch_buffer(
    buffer: ShaderContractBuffer,
    identifiers: Mapping[str, str],
    block: str,
) -> str:
    def scalar_access(variable, value: str) -> tuple[str, str] | None:
        declaration, axes = _hlsl_variable_parts(variable)
        declaration = declaration.casefold()
        value += "[0]" * axes.count("[")
        if variable.members:
            member = min(variable.members, key=lambda item: int(item.byte_offset))
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", member.name):
                return None
            return scalar_access(member, value + "." + member.name)
        if variable.matrix:
            value += "[0][0]"
        elif re.search(r"(?:float|int|uint)[2-4]$", declaration):
            value += ".x"
        return value, declaration

    for variable in sorted(buffer.variables, key=lambda item: item.byte_offset):
        identifier = identifiers.get(variable.name)
        access = scalar_access(variable, identifier) if identifier else None
        if access is None:
            continue
        value, declaration = access
        if "uint" in declaration and "float" not in declaration:
            return f"REasyCarrierBits ^= uint({value});"
        if "int" in declaration and "uint" not in declaration:
            return f"REasyCarrierBits ^= asuint(int({value}));"
        return f"REasyCarrierBits ^= asuint(float({value}));"
    opaque = re.search(r"\buint\s+(REasyOpaque_[A-Za-z0-9_]+)\b", block)
    if opaque is not None:
        return f"REasyCarrierBits ^= {opaque.group(1)};"
    raise ShaderGraphError(
        f"Constant buffer {buffer.name} has no semantically declarable carrier field."
    )


def _texture_dimensions(resource: ShaderContractResource, value: str) -> list[str]:
    dimension = resource.dimension
    rows = ["REasyCarrierA = 0u;", "REasyCarrierB = 0u;", "REasyCarrierC = 0u;", "REasyCarrierD = 0u;"]
    if resource.input_type == "rw-typed":
        arguments = {
            "buffer": "REasyCarrierA",
            "texture1d": "REasyCarrierA",
            "texture1d-array": "REasyCarrierA, REasyCarrierB",
            "texture2d": "REasyCarrierA, REasyCarrierB",
            "texture2d-array": "REasyCarrierA, REasyCarrierB, REasyCarrierC",
            "texture3d": "REasyCarrierA, REasyCarrierB, REasyCarrierC",
        }.get(dimension)
        if arguments is None:
            raise ShaderGraphError(
                f"RW texture {resource.name} has unsupported dimension {dimension}."
            )
        rows.append(f"{value}.GetDimensions({arguments});")
    elif dimension == "texture1d":
        rows.append(f"{value}.GetDimensions(0u, REasyCarrierA, REasyCarrierB);")
    elif dimension in {"texture2d", "texture-cube", "texture1d-array"}:
        rows.append(
            f"{value}.GetDimensions(0u, REasyCarrierA, REasyCarrierB, REasyCarrierC);"
        )
    elif dimension == "texture2d-ms":
        rows.append(
            f"{value}.GetDimensions(REasyCarrierA, REasyCarrierB, REasyCarrierC);"
        )
    elif dimension in {"texture2d-array", "texture3d", "texture-cube-array"}:
        rows.append(
            f"{value}.GetDimensions(0u, REasyCarrierA, REasyCarrierB, REasyCarrierC, REasyCarrierD);"
        )
    elif dimension == "texture2d-ms-array":
        rows.append(
            f"{value}.GetDimensions(REasyCarrierA, REasyCarrierB, REasyCarrierC, REasyCarrierD);"
        )
    elif dimension in {"buffer", "texture-buffer"}:
        rows.append(f"{value}.GetDimensions(REasyCarrierA);")
    else:
        raise ShaderGraphError(
            f"Texture {resource.name} has unsupported dimension {dimension}."
        )
    rows.append("REasyCarrierBits ^= REasyCarrierA ^ REasyCarrierB ^ REasyCarrierC ^ REasyCarrierD;")
    return rows


def _structured_sink_member(layout: ShaderContractBuffer | None, stride: int) -> tuple[str, str]:
    if layout is None:
        return ("slot0" if stride == 4 else "slot0.x"), "uint"
    if not layout.variables or min(v.byte_offset for v in layout.variables) != 0:
        return "REasyReserved_0", "uint"
    field = min(layout.variables, key=lambda v: v.byte_offset)
    path = _identifier(field.name)
    while True:
        declaration, axes = _hlsl_variable_parts(field)
        path += "[0]" * axes.count("[")
        if not field.members:
            break
        field = min(field.members, key=lambda v: v.byte_offset)
        path += "." + _identifier(field.name)
    if declaration in {"uint64_t", "int64_t", "double"}:
        return path, declaration
    scalar = re.fullmatch(r"(?:(?:row_major|column_major)\s+)?(float|int|uint)([1-4]?)(?:x([1-4]))?", declaration)
    if scalar is None:
        raise ShaderGraphError("The temporary structured UAV has no supported typed scalar.")
    path += "[0][0]" if scalar[3] else ".x" if scalar[2] else ""
    return path, scalar[1]


def _touch_resource(resource: ShaderContractResource) -> list[str]:
    value = _resource_object(resource)
    if resource.input_type == "raytracing-acceleration-structure":
        return [
            "{", "RayQuery<RAY_FLAG_NONE> query;",
            "RayDesc ray; ray.Origin = 0.0; ray.Direction = float3(0, 0, 1); ray.TMin = 0; ray.TMax = 1;",
            f"query.TraceRayInline({value}, RAY_FLAG_NONE, 0xff, ray);",
            "query.Proceed(); REasyCarrierBits ^= query.CommittedStatus();", "}",
        ]
    if resource.input_type in {"texture", "texture-buffer", "rw-typed"}:
        return _texture_dimensions(resource, value)
    if resource.input_type in {"structured-buffer", "rw-structured-buffer"}:
        return [
            "REasyCarrierA = 0u;",
            "REasyCarrierB = 0u;",
            f"{value}.GetDimensions(REasyCarrierA, REasyCarrierB);",
            "REasyCarrierBits ^= REasyCarrierA ^ REasyCarrierB;",
        ]
    if resource.input_type in {"byte-address-buffer", "rw-byte-address-buffer"}:
        return [
            "REasyCarrierA = 0u;",
            f"{value}.GetDimensions(REasyCarrierA);",
            "REasyCarrierBits ^= REasyCarrierA;",
        ]
    if resource.input_type in {"sampler", "constant-buffer"}:
        return []
    raise ShaderGraphError(
        f"Resource {resource.name} has no semantic carrier operation."
    )


def _input_value(field, identifier: str, component: str) -> str:
    value = f"input.{identifier}"
    physical = mask_components(field.mask)
    if len(physical) > 1:
        value += f".{'xyzw'[physical.index(component)]}"
    if field.component_type in {"uint32", "sint32"}:
        value = f"asfloat({value})"
    return value


def _carrier_source(
    contract: ShaderContract,
    *,
    entry_point: str,
    scalar_expressions: Mapping[ConstantWord, str],
    helpers: str = "",
    material_expressions: Mapping[int, str] | None = None,
    material_buffer_size: int = 0,
    thread_group_size: tuple[int, int, int] | None = None,
    touch_contract: bool = True,
    retained_resources: frozenset[str] = frozenset(),
    surface_plan: tuple = (),
) -> str:
    if contract.stage not in {"pixel", "vertex", "compute"}:
        raise ShaderGraphError(f"Material DXIL carrier does not yet support {contract.stage} shaders.")
    lines = [
        "// Canonical REasy material-contract carrier.",
        "// Every declaration is generated from the semantic SDF/DXIL contract.",
        "",
        _contract_structures(*contract.constant_buffers, *contract.structured_buffers),
    ]
    touches: list[str] = []
    # HLSL cbuffer members share one global scope, so later buffers with the same field name use an alias.
    global_names: dict[str, tuple[str, int, int]] = {}

    def unique_variable_name(buffer, variable):
        name = str(variable.name)
        owner = (str(buffer.name), int(buffer.register_space), int(buffer.bind_point))
        if name not in HLSL_RESERVED_WORDS and global_names.setdefault(name, owner) == owner:
            return name
        alias = _identifier(f"REasy_{buffer.name}_{name}")
        while alias in global_names:
            alias += "_"
        global_names[alias] = owner
        return alias

    for buffer in contract.constant_buffers:
        block, identifiers = _contract_cbuffer(buffer, unique_variable_name)
        lines.append(block)
        if touch_contract or buffer.name in retained_resources:
            touches.append(_touch_buffer(buffer, identifiers, block))

    resources = [item for item in contract.resources if item.input_type != "constant-buffer"]
    declared: set[str] = set()
    for resource in resources:
        identifier = resource_identifier(resource)
        if identifier in declared:
            raise ShaderGraphError(
                f"Resources collide on canonical HLSL name {identifier}."
            )
        declared.add(identifier)
        structured_layout = next((b for b in contract.structured_buffers if b.name == resource.name), None)
        lines.extend((_resource_declaration(resource, structured_layout), ""))
        if touch_contract or resource.name in retained_resources:
            touches.extend(_touch_resource(resource))

    if material_expressions or any(edit.material for edit in surface_plan):
        shared = next((r for r in contract.resources if r.name == "BindlessBuffer"), None)
        table = next((r for r in contract.resources if r.name == "BindlessConstantBufferViewTbl"), None)
        if table is not None:
            if material_buffer_size <= 0 or material_buffer_size % 16:
                raise ShaderGraphError("Material constant-buffer size is not register aligned.")
            lines.extend((
                f"struct REasyMaterialWords {{ uint4 words[{material_buffer_size // 16}]; }};",
                "float REasyMaterialLoad(uint address, uint byteOffset)", "{",
                "    ConstantBuffer<REasyMaterialWords> material = ResourceDescriptorHeap[NonUniformResourceIndex(address)];",
                "    return asfloat(material.words[byteOffset >> 4][(byteOffset >> 2) & 3u]);", "}", "",
            ))
        elif shared is not None and shared.input_type == "structured-buffer" and shared.sample_count in (16, 256):
            shared_layout = next((b for b in contract.structured_buffers if b.name == shared.name), None)
            field_name = "slot0"
            if shared_layout is not None:
                if (len(shared_layout.variables) != 1 or shared_layout.variables[0].byte_offset != 0
                        or shared_layout.variables[0].hlsl_declaration != "uint4"
                        or shared_layout.variables[0].array_length * 16 != shared.sample_count):
                    raise ShaderGraphError("The material shared buffer is not a uint4 block array.")
                field_name = _identifier(shared_layout.variables[0].name)
            load_expression = (
                f"{resource_identifier(shared)}[(address + byteOffset) >> 8].{field_name}[((address + byteOffset) >> 4) & 15u][((address + byteOffset) >> 2) & 3u]"
                if shared.sample_count == 256 else
                f"{resource_identifier(shared)}[address + (byteOffset >> 4)].{field_name}[(byteOffset >> 2) & 3u]"
            )
            lines.extend((
                "float REasyMaterialLoad(uint address, uint byteOffset)", "{",
                f"    return asfloat({load_expression});",
                "}", "",
            ))
        else:
            raise ShaderGraphError("Material parameter overrides have no native storage backend.")

    input_ids = {
        field.key: signature_member("in", field)
        for field in contract.inputs
    }
    output_ids = {
        field.key: signature_member("out", field)
        for field in contract.outputs
    }
    if contract.inputs:
        lines.extend(("struct REasyStageInput", "{"))
        for field in contract.inputs:
            lines.append(signature_declaration(field, input_ids[field.key]))
        lines.extend(("};", ""))
    if contract.outputs:
        lines.extend(("struct REasyStageOutput", "{"))
        for field in contract.outputs:
            lines.append(signature_declaration(field, output_ids[field.key], interpolation=""))
        lines.extend(("};", ""))

    if helpers.strip():
        if len(helpers.encode("utf-8")) > 1024 * 1024:
            raise ShaderGraphError("Material helper source is too large.")
        lines.extend((helpers.strip(), ""))

    arguments: list[tuple[str, str]] = []
    registers: dict[int, dict[str, str]] = {}
    for field in contract.inputs:
        physical = mask_components(field.mask)
        for index, component in enumerate(physical):
            name = f"REasyIn_{input_ids[field.key]}_{component}"
            arguments.append(("float", name))
            if field.register != 0xFFFFFFFF:
                registers.setdefault(int(field.register), {})[component] = name
    parameter_list = ", ".join(f"{kind} {name}" for kind, name in arguments)
    for key, expression in sorted(scalar_expressions.items()):
        helper_name = f"REasyOverride_{word_tag(key)}"
        lines.extend((f"[noinline] float {helper_name}({parameter_list})", "{"))
        for register, values in sorted(registers.items()):
            rendered = ", ".join(values.get(component, "0.0") for component in "xyzw")
            lines.append(f"    float4 v{register} = float4({rendered});")
        lines.append(f"    return float({expression});")
        lines.extend(("}", ""))
    material_parameters = ", ".join(filter(None, (parameter_list, "uint REasyMaterialAddress")))
    for byte_offset, expression in sorted((material_expressions or {}).items()):
        lines.extend((f"[noinline] float REasyMaterialOverride_o{byte_offset}({material_parameters})", "{"))
        for register, values in sorted(registers.items()):
            rendered = ", ".join(values.get(component, "0.0") for component in "xyzw")
            lines.append(f"    float4 v{register} = float4({rendered});")
        lines.extend((f"    return float({expression});", "}", ""))
    surface_lines, surface_calls = surface_helper_source(surface_plan, arguments, registers)
    lines.extend(surface_lines)
    return_type = "REasyStageOutput" if contract.outputs else "void"
    parameter = "REasyStageInput input" if contract.inputs else ""
    if contract.stage == "compute":
        if thread_group_size is None:
            raise ShaderGraphError("Compute shader has no native thread-group declaration.")
        lines.append(f"[numthreads({', '.join(map(str, thread_group_size))})]")
    lines.extend((f"{return_type} {entry_point}({parameter})", "{"))
    lines.extend(
        (
            "    uint REasyCarrierBits = 0u;",
            "    uint REasyCarrierA = 0u;",
            "    uint REasyCarrierB = 0u;",
            "    uint REasyCarrierC = 0u;",
            "    uint REasyCarrierD = 0u;",
        )
    )
    for field in contract.inputs:
        identifier = input_ids[field.key]
        for index, component in enumerate(mask_components(field.mask)):
            value = f"input.{identifier}"
            if len(mask_components(field.mask)) > 1:
                value += f".{'xyzw'[index]}"
            if field.component_type == "float32":
                lines.append(f"    REasyCarrierBits ^= asuint({value});")
            else:
                lines.append(f"    REasyCarrierBits ^= uint({value});")
    lines.extend(f"    {item}" for item in touches)
    helper_arguments = []
    for field in contract.inputs:
        identifier = input_ids[field.key]
        physical = mask_components(field.mask)
        for component in physical:
            helper_arguments.append(_input_value(field, identifier, component))
    rendered_arguments = ", ".join(helper_arguments)
    for key in sorted(scalar_expressions):
        lines.append(
            f"    REasyCarrierBits ^= asuint(REasyOverride_{word_tag(key)}({rendered_arguments}));"
        )
    material_arguments = ", ".join(filter(None, (rendered_arguments, "0u")))
    for byte_offset in sorted(material_expressions or {}):
        lines.append(f"    REasyCarrierBits ^= asuint(REasyMaterialOverride_o{byte_offset}({material_arguments}));")
    for name, scalar, placeholders in surface_calls:
        call = f"{name}({', '.join(filter(None, (rendered_arguments, *placeholders)))})"
        lines.append(f"    REasyCarrierBits ^= {'asuint' if scalar == 'float' else 'uint'}({call});")
    if any(site.exit is not None and not edit.output for edit in surface_plan for site in edit.sites):
        # A Discard edit's code calls the discard operation, which the merged module must declare.
        lines.append("    if (REasyCarrierBits == 0x7f812345u) discard;")

    # Samplers are invisible to reflection unless used, so the carrier samples each one through a compatible texture.
    heap_texture_declared = False
    for sampler in (item for item in resources if touch_contract and item.input_type == "sampler"):
        comparison = bool(int(sampler.flags) & 0x2)
        sampleable = next(
            (
                item
                for item in resources
                if item.input_type == "texture"
                and item.return_type in {"float", "unorm", "snorm"}
                and item.dimension
                in (
                    {
                        "texture1d",
                        "texture1d-array",
                        "texture2d",
                        "texture2d-array",
                        "texture-cube",
                        "texture-cube-array",
                    }
                    if comparison
                    else {
                        "texture1d",
                        "texture1d-array",
                        "texture2d",
                        "texture2d-array",
                        "texture3d",
                        "texture-cube",
                        "texture-cube-array",
                    }
                )
            ),
            None,
        )
        if sampleable is None:
            if (contract.model_major, contract.model_minor) < (6, 6):
                raise ShaderGraphError(
                    f"Sampler {sampler.name} has no sampleable texture in its native contract."
                )
            if not heap_texture_declared:
                lines.append("    Texture2D<float4> REasyCarrierHeapTexture = ResourceDescriptorHeap[0u];")
                heap_texture_declared = True
            texture = "REasyCarrierHeapTexture"
            dimension = "texture2d"
        else:
            texture = _resource_object(sampleable)
            dimension = sampleable.dimension
        sampler_value = _resource_object(sampler)
        width = texture_coordinate_width(dimension)
        components = ["0.0"] * width
        if "cube" in dimension:
            components[2] = "1.0"  # A cube samples along a nonzero direction.
        coordinate = components[0] if width == 1 else f"float{width}({', '.join(components)})"
        if comparison:
            sample = f"{texture}.SampleCmpLevelZero({sampler_value}, {coordinate}, 0.0)"
        else:
            sample = f"{texture}.SampleLevel({sampler_value}, {coordinate}, 0.0).x"
        lines.append(f"    REasyCarrierBits ^= asuint(float({sample}));")

    if contract.outputs:
        lines.append("    REasyStageOutput output = (REasyStageOutput)0;")
        for field in contract.outputs:
            width = len(mask_components(field.mask))
            base = {
                "float32": "float",
                "uint32": "uint",
                "sint32": "int",
            }.get(field.component_type)
            if base is None:
                raise ShaderGraphError(
                    f"Output {field.key} has unsupported type {field.component_type}."
                )
            scalar = (
                "asfloat(REasyCarrierBits)"
                if base == "float"
                else f"{base}(REasyCarrierBits)"
            )
            value = scalar if width == 1 else f"{base}{width}({', '.join(scalar for _ in range(width))})"
            lines.append(f"    output.{output_ids[field.key]} = {value};")
        lines.extend(("    return output;", "}"))
    elif contract.stage == "pixel":
        lines.extend(("    if (REasyCarrierBits == 0x7f812345u) discard;", "}"))
    elif not (contract.inputs or contract.resources or contract.constant_buffers
              or scalar_expressions or material_expressions):
        lines.append("}")
    else:
        sink = next((item for item in resources if item.register_class == "u"), None)
        if sink is None:
            raise ShaderGraphError("Void material carrier needs an output UAV.")
        name = _resource_object(sink)
        if sink.input_type == "rw-byte-address-buffer":
            statement = f"{name}.Store(0u, REasyCarrierBits);"
        elif sink.input_type == "rw-structured-buffer":
            layout = next((b for b in contract.structured_buffers if b.name == sink.name), None)
            member, scalar = _structured_sink_member(layout, sink.sample_count)
            value = ("REasyCarrierBits" if scalar == "uint" else f"{scalar}(REasyCarrierBits)" if scalar in {"uint64_t", "int64_t", "double"}
                     else f"as{scalar}(REasyCarrierBits)")
            statement = f"{name}[0].{member} = {value};"
        elif sink.input_type == "rw-typed":
            width = 1 if sink.dimension == "buffer" else texture_coordinate_width(sink.dimension, load=True)
            if width is None:
                raise ShaderGraphError(f"Unsupported carrier UAV dimension {sink.dimension}.")
            coordinate = "0u" if width == 1 else f"uint{width}({', '.join(['0u'] * width)})"
            value = "asfloat(REasyCarrierBits)" if sink.return_type == "float" else "REasyCarrierBits"
            statement = f"{name}[{coordinate}] = {value};"
        else:
            raise ShaderGraphError(f"Unsupported carrier output {sink.input_type}.")
        lines.extend((f"    {statement}", "}"))
    lines.append("")
    result = "\n".join(lines)
    if len(result.encode("utf-8")) > 4 * 1024 * 1024:
        raise ShaderGraphError("DXIL carrier source exceeds the safety limit.")
    return result


def _rewrite_legacy_handles(
    entry: list[str],
    source: ShaderContract,
    candidate: ShaderContract,
    texture_overrides: Mapping[tuple[int, int], tuple[int, int]],
) -> list[str]:
    return fastdxiltext.legacy_handles(entry, source, candidate, texture_overrides, DXIL_RESOURCE_CLASSES,
                                       DXIL_RESOURCE_CLASS_NUMBERS)


def _rewrite_modern_handles(
    entry: list[str],
    source: ShaderContract,
    candidate: ShaderContract,
    texture_overrides: Mapping[tuple[int, int], tuple[int, int]],
) -> list[str]:
    return fastdxiltext.modern_handles(entry, source, candidate, texture_overrides, DXIL_RESOURCE_CLASSES,
                                       DXIL_RESOURCE_CLASS_NUMBERS)


def _constant_extracts(
    entry: list[str],
    contract: ShaderContract,
    *,
    dynamic_bindings: set[tuple[int, int]] | None = None,
    dynamic_extracts: dict | None = None,
    read_ranges: dict | None = None,
) -> dict[ConstantWord, list[str]]:
    return fastdxiltext.constant_extracts(entry, contract, DXIL_RESOURCE_CLASSES, dynamic_bindings, dynamic_extracts,
                                          read_ranges)


def _carrier_override_calls(
    carrier_entry: list[str],
    wanted: set[ConstantWord],
    *,
    material_address: str | None = None,
    namespace: str = "reasy",
) -> tuple[list[str], dict[ConstantWord, str]]:
    definitions: dict[str, str] = {}
    order: list[str] = []
    calls: dict[ConstantWord, str] = {}
    helper = re.compile(
        r"REasyMaterialOverride_o(?P<offset>\d+)"
        if material_address is not None
        else OVERRIDE_PATTERN
    )
    for line in carrier_entry[1:-1]:
        match = _VALUE.match(line)
        if match is not None:
            definitions[match.group("result")] = line
            order.append(match.group("result"))
        helper_match = helper.search(line)
        if helper_match is not None and match is not None:
            key = ((0, int(helper_match["offset"])) if material_address is not None else override_word(helper_match))
            if key in wanted:
                calls[key] = match.group("result")
                if material_address is not None:
                    updated, count = re.subn(r"i32 0\)", "i32 %reasy.native.material.address)", line)
                    if count != 1:
                        raise ShaderGraphError("Material helper address argument is missing.")
                    definitions[match.group("result")] = updated
    if set(calls) != wanted:
        missing = sorted(wanted - set(calls))
        raise ShaderGraphError(f"DXIL carrier omitted material overrides: {missing!r}")
    needed = set(calls.values())
    pending = list(needed)
    while pending:
        value = pending.pop()
        line = definitions.get(value)
        if line is None:
            raise ShaderGraphError("DXIL material helper has an external SSA dependency.")
        definition_match = _VALUE.match(line)
        body = line[definition_match.end() :] if definition_match is not None else line
        for dependency in _SSA.findall(body):
            if dependency in definitions and dependency not in needed:
                needed.add(dependency)
                pending.append(dependency)
    selected = [definitions[value] for value in order if value in needed]
    rename = {value: f"%{namespace}.{index}" for index, value in enumerate(order) if value in needed}

    def rewritten(line: str) -> str:
        return _SSA.sub(
            lambda match: (
                material_address
                if match[0] == "%reasy.native.material.address"
                else rename.get(match[0], match[0])
            ),
            line,
        )

    return (
        [rewritten(line) for line in selected],
        {key: rename[value] for key, value in calls.items()},
    )


def _apply_constant_overrides(
    entry: list[str],
    carrier_entry: list[str],
    contract: ShaderContract,
    expressions: Mapping[ConstantWord, str],
) -> list[str]:
    if not expressions:
        return entry
    dynamic_extracts = {}
    extracts = _constant_extracts(entry, contract, dynamic_extracts=dynamic_extracts)
    wanted = {key for key in expressions
              if key in extracts or dynamic_word(key) in dynamic_extracts}
    if not wanted:
        return entry
    prologue, results = _carrier_override_calls(carrier_entry, wanted)
    integer_extracts = {
        match[1] for line in entry
        if "extractvalue %dx.types.CBufRet.i32" in line
        and (match := re.match(r"\s*(%\S+)\s*=\s*extractvalue %dx\.types\.CBufRet\.i32\b", line))
    }
    replacements = {}
    integer_results = {}
    for key in sorted(wanted):
        integer_result = f"%reasy.bits_{word_tag(key)}"
        if (any(source in integer_extracts for source in extracts.get(key, ()))
                or any(scalar == "i32" for _source, _register, scalar
                       in dynamic_extracts.get(dynamic_word(key), ()))):
            prologue.append(f"  {integer_result} = bitcast float {results[key]} to i32")
            integer_results[key] = integer_result
        replacements.update(
            (source, integer_result if source in integer_extracts else results[key])
            for source in extracts.get(key, ())
        )
    additions = {}
    sequence = 0
    for address, reads in dynamic_extracts.items():
        keys = sorted(key for key in wanted if dynamic_word(key) == address)
        for source, register, scalar in reads:
            previous = source
            lines = []
            for key in keys:
                condition, selected = f"%reasy.dynamic.test{sequence}", f"%reasy.dynamic.value{sequence}"
                sequence += 1
                value_type = "i32" if scalar == "i32" else "float"
                override = integer_results[key] if scalar == "i32" else results[key]
                lines.extend((
                    f"  {condition} = icmp eq i32 {register}, {key[1] // 16}",
                    f"  {selected} = select i1 {condition}, {value_type} {override}, {value_type} {previous}",
                ))
                previous = selected
            if lines:
                additions[source] = lines
                replacements[source] = previous
    insertion = 1
    if insertion < len(entry) and re.fullmatch(r"[-$._A-Za-z0-9]+:", entry[insertion].strip()):
        insertion += 1
    updated = [*entry[:insertion], *prologue, *entry[insertion:]]
    final: list[str] = []
    for line in updated:
        match = _VALUE.match(line)
        definition = match.group("result") if match is not None else ""
        final.append(
            _SSA.sub(
                lambda token: (
                    token.group(0)
                    if token.group(0) == definition
                    else replacements.get(token.group(0), token.group(0))
                ),
                line,
            )
        )
        for added in additions.get(definition, ()):
            final.append(_SSA.sub(lambda token: token[0] if token[0] == definition
                                  else replacements.get(token[0], token[0]), added))
    return final


def _apply_material_overrides(entry, carrier_entry, contract, expressions, entry_metadata):
    if not expressions:
        return entry
    additions: dict[int, list[str]] = {}
    replacements = {}
    for index, access in enumerate(material_record_accesses(entry, contract, entry_metadata=entry_metadata)):
        reads = [word for word in access.words if word.byte_offset in expressions]
        if not reads:
            continue
        wanted = {(0, word.byte_offset) for word in reads}
        prologue, results = _carrier_override_calls(
            carrier_entry, wanted, material_address=access.address,
            namespace=f"reasy.material{index}",
        )
        for word in reads:
            value = results[(0, word.byte_offset)]
            if word.scalar_type == "i32":
                integer = f"%reasy.material{index}.bits.{word.byte_offset}"
                instruction = f"  {integer} = bitcast float {value} to i32"
                if instruction not in prologue:
                    prologue.append(instruction)
                value = integer
            replacements[word.value] = value
        additions.setdefault(min(word.line_index for word in reads), []).extend(prologue)
    if not replacements:
        raise ShaderGraphError("Material parameter edit did not reach a native word load.")
    result = []
    for index, line in enumerate(entry):
        result.extend(additions.get(index, ()))
        definition = _VALUE.match(line)
        name = definition["result"] if definition else ""
        result.append(_SSA.sub(
            lambda token: token[0] if token[0] == name else replacements.get(token[0], token[0]),
            line,
        ))
    return result


def _merge_entry(
    original: str,
    carrier: str,
    *,
    entry_point: str,
    source_contract: ShaderContract,
    candidate_contract: ShaderContract,
    scalar_expressions: Mapping[ConstantWord, str],
    texture_overrides: Mapping[tuple[int, int], tuple[int, int]],
    material_expressions: Mapping[int, str],
    material_word_offsets: Mapping[int, int],
    indexed_reads=(),
    native_metadata: DxilEntryMetadata | None = None,
    carrier_metadata: DxilEntryMetadata | None = None,
    surface_plan: tuple = (),
) -> str:
    steps = None
    if scalar_expressions or material_expressions or any(
            source != target for source, target in material_word_offsets.items()):
        def steps(body, carrier_entry, metadata):
            body = _apply_constant_overrides(body, carrier_entry, candidate_contract, scalar_expressions)
            body = _apply_material_overrides(body, carrier_entry, candidate_contract, material_expressions, metadata)
            return relocate_material_record_words(body, candidate_contract, dict(material_word_offsets),
                                                  entry_metadata=metadata)
    return fastdxiltext.merge_entry(
        original, carrier, entry_point, native_metadata, carrier_metadata, indexed_reads, source_contract,
        candidate_contract, texture_overrides, surface_plan, steps,
        (DxilEntryMetadata.parse, carrier_input_rows, value_width, DXIL_RESOURCE_CLASSES, DXIL_RESOURCE_CLASS_NUMBERS))


def _attach_reflection(assembly: str, carrier: bytes, *, prepared: _PreparedCarrier | None = None) -> str:
    prepared = prepared or _prepare_carrier(carrier)
    return fastdxiltext.attach_reflection(assembly, prepared, DXIL_RESOURCE_CLASSES)


@dataclass(frozen=True, slots=True)
class _NativeDxilJob:
    assembly: str
    source: str
    source_contract: ShaderContract
    candidate_contract: ShaderContract
    carrier_contract: ShaderContract
    metadata: tuple
    expressions: tuple
    texture_overrides: tuple
    material_expressions: tuple
    material_word_offsets: tuple
    indexed_reads: tuple
    entry_point: str
    material_buffer_size: int
    carrier_arguments: tuple
    optimizer_passes: tuple
    configuration: tuple
    surface_plan: tuple = ()


def _dxil_job_key(runtime, job):
    return native_build_key(runtime,
        job.assembly, job.source, job.source_contract, job.candidate_contract,
        job.expressions, job.texture_overrides, job.material_expressions,
        job.material_word_offsets, job.indexed_reads, job.entry_point,
        job.material_buffer_size, job.carrier_arguments, job.optimizer_passes,
        job.configuration, job.surface_plan)


def _prepare_dxil_job(
    ir: ReEngineDxilSemanticIr,
    source_contract: ShaderContract,
    candidate_contract: ShaderContract,
    *,
    scalar_expressions: Mapping[ConstantWord, str] | None = None,
    texture_binding_overrides: Mapping[tuple[int, int], tuple[int, int]] | None = None,
    helpers: str = "",
    material_expressions: Mapping[int, str] | None = None,
    material_buffer_size: int = 0,
    material_word_offsets: Mapping[int, int] | None = None,
    surface_plan: tuple = (),
):
    expressions = normalize_word_expressions(scalar_expressions)
    texture_overrides = dict(texture_binding_overrides or {})
    material_expressions = dict(material_expressions or {})
    material_word_offsets = dict(material_word_offsets or {})
    original_assembly = ir.to_assembly()
    native_metadata = DxilEntryMetadata.parse(original_assembly.splitlines())
    added_inputs, added_outputs = (
        tuple(field for field in candidate if field.key not in {item.key for item in source})
        for candidate, source in ((candidate_contract.inputs, source_contract.inputs),
                                  (candidate_contract.outputs, source_contract.outputs)))
    if added_inputs or added_outputs:
        lines = with_signature_rows(original_assembly.splitlines(), native_metadata, inputs=added_inputs,
                                    outputs=added_outputs)
        original_assembly = "\n".join(lines) + "\n"
        native_metadata = DxilEntryMetadata.parse(lines)
    entry_point = _entry_name(native_metadata)
    carrier_contract = candidate_contract
    if candidate_contract.stage == "pixel":
        # Container reflection omits interpolation; use the native DXIL signature.
        modes = native_metadata.input_interpolation()
        carrier_contract = replace(candidate_contract, inputs=tuple(
            replace(field, interpolation=modes.get(field.key, field.interpolation))
            for field in candidate_contract.inputs
        ))
    indexed_reads = ()
    if candidate_contract.stage in {"mesh", "amplification", "geometry", "domain", "hull"}:
        if candidate_contract.stage in {"mesh", "amplification"} and candidate_contract.inputs:
            raise ShaderGraphError("Mesh/amplification inputs must use their native payload, not a vertex signature.")
        if candidate_contract.stage in {"geometry", "domain", "hull"}:
            count = native_metadata.primitive_input_count(candidate_contract.stage)
            indexed_reads, rewritten = stage_input_library(candidate_contract,
                (*expressions.values(), *material_expressions.values(), helpers),
                {f.register: count for f in candidate_contract.inputs if f.register != 0xFFFFFFFF})
            expressions = dict(zip(expressions, rewritten[:len(expressions)]))
            material_expressions = dict(zip(material_expressions, rewritten[len(expressions):-1]))
            helpers = rewritten[-1]
        carrier_contract = replace(candidate_contract, stage="pixel",
            primitive_execution=None, mesh_execution=None, thread_group_size=None,
            inputs=tuple(r.carrier for r in indexed_reads), patch_constants=(), outputs=(
            ShaderSignatureField("SV_Target", 0, 0, "float32", 15, 0, "target", 0),
        ))
    from ..material_region_read import per_build
    thread_group_size = native_metadata.thread_group_size()
    source = per_build("carrier_sources", (
        carrier_contract, entry_point, tuple(expressions.items()), helpers, tuple(material_expressions.items()),
        material_buffer_size, thread_group_size, tuple(surface_plan),
    ), lambda: _carrier_source(
        carrier_contract,
        entry_point=entry_point,
        scalar_expressions=expressions,
        helpers=helpers,
        material_expressions=material_expressions,
        material_buffer_size=material_buffer_size,
        thread_group_size=thread_group_size,
        surface_plan=tuple(surface_plan),
    ))
    passes = list(_DXIL_OPTIMIZER_PASSES)
    if surface_plan:
        passes[passes.index("-always-inline") + 1:passes.index("-always-inline") + 1] = optimizer_passes(surface_plan)
    if added_inputs or added_outputs:
        # The native view-ID state describes the native signature; the added rows need it recomputed.
        passes.insert(passes.index("-hlsl-dxilemit"), "-viewid-state")
    job = _NativeDxilJob(
        original_assembly, source, source_contract, candidate_contract, carrier_contract,
        (native_metadata.reference, native_metadata.operands,
         tuple(native_metadata.definitions.items()), tuple(native_metadata.storage_types.items()),
         native_metadata.payload_type),
        tuple(expressions.items()), tuple(texture_overrides.items()),
        tuple(material_expressions.items()), tuple(material_word_offsets.items()),
        tuple(indexed_reads), entry_point, material_buffer_size,
        tuple(_CARRIER_ARGUMENTS), tuple(passes), tuple(_DXIL_BUILD_CONFIGURATION), tuple(surface_plan),
    )
    runtime = _native_build_runtime()
    return job, _dxil_job_key(runtime, job), runtime


def _execute_dxil_job(job: _NativeDxilJob) -> ReEngineDxilBuild:
    reference, operands, definitions, storage_types, payload_type = job.metadata
    native_metadata = DxilEntryMetadata(reference, operands, dict(definitions), dict(storage_types), payload_type)
    try:
        carrier_result = _compile_carrier(
            job.source,
            entry_point=job.entry_point,
            target=job.carrier_contract.target,
            arguments=job.carrier_arguments,
        )
        prepared = _prepare_carrier(carrier_result.bytecode)
    except ValueError as exc:
        raise ShaderGraphError(f"DXC could not build the native material contract: {exc}") from exc
    reflected_carrier = prepared.contract
    carrier_report = validate_shader_contract(
        replace(job.carrier_contract, inputs=reflected_carrier.inputs, outputs=reflected_carrier.outputs), reflected_carrier,
    )
    if not carrier_report.compatible:
        raise ShaderGraphError(
            "Canonical HLSL carrier changed the native ABI:\n"
            + "\n".join(item.message for item in carrier_report.errors)
        )
    merged = _merge_entry(
        job.assembly,
        prepared.assembly,
        entry_point=job.entry_point,
        source_contract=job.source_contract,
        candidate_contract=reflected_carrier,
        scalar_expressions=dict(job.expressions),
        texture_overrides=dict(job.texture_overrides),
        material_expressions=dict(job.material_expressions),
        material_word_offsets=dict(job.material_word_offsets),
        indexed_reads=job.indexed_reads,
        native_metadata=native_metadata,
        carrier_metadata=prepared.metadata,
        surface_plan=job.surface_plan,
    )
    try:
        merged = fastdxiltext.always_inline(merged)
        bitcode = optimize_dxil(merged, job.optimizer_passes)
        rebuilt = assemble_dxil(_attach_reflection(disassemble_dxil(bitcode), carrier_result.bytecode, prepared=prepared)).bytecode
        _NATIVE_ACTIVITY["validate"] += 1
        rebuilt = validate_dxil(rebuilt)
        shader = enrich_dxil_shader(parse_dxbc(rebuilt), rebuilt)
    except ValueError as exc:
        raise ShaderGraphError(f"DXC rejected rebuilt material semantics: {exc}") from exc
    # Container reflection omits primitive/mesh declarations and some linkage.
    contract = implementation_contract(ShaderContract.from_shader(shader), rebuilt)
    report = validate_shader_contract(job.candidate_contract, contract)
    if not report.compatible:
        raise ShaderGraphError(
            "Rebuilt DXIL changed the native ABI:\n"
            + "\n".join(item.message for item in report.errors)
        )
    return ReEngineDxilBuild(rebuilt, contract)


def rebuild_re_engine_dxil(
    ir: ReEngineDxilSemanticIr,
    source_contract: ShaderContract,
    candidate_contract: ShaderContract,
    *,
    scalar_expressions: Mapping[ConstantWord, str] | None = None,
    texture_binding_overrides: Mapping[tuple[int, int], tuple[int, int]] | None = None,
    helpers: str = "",
    material_expressions: Mapping[int, str] | None = None,
    material_buffer_size: int = 0,
    material_word_offsets: Mapping[int, int] | None = None,
    constant_view_overrides: Mapping[tuple[int, int], tuple[int, int]] | None = None,
    surface_plan: tuple = (),
) -> ReEngineDxilBuild:
    overrides = dict(texture_binding_overrides or {})
    overrides.update({('b', *view): target for view, target in (constant_view_overrides or {}).items()})
    job, cache_key, runtime = _prepare_dxil_job(
        ir, source_contract, candidate_contract, scalar_expressions=scalar_expressions,
        texture_binding_overrides=overrides, helpers=helpers,
        material_expressions=material_expressions, material_buffer_size=material_buffer_size,
        material_word_offsets=material_word_offsets, surface_plan=surface_plan,
    )
    cached = _NATIVE_BUILD_CACHE.get(cache_key)
    if cached is not None:
        return cached
    result = _execute_dxil_job(job)
    if _native_build_runtime() == runtime:
        _NATIVE_BUILD_CACHE.put(cache_key, result)
    return result


def _native_worker_runtime(runtime):
    if runtime[1:] != _DEFAULT_NATIVE_PIPELINE:
        return None
    return dxc._runtime_identity(runtime[0])


class NativeDxilBatch:
    """DXIL rebuilds asked for one at a time. With ``stream``, worker processes take them as they come once
    MIN_PARALLEL_JOBS wait; otherwise ``results`` hands them all over at once."""

    def __init__(self, *, stream: bool = False):
        self.stream = stream
        self.items: list = []
        self.prepared, self.owners, self.aliases = {}, {}, {}
        self.workers = self.failure = self.runtime = None
        self.info = {"requests": 0, "hits": 0, "misses": 0, "coalesced": 0,
                     "workers": 0, "submitted": 0, "worker_compiles": 0, "worker_validations": 0,
                     "sequential_jobs": 0, "peak_pending_input_bytes": 0,
                     "oversized_or_mutable": 0}

    def add(self, ir, source_contract, candidate_contract, options) -> int:
        index = len(self.items)
        self.items.append(None)
        try:
            job, key, runtime = _prepare_dxil_job(ir, source_contract, candidate_contract, **options)
            cached = _NATIVE_BUILD_CACHE.get(key)
            if cached is not None:
                self.items[index] = cached
                self.info["hits"] += 1
                return index
            if key is not None and key in self.owners:
                self.aliases[index] = self.owners[key]
                self.info["coalesced"] += 1
                return index
            self.prepared[index] = job, key, runtime
            if key is not None:
                self.owners[key] = index
        except Exception as exc:
            self.items[index] = exc
            return index
        if self.stream and self.failure is None:
            try:
                if self.workers is not None:
                    self._feed(index)
                    self.workers.pump()
                elif sharing() and self._start():
                    self.workers.pump()
            except Exception as exc:
                self.failure = exc
        return index

    def _start(self) -> bool:
        """Workers take every prepared job once enough wait and all share one verified runtime."""
        if len(self.prepared) < worker.MIN_PARALLEL_JOBS or worker_count() <= 1:
            return False
        first_job, _first_key, first_runtime = next(iter(self.prepared.values()))
        same_runtime = all(runtime == first_runtime
                           and job.configuration == tuple(_DXIL_BUILD_CONFIGURATION)
                           for job, _key, runtime in self.prepared.values())
        identity = _native_worker_runtime(first_runtime) if same_runtime else None
        if identity is None:
            self.stream = False
            return False
        self.runtime = first_runtime
        self.workers = worker.NativeWorkers(identity, first_job.configuration, self._publish)
        for index in self.prepared:
            self._feed(index)
        return True

    def _feed(self, index):
        job, _key, runtime = self.prepared[index]
        if runtime == self.runtime and job.configuration == tuple(_DXIL_BUILD_CONFIGURATION):
            self.workers.feed(index, job)

    def _publish(self, outcome):
        if outcome.index not in self.prepared or self.items[outcome.index] is not None:
            raise RuntimeError("DXIL worker returned an unexpected or repeated job")
        if outcome.error_type:
            message = outcome.error_message
            if outcome.error_type != "ShaderGraphError":
                message = f"{outcome.error_type}: {message}"
            self.items[outcome.index] = ShaderGraphError(message)
        else:
            if not isinstance(outcome.result, ReEngineDxilBuild):
                raise RuntimeError("DXIL worker returned an invalid native result")
            self.items[outcome.index] = outcome.result
            _NATIVE_BUILD_CACHE.put(self.prepared[outcome.index][1], outcome.result, outcome.size)
        _NATIVE_ACTIVITY["compile"] += outcome.compiles
        _NATIVE_ACTIVITY["validate"] += outcome.validations

    def results(self) -> tuple:
        if self.failure is not None:
            raise self.failure
        with shared_workers():
            if self.workers is not None or self._start():
                info, _sequential = self.workers.finish()
                self.info.update(info)
        results = self.items
        for index, (job, key, runtime) in self.prepared.items():
            if results[index] is not None:
                continue
            self.info["sequential_jobs"] += 1
            try:
                result = _execute_dxil_job(job)
                if _native_build_runtime() == runtime:
                    _NATIVE_BUILD_CACHE.put(key, result)
                results[index] = result
            except Exception as exc:
                results[index] = exc
        for index, owner in self.aliases.items():
            results[index] = results[owner]
        self.info["requests"], self.info["misses"] = len(results), len(self.prepared)
        _LAST_NATIVE_BATCH.clear()
        _LAST_NATIVE_BATCH.update(self.info)
        return tuple(results)


def rebuild_re_engine_dxil_batch(requests):
    batch = NativeDxilBatch()
    for request in requests:
        batch.add(*request)
    return batch.results()


_DEFAULT_NATIVE_PIPELINE = _native_pipeline_functions()


__all__ = [
    "NativeDxilBatch",
    "ReEngineDxilBuild",
    "rebuild_re_engine_dxil",
    "rebuild_re_engine_dxil_batch",
]
