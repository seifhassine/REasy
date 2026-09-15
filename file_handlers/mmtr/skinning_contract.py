from __future__ import annotations

from dataclasses import dataclass
import struct


SDF_MAGIC = b"SDF\x00"
DXBC_MAGIC = b"DXBC"


class MmtrContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MmtrProfile:
    name: str
    program_size: int
    vertex_shader_offset: int
    vertex_shader_size_offset: int
    index_semantic: str = "INDEX"
    weight_semantic: str = "WEIGHT"


@dataclass(frozen=True, slots=True)
class MmtrSkinningContract:
    influence_count: int


DMC5_MMTR_PROFILE = MmtrProfile(
    name="DMC5 MMTR 1808168797",
    program_size=0x108,
    vertex_shader_offset=0x10,
    vertex_shader_size_offset=0xBC,
)


class _Reader:
    def __init__(self, data: bytes | bytearray | memoryview, label: str):
        self.data = memoryview(data)
        self.label = label

    def require(self, offset: int, size: int, field: str, *, end: int | None = None) -> None:
        upper = len(self.data) if end is None else end
        stop = offset + size
        if offset < 0 or size < 0 or stop < offset or stop > upper:
            raise MmtrContractError(
                f"{self.label}: {field} range [{offset}, {stop}) exceeds {upper} bytes"
            )

    def u16(self, offset: int, field: str) -> int:
        self.require(offset, 2, field)
        return struct.unpack_from("<H", self.data, offset)[0]

    def u32(self, offset: int, field: str, *, end: int | None = None) -> int:
        self.require(offset, 4, field, end=end)
        return struct.unpack_from("<I", self.data, offset)[0]

    def u64(self, offset: int, field: str) -> int:
        self.require(offset, 8, field)
        return struct.unpack_from("<Q", self.data, offset)[0]

    def ascii(self, offset: int, end: int, field: str) -> str:
        self.require(offset, 1, field, end=end)
        raw = self.data[offset:end].tobytes()
        terminator = raw.find(b"\x00")
        if terminator < 0:
            raise MmtrContractError(f"{self.label}: {field} is not null terminated")
        try:
            return raw[:terminator].decode("ascii")
        except UnicodeDecodeError as exc:
            raise MmtrContractError(f"{self.label}: {field} is not ASCII") from exc


def _dxbc_input_signature(
    reader: _Reader,
    shader_offset: int,
    shader_size: int,
) -> tuple[tuple[str, int], ...]:
    reader.require(shader_offset, shader_size, "vertex shader")
    shader_end = shader_offset + shader_size
    reader.require(shader_offset, 32, "DXBC header", end=shader_end)
    if reader.data[shader_offset : shader_offset + 4].tobytes() != DXBC_MAGIC:
        raise MmtrContractError(f"{reader.label}: vertex shader is not DXBC")

    total_size = reader.u32(shader_offset + 24, "DXBC size", end=shader_end)
    chunk_count = reader.u32(shader_offset + 28, "DXBC chunk count", end=shader_end)
    if total_size < 32 + chunk_count * 4 or total_size > shader_size:
        raise MmtrContractError(f"{reader.label}: invalid DXBC size {total_size}")
    dxbc_end = shader_offset + total_size
    reader.require(
        shader_offset + 32,
        chunk_count * 4,
        "DXBC chunk table",
        end=dxbc_end,
    )

    signature: tuple[int, int] | None = None
    for chunk_index in range(chunk_count):
        relative = reader.u32(
            shader_offset + 32 + chunk_index * 4,
            f"DXBC chunk {chunk_index} offset",
            end=dxbc_end,
        )
        chunk = shader_offset + relative
        reader.require(chunk, 8, f"DXBC chunk {chunk_index}", end=dxbc_end)
        tag = reader.data[chunk : chunk + 4].tobytes()
        size = reader.u32(chunk + 4, f"DXBC chunk {chunk_index} size", end=dxbc_end)
        payload = chunk + 8
        reader.require(payload, size, f"DXBC chunk {chunk_index} payload", end=dxbc_end)
        if tag == b"ISGN":
            signature = (payload, size)

    if signature is None:
        return ()
    payload, size = signature
    stride = 24
    chunk_end = payload + size
    reader.require(payload, 8, "DXBC input signature header", end=chunk_end)
    count = reader.u32(payload, "DXBC input count", end=chunk_end)
    reader.require(
        payload + 8,
        count * stride,
        "DXBC input parameters",
        end=chunk_end,
    )

    semantics = []
    for index in range(count):
        entry = payload + 8 + index * stride
        name_offset = reader.u32(
            entry,
            f"DXBC input {index} name offset",
            end=chunk_end,
        )
        semantic_index = reader.u32(
            entry + 4,
            f"DXBC input {index} semantic index",
            end=chunk_end,
        )
        semantics.append(
            (
                reader.ascii(
                    payload + name_offset,
                    chunk_end,
                    f"DXBC input {index} name",
                ).upper(),
                semantic_index,
            )
        )
    return tuple(semantics)


def _shader_influence_count(
    semantics: tuple[tuple[str, int], ...],
    profile: MmtrProfile,
) -> int | None:
    indices = {
        index for name, index in semantics if name == profile.index_semantic
    }
    weights = {
        index for name, index in semantics if name == profile.weight_semantic
    }
    if not indices and not weights:
        return None
    if indices != weights or 0 not in indices:
        raise MmtrContractError(
            f"{profile.name}: inconsistent skin index/weight shader inputs"
        )
    if indices == {0}:
        return 4
    if indices == {0, 1}:
        return 8
    raise MmtrContractError(
        f"{profile.name}: unsupported skin input semantic indices {sorted(indices)}"
    )


def parse_mmtr_skinning_contract(
    data: bytes | bytearray | memoryview,
    profile: MmtrProfile,
    *,
    label: str = "MMTR",
) -> MmtrSkinningContract:
    reader = _Reader(data, label)
    reader.require(0, 16, "SDF header")
    if reader.data[:4].tobytes() != SDF_MAGIC:
        raise MmtrContractError(f"{label}: invalid SDF magic")

    target_count = reader.u16(4, "target count")
    program_count = reader.u16(6, "program count")
    shader_binary = reader.u64(8, "shader-binary offset")
    if not target_count or not program_count:
        raise MmtrContractError(f"{label}: empty shader program table")
    record_count = target_count * program_count
    table_size = record_count * profile.program_size
    reader.require(16, table_size, "shader program table")
    table_end = 16 + table_size
    if not table_end <= shader_binary < len(reader.data):
        raise MmtrContractError(f"{label}: invalid shader-binary offset {shader_binary}")

    shader_contracts: set[int] = set()
    seen_shaders: set[tuple[int, int]] = set()
    for record_index in range(record_count):
        record = 16 + record_index * profile.program_size
        shader_offset = reader.u64(
            record + profile.vertex_shader_offset,
            f"program {record_index} vertex-shader offset",
        )
        shader_size = reader.u32(
            record + profile.vertex_shader_size_offset,
            f"program {record_index} vertex-shader size",
        )
        if not shader_offset and not shader_size:
            continue
        if not shader_offset or not shader_size:
            raise MmtrContractError(
                f"{label}: program {record_index} has an incomplete vertex shader"
            )
        if shader_offset < shader_binary:
            raise MmtrContractError(
                f"{label}: program {record_index} vertex shader precedes "
                "the shader-binary section"
            )
        shader_key = (shader_offset, shader_size)
        if shader_key in seen_shaders:
            continue
        seen_shaders.add(shader_key)
        influence_count = _shader_influence_count(
            _dxbc_input_signature(reader, shader_offset, shader_size),
            profile,
        )
        if influence_count is not None:
            shader_contracts.add(influence_count)

    if not shader_contracts:
        raise MmtrContractError(f"{label}: no skinned vertex shader was found")
    if len(shader_contracts) != 1:
        raise MmtrContractError(
            f"{label}: mixed skinning contracts {sorted(shader_contracts)}"
        )
    return MmtrSkinningContract(shader_contracts.pop())
