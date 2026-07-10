"""Stable paths for application resources and user-visible local data."""

import os
import sys
from pathlib import Path


def application_root() -> Path:
    """Return the source root, or the executable directory in a frozen build."""
    if getattr(sys, "frozen", False):
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_path(relative_path: str | os.PathLike[str], *, required: bool = False) -> Path:
    """Resolve an application resource without depending on the caller's module."""
    relative = Path(relative_path)
    candidates = (application_root() / relative, Path.cwd() / relative)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if required:
        raise FileNotFoundError(f"Could not find resource: {relative}")
    return candidates[0]


def backups_directory() -> Path:
    return application_root() / "backups"
