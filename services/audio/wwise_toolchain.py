"""Game-specific Wwise version requirements and installation validation."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Mapping, MutableMapping

from file_handlers.sound.sound_profile import (
    SoundGameProfile,
    sound_profile_for_bank_version,
    sound_profile_for_game,
    sound_profiles,
)


@dataclass(frozen=True, order=True)
class WwiseVersion:
    year: int
    major: int
    minor: int = 0
    build: int = 0

    @property
    def family(self):
        return self.year, self.major

    @property
    def text(self):
        values = (self.year, self.major, self.minor, self.build)
        return ".".join(map(str, values if self.build else values[:-1]))


@dataclass(frozen=True)
class WwiseInstallation:
    root: Path
    cli_path: Path
    version: WwiseVersion
    profile: SoundGameProfile


class WwiseToolchainError(ValueError):
    pass


_CLI_NAMES = ("WwiseCLI.exe", "WwiseConsole.exe", "WwiseCLI", "WwiseConsole")
_CLI_NAME_KEYS = {name.casefold() for name in _CLI_NAMES}
_VERSION_RE = re.compile(r"(?i)(?:Wwise[_\s|-]*|\bv)?(20\d{2})\.(\d+)\.(\d+)(?:\.(\d+))?")
_CLI_DIRS = (
    (),
    ("Authoring", "x64", "Release", "bin"),
    ("Authoring", "Win32", "Release", "bin"),
    ("x64", "Release", "bin"),
    ("Win32", "Release", "bin"),
    ("Release", "bin"),
    ("bin",),
)


def _game_key(game):
    profile = sound_profile_for_game(game)
    return profile.game if profile else re.sub(r"[^A-Z0-9]", "", str(game or "").upper())


def wwise_profile_for_game(game):
    profile = sound_profile_for_game(game)
    return profile if profile is not None and profile.required_year else None


def wwise_profile_for_bank_version(bank_version):
    profile = sound_profile_for_bank_version(bank_version)
    return profile if profile is not None and profile.required_year else None


def require_wwise_profile(game):
    profile = wwise_profile_for_game(game)
    if profile is None:
        supported = sorted(
            item.game for item in sound_profiles() if item.required_year
        )
        raise WwiseToolchainError(
            f"Wwise media authoring is not configured for {game or 'this game'}. "
            f"Currently supported: {', '.join(supported)}."
        )
    return profile


def _version_from_text(text):
    match = _VERSION_RE.search(str(text or ""))
    return WwiseVersion(*(int(value or 0) for value in match.groups())) if match else None


def _version_from_mapping(value):
    if not isinstance(value, Mapping):
        return None
    try:
        return WwiseVersion(*(int(value.get(key, 0)) for key in ("year", "major", "minor", "build")))
    except (TypeError, ValueError):
        return None


def _metadata_version(root):
    for name in ("install-entry.json", "bundle.json"):
        try:
            payload = json.loads((root / name).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        bundle = payload.get("bundle", {}) if isinstance(payload, Mapping) else {}
        for value in (bundle.get("version") if isinstance(bundle, Mapping) else None, payload.get("version")):
            version = _version_from_mapping(value)
            if version:
                return version
    return None


def _installation_root(cli_path):
    for parent in cli_path.parents:
        if any((parent / name).is_file() for name in ("install-entry.json", "bundle.json")):
            return parent
        if parent.name.casefold() == "authoring":
            return parent.parent
    return cli_path.parent


def _probe_version(cli_path):
    try:
        result = subprocess.run(
            [str(cli_path), "-help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _version_from_text(result.stdout + "\n" + result.stderr)


def inspect_wwise_cli(cli_path):
    cli_path = Path(cli_path).expanduser()
    if not cli_path.is_file() or cli_path.name.casefold() not in _CLI_NAME_KEYS:
        raise WwiseToolchainError(f"Expected an existing WwiseCLI or WwiseConsole executable: {cli_path}")
    root = _installation_root(cli_path)
    version = _metadata_version(root) or _version_from_text(root) or _probe_version(cli_path)
    if version is None:
        raise WwiseToolchainError(f"Could not determine the Wwise version installed at {root}.")
    return root, version


def _candidate_cli_paths(selected):
    selected = Path(selected).expanduser()
    if selected.is_file():
        return [selected]
    roots = [selected, *list(selected.parents)[:4]]
    with suppress(OSError):
        roots.extend(path for path in selected.iterdir() if path.is_dir() and path.name.casefold().startswith("wwise"))
    candidates = []
    for root in roots:
        for relative in _CLI_DIRS:
            for name in _CLI_NAMES:
                path = root.joinpath(*relative, name)
                if path.is_file() and path not in candidates:
                    candidates.append(path)
    return candidates


def validate_wwise_installation(selected_path, game):
    profile = require_wwise_profile(game)
    candidates = _candidate_cli_paths(selected_path)
    if not candidates:
        raise WwiseToolchainError(
            f"No Wwise command-line tool was found in '{selected_path}'.\n\n{profile.requirement_message()} Select the folder containing Authoring."
        )
    inspected, errors = [], []
    for cli_path in candidates:
        try:
            root, version = inspect_wwise_cli(cli_path)
            inspected.append((root, cli_path, version))
        except WwiseToolchainError as exc:  # noqa: PERF203 - inspect all candidates
            errors.append(str(exc))
    compatible = [item for item in inspected if profile.accepts(item[2])]
    if compatible:
        root, cli_path, version = max(compatible, key=lambda item: item[2])
        return WwiseInstallation(root, cli_path, version, profile)
    if inspected:
        versions = ", ".join(sorted({item[2].text for item in inspected}))
        raise WwiseToolchainError(f"The selected installation contains Wwise {versions}, but {profile.requirement_message()}")
    raise WwiseToolchainError(f"{errors[0] if errors else 'Version detection failed.'}\n\n{profile.requirement_message()}")


def configured_wwise_path(settings, game):
    paths = settings.get("wwise_install_paths", {})
    value = paths.get(_game_key(game), "") if isinstance(paths, Mapping) else ""
    return str(value).strip() if value else ""


def set_configured_wwise_path(settings: MutableMapping, game, path):
    current = settings.get("wwise_install_paths", {})
    paths = dict(current) if isinstance(current, Mapping) else {}
    paths[_game_key(game)] = str(path)
    settings["wwise_install_paths"] = paths


def suggested_wwise_browse_path(configured_path=""):
    configured = Path(configured_path).expanduser() if configured_path else None
    if configured and configured.exists():
        return str(configured if configured.is_dir() else configured.parent)
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            for relative in (("Audiokinetic",), ("Program Files", "Audiokinetic"), ("Program Files (x86)", "Audiokinetic")):
                candidate = Path(f"{letter}:\\").joinpath(*relative)
                if candidate.is_dir():
                    return str(candidate)
    return str(Path.home())


__all__ = [
    "WwiseInstallation",
    "WwiseToolchainError",
    "WwiseVersion",
    "configured_wwise_path",
    "inspect_wwise_cli",
    "require_wwise_profile",
    "set_configured_wwise_path",
    "suggested_wwise_browse_path",
    "validate_wwise_installation",
    "wwise_profile_for_bank_version",
    "wwise_profile_for_game",
]
