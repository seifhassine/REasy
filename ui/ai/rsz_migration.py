from __future__ import annotations

import os
import re
from hashlib import sha256
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from file_handlers.rcol.rcol_file import RcolFile
from file_handlers.rsz.rsz_data_types import (
    ArrayData,
    GameObjectRefData,
    GuidData,
    ObjectData,
    RawBytesData,
    ResourceData,
    StructData,
    UserDataData,
)
from file_handlers.rsz.rsz_file import RszFile
from ui.ai.file_migration import FileMigrationJob
from ui.ai.tool_registry import AssistantToolError, translate_tool_text as _tr
from utils.registry_manager import RegistryManager


_INDEX_RE = re.compile(r"\[\d+\]")
_FORMAT_SUFFIX_RE = re.compile(
    r"\.(user|scn|pfb|rcol|wcc)(\.\d+)?$",
    re.IGNORECASE,
)
_DETAIL_LIMIT = 500
_MISSING_DETAIL = object()


def load_type_registry(value: Any, field: str):
    raw = str(value or "").strip()
    path = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    if not raw or not path.is_file():
        raise AssistantToolError(
            _tr(
                "{field} must identify an existing RSZ type registry: {path}",
                field=field,
                path=path,
            )
        )
    registry = RegistryManager.instance().get_registry(str(path))
    if registry is None:
        raise AssistantToolError(
            _tr("Could not load RSZ type registry: {path}", path=path)
        )
    return path, registry


@dataclass
class _LoadedRsz:
    kind: str
    path: Path
    source_bytes: bytes
    rsz: RszFile
    owner: RcolFile | None = None
    file_version: int = 0

    def build(self) -> bytes:
        if self.owner is not None:
            return self.owner.write(self.file_version)
        if self.kind == "headless":
            return self.rsz.build_headless()
        return self.rsz.build()


class _Report:
    def __init__(self, change_selector=None, detail_limit: int | None = _DETAIL_LIMIT):
        self.change_selector = change_selector
        self.detail_limit = detail_limit
        self.changes: list[dict[str, Any]] = []
        self.skipped_changes: list[dict[str, Any]] = []
        self.source_only: list[str] = []
        self.source_only_details: list[dict[str, Any]] = []
        self.destination_only: list[str] = []
        self.destination_only_details: list[dict[str, Any]] = []
        self.incompatible: list[dict[str, Any]] = []
        self.matches: list[dict[str, Any]] = []
        self.change_count = 0
        self.added_change_count = 0
        self.skipped_change_count = 0
        self.source_only_count = 0
        self.destination_only_count = 0
        self.incompatible_count = 0
        self.match_count = 0

    @staticmethod
    def _append(items: list[Any], item: Any, limit: int | None) -> None:
        if limit is None or len(items) < limit:
            items.append(item)

    def changed(self, item: dict[str, Any]) -> None:
        self.change_count += 1
        if item.get("kind") == "added":
            self.added_change_count += 1
        self._append(self.changes, item, self.detail_limit)

    def wants_change(self, item: dict[str, Any]) -> bool:
        if self.change_selector is None or self.change_selector(item):
            return True
        self.skipped_change_count += 1
        self._append(self.skipped_changes, item, self.detail_limit)
        return False

    def source_missing(self, path: str, value: Any = _MISSING_DETAIL) -> None:
        self.source_only_count += 1
        self._append(self.source_only, path, self.detail_limit)
        detail = {"path": path}
        if value is not _MISSING_DETAIL:
            detail["outdated_mod_value"] = value
        self._append(self.source_only_details, detail, self.detail_limit)

    def destination_missing(
        self,
        path: str,
        value: Any = _MISSING_DETAIL,
    ) -> None:
        self.destination_only_count += 1
        self._append(self.destination_only, path, self.detail_limit)
        detail = {"path": path}
        if value is not _MISSING_DETAIL:
            detail["latest_value"] = value
        self._append(
            self.destination_only_details,
            detail,
            self.detail_limit,
        )

    def incompatible_value(self, item: dict[str, Any]) -> None:
        self.incompatible_count += 1
        self._append(self.incompatible, item, self.detail_limit)

    def matched(self, item: dict[str, Any]) -> None:
        self.match_count += 1
        self._append(self.matches, item, _DETAIL_LIMIT)

    def payload(self) -> dict[str, Any]:
        value_details_truncated = bool(
            self.detail_limit is not None
            and any(
                count > self.detail_limit
                for count in (
                    self.change_count,
                    self.skipped_change_count,
                    self.source_only_count,
                    self.destination_only_count,
                    self.incompatible_count,
                )
            )
        )
        instance_matches_truncated = self.match_count > _DETAIL_LIMIT
        return {
            "changes_applied": self.change_count,
            "added_element_count": self.added_change_count,
            "changes": self.changes,
            "skipped_change_count": self.skipped_change_count,
            "skipped_changes": self.skipped_changes,
            "source_only_value_count": self.source_only_count,
            "source_only_values": self.source_only,
            "source_only_details": self.source_only_details,
            "destination_only_value_count": self.destination_only_count,
            "destination_only_values_preserved": self.destination_only,
            "destination_only_details": self.destination_only_details,
            "incompatible_value_count": self.incompatible_count,
            "incompatible_values": self.incompatible,
            "matched_instance_count": self.match_count,
            "instance_matches": self.matches,
            "value_details_truncated": value_details_truncated,
            "instance_matches_truncated": instance_matches_truncated,
            "details_truncated": bool(
                value_details_truncated or instance_matches_truncated
            ),
        }


def _file_version(path: Path) -> int:
    match = re.search(r"\.(\d+)$", path.name)
    return int(match.group(1)) if match else 0


def _version_suffix(path: Path) -> str:
    match = re.search(r"(\.\d+)$", path.name)
    return match.group(1) if match else ""


def _format_suffix(path: Path) -> str:
    match = _FORMAT_SUFFIX_RE.search(path.name)
    return match.group(0).casefold() if match else _version_suffix(path)


def _registry_manages_resources(registry) -> bool:
    metadata = registry.registry.get("metadata", {})
    return bool(
        isinstance(metadata, dict)
        and (metadata.get("complete") or metadata.get("resources_identified"))
    )


def _read_rsz(
    path: Path,
    registry,
    *,
    data: bytes | None = None,
) -> _LoadedRsz:
    data = path.read_bytes() if data is None else data
    magic = data[:4]
    if magic == b"RCOL":
        version = _file_version(path) or 25
        owner = RcolFile()
        owner.type_registry = registry
        if not owner.read(data, file_version=version, file_path=str(path)):
            raise AssistantToolError(
                _tr("Could not parse RCOL file: {path}", path=path)
            )
        if owner.rsz is None:
            raise AssistantToolError(
                _tr("RCOL contains no headless RSZ data: {path}", path=path)
            )
        owner.rsz._registry_validation_enabled = True
        owner.rsz._validate_instance_types_against_registry()
        return _LoadedRsz("rcol", path, data, owner.rsz, owner, version)

    rsz = RszFile()
    rsz.filepath = str(path)
    rsz.type_registry = registry
    rsz.auto_resource_management = _registry_manages_resources(registry)
    if magic == b"RSZ\x00":
        rsz.read_headless(data, validate_type_registry=True)
        return _LoadedRsz("headless", path, data, rsz)
    if magic not in {b"USR\x00", b"SCN\x00", b"PFB\x00"}:
        raise AssistantToolError(
            _tr("Unsupported RSZ container signature in {path}.", path=path)
        )
    rsz.read(data, validate_type_registry=True)
    kind = {b"USR\x00": "user", b"SCN\x00": "scn", b"PFB\x00": "pfb"}[magic]
    return _LoadedRsz(kind, path, data, rsz)


def _type_name(model, segment, instance_id: int, registry) -> str:
    return model._instance_type(segment, instance_id, registry)[1]


def _instance_ids(segment) -> list[int]:
    return sorted(
        instance_id
        for instance_id, fields in segment.instances.items()
        if instance_id and isinstance(fields, dict)
    )


def _walk_references(value, path: str = ""):
    if isinstance(value, (ObjectData, UserDataData)):
        yield path, int(value.value)
        return
    if isinstance(value, (ArrayData, StructData)):
        for index, item in enumerate(value.values):
            yield from _walk_references(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for name, item in value.items():
            child = f"{path}.{name}" if path else str(name)
            yield from _walk_references(item, child)


def _references(fields: dict[str, Any]) -> dict[str, int]:
    return {path: target for path, target in _walk_references(fields) if target >= 0}


def _simple_value(value) -> Any | None:
    if isinstance(value, (GuidData, GameObjectRefData)):
        return str(value.guid_str).casefold()
    if isinstance(value, ResourceData) or value.__class__.__name__ in {
        "StringData",
        "RuntimeTypeData",
    }:
        return str(value.value or "").rstrip("\x00").casefold()
    if hasattr(value, "value") and not isinstance(value, (ObjectData, UserDataData)):
        raw = value.value
        if isinstance(raw, (str, int, float, bool)):
            return raw.casefold() if isinstance(raw, str) else raw
    return None


def _identity(fields: dict[str, Any]) -> tuple[str, Any] | None:
    preferred = ("guid", "uuid", "id", "name", "key", "hash")
    by_name = {
        str(name).casefold(): (str(name), value) for name, value in fields.items()
    }
    for token in preferred:
        for folded, (name, value) in by_name.items():
            if folded == token or folded.endswith(token):
                simple = _simple_value(value)
                if simple not in (None, "", 0):
                    return name.casefold(), simple
    return None


def _reference_candidates(
    model,
    source,
    destination,
    source_registry,
    latest_registry,
    mapping: dict[int, int],
    used: set[int],
):
    for source_parent, destination_parent in list(mapping.items()):
        if not source_parent or not destination_parent:
            continue
        source_fields = source.instances.get(source_parent)
        destination_fields = destination.instances.get(destination_parent)
        if not isinstance(source_fields, dict) or not isinstance(
            destination_fields, dict
        ):
            continue
        source_refs = _references(source_fields)
        destination_refs = _references(destination_fields)
        for path, source_id in source_refs.items():
            destination_id = destination_refs.get(path)
            if destination_id is not None:
                yield source_id, destination_id, "matched_reference_path"

        source_groups: dict[tuple[str, str], list[int]] = defaultdict(list)
        destination_groups: dict[tuple[str, str], list[int]] = defaultdict(list)
        for path, instance_id in source_refs.items():
            if instance_id not in mapping and instance_id in source.instances:
                key = (
                    _INDEX_RE.sub("[]", path),
                    _type_name(model, source, instance_id, source_registry).casefold(),
                )
                source_groups[key].append(instance_id)
        for path, instance_id in destination_refs.items():
            if instance_id not in used and instance_id in destination.instances:
                key = (
                    _INDEX_RE.sub("[]", path),
                    _type_name(
                        model, destination, instance_id, latest_registry
                    ).casefold(),
                )
                destination_groups[key].append(instance_id)
        for key, candidates in source_groups.items():
            targets = destination_groups.get(key, [])
            if len(candidates) == len(targets):
                for source_id, destination_id in zip(candidates, targets):
                    yield (
                        source_id,
                        destination_id,
                        "matched_reference_type_occurrence",
                    )


def _match_segment(
    model,
    source,
    destination,
    source_registry,
    latest_registry,
    report,
    *,
    anchors: Iterable[tuple[int, int, str]] = (),
):
    source_ids = _instance_ids(source)
    destination_ids = _instance_ids(destination)
    mapping: dict[int, int] = {0: 0}
    used: set[int] = {0}
    reasons: dict[int, str] = {}

    def add(source_id: int, destination_id: int, reason: str) -> bool:
        if source_id in mapping or destination_id in used:
            return False
        if (
            _type_name(model, source, source_id, source_registry).casefold()
            != _type_name(
                model, destination, destination_id, latest_registry
            ).casefold()
        ):
            return False
        mapping[source_id] = destination_id
        used.add(destination_id)
        reasons[source_id] = reason
        return True

    for source_id, destination_id, reason in anchors:
        add(source_id, destination_id, reason)

    for source_id, destination_id in zip(source.object_table, destination.object_table):
        add(int(source_id), int(destination_id), "object_table_position")

    source_roots: dict[str, list[int]] = defaultdict(list)
    destination_roots: dict[str, list[int]] = defaultdict(list)
    for instance_id in dict.fromkeys(int(value) for value in source.object_table):
        if instance_id not in mapping and instance_id in source.instances:
            source_roots[
                _type_name(model, source, instance_id, source_registry).casefold()
            ].append(instance_id)
    for instance_id in dict.fromkeys(int(value) for value in destination.object_table):
        if instance_id not in used and instance_id in destination.instances:
            destination_roots[
                _type_name(model, destination, instance_id, latest_registry).casefold()
            ].append(instance_id)
    for type_name, candidates in source_roots.items():
        targets = destination_roots.get(type_name, [])
        if len(candidates) == len(targets):
            for source_id, destination_id in zip(candidates, targets):
                add(source_id, destination_id, "object_table_type_occurrence")

    def propagate_references() -> None:
        while True:
            progress = False
            for source_id, destination_id, reason in _reference_candidates(
                model,
                source,
                destination,
                source_registry,
                latest_registry,
                mapping,
                used,
            ):
                progress |= add(source_id, destination_id, reason)
            if not progress:
                return

    propagate_references()

    source_identity: dict[tuple[str, tuple[str, Any]], list[int]] = defaultdict(list)
    destination_identity: dict[tuple[str, tuple[str, Any]], list[int]] = defaultdict(
        list
    )
    for instance_id in source_ids:
        if instance_id not in mapping:
            identity = _identity(source.instances[instance_id])
            if identity is not None:
                key = (
                    _type_name(model, source, instance_id, source_registry).casefold(),
                    identity,
                )
                source_identity[key].append(instance_id)
    for instance_id in destination_ids:
        if instance_id not in used:
            identity = _identity(destination.instances[instance_id])
            if identity is not None:
                key = (
                    _type_name(
                        model, destination, instance_id, latest_registry
                    ).casefold(),
                    identity,
                )
                destination_identity[key].append(instance_id)
    for key, candidates in source_identity.items():
        targets = destination_identity.get(key, [])
        if len(candidates) == len(targets) == 1:
            add(candidates[0], targets[0], "type_and_identity")

    source_types: dict[str, list[int]] = defaultdict(list)
    destination_types: dict[str, list[int]] = defaultdict(list)
    for instance_id in source_ids:
        if instance_id not in mapping:
            source_types[
                _type_name(model, source, instance_id, source_registry).casefold()
            ].append(instance_id)
    for instance_id in destination_ids:
        if instance_id not in used:
            destination_types[
                _type_name(model, destination, instance_id, latest_registry).casefold()
            ].append(instance_id)
    for type_name, candidates in source_types.items():
        targets = destination_types.get(type_name, [])
        if len(candidates) == len(targets) == 1:
            add(candidates[0], targets[0], "unique_type")

    propagate_references()

    for source_id, destination_id in mapping.items():
        if not source_id:
            continue
        report.matched(
            {
                "source_segment": source.id,
                "source_instance_id": source_id,
                "destination_segment": destination.id,
                "destination_instance_id": destination_id,
                "type_name": _type_name(model, source, source_id, source_registry),
                "reason": reasons[source_id],
            }
        )
    return mapping


class _UnmappedReference(ValueError):
    pass


def _portable_value(model, source_value, source_segment, mapping, path: str):
    if isinstance(source_value, ObjectData):
        source_id = int(source_value.value)
        if source_id not in mapping:
            raise _UnmappedReference(
                f"{path} references unmatched source instance {source_id}"
            )
        return mapping[source_id]
    if isinstance(source_value, UserDataData):
        source_id = int(source_value.value)
        if source_id not in mapping:
            raise _UnmappedReference(
                f"{path} references unmatched source userdata {source_id}"
            )
        return {"instance_id": mapping[source_id]}
    if isinstance(source_value, (ArrayData, StructData)):
        return [
            _portable_value(model, value, source_segment, mapping, f"{path}[{index}]")
            for index, value in enumerate(source_value.values)
        ]
    if isinstance(source_value, dict):
        return {
            name: _portable_value(
                model, value, source_segment, mapping, f"{path}.{name}"
            )
            for name, value in source_value.items()
        }
    if isinstance(source_value, (GuidData, GameObjectRefData)):
        return str(source_value.guid_str)
    if isinstance(source_value, RawBytesData):
        return bytes(source_value.raw_bytes).hex()
    if isinstance(source_value, ResourceData) or source_value.__class__.__name__ in {
        "StringData",
        "RuntimeTypeData",
    }:
        return str(source_value.value or "")
    return model._json_value(source_value, source_segment, None, 100000)


def _mark_embedded_modified(segment) -> None:
    context = segment.context
    while context is not None:
        if hasattr(context, "modified"):
            context.modified = True
        context = getattr(context, "parent_userdata_rui", None)


def _overlay_userdata_strings(
    source_segment,
    destination_segment,
    mapping: dict[int, int],
    destination_rsz: RszFile,
    report: _Report,
) -> None:
    destination_infos = {
        int(getattr(info, "instance_id", 0)): info
        for info in destination_segment.userdata_infos
    }
    for source_id, source_text in source_segment.userdata_strings.items():
        destination_id = mapping.get(source_id)
        info = (
            destination_infos.get(destination_id)
            if destination_id is not None
            else None
        )
        if info is None:
            continue
        old = destination_segment.userdata_strings.get(destination_id, "")
        if old == source_text:
            continue
        change = {
            "path": f"{destination_segment.id}:userdata[{destination_id}].string",
            "kind": "changed",
            "old": old,
            "new": source_text,
        }
        if not report.wants_change(change):
            continue
        if destination_segment.context is None:
            destination_rsz.set_rsz_userdata_string(info, source_text)
        elif hasattr(info, "value"):
            info.value = source_text
        elif hasattr(info, "name"):
            info.name = source_text
        else:
            report.incompatible_value(
                {
                    "path": (
                        f"{destination_segment.id}:userdata[{destination_id}].string"
                    ),
                    "reason": "userdata_string_not_editable",
                }
            )
            continue
        destination_segment.userdata_strings[destination_id] = source_text
        report.changed(change)
        _mark_embedded_modified(destination_segment)


def _overlay_value(
    model,
    source_value,
    destination_value,
    source_segment,
    destination_segment,
    mapping,
    source_registry,
    latest_registry,
    path: str,
    report: _Report,
    include_source_only: bool,
):
    source_collection = isinstance(source_value, (ArrayData, StructData))
    destination_collection = isinstance(destination_value, (ArrayData, StructData))
    if source_collection or destination_collection:
        if not (source_collection and destination_collection):
            report.incompatible_value(
                {
                    "path": path,
                    "reason": "collection_storage_mismatch",
                    "source_storage": source_value.__class__.__name__,
                    "destination_storage": destination_value.__class__.__name__,
                }
            )
            return destination_value
        if isinstance(source_value, StructData) != isinstance(
            destination_value, StructData
        ):
            report.incompatible_value(
                {
                    "path": path,
                    "reason": "collection_kind_mismatch",
                    "source_storage": source_value.__class__.__name__,
                    "destination_storage": destination_value.__class__.__name__,
                }
            )
            return destination_value
        if (
            isinstance(source_value, StructData)
            and isinstance(destination_value, StructData)
            and str(source_value.orig_type or "").casefold()
            != str(destination_value.orig_type or "").casefold()
        ):
            report.incompatible_value(
                {
                    "path": path,
                    "reason": "struct_type_mismatch",
                    "source_type": source_value.orig_type,
                    "destination_type": destination_value.orig_type,
                }
            )
            return destination_value

        destination_count = len(destination_value.values)
        overlap = min(len(source_value.values), destination_count)
        for index in range(overlap):
            destination_value.values[index] = _overlay_value(
                model,
                source_value.values[index],
                destination_value.values[index],
                source_segment,
                destination_segment,
                mapping,
                source_registry,
                latest_registry,
                f"{path}[{index}]",
                report,
                include_source_only,
            )
        for index in range(overlap, len(source_value.values)):
            item_path = f"{path}[{index}]"
            if not include_source_only:
                report.source_missing(
                    item_path,
                    model._json_value(
                        source_value.values[index],
                        source_segment,
                        source_registry,
                        64,
                    ),
                )
                continue
            try:
                value = _portable_value(
                    model,
                    source_value.values[index],
                    source_segment,
                    mapping,
                    item_path,
                )
                change = {
                    "path": item_path,
                    "kind": "added",
                    "new": value,
                }
                if not report.wants_change(change):
                    continue
                element = model._new_collection_element(
                    destination_value,
                    value,
                    destination_segment,
                    latest_registry,
                    item_path,
                    allow_references=True,
                )
                destination_value.values.append(element)
                report.changed(change)
                _mark_embedded_modified(destination_segment)
            except Exception as exc:
                report.incompatible_value(
                    {"path": item_path, "reason": "could_not_add", "error": str(exc)}
                )
        for index in range(overlap, destination_count):
            report.destination_missing(
                f"{path}[{index}]",
                model._json_value(
                    destination_value.values[index],
                    destination_segment,
                    latest_registry,
                    64,
                ),
            )
        return destination_value

    if isinstance(source_value, dict) or isinstance(destination_value, dict):
        if not isinstance(source_value, dict) or not isinstance(
            destination_value, dict
        ):
            report.incompatible_value(
                {
                    "path": path,
                    "reason": "struct_storage_mismatch",
                    "source_storage": source_value.__class__.__name__,
                    "destination_storage": destination_value.__class__.__name__,
                }
            )
            return destination_value
        for name, value in source_value.items():
            child = f"{path}.{name}"
            if name not in destination_value:
                report.source_missing(
                    child,
                    model._json_value(
                        value,
                        source_segment,
                        source_registry,
                        64,
                    ),
                )
                continue
            destination_value[name] = _overlay_value(
                model,
                value,
                destination_value[name],
                source_segment,
                destination_segment,
                mapping,
                source_registry,
                latest_registry,
                child,
                report,
                include_source_only,
            )
        for name in destination_value.keys() - source_value.keys():
            report.destination_missing(
                f"{path}.{name}",
                model._json_value(
                    destination_value[name],
                    destination_segment,
                    latest_registry,
                    64,
                ),
            )
        return destination_value

    old = model._json_value(destination_value, destination_segment, latest_registry, 64)
    if (
        not isinstance(source_value, (ObjectData, UserDataData))
        and source_value.__class__ is destination_value.__class__
        and model._json_value(source_value, source_segment, source_registry, 64) == old
    ):
        return destination_value
    try:
        portable = _portable_value(model, source_value, source_segment, mapping, path)
        replacement = model._coerce_value(
            destination_value,
            portable,
            destination_segment,
            latest_registry,
            path,
            allow_references=True,
        )
        new = model._json_value(replacement, destination_segment, latest_registry, 64)
    except Exception as exc:
        report.incompatible_value(
            {
                "path": path,
                "reason": "value_incompatible",
                "source_storage": source_value.__class__.__name__,
                "destination_storage": destination_value.__class__.__name__,
                "error": str(exc),
            }
        )
        return destination_value
    if old != new:
        change = {"path": path, "kind": "changed", "old": old, "new": new}
        if not report.wants_change(change):
            return destination_value
        report.changed(change)
        _mark_embedded_modified(destination_segment)
        return replacement
    return destination_value


def overlay_rsz_values(
    source: RszFile,
    destination: RszFile,
    source_registry,
    latest_registry,
    *,
    include_source_only: bool = False,
    change_selector=None,
    detail_limit: int | None = _DETAIL_LIMIT,
) -> dict[str, Any]:
    # Imported lazily to keep the reusable RSZ data model independent of the
    # assistant's migration tool registration.
    from ui.ai import rsz_tools as model

    report = _Report(change_selector, detail_limit)
    source_segments = model._segments(source)
    destination_segments = model._segments(destination)
    destination_by_id = {segment.id: segment for segment in destination_segments}
    segment_pairs: dict[str, tuple[Any, dict[int, int]]] = {}
    used_destination_segments: set[str] = set()

    source_main = source_segments[0]
    destination_main = destination_segments[0]
    source_gameobjects = model._gameobject_targets(source)
    destination_gameobjects = model._gameobject_targets(destination)
    main_anchors = [
        (
            int(source_gameobjects[guid]["target_instance_id"]),
            int(destination_gameobjects[guid]["target_instance_id"]),
            "gameobject_guid",
        )
        for guid in sorted(source_gameobjects.keys() & destination_gameobjects.keys())
    ]
    main_mapping = _match_segment(
        model,
        source_main,
        destination_main,
        source_registry,
        latest_registry,
        report,
        anchors=main_anchors,
    )
    segment_pairs[source_main.id] = (destination_main, main_mapping)
    used_destination_segments.add(destination_main.id)

    for source_segment in source_segments[1:]:
        parent_pair = segment_pairs.get(source_segment.parent or "")
        if parent_pair is None:
            report.source_missing(f"segment[{source_segment.id}]")
            continue
        destination_parent, parent_mapping = parent_pair
        destination_userdata = parent_mapping.get(
            source_segment.source_userdata_instance
        )
        candidates = [
            segment
            for segment in destination_segments
            if segment.id not in used_destination_segments
            and segment.parent == destination_parent.id
            and segment.source_userdata_instance == destination_userdata
        ]
        if not candidates:
            suffix = source_segment.id.rsplit("/", 1)[-1]
            candidate = destination_by_id.get(f"{destination_parent.id}/{suffix}")
            candidates = (
                [candidate]
                if candidate is not None
                and candidate.id not in used_destination_segments
                else []
            )
        if len(candidates) != 1:
            report.source_missing(f"segment[{source_segment.id}]")
            continue
        destination_segment = candidates[0]
        mapping = _match_segment(
            model,
            source_segment,
            destination_segment,
            source_registry,
            latest_registry,
            report,
        )
        segment_pairs[source_segment.id] = (destination_segment, mapping)
        used_destination_segments.add(destination_segment.id)

    for destination_segment in destination_segments:
        if destination_segment.id not in used_destination_segments:
            report.destination_missing(f"segment[{destination_segment.id}]")

    for source_segment in source_segments:
        pair = segment_pairs.get(source_segment.id)
        if pair is not None:
            destination_segment, mapping = pair
            _overlay_userdata_strings(
                source_segment,
                destination_segment,
                mapping,
                destination,
                report,
            )

    for source_segment in source_segments:
        pair = segment_pairs.get(source_segment.id)
        if pair is None:
            continue
        destination_segment, mapping = pair
        matched_destination_ids = set(mapping.values())
        for source_id in _instance_ids(source_segment):
            destination_id = mapping.get(source_id)
            type_name = _type_name(model, source_segment, source_id, source_registry)
            if destination_id is None:
                report.source_missing(
                    f"{source_segment.id}:instance[{source_id}]<{type_name}>"
                )
                continue
            source_fields = source_segment.instances[source_id]
            destination_fields = destination_segment.instances[destination_id]
            instance_path = (
                f"{destination_segment.id}:instance[{destination_id}]<{type_name}>"
            )
            for name, source_value in source_fields.items():
                path = f"{instance_path}.{name}"
                if name not in destination_fields:
                    report.source_missing(
                        path,
                        model._json_value(
                            source_value,
                            source_segment,
                            source_registry,
                            64,
                        ),
                    )
                    continue
                destination_fields[name] = _overlay_value(
                    model,
                    source_value,
                    destination_fields[name],
                    source_segment,
                    destination_segment,
                    mapping,
                    source_registry,
                    latest_registry,
                    path,
                    report,
                    include_source_only,
                )
            for name in destination_fields.keys() - source_fields.keys():
                report.destination_missing(
                    f"{instance_path}.{name}",
                    model._json_value(
                        destination_fields[name],
                        destination_segment,
                        latest_registry,
                        64,
                    ),
                )
        for destination_id in _instance_ids(destination_segment):
            if destination_id not in matched_destination_ids:
                type_name = _type_name(
                    model,
                    destination_segment,
                    destination_id,
                    latest_registry,
                )
                report.destination_missing(
                    f"{destination_segment.id}:instance[{destination_id}]<{type_name}>"
                )

    payload = report.payload()
    payload.update(
        {
            "source_segment_count": len(source_segments),
            "destination_segment_count": len(destination_segments),
            "matched_segment_count": len(segment_pairs),
            "latest_structure_preserved": True,
            "reference_ids_remapped": True,
        }
    )
    return payload


def _structure_signature(rsz: RszFile, registry) -> tuple[Any, ...]:
    from ui.ai import rsz_tools as model

    return tuple(
        (
            segment.id,
            tuple(int(value) for value in segment.object_table),
            tuple(
                _type_name(model, segment, index, registry)
                for index in range(len(segment.instance_infos))
            ),
        )
        for segment in model._segments(rsz)
    )


class RszMigrationStrategy:
    format_name = "rsz"

    def __init__(
        self,
        outdated_registry_path: Path,
        outdated_registry,
        latest_registry_path: Path,
        latest_registry,
        *,
        include_source_only: bool = False,
        approved_change_paths: dict[str, frozenset[str]] | None = None,
        expected_input_hashes: dict[str, tuple[str, str]] | None = None,
        detail_limit: int | None = _DETAIL_LIMIT,
    ):
        self.outdated_registry_path = outdated_registry_path
        self.outdated_registry = outdated_registry
        self.latest_registry_path = latest_registry_path
        self.latest_registry = latest_registry
        self.include_source_only = include_source_only
        self.approved_change_paths = approved_change_paths
        self.expected_input_hashes = expected_input_hashes
        self.detail_limit = detail_limit

    def validate_paths(self, job: FileMigrationJob) -> None:
        if _format_suffix(job.output_file) != _format_suffix(job.latest_file):
            raise AssistantToolError(
                _tr("output_file must keep the latest RSZ format/version suffix.")
            )

    def migrate(self, job: FileMigrationJob) -> tuple[bytes, dict[str, Any]]:
        if (
            self.expected_input_hashes is not None
            and job.label not in self.expected_input_hashes
        ):
            raise AssistantToolError(
                _tr(
                    "This RSZ file was not successfully analyzed: {file}",
                    file=job.label,
                )
            )
        expected = (
            self.expected_input_hashes.get(job.label)
            if self.expected_input_hashes is not None
            else None
        )
        if expected is not None:
            actual = (
                sha256(job.outdated_file.read_bytes()).hexdigest(),
                sha256(job.latest_file.read_bytes()).hexdigest(),
            )
            if actual != expected:
                raise AssistantToolError(
                    _tr(
                        "The outdated mod file or latest PAK file changed after AI analysis. Analyze this update again before writing files."
                    )
                )
        outdated = _read_rsz(job.outdated_file, self.outdated_registry)
        latest = _read_rsz(job.latest_file, self.latest_registry)
        if outdated.kind != latest.kind:
            raise AssistantToolError(
                _tr(
                    "RSZ container mismatch: outdated is {old}, latest is {new}.",
                    old=outdated.kind,
                    new=latest.kind,
                )
            )
        if (
            self.approved_change_paths is not None
            and job.label not in self.approved_change_paths
        ):
            raise AssistantToolError(
                _tr(
                    "This RSZ file has no AI-reviewed change set: {file}",
                    file=job.label,
                )
            )
        approved = (
            self.approved_change_paths[job.label]
            if self.approved_change_paths is not None
            else None
        )
        report = overlay_rsz_values(
            outdated.rsz,
            latest.rsz,
            self.outdated_registry,
            self.latest_registry,
            include_source_only=self.include_source_only,
            change_selector=(
                None
                if approved is None
                else lambda change: str(change.get("path") or "") in approved
            ),
            detail_limit=self.detail_limit,
        )
        output = latest.build() if report["changes_applied"] else latest.source_bytes
        verified = _read_rsz(
            latest.path,
            self.latest_registry,
            data=output,
        )
        if verified.kind != latest.kind:
            raise AssistantToolError(
                _tr("Migrated RSZ validation changed container type.")
            )
        if _structure_signature(
            verified.rsz,
            self.latest_registry,
        ) != _structure_signature(latest.rsz, self.latest_registry):
            raise AssistantToolError(
                _tr("Migrated RSZ validation changed the latest instance structure.")
            )
        report.update(
            {
                "container": latest.kind,
                "outdated_registry": str(self.outdated_registry_path),
                "latest_registry": str(self.latest_registry_path),
                "registries_applied_independently": True,
                "output_validated": True,
            }
        )
        return output, report
