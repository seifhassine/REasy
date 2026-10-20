from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from ..mmtr.material_preview import VIEW_DIMENSIONS, PreviewMaterial, _texture_texels
lazy from ..mmtr.resident import Resident

MATERIAL = "material."


def material_given(device: Resident, bindings: Mapping[str, str], resources: Iterable, material: PreviewMaterial,
                   load: Callable[[str], bytes | None]) -> tuple[dict[str, tuple[int, int] | int | None], list[str]]:
    """``.rtex`` textures bind null views."""
    given, unbound = {}, []
    for resource in resources:
        slot = bindings.get(resource.name, "")
        if not slot.startswith(MATERIAL) or slot in given:
            continue
        texture = resource.input_type == "texture"
        path = (material.textures if texture else material.buffers).get(resource.name)
        data = None if path is None else load(path)
        held = None
        if data and texture:
            texels = _texture_texels(data, resource.dimension, None)
            if texels is not None:
                _index, form, made = device.read(VIEW_DIMENSIONS[resource.dimension], (0, *texels))
                held = (form, made)
        elif data:
            held = device.buffer(data)
        given[slot] = held
        if held is None:
            unbound.append(resource.name)
    return given, unbound


def user_material(material: PreviewMaterial, contracts: Iterable, names: Iterable[str]) -> dict[str, bytes]:
    record, wanted, out = material.record(), set(names), {}
    for contract in contracts:
        for resource in contract.resources:
            if resource.register_class == "b" and resource.name in wanted:
                size = contract.buffer(resource.name).byte_size
                out[resource.name] = bytes(record[:size]).ljust(size, b"\0")
    return out


__all__ = ["MATERIAL", "material_given", "user_material"]
