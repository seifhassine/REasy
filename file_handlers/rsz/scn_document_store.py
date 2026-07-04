from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from file_handlers.rsz.rsz_file import RszFile

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
            if doc.dirty or doc.handler is not None:
                return doc
            doc.rsz_file = self._read_linked(source, path, data, type_registry, game_version)
            doc.revision += 1
            return doc
        doc = self._documents[key] = ScnDocument(key, path, project_dir, self._read_linked(source, path, data, type_registry, game_version))
        return doc

    def _read_linked(self, source, path: str, data: bytes, type_registry, game_version: str) -> RszFile:
        project_dir = str(getattr(source, "project_dir", "") or "")
        rsz_file = RszFile()
        rsz_file.filepath = scoped_document_path(project_dir, path)
        rsz_file.type_registry = type_registry
        rsz_file.game_version = game_version
        rsz_file.auto_resource_management = bool(getattr(getattr(source, "handler", None), "auto_resource_management", False))
        rsz_file.read(data)
        return rsz_file

    def get(self, document_id: str) -> ScnDocument | None:
        return self._documents.get(normalize_document_id(document_id))

    def mark_dirty(self, document_id: str) -> None:
        doc = self.get(document_id)
        if doc is None:
            return
        doc.dirty = True
        doc.revision += 1
        self._set_handler_modified(doc.handler, True)

    def clear_handler(self, handler: object) -> None:
        for doc in self._documents.values():
            if doc.handler is handler:
                doc.dirty = False

    def detach_handler(self, handler: object) -> None:
        for key, doc in list(self._documents.items()):
            if doc.handler is handler:
                doc.handler = None
                if doc.scene_owner is None and not doc.dirty:
                    self._documents.pop(key, None)

    def discard_handler(self, handler: object) -> None:
        for key, doc in list(self._documents.items()):
            if doc.handler is handler:
                doc.handler = None
                if doc.scene_owner is None:
                    self._documents.pop(key, None)

    def discard_owner(self, owner: object) -> None:
        for key, doc in list(self._documents.items()):
            if doc.scene_owner is owner:
                doc.scene_owner = None
                if doc.handler is None:
                    self._documents.pop(key, None)

    def claim_graphs(self, owner: object, graphs: Iterable[ScnSceneGraph]) -> None:
        self.release_owner(owner)
        for graph in graphs:
            for document_id in graph.documents:
                if doc := self.get(document_id):
                    doc.scene_owner = owner

    def release_owner(self, owner: object) -> None:
        for key, doc in list(self._documents.items()):
            if doc.scene_owner is owner:
                doc.scene_owner = None
                if doc.handler is None and not doc.dirty:
                    self._documents.pop(key, None)

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
        if viewer := getattr(handler, "_viewer", None):
            try:
                viewer.modified = modified
            except RuntimeError:
                if getattr(handler, "_viewer", None) is viewer:
                    setattr(handler, "_viewer", None)
