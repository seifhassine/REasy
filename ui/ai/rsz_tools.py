from __future__ import annotations

import copy
import json
import math
import re
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from itertools import islice
from typing import Any, Iterable

from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.rsz.rsz_data_types import (
    ArrayData,
    GameObjectRefData,
    GuidData,
    ObjectData,
    RawBytesData,
    ResourceData,
    StructData,
    UserDataData,
    get_type_class,
)
from file_handlers.rsz.rsz_file import RszFile
from ui.ai.file_migration import (
    migration_job_schema,
)
from ui.ai.pak_folder_migration import update_mod_folder_from_paks_steps
from ui.ai.rsz_migration import RszMigrationStrategy, load_type_registry
from ui.ai.rsz_update_analysis import (
    RszUpdateAnalysis,
    analyze_rsz_mod_folder_steps,
)
from ui.ai.tool_registry import (
    AiToolDefinition,
    AssistantToolError,
    tool as _tool,
    translate_tool_text as _tr,
)
from utils.enum_manager import EnumManager


RSZ_CAPABILITY = "rsz"
RSZ_EDIT_CAPABILITY = "rsz_edit"

RSZ_ASSISTANT_CAPABILITY_PROMPT = """\
RSZ read tools are enabled for USER, SCN, PFB, and headless RSZ data embedded
in formats such as RCOL. Inspect parsed in-memory data instead of guessing from
type names. Instance IDs are local to the returned segment ID; always preserve
both when following references into embedded userdata.

Use inspect_rsz for structure and exact values, describe_rsz_types for registry
definitions and enum meanings, search_rsz to locate types/fields/values, and
trace_rsz_references to explain object graphs and external file dependencies.
Resource results are exact RE Engine paths: use project or PAK tools to follow
them only when that source is within the user's request.
"""

RSZ_EDIT_ASSISTANT_CAPABILITY_PROMPT = """\
RSZ edit tools are enabled. edit_rsz performs typed edits against exact segment,
instance, and field paths returned by inspect_rsz. Prefer small targeted actions.
Use initialize_reference or delete_owned_reference only when the user asked for
a structural graph change; these reuse REasy's instance-ID remapping logic and
must be issued alone. Ordinary set/insert/delete/clear actions are atomic and do
not delete referenced instances unless delete_owned=true is explicit.

migrate_rsz_files is a low-level mechanical overlay for explicit file pairs;
use it only when the user specifically asks to transfer every compatible old
value without semantic AI selection. It cannot distinguish mod intent from an
obsolete vanilla default. Both registry JSON paths remain mandatory.

When the user supplies a mod folder and asks to update all RSZ files, call
analyze_rsz_mod_folder_update first. It recursively discovers USER, SCN, PFB,
RCOL, and WCC files and compares outdated-mod values with latest PAK values
without writing. Review every returned semantic change group, paging with
inspect_rsz_mod_folder_analysis when necessary. For each group, decide whether
it reflects the user's mod intent: apply_mod_value only with a concrete reason;
otherwise keep_latest. Treat renamed/missing fields, incompatible values, and
unmatched files as unresolved rather than guessing; inspect section="issues"
to research their exact paths and failure reasons. Then call
update_rsz_mod_folder with one reasoned decision per group. The apply step
hash-checks the analyzed inputs and validates every output. Do not enumerate or
open the directory with other tools. When no old-original files were supplied,
be explicit that identifying mod intent is semantic inference rather than a
provable three-way diff.

Completed RSZ migrations retain a post-update report. Use
inspect_file_update_report for later questions about exact latest-to-imported
value changes, imported collection elements, new latest-PAK structure that was
preserved, AI decisions that kept latest values, and unresolved paths.

Edits remain unsaved. Re-inspect after structural changes because instance IDs
can move, and only save when the user explicitly asks.
"""

_RSZ_PROMPT_HINT_RE = re.compile(
    r"(?:"
    r"\.user(?:\.\d+)?\b|\.scn(?:\.\d+)?\b|\.pfb(?:\.\d+)?\b|"
    r"\.rcol(?:\.\d+)?\b|\.wcc(?:\.\d+)?\b|"
    r"\b(?:rsz|usr|scn|pfb|rcol|headless\s+rsz)\b"
    r")",
    re.IGNORECASE,
)


def rsz_prompt_matches(prompt: str) -> bool:
    return bool(_RSZ_PROMPT_HINT_RE.search(str(prompt or "")))


def _edit_action_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "set",
                    "insert",
                    "delete",
                    "clear",
                    "initialize_reference",
                    "delete_owned_reference",
                ],
            },
            "segment": {
                "type": "string",
                "description": "Exact segment ID returned by inspect_rsz; defaults to main.",
            },
            "instance_id": {
                "type": "integer",
                "minimum": 0,
                "description": "Instance ID within segment.",
            },
            "path": {
                "type": "string",
                "description": "Exact field path, for example Settings.Speed or Entries[2].Value.",
            },
            "value": {
                "description": "Typed JSON value for set/insert, or an existing instance ID for a reference.",
            },
            "index": {
                "type": "integer",
                "minimum": 0,
                "description": "Array/struct index for insert or delete; insert defaults to append.",
            },
            "type_name": {
                "type": "string",
                "description": (
                    "Exact registry type when creating an object/userdata reference "
                    "or referenced array element."
                ),
            },
            "userdata_string": {
                "type": "string",
                "description": "Optional userdata string when creating normal RSZ userdata; defaults to type_name.",
            },
            "delete_owned": {
                "type": "boolean",
                "description": (
                    "For array delete only: also delete an exclusively owned "
                    "referenced instance and remap IDs."
                ),
            },
        },
        "required": ["operation", "instance_id", "path"],
        "additionalProperties": False,
    }


def rsz_tool_definitions() -> tuple[AiToolDefinition, ...]:
    tab = {
        "type": "string",
        "description": (
            "Optional exact open tab ID, path, or unambiguous title; defaults "
            "to the active RSZ-compatible tab."
        ),
    }
    targets = {
        "tabs": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 64,
            "description": "Optional open tab IDs, paths, or titles.",
        },
        "all_open": {
            "type": "boolean",
            "description": "Inspect every open RSZ-compatible editor; defaults to false.",
        },
    }
    return (
        _tool(
            "inspect_rsz",
            "Inspect parsed USER/SCN/PFB/headless RSZ documents, including "
            "segments, type counts, outer tables, exact typed fields, references, "
            "and external file paths. Instance listing is paginated; request exact "
            "instance IDs with include_fields=true for full values.",
            {
                **targets,
                "segment": {
                    "type": "string",
                    "description": "Segment ID to list; defaults to main.",
                },
                "instance_ids": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                    "maxItems": 128,
                },
                "type_filter": {
                    "type": "string",
                    "description": "Optional case-insensitive substring of an instance type name.",
                },
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "include_fields": {"type": "boolean"},
                "include_structure": {
                    "type": "boolean",
                    "description": "Include outer object/folder/resource/prefab/userdata tables.",
                },
                "array_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4096,
                    "description": "Maximum returned elements per array/struct; defaults to 64.",
                },
            },
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Inspecting RSZ data"),
                QT_TRANSLATE_NOOP("AiChatDock", "Inspected RSZ data"),
            ),
            capability=RSZ_CAPABILITY,
        ),
        _tool(
            "describe_rsz_types",
            "Describe exact RSZ registry types used by an open document, including "
            "IDs, CRCs, inheritance, field storage metadata, original types, and "
            "enum members.",
            {
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 32,
                    "description": "Exact type names or 0x-prefixed type IDs returned by RSZ tools.",
                },
                "tab": tab,
            },
            ["types"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Reading RSZ type definitions"),
                QT_TRANSLATE_NOOP("AiChatDock", "Read RSZ type definitions"),
            ),
            capability=RSZ_CAPABILITY,
        ),
        _tool(
            "search_rsz",
            "Search open RSZ documents across segment-local type names, field "
            "paths, original types, exact values, references, and external "
            "resource paths.",
            {
                "query": {
                    "type": "string",
                    "description": "Case-insensitive text to find.",
                },
                **targets,
                "mode": {
                    "type": "string",
                    "enum": ["all", "type", "field", "value", "resource", "reference"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "max_array_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100000,
                    "description": "Maximum searched items in each array/struct; defaults to 4096.",
                },
            },
            ["query"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Searching RSZ data"),
                QT_TRANSLATE_NOOP("AiChatDock", "Searched RSZ data"),
            ),
            capability=RSZ_CAPABILITY,
        ),
        _tool(
            "trace_rsz_references",
            "Trace inbound and/or outbound object and userdata edges from one "
            "segment-local instance, with target types, embedded-segment bridges, "
            "cycles, and external file/GUID references on visited instances.",
            {
                "instance_id": {"type": "integer", "minimum": 0},
                "tab": tab,
                "segment": {"type": "string", "description": "Defaults to main."},
                "direction": {
                    "type": "string",
                    "enum": ["outbound", "inbound", "both"],
                },
                "depth": {"type": "integer", "minimum": 1, "maximum": 12},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            ["instance_id"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Tracing RSZ references"),
                QT_TRANSLATE_NOOP("AiChatDock", "Traced RSZ references"),
            ),
            capability=RSZ_CAPABILITY,
        ),
        _tool(
            "edit_rsz",
            "Atomically apply typed set/array edits to one RSZ document, or "
            "perform one explicit structural reference creation/deletion using "
            "REasy's ID-remapping editor logic. Paths and segment-local IDs must "
            "come from inspect_rsz.",
            {
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 256,
                    "items": _edit_action_schema(),
                }
            },
            ["actions"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Editing RSZ data"),
                QT_TRANSLATE_NOOP("AiChatDock", "Edited RSZ data"),
            ),
            capability=RSZ_EDIT_CAPABILITY,
            mutation=True,
            result_card=True,
        ),
        _tool(
            "migrate_rsz_files",
            "Migrate one or many USER/SCN/PFB/headless-RSZ files on disk. "
            "This is a mechanical full overlay that cannot infer mod intent; "
            "use only when the user explicitly requests every compatible old "
            "value be transferred. Use the analyzed folder workflow for a "
            "normal mod update. "
            "The outdated and latest registries are both mandatory and are "
            "applied independently. The latest file remains the structural "
            "base; compatible values are transferred by type/graph identity "
            "and reference instance IDs are remapped. Outputs are separate "
            "and atomically written. Requires the user to first open the "
            "relevant game project with its PAK files loaded.",
            {
                "jobs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 256,
                    "items": migration_job_schema("rsz"),
                },
                "outdated_registry": {
                    "type": "string",
                    "description": (
                        "Required RSZ type-registry JSON matching every "
                        "outdated input file."
                    ),
                },
                "latest_registry": {
                    "type": "string",
                    "description": (
                        "Required latest RSZ type-registry JSON matching every "
                        "latest input and output file."
                    ),
                },
                "include_source_only": {
                    "type": "boolean",
                    "description": (
                        "Also append compatible source-only array/struct "
                        "elements. Missing latest fields, types, or instances "
                        "remain reported rather than invented. Default false."
                    ),
                },
            },
            ["jobs", "outdated_registry", "latest_registry"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Migrating RSZ files"),
                QT_TRANSLATE_NOOP("AiChatDock", "Migrated RSZ files"),
            ),
            capability=RSZ_EDIT_CAPABILITY,
            incremental=True,
            persistent=True,
        ),
        _tool(
            "analyze_rsz_mod_folder_update",
            "Read-only first stage for an RSZ mod-folder update. Recursively "
            "discover files, pair them with loaded game PAK originals, parse "
            "each side with its own registry, and group every proposed value "
            "transfer by semantic RSZ path. This writes nothing. Always call "
            "this before update_rsz_mod_folder so the AI can distinguish "
            "intentional mod values from obsolete vanilla defaults.",
            {
                "mod_folder": {
                    "type": "string",
                    "description": (
                        "Exact existing mod-folder path to scan recursively."
                    ),
                },
                "outdated_registry": {
                    "type": "string",
                    "description": (
                        "Required RSZ type-registry JSON matching the outdated "
                        "files in the mod folder."
                    ),
                },
                "latest_registry": {
                    "type": "string",
                    "description": (
                        "Required latest RSZ type-registry JSON matching the "
                        "active project's game PAK files."
                    ),
                },
                "include_source_only": {
                    "type": "boolean",
                    "description": (
                        "Include compatible source-only collection additions "
                        "as proposed changes. Default false."
                    ),
                },
            },
            ["mod_folder", "outdated_registry", "latest_registry"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Analyzing the RSZ mod update"),
                QT_TRANSLATE_NOOP("AiChatDock", "Analyzed the RSZ mod update"),
            ),
            capability=RSZ_EDIT_CAPABILITY,
            incremental=True,
            result_card=True,
        ),
        _tool(
            "inspect_rsz_mod_folder_analysis",
            "Page or search semantic change groups and unresolved issues from "
            "a prior RSZ mod-folder analysis. Use it until every group has "
            "been reviewed and schema/file issues have been investigated.",
            {
                "analysis_id": {
                    "type": "string",
                    "description": "ID returned by analyze_rsz_mod_folder_update.",
                },
                "section": {
                    "type": "string",
                    "enum": ["groups", "issues"],
                    "description": (
                        "Review proposed value groups or unresolved schema/file "
                        "issues. Default groups."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "Optional group-ID or semantic-path search.",
                },
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["analysis_id"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Inspecting RSZ update analysis"),
                QT_TRANSLATE_NOOP("AiChatDock", "Inspected RSZ update analysis"),
            ),
            capability=RSZ_EDIT_CAPABILITY,
        ),
        _tool(
            "update_rsz_mod_folder",
            "Apply an analyzed RSZ mod-folder update. Every semantic change "
            "group must have an explicit AI decision with reasoning; omitted "
            "or uncertain values keep the latest game data. Inputs and PAK "
            "bytes are hash-checked against analysis, outputs are rebuilt and "
            "validated, unrelated files are copied, and a separate folder is "
            "published atomically. Never call without first analyzing and "
            "reviewing all groups. A successful result returns an update-report "
            "ID for later exact difference inspection.",
            {
                "analysis_id": {
                    "type": "string",
                    "description": "ID returned by analyze_rsz_mod_folder_update.",
                },
                "decisions": {
                    "type": "array",
                    "maxItems": 2000,
                    "description": (
                        "Exactly one reviewed decision for every change group "
                        "in the analysis."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "group_id": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": ["apply_mod_value", "keep_latest"],
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "reason": {
                                "type": "string",
                                "description": (
                                    "Concise semantic reason tied to the mod's "
                                    "requested intent and the sampled values."
                                ),
                            },
                        },
                        "required": [
                            "group_id",
                            "action",
                            "confidence",
                            "reason",
                        ],
                        "additionalProperties": False,
                    },
                },
                "output_folder": {
                    "type": "string",
                    "description": (
                        "Optional new or empty output folder. Omit to create a "
                        "unique sibling named <mod folder>_updated."
                    ),
                },
                "allow_unresolved": {
                    "type": "boolean",
                    "description": (
                        "Proceed while copying unmatched or schema-incompatible "
                        "files/values unchanged. Default false."
                    ),
                },
            },
            ["analysis_id", "decisions"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Updating the RSZ mod folder"),
                QT_TRANSLATE_NOOP("AiChatDock", "Updated the RSZ mod folder"),
            ),
            capability=RSZ_EDIT_CAPABILITY,
            incremental=True,
            persistent=True,
        ),
    )


@dataclass
class _RszSegment:
    id: str
    instances: dict[int, Any]
    instance_infos: list[Any]
    object_table: list[int]
    userdata_infos: list[Any]
    userdata_strings: dict[int, str]
    context: Any = None
    parent: str | None = None
    source_userdata_instance: int | None = None


@dataclass
class _RszDocument:
    tab: Any
    owner_viewer: Any
    editor_viewer: Any
    rsz: RszFile
    registry: Any

    @property
    def kind(self) -> str:
        if self.rsz.is_usr:
            return "user"
        if self.rsz.is_pfb:
            return "pfb"
        if self.rsz.is_scn:
            return "scn"
        return "headless"

    def mark_modified(self, segment: _RszSegment | None = None) -> None:
        context = segment.context if segment is not None else None
        while context is not None:
            if hasattr(context, "modified"):
                context.modified = True
            context = getattr(context, "parent_userdata_rui", None)

        editor = self.editor_viewer
        if editor is not None and hasattr(editor, "mark_modified"):
            editor.mark_modified()
        owner = self.owner_viewer
        if owner is not None and owner is not editor:
            marker = getattr(owner, "_mark_modified", None)
            if callable(marker):
                marker()
            elif hasattr(owner, "modified"):
                owner.modified = True

    def refresh(self) -> None:
        editor = self.editor_viewer
        if editor is not None and hasattr(editor, "populate_tree"):
            editor.populate_tree()


def _mapped_value(mapping: dict[Any, Any], key: Any, default: Any = "") -> Any:
    try:
        return mapping.get(key, default)
    except TypeError:
        return default


def _segments(rsz: RszFile) -> list[_RszSegment]:
    result = [
        _RszSegment(
            "main",
            rsz.parsed_elements,
            rsz.instance_infos,
            rsz.object_table,
            rsz.rsz_userdata_infos,
            {
                int(getattr(item, "instance_id", 0)): str(
                    _mapped_value(
                        rsz._rsz_userdata_str_map,
                        item,
                        getattr(item, "value", ""),
                    )
                    or ""
                )
                for item in rsz.rsz_userdata_infos
            },
        )
    ]

    def append_children(parent: _RszSegment) -> None:
        for index, userdata in enumerate(parent.userdata_infos):
            instances = getattr(userdata, "embedded_instances", None)
            if not isinstance(instances, dict) or not instances:
                continue
            child = _RszSegment(
                f"{parent.id}/userdata[{index}]",
                instances,
                list(getattr(userdata, "embedded_instance_infos", ()) or ()),
                list(getattr(userdata, "embedded_object_table", ()) or ()),
                list(getattr(userdata, "embedded_userdata_infos", ()) or ()),
                {
                    int(getattr(item, "instance_id", 0)): str(
                        getattr(item, "value", "") or getattr(item, "name", "") or ""
                    )
                    for item in (getattr(userdata, "embedded_userdata_infos", ()) or ())
                },
                context=userdata,
                parent=parent.id,
                source_userdata_instance=getattr(userdata, "instance_id", None),
            )
            result.append(child)
            append_children(child)

    append_children(result[0])
    return result


def _segment_by_id(document: _RszDocument, segment_id: str) -> _RszSegment:
    requested = str(segment_id or "main").strip()
    matches = [
        segment for segment in _segments(document.rsz) if segment.id == requested
    ]
    if not matches:
        raise AssistantToolError(
            _tr("RSZ segment was not found: {segment}", segment=requested)
        )
    return matches[0]


def _type_record(registry, info) -> tuple[str, str, dict[str, Any] | None]:
    type_id = int(getattr(info, "type_id", 0) or 0)
    type_info = registry.get_type_info(type_id) if registry and type_id else None
    return (
        f"0x{type_id:08X}",
        str(type_info.get("name")) if type_info else "<unknown>",
        type_info,
    )


def _instance_type(segment: _RszSegment, instance_id: int, registry):
    if not 0 <= instance_id < len(segment.instance_infos):
        return "0x00000000", "<missing>", None
    return _type_record(registry, segment.instance_infos[instance_id])


def _enum_payload(data_obj) -> dict[str, Any] | None:
    original_type = str(getattr(data_obj, "orig_type", "") or "")
    if not original_type or not hasattr(data_obj, "value"):
        return None
    members = EnumManager.instance().get_enum_values(original_type)
    if not members:
        return None
    value = data_obj.value
    match = next((member for member in members if member.get("value") == value), None)
    if match is None and isinstance(value, int):
        alternate = value & 0xFFFFFFFF
        match = next(
            (member for member in members if member.get("value") == alternate),
            None,
        )
    return {
        "type": original_type,
        "name": match.get("name") if match else None,
        "known": match is not None,
    }


_COMPONENTS = {
    "Vec2Data": ("x", "y"),
    "Float2Data": ("x", "y"),
    "PointData": ("x", "y"),
    "Int2Data": ("x", "y"),
    "Uint2Data": ("x", "y"),
    "Vec3Data": ("x", "y", "z"),
    "Vec3ColorData": ("x", "y", "z"),
    "Float3Data": ("x", "y", "z"),
    "PositionData": ("x", "y", "z"),
    "Int3Data": ("x", "y", "z"),
    "Uint3Data": ("x", "y", "z"),
    "Vec4Data": ("x", "y", "z", "w"),
    "Float4Data": ("x", "y", "z", "w"),
    "QuaternionData": ("x", "y", "z", "w"),
    "Int4Data": ("x", "y", "z", "w"),
    "Int4ColorData": ("x", "y", "z", "w"),
    "ColorData": ("r", "g", "b", "a"),
    "RangeData": ("min", "max"),
    "RangeIData": ("min", "max"),
    "SizeData": ("width", "height"),
    "RectData": ("min_x", "min_y", "max_x", "max_y"),
}


def _clean_string(value: Any) -> tuple[str, bool]:
    text = str(value or "")
    terminated = text.endswith("\x00")
    return text.rstrip("\x00"), terminated


def _json_value(
    data_obj,
    segment: _RszSegment,
    registry,
    array_limit: int,
    depth: int = 0,
) -> Any:
    if depth > 16:
        return {"truncated": True, "reason": "max_depth"}
    if isinstance(data_obj, (ArrayData, StructData)):
        count = len(data_obj.values)
        items = [
            _json_value(item, segment, registry, array_limit, depth + 1)
            for item in islice(data_obj.values, array_limit)
        ]
        return {
            "count": count,
            "items": items,
            "truncated": count > array_limit,
        }
    if isinstance(data_obj, dict):
        return {
            str(name): _json_value(value, segment, registry, array_limit, depth + 1)
            for name, value in data_obj.items()
        }
    if isinstance(data_obj, ObjectData):
        type_id, type_name, _ = _instance_type(segment, int(data_obj.value), registry)
        return {
            "instance_id": int(data_obj.value),
            "reference_kind": "object",
            "target_type": type_name,
            "target_type_id": type_id,
        }
    if isinstance(data_obj, UserDataData):
        type_id, type_name, _ = _instance_type(segment, int(data_obj.value), registry)
        userdata_string = segment.userdata_strings.get(
            int(data_obj.value),
            data_obj.string,
        )
        return {
            "instance_id": int(data_obj.value),
            "reference_kind": "userdata",
            "userdata_string": _clean_string(userdata_string)[0],
            "target_type": type_name,
            "target_type_id": type_id,
        }
    if isinstance(data_obj, (GuidData, GameObjectRefData)):
        return str(data_obj.guid_str)
    if isinstance(data_obj, RawBytesData):
        return {
            "hex": bytes(data_obj.raw_bytes).hex().upper(),
            "byte_count": len(data_obj.raw_bytes),
        }
    if isinstance(data_obj, ResourceData) or data_obj.__class__.__name__ in {
        "StringData",
        "RuntimeTypeData",
    }:
        value, terminated = _clean_string(data_obj.value)
        return {"text": value, "null_terminated": terminated}
    class_name = data_obj.__class__.__name__
    components = _COMPONENTS.get(class_name)
    if components:
        return {name: getattr(data_obj, name) for name in components}
    if class_name in {"Mat4Data", "OBBData"}:
        return list(data_obj.values)
    if class_name == "AABBData":
        return {
            "min": [data_obj.min.x, data_obj.min.y, data_obj.min.z],
            "max": [data_obj.max.x, data_obj.max.y, data_obj.max.z],
        }
    if class_name == "CapsuleData":
        return {
            "start": [data_obj.start.x, data_obj.start.y, data_obj.start.z],
            "end": [data_obj.end.x, data_obj.end.y, data_obj.end.z],
            "radius": data_obj.radius,
        }
    if class_name in {"AreaData", "AreaDataOld"}:
        return {
            name: [getattr(data_obj, name).x, getattr(data_obj, name).y]
            for name in ("p0", "p1", "p2", "p3")
        } | {"height": data_obj.height, "bottom": data_obj.bottom}
    if hasattr(data_obj, "value"):
        return data_obj.value
    return str(data_obj)


def _field_definition(field_def: dict[str, Any] | None) -> dict[str, Any] | None:
    if not field_def:
        return None
    keys = ("type", "original_type", "array", "native", "size", "align")
    return {key: field_def[key] for key in keys if key in field_def}


def _field_payload(
    name: str,
    data_obj,
    field_def: dict[str, Any] | None,
    segment: _RszSegment,
    registry,
    array_limit: int,
) -> dict[str, Any]:
    result = {
        "path": name,
        "storage_type": data_obj.__class__.__name__,
        "value": _json_value(data_obj, segment, registry, array_limit),
    }
    definition = _field_definition(field_def)
    if definition:
        result["definition"] = definition
    enum = _enum_payload(data_obj)
    if enum:
        result["enum"] = enum
    return result


def _field_definitions(type_info: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        str(field.get("name")): field
        for field in (type_info or {}).get("fields", ())
        if field.get("name")
    }


def _instance_payload(
    segment: _RszSegment,
    instance_id: int,
    registry,
    *,
    include_fields: bool,
    array_limit: int,
) -> dict[str, Any]:
    type_id, type_name, type_info = _instance_type(segment, instance_id, registry)
    fields = segment.instances.get(instance_id)
    result = {
        "instance_id": instance_id,
        "type_name": type_name,
        "type_id": type_id,
        "crc": (
            f"0x{int(getattr(segment.instance_infos[instance_id], 'crc', 0)) & 0xFFFFFFFF:08X}"
            if 0 <= instance_id < len(segment.instance_infos)
            else None
        ),
        "object_table_indices": [
            index
            for index, value in enumerate(segment.object_table)
            if value == instance_id
        ],
        "field_count": len(fields) if isinstance(fields, dict) else 0,
    }
    if include_fields:
        definitions = _field_definitions(type_info)
        result["fields"] = [
            _field_payload(
                name,
                value,
                definitions.get(name),
                segment,
                registry,
                array_limit,
            )
            for name, value in (fields.items() if isinstance(fields, dict) else ())
        ]
    return result


def _walk_value(data_obj, path: str, max_items: int, truncated: list[bool]):
    yield path, data_obj
    if isinstance(data_obj, (ArrayData, StructData)):
        count = len(data_obj.values)
        if count > max_items:
            truncated[0] = True
        for index, value in enumerate(data_obj.values):
            if index >= max_items:
                break
            child = f"{path}[{index}]"
            yield from _walk_value(value, child, max_items, truncated)
    elif isinstance(data_obj, dict):
        for name, value in data_obj.items():
            child = f"{path}.{name}" if path else str(name)
            yield from _walk_value(value, child, max_items, truncated)


def _walk_fields(fields: Any, max_items: int, truncated: list[bool]):
    if not isinstance(fields, dict):
        return
    for name, value in fields.items():
        yield from _walk_value(value, str(name), max_items, truncated)


def _looks_like_path(value: str) -> bool:
    folded = value.replace("\\", "/").casefold()
    return bool(
        folded.startswith(("natives/", "streaming/"))
        or ("/" in folded and re.search(r"\.[a-z0-9_]+(?:\.\d+)?$", folded))
    )


def _gameobject_targets(rsz: RszFile) -> dict[str, dict[str, Any]]:
    result = {}
    for gameobject_index, gameobject in enumerate(rsz.gameobjects):
        raw_guid = getattr(gameobject, "guid", b"")
        if (
            not isinstance(raw_guid, (bytes, bytearray, memoryview))
            or len(raw_guid) != 16
        ):
            continue
        guid = str(uuid.UUID(bytes_le=bytes(raw_guid)))
        object_index = int(getattr(gameobject, "id", gameobject_index))
        if not 0 <= object_index < len(rsz.object_table):
            continue
        result[guid.casefold()] = {
            "gameobject_index": gameobject_index,
            "object_table_index": object_index,
            "target_segment": "main",
            "target_instance_id": int(rsz.object_table[object_index]),
        }
    return result


def _external_references(
    document: _RszDocument,
    *,
    max_items: int = 100000,
    segment_ids: set[str] | None = None,
    instance_ids: set[int] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    rsz = document.rsz
    references: list[dict[str, Any]] = []
    seen = set()
    truncated = [False]
    gameobject_targets = _gameobject_targets(rsz)

    def add(kind: str, value: str, **location) -> None:
        clean, _ = _clean_string(value)
        if not clean:
            return
        key = (kind, clean, tuple(sorted(location.items())))
        if key in seen:
            return
        seen.add(key)
        references.append({"kind": kind, "path": clean, **location})

    for kind, items, mapping in (
        ("resource_table", rsz.resource_infos, rsz._resource_str_map),
        ("prefab_table", rsz.prefab_infos, rsz._prefab_str_map),
        ("userdata", rsz.userdata_infos, rsz._userdata_str_map),
    ):
        for index, item in enumerate(items):
            value, _ = _clean_string(_mapped_value(mapping, item))
            resolved_kind = (
                "userdata_path"
                if kind == "userdata" and _looks_like_path(value)
                else "userdata_type"
                if kind == "userdata"
                else kind
            )
            add(resolved_kind, value, table_index=index)

    for segment in _segments(rsz):
        if segment_ids is not None and segment.id not in segment_ids:
            continue
        for instance_id, fields in segment.instances.items():
            if instance_ids is not None and instance_id not in instance_ids:
                continue
            for path, value in _walk_fields(fields, max_items, truncated) or ():
                if isinstance(value, ResourceData):
                    add(
                        "resource_field",
                        value.value,
                        segment=segment.id,
                        instance_id=instance_id,
                        field=path,
                    )
                elif value.__class__.__name__ == "StringData":
                    clean, _ = _clean_string(value.value)
                    if _looks_like_path(clean):
                        add(
                            "path_candidate",
                            clean,
                            segment=segment.id,
                            instance_id=instance_id,
                            field=path,
                        )
                elif isinstance(value, UserDataData):
                    userdata_string = segment.userdata_strings.get(
                        int(value.value),
                        value.string,
                    )
                    clean, _ = _clean_string(userdata_string)
                    if _looks_like_path(clean):
                        add(
                            "userdata_path",
                            clean,
                            segment=segment.id,
                            instance_id=instance_id,
                            field=path,
                        )
                elif isinstance(value, GameObjectRefData):
                    guid = str(value.guid_str)
                    if guid == "00000000-0000-0000-0000-000000000000":
                        continue
                    add(
                        "gameobject_guid",
                        guid,
                        segment=segment.id,
                        instance_id=instance_id,
                        field=path,
                        **gameobject_targets.get(guid.casefold(), {}),
                    )
    return references, truncated[0]


def _slots_payload(value) -> dict[str, Any]:
    names: list[str] = []
    for cls in type(value).__mro__:
        slots = getattr(cls, "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        names.extend(slots)
    result = {}
    for name in names:
        if not hasattr(value, name):
            continue
        item = getattr(value, name)
        if isinstance(item, (bytes, bytearray, memoryview)):
            raw = bytes(item)
            item = str(uuid.UUID(bytes_le=raw)) if len(raw) == 16 else raw.hex().upper()
        result[name] = item
    return result


def _outer_structure(rsz: RszFile, limit: int) -> dict[str, Any]:
    def rows(values):
        return [_slots_payload(value) for value in list(values)[:limit]]

    result = {
        "file_header": _slots_payload(rsz.header),
        "rsz_header": _slots_payload(rsz.rsz_header),
        "object_table": list(rsz.object_table[:limit]),
        "object_table_truncated": len(rsz.object_table) > limit,
        "gameobjects": rows(rsz.gameobjects),
        "gameobjects_truncated": len(rsz.gameobjects) > limit,
        "folders": rows(rsz.folder_infos),
        "folders_truncated": len(rsz.folder_infos) > limit,
        "gameobject_references": rows(rsz.gameobject_ref_infos),
        "gameobject_references_truncated": len(rsz.gameobject_ref_infos) > limit,
        "resource_table": [
            {
                "index": index,
                "path": _mapped_value(rsz._resource_str_map, item),
                **_slots_payload(item),
            }
            for index, item in enumerate(rsz.resource_infos[:limit])
        ],
        "resource_table_truncated": len(rsz.resource_infos) > limit,
        "prefab_table": [
            {
                "index": index,
                "path": _mapped_value(rsz._prefab_str_map, item),
                **_slots_payload(item),
            }
            for index, item in enumerate(rsz.prefab_infos[:limit])
        ],
        "prefab_table_truncated": len(rsz.prefab_infos) > limit,
        "userdata_table": [
            {
                "index": index,
                "type": _mapped_value(rsz._userdata_str_map, item),
                **_slots_payload(item),
            }
            for index, item in enumerate(rsz.userdata_infos[:limit])
        ],
        "userdata_table_truncated": len(rsz.userdata_infos) > limit,
    }
    return result


def _segment_payload(segment: _RszSegment, registry) -> dict[str, Any]:
    counts = Counter()
    for instance_id in segment.instances:
        _, name, _ = _instance_type(segment, instance_id, registry)
        counts[name] += 1
    return {
        "id": segment.id,
        "parent": segment.parent,
        "source_userdata_instance": segment.source_userdata_instance,
        "instance_count": len(segment.instance_infos),
        "parsed_instance_count": len(segment.instances),
        "object_roots": list(segment.object_table),
        "userdata_count": len(segment.userdata_infos),
        "type_counts": [
            {"type_name": name, "count": count}
            for name, count in sorted(counts.items())
        ],
    }


_PATH_TOKEN_RE = re.compile(r"(?:^|\.)([^.\[\]]+)|\[(\d+)\]")


def _path_tokens(path: str) -> list[str | int]:
    text = str(path or "").strip()
    tokens: list[str | int] = []
    position = 0
    for match in _PATH_TOKEN_RE.finditer(text):
        if match.start() != position:
            raise AssistantToolError(_tr("Invalid RSZ field path: {path}", path=text))
        tokens.append(
            match.group(1) if match.group(1) is not None else int(match.group(2))
        )
        position = match.end()
    if not tokens or position != len(text):
        raise AssistantToolError(_tr("Invalid RSZ field path: {path}", path=text))
    return tokens


def _child(parent, token, path: str):
    if isinstance(token, str):
        if not isinstance(parent, dict) or token not in parent:
            raise AssistantToolError(
                _tr("RSZ field path was not found: {path}", path=path)
            )
        return parent[token]
    if not isinstance(parent, (ArrayData, StructData)):
        raise AssistantToolError(
            _tr("RSZ field path is not indexable: {path}", path=path)
        )
    if not 0 <= token < len(parent.values):
        raise AssistantToolError(
            _tr("RSZ field index is out of range: {path}", path=path)
        )
    return parent.values[token]


def _resolve_path(fields: dict[str, Any], path: str):
    tokens = _path_tokens(path)
    parent: Any = fields
    current: Any = fields
    for token in tokens:
        parent = current
        current = _child(current, token, path)
    return parent, tokens[-1], current


def _replace_child(parent, token, value) -> None:
    if isinstance(token, str):
        parent[token] = value
    else:
        parent.values[token] = value


def _contains_identity(value, target) -> bool:
    if value is target:
        return True
    if isinstance(value, (ArrayData, StructData)):
        return any(_contains_identity(item, target) for item in value.values)
    if isinstance(value, dict):
        return any(_contains_identity(item, target) for item in value.values())
    return False


def _owner_instance_id(document: _RszDocument, segment_id: str, target) -> int | None:
    segment = _segment_by_id(document, segment_id)
    return next(
        (
            instance_id
            for instance_id, fields in segment.instances.items()
            if _contains_identity(fields, target)
        ),
        None,
    )


_INTEGER_BOUNDS = {
    "S8Data": (-0x80, 0x7F),
    "U8Data": (0, 0xFF),
    "S16Data": (-0x8000, 0x7FFF),
    "U16Data": (0, 0xFFFF),
    "S32Data": (-0x80000000, 0x7FFFFFFF),
    "U32Data": (0, 0xFFFFFFFF),
    "S64Data": (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF),
    "U64Data": (0, 0xFFFFFFFFFFFFFFFF),
}


def _integer_value(value: Any, label: str, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool):
        raise AssistantToolError(_tr("{field} must be an integer.", field=label))
    try:
        parsed = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise AssistantToolError(
            _tr("{field} must be an integer.", field=label)
        ) from exc
    if not bounds[0] <= parsed <= bounds[1]:
        raise AssistantToolError(
            _tr("{field} is outside its supported numeric range.", field=label)
        )
    return parsed


def _float_value(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise AssistantToolError(_tr("{field} must be numeric.", field=label))
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise AssistantToolError(_tr("{field} must be numeric.", field=label)) from exc
    if not math.isfinite(parsed):
        raise AssistantToolError(_tr("{field} must be finite.", field=label))
    return parsed


def _enum_value(data_obj, value: Any) -> Any:
    if not isinstance(value, str) or not getattr(data_obj, "orig_type", ""):
        return value
    members = EnumManager.instance().get_enum_values(data_obj.orig_type)
    matches = [
        member
        for member in members
        if str(member.get("name", "")).casefold() == value.casefold()
    ]
    if len(matches) == 1:
        return matches[0].get("value")
    return value


def _sequence(value: Any, names: Iterable[str], label: str) -> dict[str, Any]:
    names = tuple(names)
    if isinstance(value, dict):
        if set(value) != set(names):
            raise AssistantToolError(
                _tr(
                    "{field} must provide exactly: {names}",
                    field=label,
                    names=", ".join(names),
                )
            )
        return {name: value[name] for name in names}
    if not isinstance(value, list) or len(value) != len(names):
        raise AssistantToolError(
            _tr("{field} must contain {count} values.", field=label, count=len(names))
        )
    return dict(zip(names, value))


def _coerce_value(template, value: Any, segment: _RszSegment, registry, label: str):
    memo = (
        {id(segment.context): segment.context} if segment.context is not None else None
    )
    clone = copy.deepcopy(template, memo)
    class_name = clone.__class__.__name__
    if isinstance(clone, ObjectData):
        clone.value = _integer_value(
            value, label, (0, max(0, len(segment.instance_infos) - 1))
        )
        return clone
    if isinstance(clone, UserDataData):
        instance_value = value.get("instance_id") if isinstance(value, dict) else value
        if isinstance(value, dict) and set(value) != {"instance_id"}:
            raise AssistantToolError(
                _tr(
                    "{field} can only rewire to an existing instance ID. Use "
                    "initialize_reference to change userdata type or path.",
                    field=label,
                )
            )
        clone.value = _integer_value(
            instance_value,
            label,
            (0, max(0, len(segment.instance_infos) - 1)),
        )
        if clone.value and clone.value not in segment.userdata_strings:
            raise AssistantToolError(
                _tr(
                    "{field} must reference an existing userdata instance.",
                    field=label,
                )
            )
        clone.string = segment.userdata_strings.get(clone.value, "")
        return clone
    if isinstance(clone, (GuidData, GameObjectRefData)):
        try:
            parsed = uuid.UUID(str(value))
        except (ValueError, AttributeError) as exc:
            raise AssistantToolError(
                _tr("{field} must be a GUID.", field=label)
            ) from exc
        clone.guid_str = str(parsed)
        clone.raw_bytes = parsed.bytes_le
        return clone
    if isinstance(clone, RawBytesData):
        try:
            raw = bytes.fromhex(value) if isinstance(value, str) else bytes(value)
        except (TypeError, ValueError) as exc:
            raise AssistantToolError(
                _tr("{field} must be hexadecimal bytes.", field=label)
            ) from exc
        if len(raw) != clone.field_size:
            raise AssistantToolError(
                _tr(
                    "{field} must contain exactly {count} bytes.",
                    field=label,
                    count=clone.field_size,
                )
            )
        clone.raw_bytes = raw
        return clone
    if isinstance(clone, (ArrayData, StructData)):
        if not isinstance(value, list):
            raise AssistantToolError(_tr("{field} must be an array.", field=label))
        clone.values = [
            _new_collection_element(clone, item, segment, registry, f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
        return clone
    if isinstance(clone, dict):
        if not isinstance(value, dict) or set(value) != set(clone):
            raise AssistantToolError(
                _tr("{field} must provide every struct field.", field=label)
            )
        return {
            name: _coerce_value(
                child, value[name], segment, registry, f"{label}.{name}"
            )
            for name, child in clone.items()
        }
    if isinstance(clone, ResourceData) or class_name in {
        "StringData",
        "RuntimeTypeData",
    }:
        if not isinstance(value, str):
            raise AssistantToolError(_tr("{field} must be text.", field=label))
        clone.value = value
        return clone
    if class_name == "BoolData":
        if not isinstance(value, bool):
            raise AssistantToolError(_tr("{field} must be true or false.", field=label))
        clone.value = value
        return clone
    if class_name in _INTEGER_BOUNDS:
        clone.value = _integer_value(
            _enum_value(clone, value), label, _INTEGER_BOUNDS[class_name]
        )
        return clone
    if class_name in {"F32Data", "F64Data"}:
        clone.value = _float_value(value, label)
        return clone
    components = _COMPONENTS.get(class_name)
    if components:
        values = _sequence(value, components, label)
        integer = class_name.startswith(("Int", "Uint")) or class_name in {
            "ColorData",
            "RangeIData",
        }
        for name in components:
            if integer:
                if class_name == "ColorData":
                    bounds = (0, 0xFF)
                elif class_name.startswith("Uint"):
                    bounds = (0, 0xFFFFFFFF)
                else:
                    bounds = (-0x80000000, 0x7FFFFFFF)
                setattr(
                    clone, name, _integer_value(values[name], f"{label}.{name}", bounds)
                )
            else:
                setattr(clone, name, _float_value(values[name], f"{label}.{name}"))
        return clone
    if class_name in {"Mat4Data", "OBBData"}:
        expected = len(clone.values)
        if not isinstance(value, list) or len(value) != expected:
            raise AssistantToolError(
                _tr("{field} must contain {count} values.", field=label, count=expected)
            )
        clone.values = [_float_value(item, label) for item in value]
        return clone
    if class_name == "AABBData":
        if not isinstance(value, dict) or set(value) != {"min", "max"}:
            raise AssistantToolError(
                _tr("{field} must provide min and max.", field=label)
            )
        for bound in ("min", "max"):
            values = _sequence(value[bound], ("x", "y", "z"), f"{label}.{bound}")
            target = getattr(clone, bound)
            for name, item in values.items():
                setattr(target, name, _float_value(item, f"{label}.{bound}.{name}"))
        return clone
    if class_name == "CapsuleData":
        if not isinstance(value, dict) or set(value) != {"start", "end", "radius"}:
            raise AssistantToolError(
                _tr("{field} must provide start, end, and radius.", field=label)
            )
        for endpoint in ("start", "end"):
            values = _sequence(value[endpoint], ("x", "y", "z"), f"{label}.{endpoint}")
            target = getattr(clone, endpoint)
            for name, item in values.items():
                setattr(target, name, _float_value(item, f"{label}.{endpoint}.{name}"))
        clone.radius = _float_value(value["radius"], f"{label}.radius")
        return clone
    if class_name in {"AreaData", "AreaDataOld"}:
        names = {"p0", "p1", "p2", "p3", "height", "bottom"}
        if not isinstance(value, dict) or set(value) != names:
            raise AssistantToolError(
                _tr(
                    "{field} must provide four points, height, and bottom.", field=label
                )
            )
        for point in ("p0", "p1", "p2", "p3"):
            values = _sequence(value[point], ("x", "y"), f"{label}.{point}")
            target = getattr(clone, point)
            target.x = _float_value(values["x"], f"{label}.{point}.x")
            target.y = _float_value(values["y"], f"{label}.{point}.y")
        clone.height = _float_value(value["height"], f"{label}.height")
        clone.bottom = _float_value(value["bottom"], f"{label}.bottom")
        return clone
    raise AssistantToolError(
        _tr(
            "RSZ storage type is not editable through the assistant: {type}",
            type=class_name,
        )
    )


def _default_field(field_def: dict[str, Any]):
    field_class = get_type_class(
        str(field_def.get("type", "unknown")).lower(),
        int(field_def.get("size", 4)),
        bool(field_def.get("native", False)),
        bool(field_def.get("array", False)),
        int(field_def.get("align", 4) or 4),
        str(field_def.get("original_type", "") or ""),
        str(field_def.get("name", "") or ""),
    )
    original = str(field_def.get("original_type", "") or "")
    if field_def.get("array", False):
        return ArrayData([], field_class, original)
    if field_class is ObjectData:
        return ObjectData(0, original)
    if field_class is UserDataData:
        return UserDataData(0, "", original)
    if field_class is RawBytesData:
        size = int(field_def.get("size", 1))
        return RawBytesData(bytes(size), size, original)
    try:
        return field_class(orig_type=original)
    except TypeError:
        return field_class()


def _new_collection_element(
    collection,
    value: Any,
    segment: _RszSegment,
    registry,
    label: str,
):
    if isinstance(collection, StructData):
        type_info, _ = (
            registry.find_type_by_name(collection.orig_type)
            if registry
            else (None, None)
        )
        if not type_info:
            raise AssistantToolError(
                _tr("RSZ struct type was not found: {type}", type=collection.orig_type)
            )
        template = {
            field["name"]: _default_field(field)
            for field in type_info.get("fields", ())
            if field.get("name")
        }
    else:
        element_class = getattr(collection, "element_class", None)
        if element_class is None:
            raise AssistantToolError(
                _tr("RSZ array has no element type: {field}", field=label)
            )
        if element_class is ObjectData:
            template = ObjectData(0, collection.orig_type)
        elif element_class is UserDataData:
            template = UserDataData(0, "", collection.orig_type)
        elif element_class is RawBytesData:
            raise AssistantToolError(
                _tr(
                    "Raw-byte array insertion requires replacing the complete array field."
                )
            )
        else:
            try:
                template = element_class(orig_type=collection.orig_type)
            except TypeError:
                template = element_class()
    return _coerce_value(template, value, segment, registry, label)


def _refresh_collection_metadata(collection, segment: _RszSegment) -> None:
    if isinstance(collection, ArrayData) and collection.element_class in {
        ObjectData,
        UserDataData,
    }:
        for index, value in enumerate(collection.values):
            if isinstance(value, (ObjectData, UserDataData)):
                value._container_array = collection
                value._container_index = index
    context = getattr(collection, "_owning_context", None) or segment.context
    counters = getattr(context, "_array_counters", None)
    if isinstance(counters, dict):
        counters[id(collection)] = len(collection.values)


def _refresh_segment_metadata(segment: _RszSegment) -> None:
    context = segment.context
    counters = getattr(context, "_array_counters", None)
    if isinstance(counters, dict):
        counters.clear()
    for fields in segment.instances.values():
        truncated = [False]
        for _path, value in _walk_fields(fields, 1000000, truncated) or ():
            if isinstance(value, (ArrayData, StructData)):
                _refresh_collection_metadata(value, segment)


def _reference_children(
    segments: list[_RszSegment],
) -> dict[tuple[str, int], _RszSegment]:
    return {
        (segment.parent, segment.source_userdata_instance): segment
        for segment in segments
        if segment.parent is not None and segment.source_userdata_instance is not None
    }


class RszAssistantToolMixin:
    _RSZ_FOLDER_SUFFIXES = ("user", "scn", "pfb", "rcol", "wcc")

    def _migrate_rsz_files(
        self,
        jobs: list[dict[str, Any]],
        outdated_registry: str,
        latest_registry: str,
        include_source_only: bool = False,
    ) -> dict[str, Any]:
        return self._run_incremental_steps(
            self._migrate_rsz_files_steps(
                jobs,
                outdated_registry,
                latest_registry,
                include_source_only,
            )
        )

    def _migrate_rsz_files_steps(
        self,
        jobs: list[dict[str, Any]],
        outdated_registry: str,
        latest_registry: str,
        include_source_only: bool = False,
    ):
        project_context = self._require_update_project_paks()
        outdated_path, outdated_types = load_type_registry(
            outdated_registry,
            "outdated_registry",
        )
        latest_path, latest_types = load_type_registry(
            latest_registry,
            "latest_registry",
        )
        strategy = RszMigrationStrategy(
            outdated_path,
            outdated_types,
            latest_path,
            latest_types,
            include_source_only=self._boolean(
                include_source_only,
                "include_source_only",
            ),
        )
        return (
            yield from self._migrate_reported_file_jobs_steps(
                jobs,
                strategy,
                project_context,
            )
        )

    @staticmethod
    def _summarize_migrate_rsz_files(
        arguments: dict[str, Any],
    ) -> tuple[str, str]:
        jobs = arguments.get("jobs")
        count = len(jobs) if isinstance(jobs, list) else 0
        details = _tr(
            "Jobs: {count}\nOutdated registry: {old}\nLatest registry: {new}",
            count=count,
            old=arguments.get("outdated_registry", ""),
            new=arguments.get("latest_registry", ""),
        )
        outputs = [
            str(job.get("output_file", ""))
            for job in (jobs or [])[:5]
            if isinstance(job, dict)
        ]
        if outputs:
            details += "\n" + _tr(
                "Output files: {paths}",
                paths=", ".join(outputs),
            )
        return _tr("Migrate RSZ files"), details

    def _analysis_by_id(self, analysis_id: str) -> RszUpdateAnalysis:
        key = str(analysis_id or "").strip()
        analysis = self._rsz_update_analyses.get(key)
        if analysis is None:
            raise AssistantToolError(
                _tr(
                    "RSZ update analysis was not found: {analysis_id}. Analyze the mod folder again.",
                    analysis_id=key,
                )
            )
        return analysis

    def _analyze_rsz_mod_folder_update(
        self,
        mod_folder: str,
        outdated_registry: str,
        latest_registry: str,
        include_source_only: bool = False,
    ) -> dict[str, Any]:
        return self._run_incremental_steps(
            self._analyze_rsz_mod_folder_update_steps(
                mod_folder,
                outdated_registry,
                latest_registry,
                include_source_only,
            )
        )

    def _analyze_rsz_mod_folder_update_steps(
        self,
        mod_folder: str,
        outdated_registry: str,
        latest_registry: str,
        include_source_only: bool = False,
    ):
        project_context = self._require_update_project_paks()
        outdated_path, outdated_types = load_type_registry(
            outdated_registry,
            "outdated_registry",
        )
        latest_path, latest_types = load_type_registry(
            latest_registry,
            "latest_registry",
        )
        strategy = RszMigrationStrategy(
            outdated_path,
            outdated_types,
            latest_path,
            latest_types,
            include_source_only=self._boolean(
                include_source_only,
                "include_source_only",
            ),
            detail_limit=None,
        )
        analysis_id = uuid.uuid4().hex[:12]
        analysis = yield from analyze_rsz_mod_folder_steps(
            analysis_id=analysis_id,
            project=project_context["project"],
            game=project_context["game"],
            mod_folder=mod_folder,
            pak_paths=self._configured_pak_paths(),
            read_pak_file=self._read_configured_pak_file,
            strategy=strategy,
            suffixes=self._RSZ_FOLDER_SUFFIXES,
        )
        self._rsz_update_analyses[analysis_id] = analysis
        while len(self._rsz_update_analyses) > 8:
            self._rsz_update_analyses.pop(next(iter(self._rsz_update_analyses)))
        return analysis.payload()

    def _inspect_rsz_mod_folder_analysis(
        self,
        analysis_id: str,
        section: str = "groups",
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        analysis = self._analysis_by_id(analysis_id)
        section = str(section or "groups").strip().casefold()
        if section not in {"groups", "issues"}:
            raise AssistantToolError(
                _tr("section must be groups or issues.")
            )
        page = (
            analysis.group_page if section == "groups" else analysis.issue_page
        )
        return page(
            query=query,
            offset=self._integer(offset, "offset", 0, 1_000_000),
            limit=self._integer(limit, "limit", 1, 100),
        )

    def _reviewed_rsz_update_decisions(
        self,
        analysis: RszUpdateAnalysis,
        decisions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, str]], dict[str, frozenset[str]]]:
        if not isinstance(decisions, list) or len(decisions) > 2000:
            raise AssistantToolError(
                _tr("decisions must be an array with at most 2000 items.")
            )
        groups = {group.group_id: group for group in analysis.groups}
        allowed = {"group_id", "action", "confidence", "reason"}
        reviewed: list[dict[str, str]] = []
        seen: set[str] = set()
        approved: dict[str, set[str]] = {
            item.relative_path: set() for item in analysis.files
        }
        for index, raw in enumerate(decisions):
            if not isinstance(raw, dict) or set(raw) != allowed:
                raise AssistantToolError(
                    _tr("RSZ update decision {index} is invalid.", index=index + 1)
                )
            group_id = str(raw.get("group_id") or "").strip()
            action = str(raw.get("action") or "").strip().casefold()
            confidence = str(raw.get("confidence") or "").strip().casefold()
            reason = str(raw.get("reason") or "").strip()
            if group_id not in groups:
                raise AssistantToolError(
                    _tr("Unknown RSZ change group: {group_id}", group_id=group_id)
                )
            if group_id in seen:
                raise AssistantToolError(
                    _tr(
                        "RSZ change group was decided more than once: {group_id}",
                        group_id=group_id,
                    )
                )
            if action not in {"apply_mod_value", "keep_latest"}:
                raise AssistantToolError(
                    _tr("Invalid RSZ update action for {group_id}.", group_id=group_id)
                )
            if confidence not in {"high", "medium", "low"} or not reason:
                raise AssistantToolError(
                    _tr(
                        "RSZ decision {group_id} requires confidence and a semantic reason.",
                        group_id=group_id,
                    )
                )
            if action == "apply_mod_value" and confidence == "low":
                raise AssistantToolError(
                    _tr(
                        "Low-confidence RSZ group {group_id} must keep the latest game value.",
                        group_id=group_id,
                    )
                )
            if len(reason) > 1000:
                raise AssistantToolError(
                    _tr("RSZ decision reasons must be at most 1000 characters.")
                )
            seen.add(group_id)
            reviewed.append(
                {
                    "group_id": group_id,
                    "action": action,
                    "confidence": confidence,
                    "reason": reason,
                }
            )
            if action == "apply_mod_value":
                for file, path in groups[group_id].references:
                    approved[file].add(path)

        missing = sorted(groups.keys() - seen)
        if missing:
            raise AssistantToolError(
                _tr(
                    "Every analyzed RSZ change group requires an AI decision. Missing {count}: {groups}",
                    count=len(missing),
                    groups=", ".join(missing[:20]),
                )
            )
        return reviewed, {
            file: frozenset(paths) for file, paths in approved.items()
        }

    def _update_rsz_mod_folder(
        self,
        analysis_id: str,
        decisions: list[dict[str, Any]],
        output_folder: str = "",
        allow_unresolved: bool = False,
    ) -> dict[str, Any]:
        return self._run_incremental_steps(
            self._update_rsz_mod_folder_steps(
                analysis_id,
                decisions,
                output_folder,
                allow_unresolved,
            )
        )

    def _update_rsz_mod_folder_steps(
        self,
        analysis_id: str,
        decisions: list[dict[str, Any]],
        output_folder: str = "",
        allow_unresolved: bool = False,
    ):
        project_context = self._require_update_project_paks()
        analysis = self._analysis_by_id(analysis_id)
        if (
            self._path_key(project_context["project"])
            != self._path_key(analysis.project)
            or str(project_context["game"]).casefold()
            != str(analysis.game).casefold()
        ):
            raise AssistantToolError(
                _tr(
                    "The active project changed after RSZ analysis. Reopen the analyzed project or analyze the update again."
                )
            )
        allow_unresolved = self._boolean(allow_unresolved, "allow_unresolved")
        if not allow_unresolved and (
            analysis.unresolved_file_count or analysis.unresolved_value_count
        ):
            raise AssistantToolError(
                _tr(
                    "RSZ analysis has {files} unresolved files and {values} unresolved values. Investigate them or explicitly set allow_unresolved=true to preserve them unchanged.",
                    files=analysis.unresolved_file_count,
                    values=analysis.unresolved_value_count,
                )
            )
        reviewed, approved = self._reviewed_rsz_update_decisions(
            analysis,
            decisions,
        )
        outdated_path, outdated_types = load_type_registry(
            analysis.outdated_registry,
            "outdated_registry",
        )
        latest_path, latest_types = load_type_registry(
            analysis.latest_registry,
            "latest_registry",
        )
        strategy = RszMigrationStrategy(
            outdated_path,
            outdated_types,
            latest_path,
            latest_types,
            include_source_only=analysis.include_source_only,
            approved_change_paths=approved,
            expected_input_hashes=analysis.expected_hashes,
            detail_limit=None,
        )
        collector = self._new_file_update_report_collector()
        result = yield from update_mod_folder_from_paks_steps(
            mod_folder=analysis.mod_folder,
            output_folder=output_folder,
            pak_paths=self._configured_pak_paths(),
            read_pak_file=self._read_configured_pak_file,
            strategy=strategy,
            suffixes=self._RSZ_FOLDER_SUFFIXES,
            expected_pairs=analysis.expected_pairs,
            report_sink=collector,
        )
        applied = [item for item in reviewed if item["action"] == "apply_mod_value"]
        result.update(
            {
                "analysis_id": analysis.analysis_id,
                "ai_reviewed_group_count": len(reviewed),
                "ai_applied_group_count": len(applied),
                "ai_kept_latest_group_count": len(reviewed) - len(applied),
                "ai_decisions": reviewed[:100],
                "comparison_mode": "two_way_ai_intent_review",
                "old_original_available": False,
                "accuracy_note": (
                    "Imported values are exact, but without old-original "
                    "files the classification of mod intent was semantic "
                    "inference rather than a provable three-way diff."
                ),
                "allow_unresolved": allow_unresolved,
                "details_truncated": bool(
                    result.get("details_truncated") or len(reviewed) > 100
                ),
            }
        )
        decisions_by_group = {
            item["group_id"]: item for item in reviewed
        }
        decision_lookup = {
            (file, path): decisions_by_group[group.group_id]
            for group in analysis.groups
            for file, path in group.references
        }
        return self._attach_file_update_report(
            result,
            format_name="rsz",
            operation="ai_reviewed_mod_folder_update",
            project_context=project_context,
            source=analysis.mod_folder,
            output=str(result.get("output_folder") or output_folder),
            collector=collector,
            decision_lookup=decision_lookup,
        )

    def _summarize_update_rsz_mod_folder(
        self,
        arguments: dict[str, Any],
    ) -> tuple[str, str]:
        analysis = self._analysis_by_id(str(arguments.get("analysis_id") or ""))
        decisions = arguments.get("decisions")
        decision_count = len(decisions) if isinstance(decisions, list) else 0
        output = arguments.get("output_folder") or _tr("Automatic sibling folder")
        return (
            _tr("Apply AI-reviewed RSZ mod update"),
            _tr(
                "Mod folder: {mod}\nOutput folder: {output}\nAnalysis: {analysis}\nReviewed groups: {count}",
                mod=analysis.mod_folder,
                output=output,
                analysis=analysis.analysis_id,
                count=decision_count,
            ),
        )

    def _rsz_for_tab(self, tab) -> _RszDocument:
        if tab is None:
            raise AssistantToolError(_tr("No RSZ-compatible file is active."))
        owner = getattr(tab, "viewer", None)
        handler = getattr(tab, "handler", None)
        candidates: list[tuple[Any, Any]] = []

        def add(value, editor=None):
            if isinstance(value, RszFile):
                candidates.append((value, editor))

        def add_provider(provider, editor=None):
            if provider is None:
                return
            add(getattr(provider, "rsz", None), editor)
            getter = getattr(provider, "get_rsz", None)
            if callable(getter):
                add(getter(), editor)

        add(getattr(owner, "scn", None), owner)
        add(getattr(handler, "rsz_file", None), owner)
        embedded = getattr(owner, "_embedded_headless_viewer", None)
        add(getattr(embedded, "scn", None), embedded)
        add_provider(owner, embedded)
        add_provider(handler, embedded)
        for container in (
            getattr(owner, "rcol", None),
            getattr(handler, "rcol", None),
        ):
            add_provider(container, embedded)
        if not candidates:
            raise AssistantToolError(
                _tr("The selected editor does not expose parsed RSZ data.")
            )
        rsz, editor = candidates[0]
        registry = (
            getattr(rsz, "type_registry", None)
            or getattr(editor, "type_registry", None)
            or getattr(handler, "type_registry", None)
        )
        if registry is None:
            raise AssistantToolError(_tr("The selected RSZ has no type registry."))
        app = getattr(handler, "app", None) or self.app
        settings = getattr(app, "settings", {}) or {}
        editor_handler = getattr(editor, "handler", None)
        configured_game = (
            getattr(handler, "game_version", None)
            or getattr(editor, "game_version", None)
            or getattr(editor_handler, "game_version", None)
            or getattr(app, "current_game", None)
            or settings.get("game_version")
        )
        if configured_game:
            rsz.game_version = str(configured_game)
        return _RszDocument(tab, owner, editor, rsz, registry)

    @staticmethod
    def _activate_rsz_enums(document: _RszDocument) -> None:
        EnumManager.instance().game_version = document.rsz.game_version

    def _active_rsz(self) -> _RszDocument:
        return self._rsz_for_tab(self.app.get_active_tab())

    def _resolve_single_rsz(self, tab: str = "") -> _RszDocument:
        if str(tab or "").strip():
            target, _session, _payload = self._resolve_open_tab(tab)
            return self._rsz_for_tab(target)
        return self._active_rsz()

    def _resolve_rsz_targets(
        self,
        tabs: list[str] | None,
        all_open: bool,
    ) -> list[tuple[_RszDocument, dict[str, Any]]]:
        if not isinstance(all_open, bool):
            raise AssistantToolError(_tr("all_open must be true or false."))
        if tabs is not None and not isinstance(tabs, list):
            raise AssistantToolError(
                _tr("tabs must be an array of open tab references.")
            )
        if tabs and all_open:
            raise AssistantToolError(_tr("Provide tabs or all_open, not both."))
        resolved = []
        if tabs:
            records = [self._resolve_open_tab(str(reference)) for reference in tabs]
        elif all_open:
            records = self._open_tab_records()
        else:
            active = self.app.get_active_tab()
            records = [(active, None, self._tab_target_payload(active))]
        seen = set()
        for tab, _session, payload in records:
            if id(tab) in seen:
                continue
            try:
                document = self._rsz_for_tab(tab)
            except AssistantToolError:
                if all_open:
                    continue
                raise
            seen.add(id(tab))
            resolved.append((document, payload))
        if not resolved:
            raise AssistantToolError(_tr("No open RSZ-compatible editors matched."))
        return resolved

    def _inspect_rsz(
        self,
        tabs: list[str] | None = None,
        all_open: bool = False,
        segment: str = "main",
        instance_ids: list[int] | None = None,
        type_filter: str = "",
        offset: int = 0,
        limit: int = 20,
        include_fields: bool = False,
        include_structure: bool = False,
        array_limit: int = 64,
    ) -> dict[str, Any]:
        offset = self._integer(offset, "offset", 0, 10_000_000)
        limit = self._integer(limit, "limit", 1, 200)
        array_limit = self._integer(array_limit, "array_limit", 1, 4096)
        if not isinstance(include_fields, bool) or not isinstance(
            include_structure, bool
        ):
            raise AssistantToolError(
                _tr("include_fields and include_structure must be booleans.")
            )
        if instance_ids is not None and not isinstance(instance_ids, list):
            raise AssistantToolError(_tr("instance_ids must be an array."))
        requested_ids = None
        if instance_ids is not None:
            if len(instance_ids) > 128:
                raise AssistantToolError(
                    _tr("inspect_rsz accepts at most 128 instance IDs.")
                )
            requested_ids = [
                self._integer(item, "instance_id", 0, 10_000_000)
                for item in instance_ids
            ]
        needle = str(type_filter or "").strip().casefold()
        files = []
        for document, tab_payload in self._resolve_rsz_targets(tabs, all_open):
            self._activate_rsz_enums(document)
            all_segments = _segments(document.rsz)
            selected = _segment_by_id(document, segment)
            available = sorted(selected.instances)
            missing = []
            if requested_ids is not None:
                missing = [
                    item for item in requested_ids if item not in selected.instances
                ]
                available = [
                    item for item in requested_ids if item in selected.instances
                ]
            if needle:
                available = [
                    item
                    for item in available
                    if needle
                    in _instance_type(selected, item, document.registry)[1].casefold()
                ]
            total = len(available)
            page = available[offset : offset + limit]
            references, references_truncated = _external_references(
                document,
                max_items=max(4096, array_limit),
            )
            rsz_header = document.rsz.rsz_header
            payload = {
                "tab_id": tab_payload.get("id"),
                "file": tab_payload.get("source_path")
                or tab_payload.get("path")
                or tab_payload.get("title"),
                "format": document.kind,
                "headless": document.kind == "headless",
                "modified": bool(tab_payload.get("modified")),
                "game_version": document.rsz.game_version,
                "rsz_version": getattr(rsz_header, "version", None),
                "counts": {
                    "instances": len(document.rsz.instance_infos),
                    "objects": len(document.rsz.object_table),
                    "userdata": len(document.rsz.rsz_userdata_infos),
                    "resources": len(document.rsz.resource_infos),
                    "prefabs": len(document.rsz.prefab_infos),
                    "gameobjects": len(document.rsz.gameobjects),
                    "gameobject_references": len(document.rsz.gameobject_ref_infos),
                    "folders": len(document.rsz.folder_infos),
                    "segments": len(all_segments),
                },
                "segments": [
                    _segment_payload(item, document.registry) for item in all_segments
                ],
                "selected_segment": selected.id,
                "instances": [
                    _instance_payload(
                        selected,
                        item,
                        document.registry,
                        include_fields=include_fields,
                        array_limit=array_limit,
                    )
                    for item in page
                ],
                "matching_instance_count": total,
                "next_offset": offset + len(page)
                if offset + len(page) < total
                else None,
                "missing_instance_ids": missing,
                "external_references": references[:500],
                "external_references_truncated": references_truncated
                or len(references) > 500,
            }
            if include_structure:
                payload["structure"] = _outer_structure(document.rsz, limit)
            files.append(payload)
        return {"files": files, "count": len(files)}

    def _describe_rsz_types(self, types: list[str], tab: str = "") -> dict[str, Any]:
        if not isinstance(types, list) or not 1 <= len(types) <= 32:
            raise AssistantToolError(
                _tr("types must contain between 1 and 32 entries.")
            )
        document = self._resolve_single_rsz(tab)
        self._activate_rsz_enums(document)
        registry = document.registry
        results = []
        missing = []
        for requested in types:
            text = str(requested or "").strip()
            type_info = None
            type_id = None
            if text.casefold().startswith("0x"):
                try:
                    type_id = int(text, 16)
                except ValueError:
                    pass
                if type_id is not None:
                    type_info = registry.get_type_info(type_id)
            else:
                type_info, type_id = registry.find_type_by_name(text)
            if not type_info or type_id is None:
                missing.append(text)
                continue
            fields = []
            for field in type_info.get("fields", ()):
                record = {"name": field.get("name"), **(_field_definition(field) or {})}
                enum_type = str(field.get("original_type", "") or "")
                enum_values = (
                    EnumManager.instance().get_enum_values(enum_type)
                    if enum_type
                    else []
                )
                if enum_values:
                    record["enum_members"] = list(enum_values[:256])
                    record["enum_members_truncated"] = len(enum_values) > 256
                fields.append(record)
            results.append(
                {
                    "name": type_info.get("name"),
                    "type_id": f"0x{int(type_id):08X}",
                    "crc": type_info.get("crc"),
                    "parent": type_info.get("parent"),
                    "ancestors": (
                        registry.getTypeParents(type_info.get("name"))
                        if hasattr(registry, "getTypeParents")
                        else []
                    ),
                    "field_count": len(fields),
                    "fields": fields,
                }
            )
        return {"types": results, "missing": missing}

    def _search_rsz(
        self,
        query: str,
        tabs: list[str] | None = None,
        all_open: bool = False,
        mode: str = "all",
        limit: int = 100,
        max_array_items: int = 4096,
    ) -> dict[str, Any]:
        needle = str(query or "").strip().casefold()
        if not needle:
            raise AssistantToolError(_tr("query must not be empty."))
        mode = str(mode or "all").strip().casefold()
        modes = {"all", "type", "field", "value", "resource", "reference"}
        if mode not in modes:
            raise AssistantToolError(_tr("Unknown RSZ search mode: {mode}", mode=mode))
        limit = self._integer(limit, "limit", 1, 500)
        max_array_items = self._integer(max_array_items, "max_array_items", 1, 100000)
        results = []
        total = 0
        arrays_truncated = False

        def add(record):
            nonlocal total
            total += 1
            if len(results) < limit:
                results.append(record)

        for document, tab_payload in self._resolve_rsz_targets(tabs, all_open):
            file_name = (
                tab_payload.get("source_path")
                or tab_payload.get("path")
                or tab_payload.get("title")
            )
            external, truncated = _external_references(
                document, max_items=max_array_items
            )
            arrays_truncated = arrays_truncated or truncated
            if mode in {"all", "resource", "reference"}:
                for reference in external:
                    match_kind = (
                        "reference"
                        if reference.get("kind") == "gameobject_guid"
                        else "resource"
                    )
                    if mode != "all" and mode != match_kind:
                        continue
                    if needle in json.dumps(reference, ensure_ascii=False).casefold():
                        add(
                            {
                                "file": file_name,
                                "tab_id": tab_payload.get("id"),
                                "match": match_kind,
                                **reference,
                            }
                        )
            segments = _segments(document.rsz)
            child_map = _reference_children(segments)
            for segment in segments:
                for instance_id, fields in segment.instances.items():
                    type_id, type_name, _ = _instance_type(
                        segment, instance_id, document.registry
                    )
                    if mode in {"all", "type"} and needle in type_name.casefold():
                        add(
                            {
                                "file": file_name,
                                "tab_id": tab_payload.get("id"),
                                "segment": segment.id,
                                "instance_id": instance_id,
                                "type_name": type_name,
                                "type_id": type_id,
                                "match": "type",
                            }
                        )
                    truncated_flag = [False]
                    for path, value in (
                        _walk_fields(fields, max_array_items, truncated_flag) or ()
                    ):
                        arrays_truncated = arrays_truncated or truncated_flag[0]
                        original_type = str(getattr(value, "orig_type", "") or "")
                        if mode in {"all", "field"} and (
                            needle in path.casefold()
                            or needle in original_type.casefold()
                        ):
                            add(
                                {
                                    "file": file_name,
                                    "tab_id": tab_payload.get("id"),
                                    "segment": segment.id,
                                    "instance_id": instance_id,
                                    "type_name": type_name,
                                    "field": path,
                                    "original_type": original_type or None,
                                    "match": "field",
                                }
                            )
                        value_payload = _json_value(
                            value, segment, document.registry, 32
                        )
                        if (
                            mode in {"all", "value"}
                            and needle
                            in json.dumps(value_payload, ensure_ascii=False).casefold()
                        ):
                            add(
                                {
                                    "file": file_name,
                                    "tab_id": tab_payload.get("id"),
                                    "segment": segment.id,
                                    "instance_id": instance_id,
                                    "type_name": type_name,
                                    "field": path,
                                    "value": value_payload,
                                    "match": "value",
                                }
                            )
                        if mode in {"all", "reference"} and isinstance(
                            value, (ObjectData, UserDataData)
                        ):
                            target_id = int(value.value)
                            target_type_id, target_type, _ = _instance_type(
                                segment, target_id, document.registry
                            )
                            child = child_map.get((segment.id, target_id))
                            reference_payload = {
                                "kind": "object"
                                if isinstance(value, ObjectData)
                                else "userdata",
                                "target_instance_id": target_id,
                                "target_type": target_type,
                                "target_type_id": target_type_id,
                                "embedded_segment": child.id if child else None,
                            }
                            if (
                                needle
                                in json.dumps(
                                    reference_payload, ensure_ascii=False
                                ).casefold()
                            ):
                                add(
                                    {
                                        "file": file_name,
                                        "tab_id": tab_payload.get("id"),
                                        "segment": segment.id,
                                        "instance_id": instance_id,
                                        "type_name": type_name,
                                        "field": path,
                                        "match": "reference",
                                        **reference_payload,
                                    }
                                )
        return {
            "query": query,
            "mode": mode,
            "matches": results,
            "match_count": total,
            "truncated": total > limit,
            "arrays_truncated": arrays_truncated,
        }

    def _trace_rsz_references(
        self,
        instance_id: int,
        tab: str = "",
        segment: str = "main",
        direction: str = "both",
        depth: int = 4,
        limit: int = 500,
    ) -> dict[str, Any]:
        document = self._resolve_single_rsz(tab)
        selected = _segment_by_id(document, segment)
        instance_id = self._integer(instance_id, "instance_id", 0, 10_000_000)
        if instance_id not in selected.instances:
            raise AssistantToolError(
                _tr(
                    "RSZ instance {instance_id} was not found in segment {segment}.",
                    instance_id=instance_id,
                    segment=selected.id,
                )
            )
        direction = str(direction or "both").casefold()
        if direction not in {"outbound", "inbound", "both"}:
            raise AssistantToolError(
                _tr("Unknown trace direction: {direction}", direction=direction)
            )
        depth = self._integer(depth, "depth", 1, 12)
        limit = self._integer(limit, "limit", 1, 2000)
        all_segments = _segments(document.rsz)
        children = _reference_children(all_segments)
        gameobject_targets = _gameobject_targets(document.rsz)
        edges = []
        for source_id, fields in selected.instances.items():
            truncated = [False]
            for path, value in _walk_fields(fields, 100000, truncated) or ():
                if isinstance(value, GameObjectRefData):
                    guid = str(value.guid_str)
                    if guid == "00000000-0000-0000-0000-000000000000":
                        continue
                    local_target = gameobject_targets.get(guid.casefold())
                    if (
                        selected.id != "main"
                        or not local_target
                        or local_target["target_instance_id"] <= 0
                    ):
                        continue
                    target_id = local_target["target_instance_id"]
                    target_type_id, target_type, _ = _instance_type(
                        selected,
                        target_id,
                        document.registry,
                    )
                    edges.append(
                        {
                            "source": source_id,
                            "field": path,
                            "kind": "gameobject_guid",
                            "guid": guid,
                            "target": target_id,
                            "target_type": target_type,
                            "target_type_id": target_type_id,
                            **local_target,
                        }
                    )
                    continue
                if not isinstance(value, (ObjectData, UserDataData)):
                    continue
                target_id = int(value.value)
                if target_id <= 0:
                    continue
                target_type_id, target_type, _ = _instance_type(
                    selected, target_id, document.registry
                )
                child = children.get((selected.id, target_id))
                edges.append(
                    {
                        "source": source_id,
                        "field": path,
                        "kind": "object"
                        if isinstance(value, ObjectData)
                        else "userdata",
                        "target": target_id,
                        "target_type": target_type,
                        "target_type_id": target_type_id,
                        "embedded_segment": child.id if child else None,
                        "embedded_roots": list(child.object_table) if child else [],
                    }
                )
        outbound: dict[int, list[dict[str, Any]]] = {}
        inbound: dict[int, list[dict[str, Any]]] = {}
        for edge in edges:
            outbound.setdefault(edge["source"], []).append(edge)
            inbound.setdefault(edge["target"], []).append(edge)
        queue = deque([(instance_id, 0)])
        visited = {instance_id}
        selected_edges = []
        seen_edges = set()
        while queue and len(selected_edges) < limit:
            current, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            candidates = []
            if direction in {"outbound", "both"}:
                candidates.extend(
                    (edge, edge["target"]) for edge in outbound.get(current, ())
                )
            if direction in {"inbound", "both"}:
                candidates.extend(
                    (edge, edge["source"]) for edge in inbound.get(current, ())
                )
            for edge, neighbor in candidates:
                key = (edge["source"], edge["field"], edge["kind"], edge["target"])
                if key not in seen_edges:
                    seen_edges.add(key)
                    selected_edges.append(edge)
                    if len(selected_edges) >= limit:
                        break
                if neighbor not in visited and neighbor in selected.instances:
                    visited.add(neighbor)
                    queue.append((neighbor, current_depth + 1))
        external, external_truncated = _external_references(
            document,
            segment_ids={selected.id},
            instance_ids=visited,
        )
        relevant_external = [
            item
            for item in external
            if item.get("segment") == selected.id and item.get("instance_id") in visited
        ]
        return {
            "segment": selected.id,
            "root_instance_id": instance_id,
            "direction": direction,
            "depth": depth,
            "visited_instances": [
                _instance_payload(
                    selected,
                    item,
                    document.registry,
                    include_fields=False,
                    array_limit=1,
                )
                for item in sorted(visited)
            ],
            "edges": selected_edges,
            "edge_count": len(selected_edges),
            "truncated": len(selected_edges) >= limit and bool(queue),
            "external_references": relevant_external,
            "external_references_truncated": external_truncated,
        }

    @staticmethod
    def _validate_edit_action(action: Any) -> dict[str, Any]:
        if not isinstance(action, dict):
            raise AssistantToolError(_tr("Each RSZ edit action must be an object."))
        allowed = {
            "operation",
            "segment",
            "instance_id",
            "path",
            "value",
            "index",
            "type_name",
            "userdata_string",
            "delete_owned",
        }
        unknown = set(action) - allowed
        if unknown:
            raise AssistantToolError(
                _tr(
                    "Unsupported RSZ edit action fields: {fields}",
                    fields=sorted(unknown),
                )
            )
        required = {"operation", "instance_id", "path"}
        if not required.issubset(action):
            raise AssistantToolError(
                _tr("RSZ edit actions require operation, instance_id, and path.")
            )
        operation = str(action["operation"] or "").casefold()
        supported = {
            "set",
            "insert",
            "delete",
            "clear",
            "initialize_reference",
            "delete_owned_reference",
        }
        if operation not in supported:
            raise AssistantToolError(
                _tr("Unknown RSZ edit operation: {operation}", operation=operation)
            )
        if (
            operation in {"set", "insert"}
            and "value" not in action
            and not (operation == "insert" and action.get("type_name"))
        ):
            raise AssistantToolError(
                _tr("RSZ {operation} requires value.", operation=operation)
            )
        if "delete_owned" in action and not isinstance(action["delete_owned"], bool):
            raise AssistantToolError(_tr("delete_owned must be true or false."))
        if action.get("delete_owned") and operation != "delete":
            raise AssistantToolError(
                _tr("delete_owned is supported only by array delete actions.")
            )
        return {**action, "operation": operation}

    def _structural_edit(
        self,
        document: _RszDocument,
        segment: _RszSegment,
        action: dict[str, Any],
        target,
    ) -> dict[str, Any]:
        viewer = document.editor_viewer
        if viewer is None:
            raise AssistantToolError(
                _tr("This RSZ container has no structural editor available.")
            )
        operation = action["operation"]
        path = str(action["path"])
        type_name = str(action.get("type_name") or "").strip()
        if operation in {"initialize_reference", "delete_owned_reference"}:
            if segment.id != "main":
                raise AssistantToolError(
                    _tr(
                        "Direct structural reference edits currently require the main RSZ segment."
                    )
                )
            if not isinstance(target, (ObjectData, UserDataData)):
                raise AssistantToolError(
                    _tr("{path} is not an object or userdata reference.", path=path)
                )
            if isinstance(target, UserDataData):
                if operation == "initialize_reference":
                    if document.rsz.header is None:
                        raise AssistantToolError(
                            _tr(
                                "Headless RSZ userdata creation has no outer userdata "
                                "table; rewire to an existing userdata instance instead."
                            )
                        )
                    if not type_name:
                        raise AssistantToolError(
                            _tr("initialize_reference requires type_name.")
                        )
                    type_info, type_id = document.registry.find_type_by_name(type_name)
                    if not type_info or not type_id:
                        raise AssistantToolError(
                            _tr("RSZ type was not found: {type}", type=type_name)
                        )
                    userdata_string = str(action.get("userdata_string") or type_name)
                    success = viewer.object_operations.modify_userdata_field(
                        target,
                        userdata_string,
                        type_name,
                    )
                else:
                    old_instance_id = int(target.value)
                    target.value = 0
                    target.string = ""
                    cleanup = getattr(
                        viewer.object_operations,
                        "_try_cleanup_unused_userdata_instance",
                        None,
                    )
                    success = (
                        bool(cleanup(old_instance_id)) if callable(cleanup) else True
                    )
                    if not success:
                        success = True
                    viewer.mark_modified()
                if not success:
                    raise AssistantToolError(
                        _tr(
                            "REasy could not apply the structural edit at {path}.",
                            path=path,
                        )
                    )
                return {
                    "operation": operation,
                    "path": path,
                    "type_name": type_name or None,
                    "userdata_string": action.get("userdata_string"),
                    "owner_instance_id": _owner_instance_id(
                        document, segment.id, target
                    ),
                    "target_instance_id": int(target.value),
                }
            if operation == "initialize_reference":
                if not type_name:
                    raise AssistantToolError(
                        _tr("initialize_reference requires type_name.")
                    )
                type_info, type_id = document.registry.find_type_by_name(type_name)
                if not type_info or not type_id:
                    raise AssistantToolError(
                        _tr("RSZ type was not found: {type}", type=type_name)
                    )
                mode = "change" if target.value else "initialize"
                success = viewer.object_operations.modify_object_field(
                    target, type_name, mode
                )
            else:
                success = viewer.object_operations.modify_object_field(
                    target, action="delete"
                )
            if not success:
                raise AssistantToolError(
                    _tr(
                        "REasy could not apply the structural edit at {path}.",
                        path=path,
                    )
                )
            return {
                "operation": operation,
                "path": path,
                "type_name": type_name or None,
                "owner_instance_id": _owner_instance_id(document, segment.id, target),
                "target_instance_id": int(target.value),
            }

        collection = target
        if not isinstance(collection, ArrayData):
            raise AssistantToolError(_tr("{path} is not an array.", path=path))
        index = action.get("index")
        if operation == "insert":
            if not type_name:
                raise AssistantToolError(
                    _tr("Referenced array insertion requires type_name.")
                )
            type_info, type_id = document.registry.find_type_by_name(type_name)
            if not type_info or not type_id:
                raise AssistantToolError(
                    _tr("RSZ type was not found: {type}", type=type_name)
                )
            insert_at = (
                len(collection.values)
                if index is None
                else self._integer(index, "index", 0, len(collection.values))
            )
            created = viewer.create_array_element(
                type_name,
                collection,
                userdata_string=action.get("userdata_string"),
                notify=False,
            )
            if created is None or int(getattr(created, "value", 0) or 0) <= 0:
                if collection.values and collection.values[-1] is created:
                    collection.values.pop()
                raise AssistantToolError(
                    _tr("REasy could not create the referenced array element.")
                )
            if insert_at != len(collection.values) - 1:
                collection.values.insert(insert_at, collection.values.pop())
            _refresh_collection_metadata(collection, segment)
            return {
                "operation": operation,
                "path": path,
                "index": insert_at,
                "type_name": type_name,
                "owner_instance_id": _owner_instance_id(
                    document, segment.id, collection
                ),
                "target_instance_id": int(created.value),
            }
        delete_at = self._integer(index, "index", 0, len(collection.values) - 1)
        removed_instance_id = int(collection.values[delete_at].value)
        if not viewer.delete_array_element(collection, delete_at):
            raise AssistantToolError(
                _tr("REasy could not delete the owned array element.")
            )
        return {
            "operation": operation,
            "path": path,
            "index": delete_at,
            "delete_owned": True,
            "owner_instance_id": _owner_instance_id(document, segment.id, collection),
            "deleted_instance_id_before_remap": removed_instance_id,
        }

    def _apply_safe_edit(
        self,
        document: _RszDocument,
        segment: _RszSegment,
        action: dict[str, Any],
        fields: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        operation = action["operation"]
        path = str(action["path"])
        parent, token, target = _resolve_path(fields, path)
        before = _json_value(target, segment, document.registry, 4096)
        if operation == "set":
            replacement = _coerce_value(
                target, action["value"], segment, document.registry, path
            )
            after = _json_value(replacement, segment, document.registry, 4096)
            collection_edit = isinstance(target, (ArrayData, StructData, dict))
            if not collection_edit and before == after:
                return {
                    "operation": operation,
                    "path": path,
                    "before": before,
                    "after": after,
                }, False
            if isinstance(target, (ArrayData, StructData)):
                context = getattr(target, "_owning_context", None) or segment.context
                counters = getattr(context, "_array_counters", None)
                if isinstance(counters, dict):
                    counters.pop(id(target), None)
            _replace_child(parent, token, replacement)
            if isinstance(replacement, (ArrayData, StructData)):
                _refresh_collection_metadata(replacement, segment)
            return {
                "operation": operation,
                "path": path,
                "before": before,
                "after": after,
            }, collection_edit or before != after
        if not isinstance(target, (ArrayData, StructData)):
            raise AssistantToolError(
                _tr("{path} is not an RSZ array or struct collection.", path=path)
            )
        if operation == "clear":
            changed = bool(target.values)
            count = len(target.values)
            retained = [
                int(item.value)
                for item in target.values
                if isinstance(item, (ObjectData, UserDataData)) and item.value > 0
            ]
            target.values.clear()
            _refresh_collection_metadata(target, segment)
            return {
                "operation": operation,
                "path": path,
                "removed_count": count,
                "referenced_instances_retained": retained,
            }, changed
        if operation == "insert":
            index = action.get("index")
            insert_at = (
                len(target.values)
                if index is None
                else self._integer(index, "index", 0, len(target.values))
            )
            element = _new_collection_element(
                target,
                action["value"],
                segment,
                document.registry,
                f"{path}[{insert_at}]",
            )
            target.values.insert(insert_at, element)
            _refresh_collection_metadata(target, segment)
            return {
                "operation": operation,
                "path": path,
                "index": insert_at,
                "value": _json_value(element, segment, document.registry, 64),
            }, True
        if operation == "delete":
            index = self._integer(
                action.get("index"), "index", 0, len(target.values) - 1
            )
            removed = target.values.pop(index)
            _refresh_collection_metadata(target, segment)
            return {
                "operation": operation,
                "path": path,
                "index": index,
                "removed": _json_value(removed, segment, document.registry, 64),
                "referenced_instance_retained": isinstance(
                    removed, (ObjectData, UserDataData)
                )
                and bool(removed.value),
            }, True
        raise AssistantToolError(
            _tr("Unsupported safe RSZ edit operation: {operation}", operation=operation)
        )

    def _edit_rsz(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(actions, list) or not 1 <= len(actions) <= 256:
            raise AssistantToolError(
                _tr("actions must contain between 1 and 256 RSZ edits.")
            )
        document = self._active_rsz()
        self._activate_rsz_enums(document)
        normalized = [self._validate_edit_action(action) for action in actions]
        resolved = []
        structural = []
        for index, action in enumerate(normalized):
            segment = _segment_by_id(document, str(action.get("segment") or "main"))
            instance_id = self._integer(
                action["instance_id"], "instance_id", 0, 10_000_000
            )
            fields = segment.instances.get(instance_id)
            if not isinstance(fields, dict):
                raise AssistantToolError(
                    _tr(
                        "RSZ instance {instance_id} has no editable fields in {segment}.",
                        instance_id=instance_id,
                        segment=segment.id,
                    )
                )
            _parent, _token, target = _resolve_path(fields, str(action["path"]))
            owned_array_delete = False
            if (
                action["operation"] == "delete"
                and bool(action.get("delete_owned"))
                and isinstance(target, ArrayData)
            ):
                delete_index = self._integer(
                    action.get("index"),
                    "index",
                    0,
                    len(target.values) - 1,
                )
                owned_array_delete = isinstance(
                    target.values[delete_index],
                    (ObjectData, UserDataData),
                )
            is_structural = (
                action["operation"]
                in {
                    "initialize_reference",
                    "delete_owned_reference",
                }
                or (
                    action["operation"] == "insert"
                    and isinstance(target, ArrayData)
                    and target.element_class in {ObjectData, UserDataData}
                    and bool(action.get("type_name"))
                )
                or owned_array_delete
            )
            record = (index, action, segment, instance_id, fields, target)
            resolved.append(record)
            if is_structural:
                structural.append(record)
        if structural:
            if len(actions) != 1:
                raise AssistantToolError(
                    _tr(
                        "Structural RSZ edits must be issued alone because instance IDs can move."
                    )
                )
            _index, action, segment, _instance_id, _fields, target = structural[0]
            change = self._structural_edit(document, segment, action, target)
            document.mark_modified(segment)
            document.refresh()
            self._action_feedback.pulse_widget(
                getattr(document.editor_viewer, "tree", None)
            )
            return {
                "status": "completed",
                "changes_applied": 1,
                "changes": [change],
                "structural": True,
                "atomic": False,
                "instance_ids_may_have_changed": True,
            }

        segment_map = {segment.id: segment for _i, _a, segment, _id, _f, _t in resolved}
        contexts = {id(document.rsz): document.rsz}
        contexts.update(
            {
                id(item.context): item.context
                for item in segment_map.values()
                if item.context is not None
            }
        )
        backups = {}
        for _index, _action, segment, instance_id, fields, _target in resolved:
            key = (segment.id, instance_id)
            if key not in backups:
                backups[key] = copy.deepcopy(fields, contexts.copy())
        changes = []
        changed_count = 0
        changed_segments: dict[str, _RszSegment] = {}
        try:
            for index, action, segment, instance_id, fields, _target in resolved:
                change, changed = self._apply_safe_edit(
                    document, segment, action, fields
                )
                change.update(
                    {
                        "index": index,
                        "segment": segment.id,
                        "instance_id": instance_id,
                        "changed": changed,
                    }
                )
                changes.append(change)
                changed_count += int(changed)
                if changed:
                    changed_segments[segment.id] = segment
        except Exception:
            for (segment_id, instance_id), fields in backups.items():
                current = segment_map[segment_id].instances.get(instance_id)
                if isinstance(current, dict):
                    current.clear()
                    current.update(fields)
                else:
                    segment_map[segment_id].instances[instance_id] = fields
            for segment in segment_map.values():
                _refresh_segment_metadata(segment)
            document.refresh()
            raise
        if changed_count:
            for segment in changed_segments.values():
                document.mark_modified(segment)
            document.refresh()
            self._action_feedback.pulse_widget(
                getattr(document.editor_viewer, "tree", None)
            )
        return {
            "status": "completed" if changed_count else "no_changes",
            "changes_applied": changed_count,
            "changes": changes,
            "structural": False,
            "atomic": True,
        }
