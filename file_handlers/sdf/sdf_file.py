from __future__ import annotations

from functools import partial
from pathlib import Path
import struct
import threading

lazy from utils.hex_util import align
lazy from utils.resource_file_utils import resource_version_from_path

lazy from . import dxc, raytracing
lazy from ._ownership import discover_detached_variable_tables
lazy from .binary_reader import BoundedReader
lazy from .dxbc import (
    DxbcSignatureParameter,
    DxbcShader,
    parse_dxbc,
)
lazy from .models import (
    RESOURCE_KINDS,
    STAGE_KEYS,
    SDF_MAGIC,
    SdfBindingDescriptor,
    SdfBlendState,
    SdfBlendTarget,
    SdfCompactDeclaration,
    SdfConstantBuffer,
    SdfConstantVariable,
    SdfDepthStencilFace,
    SdfDepthStencilState,
    SdfDiscardedAuthoringRange,
    SdfDocument,
    SdfHeader,
    SdfInputElement,
    SdfInputLayout,
    LazyPrograms,
    SdfNamedResource,
    SdfProgram,
    SdfProgramTables,
    SdfRasterizerState,
    SdfSemanticNote,
    SdfShaderReference,
    SdfSpirvInterfaceVariable,
    SdfSpirvInputElement,
    SdfSpirvInputSignature,
    SdfSpirvModule,
    SdfTarget,
    program_names,
)
lazy from .profiles import SdfGameProfile, sdf_profile_for_resource
lazy from .runtime_cache import attach_runtime_cache_keys
lazy from .spirv import _STAGES, SpirvError, _literal_string, _spirv_instructions, _spirv_module


class SdfFormatError(ValueError):
    pass


FILL_MODE_NAMES = {0: "undefined", 2: "wireframe", 3: "solid"}
CULL_MODE_NAMES = {0: "undefined", 1: "none", 2: "front", 3: "back"}
BLEND_NAMES = {
    0: "undefined",
    1: "zero",
    2: "one",
    3: "source-color",
    4: "inverse-source-color",
    5: "source-alpha",
    6: "inverse-source-alpha",
    7: "destination-alpha",
    8: "inverse-destination-alpha",
    9: "destination-color",
    10: "inverse-destination-color",
    11: "source-alpha-saturate",
    14: "blend-factor",
    15: "inverse-blend-factor",
    16: "source1-color",
    17: "inverse-source1-color",
    18: "source1-alpha",
    19: "inverse-source1-alpha",
}
BLEND_OPERATION_NAMES = {
    0: "undefined",
    1: "add",
    2: "subtract",
    3: "reverse-subtract",
    4: "minimum",
    5: "maximum",
}
DEPTH_WRITE_MASK_NAMES = {0: "zero", 1: "all"}
COMPARISON_FUNCTION_NAMES = {
    0: "undefined",
    1: "never",
    2: "less",
    3: "equal",
    4: "less-equal",
    5: "greater",
    6: "not-equal",
    7: "greater-equal",
    8: "always",
}
# Direct3D enum values of the ids above.
CULL_NONE, CULL_FRONT, CULL_BACK = 1, 2, 3
BLEND_ZERO, BLEND_ONE, BLEND_SOURCE_ALPHA, BLEND_INVERSE_SOURCE_ALPHA = 1, 2, 5, 6
BLEND_OPERATION_ADD = 1
(COMPARE_NEVER, COMPARE_LESS, COMPARE_EQUAL, COMPARE_LESS_EQUAL, COMPARE_GREATER, COMPARE_NOT_EQUAL,
 COMPARE_GREATER_EQUAL, COMPARE_ALWAYS) = range(1, 9)
STENCIL_OPERATION_NAMES = {
    0: "undefined",
    1: "keep",
    2: "zero",
    3: "replace",
    4: "increment-saturate",
    5: "decrement-saturate",
    6: "invert",
    7: "increment",
    8: "decrement",
}

def _enum(table: tuple[int, ...], index: int) -> int:
    return table[index] if 0 <= index < len(table) else 0


def write_channels(mask: int) -> str:
    return "".join(channel for bit, channel in enumerate("RGBA") if mask & (1 << bit)) or "none"


class _SdfParser:
    def __init__(
        self,
        data: bytes,
        profile: SdfGameProfile,
        label: str,
        *,
        eager_native_reflection: bool = True,
        runtime_cache_keys: bool = True,
        discover_detached_metadata: bool = True,
        lazy_programs: bool = False,
    ):
        self.data = data
        self.reader = BoundedReader(data, label, SdfFormatError)
        self.profile = profile
        self.eager_native_reflection = eager_native_reflection
        self.runtime_cache_keys = runtime_cache_keys
        self.discover_detached_metadata = discover_detached_metadata
        self.lazy_programs = lazy_programs
        self.records: list[list] = []
        self.targets: list[SdfTarget] = []
        self.signature_cache: dict[
            tuple[str, bytes], tuple[DxbcSignatureParameter, ...]
        ] = {}
        self.table_start = 0
        self.table_end = 0
        self.shader_binary_offset = 0
        self.shader_cache: dict[tuple[int, int], DxbcShader] = {}
        self.spirv_cache: dict[tuple[int, int, str], SdfSpirvModule] = {}
        self.input_layout_cache: dict[int, SdfInputLayout] = {}
        self.rasterizer_cache: dict[int, SdfRasterizerState] = {}
        self.blend_cache: dict[int, SdfBlendState] = {}
        self.depth_cache: dict[int, SdfDepthStencilState] = {}
        self.compact_cache: dict[tuple[int, int], tuple[SdfCompactDeclaration, ...]] = {}
        self.binding_cache: dict[tuple[str, int, int], tuple[tuple[SdfBindingDescriptor, ...], tuple[int, ...]]] = {}
        self.shader_payload_cache: dict[tuple[int, int], bytes] = {}
        self.shader_reference_cache: dict[tuple[str, int, int], SdfShaderReference] = {}
        self.cb_cache: dict[tuple[int, int], tuple[SdfConstantBuffer, ...]] = {}
        self.variable_cache: dict[tuple[int, int, int], tuple[SdfConstantVariable, ...]] = {}
        self.resource_cache: dict[tuple[str, int, int], tuple[SdfNamedResource, ...]] = {}
        self.notes: dict[tuple[str, str], SdfSemanticNote] = {}
        self.discarded_authoring_ranges: list[SdfDiscardedAuthoringRange] = []
        self.baseline_input_signatures: dict[
            int,
            tuple[DxbcSignatureParameter, ...],
        ] = {}

    def note(self, field: str, message: str, status: str = "unknown") -> None:
        self.notes.setdefault((field, message), SdfSemanticNote(field, message, status))

    def _validate_name_hash(self, name: str, value: int, field: str) -> None:
        hasher = self.profile.metadata_name_hash
        if hasher is None:
            raise SdfFormatError(
                f"{self.reader.label}: {self.profile.display_name} has no "
                "metadata-name hash contract"
            )
        expected = int(hasher(name)) & 0xFFFFFFFF
        if int(value) != expected:
            raise SdfFormatError(
                f"{self.reader.label}: {field} hash 0x{int(value):08X} does "
                f"not match {name!r} (expected 0x{expected:08X})"
            )

    def parse(self) -> SdfDocument:
        reader = self.reader
        reader.require(0, 8, "SDF header")
        if reader.bytes(0, 4, "magic") != SDF_MAGIC:
            raise SdfFormatError(f"{reader.label}: invalid SDF magic")
        target_count = reader.u16(4, "target count")
        if self.profile.header_layout == "uniform":
            reader.require(0, 16, "uniform SDF header")
            program_count = reader.u16(6, "program count")
            program_counts = (program_count,) * target_count
            self.shader_binary_offset = reader.u64(8, "shader-binary offset")
            self.table_start = 16
            if bool(target_count) != bool(program_count):
                raise SdfFormatError(
                    f"{reader.label}: half-empty target/program table "
                    f"({target_count} targets, {program_count} programs)"
                )
        else:
            counts_end = 6 + target_count * 2
            reader.require(6, target_count * 2, "per-target program counts")
            program_counts = tuple(
                reader.u16(6 + index * 2, f"target {index} program count")
                for index in range(target_count)
            )
            header_qword = align(counts_end, 8)
            reader.require(counts_end, header_qword - counts_end, "aligned header padding")
            if any(reader.data[counts_end:header_qword]):
                raise SdfFormatError(
                    f"{reader.label}: aligned per-target header padding is non-zero"
                )
            self.shader_binary_offset = reader.u64(
                header_qword,
                "shader-binary offset",
            )
            self.table_start = header_qword + 8
            program_count = program_counts[0] if program_counts else 0
            # Shipped placeholders declare every target with no programs.
            if sum(program_counts) and any(count <= 0 for count in program_counts):
                raise SdfFormatError(
                    f"{reader.label}: every declared target must own at least one program"
                )

        record_count = sum(program_counts)
        self.table_end = self.table_start + record_count * self.profile.record_size
        reader.require(
            self.table_start,
            record_count * self.profile.record_size,
            "program table",
        )
        if not self.table_end <= self.shader_binary_offset <= len(self.data):
            raise SdfFormatError(
                f"{reader.label}: shader-binary offset 0x{self.shader_binary_offset:X} "
                f"does not follow table end 0x{self.table_end:X}"
            )

        self.records = self._read_records()
        targets = self.targets
        lock = threading.RLock()
        record_base = 0
        for target_index, target_program_count in enumerate(program_counts):
            target_layout = self.profile.target(target_index)
            if self.lazy_programs:
                programs = LazyPrograms(
                    tuple(self._record_name(record_base + program_index)
                          for program_index in range(target_program_count)),
                    partial(self._target_tables, target_index, record_base),
                    partial(self._drawn_program, target_index, record_base),
                    lock,
                )
            else:
                programs = tuple(
                    self._parse_program(
                        target_index,
                        program_index,
                        record_base + program_index,
                    )
                    for program_index in range(target_program_count)
                )
            targets.append(
                SdfTarget(
                    index=target_index,
                    key=target_layout.key,
                    display_name=target_layout.display_name,
                    selection_capability_bit=target_layout.selection_capability_bit,
                    programs=programs,
                )
            )
            record_base += target_program_count

        if target_count == 0:
            # Some shipped files are 16-byte empty containers.
            self.note(
                "header.target_count",
                "file is an explicit empty SDF/VSDF container",
                "empty-container",
            )
        elif target_count != len(self.profile.targets):
            self.note(
                "header.target_count",
                f"file has {target_count} targets; profile names {len(self.profile.targets)}",
                "profile-mismatch",
            )
        self._validate_program_names(targets)
        view = memoryview(self.data)
        document = SdfDocument(
            profile=self.profile,
            header=SdfHeader(
                target_count,
                program_count,
                self.shader_binary_offset,
                self.table_start,
                program_counts,
            ),
            targets=tuple(targets),
            original_data=self.data,
            shader_blob=view[self.shader_binary_offset:],
            notes=tuple(self.notes.values()),
            # Programs to be drawn add the shaders they are first read with.
            unique_shaders=self.shader_cache,
            discarded_authoring_ranges=tuple(self.discarded_authoring_ranges),
        )
        if self.runtime_cache_keys:
            try:
                attach_runtime_cache_keys(document)
            except ValueError as exc:
                raise SdfFormatError(f"{reader.label}: {exc}") from exc
        if self.discover_detached_metadata:
            document.detached_variable_tables = discover_detached_variable_tables(document)
        return document

    def _validate_program_names(self, targets: list[SdfTarget]) -> None:
        if not targets:
            return
        baseline = program_names(targets[0])
        for target in targets[1:]:
            for program_index, (left, right) in enumerate(zip(baseline, program_names(target))):
                if left != right:
                    raise SdfFormatError(
                        f"{self.reader.label}: target {target.index} program "
                        f"{program_index} name {right!r} differs from "
                        f"baseline {left!r}"
                    )

    def _record_offset(self, record_index: int) -> int:
        return self.table_start + record_index * self.profile.record_size

    def _read_records(self) -> list[list]:
        layout, stages = self.profile.program_layout, self.profile.stages
        fields = [
            (layout.name_pointer_offset, "Q"),
            *((stage.pointer_offset, "Q") for stage in stages),
            *((stage.size_offset, "I") for stage in stages),
            (layout.total_stage_binding_count_offset, "I"),
            (layout.per_stage_binding_counts_offset, f"{len(RESOURCE_KINDS) * len(stages)}s"),
            *((offset, "B") for offset in layout.distinct_resource_count_offsets),
            *((offset, "Q") for offset in layout.resource_binding_pointer_offsets),
            *((offset, "Q") for offset in layout.resource_metadata_pointer_offsets),
        ]
        order = sorted(range(len(fields)), key=lambda index: fields[index][0])
        form, end = "<", 0
        for index in order:
            offset, code = fields[index]
            if offset < end:
                raise ValueError(f"{self.profile.display_name}: program record fields overlap at +0x{offset:X}")
            form += (f"{offset - end}x" if offset > end else "") + code
            end = offset + struct.calcsize("<" + code)
        if end > self.profile.record_size:
            raise ValueError(f"{self.profile.display_name}: program record fields end past the record")
        record = struct.Struct(form + f"{self.profile.record_size - end}x")
        where = [order.index(index) for index in range(len(fields))]
        return [[row[index] for index in where]
                for row in record.iter_unpack(self.data[self.table_start:self.table_end])]

    def _record_name(self, record_index: int) -> str:
        return self.reader.string(
            self.records[record_index][0],
            f"program {record_index} name",
            start=self.table_end,
            end=self.shader_binary_offset,
        )

    def _target_tables(self, target_index: int, record_base: int, program_index: int) -> SdfProgramTables:
        return self._program_tables(target_index, program_index, record_base + program_index)

    def _program_tables(self, target_index: int, program_index: int, record_index: int) -> SdfProgramTables:
        reader = self.reader
        stage_count = len(self.profile.stages)
        name_offset, *fields = self.records[record_index]
        pointers, sizes = fields[:stage_count], fields[stage_count:2 * stage_count]
        total_stage_bindings, counts, *fields = fields[2 * stage_count:]
        distinct_counts, binding_pointers, metadata_pointers = (
            tuple(fields[:5]), fields[5:10], fields[10:15])
        name = self._record_name(record_index)
        shaders = tuple(
            self._shader_reference(stage.key, pointer, size, name, target_index=target_index)
            for stage, pointer, size in zip(self.profile.stages, pointers, sizes)
        )
        per_stage_counts = tuple(
            tuple(counts[kind * stage_count:(kind + 1) * stage_count]) for kind in range(len(RESOURCE_KINDS))
        )
        computed_total = sum(sum(counts) for counts in per_stage_counts)
        if total_stage_bindings != computed_total:
            raise SdfFormatError(
                f"{reader.label}: {name} declares {total_stage_bindings} stage bindings, "
                f"but its per-stage counts sum to {computed_total}"
            )
        named_resources = tuple(
            self._parse_named_resources(RESOURCE_KINDS[kind], metadata_pointers[kind], distinct_counts[kind], name)
            for kind in range(1, 5)
        )
        return SdfProgramTables(
            target_index=target_index,
            program_index=program_index,
            record_index=record_index,
            name_offset=name_offset,
            name=name,
            shaders=shaders,
            total_stage_binding_count=total_stage_bindings,
            per_stage_binding_counts=per_stage_counts,
            distinct_resource_counts=distinct_counts,
            bindings=tuple(
                self._parse_bindings(RESOURCE_KINDS[kind], binding_pointers[kind], distinct_counts[kind],
                                     per_stage_counts[kind], name)
                for kind in range(5)
            ),
            constant_buffers=self._parse_constant_buffers(metadata_pointers[0], distinct_counts[0], name),
            samplers=named_resources[0],
            shader_resources=named_resources[1],
            unordered_access_resources=named_resources[2],
            optional_resources=named_resources[3],
        )

    def _drawn_program(self, target_index: int, record_base: int, program_index: int) -> SdfProgram:
        """A later target's program is read against the first target's input signature, so the first target's is loaded
        first."""
        if target_index:
            self.targets[0].programs[program_index]
        return self._parse_program(target_index, program_index, record_base + program_index)

    def _parse_program(self, target_index: int, program_index: int, record_index: int) -> SdfProgram:
        reader = self.reader
        layout = self.profile.program_layout
        record = self._record_offset(record_index)
        record_end = record + self.profile.record_size
        for relative_offset, size in self.profile.reserved_record_ranges:
            raw_reserved = reader.bytes(
                record + relative_offset,
                size,
                f"program {record_index} reserved record range",
                end=record_end,
            )
            if any(raw_reserved):
                raise SdfFormatError(
                    f"{reader.label}: program {record_index} reserved record range "
                    f"+0x{relative_offset:X} is non-zero"
                )
        tables = self._program_tables(target_index, program_index, record_index)
        name, shaders = tables.name, tables.shaders

        input_layout = self._parse_input_layout(
            reader.u64(
                record + layout.input_layout_pointer_offset,
                f"{name} input layout",
            ),
            name,
        )
        input_signature = self._shader_reference(
            "input_signature",
            reader.u64(
                record + layout.input_signature_pointer_offset,
                f"{name} input-signature offset",
                end=record_end,
            ),
            reader.u32(
                record + layout.input_signature_size_offset,
                f"{name} input-signature size",
                end=record_end,
            ),
            name,
            target_index=target_index,
            input_layout=input_layout,
            native_input_signature=(
                self.baseline_input_signatures.get(program_index)
                if target_index
                else None
            ),
        )
        if target_index == 0 and input_signature.reflection is not None:
            self.baseline_input_signatures[program_index] = tuple(
                parameter
                for parameter in input_signature.reflection.input_signature
                if parameter.system_value_id == 0
            )
        self._validate_spirv_vertex_interface(input_signature, shaders, name)

        compact_count = reader.u32(
            record + layout.compact_declaration_count_offset,
            f"{name} declaration count",
            end=record_end,
        )
        reserved_tail = reader.u8(
            record + layout.reserved_tail_offset,
            f"{name} reserved byte",
            end=record_end,
        )
        requirement_mask = sum(self.profile.program_requirement_names)
        unknown_requirement_bits = reserved_tail & ~requirement_mask
        if unknown_requirement_bits:
            raise SdfFormatError(
                f"{reader.label}: {name} has unsupported compact shader "
                f"requirement bits 0x{unknown_requirement_bits:02X}"
            )
        shader_requirements = tuple(
            requirement_name
            for bit, requirement_name in self.profile.program_requirement_names.items()
            if reserved_tail & bit
        )
        group_x, group_y, group_z = struct.unpack_from(
            "<3H",
            reader.data,
            record + layout.compute_group_size_offset,
        )
        topology = reader.u16(
            record + layout.primitive_topology_offset,
            f"{name} primitive topology",
            end=record_end,
        )
        compute = next(shader for shader in shaders if shader.role == "compute")
        compute_present = compute.present
        if compute_present and compute.raytracing_library is None and not (group_x and group_y and group_z):
            raise SdfFormatError(
                f"{reader.label}: {name} compute shader/group-size presence disagrees"
            )
        # The final qword is a union: topology in the high u16, or three compute u16 thread dimensions.
        if (not compute_present or compute.raytracing_library is not None) and (group_x or group_y or group_z):
            raise SdfFormatError(f"{reader.label}: {name} has unsampled graphics wave-limit/reserved bits")
        if compute_present and topology:
            raise SdfFormatError(f"{reader.label}: {name} has unsampled compute wave limit {topology}")
        if not compute_present and topology not in self.profile.primitive_topology_names:
            raise SdfFormatError(
                f"{reader.label}: {name} has unknown primitive topology {topology}"
            )

        rasterizer = self._parse_rasterizer(
            reader.u64(record + layout.rasterizer_state_pointer_offset, f"{name} rasterizer"),
            name,
        )
        blend = self._parse_blend(
            reader.u64(record + layout.blend_state_pointer_offset, f"{name} blend"),
            name,
        )
        depth = self._parse_depth(
            reader.u64(record + layout.depth_stencil_state_pointer_offset, f"{name} depth/stencil"),
            name,
        )
        compact = self._parse_compact(
            reader.u64(
                record + layout.compact_declaration_pointer_offset,
                f"{name} compact declarations",
            ),
            compact_count,
            name,
        )
        return SdfProgram(
            target_index=target_index,
            program_index=program_index,
            record_index=record_index,
            record_offset=record,
            name_offset=tables.name_offset,
            name=name,
            input_signature=input_signature,
            shaders=shaders,
            total_stage_binding_count=tables.total_stage_binding_count,
            per_stage_binding_counts=tables.per_stage_binding_counts,
            distinct_resource_counts=tables.distinct_resource_counts,
            compact_declaration_count=compact_count,
            compute_group_size=(group_x, group_y, group_z),
            primitive_topology_id=topology,
            primitive_topology=("undefined" if compute_present else self.profile.primitive_topology_names[topology]),
            reserved_tail_byte=reserved_tail,
            shader_requirements=shader_requirements,
            unknown_shader_requirement_bits=unknown_requirement_bits,
            input_layout=input_layout,
            rasterizer_state=rasterizer,
            blend_state=blend,
            depth_stencil_state=depth,
            compact_declarations=compact,
            bindings=tables.bindings,
            constant_buffers=tables.constant_buffers,
            samplers=tables.samplers,
            shader_resources=tables.shader_resources,
            unordered_access_resources=tables.unordered_access_resources,
            optional_resources=tables.optional_resources,
        )

    def _spirv_module(
        self,
        payload: bytes,
        role: str,
        program_name: str,
    ) -> SdfSpirvModule:
        field = f"{program_name} {role} SPIR-V"
        if len(payload) < 25:
            raise SdfFormatError(f"{self.reader.label}: {field} wrapper is truncated")
        module_size = struct.unpack_from("<I", payload, 0)[0]
        if module_size < 20 or module_size % 4 or 4 + module_size >= len(payload):
            raise SdfFormatError(
                f"{self.reader.label}: {field} has invalid module size {module_size}"
            )
        module = payload[4 : 4 + module_size]
        try:
            raw, (_magic, version, generator, bound, schema) = _spirv_module(module)
            instructions = tuple(_spirv_instructions(raw))
        except SpirvError as exc:
            raise SdfFormatError(f"{self.reader.label}: {field}: {exc}") from exc
        tail = payload[4 + module_size :]
        if not tail.endswith(b"\0") or b"\0" in tail[:-1]:
            raise SdfFormatError(
                f"{self.reader.label}: {field} entry-point suffix is not one C string"
            )
        try:
            entry_point = tail[:-1].decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise SdfFormatError(
                f"{self.reader.label}: {field} entry point is not ASCII"
            ) from exc
        if not entry_point:
            raise SdfFormatError(f"{self.reader.label}: {field} entry point is empty")

        entry_points: dict[str, int] = {}
        entry_point_interfaces: dict[str, tuple[int, ...]] = {}
        integer_types: dict[int, tuple[int, bool]] = {}
        floating_types: dict[int, int] = {}
        vector_types: dict[int, tuple[int, int]] = {}
        pointer_types: dict[int, tuple[int, int]] = {}
        variables: dict[int, tuple[int, int]] = {}
        locations: dict[int, int] = {}
        for opcode, operands in instructions:
            word_count = len(operands) + 1
            if opcode == 15:
                if word_count < 4:
                    raise SdfFormatError(
                        f"{self.reader.label}: {field} has a short OpEntryPoint"
                    )
                try:
                    name, name_words = _literal_string(operands, 2)
                except SpirvError as exc:
                    raise SdfFormatError(
                        f"{self.reader.label}: {field} OpEntryPoint name: {exc}"
                    ) from exc
                if not name.isascii():
                    raise SdfFormatError(
                        f"{self.reader.label}: {field} OpEntryPoint name is not ASCII"
                    )
                entry_points[name] = operands[0]
                entry_point_interfaces[name] = operands[2 + name_words :]
            elif opcode == 21 and word_count == 4:  # OpTypeInt
                result_id, width, signedness = operands
                integer_types[result_id] = (width, bool(signedness))
            elif opcode == 22 and word_count == 3:  # OpTypeFloat
                result_id, width = operands
                floating_types[result_id] = width
            elif opcode == 23 and word_count == 4:  # OpTypeVector
                result_id, component_type, component_count = operands
                vector_types[result_id] = (component_type, component_count)
            elif opcode == 32 and word_count == 4:  # OpTypePointer
                result_id, storage_class, value_type = operands
                pointer_types[result_id] = (storage_class, value_type)
            elif opcode == 59 and word_count >= 4:  # OpVariable
                result_type, result_id, storage_class = operands[:3]
                variables[result_id] = (result_type, storage_class)
            elif opcode == 71 and word_count >= 4:  # OpDecorate
                target_id, decoration = operands[:2]
                if decoration == 30:  # Location
                    if len(operands) != 3:
                        raise SdfFormatError(
                            f"{self.reader.label}: {field} has malformed Location decoration"
                        )
                    location = operands[2]
                    previous = locations.setdefault(target_id, location)
                    if previous != location:
                        raise SdfFormatError(
                            f"{self.reader.label}: {field} variable {target_id} has two locations"
                        )
        execution_model = entry_points.get(entry_point)
        if execution_model is None:
            raise SdfFormatError(
                f"{self.reader.label}: {field} suffix {entry_point!r} is not an OpEntryPoint"
            )
        execution_model_name = _STAGES.get(execution_model, f"execution-model-{execution_model}")
        if execution_model_name != role:
            raise SdfFormatError(
                f"{self.reader.label}: {field} declares {execution_model_name}"
            )

        interface_variables: list[SdfSpirvInterfaceVariable] = []
        interface_locations: set[int] = set()
        vertex_interface = (
            entry_point_interfaces.get(entry_point, ()) if role == "vertex" else ()
        )
        for variable_id in vertex_interface:
            variable = variables.get(variable_id)
            if variable is None:
                raise SdfFormatError(
                    f"{self.reader.label}: {field} interface ID {variable_id} is not an OpVariable"
                )
            pointer_type_id, storage_class = variable
            if storage_class != 1 or variable_id not in locations:
                continue
            pointer = pointer_types.get(pointer_type_id)
            if pointer is None or pointer[0] != storage_class:
                raise SdfFormatError(
                    f"{self.reader.label}: {field} input {variable_id} has an invalid pointer type"
                )
            value_type_id = pointer[1]
            scalar_type_id = value_type_id
            vector_components = 1
            if value_type_id in vector_types:
                scalar_type_id, vector_components = vector_types[value_type_id]
                if vector_components not in {2, 3, 4}:
                    raise SdfFormatError(
                        f"{self.reader.label}: {field} input {variable_id} has an unsupported vector width"
                    )
            if scalar_type_id in floating_types:
                scalar_type = "floating-point"
                scalar_width = floating_types[scalar_type_id]
            elif scalar_type_id in integer_types:
                scalar_width, signed = integer_types[scalar_type_id]
                scalar_type = "signed-integer" if signed else "unsigned-integer"
            else:
                raise SdfFormatError(
                    f"{self.reader.label}: {field} input {variable_id} has a non-scalar value type"
                )
            if scalar_width != 32:
                raise SdfFormatError(
                    f"{self.reader.label}: {field} input {variable_id} uses {scalar_width}-bit scalars"
                )
            location = locations[variable_id]
            if location in interface_locations or variable_id >= bound:
                raise SdfFormatError(
                    f"{self.reader.label}: {field} has duplicate/out-of-bound input IDs"
                )
            interface_locations.add(location)
            interface_variables.append(
                SdfSpirvInterfaceVariable(
                    variable_id=variable_id,
                    location=location,
                    scalar_type=scalar_type,
                    vector_components=vector_components,
                )
            )
        return SdfSpirvModule(
            module=module,
            version=version,
            generator=generator,
            bound=bound,
            schema=schema,
            instruction_count=len(instructions),
            entry_point=entry_point,
            execution_model=execution_model,
            execution_model_name=execution_model_name,
            input_variables=tuple(interface_variables),
        )

    def _spirv_input_signature(
        self,
        payload: bytes,
        input_layout: SdfInputLayout | None,
        program_name: str,
        expected_signature: tuple[DxbcSignatureParameter, ...] | None,
    ) -> SdfSpirvInputSignature:
        if input_layout is None:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} SPIR-V input signature "
                "has no semantic input-layout count"
            )
        if expected_signature is None:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} SPIR-V input signature "
                "has no baseline shader-signature contract"
            )
        count = len(expected_signature)
        stride = 8
        if len(payload) != count * stride:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} SPIR-V input signature "
                f"has {len(payload)} bytes for {count} declared inputs"
            )
        scalar_types = {4: "unsigned-integer", 8: "floating-point"}
        result: list[SdfSpirvInputElement] = []
        locations: set[int] = set()
        variable_ids: set[int] = set()
        layout_by_semantic: dict[tuple[int, int], list[SdfInputElement]] = {}
        for layout_element in input_layout.elements:
            layout_by_semantic.setdefault(
                (layout_element.semantic_id, layout_element.semantic_index),
                [],
            ).append(layout_element)
        for index in range(count):
            (
                semantic_id,
                semantic_index,
                location,
                variable_id,
                scalar_type_id,
                vector_flag,
                reserved0,
                reserved1,
            ) = struct.unpack_from("<8B", payload, index * stride)
            if semantic_id not in self.profile.semantic_names:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} SPIR-V input {index} "
                    f"has unknown semantic ID {semantic_id}"
                )
            candidates = layout_by_semantic.get((semantic_id, semantic_index), ())
            if not candidates:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} SPIR-V input {index} "
                    "is absent from its input layout"
                )
            if location in locations or not variable_id or variable_id in variable_ids:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} SPIR-V input {index} "
                    "has a duplicate location/variable ID"
                )
            if scalar_type_id not in scalar_types or vector_flag not in {0, 1}:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} SPIR-V input {index} "
                    "has an unsupported scalar type/vector flag"
                )
            if reserved0 or reserved1:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} SPIR-V input {index} "
                    "reserved bytes are non-zero"
                )
            expected_scalars = {
                None if item.dxgi_format.endswith("_SINT")
                else 4 if item.dxgi_format.endswith("_UINT") else 8
                for item in candidates
            }
            if scalar_type_id not in expected_scalars:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} SPIR-V input {index} "
                    "scalar type disagrees with its input layout"
                )
            baseline = expected_signature[index]
            if (
                self.profile.semantic_names[semantic_id].casefold()
                != baseline.semantic_name.casefold()
                or semantic_index != baseline.semantic_index
                or location != baseline.register
            ):
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} SPIR-V input {index} "
                    "semantic/location disagrees with the baseline DXBC signature"
                )
            expected_scalar = {1: 4, 3: 8}.get(baseline.component_type_id)
            component_count = int(baseline.mask).bit_count()
            expected_vector = component_count > 1
            if (
                expected_scalar is None
                or scalar_type_id != expected_scalar
                or bool(vector_flag) != expected_vector
            ):
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} SPIR-V input {index} "
                    "type disagrees with the baseline DXBC signature"
                )
            locations.add(location)
            variable_ids.add(variable_id)
            result.append(
                SdfSpirvInputElement(
                    semantic_id=semantic_id,
                    semantic=self.profile.semantic_names[semantic_id],
                    semantic_index=semantic_index,
                    location=location,
                    variable_id=variable_id,
                    scalar_type=scalar_types[scalar_type_id],
                    is_vector=bool(vector_flag),
                    component_count=component_count,
                )
            )
        return SdfSpirvInputSignature(tuple(result))

    def _validate_spirv_vertex_interface(
        self,
        input_signature: SdfShaderReference,
        shaders: tuple[SdfShaderReference, ...],
        program_name: str,
    ) -> None:
        compact = input_signature.spirv_input_signature
        if compact is None:
            return
        vertex = next((item for item in shaders if item.role == "vertex"), None)
        if vertex is None or vertex.spirv is None:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} has a SPIR-V input signature "
                "without a SPIR-V vertex module"
            )
        declared = {item.variable_id: item for item in compact.elements}
        reflected = {item.variable_id: item for item in vertex.spirv.input_variables}
        if declared.keys() != reflected.keys():
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} compact inputs do not match "
                "the SPIR-V entry-point interface"
            )
        for variable_id, element in declared.items():
            variable = reflected[variable_id]
            if (
                element.location != variable.location
                or element.scalar_type != variable.scalar_type
                or element.component_count != variable.vector_components
                or element.is_vector != (variable.vector_components > 1)
            ):
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} compact input variable "
                    f"{variable_id} disagrees with the SPIR-V module"
                )

    def _shader_reference(
        self,
        role: str,
        offset: int,
        size: int,
        program_name: str,
        *,
        target_index: int,
        input_layout: SdfInputLayout | None = None,
        native_input_signature: tuple[DxbcSignatureParameter, ...] | None = None,
    ) -> SdfShaderReference:
        reference_key = (role, offset, size)
        cached = self.shader_reference_cache.get(reference_key)
        if cached is not None:
            return cached
        if not offset and not size:
            result = SdfShaderReference(role, 0, 0, binary_format="none")
            self.shader_reference_cache[reference_key] = result
            return result
        if not offset or not size:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} has incomplete {role} pointer/size"
            )
        if offset < self.shader_binary_offset:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} {role} at 0x{offset:X} precedes shader data"
            )
        self.reader.require(offset, size, f"{program_name} {role}")
        payload_key = (offset, size)
        payload = self.shader_payload_cache.get(payload_key)
        if payload is None:
            payload = self.reader.bytes(offset, size, f"{program_name} {role}")
            self.shader_payload_cache[payload_key] = payload
        if payload[:4] != b"DXBC":
            if payload[:4] == b"\x00\x10\x05\x00":
                if not self.profile.raytracing_libraries or role != "compute":
                    raise SdfFormatError(f"{program_name}: DXR library is not supported in this build/lane")
                try:
                    library = raytracing.parse_raytracing_library(
                        payload, export_layout=self.profile.raytracing_export_layout,
                    )
                    reflection = raytracing.validate_raytracing_library(library)
                    if self.eager_native_reflection:
                        try:
                            reflection = raytracing.validate_raytracing_library(library, reflect=True)
                        except dxc.DxcError:
                            pass  # Opening does not require an installed native compiler.
                except ValueError as exc:
                    raise SdfFormatError(f"{program_name}: {exc}") from exc
                return SdfShaderReference(
                    role, offset, size, reflection, payload,
                    "dxil-raytracing-library", raytracing_library=library,
                )
            if target_index not in self.profile.spirv_target_indices:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} {role} payload is not "
                    "a DXBC/DXIL container"
                )
            if role == "input_signature":
                signature = self._spirv_input_signature(
                    payload,
                    input_layout,
                    program_name,
                    native_input_signature,
                )
                return SdfShaderReference(
                    role,
                    offset,
                    size,
                    payload=payload,
                    binary_format="spirv-input-signature",
                    spirv_input_signature=signature,
                )
            spirv_key = (offset, size, role)
            spirv = self.spirv_cache.get(spirv_key)
            if spirv is None:
                spirv = self._spirv_module(payload, role, program_name)
                self.spirv_cache[spirv_key] = spirv
            return SdfShaderReference(
                role,
                offset,
                size,
                payload=payload,
                binary_format="spirv-module",
                spirv=spirv,
            )
        key = (offset, size)
        reflection = self.shader_cache.get(key)
        if reflection is None:
            reflection = parse_dxbc(
                self.data,
                offset=offset,
                size=size,
                label=f"{self.reader.label}: {program_name} {role}",
                _signature_cache=self.signature_cache,
            )
            if self.eager_native_reflection and reflection.reflection is None and any(
                chunk.tag == "DXIL" for chunk in reflection.chunks
            ):
                reflection = dxc.enrich_dxil_shader(reflection, payload)
            self.shader_cache[key] = reflection
        if role in STAGE_KEYS and not self.profile.accepts_shader_stage(
            role,
            reflection.stage,
        ):
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} {role} pointer contains a "
                f"{reflection.stage} shader"
            )
        result = SdfShaderReference(
            role,
            offset,
            size,
            reflection,
            payload,
            "dxbc-container",
        )
        self.shader_reference_cache[reference_key] = result
        return result

    def _metadata_pointer(self, offset: int, size: int, field: str) -> None:
        if not offset:
            raise SdfFormatError(f"{self.reader.label}: {field} has a null pointer")
        if offset < self.table_end:
            raise SdfFormatError(f"{self.reader.label}: {field} points into the program table")
        self.reader.require(offset, size, field, end=self.shader_binary_offset)

    def _parse_input_layout(self, offset: int, program_name: str) -> SdfInputLayout | None:
        if not offset:
            return None
        cached = self.input_layout_cache.get(offset)
        if cached is not None:
            return cached
        layout = self.profile.program_layout
        header_size = layout.input_layout_header_size
        element_size = layout.input_element_size
        self._metadata_pointer(offset, header_size, f"{program_name} input-layout header")
        count = self.reader.u32(offset, f"{program_name} input-layout count", end=self.shader_binary_offset)
        reserved = self.reader.u32(offset + 4, f"{program_name} input-layout reserved", end=self.shader_binary_offset)
        self._metadata_pointer(
            offset,
            header_size + count * element_size,
            f"{program_name} input layout",
        )
        if reserved:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} input-layout reserved "
                f"dword is 0x{reserved:X}, expected zero"
            )
        elements: list[SdfInputElement] = []
        for index in range(count):
            entry = offset + header_size + index * element_size
            semantic_id, format_id, input_slot, semantic_index, offset_flags = struct.unpack_from(
                "<4BI", self.reader.data, entry
            )
            if semantic_id not in self.profile.semantic_names:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} input {index} has "
                    f"unknown semantic ID {semantic_id}"
                )
            semantic = self.profile.semantic_names[semantic_id]
            if format_id not in self.profile.input_formats:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} input {index} has "
                    f"unknown format ID {format_id}"
                )
            dxgi_id, dxgi_name = self.profile.input_formats[format_id]
            per_instance = bool(offset_flags & 0x80000000)
            elements.append(
                SdfInputElement(
                    semantic_id=semantic_id,
                    semantic=semantic,
                    format_id=format_id,
                    dxgi_format_id=dxgi_id,
                    dxgi_format=dxgi_name,
                    input_slot=input_slot,
                    semantic_index=semantic_index,
                    aligned_byte_offset=offset_flags & 0x7FFFFFFF,
                    per_instance=per_instance,
                    instance_step_rate=1 if per_instance else 0,
                )
            )
        result = SdfInputLayout(
            offset=offset,
            element_count=count,
            reserved=reserved,
            elements=tuple(elements),
            raw=self.reader.bytes(
                offset,
                header_size + count * element_size,
                f"{program_name} input layout",
            ),
        )
        self.input_layout_cache[offset] = result
        return result

    def _parse_rasterizer(self, offset: int, program_name: str) -> SdfRasterizerState | None:
        if not offset:
            return None
        cached = self.rasterizer_cache.get(offset)
        if cached is not None:
            return cached
        size = self.profile.rasterizer_state_size
        self._metadata_pointer(offset, size, f"{program_name} rasterizer state")
        flags = self.reader.u32(offset, f"{program_name} rasterizer flags", end=self.shader_binary_offset)
        binner_mask = (
            ((1 << self.profile.rasterizer_binner_control_bits) - 1)
            << self.profile.rasterizer_binner_control_shift
        )
        binner_control = (flags & binner_mask) >> self.profile.rasterizer_binner_control_shift
        if not self.profile.accepts_binner_control(binner_control):
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} rasterizer binner control "
                f"{binner_control} is not a {self.profile.display_name} binner state"
            )
        fill_id = _enum(self.profile.fill_mode_table, (flags >> 5) & 0x3)
        cull_id = _enum(self.profile.cull_mode_table, (flags >> 7) & 0x3)
        result = SdfRasterizerState(
            offset=offset,
            packed_flags=flags,
            fill_mode_id=fill_id,
            fill_mode=FILL_MODE_NAMES.get(fill_id, f"fill-{fill_id}"),
            cull_mode_id=cull_id,
            cull_mode=CULL_MODE_NAMES.get(cull_id, f"cull-{cull_id}"),
            front_counter_clockwise=bool(flags & 0x1),
            depth_bias=self.reader.i32(offset + 4, f"{program_name} depth bias", end=self.shader_binary_offset),
            depth_bias_clamp=self.reader.f32(
                offset + 8,
                f"{program_name} depth-bias clamp",
                end=self.shader_binary_offset,
            ),
            slope_scaled_depth_bias=self.reader.f32(
                offset + 12,
                f"{program_name} slope depth bias",
                end=self.shader_binary_offset,
            ),
            depth_clip_enable=bool(flags & 0x2),
            scissor_enable=bool(flags & 0x4),
            multisample_enable=bool(flags & 0x8),
            antialiased_line_enable=bool(flags & 0x10),
            reserved_bits=flags & ~(0x1FF | binner_mask | (0x200 if self.profile.conservative_rasterization_supported else 0)),
            raw=self.reader.bytes(offset, size, f"{program_name} rasterizer state"),
            binner_control=binner_control,
            conservative_rasterization=bool(flags & 0x200),
        )
        if result.reserved_bits:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} rasterizer reserved bits "
                f"0x{result.reserved_bits:X} are non-zero"
            )
        self.rasterizer_cache[offset] = result
        return result

    def _parse_blend(self, offset: int, program_name: str) -> SdfBlendState | None:
        if not offset:
            return None
        cached = self.blend_cache.get(offset)
        if cached is not None:
            return cached
        size = self.profile.blend_state_size
        self._metadata_pointer(offset, size, f"{program_name} blend state")
        targets: list[SdfBlendTarget] = []
        for index in range(8):
            packed = self.reader.u32(
                offset + index * 4,
                f"{program_name} blend target {index}",
                end=self.shader_binary_offset,
            )
            source_id = _enum(self.profile.blend_factor_table, (packed >> 1) & 0x1F)
            destination_id = _enum(self.profile.blend_factor_table, (packed >> 6) & 0x1F)
            operation_id = _enum(self.profile.blend_operation_table, (packed >> 11) & 0x7)
            source_alpha_id = _enum(self.profile.blend_factor_table, (packed >> 14) & 0x1F)
            destination_alpha_id = _enum(self.profile.blend_factor_table, (packed >> 19) & 0x1F)
            alpha_operation_id = _enum(self.profile.blend_operation_table, (packed >> 24) & 0x7)
            write_mask = (packed >> 27) & 0xF
            targets.append(
                SdfBlendTarget(
                    index=index,
                    packed=packed,
                    blend_enable=bool(packed & 0x1),
                    source_blend_id=source_id,
                    source_blend=BLEND_NAMES.get(source_id, f"blend-{source_id}"),
                    destination_blend_id=destination_id,
                    destination_blend=BLEND_NAMES.get(destination_id, f"blend-{destination_id}"),
                    blend_operation_id=operation_id,
                    blend_operation=BLEND_OPERATION_NAMES.get(operation_id, f"operation-{operation_id}"),
                    source_alpha_blend_id=source_alpha_id,
                    source_alpha_blend=BLEND_NAMES.get(source_alpha_id, f"blend-{source_alpha_id}"),
                    destination_alpha_blend_id=destination_alpha_id,
                    destination_alpha_blend=BLEND_NAMES.get(destination_alpha_id, f"blend-{destination_alpha_id}"),
                    alpha_blend_operation_id=alpha_operation_id,
                    alpha_blend_operation=BLEND_OPERATION_NAMES.get(
                        alpha_operation_id,
                        f"operation-{alpha_operation_id}",
                    ),
                    write_mask=write_mask,
                    write_channels=write_channels(write_mask),
                    reserved_bits=(packed >> 31) & 1,
                )
            )
        flags = self.reader.u32(offset + 0x20, f"{program_name} blend flags", end=self.shader_binary_offset)
        result = SdfBlendState(
            offset=offset,
            alpha_to_coverage_enable=bool(flags & 1),
            independent_blend_enable=bool(flags & 2),
            reserved_flags=flags & ~3,
            render_targets=tuple(targets),
            raw=self.reader.bytes(offset, size, f"{program_name} blend state"),
        )
        if result.reserved_flags:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} blend reserved flags "
                f"0x{result.reserved_flags:X} are non-zero"
            )
        if any(target.reserved_bits for target in result.render_targets):
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} render-target blend "
                "reserved bit 31 is set"
            )
        self.blend_cache[offset] = result
        return result

    def _depth_face(self, packed: int, shift: int) -> SdfDepthStencilFace:
        fail_id = _enum(self.profile.stencil_operation_table, (packed >> shift) & 0xF)
        depth_fail_id = _enum(
            self.profile.stencil_operation_table,
            (packed >> (shift + 4)) & 0xF,
        )
        pass_id = _enum(
            self.profile.stencil_operation_table,
            (packed >> (shift + 8)) & 0xF,
        )
        function_id = _enum(
            self.profile.comparison_function_table,
            (packed >> (shift + 12)) & 0xF,
        )
        return SdfDepthStencilFace(
            stencil_fail_operation_id=fail_id,
            stencil_fail_operation=STENCIL_OPERATION_NAMES.get(fail_id, f"operation-{fail_id}"),
            stencil_depth_fail_operation_id=depth_fail_id,
            stencil_depth_fail_operation=STENCIL_OPERATION_NAMES.get(depth_fail_id, f"operation-{depth_fail_id}"),
            stencil_pass_operation_id=pass_id,
            stencil_pass_operation=STENCIL_OPERATION_NAMES.get(pass_id, f"operation-{pass_id}"),
            stencil_function_id=function_id,
            stencil_function=COMPARISON_FUNCTION_NAMES.get(function_id, f"comparison-{function_id}"),
        )

    def _parse_depth(self, offset: int, program_name: str) -> SdfDepthStencilState | None:
        if not offset:
            return None
        cached = self.depth_cache.get(offset)
        if cached is not None:
            return cached
        size = self.profile.depth_stencil_state_size
        self._metadata_pointer(offset, size, f"{program_name} depth/stencil state")
        packed = self.reader.u64(offset, f"{program_name} depth/stencil state", end=self.shader_binary_offset)
        write_id = _enum(self.profile.depth_write_mask_table, (packed >> 1) & 0x3)
        function_id = _enum(self.profile.comparison_function_table, (packed >> 3) & 0xF)
        result = SdfDepthStencilState(
            offset=offset,
            packed=packed,
            depth_enable=bool(packed & 1),
            depth_write_mask_id=write_id,
            depth_write_mask=DEPTH_WRITE_MASK_NAMES.get(write_id, f"mask-{write_id}"),
            depth_function_id=function_id,
            depth_function=COMPARISON_FUNCTION_NAMES.get(function_id, f"comparison-{function_id}"),
            stencil_enable=bool(packed & 0x80),
            stencil_read_mask=(packed >> 8) & 0xFF,
            stencil_write_mask=(packed >> 16) & 0xFF,
            front_face=self._depth_face(packed, 24),
            back_face=self._depth_face(packed, 40),
            depth_bounds_test_enable=bool(packed & (1 << 56)),
            reserved_bits=(packed >> 57) & 0x7F,
            raw=self.reader.bytes(offset, size, f"{program_name} depth/stencil state"),
        )
        if result.reserved_bits:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} depth/stencil reserved "
                f"bits 0x{result.reserved_bits:02X} are non-zero"
            )
        self.depth_cache[offset] = result
        return result

    def _parse_compact(self, offset: int, count: int, program_name: str) -> tuple[SdfCompactDeclaration, ...]:
        if not count:
            if offset:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} has a compact-declaration "
                    "pointer with zero semantic count"
                )
            return ()
        key = (offset, count)
        cached = self.compact_cache.get(key)
        if cached is not None:
            return cached
        stride = self.profile.program_layout.compact_declaration_size
        self._metadata_pointer(offset, count * stride, f"{program_name} compact declarations")
        result: list[SdfCompactDeclaration] = []
        for index in range(count):
            entry = offset + index * stride
            packed_semantic = self.reader.u8(
                entry,
                f"{program_name} declaration {index} semantic",
                end=self.shader_binary_offset,
            )
            packed_register = self.reader.u16(
                entry + 1,
                f"{program_name} declaration {index} register selector",
                end=self.shader_binary_offset,
            )
            packed_type = self.reader.u8(
                entry + 3,
                f"{program_name} declaration {index} type",
                end=self.shader_binary_offset,
            )
            semantic_id = packed_semantic & 0x0F
            semantic_index = packed_semantic >> 4
            if semantic_id not in self.profile.semantic_names:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} compact declaration "
                    f"{index} has unknown semantic ID {semantic_id}"
                )
            semantic = self.profile.semantic_names[semantic_id]
            if packed_register & 1:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} compact declaration "
                    f"{index} has the unused register-selector flag set"
                )
            component_type_id = packed_type >> 5
            system_value_id = packed_type & 0x1F
            if component_type_id not in self.profile.component_type_names:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} compact declaration "
                    f"{index} has unknown component type ID {component_type_id}"
                )
            if system_value_id not in self.profile.system_value_names:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} compact declaration "
                    f"{index} has unknown system value ID {system_value_id}"
                )
            signed_register = struct.unpack(
                "<h",
                struct.pack("<H", packed_register),
            )[0]
            result.append(
                SdfCompactDeclaration(
                    offset=entry,
                    semantic_id=semantic_id,
                    semantic=semantic,
                    semantic_index=semantic_index,
                    packed_register=packed_register,
                    register=signed_register >> 1,
                    register_low_bit=packed_register & 1,
                    component_type_id=component_type_id,
                    component_type=self.profile.component_type_names[component_type_id],
                    system_value_id=system_value_id,
                    system_value=self.profile.system_value_names[system_value_id],
                    raw=self.reader.bytes(entry, stride, f"{program_name} declaration {index}"),
                )
            )
        parsed = tuple(result)
        self.compact_cache[key] = parsed
        return parsed

    def _parse_bindings(
        self,
        kind: str,
        offset: int,
        count: int,
        expected_stage_counts: tuple[int, int, int, int, int, int],
        program_name: str,
    ) -> tuple[SdfBindingDescriptor, ...]:
        if not count:
            self._validate_binding_counts(
                (0,) * len(STAGE_KEYS), expected_stage_counts, program_name, kind
            )
            return ()
        key = (kind, offset, count)
        cached = self.binding_cache.get(key)
        if cached is not None:
            bindings, actual = cached
            self._validate_binding_counts(actual, expected_stage_counts, program_name, kind)
            return bindings
        stride = self.profile.program_layout.binding_descriptor_size
        self._metadata_pointer(offset, count * stride, f"{program_name} {kind} bindings")
        type_names = self.profile.resource_type_names.get(kind, {})
        result: list[SdfBindingDescriptor] = []
        for index in range(count):
            entry = offset + index * stride
            raw = self.reader.bytes(entry, stride, f"{program_name} {kind} binding {index}")
            slots = tuple(raw[:6])
            mask = raw[6]
            type_code = raw[7]
            active = tuple(STAGE_KEYS[stage] for stage in range(6) if mask & (1 << stage))
            if mask & ~0x3F:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} {kind} binding {index} "
                    f"has invalid stage mask 0x{mask:02X}"
                )
            if type_code not in type_names:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} {kind} binding {index} "
                    f"has unknown {self.profile.game} type code 0x{type_code:02X}"
                )
            result.append(
                SdfBindingDescriptor(
                    offset=entry,
                    resource_kind=kind,
                    bind_slots=slots,
                    stage_mask=mask,
                    active_stages=active,
                    type_code=type_code,
                    type_name=type_names[type_code],
                    raw=raw,
                )
            )
        parsed = tuple(result)
        actual = tuple(sum(bool(binding.stage_mask & (1 << stage)) for binding in parsed) for stage in range(6))
        self._validate_binding_counts(actual, expected_stage_counts, program_name, kind)
        self.binding_cache[key] = parsed, actual
        return parsed

    def _validate_binding_counts(
        self,
        actual: tuple[int, ...],
        expected: tuple[int, int, int, int, int, int],
        program_name: str,
        kind: str,
    ) -> None:
        if actual != expected:
            raise SdfFormatError(
                f"{self.reader.label}: {program_name} {kind} masks count as {actual}, expected {expected}"
            )

    def _parse_constant_buffers(self, offset: int, count: int, program_name: str) -> tuple[SdfConstantBuffer, ...]:
        if not count:
            return ()
        key = (offset, count)
        cached = self.cb_cache.get(key)
        if cached is not None:
            return cached
        layout = self.profile.program_layout
        buffer_stride = layout.constant_buffer_metadata_size
        variable_stride = layout.constant_variable_size
        self._metadata_pointer(
            offset,
            count * buffer_stride,
            f"{program_name} constant-buffer metadata",
        )
        result: list[SdfConstantBuffer] = []
        for index in range(count):
            entry = offset + index * buffer_stride
            name_offset = self.reader.u64(entry, f"{program_name} CB {index} name", end=self.shader_binary_offset)
            name_hash = self.reader.u32(entry + 8, f"{program_name} CB {index} hash", end=self.shader_binary_offset)
            reserved = self.reader.u32(
                entry + 12,
                f"{program_name} CB {index} reserved",
                end=self.shader_binary_offset,
            )
            if reserved:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} CB {index} reserved "
                    f"dword is 0x{reserved:08X}, expected zero"
                )
            byte_size = self.reader.u32(entry + 16, f"{program_name} CB {index} size", end=self.shader_binary_offset)
            variable_count = self.reader.u32(
                entry + 20,
                f"{program_name} CB {index} variable count",
                end=self.shader_binary_offset,
            )
            variables_offset = self.reader.u64(
                entry + 24,
                f"{program_name} CB {index} variables",
                end=self.shader_binary_offset,
            )
            name = self.reader.string(
                name_offset,
                f"{program_name} CB {index} name",
                start=self.table_end,
                end=self.shader_binary_offset,
            )
            self._validate_name_hash(
                name,
                name_hash,
                f"{program_name} CB {index}",
            )
            variable_key = (variables_offset, variable_count, byte_size)
            cached_variables = self.variable_cache.get(variable_key)
            variables: list[SdfConstantVariable] = []
            if variable_count and cached_variables is None:
                self._metadata_pointer(
                    variables_offset,
                    variable_count * variable_stride,
                    f"{program_name} {name} variables",
                )
            variable_indices = range(variable_count) if cached_variables is None else ()
            for variable_index in variable_indices:
                variable = variables_offset + variable_index * variable_stride
                variable_name_offset = self.reader.u64(
                    variable,
                    f"{name} variable {variable_index} name",
                    end=self.shader_binary_offset,
                )
                variable_name = self.reader.string(
                    variable_name_offset,
                    f"{name} variable {variable_index} name",
                    start=self.table_end,
                    end=self.shader_binary_offset,
                )
                variable_hash = self.reader.u32(
                    variable + 8,
                    f"{name} variable {variable_index} hash",
                    end=self.shader_binary_offset,
                )
                variable_byte_offset = self.reader.u16(
                    variable + 12,
                    f"{name} variable {variable_index} offset",
                    end=self.shader_binary_offset,
                )
                variable_byte_size = self.reader.u16(
                    variable + 14,
                    f"{name} variable {variable_index} size",
                    end=self.shader_binary_offset,
                )
                if (
                    not variable_name
                    and not variable_byte_offset
                    and not variable_byte_size
                ):
                    if variable_index + 1 != variable_count:
                        raise SdfFormatError(
                            f"{self.reader.label}: {name} null variable terminator "
                            "is not the final table record"
                        )
                    self.discarded_authoring_ranges.append(
                        SdfDiscardedAuthoringRange(
                            variable,
                            variable_stride,
                            "fixed-capacity constant-variable terminator",
                        )
                    )
                    self.note(
                        "constant_variable.authoring_terminator",
                        "discarded the fixed-capacity null variable-table terminator",
                        "discarded-authoring-data",
                    )
                    continue
                self._validate_name_hash(
                    variable_name,
                    variable_hash,
                    f"{name} variable {variable_index}",
                )
                if variable_name and not variable_byte_size and self.profile.constant_variable_size_64k:
                    if variable_byte_offset != 0 or byte_size != 0x10000:
                        raise SdfFormatError(f"{self.reader.label}: {name}.{variable_name} has an invalid full-size variable sentinel")
                    variable_byte_size = 0x10000
                variables.append(
                    SdfConstantVariable(
                        offset=variable,
                        name_offset=variable_name_offset,
                        name=variable_name,
                        name_hash=variable_hash,
                        byte_offset=variable_byte_offset,
                        byte_size=variable_byte_size,
                    )
                )
            if cached_variables is None:
                cached_variables = tuple(variables)
                if len(cached_variables) == variable_count:
                    self.variable_cache[variable_key] = cached_variables
            result.append(
                SdfConstantBuffer(
                    offset=entry,
                    name_offset=name_offset,
                    name=name,
                    name_hash=name_hash,
                    reserved=reserved,
                    byte_size=byte_size,
                    variable_count=len(cached_variables),
                    variables_offset=variables_offset,
                    variables=cached_variables,
                )
            )
        parsed = tuple(result)
        self.cb_cache[key] = parsed
        return parsed

    def _parse_named_resources(
        self,
        kind: str,
        offset: int,
        count: int,
        program_name: str,
    ) -> tuple[SdfNamedResource, ...]:
        if not count:
            return ()
        key = (kind, offset, count)
        cached = self.resource_cache.get(key)
        if cached is not None:
            return cached
        stride = self.profile.program_layout.named_resource_metadata_size
        self._metadata_pointer(offset, count * stride, f"{program_name} {kind} metadata")
        result: list[SdfNamedResource] = []
        for index in range(count):
            entry = offset + index * stride
            name_offset = self.reader.u64(entry, f"{program_name} {kind} {index} name", end=self.shader_binary_offset)
            reserved = self.reader.u32(
                entry + 12,
                f"{program_name} {kind} {index} reserved",
                end=self.shader_binary_offset,
            )
            if reserved:
                raise SdfFormatError(
                    f"{self.reader.label}: {program_name} {kind} {index} "
                    f"reserved dword is 0x{reserved:08X}, expected zero"
                )
            resource_name = self.reader.string(
                name_offset,
                f"{program_name} {kind} {index} name",
                start=self.table_end,
                end=self.shader_binary_offset,
            )
            name_hash = self.reader.u32(
                entry + 8,
                f"{program_name} {kind} {index} hash",
                end=self.shader_binary_offset,
            )
            self._validate_name_hash(
                resource_name,
                name_hash,
                f"{program_name} {kind} {index}",
            )
            result.append(
                SdfNamedResource(
                    offset=entry,
                    resource_kind=kind,
                    name_offset=name_offset,
                    name=resource_name,
                    name_hash=name_hash,
                    reserved=reserved,
                )
            )
        parsed = tuple(result)
        self.resource_cache[key] = parsed
        return parsed


class SdfFile:
    def __init__(self) -> None:
        self.document: SdfDocument | None = None
        self.raw_data = b""

    @staticmethod
    def can_handle(data: bytes | bytearray | memoryview) -> bool:
        return len(data) >= 4 and bytes(memoryview(data)[:4]) == SDF_MAGIC

    @classmethod
    def from_bytes(
        cls,
        data: bytes | bytearray | memoryview,
        *,
        resource_version: int | None = None,
        game: object = "",
        profile: SdfGameProfile | None = None,
        label: str = "SDF",
    ) -> "SdfFile":
        result = cls()
        result.read(
            data,
            resource_version=resource_version,
            game=game,
            profile=profile,
            label=label,
        )
        return result

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        **kwargs,
    ) -> "SdfFile":
        source = Path(path)
        kwargs.setdefault("label", str(source))
        kwargs.setdefault("resource_version", resource_version_from_path(str(source), "sdf"))
        return cls.from_bytes(source.read_bytes(), **kwargs)

    def read(
        self,
        data: bytes | bytearray | memoryview,
        *,
        resource_version: int | None = None,
        game: object = "",
        profile: SdfGameProfile | None = None,
        label: str = "SDF",
    ) -> None:
        raw = bytes(data)
        selected_profile = profile or sdf_profile_for_resource(resource_version, game=game)
        self.document = _SdfParser(raw, selected_profile, label).parse()
        self.raw_data = raw

    def require_document(self) -> SdfDocument:
        if self.document is None:
            raise ValueError("no SDF file is loaded")
        return self.document

    def write(self) -> bytes:
        return self.require_document().write()


def parse_sdf_to_draw(data: bytes | bytearray | memoryview, profile: SdfGameProfile, label: str) -> SdfDocument:
    return _SdfParser(bytes(data), profile, label, eager_native_reflection=False, runtime_cache_keys=False,
                      discover_detached_metadata=False, lazy_programs=True).parse()


__all__ = [
    "SdfFile",
    "SdfFormatError",
    "parse_sdf_to_draw",
]
