import os
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Optional, Tuple
from weakref import WeakKeyDictionary


_PAK_PATH_LOOKUP_CACHE: "WeakKeyDictionary[object, dict[str, str]]" = WeakKeyDictionary()
_DIR_ENTRIES_CACHE: dict[str, tuple[str, ...]] = {}
ResourceDataLoader = Callable[[str], Optional[Tuple[str, bytes]]]


@dataclass(frozen=True, slots=True)
class ResourceResolutionContext:
    """The owning project and resource sources for an opened asset."""

    project_dir: str = ""
    unpacked_dir: str = ""
    path_prefix: str = "natives/stm"
    pak_cached_reader: object | None = None
    game: str = ""

    def resolve(
        self,
        resource_path: str,
        selection_parent=None,
        *,
        allow_selection_dialog: bool = True,
    ) -> Optional[Tuple[str, bytes]]:
        return resolve_resource_data(
            resource_path,
            self.project_dir,
            self.unpacked_dir,
            self.path_prefix,
            self.pak_cached_reader,
            selection_parent,
            allow_selection_dialog=allow_selection_dialog,
        )

    def with_pak_reader(self, reader) -> "ResourceResolutionContext":
        return replace(self, pak_cached_reader=reader)


def normalize_resource_path(path: str) -> str:
    """Return the canonical display form of an RE Engine resource path."""
    s = (path or "").replace("\\", "/").strip().rstrip("\x00")
    if s.startswith("@"):
        s = s[1:]
    return s.lstrip("/")


def resource_version_from_path(path: str, extension: str) -> int | None:
    """Return the numeric version after a resource extension.

    Supports ordinary names (file.tex.11) and platform variants (file.oft.1.x64).
    """

    name = normalize_resource_path(path).rsplit("/", 1)[-1].casefold()
    extension = extension.casefold().lstrip(".")
    parts = name.split(".")
    for index, part in enumerate(parts[:-1]):
        if part == extension and parts[index + 1].isdecimal():
            return int(parts[index + 1])
    return None


def resource_path_with_version(path: str, extension: str, version: int) -> str:
    """Append a version unless the resource path already contains one."""

    if resource_version_from_path(path, extension) is not None:
        return path
    name_parts = normalize_resource_path(path).rsplit("/", 1)[-1].split(".")
    extension = extension.casefold().lstrip(".")
    if len(name_parts) >= 2 and name_parts[-2].casefold() == extension:
        if name_parts[-1].casefold() in {"stm", "x64"}:
            suffix_at = path.rfind(".")
            return f"{path[:suffix_at]}.{int(version)}{path[suffix_at:]}"
    return f"{path}.{int(version)}"


def resource_path_key(path: str) -> str:
    """Return a case-insensitive cache key for an RE Engine resource path."""
    return normalize_resource_path(path).casefold()


def _normalize_lookup_path(path: str) -> str:
    return resource_path_key(path)


def _resource_path_candidates(resource_path: str) -> list[str]:
    base = _normalize_lookup_path(resource_path)
    if not base:
        return []
    if base.startswith(("natives/stm/", "natives/x64/")):
        return [base]
    return [f"natives/stm/{base}", f"natives/x64/{base}", base]


def _iter_lookup_keys(path: str):
    current = path
    while current:
        yield current
        slash_idx = current.rfind("/")
        dot_idx = current.rfind(".")
        if dot_idx <= slash_idx:
            break
        current = current[:dot_idx]


def _select_matching_path(paths: list[str], parent=None, *, allow_dialog: bool = True) -> Optional[str]:
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    if not allow_dialog:
        return paths[0]

    try:
        from PySide6.QtWidgets import QInputDialog

        selected, ok = QInputDialog.getItem(
            parent,
            "Select Resource",
            "Multiple matching resources were found. Choose one:",
            paths,
            0,
            False,
        )
        return selected if ok and selected else None
    except Exception:
        return paths[0]


def find_matching_pak_path(
    pak_cached_reader,
    patterns: Iterable[str],
    parent=None,
    *,
    allow_selection_dialog: bool = True,
) -> Optional[str]:
    if not pak_cached_reader:
        return None

    cached_norm = _PAK_PATH_LOOKUP_CACHE.get(pak_cached_reader)
    if cached_norm is None:
        cached_norm = {}
        for original_path in pak_cached_reader.cached_paths(include_unknown=False):
            normalized_path = _normalize_lookup_path(original_path)
            for lookup_key in _iter_lookup_keys(normalized_path):
                cached_norm.setdefault(lookup_key, []).append(original_path)
        _PAK_PATH_LOOKUP_CACHE[pak_cached_reader] = cached_norm

    for pattern in patterns:
        needle = _normalize_lookup_path(pattern)
        if not needle:
            continue
        matches = cached_norm.get(needle)
        if matches:
            return _select_matching_path(
                matches,
                parent,
                allow_dialog=allow_selection_dialog,
            )
    return None


def _get_dir_entries(dir_path: str) -> tuple[str, ...]:
    cached_entries = _DIR_ENTRIES_CACHE.get(dir_path)
    if cached_entries is not None:
        return cached_entries

    entries = tuple(os.listdir(dir_path))
    _DIR_ENTRIES_CACHE[dir_path] = entries
    return entries


def _find_resource_in_root(
    resource_path: str,
    root_dir: str,
    path_prefix: str,
    *,
    cache_entries: bool = True,
) -> Optional[Tuple[str, bytes]]:
    if not root_dir or not os.path.isdir(root_dir):
        return None

    normalized_resource = _normalize_lookup_path(resource_path)
    prefix = path_prefix.strip("/").lower()
    full_path = normalized_resource if normalized_resource.startswith(prefix + "/") else f"{prefix}/{normalized_resource}"

    base_file_path = os.path.join(root_dir, full_path.replace("/", os.sep))
    dir_path = os.path.dirname(base_file_path)
    base_name = os.path.basename(base_file_path)
    if not os.path.isdir(dir_path):
        return None

    entries = _get_dir_entries(dir_path) if cache_entries else os.listdir(dir_path)
    target_file = next(
        (os.path.join(dir_path, f) for f in entries if f == base_name or f.startswith(base_name + ".")),
        None,
    )
    if not target_file:
        return None

    with open(target_file, "rb") as f:
        return target_file, f.read()


def _read_pak_path(path: str, pak_cached_reader) -> Optional[Tuple[str, bytes]]:
    if not path or not pak_cached_reader or not hasattr(pak_cached_reader, "get_file"):
        return None

    try:
        stream = pak_cached_reader.get_file(path)
        if stream is None:
            return None

        from file_handlers.pak.utils import guess_extension_from_header

        data = stream.read()
        try:
            ext = guess_extension_from_header(data[:64])
        except Exception:
            ext = None
        if "." in os.path.basename(path):
            name = path
        elif ext:
            name = f"{path}.{ext.lower()}"
        else:
            name = path
        return name, data
    except Exception as e:
        print(f"PAK search error: {e}")
    
    return None


def get_path_prefix_for_game(game: str) -> str:
    try:
        from ui.project_manager.constants import EXPECTED_NATIVE
        
        if game and game in EXPECTED_NATIVE:
            return "/".join(EXPECTED_NATIVE[game])
    except Exception:
        pass
    
    return "natives/stm"


def resolve_resource_data(
    resource_path: str,
    project_dir: str,
    unpacked_dir: str,
    path_prefix: str,
    pak_cached_reader,
    selection_parent=None,
    *,
    allow_selection_dialog: bool = True,
) -> Optional[Tuple[str, bytes]]:
    candidates = _resource_path_candidates(resource_path)

    for c in candidates:
        hit = _find_resource_in_root(
            c,
            project_dir,
            path_prefix,
            cache_entries=False,
        )
        if hit:
            return hit

    match = find_matching_pak_path(
        pak_cached_reader,
        candidates,
        selection_parent,
        allow_selection_dialog=allow_selection_dialog,
    )
    pak_hit = _read_pak_path(match, pak_cached_reader) if match else None
    if pak_hit:
        return pak_hit

    for c in candidates:
        hit = _find_resource_in_root(c, unpacked_dir, path_prefix)
        if hit:
            return hit

    return None


def resolve_app_resource_data(
    app,
    resource_path: str,
    selection_parent=None,
    *,
    allow_selection_dialog: bool = True,
) -> Optional[Tuple[str, bytes]]:
    context = resource_context_for_app(app)
    return (
        context.resolve(
            resource_path,
            selection_parent,
            allow_selection_dialog=allow_selection_dialog,
        )
        if context is not None
        else None
    )


def resource_context_for_app(
    app,
    *,
    project_dir: str | os.PathLike | None = None,
    game: str = "",
) -> ResourceResolutionContext | None:
    """Build a resolution context for the asset's owning project."""
    if app is None:
        return None
    project = getattr(app, "proj_dock", None)
    if project is None:
        return None

    root = os.fspath(project_dir or getattr(project, "project_dir", "") or "")
    unpacked_dir = ""
    reader = None
    ensure_context = getattr(project, "ensure_project_pak_context", None)
    if root and callable(ensure_context):
        unpacked_dir, reader = ensure_context(root)
    else:
        unpacked_dir = str(getattr(project, "unpacked_dir", "") or "")
        reader = getattr(project, "_pak_cached_reader", None)

    infer_game = getattr(project, "infer_project_game", None)
    inferred_game = infer_game(root) if root and callable(infer_game) else ""
    resolved_game = str(
        inferred_game
        or game
        or getattr(project, "current_game", "")
        or getattr(app, "current_game", "")
        or getattr(getattr(app, "project_manager", None), "current_game", "")
        or ""
    )
    return ResourceResolutionContext(
        project_dir=root,
        unpacked_dir=str(unpacked_dir or ""),
        path_prefix=get_path_prefix_for_game(resolved_game),
        pak_cached_reader=reader,
        game=resolved_game,
    )


def resource_context_for_handler(handler) -> ResourceResolutionContext | None:
    context = getattr(handler, "resource_context", None)
    if context is None:
        context = resource_context_for_app(
            getattr(handler, "app", None),
            game=str(getattr(handler, "game_version", "") or ""),
        )
        if context is not None:
            handler.resource_context = context
    return context


def resolve_handler_resource_data(
    handler,
    resource_path: str,
    selection_parent=None,
    *,
    allow_selection_dialog: bool = True,
) -> Optional[Tuple[str, bytes]]:
    context = resource_context_for_handler(handler)
    return (
        context.resolve(
            resource_path,
            selection_parent,
            allow_selection_dialog=allow_selection_dialog,
        )
        if context is not None
        else None
    )


def find_resource_in_paks(resource_path: str, pak_cached_reader, selection_parent=None) -> Optional[Tuple[str, bytes]]:
    match = find_matching_pak_path(pak_cached_reader, _resource_path_candidates(resource_path), selection_parent)
    return _read_pak_path(match, pak_cached_reader) if match else None


def find_resource_in_filesystem(resource_path: str, unpacked_dir: str, path_prefix: str) -> Optional[Tuple[str, bytes]]:
    for c in _resource_path_candidates(resource_path):
        hit = _find_resource_in_root(c, unpacked_dir, path_prefix)
        if hit:
            return hit
    return None


def _resolve_destination_relative_path(resource_path: str, source_path: str | None, unpacked_dir: str, path_prefix: str) -> str:
    prefix = path_prefix.strip("/").lower()
    if source_path:
        normalized_source = source_path.replace("\\", "/").strip()
        if os.path.isabs(source_path) and unpacked_dir:
            try:
                rel_from_unpack = os.path.relpath(source_path, unpacked_dir).replace("\\", "/")
                if not rel_from_unpack.startswith(".."):
                    normalized_source = rel_from_unpack
            except Exception:
                pass
        if normalized_source.lower().startswith(prefix + "/"):
            return normalized_source[len(prefix) + 1 :]

    normalized_resource = _normalize_lookup_path(resource_path)
    if normalized_resource.startswith(prefix + "/"):
        return normalized_resource[len(prefix) + 1 :]
    return normalized_resource


def copy_resource_to_project(
    resource_path: str,
    project_dir: str,
    unpacked_dir: str,
    path_prefix: str,
    pak_cached_reader=None,
    should_overwrite: Callable[[str], bool] | None = None,
    selection_parent=None,
) -> Optional[str]:
    resolved = resolve_resource_data(
        resource_path,
        project_dir,
        unpacked_dir,
        path_prefix,
        pak_cached_reader,
        selection_parent,
    )
    if not resolved:
        return None

    source_path, file_data = resolved
    in_project = bool(
        source_path
        and os.path.abspath(source_path).startswith(os.path.abspath(project_dir) + os.sep)
    )

    relative_path = _resolve_destination_relative_path(resource_path, source_path, unpacked_dir, path_prefix)
    if in_project:
        dest_path = source_path
    else:
        dest_path = os.path.join(project_dir, path_prefix.replace("/", os.sep), relative_path.replace("/", os.sep))
    if os.path.exists(dest_path):
        if callable(should_overwrite) and not should_overwrite(dest_path):
            return None

    if in_project:
        return dest_path

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    
    with open(dest_path, "wb") as f:
        f.write(file_data)
    
    return dest_path
