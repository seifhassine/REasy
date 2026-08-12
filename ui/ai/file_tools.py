"""Scoped folder discovery and file-operation tools for the REasy assistant."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QT_TRANSLATE_NOOP

from services.file_operations import (
    FILE_OPERATION_PLAN_TTL_SECONDS,
    FileOperationError,
    FileOperationPlan,
    FolderFileOperations,
)
from settings import AI_FILE_ACTION_MODES
from ui.ai.action_policy import AiChangeDecision
from ui.ai.tool_registry import (
    AssistantToolError,
    tool,
    translate_tool_text as _tr,
)


FILE_MANAGEMENT_CAPABILITY = "file_management"

FILE_MANAGEMENT_ASSISTANT_PROMPT = """\
Scoped folder tools are enabled because the user requested local file or
directory work. The root must be an exact absolute folder path supplied by the
user; never invent, broaden, or infer a parent folder. Discover entries with
list_folder_entries, then use plan_file_operations and apply_file_operations.

File mutations are file-only and stay beneath that root. Copy/move/rename never
overwrite, links and junctions are blocked, and delete means the OS Recycle Bin
rather than permanent deletion. Moving or renaming assets does not rewrite RE
Engine references; report that limitation when it matters. Do not apply a plan
unless the user explicitly requested those operations. Use one batch plan to
avoid per-file prompts.
"""


def file_management_prompt_matches(prompt: str) -> bool:
    text = str(prompt or "").casefold()
    mutation_verb = bool(
        re.search(
            r"\b(?:copy|move|rename|delete|remove|trash)\b",
            text,
        )
    )
    filesystem_object = bool(
        re.search(r"\b(?:files?|folders?|directories|outputs?)\b", text)
        or re.search(r"\b[^\\/\s]+\.[a-z0-9_]{1,12}(?:\.\d+)?\b", text)
    )
    discovering = bool(
        re.search(
            r"\b(?:list|show|find|discover|inspect|browse|scan|explore)\b"
            r"|\blook\s+(?:at|in|through)\b"
            r"|\bwhat(?:'s|\s+is)\s+(?:inside|in|under)\b",
            text,
        )
    )
    has_absolute_scope = bool(
        re.search(
            r"(?:\b[a-z]:[\\/]|\\\\[^\\/\s]+[\\/][^\\/\s]+)",
            text,
        )
    )
    return has_absolute_scope and (
        discovering or (mutation_verb and filesystem_object)
    )


def _operation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["copy", "move", "rename", "delete"],
            },
            "source": {
                "type": "string",
                "description": "Exact root-relative file path returned by list_folder_entries.",
            },
            "destination_directory": {
                "type": "string",
                "description": "Root-relative existing directory for copy or move; an empty string means the authorized root.",
            },
            "new_name": {
                "type": "string",
                "description": "Optional destination file name for copy/move; required for rename.",
            },
            "allow_extension_change": {
                "type": "boolean",
                "description": "Set true only when the user explicitly requested changing the file extension.",
            },
        },
        "required": ["operation", "source"],
        "additionalProperties": False,
    }


def file_tool_definitions():
    return (
        tool(
            "list_folder_entries",
            "List files and directories beneath an exact absolute folder path supplied by the user. This is read-only, does not follow links or junctions, and returns root-relative paths for file-operation planning.",
            {
                "root": {
                    "type": "string",
                    "description": "Exact existing absolute folder path supplied by the user.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional root-relative directory; defaults to the authorized root.",
                },
                "recursive": {"type": "boolean"},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 32},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            ["root"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Inspecting the requested folder"),
                QT_TRANSLATE_NOOP("AiChatDock", "Inspected the requested folder"),
            ),
            capability=FILE_MANAGEMENT_CAPABILITY,
        ),
        tool(
            "plan_file_operations",
            "Create a read-only, short-lived plan to copy, move, rename, or send files to the OS Recycle Bin beneath one user-supplied folder. Plans reject overwrites, open files, directory mutations, path escapes, links, and mixed deletion/non-deletion batches.",
            {
                "root": {
                    "type": "string",
                    "description": "Exact existing absolute folder path supplied by the user.",
                },
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 256,
                    "items": _operation_schema(),
                },
            },
            ["root", "operations"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Planning file operations"),
                QT_TRANSLATE_NOOP("AiChatDock", "Planned file operations"),
            ),
            capability=FILE_MANAGEMENT_CAPABILITY,
        ),
        tool(
            "apply_file_operations",
            "Apply one exact plan returned by plan_file_operations. Copy/move/rename batches roll back on failure; deletion uses only the OS Recycle Bin. Authorization behavior follows the user's Assistant File Actions setting.",
            {
                "plan_id": {
                    "type": "string",
                    "description": "Exact short-lived plan ID returned by plan_file_operations.",
                }
            },
            ["plan_id"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Applying file operations"),
                QT_TRANSLATE_NOOP("AiChatDock", "Applied file operations"),
            ),
            capability=FILE_MANAGEMENT_CAPABILITY,
            incremental=True,
            persistent=True,
            result_card=True,
        ),
    )


class FolderAssistantToolMixin:
    def _initialize_file_tools(self) -> None:
        self._file_request_id = 0
        self._file_request_prompt = ""
        self._file_operation_plans: dict[
            str,
            tuple[int, FolderFileOperations, FileOperationPlan],
        ] = {}
        self._file_request_authorized_roots: set[str] = set()

    def _begin_file_request(self, prompt: str) -> None:
        self._file_request_id += 1
        self._file_request_prompt = str(prompt or "")
        self._file_operation_plans.clear()
        self._file_request_authorized_roots.clear()

    def _reset_file_tools(self) -> None:
        self._file_request_prompt = ""
        self._file_operation_plans.clear()
        self._file_request_authorized_roots.clear()

    @staticmethod
    def _normalized_path_text(value: str) -> str:
        return str(value or "").strip().replace("\\", "/").rstrip("/").casefold()

    def _folder_service_for_user_root(self, root: str) -> FolderFileOperations:
        raw = str(root or "").strip()
        mentioned = self._normalized_path_text(raw)
        prompt = self._normalized_path_text(self._file_request_prompt)
        exact_mention = False
        start = 0
        while mentioned and (index := prompt.find(mentioned, start)) >= 0:
            before = prompt[index - 1] if index else ""
            end = index + len(mentioned)
            after = prompt[end] if end < len(prompt) else ""
            after_boundary = not after or after in " \t\r\n\"'`)>]},;.!?"
            if after == "/":
                trailing = prompt[end + 1] if end + 1 < len(prompt) else ""
                after_boundary = (
                    not trailing
                    or trailing in " \t\r\n\"'`)>]},;.!?"
                )
            if (
                (not before or before in " \t\r\n\"'`(<[{=:,")
                and after_boundary
            ):
                exact_mention = True
                break
            start = index + 1
        if not exact_mention:
            raise AssistantToolError(
                _tr(
                    "The folder root must be an exact absolute path supplied in the current user request."
                )
            )
        try:
            return FolderFileOperations(raw)
        except FileOperationError as exc:
            raise AssistantToolError(str(exc)) from exc

    def _list_folder_entries(
        self,
        root: str,
        path: str = "",
        recursive: bool = False,
        max_depth: int = 1,
        limit: int = 200,
    ) -> dict[str, Any]:
        service = self._folder_service_for_user_root(root)
        try:
            return service.list_entries(
                path,
                recursive=recursive,
                max_depth=max_depth,
                limit=limit,
            )
        except FileOperationError as exc:
            raise AssistantToolError(str(exc)) from exc

    @staticmethod
    def _disk_path_key(value: str | os.PathLike[str]) -> str:
        expanded = os.path.expandvars(os.path.expanduser(str(value)))
        return os.path.normcase(os.path.normpath(str(Path(expanded).resolve())))

    def _open_disk_files(self) -> dict[str, dict[str, Any]]:
        result = {}
        for _tab, _session, payload in self._open_tab_records():
            path = str(payload.get("path") or "").strip()
            if not path:
                continue
            if not Path(path).is_absolute():
                project = str(payload.get("project") or "").strip()
                if not project:
                    continue
                path = str(Path(project) / path)
            result[self._disk_path_key(path)] = payload
        return result

    def _plan_file_operations(
        self,
        root: str,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        service = self._folder_service_for_user_root(root)
        try:
            plan = service.plan(operations)
        except FileOperationError as exc:
            raise AssistantToolError(str(exc)) from exc
        open_files = self._open_disk_files()
        for item in plan.operations:
            payload = open_files.get(self._disk_path_key(item.source))
            if payload is not None:
                state = "modified" if payload.get("modified") else "open"
                raise AssistantToolError(
                    _tr(
                        "File operations refuse {state} editor files: {path}",
                        state=state,
                        path=item.source_relative,
                    )
                )
        self._file_operation_plans[plan.plan_id] = (
            self._file_request_id,
            service,
            plan,
        )
        result = plan.payload()
        mode = self._file_action_mode()
        result.update(
            {
                "status": "planned",
                "expires_in_seconds": FILE_OPERATION_PLAN_TTL_SECONDS,
                "action_mode": mode,
                "confirmation_required": not (
                    mode == "scoped_autopilot"
                    and (
                        not plan.contains_delete
                        or self._file_autopilot_allows_trash()
                    )
                ),
                "permanent_deletion": False,
                "reference_updates_performed": False,
            }
        )
        return result

    def _file_plan_record(
        self,
        plan_id: Any,
    ) -> tuple[FolderFileOperations, FileOperationPlan]:
        key = str(plan_id or "").strip()
        record = self._file_operation_plans.get(key)
        if record is None or record[0] != self._file_request_id:
            raise AssistantToolError(
                _tr("File-operation plan was not found or belongs to an earlier request.")
            )
        return record[1], record[2]

    def _apply_file_operations(self, plan_id: str) -> dict[str, Any]:
        return self._run_incremental_steps(
            self._apply_file_operations_steps(plan_id)
        )

    def _apply_file_operations_steps(self, plan_id: str):
        service, plan = self._file_plan_record(plan_id)
        try:
            result = yield from service.apply_steps(plan)
        except FileOperationError as exc:
            raise AssistantToolError(str(exc)) from exc
        finally:
            self._file_operation_plans.pop(str(plan_id), None)
        dock = getattr(self.app, "proj_dock", None)
        project_dir = str(getattr(dock, "project_dir", "") or "").strip()
        refresh = getattr(dock, "_refresh_proj", None)
        if project_dir and callable(refresh):
            project_root = Path(project_dir).resolve()
            affected_paths = (
                path
                for item in plan.operations
                for path in (item.source, item.destination)
                if path is not None
            )
            if any(path.is_relative_to(project_root) for path in affected_paths):
                refresh()
        result["plan_id"] = plan.plan_id
        result["authorization_mode"] = self._file_action_mode()
        result["reference_updates_performed"] = False
        return result

    def _file_action_mode(self) -> str:
        settings = getattr(self.app, "settings", {}) or {}
        mode = str(settings.get("ai_file_action_mode", "review")).strip().casefold()
        return mode if mode in AI_FILE_ACTION_MODES else "review"

    def _file_autopilot_allows_trash(self) -> bool:
        settings = getattr(self.app, "settings", {}) or {}
        return settings.get("ai_file_autopilot_trash") is True

    def _authorize_file_operation_plan(
        self,
        arguments: dict[str, Any],
    ) -> bool:
        _service, plan = self._file_plan_record(arguments.get("plan_id"))
        mode = self._file_action_mode()
        root_key = self._disk_path_key(plan.root)

        if plan.contains_delete:
            if mode == "scoped_autopilot" and self._file_autopilot_allows_trash():
                return True
            decision = self._confirm_file_operation_plan(arguments)
            return decision in {
                AiChangeDecision.ALLOW_ONCE,
                AiChangeDecision.ALLOW_PROMPT,
            }

        if mode == "scoped_autopilot":
            return True
        if mode == "request" and root_key in self._file_request_authorized_roots:
            return True
        if mode == "request":
            decision = self._confirm_file_operation_plan(arguments)
            allowed = decision in {
                AiChangeDecision.ALLOW_ONCE,
                AiChangeDecision.ALLOW_PROMPT,
            }
            if allowed:
                self._file_request_authorized_roots.add(root_key)
            return allowed
        decision = self._confirm_file_operation_plan(arguments)
        return decision in {
            AiChangeDecision.ALLOW_ONCE,
            AiChangeDecision.ALLOW_PROMPT,
        }

    def _confirm_file_operation_plan(
        self,
        arguments: dict[str, Any],
    ) -> AiChangeDecision:
        if not getattr(self, "_uses_default_tool_confirmation", False):
            return self._confirm_tool_action("apply_file_operations", arguments)
        action, details = self._summarize_apply_file_operations(arguments)
        _service, plan = self._file_plan_record(arguments.get("plan_id"))
        question = (
            _tr(
                "Allow this plan and later copy, move, or rename plans inside "
                "the same exact folder until the current request ends?"
            )
            if self._file_action_mode() == "request" and not plan.contains_delete
            else _tr("Allow this scoped file-operation plan?")
        )
        return self._action_policy.show_confirmation(
            _tr("Confirm Assistant File Operations"),
            _tr(
                "{question}\n\n{action}\n\n{details}",
                question=question,
                action=action,
                details=details,
            ),
            allow_prompt=False,
        )

    def _summarize_apply_file_operations(
        self,
        arguments: dict[str, Any],
    ) -> tuple[str, str]:
        _service, plan = self._file_plan_record(arguments.get("plan_id"))
        lines = [
            _tr("Folder: {root}", root=plan.root),
            _tr("Operations: {count}", count=len(plan.operations)),
        ]
        lines.extend(
            (
                f"{item.operation}: {item.source_relative}"
                + (
                    f" → {item.destination_relative}"
                    if item.destination_relative is not None
                    else " → Recycle Bin"
                )
            )
            for item in plan.operations
        )
        action = (
            _tr("Move files to the Recycle Bin")
            if plan.contains_delete
            else _tr("Apply file operations")
        )
        return action, "\n".join(lines)
