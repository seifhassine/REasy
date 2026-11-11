"""See docs/internals/gui-rendering.md, Masks."""
from __future__ import annotations

from collections.abc import Callable, Iterable


def _label(node, name: str) -> str:
    return node.runtime.enum_label(node.render_properties, name)


def mask_scope(node) -> str:
    scope = node.parent
    while scope is not None and _label(scope, "MaskMode") == "ApplyToParent":
        scope = scope.parent
    return scope.path if scope is not None else "/"


def mask_producers(nodes: Iterable, enabled: Callable) -> dict[str, dict[int, list]]:
    masks = {}
    for node in nodes:
        level = writer_level(node)
        if level >= 0 and enabled(node):
            masks.setdefault(mask_scope(node), {}).setdefault(level, []).append(node)
    return masks


def _in_scope(node, scope: str) -> bool:
    return node.path.startswith("/" if scope == "/" else f"{scope}/")


def applicable_masks(node, masks: dict[str, dict[int, list]]) -> list[list]:
    return [levels[level] for scope, levels in masks.items() if _in_scope(node, scope) for level in sorted(levels)]


def mask_level(node, masks: dict[str, dict[int, list]]) -> int:
    return max(level for scope, levels in masks.items() if _in_scope(node, scope) for level in levels)


def writer_level(node) -> int:
    return node.runtime.enum_value(node.render_properties, "MaskType") - node.runtime.enum("MaskType")["Mask"]


def mask_mode(node) -> str:
    mode, current, passed = "Default", node.parent, []
    while current is not None:
        passed.append(current)
        current = current.parent
    for ancestor in reversed(passed):
        if _label(ancestor, "MaskMode") not in ("Keep", "ApplyToParent"):
            mode = _label(ancestor, "MaskMode")
    return mode


def mask_role(node, masks: dict[str, dict[int, list]]) -> str:
    if writer_level(node) >= 0:
        return "write"
    mode = mask_mode(node)
    if _label(node, "MaskType") != "Target" or mode == "Disable" or not applicable_masks(node, masks):
        return "none"
    return "outside" if mode == "Reverse" else "inside"


def mask_passes(drawn: Iterable, masks: dict[str, dict[int, list]]) -> list[tuple]:
    written, passes = {}, []
    for node in drawn:
        role, writes, reads, clears = mask_role(node, masks), None, (), False
        if role == "write":
            writes, scope = writer_level(node), mask_scope(node)
            reads = tuple(sorted({level for at, levels in masks.items() if _in_scope(node, at) for level in levels
                                  if level < writes}))
            clears, written[writes] = written.get(writes) != scope, scope
        elif role != "none":
            reads = (mask_level(node, masks),)
        passes.append((node, role, writes, reads, clears))
    return passes
