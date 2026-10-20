from .base import MeshBufferLayout, MeshMainVersion, register_mesh_profile
from .mhwilds_profile import MhWildsMeshProfile


class MhStories3MeshProfile(MhWildsMeshProfile):
    game = "MHST3"
    stmesh_version = None
    sdf_texture_resource_version = None
    display_name = "Monster Hunter Stories 3: Twisted Reflection"
    aliases = (
        "MHST3_DEMO",
        "MonsterHunterStories3",
        "MONSTER_HUNTER_STORIES_3_TWISTED_REFLECTION",
        "MONSTER_HUNTER_STORIES_3_TWISTED_REFLECTION_TRIAL",
    )
    format_version = MeshMainVersion.PRAGMATA
    version_pairs = frozenset({(250203152, 250604100)})
    meshlet_max_triangles = 64
    meshlet_max_vertices = 64
    buffer_layout = MeshBufferLayout.PRAGMATA
    streaming_header_size = 80
    streaming_header_has_extended_prefix = True
    has_shape_key_weight_size = True
    meshlet_uses_pragmata_flag_bits = True
    meshlet_uses_mhwilds_tangent_handedness = False
    submesh_index_ranges_are_even_aligned = False
    canonical_import_contract = None
    default_for_format = False


MHST3_MESH_PROFILE = register_mesh_profile(MhStories3MeshProfile())


__all__ = ["MHST3_MESH_PROFILE", "MhStories3MeshProfile"]
