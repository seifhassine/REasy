"""See docs/internals/motion-editor.md, Retargeting."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from functools import lru_cache

lazy import numpy as np

lazy from file_handlers.mesh.blender.mapping import Bone
lazy from file_handlers.mesh.blender.skeleton_roles import resolve, role_table, split_key

from .pose import Rig

SPREAD = 0.1  # child directions spreading this much (second singular value to the first) fix a joint's twist
NEAR = 5e-3   # metres: float32 rest positions give shorter offsets no reliable direction
SNAP = 1e-3   # a fit turning less than this from its prior is the prior, within the rest positions' precision
TIE = 0.25    # summed cosines within which quarter turns fit a joint's children alike
MIRRORED = math.radians(135)  # rest poses roll limbs less than this; mirrored axes differ by a half turn


def bones_of(names, parents, frames) -> list[Bone]:
    return [Bone(name, names[parent] if parent >= 0 else "", tuple(map(float, frame[:3, 3])))
            for name, parent, frame in zip(names, parents, frames)]


def _swing(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The smallest rotation turning unit vector ``a`` onto ``b``."""
    axis, cos = np.cross(a, b), float(a @ b)
    if cos < 1e-9 - 1.0:
        axis = np.cross(a, np.eye(3)[np.argmin(np.abs(a))])
        axis /= np.linalg.norm(axis)
        return 2.0 * np.outer(axis, axis) - np.eye(3)
    k = np.array([[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]], [-axis[1], axis[0], 0.0]])
    return np.eye(3) + k + k @ k / (1.0 + cos)


def _fit(prior: np.ndarray, local: np.ndarray, world: np.ndarray) -> np.ndarray:
    """``prior`` swung so unit ``local`` directions point along ``world`` ones where they run one way; directions
    spreading apart keep it, as their geometry differs between skeletons."""
    if len(local) > 1 and (spread := np.linalg.svd(world.T @ local, compute_uv=False))[1] > SPREAD * spread[0]:
        return prior
    mean, aim = prior @ local.sum(0), world.sum(0)
    if min(np.linalg.norm(mean), np.linalg.norm(aim)) <= SNAP:
        return prior
    fitted = _swing(mean / np.linalg.norm(mean), aim / np.linalg.norm(aim)) @ prior
    return prior if np.abs(fitted - prior).max() < SNAP else fitted


@lru_cache(maxsize=1)
def _quarter_turns() -> np.ndarray:
    turns = [np.eye(3)[list(order)] * np.array(signs)[:, None] for order in itertools.permutations(range(3))
             for signs in itertools.product((1.0, -1.0), repeat=3)]
    return np.array([turn for turn in turns if np.linalg.det(turn) > 0])


def _convention(local: np.ndarray, aim: np.ndarray, rest: np.ndarray, ancestor: np.ndarray | None) -> np.ndarray:
    turns = _quarter_turns()
    if not len(local):
        return ancestor if ancestor is not None else max(turns, key=lambda turn: float(np.sum(turn * rest)))
    scores = np.einsum("kj,tjl,kl->t", aim, turns, local)
    tied = turns[scores >= scores.max() - TIE]
    if np.linalg.norm(mean := local.sum(0)) > SNAP:
        # Ties differ in twist about the bone: the rest poses' bend at the joint does not count.
        bent = rest @ mean
        rest = _swing(bent / np.linalg.norm(bent), tied[0] @ mean / np.linalg.norm(mean)) @ rest
    if ancestor is not None and any(np.array_equal(turn, ancestor) for turn in tied) and \
            np.sum(ancestor * rest) > 1 + 2 * math.cos(MIRRORED):
        return ancestor
    return max(tied, key=lambda turn: float(np.sum(turn * rest)))


def _leg(found, bones: list[Bone]) -> float:
    top, end = role_table().leg
    if found is None:
        return 0.0
    by_name, tops, lengths = {bone.name: bone for bone in bones}, set(found.bones(top)), []
    for key in (key for key in found.roles if split_key(key)[0] == end):
        name, length = found.bones(key)[0], 0.0
        while name not in tops and by_name[name].parent in by_name:
            parent = by_name[name].parent
            length += float(np.linalg.norm(np.subtract(by_name[name].head, by_name[parent].head)))
            name = parent
        if name in tops:
            lengths.append(length)
    return float(np.mean(lengths)) if lengths else 0.0


@dataclass(frozen=True, slots=True, eq=False)
class Retarget:
    bones: np.ndarray
    turns: np.ndarray
    anchors: np.ndarray
    scale: float

    @classmethod
    def of(cls, rig: Rig, names, parents, rest: np.ndarray, pairs, engine: np.ndarray) -> Retarget:
        def lineage(bone):
            while (bone := parents[bone]) >= 0:
                yield bone

        def reach(origin, row):
            for child in children.get(row, ()):
                offset = target[child, :3, 3] - target[origin, :3, 3]
                aim = rest[driver[child], :3, 3] - rest[driver[origin], :3, 3]
                if min(np.linalg.norm(offset), np.linalg.norm(aim)) > NEAR:
                    yield upright[origin].T @ offset, aim
                else:
                    yield from reach(origin, child)

        target = rig.models(rig.base_locals).astype(np.float64)
        skeletons = bones_of(names, parents, rest), bones_of(rig.names, rig.parents, target)
        found = resolve(skeletons[0]), resolve(skeletons[1])
        parts = [{} if each is None else {bone: key for key, members in each.roles.items() for bone in members}
                 for each in found]
        depth = [sum(1 for _ in lineage(bone)) for bone in range(len(names))]
        index, rows = {name: bone for bone, name in enumerate(names)}, {name: row for row, name in enumerate(rig.names)}
        chosen = {}
        for bone_name, joint_name in pairs:
            if (bone := index.get(bone_name)) is not None and (row := rows.get(joint_name)) is not None:
                rank = (parts[0].get(bone_name, "") == parts[1].get(joint_name), depth[bone])
                if row not in chosen or rank > chosen[row][0]:
                    chosen[row] = rank, bone
        driver = np.full(len(rig.names), -1, np.int64)
        for row, (_rank, bone) in chosen.items():
            driver[row] = bone
        anchors, children = np.full(len(rig.names), -1, np.int64), {}
        for row in (row for level in rig.levels for row in level.tolist()):
            if (parent := rig.parents[row]) >= 0:
                anchors[row] = parent if driver[parent] >= 0 else anchors[parent]
            if driver[row] >= 0 and anchors[row] >= 0 and driver[anchors[row]] in set(lineage(driver[row])):
                children.setdefault(int(anchors[row]), []).append(row)
        upright = target[:, :3, :3] / np.linalg.norm(target[:, :3, :3], axis=1, keepdims=True)
        local_turns = rig.base_locals[:, :3, :3].astype(np.float64)
        local_turns /= np.linalg.norm(local_turns, axis=1, keepdims=True)
        aligned, turns = np.empty((len(rig.names), 3, 3)), np.tile(np.eye(3), (len(rig.names), 1, 1))
        for row in (row for level in rig.levels for row in level.tolist()):
            parent, bone = rig.parents[row], driver[row]
            carried = (aligned[parent] if parent >= 0 else np.eye(3)) @ local_turns[row]
            if bone < 0:
                aligned[row] = carried
                continue
            # A top joint's offsets can hold where the skeleton stands (cutscene bases): its rest pose decides.
            frame = rest[bone, :3, :3]
            offsets = np.reshape(list(reach(row, row)) if anchors[row] >= 0 else [], (-1, 2, 3))
            local, world = (offsets[:, side] / np.linalg.norm(offsets[:, side], axis=1, keepdims=True)
                            for side in (0, 1))
            if engine[bone]:
                turns[row] = _convention(local, world @ frame, frame.T @ carried,
                                         turns[anchors[row]] if anchors[row] >= 0 else None)
                aligned[row] = frame @ turns[row]
            else:
                aligned[row] = _fit(carried, local, world)
                turns[row] = frame.T @ aligned[row]
        legs = _leg(found[0], skeletons[0]), _leg(found[1], skeletons[1])
        return cls(driver, turns, anchors, legs[1] / legs[0] if min(legs) > 0 else 1.0)
