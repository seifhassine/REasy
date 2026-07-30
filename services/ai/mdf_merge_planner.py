from __future__ import annotations

from typing import Any


MISSING = object()


def merge_equal(left: Any, right: Any) -> bool:
    if left is MISSING or right is MISSING:
        return left is right
    return left == right


def _merge_order_delta(
    baseline: list[str],
    modded: list[str],
    target: list[str],
) -> list[str]:
    """Apply the mod's ordering intent while retaining target-only items."""

    baseline_set = set(baseline)
    modded_set = set(modded)
    deleted = baseline_set - modded_set
    result = [item for item in target if item not in deleted]

    shared = baseline_set.intersection(modded, target)
    baseline_shared = [item for item in baseline if item in shared]
    modded_shared = [item for item in modded if item in shared]
    mod_reordered = modded_shared != baseline_shared

    additions = [item for item in modded if item not in baseline_set]
    if mod_reordered:
        allowed = set(result).union(additions)
        mod_sequence = [item for item in modded if item in allowed]
        controlled = set(mod_sequence)
        first_controlled = next(
            (
                index
                for index, item in enumerate(result)
                if item in controlled
            ),
            len(result),
        )
        destination_only = [
            item for item in result if item not in controlled
        ]
        before = [
            item
            for item in result[:first_controlled]
            if item not in controlled
        ]
        after = destination_only[len(before):]
        return [*before, *mod_sequence, *after]

    for addition in additions:
        if addition in result:
            result.remove(addition)
        mod_index = modded.index(addition)
        next_item = next(
            (item for item in modded[mod_index + 1:] if item in result),
            None,
        )
        if next_item is not None:
            result.insert(result.index(next_item), addition)
            continue
        previous_item = next(
            (
                item
                for item in reversed(modded[:mod_index])
                if item in result
            ),
            None,
        )
        if previous_item is None:
            result.append(addition)
        else:
            result.insert(result.index(previous_item) + 1, addition)
    return result


def _plan_order_merge(
    baseline: list[str],
    modded: list[str],
    target: list[str],
    path: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    shared = set(baseline).intersection(modded, target)
    baseline_shared = [item for item in baseline if item in shared]
    modded_shared = [item for item in modded if item in shared]
    target_shared = [item for item in target if item in shared]
    mod_reordered = modded_shared != baseline_shared
    target_reordered = target_shared != baseline_shared
    record = {
        "path": path,
        "baseline": baseline,
        "modded": modded,
        "target": target,
    }
    if (
        mod_reordered
        and target_reordered
        and modded_shared != target_shared
    ):
        return [], [record]

    desired = _merge_order_delta(baseline, modded, target)
    if desired == target:
        return [], []
    return [{**record, "modded": desired}], []


def plan_three_way_merge(
    baseline: Any,
    modded: Any,
    target: Any,
    path: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return safe mod changes and conflicts without mutating any MDF."""

    if merge_equal(modded, baseline) or merge_equal(target, modded):
        return [], []

    if (
        path
        and (
            path[-1] == "material_order"
            or path[-1].endswith("_order")
        )
        and all(isinstance(value, list) for value in (baseline, modded, target))
    ):
        return _plan_order_merge(baseline, modded, target, path)

    if all(isinstance(value, dict) for value in (baseline, modded, target)):
        changes: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        keys = (
            key
            for key in dict.fromkeys((*baseline, *modded, *target))
            if key != "_index"
        )
        for key in keys:
            nested_changes, nested_conflicts = plan_three_way_merge(
                baseline.get(key, MISSING),
                modded.get(key, MISSING),
                target.get(key, MISSING),
                (*path, str(key)),
            )
            changes.extend(nested_changes)
            conflicts.extend(nested_conflicts)
        return changes, conflicts

    record = {
        "path": path,
        "baseline": baseline,
        "modded": modded,
        "target": target,
    }
    if merge_equal(target, baseline):
        return [record], []
    return [], [record]
