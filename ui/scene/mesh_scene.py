from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

lazy import numpy as np

lazy from file_handlers.mesh.contract_utils import layer_payloads, layer_vertex_offset

lazy from .scene_model import SceneDrawBatch, SceneDrawMesh
lazy from .mesh_attributes import (
    SKIN_WEIGHT_ATTRIBUTE_NAMES, MeshAttributeView, merge_attribute,
)
lazy from file_handlers.mesh.normal_recalc import recalculate_mesh_normals


def payload_records_have_complete_attribute(
    records: list["MeshScenePayload"],
    name: str,
    widths: dict[str, int],
) -> bool:
    if not records:
        return False
    is_skin_weight = name in SKIN_WEIGHT_ATTRIBUTE_NAMES
    if not is_skin_weight and name not in widths:
        return False
    for record in records:
        value = getattr(record.payload, name, None)
        required_vertices = record.source_vertex_start + record.vertex_count
        if is_skin_weight:
            if value is None or value.vertex_count < required_vertices:
                return False
            continue
        if value is None or len(value) < required_vertices * widths[name]:
            return False
    return True


@dataclass(frozen=True, slots=True)
class MeshScenePayload:
    buffer_index: int
    payload: object
    vertex_count: int
    vertex_base: int
    source_vertex_start: int = 0


def mesh_geometry_choices(mesh):
    if mesh is None:
        return []
    main = max((len(data.lods) for data in mesh.meshes), default=0)
    choices = [("main", lod) for lod in range(main)]
    for layer in ("shadow", "occluder"):
        owner = getattr(mesh, f"{layer}_mesh", None)
        choices.extend((layer, lod) for lod in range(len(getattr(owner, "geometry_lods", ()) or ())))
    return choices


def mesh_lod0_submeshes(mesh) -> list[object]:
    return [submesh for _part, submesh in mesh_lod_parts(mesh)]


def mesh_lod_parts(
    mesh,
    lod_index: int = 0,
    *,
    layer: str = "main",
) -> list[tuple[int, object]]:
    parts = []
    if layer == "main":
        lods = [
            mesh_data.lods[lod_index]
            for mesh_data in mesh.meshes
            if 0 <= lod_index < len(mesh_data.lods)
        ]
    elif layer in {"shadow", "occluder"}:
        owner = getattr(mesh, f"{layer}_mesh", None)
        geometry_lods = getattr(owner, "geometry_lods", ())
        lods = (
            [geometry_lods[lod_index]]
            if 0 <= lod_index < len(geometry_lods)
            else []
        )
    else:
        raise ValueError(f"Unknown mesh geometry layer: {layer}")
    for lod in lods:
        for group_index, group in enumerate(lod.mesh_groups):
            part_index = int(getattr(group, "group_id", group_index))
            parts.extend((part_index, submesh) for submesh in group.submeshes)
    return parts


def mesh_scene_payloads(
    mesh,
    submeshes: list[object] | None = None,
    *,
    lod_index: int = 0,
    layer: str = "main",
) -> list[MeshScenePayload]:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return []
    submeshes = (
        [item for _part, item in mesh_lod_parts(mesh, lod_index, layer=layer)]
        if submeshes is None
        else submeshes
    )
    payloads = layer_payloads(mesh, layer)
    vertex_spans: dict[int, tuple[int, int]] = {}
    for submesh in submeshes:
        buffer_index = int(submesh.buffer_index)
        payload = payloads.get(buffer_index)
        if payload is None:
            raise ValueError(f"Missing mesh buffer {buffer_index}")
        available = len(payload.positions) // 3
        base = layer_vertex_offset(mesh, layer, submesh)
        count = int(getattr(submesh, "vert_count", 0))
        end = base + count if count > 0 else available
        if base < 0 or end < base or end > available:
            raise ValueError(
                f"Invalid LOD {lod_index} vertex span [{base}, {end}) "
                f"in mesh buffer {buffer_index}"
            )
        previous = vertex_spans.get(buffer_index)
        vertex_spans[buffer_index] = (
            min(base, previous[0]) if previous else base,
            max(end, previous[1]) if previous else end,
        )
    records: list[MeshScenePayload] = []
    vertex_base = 0
    for buffer_index in sorted(vertex_spans):
        if buffer_index not in payloads:
            raise ValueError(f"Missing mesh buffer {buffer_index}")
        payload = payloads[buffer_index]
        positions = np.asarray(payload.positions, dtype=np.float32).reshape(-1)
        if positions.size % 3:
            raise ValueError(f"Malformed positions in mesh buffer {buffer_index}")
        source_start, source_end = vertex_spans[buffer_index]
        vertex_count = source_end - source_start
        if not vertex_count:
            continue
        records.append(MeshScenePayload(
            buffer_index,
            payload,
            vertex_count,
            vertex_base,
            source_start,
        ))
        vertex_base += vertex_count
    return records


def _bone_modifier_visualization(mesh, records) -> np.ndarray:
    blend_data = getattr(mesh, "blend_shape_data", None)
    modifier = getattr(blend_data, "bone_modifier", None)
    effectors = getattr(modifier, "effectors", ())
    if not effectors:
        raise ValueError("No bone-modifier effectors are available")
    vertex_count = sum(record.vertex_count for record in records)
    colors = np.full((vertex_count, 4), (0.035, 0.035, 0.035, 1.0), dtype=np.float32)
    resident = next((record for record in records if record.buffer_index == 0), None)
    if resident is None:
        raise ValueError("Bone modifiers require the resident position buffer")
    source_start, count = resident.source_vertex_start, resident.vertex_count
    destination_base = resident.vertex_base
    source_end = source_start + count
    for effector in effectors:
        joint = int(effector.joint_index)
        rgb = np.asarray(
            (
                ((joint * 97 + 17) % 251) / 250.0,
                ((joint * 57 + 83) % 253) / 252.0,
                ((joint * 23 + 191) % 255) / 254.0,
            ),
            dtype=np.float32,
        )
        for vertex_index in effector.vertex_indices:
            vertex_index = int(vertex_index)
            if source_start <= vertex_index < source_end:
                colors[
                    destination_base + vertex_index - source_start,
                    :3,
                ] = rgb
    return colors


def deform_mesh_vertices(mesh, vertices, normals, records, *, lod_index=0,
                         blend_shape_channel=None, blend_shape_weight=0.0):
    if blend_shape_channel is None or not blend_shape_weight:
        return vertices, normals
    payloads = {record.buffer_index: record for record in records}
    blend_data = getattr(mesh, "blend_shape_data", None)
    body = next(
        (
            item
            for item in getattr(blend_data, "bodies", ())
            if item.lod_index == lod_index
        ),
        None,
    )
    if body is not None:
        channel = next(
            (
                item
                for item in body.channels
                if (
                    item.index == blend_shape_channel
                    if isinstance(blend_shape_channel, int)
                    else item.name == blend_shape_channel
                )
            ),
            None,
        )
        if channel is not None:
            vertices = vertices.copy()
            normals_changed = False
            for segment in channel.segments:
                record = payloads.get(segment.buffer_index)
                if record is None:
                    continue
                start = segment.base_vertex_location - record.source_vertex_start
                count = len(segment.position_deltas)
                if start < 0 or start + count > record.vertex_count:
                    raise ValueError(
                        f"Blendshape channel {channel.index} exceeds LOD "
                        f"{lod_index} geometry in buffer {segment.buffer_index}"
                    )
                destination = record.vertex_base + start
                end = destination + count
                affected_vertices = vertices[destination:end]
                affected_vertices += segment.position_contribution(
                    blend_shape_weight
                )
                vertex_cull_mask = segment.vertex_cull_mask
                if vertex_cull_mask is not None:
                    if np.shape(vertex_cull_mask) != (len(affected_vertices),):
                        raise ValueError(
                            f"Blendshape channel {channel.index} has a "
                            "mismatched vertex-cull mask"
                        )
                    vertex_cull_mask = np.asarray(
                        vertex_cull_mask,
                        dtype=np.bool_,
                    )
                    if np.any(vertex_cull_mask):
                        affected_vertices[vertex_cull_mask] = np.nan
                if normals is not None and segment.target_normals is not None:
                    if not normals_changed:
                        normals = normals.copy()
                        normals_changed = True
                    normals[destination:end] += (
                        segment.target_normals - normals[destination:end]
                    ) * float(blend_shape_weight)
            if normals_changed:
                lengths = np.linalg.norm(normals, axis=1, keepdims=True)
                np.divide(normals, lengths, out=normals, where=lengths > 1e-12)
    return vertices, normals


def build_mesh_scene(
    mesh,
    *,
    key: str = "mesh",
    color: tuple[float, float, float] | tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0),
    force_solid: bool = False,
    ignore_highlight_filter: bool = False,
    include_vertex_colors: bool = True,
    material_key: Callable[[str], str] | None = None,
    lod_index: int = 0,
    layer: str = "main",
    blend_shape_channel: int | str | None = None,
    blend_shape_weight: float = 0.0,
    visualize_attribute: str | None = None,
    attribute_view: MeshAttributeView | None = None,
    normal_recalculation_enabled: bool = True,
    normal_recalc_edge_exclusion: bool = False,
) -> list[SceneDrawMesh]:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return []

    payloads = layer_payloads(mesh, layer)
    parts = mesh_lod_parts(mesh, lod_index, layer=layer)
    submeshes = [submesh for _part, submesh in parts]
    if not payloads or not submeshes:
        return []
    records = mesh_scene_payloads(mesh, submeshes, lod_index=lod_index, layer=layer)
    if not records:
        return []
    spans = {record.buffer_index: record for record in records}
    vertex_chunks = [
        np.asarray(record.payload.positions, dtype=np.float32).reshape(-1, 3)[
            record.source_vertex_start:record.source_vertex_start + record.vertex_count]
        for record in records
    ]

    vertices = np.concatenate(vertex_chunks)
    normals = merge_attribute(records, "normals", 3, np.float32)
    tangent_xyz = merge_attribute(records, "tangents", 3, np.float32)
    tangent_w = merge_attribute(
        records,
        "tangent_handedness",
        1,
        np.float32,
    )
    tangents = None
    if tangent_xyz is not None or tangent_w is not None:
        tangent_count = (
            len(tangent_xyz) if tangent_xyz is not None else len(tangent_w)
        )
        tangents = np.column_stack(
            (
                tangent_xyz
                if tangent_xyz is not None
                else np.zeros((tangent_count, 3), dtype=np.float32),
                tangent_w[:, 0]
                if tangent_w is not None
                else np.ones(tangent_count, dtype=np.float32),
            )
        )
    colors = merge_attribute(records, "colors", 4, np.uint8) if include_vertex_colors else None
    if colors is not None:
        colors = colors.astype(np.float32) / 255.0
    uvs = merge_attribute(records, "uv0", 2, np.float32)
    uv1 = merge_attribute(records, "uv1", 2, np.float32)
    uv2 = merge_attribute(records, "uv2", 2, np.float32)
    if layer == "main":
        vertices, normals = deform_mesh_vertices(
            mesh, vertices, normals, records, lod_index=lod_index,
            blend_shape_channel=blend_shape_channel, blend_shape_weight=blend_shape_weight,
        )

    index_chunks: list[np.ndarray] = []
    batches: list[SceneDrawBatch] = []
    material_names = mesh.material_names

    for submesh_number, (part_index, submesh) in enumerate(parts):
        buffer_index = submesh.buffer_index
        payload = payloads[buffer_index]
        span = spans.get(buffer_index)
        if span is None:
            raise ValueError(f"No positions in mesh buffer {buffer_index}")
        face_array = (
            payload.integer_faces
            if payload.integer_faces is not None
            else payload.faces
        )
        start, count = submesh.faces_index_offset, submesh.indices_count
        if not count:
            continue
        end = start + count
        is_quad = bool(getattr(submesh, "is_quad", False))
        primitive_width = 4 if is_quad else 3
        if start < 0 or count < 0 or end > len(face_array) or count % primitive_width:
            raise ValueError(f"Invalid index span [{start}, {end})")
        vertex_offset = layer_vertex_offset(mesh, layer, submesh)
        raw_indices = np.asarray(face_array[start:end], dtype=np.uint64)
        if is_quad:
            quads = raw_indices.reshape(-1, 4)
            raw_indices = np.column_stack(
                (
                    quads[:, 0],
                    quads[:, 1],
                    quads[:, 2],
                    quads[:, 0],
                    quads[:, 2],
                    quads[:, 3],
                )
            ).reshape(-1)
        # The spans hold every submesh's vertices from its offset (``mesh_scene_payloads``).
        local_indices = raw_indices + vertex_offset
        if np.any(local_indices >= span.source_vertex_start + span.vertex_count):
            raise ValueError(f"Vertex exceeds selected LOD span in mesh buffer {buffer_index}")
        batch_indices = (local_indices - span.source_vertex_start + span.vertex_base).astype(np.uint32)
        index_chunks.append(batch_indices)
        material_index = submesh.material_index
        if visualize_attribute is not None:
            material_name = ""
        else:
            material_name = (
                material_names[material_index]
                if 0 <= material_index < len(material_names)
                else ""
            )
            if material_key is not None:
                material_name = material_key(material_name)
        batches.append(
            SceneDrawBatch(
                indices=batch_indices,
                material_name=material_name,
                part_index=part_index,
                alpha_test=visualize_attribute is None and bool(getattr(submesh, "alpha_test", False)),
                transparent=visualize_attribute is None and bool(getattr(submesh, "transparent", False)),
                submesh=(lod_index, submesh_number) if layer == "main" else None,
            )
        )

    if not index_chunks:
        return []

    indices = np.concatenate(index_chunks)
    if (
        layer == "main"
        and normal_recalculation_enabled
        and getattr(mesh, "normal_recalc_data", None) is not None
    ):
        recalculated = recalculate_mesh_normals(
            mesh,
            vertices,
            indices,
            edge_exclusion_enabled=normal_recalc_edge_exclusion,
            preserved_normals=normals,
            lod_index=lod_index,
        )
        if recalculated is not None:
            normals = recalculated

    if visualize_attribute is not None:
        if visualize_attribute == "bone_modifier_effectors":
            colors = _bone_modifier_visualization(mesh, records)
        else:
            attribute_view = attribute_view or MeshAttributeView(records, visualize_attribute)
            colors = attribute_view.colors(normals=normals, tangents=tangents)

    bounds_minimum = bounds_maximum = None
    if getattr(mesh, "meshes", None):
        bounding_box = getattr(mesh.meshes[0], "bounding_box", None)
        if bounding_box is not None:
            minimum, maximum = bounding_box
            bounds_minimum = np.asarray(minimum[:3], dtype=np.float32)
            bounds_maximum = np.asarray(maximum[:3], dtype=np.float32)

    return [
        SceneDrawMesh(
            key=key,
            vertices=vertices,
            indices=indices,
            color=color,
            force_solid=force_solid or visualize_attribute is not None,
            ignore_highlight_filter=ignore_highlight_filter,
            normals=normals,
            tangents=tangents,
            uvs=uvs,
            uv1=uv1,
            uv2=uv2,
            colors=colors,
            batches=batches,
            bounds_minimum=bounds_minimum,
            bounds_maximum=bounds_maximum,
            edge_excluded=normal_recalc_edge_exclusion,
        )
    ]
