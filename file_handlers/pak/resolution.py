"""RE Engine reader classification, mount scheduling, and lookup priority."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Sequence


class PriorityFamily(Enum):
    LEGACY_REVERSE = "legacy_reverse"
    FOUR_BAND = "four_band"
    PDLC = "pdlc"
    NUMBERED_PATCH = "numbered_patch"
    MODERN = "modern"


class GateFamily(Enum):
    NONE = "none"
    OLD = "old"
    CURRENT = "current"
    RE4 = "re4"


class DlcMountSchedule(Enum):
    PER_FAMILY = "per_family"
    ROOTS_THEN_SUBS = "roots_then_subs"


@dataclass(frozen=True)
class PakResolutionProfile:
    name: str
    priority: PriorityFamily
    gate: GateFamily
    witness_types: tuple[int, ...]
    dlc_schedule: DlcMountSchedule = DlcMountSchedule.PER_FAMILY


@dataclass(frozen=True)
class PakReaderInfo:
    path: str
    reader_type: int
    family: str
    family_number: int | None
    patch: int | None
    sub: int | None
    feature_flags: int
    version: tuple[int, int]
    mount_index: int = -1

    @property
    def is_main(self) -> bool:
        return self.reader_type == 1


_PROFILES = {
    "legacy": PakResolutionProfile(
        "legacy reverse", PriorityFamily.LEGACY_REVERSE, GateFamily.OLD, (2,)
    ),
    "four_old": PakResolutionProfile(
        "four-band classic", PriorityFamily.FOUR_BAND, GateFamily.OLD, (2, 3)
    ),
    "four_current": PakResolutionProfile(
        "four-band/current", PriorityFamily.FOUR_BAND, GateFamily.CURRENT, (2, 3)
    ),
    "pdlc_old": PakResolutionProfile(
        "pDLC/old", PriorityFamily.PDLC, GateFamily.OLD, (2, 3)
    ),
    "pdlc_re4": PakResolutionProfile(
        "pDLC/RE4", PriorityFamily.PDLC, GateFamily.RE4, (2, 3)
    ),
    "numbered": PakResolutionProfile(
        "numbered-patch/current",
        PriorityFamily.NUMBERED_PATCH,
        GateFamily.CURRENT,
        (2, 3),
    ),
    "modern": PakResolutionProfile(
        "modern feature/current", PriorityFamily.MODERN, GateFamily.CURRENT, (2, 3)
    ),
    "modern_roots_first": PakResolutionProfile(
        "modern feature/current (DLC roots before subs)",
        PriorityFamily.MODERN,
        GateFamily.CURRENT,
        (2, 3),
        DlcMountSchedule.ROOTS_THEN_SUBS,
    ),
}


def _game_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold()) if value else ""


_GAME_PROFILES = {
    # I haven't tested RE7 non RT. Probably same as legacy reader used by RE2 non-RT and DMC5.
    "re7": _PROFILES["legacy"],
    "dmc5": _PROFILES["legacy"],
    "re2": _PROFILES["legacy"],
    "re3": _PROFILES["four_old"],
    "reresistance": _PROFILES["four_old"],
    "re2rt": _PROFILES["four_old"],
    "re3rt": _PROFILES["four_old"],
    "re7rt": _PROFILES["four_old"],
    "re8": _PROFILES["four_current"],
    "mhrise": _PROFILES["pdlc_old"],
    "re4": _PROFILES["pdlc_re4"],
    "sf6": _PROFILES["numbered"],
    "dd2": _PROFILES["numbered"],
    "kunitsugami": _PROFILES["numbered"],
    "mhwilds": _PROFILES["modern"],
    "pragmata": _PROFILES["modern"],
    "mhst3": _PROFILES["modern"],
    "o2": _PROFILES["modern"],
    "onimushawots": _PROFILES["modern"],
    "re9": _PROFILES["modern_roots_first"],
}


_PATCH_RE = re.compile(r"\.patch_(\d+)\.pak(?:$|\.)", re.IGNORECASE)
_SUB_RE = re.compile(r"\.sub_(\d+)\.pak(?:$|\.)", re.IGNORECASE)
_APP_ID_RE = re.compile(r"_stm_(\d+)(?:\.|_)", re.IGNORECASE)


def classify_reader(
    path: str,
    feature_flags: int,
    version: tuple[int, int],
) -> PakReaderInfo:
    normalized = path.replace("\\", "/")
    pak_path = Path(normalized)
    name = pak_path.name.casefold()
    parent = pak_path.parent.name.casefold()

    if parent == "pdlc" or "pdlc" in name:
        reader_type = 3
    elif parent == "dlc" or parent.isdigit() or name.startswith("re_dlc_"):
        reader_type = 2
    else:
        reader_type = 1

    patch_match = _PATCH_RE.search(name)
    sub_match = _SUB_RE.search(name)
    app_match = _APP_ID_RE.search(name)
    family_number = int(app_match.group(1)) if app_match else None
    if family_number is None and parent.isdigit():
        family_number = int(parent)

    if reader_type == 1:
        family = "main"
    elif family_number is not None:
        family = f"{reader_type}:{family_number}"
    else:
        family = f"{reader_type}:{name.split('.pak', 1)[0]}"

    return PakReaderInfo(
        path=normalized,
        reader_type=reader_type,
        family=family,
        family_number=family_number,
        patch=int(patch_match.group(1)) if patch_match else None,
        sub=int(sub_match.group(1)) if sub_match else None,
        feature_flags=feature_flags,
        version=version,
    )


def select_profile(game: str | None) -> tuple[PakResolutionProfile, str | None, str]:
    explicit_key = _game_key(game)
    if explicit_key in _GAME_PROFILES:
        return _GAME_PROFILES[explicit_key], game, "explicit"

    # Install paths and PAK headers cannot distinguish several builds. For example
    # RE2/RE3/RE7 RT versus non-RT). Project/session game identity is therefore
    # authoritative. Callers without it get the documented future-facing
    # fallback rather than a false folder-name inference.
    return _PROFILES["modern"], None, "fallback"


def _family_key(reader: PakReaderInfo) -> tuple[int, int, str]:
    if reader.family_number is not None:
        return (0, reader.family_number, "")
    return (1, 0, reader.family)


def _member_key(reader: PakReaderInfo) -> int:
    return 0 if reader.sub is None else reader.sub + 1


def _patch_key(reader: PakReaderInfo) -> tuple[int, int]:
    return (0, 0) if reader.patch is None else (1, reader.patch)


def _physical_mount_key(
    reader: PakReaderInfo,
    profile: PakResolutionProfile,
) -> tuple:
    member = _member_key(reader)
    patch = _patch_key(reader)
    path_key = reader.path.casefold()

    if reader.reader_type == 1:
        return (0, member, patch, path_key)

    phase = 1 if reader.reader_type == 2 else 2 if reader.reader_type == 3 else 3
    family = _family_key(reader)
    if profile.dlc_schedule is DlcMountSchedule.ROOTS_THEN_SUBS:
        return (phase, member, family, patch, path_key)
    return (phase, family, member, patch, path_key)


def assign_mount_order(
    readers: Sequence[PakReaderInfo],
    profile: PakResolutionProfile,
) -> list[PakReaderInfo]:
    mounted = sorted(readers, key=lambda reader: _physical_mount_key(reader, profile))
    return [replace(reader, mount_index=index) for index, reader in enumerate(mounted)]


def _effective_key(
    reader: PakReaderInfo,
    profile: PakResolutionProfile,
) -> tuple[int, int, int]:
    later = -reader.mount_index
    is_patch = reader.patch is not None
    priority = profile.priority

    if priority is PriorityFamily.LEGACY_REVERSE:
        if reader.is_main and is_patch:
            return (0, later, 0)
        if not reader.is_main and not is_patch:
            return (1, reader.mount_index, 0)
        if not reader.is_main:
            return (2, later, 0)
        return (3, later, 0)

    if priority is PriorityFamily.FOUR_BAND:
        if reader.is_main and is_patch:
            return (0, later, 0)
        if reader.is_main:
            return (1, later, 0)
        if is_patch:
            return (2, later, 0)
        return (3, later, 0)

    if priority is PriorityFamily.PDLC:
        if reader.reader_type == 3:
            return (0, later, 0)
        if reader.is_main and is_patch:
            return (1, later, 0)
        if reader.is_main:
            return (2, later, 0)
        if is_patch:
            return (3, later, 0)
        return (4, later, 0)

    if priority is PriorityFamily.NUMBERED_PATCH:
        if reader.reader_type == 3:
            return (0, later, 0)
        if reader.is_main and is_patch:
            return (1, -int(reader.patch or 0), later)
        if reader.is_main:
            return (2, later, 0)
        if is_patch:
            return (3, later, 0)
        return (4, later, 0)

    if reader.reader_type == 3:
        return (0, later, 0)
    if not reader.is_main and (reader.feature_flags & 0x10):
        return (1, later, 0)
    if reader.is_main and is_patch:
        return (2, -int(reader.patch or 0), later)
    if reader.is_main:
        return (3, later, 0)
    if is_patch:
        return (4, later, 0)
    return (5, later, 0)


def order_readers(
    readers: Sequence[PakReaderInfo],
    profile: PakResolutionProfile,
) -> list[PakReaderInfo]:
    mounted = assign_mount_order(readers, profile)
    return sorted(mounted, key=lambda reader: _effective_key(reader, profile))


def entry_is_gated(attributes: int, gate: GateFamily, reader_has_bit20: bool) -> bool:
    mask = attributes & 0x70
    if gate is GateFamily.NONE:
        return False
    if gate is GateFamily.OLD:
        return (mask & 0x10) != 0
    if gate is GateFamily.CURRENT:
        return (mask & 0x30) != 0 and (mask & 0x60) != 0x40
    if mask == 0x10:
        return not reader_has_bit20
    return mask in {0x20, 0x30, 0x60, 0x70}


__all__ = [
    "DlcMountSchedule",
    "GateFamily",
    "PakReaderInfo",
    "PakResolutionProfile",
    "PriorityFamily",
    "classify_reader",
    "entry_is_gated",
    "order_readers",
    "select_profile",
]
