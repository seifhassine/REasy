"""See docs/internals/startup-draw-states.md, Campaign state."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData
lazy from file_handlers.rsz.plain_values import user_path
lazy from file_handlers.uvar.uvar_file import UVarFile
lazy from file_handlers.uvar.uvar_types import TypeKind


@dataclass(frozen=True, slots=True)
class CampaignChoice:
    key: int
    difficulty: int
    clear_data: bool = False


@dataclass(frozen=True, slots=True)
class CampaignSetting:
    key: int
    chapter: int
    flags: tuple[bytes, ...]
    files: tuple[tuple[int, bool], ...]
    path: str


QuestCondition = tuple[tuple[tuple[bytes, bool], ...], int]
CampaignChoices = tuple[list[tuple[int, str]], list[tuple[int, str]], list[int]]


@dataclass(frozen=True, slots=True)
class CampaignCatalog:
    settings: Mapping[int, CampaignSetting]
    chapter_starts: Mapping[int, bytes]
    difficulty_flags: Mapping[int, bytes]
    quests: Mapping[int, QuestCondition] = field(default_factory=dict)
    chapter_ends: Mapping[int, bytes] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScenarioFlags:
    cleared: Mapping[bytes, bool]
    groups: Mapping[str, tuple[bytes, ...]]
    digits: Mapping[str, int]
    booleans: frozenset[bytes] = frozenset()
    kept: Mapping[bytes, bool] = field(default_factory=dict)

    def variable(self, name: str, group: int, index: int) -> bytes | None:
        digits = self.digits.get(name)
        variables = () if digits is None else self.groups.get(f"{name}{group:0{digits}d}" if digits else name, ())
        return variables[index] if 0 <= index < len(variables) else None


def three_valued(values: Sequence[bool | None], either: bool) -> bool | None:
    return either if either in values else None if None in values else not either


@dataclass(frozen=True, slots=True)
class CampaignState:
    chapter: int
    difficulty: int
    played_through: bool
    flags: Mapping[bytes, bool]
    files: frozenset[int] = frozenset()
    quests: Mapping[int, QuestCondition] = field(default_factory=dict)
    unknown: frozenset[bytes] = frozenset()

    def check(self, checks: list[tuple[bytes, bool]], logic: int) -> bool | None:
        values = [None if (value := self.flags.get(guid)) is None and guid in self.unknown else value == compare
                  for guid, compare in checks]
        return three_valued(values, logic == 1) if logic in (0, 1) else False

    def quest_impossible(self, file_id: int) -> bool | None:
        checks, logic = self.quests.get(file_id, ((), 0))
        return self.check(list(checks), logic) if checks else False


def campaign_marker(path: str, spec: Mapping) -> str | None:
    lowered = path.replace("\\", "/").lower()
    return next((marker for marker in spec["catalogs"] if marker in lowered), None)


def read_catalog(spec: Mapping, marker: str, read_rsz: Callable[[str], object | None]) -> CampaignCatalog | None:
    catalog = read_rsz(spec["catalogs"][marker])
    if catalog is None:
        return None
    settings_spec, chapters_spec, difficulties_spec = spec["settings"], spec["chapters"], spec["difficulties"]
    files_spec = spec["files"]
    settings, starts, ends, difficulties = {}, {}, {}, {}
    for fields in _instances(catalog, settings_spec["entry"]):
        path = user_path(catalog, fields.get(settings_spec["user"]))
        user = read_rsz(path) if path else None
        listed = _refs(_root_fields(user).get(settings_spec["list"]))
        index = int(_value(fields.get(settings_spec["index"]), 0))
        if not 0 <= index < len(listed):
            continue
        setting = _fields(user, listed[index])
        key = int(_value(fields.get(settings_spec["key"]), 0))
        flags = tuple(bytes(guid.raw_bytes) for guid in _items(setting.get(settings_spec["flags"])))
        file_path = user_path(user, setting.get(settings_spec["files"]))
        files_user = read_rsz(file_path) if file_path else None
        files = tuple((int(_value(item.get(files_spec["id"]), -1)), bool(_value(item.get(files_spec["read"]), False)))
                      for item in (_fields(files_user, ref) for ref in _refs(
                          _root_fields(files_user).get(files_spec["list"]))))
        settings.setdefault(key, CampaignSetting(key, int(_value(setting.get(settings_spec["chapter"]), -1)),
                                                 flags, files, path))
    for fields in _instances(catalog, chapters_spec["entry"]):
        chapter = int(_value(fields.get(chapters_spec["chapter"]), -1))
        starts[chapter] = bytes(fields[chapters_spec["start"]].raw_bytes)
        ends[chapter] = bytes(fields[chapters_spec["end"]].raw_bytes)
    for fields in _instances(catalog, difficulties_spec["entry"]):
        difficulties[int(_value(fields.get(difficulties_spec["difficulty"]), -1))] = \
            bytes(fields[difficulties_spec["flag"]].raw_bytes)
    return CampaignCatalog(settings, starts, difficulties, read_quests(catalog, spec["quests"], read_rsz), ends)


def read_quests(catalog, spec: Mapping, read_rsz: Callable[[str], object | None]) -> dict[int, QuestCondition]:
    register = next(_instances(catalog, spec["register"]), {})
    list_path = user_path(catalog, register.get(spec["list"]))
    quest_list = read_rsz(list_path) if list_path else None
    found = {}
    for path in (user_path(quest_list, item) for item in _items(_root_fields(quest_list).get(spec["settings"]))):
        quest = read_rsz(path) if path else None
        root = _root_fields(quest)
        if not root:
            continue
        impossible = _fields(quest, int(_value(root.get(spec["impossible"]), 0) or 0))
        condition = _fields(quest, int(_value(impossible.get(spec["condition"]), 0) or 0))
        checks = tuple((bytes(check[spec["flag"]].raw_bytes), bool(_value(check.get(spec["compare"]), True)))
                       for check in (_fields(quest, ref) for ref in _refs(condition.get(spec["flags"]))))
        found[int(_value(root.get(spec["file"]), -1))] = (checks, int(_value(condition.get(spec["logic"]), 0)))
    return found


def read_scenario_flags(spec: Mapping, read_rsz: Callable[[str], object | None],
                        read_bytes: Callable[[str], bytes | None]) -> ScenarioFlags | None:
    tables = read_rsz(spec["table"])
    data = read_bytes(spec["variables"])
    if tables is None or data is None:
        return None
    names, digits_by_name = set(), {}
    for fields in _instances(tables, spec["list type"]):
        for path in (user_path(tables, item) for item in _items(fields.get(spec["list"]))):
            table = read_rsz(path) if path else None
            for data_fields in _instances(table, spec["data type"]) if table is not None else ():
                name = str(_value(data_fields.get(spec["name"]), "")).rstrip("\0")
                digits = digits_by_name.setdefault(name, int(_value(data_fields.get(spec["digits"]), 0)))
                for block in _refs(data_fields.get(spec["blocks"])):
                    number = int(_value(_fields(table, block).get(spec["group"]), 0))
                    names.add(f"{name}{number:0{digits}d}" if digits else name)
    uvar = UVarFile()
    uvar.read(data)
    cleared, groups, booleans, kept, stack = {}, {}, set(), {}, [uvar]
    while stack:
        group = stack.pop()
        stack.extend(group.embedded_uvars)
        booleans.update(_guid_bytes(variable.guid) for variable in group.variables if variable.type == TypeKind.Boolean)
        name = group.header.name
        if name in spec["kept"]:
            kept.update((_guid_bytes(variable.guid), bool(variable.value)) for variable in group.variables
                        if variable.type == TypeKind.Boolean)
        if name not in names:
            continue
        groups[name] = tuple(_guid_bytes(variable.guid) for variable in group.variables)
        for variable in group.variables:
            if variable.type == TypeKind.Boolean:
                cleared[_guid_bytes(variable.guid)] = False
    return ScenarioFlags(cleared, groups, digits_by_name, frozenset(booleans), kept)


def campaign_state(catalog: CampaignCatalog, scenario: ScenarioFlags, choice: CampaignChoice,
                   spec: Mapping) -> CampaignState | None:
    setting = catalog.settings.get(choice.key)
    lap = spec["played through"]
    if setting is None or choice.difficulty not in catalog.difficulty_flags or (
            choice.clear_data and choice.key not in lap["clear data"]):
        return None
    flags = {**scenario.cleared, **scenario.kept}
    for guid in file_flags(setting.files, scenario, spec["files"]):
        if guid is None:
            return None
        flags[guid] = True
    for guid in (*setting.flags, catalog.chapter_starts.get(setting.chapter),
                 catalog.difficulty_flags[choice.difficulty]):
        if guid is not None:
            flags[guid] = True
    unknown = scenario.booleans.difference(flags)
    if choice.clear_data:
        ends = {guid for chapter, guid in catalog.chapter_ends.items() if chapter != setting.chapter}
        flags = {guid: value for guid, value in flags.items() if guid not in ends}
        unknown |= ends
    return CampaignState(setting.chapter, choice.difficulty, choice.clear_data or bool(lap["value"]), flags,
                         frozenset(file_id for file_id, _read in setting.files), catalog.quests, frozenset(unknown))


def file_flags(files: tuple[tuple[int, bool], ...], scenario: ScenarioFlags, spec: Mapping) -> list[bytes | None]:
    found = []
    for file_id, read in files:
        kind = next((kind for kind in spec["kinds"] if kind["ids"][0] <= file_id < kind["ids"][1]), None)
        for name in () if kind is None else (kind["got"], *((kind["read"],) if read else ())):
            found.append(scenario.variable(name, kind["group"], file_id - kind["offset"]))
        if kind is None:
            found.append(None)
    return found


def choice_names(catalog: CampaignCatalog, rules: Mapping) -> CampaignChoices:
    spec, enums = rules["campaign"], rules.get("enums", {})
    keys, ranks = (enums.get(spec[part].get("enum"), {}) for part in ("settings", "difficulties"))
    return ([(key, keys.get(str(key), str(key))) for key in sorted(catalog.settings)],
            [(rank, ranks.get(str(rank), str(rank))) for rank in sorted(catalog.difficulty_flags)],
            [key for key in sorted(catalog.settings) if key in spec["played through"]["clear data"]])


def _guid_bytes(value) -> bytes:
    """A .uvar variable's GUID as stored (the .uvar reader makes it uuid.UUID(bytes=stored))."""
    return value.bytes if isinstance(value, uuid.UUID) else uuid.UUID(str(value)).bytes


def _value(value, default):
    return getattr(value, "value", default if value is None else value)


def _items(value) -> list:
    return list(value.values) if isinstance(value, ArrayData) else []


def _refs(value) -> list[int]:
    return [int(item.value) for item in _items(value) if isinstance(item, ObjectData) and item.value]


def _fields(rsz, instance: int) -> Mapping[str, object]:
    fields = rsz.parsed_elements.get(instance) if rsz is not None else None
    return fields if isinstance(fields, Mapping) else {}


def _root_fields(rsz) -> Mapping[str, object]:
    return _fields(rsz, rsz.object_table[0]) if rsz is not None and rsz.object_table else {}


def _instances(rsz, type_name: str):
    registry = rsz.type_registry
    for instance, fields in rsz.parsed_elements.items():
        info = registry.get_type_info(rsz.instance_infos[instance].type_id) or {}
        if info.get("name") == type_name and isinstance(fields, Mapping):
            yield fields
