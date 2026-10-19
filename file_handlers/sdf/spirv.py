from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import struct
from typing import Mapping


SPIRV_MAGIC = 0x07230203


class SpirvError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SpirvBlockMember:
    name: str
    byte_offset: int
    value_type: str
    value_byte_size: int = 0
    array_count: int = 1
    array_stride: int = 0
    matrix_layout: str = ""
    members: tuple["SpirvBlockMember", ...] = ()


@dataclass(frozen=True, slots=True)
class SpirvDescriptor:
    variable_id: int
    name: str
    descriptor_set: int
    binding: int
    descriptor_kind: str
    register_class: str
    dimension: str = "unknown"
    return_type: str = "unknown"
    array_count: int = 1
    byte_size: int = 0
    members: tuple[SpirvBlockMember, ...] = ()


@dataclass(frozen=True, slots=True)
class SpirvInterface:
    variable_id: int
    name: str
    storage_class: str
    location: int | None
    built_in: int | None
    value_type: str


@dataclass(frozen=True, slots=True)
class SpirvReflection:
    version: int
    entry_point: str
    stage: str
    descriptors: tuple[SpirvDescriptor, ...]
    inputs: tuple[SpirvInterface, ...]
    outputs: tuple[SpirvInterface, ...]
    execution_modes: tuple[tuple[int, tuple[int, ...]], ...] = ()

    @property
    def thread_group_size(self) -> tuple[int, int, int] | None:
        sizes = [values for mode, values in self.execution_modes if mode == 17]  # LocalSize
        if not sizes:
            return None
        if len(sizes) != 1 or len(sizes[0]) != 3 or any(n <= 0 for n in sizes[0]):
            raise SpirvError("SPIR-V LocalSize is not one positive three-dimensional group")
        return sizes[0]


_STAGES = {
    0: "vertex",
    1: "hull",
    2: "domain",
    3: "geometry",
    4: "pixel",
    5: "compute",
    5267: "amplification",  # TaskNV
    5268: "mesh",
    5364: "amplification",  # TaskEXT
    5365: "mesh",
}
_DIMENSIONS = {
    0: "texture1d",
    1: "texture2d",
    2: "texture3d",
    3: "texture-cube",
    4: "texture2d",
    5: "texture-buffer",
    6: "subpass-input",
}


def _literal_string(words: tuple[int, ...], start: int) -> tuple[str, int]:
    raw = b"".join(struct.pack("<I", word) for word in words[start:])
    terminator = raw.find(b"\0")
    if terminator < 0:
        raise SpirvError("SPIR-V literal string is unterminated")
    try:
        value = raw[:terminator].decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SpirvError("SPIR-V literal string is not UTF-8") from exc
    return value, (terminator + 1 + 3) // 4


def _one_decoration(
    decorations: Mapping[int, Mapping[int, tuple[int, ...]]],
    target: int,
    decoration: int,
) -> int | None:
    values = decorations.get(int(target), {}).get(int(decoration))
    if values is None:
        return None
    if len(values) != 1:
        raise SpirvError(
            f"SPIR-V decoration {decoration} on ID {target} has invalid operands"
        )
    return int(values[0])


def _spirv_module(module: bytes) -> tuple[bytes, tuple[int, int, int, int, int]]:
    raw = bytes(module)
    if len(raw) < 20 or len(raw) % 4:
        raise SpirvError("SPIR-V module has an invalid byte size")
    header = magic, _version, _generator, bound, schema = struct.unpack_from("<5I", raw)
    if magic != SPIRV_MAGIC or not bound or schema:
        raise SpirvError("SPIR-V module has an invalid header")
    return raw, header


def _spirv_instructions(raw: bytes):
    cursor = 20
    while cursor < len(raw):
        header = struct.unpack_from("<I", raw, cursor)[0]
        word_count, opcode = header >> 16, header & 0xFFFF
        if word_count < 1 or cursor + word_count * 4 > len(raw):
            raise SpirvError(f"malformed SPIR-V instruction at byte {cursor}")
        operands = (
            struct.unpack_from(f"<{word_count - 1}I", raw, cursor + 4)
            if word_count > 1
            else ()
        )
        yield opcode, operands
        cursor += word_count * 4


@lru_cache(maxsize=512)
def reflect_spirv(module: bytes, *, entry_point: str | None = None) -> SpirvReflection:
    raw, (_magic, version, _generator, _bound, _schema) = _spirv_module(module)
    names: dict[int, str] = {}
    member_names: dict[tuple[int, int], str] = {}
    decorations: dict[int, dict[int, tuple[int, ...]]] = {}
    member_decorations: dict[tuple[int, int], dict[int, tuple[int, ...]]] = {}
    types: dict[int, tuple[int, tuple[int, ...]]] = {}
    constants: dict[int, int] = {}
    variables: dict[int, tuple[int, int]] = {}
    entries: dict[str, tuple[int, int, tuple[int, ...]]] = {}
    modes: dict[int, dict[int, tuple[int, ...]]] = {}
    for opcode, operands in _spirv_instructions(raw):
        if opcode == 5 and len(operands) >= 2:  # OpName
            value, _ = _literal_string(operands, 1)
            names[int(operands[0])] = value
        elif opcode == 6 and len(operands) >= 3:  # OpMemberName
            value, _ = _literal_string(operands, 2)
            member_names[(int(operands[0]), int(operands[1]))] = value
        elif opcode == 15 and len(operands) >= 3:  # OpEntryPoint
            name, consumed = _literal_string(operands, 2)
            if name in entries:
                raise SpirvError(f"SPIR-V repeats entry point {name!r}")
            entries[name] = (
                int(operands[0]),
                int(operands[1]),
                tuple(int(value) for value in operands[2 + consumed :]),
            )
        elif opcode in {16, 331}:  # OpExecutionMode / OpExecutionModeId
            if len(operands) < 2:
                raise SpirvError("SPIR-V execution mode has no entry point or mode")
            if opcode == 331:
                raise SpirvError("SPIR-V ID execution modes require specialization semantics")
            function, mode = operands[:2]
            values = tuple(operands[2:])
            previous = modes.setdefault(function, {}).setdefault(mode, values)
            if previous != values:
                raise SpirvError(f"SPIR-V entry point {function} has conflicting mode {mode}")
        elif opcode in {19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 39}:
            if not operands:
                raise SpirvError(f"SPIR-V type opcode {opcode} has no result ID")
            types[int(operands[0])] = (opcode, tuple(int(value) for value in operands[1:]))
        elif opcode in {41, 42}:  # OpConstantTrue / OpConstantFalse
            if len(operands) != 2:
                raise SpirvError("malformed SPIR-V Boolean constant")
            constants[int(operands[1])] = int(opcode == 41)
        elif opcode in {43, 50}:  # OpConstant / OpSpecConstant
            if len(operands) < 3:
                raise SpirvError("malformed SPIR-V scalar constant")
            constants[int(operands[1])] = int(operands[2])
        elif opcode == 59 and len(operands) >= 3:  # OpVariable
            variables[int(operands[1])] = (int(operands[0]), int(operands[2]))
        elif opcode == 71 and len(operands) >= 2:  # OpDecorate
            target, decoration = int(operands[0]), int(operands[1])
            values = tuple(int(value) for value in operands[2:])
            previous = decorations.setdefault(target, {}).setdefault(decoration, values)
            if previous != values:
                raise SpirvError(f"SPIR-V ID {target} has conflicting decoration {decoration}")
        elif opcode == 72 and len(operands) >= 3:  # OpMemberDecorate
            key = (int(operands[0]), int(operands[1]))
            decoration = int(operands[2])
            values = tuple(int(value) for value in operands[3:])
            previous = member_decorations.setdefault(key, {}).setdefault(decoration, values)
            if previous != values:
                raise SpirvError(
                    f"SPIR-V member {key[0]}.{key[1]} has conflicting decoration {decoration}"
                )
    if not entries:
        raise SpirvError("SPIR-V module has no entry point")
    selected = str(entry_point) if entry_point is not None else next(iter(entries))
    if selected not in entries:
        raise SpirvError(f"SPIR-V module has no {selected!r} entry point")
    execution_model, _function_id, interface_ids = entries[selected]
    stage = _STAGES.get(execution_model, f"execution-model-{execution_model}")

    def type_description(type_id: int) -> str:
        declaration = types.get(int(type_id))
        if declaration is None:
            return "unknown"
        opcode, values = declaration
        if opcode == 20:
            return "bool"
        if opcode == 21 and len(values) == 2:
            width, signed = values
            return ("int" if signed else "uint") + ("" if width == 32 else str(width))
        if opcode == 22 and len(values) == 1:
            return "float" if values[0] == 32 else f"float{values[0]}"
        if opcode == 23 and len(values) == 2:
            component, count = values
            return f"{type_description(component)}{count}"
        if opcode == 24 and len(values) == 2:
            column, count = values
            return f"{type_description(column)}x{count}"
        if opcode == 28 and len(values) == 2:
            return f"{type_description(values[0])}[{constants.get(values[1], 0)}]"
        if opcode == 29 and len(values) == 1:
            return f"{type_description(values[0])}[]"
        if opcode == 30:
            return names.get(int(type_id), "struct")
        return names.get(int(type_id), "unknown")

    size_cache: dict[int, int] = {}

    def type_size(type_id: int, *, matrix_stride: int = 0, row_major: bool = False) -> int:
        if type_id in size_cache and not matrix_stride and not row_major:
            return size_cache[type_id]
        declaration = types.get(int(type_id))
        if declaration is None:
            return 0
        opcode, values = declaration
        result = 0
        if opcode in {21, 22} and values:
            result = int(values[0]) // 8
        elif opcode == 20:
            result = 4
        elif opcode == 23 and len(values) == 2:
            result = type_size(values[0]) * int(values[1])
        elif opcode == 24 and len(values) == 2:
            column = types.get(int(values[0]))
            if column is None or column[0] != 23:
                raise SpirvError("Matrix column type is not a vector")
            scalar, rows = column[1]
            columns = int(values[1])
            vector_count, width = (int(rows), columns) if row_major else (columns, int(rows))
            value_bytes = type_size(scalar) * width
            result = (matrix_stride or value_bytes) * (vector_count - 1) + value_bytes
        elif opcode == 28 and len(values) == 2:
            extent = type_size(values[0], matrix_stride=matrix_stride, row_major=row_major)
            stride = _one_decoration(decorations, type_id, 6) or extent
            count = int(constants.get(values[1], 0))
            result = stride * (count - 1) + extent if count else 0
        elif opcode == 30:
            ends = []
            for index, member_type in enumerate(values):
                row = member_decorations.get((type_id, index), {})
                offset_values = row.get(35, ())
                if len(offset_values) != 1:
                    continue
                stride_values = row.get(7, ())
                stride = int(stride_values[0]) if len(stride_values) == 1 else 0
                ends.append(int(offset_values[0]) + type_size(member_type, matrix_stride=stride, row_major=4 in row))
            result = max(ends, default=0)
        if not matrix_stride and not row_major:
            size_cache[type_id] = result
        return result

    def array_shape(type_id: int) -> tuple[int, int]:
        declaration = types.get(int(type_id))
        if declaration is None:
            return int(type_id), 1
        opcode, values = declaration
        if opcode == 28 and len(values) == 2:
            count = int(constants.get(values[1], 0))
            if count <= 0:
                raise SpirvError(f"SPIR-V array type {type_id} has no semantic length")
            return int(values[0]), count
        if opcode == 29 and len(values) == 1:
            return int(values[0]), 0
        return int(type_id), 1

    block_member_cache: dict[int, tuple[SpirvBlockMember, ...]] = {}

    def block_members(struct_type_id: int) -> tuple[SpirvBlockMember, ...]:
        cached = block_member_cache.get(int(struct_type_id))
        if cached is not None:
            return cached
        declaration = types.get(int(struct_type_id))
        if declaration is None or declaration[0] != 30:
            return ()
        values = declaration[1]
        offsets = []
        for index, member_type in enumerate(values):
            row = member_decorations.get((struct_type_id, index), {})
            offset_values = row.get(35, ())
            if len(offset_values) != 1:
                raise SpirvError(
                    f"SPIR-V block member {struct_type_id}.{index} has no exact offset"
                )
            offsets.append((index, int(offset_values[0]), member_type, row))
        result = []
        for index, offset, member_type, row in offsets:
            stride_values = row.get(7, ())
            matrix_stride = int(stride_values[0]) if len(stride_values) == 1 else 0
            natural = type_size(member_type, matrix_stride=matrix_stride, row_major=4 in row)
            base_type_id, array_count = array_shape(member_type)
            array_stride = (
                _one_decoration(decorations, member_type, 6)
                if array_count != 1
                else None
            ) or 0
            base_declaration = types.get(base_type_id)
            base_opcode = base_declaration[0] if base_declaration is not None else 0
            row_major = 4 in row
            column_major = 5 in row
            if row_major and column_major:
                raise SpirvError(
                    f"SPIR-V block member {struct_type_id}.{index} has two matrix layouts"
                )
            matrix_layout = (
                "row-major" if row_major else "column-major" if column_major else ""
            )
            if base_opcode == 24 and not matrix_layout:
                raise SpirvError(
                    f"SPIR-V matrix member {struct_type_id}.{index} has no layout decoration"
                )
            result.append(
                SpirvBlockMember(
                    member_names.get((struct_type_id, index), f"member_{index}"),
                    offset,
                    type_description(base_type_id),
                    value_byte_size=natural,
                    array_count=array_count,
                    array_stride=int(array_stride),
                    matrix_layout=matrix_layout,
                    members=(
                        block_members(base_type_id)
                        if base_opcode == 30
                        else ()
                    ),
                )
            )
        decoded = tuple(result)
        block_member_cache[int(struct_type_id)] = decoded
        return decoded

    descriptors: list[SpirvDescriptor] = []
    for variable_id, (pointer_type_id, storage_class) in variables.items():
        binding = _one_decoration(decorations, variable_id, 33)
        descriptor_set = _one_decoration(decorations, variable_id, 34)
        if binding is None and descriptor_set is None:
            continue
        if binding is None or descriptor_set is None:
            raise SpirvError(f"SPIR-V descriptor ID {variable_id} has an incomplete binding")
        pointer = types.get(pointer_type_id)
        if pointer is None or pointer[0] != 32 or len(pointer[1]) != 2:
            raise SpirvError(f"SPIR-V descriptor ID {variable_id} has an invalid pointer type")
        pointer_storage, value_type_id = pointer[1]
        if int(pointer_storage) != storage_class:
            raise SpirvError(f"SPIR-V descriptor ID {variable_id} changes storage class")
        array_count = 1
        value_type = types.get(value_type_id)
        if value_type is not None and value_type[0] == 28 and len(value_type[1]) == 2:
            value_type_id, count_id = value_type[1]
            array_count = int(constants.get(count_id, 0))
            value_type = types.get(value_type_id)
        elif value_type is not None and value_type[0] == 29 and len(value_type[1]) == 1:
            # OpTypeRuntimeArray matches D3D reflection's BindCount == 0.
            value_type_id = value_type[1][0]
            array_count = 0
            value_type = types.get(value_type_id)
        kind = "unknown"
        register_class = "?"
        dimension = "unknown"
        return_type = "unknown"
        byte_size = 0
        members: list[SpirvBlockMember] = []
        if value_type is not None:
            opcode, values = value_type
            image_type_id = value_type_id
            if opcode == 26:
                kind, register_class = "sampler", "s"
            elif opcode == 27 and len(values) == 1:
                kind, register_class = "texture", "t"
                image_type_id = values[0]
                opcode, values = types.get(image_type_id, (0, ()))
            if opcode == 25 and len(values) >= 7:
                sampled_type, dim, _depth, arrayed, multisampled, sampled, _format = values[:7]
                dimension = _DIMENSIONS.get(dim, f"dimension-{dim}")
                if multisampled:
                    if dim not in {1, 6}:
                        raise SpirvError("SPIR-V multisampled image is neither 2D nor a subpass input")
                    dimension += "-ms"
                if arrayed:
                    dimension += "-array"
                return_type = type_description(sampled_type)
                if kind == "unknown":
                    kind = "texture" if sampled == 1 else "storage-image"
                    register_class = "t" if sampled == 1 else "u"
            elif opcode == 30:
                block = 2 in decorations.get(value_type_id, {})
                buffer_block = 3 in decorations.get(value_type_id, {})
                non_writable = 24 in decorations.get(variable_id, {}) or all(
                    24 in member_decorations.get((value_type_id, index), {})
                    for index in range(len(values))
                )
                if storage_class == 2 and block:
                    kind, register_class = "constant-buffer", "b"
                elif storage_class in {2, 12} or buffer_block:
                    kind = "storage-buffer"
                    register_class = "t" if non_writable else "u"
                byte_size = type_size(value_type_id)
                members.extend(block_members(value_type_id))
        descriptors.append(
            SpirvDescriptor(
                variable_id=variable_id,
                name=names.get(variable_id, names.get(value_type_id, f"id_{variable_id}")),
                descriptor_set=descriptor_set,
                binding=binding,
                descriptor_kind=kind,
                register_class=register_class,
                dimension=dimension,
                return_type=return_type,
                array_count=array_count,
                byte_size=byte_size,
                members=tuple(members),
            )
        )

    interface: list[SpirvInterface] = []
    for variable_id in interface_ids:
        variable = variables.get(variable_id)
        if variable is None:
            raise SpirvError(f"SPIR-V entry-point interface ID {variable_id} is not a variable")
        pointer_type_id, storage_class = variable
        if storage_class not in {1, 3}:
            continue
        pointer = types.get(pointer_type_id)
        if pointer is None or pointer[0] != 32 or len(pointer[1]) != 2:
            raise SpirvError(f"SPIR-V interface ID {variable_id} has an invalid pointer type")
        interface.append(
            SpirvInterface(
                variable_id=variable_id,
                name=names.get(variable_id, f"id_{variable_id}"),
                storage_class="input" if storage_class == 1 else "output",
                location=_one_decoration(decorations, variable_id, 30),
                built_in=_one_decoration(decorations, variable_id, 11),
                value_type=type_description(pointer[1][1]),
            )
        )
    return SpirvReflection(
        version=version,
        entry_point=selected,
        stage=stage,
        descriptors=tuple(sorted(descriptors, key=lambda item: (item.descriptor_set, item.binding, item.name))),
        inputs=tuple(item for item in interface if item.storage_class == "input"),
        outputs=tuple(item for item in interface if item.storage_class == "output"),
        execution_modes=tuple(sorted(modes.get(_function_id, {}).items())),
    )


__all__ = [
    "SPIRV_MAGIC",
    "SpirvBlockMember",
    "SpirvDescriptor",
    "SpirvError",
    "SpirvInterface",
    "SpirvReflection",
    "reflect_spirv",
]
