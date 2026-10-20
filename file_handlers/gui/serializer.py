"""Exact-profile dispatch for semantic GUI serializers."""

from __future__ import annotations

lazy from .errors import GuiWriteError
lazy from .model import GuiDocument
lazy from .profiles import GuiFormatProfile, gui_profile


def serialize_gui(
    document: GuiDocument,
    *,
    profile: GuiFormatProfile | None = None,
) -> bytes:
    try:
        selected = profile or gui_profile(document.version)
    except ValueError as exc:
        raise GuiWriteError(str(exc)) from exc
    return selected.adapter.serialize(document, selected)
