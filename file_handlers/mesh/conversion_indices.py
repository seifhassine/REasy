from array import array

lazy import numpy as np

lazy from .contract_utils import layer_groups
lazy from .conversion_allocations import _rebased_draw, _select_payload, join_payloads, make_resident
lazy from .conversion_geometry import detach_topology, geometry_layers


def needs_draw_splitting(mesh, target):
    if target.canonical_ordinary_supports_32bit_indices:
        return False
    return any(np.max(_faces(payloads[s.buffer_index], s), initial=0) > 65535
               for _, groups, payloads in geometry_layers(mesh) for g in groups for s in g.submeshes)


def _faces(payload, draw):
    values = payload.index_values
    return np.asarray(values[draw.faces_index_offset:draw.faces_index_offset + draw.indices_count])


def split_large_draws(mesh, target, loss):
    if not needs_draw_splitting(mesh, target):
        return
    if mesh.blend_shape_data is not None or mesh.normal_recalc_data is not None:
        raise ValueError("Target needs 16-bit draw splitting with morph/normal vertex remapping; this combination is not supported")
    make_resident(mesh, loss)
    primary = {id(s) for data in mesh.meshes for lod in data.lods for g in lod.mesh_groups for s in g.submeshes}
    for layer, _, payloads in list(geometry_layers(mesh)):
        groups = layer_groups(layer, unique_lods=True)
        if layer in (mesh.shadow_mesh, mesh.occluder_mesh) and layer.shares_main_payload:
            if all(id(s) in primary for g in groups for s in g.submeshes):
                layer.payloads = mesh.mesh_buffer.buffer_payloads
                continue
            if target.ordinary_vertex_layout_has_no_auxiliary_elements:
                raise ValueError("Target cannot split partially shared shadow/occluder vertex allocations")
            detach_topology(layer, {id(mesh.mesh_buffer): mesh.mesh_buffer})
            groups = layer_groups(layer, unique_lods=True)
        parts, vertex_base, index_base = {}, 0, 0
        for group in groups:
            draws = []
            for draw in group.submeshes:
                payload = payloads[draw.buffer_index]
                indices = _faces(payload, draw)
                split = np.max(indices, initial=0) > 65535
                if split:
                    unused = draw.vert_count - len(np.unique(indices))
                    if unused:
                        loss(f"Removed {unused} unused vertices while splitting a 32-bit draw")
                width = 4 if draw.is_quad else 3
                step = (65535 // width) * width if split else max(1, len(indices))
                for start in range(0, max(1, len(indices)), step):
                    corners = indices[start:start + step]
                    vertices = np.unique(corners) if split else np.arange(draw.vert_count)
                    selected = vertices + draw.verts_index_offset
                    part = _select_payload(payload, selected, len(vertices))
                    part.integer_faces = array("I", np.searchsorted(vertices, corners) if split else corners)
                    draws.append(_rebased_draw(draw, vertex_base, index_base, len(vertices), len(corners)))
                    parts[len(parts)] = part
                    vertex_base += part.vertex_count
                    index_base += len(corners)
            if len(draws) > 255:
                raise ValueError("16-bit splitting exceeds the target's 255 draws per mesh group")
            group.submeshes = draws
            group.vertex_count = sum(s.vert_count for s in draws)
        payload, _ = join_payloads(parts, loss)
        if layer in mesh.meshes:
            mesh.mesh_buffer.buffer_payloads = {0: payload}
            primary = {id(s) for data in mesh.meshes for lod in data.lods
                       for g in lod.mesh_groups for s in g.submeshes}
        else:
            payload.is_auxiliary_geometry = True
            layer.shares_main_payload = False
            layer.payloads = {0: payload}
