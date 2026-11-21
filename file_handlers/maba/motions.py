"""See docs/internals/maba-format.md, Editing."""
from __future__ import annotations

from dataclasses import dataclass

from file_handlers.motion.banks import bank_entries, entry_motion, read_list


@dataclass(frozen=True, slots=True)
class BankMotion:
    name: str
    end_frame: float
    list_path: str


def bank_motions(read, motbank: str) -> dict[tuple[int, int], BankMotion]:
    found = {}
    for bank_id, path, _kind, _mask in bank_entries(read, motbank):
        lists, motion_list = {}, read_list(read, path)
        for entry in () if motion_list is None else motion_list.entries:
            if (motion := entry_motion(read, motion_list, entry, lists)) is not None:
                found.setdefault((bank_id, entry.motion_id), BankMotion(motion.name, motion.end_frame, path))
    return found
