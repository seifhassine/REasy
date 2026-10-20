from .base import MeshMainVersion, register_mesh_profile
from .dd2_profile import Dd2OldMeshProfile


class KunitsuGamiMeshProfile(Dd2OldMeshProfile):
    game = "KUNITSUGAMI"
    display_name = "Kunitsu-Gami: Path of the Goddess"
    aliases = ("KunitsuGami", "Kunitsu-Gami")
    format_version = MeshMainVersion.KUNITSUGAMI
    version_pairs = frozenset({(230727984, 240306278)})
    meshlet_max_triangles = 128
    meshlet_max_vertices = 128
    meshlet_has_extended_indices = True
    meshlet_has_implicit_part_hashes = False
    canonical_import_contract = None


KUNITSUGAMI_MESH_PROFILE = register_mesh_profile(KunitsuGamiMeshProfile())


__all__ = ["KUNITSUGAMI_MESH_PROFILE", "KunitsuGamiMeshProfile"]
