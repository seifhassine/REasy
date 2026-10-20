from array import array
from copy import copy
from dataclasses import dataclass, replace

import numpy as np

lazy from .conversion_allocations import (
    _SEGMENT_PLANES, _rebased_draw, _select_payload, join_payloads, make_resident,
)
from .mesh_file import SKIN_WEIGHT_STREAMS
lazy from .normal_recalc import (
    INDICES_PER_WORKGROUP, WORKGROUP_SLOT_MASK, NormalRecalcData, NormalRecalcLOD, NormalRecalcRedirect,
)
lazy from .conversion import _clone

# Triangles per piece of a draw too dense for one normal work window: 1,020 indices fit its 1,024 slots.
DENSE_NORMAL_CHUNK_TRIANGLES = 340


@dataclass(frozen=True)
class PartGeometry:
    lod: int
    source_vertices: np.ndarray
    # Source group/draw, primitive numbers, and output group/draw.
    draws: tuple
    support_triangles: int = 0


def _active_bones(payload):
    columns = []
    for name in SKIN_WEIGHT_STREAMS:
        stream = getattr(payload, name)
        if stream is not None:
            indices = np.asarray(stream.deform_indices, dtype=np.int32).reshape(payload.vertex_count, -1)
            weights = np.asarray(stream.weights).reshape(indices.shape)
            columns.append(np.where(weights > 0, indices, -1))
    return np.concatenate(columns, axis=1) if columns else np.empty((payload.vertex_count, 0), dtype=int)


class _Domain:
    def __init__(self, mesh, lod_index):
        self.lod_index = lod_index
        self.lod = mesh.meshes[0].lods[lod_index]
        self.payload = mesh.mesh_buffer.buffer_payloads[0]
        self.draws = [(gi, di, g, d) for gi, g in enumerate(self.lod.mesh_groups)
                      for di, d in enumerate(g.submeshes)]
        self.base = min(d.verts_index_offset for _, _, _, d in self.draws)
        faces = np.asarray(self.payload.index_values)
        self.primitives = [faces[d.faces_index_offset:d.faces_index_offset + d.indices_count].astype(np.int64)
                           .reshape(-1, 4 if d.is_quad else 3) + d.verts_index_offset
                           for _, _, _, d in self.draws]
        self.bones = _active_bones(self.payload)
        self.normal = None
        data = mesh.normal_recalc_data
        if data is not None:
            self.normal = next((n for n in data.lods if n.lod_index == lod_index), None)
            if self.normal is None and lod_index == 0:
                self.normal = NormalRecalcLOD(0, 0, 0, 0, (), (), redirects=data.redirects)
            elif self.normal is not None and not self.normal.redirects and lod_index == 0:
                self.normal = replace(self.normal, redirects=data.redirects)
        self.redirects = (np.array([r.target_vertex for r in self.normal.redirects], dtype=np.int64) + self.base
                          if self.normal else None)
        self.offsets = np.cumsum([0, *map(len, self.primitives)])
        if self.normal:
            if any(p.shape[1] != 3 for p in self.primitives):
                raise ValueError("Normal-preserving partitioning requires triangle topology")
            self.compute = self.redirects[np.concatenate(self.primitives) - self.base]
        self.required = np.array([self.base], dtype=np.int64)
        blend = mesh.blend_shape_data
        if lod_index == 0 and blend and blend.bone_modifier:
            self.required = np.union1d(self.required, [*e.vertex_indices for e in blend.bone_modifier.effectors])

    def closure(self, selection):
        rendered = np.unique(np.concatenate([self.primitives[di][ids].ravel() for di, ids in selection]))
        needed = np.union1d(rendered, self.required)
        neighbors = np.empty(0, dtype=np.int64)
        if self.normal:
            targets = self.redirects[rendered - self.base]
            neighbors = np.flatnonzero(np.isin(self.compute, targets).any(axis=1))
            needed = np.union1d(needed, np.union1d(targets, self.compute[neighbors].ravel()))
        bones = set(np.unique(self.bones[needed]).tolist()) - {-1}
        return needed, neighbors, bones


def _morphs(part, mappings):
    blend = part.blend_shape_data
    if blend is None:
        return
    for body in blend.bodies:
        origin, base = mappings[body.lod_index]
        body.alias_of_lod = None
        ranges = {}
        for ti, target in enumerate((*body.targets, *body.none_targets)):
            if not target.ranges:
                continue
            rebuilt = []
            for ri, item in enumerate(target.ranges):
                selected = np.flatnonzero((origin >= item.base_vertex_location)
                                          & (origin < item.base_vertex_location + item.vertex_count))
                runs = np.split(selected, np.flatnonzero(np.diff(selected) != 1) + 1) if len(selected) else []
                ranges[ti, ri] = [(len(rebuilt) + i, run) for i, run in enumerate(runs)]
                for run in runs:
                    rebuilt.append(replace(item, base_vertex_location=base + int(run[0]),
                                           vertex_count=len(run), blend_shape_location=0))
            target.ranges = rebuilt or [replace(target.ranges[0], base_vertex_location=base,
                                               vertex_count=int(target.is_blend_shape and target.channel_count > 0),
                                               blend_shape_location=0)]
            target.aabb = None
        for channel in body.channels:
            rebuilt = []
            for segment in channel.segments:
                for ri, run in ranges[segment.target_index, segment.range_index]:
                    rows = origin[run] - segment.base_vertex_location
                    values = {name: None if (value := getattr(segment, name)) is None else value[rows].copy()
                              for name in _SEGMENT_PLANES}
                    rebuilt.append(replace(segment, range_index=ri, buffer_index=0,
                                           base_vertex_location=base + int(run[0]), **values))
            if not rebuilt:
                for segment in channel.segments[:1]:
                    values = {name: None if (value := getattr(segment, name)) is None
                              else np.zeros((1, *value.shape[1:]), dtype=value.dtype)
                              for name in _SEGMENT_PLANES}
                    if values["target_normals"] is not None:
                        values["target_normals"] = np.asarray(part.mesh_buffer.buffer_payloads[0].normals).reshape(-1, 3)[base:base + 1].copy()
                    rebuilt.append(replace(segment, range_index=0, buffer_index=0,
                                           base_vertex_location=base, **values))
            channel.segments = rebuilt
    if blend.bone_modifier:
        origin, base = mappings[0]
        lookup = {int(old): base + i for i, old in reversed(list(enumerate(origin)))}
        blend.bone_modifier.effectors = [replace(e, vertex_indices=tuple(lookup[v] for v in e.vertex_indices))
                                         for e in blend.bone_modifier.effectors]


def _build_part(mesh, domains, selections, loss):
    part = _clone(mesh)
    payloads, normals, geometry, mappings = {}, [], [], {}
    vertex_base = face_base = 0
    for domain, selected in zip(domains, selections):
        lod = copy(domain.lod)
        lod.buffer = part.mesh_buffer
        lod.mesh_groups = []
        part.meshes[0].lods[domain.lod_index] = lod
        needed, neighbors, _bones = domain.closure(selected) if selected else (domain.required, np.empty(0, dtype=int), set())
        origins, faces, draws, redirects = [], [], [], []
        rendered_ids = []
        groups = {}
        draw_chunks = []
        for di, ids in selected:
            if domain.normal:
                canonical = domain.redirects[domain.primitives[di][ids].ravel() - domain.base]
                dense = any(len(np.unique(canonical[i:i + INDICES_PER_WORKGROUP])) > WORKGROUP_SLOT_MASK + 1
                            for i in range(0, len(canonical), INDICES_PER_WORKGROUP))
                if dense:
                    draw_chunks.extend((di, ids[i:i + DENSE_NORMAL_CHUNK_TRIANGLES])
                                       for i in range(0, len(ids), DENSE_NORMAL_CHUNK_TRIANGLES))
                    continue
            draw_chunks.append((di, ids))
        for di, ids in draw_chunks:
            gi, si, group, draw = domain.draws[di]
            if gi not in groups:
                groups[gi] = _new_group(group, lod, part.mesh_buffer)
            output_group = groups[gi]
            vertices, indices = np.unique(domain.primitives[di][ids].ravel(), return_inverse=True)
            new = _rebased_draw(draw, vertex_base + len(origins), face_base + len(faces), len(vertices), len(indices))
            new.buffer = part.mesh_buffer
            draws.append((gi, si, ids, lod.mesh_groups.index(output_group), len(output_group.submeshes)))
            output_group.submeshes.append(new)
            output_group.vertex_count += len(vertices)
            origins.extend(vertices.tolist())
            faces.extend(indices.tolist())
            if domain.normal:
                redirects.extend(domain.redirects[vertices - domain.base].tolist())
            rendered_ids.extend((ids + domain.offsets[di]).tolist())
        support = np.setdiff1d(neighbors, rendered_ids)
        missing = np.setdiff1d(needed, origins)
        # Also retain a legal empty LOD in parts which render only other LODs.
        if not origins:
            missing = np.union1d(missing, [domain.base])
        if len(missing) or len(support) or not origins:
            template = domain.draws[0]
            # Appended vertices must stay after every earlier group's span.
            group = lod.mesh_groups[-1] if lod.mesh_groups and lod.mesh_groups[-1] is groups.get(template[0]) else None
            if group is None:
                group = _new_group(template[2], lod, part.mesh_buffer)
            # Small batches avoid overflowing the compute shader's 1,024 slots.
            batches = np.array_split(support, max(1, (len(support) + 339) // 340))
            anchor = origins[0] if origins else int(missing[0])
            for i, batch in enumerate(batches):
                vertices = missing.tolist() if i == 0 else []
                targets = list(vertices)
                empty_lod = not origins and not len(batch)
                corners = 3 if empty_lod else len(batch) * 3
                indices = np.arange(len(vertices), len(vertices) + corners)
                vertices.extend([anchor] * corners)
                if domain.normal:
                    targets.extend([anchor] * 3 if empty_lod else domain.compute[batch].ravel().tolist())
                new = _rebased_draw(template[3], vertex_base + len(origins), face_base + len(faces), len(vertices),
                                    len(indices))
                new.buffer, new.is_quad = part.mesh_buffer, 0
                group.submeshes.append(new)
                group.vertex_count += len(vertices)
                origins.extend(vertices)
                faces.extend(indices.tolist())
                redirects.extend(targets)
        origin = np.asarray(origins, dtype=np.int64)
        payload = _select_payload(domain.payload, origin, len(origin))
        payload.integer_faces = array("I", faces)
        payloads[len(payloads)] = payload
        mappings[domain.lod_index] = origin, vertex_base
        if domain.normal:
            lookup = {int(old): i for i, old in reversed(list(enumerate(origins)))}
            annotations = {domain.base + i: r.annotation for i, r in enumerate(domain.normal.redirects)}
            mapped = tuple(NormalRecalcRedirect(lookup[v], annotations.get(old, False))
                           for old, v in zip(origins, redirects))
            normals.append(NormalRecalcLOD(domain.lod_index, 0, 0, 0, (), (),
                                          redirects=mapped, vertex_base=vertex_base))
        geometry.append(PartGeometry(domain.lod_index, origin, tuple(draws), len(support)))
        vertex_base += len(origin)
        face_base += len(faces)
    joined, _ = join_payloads(payloads, loss)
    part.mesh_buffer.buffer_payloads = {0: joined}
    if mesh.normal_recalc_data:
        part.normal_recalc_data = NormalRecalcData(redirects=normals[0].redirects,
            lods=tuple(normals), declared_lod_count=len(normals),
            legacy_profile=mesh.normal_recalc_data.legacy_profile)
    _morphs(part, mappings)
    if mesh.shadow_mesh is not None:
        indices = {id(lod): i for i, lod in enumerate(mesh.meshes[0].lods)}
        part.shadow_mesh.lods = [part.meshes[0].lods[indices[id(lod)]] for lod in mesh.shadow_mesh.lods]
        part.shadow_mesh.payloads = part.mesh_buffer.buffer_payloads
    return part, tuple(geometry)


def _new_group(template, lod, buffer):
    group = copy(template)
    group.buffer, group.submeshes, group.vertex_count = buffer, [], 0
    lod.mesh_groups.append(group)
    return group


def split_palettes(mesh, target, loss):
    main_lods = {id(lod) for lod in mesh.meshes[0].lods}
    shared_shadow = (mesh.shadow_mesh is None or mesh.shadow_mesh.shares_main_payload
                     and all(id(lod) in main_lods for lod in mesh.shadow_mesh.lods))
    if not shared_shadow or mesh.occluder_mesh is not None:
        raise ValueError("Palette partitioning of auxiliary geometry requires a separate resource")
    make_resident(mesh, loss)
    domains = [_Domain(mesh, i) for i in range(len(mesh.meshes[0].lods))]
    common = set(np.unique(np.concatenate([d.bones[d.required].ravel() for d in domains])).tolist()) - {-1}
    chunks = []
    def divide(domain, di, ids):
        _needed, _neighbors, bones = domain.closure([(di, ids)])
        bones |= common
        size = len(np.unique(domain.primitives[di][ids]))
        if len(bones) <= target.max_deform_bones and (target.canonical_ordinary_supports_32bit_indices or size <= 65536):
            chunks.append((domain.lod_index, di, ids, bones))
        elif len(ids) > 1:
            positions = np.asarray(domain.payload.positions).reshape(-1, 3)
            centers = positions[domain.primitives[di][ids]].mean(axis=1)
            axis = int(np.ptp(centers, axis=0).argmax())
            ids = ids[np.argsort(centers[:, axis], kind="stable")]
            middle = len(ids) // 2
            divide(domain, di, ids[:middle])
            divide(domain, di, ids[middle:])
        else:
            raise ValueError(f"One primitive and its normal dependencies need {len(bones)} deform bones; "
                             f"target supports {target.max_deform_bones}")
    for domain in domains:
        for di, primitives in enumerate(domain.primitives):
            if len(primitives):
                divide(domain, di, np.arange(len(primitives)))
    if not chunks:
        raise ValueError("Cannot partition a palette without drawable primitives")
    bins = []
    for li, di, ids, bones in sorted(chunks, key=lambda c: -len(c[3])):
        candidates = [(len(used | bones), i) for i, (used, _) in enumerate(bins)
                      if len(used | bones) <= target.max_deform_bones]
        if candidates:
            _, index = min(candidates)
        else:
            index = len(bins)
            bins.append((set(common), [[] for _ in domains]))
        bins[index][0].update(bones)
        bins[index][1][li].append((di, ids))
    output = []
    def emit(selection):
        part, geometry = _build_part(mesh, domains, selection, loss)
        lods = part.meshes[0].lods
        fits = all(len(lod.mesh_groups) <= 255 and all(len(g.submeshes) <= 255 for g in lod.mesh_groups)
                   for lod in lods)
        if target.canonical_single_part_shadow and part.shadow_mesh is not None:
            fits &= sum(len(g.submeshes) for g in part.shadow_mesh.lods[0].mesh_groups) <= 255
        if fits:
            output.append((part, geometry))
            return
        count = sum(len(ids) for items in selection for _, ids in items)
        if count < 2:
            raise ValueError("One primitive and its normal dependencies exceed the target's draw/part limits")
        halves = [[[] for _ in domains] for _ in range(2)]
        remaining = count // 2
        for li, items in enumerate(selection):
            for di, ids in items:
                split = min(remaining, len(ids))
                for side, batch in enumerate((ids[:split], ids[split:])):
                    if len(batch):
                        halves[side][li].append((di, batch))
                remaining -= split
        for half in halves:
            emit(half)
    for _, selection in bins:
        for items in selection:
            items.sort(key=lambda item: (item[0], int(item[1][0])))
        emit(selection)
    retained = np.unique(np.concatenate([g.source_vertices for _, geometry in output for g in geometry]))
    removed = mesh.mesh_buffer.buffer_payloads[0].vertex_count - len(retained)
    if removed:
        loss(f"Removed {removed} unreferenced vertices while partitioning; retained all rendering and deformation dependencies")
    return output
