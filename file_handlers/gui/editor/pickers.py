"""See docs/internals/gui-editor.md, Value editors."""

from __future__ import annotations

import uuid
from functools import partial

from PySide6.QtCore import QCoreApplication

from ui.search_popup import below


translate = QCoreApplication.translate


def asset_entries(state, kinds: tuple[str, ...]) -> list[tuple[str, str, str]]:
    return [(path.rpartition("/")[2], path.rpartition("/")[0], path) for kind in kinds for path in state.listed(kind)]


def message_entries(state) -> list[tuple[str, str, uuid.UUID]]:
    language = state.stage.language
    return [(item.name, " ".join(item.text(language).split()), uuid.UUID(item.message_id))
            for item in state.dependencies.messages()]


def _assets(state, popup, kinds: tuple[str, ...], apply, button) -> None:
    popup.pick(asset_entries(state, kinds), below(button),
               translate("GuiPickers", "Search the game's {0} files").format(" ".join(f".{kind}" for kind in kinds)),
               apply)


def _messages(state, popup, apply, button) -> None:
    popup.pick(message_entries(state), below(button),
               translate("GuiPickers", "Search the game's messages by name or text"), apply)


def picker(state, popup, type_name: str, field, apply) -> tuple[str, partial] | None:
    if field.asset:
        return (translate("GuiPickers", "Choose one of the game's files"),
                partial(_assets, state, popup, field.asset, apply))
    if field.name == state.workspace.profile.runtime_type(type_name).facts.get("message"):
        return translate("GuiPickers", "Choose one of the game's messages"), partial(_messages, state, popup, apply)
    return None
