from .base import Half2ValueDomain, MeshMainVersion, register_mesh_profile
from .onimusha_wots_profile import OnimushaWotsMeshProfile


class OnimushaWotsUpdatedMeshProfile(OnimushaWotsMeshProfile):
    game = "ONIMUSHAWOTS"
    sdf_texture_resource_version = 501527429
    stmesh_version = 260209350
    display_name = "Onimusha: Way of the Sword"
    aliases = ("ONIWOTS", "OnimushaWotS", "Onimusha Way of the Sword")
    format_version = MeshMainVersion.ONIMUSHA_WOTS_UPDATED
    version_pairs = frozenset({(250203152, 260209350)})
    meshlet_max_triangles = 126
    meshlet_max_vertices = 128
    material_shader_resource_version = 260605155
    normal_compute_uses_packed_skin_width = True
    supports_packed_rigid_skinning = True
    # The loader loops over the literal 3-bit buffer total without a modulo-eight correction.
    content_buffer_count_can_wrap = False
    canonical_buffer_count_limit = 7
    authored_blend_shape_codecs = (0, 1, 3)
    authored_meshlet_bounds_exponent_max = 4
    allows_dormant_skin_storage = True
    # 19_em500_701 has quiet binary16 NaNs in referenced UV0.
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.IEEE754,
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
    )
    # MeshCluster +3 is unread; release data stores 0/1 there.
    submesh_reserve03_is_runtime_ignored = True


ONIMUSHA_WOTS_UPDATED_MESH_PROFILE = register_mesh_profile(OnimushaWotsUpdatedMeshProfile())
