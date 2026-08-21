"""Game-neutral metadata interface used by the sound editors."""

from __future__ import annotations


class SoundMetadata:
    """Empty metadata provider for games without a registered sound profile."""

    live_wel_path = ""

    def prepare_operational_index(self, *, wait: bool = False) -> bool:
        return False

    def names(self, category: str, object_id: int) -> tuple[str, ...]:
        return ()

    def event_names(self, event_id: int) -> tuple[str, ...]:
        return self.names("event", event_id)

    def event_contexts(self, event_id: int) -> tuple[dict, ...]:
        return ()

    def event_context_lines(self, event_id: int) -> tuple[str, ...]:
        return ()

    def source_contexts(self, source_id: int) -> tuple[dict, ...]:
        return ()

    def source_context_lines(
        self, source_id: int, media_path: str | None = None
    ) -> tuple[str, ...]:
        return ()

    def bindings(self, category: str, object_id: int) -> tuple[str, ...]:
        return ()

    def external_object(self, object_id: int) -> dict:
        return {}

    def external_object_label(self, object_id: int) -> str:
        return str(int(object_id) & 0xFFFFFFFF)

    def media_packages(
        self, source_id: int, bank_path: str | None = None
    ) -> tuple[dict, ...]:
        return ()

    def banks_for_package(
        self, package_path: str, source_id: int
    ) -> tuple[str, ...]:
        return ()

    def embedded_media_banks(
        self, source_id: int, bank_path: str | None = None
    ) -> tuple[str, ...]:
        """Return complete BNKs carrying an in-bank source used elsewhere."""

        return ()

    def prefetch_media_banks(
        self, source_id: int, bank_path: str | None = None
    ) -> tuple[str, ...]:
        """Return split media BNKs carrying a streamed source's prefetch."""

        return ()

    def prefetch_event_banks(
        self, source_id: int, media_path: str | None = None
    ) -> tuple[str, ...]:
        """Return event BNKs declaring a split media bank's prefetch."""

        return ()

    def source_event_banks(
        self, source_id: int, media_path: str | None = None
    ) -> tuple[str, ...]:
        """Return event/routing banks known to reach this media source."""

        return ()

    def source_event_labels(
        self, source_id: int, media_path: str | None = None
    ) -> tuple[str, ...]:
        return ()

    def media_plugin_ids(self, source_id: int) -> tuple[int, ...]:
        """Return plug-ins that own this Source ID in another compiled bank."""

        return ()

    def label(
        self, category: str, object_id: int, *, event: bool = False
    ) -> str:
        names = self.event_names(object_id) if event else self.names(category, object_id)
        return " / ".join(names) or str(int(object_id) & 0xFFFFFFFF)

    def id_label(
        self, category: str, object_id: int, *, event: bool = False
    ) -> str:
        value = int(object_id) & 0xFFFFFFFF
        label = self.label(category, value, event=event)
        return f"{label} ({value})" if label != str(value) else label

    def search_text(
        self, category: str, object_id: int, *, event: bool = False
    ) -> str:
        return self.id_label(category, object_id, event=event).casefold()

    def resolve_id(
        self, category: str, value: str, *, event: bool = False
    ) -> int:
        try:
            result = int(str(value).strip(), 0)
        except ValueError as exc:
            raise ValueError(f"Enter a numeric {category.replace('_', ' ')} ShortID") from exc
        if not 0 <= result <= 0xFFFFFFFF:
            raise ValueError("ShortID must fit in an unsigned 32-bit integer")
        return result

    def source_lines(self) -> tuple[str, ...]:
        return ()

    def unknown_ids(self, category: str) -> tuple[int, ...]:
        return ()


__all__ = ["SoundMetadata"]
