from __future__ import annotations

import struct

lazy from .models import (
    SdfDetachedVariable,
    SdfDetachedVariableTable,
    SdfDocument,
    SdfProgram,
)


class SdfByteClaims:
    def __init__(self, size: int):
        self.size = size
        self.ranges: set[tuple[int, int]] = set()

    def add(self, offset: int, size: int) -> None:
        start = max(0, int(offset))
        end = min(self.size, start + max(0, int(size)))
        if end > start:
            self.ranges.add((start, end))

    def merged(self) -> tuple[tuple[int, int], ...]:
        result: list[list[int]] = []
        for start, end in sorted(self.ranges):
            if not result or start > result[-1][1]:
                result.append([start, end])
            else:
                result[-1][1] = max(result[-1][1], end)
        return tuple((start, end) for start, end in result)


def iter_gaps(size: int, merged: tuple[tuple[int, int], ...]):
    cursor = 0
    for start, end in (*merged, (size, size)):
        if cursor < start:
            yield cursor, start
        cursor = max(cursor, end)


def string_size(data: bytes, offset: int, end: int) -> int:
    if not 0 <= offset < end <= len(data):
        return 0
    terminator = data.find(b"\x00", offset, end)
    return terminator - offset + 1 if terminator >= 0 else 0


def claim_program_metadata(
    claims: SdfByteClaims,
    document: SdfDocument,
    program: SdfProgram,
    *,
    seen: set[int] | None = None,
) -> None:
    data = document.original_data
    layout = document.profile.program_layout
    metadata_end = document.header.shader_binary_offset
    if seen is None:
        seen = set()

    def unique(items):
        for item in items:
            if item is not None and id(item) not in seen:
                seen.add(id(item))
                yield item

    claims.add(program.name_offset, string_size(data, program.name_offset, metadata_end))
    for state in unique((
        program.input_layout,
        program.rasterizer_state,
        program.blend_state,
        program.depth_stencil_state,
    )):
        claims.add(state.offset, len(state.raw) + (8 if state.runtime_cache_key is not None else 0))
    for declaration in unique(program.compact_declarations):
        claims.add(declaration.offset, len(declaration.raw))
    for group in program.bindings:
        for binding in unique(group):
            claims.add(binding.offset, len(binding.raw))

    for buffer in unique(program.constant_buffers):
        claims.add(buffer.offset, layout.constant_buffer_metadata_size)
        claims.add(buffer.name_offset, string_size(data, buffer.name_offset, metadata_end))
        for variable in unique(buffer.variables):
            claims.add(variable.offset, layout.constant_variable_size)
            claims.add(
                variable.name_offset,
                string_size(data, variable.name_offset, metadata_end),
            )
    for group in program.resource_metadata[1:]:
        for resource in unique(group):
            claims.add(resource.offset, layout.named_resource_metadata_size)
            claims.add(
                resource.name_offset,
                string_size(data, resource.name_offset, metadata_end),
            )


def claim_document_metadata(claims: SdfByteClaims, document: SdfDocument) -> None:
    seen: set[int] = set()
    for target in document.targets:
        for program in target.programs:
            claim_program_metadata(claims, document, program, seen=seen)
    for discarded in document.discarded_authoring_ranges:
        claims.add(discarded.offset, discarded.size)


def _decode_detached_variables(
    document: SdfDocument,
    start: int,
    end: int,
) -> SdfDetachedVariableTable | None:
    stride = document.profile.program_layout.constant_variable_size
    hash_name = document.profile.metadata_name_hash
    data = document.original_data
    metadata_end = document.header.shader_binary_offset
    if stride != 0x10 or hash_name is None or start >= metadata_end:
        return None
    clipped_end = min(end, metadata_end)
    if clipped_end - start < stride or (clipped_end - start) % stride:
        return None
    variables: list[SdfDetachedVariable] = []
    for entry in range(start, clipped_end, stride):
        name_offset, name_hash, byte_offset, byte_size = struct.unpack_from(
            "<Q I H H",
            data,
            entry,
        )
        name_length = string_size(data, name_offset, metadata_end)
        if not name_length or not byte_size:
            return None
        name = data[name_offset : name_offset + name_length - 1].decode(
            "utf-8",
            "replace",
        )
        if hash_name(name) != name_hash:
            return None
        variables.append(
            SdfDetachedVariable(
                name=name,
                name_offset=name_offset,
                name_hash=name_hash,
                byte_offset=byte_offset,
                byte_size=byte_size,
            )
        )
    return SdfDetachedVariableTable(start, tuple(variables))


def discover_detached_variable_tables(
    document: SdfDocument,
) -> tuple[SdfDetachedVariableTable, ...]:
    metadata_end = document.header.shader_binary_offset
    claims = SdfByteClaims(metadata_end)
    claims.add(0, document.header.program_table_offset)
    claims.add(
        document.header.program_table_offset,
        document.header.record_count * document.profile.record_size,
    )
    claim_document_metadata(claims, document)
    tables = []
    for start, end in iter_gaps(metadata_end, claims.merged()):
        table = _decode_detached_variables(document, start, end)
        if table is not None:
            tables.append(table)
    return tuple(tables)


__all__ = [
    "SdfByteClaims",
    "claim_document_metadata",
    "discover_detached_variable_tables",
    "iter_gaps",
    "string_size",
]
