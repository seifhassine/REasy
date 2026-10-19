from copy import deepcopy
from dataclasses import replace

import numpy as np

from .profiles import BlendShapeLayout, MeshHeaderLayout, NormalRecalcLayout


def _lod_map_entry(values, index, lod):
    return values[index] if index < len(values) else (-1 if lod == 0 else index)


def _require_identity_lod_maps(blend, *, allow_reference_maps=False):
    targets = max((len(b.targets) for b in blend.bodies), default=0)
    channels = max((b.channel_count for b in blend.bodies), default=0)
    for body in blend.bodies:
        for values, count in ((body.target_lod_map, targets), (body.channel_lod_map, channels)):
            values = values[:count]
            if (values and not all(value == index for index, value in enumerate(values))
                    and not ((body.lod_index == 0 or allow_reference_maps)
                             and all(value == -1 for value in values))):
                raise ValueError("Morph LOD correspondence must be aligned before restructuring targets")


def _select_morph_codec(body, profile, loss):
    from .canonical_writer import _quantize_blend_positions, _target_quantization_aabb

    channels = {channel.index: channel for channel in body.channels}
    bounds = [_target_quantization_aabb(t, channels, i) for i, t in enumerate(body.targets)]
    errors = {}
    for codec in profile.authored_blend_shape_codecs:
        if codec == 1 and not (profile.blend_shape_standard_w_is_padding
                              or profile.blend_shape_standard_w_is_runtime_ignored):
            continue
        error = 0.0
        for channel in body.channels:
            for segment in channel.segments:
                values = np.asarray(segment.position_deltas, dtype=np.float32)
                if codec == 2:
                    decoded = values
                elif codec == 1:
                    with np.errstate(over="ignore"):
                        decoded = values.astype(np.float16).astype(np.float32)
                else:
                    if segment.vertex_cull_mask is not None and np.any(segment.vertex_cull_mask):
                        error = float("inf")
                        continue
                    low, high = bounds[segment.target_index]
                    divisor = (2047, 1023, 2047) if codec == 0 else (255, 255, 255)
                    quantized = _quantize_blend_positions(values, low, high, divisor).astype(np.float32)
                    low, high = np.asarray(low, dtype=np.float32), np.asarray(high, dtype=np.float32)
                    decoded = low + (quantized + np.float32(0.5)) * ((high - low) / np.asarray(divisor, dtype=np.float32))
                error = max(error, float(np.max(np.abs(values - decoded), initial=0)))
        errors[codec] = error
    if not errors:
        raise ValueError("Target has no fully understood morph codec")
    codec = min(errors, key=lambda value: (errors[value], value != body.compression_type, value))
    if not np.isfinite(errors[codec]):
        raise ValueError("No target morph codec can represent these deltas or vertex-culling masks")
    body.compression_type = codec
    if errors[codec]:
        loss(f"Target quantizes morph deltas (maximum component error {errors[codec]:.9g})")


def _merge_morph_ranges(mesh, body, target_index):
    target = body.targets[target_index]
    if len(target.ranges) <= 1:
        return
    ranges = sorted(enumerate(target.ranges), key=lambda item: item[1].base_vertex_location)
    start = ranges[0][1].base_vertex_location
    end = max(r.base_vertex_location + r.vertex_count for _index, r in ranges)
    cursor = start
    for _index, item in ranges:
        if item.base_vertex_location < cursor:
            raise ValueError("Overlapping morph ranges cannot be merged into the target layout")
        cursor = item.base_vertex_location + item.vertex_count
    channels = {channel.index: channel for channel in body.channels}
    for index in range(target.channel_location, target.channel_location + target.channel_count):
        channel = channels[index]
        segments = {s.range_index: s for s in channel.segments if s.target_index == target_index}
        if len(segments) != len(ranges) or len({s.buffer_index for s in segments.values()}) != 1:
            raise ValueError("Morph ranges have incomplete segments or span vertex buffers")
        first = segments[ranges[0][0]]
        payload = mesh.mesh_buffer.buffer_payloads[first.buffer_index]
        if end > payload.vertex_count:
            raise ValueError("Morph range leaves its decoded vertex buffer")
        # Zero deltas and the bind normal make vertices between ranges inert.
        positions = np.zeros((end - start, 3), dtype=np.float32)
        normals = (np.asarray(payload.normals, dtype=np.float32).reshape(-1, 3)[start:end].copy()
                   if first.target_normals is not None else None)
        mask = np.zeros(end - start, dtype=bool)
        for range_index, item in ranges:
            segment = segments[range_index]
            slot = slice(item.base_vertex_location - start,
                         item.base_vertex_location - start + item.vertex_count)
            positions[slot] = segment.position_deltas
            if normals is not None:
                if segment.target_normals is None:
                    raise ValueError("Morph ranges mix stored and recalculated normals")
                normals[slot] = segment.target_normals
            if segment.vertex_cull_mask is not None:
                mask[slot] = segment.vertex_cull_mask
        merged = replace(first, base_vertex_location=start, position_deltas=positions,
                         target_normals=normals, vertex_cull_mask=mask if mask.any() else None,
                         range_index=0, position_w=None, normal_w=None, deform_percent=None)
        channel.segments = [s for s in channel.segments if s.target_index != target_index] + [merged]
        channel.segments.sort(key=lambda s: (s.target_index, s.range_index))
    target.ranges = [replace(ranges[0][1], base_vertex_location=start, vertex_count=end - start)]
    target.aabb = None


def _merge_shared_channels(blend, *, allow_reference_maps=False):
    _require_identity_lod_maps(blend, allow_reference_maps=allow_reference_maps)
    roots = list(blend.bodies[0].targets)
    root_channels = {c.index: i for i, c in enumerate(sorted(blend.bodies[0].channels, key=lambda c: c.index))}
    for body in blend.bodies:
        old_targets = body.targets
        targets, lod_map, identities = [], [], {}
        for channel in sorted(body.channels, key=lambda c: c.index):
            owners = [(i, t) for i, t in enumerate(old_targets)
                      if t.channel_location <= channel.index < t.channel_location + t.channel_count]
            if not owners or len({t.is_blend_shape for _, t in owners}) != 1:
                raise ValueError("Cannot combine active and disabled morph ranges in one channel")
            ranges, locations, mappings = [], {}, set()
            identity = []
            for old_index, target in owners:
                lane = channel.index - target.channel_location
                identity.append(target.same_target_id)
                owner = _lod_map_entry(body.target_lod_map, old_index, body.lod_index)
                mappings.add(root_channels.get(roots[owner].channel_location + lane, -1)
                             if 0 <= owner < len(roots) else -1)
                for range_index, value in enumerate(target.ranges):
                    locations[old_index, range_index] = len(ranges)
                    ranges.append(value)
            if len(mappings) > 1:
                raise ValueError("Target cannot combine morph ranges with different LOD remapping")
            target_index = len(targets)
            targets.append(replace(owners[0][1], channel_location=channel.index, channel_count=1,
                same_target_id=identities.setdefault(tuple(sorted(set(identity))), target_index), ranges=ranges, aabb=None))
            lod_map.append(mappings.pop())
            channel.segments = [replace(s, target_index=target_index,
                range_index=locations[s.target_index, s.range_index]) for s in channel.segments]
        # Inactive targets stay at the end of the target-map domain.
        lod_map.extend(len(root_channels) + owner - len(roots) if owner >= len(roots) else -1
                       for owner in body.target_lod_map[len(old_targets):])
        body.targets, body.target_lod_map, body.alias_of_lod = targets, lod_map, None


def _reshape_targets(blend, *, allow_reference_maps=False):
    _require_identity_lod_maps(blend, allow_reference_maps=allow_reference_maps)
    root_targets = [(index, lane) for index, target in enumerate(blend.bodies[0].targets)
                    for lane in range(target.channel_count)]
    root_indices = {key: index for index, key in enumerate(root_targets)}
    for body in blend.bodies:
        old_targets, targets, indices, lod_map, identities = body.targets, [], {}, [], {}
        for index, target in enumerate(old_targets):
            owner = _lod_map_entry(body.target_lod_map, index, body.lod_index)
            for lane in range(target.channel_count):
                indices[index, lane] = len(targets)
                lod_map.append(root_indices.get((owner, lane), -1))
                targets.append(replace(target, channel_location=target.channel_location + lane,
                    channel_count=1, ranges=list(target.ranges),
                    same_target_id=identities.setdefault(target.same_target_id, len(targets))))
        for channel in body.channels:
            for segment in channel.segments:
                lane = channel.index - old_targets[segment.target_index].channel_location
                segment.target_index = indices[segment.target_index, lane]
        body.targets, body.target_lod_map, body.alias_of_lod = targets, lod_map, None


def translate_morphs(mesh, source, target, loss):
    source_has_normal_recalc = mesh.normal_recalc_data is not None
    blend = mesh.blend_shape_data
    if blend is not None:
        blend.set_channel_names(mesh.blend_shape_indices, mesh.names)
        if source.blend_shape_vertex_offsets_are_lod_relative and not target.blend_shape_vertex_offsets_are_lod_relative:
            for body in blend.bodies:
                body.alias_of_lod = None
        if blend.unresolved_semantics:
            raise ValueError("Blend-shape data has unresolved semantics: " +
                                      "; ".join(blend.unresolved_semantics))
        if (blend.bodies and mesh.normal_recalc_data is None
                and not (target.blend_shape_normal_w_is_padding
                         or target.blend_shape_normal_w_is_runtime_ignored)):
            from .normal_recalc import NormalRecalcData, NormalRecalcLOD, NormalRecalcRedirect

            loss("Target requires recalculating morph normals from topology instead of stored target normals")
            lods = tuple(NormalRecalcLOD(index, 0, 0, 0, (), (), redirects=tuple(
                NormalRecalcRedirect(i) for i in range(sum(g.vertex_count for g in lod.mesh_groups))))
                for index, lod in enumerate(mesh.meshes[0].lods))
            mesh.normal_recalc_data = NormalRecalcData(
                redirects=lods[0].redirects, lods=lods, declared_lod_count=len(lods))
        if blend.bone_modifier is not None and target.header_layout is MeshHeaderLayout.LEGACY:
            loss("Target cannot store blend-shape bone modifiers; removed the modifier")
            blend.bone_modifier = None
        if target.blend_shape_layout is BlendShapeLayout.LEGACY and len(blend.bodies) > 1:
            loss("Target supports morphs on LOD0 only; removed higher-LOD morphs")
            blend.bodies = blend.bodies[:1]
        if (not target.has_modern_blend_shape_targets or target.canonical_single_channel_morph_targets):
            from .conversion_lods import align_morph_lods
            align_morph_lods(mesh, source, target, loss)
        if (source.has_modern_blend_shape_targets != target.has_modern_blend_shape_targets
                and target.has_blend_shape_lod_maps):
            _require_identity_lod_maps(blend, allow_reference_maps=not source.has_modern_blend_shape_targets)
        if target.canonical_single_channel_morph_targets and any(
                t.channel_count > 1 for b in blend.bodies for t in b.targets):
            blend.bodies = deepcopy(blend.bodies)
            _reshape_targets(blend, allow_reference_maps=not source.has_modern_blend_shape_targets)
        if not target.has_modern_blend_shape_targets:
            blend.bodies = [deepcopy(body) for body in blend.bodies]
            if any(len({index for t in body.targets for index in range(t.channel_location,
                    t.channel_location + t.channel_count)}) != sum(t.channel_count for t in body.targets)
                    for body in blend.bodies):
                _merge_shared_channels(blend, allow_reference_maps=not source.has_modern_blend_shape_targets)
            if target.blend_shape_layout is BlendShapeLayout.LEGACY:
                for body in blend.bodies:
                    indices = [index for item in body.targets
                               for index in range(item.channel_location, item.channel_location + item.channel_count)]
                    if len(set(indices)) != len(indices):
                        raise ValueError("Target cannot preserve morph channels shared between targets")
                    remap = {old: new for new, old in enumerate(indices)}
                    for channel in body.channels:
                        channel.index = remap[channel.index]
                    for item in body.targets:
                        item.channel_location = remap.get(item.channel_location, 0)
                    body.channel_count = len(indices)
                    body.channels.sort(key=lambda channel: channel.index)
                    if (source.blend_shape_layout is not BlendShapeLayout.LEGACY
                            and any(item.channel_count != 1 for item in body.targets[:-1])):
                        loss("Target's renderer reads a morph target's deltas as if each target before it had one "
                             "channel; the channels of targets after one with several show other deltas")
        for body in blend.bodies:
            retain_identity = (target.blend_shape_range_has_part_surface_identity
                               and not target.blend_shape_range_identity_is_corpus_zero)
            if not retain_identity and any(
                    r.part_id or r.surface_range_id
                    for item in (*body.targets, *body.none_targets) for r in item.ranges):
                loss("Target cannot store morph part/surface identities")
            for channel in body.channels:
                for segment in channel.segments:
                    for name in ("position_w", "normal_w", "deform_percent"):
                        values = getattr(segment, name)
                        if values is not None and np.any(values):
                            loss(f"Target writer clears unused morph authoring metadata ({name})")
            if not target.has_modern_blend_shape_targets:
                for index in range(len(body.targets)):
                    _merge_morph_ranges(mesh, body, index)
                if body.none_targets:
                    loss("Target cannot store inactive morph targets; retained all active channels")
                    body.none_targets = []
                if any(not item.is_blend_shape and any(r.vertex_count for r in item.ranges)
                       for item in body.targets):
                    raise ValueError("Target cannot represent disabled active-table morph targets")
                cursor = 0
                for index, item in enumerate((*body.targets, *body.none_targets)):
                    if len(item.ranges) != 1 or item.channel_location != cursor:
                        raise ValueError("Target needs single-range, sequential morph channels")
                    cursor += item.channel_count
                    if item.same_target_id != index:
                        loss("Target cannot retain shared morph target identities")
                if not target.has_blend_shape_lod_maps:
                    if any(v >= 0 for v in (*body.target_lod_map[:len(body.targets)],
                                            *body.channel_lod_map[:body.channel_count])):
                        loss("Target cannot store morph LOD remapping")
                    body.target_lod_map = body.channel_lod_map = []
            for channel in body.channels:
                for segment in channel.segments:
                    if (segment.vertex_cull_mask is not None and np.any(segment.vertex_cull_mask)
                            and not target.blend_shape_nan_positions_cull_vertices):
                        loss("Target cannot retain morph vertex-culling masks")
                        segment.vertex_cull_mask = None
            _select_morph_codec(body, target, loss)
            if not retain_identity:
                for item in (*body.targets, *body.none_targets):
                    item.ranges = [replace(r, part_id=0, surface_range_id=0) for r in item.ranges]
        cursor = 0
        for body in blend.bodies:
            offset = cursor if target.has_extended_blend_shape_layout else 0
            if body.channel_offset != offset:
                body.alias_of_lod = None
            body.channel_offset = offset
            cursor += body.channel_count
    normal = mesh.normal_recalc_data
    if normal is not None:
        high_precision = {
            NormalRecalcLayout.RE4, NormalRecalcLayout.SF6, NormalRecalcLayout.MHWILDS,
            NormalRecalcLayout.RE9, NormalRecalcLayout.ONIMUSHA_WOTS, NormalRecalcLayout.DD2_UPDATED,
        }
        source_scale = 65536 if source.normal_recalc_layout in high_precision else 1023
        target_scale = 65536 if target.normal_recalc_layout in high_precision else 1023
        if source_has_normal_recalc and source_scale != target_scale:
            loss(f"Target changes normal-recalculation accumulation precision ({source_scale} to {target_scale}); "
                 "animated shading may differ")
        normal = replace(normal, reserved=(), body_list_offset=0, body_offsets=())
        if source_has_normal_recalc and not source.normal_redirect_annotation_is_runtime_semantic:
            def clear_annotations(redirects):
                return tuple(replace(r, annotation=False) for r in redirects)
            normal = replace(normal, redirects=clear_annotations(normal.redirects),
                lods=tuple(replace(lod, redirects=clear_annotations(lod.redirects)) for lod in normal.lods))
        if target.normal_recalc_layout is NormalRecalcLayout.NONE:
            raise ValueError("Target has no normal-recalculation layout")
        if (source.normal_redirect_annotation_is_runtime_semantic
                and not target.normal_redirect_annotation_is_runtime_semantic
                and (normal.edge_excluded or any(r.annotation for r in normal.redirects)
                     or any(r.annotation for lod in normal.lods for r in lod.redirects))):
            loss("Target cannot retain normal-recalculation edge exclusions")
        normal = replace(normal, edge_excluded=False)
        multi_lod = target.normal_recalc_layout in {
            NormalRecalcLayout.SF6, NormalRecalcLayout.MHWILDS, NormalRecalcLayout.RE9,
            NormalRecalcLayout.ONIMUSHA_WOTS, NormalRecalcLayout.DD2_UPDATED,
        }
        if not multi_lod and len(normal.lods) > 1:
            loss("Target supports normal recalculation on LOD0 only")
            normal = replace(normal, lods=normal.lods[:1], declared_lod_count=1)
        if not normal.redirects and normal.lods:
            normal = replace(normal, redirects=normal.lods[0].redirects)
        if target.normal_recalc_layout in {
                NormalRecalcLayout.DMC5, NormalRecalcLayout.RE2, NormalRecalcLayout.RE3}:
            from .normal_recalc import LegacyNormalRecalcProfile, NormalRecalcData

            profile = None
            if target.normal_recalc_layout is NormalRecalcLayout.DMC5:
                profile = (normal.legacy_profile
                           if source.normal_recalc_layout is NormalRecalcLayout.DMC5
                           else LegacyNormalRecalcProfile.STANDARD)
            normal = NormalRecalcData(redirects=normal.redirects, legacy_profile=profile)
        if multi_lod and not normal.lods and not normal.declared_lod_count:
            normal = replace(normal, declared_lod_count=1)
        mesh.normal_recalc_data = normal
