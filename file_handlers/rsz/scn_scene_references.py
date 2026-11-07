"""See docs/internals/startup-draw-states.md, References."""
from __future__ import annotations

from collections import deque

lazy from .scene_startup import startup_draw_requests
lazy from .scn_scene_graph import ScnForeignRequest, ScnObjectId, ScnSceneGraph, _bool_field, renderable_visible
lazy from .spawned_parts import NOT_SPAWNED


class ScnSceneReferences:
    def _activation(self, graph: ScnSceneGraph) -> dict[str, tuple[str, int]]:
        children: dict[str, list[str]] = {}
        for instance in graph.document_instances.values():
            if instance.parent_instance_id is not None:
                children.setdefault(instance.parent_instance_id, []).append(instance.instance_id)
        found: dict[str, tuple[str, int]] = {}
        roots = [graph.root_instance_id]
        for root in roots:
            queue, index = deque([root]), 0
            while queue:
                node = queue.popleft()
                found[node] = (root, index)
                index += 1
                for child in children.get(node, ()):
                    instance = graph.document_instances[child]
                    link = graph.links[instance.source_link_index] if instance.source_link_index is not None else None
                    parent = graph.documents[graph.document_instances[node].document_id]
                    folder = None if link is None else parent.objects.get(link.source_folder_id)
                    if folder is not None and folder.kind == "folder" \
                            and _bool_field(folder.fields, self.fields.get("folder startup"), True):
                        queue.append(child)
                    else:
                        roots.append(child)
        return found

    def _apply_foreign_requests(self, graph: ScnSceneGraph) -> None:
        requesting = [instance for instance in graph.document_instances.values()
                      if graph.documents[instance.document_id].startup_foreign]
        if not requesting:
            return
        whole = (startup_draw_requests(self.game_version).get("references") or {}).get("async activate")
        order = self._activation(graph)
        depth: dict[str, int] = {}
        for root, _index in order.values():
            node, hops = root, 0
            while (parent := graph.document_instances[node].parent_instance_id) is not None:
                node, hops = order[parent][0], hops + 1
            depth[root] = hops
        holders = self._guid_holders(graph)

        def instantiated(target: str, source: str, phase: str) -> bool | None:
            (target_root, target_index), (source_root, source_index) = order[target], order[source]
            if target_root == source_root:
                return True if phase != "awake" or target_index < source_index else whole
            root = source_root
            while (parent := graph.document_instances[root].parent_instance_id) is not None:
                root = order[parent][0]
                if root == target_root:
                    return True
            return None

        layers: dict[tuple[str, ScnObjectId], dict[int, list[tuple]]] = {}
        started: set[tuple[str, ScnObjectId]] = set()
        for instance in requesting:
            for position, request in enumerate(graph.documents[instance.document_id].startup_foreign):
                known = [(target, object_id, instantiated(target, instance.instance_id, request.phase))
                         for target, object_id in holders.get(request.guid, ())]
                if any(state is None for *_target, state in known):
                    if request.curves:
                        started.update((target, node) for target, object_id, _state in known
                                       for node in self._reached(graph, target, object_id, request))
                    continue
                hits = [(target, object_id) for target, object_id, state in known if state]
                if not hits:
                    continue
                target, object_id = min(hits, key=lambda hit: (depth[order[hit[0]][0]], order[hit[0]][1]))
                for node in self._reached(graph, target, object_id, request):
                    layers.setdefault((target, node), {}).setdefault(request.layer, []).append(
                        (request.draw, request.phase, order[instance.instance_id][1], position))
                    if request.curves:
                        started.add((target, node))

        drawn: dict[str, dict[ScnObjectId, bool]] = {}
        for (instance_id, object_id), by_layer in layers.items():
            document = graph.documents[graph.document_instances[instance_id].document_id]
            own = document.startup_layers.get(object_id, {})
            merged = {layer: draw for layer, (draw, _phase) in own.items()}
            for layer, entries in by_layer.items():
                # Awake requests land in instantiation order, before any start; the order of starts is not known.
                awake = [entry for entry in entries if entry[1] == "awake"]
                if awake and own.get(layer, (None, "awake"))[1] == "awake":
                    merged[layer] = max(awake, key=lambda entry: entry[2:])[0]
                starts = {entry[0] for entry in entries if entry[1] == "start"}
                if len(starts) > 1 or starts and own.get(layer, (None, ""))[1] == "start" \
                        and own[layer][0] not in starts:
                    break
                if starts:
                    merged[layer] = next(iter(starts))
            else:
                if all(merged.values()) != document.objects[object_id].draw_self:
                    drawn.setdefault(instance_id, {})[object_id] = all(merged.values())

        graph.startup_drawn = drawn
        graph.startup_unwritten = {
            (instance_id, written) for instance_id, node in started
            for written in graph.documents[graph.document_instances[instance_id].document_id]
            .startup_curve_writers.get(node, ())}
        for renderable in [item for items in graph.drawn_lists for item in items]:
            overrides = drawn.get(renderable.document_instance_id)
            if overrides is None:
                continue
            document = graph.documents[renderable.source_object_id.document_id]
            source = document.objects.get(renderable.source_object_id)
            component = document.components.get(renderable.source_component_id)
            if source is not None and component is not None and self._chain_overridden(document, source, overrides):
                renderable.visible_by_default = graph.document_instances[renderable.document_instance_id].drawn and \
                    renderable_visible(self.fields, document, source, component, overrides)
        for renderable in graph.renderables if graph.startup_unwritten else ():
            if renderable.source_kind != "mesh" or (
                    renderable.document_instance_id, renderable.source_object_id) not in graph.startup_unwritten:
                continue
            document = graph.documents[renderable.source_object_id.document_id]
            component = document.components.get(renderable.source_component_id)
            if component is not None:
                renderable.draw_state = self._mesh_state(
                    document, renderable.source_object_id, component, document.startup_materials_base,
                    graph.spawned_meshes.get((renderable.document_instance_id, component.id), NOT_SPAWNED).writes)

    @staticmethod
    def _reached(graph: ScnSceneGraph, instance_id: str, object_id: ScnObjectId,
                 request: ScnForeignRequest) -> list[ScnObjectId]:
        document = graph.documents[graph.document_instances[instance_id].document_id]
        if request.unless_name and request.unless_name in document.objects[object_id].name:
            return []
        found = [object_id]
        if request.descendants:
            index = 0
            while index < len(found):
                parent_local = found[index].local_object_id
                found.extend(item.id for item in document.objects.values()
                             if item.parent_id == parent_local and item.id != found[index])
                index += 1
        return found
