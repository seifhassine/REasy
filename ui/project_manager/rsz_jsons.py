from __future__ import annotations

from pathlib import Path


DEFAULT_RSZ_JSONS: dict[str, str] = {
    "DD2": "rszdd2.json",
    "DMC5": "rszdmc5.json",
    "MHRise": "rszmhrise.json",
    "MHST3": "rszmhst3.json",
    "MHWilds": "rszmhwilds.json",
    "O2": "rszo2.json",
    "OnimushaWOTS": "rszoniwots.json",
    "Pragmata": "rszpragmata.json",
    "RE2RT": "rszre2rt.json",
    "RE2": "rszre2.json",
    "RE3RT": "rszre3rt.json",
    "RE3": "rszre3.json",
    "RE4": "rszre4.json",
    "RE7RT": "rszre7rt.json",
    "RE7": "rszre7.json",
    "RE8": "rszre8.json",
    "SF6": "rszsf6.json",
    "RE9": "rszre9.json",
    "REResistance": "rszreresistance.json",
    "KunitsuGami": "rszkunitsugami.json",
}


def find_default_rsz_json_path(game: str | None, base_dir: Path) -> Path | None:
    json_name = DEFAULT_RSZ_JSONS.get(game or "")
    if not json_name:
        return None

    candidates = (
        base_dir / "resources" / "data" / "dumps" / json_name,
        base_dir / json_name,
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def resolve_rsz_json_path(
    project_dir: Path | str,
    game: str | None,
    base_dir: Path,
    configured_path: object = None,
) -> Path | None:
    if isinstance(configured_path, str) and configured_path.strip():
        candidate = Path(configured_path.strip())
        if not candidate.is_absolute():
            candidate = Path(project_dir) / candidate
        if candidate.is_file():
            return candidate.resolve()

    default_path = find_default_rsz_json_path(game, base_dir)
    return default_path.resolve() if default_path else None
