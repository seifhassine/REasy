from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from file_handlers.rsz.rsz_file import RszFile
from file_handlers.rsz.rsz_handler import RszHandler

from .scn_scene_graph import ScnSceneGraph, normalize_document_id, normalize_scene_path


def scoped_document_path(project_dir: str, path: str) -> str:
    path = normalize_scene_path(path)
    if project_dir and path and not Path(path).is_absolute():
        project_key = os.path.normcase(os.path.abspath(project_dir)).replace("\\", "/")
        return f"{project_key}|{path}"
    return path


@dataclass(slots=True)
class ScnDocument:
    document_id: str
    resource_path: str
    project_dir: str
    rsz_file: RszFile
    handler: object | None = None
    dirty: bool = False
    revision: int = 0
    scene_owner: object | None = None

    @property
    def save_path(self) -> str:
        path = normalize_scene_path(self.resource_path)
        if not self.project_dir:
            raise ValueError(f"SCN has no project save target: {path}")
        target = Path(path) if Path(path).is_absolute() else Path(self.project_dir, *path.split("/"))
        root = Path(self.project_dir).resolve()
        resolved = target.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"SCN save target escapes project: {path}")
        return str(target)


class ScnDocumentStore:
    def __init__(self):
        self._documents: dict[str, ScnDocument] = {}

    def document_id(self, project_dir: str, path: str) -> str:
        return normalize_document_id(scoped_document_path(project_dir, path))

    def attach_source(self, source, rsz_file: RszFile | None = None, handler: object | None = None, *, replace: bool = False) -> ScnDocument:
        handler = handler or getattr(source, "handler", None)
        rsz_file = rsz_file or getattr(handler, "rsz_file", None)
        if rsz_file is None:
            raise ValueError("SCN source has no parsed RszFile")
        project_dir = str(getattr(source, "project_dir", "") or "")
        path = normalize_scene_path(str(getattr(source, "path", "") or getattr(handler, "filepath", "") or getattr(rsz_file, "filepath", "")))
        key = self.document_id(project_dir, path)
        doc = self._documents.get(key)
        if doc is None:
            doc = self._documents[key] = ScnDocument(key, path, project_dir, rsz_file, handler)
        else:
            if replace:
                doc.rsz_file = rsz_file
                doc.dirty = False
                doc.revision += 1
            elif doc.scene_owner is not None and doc.rsz_file is not rsz_file:
                if handler is not None:
                    setattr(handler, "rsz_file", doc.rsz_file)
            else:
                doc.rsz_file = rsz_file
            doc.handler = handler or doc.handler
            doc.resource_path = path or doc.resource_path
            doc.project_dir = project_dir or doc.project_dir
        return doc

    def load_linked(self, source, path: str, data: bytes, type_registry, game_version: str) -> ScnDocument:
        project_dir = str(getattr(source, "project_dir", "") or "")
        path = normalize_scene_path(path)
        key = self.document_id(project_dir, path)
        doc = self._documents.get(key)
        if doc is not None:
            if doc.dirty or (doc.handler is not None and not self._is_background_handler(doc.handler)):
                return doc
            doc.handler = self._read_linked_handler(source, path, data, type_registry, game_version)
            doc.rsz_file = doc.handler.rsz_file
            doc.revision += 1
            return doc
        handler = self._read_linked_handler(source, path, data, type_registry, game_version)
        doc = self._documents[key] = ScnDocument(key, path, project_dir, handler.rsz_file, handler)
        return doc

    def _read_linked_handler(self, source, path: str, data: bytes, type_registry, game_version: str) -> RszHandler:
        project_dir = str(getattr(source, "project_dir", "") or "")
        source_handler = getattr(source, "handler", None)
        handler = RszHandler()
        handler.app = getattr(source_handler, "app", None)
        handler.filepath = scoped_document_path(project_dir, path)
        handler.type_registry = type_registry
        handler.game_version = game_version
        handler.auto_resource_management = bool(getattr(source_handler, "auto_resource_management", False))
        handler._scn_background_handler = True
        handler.read(data)
        return handler

    def get(self, document_id: str) -> ScnDocument | None:
        return self._documents.get(normalize_document_id(document_id))

    def mark_dirty(self, document_id: str) -> None:
        if doc := self.get(document_id):
            doc.dirty = True
            doc.revision += 1
            self._set_handler_modified(doc.handler, True)

    def clear_handler(self, handler: object) -> None:
        for doc in self._documents.values():
            if doc.handler is handler:
                doc.dirty = False
        self._set_handler_modified(handler, False)

    def document_ids_for_handler(self, handler: object) -> set[str]:
        return {doc.document_id for doc in self._documents.values() if doc.handler is handler}

    def detach_handler(self, handler: object) -> None:
        self._release_handler(handler)

    def discard_handler(self, handler: object) -> None:
        self._release_handler(handler, keep_dirty=False)

    def _release_handler(self, handler: object, *, keep_dirty: bool = True) -> None:
        for key, doc in list(self._documents.items()):
            if doc.handler is handler:
                doc.handler = None
                if doc.scene_owner is None and (not keep_dirty or not doc.dirty):
                    self._documents.pop(key, None)

    def discard_owner(self, owner: object) -> None:
        self._release_owner(owner, keep_dirty=False)

    def claim_graphs(self, owner: object, graphs: Iterable[ScnSceneGraph]) -> None:
        document_ids = {document_id for graph in graphs for document_id in graph.documents}
        for key, doc in list(self._documents.items()):
            if doc.scene_owner is owner and doc.document_id not in document_ids:
                doc.scene_owner = None
                self._drop_scene_only_doc(key, doc)
        for document_id in document_ids:
            if doc := self.get(document_id):
                doc.scene_owner = owner

    def release_owner(self, owner: object) -> None:
        self._release_owner(owner)

    def _release_owner(self, owner: object, *, keep_dirty: bool = True) -> None:
        for key, doc in list(self._documents.items()):
            if doc.scene_owner is owner:
                doc.scene_owner = None
                self._drop_scene_only_doc(key, doc, keep_dirty=keep_dirty)

    def documents_for_owner(self, owner: object) -> list[ScnDocument]:
        return [doc for doc in self._documents.values() if doc.scene_owner is owner]

    def dirty_documents(self, graphs: Iterable[ScnSceneGraph]) -> list[ScnDocument]:
        ids = {document_id for graph in graphs for document_id in graph.documents}
        return [doc for document_id in ids if (doc := self.get(document_id)) and doc.dirty]

    def save_graphs(self, graphs: Iterable[ScnSceneGraph]) -> int:
        count = 0
        for doc in self.dirty_documents(graphs):
            data = doc.rsz_file.build()
            path = Path(doc.save_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            doc.dirty = False
            self._set_handler_modified(doc.handler, False)
            count += 1
        return count

    @staticmethod
    def _set_handler_modified(handler: object | None, modified: bool) -> None:
        if handler is None:
            return
        setattr(handler, "modified", modified)
        for attr in ("_viewer", "_scene_raw_viewer"):
            viewer = getattr(handler, attr, None)
            if viewer is None:
                continue
            try:
                viewer.modified = modified
            except RuntimeError:
                if getattr(handler, attr, None) is viewer:
                    setattr(handler, attr, None)

    @staticmethod
    def _is_background_handler(handler: object | None) -> bool:
        return bool(getattr(handler, "_scn_background_handler", False))

    def _drop_scene_only_doc(self, key: str, doc: ScnDocument, *, keep_dirty: bool = True) -> None:
        if (not keep_dirty or not doc.dirty) and (doc.handler is None or self._is_background_handler(doc.handler)):
            self._documents.pop(key, None)
