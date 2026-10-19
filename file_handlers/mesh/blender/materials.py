"""See docs/mesh-conversion.md, Blender round trip."""

from __future__ import annotations

from ..mesh_file import MPLY_MAGIC
from .scene import LAYERS, layer_lods


def compact_names(mesh):
    names, tables = [], (mesh.material_indices, mesh.bone_indices, mesh.blend_shape_indices)
    if mesh.header.magic == MPLY_MAGIC:
        # MPLY material names lead the table by position; its material index table is an order.
        names, tables = list(mesh.material_names), (mesh.bone_indices, mesh.blend_shape_indices)
    for table in tables:
        for position, index in enumerate(table):
            table[position] = len(names)
            names.append(mesh.names[index])
    mesh.names = names


def _submeshes(mesh):
    """LODs and layers may share submesh objects."""
    return list({id(submesh): submesh for layer in LAYERS for _mesh, _lod, lod in layer_lods(mesh, layer)
                 for group in getattr(lod, "mesh_groups", ()) for submesh in group.submeshes}.values())


def _set_count(mesh, count):
    for data in mesh.meshes:
        data.material_count = count
    if mesh.shadow_mesh is not None:
        mesh.shadow_mesh.material_count = count


def normalize_mply(mesh, original_names):
    chunks = mesh.mesh_buffer.mply_chunks
    used = sorted({int(chunk.material_id) for chunk in chunks})
    if mesh.material_names == original_names and len(used) == len(mesh.material_names):
        return None
    dropped = [name for index, name in enumerate(mesh.material_names) if index not in used]
    names = sorted({mesh.material_names[index] for index in used})
    rank = {name: position for position, name in enumerate(names)}
    remap = {index: rank[mesh.material_names[index]] for index in used}
    for chunk in chunks:
        chunk.material_id = remap[int(chunk.material_id)]
    for submesh in _submeshes(mesh):
        submesh.material_index = remap.get(int(submesh.material_index), 0)
    mesh.material_names = names
    mesh.material_indices = list(dict.fromkeys(int(chunk.material_id) for chunk in chunks))
    _set_count(mesh, len(names))
    compact_names(mesh)
    return dropped


__all__ = ["compact_names", "normalize_mply"]
