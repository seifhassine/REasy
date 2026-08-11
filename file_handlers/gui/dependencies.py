"""Compose the existing GCF, MSG, OFT, IFT, UVS, and TEX handlers for GUI."""

from __future__ import annotations

from dataclasses import dataclass

from file_handlers.font.catalog import GuiFontCatalog
from file_handlers.gcf.gcf_file import GcfFile
from file_handlers.gcf.model import GcfData
from file_handlers.ift.catalog import IconFontCatalog
from file_handlers.msg.msg_handler import MsgHandler
from utils.resource_file_utils import ResourceDataLoader, resource_path_with_version

from .assets import GuiAssetCatalog
from .errors import GuiAssetError
from .profiles import GuiFormatProfile


@dataclass(frozen=True, slots=True)
class LocalizedMessage:
    message_id: str
    name: str
    values: dict[int, str]
    source: str

    def text(self, language: int) -> str:
        # Language fallback is selected by the game/application layer, not by
        # MSG deserialization.  Returning another language here silently
        # changes the authored runtime input and conceals missing data.
        return self.values.get(int(language), "")


class GuiDependencyCatalog:
    """Resolve all non-GUI dependencies through their canonical handlers.

    Nothing is scanned from a hard-coded extraction directory.  The catalog
    uses the same project/PAK loader as the opened file and loads the global
    GCF plus message tables only when requested.
    """

    def __init__(
        self,
        resource_data_loader: ResourceDataLoader,
        profile: GuiFormatProfile,
    ) -> None:
        self.resource_data_loader = resource_data_loader
        self.profile = profile
        self.assets = GuiAssetCatalog(resource_data_loader, profile)
        self.errors: list[str] = []
        self._config: GcfData | None = None
        self._config_loaded = False
        self._messages_loaded = False
        self._messages: dict[str, LocalizedMessage] = {}
        self._messages_by_name: dict[str, LocalizedMessage] = {}
        self._font_catalog: GuiFontCatalog | None = None
        self._icon_catalog: IconFontCatalog | None = None

    @property
    def config(self) -> GcfData | None:
        if not self._config_loaded:
            self._load_config()
        return self._config

    def _load_config(self) -> None:
        self._config_loaded = True
        for path in self.profile.config_resource_paths:
            versioned = resource_path_with_version(
                path,
                "gcf",
                self.profile.default_gcf_version,
            )
            resolved = self.resource_data_loader(versioned)
            if resolved is None:
                continue
            source, data = resolved
            try:
                config = GcfFile.from_bytes(bytes(data)).require_model()
            except Exception as exc:
                self.errors.append(f"{source}: {exc}")
                continue
            # Profile order expresses the game's config role explicitly.  A
            # content-count heuristic can select a different resource when a
            # mod deliberately removes or adds entries.
            self._config = config
            return
        self.errors.append("no configured GUI GCF resource could be resolved")

    @property
    def font_catalog(self) -> GuiFontCatalog | None:
        if self._font_catalog is None and self.config is not None:
            self._font_catalog = GuiFontCatalog(
                self.config,
                self.resource_data_loader,
                strict=False,
            )
        return self._font_catalog

    @property
    def icon_catalog(self) -> IconFontCatalog | None:
        if self._icon_catalog is None and self.config is not None:
            if self.config.icon_font_asset_path:
                self._icon_catalog = IconFontCatalog.from_gcf(
                    self.config,
                    self.resource_data_loader,
                )
        return self._icon_catalog

    def load_messages(self) -> int:
        if self._messages_loaded:
            return len(self._messages)
        self._messages_loaded = True
        config = self.config
        if config is None:
            return 0
        for path in config.message_asset_paths:
            if not path:
                continue
            resolved = self.resource_data_loader(path)
            if resolved is None:
                self.errors.append(f"unresolved MSG resource {path!r}")
                continue
            source, data = resolved
            handler = MsgHandler()
            try:
                handler.read(bytes(data))
            except Exception as exc:
                self.errors.append(f"{source}: {exc}")
                continue
            for entry in handler.entries:
                message_id = str(entry.get("uuid", "")).casefold()
                if not message_id:
                    continue
                values = {
                    int(language): str(value)
                    for language, value in zip(
                        handler.useLanguages,
                        entry.get("content", ()),
                    )
                    if value
                }
                existing = self._messages.get(message_id)
                if existing is None:
                    self._messages[message_id] = LocalizedMessage(
                        message_id,
                        str(entry.get("name", "")),
                        values,
                        str(source),
                    )
                elif values:
                    merged = dict(existing.values)
                    for language, value in values.items():
                        merged.setdefault(language, value)
                    self._messages[message_id] = LocalizedMessage(
                        existing.message_id,
                        existing.name or str(entry.get("name", "")),
                        merged,
                        existing.source,
                    )
        for message in self._messages.values():
            if message.name:
                self._messages_by_name.setdefault(message.name.casefold(), message)
        return len(self._messages)

    @property
    def messages_loaded(self) -> bool:
        return self._messages_loaded

    def cached_message(self, message_id: str | None, language: int = 1) -> str:
        if not message_id:
            return ""
        message = self._messages.get(str(message_id).casefold())
        return message.text(language) if message is not None else ""

    def resolve_message(self, message_id: str | None, language: int = 1) -> str:
        if not message_id:
            return ""
        self.load_messages()
        return self.cached_message(message_id, language)

    def cached_named_message(self, name: str | None, language: int = 1) -> str:
        if not name:
            return ""
        message = self._messages_by_name.get(str(name).casefold())
        return message.text(language) if message is not None else ""

    def resolve_named_message(self, name: str | None, language: int = 1) -> str:
        if not name:
            return ""
        self.load_messages()
        return self.cached_named_message(name, language)

    def message_name(self, message_id: str | None) -> str:
        if not message_id:
            return ""
        self.load_messages()
        message = self._messages.get(str(message_id).casefold())
        return message.name if message is not None else ""

    def require_assets(self) -> GuiAssetCatalog:
        if self.resource_data_loader is None:
            raise GuiAssetError("GUI resource loader is unavailable")
        return self.assets
