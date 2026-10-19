from __future__ import annotations

from functools import partial

lazy import numpy as np

from .contract_utils import (
    ContractValidator,
    all_groups,
    assert_canonical_layout_constants,
    assert_late_normal_recalc_import_storage,
    assert_zero_index_padding,
    split_auxiliary_layouts,
)
from .models import MESH_MAGIC, MPLY_MAGIC


PRAGMATA_VERTEX_STRIDES = {
    0: 12,  # Position: float3
    1: 8,   # Normal/tangent: two R8G8B8A8_SNORM records
    2: 4,   # UV0: half2
    3: 4,   # UV1: half2
    4: 16,  # Primary packed 3x10-bit + 3x10-bit skin record
    5: 4,   # Color: R8G8B8A8_UNORM
    6: 4,
    7: 16,  # Twelve-influence continuation skin record
}

_VALIDATOR = ContractValidator("Pragmata release canonical contract")
_fail = _VALIDATOR.fail
_require = _VALIDATOR.require
_require_zero = _VALIDATOR.require_zero
_require_equal = _VALIDATOR.require_equal


_assert_element_headers = partial(
    _VALIDATOR.assert_element_headers,
    strides=PRAGMATA_VERTEX_STRIDES,
    unknown_semantic_expectation=(
        "must be a modeled release Pragmata vertex semantic"
    ),
    forbidden_canonical_semantics={
        6: "must not synthesize corpus-absent UV2 semantic 6",
    },
)


def _assert_mply_runtime(mesh: object, mesh_buffer: object) -> None:
    _VALIDATOR.assert_late_meshlet_runtime(mesh, mesh_buffer)
    for cluster_index, cluster in enumerate(mesh_buffer.mply_cluster_headers):
        _require_zero(f"MPLY cluster[{cluster_index}] reserved bit 31", cluster.reserved_bit31)
    for chunk_index, chunk in enumerate(mesh_buffer.mply_chunks):
        _require_equal(f"MPLY chunk[{chunk_index}] tangents count", len(chunk.tangents), int(chunk.vert_count))


def assert_pragmata_runtime_contract(mesh: object) -> None:
    _VALIDATOR.assert_late_header(mesh)
    mesh_buffer = mesh.mesh_buffer
    _require(mesh_buffer is not None, "MeshBuffer", "must be decoded", None)
    if mesh.header.magic == MPLY_MAGIC:
        _assert_mply_runtime(mesh, mesh_buffer)
    elif mesh.header.magic == MESH_MAGIC:
        _VALIDATOR.assert_late_ordinary_runtime(mesh, mesh_buffer, _assert_element_headers)
    else:
        _fail("MeshHeader.magic", "must be MESH or MPLY", mesh.header.magic)
    _VALIDATOR.assert_late_shared_runtime(mesh)


def _assert_dense_auxiliary_import(mesh: object) -> None:
    for layer_name, layer in (
        ("ShadowMeshLayout", getattr(mesh, "shadow_mesh", None)),
        ("OccluderMeshLayout", getattr(mesh, "occluder_mesh", None)),
    ):
        if layer is None:
            continue
        for buffer_index, payload in getattr(layer, "payloads", {}).items():
            for stream in getattr(payload, "attribute_streams", ()):
                _require_zero(
                    f"{layer_name} BufferLayout[{buffer_index}] {stream.semantic} implicit zero suffix",
                    stream.implicit_zero_suffix_byte_length,
                )


def _assert_import_common(mesh: object) -> None:
    flags = mesh.header.decoded_content_flags
    is_mply = mesh.header.magic == MPLY_MAGIC
    for name in ("solved_offset", "quad_enable", "use_ray_tracing_vertex_animation"):
        _require_zero(f"MeshHeader.contentFlags.{name}", getattr(flags, name))
    _require_zero("MeshHeader.contentFlags.hasTertiaryUV", flags.has_tertiary_uv)
    _require_zero("MeshHeader.contentFlags.hasLightmapUV", flags.has_lightmap_uv)
    _require_zero("MeshHeader light-map allocation", (mesh.header.light_map_used, mesh.header.light_map_width, mesh.header.light_map_height))
    _VALIDATOR.assert_content_policy(flags, is_mply)
    for group_index, group in enumerate(all_groups(mesh)):
        for submesh_index, submesh in enumerate(group.submeshes):
            field = f"MeshGroup[{group_index}].MeshCluster[{submesh_index}]"
            _require_zero(f"{field}.isQuad", submesh.is_quad)
            _require_zero(f"{field}.reserve03", submesh.reserve)


def _assert_mply_import(mesh: object) -> None:
    _VALIDATOR.assert_late_mply_import(mesh, exponent_max=4)
    for chunk_index, chunk in enumerate(mesh.mesh_buffer.mply_chunks):
        flags = chunk.flags
        _require_zero(f"MPLY chunk[{chunk_index}] corpus-absent no-tangent flag", flags.is_meshlet_no_tangent)
        _require_zero(
            f"MPLY chunk[{chunk_index}] corpus-absent texcoord3 flags",
            (flags.is_meshlet_compressed_texcoord3, flags.is_meshlet_use_texcoord3),
        )
        _require(
            not (flags.is_meshlet_compressed_skinned and not flags.is_meshlet_use_skinned),
            f"MPLY chunk[{chunk_index}] skin flag pair",
            "must use shipped absent, per-vertex, or shared state",
            (flags.is_meshlet_use_skinned, flags.is_meshlet_compressed_skinned),
        )
        if chunk.skin_record_bytes:
            records = np.frombuffer(chunk.skin_record_bytes, dtype=np.uint8).reshape(-1, 16)
            packed = records[:, :8].copy().view("<u4").reshape(-1, 2)
            _require(bool(np.all((packed >> 30) == 0)), f"MPLY chunk[{chunk_index}] packed-index reserve", "must be cleared by canonical construction", sorted(set(map(int, (packed >> 30).flat))))
            _require_zero(f"MPLY chunk[{chunk_index}] runtime-unread weight bytes 13-15", records[:, 13:16])


def _assert_ordinary_import(mesh: object) -> None:
    mesh_buffer = mesh.mesh_buffer
    _VALIDATOR.assert_late_ordinary_skin_counts(mesh)
    _assert_element_headers("resident BufferLayout elements", mesh_buffer.buffer_headers, canonical=True, require_position_at_zero=True)
    for index, auxiliary in enumerate(split_auxiliary_layouts(mesh_buffer.extra_buffer_headers)):
        _assert_element_headers(f"resident BufferLayout auxiliary[{index}]", auxiliary, canonical=True, require_position_at_zero=False)
    for buffer_index, stream in mesh_buffer.streaming_buffer_headers.items():
        _assert_element_headers(f"streaming BufferLayout[{buffer_index}] elements", stream.vertex_elements, canonical=True, require_position_at_zero=True)
        for index, auxiliary in enumerate(split_auxiliary_layouts(stream.extra_vertex_elements)):
            _assert_element_headers(f"streaming BufferLayout[{buffer_index}] auxiliary[{index}]", auxiliary, canonical=True, require_position_at_zero=False)
    _assert_dense_auxiliary_import(mesh)
    ordinary_influence_count = (
        int(mesh.meshes[0].skin_weight_influence_count)
        if mesh.meshes
        else 0
    )
    for buffer_index, payload in mesh_buffer.buffer_payloads.items():
        _VALIDATOR.assert_packed_skin_payload(f"BufferLayout[{buffer_index}]", payload, ordinary_influence_count)
    shadow = getattr(mesh, "shadow_mesh", None)
    occluder = getattr(mesh, "occluder_mesh", None)
    for label, layer, influence_count in (
        ("ShadowMeshLayout", shadow, int(shadow.skin_weight_influence_count) if shadow is not None else 0),
        ("OccluderMeshLayout", occluder, 0),
    ):
        if layer is None:
            continue
        for buffer_index, payload in layer.payloads.items():
            if payload is not mesh_buffer.buffer_payloads.get(buffer_index):
                _VALIDATOR.assert_packed_skin_payload(f"{label} BufferLayout[{buffer_index}]", payload, influence_count)
    assert_late_normal_recalc_import_storage(mesh, _VALIDATOR)
    assert_zero_index_padding(mesh, _VALIDATOR)
    _VALIDATOR.assert_blend_shape_import_reserves(mesh)


def assert_pragmata_import_safe(mesh: object) -> None:
    assert_pragmata_runtime_contract(mesh)
    assert_canonical_layout_constants(_VALIDATOR, mesh)
    _require(bool(mesh.is_full), "MeshFile load mode", "must be FULL for canonical serialization", mesh.load_mode)
    _assert_import_common(mesh)
    if mesh.header.magic == MPLY_MAGIC:
        _assert_mply_import(mesh)
    else:
        _assert_ordinary_import(mesh)


__all__ = [
    "PRAGMATA_VERTEX_STRIDES",
    "assert_pragmata_import_safe",
    "assert_pragmata_runtime_contract",
]
