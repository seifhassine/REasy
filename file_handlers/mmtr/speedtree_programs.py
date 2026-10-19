"""See docs/internals/speedtree.md, Programs."""
from __future__ import annotations

from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.stmesh.stmesh_file import BatchType

from .meshlets import MeshletPrograms
from .pass_choice import PassChoiceError, speedtree_route
from .speedtree_catalog import SpeedTreeCatalog, SpeedTreePlacement


def speedtree_placement(catalog: SpeedTreeCatalog | None, placement: str | None) -> SpeedTreePlacement:
    if catalog is None:
        raise PassChoiceError(QT_TRANSLATE_NOOP(
            "PassChoice", "REasy has not read how this game's renderer picks the programs of SpeedTree meshes."))
    if placement is None:
        return catalog.viewed
    found = catalog.placements.get(placement)
    if found is None:
        raise PassChoiceError(QT_TRANSLATE_NOOP(
            "PassChoice", "No capture of this game build shows how the renderer draws a SpeedTree mesh placed as "
                          "a {placement}."), {"placement": placement})
    return found


def speedtree_stem(catalog: SpeedTreeCatalog | None, placement: str | None, batch: int, material_flags: int,
                   grass: bool) -> str:
    drawn = speedtree_placement(catalog, placement)
    if drawn.stem is None:
        raise PassChoiceError(QT_TRANSLATE_NOOP(
            "PassChoice", "This game's renderer draws SpeedTree meshes placed this way as meshlets, not from the "
                          "mesh's vertex streams."))
    programs = catalog.programs
    try:
        found = programs.shader_types.get(BatchType(batch).name)
    except ValueError:
        found = None
    if found is None:
        raise PassChoiceError(QT_TRANSLATE_NOOP(
            "PassChoice", "REasy has not read which programs the renderer draws SpeedTree batches of type {batch} "
                          "with."), {"batch": batch})
    own, kind = found
    held = {marker for marker, _bits in programs.material_markers if marker in own}
    added = {marker for marker, bits in programs.material_markers if material_flags & bits} - held
    if added and kind not in programs.kinds_taking_material_markers:
        raise PassChoiceError(QT_TRANSLATE_NOOP(
            "PassChoice", "REasy has not read whether the renderer draws a SpeedTree batch of type {batch} two-sided "
                          "or alpha-tested when its material's flags say so."), {"batch": batch})
    if kind == "Billboard":
        if drawn.billboard_stem is None:
            raise PassChoiceError(QT_TRANSLATE_NOOP(
                "PassChoice", "No capture of this game build shows the programs the renderer draws SpeedTree "
                              "billboards with."))
        return drawn.billboard_stem
    marker = "".join(marker for marker, _bits in programs.material_markers if marker in held | added)
    return drawn.stem.format(marker=marker, kind=kind, input=drawn.input_types["grass" if grass else "tree"])


def speedtree_meshlet_programs(catalog: SpeedTreeCatalog | None, placement: str | None, material_flags: int,
                               billboard: bool) -> MeshletPrograms:
    drawn = speedtree_placement(catalog, placement)
    if drawn.meshlets is None:
        raise PassChoiceError(QT_TRANSLATE_NOOP(
            "PassChoice", "No capture of this game build shows how the renderer draws meshlet SpeedTree meshes."))
    names = drawn.meshlets
    marker = "".join(marker for marker, bits in catalog.programs.material_markers if material_flags & bits)
    word = names.billboard if billboard else ""
    return MeshletPrograms(names.raster.format(marker=marker, billboard=word), names.shade.format(billboard=word),
                           names.transform.format(billboard=word))


def speedtree_cluster_route(catalog: SpeedTreeCatalog | None, placement: str | None, batch: int, material_flags: int,
                            grass: bool) -> str:
    return speedtree_route(speedtree_stem(catalog, placement, batch, material_flags, grass))


__all__ = ["speedtree_cluster_route", "speedtree_meshlet_programs", "speedtree_placement", "speedtree_stem"]
