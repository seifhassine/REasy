"""See docs/internals/character-assembly.md."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial

lazy from utils.resource_file_utils import normalize_resource_path

lazy from .material_variations import enum_names
lazy from .plain_values import plain_value, root_fields, user_path
lazy from .scn_scene_graph import (
    VIA_RENDER_MESH, VIA_TRANSFORM, ScnComponent, ScnComponentId, ScnObjectId, ScnSceneDocument, ScnSceneGraph,
    ScnSceneObject, _bool_field,
)
lazy from .spawn_rules import SpawnChoice, resource_path, spawn_choice_key, spawners, type_name_of
lazy from .spawned_parts import SpawnedMesh


class ScnPartSwapper:
    def _swap_parts(self, document: ScnSceneDocument, graph: ScnSceneGraph) -> None:
        found = spawners(self.game_version)
        for component in list(document.components.values()):
            spec = found.get(component.type_name, {}).get("swaps")
            owner = None if spec is None else document.objects.get(component.owner)
            if owner is None or not _bool_field(component.fields, self.fields.get("enabled"), True):
                continue
            held = self._swap_sources(spec, document.document_id)
            meta = plain_value(component.fields.get(spec["meta"]), partial(self._fields, document.rsz_file))
            if "characters" in spec:
                self._pick_character(graph, owner, meta, spec["characters"], held["characters"], partial(
                    spawn_choice_key, document.document_id, component.id.instance_id))
            for name, rule in spec["rows"].items():
                if {"item", "styles"} & rule.keys():
                    self._pick_style(graph, owner, meta, name, rule, held, spawn_choice_key(
                        document.document_id, component.id.instance_id, name))
            rows = {"meta": meta}
            rows |= {name: held["rows"][name].get(tuple(map(meta.get, rule["keys"].values()))) if all(
                _holds(condition, rows, {}) for condition in rule.get("if", ())) else None
                     for name, rule in spec["rows"].items()}
            head = next((item for item in document.objects.values() if item.name == spec["head"]
                         and item.parent_id == owner.id.local_object_id), None)
            species = enum_names(self.game_version, spec["species"][1]).get(meta.get(spec["species"][0]), "")
            made = {}
            for part in spec["parts"]:
                kind, meshes, skins = part["catalogs"]
                mesh = self._held(held, kind, _catalog_fields(meshes, species), (rows[part["row"]] or {}).get(
                    part["mesh"]), spec["mesh"], document.document_id)
                holder = head if part.get("on") == "head" else owner
                skin = (rows[part.get("skin row", part["row"])] or {}).get(part["skin"])
                if mesh and holder is not None:
                    made[part["name"]] = part, holder, mesh, self._held(
                        held, kind, _catalog_fields(skins, species), skin, spec["skin"], document.document_id)
            weapon = partial(_weapon, held["weapons"], spec["weapons"])
            recalculation = _bool_field(component.fields, spec["normal recalculation"], False)
            for name, (part, holder, mesh, material) in made.items():
                item = self._swap_object(document, graph, holder, name, held["object"]) if part.get("made") else holder
                if item is None or item.mesh_component is None:
                    continue
                document.slotted[item.mesh_component] = SpawnedMesh(mesh, material, enabled=_enabled(
                    part, rows, made, weapon, spec["bits"]), normal_recalculation=recalculation)
                document.joints[item.id] = self._resource_joints(mesh, document.document_id) or None
                if "shown" in part:
                    item.draw_self = any(all(_holds(condition, rows, made) for condition in listed)
                                         for listed in part["shown"])

    def _pick_character(self, graph: ScnSceneGraph, owner: ScnSceneObject, meta: dict, rule: Mapping,
                        characters: Mapping, key: Callable[[int], str]) -> None:
        levels = character_levels(characters, enum_names(self.game_version, rule["enum"]), lambda level, count: (
            picked if 0 <= (picked := self.choices.get(key(level), 0)) < count else 0))
        for level, (name, (labels, picked)) in enumerate(zip(("character", *rule["rows"]), levels)):
            graph.spawn_choices.append(SpawnChoice(key(level), f"{owner.name} · {name}", labels, picked))
        picked_rows = list(characters.values())[levels[0][1] - 1] if levels[0][1] else ()
        for rows, (_labels, picked) in zip(picked_rows, levels[1:]):
            meta.update((name, value) for name, value in list(rows.values())[picked].items() if name in meta)

    def _pick_style(self, graph: ScnSceneGraph, owner: ScnSceneObject, meta: dict, name: str, rule: Mapping,
                    held: Mapping, key: str) -> None:
        names = enum_names(self.game_version, rule["item"]["enum"] if "item" in rule else rule["styles"])
        values, *others, field = {label: value for value, label in names.items()}, *rule["keys"].values()
        listed = [(f"{item} {style}", values[style]) for item, number in held["items"].get(
            rule["item"]["category"], ()) if (style := rule["item"]["name"].format(number)) in values] \
            if "item" in rule else [(label, value) for value, label in names.items()]
        listed = [(label, value) for label, value in listed
                  if (*map(meta.get, others), value) in held["rows"][name]]
        picked = self.choices.get(key, 0) if 0 <= self.choices.get(key, 0) <= len(listed) else 0
        graph.spawn_choices.append(SpawnChoice(key, f"{owner.name} · {name}", (
            names.get(meta.get(field), "-"), *(label for label, _value in listed)), picked))
        if picked:
            meta[field] = listed[picked - 1][1]

    def _held(self, held: Mapping, kind: str, fields: list[str], key, holder, document_id: str) -> str:
        prefab = next((found for field in fields if (found := held["catalogs"].get((kind, field), {}).get(key))),
                      None) if key else None
        rsz = self._user_file(prefab, document_id) if prefab else None
        return next((normalize_resource_path(plain_value(entry.get(holder[1], ""))) for entry in _of_type(
            rsz, holder[0])), "")

    def _swap_object(self, document: ScnSceneDocument, graph: ScnSceneGraph, holder: ScnSceneObject, name: str,
                     path: str) -> ScnSceneObject | None:
        rsz = self._user_file(path, document.document_id) if path else None
        root = next(iter(getattr(rsz, "gameobjects", None) or ()), None)
        if root is None:
            return None
        scn, own = document.rsz_file, rsz.object_table[root.id]
        local = max([len(scn.object_table) - 1, *document.object_by_local_id]) + 1
        instance = max([len(scn.instance_infos) - 1, *(key.instance_id for key in document.components)]) + 1
        fields = self._fields(rsz, own)
        item = ScnSceneObject(ScnObjectId(document.document_id, local), "gameobject", instance,
                              holder.id.local_object_id, name, self._instance_type_id(rsz, own),
                              self._type_name(rsz, own), fields,
                              draw_self=_bool_field(fields, self.fields.get("draw self"), True))
        for offset, source in enumerate(rsz.object_table[root.id + 1:root.id + 1 + root.component_count], 1):
            kind = self._type_name(rsz, source)
            if kind not in (VIA_TRANSFORM, VIA_RENDER_MESH):
                continue
            key = ScnComponentId(document.document_id, instance + offset)
            component = ScnComponent(key, item.id, None, self._instance_type_id(rsz, source), kind,
                                     self._fields(rsz, source))
            document.components[key] = component
            item.components.append(key)
            if kind == VIA_RENDER_MESH:
                item.mesh_component = key
            elif (transform := self._extract_transform(component, graph, item.id)) is not None:
                item.transform_component, item.transform = key, replace(transform, same_joints_constraint=True)
        document.objects[item.id] = item
        document.object_by_local_id[local] = item.id
        return item

    def _swap_sources(self, spec: Mapping, document_id: str) -> dict:
        if (key := ("swaps", self.game_version)) in self.user_reads:
            return self.user_reads[key]
        read = partial(self._user_file, document_id=document_id)
        holders = registered_holders(spec, read)
        rows = {name: {} for name in spec["rows"]}
        for kind, rsz, fields in holders:
            for name, rule in spec["rows"].items() if kind == spec["tables"]["type"] else ():
                for entry in _rows(read, _path(rsz, fields, rule["table"]), spec["tables"]["list"]):
                    if all(keys := tuple(map(entry.get, rule["keys"]))):
                        rows[name][keys] = entry
        wanted = {(part["catalogs"][0], field) for part in spec["parts"] for named in part["catalogs"][1:]
                  for field in _catalog_fields(named)}
        catalogs, listing = {key: {} for key in wanted}, spec["catalog"]
        for kind, rsz, fields in holders:
            for field in (field for wanted_kind, field in wanted if wanted_kind == kind):
                catalogs[kind, field] |= {entry[listing["key"]]: _plain_path(entry[listing["prefab"]])
                                          for entry in _rows(read, _path(rsz, fields, field), listing["list"])}
        items = spec["items"]
        found = {}
        for path in items["files"]:
            for entry in _rows(read, path, items["list"]):
                found.setdefault(entry[items["category"]], []).append((entry[items["id"]], entry[items["style"]]))
        manager, weapons = spec["manager"], spec["weapons"]
        rsz = read(manager["scene"])
        fields = next(_of_type(rsz, manager["type"]), {})
        self.user_reads[key] = {
            "rows": rows, "catalogs": catalogs, "items": found, "object": _path(rsz, fields, manager["object"]),
            "characters": edit_characters(spec["characters"], holders, read) if "characters" in spec else {},
            "weapons": {name: {(entry.get(table[1]) if len(table) > 1 else None, entry[weapons["key"]]): (
                entry[weapons["used"]], entry[weapons["parts"]]) for entry in _rows(
                read, _path(rsz, fields, table[0]), weapons["list"])} for name, table in weapons["tables"].items()}}
        return self.user_reads[key]


def registered_holders(spec: Mapping, read: Callable[[str], object | None]) -> list[tuple[str, object, Mapping]]:
    holders = []
    for scene in spec["registers"]["scenes"]:
        rsz = read(scene)
        for fields in _of_type(rsz):
            if (value := fields.get(spec["registers"]["field"])) is not None and (
                    prefab := read(resource_path(value, partial(_fields, rsz)))) is not None:
                holders += [(type_name_of(prefab, index), prefab, _fields(prefab, index))
                            for index in range(1, len(prefab.instance_infos))]
    return holders


def edit_characters(rule: Mapping, holders: list, read: Callable[[str], object | None]) -> dict:
    found = {}
    for kind, rsz, fields in holders:
        for table in getattr(fields.get(rule["tables"]), "values", None) or () if kind == rule["holder"] else ():
            for row in _rows(read, user_path(rsz, table), rule["list"]):
                found.setdefault(row[rule["key"]], [{entry[field]: {name: value for name, value in entry.items()
                                                                     if isinstance(value, int)} for entry in row[name]}
                                                    for name, field in rule["rows"].values()])
    return found


def character_levels(characters: Mapping, names: Mapping, picked: Callable[[int, int], int]) -> list[tuple]:
    labels = ("-", *(names.get(key, str(key)) for key in characters))
    levels = [(labels, picked(0, len(labels)))]
    for rows in list(characters.values())[levels[0][1] - 1] if levels[0][1] else ():
        levels.append((tuple(map(str, rows)), picked(len(levels), len(rows))))
    return levels


def _enabled(part: Mapping, rows: Mapping, made: Mapping, weapon: Callable[[str, object], int],
             bits: int) -> tuple[bool, ...] | None:
    if "enable" not in part:
        return None
    row = rows[part["row"]] or {}
    shown = _mask(part["enable"], row, rows, made, weapon) & ~(
        _mask(part.get("hidden", ()), row, rows, made, weapon) & ~int(row.get(part.get("kept")) or 0))
    return tuple(bool(shown >> bit & 1) for bit in range(bits))


def _weapon(tables: Mapping, rule: Mapping, table: str, mesh) -> int:
    used, parts = tables[table].get((mesh, rule["job"]), (0, 0))
    return parts if used & rule["hands"] else 0


def _mask(terms, row: Mapping, rows: Mapping, made: Mapping, weapon: Callable[[str, object], int]) -> int:
    value = 0
    for term in terms:
        if isinstance(term, str):
            value |= int(row.get(term) or 0)
        elif "weapon" in term:
            value |= weapon(term["weapon"], row.get(term["mesh"]) if "mesh" in term else None)
        elif all(_holds(condition, rows, made) for condition in term.get("if", [{"row": term.get("row")}])):
            value |= int(term["value"] if "value" in term else rows[term["row"]].get(term["field"]) or 0)
    return value


def _holds(condition: Mapping, rows: Mapping, made: Mapping) -> bool:
    if "part" in condition:
        found = condition["part"] in made
    else:
        row = rows.get(condition["row"])
        value = row.get(condition["field"]) if row is not None and "field" in condition else row
        if "has" in condition:
            found = (rows.get(condition["has"]["row"]) or {}).get(condition["has"]["field"]) in (value or ())
        elif "is" in condition:
            found = value == condition["is"]
        else:
            found = bool(int(value or 0) & condition["mask"]) if "mask" in condition else bool(value)
    return found != bool(condition.get("not"))


def _catalog_fields(named, species: str | None = None) -> list[str]:
    if isinstance(named, Mapping):
        return [field for value in (named.values() if species is None else [named.get(species, ())])
                for field in _catalog_fields(value)]
    return [named] if isinstance(named, str) else list(named)


def _of_type(rsz, type_name: str | None = None):
    for index in range(1, len(getattr(rsz, "instance_infos", ()))):
        if type_name is None or type_name_of(rsz, index) == type_name:
            yield _fields(rsz, index)


def _fields(rsz, index: int) -> Mapping:
    fields = rsz.parsed_elements.get(index, {})
    return fields if isinstance(fields, Mapping) else {}


def _path(rsz, fields: Mapping, name: str) -> str:
    return resource_path(fields.get(name), partial(_fields, rsz)) if rsz is not None else ""


def _rows(read: Callable[[str], object | None], path: str, name: str) -> list:
    rsz = read(path) if path else None
    return (root_fields(rsz) or {}).get(name) or [] if rsz is not None else []


def _plain_path(value) -> str:
    return value if isinstance(value, str) else next(
        (item for item in value.values() if isinstance(item, str) and item), "") if isinstance(value, Mapping) else ""
