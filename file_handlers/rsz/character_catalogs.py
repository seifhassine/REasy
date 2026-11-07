"""See docs/internals/character-assembly.md."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial

lazy from file_handlers.motion.banks import _paths as motion_paths

lazy from .game_registry import game_type_registry
lazy from .material_variations import enum_names
lazy from .part_swapper import character_levels, edit_characters, registered_holders
lazy from .plain_values import plain_value, user_path
lazy from .rsz_data_types import ObjectData, ResourceData
lazy from .rsz_file import RszFile
lazy from .scene_assembly import opened_file
lazy from .scn_scene_adapters import FolderLinkAdapter
lazy from .scn_scene_graph import VIA_FOLDER
lazy from .spawn_rules import (
    FileFields, choice_levels, choice_path, field_values, resource_path, spawners, type_name_of,
)
lazy from .utils.rsz_instance_defaults import type_table
lazy from utils.resource_file_utils import resource_key

CHARACTER_CATALOGS_RESOURCE = "resources/data/character_catalogs.json"

Read = Callable[[str], tuple[str, bytes] | None]
RszOf = Callable[[str], object | None]


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    group: str
    path: str
    catalog: str


def catalog_entries(game: str, rsz_of: RszOf, name: str | None = None) -> list[CatalogEntry]:
    found = {}
    for catalog in type_table(CHARACTER_CATALOGS_RESOURCE).get(str(game).upper(), ()):
        for path in catalog["files"] if name in (None, catalog["name"]) else ():
            for group, target in _walk(path, catalog["steps"], "", rsz_of, game):
                found.setdefault(target.casefold(), CatalogEntry(group, target, catalog["name"]))
    return list(found.values())


def rsz_reader(game: str, read: Read, files: dict | None = None) -> RszOf:
    return partial(_read_rsz, read=read, registry=game_type_registry(game), game=game, files=files)


def catalog_files(game: str, name: str) -> list[str]:
    return next((catalog["files"] for catalog in type_table(CHARACTER_CATALOGS_RESOURCE).get(str(game).upper(), ())
                 if catalog["name"] == name), [])


def spawn_templates(game: str, rsz_of: RszOf) -> set[str]:
    return {resource_key(entry.path) for catalog in type_table(CHARACTER_CATALOGS_RESOURCE).get(str(game).upper(), ())
            if catalog.get("spawned") for entry in catalog_entries(game, rsz_of, catalog["name"])}


def catalog_groups(game: str, rsz_of: RszOf, name: str) -> dict:
    found = {}
    for entry in catalog_entries(game, rsz_of, name):
        found.setdefault(resource_key(entry.path), entry.group)
    return found


def character_shells(game: str, name: str, rsz_of: RszOf, field: str, base: str) -> dict:
    found, pending, seen = {}, list(catalog_files(game, name)), set()
    while pending:
        path = pending.pop(0)
        if path.casefold() in seen or (rsz := rsz_of(path)) is None:
            continue
        seen.add(path.casefold())
        pending[:0] = [link for instance in range(1, len(rsz.instance_infos)) if type_name_of(rsz, instance) ==
                       VIA_FOLDER and (link := FolderLinkAdapter(_fields(rsz, instance)).scene_path())]
        for item in getattr(rsz, "gameobjects", ()):
            components = tuple((type_name_of(rsz, instance), _fields(rsz, instance))
                               for instance in rsz.object_table[item.id + 1:item.id + 1 + item.component_count])
            kind = next((plain_value(value) for _type, fields in components
                         if (value := fields.get(field)) is not None), None)
            if kind and kind not in found and any(base in rsz.type_registry.getTypeParents(type_name)
                                                  for type_name, _fields in components):
                found[kind] = components, tuple(motion_paths([value for _type, fields in components
                                                             for value in fields.values()], rsz.parsed_elements, set()))
    return found


class CatalogFile:
    def __init__(self, game: str, rsz, user_file: Callable[[str], object | None], reads: dict | None = None):
        self.game, self.entries, self.user_file = game, spawners(game), user_file
        self.reads = {} if reads is None else reads
        self.resolve = FileFields(rsz, partial(opened_file, user_file=user_file, user_paths={}))
        self.spawners = [(instance, name, _levels(self.entries[name])) for instance in getattr(rsz, "object_table", ())
                         if (name := type_name_of(rsz, instance)) in self.entries]

    @classmethod
    def of(cls, game: str, path: str, read: Read, reads: dict | None = None) -> CatalogFile:
        user_file = rsz_reader(game, read, {})
        return cls(game, user_file(path), user_file, reads)

    def choices(self, instance: int, picks: Sequence[int]) -> list[str]:
        name = type_name_of(self.resolve.rsz, instance)
        picked = lambda level, _count: picks[level] if level < len(picks) else 0  # noqa: E731
        if rule := self.entries[name].get("swaps", {}).get("characters"):
            if name not in self.reads:
                holders = registered_holders(self.entries[name]["swaps"], self.user_file)
                self.reads[name] = edit_characters(rule, holders, self.user_file)
            levels = character_levels(self.reads[name], enum_names(self.game, rule["enum"]), picked)
            return list(levels[len(picks)][0]) if len(levels) > len(picks) else []
        levels, _complete = choice_path(self.resolve(instance), self.entries[name], self.resolve, self.game, picked,
                                        len(picks) + 1)
        return [label for label, *_ in levels[-1][0]] if len(levels) > len(picks) else []


def _levels(entry: Mapping) -> int:
    rule = entry.get("swaps", {}).get("characters")
    return len(rule["rows"]) + 1 if rule else len(choice_levels(entry))


def _read_rsz(path: str, read: Read, registry, game: str, files: dict | None = None):
    if files is not None:
        if path.casefold() not in files:
            files[path.casefold()] = _read_rsz(path, read, registry, game)
        return files[path.casefold()]
    hit = None if registry is None else read(path)
    if hit is None:
        return None
    rsz = RszFile()
    rsz.filepath, rsz.type_registry, rsz.game_version = hit[0], registry, game
    try:
        rsz.read(hit[1])
    except (ValueError, KeyError, IndexError, EOFError):
        return None
    return rsz


def _walk(path: str, steps, group: str, rsz_of: RszOf, game: str) -> Iterator[tuple[str, str]]:
    if not steps:
        yield group, path
        return
    rsz = rsz_of(path)
    if rsz is None:
        return
    step, resolve = steps[0], partial(_fields, rsz)
    roots = [root for root in rsz.object_table[:1] if rsz.is_usr and type_name_of(rsz, root) == step["type"]] or [
        instance for instance in range(1, len(rsz.instance_infos)) if type_name_of(rsz, instance) == step["type"]]
    for root in map(resolve, roots):
        listed = [(name, value) for name in step["fields"] if (value := root.get(name)) is not None] \
            if "fields" in step else [(None, entry) for entry in field_values(root, [*step["entries"], "*"], resolve)]
        for field, entry in listed:
            fields = resolve(int(entry.value or 0)) if isinstance(entry, ObjectData) else {}
            value = fields.get(step["file"]) if "file" in step else entry
            target = resource_path(value, resolve) if isinstance(value, ResourceData) else (
                user_path(rsz, value) or resource_path(value, resolve))
            if not target or "holding" in step and not _holds(target, step["holding"], rsz_of):
                continue
            named = field or group
            if "group" in step:
                value = plain_value(fields.get(step["group"], root.get(step["group"], "")))
                named = enum_names(game, step["enum"]).get(value, str(value)) if "enum" in step else str(value)
            if len(steps) == 1:
                yield named, target
            else:
                yield from _walk(target, steps[1:], named, rsz_of, game)


def _holds(path: str, type_name: str, rsz_of: RszOf) -> bool:
    rsz = rsz_of(path)
    return rsz is not None and any(type_name_of(rsz, instance) == type_name
                                   for instance in range(1, len(rsz.instance_infos)))


def _fields(rsz, instance_id: int) -> dict:
    fields = rsz.parsed_elements.get(instance_id, {})
    return fields if isinstance(fields, dict) else {}
