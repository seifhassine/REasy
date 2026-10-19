from array import array
from copy import copy, deepcopy
from dataclasses import replace

lazy import numpy as np

lazy from .contract_utils import layer_groups
from .mesh_file import SKIN_WEIGHT_STREAMS, MeshBufferPayload


_PLANES = {"positions": 3, "normals": 3, "tangents": 3, "normal_ws": 1,
           "tangent_ws": 1, "uv0": 2, "uv1": 2, "uv2": 2, "colors": 4}
_SEGMENT_PLANES = ("position_deltas", "position_w", "target_normals", "normal_w", "deform_percent", "vertex_cull_mask")
_WEIGHT_METADATA = ("runtime_ignored_weight_bytes", "trailing_weight_bytes", "explicit_sixth_weight_bytes",
                    "unused_seventh_weight_bytes", "unused_eighth_weight_bytes", "packed_index_reserved_bits")


def _slice_payload(payload, start, end):
    return _select_payload(payload, slice(start, end), end - start)


def _select_rows(values, width, selection):
    if not width or not len(values):
        return values[:0]
    if isinstance(selection, slice):
        return values[selection.start * width:selection.stop * width]
    data = np.frombuffer(values, dtype=np.uint8) if isinstance(values, bytes) else np.asarray(values)
    selected = data.reshape(-1, width)[selection].ravel()
    return selected.tobytes() if isinstance(values, bytes) else array(values.typecode, selected)


def select_skin_rows(stream, selection, count):
    metadata = {}
    for field in _WEIGHT_METADATA:
        values = getattr(stream, field)
        width = len(values) // stream.vertex_count if stream.vertex_count else 0
        metadata[field] = _select_rows(values, width, selection)
    return replace(stream, vertex_count=count,
                   deform_indices=_select_rows(stream.deform_indices, stream.influence_count, selection),
                   weights=_select_rows(stream.weights, stream.influence_count, selection), **metadata)


def _select_payload(payload, selection, count):
    result = MeshBufferPayload(vertex_count=count, integer_faces=array("I"),
                               is_auxiliary_geometry=payload.is_auxiliary_geometry,
                               declared_vertex_semantics=payload.declared_vertex_semantics)
    for name, width in _PLANES.items():
        setattr(result, name, _select_rows(getattr(payload, name), width, selection))
    for name in SKIN_WEIGHT_STREAMS:
        stream = getattr(payload, name)
        if stream is not None:
            setattr(result, name, select_skin_rows(stream, selection, count))
    return result


def _parts(moves, buffer_index, start, count):
    cursor, end, result = start, start + count, []
    for index, low, high, destination in sorted(moves, key=lambda item: item[1]):
        if index != buffer_index or high <= cursor or low > cursor:
            continue
        stop = min(high, end)
        new_start = destination + cursor - low
        if result and result[-1][0] + result[-1][1] == cursor and result[-1][2] + result[-1][1] == new_start:
            old, size, new = result[-1]
            result[-1] = old, size + stop - cursor, new
        else:
            result.append((cursor, stop - cursor, new_start))
        cursor = stop
        if cursor == end:
            return result
    if count == 0:
        for index, low, high, destination in moves:
            if index == buffer_index and low <= start <= high:
                return [(start, 0, destination + start - low)]
        for index, _low, _high, destination in moves:
            if index == buffer_index:
                return [(start, 0, destination)]
    raise ValueError(f"Vertex range [{start}, {end}) has no allocation in buffer {buffer_index}")


def join_payloads(payloads, loss):
    payloads = dict(sorted(payloads.items()))
    result = MeshBufferPayload(integer_faces=array("I"), is_auxiliary_geometry=all(
        payload.is_auxiliary_geometry for payload in payloads.values()))
    bases = {}
    for index, payload in payloads.items():
        bases[index] = result.vertex_count, len(result.integer_faces)
        result.vertex_count += payload.vertex_count
        faces = payload.index_values
        result.integer_faces.extend(int(value) for value in faces)
    for name, width in _PLANES.items():
        if not any(len(getattr(p, name)) for p in payloads.values()):
            continue
        default = {"tangent_ws": 127, "colors": 255}.get(name, 0)
        dest = getattr(result, name)
        for payload in payloads.values():
            values = getattr(payload, name)
            if not len(values) and payload.vertex_count:
                if name not in ("normal_ws", "tangent_ws", "colors"):
                    loss(f"Filled missing {name} values while combining vertex buffers")
                values = [default] * (payload.vertex_count * width)
            dest.extend(values)
    for name in SKIN_WEIGHT_STREAMS:
        sources = [getattr(p, name) for p in payloads.values() if p.vertex_count]
        if not sources or all(s is None for s in sources):
            continue
        if any(s is None for s in sources):
            raise ValueError("Cannot combine skinned and unskinned buffers without changing deformation")
        first = sources[0]
        if any((s.influence_count, s.implicit_last_weight) !=
               (first.influence_count, first.implicit_last_weight) for s in sources):
            raise ValueError("Cannot combine different influence layouts")
        metadata = {}
        for field in _WEIGHT_METADATA:
            widths = {len(getattr(s, field)) // s.vertex_count for s in sources if getattr(s, field)}
            if len(widths) > 1:
                raise ValueError(f"Cannot combine incompatible {field} layouts")
            width = next(iter(widths), 0)
            metadata[field] = b"".join(getattr(s, field) or bytes(s.vertex_count * width) for s in sources)
        setattr(result, name, replace(first, vertex_count=result.vertex_count,
            deform_indices=array("H", (*s.deform_indices for s in sources)),
            weights=array("f", (*s.weights for s in sources)),
            **metadata,
        ))
    return result, bases


def _rebased_draw(draw, vertex_offset, face_offset, vertex_count, index_count):
    new = copy(draw)
    new.buffer_index, new.verts_index_offset, new.faces_index_offset = 0, vertex_offset, face_offset
    new.vert_count, new.indices_count = vertex_count, index_count
    return new


def _pack(groups, payloads, loss, *, report_orphans=False):
    slices, moves = {}, []
    coverage = {}
    vertex_base = face_base = 0
    for group in groups:
        submeshes = group.submeshes
        if not submeshes:
            continue
        indices = {submesh.buffer_index for submesh in submeshes}
        if len(indices) != 1:
            raise ValueError("One native mesh group cannot mix vertex buffers")
        index = indices.pop()
        payload = payloads[index]
        end = submeshes[-1].verts_index_offset + submeshes[-1].vert_count
        start = end - group.vertex_count
        if not 0 <= start <= end <= payload.vertex_count:
            raise ValueError("Mesh group allocation leaves its decoded vertex payload")
        part = _slice_payload(payload, start, end)
        coverage.setdefault(id(payload), []).append((start, end))
        faces = payload.index_values
        moves.append((index, start, end, vertex_base))
        for submesh in submeshes:
            first, count = submesh.faces_index_offset, submesh.indices_count
            submesh.faces_index_offset = face_base + len(part.integer_faces)
            part.integer_faces.extend(int(value) for value in faces[first:first + count])
            submesh.verts_index_offset += vertex_base - start
            submesh.buffer_index = 0
        slices[len(slices)] = part
        vertex_base += part.vertex_count
        face_base += len(part.integer_faces)
    if report_orphans:
        orphan_count = 0
        for payload in {id(payload): payload for payload in payloads.values()}.values():
            covered = previous = 0
            for start, end in sorted(coverage.get(id(payload), ())):
                covered += max(0, end - max(start, previous))
                previous = max(previous, end)
            orphan_count += payload.vertex_count - covered
        if orphan_count:
            loss(f"Removed {orphan_count} vertices outside every native draw allocation")
    joined, _bases = join_payloads(slices, loss)
    if not joined.vertex_count:
        joined.declared_vertex_semantics = next(
            (payload.declared_vertex_semantics for payload in payloads.values() if payload.declared_vertex_semantics), (0,))
    return joined, moves


def _rebase_morphs(mesh, lod_moves, all_moves):
    blend = mesh.blend_shape_data
    if blend is None:
        return
    for body_index, original in enumerate(blend.bodies):
        body = blend.bodies[body_index] = deepcopy(original)
        moves = lod_moves[body.lod_index]
        if body.alias_of_lod is not None and moves != lod_moves[body.alias_of_lod]:
            body.alias_of_lod = None
        buffers = {index for index, *_rest in moves}
        range_maps = {}
        for target_index, target in enumerate((*body.targets, *body.none_targets)):
            ranges = []
            for range_index, item in enumerate(target.ranges):
                matching = {segment.buffer_index for channel in body.channels for segment in channel.segments
                            if segment.target_index == target_index and segment.range_index == range_index}
                candidates = matching or buffers
                if len(candidates) != 1:
                    raise ValueError("Morph range has no unique source vertex buffer")
                parts = _parts(moves, next(iter(candidates)), item.base_vertex_location, item.vertex_count)
                range_maps[target_index, range_index] = len(ranges)
                for old, count, new in parts:
                    ranges.append(replace(item, base_vertex_location=new, vertex_count=count,
                                          blend_shape_location=item.blend_shape_location + old - item.base_vertex_location))
            target.ranges = ranges
        for channel in body.channels:
            segments = []
            for segment in channel.segments:
                parts = _parts(moves, segment.buffer_index, segment.base_vertex_location, len(segment.position_deltas))
                for part_index, (old, count, new) in enumerate(parts):
                    offset = old - segment.base_vertex_location
                    values = {}
                    for name in _SEGMENT_PLANES:
                        value = getattr(segment, name)
                        values[name] = None if value is None else value[offset:offset + count].copy()
                    segments.append(replace(segment, base_vertex_location=new, buffer_index=0,
                        range_index=range_maps[segment.target_index, segment.range_index] + part_index, **values))
            channel.segments = segments
    if blend.bone_modifier is not None:
        blend.bone_modifier.effectors = [replace(effector, vertex_indices=tuple(
            _parts(all_moves, 0, index, 1)[0][2] for index in effector.vertex_indices))
            for effector in blend.bone_modifier.effectors]


def make_resident(mesh, loss):
    buffer = mesh.mesh_buffer
    payloads = dict(buffer.buffer_payloads)
    for alias, owner in buffer.streaming_payload_aliases.items():
        visited = {alias}
        while owner in buffer.streaming_payload_aliases:
            if owner in visited:
                raise ValueError("Streaming alias relation contains a cycle")
            visited.add(owner)
            owner = buffer.streaming_payload_aliases[owner]
        payloads[alias] = payloads[owner]
    lods = mesh.meshes[0].lods if mesh.meshes else []
    unique_lods = list({id(lod): lod for lod in lods}.values())
    # Before _pack: shared auxiliary layers alias primary submeshes, whose offsets it rewrites.
    primary_submeshes = {id(s) for lod in unique_lods for g in lod.mesh_groups for s in g.submeshes}
    auxiliary = []
    for layer in (mesh.shadow_mesh, mesh.occluder_mesh):
        if layer is not None:
            topology = deepcopy(layer.lods if hasattr(layer, "lods") else layer.mesh_groups, {id(buffer): buffer})
            snapshot = ([*lod.mesh_groups for lod in {id(lod): lod for lod in topology}.values()]
                        if hasattr(layer, "lods") else topology)
            auxiliary.append((layer, layer_groups(layer, unique_lods=True), layer.payloads,
                              topology, snapshot))
    joined, all_moves = _pack([*lod.mesh_groups for lod in unique_lods], payloads, loss,
                             report_orphans=bool(unique_lods))
    buffer.buffer_payloads = {0: joined}
    lod_moves, cursor = {}, 0
    for lod in unique_lods:
        count = sum(bool(group.submeshes) for group in lod.mesh_groups)
        lod_moves[id(lod)] = all_moves[cursor:cursor + count]
        cursor += count
    ordered_moves = [lod_moves[id(lod)] for lod in lods]
    for layer, groups, old_payloads, topology, snapshot in auxiliary:
        shared = layer.shares_main_payload
        changes = []
        if shared:
            try:
                for group, old_group in zip(groups, snapshot):
                    for submesh, old in zip(group.submeshes, old_group.submeshes):
                        if id(submesh) in primary_submeshes:
                            continue
                        parts = _parts(all_moves, old.buffer_index, old.verts_index_offset, old.vert_count)
                        if len(parts) != 1:
                            raise ValueError("Auxiliary allocation is no longer contiguous")
                        changes.append((submesh, old, parts[0][2]))
            except ValueError:
                shared = False
        if shared:
            for submesh, old, new_start in changes:
                source = old_payloads[old.buffer_index]
                faces = source.index_values
                submesh.faces_index_offset = len(joined.integer_faces)
                joined.integer_faces.extend(int(value) for value in faces[old.faces_index_offset:old.faces_index_offset + old.indices_count])
                submesh.verts_index_offset, submesh.buffer_index = new_start, 0
            result = joined
        else:
            if layer.shares_main_payload:
                if hasattr(layer, "lods"):
                    layer.lods = topology
                else:
                    layer.mesh_groups = topology
                groups = snapshot
            result, _moves = _pack(groups, old_payloads, loss)
            result.is_auxiliary_geometry = True
        layer.shares_main_payload = shared
        layer.payloads = {0: result}
    _rebase_morphs(mesh, ordered_moves, all_moves)
    if mesh.normal_recalc_data is not None:
        normals = mesh.normal_recalc_data
        updated = []
        for lod in normals.lods:
            moves = ordered_moves[lod.lod_index]
            offsets = {new - start for _index, start, _end, new in moves}
            if len(offsets) != 1:
                raise ValueError("Normal-recalculation vertex domain requires a nonuniform remap")
            base = min(s.verts_index_offset for g in lods[lod.lod_index].mesh_groups for s in g.submeshes)
            updated.append(replace(lod, buffer_index=0, vertex_base=base))
        mesh.normal_recalc_data = replace(normals, lods=tuple(updated))
    mesh.streaming_info = None
    buffer.streaming_buffer_headers = {}
    buffer.streaming_payload_aliases = {}
    buffer.streaming_buffer_count = 0
