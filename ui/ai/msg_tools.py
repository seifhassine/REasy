from __future__ import annotations

import copy
import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt

from file_handlers.msg.msg_handler import MsgHandler
from file_handlers.msg.msg_viewer import MsgViewer
from ui.ai.tool_registry import (
    AiToolDefinition,
    AssistantToolError,
    tool,
    translate_tool_text as _tr,
)

MSG_CAPABILITY = "msg"
MSG_EDIT_CAPABILITY = "msg_edit"

MSG_ASSISTANT_CAPABILITY_PROMPT = """\
MSG read tools are enabled. MSG files contain localized message entries with
UUIDs, names, SoundIDs, text for each language, and typed per-entry attributes.
Use inspect_msg for exact in-memory data and compare_msg_files for deterministic
two-file comparisons. Use list_project_files with extension=".msg" when finding
MSG files in a project.

Inspection is paginated to keep context small. Prefer a query or exact entry
selectors over requesting a whole large file, and reuse returned facts during
the current request.
"""

MSG_EDIT_ASSISTANT_CAPABILITY_PROMPT = """\
MSG edit tools are enabled. Use edit_msg to apply one or many ordered operations
to a single MSG, including entry, language, and attribute-schema changes. It
validates the complete plan before changing the visible editor. Its top-level
arguments are actions and optional tab; content belongs inside an upsert_entry
action. For changes spanning multiple open MSG files, use batch_edit_files once
without trying separate edit_msg calls first. Each batch item puts tab and tool
at the outer level and {"actions": [...]} inside arguments.

Use copy_msg_values for a source-to-destination transfer. Map "from A to B"
literally: source=A and destination=B. REasy confirms the resolved direction
before applying changes. An opened PAK-backed MSG is a valid unsaved in-memory
destination and does not need to be extracted first.

MSG edits stay unsaved until a save tool succeeds. Importing JSON replaces the
active MSG's editable data; exporting JSON writes a separate file and requires
confirmation.
"""

_MSG_PROMPT_HINT_RE = re.compile(
    r"(?:\bmsgs?\b|\.msg(?:\.\d+)?\b|\bmessage\s+files?\b|"
    r"\blocali[sz]ation\s+files?\b|(?:消息文件|本地化文件|翻译文件))",
    re.IGNORECASE,
)

_ENTRY_SELECTOR = {
    "anyOf": [{"type": "integer"}, {"type": "string"}],
    "description": "Entry index, exact UUID, or unambiguous exact entry name.",
}
_LANGUAGE_SELECTOR = {
    "anyOf": [{"type": "integer"}, {"type": "string"}],
    "description": (
        "Numeric RE language code, exact language name, or common locale "
        "alias such as en, ja, zh-CN, or pt-BR."
    ),
}
_ATTRIBUTE_SELECTOR = {
    "anyOf": [{"type": "integer"}, {"type": "string"}],
    "description": "Attribute index or unambiguous exact attribute name.",
}
_ATTRIBUTE_VALUE = {
    "anyOf": [
        {"type": "string"},
        {"type": "integer"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
    ]
}
_MSG_EDIT_ACTION_FIELDS = {
    "upsert_entry": frozenset(
        {
            "operation",
            "entry",
            "uuid",
            "name",
            "sound_id",
            "content",
            "attributes",
        }
    ),
    "duplicate_entry": frozenset(
        {"operation", "entry", "uuid", "name"}
    ),
    "delete_entry": frozenset({"operation", "entry"}),
    "upsert_attribute": frozenset(
        {
            "operation",
            "attribute",
            "name",
            "attribute_type",
            "default",
        }
    ),
    "delete_attribute": frozenset({"operation", "attribute"}),
    "upsert_language": frozenset(
        {
            "operation",
            "language",
            "language_code",
            "default_text",
        }
    ),
    "delete_language": frozenset({"operation", "language"}),
}
_MSG_EDIT_OPERATIONS = tuple(_MSG_EDIT_ACTION_FIELDS)
_MSG_COPY_SECTIONS = ("name", "sound_id", "content", "attributes")
_PARAM_TYPES = {
    "string": 2,
    "integer": 0,
    "float": 1,
}
_PARAM_TYPE_NAMES = {
    -1: "string",
    **{value: name for name, value in _PARAM_TYPES.items()},
}
_LANGUAGE_ALIASES = {
    "ja": 0,
    "jp": 0,
    "jpn": 0,
    "en": 1,
    "eng": 1,
    "fr": 2,
    "fra": 2,
    "it": 3,
    "ita": 3,
    "de": 4,
    "deu": 4,
    "es": 5,
    "spa": 5,
    "ru": 6,
    "rus": 6,
    "pl": 7,
    "pol": 7,
    "nl": 8,
    "nld": 8,
    "pt": 9,
    "ptpt": 9,
    "por": 9,
    "ptbr": 10,
    "ko": 11,
    "kr": 11,
    "kor": 11,
    "zhtw": 12,
    "zhhant": 12,
    "cht": 12,
    "zh": 13,
    "zhcn": 13,
    "zhhans": 13,
    "chs": 13,
    "fi": 14,
    "fin": 14,
    "sv": 15,
    "swe": 15,
    "da": 16,
    "dan": 16,
    "no": 17,
    "nb": 17,
    "nn": 17,
    "nor": 17,
    "cs": 18,
    "cz": 18,
    "ces": 18,
    "hu": 19,
    "hun": 19,
    "sk": 20,
    "slk": 20,
    "ar": 21,
    "ara": 21,
    "tr": 22,
    "tur": 22,
    "bg": 23,
    "bul": 23,
    "el": 24,
    "gr": 24,
    "ell": 24,
    "ro": 25,
    "ron": 25,
    "th": 26,
    "tha": 26,
    "日语": 0,
    "英语": 1,
    "英文": 1,
    "繁体中文": 12,
    "简体中文": 13,
}
_MISSING = object()

_EXPORT_MSG_JSON_ACTION = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "Export the active MSG to JSON",
)
_JSON_FILE_DETAIL = QT_TRANSLATE_NOOP(
    "ReasyAssistantTools",
    "JSON file: {path}",
)


def msg_prompt_matches(prompt: str) -> bool:
    return bool(_MSG_PROMPT_HINT_RE.search(str(prompt or "")))


def _normalize_language_selector(value: Any) -> str:
    return re.sub(r"[\s_-]+", "", str(value or "").strip().casefold())


def _edit_action_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": list(_MSG_EDIT_OPERATIONS),
            },
            "entry": _ENTRY_SELECTOR,
            "uuid": {"type": "string"},
            "name": {"type": "string"},
            "sound_id": {
                "type": "integer",
                "minimum": 0,
                "maximum": 0xFFFFFFFF,
            },
            "content": {
                "type": "array",
                "maxItems": 64,
                "description": (
                    "Localized text updates for this upsert_entry action. "
                    "Do not place content at edit_msg's top level."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "language": _LANGUAGE_SELECTOR,
                        "text": {"type": "string"},
                    },
                    "required": ["language", "text"],
                    "additionalProperties": False,
                },
            },
            "attributes": {
                "type": "array",
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "properties": {
                        "attribute": _ATTRIBUTE_SELECTOR,
                        "value": _ATTRIBUTE_VALUE,
                    },
                    "required": ["attribute", "value"],
                    "additionalProperties": False,
                },
            },
            "attribute": _ATTRIBUTE_SELECTOR,
            "attribute_type": {
                "type": "string",
                "enum": list(_PARAM_TYPES),
            },
            "default": _ATTRIBUTE_VALUE,
            "language": _LANGUAGE_SELECTOR,
            "language_code": {
                "type": "integer",
                "minimum": 0,
                "maximum": 0xFFFFFFFF,
            },
            "default_text": {"type": "string"},
        },
        "required": ["operation"],
        "additionalProperties": False,
    }


def msg_tool_definitions() -> tuple[AiToolDefinition, ...]:
    """Return MSG tools for composition into the one global registry."""

    return (
        tool(
            "inspect_msg",
            "Inspect one or more open MSG editors exactly from current in-memory data. Filter by query, exact entries, and languages; results are paginated.",
            {
                "tabs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 16,
                    "description": "Optional open tab IDs, paths, or titles.",
                },
                "all_open": {
                    "type": "boolean",
                    "description": "Inspect every open MSG editor.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional case-insensitive name, UUID, text, or attribute-value search.",
                },
                "entries": {
                    "type": "array",
                    "items": _ENTRY_SELECTOR,
                    "maxItems": 128,
                    "description": "Optional exact entries. Empty means no filter.",
                },
                "languages": {
                    "type": "array",
                    "items": _LANGUAGE_SELECTOR,
                    "maxItems": 64,
                    "description": "Optional languages whose text should be returned.",
                },
                "offset": {"type": "integer", "minimum": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Entries per file; defaults to 50.",
                },
            },
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Inspecting open MSG data"),
                QT_TRANSLATE_NOOP("AiChatDock", "Inspected open MSG data"),
            ),
            capability=MSG_CAPABILITY,
        ),
        tool(
            "compare_msg_files",
            "Read-only exact comparison of two open MSGs, including version, language and attribute schemas, entry presence/order, UUID-linked fields, localized text, and typed attributes.",
            {
                "left": {
                    "type": "string",
                    "description": "Optional left open-tab ID, path, or title.",
                },
                "right": {
                    "type": "string",
                    "description": "Optional right open-tab ID, path, or title.",
                },
                "entries": {
                    "type": "array",
                    "items": _ENTRY_SELECTOR,
                    "maxItems": 256,
                },
                "languages": {
                    "type": "array",
                    "items": _LANGUAGE_SELECTOR,
                    "maxItems": 64,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum difference details; defaults to 200.",
                },
            },
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Comparing open MSG files"),
                QT_TRANSLATE_NOOP("AiChatDock", "Compared open MSG files"),
            ),
            capability=MSG_CAPABILITY,
        ),
        tool(
            "select_msg_entry",
            "Select an MSG entry and optional language in the visible editor without changing data.",
            {
                "entry": _ENTRY_SELECTOR,
                "language": _LANGUAGE_SELECTOR,
            },
            ["entry"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Selecting the MSG entry"),
                QT_TRANSLATE_NOOP("AiChatDock", "Selected the MSG entry"),
            ),
            capability=MSG_CAPABILITY,
        ),
        tool(
            "edit_msg",
            "Atomically apply ordered entry, duplicate, attribute-schema, and language operations to one MSG. Call with top-level actions plus an optional destination tab; content is nested inside an upsert_entry action. Omit entry/attribute/language on an upsert to add a new item; provide it to update an existing item.",
            {
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 256,
                    "description": (
                        "Ordered MSG edit operations. This is the only "
                        "required top-level edit_msg field."
                    ),
                    "items": _edit_action_schema(),
                }
            },
            ["actions"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Editing MSG data"),
                QT_TRANSLATE_NOOP("AiChatDock", "Edited MSG data"),
            ),
            capability=MSG_EDIT_CAPABILITY,
            incremental=True,
            mutation=True,
            result_card=True,
        ),
        tool(
            "copy_msg_values",
            "Copy selected MSG values from one open tab to another in the literal source-to-destination direction. Entries match by UUID, languages by code, and attributes by name and type. Destination-only data is preserved.",
            {
                "source": {
                    "type": "string",
                    "description": "The FROM open-tab ID, path, or title.",
                },
                "destination": {
                    "type": "string",
                    "description": "The TO open-tab ID, path, or title.",
                },
                "entries": {
                    "type": "array",
                    "items": _ENTRY_SELECTOR,
                    "maxItems": 256,
                },
                "languages": {
                    "type": "array",
                    "items": _LANGUAGE_SELECTOR,
                    "maxItems": 64,
                },
                "attributes": {
                    "type": "array",
                    "items": _ATTRIBUTE_SELECTOR,
                    "maxItems": 128,
                },
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(_MSG_COPY_SECTIONS),
                    },
                    "maxItems": len(_MSG_COPY_SECTIONS),
                },
                "include_source_only": {
                    "type": "boolean",
                    "description": "Also add source-only entries, languages, and attributes. Default false. Destination-only data is never deleted.",
                },
            },
            ["source", "destination"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Copying MSG values"),
                QT_TRANSLATE_NOOP("AiChatDock", "Copied MSG values"),
            ),
            capability=MSG_EDIT_CAPABILITY,
            ui_edit=True,
            result_card=True,
            unsaved_result=True,
        ),
        tool(
            "import_msg_json",
            "Replace the active MSG's editable in-memory data from an existing JSON file exported by REasy. The MSG remains unsaved.",
            {
                "path": {
                    "type": "string",
                    "description": "Existing JSON file path.",
                }
            },
            ["path"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Importing MSG JSON"),
                QT_TRANSLATE_NOOP("AiChatDock", "Imported MSG JSON"),
            ),
            capability=MSG_EDIT_CAPABILITY,
            mutation=True,
            result_card=True,
        ),
        tool(
            "export_msg_json",
            "Export the active MSG's current in-memory data to a JSON file. This writes to disk and requires confirmation.",
            {
                "path": {
                    "type": "string",
                    "description": "Destination JSON path; .json is appended when omitted.",
                }
            },
            ["path"],
            activity=(
                QT_TRANSLATE_NOOP("AiChatDock", "Exporting MSG JSON"),
                QT_TRANSLATE_NOOP("AiChatDock", "Exported MSG JSON"),
            ),
            capability=MSG_CAPABILITY,
            persistent=True,
        ),
    )


class MsgAssistantToolMixin:
    """MSG implementations composed into ``ReasyAssistantTools``."""

    @staticmethod
    def _msg_for_tab(tab) -> tuple[Any, MsgViewer]:
        viewer = getattr(tab, "viewer", None)
        if tab is None or not isinstance(viewer, MsgViewer):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "The selected editor is not an MSG file.",
                    )
                )
            )
        if not isinstance(getattr(viewer, "handler", None), MsgHandler):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "The selected MSG has no parsed data.",
                    )
                )
            )
        return tab, viewer

    def _active_msg(self) -> tuple[Any, MsgViewer]:
        return self._msg_for_tab(self.app.get_active_tab())

    def _resolve_msg_tab(self, reference: str):
        tab, session, payload = self._resolve_open_tab(reference)
        _tab, viewer = self._msg_for_tab(tab)
        return tab, session, payload, viewer

    def _resolve_msg_targets(
        self,
        tabs: list[str] | None,
        all_open: bool,
    ) -> list[tuple[Any, MsgViewer, dict[str, Any]]]:
        if not isinstance(all_open, bool):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "all_open must be true or false.",
                    )
                )
            )
        if tabs is not None and not isinstance(tabs, list):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "tabs must be an array of open tab IDs, paths, or titles.",
                    )
                )
            )
        if all_open and tabs:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "inspect_msg cannot combine all_open with explicit tabs.",
                    )
                )
            )
        if tabs and len(tabs) > 16:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "inspect_msg accepts at most 16 tabs in one call.",
                    )
                )
            )

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

        targets = []
        seen: set[int] = set()
        for tab, payload in candidates:
            if tab is None or id(tab) in seen:
                continue
            try:
                _tab, viewer = self._msg_for_tab(tab)
            except AssistantToolError:
                if all_open:
                    continue
                raise
            seen.add(id(tab))
            targets.append((tab, viewer, payload))
        if not targets:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "No open MSG editors are available.",
                    )
                )
            )
        return targets

    @staticmethod
    def _entry_index(
        entries: list[dict[str, Any]],
        selector: Any,
    ) -> int:
        if isinstance(selector, bool):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG entry selector is invalid.",
                    )
                )
            )
        if isinstance(selector, int):
            index = selector
        else:
            text = str(selector or "").strip()
            if not text:
                raise AssistantToolError(
                    _tr(
                        QT_TRANSLATE_NOOP(
                            "ReasyAssistantTools",
                            "MSG entry selector must not be empty.",
                        )
                    )
                )
            if text.lstrip("-").isdigit():
                index = int(text)
            else:
                folded = text.casefold()
                uuid_matches = [
                    idx
                    for idx, entry in enumerate(entries)
                    if str(entry.get("uuid", "")).casefold() == folded
                ]
                matches = uuid_matches or [
                    idx
                    for idx, entry in enumerate(entries)
                    if str(entry.get("name", "")).casefold() == folded
                ]
                if not matches:
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "MSG entry not found: {entry}",
                            ),
                            entry=selector,
                        )
                    )
                if len(matches) > 1:
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "MSG entry name is ambiguous: {entry}",
                            ),
                            entry=selector,
                        )
                    )
                index = matches[0]
        if not 0 <= index < len(entries):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG entry index out of range: {index}",
                    ),
                    index=index,
                )
            )
        return index

    @staticmethod
    def _language_index(
        languages: list[dict[str, Any]],
        selector: Any,
    ) -> int:
        if isinstance(selector, bool):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG language selector is invalid.",
                    )
                )
            )
        if isinstance(selector, int):
            code = selector
            matches = [
                index
                for index, language in enumerate(languages)
                if int(language["code"]) == code
            ]
        else:
            text = str(selector or "").strip()
            if not text:
                raise AssistantToolError(
                    _tr(
                        QT_TRANSLATE_NOOP(
                            "ReasyAssistantTools",
                            "MSG language selector must not be empty.",
                        )
                    )
                )
            if text.lstrip("+").isdigit():
                code = int(text)
                matches = [
                    index
                    for index, language in enumerate(languages)
                    if int(language["code"]) == code
                ]
            else:
                normalized = _normalize_language_selector(text)
                alias_code = _LANGUAGE_ALIASES.get(normalized)
                if alias_code is not None:
                    matches = [
                        index
                        for index, language in enumerate(languages)
                        if int(language["code"]) == alias_code
                    ]
                else:
                    matches = [
                        index
                        for index, language in enumerate(languages)
                        if _normalize_language_selector(
                            language.get("name", "")
                        )
                        == normalized
                    ]
        if not matches:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG language not found: {language}",
                    ),
                    language=selector,
                )
            )
        if len(matches) > 1:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG language is ambiguous: {language}",
                    ),
                    language=selector,
                )
            )
        return matches[0]

    @staticmethod
    def _attribute_index(
        params: list[dict[str, Any]],
        selector: Any,
    ) -> int:
        if isinstance(selector, bool):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG attribute selector is invalid.",
                    )
                )
            )
        if isinstance(selector, int):
            index = selector
        else:
            text = str(selector or "").strip()
            if not text:
                raise AssistantToolError(
                    _tr(
                        QT_TRANSLATE_NOOP(
                            "ReasyAssistantTools",
                            "MSG attribute selector must not be empty.",
                        )
                    )
                )
            if text.lstrip("-").isdigit():
                index = int(text)
            else:
                folded = text.casefold()
                matches = [
                    idx
                    for idx, param in enumerate(params)
                    if str(param.get("name", "")).casefold() == folded
                ]
                if not matches:
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "MSG attribute not found: {attribute}",
                            ),
                            attribute=selector,
                        )
                    )
                if len(matches) > 1:
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "MSG attribute name is ambiguous: {attribute}",
                            ),
                            attribute=selector,
                        )
                    )
                index = matches[0]
        if not 0 <= index < len(params):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG attribute index out of range: {index}",
                    ),
                    index=index,
                )
            )
        return index

    @staticmethod
    def _parameter_type(value: Any) -> int:
        if isinstance(value, str):
            key = value.strip().casefold()
            if key in _PARAM_TYPES:
                return _PARAM_TYPES[key]
        elif isinstance(value, int) and not isinstance(value, bool):
            if value in _PARAM_TYPE_NAMES:
                return value
        raise AssistantToolError(
            _tr(
                QT_TRANSLATE_NOOP(
                    "ReasyAssistantTools",
                    "MSG attribute type must be string, integer, or float.",
                )
            )
        )

    @staticmethod
    def _default_attribute(param_type: int) -> Any:
        return "" if param_type in (-1, 2) else 0 if param_type == 0 else 0.0

    @staticmethod
    def _attribute_value(param_type: int, value: Any) -> Any:
        try:
            if param_type in (-1, 2):
                return "" if value is None else str(value)
            if param_type == 0:
                if isinstance(value, bool):
                    raise ValueError
                parsed = int(value)
                if not -(1 << 63) <= parsed < (1 << 63):
                    raise ValueError
                return parsed
            if param_type == 1:
                if isinstance(value, bool):
                    raise ValueError
                parsed = float(value)
                if not math.isfinite(parsed):
                    raise ValueError
                return parsed
        except (TypeError, ValueError, OverflowError) as exc:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG attribute value is incompatible with type {type}.",
                    ),
                    type=_PARAM_TYPE_NAMES.get(param_type, param_type),
                )
            ) from exc
        raise AssistantToolError(
            _tr(
                QT_TRANSLATE_NOOP(
                    "ReasyAssistantTools",
                    "Unsupported MSG attribute type: {type}",
                ),
                type=param_type,
            )
        )

    def _msg_entry_payload(
        self,
        handler: MsgHandler,
        entry: dict[str, Any],
        index: int,
        language_indices: list[int],
    ) -> dict[str, Any]:
        contents = list(entry.get("content", []))
        attributes = list(entry.get("attributes", []))
        result = {
            "index": index,
            "uuid": entry.get("uuid", ""),
            "name": entry.get("name", ""),
            "sound_id": int(entry.get("SoundID", 0)),
            "content": [
                {
                    "language_index": language_index,
                    "language_code": int(
                        handler.useLanguages[language_index]
                    ),
                    "language": handler.get_language_name(
                        handler.useLanguages[language_index]
                    ),
                    "text": (
                        contents[language_index]
                        if language_index < len(contents)
                        else ""
                    ),
                }
                for language_index in language_indices
            ],
            "attributes": [
                {
                    "index": attr_index,
                    "name": (
                        handler.userParamNames[attr_index]
                        if attr_index < len(handler.userParamNames)
                        else ""
                    ),
                    "type": int(param_type),
                    "type_name": _PARAM_TYPE_NAMES.get(
                        int(param_type),
                        "unknown",
                    ),
                    "value": (
                        attributes[attr_index]
                        if attr_index < len(attributes)
                        else None
                    ),
                }
                for attr_index, param_type in enumerate(
                    handler.userParamTypes
                )
            ],
        }
        if handler._by_hash(handler.header.get("version", 0)):
            result["name_hash"] = int(entry.get("nameHash", 0))
        else:
            result["stored_index"] = int(entry.get("index", index))
        return result

    def _inspect_msg(
        self,
        tabs: list[str] | None = None,
        all_open: bool = False,
        query: str = "",
        entries: list[Any] | None = None,
        languages: list[Any] | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if entries is not None and not isinstance(entries, list):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "entries must be an array of MSG entry selectors.",
                    )
                )
            )
        if languages is not None and not isinstance(languages, list):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "languages must be an array of MSG language selectors.",
                    )
                )
            )
        offset = self._integer(offset, "offset", 0, 1_000_000)
        limit = self._integer(limit, "limit", 1, 200)
        needle = str(query or "").casefold()
        files = []

        for tab, viewer, payload in self._resolve_msg_targets(
            tabs,
            all_open,
        ):
            handler = viewer.handler
            language_data = [
                {
                    "code": int(code),
                    "name": handler.get_language_name(code),
                }
                for code in handler.useLanguages
            ]
            language_indices = (
                list(range(len(language_data)))
                if not languages
                else list(
                    dict.fromkeys(
                        self._language_index(language_data, selector)
                        for selector in languages
                    )
                )
            )
            if entries:
                entry_indices = list(
                    dict.fromkeys(
                        self._entry_index(handler.entries, selector)
                        for selector in entries
                    )
                )
            else:
                entry_indices = list(range(len(handler.entries)))
            if needle:
                entry_indices = [
                    index
                    for index in entry_indices
                    if needle
                    in "\n".join(
                        (
                            str(handler.entries[index].get("uuid", "")),
                            str(handler.entries[index].get("name", "")),
                            *(
                                str(value)
                                for value in handler.entries[index].get(
                                    "content",
                                    [],
                                )
                            ),
                            *(
                                str(value)
                                for value in handler.entries[index].get(
                                    "attributes",
                                    [],
                                )
                            ),
                        )
                    ).casefold()
                ]
            page = entry_indices[offset : offset + limit]
            pak_backed = bool(getattr(tab, "pak_source_path", None))
            files.append(
                {
                    **self._comparison_file_payload(payload),
                    "version": int(handler.header.get("version", 0)),
                    "encrypted": bool(handler.is_encrypted),
                    "editable_in_memory": True,
                    "save_requires_copy": pak_backed,
                    "entry_count": len(handler.entries),
                    "matched_entry_count": len(entry_indices),
                    "offset": offset,
                    "returned_entry_count": len(page),
                    "truncated": offset + len(page) < len(entry_indices),
                    "next_offset": (
                        offset + len(page)
                        if offset + len(page) < len(entry_indices)
                        else None
                    ),
                    "languages": [
                        {
                            "index": index,
                            **language_data[index],
                        }
                        for index in language_indices
                    ],
                    "user_params": [
                        {
                            "index": index,
                            "name": (
                                handler.userParamNames[index]
                                if index < len(handler.userParamNames)
                                else ""
                            ),
                            "type": int(param_type),
                            "type_name": _PARAM_TYPE_NAMES.get(
                                int(param_type),
                                "unknown",
                            ),
                        }
                        for index, param_type in enumerate(
                            handler.userParamTypes
                        )
                    ],
                    "entries": [
                        self._msg_entry_payload(
                            handler,
                            handler.entries[index],
                            index,
                            language_indices,
                        )
                        for index in page
                    ],
                }
            )
        return {
            "files": files,
            "file_count": len(files),
            "read_in_memory": True,
            "tabs_activated": False,
        }

    def _comparison_inputs(
        self,
        left: str,
        right: str,
    ):
        left_ref = str(left or "").strip()
        right_ref = str(right or "").strip()
        if bool(left_ref) != bool(right_ref):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "compare_msg_files requires both left and right, or "
                        "neither when exactly two MSGs are open.",
                    )
                )
            )
        if left_ref:
            left_data = self._resolve_msg_tab(left_ref)
            right_data = self._resolve_msg_tab(right_ref)
        else:
            targets = self._resolve_msg_targets(None, True)
            if len(targets) != 2:
                raise AssistantToolError(
                    _tr(
                        QT_TRANSLATE_NOOP(
                            "ReasyAssistantTools",
                            "compare_msg_files found {count} open MSGs; specify two tab IDs.",
                        ),
                        count=len(targets),
                    )
                )
            left_tab, left_viewer, left_payload = targets[0]
            right_tab, right_viewer, right_payload = targets[1]
            left_data = (left_tab, None, left_payload, left_viewer)
            right_data = (right_tab, None, right_payload, right_viewer)
        if left_data[0] is right_data[0]:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "The MSG comparison inputs must be different tabs.",
                    )
                )
            )
        return left_data, right_data

    @staticmethod
    def _difference(
        path: str,
        left: Any,
        right: Any,
    ) -> dict[str, Any]:
        return {
            "path": path,
            "kind": (
                "right_only"
                if left is _MISSING
                else "left_only"
                if right is _MISSING
                else "changed"
            ),
            "left": (
                {"state": "missing"}
                if left is _MISSING
                else copy.deepcopy(left)
            ),
            "right": (
                {"state": "missing"}
                if right is _MISSING
                else copy.deepcopy(right)
            ),
        }

    def _compare_msg_files(
        self,
        left: str = "",
        right: str = "",
        entries: list[Any] | None = None,
        languages: list[Any] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if entries is not None and not isinstance(entries, list):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "entries must be an array of MSG entry selectors.",
                    )
                )
            )
        if languages is not None and not isinstance(languages, list):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "languages must be an array of MSG language selectors.",
                    )
                )
            )
        limit = self._integer(limit, "limit", 1, 500)
        left_data, right_data = self._comparison_inputs(left, right)
        left_tab, _left_session, left_payload, left_viewer = left_data
        right_tab, _right_session, right_payload, right_viewer = right_data
        left_handler = left_viewer.handler
        right_handler = right_viewer.handler
        differences: list[dict[str, Any]] = []

        def add(path: str, left_value: Any, right_value: Any):
            if left_value != right_value:
                differences.append(
                    self._difference(path, left_value, right_value)
                )

        add(
            "header.version",
            int(left_handler.header.get("version", 0)),
            int(right_handler.header.get("version", 0)),
        )

        left_languages = [
            {
                "code": int(code),
                "name": left_handler.get_language_name(code),
            }
            for code in left_handler.useLanguages
        ]
        right_languages = [
            {
                "code": int(code),
                "name": right_handler.get_language_name(code),
            }
            for code in right_handler.useLanguages
        ]
        selected_codes: set[int] | None = None
        selection_misses = []
        if languages:
            selected_codes = set()
            for selector in languages:
                matched = False
                for language_data in (left_languages, right_languages):
                    try:
                        index = self._language_index(
                            language_data,
                            selector,
                        )
                    except AssistantToolError:
                        continue
                    selected_codes.add(
                        int(language_data[index]["code"])
                    )
                    matched = True
                if not matched:
                    selection_misses.append(
                        {"kind": "language", "selector": selector}
                    )

        left_codes = [
            int(item["code"])
            for item in left_languages
            if selected_codes is None or int(item["code"]) in selected_codes
        ]
        right_codes = [
            int(item["code"])
            for item in right_languages
            if selected_codes is None or int(item["code"]) in selected_codes
        ]
        left_code_set = set(left_codes)
        right_code_set = set(right_codes)
        for code in sorted(left_code_set | right_code_set):
            if code not in left_code_set:
                add(f"language[{code}]", _MISSING, code)
            elif code not in right_code_set:
                add(f"language[{code}]", code, _MISSING)
        if left_code_set == right_code_set:
            add("languages.order", left_codes, right_codes)
        shared_codes = left_code_set & right_code_set
        left_language_index = {
            int(code): index
            for index, code in enumerate(left_handler.useLanguages)
        }
        right_language_index = {
            int(code): index
            for index, code in enumerate(right_handler.useLanguages)
        }

        def attribute_schema(handler: MsgHandler):
            occurrences: dict[str, int] = {}
            order = []
            records = {}
            for index, param_type in enumerate(handler.userParamTypes):
                name = (
                    handler.userParamNames[index]
                    if index < len(handler.userParamNames)
                    else ""
                )
                folded = name.casefold()
                occurrence = occurrences.get(folded, 0) + 1
                occurrences[folded] = occurrence
                key = (folded, occurrence)
                order.append(key)
                records[key] = (
                    index,
                    {"name": name, "type": int(param_type)},
                )
            return order, records

        left_param_order, left_param_records = attribute_schema(left_handler)
        right_param_order, right_param_records = attribute_schema(
            right_handler
        )
        shared_params = []
        all_param_keys = set(left_param_records) | set(right_param_records)
        for key in sorted(all_param_keys):
            left_record = left_param_records.get(key)
            right_record = right_param_records.get(key)
            param = (
                left_record[1]
                if left_record is not None
                else right_record[1]
            )
            suffix = f"#{key[1]}" if key[1] > 1 else ""
            path = f"attribute_schema[{param['name']}{suffix}]"
            if left_record is None:
                add(path, _MISSING, right_record[1])
                continue
            if right_record is None:
                add(path, left_record[1], _MISSING)
                continue
            left_index, left_param = left_record
            right_index, right_param = right_record
            add(f"{path}.name", left_param["name"], right_param["name"])
            add(f"{path}.type", left_param["type"], right_param["type"])
            if left_param["type"] == right_param["type"]:
                shared_params.append(
                    (
                        key,
                        left_index,
                        right_index,
                        left_param,
                    )
                )
        if set(left_param_order) == set(right_param_order):
            def ordered_names(order, records):
                return [
                    (
                        records[key][1]["name"]
                        + (f"#{key[1]}" if key[1] > 1 else "")
                    )
                    for key in order
                ]

            add(
                "attribute_schema.order",
                ordered_names(left_param_order, left_param_records),
                ordered_names(right_param_order, right_param_records),
            )

        left_entries = {
            str(entry.get("uuid", "")).casefold(): (index, entry)
            for index, entry in enumerate(left_handler.entries)
        }
        right_entries = {
            str(entry.get("uuid", "")).casefold(): (index, entry)
            for index, entry in enumerate(right_handler.entries)
        }
        selected_uuids: set[str] | None = None
        if entries:
            selected_uuids = set()
            for selector in entries:
                matched = False
                for handler in (left_handler, right_handler):
                    try:
                        index = self._entry_index(
                            handler.entries,
                            selector,
                        )
                    except AssistantToolError:
                        continue
                    selected_uuids.add(
                        str(
                            handler.entries[index].get("uuid", "")
                        ).casefold()
                    )
                    matched = True
                if not matched:
                    selection_misses.append(
                        {"kind": "entry", "selector": selector}
                    )
        all_uuids = set(left_entries) | set(right_entries)
        if selected_uuids is not None:
            all_uuids.intersection_update(selected_uuids)

        left_order = [
            str(entry.get("uuid", "")).casefold()
            for entry in left_handler.entries
            if str(entry.get("uuid", "")).casefold() in all_uuids
        ]
        right_order = [
            str(entry.get("uuid", "")).casefold()
            for entry in right_handler.entries
            if str(entry.get("uuid", "")).casefold() in all_uuids
        ]
        if set(left_order) == set(right_order):
            add("entries.order", left_order, right_order)

        for entry_uuid in sorted(all_uuids):
            left_entry_data = left_entries.get(entry_uuid)
            right_entry_data = right_entries.get(entry_uuid)
            path = f"entry[{entry_uuid}]"
            if left_entry_data is None:
                add(path, _MISSING, {"present": True})
                continue
            if right_entry_data is None:
                add(path, {"present": True}, _MISSING)
                continue
            _left_index, left_entry = left_entry_data
            _right_index, right_entry = right_entry_data
            add(
                f"{path}.name",
                left_entry.get("name", ""),
                right_entry.get("name", ""),
            )
            add(
                f"{path}.sound_id",
                int(left_entry.get("SoundID", 0)),
                int(right_entry.get("SoundID", 0)),
            )
            left_content = list(left_entry.get("content", []))
            right_content = list(right_entry.get("content", []))
            for code in sorted(shared_codes):
                left_index = left_language_index[code]
                right_index = right_language_index[code]
                add(
                    f"{path}.content[{code}]",
                    (
                        left_content[left_index]
                        if left_index < len(left_content)
                        else ""
                    ),
                    (
                        right_content[right_index]
                        if right_index < len(right_content)
                        else ""
                    ),
                )
            left_attributes = list(left_entry.get("attributes", []))
            right_attributes = list(right_entry.get("attributes", []))
            for key, left_index, right_index, param in shared_params:
                suffix = f"#{key[1]}" if key[1] > 1 else ""
                add(
                    f"{path}.attribute[{param['name']}{suffix}]",
                    (
                        left_attributes[left_index]
                        if left_index < len(left_attributes)
                        else self._default_attribute(
                            param["type"]
                        )
                    ),
                    (
                        right_attributes[right_index]
                        if right_index < len(right_attributes)
                        else self._default_attribute(
                            param["type"]
                        )
                    ),
                )

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

    def _focus_msg_entry(
        self,
        viewer: MsgViewer,
        index: int,
        language_code: int | None = None,
    ) -> bool:
        if viewer.search_edit.text():
            viewer.search_edit.clear()
        if language_code is not None:
            language_data = [
                {
                    "code": int(code),
                    "name": viewer.handler.get_language_name(code),
                }
                for code in viewer.handler.useLanguages
            ]
            language_index = self._language_index(
                language_data,
                language_code,
            )
            viewer.language_combo.setCurrentIndex(language_index)
        model = viewer.tree.model()
        for row in range(model.rowCount()):
            item = model.item(row, 0)
            meta = item.data(Qt.UserRole) if item is not None else None
            if meta and int(meta.get("entry_index", -1)) == index:
                model_index = model.index(row, 0)
                viewer.tree.setCurrentIndex(model_index)
                viewer._update_details_panel()
                return self._action_feedback.pulse_index(
                    viewer.tree,
                    model_index,
                )
        return False

    def _select_msg_entry(
        self,
        entry: Any,
        language: Any = None,
    ) -> dict[str, Any]:
        _tab, viewer = self._active_msg()
        index = self._entry_index(viewer.handler.entries, entry)
        language_code = None
        if language is not None:
            language_data = [
                {
                    "code": int(code),
                    "name": viewer.handler.get_language_name(code),
                }
                for code in viewer.handler.useLanguages
            ]
            language_index = self._language_index(
                language_data,
                language,
            )
            language_code = int(language_data[language_index]["code"])
        self._focus_msg_entry(viewer, index, language_code)
        selected = viewer.handler.entries[index]
        return {
            "entry_index": index,
            "uuid": selected.get("uuid", ""),
            "name": selected.get("name", ""),
            "language_code": language_code,
        }

    def _plan_msg_edits(
        self,
        handler: MsgHandler,
        actions: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(actions, list) or not actions:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "edit_msg requires at least one action.",
                    )
                )
            )
        if len(actions) > 256:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "edit_msg accepts at most 256 actions.",
                    )
                )
            )
        data = copy.deepcopy(handler.to_json_dict())
        results = []

        def languages() -> list[dict[str, Any]]:
            return data["languages"]

        def params() -> list[dict[str, Any]]:
            return data["user_params"]

        def entries() -> list[dict[str, Any]]:
            return data["entries"]

        for action_index, action in enumerate(actions):
            if not isinstance(action, dict):
                raise AssistantToolError(
                    _tr(
                        QT_TRANSLATE_NOOP(
                            "ReasyAssistantTools",
                            "MSG edit action {index} must be an object.",
                        ),
                        index=action_index,
                    )
                )
            operation = str(action.get("operation") or "")
            if operation not in _MSG_EDIT_ACTION_FIELDS:
                raise AssistantToolError(
                    _tr(
                        QT_TRANSLATE_NOOP(
                            "ReasyAssistantTools",
                            "Unknown MSG edit operation: {operation}",
                        ),
                        operation=operation,
                    )
                )
            unexpected = set(action) - _MSG_EDIT_ACTION_FIELDS[operation]
            if unexpected:
                raise AssistantToolError(
                    _tr(
                        QT_TRANSLATE_NOOP(
                            "ReasyAssistantTools",
                            "Unsupported fields for {operation}: {fields}",
                        ),
                        operation=operation,
                        fields=sorted(unexpected),
                    )
                )

            if operation == "upsert_entry":
                is_new = "entry" not in action
                if is_new:
                    entry = {
                        "uuid": str(uuid.uuid4()),
                        "name": "",
                        "SoundID": 0,
                        "content": ["" for _ in languages()],
                        "attributes": [
                            self._default_attribute(
                                int(param["type"])
                            )
                            for param in params()
                        ],
                    }
                    entries().append(entry)
                    entry_index = len(entries()) - 1
                else:
                    entry_index = self._entry_index(
                        entries(),
                        action["entry"],
                    )
                    entry = entries()[entry_index]
                changed = []
                if "uuid" in action:
                    try:
                        parsed_uuid = str(
                            uuid.UUID(str(action["uuid"]))
                        ).lower()
                    except (ValueError, AttributeError) as exc:
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "MSG entry UUID is invalid.",
                                )
                            )
                        ) from exc
                    if any(
                        index != entry_index
                        and str(other.get("uuid", "")).casefold()
                        == parsed_uuid.casefold()
                        for index, other in enumerate(entries())
                    ):
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "MSG entry UUID already exists: {uuid}",
                                ),
                                uuid=parsed_uuid,
                            )
                        )
                    if str(entry.get("uuid", "")).casefold() != (
                        parsed_uuid.casefold()
                    ):
                        entry["uuid"] = parsed_uuid
                        changed.append("uuid")
                if "name" in action:
                    name = str(action["name"])
                    if entry.get("name", "") != name:
                        entry["name"] = name
                        changed.append("name")
                if "sound_id" in action:
                    sound_id = action["sound_id"]
                    if (
                        isinstance(sound_id, bool)
                        or not isinstance(sound_id, int)
                        or not 0 <= sound_id <= 0xFFFFFFFF
                    ):
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "MSG SoundID must be an unsigned 32-bit integer.",
                                )
                            )
                        )
                    if int(entry.get("SoundID", 0)) != sound_id:
                        entry["SoundID"] = sound_id
                        changed.append("sound_id")
                content_updates = action.get("content", [])
                if not isinstance(content_updates, list):
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "MSG content updates must be an array.",
                            )
                        )
                    )
                seen_languages = set()
                for update in content_updates:
                    if not isinstance(update, dict) or set(update) != {
                        "language",
                        "text",
                    }:
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "MSG content update is invalid.",
                                )
                            )
                        )
                    language_index = self._language_index(
                        languages(),
                        update["language"],
                    )
                    if language_index in seen_languages:
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "MSG language was updated more than once in one entry action.",
                                )
                            )
                        )
                    seen_languages.add(language_index)
                    text = str(update["text"])
                    if entry["content"][language_index] != text:
                        entry["content"][language_index] = text
                        changed.append(
                            f"content[{languages()[language_index]['code']}]"
                        )
                attribute_updates = action.get("attributes", [])
                if not isinstance(attribute_updates, list):
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "MSG attribute updates must be an array.",
                            )
                        )
                    )
                seen_attributes = set()
                for update in attribute_updates:
                    if not isinstance(update, dict) or set(update) != {
                        "attribute",
                        "value",
                    }:
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "MSG attribute update is invalid.",
                                )
                            )
                        )
                    attr_index = self._attribute_index(
                        params(),
                        update["attribute"],
                    )
                    if attr_index in seen_attributes:
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "MSG attribute was updated more than once in one entry action.",
                                )
                            )
                        )
                    seen_attributes.add(attr_index)
                    value = self._attribute_value(
                        int(params()[attr_index]["type"]),
                        update["value"],
                    )
                    if entry["attributes"][attr_index] != value:
                        entry["attributes"][attr_index] = value
                        changed.append(
                            f"attribute[{params()[attr_index]['name']}]"
                        )
                results.append(
                    {
                        "operation": operation,
                        "created": is_new,
                        "entry_index": entry_index,
                        "entry_uuid": entry["uuid"],
                        "changed": changed,
                    }
                )
                continue

            if operation == "duplicate_entry":
                if "entry" not in action:
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "duplicate_entry requires entry.",
                            )
                        )
                    )
                source_index = self._entry_index(
                    entries(),
                    action["entry"],
                )
                duplicate = copy.deepcopy(entries()[source_index])
                try:
                    duplicate_uuid = str(
                        uuid.UUID(
                            str(action.get("uuid") or uuid.uuid4())
                        )
                    ).lower()
                except (ValueError, AttributeError) as exc:
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "MSG entry UUID is invalid.",
                            )
                        )
                    ) from exc
                if any(
                    str(other.get("uuid", "")).casefold()
                    == duplicate_uuid.casefold()
                    for other in entries()
                ):
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "MSG entry UUID already exists: {uuid}",
                            ),
                            uuid=duplicate_uuid,
                        )
                    )
                duplicate["uuid"] = duplicate_uuid
                duplicate["name"] = str(
                    action.get(
                        "name",
                        f"{duplicate.get('name', '')} (Copy)",
                    )
                )
                entries().append(duplicate)
                results.append(
                    {
                        "operation": operation,
                        "created": True,
                        "source_entry_index": source_index,
                        "entry_index": len(entries()) - 1,
                        "entry_uuid": duplicate_uuid,
                        "entry_name": duplicate["name"],
                        "changed": ["created"],
                    }
                )
                continue

            if operation == "delete_entry":
                if "entry" not in action:
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "delete_entry requires entry.",
                            )
                        )
                    )
                entry_index = self._entry_index(
                    entries(),
                    action["entry"],
                )
                removed = entries().pop(entry_index)
                results.append(
                    {
                        "operation": operation,
                        "entry_index": entry_index,
                        "entry_uuid": removed.get("uuid", ""),
                        "entry_name": removed.get("name", ""),
                    }
                )
                continue

            if operation == "upsert_attribute":
                is_new = "attribute" not in action
                if is_new:
                    name = str(action.get("name") or "").strip()
                    if not name:
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "A new MSG attribute requires name.",
                                )
                            )
                        )
                    if "attribute_type" not in action:
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "A new MSG attribute requires attribute_type.",
                                )
                            )
                        )
                    param_type = self._parameter_type(
                        action["attribute_type"]
                    )
                    if any(
                        str(param.get("name", "")).casefold()
                        == name.casefold()
                        for param in params()
                    ):
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "MSG attribute name already exists: {name}",
                                ),
                                name=name,
                            )
                        )
                    param = {"name": name, "type": param_type}
                    params().append(param)
                    attr_index = len(params()) - 1
                    default = self._attribute_value(
                        param_type,
                        action.get(
                            "default",
                            self._default_attribute(param_type),
                        ),
                    )
                    for entry in entries():
                        entry["attributes"].append(copy.deepcopy(default))
                    changed = ["created"]
                else:
                    if "default" in action:
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "default is only valid when adding an MSG attribute.",
                                )
                            )
                        )
                    attr_index = self._attribute_index(
                        params(),
                        action["attribute"],
                    )
                    param = params()[attr_index]
                    changed = []
                    if "name" in action:
                        name = str(action["name"]).strip()
                        if not name:
                            raise AssistantToolError(
                                _tr(
                                    QT_TRANSLATE_NOOP(
                                        "ReasyAssistantTools",
                                        "MSG attribute name must not be empty.",
                                    )
                                )
                            )
                        if any(
                            index != attr_index
                            and str(other.get("name", "")).casefold()
                            == name.casefold()
                            for index, other in enumerate(params())
                        ):
                            raise AssistantToolError(
                                _tr(
                                    QT_TRANSLATE_NOOP(
                                        "ReasyAssistantTools",
                                        "MSG attribute name already exists: {name}",
                                    ),
                                    name=name,
                                )
                            )
                        if param.get("name", "") != name:
                            param["name"] = name
                            changed.append("name")
                    if "attribute_type" in action:
                        param_type = self._parameter_type(
                            action["attribute_type"]
                        )
                        if int(param["type"]) != param_type:
                            for entry in entries():
                                entry["attributes"][attr_index] = (
                                    self._attribute_value(
                                        param_type,
                                        entry["attributes"][attr_index],
                                    )
                                )
                            param["type"] = param_type
                            changed.append("type")
                results.append(
                    {
                        "operation": operation,
                        "created": is_new,
                        "attribute_index": attr_index,
                        "attribute_name": param["name"],
                        "attribute_type": int(param["type"]),
                        "changed": changed,
                    }
                )
                continue

            if operation == "delete_attribute":
                if "attribute" not in action:
                    raise AssistantToolError(
                        _tr(
                            QT_TRANSLATE_NOOP(
                                "ReasyAssistantTools",
                                "delete_attribute requires attribute.",
                            )
                        )
                    )
                attr_index = self._attribute_index(
                    params(),
                    action["attribute"],
                )
                removed = params().pop(attr_index)
                for entry in entries():
                    entry["attributes"].pop(attr_index)
                results.append(
                    {
                        "operation": operation,
                        "attribute_index": attr_index,
                        "attribute_name": removed.get("name", ""),
                    }
                )
                continue

            if operation == "upsert_language":
                is_new = "language" not in action
                if is_new:
                    if "language_code" not in action:
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "A new MSG language requires language_code.",
                                )
                            )
                        )
                    language_code = self._integer(
                        action["language_code"],
                        "language_code",
                        0,
                        0xFFFFFFFF,
                    )
                    if any(
                        int(language["code"]) == language_code
                        for language in languages()
                    ):
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "MSG language code already exists: {code}",
                                ),
                                code=language_code,
                            )
                        )
                    language = {
                        "code": language_code,
                        "name": handler.get_language_name(language_code),
                    }
                    languages().append(language)
                    language_index = len(languages()) - 1
                    default_text = str(action.get("default_text", ""))
                    for entry in entries():
                        entry["content"].append(default_text)
                    changed = ["created"]
                else:
                    if "default_text" in action:
                        raise AssistantToolError(
                            _tr(
                                QT_TRANSLATE_NOOP(
                                    "ReasyAssistantTools",
                                    "default_text is only valid when adding an MSG language.",
                                )
                            )
                        )
                    language_index = self._language_index(
                        languages(),
                        action["language"],
                    )
                    language = languages()[language_index]
                    changed = []
                    if "language_code" in action:
                        language_code = self._integer(
                            action["language_code"],
                            "language_code",
                            0,
                            0xFFFFFFFF,
                        )
                        if any(
                            index != language_index
                            and int(other["code"]) == language_code
                            for index, other in enumerate(languages())
                        ):
                            raise AssistantToolError(
                                _tr(
                                    QT_TRANSLATE_NOOP(
                                        "ReasyAssistantTools",
                                        "MSG language code already exists: {code}",
                                    ),
                                    code=language_code,
                                )
                            )
                        if int(language["code"]) != language_code:
                            language["code"] = language_code
                            language["name"] = handler.get_language_name(
                                language_code
                            )
                            changed.append("code")
                results.append(
                    {
                        "operation": operation,
                        "created": is_new,
                        "language_index": language_index,
                        "language_code": int(language["code"]),
                        "changed": changed,
                    }
                )
                continue

            if "language" not in action:
                raise AssistantToolError(
                    _tr(
                        QT_TRANSLATE_NOOP(
                            "ReasyAssistantTools",
                            "delete_language requires language.",
                        )
                    )
                )
            language_index = self._language_index(
                languages(),
                action["language"],
            )
            removed = languages().pop(language_index)
            for entry in entries():
                entry["content"].pop(language_index)
            results.append(
                {
                    "operation": operation,
                    "language_index": language_index,
                    "language_code": int(removed["code"]),
                }
            )
        return data, results

    def _apply_msg_data(
        self,
        viewer: MsgViewer,
        data: dict[str, Any],
    ):
        viewer.handler.load_json_dict(data)
        viewer._refresh_after_import()
        viewer._set_modified(True)

    def _edit_msg(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        return self._run_incremental_steps(self._edit_msg_steps(actions))

    def _edit_msg_steps(self, actions: list[dict[str, Any]]):
        _tab, viewer = self._active_msg()
        data, results = self._plan_msg_edits(viewer.handler, actions)
        applied_results = [
            result
            for result in results
            if result.get("created")
            or result["operation"].startswith("delete_")
            or bool(result.get("changed"))
        ]
        if applied_results:
            self._apply_msg_data(viewer, data)
        for index, result in enumerate(results):
            entry_uuid = result.get("entry_uuid")
            if (
                applied_results
                and entry_uuid
                and result["operation"] != "delete_entry"
            ):
                try:
                    entry_index = self._entry_index(
                        viewer.handler.entries,
                        entry_uuid,
                    )
                except AssistantToolError:
                    entry_index = None
                if entry_index is not None:
                    self._focus_msg_entry(viewer, entry_index)
            elif result["operation"] == "delete_entry":
                self._action_feedback.pulse_widget(viewer.tree)
            elif "language_index" in result:
                self._action_feedback.pulse_widget(
                    viewer.language_combo
                )
            elif "attribute_index" in result:
                self._action_feedback.pulse_widget(
                    viewer.attributes_group
                )
            yield {
                "stage": "editing_msg",
                "current": index + 1,
                "completed": index,
                "total": len(results),
                "item": result["operation"],
            }
        return {
            "status": "completed" if applied_results else "no_changes",
            "requested_count": len(results),
            "applied_count": len(applied_results),
            "changes_planned": len(applied_results),
            "changes_applied": len(applied_results),
            "results": results,
            "entry_count": len(viewer.handler.entries),
            "language_count": len(viewer.handler.useLanguages),
            "attribute_count": len(viewer.handler.userParamTypes),
            "complete": True,
        }

    def _selected_copy_indices(
        self,
        items: list[dict[str, Any]],
        selectors: list[Any] | None,
        resolver,
    ) -> list[int]:
        if not selectors:
            return list(range(len(items)))
        return list(
            dict.fromkeys(resolver(items, selector) for selector in selectors)
        )

    def _copy_msg_values(
        self,
        source: str,
        destination: str,
        entries: list[Any] | None = None,
        languages: list[Any] | None = None,
        attributes: list[Any] | None = None,
        sections: list[str] | None = None,
        include_source_only: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(include_source_only, bool):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "include_source_only must be true or false.",
                    )
                )
            )
        if any(
            value is not None and not isinstance(value, list)
            for value in (entries, languages, attributes, sections)
        ):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG copy selectors must be arrays.",
                    )
                )
            )
        source_data = self._resolve_msg_tab(source)
        destination_data = self._resolve_msg_tab(destination)
        source_tab, _source_session, source_payload, source_viewer = (
            source_data
        )
        target_tab, _target_session, target_payload, target_viewer = (
            destination_data
        )
        if source_tab is target_tab:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "The MSG source and destination must be different files.",
                    )
                )
            )
        copy_metadata = {
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
            "destination_pak_backed": bool(
                target_payload.get("pak_backed")
            ),
            "editable_in_memory": True,
            "save_requires_copy": bool(target_payload.get("pak_backed")),
        }
        source_json = source_viewer.handler.to_json_dict()
        target_json = copy.deepcopy(target_viewer.handler.to_json_dict())
        source_sections = (
            set(_MSG_COPY_SECTIONS)
            if not sections
            else {str(value) for value in sections}
        )
        unknown_sections = source_sections.difference(_MSG_COPY_SECTIONS)
        if unknown_sections:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "Unknown MSG copy sections: {sections}",
                    ),
                    sections=sorted(unknown_sections),
                )
            )
        source_entry_indices = self._selected_copy_indices(
            source_json["entries"],
            entries,
            self._entry_index,
        )
        source_language_indices = (
            self._selected_copy_indices(
                source_json["languages"],
                languages,
                self._language_index,
            )
            if "content" in source_sections
            else []
        )
        source_attribute_indices = (
            self._selected_copy_indices(
                source_json["user_params"],
                attributes,
                self._attribute_index,
            )
            if "attributes" in source_sections
            else []
        )
        changes = []
        source_only = []
        incompatible = []
        destination_only = []

        target_language_codes = {
            int(language["code"]): index
            for index, language in enumerate(target_json["languages"])
        }
        selected_source_codes = {
            int(source_json["languages"][index]["code"])
            for index in source_language_indices
        }
        if "content" in source_sections and not languages:
            destination_only.extend(
                f"language[{code}]"
                for code in target_language_codes
                if code not in selected_source_codes
            )
        language_map = {}
        for source_index in source_language_indices:
            source_language = source_json["languages"][source_index]
            code = int(source_language["code"])
            target_index = target_language_codes.get(code)
            if target_index is None:
                if not include_source_only:
                    source_only.append(f"language[{code}]")
                    continue
                target_json["languages"].append(
                    copy.deepcopy(source_language)
                )
                target_index = len(target_json["languages"]) - 1
                target_language_codes[code] = target_index
                for entry in target_json["entries"]:
                    entry["content"].append("")
                changes.append(
                    {
                        "path": f"language[{code}]",
                        "kind": "added",
                    }
                )
            language_map[source_index] = target_index

        target_params_by_name = {
            str(param.get("name", "")).casefold(): (index, param)
            for index, param in enumerate(target_json["user_params"])
        }
        selected_source_param_names = {
            str(
                source_json["user_params"][index].get("name", "")
            ).casefold()
            for index in source_attribute_indices
        }
        if "attributes" in source_sections and not attributes:
            destination_only.extend(
                f"attribute[{param.get('name', '')}]"
                for key, (_index, param) in target_params_by_name.items()
                if key not in selected_source_param_names
            )
        attribute_map = {}
        for source_index in source_attribute_indices:
            source_param = source_json["user_params"][source_index]
            key = str(source_param.get("name", "")).casefold()
            target_match = target_params_by_name.get(key)
            if target_match is None:
                if not include_source_only:
                    source_only.append(
                        f"attribute[{source_param.get('name', '')}]"
                    )
                    continue
                target_json["user_params"].append(
                    copy.deepcopy(source_param)
                )
                target_index = len(target_json["user_params"]) - 1
                target_params_by_name[key] = (
                    target_index,
                    target_json["user_params"][target_index],
                )
                default = self._default_attribute(
                    int(source_param["type"])
                )
                for entry in target_json["entries"]:
                    entry["attributes"].append(copy.deepcopy(default))
                changes.append(
                    {
                        "path": f"attribute[{source_param.get('name', '')}]",
                        "kind": "added",
                    }
                )
            else:
                target_index, target_param = target_match
                if int(target_param["type"]) != int(source_param["type"]):
                    incompatible.append(
                        {
                            "path": (
                                f"attribute[{source_param.get('name', '')}]"
                            ),
                            "source_type": int(source_param["type"]),
                            "destination_type": int(target_param["type"]),
                        }
                    )
                    continue
            attribute_map[source_index] = target_index

        target_entries = {
            str(entry.get("uuid", "")).casefold(): (index, entry)
            for index, entry in enumerate(target_json["entries"])
        }
        selected_source_uuids = set()
        for source_index in source_entry_indices:
            source_entry = source_json["entries"][source_index]
            entry_uuid = str(source_entry.get("uuid", "")).casefold()
            selected_source_uuids.add(entry_uuid)
            target_match = target_entries.get(entry_uuid)
            entry_added = target_match is None
            if target_match is None:
                if not include_source_only:
                    source_only.append(f"entry[{entry_uuid}]")
                    continue
                target_entry = {
                    "uuid": source_entry["uuid"],
                    "name": (
                        source_entry.get("name", "")
                        if "name" in source_sections
                        else ""
                    ),
                    "SoundID": (
                        int(source_entry.get("SoundID", 0))
                        if "sound_id" in source_sections
                        else 0
                    ),
                    "content": [
                        "" for _ in target_json["languages"]
                    ],
                    "attributes": [
                        self._default_attribute(int(param["type"]))
                        for param in target_json["user_params"]
                    ],
                }
                target_json["entries"].append(target_entry)
                target_index = len(target_json["entries"]) - 1
                target_entries[entry_uuid] = (target_index, target_entry)
            else:
                target_index, target_entry = target_match
            if "name" in source_sections:
                old = target_entry.get("name", "")
                new = source_entry.get("name", "")
                if old != new:
                    target_entry["name"] = new
                    changes.append(
                        {
                            "path": f"entry[{entry_uuid}].name",
                            "kind": "changed",
                            "old": old,
                            "new": new,
                        }
                    )
            if "sound_id" in source_sections:
                old = int(target_entry.get("SoundID", 0))
                new = int(source_entry.get("SoundID", 0))
                if old != new:
                    target_entry["SoundID"] = new
                    changes.append(
                        {
                            "path": f"entry[{entry_uuid}].sound_id",
                            "kind": "changed",
                            "old": old,
                            "new": new,
                        }
                    )
            if "content" in source_sections:
                for source_language, target_language in language_map.items():
                    old = target_entry["content"][target_language]
                    new = source_entry["content"][source_language]
                    if old != new:
                        target_entry["content"][target_language] = new
                        if not entry_added:
                            code = source_json["languages"][
                                source_language
                            ]["code"]
                            changes.append(
                                {
                                    "path": (
                                        f"entry[{entry_uuid}].content[{code}]"
                                    ),
                                    "kind": "changed",
                                    "old": old,
                                    "new": new,
                                }
                            )
            if "attributes" in source_sections:
                for source_attr, target_attr in attribute_map.items():
                    old = target_entry["attributes"][target_attr]
                    new = source_entry["attributes"][source_attr]
                    if old != new:
                        target_entry["attributes"][target_attr] = (
                            copy.deepcopy(new)
                        )
                        if not entry_added:
                            name = source_json["user_params"][
                                source_attr
                            ]["name"]
                            changes.append(
                                {
                                    "path": (
                                        f"entry[{entry_uuid}].attribute[{name}]"
                                    ),
                                    "kind": "changed",
                                    "old": old,
                                    "new": new,
                                }
                            )
            if entry_added:
                changes.append(
                    {"path": f"entry[{entry_uuid}]", "kind": "added"}
                )

        if not entries:
            destination_only.extend(
                f"entry[{entry_uuid}]"
                for entry_uuid in target_entries
                if entry_uuid not in selected_source_uuids
            )
        if changes and not self._confirm_file_copy_for_request(
            source_payload,
            target_payload,
            len(changes),
        ):
            return {
                "status": "cancelled",
                "cancelled": True,
                "changes_planned": len(changes),
                "changes_applied": 0,
                **copy_metadata,
            }
        if changes:
            self._activate_open_tab(target_payload["id"])
            self._apply_msg_data(target_viewer, target_json)
            if target_viewer.handler.entries:
                self._focus_msg_entry(
                    target_viewer,
                    min(
                        len(target_viewer.handler.entries) - 1,
                        next(
                            (
                                index
                                for index, entry in enumerate(
                                    target_viewer.handler.entries
                                )
                                if str(entry.get("uuid", "")).casefold()
                                in selected_source_uuids
                            ),
                            0,
                        ),
                    ),
                )
        return {
            "status": (
                "partial"
                if source_only or incompatible
                else "destination_only_values_preserved"
                if destination_only
                else "completed"
                if changes
                else "no_changes"
            ),
            "cancelled": False,
            "changes_planned": len(changes),
            "changes_applied": len(changes),
            "changes": changes[:500],
            "details_truncated": len(changes) > 500,
            "source_only_value_count": len(source_only),
            "source_only_values": source_only[:500],
            "destination_only_value_count": len(destination_only),
            "destination_only_values_preserved": destination_only[:500],
            "incompatible_value_count": len(incompatible),
            "incompatible_values": incompatible[:500],
            "modified": bool(target_viewer.modified),
            "saved": False,
            **copy_metadata,
        }

    def _load_msg_json(
        self,
        handler: MsgHandler,
        path: str,
    ) -> tuple[Path, dict[str, Any]]:
        target = Path(
            os.path.expandvars(os.path.expanduser(str(path or "")))
        ).resolve()
        if not target.is_file():
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "JSON file does not exist: {path}",
                    ),
                    path=target,
                )
            )
        try:
            with target.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "Could not read MSG JSON {path}: {error}",
                    ),
                    path=target,
                    error=exc,
                )
            ) from exc
        if not isinstance(payload, dict):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG JSON root must be an object.",
                    )
                )
            )
        scratch = MsgHandler()
        scratch.header = copy.deepcopy(handler.header)
        scratch.is_encrypted = handler.is_encrypted
        try:
            scratch.load_json_dict(payload)
            for entry in scratch.entries:
                entry["uuid"] = str(
                    uuid.UUID(str(entry.get("uuid", "")))
                ).lower()
        except (KeyError, TypeError, ValueError) as exc:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG JSON data is invalid: {error}",
                    ),
                    error=exc,
                )
            ) from exc
        if len({entry["uuid"] for entry in scratch.entries}) != len(
            scratch.entries
        ):
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "MSG JSON contains duplicate entry UUIDs.",
                    )
                )
            )
        return target, scratch.to_json_dict()

    def _import_msg_json(self, path: str) -> dict[str, Any]:
        _tab, viewer = self._active_msg()
        target, data = self._load_msg_json(viewer.handler, path)
        changed = data != viewer.handler.to_json_dict()
        if changed:
            self._apply_msg_data(viewer, data)
            if viewer.handler.entries:
                self._focus_msg_entry(viewer, 0)
        self._action_feedback.pulse_widget(viewer.import_btn)
        return {
            "status": "completed" if changed else "no_changes",
            "changes_planned": int(changed),
            "changes_applied": int(changed),
            "imported": str(target),
            "entry_count": len(viewer.handler.entries),
            "language_count": len(viewer.handler.useLanguages),
            "attribute_count": len(viewer.handler.userParamTypes),
        }

    def _export_msg_json(self, path: str) -> dict[str, Any]:
        _tab, viewer = self._active_msg()
        requested = str(path or "").strip()
        if not requested:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "JSON destination path must not be empty.",
                    )
                )
            )
        target = Path(
            os.path.expandvars(os.path.expanduser(requested))
        ).resolve()
        if target.suffix.casefold() != ".json":
            target = target.with_name(target.name + ".json")
        if not target.parent.is_dir():
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "JSON destination folder does not exist: {path}",
                    ),
                    path=target.parent,
                )
            )
        try:
            viewer.handler.export_json(str(target))
        except OSError as exc:
            raise AssistantToolError(
                _tr(
                    QT_TRANSLATE_NOOP(
                        "ReasyAssistantTools",
                        "Could not export MSG JSON {path}: {error}",
                    ),
                    path=target,
                    error=exc,
                )
            ) from exc
        self._action_feedback.pulse_widget(viewer.export_btn)
        return {
            "exported": str(target),
            "entry_count": len(viewer.handler.entries),
        }

    @staticmethod
    def _summarize_export_msg_json(
        arguments: dict[str, Any],
    ) -> tuple[str, str]:
        return (
            _tr(_EXPORT_MSG_JSON_ACTION),
            _tr(_JSON_FILE_DETAIL, path=arguments.get("path", "")),
        )
