"""Version profiles for RE Engine GUIR resources.

The outer GUI version and the embedded compact-CLIP version are dispatched
together. Unsupported versions fail closed
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from file_handlers.motion.profiles import MotionFormatProfile

from .adapter import GuiFormatAdapter


@dataclass(frozen=True, slots=True)
class GuiFormatProfile:
    name: str
    version: int
    adapter: GuiFormatAdapter
    runtime_target: str
    motion: MotionFormatProfile
    default_uvs_version: int
    default_tex_version: int
    default_mesh_version: int
    default_mdf_version: int
    default_gcf_version: int
    mesh_pixels_per_unit: float
    config_resource_paths: tuple[str, ...]
    controller_index_path: str | None = None
    preview_scenario_catalog_path: str | None = None


GUI_PROFILES: dict[int, GuiFormatProfile] = {}


def register_gui_profile(profile: GuiFormatProfile) -> None:
    """Register one exact serialized version; duplicate claims are errors."""

    version = int(profile.version)
    if version in GUI_PROFILES:
        raise ValueError(f"GUIR version {version} is already registered")
    GUI_PROFILES[version] = profile


def gui_profile(version: int) -> GuiFormatProfile:
    try:
        return GUI_PROFILES[int(version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(GUI_PROFILES)))
        raise ValueError(
            f"unsupported GUIR version {version}; supported versions: {supported}"
        ) from exc


def gui_profile_from_data(
    data: bytes | bytearray | memoryview,
) -> GuiFormatProfile:

    if len(data) < 4:
        raise ValueError("file is too small for a GUIR version")
    return gui_profile(struct.unpack_from("<I", data, 0)[0])


from .dmc5.profile import DMC5_GUI_PROFILE

register_gui_profile(DMC5_GUI_PROFILE)
