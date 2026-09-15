"""DMC5 GUIR resource/dependency profile."""

from file_handlers.motion.profiles import DMC5_PROFILE

from ..profiles import GuiFormatProfile
from .adapter import DMC5_GUI_ADAPTER


DMC5_GUI_PROFILE = GuiFormatProfile(
    name="Devil May Cry 5",
    version=DMC5_GUI_ADAPTER.version,
    adapter=DMC5_GUI_ADAPTER,
    runtime_target="stm",
    motion=DMC5_PROFILE,
    default_uvs_version=7,
    default_tex_version=11,
    default_mesh_version=1808282334,
    default_mdf_version=10,
    default_gcf_version=15,
    mesh_pixels_per_unit=100.0,
    config_resource_paths=("ui/guiconfig.gcf", "systems/gui/config.gcf"),
    controller_index_path="resources/data/gui/dmc5_controller_index.json",
    preview_scenario_catalog_path=(
        "resources/data/gui/dmc5_preview_scenarios.json"
    ),
)
