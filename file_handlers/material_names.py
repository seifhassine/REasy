from __future__ import annotations


def resolve_material_name(name: str, items, key=None):
    name = str(name)
    if key is None:
        if name in items:
            return name
    else:
        exact = next((item for item in items if key(item) == name), None)
        if exact is not None:
            return exact
    folded = name.casefold()
    matches = [item for item in items if (item if key is None else key(item)).casefold() == folded]
    return matches[0] if len(matches) == 1 else None


def logical_material_parameter_name(name: str, *, buffer_names=("UserMaterial",), prefixes=("VAR_",)) -> str:
    name = str(name)
    for buffer in buffer_names:
        qualifier = f"REasy_{buffer}_"
        if name.startswith(qualifier):
            name = name[len(qualifier):]
            break
    for prefix in prefixes:
        if name.casefold().startswith(prefix.casefold()):
            return name[len(prefix):]
    return name


def is_private_material_parameter(name: object) -> bool:
    normalized = str(name or "").casefold()
    return normalized.startswith(
        ("capcom_material_reserve", "var_capcom_material_reserve")
    )


__all__ = ["is_private_material_parameter", "logical_material_parameter_name", "resolve_material_name"]
