from __future__ import annotations

import math
from functools import partial

lazy import numpy as np

from .contract_utils import (
    ContractValidator,
    all_groups,
    all_lods,
    assert_canonical_layout_constants,
    assert_zero_index_padding,
    split_auxiliary_layouts,
)
from .models import MESH_MAGIC, MPLY_MAGIC
from .profiles.base import Half2ValueDomain
lazy from .canonical_contract import assert_mesh_semantic_domains


RE9_VERTEX_STRIDES = {
    0: 12,  # Position: float3
    1: 8,   # Normal/tangent: two R8G8B8A8_SNORM records
    2: 4,   # UV0: half2
    3: 4,   # UV1: half2
    4: 16,  # Weight8
    5: 4,   # Color: R8G8B8A8_UNORM
    6: 4,   # UV2/light-map UV: half2
    7: 16,
}

_VALIDATOR = ContractValidator("Resident Evil Requiem canonical contract")
_fail = _VALIDATOR.fail
_require = _VALIDATOR.require
_require_zero = _VALIDATOR.require_zero
_require_equal = _VALIDATOR.require_equal


_assert_element_headers = partial(
    _VALIDATOR.assert_element_headers,
    strides=RE9_VERTEX_STRIDES,
    unknown_semantic_expectation="must be a modeled RE9 vertex semantic",
    forbidden_canonical_semantics={
        7: "must not use corpus-absent RE9 vertex semantic 7",
    },
)


def _assert_half2_value_domain(
    field: str,
    values: object,
    domain: Half2ValueDomain,
) -> None:
    array_value = np.asarray(values, dtype=np.float64).reshape(-1)
    nan_mask = np.isnan(array_value)
    finite_mask = np.isfinite(array_value)
    if domain is not Half2ValueDomain.IEEE754:
        _require_zero(f"{field} NaN values", nan_mask)
    if domain is Half2ValueDomain.FINITE:
        _require(
            bool(finite_mask.all()),
            field,
            "must contain only finite binary16 values",
            "infinity",
        )
    finite_values = np.abs(array_value[finite_mask])
    _require(
        bool((finite_values <= np.finfo(np.float16).max).all()),
        field,
        "contains a finite value outside the binary16 domain",
        "finite overflow",
    )


def _assert_meshlet_runtime_contract(mesh: object, mesh_buffer: object) -> None:
    _VALIDATOR.assert_late_meshlet_layout(mesh, mesh_buffer)
    layout = mesh_buffer.meshlet_layout
    bvh = mesh_buffer.meshlet_bvh
    _VALIDATOR.assert_meshlet_lod_lanes(layout.header)
    _VALIDATOR.assert_meshlet_region_ends(layout, explicit=False)
    if not all(
        math.isfinite(float(component))
        for pivot in layout.group_pivot_info.pivots
        for component in pivot
    ):
        _fail("MPLY group-pivot coordinates", "must be finite", "nonfinite data")
    for lod_index in range(8):
        exponent = int(bvh.cluster_bounds_scale_exponents[lod_index])
        _require(
            -128 <= exponent <= 127,
            f"MeshletBVH.clusterBoundsScaleExponent[{lod_index}]",
            "must decode as a signed byte",
            exponent,
        )
    for cluster_index, cluster in enumerate(mesh_buffer.mply_cluster_headers):
        _require_zero(
            f"MPLY cluster[{cluster_index}] reserved bit 31",
            cluster.reserved_bit31,
        )
    for chunk_index, chunk in enumerate(mesh_buffer.mply_chunks):
        flags = chunk.flags
        vert_count = int(chunk.vert_count)
        _require_equal(
            f"MPLY chunk[{chunk_index}] tangent count",
            len(chunk.tangents),
            0 if flags.is_meshlet_no_tangent else vert_count,
        )
        expected_mask_size = (
            ((vert_count + 31) // 32) * 4
            if (
                flags.is_meshlet_no_tangent
                and int(flags.tangent_handedness_mode) == 2
            )
            else 0
        )
        _require_equal(
            f"MPLY chunk[{chunk_index}] tangent-orientation mask size",
            len(chunk.tangent_mask_bytes),
            expected_mask_size,
        )
    _VALIDATOR.assert_meshlet_positions_in_bounds(mesh_buffer)


def _assert_ordinary_runtime_contract(mesh: object, mesh_buffer: object) -> None:
    assert_mesh_semantic_domains(mesh)
    _VALIDATOR.assert_late_buffer_layouts(mesh, mesh_buffer, _assert_element_headers)
    _require(
        0 <= int(mesh_buffer.vertex_buffer_size) <= int(mesh_buffer.total_buffer_size),
        "resident BufferLayout.vertexBufferSize",
        "must fit totalBufferSize",
        (mesh_buffer.vertex_buffer_size, mesh_buffer.total_buffer_size),
    )
    _require(
        int(mesh_buffer.vertex_buffer_size)
        <= int(mesh_buffer.shadow_mesh_index_buffer_offset)
        <= int(mesh_buffer.occluder_mesh_index_buffer_offset)
        <= int(mesh_buffer.total_buffer_size),
        "resident BufferLayout auxiliary index boundaries",
        "must be monotonic from the main-index start through totalBufferSize",
        (
            mesh_buffer.vertex_buffer_size,
            mesh_buffer.shadow_mesh_index_buffer_offset,
            mesh_buffer.occluder_mesh_index_buffer_offset,
            mesh_buffer.total_buffer_size,
        ),
    )
    for buffer_index, stream in mesh_buffer.streaming_buffer_headers.items():
        _require(
            0 <= int(stream.vertex_buffer_length) <= int(stream.unpadded_buffer_size),
            f"streaming BufferLayout[{buffer_index}].vertexBufferLength",
            "must fit unpaddedBufferSize",
            (stream.vertex_buffer_length, stream.unpadded_buffer_size),
        )
        _require(
            int(stream.vertex_buffer_length)
            <= int(stream.shadow_mesh_index_buffer_offset)
            <= int(stream.occluder_mesh_index_buffer_offset)
            <= int(stream.unpadded_buffer_size),
            f"streaming BufferLayout[{buffer_index}] auxiliary index boundaries",
            "must be monotonic from the main-index start through unpaddedBufferSize",
            (
                stream.vertex_buffer_length,
                stream.shadow_mesh_index_buffer_offset,
                stream.occluder_mesh_index_buffer_offset,
                stream.unpadded_buffer_size,
            ),
        )

    flags = mesh.header.decoded_content_flags
    resident_has_lightmap_uv = any(
        int(item.type) == 6 for item in mesh_buffer.buffer_headers
    )
    _require_equal(
        "MeshHeader.contentFlags.hasLightmapUV/resident UV2 stream",
        bool(flags.has_lightmap_uv),
        resident_has_lightmap_uv,
    )
    for buffer_index, stream in mesh_buffer.streaming_buffer_headers.items():
        _require_equal(
            f"MeshHeader.contentFlags.hasLightmapUV/streaming BufferLayout[{buffer_index}] UV2 stream",
            bool(flags.has_lightmap_uv),
            any(int(item.type) == 6 for item in stream.vertex_elements),
        )

    for mesh_index, mesh_data in enumerate(mesh.meshes):
        _VALIDATOR.assert_mesh_data_padding(mesh_index, mesh_data)
        _require(
            int(mesh_data.skin_weight_count_raw) in (0x12, 0x02),
            f"MeshData[{mesh_index}].SkinWeightCount",
            "must select eight influences, with or without the producer-owned upper tag",
            mesh_data.skin_weight_count_raw,
        )


def _assert_shared_runtime_objects(mesh: object) -> None:
    _VALIDATOR.assert_late_geometry_reserves(mesh)
    for owner, lod_index, lod in all_lods(mesh):
        _require(
            0 <= int(lod.lod_level) <= 7,
            f"{owner}.MeshLOD[{lod_index}].lodLevel",
            "must be in the native authored LOD domain [0, 7]",
            lod.lod_level,
        )
    for group_index, group in enumerate(all_groups(mesh)):
        for submesh_index, submesh in enumerate(group.submeshes):
            field = f"MeshGroup[{group_index}].MeshCluster[{submesh_index}]"
            _require_zero(f"{field}.isQuad", submesh.is_quad)
            _require_zero(f"{field}.reserve03", submesh.reserve)


def assert_re9_runtime_contract(mesh: object) -> None:
    _VALIDATOR.assert_late_header(mesh)
    header = mesh.header
    mesh_buffer = mesh.mesh_buffer
    if mesh_buffer is None:
        _fail("MeshBuffer", "must be decoded", None)
    if header.magic == MPLY_MAGIC:
        _assert_meshlet_runtime_contract(mesh, mesh_buffer)
    elif header.magic == MESH_MAGIC:
        _assert_ordinary_runtime_contract(mesh, mesh_buffer)
    else:
        _fail("MeshHeader.magic", "must be MESH or MPLY", header.magic)
    _assert_shared_runtime_objects(mesh)


def _assert_mply_import(mesh: object, mesh_buffer: object) -> None:
    layout = mesh_buffer.meshlet_layout
    bvh = mesh_buffer.meshlet_bvh
    meshlet_header = layout.header
    _require_equal(
        "MeshletHeader canonical MeshletRasterSwitchThreshold",
        int(meshlet_header.serialized_meshlet_raster_switch_threshold),
        mesh.profile.canonical_mply_raster_switch_threshold,
    )
    for lod_index, factor in enumerate(meshlet_header.lod_factors):
        expected = float(lod_index) if lod_index < meshlet_header.lod_num else 0.0
        _require_equal(f"MeshletHeader.lodFactor[{lod_index}]", float(factor), expected)
    info = layout.group_pivot_info
    _require(
        int(info.blob_relative_offset) in (0, 0x90),
        "MPLY group-pivot descriptor offset",
        "must use the canonical absent/0x90 encoding",
        info.blob_relative_offset,
    )
    for lod_index in range(8):
        if not int(bvh.cluster_counts[lod_index]):
            continue
        exponent = int(bvh.cluster_bounds_scale_exponents[lod_index])
        _require(
            -1 <= exponent <= 4,
            f"MeshletBVH.clusterBoundsScaleExponent[{lod_index}]",
            "must remain in the corpus-observed signed range [-1, 4]",
            exponent,
        )

    for chunk_index, chunk in enumerate(mesh_buffer.mply_chunks):
        flags = chunk.flags
        _require(
            int(flags.position_compress_level) != 3,
            f"MPLY chunk[{chunk_index}].positionCompressLevel",
            "must not synthesize corpus-absent mode 3",
            flags.position_compress_level,
        )
        _require(
            int(flags.tangent_handedness_mode) != 3,
            f"MPLY chunk[{chunk_index}].tangentHandednessMode",
            "must not synthesize corpus-absent mode 3",
            flags.tangent_handedness_mode,
        )
        _require_zero(
            f"MPLY chunk[{chunk_index}] producer-reserved bit 11",
            flags.producer_reserved_bit11,
        )
        _require_zero(
            f"MPLY chunk[{chunk_index}] corpus-absent texcoord3 flags",
            (
                flags.is_meshlet_compressed_texcoord3,
                flags.is_meshlet_use_texcoord3,
            ),
        )
        _require(
            bool(flags.is_meshlet_use_vertex_color),
            f"MPLY chunk[{chunk_index}] vertex-color declaration",
            "must remain set because every shipped RE9 chunk declares it",
            flags.is_meshlet_use_vertex_color,
        )
        _require_equal(
            f"MPLY chunk[{chunk_index}] skin flag pair",
            bool(flags.is_meshlet_use_skinned),
            bool(flags.is_meshlet_compressed_skinned),
        )
        if flags.has_skin_stream:
            _require(
                bool(flags.skin_stream_is_shared),
                f"MPLY chunk[{chunk_index}] skin stream",
                "must use the corpus-observed shared Weight8 record",
                "per-vertex records",
            )
            if chunk.skin_weights is not None:
                _require_zero(
                    f"MPLY chunk[{chunk_index}] runtime-ignored final weight byte",
                    chunk.skin_weights.runtime_ignored_weight_bytes,
                )
        _require_zero(
            f"MPLY chunk[{chunk_index}] runtime-unread face alignment residue",
            chunk.face_alignment_padding,
        )
        _require_zero(
            f"MPLY chunk[{chunk_index}] position alignment padding",
            chunk.position_alignment_padding,
        )
        for plane_index, name in enumerate(("uv0", "uv1", "uv2")):
            values = getattr(chunk, name)
            if len(values):
                _assert_half2_value_domain(
                    f"MPLY chunk[{chunk_index}].{name}",
                    values,
                    mesh.profile.canonical_mply_uv_half2_domains[plane_index],
                )
        for bit_index in range(
            int(chunk.vert_count),
            len(chunk.tangent_mask_bytes) * 8,
        ):
            if chunk.tangent_mask_bytes[bit_index >> 3] & (1 << (bit_index & 7)):
                _fail(
                    f"MPLY chunk[{chunk_index}] tangent-mask padding",
                    "must be zero",
                    bit_index,
                )


def _assert_ordinary_import(mesh: object, mesh_buffer: object) -> None:
    for mesh_index, mesh_data in enumerate(getattr(mesh, "meshes", ())):
        _require_equal(
            f"MeshData[{mesh_index}].SkinWeightCount",
            int(mesh_data.skin_weight_count_raw),
            0x12,
        )
        _require_equal(
            f"MeshData[{mesh_index}] AABB padding",
            (
                int(mesh_data.aabb_min_padding),
                int(mesh_data.aabb_max_padding),
            ),
            (1, 1),
        )
    shadow = getattr(mesh, "shadow_mesh", None)
    if shadow is not None:
        _require_equal(
            "ShadowMeshLayout.SkinWeightCount",
            int(shadow.skin_weight_count_raw),
            0x12,
        )
        if shadow.shares_main_payload:
            main_lods = [
                *mesh_data.lods
                for mesh_data in mesh.meshes
            ]
            _require(
                all(any(lod is main_lod for main_lod in main_lods) for lod in shadow.lods),
                "ShadowMeshLayout shared LOD ownership",
                "must retain main-MeshLOD object identity",
                "duplicated shadow LOD",
            )

    for mesh_index, mesh_data in enumerate(getattr(mesh, "meshes", ())):
        for lod_index, lod in enumerate(getattr(mesh_data, "lods", ())):
            _require_equal(
                f"MeshData[{mesh_index}].MeshLOD[{lod_index}].lodLevel",
                int(lod.lod_level),
                lod_index,
            )

    _assert_element_headers(
        "resident BufferLayout elements",
        mesh_buffer.buffer_headers,
        canonical=True,
        require_position_at_zero=True,
    )
    for layout_index, auxiliary in enumerate(
        split_auxiliary_layouts(mesh_buffer.extra_buffer_headers)
    ):
        _assert_element_headers(
            f"resident BufferLayout auxiliary[{layout_index}]",
            auxiliary,
            canonical=True,
            require_position_at_zero=False,
        )
    for buffer_index, stream in mesh_buffer.streaming_buffer_headers.items():
        _assert_element_headers(
            f"streaming BufferLayout[{buffer_index}] elements",
            stream.vertex_elements,
            canonical=True,
            require_position_at_zero=True,
        )
        for layout_index, auxiliary in enumerate(
            split_auxiliary_layouts(stream.extra_vertex_elements)
        ):
            _assert_element_headers(
                f"streaming BufferLayout[{buffer_index}] auxiliary[{layout_index}]",
                auxiliary,
                canonical=True,
                require_position_at_zero=False,
            )
    for buffer_index, payload in mesh_buffer.buffer_payloads.items():
        for plane_index, name in enumerate(("uv0", "uv1", "uv2")):
            values = getattr(payload, name)
            if values:
                _assert_half2_value_domain(
                    f"BufferLayout[{buffer_index}].{name}",
                    values,
                    mesh.profile.canonical_ordinary_uv_half2_domains[plane_index],
                )
    _VALIDATOR.assert_ignored_weight_bytes_zero(mesh_buffer)
    assert_zero_index_padding(mesh, _VALIDATOR)


def _assert_import_reserves(mesh: object) -> None:
    _VALIDATOR.assert_blend_shape_import_reserves(mesh)
    blend = getattr(mesh, "blend_shape_data", None)
    if blend is not None and blend.bone_modifier is not None:
        _require_zero("BlendShapeBoneModifier.reserve", blend.bone_modifier.reserve)


def assert_re9_import_safe(mesh: object) -> None:
    assert_re9_runtime_contract(mesh)
    assert_canonical_layout_constants(_VALIDATOR, mesh)
    _require(
        bool(mesh.is_full),
        "MeshFile load mode",
        "must be FULL for canonical serialization",
        mesh.load_mode,
    )
    flags = mesh.header.decoded_content_flags
    is_mply = mesh.header.magic == MPLY_MAGIC
    _require_zero("MeshHeader.contentFlags.solvedOffset", flags.solved_offset)
    _require_zero("MeshHeader.contentFlags.quadEnable", flags.quad_enable)
    _require_zero("MeshHeader.contentFlags.mergeStaticMeshBVH", flags.merge_static_mesh_bvh)
    _VALIDATOR.assert_content_policy(flags, is_mply)
    mesh_buffer = mesh.mesh_buffer
    if is_mply:
        _assert_mply_import(mesh, mesh_buffer)
    else:
        _assert_ordinary_import(mesh, mesh_buffer)
    _assert_import_reserves(mesh)


__all__ = [
    "RE9_VERTEX_STRIDES",
    "assert_re9_import_safe",
    "assert_re9_runtime_contract",
]
