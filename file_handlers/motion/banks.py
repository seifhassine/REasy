"""See docs/internals/motion-editor.md, Preview."""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass

lazy from file_handlers.motbank.motbank_file import MotbankFile
lazy from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData
lazy from file_handlers.rsz.scn_scene_graph import normalize_scene_path
lazy from utils.resource_file_utils import resolve_handler_resource_data

from .decode import _flag, read_mot, read_motlist, read_motpack
from .model import Motion, MotionList, MotionListEntry, MotionPack, MotionPackEntry, ReferencePath, Sequence
from .rules import display_names, list_profile

_MOTION_FILE = re.compile(r"\.(motbank|motlist)(\.\d+)?$", re.I)


@dataclass(frozen=True, slots=True)
class BankReference:
    owner: str
    path: str
    holders: tuple = ()


def names_motions(rsz_file) -> bool:
    return any(next(_paths(fields.values(), {}, set()), None) is not None
               for fields in rsz_file.parsed_elements.values() if isinstance(fields, dict))


def bank_references(graphs) -> list[BankReference]:
    found: dict[tuple[str, str], BankReference] = {}

    def add(name: str, path: str, owner) -> None:
        held = found.get((name, path.casefold()))
        holders = () if held is None else held.holders
        found[name, path.casefold()] = BankReference(name, path, holders + ((owner,) if owner not in holders else ()))
    for graph in graphs:
        for document in graph.documents.values():
            elements = document.rsz_file.parsed_elements
            for component in document.components.values():
                owner = document.objects.get(component.owner)
                name = owner.name if owner is not None else ""
                given = document.resources.get(component.id, {})
                values = [value for key, value in component.fields.items() if not isinstance(given.get(key), str)]
                for path in (*(path for path in _paths(values, elements, set())
                               if (component.id, normalize_scene_path(path)) not in graph.lent), *(
                        path for value in given.values() for path in ((value,) if isinstance(value, str) else value)
                        if _MOTION_FILE.search(path))):
                    add(name, path, component.owner)
        for host, (_kind, _components, paths) in graph.shells.items():
            for path in paths:
                add(graph.documents[host.document_id].objects[host].name, path, host)
    return list(found.values())


def _paths(values, elements: dict, seen: set[int]):
    for value in values:
        if isinstance(value, ArrayData):
            yield from _paths(value.values, elements, seen)
        elif isinstance(value, ObjectData):
            if value.value not in seen and isinstance(fields := elements.get(value.value), dict):
                seen.add(value.value)
                yield from _paths(fields.values(), elements, seen)
        elif isinstance(text := getattr(value, "value", None), str) and _MOTION_FILE.search(text := text.rstrip("\0")):
            yield text


def list_paths(read, path: str) -> list[tuple[int, str]]:
    if ".motlist" in path.casefold():
        return [(0, path)]
    if (data := read(path)) is None:
        return []
    bank = MotbankFile()
    bank.read(data)
    return [(item.bank_id, item.path) for item in bank.items if item.path]


def bank_joint_map(read, path: str) -> str:
    if ".motlist" in path.casefold() or (data := read(path)) is None:
        return ""
    bank = MotbankFile()
    bank.read(data)
    return bank.jmap_path


def handler_reader(handler):
    return lambda path: (hit := resolve_handler_resource_data(handler, path, allow_selection_dialog=False)) and hit[1]


def read_list(read, path: str) -> MotionList | None:
    data = read(path)
    return None if data is None else read_motlist(data)


def entry_motion(read, motion_list: MotionList, entry: MotionListEntry,
                 lists: dict[str, MotionList | MotionPack | None]) -> Motion | None:
    seen: set[str] = set()
    while not isinstance(entry.item, Motion):
        if isinstance(entry.item, ReferencePath):
            data = read(entry.item.path)
            return read_mot(data) if data is not None and data[4:8] == b"mot " else None
        if _pack_row(motion_list, entry):
            return None if (held := _packed(_pack(read, motion_list, entry, lists), entry.source_key)) is None \
                else held.item
        if entry.item is not None or not motion_list.base_list or motion_list.base_list in seen:
            return None
        seen.add(motion_list.base_list)
        motion_list, entry = _base_row(read, motion_list, _source_id(motion_list, entry), lists)
        if entry is None:
            return None
    return entry.item


def stop_row(read, motion_list: MotionList, lists: dict[str, MotionList | MotionPack | None]) -> MotionListEntry | None:
    base = _base_row(read, motion_list, -1, lists)[0] if motion_list.base_list else None
    known = None if base is None else {held.motion_id for held in base.entries}
    for entry in motion_list.entries:
        if entry.item is not None:
            continue
        if _pack_row(motion_list, entry):
            if (pack := _pack(read, motion_list, entry, lists)) is not None and _packed(pack, entry.source_key) is None:
                return entry
        elif known is not None and _source_id(motion_list, entry) not in known:
            return entry
    return None


def entry_element(read, motion_list: MotionList, entry: MotionListEntry,
                  lists: dict[str, MotionList | MotionPack | None]) -> MotionListEntry | None:
    seen: set[str] = set()
    while entry is not None and entry.item is None and not _keyed(motion_list, entry):
        if not motion_list.base_list or motion_list.base_list in seen:
            return None
        seen.add(motion_list.base_list)
        motion_list, entry = _base_row(read, motion_list, entry.motion_id, lists)
    return entry


def entry_mirrored(read, motion_list: MotionList, entry: MotionListEntry,
                   lists: dict[str, MotionList | MotionPack | None]) -> bool:
    return (found := entry_element(read, motion_list, entry, lists)) is not None and bool(
        found.flags & _flag("ElementFlags", "Flags_Mirror"))


def entry_reversed(read, motion_list: MotionList, entry: MotionListEntry,
                   lists: dict[str, MotionList | MotionPack | None]) -> bool:
    return list_profile(motion_list.version).reverse and (
        found := entry_element(read, motion_list, entry, lists)) is not None and bool(
        found.flags & _flag("ElementFlags", "Flags_Reverse"))


def entry_sequences(read, motion_list: MotionList, entry: MotionListEntry,
                    lists: dict[str, MotionList | MotionPack | None], seen: frozenset = frozenset()) -> list[Sequence]:
    if entry.item is None and not _keyed(motion_list, entry):
        found = (None, None) if not motion_list.base_list or motion_list.base_list in seen else _base_row(
            read, motion_list, entry.motion_id, lists)
        played = [] if found[1] is None else entry_sequences(read, *found, lists, seen | {motion_list.base_list})
    else:
        motion = entry_motion(read, motion_list, entry, lists)
        played = [] if motion is None or entry_reversed(read, motion_list, entry, lists) else list(
            {sequence.category: sequence for sequence in motion.sequences}.values())
    slots: dict[int, list[Sequence]] = {}
    for sequence in played:
        slots.setdefault(sequence.category, []).append(sequence)
    masked = list_profile(motion_list.version).row_sequences == "mask"
    for sequence in entry.overrides:
        slots[sequence.category] = [sequence, *slots.get(sequence.category, ())] if masked and \
            entry.additional_sequences >> sequence.category & 1 else [sequence]
    return [sequence for category in sorted(slots) if category in display_names("SequenceCategory")
            for sequence in slots[category]]


def _keyed(motion_list: MotionList, entry: MotionListEntry) -> bool:
    return list_profile(motion_list.version).source_key != "ignored" and entry.source_key != 0xFFFFFFFF


def _source_id(motion_list: MotionList, entry: MotionListEntry) -> int:
    return entry.source_key if _keyed(motion_list, entry) else entry.motion_id


def _pack_row(motion_list: MotionList, entry: MotionListEntry) -> bool:
    return entry.item is None and _keyed(motion_list, entry) and entry.source_bank != 0xFF \
        and list_profile(motion_list.version).source_key == "bank"


def _pack(read, motion_list: MotionList, entry: MotionListEntry, lists: dict) -> MotionPack | None:
    packs = motion_list.motion_packs
    path = packs[entry.source_bank] if entry.source_bank < len(packs) else None
    if path is not None and path not in lists:
        data = read(path)
        lists[path] = None if data is None else read_motpack(data)
    return lists.get(path)


def _packed(pack: MotionPack | None, key: int) -> MotionPackEntry | None:
    """Binary search, as the engine looks IDs up: an unsorted pack misses as it does in game."""
    ids = [] if pack is None else [held.motion_id for held in pack.entries]
    at = bisect_left(ids, key)
    return pack.entries[at] if at < len(ids) and ids[at] == key else None


def _base_row(read, motion_list: MotionList, motion_id: int, lists: dict) -> tuple[MotionList | None,
                                                                                   MotionListEntry | None]:
    path = motion_list.base_list
    if path not in lists:
        lists[path] = read_list(read, path)
    base = lists[path]
    return base, None if base is None else next((held for held in base.entries if held.motion_id == motion_id), None)
