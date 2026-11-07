"""See docs/internals/motion-editor.md, Blend shapes and material controllers."""

from __future__ import annotations

from dataclasses import dataclass

lazy import numpy as np

lazy from file_handlers.native_math import f16, f32, fmul

lazy from file_handlers.rsz.character_catalogs import rsz_reader
lazy from file_handlers.rsz.plain_values import plain_fields, root_fields, user_path
lazy from file_handlers.rsz.spawn_rules import type_name_of
lazy from utils.hash_util import murmur3_hash_utf16le

from .rules import controller_rules


@dataclass(frozen=True, slots=True)
class MaterialController:
    holder: object
    outputs: tuple[tuple[str | int, str | int, tuple], ...]
    joints: bool
    hashed: bool
    shape: tuple[float, ...]
    corrections: dict[int, tuple[float, float]]
    sums: bool = False

    def totals(self, weights: dict[int, float], slots: tuple[int, ...], materials: set):
        """Channel indices are bytes: 255 past the first 256 ``slots``, and one past them throws, ending the update's
        writes; totals sum in double, as float32."""
        first = {}
        for at, value in enumerate(slots[:256]):
            first.setdefault(value, at)
        for material, variable, columns in self.outputs if slots else ():
            if material not in materials:
                continue
            found = []
            for pairs in columns:
                total = 0.0
                for value, rate in pairs:
                    if (slot := first.get(value, 255)) >= len(slots):
                        return
                    total += fmul(rate, weights.get(slots[slot], 0.0))
                found.append(f32(total))
            yield material, variable, tuple(found)

    def values(self, deform: dict[int, float], joints: dict[int, float]):
        given = joints if self.joints else deform
        for material, variable, inputs in self.outputs:
            found = np.array([given.get(value, 0.0) for value in inputs], np.float32)
            for at, value in enumerate(inputs):
                if value in self.corrections:
                    top, ratio = self.corrections[value]
                    powered = np.power(found[at], np.float32(ratio))
                    found[at] = top if np.isnan(powered) else min(powered, np.float32(top))
            if self.joints:
                scale, offset, limit = (np.float32(value) for value in self.shape)
                scaled = scale * found
                rise = np.float32(1.0 / (1.0 - float(offset))) * (scaled - offset) if offset < 1.0 else scaled * 0
                found = np.where((offset < 1.0) & (scaled >= offset), np.minimum(rise, limit), np.float32(0.0))
            else:
                low, high = (np.float32(value) for value in self.shape)
                found = np.where(found < low, low, np.minimum(found, high))
            yield material, variable, float(found[0]) if len(inputs) == 1 else tuple(float(value) for value in found)


def material_controllers(documents, game: str, read) -> list[MaterialController]:
    rules, found = controller_rules(), []
    users = None
    for document in documents.values():
        for item in document.objects.values():
            for component in (document.components[key] for key in item.components if key in document.components):
                for kind, rule in rules.items():
                    if rule["types"].get(game.upper()) != component.type_name:
                        continue
                    fields = plain_fields(document, component)
                    if kind in ("facial_wrinkle", "wrinkle_names"):
                        if users is None:
                            users = rsz_reader(game, lambda path: None if (data := read(path)) is None else (
                                path, data), {})
                        made = (_facial_wrinkle if kind == "facial_wrinkle" else _wrinkle_names)(
                            rule, users(user_path(document.rsz_file, component.fields.get(rule["config"]))))
                    elif kind == "blend_texture":
                        made = _blend_texture(rule, fields, game)
                    elif kind == "muscle_normals":
                        if users is None:
                            users = rsz_reader(game, lambda path: None if (data := read(path)) is None else (
                                path, data), {})
                        path = document.resources.get(component.id, {}).get(rule["asset"]) or fields.get(rule["asset"])
                        made = _muscle_normals(rule, path and users(path))
                    else:
                        made = _joint_wrinkle(rule, fields)
                    if made is not None:
                        found.append(MaterialController(item.id, *made))
    return found


def _facial_wrinkle(rule: dict, config):
    if config is None or (fields := root_fields(config)) is None:
        return None
    attributes = rule["attributes"][1:] if type_name_of(config, config.object_table[0]) == rule["float4"] \
        else rule["attributes"][:1]
    outputs = tuple((param[rule["material"]], param[rule["variable"]],
                     tuple(murmur3_hash_utf16le(param[name]) for name in attributes))
                    for param in fields.get(rule["params"]) or ())
    return outputs, False, False, tuple(rule["clamp"]), {}


def _wrinkle_names(rule: dict, config):
    """Whole fours only: the game throws on the rest."""
    if config is None or (fields := root_fields(config)) is None:
        return None
    hashes = [murmur3_hash_utf16le(name) for name in fields.get(rule["names"]) or ()]
    width = 4 if fields.get(rule["float4"]) else 1
    outputs = tuple((fields.get(rule["material"]) or "", rule["variable4"].format(at + 1, at + 4) if width == 4
                     else rule["variable"].format(at + 1), tuple(hashes[at:at + width]))
                    for at in range(0, len(hashes) - width + 1, width))
    return outputs, False, False, tuple(rule["clamp"]), {}


def _blend_texture(rule: dict, fields: dict, game: str):
    kind = int(fields.get(rule["type"]) or 0)
    listed = rule["names"][game.upper()]
    names = listed.get(f"legacy {kind}") if fields.get(rule["legacy"]) else None
    names = names or listed.get(str(kind)) or listed["0"]
    hashes = [murmur3_hash_utf16le(name) for name in names]
    head, teeth = fields.get(rule["head"]) or "", fields.get(rule["teeth"]) or ""
    outputs = [(head, rule["weight"].format(at + 1), (value,)) for at, value in enumerate(hashes) if head]
    if teeth and rule["jaw open"] < len(hashes):
        outputs.append((teeth, rule["teeth weight"], (hashes[rule["jaw open"]],)))
    corrections = {} if kind != rule["corrected type"] or not fields.get(rule["correct"]) else {
        hashes[int(entry[rule["part"]])]: (float(entry[rule["max"]]), float(entry[rule["ratio"]]))
        for entry in fields.get(rule["corrections"]) or () if 0 <= int(entry[rule["part"]]) < len(hashes)}
    return tuple(outputs), False, False, tuple(rule["clamp"]), corrections


def _muscle_normals(rule: dict, data):
    if data is None or (fields := root_fields(data)) is None:
        return None
    outputs = tuple((material[rule["material"]], variable, tuple(tuple(
        (murmur3_hash_utf16le(merged[rule["name"]]), f16(merged[rule["rate"]]))
        for merged in (material.get(name) or {}).get(rule["deform"]) or ()) for name in names))
                    for material in fields.get(rule["materials"]) or () if material.get(rule["material"])
                    for variable, *names in rule["rows"]
                    if any((material.get(name) or {}).get(rule["deform"]) for name in names))
    return outputs, False, False, (), {}, True


def _joint_wrinkle(rule: dict, fields: dict):
    outputs = tuple((int(table[rule["material"]]), int(param[rule["target"]]),
                     tuple(int(param[name]) for name in rule["joints"]))
                    for table in fields.get(rule["tables"]) or () for param in table.get(rule["params"]) or ())
    return outputs, True, True, tuple(float(fields.get(rule[name]) or 0.0) for name in ("scale", "offset", "limit")), {}


__all__ = ["MaterialController", "material_controllers"]
