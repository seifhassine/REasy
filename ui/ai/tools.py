from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import tempfile
from functools import partial
from inspect import signature
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from PySide6.QtCore import QT_TRANSLATE_NOOP, QTimer, Qt

from app_config import GAMES
from services.ai.mdf_merge_planner import (
    MISSING as _MISSING,
    merge_equal as _merge_equal,
    plan_three_way_merge as _plan_three_way_merge,
)
from ui.ai.action_feedback import AiActionFeedback
from ui.ai.action_policy import (
    AiActionPolicy,
    AiChangeDecision,
)
from ui.ai.file_migration import migrate_file_jobs_steps
from ui.ai.file_tools import (
    FILE_MANAGEMENT_ASSISTANT_PROMPT,
    FILE_MANAGEMENT_CAPABILITY,
    FolderAssistantToolMixin,
    file_management_prompt_matches,
    file_tool_definitions,
)
from ui.ai.tool_registry import (
    AiToolDefinition,
    AssistantToolError,
    ToolSchemaContext,
    tool as _tool,
    translate_tool_text as _tr,
)
from ui.ai.update_reports import FileUpdateReport, UpdateReportCollector
from ui.ai.msg_tools import (
    MSG_ASSISTANT_CAPABILITY_PROMPT,
    MSG_CAPABILITY,
    MSG_EDIT_ASSISTANT_CAPABILITY_PROMPT,
    MSG_EDIT_CAPABILITY,
    MsgAssistantToolMixin,
    msg_prompt_matches,
    msg_tool_definitions,
)
from ui.ai.rsz_tools import (
    RSZ_ASSISTANT_CAPABILITY_PROMPT,
    RSZ_CAPABILITY,
    RSZ_EDIT_ASSISTANT_CAPABILITY_PROMPT,
    RSZ_EDIT_CAPABILITY,
    RszAssistantToolMixin,
    rsz_prompt_matches,
    rsz_tool_definitions,
)
from ui.project_manager.constants import PROJECTS_ROOT
from ui.project_manager.project_picker_dialog import discover_projects

if TYPE_CHECKING:
    from file_handlers.mdf.mdf_viewer import MdfViewer


AI_ASSISTANT_BASE_PROMPT = """\
You are the AI Assistant embedded in REasy, an RE Engine mod editor.

Use the supplied tools for facts and actions. Do not invent file contents,
project state, or successful actions. When a request is ambiguous, ask one
concise question instead of guessing.

Format-specific editor tools are loaded only when relevant. If a result reveals
an MDF, MSG, or RSZ-based file and its read tools are unavailable, call
enable_ai_capability with capability="mdf", capability="msg", or
capability="rsz". Load the corresponding "mdf_edit", "msg_edit", or
"rsz_edit" capability only for requested changes or ordinary two-file value
copies. Load capability="mdf_three_way" only when the user explicitly asks for
a "three-way" or "3-way" migration, and capability="mdf_folder_update" only
when they explicitly request an external-folder mod rebuild. Use
capability="pak" only to browse, open, or add files from game PAK archives. Do
not call a capability when the needed tools are already supplied.

Read-only research may cross file formats. Do not assume the active editor is
the only relevant source: use the active project's file listing to discover
likely MDF, MSG, and RSZ evidence, then inspect only the files that can answer
the question. MSG localized text can provide names, labels, descriptions, and
dialogue that explain otherwise opaque RSZ values. Do not bulk-open unrelated
files, and do not treat research as permission to edit. Preserve exact file
paths from tool results when correlating or reporting evidence. Disk-backed
results use absolute paths; PAK-backed results use exact archive-internal paths.

You may find and directly open existing projects, list or open project files,
open project settings, and start Fluffy ZIP or PAK export workflows. Prefer
list_projects followed by open_project when the user asks to find or open an
existing project. Only show the project-library dialog when the user explicitly
asks to see the project manager or choose through the UI. New projects still use
REasy's guided dialog. Export tools may display dialogs and may only start an
asynchronous workflow, so report exactly what their tool result says.

Projects are optional. Use open_file for an exact local filesystem path when the
user wants to work directly with a file. With no active project, REasy opens it
as a standalone tab; inspection, comparison, editing, and saving tools work on
standalone tabs exactly as they do on project tabs. Never require the user to
create or open a project just to work with an already-open or directly opened
file.

File-update workflows are the exception: migrate_mdf_files, update_mod_folder,
migrate_msg_files, update_msg_mod_folder, migrate_rsz_files, and
analyze_rsz_mod_folder_update/update_rsz_mod_folder require the user to first
open the project for the relevant game and load that game's PAK files in the
Project Browser. If that precondition is missing, stop and explain it; do not
open a project, show a picker, create a project, or begin the update on the
user's behalf. Return control to the user so they can open the project
themselves.

Honor the source named by the user. For a file requested from or in a project,
search with list_project_files and open it with open_project_file. Never
silently substitute a game-PAK file; use PAK discovery only when the user asks
for the PAK/game-original source, or after reporting that no project file
matched and obtaining confirmation to fall back.

Use get_reasy_context when you need workspace or navigation details that another
tool result did not already provide. It reports every open project and tab,
including modified in-memory files. Prefer activate_open_tab over reopening a
path so unsaved editor state is preserved.

When the user explicitly supplies an absolute folder and asks to discover,
copy, move, rename, or delete local files, enable capability="file_management"
if its tools are not already available. The supplied folder is the complete
authorization boundary: never invent a root, broaden it to a parent, or treat a
read-only inspection request as permission to mutate. File deletion means the
OS Recycle Bin and never permanent removal.

Completed RSZ and MSG disk or mod-folder update attempts retain an exact,
paginated post-update report. When the user later asks what was imported into
the latest files, which elements were new, what stayed at the latest PAK value,
or what could not be imported, call inspect_file_update_report. Omit update_id
for the most recent update; use section="updates" to find an older one. Fetch
the report instead of inferring differences from the conversation or reopening
every file. Page or filter detailed sections as needed, and never claim the
detail list is exhaustive when details_complete is false. These reports
describe output copies built from the latest PAK originals; never imply that
the game PAK archives themselves were changed.

Changes made through editor tools remain unsaved; only call save_active_file or save_all_modified_files
when the user explicitly asks to save. Use Save All after a batch only when the
user asks to save every edited file.

Reply in the language used by the user unless they explicitly request another
language. Keep exact file paths, identifiers, field names, and numeric values
unchanged.

When using a Markdown comparison table, keep every row on one source line, give
every row an explicit first-column label, and separate multiple values in one
cell with semicolons. Do not place HTML or line breaks inside table cells.
"""

MDF_ASSISTANT_CAPABILITY_PROMPT = """\
MDF read tools are enabled. Inspect an MDF when its contents are not already
known. When a request is ambiguous about a material or value, ask a concise
question instead of guessing. Use list_project_files with query=".mdf2" for
project MDF searches, then use open_project_file on the selected result path.

Minimize tool calls and reuse facts already returned during the current request.
For an exact comparison of two MDFs, use compare_mdf_files. It checks supported
in-memory MDF values locally and returns only differences. Never infer equality
from an inspect_mdf summary or a copy_mdf_values result. When a copy result has
a nonzero destination_only_value_count, report those paths as differences that
were preserved by design. Comparison requests are read-only, so do not call
copy, migration, or edit tools unless the user also asks to change data. For
other read-only work, inspect multiple tabs or materials in one inspect_mdf call
instead of activating tabs or calling it once per material.
"""

MDF_EDIT_ASSISTANT_CAPABILITY_PROMPT = """\
MDF edit tools are enabled. MDF mutation tools drive the visible REasy editor.
Inspect an MDF before a targeted manual edit when its contents are not already
known. Preserve fields the user did not ask to change and prefer targeted
upserts.

Use copy_mdf_values for a two-file source-to-destination copy. It inspects its
inputs itself, so do not inspect first solely to use it.
This is the default for ordinary MDF migration, update, and carry-over requests.
Do not use migrate_mdf_files unless the user explicitly says "three-way" or
"3-way".
An opened PAK-backed MDF is a valid copy destination and is edited directly in
memory; never extract or add it to the project unless the user asks.
For copy_mdf_values, map "from A to B" literally: source=A and destination=B.
Never reverse them. REasy asks the
user to confirm the resolved direction before applying changes.

Individual MDF mutation tools edit the active MDF unless an exact tab ID is
supplied. For edits spanning multiple open files, use batch_edit_files once
and give every action its destination tab ID. Never repeat active-only edits
while assuming that they target different files.

After making changes, briefly list what changed and remind the user that the MDF
is unsaved unless a save tool succeeded.
"""

MDF_THREE_WAY_ASSISTANT_CAPABILITY_PROMPT = """\
Explicit three-way MDF migration is enabled. Use migrate_mdf_files only because
the user explicitly requested a "three-way" or "3-way" migration and the old
original, modded old, and latest destination are all available. The tool
inspects its inputs itself, so do not inspect first solely to use it. Report
conflicts, skipped values, failures, and whether results remain unsaved.
"""

MDF_FOLDER_UPDATE_ASSISTANT_CAPABILITY_PROMPT = """\
External-folder MDF updating is enabled. Use update_mod_folder only because the
user explicitly requested an external-folder rebuild against latest extracted
originals. The tool inspects its inputs itself, so do not inspect first solely
to use it. Report skipped values, failures, and output paths.
"""

PAK_ASSISTANT_CAPABILITY_PROMPT = """\
PAK file tools are enabled. Use them when the user explicitly requests a game
PAK/archive source, the game-original or vanilla source, or has confirmed a
fallback after no requested project file matched.

For a follow-up referring to PAK results already listed, reuse an exact returned
path instead of listing or checking the workspace again. Use open_pak_file to
open it and add_pak_file_to_project when the user also asks to add it to the
project; both may be called in the same round.

PAK-backed tabs open in the appropriate REasy editor when the format is
supported. Use any format-specific tools that become available after opening.
Changes cannot be written back into the game archive; saving an editable tab
instead creates or updates a project copy, or uses Save As when no project is
open, and may show a confirmation dialog.
"""

MDF_CAPABILITY = "mdf"
MDF_EDIT_CAPABILITY = "mdf_edit"
MDF_THREE_WAY_CAPABILITY = "mdf_three_way"
MDF_FOLDER_UPDATE_CAPABILITY = "mdf_folder_update"
PAK_CAPABILITY = "pak"
_UPDATE_PROJECT_TOOL_NAMES = frozenset(
    {
        "migrate_mdf_files",
        "update_mod_folder",
        "migrate_msg_files",
        "update_msg_mod_folder",
        "migrate_rsz_files",
        "update_rsz_mod_folder",
    }
)
_PROJECT_OPEN_TOOL_NAMES = frozenset(
    {
        "open_project",
        "show_open_project_dialog",
        "show_create_project_dialog",
    }
)
_CAPABILITY_PROMPTS = {
    MDF_CAPABILITY: MDF_ASSISTANT_CAPABILITY_PROMPT,
    MDF_EDIT_CAPABILITY: MDF_EDIT_ASSISTANT_CAPABILITY_PROMPT,
    MDF_THREE_WAY_CAPABILITY: MDF_THREE_WAY_ASSISTANT_CAPABILITY_PROMPT,
    MDF_FOLDER_UPDATE_CAPABILITY: (
        MDF_FOLDER_UPDATE_ASSISTANT_CAPABILITY_PROMPT
    ),
    MSG_CAPABILITY: MSG_ASSISTANT_CAPABILITY_PROMPT,
    MSG_EDIT_CAPABILITY: MSG_EDIT_ASSISTANT_CAPABILITY_PROMPT,
    RSZ_CAPABILITY: RSZ_ASSISTANT_CAPABILITY_PROMPT,
    RSZ_EDIT_CAPABILITY: RSZ_EDIT_ASSISTANT_CAPABILITY_PROMPT,
    PAK_CAPABILITY: PAK_ASSISTANT_CAPABILITY_PROMPT,
    FILE_MANAGEMENT_CAPABILITY: FILE_MANAGEMENT_ASSISTANT_PROMPT,
}


def _validate_capabilities(capabilities: Iterable[str]) -> frozenset[str]:
    enabled = frozenset(capabilities)
    unknown = enabled.difference(_CAPABILITY_PROMPTS)
    if unknown:
        raise ValueError(f"Unknown AI capabilities: {sorted(unknown)}")
    return enabled


def assistant_system_prompt(capabilities: Iterable[str] = ()) -> str:
    """Build the prompt from the small base plus enabled format guidance."""

    enabled = _validate_capabilities(capabilities)
    parts = [
        AI_ASSISTANT_BASE_PROMPT.rstrip(),
        *(
            prompt.rstrip()
            for name, prompt in _CAPABILITY_PROMPTS.items()
            if name in enabled
        ),
    ]
    return "\n\n".join(parts)


MAX_MIGRATION_JOB_PAYLOAD_BYTES = 256_000
_VERSIONED_MDF_RE = re.compile(r"\.mdf2\.\d+$", re.IGNORECASE)
_MDF_PROMPT_HINT_RE = re.compile(
    r"(?:\bmdf(?:s|2)?\b|\.mdf2(?:\.\d+)?)",
    re.IGNORECASE,
)
_CROSS_FORMAT_RESEARCH_PROMPT_HINT_RE = re.compile(
    r"(?:"
    r"\b(?:research(?:ed|ing)?|investigat(?:e[ds]?|ing|ion)|"
    r"analy(?:sis|[sz](?:e[ds]?|ing))|examin(?:e[ds]?|ing)|stud(?:y|ied|ying)|"
    r"understand(?:ing)?|determin(?:e[ds]?|ing)|identif(?:y|ied|ying)|"
    r"explain(?:ed|ing)?|trac(?:e[ds]?|ing))\b"
    r"|\b(?:look|dig)\s+into\b"
    r"|\b(?:figure|find)\s+out\b"
    r"|(?:研究|调查|分析|查明|弄清|理解)"
    r")",
    re.IGNORECASE,
)
_EDIT_PROMPT_HINT_RE = re.compile(
    r"(?:"
    r"\b(?:edit|change|modify|set|toggle|add|delete|remove|replace|copy|"
    r"apply|batch)\b"
    r"|\bcarry\s+(?:it\s+)?over\b"
    r"|(?:编辑|修改|更改|切换|设置|添加|删除|移除|替换|复制|应用|批量)"
    r")",
    re.IGNORECASE,
)
_MDF_THREE_WAY_PROMPT_HINT_RE = re.compile(
    r"(?:"
    r"\b(?:three|3)[- ]?way\b"
    r"|(?:三方|三路|三向)(?:迁移|合并)?"
    r")",
    re.IGNORECASE,
)
_MDF_NORMAL_MIGRATION_PROMPT_HINT_RE = re.compile(
    r"(?:"
    r"\b(?:migrat(?:e|ion)|outdated\s+mod|old\s+mod|"
    r"update\s+(?:(?:this|the|my|an?)\s+)?mod)\b"
    r"|(?:迁移|旧版模组|旧模组|更新模组)"
    r")",
    re.IGNORECASE,
)
_FORMAT_MIGRATION_PROMPT_HINT_RE = re.compile(
    r"(?:"
    r"\b(?:migrat(?:e|ion)|updat(?:e|ed|ing)|upgrade)\b"
    r"|\bcarry\s+(?:it\s+)?over\b"
    r"|(?:迁移|更新|升级)"
    r")",
    re.IGNORECASE,
)
_MDF_FOLDER_UPDATE_PROMPT_HINT_RE = re.compile(
    r"(?:"
    r"\b(?:update|rebuild|migrate)\b[^\n.!?]{0,50}"
    r"\b(?:external\s+)?mod\s+folder\b"
    r"|\b(?:external\s+)?mod\s+folder\b[^\n.!?]{0,50}"
    r"\b(?:update|rebuild|migrate)\b"
    r"|(?:更新|重建|迁移)[^\n。！？]{0,24}(?:外部)?模组文件夹"
    r"|(?:外部)?模组文件夹[^\n。！？]{0,24}(?:更新|重建|迁移)"
    r")",
    re.IGNORECASE,
)
_THREE_WAY_FOLLOW_UP_RE = re.compile(
    r"(?:"
    r"^\s*(?:yes|okay|ok|sure|do\s+(?:it|that)|go\s+ahead|proceed|"
    r"continue|run\s+(?:it|that)|apply\s+(?:it|that))\s*[.!?]*\s*$"
    r"|\b(?:old\s+original|modded\s+old|latest\s+(?:original|destination))\b"
    r"|^\s*(?:是|是的|好的|可以|执行吧|继续|开始吧|就这么做)[。！？.!?]*\s*$"
    r")",
    re.IGNORECASE,
)
_PAK_PROMPT_HINT_RE = re.compile(
    r"(?:\bpaks?\b|\bgame\s+archives?\b)",
    re.IGNORECASE,
)
_GAME_ORIGINAL_PROMPT_HINT_RE = re.compile(
    r"(?:"
    r"\b(?:game[- ]original|original\s+game\s+"
    r"(?:file|copy|version)|vanilla(?:\s+(?:game|file|copy|version))?)\b"
    r"|\b(?:projects?(?:['’]s)?\s+(?:file|copy|mdf)|"
    r"mod(?:ded)?\s+(?:file|copy|mdf))\b[^\n.!?]{0,80}"
    r"\b(?:and|versus|vs\.?|against|with)\b[^\n.!?]{0,40}"
    r"\b(?:the\s+)?(?:game\s+)?original\b"
    r"|\b(?:the\s+)?(?:game\s+)?original\b[^\n.!?]{0,40}"
    r"\b(?:and|versus|vs\.?|against|with)\b[^\n.!?]{0,80}"
    r"\b(?:projects?(?:['’]s)?\s+(?:file|copy|mdf)|"
    r"mod(?:ded)?\s+(?:file|copy|mdf))\b"
    r"|(?:项目|模组)(?:文件|副本|MDF)?[^\n。！？]{0,30}"
    r"(?:和|与|对比|比较|相较于)[^\n。！？]{0,20}"
    r"(?:游戏)?原版(?:文件|副本|MDF)?"
    r"|(?:游戏)?原版(?:文件|副本|MDF)?[^\n。！？]{0,20}"
    r"(?:和|与|对比|比较|相较于)[^\n。！？]{0,30}"
    r"(?:项目|模组)(?:文件|副本|MDF)?"
    r"|(?:游戏原版|原版游戏文件|原始游戏文件|未修改的游戏文件)"
    r")",
    re.IGNORECASE,
)
_PROJECT_SOURCE_HINT_RE = re.compile(
    r"(?:"
    r"\bfrom\b[^\n.!?]{0,80}\bprojects?\b"
    r"|\b(?:open|find|search|locate|look\s+for)\b[^\n.!?]{0,80}"
    r"\b(?:in|inside|within)\b[^\n.!?]{0,50}\bprojects?\b"
    r"|\bprojects?(?:['’]s)?\s+(?:file|copy|mdf)\b"
    r"|从[^\n。！？]{0,30}项目"
    r"|项目(?:中|内|里)(?:的)?(?:文件|MDF)"
    r")",
    re.IGNORECASE,
)


def _pak_source_requested(prompt: str) -> bool:
    return bool(
        _PAK_PROMPT_HINT_RE.search(prompt)
        or _GAME_ORIGINAL_PROMPT_HINT_RE.search(prompt)
    )


def _file_update_requested(prompt: str) -> bool:
    prompt = str(prompt or "")
    if (
        _MDF_THREE_WAY_PROMPT_HINT_RE.search(prompt)
        or _MDF_NORMAL_MIGRATION_PROMPT_HINT_RE.search(prompt)
        or _MDF_FOLDER_UPDATE_PROMPT_HINT_RE.search(prompt)
    ):
        return True
    return bool(
        _FORMAT_MIGRATION_PROMPT_HINT_RE.search(prompt)
        and (
            _MDF_PROMPT_HINT_RE.search(prompt)
            or msg_prompt_matches(prompt)
            or rsz_prompt_matches(prompt)
        )
    )


_FOLLOW_UP_REFERENCE_RE = re.compile(
    r"(?:"
    r"\b(?:it|its|them|they|those|these|that|this|one|ones|any|all|both|same)\b"
    r"|(?:它|它们|这些|那些|任意|任何|全部|其中)"
    r")",
    re.IGNORECASE,
)
_MDF_COPY_SECTIONS = frozenset(
    {
        "header",
        "overview",
        "flags",
        "textures",
        "parameters",
        "gpu_buffers",
        "shader_lods",
    }
)

_CONFIRM_COPY_TITLE = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Confirm File Copy Direction",
)
_CONFIRM_COPY_MESSAGE = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Copy values in this direction?\n\n"
    "{source_kind} {source_id} → {destination_kind} {destination_id}\n\n"
    "Source — read values from:\n"
    "{source_path}\n\n"
    "Destination — will be modified in memory:\n"
    "{destination_path}\n\n"
    "Planned changes: {change_count}\n\n"
    "Continue?",
)
_PAK_BACKED_FILE = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "PAK-backed file",
)
_PROJECT_FILE = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Project file",
)
_OPEN_FILE = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Open file",
)
_CONFIRM_AI_ACTION_TITLE = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Confirm AI Action",
)
_CONFIRM_AI_ACTION_MESSAGE = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Allow the AI Assistant to perform this action?\n\n"
    "{action}\n\n"
    "{details}\n\n"
    "This action may write files to disk.",
)
_ADD_PAK_FILE_ACTION = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Add a PAK file to the active project",
)
_EXPORT_MOD_ACTION = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Export the active project",
)
_SAVE_ACTIVE_FILE_ACTION = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Save the active file",
)
_SAVE_ALL_FILES_ACTION = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Save all modified files",
)
_UPDATE_MOD_FOLDER_ACTION = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Create an updated mod folder",
)
_PAK_PATH_DETAIL = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "PAK path: {path}",
)
_EXPORT_FORMAT_DETAIL = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Export format: {format}",
)
_ACTIVE_FILE = QT_TRANSLATE_NOOP("ReasyAssistantTools", "Active file")
_FILE_DETAIL = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "File: {path}",
)
_ALL_MODIFIED_FILES_DETAIL = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "All currently modified files in REasy",
)
_AUTOMATIC_OUTPUT = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Automatically chosen",
)
_MOD_FOLDER_DETAILS = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Mod folder: {mod_folder}\n"
    "Originals folder: {originals_folder}\n"
    "Output folder: {output_folder}",
)
_OUTPUT_INSPECTION_ERROR = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Could not inspect output_folder {path}: {error}",
)
_OUTPUT_CHANGED_ERROR = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "output_folder is no longer empty: {path}",
)
_OUTPUT_FINALIZE_ERROR = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Could not finalize output_folder {path}: {error}",
)
_BLOCKED_PAK_CAPABILITY_ERROR = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "AI capability '{capability}' is unavailable because the user requested "
    "a project file.",
)
_BLOCKED_THREE_WAY_CAPABILITY_ERROR = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "AI capability '{capability}' is unavailable because this request did "
    "not explicitly ask for three-way migration.",
)
_BLOCKED_FOLDER_UPDATE_CAPABILITY_ERROR = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "AI capability '{capability}' is unavailable because this request did "
    "not explicitly ask for an external-folder update.",
)
_BLOCKED_CAPABILITY_ERRORS = {
    PAK_CAPABILITY: _BLOCKED_PAK_CAPABILITY_ERROR,
    MDF_THREE_WAY_CAPABILITY: _BLOCKED_THREE_WAY_CAPABILITY_ERROR,
    MDF_FOLDER_UPDATE_CAPABILITY: (
        _BLOCKED_FOLDER_UPDATE_CAPABILITY_ERROR
    ),
}


def _is_versioned_mdf(path: Path) -> bool:
    return bool(_VERSIONED_MDF_RE.search(path.name))


def _mdf_relative_key(path: Path) -> str:
    return _VERSIONED_MDF_RE.sub(".mdf2", path.as_posix()).casefold()


def _capability_schema_properties(
    context: ToolSchemaContext,
) -> dict[str, Any]:
    return {
        "capability": {
            "type": "string",
            "enum": sorted(context.available_capabilities),
        }
    }


def _batch_edit_schema_properties(
    context: ToolSchemaContext,
) -> dict[str, Any]:
    return {
        "actions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "tab": {
                        "type": "string",
                        "description": (
                            "Exact destination tab ID returned by an open "
                            "tool or get_reasy_context."
                        ),
                    },
                    "tool": {
                        "type": "string",
                        "enum": list(context.mutation_tool_names),
                    },
                    "arguments": {
                        "type": "object",
                        "description": (
                            "Arguments accepted by the named edit tool, "
                            "excluding tab."
                        ),
                    },
                },
                "required": ["tab", "tool", "arguments"],
                "additionalProperties": False,
            },
        }
    }


_MATERIAL = {
    "type": "string",
    "description": "Material name or zero-based material index.",
}
_INDEX = {
    "type": "integer",
    "description": "Zero-based row index. Use -1 to append for upsert tools.",
}


class ReasyAssistantTools(
    FolderAssistantToolMixin,
    RszAssistantToolMixin,
    MsgAssistantToolMixin,
):
    """Expose constrained REasy and format-editor operations to chat model."""

    _tool_definitions_cache: tuple[AiToolDefinition, ...] | None = None
    _tool_registry_cache: dict[str, AiToolDefinition] | None = None

    def __init__(
        self,
        app_window,
        confirm_file_copy: Callable[
            [dict[str, Any], dict[str, Any], int],
            AiChangeDecision,
        ]
        | None = None,
        confirm_tool_action: Callable[
            [str, dict[str, Any]],
            AiChangeDecision,
        ]
        | None = None,
    ):
        self.app = app_window
        self._action_feedback = AiActionFeedback(app_window)
        self._action_policy = AiActionPolicy(app_window)
        self._confirm_file_copy = (
            confirm_file_copy or self._show_file_copy_confirmation
        )
        self._confirm_tool_action = (
            confirm_tool_action or self._show_tool_action_confirmation
        )
        self._uses_default_tool_confirmation = confirm_tool_action is None
        self._next_tab_id = 1
        self._enabled_capabilities: set[str] = set()
        self._blocked_capabilities: set[str] = set()
        self._update_waiting_for_user_project = False
        self._rsz_update_analyses: dict[str, Any] = {}
        self._file_update_reports: dict[str, FileUpdateReport] = {}
        self._initialize_file_tools()

    def _pulse_open_tab(self, tab):
        sessions = getattr(
            getattr(self.app, "project_workspace", None),
            "sessions",
            None,
        )
        notebook = getattr(sessions, "notebook", None)
        widget = getattr(tab, "notebook_widget", None)
        if notebook is not None and widget is not None:
            try:
                index = notebook.indexOf(widget)
            except (AttributeError, RuntimeError):
                index = -1
            if index >= 0 and self._action_feedback.pulse_tab(
                notebook,
                index,
            ):
                return
        self._action_feedback.pulse_widget(widget)

    def _pulse_project_label(self):
        dock = getattr(self.app, "proj_dock", None)
        self._action_feedback.pulse_widget(
            getattr(dock, "project_label", None)
        )

    @property
    def enabled_capabilities(self) -> frozenset[str]:
        return frozenset(self._enabled_capabilities)

    @property
    def blocked_capabilities(self) -> frozenset[str]:
        return frozenset(self._blocked_capabilities)

    @classmethod
    def is_ui_edit_tool(cls, name: str) -> bool:
        definition = cls.tool_definition(name)
        return bool(definition and definition.ui_edit)

    @classmethod
    def has_result_card(cls, name: str) -> bool:
        definition = cls.tool_definition(name)
        return bool(definition and definition.result_card)

    @classmethod
    def result_stays_unsaved(cls, name: str) -> bool:
        definition = cls.tool_definition(name)
        return bool(definition and definition.unsaved_result)

    @classmethod
    def tool_activity(cls, name: str) -> tuple[str, str] | None:
        definition = cls.tool_definition(name)
        return definition.activity if definition else None

    def set_ui_editing_active(
        self,
        active: bool,
        *,
        immediate: bool = False,
    ) -> bool:
        return self._action_feedback.set_editing_active(
            active,
            immediate=immediate,
        )

    def _inferred_capabilities(self, prompt: str = "") -> set[str]:
        inferred = set()
        prompt = str(prompt or "")
        cross_format_research = bool(
            _CROSS_FORMAT_RESEARCH_PROMPT_HINT_RE.search(prompt)
        )
        active_mdf = False
        active_msg = False
        active_rsz = False
        active_tab = self.app.get_active_tab()
        try:
            self._mdf_for_tab(active_tab)
        except AssistantToolError:
            pass
        else:
            active_mdf = True
        try:
            self._msg_for_tab(active_tab)
        except AssistantToolError:
            pass
        else:
            active_msg = True
        try:
            self._rsz_for_tab(active_tab)
        except AssistantToolError:
            pass
        else:
            active_rsz = True

        if (
            (active_mdf or active_msg or active_rsz)
            and getattr(active_tab, "pak_source_path", None)
        ):
            inferred.add(PAK_CAPABILITY)

        three_way_request = bool(
            _MDF_THREE_WAY_PROMPT_HINT_RE.search(prompt)
        )
        folder_update_request = bool(
            _MDF_FOLDER_UPDATE_PROMPT_HINT_RE.search(prompt)
        )
        normal_migration_request = bool(
            _MDF_NORMAL_MIGRATION_PROMPT_HINT_RE.search(prompt)
        ) and not (three_way_request or folder_update_request)
        mdf_format_request = bool(
            active_mdf
            or three_way_request
            or folder_update_request
            or normal_migration_request
            or _MDF_PROMPT_HINT_RE.search(prompt)
        )
        mdf_request = mdf_format_request or cross_format_research
        if mdf_request:
            inferred.add(MDF_CAPABILITY)
        if mdf_format_request and (
            normal_migration_request
            or _EDIT_PROMPT_HINT_RE.search(prompt)
        ):
            inferred.add(MDF_EDIT_CAPABILITY)
        if three_way_request:
            inferred.add(MDF_THREE_WAY_CAPABILITY)
        if folder_update_request:
            inferred.add(MDF_FOLDER_UPDATE_CAPABILITY)
        msg_format_request = bool(active_msg or msg_prompt_matches(prompt))
        msg_request = msg_format_request or cross_format_research
        if msg_request:
            inferred.add(MSG_CAPABILITY)
        if msg_format_request and (
            _EDIT_PROMPT_HINT_RE.search(prompt)
            or _FORMAT_MIGRATION_PROMPT_HINT_RE.search(prompt)
        ):
            inferred.add(MSG_EDIT_CAPABILITY)
        rsz_format_request = bool(active_rsz or rsz_prompt_matches(prompt))
        rsz_request = rsz_format_request or cross_format_research
        if rsz_request:
            inferred.add(RSZ_CAPABILITY)
        if rsz_format_request and (
            _EDIT_PROMPT_HINT_RE.search(prompt)
            or _FORMAT_MIGRATION_PROMPT_HINT_RE.search(prompt)
        ):
            inferred.add(RSZ_EDIT_CAPABILITY)
        if _pak_source_requested(prompt):
            inferred.add(PAK_CAPABILITY)
        if file_management_prompt_matches(prompt):
            inferred.add(FILE_MANAGEMENT_CAPABILITY)
        return inferred

    def begin_request(self, prompt: str = "") -> frozenset[str]:
        """Reset format tools to those relevant to a new user request."""

        self._action_policy.begin_request()
        prompt = str(prompt or "")
        self._begin_file_request(prompt)
        self._update_waiting_for_user_project = bool(
            not getattr(self.app, "current_project", None)
            and _file_update_requested(prompt)
        )
        previous = self._enabled_capabilities.copy()
        follow_up = bool(
            previous and _FOLLOW_UP_REFERENCE_RE.search(prompt)
        )
        explicit_three_way = bool(
            _MDF_THREE_WAY_PROMPT_HINT_RE.search(prompt)
        )
        continue_three_way = bool(
            MDF_THREE_WAY_CAPABILITY in previous
            and _THREE_WAY_FOLLOW_UP_RE.search(prompt)
        )
        explicit_folder_update = bool(
            _MDF_FOLDER_UPDATE_PROMPT_HINT_RE.search(prompt)
        )
        pak_source_requested = _pak_source_requested(prompt)
        continue_folder_update = bool(
            MDF_FOLDER_UPDATE_CAPABILITY in previous and follow_up
        )
        self._blocked_capabilities = set()
        if not (explicit_three_way or continue_three_way):
            self._blocked_capabilities.add(MDF_THREE_WAY_CAPABILITY)
        if not (explicit_folder_update or continue_folder_update):
            self._blocked_capabilities.add(
                MDF_FOLDER_UPDATE_CAPABILITY
            )
        if (
            _PROJECT_SOURCE_HINT_RE.search(prompt)
            and not pak_source_requested
        ):
            self._blocked_capabilities.add(PAK_CAPABILITY)
        inferred = self._inferred_capabilities(prompt)
        if follow_up or continue_three_way or continue_folder_update:
            inferred.update(previous)
        inferred.difference_update(self._blocked_capabilities)
        self._enabled_capabilities = set(
            _validate_capabilities(inferred)
        )
        return self.enabled_capabilities

    def refresh_capabilities(self) -> frozenset[str]:
        """Add tools for a format opened during the current request."""

        inferred = self._enabled_capabilities.union(
            self._inferred_capabilities()
        ).difference(self._blocked_capabilities)
        self._enabled_capabilities = set(_validate_capabilities(inferred))
        return self.enabled_capabilities

    def reset_capabilities(self) -> None:
        self._enabled_capabilities.clear()
        self._blocked_capabilities.clear()
        self._update_waiting_for_user_project = False
        self._action_policy.reset()
        self._reset_file_tools()

    @staticmethod
    def _boolean(value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise AssistantToolError(
                _tr("{field} must be true or false.", field=field)
            )
        return value

    @staticmethod
    def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise AssistantToolError(_tr("{field} must be an integer.", field=field))
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise AssistantToolError(
                _tr("{field} must be an integer.", field=field)
            ) from exc
        if not minimum <= parsed <= maximum:
            raise AssistantToolError(
                _tr(
                    "{field} must be between {minimum} and {maximum}.",
                    field=field,
                    minimum=minimum,
                    maximum=maximum,
                )
            )
        return parsed

    @staticmethod
    def _uint64(value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise AssistantToolError(_tr("{field} must be an integer.", field=field))
        try:
            parsed = int(value, 0) if isinstance(value, str) else int(value)
        except (TypeError, ValueError) as exc:
            raise AssistantToolError(
                _tr(
                    "{field} must be an integer or 0x-prefixed integer.",
                    field=field,
                )
            ) from exc
        if not 0 <= parsed <= 0xFFFFFFFFFFFFFFFF:
            raise AssistantToolError(
                _tr(
                    "{field} must fit in an unsigned 64-bit integer.",
                    field=field,
                )
            )
        return parsed

    @staticmethod
    def _table_row(
        value: Any,
        row_count: int,
        label: str,
        *,
        allow_append: bool = False,
    ) -> int:
        try:
            row = int(value)
        except (TypeError, ValueError) as exc:
            raise AssistantToolError(
                _tr(
                    "{label} index out of range: {index}",
                    label=label,
                    index=value,
                )
            ) from exc
        if allow_append and row == -1:
            return row_count
        if not 0 <= row < row_count:
            raise AssistantToolError(
                _tr(
                    "{label} index out of range: {index}",
                    label=label,
                    index=row,
                )
            )
        return row

    @staticmethod
    def _build_tool_definitions() -> tuple[AiToolDefinition, ...]:
        definitions = (
            _tool(
                "get_reasy_context",
                "Get every open project and tab, including standalone/direct-open, active, modified, detached, PAK-backed, and in-memory editability state.",
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Reading the current REasy workspace"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Checked the current REasy workspace"),
                ),
            ),
            _tool(
                "inspect_file_update_report",
                "Retrieve exact details retained after completed RSZ or MSG "
                "disk/folder updates. It can list prior updates or page the "
                "latest update's imported value changes, new elements from "
                "either the mod or latest PAK, AI-kept latest values, "
                "unresolved differences, and per-file counts. Omit update_id "
                "to inspect the most recent update.",
                {
                    "update_id": {
                        "type": "string",
                        "description": (
                            "Optional update ID returned by an updater. Omit "
                            "for the most recent successful update."
                        ),
                    },
                    "section": {
                        "type": "string",
                        "enum": [
                            "summary",
                            "imported",
                            "new_elements",
                            "kept_latest",
                            "unresolved",
                            "files",
                            "updates",
                        ],
                        "description": "Report section; defaults to summary.",
                    },
                    "file": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive file, PAK path, or "
                            "output-path filter."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive search across paths, "
                            "values, kinds, reasons, and decisions."
                        ),
                    },
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Maximum rows; defaults to 50.",
                    },
                },
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Reading the file update report"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Read the file update report"),
                ),
            ),
            _tool(
                "enable_ai_capability",
                "Load tools for the next round. Use 'mdf', 'msg', or 'rsz' for "
                "format inspection; use the corresponding *_edit capability only "
                "for requested changes. RSZ covers USER/SCN/PFB and headless RSZ "
                "containers such as RCOL. Use 'mdf_three_way' only after an explicit "
                "three-way request, 'mdf_folder_update' only for an explicitly "
                "requested external-folder rebuild, and 'pak' only for explicitly "
                "requested game-archive, game-original, or vanilla sources.",
                _capability_schema_properties,
                ["capability"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Loading the requested editor tools"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Loaded the requested editor tools"),
                ),
            ),
            _tool(
                "activate_open_tab",
                "Switch to an already-open REasy tab without reopening its file or losing unsaved in-memory edits.",
                {
                    "tab": {
                        "type": "string",
                        "description": "Exact tab ID returned by get_reasy_context, or an unambiguous open path or title.",
                    }
                },
                ["tab"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Switching to the open tab"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Switched to the open tab"),
                ),
            ),
            _tool(
                "list_project_files",
                "List files of any type in the active REasy project without a dialog. Search here first when the user requests a file from or in their project. Every match includes its absolute disk path and complete project-relative path.",
                {
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive relative-path filter.",
                    },
                    "extension": {
                        "type": "string",
                        "description": "Optional format suffix such as .mdf2, .scn, .mesh, or .tex. Numeric game versions are matched automatically.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Maximum results; defaults to 200.",
                    },
                },
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Looking through the active project"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Checked the active project's files"),
                ),
            ),
            _tool(
                "open_project_file",
                "Open an exact file from the active project directly without a dialog. Use this, not a PAK opener, when the user requested the project copy.",
                {
                    "path": {
                        "type": "string",
                        "description": "Exact absolute path or project_relative_path returned by list_project_files.",
                    }
                },
                ["path"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Opening the requested project file"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Opened the requested project file"),
                ),
            ),
            _tool(
                "list_pak_files",
                "List file paths from configured game PAKs. Use only for an explicit PAK/game-original request or a user-confirmed fallback, never instead of searching a requested project.",
                {
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive path filter. Use a distinctive name, folder, or format marker such as .mdf2.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Maximum results; defaults to 200.",
                    },
                },
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Searching files in game PAKs"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Searched files in game PAKs"),
                ),
                capability=PAK_CAPABILITY,
            ),
            _tool(
                "open_pak_file",
                "Open an exact file path from configured game PAKs when REasy supports its format. Format-specific tools may become available afterward; changes cannot be written back into the archive.",
                {"path": {"type": "string", "description": "Exact PAK-relative path returned by list_pak_files."}},
                ["path"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Opening a file from the game PAKs"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Opened the PAK file"),
                ),
                capability=PAK_CAPABILITY,
            ),
            _tool(
                "add_pak_file_to_project",
                "Extract an exact file path from configured game PAKs into the active project. Use only for an explicit PAK/game-original source or confirmed fallback.",
                {"path": {"type": "string", "description": "Exact PAK-relative path returned by list_pak_files."}},
                ["path"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Adding a PAK file to the project"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Added the PAK file to the project"),
                ),
                capability=PAK_CAPABILITY,
                persistent=True,
            ),
            _tool(
                "open_file",
                "Open an existing file by an exact filesystem path. This does not require a project; with no active project the file opens in a standalone tab.",
                {"path": {"type": "string", "description": "Absolute or working-directory-relative file path."}},
                ["path"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Opening the requested file"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Opened the requested file"),
                ),
            ),
            _tool(
                "show_open_project_dialog",
                "Show REasy's project library dialog only when the user explicitly asks for it. Never use it to satisfy a file-update prerequisite.",
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Opening the project library"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Opened the project library"),
                ),
            ),
            _tool(
                "list_projects",
                "Search REasy's project library directly without opening its UI.",
                {
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive name, path, description, or author filter.",
                    },
                    "game": {
                        "type": "string",
                        "description": "Optional exact game identifier such as RE4 or DMC5.",
                    },
                },
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Looking through your project library"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Checked your project library"),
                ),
            ),
            _tool(
                "open_project",
                "Directly open or switch to an existing REasy project without showing the project library. Never use it to satisfy a file-update prerequisite; the user must open that project themselves.",
                {
                    "project": {
                        "type": "string",
                        "description": "Exact project name, library-relative path, or path returned by list_projects.",
                    },
                    "game": {
                        "type": "string",
                        "description": "Optional game identifier used to disambiguate duplicate names.",
                    },
                },
                ["project"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Opening the requested project"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Opened the requested project"),
                ),
            ),
            _tool(
                "close_active_project",
                "Close the active project directly. REasy may ask about unsaved files.",
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Closing the active project"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Closed the active project"),
                ),
            ),
            _tool(
                "show_create_project_dialog",
                "Show REasy's guided new-project dialog only when explicitly requested. Never use it to satisfy a file-update prerequisite.",
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Opening project creation"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Opened project creation"),
                ),
            ),
            _tool(
                "show_project_settings",
                "Show settings for the active project.",
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Opening project settings"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Opened project settings"),
                ),
            ),
            _tool(
                "export_mod",
                "Start export of the active project. The UI may ask for configuration or downloads.",
                {"format": {"type": "string", "enum": ["fluffy_zip", "pak"]}},
                ["format"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Preparing the mod export"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Started the mod export"),
                ),
                persistent=True,
            ),
            _tool(
                "save_active_file",
                "Save the active file through REasy. For a PAK-backed tab this routes to a project copy or Save As; it never writes into the PAK archive. Use only when the user explicitly asks to save.",
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Saving the active file"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Saved the active file"),
                ),
                persistent=True,
            ),
            _tool(
                "save_all_modified_files",
                "Save every modified file across all open REasy projects, scratch tabs, detached tabs, and scene-managed editors. Use only when the user explicitly asks to save all edited files.",
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Saving all modified files"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Saved all modified files"),
                ),
                persistent=True,
            ),
            _tool(
                "inspect_mdf",
                "Batch-inspect one or more open MDF editors directly from their in-memory data without switching tabs. Compact output is the default when neither materials nor all_materials is provided and omits parameter values, so it cannot establish that files are identical. Provide several materials to read them all in one call; use compare_mdf_files for exact comparison.",
                {
                    "tabs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 16,
                        "description": "Optional open-tab IDs, paths, or unambiguous titles. Omit to inspect the active MDF.",
                    },
                    "all_open": {
                        "type": "boolean",
                        "description": "Inspect every currently open MDF. Do not combine with tabs.",
                    },
                    "materials": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "integer"},
                            ]
                        },
                        "maxItems": 128,
                        "description": "Optional material names or zero-based indices to inspect in full across every target MDF.",
                    },
                    "all_materials": {
                        "type": "boolean",
                        "description": "Return every material in full. Do not combine with materials; omit both detail selectors when full data is unnecessary.",
                    },
                },
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Inspecting open MDF data"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Inspected open MDF data"),
                ),
                capability=MDF_CAPABILITY,
            ),
            _tool(
                "compare_mdf_files",
                "Read-only exact comparison of two open MDFs using their current in-memory data. Compares supported header and material values, including numeric parameter values, with table rows matched by identity. Returns only differences and never activates, copies, or edits either tab. Omit left and right when exactly two MDFs are open; otherwise provide both tab references. Use this—not inspect_mdf summaries—when the user asks whether two MDFs differ.",
                {
                    "left": {
                        "type": "string",
                        "description": "Optional open-tab ID, unambiguous title, or open path for the first MDF.",
                    },
                    "right": {
                        "type": "string",
                        "description": "Optional open-tab ID, unambiguous title, or open path for the second MDF.",
                    },
                    "materials": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 128,
                        "description": "Optional material names to compare. Omit to compare every material.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Maximum difference details to return. The total count is always reported.",
                    },
                },
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Comparing open MDF files"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Compared open MDF files"),
                ),
                capability=MDF_CAPABILITY,
            ),
            _tool(
                "copy_mdf_values",
                "Safely copy values shared by two MDFs through the destination's visible editor. Direction is literal: values flow from source to destination, and REasy asks the user to confirm that resolved direction before applying changes. Rows are matched by material and parameter/buffer name, texture type, or shader-LOD index; destination-only materials and rows are preserved and reported as differences. Destination parameter structure is also preserved. Optional selectors limit the copy. Source-only rows are reported and are added only when include_source_only is explicitly true. A PAK-backed destination is edited directly in memory without extraction, keeps its MDF version, and stays unsaved.",
                {
                    "source": {
                        "type": "string",
                        "description": "The FROM side: open-tab ID, unambiguous title, or exact filesystem path for the MDF whose values should be read and copied. Filesystem paths are opened when needed.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "The TO side: open-tab ID, unambiguous title, or exact filesystem path for the MDF that should be modified to receive the values. Use the tab ID returned by open_pak_file for a PAK-backed destination.",
                    },
                    "materials": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 128,
                        "description": "Optional material names to copy. Omit to process every source material.",
                    },
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": sorted(_MDF_COPY_SECTIONS),
                        },
                        "maxItems": len(_MDF_COPY_SECTIONS),
                        "description": "Optional MDF sections to copy. Omit to process every section.",
                    },
                    "values": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "integer"},
                            ]
                        },
                        "maxItems": 256,
                        "description": "Optional field or row identities within the selected sections: overview/flag names, texture types, parameter or GPU-buffer names, header fields, or shader-LOD indices.",
                    },
                    "include_source_only": {
                        "type": "boolean",
                        "description": "Also add source materials and rows missing from the destination. Default false; use true only when the user explicitly requests additions that are absent from the destination, not merely when they say 'all values'. Destination-only data is never deleted.",
                    },
                },
                ["source", "destination"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Copying MDF values"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Copied MDF values"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                ui_edit=True,
                result_card=True,
                unsaved_result=True,
            ),
            _tool(
                "migrate_mdf_files",
                "Run an explicitly requested three-way MDF migration for one or more files. Requires the user to first open the relevant game project with its PAK files loaded. Each job compares an old original with its modded copy and applies only the mod's changes to a latest-update destination through the MDF UI. Do not use this for an ordinary two-file migration.",
                {
                    "jobs": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_original": {
                                    "type": "string",
                                    "description": "Tab ID, open title, or exact filesystem path for the unmodified MDF from the old game update. Filesystem paths are opened when needed.",
                                },
                                "modded": {
                                    "type": "string",
                                    "description": "Tab ID, open title, or exact filesystem path for the outdated modded MDF. Filesystem paths are opened when needed.",
                                },
                                "destination": {
                                    "type": "string",
                                    "description": "Tab ID, open title, or exact filesystem path for the latest-update MDF that should receive the mod changes. A PAK-backed tab is a valid unsaved in-memory destination.",
                                },
                                "label": {
                                    "type": "string",
                                    "description": "Optional short label used in the migration result.",
                                },
                            },
                            "required": [
                                "old_original",
                                "modded",
                                "destination",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "conflict_policy": {
                        "type": "string",
                        "enum": ["skip", "abort", "prefer_mod"],
                        "description": "skip applies safe changes and reports conflicts; abort applies nothing if any conflict exists; prefer_mod overwrites conflicts with mod values only when the user explicitly requests it.",
                    },
                },
                ["jobs"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Migrating MDF files"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Migrated MDF files"),
                ),
                capability=MDF_THREE_WAY_CAPABILITY,
                incremental=True,
                ui_edit=True,
                result_card=True,
                unsaved_result=True,
            ),
            _tool(
                "update_mod_folder",
                "Create an updated mod from an external mod folder and a folder of latest extracted originals. Requires the user to first open the relevant game project with its PAK files loaded. Recursively match versioned MDFs by relative path, use each latest original as the output base, overlay source MDF values through the UI, and copy every other mod file unchanged. This writes a separate output folder; use only when the user explicitly requests it.",
                {
                    "mod_folder": {
                        "type": "string",
                        "description": "Folder containing the existing mod files.",
                    },
                    "originals_folder": {
                        "type": "string",
                        "description": "Folder containing the latest extracted original files.",
                    },
                    "output_folder": {
                        "type": "string",
                        "description": "Optional new or empty output folder. Omit to create a uniquely named sibling of the mod folder.",
                    },
                    "include_source_only": {
                        "type": "boolean",
                        "description": "Also add mod MDF materials and rows absent from the latest originals. Default false; use true only when the user explicitly requests additions absent from the originals, not merely when they say 'all values'.",
                    },
                },
                ["mod_folder", "originals_folder"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Building the updated mod folder"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Built the updated mod folder"),
                ),
                capability=MDF_FOLDER_UPDATE_CAPABILITY,
                incremental=True,
                ui_edit=True,
                persistent=True,
            ),
            _tool(
                "batch_edit_files",
                "Apply ordered registered edit tools to multiple open files in one call. Every action must name its exact destination tab ID. A failed action is reported without stopping later actions.",
                _batch_edit_schema_properties,
                ["actions"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Editing files"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Edited files"),
                ),
                capability=(
                    MDF_EDIT_CAPABILITY,
                    MSG_EDIT_CAPABILITY,
                    RSZ_EDIT_CAPABILITY,
                ),
                incremental=True,
                ui_edit=True,
                result_card=True,
                unsaved_result=True,
            ),
            _tool(
                "select_mdf_material",
                "Select a material and visible MDF editor section without changing data.",
                {
                    "material": _MATERIAL,
                    "section": {
                        "type": "string",
                        "enum": ["overview", "textures", "parameters", "gpu_buffers", "shader_lods"],
                    },
                },
                ["material"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Selecting the requested material"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Selected the requested material"),
                ),
                capability=MDF_CAPABILITY,
                handler="_select_mdf_material_tool",
            ),
            _tool(
                "edit_mdf_header",
                "Edit visible MDF header controls. Include only fields that should change.",
                {
                    "changes": {
                        "type": "object",
                        "properties": {
                            "version": {"type": "integer"},
                            "meshlet_material": {"type": "boolean"},
                        },
                        "additionalProperties": False,
                    }
                },
                ["changes"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Updating the MDF header"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Updated the MDF header"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "add_mdf_material",
                "Add a blank material through the MDF UI and give it a unique name.",
                {"name": {"type": "string"}},
                ["name"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Adding a material"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Added a material"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "delete_mdf_material",
                "Delete one material through the MDF UI.",
                {"material": _MATERIAL},
                ["material"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Deleting a material"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Deleted a material"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "edit_mdf_overview",
                "Edit visible overview fields for one material. Include only fields that should change.",
                {
                    "material": _MATERIAL,
                    "changes": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "mmtr_path": {"type": "string"},
                            "shader_type": {
                                "description": "Shader name shown in REasy or its zero-based numeric index.",
                                "anyOf": [{"type": "string"}, {"type": "integer"}],
                            },
                            "bake_texture_array_size": {"type": "integer", "minimum": 0},
                            "unknown_64": {
                                "description": "Integer or 0x-prefixed integer text.",
                                "anyOf": [{"type": "string"}, {"type": "integer"}],
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                ["material", "changes"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Updating material details"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Updated material details"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "edit_mdf_flags",
                "Edit named material flags and numeric flag fields through visible controls. Include only fields that should change.",
                {
                    "material": _MATERIAL,
                    "changes": {
                        "type": "object",
                        "description": "Use exact boolean flag names returned by inspect_mdf. Numeric fields are defined below.",
                        "properties": {
                            "tessellation": {"type": "integer"},
                            "phong": {"type": "integer", "minimum": 0, "maximum": 255},
                            "transparent_priority_bias": {"type": "integer", "minimum": -128, "maximum": 127},
                        },
                        "additionalProperties": {"type": "boolean"},
                    },
                },
                ["material", "changes"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Updating material flags"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Updated material flags"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "upsert_mdf_texture",
                "Append or replace one texture row through the visible Textures table.",
                {
                    "material": _MATERIAL,
                    "index": _INDEX,
                    "texture_type": {"type": "string"},
                    "texture_path": {"type": "string"},
                    "locked": {"type": "boolean"},
                },
                ["material", "index", "texture_type", "texture_path", "locked"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Updating a texture"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Updated a texture"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "delete_mdf_texture",
                "Delete one texture row through the visible Textures table.",
                {"material": _MATERIAL, "index": {"type": "integer", "minimum": 0}},
                ["material", "index"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Removing a texture"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Removed a texture"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "upsert_mdf_parameter",
                "Append or replace one parameter row through the visible Parameters table.",
                {
                    "material": _MATERIAL,
                    "index": _INDEX,
                    "name": {"type": "string"},
                    "component_count": {"type": "integer", "minimum": 1, "maximum": 4},
                    "values": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "One to four values; the first component_count values are used.",
                    },
                    "locked": {"type": "boolean"},
                },
                ["material", "index", "name", "component_count", "values", "locked"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Updating a parameter"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Updated a parameter"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "delete_mdf_parameter",
                "Delete one parameter row through the visible Parameters table.",
                {"material": _MATERIAL, "index": {"type": "integer", "minimum": 0}},
                ["material", "index"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Removing a parameter"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Removed a parameter"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "upsert_mdf_gpu_buffer",
                "Append or replace one GPU-buffer name/data row (MDF version 19+).",
                {
                    "material": _MATERIAL,
                    "index": _INDEX,
                    "name": {"type": "string"},
                    "data": {"type": "string"},
                },
                ["material", "index", "name", "data"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Updating a GPU buffer"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Updated a GPU buffer"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "delete_mdf_gpu_buffer",
                "Delete one GPU-buffer row (MDF version 19+).",
                {"material": _MATERIAL, "index": {"type": "integer", "minimum": 0}},
                ["material", "index"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Removing a GPU buffer"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Removed a GPU buffer"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "upsert_mdf_shader_lod",
                "Append or replace one shader-LOD redirect row (MDF version 31+).",
                {
                    "material": _MATERIAL,
                    "index": _INDEX,
                    "texture_table": {"type": "array", "items": {"type": "integer"}},
                    "byte_buffer_table": {"type": "array", "items": {"type": "integer"}},
                },
                ["material", "index", "texture_table", "byte_buffer_table"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Updating a shader LOD"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Updated a shader LOD"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
            _tool(
                "delete_mdf_shader_lod",
                "Delete one shader-LOD redirect row (MDF version 31+).",
                {"material": _MATERIAL, "index": {"type": "integer", "minimum": 0}},
                ["material", "index"],
                activity=(
                    QT_TRANSLATE_NOOP("AiChatDock", "Removing a shader LOD"),
                    QT_TRANSLATE_NOOP("AiChatDock", "Removed a shader LOD"),
                ),
                capability=MDF_EDIT_CAPABILITY,
                mutation=True,
            ),
        )
        return (
            *definitions,
            *file_tool_definitions(),
            *msg_tool_definitions(),
            *rsz_tool_definitions(),
        )

    @classmethod
    def tool_definitions(cls) -> tuple[AiToolDefinition, ...]:
        if cls._tool_definitions_cache is None:
            cls._tool_definitions_cache = cls._build_tool_definitions()
        return cls._tool_definitions_cache

    @classmethod
    def tool_registry(cls) -> dict[str, AiToolDefinition]:
        if cls._tool_registry_cache is None:
            definitions = cls.tool_definitions()
            registry = {
                definition.name: definition
                for definition in definitions
            }
            if len(registry) != len(definitions):
                raise RuntimeError("Duplicate AI tool definition.")
            cls._tool_registry_cache = registry
        return cls._tool_registry_cache

    @classmethod
    def tool_definition(cls, name: str) -> AiToolDefinition | None:
        return cls.tool_registry().get(name)

    @classmethod
    def mutation_tool_names(cls) -> tuple[str, ...]:
        return tuple(
            sorted(
                definition.name
                for definition in cls.tool_definitions()
                if definition.mutation
            )
        )

    @classmethod
    def schemas(
        cls,
        capabilities: Iterable[str] | None = None,
        blocked_capabilities: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        blocked = frozenset(blocked_capabilities)
        unknown_blocked = blocked.difference(_CAPABILITY_PROMPTS)
        if unknown_blocked:
            raise ValueError(
                f"Unknown blocked AI capabilities: {sorted(unknown_blocked)}"
            )
        available_capabilities = frozenset(_CAPABILITY_PROMPTS).difference(
            blocked
        )
        enabled = (
            frozenset(_CAPABILITY_PROMPTS)
            if capabilities is None
            else _validate_capabilities(capabilities)
        ).difference(blocked)
        visible_definitions = tuple(
            definition
            for definition in cls.tool_definitions()
            if definition.is_enabled(enabled)
        )
        context = ToolSchemaContext(
            available_capabilities=available_capabilities,
            mutation_tool_names=tuple(
                sorted(
                    definition.name
                    for definition in visible_definitions
                    if definition.mutation
                )
            ),
        )
        return [
            definition.schema(context)
            for definition in visible_definitions
        ]

    @staticmethod
    def _decode_tool_arguments(arguments_json: str) -> dict[str, Any]:
        arguments = json.loads(arguments_json or "{}")
        if not isinstance(arguments, dict):
            raise AssistantToolError(
                _tr("Tool arguments must be a JSON object.")
            )
        return arguments

    @staticmethod
    def _json_tool_result(*, result: Any = None, error: Exception | None = None):
        payload = (
            {"success": False, "error": str(error)}
            if error is not None
            else {"success": True, "result": result}
        )
        return json.dumps(payload, ensure_ascii=False)

    def _tool_action_summary(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[str, str]:
        definition = self.tool_definition(name)
        summary_handler = (
            definition.confirmation_handler_name
            if definition is not None
            else None
        )
        if summary_handler is None:
            raise AssistantToolError(_tr("Unknown tool: {name}", name=name))
        return getattr(self, summary_handler)(arguments)

    @staticmethod
    def _summarize_add_pak_file_to_project(
        arguments: dict[str, Any],
    ) -> tuple[str, str]:
        return (
            _tr(_ADD_PAK_FILE_ACTION),
            _tr(_PAK_PATH_DETAIL, path=arguments.get("path", "")),
        )

    @staticmethod
    def _summarize_export_mod(
        arguments: dict[str, Any],
    ) -> tuple[str, str]:
        return (
            _tr(_EXPORT_MOD_ACTION),
            _tr(
                _EXPORT_FORMAT_DETAIL,
                format=arguments.get("format", ""),
            ),
        )

    def _summarize_save_active_file(
        self,
        _arguments: dict[str, Any],
    ) -> tuple[str, str]:
        tab = self.app.get_active_tab()
        path = (
            getattr(tab, "filename", "")
            or getattr(tab, "source_path", "")
            or _tr(_ACTIVE_FILE)
        )
        return (
            _tr(_SAVE_ACTIVE_FILE_ACTION),
            _tr(_FILE_DETAIL, path=path),
        )

    @staticmethod
    def _summarize_save_all_modified_files(
        _arguments: dict[str, Any],
    ) -> tuple[str, str]:
        return (
            _tr(_SAVE_ALL_FILES_ACTION),
            _tr(_ALL_MODIFIED_FILES_DETAIL),
        )

    @staticmethod
    def _summarize_update_mod_folder(
        arguments: dict[str, Any],
    ) -> tuple[str, str]:
        output = arguments.get("output_folder") or _tr(_AUTOMATIC_OUTPUT)
        return (
            _tr(_UPDATE_MOD_FOLDER_ACTION),
            _tr(
                _MOD_FOLDER_DETAILS,
                mod_folder=arguments.get("mod_folder", ""),
                originals_folder=arguments.get("originals_folder", ""),
                output_folder=output,
            ),
        )

    def _show_tool_action_confirmation(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> AiChangeDecision:
        action, details = self._tool_action_summary(name, arguments)
        return self._action_policy.show_confirmation(
            _tr(_CONFIRM_AI_ACTION_TITLE),
            _tr(
                _CONFIRM_AI_ACTION_MESSAGE,
                action=action,
                details=details,
            ),
        )

    def authorize_tool_json(self, name: str, arguments_json: str) -> bool:
        """Ask before an  AI action writes persistent data."""

        definition = self.tool_definition(name)
        if definition is None or not definition.persistent:
            return True
        arguments = self._decode_tool_arguments(arguments_json)
        if name == "apply_file_operations":
            return self._authorize_file_operation_plan(arguments)
        if name in _UPDATE_PROJECT_TOOL_NAMES:
            self._require_update_project_paks()
        return self._action_policy.request(
            lambda: self._confirm_tool_action(name, arguments)
        )

    def execute_json(self, name: str, arguments_json: str) -> str:
        try:
            arguments = self._decode_tool_arguments(arguments_json)
            result = self.execute(name, arguments)
        except Exception as exc:
            return self._json_tool_result(error=exc)
        return self._json_tool_result(result=result)

    def begin_incremental_json(self, name: str, arguments_json: str):
        """Return a cooperative execution for long-running GUI-backed tools."""

        definition = self.tool_definition(name)
        handler_name = (
            definition.incremental_handler_name
            if definition is not None
            else None
        )
        if handler_name is None:
            return None
        handler = getattr(self, handler_name)

        def run():
            original_active_tab = (
                self.app.get_active_tab()
                if name == "update_mod_folder"
                else None
            )
            try:
                arguments = self._decode_tool_arguments(arguments_json)
                call_arguments = self._prepare_tool_call_arguments(
                    definition,
                    arguments,
                )
                try:
                    signature(handler).bind(**call_arguments)
                except TypeError as exc:
                    raise AssistantToolError(
                        _tr("Invalid arguments for tool: {name}", name=name)
                    ) from exc
                result = yield from handler(**call_arguments)
                result = self._attach_mutation_target(
                    definition,
                    result,
                )
            except Exception as exc:
                return self._json_tool_result(error=exc)
            finally:
                if original_active_tab is not None:
                    self._restore_batch_focus(original_active_tab)
            return self._json_tool_result(result=result)

        return run()

    @staticmethod
    def _run_incremental_steps(steps):
        while True:
            try:
                next(steps)
            except StopIteration as completed:
                return completed.value

    def _prepare_tool_call_arguments(
        self,
        definition: AiToolDefinition,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if not definition.mutation or "tab" not in arguments:
            return arguments
        call_arguments = dict(arguments)
        target = call_arguments.pop("tab")
        self._activate_open_tab(str(target))
        return call_arguments

    def _attach_mutation_target(
        self,
        definition: AiToolDefinition,
        result: Any,
    ) -> Any:
        if not definition.mutation:
            return result
        result = dict(result)
        payload = self._tab_target_payload(self.app.get_active_tab())
        result["target"] = {
            "tab_id": payload["id"],
            "file": self._file_result_path(payload),
            "pak_backed": bool(payload.get("pak_backed")),
        }
        return result

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        definition = self.tool_definition(name)
        if definition is None:
            raise AssistantToolError(_tr("Unknown tool: {name}", name=name))
        if name in _PROJECT_OPEN_TOOL_NAMES:
            self._require_project_open_action_allowed()
        handler = getattr(self, definition.handler_name)
        call_arguments = self._prepare_tool_call_arguments(
            definition,
            arguments,
        )
        try:
            signature(handler).bind(**call_arguments)
        except TypeError as exc:
            raise AssistantToolError(
                _tr("Invalid arguments for tool: {name}", name=name)
            ) from exc
        result = handler(**call_arguments)
        return self._attach_mutation_target(definition, result)

    def _enable_ai_capability(self, capability: str) -> dict[str, Any]:
        name = str(capability or "").strip().casefold()
        if name not in _CAPABILITY_PROMPTS:
            raise AssistantToolError(
                _tr(
                    "Unknown AI capability: {capability}",
                    capability=capability,
                )
            )
        if name in self._blocked_capabilities:
            raise AssistantToolError(
                _tr(
                    _BLOCKED_CAPABILITY_ERRORS[name],
                    capability=name,
                )
            )
        additions = {name}
        if name in {
            MDF_EDIT_CAPABILITY,
            MDF_THREE_WAY_CAPABILITY,
            MDF_FOLDER_UPDATE_CAPABILITY,
        }:
            additions.add(MDF_CAPABILITY)
        elif name == MSG_EDIT_CAPABILITY:
            additions.add(MSG_CAPABILITY)
        elif name == RSZ_EDIT_CAPABILITY:
            additions.add(RSZ_CAPABILITY)
        self._enabled_capabilities = set(
            _validate_capabilities(
                self._enabled_capabilities.union(additions)
            )
        )
        return {
            "enabled": name,
            "available_next_round": True,
        }

    @staticmethod
    def _new_file_update_report_collector() -> UpdateReportCollector:
        return UpdateReportCollector()

    def _migrate_reported_file_jobs_steps(
        self,
        jobs: list[dict[str, Any]],
        strategy,
        project_context: dict[str, Any],
        *,
        publish_mode: str = "separate",
        backup_folder: str = "",
    ):
        open_files = self._open_disk_files()
        target_fields = {
            "separate": ("output_file",),
            "replace_outdated_with_backup": (
                "outdated_file",
                "output_file",
            ),
            "replace_latest_with_backup": ("latest_file",),
        }.get(str(publish_mode or "separate").strip().casefold(), ())
        for job in jobs if isinstance(jobs, list) else ():
            if not isinstance(job, dict):
                continue
            for field in ("outdated_file", "latest_file", "output_file"):
                raw_path = str(job.get(field) or "").strip()
                if not raw_path:
                    continue
                opened = open_files.get(self._disk_path_key(raw_path))
                if opened is not None and (
                    field in target_fields or opened.get("modified")
                ):
                    state = "modified" if opened.get("modified") else "open"
                    raise AssistantToolError(
                        _tr(
                            "Migration refuses to use an {state} editor file for this disk operation: {path}",
                            state=state,
                            path=raw_path,
                        )
                    )
        collector = self._new_file_update_report_collector()
        result = yield from migrate_file_jobs_steps(
            jobs,
            strategy,
            report_sink=collector,
            publish_mode=publish_mode,
            backup_folder=backup_folder,
        )
        format_name = str(strategy.format_name).casefold()
        format_label = format_name.upper()
        single_job = len(jobs) == 1 and isinstance(jobs[0], dict)
        source = (
            str(jobs[0].get("outdated_file", ""))
            if single_job
            else f"{len(jobs)} explicit {format_label} files"
        )
        published_jobs = result.get("jobs") or []
        output = (
            str(published_jobs[0].get("output_file", ""))
            if len(published_jobs) == 1
            else f"{len(collector.files)} output {format_label} files"
        )
        return self._attach_file_update_report(
            result,
            format_name=format_name,
            operation="file_migration",
            project_context=project_context,
            source=source,
            output=output,
            collector=collector,
        )

    def _record_file_update_report(
        self,
        *,
        format_name: str,
        operation: str,
        project: str,
        game: str,
        source: str,
        output: str,
        result: dict[str, Any],
        collector: UpdateReportCollector,
        decision_lookup: dict[
            tuple[str, str], dict[str, Any]
        ] | None = None,
    ) -> FileUpdateReport:
        summary = {
            key: copy.deepcopy(value)
            for key, value in result.items()
            if key not in {"jobs", "ai_decisions"}
        }
        report = FileUpdateReport.create(
            format_name=format_name,
            operation=operation,
            project=project,
            game=game,
            source=source,
            output=output,
            result=summary,
            files=collector.files,
            decision_lookup=decision_lookup,
        )
        self._file_update_reports[report.update_id] = report
        while len(self._file_update_reports) > 16:
            self._file_update_reports.pop(next(iter(self._file_update_reports)))
        return report

    def _attach_file_update_report(
        self,
        result: dict[str, Any],
        *,
        format_name: str,
        operation: str,
        project_context: dict[str, Any],
        source: str,
        output: str,
        collector: UpdateReportCollector,
        decision_lookup: dict[
            tuple[str, str], dict[str, Any]
        ] | None = None,
    ) -> dict[str, Any]:
        report = self._record_file_update_report(
            format_name=format_name,
            operation=operation,
            project=project_context["project"],
            game=project_context["game"],
            source=source,
            output=output,
            result=result,
            collector=collector,
            decision_lookup=decision_lookup,
        )
        result.update(
            {
                "update_report_id": report.update_id,
                "update_report_available": True,
                "update_report_sections": report.summary()[
                    "available_sections"
                ],
            }
        )
        return result

    def _file_update_report(self, update_id: str = "") -> FileUpdateReport:
        key = str(update_id or "").strip()
        if key:
            report = self._file_update_reports.get(key)
        else:
            report = (
                next(reversed(self._file_update_reports.values()))
                if self._file_update_reports
                else None
            )
        if report is None:
            if key:
                raise AssistantToolError(
                    _tr(
                        "File update report was not found: {update_id}.",
                        update_id=key,
                    )
                )
            raise AssistantToolError(
                _tr("No completed RSZ or MSG file update report is available.")
            )
        return report

    def _inspect_file_update_report(
        self,
        update_id: str = "",
        section: str = "summary",
        file: str = "",
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        section = str(section or "summary").strip().casefold()
        allowed = {
            "summary",
            "imported",
            "new_elements",
            "kept_latest",
            "unresolved",
            "files",
            "updates",
        }
        if section not in allowed:
            raise AssistantToolError(
                _tr("Unknown file update report section: {section}", section=section)
            )
        offset = self._integer(offset, "offset", 0, 1_000_000)
        limit = self._integer(limit, "limit", 1, 200)
        if section == "updates":
            reports = list(reversed(self._file_update_reports.values()))
            needle = " ".join(
                value.strip() for value in (str(file or ""), str(query or ""))
                if value.strip()
            ).casefold()
            summaries = [report.summary() for report in reports]
            if needle:
                summaries = [
                    item
                    for item in summaries
                    if needle
                    in json.dumps(
                        item,
                        ensure_ascii=False,
                        default=str,
                        sort_keys=True,
                    ).casefold()
                ]
            page = summaries[offset : offset + limit]
            return {
                "section": "updates",
                "offset": offset,
                "limit": limit,
                "total": len(summaries),
                "updates": page,
                "next_offset": (
                    offset + len(page)
                    if offset + len(page) < len(summaries)
                    else None
                ),
            }
        report = self._file_update_report(update_id)
        return report.inspect(
            section=section,
            file_filter=file,
            query=query,
            offset=offset,
            limit=limit,
        )

    def _tab_id(self, tab) -> str:
        tab_id = getattr(tab, "_reasy_ai_tab_id", "")
        if tab_id:
            return tab_id
        tab_id = f"tab-{self._next_tab_id}"
        self._next_tab_id += 1
        try:
            setattr(tab, "_reasy_ai_tab_id", tab_id)
        except (AttributeError, TypeError):
            return f"tab-{id(tab):x}"
        return tab_id

    @staticmethod
    def _tab_title(tab) -> str:
        title = str(getattr(tab, "title", "") or "").strip()
        filename = str(getattr(tab, "filename", "") or "").strip()
        return title or (Path(filename).name if filename else "Untitled")

    @staticmethod
    def _resolved_disk_file_path(
        filename: str,
        *,
        project: str | None = None,
        pak_backed: bool = False,
    ) -> str | None:
        """Return an absolute disk path without inventing one for a PAK entry."""

        raw = str(filename or "").strip()
        if not raw:
            return None
        candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
        if pak_backed and not candidate.is_absolute():
            return None
        if not candidate.is_absolute() and project:
            candidate = Path(project) / candidate
        try:
            return str(candidate.resolve())
        except (OSError, RuntimeError):
            return str(candidate.absolute())

    @staticmethod
    def _file_result_path(payload: dict[str, Any]) -> str | None:
        """Return the exact usable identity for a disk or PAK-backed file."""

        return (
            payload.get("source_path")
            or payload.get("path")
            or payload.get("title")
        )

    def _open_tab_records(self):
        manager = self.app.project_workspace.sessions
        scratch = manager.get(None)
        sessions = [scratch, *manager.project_sessions()]
        active_session = manager.get(manager.active_key)
        active_tab = self.app.get_active_tab()
        records = []
        seen = set()

        def append(tab, session) -> None:
            if tab is None or id(tab) in seen:
                return
            seen.add(id(tab))
            filename = str(getattr(tab, "filename", "") or "")
            source_path = str(getattr(tab, "pak_source_path", "") or "")
            viewer = getattr(tab, "viewer", None)
            handler = getattr(tab, "handler", None)
            editor = (
                type(viewer).__name__
                if viewer is not None
                else type(handler).__name__
                if handler is not None
                else type(tab).__name__
            )
            windows = manager.windows_for([tab])
            hidden = bool(getattr(tab, "_workspace_hidden", False))
            pak_backed = bool(source_path)
            project = getattr(session, "path", None)
            disk_path = self._resolved_disk_file_path(
                filename,
                project=project,
                pak_backed=pak_backed,
            )
            game = (
                getattr(session, "game", None)
                or getattr(handler, "game_version", None)
                or getattr(viewer, "game_version", None)
            )
            payload = {
                "id": self._tab_id(tab),
                "title": self._tab_title(tab),
                "path": disk_path,
                "source_path": source_path or None,
                "editor": editor,
                "modified": bool(getattr(tab, "modified", False)),
                "active": tab is active_tab,
                "detached": bool(windows),
                "activatable": not hidden,
                "editable_in_memory": not hidden,
                "pak_backed": pak_backed,
                "save_requires_copy": pak_backed,
                "workspace": "project" if project else "standalone",
                "project": project,
                "project_name": Path(project).name if project else None,
                "game": game,
                "project_active": bool(project and session is active_session),
            }
            if pak_backed:
                payload["can_write_back_to_pak"] = False
            records.append((tab, session, payload))

        for session in sessions:
            if session is not None:
                for tab in session.tabs:
                    append(tab, session)

        # Direct-open and detached tabs normally belong to the scratch session.
        # Include UI-owned tabs as a fallback so assistant access does not depend
        # on a project/session bookkeeping detail.
        tab_lookup = getattr(self.app, "tabs", {})
        extra_tabs = list(tab_lookup.values()) if hasattr(tab_lookup, "values") else []
        extra_tabs.append(active_tab)
        notebook = getattr(manager, "notebook", None)
        extra_tabs.extend(
            getattr(window, "file_tab", None)
            for window in getattr(notebook, "_floating_windows", ())
        )
        session_for_tab = getattr(manager, "session_for_tab", None)
        for tab in extra_tabs:
            session = session_for_tab(tab) if callable(session_for_tab) else None
            append(tab, session)
        return records

    def _get_reasy_context(self) -> dict[str, Any]:
        manager = self.app.project_workspace.sessions
        records = self._open_tab_records()
        active_tab = next(
            (payload for _tab, _session, payload in records if payload["active"]),
            None,
        )
        open_projects = []
        for session in manager.project_sessions():
            project_tabs = [
                payload
                for _tab, owner, payload in records
                if owner is session
            ]
            open_projects.append(
                {
                    "name": Path(session.path).name if session.path else "",
                    "path": session.path,
                    "game": session.game,
                    "active": session is manager.get(manager.active_key),
                    "tab_ids": [tab["id"] for tab in project_tabs],
                    "modified_tabs": sum(
                        1 for tab in project_tabs if tab["modified"]
                    ),
                }
            )
        return {
            "active_project": self.app.current_project,
            "active_game": self.app.current_game,
            "active_tab_id": active_tab["id"] if active_tab else None,
            "active_workspace": active_tab.get("workspace") if active_tab else None,
            "standalone_tab_count": sum(
                payload.get("workspace") == "standalone"
                for _tab, _session, payload in records
            ),
            "open_projects": open_projects,
            "open_tabs": [payload for _tab, _session, payload in records],
        }

    @staticmethod
    def _path_key(value: str) -> str:
        return os.path.normcase(
            os.path.abspath(os.path.expandvars(os.path.expanduser(value)))
        )

    def _resolve_open_tab(self, reference: str):
        requested = str(reference or "").strip()
        if not requested:
            raise AssistantToolError(_tr("tab must not be empty."))
        records = self._open_tab_records()
        folded = requested.casefold()
        matches = [
            record
            for record in records
            if record[2]["id"].casefold() == folded
        ]
        if not matches:
            requested_path = self._path_key(requested)
            for record in records:
                payload = record[2]
                candidates = {
                    str(payload["title"] or "").casefold(),
                    Path(str(payload["path"] or "")).name.casefold(),
                    Path(str(payload["source_path"] or "")).name.casefold(),
                }
                if folded in candidates:
                    matches.append(record)
                    continue
                for path_value in (payload["path"], payload["source_path"]):
                    if path_value and self._path_key(str(path_value)) == requested_path:
                        matches.append(record)
                        break
        if not matches:
            raise AssistantToolError(
                _tr(
                    "No open tab matched '{tab}'. Call get_reasy_context to inspect the live workspace.",
                    tab=requested,
                )
            )
        if len(matches) > 1:
            choices = ", ".join(
                f"{payload['id']} ({payload['title']})"
                for _tab, _session, payload in matches
            )
            raise AssistantToolError(
                _tr(
                    "Open tab '{tab}' is ambiguous: {choices}. Use its exact tab ID.",
                    tab=requested,
                    choices=choices,
                )
            )
        return matches[0]

    def _activate_open_tab(self, tab: str) -> dict[str, Any]:
        target, _session, payload = self._resolve_open_tab(tab)
        if not payload["activatable"]:
            raise AssistantToolError(
                _tr(
                    "That tab is currently managed by a scene and cannot be activated directly.",
                )
            )
        if not self._focus_open_tab(target):
            raise AssistantToolError(
                _tr(
                    "REasy could not activate open tab: {tab_id}",
                    tab_id=payload["id"],
                )
            )
        self._pulse_open_tab(target)
        refreshed = next(
            item
            for _tab, _owner, item in self._open_tab_records()
            if item["id"] == payload["id"]
        )
        return {
            "activated": refreshed,
            "reused_in_memory": True,
            "disk_reloaded": False,
        }

    def _focus_open_tab(self, tab) -> bool:
        if tab is None or getattr(tab, "_workspace_hidden", False):
            return False
        focus = getattr(self.app.project_workspace, "focus_open_tab", None)
        if callable(focus) and focus(tab):
            return True
        if tab is self.app.get_active_tab():
            return True

        manager = getattr(self.app.project_workspace, "sessions", None)
        notebook = getattr(manager, "notebook", None) or getattr(
            self.app, "notebook", None
        )
        widget = getattr(tab, "notebook_widget", None)
        if notebook is not None and widget is not None:
            try:
                index = notebook.indexOf(widget)
            except (AttributeError, RuntimeError):
                index = -1
            if index >= 0:
                notebook.setCurrentIndex(index)
                ensure_loaded = getattr(
                    getattr(tab, "preview", None), "ensure_loaded", None
                )
                if callable(ensure_loaded):
                    ensure_loaded()
                return True

        windows_for = getattr(manager, "windows_for", None)
        windows = windows_for([tab]) if callable(windows_for) else ()
        for window in windows:
            window.show()
            window.raise_()
            window.activateWindow()
        return bool(windows)

    def _active_project_root(self) -> Path:
        path = self.app.current_project
        if not path:
            raise AssistantToolError(_tr("No REasy project is active."))
        root = Path(path).resolve()
        if not root.is_dir():
            raise AssistantToolError(
                _tr("The active project folder no longer exists.")
            )
        return root

    @staticmethod
    def _project_payload(entry, active_project: str | None = None) -> dict[str, Any]:
        active = False
        if active_project:
            try:
                active = Path(active_project).resolve() == entry.path.resolve()
            except (OSError, RuntimeError):
                active = False
        return {
            "name": entry.name,
            "game": entry.game,
            "path": str(entry.path.resolve()),
            "description": entry.description,
            "author": entry.author,
            "version": entry.version,
            "source": entry.source,
            "active": active,
        }

    def _project_entries(self):
        try:
            return discover_projects(Path(PROJECTS_ROOT), GAMES)
        except OSError as exc:
            raise AssistantToolError(
                _tr(
                    "REasy could not read the project library: {error}",
                    error=exc,
                )
            ) from exc

    def _list_projects(self, query: str = "", game: str = "") -> dict[str, Any]:
        needle = str(query or "").strip().casefold()
        game_filter = str(game or "").strip().casefold()
        matches = []
        for entry in self._project_entries():
            if game_filter and entry.game.casefold() != game_filter:
                continue
            searchable = "\n".join(
                (
                    entry.name,
                    entry.game,
                    str(entry.path),
                    entry.description,
                    entry.author,
                )
            ).casefold()
            if needle and needle not in searchable:
                continue
            matches.append(
                self._project_payload(entry, self.app.current_project)
            )
        return {
            "projects_root": str(Path(PROJECTS_ROOT).resolve()),
            "projects": matches[:200],
            "count": len(matches),
            "truncated": len(matches) > 200,
        }

    def _resolve_project(self, project: str, game: str = ""):
        requested = str(project or "").strip()
        if not requested:
            raise AssistantToolError(_tr("project must not be empty."))

        game_filter = str(game or "").strip().casefold()
        entries = [
            entry
            for entry in self._project_entries()
            if not game_filter or entry.game.casefold() == game_filter
        ]
        requested_key = requested.replace("\\", "/").rstrip("/").casefold()
        if not requested_key:
            raise AssistantToolError(
                _tr("project must identify a project name or path.")
            )

        def identifiers(entry) -> set[str]:
            values = {
                entry.name,
                entry.path.name,
                str(entry.path),
            }
            try:
                values.add(entry.path.relative_to(PROJECTS_ROOT).as_posix())
            except ValueError:
                pass
            return {
                value.replace("\\", "/").rstrip("/").casefold()
                for value in values
            }

        exact = [entry for entry in entries if requested_key in identifiers(entry)]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            matches = exact
        else:
            matches = [
                entry
                for entry in entries
                if any(requested_key in value for value in identifiers(entry))
            ]
        if not matches:
            raise AssistantToolError(
                _tr(
                    "No REasy project matched '{project}'. "
                    "Call list_projects to inspect the project library.",
                    project=requested,
                )
            )
        if len(matches) > 1:
            choices = ", ".join(
                f"{entry.game}/{entry.name}" for entry in matches[:10]
            )
            raise AssistantToolError(
                _tr(
                    "Project '{project}' is ambiguous: {choices}. Provide a "
                    "game or exact path from list_projects.",
                    project=requested,
                    choices=choices,
                )
            )
        return matches[0]

    def _open_project(self, project: str, game: str = "") -> dict[str, Any]:
        entry = self._resolve_project(project, game)
        if not self.app.project_workspace.open(entry.path, entry.game):
            raise AssistantToolError(
                _tr("REasy could not open project: {path}", path=entry.path)
            )
        self._pulse_project_label()
        return {
            "opened": self._project_payload(entry, str(entry.path)),
            "dialog_shown": False,
        }

    def _close_active_project(self) -> dict[str, Any]:
        project = str(self._active_project_root())
        if not self.app.project_workspace.close():
            raise AssistantToolError(
                _tr(
                    "The active project was not closed. It may be required by a "
                    "scene, or the user may have cancelled an unsaved-file prompt."
                )
            )
        self._pulse_project_label()
        return {"closed": project}

    def _list_project_files(
        self,
        query: str = "",
        extension: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        limit = self._integer(limit, "limit", 1, 500)
        root, files, truncated = self._scan_project_files(
            query,
            extension=extension,
            limit=limit,
        )
        return {
            "project": str(root),
            "files": files,
            "count": len(files),
            "truncated": truncated,
        }

    @staticmethod
    def _matches_project_extension(filename: str, extension: str) -> bool:

        name = filename.casefold()
        suffix = extension.casefold()
        return name.endswith(suffix) or bool(
            re.search(rf"{re.escape(suffix)}\.\d+$", name)
        )

    def _scan_project_files(
        self,
        query: str = "",
        *,
        extension: str = "",
        limit: int,
    ) -> tuple[Path, list[dict[str, str]], bool]:
        root = self._active_project_root()
        needle = str(query or "").strip().casefold()
        suffix = str(extension or "").strip().casefold()
        files: list[dict[str, str]] = []
        truncated = False
        try:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                if needle and needle not in relative.casefold():
                    continue
                if suffix and not self._matches_project_extension(
                    path.name,
                    suffix,
                ):
                    continue
                if len(files) >= limit:
                    truncated = True
                    break
                files.append(
                    {
                        "path": str(path),
                        "project_relative_path": relative,
                    }
                )
        except OSError as exc:
            raise AssistantToolError(
                _tr(
                    "REasy could not scan the active project: {error}",
                    error=exc,
                )
            ) from exc
        files.sort(key=lambda item: item["project_relative_path"].casefold())
        return root, files, truncated

    def _open_project_file(self, path: str) -> dict[str, Any]:
        root = self._active_project_root()
        requested = str(path or "").strip()
        if not requested:
            raise AssistantToolError(_tr("path must not be empty."))
        candidate = Path(os.path.expandvars(os.path.expanduser(requested)))
        target = (
            candidate if candidate.is_absolute() else root / candidate
        ).resolve()
        if not target.is_relative_to(root):
            raise AssistantToolError(
                _tr("Project file paths must stay inside the active project.")
            )
        if not target.is_file():
            raise AssistantToolError(
                _tr("Project file does not exist: {path}", path=target)
            )
        if not self.app._open_path(str(target)):
            raise AssistantToolError(_tr("REasy could not open: {path}", path=target))
        self._pulse_open_tab(self.app.get_active_tab())
        return {
            "opened": str(target),
            "project_relative_path": target.relative_to(root).as_posix(),
        }

    def _loaded_project_pak_state(self):
        root = self._active_project_root()
        dock = self.app.proj_dock
        selected = tuple(getattr(dock, "_pak_selected_paks", None) or ())
        if not selected:
            raise AssistantToolError(
                _tr(
                    "No game PAKs are configured and scanned for the active project."
                )
            )
        paths = tuple(
            getattr(dock, "_pak_all_paths", None)
            or getattr(dock, "_pak_base_paths", None)
            or ()
        )
        if not paths:
            raise AssistantToolError(
                _tr(
                    "No PAK file list is loaded. Configure the PAK source and "
                    ".list file in the Project Browser first."
                )
            )
        return root, dock, selected, paths

    def _require_update_project_paks(self) -> dict[str, Any]:
        if not getattr(self.app, "current_project", None):
            self._update_waiting_for_user_project = True
            raise AssistantToolError(
                _tr(
                    "This file update cannot continue. The user must open the relevant game project themselves and load that game's PAK files in the Project Browser; the AI assistant will not open or choose a project for this prerequisite."
                )
            )
        root, dock, selected, paths = self._loaded_project_pak_state()
        dock_project = str(getattr(dock, "project_dir", None) or "").strip()
        if dock_project and self._path_key(dock_project) != self._path_key(str(root)):
            raise AssistantToolError(
                _tr(
                    "The loaded Project Browser PAK context belongs to a different project."
                )
            )
        game = str(
            getattr(self.app, "current_game", None)
            or getattr(dock, "current_game", None)
            or ""
        ).strip()
        if not game:
            raise AssistantToolError(
                _tr(
                    "File updates require an active project with a known game and that game's PAK files loaded."
                )
            )
        dock_game = str(getattr(dock, "current_game", None) or "").strip()
        if dock_game and dock_game.casefold() != game.casefold():
            raise AssistantToolError(
                _tr(
                    "The active project game does not match the loaded Project Browser PAK context."
                )
            )
        known_paths = []
        for path in paths:
            normalized = str(path or "").strip().replace("\\", "/")
            if (
                normalized
                and not normalized.endswith("/")
                and not normalized.casefold().startswith("__unknown/")
            ):
                known_paths.append(normalized)
        if not known_paths:
            raise AssistantToolError(
                _tr(
                    "The active project has no loaded PAK file-path index. Load the correct game's PAK files in the Project Browser first."
                )
            )
        return {
            "project": str(root),
            "game": game,
            "pak_count": len(selected),
            "indexed_path_count": len(known_paths),
        }

    def _require_project_open_action_allowed(self) -> None:
        if (
            self._update_waiting_for_user_project
            and not getattr(self.app, "current_project", None)
        ):
            raise AssistantToolError(
                _tr(
                    "This file update is waiting for the user to open the relevant game project themselves. The AI assistant cannot open, choose, or create a project for this prerequisite."
                )
            )

    def _configured_pak_paths(self) -> list[str]:
        _root, _dock, _selected, paths = self._loaded_project_pak_state()
        unique: dict[str, str] = {}
        for path in paths:
            normalized = str(path or "").strip().replace("\\", "/")
            if (
                not normalized
                or normalized.endswith("/")
                or normalized.casefold().startswith("__unknown/")
            ):
                continue
            unique.setdefault(normalized.casefold(), normalized)
        return sorted(unique.values(), key=str.casefold)

    def _list_pak_files(
        self,
        query: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        limit = self._integer(limit, "limit", 1, 500)
        needle = str(query or "").strip().casefold()
        matches = [
            path
            for path in self._configured_pak_paths()
            if not needle or needle in path.casefold()
        ]
        return {
            "files": matches[:limit],
            "count": len(matches),
            "truncated": len(matches) > limit,
        }

    def _resolve_pak_path(self, path: str) -> str:
        requested = str(path).strip().replace("\\", "/").casefold()
        matches = [
            candidate
            for candidate in self._configured_pak_paths()
            if candidate.replace("\\", "/").casefold() == requested
        ]
        if not matches:
            raise AssistantToolError(
                _tr(
                    "Path was not found in the configured PAK list: {path}",
                    path=path,
                )
            )
        return matches[0]

    def _read_configured_pak_file(self, path: str) -> bytes | None:
        root, dock, _selected, _paths = self._loaded_project_pak_state()
        read_file = getattr(dock, "read_project_pak_file", None)
        if callable(read_file):
            return read_file(str(root), path)
        ensure_reader = getattr(dock, "_ensure_project_pak_reader", None)
        reader = ensure_reader() if callable(ensure_reader) else None
        stream = reader.get_file(path) if reader is not None else None
        return stream.read() if stream is not None else None

    def _open_pak_file(self, path: str) -> dict[str, Any]:
        resolved = self._resolve_pak_path(path)
        if not self.app.proj_dock._open_pak_path_in_editor(resolved):
            raise AssistantToolError(
                _tr("REasy could not open the PAK file: {path}", path=resolved)
            )
        opened_record = next(
            (
                record
                for record in self._open_tab_records()
                if str(record[2].get("source_path") or "")
                .replace("\\", "/")
                .casefold()
                == resolved.replace("\\", "/").casefold()
            ),
            None,
        )
        opened = opened_record[2] if opened_record else None
        if opened_record:
            self._pulse_open_tab(opened_record[0])
        result = {
            "opened_pak_path": resolved,
            "tab_id": opened["id"] if opened else None,
            "can_write_back_to_pak": False,
            "save_requires_copy": True,
        }
        if opened:
            result.update(
                {
                    "editor": opened["editor"],
                    "editable_in_memory": opened["editable_in_memory"],
                }
            )
        return result

    def _add_pak_file_to_project(self, path: str) -> dict[str, Any]:
        resolved = self._resolve_pak_path(path)
        self.app.proj_dock._extract_from_paks_to_project([resolved])
        if not self._action_feedback.pulse_widget(
            getattr(self.app.proj_dock, "tree_proj", None)
        ):
            self._pulse_project_label()
        return {
            "requested_pak_path": resolved,
            "message": "REasy completed or displayed the extraction workflow.",
        }

    def _open_file(self, path: str) -> dict[str, Any]:
        requested = str(path or "").strip()
        target = Path(
            os.path.expandvars(os.path.expanduser(requested))
        ).resolve()
        if not target.is_file():
            raise AssistantToolError(_tr("File does not exist: {path}", path=target))

        target_key = self._path_key(str(target))

        def matching_records():
            return [
                record
                for record in self._open_tab_records()
                if record[2].get("path")
                and self._path_key(str(record[2]["path"])) == target_key
            ]

        existing = matching_records()
        if existing:
            active = [item for item in existing if item[2].get("active")]
            if len(existing) > 1 and len(active) != 1:
                choices = ", ".join(item[2]["id"] for item in existing)
                raise AssistantToolError(
                    _tr(
                        "File is open in multiple tabs: {tabs}. Activate the intended tab instead.",
                        tabs=choices,
                    )
                )
            record = active[0] if active else existing[0]
            if not self._focus_open_tab(record[0]):
                raise AssistantToolError(
                    _tr("REasy could not activate open file: {path}", path=target)
                )
            self._pulse_open_tab(record[0])
            payload = self._tab_target_payload(record[0])
            return {
                "opened": str(target),
                "opened_tab": payload,
                "reused_open_tab": True,
                "disk_reloaded": False,
            }

        before = {id(tab) for tab, _session, _payload in self._open_tab_records()}
        opened = bool(self.app._open_path(str(target)))
        if not opened:
            raise AssistantToolError(_tr("REasy could not open: {path}", path=target))
        record = next(iter(matching_records()), None)
        if record is None:
            raise AssistantToolError(
                _tr("REasy did not expose an editor tab for: {path}", path=target)
            )
        self._pulse_open_tab(record[0])
        return {
            "opened": str(target),
            "opened_tab": record[2],
            "reused_open_tab": id(record[0]) in before,
            "disk_reloaded": False,
        }

    def _show_open_project_dialog(self) -> str:
        QTimer.singleShot(0, self.app.open_project)
        return "The project library dialog was shown."

    def _show_create_project_dialog(self) -> str:
        self.app.new_project()
        return "The guided new-project dialog was shown."

    def _show_project_settings(self) -> str:
        self._active_project_root()
        self.app.proj_dock._proj_settings()
        return "The active project's settings dialog was shown."

    def _export_mod(self, format: str) -> str:
        self._active_project_root()
        if format == "fluffy_zip":
            self.app.proj_dock._export_zip()
            return "The Fluffy ZIP export workflow finished or displayed its result in REasy."
        if format == "pak":
            self.app.proj_dock._export_mod()
            return "The PAK export workflow was started. Its progress and final result are shown by REasy."
        raise AssistantToolError(_tr("format must be 'fluffy_zip' or 'pak'."))

    def _save_active_file(self) -> dict[str, Any]:
        tab = self.app.get_active_tab()
        if tab is None:
            raise AssistantToolError(_tr("No file is active."))
        saved = bool(tab.direct_save())
        if not saved:
            raise AssistantToolError(_tr("The file was not saved."))
        self._pulse_open_tab(tab)
        return {"saved": tab.filename}

    def _save_all_modified_files(self) -> dict[str, Any]:
        save_all = getattr(self.app, "save_all_modified_files", None)
        if not callable(save_all):
            raise AssistantToolError(
                _tr(
                    "This REasy window does not support Save All.",
                )
            )
        targets = [
            tab
            for tab, _session, payload in self._open_tab_records()
            if payload["modified"]
        ]
        result = save_all()
        for tab in targets:
            if not bool(getattr(tab, "modified", False)):
                self._pulse_open_tab(tab)
        return result

    @staticmethod
    def _mdf_for_tab(tab) -> tuple[Any, MdfViewer]:
        from file_handlers.mdf.mdf_viewer import MdfViewer

        if tab is None or not isinstance(getattr(tab, "viewer", None), MdfViewer):
            raise AssistantToolError(
                _tr(
                    "The selected editor is not an MDF file.",
                )
            )
        viewer: MdfViewer = tab.viewer
        if not viewer.handler.mdf:
            raise AssistantToolError(
                _tr(
                    "The selected MDF has no parsed data.",
                )
            )
        return tab, viewer

    def _active_mdf(self) -> tuple[Any, MdfViewer]:
        return self._mdf_for_tab(self.app.get_active_tab())

    @staticmethod
    def _resolve_material(viewer: MdfViewer, material: Any) -> int:
        materials = viewer.handler.mdf.materials
        if isinstance(material, int):
            index = material
        else:
            text = str(material).strip()
            if text.lstrip("-").isdigit():
                index = int(text)
            else:
                exact = [
                    index
                    for index, item in enumerate(materials)
                    if (item.header.mat_name or "").casefold() == text.casefold()
                ]
                if not exact:
                    raise AssistantToolError(
                        _tr("Material not found: {material}", material=material)
                    )
                if len(exact) > 1:
                    raise AssistantToolError(
                        _tr(
                            "Material name is ambiguous: {material}",
                            material=material,
                        )
                    )
                index = exact[0]
        if not 0 <= index < len(materials):
            raise AssistantToolError(
                _tr("Material index out of range: {index}", index=index)
            )
        return index

    def _select_material(self, viewer: MdfViewer, material: Any, section: str = "overview") -> int:
        index = self._resolve_material(viewer, material)
        if viewer.filter_edit.text():
            viewer.filter_edit.clear()
        viewer.materials_table.clearSelection()
        viewer.materials_table.selectRow(index)
        viewer._current_index = index
        section_indexes = {
            "overview": 0,
            "textures": 1,
            "parameters": 2,
            "gpu_buffers": viewer.gpbf_tab_idx,
            "shader_lods": viewer.shaderLODRedirects_tab_idx,
        }
        if section not in section_indexes:
            raise AssistantToolError(
                _tr("Unknown MDF section: {section}", section=section)
            )
        version = viewer._current_file_version()
        if section == "gpu_buffers" and version < 19:
            raise AssistantToolError(
                _tr("GPU buffers require MDF version 19 or newer.")
            )
        if section == "shader_lods" and version < 31:
            raise AssistantToolError(
                _tr("Shader LOD redirects require MDF version 31 or newer.")
            )
        viewer.tabs.setCurrentIndex(section_indexes[section])
        viewer._refresh_details_for_current_material()
        return index

    @staticmethod
    def _flags_for(viewer: MdfViewer, index: int) -> dict[str, Any]:
        header = viewer.handler.mdf.materials[index].header
        raw = int(header.material_flags)
        version = viewer._current_file_version()
        flags: dict[str, Any] = {}
        for bit, name in enumerate(viewer._flags1_names):
            flags[name] = bool((raw >> bit) & 1)
        if version >= 31:
            flags["TransparentZPostPassEnable"] = bool((raw >> 10) & 1)
            flags["tessellation"] = (raw >> 11) & 0x1F
        else:
            flags["tessellation"] = (raw >> 10) & 0x3F
        flags["phong"] = (raw >> 16) & 0xFF
        for bit, name in enumerate(viewer._flags2_names):
            flags[name] = bool((raw >> (24 + bit)) & 1)
        if version >= 31:
            for bit, name in enumerate(viewer._flags3_names):
                flags[name] = bool((raw >> (32 + bit)) & 1)
            bias = (raw >> 40) & 0xFF
            flags["transparent_priority_bias"] = bias - 256 if bias >= 128 else bias
        return flags

    @staticmethod
    def _material_summary(item, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "name": item.header.mat_name,
            "textures": len(item.textures),
            "parameters": len(item.parameters),
            "gpu_buffers": len(item.gpu_buffers),
            "shader_lods": len(item.shader_lod_redirects),
        }

    def _material_details(self, viewer: MdfViewer, index: int) -> dict[str, Any]:
        mdf = viewer.handler.mdf
        item = mdf.materials[index]
        header = item.header
        unknown_64 = (
            int(header.ukn)
            if mdf.file_version >= 51
            else int(header.ukn_re7)
            if mdf.file_version == 6
            else None
        )
        return {
            "index": index,
            "name": header.mat_name,
            "mmtr_path": header.mmtr_path,
            "shader_type": {
                "index": int(header.shader_type),
                "name": (
                    viewer._shader_names[int(header.shader_type)]
                    if 0 <= int(header.shader_type) < len(viewer._shader_names)
                    else None
                ),
            },
            "bake_texture_array_size": int(header.BakeTextureArraySize),
            "unknown_64": unknown_64,
            "flags": self._flags_for(viewer, index),
            "textures": [
                {
                    "index": row,
                    "type": texture.tex_type,
                    "path": texture.tex_path,
                    "locked": bool(texture.locked),
                }
                for row, texture in enumerate(item.textures)
            ],
            "parameters": [
                {
                    "index": row,
                    "name": parameter.name,
                    "component_count": int(parameter.component_count),
                    "values": list(parameter.parameter[: parameter.component_count]),
                    "locked": bool(parameter.component_locked),
                }
                for row, parameter in enumerate(item.parameters)
            ],
            "gpu_buffers": [
                {"index": row, "name": name.name, "data": data.name}
                for row, (name, data) in enumerate(item.gpu_buffers)
            ],
            "shader_lods": [
                {
                    "index": row,
                    "texture_table": list(texture_table),
                    "byte_buffer_table": list(byte_buffer_table),
                }
                for row, (texture_table, byte_buffer_table) in enumerate(item.shader_lod_redirects)
            ],
        }

    @staticmethod
    def _inspect_mdf_header(tab, viewer: MdfViewer) -> dict[str, Any]:
        mdf = viewer.handler.mdf
        pak_backed = bool(getattr(tab, "pak_source_path", None))
        result = {
            "file": str(getattr(tab, "filename", "") or ""),
            "modified": bool(getattr(tab, "modified", False)),
            "version": int(mdf.file_version),
            "meshlet_material": bool(mdf.header.meshlet_material),
            "editable_in_memory": True,
            "pak_backed": pak_backed,
            "save_requires_copy": pak_backed,
        }
        if pak_backed:
            result["can_write_back_to_pak"] = False
        return result

    def _tab_target_payload(self, tab) -> dict[str, Any]:
        record = next(
            (
                payload
                for current, _session, payload in self._open_tab_records()
                if current is tab
            ),
            None,
        )
        if record is not None:
            return record
        pak_backed = bool(getattr(tab, "pak_source_path", None))
        disk_path = self._resolved_disk_file_path(
            str(getattr(tab, "filename", "") or ""),
            pak_backed=pak_backed,
        )
        payload = {
            "id": self._tab_id(tab),
            "title": self._tab_title(tab),
            "path": disk_path,
            "source_path": getattr(tab, "pak_source_path", None),
            "editable_in_memory": True,
            "pak_backed": pak_backed,
            "save_requires_copy": pak_backed,
            "workspace": "standalone",
            "project": None,
            "project_name": None,
            "game": None,
            "project_active": False,
            "active": tab is self.app.get_active_tab(),
        }
        if pak_backed:
            payload["can_write_back_to_pak"] = False
        return payload

    def _comparison_file_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "tab_id": payload["id"],
            "title": payload["title"],
            "file": self._file_result_path(payload),
            "path": payload.get("path"),
            "source_path": payload.get("source_path"),
            "modified": bool(payload.get("modified")),
            "pak_backed": bool(payload.get("pak_backed")),
        }

    def _resolve_mdf_targets(
        self,
        tabs: list[str] | None,
        all_open: bool,
    ) -> list[tuple[Any, MdfViewer, dict[str, Any]]]:
        if not isinstance(all_open, bool):
            raise AssistantToolError(_tr("all_open must be true or false."))
        if tabs is not None and not isinstance(tabs, list):
            raise AssistantToolError(
                _tr(
                    "tabs must be an array of open tab IDs, paths, or titles.",
                )
            )
        if all_open and tabs:
            raise AssistantToolError(
                _tr(
                    "inspect_mdf cannot combine all_open with explicit tabs.",
                )
            )
        if tabs and len(tabs) > 16:
            raise AssistantToolError(
                _tr(
                    "inspect_mdf accepts at most 16 tabs in one call.",
                )
            )

        candidates: list[tuple[Any, dict[str, Any]]]
        if all_open:
            candidates = [
                (tab, payload)
                for tab, _session, payload in self._open_tab_records()
            ]
        elif tabs:
            candidates = [
                (tab, payload)
                for tab, _session, payload in (
                    self._resolve_open_tab(reference) for reference in tabs
                )
            ]
        else:
            tab = self.app.get_active_tab()
            candidates = [(tab, self._tab_target_payload(tab))]

        targets: list[tuple[Any, MdfViewer, dict[str, Any]]] = []
        seen: set[int] = set()
        for tab, payload in candidates:
            if tab is None or id(tab) in seen:
                continue
            try:
                _tab, viewer = self._mdf_for_tab(tab)
            except AssistantToolError:
                if all_open:
                    continue
                raise
            seen.add(id(tab))
            targets.append((tab, viewer, payload))

        if not targets:
            raise AssistantToolError(
                _tr(
                    "No open MDF editors are available to inspect.",
                )
            )
        return targets

    def _inspect_mdf(
        self,
        tabs: list[str] | None = None,
        all_open: bool = False,
        materials: list[Any] | None = None,
        all_materials: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(all_materials, bool):
            raise AssistantToolError(_tr("all_materials must be true or false."))
        if materials is not None and not isinstance(materials, list):
            raise AssistantToolError(
                _tr(
                    "materials must be an array of material names or indices.",
                )
            )
        if materials is not None and len(materials) > 128:
            raise AssistantToolError(
                _tr(
                    "inspect_mdf accepts at most 128 materials in one call.",
                )
            )
        if all_materials and materials:
            raise AssistantToolError(
                _tr(
                    "inspect_mdf cannot combine all_materials with explicit materials.",
                )
            )

        targets = self._resolve_mdf_targets(tabs, all_open)
        full_details = all_materials or bool(materials)
        files = []
        for tab, viewer, target in targets:
            result = {
                **self._inspect_mdf_header(tab, viewer),
                "file": self._file_result_path(target),
                "tab_id": target["id"],
                "title": target["title"],
                "source_path": target.get("source_path"),
                "project": target.get("project"),
                "project_name": target.get("project_name"),
                "game": target.get("game"),
                "active": bool(target.get("active")),
            }
            mdf_materials = viewer.handler.mdf.materials
            if not full_details:
                result["materials"] = [
                    self._material_summary(item, index)
                    for index, item in enumerate(mdf_materials)
                ]
            else:
                requested = (
                    list(range(len(mdf_materials)))
                    if all_materials
                    else list(materials or [])
                )
                selected_indices = []
                missing = []
                for reference in requested:
                    try:
                        index = self._resolve_material(viewer, reference)
                    except AssistantToolError as exc:
                        missing.append(
                            {"requested": reference, "error": str(exc)}
                        )
                        continue
                    if index not in selected_indices:
                        selected_indices.append(index)
                result["materials"] = [
                    self._material_details(viewer, index)
                    for index in selected_indices
                ]
                if missing:
                    result["missing_materials"] = missing
            files.append(result)

        return {
            "mode": "full" if full_details else "summary",
            "files": files,
            "count": len(files),
            "read_in_memory": True,
            "tabs_activated": False,
        }

    def _resolve_or_open_mdf_tab(self, reference: str):
        requested = str(reference or "").strip()
        try:
            tab, session, payload = self._resolve_open_tab(requested)
        except AssistantToolError as original_error:
            candidate = Path(
                os.path.expandvars(os.path.expanduser(requested))
            ).resolve()
            if not candidate.is_file():
                raise original_error
            if not self.app._open_path(str(candidate)):
                raise AssistantToolError(
                    _tr(
                        "REasy could not open MDF file: {path}",
                        path=candidate,
                    )
                )
            tab, session, payload = self._resolve_open_tab(str(candidate))
        _tab, viewer = self._mdf_for_tab(tab)
        return tab, session, payload, viewer

    def _migration_snapshot(self, viewer: MdfViewer) -> dict[str, Any]:
        mdf = viewer.handler.mdf
        version = viewer._current_file_version()
        materials: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(mdf.materials):
            details = self._material_details(viewer, index)
            name = str(details["name"] or "").strip()
            if not name:
                raise AssistantToolError(
                    _tr(
                        "Every MDF material must have a name for this operation.",
                    )
                )
            key = name.casefold()
            if key in materials:
                raise AssistantToolError(
                    _tr(
                        "MDF material names must be unique for this operation; duplicate: {name}",
                        name=name,
                    )
                )

            overview: dict[str, Any] = {
                "mmtr_path": details["mmtr_path"],
                "shader_type": details["shader_type"]["index"],
            }
            if version >= 31:
                overview["bake_texture_array_size"] = details[
                    "bake_texture_array_size"
                ]
            if version == 6 or version >= 51:
                overview["unknown_64"] = details["unknown_64"]

            material = {
                "_index": index,
                "name": name,
                "overview": overview,
                "flags": details["flags"],
                "textures": [
                    {
                        key: value
                        for key, value in row.items()
                        if key != "index"
                    }
                    for row in details["textures"]
                ],
                "parameters": [
                    {
                        key: value
                        for key, value in row.items()
                        if key != "index"
                    }
                    for row in details["parameters"]
                ],
            }
            if version >= 19:
                material["gpu_buffers"] = [
                    {
                        key: value
                        for key, value in row.items()
                        if key != "index"
                    }
                    for row in details["gpu_buffers"]
                ]
            if version >= 31:
                material["shader_lods"] = [
                    {
                        key: value
                        for key, value in row.items()
                        if key != "index"
                    }
                    for row in details["shader_lods"]
                ]
            materials[key] = material

        return {
            "meshlet_material": bool(mdf.header.meshlet_material),
            "materials": materials,
        }

    def _normalized_mdf_snapshot(
        self,
        viewer: MdfViewer,
    ) -> dict[str, Any]:
        source = self._migration_snapshot(viewer)
        normalized: dict[str, Any] = {}
        for material_key, material in source["materials"].items():
            item = {
                key: copy.deepcopy(value)
                for key, value in material.items()
                if key != "_index"
            }
            for table in (
                "textures",
                "parameters",
                "gpu_buffers",
                "shader_lods",
            ):
                if table not in item:
                    continue
                rows: dict[str, Any] = {}
                order = []
                occurrences: dict[str, int] = {}
                for index, row in enumerate(item[table]):
                    identity, _display = self._copy_row_identity(
                        table,
                        row,
                        index,
                    )
                    occurrence = occurrences.get(identity, 0)
                    occurrences[identity] = occurrence + 1
                    row_key = (
                        identity
                        if occurrence == 0
                        else f"{identity}#{occurrence + 1}"
                    )
                    rows[row_key] = row
                    order.append(row_key)
                item[table] = rows
                item[f"{table}_order"] = order
            normalized[material_key] = item
        return {
            "version": viewer._current_file_version(),
            "meshlet_material": source["meshlet_material"],
            "material_order": list(source["materials"]),
            "materials": normalized,
        }

    def _comparison_snapshot(
        self,
        viewer: MdfViewer,
        material_keys: set[str] | None,
    ) -> dict[str, Any]:
        source = self._normalized_mdf_snapshot(viewer)
        materials = {
            key: value
            for key, value in source["materials"].items()
            if material_keys is None or key in material_keys
        }
        return {
            "version": source["version"],
            "meshlet_material": source["meshlet_material"],
            "material_order": [
                key
                for key in source["material_order"]
                if key in materials
            ],
            "materials": materials,
        }

    @staticmethod
    def _comparison_path_text(path: tuple[str, ...]) -> str:
        if not path:
            return "MDF"
        if path[0] == "material_order":
            return "materials.order"
        if path[0] != "materials" or len(path) < 2:
            return ".".join(path)
        text = f"material[{path[1]}]"
        if len(path) == 2:
            return text
        section = path[2]
        if section.endswith("_order"):
            return f"{text}.{section.removesuffix('_order')}.order"
        text += f".{section}"
        if section in {
            "textures",
            "parameters",
            "gpu_buffers",
            "shader_lods",
        } and len(path) >= 4:
            text += f"[{path[3]}]"
            path = (*path[:3], *path[4:])
        if len(path) > 3:
            text += "." + ".".join(path[3:])
        return text

    @staticmethod
    def _comparison_result_value(value: Any) -> Any:
        if value is _MISSING:
            return {"state": "missing"}
        if isinstance(value, list) and len(value) > 16:
            return {
                "item_count": len(value),
                "preview": value[:16],
            }
        if isinstance(value, dict):
            row_fields = {
                "type",
                "path",
                "locked",
                "name",
                "component_count",
                "values",
                "data",
                "texture_table",
                "byte_buffer_table",
            }
            if set(value).issubset(row_fields):
                return value
            return {"fields": list(value)}
        return value

    def _collect_mdf_differences(
        self,
        left: Any,
        right: Any,
        path: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        if _merge_equal(left, right):
            return []
        if (
            path
            and path[-1].endswith("_order")
            and isinstance(left, list)
            and isinstance(right, list)
        ):
            shared = set(left).intersection(right)
            left = [value for value in left if value in shared]
            right = [value for value in right if value in shared]
            if _merge_equal(left, right):
                return []
        if isinstance(left, dict) and isinstance(right, dict):
            differences = []
            for key in dict.fromkeys((*left, *right)):
                differences.extend(
                    self._collect_mdf_differences(
                        left.get(key, _MISSING),
                        right.get(key, _MISSING),
                        (*path, str(key)),
                    )
                )
            return differences
        return [
            {
                "path": self._comparison_path_text(path),
                "kind": (
                    "right_only"
                    if left is _MISSING
                    else "left_only"
                    if right is _MISSING
                    else "changed"
                ),
                "left": self._comparison_result_value(left),
                "right": self._comparison_result_value(right),
            }
        ]

    def _compare_mdf_files(
        self,
        left: str = "",
        right: str = "",
        materials: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        left_ref = str(left or "").strip()
        right_ref = str(right or "").strip()
        if bool(left_ref) != bool(right_ref):
            raise AssistantToolError(
                _tr(
                    "compare_mdf_files requires both left and right, or neither when exactly two MDFs are open."
                )
            )
        if left_ref:
            left_tab, _left_session, left_payload = self._resolve_open_tab(
                left_ref
            )
            right_tab, _right_session, right_payload = (
                self._resolve_open_tab(right_ref)
            )
            _left_tab, left_viewer = self._mdf_for_tab(left_tab)
            _right_tab, right_viewer = self._mdf_for_tab(right_tab)
        else:
            targets = self._resolve_mdf_targets(None, True)
            if len(targets) != 2:
                raise AssistantToolError(
                    _tr(
                        "compare_mdf_files found {count} open MDFs; specify two tab IDs.",
                        count=len(targets),
                    )
                )
            (
                (left_tab, left_viewer, left_payload),
                (right_tab, right_viewer, right_payload),
            ) = targets
        if left_tab is right_tab:
            raise AssistantToolError(
                _tr("The MDF comparison inputs must be different tabs.")
            )

        material_keys = None
        material_names: dict[str, str] = {}
        if materials is not None:
            if not isinstance(materials, list):
                raise AssistantToolError(
                    _tr("materials must be an array of MDF material names.")
                )
            if len(materials) > 128:
                raise AssistantToolError(
                    _tr("compare_mdf_files accepts at most 128 materials.")
                )
            for material in materials:
                if not isinstance(material, str) or not material.strip():
                    raise AssistantToolError(
                        _tr("MDF comparison material names must not be empty.")
                    )
                material_names.setdefault(
                    self._copy_identity(material),
                    material.strip(),
                )
            material_keys = set(material_names) or None

        left_snapshot = self._comparison_snapshot(
            left_viewer,
            material_keys,
        )
        right_snapshot = self._comparison_snapshot(
            right_viewer,
            material_keys,
        )
        differences = self._collect_mdf_differences(
            left_snapshot,
            right_snapshot,
        )
        limit = self._integer(limit, "limit", 1, 500)
        available_materials = set(
            left_snapshot["materials"]
        ).union(right_snapshot["materials"])
        selection_misses = [
            material_names[key]
            for key in material_names.keys() - available_materials
        ]

        return {
            "identical": not differences,
            "difference_count": len(differences),
            "differences": differences[:limit],
            "truncated": len(differences) > limit,
            "left": self._comparison_file_payload(left_payload),
            "right": self._comparison_file_payload(right_payload),
            "selection_misses": selection_misses,
            "read_in_memory": True,
            "tabs_activated": False,
            "files_modified": False,
        }

    @staticmethod
    def _migration_path_text(path: tuple[str, ...]) -> str:
        if len(path) >= 2 and path[0] == "materials":
            suffix = ".".join(path[2:])
            return f"material[{path[1]}]" + (f".{suffix}" if suffix else "")
        return ".".join(path)

    @staticmethod
    def _migration_result_value(value: Any) -> Any:
        if value is _MISSING:
            return {"state": "missing"}
        if isinstance(value, list):
            return {"row_count": len(value)}
        if isinstance(value, dict):
            return {"fields": list(value)}
        return value

    @staticmethod
    def _denormalize_mdf_table(
        material: dict[str, Any],
        table: str,
    ) -> list[dict[str, Any]]:
        rows = material.get(table, {})
        if isinstance(rows, list):
            return copy.deepcopy(rows)
        order = material.get(f"{table}_order", list(rows))
        ordered = [
            copy.deepcopy(rows[key])
            for key in order
            if key in rows
        ]
        ordered.extend(
            copy.deepcopy(row)
            for key, row in rows.items()
            if key not in order
        )
        return ordered

    def _denormalize_mdf_material(
        self,
        material: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        result = {
            key: copy.deepcopy(value)
            for key, value in material.items()
            if not key.endswith("_order")
            and key
            not in {
                "textures",
                "parameters",
                "gpu_buffers",
                "shader_lods",
            }
        }
        result["_index"] = index
        for table in (
            "textures",
            "parameters",
            "gpu_buffers",
            "shader_lods",
        ):
            if table in material:
                result[table] = self._denormalize_mdf_table(
                    material,
                    table,
                )
        return result

    @staticmethod
    def _set_migration_snapshot_value(
        snapshot: dict[str, Any],
        path: tuple[str, ...],
        value: Any,
    ):
        current = snapshot
        for part in path[:-1]:
            current = current[part]
        if value is _MISSING:
            current.pop(path[-1], None)
        else:
            current[path[-1]] = copy.deepcopy(value)

    def _prepare_migration_changes(
        self,
        target: dict[str, Any],
        changes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Collapse normalized row edits into UI-applicable MDF operations."""

        desired = copy.deepcopy(target)
        for change in changes:
            self._set_migration_snapshot_value(
                desired,
                change["path"],
                change["modded"],
            )
        requested_order = desired.get("material_order", [])
        material_keys = desired.get("materials", {})
        material_order = [
            key for key in requested_order if key in material_keys
        ]
        material_order.extend(
            key for key in material_keys if key not in material_order
        )
        desired["material_order"] = material_order

        prepared: list[dict[str, Any]] = []
        table_paths: dict[tuple[str, str], dict[str, Any]] = {}
        whole_materials: dict[str, dict[str, Any]] = {}
        material_order_change: dict[str, Any] | None = None
        table_names = {
            "textures",
            "parameters",
            "gpu_buffers",
            "shader_lods",
        }
        for change in changes:
            path = change["path"]
            if path == ("material_order",):
                material_order_change = change
                continue
            if len(path) == 2 and path[0] == "materials":
                whole_materials.setdefault(path[1], change)
                continue
            if len(path) >= 3 and path[0] == "materials":
                section = path[2]
                table = (
                    section.removesuffix("_order")
                    if section.endswith("_order")
                    else section
                )
                if table in table_names:
                    table_paths.setdefault((path[1], table), change)
                    continue
            prepared.append(change)

        for material_key, change in whole_materials.items():
            material = desired["materials"].get(material_key, _MISSING)
            if material is _MISSING:
                modded = _MISSING
            else:
                index = (
                    material_order.index(material_key)
                    if material_key in material_order
                    else len(material_order)
                )
                modded = self._denormalize_mdf_material(
                    material,
                    index,
                )
            prepared.append({**change, "modded": modded})

        for (material_key, table), change in table_paths.items():
            material = desired["materials"].get(material_key)
            if material is None or table not in material:
                continue
            prepared.append(
                {
                    **change,
                    "path": ("materials", material_key, table),
                    "modded": self._denormalize_mdf_table(
                        material,
                        table,
                    ),
                }
            )

        if material_order_change is not None:
            prepared.append(
                {
                    **material_order_change,
                    "modded": list(material_order),
                }
            )
        return prepared

    @staticmethod
    def _migration_path_supported(
        target: dict[str, Any],
        path: tuple[str, ...],
    ) -> bool:
        if path == ("material_order",):
            return True
        if len(path) == 2 and path[0] == "materials":
            return True
        current: Any = target
        for part in path:
            if not isinstance(current, dict) or part not in current:
                return False
            current = current[part]
        return True

    def _migration_change_supported(
        self,
        target: dict[str, Any],
        viewer: MdfViewer,
        change: dict[str, Any],
    ) -> bool:
        path = change["path"]
        table_names = {
            "textures",
            "parameters",
            "gpu_buffers",
            "shader_lods",
        }
        if len(path) >= 3 and path[0] == "materials":
            section = path[2]
            table = (
                section.removesuffix("_order")
                if section.endswith("_order")
                else section
            )
            if table in table_names:
                version = viewer._current_file_version()
                return (
                    path[1] in target.get("materials", {})
                    and (table != "gpu_buffers" or version >= 19)
                    and (table != "shader_lods" or version >= 31)
                )
        if not self._migration_path_supported(target, path):
            return False
        if path == ("material_order",):
            return True
        if path == ("meshlet_material",):
            return hasattr(viewer, "meshlet_check")
        if (
            len(path) >= 4
            and path[0] == "materials"
            and path[2] == "overview"
            and path[3] == "shader_type"
        ):
            value = change["modded"]
            return (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value < viewer.shader_combo.count()
            )
        if (
            len(path) != 2
            or path[0] != "materials"
            or change["modded"] is _MISSING
        ):
            return True

        material = change["modded"]
        if not isinstance(material, dict):
            return False
        version = viewer._current_file_version()
        overview = material.get("overview", {})
        allowed_overview = {"mmtr_path", "shader_type"}
        if version >= 31:
            allowed_overview.add("bake_texture_array_size")
        if version == 6 or version >= 51:
            allowed_overview.add("unknown_64")
        if not set(overview).issubset(allowed_overview):
            return False
        shader_type = overview.get("shader_type")
        if (
            not isinstance(shader_type, int)
            or isinstance(shader_type, bool)
            or not 0 <= shader_type < viewer.shader_combo.count()
        ):
            return False
        if version < 19 and material.get("gpu_buffers"):
            return False
        if version < 31 and material.get("shader_lods"):
            return False

        supported_flags = {
            *viewer._flags1_names,
            *viewer._flags2_names,
            "tessellation",
            "phong",
        }
        if version >= 31:
            supported_flags.update(
                {
                    "TransparentZPostPassEnable",
                    *viewer._flags3_names,
                    "transparent_priority_bias",
                }
            )
        return set(material.get("flags", {})).issubset(supported_flags)

    def _replace_mdf_table(
        self,
        viewer: MdfViewer,
        material: str,
        table_name: str,
        rows: list[dict[str, Any]],
    ):
        material_index = self._resolve_material(viewer, material)
        item = viewer.handler.mdf.materials[material_index]
        specs = {
            "textures": (
                "textures",
                self._delete_mdf_texture,
                self._upsert_mdf_texture,
                ("type", "path", "locked"),
            ),
            "parameters": (
                "parameters",
                self._delete_mdf_parameter,
                self._upsert_mdf_parameter,
                ("name", "component_count", "values", "locked"),
            ),
            "gpu_buffers": (
                "gpu_buffers",
                self._delete_mdf_gpu_buffer,
                self._upsert_mdf_gpu_buffer,
                ("name", "data"),
            ),
            "shader_lods": (
                "shader_lod_redirects",
                self._delete_mdf_shader_lod,
                self._upsert_mdf_shader_lod,
                ("texture_table", "byte_buffer_table"),
            ),
        }
        spec = specs.get(table_name)
        if spec is None:
            raise AssistantToolError(
                _tr(
                    "Unsupported MDF table: {table}",
                    table=table_name,
                )
            )
        collection_name, delete_row, upsert_row, fields = spec
        for row_index in range(
            len(getattr(item, collection_name)) - 1,
            -1,
            -1,
        ):
            delete_row(material, row_index)
        for row in rows:
            upsert_row(material, -1, *(row[field] for field in fields))

    def _reorder_mdf_materials(
        self,
        viewer: MdfViewer,
        order: list[str],
    ):
        materials = viewer.handler.mdf.materials
        by_name = {
            item.header.mat_name.casefold(): item
            for item in materials
        }
        desired = [
            by_name[key]
            for key in order
            if key in by_name
        ]
        desired_ids = {id(item) for item in desired}
        desired.extend(
            item
            for item in materials
            if id(item) not in desired_ids
        )
        if len(desired) == len(materials) and all(
            left is right
            for left, right in zip(desired, materials)
        ):
            return
        selected_name = ""
        current_index = viewer._get_current_index()
        if 0 <= current_index < len(materials):
            selected_name = materials[current_index].header.mat_name.casefold()
        materials[:] = desired
        viewer._clear_params_cache()
        viewer._refresh_materials_list()
        if materials:
            selected_index = next(
                (
                    index
                    for index, item in enumerate(materials)
                    if item.header.mat_name.casefold() == selected_name
                ),
                0,
            )
            viewer.materials_table.selectRow(selected_index)
            viewer._current_index = selected_index
            viewer._refresh_details_for_current_material()
        viewer.modified = True
        self._action_feedback.pulse_widget(
            viewer.materials_table.viewport()
        )

    def _apply_material_snapshot(
        self,
        viewer: MdfViewer,
        material: dict[str, Any],
    ):
        name = material["name"]
        insert_at = max(
            0,
            min(
                int(material.get("_index", len(viewer.handler.mdf.materials))),
                len(viewer.handler.mdf.materials),
            ),
        )
        if insert_at == len(viewer.handler.mdf.materials):
            self._add_mdf_material(name)
        else:
            from file_handlers.mdf.mdf_file import MatData

            blank = MatData()
            blank.header.mat_name = viewer._generate_unique_material_name(
                "Material",
                viewer._get_existing_material_names(),
            )
            viewer._insert_materials([blank], insert_at=insert_at)
            self._select_material(viewer, insert_at, "overview")
            viewer.matname_edit.setText(name)
            self._action_feedback.pulse_table_row(
                viewer.materials_table,
                insert_at,
            )
        version = viewer._current_file_version()

        overview = {
            key: value
            for key, value in material["overview"].items()
            if key in {"mmtr_path", "shader_type"}
            or (key == "bake_texture_array_size" and version >= 31)
            or (key == "unknown_64" and (version == 6 or version >= 51))
        }
        if overview:
            self._edit_mdf_overview(name, overview)

        index = self._resolve_material(viewer, name)
        supported_flags = self._flags_for(viewer, index)
        flags = {
            key: value
            for key, value in material["flags"].items()
            if key in supported_flags
        }
        if flags:
            self._edit_mdf_flags(name, flags)

        self._replace_mdf_table(
            viewer,
            name,
            "textures",
            material["textures"],
        )
        self._replace_mdf_table(
            viewer,
            name,
            "parameters",
            material["parameters"],
        )
        if version >= 19 and "gpu_buffers" in material:
            self._replace_mdf_table(
                viewer,
                name,
                "gpu_buffers",
                material["gpu_buffers"],
            )
        if version >= 31 and "shader_lods" in material:
            self._replace_mdf_table(
                viewer,
                name,
                "shader_lods",
                material["shader_lods"],
            )

    def _apply_migration_changes(
        self,
        target_tab,
        viewer: MdfViewer,
        changes: list[dict[str, Any]],
    ) -> tuple[list[str], list[dict[str, str]]]:
        if not self._focus_open_tab(target_tab):
            raise AssistantToolError(
                _tr(
                    "REasy could not activate the destination MDF.",
                )
            )
        self._pulse_open_tab(target_tab)

        applied: list[str] = []
        failures: list[dict[str, str]] = []

        header_changes = [
            change for change in changes if change["path"] == ("meshlet_material",)
        ]
        for change in header_changes:
            path_text = self._migration_path_text(change["path"])
            try:
                self._edit_mdf_header(
                    {"meshlet_material": change["modded"]}
                )
                applied.append(path_text)
            except Exception as exc:
                failures.append({"path": path_text, "error": str(exc)})

        whole_materials = [
            change
            for change in changes
            if len(change["path"]) == 2
            and change["path"][0] == "materials"
        ]
        for change in whole_materials:
            path_text = self._migration_path_text(change["path"])
            material_key = change["path"][1]
            try:
                existing = {
                    item.header.mat_name.casefold()
                    for item in viewer.handler.mdf.materials
                }
                if change["modded"] is _MISSING:
                    if material_key in existing:
                        self._delete_mdf_material(material_key)
                else:
                    if material_key in existing:
                        self._delete_mdf_material(material_key)
                    self._apply_material_snapshot(viewer, change["modded"])
                applied.append(path_text)
            except Exception as exc:
                failures.append({"path": path_text, "error": str(exc)})

        grouped: dict[str, dict[str, Any]] = {}
        for change in changes:
            path = change["path"]
            if len(path) < 3 or path[0] != "materials":
                continue
            group = grouped.setdefault(
                path[1],
                {
                    "overview": {},
                    "overview_paths": [],
                    "flags": {},
                    "flag_paths": [],
                    "tables": [],
                    "rename": None,
                },
            )
            if len(path) == 3 and path[2] == "name":
                group["rename"] = change
            elif len(path) == 4 and path[2] == "overview":
                group["overview"][path[3]] = change["modded"]
                group["overview_paths"].append(path)
            elif len(path) == 4 and path[2] == "flags":
                group["flags"][path[3]] = change["modded"]
                group["flag_paths"].append(path)
            elif len(path) == 3 and path[2] in {
                "textures",
                "parameters",
                "gpu_buffers",
                "shader_lods",
            }:
                group["tables"].append(change)
            else:
                path_text = self._migration_path_text(path)
                failures.append(
                    {
                        "path": path_text,
                        "error": _tr("Unsupported MDF change."),
                    }
                )

        for material_key, group in grouped.items():
            if group["overview"]:
                paths = [
                    self._migration_path_text(path)
                    for path in group["overview_paths"]
                ]
                try:
                    self._edit_mdf_overview(
                        material_key,
                        group["overview"],
                    )
                    applied.extend(paths)
                except Exception as exc:
                    failures.extend(
                        {"path": path, "error": str(exc)} for path in paths
                    )

            if group["flags"]:
                paths = [
                    self._migration_path_text(path)
                    for path in group["flag_paths"]
                ]
                try:
                    self._edit_mdf_flags(material_key, group["flags"])
                    applied.extend(paths)
                except Exception as exc:
                    failures.extend(
                        {"path": path, "error": str(exc)} for path in paths
                    )

            for change in group["tables"]:
                path_text = self._migration_path_text(change["path"])
                try:
                    self._replace_mdf_table(
                        viewer,
                        material_key,
                        change["path"][2],
                        change["modded"],
                    )
                    applied.append(path_text)
                except Exception as exc:
                    failures.append({"path": path_text, "error": str(exc)})

            rename = group["rename"]
            if rename is not None:
                path_text = self._migration_path_text(rename["path"])
                try:
                    self._edit_mdf_overview(
                        material_key,
                        {"name": rename["modded"]},
                    )
                    applied.append(path_text)
                except Exception as exc:
                    failures.append({"path": path_text, "error": str(exc)})

        for change in changes:
            if change["path"] != ("material_order",):
                continue
            path_text = self._migration_path_text(change["path"])
            try:
                self._reorder_mdf_materials(
                    viewer,
                    change["modded"],
                )
                applied.append(path_text)
            except Exception as exc:
                failures.append({"path": path_text, "error": str(exc)})

        return applied, failures

    def _migration_conflict_payload(
        self,
        conflict: dict[str, Any],
        resolution: str,
    ) -> dict[str, Any]:
        return {
            "path": self._migration_path_text(conflict["path"]),
            "baseline": self._migration_result_value(conflict["baseline"]),
            "modded": self._migration_result_value(conflict["modded"]),
            "destination": self._migration_result_value(conflict["target"]),
            "reason": conflict.get("reason", "destination_changed"),
            "resolution": resolution,
        }

    def _migrate_mdf_files(
        self,
        jobs: list[dict[str, Any]],
        conflict_policy: str = "skip",
    ) -> dict[str, Any]:
        self._require_update_project_paks()
        resolve, close_opened_tabs = self._tracked_migration_resolver()
        try:
            return self._migrate_mdf_files_impl(
                jobs,
                conflict_policy,
                resolve,
            )
        finally:
            close_opened_tabs()

    def _migrate_mdf_files_steps(
        self,
        jobs: list[dict[str, Any]],
        conflict_policy: str = "skip",
    ):
        self._require_update_project_paks()
        resolve, close_opened_tabs = self._tracked_migration_resolver()
        try:
            return (
                yield from self._migrate_mdf_files_impl_steps(
                    jobs,
                    conflict_policy,
                    resolve,
                )
            )
        finally:
            close_opened_tabs()

    def _tracked_migration_resolver(self):
        opened_tabs: dict[int, Any] = {}

        def resolve(reference: str):
            existing = {
                id(tab)
                for tab, _session, _payload in self._open_tab_records()
            }
            result = self._resolve_or_open_mdf_tab(reference)
            tab = result[0]
            if id(tab) not in existing:
                opened_tabs.setdefault(id(tab), tab)
            return result

        def close_opened_tabs():
            for tab in reversed(list(opened_tabs.values())):
                self._close_batch_tab(tab, True)

        return resolve, close_opened_tabs

    def _migrate_mdf_files_impl(
        self,
        jobs: list[dict[str, Any]],
        conflict_policy: str,
        resolve,
    ) -> dict[str, Any]:
        return self._run_incremental_steps(
            self._migrate_mdf_files_impl_steps(
                jobs,
                conflict_policy,
                resolve,
            )
        )

    def _migrate_mdf_files_impl_steps(
        self,
        jobs: list[dict[str, Any]],
        conflict_policy: str,
        resolve,
    ):
        if not isinstance(jobs, list) or not jobs:
            raise AssistantToolError(
                _tr(
                    "jobs must contain at least one MDF migration job.",
                )
            )
        payload_bytes = len(
            json.dumps(
                jobs,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if payload_bytes > MAX_MIGRATION_JOB_PAYLOAD_BYTES:
            raise AssistantToolError(
                _tr(
                    "The MDF migration request is too large for one efficient call; split it into smaller batches.",
                )
            )
        if conflict_policy not in {"skip", "abort", "prefer_mod"}:
            raise AssistantToolError(
                _tr(
                    "Invalid MDF migration conflict policy.",
                )
            )

        planned_jobs = []
        destination_ids: set[str] = set()
        for job_index, job in enumerate(jobs):
            item = (
                str(
                    job.get("label")
                    or job.get("destination")
                    or ""
                )
                if isinstance(job, dict)
                else ""
            )
            yield {
                "stage": "planning_migration",
                "current": job_index + 1,
                "completed": job_index,
                "total": len(jobs),
                "item": item,
            }
            if not isinstance(job, dict):
                raise AssistantToolError(
                    _tr(
                        "Every MDF migration job must be an object.",
                    )
                )
            missing_fields = [
                field
                for field in ("old_original", "modded", "destination")
                if not str(job.get(field, "")).strip()
            ]
            if missing_fields:
                raise AssistantToolError(
                    _tr(
                        "MDF migration job {index} is missing: {fields}",
                        index=job_index + 1,
                        fields=", ".join(missing_fields),
                    )
                )

            old_tab, _old_session, old_payload, old_viewer = (
                resolve(job["old_original"])
            )
            mod_tab, _mod_session, mod_payload, mod_viewer = (
                resolve(job["modded"])
            )
            target_tab, _target_session, target_payload, target_viewer = (
                resolve(job["destination"])
            )
            if len({id(old_tab), id(mod_tab), id(target_tab)}) != 3:
                raise AssistantToolError(
                    _tr(
                        "Each migration job must use three different MDF files.",
                    )
                )
            if target_payload["id"] in destination_ids:
                raise AssistantToolError(
                    _tr(
                        "A migration destination can appear only once per batch.",
                    )
                )
            destination_ids.add(target_payload["id"])

            old_version = old_viewer._current_file_version()
            mod_version = mod_viewer._current_file_version()
            if old_version != mod_version:
                raise AssistantToolError(
                    _tr(
                        "The old original and modded MDF must use the same MDF version.",
                    )
                )

            baseline = self._normalized_mdf_snapshot(old_viewer)
            modded = self._normalized_mdf_snapshot(mod_viewer)
            target = self._normalized_mdf_snapshot(target_viewer)
            changes, conflicts = _plan_three_way_merge(
                baseline,
                modded,
                target,
            )

            unsupported = [
                change
                for change in changes
                if not self._migration_change_supported(
                    target,
                    target_viewer,
                    change,
                )
            ]
            for change in unsupported:
                change["reason"] = "unsupported_by_destination"
            changes = [
                change for change in changes if change not in unsupported
            ]
            conflicts.extend(unsupported)

            overwritten = []
            if conflict_policy == "prefer_mod":
                still_conflicting = []
                for conflict in conflicts:
                    if self._migration_change_supported(
                        target,
                        target_viewer,
                        conflict,
                    ):
                        changes.append(conflict)
                        overwritten.append(conflict)
                    else:
                        still_conflicting.append(conflict)
                conflicts = still_conflicting

            prepared_changes = self._prepare_migration_changes(
                target,
                changes,
            )
            planned_jobs.append(
                {
                    "label": str(job.get("label") or target_payload["title"]),
                    "old": old_payload,
                    "mod": mod_payload,
                    "target": target_payload,
                    "target_tab": target_tab,
                    "target_viewer": target_viewer,
                    "target_version": target_viewer._current_file_version(),
                    "changes": prepared_changes,
                    "conflicts": conflicts,
                    "overwritten": overwritten,
                }
            )

        abort_batch = conflict_policy == "abort" and any(
            job["conflicts"] for job in planned_jobs
        )
        results = []
        total_applied = 0
        total_conflicts = 0
        files_modified = 0
        compact_results = len(planned_jobs) > 20
        for job_index, job in enumerate(planned_jobs):
            yield {
                "stage": "applying_migration",
                "current": job_index + 1,
                "completed": job_index,
                "total": len(planned_jobs),
                "item": job["label"],
            }
            if abort_batch:
                applied: list[str] = []
                failures: list[dict[str, str]] = []
            elif job["changes"]:
                applied, failures = self._apply_migration_changes(
                    job["target_tab"],
                    job["target_viewer"],
                    job["changes"],
                )
            else:
                applied, failures = [], []

            conflict_resolution = "aborted" if abort_batch else "skipped"
            conflict_payloads = [
                self._migration_conflict_payload(
                    conflict,
                    conflict_resolution,
                )
                for conflict in job["conflicts"]
            ]
            conflict_payloads.extend(
                self._migration_conflict_payload(
                    conflict,
                    "prefer_mod",
                )
                for conflict in job["overwritten"]
            )
            conflict_count = len(conflict_payloads)
            total_conflicts += conflict_count
            total_applied += len(applied)
            if applied:
                files_modified += 1

            if abort_batch:
                status = "aborted"
            elif failures:
                status = "partial" if applied else "failed"
            elif job["conflicts"]:
                status = "partial" if applied else "conflicts_only"
            elif applied:
                status = "updated"
            else:
                status = "already_up_to_date"

            job_result = {
                "label": job["label"],
                "status": status,
                "destination": job["target"]["id"],
                "destination_file": job["target"]["path"],
                "destination_pak_backed": bool(
                    job["target"].get("pak_backed")
                ),
                "applied_change_count": len(applied),
                "modified": bool(job["target_viewer"].modified),
                "saved": False,
            }
            if not compact_results or conflict_payloads or failures:
                job_result.update(
                    {
                        "old_original": job["old"]["id"],
                        "modded": job["mod"]["id"],
                        "destination_version": job["target_version"],
                        "applied_changes": applied,
                        "conflicts": conflict_payloads,
                        "failures": failures,
                    }
                )
            results.append(job_result)

        return {
            "jobs": results,
            "jobs_processed": len(results),
            "files_modified": files_modified,
            "changes_applied": total_applied,
            "conflicts": total_conflicts,
            "conflict_policy": conflict_policy,
            "aborted": abort_batch,
            "result_compacted": compact_results,
            "destination_versions_preserved": True,
            "saved": False,
        }

    @staticmethod
    def _folder_path(value: str, field: str) -> Path:
        raw = str(value or "").strip()
        path = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
        if not raw or not path.is_dir() or path.parent == path:
            raise AssistantToolError(
                _tr(
                    "{field} must identify an existing non-root folder: {path}",
                    field=field,
                    path=path,
                )
            )
        return path

    @staticmethod
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
            return sorted(
                files,
                key=lambda path: path.relative_to(root).as_posix().casefold(),
            )
        except OSError as exc:
            raise AssistantToolError(
                _tr("Could not scan folder {path}: {error}", path=root, error=exc)
            ) from exc

    def _resolve_mod_output_folder(
        self,
        mod_root: Path,
        originals_root: Path,
        requested: str,
    ) -> Path:
        raw = str(requested or "").strip()
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
            output in {mod_root, originals_root}
            or mod_root in output.parents
            or originals_root in output.parents
        ):
            raise AssistantToolError(
                _tr(
                    "output_folder must be outside mod_folder and originals_folder."
                )
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
                    _OUTPUT_INSPECTION_ERROR,
                    path=output,
                    error=exc,
                )
            ) from exc
        return output

    @staticmethod
    def _create_mod_staging_folder(output: Path) -> Path:
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

    @staticmethod
    def _publish_mod_output(staging: Path, output: Path) -> None:
        removed_empty_output = False
        try:
            if output.exists():
                if (
                    not output.is_dir()
                    or next(output.iterdir(), None) is not None
                ):
                    raise OSError(
                        _tr(
                            _OUTPUT_CHANGED_ERROR,
                            path=output,
                        )
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
                    _OUTPUT_FINALIZE_ERROR,
                    path=output,
                    error=exc,
                )
            ) from exc

    @staticmethod
    def _copy_mod_file(source: Path, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    @staticmethod
    def _copy_identity(value: Any) -> str:
        return str(value).strip().casefold()

    def _copy_scope(
        self,
        materials: list[str] | None,
        sections: list[str] | None,
        values: list[Any] | None,
        include_source_only: bool,
    ) -> dict[str, Any]:
        if materials is not None and not isinstance(materials, list):
            raise AssistantToolError(
                _tr("materials must be an array of MDF material names.")
            )
        if sections is not None and not isinstance(sections, list):
            raise AssistantToolError(
                _tr("sections must be an array of MDF section names.")
            )
        if values is not None and not isinstance(values, list):
            raise AssistantToolError(
                _tr(
                    "values must be an array of MDF field names, row names, or shader-LOD indices."
                )
            )
        if materials is not None and len(materials) > 128:
            raise AssistantToolError(
                _tr("copy_mdf_values accepts at most 128 materials.")
            )
        if values is not None and len(values) > 256:
            raise AssistantToolError(
                _tr("copy_mdf_values accepts at most 256 value selectors.")
            )

        material_names: dict[str, str] | None = None
        if materials:
            material_names = {}
            for material in materials:
                if not isinstance(material, str) or not material.strip():
                    raise AssistantToolError(
                        _tr("MDF copy material names must not be empty.")
                    )
                material_names.setdefault(
                    self._copy_identity(material),
                    material.strip(),
                )

        selected_sections: set[str] | None = None
        if sections:
            selected_sections = {
                str(section).strip().casefold() for section in sections
            }
            unknown = selected_sections.difference(_MDF_COPY_SECTIONS)
            if unknown:
                raise AssistantToolError(
                    _tr(
                        "Unknown MDF copy sections: {sections}",
                        sections=", ".join(sorted(unknown)),
                    )
                )

        selected_values: dict[str, Any] | None = None
        if values:
            selected_values = {}
            for value in values:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (str, int))
                    or isinstance(value, str)
                    and not value.strip()
                ):
                    raise AssistantToolError(
                        _tr(
                            "MDF copy value selectors must be non-empty names or integer indices."
                        )
                    )
                selected_values.setdefault(
                    self._copy_identity(value),
                    value,
                )

        return {
            "materials": material_names,
            "sections": selected_sections,
            "values": selected_values,
            "include_source_only": self._boolean(
                include_source_only,
                "include_source_only",
            ),
        }

    @staticmethod
    def _copy_value_path(
        material: str,
        section: str,
        value: Any | None = None,
        occurrence: int = 0,
    ) -> str:
        if section == "material":
            path = f"material[{material}]"
        else:
            path = (
                f"material[{material}].{section}"
                if material
                else section
            )
        if value is not None:
            path += f"[{value}]"
        if occurrence:
            path += f"#{occurrence + 1}"
        return path

    def _copy_row_identity(
        self,
        table: str,
        row: dict[str, Any],
        index: int,
    ) -> tuple[str, Any]:
        field = {
            "textures": "type",
            "parameters": "name",
            "gpu_buffers": "name",
        }.get(table)
        display = row.get(field, "") if field else index
        return self._copy_identity(display), display

    def _copy_row_groups(
        self,
        table: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, list[tuple[int, dict[str, Any], Any]]]:
        groups: dict[str, list[tuple[int, dict[str, Any], Any]]] = {}
        for index, row in enumerate(rows):
            key, display = self._copy_row_identity(table, row, index)
            groups.setdefault(key, []).append((index, row, display))
        return groups

    @staticmethod
    def _copy_matched_row(
        table: str,
        source: dict[str, Any],
        target: dict[str, Any],
        path: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        desired = copy.deepcopy(target)
        mismatch = None
        if table == "textures":
            desired["path"] = source["path"]
            desired["locked"] = source["locked"]
        elif table == "parameters":
            source_count = int(source["component_count"])
            target_count = int(target["component_count"])
            copied_count = min(source_count, target_count)
            target_values = list(target["values"])
            target_values[:copied_count] = list(source["values"])[:copied_count]
            desired["values"] = target_values
            desired["locked"] = source["locked"]
            if source_count != target_count:
                mismatch = {
                    "path": path,
                    "reason": "component_count_mismatch",
                    "source_component_count": source_count,
                    "destination_component_count": target_count,
                    "components_copied": copied_count,
                }
        elif table == "gpu_buffers":
            desired["data"] = source["data"]
        else:
            desired = copy.deepcopy(source)
        return desired, mismatch

    def _plan_two_way_mdf_overlay(
        self,
        source: dict[str, Any],
        target: dict[str, Any],
        scope: dict[str, Any],
    ) -> dict[str, Any]:
        material_selectors = scope["materials"]
        section_selectors = scope["sections"]
        value_selectors = scope["values"]
        include_source_only = scope["include_source_only"]
        matched_materials: set[str] = set()
        matched_values: set[str] = set()
        changes: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        source_only: list[str] = []
        source_only_added: list[str] = []
        destination_only: list[str] = []
        incompatible: list[dict[str, Any]] = []

        def section_selected(section: str) -> bool:
            return (
                section_selectors is None
                or section in section_selectors
            )

        def value_selected(value: Any, *, source_value: bool) -> bool:
            key = self._copy_identity(value)
            if value_selectors is not None and key not in value_selectors:
                return False
            if source_value and value_selectors is not None:
                matched_values.add(key)
            return True

        def material_selected(key: str) -> bool:
            return material_selectors is None or key in material_selectors

        def match_source_material_values(material: dict[str, Any]):
            if value_selectors is None:
                return
            for section in ("overview", "flags"):
                if section_selected(section):
                    for field in material[section]:
                        value_selected(field, source_value=True)
            for table in (
                "textures",
                "parameters",
                "gpu_buffers",
                "shader_lods",
            ):
                if section_selected(table):
                    for index, row in enumerate(material.get(table, [])):
                        _key, display = self._copy_row_identity(
                            table,
                            row,
                            index,
                        )
                        value_selected(display, source_value=True)

        def append_change(
            path: tuple[str, ...],
            source_value: Any,
            target_value: Any,
        ):
            if _merge_equal(source_value, target_value):
                return
            changes.append(
                {
                    "path": path,
                    "baseline": target_value,
                    "modded": source_value,
                    "target": target_value,
                }
            )

        if (
            section_selected("header")
            and value_selected(
                "meshlet_material",
                source_value=True,
            )
        ):
            append_change(
                ("meshlet_material",),
                source["meshlet_material"],
                target["meshlet_material"],
            )

        source_materials = source["materials"]
        target_materials = target["materials"]
        for material_key, source_material in source_materials.items():
            if not material_selected(material_key):
                continue
            matched_materials.add(material_key)
            target_material = target_materials.get(material_key)
            material_path = self._copy_value_path(
                material_key,
                "material",
            )
            if target_material is None:
                match_source_material_values(source_material)
                can_add_whole_material = (
                    include_source_only
                    and section_selectors is None
                    and value_selectors is None
                )
                if can_add_whole_material:
                    append_change(
                        ("materials", material_key),
                        source_material,
                        _MISSING,
                    )
                    source_only_added.append(material_path)
                else:
                    source_only.append(material_path)
                    if include_source_only:
                        incompatible.append(
                            {
                                "path": material_path,
                                "reason": "partial_material_addition_is_unsupported",
                            }
                        )
                continue

            for section in ("overview", "flags"):
                if not section_selected(section):
                    continue
                source_fields = source_material[section]
                target_fields = target_material[section]
                for field, source_value in source_fields.items():
                    if not value_selected(field, source_value=True):
                        continue
                    path_text = self._copy_value_path(
                        target_material["name"],
                        section,
                        field,
                    )
                    if field not in target_fields:
                        source_only.append(path_text)
                        continue
                    append_change(
                        ("materials", material_key, section, field),
                        source_value,
                        target_fields[field],
                    )
                for field in target_fields.keys() - source_fields.keys():
                    if value_selected(field, source_value=False):
                        destination_only.append(
                            self._copy_value_path(
                                target_material["name"],
                                section,
                                field,
                            )
                        )

            for table in (
                "textures",
                "parameters",
                "gpu_buffers",
                "shader_lods",
            ):
                if not section_selected(table):
                    continue
                source_rows = source_material.get(table, [])
                target_rows = target_material.get(table)
                source_groups = self._copy_row_groups(table, source_rows)
                if target_rows is None:
                    for group in source_groups.values():
                        for occurrence, (_index, _row, display) in enumerate(group):
                            if value_selected(display, source_value=True):
                                path_text = self._copy_value_path(
                                    target_material["name"],
                                    table,
                                    display,
                                    occurrence,
                                )
                                source_only.append(path_text)
                                if include_source_only:
                                    incompatible.append(
                                        {
                                            "path": path_text,
                                            "reason": "section_unsupported_by_destination",
                                        }
                                    )
                    continue

                target_groups = self._copy_row_groups(table, target_rows)
                for identity, source_group in source_groups.items():
                    display = source_group[0][2]
                    if not value_selected(display, source_value=True):
                        continue
                    target_group = target_groups.get(identity, [])
                    shared_count = min(len(source_group), len(target_group))
                    for occurrence in range(shared_count):
                        _source_index, source_row, source_display = (
                            source_group[occurrence]
                        )
                        target_index, target_row, _target_display = (
                            target_group[occurrence]
                        )
                        path_text = self._copy_value_path(
                            target_material["name"],
                            table,
                            source_display,
                            occurrence,
                        )
                        desired, mismatch = self._copy_matched_row(
                            table,
                            source_row,
                            target_row,
                            path_text,
                        )
                        if mismatch is not None:
                            incompatible.append(mismatch)
                        if desired != target_row:
                            rows.append(
                                {
                                    "path": path_text,
                                    "material": target_material["name"],
                                    "table": table,
                                    "index": target_index,
                                    "row": desired,
                                }
                            )

                    for occurrence in range(shared_count, len(source_group)):
                        _source_index, source_row, source_display = (
                            source_group[occurrence]
                        )
                        path_text = self._copy_value_path(
                            target_material["name"],
                            table,
                            source_display,
                            occurrence,
                        )
                        if include_source_only:
                            rows.append(
                                {
                                    "path": path_text,
                                    "material": target_material["name"],
                                    "table": table,
                                    "index": -1,
                                    "row": copy.deepcopy(source_row),
                                }
                            )
                            source_only_added.append(path_text)
                        else:
                            source_only.append(path_text)

                for identity, target_group in target_groups.items():
                    source_group = source_groups.get(identity, [])
                    display = target_group[0][2]
                    if not value_selected(display, source_value=False):
                        continue
                    for occurrence in range(len(source_group), len(target_group)):
                        destination_only.append(
                            self._copy_value_path(
                                target_material["name"],
                                table,
                                target_group[occurrence][2],
                                occurrence,
                            )
                        )

        for material_key, target_material in target_materials.items():
            if (
                material_key not in source_materials
                and material_selected(material_key)
                and section_selectors is None
                and value_selectors is None
            ):
                destination_only.append(
                    self._copy_value_path(
                        target_material["name"],
                        "material",
                    )
                )

        selection_misses = []
        if material_selectors is not None:
            selection_misses.extend(
                f"material:{material_selectors[key]}"
                for key in material_selectors.keys() - matched_materials
            )
        if value_selectors is not None:
            selection_misses.extend(
                f"value:{value_selectors[key]}"
                for key in value_selectors.keys() - matched_values
            )
        return {
            "changes": changes,
            "rows": rows,
            "source_only": source_only,
            "source_only_added": source_only_added,
            "destination_only": destination_only,
            "incompatible": incompatible,
            "selection_misses": selection_misses,
        }

    def _apply_overlay_row(self, operation: dict[str, Any]):
        row = operation["row"]
        material = operation["material"]
        index = operation["index"]
        table = operation["table"]
        if table == "textures":
            self._upsert_mdf_texture(
                material,
                index,
                row["type"],
                row["path"],
                row["locked"],
            )
        elif table == "parameters":
            self._upsert_mdf_parameter(
                material,
                index,
                row["name"],
                row["component_count"],
                row["values"],
                row["locked"],
            )
        elif table == "gpu_buffers":
            self._upsert_mdf_gpu_buffer(
                material,
                index,
                row["name"],
                row["data"],
            )
        else:
            self._upsert_mdf_shader_lod(
                material,
                index,
                row["texture_table"],
                row["byte_buffer_table"],
            )

    def _overlay_mdf_values(
        self,
        source_viewer: MdfViewer,
        target_tab,
        target_viewer: MdfViewer,
        *,
        scope: dict[str, Any] | None = None,
        include_source_only: bool = False,
        confirm_apply: Callable[[int], bool] | None = None,
    ) -> dict[str, Any]:
        source = self._migration_snapshot(source_viewer)
        target = self._migration_snapshot(target_viewer)
        if scope is None:
            scope = self._copy_scope(
                None,
                None,
                None,
                include_source_only,
            )
        plan = self._plan_two_way_mdf_overlay(source, target, scope)
        changes = plan["changes"]
        unsupported = [
            change
            for change in changes
            if not self._migration_change_supported(target, target_viewer, change)
        ]
        supported = [change for change in changes if change not in unsupported]
        planned_changes = len(supported) + len(plan["rows"])
        confirmation_shown = bool(planned_changes and confirm_apply is not None)
        cancelled = (
            confirmation_shown and not confirm_apply(planned_changes)
        )
        if cancelled:
            applied, failures = [], []
        else:
            applied, failures = (
                self._apply_migration_changes(
                    target_tab,
                    target_viewer,
                    supported,
                )
                if supported
                else ([], [])
            )
        if plan["rows"] and not cancelled:
            if not supported and not self._focus_open_tab(target_tab):
                raise AssistantToolError(
                    _tr("REasy could not activate the destination MDF.")
                )
            for operation in plan["rows"]:
                try:
                    self._apply_overlay_row(operation)
                    applied.append(operation["path"])
                except Exception as exc:
                    failures.append(
                        {
                            "path": operation["path"],
                            "error": str(exc),
                        }
                    )
        if not cancelled:
            failures.extend(
                {
                    "path": self._migration_path_text(change["path"]),
                    "error": _tr(
                        "The destination MDF version does not support this source value."
                    ),
                }
                for change in unsupported
            )
        detail_limit = 100
        details = (
            plan["source_only"],
            plan["source_only_added"],
            plan["destination_only"],
            plan["incompatible"],
            plan["selection_misses"],
            failures,
        )
        source_only_added = [
            path
            for path in plan["source_only_added"]
            if path in applied
        ]
        return {
            "cancelled": cancelled,
            "confirmation_shown": confirmation_shown,
            "changes_planned": planned_changes,
            "changes_applied": len(applied),
            "unsupported_changes": len(unsupported),
            "failures": failures,
            "source_only_value_count": len(plan["source_only"]),
            "source_only_values": plan["source_only"][:detail_limit],
            "source_only_values_added_count": len(
                source_only_added
            ),
            "source_only_values_added": source_only_added[:detail_limit],
            "destination_only_value_count": len(
                plan["destination_only"]
            ),
            "destination_only_values_preserved": plan["destination_only"][
                :detail_limit
            ],
            "incompatible_value_count": len(plan["incompatible"]),
            "incompatible_values": plan["incompatible"][:detail_limit],
            "selection_misses": plan["selection_misses"][:detail_limit],
            "details_truncated": any(
                len(items) > detail_limit for items in details
            ),
        }

    @staticmethod
    def _copy_endpoint_kind(payload: dict[str, Any]) -> str:
        if payload.get("pak_backed"):
            return _tr(_PAK_BACKED_FILE)
        if payload.get("project"):
            return _tr(_PROJECT_FILE)
        return _tr(_OPEN_FILE)

    def _show_file_copy_confirmation(
        self,
        source: dict[str, Any],
        destination: dict[str, Any],
        change_count: int,
    ) -> AiChangeDecision:
        def display_path(payload: dict[str, Any]) -> str:
            return str(
                payload.get("source_path")
                or payload.get("path")
                or payload.get("title")
                or payload["id"]
            )

        return self._action_policy.show_confirmation(
            _tr(_CONFIRM_COPY_TITLE),
            _tr(
                _CONFIRM_COPY_MESSAGE,
                source_kind=self._copy_endpoint_kind(source),
                source_id=source["id"],
                source_path=display_path(source),
                destination_kind=self._copy_endpoint_kind(destination),
                destination_id=destination["id"],
                destination_path=display_path(destination),
                change_count=change_count,
            )
        )

    def _confirm_file_copy_for_request(
        self,
        source: dict[str, Any],
        destination: dict[str, Any],
        change_count: int,
    ) -> bool:
        return self._action_policy.request(
            lambda: self._confirm_file_copy(
                source,
                destination,
                change_count,
            )
        )

    def _copy_mdf_values(
        self,
        source: str,
        destination: str,
        materials: list[str] | None = None,
        sections: list[str] | None = None,
        values: list[Any] | None = None,
        include_source_only: bool = False,
    ) -> dict[str, Any]:
        scope = self._copy_scope(
            materials,
            sections,
            values,
            include_source_only,
        )
        source_tab, _source_session, source_payload, source_viewer = (
            self._resolve_or_open_mdf_tab(source)
        )
        target_tab, _target_session, target_payload, target_viewer = (
            self._resolve_or_open_mdf_tab(destination)
        )
        if source_tab is target_tab:
            raise AssistantToolError(
                _tr(
                    "The MDF source and destination must be different files."
                )
            )

        target_version = target_viewer._current_file_version()
        confirm_apply = (
            None
            if self._action_policy.allows_all_changes
            else partial(
                self._confirm_file_copy_for_request,
                source_payload,
                target_payload,
            )
        )
        result = self._overlay_mdf_values(
            source_viewer,
            target_tab,
            target_viewer,
            scope=scope,
            confirm_apply=confirm_apply,
        )
        if result["cancelled"]:
            status = "cancelled"
        elif result["failures"]:
            status = (
                "partial"
                if result["changes_applied"]
                else "failed"
            )
        elif any(
            (
                result["source_only_value_count"],
                result["incompatible_value_count"],
                result["selection_misses"],
            )
        ):
            status = (
                "partial"
                if result["changes_applied"]
                else "no_matching_values"
            )
        elif result["changes_applied"]:
            status = "updated"
        elif result["destination_only_value_count"]:
            status = "destination_only_values_preserved"
        else:
            status = "already_up_to_date"
        pak_backed = bool(target_payload.get("pak_backed"))
        return {
            "status": status,
            "source": source_payload["id"],
            "source_file": (
                source_payload.get("source_path")
                or source_payload.get("path")
            ),
            "destination": target_payload["id"],
            "destination_file": (
                target_payload.get("source_path")
                or target_payload.get("path")
            ),
            "destination_pak_backed": pak_backed,
            "editable_in_memory": True,
            "save_requires_copy": pak_backed,
            "destination_version": target_version,
            "destination_version_preserved": (
                target_viewer._current_file_version() == target_version
            ),
            "modified": bool(target_viewer.modified),
            "saved": False,
            **result,
        }

    def _opened_mdf_reference(self, path: Path):
        existing = {id(tab) for tab, _, _ in self._open_tab_records()}
        tab, _, _, viewer = self._resolve_or_open_mdf_tab(str(path))
        return tab, viewer, id(tab) not in existing

    def _close_batch_tab(self, tab, opened_by_tool: bool):
        close_tab = getattr(self.app, "_close_tab_object", None)
        if (
            opened_by_tool
            and not bool(getattr(tab, "modified", False))
            and callable(close_tab)
        ):
            try:
                close_tab(tab, record_history=False)
            except RuntimeError:
                pass

    def _restore_batch_focus(self, tab):
        if tab is None:
            return
        try:
            self._focus_open_tab(tab)
        except RuntimeError:
            pass

    def _update_mdf_output(
        self,
        source_path: Path,
        output_path: Path,
        include_source_only: bool = False,
    ) -> dict[str, Any]:
        source_tab = target_tab = None
        source_opened = target_opened = saved = False
        try:
            source_tab, source_viewer, source_opened = (
                self._opened_mdf_reference(source_path)
            )
            target_tab, target_viewer, target_opened = (
                self._opened_mdf_reference(output_path)
            )
            result = self._overlay_mdf_values(
                source_viewer,
                target_tab,
                target_viewer,
                include_source_only=include_source_only,
            )
            saved = not result["changes_applied"] or bool(target_tab.direct_save())
            if not saved:
                raise AssistantToolError(
                    _tr("REasy could not save updated MDF: {path}", path=output_path)
                )
            return {**result, "saved": True}
        finally:
            if target_tab is not None and saved:
                self._close_batch_tab(target_tab, target_opened)
            if source_tab is not None:
                self._close_batch_tab(source_tab, source_opened)

    def _update_mod_folder(
        self,
        mod_folder: str,
        originals_folder: str,
        output_folder: str = "",
        include_source_only: bool = False,
    ) -> dict[str, Any]:
        original_active_tab = self.app.get_active_tab()
        steps = self._update_mod_folder_steps(
            mod_folder,
            originals_folder,
            output_folder,
            include_source_only,
        )
        try:
            while True:
                try:
                    next(steps)
                except StopIteration as completed:
                    return completed.value
        finally:
            self._restore_batch_focus(original_active_tab)

    def _update_mod_folder_steps(
        self,
        mod_folder: str,
        originals_folder: str,
        output_folder: str = "",
        include_source_only: bool = False,
    ):
        self._require_update_project_paks()
        output_state: dict[str, Any] = {}
        try:
            result = yield from self._build_mod_output_steps(
                mod_folder,
                originals_folder,
                output_folder,
                include_source_only,
                output_state,
            )
            staging = output_state["staging"]
            output = output_state["output"]
            self._publish_mod_output(staging, output)
            result["output_folder"] = str(output)
            return result
        finally:
            staging = output_state.get("staging")
            if isinstance(staging, Path) and staging.exists():
                try:
                    shutil.rmtree(staging)
                except OSError:
                    pass

    def _build_mod_output_steps(
        self,
        mod_folder: str,
        originals_folder: str,
        output_folder: str,
        include_source_only: bool,
        output_state: dict[str, Any],
    ):
        include_source_only = self._boolean(
            include_source_only,
            "include_source_only",
        )
        mod_root = self._folder_path(mod_folder, "mod_folder")
        originals_root = self._folder_path(originals_folder, "originals_folder")
        if (
            mod_root == originals_root
            or mod_root in originals_root.parents
            or originals_root in mod_root.parents
        ):
            raise AssistantToolError(
                _tr(
                    "mod_folder and originals_folder must be different and must not contain one another."
                )
            )

        mod_files = yield from self._scan_folder_steps(mod_root)
        if not mod_files:
            raise AssistantToolError(_tr("The mod folder contains no files."))
        original_files = yield from self._scan_folder_steps(
            originals_root
        )
        originals = [
            path for path in original_files
            if _is_versioned_mdf(path)
        ]
        output = self._resolve_mod_output_folder(
            mod_root, originals_root, output_folder
        )
        output_root = self._create_mod_staging_folder(output)
        output_state.update(output=output, staging=output_root)
        exact: dict[str, list[Path]] = {}
        logical: dict[str, list[Path]] = {}
        for path in originals:
            relative = path.relative_to(originals_root)
            exact.setdefault(relative.as_posix().casefold(), []).append(path)
            logical.setdefault(_mdf_relative_key(relative), []).append(path)

        copied = matched = updated = changes_applied = unsupported_changes = 0
        source_only_values = destination_only_values = incompatible_values = 0
        unmatched: list[str] = []
        ambiguous: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        value_warnings: list[dict[str, Any]] = []
        updated_files: list[str] = []
        used_outputs: set[str] = set()

        def copy_file(source: Path, destination: Path, label: str) -> bool:
            nonlocal copied
            try:
                self._copy_mod_file(source, destination)
                copied += 1
                return True
            except OSError as exc:
                failures.append(
                    {
                        "file": label,
                        "error": _tr("Could not copy file: {error}", error=exc),
                    }
                )
                return False

        for index, source in enumerate(mod_files):
            relative = source.relative_to(mod_root)
            relative_text = relative.as_posix()
            yield {
                "stage": "updating_mod_folder",
                "current": index + 1,
                "completed": index,
                "total": len(mod_files),
                "item": relative_text,
            }
            if not _is_versioned_mdf(source):
                copy_file(source, output_root / relative, relative_text)
                continue

            candidates = (
                exact.get(relative_text.casefold())
                or logical.get(_mdf_relative_key(relative), [])
            )
            if len(candidates) != 1:
                copy_file(source, output_root / relative, relative_text)
                if candidates:
                    ambiguous.append(
                        {
                            "file": relative_text,
                            "candidates": [
                                path.relative_to(originals_root).as_posix()
                                for path in candidates[:10]
                            ],
                        }
                    )
                else:
                    unmatched.append(relative_text)
                continue

            original = candidates[0]
            output_relative = original.relative_to(originals_root)
            output_text = output_relative.as_posix()
            output_key = output_text.casefold()
            if output_key in used_outputs:
                failures.append(
                    {
                        "file": relative_text,
                        "error": _tr(
                            "Multiple mod MDF files matched the same output path."
                        ),
                    }
                )
                continue
            used_outputs.add(output_key)
            matched += 1
            output_path = output_root / output_relative
            if not copy_file(original, output_path, relative_text):
                continue
            try:
                result = self._update_mdf_output(
                    source,
                    output_path,
                    include_source_only,
                )
                changes_applied += result["changes_applied"]
                unsupported_changes += result["unsupported_changes"]
                source_only_values += result["source_only_value_count"]
                destination_only_values += result[
                    "destination_only_value_count"
                ]
                incompatible_values += result["incompatible_value_count"]
                if (
                    result["source_only_value_count"]
                    or result["incompatible_value_count"]
                ):
                    value_warnings.append(
                        {
                            "file": relative_text,
                            "source_only_values": result[
                                "source_only_values"
                            ],
                            "incompatible_values": result[
                                "incompatible_values"
                            ],
                        }
                    )
                if result["failures"]:
                    failures.append(
                        {
                            "file": relative_text,
                            "output": output_text,
                            "details": result["failures"],
                        }
                    )
                else:
                    updated += 1
                    updated_files.append(output_text)
            except Exception as exc:
                failures.append(
                    {
                        "file": relative_text,
                        "output": output_text,
                        "error": str(exc),
                    }
                )

        detail_limit = 100
        details = (
            unmatched,
            ambiguous,
            failures,
            value_warnings,
            updated_files,
        )
        return {
            "output_folder": str(output_root),
            "mod_folder": str(mod_root),
            "originals_folder": str(originals_root),
            "files_scanned": len(mod_files),
            "files_copied": copied,
            "mdf_pairs_matched": matched,
            "mdf_files_updated": updated,
            "changes_applied": changes_applied,
            "unsupported_changes": unsupported_changes,
            "source_only_value_count": source_only_values,
            "destination_only_value_count": destination_only_values,
            "incompatible_value_count": incompatible_values,
            "value_warnings": value_warnings[:detail_limit],
            "unmatched_mdf_files": unmatched[:detail_limit],
            "ambiguous_mdf_matches": ambiguous[:detail_limit],
            "failures": failures[:detail_limit],
            "updated_mdf_files": updated_files[:detail_limit],
            "details_truncated": any(
                len(items) > detail_limit for items in details
            ),
            "complete": not any(
                (
                    unmatched,
                    ambiguous,
                    failures,
                    unsupported_changes,
                    source_only_values,
                    incompatible_values,
                )
            ),
            "source_folders_untouched": True,
        }

    def _batch_edit_files(
        self,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._run_incremental_steps(
            self._batch_edit_files_steps(actions)
        )

    def _batch_edit_files_steps(
        self,
        actions: list[dict[str, Any]],
    ):
        if not isinstance(actions, list) or not actions:
            raise AssistantToolError(
                _tr(
                    "Invalid arguments for tool: {name}",
                    name="batch_edit_files",
                )
            )

        results: list[dict[str, Any]] = []
        applied_count = 0
        successful_count = 0
        required = {"tab", "tool", "arguments"}
        for index, action in enumerate(actions):
            requested_tab = (
                str(action.get("tab") or "").strip()
                if isinstance(action, dict)
                else ""
            )
            yield {
                "stage": "editing",
                "current": index + 1,
                "completed": index,
                "total": len(actions),
                "item": requested_tab,
            }
            tool_name = ""
            try:
                if not isinstance(action, dict) or set(action) != required:
                    raise AssistantToolError(
                        _tr(
                            "Invalid arguments for tool: {name}",
                            name="batch_edit_files",
                        )
                    )
                tool_name = str(action["tool"] or "").strip()
                definition = self.tool_definition(tool_name)
                if definition is None or not definition.mutation:
                    raise AssistantToolError(
                        _tr("Unknown tool: {name}", name=tool_name)
                    )
                arguments = action["arguments"]
                if not isinstance(arguments, dict):
                    raise AssistantToolError(
                        _tr("Tool arguments must be a JSON object.")
                    )
                call_arguments = dict(arguments)
                call_arguments["tab"] = requested_tab
                edit_result = self.execute(tool_name, call_arguments)
            except Exception as exc:
                results.append(
                    {
                        "index": index,
                        "tool": tool_name or None,
                        "requested_tab": requested_tab or None,
                        "success": False,
                        "error": str(exc),
                    }
                )
                continue

            successful_count += 1
            changes_applied = edit_result.get("changes_applied")
            applied = not (
                isinstance(changes_applied, int)
                and not isinstance(changes_applied, bool)
                and changes_applied == 0
            )
            applied_count += int(applied)
            results.append(
                {
                    "index": index,
                    "tool": tool_name,
                    "success": True,
                    "applied": applied,
                    **edit_result,
                }
            )

        failed_count = len(actions) - successful_count
        return {
            "status": (
                "failed"
                if not successful_count
                else "partial"
                if failed_count
                else "no_changes"
                if not applied_count
                else "completed"
            ),
            "requested_count": len(actions),
            "successful_count": successful_count,
            "applied_count": applied_count,
            "failed_count": failed_count,
            "complete": failed_count == 0,
            "results": results,
        }

    def _select_mdf_material_tool(
        self,
        material: str,
        section: str = "overview",
    ) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        index = self._select_material(viewer, material, section)
        self._action_feedback.pulse_table_row(
            viewer.materials_table,
            index,
        )
        return {"selected_material": viewer.handler.mdf.materials[index].header.mat_name, "section": section}

    def _edit_mdf_header(self, changes: dict[str, Any]) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        if not isinstance(changes, dict) or not changes:
            raise AssistantToolError(_tr("No MDF header changes were supplied."))
        unknown = set(changes) - {"version", "meshlet_material"}
        if unknown:
            raise AssistantToolError(
                _tr(
                    "Unsupported MDF header fields: {fields}",
                    fields=sorted(unknown),
                )
            )
        version = (
            self._integer(changes["version"], "version", 0, 0x7FFF)
            if "version" in changes
            else None
        )
        meshlet = (
            self._boolean(changes["meshlet_material"], "meshlet_material")
            if "meshlet_material" in changes
            else None
        )
        if meshlet is not None and not hasattr(viewer, "meshlet_check"):
            raise AssistantToolError(
                _tr(
                    "This REasy build does not expose the meshlet-material control."
                )
            )

        if version is not None:
            viewer.version_edit.setText(str(version))
            self._action_feedback.pulse_widget(viewer.version_edit)
        if meshlet is not None:
            viewer.meshlet_check.setChecked(meshlet)
            self._action_feedback.pulse_widget(viewer.meshlet_check)
        return {"changed": list(changes), "version": viewer._current_file_version()}

    def _add_mdf_material(self, name: str) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        clean_name = str(name).strip()
        if not clean_name:
            raise AssistantToolError(_tr("Material name cannot be empty."))
        if viewer._check_duplicate_material_name(clean_name):
            raise AssistantToolError(
                _tr(
                    "A material named '{name}' already exists.",
                    name=clean_name,
                )
            )
        if viewer.filter_edit.text():
            viewer.filter_edit.clear()
        new_index = len(viewer.handler.mdf.materials)
        viewer.add_btn.click()
        self._select_material(viewer, new_index, "overview")
        viewer.matname_edit.setText(clean_name)
        self._action_feedback.pulse_table_row(
            viewer.materials_table,
            new_index,
        )
        return {"added_material": clean_name, "index": new_index}

    def _delete_mdf_material(self, material: str) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        index = self._select_material(viewer, material, "overview")
        name = viewer.handler.mdf.materials[index].header.mat_name
        self._action_feedback.pulse_table_row(
            viewer.materials_table,
            index,
        )
        viewer.del_btn.click()
        if viewer.handler.mdf.materials:
            self._select_material(viewer, min(index, len(viewer.handler.mdf.materials) - 1), "overview")
        return {"deleted_material": name, "index": index}

    def _edit_mdf_overview(self, material: str, changes: dict[str, Any]) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        material_index = self._resolve_material(viewer, material)
        if not isinstance(changes, dict) or not changes:
            raise AssistantToolError(_tr("No overview changes were supplied."))
        allowed = {"name", "mmtr_path", "shader_type", "bake_texture_array_size", "unknown_64"}
        unknown = set(changes) - allowed
        if unknown:
            raise AssistantToolError(
                _tr(
                    "Unsupported overview fields: {fields}",
                    fields=sorted(unknown),
                )
            )

        name = None
        if "name" in changes:
            name = str(changes["name"])
            if viewer._check_duplicate_material_name(name, exclude_index=material_index):
                raise AssistantToolError(
                    _tr(
                        "A material named '{name}' already exists.",
                        name=name,
                    )
                )

        mmtr_path = str(changes["mmtr_path"]) if "mmtr_path" in changes else None
        shader_index = None
        if "shader_type" in changes:
            shader = changes["shader_type"]
            if isinstance(shader, str) and not shader.strip().lstrip("-").isdigit():
                matches = [
                    index
                    for index, name in enumerate(viewer._shader_names)
                    if name.casefold() == shader.strip().casefold()
                ]
                if not matches:
                    raise AssistantToolError(
                        _tr("Unknown shader type: {shader}", shader=shader)
                    )
                shader_index = matches[0]
            else:
                shader_index = int(shader)
            if not 0 <= shader_index < viewer.shader_combo.count():
                raise AssistantToolError(
                    _tr(
                        "Shader index out of range: {index}",
                        index=shader_index,
                    )
                )

        version = viewer._current_file_version()
        bake_texture_array_size = None
        if "bake_texture_array_size" in changes:
            if version < 31:
                raise AssistantToolError(
                    _tr(
                        "BakeTextureArraySize requires MDF version 31 or newer."
                    )
                )
            bake_texture_array_size = self._integer(
                changes["bake_texture_array_size"],
                "bake_texture_array_size",
                0,
                0x7FFFFFFF,
            )

        unknown_64 = None
        if "unknown_64" in changes:
            if version != 6 and version < 51:
                raise AssistantToolError(
                    _tr(
                        "The editable 64-bit unknown field exists only in MDF "
                        "version 6 or 51+."
                    )
                )
            unknown_64 = self._uint64(changes["unknown_64"], "unknown_64")

        self._select_material(viewer, material_index, "overview")
        if name is not None:
            viewer.matname_edit.setText(name)
            self._action_feedback.pulse_widget(viewer.matname_edit)
        if mmtr_path is not None:
            viewer.mmtr_edit.setText(mmtr_path)
            self._action_feedback.pulse_widget(viewer.mmtr_edit)
        if shader_index is not None:
            viewer.shader_combo.setCurrentIndex(shader_index)
            self._action_feedback.pulse_widget(viewer.shader_combo)
        if bake_texture_array_size is not None:
            viewer.bake_texture_spin.setValue(bake_texture_array_size)
            self._action_feedback.pulse_widget(viewer.bake_texture_spin)
        if unknown_64 is not None:
            viewer.ukn_edit.setText(str(unknown_64))
            self._action_feedback.pulse_widget(viewer.ukn_edit)
        return {"material": viewer.matname_edit.text(), "changed": list(changes)}

    def _edit_mdf_flags(self, material: str, changes: dict[str, Any]) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        index = self._resolve_material(viewer, material)
        if not isinstance(changes, dict) or not changes:
            raise AssistantToolError(_tr("No flag changes were supplied."))
        checkboxes = {
            name.casefold(): (name, checkbox)
            for name, checkbox in [
                *zip(viewer._flags1_names, viewer.flags1_checks),
                ("TransparentZPostPassEnable", viewer.transparent_zpostpass_check),
                *zip(viewer._flags2_names, viewer.flags2_checks),
                *zip(viewer._flags3_names, viewer.flags3_checks),
            ]
        }
        numeric = {"tessellation", "phong", "transparent_priority_bias"}
        unknown = [
            name for name in changes if name.casefold() not in checkboxes and name.casefold() not in numeric
        ]
        if unknown:
            raise AssistantToolError(
                _tr("Unknown material flags: {flags}", flags=unknown)
            )
        version = viewer._current_file_version()
        version31_names = {
            "transparentzpostpassenable",
            *(name.casefold() for name in viewer._flags3_names),
        }
        validated: dict[str, Any] = {}
        for name, value in changes.items():
            folded = name.casefold()
            if folded in checkboxes:
                canonical, _checkbox = checkboxes[folded]
                if version < 31 and folded in version31_names:
                    raise AssistantToolError(
                        _tr(
                            "{field} requires MDF version 31 or newer.",
                            field=canonical,
                        )
                    )
                validated[folded] = self._boolean(value, canonical)
            elif folded == "tessellation":
                validated[folded] = self._integer(
                    value, "tessellation", 0, 31 if version >= 31 else 63
                )
            elif folded == "phong":
                validated[folded] = self._integer(value, "phong", 0, 255)
            elif folded == "transparent_priority_bias":
                if version < 31:
                    raise AssistantToolError(
                        _tr(
                            "TransparentPriorityBias requires MDF version 31 "
                            "or newer."
                        )
                    )
                validated[folded] = self._integer(
                    value, "transparent_priority_bias", -128, 127
                )

        self._select_material(viewer, index, "overview")
        for name in changes:
            folded = name.casefold()
            if folded in checkboxes:
                _canonical, checkbox = checkboxes[folded]
                checkbox.setChecked(validated[folded])
                self._action_feedback.pulse_widget(checkbox)
            elif folded == "tessellation":
                viewer.tess_spin.setValue(validated[folded])
                self._action_feedback.pulse_widget(viewer.tess_spin)
            elif folded == "phong":
                viewer.phong_spin.setValue(validated[folded])
                self._action_feedback.pulse_widget(viewer.phong_spin)
            elif folded == "transparent_priority_bias":
                viewer.transparent_priority_bias_spin.setValue(validated[folded])
                self._action_feedback.pulse_widget(
                    viewer.transparent_priority_bias_spin
                )
        return {
            "material": viewer.handler.mdf.materials[index].header.mat_name,
            "changed": list(changes),
        }

    def _upsert_mdf_texture(
        self,
        material: str,
        index: int,
        texture_type: str,
        texture_path: str,
        locked: bool,
    ) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        material_index = self._select_material(viewer, material, "textures")
        textures = viewer.handler.mdf.materials[material_index].textures
        row = self._table_row(
            index,
            len(textures),
            "Texture",
            allow_append=True,
        )
        locked_value = self._boolean(locked, "locked")
        if row == len(textures):
            viewer.tex_add_btn.click()
        viewer.textures_table.item(row, 0).setText(str(texture_type))
        viewer.textures_table.item(row, 1).setText(str(texture_path))
        viewer.textures_table.item(row, 2).setCheckState(
            Qt.Checked if locked_value else Qt.Unchecked
        )
        viewer.textures_table.selectRow(row)
        self._action_feedback.pulse_table_row(
            viewer.textures_table,
            row,
        )
        return {"material": material_index, "texture_index": row, "action": "upserted"}

    def _delete_mdf_texture(self, material: str, index: int) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        material_index = self._select_material(viewer, material, "textures")
        textures = viewer.handler.mdf.materials[material_index].textures
        row = self._table_row(index, len(textures), "Texture")
        viewer.textures_table.clearSelection()
        viewer.textures_table.selectRow(row)
        self._action_feedback.pulse_table_row(
            viewer.textures_table,
            row,
        )
        viewer.tex_del_btn.click()
        return {"material": material_index, "texture_index": row, "action": "deleted"}

    def _upsert_mdf_parameter(
        self,
        material: str,
        index: int,
        name: str,
        component_count: int,
        values: list[float],
        locked: bool,
    ) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        material_index = self._select_material(viewer, material, "parameters")
        parameters = viewer.handler.mdf.materials[material_index].parameters
        row = self._table_row(
            index,
            len(parameters),
            "Parameter",
            allow_append=True,
        )
        count = self._integer(component_count, "component_count", 1, 4)
        if not isinstance(values, list) or len(values) < count:
            raise AssistantToolError(
                _tr(
                    "At least {count} parameter values are required.",
                    count=count,
                )
            )
        parsed_values: list[float] = []
        for component in range(count):
            try:
                parsed = float(values[component])
            except (TypeError, ValueError) as exc:
                raise AssistantToolError(
                    _tr(
                        "Parameter component {component} must be a number.",
                        component=component,
                    )
                ) from exc
            if not math.isfinite(parsed):
                raise AssistantToolError(
                    _tr(
                        "Parameter component {component} must be finite.",
                        component=component,
                    )
                )
            parsed_values.append(parsed)
        locked_value = self._boolean(locked, "locked")

        if row == len(parameters):
            viewer.par_add_btn.click()
        table = viewer._get_or_create_params_table(material_index)
        table.item(row, 0).setText(str(name))
        table.item(row, 1).setText(str(count))
        table.item(row, 2).setCheckState(
            Qt.Checked if locked_value else Qt.Unchecked
        )
        for component in range(count):
            table.item(row, 3 + component).setText(str(parsed_values[component]))
        table.selectRow(row)
        self._action_feedback.pulse_table_row(table, row)
        return {"material": material_index, "parameter_index": row, "action": "upserted"}

    def _delete_mdf_parameter(self, material: str, index: int) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        material_index = self._select_material(viewer, material, "parameters")
        parameters = viewer.handler.mdf.materials[material_index].parameters
        row = self._table_row(index, len(parameters), "Parameter")
        table = viewer._get_or_create_params_table(material_index)
        table.clearSelection()
        table.selectRow(row)
        self._action_feedback.pulse_table_row(table, row)
        viewer.par_del_btn.click()
        return {"material": material_index, "parameter_index": row, "action": "deleted"}

    def _upsert_mdf_gpu_buffer(
        self, material: str, index: int, name: str, data: str
    ) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        material_index = self._select_material(viewer, material, "gpu_buffers")
        buffers = viewer.handler.mdf.materials[material_index].gpu_buffers
        row = self._table_row(
            index,
            len(buffers),
            "GPU-buffer",
            allow_append=True,
        )
        if row == len(buffers):
            viewer.gpbf_add_btn.click()
        viewer.gpbf_table.item(row, 0).setText(str(name))
        viewer.gpbf_table.item(row, 1).setText(str(data))
        viewer.gpbf_table.selectRow(row)
        self._action_feedback.pulse_table_row(
            viewer.gpbf_table,
            row,
        )
        return {"material": material_index, "gpu_buffer_index": row, "action": "upserted"}

    def _delete_mdf_gpu_buffer(self, material: str, index: int) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        material_index = self._select_material(viewer, material, "gpu_buffers")
        buffers = viewer.handler.mdf.materials[material_index].gpu_buffers
        row = self._table_row(index, len(buffers), "GPU-buffer")
        viewer.gpbf_table.clearSelection()
        viewer.gpbf_table.selectRow(row)
        self._action_feedback.pulse_table_row(
            viewer.gpbf_table,
            row,
        )
        viewer.gpbf_del_btn.click()
        return {"material": material_index, "gpu_buffer_index": row, "action": "deleted"}

    def _upsert_mdf_shader_lod(
        self,
        material: str,
        index: int,
        texture_table: list[int],
        byte_buffer_table: list[int],
    ) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        material_index = self._select_material(viewer, material, "shader_lods")
        redirects = viewer.handler.mdf.materials[material_index].shader_lod_redirects
        row = self._table_row(
            index,
            len(redirects),
            "Shader-LOD",
            allow_append=True,
        )
        if not isinstance(texture_table, list) or not isinstance(byte_buffer_table, list):
            raise AssistantToolError(
                _tr("Shader-LOD tables must be arrays of integers.")
            )
        parsed_texture_table = [
            self._integer(value, "texture_table value", -0x80000000, 0x7FFFFFFF)
            for value in texture_table
        ]
        parsed_byte_buffer_table = [
            self._integer(value, "byte_buffer_table value", -0x80000000, 0x7FFFFFFF)
            for value in byte_buffer_table
        ]

        if row == len(redirects):
            viewer.shaderLOD_count_spin.setValue(len(redirects) + 1)
        viewer.shaderLODRedirects_table.item(row, 0).setText(
            ",".join(str(value) for value in parsed_texture_table)
        )
        viewer.shaderLODRedirects_table.item(row, 1).setText(
            ",".join(str(value) for value in parsed_byte_buffer_table)
        )
        viewer.shaderLODRedirects_table.selectRow(row)
        self._action_feedback.pulse_table_row(
            viewer.shaderLODRedirects_table,
            row,
        )
        return {"material": material_index, "shader_lod_index": row, "action": "upserted"}

    def _delete_mdf_shader_lod(self, material: str, index: int) -> dict[str, Any]:
        _tab, viewer = self._active_mdf()
        material_index = self._select_material(viewer, material, "shader_lods")
        redirects = viewer.handler.mdf.materials[material_index].shader_lod_redirects
        row = self._table_row(index, len(redirects), "Shader-LOD")
        self._action_feedback.pulse_table_row(
            viewer.shaderLODRedirects_table,
            row,
        )
        for target in range(row, len(redirects) - 1):
            texture_table, byte_buffer_table = redirects[target + 1]
            viewer.shaderLODRedirects_table.item(target, 0).setText(
                ",".join(str(int(value)) for value in texture_table)
            )
            viewer.shaderLODRedirects_table.item(target, 1).setText(
                ",".join(str(int(value)) for value in byte_buffer_table)
            )
        viewer.shaderLOD_count_spin.setValue(len(redirects) - 1)
        return {"material": material_index, "shader_lod_index": row, "action": "deleted"}
