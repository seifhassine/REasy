lazy from utils.app_paths import application_root
from utils.registry_manager import RegistryManager
lazy import ui.project_manager.rsz_jsons as rsz_jsons


def default_rsz_json_path(game: str):
    # Settings and mesh profiles spell the game differently ("Pragmata", "PRAGMATA").
    named = next((name for name in rsz_jsons.DEFAULT_RSZ_JSONS if name.casefold() == game.casefold()), game)
    return rsz_jsons.find_default_rsz_json_path(named, application_root())


def game_type_registry(game: str):
    path = default_rsz_json_path(game)
    return RegistryManager.instance().get_registry(str(path)) if path else None
