from __future__ import annotations

from .constant_words import ConstantWord, constant_word, word_tag, override_word, dynamic_word, OVERRIDE_PATTERN, normalize_word_expressions

from dataclasses import dataclass, replace
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
from .re_engine_dxil_recovery import _GLOBAL, _TYPE, LLVM_NAME, ReEngineDxilSemanticIr
from .re_engine_bindless_material import material_record_accesses, relocate_material_record_words
from .re_engine_dxil_signatures import DxilEntryMetadata, map_carrier_inputs, with_signature_rows
from . import re_engine_dxil_signatures as _dxil_signatures
from .material_stage_inputs import stage_input_library
from ..native_build_cache import NativeBuildCache, native_build_key
from ..native_resource import DXIL_RESOURCE_CLASS_NUMBERS, DXIL_RESOURCE_CLASSES
from ..material_implementation import implementation_contract
lazy from file_handlers.bounded_cache import BoundedCache
lazy from ..native_resource import complete_typed_resources
lazy from .. import surface_splice_dxil
lazy from ..surface_splice_dxil import apply_surface_plan, optimizer_passes, place_surface_code, surface_helper_source
lazy from . import re_engine_dxil_worker as worker


_DEFINE = re.compile(r'^define\b.*?@(?P<name>' + LLVM_NAME + r')\(')
_DECLARE = re.compile(r'^declare\b.*?@(?P<name>' + LLVM_NAME + r')\(')
_METADATA_DEFINITION = re.compile(r'^!(?P<id>\d+)\s*=')
_SSA = re.compile('%' + LLVM_NAME)
_MODULE_REFERENCE = re.compile('@' + LLVM_NAME)
_VALUE = re.compile(r'^(?P<indent>\s*)(?P<result>%' + LLVM_NAME + r')\s*=')
_CALL = re.compile(r'@(?P<name>' + LLVM_NAME + r')\(')
_SYMBOL_PREFIXES = {_DEFINE: "define", _DECLARE: "declare", _TYPE: "%", _GLOBAL: "@"}
_ATTRIBUTE_REFERENCE = re.compile(r"#(\d+)")
_DECLARATION_ATTRIBUTE = re.compile(r"\s+#(\d+)\s*$")
_METADATA_REFERENCE = re.compile(r"!(\d+)")
_ATTRIBUTE_DEFINITION = re.compile(r"^attributes #(\d+) = (.+)$")
_INSTRUCTION_METADATA_REFERENCE = re.compile(r"![A-Za-z0-9_.-]+\s+!(\d+)")

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
            validate_shader_contract, _merge_entry, _attach_reflection, _reflection_cbuffer_usage,
            _constant_extracts, _function_spans, _execute_dxil_job,
            _rewrite_legacy_handles, _rewrite_modern_handles, _remapped_handle_index,
            _prepare_dxil_job, _entry_name, _dxil_job_key, stage_input_library,
            DxilEntryMetadata.parse.__func__, DxilEntryMetadata._properties,
            DxilEntryMetadata.input_interpolation, DxilEntryMetadata.output_interpolation,
            DxilEntryMetadata.patch_interpolation, DxilEntryMetadata.primitive_execution,
            DxilEntryMetadata.mesh_execution, DxilEntryMetadata.primitive_input_count,
            DxilEntryMetadata.thread_group_size, _dxil_signatures.metadata_definitions,
            _dxil_metadata.metadata_operands, _dxil_metadata._parse_metadata_operands,
            _dxil_metadata._METADATA_TUPLE, _dxil_metadata._NESTED_OPERAND,
            _module_tables, _ATTRIBUTE_DEFINITION, _INSTRUCTION_METADATA_REFERENCE,
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


def _compile_carrier(source: str, *, entry_point: str, target: str, arguments=None):
    arguments = _CARRIER_ARGUMENTS if arguments is None else tuple(arguments)

    def build():
        _NATIVE_ACTIVITY["compile"] += 1
        return compile_hlsl_dxc(source, entry_point=entry_point, target=target, extra_arguments=arguments)
    return _bounded_region_cache(
        "re_engine_dxil_carriers", (source, entry_point, target, arguments), _CARRIER_CACHE_BYTES, build,
        lambda result: len(source.encode("utf-8")) + len(result.bytecode) + len(result.diagnostics.encode("utf-8")))


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


def _symbol(value: str) -> str:
    return value[1:-1] if value.startswith('"') and value.endswith('"') else value


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


def _function_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    index = 0
    while index < len(lines):
        match = _DEFINE.match(lines[index]) if lines[index].startswith("define") else None
        if match is None:
            index += 1
            continue
        name = _symbol(match.group("name"))
        stop = index + 1
        while stop < len(lines) and lines[stop].strip() != "}":
            stop += 1
        if stop >= len(lines) or name in result:
            raise ShaderGraphError("DXIL carrier contains an invalid function table.")
        result[name] = (index, stop + 1)
        index = stop + 1
    return result


def _module_symbols(lines: list[str], pattern: re.Pattern[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    prefix = _SYMBOL_PREFIXES.get(pattern)
    for line in lines:
        if prefix is not None and not line.startswith(prefix):
            continue
        match = pattern.match(line)
        if match is not None:
            result[match.group("name")] = line
    return result


def _module_tables(lines):
    symbols = {pattern: {} for pattern in (_DEFINE, _DECLARE, _TYPE, _GLOBAL)}
    metadata, attributes = {}, {}
    for line in lines:
        first = line[:1]
        if first == "d":
            pattern = _DEFINE if line.startswith("define") else _DECLARE
        elif first == "%":
            pattern = _TYPE
        elif first == "@":
            pattern = _GLOBAL
        elif first == "!":
            if match := _METADATA_DEFINITION.match(line):
                metadata[int(match["id"])] = line
            continue
        elif first == "a":
            if match := _ATTRIBUTE_DEFINITION.match(line):
                attributes[int(match[1])] = match[2]
            continue
        else:
            continue
        if match := pattern.match(line):
            symbols[pattern][match["name"]] = line
    return symbols, metadata, attributes


def _override_key(overrides, resource, key):
    if resource.sampleable:
        return overrides.get(key, key)
    if resource.input_type == 'constant-buffer':
        return overrides.get(('b', *key), key)
    return key


def _remapped_handle_index(source, target, index, reserved, instructions):
    source_count = 0 if source.bind_count == 0xFFFFFFFF else source.bind_count
    target_count = 0 if target.bind_count == 0xFFFFFFFF else target.bind_count
    moved = (source.register_space, source.bind_point) != (target.register_space, target.bind_point)
    if moved and (
            source.input_type, source.return_type, source.dimension, source.sample_count, source_count
    ) != (target.input_type, target.return_type, target.dimension, target.sample_count, target_count):
        raise ShaderGraphError(
            f"Resource remap {source.name} -> {target.name} changes its native type or array extent."
        )
    if source_count == 1:
        return str(target.bind_point)
    offset = target.bind_point - source.bind_point
    if not offset:
        return index
    index = index.strip()
    if re.fullmatch(r'-?\d+', index):
        return str((int(index) + offset) & 0xFFFFFFFF)
    # DXIL handle indices are absolute registers.
    name = '%reasy.resource_index'
    serial = 0
    while name in reserved:
        serial += 1
        name = f'%reasy.resource_index_{serial}'
    reserved.add(name)
    instructions.append(f'  {name} = add i32 {index}, {offset}')
    return name


def _rewrite_legacy_handles(
    entry: list[str],
    source: ShaderContract,
    candidate: ShaderContract,
    texture_overrides: Mapping[tuple[int, int], tuple[int, int]],
) -> list[str]:
    source_by_id = {
        (item.register_class, int(item.resource_id)): item
        for item in source.resources
    }
    candidate_by_binding = {
        (item.register_class, int(item.register_space), int(item.bind_point)): item
        for item in candidate.resources
    }
    pattern = re.compile(
        r'(?P<prefix>@dx\.op\.createHandle\(i32\s+57,\s+i8\s+)(?P<class>\d+),\s+i32\s+(?P<id>\d+),\s+i32\s+(?P<index>[^,]+)(?P<suffix>,\s+i1\s+(?:true|false)\))'
    )
    result = []
    reserved = {*_SSA.findall(line) for line in entry}
    for line in entry:
        def replace_handle(match: re.Match[str]) -> str:
            register_class = DXIL_RESOURCE_CLASSES.get(int(match.group("class")))
            source_resource = source_by_id.get(
                (register_class or "", int(match.group("id")))
            )
            if source_resource is None:
                raise ShaderGraphError("Recovered DXIL references an unknown resource range.")
            source_key = (
                int(source_resource.register_space),
                int(source_resource.bind_point),
            )
            target_key = _override_key(texture_overrides, source_resource, source_key)
            target = candidate_by_binding.get(
                (source_resource.register_class, int(target_key[0]), int(target_key[1]))
            )
            if target is None:
                raise ShaderGraphError(
                    f"Rebuilt DXIL is missing {source_resource.register_class}{target_key[1]} space {target_key[0]}."
                )
            index = _remapped_handle_index(
                source_resource, target, match.group("index"), reserved, result,
            )
            return (
                match.group("prefix")
                + str(DXIL_RESOURCE_CLASS_NUMBERS[target.register_class])
                + ", i32 "
                + str(int(target.resource_id))
                + ", i32 "
                + index
                + match.group("suffix")
            )
        result.append(pattern.sub(replace_handle, line) if "@dx.op.createHandle(" in line else line)
    return result


def _rewrite_modern_handles(
    entry: list[str],
    source: ShaderContract,
    candidate: ShaderContract,
    texture_overrides: Mapping[tuple[int, int], tuple[int, int]],
) -> list[str]:
    source_by_binding = {
        (item.register_class, int(item.register_space), int(item.bind_point)): item
        for item in source.resources
    }
    candidate_by_binding = {
        (item.register_class, int(item.register_space), int(item.bind_point)): item
        for item in candidate.resources
    }
    creation = re.compile(
        r'(?P<prefix>^(?P<result>%[^\s=]+)\s*=\s*call\s+%dx\.types\.Handle\s+@dx\.op\.createHandleFromBinding\(i32\s+217,\s+%dx\.types\.ResBind\s+)'
        r'(?P<binding>zeroinitializer|\{\s*i32\s+(?P<lower>\d+),\s*i32\s+(?P<upper>\d+|-1),\s*i32\s+(?P<space>\d+),\s*i8\s+(?P<class>\d+)\s*\})'
        r'(?P<middle>,\s+i32\s+)(?P<index>[^,]+)(?P<suffix>,\s+i1\s+(?:true|false)\).*)'
    )
    annotation = re.compile(
        r'^(?P<result>%[^\s=]+)\s*=\s*call\s+%dx\.types\.Handle\s+@dx\.op\.annotateHandle\('
        r'i32\s+216,\s+%dx\.types\.Handle\s+(?P<base>%[^,]+),\s+'
        r'%dx\.types\.ResourceProperties\s+\{\s*i32\s+(?P<kind>\d+),\s+i32\s+(?P<size>\d+)\s*\}(?P<tail>.*)$'
    )
    handle_resources: dict[str, ShaderContractResource] = {}
    result: list[str] = []
    reserved = {*_SSA.findall(line) for line in entry}
    for line in entry:
        match = creation.match(line.strip()) if "@dx.op.createHandleFromBinding(" in line else None
        if match is not None:
            if match.group("binding") == "zeroinitializer":
                lower = space = resource_class = 0
            else:
                lower = int(match.group("lower"))
                space = int(match.group("space"))
                resource_class = int(match.group("class"))
            register_class = DXIL_RESOURCE_CLASSES.get(resource_class, "")
            source_resource = source_by_binding.get((register_class, space, lower))
            if source_resource is None:
                raise ShaderGraphError("Recovered DXIL uses an unknown resource binding.")
            source_key = (space, lower)
            target_key = _override_key(texture_overrides, source_resource, source_key)
            target = candidate_by_binding.get(
                (register_class, int(target_key[0]), int(target_key[1]))
            )
            if target is None:
                raise ShaderGraphError(
                    f"Rebuilt DXIL is missing {register_class}{target_key[1]} space {target_key[0]}."
                )
            target_upper = (
                -1
                if int(target.bind_count) in {0, 0xFFFFFFFF}
                else int(target.bind_point) + int(target.bind_count) - 1
            )
            binding = (
                "zeroinitializer"
                if (
                    int(target.bind_point) == 0
                    and target_upper == 0
                    and int(target.register_space) == 0
                    and DXIL_RESOURCE_CLASS_NUMBERS[target.register_class] == 0
                )
                else (
                    f"{{ i32 {int(target.bind_point)}, i32 {target_upper}, "
                    f"i32 {int(target.register_space)}, i8 {DXIL_RESOURCE_CLASS_NUMBERS[target.register_class]} }}"
                )
            )
            index = _remapped_handle_index(
                source_resource, target, match.group("index"), reserved, result,
            )
            line = (
                match.group("prefix")
                + binding
                + match.group("middle")
                + index
                + match.group("suffix")
            )
            handle_resources[match.group("result")] = target
        annotation_match = annotation.match(line.strip()) if "@dx.op.annotateHandle(" in line else None
        if annotation_match is not None:
            target = handle_resources.get(annotation_match.group("base"))
            if target is not None:
                handle_resources[annotation_match.group("result")] = target
                if target.input_type == "constant-buffer":
                    buffer = next(
                        (
                            item
                            for item in candidate.constant_buffers
                            if int(item.bind_point) == int(target.bind_point)
                            and int(item.register_space) == int(target.register_space)
                        ),
                        None,
                    )
                    if buffer is None:
                        raise ShaderGraphError(
                            f"Rebuilt DXIL has no layout for constant buffer {target.name}."
                        )
                    line = re.sub(
                        r'(%dx\.types\.ResourceProperties\s+\{\s*i32\s+\d+,\s+i32\s+)\d+(\s*\})',
                        rf"\g<1>{int(buffer.byte_size)}\g<2>",
                        line,
                        count=1,
                    )
        result.append(line)
    return result


def _constant_extracts(
    entry: list[str],
    contract: ShaderContract,
    *,
    dynamic_bindings: set[tuple[int, int]] | None = None,
    dynamic_extracts: dict | None = None,
    read_ranges: dict | None = None,
) -> dict[ConstantWord, list[str]]:
    resources_by_id = {
        (item.register_class, int(item.resource_id)): item
        for item in contract.resources
    }
    resources_by_binding = {
        (item.register_class, int(item.register_space), int(item.bind_point)): item
        for item in contract.resources
    }
    handles: dict[str, ShaderContractResource] = {}
    loads: dict[str, tuple[ShaderContractResource, int | str]] = {}
    result: dict[ConstantWord, list[str]] = {}
    legacy = re.compile(
        r'^(?P<result>%[^\s=]+)\s*=.*@dx\.op\.createHandle\(i32\s+57,\s+i8\s+(?P<class>\d+),\s+i32\s+(?P<id>\d+),'
    )
    modern = re.compile(
        r'^(?P<result>%[^\s=]+)\s*=.*@dx\.op\.createHandleFromBinding\(i32\s+217,\s+%dx\.types\.ResBind\s+'
        r'(?P<binding>zeroinitializer|\{\s*i32\s+(?P<lower>\d+),\s*i32\s+\d+,\s*i32\s+(?P<space>\d+),\s*i8\s+(?P<class>\d+)\s*\})'
    )
    annotate = re.compile(
        r'^(?P<result>%[^\s=]+)\s*=.*@dx\.op\.annotateHandle\([^,]+,\s+%dx\.types\.Handle\s+(?P<base>%[^,]+),'
    )
    load = re.compile(
        r'^(?P<result>%[^\s=]+)\s*=\s*call\s+%dx\.types\.CBufRet\.[fi](?:16|32|64)\s+'
        r'@dx\.op\.cbufferLoadLegacy\.[fi](?:16|32|64)\(i32\s+59,\s+%dx\.types\.Handle\s+(?P<handle>%[^,]+),\s+i32\s+(?P<register>[^)]+)\)'
    )
    extract = re.compile(
        r'^(?P<result>%[^\s=]+)\s*=\s*extractvalue\s+%dx\.types\.CBufRet\.(?P<scalar>[fi](?:16|32|64))\s+(?P<load>%[^,]+),\s+(?P<component>\d+)'
    )
    for source_line in entry:
        line = source_line.strip()
        if "@dx.op.createHandle(" in line and (match := legacy.match(line)):
            register_class = DXIL_RESOURCE_CLASSES.get(int(match.group("class")), "")
            resource = resources_by_id.get((register_class, int(match.group("id"))))
            if resource is not None:
                handles[match.group("result")] = resource
            continue
        if "@dx.op.createHandleFromBinding(" in line and (match := modern.match(line)):
            if match.group("binding") == "zeroinitializer":
                lower = space = resource_class = 0
            else:
                lower = int(match.group("lower"))
                space = int(match.group("space"))
                resource_class = int(match.group("class"))
            resource = resources_by_binding.get(
                (DXIL_RESOURCE_CLASSES.get(resource_class, ""), space, lower)
            )
            if resource is not None:
                handles[match.group("result")] = resource
            continue
        if "@dx.op.annotateHandle(" in line and (match := annotate.match(line)):
            resource = handles.get(match.group("base"))
            if resource is not None:
                handles[match.group("result")] = resource
            continue
        if "@dx.op.cbufferLoadLegacy." in line and (match := load.match(line)):
            resource = handles.get(match.group("handle"))
            if resource is not None and resource.input_type == "constant-buffer":
                register = match.group("register").strip()
                if register.isdecimal():
                    loads[match.group("result")] = (resource, int(register))
                else:
                    loads[match.group("result")] = (resource, register)
                    if dynamic_bindings is not None:
                        dynamic_bindings.add((int(resource.register_space), int(resource.bind_point)))
            continue
        if "extractvalue" in line and (match := extract.match(line)):
            loaded = loads.get(match.group("load"))
            if loaded is None:
                continue
            resource, register = loaded
            size = int(match.group('scalar')[1:]) // 8
            offset = register * 16 + int(match.group('component')) * size if isinstance(register, int) else None
            if read_ranges is not None:
                read_ranges.setdefault((int(resource.register_space), int(resource.bind_point)), []).append(
                    (offset, size, match.group('result')))
            if size != 4:
                continue  # Scalar material overrides address 32-bit words.
            if isinstance(register, str):
                if dynamic_extracts is not None:
                    key = constant_word(int(resource.bind_point), int(match.group("component")), int(resource.register_space))
                    dynamic_extracts.setdefault(key, []).append((match.group("result"), register, match.group("scalar")))
                continue
            byte_offset = register * 16 + int(match.group("component")) * 4
            result.setdefault(constant_word(int(resource.bind_point), byte_offset, int(resource.register_space)), []).append(
                match.group("result")
            )
    return result


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
    original_lines = original.splitlines()
    carrier_lines = carrier.splitlines()
    original_spans = _function_spans(original_lines)
    carrier_spans = _function_spans(carrier_lines)
    if entry_point not in original_spans or entry_point not in carrier_spans:
        raise ShaderGraphError("DXIL entry-function routing is inconsistent.")
    carrier_start, carrier_stop = carrier_spans[entry_point]
    native_metadata = native_metadata or DxilEntryMetadata.parse(original_lines)
    carrier_metadata = carrier_metadata or DxilEntryMetadata.parse(carrier_lines)
    carrier_entry = map_carrier_inputs(
        carrier_lines[carrier_start:carrier_stop], native_metadata, carrier_metadata, indexed_reads,
    )
    original_symbols, original_metadata, native_attributes = _module_tables(original_lines)
    carrier_symbols, carrier_metadata_lines, carrier_attributes = _module_tables(carrier_lines)
    referenced_attributes = {int(n) for start, stop in original_spans.values()
                             for line in original_lines[start:stop] if "#" in line
                             for n in _ATTRIBUTE_REFERENCE.findall(line)}
    referenced_attributes |= {int(match[1]) for declaration in original_symbols[_DECLARE].values()
                              if (match := _DECLARATION_ATTRIBUTE.search(declaration))}
    if not referenced_attributes.issubset(native_attributes):
        raise ShaderGraphError("Native DXIL function references an absent attribute group.")
    attribute_start = max(carrier_attributes, default=-1) + 1
    attribute_map = {old: attribute_start + i for i, old in enumerate(sorted(referenced_attributes))}
    carrier_lines.extend(f"attributes #{new} = {native_attributes[old]}" for old, new in attribute_map.items())

    entries = []
    for name, (start, stop) in original_spans.items():
        if name != entry_point and name in carrier_spans:
            raise ShaderGraphError(f"Native DXIL function {name} collides with an expression helper.")
        body = [_ATTRIBUTE_REFERENCE.sub(lambda m: f"#{attribute_map[int(m[1])]}", line) if "#" in line else line
                for line in original_lines[start:stop]]
        surface_code = {}
        if surface_plan and name == entry_point:
            body, surface_code = apply_surface_plan(body, start, carrier_lines, carrier_entry, surface_plan,
                                                    native_metadata.output_rows())
        body = _rewrite_legacy_handles(body, source_contract, candidate_contract, texture_overrides)
        body = _rewrite_modern_handles(body, source_contract, candidate_contract, texture_overrides)
        body = _apply_constant_overrides(body, carrier_entry, candidate_contract, scalar_expressions)
        body = _apply_material_overrides(body, carrier_entry, candidate_contract, material_expressions, native_metadata)
        body = relocate_material_record_words(body, candidate_contract, dict(material_word_offsets), entry_metadata=native_metadata)
        if surface_code:
            body = place_surface_code(body, surface_code)
        entries.extend(body)
    entry = entries


    carrier_functions = carrier_symbols[_DEFINE]
    carrier_declarations = carrier_symbols[_DECLARE]
    original_declarations = original_symbols[_DECLARE]
    carrier_globals = carrier_symbols[_GLOBAL]
    original_globals = original_symbols[_GLOBAL]
    calls = {
        match.group("name")
        for line in entry
        for match in _CALL.finditer(line)
    }
    additions: list[str] = []
    for name in sorted(calls):
        if _symbol(name) in original_spans or name in carrier_functions or name in carrier_declarations:
            continue
        declaration = original_declarations.get(name)
        if declaration is None:
            raise ShaderGraphError(f"Recovered DXIL calls undeclared function {_symbol(name)}.")
        additions.append(_DECLARATION_ATTRIBUTE.sub(lambda m: f" #{attribute_map[int(m[1])]}", declaration))

    missing_globals = original_globals.keys() - carrier_globals.keys()
    required_globals = {
        token
        for line in entry
        for token in _MODULE_REFERENCE.findall(line)
        if token in missing_globals
    } if missing_globals else set()
    pending_globals = list(required_globals)
    while pending_globals:
        name = pending_globals.pop()
        definition = original_globals[name]
        for token in _MODULE_REFERENCE.findall(definition):
            dependency = token
            if (
                dependency in original_globals
                and dependency not in carrier_globals
                and dependency not in required_globals
            ):
                required_globals.add(dependency)
                pending_globals.append(dependency)
    imported_globals = [original_globals[name] for name in sorted(required_globals)]

    carrier_types = carrier_symbols[_TYPE]
    original_types = original_symbols[_TYPE]
    missing_types = original_types.keys() - carrier_types.keys()
    required_types = {
        token
        for line in (*entry, *additions, *imported_globals)
        for token in _SSA.findall(line)
        if token in missing_types
    } if missing_types else set()
    pending = list(required_types)
    imported_types: list[str] = []
    imported_type_names = set()
    while pending:
        name = pending.pop()
        if name in carrier_types or name in imported_type_names:
            continue
        definition = original_types.get(name)
        if definition is None:
            continue
        imported_types.append(definition)
        imported_type_names.add(name)
        pending.extend(
            token
            for token in _SSA.findall(definition)
            if token != name and token in original_types
        )

    referenced_metadata = {
        int(value)
        for line in (*entry, *imported_globals)
        if "!" in line
        for value in _INSTRUCTION_METADATA_REFERENCE.findall(line)
    }
    referenced_metadata.update(
        int(reference[1:]) for reference in (
            native_metadata.operands[2], native_metadata.operands[4],
        ) if re.fullmatch(r"!\d+", reference)
    )
    native_view_state = next((line for line in original_lines if line.startswith("!dx.viewIdState = ")), None)
    if native_view_state is not None:
        referenced_metadata.update(int(n) for n in re.findall(r"!(\d+)", native_view_state))
    closure = set(referenced_metadata)
    pending_ids = list(referenced_metadata)
    while pending_ids:
        current = pending_ids.pop()
        definition = original_metadata.get(current)
        if definition is None:
            raise ShaderGraphError("Recovered DXIL references missing instruction metadata.")
        for value in _METADATA_REFERENCE.findall(definition.split("=", 1)[1]):
            number = int(value)
            if number not in closure:
                closure.add(number)
                pending_ids.append(number)
    next_id = max(carrier_metadata_lines, default=-1) + 1
    metadata_map = {old: next_id + index for index, old in enumerate(sorted(closure))}

    def remap_metadata(line: str) -> str:
        if "!" not in line:
            return line
        return _METADATA_REFERENCE.sub(
            lambda match: f"!{metadata_map.get(int(match.group(1)), int(match.group(1)))}",
            line,
        )

    entry = [remap_metadata(line) for line in entry]
    metadata_additions = [
        remap_metadata(original_metadata[old]) for old in sorted(closure)
    ]
    # LLVM named types must be defined before use; import them with the carrier's other types.
    if imported_types:
        type_insertion = next(
            (
                index
                for index, line in enumerate(carrier_lines)
                if _GLOBAL.match(line) or _DEFINE.match(line) or _DECLARE.match(line)
            ),
            len(carrier_lines),
        )
        type_rows = [*sorted(imported_types), ""]
        carrier_lines[type_insertion:type_insertion] = type_rows
        if type_insertion <= carrier_start:
            carrier_start += len(type_rows)
            carrier_stop += len(type_rows)

    if imported_globals:
        global_insertion = next(
            (
                index
                for index, line in enumerate(carrier_lines)
                if _DEFINE.match(line) or _DECLARE.match(line)
            ),
            len(carrier_lines),
        )
        global_rows = [*imported_globals, ""]
        carrier_lines[global_insertion:global_insertion] = global_rows
        if global_insertion <= carrier_start:
            carrier_start += len(global_rows)
            carrier_stop += len(global_rows)

    insertion = next(
        (
            index
            for index, line in enumerate(carrier_lines)
            if line.startswith(("attributes ", "!llvm.", "!dx."))
            or _METADATA_DEFINITION.match(line)
        ),
        len(carrier_lines),
    )
    if additions:
        declaration_rows = [*additions, ""]
        carrier_lines[insertion:insertion] = declaration_rows
        if insertion <= carrier_start:
            carrier_start += len(declaration_rows)
            carrier_stop += len(declaration_rows)
    carrier_lines[carrier_start:carrier_stop] = entry
    if metadata_additions:
        carrier_lines.extend(metadata_additions)
    carrier_lines = [line for line in carrier_lines if not line.startswith("!dx.viewIdState = ")]
    if native_view_state is not None:
        carrier_lines.append(remap_metadata(native_view_state))
    entry_operands = list(carrier_metadata.operands)
    entry_operands[2] = remap_metadata(native_metadata.operands[2])
    entry_operands[4] = remap_metadata(native_metadata.operands[4])
    carrier_lines = [
        f"{carrier_metadata.reference} = !{{{', '.join(entry_operands)}}}"
        if line.startswith(carrier_metadata.reference + " = ") else line
        for line in carrier_lines
    ]
    model_reference = carrier_metadata.definitions.get("!dx.shaderModel", ())
    if len(model_reference) != 1:
        raise ShaderGraphError("DXIL carrier has no shader-model declaration.")
    model = (f'!"{source_contract.target.split("_")[0]}"',
             f"i32 {source_contract.model_major}", f"i32 {source_contract.model_minor}")
    carrier_lines = [f"{model_reference[0]} = !{{{', '.join(model)}}}"
                     if line.startswith(model_reference[0] + " = ") else line for line in carrier_lines]
    used_functions = {
        match["name"]
        for line in carrier_lines
        if not (line.startswith("define") and _DEFINE.match(line))
        and not (line.startswith("declare") and _DECLARE.match(line))
        for match in _CALL.finditer(line)
    }
    carrier_lines = [
        line for line in carrier_lines
        if not line.startswith("declare") or (declaration := _DECLARE.match(line)) is None
        or declaration["name"] in used_functions
    ]
    return "\n".join(carrier_lines) + "\n"


def _reflection_cbuffer_usage(lines, prepared, definitions):
    metadata = prepared.reflection_metadata.definitions
    resources = metadata.get(prepared.reflection_metadata.operands[3], ())
    if len(resources) != 4:
        return
    buffers = {(b.register_space, b.bind_point): b for b in prepared.contract.constant_buffers}
    types = {}
    for reference in metadata.get(resources[2], ()):
        row = metadata[reference]
        buffer = buffers[int(row[3].split()[1]), int(row[4].split()[1])]
        types[row[1].removesuffix('* undef') + ' undef'] = buffer

    if not types:
        return
    reads = {}
    for start, stop in _function_spans(lines).values():
        body = lines[start:stop]
        referenced = {name for line in body for name in _SSA.findall(line.split('=', 1)[-1].partition(';')[0])}
        ranges = {}
        _constant_extracts(body, prepared.contract, read_ranges=ranges)
        for binding, values in ranges.items():
            reads.setdefault(binding, set()).update((offset, size) for offset, size, value in values if value in referenced)

    next_id = max(definitions, default=-1) + 1
    def add(operands):
        nonlocal next_id
        reference = f'!{next_id}'
        definitions[next_id] = f"{reference} = !{{{', '.join(operands)}}}"
        next_id += 1
        return reference

    for reference in metadata.get('!dx.typeAnnotations', ()):
        row = list(metadata[reference])
        if row[0] != 'i32 0':
            continue  # Function annotations do not describe buffer fields.
        for index in range(1, len(row), 2):
            buffer = types.get(row[index])
            if buffer is None:
                continue
            aggregate = list(metadata[row[index + 1]])
            if len(aggregate) != len(buffer.variables) + 1:
                raise ShaderGraphError(f'DXIL reflection fields disagree with {buffer.name}.')
            binding = buffer.register_space, buffer.bind_point
            for field_index, variable in enumerate(buffer.variables, 1):
                annotation = metadata[aggregate[field_index]]
                # DXIL field-annotation tag 9 is CBVarUsed.
                fields = [token for tag, value in zip(annotation[::2], annotation[1::2])
                          if tag != 'i32 9' for token in (tag, value)]
                used = any(offset is None or (offset < variable.byte_offset + variable.byte_size
                                               and variable.byte_offset < offset + size)
                           for offset, size in reads.get(binding, ()))
                if used:
                    fields.extend(('i32 9', 'i1 true'))
                aggregate[field_index] = add(fields)
            row[index + 1] = add(aggregate)
        definitions[int(reference[1:])] = f"{reference} = !{{{', '.join(row)}}}"


def _attach_reflection(assembly: str, carrier: bytes, *, prepared: _PreparedCarrier | None = None) -> str:
    prepared = prepared or _prepare_carrier(carrier)
    reflected = prepared.reflection.splitlines()
    lines = assembly.splitlines()
    definitions = {int(m["id"]): line for line in reflected
                   if line.startswith("!") and (m := _METADATA_DEFINITION.match(line))}
    _reflection_cbuffer_usage(lines, prepared, definitions)
    reflected_entry = prepared.reflection_metadata
    finalized_entry = DxilEntryMetadata.parse(lines)
    reflected_resources = reflected_entry.definitions.get(reflected_entry.operands[3], ())
    finalized_resources = finalized_entry.definitions.get(finalized_entry.operands[3], ())
    if len(reflected_resources) == 4 and len(finalized_resources) == 4:
        finalized_uavs = {}
        for ref in finalized_entry.definitions.get(finalized_resources[1], ()):
            row = finalized_entry.definitions[ref]
            if len(row) != 11:
                raise ShaderGraphError("Finalized DXIL UAV has an invalid resource schema.")
            finalized_uavs[row[3:6]] = row
        for ref in reflected_entry.definitions.get(reflected_resources[1], ()):
            row = list(reflected_entry.definitions[ref])
            native = finalized_uavs.get(tuple(row[3:6]))
            if native is not None:
                row[6:] = [re.sub(r"!(\d+)", r"!native.\1", token) for token in native[6:]]
                definitions[int(ref[1:])] = f"{ref} = !{{{', '.join(row)}}}"
    named = {line.split(" = ", 1)[0]: line for line in reflected if line.startswith(("!dx.resources =", "!dx.typeAnnotations ="))}
    if "!dx.typeAnnotations" in named:
        refs = [f"!{n}" for n in re.findall(r"!(\d+)", named["!dx.typeAnnotations"])
                if re.match(r"!\d+ = !\{i32 0,", definitions[int(n)])]
        named["!dx.typeAnnotations"] = "!dx.typeAnnotations = !{" + ", ".join(refs) + "}"
    pending = {int(n) for line in named.values() for n in re.findall(r"!(\d+)", line)}
    closure = set()
    while pending:
        number = pending.pop()
        if number in closure:
            continue
        definition = definitions.get(number)
        if definition is None:
            raise ShaderGraphError("DXC reflection references missing type metadata.")
        closure.add(number)
        pending.update(int(n) for n in re.findall(r"!(\d+)", definition.split("=", 1)[1]))
    offset = max((int(m["id"]) for line in lines
                  if line.startswith("!") and (m := _METADATA_DEFINITION.match(line))), default=-1) + 1
    ids = {number: offset + index for index, number in enumerate(sorted(closure))}
    def remap(line):
        result = re.sub(r"!(\d+)", lambda m: f"!{ids[int(m[1])]}", line)
        return re.sub(r"!native\.(\d+)", r"!\1", result)
    entry = finalized_entry
    operands = list(entry.operands)
    root = re.search(r"!(\d+)", named.get("!dx.resources", ""))
    if root is not None:
        operands[3] = f"!{ids[int(root[1])]}"
        lines = [f"{entry.reference} = !{{{', '.join(operands)}}}"
                 if line.startswith(entry.reference + " = ") else line for line in lines]
    types = _module_symbols(lines, _TYPE)
    imported = []
    for name, definition in _module_symbols(reflected, _TYPE).items():
        if name in types and types[name] != definition:
            raise ShaderGraphError(f"DXC reflection has a conflicting type {name}.")
        if name not in types:
            imported.append(definition)
    insertion = next((i for i, line in enumerate(lines) if _GLOBAL.match(line) or _DEFINE.match(line) or _DECLARE.match(line)), len(lines))
    lines[insertion:insertion] = imported
    lines = [line for line in lines if not line.startswith("!") or line.split(" = ", 1)[0] not in named]
    lines.extend(remap(line) for line in named.values())
    lines.extend(remap(definitions[number]) for number in sorted(closure))
    return "\n".join(lines) + "\n"


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
    source = _carrier_source(
        carrier_contract,
        entry_point=entry_point,
        scalar_expressions=expressions,
        helpers=helpers,
        material_expressions=material_expressions,
        material_buffer_size=material_buffer_size,
        thread_group_size=native_metadata.thread_group_size(),
        surface_plan=tuple(surface_plan),
    )
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
        merged = re.sub(r"\bnoinline\b", "alwaysinline", merged)
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


def rebuild_re_engine_dxil_batch(requests):
    requests = tuple(requests)
    results = [None] * len(requests)
    prepared, owners, aliases = {}, {}, {}
    info = {"requests": len(requests), "hits": 0, "misses": 0, "coalesced": 0,
            "workers": 0, "submitted": 0, "worker_compiles": 0, "worker_validations": 0,
            "sequential_jobs": 0, "peak_pending_input_bytes": 0,
            "oversized_or_mutable": 0}
    for index, (ir, source_contract, candidate_contract, options) in enumerate(requests):
        try:
            job, key, runtime = _prepare_dxil_job(ir, source_contract, candidate_contract, **options)
            cached = _NATIVE_BUILD_CACHE.get(key)
            if cached is not None:
                results[index] = cached
                info["hits"] += 1
            elif key is not None and key in owners:
                aliases[index] = owners[key]
                info["coalesced"] += 1
            else:
                prepared[index] = job, key, runtime
                if key is not None:
                    owners[key] = index
        except Exception as exc:
            results[index] = exc
    info["misses"] = len(prepared)

    def publish(outcome):
        if outcome.index not in prepared or results[outcome.index] is not None:
            raise RuntimeError("DXIL worker returned an unexpected or repeated job")
        if outcome.error_type:
            message = outcome.error_message
            if outcome.error_type != "ShaderGraphError":
                message = f"{outcome.error_type}: {message}"
            results[outcome.index] = ShaderGraphError(message)
        else:
            if not isinstance(outcome.result, ReEngineDxilBuild):
                raise RuntimeError("DXIL worker returned an invalid native result")
            results[outcome.index] = outcome.result
            _NATIVE_BUILD_CACHE.put(prepared[outcome.index][1], outcome.result)
        _NATIVE_ACTIVITY["compile"] += outcome.compiles
        _NATIVE_ACTIVITY["validate"] += outcome.validations

    if len(prepared) >= worker.MIN_PARALLEL_JOBS and worker.native_worker_count() > 1:
        first_job, _first_key, first_runtime = next(iter(prepared.values()))
        same_runtime = all(runtime == first_runtime
                           and job.configuration == tuple(_DXIL_BUILD_CONFIGURATION)
                           for job, _key, runtime in prepared.values())
        identity = _native_worker_runtime(first_runtime) if same_runtime else None
        if identity is not None:
            info.update(worker.run_native_workers(
                tuple((index, item[0]) for index, item in prepared.items()),
                identity, first_job.configuration, publish,
            ))
    for index, (job, key, runtime) in prepared.items():
        if results[index] is not None:
            continue
        info["sequential_jobs"] += 1
        try:
            result = _execute_dxil_job(job)
            if _native_build_runtime() == runtime:
                _NATIVE_BUILD_CACHE.put(key, result)
            results[index] = result
        except Exception as exc:
            results[index] = exc
    for index, owner in aliases.items():
        results[index] = results[owner]
    _LAST_NATIVE_BATCH.clear()
    _LAST_NATIVE_BATCH.update(info)
    return tuple(results)


_DEFAULT_NATIVE_PIPELINE = _native_pipeline_functions()


__all__ = [
    "ReEngineDxilBuild",
    "rebuild_re_engine_dxil",
    "rebuild_re_engine_dxil_batch",
]
