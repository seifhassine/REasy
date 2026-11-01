"""See docs/internals/mot-format.md, Evaluation."""

from __future__ import annotations

from dataclasses import dataclass

lazy import numpy as np

from file_handlers.engine_math import make_trs_matrix, transform_product
from utils.hash_util import murmur3_hash_utf16le

from .decode import TRANSFORMS
from .keys import Curve, GpuCurves
from .model import Motion, Skeleton, Track
from .rules import encoding, mot_profile

_WIDTHS = (3, 4, 3)


class MotionSampler:
    def __init__(self, motion: Motion, previous: MotionSampler | None = None):
        """``previous`` lends the curves of the tracks it shares, as after an edit of the motion."""
        self.motion = motion
        family = mot_profile(motion.version).keys
        slots: dict[int, int] = {}
        lent = {} if previous is None else previous._made
        self._made: dict[int, tuple[Track, Curve]] = {}

        def slot(name_hash: int) -> int:
            return slots.setdefault(name_hash, len(slots))

        def curve(track: Track) -> Curve:
            held = lent.get(id(track))
            made = held[1] if held is not None and held[0] is track else Curve(
                track, encoding(family, track.kind, track.encoding))
            self._made[id(track)] = (track, made)
            return made

        self.curves = [(slot(channel.name_hash), component, curve(track)) for channel in motion.channels
                       for component, name in enumerate(TRANSFORMS) if (track := getattr(channel, name)) is not None]
        gpu = motion.gpu
        self.gpu = None if gpu is None else (GpuCurves(gpu), np.array([slot(value) for value in gpu.joint_hashes],
                                                                       np.int64))
        self.poses = [(slot(pose.name_hash), pose.frames) for pose in motion.joint_poses or () if pose.frames]
        self.hashes = tuple(slots)
        self.properties = {prop.name_hash: curve(prop.track) for prop in motion.properties}

    def sample(self, frame: float) -> tuple[list[np.ndarray], list[np.ndarray]]:
        """Per component (translation, rotation, scale): each joint slot's value, and whether the motion keys it."""
        count = len(self.hashes)
        values = [np.zeros((count, width), np.float32) for width in _WIDTHS]
        keyed = [np.zeros(count, bool) for _ in _WIDTHS]
        for slot, component, curve in self.curves:
            values[component][slot], keyed[component][slot] = curve.sample(frame), True
        if self.gpu is not None:
            curves, slots = self.gpu
            values[0][slots], values[1][slots] = curves.sample(frame - self.motion.gpu.start_frame)
            keyed[0][slots] = keyed[1][slots] = True
        for slot, frames in self.poses:
            pose = frames[min(max(int(frame), 0), len(frames) - 1)]
            for component, name in enumerate(TRANSFORMS):
                if (value := getattr(pose, name)) is not None:
                    values[component][slot], keyed[component][slot] = value, True
        return values, keyed

    def property_values(self, frame: float) -> dict[int, float]:
        return {name_hash: float(curve.sample(frame)[0]) for name_hash, curve in self.properties.items()}


def _locals(translations: np.ndarray, rotations: np.ndarray, scales: np.ndarray) -> np.ndarray:
    return np.array([make_trs_matrix(t, r, s) for t, r, s in zip(translations.tolist(), rotations.tolist(),
                                                                 scales.tolist())], np.float32).reshape(-1, 4, 4)


@dataclass(frozen=True, slots=True, eq=False)
class Rig:
    """Joints at their base pose: name hashes, parents (-1: none), local translations, rotations (x, y, z, w) and
    scales, and their local matrices; ``levels`` group the joints by depth, roots first."""
    hashes: tuple[int, ...]
    parents: np.ndarray
    base: tuple[np.ndarray, np.ndarray, np.ndarray]
    levels: tuple[np.ndarray, ...]
    base_locals: np.ndarray
    names: tuple[str, ...] = ()

    @classmethod
    def of(cls, hashes, parents, translations, rotations, scales, names=()) -> Rig:
        count = len(hashes)
        parents = np.array([parent if 0 <= parent < count else -1 for parent in parents], np.int64)
        depths = np.full(count, -1, np.int64)
        for joint in range(count):
            chain, node = [], joint
            while node >= 0 and depths[node] < 0 and node not in chain:
                chain.append(node)
                node = parents[node]
            if node in chain:
                parents[chain[-1]] = node = -1
            depth = depths[node] + 1 if node >= 0 else 0
            for member in reversed(chain):
                depths[member], depth = depth, depth + 1
        levels = tuple(np.flatnonzero(depths == depth) for depth in range(int(depths.max(initial=-1)) + 1))
        base = (np.asarray(translations, np.float32).reshape(count, 3),
                np.asarray(rotations, np.float32).reshape(count, 4), np.asarray(scales, np.float32).reshape(count, 3))
        return cls(tuple(int(value) for value in hashes), parents, base, levels, _locals(*base), tuple(names))

    @classmethod
    def of_joints(cls, joints) -> Rig:
        return cls.of(joints.hashes, joints.parents, joints.positions, joints.rotations, joints.scales, joints.names)

    @classmethod
    def of_skeleton(cls, *skeletons: Skeleton) -> Rig:
        """The joints of ``skeletons`` by name, each as the first skeleton naming it has it."""
        named = {}
        for skeleton in skeletons:
            for joint in skeleton.joints:
                named.setdefault(joint.name, joint)
        joints, index = list(named.values()), {name: row for row, name in enumerate(named)}
        return cls.of([murmur3_hash_utf16le(name) for name in named],
                      [index.get(getattr(joint.parent, "name", None), -1) for joint in joints],
                      [joint.translation for joint in joints], [joint.rotation for joint in joints],
                      np.ones((len(joints), 3), np.float32), list(named))

    def bind(self, hashes: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
        """The rows of this rig's joints a sampler's slots (by ``hashes``) key, and those slots."""
        slots = {value: slot for slot, value in enumerate(hashes)}
        pairs = [(row, slots[value]) for row, value in enumerate(self.hashes) if value in slots]
        return np.array([row for row, _ in pairs], np.int64), np.array([slot for _, slot in pairs], np.int64)

    def posed(self, sample: tuple[list[np.ndarray], list[np.ndarray]], binding: tuple[np.ndarray, np.ndarray],
              lock_root: bool = False) -> tuple[np.ndarray, ...]:
        values, keyed = sample
        rows, slots = binding
        posed = tuple(array.copy() for array in self.base)
        for component, target in enumerate(posed):
            mask = keyed[component][slots]
            target[rows[mask]] = values[component][slots[mask]]
        if lock_root and self.levels:
            posed[0][self.levels[0]] = self.base[0][self.levels[0]]
        return posed

    def locals(self, posed: tuple[np.ndarray, ...], rows: np.ndarray) -> np.ndarray:
        made = self.base_locals.copy()
        made[rows] = _locals(*(component[rows] for component in posed))
        return made

    def models(self, locals_: np.ndarray, held: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
        """4 x 4 model matrices (column vectors); ``held`` (rows, matrices) are kept as given."""
        models, kept = locals_.copy(), np.zeros(len(self.hashes), bool)
        if held is not None:
            models[held[0]], kept[held[0]] = held[1], True
        for level in self.levels[1:]:
            free = level[~kept[level]]
            models[free] = transform_product(models[self.parents[free]], locals_[free])
        return models
