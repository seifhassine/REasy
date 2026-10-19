from __future__ import annotations

from dataclasses import replace
from file_handlers.material_names import is_private_material_parameter

from .dxc import DxcError, enrich_dxil_shader
from .models import RESOURCE_KINDS, STAGE_KEYS, SdfProgram
from .profiles import SdfGameProfile
from .shader_graph import (
    ShaderContract,
    ShaderContractBuffer,
    ShaderContractVariable,
)


class SdfContractError(ValueError):
    pass


_RESOURCE_CLASSES = {
    "constant_buffer": frozenset(("constant-buffer",)),
    "sampler": frozenset(("sampler",)),
    "shader_resource": frozenset(
        (
            "texture-buffer",
            "texture",
            "structured-buffer",
            "byte-address-buffer",
            "raytracing-acceleration-structure",
        )
    ),
    "unordered_access": frozenset(
        (
            "rw-typed",
            "rw-structured-buffer",
            "rw-byte-address-buffer",
            "append-structured-buffer",
            "consume-structured-buffer",
            "rw-structured-buffer-counter",
        )
    ),
    "optional_resource": frozenset(
        (
            "texture-buffer",
            "texture",
            "structured-buffer",
            "byte-address-buffer",
            "rw-typed",
            "rw-structured-buffer",
            "rw-byte-address-buffer",
        )
    ),
}


def matches_outer_resource(resource, kind: str, name: str, slot: int) -> bool:
    return (resource.input_type in _RESOURCE_CLASSES[kind]
        and int(resource.bind_point) == slot and int(resource.register_space) == 0
        and (not name or resource.name.casefold() == name.casefold()))


def _active_outer_resources(program: SdfProgram, role: str):
    stage_index = STAGE_KEYS.index(role)
    for kind, metadata, bindings in zip(
        RESOURCE_KINDS,
        program.resource_metadata,
        program.bindings,
    ):
        if len(metadata) != len(bindings):
            raise SdfContractError(
                f"{program.name} {kind} metadata/binding counts disagree"
            )
        for resource, binding in zip(metadata, bindings):
            if role not in binding.active_stages:
                continue
            yield (
                kind,
                resource,
                binding,
                int(binding.bind_slots[stage_index]),
            )


def _reconcile_outer_contract(
    contract: ShaderContract,
    program: SdfProgram,
    role: str,
    profile: SdfGameProfile,
) -> ShaderContract:
    reflected_by_name: dict[str, list] = {}
    for resource in contract.resources:
        reflected_by_name.setdefault(resource.name.casefold(), []).append(resource)
    matched: set[tuple[str, int, int, str]] = set()
    outer_buffers = {item.name.casefold(): item for item in program.constant_buffers}
    for kind, metadata, binding, slot in _active_outer_resources(program, role):
        pool = reflected_by_name.get(metadata.name.casefold(), ()) if metadata.name else contract.resources
        candidates = [item for item in pool if matches_outer_resource(item,kind,metadata.name,slot)]
        if len(candidates) != 1:
            detail = ", ".join(
                f"{item.input_type} {item.register_class}{item.bind_point}"
                for item in reflected_by_name.get(metadata.name.casefold(), ())
            ) or "absent"
            raise SdfContractError(
                f"{program.name} {role} binding {metadata.name!r} is {detail}; "
                f"SDF declares {kind} slot {slot} ({binding.type_name})"
            )
        candidate = candidates[0]
        if int(candidate.bind_count) <= 0:
            raise SdfContractError(
                f"{program.name} {role} binding {metadata.name!r} has no elements"
            )
        matched.add(
            (
                candidate.register_class,
                int(candidate.register_space),
                int(candidate.bind_point),
                candidate.name.casefold(),
            )
        )

    for resource in contract.resources:
        key = (
            resource.register_class,
            int(resource.register_space),
            int(resource.bind_point),
            resource.name.casefold(),
        )
        if key not in matched:
            implicit = (
                role,
                resource.name,
                resource.register_class,
                int(resource.bind_point),
                int(resource.register_space),
            )
            if implicit in profile.implicit_resource_bindings:
                continue
            raise SdfContractError(
                f"{program.name} {role} reflects {resource.name!r} at "
                f"{resource.register_class}{resource.bind_point}, space {resource.register_space}, but the SDF "
                "does not bind it to that stage"
            )

    buffers: list[ShaderContractBuffer] = []
    for buffer in contract.constant_buffers:
        outer = outer_buffers.get(buffer.name.casefold())
        if outer is None:
            binding = next(
                (
                    item
                    for item in contract.resources
                    if item.input_type == "constant-buffer"
                    and item.name.casefold() == buffer.name.casefold()
                    and int(item.bind_point) == int(buffer.bind_point)
                    and int(item.register_space) == int(buffer.register_space)
                ),
                None,
            )
            implicit = (
                role,
                buffer.name,
                "b",
                int(buffer.bind_point),
                int(buffer.register_space),
            )
            if binding is not None and implicit in profile.implicit_resource_bindings:
                buffers.append(buffer)
                continue
            raise SdfContractError(
                f"{program.name} {role} reflects undeclared constant buffer {buffer.name!r}"
            )
        # The outer table may describe fewer entries than the stage uses (DD2/WotS cbOutline: 7 of 15).
        size = max(int(buffer.byte_size), int(outer.byte_size))
        buffers.append(buffer if buffer.byte_size == size else replace(buffer, byte_size=size))
    resolved = tuple(buffers)
    return contract if resolved == contract.constant_buffers else replace(contract, constant_buffers=resolved)


def shader_contract_for_program(
    profile: SdfGameProfile,
    program: SdfProgram,
    role: str,
    *,
    native_contract_cache: dict[str, ShaderContract] | None = None,
) -> ShaderContract:
    key = str(role).strip().lower()
    if key not in STAGE_KEYS:
        raise SdfContractError(f"unknown shader role {role!r}")
    reference = program.shader(key)
    if reference is None or not reference.present:
        raise SdfContractError(f"{program.name} has no {key} shader")
    is_library = reference.raytracing_library is not None
    if reference.binary_format not in {"dxbc-container", "dxil-raytracing-library"} or reference.reflection is None:
        raise SdfContractError(
            f"{program.name} {key} uses {reference.binary_format}; its native "
            "contract needs the SPIR-V path"
        )
    reflected = reference.reflection
    if reflected.reflection is None:
        try:
            reflected = enrich_dxil_shader(reflected, reference.raytracing_library.bytecode if is_library else reference.payload)
        except DxcError as exc:
            raise SdfContractError(
                f"could not reflect {program.name} {key}: {exc}"
            ) from exc
    contract = (
        native_contract_cache.get(reflected.sha256)
        if native_contract_cache is not None else None
    )
    if contract is None:
        contract = ShaderContract.from_shader(reflected)
        if contract.stage == 'compute' and contract.thread_group_size is None:
            from .hlsl import dxbc_thread_group_size
            contract = replace(contract, thread_group_size=dxbc_thread_group_size(reference.payload))
        if native_contract_cache is not None:
            native_contract_cache[reflected.sha256] = contract
    if not (is_library and profile.raytracing_libraries and key == "compute" and contract.stage == "library") and not profile.accepts_shader_stage(key, contract.stage):
        raise SdfContractError(
            f"{program.name} {key} payload declares stage {contract.stage}"
        )
    return _reconcile_outer_contract(contract, program, key, profile)


def material_buffer_contract(
    contract: ShaderContract,
    program: SdfProgram,
    *,
    buffer_names: tuple[str, ...],
    logical_name,
    allow_unreflected_fields: bool = False,
    _indexed_names: bool = False,
) -> ShaderContract:
    wanted = {str(item).casefold() for item in buffer_names}
    outer = {
        item.name.casefold(): item
        for item in program.constant_buffers
        if item.name.casefold() in wanted
    }
    compiler_aliases = None

    def compiler_alias(buffer, variable):
        nonlocal compiler_aliases
        if compiler_aliases is None:
            from .shader_graph import _constant_buffer_identifiers
            authored_buffers = []
            for reflected in contract.constant_buffers:
                authored = outer.get(reflected.name.casefold())
                if authored is None:
                    authored_buffers.append(reflected)
                    continue
                by_layout = {}
                for item in reflected.variables:
                    by_layout.setdefault((int(item.byte_offset), int(item.byte_size)), []).append(item)
                declarations = []
                for item in authored.variables:
                    if is_private_material_parameter(item.name):
                        continue
                    matches = by_layout.get((int(item.byte_offset), int(item.byte_size)), ())
                    declarations.append(replace(matches[0], name=str(item.name)) if len(matches) == 1 else
                                        ShaderContractVariable(str(item.name), int(item.byte_offset),
                                                               int(item.byte_size), None, None))
                authored_buffers.append(replace(reflected, variables=tuple(declarations)))
            compiler_aliases = _constant_buffer_identifiers(
                authored_buffers, reserved=(resource.name for resource in contract.resources))
        return compiler_aliases.get((buffer.name, str(variable.name)))

    result: list[ShaderContractBuffer] = []
    for buffer in contract.constant_buffers:
        metadata = outer.get(buffer.name.casefold())
        if metadata is None:
            result.append(buffer)
            continue
        if int(buffer.byte_size) > int(metadata.byte_size):
            raise SdfContractError(
                f"{program.name} material buffer {buffer.name} declares {buffer.byte_size} bytes; "
                f"its material allocation is only {metadata.byte_size}"
            )
        def field_name(name):
            return str(logical_name(str(name))).casefold()

        reflected_by_key: dict[tuple[str, int, int], list[ShaderContractVariable]] = {}
        reflected_exact = {}
        reflected_by_name = {} if _indexed_names else None
        for variable in buffer.variables:
            name = field_name(variable.name)
            reflected_exact.setdefault((str(variable.name), int(variable.byte_offset),
                                        int(variable.byte_size)), []).append(variable)
            if reflected_by_name is not None:
                reflected_by_name.setdefault(name, []).append(variable)
            reflected_by_key.setdefault(
                (
                    name,
                    int(variable.byte_offset),
                    int(variable.byte_size),
                ),
                [],
            ).append(variable)
        variables: list[ShaderContractVariable] = []
        for variable in metadata.variables:
            if is_private_material_parameter(variable.name):
                continue
            key = (
                str(variable.name).casefold(),
                int(variable.byte_offset),
                int(variable.byte_size),
            )
            candidates = reflected_exact.get((str(variable.name), key[1], key[2]), ())
            if not candidates:
                candidates = reflected_by_key.get(key, ())
            if len(candidates) != 1:
                alias = compiler_alias(buffer, variable)
                if alias is not None:
                    aliased = reflected_exact.get((alias, key[1], key[2]), ())
                    if aliased:
                        candidates = aliased
            if len(candidates) != 1:
                same_name = reflected_by_name.get(key[0], ()) if reflected_by_name is not None else [
                    item for item in buffer.variables if field_name(item.name) == key[0]
                ]
                same_name = [*same_name, *(item for item in buffer.variables
                                         if str(item.name) in {str(variable.name), alias})]
                if allow_unreflected_fields and not same_name:
                    continue
                reflected_layout = ", ".join(
                    f"{item.byte_offset}+{item.byte_size}"
                    for item in same_name
                ) or "absent"
                raise SdfContractError(
                    f"{program.name} material field {buffer.name}.{variable.name} "
                    f"at {variable.byte_offset}+{variable.byte_size} does not have "
                    f"one exact reflected declaration (reflected: {reflected_layout})"
                )
            candidate = candidates[0]
            variables.append(
                candidate if (type(candidate) is ShaderContractVariable
                              and type(candidate.name) is str and candidate.name == str(variable.name)
                              and type(candidate.byte_offset) is int and candidate.byte_offset == key[1]
                              and type(candidate.byte_size) is int and candidate.byte_size == key[2]) else
                replace(candidate, name=str(variable.name), byte_offset=key[1], byte_size=key[2])
            )
        # Shaders also read engine-wide fields the MMTR table does not author (SF6's UserMaterial).
        occupied = [(int(item.byte_offset), int(item.byte_offset) + int(item.byte_size))
                    for item in (*variables, *metadata.variables)]
        taken = {str(item.name).casefold() for item in variables}
        authored = {field_name(item.name) for item in metadata.variables}
        for item in sorted(buffer.variables, key=lambda value: (int(value.byte_offset), str(value.name))):
            start, stop = int(item.byte_offset), int(item.byte_offset) + int(item.byte_size)
            if (is_private_material_parameter(item.name) or field_name(item.name) in authored or stop <= start
                    or str(item.name).startswith(("REasyOpaque_", "REasyReserved_"))
                    or any(start < other_stop and stop > other_start for other_start, other_stop in occupied)):
                continue
            name = str(logical_name(str(item.name)))
            if name.casefold() in taken:
                name = str(item.name)
            if name.casefold() in taken:
                continue
            variables.append(item if name == item.name else replace(item, name=name))
            occupied.append((start, stop))
            taken.add(name.casefold())
        variables.sort(key=lambda item: (int(item.byte_offset), str(item.name).casefold()))
        resolved = tuple(variables)
        result.append(buffer if (type(buffer) is ShaderContractBuffer
                                 and type(buffer.byte_size) is int and buffer.byte_size == int(metadata.byte_size)
                                 and type(buffer.variables) is tuple and len(buffer.variables) == len(resolved)
                                 and all(before is after for before, after in zip(buffer.variables, resolved)))
                      else replace(buffer, byte_size=int(metadata.byte_size), variables=resolved))
    return contract if type(contract) is ShaderContract and type(contract.constant_buffers) is tuple and all(
        before is after for before, after in zip(contract.constant_buffers, result)
    ) else replace(
        contract, constant_buffers=tuple(result),
    )


__all__ = [
    "SdfContractError",
    "material_buffer_contract",
    "shader_contract_for_program",
]
