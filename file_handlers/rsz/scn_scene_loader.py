from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from utils.resource_file_utils import get_path_prefix_for_game, resolve_app_resource_data, resolve_resource_data

from .scn_scene_graph import (
    ScnResolvedResource,
    ScnSceneDiagnostic,
    ScnSceneGraph,
    ScnSceneGraphBuilder,
    normalize_scene_path,
)
from .scn_document_store import ScnDocumentStore, scoped_document_path


@dataclass(slots=True)
class ScnSceneSource:
    path: str
    handler: object
    label: str = ""
    project_dir: str = ""
    game_version: str = ""
    origin: str = ""


def scn_identity_keys(path: str) -> set[str]:
    value = str(path or "")
    normalized = normalize_scene_path(value).lower()
    keys = {normalized} if normalized else set()
    if "|" not in normalized:
        for marker in ("natives/stm/", "natives/x64/"):
            index = normalized.find(marker)
            if index >= 0:
                keys.add(normalized[index:])
    try:
        candidate = Path(value)
        if candidate.is_absolute() or candidate.exists():
            keys.add(os.path.normcase(os.path.abspath(value)))
    except Exception:
        pass
    return keys


def scn_source_identity_keys(source: ScnSceneSource) -> set[str]:
    return scn_identity_keys(scoped_document_path(source.project_dir, source.path))


class ScnSceneLoader:
    def __init__(self, document_store: ScnDocumentStore | None = None):
        self.document_store = document_store or ScnDocumentStore()
        self._source_by_document: dict[str, ScnSceneSource] = {}
        self._source_by_graph: dict[int, ScnSceneSource] = {}
        self._building_source: ScnSceneSource | None = None

    def source_for_graph(self, graph: ScnSceneGraph) -> ScnSceneSource | None:
        return self._source_by_graph.get(id(graph))

    def build_graphs(
        self,
        sources: list[ScnSceneSource],
        *,
        max_depth: int = 8,
        skip_document_ids: set[str] | None = None,
    ) -> list[ScnSceneGraph]:
        valid_sources = [source for source in sources if getattr(source.handler, "rsz_file", None) is not None]
        self._source_by_document.clear()
        self._source_by_graph.clear()
        if not valid_sources:
            return []

        graphs: list[ScnSceneGraph] = []
        seen_document_ids = set(skip_document_ids or ())
        for source in valid_sources:
            handler = source.handler
            builder = ScnSceneGraphBuilder(
                getattr(handler, "type_registry", None),
                resource_resolver=self.resolve_resource,
                game_version=str(getattr(source, "game_version", "") or getattr(handler, "game_version", "") or ""),
                max_depth=max_depth,
            )
            root_path = scoped_document_path(source.project_dir, str(source.path or getattr(handler, "filepath", "") or getattr(handler.rsz_file, "filepath", "") or ""))
            document = self.document_store.attach_source(source, handler.rsz_file, handler)
            self._source_by_document[document.document_id] = source
            self._building_source = source
            try:
                graph = builder.build(document.rsz_file, root_path=root_path)
            finally:
                self._building_source = None
            self._source_by_graph[id(graph)] = source
            self._suppress_seen_documents(graph, seen_document_ids)
            graphs.append(graph)
            for document_id in graph.documents:
                self._source_by_document[document_id] = source
            seen_document_ids.update(graph.documents)
        return graphs

    @staticmethod
    def _suppress_seen_documents(graph: ScnSceneGraph, seen_document_ids: set[str]) -> None:
        duplicates = set(graph.documents) & seen_document_ids
        if not duplicates:
            return
        graph.renderables = [r for r in graph.renderables if r.source_object_id.document_id not in duplicates]
        for document_id in sorted(duplicates):
            document = graph.documents.get(document_id)
            graph.diagnostics.append(
                ScnSceneDiagnostic(
                    severity="warning",
                    code="duplicate_linked_scn",
                    message="Linked SCN is already present in this scene and was skipped.",
                    document_id=document_id,
                    path=getattr(document, "source_path", "") or document_id,
                )
            )

    @staticmethod
    def document_identity_keys(graphs: list[ScnSceneGraph]) -> set[str]:
        keys: set[str] = set()
        for graph in graphs:
            for document in graph.documents.values():
                keys.update(scn_identity_keys(document.source_path or document.document_id))
                keys.update(scn_identity_keys(document.document_id))
        return keys

    def resolve_resource(self, resource_path: str, parent_document_id: str | None = None) -> ScnResolvedResource | None:
        return self.resolve_resource_for_source(
            self._building_source or self._source_by_document.get(parent_document_id or ""),
            resource_path,
        )

    def resolve_resource_for_source(self, source: ScnSceneSource | None, resource_path: str) -> ScnResolvedResource | None:
        normalized = normalize_scene_path(resource_path)
        if not normalized:
            return None

        handler = source.handler if source is not None else None
        is_scn = ".scn" in normalized.lower() and source is not None
        context = self.resource_context_for_source(source)
        def found(path: str, data: bytes) -> ScnResolvedResource:
            if is_scn and context:
                path = self._project_resource_path(path, context[0], context[1], context[2])
            return self._linked_scn_resource(source, path, data) if is_scn else ScnResolvedResource(path=path, data=data)

        hit = resolve_resource_data(normalized, *context, allow_selection_dialog=False) if context else resolve_app_resource_data(getattr(handler, "app", None), normalized, allow_selection_dialog=False)
        if hit is not None:
            return found(hit[0], hit[1])

        direct = Path(normalized)
        if direct.is_file():
            return found(str(direct), direct.read_bytes())

        source_path = Path(str((source.path if source is not None else "") or getattr(handler, "filepath", "") or ""))
        if source_path.is_file():
            sibling = source_path.parent / direct.name
            if sibling.is_file():
                return found(str(sibling), sibling.read_bytes())
        return None

    def _linked_scn_resource(self, source: ScnSceneSource, path: str, data: bytes) -> ScnResolvedResource:
        handler = getattr(source, "handler", None)
        doc = self.document_store.load_linked(
            source,
            path,
            data,
            getattr(handler, "type_registry", None),
            str(getattr(source, "game_version", "") or getattr(handler, "game_version", "") or ""),
        )
        self._source_by_document[doc.document_id] = source
        return ScnResolvedResource(path=scoped_document_path(doc.project_dir, doc.resource_path), rsz_file=doc.rsz_file)

    @staticmethod
    def _project_resource_path(path: str, project_dir: str, unpacked_dir: str, path_prefix: str) -> str:
        for root in (project_dir, unpacked_dir):
            if not root:
                continue
            try:
                rel = os.path.relpath(path, root).replace("\\", "/")
                if not rel.startswith("../") and rel != "..":
                    return normalize_scene_path(rel)
            except (TypeError, ValueError):
                pass
        normalized = normalize_scene_path(path)
        prefix = path_prefix.strip("/")
        if Path(path).is_absolute() or normalized.lower().startswith(prefix.lower() + "/"):
            return normalized
        return normalize_scene_path(f"{prefix}/{normalized}")

    def resource_context_for_source(self, source: ScnSceneSource | None):
        handler = source.handler if source is not None else None
        app = getattr(handler, "app", None)
        proj = getattr(app, "proj_dock", None) if app is not None else None
        project_dir = str(getattr(source, "project_dir", "") or getattr(proj, "project_dir", "") or "")
        game = str(getattr(source, "game_version", "") or getattr(handler, "game_version", "") or getattr(app, "current_game", "") or "")
        if not project_dir or proj is None:
            return None
        unpacked_dir, reader = proj.ensure_project_pak_context(project_dir)
        return project_dir, unpacked_dir, get_path_prefix_for_game(game), reader
