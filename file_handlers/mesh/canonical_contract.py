from __future__ import annotations

from collections.abc import Iterable
import math

from .contract_utils import (
    ContractValidator,
    all_groups,
    all_lods,
    assert_canonical_layout_constants,
    assert_zero_index_padding,
    has_nonzero,
    layer_groups,
)
from .conversion_geometry import mesh_payloads
from .mesh_file import SKIN_WEIGHT_STREAMS


_LAYOUT_VALIDATOR = ContractValidator("Canonical MESH contract")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Canonical MESH contract: {message}")


def _weight_streams(mesh: object) -> Iterable[object]:
    return tuple(
        stream
        for payload in mesh_payloads(mesh)
        for stream in (getattr(payload, name) for name in SKIN_WEIGHT_STREAMS)
        if stream is not None
    )


def _assert_vertex_layout(
    payload: object,
    vertex_count: int,
    label: str,
    *,
    expected_start: int,
    allow_sparse_physical_backing: bool = False,
) -> int:
    headers = tuple(payload.buffer_headers)
    types = tuple(int(header.type) for header in headers)
    _require(
        types == tuple(sorted(set(types))),
        f"{label} vertex elements must use unique ascending semantic slots",
    )
    if not headers:
        _require(
            int(vertex_count) == 0,
            f"{label} has a vertex topology but no element layout",
        )
        return int(expected_start)
    _require(
        int(headers[0].type) == 0,
        f"{label} element layout must begin with Position",
    )
    expected_offset = int(expected_start)
    streams = tuple(getattr(payload, "attribute_streams", ()))
    for header_index, header in enumerate(headers):
        _require(
            int(header.offset) == expected_offset,
            f"{label} element {int(header.type)} does not follow the previous "
            "declared physical plane",
        )
        expected_offset += (
            int(streams[header_index].native_stored_byte_length)
            if allow_sparse_physical_backing and streams
            else int(header.size) * int(vertex_count)
        )
    return expected_offset


def _assert_index_padding(
    groups: Iterable[object],
    payloads: dict[int, object],
    label: str,
) -> None:
    for group_index, group in enumerate(groups):
        for cluster_index, submesh in enumerate(group.submeshes):
            payload = payloads.get(int(submesh.buffer_index))
            _require(
                payload is not None,
                f"{label} group {group_index} cluster {cluster_index} has no "
                "owning index payload",
            )
            _require(
                int(submesh.faces_index_offset) + int(submesh.allocated_index_count)
                <= len(payload.index_values),
                f"{label} group {group_index} cluster {cluster_index} index "
                "allocation leaves its semantic buffer domain",
            )


def _assert_name_domains(mesh: object) -> None:
    header = mesh.header
    _require(
        len(mesh.names) == int(header.name_count),
        f"name table has {len(mesh.names)} strings, expected {header.name_count}",
    )

    material_count = (
        int(mesh.meshes[0].material_count) if getattr(mesh, "meshes", ()) else 0
    )
    _require(
        len(mesh.material_indices) == material_count,
        "material-name index count must equal MeshData.materialCount",
    )
    _require(
        len(mesh.bone_indices) == int(mesh.joint_count),
        "joint-name index count must equal SkeletonLayout.jointCount",
    )

    channel_count = int(header.name_count) - material_count - int(mesh.joint_count)
    _require(channel_count >= 0, "global name-table partition is negative")
    indexed_domains = [
        ("material", mesh.material_indices),
        ("joint", mesh.bone_indices),
    ]
    if mesh.is_full:
        _require(
            len(mesh.blend_shape_indices) == channel_count,
            "channel-name index count must equal "
            "nameCount-materialCount-jointCount",
        )
        indexed_domains.append(("blend-shape channel", mesh.blend_shape_indices))
    for label, values in indexed_domains:
        _require(
            all(0 <= int(value) < int(header.name_count) for value in values),
            f"{label} name index leaves the global string table",
        )


def _assert_blend_shape_domains(mesh: object) -> None:
    blend = getattr(mesh, "blend_shape_data", None)
    if blend is None:
        return
    _require(
        not blend.unresolved_semantics,
        "a complete profile produced unresolved blend-shape semantics: "
        + "; ".join(blend.unresolved_semantics),
    )
    _require(
        not blend.layout_unknown_fields,
        "complete profiles must not expose anonymous blend-layout fields",
    )


def _assert_topology_domains(mesh: object) -> None:
    raw_count_map = dict(mesh.profile.skin_weight_influences_by_raw)
    for mesh_data in getattr(mesh, "meshes", ()):
        _require(
            int(mesh_data.skin_weight_count_raw) in raw_count_map,
            "MeshData SkinWeightCount is not a canonical build encoding",
        )
        if mesh.profile.mesh_data_has_modern_bounds:
            expected_padding = int(mesh.profile.canonical_modern_aabb_padding)
            _require(
                int(mesh_data.chunk) == 0
                and int(mesh_data.aabb_min_padding) == expected_padding
                and int(mesh_data.aabb_max_padding) == expected_padding,
                "modern MeshData AABB padding must use the profile-owned "
                "native serializer constant",
            )
        else:
            _require(
                float(mesh_data.bounding_box[0][3]) == 1.0
                and float(mesh_data.bounding_box[1][3]) == 1.0,
                "MeshData AABB homogeneous W lanes must both equal 1.0",
            )
        _require(
            len(mesh_data.lod_offsets) >= len(mesh_data.lods),
            "parsed LODs exceed the MeshData LOD pointer table",
        )
    for _label, _index, lod in all_lods(mesh):
        _require(
            len(lod.group_offsets) >= len(lod.mesh_groups),
            "parsed mesh groups exceed the LOD group-pointer table",
        )
    for group in all_groups(mesh):
        _require(
            len(group.submeshes) == int(group.cluster_count),
            "MeshGroup clusterCount does not own its MeshCluster array",
        )
        _require(
            sum(int(submesh.vert_count) for submesh in group.submeshes)
            == int(group.vertex_count),
            "MeshGroup vertexCount does not partition its MeshCluster domains",
        )
        _require(
            sum(int(submesh.allocated_index_count) for submesh in group.submeshes)
            == int(group.face_count),
            "MeshGroup indexCount does not own its allocated draw ranges",
        )
        for submesh, next_submesh in zip(group.submeshes, group.submeshes[1:]):
            _require(
                int(next_submesh.verts_index_offset)
                == int(submesh.verts_index_offset) + int(submesh.vert_count),
                "ordered MeshCluster baseVertex values do not partition "
                "MeshGroup.vertexCount",
            )
            _require(
                int(next_submesh.faces_index_offset)
                == int(submesh.faces_index_offset)
                + int(submesh.allocated_index_count),
                "MeshCluster index allocations must be consecutive",
            )


def _assert_buffer_domains(mesh: object) -> None:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return
    counts = mesh_buffer.vertex_counts_by_buffer
    index_counts = mesh_buffer.index_counts_by_buffer
    index_stride = 4 if mesh_buffer.has_32bit_indices else 2
    vertex_layout_ends: dict[int, int] = {}
    for buffer_index, vertex_count in counts.items():
        payload = mesh_buffer.buffer_payloads.get(buffer_index)
        _require(payload is not None, f"buffer {buffer_index} has topology but no payload")
        _require(
            int(payload.vertex_count) == int(vertex_count),
            f"buffer {buffer_index} vertex domain is not topology-owned",
        )
        _require(
            not payload.unknown_attributes,
            f"buffer {buffer_index} contains an untyped vertex stream",
        )
        vertex_layout_ends[int(buffer_index)] = _assert_vertex_layout(
            payload,
            int(vertex_count),
            f"buffer {buffer_index}",
            expected_start=0,
        )
        _require(
            len(payload.attribute_streams) == len(payload.buffer_headers),
            f"buffer {buffer_index} element headers do not own exactly one "
            "decoded attribute stream each",
        )
        expected_index_bytes = int(index_counts.get(buffer_index, 0)) * index_stride
        _require(
            len(payload.face_bytes) == expected_index_bytes,
            f"buffer {buffer_index} index payload is not topology-sized",
        )

    info = getattr(mesh, "streaming_info", None)
    if info is not None:
        _require(
            len(mesh_buffer.streaming_buffer_headers) == mesh.profile.streaming_layout_count(info.entries),
            "StreamingInfo entries do not own the streaming BufferLayout array",
        )

    main_groups = [group for mesh_data in mesh.meshes for group in layer_groups(mesh_data)]
    _assert_index_padding(
        main_groups,
        mesh_buffer.buffer_payloads,
        "main mesh",
    )

    for label, layer in (("shadow mesh", mesh.shadow_mesh), ("occluder mesh", mesh.occluder_mesh)):
        if layer is None:
            continue
        groups = layer_groups(layer)
        payloads = dict(layer.payloads)
        _assert_index_padding(
            groups,
            payloads,
            label,
        )
        for buffer_index, payload in payloads.items():
            main_payload = mesh_buffer.buffer_payloads.get(buffer_index)
            if (
                main_payload is not None
                and tuple(payload.buffer_headers)
                == tuple(main_payload.buffer_headers)
            ):
                continue
            vertex_count = int(layer.vertex_counts_by_buffer.get(buffer_index, 0))
            _require(
                int(payload.vertex_count) == vertex_count,
                f"{label} buffer {buffer_index} vertex domain is not "
                "topology-owned",
            )
            vertex_layout_ends[int(buffer_index)] = _assert_vertex_layout(
                payload,
                vertex_count,
                f"{label} buffer {buffer_index}",
                expected_start=vertex_layout_ends.get(int(buffer_index), 0),
                allow_sparse_physical_backing=bool(
                    getattr(payload, "is_auxiliary_geometry", False)
                    and getattr(
                        payload,
                        "auxiliary_vertex_planes_may_omit_zero_suffix",
                        False,
                    )
                ),
            )


def _assert_misc_domains(mesh: object) -> None:
    pivots = getattr(mesh, "group_pivots", None)
    if pivots is not None:
        _require(
            int(pivots.buffer_size) == len(pivots.pivots) * 12,
            "GroupPivotLayout.bufferSize must own its Float3 array",
        )
        _require(
            all(
                math.isfinite(float(component))
                for pivot in pivots.pivots
                for component in pivot
            ),
            "GroupPivotLayout contains a nonfinite coordinate",
        )
    bounds = getattr(mesh, "bounding_boxes", None)
    if bounds is not None:
        for box_index, box in enumerate(bounds.boxes):
            _LAYOUT_VALIDATOR.assert_finite_ordered_aabb(
                f"BoundingBoxLayout box {box_index}",
                box.minimum,
                box.maximum,
            )
    shadow = getattr(mesh, "shadow_mesh", None)
    if shadow is not None:
        raw_count_map = dict(mesh.profile.skin_weight_influences_by_raw)
        _require(
            int(shadow.skin_weight_count) in raw_count_map,
            "ShadowMeshLayout SkinWeightCount is not a canonical build encoding",
        )

    for mesh_index, mesh_data in enumerate(getattr(mesh, "meshes", ())):
        sphere = tuple(mesh_data.bounding_sphere)
        minimum, maximum = mesh_data.bounding_box
        _require(
            all(math.isfinite(float(value)) for value in sphere),
            f"MeshData {mesh_index} bounding sphere must be finite",
        )
        _LAYOUT_VALIDATOR.assert_finite_ordered_aabb(
            f"MeshData {mesh_index} AABB",
            minimum,
            maximum,
        )
        for lod_index, lod in enumerate(mesh_data.lods):
            _require(
                math.isfinite(float(lod.lod_factor))
                and float(lod.lod_factor) >= 0.0
                or (mesh.profile.ordinary_lod_factor_supports_nan
                    and math.isnan(float(lod.lod_factor))),
                f"MeshData {mesh_index} LOD {lod_index} factor must be finite "
                "and nonnegative",
            )

    if mesh.parts_name_hashes:
        _require(bool(mesh.meshes and mesh.meshes[0].lods), "part hashes need LOD0")
        expected = int(mesh.meshes[0].lods[0].part_count)
        _require(
            len(mesh.parts_name_hashes) == expected,
            "ordinary part-hash count must equal LOD0 partCount",
        )


def assert_mesh_semantic_domains(mesh: object) -> None:
    _assert_name_domains(mesh)
    _assert_blend_shape_domains(mesh)
    _assert_topology_domains(mesh)
    _assert_buffer_domains(mesh)
    _assert_misc_domains(mesh)


assert_complete_mesh_runtime_contract = assert_mesh_semantic_domains


def assert_complete_mesh_import_safe(mesh: object) -> None:
    assert_complete_mesh_runtime_contract(mesh)
    assert_mesh_canonical_storage(mesh)
    permitted = mesh.profile.canonical_vertex_semantics
    _require(permitted is not None, "profile has no canonical vertex-slot policy")
    _require(
        mesh.profile.canonical_ordinary_supports_32bit_indices
        or not mesh.mesh_buffer.has_32bit_indices,
        f"{mesh.profile.display_name} 32-bit indices are corpus-absent "
        "and cannot be authored",
    )
    for payload in mesh_payloads(mesh):
        for element in payload.buffer_headers:
            _require(
                int(element.type) in permitted,
                f"{mesh.profile.display_name} vertex semantic {int(element.type)} "
                "is corpus-absent and cannot be authored",
            )


def assert_mesh_canonical_storage(mesh: object) -> None:
    assert_canonical_layout_constants(_LAYOUT_VALIDATOR, mesh)
    _require(mesh.is_full, "canonical serialization requires a FULL load")
    header = mesh.header
    flags = header.decoded_content_flags
    if flags is not None:
        _require(not flags.reserved_bits, "ContentFlags reserved bits must be zero")
    _require(
        not (int(header.content_flags_storage_raw) >> 24),
        "ContentFlags storage high byte must be zero",
    )
    _require(not int(header.header_pad), "MeshHeader alignment padding must be zero")
    _require(
        not int(header.legacy_solved_offset),
        "legacy solved-offset storage must be zero",
    )
    _require(
        not int(header.legacy_light_map_raw),
        "legacy light-map metadata is corpus-unused and must be zero",
    )
    _require(
        not int(header.light_map_reserved),
        "light-map metadata reserved bits must be zero",
    )
    _require(
        not int(header.light_map_padding),
        "light-map metadata alignment padding must be zero",
    )

    mesh_buffer = mesh.mesh_buffer
    if mesh_buffer is not None:
        _require(not int(mesh_buffer.rt_padding), "RT BufferLayout padding must be zero")
        if mesh.profile.shape_key_weight_pointer_is_corpus_zero:
            _require(
                not int(mesh_buffer.shapekey_weight_buffer_size),
                "corpus-absent shape-key weight storage must remain absent",
            )
        if not mesh.profile.supports_secondary_skin_weights:
            for payload in mesh_buffer.buffer_payloads.values():
                _require(
                    payload.extra_skin_weights is None
                    and not payload.skin_weight_continuation_bytes,
                    "corpus-absent type-7 skin streams must remain absent",
                )
        if mesh.profile.shape_key_weight_pointer_is_corpus_zero:
            for stream_index, stream_header in enumerate(mesh_buffer.streaming_buffer_headers.values()):
                _require(
                    not int(stream_header.shapekey_weight_buffer_size),
                    f"streaming buffer {stream_index} corpus-absent shape-key "
                    "storage must remain absent",
                )
        assert_zero_index_padding(mesh, _LAYOUT_VALIDATOR)

    for mesh_data in mesh.meshes:
        _require(not int(mesh_data.reserve), "MeshData reserve must be zero")
        if mesh.profile.legacy_reserved_storage_is_corpus_zero:
            _require(
                not int(mesh_data.shared_lod_bits) and not int(mesh_data.ukn1),
                "legacy MeshData reserved storage must be zero",
            )
        for lod in mesh_data.lods:
            _require(not int(lod.reserve), "MeshLOD reserve must be zero")
            if mesh.profile.legacy_reserved_storage_is_corpus_zero:
                _require(
                    not int(lod.vertex_format),
                    "corpus-zero MeshLOD.vertexFormat must be zero",
                )

    for group in all_groups(mesh):
        _require(not any(group.reserve), "MeshGroup reserve bytes must be zero")
        for submesh in group.submeshes:
            _require(not int(submesh.reserve), "MeshCluster reserve must be zero")
            _require(
                not int(submesh.streaming_offset)
                and not int(submesh.streaming_offset2),
                "MeshCluster streaming reserve offsets must be zero",
            )
            _require(
                not int(submesh.ukn1)
                and not int(submesh.runtime_ignored_tail_residue),
                "MeshCluster producer/reserved storage must be zero",
            )

    # Every profile that reaches here keeps legacy reserved storage at zero, the occluder selectors included.
    _LAYOUT_VALIDATOR.assert_auxiliary_reserves(mesh)
    if mesh.group_pivots is not None:
        _require(not int(mesh.group_pivots.reserve), "GroupPivotLayout reserve must be zero")
    if mesh.bounding_boxes is not None:
        _require(not int(mesh.bounding_boxes.reserve), "BoundingBoxLayout reserve must be zero")

    blend = mesh.blend_shape_data
    if blend is not None:
        _require(not any(blend.layout_reserve), "blend-layout reserve must be zero")
        _require(
            not any(blend.legacy_aux_offsets),
            "legacy blend auxiliary pointers must be absent",
        )
        _require(not blend.legacy_blend_s, "legacy blend auxiliary data must be absent")
        _require(not blend.legacy_blend_ss, "legacy blend channel map must be absent")
        profile = mesh.profile
        ignored_fields = (
            *(("position_w",) if profile.blend_shape_standard_w_is_padding
              or profile.blend_shape_standard_w_is_runtime_ignored else ()),
            *(("normal_w",) if profile.blend_shape_normal_w_is_padding
              or profile.blend_shape_normal_w_is_runtime_ignored else ()),
            *(("deform_percent",) if profile.blend_shape_deform_percent_is_runtime_ignored else ()),
        )
        for body in blend.bodies:
            _require(not any(body.reserve), "blend-body reserve must be zero")
            for target in (*body.targets, *body.none_targets):
                _require(
                    target.raw_reserve is None or not any(target.raw_reserve),
                    "blend-target reserve must be zero",
                )
                if (
                    target.aabb is not None
                    and mesh.profile.blend_shape_aabb_w_is_runtime_ignored
                ):
                    _require(
                        not float(target.aabb.minimum_w)
                        and not float(target.aabb.maximum_w),
                        "runtime-unread blend-target AABB W lanes must be zero",
                    )
            for channel in body.channels:
                for segment in channel.segments:
                    for field in ignored_fields:
                        _require(
                            not has_nonzero(getattr(segment, field, None)),
                            f"runtime-unread blend channel {channel.index} "
                            f"{field} storage must be zero",
                        )
        if blend.bone_modifier is not None:
            _require(
                not int(blend.bone_modifier.reserve),
                "BlendShapeBoneModifier reserve must be zero",
            )

    normal = mesh.normal_recalc_data
    if (
        normal is not None
        and not mesh.profile.normal_redirect_annotation_is_runtime_semantic
    ):
        redirects = [*normal.redirects]
        redirects.extend(
            redirect
            for lod in normal.lods
            for redirect in lod.redirects
        )
        _require(
            not any(bool(redirect.annotation) for redirect in redirects),
            "runtime-masked normal-redirect annotation bits must be zero",
        )

    for stream in _weight_streams(mesh) if mesh_buffer is not None else ():
        if int(stream.influence_count) == 6:
            # SF6 rebuilds influence six from bytes 8..12 and never reads bytes 13..15.
            _require(
                not any(stream.explicit_sixth_weight_bytes),
                "runtime-unread sixth skin-weight cache must be zero",
            )
            _require(
                not any(stream.unused_seventh_weight_bytes),
                "unused seventh skin-weight lane must be zero",
            )
            _require(
                not any(stream.unused_eighth_weight_bytes),
                "unused eighth skin-weight lane must be zero",
            )
            _require(
                not any(stream.packed_index_reserved_bits),
                "packed skin-index reserve fields must be zero",
            )
        elif stream.implicit_last_weight:
            _require(
                not any(stream.runtime_ignored_weight_bytes),
                "runtime-unread final skin-weight storage must be zero",
            )


__all__ = [
    "assert_complete_mesh_import_safe",
    "assert_complete_mesh_runtime_contract",
    "assert_mesh_semantic_domains",
    "assert_mesh_canonical_storage",
]
