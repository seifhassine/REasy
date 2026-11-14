from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from file_handlers.build_catalog import catalog_reader
lazy from file_handlers.mmtr.profiles.base import mmtr_profile_for_game, mmtr_profiles, same_mmtr_profile

lazy from .profiles import tex_profile, tex_profiles

CATALOG_FORMAT = 1
CATALOG_DIRECTORY = "resources/data/texture_kinds"
COLOUR_MEANINGS = frozenset({"base_color", "emissive", "color"})
NORMAL_CODECS = {"rgb": "xyz", "ga": "pair", "rg": "xy"}
_KIND_NAME = re.compile(r"[A-Z]{2,}[A-Z0-9]*")


class TextureKindError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Lane:
    channels: str
    meaning: str

    @property
    def codec(self) -> str:
        if self.meaning == "normal":
            return NORMAL_CODECS[self.channels]
        return "colour" if self.meaning in COLOUR_MEANINGS else "value"


@dataclass(frozen=True, slots=True)
class TextureKind:
    name: str
    type_name: str | None
    lanes: tuple[Lane, ...]
    files: int
    formats: Mapping[str, int]
    neutral: tuple[int, ...] | None
    slots: Mapping[str, int]
    paired: int
    omitted: Mapping[int, int]
    floor: int | None


@dataclass(frozen=True, slots=True)
class TextureKindCatalog:
    game: str
    resource_version: int
    kinds: Mapping[str, TextureKind]
    type_lanes: Mapping[str, tuple[Lane, ...]]
    stored_chunks: bool

    def kind(self, path: str) -> TextureKind | None:
        return self.kinds.get(kind_name(path))

    def lanes(self, path: str, type_name: str | None) -> tuple[Lane, ...]:
        kind = self.kind(path)
        return kind.lanes if kind is not None else self.type_lanes.get(type_name or "", ())


def kind_name(path: str) -> str:
    stem = str(path).replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]
    suffix = stem.rsplit("_", 1)[-1].upper() if "_" in stem else ""
    return suffix if _KIND_NAME.fullmatch(suffix) else ""


def parse_lanes(text: str) -> tuple[Lane, ...]:
    lanes = tuple(Lane(*item.split(":", 1)) for item in text.split())
    channels = "".join(lane.channels for lane in lanes)
    if len(set(channels)) != len(channels) or set(channels) - set("rgba"):
        raise TextureKindError(f"lanes {text!r} do not name each of r, g, b and a at most once")
    for lane in lanes:
        if lane.meaning == "normal" and lane.channels not in NORMAL_CODECS:
            raise TextureKindError(f"no normal encoding stores its vector in {lane.channels}")
    return lanes


def lanes_text(lanes: tuple[Lane, ...]) -> str:
    return " ".join(f"{lane.channels}:{lane.meaning}" for lane in lanes)


def catalog_to_dict(catalog: TextureKindCatalog) -> dict:
    return {
        "format": CATALOG_FORMAT,
        "game": catalog.game,
        "resource_version": catalog.resource_version,
        "kinds": {name: {"type": kind.type_name, "lanes": lanes_text(kind.lanes), "files": kind.files,
                         "formats": dict(kind.formats), "neutral": list(kind.neutral) if kind.neutral else None,
                         "slots": dict(kind.slots), "paired": kind.paired,
                         "omitted": {str(count): files for count, files in kind.omitted.items()},
                         "floor": kind.floor}
                  for name, kind in catalog.kinds.items()},
        "type_lanes": {name: lanes_text(lanes) for name, lanes in catalog.type_lanes.items()},
        "stored_chunks": catalog.stored_chunks,
    }


def catalog_from_dict(data: Mapping) -> TextureKindCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise TextureKindError(f"texture kind catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    return TextureKindCatalog(
        str(data["game"]), int(data["resource_version"]),
        {str(name): TextureKind(
            str(name), item["type"], parse_lanes(item["lanes"]), int(item["files"]),
            {str(key): int(value) for key, value in item["formats"].items()},
            tuple(int(value) for value in item["neutral"]) if item["neutral"] else None,
            {str(key): int(value) for key, value in item["slots"].items()}, int(item["paired"]),
            {int(key): int(value) for key, value in item["omitted"].items()},
            None if item["floor"] is None else int(item["floor"]))
         for name, item in data["kinds"].items()},
        {str(name): parse_lanes(text) for name, text in data["type_lanes"].items()},
        bool(data["stored_chunks"]),
    )


texture_kind_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, TextureKindError, "texture kind")


def kind_catalogs() -> dict[str, TextureKindCatalog]:
    return {profile.game: catalog for profile in mmtr_profiles() if (catalog := texture_kind_catalog_for(profile))}


def kind_catalog(version: int, game: str = "") -> TextureKindCatalog | None:
    catalogs = {catalog.game: catalog for name in tex_profile(version).games
                if (profile := mmtr_profile_for_game(name)) and (catalog := texture_kind_catalog_for(profile))}
    chosen = mmtr_profile_for_game(game)
    if chosen is not None and chosen.game in catalogs:
        return catalogs[chosen.game]
    return next(iter(catalogs.values())) if len(catalogs) == 1 else None


def cook_version(game: str) -> int:
    return max(profile.version for profile in tex_profiles() if any(same_mmtr_profile(name, game)
                                                                     for name in profile.games))
