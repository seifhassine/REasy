"""See docs/internals/startup-draw-states.md, Folder draw states."""
from __future__ import annotations

from collections.abc import Mapping

lazy import numpy as np

lazy from file_handlers.rsz.rsz_data_types import OBBData

lazy from .scene_startup import startup_draw_requests
lazy from .scn_scene_graph import (
    ScnCameraArea, ScnCameraRanges, ScnCameraSwitch, ScnObjectId, ScnRenderableMesh, ScnSceneDocument, ScnSceneGraph,
    ScnSceneObject, _bool_field, _numeric_field, _object_ref_id, _object_runs, _strip_value, _vector3, parent_of,
    placed_matrix, renderable_visible,
)


class ScnSceneFolders:
    def _collect_camera_switches(self, graph: ScnSceneGraph) -> None:
        rules = startup_draw_requests(self.game_version)
        spec = rules.get("folders") or {}
        cameras = {name: rule["camera"] for name, rule in spec.get("switches", {}).items() if "camera" in rule}
        areas = {name: rule["camera area"] for name, rule in spec.get("switches", {}).items() if "camera area" in rule}
        if not cameras and not areas:
            return
        holders = self._guid_holders(graph)
        linked: dict[str, list[str]] = {}
        for instance in graph.document_instances.values():
            if instance.parent_instance_id is not None:
                linked.setdefault(instance.parent_instance_id, []).append(instance.instance_id)
        by_instance: dict[str, list[ScnRenderableMesh]] = {}
        for renderable in graph.renderables:
            by_instance.setdefault(renderable.document_instance_id, []).append(renderable)
        for instance in graph.document_instances.values():
            document = graph.documents[instance.document_id]
            for item in document.objects.values():
                if not _object_runs(document, item.id, spec.get("when"), rules["options"]["update"]):
                    continue
                for component in map(document.components.__getitem__, item.components):
                    rule = cameras.get(component.type_name) or areas.get(component.type_name)
                    if rule is None or not _bool_field(component.fields, self.fields.get("enabled"), True):
                        continue
                    folders = []
                    for side in ("inside", "far"):
                        found = holders.get(
                            bytes(getattr(component.fields.get(rule.get(side)), "raw_bytes", b"") or b""), [])
                        here = [hit for hit in found if hit[0] == instance.instance_id] or found
                        held = None
                        if len(here) == 1:
                            target_instance, target = here[0]
                            target_document = graph.documents[graph.document_instances[target_instance].document_id]
                            if any(target_document.components[other].type_name == rule["container"]
                                   for other in target_document.objects[target].components):
                                held = self._object_folder(graph, target_instance, target)
                        folders.append(held)
                    if folders[0] is None:
                        continue
                    world = item.document_world_matrix if instance.parent_instance_id is None else placed_matrix(
                        instance.base_world_matrix, item.document_world_matrix)
                    objects = [frozenset() if held is None else self._folder_objects(graph, *held, linked)
                               for held in folders]
                    if component.type_name in areas:
                        held = self._area_boxes(document, item, rule, world)
                        if held is not None:
                            graph.camera_switches.append(ScnCameraArea(
                                held[0], float(rule["radius"]), held[1],
                                self._folder_keys(graph, *folders[0], by_instance, linked), folders[0],
                                inside_objects=objects[0]))
                        continue
                    axis = np.asarray(world, np.float64)[:3, 2]
                    axis = axis / (np.linalg.norm(axis) or 1.0)
                    graph.camera_switches.append(ScnCameraSwitch(
                        _vector3(component.fields.get(rule["center"])),
                        float(_numeric_field(component.fields, rule["show"], 0.0)),
                        float(_numeric_field(component.fields, rule["no show"], 0.0)),
                        _bool_field(component.fields, rule["frustum"], False),
                        _vector3(component.fields.get(rule["frustum center"])),
                        tuple(map(float, axis)),
                        self._folder_keys(graph, *folders[0], by_instance, linked),
                        frozenset() if folders[1] is None else self._folder_keys(graph, *folders[1], by_instance,
                                                                                 linked),
                        folders[0], folders[1], *objects))

    def _area_boxes(self, document: ScnSceneDocument, item: ScnSceneObject, rule: Mapping, world) -> tuple | None:
        scn, world = document.rsz_file, np.asarray(world, np.float64)
        boxes, others = [], False

        def shapes(instance: int):
            type_name = self._type_name(scn, instance)
            fields = self._fields(scn, instance)
            if type_name == rule["box"]:
                yield fields.get(rule["obb"])
            elif type_name == rule["compound"]:
                for entry in getattr(fields.get(rule["instances"]), "values", None) or ():
                    yield from shapes(_object_ref_id(
                        self._fields(scn, _object_ref_id(entry)).get(rule["instance shape"])))
            else:
                yield None

        for component in map(document.components.__getitem__, item.components):
            if component.type_name != rule["colliders"] \
                    or not _bool_field(component.fields, self.fields.get("enabled"), True):
                continue
            for entry in getattr(component.fields.get(rule["list"]), "values", None) or ():
                collider = self._fields(scn, _object_ref_id(entry))
                if not _bool_field(collider, self.fields.get("enabled"), True):
                    continue
                if _strip_value(getattr(collider.get(rule["filter"]), "value", "")) not in rule["hits"]:
                    others = True
                    continue
                for obb in shapes(_object_ref_id(collider.get(rule["shape"]))):
                    if not isinstance(obb, OBBData):
                        return None
                    raw = np.asarray(obb.values, np.float64).reshape(5, 4)
                    axes = (world[:3, :3] @ raw[:3, :3].T).T
                    centre = world[:3, :3] @ raw[3, :3] + world[:3, 3]
                    boxes.append((tuple(tuple(map(float, row)) for row in axes), tuple(map(float, centre)),
                                  tuple(map(float, raw[4, :3]))))
        return tuple(boxes), others

    def _collect_camera_ranges(self, graph: ScnSceneGraph) -> None:
        rules = startup_draw_requests(self.game_version)
        spec = rules.get("camera ranges")
        if not spec:
            return
        types = {name: rule for name, rule in spec["types"].items() if "gap" not in rule}
        positions, initially, keys = [], [], []
        holders: dict[str, dict[ScnObjectId, int]] = {}
        for instance in graph.document_instances.values():
            document = graph.documents[instance.document_id]
            for item in document.objects.values():
                if item.kind == "folder" or not item.draw_self or not _object_runs(
                        document, item.id, spec["when"], rules["options"]["update"]):
                    continue
                held = {document.components[component_id].type_name: document.components[component_id]
                        for component_id in item.components if component_id in document.components}
                found = next(((component, rule) for name, rule in types.items()
                              if (component := held.get(name)) is not None and rule.get("unless") not in held), None)
                if found is None or not _bool_field(found[0].fields, self.fields.get("enabled"), True):
                    continue
                world = item.document_world_matrix if instance.parent_instance_id is None else placed_matrix(
                    instance.base_world_matrix, item.document_world_matrix)
                holders.setdefault(instance.instance_id, {})[item.id] = len(positions)
                positions.append(np.asarray(world, np.float64)[:3, 3])
                initially.append(_bool_field(found[0].fields, found[1]["flag"], True))
                keys.append([])
        if not positions:
            return
        for renderable in graph.renderables:
            held = holders.get(renderable.document_instance_id)
            if not held:
                continue
            document = graph.documents[graph.document_instances[renderable.document_instance_id].document_id]
            node, seen = document.objects.get(renderable.source_object_id), set()
            while node is not None and node.id not in seen:
                seen.add(node.id)
                if (index := held.get(node.id)) is not None:
                    keys[index].append(renderable.key)
                parent = document.object_by_local_id.get(node.parent_id)
                node = None if parent is None else document.objects.get(parent)
        graph.camera_ranges = ScnCameraRanges(
            np.asarray(positions, np.float32), np.asarray(initially, bool), tuple(map(frozenset, keys)),
            float(spec["far"]), float(spec["near"]), float(spec["cone"]), float(spec["turn"]))

    @staticmethod
    def _object_folder(graph: ScnSceneGraph, instance_id: str,
                       object_id: ScnObjectId) -> tuple[str, ScnObjectId] | None:
        document = graph.documents[graph.document_instances[instance_id].document_id]
        node, seen = document.object_by_local_id.get(document.objects[object_id].parent_id), {object_id}
        while node is not None and node not in seen:
            if document.objects[node].kind == "folder":
                return instance_id, node
            seen.add(node)
            node = document.object_by_local_id.get(document.objects[node].parent_id)
        instance = graph.document_instances[instance_id]
        if instance.parent_instance_id is None or instance.source_link_index is None:
            return None
        return instance.parent_instance_id, graph.links[instance.source_link_index].source_folder_id

    def _folder_keys(self, graph: ScnSceneGraph, instance_id: str, folder_id: ScnObjectId, by_instance: Mapping,
                     linked: Mapping) -> frozenset[str]:
        document = graph.documents[graph.document_instances[instance_id].document_id]
        built, forced = document.objects[folder_id].draw_self, {folder_id: True}
        keys = []

        def take(renderable, owner: ScnSceneDocument, drawn: bool, overrides) -> None:
            source = owner.objects.get(renderable.source_object_id)
            component = owner.components.get(renderable.source_component_id)
            if source is not None and component is not None and (renderable.visible_by_default if built else (
                    drawn and renderable_visible(self.fields, owner, source, component, overrides))):
                keys.append(renderable.key)

        def link_folder(child: str, owner: ScnSceneDocument) -> ScnSceneObject | None:
            instance = graph.document_instances[child]
            return None if instance.source_link_index is None else owner.objects.get(
                graph.links[instance.source_link_index].source_folder_id)

        drawn = graph.document_instances[instance_id].drawn
        for renderable in by_instance.get(instance_id, ()):
            source = document.objects.get(renderable.source_object_id)
            if source is not None and self._chain_overridden(document, source, forced):
                take(renderable, document, drawn, forced)
        stack = [(child, drawn and self._chain_drawn(document, folder, forced)) for child in linked.get(instance_id, ())
                 if (folder := link_folder(child, document)) is not None
                 and self._chain_overridden(document, folder, forced)]
        while stack:
            child, above = stack.pop()
            owner = graph.documents[graph.document_instances[child].document_id]
            for renderable in by_instance.get(child, ()):
                take(renderable, owner, above, None)
            stack.extend((grandchild, above and self._chain_drawn(owner, folder))
                         for grandchild in linked.get(child, ())
                         if (folder := link_folder(grandchild, owner)) is not None)
        return frozenset(keys)

    @classmethod
    def _folder_objects(cls, graph: ScnSceneGraph, instance_id: str, folder_id: ScnObjectId,
                        linked: Mapping) -> frozenset[ScnObjectId]:
        document = graph.documents[graph.document_instances[instance_id].document_id]
        under = {folder_id: True}
        found = [item.id for item in document.objects.values() if cls._chain_overridden(document, item, under)]
        stack = []
        for child in linked.get(instance_id, ()):
            instance = graph.document_instances[child]
            folder = None if instance.source_link_index is None else document.objects.get(
                graph.links[instance.source_link_index].source_folder_id)
            if folder is not None and cls._chain_overridden(document, folder, under):
                stack.append(child)
        while stack:
            child = stack.pop()
            found.extend(graph.documents[graph.document_instances[child].document_id].objects)
            stack.extend(linked.get(child, ()))
        return frozenset(found)

    @staticmethod
    def _chain_overridden(document: ScnSceneDocument, source: ScnSceneObject, overrides: Mapping) -> bool:
        current, seen = source, set()
        while current is not None and current.id not in seen:
            if current.id in overrides:
                return True
            seen.add(current.id)
            parent = document.object_by_local_id.get(current.parent_id)
            current = None if parent is None else document.objects.get(parent)
        return False

    @staticmethod
    def _chain_drawn(
        document: ScnSceneDocument,
        source_object: ScnSceneObject,
        drawn: Mapping[ScnObjectId, bool] | None = None,
    ) -> bool:
        current = source_object
        visited: set[ScnObjectId] = set()
        while current.id not in visited:
            visited.add(current.id)
            if not (drawn.get(current.id, current.draw_self) if drawn else current.draw_self):
                return False
            if (parent := parent_of(document, current)) is None:
                break
            current = parent
        return True
