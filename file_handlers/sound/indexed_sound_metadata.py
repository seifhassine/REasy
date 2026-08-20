"""Generated names and game-side context shared by RE Engine sound profiles."""

from __future__ import annotations

import gzip
import json
from bisect import bisect_left
from functools import lru_cache
import re

from utils.app_paths import resource_path
from utils.resource_file_utils import resource_context_for_handler

from .sound_metadata import SoundMetadata
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
        self.live_wel_path = ""

    @classmethod
    def for_handler(cls, handler) -> "IndexedSoundMetadata":
        metadata = cls(getattr(handler, "filepath", "") or getattr(handler, "filename", ""))
        metadata._load_live_wel(handler)
        return metadata

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
        values = (
            self.data.get("embedded_media_by_bank", {})
            .get(bank, {})
            .get(source, ())
        )
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
        values = (
            self.data.get("prefetch_media_by_bank", {})
            .get(bank, {})
            .get(str(int(source_id) & 0xFFFFFFFF), ())
        )
        return _strings(values)

    def prefetch_event_banks(
        self, source_id: int, media_path: str | None = None
    ) -> tuple[str, ...]:
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
        return _strings(paths)

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
