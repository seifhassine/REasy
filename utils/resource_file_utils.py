import os
from typing import Iterable, Optional, Tuple


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


def find_matching_pak_path(pak_cached_reader, patterns: Iterable[str]) -> Optional[str]:
    if not pak_cached_reader:
        return None
    
    cached_norm = [(p, _normalize_lookup_path(p)) for p in pak_cached_reader.cached_paths(include_unknown=False)]
    for pattern in patterns:
        needle = _normalize_lookup_path(pattern)
        if not needle:
            continue
        hit = next((orig for orig, norm in cached_norm if norm == needle or norm.startswith(needle + ".")), None)
        if hit:
            return hit
    return None


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
        (os.path.join(dir_path, f) for f in os.listdir(dir_path) if f == base_name or f.startswith(base_name + ".")),
        None,
    )
    if not target_file:
        return None

    with open(target_file, "rb") as f:
        return target_file, f.read()


def _read_pak_path(path: str, pak_selected_paks) -> Optional[Tuple[str, bytes]]:
    if not path or not pak_selected_paks:
        return None
    try:
        from file_handlers.pak.reader import PakReader
        from file_handlers.pak.utils import guess_extension_from_header

        reader = PakReader()
        reader.pak_file_priority = list(pak_selected_paks)
        reader.add_files(path)

        for pth, stream in reader.find_files():
            if pth.lower() != path.lower():
                continue
            data = stream.read()
            try:
                ext = guess_extension_from_header(data[:64])
            except Exception:
                ext = None
            name = pth if "." in os.path.basename(pth) else (pth + ("." + ext.lower() if ext else ""))
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


def resolve_resource_data( resource_path: str, project_dir: str, unpacked_dir: str, path_prefix: str, pak_cached_reader, pak_selected_paks) -> Optional[Tuple[str, bytes]]:
    candidates = _resource_path_candidates(resource_path)

    for c in candidates:
        hit = _find_resource_in_root(c, project_dir, path_prefix)
        if hit:
            return hit

    match = find_matching_pak_path(pak_cached_reader, candidates)
    pak_hit = _read_pak_path(match, pak_selected_paks) if match else None
    if pak_hit:
        return pak_hit

    for c in candidates:
        hit = _find_resource_in_root(c, unpacked_dir, path_prefix)
        if hit:
            return hit

    return None


def find_resource_in_paks(resource_path: str, pak_cached_reader, pak_selected_paks) -> Optional[Tuple[str, bytes]]:
    match = find_matching_pak_path(pak_cached_reader, _resource_path_candidates(resource_path))
    return _read_pak_path(match, pak_selected_paks) if match else None


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


def copy_resource_to_project(resource_path: str, project_dir: str, unpacked_dir: str, path_prefix: str, pak_cached_reader=None, pak_selected_paks=None,) -> Optional[str]:
    resolved = resolve_resource_data( resource_path, project_dir, unpacked_dir, path_prefix, pak_cached_reader, pak_selected_paks)
    if not resolved:
        return None

    source_path, file_data = resolved
    if source_path and os.path.abspath(source_path).startswith(os.path.abspath(project_dir) + os.sep):
        return source_path

    relative_path = _resolve_destination_relative_path(resource_path, source_path, unpacked_dir, path_prefix)
    dest_path = os.path.join(project_dir, path_prefix.replace("/", os.sep), relative_path.replace("/", os.sep))
    
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    
    with open(dest_path, "wb") as f:
        f.write(file_data)
    
    return dest_path
