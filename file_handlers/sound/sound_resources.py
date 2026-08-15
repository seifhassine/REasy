"""Shared path, loading, and opening helpers for profiled sound resources."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from utils.resource_file_utils import resource_context_for_handler


@dataclass(frozen=True, slots=True)
class RelatedSoundPaths:
    bank: str
    index_pck: str
    streaming_pck: str


def resource_key(path: str) -> str:
    """Return a stable game-relative key for local, project, or PAK paths."""

    value = str(path or "").replace("\\", "/").strip().lstrip("@").casefold()
    marker = value.find("natives/")
    return value[marker:] if marker >= 0 else value.lstrip("/")


def matching_sound_companion_path(path: str, profile=None) -> str | None:
    """Return a location hint; media matching still requires runtime validation."""

    if profile is None:
        from .sound_profile import sound_profile_for_path

        profile = sound_profile_for_path(path)
    return profile.matching_companion_path(path) if profile is not None else None


def local_sound_path(handler, path: str) -> Path | None:
    """Resolve a logical sound path to its likely local project location."""

    value = str(path or "")
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate.resolve()
    key = resource_key(path)
    context = resource_context_for_handler(handler)
    project_dir = str(getattr(context, "project_dir", "") or "")
    if project_dir:
        return (Path(project_dir) / Path(key.replace("/", "\\"))).resolve()
    current = str(getattr(handler, "filepath", "") or "").replace("\\", "/")
    marker = current.casefold().find("natives/")
    if marker >= 0 and Path(current).is_absolute():
        return (Path(current[:marker]) / Path(key.replace("/", "\\"))).resolve()
    return Path(current).parent / Path(key).name if current else None


def local_sound_directories(handler, path: str) -> tuple[Path, ...]:
    """Return project/unpacked directories corresponding to a logical path."""

    roots = []
    context = resource_context_for_handler(handler)
    if context is not None:
        roots.extend((context.project_dir, context.unpacked_dir))
    current = str(getattr(handler, "filepath", "") or "").replace("\\", "/")
    marker = current.casefold().find("natives/")
    if marker >= 0 and Path(current).is_absolute():
        roots.append(current[:marker])
    parent = Path(resource_key(path).replace("/", "\\")).parent
    return tuple(dict.fromkeys(
        (Path(root) / parent).resolve() for root in roots if root
    ))


def read_sound_resource(
    handler,
    path: str,
    *,
    cache: bool = True,
) -> tuple[str, bytes] | None:
    """Read a related project, PAK, unpacked, or local sound resource."""

    key = resource_key(path)
    if key == resource_key(getattr(handler, "filepath", "")):
        return key, bytes(handler.raw_data)
    pending = getattr(handler, "pending_related_outputs", dict)()
    if key in pending:
        return key, bytes(pending[key])
    cached = getattr(handler, "cached_related_input", lambda _path: None)(key)
    if cached is not None:
        return key, cached

    context = resource_context_for_handler(handler)
    resolved = (
        context.resolve(key, allow_selection_dialog=False)
        if context is not None
        else None
    )
    reader = getattr(context, "pak_cached_reader", None) if context else None
    pak_stream = reader.get_file(key) if resolved is None and reader else None
    local = local_sound_path(handler, key)
    if resolved is not None:
        data = bytes(resolved[1])
    elif pak_stream is not None:
        data = pak_stream.read()
    elif local is not None and local.is_file():
        data = local.read_bytes()
    else:
        return None
    if cache:
        getattr(handler, "cache_related_input", lambda *_args: None)(key, data)
    return key, data


def open_sound_resource(handler, path: str) -> bool:
    """Open a local or PAK-backed sound path in the owning REasy window."""

    path = str(path or "")
    if not path:
        return False
    app = getattr(handler, "app", None)
    opener = getattr(app, "_open_path", None) if app else None
    local = local_sound_path(handler, path)
    if local is not None and local.is_file() and callable(opener) and opener(str(local)):
        return True
    project = getattr(app, "proj_dock", None) if app else None
    pak_opener = getattr(project, "_open_pak_path_in_editor", None)
    return bool(
        not os.path.isabs(path)
        and callable(pak_opener)
        and pak_opener(resource_key(path))
    )


__all__ = [
    "RelatedSoundPaths",
    "local_sound_directories",
    "local_sound_path",
    "matching_sound_companion_path",
    "open_sound_resource",
    "read_sound_resource",
    "resource_key",
]
