"""Game-specific runtime expansion of custom GUI text tags."""

from __future__ import annotations

import re
from collections.abc import Callable


NamedMessageResolver = Callable[[str, int], str]
_DMC5_INDEX_PARAMETER = re.compile(r'id="([0-9]+)"')
# MessageTagConverter.tagConvertTRCCore (retail RVA 0x1490F10) selects these
# consecutive via.gui.MsgID series from RuntimeEnvironment's target predicates.
_DMC5_TRC_GROUP_BY_TARGET = {
    "ps4": 0,
    "xb": 1,
    "xb1": 1,
    "pc": 2,
    "stm": 2,
    "uwp": 3,
}


def resolve_dmc5_text_tag(
    name: str,
    parameter: str,
    language: int,
    named_message: NamedMessageResolver,
    runtime_target: str = "",
) -> str | None:
    """Resolve DMC5's localized KEY and platform-sensitive TRC tags."""

    match = _DMC5_INDEX_PARAMETER.fullmatch(parameter)
    if match is None:
        return None
    index = int(match.group(1))
    if name == "KEY":
        message_name = f"KeyAssign_{index:03d}0"
    elif name == "TRC":
        group = _DMC5_TRC_GROUP_BY_TARGET.get(runtime_target.casefold())
        if group is None:
            return None
        message_name = f"TRC_{group * 1000 + index * 10:04d}"
    else:
        return None
    return named_message(message_name, int(language)) or None
