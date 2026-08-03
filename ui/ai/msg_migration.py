from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Iterable

from file_handlers.msg.msg_handler import MsgHandler
from ui.ai.file_migration import FileMigrationJob
from ui.ai.tool_registry import AssistantToolError, translate_tool_text as _tr


MSG_COPY_SECTIONS = frozenset({"name", "sound_id", "content", "attributes"})
_MSG_SUFFIX_RE = re.compile(r"\.msg(?:\.\d+)?$", re.IGNORECASE)
_VERSION_SUFFIX_RE = re.compile(r"(\.msg)(\.\d+)?$", re.IGNORECASE)


def default_msg_attribute(param_type: int) -> Any:
    return "" if param_type in (-1, 2) else 0 if param_type == 0 else 0.0


def overlay_msg_data(
    source: dict[str, Any],
    destination: dict[str, Any],
    *,
    entry_indices: Iterable[int] | None = None,
    language_indices: Iterable[int] | None = None,
    attribute_indices: Iterable[int] | None = None,
    sections: Iterable[str] | None = None,
    include_source_only: bool = False,
    detail_limit: int | None = 500,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Overlay compatible MSG values while retaining destination structure."""

    target = copy.deepcopy(destination)
    selected_sections = MSG_COPY_SECTIONS if sections is None else frozenset(sections)
    unknown = selected_sections.difference(MSG_COPY_SECTIONS)
    if unknown:
        raise AssistantToolError(
            _tr("Unknown MSG copy sections: {sections}", sections=sorted(unknown))
        )

    source_entries = list(
        range(len(source["entries"])) if entry_indices is None else entry_indices
    )
    source_languages = (
        list(
            range(len(source["languages"]))
            if language_indices is None
            else language_indices
        )
        if "content" in selected_sections
        else []
    )
    source_attributes = (
        list(
            range(len(source["user_params"]))
            if attribute_indices is None
            else attribute_indices
        )
        if "attributes" in selected_sections
        else []
    )

    changes: list[dict[str, Any]] = []
    source_only: list[str] = []
    source_only_details: list[dict[str, Any]] = []
    incompatible: list[dict[str, Any]] = []
    destination_only: list[str] = []
    destination_only_details: list[dict[str, Any]] = []

    target_language_codes = {
        int(language["code"]): index
        for index, language in enumerate(target["languages"])
    }
    selected_source_codes = {
        int(source["languages"][index]["code"]) for index in source_languages
    }
    if "content" in selected_sections and language_indices is None:
        destination_only.extend(
            f"language[{code}]"
            for code in target_language_codes
            if code not in selected_source_codes
        )
        destination_only_details.extend(
            {
                "path": f"language[{code}]",
                "latest_value": copy.deepcopy(
                    target["languages"][target_language_codes[code]]
                ),
            }
            for code in target_language_codes
            if code not in selected_source_codes
        )

    language_map: dict[int, int] = {}
    for source_index in source_languages:
        source_language = source["languages"][source_index]
        code = int(source_language["code"])
        target_index = target_language_codes.get(code)
        if target_index is None:
            if not include_source_only:
                source_only.append(f"language[{code}]")
                source_only_details.append(
                    {
                        "path": f"language[{code}]",
                        "outdated_mod_value": copy.deepcopy(source_language),
                    }
                )
                continue
            target["languages"].append(copy.deepcopy(source_language))
            target_index = len(target["languages"]) - 1
            target_language_codes[code] = target_index
            for entry in target["entries"]:
                entry["content"].append("")
            changes.append(
                {
                    "path": f"language[{code}]",
                    "kind": "added",
                    "new": copy.deepcopy(source_language),
                }
            )
        language_map[source_index] = target_index

    target_params = {
        str(param.get("name", "")).casefold(): (index, param)
        for index, param in enumerate(target["user_params"])
    }
    selected_param_names = {
        str(source["user_params"][index].get("name", "")).casefold()
        for index in source_attributes
    }
    if "attributes" in selected_sections and attribute_indices is None:
        destination_only.extend(
            f"attribute[{param.get('name', '')}]"
            for key, (_index, param) in target_params.items()
            if key not in selected_param_names
        )
        destination_only_details.extend(
            {
                "path": f"attribute[{param.get('name', '')}]",
                "latest_value": copy.deepcopy(param),
            }
            for key, (_index, param) in target_params.items()
            if key not in selected_param_names
        )

    attribute_map: dict[int, int] = {}
    for source_index in source_attributes:
        source_param = source["user_params"][source_index]
        key = str(source_param.get("name", "")).casefold()
        target_match = target_params.get(key)
        if target_match is None:
            if not include_source_only:
                source_only.append(f"attribute[{source_param.get('name', '')}]")
                source_only_details.append(
                    {
                        "path": f"attribute[{source_param.get('name', '')}]",
                        "outdated_mod_value": copy.deepcopy(source_param),
                    }
                )
                continue
            target["user_params"].append(copy.deepcopy(source_param))
            target_index = len(target["user_params"]) - 1
            target_params[key] = (target_index, target["user_params"][target_index])
            default = default_msg_attribute(int(source_param["type"]))
            for entry in target["entries"]:
                entry["attributes"].append(copy.deepcopy(default))
            changes.append(
                {
                    "path": f"attribute[{source_param.get('name', '')}]",
                    "kind": "added",
                    "new": copy.deepcopy(source_param),
                }
            )
        else:
            target_index, target_param = target_match
            if int(target_param["type"]) != int(source_param["type"]):
                incompatible.append(
                    {
                        "path": f"attribute[{source_param.get('name', '')}]",
                        "source_type": int(source_param["type"]),
                        "destination_type": int(target_param["type"]),
                    }
                )
                continue
        attribute_map[source_index] = target_index

    target_entries = {
        str(entry.get("uuid", "")).casefold(): (index, entry)
        for index, entry in enumerate(target["entries"])
    }
    selected_uuids: set[str] = set()
    for source_index in source_entries:
        source_entry = source["entries"][source_index]
        entry_uuid = str(source_entry.get("uuid", "")).casefold()
        selected_uuids.add(entry_uuid)
        target_match = target_entries.get(entry_uuid)
        entry_added = target_match is None
        if target_match is None:
            if not include_source_only:
                source_only.append(f"entry[{entry_uuid}]")
                source_only_details.append(
                    {
                        "path": f"entry[{entry_uuid}]",
                        "outdated_mod_value": copy.deepcopy(source_entry),
                    }
                )
                continue
            target_entry = {
                "uuid": source_entry["uuid"],
                "name": source_entry.get("name", "")
                if "name" in selected_sections
                else "",
                "SoundID": (
                    int(source_entry.get("SoundID", 0))
                    if "sound_id" in selected_sections
                    else 0
                ),
                "content": ["" for _ in target["languages"]],
                "attributes": [
                    default_msg_attribute(int(param["type"]))
                    for param in target["user_params"]
                ],
            }
            target["entries"].append(target_entry)
            target_entries[entry_uuid] = (len(target["entries"]) - 1, target_entry)
        else:
            _target_index, target_entry = target_match

        if "name" in selected_sections:
            _set_msg_value(
                target_entry,
                "name",
                source_entry.get("name", ""),
                f"entry[{entry_uuid}].name",
                changes,
                record=not entry_added,
            )
        if "sound_id" in selected_sections:
            _set_msg_value(
                target_entry,
                "SoundID",
                int(source_entry.get("SoundID", 0)),
                f"entry[{entry_uuid}].sound_id",
                changes,
                record=not entry_added,
            )
        if "content" in selected_sections:
            for source_language, target_language in language_map.items():
                old = target_entry["content"][target_language]
                new = source_entry["content"][source_language]
                if old != new:
                    target_entry["content"][target_language] = new
                    if not entry_added:
                        code = source["languages"][source_language]["code"]
                        changes.append(
                            {
                                "path": f"entry[{entry_uuid}].content[{code}]",
                                "kind": "changed",
                                "old": old,
                                "new": new,
                            }
                        )
        if "attributes" in selected_sections:
            for source_attr, target_attr in attribute_map.items():
                old = target_entry["attributes"][target_attr]
                new = source_entry["attributes"][source_attr]
                if old != new:
                    target_entry["attributes"][target_attr] = copy.deepcopy(new)
                    if not entry_added:
                        name = source["user_params"][source_attr]["name"]
                        changes.append(
                            {
                                "path": f"entry[{entry_uuid}].attribute[{name}]",
                                "kind": "changed",
                                "old": old,
                                "new": new,
                            }
                        )
        if entry_added:
            changes.append(
                {
                    "path": f"entry[{entry_uuid}]",
                    "kind": "added",
                    "new": copy.deepcopy(target_entry),
                }
            )

    if entry_indices is None:
        destination_only.extend(
            f"entry[{entry_uuid}]"
            for entry_uuid in target_entries
            if entry_uuid not in selected_uuids
        )
        destination_only_details.extend(
            {
                "path": f"entry[{entry_uuid}]",
                "latest_value": copy.deepcopy(entry),
            }
            for entry_uuid, (_index, entry) in target_entries.items()
            if entry_uuid not in selected_uuids
        )

    def detail(items: list[Any]) -> list[Any]:
        return items if detail_limit is None else items[:detail_limit]

    return target, {
        "changes_applied": len(changes),
        "added_element_count": sum(
            1 for item in changes if item.get("kind") == "added"
        ),
        "changes": detail(changes),
        "source_only_value_count": len(source_only),
        "source_only_values": detail(source_only),
        "source_only_details": detail(source_only_details),
        "destination_only_value_count": len(destination_only),
        "destination_only_values_preserved": detail(destination_only),
        "destination_only_details": detail(destination_only_details),
        "incompatible_value_count": len(incompatible),
        "incompatible_values": detail(incompatible),
        "details_truncated": bool(
            detail_limit is not None
            and any(
                len(items) > detail_limit
                for items in (
                    changes,
                    source_only,
                    source_only_details,
                    destination_only,
                    destination_only_details,
                    incompatible,
                )
            )
        ),
    }


def _set_msg_value(
    target: dict[str, Any],
    key: str,
    new: Any,
    path: str,
    changes: list[dict[str, Any]],
    *,
    record: bool,
) -> None:
    old = target.get(key, "" if key == "name" else 0)
    if old == new:
        return
    target[key] = new
    if record:
        changes.append({"path": path, "kind": "changed", "old": old, "new": new})


def _suffix(path: Path) -> tuple[str, str]:
    match = _VERSION_SUFFIX_RE.search(path.name)
    return (
        (
            match.group(1).casefold(),
            (match.group(2) or "").casefold(),
        )
        if match
        else ("", "")
    )


def _canonical_msg(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "languages": [int(item["code"]) for item in data["languages"]],
        "user_params": [
            (str(item.get("name", "")), int(item["type"]))
            for item in data["user_params"]
        ],
        "entries": {
            str(item["uuid"]).casefold(): {
                "name": item.get("name", ""),
                "SoundID": int(item.get("SoundID", 0)),
                "content": item["content"],
                "attributes": item["attributes"],
            }
            for item in data["entries"]
        },
    }


class MsgMigrationStrategy:
    format_name = "msg"

    def __init__(
        self,
        *,
        include_source_only: bool = False,
        detail_limit: int | None = 500,
    ):
        self.include_source_only = include_source_only
        self.detail_limit = detail_limit

    def validate_paths(self, job: FileMigrationJob) -> None:
        for field, path in (
            ("outdated_file", job.outdated_file),
            ("latest_file", job.latest_file),
            ("output_file", job.output_file),
        ):
            if not _MSG_SUFFIX_RE.search(path.name):
                raise AssistantToolError(
                    _tr(
                        "{field} is not an MSG path: {path}",
                        field=field,
                        path=path,
                    )
                )
        if _suffix(job.output_file) != _suffix(job.latest_file):
            raise AssistantToolError(
                _tr("output_file must keep the latest MSG file-version suffix.")
            )

    def migrate(self, job: FileMigrationJob) -> tuple[bytes, dict[str, Any]]:
        source_bytes = job.outdated_file.read_bytes()
        latest_bytes = job.latest_file.read_bytes()
        if not MsgHandler.can_handle(source_bytes):
            raise AssistantToolError(_tr("The outdated input is not an MSG file."))
        if not MsgHandler.can_handle(latest_bytes):
            raise AssistantToolError(_tr("The latest input is not an MSG file."))

        source = MsgHandler()
        latest = MsgHandler()
        source.read(source_bytes)
        latest.read(latest_bytes)
        migrated, report = overlay_msg_data(
            source.to_json_dict(),
            latest.to_json_dict(),
            include_source_only=self.include_source_only,
            detail_limit=self.detail_limit,
        )
        if report["changes_applied"]:
            latest.load_json_dict(migrated)
            output = latest.rebuild()
            verified = MsgHandler()
            verified.read(output)
            if _canonical_msg(verified.to_json_dict()) != _canonical_msg(migrated):
                raise AssistantToolError(
                    _tr("Migrated MSG did not round-trip to the planned values.")
                )
        else:
            output = latest_bytes

        report.update(
            {
                "outdated_version": source.header.get("version"),
                "latest_version": latest.header.get("version"),
                "latest_structure_preserved": True,
                "output_validated": True,
            }
        )
        return output, report
