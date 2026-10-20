"""See docs/mesh-conversion.md, Models from other sources."""

from __future__ import annotations

import re
from dataclasses import dataclass

lazy import numpy as np

from ..canonical_writer import serialize_mesh
from .export import joint_names
from .mapping import ADD, DROP, MERGE, Bone
lazy from .materials import compact_names
from .package import from_blender, matrix_to_blender
from .rebuild import BlendImportError, parse_source, rebuild_from_dump
lazy from .scene import build_scene

LAYER_LABELS = {"main": "Main", "shadow": "Shadow", "occluder": "Occluder"}
LOD_TAG = re.compile(r"lod\s*(\d+)\s*$", re.IGNORECASE)


@dataclass
class Reference:
    main: bytes
    streaming: bytes | None
    file_version: int
    label: str = ""

    @classmethod
    def from_mesh(cls, mesh, file_version, label=""):
        encoded = serialize_mesh(mesh)
        return cls(encoded.main, encoded.streaming, int(file_version), label)

    def parse(self):
        return parse_source(self.main, self.streaming, self.file_version)


def reference_bones(mesh):
    names = joint_names(mesh)
    return [Bone(names[joint], names[node.parent_index] if 0 <= node.parent_index < len(names) else "",
                 tuple(np.asarray(mesh.world_matrices[joint], np.float64).reshape(4, 4)[3, :3]))
            for joint, node in enumerate(mesh.bones)]


def _armatures_in_use(manifest):
    used = {item.get("parent", "") for item in manifest["objects"]}
    used.update(target for item in manifest["objects"] for _name, kind, target in item.get("modifiers", [])
                if kind == "ARMATURE")
    armatures = manifest.get("armatures", [])
    return [a for a in armatures if a["name"] in used] or list(armatures)


def _bone_world(armature, bone):
    world = np.asarray(armature["matrix"], np.float64).reshape(4, 4)
    return world @ np.asarray(bone["matrix"], np.float64).reshape(4, 4)


def source_bones(dump):
    bones = {}
    for armature in _armatures_in_use(dump.manifest):
        for bone in armature["bones"]:
            head = from_blender(_bone_world(armature, bone)[:3, 3])[0]
            bones.setdefault(bone["name"], Bone(bone["name"], bone["parent"], tuple(map(float, head))))
    weighted = {}
    for number, item in enumerate(dump.manifest["objects"]):
        groups = item.get("groups", [])
        indices = dump.arrays.get(f"o{number}.wg", np.zeros(0, np.int32)).astype(np.int64)
        vertices = dump.arrays.get(f"o{number}.wv", np.zeros(0, np.int32)).astype(np.int64)
        valid = dump.arrays.get(f"o{number}.ww", np.zeros(0, np.float32)) > 0
        stride = int(vertices.max(initial=0)) + 1
        counts = np.bincount(np.unique(indices[valid] * stride + vertices[valid]) // stride, minlength=len(groups))
        for group, name in enumerate(groups):
            weighted[name] = weighted.get(name, 0) + int(counts[group])
            bones.setdefault(name, Bone(name))
    return _parents_first(bones), weighted


def _parents_first(bones):
    ordered, placed = [], set()

    def place(bone, trail):
        if bone.name in placed or bone.name in trail:
            return
        if bone.parent in bones:
            place(bones[bone.parent], trail | {bone.name})
        placed.add(bone.name)
        ordered.append(bone)

    for bone in bones.values():
        place(bone, frozenset())
    return ordered


def _orthonormal(matrix):
    """Engine joints are rigid. ``matrix`` is in Blender's column-vector form, so translation sits in column 3 and is
    kept."""
    result = np.array(matrix, np.float64).reshape(4, 4)
    u, _s, vt = np.linalg.svd(result[:3, :3])
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        rotation[:, 0] = -rotation[:, 0]
    result[:3, :3], result[3] = rotation, (0, 0, 0, 1)
    return result


def _reference_armature(mesh, mapping, source_armatures):
    names = joint_names(mesh)
    bones = []
    for bone in reference_bones(mesh):
        joint = len(bones)
        rest = matrix_to_blender(mesh.world_matrices[joint]).ravel().tolist()
        bones.append({"name": bone.name, "parent": bone.parent, "matrix": rest, "rest": rest, "joint": joint,
                      "deform": True, "exported": bone.name})
    source = {bone["name"]: (armature, bone) for armature in source_armatures for bone in armature["bones"]}
    used, added = set(names), {}
    for row in mapping.rows:
        if row.mapped or row.action != ADD:
            continue
        parent = next(filter(None, (_joint_of(a, mapping, added) for a in mapping.ancestors(row.source))), "")
        if row.source in source:
            rest = _orthonormal(_bone_world(*source[row.source]))
        elif parent in names:
            # A bare vertex group has no rest pose of its own; it starts at its parent joint.
            rest = matrix_to_blender(mesh.world_matrices[names.index(parent)])
        else:
            rest = np.eye(4)
        name = added[row.source] = _unique(row.source, used)
        bones.append({"name": name, "parent": parent, "matrix": rest.ravel().tolist(), "rest": [], "joint": -1,
                      "deform": True, "exported": name})
    return {"name": "reference", "matrix": np.eye(4).ravel().tolist(), "bones": bones}, added


def _unique(name, used):
    candidate, number = name, 1
    while candidate in used:
        number += 1
        candidate = f"{name}.{number}"
    used.add(candidate)
    return candidate


def _joint_of(name, mapping, added):
    row = mapping.row(name)
    return row.target if row is not None and row.mapped else added.get(name)


def _group_target(name, mapping, added):
    row = mapping.row(name)
    if row is None or (not row.mapped and row.action == DROP):
        return None
    if not row.mapped and row.action == MERGE:
        return mapping.merge_target(name) or None
    return _joint_of(name, mapping, added)


def _layout(mesh):
    scene = build_scene(mesh, merge_seams=False)
    keys = sorted({(item.layer, item.lod, item.mesh_index) for item in scene.objects if not item.alias_of},
                  key=lambda key: (list(LAYER_LABELS).index(key[0]), key[2], key[1]))
    return [{"name": f"{LAYER_LABELS[layer]} LOD{lod}" + (f" · {index}" if index else ""), "layer": layer,
             "lod": lod, "mesh_index": index} for layer, lod, index in keys]


def lod_tag(collection):
    match = LOD_TAG.search(collection or "")
    return int(match.group(1)) if match else None


def _lod_objects(manifest, collections):
    tagged, untagged = {}, []
    for number, item in enumerate(manifest["objects"]):
        tag = lod_tag(item.get("collection", ""))
        (untagged if tag is None else tagged.setdefault(tag, [])).append(number)
    main_lods = {c["lod"] for c in collections if c["layer"] == "main"}
    chosen, changes, losses = {}, [], []
    for collection in collections:
        wanted = max(main_lods, default=0) if collection["layer"] == "occluder" else collection["lod"]
        lower = [tag for tag in tagged if tag <= wanted]
        source = max(lower) if lower else min(tagged, default=None)
        chosen[collection["name"]] = untagged + tagged.get(source, [])
        if tagged and source != wanted and collection["layer"] == "main":
            changes.append(f"Main LOD{wanted} has no objects of its own; it reuses LOD{source}'s")
    if not tagged and len(main_lods) > 1:
        changes.append(f"No collection names a LOD, so all {len(main_lods)} LODs use the full model; put lower-detail "
                       "versions in collections named LOD1, LOD2, … to keep distant draws cheap")
    unused = sorted(set(tagged) - main_lods)
    if unused:
        losses.append("The reference has no " + ", ".join(f"LOD{tag}" for tag in unused)
                      + "; objects tagged for it were ignored")
    return chosen, changes, losses


def _object_weights(number, item, arrays, mapping, added):
    targets = [_group_target(name, mapping, added) for name in item.get("groups", [])]
    names = sorted({target for target in targets if target is not None})
    group_of = np.array([names.index(t) if t is not None else -1 for t in targets], np.int64)
    groups = arrays.get(f"o{number}.wg", np.zeros(0, np.int32)).astype(np.int64)
    keep = group_of[groups] >= 0
    return (names, arrays.get(f"o{number}.wv", np.zeros(0, np.int32))[keep], group_of[groups[keep]].astype(np.int32),
            arrays.get(f"o{number}.ww", np.zeros(0, np.float32))[keep])


def scene_on_reference(dump, mesh, mapping, file_version):
    manifest = dump.manifest
    armature, added = _reference_armature(mesh, mapping, _armatures_in_use(manifest))
    collections = _layout(mesh)
    if not collections:
        raise BlendImportError("The reference mesh has no geometry to take a layout from")
    chosen, changes, losses = _lod_objects(manifest, collections)
    empty = next((name for name, objects in chosen.items() if not objects), None)
    if empty is not None:
        raise BlendImportError(f"No scene object is assigned to {empty}")
    weights = [_object_weights(n, item, dump.arrays, mapping, added) for n, item in enumerate(manifest["objects"])]
    arrays = {key: value for key, value in dump.arrays.items() if key.startswith("m")}  # shared by every copy
    objects = []
    for collection in collections:
        for number in chosen[collection["name"]]:
            names, *values = weights[number]
            own = f"o{len(objects)}."
            arrays[own + "wv"], arrays[own + "wg"], arrays[own + "ww"] = values
            objects.append(dict(manifest["objects"][number], collection=collection["name"], groups=names, props={}))
    # A static reference keeps the scene's own armatures, only so the import can say they were ignored.
    restated = dict(manifest, foreign=False, file_version=str(file_version), merge_seams=False,
                    collections=collections, objects=objects,
                    armatures=[armature] if mesh.bones else manifest.get("armatures", []), outside=[])
    losses += [f"{name} is hidden in Blender; ignored it" for name in manifest.get("hidden", [])]
    return restated, arrays, changes, losses


def reference_import(dump, reference, mapping, *, recompute_normals=False, skin_unweighted=False):
    if not dump.manifest.get("foreign"):
        raise BlendImportError("This scene was exported by REasy; import it onto its own mesh instead")
    if not dump.manifest["objects"]:
        raise BlendImportError("The scene has no visible mesh objects")
    base = reference.parse()
    # The reference's morphs describe geometry the import replaces.
    base.blend_shape_data, base.blend_shape_indices = None, []
    compact_names(base)
    stripped = serialize_mesh(base)
    manifest, arrays, changes, losses = scene_on_reference(dump, base, mapping, reference.file_version)
    result = rebuild_from_dump(manifest, arrays, stripped.main, stripped.streaming,
                               recompute_normals=recompute_normals, reference=True, skin_unweighted=skin_unweighted)
    result.changes[:0], result.losses[:0] = changes, losses
    model, native = _lod0_triangles(result.mesh), _lod0_triangles(base)
    if model > DENSE_TRIANGLES and model > DENSE_FACTOR * max(native, 1):
        result.losses.append(f"LOD0 has {model:,} triangles, {model // max(native, 1):,}× the reference's {native:,}; "
                             "the game may stutter or run out of memory. Decimate the model, or apply a lower "
                             "Multires/Subdivision level, before importing")
    return result


DENSE_TRIANGLES = 300_000  # over twice RE9's densest Leon/Grace part at LOD0 (136k triangles)
DENSE_FACTOR = 8


def _lod0_triangles(mesh):
    if mesh.mesh_buffer.mply_chunks:
        return sum(len(chunk.faces) // 3 for chunk in mesh.mesh_buffer.mply_chunks if chunk.lod_index == 0)
    if not mesh.meshes or not mesh.meshes[0].lods:
        return 0
    return sum(int(submesh.indices_count) // 3 for group in mesh.meshes[0].lods[0].mesh_groups
               for submesh in group.submeshes)


__all__ = ["Reference", "lod_tag", "reference_bones", "reference_import", "scene_on_reference", "source_bones"]
