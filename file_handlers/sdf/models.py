from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

if TYPE_CHECKING:
    from .dxbc import DxbcShader
    from .profiles import SdfGameProfile


SDF_MAGIC = b"SDF\x00"
STAGE_KEYS = ("vertex", "hull", "domain", "geometry", "pixel", "compute")
STATE_KINDS = {
    "rasterizer": ("rasterizer_state", "set_rasterizer_state"),
    "blend": ("blend_state", "set_blend_state"),
    "depth": ("depth_stencil_state", "set_depth_stencil_state"),
}
STAGE_DISPLAY_NAMES = {
    "vertex": QT_TRANSLATE_NOOP("ShaderStages", "Vertex"), "hull": QT_TRANSLATE_NOOP("ShaderStages", "Tessellation control"),
    "domain": QT_TRANSLATE_NOOP("ShaderStages", "Tessellation evaluation"), "geometry": QT_TRANSLATE_NOOP("ShaderStages", "Geometry"),
    "pixel": QT_TRANSLATE_NOOP("ShaderStages", "Pixel"), "compute": QT_TRANSLATE_NOOP("ShaderStages", "Compute"),
}


def stage_display_name(role: str) -> str:
    name = STAGE_DISPLAY_NAMES.get(role)
    return QCoreApplication.translate("ShaderStages", name) if name else role.replace("_", " ").title()


def humanize_identifier(text: str) -> str:
    text = str(text).replace("_", " ").replace("-", " ")
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    return " ".join(text.split())


SHADER_TARGET_PREFIXES = {
    "vertex": "vs", "hull": "hs", "domain": "ds", "geometry": "gs", "pixel": "ps",
    "compute": "cs", "mesh": "ms", "amplification": "as", "library": "lib",
}


def shader_target(stage: str, model_major: int, model_minor: int) -> str:
    prefix = SHADER_TARGET_PREFIXES.get(stage)
    return f"{prefix}_{model_major}_{model_minor}" if prefix else ""


RESOURCE_KINDS = (
    "constant_buffer",
    "sampler",
    "shader_resource",
    "unordered_access",
    "optional_resource",
)


@dataclass(frozen=True, slots=True)
class SdfSemanticNote:
    field: str
    message: str
    status: str = "unknown"


@dataclass(frozen=True, slots=True)
class SdfHeader:
    target_count: int
    program_count: int
    shader_binary_offset: int
    program_table_offset: int = 0x10
    program_counts: tuple[int, ...] = ()

    @property
    def record_count(self) -> int:
        if self.program_counts:
            return sum(self.program_counts)
        return self.target_count * self.program_count


@dataclass(frozen=True, slots=True)
class SdfSpirvInterfaceVariable:
    variable_id: int
    location: int
    scalar_type: str
    vector_components: int


@dataclass(frozen=True, slots=True)
class SdfSpirvModule:
    module: bytes = field(repr=False)
    version: int
    generator: int
    bound: int
    schema: int
    instruction_count: int
    entry_point: str
    execution_model: int
    execution_model_name: str
    input_variables: tuple[SdfSpirvInterfaceVariable, ...] = ()


@dataclass(frozen=True, slots=True)
class SdfSpirvInputElement:
    semantic_id: int
    semantic: str
    semantic_index: int
    location: int
    variable_id: int
    scalar_type: str
    is_vector: bool
    component_count: int


@dataclass(frozen=True, slots=True)
class SdfSpirvInputSignature:
    elements: tuple[SdfSpirvInputElement, ...]


@dataclass(frozen=True, slots=True)
class SdfRayTracingLibrary:
    raygen: str
    closesthit: str = ""
    miss: str = ""
    anyhit: str = ""
    bytecode: bytes = field(default=b"", repr=False)
    miss_shadow: str = ""
    anyhit_shadow: str = ""
    miss_vfx: str = ""
    anyhit_vfx: str = ""
    anyhit_vfx_shadow: str = ""
    export_layout: str = "standard"


@dataclass(frozen=True, slots=True)
class SdfRuntimeCacheKey:
    value: int
    algorithm: str = "opaque"
    source_digest: bytes = field(default=b"", repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SdfShaderReference:
    role: str
    offset: int
    size: int
    reflection: "DxbcShader | None" = None
    payload: bytes = field(default=b"", repr=False)
    binary_format: str = "none"
    spirv: SdfSpirvModule | None = None
    spirv_input_signature: SdfSpirvInputSignature | None = None
    raytracing_library: SdfRayTracingLibrary | None = None
    runtime_cache_key: SdfRuntimeCacheKey | None = None

    @property
    def present(self) -> bool:
        return bool(self.offset and self.size)


@dataclass(frozen=True, slots=True)
class SdfInputElement:
    semantic_id: int
    semantic: str
    format_id: int
    dxgi_format_id: int
    dxgi_format: str
    input_slot: int
    semantic_index: int
    aligned_byte_offset: int
    per_instance: bool
    instance_step_rate: int


@dataclass(frozen=True, slots=True)
class SdfInputLayout:
    offset: int
    element_count: int
    reserved: int
    elements: tuple[SdfInputElement, ...]
    raw: bytes
    runtime_cache_key: SdfRuntimeCacheKey | None = None


@dataclass(frozen=True, slots=True)
class SdfCompactDeclaration:
    offset: int
    semantic_id: int
    semantic: str
    semantic_index: int
    packed_register: int
    register: int
    register_low_bit: int
    component_type_id: int
    component_type: str
    system_value_id: int
    system_value: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class SdfBindingDescriptor:
    offset: int
    resource_kind: str
    bind_slots: tuple[int, int, int, int, int, int]
    stage_mask: int
    active_stages: tuple[str, ...]
    type_code: int
    type_name: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class SdfConstantVariable:
    offset: int
    name_offset: int
    name: str
    name_hash: int
    byte_offset: int
    byte_size: int


@dataclass(frozen=True, slots=True)
class SdfDetachedVariable:
    name: str
    name_offset: int
    name_hash: int
    byte_offset: int
    byte_size: int


@dataclass(frozen=True, slots=True)
class SdfDetachedVariableTable:
    offset: int
    variables: tuple[SdfDetachedVariable, ...]

    @property
    def size(self) -> int:
        return len(self.variables) * 0x10


@dataclass(frozen=True, slots=True)
class SdfDiscardedAuthoringRange:
    offset: int
    size: int
    kind: str


@dataclass(frozen=True, slots=True)
class SdfConstantBuffer:
    offset: int
    name_offset: int
    name: str
    name_hash: int
    reserved: int
    byte_size: int
    variable_count: int
    variables_offset: int
    variables: tuple[SdfConstantVariable, ...]


@dataclass(frozen=True, slots=True)
class SdfNamedResource:
    offset: int
    resource_kind: str
    name_offset: int
    name: str
    name_hash: int
    reserved: int


@dataclass(frozen=True, slots=True)
class SdfRasterizerState:
    offset: int
    packed_flags: int
    fill_mode_id: int
    fill_mode: str
    cull_mode_id: int
    cull_mode: str
    front_counter_clockwise: bool
    depth_bias: int
    depth_bias_clamp: float
    slope_scaled_depth_bias: float
    depth_clip_enable: bool
    scissor_enable: bool
    multisample_enable: bool
    antialiased_line_enable: bool
    reserved_bits: int
    raw: bytes
    binner_control: int = 0
    conservative_rasterization: bool = False
    runtime_cache_key: SdfRuntimeCacheKey | None = None


@dataclass(frozen=True, slots=True)
class SdfBlendTarget:
    index: int
    packed: int
    blend_enable: bool
    source_blend_id: int
    source_blend: str
    destination_blend_id: int
    destination_blend: str
    blend_operation_id: int
    blend_operation: str
    source_alpha_blend_id: int
    source_alpha_blend: str
    destination_alpha_blend_id: int
    destination_alpha_blend: str
    alpha_blend_operation_id: int
    alpha_blend_operation: str
    write_mask: int
    write_channels: str
    reserved_bits: int


@dataclass(frozen=True, slots=True)
class SdfBlendState:
    offset: int
    alpha_to_coverage_enable: bool
    independent_blend_enable: bool
    reserved_flags: int
    render_targets: tuple[SdfBlendTarget, ...]
    raw: bytes
    runtime_cache_key: SdfRuntimeCacheKey | None = None


@dataclass(frozen=True, slots=True)
class SdfDepthStencilFace:
    stencil_fail_operation_id: int
    stencil_fail_operation: str
    stencil_depth_fail_operation_id: int
    stencil_depth_fail_operation: str
    stencil_pass_operation_id: int
    stencil_pass_operation: str
    stencil_function_id: int
    stencil_function: str


@dataclass(frozen=True, slots=True)
class SdfDepthStencilState:
    offset: int
    packed: int
    depth_enable: bool
    depth_write_mask_id: int
    depth_write_mask: str
    depth_function_id: int
    depth_function: str
    stencil_enable: bool
    stencil_read_mask: int
    stencil_write_mask: int
    front_face: SdfDepthStencilFace
    back_face: SdfDepthStencilFace
    depth_bounds_test_enable: bool
    reserved_bits: int
    raw: bytes
    runtime_cache_key: SdfRuntimeCacheKey | None = None


class _ProgramResources:
    __slots__ = ()

    def shader(self, stage: str) -> SdfShaderReference | None:
        key = str(stage).strip().lower()
        return next((shader for shader in self.shaders if shader.role == key), None)

    @property
    def resource_metadata(self) -> tuple[tuple, ...]:
        return (self.constant_buffers, self.samplers, self.shader_resources,
                self.unordered_access_resources, self.optional_resources)


@dataclass(frozen=True, slots=True)
class SdfProgramTables(_ProgramResources):
    target_index: int
    program_index: int
    record_index: int
    name_offset: int
    name: str
    shaders: tuple[SdfShaderReference, ...]
    total_stage_binding_count: int
    per_stage_binding_counts: tuple[tuple[int, int, int, int, int, int], ...]
    distinct_resource_counts: tuple[int, int, int, int, int]
    bindings: tuple[tuple[SdfBindingDescriptor, ...], ...]
    constant_buffers: tuple[SdfConstantBuffer, ...]
    samplers: tuple[SdfNamedResource, ...]
    shader_resources: tuple[SdfNamedResource, ...]
    unordered_access_resources: tuple[SdfNamedResource, ...]
    optional_resources: tuple[SdfNamedResource, ...]


@dataclass(frozen=True, slots=True)
class SdfProgram(_ProgramResources):
    target_index: int
    program_index: int
    record_index: int
    record_offset: int
    name_offset: int
    name: str
    input_signature: SdfShaderReference
    shaders: tuple[SdfShaderReference, ...]
    total_stage_binding_count: int
    per_stage_binding_counts: tuple[tuple[int, int, int, int, int, int], ...]
    distinct_resource_counts: tuple[int, int, int, int, int]
    compact_declaration_count: int
    compute_group_size: tuple[int, int, int]
    primitive_topology_id: int
    primitive_topology: str
    reserved_tail_byte: int
    shader_requirements: tuple[str, ...] = ()
    unknown_shader_requirement_bits: int = 0
    input_layout: SdfInputLayout | None = None
    rasterizer_state: SdfRasterizerState | None = None
    blend_state: SdfBlendState | None = None
    depth_stencil_state: SdfDepthStencilState | None = None
    compact_declarations: tuple[SdfCompactDeclaration, ...] = ()
    bindings: tuple[tuple[SdfBindingDescriptor, ...], ...] = ()
    constant_buffers: tuple[SdfConstantBuffer, ...] = ()
    samplers: tuple[SdfNamedResource, ...] = ()
    shader_resources: tuple[SdfNamedResource, ...] = ()
    unordered_access_resources: tuple[SdfNamedResource, ...] = ()
    optional_resources: tuple[SdfNamedResource, ...] = ()

    @property
    def raytracing_library(self) -> SdfRayTracingLibrary | None:
        compute = self.shader("compute")
        return compute.raytracing_library if compute is not None else None


class _Lazy(Sequence):
    """Thread safe; ``lock`` must be reentrant because a read can index another _Lazy sharing it."""

    def __init__(self, read: Callable[[int], object], count: int, lock):
        self._read = read
        self._items: list = [None] * count
        self._lock = lock

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[item] for item in range(len(self))[index])
        item = self._items[index]
        if item is None:
            with self._lock:
                item = self._items[index]
                if item is None:
                    item = self._items[index] = self._read(range(len(self))[index])
        return item


class LazyPrograms(_Lazy):
    """``lock`` is shared by the file's targets."""

    def __init__(self, names: tuple[str, ...], tables: Callable[[int], SdfProgramTables],
                 parse: Callable[[int], SdfProgram], lock):
        self.names = names
        self.tables = _Lazy(tables, len(names), lock)
        super().__init__(lambda index: parse(self.tables[index].program_index), len(names), lock)


@dataclass(frozen=True, slots=True)
class SdfTarget:
    index: int
    key: str
    display_name: str
    selection_capability_bit: int | None
    programs: tuple[SdfProgram, ...] | LazyPrograms


def program_tables(target) -> Sequence[SdfProgram | SdfProgramTables]:
    programs = target.programs
    return programs.tables if isinstance(programs, LazyPrograms) else programs


def program_names(target) -> tuple[str, ...]:
    programs = target.programs
    return programs.names if isinstance(programs, LazyPrograms) else tuple(program.name for program in programs)


@dataclass(slots=True)
class SdfDocument:
    profile: "SdfGameProfile"
    header: SdfHeader
    targets: tuple[SdfTarget, ...]
    original_data: bytes = field(repr=False)
    shader_blob: memoryview = field(repr=False)
    notes: tuple[SdfSemanticNote, ...] = ()
    unique_shaders: dict[tuple[int, int], "DxbcShader"] = field(default_factory=dict)
    detached_variable_tables: tuple[SdfDetachedVariableTable, ...] = ()
    discarded_authoring_ranges: tuple[SdfDiscardedAuthoringRange, ...] = ()

    @property
    def file_size(self) -> int:
        return len(self.original_data)

    def write(self) -> bytes:
        return self.original_data

    def write_canonical(self) -> bytes:
        from .canonical_writer import serialize_sdf

        return serialize_sdf(self)


__all__ = [name for name in globals() if name.startswith("Sdf")]
