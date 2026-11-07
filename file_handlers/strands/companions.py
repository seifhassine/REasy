lazy from file_handlers.rsz.game_registry import default_rsz_json_path, game_type_registry
lazy from file_handlers.rsz.rsz_file import RszFile
lazy from file_handlers.rsz.scn_scene_graph import ScnSceneGraphBuilder


def _registry(handler, game):
    registry = getattr(handler, 'type_registry', None) or game_type_registry(game)
    if registry is None:
        raise ValueError(f'Could not load {game} type registry: {default_rsz_json_path(game) or "no RSZ dump found"}')
    return registry


def prefab_graph(registry, path, data, game):
    rsz = RszFile()
    rsz.filepath, rsz.game_version, rsz.type_registry = path, game, registry
    rsz.read(data, validate_type_registry=True)
    return ScnSceneGraphBuilder(registry, game_version=game).build(rsz, root_path=path)
