"""Runtime resolution of game-specific GUI ``<ICON>`` aliases."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


# Literal virtual keys passed by DMC5's DECIDE/CANCEL icon-tag callbacks.
DMC5_MENU_DECIDE_VK = 69  # E
DMC5_MENU_CANCEL_VK = 2   # Right mouse button

_PAD_ICONS = {
    1: "DirU", 2: "DirD", 3: "DirL", 4: "DirR",
    5: "BtnU", 6: "BtnD", 7: "BtnL", 8: "BtnR",
    9: "LB", 10: "LT", 11: "RB", 12: "RT",
    13: "LStP", 14: "RStP", 15: "CenL", 16: "CenR",
    17: "CenC", 21: "Dir",
}

_KEY_ICONS = {
    1: "MOUSE_1", 2: "MOUSE_2", 4: "MOUSE_3", 5: "MOUSE_5",
    6: "MOUSE_4", 8: "KEY_BS", 9: "KEY_TAB", 13: "KEY_ENTER_L",
    27: "KEY_ESC", 32: "KEY_SPACE", 33: "KEY_PAGE_U", 34: "KEY_PAGE_D",
    35: "KEY_END", 36: "KEY_HOME", 37: "KEY_CURSOR_L",
    38: "KEY_CURSOR_U", 39: "KEY_CURSOR_R", 40: "KEY_CURSOR_D",
    45: "KEY_INS", 46: "KEY_DEL", 106: "KEY_ASTERISK",
    107: "KEY_PLUS_SIGN", 109: "KEY_HYPHEN_R", 110: "KEY_PERIOD_R",
    111: "KEY_SLASH_R", 146: "KEY_ENTER_R", 160: "KEY_SHIFT_L",
    161: "KEY_SHIFT_R", 162: "KEY_CTRL_L", 163: "KEY_CTRL_R",
    164: "KEY_ALT_L", 165: "KEY_ALT_R", 186: "KEY_COLON",
    187: "KEY_SEMICOLON", 188: "KEY_COMMA", 189: "KEY_HYPHEN_L",
    190: "KEY_PERIOD_L", 191: "KEY_SLASH_L", 192: "KEY_AT_SIGN",
    219: "KEY_BRACKET_L", 220: "KEY_YEN_SIGN", 221: "KEY_BRACKET_R",
    222: "KEY_CARET", 226: "KEY_UNDERSCORE",
}

_FIXED_ALIASES = {
    "BtnD": "BtnD", "BtnR": "BtnR", "CenL": "CenL", "CenC": "CenC",
    "Dir": "Dir", "ACT_LS": "LS",
    **{f"CFG_{name}": name for name in (
        "BtnD", "BtnR", "BtnL", "BtnU", "LB", "RB", "LT", "RT",
        "LStP", "RStP", "CenL", "CenR", "DirU", "DirD", "DirL", "DirR",
    )},
}

# alias -> (keyboard virtual key, pad glyph, honor swapped mouse buttons)
_DEVICE_ALIASES = {
    "BtnL": (66, "BtnL", False), "BtnU": (86, "BtnU", False),
    "LBtn": (9, "LB", False), "LB": (9, "LB", False),
    "RBtn": (9, "RB", False), "RB": (9, "RB", False),
    "LTrg": (37, "LT", False), "LT": (37, "LT", False),
    "RTrg": (39, "RT", False), "RT": (39, "RT", False),
    "LStP": (115, "LStP", False), "RStP": (120, "RStP", False),
    "CenR": (27, "CenR", False), "DirU": (38, "DirU", False),
    "DirD": (40, "DirD", False), "DirL": (37, "DirL", False),
    "DirR": (39, "DirR", False),
}

# alias -> (DMC5 action index, native pad fallback)
_ACTION_ALIASES = {
    "ACT_BtnD": (2, 6), "ACT_BtnU": (0, 5), "ACT_BtnL": (1, 7),
    "ACT_BtnR": (12, 8), "ACT_RT": (13, 12), "ACT_LT": (14, 10),
    "ACT_RB": (4, 11), "ACT_LB": (16, 9), "ACT_CenL": (3, 15),
    "ACT_DirU": (8, 1), "ACT_DirL": (10, 3), "ACT_DirR": (11, 4),
    "ACT_DirD": (9, 2), "ACT_Dir": (6, 21),
    "ACT_LStP": (5, 13), "ACT_RStP": (15, 14),
}


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
    key: int | None,
    *,
    bind_key: bool = False,
    swapped: bool = False,
) -> str:
    if key is None:
        return "KEY"
    key = int(key)
    if bind_key and swapped and key in (1, 2):
        key = 3 - key
    if 48 <= key <= 57:
        return f"KEY_{chr(key)}_L"
    if 65 <= key <= 90:
        return f"KEY_{chr(key)}"
    if 96 <= key <= 105:
        return f"KEY_{key - 96}_R"
    if 112 <= key <= 123:
        return f"KEY_F{key - 111}"
    return _KEY_ICONS.get(key, "KEY")


def _pad_icon(button: int | None, fallback: int) -> str:
    return _PAD_ICONS.get(int(button), _PAD_ICONS[fallback]) if button is not None else _PAD_ICONS[fallback]


def resolve_dmc5_icon_tag(
    name: str,
    context: Dmc5IconInputContext | None = None,
) -> str:
    """Apply DMC5's case-sensitive ``app.IconTagConverter`` semantics."""

    runtime = context or Dmc5IconInputContext()
    if name in _FIXED_ALIASES:
        return _FIXED_ALIASES[name]
    if name in _DEVICE_ALIASES:
        key, pad, bind_key = _DEVICE_ALIASES[name]
        return (
            _keyboard_icon(key, bind_key=bind_key, swapped=runtime.mouse_buttons_swapped)
            if runtime.keyboard_mode else pad
        )
    if name in _ACTION_ALIASES:
        action, fallback = _ACTION_ALIASES[name]
        return (
            _keyboard_icon(
                runtime.keyboard_bindings.get(action),
                swapped=runtime.mouse_buttons_swapped,
            )
            if runtime.keyboard_mode
            else _pad_icon(runtime.pad_bindings.get(action), fallback)
        )
    if name == "LS_UD":
        return "MOUSE_SCR_UD" if runtime.keyboard_mode else "LS_UD"
    if name == "DECIDE":
        if runtime.keyboard_mode:
            return _keyboard_icon(DMC5_MENU_DECIDE_VK)
        return _pad_icon(runtime.menu_decide_button, 8)
    if name == "CANCEL":
        if runtime.keyboard_mode:
            return _keyboard_icon(
                DMC5_MENU_CANCEL_VK,
                bind_key=True,
                swapped=runtime.mouse_buttons_swapped,
            )
        return _pad_icon(runtime.menu_cancel_button, 6)
    if name in {"CHECK", "SORT"}:
        key, button = ((66, 7) if name == "CHECK" else (86, 5))
        return _keyboard_icon(key) if runtime.keyboard_mode else _PAD_ICONS[button]
    if name == "ACT_RS":
        return "MOUSE" if runtime.keyboard_mode else "RS"
    if name == "KEY_DRAG":
        return "MOUSE_DRAG_2" if runtime.mouse_buttons_swapped else "MOUSE_DRAG_1"
    if name == "DirLR":
        return "KEY_LR" if runtime.keyboard_mode else "DirLR"
    return name
