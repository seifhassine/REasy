from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import QFileDialog

from file_handlers.pak import normalize_pak_path
from utils.app_paths import resource_path


@dataclass(frozen=True, slots=True)
class PakListSpec:
    game: str
    filename: str
    directory_markers: tuple[str, ...] = ()
    is_default: bool = True


PAK_LIST_SPECS = (
    PakListSpec("DD2", "DD2_STM.list", ("Dragons Dogma 2",)),
    PakListSpec("DMC5", "DMC5_STM.list", ("Devil May Cry 5",)),
    PakListSpec("MHRise", "MHR_STM.list", ("MonsterHunterRise",)),
    PakListSpec(
        "MHST3",
        "MHST3_STM.list",
        ("MONSTER_HUNTER_STORIES_3_TWISTED_REFLECTION",),
    ),
    PakListSpec("MHST3", "MHST3_DEMO_STM.list", is_default=False),
    PakListSpec("MHWilds", "MHWS_STM.list", ("MonsterHunterWilds",)),
    PakListSpec("O2", "O2_STM.list", ("Onimusha2",)),
    PakListSpec("OnimushaWOTS", "ONIWOTS_STM.list", ("OnimushaWotS_Demo",)),
    PakListSpec("Pragmata", "PRAGMATA_STM.list", ("PRAGMATA",)),
    PakListSpec(
        "Pragmata",
        "PRAGMATA_DEMO_STM.list",
        ("PRAGMATA SKETCHBOOK",),
        is_default=False,
    ),
    PakListSpec(
        "RE2RT",
        "RE2_RT_STM.list",
        ("RESIDENT EVIL 2  BIOHAZARD RE2",),
    ),
    PakListSpec(
        "RE2",
        "RE2_STM.list",
        ("RESIDENT EVIL 2  BIOHAZARD RE2",),
    ),
    PakListSpec("RE3RT", "RE3_RT_STM.list", ("RE3",)),
    PakListSpec("RE3", "RE3_STM.list", ("RE3",)),
    PakListSpec(
        "RE4",
        "RE4_STM.list",
        ("RESIDENT EVIL 4  BIOHAZARD RE4",),
    ),
    PakListSpec("RE7", "RE7_STM.list", ("RESIDENT EVIL 7 biohazard",)),
    PakListSpec("RE7RT", "RE7_RT_STM.list", ("RESIDENT EVIL 7 biohazard",)),
    PakListSpec(
        "RE8",
        "RE8_STM.list",
        ("Resident Evil Village BIOHAZARD VILLAGE",),
    ),
    PakListSpec("REResistance", "RER_STM.list"),
    PakListSpec("SF6", "SF6_STM.list", ("Street Fighter 6",)),
    PakListSpec(
        "RE9",
        "RE9_STM.list",
        ("RESIDENT EVIL requiem BIOHAZARD requiem",),
    ),
    PakListSpec("KunitsuGami", "KUNITSUGAMI_STM.list", ("KunitsuGami",)),
)

DEFAULT_PAK_FILE_LISTS: dict[str, str] = {
    spec.game: spec.filename for spec in PAK_LIST_SPECS if spec.is_default
}
_PAK_LIST_GAME_BY_NAME = {
    spec.filename.casefold(): spec.game for spec in PAK_LIST_SPECS
}

def choose_pak_list_file(parent) -> str:
    return QFileDialog.getOpenFileName(
        parent, parent.tr("Open list file"), str(resource_path("resources/data/lists")),
        parent.tr("List files (*.list *.txt);;All files (*)"),
    )[0]

def read_pak_list_file(path: str | Path) -> list[str]:
    with Path(path).open("r", encoding="utf-8") as f:
        return list(dict.fromkeys(
            item
            for line in f
            if (item := normalize_pak_path(line, lowercase=True))
        ))


def game_for_pak_list_path(path: str | Path) -> str | None:
    return _PAK_LIST_GAME_BY_NAME.get(Path(path).name.casefold())

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
    matches = [
        (len(marker), spec)
        for spec in PAK_LIST_SPECS
        for marker in spec.directory_markers
        if marker.lower() in name
    ]
    if not matches:
        return []
    longest_marker = max(length for length, _spec in matches)
    suggested: list[Path] = []
    for length, spec in matches:
        if length != longest_marker:
            continue
        candidate = lists_dir / spec.filename
        if candidate.is_file() and candidate not in suggested:
            suggested.append(candidate)
    return suggested
