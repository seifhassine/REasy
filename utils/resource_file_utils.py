import os
import re
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple
from weakref import WeakKeyDictionary

from app_config import GAME_NATIVE_PATHS


_PAK_PATH_LOOKUP_CACHE: "WeakKeyDictionary[object, tuple[object, dict[str, list[str]]]]" = WeakKeyDictionary()
_DIR_PATH_LOOKUP_CACHE: dict[str, dict[str, str]] = {}
_PLATFORM_SUFFIX = r'\.(?:stm|x64)'
_VERSION_SUFFIX = rf'\.\d+(?:{_PLATFORM_SUFFIX})?'
_SUFFIX = rf'(?:{_VERSION_SUFFIX}|{_PLATFORM_SUFFIX})'
_RESOURCE_SUFFIX = re.compile(_SUFFIX + '$', re.I)
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
        require_unique: bool = False,
    ) -> Optional[Tuple[str, bytes]]:
        return resolve_resource_data(
            resource_path,
            self.project_dir,
            self.unpacked_dir,
            self.path_prefix,
            self.pak_cached_reader,
            selection_parent,
            allow_selection_dialog=allow_selection_dialog,
            game=self.game,
            require_unique=require_unique,
        )

    def locate(self, resource_path: str, *, require_unique: bool = False) -> "ResourceLocation | None":
        return next(_locations(resource_path, self.project_dir, self.unpacked_dir, self.path_prefix,
                               self.pak_cached_reader, None, allow_selection_dialog=False, game=self.game,
                               require_unique=require_unique), None)

    def with_pak_reader(self, reader) -> "ResourceResolutionContext":
        return replace(self, pak_cached_reader=reader)


@dataclass(frozen=True, slots=True)
class ResourceLocation:
    path: str
    pak: object | None = None

    def read(self) -> Optional[Tuple[str, bytes]]:
        if self.pak is not None:
            return _read_pak_path(self.path, self.pak)
        try:
            with open(self.path, "rb") as file:
                return self.path, file.read()
        except OSError:
            return None


# PAK decompression releases the GIL, so reads run in parallel.
_READERS = ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1), thread_name_prefix="Resource reads")


def _read_bytes(location: ResourceLocation) -> bytes | None:
    found = location.read()
    return None if found is None else found[1]


class PrefetchedFiles(Mapping):
    def __init__(self, files: Mapping = ()):
        self._files = {path: _READERS.submit(_read_bytes, value) if isinstance(value, ResourceLocation) else value
                       for path, value in files.items()}

    def __getitem__(self, path: str) -> bytes | None:
        value = self._files[path]
        return value.result() if isinstance(value, Future) else value

    def __iter__(self) -> Iterator[str]:
        return iter(self._files)

    def __len__(self) -> int:
        return len(self._files)


def normalize_resource_path(path: str) -> str:
    s = (path or "").replace("\\", "/").strip().rstrip("\x00")
    if s.startswith("@"):
        s = s[1:]
    return s.lstrip("/")


def resource_version_from_path(path: str, extension: str) -> int | None:
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


def resource_path_at_version(path: str, extension: str, version: int) -> str:
    current = resource_version_from_path(path, extension)
    if current is None:
        return resource_path_with_version(path, extension, version)
    pattern = rf"(?i)(\.{re.escape(extension.lstrip('.'))}\.){current}(?=\.|$)"
    return re.sub(pattern, rf"\g<1>{int(version)}", path, count=1)


def streaming_companion_path(path: str) -> str | None:
    split = None if path.replace("\\", "/").endswith("/") else natives_split(path)
    if split is None:
        return None
    root, rest = split
    rest = rest[1:] if rest[0].casefold() == "streaming" else ("streaming", *rest)
    return "/".join((root.as_posix(), *rest))


def natives_split(path) -> tuple[Path, tuple[str, ...]] | None:
    parts = Path(path).parts
    natives = next((index for index in range(len(parts) - 3, -1, -1) if parts[index].casefold() == "natives"), None)
    if natives is None:
        return None
    return Path(*parts[: natives + 2]), parts[natives + 2 :]


def natives_relative(path) -> str | None:
    split = natives_split(path)
    return None if split is None else "/".join((*split[0].parts[-2:], *split[1]))


def build_resource_path(path: str, game: str) -> str:
    """Qualifying before searching keeps a stale project copy from shadowing the PAK version."""
    extension = normalize_resource_path(path).rsplit(".", 1)[-1].casefold()
    if not game or extension not in {"mmtr", "mmtrs"}:
        return path
    from file_handlers.mmtr.profiles import mmtr_profile_for_game

    profile = mmtr_profile_for_game(game)
    version = profile.build_resource_version if profile is not None else None
    return resource_path_with_version(path, extension, version) if version is not None else path


def resource_key(path) -> str:
    path = os.path.normpath(str(path).rstrip('\0').removeprefix('@')).replace('\\', '/').casefold()
    path = re.sub(r'^.*?natives/[^/]+/', '', path)
    return re.sub(_VERSION_SUFFIX + '$', '', path)


def resource_path_key(path: str) -> str:
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


def _select_matching_path(paths: list[str], parent=None, *, allow_dialog: bool = True, require_unique: bool = False) -> Optional[str]:
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    if require_unique:
        raise ValueError('Multiple resource versions match; select an explicit file: ' + ', '.join(paths))
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


def _build_pak_path_lookup(pak_cached_reader) -> dict[str, list[str]]:
    cached_known_paths = getattr(pak_cached_reader, "cached_known_paths", None)
    paths = list(
        cached_known_paths()
        if callable(cached_known_paths)
        else pak_cached_reader.cached_paths(include_unknown=False)
    )

    lookup: dict[str, list[str]] = {}
    for original_path in paths:
        normalized_path = _normalize_lookup_path(original_path)
        for lookup_key in _iter_lookup_keys(normalized_path):
            lookup.setdefault(lookup_key, []).append(original_path)
    return lookup


def _match_pak_lookup(
    lookup: dict[str, list[str]],
    patterns: Iterable[str],
    parent=None,
    *,
    allow_selection_dialog: bool = True,
    require_unique: bool = False,
) -> tuple[bool, Optional[str]]:
    for pattern in patterns:
        needle = _normalize_lookup_path(pattern)
        if not needle:
            continue
        matches = lookup.get(needle)
        if matches:
            if require_unique:
                matches = list({resource_path_key(p): p for p in matches}.values())
                exact = [p for p in matches if resource_path_key(p) == needle]
                matches = exact or [p for p in matches if _RESOURCE_SUFFIX.fullmatch(resource_path_key(p)[len(needle):])]
                if not matches:
                    continue
            return (
                True,
                _select_matching_path(
                    matches,
                    parent,
                    allow_dialog=allow_selection_dialog,
                    require_unique=require_unique,
                ),
            )
    return False, None


def find_matching_pak_path(
    pak_cached_reader,
    patterns: Iterable[str],
    parent=None,
    *,
    allow_selection_dialog: bool = True,
    require_unique: bool = False,
) -> Optional[str]:
    if not pak_cached_reader:
        return None

    revision = getattr(pak_cached_reader, "path_lookup_revision", None)
    cached = _PAK_PATH_LOOKUP_CACHE.get(pak_cached_reader)
    rebuilt = cached is None or (revision is not None and cached[0] != revision)
    if rebuilt:
        cached_norm = _build_pak_path_lookup(pak_cached_reader)
        _PAK_PATH_LOOKUP_CACHE[pak_cached_reader] = revision, cached_norm
    else:
        cached_norm = cached[1]

    pattern_list = list(patterns)
    options = {"allow_selection_dialog": allow_selection_dialog, "require_unique": require_unique}
    found, match = _match_pak_lookup(cached_norm, pattern_list, parent, **options)
    if found or revision is not None or rebuilt:
        return match

    cached_norm = _build_pak_path_lookup(pak_cached_reader)
    _PAK_PATH_LOOKUP_CACHE[pak_cached_reader] = revision, cached_norm
    return _match_pak_lookup(cached_norm, pattern_list, parent, **options)[1]


def _get_dir_path_lookup(dir_path: str) -> dict[str, str]:
    lookup = _DIR_PATH_LOOKUP_CACHE.get(dir_path)
    if lookup is None:
        lookup = {}
        for entry in os.listdir(dir_path):
            for key in _iter_lookup_keys(entry):
                lookup.setdefault(key, entry)
            if entry.startswith("."):
                lookup.setdefault("", entry)
        _DIR_PATH_LOOKUP_CACHE[dir_path] = lookup
    return lookup


def _locate_in_root(
    resource_path: str,
    root_dir: str,
    path_prefix: str,
    *,
    cache_entries: bool = True,
    require_unique: bool = False,
) -> Optional[str]:
    if not root_dir or not os.path.isdir(root_dir):
        return None

    normalized_resource = _normalize_lookup_path(resource_path)
    prefix = path_prefix.strip("/").lower()
    full_path = normalized_resource if normalized_resource.startswith(("natives/", prefix + "/")) else f"{prefix}/{normalized_resource}"

    base_file_path = os.path.join(root_dir, full_path.replace("/", os.sep))
    dir_path = os.path.dirname(base_file_path)
    base_name = os.path.basename(base_file_path)
    if not os.path.isdir(dir_path):
        return None

    if require_unique:
        return locate_local_resource(Path(base_file_path), require_unique=True)

    if cache_entries:
        target_name = _get_dir_path_lookup(dir_path).get(base_name)
    else:
        prefix = base_name + "."
        target_name = next(
            (entry for entry in os.listdir(dir_path)
             if entry == base_name or entry.startswith(prefix)),
            None,
        )
    if target_name is None:
        return None
    return os.path.join(dir_path, target_name)


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
    return "/".join(GAME_NATIVE_PATHS.get(game, ("natives", "stm")))


def _locations(
    resource_path: str,
    project_dir: str,
    unpacked_dir: str,
    path_prefix: str,
    pak_cached_reader,
    selection_parent=None,
    *,
    allow_selection_dialog: bool = True,
    game: str = "",
    require_unique: bool = False,
) -> Iterator[ResourceLocation]:
    resource_path = build_resource_path(resource_path, game)
    candidates = _resource_path_candidates(resource_path)
    for c in candidates:
        found = _locate_in_root(c, project_dir, path_prefix, cache_entries=False, require_unique=require_unique)
        if found:
            yield ResourceLocation(found)
    match = find_matching_pak_path(
        pak_cached_reader,
        candidates,
        selection_parent,
        allow_selection_dialog=allow_selection_dialog,
        require_unique=require_unique,
    )
    if match:
        yield ResourceLocation(match, pak_cached_reader)
    for c in candidates:
        found = _locate_in_root(c, unpacked_dir, path_prefix, require_unique=require_unique)
        if found:
            yield ResourceLocation(found)


def resolve_resource_data(
    resource_path: str,
    project_dir: str,
    unpacked_dir: str,
    path_prefix: str,
    pak_cached_reader,
    selection_parent=None,
    *,
    allow_selection_dialog: bool = True,
    game: str = "",
    require_unique: bool = False,
) -> Optional[Tuple[str, bytes]]:
    locations = _locations(resource_path, project_dir, unpacked_dir, path_prefix, pak_cached_reader,
                           selection_parent, allow_selection_dialog=allow_selection_dialog, game=game,
                           require_unique=require_unique)
    return next(filter(None, (location.read() for location in locations)), None)




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


def locate_handler_resource(handler, resource_path: str) -> ResourceLocation | None:
    context = resource_context_for_handler(handler)
    return context.locate(resource_path) if context is not None else None


def resolve_handler_resource_data(
    handler,
    resource_path: str,
    selection_parent=None,
    *,
    allow_selection_dialog: bool = True,
    require_unique: bool = False,
) -> Optional[Tuple[str, bytes]]:
    context = resource_context_for_handler(handler)
    return (
        context.resolve(
            resource_path,
            selection_parent,
            allow_selection_dialog=allow_selection_dialog,
            require_unique=require_unique,
        )
        if context is not None
        else None
    )


def locate_local_resource(path: Path, *, require_unique: bool = False) -> Optional[str]:
    path = Path(os.path.normpath(path))
    if path.is_file():
        return str(path)
    if not path.parent.is_dir():
        return None
    prefix = path.name.casefold() + '.'
    matches = [p for p in path.parent.iterdir() if p.is_file() and p.name.casefold().startswith(prefix)]
    if require_unique:
        matches = [p for p in matches if _RESOURCE_SUFFIX.fullmatch(p.name[len(path.name):])]
    return _select_matching_path([str(p) for p in matches], allow_dialog=False, require_unique=require_unique)


def find_local_resource(path: Path, *, require_unique: bool = False) -> Optional[Tuple[str, bytes]]:
    found = locate_local_resource(path, require_unique=require_unique)
    return (found, Path(found).read_bytes()) if found else None


def resolve_companion_resource(handler, path: str, source_path: str = '') -> Optional[Tuple[str, bytes]]:
    path = str(path).replace('\\', '/').rstrip('\0').removeprefix('@')
    if not path:
        return None
    local = Path(path)
    if local.is_absolute():
        return find_local_resource(local, require_unique=True)
    source = Path(source_path or str(getattr(handler, 'filepath', '') or ''))
    candidates = [source.parent / local] if source.is_file() else [local]
    native = re.match(r'(?i)(.*?)(natives/[^/]+/)', source.as_posix())
    if native and source.is_file():
        rooted = path if path.casefold().startswith('natives/') else native[2] + path
        candidates.append(Path(native[1]) / rooted)
    for candidate in dict.fromkeys(candidates):
        hit = find_local_resource(candidate, require_unique=True)
        if hit:
            return hit
    if not path.casefold().startswith('natives/'):
        owner = source.as_posix()[len(native[1]):] if native else source.as_posix()
        if not Path(owner).is_absolute():
            relative = os.path.normpath(str(Path(owner).parent / local))
            hit = resolve_handler_resource_data(handler, relative, allow_selection_dialog=False, require_unique=True)
            if hit:
                return hit
    return resolve_handler_resource_data(handler, path, allow_selection_dialog=False, require_unique=True)


def nearby_resource_paths(handler, extension: str, *, source_path=None, parent_levels: int = 0,
                          child_levels: int = 0) -> tuple[str, ...]:
    source = Path(source_path or str(getattr(handler, 'filepath', '') or ''))
    context = resource_context_for_handler(handler)
    resource = natives_relative(source) or (f'{context.path_prefix}/{source.as_posix()}'
                                            if context and not source.is_absolute() else '')
    directories = [Path(resource).parent] if resource else []
    if directories:
        directories += list(directories[0].parents)[:parent_levels]
    pattern = re.compile(rf'\.{re.escape(extension)}{_SUFFIX}?$', re.I)
    candidates = []

    def collect(directory, depth=0):
        if directory.is_dir():
            candidates.extend(sorted(str(p) for p in directory.iterdir() if p.is_file() and pattern.search(p.name)))
            if depth:
                for folder in sorted(p for p in directory.iterdir() if p.is_dir()):
                    collect(folder, depth - 1)

    if source.is_file():
        searched = list(source.parents)[:parent_levels + 1]
        for index, directory in enumerate(searched):
            collect(directory, child_levels if index == len(searched) - 1 else 0)
    for index, directory in enumerate(directories):
        if context and context.project_dir:
            collect(Path(context.project_dir) / directory, child_levels if index == len(directories) - 1 else 0)
    reader = context.pak_cached_reader if context else None
    if reader:
        known = getattr(reader, 'cached_known_paths', None)
        paths = known() if callable(known) else reader.cached_paths(include_unknown=False)
        parents = {p.as_posix().casefold() for p in directories}
        top = directories[-1].as_posix().casefold() if directories else None

        def near(folder: str) -> bool:
            if folder in parents:
                return True
            for _level in range(child_levels):
                folder = folder.rsplit('/', 1)[0] if '/' in folder else ''
                if folder == top:
                    return True
            return False
        candidates.extend(sorted(p for p in paths if pattern.search(p)
                                 and near(normalize_resource_path(p).casefold().rsplit('/', 1)[0])))
    for index, directory in enumerate(directories):
        if context and context.unpacked_dir:
            collect(Path(context.unpacked_dir) / directory, child_levels if index == len(directories) - 1 else 0)
    unique = {}
    for candidate in candidates:
        key = resource_path_key(candidate)
        native = re.search(r'(?:^|/)(natives/.*)', key)
        unique.setdefault(native[1] if native else key, candidate)
    return tuple(unique.values())




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
    *,
    game: str = "",
) -> Optional[str]:
    resolved = resolve_resource_data(
        resource_path,
        project_dir,
        unpacked_dir,
        path_prefix,
        pak_cached_reader,
        selection_parent,
        game=game,
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
