"""New channels go after the LOD's last active target so no existing channel, name or LOD-map entry moves."""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from ..blend_shape import (
    BlendShapeBody, BlendShapeChannel, BlendShapeData, BlendShapeDeltaSegment, BlendShapeTarget,
    BlendShapeTargetRange,
)
from .scene import _shape_key_names, layer_lods, main_buffer_index


def lod_channel_names(mesh, lod):
    blend = mesh.blend_shape_data
    names = {}
    for body_index, body in enumerate(blend.bodies if blend else ()):
        if body.lod_index != lod or body.alias_of_lod is not None:
            continue
        for position, name in enumerate(_shape_key_names(body.channels)):
            names.setdefault(name, (body_index, position))
    return names


def _lod_buffer(mesh, lod):
    for mesh_index, index, item in layer_lods(mesh, "main"):
        if index == lod and item.mesh_groups and item.mesh_groups[0].submeshes:
            return int(item.mesh_groups[0].submeshes[0].buffer_index)
    return 0


def _body(ctx, lod):
    mesh = ctx.mesh
    blend = mesh.blend_shape_data
    for index, body in enumerate(blend.bodies if blend else ()):
        if body.lod_index == lod:
            if body.alias_of_lod is not None:
                ctx.report.loss(f"LOD{lod} reuses another LOD's morphs; new shape keys there were ignored")
                return None, None
            return index, body
    lods = sorted(body.lod_index for body in (blend.bodies if blend else ()))
    if lods != list(range(lod)):
        ctx.report.loss(f"LOD{lod} has no morph body and lower LODs lack one too; new shape keys there were ignored")
        return None, None
    profile = mesh.profile
    if mesh.normal_recalc_data is None and not (profile.blend_shape_normal_w_is_padding
                                                or profile.blend_shape_normal_w_is_runtime_ignored):
        # Such meshes store explicit target normals whose w component has no known meaning here.
        ctx.report.loss(f"LOD{lod} has no morphs and this game's morph normal format cannot be authored; "
                        "new shape keys there were ignored")
        return None, None
    template = blend.bodies[-1] if blend and blend.bodies else None
    codecs = tuple(profile.authored_blend_shape_codecs)
    compression = template.compression_type if template is not None else (2 if 2 in codecs else codecs[0])
    base = _first_vertex(mesh, lod)
    target = BlendShapeTarget(0, 0, 0, [BlendShapeTargetRange(base, 0, 0)], None)
    channel_offset = (template.channel_offset + template.channel_count) if template is not None else 0
    body = BlendShapeBody(lod, compression, channel_offset, 0, [target])
    if blend is None:
        mesh.blend_shape_data = blend = BlendShapeData(bodies=[])
        mesh.blend_shape_indices = list(getattr(mesh, "blend_shape_indices", []) or [])
    blend.bodies.append(body)
    ctx.report.change(f"Added morphs to LOD{lod}")
    return len(blend.bodies) - 1, body


def _first_vertex(mesh, lod):
    for _mesh_index, index, item in layer_lods(mesh, "main"):
        if index == lod:
            starts = [int(s.verts_index_offset) for g in item.mesh_groups for s in g.submeshes]
            return min(starts, default=0)
    return 0


def _zero_segment(template, explicit_normals, item, target_index, range_index, bind_normals, buffer_index, lod):
    count = int(item.vertex_count)
    first = int(item.base_vertex_location)
    normals = None
    if explicit_normals:
        normals = np.zeros((count, 3), np.float32)
        normals[:max(0, min(count, len(bind_normals) - first))] = bind_normals[first:first + count]
    if template is None:
        return BlendShapeDeltaSegment(first, np.zeros((count, 3), np.float32), target_normals=normals,
                                      buffer_index=buffer_index, lod_index=lod, target_index=target_index,
                                      range_index=range_index)
    extras = {}
    for name in ("position_w", "normal_w", "deform_percent", "vertex_cull_mask"):
        value = getattr(template, name)
        extras[name] = None if value is None else np.zeros((count, *np.asarray(value).shape[1:]), np.asarray(value).dtype)
    return replace(template, base_vertex_location=first, position_deltas=np.zeros((count, 3), np.float32),
                   target_normals=normals, target_index=target_index, range_index=range_index, **extras)


def _insert_channel(ctx, body_index, body, name):
    mesh = ctx.mesh
    profile = mesh.profile
    # Packed deltas share their target's quantization box; a target of its own leaves the others untouched.
    own = profile.canonical_single_channel_morph_targets or body.compression_type in (0, 3)
    if own and body.targets and body.targets[-1].channel_count:
        source = body.targets[-1]
        target = BlendShapeTarget(source.channel_location + source.channel_count, 0, len(body.targets),
                                  [replace(source.ranges[0], vertex_count=0, blend_shape_location=0)], None)
        position = len(body.targets)
        body.targets.insert(position, target)
        for channel in body.channels:
            for segment in channel.segments:
                if segment.target_index >= position:
                    channel.segments[channel.segments.index(segment)] = replace(segment,
                                                                               target_index=segment.target_index + 1)
        if body.target_lod_map:
            value = body.target_lod_map[0] if body.target_lod_map else -1
            body.target_lod_map = [*body.target_lod_map[:position], value, *body.target_lod_map[position:]]
    else:
        target = max(body.targets, key=lambda t: (t.channel_location + t.channel_count, t.channel_count))
    target_index = body.targets.index(target)
    index = target.channel_location + target.channel_count
    if any(channel.index >= index for channel in body.channels):
        ctx.report.loss(f"Shape key {name!r} cannot be appended to its morph target; ignored it")
        return None
    count = body.channel_count
    target.channel_count += 1
    body.channel_count += 1
    target.aabb = None
    if body.channel_lod_map:
        values = [int(v) for v in body.channel_lod_map[:count]]
        identity = values == list(range(count))
        values.append(index if identity else -1)
        body.channel_lod_map = values
    blend = mesh.blend_shape_data
    if profile.has_extended_blend_shape_layout:
        for other in blend.bodies:
            if other is not body and other.channel_offset >= body.channel_offset + count:
                other.channel_offset += 1
    slot = len(mesh.blend_shape_indices)
    mesh.blend_shape_indices.append(len(mesh.names))
    mesh.names.append(name)
    buffer_index = _lod_buffer(mesh, body.lod_index)
    payload = mesh.mesh_buffer.buffer_payloads.get(buffer_index)
    bind_normals = np.asarray(payload.normals, np.float32).reshape(-1, 3) if payload is not None else np.zeros((0, 3))
    template = next((s for c in body.channels for s in c.segments if s.target_index == target_index), None)
    # Payloads store target normals exactly when the mesh does not recalculate them.
    explicit = template.target_normals is not None if template is not None else mesh.normal_recalc_data is None
    segments = [_zero_segment(template, explicit, item, target_index, range_index, bind_normals, buffer_index,
                              body.lod_index)
                for range_index, item in enumerate(target.ranges)]
    body.channels.append(BlendShapeChannel(index, slot, name, segments))
    return body_index, len(body.channels) - 1


def add_channels(ctx):
    wanted = {}
    for plan in ctx.plans:
        if plan.layer != "main" or not plan.sources:
            continue
        payload = ctx.domains.payloads[plan.domain] if plan.domain >= 0 else None
        if payload is not None and main_buffer_index(ctx.mesh, payload) is None:
            continue
        scene_mesh = ctx.scene.meshes.get(ctx.scene_object(plan.key).mesh_key) if plan.key else None
        known = set(lod_channel_names(ctx.mesh, plan.lod))
        if scene_mesh is not None:
            known |= {name for _b, _p, name, _d, _n in scene_mesh.channels}
        for obj, _faces in plan.sources:
            for name, delta in obj.shape_keys.items():
                if name not in known and np.any(delta != 0):
                    wanted.setdefault(plan.lod, {}).setdefault(name, None)
    for lod, names in sorted(wanted.items()):
        body_index, body = _body(ctx, lod)
        if body is None:
            continue
        for name in names:
            added = _insert_channel(ctx, body_index, body, name)
            if added is not None:
                ctx.added_channels.setdefault(lod, {})[name] = added
        added = list(ctx.added_channels.get(lod, ()))
        if added:
            listed = ", ".join(repr(name) for name in added[:4]) + ("…" if len(added) > 4 else "")
            ctx.report.change(f"Added {len(added)} morph channel{'s' if len(added) > 1 else ''} to LOD{lod}: {listed}")
