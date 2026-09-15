"""Storage and discovery for timestamped file backups."""

from datetime import datetime
from pathlib import Path
import re

from utils.app_paths import backups_directory


BACKUP_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"


def create_backup(
    source_path: str | Path,
    data: bytes,
    *,
    backup_dir: str | Path | None = None,
    created_at: datetime | None = None,
) -> Path:
    destination = Path(backup_dir) if backup_dir is not None else backups_directory()
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = (created_at or datetime.now()).strftime(BACKUP_TIMESTAMP_FORMAT)
    backup_path = destination / f"{timestamp}_{Path(source_path).name}"
    backup_path.write_bytes(data)
    return backup_path


def find_backups(
    source_path: str | Path,
    *,
    backup_dir: str | Path | None = None,
) -> list[tuple[str, str, str]]:
    directory = Path(backup_dir) if backup_dir is not None else backups_directory()
    if not directory.is_dir():
        return []

    pattern = re.compile(rf"(\d{{8}}_\d{{6}})_{re.escape(Path(source_path).name)}$")
    matches = []
    for path in directory.iterdir():
        match = pattern.fullmatch(path.name)
        if not match:
            continue
        try:
            created_at = datetime.strptime(match.group(1), BACKUP_TIMESTAMP_FORMAT)
        except ValueError:
            continue
        matches.append((created_at.strftime("%Y-%m-%d %H:%M:%S"), str(path), path.name))
    return sorted(matches, reverse=True)
