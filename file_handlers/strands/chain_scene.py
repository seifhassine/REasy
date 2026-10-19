"""See docs/internals/strands-rendering.md."""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.clsp.clsp_file import TAG_BITS, CollisionShape, read_clsp
lazy from file_handlers.clsp.placement import hung_joints
from file_handlers.clsp.registered import named, registered
lazy from file_handlers.mdf.mdf_file import MdfFile
from file_handlers.mmtr.material_preview import (
    PreviewMaterial, _texture_texels, gpu_buffer_contents,
)
lazy from file_handlers.mmtr.pretransform import ChainLevel, refusal, chain_level, pretransform_catalog_for
from file_handlers.rsz.plain_values import plain_fields
from file_handlers.rsz.scene_joints import JointSet
lazy from utils.resource_file_utils import (
    resource_key,
    resource_path_with_version,
    nearby_resource_paths,
    resolve_companion_resource,
    resource_context_for_handler,
)

lazy from .bindings import StrandBindings
from .chain_catalog import StrandCatalog, strand_catalog_for
from .chain_frame import Binding, SetSource
from .companions import prefab_graph
from .gpus import GpuStrandsPreset, gpus_version
from .lod import first_lod, lod_records
lazy from .prefab_sets import prefab_sets
from .strands_file import StrandsFile
lazy from file_handlers.mesh.mesh_file import MeshLoadMode
lazy from file_handlers.mesh.mesh_handler import MeshHandler
lazy from .companions import _registry

_INTERNAL_SKINNING = "useInternalSkinning"
_UNCLAIMED = -(2 ** 31)
# The game ranks a ``.clsp`` named directly by user files below every preset.
_NAMED_PRIORITY = _UNCLAIMED + 1
# The renderer's 1x1 white texture: the wetness mask without a stamp.
_WHITE = (28, 1, 1, 1, 1, b"\xff\xff\xff\xff")


@dataclass(frozen=True, slots=True)
class GameFiles:
    """``nearby`` lists the same folder and the one above, plus siblings with ``beside``."""
    find: Callable[[str, str], tuple[str, bytes] | None]
    nearby: Callable[..., tuple[str, ...]]
    registry: object
    mesh: Callable[..., object]


@dataclass(frozen=True, slots=True)
class Holding:
    path: str
    shapes: tuple[CollisionShape, ...]
    priority: int
    holder: str
    owner: int | None
    everywhere: bool = False
    taken: bool = True


@dataclass(frozen=True, slots=True)
class Settled:
    reached: tuple[tuple[int, ...], ...]
    tree_claims: tuple[int, ...]
    tree_by: tuple[int | None, ...]
    final_by: tuple[int | None, ...]

    def taker(self, holding: Holding, bit: int) -> int | None:
        if holding.owner is not None and self.tree_claims[bit] > holding.priority:
            return self.tree_by[bit]
        return self.final_by[bit]


@dataclass(frozen=True, slots=True)
class ChainScene:
    catalog: StrandCatalog
    document: object
    owner: int
    source: SetSource
    character: object
    holder: int
    colliders: tuple[tuple[int, CollisionShape], ...]
    viewed: JointSet
    mesh_fields: Mapping[str, object]
    boxes: tuple[tuple[tuple[float, ...], tuple[float, ...]], ...]
    remap: tuple[int, ...]
    meshes: Mapping[str, object]
    skeletons: Mapping[str, bytes]
    unplaced: tuple[CollisionShape, ...] = ()
    lost: tuple[tuple[int, CollisionShape], ...] = ()
    holdings: tuple[Holding, ...] = ()


def _holders(catalog: StrandCatalog, graph, mesh_path: str) -> list[tuple[object, object, dict]]:
    kind, resource = catalog.lod.mesh_component, catalog.placement.mesh["resource"]
    return [(document, item, fields) for document in graph.documents.values() for item in document.objects.values()
            for key in item.components if document.components[key].type_name == kind
            for fields in [plain_fields(document, document.components[key])]
            if resource_key(fields[resource]) == resource_key(mesh_path)]


def character_prefab(files: GameFiles, game: str, catalog: StrandCatalog, graph, owner: str, mesh_path: str,
                     character_path: str) -> tuple[str, object, tuple[object, object, dict]]:
    return chosen_character(character_candidates(files, game, catalog, graph, owner, mesh_path, character_path),
                            mesh_path)


def character_candidates(files: GameFiles, game: str, catalog: StrandCatalog, graph, owner: str, mesh_path: str,
                         character_path: str) -> list[tuple[str, object, list]]:
    if character_path:
        hit = files.find(character_path, "")
        if hit is None:
            raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The prefab {path} was not found."),
                           path=character_path)
        found = [(hit[0], prefab_graph(files.registry, *hit, game))]
    else:
        found = [(owner, graph)]
        named = resource_key(mesh_path).rsplit("/", 1)[-1].encode("utf-16-le")
        near = (*files.nearby("pfb", owner), *files.nearby("pfb", mesh_path))
        for path in dict.fromkeys((*near, *files.nearby("pfb", owner, True))):
            hit = files.find(path, "")
            if hit is not None and resource_key(hit[0]) != resource_key(owner) and named in hit[1].lower():
                found.append((hit[0], prefab_graph(files.registry, *hit, game)))
    holding = [(path, held, _holders(catalog, held, mesh_path)) for path, held in found]
    return [entry for entry in holding if entry[2]]


def chosen_character(holding: list[tuple[str, object, list]], mesh_path: str
                     ) -> tuple[str, object, tuple[object, object, dict]]:
    if not holding:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "No prefab holding the mesh {mesh} was found."),
                       mesh=mesh_path)
    if len(holding) > 1:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "{count} prefabs hold the mesh {mesh}: choose the "
                                                            "character prefab."), count=len(holding), mesh=mesh_path)
    (path, held, objects), = holding
    for document, item, fields in objects:
        parents = {other.id.local_object_id: other for other in document.objects.values()}

        def above(other) -> bool:
            while other.parent_id in parents:
                other = parents[other.parent_id]
                if other is item:
                    return True
            return False
        if all(other is item or held_document is document and above(other)
               for held_document, other, _fields in objects):
            return path, held, (document, item, fields)
    raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "{count} objects of the prefab {path} hold the mesh, none "
                                                        "under another."), count=len(objects), path=path)


def _tree_holdings(files: GameFiles, catalog: StrandCatalog, document, holder, path: str) -> tuple[Holding, ...]:
    placement = catalog.placement
    kind, preset, info = placement.preset_component, placement.preset, placement.preset_info

    def held(item) -> dict | None:
        found = [document.components[key] for key in item.components
                 if document.components[key].type_name == kind]
        return plain_fields(document, found[0]) if found else None
    parents = {item.id.local_object_id: item for item in document.objects.values()}
    root = holder
    if held(root) is None:
        return ()
    while root.parent_id in parents and held(parents[root.parent_id]) is not None:
        root = parents[root.parent_id]
    tree = []

    def visit(item) -> None:
        tree.append((item, held(item)))
        for child in document.objects.values():
            if child.parent_id == item.id.local_object_id and held(child) is not None:
                visit(child)
    visit(root)
    clsp: dict[str, tuple[str, tuple[CollisionShape, ...]]] = {}
    global_shapes = any(fields[preset["global_shapes"]] for _item, fields in tree)
    holdings = []
    for item, fields in tree:
        for entry in _enabled_infos(placement, fields):
            resource = entry[info["resource"]]
            if not resource:
                continue
            if resource not in clsp:
                clsp[resource] = _clsp(files, resource, path)
            everywhere = bool(entry[info["global"]])
            holdings.append(Holding(*clsp[resource], int(fields[preset["priority"]]), path, item.id.local_object_id,
                                    everywhere, global_shapes or not everywhere))
    return tuple(holdings)


def _claims(tagged) -> tuple[list[int], list[int | None]]:
    claims, by = [_UNCLAIMED] * TAG_BITS, [None] * TAG_BITS
    for tag, priority, number in tagged:
        for bit in range(TAG_BITS):
            if tag >> bit & 1 and priority > claims[bit]:
                claims[bit], by[bit] = priority, number
    return claims, by


def _won(claims: Sequence[int], priority: int) -> int:
    return sum(1 << bit for bit in range(TAG_BITS) if claims[bit] == priority)


def settle(holdings: Sequence[Holding], shapes: Sequence[Sequence[CollisionShape]] | None = None) -> Settled:
    """As the game gives tag bits: each to the highest priority, the character tree's shapes before connected presets'.
    """
    shapes = [holding.shapes for holding in holdings] if shapes is None else shapes
    tree_claims, tree_by = _claims((shape.tag, holding.priority, number)
                                   for number, (holding, held) in enumerate(zip(holdings, shapes))
                                   if holding.owner is not None for shape in held)
    unclaimed = _won(tree_claims, _UNCLAIMED)
    reached = []
    for holding, held in zip(holdings, shapes):
        if holding.owner is None:
            reached.append([shape.tag for shape in held])
        elif holding.everywhere:
            reached.append([shape.tag & unclaimed if holding.taken else 0 for shape in held])
        else:
            won = _won(tree_claims, holding.priority)
            reached.append([shape.tag & won for shape in held])
    final_claims, final_by = _claims((tag, holding.priority, number)
                                     for number, (holding, tags) in enumerate(zip(holdings, reached))
                                     if not holding.everywhere for tag in tags)
    for holding, tags in zip(holdings, reached):
        if not holding.everywhere:
            won = _won(final_claims, holding.priority)
            tags[:] = [tag & won for tag in tags]
    return Settled(tuple(map(tuple, reached)), tuple(tree_claims), tuple(tree_by), tuple(final_by))


def _enabled_infos(placement, fields: Mapping) -> Iterator[Mapping]:
    if fields[placement.preset["enabled"]]:
        yield from (entry for entry in fields[placement.preset["infos"]] if entry[placement.preset_info["enabled"]])


def _clsp(files: GameFiles, resource: str, owner: str) -> tuple[str, tuple[CollisionShape, ...]]:
    found = files.find(resource, owner)
    if found is None:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The file {path} was not found."), path=resource)
    return found[0], read_clsp(found[1], found[0])


def _registered_holdings(files: GameFiles, catalog: StrandCatalog, game: str, path: str) -> tuple[Holding, ...]:
    def named_by(user: str) -> frozenset[str] | None:
        hit = files.find(user, "")
        return None if hit is None else named(files.registry, game, *hit)
    connected = registered(path, files.nearby("user", path), named_by)
    placement = catalog.placement
    resources = {resource_key(item): (item, path, _NAMED_PRIORITY, "") for item in connected.presets}
    for part in connected.parts:
        found = files.find(part, path)
        if found is None:
            raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The prefab {path} was not found."), path=part)
        for document in prefab_graph(files.registry, *found, game).documents.values():
            for component in document.components.values():
                if component.type_name != placement.preset_component:
                    continue
                fields = plain_fields(document, component)
                priority = int(fields[placement.preset["priority"]])
                for entry in _enabled_infos(placement, fields):
                    if resource := entry[placement.preset_info["resource"]]:
                        key = resource_key(resource)
                        held = resources.get(key)
                        resources[key] = ((resource, found[0], priority, found[0]) if held is None
                                          else (*held[:2], priority, found[0]) if priority > held[2] else held)
    return tuple(Holding(*_clsp(files, resource, owner), priority, holder, None)
                 for resource, owner, priority, holder in resources.values())


def _hung(shape: CollisionShape, joints: JointSet | None) -> bool:
    return joints is not None and all(joints.hashed(end) is not None for end in hung_joints(shape))


def _placement_files(files: GameFiles, graphs, catalog: StrandCatalog, owner: str) -> tuple[dict, dict]:
    placement, meshes, skeletons = catalog.placement, {}, {}
    for document in (*graph.documents.values() for graph in graphs):
        for component in document.components.values():
            if component.type_name not in (placement.mesh_component, placement.skeleton_component):
                continue
            is_mesh = component.type_name == placement.mesh_component
            path = plain_fields(document, component)[(placement.mesh if is_mesh else placement.skeleton)["resource"]]
            hit = files.find(path, owner) if path else None
            if hit is not None:
                (meshes if is_mesh else skeletons)[path] = files.mesh(*hit, light=True) if is_mesh else hit[1]
    return meshes, skeletons


@dataclass(frozen=True, slots=True)
class SetRead:
    catalog: StrandCatalog
    owner: str
    graph: object
    chosen: object
    own: object
    source: SetSource
    bound: bool


@dataclass(frozen=True, slots=True)
class CharacterRead:
    path: str
    characters: object
    document: object
    holder: object
    mesh_fields: Mapping[str, object]
    holdings: tuple[Holding, ...]


def bound_mesh_path(bindings: StrandBindings, catalog: StrandCatalog, graph, strands_path: str) -> str:
    """RE4 ``.sbd`` files name no mesh: the set's object draws it."""
    if bindings.mesh_path:
        return bindings.mesh_path
    placement, render = catalog.placement, catalog.render.arguments
    meshes = [plain_fields(document, document.components[key])[placement.mesh["resource"]]
              for item in prefab_sets(graph, catalog)
              if resource_key(item.render[render["strands"]]) == resource_key(strands_path)
              for document in [graph.documents[item.owner.id.document_id]] for key in item.owner.components
              if document.components[key].type_name == placement.mesh_component]
    if len(meshes) != 1 or not meshes[0]:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The strands' binding names no mesh, and their object "
                                                            "draws {count} meshes."), count=len(meshes))
    return meshes[0]


def shader_paths(catalog: StrandCatalog) -> dict[str, str]:
    files = (catalog.file, catalog.render.file, *(() if catalog.skinning is None else (catalog.skinning.file,)))
    return {path: resource_path_with_version(path, "sdf", catalog.resource_version) for path in files}


def gather(files: GameFiles, game: str, profile, mesh, mesh_path: str, strands_path: str, prefab_path: str,
           character_path: str, lod: int, level: ChainLevel | None) -> ChainScene:
    read = read_set(files, game, profile, mesh, mesh_path, strands_path, prefab_path, lod, level)
    character = read_character(files, read, character_prefab(files, game, read.catalog, read.graph, read.owner,
                                                             mesh_path, character_path), lod)
    holdings = (*character.holdings, *_registered_holdings(files, read.catalog, game, character.path))
    meshes, skeletons = _placement_files(files, (read.graph, character.characters), read.catalog, read.owner)
    boxes = () if mesh.bounding_boxes is None else tuple(
        (tuple(box.minimum), tuple(box.maximum)) for box in mesh.bounding_boxes.boxes)
    viewed, holder = JointSet.of_mesh(mesh), character.holder.id.local_object_id
    own, everywhere, connected, unplaced, lost = [], [], [], [], []
    for holding, tags in zip(holdings, settle(holdings).reached):
        for shape, tag in zip(holding.shapes, tags):
            hung = holding.owner is not None or _hung(shape, viewed)
            owner = holder if holding.owner is None else holding.owner
            if tag:
                kept = dataclasses.replace(shape, tag=tag)
                if holding.owner is None:
                    (connected if hung else unplaced).append((owner, kept))
                else:
                    (everywhere if holding.everywhere else own).append((owner, kept))
            elif shape.tag and hung and not holding.everywhere:
                lost.append((owner, shape))
    return ChainScene(read.catalog, read.own, read.chosen.owner.id.local_object_id, read.source, character.document,
                      holder, (*own, *everywhere, *connected), viewed, character.mesh_fields, boxes,
                      tuple(mesh.bone_remap_indices), meshes, skeletons, tuple(shape for _owner, shape in unplaced),
                      tuple(lost), holdings)


def read_set(files: GameFiles, game: str, profile, mesh, mesh_path: str, strands_path: str, prefab_path: str,
             lod: int, level: ChainLevel | None) -> SetRead:
    catalog = strand_catalog_for(profile)
    if catalog is None:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "REasy has no catalog of this build's strand chain."))
    if not int(mesh.joint_count):
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The strands are drawn over a mesh with bones only."))
    if not prefab_path:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "No prefab holding the strands' set was found."))
    hit = files.find(prefab_path, "")
    if hit is None:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The prefab {path} was not found."), path=prefab_path)
    owner, graph = hit[0], prefab_graph(files.registry, *hit, game)
    arguments, render = catalog.constants.arguments, catalog.render.arguments
    found = [item for item in prefab_sets(graph, catalog)
             if resource_key(item.render[render["strands"]]) == resource_key(strands_path)]
    if len(found) != 1:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The prefab holds {count} sets drawing {path}."),
                       count=len(found), path=strands_path)
    chosen, = found
    own = graph.documents[chosen.owner.id.document_id]
    if any(info.object_id == chosen.owner.id.local_object_id for info in own.rsz_file.gameobject_ref_infos):
        raise refusal(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The set's object references objects of its prefab, which REasy does not resolve."))
    spring = catalog.generation == "spring"
    if chosen.render[catalog.lod.fields["simulation"]] != catalog.states["simulation"]:
        raise refusal(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The strands are not driven by their dynamics, a path no capture shows."))
    if chosen.render[render["shading_quality"]] != catalog.states["shading_quality"]:
        raise refusal(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The strands' shading quality takes the guide shading path, which no capture shows."))

    def read(path: str) -> tuple[str, bytes]:
        found = files.find(path, owner)
        if found is None:
            raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The file {path} was not found."), path=path)
        return found
    placement = catalog.placement
    stamps = [own.components[key] for key in chosen.owner.components
              if own.components[key].type_name == placement.stamp_component]
    texture = plain_fields(own, stamps[0])[placement.stamp["texture"]] \
        if stamps and chosen.dynamics[placement.dynamics["stamp_mask"]] else ""
    mask = _texture_texels(read(texture)[1], "texture2d", None) if texture else _WHITE
    if mask is None:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The stamp's texture {path} is not a 2D texture."),
                       path=texture)
    strands = StrandsFile()
    strands.read(read(chosen.render[render["strands"]])[1])
    lod_settings = catalog.render.lod_settings
    settings = None if lod_settings is None else files.find(lod_settings, "")
    record = None if settings is None else lod_records(settings[1]).get(strands.preset_name_hash)
    binding = None
    if chosen.dynamics[arguments["binding"]]:
        bindings = StrandBindings.read(read(chosen.dynamics[arguments["binding"]])[1])
        bound_path = bound_mesh_path(bindings, catalog, graph, strands_path)
        if resource_key(bound_path) != resource_key(mesh_path):
            raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The strands are bound to the mesh {path}; view it "
                                                                "to draw them."), path=bound_path)
        binding = _spring_binding(bindings, mesh, lod, level) if spring else \
            _binding(catalog, chosen.dynamics, bindings, mesh, lod, level, profile)
    master, material = None, None
    if chosen.render[render["material"]]:
        path, data = read(chosen.render[render["material"]])
        mdf = MdfFile()
        mdf.read(data, path)
        if len(mdf.materials) != 1:
            raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The strands' material file {path} holds {count} "
                                                                "materials."), path=path, count=len(mdf.materials))
        drawn, = mdf.materials
        master, material = drawn.header.mmtr_path, PreviewMaterial.from_mdf(drawn)

    def preset(path: str) -> GpuStrandsPreset | None:
        found = files.find(path, owner)
        return None if found is None else GpuStrandsPreset.read(found[1], gpus_version(found[0]))

    def load(path: str) -> bytes | None:
        found = files.find(path, owner)
        if found is None:
            return None
        return gpu_buffer_contents(*found) if material is not None and path in material.buffers.values() else found[1]
    source = SetSource(chosen.render, chosen.dynamics, strands, first_lod(strands, catalog.lod.limit), record,
                       binding, master, material, preset, load, mask)
    return SetRead(catalog, owner, graph, chosen, own, source, bool(chosen.dynamics[arguments["binding"]]))


def read_character(files: GameFiles, read: SetRead, choice: tuple[str, object, tuple[object, object, dict]],
                   lod: int) -> CharacterRead:
    catalog = read.catalog
    character, characters, (document, holder, held) = choice
    mesh_fields = {name: held[name] for name in {*catalog.lod.mesh_fields.values(), catalog.placement.mesh["lod_mode"],
                                                 catalog.placement.mesh["lod_level"]}}
    if read.bound:
        mode, fixed = (mesh_fields[catalog.placement.mesh[name]] for name in ("lod_mode", "lod_level"))
        if mode == catalog.states["fixed_lod_mode"] and fixed != lod:
            raise refusal(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The mesh's component fixes its LOD at {level}; view that LOD to draw the "
                                   "strands."), level=fixed)
    return CharacterRead(character, characters, document, holder, mesh_fields,
                         _tree_holdings(files, catalog, document, holder, character))


def _binding(catalog: StrandCatalog, dynamics: Mapping[str, object], bindings: StrandBindings, mesh, lod: int,
             level: ChainLevel | None, profile) -> Binding:
    program = _skinning_program(catalog, dynamics, bindings, mesh, lod, level)
    plain = program != catalog.skinning.program
    # Path B reads the LOD's vertex buffer whether the passes deform it or not.
    held = chain_level(mesh, lod, pretransform_catalog_for(profile), deformed_only=False) \
        if plain and level is None else level
    if held is None:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The binding mesh has no submeshes at this LOD."))
    return Binding(bindings.tables[lod], bindings.quantized, held.source, _groups(mesh, lod, held), held.base,
                   held.weights_offset, held.uv_offset, tuple(mesh.bone_remap_indices),
                   np.asarray(mesh.inverse_bind_matrices[:int(mesh.joint_count)], np.float32).reshape(-1, 4, 4),
                   program, held.position_offset if plain else None)


def _spring_binding(bindings: StrandBindings, mesh, lod: int, level: ChainLevel | None) -> Binding:
    if level is None:
        raise refusal(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The renderer skins the binding mesh's LOD as it draws it, leaving no positions for the "
                               "strands' binding, which no capture shows."))
    if lod >= len(bindings.tables):
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The binding file holds no table of LOD {level}, where "
                                                            "the game leaves the binding mesh unskinned."), level=lod)
    return Binding(bindings.tables[lod], bindings.quantized, level.source, _groups(mesh, lod, level), level.base,
                   level.weights_offset, level.uv_offset, tuple(mesh.bone_remap_indices),
                   np.asarray(mesh.inverse_bind_matrices[:int(mesh.joint_count)], np.float32).reshape(-1, 4, 4),
                   "", None)


def _skinning_program(catalog: StrandCatalog, dynamics: Mapping[str, object], bindings: StrandBindings, mesh,
                      lod: int, level: ChainLevel | None) -> str:
    """Path A or B: see docs/internals/strands-rendering.md."""
    sources = catalog.constants
    values = {name: dynamics[field] for name, field in sources.copies.items()} | dict(sources.literals)
    if not values[_INTERNAL_SKINNING]:
        raise refusal(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The strands' binding reads the mesh's own deformed positions, which no capture "
                               "shows."))
    if lod >= len(bindings.tables):
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The binding file holds no table of LOD {level}, where "
                                                            "the game leaves the binding mesh unskinned."), level=lod)
    flags, section = mesh.header.decoded_content_flags, catalog.skinning
    unseen = refusal(QT_TRANSLATE_NOOP(
        "MaterialPreview", "The game skins the binding mesh at this LOD with programs no capture shows."))
    if flags is None or not flags.is_skinning or (level is not None and not level.skinned):
        raise unseen
    if flags.has_blend_shape and level is not None:
        if level.blend is None:
            raise refusal(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The binding mesh has no blend shapes at this LOD, where no capture shows what the "
                                   "game's binding reads."))
        return section.program
    program = section.plain.get(int(mesh.meshes[0].skin_weight_count_code))
    if program is None:
        raise unseen
    return program


def _groups(mesh, lod: int, level: ChainLevel) -> tuple[tuple[int, int], ...]:
    runs: dict[int, list] = {}
    for part in level.submeshes:
        runs.setdefault(part.group, []).append(part)
    out = []
    for group in mesh.meshes[0].lods[lod].mesh_groups:
        parts = sorted(runs[int(group.group_id)], key=lambda part: part.first)
        if any(part.first + part.count != after.first for part, after in zip(parts, parts[1:])):
            raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "A mesh group of the binding mesh's LOD holds "
                                                                "vertices apart."))
        out.append((parts[0].first, sum(part.count for part in parts)))
    return tuple(out)


def with_files(files: GameFiles, held: Mapping[str, tuple[str, bytes]]) -> GameFiles:
    if not held:
        return files

    def find(path: str, owner: str):
        return held.get(resource_key(path)) or files.find(path, owner)
    return dataclasses.replace(files, find=find)


def handler_files(handler, game: str) -> GameFiles:
    def mesh(path: str, data: bytes, light: bool = False):
        return MeshHandler.from_bytes(path, data, resource_context=resource_context_for_handler(handler),
                                      game_version=getattr(handler, "game_version", ""),
                                      load_mode=MeshLoadMode.LIGHT if light else MeshLoadMode.FULL).mesh
    def nearby(extension: str, source: str, beside: bool = False) -> tuple[str, ...]:
        return nearby_resource_paths(handler, extension, source_path=source, parent_levels=1,
                                     child_levels=1 if beside else 0)
    return GameFiles(lambda path, owner: resolve_companion_resource(handler, path, owner), nearby,
                     _registry(handler, game), mesh)


__all__ = ["ChainScene", "CharacterRead", "GameFiles", "Holding", "SetRead", "Settled", "character_candidates",
           "character_prefab", "chosen_character", "gather", "handler_files", "read_character", "read_set", "settle",
           "shader_paths", "with_files"]
