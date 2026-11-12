"""See docs/internals/bhvt-editor.md, Canvas."""

from __future__ import annotations

import math
from collections import defaultdict, deque

COLUMN, ROW, TALL = 170.0, 22.0, 12


def state_layout(sizes: dict, edges: list[tuple], roots: list) -> dict:
    after, before = defaultdict(list), defaultdict(list)
    for source, target in edges:
        if source != target and target not in after[source]:
            after[source].append(target)
            before[target].append(source)
    level: dict = {}
    for start, first in [*((root, 0) for root in roots), *((key, 1) for key in sizes if key in after or key in before)]:
        queue = deque([start] if start not in level else ())
        level.setdefault(start, first)
        while queue:
            for target in after[key := queue.popleft()]:
                if target not in level:
                    level[target] = level[key] + 1
                    queue.append(target)
    columns = defaultdict(list)
    for key in sizes:
        if key in level:
            columns[level[key]].append(key)
    places, left, rank = {}, 0.0, {}
    for at in sorted(columns):
        keys = sorted(columns[at], key=lambda key: sum(rank.get(item, 0) for item in before[key]) / max(
            1, len(before[key])) if at and before[key] else math.inf if at else 0)
        for part in range(0, len(keys), TALL):
            column = keys[part:part + TALL]
            top = -sum(sizes[key][1] + ROW for key in column) / 2
            for key in column:
                places[key] = (left, top)
                rank[key] = top
                top += sizes[key][1] + ROW
            left += max(sizes[key][0] for key in column) + COLUMN
    loose = [key for key in sizes if key not in places]
    if loose:
        width = max(sizes[key][0] for key in loose) + 30
        across = max(4, math.ceil(math.sqrt(len(loose) * 1.6)))
        top = max((place[1] + sizes[key][1] for key, place in places.items()), default=0.0) + 60
        for start in range(0, len(loose), across):
            row = loose[start:start + across]
            for slot, key in enumerate(row):
                places[key] = (slot * width, top)
            top += max(sizes[key][1] for key in row) + ROW
    return places


def tree_layout(sizes: dict, children: dict, root, left: float = 0.0, places: dict | None = None,
                bottom: list | None = None) -> dict:
    places, bottom = {} if places is None else places, [0.0] if bottom is None else bottom
    width, height = sizes[root]
    top = bottom[0]
    for child in children.get(root, ()):
        tree_layout(sizes, children, child, left + width + COLUMN, places, bottom)
    if kids := children.get(root):
        middle = (places[kids[0]][1] + places[kids[-1]][1] + sizes[kids[-1]][1]) / 2
        top = max(top, middle - height / 2)
    places[root] = (left, top)
    bottom[0] = max(bottom[0], top + height + ROW)
    return places
