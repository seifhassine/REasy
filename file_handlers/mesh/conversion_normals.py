from dataclasses import replace
import hashlib
import struct

lazy import numpy as np

lazy from .normal_recalc import (
    INDICES_PER_WORKGROUP, VERTEX_INDEX_BITS, VERTEX_INDEX_MASK,
    NormalRecalcLOD, NormalRecalcSubmesh, NormalRecalcWorkItem,
    derive_dmc5_normal_indices, derive_re8_normal_indices,
    derive_sf6_normal_indices,
)
from .profiles import NormalRecalcLayout


WORK_ASSIGNMENT_LOSS = "Target changes native normal-compute work assignments; animated shading may differ"
PARTITION_WORK_LOSS = ("Rebuilt overflowed or aliased source normal-compute batches for partitioning; "
                       "animated shading may differ")


def normal_work_needs_rebuild(mesh, *, check_slots=True):
    data = mesh.normal_recalc_data
    if data is None:
        return False
    lods = data.lods or (NormalRecalcLOD(0, 0, 0, 0, (), (), redirects=data.redirects),)
    for lod in lods:
        draws = _draws(mesh, lod.lod_index)
        topology = _topology(mesh, draws)
        redirects = np.asarray([r.target_vertex for r in lod.redirects or data.redirects])
        words = np.asarray(_face_words(mesh, draws, lod.lod_index), dtype=np.uint32)
        cursor = 0
        for draw in topology:
            end = cursor + len(draw.indices)
            expected = redirects[np.asarray(draw.indices, dtype=int) + draw.base_vertex]
            if not np.array_equal(words[cursor:end] & VERTEX_INDEX_MASK, expected):
                return True
            if check_slots:
                for start in range(cursor, end, INDICES_PER_WORKGROUP):
                    pairs = np.unique(words[start:min(start + INDICES_PER_WORKGROUP, end)])
                    if len(np.unique(pairs >> VERTEX_INDEX_BITS)) != len(pairs):
                        return True
            cursor = end + len(draw.indices) % 2
    return False


def _draws(mesh, index):
    return [*group.submeshes for group in mesh.meshes[0].lods[index].mesh_groups]


def _topology(mesh, draws):
    base = min(draw.verts_index_offset for draw in draws)
    result = []
    for draw in draws:
        payload = mesh.mesh_buffer.buffer_payloads[draw.buffer_index]
        faces = payload.index_values
        indices = tuple(int(value) for value in faces[draw.faces_index_offset:draw.faces_index_offset + draw.indices_count])
        result.append(NormalRecalcSubmesh(draw.verts_index_offset - base, indices, draw.vert_count))
    return result


def _face_words(mesh, draws, index):
    data = mesh.normal_recalc_data
    lod = next((lod for lod in data.lods if lod.lod_index == index), None)
    if lod is not None and lod.face_entries:
        return lod.face_entries
    topology = _topology(mesh, draws)
    layout = mesh.profile.normal_recalc_layout
    if layout is NormalRecalcLayout.DMC5:
        return derive_dmc5_normal_indices(data, topology)
    if layout is NormalRecalcLayout.RE8:
        return derive_re8_normal_indices(data, topology)
    return derive_sf6_normal_indices(data, topology, index)


def materialize_normal_work(mesh):
    data = mesh.normal_recalc_data
    if data is None:
        return
    leading = next((lod for lod in data.lods if lod.lod_index == 0), None)
    if leading is None or not leading.work_items:
        draws = _draws(mesh, 0)
        words = _face_words(mesh, draws, 0)
        items, cursor = [], 0
        for draw in draws:
            items.extend(NormalRecalcWorkItem(word & VERTEX_INDEX_MASK, word >> VERTEX_INDEX_BITS)
                         for word in words[cursor:cursor + draw.indices_count])
            cursor += draw.indices_count + draw.indices_count % 2
        if leading is None:
            leading = NormalRecalcLOD(0, 0, 0, 0, (), (), redirects=data.redirects)
        leading = replace(leading, face_entries=words, work_items=tuple(items))
    mesh.normal_recalc_data = replace(data, legacy_profile=None,
        lods=(leading, *(lod for lod in data.lods if lod.lod_index != 0)))


def reflect_normal_work(mesh):
    data = mesh.normal_recalc_data
    if data is None or not data.lods:
        return
    def reverse(values, draws, padded):
        if not values:
            return values
        result, cursor = [], 0
        for draw in draws:
            end = cursor + draw.indices_count
            corners = values[cursor:end]
            result.extend(value for triangle in zip(corners[::3], corners[2::3], corners[1::3]) for value in triangle)
            padding = draw.indices_count % 2 if padded else 0
            result.extend(values[end:end + padding])
            cursor = end + padding
        if cursor != len(values):
            raise ValueError("Normal-compute work table does not match draw topology")
        return tuple(result)

    mesh.normal_recalc_data = replace(data, lods=tuple(replace(lod, **{
        field: reverse(getattr(lod, field), _draws(mesh, lod.lod_index), field != "work_items")
        for field in ("face_entries", "work_items", "vertex_page_entries")
    }) for lod in data.lods))


def normal_work_signatures(mesh):
    data = mesh.normal_recalc_data
    if data is None:
        return {}
    lods = {lod.lod_index: lod for lod in data.lods}
    signatures = {}
    for index in sorted(lods) if lods else range(data.declared_lod_count or 1):
        draws = _draws(mesh, index)
        words = _face_words(mesh, draws, index)
        words = np.asarray(words, dtype=np.uint32)
        digest, cursor = hashlib.sha256(), 0
        for draw in draws:
            end = cursor + draw.indices_count
            for start in range(cursor, end, INDICES_PER_WORKGROUP):
                batch = words[start:min(start + INDICES_PER_WORKGROUP, end)]
                _, first, inverse = np.unique(batch >> VERTEX_INDEX_BITS, return_index=True, return_inverse=True)
                digest.update(struct.pack("<I", len(batch)))
                digest.update((batch & VERTEX_INDEX_MASK).astype("<u4").tobytes())
                digest.update(first[inverse].astype("<u4").tobytes())
            cursor = end + draw.indices_count % 2
        if cursor != len(words):
            raise ValueError("Normal-compute work table does not match draw topology")
        signatures[index] = digest.digest()
    return signatures
