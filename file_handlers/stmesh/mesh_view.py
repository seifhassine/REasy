from __future__ import annotations

from array import array

lazy import numpy as np

lazy from file_handlers.mesh.mesh_file import MESH_MAGIC, MeshBuffer, MeshBufferPayload, MeshContentFlags, MeshData, \
    MeshFile, MeshGroup, MeshLOD, Submesh
lazy from file_handlers.mesh.profiles import MeshGameProfile

lazy from .geometry import DecodedVertices, decode_billboards, decode_chunk, decode_geometry, draws
lazy from .stmesh_file import BufferLayout, StmeshFile

_SUBMESH_VERTICES = 0x10000


def _sources(mesh: StmeshFile) -> dict[object, DecodedVertices]:
    sources: dict[object, DecodedVertices] = {}
    if mesh.geometry is not None:
        sources["3d"] = decode_geometry(mesh.geometry, mesh.layout is BufferLayout.LATE)
    if mesh.billboards is not None:
        sources["billboards"] = decode_billboards(mesh.billboards)
    if mesh.meshlets is not None:
        for lod, entry in enumerate(mesh.meshlets.lods):
            for number, chunk in enumerate(entry.chunks):
                sources["chunk", lod, number] = decode_chunk(chunk)
    return sources


def stmesh_mesh(mesh: StmeshFile,
                profile: MeshGameProfile) -> tuple[MeshFile, dict[Submesh, int | tuple[tuple[int, int], ...]]]:
    """Second value per submesh: its cluster number in the LOD, or its meshlet chunk run as (LOD, number) pairs."""
    sources = _sources(mesh)
    bases, at = {}, 0
    for key, vertices in sources.items():
        bases[key] = at
        at += vertices.count
    payload = MeshBufferPayload(vertex_count=at, integer_faces=array("I"))
    if sources:
        joined = {name: np.concatenate([getattr(vertices, name) for vertices in sources.values()])
                  for name in ("positions", "texcoords", "normals", "tangents", "colors")}
        payload.positions.extend(joined["positions"].ravel().tolist())
        payload.normals.extend(joined["normals"].ravel().tolist())
        payload.tangents.extend(joined["tangents"][:, :3].ravel().tolist())
        payload.tangent_ws.extend(((joined["tangents"][:, 3] < 0) * 0x81 + (joined["tangents"][:, 3] >= 0) * 0x7f)
                                  .astype(np.uint8).tolist())
        payload.normal_ws.extend([0x7f] * at)
        payload.uv0.extend(joined["texcoords"].ravel().astype(np.float64).tolist())
        payload.colors.extend(joined["colors"].ravel().tolist())

    buffer = MeshBuffer(profile)
    buffer.buffer_payloads = {0: payload}
    data = MeshData(buffer)
    lods: dict[int, MeshLOD] = {}
    clusters: dict[Submesh, int] = {}
    runs: dict[Submesh, list[tuple[int, int]]] = {}
    numbers: dict[int, int] = {}
    for lod, material, _batch, key, triangles in draws(mesh):
        entry = lods.get(lod)
        if entry is None:
            entry = lods[lod] = MeshLOD(buffer)
            entry.lod_level = lod
            entry.mesh_groups = [MeshGroup(buffer)]
        group = entry.mesh_groups[0]
        base = bases[key]
        if key in ("3d", "billboards") and len(triangles):
            low = int(triangles.min())
            vertex_start, local = base + low, triangles - low
            span = int(triangles.max()) - low + 1
        else:
            vertex_start, local, span = base, triangles, sources[key].count if key in sources else 0
        submesh = group.submeshes[-1] if group.submeshes and isinstance(key, tuple) else None
        if (submesh is None or submesh.material_index != material
                or vertex_start != submesh.verts_index_offset + submesh.vert_count
                or submesh.vert_count + span > _SUBMESH_VERTICES):
            submesh = Submesh(buffer)
            submesh.material_index = material
            submesh.verts_index_offset = vertex_start
            submesh.faces_index_offset = len(payload.integer_faces)
            group.submeshes.append(submesh)
        if not isinstance(key, tuple):
            clusters[submesh] = numbers.get(lod, 0)
            numbers[lod] = clusters[submesh] + 1
        else:
            runs.setdefault(submesh, []).append(key[1:])
        payload.integer_faces.extend((local + (vertex_start - submesh.verts_index_offset)).ravel().tolist())
        submesh.vert_count = max(submesh.vert_count, vertex_start - submesh.verts_index_offset + span)
        submesh.indices_count += local.size
        group.vertex_count += span
    data.lods = [lods[number] for number in sorted(lods)]
    for entry in data.lods:
        entry.part_count = len(entry.mesh_groups)
    low, high = (np.float32(value) for value in mesh.bounds)
    data.bounding_box = ((*map(float, low), 0.0), (*map(float, high), 0.0))
    center = (low + high) * np.float32(0.5)
    data.bounding_sphere = (*map(float, center), float(np.linalg.norm(high - center)))
    data.material_count = len(mesh.material_names)
    data.uv_count = 1

    view = MeshFile()
    view.header.magic = MESH_MAGIC
    view.header.profile = profile
    # Not skinned: SpeedTree meshes have no joints.
    view.header.decoded_content_flags = MeshContentFlags.from_raw(0, profile)
    view.mesh_buffer = buffer
    view.meshes = [data]
    view.material_names = list(mesh.material_names)
    view.material_indices = list(range(len(mesh.material_names)))
    return view, {**clusters, **{submesh: tuple(chunks) for submesh, chunks in runs.items()}}


__all__ = ["stmesh_mesh"]
