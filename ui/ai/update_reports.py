from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


_PAGE_LIMIT = 200


def _text_matches(item: Mapping[str, Any], query: str) -> bool:
    if not query:
        return True
    return query.casefold() in json.dumps(
        item,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
    ).casefold()


def _path_detail(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {"path": str(value)}


class UpdateReportCollector:
    """Collect full per-file migration reports without expanding tool results."""

    def __init__(self):
        self.files: list[dict[str, Any]] = []

    def __call__(self, item: dict[str, Any]) -> None:
        self.files.append(copy.deepcopy(item))


@dataclass(frozen=True)
class FileUpdateReport:
    update_id: str
    created_at: str
    format: str
    operation: str
    project: str
    game: str
    source: str
    output: str
    result: dict[str, Any]
    files: tuple[dict[str, Any], ...]
    decision_lookup: Mapping[tuple[str, str], dict[str, Any]]

    @classmethod
    def create(
        cls,
        *,
        format_name: str,
        operation: str,
        project: str,
        game: str,
        source: str,
        output: str,
        result: Mapping[str, Any],
        files: Iterable[dict[str, Any]],
        decision_lookup: Mapping[
            tuple[str, str], Mapping[str, Any]
        ] | None = None,
    ) -> "FileUpdateReport":
        decisions = {
            (str(file).casefold(), str(path)): copy.deepcopy(dict(decision))
            for (file, path), decision in (decision_lookup or {}).items()
        }
        return cls(
            update_id=uuid.uuid4().hex[:12],
            created_at=datetime.now(timezone.utc).isoformat(),
            format=str(format_name or "").casefold(),
            operation=str(operation or ""),
            project=str(project or ""),
            game=str(game or ""),
            source=str(source or ""),
            output=str(output or ""),
            result=copy.deepcopy(dict(result)),
            files=tuple(copy.deepcopy(tuple(files))),
            decision_lookup=decisions,
        )

    def _common(self, file: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "file": str(file.get("file") or file.get("label") or ""),
            "latest_pak_path": str(file.get("latest_pak_path") or ""),
            "latest_file": str(file.get("latest_file") or ""),
            "output": str(
                file.get("output_file") or file.get("output") or ""
            ),
            "output_relative": str(file.get("output") or ""),
        }

    def _decision(self, file: str, path: str) -> dict[str, Any] | None:
        decision = self.decision_lookup.get((file.casefold(), path))
        return copy.deepcopy(decision) if decision is not None else None

    def _imported(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for file in self.files:
            common = self._common(file)
            report = file.get("report") or {}
            for raw in report.get("changes", []):
                if not isinstance(raw, dict):
                    continue
                path = str(raw.get("path") or "")
                kind = str(raw.get("kind") or "changed")
                item = {
                    **common,
                    "path": path,
                    "kind": kind,
                    "origin": "outdated_mod",
                    "status": "imported",
                }
                if "old" in raw:
                    item["latest_value"] = copy.deepcopy(raw["old"])
                if "new" in raw:
                    item["imported_value"] = copy.deepcopy(raw["new"])
                decision = self._decision(common["file"], path)
                if decision is not None:
                    item["ai_decision"] = decision
                items.append(item)
        return items

    def _new_elements(self) -> list[dict[str, Any]]:
        items = [
            {**item, "element_source": "outdated_mod"}
            for item in self._imported()
            if item.get("kind") == "added"
        ]
        for file in self.files:
            common = self._common(file)
            report = file.get("report") or {}
            details = report.get("destination_only_details")
            if not isinstance(details, list):
                details = report.get("destination_only_values_preserved", [])
            for raw in details:
                detail = _path_detail(raw)
                items.append(
                    {
                        **common,
                        **detail,
                        "kind": "latest_only",
                        "element_source": "latest_pak",
                        "origin": "latest_pak",
                        "status": "preserved_from_latest",
                    }
                )
        return items

    def _kept_latest(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for file in self.files:
            common = self._common(file)
            report = file.get("report") or {}
            for raw in report.get("skipped_changes", []):
                if not isinstance(raw, dict):
                    continue
                path = str(raw.get("path") or "")
                item = {
                    **common,
                    "path": path,
                    "kind": str(raw.get("kind") or "changed"),
                    "status": "kept_latest",
                }
                if "old" in raw:
                    item["latest_value"] = copy.deepcopy(raw["old"])
                if "new" in raw:
                    item["outdated_mod_value"] = copy.deepcopy(raw["new"])
                decision = self._decision(common["file"], path)
                if decision is not None:
                    item["ai_decision"] = decision
                items.append(item)
        return items

    def _unresolved(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for file in self.files:
            common = self._common(file)
            report = file.get("report") or {}
            details = report.get("source_only_details")
            if not isinstance(details, list):
                details = report.get("source_only_values", [])
            for raw in details:
                items.append(
                    {
                        **common,
                        **_path_detail(raw),
                        "kind": "source_only",
                        "status": "not_imported",
                    }
                )
            for raw in report.get("incompatible_values", []):
                detail = (
                    copy.deepcopy(raw)
                    if isinstance(raw, dict)
                    else {"path": str(raw)}
                )
                items.append(
                    {
                        **common,
                        **detail,
                        "kind": "incompatible",
                        "status": "not_imported",
                    }
                )

        for key, kind in (
            ("failures", "failure"),
            ("unmatched_files", "unmatched"),
            ("ambiguous_matches", "ambiguous"),
        ):
            for raw in self.result.get(key, []):
                detail = (
                    copy.deepcopy(raw)
                    if isinstance(raw, dict)
                    else {"file": str(raw)}
                )
                items.append(
                    {**detail, "kind": kind, "status": "not_imported"}
                )
        return items

    def _file_items(self) -> list[dict[str, Any]]:
        items = []
        for file in self.files:
            report = file.get("report") or {}
            items.append(
                {
                    **self._common(file),
                    "status": file.get("status", "updated"),
                    "changes_imported": int(report.get("changes_applied", 0)),
                    "new_elements_imported": int(
                        report.get(
                            "added_element_count",
                            sum(
                                1
                                for item in report.get("changes", [])
                                if isinstance(item, dict)
                                and item.get("kind") == "added"
                            ),
                        )
                    ),
                    "latest_only_values_preserved": int(
                        report.get("destination_only_value_count", 0)
                    ),
                    "candidate_changes_kept_latest": int(
                        report.get("skipped_change_count", 0)
                    ),
                    "unresolved_values": int(
                        report.get("source_only_value_count", 0)
                    )
                    + int(report.get("incompatible_value_count", 0)),
                    "details_truncated": bool(
                        report.get(
                            "value_details_truncated",
                            report.get("details_truncated", False),
                        )
                    ),
                }
            )
        return items

    def _section_items(self, section: str) -> list[dict[str, Any]]:
        if section == "imported":
            return self._imported()
        if section == "new_elements":
            return self._new_elements()
        if section == "kept_latest":
            return self._kept_latest()
        if section == "unresolved":
            return self._unresolved()
        if section == "files":
            return self._file_items()
        raise ValueError(f"Unknown update-report section: {section}")

    def _reported_section_counts(self) -> dict[str, int]:
        imported = sum(
            int((file.get("report") or {}).get("changes_applied", 0))
            for file in self.files
        )
        added = sum(
            int(
                (file.get("report") or {}).get(
                    "added_element_count",
                    sum(
                        1
                        for item in (file.get("report") or {}).get(
                            "changes", []
                        )
                        if isinstance(item, dict)
                        and item.get("kind") == "added"
                    ),
                )
            )
            for file in self.files
        )
        latest_only = sum(
            int(
                (file.get("report") or {}).get(
                    "destination_only_value_count", 0
                )
            )
            for file in self.files
        )
        kept_latest = sum(
            int((file.get("report") or {}).get("skipped_change_count", 0))
            for file in self.files
        )
        unresolved = sum(
            int(
                (file.get("report") or {}).get(
                    "source_only_value_count", 0
                )
            )
            + int(
                (file.get("report") or {}).get(
                    "incompatible_value_count", 0
                )
            )
            for file in self.files
        ) + sum(
            int(self.result.get(count_key, len(self.result.get(list_key, []))))
            for list_key, count_key in (
                ("failures", "failure_count"),
                ("unmatched_files", "unmatched_file_count"),
                ("ambiguous_matches", "ambiguous_match_count"),
            )
        )
        return {
            "imported": imported,
            "new_elements": added + latest_only,
            "kept_latest": kept_latest,
            "unresolved": unresolved,
            "files": len(self.files),
        }

    def _available_section_counts(self) -> dict[str, int]:
        imported = added = latest_only = kept_latest = unresolved = 0
        for file in self.files:
            report = file.get("report") or {}
            changes = [
                item
                for item in report.get("changes", [])
                if isinstance(item, dict)
            ]
            imported += len(changes)
            added += sum(item.get("kind") == "added" for item in changes)
            destination_details = report.get("destination_only_details")
            if not isinstance(destination_details, list):
                destination_details = report.get(
                    "destination_only_values_preserved", []
                )
            latest_only += len(destination_details)
            kept_latest += sum(
                isinstance(item, dict)
                for item in report.get("skipped_changes", [])
            )
            source_details = report.get("source_only_details")
            if not isinstance(source_details, list):
                source_details = report.get("source_only_values", [])
            unresolved += len(source_details) + len(
                report.get("incompatible_values", [])
            )
        unresolved += sum(
            len(self.result.get(key, []))
            for key in ("failures", "unmatched_files", "ambiguous_matches")
        )
        return {
            "imported": imported,
            "new_elements": added + latest_only,
            "kept_latest": kept_latest,
            "unresolved": unresolved,
            "files": len(self.files),
        }

    def summary(self) -> dict[str, Any]:
        counts = self._reported_section_counts()
        available = self._available_section_counts()
        truncated = bool(
            self.result.get("report_details_truncated")
            or any(
                bool(
                    (file.get("report") or {}).get(
                        "value_details_truncated",
                        (file.get("report") or {}).get(
                            "details_truncated"
                        ),
                    )
                )
                for file in self.files
            )
            or any(available[key] < count for key, count in counts.items())
        )
        summary = {
            "update_id": self.update_id,
            "created_at": self.created_at,
            "format": self.format,
            "operation": self.operation,
            "project": self.project,
            "game": self.game,
            "source": self.source,
            "output": self.output,
            "status": self.result.get("status", "completed"),
            "files_updated": len(self.files),
            "section_counts": counts,
            "available_detail_counts": available,
            "details_complete": not truncated,
            "available_sections": [
                "summary",
                "imported",
                "new_elements",
                "kept_latest",
                "unresolved",
                "files",
            ],
        }
        first_report = (
            (self.files[0].get("report") or {}) if self.files else {}
        )
        for key in (
            "analysis_id",
            "latest_source",
            "comparison_mode",
            "old_original_available",
            "accuracy_note",
            "outdated_registry",
            "latest_registry",
            "source_folder_untouched",
            "source_files_untouched",
            "pak_files_untouched",
            "latest_files_untouched",
        ):
            value = self.result.get(key, first_report.get(key))
            if value not in (None, ""):
                summary[key] = copy.deepcopy(value)
        return summary

    def inspect(
        self,
        *,
        section: str,
        file_filter: str = "",
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if section == "summary":
            return self.summary()
        items = self._section_items(section)
        file_needle = str(file_filter or "").strip().casefold()
        if file_needle:
            items = [
                item
                for item in items
                if file_needle
                in " ".join(
                    str(item.get(key) or "")
                    for key in ("file", "latest_pak_path", "latest_file", "output")
                ).casefold()
            ]
        needle = str(query or "").strip()
        if needle:
            items = [item for item in items if _text_matches(item, needle)]
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), _PAGE_LIMIT))
        page = items[offset : offset + limit]
        reported_total = self._reported_section_counts()[section]
        filters_applied = bool(file_needle or needle)
        return {
            "update_id": self.update_id,
            "format": self.format,
            "operation": self.operation,
            "section": section,
            "file_filter": file_filter,
            "query": query,
            "offset": offset,
            "limit": limit,
            "total": len(items),
            "reported_total": reported_total,
            "reported_total_scope": "whole_section_before_filters",
            "filters_applied": filters_applied,
            "items": page,
            "next_offset": (
                offset + len(page)
                if offset + len(page) < len(items)
                else None
            ),
            "details_complete": self.summary()["details_complete"],
        }
