from __future__ import annotations

from functools import partial

from .contract_utils import (
    ContractValidator,
    all_groups,
    assert_canonical_layout_constants,
    assert_zero_index_padding,
)
from .models import MESH_MAGIC, MPLY_MAGIC


ONIMUSHA_WOTS_VERTEX_STRIDES = {
    0: 12,  # Position: float3
    1: 8,   # Normal and tangent: two R8G8B8A8_SNORM values
    2: 4,   # UV0: half2
    3: 4,   # UV1: half2
    4: 16,  # Primary packed six-influence skin record
    5: 4,   # Color: R8G8B8A8_UNORM
    6: 4,
    7: 16,  # Twelve-influence continuation skin record
}

_VALIDATOR = ContractValidator(
    "Onimusha: Way of the Sword canonical contract"
)
_fail = _VALIDATOR.fail
_require = _VALIDATOR.require
_require_zero = _VALIDATOR.require_zero
_require_equal = _VALIDATOR.require_equal


_assert_element_headers = partial(
    _VALIDATOR.assert_element_headers,
    strides=ONIMUSHA_WOTS_VERTEX_STRIDES,
    unknown_semantic_expectation="must be a modeled WotS vertex semantic",
    forbidden_canonical_semantics={
        6: "must not synthesize corpus-absent WotS UV2 semantic 6",
    },
)


def _assert_mply_runtime(mesh: object, mesh_buffer: object) -> None:
    _VALIDATOR.assert_late_meshlet_runtime(mesh, mesh_buffer)
    layout = mesh_buffer.meshlet_layout
    _require_equal("MeshletLayout extension-word count", len(layout.extension_words), 2)
    _require_zero("MeshletLayout extension word 2", layout.extension_words[1])
    _require_zero(
        "MPLY absent parts-name-hash table",
        (
            mesh.header.parts_name_hash_offset,
            mesh.parts_name_hashes,
            mesh.parts_name_hash_ids,
            mesh.parts_name_hashes_implicit,
        ),
    )
    material_count = int(mesh.header.name_count) - int(mesh.joint_count)
    _require(material_count >= 0, "MPLY material-name partition", "must be nonnegative", material_count)
    _require_equal("MPLY material-name partition", int(mesh.meshes[0].material_count), material_count)
    for chunk_index, chunk in enumerate(mesh_buffer.mply_chunks):
        flags = chunk.flags
        vert_count = int(chunk.vert_count)
        _require_equal(f"MPLY chunk[{chunk_index}] tangent XYZ count", len(chunk.tangents),
                       0 if flags.is_meshlet_no_tangent else vert_count)
        _require_equal(f"MPLY chunk[{chunk_index}] tangent-mask byte size", len(chunk.tangent_mask_bytes),
                       (vert_count + 31) // 32 * 4 if flags.has_tangent_bits_block else 0)


def _assert_ordinary_runtime(mesh: object, mesh_buffer: object) -> None:
    _VALIDATOR.assert_late_ordinary_runtime(mesh, mesh_buffer, _assert_element_headers)
    flags = mesh.header.decoded_content_flags
    if flags.is_skinning or not mesh.profile.allows_dormant_skin_storage:
        for buffer_index, payload in mesh_buffer.buffer_payloads.items():
            _require_equal(
                f"MeshHeader.isSkinning/BufferLayout[{buffer_index}]",
                bool(flags.is_skinning),
                any(int(item.type) == 4 for item in payload.buffer_headers),
            )


def _assert_reserve03(mesh: object) -> None:
    for group_index, group in enumerate(all_groups(mesh)):
        for submesh_index, submesh in enumerate(group.submeshes):
            field = f"MeshGroup[{group_index}].MeshCluster[{submesh_index}].reserve03"
            if mesh.profile.submesh_reserve03_is_runtime_ignored:
                _require(submesh.runtime_ignored_reserve03 in (0, 1), field, "must use the observed producer 0/1 domain",
                         submesh.runtime_ignored_reserve03)
            else:
                _require_zero(field, submesh.runtime_ignored_reserve03)


def assert_onimusha_wots_runtime_contract(mesh: object) -> None:
    _VALIDATOR.assert_late_header(mesh)
    mesh_buffer = mesh.mesh_buffer
    _require(mesh_buffer is not None, "MeshBuffer", "must be decoded", None)
    if mesh.header.magic == MPLY_MAGIC:
        _assert_mply_runtime(mesh, mesh_buffer)
    elif mesh.header.magic == MESH_MAGIC:
        _assert_ordinary_runtime(mesh, mesh_buffer)
    else:
        _fail("MeshHeader.magic", "must be MESH or MPLY", mesh.header.magic)
    _VALIDATOR.assert_late_shared_runtime(mesh)
    _assert_reserve03(mesh)


def _assert_import_common(mesh: object) -> None:
    flags = mesh.header.decoded_content_flags
    is_mply = mesh.header.magic == MPLY_MAGIC
    _require_zero(
        "MeshHeader corpus-absent geometry flags",
        (
            flags.solved_offset,
            flags.quad_enable,
            flags.has_tertiary_uv,
            flags.has_lightmap_uv,
            flags.use_ray_tracing_vertex_animation,
            flags.merge_static_mesh_bvh,
        ),
    )
    _require_zero(
        "MeshHeader light-map allocation",
        (mesh.header.light_map_used, mesh.header.light_map_width, mesh.header.light_map_height),
    )
    _require(bool(flags.has_vertex_color), "MeshHeader.contentFlags.hasVertexColor", "must remain enabled in canonical WotS output", False)
    _VALIDATOR.assert_content_policy(flags, is_mply)
    for group_index, group in enumerate(all_groups(mesh)):
        for submesh_index, submesh in enumerate(group.submeshes):
            _require_zero(
                f"MeshGroup[{group_index}].MeshCluster[{submesh_index}].isQuad",
                submesh.is_quad,
            )
            _require_zero(
                f"MeshGroup[{group_index}].MeshCluster[{submesh_index}].reserve03",
                submesh.runtime_ignored_reserve03,
            )


def _assert_mply_import(mesh: object) -> None:
    _VALIDATOR.assert_late_mply_import(mesh, exponent_max=mesh.profile.authored_meshlet_bounds_exponent_max)
    mesh_buffer = mesh.mesh_buffer
    _require_equal(
        "MeshletHeader.MPMRLodStrategy",
        int(mesh_buffer.meshlet_layout.header.serialized_mpmr_lod_strategy),
        0,
    )
    for cluster_index, cluster in enumerate(mesh_buffer.mply_cluster_headers):
        _require_zero(
            f"MPLY cluster[{cluster_index}] corpus-absent material/raster flags",
            (
                cluster.no_ray_tracing,
                cluster.alpha_test,
                cluster.transparent,
                cluster.meshlet_material_flag_extension,
                cluster.reserved_bit31,
            ),
        )
    for chunk_index, chunk in enumerate(mesh_buffer.mply_chunks):
        flags = chunk.flags
        _require(bool(flags.is_meshlet_no_tangent), f"MPLY chunk[{chunk_index}] no-tangent declaration", "must remain enabled in canonical WotS output", False)
        _require_zero(
            f"MPLY chunk[{chunk_index}] corpus-absent UV2/skin flags",
            (
                flags.is_meshlet_compressed_texcoord3,
                flags.is_meshlet_use_texcoord3,
                flags.is_meshlet_use_skinned,
                flags.is_meshlet_compressed_skinned,
            ),
        )


def _assert_ordinary_import(mesh: object) -> None:
    mesh_buffer = mesh.mesh_buffer
    _VALIDATOR.assert_late_ordinary_skin_counts(mesh)
    _require_zero("ordinary 32-bit index mode", mesh_buffer.has_32bit_indices)
    _require_zero(
        "ordinary corpus-absent occluder layout",
        getattr(mesh, "occluder_mesh", None),
    )
    _assert_element_headers("resident BufferLayout elements", mesh_buffer.buffer_headers, canonical=True,
                            require_position_at_zero=True)
    for buffer_index, stream in mesh_buffer.streaming_buffer_headers.items():
        _assert_element_headers(f"streaming BufferLayout[{buffer_index}] elements", stream.vertex_elements,
                                canonical=True, require_position_at_zero=True)

    influence_count = int(mesh.meshes[0].skin_weight_influence_count) if mesh.meshes else 0
    for buffer_index, payload in mesh_buffer.buffer_payloads.items():
        _require_zero(
            f"BufferLayout[{buffer_index}] corpus-absent alternative weight streams",
            (
                payload.extra_skin_weights,
                payload.secondary_skin_weights,
                payload.additional_vertex_weights,
            ),
        )
        _VALIDATOR.assert_packed_skin_payload(f"BufferLayout[{buffer_index}]", payload, influence_count)
    if influence_count == 6:
        _require_zero(
            "six-influence continuation semantic",
            tuple(
                item
                for payload in mesh_buffer.buffer_payloads.values()
                for item in payload.buffer_headers
                if int(item.type) == 7
            ),
        )
    elif influence_count == 12:
        for buffer_index, payload in mesh_buffer.buffer_payloads.items():
            semantics = {int(item.type) for item in payload.buffer_headers}
            if 4 in semantics:
                _require(7 in semantics, f"BufferLayout[{buffer_index}] twelve-influence continuation", "must contain semantic 7", semantics)

    assert_zero_index_padding(mesh, _VALIDATOR)

    _VALIDATOR.assert_blend_shape_import_reserves(mesh)
    blend = getattr(mesh, "blend_shape_data", None)
    for body in blend.bodies if blend is not None else ():
        _require_zero(f"BlendShapeBody[{body.lod_index}] undecoded payload", body.raw_unvisualized_payload)
        for target in (*body.targets, *body.none_targets):
            for range_index, target_range in enumerate(target.ranges):
                _require_zero(
                    f"BlendShapeBody[{body.lod_index}] target range [{range_index}] part/surface identity",
                    (target_range.part_id, target_range.surface_range_id),
                )


def assert_onimusha_wots_import_safe(mesh: object) -> None:
    assert_onimusha_wots_runtime_contract(mesh)
    assert_canonical_layout_constants(_VALIDATOR, mesh)
    _require(bool(mesh.is_full), "MeshFile load mode", "must be FULL for canonical serialization", mesh.load_mode)
    _assert_import_common(mesh)
    if mesh.header.magic == MPLY_MAGIC:
        _assert_mply_import(mesh)
    else:
        _assert_ordinary_import(mesh)


__all__ = [
    "ONIMUSHA_WOTS_VERTEX_STRIDES",
    "assert_onimusha_wots_import_safe",
    "assert_onimusha_wots_runtime_contract",
]
