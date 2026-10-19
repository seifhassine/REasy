from functools import lru_cache
import json
from pathlib import Path

from utils.app_paths import resource_path


def catalog_file_name(game: str, resource_version: int) -> str:
    return f"{str(game).casefold()}.{int(resource_version)}.json"


def catalog_reader(directory: str, from_dict, error: type[Exception], label: str, errors: tuple = ()):
    caught = (OSError, ValueError, KeyError, TypeError, *errors)

    @lru_cache(maxsize=None)
    def read(game: str, version: int):
        path = Path(resource_path(f"{directory}/{catalog_file_name(game, version)}"))
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as stream:
                catalog = from_dict(json.load(stream))
        except caught as exc:
            raise error(f"cannot read {label} catalog {path}: {exc}") from exc
        if catalog.resource_version != version:
            raise error(f"{path} describes build {catalog.resource_version}, not {version}")
        return catalog

    def catalog_for(profile):
        version = profile.build_resource_version
        return None if version is None else read(str(profile.game), int(version))

    return catalog_for
