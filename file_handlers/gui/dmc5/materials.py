"""See docs/internals/gui-rendering.md, Materials."""
from __future__ import annotations

from dataclasses import replace
import re

from file_handlers.native_math import f32, fdiv
lazy from file_handlers.mmtr.material_preview import PreviewMaterial
lazy from file_handlers.mmtr.profiles.dxbc_recovery import recover_dxbc_ir
lazy from file_handlers.mmtr.surface_flow import constant_field
lazy from file_handlers.mmtr.surface_flow_dxbc import dxbc_flow
lazy from utils.hash_util import murmur3_hash_utf16le

lazy from .vertices import runtime_texture


class MaterialGap(ValueError):
    pass


def slotted(node, rule, material, version: int) -> PreviewMaterial:
    made = PreviewMaterial.from_mdf(material, version)
    properties = node.render_properties
    parameters, named = {}, {}
    for parameter in material.parameters:
        parameters.setdefault(int(parameter.hash), parameter)
    for texture in material.textures:
        named.setdefault(int(texture.hash), str(texture.tex_type))
    values, textures = list(made.values), dict(made.textures)
    for slot in range(rule["slots"]):
        kind = node.runtime.enum_label(properties, f"{rule['type']}{slot}", rule["enum"])
        if kind not in rule["values"]:
            continue
        key, value = murmur3_hash_utf16le(str(properties[f"{rule['name']}{slot}"])), properties[
            f"{rule['values'][kind]}{slot}"]
        parameter = parameters.get(key)
        if kind == "Texture":
            if key in named and not value and not rule["loaded_textures"]:
                raise MaterialGap(f"variable slot {slot} sets no texture, and what the game samples then is unread")
            if key in named and value:
                textures[named[key]] = str(value)
        elif parameter is not None and int(parameter.component_count) == rule["components"][kind]:
            values.append((int(parameter.param_rel_offset), tuple(
                fdiv(float(item), rule["color_divisor"]) if kind == "Color" else f32(item)
                for item in ([value] if rule["components"][kind] == 1 else value))))
    return replace(made, values=tuple(values), textures=textures)


_REACHED: dict[tuple[bytes, frozenset], set[tuple[str, str]]] = {}


def _reaching(contract, payload: bytes, read: set) -> set[tuple[str, str]]:
    key = payload, frozenset(read)
    if key not in _REACHED:
        _REACHED[key] = _reached(contract, payload, read)
    return _REACHED[key]


def _reached(contract, payload: bytes, read: set) -> set[tuple[str, str]]:
    flow, buffers = dxbc_flow(recover_dxbc_ir(payload), contract), {
        item.bind_point: item.name for item in contract.constant_buffers}
    stack, seen, found = [store.value for store in flow.stores if store.value is not None and (
        store.semantic == "sv_position" or (store.semantic, store.semantic_index, store.lane) in read)], set(), set()
    while stack:
        at = stack.pop()
        if at in seen:
            continue
        seen.add(at)
        value = flow.values[at]
        if value.kind == "source" and value.signature[:1] == ("cb",):
            origin = value.origin
            binding = origin[1] if origin[0] == "cb" else int(re.match(r"cb(\d+)", origin[0])[1])
            found.add((buffers[binding], re.split(r"[.@]", constant_field(
                contract, binding, 0, origin[2] * 16 + origin[3] * 4))[0] if origin[0] == "cb" else origin[0]))
        stack += [*value.operands, *value.controls]
    return found


def unread(program, shaders, users, material, known, textures) -> str | None:
    read = {(field.semantic_name.casefold(), field.semantic_index, lane) for field in shaders["pixel"][0].inputs
            for lane in range(4) if field.read_write_mask >> lane & 1}
    for shader in program.shaders:
        if not shader.present:
            continue
        reflection = shader.reflection.reflection
        reads = _reaching(*shaders[shader.role], read) if shader.role == "vertex" else {
            (buffer.name, item.name) for buffer in reflection.constant_buffers for item in buffer.variables
            if item.flags & 2}
        missing = sorted((buffer, name) for buffer, name in reads if buffer not in users and name not in known)
        if missing:
            return f"its material reads {missing[0][0]}.{missing[0][1]}, which the preview doesn't have"
        for resource in reflection.resources:
            if resource.input_type == "texture" and resource.name not in material.textures.keys() | textures.keys():
                return f"its material reads the texture {resource.name}, which neither its MDF nor the GUI binds"
            if resource.input_type == "texture" and runtime_texture(material.textures.get(resource.name, "")):
                return f"its material reads {resource.name}, a render target the game fills at run time"
    return None
