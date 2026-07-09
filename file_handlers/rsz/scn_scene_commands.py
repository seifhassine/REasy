from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .scn_document_store import ScnDocumentStore
from .scn_scene_adapters import CompositeMeshAdapter, TransformAdapter, decompose_trs
from .scn_scene_graph import ScnLightProbeBinding, ScnObjectId, ScnRenderableMesh, ScnSceneGraph, ScnTransform


def _identity() -> np.ndarray:
    return np.identity(4, dtype=np.float32)


@dataclass(slots=True)
class TransformEditResult:
    matrices: dict[str, np.ndarray] = field(default_factory=dict)
    dirty_documents: set[str] = field(default_factory=set)
    changed_fields: list[object] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    edits: list["TransformEditRecord"] = field(default_factory=list)
    handled: bool = False


@dataclass(slots=True)
class TransformEditRecord:
    graph: ScnSceneGraph
    key: str
    source_object_id: ScnObjectId
    source_transform_instance_id: int | None
    changed_fields: tuple[object, ...]
    old_transform: ScnTransform
    new_transform: ScnTransform


class TransformSelectionCommand:
    def __init__(self, graphs: list[ScnSceneGraph], store: ScnDocumentStore, matrices: dict[str, np.ndarray]):
        self.graphs = graphs
        self.store = store
        self.matrices = {key: np.asarray(value, dtype=np.float32) for key, value in matrices.items()}

    def execute(self) -> TransformEditResult:
        result = TransformEditResult()
        for graph in self.graphs:
            by_key = {renderable.key: renderable for renderable in graph.renderables}
            probes_by_key = {binding.key: binding for binding in graph.light_probes}
            changed_objects: set[ScnObjectId] = set()
            dirty_documents: set[str] = set()
            object_targets: dict[ScnObjectId, tuple[ScnRenderableMesh, np.ndarray]] = {}
            composite_targets: list[tuple[ScnRenderableMesh, np.ndarray]] = []
            light_probe_targets: list[tuple[ScnLightProbeBinding, np.ndarray]] = []
            for key, matrix in self.matrices.items():
                renderable = by_key.get(key)
                if renderable is None:
                    binding = probes_by_key.get(key)
                    if binding is not None:
                        result.handled = True
                        light_probe_targets.append((binding, matrix))
                    continue
                result.handled = True
                if renderable.source_kind == "foliage":
                    result.skipped[key] = "FOL instance transforms are read-only"
                elif renderable.source_kind == "composite_mesh":
                    composite_targets.append((renderable, matrix))
                else:
                    try:
                        instance = graph.document_instances[renderable.document_instance_id]
                        object_targets.setdefault(renderable.source_object_id, (renderable, self._inverse(instance.base_world_matrix) @ matrix))
                    except Exception as exc:
                        result.skipped[key] = str(exc)
            written_worlds: dict[ScnObjectId, np.ndarray] = {}
            for object_id, (renderable, document_world) in sorted(object_targets.items(), key=lambda item: self._object_depth(graph, item[0])):
                try:
                    edit = self._write_object(graph, renderable, document_world, written_worlds)
                    result.edits.append(edit)
                    result.changed_fields.extend(edit.changed_fields)
                    changed_objects.add(object_id)
                    dirty_documents.add(object_id.document_id)
                except Exception as exc:
                    result.skipped[renderable.key] = str(exc)
            for renderable, matrix in composite_targets:
                try:
                    instance = graph.document_instances[renderable.document_instance_id]
                    edit = self._write_composite(graph, renderable, self._inverse(instance.base_world_matrix) @ matrix, written_worlds)
                    result.edits.append(edit)
                    result.changed_fields.extend(edit.changed_fields)
                    result.matrices[renderable.key] = renderable.world_matrix
                    dirty_documents.add(renderable.source_object_id.document_id)
                except Exception as exc:
                    result.skipped[renderable.key] = str(exc)
            for binding, _matrix in light_probe_targets:
                try:
                    changed = self._write_light_probe_obb(binding)
                    if changed:
                        result.changed_fields.extend(changed)
                        dirty_documents.add(binding.source_object_id.document_id)
                except Exception as exc:
                    result.skipped[binding.key] = str(exc)
            if changed_objects:
                result.matrices.update(sync_graph_transforms(graph, object_ids=changed_objects))
            result.dirty_documents.update(dirty_documents)
        for document_id in result.dirty_documents:
            self.store.mark_dirty(document_id)
        return result

    def undo(self, result: TransformEditResult) -> TransformEditResult:
        return self._apply_records(result.edits, old=True)

    def redo(self, result: TransformEditResult) -> TransformEditResult:
        return self._apply_records(result.edits, old=False)

    def _write_object(self, graph: ScnSceneGraph, renderable: ScnRenderableMesh, document_world: np.ndarray, written_worlds: dict[ScnObjectId, np.ndarray]) -> TransformEditRecord:
        document = graph.documents[renderable.source_object_id.document_id]
        scene_object = document.objects[renderable.source_object_id]
        if scene_object.transform_component is None:
            raise ValueError("Object has no editable via.Transform")
        fields = document.components[scene_object.transform_component].fields
        adapter = TransformAdapter(fields)
        old_transform = adapter.read()
        parent_matrix = _identity()
        if parent_id := document.object_by_local_id.get(scene_object.parent_id):
            parent_matrix = written_worlds.get(parent_id, document.objects[parent_id].document_world_matrix)
        new_transform = decompose_trs(self._inverse(parent_matrix) @ document_world, old_transform)
        adapter.write(new_transform)
        written_worlds[renderable.source_object_id] = document_world.astype(np.float32)
        return TransformEditRecord(graph, renderable.key, renderable.source_object_id, None, adapter.transform_fields(), old_transform, new_transform)

    def _write_composite(self, graph: ScnSceneGraph, renderable: ScnRenderableMesh, document_world: np.ndarray, written_worlds: dict[ScnObjectId, np.ndarray] | None = None) -> TransformEditRecord:
        if not renderable.source_transform_instance_id:
            raise ValueError("Composite transform is embedded and has no editable instance id")
        document = graph.documents[renderable.source_object_id.document_id]
        fields = getattr(document.rsz_file, "parsed_elements", {}).get(renderable.source_transform_instance_id)
        if not fields:
            raise ValueError("Composite transform fields are missing")
        adapter = CompositeMeshAdapter(fields)
        old_transform = adapter.read_transform()
        instance = graph.document_instances[renderable.document_instance_id]
        owner_matrix = (written_worlds or {}).get(renderable.source_object_id, document.objects[renderable.source_object_id].document_world_matrix)
        new_transform = decompose_trs(self._inverse(owner_matrix) @ document_world, old_transform)
        adapter.write_transform(new_transform)
        renderable.document_world_matrix = (owner_matrix @ new_transform.local_matrix).astype(np.float32)
        renderable.world_matrix = (instance.base_world_matrix @ renderable.document_world_matrix).astype(np.float32)
        return TransformEditRecord(graph, renderable.key, renderable.source_object_id, renderable.source_transform_instance_id, adapter.transform_fields(), old_transform, new_transform)

    @staticmethod
    def _write_light_probe_obb(binding: ScnLightProbeBinding) -> tuple[object, ...]:
        changed = []
        for box in binding.obbs:
            if box.is_default_unit_box():
                continue
            box.write_field()
            changed.append(box.field_data)
        return tuple(changed)

    def _apply_records(self, records: list[TransformEditRecord], *, old: bool) -> TransformEditResult:
        result = TransformEditResult()
        changed: dict[int, tuple[ScnSceneGraph, set[ScnObjectId]]] = {}
        for record in records:
            try:
                self._write_record(record, record.old_transform if old else record.new_transform)
                changed.setdefault(id(record.graph), (record.graph, set()))[1].add(record.source_object_id)
                result.dirty_documents.add(record.source_object_id.document_id)
                result.changed_fields.extend(record.changed_fields)
                result.edits.append(record)
            except Exception as exc:
                result.skipped[record.key] = str(exc)
        for graph, object_ids in changed.values():
            result.matrices.update(sync_graph_transforms(graph, object_ids=object_ids))
        for document_id in result.dirty_documents:
            self.store.mark_dirty(document_id)
        return result

    @staticmethod
    def _write_record(record: TransformEditRecord, transform: ScnTransform) -> None:
        document = record.graph.documents[record.source_object_id.document_id]
        if record.source_transform_instance_id is not None:
            fields = getattr(document.rsz_file, "parsed_elements", {}).get(record.source_transform_instance_id)
            if not fields:
                raise ValueError("Composite transform fields are missing")
            CompositeMeshAdapter(fields).write_transform(transform)
            return
        scene_object = document.objects[record.source_object_id]
        if scene_object.transform_component is None:
            raise ValueError("Object has no editable via.Transform")
        TransformAdapter(document.components[scene_object.transform_component].fields).write(transform)

    @staticmethod
    def _inverse(matrix: np.ndarray) -> np.ndarray:
        return np.linalg.inv(np.asarray(matrix, dtype=np.float32)).astype(np.float32)

    @staticmethod
    def _object_depth(graph: ScnSceneGraph, object_id: ScnObjectId) -> int:
        document = graph.documents.get(object_id.document_id)
        scene_object = document.objects.get(object_id) if document else None
        depth, seen = 0, set()
        while document and scene_object and scene_object.id not in seen:
            seen.add(scene_object.id)
            parent_id = document.object_by_local_id.get(scene_object.parent_id)
            scene_object = document.objects.get(parent_id) if parent_id else None
            depth += 1
        return depth


class RawTransformFieldCommand:
    def __init__(self, graphs: list[ScnSceneGraph], store: ScnDocumentStore, document_ids: set[str], changed_field: object):
        self.graphs = graphs
        self.store = store
        self.document_ids = set(document_ids)
        self.changed_field = changed_field

    def execute(self) -> TransformEditResult:
        result = TransformEditResult()
        if self.changed_field is None:
            return result
        result.changed_fields.append(self.changed_field)
        for graph in self.graphs:
            object_ids = self._changed_objects(graph, result)
            if not object_ids:
                continue
            result.handled = True
            try:
                result.matrices.update(sync_graph_transforms(graph, object_ids=object_ids))
                result.dirty_documents.update(object_id.document_id for object_id in object_ids)
            except Exception as exc:
                result.skipped[f"{graph.root_instance_id}:raw_transform"] = str(exc)
        for document_id in result.dirty_documents:
            self.store.mark_dirty(document_id)
        return result

    def _changed_objects(self, graph: ScnSceneGraph, result: TransformEditResult) -> set[ScnObjectId]:
        object_ids: set[ScnObjectId] = set()
        for document_id in self.document_ids:
            document = graph.documents.get(document_id)
            if document is None:
                continue
            for component in document.components.values():
                if component.type_name != "via.Transform":
                    continue
                try:
                    adapter = TransformAdapter(component.fields)
                    if not adapter.owns_field(self.changed_field):
                        continue
                    if component.owner is not None:
                        object_ids.add(component.owner)
                except Exception as exc:
                    result.skipped[str(component.id.instance_id)] = str(exc)
            parsed = getattr(document.rsz_file, "parsed_elements", {})
            for renderable in graph.renderables:
                if renderable.source_kind != "composite_mesh" or renderable.source_object_id.document_id != document_id or not renderable.source_transform_instance_id:
                    continue
                fields = parsed.get(renderable.source_transform_instance_id)
                if not fields:
                    continue
                try:
                    adapter = CompositeMeshAdapter(fields)
                    if adapter.owns_transform_field(self.changed_field):
                        object_ids.add(renderable.source_object_id)
                except Exception as exc:
                    result.skipped[renderable.key] = str(exc)
        return object_ids


def sync_graph_transforms(
    graph: ScnSceneGraph,
    document_ids: set[str] | None = None,
    object_ids: set[ScnObjectId] | None = None,
) -> dict[str, np.ndarray]:
    document_ids = document_ids or ({object_id.document_id for object_id in object_ids} if object_ids else None)
    old_locals = _renderable_locals(graph)
    for document_id, document in graph.documents.items():
        if document_ids is None or document_id in document_ids:
            for scene_object in document.objects.values():
                if scene_object.transform_component is not None:
                    component = document.components[scene_object.transform_component]
                    scene_object.transform = TransformAdapter(component.fields).read()
            _compute_worlds(document)
    _refresh_instance_bases(graph)
    return _refresh_renderables(graph, old_locals, _affected_renderable_keys(graph, object_ids) if object_ids else None)


def _compute_worlds(document) -> None:
    visiting, visited = set(), set()

    def compute(scene_object):
        if scene_object.id in visited or scene_object.id in visiting:
            return scene_object.document_world_matrix
        visiting.add(scene_object.id)
        parent = document.object_by_local_id.get(scene_object.parent_id)
        parent_matrix = compute(document.objects[parent]) if parent in document.objects else _identity()
        local = scene_object.transform.local_matrix if scene_object.transform else _identity()
        scene_object.document_world_matrix = (parent_matrix @ local).astype(np.float32)
        visiting.discard(scene_object.id)
        visited.add(scene_object.id)
        return scene_object.document_world_matrix

    for scene_object in document.objects.values():
        compute(scene_object)


def _refresh_instance_bases(graph: ScnSceneGraph) -> None:
    for instance in graph.document_instances.values():
        if instance.parent_instance_id is None or instance.source_link_index is None:
            continue
        link = graph.links[instance.source_link_index]
        parent = graph.document_instances.get(instance.parent_instance_id)
        source_doc = graph.documents.get(link.source_folder_id.document_id)
        source_folder = source_doc.objects.get(link.source_folder_id) if source_doc else None
        if parent is None or source_folder is None:
            continue
        offset = _identity()
        if link.folder_offset is not None:
            offset[:3, 3] = np.asarray(link.folder_offset, dtype=np.float32)
        instance.base_world_matrix = (parent.base_world_matrix @ source_folder.document_world_matrix @ offset).astype(np.float32)


def _refresh_renderables(graph: ScnSceneGraph, old_locals: dict[str, np.ndarray], keys: set[str] | None = None) -> dict[str, np.ndarray]:
    matrices = {}
    for renderable in graph.renderables:
        if keys is not None and renderable.key not in keys:
            continue
        document = graph.documents.get(renderable.source_object_id.document_id)
        instance = graph.document_instances.get(renderable.document_instance_id)
        scene_object = document.objects.get(renderable.source_object_id) if document else None
        if instance is None or scene_object is None:
            continue
        local = _renderable_local(document, renderable, old_locals)
        renderable.document_world_matrix = (scene_object.document_world_matrix @ local).astype(np.float32)
        renderable.world_matrix = (instance.base_world_matrix @ renderable.document_world_matrix).astype(np.float32)
        matrices[renderable.key] = renderable.world_matrix
    return matrices


def _affected_renderable_keys(graph: ScnSceneGraph, object_ids: set[ScnObjectId]) -> set[str]:
    objects, instances = _object_subtree_ids(graph, object_ids), set()
    changed = True
    while changed:
        changed = False
        for instance in graph.document_instances.values():
            link = graph.links[instance.source_link_index] if instance.source_link_index is not None else None
            if instance.instance_id not in instances and (instance.parent_instance_id in instances or (link and link.source_folder_id in objects)):
                instances.add(instance.instance_id)
                changed = True
    return {renderable.key for renderable in graph.renderables if renderable.source_object_id in objects or renderable.document_instance_id in instances}


def _object_subtree_ids(graph: ScnSceneGraph, object_ids: set[ScnObjectId]) -> set[ScnObjectId]:
    affected = set(object_ids)
    changed = True
    while changed:
        changed = False
        for document in graph.documents.values():
            parent_ids = {object_id.local_object_id for object_id in affected if object_id.document_id == document.document_id}
            for scene_object in document.objects.values():
                if scene_object.id not in affected and scene_object.parent_id in parent_ids:
                    affected.add(scene_object.id)
                    changed = True
    return affected


def _renderable_locals(graph: ScnSceneGraph) -> dict[str, np.ndarray]:
    return {
        renderable.key: np.linalg.inv(scene_object.document_world_matrix) @ renderable.document_world_matrix
        for renderable in graph.renderables
        if (document := graph.documents.get(renderable.source_object_id.document_id))
        and (scene_object := document.objects.get(renderable.source_object_id))
        and renderable.source_kind != "mesh"
    }


def _renderable_local(document, renderable: ScnRenderableMesh, old_locals: dict[str, np.ndarray]) -> np.ndarray:
    if renderable.source_kind == "mesh":
        return _identity()
    if renderable.source_kind == "composite_mesh" and renderable.source_transform_instance_id:
        fields = getattr(document.rsz_file, "parsed_elements", {}).get(renderable.source_transform_instance_id)
        if fields:
            return CompositeMeshAdapter(fields).read_transform().local_matrix
    return old_locals.get(renderable.key, _identity())
