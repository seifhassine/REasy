"""See docs/internals/rcol-names.md."""
import json
from functools import cache

lazy from utils.app_paths import resource_path
lazy from utils.game_keys import game_key


def names_resource(game: str) -> str:
    return f"resources/data/rcol_names/{game_key(game).lower()}.json"


@cache
def reversed_names(game: str) -> dict[int, str]:
    path = resource_path(names_resource(game))
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as handle:
        return {int(name_hash, 16): name for name_hash, name in json.load(handle).items()}
