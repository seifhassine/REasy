"""Constrained, recoverable file operations within one user-authorized folder."""

from __future__ import annotations

import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QFile

from services.file_io import (
    FileFingerprint,
    copy_file_atomically,
    file_fingerprint,
    reserve_new_file,
)

MAX_FILE_OPERATIONS = 256
FILE_OPERATION_PLAN_TTL_SECONDS = 10 * 60
_PROTECTED_PARTS = frozenset({".git", ".reasy_project.json"})


class FileOperationError(ValueError):
    """A safe file operation could not be planned or completed."""


@dataclass(frozen=True)
class PlannedFileOperation:
    operation: str
    source: Path
    source_relative: str
    fingerprint: FileFingerprint
    destination: Path | None = None
    destination_relative: str | None = None

    def payload(self) -> dict[str, Any]:
        result = {
            "operation": self.operation,
            "source": self.source_relative,
            "size": self.fingerprint.size,
        }
        if self.destination_relative is not None:
            result["destination"] = self.destination_relative
        return result


@dataclass(frozen=True)
class FileOperationPlan:
    plan_id: str
    root: Path
    operations: tuple[PlannedFileOperation, ...]
    created_at: float

    @property
    def contains_delete(self) -> bool:
        return any(item.operation == "delete" for item in self.operations)

    @property
    def total_size(self) -> int:
        return sum(
            item.fingerprint.size
            for item in {
                operation.source: operation for operation in self.operations
            }.values()
        )

    def payload(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "root": str(self.root),
            "operation_count": len(self.operations),
            "total_size": self.total_size,
            "contains_delete": self.contains_delete,
            "operations": [item.payload() for item in self.operations],
        }


@dataclass
class _RollbackRecord:
    operation: str
    current: Path
    original: Path | None
    fingerprint: FileFingerprint | None


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        if os.name == "nt":
            try:
                attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
            except FileNotFoundError:
                return False
            return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return False
    except OSError:
        return True


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _suffix_signature(path: Path) -> tuple[str, ...]:
    return tuple(suffix.casefold() for suffix in path.suffixes)


def _is_reserved_name(name: str) -> bool:
    checker = getattr(os.path, "isreserved", None)
    if callable(checker):
        return bool(checker(name))
    return Path(name).is_reserved()


class FolderFileOperations:
    """Plan and apply file-only operations beneath one canonical root."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = self.resolve_root(root)

    @staticmethod
    def resolve_root(root: str | os.PathLike[str]) -> Path:
        raw = str(root or "").strip()
        candidate = Path(raw)
        if not raw or not candidate.is_absolute():
            raise FileOperationError("Folder access requires an absolute path.")
        if any(part == ".." for part in candidate.parts):
            raise FileOperationError("The authorized folder cannot contain traversal.")
        if any(part.casefold() in _PROTECTED_PARTS for part in candidate.parts):
            raise FileOperationError("Protected application metadata cannot be authorized.")
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current /= part
            if _is_link_or_junction(current):
                raise FileOperationError(
                    "The authorized folder cannot contain links or junctions."
                )
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise FileOperationError(f"Folder does not exist: {candidate}") from exc
        if not resolved.is_dir() or resolved.parent == resolved:
            raise FileOperationError(
                "The authorized path must be an existing non-root folder."
            )
        return resolved

    @staticmethod
    def _relative_path(
        value: Any,
        field: str,
        *,
        allow_empty: bool = False,
    ) -> Path:
        raw = str(value or "").strip()
        if not raw and allow_empty:
            return Path()
        path = Path(raw)
        if (
            not raw
            or path == Path()
            or path.is_absolute()
            or bool(path.drive)
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise FileOperationError(
                f"{field} must be a root-relative path without traversal."
            )
        for part in path.parts:
            if (
                part.casefold() in _PROTECTED_PARTS
                or "\x00" in part
                or (os.name == "nt" and (":" in part or part.endswith((" ", "."))))
            ):
                raise FileOperationError(f"{field} contains a protected path component.")
        return path

    @staticmethod
    def _filename(value: Any, field: str = "new_name") -> str:
        relative = FolderFileOperations._relative_path(value, field)
        if len(relative.parts) != 1:
            raise FileOperationError(f"{field} must contain a file name only.")
        if _is_reserved_name(relative.name):
            raise FileOperationError(f"{field} is reserved by the operating system.")
        return relative.name

    def _assert_no_links(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise FileOperationError("Path escapes the authorized folder.") from exc
        current = self.root
        for part in relative.parts:
            current /= part
            if _is_link_or_junction(current):
                raise FileOperationError(
                    f"Links and junctions are not mutable: {relative.as_posix()}"
                )

    def _existing_member(
        self,
        value: Any,
        field: str,
        *,
        allow_root: bool = False,
    ) -> Path:
        relative = self._relative_path(value, field, allow_empty=allow_root)
        candidate = self.root / relative
        self._assert_no_links(candidate)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise FileOperationError(f"{field} does not exist: {relative.as_posix()}") from exc
        if not resolved.is_relative_to(self.root) or (resolved == self.root and not allow_root):
            raise FileOperationError(f"{field} escapes the authorized folder.")
        return resolved

    def _destination(self, directory: Any, name: str) -> Path:
        parent = self._existing_member(
            directory,
            "destination_directory",
            allow_root=True,
        )
        if not parent.is_dir():
            raise FileOperationError("destination_directory is not a directory.")
        destination = parent / self._filename(name)
        self._assert_no_links(destination)
        if destination.exists() or destination.is_symlink():
            raise FileOperationError(
                f"Destination already exists: {destination.relative_to(self.root).as_posix()}"
            )
        return destination

    def list_entries(
        self,
        path: str = "",
        *,
        recursive: bool = False,
        max_depth: int = 1,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not isinstance(recursive, bool):
            raise FileOperationError("recursive must be true or false.")
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not 1 <= max_depth <= 32:
            raise FileOperationError("max_depth must be between 1 and 32.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 2000:
            raise FileOperationError("limit must be between 1 and 2000.")
        start = self._existing_member(path, "path", allow_root=True)
        if not start.is_dir():
            raise FileOperationError("path must identify a directory.")

        entries: list[dict[str, Any]] = []
        pending = [(start, 0)]
        truncated = False
        while pending:
            directory, depth = pending.pop()
            try:
                children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except OSError as exc:
                raise FileOperationError(f"Could not inspect {directory}: {exc}") from exc
            nested: list[Path] = []
            for child in children:
                child_path = Path(child.path)
                relative = child_path.relative_to(self.root).as_posix()
                linked = child.is_symlink() or _is_link_or_junction(child_path)
                protected = child.name.casefold() in _PROTECTED_PARTS
                try:
                    is_directory = child.is_dir(follow_symlinks=False)
                    is_file = child.is_file(follow_symlinks=False)
                    stat = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise FileOperationError(f"Could not inspect {relative}: {exc}") from exc
                kind = (
                    "blocked_link"
                    if linked
                    else "protected"
                    if protected
                    else "directory"
                    if is_directory
                    else "file"
                    if is_file
                    else "other"
                )
                if len(entries) >= limit:
                    truncated = True
                    pending.clear()
                    break
                entries.append(
                    {
                        "path": relative,
                        "name": child.name,
                        "kind": kind,
                        "size": int(stat.st_size) if is_file else None,
                        "modified_ns": int(stat.st_mtime_ns),
                        "mutable": kind == "file",
                    }
                )
                if (
                    recursive
                    and is_directory
                    and not linked
                    and not protected
                    and depth + 1 < max_depth
                ):
                    nested.append(child_path)
            if not truncated:
                pending.extend((child, depth + 1) for child in reversed(nested))
        return {
            "root": str(self.root),
            "path": start.relative_to(self.root).as_posix() if start != self.root else "",
            "entries": entries,
            "count": len(entries),
            "truncated": truncated,
        }

    def plan(self, operations: list[dict[str, Any]]) -> FileOperationPlan:
        if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_FILE_OPERATIONS:
            raise FileOperationError(
                f"operations must contain between 1 and {MAX_FILE_OPERATIONS} items."
            )

        planned: list[PlannedFileOperation] = []
        source_operations: dict[str, list[str]] = {}
        destinations: set[str] = set()
        for index, raw in enumerate(operations):
            if not isinstance(raw, dict):
                raise FileOperationError(f"operations[{index}] must be an object.")
            operation = str(raw.get("operation") or "").strip().casefold()
            allowed = {
                "copy": {"operation", "source", "destination_directory", "new_name", "allow_extension_change"},
                "move": {"operation", "source", "destination_directory", "new_name", "allow_extension_change"},
                "rename": {"operation", "source", "new_name", "allow_extension_change"},
                "delete": {"operation", "source"},
            }
            if operation not in allowed or set(raw).difference(allowed[operation]):
                raise FileOperationError(f"operations[{index}] is invalid.")
            if "source" not in raw:
                raise FileOperationError(f"operations[{index}].source is required.")
            if (
                "allow_extension_change" in raw
                and not isinstance(raw["allow_extension_change"], bool)
            ):
                raise FileOperationError(
                    f"operations[{index}].allow_extension_change must be true or false."
                )
            source = self._existing_member(raw["source"], f"operations[{index}].source")
            if not source.is_file():
                raise FileOperationError("Only files can be copied, moved, renamed, or deleted.")
            source_key = _path_key(source)
            source_operations.setdefault(source_key, []).append(operation)

            destination = None
            destination_relative = None
            if operation in {"copy", "move", "rename"}:
                if operation == "rename":
                    if "new_name" not in raw:
                        raise FileOperationError(f"operations[{index}].new_name is required.")
                    destination_directory = source.parent.relative_to(self.root).as_posix()
                else:
                    if "destination_directory" not in raw:
                        raise FileOperationError(
                            f"operations[{index}].destination_directory is required."
                        )
                    destination_directory = raw["destination_directory"]
                new_name = self._filename(raw.get("new_name") or source.name)
                if (
                    _suffix_signature(Path(new_name)) != _suffix_signature(source)
                    and raw.get("allow_extension_change") is not True
                ):
                    raise FileOperationError(
                        "Changing a file extension requires allow_extension_change=true."
                    )
                destination = self._destination(destination_directory, new_name)
                destination_relative = destination.relative_to(self.root).as_posix()
                destination_key = _path_key(destination)
                if destination_key == source_key:
                    raise FileOperationError("Source and destination are the same file.")
                if destination_key in destinations:
                    raise FileOperationError("Every destination in a plan must be unique.")
                destinations.add(destination_key)

            planned.append(
                PlannedFileOperation(
                    operation=operation,
                    source=source,
                    source_relative=source.relative_to(self.root).as_posix(),
                    fingerprint=file_fingerprint(source),
                    destination=destination,
                    destination_relative=destination_relative,
                )
            )

        if any(
            len(uses) > 1 and any(operation != "copy" for operation in uses)
            for uses in source_operations.values()
        ):
            raise FileOperationError(
                "A moved, renamed, or deleted file cannot be reused in the same plan."
            )
        source_keys = set(source_operations)
        if destinations.intersection(source_keys):
            raise FileOperationError(
                "A destination cannot also be a source in the same plan."
            )
        contains_delete = any(item.operation == "delete" for item in planned)
        if contains_delete and not all(item.operation == "delete" for item in planned):
            raise FileOperationError(
                "Recycle Bin operations must use a separate plan from copy, move, and rename."
            )
        return FileOperationPlan(
            plan_id=uuid.uuid4().hex,
            root=self.root,
            operations=tuple(planned),
            created_at=time.monotonic(),
        )

    def _validate_plan(self, plan: FileOperationPlan) -> None:
        if plan.root != self.root:
            raise FileOperationError("The plan belongs to a different authorized folder.")
        if time.monotonic() - plan.created_at > FILE_OPERATION_PLAN_TTL_SECONDS:
            raise FileOperationError("The file-operation plan expired. Create a new plan.")
        for item in plan.operations:
            self._assert_no_links(item.source)
            if (
                not item.source.is_file()
                or file_fingerprint(item.source) != item.fingerprint
            ):
                raise FileOperationError(
                    f"Source changed after planning: {item.source_relative}"
                )
            if item.destination is not None:
                self._assert_no_links(item.destination)
                if item.destination.exists() or item.destination.is_symlink():
                    raise FileOperationError(
                        f"Destination changed after planning: {item.destination_relative}"
                    )

    @staticmethod
    def _copy_file(source: Path, destination: Path) -> None:
        try:
            copy_file_atomically(source, destination)
        except FileExistsError as exc:
            raise FileOperationError(f"Destination already exists: {destination}") from exc

    @staticmethod
    def _move_file(source: Path, destination: Path) -> None:
        if source.stat().st_dev != destination.parent.stat().st_dev:
            raise FileOperationError("Moves must stay on the same filesystem.")
        try:
            reserve_new_file(destination)
        except FileExistsError as exc:
            raise FileOperationError(f"Destination already exists: {destination}") from exc
        try:
            os.replace(source, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def _rollback(journal: list[_RollbackRecord]) -> list[str]:
        failures = []
        for record in reversed(journal):
            try:
                if record.fingerprint is None:
                    raise FileOperationError("created file could not be verified")
                if (
                    not record.current.is_file()
                    or file_fingerprint(record.current) != record.fingerprint
                ):
                    raise FileOperationError("created file changed before rollback")
                if record.operation == "copy":
                    record.current.unlink()
                elif record.original is None:
                    raise FileOperationError("original move path is missing")
                elif record.original.exists() or record.original.is_symlink():
                    raise FileOperationError("original path was recreated before rollback")
                else:
                    os.replace(record.current, record.original)
            except Exception as exc:
                failures.append(f"{record.current}: {exc}")
        return failures

    def apply_steps(self, plan: FileOperationPlan):
        self._validate_plan(plan)
        if plan.contains_delete:
            yield {
                "stage": "moving_files_to_recycle_bin",
                "current": 0,
                "completed": 0,
                "total": len(plan.operations),
            }
            completed = []
            failures = []
            for item in plan.operations:
                try:
                    self._assert_no_links(item.source)
                    if (
                        not item.source.is_file()
                        or file_fingerprint(item.source) != item.fingerprint
                    ):
                        raise FileOperationError("Source changed after planning.")
                    if not QFile.moveToTrash(str(item.source)):
                        raise FileOperationError("The operating system rejected the Recycle Bin operation.")
                    completed.append(item.payload())
                except Exception as exc:
                    failures.append({**item.payload(), "error": str(exc)})
            return {
                "status": "completed" if not failures else "partial" if completed else "failed",
                "root": str(self.root),
                "operation_count": len(plan.operations),
                "completed_count": len(completed),
                "failure_count": len(failures),
                "operations": completed,
                "failures": failures,
                "deleted_permanently": False,
                "sent_to_recycle_bin": len(completed),
            }

        journal: list[_RollbackRecord] = []
        try:
            for index, item in enumerate(plan.operations):
                yield {
                    "stage": "applying_file_operations",
                    "current": index + 1,
                    "completed": index,
                    "total": len(plan.operations),
                    "item": item.source_relative,
                }
                if item.destination is None:
                    raise FileOperationError("The planned destination is missing.")
                self._assert_no_links(item.source)
                if (
                    not item.source.is_file()
                    or file_fingerprint(item.source) != item.fingerprint
                ):
                    raise FileOperationError(
                        f"Source changed after planning: {item.source_relative}"
                    )
                self._assert_no_links(item.destination)
                if item.operation == "copy":
                    self._copy_file(item.source, item.destination)
                    record = _RollbackRecord("copy", item.destination, None, None)
                    journal.append(record)
                    record.fingerprint = file_fingerprint(item.destination)
                else:
                    self._move_file(item.source, item.destination)
                    journal.append(
                        _RollbackRecord(
                            "move",
                            item.destination,
                            item.source,
                            item.fingerprint,
                        )
                    )
            return {
                "status": "completed",
                "root": str(self.root),
                "operation_count": len(plan.operations),
                "completed_count": len(plan.operations),
                "failure_count": 0,
                "operations": [item.payload() for item in plan.operations],
                "failures": [],
                "rolled_back": False,
            }
        except BaseException as exc:
            rollback_failures = self._rollback(journal)
            if rollback_failures:
                raise FileOperationError(
                    f"File operation failed ({exc}); rollback also failed: "
                    + "; ".join(rollback_failures)
                ) from exc
            journal.clear()
            if not isinstance(exc, Exception):
                raise
            raise FileOperationError(
                f"File operation failed and was rolled back: {exc}"
            ) from exc

    def trash_entry(self, relative_path: str) -> None:
        """Move one file or directory to the OS trash without a permanent fallback."""
        target = self._existing_member(relative_path, "path")
        if not (target.is_file() or target.is_dir()):
            raise FileOperationError("Only files and directories can be moved to the Recycle Bin.")
        if not QFile.moveToTrash(str(target)):
            raise FileOperationError("The operating system rejected the Recycle Bin operation.")
