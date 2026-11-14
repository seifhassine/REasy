"""See docs/internals/gui-rendering.md, Masks."""
from __future__ import annotations

from collections.abc import Callable, Iterable


def _label(node, name: str) -> str:
    return node.runtime.enum_label(node.render_properties, name)


def mask_passes(nodes: Iterable, enabled: Callable, draws: Callable) -> dict[str, tuple]:
    counts, mode, held, skipped, passes = None, "Default", [], set(), {}
    for node in nodes:
        if id(node.parent) in skipped or not enabled(node):
            skipped.add(id(node))
            continue
        types = node.runtime.enum("MaskType")
        counts = counts or [0] * (max(types.values()) - types["Mask"] + 1)
        while held and held[-1][0] is not node.parent:
            _left, counts, mode = held.pop()
        if not draws(node):
            own = _label(node, "MaskMode")
            held.append((node, [count + (own == "ApplyToParent") for count in counts], mode))
            counts = list(counts)
            if own not in ("Keep", "ApplyToParent"):
                mode = own
            continue
        level = node.runtime.enum_value(node.render_properties, "MaskType") - types["Mask"]
        if level < 0:
            masked = _label(node, "MaskType") == "Target" and mode != "Disable" and any(counts)
            passes[node.path] = (("outside" if mode == "Reverse" else "inside") if masked else "none", None,
                                 (next(at for at, count in enumerate(counts) if count),) if masked else (), False)
        else:
            reads = tuple(at for at in range(level + 1, len(counts)) if counts[at])
            counts[level] += 1
            passes[node.path] = ("write", level, reads, counts[level] == 1)
    return passes
