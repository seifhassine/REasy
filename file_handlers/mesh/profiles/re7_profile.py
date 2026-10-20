from .base import MeshGameProfile, MeshMainVersion, register_mesh_profile
from .re_rt_profile import ReRtMeshProfile


class Re7MeshProfile(MeshGameProfile):
    game = "RE7"
    display_name = "Resident Evil 7"
    aliases = ("Resident Evil 7", "Resident Evil 7 biohazard")
    format_version = MeshMainVersion.RE7
    version_pairs = frozenset({(352921600, 32)})
    default_for_format = True


class Re7RtMeshProfile(ReRtMeshProfile):
    game = "RE7RT"
    display_name = "Resident Evil 7 (RT)"
    aliases = ("RE7 RT", "RE7_RT", "RE7_RT_STM", "Resident Evil 7 RT")
    version_pairs = frozenset({(21041600, 220128762)})
    primary_version_pairs = version_pairs
    default_for_format = False


RE7_MESH_PROFILE = register_mesh_profile(Re7MeshProfile())
RE7RT_MESH_PROFILE = register_mesh_profile(Re7RtMeshProfile())


__all__ = [
    "RE7_MESH_PROFILE",
    "RE7RT_MESH_PROFILE",
    "Re7MeshProfile",
    "Re7RtMeshProfile",
]
