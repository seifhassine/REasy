"""Generated names and game-side context shared by RE Engine sound profiles."""

from __future__ import annotations

import gzip
import json
from bisect import bisect_left
from functools import lru_cache
import re

from utils.app_paths import resource_path
from utils.resource_file_utils import resource_context_for_handler

from .runtime_sound_index import request_runtime_sound_index
from .sound_metadata import MessageSoundReference, SoundMetadata
from .sound_resources import resource_key
from .wwise_schema import BNK_PLUGIN_NAMES, BNK_STANDARD_CUE_NAMES


STANDARD_CUE_FALLBACKS = {
    "cue": {
        object_id: (name,)
        for object_id, name in BNK_STANDARD_CUE_NAMES.items()
        if object_id
    },
}


def bank_key(path: str) -> str:
    """Return the language-independent Wwise bank basename used by the index."""

    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()
    markers = [
        name.find(marker)
        for marker in (".sbnk", ".spck", ".bnk", ".pck")
        if marker in name
    ]
    return name[: min(markers)] if markers else name


def _strings(values) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


def _contains_sorted(values, target: int) -> bool:
    index = bisect_left(values, target)
    return index < len(values) and values[index] == target


def _usage_label(binding: str) -> str:
    """Turn an exact code field binding into an honest, readable fallback."""

    field = str(binding).rsplit(".", 1)[-1].lstrip("_")
    field = re.sub(r"Trigger(?:ID|List)?$", "", field, flags=re.IGNORECASE)
    field = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", field).replace("_", " ")
    label = " ".join(field.split())
    return f"Usage: {label}" if label and label.casefold() != "id" else ""


@lru_cache(maxsize=None)
def _load_index(index_resource: str) -> dict:
    try:
        path = resource_path(index_resource)
        opener = gzip.open if path.suffix.casefold() == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


class IndexedSoundMetadata(SoundMetadata):
    """Read-only generated metadata plus a live overlay for the opened WEL."""

    index_resource = ""
    game_name = "RE Engine game"
    fallbacks: dict[str, dict[int, tuple[str, ...]]] = {}

    def __init__(self, source_path: str = ""):
        self.source_path = str(source_path or "")
        self.bank = bank_key(self.source_path)
        self.data = _load_index(self.index_resource) if self.index_resource else {}
        self._live_events: dict[int, tuple[str, ...]] = {}
        self._reverse: dict[str, dict[str, tuple[int, ...]]] = {}
        self._package_banks: dict[tuple[str, int], tuple[str, ...]] | None = None
        self._prefetch_event_banks: dict[tuple[str, int], tuple[str, ...]] | None = None
        self._source_groups: dict[int, tuple[dict, ...]] | None = None
        self._message_sources: dict[str, tuple[int, ...]] = {}
        self._message_references: dict[
            str, tuple[MessageSoundReference, ...]
        ] = {}
        self._message_source_kinds: set[bool] = set()
        self._message_segments: dict[
            str, tuple[MessageSoundReference, ...]
        ] | None = None
        self._trigger_references: dict[
            int, tuple[MessageSoundReference, ...]
        ] = {}
        self._runtime_handle = None
        self.live_wel_path = ""

    @classmethod
    def for_handler(cls, handler, profile=None) -> "IndexedSoundMetadata":
        metadata = cls(getattr(handler, "filepath", "") or getattr(handler, "filename", ""))
        metadata.attach_runtime_context(handler, profile)
        metadata._load_live_wel(handler)
        return metadata

    def attach_runtime_context(self, handler, profile) -> None:
        context = resource_context_for_handler(handler)
        reader = getattr(context, "pak_cached_reader", None) if context else None
        self._runtime_handle = (
            request_runtime_sound_index(reader, profile)
            if reader is not None and profile is not None else None
        )

    def attach_runtime_handle(self, handle) -> None:
        self._runtime_handle = handle

    def prepare_operational_index(self, *, wait: bool = False) -> bool:
        return bool(self._runtime_handle and self._runtime_handle.get(wait=wait))

    def _runtime_index(self):
        return self._runtime_handle.get() if self._runtime_handle else None

    def _name_table(self, category: str) -> dict:
        value = self.data.get("names", {}).get(category, {})
        return value if isinstance(value, dict) else {}

    def names(self, category: str, object_id: int) -> tuple[str, ...]:
        if category == "event" and int(object_id) in self._live_events:
            return self._live_events[int(object_id)]
        values = self._name_table(category).get(str(int(object_id) & 0xFFFFFFFF), ())
        names = _strings(values)
        if names:
            return names
        if category == "plugin":
            name = BNK_PLUGIN_NAMES.get(int(object_id) & 0xFFFFFFFF)
            if name:
                return (name,)
        return self.fallbacks.get(category, {}).get(int(object_id) & 0xFFFFFFFF, ())

    def event_record(self, event_id: int) -> dict:
        banks = self.data.get("bank_events", {})
        record = banks.get(self.bank, {}).get(str(int(event_id) & 0xFFFFFFFF), {})
        return record if isinstance(record, dict) else {}

    def bank_record(self) -> dict:
        record = self.data.get("banks", {}).get(self.bank, {})
        return record if isinstance(record, dict) else {}

    def source_lines(self) -> tuple[str, ...]:
        record = self.bank_record()
        values = []
        if record.get("wel"):
            values.append(f"Event list: {record['wel']}")
        values.extend(f"Game binding: {path}" for path in record.get("containers", ()))
        values.extend(f"Prefab binding: {path}" for path in record.get("prefabs", ()))
        values.extend(f"Scene binding: {path}" for path in record.get("scenes", ()))
        return _strings(values)

    def event_names(self, event_id: int) -> tuple[str, ...]:
        live = self._live_events.get(int(event_id))
        if live:
            return live
        record = self.event_record(event_id)
        names = _strings(record.get("names", ()))
        if names:
            return names
        names = self.names("event", event_id)
        if names:
            return names
        return _strings(
            label
            for binding in self.bindings("event", event_id)
            if (label := _usage_label(binding))
        )[:4]

    def event_contexts(self, event_id: int) -> tuple[dict, ...]:
        values = self.event_record(event_id).get("contexts", ())
        return tuple(value for value in values if isinstance(value, dict))

    def event_context_lines(self, event_id: int) -> tuple[str, ...]:
        """Format shipped scene/user provenance without implying files must exist."""

        lines = []
        for context in self.event_contexts(event_id):
            relation = context.get("match", "scene")
            location = (
                context.get("scene") or context.get("prefab")
                or context.get("user") or "unknown resource"
            )
            line = f"{relation}: {location}"
            if context.get("game_object"):
                line += f" · GameObject {context['game_object']}"
            if context.get("component"):
                line += f" · {context['component']}"
            trigger = str(context.get("trigger") or "").strip()
            trigger_id = context.get("trigger_id")
            if trigger:
                line += f" · Trigger {trigger}"
            elif isinstance(trigger_id, int):
                aliases = self.names("trigger", trigger_id)
                line += (
                    f" · Trigger {' / '.join(aliases)} ({trigger_id})"
                    if aliases else f" · Trigger ID {trigger_id}"
                )
            if isinstance(context.get("message_id"), int):
                line += f" · Message ID {context['message_id']}"
            if context.get("joint"):
                line += f" · Joint {context['joint']}"
            if context.get("state_changes"):
                line += " · " + ", ".join(context["state_changes"])
            if context.get("tag_transition"):
                line += f" · Tags {context['tag_transition']}"
            origins = context.get("embedded_user", ())
            if origins:
                line += " · embedded user record " + ", ".join(origins)
            lines.append(line)
        return _strings(lines)

    def source_contexts(self, source_id: int) -> tuple[dict, ...]:
        if self._source_groups is None:
            reverse: dict[int, list[dict]] = {}
            for group in self.data.get("media_groups", ()):
                if not isinstance(group, dict):
                    continue
                for value in group.get("source_ids", ()):
                    reverse.setdefault(int(value) & 0xFFFFFFFF, []).append(group)
            self._source_groups = {
                value: tuple(groups) for value, groups in reverse.items()
            }
        return self._source_groups.get(int(source_id) & 0xFFFFFFFF, ())

    def message_source_ids(self, message_id: object) -> tuple[int, ...]:
        """Return all media explicitly linked to one game message identifier."""

        key = str(message_id or "").strip().strip("{}").casefold()
        guid_like = "-" in key
        if not key or (not guid_like and not key.isdecimal()):
            return ()
        if guid_like not in self._message_source_kinds:
            reverse: dict[str, list[MessageSoundReference]] = {}
            for group in self.data.get("media_groups", ()):
                if not isinstance(group, dict):
                    continue
                group_key = (
                    str(group.get("message_id", ""))
                    .strip()
                    .strip("{}")
                    .casefold()
                )
                if not group_key or ("-" in group_key) != guid_like:
                    continue
                banks = tuple(bank_key(value) for value in _strings(
                    group.get("bank_families", ())
                ))
                reverse.setdefault(group_key, []).extend(
                    MessageSoundReference(
                        int(value) & 0xFFFFFFFF, banks=banks
                    )
                    for value in group.get("source_ids", ())
                    if isinstance(value, int) and value
                )
            self._message_references.update({
                group_key: tuple(dict.fromkeys(references))
                for group_key, references in reverse.items()
            })
            self._message_sources.update({
                group_key: tuple(dict.fromkeys(
                    reference.source_id for reference in references
                ))
                for group_key, references in self._message_references.items()
                if ("-" in group_key) == guid_like
            })
            self._message_source_kinds.add(guid_like)
        return self._message_sources.get(key, ())

    def message_sound_references(
        self, message_id: object
    ) -> tuple[MessageSoundReference, ...]:
        """Return exact standalone media and timeline-clipped stream references."""

        key = str(message_id or "").strip().strip("{}").casefold()
        if not key:
            return ()
        if self._message_segments is None:
            reverse: dict[str, list[MessageSoundReference]] = {}
            for group in self.data.get("message_segments", ()):
                if not isinstance(group, dict):
                    continue
                group_key = (
                    str(group.get("message_id", ""))
                    .strip()
                    .strip("{}")
                    .casefold()
                )
                start_ms = int(group.get("start_ms", 0))
                end_ms = int(group.get("end_ms", 0))
                full_source = start_ms == end_ms == 0
                if not group_key or (
                    not full_source and (start_ms < 0 or end_ms <= start_ms)
                ):
                    continue
                reverse.setdefault(group_key, []).extend(
                    MessageSoundReference(
                        int(source_id) & 0xFFFFFFFF,
                        start_ms,
                        end_ms,
                        tuple(bank_key(value) for value in _strings(
                            group.get("banks", ())
                        )),
                    )
                    for source_id in group.get("source_ids", ())
                    if isinstance(source_id, int) and source_id
                )
            self._message_segments = {
                group_key: tuple(dict.fromkeys(references))
                for group_key, references in reverse.items()
            }

        segments = self._message_segments.get(key, ())
        segmented_sources = {value.source_id for value in segments}
        self.message_source_ids(message_id)
        standalone = tuple(
            reference
            for reference in self._message_references.get(key, ())
            if reference.source_id not in segmented_sources
        )
        return tuple(dict.fromkeys((*segments, *standalone)))

    def sources_for_triggers(
        self, trigger_ids
    ) -> dict[int, tuple[int, ...]]:
        return {
            trigger_id: tuple(dict.fromkeys(
                reference.source_id for reference in references
            ))
            for trigger_id, references in self.sound_references_for_triggers(
                trigger_ids
            ).items()
        }

    def sound_references_for_triggers(
        self, trigger_ids
    ) -> dict[int, tuple[MessageSoundReference, ...]]:
        """Resolve trigger media while retaining its exact event-bank family."""

        requested = {
            int(value) & 0xFFFFFFFF for value in trigger_ids
            if isinstance(value, int) and value
        }
        missing = requested - self._trigger_references.keys()
        if missing:
            pair_triggers: dict[tuple[str, int], set[int]] = {}
            for bank, events in self.data.get("bank_events", {}).items():
                if not isinstance(events, dict):
                    continue
                for raw_event, record in events.items():
                    if (
                        not str(raw_event).isdecimal()
                        or not isinstance(record, dict)
                    ):
                        continue
                    matches = missing.intersection(record.get("trigger_ids", ()))
                    if matches:
                        pair_triggers[(bank, int(raw_event))] = matches

            resolved = {trigger_id: set() for trigger_id in missing}
            records = self.data.get("source_event_records", ())
            if pair_triggers:
                for raw_source, values in self.data.get(
                    "source_events", {}
                ).items():
                    source_id = int(raw_source) & 0xFFFFFFFF
                    for value in values:
                        if isinstance(value, int) and records:
                            if not 0 <= value < len(records):
                                continue
                            record = records[value]
                            if not isinstance(record, list) or len(record) < 2:
                                continue
                            pair = str(record[0]), int(record[1])
                        elif isinstance(value, dict):
                            pair = (
                                str(value.get("bank", "")),
                                int(value.get("event", 0)),
                            )
                        else:
                            continue
                        for trigger_id in pair_triggers.get(pair, ()):
                            resolved[trigger_id].add(
                                MessageSoundReference(
                                    source_id, banks=(bank_key(pair[0]),)
                                )
                            )
            self._trigger_references.update({
                trigger_id: tuple(sorted(
                    references,
                    key=lambda reference: (
                        reference.source_id, reference.banks
                    ),
                ))
                for trigger_id, references in resolved.items()
            })
        return {
            trigger_id: self._trigger_references[trigger_id]
            for trigger_id in requested
            if self._trigger_references.get(trigger_id)
        }

    def preview_media_paths(self, source_id: int) -> tuple[str, ...]:
        """Return exact installed or shipped container candidates for preview."""

        source_id = int(source_id) & 0xFFFFFFFF
        return self.preview_media_paths_for_sources((source_id,)).get(source_id, ())

    def preview_media_paths_for_sources(
        self, source_ids
    ) -> dict[int, tuple[str, ...]]:
        """Resolve many Source IDs with one pass over compact package links."""

        wanted = {int(value) & 0xFFFFFFFF for value in source_ids}
        runtime = self._runtime_index()
        paths = {
            source_id: [
                *(runtime.preview_media_paths(source_id) if runtime else ()),
                *_strings(
                    self.data.get("embedded_media", {}).get(str(source_id), ())
                ),
                *self._indexed_source_bank_paths(source_id),
            ]
            for source_id in wanted
        }

        package_table = self.data.get("media_packages", {})
        for groups in self.data.get("media_links", {}).values():
            if not isinstance(groups, dict):
                continue
            if package_table:
                for package_key, source_ids in groups.items():
                    record = package_table.get(package_key, {})
                    if isinstance(record, dict):
                        candidates = tuple(filter(None, (
                            record.get("streaming", ""),
                            record.get("index", ""),
                        )))
                        for source_id in wanted.intersection(source_ids):
                            paths[source_id].extend(candidates)
            else:
                for raw_source, records in groups.items():
                    try:
                        source_id = int(raw_source) & 0xFFFFFFFF
                    except (TypeError, ValueError):
                        continue
                    if source_id not in wanted:
                        continue
                    for record in records:
                        if isinstance(record, dict):
                            paths[source_id].extend(filter(None, record.values()))
        return {
            source_id: normalized
            for source_id, values in paths.items()
            if (normalized := _strings(values))
        }

    def preview_media_paths_for_references(
        self, references
    ) -> dict[MessageSoundReference, tuple[str, ...]]:
        """Narrow timeline references to their exact Wwise bank families."""

        references = tuple(dict.fromkeys(references))
        resolved = {}
        fallback = []
        bank_records = self.data.get("banks", {})
        embedded = self.data.get("embedded_media", {})
        for reference in references:
            wanted_banks = set(reference.banks)
            if not wanted_banks:
                fallback.append(reference)
                continue

            source_id = reference.source_id
            source_paths = self._indexed_source_bank_paths(source_id)
            declarations = tuple(
                path for path in source_paths if bank_key(path) in wanted_banks
            )
            anchors = list(declarations)
            for bank in wanted_banks:
                record = bank_records.get(bank, {})
                if isinstance(record, dict):
                    anchors.extend(_strings(record.get("paths", ())))

            # The event bank itself is an exact candidate. Compact indexes do
            # not retain every ordinary in-bank Source declaration, and a
            # source can be embedded directly in this bank (not a PCK/media
            # sibling). Extraction still verifies the requested Source ID.
            candidates = list(anchors)
            candidates.extend(
                path
                for path in _strings(embedded.get(str(source_id), ()))
                if bank_key(path) in wanted_banks
            )
            for path in _strings(anchors):
                candidates.extend(self.embedded_media_banks(source_id, path))
                candidates.extend(self.prefetch_media_banks(source_id, path))
                for package in self.media_packages(source_id, path):
                    if isinstance(package, dict):
                        candidates.extend(filter(None, (
                            package.get("streaming", ""),
                            package.get("index", ""),
                        )))
            if paths := _strings(candidates):
                resolved[reference] = paths

        general = self.preview_media_paths_for_sources(
            reference.source_id for reference in fallback
        )
        resolved.update({
            reference: general[reference.source_id]
            for reference in fallback
            if general.get(reference.source_id)
        })
        return resolved

    def source_context_lines(
        self, source_id: int, media_path: str | None = None
    ) -> tuple[str, ...]:
        """Format exact PFB MediaId/MessageId relationships for one WEM."""

        source_id = int(source_id) & 0xFFFFFFFF
        lines = [
            f"Used by {label}"
            for label in self.source_event_labels(source_id, media_path)
        ]
        for group in self.source_contexts(source_id):
            banks = group.get("banks", {})

            def source_paths(value: int) -> tuple[str, ...]:
                legacy = banks.get(str(value), ()) if isinstance(banks, dict) else ()
                return _strings(legacy) or self._indexed_source_bank_paths(value)

            current_locales = self._bank_locales(source_paths(source_id))
            for related in group.get("source_ids", ()):
                related = int(related) & 0xFFFFFFFF
                if related == source_id:
                    continue
                paths = source_paths(related)
                related_locales = self._bank_locales(paths)
                kind = (
                    "Localized counterpart"
                    if current_locales and related_locales
                    and current_locales.isdisjoint(related_locales)
                    else "Related media"
                )
                locations = ", ".join(path.rsplit("/", 1)[-1] for path in paths)
                lines.append(
                    f"{kind}: Source {related}"
                    + (f" · {locations}" if locations else "")
                )
            prefabs = _strings(group.get("prefabs", ()))
            game_objects = _strings(group.get("game_objects", ()))
            users = _strings(group.get("users", ()))
            if prefabs:
                line = "Used by prefab: " + ", ".join(
                    path.rsplit("/", 1)[-1] for path in prefabs
                )
                if game_objects:
                    line += " · GameObject " + ", ".join(game_objects)
                lines.append(line)
            if users:
                lines.append("Used by message data: " + ", ".join(
                    path.rsplit("/", 1)[-1] for path in users
                ))
            message_id = group.get("message_id")
            if message_id:
                label = "Message GUID" if "-" in str(message_id) else "Message ID"
                lines.append(f"{label}: {message_id}")
        return _strings(lines)

    def _indexed_source_bank_paths(self, source_id: int) -> tuple[str, ...]:
        table = self.data.get("source_bank_paths", ())
        indexes = self.data.get("source_banks", {}).get(
            str(int(source_id) & 0xFFFFFFFF), ()
        )
        return _strings(
            table[index]
            for value in indexes
            if isinstance(value, int) and 0 <= (index := value) < len(table)
        )

    def _source_event_records(
        self, source_id: int, media_path: str | None = None
    ) -> tuple[dict, ...]:
        source_id = int(source_id) & 0xFFFFFFFF
        values = self.data.get("source_events", {}).get(str(source_id), ())
        table = self.data.get("source_event_records", ())
        if table:
            records = tuple(
                {"bank": table[index][0], "event": table[index][1]}
                for value in values
                if isinstance(value, int) and 0 <= (index := value) < len(table)
                and isinstance(table[index], list) and len(table[index]) >= 2
            )
        else:
            records = tuple(record for record in values if isinstance(record, dict))
        if not media_path:
            return records
        media = resource_key(media_path)
        return tuple(
            record for record in records
            if any(
                media in (
                    self.embedded_media_banks(source_id, event_path)
                    + self.prefetch_media_banks(source_id, event_path)
                )
                for event_path in self.data.get("banks", {}).get(
                    str(record.get("bank", "")), {}
                ).get("paths", ())
            )
        )

    def source_event_labels(
        self, source_id: int, media_path: str | None = None
    ) -> tuple[str, ...]:
        records = self._source_event_records(source_id, media_path)
        bank_events = self.data.get("bank_events", {})
        labels = []
        for record in records:
            bank = str(record.get("bank", ""))
            event_id = int(record.get("event", 0)) & 0xFFFFFFFF
            event = bank_events.get(bank, {}).get(str(event_id), {})
            names = _strings(event.get("names", ())) if isinstance(event, dict) else ()
            label = " / ".join(names) or f"Event {event_id}"
            labels.append(f"{label} · {bank}" if bank else label)
        return _strings(labels)

    @staticmethod
    def _bank_locales(paths) -> set[str]:
        locales = set()
        for path in _strings(paths):
            match = re.search(r"\.(?:x64|stm)\.([^./]+)$", path.casefold())
            if match:
                locales.add(match.group(1))
        return locales

    def bindings(self, category: str, object_id: int) -> tuple[str, ...]:
        values = (
            self.data.get("bindings", {})
            .get(category, {})
            .get(str(int(object_id) & 0xFFFFFFFF), ())
        )
        return _strings(values)

    def external_object(self, object_id: int) -> dict:
        record = self.data.get("external_objects", {}).get(
            str(int(object_id) & 0xFFFFFFFF), {}
        )
        return record if isinstance(record, dict) else {}

    def external_object_label(self, object_id: int) -> str:
        object_id = int(object_id) & 0xFFFFFFFF
        record = self.external_object(object_id)
        if not record:
            return f"External Wwise object {object_id}"
        types = _strings(record.get("types", ()))
        banks = _strings(record.get("banks", ()))
        roles = _strings(record.get("roles", ()))
        kind = " / ".join(types)
        if banks:
            return f"Cross-bank {kind or 'Wwise object'} {object_id} in {', '.join(banks)}"
        role = " / ".join(roles) or "Wwise object"
        required = []
        for bank in record.get("required_banks", ()):
            if not isinstance(bank, dict):
                continue
            required.extend(_strings(bank.get("names", ())))
            if not bank.get("names") and bank.get("id") is not None:
                required.append(str(bank["id"]))
        suffix = f"; required bank {', '.join(dict.fromkeys(required))}" if required else ""
        return f"Unavailable {role} {object_id} (not serialized in shipped banks{suffix})"

    def media_packages(self, source_id: int, bank_path: str | None = None) -> tuple[dict, ...]:
        bank = resource_key(bank_path or self.source_path)
        source_id = int(source_id) & 0xFFFFFFFF
        if runtime := self._runtime_index():
            return runtime.media_packages(source_id, bank)
        package_table = self.data.get("media_packages", {})
        if package_table:
            groups = self.data.get("media_links", {}).get(bank, {})
            return tuple(
                package_table[key]
                for key, source_ids in groups.items()
                if key in package_table and _contains_sorted(source_ids, source_id)
            )
        values = self.data.get("media_links", {}).get(bank, {}).get(
            str(source_id), ()
        )
        return tuple(value for value in values if isinstance(value, dict))

    def banks_for_package(self, package_path: str, source_id: int) -> tuple[str, ...]:
        if runtime := self._runtime_index():
            return runtime.banks_for_package(package_path, source_id)
        key = (resource_key(package_path), int(source_id) & 0xFFFFFFFF)
        package_table = self.data.get("media_packages", {})
        if package_table:
            if self._package_banks is None:
                self._package_banks = {}
            if key not in self._package_banks:
                matches = []
                for bank, groups in self.data.get("media_links", {}).items():
                    for package_key, source_ids in groups.items():
                        record = package_table.get(package_key, {})
                        if (
                            key[0] in map(resource_key, record.values())
                            and _contains_sorted(source_ids, key[1])
                        ):
                            matches.append(bank)
                self._package_banks[key] = tuple(dict.fromkeys(matches))
            return self._package_banks[key]
        if self._package_banks is None:
            reverse: dict[tuple[str, int], list[str]] = {}
            for bank, sources in self.data.get("media_links", {}).items():
                for raw_id, packages in sources.items():
                    for package in packages:
                        for path in package.values():
                            reverse.setdefault(
                                (resource_key(path), int(raw_id)), []
                            ).append(bank)
            self._package_banks = {
                item: tuple(dict.fromkeys(banks))
                for item, banks in reverse.items()
            }
        return self._package_banks.get(key, ())

    def embedded_media_banks(
        self, source_id: int, bank_path: str | None = None
    ) -> tuple[str, ...]:
        bank = resource_key(bank_path or self.source_path)
        source = str(int(source_id) & 0xFFFFFFFF)
        specific = _strings(
            self.data.get("embedded_media_by_bank", {})
            .get(bank, {})
            .get(source, ())
        )
        if runtime := self._runtime_index():
            live = runtime.embedded_media_banks(source_id, bank)
            if specific:
                allowed = set(map(resource_key, specific))
                narrowed = tuple(path for path in live if resource_key(path) in allowed)
                if narrowed:
                    return narrowed
            return live
        values = specific
        if not values:
            values = self.data.get("embedded_media", {}).get(source, ())
        if not values:
            values = self.data.get("embedded_media_links", {}).get(bank, {}).get(
                source, ()
            )
        return _strings(values)

    def prefetch_media_banks(
        self, source_id: int, bank_path: str | None = None
    ) -> tuple[str, ...]:
        bank = resource_key(bank_path or self.source_path)
        if runtime := self._runtime_index():
            return runtime.prefetch_media_banks(source_id, bank)
        values = (
            self.data.get("prefetch_media_by_bank", {})
            .get(bank, {})
            .get(str(int(source_id) & 0xFFFFFFFF), ())
        )
        return _strings(values)

    def prefetch_event_banks(
        self, source_id: int, media_path: str | None = None
    ) -> tuple[str, ...]:
        if runtime := self._runtime_index():
            return runtime.prefetch_event_banks(
                source_id, resource_key(media_path or self.source_path)
            )
        key = (resource_key(media_path or self.source_path), int(source_id) & 0xFFFFFFFF)
        if self._prefetch_event_banks is None:
            reverse: dict[tuple[str, int], list[str]] = {}
            for bank, sources in self.data.get("prefetch_media_by_bank", {}).items():
                for raw_id, paths in sources.items():
                    for path in _strings(paths):
                        reverse.setdefault(
                            (resource_key(path), int(raw_id)), []
                        ).append(bank)
            self._prefetch_event_banks = {
                item: tuple(dict.fromkeys(banks))
                for item, banks in reverse.items()
            }
        return self._prefetch_event_banks.get(key, ())

    def source_event_banks(
        self, source_id: int, media_path: str | None = None
    ) -> tuple[str, ...]:
        paths = []
        for record in self._source_event_records(source_id, media_path):
            bank = str(record.get("bank", ""))
            paths.extend(self.data.get("banks", {}).get(bank, {}).get("paths", ()))
        exact = _strings(paths)
        if exact:
            return exact
        if runtime := self._runtime_index():
            return runtime.source_event_banks(
                source_id, resource_key(media_path or self.source_path)
            )
        return ()

    def media_plugin_ids(self, source_id: int) -> tuple[int, ...]:
        values = self.data.get("media_plugins", {}).get(
            str(int(source_id) & 0xFFFFFFFF), ()
        )
        return tuple(dict.fromkeys(int(value) & 0xFFFFFFFF for value in values))

    def label(self, category: str, object_id: int, *, event: bool = False) -> str:
        object_id = int(object_id) & 0xFFFFFFFF
        names = self.event_names(object_id) if event else self.names(category, object_id)
        return " / ".join(names) if names else str(object_id)

    def id_label(self, category: str, object_id: int, *, event: bool = False) -> str:
        object_id = int(object_id) & 0xFFFFFFFF
        names = self.event_names(object_id) if event else self.names(category, object_id)
        return f"{' / '.join(names)} [{object_id}]" if names else str(object_id)

    def search_text(self, category: str, object_id: int, *, event: bool = False) -> str:
        names = self.event_names(object_id) if event else self.names(category, object_id)
        return " ".join(names)

    def resolve_id(self, category: str, value: str, *, event: bool = False) -> int:
        """Resolve a numeric ID or an exact recovered name; hash Wwise names as fallback."""

        text = str(value).strip()
        if not text:
            raise ValueError("ID or name is required")
        try:
            parsed = int(text, 10 if text.lstrip("+-").isdigit() else 0)
        except ValueError:
            parsed = None
        if parsed is not None:
            if not 0 <= parsed <= 0xFFFFFFFF:
                raise ValueError(f"ID is outside the unsigned 32-bit range: {text}")
            return parsed
        key = f"{category}:{int(event)}"
        reverse = self._reverse.get(key)
        if reverse is None:
            table: dict[str, set[int]] = {}
            source = self._name_table(category)
            for raw_id, names in source.items():
                for name in _strings(names):
                    table.setdefault(name.casefold(), set()).add(int(raw_id))
            for object_id, names in self.fallbacks.get(category, {}).items():
                for name in names:
                    table.setdefault(name.casefold(), set()).add(object_id)
            if event:
                for raw_id, record in self.data.get("bank_events", {}).get(self.bank, {}).items():
                    for name in _strings(record.get("names", ())):
                        table.setdefault(name.casefold(), set()).add(int(raw_id))
                for object_id, names in self._live_events.items():
                    for name in names:
                        table.setdefault(name.casefold(), set()).add(object_id)
            reverse = {name: tuple(sorted(ids)) for name, ids in table.items()}
            self._reverse[key] = reverse
        matches = reverse.get(text.casefold(), ())
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"{text!r} identifies multiple {self.game_name} IDs; enter a numeric ID"
            )
        if category == "trigger":
            raise ValueError(f"Unknown {self.game_name} trigger name: {text}")
        from .bnk_parser import wwise_id_from_name

        return wwise_id_from_name(text)

    def unknown_ids(self, category: str) -> tuple[int, ...]:
        values = self.data.get("unnamed_ids", {}).get(category)
        if values is None:
            values = self.data.get("unknowns", {}).get(category, ())
        return tuple(int(value) for value in values) if isinstance(values, list) else ()

    def _load_live_wel(self, handler) -> None:
        if not self.bank:
            return
        record = self.data.get("banks", {}).get(self.bank, {})
        path = record.get("wel", "") if isinstance(record, dict) else ""
        if not path:
            path = self.data.get("bank_events", {}).get(self.bank, {}).get("_wel", "")
        if not path:
            return
        context = resource_context_for_handler(handler)
        if context is None:
            return
        try:
            resolved = context.resolve(path, allow_selection_dialog=False)
        except (OSError, ValueError, TypeError):
            return
        if resolved is None:
            return
        try:
            from file_handlers.wel.wel_file import WELFile

            wel = WELFile()
            if not wel.read(resolved[1]):
                return
        except (OSError, ValueError, TypeError):
            return
        for entry in wel.events:
            names = self.names("trigger", entry.mTriggerId)
            if names:
                self._live_events[entry.mEventId] = names
        self.live_wel_path = resolved[0]


__all__ = ["IndexedSoundMetadata", "STANDARD_CUE_FALLBACKS", "bank_key"]
