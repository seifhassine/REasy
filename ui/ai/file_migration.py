from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from ui.ai.tool_registry import AssistantToolError, translate_tool_text as _tr


MAX_MIGRATION_JOBS = 256


def migration_job_schema(format_name: str) -> dict[str, Any]:
    label = format_name.upper()
    return {
        "type": "object",
        "properties": {
            "outdated_file": {
                "type": "string",
                "description": f"Existing outdated {label} whose values should be carried over.",
            },
            "latest_file": {
                "type": "string",
                "description": f"Existing latest {label}; its structure and format version remain the output base.",
            },
            "output_file": {
                "type": "string",
                "description": (
                    f"Output {label} path. It must differ from both inputs; an "
                    "existing file is atomically replaced after confirmation."
                ),
            },
            "label": {
                "type": "string",
                "description": "Optional short label for this batch item.",
            },
        },
        "required": ["outdated_file", "latest_file", "output_file"],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class FileMigrationJob:
    outdated_file: Path
    latest_file: Path
    output_file: Path
    label: str


class FileMigrationStrategy(Protocol):
    format_name: str

    def validate_paths(self, job: FileMigrationJob) -> None: ...

    def migrate(self, job: FileMigrationJob) -> tuple[bytes, dict[str, Any]]: ...


def _input_path(value: Any, field: str) -> Path:
    raw = str(value or "").strip()
    path = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    if not raw or not path.is_file():
        raise AssistantToolError(
            _tr(
                "{field} must identify an existing file: {path}", field=field, path=path
            )
        )
    return path


def _output_path(value: Any, field: str) -> Path:
    raw = str(value or "").strip()
    path = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    if not raw or path.parent == path or path.is_dir():
        raise AssistantToolError(
            _tr("{field} must identify an output file: {path}", field=field, path=path)
        )
    return path


def prepare_migration_jobs(
    jobs: list[dict[str, Any]],
    strategy: FileMigrationStrategy,
) -> list[FileMigrationJob]:
    if not isinstance(jobs, list) or not jobs:
        raise AssistantToolError(_tr("jobs must be a non-empty array."))
    if len(jobs) > MAX_MIGRATION_JOBS:
        raise AssistantToolError(
            _tr(
                "A file migration accepts at most {count} jobs.",
                count=MAX_MIGRATION_JOBS,
            )
        )

    prepared: list[FileMigrationJob] = []
    allowed = {"outdated_file", "latest_file", "output_file", "label"}
    required = {"outdated_file", "latest_file", "output_file"}
    for index, item in enumerate(jobs):
        if (
            not isinstance(item, dict)
            or not required.issubset(item)
            or set(item).difference(allowed)
        ):
            raise AssistantToolError(
                _tr("Migration job {index} is invalid.", index=index + 1)
            )
        outdated = _input_path(item["outdated_file"], f"jobs[{index}].outdated_file")
        latest = _input_path(item["latest_file"], f"jobs[{index}].latest_file")
        output = _output_path(item["output_file"], f"jobs[{index}].output_file")
        if outdated == latest:
            raise AssistantToolError(
                _tr(
                    "Migration job {index} must use different outdated and latest files.",
                    index=index + 1,
                )
            )
        if output in {outdated, latest}:
            raise AssistantToolError(
                _tr(
                    "Migration job {index} must write to a separate output file.",
                    index=index + 1,
                )
            )
        job = FileMigrationJob(
            outdated,
            latest,
            output,
            str(item.get("label") or output.name).strip() or output.name,
        )
        strategy.validate_paths(job)
        prepared.append(job)

    outputs = [job.output_file for job in prepared]
    if len(set(outputs)) != len(outputs):
        raise AssistantToolError(
            _tr("Every migration job must use a distinct output_file.")
        )
    inputs = {path for job in prepared for path in (job.outdated_file, job.latest_file)}
    collisions = inputs.intersection(outputs)
    if collisions:
        raise AssistantToolError(
            _tr(
                "A migration output cannot overwrite another job's input: {path}",
                path=sorted(collisions, key=str)[0],
            )
        )
    return prepared


def _stage_output(job: FileMigrationJob, data: bytes) -> Path:
    temporary: Path | None = None
    try:
        job.output_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{job.output_file.name}.reasy-",
            dir=job.output_file.parent,
        )
        temporary = Path(raw_temporary)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except OSError as exc:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
        raise AssistantToolError(
            _tr(
                "Could not stage output file {path}: {error}",
                path=job.output_file,
                error=exc,
            )
        ) from exc


def _publish_output(job: FileMigrationJob, staged: Path) -> None:
    try:
        os.replace(staged, job.output_file)
        try:
            shutil.copystat(job.latest_file, job.output_file)
        except OSError:
            pass
    except OSError as exc:
        raise AssistantToolError(
            _tr(
                "Could not finalize output file {path}: {error}",
                path=job.output_file,
                error=exc,
            )
        ) from exc


def migrate_file_jobs_steps(
    jobs: list[dict[str, Any]],
    strategy: FileMigrationStrategy,
    *,
    report_sink: Callable[[dict[str, Any]], None] | None = None,
):
    prepared = prepare_migration_jobs(jobs, strategy)
    staged: list[tuple[FileMigrationJob, Path, dict[str, Any]]] = []
    failures: list[dict[str, Any]] = []
    try:
        for index, job in enumerate(prepared):
            yield {
                "stage": "migrating_files",
                "current": index + 1,
                "completed": index,
                "total": len(prepared),
                "item": job.label,
            }
            try:
                output, report = strategy.migrate(job)
                temporary = _stage_output(job, output)
                staged.append((job, temporary, report))
            except Exception as exc:
                failures.append(
                    {
                        "label": job.label,
                        "outdated_file": str(job.outdated_file),
                        "latest_file": str(job.latest_file),
                        "output_file": str(job.output_file),
                        "error": str(exc),
                    }
                )

        published: list[dict[str, Any]] = []
        for index, (job, temporary, report) in enumerate(staged):
            yield {
                "stage": "writing_migrated_files",
                "current": index + 1,
                "completed": index,
                "total": len(staged),
                "item": job.label,
            }
            try:
                _publish_output(job, temporary)
                has_warnings = bool(
                    report.get("source_only_value_count", 0)
                    or report.get("incompatible_value_count", 0)
                )
                status = (
                    "partial"
                    if has_warnings
                    else "updated"
                    if report.get("changes_applied", 0)
                    else "copied_latest_without_changes"
                )
                published.append(
                    {
                        "label": job.label,
                        "format": strategy.format_name,
                        "status": status,
                        "outdated_file": str(job.outdated_file),
                        "latest_file": str(job.latest_file),
                        "output_file": str(job.output_file),
                        **report,
                    }
                )
                if report_sink is not None:
                    report_sink(
                        {
                            "file": job.label,
                            "label": job.label,
                            "status": status,
                            "outdated_file": str(job.outdated_file),
                            "latest_file": str(job.latest_file),
                            "output_file": str(job.output_file),
                            "report": report,
                        }
                    )
            except Exception as exc:
                failures.append(
                    {
                        "label": job.label,
                        "outdated_file": str(job.outdated_file),
                        "latest_file": str(job.latest_file),
                        "output_file": str(job.output_file),
                        "error": str(exc),
                    }
                )

        detail_keys = (
            "changes_applied",
            "source_only_value_count",
            "destination_only_value_count",
            "incompatible_value_count",
        )
        totals = {
            key: sum(int(item.get(key, 0)) for item in published) for key in detail_keys
        }
        has_value_warnings = bool(
            totals["source_only_value_count"] or totals["incompatible_value_count"]
        )
        return {
            "format": strategy.format_name,
            "status": "completed"
            if not failures and not has_value_warnings
            else "partial"
            if published
            else "failed",
            "jobs_processed": len(prepared),
            "files_written": len(published),
            "jobs": published,
            "failures": failures,
            "source_files_untouched": True,
            "latest_files_untouched": True,
            "complete": not failures and not has_value_warnings,
            **totals,
        }
    finally:
        for _job, temporary, _report in staged:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
