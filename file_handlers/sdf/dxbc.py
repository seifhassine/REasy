from __future__ import annotations

lazy import ctypes
from dataclasses import dataclass
import hashlib
import os
import struct

lazy from .binary_reader import BoundedReader
lazy from .com import blob_bytes, com_release
lazy from .hlsl import HlslCompileError, _compiler


DXBC_MAGIC = b"DXBC"

SHADER_STAGE_NAMES = {
    0: "pixel",
    1: "vertex",
    2: "geometry",
    3: "hull",
    4: "domain",
    5: "compute",
    6: "library",
    7: "raygeneration",
    8: "intersection",
    9: "anyhit",
    10: "closesthit",
    11: "miss",
    12: "callable",
    # RE Engine stores mesh/amplification shaders in the legacy hull/domain lanes.
    13: "mesh",
    14: "amplification",
}

RDEF_SHADER_TYPE_NAMES = {
    0x5053: "pixel",     # "SP" in little-endian storage (PS)
    0x5653: "vertex",
    0x4753: "geometry",
    0x4853: "hull",
    0x4453: "domain",
    0x4353: "compute",
}

COMPONENT_TYPE_NAMES = {
    0: "unknown",
    1: "uint32",
    2: "sint32",
    3: "float32",
}

SYSTEM_VALUE_NAMES = {
    0: "undefined",
    1: "position",
    2: "clip-distance",
    3: "cull-distance",
    4: "render-target-array-index",
    5: "viewport-array-index",
    6: "vertex-id",
    7: "primitive-id",
    8: "instance-id",
    9: "front-face",
    10: "sample-index",
    11: "final-quad-edge-tessfactor",
    12: "final-quad-inside-tessfactor",
    13: "final-tri-edge-tessfactor",
    14: "final-tri-inside-tessfactor",
    15: "final-line-detail-tessfactor",
    16: "final-line-density-tessfactor",
    23: "barycentrics",
    24: "shading-rate",
    25: "cull-primitive",
    64: "target",
    65: "depth",
    66: "coverage",
    67: "depth-greater-equal",
    68: "depth-less-equal",
}

RESOURCE_INPUT_TYPE_NAMES = {
    0: "constant-buffer",
    1: "texture-buffer",
    2: "texture",
    3: "sampler",
    4: "rw-typed",
    5: "structured-buffer",
    6: "rw-structured-buffer",
    7: "byte-address-buffer",
    8: "rw-byte-address-buffer",
    9: "append-structured-buffer",
    10: "consume-structured-buffer",
    11: "rw-structured-buffer-counter",
    12: "raytracing-acceleration-structure",
}

RESOURCE_RETURN_TYPE_NAMES = {
    0: "unknown",
    1: "unorm",
    2: "snorm",
    3: "sint",
    4: "uint",
    5: "float",
    6: "mixed",
    7: "double",
    8: "continued",
}

RESOURCE_DIMENSION_NAMES = {
    0: "unknown",
    1: "buffer",
    2: "texture1d",
    3: "texture1d-array",
    4: "texture2d",
    5: "texture2d-array",
    6: "texture2d-ms",
    7: "texture2d-ms-array",
    8: "texture3d",
    9: "texture-cube",
    10: "texture-cube-array",
    11: "buffer-extended",
}

CONSTANT_BUFFER_TYPE_NAMES = {
    0: "constant-buffer",
    1: "texture-buffer",
    2: "interface-pointers",
    3: "resource-bind-info",
}

VARIABLE_CLASS_NAMES = {
    0: "scalar",
    1: "vector",
    2: "matrix-rows",
    3: "matrix-columns",
    4: "object",
    5: "struct",
    6: "interface-class",
    7: "interface-pointer",
}

VARIABLE_TYPE_NAMES = {
    0: "void",
    1: "bool",
    2: "int",
    3: "float",
    4: "string",
    5: "texture",
    6: "texture1d",
    7: "texture2d",
    8: "texture3d",
    9: "texture-cube",
    10: "sampler",
    11: "sampler1d",
    12: "sampler2d",
    13: "sampler3d",
    14: "sampler-cube",
    15: "pixel-shader",
    16: "vertex-shader",
    17: "pixel-fragment",
    18: "vertex-fragment",
    19: "uint",
    20: "uint8",
    21: "geometry-shader",
    22: "rasterizer",
    23: "depth-stencil",
    24: "blend",
    25: "buffer",
    26: "constant-buffer",
    27: "texture-buffer",
    28: "texture1d-array",
    29: "texture2d-array",
    30: "render-target-view",
    31: "depth-stencil-view",
    32: "texture2d-ms",
    33: "texture2d-ms-array",
    34: "texture-cube-array",
    35: "hull-shader",
    36: "domain-shader",
    37: "interface-pointer",
    38: "compute-shader",
    39: "double",
    40: "rw-texture1d",
    41: "rw-texture1d-array",
    42: "rw-texture2d",
    43: "rw-texture2d-array",
    44: "rw-texture3d",
    45: "rw-buffer",
    46: "byte-address-buffer",
    47: "rw-byte-address-buffer",
    48: "structured-buffer",
    49: "rw-structured-buffer",
    50: "append-structured-buffer",
    51: "consume-structured-buffer",
    52: "min8float",
    53: "min10float",
    54: "min16float",
    55: "min12int",
    56: "min16int",
    57: "min16uint",
    58: "int16",
    59: "uint16",
    60: "float16",
    61: "int64",
    62: "uint64",
}

RDEF_DESCRIPTOR_ABI_NAMES = {
    0: "legacy",
    0x31314452: "shader-model-5.0 RD11",
    # Non-printable marker emitted by FXC for SM 5.1 RDEF.
    0x25441313: "shader-model-5.1",
}

SHADER_FEATURE_FLAG_NAMES = {
    0x00000001: "doubles",
    0x00000002: "early-depth-stencil",
    0x00000004: "UAVs-at-every-stage",
    0x00000008: "64-UAVs",
    0x00000010: "minimum-precision",
    0x00000020: "D3D11.1-double-extensions",
    0x00000040: "D3D11.1-shader-extensions",
    0x00000080: "level-9-comparison-filtering",
    0x00000100: "tiled-resources",
    0x00000200: "stencil-reference",
    0x00000400: "inner-coverage",
    0x00000800: "typed-UAV-load-additional-formats",
    0x00001000: "rasterizer-ordered-views",
    0x00002000: "viewport-and-RT-array-index-from-feeding-shader",
    0x00004000: "wave-operations",
    0x00008000: "int64-operations",
    0x00010000: "view-id",
    0x00020000: "barycentrics",
    0x00040000: "native-16-bit-operations",
    0x00080000: "shading-rate",
    0x00100000: "raytracing-tier-1.1",
    0x00200000: "sampler-feedback",
    0x00400000: "atomic-int64-on-typed-resource",
    0x00800000: "atomic-int64-on-group-shared",
    0x01000000: "derivatives-in-mesh-and-amplification-shaders",
    0x02000000: "resource-descriptor-heap-indexing",
    0x04000000: "sampler-descriptor-heap-indexing",
    0x08000000: "wave-matrix-multiply-accumulate",
    0x10000000: "atomic-int64-on-descriptor-heap-resource",
    0x20000000: "advanced-texture-operations",
    0x40000000: "writable-MSAA-textures",
    0x80000000: "sample-compare-gradient-or-bias",
    0x100000000: "extended-command-info",
}

SHADER_STATISTIC_NAMES = (
    "instruction-count",
    "temporary-register-count",
    "constant-definition-count",
    "declaration-count",
    "float-instruction-count",
    "integer-instruction-count",
    "unsigned-integer-instruction-count",
    "static-flow-control-count",
    "dynamic-flow-control-count",
    "macro-instruction-count",
    "temporary-array-count",
    "array-instruction-count",
    "cut-instruction-count",
    "emit-instruction-count",
    "texture-sample-count",
    "texture-load-count",
    "texture-compare-sample-count",
    "texture-bias-sample-count",
    "texture-gradient-sample-count",
    "move-instruction-count",
    "conditional-move-instruction-count",
    "type-conversion-instruction-count",
    "bitwise-instruction-count",
    "input-primitive",
    "geometry-output-topology",
    "geometry-maximum-output-vertex-count",
    "texture-gather-count",
    "texture-LOD-count",
    "sample-frequency",
    "geometry-instance-count",
    "control-point-count",
    "hull-output-primitive",
    "hull-partitioning",
    "tessellator-domain",
    "barrier-instruction-count",
    "atomic-instruction-count",
    "texture-store-instruction-count",
)

PRIMITIVE_NAMES = {
    0: "undefined",
    1: "point",
    2: "line",
    3: "triangle",
    6: "line-with-adjacency",
    7: "triangle-with-adjacency",
}
for _control_points in range(1, 33):
    PRIMITIVE_NAMES[7 + _control_points] = f"patch-{_control_points}-control-points"

PRIMITIVE_TOPOLOGY_NAMES = {
    0: "undefined",
    1: "point-list",
    2: "line-list",
    3: "line-strip",
    4: "triangle-list",
    5: "triangle-strip",
    10: "line-list-adjacency",
    11: "line-strip-adjacency",
    12: "triangle-list-adjacency",
    13: "triangle-strip-adjacency",
}
for _control_points in range(1, 33):
    PRIMITIVE_TOPOLOGY_NAMES[32 + _control_points] = f"patch-list-{_control_points}"

HULL_OUTPUT_PRIMITIVE_NAMES = {
    0: "undefined",
    1: "point",
    2: "line",
    3: "triangle-clockwise",
    4: "triangle-counter-clockwise",
}
HULL_PARTITIONING_NAMES = {
    0: "undefined",
    1: "integer",
    2: "power-of-two",
    3: "fractional-odd",
    4: "fractional-even",
}
TESSELLATOR_DOMAIN_NAMES = {
    0: "undefined",
    1: "isoline",
    2: "triangle",
    3: "quad",
}


class DxbcFormatError(ValueError):
    pass


class DxbcDisassemblyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DxbcChunk:
    tag: str
    offset: int
    payload_offset: int
    size: int
    raw: bytes


@dataclass(frozen=True, slots=True)
class DxbcSignatureParameter:
    semantic_name: str
    semantic_index: int
    register: int
    system_value_id: int
    system_value: str
    component_type_id: int
    component_type: str
    mask: int
    read_write_mask: int
    stream: int
    minimum_precision: int

    @property
    def components(self) -> str:
        return "".join(c for bit, c in enumerate("xyzw") if self.mask & (1 << bit))


@dataclass(frozen=True, slots=True)
class DxbcTypeMember:
    name_offset: int
    name: str
    type_offset: int
    byte_offset: int
    variable_type: "DxbcType"
    extended_data: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DxbcType:
    offset: int
    variable_class_id: int
    variable_class: str
    variable_type_id: int
    variable_type: str
    rows: int
    columns: int
    element_count: int
    member_count: int
    members_offset: int
    name_offset: int
    name: str
    reserved_data: tuple[int, ...]
    extended_data: tuple[int, ...]
    members: tuple[DxbcTypeMember, ...]

    @property
    def array_length(self) -> int:
        return max(1, self.element_count)

    @property
    def display_name(self) -> str:
        suffix = f"[{self.element_count}]" if self.element_count else ""
        return f"{self.name or self.variable_type}{suffix}"


@dataclass(frozen=True, slots=True)
class DxbcVariable:
    name: str
    start_offset: int
    size: int
    flags: int
    type_offset: int
    variable_type: DxbcType
    default_value_offset: int
    default_value: bytes | None
    start_texture: int
    texture_size: int
    start_sampler: int
    sampler_size: int
    extended_data: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DxbcConstantBuffer:
    name: str
    variable_count: int
    byte_size: int
    flags: int
    buffer_type_id: int
    buffer_type: str
    variables: tuple[DxbcVariable, ...]
    extended_data: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DxbcResourceBinding:
    name: str
    input_type_id: int
    input_type: str
    return_type_id: int
    return_type: str
    dimension_id: int
    dimension: str
    sample_count: int
    bind_point: int
    bind_count: int
    flags: int
    register_space: int
    resource_id: int
    extended_data: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DxbcLibraryFunction:
    name: str
    stage: str
    resources: tuple[DxbcResourceBinding, ...]
    constant_buffers: tuple[DxbcConstantBuffer, ...]
    linkage_name: str = ""


@dataclass(frozen=True, slots=True)
class DxbcReflection:
    shader_type_id: int
    shader_type: str
    major_version: int
    minor_version: int
    flags: int
    creator: str
    descriptor_tag: int
    descriptor_abi: str
    header_size: int
    constant_buffer_stride: int
    resource_stride: int
    variable_stride: int
    type_stride: int
    type_member_stride: int
    header_reserved: int
    constant_buffers: tuple[DxbcConstantBuffer, ...]
    resources: tuple[DxbcResourceBinding, ...]
    types: tuple[DxbcType, ...]
    thread_group_size: tuple[int, int, int] | None = None
    library_functions: tuple[DxbcLibraryFunction, ...] = ()


@dataclass(frozen=True, slots=True)
class DxbcStatistic:
    index: int
    name: str
    value: int
    value_name: str = ""

    @property
    def display_value(self) -> str:
        return f"{self.value} ({self.value_name})" if self.value_name else str(self.value)


@dataclass(frozen=True, slots=True)
class DxbcShader:
    source_offset: int
    size: int
    checksum: bytes
    checksum_hex: str
    container_version: int
    total_size: int
    sha256: str
    chunks: tuple[DxbcChunk, ...]
    stage: str
    model_major: int
    model_minor: int
    input_signature: tuple[DxbcSignatureParameter, ...]
    output_signature: tuple[DxbcSignatureParameter, ...]
    patch_signature: tuple[DxbcSignatureParameter, ...]
    reflection: DxbcReflection | None
    statistics: tuple[DxbcStatistic, ...]
    feature_flags: int
    features: tuple[str, ...]
    unknown_feature_flags: int


def _parse_signature(
    reader: BoundedReader,
    chunk: DxbcChunk,
) -> tuple[DxbcSignatureParameter, ...]:
    start = chunk.payload_offset
    end = start + chunk.size
    reader.require(start, 8, f"{chunk.tag} header", end=end)
    count = reader.u32(start, f"{chunk.tag} count", end=end)

    if chunk.tag == "OSG5":
        stride, stream_prefix, has_min_precision = 28, True, False
    elif chunk.tag.endswith("1"):
        stride, stream_prefix, has_min_precision = 32, True, True
    else:
        stride, stream_prefix, has_min_precision = 24, False, False
    entries = start + 8
    reader.require(entries, count * stride, f"{chunk.tag} entries", end=end)

    result: list[DxbcSignatureParameter] = []
    for index in range(count):
        entry = entries + index * stride
        stream = reader.u32(entry, f"{chunk.tag}[{index}] stream", end=end) if stream_prefix else 0
        base = entry + (4 if stream_prefix else 0)
        name_offset = reader.u32(base, f"{chunk.tag}[{index}] name", end=end)
        semantic_index = reader.u32(base + 4, f"{chunk.tag}[{index}] semantic index", end=end)
        system_value_id = reader.u32(base + 8, f"{chunk.tag}[{index}] system value", end=end)
        component_type_id = reader.u32(base + 12, f"{chunk.tag}[{index}] component type", end=end)
        register = reader.u32(base + 16, f"{chunk.tag}[{index}] register", end=end)
        mask = reader.u8(base + 20, f"{chunk.tag}[{index}] mask", end=end)
        read_write_mask = reader.u8(base + 21, f"{chunk.tag}[{index}] read/write mask", end=end)
        minimum_precision = (
            reader.u16(base + 24, f"{chunk.tag}[{index}] minimum precision", end=end)
            if has_min_precision
            else 0
        )
        result.append(
            DxbcSignatureParameter(
                semantic_name=reader.string(
                    start + name_offset,
                    f"{chunk.tag}[{index}] semantic name",
                    start=start,
                    end=end,
                ),
                semantic_index=semantic_index,
                register=register,
                system_value_id=system_value_id,
                system_value=SYSTEM_VALUE_NAMES.get(system_value_id, f"value-{system_value_id}"),
                component_type_id=component_type_id,
                component_type=COMPONENT_TYPE_NAMES.get(
                    component_type_id,
                    f"component-{component_type_id}",
                ),
                mask=mask,
                read_write_mask=read_write_mask,
                stream=stream,
                minimum_precision=minimum_precision,
            )
        )
    return tuple(result)


def _parse_rdef(reader: BoundedReader, chunk: DxbcChunk) -> DxbcReflection:
    start = chunk.payload_offset
    end = start + chunk.size
    reader.require(start, 28, "RDEF header", end=end)
    constant_buffer_count = reader.u32(start, "RDEF constant-buffer count", end=end)
    constant_buffer_offset = reader.u32(start + 4, "RDEF constant-buffer offset", end=end)
    resource_count = reader.u32(start + 8, "RDEF resource count", end=end)
    resource_offset = reader.u32(start + 12, "RDEF resource offset", end=end)
    minor_version = reader.u8(start + 16, "RDEF minor version", end=end)
    major_version = reader.u8(start + 17, "RDEF major version", end=end)
    shader_type_id = reader.u16(start + 18, "RDEF shader type", end=end)
    flags = reader.u32(start + 20, "RDEF flags", end=end)
    creator_offset = reader.u32(start + 24, "RDEF creator offset", end=end)

    # SM 5.1 RDEF replaces the RD11 marker and grows the resource stride from 0x20 to 0x28.
    descriptor_tag = 0
    header_size = 28
    constant_buffer_stride = 24
    resource_stride = 32
    variable_stride = 40
    type_stride = 36
    type_member_stride = 12
    header_reserved = 0
    if chunk.size >= 60:
        descriptor_tag = reader.u32(start + 28, "RDEF descriptor tag", end=end)
        (
            header_size,
            constant_buffer_stride,
            resource_stride,
            variable_stride,
            type_stride,
            type_member_stride,
            header_reserved,
        ) = struct.unpack_from("<7I", reader.data, start + 32)
        if header_size < 28 or header_size > chunk.size:
            raise DxbcFormatError(f"{reader.label}: invalid RDEF header size {header_size}")
        if (
            constant_buffer_stride < 24
            or resource_stride < 32
            or variable_stride < 40
            or type_stride < 16
            or type_member_stride < 12
        ):
            raise DxbcFormatError(
                f"{reader.label}: invalid RDEF descriptor strides "
                f"{constant_buffer_stride}/{resource_stride}/{variable_stride}/"
                f"{type_stride}/{type_member_stride}"
            )

    type_cache: dict[int, DxbcType] = {}
    type_in_progress: set[int] = set()

    def parse_type(type_offset: int) -> DxbcType:
        cached = type_cache.get(type_offset)
        if cached is not None:
            return cached
        if type_offset in type_in_progress:
            raise DxbcFormatError(
                f"{reader.label}: cyclic RDEF type reference at 0x{type_offset:X}"
            )
        type_in_progress.add(type_offset)
        try:
            entry = start + type_offset
            reader.require(entry, type_stride, f"RDEF type 0x{type_offset:X}", end=end)
            (
                variable_class_id,
                variable_type_id,
                rows,
                columns,
                element_count,
                member_count,
                members_offset,
            ) = struct.unpack_from("<6HI", reader.data, entry)

            name_offset = 0
            descriptor_extra: tuple[int, ...] = ()
            if type_stride >= 20:
                name_offset = reader.u32(
                    entry + type_stride - 4,
                    f"RDEF type 0x{type_offset:X} name",
                    end=end,
                )
                extra_count = (type_stride - 20) // 4
                if extra_count:
                    descriptor_extra = struct.unpack_from(
                        f"<{extra_count}I",
                        reader.data,
                        entry + 16,
                    )
            reserved_data = descriptor_extra[:4]
            extended_data = descriptor_extra[4:]
            name = (
                reader.string(
                    start + name_offset,
                    f"RDEF type 0x{type_offset:X} name",
                    start=start,
                    end=end,
                )
                if name_offset
                else VARIABLE_TYPE_NAMES.get(variable_type_id, f"type-{variable_type_id}")
            )

            members: list[DxbcTypeMember] = []
            if member_count:
                if not members_offset:
                    raise DxbcFormatError(
                        f"{reader.label}: RDEF type 0x{type_offset:X} has members but no table"
                    )
                member_entries = start + members_offset
                reader.require(
                    member_entries,
                    member_count * type_member_stride,
                    f"RDEF type 0x{type_offset:X} members",
                    end=end,
                )
                for member_index in range(member_count):
                    member = member_entries + member_index * type_member_stride
                    member_name, member_type, member_byte_offset = struct.unpack_from(
                        "<3I",
                        reader.data,
                        member,
                    )
                    member_extra = (
                        struct.unpack_from(
                            f"<{(type_member_stride - 12) // 4}I",
                            reader.data,
                            member + 12,
                        )
                        if type_member_stride > 12
                        else ()
                    )
                    members.append(
                        DxbcTypeMember(
                            name_offset=member_name,
                            name=reader.string(
                                start + member_name,
                                f"RDEF type 0x{type_offset:X} member {member_index} name",
                                start=start,
                                end=end,
                            ),
                            type_offset=member_type,
                            byte_offset=member_byte_offset,
                            variable_type=parse_type(member_type),
                            extended_data=member_extra,
                        )
                    )

            parsed = DxbcType(
                offset=type_offset,
                variable_class_id=variable_class_id,
                variable_class=VARIABLE_CLASS_NAMES.get(
                    variable_class_id,
                    f"class-{variable_class_id}",
                ),
                variable_type_id=variable_type_id,
                variable_type=VARIABLE_TYPE_NAMES.get(
                    variable_type_id,
                    f"type-{variable_type_id}",
                ),
                rows=rows,
                columns=columns,
                element_count=element_count,
                member_count=member_count,
                members_offset=members_offset,
                name_offset=name_offset,
                name=name,
                reserved_data=reserved_data,
                extended_data=extended_data,
                members=tuple(members),
            )
            type_cache[type_offset] = parsed
            return parsed
        finally:
            type_in_progress.remove(type_offset)

    resources: list[DxbcResourceBinding] = []
    resource_entries = start + resource_offset
    reader.require(resource_entries, resource_count * resource_stride, "RDEF resources", end=end)
    for index in range(resource_count):
        entry = resource_entries + index * resource_stride
        (
            name_offset,
            input_type,
            return_type,
            dimension,
            samples,
            bind_point,
            bind_count,
            resource_flags,
        ) = struct.unpack_from("<8I", reader.data, entry)
        extended_data = (
            struct.unpack_from(
                f"<{(resource_stride - 32) // 4}I",
                reader.data,
                entry + 32,
            )
            if resource_stride > 32
            else ()
        )
        register_space = extended_data[0] if len(extended_data) >= 1 else 0
        resource_id = extended_data[1] if len(extended_data) >= 2 else 0
        resources.append(
            DxbcResourceBinding(
                name=reader.string(start + name_offset, f"RDEF resource {index} name", start=start, end=end),
                input_type_id=input_type,
                input_type=RESOURCE_INPUT_TYPE_NAMES.get(input_type, f"input-{input_type}"),
                return_type_id=return_type,
                return_type=RESOURCE_RETURN_TYPE_NAMES.get(return_type, f"return-{return_type}"),
                dimension_id=dimension,
                dimension=RESOURCE_DIMENSION_NAMES.get(dimension, f"dimension-{dimension}"),
                sample_count=samples,
                bind_point=bind_point,
                bind_count=bind_count,
                flags=resource_flags,
                register_space=register_space,
                resource_id=resource_id,
                extended_data=extended_data,
            )
        )

    constant_buffers: list[DxbcConstantBuffer] = []
    buffer_entries = start + constant_buffer_offset
    reader.require(
        buffer_entries,
        constant_buffer_count * constant_buffer_stride,
        "RDEF constant buffers",
        end=end,
    )
    for index in range(constant_buffer_count):
        entry = buffer_entries + index * constant_buffer_stride
        name_offset, variable_count, variable_offset, byte_size, buffer_flags, buffer_type = struct.unpack_from(
            "<6I", reader.data, entry
        )
        buffer_extended_data = (
            struct.unpack_from(
                f"<{(constant_buffer_stride - 24) // 4}I",
                reader.data,
                entry + 24,
            )
            if constant_buffer_stride > 24
            else ()
        )
        variables: list[DxbcVariable] = []
        variable_entries = start + variable_offset
        reader.require(
            variable_entries,
            variable_count * variable_stride,
            f"RDEF buffer {index} variables",
            end=end,
        )
        for variable_index in range(variable_count):
            variable = variable_entries + variable_index * variable_stride
            (
                variable_name,
                variable_start,
                variable_size,
                variable_flags,
                type_offset,
                default_value_offset,
                start_texture,
                texture_size,
                start_sampler,
                sampler_size,
            ) = struct.unpack_from("<10I", reader.data, variable)
            variable_extended_data = (
                struct.unpack_from(
                    f"<{(variable_stride - 40) // 4}I",
                    reader.data,
                    variable + 40,
                )
                if variable_stride > 40
                else ()
            )
            variable_type = parse_type(type_offset)
            default_value = None
            if default_value_offset:
                default_start = start + default_value_offset
                reader.require(
                    default_start,
                    variable_size,
                    f"RDEF buffer {index} variable {variable_index} default value",
                    end=end,
                )
                default_value = reader.data[
                    default_start : default_start + variable_size
                ].tobytes()
            variables.append(
                DxbcVariable(
                    name=reader.string(
                        start + variable_name,
                        f"RDEF buffer {index} variable {variable_index} name",
                        start=start,
                        end=end,
                    ),
                    start_offset=variable_start,
                    size=variable_size,
                    flags=variable_flags,
                    type_offset=type_offset,
                    variable_type=variable_type,
                    default_value_offset=default_value_offset,
                    default_value=default_value,
                    start_texture=start_texture,
                    texture_size=texture_size,
                    start_sampler=start_sampler,
                    sampler_size=sampler_size,
                    extended_data=variable_extended_data,
                )
            )
        constant_buffers.append(
            DxbcConstantBuffer(
                name=reader.string(start + name_offset, f"RDEF buffer {index} name", start=start, end=end),
                variable_count=variable_count,
                byte_size=byte_size,
                flags=buffer_flags,
                buffer_type_id=buffer_type,
                buffer_type=CONSTANT_BUFFER_TYPE_NAMES.get(buffer_type, f"buffer-{buffer_type}"),
                variables=tuple(variables),
                extended_data=buffer_extended_data,
            )
        )

    creator = reader.string(start + creator_offset, "RDEF creator", start=start, end=end) if creator_offset else ""
    return DxbcReflection(
        shader_type_id=shader_type_id,
        shader_type=RDEF_SHADER_TYPE_NAMES.get(
            shader_type_id,
            SHADER_STAGE_NAMES.get(shader_type_id, f"stage-{shader_type_id}"),
        ),
        major_version=major_version,
        minor_version=minor_version,
        flags=flags,
        creator=creator,
        descriptor_tag=descriptor_tag,
        descriptor_abi=RDEF_DESCRIPTOR_ABI_NAMES.get(
            descriptor_tag,
            f"unrecognized-0x{descriptor_tag:08X}",
        ),
        header_size=header_size,
        constant_buffer_stride=constant_buffer_stride,
        resource_stride=resource_stride,
        variable_stride=variable_stride,
        type_stride=type_stride,
        type_member_stride=type_member_stride,
        header_reserved=header_reserved,
        constant_buffers=tuple(constant_buffers),
        resources=tuple(resources),
        types=tuple(type_cache[offset] for offset in sorted(type_cache)),
    )


def _statistic_value_name(name: str, value: int) -> str:
    if name == "input-primitive":
        return PRIMITIVE_NAMES.get(value, f"primitive-{value}")
    if name == "geometry-output-topology":
        return PRIMITIVE_TOPOLOGY_NAMES.get(value, f"topology-{value}")
    if name == "hull-output-primitive":
        return HULL_OUTPUT_PRIMITIVE_NAMES.get(value, f"output-{value}")
    if name == "hull-partitioning":
        return HULL_PARTITIONING_NAMES.get(value, f"partitioning-{value}")
    if name == "tessellator-domain":
        return TESSELLATOR_DOMAIN_NAMES.get(value, f"domain-{value}")
    if name == "sample-frequency":
        return "enabled" if value else "disabled"
    return ""


def _read_dxbc_directory(
    reader: BoundedReader,
    offset: int,
    size: int,
) -> tuple[bytes, int, int, int, tuple[DxbcChunk, ...]]:
    reader.require(offset, size, "container")
    supplied_end = offset + size
    reader.require(offset, 32, "header", end=supplied_end)
    if reader.data[offset : offset + 4].tobytes() != DXBC_MAGIC:
        raise DxbcFormatError(f"{reader.label}: invalid DXBC magic at 0x{offset:X}")

    checksum = reader.data[offset + 4 : offset + 20].tobytes()
    container_version = reader.u32(offset + 20, "container version", end=supplied_end)
    total_size = reader.u32(offset + 24, "total size", end=supplied_end)
    chunk_count = reader.u32(offset + 28, "chunk count", end=supplied_end)
    if total_size < 32 + chunk_count * 4 or total_size > size:
        raise DxbcFormatError(
            f"{reader.label}: invalid total size {total_size} for {size} bytes"
        )
    end = offset + total_size
    reader.require(offset + 32, chunk_count * 4, "chunk offsets", end=end)

    chunks: list[DxbcChunk] = []
    seen_offsets: set[int] = set()
    for index in range(chunk_count):
        relative = reader.u32(offset + 32 + index * 4, f"chunk {index} offset", end=end)
        chunk_offset = offset + relative
        if chunk_offset in seen_offsets:
            raise DxbcFormatError(
                f"{reader.label}: duplicate chunk offset 0x{relative:X}"
            )
        seen_offsets.add(chunk_offset)
        reader.require(chunk_offset, 8, f"chunk {index} header", end=end)
        tag_bytes = reader.data[chunk_offset : chunk_offset + 4].tobytes()
        tag = tag_bytes.decode("ascii", "replace")
        chunk_size = reader.u32(chunk_offset + 4, f"{tag} size", end=end)
        payload = chunk_offset + 8
        reader.require(payload, chunk_size, f"{tag} payload", end=end)
        chunks.append(
            DxbcChunk(
                tag=tag,
                offset=chunk_offset,
                payload_offset=payload,
                size=chunk_size,
                raw=reader.data[payload : payload + chunk_size].tobytes(),
            )
        )
    return checksum, container_version, total_size, end, tuple(chunks)


def _joined_signature(parts: list[tuple[DxbcSignatureParameter, ...]]) -> tuple[DxbcSignatureParameter, ...]:
    if len(parts) == 1:
        return parts[0]
    return tuple(parameter for part in parts for parameter in part)


def parse_dxbc(
    data: bytes | bytearray | memoryview,
    *,
    offset: int = 0,
    size: int | None = None,
    label: str = "DXBC",
    _signature_cache: dict[
        tuple[str, bytes], tuple[DxbcSignatureParameter, ...]
    ] | None = None,
) -> DxbcShader:
    reader = BoundedReader(data, label, DxbcFormatError)
    if size is None:
        size = len(reader.data) - offset
    checksum, container_version, total_size, end, chunks = _read_dxbc_directory(
        reader,
        offset,
        size,
    )

    input_signature: list[tuple[DxbcSignatureParameter, ...]] = []
    output_signature: list[tuple[DxbcSignatureParameter, ...]] = []
    patch_signature: list[tuple[DxbcSignatureParameter, ...]] = []
    reflection = None
    statistics: tuple[DxbcStatistic, ...] = ()
    feature_flags = 0
    stage = "unknown"
    model_major = model_minor = 0
    is_dxil = any(chunk.tag == "DXIL" for chunk in chunks)
    signature_tags = {"ISGN", "OSGN", "PCSG", "ISG1", "OSG1", "PSG1", "OSG5"}
    for chunk in chunks:
        if chunk.tag in signature_tags:
            key = (chunk.tag, chunk.raw)
            parameters = _signature_cache.get(key) if _signature_cache is not None else None
            if parameters is None:
                parameters = _parse_signature(reader, chunk)
                if _signature_cache is not None:
                    _signature_cache[key] = parameters
            if chunk.tag.startswith("I"):
                input_signature.append(parameters)
            elif chunk.tag.startswith("P"):
                patch_signature.append(parameters)
            else:
                output_signature.append(parameters)
        elif chunk.tag == "RDEF":
            reflection = _parse_rdef(reader, chunk)
        elif chunk.tag in {"SHDR", "SHEX"} and chunk.size >= 4:
            token = reader.u32(chunk.payload_offset, f"{chunk.tag} version token", end=chunk.payload_offset + chunk.size)
            stage_id = (token >> 16) & 0xFFFF
            stage = SHADER_STAGE_NAMES.get(stage_id, f"stage-{stage_id}")
            model_major = (token >> 4) & 0xF
            model_minor = token & 0xF
        elif chunk.tag == "DXIL":
            if chunk.size < 24 or chunk.size % 4:
                raise DxbcFormatError(
                    f"{label}: DXIL program chunk has invalid size {chunk.size}"
                )
            limit = chunk.payload_offset + chunk.size
            program_token = reader.u32(
                chunk.payload_offset,
                "DXIL program version token",
                end=limit,
            )
            declared_words = reader.u32(
                chunk.payload_offset + 4,
                "DXIL program word count",
                end=limit,
            )
            if declared_words * 4 != chunk.size:
                raise DxbcFormatError(
                    f"{label}: DXIL program declares {declared_words * 4} bytes, "
                    f"container stores {chunk.size}"
                )
            if reader.bytes(
                chunk.payload_offset + 8,
                4,
                "DXIL program magic",
                end=limit,
            ) != b"DXIL":
                raise DxbcFormatError(f"{label}: invalid DXIL program magic")
            bitcode_relative = reader.u32(
                chunk.payload_offset + 16,
                "DXIL bitcode offset",
                end=limit,
            )
            bitcode_size = reader.u32(
                chunk.payload_offset + 20,
                "DXIL bitcode size",
                end=limit,
            )
            # Offset from the start of the four-dword DXIL bitcode header at payload + 8.
            bitcode_offset = chunk.payload_offset + 8 + bitcode_relative
            reader.require(
                bitcode_offset,
                bitcode_size,
                "DXIL LLVM bitcode",
                end=limit,
            )
            if bitcode_offset + bitcode_size != limit:
                raise DxbcFormatError(
                    f"{label}: DXIL bitcode does not end at its program boundary"
                )
            if reader.bytes(
                bitcode_offset,
                4,
                "DXIL LLVM bitcode magic",
                end=limit,
            ) != b"BC\xC0\xDE":
                raise DxbcFormatError(f"{label}: invalid DXIL LLVM bitcode magic")
            stage_id = (program_token >> 16) & 0xFFFF
            stage = SHADER_STAGE_NAMES.get(stage_id, f"stage-{stage_id}")
            shader_model = program_token & 0xFFFF
            model_major = (shader_model >> 4) & 0xF
            model_minor = shader_model & 0xF
        elif chunk.tag == "STAT" and not is_dxil and chunk.size % 4 == 0:
            # DXIL's STAT is a reflection bitcode program, not the SM4/5 counter table.
            values = struct.unpack_from(
                f"<{chunk.size // 4}I",
                reader.data,
                chunk.payload_offset,
            )
            names = [SHADER_STATISTIC_NAMES[index] if index < len(SHADER_STATISTIC_NAMES) else f"statistic-{index}"
                     for index in range(len(values))]
            statistics = tuple(DxbcStatistic(index, name, value, _statistic_value_name(name, value))
                               for index, (name, value) in enumerate(zip(names, values)))
        elif chunk.tag == "SFI0":
            if chunk.size != 8:
                raise DxbcFormatError(
                    f"{label}: SFI0 feature-info chunk has {chunk.size} bytes, expected 8"
                )
            feature_flags = struct.unpack_from("<Q", reader.data, chunk.payload_offset)[0]

    if stage == "unknown" and reflection is not None:
        stage = reflection.shader_type
        model_major = reflection.major_version
        model_minor = reflection.minor_version

    raw = reader.data[offset:end].tobytes()
    known_feature_mask = sum(SHADER_FEATURE_FLAG_NAMES)
    return DxbcShader(
        source_offset=offset,
        size=size,
        checksum=checksum,
        checksum_hex=checksum.hex(),
        container_version=container_version,
        total_size=total_size,
        sha256=hashlib.sha256(raw).hexdigest(),
        chunks=tuple(chunks),
        stage=stage,
        model_major=model_major,
        model_minor=model_minor,
        input_signature=_joined_signature(input_signature),
        output_signature=_joined_signature(output_signature),
        patch_signature=_joined_signature(patch_signature),
        reflection=reflection,
        statistics=statistics,
        feature_flags=feature_flags,
        features=tuple(
            name
            for flag, name in SHADER_FEATURE_FLAG_NAMES.items()
            if feature_flags & flag
        ),
        unknown_feature_flags=feature_flags & ~known_feature_mask,
    )


def disassemble_dxbc(
    data: bytes | bytearray | memoryview,
    *,
    hex_literals: bool = False,
) -> str:
    raw = bytes(data)
    if len(raw) < 4 or raw[:4] != DXBC_MAGIC:
        raise DxbcDisassemblyError("input is not a DXBC container")
    if os.name != "nt":
        raise DxbcDisassemblyError("DXBC disassembly requires Windows D3DCompiler")

    try:
        compiler = _compiler()
    except HlslCompileError as exc:
        raise DxbcDisassemblyError(str(exc)) from exc
    flags = 0x4 | 0x20  # D3D_DISASM_ENABLE_INSTRUCTION_NUMBERING | D3D_DISASM_ENABLE_INSTRUCTION_OFFSET
    if hex_literals:
        # Decimal disassembly is lossy for float immediates.
        flags |= 0x80  # D3D_DISASM_PRINT_HEX_LITERALS (d3dcompiler.h).

    function = compiler.D3DDisassemble
    function.argtypes = (
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    function.restype = ctypes.c_long
    source = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    blob = ctypes.c_void_p()
    result = function(source, len(raw), flags, None, ctypes.byref(blob))
    if result < 0 or not blob.value:
        raise DxbcDisassemblyError(f"D3DDisassemble failed with HRESULT 0x{result & 0xFFFFFFFF:08X}")

    try:
        text = blob_bytes(blob)
    finally:
        com_release(blob)
    if not text:
        raise DxbcDisassemblyError("D3DDisassemble returned an empty blob")
    return text.rstrip(b"\x00").decode("utf-8", "replace")


__all__ = [
    *[name for name in globals() if name.startswith("Dxbc") or name.endswith("_NAMES")],
    "disassemble_dxbc",
    "parse_dxbc",
]
