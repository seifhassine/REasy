from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from utils.resource_file_utils import resolve_app_resource_data

from .scn_scene_graph import (
    ScnResolvedResource,
    ScnSceneDiagnostic,
    ScnSceneGraph,
    ScnSceneGraphBuilder,
    normalize_document_id,
    normalize_scene_path,
)


@dataclass(slots=True)
class ScnSceneSource:
    path: str
    handler: object
    label: str = ""


def scn_identity_keys(path: str) -> set[str]:
    value = str(path or "")
    normalized = normalize_scene_path(value).lower()
    keys = {normalized} if normalized else set()
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


class ScnSceneLoader:
    def __init__(self):
        self._source_by_document: dict[str, ScnSceneSource] = {}
        self._building_source: ScnSceneSource | None = None

    def source_for_document(self, document_id: str) -> ScnSceneSource | None:
        return self._source_by_document.get(document_id)

    def build_graphs(
        self,
        sources: list[ScnSceneSource],
        *,
        max_depth: int = 8,
        skip_document_ids: set[str] | None = None,
    ) -> list[ScnSceneGraph]:
        valid_sources = [source for source in sources if getattr(source.handler, "rsz_file", None) is not None]
        self._source_by_document.clear()
        if not valid_sources:
            return []

        first = valid_sources[0].handler
        builder = ScnSceneGraphBuilder(
            getattr(first, "type_registry", None),
            resource_resolver=self.resolve_resource,
            game_version=str(getattr(first, "game_version", "") or ""),
            max_depth=max_depth,
        )
        graphs: list[ScnSceneGraph] = []
        seen_document_ids = set(skip_document_ids or ())
        for source in valid_sources:
            handler = source.handler
            root_path = str(source.path or getattr(handler, "filepath", "") or getattr(handler.rsz_file, "filepath", "") or "")
            self._source_by_document.setdefault(normalize_document_id(root_path), source)
            self._building_source = source
            try:
                graph = builder.build(handler.rsz_file, root_path=root_path)
            finally:
                self._building_source = None
            self._suppress_seen_documents(graph, seen_document_ids)
            graphs.append(graph)
            for document_id in graph.documents:
                self._source_by_document.setdefault(document_id, source)
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
        normalized = normalize_scene_path(resource_path)
        if not normalized:
            return None

        source = self._source_by_document.get(parent_document_id or "") or self._building_source
        handler = source.handler if source is not None else None
        hit = resolve_app_resource_data(
            getattr(handler, "app", None),
            normalized,
            allow_selection_dialog=False,
        )
        if hit is not None:
            return ScnResolvedResource(path=hit[0], data=hit[1])

        direct = Path(normalized)
        if direct.is_file():
            return ScnResolvedResource(path=str(direct), data=direct.read_bytes())

        source_path = Path(str(parent_document_id or (source.path if source is not None else "") or getattr(handler, "filepath", "") or ""))
        if source_path.is_file():
            sibling = source_path.parent / direct.name
            if sibling.is_file():
                return ScnResolvedResource(path=str(sibling), data=sibling.read_bytes())
        return None
