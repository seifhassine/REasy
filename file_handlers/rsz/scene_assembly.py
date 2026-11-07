"""See docs/internals/character-assembly.md."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

lazy from file_handlers.rsz.plain_values import plain_value, root_fields, user_path
lazy from file_handlers.rsz.rsz_data_types import ObjectData, UserDataData
lazy from utils.hash_util import murmur3_hash_utf16le
lazy from utils.resource_file_utils import normalize_resource_path, resource_key

lazy from .character_catalogs import catalog_entries, catalog_files, catalog_groups, character_shells, spawn_templates
lazy from .scn_scene_graph import (
    VIA_MOTION_SKELETONS, ScnComponent, ScnComponentId, ScnFolderReference, ScnJoints, ScnObjectId, ScnSceneDocument,
    ScnSceneGraph, ScnSceneObject, _bool_field, _object_ref_id, _strip_value, joint_constraint_holders,
    normalize_scene_path,
)
lazy from .part_swapper import _plain_path
lazy from .model_conditions import condition_values, condition_writes, model_condition_rules, parts_shown
lazy from .spawn_rules import (
    FileFields, SpawnChoice, Spawned, choice_levels, choice_path, choices, field_pairs, field_values, opened,
    random_choices, resource_path, set_choices, spawn_choice_key, spawned, spawners, type_name_of, value_at,
)
lazy from .spawned_parts import SpawnedMesh


@dataclass(frozen=True, slots=True)
class EmbeddedFile:
    type_registry: object
    object_table: list
    instance_infos: list
    parsed_elements: dict
    rsz_userdata_infos: list


class ScnSceneAssembly:
    def _collect_spawner_host(self, document: ScnSceneDocument) -> None:
        scn = document.rsz_file
        instance_id = self._object_instance_id(scn, 0)
        type_name = self._type_name(scn, instance_id)
        if not getattr(scn, "is_usr", False) or type_name not in spawners(self.game_version):
            return
        type_id, fields = self._instance_type_id(scn, instance_id), self._fields(scn, instance_id)
        host = ScnSceneObject(ScnObjectId(document.document_id, 0), "gameobject", instance_id, -1,
                              Path(document.source_path).name, type_id, type_name, fields)
        component_id = ScnComponentId(document.document_id, instance_id)
        document.components[component_id] = ScnComponent(component_id, host.id, 0, type_id, type_name, fields)
        host.components.append(component_id)
        document.objects[host.id] = host
        document.object_by_local_id[0] = host.id

    def _hang_spawned(self, graph: ScnSceneGraph, document: ScnSceneDocument, instance_id: str,
                      reference: ScnFolderReference, child: ScnSceneDocument, child_instance_id: str) -> None:
        component = document.components[reference.spawner]
        spec = spawners(self.game_version).get(component.type_name)
        key, roots = (instance_id, reference.source_folder_id), [
            item for item in child.objects.values()
            if item.kind == "gameobject" and item.parent_id not in child.object_by_local_id]
        leads = spec is not None and spec.get("skeleton") == "base"
        leader = self._spawn_leaders.get(key) if leads else None
        if leads and leader is None and (first := next((item for item in roots if item.transform is not None and not
                                                        item.transform.same_joints_constraint), None)) is not None:
            self._spawn_leaders[key] = (child_instance_id, child, first)
        host_instance, hosting, host = leader or (instance_id, document, document.objects.get(
            reference.source_folder_id))
        if len(roots) != 1 or host is None or roots[0].transform is None or not roots[0].transform.parent_joint:
            return
        self._collect_object_joints(hosting, host, graph)
        if (joints := hosting.joints.get(host.id)) is not None and joints.index(
                roots[0].transform.parent_joint) is not None:
            graph.hung_spawns[child_instance_id] = (host_instance, host.id, roots[0].id)

    def _join_part_skeletons(self, graph: ScnSceneGraph) -> None:
        found, held = spawners(self.game_version), {}
        for instance in graph.document_instances.values():
            link = None if instance.source_link_index is None else graph.links[instance.source_link_index]
            source = None if link is None else graph.document_instances.get(link.source_document_instance_id)
            document = None if source is None or link.spawner is None else graph.documents.get(source.document_id)
            host = None if document is None else document.objects.get(link.source_folder_id)
            component = None if host is None else document.components.get(link.spawner)
            spec = None if component is None else found.get(component.type_name)
            if spec is not None:
                held.setdefault((source.instance_id, host.id), (document, host, component, spec, []))[4].append(
                    instance)
        for key, (document, host, spawner, spec, instances) in held.items():
            instances.sort(key=lambda instance: instance.source_link_index)
            roots = [(instance, graph.documents[instance.document_id], item) for instance in instances
                     for item in graph.documents[instance.document_id].objects.values()
                     if item.kind == "gameobject"
                     and item.parent_id not in graph.documents[instance.document_id].object_by_local_id]
            if "resources" in spec:
                self._set_resources(document, host, spawner, spec["resources"], roots, graph)
            if spec.get("skeleton") == "parts":
                for _instance, child, item in roots:
                    self._collect_object_joints(child, item, graph)
                if shell := self._character_shell(spec.get("shell"), document):
                    graph.shells[host.id] = shell
                if shell and shell[1]:
                    skeleton = next((fields for type_name, fields in shell[1] if type_name in VIA_MOTION_SKELETONS
                                     and _bool_field(fields, self.fields.get("skeleton auto apply"), True)), {})
                    path = normalize_scene_path(plain_value(skeleton.get(self.fields.get("skeleton resource"), "")))
                    document.joints[host.id] = path and self._resource_joints(path, document.document_id) or None
                elif joints := [child.joints[item.id] for _instance, child, item in roots
                                if child.joints.get(item.id)]:
                    document.joints[host.id] = ScnJoints.joined(joints)
            elif spec.get("skeleton") == "base" and (leader := self._spawn_leaders.get(key)):
                self._collect_object_joints(leader[1], leader[2], graph)
                graph.spawned_owners.update((instance.instance_id, leader[2].id) for instance in instances
                                            if instance.instance_id != leader[0])
                follows = spec.get("follows") or {}
                graph.followed_joints.update(
                    (instance.instance_id, tuple(_strip_value(getattr(name, "value", "")) for name in getattr(
                        component.fields.get(follows["joints"]), "values", ())))
                    for instance, child, item in roots if follows and instance.instance_id != leader[0]
                    for component in (child.components[c] for c in item.components)
                    if component.type_name == follows["component"])
            elif "skeleton" not in spec and (spec.get("same joints") or "reparent" in spec or any(
                    item.transform is not None and item.transform.same_joints_constraint for *_found, item in roots)):
                self._collect_object_joints(document, host, graph)
            if spec.get("same joints"):
                graph.same_joints.update((instance.instance_id, frozenset(
                    item.id for other, _child, item in roots if other is instance)) for instance in instances)
            if "reparent" in spec:
                graph.reparented.update((instance.instance_id, frozenset(
                    item.id for other, child, root in roots if other is instance for item in child.objects.values()
                    if child.object_by_local_id.get(item.parent_id) == root.id and not any(
                        child.components[c].type_name == spec["reparent"]["except"] for c in item.components)))
                    for instance in instances)
            replaced = {(instance.instance_id, component.type_name) for instance, child, item in roots
                        for component in (child.components[c] for c in item.components)
                        if component.type_name in spec.get("replaces", ())}
            hidden = {(instance.instance_id, item.id) for instance in instances
                      for child in (graph.documents[instance.document_id],) for item in child.objects.values()
                      if item.parent_id in child.object_by_local_id and any(
                          (other, child.components[c].type_name) in replaced for c in item.components
                          for other, _ in replaced if other != instance.instance_id)}
            for renderable in graph.renderables if hidden else ():
                document_of = graph.documents[renderable.source_object_id.document_id]
                current, seen = document_of.objects.get(renderable.source_object_id), set()
                while current is not None and current.id not in seen:
                    if (renderable.document_instance_id, current.id) in hidden:
                        renderable.visible_by_default = False
                        break
                    seen.add(current.id)
                    current = document_of.objects.get(document_of.object_by_local_id.get(current.parent_id))
        for document in graph.documents.values():
            for component in document.components.values():
                if (spec := found.get(component.type_name)) is not None and "resources" in spec and not {
                        "prefabs", "spawns"} & spec.keys() and (host := document.objects.get(component.owner)) \
                        is not None and _bool_field(component.fields, self.fields.get("enabled"), True):
                    self._set_resources(document, host, component, spec["resources"], [], graph)

    def _show_spawn_template(self, document: ScnSceneDocument) -> None:
        if (held := ("spawn templates", self.game_version)) not in self.user_reads:
            self.user_reads[held] = spawn_templates(self.game_version, partial(
                self._user_file, document_id=document.document_id))
        if resource_key(document.document_id) in self.user_reads[held]:
            for item in document.objects.values():
                if item.kind == "gameobject" and item.parent_id not in document.object_by_local_id:
                    item.draw_self = True

    def _character_shell(self, spec: Mapping | None, document: ScnSceneDocument) -> tuple[str, tuple, tuple] | None:
        if not spec:
            return None
        read = partial(self._user_file, document_id=document.document_id)
        if (held := ("character kinds", spec["kinds"])) not in self.user_reads:
            self.user_reads[held] = catalog_groups(self.game_version, read, spec["kinds"])
        if not (kind := self.user_reads[held].get(resource_key(document.document_id))):
            return None
        if (held := ("character shells", spec["pools"], spec["kind"], spec["updater"])) not in self.user_reads:
            self.user_reads[held] = character_shells(self.game_version, spec["pools"], read, spec["kind"],
                                                     spec["updater"])
        return kind, *(self.user_reads[held].get(kind) or ((), ()))

    def _set_resources(self, document: ScnSceneDocument, host: ScnSceneObject, spawner: ScnComponent, rules: Mapping,
                       roots: list, graph: ScnSceneGraph) -> None:
        resolve = FileFields(document.rsz_file, partial(self._opened_file, document.document_id))
        values = {name: next((value for path in paths
                              if (value := value_at(spawner.fields, path, resolve)) is not None), None)
                  for name, paths in rules.get("values", {}).items()}
        table = resolve.follow(rules["file"]) if "file" in rules else None
        root = table(table.rsz.object_table[0]) if table and type_name_of(
            table.rsz, table.rsz.object_table[0]) == rules["type"] else {}
        changed = {host.id: (document, host)}
        for row in rules["sets"]:
            if any(values[name] not in (wanted if isinstance(wanted, list) else [wanted])
                   for name, wanted in row.get("when", {}).items()):
                continue
            on = row.get("on")
            objects = [(document, host)] if on is None else [(child, top) for _instance, child, top in roots] \
                if on == "root" else [(child, item) for _instance, child, top in roots
                                      if (item := self._referenced(child, top, on)) is not None]
            if "value" in row:
                reached = []
            elif "table" in row:
                reached = [(*pair, None) for pair in field_pairs(root, row["table"], table)]
            elif "fields" in row:
                reached = [(*pair, spawner.id) for pair in field_pairs(spawner.fields, row["fields"], resolve)]
            else:
                reached = [(*pair, c) for _instance, child, top in roots for item in child.objects.values()
                           if child.object_by_local_id.get(item.parent_id) == top.id for c in item.components
                           if child.components[c].type_name == row["spawned"][0]
                           for pair in field_pairs(child.components[c].fields, row["spawned"][1:],
                                                   child.rsz_file.parsed_elements.get)]
            found = [(row["value"], None)] if "value" in row else [
                (normalize_scene_path(path), source) for value, here, source in reached
                if (path := resource_path(value, here))]
            for owner, item in objects:
                target = next((owner.components[c] for c in item.components
                               if owner.components[c].type_name == row["component"]), None)
                if target is None or "empty" in row and plain_value(target.fields.get(row["empty"])):
                    continue
                changed[item.id] = owner, item
                graph.lent.update((source, path) for path, source in found if source is not None)
                held = owner.resources.setdefault(target.id, {})
                if row.get("adds"):
                    held[row["field"]] = (*held.get(row["field"], ()), *(path for path, _source in found))
                elif found:
                    held[row["field"]] = found[0][0]
        holders = joint_constraint_holders(self.game_version)
        for owner, item in changed.values():
            owner.joints.pop(item.id, None)
            owner.constraints.pop(item.id, None)
            self._collect_object_joints(owner, item, graph)
            self._collect_object_constraints(owner, item, holders)

    def _referenced(self, document: ScnSceneDocument, item: ScnSceneObject, on: Mapping) -> ScnSceneObject | None:
        component = next((document.components[c] for c in item.components
                          if document.components[c].type_name == on["component"]), None)
        refs = {(info.object_id, info.property_id, info.array_index): info.target_id
                for info in getattr(document.rsz_file, "gameobject_ref_infos", ())}
        found = None if component is None else self._guid_objects(document).get(getattr(
            component.fields.get(on["ref"]), "raw_bytes", None)) or document.object_by_local_id.get(
            refs.get((component.object_table_id, on["property"], 0)))
        return None if found is None else document.objects.get(found)

    def _collect_spawned_prefabs(self, document: ScnSceneDocument, scene_object: ScnSceneObject, entries,
                                 graph: ScnSceneGraph) -> None:
        resolve = FileFields(document.rsz_file, partial(self._opened_file, document.document_id))
        for component_id in scene_object.components:
            component = document.components.get(component_id)
            spawner = None if component is None else entries.get(component.type_name)
            if spawner is None or not _bool_field(component.fields, self.fields.get("enabled"), True) or (
                    spawner.get("drawn only") and not self._chain_drawn(document, scene_object)):
                continue
            fields, here = component.fields, resolve
            if "spawns" in spawner:
                if (found := self._created(spawner, fields, resolve, entries, document.document_id)) is None:
                    continue
                spawner, fields, here = found
            key = partial(spawn_choice_key, document.document_id, component.id.instance_id)
            levels, complete = choice_path(fields, spawner, here, self.game_version, lambda level, count: (
                picked if 0 <= (picked := self.choices.get(key(level), 0)) < count else 0))
            for level, (listed, picked) in enumerate(levels):
                graph.spawn_choices.append(SpawnChoice(key(level), f"{scene_object.name} · {listed[picked][2]}",
                                                       tuple(label for label, *_ in listed), picked))
            chosen = [(fields, here), *((listed[picked][1], listed[picked][3])
                                        for listed, picked in levels)] if complete else []
            lists = ["", *(listed[picked][2] for listed, picked in levels)]
            if chosen and "slots" in spawner:
                merged = _merged(spawner.get("merge"), {document.components[c].type_name
                                                        for c in scene_object.components}, *levels[0]) if levels else []
                self._fill_slots(graph, document, scene_object, spawner["slots"], key("slots"),
                                 [(levels[-1][0][levels[-1][1]][0] if levels else "", *chosen[-1]), *merged])
            found = [*self._base_spawns(spawner, chosen, lists[-1], document.document_id),
                     *spawned(*chosen[-1][:1], spawner, chosen[-1][1]),
                     *self._looked_up(spawner, chosen, document.document_id)] if chosen and "prefabs" in spawner else []
            if "montages" in spawner:
                found += self._montage_spawns(spawner, key("montage"), scene_object.name, document.document_id, graph)
            own = {normalize_scene_path(item.path) for item in found}
            found += [item for item in self._joined_spawns(spawner, chosen, document.document_id)
                      if normalize_scene_path(item.path) not in own] if chosen else []
            child_draw = []
            children = spawner.get("children")
            if children:
                for entry in getattr(component.fields.get(children["list"]), "values", None) or ():
                    fields = self._fields(document.rsz_file, _object_ref_id(entry))
                    name = _strip_value(getattr(fields.get(children["name"]), "value", ""))
                    if name:
                        child_draw.append((name, _bool_field(fields, children["draw"], True)))
            document.folder_references.extend(
                ScnFolderReference(holder, path, None, tuple(child_draw), component.id, item.parts_off, item.materials,
                                   item.name)
                for item in found if (path := normalize_scene_path(item.path))
                and (holder := self._holder(document, scene_object, item.under)) is not None)

    def _montage_spawns(self, spawner: Mapping, key: str, name: str, document_id: str,
                        graph: ScnSceneGraph) -> list[Spawned]:
        spec, rule = spawner["montages"], spawner["montage rows"]
        held = ("montages", *spec["registers"], spec["type"], spec["table"], spec["parts"])
        if held not in self.user_reads:
            rows = []
            for path in spec["registers"]:
                rsz = self._user_file(path, document_id)
                register = next((self._fields(rsz, index) for index in range(1, len(getattr(rsz, "instance_infos", ())))
                                 if self._type_name(rsz, index) == spec["type"]), None)
                read = [self._opened_file(document_id, rsz, register.get(spec[part])) for part in ("table", "parts")] \
                    if register is not None else [None, None]
                table, parts = (root_fields(file) or {} if file is not None else {} for file in read)
                rows += [(row, parts) for row in table.get(rule["rows"]) or ()]
            self.user_reads[held] = rows
        rows = self.user_reads[held]
        if not rows:
            return []
        picked = picked if 0 <= (picked := self.choices.get(key, 0)) < len(rows) else 0
        graph.spawn_choices.append(SpawnChoice(key, f"{name} · montage", tuple(
            str(row.get(rule["id"])) for row, _parts in rows), picked))
        row, parts = rows[picked]
        found = []
        for field, listed in rule["keys"].items():
            prefabs = {item.get(rule["key"]): item.get(rule["prefab"]) for item in parts.get(listed) or ()}
            for part in row.get(field) if isinstance(row.get(field), list) else [row.get(field)]:
                if path := _plain_path(prefabs.get(part)):
                    found.append(Spawned(path))
        return found

    def _holder(self, document: ScnSceneDocument, scene_object: ScnSceneObject, under) -> ScnObjectId | None:
        component = next((document.components[c] for c in scene_object.components
                          if under and document.components[c].type_name == under["component"]), None)
        return scene_object.id if component is None else self._guid_objects(document).get(
            getattr(component.fields.get(under["ref"]), "raw_bytes", None))

    def _spawn_materials(self, graph: ScnSceneGraph, materials, child: ScnSceneDocument, instance_id: str) -> None:
        names = self._material_param_names()
        if not materials or names is None:
            return
        writes = tuple(frozendict({names.material: material, names.variable: variable, names.vector: value})
                       for material, variable, value in materials)
        for item in child.objects.values():
            if item.mesh_component is not None and item.parent_id not in child.object_by_local_id:
                held = graph.spawned_meshes.get((instance_id, item.mesh_component), SpawnedMesh())
                graph.spawned_meshes[instance_id, item.mesh_component] = replace(held, writes=held.writes + writes)

    def _opened_file(self, document_id: str, rsz, value):
        return opened_file(rsz, value, partial(self._user_file, document_id=document_id), self._user_paths)

    def _fill_slots(self, graph: ScnSceneGraph, document: ScnSceneDocument, holder: ScnSceneObject, spec: Mapping,
                    key: str, layers: list) -> None:
        named, own = {}, partial(self._fields, document.rsz_file)
        for item in document.objects.values():
            current, seen = item, set()
            while current is not None and current.id not in seen and current.id != holder.id:
                seen.add(current.id)
                current = document.objects.get(document.object_by_local_id.get(current.parent_id))
            for component in (document.components[c] for c in item.components
                              if current is not None and current.id == holder.id):
                if component.type_name == spec["component"]:
                    named.setdefault(value_at(component.fields, spec["field"], own), (item, component))
        paths, kept = spec["parts"], {}
        layers = [self._montage(graph, holder, f"{key}|{layer}", spec.get("montage"), *found)
                  for layer, found in enumerate(layers)]
        random, materials, infos = spec.get("random"), spec.get("materials"), spec.get("infos", {})
        names = None if materials is None else self._material_param_names()
        formats = model_condition_rules(self.game_version) if infos else {}
        groups = [{} if random is None or value_at(fields, random["lock"], resolve) else {
            value_at(group, random["name"], inner): (group, inner) for value, place in field_pairs(
                fields, [*random["groups"], "*"], resolve) for group, inner in (opened(value, place),)}
            for fields, resolve, _added in layers]
        for layer, part, here in ((layer, *pair) for layer, (fields, resolve, _added) in enumerate(layers)
                                  for steps in ([paths] if isinstance(paths[0], str) else paths)
                                  for pair in field_pairs(fields, [*steps, "*"], resolve)):
            entry, there = opened(part, here)
            name = value_at(entry, spec["name"], there)
            item, slot = named.get(name, (None, None))
            priority = value_at(entry, spec["priority"], there) or 0 if "priority" in spec else 0
            if item is None or item.mesh_component is None or kept.get(item.mesh_component, (False, 0)) >= (
                    True, priority):
                continue
            condition = value_at(entry, spec["condition"], there) if "condition" in spec else None
            fields, resolve, added = layers[layer]
            ignore = materials is not None and bool(value_at(fields, materials["ignore"], resolve))
            material = value_at(entry, materials["id"], there) if materials and not ignore else None
            if listed := set_choices(entry, spec.get("sets"), there, not ignore) or random_choices(
                    entry, groups[layer].get(name), random, there):
                choice = f"{key}|{layer}|{name}"
                picked = self.choices.get(choice, 0) if 0 <= self.choices.get(choice, 0) < len(listed) else 0
                graph.spawn_choices.append(SpawnChoice(choice, f"{holder.name} · {item.name}",
                                                       tuple(label for label, *_ in listed), picked))
                _label, entry, there, condition, material = listed[picked]
            enables = {int(value_at(found, spec["enables"]["index"], inner)): bool(
                value_at(found, spec["enables"]["on"], inner)) for value, place in field_pairs(
                entry, [*spec["enables"]["list"], "*"], there) for found, inner in (opened(value, place),)} \
                if "enables" in spec else parts_shown(*_info(entry, infos["parts"]["main"], there), formats["parts"],
                                                       condition) if "shown" in spec else {}
            mesh = normalize_resource_path(value_at(entry, spec["mesh"], there) or "")
            document.slotted[item.mesh_component] = SpawnedMesh(
                mesh, normalize_resource_path(value_at(entry, spec["material"], there) or ""),
                condition_writes(condition_values(*_info(entry, infos["materials"]["main"], there),
                                                  formats["materials"], [material, *added], self.game_version), names)
                if names else (), enabled=tuple(enables.get(index, True) for index in range(max(enables) + 1))
                if enables else None, infos=_slot_infos(infos, entry, there, slot.fields, own))
            kept[item.mesh_component] = (bool(mesh), priority)

    def _montage(self, graph: ScnSceneGraph, holder: ScnSceneObject, key: str, spec, label: str, fields, resolve):
        found = field_pairs(fields, [spec["data"]], resolve) if spec else []
        fields, resolve = opened(*found[0]) if found else (fields, resolve)
        added = []
        for _step in range(4) if spec else ():
            if inner := field_pairs(fields, [spec["inner"]], resolve):
                added[:0] = value_at(fields, spec["materials"], resolve) or [] if "materials" in spec else []
                fields, resolve = opened(*inner[0])
            elif listed := choices(fields, {"list": [spec["choices"]]}, resolve):
                picked = self.choices.get(key, 0) if 0 <= self.choices.get(key, 0) < len(listed) else 0
                graph.spawn_choices.append(SpawnChoice(key, f"{holder.name} · {label}",
                                                       tuple(name for name, *_ in listed), picked))
                fields, resolve = listed[picked][1], listed[picked][3]
            else:
                break
        return fields, resolve, added

    def _joined_spawns(self, spawner: Mapping, chosen: list, document_id: str) -> list[Spawned]:
        spec = spawner.get("extends")
        if spec is None:
            return []
        wanted = [_chosen_value(chosen, name) for name in spec["match"]]
        found = []
        for listing, value in self._catalog_entries(spec, document_id):
            other = self._user_file(user_path(listing, value), document_id)
            entry = self._user_fields(user_path(listing, value), document_id) or {}
            if other is not None and [value_at(entry, name, partial(self._fields, other))
                                      for name in spec["match"]] == wanted:
                found += spawned(entry, spawner, partial(self._fields, other))
        return found

    def _base_spawns(self, spawner: Mapping, chosen: list, listed: str, document_id: str) -> list[Spawned]:
        specs = spawner.get("base", ())
        for spec in [specs] if isinstance(specs, Mapping) else specs:
            if "path" in spec or "paths" in spec:
                path = spec.get("path") or spec["paths"].get(listed)
            elif "field" in spec:
                path = next((found for fields, resolve in reversed(chosen) for value, here in field_pairs(
                    fields, spec["field"], resolve) if (found := resource_path(value, here))), None)
            else:
                path = self._catalog_prefab(spec, self._naming_key(spec["named by"], document_id) if "named by" in spec
                                            else _chosen_value(chosen, spec["match"]), document_id)
            if path:
                return [Spawned(path)]
        return []

    def _naming_key(self, spec: Mapping, document_id: str):
        own = resource_key(document_id)
        for listing, value in self._catalog_entries(spec, document_id):
            here = partial(self._fields, listing)
            entry = value if isinstance(value, Mapping) else here(_object_ref_id(value))
            if any(resource_key(resource_path(found, here)) == own
                   for found in field_values(entry, spec["file"], here)):
                return value_at(entry, spec["key"], here)
        return None

    def _looked_up(self, spawner: Mapping, chosen: list, document_id: str) -> list[Spawned]:
        spec = spawner.get("looked up")
        parts = [] if spec is None else [opened(value, place) for value, place in field_pairs(
            *chosen[-1][:1], [*spec["list"], "*"], chosen[-1][1])]
        if not any(value_at(part, spec["when"], there) for part, there in parts):
            return []
        listing, keyed = {**spec, "list": spec["lists"][int(_chosen_value(chosen, spec["by"]) or 0)]}, {}
        for catalog, value in self._catalog_entries(listing, document_id):
            here = partial(self._fields, catalog)
            entry = value if isinstance(value, Mapping) else here(_object_ref_id(value))
            keyed.setdefault(tuple(value_at(entry, key, here) for key in spec["key"]), (entry, here))
        found = []
        for index, (part, there) in enumerate(parts):
            entry, here = keyed.get(tuple(value_at(part, key, there) for key in spec["key"]), ({}, None)) \
                if value_at(part, spec["when"], there) else ({}, None)
            prefabs = field_values(entry, [*spec["prefabs"], "*"], here) if here is not None else []
            if index < len(prefabs) and (path := resource_path(prefabs[index], here)):
                found.append(Spawned(path))
        return found

    def _created(self, creator: Mapping, fields: Mapping, resolve, entries: Mapping, document_id: str):
        spec, choice = creator["spawns"], creator["spawns"].get("entry", {})
        if "when" in spec and value_at(fields, spec["when"], resolve) != spec.get("is", True):
            return None
        files = [resolve.follow(path) for path in self._catalog_paths(spec, fields, resolve, document_id)] \
            if "catalog" in spec else [place.follow(value) for value, place in field_pairs(
                fields, spec["file"], resolve)]
        wanted = tuple(murmur3_hash_utf16le(value or "") if choice.get("hash") else value
                       for value in (value_at(fields, path, resolve) for path in choice.get("match", ())))
        spawner = entries[spec["type"]] | {key: value for key, value in creator.items()
                                           if key not in ("spawns", "if set")}
        for name, replaced in creator.get("if set", {}).items():
            spawner = spawner | replaced if value_at(fields, name, resolve) else spawner
        levels = choice_levels(spawner)
        for here in filter(None, files):
            found = next(((here(index), here) for index in (here.rsz.object_table[0], *range(len(
                here.rsz.instance_infos))) if type_name_of(here.rsz, index) == spec["type"]), None)
            if found and choice:
                if (key := (id(here.rsz), spec["type"], str(choice["key"]))) not in self._entries_cache:
                    lists, keyed = levels[0]["list"], self._entries_cache.setdefault(key, {})
                    for steps in [lists] if isinstance(lists[0], str) else lists:
                        for entry, there in (opened(*pair) for pair in field_pairs(found[0], [*steps, "*"], here)):
                            keyed.setdefault(tuple(value_at(entry, path, there) for path in choice["key"]),
                                             (entry, there))
                found = self._entries_cache[key].get(wanted)
            if found:
                return spawner | {"choices": levels[1:]} if choice else spawner, *found
        return None

    def _catalog_paths(self, spec: Mapping, fields: Mapping, resolve, document_id: str) -> list[str]:
        read = partial(self._user_file, document_id=document_id)
        if "key" not in spec:
            return [entry.path for entry in catalog_entries(self.game_version, read, spec["catalog"])]
        held = (document_id, "catalog", spec["catalog"], *spec["key"])
        if held not in self.user_reads:
            keyed = {}
            for entry in catalog_entries(self.game_version, read, spec["catalog"]):
                if (rsz := read(entry.path)) is not None and rsz.object_table:
                    here = partial(self._fields, rsz)
                    keyed.setdefault(tuple(value_at(here(rsz.object_table[0]), name, here) for name in spec["key"]),
                                     entry.path)
            self.user_reads[held] = keyed
        found = self.user_reads[held].get(tuple(value_at(fields, name, resolve) for name in spec["match"]))
        return [] if found is None else [found]

    def _catalog_prefab(self, spec: Mapping, wanted, document_id: str) -> str | None:
        for listing, value in self._catalog_entries(spec, document_id):
            entry_resolve = partial(self._fields, listing)
            entry = value if isinstance(value, Mapping) else entry_resolve(_object_ref_id(value))
            if value_at(entry, spec["key"], entry_resolve) == wanted:
                return resource_path(next(iter(field_values(entry, spec["prefab"], entry_resolve)), None),
                                     entry_resolve)
        return None

    def _catalog_entries(self, spec: Mapping, document_id: str):
        for catalog in spec["catalogs"] if "catalogs" in spec else catalog_files(self.game_version, spec["catalog"]):
            listing = self._user_file(catalog, document_id)
            if listing is None or not listing.object_table \
                    or self._type_name(listing, listing.object_table[0]) != spec["type"]:
                continue
            root = self._user_fields(catalog, document_id) or {}
            for value in field_values(root, [*spec["list"], "*"], partial(self._fields, listing)):
                yield listing, value


def _info(fields: Mapping, path, resolve: Callable[[int], Mapping]) -> tuple[Mapping, Callable]:
    found = field_pairs(fields, path, resolve)
    return opened(*found[0]) if found else ({}, resolve)


def _slot_infos(infos: Mapping, entry: Mapping, resolve: Callable[[int], Mapping], slot: Mapping,
                own: Callable[[int], Mapping]) -> tuple[tuple[str, int, str], ...]:
    found = []
    for kind, rule in infos.items():
        start, end = value_at(slot, rule["range"], own) or (0, -1)
        main = [resource_path(value, here) for value, here in field_pairs(entry, rule["main"], resolve)][:1] or [""]
        extras = [resource_path(value, here) for value, here in field_pairs(entry, rule["extras"], resolve)]
        listed = [*main, *extras][:end + 1] if rule.get("joined") else [*main, *extras[:end + 1]]
        found += [(kind, start + index, "") for index in range(end + 1)] if rule.get("cleared") else []
        found += [(kind, start + index, path) for index, path in enumerate(listed) if path]
    return tuple(found)


def opened_file(rsz, value, user_file: Callable[[str], object | None], user_paths: dict):
    if isinstance(value, (ObjectData, UserDataData)) and (block := next((
            info for info in getattr(rsz, "rsz_userdata_infos", ()) or ()
            if info.instance_id == int(value.value or 0) and getattr(info, "embedded_object_table", None)), None)):
        return EmbeddedFile(rsz.type_registry, block.embedded_object_table, block.embedded_instance_infos,
                            block.embedded_instances, block.embedded_userdata_infos)
    if isinstance(value, str):
        path = value
    elif isinstance(value, UserDataData):
        path = user_path(rsz, value)
    elif isinstance(value, ObjectData):
        if id(rsz) not in user_paths or user_paths[id(rsz)][0] is not rsz:
            user_paths[id(rsz)] = rsz, {info.instance_id: text.rstrip("\0") for info, text in getattr(
                rsz, "_rsz_userdata_str_map", {}).items()}
        path = user_paths[id(rsz)][1].get(int(value.value or 0), "")
    else:
        return None
    return user_file(path) if path else None


def _merged(spec, held: set[str], listed: list, picked: int) -> list:
    rule = next((rule for rule in (spec or {}).get("rules", ()) if rule["holder"] in held), None)
    keys = [value_at(entry, spec["key"], there) for _label, entry, _list, there in listed] if rule else []

    def inside(value, bands) -> bool:
        return value is not None and any((low is None or value >= low) and (high is None or value <= high)
                                         for low, high in bands)
    return [(label, entry, there) for index, (label, entry, _list, there) in enumerate(listed)
            if index != picked and inside(keys[index], rule["adds"])] if keys and inside(keys[picked], rule["when"]) \
        else []


def _chosen_value(chosen: list, path):
    return next((found for fields, resolve in reversed(chosen)
                 if (found := value_at(fields, path, resolve)) is not None), None)
