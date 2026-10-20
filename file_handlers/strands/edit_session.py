from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

lazy from file_handlers.mmtr.pretransform import chain_level, pretransform_catalog_for
from file_handlers.mmtr.profiles import mmtr_profile_for_game
from utils.resource_file_utils import natives_relative

from .binding_surface import BindingSurface
lazy from .bindings import StrandBindings
from .chain_catalog import strand_catalog_for
from .chain_scene import ChainScene, bound_mesh_path, gather, handler_files
from .companions import prefab_graph


@dataclass(frozen=True, slots=True)
class EditFile:
    named: str
    found: str
    data: bytes


@dataclass(frozen=True, slots=True)
class EditSession:
    """``presets`` lists the base preset first."""
    scene: ChainScene
    strands: EditFile
    binding: EditFile
    surfaces: tuple[BindingSurface, ...] | None
    surfaces_refused: str
    presets: tuple[EditFile, ...]
    colliders: tuple[EditFile, ...]
    lod_settings: EditFile | None


def load_session(handler, prefab_path: str, entry, character_path: str, game: str) -> EditSession:
    files, profile = handler_files(handler, game), mmtr_profile_for_game(game)

    def find(path: str, owner: str) -> EditFile:
        hit = files.find(path, owner)
        if hit is None:
            raise ValueError(f"The file {path} was not found.")
        return EditFile(path, *hit)

    prefab = find(prefab_path, "")
    strands, binding = find(entry.strands_path, prefab.found), find(entry.binding_path, prefab.found)
    bound = StrandBindings.read(binding.data)
    mesh = find(bound.mesh_path or bound_mesh_path(bound, strand_catalog_for(profile), prefab_graph(
        files.registry, prefab.found, prefab.data, game), entry.strands_path), prefab.found)
    parsed = files.mesh(mesh.found, mesh.data)
    chains = pretransform_catalog_for(profile)
    scene = gather(files, game, profile, parsed, mesh.found, entry.strands_path, prefab.found, character_path, 0,
                   None if chains is None else chain_level(parsed, 0, chains))
    surfaces, refused = None, ""
    try:
        surfaces = tuple(BindingSurface.of(parsed, level).prepare() for level in range(len(bound.tables)))
    except ValueError as error:
        refused = str(error)
    arguments = scene.catalog.constants.arguments
    dynamics = scene.source.dynamics
    named = [dynamics[arguments["preset"]], *(dynamics[arguments["more_presets"]] or ())]
    presets = {path: find(path, prefab.found) for path in dict.fromkeys(path for path in named if path)}
    colliders = dict.fromkeys(shape.read_from[0] for _owner, shape in (*scene.colliders, *scene.lost))
    lod_settings = scene.catalog.render.lod_settings
    settings = None if lod_settings is None else files.find(lod_settings, "")
    return EditSession(scene, strands, binding, surfaces, refused, tuple(presets.values()),
                       tuple(find(path, "") for path in colliders),
                       None if settings is None else EditFile(lod_settings, *settings))


def save_target(found: str, project_dir: str = "", folder: str = "") -> str | None:
    if project_dir and os.path.isabs(found):
        try:
            Path(found).resolve().relative_to(Path(project_dir).resolve())
        except ValueError:
            pass
        else:
            return str(Path(found))
    root = project_dir or folder
    relative = natives_relative(found)
    if not root or relative is None:
        return None
    return str(Path(root, *relative.split("/")))


__all__ = ["EditFile", "EditSession", "load_session", "save_target"]
