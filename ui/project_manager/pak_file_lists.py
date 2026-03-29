from __future__ import annotations

from pathlib import Path

DEFAULT_PAK_FILE_LISTS: dict[str, str] = {
    "DD2": "DD2_STM.list",
    "DMC5": "DMC5_STM.list",
    "MHRise": "MHR_STM.list",
    "MHST3": "MHST3_DEMO_STM.list",
    "MHWilds": "MHWS_STM.list",
    "O2": "O2_STM.list",
    "Pragmata": "PRAGMATA_DEMO_STM.list",
    "RE2RT": "RE2_RT_STM.list",
    "RE2": "RE2_STM.list",
    "RE3RT": "RE3_RT_STM.list",
    "RE3": "RE3_STM.list",
    "RE4": "RE4_STM.list",
    "RE7RT": "RE7_RT_STM.list",
    "RE7": "RE7_STM.list",
    "RE8": "RE8_STM.list",
    "SF6": "SF6_STM.list",
    "RE9": "RE9_STM.list",
    "KunitsuGami": "KUNITSUGAMI_STM.list",
}

DIRECTORY_NAME_PAK_LIST_SUGGESTIONS: dict[str, tuple[str, ...]] = {
    "Devil May Cry 5": ("DMC5_STM.list",),
    "Dragons Dogma 2": ("DD2_STM.list",),
    "KunitsuGami": ("KUNITSUGAMI_STM.list",),
    "MonsterHunterRise": ("MHR_STM.list",),
    "MonsterHunterWilds": ("MHWS_STM.list",),
    "Onimusha2": ("O2_STM.list",),
    "RE3": ("RE3_RT_STM.list", "RE3_STM.list"),
    "RESIDENT EVIL 2  BIOHAZARD RE2": ("RE2_RT_STM.list", "RE2_STM.list"),
    "RESIDENT EVIL 4  BIOHAZARD RE4": ("RE4_STM.list",),
    "RESIDENT EVIL 7 biohazard": ("RE7_STM.list", "RE7_RT_STM.list"),
    "requiem": ("RE9_STM.list",),
    "Resident Evil Village BIOHAZARD VILLAGE": ("RE8_STM.list",),
    "Street Fighter 6": ("SF6_STM.list",),
}

def find_default_pak_list_path(game: str | None, base_dir: Path) -> Path | None:
    if not game:
        return None
    list_name = DEFAULT_PAK_FILE_LISTS.get(game)
    if not list_name:
        return None

    candidate = base_dir / "resources" / "data" / "lists" / list_name
    return candidate if candidate.is_file() else None

def find_suggested_pak_list_paths_for_directory(directory_name: str, base_dir: Path) -> list[Path]:
    name = (directory_name or "").strip().lower()
    if not name:
        return []
    lists_dir = base_dir / "resources" / "data" / "lists"
    suggested: list[Path] = []
    for marker, list_names in DIRECTORY_NAME_PAK_LIST_SUGGESTIONS.items():
        if marker.lower() not in name:
            continue
        for list_name in list_names:
            candidate = lists_dir / list_name
            if candidate.is_file() and candidate not in suggested:
                suggested.append(candidate)
    return suggested