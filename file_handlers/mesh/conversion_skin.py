from array import array
from dataclasses import replace

import numpy as np

from .conversion_geometry import geometry_layers, mesh_payloads
from .mesh_file import MPLY_MAGIC, SKIN_WEIGHT_STREAMS, SkinWeightBuffer
from .profiles import MaterialSkinningLayout, SkinWeightLayout
from .skinning_contract import MeshSkinningContract, normal_compute_influence_count, packed_rigid_joint_indices


class SourceSkinningAmbiguity(ValueError):
    pass


def source_has_material_skinning(profile):
    return profile.has_material_skinning_contract or _shared_legacy_profile(profile)


def _shared_legacy_profile(profile):
    return profile.game in ("RE3_RESISTANCE", "RERESISTANCE")


def _material_layout(profile):
    return (MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8 if _shared_legacy_profile(profile)
            else profile.material_skinning_layout)


def skin_weight_capacity(profile, *, meshlets=False):
    if not meshlets and profile.skin_weight_count_selects_influence_layout:
        return max(count for _raw, count in profile.skin_weight_influences_by_raw)
    return profile.skin_weight_influence_count


def target_weight_options(profile, *, meshlets=False):
    if meshlets:
        return (profile.skin_weight_influence_count,)
    layout = _material_layout(profile)
    if layout is MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8:
        # Native compute skinning supports both modes (normals recalculated or not).
        return (4, 8)
    capacity = skin_weight_capacity(profile)
    if layout is MaterialSkinningLayout.LATE_DXIL_PACKED_10BIT:
        return tuple(count for count in (3, 6, 12) if count <= capacity)
    if profile.skin_weight_count_selects_influence_layout:
        return tuple(sorted({count for _raw, count in profile.skin_weight_influences_by_raw}))
    return (capacity,)


def _target_shader_count(profile, influence_count):
    return (4 if influence_count == 3 and _material_layout(profile)
            is MaterialSkinningLayout.LATE_DXIL_PACKED_10BIT else influence_count)


def source_weight_options(mesh):
    layout = _material_layout(mesh.profile)
    widths = {payload.skin_weights.influence_count for payload in mesh_payloads(mesh)
              if payload.skin_weights is not None}
    if len(widths) != 1:
        raise ValueError("Source must have one decoded skinning width for a shader override")
    allowed = (4, 6, 12) if layout is MaterialSkinningLayout.LATE_DXIL_PACKED_10BIT else (4, 8)
    if mesh.profile.supports_packed_rigid_skinning:
        allowed = (1, *allowed)
    width = widths.pop()
    return tuple(count for count in allowed if count <= width)


def source_skinning_contract(mesh, selector=None, *, material_counts=None):
    if selector is not None and material_counts is not None:
        raise ValueError("Choose a source shader or per-material shaders, not both")
    allowed = source_weight_options(mesh)
    width = next(payload.skin_weights.influence_count for payload in mesh_payloads(mesh)
                 if payload.skin_weights is not None)
    layout = _material_layout(mesh.profile)
    counts = (selector,) if material_counts is None else tuple(material_counts.values())
    if not counts or any(type(count) is not int or count not in allowed for count in counts):
        raise ValueError(f"Source shader must select one of {allowed}")
    return MeshSkinningContract(width, default_influence_count=selector,
        material_influence_counts=dict(material_counts) if material_counts is not None else None, material_layout=layout,
        implicit_last_weight=layout is MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8)


def target_skinning_contract(mesh, profile=None, *, influence_count=None):
    profile = profile or mesh.profile
    flags = mesh.header.decoded_content_flags
    if (mesh.header.magic == MPLY_MAGIC or not source_has_material_skinning(profile)
            or (flags is not None and not flags.is_skinning)):
        return None
    streams = [payload.skin_weights for payload in mesh_payloads(mesh) if payload.skin_weights is not None]
    if not streams:
        return None
    width = max(stream.influence_count for stream in streams)
    legacy = _material_layout(profile) is MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8
    needed = max(np.count_nonzero(np.asarray(stream.weights).reshape(stream.vertex_count, width), axis=1)
                 .max(initial=0) for stream in streams)
    selector = (4 if needed <= 4 else 8) if legacy else width
    if influence_count is not None:
        selector = _target_shader_count(profile, influence_count)
    compute_count = ((8 if legacy else width) if mesh.normal_recalc_data is not None
                     and (legacy or profile.normal_compute_uses_packed_skin_width) else None)
    return MeshSkinningContract(width, default_influence_count=selector,
        material_layout=_material_layout(profile), implicit_last_weight=legacy,
        lod0_compute_influence_count=compute_count)


def coalesce_weights(indices, weights):
    weights = weights.copy()
    for lane in range(1, weights.shape[1]):
        for earlier in range(lane):
            duplicate = indices[:, lane] == indices[:, earlier]
            weights[duplicate, earlier] += weights[duplicate, lane]
            weights[duplicate, lane] = 0
    return weights


def quantize_normalized_weights(weights):
    scaled = weights / weights.sum(axis=1, keepdims=True) * 255
    units = np.floor(scaled).astype(np.uint16)
    remaining = 255 - units.sum(axis=1)
    order = np.argsort(-(scaled - units), axis=1, kind="stable")
    increments = np.arange(weights.shape[1])[None, :] < remaining[:, None]
    np.put_along_axis(units, order, np.take_along_axis(units, order, axis=1) + increments, axis=1)
    return units.astype(np.float64) / 255


def _shader_weights(weights, selector, layout, *, implicit=True):
    result = np.zeros_like(weights)
    late = layout is MaterialSkinningLayout.LATE_DXIL_PACKED_10BIT
    if selector > weights.shape[1] or selector not in ((1, 4, 6, 12) if late else (4, 8)):
        raise ValueError(f"Source shader {selector} exceeds or mismatches the decoded skin layout")
    if selector == 1:
        result[:, 0] = 1
        return result
    count = 3 if late and selector == 4 else selector
    normalized = selector == 4
    result[:, :count] = weights[:, :count]
    if normalized:
        totals = result.sum(axis=1, keepdims=True)
        if not np.isfinite(result).all() or np.any(result < 0) or (late and np.any(totals <= 0)):
            raise ValueError("Source material shader has vertices with no usable bone influence")
        # Legacy DXBC produces NaN positions for a zero weight sum.
        np.divide(result, totals, out=result, where=totals != 0)
    elif late or implicit:
        residual = 1.0 - result[:, :count - 1].sum(axis=1)
        if np.any((residual < -1e-5) | (residual > 1 + 1e-5)):
            raise ValueError("Source material shader has invalid residual bone weights")
        result[:, count - 1] = np.clip(residual, 0, 1)
    elif np.any(result.sum(axis=1) <= 0):
        raise ValueError("Zero-total skinning requires a verified normalized source shader")
    return result


def adapt_source_skinning(mesh, source_skinning_contract=None):
    for payload, stream in _source_skinning_streams(mesh, source_skinning_contract):
        payload.skin_weights = stream


def validate_source_skinning(mesh):
    for _payload, _stream in _source_skinning_streams(mesh):
        pass


def equivalent_source_skinning(mesh, contracts):
    def deformation(contract):
        for _payload, stream in _source_skinning_streams(mesh, contract):
            weights = np.asarray(stream.weights).reshape(stream.vertex_count, stream.influence_count)
            indices = np.asarray(stream.deform_indices).reshape(weights.shape)
            weights = coalesce_weights(indices, weights)
            indices = indices.astype(np.int64)
            indices[weights == 0] = -1
            order = np.argsort(indices, axis=1, kind="stable")
            yield np.take_along_axis(indices, order, axis=1), np.take_along_axis(weights, order, axis=1)

    try:
        baseline = tuple(deformation(contracts[0]))
        for contract in contracts[1:]:
            for before, after in zip(baseline, deformation(contract), strict=True):
                if (not np.array_equal(before[0], after[0])
                        or not np.allclose(before[1], after[1], atol=1e-7, rtol=0)):
                    return False
    except ValueError:
        return False
    return True


def _source_skinning_streams(mesh, source_skinning_contract=None):
    profile = mesh.profile
    flags = mesh.header.decoded_content_flags
    if (mesh.header.magic == MPLY_MAGIC or not source_has_material_skinning(profile)
            or (flags is not None and not flags.is_skinning)):
        return
    payloads = [payload for payload in mesh_payloads(mesh) if payload.skin_weights is not None]
    if not payloads:
        return
    contract = source_skinning_contract
    layout = contract.material_layout if contract is not None else _material_layout(profile)
    if layout is MaterialSkinningLayout.NONE:
        layout = _material_layout(profile)
    compute_count = (contract.lod0_compute_influence_count if contract is not None
                     else normal_compute_influence_count(mesh))
    compute_groups = {id(group) for layer in mesh.meshes for lod in layer.lods[:1] for group in lod.mesh_groups}
    limits = {id(payload): np.zeros(payload.vertex_count, dtype=np.uint8) for payload in payloads}
    referenced = {id(payload): np.zeros(payload.vertex_count, dtype=bool) for payload in payloads}
    for _layer, groups, buffers in geometry_layers(mesh):
        for group in groups:
            for submesh in group.submeshes:
                payload = buffers.get(submesh.buffer_index)
                if payload is None or id(payload) not in limits or not submesh.indices_count:
                    continue
                selector = (compute_count if compute_count is not None and id(group) in compute_groups
                            else contract.default_influence_count if contract is not None else 0)
                if contract is not None and selector is None:
                    if not 0 <= submesh.material_index < len(mesh.material_names):
                        raise ValueError("Source skinning references an invalid material")
                    material = mesh.material_names[submesh.material_index]
                    selector = (contract.material_influence_counts or {}).get(material)
                    if selector is None:
                        raise ValueError(f"Source material {material!r} has no resolved skinning shader")
                start = submesh.verts_index_offset
                count = submesh.vert_count
                if count <= 0:
                    faces = payload.index_values
                    first = submesh.faces_index_offset
                    count = max(faces[first:first + submesh.indices_count], default=-1) + 1
                selected = limits[id(payload)][start:start + count]
                if start < 0 or len(selected) != count or np.any((selected != 0) & (selected != selector)):
                    raise ValueError("Overlapping source geometry has conflicting material skinning shaders")
                selected[:] = selector
                referenced[id(payload)][start:start + count] = True
    for payload in payloads:
        stream = payload.skin_weights
        if contract is not None and contract.decoded_influence_count != stream.influence_count:
            raise ValueError("Source shader contract does not match its decoded skinning width")
        weights = np.asarray(stream.weights, dtype=np.float64).reshape(stream.vertex_count, stream.influence_count)
        indices = np.asarray(stream.deform_indices).reshape(weights.shape).copy()
        result = weights.copy()
        selectors = limits[id(payload)]
        active = referenced[id(payload)]
        for selector in np.unique(selectors[active]):
            selected = (selectors == selector) & active
            if selector:
                if selector == 1:
                    joints = packed_rigid_joint_indices(indices[selected])
                    if np.any(joints >= len(mesh.bone_remap_indices)):
                        raise ValueError("Source rigid shader references a missing deform bone")
                    indices[selected, 0] = joints
                result[selected] = _shader_weights(weights[selected], int(selector), layout,
                                                   implicit=contract.implicit_last_weight if contract is not None else True)
                continue
            choices = (4, 6, 12) if layout is MaterialSkinningLayout.LATE_DXIL_PACKED_10BIT else (4, 8)
            if profile.supports_packed_rigid_skinning:
                choices = (1, *choices)
            possible = []
            selected_indices = indices[selected]
            if _shared_legacy_profile(profile):
                possible.append(coalesce_weights(selected_indices, weights[selected]))
            for choice in choices:
                if choice <= stream.influence_count:
                    try:
                        effective = _shader_weights(weights[selected], choice, layout)
                        if choice == 1 and np.any(packed_rigid_joint_indices(selected_indices) != selected_indices[:, 0]):
                            possible.append(None)
                            continue
                        possible.append(coalesce_weights(selected_indices, effective))
                    except ValueError:
                        possible.append(None)
            if any(value is None for value in possible) or any(
                    not np.allclose(possible[0], value, atol=1e-7, rtol=0) for value in possible[1:]):
                raise SourceSkinningAmbiguity(
                    "Source skinning is ambiguous: supported shaders interpret these stored weights differently. "
                    "Select the source MDF or choose a manual source skinning mode")
            result[selected] = possible[0]
        yield payload, replace(stream, weights=array("f", result.ravel()),
                               deform_indices=array("H", indices.ravel()))


def convert_skin_streams(mesh, owners, profile, loss, *, meshlets=False, requirement=None,
                         target_influence_count=None, defer_palette=False):
    owners = tuple(owners)
    streams = [(owner, name, getattr(owner, name, None)) for owner in owners for name in SKIN_WEIGHT_STREAMS]
    streams = [(owner, name, stream) for owner, name, stream in streams if stream is not None]
    shared = _shared_legacy_profile(profile) and not meshlets
    choices = target_weight_options(profile, meshlets=meshlets)
    if target_influence_count is not None:
        if type(target_influence_count) is not int or target_influence_count not in choices:
            raise ValueError(f"Target influence count must be one of {choices} for this geometry and normal layout")
    if not streams:
        return
    flags = mesh.header.decoded_content_flags
    if flags is not None and not flags.is_skinning:
        for owner, name, _stream in streams:
            setattr(owner, name, None)
            loss(f"Removed inactive {name} storage; bone skinning remains disabled")
        return
    limit = profile.max_deform_bones - 1
    used = set()
    needed = 0
    for _owner, _name, stream in streams:
        weights = np.asarray(stream.weights).reshape(stream.vertex_count, stream.influence_count)
        indices = np.asarray(stream.deform_indices).reshape(weights.shape)
        if not np.isfinite(weights).all() or np.any(weights < 0) or np.any(indices < 0):
            raise ValueError("Skin weights must be finite and nonnegative, with valid bone indices")
        used.update(int(value) for value in np.unique(indices[weights > 0]))
        effective_weights = (coalesce_weights(indices, weights)
                             if shared or stream.influence_count > profile.skin_weight_influence_count else weights)
        needed = max(needed, int(np.count_nonzero(effective_weights, axis=1).max(initial=0)))
    if used and max(used) >= len(mesh.bone_remap_indices):
        raise ValueError("Active skin weight references a missing deform bone")

    capacity = profile.skin_weight_influence_count
    if (target_influence_count if target_influence_count is not None else needed) > capacity:
        capacity = skin_weight_capacity(profile, meshlets=meshlets)
    legacy_material = (not meshlets and
                       _material_layout(profile) is MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8)
    if shared and target_influence_count is None and needed > 4:
        raise ValueError("Shared RE3/Resistance skinning is ambiguous; choose an explicit target shader (4 or 8)")
    selector = 4 if needed <= 4 else 8
    if target_influence_count is not None:
        selector = _target_shader_count(profile, target_influence_count)
    effective = target_influence_count or (4 if shared and selector == 4 else capacity)
    implicit = profile.skin_weight_uses_implicit_last_weight
    for owner, name, stream in streams:
        native_weights = np.asarray(stream.weights).reshape(stream.vertex_count, stream.influence_count)
        totals = native_weights.sum(axis=1)
        native_precision = np.all(np.abs(native_weights * 255 - np.rint(native_weights * 255)) <= 1e-5)
        if (not legacy_material and stream.influence_count == capacity
                and stream.declared_influence_count == capacity
                and stream.implicit_last_weight == implicit
                and (not implicit or np.all(np.abs(totals - 1.0) <= 1e-6))
                and (not implicit or native_precision)
                and not np.any(native_weights[:, effective:])
                and max(stream.deform_indices, default=0) <= limit):
            continue
        shape = (stream.vertex_count, stream.influence_count)
        indices = np.asarray(stream.deform_indices).reshape(shape)
        weights = np.asarray(stream.weights, dtype=np.float64).reshape(shape).copy()
        if stream.influence_count > effective:
            weights = coalesce_weights(indices, weights)
        out_indices = np.zeros((stream.vertex_count, capacity), dtype=np.uint16)
        out_weights = np.zeros((stream.vertex_count, capacity), dtype=np.float64)
        count = min(effective, stream.influence_count)
        order = np.argsort(weights == 0, axis=1, kind="stable")[:, :count]
        reduced = np.count_nonzero(weights, axis=1) > effective
        order[reduced] = np.sort(np.argsort(-weights[reduced], axis=1, kind="stable")[:, :count], axis=1)
        out_indices[:, :count] = np.take_along_axis(indices, order, axis=1)
        out_weights[:, :count] = np.take_along_axis(weights, order, axis=1)
        out_indices[out_weights == 0] = 0
        if reduced.any():
            loss(f"{name}: reduced vertices to their {effective} strongest bone influences")
        totals = out_weights.sum(axis=1, keepdims=True)
        if (implicit or legacy_material) and np.any(np.abs(totals - 1.0) > 1e-6):
            loss(f"{name}: normalized weights for the target's "
                 + ("material skinning shader" if legacy_material else "implicit final influence"))
            if np.any(totals <= 0):
                raise ValueError("An unweighted vertex cannot be converted to residual skin weights")
            out_weights /= totals
        elif reduced.any():
            out_weights[reduced] /= totals[reduced]
        if (implicit or (legacy_material and (selector == 8 or mesh.normal_recalc_data is not None))
                or (shared and target_influence_count is None)):
            quantized = quantize_normalized_weights(out_weights)
            error = np.abs(quantized - out_weights).max(initial=0)
            if error > 1e-7:
                loss(f"Target quantizes {name} (maximum weight error {error:.9g})")
            out_weights = quantized
        stored = capacity - int(implicit)
        native_capacity = 16 if capacity == 12 else 8
        setattr(owner, name, SkinWeightBuffer(
            influence_count=capacity, vertex_count=stream.vertex_count,
            deform_indices=array("H", out_indices.ravel()),
            weights=array("f", out_weights.ravel()),
            declared_influence_count=capacity, stored_weight_count=stored,
            native_weight_capacity=native_capacity,
            packed_index_word_count=0 if profile.skin_weight_layout is SkinWeightLayout.BYTE8 else 4 if capacity == 12 else 2,
            implicit_last_weight=implicit,
            runtime_ignored_weight_bytes=bytes(stream.vertex_count * (native_capacity - stored))
            if implicit else b"",
        ))
    compact_skin_palette(mesh, owners, profile, defer=defer_palette)
    if (not meshlets and source_has_material_skinning(profile)
            and (not shared or target_influence_count is not None)):
        shader = selector if legacy_material or target_influence_count is not None else capacity
        mode = "4-weight skinning shader (3 effective influences)" if target_influence_count == 3 else f"{shader}-influence skinning shader"
        (requirement or loss)(f"Target materials must select the {mode} "
                             "to match the exported weights")


def compact_skin_palette(mesh, owners, profile, *, defer=False):
    streams = [(owner, name, stream) for owner in owners for name in SKIN_WEIGHT_STREAMS
               if (stream := getattr(owner, name, None)) is not None]
    if not streams or len(mesh.bone_remap_indices) <= profile.max_deform_bones:
        return
    used = np.unique(np.concatenate([
        np.asarray(getattr(owner, name).deform_indices)[np.asarray(getattr(owner, name).weights) > 0]
        for owner, name, _stream in streams])).tolist()
    if not (defer and len(used) > profile.max_deform_bones):
        if len(used) > profile.max_deform_bones:
            raise ValueError(f"Target supports only {profile.max_deform_bones} deform bones; this mesh needs {len(used)}")
        # The native uploader visits the whole palette, including unused entries.
        used = used or [0]
        table = np.zeros(used[-1] + 1, dtype=np.uint16)
        table[used] = np.arange(len(used), dtype=np.uint16)
        for owner, name, _stream in streams:
            stream = getattr(owner, name)
            indices = np.asarray(stream.deform_indices).copy()
            indices[np.asarray(stream.weights) == 0] = 0
            setattr(owner, name, replace(stream, deform_indices=array("H", table[indices])))
        if mesh.bounding_boxes and len(mesh.bounding_boxes.boxes) == len(mesh.bone_remap_indices):
            mesh.bounding_boxes.boxes = [mesh.bounding_boxes.boxes[index] for index in used]
        mesh.bone_remap_indices = [mesh.bone_remap_indices[index] for index in used]
        mesh.bone_remap_count = len(used)
