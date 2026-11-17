"""See docs/internals/gui-rendering.md, Materials."""
from __future__ import annotations

from dataclasses import replace
import re

from file_handlers.native_math import f32, fdiv
lazy import fastdxiltext
lazy from file_handlers.mmtr.material_preview import PreviewMaterial
lazy from file_handlers.mmtr.native_resource import DXIL_RESOURCE_CLASSES
lazy from file_handlers.mmtr.profiles.dxbc_recovery import recover_dxbc_ir
lazy from file_handlers.mmtr.profiles.re_engine_dxil_recovery import recover_re_engine_dxil_ir
lazy from file_handlers.mmtr.surface_flow import constant_field, recovered_surface_flow
lazy from file_handlers.mmtr.surface_flow_dxbc import dxbc_flow
lazy from utils.hash_util import murmur3_hash_utf16le

lazy from .vertices import mixed_color, runtime_texture


class MaterialGap(ValueError):
    pass


def slotted(node, rule, material, version: int, assets) -> PreviewMaterial:
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
        if kind not in rule["values"] or "material" in rule and murmur3_hash_utf16le(
                str(properties[f"{rule['material']}{slot}"])) != int(material.header.mat_name_hash):
            continue
        name = f"{rule['values'][kind]}{slot}"
        key, value = murmur3_hash_utf16le(str(properties[f"{rule['name']}{slot}"])), (
            mixed_color(node, name, assets) if kind == "Color" else properties[name])
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


_REACHED: dict[tuple[bytes, frozenset | None], set[tuple[str, str]]] = {}


def _reaching(contract, payload: bytes, read: set | None) -> set[tuple[str, str]]:
    key = payload, None if read is None else frozenset(read)
    if key not in _REACHED:
        _REACHED[key] = _reached(contract, payload, read)
    return _REACHED[key]


def _named_flow(contract, payload: bytes):
    if int(contract.model_major) < 6:
        flow, buffers, named = dxbc_flow(recover_dxbc_ir(payload), contract), {
            item.bind_point: item.name for item in contract.constant_buffers}, {}
        for at, value in enumerate(flow.values):
            if value.kind == "source" and value.signature[:1] == ("cb",):
                origin = value.origin
                binding = origin[1] if origin[0] == "cb" else int(re.match(r"cb(\d+)", origin[0])[1])
                named[at] = (buffers[binding], re.split(r"[.@]", constant_field(
                    contract, binding, 0, origin[2] * 16 + origin[3] * 4))[0] if origin[0] == "cb" else origin[0])
        return flow, named
    ir = recover_re_engine_dxil_ir(payload)
    lines = ir.to_assembly().splitlines()
    buffers, words = fastdxiltext.value_names(
        lines, {}, {(item.register_class, item.resource_id): item.name for item in contract.resources},
        {(item.register_class, item.register_space, item.bind_point): item.name for item in contract.resources},
        DXIL_RESOURCE_CLASSES, "SamplerDescriptorHeap", "ResourceDescriptorHeap"), fastdxiltext.constant_words(lines)
    flow, named = recovered_surface_flow("dxil", ir, contract), {}
    for at, value in enumerate(flow.values):
        if value.origin in words:
            resource, (_handle, row, lane) = contract.resource(buffers[value.origin][1]), words[value.origin]
            named[at] = resource.name, re.split(r"[.@]", constant_field(
                contract, resource.bind_point, resource.register_space, row * 16 + lane * 4))[0]
    return flow, named


def _reached(contract, payload: bytes, read: set | None) -> set[tuple[str, str]]:
    flow, named = _named_flow(contract, payload)
    if read is None:
        return set(named.values())
    stack, seen, found = [store.value for store in flow.stores if store.value is not None and (
        store.semantic == "sv_position" or (store.semantic, store.semantic_index, store.lane) in read)], set(), set()
    while stack:
        at = stack.pop()
        if at in seen:
            continue
        seen.add(at)
        if at in named:
            found.add(named[at])
        value = flow.values[at]
        stack += [*value.operands, *value.controls]
    return found


def unread(program, shaders, users, material, known, textures, frame) -> str | None:
    read = {(field.semantic_name.casefold(), field.semantic_index, lane) for field in shaders["pixel"][0].inputs
            for lane in range(4) if field.read_write_mask >> lane & 1}
    for shader in program.shaders:
        if not shader.present:
            continue
        (contract, payload), reflection = shaders[shader.role], shader.reflection.reflection
        dxil = int(contract.model_major) >= 6
        reads = _reaching(contract, payload, read if shader.role == "vertex" else None) if (
            shader.role == "vertex" or dxil) else {(buffer.name, item.name) for buffer in reflection.constant_buffers
                                                   for item in buffer.variables if item.flags & 2}
        missing = sorted((buffer, name) for buffer, name in reads if buffer not in users and name not in known)
        if missing:
            return f"its material reads {missing[0][0]}.{missing[0][1]}, which the preview doesn't have"
        for resource in contract.resources if dxil else reflection.resources:
            if resource.input_type == "texture" and resource.name in frame:
                return f"its material reads {resource.name}, an image of the frame the game makes while it runs"
            if resource.input_type == "texture" and resource.name not in material.textures.keys() | textures.keys():
                return f"its material reads the texture {resource.name}, which neither its MDF nor the GUI binds"
            if resource.input_type == "texture" and runtime_texture(material.textures.get(resource.name, "")):
                return f"its material reads {resource.name}, a render target the game fills at run time"
    return None
