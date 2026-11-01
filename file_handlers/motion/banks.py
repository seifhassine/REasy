"""See docs/internals/motion-editor.md, Preview."""

from __future__ import annotations

import re
from dataclasses import dataclass

lazy from file_handlers.motbank.motbank_file import MotbankFile
lazy from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData
lazy from utils.resource_file_utils import resolve_handler_resource_data

from .decode import read_mot, read_motlist
from .model import Motion, MotionList, MotionListEntry, ReferencePath
from .rules import list_profile

_MOTION_FILE = re.compile(r"\.(motbank|motlist)(\.\d+)?$", re.I)


@dataclass(frozen=True, slots=True)
class BankReference:
    owner: str
    path: str


def names_motions(rsz_file) -> bool:
    return any(next(_paths(fields.values(), {}, set()), None) is not None
               for fields in rsz_file.parsed_elements.values() if isinstance(fields, dict))


def bank_references(graphs) -> list[BankReference]:
    found: dict[tuple[str, str], BankReference] = {}
    for graph in graphs:
        for document in graph.documents.values():
            elements = document.rsz_file.parsed_elements
            for component in document.components.values():
                owner = document.objects.get(component.owner)
                for path in _paths(component.fields.values(), elements, set()):
                    reference = BankReference(owner.name if owner is not None else "", path)
                    found.setdefault((reference.owner, path.casefold()), reference)
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


def list_paths(handler, path: str) -> list[tuple[int, str]]:
    if ".motlist" in path.casefold():
        return [(0, path)]
    hit = resolve_handler_resource_data(handler, path, allow_selection_dialog=False)
    if hit is None:
        return []
    bank = MotbankFile()
    bank.read(hit[1])
    return [(item.bank_id, item.path) for item in bank.items if item.path]


def read_list(handler, path: str) -> MotionList | None:
    hit = resolve_handler_resource_data(handler, path, allow_selection_dialog=False)
    return None if hit is None else read_motlist(hit[1])


def entry_motion(handler, motion_list: MotionList, entry: MotionListEntry,
                 lists: dict[str, MotionList | None]) -> Motion | None:
    """``lists`` caches the base lists read."""
    seen: set[str] = set()
    while not isinstance(entry.item, Motion):
        if isinstance(entry.item, ReferencePath):
            hit = resolve_handler_resource_data(handler, entry.item.path, allow_selection_dialog=False)
            return read_mot(hit[1]) if hit is not None and hit[1][4:8] == b"mot " else None
        path = motion_list.base_list
        if entry.item is not None or not path or path in seen:
            return None
        seen.add(path)
        if path not in lists:
            lists[path] = read_list(handler, path)
        keyed = "source_key" in list_profile(motion_list.version).records["row"].names
        key = entry.source_key if keyed else entry.motion_id
        motion_list = lists[path]
        entry = None if motion_list is None else next((held for held in motion_list.entries if held.motion_id == key),
                                                      None)
        if entry is None:
            return None
    return entry.item
