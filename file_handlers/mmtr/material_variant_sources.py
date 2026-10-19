from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
from typing import Callable, Iterable

from file_handlers.mdf.mdf_file import MdfFile
from utils.app_paths import PROJECT_METADATA_FOLDER
from utils.resource_file_utils import normalize_resource_path, resource_version_from_path


@dataclass(frozen=True, slots=True)
class MdfConsumers:
    choices: tuple
    unread: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class MdfVariantSourceChoice:
    label: str
    path: str
    data: bytes
    origin: str = "disk"
    relative_path: str = ""
    matching_material_indices: tuple[int, ...] = ()
    owner: object | None = None

    @property
    def suggested_output_path(self) -> str:
        return normalize_resource_path(self.relative_path or Path(self.path).name)


def _path_key(origin: str, relative_path: str) -> tuple[str, str]:
    return str(origin).casefold(), normalize_resource_path(relative_path).casefold()


def _raw_may_reference(data: bytes, mmtr_resource_path: str) -> bool:
    needle = normalize_resource_path(mmtr_resource_path).casefold()
    if not needle:
        return False
    for offset in (0, 1):
        decoded = data[offset:].decode("utf-16le", errors="ignore")
        if needle in decoded.replace("\\", "/").casefold():
            return True
    return needle.encode("utf-8") in data.replace(b"\\", b"/").lower()


def matching_material_indices(
    data: bytes,
    path: str,
    mmtr_resource_path: str,
    *,
    raw_prefilter: bool = True,
) -> tuple[int, ...]:
    if raw_prefilter and not _raw_may_reference(data, mmtr_resource_path):
        return ()
    mdf = MdfFile()
    mdf.read(bytes(data), path)
    wanted = normalize_resource_path(mmtr_resource_path).casefold()
    return tuple(
        index
        for index, material in enumerate(mdf.materials)
        if normalize_resource_path(material.header.mmtr_path).casefold() == wanted
    )


def make_mdf_source_choice(
    *,
    data: bytes,
    path: str,
    mmtr_resource_path: str,
    origin: str,
    relative_path: str = "",
    label: str = "",
    owner=None,
    require_match: bool = True,
) -> MdfVariantSourceChoice | None:
    matches = matching_material_indices(data, path, mmtr_resource_path, raw_prefilter=require_match)
    if require_match and not matches:
        return None
    normalized_relative = normalize_resource_path(relative_path)
    return MdfVariantSourceChoice(
        label=str(label or normalized_relative or path),
        path=str(path),
        data=bytes(data),
        origin=str(origin or "disk").casefold(),
        relative_path=normalized_relative,
        matching_material_indices=matches,
        owner=owner,
    )


def _iter_mdf_files(root: str):
    if not root or not os.path.isdir(root):
        return
    for directory, folders, files in os.walk(root):
        folders[:] = [folder for folder in folders if folder != PROJECT_METADATA_FOLDER]
        folders.sort(key=str.casefold)
        for name in sorted(files, key=str.casefold):
            if resource_version_from_path(name, "mdf2") is None:
                continue
            absolute = os.path.join(directory, name)
            relative = os.path.relpath(absolute, root).replace("\\", "/")
            yield absolute, relative


def discover_mdf_consumers(
    *,
    mmtr_resource_path: str,
    project_dir: str = "",
    unpacked_dir: str = "",
    pak_paths: Iterable[str] = (),
    pak_loader: Callable[[str], bytes | None] | None = None,
    open_sources: Iterable[MdfVariantSourceChoice] = (),
    progress: Callable[[int, str], None] | None = None,
) -> MdfConsumers:
    choices: list[MdfVariantSourceChoice] = []
    unread: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    visited = 0

    def report(label: str) -> None:
        nonlocal visited
        visited += 1
        if progress is not None and (visited == 1 or visited % 25 == 0):
            progress(visited, label)

    def add(choice: MdfVariantSourceChoice | None) -> None:
        if choice is None:
            return
        key = _path_key(choice.origin, choice.relative_path or choice.path)
        if key in seen:
            return
        seen.add(key)
        choices.append(choice)

    def read(label: str, **arguments) -> MdfVariantSourceChoice | None:
        try:
            return make_mdf_source_choice(mmtr_resource_path=mmtr_resource_path, label=label, **arguments)
        except ValueError as exc:
            unread.append((label, str(exc)))
            return None

    for source in open_sources:
        report(source.label)
        add(read(source.label, data=source.data, path=source.path, origin=source.origin,
                 relative_path=source.relative_path, owner=source.owner))

    for origin, root in (("project", project_dir), ("system", unpacked_dir)):
        for absolute, relative in _iter_mdf_files(root):
            key = _path_key(origin, relative)
            if key in seen:
                continue
            report(relative)
            try:
                data = Path(absolute).read_bytes()
            except OSError as exc:
                unread.append((relative, str(exc)))
                continue
            add(read(relative, data=data, path=absolute, origin=origin, relative_path=relative))

    if pak_loader is not None:
        candidates = sorted(
            {
                normalize_resource_path(path)
                for path in pak_paths
                if resource_version_from_path(path, "mdf2") is not None
            },
            key=str.casefold,
        )
        candidates = [
            relative
            for relative in candidates
            if _path_key("pak", relative) not in seen
        ]

        def inspect_pak(relative: str):
            try:
                data = pak_loader(relative)
                if not data:
                    return None, None
                return make_mdf_source_choice(data=data, path=relative, mmtr_resource_path=mmtr_resource_path,
                                              origin="pak", relative_path=relative, label=relative), None
            except (OSError, ValueError) as exc:
                return None, (relative, str(exc))

        worker_count = min(6, max(1, len(candidates)))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="reasy-mdf-search",
        ) as executor:
            futures = {
                executor.submit(inspect_pak, relative): relative
                for relative in candidates
            }
            for future in as_completed(futures):
                report(futures[future])
                choice, failure = future.result()
                if failure is not None:
                    unread.append(failure)
                add(choice)

    rank = {"project": 0, "pak": 1, "system": 2, "disk": 3}
    choices.sort(
        key=lambda item: (
            rank.get(item.origin, 9),
            item.suggested_output_path.casefold(),
        )
    )
    return MdfConsumers(tuple(choices), tuple(unread))


__all__ = [
    "MdfConsumers",
    "MdfVariantSourceChoice",
    "discover_mdf_consumers",
    "make_mdf_source_choice",
    "matching_material_indices",
]
