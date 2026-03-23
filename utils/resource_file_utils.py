import os
from typing import Callable, Iterable, Optional, Tuple
from weakref import WeakKeyDictionary


_PAK_PATH_LOOKUP_CACHE: "WeakKeyDictionary[object, dict[str, str]]" = WeakKeyDictionary()
_DIR_ENTRIES_CACHE: dict[str, tuple[str, ...]] = {}


def _normalize_lookup_path(path: str) -> str:
    s = (path or "").replace("\\", "/").strip().rstrip("\x00")
    if s.startswith("@"):
        s = s[1:]
    return s.lstrip("/").lower()


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


def _select_matching_path(paths: list[str], parent=None) -> Optional[str]:
    if not paths:
        return None
    if len(paths) == 1:
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


def find_matching_pak_path(pak_cached_reader, patterns: Iterable[str], parent=None) -> Optional[str]:
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
            return _select_matching_path(matches, parent)
    return None


def _get_dir_entries(dir_path: str) -> tuple[str, ...]:
    cached_entries = _DIR_ENTRIES_CACHE.get(dir_path)
    if cached_entries is not None:
        return cached_entries

    entries = tuple(os.listdir(dir_path))
    _DIR_ENTRIES_CACHE[dir_path] = entries
    return entries


def _find_resource_in_root(resource_path: str, root_dir: str, path_prefix: str) -> Optional[Tuple[str, bytes]]:
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

    target_file = next(
        (os.path.join(dir_path, f) for f in _get_dir_entries(dir_path) if f == base_name or f.startswith(base_name + ".")),
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
        name = path if "." in os.path.basename(path) else (path + ("." + ext.lower() if ext else ""))
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
    pak_selected_paks,
    selection_parent=None,
) -> Optional[Tuple[str, bytes]]:
    candidates = _resource_path_candidates(resource_path)

    for c in candidates:
        hit = _find_resource_in_root(c, project_dir, path_prefix)
        if hit:
            return hit

    match = find_matching_pak_path(pak_cached_reader, candidates, selection_parent)
    pak_hit = _read_pak_path(match, pak_cached_reader) if match else None
    if pak_hit:
        return pak_hit

    for c in candidates:
        hit = _find_resource_in_root(c, unpacked_dir, path_prefix)
        if hit:
            return hit

    return None


def find_resource_in_paks(resource_path: str, pak_cached_reader, pak_selected_paks, selection_parent=None) -> Optional[Tuple[str, bytes]]:
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
    pak_selected_paks=None,
    should_overwrite: Callable[[str], bool] | None = None,
    selection_parent=None,
) -> Optional[str]:
    resolved = resolve_resource_data(
        resource_path,
        project_dir,
        unpacked_dir,
        path_prefix,
        pak_cached_reader,
        pak_selected_paks,
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
