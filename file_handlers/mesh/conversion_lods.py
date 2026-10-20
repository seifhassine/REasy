from dataclasses import replace

lazy from .blend_shape import BlendShapeChannel, BlendShapeTarget, BlendShapeTargetRange, blend_shape_name_offsets


ACTIVATION_LOSS = ("Source morph LOD target and channel maps disagree; preserved channel bindings "
                   "but rebuilt target activation groups")


def _map(values, count, local_count, *, reference=False):
    if not values or reference:
        return [i if i < local_count else -1 for i in range(count)]
    if len(values) < count:
        raise ValueError("Morph LOD map is shorter than its canonical domain")
    result = list(values[:count])
    if any(value < -1 or value >= local_count for value in result):
        raise ValueError("Morph LOD map references an unavailable local entry")
    active = [value for value in result if value >= 0]
    if len(active) != len(set(active)):
        raise ValueError("Morph LOD map assigns multiple controls to one local entry")
    return result


def _coalesce_groups(bodies):
    def signature(item):
        return (item.same_target_id, item.is_blend_shape, item.part_index,
                tuple((r.base_vertex_location, r.vertex_count, r.part_id, r.surface_range_id)
                      for r in item.ranges))

    boundaries = [0, *(i for i in range(1, bodies[0].channel_count)
        if any(signature(b.targets[i - 1]) != signature(b.targets[i]) for b in bodies)),
        bodies[0].channel_count]
    for body in bodies:
        targets, indices, identities = [], {}, {}
        for start, end in zip(boundaries, boundaries[1:]):
            owner, index = body.targets[start], len(targets)
            targets.append(replace(owner, channel_count=end - start,
                same_target_id=identities.setdefault(owner.same_target_id, index)))
            indices.update((old, index) for old in range(start, end))
        for channel in body.channels:
            channel.segments = [replace(s, target_index=indices[s.target_index]) for s in channel.segments]
        body.targets = targets
        body.target_lod_map = list(range(len(targets)))


def align_morph_lods(mesh, source, target, loss):
    blend = mesh.blend_shape_data
    if not blend or not blend.bodies or not source.has_modern_blend_shape_targets:
        return
    bodies = sorted(blend.bodies, key=lambda body: body.lod_index)
    target_count = max(len(body.targets) for body in bodies)
    channel_count = max(body.channel_count for body in bodies)
    unusual = any(any(v != i for i, v in enumerate(values[:count]))
                  for body in bodies[1:]
                  for values, count in ((body.target_lod_map, target_count),
                                        (body.channel_lod_map, channel_count)))
    if not unusual:
        return
    maps = []
    for body in bodies:
        reference = body is bodies[0]
        targets = _map(body.target_lod_map, target_count, len(body.targets), reference=reference)
        channels = _map(body.channel_lod_map, channel_count, body.channel_count, reference=reference)
        groups = {}
        for owner in body.targets:
            for index in range(owner.channel_location, owner.channel_location + owner.channel_count):
                groups.setdefault(index, set()).add(owner.same_target_id)
        covered, inconsistent = set(), False
        for canonical, root in enumerate(bodies[0].targets):
            local = targets[canonical]
            if local < 0:
                continue
            owner = body.targets[local]
            for index in range(root.channel_location, root.channel_location + root.channel_count):
                mapped = channels[index]
                if mapped < 0:
                    continue
                if owner.same_target_id not in groups.get(mapped, ()):
                    inconsistent = True
                covered.add(index)
        # targetLodMap=-1 skips activation before channel maps are read.
        for index in range(bodies[0].channel_count):
            if index not in covered:
                channels[index] = -1
        if inconsistent:
            loss(ACTIVATION_LOSS)
        maps.append(channels)

    # Maximum-sized native tables can end in slots unused by every LOD.
    domain = [i for i in range(channel_count)
              if i < bodies[0].channel_count or any(mapping[i] >= 0 for mapping in maps)]
    maps = [[mapping[i] for i in domain] for mapping in maps]

    extra_names = {}
    for body, mapping in zip(bodies, maps):
        for channel in body.channels:
            if channel.index not in mapping:
                name = f"{channel.name} [LOD {body.lod_index}]"
                if name in (mesh.names[i] for i in mesh.blend_shape_indices):
                    raise ValueError(f"Ambiguous LOD-only morph control name: {name}")
                extra_names[len(mapping)] = BlendShapeChannel(len(mapping), len(mesh.blend_shape_indices), name)
                mesh.blend_shape_indices.append(len(mesh.names))
                mesh.names.append(name)
                for other in maps:
                    other.append(channel.index if other is mapping else -1)
    count = len(maps[0])
    templates = {}
    offsets = blend_shape_name_offsets(blend, source)
    for body, mapping in zip(bodies, maps):
        channels = {channel.index: channel for channel in body.channels}
        for index, local in enumerate(mapping):
            if local in channels:
                templates.setdefault(index, channels[local])
            elif local >= 0:
                slot = offsets[body.lod_index] + local
                if slot < len(mesh.blend_shape_indices):
                    templates.setdefault(index, BlendShapeChannel(local, slot))
    for index in range(bodies[0].channel_count):
        slot = offsets[bodies[0].lod_index] + index
        if slot < len(mesh.blend_shape_indices):
            templates.setdefault(index, BlendShapeChannel(index, slot))
    templates.update(extra_names)

    for body, mapping in zip(bodies, maps):
        channels = {channel.index: channel for channel in body.channels}
        targets, aligned, identities = [], [], {}
        for index, local in enumerate(mapping):
            channel = channels.get(local)
            template = channel or templates.get(index)
            if template is None:
                raise ValueError("Canonical morph control has no name or channel in any LOD")
            segments, ranges, owners = [], [], []
            if channel:
                for segment in channel.segments:
                    owner = body.targets[segment.target_index]
                    owners.append(owner)
                    ranges.append(owner.ranges[segment.range_index])
                    segments.append(replace(segment, target_index=index, range_index=len(ranges) - 1))
            if segments:
                identity = tuple(sorted({owner.same_target_id for owner in owners}))
                item = replace(owners[0], channel_location=index, channel_count=1,
                               same_target_id=identities.setdefault(identity, index), ranges=ranges, aabb=None)
            else:
                active = target.canonical_single_channel_morph_targets
                empty = [] if active else [BlendShapeTargetRange(0, 0, 0)]
                item = BlendShapeTarget(index, 1, index, empty, None, is_blend_shape=active)
            targets.append(item)
            aligned.append(BlendShapeChannel(index, template.name_slot, template.name, segments))
        body.targets, body.channels = targets, aligned
        body.channel_count = count
        body.alias_of_lod = None
        body.target_lod_map = body.channel_lod_map = list(range(count))
    if not target.has_modern_blend_shape_targets:
        _coalesce_groups(bodies)
