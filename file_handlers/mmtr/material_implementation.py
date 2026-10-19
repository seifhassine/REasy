from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
import hashlib
import json
import re

from file_handlers.sdf.dxbc import parse_dxbc
from file_handlers.sdf.dxc import DxcError, compile_hlsl_dxc, disassemble_dxil, enrich_dxil_shader, validate_dxil
from file_handlers.sdf.graph_snapshot import compiler_graph_copy
from file_handlers.sdf.hlsl import compile_hlsl_source_detailed
from file_handlers.sdf.models import STAGE_KEYS
from file_handlers.sdf.shader_contract_storage import contract_from_storage, contract_to_storage
from file_handlers.sdf.shader_stage_execution import ShaderPrimitiveExecution, ShaderMeshExecution
from file_handlers.sdf.shader_graph import (
    ShaderContract, ShaderGraph, ShaderGraphError, generate_graph_hlsl, validate_graph, validate_shader_contract,
)
from .material_function import compilation_graph
from .native_build_cache import NativeBuildCache, native_build_key


_IMPLEMENTATION_DXIL_CONFIGURATION = (
    'authored-implementation-v1', 'REasyShaderGraph.hlsl',
    '-Ges', '-O3', '-WX', '-Qstrip_debug',
)
_IMPLEMENTATION_DXIL_ARGUMENTS = ('-Wno-error=inline-asm',)
_IMPLEMENTATION_BUILD_CACHE = NativeBuildCache()


def _implementation_build_runtime():
    from file_handlers.sdf import dxc
    from file_handlers.sdf.dxil_structured_views import coalesce_structured_views
    from .profiles.re_engine_dxil_signatures import DxilEntryMetadata
    from .native_resource import complete_typed_resources

    try:
        library = dxc._library()
    except DxcError:
        return None
    return (library, compile_hlsl_dxc, dxc.assemble_dxil, dxc._operation_result, disassemble_dxil,
            validate_dxil, enrich_dxil_shader, parse_dxbc,
            getattr(ShaderContract.from_shader, '__func__', ShaderContract.from_shader),
            coalesce_structured_views, complete_typed_resources,
            getattr(DxilEntryMetadata.parse, '__func__', DxilEntryMetadata.parse),
            DxilEntryMetadata.geometry_stream_declaration,
            implementation_contract, _check_interpolation, validate_shader_contract)


@dataclass(slots=True)
class MaterialProgramImplementation:
    name: str
    routes: tuple[tuple[int, int, str], ...]
    contract: ShaderContract
    graph: ShaderGraph
    source: str = ""

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 256:
            raise ShaderGraphError("An implementation needs a name of at most 256 characters.")
        if not isinstance(self.routes, tuple) or not self.routes or len(self.routes) > 65536:
            raise ShaderGraphError("Implementation routes are empty, invalid, or too large.")
        role = {'mesh': 'hull', 'amplification': 'domain'}.get(self.contract.stage, self.contract.stage)
        for row in self.routes:
            if (not isinstance(row, tuple) or len(row) != 3 or any(type(i) is not int or i < 0 for i in row[:2])
                    or row[2] not in STAGE_KEYS or row[2] != role):
                raise ShaderGraphError("An implementation has an invalid renderer route.")
        if len(set(self.routes)) != len(self.routes):
            raise ShaderGraphError("Implementation routes are duplicated.")
        if self.graph.stage != self.contract.stage:
            raise ShaderGraphError("The implementation graph has a different stage from its renderer ABI.")
        if not isinstance(self.source, str) or len(self.source.encode("utf-8")) > 4 * 1024 * 1024:
            raise ShaderGraphError("An implementation's HLSL is invalid or larger than 4 MiB.")

    @property
    def content_hash(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def to_dict(self):
        return {"name": self.name, "routes": [list(row) for row in self.routes],
            "contract": contract_to_storage(self.contract), "graph": self.graph.to_dict(),
            **({"source": self.source} if self.source else {})}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {"source"} != {"name", "routes", "contract", "graph"}:
            raise ShaderGraphError("Invalid material implementation.")
        routes = value["routes"]
        if not isinstance(routes, list) or len(routes) > 65536 or any(not isinstance(row, list) or len(row) != 3 for row in routes):
            raise ShaderGraphError("Invalid material implementation routes.")
        return cls(value["name"], tuple(tuple(row) for row in routes),
            contract_from_storage(value["contract"], source_sha256=""), ShaderGraph.from_dict(value["graph"]),
            value.get("source", ""))

    def diagnostics(self, *, source_profile=None):
        return validate_graph(compilation_graph(self.graph, source_profile=source_profile), self.contract)

    def renderer_contract(self, native):
        report = validate_shader_contract(self.contract, native)
        if not report.compatible:
            raise ShaderGraphError("The renderer template no longer matches this implementation:\n"
                + "\n".join(issue.message for issue in report.issues))
        _check_interpolation(self.contract, native)
        return self.contract

    def generate(self, contract, *, early_depth_stencil=False, source_profile=None):
        if contract.fingerprint != self.contract.fingerprint:
            raise ShaderGraphError("The renderer ABI changed. Rebind this implementation explicitly before building.")
        from .implementation_source_cache import generate_implementation_source
        generated = generate_implementation_source(self, contract, early_depth_stencil=early_depth_stencil,
            source_profile=source_profile, generator=generate_graph_hlsl, graph_builder=compilation_graph)
        return replace(generated, source=self.source) if self.source else generated

    def _bind_material_interface_for_compilation(self, source, target, project, profile):
        if source.fingerprint != self.contract.fingerprint:
            raise ShaderGraphError("The renderer ABI changed. Rebind this implementation explicitly before building.")
        structural = replace(target, constant_buffers=source.constant_buffers,
            structured_buffers=source.structured_buffers, resources=source.resources)
        if structural.fingerprint != source.fingerprint:
            raise ShaderGraphError("A material interface edit changed the renderer execution interface.")
        logical = profile.logical_parameter_name
        definitions = {str(item.source_name or item.name): item.name for item in project.parameters}
        graph = compiler_graph_copy(self.graph)
        for node in graph.nodes:
            if node.kind not in {"parameter", "structured_parameter"}:
                continue
            structured = node.kind == "structured_parameter"
            buffer_name = str(node.properties.get("resource" if structured else "buffer", ""))
            old = source.structured_buffer(buffer_name) if structured else source.buffer(buffer_name)
            new = target.structured_buffer(buffer_name) if structured else target.buffer(buffer_name)
            variable_name = str(node.properties.get("variable", ""))
            variable = next((v for v in old.variables if v.name == variable_name), None) if old else None
            if variable is None or new is None:
                raise ShaderGraphError("An implementation input no longer has its declared renderer buffer.")
            material = old.name == profile.user_buffer_name
            name = logical(variable.name) if material else variable.name
            wanted = definitions.get(name, name) if material else name
            matches = [v for v in new.variables if (logical(v.name) if material else v.name) == wanted]
            if len(matches) == 1 and _declared_types(matches[0]) == _declared_types(variable):
                node.properties["variable"] = matches[0].name
                continue
            extended = _retain_native_input(target, new, variable, structured) if material and not matches else None
            if extended is None:
                raise ShaderGraphError(f"The implementation input {old.name}.{variable.name} was removed or changed type. Reconnect it explicitly.")
            target = extended
            node.properties["variable"] = variable.name
        graph.contract_fingerprint = target.fingerprint
        return replace(self, contract=target, graph=graph)


def _declared_types(variable):
    return (variable.graph_type, variable.array_length, variable.matrix, variable.members)


def _retain_native_input(contract, buffer, variable, structured):
    start = variable.byte_offset
    end = start + max(variable.byte_size, 1)
    if start < 0 or end > buffer.byte_size:
        return None
    if any(item.name == variable.name for item in buffer.variables):
        return None
    if any(start < other.byte_offset + max(other.byte_size, 1) and other.byte_offset < end for other in buffer.variables):
        return None
    variables = tuple(sorted(buffer.variables + (variable,), key=lambda item: (item.byte_offset, item.name)))
    attribute = "structured_buffers" if structured else "constant_buffers"
    buffers = tuple(replace(item, variables=variables) if item is buffer else item for item in getattr(contract, attribute))
    return replace(contract, **{attribute: buffers})


def implementation_routes(project):
    result = {}
    for implementation in project.implementations:
        for route in implementation.routes:
            if route in result:
                raise ShaderGraphError("Two editable implementations own the same renderer route.")
            result[route] = implementation
    return result


def _cached_contract_fingerprint(contract: ShaderContract, fingerprints: dict) -> str:
    try:
        fingerprint = fingerprints.get(contract)
    except TypeError:
        return contract.fingerprint
    if fingerprint is None:
        fingerprint = contract.fingerprint
        fingerprints[contract] = fingerprint
    return fingerprint


def group_implementation_routes(document, routes, owners):
    from .material_project import material_program_contract

    fingerprints = {}
    owner_hashes = {}
    grouped = {}
    for route in routes:
        target, index, role = route
        shader = document.sdf.targets[target].programs[index].shader(role)
        try:
            contract = material_program_contract(document, *route,
                allow_unreflected_material_fields=True)
            contract_key = _cached_contract_fingerprint(contract, fingerprints)
        except (ShaderGraphError, ValueError):
            contract_key = route
        owner = owners.get(route)
        owner_hash = None
        if owner is not None:
            identity = id(owner)
            if identity not in owner_hashes:
                owner_hashes[identity] = owner.content_hash
            owner_hash = owner_hashes[identity]
        key = role, shader.payload, contract_key, owner_hash
        grouped.setdefault(key, []).append(route)
    return {members[0]: tuple(members) for members in grouped.values()}


@dataclass(frozen=True, slots=True)
class CompiledImplementation:
    bytecode: bytes
    semantic_contract: ShaderContract
    binary_format: str = "dxbc-container"
    diagnostics: str = ''


@dataclass(frozen=True)
class ImplementationExecution:
    early_depth_stencil: bool
    primitive_execution: ShaderPrimitiveExecution | None = None
    mesh_execution: ShaderMeshExecution | None = None


def implementation_contract(contract, bytecode):
    if bytes(bytecode[:4]) == b'\x03\x02\x23\x07':
        from file_handlers.sdf.spirv_implementation import complete_vulkan_contract
        return complete_vulkan_contract(contract, bytecode)
    if contract.model_major >= 6:
        from .profiles.re_engine_dxil_signatures import DxilEntryMetadata
        from .native_resource import complete_typed_resources
        lines = disassemble_dxil(bytecode).splitlines()
        entry = DxilEntryMetadata.parse(lines)
        contract = complete_typed_resources(contract, entry, lines)
    if contract.stage == 'compute':
        if contract.model_major >= 6:
            dimensions = entry.thread_group_size()
        else:
            from file_handlers.sdf.hlsl import dxbc_thread_group_size
            dimensions = dxbc_thread_group_size(bytecode)
        if contract.thread_group_size is not None and contract.thread_group_size != dimensions:
            raise ShaderGraphError('Reflected and native thread-group dimensions disagree.')
        return replace(contract, thread_group_size=dimensions)
    if contract.model_major >= 6:
        try:
            inputs, outputs = entry.input_interpolation(), entry.output_interpolation(streams=True)
        except DxcError as exc:
            raise ShaderGraphError(str(exc)) from exc
        execution = entry.primitive_execution()
        if contract.primitive_execution is not None and contract.primitive_execution != execution:
            raise ShaderGraphError('Native primitive execution disagrees with its renderer contract.')
        mesh_execution = entry.mesh_execution()
        if contract.mesh_execution is not None and contract.mesh_execution != mesh_execution:
            raise ShaderGraphError('Native mesh execution disagrees with its renderer contract.')
        dimensions = entry.thread_group_size() if mesh_execution is not None else contract.thread_group_size
        if contract.thread_group_size is not None and contract.thread_group_size != dimensions:
            raise ShaderGraphError('Reflected and native thread-group dimensions disagree.')
        patches = entry.patch_interpolation()
        return replace(contract,primitive_execution=execution,mesh_execution=mesh_execution,thread_group_size=dimensions,
            patch_constants=tuple(replace(field, interpolation=patches.get(field.key, field.interpolation)) for field in contract.patch_constants),
            inputs=tuple(replace(field, interpolation=inputs.get(field.key, field.interpolation)) for field in contract.inputs),
            outputs=tuple(replace(field, interpolation=outputs.get(field.stream_key, field.interpolation)) for field in contract.outputs))
    if contract.stage in {'vertex','geometry','domain','hull'}:
        from .profiles.dxbc_recovery import recover_dxbc_ir
        from .profiles.dxbc_stages import signature_packing_modes
        execution = recover_dxbc_ir(bytecode).execution if contract.stage != 'vertex' else None
        if contract.primitive_execution is not None and contract.primitive_execution != execution:
            raise ShaderGraphError('Native primitive execution disagrees with its renderer contract.')
        inputs = signature_packing_modes(contract.inputs) if contract.stage != 'vertex' else {}
        outputs = {stream:signature_packing_modes(tuple(field for field in contract.outputs if field.stream==stream))
                   for stream in {field.stream for field in contract.outputs}}
        return replace(contract, primitive_execution=execution,
            inputs=tuple(replace(field, interpolation=inputs.get(field.key, field.interpolation)) for field in contract.inputs),
            outputs=tuple(replace(field, interpolation=outputs[field.stream].get(field.key, field.interpolation)) for field in contract.outputs))
    if contract.stage != "pixel" or not contract.inputs:
        return contract
    from .profiles.dxbc_recovery import recover_dxbc_ir
    from .profiles.dxbc_stages import pixel_signature_interpolation
    modes = pixel_signature_interpolation(contract.inputs, recover_dxbc_ir(bytecode).input_interpolation)
    return replace(contract, inputs=tuple(replace(field, interpolation=modes.get(field.key, field.interpolation))
        for field in contract.inputs))


def _check_interpolation(reference, candidate):
    for name in ('inputs', 'outputs'):
        originals = {field.stream_key: field for field in getattr(reference, name)}
        for field in getattr(candidate, name):
            original = originals.get(field.stream_key)
            if (name == 'outputs' or field.read_write_mask) and original is not None and original.interpolation and original.interpolation != field.interpolation:
                raise ShaderGraphError(f"The replacement changed live interpolation of {field.display_name}.")


def implementation_execution(bytecode):
    if bytes(bytecode[:4]) == b'\x03\x02\x23\x07':
        from file_handlers.sdf.spirv_contract import spirv_shader_contract
        contract = implementation_contract(spirv_shader_contract(bytecode), bytecode)
        return ImplementationExecution(contract.vulkan_execution.early_fragment_tests, contract.primitive_execution)
    shader = parse_dxbc(bytecode)
    if shader.unknown_feature_flags:
        raise ShaderGraphError("The renderer template has unidentified shader feature flags.")
    if shader.model_major >= 6:
        from .profiles.re_engine_dxil_signatures import DxilEntryMetadata
        try:
            entry = DxilEntryMetadata.parse(disassemble_dxil(bytecode).splitlines())
        except DxcError as exc:
            raise ShaderGraphError(str(exc)) from exc
        # ForceEarlyDepthStencil is bit 3 of the DXIL entry ShaderFlags.
        return ImplementationExecution(bool(entry.shader_flags() & (1 << 3)),entry.primitive_execution(),entry.mesh_execution())
    from .profiles.dxbc_recovery import recover_dxbc_ir
    ir = recover_dxbc_ir(bytecode)
    if "refactoringAllowed" not in ir.global_flags:
        raise ShaderGraphError("This renderer template needs an explicit graph precision policy before its body can be replaced.")
    return ImplementationExecution("forceEarlyDepthStencil" in ir.global_flags, ir.execution if ir.stage in {'gs','ds','hs'} else None)


def _retain_contract_bindings(bytecode, reflected, contract):
    def bindings(resources):
        return {(item.register_class, int(item.register_space), int(item.bind_point)) for item in resources}

    if bindings(contract.resources) <= bindings(ShaderContract.from_shader(reflected).resources):
        return bytecode, reflected
    try:
        retained = _rebuilt_dxil(bytecode, reflected, contract)
    except (ShaderGraphError, DxcError, ValueError):
        return bytecode, reflected  # Callers' ABI checks report the dropped binding.
    return retained, enrich_dxil_shader(parse_dxbc(retained), retained)


def _rebuilt_dxil(bytecode, reflected, contract, **options):
    from .profiles.re_engine_dxil_recovery import recover_re_engine_dxil_ir
    from .profiles.re_engine_dxil_authoring import rebuild_re_engine_dxil
    source = implementation_contract(ShaderContract.from_shader(reflected), bytecode)
    return rebuild_re_engine_dxil(recover_re_engine_dxil_ir(bytecode), source, contract, **options).bytecode


def _implementation_build_key(generated, contract, binary_format):
    arguments = _IMPLEMENTATION_DXIL_ARGUMENTS + (('-enable-16bit-types',) if generated.native_16bit else ())
    runtime = _implementation_build_runtime()
    return arguments, native_build_key(runtime, generated, contract, binary_format,
        arguments, _IMPLEMENTATION_DXIL_CONFIGURATION) if runtime is not None else None


def _inline_precise_helpers(bytecode):
    assembly = disassemble_dxil(bytecode)
    if 'REasyPrecise_' not in assembly:
        return bytecode
    from .profiles.re_engine_dxil_authoring import _DXIL_OPTIMIZER_PASSES, _attach_reflection
    from file_handlers.sdf.dxc import assemble_dxil, optimize_dxil
    bitcode = optimize_dxil(re.sub(r"\bnoinline\b", "alwaysinline", assembly), _DXIL_OPTIMIZER_PASSES)
    return assemble_dxil(_attach_reflection(disassemble_dxil(bitcode), bytecode)).bytecode


def compile_implementation(generated, contract, binary_format):
    if binary_format == 'spirv-module':
        from file_handlers.sdf.spirv_implementation import compile_vulkan_implementation
        return compile_vulkan_implementation(generated, contract)
    if binary_format != "dxbc-container":
        raise ShaderGraphError("Graph-owned bodies need an explicit native binding adapter for this shader backend.")
    cache_key = None
    if contract.model_major >= 6:
        try:
            if generated.native_16bit and (contract.model_major, contract.model_minor) < (6, 2):
                raise ShaderGraphError('Native 16-bit arithmetic requires Shader Model 6.2 or later.')
            generated, contract = deepcopy(generated), deepcopy(contract)
            arguments, cache_key = _implementation_build_key(generated, contract, binary_format)
            cached = _IMPLEMENTATION_BUILD_CACHE.get(cache_key)
            if cached is not None:
                return cached
            # DXC's wave-sensitivity warning on gradient intrinsics is advisory.
            result = compile_hlsl_dxc(generated.source, entry_point=generated.entry_point, target=generated.target,
                extra_arguments=arguments)
            warnings = re.findall(r'^(?:.*?:\d+:\d+: )?warning: (.*)$', result.diagnostics, re.MULTILINE)
            advisory = 'Gradient operations are not affected by wave-sensitive data or control flow. [-Winline-asm]'
            allowed = {advisory} | {f'Declared output {semantic}{index} not fully written in shader. [-Winline-asm]'
                                   for semantic,index,_mask in generated.unwritten_outputs}
            if any(message.strip() not in allowed for message in warnings):
                raise DxcError(result.diagnostics)
            bytecode = result.bytecode
            if generated.structured_view_spaces:
                from file_handlers.sdf.dxil_structured_views import coalesce_structured_views
                bytecode = coalesce_structured_views(bytecode, contract, generated.structured_view_spaces)
            if contract.stage=='geometry':
                from .profiles.re_engine_dxil_signatures import DxilEntryMetadata
                from file_handlers.sdf.dxc import assemble_dxil
                lines = disassemble_dxil(bytecode).splitlines()
                entry = DxilEntryMetadata.parse(lines)
                restored = entry.geometry_stream_declaration(lines,contract.primitive_execution.output_streams)
                if restored != lines:
                    bytecode = assemble_dxil('\n'.join(restored)+'\n').bytecode
            bytecode = validate_dxil(_inline_precise_helpers(bytecode))
            reflected = enrich_dxil_shader(parse_dxbc(bytecode), bytecode)
            from file_handlers.sdf.shader_graph_constant_buffer import constant_view_overrides
            views = constant_view_overrides(generated.source)
            if views:
                bytecode = _rebuilt_dxil(bytecode, reflected, contract, constant_view_overrides=views)
                reflected = enrich_dxil_shader(parse_dxbc(bytecode), bytecode)
            bytecode, reflected = _retain_contract_bindings(bytecode, reflected, contract)
        except DxcError as exc:
            raise ShaderGraphError(str(exc)) from exc
    else:
        if generated.native_16bit:
            raise ShaderGraphError('Native 16-bit arithmetic requires Shader Model 6.2 or later; minimum precision is a separate type.')
        # FXC's X4000 pragma state is translation-unit-wide, so it cannot be disabled locally.
        unspecified_returns = {'} return REasyUnassignedCaseResult;': 'REasyUnassignedCaseResult',
                               'return REasyUnassignedValue;': 'REasyUnassignedValue'}
        unspecified_lines = {i: unspecified_returns[line.strip()] for i,line in enumerate(generated.source.splitlines(),1)
                             if line.strip() in unspecified_returns}
        repeat_lines = {i for i, line in enumerate(generated.source.splitlines(), 1) if line.strip() == '[loop] while (true)'}
        result = compile_hlsl_source_detailed(generated.source, entry_point=generated.entry_point,
            target=generated.target, warnings_as_errors=False, ieee_strict=generated.ieee_strict,
            optimize=not generated.signed_zero_constants)
        float_advisory = r'.+\(\d+,\d+(?:-\d+)?\): warning (?:X4008: floating point division by zero|X4118: imaginary square root)'
        gradient_advisory = (r'.+\(\d+,\d+(?:-\d+)?\): warning X3595: gradient instruction used in a loop with '
                             r'varying iteration; partial derivatives may have undefined value')
        # FXC's optimizer collapses the sign of constant zeros, even with /Gis.
        if not generated.signed_zero_constants and any(re.fullmatch(float_advisory, line.strip()) for line in result.diagnostics.splitlines()):
            checked = compile_hlsl_source_detailed(generated.source, entry_point=generated.entry_point,
                target=generated.target, warnings_as_errors=False, ieee_strict=generated.ieee_strict, optimize=False)
            result = replace(checked, diagnostics='\n'.join(dict.fromkeys(
                (*result.diagnostics.rstrip('\0').splitlines(), *checked.diagnostics.rstrip('\0').splitlines()))))
        for line in result.diagnostics.splitlines():
            if 'warning' not in line.casefold():
                continue
            # Float division/rsq at zero and sqrt of negatives are IEEE-defined, so FXC's advisory about them is
            # harmless.
            if re.fullmatch(float_advisory,line.strip()) or re.fullmatch(gradient_advisory, line.strip()):
                continue
            trips = re.fullmatch(r'.+\((\d+),\d+(?:-\d+)?\): warning X3557: loop only executes for \d+ iteration\(s\), '
                                 r'consider removing \[loop\]', line.strip())
            if trips and int(trips[1]) in repeat_lines:
                continue
            if generated.unwritten_outputs and re.fullmatch(r".+\(\d+,\d+(?:-\d+)?\): warning X3578: Output value '" + re.escape(generated.entry_point) + r"' is not completely initialized", line.strip()):
                continue
            if generated.unassigned_values and re.fullmatch(r'.+\(\d+,\d+(?:-\d+)?\): warning X4000: use of potentially uninitialized variable \((?:REasyBranch_[0-9a-f]+|REasyRepeat_[0-9a-f]+|reasyGraphShared\d+|reasyRegion_[0-9a-f]{24}(?:Value|State\d+|Arm[01]Shared\d+))\)', line.strip()):
                continue
            match = re.fullmatch(r'.+\((\d+),\d+\): warning X4000: use of potentially uninitialized variable \((REasyUnassignedCaseResult|REasyUnassignedValue)\)',line.strip())
            if match is None or unspecified_lines.get(int(match[1])) != match[2]:
                raise ShaderGraphError(result.diagnostics)
        reflected = parse_dxbc(result.bytecode)
        bytecode = result.bytecode
    semantic_contract = implementation_contract(ShaderContract.from_shader(reflected), bytecode)
    for semantic, index, mask in generated.unwritten_outputs:
        output = next((field for field in semantic_contract.outputs if field.key == (semantic.casefold(), index)), None)
        if output is None or output.read_write_mask & mask != mask:
            raise ShaderGraphError('Compilation did not retain the explicitly unwritten output components.')
    _check_interpolation(contract, semantic_contract)
    completed = CompiledImplementation(bytecode, semantic_contract, diagnostics=result.diagnostics.rstrip('\0').strip())
    if cache_key is not None and validate_shader_contract(contract, semantic_contract).compatible:
        try:
            after_key = _implementation_build_key(generated, contract, binary_format)[1]
        except (TypeError, ValueError, RecursionError, OverflowError):
            after_key = None
        if after_key == cache_key:
            _IMPLEMENTATION_BUILD_CACHE.put(cache_key, completed)
    return completed


__all__ = ["MaterialProgramImplementation", "implementation_routes", "compile_implementation", "implementation_execution", "implementation_contract"]
