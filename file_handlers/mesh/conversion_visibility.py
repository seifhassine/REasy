from array import array
from dataclasses import replace

import numpy as np

from .conversion_geometry import geometry_layers, mesh_payloads
from .conversion_skin import _material_layout
from .mesh_file import MESH_MAGIC
from .profiles import MaterialSkinningLayout


DISCARDED_SKINNING_LOSS = "Removed triangles discarded by the source's zero-total skinning"


def materialize_skinning_visibility(mesh, loss):
    flags = mesh.header.decoded_content_flags
    if (mesh.header.magic != MESH_MAGIC
            or _material_layout(mesh.profile) is not MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8
            or flags is not None and not flags.is_skinning):
        return
    dead = {}
    for payload in mesh_payloads(mesh):
        stream = payload.skin_weights
        if stream is not None:
            weights = np.asarray(stream.weights).reshape(stream.vertex_count, stream.influence_count)
            mask = np.all(weights == 0, axis=1)
            if mask.any():
                dead[id(payload)] = mask
    if not dead:
        return
    if mesh.normal_recalc_data is not None:
        raise ValueError("Zero-total skinning with normal-recalculation dependencies is not supported")
    if not mesh.bone_remap_indices:
        raise ValueError("Zero-total skinning has no deform palette for unused vertex storage")

    plans, owners, discarded = {}, {}, 0
    for _layer, groups, payloads in geometry_layers(mesh):
        for group in groups:
            for draw in group.submeshes:
                payload = payloads[draw.buffer_index]
                key = id(payload)
                previous = owners.get(id(draw))
                if previous is not None and previous != key and (previous in dead or key in dead):
                    raise ValueError("Shared source topology has different zero-weight vertex payloads")
                owners[id(draw)] = key
                if key not in dead or previous is not None:
                    continue
                faces = payload.index_values
                first, count = draw.faces_index_offset, draw.indices_count
                if min(first, count) < 0 or first + count > len(faces):
                    raise ValueError("Source skinning draw has an invalid index range")
                corners = np.asarray(faces[first:first + count], dtype=np.int64)
                width = 4 if draw.is_quad else 3
                primitives = corners.reshape(-1, width)
                addresses = primitives + draw.verts_index_offset
                if np.any(addresses < 0) or np.any(addresses >= payload.vertex_count):
                    raise ValueError("Source skinning draw references an invalid vertex")
                hidden = dead[key][addresses].any(axis=1)
                if draw.is_quad and hidden.any():
                    raise ValueError("Zero-total skinning on tessellated patches is not supported")
                discarded += int(hidden.sum())
                plans.setdefault(key, (payload, []))[1].append((draw, primitives[~hidden].ravel()))

    for payload, draws in plans.values():
        faces = array("I")
        for draw, corners in draws:
            draw.faces_index_offset = len(faces)
            draw.indices_count = len(corners)
            faces.extend(corners)
        payload.integer_faces = faces

    count = 0
    for payload in mesh_payloads(mesh):
        mask = dead.get(id(payload))
        if mask is None:
            continue
        stream = payload.skin_weights
        weights = np.asarray(stream.weights).reshape(stream.vertex_count, stream.influence_count).copy()
        indices = np.asarray(stream.deform_indices).reshape(weights.shape).copy()
        weights[mask, 0] = 1
        indices[mask] = 0
        payload.skin_weights = replace(stream, weights=array("f", weights.ravel()),
                                       deform_indices=array("H", indices.ravel()))
        count += int(mask.sum())
    if discarded:
        loss(f"{DISCARDED_SKINNING_LOSS} ({discarded}); retained vertex and morph storage")
    loss(f"Assigned inert weights to {count} unreferenced zero-weight vertices for target storage")
