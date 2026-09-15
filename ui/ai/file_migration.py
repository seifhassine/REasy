from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from services.file_io import FileFingerprint, copy_file_atomically, file_fingerprint
from ui.ai.tool_registry import AssistantToolError, translate_tool_text as _tr


MAX_MIGRATION_JOBS = 256
MIGRATION_PUBLISH_MODE_VALUES = (
    "separate",
    "replace_outdated_with_backup",
    "replace_latest_with_backup",
)
MIGRATION_PUBLISH_MODES = frozenset(MIGRATION_PUBLISH_MODE_VALUES)


def migration_publish_schema_properties() -> dict[str, Any]:
    return {
        "publish_mode": {
            "type": "string",
            "enum": list(MIGRATION_PUBLISH_MODE_VALUES),
            "description": (
                "Default separate. Replacement modes stage every output, retain "
                "backups, and roll the batch back on failure. When replacing the "
                "outdated input, output_file may name a new latest-version file "
                "beside outdated_file."
            ),
        },
        "backup_folder": {
            "type": "string",
            "description": (
                "Optional absolute backup parent for replacement mode; otherwise "
                "backups are retained beside the replaced files."
            ),
        },
    }


def migration_confirmation_details(
    arguments: dict[str, Any],
    *,
    extra_lines: tuple[str, ...] = (),
) -> str:
    jobs = arguments.get("jobs")
    jobs = jobs if isinstance(jobs, list) else []
    mode = str(arguments.get("publish_mode") or "separate")
    target_field = {
        "replace_outdated_with_backup": "outdated_file",
        "replace_latest_with_backup": "latest_file",
    }.get(mode, "output_file")
    targets = [
        str(job[target_field])
        for job in jobs[:5]
        if isinstance(job, dict) and job.get(target_field)
    ]
    lines = [
        _tr("Jobs: {count}", count=len(jobs)),
        _tr("Publish mode: {mode}", mode=mode),
        *extra_lines,
    ]
    if targets:
        lines.append(_tr("Publish targets: {paths}", paths=", ".join(targets)))
    replacement_outputs = [
        str(job["output_file"])
        for job in jobs[:5]
        if mode != "separate"
        and isinstance(job, dict)
        and job.get("output_file")
    ]
    if replacement_outputs:
        lines.append(
            _tr(
                "Final output files: {paths}",
                paths=", ".join(replacement_outputs),
            )
        )
    if arguments.get("backup_folder"):
        lines.append(
            _tr("Backup folder: {path}", path=arguments["backup_folder"])
        )
    if len(jobs) > 5:
        lines.append(_tr("Additional jobs: {count}", count=len(jobs) - 5))
    return "\n".join(line for line in lines if line)


def migration_job_schema(
    format_name: str,
    *,
    allow_protected_replace: bool = False,
) -> dict[str, Any]:
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
                    f"Output {label} path. Required for separate publishing and "
                    "must differ from both inputs; omit it when using a protected "
                    "replacement publish mode."
                    if allow_protected_replace
                    else f"Output {label} path. It must differ from both inputs; an "
                    "existing file is atomically replaced after confirmation."
                ),
            },
            "label": {
                "type": "string",
                "description": "Optional short label for this batch item.",
            },
        },
        "required": [
            "outdated_file",
            "latest_file",
            *([] if allow_protected_replace else ["output_file"]),
        ],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class FileMigrationJob:
    outdated_file: Path
    latest_file: Path
    output_file: Path
    label: str
    replacement_file: Path | None = None


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


def _publish_mode(value: Any) -> str:
    mode = str(value or "separate").strip().casefold()
    if mode not in MIGRATION_PUBLISH_MODES:
        raise AssistantToolError(
            _tr("Unsupported migration publish_mode: {mode}", mode=mode)
        )
    return mode


def _migration_input_fingerprint(path: Path) -> FileFingerprint:
    try:
        return file_fingerprint(path)
    except OSError as exc:
        raise AssistantToolError(
            _tr("Could not inspect migration input {path}: {error}", path=path, error=exc)
        ) from exc


def _assert_inputs_unchanged(
    prepared: list[FileMigrationJob],
    fingerprints: dict[Path, FileFingerprint],
) -> None:
    for job in prepared:
        for path in (job.outdated_file, job.latest_file):
            if _migration_input_fingerprint(path) != fingerprints[path]:
                raise AssistantToolError(
                    _tr(
                        "Migration input changed before replacement: {path}",
                        path=path,
                    )
                )


def prepare_migration_jobs(
    jobs: list[dict[str, Any]],
    strategy: FileMigrationStrategy,
    *,
    publish_mode: str = "separate",
) -> list[FileMigrationJob]:
    publish_mode = _publish_mode(publish_mode)
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
    required = {"outdated_file", "latest_file"}
    if publish_mode == "separate":
        required.add("output_file")
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
        replacement_file = (
            outdated
            if publish_mode == "replace_outdated_with_backup"
            else latest
            if publish_mode == "replace_latest_with_backup"
            else None
        )
        if replacement_file is None:
            output = _output_path(
                item["output_file"],
                f"jobs[{index}].output_file",
            )
        elif "output_file" in item and str(item.get("output_file") or "").strip():
            requested_output = _output_path(
                item["output_file"],
                f"jobs[{index}].output_file",
            )
            if (
                publish_mode == "replace_latest_with_backup"
                and requested_output != replacement_file
            ):
                raise AssistantToolError(
                    _tr(
                        "Migration job {index} output_file must be omitted or match latest_file when replacing the latest input.",
                        index=index + 1,
                    )
                )
            if (
                publish_mode == "replace_outdated_with_backup"
                and requested_output != replacement_file
                and (
                    requested_output.parent != outdated.parent
                    or requested_output.exists()
                )
            ):
                raise AssistantToolError(
                    _tr(
                        "Migration job {index} replacement output must be a new file beside outdated_file.",
                        index=index + 1,
                    )
                )
            output = requested_output
        else:
            output = replacement_file
        if outdated == latest:
            raise AssistantToolError(
                _tr(
                    "Migration job {index} must use different outdated and latest files.",
                    index=index + 1,
                )
            )
        if publish_mode == "separate" and output in {outdated, latest}:
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
            replacement_file,
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
    if publish_mode == "separate" and collisions:
        raise AssistantToolError(
            _tr(
                "A migration output cannot overwrite another job's input: {path}",
                path=sorted(collisions, key=str)[0],
            )
        )
    if publish_mode != "separate":
        replacement_inputs = [
            path
            for job in prepared
            for path in (job.outdated_file, job.latest_file)
        ]
        if len(set(replacement_inputs)) != len(replacement_inputs):
            raise AssistantToolError(
                _tr("Protected replacement jobs must use distinct input files.")
            )
        for job in prepared:
            other_inputs = inputs.difference({job.replacement_file})
            if job.output_file in other_inputs:
                raise AssistantToolError(
                    _tr(
                        "A replacement output cannot overwrite another migration input: {path}",
                        path=job.output_file,
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


def _publish_output(
    job: FileMigrationJob,
    staged: Path,
    *,
    no_overwrite: bool = False,
) -> None:
    reserved = False
    try:
        if no_overwrite:
            descriptor = os.open(
                job.output_file,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.close(descriptor)
            reserved = True
        os.replace(staged, job.output_file)
        reserved = False
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
    finally:
        if reserved:
            job.output_file.unlink(missing_ok=True)


def _backup_paths(
    prepared: list[FileMigrationJob],
    backup_folder: str,
) -> tuple[list[Path], Path | None]:
    tag = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    raw = str(backup_folder or "").strip()
    run_root = None
    if raw:
        candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not candidate.is_absolute():
            raise AssistantToolError(_tr("backup_folder must be an absolute path."))
        parent = candidate.resolve()
        if parent.exists() and not parent.is_dir():
            raise AssistantToolError(
                _tr("backup_folder must identify a directory: {path}", path=parent)
            )
        run_root = parent / f"reasy-migration-backup-{tag}"
        if run_root.exists():
            raise AssistantToolError(_tr("A migration backup folder already exists."))
        backups = [
            run_root
            / f"{index + 1:04d}_{(job.replacement_file or job.output_file).name}"
            for index, job in enumerate(prepared)
        ]
    else:
        backups = [
            (job.replacement_file or job.output_file).with_name(
                f"{(job.replacement_file or job.output_file).name}.reasy-backup-{tag}"
            )
            for job in prepared
        ]
    if any(path.exists() for path in backups):
        raise AssistantToolError(_tr("A migration backup path already exists."))
    return backups, run_root


def _create_backup(source: Path, destination: Path) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_file_atomically(source, destination)
    except OSError as exc:
        raise AssistantToolError(
            _tr(
                "Could not create migration backup {path}: {error}",
                path=destination,
                error=exc,
            )
        ) from exc


def _restore_backup(backup: Path, destination: Path) -> None:
    try:
        copy_file_atomically(backup, destination, overwrite=True)
    except OSError as exc:
        raise AssistantToolError(
            _tr(
                "Could not restore migration backup for {path}: {error}",
                path=destination,
                error=exc,
            )
        ) from exc


def _migration_result_status(report: dict[str, Any]) -> str:
    has_warnings = bool(
        report.get("source_only_value_count", 0)
        or report.get("incompatible_value_count", 0)
    )
    return (
        "partial"
        if has_warnings
        else "updated"
        if report.get("changes_applied", 0)
        else "copied_latest_without_changes"
    )


def _published_job(
    job: FileMigrationJob,
    report: dict[str, Any],
    *,
    format_name: str,
    backup: Path | None,
) -> dict[str, Any]:
    return {
        "label": job.label,
        "format": format_name,
        "status": _migration_result_status(report),
        "outdated_file": str(job.outdated_file),
        "latest_file": str(job.latest_file),
        "output_file": str(job.output_file),
        "backup_file": str(backup) if backup is not None else None,
        **report,
    }


def migrate_file_jobs_steps(
    jobs: list[dict[str, Any]],
    strategy: FileMigrationStrategy,
    *,
    report_sink: Callable[[dict[str, Any]], None] | None = None,
    publish_mode: str = "separate",
    backup_folder: str = "",
):
    publish_mode = _publish_mode(publish_mode)
    prepared = prepare_migration_jobs(
        jobs,
        strategy,
        publish_mode=publish_mode,
    )
    replacement_mode = publish_mode != "separate"
    backups, backup_root = (
        _backup_paths(prepared, backup_folder)
        if replacement_mode
        else ([], None)
    )
    input_fingerprints = (
        {
            path: _migration_input_fingerprint(path)
            for job in prepared
            for path in (job.outdated_file, job.latest_file)
        }
        if replacement_mode
        else {}
    )
    staged: list[tuple[FileMigrationJob, Path, dict[str, Any]]] = []
    failures: list[dict[str, Any]] = []
    published: list[dict[str, Any]] = []
    created_backups: list[Path] = []
    rollback_performed = False
    transaction_committed = False
    preserve_failed_backups = False
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

        if replacement_mode and not failures:
            replaced: list[tuple[FileMigrationJob, Path]] = []

            def rollback_replacements() -> list[str]:
                rollback_errors = []
                for replaced_job, replaced_backup in reversed(replaced):
                    try:
                        _restore_backup(
                            replaced_backup,
                            replaced_job.replacement_file
                            or replaced_job.output_file,
                        )
                        if (
                            replaced_job.replacement_file is not None
                            and replaced_job.output_file
                            != replaced_job.replacement_file
                        ):
                            replaced_job.output_file.unlink(missing_ok=True)
                    except Exception as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                return rollback_errors

            try:
                _assert_inputs_unchanged(prepared, input_fingerprints)
                for index, ((job, _temporary, _report), backup) in enumerate(
                    zip(staged, backups)
                ):
                    yield {
                        "stage": "backing_up_migration_inputs",
                        "current": index + 1,
                        "completed": index,
                        "total": len(staged),
                        "item": job.label,
                    }
                    _create_backup(
                        job.replacement_file or job.output_file,
                        backup,
                    )
                    created_backups.append(backup)

                _assert_inputs_unchanged(prepared, input_fingerprints)

                for index, ((job, temporary, report), backup) in enumerate(
                    zip(staged, backups)
                ):
                    yield {
                        "stage": "replacing_migration_inputs",
                        "current": index + 1,
                        "completed": index,
                        "total": len(staged),
                        "item": job.label,
                    }
                    _publish_output(
                        job,
                        temporary,
                        no_overwrite=(
                            job.replacement_file is not None
                            and job.output_file != job.replacement_file
                        ),
                    )
                    replaced.append((job, backup))
                    if (
                        job.replacement_file is not None
                        and job.output_file != job.replacement_file
                    ):
                        job.replacement_file.unlink()
                    published.append(
                        _published_job(
                            job,
                            report,
                            format_name=strategy.format_name,
                            backup=backup,
                        )
                    )
                transaction_committed = True
            except BaseException as exc:
                replaced_outputs = {job.output_file for job, _backup in replaced}
                for (job, temporary, _report), backup in zip(staged, backups):
                    if (
                        job.output_file not in replaced_outputs
                        and not temporary.exists()
                    ):
                        replaced.append((job, backup))
                rollback_errors = rollback_replacements()
                rollback_performed = bool(replaced)
                preserve_failed_backups = bool(rollback_errors)
                published.clear()
                if not isinstance(exc, Exception):
                    raise
                failures.append(
                    {
                        "label": "migration transaction",
                        "error": (
                            str(exc)
                            if not rollback_errors
                            else f"{exc}; rollback failures: {'; '.join(rollback_errors)}"
                        ),
                    }
                )
        elif not replacement_mode:
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
                    published.append(
                        _published_job(
                            job,
                            report,
                            format_name=strategy.format_name,
                            backup=None,
                        )
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

        if report_sink is not None:
            report_by_output = {
                str(job.output_file): report
                for job, _temporary, report in staged
            }
            for item in published:
                report_sink(
                    {
                        "file": item["label"],
                        "label": item["label"],
                        "status": item["status"],
                        "outdated_file": item["outdated_file"],
                        "latest_file": item["latest_file"],
                        "output_file": item["output_file"],
                        "backup_file": item["backup_file"],
                        "report": report_by_output[item["output_file"]],
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
            "publish_mode": publish_mode,
            "status": "completed"
            if not failures and not has_value_warnings
            else "partial"
            if published
            else "failed",
            "jobs_processed": len(prepared),
            "files_written": len(published),
            "jobs": published,
            "failures": failures,
            "source_files_untouched": not (
                published and publish_mode == "replace_outdated_with_backup"
            ),
            "latest_files_untouched": not (
                published and publish_mode == "replace_latest_with_backup"
            ),
            "backup_files": [
                str(path)
                for path in (
                    created_backups
                    if transaction_committed or preserve_failed_backups
                    else []
                )
            ],
            "backup_count": (
                len(created_backups)
                if transaction_committed or preserve_failed_backups
                else 0
            ),
            "backup_folder": (
                str(backup_root)
                if backup_root is not None
                and (transaction_committed or preserve_failed_backups)
                else None
            ),
            "rollback_performed": rollback_performed,
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
        if (
            publish_mode != "separate"
            and not transaction_committed
            and not preserve_failed_backups
        ):
            for backup in created_backups:
                backup.unlink(missing_ok=True)
            if backup_root is not None:
                try:
                    backup_root.rmdir()
                except OSError:
                    pass
