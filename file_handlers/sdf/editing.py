from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from functools import partial
import hashlib
import struct

lazy from file_handlers.bounded_cache import BoundedCache
lazy from utils.hex_util import align

from . import canonical_writer as _writer
lazy from . import dxc, hlsl, raytracing, raytracing_abi
from .dxbc import parse_dxbc
lazy from .models import RESOURCE_KINDS, STAGE_KEYS, SdfDocument, SdfProgram
lazy from .runtime_cache import runtime_cache_key_bytes, state_runtime_cache_key, xxh64
lazy from .spirv import SPIRV_MAGIC, SpirvError, reflect_spirv
lazy from .spirv_implementation import relink_vertex_input_ids

_REPLACEMENT_EXECUTION_CACHE_ENTRIES = 1024
_REPLACEMENT_EXECUTION_CACHE_BYTES = 32 * 1024 * 1024


class SdfEditError(ValueError):
    pass


_bounded = partial(_writer._bounded, error=SdfEditError)
_ascii_name = partial(_writer._ascii_name, error=SdfEditError)
_pack_fields = partial(_writer._pack_fields, error=SdfEditError)


def _resource_kind_index(resource_kind) -> int:
    try:
        return RESOURCE_KINDS.index(str(resource_kind))
    except ValueError as exc:
        raise SdfEditError(f"unknown resource kind {resource_kind!r}") from exc


def _binding_fields(change: Mapping[str, object], kind: str, name: str, *, default_type_code: int):
    slots_value = change.get("bind_slots", ())
    if (not isinstance(slots_value, Sequence) or isinstance(slots_value, (str, bytes, bytearray))
            or len(slots_value) != len(STAGE_KEYS)):
        raise SdfEditError(f"{kind} {name!r} needs six binding slots")
    label = f"{kind} {name}"
    return (
        tuple(_bounded(value, 0, 0xFF, f"{label} binding slot") for value in slots_value),
        _bounded(int(change.get("stage_mask", 0)), 0, 0x3F, f"{label} stage mask"),
        _bounded(int(change.get("type_code", default_type_code)), 0, 0xFF, f"{label} type code"),
        _bounded(int(change.get("reserved", 0)), 0, 0xFFFFFFFF, f"{label} reserved value"),
    )


def _claim_slots(occupied: set[tuple[int, int]], slots, stage_mask: int, what: str) -> None:
    for stage in range(len(STAGE_KEYS)):
        if not stage_mask & (1 << stage):
            continue
        key = (stage, slots[stage])
        if key in occupied:
            raise SdfEditError(f"two {what} use {STAGE_KEYS[stage]} slot {slots[stage]}")
        occupied.add(key)


class SdfBinaryEditor:
    def __init__(self, document: SdfDocument, *, key_sources: Sequence[SdfDocument] = ()):
        if not document.profile.binary_editing:
            raise SdfEditError(
                f"binary editing is not registered for {document.profile.display_name}"
            )
        self.document = document
        self._key_sources = (document, *key_sources)
        self.profile = document.profile
        self._data = bytearray(document.original_data)
        self._shader_binary_offset = document.header.shader_binary_offset
        self._metadata_end = self._shader_binary_offset
        self._pointer_reference_counts: dict[int, int] | None = None
        self._reference_counts_complete = False
        self._metadata_name_offsets: set[int] | None = None
        self._state_keys_by_body: dict[tuple[str, bytes], object] | None = None
        layout = self.profile.program_layout
        self._pointer_fields = (
            layout.name_pointer_offset,
            layout.input_signature_pointer_offset,
            *(stage.pointer_offset for stage in self.profile.stages),
            layout.input_layout_pointer_offset,
            layout.rasterizer_state_pointer_offset,
            layout.blend_state_pointer_offset,
            layout.depth_stencil_state_pointer_offset,
            layout.compact_declaration_pointer_offset,
            *layout.resource_binding_pointer_offsets,
            *layout.resource_metadata_pointer_offsets,
        )
        self._pointer_field_counts = Counter(self._pointer_fields)
        self._shader_binary_pointer_offset = (
            8
            if self.profile.header_layout == "uniform"
            else align(6 + int(document.header.target_count) * 2, 8)
        )
        self._replacement_shader_offsets: dict[tuple[str, bytes], int] = {}
        self._replacement_execution = BoundedCache()
        self._replacement_metadata_offsets: dict[tuple[object, ...], int] = {}
        self._replacement_resource_table_offsets: dict[
            tuple[object, ...],
            tuple[int, int],
        ] = {}

    @property
    def data(self) -> bytes:
        if self._metadata_end != self._shader_binary_offset:
            self._resize_metadata_capacity(self._metadata_end - self._shader_binary_offset)
        return bytes(self._data)

    def program(self, target_index: int, program_index: int) -> SdfProgram:
        try:
            return self.document.targets[int(target_index)].programs[int(program_index)]
        except (IndexError, TypeError) as exc:
            raise SdfEditError(
                f"program {target_index}:{program_index} does not exist"
            ) from exc

    def shared_reference_count(self, offset: int) -> int:
        if not offset:
            return 0
        counts = self._pointer_reference_counts
        if counts is not None and (self._reference_counts_complete or offset in counts):
            return counts.get(offset, 0)
        if counts is None:
            count = self._count_pointer_references(offset)
            self._pointer_reference_counts = {offset: count}
            return count
        counts = {}
        for target in self.document.targets:
            for program in target.programs:
                for relative in self._pointer_fields:
                    pointer = self._program_pointer(program, relative)
                    if pointer:
                        counts[pointer] = counts.get(pointer, 0) + 1
        self._pointer_reference_counts = counts
        self._reference_counts_complete = True
        return counts.get(offset, 0)

    def _metadata_hasher(self):
        hasher = self.profile.metadata_name_hash
        if hasher is None:
            raise SdfEditError(f"metadata name hashing is not registered for {self.profile.display_name}")
        return hasher

    def _count_pointer_references(self, offset: int) -> int:
        programs = [*target.programs for target in self.document.targets]
        if not programs:
            return 0
        start = programs[0].record_offset
        stride = self.profile.record_size
        end = start + len(programs) * stride
        try:
            encoded = struct.pack("<Q", offset) if type(offset) in (int, bool) else None
        except (struct.error, TypeError, OverflowError):
            encoded = None
        if (
            encoded is not None
            and stride > 0
            and 0 <= start < end <= len(self._data)
            and all(0 <= relative <= stride - 8 for relative in self._pointer_fields)
            and all(program.record_offset == start + index * stride
                    for index, program in enumerate(programs))
        ):
            count = 0
            position = start
            while (position := self._data.find(encoded, position, end)) >= 0:
                count += self._pointer_field_counts.get((position - start) % stride, 0)
                position += 1
            return count
        return sum(
            struct.unpack_from("<Q", self._data, program.record_offset + relative)[0] == offset
            for program in programs
            for relative in self._pointer_fields
        )

    def _program_pointer(self, program: SdfProgram, relative_offset: int) -> int:
        return struct.unpack_from("<Q", self._data, program.record_offset + relative_offset)[0]

    def _set_program_pointer(self, program: SdfProgram, relative_offset: int, value: int) -> None:
        value = int(value)
        previous = self._program_pointer(program, relative_offset)
        struct.pack_into("<Q", self._data, program.record_offset + relative_offset, value)
        counts = self._pointer_reference_counts
        if counts is not None and previous != value:
            weight = self._pointer_field_counts.get(relative_offset, 0)
            if previous and weight and (self._reference_counts_complete or previous in counts):
                remaining = counts[previous] - weight
                if remaining or not self._reference_counts_complete:
                    counts[previous] = remaining
                else:
                    del counts[previous]
            if value and weight and (self._reference_counts_complete or value in counts):
                counts[value] = counts.get(value, 0) + weight

    def _binding_table_owners(self, offset: int, size: int):
        layout = self.profile.program_layout
        for target in self.document.targets:
            for program in target.programs:
                for kind_index, relative in enumerate(layout.resource_binding_pointer_offsets):
                    count = self._data[program.record_offset + layout.distinct_resource_count_offsets[kind_index]]
                    pointer = self._program_pointer(program, relative)
                    end = pointer + count * layout.binding_descriptor_size
                    if count and max(pointer, offset) < min(end, offset + size):
                        yield program, kind_index

    def _synchronize_binding_counts(self, program: SdfProgram, kind_index: int) -> None:
        layout = self.profile.program_layout
        count = self._data[program.record_offset + layout.distinct_resource_count_offsets[kind_index]]
        pointer = self._program_pointer(program, layout.resource_binding_pointer_offsets[kind_index])
        masks = self._data[pointer + 6 : pointer + count * layout.binding_descriptor_size : layout.binding_descriptor_size]
        stages = len(STAGE_KEYS)
        counts = bytes(sum(bool(mask & (1 << stage)) for mask in masks) for stage in range(stages))
        all_counts_offset = program.record_offset + layout.per_stage_binding_counts_offset
        count_offset = all_counts_offset + kind_index * stages
        self._data[count_offset : count_offset + stages] = counts
        struct.pack_into(
            "<I",
            self._data,
            program.record_offset + layout.total_stage_binding_count_offset,
            sum(self._data[all_counts_offset : all_counts_offset + len(RESOURCE_KINDS) * stages]),
        )

    def _install_resource_table(
        self,
        program: SdfProgram,
        kind_index: int,
        offsets: tuple[int, int],
        count: int,
    ) -> None:
        layout = self.profile.program_layout
        self._set_program_pointer(
            program, layout.resource_binding_pointer_offsets[kind_index], offsets[0]
        )
        self._set_program_pointer(
            program, layout.resource_metadata_pointer_offsets[kind_index], offsets[1]
        )
        self._data[
            program.record_offset + layout.distinct_resource_count_offsets[kind_index]
        ] = count
        self._synchronize_binding_counts(program, kind_index)

    def _replace_resource_table(self, program: SdfProgram, kind_index: int, tag: str,
                                existing: tuple, normalized: list, build_metadata) -> None:
        normalized_tuple = tuple(normalized)
        if existing == normalized_tuple and self._resource_table_is_original(program, kind_index):
            return
        layout = self.profile.program_layout
        cache_key = (
            tag,
            kind_index,
            self._program_pointer(program, layout.resource_binding_pointer_offsets[kind_index]),
            self._program_pointer(program, layout.resource_metadata_pointer_offsets[kind_index]),
            normalized_tuple,
        )
        cached = self._replacement_resource_table_offsets.get(cache_key)
        if cached is None:
            cached = (0, 0)
            if normalized:
                binding_raw = b"".join(bytes((*row[1], row[2], row[3])) for row in normalized)
                cached = (self._insert_metadata(binding_raw, alignment=8), build_metadata())
            self._replacement_resource_table_offsets[cache_key] = cached
        self._install_resource_table(program, kind_index, cached, len(normalized))

    def _resource_table_is_original(self, program: SdfProgram, kind_index: int) -> bool:
        layout = self.profile.program_layout
        source = self.document.original_data
        count_field = program.record_offset + layout.distinct_resource_count_offsets[kind_index]
        count = self._data[count_field]
        if count != source[count_field]:
            return False
        for relative, stride in (
            (layout.resource_binding_pointer_offsets[kind_index], layout.binding_descriptor_size),
            (layout.resource_metadata_pointer_offsets[kind_index],
             layout.constant_buffer_metadata_size if kind_index == 0 else layout.named_resource_metadata_size),
        ):
            pointer = self._program_pointer(program, relative)
            original_pointer = struct.unpack_from("<Q", source, program.record_offset + relative)[0]
            if pointer != original_pointer:
                return False
            end = pointer + count * stride
            if self._data[pointer:end] != source[pointer:end]:
                return False
        return True

    def _insert_metadata(self, raw: bytes, *, alignment: int = 8) -> int:
        offset = self._metadata_end
        prefix = (-offset) % max(1, int(alignment))
        payload_size = prefix + len(raw)
        # DXBC is normally 16-byte aligned.
        delta = align(payload_size, 16)
        block = b"\x00" * prefix + bytes(raw) + b"\x00" * (delta - payload_size)
        required_end = offset + delta
        if required_end > self._shader_binary_offset:
            used = offset - self.document.header.shader_binary_offset
            growth = required_end - self._shader_binary_offset
            if used:
                growth = align(max(16 * 1024, used, growth), 16)
            self._resize_metadata_capacity(growth)
        self._data[offset:required_end] = block
        self._metadata_end = required_end
        return offset + prefix

    def _resize_metadata_capacity(self, delta: int) -> None:
        old_shader_offset = self._shader_binary_offset
        layout = self.profile.program_layout
        shader_pointer_fields = (
            layout.input_signature_pointer_offset,
            *(stage.pointer_offset for stage in self.profile.stages),
        )
        pointers: dict[int, int] = {}
        for target in self.document.targets:
            for program in target.programs:
                for relative in shader_pointer_fields:
                    field = program.record_offset + relative
                    pointer = struct.unpack_from("<Q", self._data, field)[0]
                    if pointer:
                        if pointer < old_shader_offset:
                            raise SdfEditError(
                                f"shader pointer 0x{pointer:X} precedes shader region 0x{old_shader_offset:X}"
                            )
                        pointers[field] = pointer
        if delta > 0:
            self._data[old_shader_offset:old_shader_offset] = bytes(delta)
        else:
            del self._data[old_shader_offset + delta:old_shader_offset]
        self._shader_binary_offset = old_shader_offset + delta
        struct.pack_into("<Q", self._data, self._shader_binary_pointer_offset, self._shader_binary_offset)
        for field, pointer in pointers.items():
            struct.pack_into("<Q", self._data, field, pointer + delta)
        if self._pointer_reference_counts is not None:
            if delta < 0 and not self._reference_counts_complete and any(
                not count and self._shader_binary_offset <= pointer < old_shader_offset
                for pointer, count in self._pointer_reference_counts.items()
            ):
                self._pointer_reference_counts = None
            else:
                self._pointer_reference_counts = {
                    pointer + delta if pointer >= old_shader_offset else pointer: count
                    for pointer, count in self._pointer_reference_counts.items()
                }
        if self._replacement_shader_offsets:
            self._replacement_shader_offsets = {
                key: offset + delta if offset >= old_shader_offset else offset
                for key, offset in self._replacement_shader_offsets.items()
            }

    def _replace_pointed_block(
        self,
        program: SdfProgram,
        pointer_relative_offset: int,
        raw: bytes,
        *,
        isolate: bool,
        expected_size: int | None = None,
        existing_size: int | None = None,
        alignment: int = 8,
    ) -> int:
        old_offset = self._program_pointer(program, pointer_relative_offset)
        if expected_size is not None and len(raw) != expected_size:
            raise SdfEditError(
                f"replacement block is {len(raw)} bytes; expected {expected_size}"
            )
        if not isolate:
            if not old_offset:
                raise SdfEditError("an absent block cannot be edited in shared mode")
            if existing_size is not None and len(raw) != existing_size:
                raise SdfEditError(
                    "a resized metadata block requires selected-program copy-on-write scope"
                )
            if old_offset + len(raw) > self._metadata_end:
                raise SdfEditError("replacement block exceeds the metadata region")
            self._data[old_offset : old_offset + len(raw)] = raw
            return old_offset
        if (
            old_offset
            and existing_size == len(raw)
            and self.shared_reference_count(old_offset) <= 1
        ):
            self._data[old_offset : old_offset + len(raw)] = raw
            return old_offset
        new_offset = self._insert_metadata(raw, alignment=alignment)
        self._set_program_pointer(program, pointer_relative_offset, new_offset)
        return new_offset

    def _metadata_uses_name(self, offset: int) -> bool:
        if self._metadata_name_offsets is None:
            names: set[int] = set()
            seen = set()

            def add_names(rows):
                if id(rows) in seen:
                    return False
                seen.add(id(rows))
                names.update(row.name_offset for row in rows)
                return True

            for target in self.document.targets:
                for program in target.programs:
                    if add_names(program.constant_buffers):
                        for buffer in program.constant_buffers:
                            add_names(buffer.variables)
                    for rows in program.resource_metadata[1:]:
                        add_names(rows)
            for table in self.document.detached_variable_tables:
                add_names(table.variables)
            self._metadata_name_offsets = names
        return offset in self._metadata_name_offsets

    def rename_logical_program(self, program_index: int, name: str) -> None:
        index = int(program_index)
        try:
            programs = [target.programs[index] for target in self.document.targets]
        except IndexError as exc:
            raise SdfEditError(f"logical program {index} does not exist") from exc
        encoded = str(name).encode("utf-8")
        if not encoded or b"\x00" in encoded:
            raise SdfEditError("program name must be non-empty and contain no NUL byte")
        relative = self.profile.program_layout.name_pointer_offset
        terminated = encoded + b"\x00"
        current_pointers = tuple(self._program_pointer(program, relative) for program in programs)
        if all(pointer and self._data[pointer:pointer + len(terminated)] == terminated
               for pointer in current_pointers):
            return
        pointers = {program.name_offset for program in programs}
        old_names = {program.name for program in programs}
        if len(pointers) == 1 and len(old_names) == 1:
            pointer = next(iter(pointers))
            old_size = len(next(iter(old_names)).encode("utf-8")) + 1
            if (len(encoded) + 1 <= old_size
                    and self.shared_reference_count(pointer) == len(programs)
                    and not self._metadata_uses_name(pointer)):
                replacement = encoded + b"\x00" * (old_size - len(encoded))
                self._data[pointer : pointer + old_size] = replacement
                return
        offset = self._insert_metadata(encoded + b"\x00", alignment=1)
        for program in programs:
            self._set_program_pointer(program, relative, offset)

    def set_primitive_topology(self, target_index: int, program_index: int, topology_id: int) -> None:
        program = self.program(target_index, program_index)
        value = _bounded(topology_id, 0, 0xFFFF, "primitive topology")
        compute = program.shader('compute')
        if compute and compute.present and value:
            raise SdfEditError('Compute programs have no primitive topology; their wave limit must remain zero')
        if not (compute and compute.present) and value not in self.profile.primitive_topology_names:
            raise SdfEditError(f"unknown engine primitive topology {value}")
        struct.pack_into(
            "<H",
            self._data,
            program.record_offset + self.profile.program_layout.primitive_topology_offset,
            value,
        )

    def set_compute_group_size(
        self,
        target_index: int,
        program_index: int,
        size: Sequence[int],
    ) -> None:
        program = self.program(target_index, program_index)
        if len(size) != 3:
            raise SdfEditError("compute group size needs exactly three dimensions")
        values = tuple(_bounded(value, 0, 0xFFFF, "compute group dimension") for value in size)
        compute = next(stage for stage in self.profile.stages if stage.key == "compute")
        pointer = self._program_pointer(program, compute.pointer_offset)
        present = bool(pointer)
        library = self.profile.raytracing_libraries and self._data[pointer : pointer + 4] == b"\x00\x10\x05\x00"
        if (present and not library and not all(values)) or ((not present or library) and any(values)):
            raise SdfEditError(
                "compute shaders require three non-zero group dimensions; graphics programs require 0,0,0, as do DXR libraries"
            )
        struct.pack_into(
            "<3H",
            self._data,
            program.record_offset + self.profile.program_layout.compute_group_size_offset,
            *values,
        )

    def replace_shader(
        self,
        target_index: int,
        program_index: int,
        role: str,
        dxbc: bytes,
    ) -> None:
        current = self.program(target_index, program_index).shader(role)
        if current is not None and current.raytracing_library is not None:
            library = replace(current.raytracing_library, bytecode=bytes(dxbc))
            self.replace_shader_payload(target_index, program_index, role,
                raytracing.serialize_raytracing_library(library, export_layout=self.profile.raytracing_export_layout),
                binary_format="dxil-raytracing-library")
            return
        if bytes(dxbc[:4]) == struct.pack("<I", SPIRV_MAGIC):
            self.replace_shader_payload(target_index, program_index, role, dxbc,
                                        binary_format="spirv-module", entry_point=None)
            return
        self.replace_shader_payload(
            target_index,
            program_index,
            role,
            dxbc,
            binary_format="dxbc-container",
        )


    def _dxbc_replacement_execution(self, raw: bytes, role: str):
        cached = self._replacement_execution.get(raw)
        if cached is not None:
            return cached
        try:
            reflection = parse_dxbc(raw, label=f"replacement {role}")
        except Exception as exc:
            raise SdfEditError(
                f"replacement {role} is not a valid DXBC container: {exc}"
            ) from exc
        actual_stage = reflection.stage
        group_size = None
        if actual_stage == "compute":
            try:
                if reflection.model_major >= 6:
                    group_size = dxc.enrich_dxil_shader(reflection, raw).reflection.thread_group_size
                else:
                    group_size = hlsl.dxbc_thread_group_size(raw)
                if group_size is not None:
                    group_size = tuple(group_size)
            except Exception as exc:
                raise SdfEditError(f"Cannot read replacement compute execution dimensions: {exc}") from exc
        result = actual_stage, group_size
        self._replacement_execution.put(raw, result, len(raw) + 256, _REPLACEMENT_EXECUTION_CACHE_ENTRIES,
                                        _REPLACEMENT_EXECUTION_CACHE_BYTES)
        return result

    def replace_shader_payload(
        self,
        target_index: int,
        program_index: int,
        role: str,
        payload: bytes,
        *,
        binary_format: str,
        entry_point: str | None = "main",
    ) -> None:
        program = self.program(target_index, program_index)
        key = str(role).strip().lower()
        raw = bytes(payload)
        if not raw:
            raise SdfEditError("replacement shader is empty")
        group_size = None
        format_key = str(binary_format).strip().casefold()
        is_library = format_key == "dxil-raytracing-library"
        if is_library:
            if key != "compute" or not self.profile.raytracing_libraries:
                raise SdfEditError("DXR libraries are not supported in this build/lane")
            try:
                library = raytracing.parse_raytracing_library(raw, export_layout=self.profile.raytracing_export_layout)
                raytracing.validate_raytracing_library(library, reflect=True)
                current = program.shader(key)
                if current is not None and current.raytracing_library is not None:
                    raytracing_abi.validate_raytracing_replacement(current.raytracing_library, library)
            except Exception as exc:
                raise SdfEditError(f"replacement {key} is not a valid DXR library: {exc}") from exc
            actual_stage = "library"
            group_size = (0, 0, 0)  # DispatchRays has no compute thread group.
        elif format_key == "dxbc-container":
            actual_stage, group_size = self._dxbc_replacement_execution(raw, key)
        elif format_key == "spirv-module":
            if int(target_index) not in self.profile.spirv_target_indices:
                raise SdfEditError(
                    "SPIR-V shaders are not valid for the selected SDF target"
                )
            # None installs the module's own (first) entry point.
            entry = None if entry_point is None else str(entry_point).strip()
            if entry is not None and (not entry or not entry.isascii() or "\0" in entry):
                raise SdfEditError("replacement SPIR-V entry point is invalid")
            try:
                reflected_spirv = reflect_spirv(raw, entry_point=entry)
            except SpirvError as exc:
                raise SdfEditError(f"invalid replacement SPIR-V: {exc}") from exc
            entry = reflected_spirv.entry_point
            if not entry.isascii():
                raise SdfEditError("replacement SPIR-V entry point is invalid")
            actual_stage = reflected_spirv.stage
            group_size = reflected_spirv.thread_group_size if actual_stage == "compute" else None
            if actual_stage=='vertex' and program.input_signature.spirv_input_signature is not None:
                raw=relink_vertex_input_ids(raw,reflected_spirv,program.input_signature.spirv_input_signature)
            raw = struct.pack("<I", len(raw)) + raw + entry.encode("ascii") + b"\0"
        else:
            raise SdfEditError(f"unsupported replacement shader format {binary_format!r}")

        if key == "compute" and not is_library:
            if group_size is None or len(group_size) != 3 or any(not 1 <= n <= 0xffff for n in group_size):
                raise SdfEditError("Replacement compute shader has no encodable native thread-group dimensions")
        pointer_relative, size_relative = self._shader_slot(key, role)
        if key != "input_signature" and not is_library and not self.profile.accepts_shader_stage(key, actual_stage):
            raise SdfEditError(f"a {actual_stage} shader cannot replace the {key} stage")

        old_pointer = self._program_pointer(program, pointer_relative)
        old_size = struct.unpack_from(
            "<I", self._data, program.record_offset + size_relative
        )[0]
        # The pointer/size pair covers bytecode only; keyed builds append the runtime key.
        record = raw + (struct.pack("<Q", xxh64(raw)) if self.profile.runtime_cache_keys else b"")
        old_record_size = old_size + (8 if self.profile.runtime_cache_keys else 0)
        if (
            old_pointer
            and old_size == len(raw)
            and bytes(self._data[old_pointer : old_pointer + old_record_size]) == record
        ):
            if key == "compute":
                self.set_compute_group_size(target_index, program_index, group_size)
            return
        replacement_key = (f"{format_key}:{key}", record)
        cached_offset = self._replacement_shader_offsets.get(replacement_key)
        if (cached_offset is not None
                and self._data[cached_offset : cached_offset + len(record)] == record):
            offset = cached_offset
            self._set_program_pointer(program, pointer_relative, offset)
        else:
            if (
                old_pointer
                and len(raw) == old_size
                and self.shared_reference_count(old_pointer) <= 1
            ):
                offset = old_pointer
                self._data[offset : offset + old_record_size] = record
            else:
                offset = align(len(self._data), 16)
                self._data.extend(b"\x00" * (offset - len(self._data)))
                self._data.extend(record)
                self._set_program_pointer(program, pointer_relative, offset)
            self._replacement_shader_offsets[replacement_key] = offset
        struct.pack_into("<I", self._data, program.record_offset + size_relative, len(raw))

        if key == "compute":
            self.set_compute_group_size(target_index, program_index, group_size)

    def _shader_slot(self, key: str, role: str) -> tuple[int, int]:
        if key == "input_signature":
            layout = self.profile.program_layout
            return layout.input_signature_pointer_offset, layout.input_signature_size_offset
        stage = next((item for item in self.profile.stages if item.key == key), None)
        if stage is None:
            raise SdfEditError(f"unknown shader role {role!r}")
        return stage.pointer_offset, stage.size_offset

    def clear_shader(self, target_index: int, program_index: int, role: str) -> None:
        program = self.program(target_index, program_index)
        key = str(role).strip().lower()
        pointer_relative, size_relative = self._shader_slot(key, role)
        self._set_program_pointer(program, pointer_relative, 0)
        struct.pack_into("<I", self._data, program.record_offset + size_relative, 0)
        if key == "compute":
            offset = program.record_offset + self.profile.program_layout.compute_group_size_offset
            struct.pack_into("<3H", self._data, offset, 0, 0, 0)

    def set_rasterizer_state(
        self,
        target_index: int,
        program_index: int,
        values: Mapping[str, object],
        *,
        isolate: bool = True,
    ) -> None:
        program = self.program(target_index, program_index)
        state = program.rasterizer_state
        if state is None:
            raise SdfEditError("selected program has no rasterizer state")
        if values.get("binner_control", state.binner_control) != state.binner_control:
            raise SdfEditError("Rasterizer binner control must remain at the build's sampled value")
        if values.get("conservative_rasterization", False) and not self.profile.conservative_rasterization_supported:
            raise SdfEditError("Conservative rasterization is not verified for this build")
        flags = _pack_fields(state.packed_flags, _writer._RASTERIZER_FIELDS, self.profile,
                             lambda field: values.get(field, getattr(state, field)))
        raw = struct.pack(
            "<Ii2f",
            flags,
            int(values.get("depth_bias", state.depth_bias)),
            float(values.get("depth_bias_clamp", state.depth_bias_clamp)),
            float(values.get("slope_scaled_depth_bias", state.slope_scaled_depth_bias)),
        )
        self._replace_state(program, self.profile.program_layout.rasterizer_state_pointer_offset, state, raw,
                            "rasterizer state", isolate, self.profile.rasterizer_state_size)

    def set_blend_state(
        self,
        target_index: int,
        program_index: int,
        values: Mapping[str, object],
        *,
        isolate: bool = True,
    ) -> None:
        program = self.program(target_index, program_index)
        state = program.blend_state
        if state is None:
            raise SdfEditError("selected program has no blend state")
        requested_targets = values.get("render_targets", ())
        target_values = list(requested_targets) if isinstance(requested_targets, Sequence) else []
        packed_targets: list[int] = []
        for index, target in enumerate(state.render_targets):
            change = target_values[index] if index < len(target_values) else {}
            if not isinstance(change, Mapping):
                raise SdfEditError(f"render target {index} values must be a mapping")
            packed_targets.append(_pack_fields(target.packed, _writer._BLEND_TARGET_FIELDS, self.profile,
                                               lambda field: change.get(field, getattr(target, field)),
                                               f"render target {index}"))
        flags = _pack_fields(state.reserved_flags, _writer._BLEND_FLAGS, self.profile,
                             lambda field: values.get(field, getattr(state, field)))
        self._replace_state(program, self.profile.program_layout.blend_state_pointer_offset, state,
                            struct.pack("<9I", *packed_targets, flags), "blend state", isolate,
                            self.profile.blend_state_size)

    def set_depth_stencil_state(
        self,
        target_index: int,
        program_index: int,
        values: Mapping[str, object],
        *,
        isolate: bool = True,
    ) -> None:
        program = self.program(target_index, program_index)
        state = program.depth_stencil_state
        if state is None:
            raise SdfEditError("selected program has no depth/stencil state")
        packed = _pack_fields(state.packed, _writer._DEPTH_STENCIL_FIELDS, self.profile,
                              lambda field: values.get(field, getattr(state, field)))
        for attribute, base, label in _writer._STENCIL_FACES:
            change, face = values.get(attribute, {}), getattr(state, attribute)
            if not isinstance(change, Mapping):
                raise SdfEditError(f"{attribute} values must be a mapping")
            packed = _pack_fields(packed, _writer._STENCIL_FACE_FIELDS, self.profile,
                                  lambda field: change.get(field, getattr(face, field)), label, base=base)
        self._replace_state(program, self.profile.program_layout.depth_stencil_state_pointer_offset, state,
                            struct.pack("<Q", packed), "depth/stencil state", isolate,
                            self.profile.depth_stencil_state_size)

    def set_input_layout(
        self,
        target_index: int,
        program_index: int,
        elements: Sequence[Mapping[str, object]],
        *,
        isolate: bool = True,
    ) -> None:
        program = self.program(target_index, program_index)
        layout = program.input_layout
        if layout is None:
            raise SdfEditError("selected program has no input layout")
        raw = bytearray(struct.pack("<II", len(elements), layout.reserved))
        for index, element in enumerate(elements):
            semantic_id = _bounded(int(element["semantic_id"]), 0, 0xFF, f"input {index} semantic")
            format_id = _bounded(int(element["format_id"]), 0, 0xFF, f"input {index} format")
            if format_id not in self.profile.input_formats:
                raise SdfEditError(f"input {index} has unknown format ID {format_id}")
            input_slot = _bounded(int(element.get("input_slot", 0)), 0, 0xFF, f"input {index} slot")
            semantic_index = _bounded(
                int(element.get("semantic_index", 0)), 0, 0xFF, f"input {index} semantic index"
            )
            byte_offset = _bounded(
                int(element.get("aligned_byte_offset", 0)), 0, 0x7FFFFFFF, f"input {index} byte offset"
            )
            raw.extend(_writer._input_element_bytes(semantic_id, format_id, input_slot, semantic_index, byte_offset,
                                                    bool(element.get("per_instance", False))))
        self._replace_state(program, self.profile.program_layout.input_layout_pointer_offset, layout, raw.take_bytes(),
                            "input layout", isolate)

    def _known_state_key(self, field: str, body: bytes):
        known = self._state_keys_by_body
        if known is None:
            known = {}
            fields = {"rasterizer_state": "rasterizer state", "blend_state": "blend state",
                      "depth_stencil_state": "depth/stencil state", "input_layout": "input layout"}
            for target in (*source.targets for source in self._key_sources):
                for program in target.programs:
                    for attribute, name in fields.items():
                        state = getattr(program, attribute)
                        key = state.runtime_cache_key if state is not None else None
                        if key is not None and key.source_digest == hashlib.sha256(state.raw).digest():
                            known.setdefault((name, bytes(state.raw)), key)
            self._state_keys_by_body = known
        return known.get((field, bytes(body)))

    def _replace_state(self, program: SdfProgram, pointer_offset: int, state, body: bytes, field: str,
                       isolate: bool, size: int | None = None) -> None:
        """``size`` is the body's fixed size, None when it varies."""
        key_size = 8 if state.runtime_cache_key is not None else 0
        self._replace_pointed_block(program, pointer_offset, self._state_record(state, body, field), isolate=isolate,
                                    expected_size=None if size is None else size + key_size,
                                    existing_size=len(state.raw) + key_size)

    def _state_record(self, state, body: bytes, field: str) -> bytes:
        key = state.runtime_cache_key
        if self.profile.runtime_cache_keys and bytes(body) != bytes(state.raw):
            key = self._known_state_key(field, body) or key
        if self.profile.runtime_cache_keys and key is None:
            key = state_runtime_cache_key(body)
        if not self.profile.runtime_cache_keys and key is not None:
            raise SdfEditError(f"{field} has a runtime cache key in a build without that field")
        try:
            return body + runtime_cache_key_bytes(key, body, field, shader=False)
        except ValueError as exc:
            raise SdfEditError(str(exc)) from exc

    def set_compact_declarations(
        self,
        target_index: int,
        program_index: int,
        declarations: Sequence[Mapping[str, object]],
        *,
        isolate: bool = True,
    ) -> None:
        program = self.program(target_index, program_index)
        raw = bytearray()
        for index, declaration in enumerate(declarations):
            semantic_id = _bounded(
                int(declaration["semantic_id"]), 0, 0x0F, f"declaration {index} semantic"
            )
            semantic_index = _bounded(
                int(declaration.get("semantic_index", 0)), 0, 0x0F, f"declaration {index} semantic index"
            )
            register = _bounded(
                int(declaration.get("register", 0)),
                -0x4000,
                0x3FFF,
                f"declaration {index} register",
            )
            register_low_bit = _bounded(
                int(declaration.get("register_low_bit", 0)), 0, 1, f"declaration {index} register low bit"
            )
            if register_low_bit:
                raise SdfEditError(
                    f"declaration {index} uses the unused register-selector flag"
                )
            component = _bounded(
                int(declaration.get("component_type_id", 0)), 0, 0x07, f"declaration {index} component type"
            )
            system_value = _bounded(
                int(declaration.get("system_value_id", 0)), 0, 0x1F, f"declaration {index} system value"
            )
            if component not in self.profile.component_type_names:
                raise SdfEditError(f"declaration {index} has unknown component type ID {component}")
            if system_value not in self.profile.system_value_names:
                raise SdfEditError(f"declaration {index} has unknown system value ID {system_value}")
            raw.extend(
                struct.pack(
                    "<BHB",
                    semantic_id | (semantic_index << 4),
                    ((register << 1) | register_low_bit) & 0xFFFF,
                    system_value | (component << 5),
                )
            )
        layout = self.profile.program_layout
        count_field = program.record_offset + layout.compact_declaration_count_offset
        struct.pack_into("<I", self._data, count_field, len(declarations))
        if not declarations:
            self._set_program_pointer(program, layout.compact_declaration_pointer_offset, 0)
            return
        self._replace_pointed_block(
            program,
            layout.compact_declaration_pointer_offset,
            raw.take_bytes(),
            isolate=isolate,
            existing_size=program.compact_declaration_count * layout.compact_declaration_size,
            alignment=4,
        )

    def set_binding(
        self,
        target_index: int,
        program_index: int,
        resource_kind: str,
        binding_index: int,
        *,
        bind_slots: Sequence[int],
        stage_mask: int,
        type_code: int,
        isolate: bool = True,
    ) -> None:
        program = self.program(target_index, program_index)
        kind_index = _resource_kind_index(resource_kind)
        layout = self.profile.program_layout
        count = self._data[program.record_offset + layout.distinct_resource_count_offsets[kind_index]]
        index = int(binding_index)
        if not 0 <= index < count:
            raise SdfEditError(f"{resource_kind} binding {index} does not exist")
        if len(bind_slots) != len(STAGE_KEYS):
            raise SdfEditError("a binding needs one slot for each of the six shader stages")
        slots = tuple(_bounded(value, 0, 0xFF, "binding slot") for value in bind_slots)
        mask = _bounded(stage_mask, 0, 0x3F, "binding stage mask")
        resource_type = _bounded(type_code, 0, 0xFF, "resource type code")
        pointer_relative = layout.resource_binding_pointer_offsets[kind_index]
        pointer = self._program_pointer(program, pointer_relative)
        size = count * layout.binding_descriptor_size
        raw = bytearray(self._data[pointer : pointer + size])
        start = index * layout.binding_descriptor_size
        raw[start : start + layout.binding_descriptor_size] = bytes((*slots, mask, resource_type))
        owners = tuple(self._binding_table_owners(pointer, size))
        if isolate and len(owners) > 1:
            new_offset = self._insert_metadata(raw.take_bytes(), alignment=8)
            self._set_program_pointer(program, pointer_relative, new_offset)
        else:
            new_offset = self._replace_pointed_block(
                program,
                pointer_relative,
                raw.take_bytes(),
                isolate=isolate,
                expected_size=size,
                existing_size=size,
            )

        self._replacement_resource_table_offsets = {
            cache_key: offsets
            for cache_key, offsets in self._replacement_resource_table_offsets.items()
            if max(offsets[0], new_offset) >= min(
                offsets[0] + len(cache_key[-1]) * layout.binding_descriptor_size,
                new_offset + size,
            )
        }

        for owner, owner_kind in ((program, kind_index),) if isolate else owners:
            self._synchronize_binding_counts(owner, owner_kind)

    def set_named_resource_table(
        self,
        target_index: int,
        program_index: int,
        resource_kind: str,
        resources: Sequence[Mapping[str, object]],
    ) -> None:
        program = self.program(target_index, program_index)
        kind_index = _resource_kind_index(resource_kind)
        if kind_index == 0:
            raise SdfEditError(
                "constant buffers require their structured metadata editor"
            )
        changes = tuple(resources)
        if len(changes) > 0xFF:
            raise SdfEditError("a program cannot encode more than 255 resources of one kind")
        hasher = self._metadata_hasher()

        normalized: list[tuple[str, tuple[int, ...], int, int, int]] = []
        names: set[str] = set()
        occupied_slots: set[tuple[int, int]] = set()
        for index, change in enumerate(changes):
            if not isinstance(change, Mapping):
                raise SdfEditError(f"resource {index} values must be a mapping")
            name = _ascii_name(change.get("name", ""), f"resource {index} name", allow_empty=True)
            # DMC5 hashes metadata names case-sensitively; some fields differ only by case.
            if name and name in names:
                raise SdfEditError(f"resource name {name!r} is duplicated")
            if name:
                names.add(name)
            slots, stage_mask, type_code, reserved = _binding_fields(change, "resource", name, default_type_code=0)
            _claim_slots(occupied_slots, slots, stage_mask, f"{resource_kind} resources")
            normalized.append((name, slots, stage_mask, type_code, reserved))

        metadata = program.resource_metadata[kind_index]
        bindings = program.bindings[kind_index]
        existing = tuple(
            (
                item.name,
                tuple(binding.bind_slots),
                int(binding.stage_mask),
                int(binding.type_code),
                int(item.reserved),
            )
            for item, binding in zip(metadata, bindings)
        )

        def build_metadata() -> int:
            stride = self.profile.program_layout.named_resource_metadata_size
            metadata_raw = bytearray(len(normalized) * stride)
            name_relatives = []
            for index, (name, _slots, _mask, _type, reserved) in enumerate(normalized):
                struct.pack_into("<II", metadata_raw, index * stride + 8, hasher(name), reserved)
                name_relatives.append(len(metadata_raw))
                metadata_raw.extend(name.encode("ascii") + b"\x00")
            metadata_offset = self._insert_metadata(metadata_raw, alignment=8)
            for index, relative in enumerate(name_relatives):
                struct.pack_into("<Q", self._data, metadata_offset + index * stride, metadata_offset + relative)
            return metadata_offset

        self._replace_resource_table(program, kind_index, "named-resource-table", existing, normalized, build_metadata)

    def set_constant_buffer_table(
        self,
        target_index: int,
        program_index: int,
        buffers: Sequence[Mapping[str, object]],
    ) -> None:
        program = self.program(target_index, program_index)
        changes = tuple(buffers)
        if len(changes) > 0xFF:
            raise SdfEditError("a program cannot encode more than 255 constant buffers")
        hasher = self._metadata_hasher()

        normalized: list[
            tuple[
                str,
                tuple[int, ...],
                int,
                int,
                int,
                int,
                tuple[tuple[str, int, int], ...],
            ]
        ] = []
        names: set[str] = set()
        occupied_slots: set[tuple[int, int]] = set()
        for index, change in enumerate(changes):
            if not isinstance(change, Mapping):
                raise SdfEditError(f"constant buffer {index} values must be a mapping")
            name = _ascii_name(change.get("name", ""), f"constant buffer {index} name")
            if name in names:
                raise SdfEditError(f"constant-buffer name {name!r} is duplicated")
            names.add(name)

            slots, stage_mask, type_code, reserved = _binding_fields(
                change, "constant buffer", name, default_type_code=0xFF
            )
            byte_size = _bounded(
                int(change.get("byte_size", 0)), 0, 0xFFFFFFFF, f"constant buffer {name} byte size"
            )
            _claim_slots(occupied_slots, slots, stage_mask, "constant buffers")

            values = change.get("variables", ())
            if not isinstance(values, Sequence) or isinstance(
                values,
                (str, bytes, bytearray),
            ):
                raise SdfEditError(
                    f"constant buffer {name!r} variables must be a sequence"
                )
            variables: list[tuple[str, int, int]] = []
            variable_names: set[str] = set()
            for variable_index, value in enumerate(values):
                if not isinstance(value, Mapping):
                    raise SdfEditError(
                        f"constant buffer {name!r} variable {variable_index} must be a mapping"
                    )
                variable_name = _ascii_name(value.get("name", ""), f"constant-buffer variable {variable_index}")
                if variable_name in variable_names:
                    raise SdfEditError(
                        f"constant buffer {name!r} duplicates variable {variable_name!r}"
                    )
                variable_names.add(variable_name)
                byte_offset = _bounded(
                    int(value.get("byte_offset", 0)),
                    0,
                    0xFFFF,
                    f"variable {variable_name} byte offset",
                )
                variable_size = _bounded(
                    int(value.get("byte_size", 0)),
                    1,
                    0x10000 if self.profile.constant_variable_size_64k and byte_offset == 0 and byte_size == 0x10000 else 0xFFFF,
                    f"variable {variable_name} byte size",
                )
                if byte_offset + variable_size > byte_size:
                    raise SdfEditError(
                        f"variable {variable_name} ends at byte "
                        f"{byte_offset + variable_size}, past the {byte_size}-byte "
                        f"constant buffer"
                    )
                variables.append((variable_name, byte_offset, variable_size))
            normalized.append(
                (
                    name,
                    slots,
                    stage_mask,
                    type_code,
                    reserved,
                    byte_size,
                    tuple(variables),
                )
            )

        existing = tuple(
            (
                buffer.name,
                tuple(binding.bind_slots),
                int(binding.stage_mask),
                int(binding.type_code),
                int(buffer.reserved),
                int(buffer.byte_size),
                tuple(
                    (variable.name, variable.byte_offset, variable.byte_size)
                    for variable in buffer.variables
                ),
            )
            for buffer, binding in zip(program.constant_buffers, program.bindings[0])
        )

        def build_metadata() -> int:
            layout = self.profile.program_layout
            buffer_stride = layout.constant_buffer_metadata_size
            variable_stride = layout.constant_variable_size
            metadata_raw = bytearray(len(normalized) * buffer_stride)
            pointer_relocations: list[int] = []
            for buffer_index, (name, *_binding, reserved, byte_size, variables) in enumerate(normalized):
                while len(metadata_raw) % 8:
                    metadata_raw.append(0)
                variables_relative = len(metadata_raw) if variables else 0
                if variables:
                    metadata_raw.extend(b"\x00" * (len(variables) * variable_stride))
                for variable_index, (variable_name, byte_offset, variable_size) in enumerate(variables):
                    entry = variables_relative + variable_index * variable_stride
                    struct.pack_into(
                        "<QIHH", metadata_raw, entry,
                        len(metadata_raw), hasher(variable_name), byte_offset, variable_size & 0xFFFF,
                    )
                    metadata_raw.extend(variable_name.encode("ascii") + b"\x00")
                    pointer_relocations.append(entry)

                name_relative = len(metadata_raw)
                metadata_raw.extend(name.encode("ascii") + b"\x00")
                buffer_entry = buffer_index * buffer_stride
                struct.pack_into(
                    "<QIIIIQ", metadata_raw, buffer_entry,
                    name_relative, hasher(name), reserved, byte_size, len(variables), variables_relative,
                )
                pointer_relocations.append(buffer_entry)
                if variables_relative:
                    pointer_relocations.append(buffer_entry + 24)

            metadata_offset = self._insert_metadata(metadata_raw, alignment=8)
            for relative in pointer_relocations:
                value = struct.unpack_from("<Q", self._data, metadata_offset + relative)[0]
                struct.pack_into("<Q", self._data, metadata_offset + relative, metadata_offset + value)
            return metadata_offset

        self._replace_resource_table(program, 0, "constant-buffer-table", existing, normalized, build_metadata)

    def set_resource_metadata(
        self,
        target_index: int,
        program_index: int,
        resource_kind: str,
        resource_index: int,
        *,
        name: str,
        byte_size: int | None = None,
        variables: Sequence[Mapping[str, object]] | None = None,
    ) -> None:
        program = self.program(target_index, program_index)
        kind_index = _resource_kind_index(resource_kind)
        metadata = program.resource_metadata[kind_index]
        index = int(resource_index)
        if not 0 <= index < len(metadata):
            raise SdfEditError(f"{resource_kind} metadata {index} does not exist")
        encoded_name = str(name).encode("ascii", "strict")
        if b"\x00" in encoded_name:
            raise SdfEditError("resource names must be ASCII without NUL bytes")
        hasher = self._metadata_hasher()

        layout = self.profile.program_layout
        pointer_relative = layout.resource_metadata_pointer_offsets[kind_index]
        if kind_index:
            if metadata[index].name == str(name):
                return
            stride = layout.named_resource_metadata_size
            first = metadata[0].offset
            raw = bytearray(self._data[first : first + len(metadata) * stride])
            string_relative = len(raw)
            raw.extend(encoded_name + b"\x00")
            new_base = self._insert_metadata(raw.take_bytes(), alignment=8)
            entry = new_base + index * stride
            struct.pack_into("<Q", self._data, entry, new_base + string_relative)
            struct.pack_into("<I", self._data, entry + 8, hasher(str(name)))
            self._set_program_pointer(program, pointer_relative, new_base)
            return

        buffers = program.constant_buffers
        target = buffers[index]
        buffer_stride = layout.constant_buffer_metadata_size
        variable_stride = layout.constant_variable_size
        first = buffers[0].offset
        raw = bytearray(self._data[first : first + len(buffers) * buffer_stride])
        target_entry_relative = index * buffer_stride
        target_size = (
            target.byte_size
            if byte_size is None
            else _bounded(byte_size, 0, 0xFFFFFFFF, "constant-buffer byte size")
        )
        struct.pack_into("<I", raw, target_entry_relative + 16, target_size)

        variable_relative = None
        variable_changes = None
        if variables is not None:
            variable_changes = list(variables)
            if len(variable_changes) > 0xFFFFFFFF:
                raise SdfEditError("constant-buffer variable table is too large")
            struct.pack_into(
                "<I",
                raw,
                target_entry_relative + 20,
                len(variable_changes),
            )
            if variable_changes:
                while len(raw) % 8:
                    raw.append(0)
                variable_relative = len(raw)
                raw.extend(b"\x00" * (len(variable_changes) * variable_stride))
            else:
                struct.pack_into("<Q", raw, target_entry_relative + 24, 0)

        normalized_changes: tuple[tuple[str, int, int], ...] | None = None
        if variable_changes is not None:
            normalized = []
            for variable_index, change in enumerate(variable_changes):
                if not isinstance(change, Mapping):
                    raise SdfEditError(f"variable {variable_index} values must be a mapping")
                original = (
                    target.variables[variable_index]
                    if variable_index < len(target.variables)
                    else None
                )
                variable_name = str(
                    change.get("name", original.name if original is not None else "")
                )
                encoded = variable_name.encode("ascii", "strict")
                if not encoded or b"\x00" in encoded:
                    raise SdfEditError(
                        f"constant-buffer variable {variable_index} needs a non-empty ASCII name"
                    )
                value_offset = _bounded(
                    int(
                        change.get(
                            "byte_offset",
                            original.byte_offset if original is not None else 0,
                        )
                    ),
                    0,
                    0xFFFF,
                    f"variable {variable_name} byte offset",
                )
                value_size = _bounded(
                    int(
                        change.get(
                            "byte_size",
                            original.byte_size if original is not None else 0,
                        )
                    ),
                    1,
                    0x10000 if self.profile.constant_variable_size_64k and value_offset == 0 and target_size == 0x10000 else 0xFFFF,
                    f"variable {variable_name} byte size",
                )
                if value_offset + value_size > target_size:
                    raise SdfEditError(
                        f"variable {variable_name} ends at byte {value_offset + value_size}, "
                        f"past the {target_size}-byte constant buffer"
                    )
                normalized.append((variable_name, value_offset, value_size))
            normalized_changes = tuple(normalized)

        existing_variables = tuple(
            (item.name, int(item.byte_offset), int(item.byte_size))
            for item in target.variables
        )
        if (
            target.name == str(name)
            and int(target.byte_size) == target_size
            and (
                normalized_changes is None
                or normalized_changes == existing_variables
            )
        ):
            return

        original_pointer = self._program_pointer(program, pointer_relative)
        cache_key = (
            "constant-buffer-layout",
            original_pointer,
            index,
            str(name),
            target_size,
            normalized_changes,
        )
        cached = self._replacement_metadata_offsets.get(cache_key)
        if cached is not None:
            self._set_program_pointer(program, pointer_relative, cached)
            return

        name_relative = len(raw)
        raw.extend(encoded_name + b"\x00")
        if variable_relative is not None:
            for variable_index, (variable_name, value_offset, value_size) in enumerate(
                normalized_changes
            ):
                relative = variable_relative + variable_index * variable_stride
                struct.pack_into("<IHH", raw, relative + 8, hasher(variable_name), value_offset, value_size & 0xFFFF)
                variable_name_relative = len(raw)
                raw.extend(variable_name.encode("ascii") + b"\x00")
                struct.pack_into("<Q", raw, relative, variable_name_relative)

        new_base = self._insert_metadata(raw.take_bytes(), alignment=8)
        target_entry = new_base + target_entry_relative
        struct.pack_into("<Q", self._data, target_entry, new_base + name_relative)
        struct.pack_into("<I", self._data, target_entry + 8, hasher(str(name)))
        if variable_relative is not None:
            struct.pack_into("<Q", self._data, target_entry + 24, new_base + variable_relative)
            for variable_index in range(len(normalized_changes)):
                entry = new_base + variable_relative + variable_index * variable_stride
                relative_string = struct.unpack_from("<Q", self._data, entry)[0]
                struct.pack_into("<Q", self._data, entry, new_base + relative_string)
        self._set_program_pointer(program, pointer_relative, new_base)
        self._replacement_metadata_offsets[cache_key] = new_base


__all__ = ["SdfBinaryEditor", "SdfEditError"]
