from __future__ import annotations

import json
from dataclasses import asdict
from .shader_stage_execution import ShaderPrimitiveExecution, ShaderMeshExecution
from .spirv_execution import ShaderVulkanExecution
from .storage_codec import pack_ir_storage, unpack_ir_storage

from file_handlers.sdf.shader_graph import (
    ShaderContract,
    ShaderContractBuffer,
    ShaderContractResource,
    ShaderContractVariable,
    ShaderGraphError,
    ShaderSignatureField,
)


SCHEMA = "reasy.shader-contract"
VERSION = 1
MAX_JSON_BYTES = 4 * 1024 * 1024


def _field(item: ShaderSignatureField) -> dict[str, object]:
    return {
        "semantic": item.semantic_name,
        "index": item.semantic_index,
        "register": item.register,
        "component_type": item.component_type,
        "mask": item.mask,
        "read_write_mask": item.read_write_mask,
        "system_value": item.system_value,
        "stream": item.stream,
        "array_length": item.array_length,
        "interpolation": item.interpolation,
    }


def variable_to_dict(item: ShaderContractVariable) -> dict[str, object]:
    return {
        "name": item.name,
        "byte_offset": item.byte_offset,
        "byte_size": item.byte_size,
        "hlsl_declaration": item.hlsl_declaration,
        "graph_type": item.graph_type,
        "array_length": item.array_length,
        "matrix": item.matrix,
        "struct_name": item.struct_name,
        "members": [variable_to_dict(value) for value in item.members],
    }


def _buffer(item: ShaderContractBuffer) -> dict[str, object]:
    return {
        "name": item.name,
        "byte_size": item.byte_size,
        "bind_point": item.bind_point,
        "bind_count": item.bind_count,
        "register_space": item.register_space,
        "variables": [variable_to_dict(value) for value in item.variables],
    }


_EXECUTIONS = (("primitive_execution", ShaderPrimitiveExecution), ("mesh_execution", ShaderMeshExecution),
               ("vulkan_execution", ShaderVulkanExecution))


def contract_to_data(contract: ShaderContract) -> dict[str, object]:
    return {
        **{name: asdict(getattr(contract, name)) for name, _type in _EXECUTIONS if getattr(contract, name) is not None},
        "schema": SCHEMA,
        "version": VERSION,
        "stage": contract.stage,
        "model": [contract.model_major, contract.model_minor],
        "inputs": [_field(item) for item in contract.inputs],
        "outputs": [_field(item) for item in contract.outputs],
        "patch_constants": [_field(item) for item in contract.patch_constants],
        "thread_group_size": list(contract.thread_group_size) if contract.thread_group_size is not None else None,
        "resources": [
            {
                "name": item.name,
                "input_type": item.input_type,
                "return_type": item.return_type,
                "dimension": item.dimension,
                "sample_count": item.sample_count,
                "bind_point": item.bind_point,
                "bind_count": item.bind_count,
                "flags": item.flags,
                "register_space": item.register_space,
                "resource_id": item.resource_id,
            }
            for item in contract.resources
        ],
        "constant_buffers": [_buffer(item) for item in contract.constant_buffers],
        "structured_buffers": [_buffer(item) for item in contract.structured_buffers],
    }


def contract_to_storage(contract: ShaderContract) -> str:
    raw = json.dumps(
        contract_to_data(contract),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(raw) > MAX_JSON_BYTES:
        raise ShaderGraphError("Recovered shader contract is too large.")
    return pack_ir_storage(raw, level=9)


def _contract_from_data(value: object, source_sha256: str) -> ShaderContract:
    if (
        not isinstance(value, dict)
        or value.get("schema") != SCHEMA
        or int(value.get("version", -1)) != VERSION
    ):
        raise ShaderGraphError("Recovered shader contract is invalid.")

    def text(item: object, *, maximum: int = 256, empty: bool = False) -> str:
        result = str(item)
        if (not result and not empty) or len(result) > maximum:
            raise ShaderGraphError("Recovered shader contract is invalid.")
        return result

    def number(
        item: object,
        *,
        minimum: int = 0,
        maximum: int = 0xFFFFFFFF,
    ) -> int:
        result = int(item)
        if not minimum <= result <= maximum:
            raise ShaderGraphError("Recovered shader contract is invalid.")
        return result

    def sequence(container: object, name: str, maximum: int) -> list[object]:
        if not isinstance(container, dict):
            raise ShaderGraphError("Recovered shader contract is invalid.")
        result = container.get(name, [])
        if not isinstance(result, list) or len(result) > maximum:
            raise ShaderGraphError("Recovered shader contract is invalid.")
        return result

    def fields(name: str, maximum: int) -> tuple[ShaderSignatureField, ...]:
        result = []
        for item in sequence(value, name, maximum):
            if not isinstance(item, dict):
                raise ShaderGraphError("Recovered shader signature is invalid.")
            register = number(item.get("register", 0))
            # D3D reflection uses UINT_MAX for system values without a signature register.
            if register > 255 and register != 0xFFFFFFFF:
                raise ShaderGraphError("Recovered shader signature register is invalid.")
            result.append(
                ShaderSignatureField(
                    semantic_name=text(item.get("semantic", "")),
                    semantic_index=number(item.get("index", 0), maximum=255),
                    register=register,
                    component_type=text(
                        item.get("component_type", ""), maximum=32
                    ),
                    mask=number(item.get("mask", 0), maximum=15),
                    read_write_mask=number(
                        item.get("read_write_mask", 0), maximum=15
                    ),
                    system_value=text(
                        item.get("system_value", ""), maximum=64
                    ),
                    stream=number(item.get("stream", 0), maximum=3),
                    array_length=number(item.get("array_length", 1), minimum=1, maximum=65536),
                    interpolation=text(item.get("interpolation", ""), maximum=64, empty=True),
                )
            )
        return tuple(result)

    def variables(
        container: object,
        depth: int = 0,
    ) -> tuple[ShaderContractVariable, ...]:
        if depth > 16:
            raise ShaderGraphError("Recovered shader variable nesting is invalid.")
        result = []
        for item in sequence({"items": container}, "items", 8192):
            if not isinstance(item, dict):
                raise ShaderGraphError("Recovered shader variable is invalid.")
            declaration = item.get("hlsl_declaration")
            graph_type = item.get("graph_type")
            result.append(
                ShaderContractVariable(
                    name=text(item.get("name", "")),
                    byte_offset=number(item.get("byte_offset", 0)),
                    byte_size=number(item.get("byte_size", 0)),
                    hlsl_declaration=(
                        None
                        if declaration is None
                        else text(declaration, maximum=128)
                    ),
                    graph_type=(
                        None
                        if graph_type is None
                        else text(graph_type, maximum=32)
                    ),
                    array_length=number(
                        item.get("array_length", 1), minimum=1, maximum=8192
                    ),
                    matrix=bool(item.get("matrix", False)),
                    struct_name=text(item.get("struct_name", ""), maximum=128, empty=True),
                    members=variables(item.get("members", []), depth + 1),
                )
            )
        return tuple(result)

    def buffers(name: str) -> tuple[ShaderContractBuffer, ...]:
        result = []
        for item in sequence(value, name, 512):
            if not isinstance(item, dict):
                raise ShaderGraphError("Recovered shader buffer is invalid.")
            result.append(
                ShaderContractBuffer(
                    name=text(item.get("name", "")),
                    byte_size=number(item.get("byte_size", 0)),
                    bind_point=number(item.get("bind_point", 0), maximum=4095),
                    bind_count=number(
                        # D3D reflection reports zero for an unbounded descriptor array.
                        item.get("bind_count", 1)
                    ),
                    variables=variables(item.get("variables", [])),
                    register_space=number(item.get("register_space", 0)),
                )
            )
        return tuple(result)

    model = value.get("model", [])
    if not isinstance(model, list) or len(model) != 2:
        raise ShaderGraphError("Recovered shader model is invalid.")
    resources = []
    for item in sequence(value, "resources", 2048):
        if not isinstance(item, dict):
            raise ShaderGraphError("Recovered shader resource is invalid.")
        resources.append(
            ShaderContractResource(
                name=text(item.get("name", ""), empty=True),
                input_type=text(item.get("input_type", ""), maximum=64),
                return_type=text(item.get("return_type", ""), maximum=64),
                dimension=text(item.get("dimension", ""), maximum=64),
                sample_count=number(item.get("sample_count", 0)),
                bind_point=number(item.get("bind_point", 0)),
                bind_count=number(item.get("bind_count", 1)),
                flags=number(item.get("flags", 0)),
                register_space=number(item.get("register_space", 0)),
                resource_id=number(item.get("resource_id", 0)),
            )
        )
    dimensions = value.get('thread_group_size')
    if dimensions is not None:
        if not isinstance(dimensions, list) or len(dimensions) != 3 or any(type(n) is not int or not 1 <= n <= 1024 for n in dimensions):
            raise ShaderGraphError('Recovered thread-group dimensions are invalid.')
        dimensions = tuple(dimensions)
    contract = ShaderContract(
        stage=text(value.get("stage", ""), maximum=16),
        model_major=number(model[0], minimum=4, maximum=6),
        model_minor=number(model[1], maximum=9),
        inputs=fields("inputs", 512),
        outputs=fields("outputs", 64),
        resources=tuple(resources),
        constant_buffers=buffers("constant_buffers"),
        source_sha256=str(source_sha256),
        structured_buffers=buffers("structured_buffers"),
        patch_constants=fields("patch_constants", 128),
        thread_group_size=dimensions,
        **{name: kind(**value[name]) if value.get(name) is not None else None for name, kind in _EXECUTIONS},
    )
    # Raises for a stage with no shader-model target.
    contract.target
    if contract.primitive_execution is not None:
        contract.primitive_execution.validate_stage(contract.stage)
    if contract.mesh_execution is not None:
        contract.mesh_execution.validate_stage(contract.stage)
    return contract


def contract_from_storage(text: object, source_sha256: str) -> ShaderContract:
    try:
        value = json.loads(
            unpack_ir_storage(text, MAX_JSON_BYTES, "Recovered shader contract data")
        )
    except json.JSONDecodeError as exc:
        raise ShaderGraphError("Recovered shader contract is invalid.") from exc
    return _contract_from_data(value, source_sha256)


__all__ = [
    "SCHEMA",
    "VERSION",
    "contract_from_storage",
    "contract_to_data",
    "contract_to_storage",
]
