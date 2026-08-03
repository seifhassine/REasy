from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from ui.ai.file_migration import FileMigrationJob
from ui.ai.pak_folder_migration import (
    PakFolderDiscovery,
    discover_mod_folder_pak_files_steps,
)
from ui.ai.rsz_migration import RszMigrationStrategy
from ui.ai.tool_registry import AssistantToolError, translate_tool_text as _tr


_INSTANCE_ID_RE = re.compile(r"(?P<label>instance|userdata)\[\d+\]")
_GROUP_PAGE_LIMIT = 100


@dataclass(frozen=True)
class RszAnalyzedFile:
    relative_path: str
    source_file: Path
    latest_pak_path: str
    output_relative_path: str
    source_hash: str
    latest_hash: str
    report: dict[str, Any]


@dataclass(frozen=True)
class RszChangeGroup:
    group_id: str
    path_pattern: str
    references: tuple[tuple[str, str], ...]
    samples: tuple[dict[str, Any], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "path_pattern": self.path_pattern,
            "change_count": len(self.references),
            "file_count": len({file for file, _path in self.references}),
            "samples": list(self.samples),
        }


@dataclass(frozen=True)
class RszUpdateAnalysis:
    analysis_id: str
    project: str
    game: str
    mod_folder: str
    outdated_registry: str
    latest_registry: str
    include_source_only: bool
    discovery: PakFolderDiscovery
    files: tuple[RszAnalyzedFile, ...]
    groups: tuple[RszChangeGroup, ...]
    failures: tuple[dict[str, Any], ...]

    @property
    def expected_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (item.relative.as_posix(), str(item.latest_path))
            for item in self.discovery.matched_items
        )

    @property
    def expected_hashes(self) -> dict[str, tuple[str, str]]:
        return {
            item.relative_path: (item.source_hash, item.latest_hash)
            for item in self.files
        }

    @property
    def unresolved_value_count(self) -> int:
        return sum(
            int(item.report.get("source_only_value_count", 0))
            + int(item.report.get("incompatible_value_count", 0))
            for item in self.files
        )

    @property
    def unresolved_file_count(self) -> int:
        return sum(
            1 for item in self.discovery.format_items if not item.latest_path
        ) + len(self.failures)

    def group_page(
        self,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        needle = str(query or "").strip().casefold()
        groups = [
            group
            for group in self.groups
            if not needle
            or needle in group.group_id.casefold()
            or needle in group.path_pattern.casefold()
        ]
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), _GROUP_PAGE_LIMIT))
        page = groups[offset : offset + limit]
        return {
            "analysis_id": self.analysis_id,
            "project": self.project,
            "game": self.game,
            "query": query,
            "offset": offset,
            "limit": limit,
            "total_groups": len(groups),
            "groups": [group.payload() for group in page],
            "next_offset": offset + len(page)
            if offset + len(page) < len(groups)
            else None,
        }

    def issue_page(
        self,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        for item in self.files:
            issues.extend(
                {
                    "kind": "source_only",
                    "file": item.relative_path,
                    "path": path,
                    "meaning": (
                        "Present in the outdated mod but not safely mapped to "
                        "the latest schema."
                    ),
                }
                for path in item.report.get("source_only_values", [])
            )
            issues.extend(
                {
                    "kind": "incompatible",
                    "file": item.relative_path,
                    **value,
                }
                for value in item.report.get("incompatible_values", [])
                if isinstance(value, dict)
            )
        issues.extend(
            {
                "kind": item.issue or "unmatched",
                "file": item.relative.as_posix(),
                "candidates": list(item.candidates[:10]),
            }
            for item in self.discovery.format_items
            if not item.latest_path
        )
        issues.extend(
            {"kind": "analysis_failure", **failure}
            for failure in self.failures
        )
        needle = str(query or "").strip().casefold()
        if needle:
            issues = [
                issue
                for issue in issues
                if needle
                in " ".join(str(value) for value in issue.values()).casefold()
            ]
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), _GROUP_PAGE_LIMIT))
        page = issues[offset : offset + limit]
        return {
            "analysis_id": self.analysis_id,
            "query": query,
            "offset": offset,
            "limit": limit,
            "total_issues": len(issues),
            "issues": page,
            "next_offset": offset + len(page)
            if offset + len(page) < len(issues)
            else None,
        }

    def payload(self) -> dict[str, Any]:
        unmatched = [
            item.relative.as_posix()
            for item in self.discovery.format_items
            if item.issue == "unmatched"
        ]
        ambiguous = [
            {
                "file": item.relative.as_posix(),
                "reason": item.issue,
                "candidates": list(item.candidates[:10]),
            }
            for item in self.discovery.format_items
            if item.issue and item.issue != "unmatched"
        ]
        files = [
            {
                "file": item.relative_path,
                "latest_pak_path": item.latest_pak_path,
                "proposed_change_count": int(
                    item.report.get("changes_applied", 0)
                ),
                "source_only_value_count": int(
                    item.report.get("source_only_value_count", 0)
                ),
                "destination_only_value_count": int(
                    item.report.get("destination_only_value_count", 0)
                ),
                "incompatible_value_count": int(
                    item.report.get("incompatible_value_count", 0)
                ),
            }
            for item in self.files
        ]
        page = self.group_page()
        issues = self.issue_page()
        return {
            "status": "analysis_complete" if not self.failures else "partial",
            "analysis_id": self.analysis_id,
            "project": self.project,
            "game": self.game,
            "mod_folder": self.mod_folder,
            "outdated_registry": self.outdated_registry,
            "latest_registry": self.latest_registry,
            "comparison_mode": "two_way_ai_intent_review",
            "old_original_available": False,
            "accuracy_note": (
                "Without old-original files, mod intent is inferred from the "
                "user's request, semantic paths, repeated patterns, and sampled "
                "old-mod/latest-game values; it is not a provable three-way diff."
            ),
            "files_scanned": len(self.discovery.files),
            "rsz_files_found": len(self.discovery.format_items),
            "rsz_pairs_analyzed": len(self.files),
            "change_group_count": len(self.groups),
            "proposed_change_count": sum(
                int(item.report.get("changes_applied", 0)) for item in self.files
            ),
            "unresolved_file_count": self.unresolved_file_count,
            "unresolved_value_count": self.unresolved_value_count,
            "files": files[:_GROUP_PAGE_LIMIT],
            "groups": page["groups"],
            "next_group_offset": page["next_offset"],
            "issues": issues["issues"],
            "next_issue_offset": issues["next_offset"],
            "unmatched_files": unmatched[:_GROUP_PAGE_LIMIT],
            "ambiguous_matches": ambiguous[:_GROUP_PAGE_LIMIT],
            "failures": list(self.failures[:_GROUP_PAGE_LIMIT]),
            "ai_review_required": True,
            "write_performed": False,
            "source_folder_untouched": True,
            "details_truncated": bool(
                len(files) > _GROUP_PAGE_LIMIT
                or len(unmatched) > _GROUP_PAGE_LIMIT
                or len(ambiguous) > _GROUP_PAGE_LIMIT
                or len(self.failures) > _GROUP_PAGE_LIMIT
                or page["next_offset"] is not None
                or issues["next_offset"] is not None
            ),
        }


def _change_groups(files: Iterable[RszAnalyzedFile]) -> tuple[RszChangeGroup, ...]:
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for item in files:
        for change in item.report.get("changes", []):
            path = str(change.get("path") or "")
            if not path:
                continue
            pattern = _INSTANCE_ID_RE.sub(
                lambda match: f"{match.group('label')}[]",
                path,
            )
            grouped.setdefault(pattern, []).append((item.relative_path, change))

    result = []
    for index, pattern in enumerate(sorted(grouped, key=str.casefold), 1):
        changes = grouped[pattern]
        references = tuple(
            (file, str(change.get("path") or "")) for file, change in changes
        )
        samples = tuple(
            {
                "file": file,
                "path": change.get("path"),
                "kind": change.get("kind"),
                "latest_game_value": change.get("old"),
                "outdated_mod_value": change.get("new"),
            }
            for file, change in changes[:5]
        )
        result.append(
            RszChangeGroup(
                group_id=f"g{index:04d}",
                path_pattern=pattern,
                references=references,
                samples=samples,
            )
        )
    return tuple(result)


def analyze_rsz_mod_folder_steps(
    *,
    analysis_id: str,
    project: str,
    game: str,
    mod_folder: str,
    pak_paths: Iterable[str],
    read_pak_file: Callable[[str], bytes | None],
    strategy: RszMigrationStrategy,
    suffixes: Iterable[str],
):
    discovery = yield from discover_mod_folder_pak_files_steps(
        mod_folder=mod_folder,
        pak_paths=pak_paths,
        suffixes=suffixes,
        format_name="rsz",
    )
    analyzed: list[RszAnalyzedFile] = []
    failures: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="reasy-rsz-analysis-") as temporary:
        temporary_root = Path(temporary)
        for index, item in enumerate(discovery.matched_items):
            relative = item.relative.as_posix()
            yield {
                "stage": "analyzing_rsz_update",
                "current": index + 1,
                "completed": index,
                "total": len(discovery.matched_items),
                "item": relative,
            }
            try:
                source_data = item.source.read_bytes()
                latest_data = read_pak_file(str(item.latest_path))
                if latest_data is None:
                    raise AssistantToolError(
                        _tr(
                            "The latest file could not be read from the loaded PAKs: {path}",
                            path=item.latest_path,
                        )
                    )
                work = temporary_root / str(index)
                source_file = work / "source" / item.source.name
                latest_file = work / "latest" / PurePosixPath(
                    str(item.latest_path)
                ).name
                output_file = work / "output" / PurePosixPath(
                    str(item.latest_path)
                ).name
                source_file.parent.mkdir(parents=True, exist_ok=True)
                latest_file.parent.mkdir(parents=True, exist_ok=True)
                source_file.write_bytes(source_data)
                latest_file.write_bytes(latest_data)
                job = FileMigrationJob(
                    source_file,
                    latest_file,
                    output_file,
                    relative,
                )
                strategy.validate_paths(job)
                _output, report = strategy.migrate(job)
                analyzed.append(
                    RszAnalyzedFile(
                        relative_path=relative,
                        source_file=item.source,
                        latest_pak_path=str(item.latest_path),
                        output_relative_path=(
                            item.output_relative or item.relative
                        ).as_posix(),
                        source_hash=sha256(source_data).hexdigest(),
                        latest_hash=sha256(latest_data).hexdigest(),
                        report=report,
                    )
                )
            except Exception as exc:
                failures.append(
                    {
                        "file": relative,
                        "latest_pak_path": item.latest_path,
                        "error": str(exc),
                    }
                )

    if not analyzed:
        raise AssistantToolError(
            _tr("No RSZ file pair could be analyzed for this mod folder.")
        )
    return RszUpdateAnalysis(
        analysis_id=analysis_id,
        project=project,
        game=game,
        mod_folder=str(discovery.mod_root),
        outdated_registry=str(strategy.outdated_registry_path),
        latest_registry=str(strategy.latest_registry_path),
        include_source_only=bool(strategy.include_source_only),
        discovery=discovery,
        files=tuple(analyzed),
        groups=_change_groups(analyzed),
        failures=tuple(failures),
    )
