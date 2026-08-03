from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from ui.ai.file_migration import FileMigrationJob, FileMigrationStrategy
from ui.ai.tool_registry import AssistantToolError, translate_tool_text as _tr


_DETAIL_LIMIT = 100


@dataclass
class PakFolderItem:
    source: Path
    relative: Path
    latest_path: str | None = None
    output_relative: Path | None = None
    candidates: tuple[str, ...] = ()
    issue: str = ""


@dataclass(frozen=True)
class PakFolderDiscovery:
    mod_root: Path
    files: tuple[Path, ...]
    items: tuple[PakFolderItem, ...]
    format_items: tuple[PakFolderItem, ...]

    @property
    def matched_items(self) -> tuple[PakFolderItem, ...]:
        return tuple(item for item in self.format_items if item.latest_path)


def _input_folder(value: Any) -> Path:
    raw = str(value or "").strip()
    path = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    if not raw or not path.is_dir() or path.parent == path:
        raise AssistantToolError(
            _tr(
                "mod_folder must identify an existing non-root folder: {path}",
                path=path,
            )
        )
    return path


def _output_folder(mod_root: Path, value: Any) -> Path:
    raw = str(value or "").strip()
    output = (
        Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
        if raw
        else mod_root.with_name(f"{mod_root.name}_updated")
    )
    if not raw:
        base = output
        number = 2
        while output.exists():
            output = base.with_name(f"{base.name}_{number}")
            number += 1
    if (
        output == mod_root
        or mod_root in output.parents
        or output in mod_root.parents
    ):
        raise AssistantToolError(
            _tr("output_folder must be separate from mod_folder.")
        )
    try:
        if output.exists() and (
            not output.is_dir() or next(output.iterdir(), None) is not None
        ):
            raise AssistantToolError(
                _tr("output_folder must be new or empty: {path}", path=output)
            )
    except AssistantToolError:
        raise
    except OSError as exc:
        raise AssistantToolError(
            _tr(
                "Could not inspect output_folder {path}: {error}",
                path=output,
                error=exc,
            )
        ) from exc
    return output


def _scan_folder_steps(root: Path):
    files: list[Path] = []

    def raise_scan_error(error: OSError):
        raise error

    try:
        for directory, folders, filenames in os.walk(
            root,
            onerror=raise_scan_error,
        ):
            folders.sort(key=str.casefold)
            filenames.sort(key=str.casefold)
            base = Path(directory)
            files.extend(base / name for name in filenames)
            yield
    except OSError as exc:
        raise AssistantToolError(
            _tr("Could not scan folder {path}: {error}", path=root, error=exc)
        ) from exc
    return sorted(
        files,
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )


def _format_pattern(suffixes: Iterable[str]) -> re.Pattern[str]:
    names = tuple(
        sorted(
            {
                str(suffix or "").strip().lstrip(".").casefold()
                for suffix in suffixes
                if str(suffix or "").strip().lstrip(".")
            }
        )
    )
    if not names:
        raise ValueError("At least one format suffix is required.")
    return re.compile(
        rf"\.(?P<format>{'|'.join(map(re.escape, names))})"
        rf"(?:\.(?P<version>\d+))?$",
        re.IGNORECASE,
    )


def _normalized_pak_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/").strip("/")
    parts = PurePosixPath(path).parts
    if not path or not parts or any(part in {"", ".", ".."} for part in parts):
        return ""
    return "/".join(parts)


def _logical_key(value: str, pattern: re.Pattern[str]) -> str:
    normalized = _normalized_pak_path(value)
    return pattern.sub(
        lambda match: f".{match.group('format').casefold()}",
        normalized,
    ).casefold()


def _preferred_versions(
    paths: Iterable[str],
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    grouped: dict[str, list[str]] = {}
    for path in paths:
        grouped.setdefault(_logical_key(path, pattern), []).append(path)
    preferred: list[str] = []
    for values in grouped.values():
        highest = max(
            int(match.group("version"))
            if (match := pattern.search(path)) and match.group("version")
            else -1
            for path in values
        )
        choices = sorted(
            {
                path
                for path in values
                if (
                    int(match.group("version"))
                    if (match := pattern.search(path))
                    and match.group("version")
                    else -1
                )
                == highest
            },
            key=str.casefold,
        )
        preferred.extend(choices[:1])
    return tuple(sorted(preferred, key=str.casefold))


def _source_keys(relative: Path, pattern: re.Pattern[str]) -> tuple[str, ...]:
    parts = relative.parts
    values = [relative.as_posix()]
    natives = next(
        (
            index
            for index, part in enumerate(parts)
            if part.casefold() == "natives"
        ),
        None,
    )
    if natives is not None:
        values.insert(0, "/".join(parts[natives:]))
    keys = []
    for value in values:
        key = _logical_key(value, pattern)
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def _match_latest_paths(
    relative: Path,
    latest_by_key: dict[str, list[str]],
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    keys = _source_keys(relative, pattern)
    for key in keys:
        if paths := latest_by_key.get(key):
            return _preferred_versions(paths, pattern)

    candidates: list[str] = []
    for source_key in keys:
        suffix = f"/{source_key}"
        for latest_key, paths in latest_by_key.items():
            if latest_key.endswith(suffix):
                candidates.extend(paths)
        if candidates:
            break
    return _preferred_versions(candidates, pattern)


def _output_relative(relative: Path, latest_path: str) -> Path:
    source_parts = relative.parts
    latest_parts = PurePosixPath(latest_path).parts
    source_natives = next(
        (
            index
            for index, part in enumerate(source_parts)
            if part.casefold() == "natives"
        ),
        None,
    )
    latest_natives = next(
        (
            index
            for index, part in enumerate(latest_parts)
            if part.casefold() == "natives"
        ),
        None,
    )
    if source_natives is not None and latest_natives is not None:
        return Path(
            *source_parts[:source_natives],
            *latest_parts[latest_natives:],
        )
    return relative.with_name(latest_parts[-1])


def _create_staging(output: Path) -> Path:
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        return Path(
            tempfile.mkdtemp(
                prefix=f".{output.name}.reasy-",
                dir=output.parent,
            )
        )
    except OSError as exc:
        raise AssistantToolError(
            _tr(
                "Could not create output_folder {path}: {error}",
                path=output,
                error=exc,
            )
        ) from exc


def _publish(staging: Path, output: Path) -> None:
    removed_empty_output = False
    try:
        if output.exists():
            if not output.is_dir() or next(output.iterdir(), None) is not None:
                raise OSError(
                    _tr("output_folder is no longer empty: {path}", path=output)
                )
            output.rmdir()
            removed_empty_output = True
        staging.replace(output)
    except OSError as exc:
        if removed_empty_output and not output.exists():
            try:
                output.mkdir()
            except OSError:
                pass
        raise AssistantToolError(
            _tr(
                "Could not finalize output_folder {path}: {error}",
                path=output,
                error=exc,
            )
        ) from exc


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def discover_mod_folder_pak_files_steps(
    *,
    mod_folder: str,
    pak_paths: Iterable[str],
    suffixes: Iterable[str],
    format_name: str,
):
    """Discover and version-match supported files without reading PAK data."""

    mod_root = _input_folder(mod_folder)
    pattern = _format_pattern(suffixes)
    files = yield from _scan_folder_steps(mod_root)
    if not files:
        raise AssistantToolError(_tr("The mod folder contains no files."))

    latest_by_key: dict[str, list[str]] = {}
    for value in pak_paths:
        path = _normalized_pak_path(value)
        if path and pattern.search(path):
            latest_by_key.setdefault(_logical_key(path, pattern), []).append(path)
    if not latest_by_key:
        raise AssistantToolError(
            _tr(
                "The loaded PAK path index contains no {format} files.",
                format=format_name.upper(),
            )
        )

    items: list[PakFolderItem] = []
    format_items: list[PakFolderItem] = []
    for source in files:
        relative = source.relative_to(mod_root)
        item = PakFolderItem(source, relative, output_relative=relative)
        items.append(item)
        if not pattern.search(relative.name):
            continue
        format_items.append(item)
        candidates = _match_latest_paths(relative, latest_by_key, pattern)
        item.candidates = candidates
        if len(candidates) == 1:
            item.latest_path = candidates[0]
            item.output_relative = _output_relative(relative, candidates[0])
        elif candidates:
            item.issue = "ambiguous"
        else:
            item.issue = "unmatched"

    if not format_items:
        raise AssistantToolError(
            _tr(
                "The mod folder contains no {format} files.",
                format=format_name.upper(),
            )
        )

    targets: dict[str, list[PakFolderItem]] = {}
    for item in items:
        target = item.output_relative or item.relative
        targets.setdefault(target.as_posix().casefold(), []).append(item)
    for collisions in targets.values():
        if len(collisions) < 2:
            continue
        for item in collisions:
            if item.latest_path:
                item.issue = "output_collision"
                item.latest_path = None
                item.output_relative = item.relative

    discovery = PakFolderDiscovery(
        mod_root=mod_root,
        files=tuple(files),
        items=tuple(items),
        format_items=tuple(format_items),
    )
    if not discovery.matched_items:
        raise AssistantToolError(
            _tr(
                "None of the mod folder's {count} {format} files matched an unambiguous path in the loaded game PAKs.",
                count=len(format_items),
                format=format_name.upper(),
            )
        )
    return discovery


def update_mod_folder_from_paks_steps(
    *,
    mod_folder: str,
    output_folder: str,
    pak_paths: Iterable[str],
    read_pak_file: Callable[[str], bytes | None],
    strategy: FileMigrationStrategy,
    suffixes: Iterable[str],
    expected_pairs: Iterable[tuple[str, str]] | None = None,
    report_sink: Callable[[dict[str, Any]], None] | None = None,
):
    """Update matching mod files from loaded PAK bases as one atomic folder."""

    discovery = yield from discover_mod_folder_pak_files_steps(
        mod_folder=mod_folder,
        pak_paths=pak_paths,
        suffixes=suffixes,
        format_name=strategy.format_name,
    )
    mod_root = discovery.mod_root
    files = list(discovery.files)
    items = list(discovery.items)
    format_items = list(discovery.format_items)
    matched = list(discovery.matched_items)
    actual_pairs = {
        (item.relative.as_posix().casefold(), str(item.latest_path).casefold())
        for item in matched
    }
    if expected_pairs is not None and actual_pairs != {
        (str(relative).casefold(), str(latest).casefold())
        for relative, latest in expected_pairs
    }:
        raise AssistantToolError(
            _tr(
                "The mod folder or loaded PAK paths changed after AI analysis. Analyze the update again before writing files."
            )
        )
    output = _output_folder(mod_root, output_folder)

    staging = _create_staging(output)
    format_item_ids = {id(item) for item in format_items}
    copied = updated = changes = skipped = 0
    source_only = destination_only = incompatible = 0
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    unmatched: list[str] = []
    ambiguous: list[dict[str, Any]] = []

    def copy_source(item: PakFolderItem) -> None:
        nonlocal copied
        _copy_file(item.source, staging / item.relative)
        copied += 1

    try:
        with tempfile.TemporaryDirectory(prefix="reasy-pak-latest-") as temporary:
            latest_root = Path(temporary)
            for index, item in enumerate(items):
                relative_text = item.relative.as_posix()
                yield {
                    "stage": "updating_mod_folder",
                    "current": index + 1,
                    "completed": index,
                    "total": len(items),
                    "item": relative_text,
                }
                if id(item) not in format_item_ids:
                    try:
                        copy_source(item)
                    except OSError as exc:
                        failures.append({"file": relative_text, "error": str(exc)})
                    continue
                if not item.latest_path:
                    try:
                        copy_source(item)
                    except OSError as exc:
                        failures.append({"file": relative_text, "error": str(exc)})
                    if item.issue == "unmatched":
                        unmatched.append(relative_text)
                    else:
                        ambiguous.append(
                            {
                                "file": relative_text,
                                "reason": item.issue,
                                "candidates": list(item.candidates[:10]),
                            }
                        )
                    continue

                output_relative = item.output_relative or item.relative
                destination = staging / output_relative
                try:
                    latest_data = read_pak_file(item.latest_path)
                    if latest_data is None:
                        raise AssistantToolError(
                            _tr(
                                "The latest file could not be read from the loaded PAKs: {path}",
                                path=item.latest_path,
                            )
                        )
                    latest_file = latest_root / str(index) / PurePosixPath(
                        item.latest_path
                    ).name
                    latest_file.parent.mkdir(parents=True, exist_ok=True)
                    latest_file.write_bytes(latest_data)
                    job = FileMigrationJob(
                        item.source,
                        latest_file,
                        destination,
                        relative_text,
                    )
                    strategy.validate_paths(job)
                    data, report = strategy.migrate(job)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    try:
                        shutil.copystat(item.source, destination)
                    except OSError:
                        pass
                    updated += 1
                    changes += int(report.get("changes_applied", 0))
                    skipped += int(report.get("skipped_change_count", 0))
                    source_only += int(report.get("source_only_value_count", 0))
                    destination_only += int(
                        report.get("destination_only_value_count", 0)
                    )
                    incompatible += int(report.get("incompatible_value_count", 0))
                    has_warnings = bool(
                        report.get("source_only_value_count", 0)
                        or report.get("incompatible_value_count", 0)
                    )
                    results.append(
                        {
                            "file": relative_text,
                            "latest_pak_path": item.latest_path,
                            "output": output_relative.as_posix(),
                            "status": "partial" if has_warnings else "updated",
                            "changes_applied": int(
                                report.get("changes_applied", 0)
                            ),
                            "skipped_change_count": int(
                                report.get("skipped_change_count", 0)
                            ),
                            "source_only_value_count": int(
                                report.get("source_only_value_count", 0)
                            ),
                            "destination_only_value_count": int(
                                report.get("destination_only_value_count", 0)
                            ),
                            "incompatible_value_count": int(
                                report.get("incompatible_value_count", 0)
                            ),
                        }
                    )
                    if report_sink is not None:
                        report_sink(
                            {
                                "file": relative_text,
                                "status": (
                                    "partial" if has_warnings else "updated"
                                ),
                                "outdated_file": str(item.source),
                                "latest_pak_path": item.latest_path,
                                "output": output_relative.as_posix(),
                                "output_file": str(output / output_relative),
                                "report": report,
                            }
                        )
                except Exception as exc:
                    try:
                        if destination.exists():
                            destination.unlink()
                        copy_source(item)
                    except OSError as copy_exc:
                        failures.append(
                            {
                                "file": relative_text,
                                "latest_pak_path": item.latest_path,
                                "error": f"{exc}; fallback copy failed: {copy_exc}",
                            }
                        )
                    else:
                        failures.append(
                            {
                                "file": relative_text,
                                "latest_pak_path": item.latest_path,
                                "error": str(exc),
                            }
                        )

        _publish(staging, output)
        issue_count = len(unmatched) + len(ambiguous) + len(failures)
        warning_count = source_only + incompatible
        details = (results, failures, unmatched, ambiguous)
        return {
            "format": strategy.format_name,
            "status": (
                "completed"
                if not issue_count and not warning_count
                else "partial"
                if updated
                else "failed"
            ),
            "mod_folder": str(mod_root),
            "output_folder": str(output),
            "latest_source": "loaded_game_paks",
            "files_scanned": len(files),
            "format_files_found": len(format_items),
            "jobs_processed": len(matched),
            "files_modified": updated,
            "files_copied": copied,
            "changes_applied": changes,
            "skipped_change_count": skipped,
            "source_only_value_count": source_only,
            "destination_only_value_count": destination_only,
            "incompatible_value_count": incompatible,
            "jobs": results[:_DETAIL_LIMIT],
            "failures": failures[:_DETAIL_LIMIT],
            "failure_count": len(failures),
            "unmatched_files": unmatched[:_DETAIL_LIMIT],
            "unmatched_file_count": len(unmatched),
            "ambiguous_matches": ambiguous[:_DETAIL_LIMIT],
            "ambiguous_match_count": len(ambiguous),
            "report_details_truncated": any(
                len(values) > _DETAIL_LIMIT
                for values in (failures, unmatched, ambiguous)
            ),
            "details_truncated": any(
                len(values) > _DETAIL_LIMIT for values in details
            ),
            "source_folder_untouched": True,
            "pak_files_untouched": True,
            "complete": not issue_count and not warning_count,
        }
    finally:
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError:
                pass
