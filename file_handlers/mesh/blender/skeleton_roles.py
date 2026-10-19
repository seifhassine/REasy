from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from utils.app_paths import resource_path

ROLES_FILE = "resources/data/skeletons/roles.json"

MINIMUM_ROLES = 6


@dataclass(frozen=True)
class Family:
    name: str
    label: str
    roles: tuple          # ((role key, (compiled pattern, ...)), ...) in claiming order
    normalized: bool


@dataclass(frozen=True)
class RoleTable:
    families: tuple
    chains: frozenset
    proportional: frozenset
    anchors: dict
    fallbacks: dict


@dataclass(frozen=True)
class Resolution:
    family: Family
    roles: dict           # role key -> bone names, proximal first

    def bones(self, key):
        return self.roles.get(key, [])


def role_key(role, side=""):
    return f"{role}.{side}" if side else role


def split_key(key):
    role, _, side = key.partition(".")
    return role, side


def _family(data):
    roles = []
    for role, patterns in data["roles"].items():
        sided = any("{side}" in p for p in patterns)
        for side, token in (data["sides"].items() if sided else (("", ""),)):
            compiled = tuple(re.compile(p.replace("{side}", re.escape(token))) for p in patterns)
            roles.append((role_key(role, side), compiled))
    return Family(data["name"], data.get("label", data["name"]), tuple(roles), bool(data.get("normalized")))


def table_from_dict(data):
    return RoleTable(tuple(_family(f) for f in data["families"]), frozenset(data["chains"]),
                     frozenset(data["proportional"]), dict(data.get("anchors", {})), dict(data.get("fallbacks", {})))


@lru_cache(maxsize=1)
def role_table():
    with open(resource_path(ROLES_FILE, required=True), encoding="utf-8") as stream:
        return table_from_dict(json.load(stream))


def bare_name(name):
    return re.split(r"[:|]", name)[-1]


_RIG_TOKENS = {"def", "deform", "bip", "bip01", "bip001", "valvebiped", "jnt", "joint", "bn"}
_SIDES = {"l": "l", "left": "l", "r": "r", "right": "r"}
_SYNONYMS = {
    "hips": "hip", "pelvis": "hip",
    "upleg": "thigh", "upperleg": "thigh",
    "leg": "shin", "lowerleg": "shin", "calf": "shin", "knee": "shin",
    "arm": "upperarm",
    "lowerarm": "forearm", "elbow": "forearm",
    "clavicle": "shoulder", "collar": "shoulder",
    "toebase": "toe", "toes": "toe",
}


def normalized_name(name):
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", re.split(r"[:|]", name)[-1])
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if t and t not in _RIG_TOKENS]
    side = next((t for t in tokens if t in _SIDES), None) if len(tokens) > 1 else None
    if side is not None:
        tokens.remove(side)
    word = "".join(tokens)
    return f"{_SIDES.get(side, '')}|{_SYNONYMS.get(word, word)}"


def _natural(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _rank(patterns, form):
    return next((i for i, pattern in enumerate(patterns) if pattern.fullmatch(form)), None)


def resolve(bones, weighted=None, table=None):
    """Several families may contribute: some RE4 characters mix RE2 limbs with RE4 spine and head names. ``weighted``
    (name -> vertex count) prefers weighted bones where several match, such as Rigify deform bones next to controls."""
    table = table or role_table()
    forms = {False: [(b.name, bare_name(b.name)) for b in bones], True: [(b.name, normalized_name(b.name)) for b in bones]}
    scored = []
    for family in table.families:
        matches = {key: [(rank, name) for name, form in forms[family.normalized]
                         if (rank := _rank(patterns, form)) is not None]
                   for key, patterns in family.roles}
        matched = [key for key, found in matches.items() if found]
        scored.append((len(matched), sum(bool(split_key(key)[1]) for key in matched), family, matches))
    scored.sort(key=lambda item: -item[0])
    score, limbs, primary, _matches = scored[0]
    if score < MINIMUM_ROLES or not limbs:
        return None
    return Resolution(primary, _claim([item[3] for item in scored], bones, weighted or {}, table))


def _claim(families, bones, weighted, table):
    by_name = {b.name: b for b in bones}

    def ancestors(name):
        seen, parent = {name}, by_name[name].parent
        while parent in by_name and parent not in seen:
            seen.add(parent)
            yield parent
            parent = by_name[parent].parent

    depth = {name: sum(1 for _ in ancestors(name)) for name in by_name}
    taken, roles, ranks = set(), {}, {}
    for position, matches in enumerate(families):
        for key, candidates in matches.items():
            role, side = split_key(key)
            found = [((position, rank), name) for rank, name in candidates if name not in taken]
            if not found or (key in roles and role not in table.chains):
                continue
            if role in table.chains:
                if key in roles:
                    # Another family only extends a chain along its hierarchy (RE4's Spine_2 under RE2's spine_1), never
                    # with unrelated bones sharing a pattern.
                    lineage = set(roles[key])
                    found = [(rank, name) for rank, name in found
                             if lineage & set(ancestors(name)) or any(name in set(ancestors(m)) for m in lineage)]
                    if not found:
                        continue
                ranks.update((name, rank) for rank, name in found)
                roles[key] = _chain(roles.get(key, []) + [name for _rank, name in found], ranks, by_name, ancestors,
                                    _anchor(roles, table.anchors.get(role, ""), side, by_name))
                taken.update(roles[key])
            else:
                best = min(rank for rank, _name in found)
                first = [name for rank, name in found if rank == best]
                roles[key] = [min(first, key=lambda n: (weighted.get(n, 0) <= 0, depth[n], _natural(n)))]
                taken.add(roles[key][0])
    return roles


def _anchor(roles, anchor, side, by_name):
    found = roles.get(role_key(anchor, side)) or roles.get(anchor)
    return by_name[found[-1]].head if found else None


def _chain(members, ranks, by_name, ancestors, origin):
    inside = set(members)

    def order(name):
        head = by_name[name].head
        reach = float(np.linalg.norm(np.subtract(head, origin))) if head is not None and origin is not None else 0.0
        return sum(1 for a in ancestors(name) if a in inside), ranks[name], reach, _natural(name)

    return sorted(members, key=order)


def role_pairs(source, target, table=None):
    """Several sources may share a target, such as a knee helper and the shin on a rig without knees."""
    table = table or role_table()
    pairs = []
    for key, bones in source.roles.items():
        role, side = split_key(key)
        found = target.bones(key)
        if found and role in table.chains:
            if role in table.proportional:
                scale = (len(found) - 1) / (len(bones) - 1) if len(bones) > 1 else 0.0
                pairs.extend((bone, found[round(i * scale)]) for i, bone in enumerate(bones))
                continue
            offset = len(found) - len(bones)
            for i, bone in enumerate(bones):
                if i + offset >= 0:
                    pairs.append((bone, found[i + offset]))
                elif fallback := _fallback(role, side, target, table):
                    pairs.append((bone, fallback))
        elif found:
            pairs.append((bones[0], found[0]))
        elif fallback := _fallback(role, side, target, table):
            pairs.extend((bone, fallback) for bone in bones)
    return pairs


def _fallback(role, side, target, table):
    for other in table.fallbacks.get(role, ()):
        found = target.bones(role_key(other, side)) or target.bones(other)
        if found:
            return found[-1]
    return ""


__all__ = ["Family", "Resolution", "RoleTable", "bare_name", "normalized_name", "resolve", "role_key", "role_pairs",
           "role_table", "table_from_dict"]
