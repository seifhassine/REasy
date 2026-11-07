"""See docs/internals/character-assembly.md."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

lazy from .material_variations import enum_names
lazy from .plain_values import plain_value
lazy from .rsz_data_types import ArrayData, ObjectData, ResourceData, StringData, UserDataData
lazy from .utils.rsz_instance_defaults import type_table

SPAWNED_PREFABS_RESOURCE = "resources/data/spawned_prefabs.json"


@dataclass(frozen=True, slots=True)
class Spawned:
    path: str
    parts_off: tuple[int, ...] = ()
    materials: tuple[tuple, ...] = ()
    under: Mapping | None = None
    name: str = ""


@dataclass(frozen=True, slots=True)
class FileFields:
    rsz: object
    opened: Callable[[object, object], object | None] | None = None

    def __call__(self, instance: int) -> Mapping:
        fields = getattr(self.rsz, "parsed_elements", {}).get(instance, {})
        return fields if isinstance(fields, Mapping) else {}

    def follow(self, value) -> FileFields | None:
        other = None if self.opened is None else self.opened(self.rsz, value)
        return None if other is None or not other.object_table else FileFields(other, self.opened)

    def component(self, value, type_name: str) -> tuple[Mapping, FileFields] | None:
        other = None if self.opened is None else self.opened(self.rsz, resource_path(value, self))
        found = None if other is None else FileFields(other, self.opened)
        index = None if found is None else next((index for index in range(len(other.instance_infos))
                                                 if type_name_of(other, index) == type_name), None)
        return None if index is None else (found(index), found)


@dataclass(frozen=True, slots=True)
class SpawnChoice:
    key: str
    label: str
    choices: tuple[str, ...]
    applied: int


def spawners(game: str) -> dict[str, dict]:
    entries = type_table(SPAWNED_PREFABS_RESOURCE).get(str(game).upper(), {})
    return {name: entries[entry["like"]] | entry if "like" in entry else entry for name, entry in entries.items()}


def holds_spawner(rsz_file) -> bool:
    if getattr(rsz_file, "is_usr", False):
        return spawner_file(rsz_file)
    found = spawners(getattr(rsz_file, "game_version", ""))
    return bool(found) and any(type_name_of(rsz_file, index) in found for index in range(len(rsz_file.instance_infos)))


def spawner_file(rsz_file) -> bool:
    table = getattr(rsz_file, "object_table", None)
    if not getattr(rsz_file, "is_usr", False) or not table:
        return False
    return type_name_of(rsz_file, table[0]) in spawners(getattr(rsz_file, "game_version", ""))


def type_name_of(rsz_file, instance: int) -> str:
    return (rsz_file.type_registry.get_type_info(rsz_file.instance_infos[instance].type_id) or {}).get("name", "")


def field_pairs(fields: Mapping[str, object], steps,
                resolve: Callable[[int], Mapping]) -> list[tuple[object, Callable]]:
    found: list = [(fields, resolve)]
    for step in steps:
        if isinstance(step, Mapping) and "where" in step:
            wanted = value_at(fields, step["is"], resolve) if isinstance(step["is"], list) else step["is"]
            kept = [(value, here) for value, here in found for entry, there in (opened(value, here),)
                    if value_at(entry, step["where"], there) == wanted]
            found = kept or found[:1] if step.get("or first") else kept
            continue
        reached = []
        for value, here in found:
            if step == "*":
                reached += _each(value, here)
                continue
            if isinstance(step, Mapping) and "find" in step:
                files = filter(None, map(here.follow, step["find"])) if isinstance(here, FileFields) else ()
                wanted = plain_value(value)
                matched = [(item, place) for other in files
                           if type_name_of(other.rsz, other.rsz.object_table[0]) == step["type"]
                           for item, place in field_pairs(other(other.rsz.object_table[0]), [*step["list"], "*"], other)
                           for entry, there in (opened(item, place),) if value_at(entry, step["key"], there) == wanted]
                reached += matched[:1] if step.get("first") else matched
                continue
            if isinstance(step, Mapping):
                inside = here.component(value, step["component"]) if isinstance(here, FileFields) else None
                reached += [inside] if inside else []
                continue
            mapping, there = opened(value, here)
            if (item := mapping.get(step)) is not None:
                reached.append((item, there))
        found = reached
    return found


def _each(value, resolve: Callable[[int], Mapping]) -> list[tuple[object, Callable]]:
    if not isinstance(value, (Mapping, ObjectData, UserDataData)):
        return [(item, resolve) for item in getattr(value, "values", None) or ()]
    mapping, there = opened(value, resolve)
    return [(item, there) for item in mapping.values() if isinstance(item, (ObjectData, UserDataData))]


def field_values(fields: Mapping[str, object], steps, resolve: Callable[[int], Mapping]) -> list:
    return [value for value, _here in field_pairs(fields, steps, resolve)]


def opened(value, resolve: Callable[[int], Mapping]) -> tuple[Mapping, Callable]:
    if isinstance(value, Mapping):
        return value, resolve
    follow = getattr(resolve, "follow", None)
    if follow is not None and (other := follow(value)) is not None:
        return other(other.rsz.object_table[0]), other
    return {} if isinstance(value, UserDataData) else resolve(_instance(value)), resolve


def choice_levels(spawner: Mapping) -> list[Mapping]:
    levels = spawner.get("choices", ())
    return [levels] if isinstance(levels, Mapping) else list(levels)


def spawn_choice_key(document_id: str, instance: int, level: int | str) -> str:
    return f"{document_id}|{instance}|{level}"


def choice_path(fields: Mapping[str, object], spawner: Mapping, resolve: Callable[[int], Mapping], game: str,
                picked: Callable[[int, int], int], levels: int | None = None) -> tuple[list[tuple[list, int]], bool]:
    found, here = [], (fields, resolve)
    for level, spec in enumerate(choice_levels(spawner)[:levels]):
        listed = choices(here[0], spec, here[1], game)
        if not listed:
            return found, False
        index = picked(level, len(listed))
        found.append((listed, index))
        here = listed[index][1], listed[index][3]
    return found, True


def choices(fields: Mapping[str, object], level: Mapping, resolve: Callable[[int], Mapping],
            game: str = "") -> list[tuple[str, Mapping, str, Callable]]:
    names = enum_names(game, level["enum"]) if "enum" in level else None
    lists = [level["list"]] if isinstance(level["list"][0], str) else level["list"]
    found = []
    for steps in lists:
        for value, here in field_pairs(fields, [*steps, "*"], resolve):
            if "type" in level and _value_type(value, here) != level["type"]:
                continue
            entry, there = opened(value, here)
            key = value_at(entry, level["key"], there) if "key" in level else None
            named = resource_path(value, here) if isinstance(value, UserDataData) else _first_path(entry, there)
            label = names[key] if names is not None and key in names else " ".join(
                filter(None, ("" if key is None else str(key), PurePosixPath(named).stem)))
            found.append((label, entry, steps[-1], there))
    several = len({listed for _label, _entry, listed, _there in found}) > 1
    return [(f"{listed} {label}" if several else label, entry, listed, there) for label, entry, listed, there in found]


def _value_type(value, resolve: Callable[[int], Mapping]) -> str:
    if isinstance(resolve, FileFields) and (other := resolve.follow(value)) is not None:
        return type_name_of(other.rsz, other.rsz.object_table[0])
    rsz = getattr(resolve, "rsz", None)
    return type_name_of(rsz, int(value.value or 0)) if rsz is not None and isinstance(value, ObjectData) else ""


def value_at(fields: Mapping[str, object], path, resolve: Callable[[int], Mapping]):
    found = field_values(fields, [path] if isinstance(path, str) else path, resolve)
    return plain_value(found[0]) if found else None


def spawned(fields: Mapping[str, object], spawner: Mapping, resolve: Callable[[int], Mapping]) -> list[Spawned]:
    paths, off = spawner["prefabs"], spawner.get("parts off")
    found = []
    for steps in [paths] if isinstance(paths[0], str) else paths:
        under = steps[-1]["under"] if isinstance(steps[-1], Mapping) and "under" in steps[-1] else None
        steps = steps[:-1] if under else steps
        if steps[-1] == "*":
            found += [Spawned(path, under=under) for value, here in field_pairs(fields, steps, resolve)
                      if (path := resource_path(value, here))]
            continue
        for holder, here in field_pairs(fields, steps[:-1], resolve):
            mapping, there = opened(holder, here)
            if path := resource_path(mapping.get(steps[-1]), there):
                found.append(Spawned(path, _parts_off(mapping, off, there),
                                     _materials(mapping, spawner.get("materials"), there), under,
                                     str(value_at(mapping, spawner["name"], there) or "") if "name" in spawner else ""))
    return found


def _materials(fields: Mapping[str, object], spec, resolve: Callable[[int], Mapping]) -> tuple[tuple, ...]:
    if not spec:
        return ()
    return tuple((value_at(material, spec["material"], there), value_at(param, spec["variable"], inner),
                  value_at(param, spec["value"], inner))
                 for value, here in field_pairs(fields, [*spec["list"], "*"], resolve)
                 for material, there in (opened(value, here),)
                 for item, place in field_pairs(material, [*spec["params"], "*"], there)
                 for param, inner in (opened(item, place),) if not value_at(param, spec["off"], inner))


def set_choices(entry: Mapping[str, object], spec: Mapping | None, resolve: Callable[[int], Mapping],
                crossed: bool = True) -> list[tuple[str, Mapping, Callable, object, object]]:
    found = []
    for value, here in field_pairs(entry, [spec["list"], "*"], resolve) if spec else ():
        listed, there = opened(value, here)
        materials = crossed and "materials" in spec and value_at(listed, spec["materials"], there) or [None]
        pairs = [(condition, material) for condition in value_at(listed, spec["conditions"], there) or [None]
                 for material in materials]
        stem = PurePosixPath(_first_path(listed, there)).stem
        found += [(f"{stem} {index + 1}" if len(pairs) > 1 else stem, listed, there, *pair)
                  for index, pair in enumerate(pairs)]
    return [*found, ("-", {}, resolve, None, None)] if found and value_at(entry, spec["none"], resolve) else found


def random_choices(entry: Mapping[str, object], group: tuple[Mapping, Callable] | None, spec: Mapping | None,
                   resolve: Callable[[int], Mapping]) -> list[tuple[str, Mapping, Callable, object, object]]:
    hidden, found = spec is not None and bool(value_at(entry, spec["hidden"], resolve)), []
    for index, (value, here) in enumerate(field_pairs(group[0], [spec["list"], "*"], group[1]) if group else ()):
        candidate, there = opened(value, here)
        hidden |= bool(value_at(candidate, spec["hidden"], there))
        fields = {name: values[0] for name, path in spec["fields"].items()
                  if (values := field_values(candidate, [path] if isinstance(path, str) else path, there))}
        (name, path), = spec["pick"].items()
        found += [(f"{PurePosixPath(_first_path(item, there)).stem} {index + 1}", {**fields, name: item}, there, None,
                   None) for item in field_values(candidate, [*path, "*"], there)]
    if not found and not hidden:
        return []
    return [(PurePosixPath(_first_path(entry, resolve)).stem, entry, resolve, None, None), *found,
            *([("-", {}, resolve, None, None)] if hidden else [])]


def _parts_off(fields: Mapping[str, object], off, resolve: Callable[[int], Mapping]) -> tuple[int, ...]:
    if not off:
        return ()
    if isinstance(off, str):
        return tuple(int(getattr(value, "value", value)) for value in getattr(fields.get(off), "values", None) or ())
    return tuple(int(value_at(entry, off["index"], there)) for value, here in field_pairs(
        fields, [*off["list"], "*"], resolve) for entry, there in (opened(value, here),)
                 if not value_at(entry, off["on"], there))


def resource_path(value, resolve: Callable[[int], Mapping]) -> str:
    if isinstance(value, UserDataData):
        return value.string.rstrip("\0")
    if value is not None and not isinstance(value, (ResourceData, StringData)):
        value = next((item for item in opened(value, resolve)[0].values()
                      if isinstance(item, (ResourceData, StringData)) and _text(item)), None)
    return "" if value is None else _text(value)


def _first_path(value, resolve: Callable[[int], Mapping], depth: int = 0) -> str:
    if isinstance(value, (ResourceData, StringData, UserDataData)):
        return text if "/" in (text := resource_path(value, resolve)) else ""
    items = value.values if isinstance(value, ArrayData) else opened(value, resolve)[0].values() \
        if depth < 6 and isinstance(value, (Mapping, ObjectData)) else ()
    return next((found for item in items if (found := _first_path(item, resolve, depth + 1))), "")


def _instance(value) -> int:
    if isinstance(value, ObjectData):
        return int(value.value or 0)
    if isinstance(value, ArrayData):
        return 0
    return int(value) if isinstance(value, int) else 0


def _text(value) -> str:
    return str(getattr(value, "value", "") or "").strip().rstrip("\x00").strip()
