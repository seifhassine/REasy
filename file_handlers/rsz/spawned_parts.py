"""See docs/internals/material-variations.md."""
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Mapping

lazy from file_handlers.rsz.plain_values import plain_value, user_path
lazy from utils.resource_file_utils import normalize_resource_path

lazy from .material_variations import VariationEntry, chosen, enum_names, file_entries, variation_choices, \
    variation_entries, variation_rules
lazy from .scn_scene_graph import ScnComponentId, ScnSceneGraph, joint_constraint_holders
lazy from .spawn_rules import spawners


@dataclass(frozen=True, slots=True)
class SpawnedMesh:
    mesh: str = ""
    material: str = ""
    writes: tuple[Mapping, ...] = ()
    part: str = ""
    entries: tuple[VariationEntry, ...] = ()
    enabled: tuple[bool, ...] | None = None
    infos: tuple[tuple[str, int, str], ...] = ()
    normal_recalculation: bool | None = None


NOT_SPAWNED = SpawnedMesh()


@dataclass(frozen=True, slots=True)
class MaterialVariation:
    key: str
    label: str
    data: str
    base: str
    choices: tuple[tuple[int, str], ...]
    applied: int
    fixed: bool
    parent: str
    spawner: ScnComponentId
    child: str


@dataclass(frozen=True, slots=True)
class PartColours:
    variation: MaterialVariation
    part: str
    mesh: str
    materials: dict[str, tuple[str, tuple[tuple[VariationEntry, bool], ...]]]


def repicked(builder, graph) -> tuple[ScnSceneGraph, dict[str, Mapping]]:
    picked = ScnSceneGraph(graph.root_document_id, graph.root_instance_id)
    for variation in graph.variations:
        spawn_parts(builder, picked, graph.documents[variation.parent], variation.spawner,
                    graph.documents[variation.child], variation.key)
    states = {}
    for item in graph.renderables:
        spawned = picked.spawned_meshes.get((item.document_instance_id, item.source_component_id))
        if spawned is not None:
            document = graph.documents[item.source_object_id.document_id]
            base = (item.document_instance_id, item.source_object_id) in graph.startup_unwritten
            states[item.key] = builder._mesh_state(document, item.source_object_id,
                                                   document.components[item.source_component_id],
                                                   document.startup_materials_base if base else None, spawned.writes)
    return picked, states


def spawn_parts(builder, graph, document, spawner, child, instance_id: str) -> None:
    component = document.components[spawner]
    parts = spawners(builder.game_version)[component.type_name].get("parts")
    holder = None if parts is None else next(
        (item for item in child.components.values() if item.type_name == parts["holder"]), None)
    if holder is None:
        return
    refs = {(info.object_id, info.property_id, info.array_index): info.target_id
            for info in child.rsz_file.gameobject_ref_infos}
    objects = {int(key): target for key, name in parts["refs"].items() if (target := child.object_by_local_id.get(
        refs.get((holder.object_table_id, parts["prefab ids"][name], 0)))) is not None}
    fields = partial(builder._fields, document.rsz_file)
    assigned, datas = {}, {}
    for entry in getattr(builder._field_at(document, component.fields, parts["list"]), "values", None) or ():
        values = fields(entry.value)
        target = objects.get(getattr(values.get(parts["key"]), "value", None))
        if target is not None:
            assigned.setdefault(target, tuple(normalize_resource_path(getattr(values.get(parts[name]), "value", ""))
                                              for name in ("mesh", "material")))
            datas.setdefault(target, user_path(document.rsz_file, values.get(parts.get("constraints"))))
    entries = _variation_entries(builder, graph, document, component, child, holder, instance_id)
    names = builder._material_param_names()
    writes = {} if names is None or names.material is None else {
        part: tuple(frozendict({names.material: entry.material, names.variable: entry.parameter,
                                **({names.scalar: entry.values[0]} if len(entry.values) == 1
                                   else {names.vector: entry.values})})
                    for entry in found if entry.applied) for part, found in entries.items()}
    labels = enum_names(builder.game_version, parts["enum"]) if "enum" in parts else {}
    holders = joint_constraint_holders(builder.game_version) if "constraints" in parts else {}
    for part, target in objects.items():
        mesh, material = assigned.get(target, ("", ""))
        if (component_id := child.objects[target].mesh_component) is not None and (
                mesh or material or writes.get(part)):
            graph.spawned_meshes[instance_id, component_id] = SpawnedMesh(
                mesh, material, writes.get(part, ()), labels.get(part, str(part)), tuple(entries.get(part, ())))
        jointed = [key for key in child.objects[target].components
                   if "data" in holders.get(getattr(child.components.get(key), "type_name", None), {})]
        lent = {"data": datas.get(target, "")} | ({"updater": datas[target]} if part in parts.get(
            "updaters", ()) and datas.get(target) else {})
        for key in jointed:
            child.resources.setdefault(key, {}).update(lent)
        if not jointed and datas.get(target):
            child.created.setdefault(target, {}).update(
                (kind, lent) for kind, spec in holders.items() if "data" in spec)
        if jointed or datas.get(target):
            child.constraints.pop(target, None)
            builder._collect_object_constraints(child, child.objects[target], holders)


def _variation_entries(builder, graph, document, component, child, holder,
                       instance_id: str) -> dict[int, list[VariationEntry]]:
    spec = spawners(builder.game_version)[component.type_name].get("variation")
    rule = None if spec is None else variation_rules(builder.game_version).get(spec["controller"])
    controller = None if rule is None else next(
        (child.components[item] for item in child.objects[holder.owner].components
         if item in child.components and child.components[item].type_name == spec["controller"]), None)
    path = "" if controller is None else user_path(
        document.rsz_file, builder._field_at(document, component.fields, spec["data"]))
    table = _user_data(builder, rule, document.document_id, path)
    if table is None:
        if path:
            builder._warn(graph, "material_variation_unread",
                          f"Unable to read {path}: its material variation is not applied.", document.document_id,
                          instance_id, component.owner, component.id, path)
        return {}
    own = {name: getattr(controller.fields.get(name), "value", None)
           for name in (rule["choice"], rule["fixed"], rule["fixed choice"])}
    applied = chosen(rule, own, builder.variations.get(instance_id))
    graph.variations.append(MaterialVariation(
        instance_id, rule["label"], path, rule["base"], variation_choices(rule, table, builder.game_version), applied,
        bool(own[rule["fixed"]]), document.document_id, component.id, child.document_id))
    return variation_entries(rule, table, applied, partial(_user_data, builder, rule, document.document_id))


def _user_data(builder, rule: Mapping, document_id: str, path: str, base: bool | None = None) -> dict | None:
    kept = {} if normalize_resource_path(path) in builder.user_files else builder.user_reads
    if (document_id, path, base) not in kept:
        user, root = builder._user_file(path, document_id), None
        if user is not None and user.object_table:
            fields = partial(builder._fields, user)
            root = {name: plain_value(value, fields) for name, value in fields(user.object_table[0]).items()}
        kept[document_id, path, base] = root if base is None else file_entries(rule, root, path, base)
    return kept[document_id, path, base]
