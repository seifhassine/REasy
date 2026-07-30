from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AiToolOutcome:
    status: str
    fields: tuple[tuple[str, str], ...]
    metrics: tuple[tuple[str, str], ...]
    details: tuple[str, ...]
    details_truncated: bool = False


_PARTIAL_STATUSES = {
    "partial",
    "conflicts_only",
    "no_matching_values",
    "destination_only_values_preserved",
}
_FAILED_STATUSES = {"failed", "aborted"}
_NO_CHANGE_STATUSES = {"already_up_to_date", "no_changes"}
_DETAIL_KEYS = (
    "results",
    "jobs",
    "failures",
    "applied_changes",
    "changes",
    "updated_mdf_files",
    "source_only_values",
    "destination_only_values_preserved",
    "incompatible_values",
    "selection_misses",
)


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, (list, tuple)):
        return len(value)
    return 0


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _detail_text(value: Any) -> str:
    if not isinstance(value, dict):
        return _text(value)
    target = (
        value.get("destination_file")
        or value.get("file")
        or value.get("requested_tab")
        or value.get("label")
        or value.get("path")
        or value.get("target")
        or ""
    )
    action = (
        value.get("tool")
        or value.get("operation")
        or value.get("status")
        or ""
    )
    error = value.get("error") or ""
    parts = [str(part) for part in (target, action, error) if part]
    return " — ".join(parts) or _text(value)


def summarize_tool_result(
    result: Any,
    *,
    unsaved_by_default: bool = False,
    detail_limit: int = 24,
) -> AiToolOutcome | None:
    """Normalize heterogeneous tool payloads for deterministic UI cards."""

    if not isinstance(result, dict):
        return None

    fields = []
    for key, source_key in (
        ("source", "source_file"),
        ("destination", "destination_file"),
        ("output", "output_folder"),
    ):
        value = _text(result.get(source_key))
        if value:
            fields.append((key, value))
    target = result.get("target")
    if isinstance(target, dict):
        target_text = _text(target.get("file") or target.get("tab_id"))
        if target_text and not any(
            value == target_text for _key, value in fields
        ):
            fields.append(("target", target_text))

    requested = _count(result.get("requested_count"))
    applied = _count(result.get("applied_count"))
    jobs = _count(result.get("jobs_processed"))
    files = max(
        _count(result.get("files_modified")),
        _count(result.get("mdf_files_updated")),
        _count(result.get("saved_count")),
    )
    changes = _count(result.get("changes_applied"))
    planned = _count(result.get("changes_planned"))
    failures = max(
        _count(result.get("failed_count")),
        _count(result.get("failures")),
    )
    conflicts = _count(result.get("conflicts"))
    warnings = sum(
        _count(result.get(key))
        for key in (
            "unsupported_changes",
            "source_only_value_count",
            "destination_only_value_count",
            "incompatible_value_count",
        )
    ) + _count(result.get("selection_misses"))

    metrics = []
    if requested:
        metrics.append(("actions", f"{applied}/{requested}"))
    if jobs:
        metrics.append(("jobs", str(jobs)))
    if files:
        metrics.append(("files", str(files)))
    if changes or planned:
        metrics.append(
            (
                "changes",
                f"{changes}/{planned}" if planned else str(changes),
            )
        )
    if failures:
        metrics.append(("failures", str(failures)))
    if conflicts:
        metrics.append(("conflicts", str(conflicts)))
    if warnings:
        metrics.append(("warnings", str(warnings)))

    raw_status = str(result.get("status") or "").casefold()
    cancelled = bool(result.get("cancelled"))
    saved = result.get("saved")
    if isinstance(saved, str) and saved:
        fields.append(("saved_file", saved))
        metrics.append(("state", "saved"))
    elif isinstance(saved, bool):
        if saved:
            metrics.append(("state", "saved"))
        elif (
            not cancelled
            and raw_status not in _NO_CHANGE_STATUSES
            and result.get("modified") is not False
        ):
            metrics.append(("state", "unsaved"))
    elif (
        unsaved_by_default
        and not cancelled
        and raw_status not in _NO_CHANGE_STATUSES
        and result.get("modified") is not False
    ):
        metrics.append(("state", "unsaved"))

    if cancelled:
        status = "cancelled"
    elif (
        raw_status in _FAILED_STATUSES
        or result.get("aborted") is True
        or result.get("success") is False
    ):
        status = (
            "partial"
            if any((applied, files, changes))
            else "failed"
        )
    elif (
        raw_status in _PARTIAL_STATUSES
        or result.get("complete") is False
        or failures
        or conflicts
        or warnings
    ):
        status = "partial"
    elif (
        raw_status in _NO_CHANGE_STATUSES
        or (
            isinstance(result.get("jobs"), (list, tuple))
            and bool(result["jobs"])
            and all(
                isinstance(job, dict)
                and str(job.get("status") or "").casefold()
                in _NO_CHANGE_STATUSES
                for job in result["jobs"]
            )
        )
    ):
        status = "no_changes"
    else:
        status = "completed"

    details = []
    details_truncated = bool(result.get("details_truncated"))
    for key in _DETAIL_KEYS:
        values = result.get(key)
        if not isinstance(values, (list, tuple)):
            continue
        for value in values:
            text = _detail_text(value)
            if text:
                details.append(text)
            if len(details) >= detail_limit:
                details_truncated = True
                break
        if len(details) >= detail_limit:
            break

    return AiToolOutcome(
        status=status,
        fields=tuple(fields),
        metrics=tuple(metrics),
        details=tuple(details),
        details_truncated=details_truncated,
    )
