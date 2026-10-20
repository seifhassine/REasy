from __future__ import annotations

from bisect import bisect_right
from dataclasses import replace
from hashlib import sha256
import struct

lazy from xxhash import xxh64_intdigest

lazy from ._ownership import SdfByteClaims, claim_document_metadata
lazy from .models import SdfDocument, SdfRuntimeCacheKey


_MASK64 = (1 << 64) - 1
SHADER_CACHE_SEED = 0xFFFFFFFF
XXH64_ALGORITHM = "xxh64-seed-ffffffff"
STATE_IDENTITY_ALGORITHM = "state-identity-xxh64-seed-ffffffff"


def xxh64(data: bytes) -> int:
    return xxh64_intdigest(data, seed=SHADER_CACHE_SEED)


def runtime_cache_key_bytes(
    key: SdfRuntimeCacheKey | None, body: bytes, field: str, *, shader: bool = False,
) -> bytes:
    if key is None:
        return b""
    allowed = {XXH64_ALGORITHM} if shader else {"opaque", STATE_IDENTITY_ALGORITHM}
    if key.algorithm not in allowed:
        kind = "shader" if shader else "state/input-layout"
        raise ValueError(
            f"{field} {kind} runtime cache key requires {' or '.join(sorted(map(repr, allowed)))}; "
            f"{key.algorithm!r} is not verified for this record kind"
        )
    value = int(key.value)
    if not 0 <= value <= _MASK64:
        raise ValueError(f"{field} runtime cache key is not a u64")
    digest = sha256(body).digest()
    if shader:
        expected = xxh64(body)
        if key.source_digest == digest and expected != value:
            raise ValueError(f"{field} runtime cache key disagrees with its unchanged body")
        value = expected
    elif key.source_digest != digest or not key.source_digest:
        value = xxh64(body)
    elif key.algorithm == STATE_IDENTITY_ALGORITHM and value != xxh64(body):
        raise ValueError(f"{field} generated state identity disagrees with its unchanged body")
    return struct.pack("<Q", value)


def shader_runtime_cache_key(body: bytes) -> SdfRuntimeCacheKey:
    return SdfRuntimeCacheKey(xxh64(body), XXH64_ALGORITHM, sha256(body).digest())


def state_runtime_cache_key(body: bytes) -> SdfRuntimeCacheKey:
    return SdfRuntimeCacheKey(xxh64(body), STATE_IDENTITY_ALGORITHM, sha256(body).digest())


def attach_runtime_cache_keys(document: SdfDocument) -> None:
    if not document.profile.runtime_cache_keys:
        return

    claims = SdfByteClaims(len(document.original_data))
    claims.add(0, document.header.program_table_offset + document.header.record_count * document.profile.record_size)
    claim_document_metadata(claims, document)
    shaders = {}
    states = {}
    attributes = ("input_layout", "rasterizer_state", "blend_state", "depth_stencil_state")
    for target in document.targets:
        for program in target.programs:
            for reference in (program.input_signature, *program.shaders):
                if reference.present:
                    shaders.setdefault((reference.offset, reference.size), reference)
                    claims.add(reference.offset, reference.size)
            for attribute in attributes:
                state = getattr(program, attribute)
                if state is not None:
                    states.setdefault((attribute, state.offset), state)
    merged = claims.merged()
    starts = tuple(start for start, _end in merged)
    data = document.original_data

    def required_key(offset, size, field):
        end = offset + size
        index = bisect_right(starts, end) - 1
        if index >= 0 and merged[index][1] > end:
            raise ValueError(f"{field} runtime cache key overlaps another parsed object at 0x{end:X}")
        next_start = starts[index + 1] if index + 1 < len(starts) else len(data)
        if end + 8 > next_start:
            raise ValueError(f"{field} runtime cache key is missing or truncated at 0x{end:X}")
        if offset < document.header.shader_binary_offset and end + 8 > document.header.shader_binary_offset:
            raise ValueError(f"{field} runtime cache key exceeds the metadata region")
        return struct.unpack_from("<Q", data, end)[0]

    shader_keys = {}
    for location, reference in shaders.items():
        value = required_key(*location, reference.role)
        key = shader_runtime_cache_key(reference.payload)
        if value != key.value:
            raise ValueError(
                f"{reference.role} runtime cache key at 0x{reference.offset + reference.size:X} "
                f"is 0x{value:016X}; expected xxHash64 0x{key.value:016X}"
            )
        shader_keys[location] = key

    state_keys = {}
    for location, state in states.items():
        value = required_key(state.offset, len(state.raw), location[0])
        state_keys[location] = SdfRuntimeCacheKey(value, source_digest=sha256(state.raw).digest())

    keyed_values = {}

    def keyed(value, key):
        if key is None:
            return value
        if id(value) not in keyed_values:
            keyed_values[id(value)] = replace(value, runtime_cache_key=key)
        return keyed_values[id(value)]

    def shader(reference):
        return keyed(reference, shader_keys.get((reference.offset, reference.size)))

    def state(attribute, value):
        return None if value is None else keyed(value, state_keys.get((attribute, value.offset)))

    if shader_keys or state_keys:
        document.targets = tuple(
            replace(target, programs=tuple(
                replace(program,
                    input_signature=shader(program.input_signature),
                    shaders=tuple(shader(reference) for reference in program.shaders),
                    **{attribute: state(attribute, getattr(program, attribute)) for attribute in attributes},
                )
                for program in target.programs
            ))
            for target in document.targets
        )


__all__ = [
    "SHADER_CACHE_SEED", "XXH64_ALGORITHM", "attach_runtime_cache_keys",
    "STATE_IDENTITY_ALGORITHM", "runtime_cache_key_bytes", "shader_runtime_cache_key", "state_runtime_cache_key", "xxh64",
]
