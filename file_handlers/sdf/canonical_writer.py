from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
import struct

lazy from utils.hex_util import align

lazy from .models import RESOURCE_KINDS, STAGE_KEYS, SDF_MAGIC, SdfDocument, SdfProgram
lazy from .raytracing import serialize_raytracing_library
lazy from .runtime_cache import runtime_cache_key_bytes, shader_runtime_cache_key, state_runtime_cache_key
lazy from .sdf_file import SdfFile
lazy from .spirv import SpirvError, _spirv_module


class SdfCanonicalWriteError(ValueError):
    pass


def _bounded(value: int, minimum: int, maximum: int, field: str, error: type = SdfCanonicalWriteError) -> int:
    result = int(value)
    if not minimum <= result <= maximum:
        raise error(f"{field} must be in [{minimum}, {maximum}], got {result}")
    return result


def _ascii_name(value: object, field: str, *, allow_empty: bool = False,
                error: type = SdfCanonicalWriteError) -> str:
    name = str(value)
    if (not name and not allow_empty) or "\x00" in name:
        qualifier = "contain no NUL" if allow_empty else "be non-empty and contain no NUL"
        raise error(f"{field} must {qualifier}")
    try:
        name.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise error(f"{field} must contain only ASCII") from exc
    return name


def _selector(table: tuple[int, ...], public_value: int, field: str, original: int,
              error: type = SdfCanonicalWriteError) -> int:
    value = int(public_value)
    if 0 <= original < len(table) and table[original] == value:
        return original
    try:
        return table.index(value)
    except ValueError as exc:
        raise error(f"{field} value {value} has no encoding in this game profile") from exc


# Packed render-state fields: (attribute, bit, mask, enum table or None, error name). Fields with a table store the
# enum's table index; others their value, or truth at mask 1.
_RASTERIZER_FIELDS = (
    ("front_counter_clockwise", 0, 0x1, None, "front counter-clockwise"),
    ("depth_clip_enable", 1, 0x1, None, "depth clip"),
    ("scissor_enable", 2, 0x1, None, "scissor"),
    ("multisample_enable", 3, 0x1, None, "multisample"),
    ("antialiased_line_enable", 4, 0x1, None, "antialiased lines"),
    ("fill_mode_id", 5, 0x3, "fill_mode_table", "fill mode"),
    ("cull_mode_id", 7, 0x3, "cull_mode_table", "cull mode"),
    ("conservative_rasterization", 9, 0x1, None, "conservative rasterization"),
)
_BLEND_TARGET_FIELDS = (
    ("blend_enable", 0, 0x1, None, "blend enable"),
    ("source_blend_id", 1, 0x1F, "blend_factor_table", "source factor"),
    ("destination_blend_id", 6, 0x1F, "blend_factor_table", "destination factor"),
    ("blend_operation_id", 11, 0x7, "blend_operation_table", "operation"),
    ("source_alpha_blend_id", 14, 0x1F, "blend_factor_table", "source-alpha factor"),
    ("destination_alpha_blend_id", 19, 0x1F, "blend_factor_table", "destination-alpha factor"),
    ("alpha_blend_operation_id", 24, 0x7, "blend_operation_table", "alpha operation"),
    ("write_mask", 27, 0xF, None, "write mask"),
)
_BLEND_FLAGS = (
    ("alpha_to_coverage_enable", 0, 0x1, None, "alpha to coverage"),
    ("independent_blend_enable", 1, 0x1, None, "independent blend"),
)
_DEPTH_STENCIL_FIELDS = (
    ("depth_enable", 0, 0x1, None, "depth enable"),
    ("depth_write_mask_id", 1, 0x3, "depth_write_mask_table", "depth write mask"),
    ("depth_function_id", 3, 0xF, "comparison_function_table", "depth comparison function"),
    ("stencil_enable", 7, 0x1, None, "stencil enable"),
    ("stencil_read_mask", 8, 0xFF, None, "stencil read mask"),
    ("stencil_write_mask", 16, 0xFF, None, "stencil write mask"),
    ("depth_bounds_test_enable", 56, 0x1, None, "depth bounds test"),
)
# Each stencil face's fields, relative to the face's first bit in the depth/stencil word.
_STENCIL_FACE_FIELDS = (
    ("stencil_fail_operation_id", 0, 0xF, "stencil_operation_table", "fail operation"),
    ("stencil_depth_fail_operation_id", 4, 0xF, "stencil_operation_table", "depth-fail operation"),
    ("stencil_pass_operation_id", 8, 0xF, "stencil_operation_table", "pass operation"),
    ("stencil_function_id", 12, 0xF, "comparison_function_table", "comparison function"),
)
_STENCIL_FACES = (("front_face", 24, "front stencil"), ("back_face", 40, "back stencil"))


def _pack_fields(packed: int, fields, profile, value, label: str = "", *, base: int = 0,
                 error: type = SdfCanonicalWriteError) -> int:
    for attribute, bit, mask, table, name in fields:
        shift = base + bit
        requested = value(attribute)
        if mask == 1 and table is None:
            encoded = int(bool(requested))
        elif table is None:
            encoded = _bounded(requested, 0, mask, f"{label} {name}" if label else name, error)
        else:
            encoded = _selector(getattr(profile, table), requested, f"{label} {name}" if label else name,
                                (packed >> shift) & mask, error)
        packed = (packed & ~(mask << shift)) | (encoded << shift)
    return packed


def _named_id(
    names: Mapping[int, str],
    value: str,
    fallback_id: int,
    field: str,
) -> int:
    key = str(value).casefold()
    matches = [int(index) for index, name in names.items() if name.casefold() == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        fallback = int(fallback_id)
        if fallback in matches:
            return fallback
        raise SdfCanonicalWriteError(f"{field} name {value!r} is ambiguous")
    raise SdfCanonicalWriteError(f"{field} name {value!r} is not registered")


def _semantic_id(profile, semantic: str, field: str) -> int:
    return _bounded(
        _named_id(profile.semantic_names, semantic, -1, field),
        0,
        0xFF,
        field,
    )


def _format_id(profile, element, field: str) -> int:
    public_id = int(element.dxgi_format_id)
    public_name = str(element.dxgi_format).casefold()
    matches = [
        int(index)
        for index, (dxgi_id, dxgi_name) in profile.input_formats.items()
        if int(dxgi_id) == public_id and str(dxgi_name).casefold() == public_name
    ]
    if len(matches) != 1:
        raise SdfCanonicalWriteError(
            f"{field} DXGI format {element.dxgi_format!r} ({public_id}) "
            "does not have one canonical internal encoding"
        )
    return _bounded(matches[0], 0, 0xFF, field)


def _resource_type_code(profile, binding, field: str) -> int:
    table = profile.resource_type_names.get(binding.resource_kind, {})
    name = str(binding.type_name).casefold()
    matches = [int(code) for code, label in table.items() if label.casefold() == name]
    if len(matches) != 1:
        raise SdfCanonicalWriteError(
            f"{field} type {binding.type_name!r} does not have one canonical encoding"
        )
    return _bounded(matches[0], 0, 0xFF, field)


def _topology_id(profile, program) -> int:
    compute = program.shader("compute")
    if compute and compute.present:
        if int(program.primitive_topology_id):
            raise SdfCanonicalWriteError(f"{program.name}: compute topology/wave-limit word must be zero")
        return 0
    return _bounded(_named_id(
        profile.primitive_topology_names, program.primitive_topology,
        program.primitive_topology_id, f"{program.name} primitive topology",
    ), 0, 0xFFFF, "primitive topology")


def _input_layout_key(profile, layout) -> tuple:
    if layout is None:
        return ()
    if int(layout.reserved):
        raise SdfCanonicalWriteError("input-layout reserved dword must be zero")
    result = []
    for index, element in enumerate(layout.elements):
        if bool(element.per_instance) != bool(element.instance_step_rate):
            raise SdfCanonicalWriteError(
                f"input element {index} instance classification is inconsistent"
            )
        if int(element.instance_step_rate) not in {0, 1}:
            raise SdfCanonicalWriteError(
                "the SDF input ABI represents only per-vertex or step-rate-one inputs"
            )
        result.append(
            (
                _semantic_id(profile, element.semantic, f"input element {index} semantic"),
                _format_id(profile, element, f"input element {index} format"),
                _bounded(element.input_slot, 0, 0xFF, f"input element {index} slot"),
                _bounded(
                    element.semantic_index,
                    0,
                    0xFF,
                    f"input element {index} semantic index",
                ),
                _bounded(
                    element.aligned_byte_offset,
                    0,
                    0x7FFFFFFF,
                    f"input element {index} byte offset",
                ),
                bool(element.per_instance),
            )
        )
    return tuple(result)


def _input_element_bytes(semantic: int, fmt: int, slot: int, semantic_index: int, byte_offset: int,
                         per_instance: bool) -> bytes:
    return struct.pack("<4BI", semantic, fmt, slot, semantic_index, byte_offset | (0x80000000 if per_instance else 0))


def _input_layout_bytes(profile, layout) -> bytes:
    key = _input_layout_key(profile, layout)
    result = bytearray(struct.pack("<II", len(key), 0))
    for element in key:
        result.extend(_input_element_bytes(*element))
    return result.take_bytes()


def _raster_bytes(profile, state) -> bytes:
    if state is None:
        return b""
    if int(state.reserved_bits):
        raise SdfCanonicalWriteError("rasterizer state has unsupported reserved bits")
    if state.conservative_rasterization and not profile.conservative_rasterization_supported:
        raise SdfCanonicalWriteError("Conservative rasterization is not verified for this build")
    flags = _pack_fields(0, _RASTERIZER_FIELDS, profile, partial(getattr, state))
    if not profile.accepts_binner_control(state.binner_control):
        raise SdfCanonicalWriteError("Rasterizer binner control is not a state of this build")
    flags |= _bounded(
        state.binner_control,
        0,
        (1 << profile.rasterizer_binner_control_bits) - 1,
        "rasterizer binner control",
    ) << profile.rasterizer_binner_control_shift
    return struct.pack(
        "<Ii2f",
        flags,
        int(state.depth_bias),
        float(state.depth_bias_clamp),
        float(state.slope_scaled_depth_bias),
    )


def _blend_bytes(profile, state) -> bytes:
    if state is None:
        return b""
    if int(state.reserved_flags):
        raise SdfCanonicalWriteError("blend state has unsupported reserved flags")
    if len(state.render_targets) != 8:
        raise SdfCanonicalWriteError("blend state must contain eight render targets")
    result = bytearray()
    for index, target in enumerate(state.render_targets):
        if int(target.reserved_bits):
            raise SdfCanonicalWriteError(
                f"blend target {index} has the unsupported high bit set"
            )
        packed = _pack_fields(0, _BLEND_TARGET_FIELDS, profile, partial(getattr, target), f"blend target {index}")
        result.extend(struct.pack("<I", packed))
    result.extend(struct.pack("<I", _pack_fields(0, _BLEND_FLAGS, profile, partial(getattr, state))))
    return result.take_bytes()


def _depth_bytes(profile, state) -> bytes:
    if state is None:
        return b""
    if int(state.reserved_bits):
        raise SdfCanonicalWriteError("depth/stencil state has unsupported high bits")
    packed = _pack_fields(0, _DEPTH_STENCIL_FIELDS, profile, partial(getattr, state))
    for attribute, base, label in _STENCIL_FACES:
        packed = _pack_fields(packed, _STENCIL_FACE_FIELDS, profile, partial(getattr, getattr(state, attribute)),
                              label, base=base)
    return struct.pack("<Q", packed)


def _compact_key(profile, declarations) -> tuple:
    result = []
    for index, declaration in enumerate(declarations):
        semantic = _semantic_id(
            profile,
            declaration.semantic,
            f"compact declaration {index} semantic",
        )
        semantic = _bounded(semantic, 0, 0x0F, f"compact declaration {index} semantic")
        semantic_index = _bounded(
            declaration.semantic_index,
            0,
            0x0F,
            f"compact declaration {index} semantic index",
        )
        register = _bounded(
            declaration.register,
            -0x4000,
            0x3FFF,
            f"compact declaration {index} register",
        )
        if int(declaration.register_low_bit):
            raise SdfCanonicalWriteError(
                f"compact declaration {index} has the unused register-selector flag set"
            )
        component = _named_id(
            profile.component_type_names,
            declaration.component_type,
            declaration.component_type_id,
            f"compact declaration {index} component type",
        )
        system_value = _named_id(
            profile.system_value_names,
            declaration.system_value,
            declaration.system_value_id,
            f"compact declaration {index} system value",
        )
        result.append(
            (
                semantic | (semantic_index << 4),
                (register << 1) & 0xFFFF,
                _bounded(component, 0, 7, "component type"),
                _bounded(system_value, 0, 0x1F, "system value"),
            )
        )
    return tuple(result)


def _compact_bytes(key) -> bytes:
    return b"".join(
        struct.pack("<BHB", semantic, register, system | (component << 5))
        for semantic, register, component, system in key
    )


def _binding_key(profile, bindings, kind: str) -> tuple:
    result = []
    for index, binding in enumerate(bindings):
        if binding.resource_kind != kind:
            raise SdfCanonicalWriteError(
                f"{kind} binding {index} is tagged {binding.resource_kind!r}"
            )
        slots = tuple(
            _bounded(slot, 0, 0xFF, f"{kind} binding {index} slot")
            for slot in binding.bind_slots
        )
        if len(slots) != len(STAGE_KEYS):
            raise SdfCanonicalWriteError(f"{kind} binding {index} needs six stage slots")
        mask = _bounded(binding.stage_mask, 0, 0x3F, f"{kind} binding {index} stage mask")
        active = tuple(STAGE_KEYS[stage] for stage in range(6) if mask & (1 << stage))
        if tuple(binding.active_stages) != active:
            raise SdfCanonicalWriteError(
                f"{kind} binding {index} active stages disagree with its mask"
            )
        result.append(
            (
                slots,
                mask,
                _resource_type_code(profile, binding, f"{kind} binding {index}"),
            )
        )
    return tuple(result)


def _binding_bytes(key) -> bytes:
    return b"".join(
        bytes((*slots, mask, type_code))
        for slots, mask, type_code in key
    )


def _variable_key(variables, *, allow_64k=False) -> tuple:
    result = []
    for index, variable in enumerate(variables):
        name = _ascii_name(variable.name, f"constant variable {index} name")
        byte_offset = _bounded(variable.byte_offset, 0, 0xFFFF, f"{name} byte offset")
        byte_size = _bounded(variable.byte_size, 1, 0x10000 if allow_64k else 0xFFFF, f"{name} byte size")
        if byte_size == 0x10000 and byte_offset != 0:
            raise SdfCanonicalWriteError(f"{name}: a 64-KiB field must start at zero")
        result.append((name, byte_offset, byte_size))
    return tuple(result)


def _constant_buffer_key(buffers, variable_key=_variable_key) -> tuple:
    result = []
    for index, buffer in enumerate(buffers):
        if int(buffer.reserved):
            raise SdfCanonicalWriteError(
                f"constant buffer {index} reserved dword must be zero"
            )
        name = _ascii_name(buffer.name, f"constant buffer {index} name")
        byte_size = _bounded(buffer.byte_size, 1, 0xFFFFFFFF, f"{name} byte size")
        variables = variable_key(buffer.variables)
        for variable_name, offset, size in variables:
            if offset + size > byte_size:
                raise SdfCanonicalWriteError(
                    f"{name}.{variable_name} ends beyond its {byte_size}-byte buffer"
                )
        result.append((name, byte_size, variables))
    return tuple(result)


def _named_resource_key(resources, kind: str) -> tuple:
    result = []
    for index, resource in enumerate(resources):
        if resource.resource_kind != kind:
            raise SdfCanonicalWriteError(
                f"{kind} metadata {index} is tagged {resource.resource_kind!r}"
            )
        if int(resource.reserved):
            raise SdfCanonicalWriteError(f"{kind} metadata {index} reserved dword must be zero")
        # Some VFX routes bind an engine SRV by slot only: empty name, Murmur3 hash of the empty string.
        result.append(
            _ascii_name(
                resource.name,
                f"{kind} metadata {index} name",
                allow_empty=True,
            )
        )
    return tuple(result)


def _requirements_byte(profile, program) -> int:
    reverse: dict[str, int] = {}
    for bit, name in profile.program_requirement_names.items():
        key = str(name).casefold()
        if key in reverse:
            raise SdfCanonicalWriteError(f"shader requirement {name!r} has two bits")
        reverse[key] = int(bit)
    value = 0
    for name in program.shader_requirements:
        bit = reverse.get(str(name).casefold())
        if bit is None:
            raise SdfCanonicalWriteError(
                f"shader requirement {name!r} is not registered for {profile.display_name}"
            )
        value |= bit
    if int(program.unknown_shader_requirement_bits):
        raise SdfCanonicalWriteError("program has unknown compact shader-requirement bits")
    return _bounded(value, 0, 0xFF, "shader requirements")


def _shader_payload(profile, reference, field: str, *, target_index: int) -> bytes:
    if not reference.present:
        if reference.payload:
            raise SdfCanonicalWriteError(f"{field} has payload bytes but is absent")
        return b""
    if target_index not in range(len(profile.targets)):
        raise SdfCanonicalWriteError(f"{field} has an unsupported target index")
    if reference.binary_format == "dxil-raytracing-library":
        if not profile.raytracing_libraries or reference.role != "compute" or reference.raytracing_library is None:
            raise SdfCanonicalWriteError(f"{field} has no supported semantic DXR library")
        try:
            return serialize_raytracing_library(
                reference.raytracing_library, export_layout=profile.raytracing_export_layout,
            )
        except ValueError as exc:
            raise SdfCanonicalWriteError(f"{field}: {exc}") from exc
    if reference.binary_format == "spirv-input-signature":
        if target_index not in profile.spirv_target_indices:
            raise SdfCanonicalWriteError(f"{field} is on a non-SPIR-V target")
        signature = reference.spirv_input_signature
        if signature is None:
            raise SdfCanonicalWriteError(f"{field} lacks decoded SPIR-V inputs")
        scalar_types = {
            "unsigned-integer": 4,
            "floating-point": 8,
        }
        result = bytearray()
        locations: set[int] = set()
        variables: set[int] = set()
        for index, element in enumerate(signature.elements):
            semantic = _semantic_id(profile, element.semantic, f"{field} input {index} semantic")
            scalar = scalar_types.get(str(element.scalar_type))
            if scalar is None:
                raise SdfCanonicalWriteError(
                    f"{field} input {index} has unsupported scalar type {element.scalar_type!r}"
                )
            location = _bounded(element.location, 0, 0xFF, f"{field} input {index} location")
            variable = _bounded(element.variable_id, 1, 0xFF, f"{field} input {index} variable ID")
            if location in locations or variable in variables:
                raise SdfCanonicalWriteError(f"{field} has duplicate SPIR-V input IDs")
            locations.add(location)
            variables.add(variable)
            component_count = _bounded(
                element.component_count,
                1,
                4,
                f"{field} input {index} component count",
            )
            if bool(element.is_vector) != (component_count > 1):
                raise SdfCanonicalWriteError(
                    f"{field} input {index} vector flag/component count disagree"
                )
            result.extend(
                struct.pack(
                    "<8B",
                    semantic,
                    _bounded(element.semantic_index, 0, 0xFF, f"{field} semantic index"),
                    location,
                    variable,
                    scalar,
                    int(bool(element.is_vector)),
                    0,
                    0,
                )
            )
        return result.take_bytes()
    if reference.binary_format == "spirv-module":
        if target_index not in profile.spirv_target_indices:
            raise SdfCanonicalWriteError(f"{field} is on a non-SPIR-V target")
        spirv = reference.spirv
        if spirv is None:
            raise SdfCanonicalWriteError(f"{field} lacks a decoded SPIR-V module")
        try:
            module, (_magic, version, generator, bound, schema) = _spirv_module(spirv.module)
        except SpirvError as exc:
            raise SdfCanonicalWriteError(f"{field}: {exc}") from exc
        if (version, generator, bound, schema) != (
            int(spirv.version), int(spirv.generator), int(spirv.bound), int(spirv.schema),
        ):
            raise SdfCanonicalWriteError(f"{field} SPIR-V header disagrees with its model")
        entry_point = _ascii_name(spirv.entry_point, f"{field} entry point")
        if spirv.execution_model_name != reference.role:
            raise SdfCanonicalWriteError(f"{field} execution model disagrees with its role")
        return struct.pack("<I", len(module)) + module + entry_point.encode("ascii") + b"\0"

    payload = bytes(reference.payload)
    if not payload:
        raise SdfCanonicalWriteError(f"{field} is present without semantic payload bytes")
    if payload[:4] == b"DXBC":
        if reference.binary_format not in {"dxbc-container", ""}:
            raise SdfCanonicalWriteError(f"{field} DXBC payload has an inconsistent format tag")
        return payload
    raise SdfCanonicalWriteError(f"{field} has an unsupported compiled payload format")


def _cache_key_bytes(profile, value, body: bytes, field: str, *, shader: bool = False) -> bytes:
    key = value.runtime_cache_key
    if not profile.runtime_cache_keys:
        if key is not None:
            raise SdfCanonicalWriteError(f"{field} has a runtime cache key in a build without that field")
        return b""
    if key is None:
        key = shader_runtime_cache_key(body) if shader else state_runtime_cache_key(body)
    try:
        return runtime_cache_key_bytes(key, body, field, shader=shader)
    except ValueError as exc:
        raise SdfCanonicalWriteError(str(exc)) from exc


def _state_bytes(profile, value, serializer, field: str) -> bytes:
    if value is None:
        return b""
    body = serializer(profile, value)
    return body + _cache_key_bytes(profile, value, body, field)


class _Arena:
    def __init__(self, initial_size: int):
        self.data = bytearray(initial_size)
        self.caches: dict[str, dict[object, int]] = {}

    def add(self, category: str, key: object, payload: bytes, *, alignment: int) -> int:
        cache = self.caches.setdefault(category, {})
        cached = cache.get(key)
        if cached is not None:
            return cached
        offset = align(len(self.data), alignment)
        self.data.extend(b"\0" * (offset - len(self.data)))
        self.data.extend(payload)
        cache[key] = offset
        return offset

    def reserve(self, category: str, key: object, size: int, *, alignment: int) -> int:
        return self.add(category, key, b"\0" * size, alignment=alignment)


@dataclass(slots=True)
class _ProgramBuild:
    program: SdfProgram
    record: int
    metadata_groups: tuple


class _SemanticKeys:
    def __init__(self, profile):
        self.profile = profile
        self.cache = {}

    def shader(self, reference, target_index: int, field: str) -> tuple[bytes, bytes]:
        identity = ("shader", id(reference), target_index)
        cached = self.cache.get(identity)
        if cached is not None:
            return cached[1]
        payload = _shader_payload(self.profile, reference, field, target_index=target_index)
        trailer = _cache_key_bytes(self.profile, reference, payload, field, shader=True) if payload else b""
        result = (payload, trailer)
        self.cache[identity] = (reference, result)
        return result

    def __call__(self, kind, values):
        identity = (kind, id(values))
        cached = self.cache.get(identity)
        if cached is not None:
            return cached[1]
        if isinstance(kind, tuple):
            result = _binding_key(self.profile, values, kind[1])
        elif kind == "stage-counts":
            result = tuple(
                sum(bool(binding.stage_mask & (1 << stage)) for binding in values)
                for stage in range(len(STAGE_KEYS))
            )
        elif kind == "input-layout":
            result = _state_bytes(self.profile, values, _input_layout_bytes, kind)
        elif kind == "rasterizer":
            result = _state_bytes(self.profile, values, _raster_bytes, kind)
        elif kind == "blend":
            result = _state_bytes(self.profile, values, _blend_bytes, kind)
        elif kind == "depth-stencil":
            result = _state_bytes(self.profile, values, _depth_bytes, kind)
        elif kind == "constant_buffer":
            result = _constant_buffer_key(values, lambda variables: self("variables", variables))
        elif kind == "variables":
            result = _variable_key(values, allow_64k=self.profile.constant_variable_size_64k)
        elif kind == "compact":
            result = _compact_key(self.profile, values)
        else:
            result = _named_resource_key(values, kind)
        self.cache[identity] = (values, result)
        return result


class _CanonicalWriter:
    def __init__(self, document: SdfDocument):
        self.profile = document.profile
        self.keys = _SemanticKeys(self.profile)
        self.targets = tuple(document.targets)
        self.program_counts = tuple(len(target.programs) for target in self.targets)
        self.target_count = len(self.targets)
        self.header_size = self._header_size()
        self.record_count = sum(self.program_counts)
        self.table_end = self.header_size + self.record_count * self.profile.record_size
        self.arena = _Arena(self.table_end)
        self.programs: list[_ProgramBuild] = []
        self.string_offsets: dict[str, int] = {}
        self.variable_offsets: dict[tuple, int] = {}
        self.cb_offsets: dict[tuple, int] = {}
        self.named_offsets: dict[tuple[str, tuple], int] = {}

    def _header_size(self) -> int:
        if self.profile.header_layout == "uniform":
            return 0x10
        counts_end = 6 + self.target_count * 2
        return align(counts_end, 8) + 8

    def _validate_targets(self) -> None:
        if self.target_count > 0xFFFF:
            raise SdfCanonicalWriteError("target count exceeds u16")
        if self.target_count > len(self.profile.targets):
            raise SdfCanonicalWriteError(
                f"{self.profile.display_name} defines only {len(self.profile.targets)} "
                f"target contracts, got {self.target_count}"
            )
        for target_index, (target, count) in enumerate(zip(self.targets, self.program_counts)):
            if int(target.index) != target_index:
                raise SdfCanonicalWriteError("target indices must be dense and ordered")
            _bounded(count, 0, 0xFFFF, f"target {target_index} program count")
            for program_index, program in enumerate(target.programs):
                if int(program.target_index) != target_index or int(program.program_index) != program_index:
                    raise SdfCanonicalWriteError("program indices must be dense and target-owned")
        if any(self.program_counts) and not all(self.program_counts):
            raise SdfCanonicalWriteError("every non-empty target needs at least one program")
        if self.profile.header_layout == "uniform" and len(set(self.program_counts)) > 1:
            raise SdfCanonicalWriteError("uniform-header targets need equal program counts")

    def _record_pointer(self, build: _ProgramBuild, relative: int, value: int) -> None:
        struct.pack_into("<Q", self.arena.data, build.record + relative, int(value))

    def _prepare_records(self) -> None:
        ordinal = 0
        layout = self.profile.program_layout
        for target_index, target in enumerate(self.targets):
            for program_index, program in enumerate(target.programs):
                record = self.header_size + ordinal * self.profile.record_size
                ordinal += 1
                metadata_groups = program.resource_metadata
                if len(program.bindings) != len(RESOURCE_KINDS):
                    raise SdfCanonicalWriteError(f"{program.name}: needs five binding groups")
                counts = tuple(len(group) for group in program.bindings)
                if any(count > 0xFF for count in counts):
                    raise SdfCanonicalWriteError(f"{program.name}: a resource group exceeds 255 entries")
                if tuple(len(group) for group in metadata_groups) != counts:
                    raise SdfCanonicalWriteError(
                        f"{program.name}: binding and metadata group lengths disagree"
                    )
                stage_counts = tuple(
                    self.keys("stage-counts", group)
                    for group in program.bindings
                )
                if any(value > 0xFF for group in stage_counts for value in group):
                    raise SdfCanonicalWriteError(f"{program.name}: per-stage binding count exceeds u8")
                total = sum(sum(group) for group in stage_counts)
                name = _ascii_name(program.name, f"program {target_index}:{program_index} name")
                build = _ProgramBuild(program, record, metadata_groups)
                self.programs.append(build)
                struct.pack_into("<I", self.arena.data, record + layout.total_stage_binding_count_offset, total)
                for kind_index, values in enumerate(stage_counts):
                    start = record + layout.per_stage_binding_counts_offset + kind_index * len(STAGE_KEYS)
                    self.arena.data[start : start + len(STAGE_KEYS)] = bytes(values)
                for kind_index, count in enumerate(counts):
                    self.arena.data[record + layout.distinct_resource_count_offsets[kind_index]] = count
                struct.pack_into(
                    "<I",
                    self.arena.data,
                    record + layout.compact_declaration_count_offset,
                    len(program.compact_declarations),
                )
                self.arena.data[record + layout.reserved_tail_offset] = _requirements_byte(
                    self.profile,
                    program,
                )
                group = tuple(
                    _bounded(value, 0, 0xFFFF, f"{name} compute group dimension")
                    for value in program.compute_group_size
                )
                if len(group) != 3:
                    raise SdfCanonicalWriteError(f"{name}: compute group needs three dimensions")
                compute = program.shader("compute")
                compute_present = bool(compute and compute.present)
                ray_library = bool(compute and compute.raytracing_library)
                if compute_present and not ray_library and not all(group):
                    raise SdfCanonicalWriteError(
                        f"{name}: compute shader/group-size presence disagrees"
                    )
                if (not compute_present or ray_library) and any(group):
                    raise SdfCanonicalWriteError(f"{name}: unsampled graphics wave-limit/reserved bits")
                struct.pack_into(
                    "<3H",
                    self.arena.data,
                    record + layout.compute_group_size_offset,
                    *group,
                )
                topology = _topology_id(self.profile, program)
                struct.pack_into(
                    "<H",
                    self.arena.data,
                    record + layout.primitive_topology_offset,
                    topology,
                )

    def _allocate_state_sections(self) -> None:
        layout = self.profile.program_layout
        states = (
            ("input-layout", "input_layout", layout.input_layout_pointer_offset),
            ("depth-stencil", "depth_stencil_state", layout.depth_stencil_state_pointer_offset),
            ("blend", "blend_state", layout.blend_state_pointer_offset),
            ("rasterizer", "rasterizer_state", layout.rasterizer_state_pointer_offset),
        )
        for category, attribute, pointer_relative in states:
            for build in self.programs:
                value = getattr(build.program, attribute)
                if value is not None:
                    payload = self.keys(category, value)
                    self._record_pointer(build, pointer_relative,
                                         self.arena.add(category, payload, payload, alignment=8))

    def _allocate_named_metadata(self) -> None:
        layout = self.profile.program_layout
        # Mirrors the compiler order: texture/UAV interfaces, capability-selected resources, samplers, then constant buffers.
        for kind_index in (2, 3, 4, 1):
            kind = RESOURCE_KINDS[kind_index]
            for build in self.programs:
                resources = build.metadata_groups[kind_index]
                if not resources:
                    continue
                key = self.keys(kind, resources)
                offset = self.named_offsets[kind, key] = self.arena.reserve(
                    f"metadata:{kind}", key, len(key) * layout.named_resource_metadata_size, alignment=8)
                self._record_pointer(
                    build,
                    layout.resource_metadata_pointer_offsets[kind_index],
                    offset,
                )

    def _allocate_constant_buffers(self) -> None:
        layout = self.profile.program_layout
        for build in self.programs:
            buffers = build.metadata_groups[0]
            if not buffers:
                continue
            key = self.keys("constant_buffer", buffers)
            offset = self.cb_offsets[key] = self.arena.reserve(
                "metadata:constant-buffer", key, len(key) * layout.constant_buffer_metadata_size, alignment=8)
            self._record_pointer(
                build,
                layout.resource_metadata_pointer_offsets[0],
                offset,
            )
        for cb_key in self.cb_offsets:
            for _name, _size, variables in cb_key:
                if not variables:
                    continue
                self.variable_offsets[variables] = self.arena.reserve(
                    "constant-variables",
                    variables,
                    len(variables) * layout.constant_variable_size,
                    alignment=8,
                )

    def _allocate_binding_sections(self) -> None:
        layout = self.profile.program_layout
        binding_offsets: dict[tuple[str, int], int] = {}
        compact_offsets: dict[int, int] = {}
        for build in self.programs:
            program = build.program
            for kind_index, kind in enumerate(RESOURCE_KINDS):
                bindings = program.bindings[kind_index]
                if not bindings:
                    continue
                identity = (kind, id(bindings))
                offset = binding_offsets.get(identity)
                if offset is None:
                    key = self.keys(("binding", kind), bindings)
                    payload = _binding_bytes(key)
                    offset = self.arena.add(
                        f"binding:{kind}", key, payload, alignment=8,
                    )
                    binding_offsets[identity] = offset
                self._record_pointer(
                    build,
                    layout.resource_binding_pointer_offsets[kind_index],
                    offset,
                )
            if program.compact_declarations:
                identity = id(program.compact_declarations)
                offset = compact_offsets.get(identity)
                if offset is None:
                    key = self.keys("compact", program.compact_declarations)
                    payload = _compact_bytes(key)
                    offset = self.arena.add(
                        "compact-declarations", key, payload, alignment=4,
                    )
                    compact_offsets[identity] = offset
                self._record_pointer(
                    build,
                    layout.compact_declaration_pointer_offset,
                    offset,
                )

    def _collect_strings(self) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()

        def add(name: str) -> None:
            if name not in seen:
                seen.add(name)
                result.append(name)

        for build in self.programs:
            add(_ascii_name(build.program.name, "program name"))
        for _kind, key in self.named_offsets:
            for name in key:
                add(name)
        for key in self.cb_offsets:
            for name, _size, variables in key:
                add(name)
                for variable_name, _offset, _variable_size in variables:
                    add(variable_name)
        return tuple(result)

    def _allocate_strings(self) -> None:
        for name in self._collect_strings():
            self.string_offsets[name] = self.arena.add(
                "string",
                name,
                name.encode("ascii") + b"\0",
                alignment=1,
            )

    def _hash(self, name: str) -> int:
        hasher = self.profile.metadata_name_hash
        if hasher is None:
            raise SdfCanonicalWriteError(
                f"{self.profile.display_name} has no metadata-name hash function"
            )
        return _bounded(hasher(name), 0, 0xFFFFFFFF, f"{name} hash")

    def _patch_metadata(self) -> None:
        layout = self.profile.program_layout
        for (kind, key), offset in self.named_offsets.items():
            for index, name in enumerate(key):
                struct.pack_into(
                    "<QII",
                    self.arena.data,
                    offset + index * layout.named_resource_metadata_size,
                    self.string_offsets[name],
                    self._hash(name),
                    0,
                )
        for variables, offset in self.variable_offsets.items():
            for index, (name, byte_offset, byte_size) in enumerate(variables):
                struct.pack_into(
                    "<QIHH",
                    self.arena.data,
                    offset + index * layout.constant_variable_size,
                    self.string_offsets[name],
                    self._hash(name),
                    byte_offset,
                    byte_size & 0xFFFF,
                )
        for buffers, offset in self.cb_offsets.items():
            for index, (name, byte_size, variables) in enumerate(buffers):
                struct.pack_into(
                    "<QIIIIQ",
                    self.arena.data,
                    offset + index * layout.constant_buffer_metadata_size,
                    self.string_offsets[name],
                    self._hash(name),
                    0,
                    byte_size,
                    len(variables),
                    self.variable_offsets.get(variables, 0),
                )

    def _allocate_shaders(self) -> int:
        layout = self.profile.program_layout
        shader_offset = len(self.arena.data)
        for build in self.programs:
            program = build.program
            references = (
                (
                    program.input_signature,
                    layout.input_signature_pointer_offset,
                    layout.input_signature_size_offset,
                ),
                *(
                    (program.shader(stage.key), stage.pointer_offset, stage.size_offset)
                    for stage in self.profile.stages
                ),
            )
            for reference, pointer_relative, size_relative in references:
                if reference is None:
                    continue
                payload, trailer = self.keys.shader(
                    reference,
                    program.target_index,
                    f"{program.name} {reference.role}",
                )
                if not payload:
                    continue
                record = payload + trailer
                offset = self.arena.add("shader-payload", record, record, alignment=1)
                self._record_pointer(build, pointer_relative, offset)
                struct.pack_into(
                    "<I",
                    self.arena.data,
                    build.record + size_relative,
                    len(payload),
                )
        return shader_offset

    def _patch_program_names(self) -> None:
        relative = self.profile.program_layout.name_pointer_offset
        for build in self.programs:
            self._record_pointer(build, relative, self.string_offsets[build.program.name])

    def _patch_header(self, shader_offset: int) -> None:
        self.arena.data[:4] = SDF_MAGIC
        struct.pack_into("<H", self.arena.data, 4, self.target_count)
        if self.profile.header_layout == "uniform":
            count = self.program_counts[0] if self.program_counts else 0
            struct.pack_into("<HQ", self.arena.data, 6, count, shader_offset)
            return
        for index, count in enumerate(self.program_counts):
            struct.pack_into("<H", self.arena.data, 6 + index * 2, count)
        shader_field = align(6 + self.target_count * 2, 8)
        struct.pack_into("<Q", self.arena.data, shader_field, shader_offset)

    def write(self) -> bytes:
        self._validate_targets()
        self._prepare_records()
        self._allocate_state_sections()
        self._allocate_named_metadata()
        self._allocate_constant_buffers()
        self._allocate_binding_sections()
        self._allocate_strings()
        self._patch_metadata()
        self._patch_program_names()
        shader_offset = self._allocate_shaders()
        self._patch_header(shader_offset)
        return bytes(self.arena.data)


def _program_semantic_key(profile, program, keys) -> tuple:
    metadata_groups = program.resource_metadata
    shader_key = []
    for reference in (program.input_signature, *program.shaders):
        field = f"{program.name} {reference.role}"
        payload, trailer = keys.shader(reference, program.target_index, field)
        shader_key.append((reference.role, payload, trailer, reference.binary_format if reference.present else "none"))
    return (
        program.name,
        tuple(shader_key),
        keys("input-layout", program.input_layout),
        keys("rasterizer", program.rasterizer_state),
        keys("blend", program.blend_state),
        keys("depth-stencil", program.depth_stencil_state),
        keys("compact", program.compact_declarations),
        tuple(
            keys(("binding", kind), group)
            for kind, group in zip(RESOURCE_KINDS, program.bindings)
        ),
        keys("constant_buffer", metadata_groups[0]),
        tuple(
            keys(RESOURCE_KINDS[index], metadata_groups[index])
            for index in range(1, len(RESOURCE_KINDS))
        ),
        tuple(program.compute_group_size),
        _topology_id(profile, program),
        tuple(sorted(program.shader_requirements)),
    )


def semantic_fingerprint(document: SdfDocument) -> tuple:
    keys = _SemanticKeys(document.profile)
    return (
        document.profile.game,
        tuple(
            tuple(_program_semantic_key(document.profile, program, keys) for program in target.programs)
            for target in document.targets
        ),
    )


def serialize_sdf(document: SdfDocument, *, validate: bool = True) -> bytes:
    result = _CanonicalWriter(document).write()
    if not validate:
        return result
    reparsed = SdfFile.from_bytes(
        result,
        profile=document.profile,
        label=f"canonical {document.profile.display_name} SDF",
    ).require_document()
    if semantic_fingerprint(reparsed) != semantic_fingerprint(document):
        raise SdfCanonicalWriteError("canonical SDF reparse changed runtime semantics")
    second = _CanonicalWriter(reparsed).write()
    if second != result:
        raise SdfCanonicalWriteError("canonical SDF serialization is not idempotent")
    return result


__all__ = [
    "SdfCanonicalWriteError",
    "semantic_fingerprint",
    "serialize_sdf",
]
