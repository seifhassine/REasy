

from __future__ import annotations

from pathlib import Path

from .errors import GuiFormatError
from .model import GuiDocument
from .profiles import GuiFormatProfile, gui_profile_from_data


def parse_gui(
    data: bytes,
    source: str = "<bytes>",
    *,
    profile: GuiFormatProfile | None = None,
) -> GuiDocument:
    payload = bytes(data)
    if profile is None:
        try:
            profile = gui_profile_from_data(payload)
        except ValueError as exc:
            raise GuiFormatError(f"{source}: {exc}") from exc
    return profile.adapter.parse(payload, source, profile)


def parse_gui_file(
    path: str | Path,
    *,
    profile: GuiFormatProfile | None = None,
) -> GuiDocument:
    source = Path(path)
    return parse_gui(source.read_bytes(), str(source), profile=profile)
