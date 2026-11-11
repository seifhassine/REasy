"""Game-specific runtime expansion of custom GUI text tags."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Mapping


NamedMessageResolver = Callable[[str, int], str]


def resolve_dmc5_text_tag(
    name: str,
    parameter: str,
    language: int,
    named_message: NamedMessageResolver,
    tags: Mapping[str, Any],
    runtime_target: str,
) -> str | None:
    """Resolve DMC5's localized KEY and platform-sensitive TRC tags."""

    rule = tags["tags"].get(name)
    match = re.fullmatch(tags["parameter"], parameter)
    if rule is None or match is None:
        return None
    index = int(match.group(1))
    if "groups" in rule:
        group = rule["groups"].get(runtime_target.casefold())
        if group is None:
            return None
        index = group * rule["group_step"] + index * rule["index_step"]
    return named_message(rule["message"].format(index=index), int(language)) or None
