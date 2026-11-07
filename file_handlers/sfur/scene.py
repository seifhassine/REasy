"""See docs/internals/shell-fur.md."""
from __future__ import annotations

import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.mesh.lod_settings import LodSettingsError, classic_table, lod_settings_file
lazy from file_handlers.mesh.material_resolver import MeshMaterialResolver
lazy from file_handlers.mesh.scene_lit import SceneLitObject, _bone_boxed
lazy from file_handlers.mesh.scene_lods import _classic_mode
lazy from file_handlers.mmtr.draw_rules import drawing_rules
lazy from file_handlers.mmtr.material_preview import MaterialPreviewError, missing_texture
lazy from file_handlers.mmtr.mesh_draw import SubmeshRange
lazy from file_handlers.rsz.plain_values import plain_fields
lazy from ui.scene.mesh_scene import mesh_lod_parts

from .sfur_file import FurParameter, read_shell_fur
from .shell_fur import (
    ShellFurCatalog, ShellFurObject, ShellLods, component_parameters, named_parameters, shell_fur_catalog,
    shell_records,
)


@dataclass(frozen=True, slots=True)
class SceneShellFur:
    key: str
    resource: str = ""
    parameters: tuple[FurParameter, ...] = ()
    refused: str = ""
    lod_settings: str = ""
    lod_limit: int | None = None


def scene_shell_furs(graph, catalog: ShellFurCatalog, mesh_component: str) -> list[SceneShellFur]:
    names = catalog.component
    owners: dict[object, SceneShellFur] = {}
    for document in graph.documents.values():
        for owner in document.objects.values():
            held = [document.components[key] for key in owner.components
                    if document.components[key].type_name == names.type]
            if not held:
                continue
            if len(held) != 1:
                owners[owner.id] = SceneShellFur("", refused=str(MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The object {name} holds several shell fur components."),
                    {"name": owner.name})))
                continue
            fields = plain_fields(document, held[0])
            if not all(fields.get(name, True) for name in names.shown):
                continue
            kinds = int(fields.get(names.shell_type) or 0), int(fields.get(names.simulation) or 0)
            if kinds[0] not in catalog.shell_types or kinds[1] not in catalog.simulations:
                owners[owner.id] = SceneShellFur("", refused=str(MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "No capture shows the renderer drawing shells of shell type {shell} and "
                                       "simulation type {simulation}."), {"shell": kinds[0], "simulation": kinds[1]})))
                continue
            if names.shell_count and int(fields.get(names.shell_count) or 0):
                owners[owner.id] = SceneShellFur("", refused=str(MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "No capture shows what a shell fur component's own shell count other than 0 "
                                       "does."))))
                continue
            if names.count_scale and float(fields[names.count_scale]) != 1.0:
                owners[owner.id] = SceneShellFur("", refused=str(MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "No capture shows what a shell count scale other than 1 does."))))
                continue
            owners[owner.id] = SceneShellFur("", str(fields.get(names.resource) or ""),
                                             component_parameters(fields.get(names.parameters) or (), catalog),
                                             lod_settings=str(fields.get(names.lod_settings) or ""),
                                             lod_limit=int(fields[names.lod_limit]) if names.lod_limit else None)
    return [replace(found, key=renderable.key) for renderable in graph.renderables
            if renderable.decal_receive[0] == mesh_component
            and (found := owners.get(renderable.source_object_id)) is not None]


def shell_fur_file(resolve, path: str) -> tuple:
    if not path:
        return ()
    found = resolve(path)
    if found is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The shell fur file {path} was not found."), {"path": path})
    try:
        return read_shell_fur(found[1])
    except (ValueError, struct.error) as exc:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The shell fur file {path} could not be read: {reason}"),
            {"path": path, "reason": str(exc)}) from exc


def gather_shell_furs(components: Sequence[SceneShellFur], objects: list[SceneLitObject], sources: list, meshes,
                      gathered: dict, read: dict, profile, files: dict, lod_settings_for: Callable,
                      streaming: bool = False) -> str:
    told = {item.key: item for item in components}
    refused, entries = [], {}
    for number, item in enumerate(objects):
        found = told.get(meshes[item.mesh].key)
        grouped, lod = sources[number]
        level = item.of.level
        # A deformed mesh draws its first LOD.
        if found is None or level is not None and lod:
            continue
        try:
            if found.refused:
                raise MaterialPreviewError(found.refused)
            catalog = shell_fur_catalog(profile)
            if id(grouped[0].handler) not in read:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The renderer draws shells over the parts of a .mesh file."))
            if item.of.skinned and not _bone_boxed(item.of):
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The renderer picks a skinned mesh's shells' LOD by its bones' boxes, "
                                       "which its file does not hold."))
            resolve, mesh = gathered[grouped].resolve, read[id(grouped[0].handler)][2].mesh
            if found.resource not in entries:
                entries[found.resource] = shell_fur_file(resolve, found.resource)
            parameters = named_parameters(entries[found.resource], found.parameters)
            names = mesh.material_names
            shading = {spot.part.submesh: spot.part.material.shading_type for spot in item.parts}
            lods = ()
            if level is not None:
                spans = []
                for submesh in level.submeshes:
                    shown = SubmeshRange(submesh.first_index, submesh.index_count, submesh.first)
                    spans.append((shown, submesh.count, names[submesh.material] if submesh.material < len(names)
                                  else "", shading.get(shown, -1), submesh.material))
                records = shell_records(spans, parameters, catalog)
                program = catalog.programs.get("skinned" if item.of.skinned else "static")
                if program is None:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "No capture shows the program the renderer makes this pre-transformed "
                                           "mesh's shells with."))
            else:
                program, records = None, shell_records(_lod_spans(mesh, lod, shading), parameters, catalog, lod)
                # The view picks the LOD of an object whose rule picks its mesh's.
                if not lod and item.levels is not None and item.levels.classic is not None:
                    lods = tuple(shell_records(_lod_spans(mesh, coarser, {
                        part.submesh: part.material.shading_type for part in parts}), parameters, catalog, coarser)
                        for coarser, (_of, parts) in enumerate(item.levels.lods, 1))
            if not records and not any(lods):
                continue
            for record in (*records, *(record for held in lods for record in held)):
                path = record.parameter.grooming_texture or catalog.default_texture
                if not record.drawn or path in files:
                    continue
                texture = MeshMaterialResolver.locate_texture_path(grouped[0].handler, path,
                                                                   prefer_streaming=streaming)
                if texture is None:
                    raise missing_texture(path)
                files[path] = texture
            levels = _shell_lods(lod_settings_for, grouped[0], gathered[grouped], mesh, item, found.lod_settings, lod,
                                 catalog, profile)
            objects[number] = replace(item, shell_fur=ShellFurObject(
                records, program, sum(record.shells * record.count for record in records), lods,
                None if levels is None else replace(levels, lod_limit=found.lod_limit)))
        except MaterialPreviewError as exc:
            refused.append(str(exc))
    return "; ".join(dict.fromkeys(refused))


def _shell_lods(lod_settings_for: Callable, session, group, mesh, item: SceneLitObject, lod_settings: str, lod: int,
                catalog, profile) -> ShellLods | None:
    rules = drawing_rules(profile)
    rule = rules.lods.get(item.decal_receive[0])
    if rule is None and not rules.lods:
        return None
    if rule is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "REasy's draw rules of this game build give the mesh no LOD rule, which picks its "
                               "shells' LOD."))
    path = lod_settings_file(lod_settings, rule.lod_settings_version) if lod_settings else None
    settings = lod_settings_for(session, group, rule, path)
    if isinstance(settings, str):
        raise MaterialPreviewError(settings)
    main = mesh.meshes[0]
    try:
        scale, shells = classic_table(settings, int(mesh.header.lod_hash), catalog.lod_count,
                                      float(main.bounding_sphere[3]), rule.rate, rule.rate2, rule.size,
                                      rule.exponent)
    except LodSettingsError as exc:
        raise MaterialPreviewError(str(exc)) from exc
    classic = None if lod or item.of.level is not None or item.levels is None else item.levels.classic
    if classic is None:
        box = np.asarray(main.bounding_box, np.float64)[:, :3]
        return ShellLods(tuple(map(float, (box[0] + box[1]) * 0.5)), scale, (), shells, lod,
                         rule.depth_over_p00)
    return ShellLods(classic.centre, classic.scale, classic.rules[item.decal_receive[0]][1], shells,
                     _classic_mode(rules, item.draw_state, rule)[0], rule.depth_over_p00)


def _lod_spans(mesh, lod: int, shading: Mapping[SubmeshRange, int]) -> list[tuple[SubmeshRange, int, str, int, int]]:
    names, payloads = mesh.material_names, mesh.mesh_buffer.buffer_payloads
    parts = [submesh for _group, submesh in mesh_lod_parts(mesh, lod)]
    spans = []
    for number, submesh in enumerate(parts):
        after = [other.verts_index_offset for other in parts[number + 1:] if other.buffer_index == submesh.buffer_index]
        end = after[0] if after else int(payloads[submesh.buffer_index].vertex_count)
        shown, material = SubmeshRange.from_submesh(submesh), submesh.material_index
        spans.append((shown, end - submesh.verts_index_offset, names[material] if material < len(names) else "",
                      shading.get(shown, -1), material))
    return spans


__all__ = ["SceneShellFur", "gather_shell_furs", "scene_shell_furs", "shell_fur_file"]
