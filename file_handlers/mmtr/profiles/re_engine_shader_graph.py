from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
from pathlib import Path
import re
from typing import Mapping
from file_handlers.material_names import logical_material_parameter_name
from file_handlers.mdf.material_layout import MaterialDescriptorLayout

from file_handlers.sdf.contracts import (
    shader_contract_for_program,
)
from file_handlers.sdf.dxc import DxcError, enrich_dxil_shader
from file_handlers.sdf.dxbc import parse_dxbc
from file_handlers.sdf.hlsl import compile_hlsl_source_detailed
from file_handlers.sdf.models import RESOURCE_KINDS, STAGE_KEYS
from file_handlers.sdf.spirv import (
    SpirvDescriptor,
    SpirvError,
    SpirvReflection,
    reflect_spirv,
)
from file_handlers.sdf import storage_codec
from file_handlers.sdf.spirv_ir import SpirvSemanticIr
from file_handlers.sdf.spirv_contract import spirv_shader_contract
from file_handlers.sdf.spirv_tools import validate_spirv
from file_handlers.sdf.shader_graph import (
    ShaderContract,
    ShaderContractBuffer,
    ShaderContractVariable,
    ShaderGraph,
    ShaderGraphError,
    ShaderGraphNode,
    ShaderGraphNodeExtension,
    register_shader_graph_node_extension,
    splat_expression,
    texture_hlsl_type,
)

from file_handlers.sdf.hlsl_contract import resource_identifier
from file_handlers.sdf.shader_contract_storage import contract_from_storage, contract_to_storage
from ..native_build_cache import NativeBuildCache, native_build_key
from ..models import BINDLESS_PARAMETER_STORAGES, MmtrMaterialInterface, MmtrProgramSemantics, MmtrResourceContract
from ..pass_purpose import graph_draws_for, pass_purpose
from ..surface_data import graph_reads_surface_data, program_surface_data, program_vertex_inputs
from ..surface_meaning import pixel_shader_digest
from ..vertex_values import PER_VERTEX_KIND, add_vertex_values, vertex_value_plan
from .base import bound_texture_names, same_mmtr_profile
from .dxbc_recovery import (
    DxbcSemanticIr,
    generate_recovered_dxbc_shader,
    recover_dxbc_ir,
    relocated_dxbc_inputs,
    validate_recovered_dxbc_shader_bytecode,
)
from .material_graph_expression import (
    recovered_graph_expression,
    recovered_master_pins,
)
from file_handlers.sdf.shader_graph_math import value_width
from .re_engine_dxil_authoring import rebuild_re_engine_dxil_batch
from .re_engine_dxil_recovery import ReEngineDxilSemanticIr, recover_re_engine_dxil_ir
from . import re_engine_dxil_recovery as _dxil_recovery
from .re_engine_bindless_material import material_record_accesses, material_texture_accesses
from .re_engine_spirv_authoring import rebuild_spirv, spirv_uniform_reads
from .dxbc_stages import with_pixel_interpolation
from .renderer_inputs import (
    RENDERER_VALUE_KIND, add_engine_buffers, add_engine_resources, graph_renderer_buffers, graph_renderer_resources,
    renderer_buffer, renderer_input_pins, renderer_only,
)
from .constant_words import ConstantWord


RE_ENGINE_RECOVERED_MASTER_KIND = "re_engine_recovered_master_material"
SEMANTIC_BUNDLE_VERSION = 4
_MODEL_PREFIX = "re_engine.recovered_master."
_DXIL_PROGRAM_CACHE = NativeBuildCache(max_entries=2048, max_bytes=64 * 1024 * 1024)
_DXIL_INPUT_ANALYSIS_CACHE = NativeBuildCache(max_entries=2048, max_bytes=16 * 1024 * 1024)


def _master_expression(_node, _pin, _incoming):
    raise ShaderGraphError(
        "Recovered RE Engine master materials are compiled by the semantic material builder."
    )


RE_ENGINE_RECOVERED_MASTER_NODE = register_shader_graph_node_extension(
    ShaderGraphNodeExtension(
        kind=RE_ENGINE_RECOVERED_MASTER_KIND,
        title="Recovered Master Material",

        pins_provider=recovered_master_pins,
        expression=_master_expression,
        description=(
            "The complete shipped master-material function recovered from the "
            "game's compiled programs. Its typed MDF parameters and texture "
            "slots remain ordinary editable graph connections."
        ),
    )
)


def _is_project(project, profile_id: str) -> bool:
    return bool(
        str(getattr(project, "source_material_model", "")).startswith(
            _MODEL_PREFIX + str(profile_id).casefold() + "."
        )
        and any(node.kind == RE_ENGINE_RECOVERED_MASTER_KIND for node in project.graph.nodes)
    )


def _master(project) -> ShaderGraphNode:
    values = [node for node in project.graph.nodes if node.kind == RE_ENGINE_RECOVERED_MASTER_KIND]
    if len(values) != 1:
        raise ShaderGraphError("Recovered material needs exactly one master-material function.")
    return values[0]


@dataclass(frozen=True, slots=True)
class _ValidatedDxilAssembly:
    assembly: str

    def to_assembly(self) -> str:
        return self.assembly


@dataclass(frozen=True, slots=True)
class _RecoveredProgram:
    ir_kind: str
    ir: DxbcSemanticIr | _ValidatedDxilAssembly | SpirvSemanticIr
    contract: ShaderContract
    digest: str
    binary_format: str


@dataclass(frozen=True, slots=True)
class _DxilProduct:
    ir: ReEngineDxilSemanticIr | _ValidatedDxilAssembly
    source_contract: ShaderContract
    scalar_expressions: Mapping[ConstantWord, str]
    texture_overrides: Mapping[tuple[int, int], tuple[int, int]]
    helpers: str
    material_expressions: Mapping[int, str] | None = None
    material_buffer_size: int = 0
    source: str = "// DXIL semantic material product"
    entry_point: str = "main"
    target: str = "ps_6_0"
    material_word_offsets: Mapping[int, int] | None = None
    surface_plan: tuple = ()

    def rebuild_options(self) -> dict[str, object]:
        return {
            "scalar_expressions": self.scalar_expressions,
            "texture_binding_overrides": self.texture_overrides,
            "helpers": self.helpers,
            "material_expressions": self.material_expressions,
            "material_buffer_size": self.material_buffer_size,
            "material_word_offsets": self.material_word_offsets,
            "surface_plan": self.surface_plan,
        }


@dataclass(frozen=True, slots=True)
class _SpirvProduct:
    ir: SpirvSemanticIr
    source_contract: ShaderContract
    logical_contract: ShaderContract
    native: SpirvReflection
    resource_kinds: Mapping[str, str]
    scalar_expressions: Mapping[ConstantWord, str]
    texture_overrides: Mapping[tuple[int, int], tuple[int, int]]
    helpers: str = ""
    renamed_members: tuple[tuple[str, str], ...] = ()
    surface_plan: tuple = ()

    @property
    def source(self) -> str:
        return "// Native SPIR-V semantic material product"

    @property
    def entry_point(self) -> str:
        return self.native.entry_point

    @property
    def target(self) -> str:
        return replace(self.logical_contract, model_major=6, model_minor=0).target


@dataclass(frozen=True, slots=True)
class _NativeCompileResult:
    bytecode: bytes
    semantic_contract: ShaderContract
    binary_format: str = "dxbc-container"


def _native_spirv(shader) -> SpirvReflection:
    if shader is None or shader.spirv is None:
        raise ShaderGraphError("SPIR-V material route has no decoded native module.")
    try:
        reflected = reflect_spirv(
            shader.spirv.module,
            entry_point=shader.spirv.entry_point,
        )
    except SpirvError as exc:
        raise ShaderGraphError(f"SPIR-V material route is malformed: {exc}") from exc
    return reflected


def _outer_resource_kinds(program, role: str) -> dict[str, str]:
    try:
        stage_index = STAGE_KEYS.index(str(role))
    except ValueError as exc:
        raise ShaderGraphError(f"Unknown shader stage {role!r}.") from exc
    groups = (
        program.constant_buffers,
        program.samplers,
        program.shader_resources,
        program.unordered_access_resources,
        program.optional_resources,
    )
    result: dict[str, str] = {}
    for kind, metadata, bindings in zip(RESOURCE_KINDS, groups, program.bindings):
        if len(metadata) != len(bindings):
            raise ShaderGraphError("SPIR-V resource metadata and bindings disagree.")
        for item, binding in zip(metadata, bindings):
            if not (int(binding.stage_mask) & (1 << stage_index)):
                continue
            key = str(item.name).casefold()
            previous = result.setdefault(key, kind)
            if previous != kind:
                raise ShaderGraphError(
                    f"SPIR-V resource {item.name!r} has two native ownership classes."
                )
    return result


def _logical_resource_kind(resource) -> str:
    if resource.input_type == "constant-buffer":
        return "constant_buffer"
    if resource.input_type == "sampler":
        return "sampler"
    if resource.register_class == "u":
        return "unordered_access"
    return "shader_resource"


def _descriptor_resource_kind(descriptor: SpirvDescriptor) -> str:
    return {
        "constant-buffer": "constant_buffer",
        "sampler": "sampler",
        "texture": "shader_resource",
        "storage-buffer": (
            "shader_resource" if descriptor.register_class == "t" else "unordered_access"
        ),
        "storage-image": "unordered_access",
    }.get(descriptor.descriptor_kind, "optional_resource")


def _spirv_binding_plan(
    resources,
    native: SpirvReflection,
    resource_kinds: Mapping[str, str],
    *,
    used_names: frozenset[str] | None = None,
) -> dict[str, tuple[int, int, str, int]]:
    logical: dict[str, object] = {}
    for resource in resources:
        key = resource.name.casefold()
        if key in logical:
            raise ShaderGraphError(f"SPIR-V contract repeats resource {resource.name!r}.")
        logical[key] = resource
    wanted = frozenset(logical) if used_names is None else frozenset(used_names)
    unknown = wanted - logical.keys()
    if unknown:
        raise ShaderGraphError(
            "SPIR-V compiler retained undeclared resources: " + ", ".join(sorted(unknown))
        )
    sets = {int(item.descriptor_set) for item in native.descriptors}
    if not sets and not wanted:
        return {}
    if len(sets) != 1:
        raise ShaderGraphError("SPIR-V material descriptors do not use one native descriptor set.")
    descriptor_set = next(iter(sets))
    native_order = sorted(native.descriptors, key=lambda item: int(item.binding))
    native_bindings = [int(item.binding) for item in native_order]
    if native_bindings != list(range(len(native_bindings))):
        raise ShaderGraphError("SPIR-V material descriptors do not use the canonical dense binding ABI.")
    native_by_name = {item.name.casefold(): item for item in native_order}
    if len(native_by_name) != len(native_order):
        raise ShaderGraphError("SPIR-V material descriptors repeat a name.")

    grouped: dict[str, list[str]] = {kind: [] for kind in RESOURCE_KINDS}
    for descriptor in native_order:
        key = descriptor.name.casefold()
        if key not in wanted:
            continue
        kind = resource_kinds.get(key, _descriptor_resource_kind(descriptor))
        grouped[kind].append(key)
    new_names = sorted(
        wanted - native_by_name.keys(),
        key=lambda key: (
            RESOURCE_KINDS.index(
                resource_kinds.get(key, _logical_resource_kind(logical[key]))
            ),
            logical[key].register_class,
            int(logical[key].register_space),
            int(logical[key].bind_point),
            key,
        ),
    )
    for key in new_names:
        kind = resource_kinds.get(key, _logical_resource_kind(logical[key]))
        grouped[kind].append(key)

    result: dict[str, tuple[int, int, str, int]] = {}
    binding = 0
    for kind in RESOURCE_KINDS:
        for kind_slot, key in enumerate(grouped[kind]):
            result[key] = (descriptor_set, binding, kind, kind_slot)
            binding += 1
    if set(result) != wanted:
        raise ShaderGraphError("SPIR-V descriptor ownership is incomplete.")
    return result


def _spirv_interface_abi(values) -> frozenset[tuple[int | None, int | None, str]]:
    return frozenset(
        (item.location, item.built_in, item.value_type)
        for item in values
    )


def _spirv_candidate_contract(
    logical: ShaderContract,
    reflected: SpirvReflection,
) -> ShaderContract:
    def validate_block_member(variable, member, path: str) -> None:
        if int(variable.byte_offset) != int(member.byte_offset):
            raise ShaderGraphError(
                f"SPIR-V constant buffer {path} changed byte offset."
            )
        if int(variable.byte_size) != int(member.value_byte_size):
            raise ShaderGraphError(
                f"SPIR-V constant buffer {path} changed semantic byte size."
            )
        if int(variable.array_length) != int(member.array_count):
            raise ShaderGraphError(
                f"SPIR-V constant buffer {path} changed array length."
            )
        if variable.members:
            if not member.members:
                raise ShaderGraphError(
                    f"SPIR-V constant buffer {path} changed from a structure."
                )
            native_members = {
                item.name.casefold(): item for item in member.members
            }
            if len(native_members) != len(member.members):
                raise ShaderGraphError(
                    f"SPIR-V constant buffer {path} repeats a structure member."
                )
            if {
                item.name.casefold() for item in variable.members
            } != set(native_members):
                raise ShaderGraphError(
                    f"SPIR-V constant buffer {path} changed nested fields."
                )
            for child in variable.members:
                validate_block_member(
                    child,
                    native_members[child.name.casefold()],
                    f"{path}.{child.name}",
                )
            if member.matrix_layout:
                raise ShaderGraphError(
                    f"SPIR-V constant buffer {path} changed structure kind."
                )
            return
        if member.members:
            raise ShaderGraphError(
                f"SPIR-V constant buffer {path} changed to a structure."
            )
        declaration = str(variable.hlsl_declaration or "").strip().casefold()
        layout = ""
        if declaration.startswith("row_major "):
            # DXC represents HLSL row vectors as SPIR-V column vectors, so the storage decoration is inverted.
            layout = "column-major"
            declaration = declaration[len("row_major ") :]
        elif declaration.startswith("column_major "):
            layout = "row-major"
            declaration = declaration[len("column_major ") :]
        expected_declaration = declaration
        matrix_declaration = re.fullmatch(
            r"(?P<base>float|int|uint)(?P<rows>[1-4])x(?P<columns>[1-4])",
            declaration,
        )
        if matrix_declaration is not None:
            expected_declaration = (
                f"{matrix_declaration.group('base')}"
                f"{matrix_declaration.group('columns')}x"
                f"{matrix_declaration.group('rows')}"
            )
        if expected_declaration != str(member.value_type).casefold():
            raise ShaderGraphError(
                f"SPIR-V constant buffer {path} changed scalar/vector/matrix type."
            )
        if bool(variable.matrix) != bool(member.matrix_layout):
            raise ShaderGraphError(
                f"SPIR-V constant buffer {path} changed matrix kind."
            )
        if variable.matrix and layout != member.matrix_layout:
            raise ShaderGraphError(
                f"SPIR-V constant buffer {path} changed matrix orientation."
            )

    resources_by_name = {item.name.casefold(): item for item in logical.resources}
    buffers_by_name = {item.name.casefold(): item for item in logical.constant_buffers}
    structured_by_name = {
        item.name.casefold(): item for item in logical.structured_buffers
    }
    used_resources = []
    used_buffers = []
    used_structured_buffers = []
    for descriptor in reflected.descriptors:
        key = descriptor.name.casefold()
        resource = resources_by_name.get(key)
        if resource is None:
            raise ShaderGraphError(
                f"SPIR-V compiler retained unknown resource {descriptor.name!r}."
            )
        expected_kind = _logical_resource_kind(resource)
        if (
            expected_kind == "constant_buffer"
            and descriptor.descriptor_kind != "constant-buffer"
        ) or (
            expected_kind == "sampler" and descriptor.descriptor_kind != "sampler"
        ) or (
            expected_kind == "shader_resource"
            and descriptor.register_class != "t"
        ) or (
            expected_kind == "unordered_access"
            and descriptor.register_class != "u"
        ):
            raise ShaderGraphError(
                f"SPIR-V resource {resource.name} changed native resource class."
            )
        if resource.input_type == "texture":
            if descriptor.dimension != resource.dimension:
                raise ShaderGraphError(
                    f"SPIR-V texture {resource.name} changed dimension."
                )
            if descriptor.return_type != ('int' if resource.return_type == 'sint' else resource.return_type):
                raise ShaderGraphError(
                    f"SPIR-V texture {resource.name} changed scalar type."
                )
        if int(descriptor.array_count) != int(resource.bind_count):
            raise ShaderGraphError(
                f"SPIR-V resource {resource.name} changed descriptor count."
            )
        used_resources.append(resource)
        if descriptor.descriptor_kind == "constant-buffer":
            buffer = buffers_by_name.get(key)
            if buffer is None or int(descriptor.byte_size) != int(buffer.byte_size):
                raise ShaderGraphError(
                    f"SPIR-V constant buffer {resource.name} changed byte extent."
                )
            reflected_members = {
                item.name.casefold(): item for item in descriptor.members
            }
            if len(reflected_members) != len(descriptor.members):
                raise ShaderGraphError(
                    f"SPIR-V constant buffer {resource.name} repeats a field."
                )
            for variable in buffer.variables:
                member = reflected_members.get(variable.name.casefold())
                if member is None:
                    raise ShaderGraphError(
                        f"SPIR-V constant buffer {resource.name}.{variable.name} changed layout."
                    )
                validate_block_member(
                    variable,
                    member,
                    f"{resource.name}.{variable.name}",
                )
            used_buffers.append(buffer)
        elif (
            descriptor.descriptor_kind == "storage-buffer"
            and resource.input_type in {"structured-buffer", "rw-structured-buffer", "rw-structured-buffer-counter"}
        ):
            buffer = structured_by_name.get(key)
            if buffer is None or int(buffer.byte_size) <= 0:
                raise ShaderGraphError(
                    f"SPIR-V structured buffer {resource.name} has no semantic layout."
                )
            if len(descriptor.members) != 1:
                raise ShaderGraphError(
                    f"SPIR-V structured buffer {resource.name} has no unique element array."
                )
            element = descriptor.members[0]
            if (
                int(element.array_count) != 0
                or int(element.array_stride) != int(buffer.byte_size)
            ):
                raise ShaderGraphError(
                    f"SPIR-V structured buffer {resource.name} changed element stride."
                )
            if not element.members:
                if (
                    len(buffer.variables) != 1
                    or buffer.variables[0].name != "$Element"
                    or buffer.variables[0].hlsl_declaration != element.value_type
                    or buffer.variables[0].byte_size != element.array_stride
                    or buffer.variables[0].byte_offset != 0
                ):
                    raise ShaderGraphError(f"SPIR-V structured buffer {resource.name} changed its scalar/vector element type.")
                used_structured_buffers.append(buffer)
                continue
            native_members = {
                item.name.casefold(): item for item in element.members
            }
            if len(native_members) != len(element.members):
                raise ShaderGraphError(
                    f"SPIR-V structured buffer {resource.name} repeats an element field."
                )
            physical_slots = all(
                re.fullmatch(r"slot\d+", name) is not None
                for name in native_members
            )
            if physical_slots:
                words = int(buffer.byte_size) // 4
                expected_slots = {
                    f"slot{slot}": (
                        slot * 16,
                        min(4, words - slot * 4),
                    )
                    for slot in range((words + 3) // 4)
                }
                if set(native_members) != set(expected_slots):
                    raise ShaderGraphError(
                        f"SPIR-V structured buffer {resource.name} changed its typed word carrier."
                    )
                for name, (byte_offset, width) in expected_slots.items():
                    member = native_members[name]
                    value_type = "uint" if width == 1 else f"uint{width}"
                    if (
                        int(member.byte_offset) != byte_offset
                        or int(member.value_byte_size) != width * 4
                        or member.value_type != value_type
                        or int(member.array_count) != 1
                        or member.matrix_layout
                        or member.members
                    ):
                        raise ShaderGraphError(
                            f"SPIR-V structured buffer {resource.name}.{name} "
                            "changed its typed word carrier."
                        )
            else:
                if {
                    item.name.casefold() for item in buffer.variables
                } != set(native_members):
                    raise ShaderGraphError(
                        f"SPIR-V structured buffer {resource.name} changed semantic fields."
                    )
                for variable in buffer.variables:
                    validate_block_member(
                        variable,
                        native_members[variable.name.casefold()],
                        f"{resource.name}.{variable.name}",
                    )
            used_structured_buffers.append(buffer)
    return replace(
        logical,
        resources=tuple(used_resources),
        constant_buffers=tuple(used_buffers),
        structured_buffers=tuple(used_structured_buffers),
    )


def _compile_spirv_product(product: _SpirvProduct) -> _NativeCompileResult:
    assignments = _spirv_binding_plan(
        product.logical_contract.resources, product.native, product.resource_kinds,
    )
    bytecode = rebuild_spirv(
        product.ir, product.source_contract, product.logical_contract,
        scalar_expressions=product.scalar_expressions, texture_overrides=product.texture_overrides,
        helpers=product.helpers, assignments=assignments, renamed_members=dict(product.renamed_members),
        surface_plan=product.surface_plan,
    )
    reflected = reflect_spirv(bytecode, entry_point=product.entry_point)
    assignments = _spirv_binding_plan(
        product.logical_contract.resources, product.native, product.resource_kinds,
        used_names=frozenset(item.name.casefold() for item in reflected.descriptors),
    )
    by_id = {item.variable_id: assignments[item.name.casefold()] for item in reflected.descriptors}
    rows = []
    ir = SpirvSemanticIr.from_bytes(bytecode)
    for row in ir.instructions:
        if row.opcode == 71 and row.values[0] in by_id and row.values[1] in {33, 34}:
            operands = list(row.operands)
            operands[2] = replace(operands[2], value=by_id[row.values[0]][1 if row.values[1] == 33 else 0])
            row = replace(row, operands=tuple(operands))
        rows.append(row)
    bytecode = replace(ir, instructions=tuple(rows)).write()
    validate_spirv(bytecode)
    reflected = reflect_spirv(bytecode, entry_point=product.entry_point)
    if (reflected.stage, reflected.execution_modes) != (product.native.stage, product.native.execution_modes):
        raise ShaderGraphError("Native Vulkan stage execution contract changed.")
    for name in ("inputs", "outputs"):
        native = {item.key for item in getattr(product.source_contract, name)}
        added = {(int(field.register), None, f"{field.component_type[:-2].replace('sint', 'int')}4")
                 for field in getattr(product.logical_contract, name) if field.key not in native}
        if _spirv_interface_abi(getattr(reflected, name)) != _spirv_interface_abi(getattr(product.native, name)) | added:
            raise ShaderGraphError(f"Native Vulkan {name} interface changed.")
    return _NativeCompileResult(bytecode, _spirv_candidate_contract(product.logical_contract, reflected),
                                binary_format="spirv-module")


def _recover_shader(document, profile, target_index, program_index, role, interpolation_cache) -> dict[str, object]:
    source_program = document.sdf.targets[target_index].programs[program_index]
    shader = source_program.shader(role)
    raw = bytes(shader.payload)
    contract = profile.shader_contract(document, source_program, role)
    if shader.spirv:
        kind = "spirv"
        semantic_ir = SpirvSemanticIr.from_bytes(shader.spirv.module).to_storage()
    elif int(contract.model_major) < 6:
        kind = "dxbc-sm5"
        semantic_ir = recover_dxbc_ir(raw).to_storage()
        # SM5 producer signatures omit interpolation; the paired pixel program supplies it.
        pixel = source_program.shader("pixel")
        last_stage = next((stage for stage in ("geometry", "domain", "vertex")
                           if source_program.shader(stage) and source_program.shader(stage).present), None)
        if role == last_stage and pixel and pixel.present and pixel.binary_format == "dxbc-container" and pixel.reflection.model_major < 6:
            if pixel.payload not in interpolation_cache:
                interpolation_cache[pixel.payload] = recover_dxbc_ir(pixel.payload).input_interpolation
            pixel_contract = profile.shader_contract(document, source_program, "pixel")
            contract = with_pixel_interpolation(contract, pixel_contract, interpolation_cache[pixel.payload])
    else:
        kind = "dxil"
        semantic_ir = _recover_dxil_recipe(raw)
    return {"ir_kind": kind, "semantic_ir": semantic_ir, "semantic_contract": contract_to_storage(contract)}


def _route_record(document, target_index, program_index, role) -> dict[str, object]:
    shader = document.sdf.targets[target_index].programs[program_index].shader(role)
    return {"target": int(target_index), "program": int(program_index), "role": role,
            "source_sha256": pixel_shader_digest(shader), "binary_format": str(shader.binary_format)}


def _bundle(document, profile, *, recover_programs: bool = True) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    programs: dict[str, dict[str, object]] = {}
    interpolation_cache = {}
    routes: list[dict[str, object]] = []
    for target_index, program_index, role in profile.material_program_routes(document):
        route = _route_record(document, target_index, program_index, role)
        digest = str(route["source_sha256"])
        if digest not in programs:
            programs[digest] = {"source_sha256": digest}
            if recover_programs:
                programs[digest].update(_recover_shader(document, profile, target_index, program_index, role,
                                                        interpolation_cache))
        routes.append(route)
    if not programs or not routes:
        raise ShaderGraphError("Master material has no recoverable material program.")
    return (
        [programs[key] for key in sorted(programs)],
        sorted(routes, key=lambda item: (int(item["target"]), int(item["program"]), str(item["role"]))),
    )


def _surface_route_table(document, profile) -> list[dict[str, object]]:
    material = set(profile.material_program_routes(document))
    return [_route_record(document, target_index, program_index, shader.role)
            for target_index, target in enumerate(document.sdf.targets)
            for program_index, program in enumerate(target.programs) for shader in program.shaders
            if shader.present and shader.role in profile.authoring_roles
            and (target_index, program_index, shader.role) not in material]


def _recover_project_programs(document, project, profile) -> None:
    master = _master(project)
    programs, routes = _bundle(document, profile)
    master.properties.update(
        semantic_programs=programs,
        program_routes=routes,
        semantic_bundle_version=SEMANTIC_BUNDLE_VERSION,
    )


def _texture_input(graph, definition, *, x, y):
    node = next((n for n in graph.nodes if n.kind == "material_texture"
                 and n.properties.get("texture_id") == definition.texture_id), None)
    if node is None:
        node = graph.add_node("material_texture", x=x, y=y, properties={
            "texture_id": definition.texture_id, "name": definition.name,
            "display_name": definition.name.replace("_", " "), "dimension": definition.dimension,
        })
    pin = {"pin": f"texture_{definition.texture_id}", "label": definition.name.replace("_", " "),
           "value_type": definition.dimension or "texture", "input_kind": "texture",
           "source_id": definition.texture_id, "source_name": definition.source_name or definition.name}
    return node, pin


def _expose_descriptor_inputs(document, project, profile):
    from uuid import uuid4
    from ..material_project import MaterialTextureDefinition
    master = _master(project)
    layouts = master.properties.get("descriptor_layouts", ())
    if len(layouts) != 1:
        return False
    original_names = set(layouts[0]["textures"])
    pins = master.properties["input_pins"]
    represented = {pin.get("source_name") for pin in pins if pin.get("input_kind") == "texture"}
    definitions = {t.name: t for t in project.textures}
    changed = False
    for resource in profile.editable_textures(document):
        if resource.type_names or resource.name not in original_names or resource.name in represented:
            continue
        definition = definitions.get(resource.name)
        if definition is None:
            definition = MaterialTextureDefinition(uuid4().hex, resource.name, role="custom", dimension=None, source_name=resource.name)
            project.textures.append(definition)
        node, pin = _texture_input(project.graph, definition, x=master.x - 340, y=master.y + len(pins) * 35)
        # Reporting constrains the descriptor identity, not the chosen image type.
        pin["value_type"] = "texture"
        pins.append(pin)
        project.graph.connect(node.node_id, "texture_object", master.node_id, pin["pin"])
        if resource.name not in project.source_texture_names:
            project.source_texture_names.append(resource.name)
        changed = True
    return changed


def _surface_sources(document, profile, routes) -> list[tuple[str, object, list]]:
    from ..material_family import distinct_members
    if document.material_family is None:
        return [("", document, [*routes, *_surface_route_table(document, profile)])]
    return [(f"LOD {', '.join(map(str, lods))} · ", member,
             [*_bundle(member, profile, recover_programs=False)[1], *_surface_route_table(member, profile)])
            for lods, member in distinct_members(document.material_family)]


def _surface_outputs(document, profile, routes) -> tuple[list, list, list, str]:
    from ..surface_meaning import catalog_fingerprint, surface_output_catalog_for
    from ..surface_outputs import (
        native_program_surface, output_pin_records, prefetch_native_program_surfaces, reach_records,
    )
    catalog = surface_output_catalog_for(document.profile)
    entries, errors = [], []
    sources = _surface_sources(document, profile, routes)
    prefetch_native_program_surfaces(profile, [(member, int(route["target"]), int(route["program"]), str(route["role"]))
                                               for _prefix, member, member_routes in sources for route in member_routes])
    for prefix, member, member_routes in sources:
        for route in member_routes:
            target, index, role = int(route["target"]), int(route["program"]), str(route["role"])
            program = member.sdf.targets[target].programs[index]
            name = prefix + program.name
            pixel = program.shader("pixel")
            shader = pixel_shader_digest(pixel) if pixel is not None and pixel.present else ""
            try:
                entries.append((name, pass_purpose(member, profile, target, index), shader,
                                *native_program_surface(member, profile, target, index, role)))
            except ShaderGraphError as exc:
                errors.append({"pass": name, "role": role, "target": target, "reason": str(exc)})
    return (output_pin_records(((name, purpose, shader, channels) for name, purpose, shader, channels, _reach
                                in entries), catalog),
            errors, reach_records([(name, channels, reach) for name, _purpose, _shader, channels, reach in entries],
                                  document.material_interface.parameters), catalog_fingerprint(catalog))


def _connect_surface_outputs(graph, master, surface, records, errors, reach, meaning) -> bool:
    from ..surface_outputs import SURFACE_OUTPUTS_VERSION
    names = {str(record["pin"]) for record in records}
    stale = [link for link in graph.links
             if (link.source_node == master.node_id and link.source_pin.startswith("channel_")
                 and link.source_pin not in names)
             or (link.target_node == surface.node_id and link.target_pin.startswith("channel_")
                 and link.target_pin not in names)]
    for link in stale:
        graph.links.remove(link)
    wired = {link.target_pin for link in graph.links if link.target_node == surface.node_id}
    master.properties.update(output_pins=records, output_errors=errors, reach=reach,
                             surface_outputs_version=SURFACE_OUTPUTS_VERSION, surface_meaning=meaning)
    surface.properties["channel_pins"] = [{key: value for key, value in record.items() if key != "passes"}
                                          for record in records]
    for record in records:
        if record["form"] == "value" and record["pin"] not in wired:
            graph.connect(master.node_id, str(record["pin"]), surface.node_id, str(record["pin"]))


def _surface(project):
    return next((node for node in project.graph.nodes
                 if node.kind == "material_surface" and node.properties.get("accepts_recovered_material")), None)


def _edited_channels(project) -> list[tuple[dict, object]]:
    master = _master(project)
    surface = _surface(project)
    if surface is None:
        return []
    incoming = {(link.target_node, link.target_pin): link for link in project.graph.links}
    edited = []
    for record in surface.properties.get("channel_pins", ()):
        link = incoming.get((surface.node_id, record["pin"]))
        if link is not None and not (link.source_node == master.node_id and link.source_pin == record["pin"]):
            edited.append((record, link))
    return edited


def _reads_vertex_data(graph) -> bool:
    return graph_reads_surface_data(graph) or any(node.kind == PER_VERTEX_KIND for node in graph.nodes)


def _vertex_values(project, document, profile, target_index: int, program_index: int) -> tuple[tuple, tuple]:
    from ..surface_outputs import channel_for_pin, native_program_channels
    graph = project.graph
    if not _reads_vertex_data(graph):
        return (), ()
    program = document.sdf.targets[target_index].programs[program_index]
    pixel = program.shader("pixel")
    if pixel is None or not pixel.present:
        return (), ()
    edited = _edited_channels(project)
    if edited:
        try:
            channels = native_program_channels(document, profile, target_index, program_index, "pixel")
        except ShaderGraphError:
            channels = ()  # The master reports it among its unreadable outputs; it has no pin to edit.
        edited = [link for record, link in edited if channel_for_pin(channels, record) is not None]
    return vertex_value_plan(document, profile, graph, target_index, program_index,
                             [link.source_node for link in edited])


def _vertex_value_routes(project, document, profile, routes) -> tuple[tuple[int, int, str], ...]:
    if not _reads_vertex_data(project.graph):
        return ()
    result = []
    for target, program in dict.fromkeys((target, program) for target, program, role in routes if role == "pixel"):
        if (target, program, "vertex") not in routes and _vertex_values(project, document, profile, target, program)[0]:
            result.append((target, program, "vertex"))
    return tuple(result)


def _edited_surface_routes(project, document, profile) -> tuple[tuple[int, int, str], ...]:
    from ..surface_outputs import channel_pin_name, native_program_channels
    edited = {record["pin"] for record, _link in _edited_channels(project)}
    result = []
    for route in _surface_route_table(document, profile) if edited else ():
        key = (int(route["target"]), int(route["program"]), str(route["role"]))
        try:
            channels = native_program_channels(document, profile, *key)
        except ShaderGraphError:
            continue  # The master reports it among its unreadable outputs; it has no pin to edit.
        if any(channel_pin_name(split.channel, form) in edited for split in channels for form in ("value", "stored")):
            result.append(key)
    return tuple(result)


def _upgrade_surface_outputs(document, project, profile) -> bool:
    from ..surface_meaning import catalog_fingerprint, surface_output_catalog_for
    from ..surface_outputs import SURFACE_OUTPUTS_VERSION
    master = _master(project)
    if (master.properties.get("surface_outputs_version") == SURFACE_OUTPUTS_VERSION
            and master.properties.get("surface_meaning") == catalog_fingerprint(surface_output_catalog_for(
                document.profile))):
        return False
    surface = _surface(project)
    if surface is None:
        raise ShaderGraphError("Recovered material has no surface to receive its outputs.")
    _connect_surface_outputs(project.graph, master, surface, *_surface_outputs(
        document, profile, master.properties.get("program_routes", [])))
    return True


def _seed(document, project, profile, *, defer_shader_recovery: bool = False) -> None:
    graph = ShaderGraph("material", "")
    surface = graph.add_node(
        "material_surface",
        x=1260.0,
        y=180.0,
        properties={"accepts_recovered_material": True},
    )
    pins: list[dict[str, object]] = []
    sources: list[tuple[ShaderGraphNode, str, str]] = []
    rows = 12
    for index, definition in enumerate(project.parameters):
        column, row = divmod(index, rows)
        node = graph.add_node(
            "material_parameter",
            x=-880.0 - column * 320.0,
            y=40.0 + row * 118.0,
            properties={
                "parameter_id": definition.parameter_id,
                "name": definition.name,
                "display_name": definition.name.replace("_", " "),
                "value_type": definition.value_type,
            },
        )
        pin = f"parameter_{definition.parameter_id}"
        pins.append(
            {
                "pin": pin,
                "label": definition.name.replace("_", " "),
                "value_type": definition.value_type,
                "input_kind": "parameter",
                "source_id": definition.parameter_id,
                "source_name": definition.source_name or definition.name,
                "source_byte_offset": int(
                    definition.source_byte_offset
                    if definition.source_byte_offset is not None
                    else -1
                ),
            }
        )
        sources.append((node, "value", pin))
    texture_column = (len(project.parameters) + rows - 1) // rows
    texture_rows = 6
    for index, definition in enumerate(project.textures):
        column, row = divmod(index, texture_rows)
        node, pin = _texture_input(
            graph, definition,
            x=-880.0 - (texture_column + column) * 320.0,
            y=40.0 + row * 260.0,
        )
        pins.append(pin)
        sources.append((node, "texture_object", pin["pin"]))
    if not project.parameters:
        for index, pin in enumerate(renderer_input_pins(document, profile)):
            column, row = divmod(index, rows)
            column += texture_column + (len(project.textures) + texture_rows - 1) // texture_rows
            node = graph.add_node(RENDERER_VALUE_KIND, x=-880.0 - column * 320.0,
                                  y=40.0 + row * 118.0,
                                  properties={**pin, "display_name": pin["label"]})
            pins.append(pin)
            sources.append((node, "value", pin["pin"]))
    semantic_programs, routes = _bundle(
        document, profile, recover_programs=not defer_shader_recovery,
    )
    source_name = Path(str(getattr(document, "source_label", ""))).name
    display = source_name.split(".mmtr", 1)[0].replace("_", " ").strip()
    master = graph.add_node(
        RE_ENGINE_RECOVERED_MASTER_KIND,
        x=720.0,
        y=120.0,
        properties={
            "display_name": (display.title() + " Master Material") if display else "Recovered Master Material",
            "input_pins": pins,
            "semantic_programs": [] if defer_shader_recovery else semantic_programs,
            "program_routes": routes,
            "semantic_bundle_version": 0 if defer_shader_recovery else SEMANTIC_BUNDLE_VERSION,
            "semantic_reconstruction_boundary": True,
            "parameter_storage": "renderer-constant-buffers" if renderer_only(document, profile) else document.material_interface.parameter_storage,
            "parameter_buffer_sizes": list(document.material_interface.user_buffer_sizes),
            "descriptor_layouts": [item.to_dict() for item in document.material_interface.descriptor_layouts],
            "shader_lod": document.material_interface.shader_lod,
        },
    )
    for node, source_pin, target_pin in sources:
        graph.connect(node.node_id, source_pin, master.node_id, target_pin)
    graph.connect(master.node_id, "material", surface.node_id, "recovered_material")
    _connect_surface_outputs(graph, master, surface, *_surface_outputs(document, profile, routes))
    digest = hashlib.sha256(
        "\n".join(sorted(str(item["source_sha256"]) for item in semantic_programs)).encode("ascii")
    ).hexdigest()
    project.name = display.title() + " Material" if display else "Recovered Material"
    project.source_material_model = _MODEL_PREFIX + profile.profile_id.casefold() + "." + digest[:24]
    project.graph = graph
    project._prepare_semantics = (
        lambda destination: _recover_project_programs(document, destination, profile)
    ) if defer_shader_recovery else None


def _dxil_recipe_runtime():
    record = _dxil_recovery.DxilSemanticRecord
    values = (
        recover_re_engine_dxil_ir, _dxil_recovery.recover_re_engine_dxil_ir,
        _dxil_recovery.parse_re_engine_dxil_assembly,
        record, record.__new__, record.__init__, record.__post_init__, record.to_dict, record.from_dict,
        ReEngineDxilSemanticIr, ReEngineDxilSemanticIr.__post_init__,
        ReEngineDxilSemanticIr.to_json, ReEngineDxilSemanticIr.to_storage,
        ReEngineDxilSemanticIr.to_assembly, ReEngineDxilSemanticIr.from_json,
        ReEngineDxilSemanticIr.from_storage,
        _dxil_recovery._validate_record, _dxil_recovery._validate_module_structure,
        _dxil_recovery._opcode, _dxil_recovery._module_record,
        _dxil_recovery._strip_comment, _dxil_recovery._bracket_delta,
        storage_codec.pack_ir_storage, storage_codec.unpack_ir_storage, _dxil_recovery._unpack_storage,
        _dxil_recovery.json.dumps, _dxil_recovery.json.loads,
        storage_codec.zlib.compress, storage_codec.zlib.decompressobj,
        storage_codec.base64.urlsafe_b64encode, storage_codec.base64.b64decode,
        _dxil_recovery._EXECUTABLE_OPCODES, _dxil_recovery._CALL_PREFIXES,
        _dxil_recovery._ARITHMETIC_FLAGS, _dxil_recovery._ASSIGNMENT,
        _dxil_recovery._DEFINE, _dxil_recovery._LABEL, _dxil_recovery._UNNAMED_LABEL,
        _dxil_recovery._METADATA, _dxil_recovery._TYPE,
        _dxil_recovery._GLOBAL, _dxil_recovery._DECLARE, _dxil_recovery._ATTRIBUTE,
    )
    return tuple(getattr(value, "__func__", value) for value in values)


_DXIL_RECIPE_PUBLICATION_RUNTIME = _dxil_recipe_runtime()


def _dxil_recipe_context():
    return _dxil_recipe_runtime(), (
        _dxil_recovery.IR_SCHEMA, _dxil_recovery.IR_VERSION,
        storage_codec.IR_STORAGE_PREFIX, _dxil_recovery.MAX_IR_JSON_BYTES,
        _dxil_recovery.MAX_IR_STORAGE_BYTES, _dxil_recovery.MAX_IR_LINES,
    )


def _dxil_program_key(storage, context=None):
    runtime, rules = _dxil_recipe_context() if context is None else context
    return native_build_key(runtime, storage, *rules)


def _recover_dxil_recipe(bytecode):
    context = _dxil_recipe_context()
    ir = recover_re_engine_dxil_ir(bytecode)
    schema, version, prefix, *limits = context[1]
    canonical = (context[0] == _DXIL_RECIPE_PUBLICATION_RUNTIME
        and type(schema) is str and type(version) is int and type(prefix) is str
        and all(type(limit) is int for limit in limits)
        and type(ir) is ReEngineDxilSemanticIr and type(ir.records) is tuple
        and all(type(record) is _dxil_recovery.DxilSemanticRecord
                and all(type(value) is str for value in
                        (record.kind, record.text, record.opcode, record.result))
                for record in ir.records))
    if canonical and (not ir.records or len(ir.records) > _dxil_recovery.MAX_IR_LINES):
        raise ShaderGraphError("Recovered DXIL semantics are invalid or too large.")
    storage = ir.to_storage()
    if not isinstance(storage, str) or len(storage) > _dxil_recovery.MAX_IR_STORAGE_BYTES:
        raise ShaderGraphError("Recovered DXIL semantics are too large.")
    if canonical and context == _dxil_recipe_context():
        key = _dxil_program_key(storage, context)
        if _DXIL_PROGRAM_CACHE.get(key) is None:
            _DXIL_PROGRAM_CACHE.put(key, _ValidatedDxilAssembly(ir.to_assembly()))
    else:
        _program_ir("dxil", storage)
    return storage


def _program_ir(kind, storage):
    if kind == "dxil":
        context = _dxil_recipe_context()
        decoder = ReEngineDxilSemanticIr.from_storage
        key = _dxil_program_key(storage, context)
        cached = _DXIL_PROGRAM_CACHE.get(key)
        if cached is not None:
            return cached
        result = _ValidatedDxilAssembly(decoder(storage).to_assembly())
        if context == _dxil_recipe_context():
            _DXIL_PROGRAM_CACHE.put(key, result)
        return result
    return _other_program_ir(kind, storage)


@lru_cache(maxsize=8)
def _other_program_ir(kind, storage):
    if kind == "spirv":
        return SpirvSemanticIr.from_storage(storage)
    if kind == "dxbc-sm5":
        return DxbcSemanticIr.from_storage(storage)
    raise ShaderGraphError("Recovered material semantic program kind is invalid.")


def _program_reference(project, semantics, document=None):
    from ..material_region_read import region_derived_cache

    master = _master(project)
    programs = master.properties.get("semantic_programs", [])
    routes = master.properties.get("program_routes", [])
    if not isinstance(programs, list) or not isinstance(routes, list):
        raise ShaderGraphError("Recovered material program bundle is missing.")
    key = (int(semantics.target_index), int(semantics.program_index), semantics.shader_role)
    cache = region_derived_cache("re_engine_program_references")
    snapshot = cache.get(id(master)) if cache is not None else None
    if snapshot is None:
        route_index, program_index = {}, {}
        for item in routes:
            if isinstance(item, dict):
                route_key = (int(item.get("target", -1)), int(item.get("program", -1)), item.get("role", "pixel"))
                route_index.setdefault(route_key, item)
        for item in programs:
            if isinstance(item, dict):
                program_index.setdefault(str(item.get("source_sha256", "")), item)
        snapshot = master, route_index, program_index
        if cache is not None:
            cache[id(master)] = snapshot
    route = snapshot[1].get(key)
    if route is None:
        # A shader that reads no material data has no stored program; a build recovers it from the native bytes.
        if document is None:
            raise ShaderGraphError("Recovered material route is missing.")
        route = _route_record(document, *key)
        return str(route["source_sha256"]), None, str(route["binary_format"])
    digest = str(route.get("source_sha256", ""))
    item = snapshot[2].get(digest)
    if item is None:
        raise ShaderGraphError("Recovered material semantic program is missing.")
    return digest, item, str(route.get("binary_format", "dxbc-container"))


def _program(project, semantics, document, profile) -> _RecoveredProgram:
    digest, item, binary_format = _program_reference(project, semantics, document)
    if item is None:
        item = _recover_shader(document, profile, int(semantics.target_index), int(semantics.program_index),
                               semantics.shader_role, {})
    source_contract = contract_from_storage(item.get("semantic_contract"), digest)
    kind = str(item.get("ir_kind", ""))
    ir = _program_ir(kind, item.get("semantic_ir"))
    return _RecoveredProgram(
        kind,
        ir,
        source_contract,
        digest,
        binary_format,
    )


def _program_key(digest: str, binary_format: str, semantics: MmtrProgramSemantics) -> str:
    # Equal shaders of passes that draw for different purposes or are handed different data build differently.
    key = digest + ":" + binary_format + repr((semantics.purpose, semantics.surface_data, semantics.vertex_values))
    if binary_format == "spirv-module":
        descriptor = semantics.descriptor
        # Normalised: the build and the shader key reach these values in different types.
        key += repr((str(descriptor.family), bool(descriptor.alpha_test), bool(descriptor.two_sided),
                     int(semantics.target_index)))
    return key


def _identity_source(project, value, source, source_pin: str, contract, profile) -> bool:
    if value.get("input_kind") == "renderer_value":
        return (source.kind == RENDERER_VALUE_KIND and source_pin == "value"
                and source.properties.get("source_register_space", 0) == value.get("source_register_space", 0) and all(
            source.properties.get(key) == value.get(key)
            for key in ("source_buffer", "source_name", "source_byte_offset", "value_type", "value_layout")
        ))
    if str(value.get("input_kind", "")) == "parameter":
        same_source = (
            source.kind == "material_parameter"
            and source_pin == "value"
            and str(source.properties.get("parameter_id", "")) == str(value.get("source_id", ""))
        )
        if not same_source:
            return False
        definition = project.parameter(str(value.get("source_id", "")))
        if definition is None or definition.value_type != value.get("value_type"):
            return False
        buffer = contract.buffer(profile.user_buffer_name)
        if buffer is None:
            return definition.source_byte_offset == value.get("source_byte_offset")
        name = profile.shader_parameter_name(profile.material_parameter_name(definition, contract, project.source_profile))
        variable = next((item for item in buffer.variables if item.name == name), None)
        return variable is not None and (
            variable.byte_offset == value.get("source_byte_offset")
            and variable.byte_size == value_width(definition.value_type) * 4
        )
    definition = project.texture(str(value.get("source_id", "")))
    return (
        source.kind == "material_texture"
        and source_pin == "texture_object"
        and str(source.properties.get("texture_id", "")) == str(value.get("source_id", ""))
        and definition is not None
        and definition.name == value.get("source_name")
        and (definition.dimension or "texture") == value.get("value_type")
    )


def _dxbc_reads(ir: DxbcSemanticIr, buffer) -> set[int] | None:
    reads: set[int] = set()
    pattern = re.compile(
        rf"cb{int(buffer.bind_point)}\[(?P<register>[^\]]+)\](?:\.(?P<swizzle>[xyzw]{{1,4}}))?",
        re.IGNORECASE,
    )
    for instruction in ir.instructions:
        for operand in instruction.operands:
            for match in pattern.finditer(operand):
                if not match.group("register").strip().isdigit():
                    return None
                register = int(match.group("register"))
                reads.update(
                    register * 16 + "xyzw".index(component) * 4
                    for component in match.group("swizzle") or "xyzw"
                )
    return reads


def _dxbc_structured_reads(ir: DxbcSemanticIr, bind_point: int) -> set[int] | None:
    reads: set[int] = set()
    for instruction in ir.instructions:
        if instruction.opcode != "ld_structured_indexable" or len(instruction.operands) < 4:
            continue
        resource = re.fullmatch(r"t(\d+)(?:\.[xyzw]{1,4})?", instruction.operands[3].strip(), re.IGNORECASE)
        if resource is None or int(resource.group(1)) != bind_point:
            continue
        offset = re.fullmatch(r"l\(\s*(0x[0-9a-f]+|\d+)\s*\)", instruction.operands[2].strip(), re.IGNORECASE)
        destination = re.fullmatch(r"r\d+(?:\.([xyzw]{1,4}))?", instruction.operands[0].strip(), re.IGNORECASE)
        if offset is None or destination is None:
            return None
        base = int(offset.group(1), 0)
        reads.update(base + component * 4 for component in range(len(destination.group(1) or "xyzw")))
    return reads


def _buffer_reads(recovered, buffer):
    """Byte offsets of the words read; None when unknown."""
    if recovered.ir_kind == "spirv":
        return spirv_uniform_reads(recovered.ir, buffer.name)
    if recovered.ir_kind == "dxbc-sm5":
        return _dxbc_reads(recovered.ir, buffer)
    return _dxil_reads(recovered.ir, recovered.contract, buffer.bind_point, buffer.register_space)


def _dxil_reads(ir: ReEngineDxilSemanticIr | _ValidatedDxilAssembly, contract, bind_point: int, register_space: int = 0) -> set[int] | None:
    from .re_engine_dxil_authoring import _constant_extracts
    from .constant_words import word_parts

    assembly = ir.to_assembly()
    key = native_build_key((_constant_extracts,), "constant-words", assembly, contract)
    cached = _DXIL_INPUT_ANALYSIS_CACHE.get(key)
    if cached is None:
        dynamic_bindings = set()
        extracts = _constant_extracts(assembly.splitlines(), contract, dynamic_bindings=dynamic_bindings)
        cached = tuple(sorted(dynamic_bindings)), tuple(extracts)
        _DXIL_INPUT_ANALYSIS_CACHE.put(key, cached)
    dynamic_bindings, extracts = cached
    if (register_space, int(bind_point)) in dynamic_bindings:
        return None
    return {
        offset
        for key in extracts
        for binding, offset, space in (word_parts(key),)
        if binding == bind_point and space == register_space
    }


def _dxil_material_accesses(assembly, contract):
    key = native_build_key((material_record_accesses,), "material-words", assembly, contract)
    cached = _DXIL_INPUT_ANALYSIS_CACHE.get(key)
    if cached is not None:
        return cached
    result = material_record_accesses(assembly.splitlines(), contract)
    _DXIL_INPUT_ANALYSIS_CACHE.put(key, result)
    return result


def _dxil_texture_accesses(assembly, contract, accesses):
    key = native_build_key((material_texture_accesses,), "material-textures", assembly, contract, accesses)
    cached = _DXIL_INPUT_ANALYSIS_CACHE.get(key)
    if cached is not None:
        return cached
    result = material_texture_accesses(assembly.splitlines(), contract, accesses)
    _DXIL_INPUT_ANALYSIS_CACHE.put(key, result)
    return result


def _renderer_word_bits(expression, value_type):
    if value_type.startswith(("int", "uint")):
        return f"asfloat({expression})"
    if value_type.startswith("bool"):
        return f"asfloat(uint({expression}))"
    return expression


def _bindless_texture_resolver(profile, master, contract, recovered, descriptor_layout, source_descriptor_layout,
                               helper_sources: dict):
    """The resolver adds its helper to ``helper_sources``."""
    from file_handlers.mmtr.material_project import _TEXTURE_CONTRACT_DIMENSIONS
    native: dict[str, object] = {}

    def texture_object(definition):
        if descriptor_layout is None:
            raise ShaderGraphError("Bindless texture sampling needs an exact MDF descriptor layout.")
        words = descriptor_layout.word_offsets(master.properties.get("shader_lod"))
        offset = words.get(("texture", definition.name))
        if offset is None or definition.dimension not in _TEXTURE_CONTRACT_DIMENSIONS:
            raise ShaderGraphError(f"{definition.name} has no typed descriptor in this material's active layout.")
        offset += profile.material_record_prefix_size
        object_type = texture_hlsl_type(_TEXTURE_CONTRACT_DIMENSIONS[definition.dimension])
        name = f"REasyMaterialTexture_o{offset}"
        if "uses" not in native:
            assembly = recovered.ir.to_assembly()
            native["uses"] = _dxil_texture_accesses(assembly, recovered.contract,
                                                    _dxil_material_accesses(assembly, recovered.contract))
        source_offset = (source_descriptor_layout.word_offsets(master.properties.get("shader_lod")).get(
            ("texture", definition.source_name or definition.name)) if source_descriptor_layout is not None else None)
        uses = [use for use in native["uses"]
                if use.dimension == _TEXTURE_CONTRACT_DIMENSIONS[definition.dimension]]
        exact = [use for use in uses if source_offset is not None
                 and use.byte_offset == source_offset + profile.material_record_prefix_size]
        bindings = {use.resource for use in (exact or uses)}
        if len(bindings) != 1:
            raise ShaderGraphError(f"{definition.name} has no unambiguous native material descriptor binding.")
        binding = next(iter(bindings))
        if binding is None:
            if (contract.model_major, contract.model_minor) < (6, 6):
                raise ShaderGraphError("Native descriptor-heap sampling needs Shader Model 6.6.")
            texture = "ResourceDescriptorHeap[NonUniformResourceIndex(descriptor)]"
        else:
            if binding.bind_count == 1 or binding.return_type not in {"float", "unorm", "snorm"}:
                raise ShaderGraphError(f"{definition.name} needs a float texture descriptor array.")
            texture = f"{resource_identifier(binding)}[NonUniformResourceIndex(descriptor)]"
        helper_sources.setdefault(name, (
            f"{object_type} {name}(uint address)\n{{\n"
            f"    uint descriptor = asuint(REasyMaterialLoad(address, {offset}u));\n"
            f"    {object_type} texture = {texture};\n"
            "    return texture;\n}"
        ))
        return f"{name}(REasyMaterialAddress)"

    return texture_object


def _overrides(profile, project, contract, semantics, recovered: _RecoveredProgram,
               descriptor_layout=None, source_descriptor_layout=None):
    from .constant_words import constant_word
    from .renderer_inputs import renderer_fields, renderer_field_expression
    from file_handlers.sdf.aggregate_values import aggregate_projection
    master = _master(project)
    values = master.properties.get("input_pins", [])
    if not isinstance(values, list):
        raise ShaderGraphError("Recovered material inputs are invalid.")
    incoming = {
        (link.target_node, link.target_pin): link for link in project.graph.links
    }
    pending = []
    for value in values:
        if not isinstance(value, dict):
            raise ShaderGraphError("Recovered material input is invalid.")
        pin = str(value.get("pin", ""))
        link = incoming.get((master.node_id, pin))
        if link is None:
            raise ShaderGraphError(f"Recovered material input {value.get('label', pin)} is unconnected.")
        source = project.graph.node(link.source_node)
        if source is None:
            raise ShaderGraphError("Recovered material connection is dangling.")
        if not _identity_source(project, value, source, link.source_pin, contract, profile):
            pending.append((value, link, source))
    parameter_edits = any(value.get("input_kind") == "parameter" for value, _link, _source in pending)
    buffer = contract.buffer(profile.user_buffer_name)
    instances = (
        contract.resource(profile.instanced_material_buffer)
        if buffer is None and profile.instanced_material_buffer and recovered.ir_kind == "dxbc-sm5" else None
    )
    dynamic = (
        master.properties.get("parameter_storage") in BINDLESS_PARAMETER_STORAGES
        and buffer is None
    )
    native_assembly = recovered.ir.to_assembly() if dynamic and parameter_edits and recovered.ir_kind == "dxil" else ""
    material_accesses = _dxil_material_accesses(native_assembly, recovered.contract) if native_assembly else ()
    material_reads = {word.byte_offset for access in material_accesses for word in access.words}
    reads = None
    if instances is not None and parameter_edits:
        reads = _dxbc_structured_reads(recovered.ir, instances.bind_point)
    elif buffer is not None and parameter_edits:
        reads = _buffer_reads(recovered, buffer)
    expressions: dict[ConstantWord, str] = {}
    structured: dict[tuple[int, int], str] = {}
    material_expressions: dict[int, str] = {}
    textures: dict[tuple[int, int], tuple[int, int]] = {}
    helper_sources: dict[str, str] = {}
    texture_object = _bindless_texture_resolver(profile, master, contract, recovered, descriptor_layout,
                                                source_descriptor_layout, helper_sources)

    for value, link, source in pending:
        pin = str(value.get("pin", ""))
        kind = str(value.get("input_kind", ""))
        if kind in {"parameter", "renderer_value"}:
            input_buffer = renderer_buffer(contract, value) if kind == "renderer_value" else buffer
            input_dynamic = dynamic and kind == "parameter"
            input_reads = reads
            if kind == "renderer_value" and input_buffer is not None:
                input_reads = _buffer_reads(recovered, input_buffer)
            input_instances = instances if kind == "parameter" else None
            if input_buffer is None and not input_dynamic and input_instances is None:
                continue
            if kind == "renderer_value" and "value_layout" in value:
                for index, field in enumerate(renderer_fields(value)):
                    if input_reads is not None and not set(field.offsets) & input_reads:
                        continue
                    producer, socket, projection = aggregate_projection(project.graph, source, link.source_pin, index, incoming)
                    if producer.kind == RENDERER_VALUE_KIND and projection is not None:
                        original = renderer_fields(producer.properties)[projection]
                        if (renderer_buffer(contract, producer.properties) == input_buffer
                                and original.offsets == field.offsets and original.value_type == field.value_type):
                            continue
                        expression = renderer_field_expression(contract, producer.properties, original)
                        actual_type = original.value_type
                    else:
                        result = recovered_graph_expression(profile, project, contract, semantics, producer, socket,
                                                             incoming, set(), helper_sources)
                        expression, actual_type = result.value, result.value_type
                        if projection is not None:
                            if actual_type != value["value_type"]:
                                raise ShaderGraphError("Renderer aggregate connection changes its logical type")
                            expression, actual_type = f"({expression}).field{projection}", field.value_type
                    if actual_type != field.value_type:
                        raise ShaderGraphError(f"Renderer field {field.path} expects {field.value_type}, not {actual_type}")
                    for component, offset in enumerate(field.offsets):
                        if input_reads is not None and offset not in input_reads:
                            continue
                        scalar = expression if len(field.offsets) == 1 else f"({expression}).{'xyzw'[component]}"
                        expressions[constant_word(input_buffer.bind_point, offset, input_buffer.register_space)] = _renderer_word_bits(scalar, field.value_type)
                continue
            byte_offset = int(value.get("source_byte_offset", -1))
            if input_dynamic:
                byte_offset += profile.material_record_prefix_size
            if input_instances is not None:
                # The instance element packs members tightly, so address them by name.
                element = recovered.contract.structured_buffer(input_instances.name)
                member = next((item for item in (element.variables if element else ())
                               if profile.logical_parameter_name(item.name) == value.get("source_name")), None)
                if member is None:
                    continue
                if member.byte_size != value_width(str(value.get("value_type", "float"))) * 4:
                    raise ShaderGraphError(f"{value.get('label', pin)} has a different type in the instance buffer.")
                byte_offset = member.byte_offset
            value_type = str(value.get("value_type", "float"))
            if kind == "renderer_value":
                layout = renderer_fields(value)
                words = [(offset, field.value_type,
                          (f".field{index}" if "value_layout" in value else "")
                          + (f".{'xyzw'[component]}" if len(field.offsets) > 1 else ""))
                         for index, field in enumerate(layout) for component, offset in enumerate(field.offsets)]
            else:
                words = [(byte_offset + component * 4, value_type,
                          f".{'xyzw'[component]}" if value_width(value_type) > 1 else "")
                         for component in range(value_width(value_type))]
            active = {offset for offset, _type, _path in words
                      if (offset in material_reads if input_dynamic else input_reads is None or offset in input_reads)}
            if not active:
                continue
            result = recovered_graph_expression(
                profile,
                project,
                contract,
                semantics,
                source,
                link.source_pin,
                incoming,
                set(),
                helper_sources,
                texture_resolver=texture_object if input_dynamic else None,
            )
            expression = result.value
            if result.value_type != value_type:
                if result.value_type == "float" and value_type.startswith("float"):
                    expression = splat_expression(value_type, expression)
                else:
                    raise ShaderGraphError(
                        f"{value.get('label', pin)} expects {value_type}, not {result.value_type}."
                    )
            for offset, field_type, path in words:
                if offset not in active:
                    continue
                scalar = f"({expression}){path}" if path else expression
                scalar = _renderer_word_bits(scalar, field_type)
                if input_dynamic:
                    material_expressions[offset] = scalar
                elif input_instances is not None:
                    structured[(int(input_instances.bind_point), offset)] = scalar
                else:
                    expressions[constant_word(int(input_buffer.bind_point), offset, input_buffer.register_space)] = scalar
        elif kind == "texture":
            original = recovered.contract.resource(str(value.get("source_name", "")))
            if original is None or not original.sampleable:
                continue
            if source.kind != "material_texture" or link.source_pin != "texture_object":
                raise ShaderGraphError(f"{value.get('label', pin)} needs a texture asset.")
            definition = project.texture(str(source.properties.get("texture_id", "")))
            target_name = profile.material_texture_resource(definition, contract) if definition else ""
            target = contract.resource(target_name)
            if target is None or not target.sampleable:
                raise ShaderGraphError(f"{value.get('label', pin)} has no native texture binding.")
            if (
                original.input_type,
                original.return_type,
                original.dimension,
                original.sample_count,
            ) != (
                target.input_type,
                target.return_type,
                target.dimension,
                target.sample_count,
            ):
                raise ShaderGraphError("Texture remapping cannot change a native resource type.")
            textures[(int(original.register_space), int(original.bind_point))] = (
                int(target.register_space),
                int(target.bind_point),
            )
    return expressions, structured, textures, "\n\n".join(helper_sources.values()), material_expressions


def _coerced(result, value_type: str, label: str) -> str:
    if result.value_type == value_type:
        return result.value
    if result.value_type == "float" and value_type.startswith("float"):
        return splat_expression(value_type, result.value)
    if result.value_type in {"float", "int", "uint"} and value_type in {"float", "int", "uint"}:
        return f"{value_type}({result.value})"
    raise ShaderGraphError(f"{label} expects {value_type}, not {result.value_type}.")


def _added_values(profile, project, contract, semantics, helpers: dict, texture_resolver=None):
    from ..surface_data import surface_data_value
    from ..surface_flow import ChannelSplit, SurfaceChannel
    from ..surface_outputs import SurfaceEdit
    from ..vertex_values import VALUE_SEMANTIC
    incoming = {(link.target_node, link.target_pin): link for link in project.graph.links}
    edits = []
    for value in semantics.vertex_values:
        kind, identity = value.source.split(":", 1)
        if kind == "datum":
            expression = surface_data_value(semantics, contract, {"datum": identity, "value_type": value.value_type})
        else:
            link = incoming.get((identity, "value"))
            source = project.graph.node(link.source_node) if link is not None else None
            if source is None:
                raise ShaderGraphError("Computed per Vertex needs a Value.")
            expression = _coerced(recovered_graph_expression(
                profile, project, contract, semantics, source, link.source_pin, incoming, set(), helpers,
                texture_resolver=texture_resolver), value.value_type, "Computed per Vertex")
        stored = f"{value.scalar}4"
        channel = SurfaceChannel("vertex", "added", VALUE_SEMANTIC.casefold(), value.index, (0, 1, 2, 3), stored, stored)
        edits.append(SurfaceEdit(ChannelSplit(channel, ()), "stored", value.stored_expression(expression), stored,
                                 material=re.search(r"\bREasyMaterialAddress\b", expression) is not None))
    return edits


def _surface_edits(profile, project, contract, semantics, recovered: _RecoveredProgram, helpers: dict,
                   texture_resolver=None):
    from ..surface_flow import recovered_surface_flow
    from ..surface_outputs import SurfaceEdit, SurfaceTap, channel_for_pin, recovered_channels
    master = _master(project)
    edited = _edited_channels(project)
    added = (_added_values(profile, project, contract, semantics, helpers, texture_resolver)
             if semantics.shader_role == "vertex" else [])
    if not (edited or added):
        return (), None
    incoming = {(link.target_node, link.target_pin): link for link in project.graph.links}
    flow = recovered_surface_flow(recovered.ir_kind, recovered.ir, recovered.contract)
    channels = recovered_channels(recovered.ir_kind, recovered.ir, recovered.contract, flow)
    if recovered.ir_kind == "dxil":
        from ..surface_outputs import material_texture_names, named_texture_reads
        layouts = tuple(MaterialDescriptorLayout.from_dict(item) for item in master.properties.get("descriptor_layouts", ()))
        channels = named_texture_reads(channels, material_texture_names(
            layouts, master.properties.get("shader_lod"), profile.material_record_prefix_size))
    outputs = {record["pin"]: (index, record) for index, record in enumerate(master.properties.get("output_pins", ()))}
    # Only a bindless DXIL pass hands the drawn material's record address to the code that stores a channel.
    material_readable = (recovered.ir_kind == "dxil" and contract.buffer(profile.user_buffer_name) is None
                         and master.properties.get("parameter_storage") in BINDLESS_PARAMETER_STORAGES)
    if any(edit.material for edit in added) and not material_readable:
        raise ShaderGraphError("Computed per Vertex uses material values this pass's vertex shader cannot read. "
                               "Use Drawing For to leave this pass out of the edit.")
    edits, forms = list(added), {}
    for record, link in edited:
        split = channel_for_pin(channels, record)
        if split is None:
            continue
        form = "value" if split.channel.packed and record["form"] == "value" else "stored"
        if forms.setdefault(split.channel.key, form) != form:
            raise ShaderGraphError(f"{record['label']} is edited both before packing and as stored; edit one of them.")
        taps: dict[str, SurfaceTap] = {}

        def resolve(pin):
            index, tap_record = outputs[pin]
            tap_split = channel_for_pin(channels, tap_record)
            if tap_split is None:
                raise ShaderGraphError(f"{tap_record['label']} is not produced in every pass that produces "
                                       f"{record['label']}. Use Drawing For to leave this pass out of the edit.")
            if tap_split.channel.scope == "discard" and split.channel.scope != "discard":
                raise ShaderGraphError(f"{record['label']} reads Discard; only the Discard input can.")
            tap_form = "value" if tap_split.channel.packed and tap_record["form"] == "value" else "stored"
            return taps.setdefault(pin, SurfaceTap(f"REasySurfaceTap_{index}", tap_split, tap_form,
                                                   str(tap_record["value_type"]))).name

        source = project.graph.node(link.source_node)
        if source is None:
            raise ShaderGraphError("Recovered material connection is dangling.")
        value_type = str(record["value_type"])
        expression = _coerced(recovered_graph_expression(
            profile, project, contract, semantics, source, link.source_pin, incoming, set(), helpers,
            texture_resolver=texture_resolver, channel_resolver=resolve), value_type, record["label"])
        own = taps.get(record["pin"])
        if own is not None and expression == own.name and own.split == split and own.form == form:
            continue
        material = re.search(r"\bREasyMaterialAddress\b", expression) is not None
        if material and not material_readable:
            raise ShaderGraphError(f"{record['label']} uses material values this pass cannot read. "
                                   "Use Drawing For to leave this pass out of the edit.")
        edits.append(SurfaceEdit(split, form, expression, value_type, tuple(taps.values()), material))
    return tuple(edits), flow


def _descriptor_plan(project, layout):
    master = _master(project)
    candidates = [MaterialDescriptorLayout.from_dict(v) for v in master.properties.get("descriptor_layouts", ())]
    if len(candidates) != 1:
        raise ShaderGraphError("Bindless editing needs one exact referencing MDF descriptor layout.")
    original = candidates[0]
    shader_lod = master.properties.get("shader_lod")
    exposed = {str(p["source_name"]) for p in master.properties.get("input_pins", ()) if p.get("input_kind") == "texture"}
    authored = tuple(t.name for t in project.textures)
    dormant = tuple(name for name in original.textures if name not in exposed and name not in authored)
    incoming = {l.target_pin: l for l in project.graph.links if l.target_node == master.node_id}
    texture_targets = {}
    for pin in master.properties.get("input_pins", ()):
        if pin.get("input_kind") != "texture":
            continue
        link = incoming.get(pin["pin"])
        node = project.graph.node(link.source_node) if link else None
        definition = project.texture(node.properties.get("texture_id", "")) if node else None
        if node is None or node.kind != "material_texture" or link.source_pin != "texture_object" or definition is None:
            raise ShaderGraphError(f"{pin.get('label', pin['pin'])} needs a texture asset.")
        if pin.get("value_type") != "texture" and definition.dimension != pin.get("value_type"):
            raise ShaderGraphError("A native texture consumer cannot reinterpret its descriptor dimension.")
        source_name = str(pin["source_name"])
        if source_name not in original.textures:
            raise ShaderGraphError("A material texture has no MDF descriptor slot.")
        texture_targets[source_name] = definition.name
    texture_names = authored + dormant
    redirects = original.shader_lod_redirects
    if redirects:
        new_inputs = {t.name for t in project.textures if t.name not in original.textures and t.name not in texture_targets.values()}
        redirects = tuple((tuple(dict.fromkeys(
            [texture_names.index(texture_targets.get(original.textures[j], original.textures[j])) for j in indices]
            + [i for i, name in enumerate(texture_names) if name in new_inputs])), buffers)
            for indices, buffers in redirects)
    updated = replace(original, parameter_size=layout.byte_size, textures=texture_names, shader_lod_redirects=redirects)
    old_words, new_words = original.word_offsets(shader_lod), updated.word_offsets(shader_lod)
    offsets = {old_words["texture", name]: new_words["texture", target]
               for name, target in texture_targets.items() if ("texture", name) in old_words}
    for key, offset in old_words.items():
        if key[0] != "texture":
            offsets[offset] = new_words[key]
        elif offset not in offsets:
            if key not in new_words:
                raise ShaderGraphError(f"The native MDF texture {key[1]} is missing from the graph interface.")
            offsets[offset] = new_words[key]
    return original, updated, offsets


def _validate_fixed_descriptor_interface(project):
    master = _master(project)
    pins = master.properties.get("input_pins", ())
    if {(p.name, p.value_type, p.source_byte_offset) for p in project.parameters} != {
        (p["source_name"], p["value_type"], p["source_byte_offset"]) for p in pins if p.get("input_kind") == "parameter"
    } or {(t.name, t.dimension) for t in project.textures} != {
        (p["source_name"], p["value_type"]) for p in pins if p.get("input_kind") == "texture"
    }:
        raise ShaderGraphError("Bindless interface changes require an unambiguous MDF/LOD descriptor context.")
    incoming = {link.target_pin: link for link in project.graph.links if link.target_node == master.node_id}
    for pin in pins:
        if pin.get("input_kind") != "texture":
            continue
        link = incoming.get(pin["pin"])
        node = project.graph.node(link.source_node) if link else None
        definition = project.texture(str(node.properties.get("texture_id", ""))) if node else None
        if (node is None or node.kind != "material_texture" or link.source_pin != "texture_object"
                or definition is None or definition.name != pin["source_name"] or definition.dimension != pin["value_type"]):
            raise ShaderGraphError("Bindless texture remapping requires an unambiguous MDF/LOD descriptor context.")


@dataclass(frozen=True, slots=True)
class ReEngineShaderGraphProfile:
    profile_id: str
    user_buffer_name: str = "UserMaterial"
    # Structured per-instance copy of UserMaterial read by instanced DXBC passes.
    instanced_material_buffer: str = ""
    material_instance_resource_names: tuple[str, ...] = ()
    # Runtime arena/CBV prefix, not part of the named UserMaterial/MDF buffer.
    material_record_prefix_size: int = 0
    authoring_roles: tuple[str, ...] = STAGE_KEYS
    descriptor_layout_editing: bool = True

    def supplement_material_interface(
        self,
        interface: MmtrMaterialInterface,
        source_label: object,
    ) -> MmtrMaterialInterface:
        if interface.parameters and any(
            resource.name in self.material_instance_resource_names
            for resource in interface.resources
        ):
            return replace(interface, parameter_storage="mixed-material-constant-buffer")
        return interface

    @staticmethod
    def logical_parameter_name(value: str) -> str:
        return logical_material_parameter_name(value)

    @staticmethod
    def shader_parameter_name(value: str) -> str:
        name = str(value)
        return name if name.casefold().startswith("var_") else "VAR_" + name

    def material_parameter_name(self, definition, contract, source_profile: str) -> str:
        if same_mmtr_profile(source_profile, self.profile_id):
            return str(definition.name)
        buffer = contract.buffer(self.user_buffer_name) or contract.structured_buffer(self.instanced_material_buffer)
        existing = {
            self.logical_parameter_name(item.name): self.logical_parameter_name(item.name)
            for item in (buffer.variables if buffer is not None else ())
        }
        folded: dict[str, list[str]] = {}
        for name in existing:
            folded.setdefault(name.casefold(), []).append(name)
        for candidate in (definition.source_name, definition.name):
            if candidate in existing:
                return existing[candidate]
            matches = folded.get(str(candidate).casefold(), ())
            if len(matches) == 1:
                return matches[0]
        return str(definition.source_name or definition.name)

    def material_parameter_storage_expression(self, project, definition, contract) -> str | None:
        if (
            _is_project(project, self.profile_id)
            and _master(project).properties.get("parameter_storage") in BINDLESS_PARAMETER_STORAGES
            and contract.buffer(self.user_buffer_name) is None
        ):
            if definition.source_byte_offset is None:
                raise ShaderGraphError("A bindless material parameter needs its MDF field offset.")
            width = value_width(definition.value_type)
            parts = [
                f"REasyMaterialLoad(REasyMaterialAddress, {self.material_record_prefix_size + int(definition.source_byte_offset) + i * 4}u)"
                for i in range(width)
            ]
            return parts[0] if width == 1 else f"{definition.value_type}({', '.join(parts)})"
        return None

    def editable_textures(self, document):
        cached = document._material_texture_contracts_cache.get(self.profile_id)
        if cached is not None:
            return cached
        layouts = document.material_interface.descriptor_layouts
        order = {name: i for i, name in enumerate(layouts[0].textures)} if len(layouts) == 1 else {}
        if document.material_family is not None:
            from ..material_project import material_resource_dimension
            candidates = {}
            for member in document.material_family.members:
                for item in self.editable_textures(member):
                    key = item.name
                    existing = candidates.get(key)
                    dimension = material_resource_dimension(item)
                    previous = material_resource_dimension(existing) if existing is not None else ""
                    if dimension and previous and dimension != previous:
                        raise ShaderGraphError(f"MMTRS texture {item.name} has inconsistent native dimensions.")
                    if existing is None or (dimension and not previous):
                        candidates[key] = item
        else:
            names = bound_texture_names(document)
            candidates = {item.name: item
                for item in document.material_interface.resources
                if item.resource_kind == "shader_resource" and item.name.casefold() in names
            }
        if document.material_family is None and len(layouts) == 1 and (not layouts[0].shader_lod_redirects or document.material_interface.shader_lod is not None) and order.keys() - candidates.keys():
            from ..material_usage import native_input_usage
            slots = {offset: name for (kind, name), offset in layouts[0].word_offsets(document.material_interface.shader_lod).items() if kind == "texture"}
            native = native_input_usage(document)
            dimensions = dict(native.indirect_textures)
            for offset in native.record_words:
                name = slots.get(offset)
                if name is not None and name not in candidates:
                    dimension = dimensions.get(offset)
                    candidates[name] = MmtrResourceContract("shader_resource", name, (dimension,) if dimension else ())
        result = tuple(sorted(candidates.values(), key=lambda item: (order.get(item.name, len(order)), item.name)))
        document._material_texture_contracts_cache[self.profile_id] = result
        return result

    @staticmethod
    def material_sampler(contract: ShaderContract) -> str:
        for name in ("AutomaticWrap", "BilinearWrap", "TrilinearWrap", "BilinearClamp"):
            resource = contract.resource(name)
            if resource is not None and resource.input_type == "sampler" and not (resource.flags & 0x2):
                return resource.name
        return next(
            (item.name for item in contract.resources if item.input_type == "sampler" and not (item.flags & 0x2)),
            "",
        )

    @staticmethod
    def material_texture_resource(definition, contract: ShaderContract) -> str:
        if definition is None:
            return ""
        for name in (definition.name, definition.source_name):
            resource = next((item for item in contract.resources if item.name == name), None)
            if resource is not None and resource.sampleable:
                return resource.name
        return ""

    def material_texture_type_code(self, dimension: str) -> int:
        try:
            return _TEXTURE_CODES[str(dimension)]
        except KeyError as exc:
            raise ValueError(
                f"{self.profile_id} cannot encode texture dimension {dimension!r}"
            ) from exc

    @staticmethod
    def material_output_binding_point(document, semantics, contract, resource) -> int:
        program = document.sdf.targets[int(semantics.target_index)].programs[
            int(semantics.program_index)
        ]
        role = semantics.shader_role
        shader = program.shader(role)
        if shader is None or shader.binary_format != "spirv-module":
            return int(resource.bind_point)
        if contract.vulkan_execution is not None:
            binding = next((item for item in contract.vulkan_execution.bindings
                            if item.name == resource.name), None)
            if binding is None:
                raise ShaderGraphError(f"Vulkan material resource {resource.name!r} has no compiled linkage.")
            return int(binding.slot)
        native = _native_spirv(shader)
        plan = _spirv_binding_plan(
            contract.resources,
            native,
            _outer_resource_kinds(program, role),
        )
        assignment = plan.get(resource.name.casefold())
        if assignment is None:
            raise ShaderGraphError(
                f"SPIR-V material binding {resource.name!r} is absent from the compiled route."
            )
        return int(assignment[3])

    def seed_material_project(self, document, project, *, defer_shader_recovery: bool = False) -> None:
        if not self.material_program_routes(document):
            graph = ShaderGraph("material", "")
            graph.add_node("material_surface", x=420.0, y=140.0)
            project.name = "Compiled Master Material"
            project.source_material_model = _MODEL_PREFIX + self.profile_id.casefold() + ".compiled_only"
            project.graph = graph
            project._prepare_semantics = None
            return
        _seed(document, project, self, defer_shader_recovery=defer_shader_recovery or document.material_family is not None)
        if document.material_family is not None:
            from ..material_family import seed_material_family
            seed_material_family(document, project, defer_shader_recovery=defer_shader_recovery)

    def seed_material_project_interface(self, document, project) -> None:
        self.seed_material_project(document, project, defer_shader_recovery=True)

    def upgrade_material_project(self, document, project) -> bool:
        if not _is_project(project, self.profile_id):
            _seed(document, project, self)
            return True
        master = _master(project)
        changed = False
        for name, value in (
            ("parameter_storage", "renderer-constant-buffers" if renderer_only(document, self) else document.material_interface.parameter_storage),
            ("parameter_buffer_sizes", list(document.material_interface.user_buffer_sizes)),
            ("descriptor_layouts", [item.to_dict() for item in document.material_interface.descriptor_layouts]),
        ):
            if name not in master.properties:
                master.properties[name] = value
                changed = True
        changed = _expose_descriptor_inputs(document, project, self) or changed
        changed = _upgrade_surface_outputs(document, project, self) or changed
        expected = set(self.material_program_routes(document))
        routes = master.properties.get("program_routes", [])
        represented = {
            (int(item.get("target", -1)), int(item.get("program", -1)), str(item.get("role", "pixel")))
            for item in routes
            if isinstance(item, dict)
        } if isinstance(routes, list) else set()
        if (
            (
                int(master.properties.get("semantic_bundle_version", 0)) >= SEMANTIC_BUNDLE_VERSION
                or project._prepare_semantics is not None
            )
            and expected.issubset(represented)
        ):
            return changed
        if document.material_family is not None:
            from ..material_family import seed_material_family
            seed_material_family(document, project, defer_shader_recovery=False)
            return True
        programs, routes = _bundle(document, self)
        master.properties["semantic_programs"] = programs
        master.properties["program_routes"] = routes
        master.properties["semantic_bundle_version"] = SEMANTIC_BUNDLE_VERSION
        project._prepare_semantics = None
        return True

    def shader_contract(self, document, program, role: str) -> ShaderContract:
        resolved_role = str(role).strip().casefold()
        cache_key = (
            self.profile_id,
            int(program.target_index),
            int(program.program_index),
            resolved_role,
        )
        cached = document._shader_contract_cache.get(cache_key)
        if cached is not None:
            return cached
        shader = program.shader(resolved_role)
        if shader is None or not shader.present:
            raise ShaderGraphError(f"{program.name} has no {resolved_role} shader.")
        if shader.binary_format != "spirv-module":
            contract = shader_contract_for_program(
                document.profile.sdf_profile,
                program,
                resolved_role,
                native_contract_cache=document._native_shader_contract_cache,
            )
            document._shader_contract_cache[cache_key] = contract
            return contract
        native = _native_spirv(shader)
        native_key = ("spirv", shader.spirv.module, shader.spirv.entry_point)
        candidate = document._native_shader_contract_cache.get(native_key)
        if candidate is None:
            candidate = spirv_shader_contract(shader.spirv.module, entry_point=shader.spirv.entry_point)
            document._native_shader_contract_cache[native_key] = candidate
        resource_kinds = _outer_resource_kinds(program, role)
        native_names = frozenset(item.name.casefold() for item in native.descriptors)
        missing_outer = set(resource_kinds) - native_names
        if missing_outer:
            raise ShaderGraphError(
                "SPIR-V module omits native SDF resources: "
                + ", ".join(sorted(missing_outer))
            )
        for descriptor in native.descriptors:
            key = descriptor.name.casefold()
            outer_kind = resource_kinds.get(key)
            if outer_kind is None or outer_kind == "optional_resource":
                continue
            if outer_kind != _descriptor_resource_kind(descriptor):
                raise ShaderGraphError(
                    f"SPIR-V resource {descriptor.name!r} disagrees with its SDF ownership class."
                )
        _spirv_binding_plan(
            candidate.resources,
            native,
            resource_kinds,
            used_names=native_names,
        )
        document._shader_contract_cache[cache_key] = candidate
        return candidate

    def material_program_routes(self, document):
        cached = document._material_program_routes_cache.get(self.profile_id)
        if cached is not None:
            return cached
        names = {item.name.casefold() for item in self.editable_textures(document)}
        renderer = renderer_only(document, self)
        user_names = {item.casefold() for item in document.profile.user_material_buffer_names}
        result = []
        for semantics in document.programs:
            program = document.sdf.targets[semantics.target_index].programs[semantics.program_index]
            outer_material = any(item.name.casefold() in user_names for item in program.constant_buffers) or any(
                item.name.casefold() in names for item in program.shader_resources
            )
            dynamic = (
                bool(document.material_interface.parameters or document.material_interface.descriptor_layouts)
                and any(
                    item.name in self.material_instance_resource_names
                    or item.name == self.instanced_material_buffer
                    for item in program.shader_resources
                )
            )
            if not outer_material and not dynamic and not renderer:
                continue
            for shader in program.shaders:
                if not shader.present or shader.role not in self.authoring_roles:
                    continue
                try:
                    contract = self.shader_contract(document, program, shader.role)
                except ValueError:
                    continue
                if renderer or any(item.name.casefold() in user_names for item in contract.constant_buffers) or any(
                    item.name.casefold() in names or (dynamic and (
                        item.name in self.material_instance_resource_names or item.name == self.instanced_material_buffer))
                    for item in contract.resources
                ):
                    result.append((semantics.target_index, semantics.program_index, shader.role))
        resolved = tuple(result)
        document._material_program_routes_cache[self.profile_id] = resolved
        return resolved

    def material_interface_editing_supported(self, document, project=None) -> bool:
        master = _master(project) if project is not None and _is_project(project, self.profile_id) else None
        storage = master.properties.get("parameter_storage") if master else document.material_interface.parameter_storage
        if storage == "renderer-constant-buffers" or (project is None and renderer_only(document, self)):
            return False
        if storage not in BINDLESS_PARAMETER_STORAGES:
            return True
        layouts = tuple(MaterialDescriptorLayout.from_dict(v) for v in master.properties.get("descriptor_layouts", ())) if master else document.material_interface.descriptor_layouts
        shader_lod = master.properties.get("shader_lod") if master else document.material_interface.shader_lod
        return self.descriptor_layout_editing and len(layouts) == 1 and (not layouts[0].shader_lod_redirects or shader_lod is not None)

    def material_descriptor_layout(self, project, document, layout):
        if (not _is_project(project, self.profile_id)
                or _master(project).properties.get("parameter_storage") not in BINDLESS_PARAMETER_STORAGES
                or not self.material_interface_editing_supported(document, project)):
            return None, None
        original, updated, _offsets = _descriptor_plan(project, layout)
        current_instance = _master(project).properties.get("instance_descriptor_layout")
        return MaterialDescriptorLayout.from_dict(current_instance) if current_instance else original, updated

    def validate_material_project_edit(self, project, document) -> None:
        from file_handlers.mmtr.material_project import material_authoring_coverage
        coverage = material_authoring_coverage(document, project)
        if not coverage.complete:
            raise ShaderGraphError("This material has shader consumers without a validated rebuild route.")

    def material_project_program_routes(self, project, document):
        if not _is_project(project, self.profile_id):
            return self.material_program_routes(document)
        routes = _master(project).properties.get("program_routes", [])
        if not isinstance(routes, list):
            raise ShaderGraphError("Recovered material route table is invalid.")
        result = []
        for item in routes:
            if not isinstance(item, dict):
                raise ShaderGraphError("Recovered material route is invalid.")
            key = (int(item.get("target", -1)), int(item.get("program", -1)), str(item.get("role", "pixel")))
            if (
                key[0] < 0
                or key[0] >= len(document.sdf.targets)
                or key[1] < 0
                or key[1] >= len(document.sdf.targets[key[0]].programs)
                or key[2] not in STAGE_KEYS
            ):
                raise ShaderGraphError("Recovered material does not match this MMTR.")
            result.append(key)
        if not result or len(result) != len(set(result)):
            raise ShaderGraphError("Recovered material route table is empty or ambiguous.")
        result.extend(_edited_surface_routes(project, document, self))
        return (*result, *_vertex_value_routes(project, document, self, result))

    def material_project_owns_contract(self, project) -> bool:
        return _is_project(project, self.profile_id)

    def material_project_shader_key(self, project, contract, semantics, document):
        if not _is_project(project, self.profile_id):
            return ""
        digest, _item, binary_format = _program_reference(project, semantics, document)
        return _program_key(digest, binary_format, self._pass_semantics(project, document, semantics))

    def _pass_semantics(self, project, document, semantics):
        target, program = semantics.target_index, semantics.program_index
        if graph_draws_for(project.graph):
            semantics = replace(semantics, purpose=pass_purpose(document, self, target, program))
        if semantics.shader_role in {"vertex", "pixel"}:
            values, refusals = _vertex_values(project, document, self, target, program)
            semantics = replace(semantics, vertex_values=values, vertex_value_refusals=refusals)
        if graph_reads_surface_data(project.graph):
            if semantics.shader_role == "pixel":
                semantics = replace(semantics, surface_data=program_surface_data(document, self, target, program))
            elif semantics.shader_role == "vertex":
                semantics = replace(semantics, surface_data=program_vertex_inputs(document, self, target, program))
        return semantics

    def generate_recovered_material_shader_source(
        self, project, document, contract, semantics
    ):
        if not _is_project(project, self.profile_id):
            return None
        from file_handlers.mmtr.material_project import _augmented_contract

        semantics = self._pass_semantics(project, document, semantics)
        recovered = _program(project, semantics, document, self)
        master = _master(project)
        bindless = master.properties.get("parameter_storage") in BINDLESS_PARAMETER_STORAGES
        material_word_offsets = {}
        descriptor_layout = None
        source_descriptor_layout = None
        if bindless:
            original = {
                str(pin.get("source_id")): pin
                for pin in master.properties.get("input_pins", [])
                if isinstance(pin, dict) and pin.get("input_kind") == "parameter"
            }
            sizes = master.properties.get("parameter_buffer_sizes", [])
            if len(sizes) != 1:
                raise ShaderGraphError("Bindless material allocation needs one exact MDF size.")
            native_buffer = recovered.contract.buffer(self.user_buffer_name)
            semantic_buffer = ShaderContractBuffer(
                self.user_buffer_name, int(sizes[0]), native_buffer.bind_point if native_buffer else 0, 1,
                tuple(ShaderContractVariable(self.shader_parameter_name(pin["source_name"]), int(pin["source_byte_offset"]),
                                             value_width(pin["value_type"]) * 4, pin["value_type"], pin["value_type"])
                      for pin in original.values()),
            )
            adjusted, layout = _augmented_contract(
                project, recovered.contract, self, document, semantics.shader_role, material_buffer=semantic_buffer,
                augment_textures=native_buffer is not None or any(
                    recovered.contract.resource(pin["source_name"]) is not None
                    for pin in master.properties.get("input_pins", ()) if pin.get("input_kind") == "texture"
                ),
            )
            if self.material_interface_editing_supported(document, project):
                source_descriptor_layout, descriptor_layout, material_word_offsets = _descriptor_plan(project, layout)
                prefix = self.material_record_prefix_size
                material_word_offsets = {source + prefix: target + prefix for source, target in material_word_offsets.items()}
            else:
                _validate_fixed_descriptor_interface(project)
            by_name = {v.name: v for v in layout.variables}
            project = replace(project, parameters=[replace(p, source_byte_offset=by_name[p.name].byte_offset) for p in project.parameters])
        else:
            adjusted, layout = _augmented_contract(project, recovered.contract, self, document, semantics.shader_role)
        adjusted = add_engine_buffers(document, adjusted, semantics, graph_renderer_buffers(project.graph))
        adjusted = add_engine_resources(document, adjusted, semantics, graph_renderer_resources(project.graph))
        adjusted = add_vertex_values(document, self, adjusted, semantics)
        expressions, structured, textures, helpers, material_expressions = _overrides(
            self,
            project,
            adjusted,
            semantics,
            recovered,
            descriptor_layout,
            source_descriptor_layout,
        )
        semantic_key = _program_key(recovered.digest, recovered.binary_format, semantics)
        if recovered.ir_kind == "spirv":
            route = document.sdf.targets[semantics.target_index].programs[semantics.program_index]
            renamed = tuple(sorted(
                (self.shader_parameter_name(variable.source_name), self.shader_parameter_name(variable.name))
                for variable in layout.variables
                if variable.source_name and variable.source_name != variable.name))
            surface_helpers: dict = {}
            edits, flow = _surface_edits(self, project, adjusted, semantics, recovered, surface_helpers)
            surface_plan = ()
            if edits:
                from ..surface_splice_spirv import spirv_surface_plan
                surface_plan = spirv_surface_plan(recovered.ir, flow, edits)
                helpers = "\n\n".join(filter(None, (helpers, *surface_helpers.values())))
            product = _SpirvProduct(recovered.ir, recovered.contract, adjusted,
                                    _native_spirv(route.shader(semantics.shader_role)),
                                    _outer_resource_kinds(route, semantics.shader_role),
                                    expressions, textures, helpers, renamed, surface_plan)
            return product, adjusted, layout, semantic_key
        if recovered.ir_kind == "dxbc-sm5":
            texture_slots = {
                source[1]: target[1] for source, target in textures.items()
                if source[0] == 0 and target[0] == 0
            }
            surface_helpers: dict = {}
            edits, flow = _surface_edits(self, project, adjusted, semantics, recovered, surface_helpers)
            # Added values move the inputs the rasterizer generates to registers after them.
            ir, hooks = relocated_dxbc_inputs(recovered.ir, recovered.contract, adjusted), None
            if edits:
                from ..surface_splice_dxbc import splice_dxbc_surface
                ir, hooks = splice_dxbc_surface(ir, flow, adjusted, edits)
                helpers = "\n\n".join(filter(None, (helpers, *surface_helpers.values())))
            generated = generate_recovered_dxbc_shader(
                ir, adjusted, constant_overrides=expressions, structured_overrides=structured,
                texture_binding_overrides=texture_slots, additional_helpers=helpers,
                semantic_buffer_names=frozenset((self.user_buffer_name,)), instruction_hooks=hooks,
            )
            return generated, adjusted, layout, semantic_key
        if recovered.binary_format == "spirv-module":
            raise ShaderGraphError("Legacy Vulkan project needs native semantic recovery before rebuilding.")
        surface_helpers: dict = {}
        edits, flow = _surface_edits(self, project, adjusted, semantics, recovered, surface_helpers,
                                     texture_resolver=_bindless_texture_resolver(
                                         self, master, adjusted, recovered, descriptor_layout,
                                         source_descriptor_layout, surface_helpers))
        surface_plan = ()
        if edits:
            from ..surface_splice_dxil import dxil_surface_plan
            surface_plan = dxil_surface_plan(flow, edits, recovered.ir.to_assembly(), recovered.contract)
            helpers = "\n\n".join(filter(None, (helpers, *surface_helpers.values())))
        product = _DxilProduct(
            recovered.ir,
            recovered.contract,
            expressions,
            textures,
            helpers,
            material_expressions,
            ((descriptor_layout.allocation_size(master.properties.get("shader_lod"))
              if descriptor_layout is not None else layout.byte_size)
             + self.material_record_prefix_size)
            if material_expressions or any(edit.material for edit in surface_plan) else 0,
            target=adjusted.target,
            material_word_offsets=material_word_offsets,
            surface_plan=surface_plan,
        )
        return product, adjusted, layout, semantic_key

    def compile_material_shaders(self, project, requests):
        results = [None] * len(requests)
        dxil = []
        for index, request in enumerate(requests):
            generated = request.generated
            if isinstance(generated, _DxilProduct):
                dxil.append((index, (generated.ir, generated.source_contract,
                    request.adjusted_contract, generated.rebuild_options())))
            else:
                try:
                    results[index] = self.compile_material_shader(
                        project, generated, request.adjusted_contract, request.semantics,
                        request.shader, request.target_index,
                    )
                except Exception as exc:
                    results[index] = exc
        if dxil:
            for (index, _request), result in zip(dxil, rebuild_re_engine_dxil_batch(tuple(item[1] for item in dxil))):
                results[index] = result if isinstance(result, Exception) else _NativeCompileResult(result.bytecode, result.contract)
        return tuple(results)

    def compile_material_shader(
        self,
        _project,
        generated,
        adjusted,
        _semantics,
        shader,
        _target_index,
    ):
        if isinstance(generated, _SpirvProduct):
            try:
                return _compile_spirv_product(generated)
            except (DxcError, SpirvError) as exc:
                raise ShaderGraphError(
                    f"DXC could not build the SPIR-V material route: {exc}"
                ) from exc
        if shader.binary_format == "spirv-module":
            raise ShaderGraphError("SPIR-V material route lacks its native binding plan.")
        result = compile_hlsl_source_detailed(
            generated.source,
            entry_point=generated.entry_point,
            target=generated.target,
            ieee_strict=generated.ieee_strict,
            warnings_as_errors=True,
        )
        if adjusted.stage in {"domain", "geometry", "hull"}:
            original = recover_dxbc_ir(bytes(shader.payload))
            rebuilt = recover_dxbc_ir(result.bytecode)
            if original.execution != rebuilt.execution:
                raise ShaderGraphError("Compiled primitive/tessellation execution contract changed")
        return result

    @staticmethod
    def compiled_material_shader_contract(bytecode, binary_format):
        if binary_format == "spirv-module":
            raise ShaderGraphError("SPIR-V material compiler did not return a reflected contract.")
        parsed = parse_dxbc(bytecode)
        if int(parsed.model_major) >= 6:
            parsed = enrich_dxil_shader(parsed, bytecode)
        # Container reflection has no DXIL NumThreads; read it from the bytecode.
        from file_handlers.mmtr.material_implementation import implementation_contract
        return implementation_contract(ShaderContract.from_shader(parsed), bytecode)

    @staticmethod
    def validate_material_shader_bytecode(bytecode):
        if bytes(bytecode)[:4] != b"DXBC":
            return
        parsed = parse_dxbc(bytecode)
        if int(parsed.model_major) < 6:
            validate_recovered_dxbc_shader_bytecode(bytecode)


_TEXTURE_CODES = {
    "texture1d": 0x01,
    "texture1darray": 0x05,
    "texture2d": 0x02,
    "texture2darray": 0x06,
    "texture3d": 0x03,
    "texturecube": 0x04,
}


BINDLESS_MATERIAL_RESOURCES = ("BindlessBuffer", "BindlessConstantBufferViewTbl")


def re_engine_shader_graph_profile(
    game: str,
    *,
    bindless_materials: bool = False,
    instanced_material_buffer: str = "",
    material_record_prefix_size: int = 0,
    full_authoring: bool = True,
) -> ReEngineShaderGraphProfile:
    """``full_authoring`` is off for unverified builds."""
    return ReEngineShaderGraphProfile(
        str(game),
        instanced_material_buffer=instanced_material_buffer,
        material_instance_resource_names=BINDLESS_MATERIAL_RESOURCES if bindless_materials else (),
        material_record_prefix_size=material_record_prefix_size,
        authoring_roles=STAGE_KEYS if full_authoring else ("pixel",),
        descriptor_layout_editing=full_authoring,
    )


__all__ = [
    "RE_ENGINE_RECOVERED_MASTER_KIND",
    "RE_ENGINE_RECOVERED_MASTER_NODE",
    "ReEngineShaderGraphProfile",
    "re_engine_shader_graph_profile",
]
