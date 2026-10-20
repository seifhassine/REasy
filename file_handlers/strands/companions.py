lazy from utils.app_paths import application_root
from utils.registry_manager import RegistryManager
lazy import ui.project_manager.rsz_jsons as rsz_jsons
lazy from file_handlers.rsz.rsz_file import RszFile
lazy from file_handlers.rsz.scn_scene_graph import ScnSceneGraphBuilder


def _registry(handler, game):
    registry = getattr(handler, 'type_registry', None)
    if registry:
        return registry
    # Settings and mesh profiles spell the game differently ("Pragmata", "PRAGMATA").
    named = next((name for name in rsz_jsons.DEFAULT_RSZ_JSONS if name.casefold() == game.casefold()), game)
    path = rsz_jsons.find_default_rsz_json_path(named, application_root())
    registry = RegistryManager.instance().get_registry(str(path)) if path else None
    if registry is None:
        raise ValueError(f'Could not load {game} type registry: {path or "no RSZ dump found"}')
    return registry


def prefab_graph(registry, path, data, game):
    rsz = RszFile()
    rsz.filepath, rsz.game_version, rsz.type_registry = path, game, registry
    rsz.read(data, validate_type_registry=True)
    return ScnSceneGraphBuilder(registry, game_version=game).build(rsz, root_path=path)
