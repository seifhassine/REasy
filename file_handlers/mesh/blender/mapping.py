from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from .skeleton_roles import normalized_name, resolve, role_pairs

MERGE, ADD, DROP = "merge", "add", "drop"
ACTIONS = (MERGE, ADD, DROP)

EXACT, ROLE, NAME, POSITION, MANUAL, LOADED = "exact", "role", "name", "position", "manual", "loaded"

MAPPING_FORMAT = "reasy-bone-mapping"


@dataclass(frozen=True)
class Bone:
    name: str
    parent: str = ""
    head: tuple | None = None  # engine-space rest position; None for a bare vertex group


@dataclass
class MappingRow:
    source: str
    weighted: int = 0
    target: str = ""
    action: str = MERGE        # what an unmapped row does with its weights
    origin: str = ""  # EXACT, ROLE, NAME, POSITION, MANUAL or LOADED; "" when unmapped

    @property
    def mapped(self):
        return bool(self.target)


@dataclass
class BoneMapping:
    source: dict               # name -> Bone, parents first
    target: dict               # name -> Bone, joint order
    rows: list = field(default_factory=list)
    families: tuple = ()       # (source, target) naming family labels when body-part roles were matched

    def __post_init__(self):
        self._rows = {row.source: row for row in self.rows}

    def row(self, name):
        return self._rows.get(name)

    def set_target(self, name, target, origin=MANUAL):
        row = self._rows[name]
        row.target = target if target in self.target else ""
        row.origin = origin if row.target else ""

    def ancestors(self, name):
        seen, parent = {name}, self.source[name].parent if name in self.source else ""
        while parent and parent not in seen:
            seen.add(parent)
            yield parent
            parent = self.source[parent].parent if parent in self.source else ""

    def mapped_ancestor(self, name):
        return next((row.target for row in map(self.row, self.ancestors(name)) if row and row.mapped), "")

    def merge_target(self, name):
        root = next((bone.name for bone in self.target.values() if not bone.parent), next(iter(self.target), ""))
        return self.mapped_ancestor(name) or root

    def rest_offset(self, row):
        """In metres."""
        source, target = self.source.get(row.source), self.target.get(row.target)
        if not row.mapped or source is None or target is None or source.head is None or target.head is None:
            return None
        return float(np.linalg.norm(np.subtract(source.head, target.head)))

    def skeleton_size(self):
        heads = np.array([b.head for b in self.target.values() if b.head is not None], dtype=np.float64)
        return float(np.ptp(heads, axis=0).max()) if len(heads) > 1 else 1.0

    def to_json(self):
        entries = {row.source: {"target": row.target} if row.mapped else {"action": row.action} for row in self.rows}
        return json.dumps({"format": MAPPING_FORMAT, "version": 1, "entries": entries}, indent=1)

    def apply_json(self, text):
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("format") != MAPPING_FORMAT or not isinstance(
                data.get("entries", {}), dict):
            raise ValueError("Not a REasy bone mapping")
        applied = 0
        for row in self.rows:
            entry = data.get("entries", {}).get(row.source)
            if not isinstance(entry, dict):
                continue
            target = entry.get("target", "")
            row.target, row.origin = (target, LOADED) if target in self.target else ("", "")
            if not row.mapped:
                row.action = entry.get("action") if entry.get("action") in ACTIONS else MERGE
            applied += 1
        return applied


def suggest(source, target, weighted):
    mapping = BoneMapping({b.name: b for b in source}, {b.name: b for b in target},
                          [MappingRow(b.name, int(weighted.get(b.name, 0))) for b in source])
    taken = set()

    def claim(row, name, origin):
        row.target, row.origin = name, origin
        taken.add(name)

    # Body parts decide before names: a shared name can mean another part (SF6's L_Knee is the shin; L_Toe is the ball
    # in RE4 but the toe tip in Wilds).
    roles = resolve(source, weighted), resolve(target)
    if all(roles):
        _suggest_roles(mapping, *roles, claim)
        mapping.families = (roles[0].family.label, roles[1].family.label)
    _suggest_exact(mapping, taken, claim)
    keys = {}
    for name in mapping.target:
        keys.setdefault(normalized_name(name), []).append(name)
    for row in mapping.rows:
        if row.mapped:
            continue
        candidates = [n for n in keys.get(normalized_name(row.source), ()) if n not in taken]
        if len(candidates) == 1:
            claim(row, candidates[0], NAME)
    _suggest_positions(mapping, taken, claim)
    return mapping


def _suggest_exact(mapping, taken, claim):
    for row in mapping.rows:
        if not row.mapped and row.source in mapping.target and row.source not in taken:
            claim(row, row.source, EXACT)


def _suggest_roles(mapping, source, target, claim):
    for bone, joint in role_pairs(source, target):
        row = mapping.row(bone)
        if row is not None and not row.mapped and joint in mapping.target:
            claim(row, joint, ROLE)


def _suggest_positions(mapping, taken, claim):
    tolerance = 0.02 * mapping.skeleton_size()
    children = {}
    for bone in mapping.target.values():
        children.setdefault(bone.parent, []).append(bone.name)

    def descendants(name):
        stack, result = list(children.get(name, ())), []
        while stack:
            result.append(stack.pop())
            stack.extend(children.get(result[-1], ()))
        return result

    for row in mapping.rows:
        bone = mapping.source[row.source]
        if row.mapped or bone.head is None:
            continue
        anchor = mapping.mapped_ancestor(row.source)
        pool = [n for n in (descendants(anchor) if anchor else mapping.target)
                if n not in taken and mapping.target[n].head is not None]
        if pool:
            distances = [np.linalg.norm(np.subtract(bone.head, mapping.target[n].head)) for n in pool]
            best = int(np.argmin(distances))
            if distances[best] <= tolerance:
                claim(row, pool[best], POSITION)


__all__ = ["ACTIONS", "ADD", "Bone", "BoneMapping", "DROP", "EXACT", "LOADED", "MANUAL", "MERGE", "MappingRow",
           "NAME", "POSITION", "ROLE", "normalized_name", "suggest"]
