"""See docs/internals/maba-format.md, Sampling."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import accumulate

import numpy as np

from file_handlers.engine_math import transform_product
from file_handlers.mmtr.engine_buffers import engine_inverse
from file_handlers.motion.banks import entry_mirrored, entry_motion, read_list
from file_handlers.motion.layers import MotionJoints, MotionLayer, joint_map_mirror
from file_handlers.motion.pose import MotionSampler, Rig
from file_handlers.skeleton import read_skeleton
from utils.hash_util import murmur3_hash_utf16le

_WORD = re.compile(r"(.*?)(?:>(\d+)|(@-1))?")
_VALUES = {"position": lambda local: local[:3, 3], "x_axis": lambda local: local[:3, 0],
           "z_axis": lambda local: local[:3, 2], "xz_direction": lambda local: _unit(local[[0, 2], 3])}


def _unit(vector: np.ndarray) -> np.ndarray:
    length = np.sqrt(np.sum(vector * vector, dtype=np.float32))
    return vector / length if length else vector


def character_rig(skeleton: bytes, joint_map: bytes | None) -> tuple[Rig, tuple | None]:
    joints = read_skeleton(skeleton).joints
    rows = {id(joint): row for row, joint in enumerate(joints)}
    return Rig.of([murmur3_hash_utf16le(joint.name) for joint in joints],
                  [rows.get(id(joint.parent), -1) for joint in joints], [joint.position for joint in joints],
                  [joint.rotation for joint in joints], [joint.scale for joint in joints],
                  [joint.name for joint in joints], [rows.get(id(joint.mirror), -1) for joint in joints]), \
        None if joint_map is None else joint_map_mirror(joint_map)


class MotionPose:
    def __init__(self, motion, mirrored: bool, rig: Rig, table: tuple | None):
        self.end, self.rig, self.mirrored = float(motion.end_frame), rig, mirrored
        self.sampler = MotionSampler(motion)
        self.joints = MotionJoints.of(rig, (), self.sampler.hashes, 0, table)
        self.binding = self.joints.bind(self.sampler.hashes)
        self.weights = np.repeat(self.sampler.weights[self.binding[1], None], 3, axis=1)
        self.rows = {name: row for row, name in enumerate(rig.names)}
        self._models: dict[float, np.ndarray] = {}

    def model(self, joint: str, frame: float) -> np.ndarray | None:
        if (row := self.rows.get(joint)) is None:
            return None
        if frame not in self._models:
            pose = self.joints.layer_pose(self.sampler.sample(frame), self.binding, self.weights)
            if self.mirrored:
                self.joints.mirror(pose)
            count = len(self.rig.hashes)
            out = self.joints.stacked([(MotionLayer(), lambda _below: pose)])
            self._models[frame] = self.rig.models(self.rig.locals(tuple(values[:count] for values in out.values),
                                                                  np.arange(count)))
        return self._models[frame][row]

    def value(self, kind: str, name: str, frame: float) -> np.ndarray | None:
        words = name.split(" ")
        if len(words) != 3:
            return None
        joint, parent = (_WORD.fullmatch(word) for word in words[1:])
        if parent.group(2) or parent.group(3):
            return None
        at = self.end if joint.group(3) else min(frame + int(joint.group(2)), self.end) if joint.group(2) else frame
        moved, parent = self.model(joint.group(1), at), self.model(words[2], frame)
        if moved is None or parent is None:
            return None
        return _VALUES[kind](transform_product(engine_inverse(parent), moved))


@dataclass(slots=True)
class Sampled:
    values: dict[int, np.ndarray] = field(default_factory=dict)
    count: int = 0
    unsampled: set[str] = field(default_factory=set)
    missing: set[int] = field(default_factory=set)


def sample(read, skeleton: str, joint_map: str, sampled: dict[str, str], work: list) -> Sampled:
    if (skeleton_data := read(skeleton)) is None:
        raise ValueError(f"cannot read the skeleton {skeleton}")
    joint_map_data = read(joint_map) if joint_map else None
    if joint_map and joint_map_data is None:
        raise ValueError(f"cannot read the joint map {joint_map}")
    rig, table = character_rig(skeleton_data, joint_map_data)
    lists, found, made = {}, Sampled(), {}
    for token, keys, path, motion_id, frames, values, rows, chosen in work:
        if (path, motion_id) not in made:
            made[path, motion_id] = _pose(read, path, motion_id, lists, rig, table)
        if (pose := made[path, motion_id]) is None:
            found.missing.add(token)
            continue
        values, starts = values.copy(), [0, *accumulate(size for _name, size in keys)]
        for index in chosen:
            name, size = keys[index]
            kind = name.split(" ")[0]
            for row in rows:
                if kind not in sampled or (value := pose.value(sampled[kind], name, float(frames[row]))) is None or \
                        len(value) != size:
                    found.unsampled.add(kind)
                    break
                values[row, starts[index]:starts[index] + size] = value
                found.count += size
        found.values[token] = values
    return found


def _pose(read, path: str | None, motion_id: int, lists: dict, rig: Rig, table: tuple | None) -> MotionPose | None:
    if path is None:
        return None
    if path not in lists:
        lists[path] = read_list(read, path), {}
    motion_list, held = lists[path]
    entry = None if motion_list is None else next(
        (entry for entry in motion_list.entries if entry.motion_id == motion_id), None)
    motion = None if entry is None else entry_motion(read, motion_list, entry, held)
    return None if motion is None else MotionPose(motion, entry_mirrored(read, motion_list, entry, held), rig, table)
