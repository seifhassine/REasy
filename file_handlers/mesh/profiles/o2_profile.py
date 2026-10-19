from .base import (
    MeshBufferLayout,
    MeshHeaderLayout,
    MeshMainVersion,
    register_mesh_profile,
)
from .dd2_profile import Dd2MeshProfile


class Onimusha2MeshProfile(Dd2MeshProfile):
    game = "O2"
    display_name = "Onimusha 2: Samurai's Destiny"
    aliases = (
        "Onimusha2",
        "Onimusha 2",
        "Onimusha 2 Samurai's Destiny",
    )
    format_version = MeshMainVersion.ONIMUSHA
    version_pairs = frozenset(
        {
            (240704828, 240827123),
            (240827123, 240827123),
        }
    )
    header_layout = MeshHeaderLayout.WILDS
    buffer_layout = MeshBufferLayout.WILDS
    submesh_has_ukn1 = True
    meshlet_uses_legacy_flag_bits = False
    canonical_import_contract = None


O2_MESH_PROFILE = register_mesh_profile(Onimusha2MeshProfile())


__all__ = [
    "O2_MESH_PROFILE",
    "Onimusha2MeshProfile",
]
