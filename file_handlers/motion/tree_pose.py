"""See docs/internals/mottree-runtime.md, Pose blending."""

from __future__ import annotations

lazy import numpy as np

from file_handlers.engine_math import crt

from .decode import TRANSFORMS
from .model import Motion
from .pose import Rig, _hamilton, _slerp

_ONE, F_EPSILON, _FULL = np.float32(1), np.float32(1.1920929e-07), np.float32(0.99999988)


class TreePose:
    __slots__ = ("values", "weights")

    def __init__(self, count: int):
        self.values = [np.zeros((count, 3), np.float32), np.tile(np.array([0, 0, 0, 1], np.float32), (count, 1)),
                       np.ones((count, 3), np.float32)]
        self.weights = np.zeros((count, 3), np.float32)

    def clear(self) -> None:
        self.weights[:] = 0

    def copy_from(self, other: TreePose) -> None:
        for mine, theirs in zip(self.values, other.values):
            mine[:] = theirs
        self.weights[:] = other.weights


class PoseFrames:
    def __init__(self, rows: np.ndarray, values: list, held: list, vectors: np.ndarray | None = None,
                 masks: np.ndarray | None = None):
        self.rows, self.values, self.held, self.vectors = rows, values, held, vectors
        self.count = len(values[0])
        self.masks = np.ones((len(rows), 3), bool) if masks is None else masks

    @classmethod
    def of_motion(cls, motion: Motion, hashes) -> PoseFrames:
        rows = {value: row for row, value in enumerate(hashes)}
        poses = [(rows[pose.name_hash], pose.frames) for pose in motion.joint_poses or ()
                 if pose.name_hash in rows and pose.frames]
        count = min((len(frames) for _row, frames in poses), default=0)
        values = [np.zeros((count, len(poses), width), np.float32) for width in (3, 4, 3)]
        held = [np.zeros((count, len(poses)), bool) for _ in range(3)]
        for joint, (_row, frames) in enumerate(poses):
            for index, pose in enumerate(frames[:count]):
                for component, name in enumerate(TRANSFORMS):
                    if (value := getattr(pose, name)) is not None:
                        values[component][index, joint], held[component][index, joint] = value, True
        values[2] = np.where(held[2][..., None], values[2] - _ONE, 0).astype(np.float32)
        return cls(np.array([row for row, _frames in poses], np.int64), values, held)

    def add(self, pose: TreePose, frames: list[tuple[float, float]], limit: int, vector: str | None = None) -> None:
        count = len(self.rows)
        moved = (np.zeros((count, 3), np.float32), np.tile(np.array([0, 0, 0, 1], np.float32), (count, 1))
                 if vector is None else np.zeros((count, 3), np.float32), np.zeros((count, 3), np.float32))
        changed = np.zeros((count, 3), bool)
        for position, (frame, weight) in enumerate(frames):
            if 0 <= limit and (limit <= position or weight == 0):
                break
            index = int(frame + 0.5)
            if frame < 0 or weight == 0 or index >= self.count:
                continue
            weight = np.float32(weight)
            translated, turned, scaled = (held[index] & self.masks[:, component]
                                          for component, held in enumerate(self.held))
            moved[0][translated] += weight * self.values[0][index][translated]
            if vector is None:
                moved[1][turned] = _turned(moved[1][turned], self.values[1][index][turned],
                                           np.full(int(turned.sum()), weight, np.float32))
            else:
                moved[1][turned] += weight * self.vectors[index][turned]
            moved[2][scaled] += weight * self.values[2][index][scaled]
            changed |= np.stack([translated, turned, scaled], axis=1)
        for component in range(3):
            on = changed[:, component]
            rows = self.rows[on]
            if component != 1:
                pose.values[component][rows] = pose.values[component][rows] + moved[component][on]
            elif vector != "ExponentialMap":
                pose.values[1][rows] = _hamilton(pose.values[1][rows], moved[1][on] if vector is None
                                                 else _euler_turns(moved[1][on]))
            elif len(rows):
                pose.values[1][rows] = [_exponential_added(rotation, added)
                                        for rotation, added in zip(pose.values[1][rows], moved[1][on])]
            pose.weights[rows, component] = _ONE


def _exponential_added(rotation: np.ndarray, added: np.ndarray) -> tuple:
    f, epsilon = np.float32, np.float32(1e-5)
    x, y, z, w = (f(value) for value in (rotation if rotation[3] >= 0 else -rotation))
    if abs(w) < 1:
        angle = f(crt("acosf")(float(w)))
        sine = f(crt("sinf")(float(angle)))
        if abs(sine) >= epsilon:
            x, y, z = x * (angle / sine), y * (angle / sine), z * (angle / sine)
    x, y, z = x + added[0], y + added[1], z + added[2]
    length = f(np.sqrt((x * x + y * y) + z * z))
    sine = f(crt("sinf")(float(length)))
    if abs(sine) >= epsilon:
        x, y, z = x * (sine / length), y * (sine / length), z * (sine / length)
    return x, y, z, f(crt("cosf")(float(length)))


def _euler_turns(angles: np.ndarray) -> np.ndarray:
    """Euler angles (x, y, z per row) as the turn z(y(x)), the order MH Wilds' table-layout frames take
    (docs/internals/motion-editor.md, Pose blends)."""
    sines, cosines = np.sin(angles * np.float32(0.5)), np.cos(angles * np.float32(0.5))
    zero = np.zeros(len(angles), np.float32)
    turns = [np.stack([sines[:, axis] if index == axis else zero for index in range(3)] + [cosines[:, axis]], axis=1)
             for axis in range(3)]
    return _hamilton(_hamilton(turns[2], turns[1]), turns[0])


def _normalized(quaternions: np.ndarray) -> np.ndarray:
    """Times 1 / sqrt(((x² + y²) + z²) + w²), summed in the engine's order."""
    square = quaternions * quaternions
    return quaternions * (_ONE / np.sqrt(((square[:, 0] + square[:, 1]) + square[:, 2]) + square[:, 3]))[:, None]


def _nlerp(a: np.ndarray, b: np.ndarray, t) -> np.ndarray:
    dot = ((a[:, 1] * b[:, 1] + a[:, 0] * b[:, 0]) + a[:, 2] * b[:, 2]) + a[:, 3] * b[:, 3]
    b = np.where((dot < 0)[:, None], -b, b)
    return _normalized((b - a) * t + a)


def _polar(a: np.ndarray, b: np.ndarray, t) -> np.ndarray:
    radii = np.sqrt(a[:, 0] * a[:, 0] + a[:, 2] * a[:, 2]), np.sqrt(b[:, 0] * b[:, 0] + b[:, 2] * b[:, 2])
    out = (b - a) * t + a
    turn = (radii[0] > F_EPSILON) & (radii[1] > F_EPSILON)
    if turn.any():
        ax, az = a[turn, 0] / radii[0][turn], a[turn, 2] / radii[0][turn]
        bx, bz = b[turn, 0] / radii[1][turn], b[turn, 2] / radii[1][turn]
        angle = np.arctan2(az * bx - ax * bz, ax * bx + az * bz) * t
        radius = (radii[1][turn] - radii[0][turn]) * t + radii[0][turn]
        cosine, sine = np.cos(angle), np.sin(angle)
        out[turn, 0] = (ax * cosine + az * sine) * radius
        out[turn, 2] = (az * cosine - ax * sine) * radius
    return out.astype(np.float32)


def _turned(base: np.ndarray, added: np.ndarray, weight: np.ndarray) -> np.ndarray:
    scaled = added * weight[:, None]
    scaled[:, 3] += np.clip(_ONE - weight, 0, 1)
    return _hamilton(base, _normalized(scaled))


def blend_normal(dst: TreePose, src: TreePose, rate, rows: np.ndarray, slerped: np.ndarray,
                 polar: bool = False) -> None:
    for component in range(3):
        mine, theirs = dst.weights[rows, component] != 0, src.weights[rows, component] != 0
        both, only = rows[mine & theirs], rows[theirs & ~mine]
        target, value = dst.values[component], src.values[component]
        if len(both):
            if component == 1:
                turned = slerped[both]
                target[both[~turned]] = _nlerp(target[both[~turned]], value[both[~turned]], rate)
                if turned.any():
                    rate_rows = np.full((int(turned.sum()), 1), rate, np.float32)
                    target[both[turned]] = _slerp(target[both[turned]], value[both[turned]], rate_rows)
            elif component == 0 and polar and slerped[both].any():
                root = both[slerped[both]]
                target[root] = _polar(target[root], value[root], rate)
                rest = both[~slerped[both]]
                target[rest] = (value[rest] - target[rest]) * rate + target[rest]
            else:
                target[both] = (value[both] - target[both]) * rate + target[both]
        target[only] = value[only]
        dst.weights[rows[mine | theirs], component] = 1


def blend_add(dst: TreePose, src: TreePose, rate, rows: np.ndarray, rest: list[np.ndarray]) -> None:
    for component in range(3):
        weight = src.weights[rows, component]
        on = rows[weight != 0]
        if not len(on):
            continue
        amount = src.weights[on, component] * rate
        unset = dst.weights[on, component] == 0
        base = np.where(unset[:, None], rest[component][on], dst.values[component][on])
        added = src.values[component][on]
        dst.values[component][on] = (base + amount[:, None] * added if component == 0 else
                                     _turned(base, added, amount) if component == 1 else
                                     base + (added - _ONE) * amount[:, None])
        dst.weights[on[~unset] if component == 2 else on, component] = 1


def layer_base(dst: TreePose, src: TreePose, rest: list[np.ndarray], interpolated: np.ndarray) -> None:
    for component in range(3):
        weight = src.weights[:, component]
        full, part = weight >= _FULL, (weight > F_EPSILON) & (weight < _FULL)
        target, value, base = dst.values[component], src.values[component], rest[component]
        target[:] = np.where((full | part & ~interpolated)[:, None], value, base)
        dst.weights[:, component] = np.where(full, _ONE, np.where(part, weight, 0))
        blended = np.flatnonzero(part & interpolated)
        if len(blended):
            amount = weight[blended, None]
            target[blended] = _slerp(base[blended], value[blended], amount) if component == 1 else \
                (value[blended] - base[blended]) * amount + base[blended]


def blend_layer(dst: TreePose, src: TreePose, rate, slerped: np.ndarray, interpolated: np.ndarray) -> None:
    """Components in the engine's order: rotation, translation, scale."""
    for component in (1, 0, 2):
        t = src.weights[:, component] * np.float32(rate)
        rows = np.flatnonzero(t != 0)
        t, held = t[rows], dst.weights[rows, component]
        copy = (t == _ONE) | ~interpolated[rows] & (dst.weights[rows, 0 if component == 2 else component] == 0)
        target, value = dst.values[component], src.values[component]
        mixed, amount = rows[~copy], t[~copy, None]
        if component == 1:
            turned = slerped[mixed]
            target[mixed[~turned]] = _nlerp(target[mixed[~turned]], value[mixed[~turned]], amount[~turned])
            if turned.any():
                target[mixed[turned]] = _slerp(target[mixed[turned]], value[mixed[turned]], amount[turned])
        elif len(mixed):
            target[mixed] = (value[mixed] - target[mixed]) * amount + target[mixed]
        target[rows[copy]] = value[rows[copy]]
        dst.weights[rows, component] = np.where(copy, t, held + (_ONE - held) * t if component == 2 else
                                                (_ONE - t) * held + t)


def blend_layer_alpha(dst: TreePose, src: TreePose, rate, rows: np.ndarray) -> None:
    for component in range(3):
        alpha = src.weights[rows, component] * rate
        on = alpha != 0
        rows_on, alpha = rows[on], alpha[on]
        if not len(rows_on):
            continue
        full = alpha >= 1
        total = np.where(full, _ONE, (_ONE - alpha) * dst.weights[rows_on, component] + alpha)
        share = np.where(full, _ONE, alpha / total)
        target, value = dst.values[component], src.values[component]
        copy = share >= 1
        blended = rows_on[~copy]
        if len(blended):
            target[blended] = (_nlerp(target[blended], value[blended], share[~copy][:, None]) if component == 1 else
                               (value[blended] - target[blended]) * share[~copy][:, None] + target[blended])
        target[rows_on[copy]] = value[rows_on[copy]]
        dst.weights[rows_on, component] = total


def blend_interpolation(dst: TreePose, src: TreePose, rate, rows: np.ndarray, slerped: np.ndarray) -> None:
    for component in range(3):
        theirs = src.weights[rows, component]
        mine = dst.weights[rows, component]
        fading = rows[(theirs == 0) & (mine != 0)]
        dst.weights[fading, component] = (_ONE - rate) * dst.weights[fading, component]
        both, only = rows[(theirs != 0) & (mine != 0)], rows[(theirs != 0) & (mine == 0)]
        target, value = dst.values[component], src.values[component]
        if len(both):
            if component == 1:
                turned = slerped[both]
                target[both[~turned]] = _nlerp(target[both[~turned]], value[both[~turned]], rate)
                if turned.any():
                    target[both[turned]] = _slerp(target[both[turned]], value[both[turned]],
                                                  np.full((int(turned.sum()), 1), rate, np.float32))
            else:
                target[both] = (value[both] - target[both]) * rate + target[both]
            dst.weights[both, component] = (src.weights[both, component] - dst.weights[both, component]) * rate \
                + dst.weights[both, component]
        target[only] = value[only]
        dst.weights[only, component] = src.weights[only, component] * rate


def mirror_rows(hashes, rig: Rig, table: tuple[dict, tuple[int, int]] | None) -> tuple:
    at = {value: row for row, value in enumerate(hashes)}
    alone, first, second = [], [], []
    for index, other in enumerate(rig.symmetry):
        if index != rig.root and 0 <= other and rig.hashes[index] in at and rig.hashes[other] in at:
            if other == index:
                alone.append(index)
            elif index < other:
                first.append(index)
                second.append(other)
    rows = [np.array([at[rig.hashes[index]] for index in found], np.int64) for found in (alone, first, second)]
    flags = None
    if table is not None:
        joints, default = table
        flags = np.zeros((len(hashes), 2), np.uint8)
        for found, side in ((alone, rows[0]), (first, rows[1]), (first, rows[2])):
            flags[side] = np.array([joints.get(rig.hashes[index], default) for index in found], np.uint8).reshape(-1, 2)
    return at.get(rig.hashes[rig.root], -1) if rig.root >= 0 else -1, *rows, flags


def mirror(pose: TreePose, root: int, alone: np.ndarray, first: np.ndarray, second: np.ndarray,
           flags: np.ndarray | None, bases: np.ndarray) -> None:
    move, turn = pose.values[0], pose.values[1]
    if root >= 0:
        turn[root, 1:3] *= -1
        move[root, 0] *= -1
    if flags is None:
        rows = np.concatenate([alone, first, second])
        turn[rows, 1:3] *= -1
        move[rows, 0] *= -1
        for array in (*pose.values, pose.weights):
            array[first], array[second] = array[second], array[first]
        return
    rows, axes = np.concatenate([alone, first, second]), np.array([1, 2, 4], np.uint8)
    for component, values in enumerate((move, turn)):
        bits = flags[:, component]
        on = rows[bits[rows] & 8 != 0]
        if component:
            values[on] = _hamilton(bases[on] * np.float32([-1, -1, -1, 1]), values[on])
        values[on, :3] *= np.where(bits[on, None] & axes, np.float32(-1), np.float32(1))
        swapped = bits[first] & 8 != 0
        a, b = first[swapped], second[swapped]
        values[a], values[b] = values[b], values[a]
        pose.weights[a, component], pose.weights[b, component] = pose.weights[b, component], pose.weights[a, component]
        if component:
            values[on] = _hamilton(bases[on], values[on])
