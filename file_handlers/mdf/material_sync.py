from __future__ import annotations

from dataclasses import dataclass

from .mdf_file import MatData, ParamHeader, TexHeader
from file_handlers.material_names import resolve_material_name
from file_handlers.mmtr.material_project import (
    MATERIAL_TEXTURE_DIMENSIONS, MaterialBufferLayout, MaterialBufferVariable, MaterialTextureSlot, valid_default_path,
)


class MaterialSyncError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MaterialSyncResult:
    preserved: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MaterialTextureSyncResult:
    preserved: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]


def _layout_variables(layout: MaterialBufferLayout) -> tuple[MaterialBufferVariable, ...]:
    variables = tuple(sorted(layout.variables, key=lambda item: (item.byte_offset, item.name.casefold())))
    byte_size = layout.byte_size
    if byte_size < 0 or byte_size % 16 or (byte_size == 0 and variables):
        raise MaterialSyncError(
            f"MMTR material buffer size must be register aligned (zero only for an empty parameter table), got {byte_size}."
        )
    previous_end, previous_name = 0, ""
    names: set[str] = set()
    for variable in variables:
        name, offset, size = variable.name.strip(), variable.byte_offset, variable.byte_size
        if not name:
            raise MaterialSyncError("MMTR material parameters need non-empty names.")
        if name in names:
            raise MaterialSyncError(f"MMTR material parameter {name!r} is duplicated.")
        names.add(name)
        if offset < 0 or offset % 4:
            raise MaterialSyncError(
                f"MMTR material parameter {name!r} has invalid byte offset {offset}."
            )
        if size not in {4, 8, 12, 16}:
            raise MaterialSyncError(
                f"MMTR material parameter {name!r} has unsupported byte size {size}."
            )
        end = offset + size
        if end > byte_size:
            raise MaterialSyncError(
                f"MMTR material parameter {name!r} ends beyond its {byte_size}-byte buffer."
            )
        if offset < previous_end:
            raise MaterialSyncError(
                f"MMTR material parameters {previous_name!r} and {name!r} overlap."
            )
        previous_end, previous_name = end, name
    return variables


def _parameter_values(source: ParamHeader | None, variable: MaterialBufferVariable,
                      component_count: int) -> tuple[float, float, float, float]:
    raw = source.parameter if source is not None else variable.default
    values = [float(value) for value in raw[:component_count]]
    values.extend(0.0 for _ in range(4 - len(values)))
    return tuple(values)


def _by_name(items, attribute: str, kind: str) -> dict[str, object]:
    by_name: dict[str, object] = {}
    for item in items:
        name = getattr(item, attribute)
        if name in by_name:
            raise MaterialSyncError(f"MDF material contains duplicate {kind} name {name!r}.")
        by_name[name] = item
    return by_name


def _take_existing(existing: dict[str, object], entry: MaterialBufferVariable | MaterialTextureSlot, consumed: set[str]):
    key = resolve_material_name(entry.name, existing)
    if key is None:
        key = resolve_material_name(entry.source_name, existing) or None
    if key is None or key in consumed:
        return None
    consumed.add(key)
    return existing[key]


def synchronize_material_parameters(material: MatData, layout: MaterialBufferLayout) -> MaterialSyncResult:
    variables = _layout_variables(layout)
    existing_by_name = _by_name(material.parameters, "name", "parameter")

    synchronized: list[ParamHeader] = []
    preserved: list[str] = []
    added: list[str] = []
    consumed_existing: set[str] = set()
    previous_end = 0
    for variable in variables:
        name, offset, size = variable.name, variable.byte_offset, variable.byte_size
        component_count = size // 4
        source = _take_existing(existing_by_name, variable, consumed_existing)
        (added if source is None else preserved).append(name)
        parameter = ParamHeader(
            name=name,
            component_count=component_count,
            component_locked=0 if source is None else source.component_locked,
            param_rel_offset=offset,
            gap_size=offset - previous_end,
            parameter=_parameter_values(source, variable, component_count),
        )
        parameter._layout_locked = True
        synchronized.append(parameter)
        previous_end = offset + size

    removed = tuple(parameter.name for parameter in material.parameters if parameter.name not in consumed_existing)
    material.parameters = synchronized
    material.header.param_count = len(synchronized)
    material.header.params_size = layout.byte_size
    material.parameter_layout_size = layout.byte_size
    return MaterialSyncResult(
        preserved=tuple(preserved),
        added=tuple(added),
        removed=removed,
    )


def parameter_layout_errors(material: MatData, layout: MaterialBufferLayout) -> tuple[str, ...]:
    try:
        variables = _layout_variables(layout)
    except MaterialSyncError as exc:
        return (str(exc),)
    errors: list[str] = []
    parameters = material.parameters
    if len(parameters) != len(variables):
        errors.append(
            f"parameter count is {len(parameters)}; MMTR requires {len(variables)}"
        )
    for index, variable in enumerate(variables):
        if index >= len(parameters):
            errors.append(f"missing parameter {variable.name!r}")
            continue
        parameter = parameters[index]
        actual = (parameter.name, parameter.param_rel_offset, max(1, parameter.component_count) * 4)
        expected = (variable.name, variable.byte_offset, variable.byte_size)
        if actual != expected:
            errors.append(
                f"parameter {index} is {actual[0]!r}@{actual[1]}:{actual[2]}; "
                f"MMTR requires {expected[0]!r}@{expected[1]}:{expected[2]}"
            )
    header_size = material.header.params_size
    if header_size != layout.byte_size:
        errors.append(
            f"parameter buffer is {header_size} bytes; MMTR requires {layout.byte_size}"
        )
    return tuple(errors)


def _texture_slots(layout: tuple[MaterialTextureSlot, ...]) -> tuple[MaterialTextureSlot, ...]:
    if len(layout) > 0xFF:
        raise MaterialSyncError("an MDF material cannot encode more than 255 textures")
    names: set[str] = set()
    for slot in layout:
        name = slot.name.strip()
        if not name:
            raise MaterialSyncError("MMTR material textures need non-empty names.")
        if name in names:
            raise MaterialSyncError(f"MMTR material texture {name!r} is duplicated.")
        names.add(name)
        # A non-sampled MDF descriptor has no shader dimensionality.
        if slot.dimension is not None and slot.dimension not in MATERIAL_TEXTURE_DIMENSIONS:
            raise MaterialSyncError(
                f"MMTR material texture {name!r} has an unsupported dimension."
            )
        if not valid_default_path(slot.default_path):
            raise MaterialSyncError(
                f"MMTR material texture {name!r} has an invalid default path."
            )
    return layout


def synchronize_material_textures(material: MatData, layout: tuple[MaterialTextureSlot, ...]) -> MaterialTextureSyncResult:
    slots = _texture_slots(layout)
    existing_by_name = _by_name(material.textures, "tex_type", "texture")

    synchronized: list[TexHeader] = []
    preserved: list[str] = []
    added: list[str] = []
    consumed: set[str] = set()
    for slot in slots:
        source = _take_existing(existing_by_name, slot, consumed)
        if source is None:
            added.append(slot.name)
            texture = TexHeader(tex_type=slot.name, tex_path=slot.default_path, locked=0)
        else:
            preserved.append(slot.name)
            texture = TexHeader(tex_type=slot.name, tex_path=source.tex_path, locked=source.locked)
        synchronized.append(texture)

    removed = tuple(texture.tex_type for texture in material.textures if texture.tex_type not in consumed)
    material.textures = synchronized
    material.header.tex_count = len(synchronized)
    return MaterialTextureSyncResult(
        preserved=tuple(preserved),
        added=tuple(added),
        removed=removed,
    )


def texture_layout_errors(material: MatData, layout: tuple[MaterialTextureSlot, ...]) -> tuple[str, ...]:
    try:
        slots = _texture_slots(layout)
    except MaterialSyncError as exc:
        return (str(exc),)
    textures = material.textures
    errors: list[str] = []
    if len(textures) != len(slots):
        errors.append(
            f"texture count is {len(textures)}; MMTR requires {len(slots)}"
        )
    for index, slot in enumerate(slots):
        if index >= len(textures):
            errors.append(f"missing texture {slot.name!r}")
            continue
        actual = textures[index].tex_type
        if actual != slot.name:
            errors.append(
                f"texture {index} is {actual!r}; MMTR requires {slot.name!r}"
            )
    return tuple(errors)


__all__ = [
    "MaterialSyncError",
    "MaterialSyncResult",
    "MaterialTextureSyncResult",
    "parameter_layout_errors",
    "synchronize_material_parameters",
    "synchronize_material_textures",
    "texture_layout_errors",
]
