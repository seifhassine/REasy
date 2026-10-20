import re
from typing import Callable

from .shader_graph import (ShaderContractBuffer, ShaderContractResource, ShaderGraphError,
                           _identifier, _hlsl_register, _resource_hlsl_type, _hlsl_variable_declaration,
                           _packoffset, texture_element_type)


def resource_identifier(resource):
    return _identifier(resource.name, prefix=f"REasyResource{resource.bind_point}")


def resource_array_suffix(resource: ShaderContractResource) -> str:
    count = int(resource.bind_count)
    if count == 1:
        return ""
    if count in {0, 0xFFFFFFFF}:
        return "[]"
    if count <= 0 or count > 1_048_576:
        raise ShaderGraphError(f"Resource {resource.name} has an invalid array size.")
    return f"[{count}]"


def resource_declaration(resource: ShaderContractResource, structured_layout: ShaderContractBuffer | None = None, *,
                         identifier: str | None = None, coherent: bool = False, attribute: str = "") -> str:
    identifier = resource_identifier(resource) if identifier is None else identifier
    suffix = resource_array_suffix(resource)
    register = _hlsl_register(
        resource.register_class,
        resource.bind_point,
        resource.register_space,
    )
    coherence = "globallycoherent " if coherent else ""
    if resource.input_type == "texture":
        return f"{attribute}{coherence}{_resource_hlsl_type(resource)} {identifier}{suffix} : {register};"
    if resource.input_type == "raytracing-acceleration-structure":
        return f"RaytracingAccelerationStructure {identifier}{suffix} : {register};"
    if resource.input_type == "sampler":
        kind = "SamplerComparisonState" if int(resource.flags) & 0x2 else "SamplerState"
        return f"{attribute}{kind} {identifier}{suffix} : {register};"
    if resource.input_type == "byte-address-buffer":
        return f"{attribute}{coherence}ByteAddressBuffer {identifier}{suffix} : {register};"
    if resource.input_type in {"structured-buffer", "rw-structured-buffer"}:
        stride = int(resource.sample_count)
        if stride <= 0 or stride % 4:
            raise ShaderGraphError(
                f"Structured buffer {resource.name} has invalid stride {stride}."
            )
        element = _identifier(
            f"REasy_{resource.name}_Element",
            prefix=f"REasyStructured{resource.bind_point}",
        )
        words = stride // 4
        rows = [f"struct {element}", "{"]
        if structured_layout is not None:
            cursor = 0
            for field in sorted(structured_layout.variables, key=lambda v: v.byte_offset):
                if field.byte_offset < cursor or field.byte_offset % 4 or not field.hlsl_declaration:
                    raise ShaderGraphError(f"Structured buffer {resource.name} has an invalid field layout.")
                for offset in range(cursor, field.byte_offset, 4):
                    rows.append(f"    uint REasyReserved_{offset};")
                rows.append(f"    {_hlsl_variable_declaration(field, _identifier(field.name))};")
                cursor = field.byte_offset + field.byte_size
            if cursor > stride:
                raise ShaderGraphError(f"Structured buffer {resource.name} fields exceed its stride.")
            for offset in range(cursor, stride, 4):
                rows.append(f"    uint REasyReserved_{offset};")
        else:
            for slot in range((words + 3) // 4):
                width = min(4, words - slot * 4)
                value_type = "uint" if width == 1 else f"uint{width}"
                rows.append(f"    {value_type} slot{slot};")
        object_type = "StructuredBuffer" if resource.input_type == "structured-buffer" else "RWStructuredBuffer"
        rows.extend(("};", f"{object_type}<{element}> {identifier}{suffix} : {register};"))
        return "\n".join(rows)
    if resource.input_type == "texture-buffer":
        return f"Buffer<{texture_element_type(resource.return_type)}> {identifier}{suffix} : {register};"
    if resource.input_type == "rw-byte-address-buffer":
        return f"{attribute}{coherence}RWByteAddressBuffer {identifier}{suffix} : {register};"
    if resource.input_type == "rw-typed":
        object_type = {
            "buffer": "RWBuffer",
            "texture1d": "RWTexture1D",
            "texture1d-array": "RWTexture1DArray",
            "texture2d": "RWTexture2D",
            "texture2d-array": "RWTexture2DArray",
            "texture3d": "RWTexture3D",
        }.get(resource.dimension)
        if object_type is None:
            raise ShaderGraphError(
                f"RW resource {resource.name} has unsupported dimension {resource.dimension}."
            )
        # 64-bit typed UAVs must be scalar.
        count = ((int(resource.flags) >> 2) & 3) + 1
        element = {"uint64": "uint64_t", "sint64": "int64_t"}.get(resource.return_type) or (
            {"uint": "uint", "sint": "int"}.get(resource.return_type, "float") + (str(count) if count > 1 else ""))
        return f"{object_type}<{element}> {identifier}{suffix} : {register};"
    raise ShaderGraphError(
        f"Resource {resource.name} uses unsupported native kind {resource.input_type}."
    )


def contract_structures(*buffers: ShaderContractBuffer) -> str:
    return "\n".join(contract_structure_blocks(*buffers).values())


def contract_structure_blocks(*buffers: ShaderContractBuffer) -> dict[str, str]:
    definitions: dict[str, tuple[tuple[object, ...], str]] = {}

    # ``visit`` takes itself instead of closing over its own name, which would keep every visited buffer alive until the
    # next GC.
    def visit(variable, recurse) -> None:
        for member in variable.members:
            recurse(member, recurse)
        if not variable.members:
            return
        name = str(variable.struct_name or variable.hlsl_declaration or "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ShaderGraphError(
                f"Constant-buffer aggregate {buffer.name}.{variable.name} has no canonical type name."
            )
        rows = [f"struct {name}", "{"]
        signature = []
        for member in sorted(variable.members, key=lambda item: int(item.byte_offset)):
            declaration = str(member.hlsl_declaration or "").strip()
            identifier = str(member.name)
            if (
                not declaration
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier)
                or int(member.byte_offset) < 0
                or int(member.byte_offset) % 4
                or int(member.byte_size) <= 0
            ):
                raise ShaderGraphError(
                    f"Constant-buffer aggregate field {name}.{identifier} cannot be declared canonically."
                )
            rows.append(f"    {_hlsl_variable_declaration(member, identifier)};")
            signature.append(
                (
                    identifier,
                    declaration,
                    int(member.array_length),
                    bool(member.matrix),
                    str(member.struct_name),
                )
            )
        rows.extend(("};", ""))
        value = (tuple(signature), "\n".join(rows))
        previous = definitions.setdefault(name, value)
        if previous[0] != value[0]:
            raise ShaderGraphError(
                f"Constant-buffer aggregate type {name} has conflicting semantic layouts."
            )

    for buffer in buffers:
        for variable in buffer.variables:
            visit(variable, visit)
    return {name: value[1] for name, value in definitions.items()}


def contract_cbuffer(
    buffer: ShaderContractBuffer,
    native_variable_name: Callable[[ShaderContractBuffer, object], str],
) -> tuple[str, dict[str, str]]:
    if int(buffer.bind_point) < 0 or int(buffer.byte_size) <= 0:
        raise ShaderGraphError(f"Constant buffer {buffer.name} has an invalid contract.")
    buffer_id = resource_identifier(buffer)
    rows = [
        f"cbuffer {buffer_id} : {_hlsl_register('b', buffer.bind_point, buffer.register_space)}",
        "{",
    ]
    identifiers: dict[str, str] = {}
    maximum_end = 0
    occupied_names: set[str] = set()
    for variable in sorted(buffer.variables, key=lambda item: item.byte_offset):
        declaration = str(variable.hlsl_declaration or "").strip()
        identifier = str(native_variable_name(buffer, variable)).strip()
        if (
            not declaration
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier)
            or identifier in occupied_names
            or int(variable.byte_offset) < 0
            or int(variable.byte_offset) % 4
            or int(variable.byte_size) <= 0
        ):
            raise ShaderGraphError(
                f"Constant-buffer field {buffer.name}.{variable.name} cannot be declared canonically."
            )
        rows.append(
            f"    {_hlsl_variable_declaration(variable, identifier)} : packoffset({_packoffset(variable.byte_offset)});"
        )
        identifiers[variable.name] = identifier
        occupied_names.add(identifier)
        maximum_end = max(maximum_end, int(variable.byte_offset) + int(variable.byte_size))
    if maximum_end < int(buffer.byte_size):
        # SPIR-V does not round blocks to 16 bytes, so pad explicitly.
        offset = int(buffer.byte_size) - 4
        padding = f"REasyReserved_s{int(buffer.register_space)}_b{int(buffer.bind_point)}_{offset}"
        rows.append(f"    uint {padding} : packoffset({_packoffset(offset)});")
    rows.extend(("};", ""))
    return "\n".join(rows), identifiers
