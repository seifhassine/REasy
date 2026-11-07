"""See docs/internals/sequence-tracks.md, How games read them."""
from __future__ import annotations

from functools import partial
from typing import NamedTuple

lazy from file_handlers.mmtr.draw_rules import MESH_COMPONENT, draw_rules_for
lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.rsz.character_catalogs import rsz_reader
lazy from file_handlers.rsz.model_conditions import condition_values, holder_infos, model_condition_rules, parts_shown
lazy from file_handlers.rsz.plain_values import plain_fields, plain_value, root_fields, typed_fields
lazy from file_handlers.rsz.scene_assembly import opened_file
lazy from file_handlers.rsz.spawn_rules import FileFields
lazy from utils.hash_util import murmur3_hash_utf16le
lazy from utils.resource_file_utils import resource_key

from .banks import handler_reader
from .layers import motion_component
from .model import Motion
from .rules import sequence_track_rules
from .sequence_tracks import Fade, TrackPlayer, TrackTargets, condition_pairs, driven


def _user_root(users, path) -> dict | None:
    return root_fields(rsz) if path and (rsz := users(path)) is not None else None


class _TrackScene(NamedTuple):
    game: dict
    document: object
    item: object
    holder: object
    held: dict
    registry: object
    targets: TrackTargets
    users: object
    kinds: set
    present: set
    nodes: list


class MotionPreviewTracks:
    def _track_player(self, motion, holders: tuple) -> TrackPlayer | None:
        if not isinstance(motion, Motion) or not holders or self.prefab is None:
            return None
        game = sequence_track_rules(self.prefab.game_version)
        holder = holders[0]
        document = next((graph.documents[holder.document_id] for graph in self.scene.graphs
                         if holder.document_id in graph.documents), None)
        item = None if document is None else document.objects.get(holder)
        if item is None or not game:
            return None
        registry = document.rsz_file.type_registry
        kinds, held = set(), {}
        for component in item.components:
            if (found := document.components.get(component)) is not None:
                kinds.add(found.type_name)
                kinds.update(registry.getTypeParents(found.type_name) if registry is not None else ())
                held.setdefault(found.type_name, found)
        users = rsz_reader(self.prefab.game_version, lambda path: None if (data := handler_reader(self.prefab)(
            path)) is None else (path, data), {})
        kinds.update(self._uniques(game.get("uniques"), document, users))
        kinds.update(self._minis(game.get("minis"), document, item, users))
        targets, read = TrackTargets(document, holder, () if self._poser is None else self._poser.parts(holder)), {}
        nodes = [node for sequence in (motion.sequences if self._row[2] is None else self._row[2])
                 for root in sequence.clip.roots for node in root.children]
        present = {node.name for node in nodes}
        scene = _TrackScene(game, document, item, holder, held, registry, targets, users, kinds, present, nodes)
        for kind, rule in game["classes"].items():
            if (entry := self._class_read(kind, rule, scene)) is not None:
                read[kind] = entry
        masks = None
        if (filters := game.get("filters")) is not None and filters["component"] in held:
            masks = [page[filters["default"]] for page in plain_fields(document, held[filters["component"]])[
                filters["pages"]]]
            for kind, bits in filters["start"].items():
                for page, bit in bits if kind in kinds else ():
                    masks[page] |= 1 << bit
        return TrackPlayer(motion, read, targets, game["reset"], masks, self._row[2]) if read else None

    def _class_read(self, kind: str, rule: dict, scene: _TrackScene) -> dict | None:
        game, document, item, holder, held, registry, targets, users, kinds, present, nodes = scene
        if "consumers" in rule and not kinds.intersection(rule["consumers"]) or "runtime" in rule and (
                kind not in present or not all(targets.holding(name) for name in rule["runtime"]["below"])):
            return None
        if (screen := rule.get("screen")) is not None:
            return self._screen_read(rule, screen, targets, users)
        elif (face := rule.get("sequence")) is not None:
            return self._sequence_read(rule, face, document, item, kinds, targets)
        elif "request" in rule or "set" in rule:
            if kind in present and (found := self._runtime_driver(rule, game["configuration"], document, item,
                                                                   users)) is not None:
                return found
        elif "rate" in rule:
            if kind in present and (groups := self._rate_groups(rule["rate"], game["configuration"], document,
                                                                item, users)):
                return rule | {"groups": groups}
        elif "ratio" in rule:
            if (found := self._ratio_rows(rule["ratio"], document, item)) is not None:
                return rule | found
        elif (condition := rule.get("condition")) is not None:
            if (found := self._condition_objects(condition, kinds, document, targets, users, condition_pairs(
                    condition, [node for node in nodes if node.name == kind]))) is not None:
                return rule | found
        elif (lights := rule.get("lights")) is not None:
            named = {item.name: item.id for item in targets.children.get(holder, ())}
            return rule | {"targets": [named[name] for name in lights["objects"] if name in named]}
        elif (floats := rule.get("floats")) is not None:
            if (component := held.get(floats["component"])) is not None and targets.referenced(getattr(
                    component.fields.get(floats["ref"][0]), "raw_bytes", None), component.object_table_id,
                    floats["ref"][1]) is not None:
                return rule
        elif (face := rule.get("emotion")) is not None:
            if (found := held.get(face["controller"])) is not None and not plain_fields(document, found).get(
                    face["ignore"], 0) >> self._played_layer(holder) & 1:
                return rule | self._faces(face, rule, document, found, targets, users, self.prefab.game_version)
        elif (hair := rule.get("hair")) is not None:
            path = (component := held.get(hair["buffer"]["component"])) and plain_fields(document, component).get(
                hair["buffer"]["field"])
            return rule | {"buffer": resource_key(path) if path else None}
        elif "shapes" in rule or "normals" in rule:
            if (found := self._muscle(game.get("muscle"), targets, users)) is not None:
                return rule | found
        elif "blink" in rule:
            if kind not in present or self._poser is None:
                return None
            if self._packages is None:
                self._packages = self._enemy_packages(game["blink"], users)
            if (eyes := self._eyes(game["blink"], self._packages, document, held, users, partial(
                    self._poser.motion, (holder,)))) is not None:
                return rule | {"eyes": eyes}
        elif "lip" in rule:
            if (found := targets.holding(game["lip"]["controller"], "Enabled")) is not None:
                return rule | {"lips": game["lip"] | {"target": found[0]}}
        elif (weapon := rule.get("weapon")) is not None:
            return rule | {"off": weapon["off"].get(self._catalog_key(weapon, document, users), [])}
        elif "driver" not in rule:
            return rule
        elif (component := next((held[name] for name in held if name == rule["driver"]["component"] or (
                registry is not None and rule["driver"]["component"] in registry.getTypeParents(name))), None)) \
                and (found := driven(rule, plain_fields(document, component), partial(_user_root, users))) \
                is not None:
            return self._driver_read(rule, found, component, document, targets)
        return None

    def _screen_read(self, rule: dict, screen: dict, targets: TrackTargets, users) -> dict | None:
        if (found := targets.holding(screen["behavior"], "Enabled")) is None:
            return None
        fields, (listed, texture, *sprite) = plain_fields(found[2], found[1]), screen["sprites"]
        data = (rsz := fields.get(screen["data"]) and users(fields[screen["data"]])) and root_fields(rsz) or {}
        return rule | {"target": found[0], "start": (fields[screen["monitor"]["field"]], fields[
            screen["parts"]["field"]]), "sprites": {item[sprite[0]][sprite[1]]: item[texture]
                                                    for item in data.get(listed) or () if item[sprite[0]]}}

    def _sequence_read(self, rule: dict, face: dict, document, item, kinds: set, targets: TrackTargets) -> dict | None:
        if (found := targets.holding(face["controller"], "Enabled")) is None:
            return None
        layers = (motion_component(document, item, self.prefab.game_version) or ((),))[0]
        at = self._layer if self._layer is not None else next(
            (at for at, layer in enumerate(layers) if layer.bank == self._played[1]), 0)
        order = next((rows for name, rows in face["layers"].items() if name in kinds), ())
        waits = bool(plain_fields(found[2], found[1]).get(face["wait"]))
        return rule | {"target": found[0], "wait": waits, "order": order.index(at) if at in order and (
            at >= len(layers) or layers[at].rate > 0) else None}

    def _driver_read(self, rule: dict, found: dict, component, document, targets: TrackTargets) -> dict | None:
        face, facial = rule["driver"].get("face"), rule["driver"].get("facial")
        if face:
            found["target"] = targets.referenced(getattr(component.fields.get(face), "raw_bytes", None))
        elif facial:
            if (target := (targets.holding(*facial) or (None,))[0]) is None or self._poser is None or not (
                    lists := self._poser.bank_lists((target,))):
                return None
            found |= {"target": target, "bank": lists[0][0], "motion_of": partial(self._poser.motion,
                                                                                  (target,))}
        if (mode := rule.get("mode")) is not None:
            variable, (getter, rows, name, field) = found["data"][mode["variable"]], mode["getter"]
            found |= {"variable": variable, "face": targets.find(None, {"child": mode["face"]}), "materials": [
                (item.id, row[name]) for item in targets.below() for key in item.components
                if (part := document.components.get(key)) is not None and part.type_name == getter
                for row in plain_fields(document, part).get(rows) or () if row[field] == variable]}
        return found

    def _character(self, configuration: dict, document, item, users) -> tuple[set, dict | None]:
        if item.id not in self._runtime_parts:
            read_user, registry = partial(_user_root, users), document.rsz_file.type_registry
            own = tuple((found.type_name, found.fields) for key in item.components
                        if (found := document.components.get(key)) is not None)
            if (kind := next((plain_value(value) for _type, fields in own
                              if (value := fields.get(configuration["kind"])) is not None), None)) is None:
                kind, own = next((shell[:2] for graph in self.scene.graphs if (shell := graph.shells.get(item.id))),
                                 (None, ()))
            self._runtime_parts[item.id] = {name for type_name, _fields in own for name in (
                type_name, *registry.getTypeParents(type_name))}, next((
                    read_user(entry[configuration["file"]]) for path in configuration["catalogs"]
                    for entry in (read_user(path) or {}).get(configuration["list"]) or ()
                    if kind and entry.get(configuration["key"]) == kind), None)
        return self._runtime_parts[item.id]

    @staticmethod
    def _faces(spec: dict, rule: dict, document, component, targets: TrackTargets, users, game: str) -> dict:
        fields, scn = plain_fields(document, component), document.rsz_file
        listed, head, prop = spec["targets"]
        faces = []
        for entry, setting in zip(getattr(component.fields.get(listed), "values", ()), fields.get(listed) or ()):
            target = targets.referenced(getattr(scn.parsed_elements[entry.value].get(head), "raw_bytes", None), (
                scn.object_table.index(entry.value) if entry.value in scn.object_table else None), prop) or \
                targets.holder
            count = len((motion_component(document, document.objects[target], game) or ((),))[0])
            faces.append((target, *((setting[layer] if 0 <= setting[layer] < count else -1, setting[bank])
                                    for layer, bank in spec["layers"])))
        (empty, cleared), (none, still) = spec["empty"], spec["none"]
        return {"faces": faces, "start": none if fields.get(still) else 0,
                "interp": _user_root(users, fields.get(spec["setting"])),
                "defaults": rule["defaults"] | ({spec["emotion"]: empty} if fields.get(cleared) else {})}

    @staticmethod
    def _ratio_rows(spec: dict, document, item) -> dict | None:
        parent = document.objects.get(document.object_by_local_id.get(item.parent_id))
        controller = None if parent is None else next((found for key in parent.components if (
            found := document.components.get(key)) is not None and found.type_name == spec["controller"]), None)
        if controller is None:
            return None
        children = {}
        for other in document.objects.values():
            if document.object_by_local_id.get(other.parent_id) == parent.id:
                children.setdefault(other.name, other.id)
        tags = {int(tag): parent.id if name is None else children.get(name) for tag, name in spec["tags"].items()}
        fields = plain_fields(document, controller)
        return None if tags.get(spec["reads"]) != item.id else {"start": float(fields[spec["start"]]), "rows": [
            (tags[row[spec["target"]]], row[spec["material"]], row[spec["variable"]])
            for row in fields.get(spec["rows"]) or () if tags.get(row[spec["target"]]) is not None]}

    @staticmethod
    def _uniques(spec: dict | None, document, users) -> set[str]:
        key = MotionPreviewTracks._catalog_key(spec or {}, document, users)
        return {spec["classes"][key]} if key in (spec or {}).get("classes", {}) else set()

    @staticmethod
    def _catalog_key(spec: dict, document, users) -> str | None:
        own = resource_key(document.source_path)
        for path in spec.get("catalogs", ()):
            for entry in (_user_root(users, path) or {}).get(spec["list"]) or ():
                file = entry.get(spec["file"])
                found = next((value for value in file.values() if isinstance(value, str) and value), "") \
                    if isinstance(file, dict) else file or ""
                if found and resource_key(found) == own:
                    return str(entry[spec["key"]])
        return None

    @staticmethod
    def _minis(spec: dict | None, document, item, users) -> set[str]:
        registry, found = document.rsz_file.type_registry, set()
        for key in item.components if spec and registry is not None else ():
            if (component := document.components.get(key)) is None or spec["holder"] not in (
                    component.type_name, *registry.getTypeParents(component.type_name)):
                continue
            rsz = (path := plain_fields(document, component).get(spec["field"])) and users(path)
            found.update(spec["classes"][name] for info in getattr(rsz, "instance_infos", ())
                         if (name := (registry.get_type_info(info.type_id) or {}).get("name")) in spec["classes"])
        return found

    @staticmethod
    def _muscle(spec: dict | None, targets: TrackTargets, users) -> dict | None:
        if spec is None or (found := targets.holding(spec["component"])) is None:
            return None
        target, component, document = found
        path = document.resources.get(component.id, {}).get(spec["asset"]) or plain_fields(document, component).get(
            spec["asset"])
        fields = (asset := path and users(path)) and root_fields(asset) or {}
        files = (parameter := fields.get(spec["parameter"]) and users(fields[spec["parameter"]])) and root_fields(
            parameter) or {}
        rows = {kind: ((data := files.get(spec[kind]["file"]) and users(files[spec[kind]["file"]])) and root_fields(
            data) or {}).get(spec[kind]["list"]) or () for kind in ("shapes", "normals")}
        channels, materials = {}, {}
        for row in rows["shapes"]:
            channels.setdefault(row[spec["shapes"]["id"]], []).append(murmur3_hash_utf16le(row[spec["shapes"]["name"]]))
        for row in rows["normals"]:
            materials.setdefault(row[spec["normals"]["id"]], []).append(int(row[spec["normals"]["material"]]))
        return {"target": target, "channels": channels, "materials": materials, "muscle": spec}

    @staticmethod
    def _enemy_packages(spec: dict, users) -> dict:
        packages, (field, path), (kind, named) = {}, spec["prefab"], spec["holder"]
        for catalog in spec["catalogs"]:
            for entry in (_user_root(users, catalog) or {}).get(spec["list"]) or ():
                found = typed_fields(rsz, kind) if (rsz := users(entry[field][path])) is not None else {}
                package = _user_root(users, found.get(named)) or {}
                if prefab := (package.get(spec["character"][0]) or {}).get(spec["character"][1]):
                    packages.setdefault(resource_key(prefab), package)
        return packages

    @staticmethod
    def _eyes(spec: dict, packages: dict, document, held: dict, users, motion_of) -> dict | None:
        param = packages.get(resource_key(document.source_path))
        for name in spec["param"]:
            param = _user_root(users, value) if isinstance(value := (param or {}).get(name), str) else value
        if not param:
            return None
        if param[spec["type"]] == spec["parts"][0]:
            return {"part": spec["parts"][1]}
        (shift, mask), rows = spec["set"], {"layer": spec["layer"]}
        for name, field in spec["motions"].items():
            bank, motion = param[field] >> shift, param[field] & mask
            if param[field] == spec["none"] or (played := motion_of(bank, motion)) is None:
                return None
            rows[name] = bank, motion, *MotionPreviewTracks._connected(spec["connect"], document, held, users, bank,
                                                                 motion), float(played.end_frame)
        return rows

    @staticmethod
    def _connected(spec: dict, document, held: dict, users, bank: int, motion: int) -> tuple:
        (kind, field), (listed, named), (rows, key, path), (data, ident) = (
            spec["holder"], spec["assets"], spec["banks"], spec["data"])
        pack = _user_root(users, plain_fields(document, held[kind]).get(field)) if kind in held else None
        for asset in (pack or {}).get(listed) or ():
            for row in (_user_root(users, asset.get(named)) or {}).get(rows) or ():
                connect = _user_root(users, row[path]) if row[key] == bank else None
                while connect is not None:
                    if (entry := next((item for item in connect.get(data) or () if item[ident] == motion), None)):
                        change = next((item for item in entry[spec["change"][0]]
                                       if item[spec["change"][1]] == spec["change"][2]), None)
                        frames, *rest = [change[name] for name in spec["fields"]] if change else spec["missing"]
                        return float(frames), *rest
                    connect = _user_root(users, connect.get(spec["inherit"]))
        frames, *rest = spec["missing"]
        return float(frames), *rest

    def _condition_objects(self, spec: dict, kinds: set, document, targets: TrackTargets, users,
                           pairs: set = frozenset()) -> dict | None:
        ids = {} if "pairs" in spec or "holder" in spec else next(
            (table for name, table in spec["ids"].items() if name in kinds), None)
        game = self.prefab.game_version
        if ids is None or not (rules := model_condition_rules(game)):
            return None
        opened = {}

        def info(path: str):
            if path not in opened:
                rsz = users(path) if path else None
                resolve = None if rsz is None else FileFields(
                    rsz, lambda held, value: opened_file(held, value, users, {}))
                opened[path] = None if resolve is None else (resolve(rsz.object_table[0]), resolve)
            return opened[path]
        objects = {key: [] for key in (*ids.values(), *pairs)}
        for item in targets.below():
            slotted = document.slotted.get(item.mesh_component)
            held = holder_infos([(found.type_name, plain_fields(document, found)) for key in item.components
                                 if (found := document.components.get(key)) is not None], rules,
                                () if slotted is None else slotted.infos)
            for key, listed in objects.items():
                (ident, holder), shown, values = key if isinstance(key, tuple) else (key, None), {}, []
                parts = held.get("parts", {})
                for read in filter(None, map(info, parts.values() if holder is None else [parts.get(holder)])):
                    shown.update(parts_shown(*read, rules["parts"], ident))
                materials = held.get("materials", {})
                for read in filter(None, map(info, (materials.values() if holder is None else [materials.get(holder)])
                                         if spec.get("materials", True) else ())):
                    values += condition_values(*read, rules["materials"], [ident], game)
                if shown or values:
                    listed.append((item.id, shown, tuple(values)))
        settings = spec.get("modes", {}).get("alpha", {}).get("settings")
        return {"ids": ids, "objects": objects, "forced": tuple(
            item.id for item in targets.below() if any(document.components[key].type_name == settings
                                                       for key in item.components if key in document.components))}

    def _track_materials(self, materials: dict, written: dict) -> None:
        drawn = rules = None
        for target, set_materials in written.items():
            materials[target] = {**materials.get(target, {})}
            surfaces = [(skinned.key, surface) for skinned in self._poser.skinned if skinned.object_id == target
                        for surface in self.scene.material_surfaces(skinned.key)]
            for material, variables in set_materials.items():
                for variable, value in variables.items():
                    for key, surface in [(None, None)] if isinstance(material, str) and isinstance(variable, str) \
                            else surfaces:
                        name = material if surface is None else surface.material_name
                        names = {variable} if isinstance(variable, str) else {
                            item.name for item in surface.parameters if murmur3_hash_utf16le(item.name) == variable} | {
                            item.texture_type for item in surface.textures
                            if murmur3_hash_utf16le(item.texture_type) == variable}
                        if surface is not None and (material is not None and material not in (
                                name, murmur3_hash_utf16le(name)) or not any(
                                item.name in names for item in surface.parameters) and not any(
                                item.texture_type in names for item in surface.textures)):
                            continue
                        for found in names:
                            set_value = value
                            if isinstance(value, Fade):
                                if drawn is None:
                                    drawn = {item.key: item for graph in self.scene.graphs
                                             for item in graph.renderables}
                                    rules = draw_rules_for(mmtr_profile_for_game(self.prefab.game_version))
                                base = materials[target].get(name, {}).get(found)
                                if base is None and surface is not None:
                                    shown = surface if rules is None or key not in drawn else rules.overridden(
                                        surface, MESH_COMPONENT, drawn[key].draw_state)
                                    base = next((item.values for item in shown.parameters if item.name == found), None)
                                if base is None:
                                    continue
                                set_value = value.of(base if len(base) != 1 else base[0]) \
                                    if isinstance(base, tuple) else value.of(base)
                            materials[target][name] = {**materials[target].get(name, {}), found: set_value}

    def _part_roots(self, holder) -> list[tuple[str, object, object]]:
        names = {link.resolved_document_id: link.name for graph in self.scene.graphs for link in graph.links}
        return [(names.get(parts.document_id), parts, other) for parts, other in self._poser.parts(holder)
                if other.parent_id not in parts.object_by_local_id]

    def _runtime_driver(self, rule: dict, configuration: dict, document, item, users) -> dict | None:
        read_user = partial(_user_root, users)
        types, settings = self._character(configuration, document, item, users)
        if settings is None or self._poser is None or rule["driver"].get("updater", "") not in {"", *types} or (
                found := driven(rule, settings, read_user)) is None:
            return None
        part = next((other.id for name, _parts, other in self._part_roots(item.id)
                     if name == found["data"].get(rule["driver"]["part"])), None)
        change = rule.get("request") or rule["set"]
        layers, at = () if part is None else self._poser.layers(part), change["layer"]
        return None if part is None else found | {"target": part, "start": (layers[at].bank, layers[at].motion)
                                                  if at < len(layers) else (0, change.get("none", -1))}

    def _rate_groups(self, rate: dict, configuration: dict, document, item, users) -> dict:
        read_user = partial(_user_root, users)
        if self._poser is None or rate["updater"] not in self._character(configuration, document, item, users)[0]:
            return {}
        found = {}
        for name, parts, other in self._part_roots(item.id):
            for key in other.components:
                fields = plain_fields(parts, held) if (held := parts.components.get(key)) is not None and \
                    held.type_name == rate["component"] else {}
                param = fields.get("Enabled") and (read_user(fields.get(rate["user"])) or {}).get(rate["param"])
                groups = {user[rate["name"]]: user[rate["params"]] for path in (param or {}).get(rate["groups"]) or ()
                          if (user := read_user(path))}
                if name and groups:
                    found[name] = other.id, groups
        return found
