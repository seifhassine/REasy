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
}

def find_default_pak_list_path(game: str | None, base_dir: Path) -> Path | None:
    if not game:
        return None
    list_name = DEFAULT_PAK_FILE_LISTS.get(game)
    if not list_name:
        return None

    candidate = base_dir / "resources" / "data" / "lists" / list_name
    return candidate if candidate.is_file() else None
