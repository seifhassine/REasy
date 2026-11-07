from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass, replace

lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.mmtr.material_preview import MaterialPreviewError
lazy from file_handlers.mmtr.wrinkle import (
    WrinkleCatalog, component_settings, mesh_joints, render_target_form, wrinkle_catalog_for, wrinkle_object,
    wrinkle_proxy,
)
lazy from file_handlers.rsz.plain_values import plain_fields
lazy from utils.resource_file_utils import resource_version_from_path

lazy from .mesh_file import MeshFile
lazy from .scene_lit import SceneLitObject


@dataclass(frozen=True, slots=True)
class SceneWrinkle:
    key: str
    render_target: str = ""
    spread: bool = False
    proxy: str = ""
    refused: str = ""


def scene_wrinkles(graph, catalog: WrinkleCatalog) -> list[SceneWrinkle]:
    names = catalog.component
    owners: dict[object, SceneWrinkle] = {}
    for document in graph.documents.values():
        for owner in document.objects.values():
            held = {kind: [document.components[key] for key in owner.components
                           if document.components[key].type_name == kind] for kind in (names.type, names.proxy)}
            if not held[names.type]:
                continue
            if len(held[names.type]) != 1 or len(held[names.proxy]) != 1:
                owners[owner.id] = SceneWrinkle("", refused=str(MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The object {name} does not hold one wrinkle and one skinning component."),
                    {"name": owner.name})))
                continue
            proxy = str(plain_fields(document, held[names.proxy][0]).get(names.mesh) or "")
            try:
                target, spread = component_settings(plain_fields(document, held[names.type][0]), catalog)
            except MaterialPreviewError as exc:
                owners[owner.id] = SceneWrinkle("", refused=str(exc))
                continue
            owners[owner.id] = SceneWrinkle("", target, spread, proxy, "" if proxy else str(MaterialPreviewError(
                QT_TRANSLATE_NOOP("MaterialPreview", "The object {name}'s skinning component names no proxy mesh."),
                {"name": owner.name})))
    return [SceneWrinkle(renderable.key, *(getattr(found, name) for name in ("render_target", "spread", "proxy",
                                                                            "refused")))
            for renderable in graph.renderables if (found := owners.get(renderable.source_object_id)) is not None]


def gather_wrinkles(components: Sequence[SceneWrinkle], objects: list[SceneLitObject], sources: list, meshes,
                    gathered: dict, read: dict, profile, files: dict) -> tuple[bool, str]:
    told = {item.key: item for item in components}
    catalog = wrinkle_catalog_for(profile) if told else None
    refused, proxies = [], {}
    for number, item in enumerate(objects):
        wrinkle = told.get(meshes[item.mesh].key)
        if wrinkle is None:
            continue
        grouped, _lod = sources[number]
        session, resolve = grouped[0], gathered[grouped].resolve
        try:
            if wrinkle.refused:
                raise MaterialPreviewError(wrinkle.refused)
            if catalog is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "REasy has no catalog of the wrinkle passes of this game build."))
            if not item.of.skinned or id(session.handler) not in read:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The wrinkle passes skin their proxy by the bones of a skinned mesh."))
            if wrinkle.proxy not in proxies:
                proxies[wrinkle.proxy] = _wrinkle_proxy(resolve, wrinkle.proxy)
            target = resolve(wrinkle.render_target)
            if target is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The render texture file {path} was not found."),
                    {"path": wrinkle.render_target})
            render_target_form(target[1])
            files[wrinkle.render_target] = target[1]
            joints, binds = mesh_joints(read[id(session.handler)][2].mesh)
            objects[number] = replace(item, wrinkle=wrinkle_object(
                proxies[wrinkle.proxy], joints, binds, wrinkle.render_target, wrinkle.spread))
        except MaterialPreviewError as exc:
            refused.append(str(exc))
    return any(item.wrinkle is not None for item in objects), "; ".join(dict.fromkeys(refused))


def _wrinkle_proxy(resolve, path: str):
    found = resolve(path)
    if found is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The wrinkle proxy mesh {path} was not found."), {"path": path})
    mesh = MeshFile()
    try:
        mesh.read(found[1], file_version=resource_version_from_path(found[0], "mesh"))
    except (ValueError, IndexError, struct.error) as exc:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The wrinkle proxy mesh {path} could not be read: {reason}"),
            {"path": path, "reason": str(exc)}) from exc
    return wrinkle_proxy(mesh)


__all__ = ["SceneWrinkle", "gather_wrinkles", "scene_wrinkles"]
