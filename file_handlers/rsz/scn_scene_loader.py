from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

lazy from utils.resource_file_utils import (
    ResourceResolutionContext,
    resource_context_for_app,
    resource_context_for_handler,
)

from .scn_scene_graph import (
    ScnResolvedResource,
    ScnSceneDiagnostic,
    ScnSceneGraph,
    ScnSceneGraphBuilder,
    normalize_scene_path,
)
lazy from .scene_startup import startup_draw_requests
lazy from file_handlers.rsz.rsz_file import RszFile
lazy from .scn_document_store import ScnDocument, ScnDocumentStore, scoped_document_path
lazy from .campaign_state import CampaignChoice, CampaignChoices, choice_names
lazy from .spawn_rules import spawner_file
lazy from .spawned_parts import repicked


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


def holds_scene(rsz_file) -> bool:
    return bool(getattr(rsz_file, "is_scn", False) or getattr(rsz_file, "is_pfb", False)) or spawner_file(rsz_file)


def scn_source_identity_keys(source: ScnSceneSource) -> set[str]:
    return scn_identity_keys(scoped_document_path(source.project_dir, source.path))


def scene_source_file(source: ScnSceneSource | None) -> str:
    """The file a source was opened from; its resources are looked for beside it too."""
    handler = source.handler if source is not None else None
    return str((source.path if source is not None else "") or getattr(handler, "filepath", "") or "")


def read_scene_resource(context: ResourceResolutionContext | None, normalized: str,
                        source_file: str) -> tuple[str, bytes] | None:
    """A normalized resource path's file and bytes: in the source's context, as a path, or beside the source's file.
    It reads files only, so the resource readers run it with a context made on the UI thread."""
    if not normalized:
        return None
    hit = context.resolve(normalized, allow_selection_dialog=False) if context is not None else None
    if hit is not None:
        return hit
    direct = Path(normalized)
    if direct.is_file():
        return str(direct), direct.read_bytes()
    source_path = Path(source_file)
    if source_path.is_file():
        sibling = source_path.parent / direct.name
        if sibling.is_file():
            return str(sibling), sibling.read_bytes()
    return None


class ScnSceneLoader:
    def __init__(self, document_store: ScnDocumentStore | None = None):
        self.document_store = document_store or ScnDocumentStore()
        self._source_by_document: dict[str, ScnSceneSource] = {}
        self._source_by_graph: dict[int, ScnSceneSource] = {}
        self._building_source: ScnSceneSource | None = None
        self.user_documents: dict[str, ScnDocument] = {}
        self.user_reads: dict = {}

    def source_for_graph(self, graph: ScnSceneGraph) -> ScnSceneSource | None:
        return self._source_by_graph.get(id(graph))

    def _builder(self, source: ScnSceneSource, **options) -> ScnSceneGraphBuilder:
        handler = source.handler
        return ScnSceneGraphBuilder(
            getattr(handler, "type_registry", None), resource_resolver=self.resolve_resource,
            game_version=self._game_version(source), user_reads=self.user_reads,
            user_files={key: document.rsz_file for key, document in self.user_documents.items()}, **options)

    @staticmethod
    def _game_version(source: ScnSceneSource | None) -> str:
        return str(getattr(source, "game_version", "") or getattr(getattr(source, "handler", None), "game_version", "")
                   or "")

    def user_document(self, graph: ScnSceneGraph, path: str) -> ScnDocument | None:
        key = normalize_scene_path(path)
        if key not in self.user_documents:
            source = self.source_for_graph(graph)
            resolved = self.resolve_resource_for_source(source, path)
            if resolved is None or resolved.data is None:
                return None
            rsz = RszFile()
            rsz.filepath, rsz.type_registry = path, getattr(getattr(source, "handler", None), "type_registry", None)
            rsz.game_version = self._game_version(source)
            rsz.read(resolved.data)
            context = self.resource_context_for_source(source)
            project = str(getattr(context, "project_dir", "") or "")
            resource = resolved.path if context is None else self._project_resource_path(
                resolved.path, project, context.unpacked_dir, context.path_prefix)
            self.user_documents[key] = ScnDocument(key, resource, project, rsz)
        return self.user_documents[key]

    def campaign_choices(self, graph: ScnSceneGraph) -> CampaignChoices | None:
        source, document = self.source_for_graph(graph), graph.documents.get(graph.root_document_id)
        if source is None or document is None:
            return None
        builder = self._builder(source)
        catalog = builder.campaign_catalog(str(document.source_path), document.document_id)
        return None if catalog is None else choice_names(catalog, startup_draw_requests(builder.game_version))

    def variation_states(self, graph: ScnSceneGraph,
                         variations: Mapping[str, int]) -> tuple[ScnSceneGraph, dict[str, Mapping]]:
        return repicked(self._builder(self.source_for_graph(graph), variations=variations), graph)

    def build_graphs(
        self,
        sources: list[ScnSceneSource],
        *,
        max_depth: int = 8,
        kept: Sequence[ScnSceneGraph] = (),
        campaign: CampaignChoice | None = None,
        variations: Mapping[str, int] | None = None,
        choices: Mapping[str, int] | None = None,
    ) -> list[ScnSceneGraph]:
        valid_sources = [source for source in sources if getattr(source.handler, "rsz_file", None) is not None]
        self.keep(kept)
        if not valid_sources:
            return []

        graphs: list[ScnSceneGraph] = []
        seen_document_ids = {document_id for graph in kept for document_id in graph.documents}
        for source in valid_sources:
            handler = source.handler
            builder = self._builder(source, max_depth=max_depth, campaign=campaign, variations=variations,
                                    choices=choices)
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

    def keep(self, graphs: Sequence[ScnSceneGraph]) -> None:
        sources = [(graph, self._source_by_graph[id(graph)]) for graph in graphs]
        self.user_reads.clear()
        self._source_by_graph = {id(graph): source for graph, source in sources}
        self._source_by_document = {document_id: source for graph, source in sources
                                    for document_id in graph.documents}

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
        context = self.resource_context_for_source(source)
        hit = read_scene_resource(context, normalized, scene_source_file(source))
        if hit is None:
            return None
        path, data = hit
        if ".scn" not in normalized.lower() or source is None:
            return ScnResolvedResource(path=path, data=data)
        if context:
            path = self._project_resource_path(
                path,
                context.project_dir,
                context.unpacked_dir,
                context.path_prefix,
            )
        return self._linked_scn_resource(source, path, data)

    def _linked_scn_resource(self, source: ScnSceneSource, path: str, data: bytes) -> ScnResolvedResource:
        handler = getattr(source, "handler", None)
        doc = self.document_store.load_linked(
            source,
            path,
            data,
            getattr(handler, "type_registry", None),
            self._game_version(source),
        )
        self._source_by_document[doc.document_id] = source
        return ScnResolvedResource(path=scoped_document_path(doc.project_dir, doc.resource_path), rsz_file=doc.rsz_file)

    @staticmethod
    def _project_resource_path(
        path: str,
        project_dir: str,
        unpacked_dir: str,
        path_prefix: str,
    ) -> str:
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

    def resource_context_for_source(
        self,
        source: ScnSceneSource | None,
    ) -> ResourceResolutionContext | None:
        handler = source.handler if source is not None else None
        app = getattr(handler, "app", None)
        inherited = resource_context_for_handler(handler)
        source_project = str(getattr(source, "project_dir", "") or "")
        source_game = str(getattr(source, "game_version", "") or "")
        if inherited is not None and not source_project and not source_game:
            return inherited
        project_dir = str(
            source_project
            or getattr(inherited, "project_dir", "")
            or ""
        )
        game = str(
            source_game
            or getattr(handler, "game_version", "")
            or getattr(inherited, "game", "")
            or ""
        )
        return (
            resource_context_for_app(
                app,
                project_dir=project_dir or None,
                game=game,
            )
            or inherited
        )
