"""Runtime resolution of game-specific GUI ``<ICON>`` aliases."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Dmc5IconInputContext:
    """Runtime values consumed by DMC5's recovered icon-tag callbacks."""

    keyboard_mode: bool = False
    mouse_buttons_swapped: bool = False
    keyboard_bindings: Mapping[int, int] = field(default_factory=dict)
    pad_bindings: Mapping[int, int] = field(default_factory=dict)
    menu_decide_button: int | None = None
    menu_cancel_button: int | None = None


def _keyboard_icon(
    icons: Mapping[str, Any],
    key: int | None,
    *,
    bind_key: bool = False,
    swapped: bool = False,
) -> str:
    if key is None:
        return icons["key_fallback"]
    key = int(key)
    first, second = icons["swapped_buttons"]
    if bind_key and swapped and key in (first, second):
        key = second if key == first else first
    for start, end, name, base in icons["key_ranges"]:
        if start <= key <= end:
            return name.format(char=chr(key), number=key - base)
    return icons["keys"].get(str(key), icons["key_fallback"])


def _pad_icon(icons: Mapping[str, Any], button: int | None, fallback: int) -> str:
    pad = icons["pad"]
    return pad.get(str(int(button)), pad[str(fallback)]) if button is not None else pad[str(fallback)]


def resolve_dmc5_icon_tag(
    name: str,
    icons: Mapping[str, Any],
    context: Dmc5IconInputContext | None = None,
) -> str:
    """Apply DMC5's case-sensitive ``app.IconTagConverter`` semantics."""

    runtime = context or Dmc5IconInputContext()
    swapped = runtime.mouse_buttons_swapped
    if name in icons["fixed"]:
        return icons["fixed"][name]
    if name in icons["device"]:
        key, pad, bind_key = icons["device"][name]
        return _keyboard_icon(icons, key, bind_key=bind_key, swapped=swapped) if runtime.keyboard_mode else pad
    if name in icons["actions"]:
        action, fallback = icons["actions"][name]
        if runtime.keyboard_mode:
            return _keyboard_icon(icons, runtime.keyboard_bindings.get(action), swapped=swapped)
        return _pad_icon(icons, runtime.pad_bindings.get(action), fallback)
    if name in icons["by_mode"]:
        keyboard, pad = icons["by_mode"][name]
        return keyboard if runtime.keyboard_mode else pad
    if name in icons["menu"]:
        key, bind_key, fallback = icons["menu"][name]
        if runtime.keyboard_mode:
            return _keyboard_icon(icons, key, bind_key=bind_key, swapped=swapped)
        return _pad_icon(icons, runtime.menu_decide_button if name == "DECIDE" else runtime.menu_cancel_button,
                         fallback)
    if name in icons["keyed"]:
        key, button = icons["keyed"][name]
        return _keyboard_icon(icons, key) if runtime.keyboard_mode else icons["pad"][str(button)]
    if name in icons["by_swap"]:
        normal, swapped_name = icons["by_swap"][name]
        return swapped_name if swapped else normal
    return name
