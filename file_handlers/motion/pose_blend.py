"""See docs/internals/motion-editor.md, Pose blends."""

from __future__ import annotations

import struct
from dataclasses import dataclass

lazy import numpy as np

from .decode import _flag
from .errors import MotionFormatError
from .rules import display_names, joint_pose_layout, pose_blend_head


@dataclass(frozen=True, slots=True)
class PoseBlend:
    parameters: bool
    controls: tuple[int, ...]
    axis: int
    coefficient: float
    direct: bool
    limit: int
    bank: int
    motion: int
    poses: int

    @classmethod
    def of(cls, blob: bytes) -> PoseBlend:
        head = pose_blend_head()
        fields = head.decode(blob, 0)
        controls = () if fields["mode"] else struct.unpack_from(f"<{fields['count']}I", blob, head.size)
        return cls(bool(fields["mode"]), controls, fields["axis"], fields["coefficient"], bool(fields["flags"] & 1),
                   fields["limit"], fields["bank"], fields["motion"], fields["poses"])

    def frames(self, values: np.ndarray, limit: int | None = None) -> list[tuple[float, float]]:
        """Heaviest first by a stable sort when the blend caps them, as the engine sorts."""
        weights = values[:, self.axis] * np.float32(self.coefficient)
        if not self.direct:
            weights = np.clip(weights, 0, 1)
        frames = [(float(index), weight) for index, weight in enumerate(weights.tolist())]
        return sorted(frames, key=lambda frame: -frame[1]) if (self.limit if limit is None else limit) >= 0 else frames


@dataclass(frozen=True, slots=True)
class JointPoses:
    hashes: tuple[int, ...]
    translations: np.ndarray
    quaternions: np.ndarray
    vectors: np.ndarray | None
    scales: np.ndarray
    held: np.ndarray
    adds: str | None

    @classmethod
    def of(cls, blob: bytes, offset: int) -> JointPoses:
        records, rotations, tables = joint_pose_layout()
        data = records["data"].decode(blob, offset)
        rotation = display_names("JointPoseRotation")[data["rotation"]]
        calc = display_names("JointPoseCalc")[data["calc"]]
        count, frames = data["joints"], data["frames"]
        bits = [_flag("JointPoseFlags", name) for name in ("Trans", "Rotation", "Scale")]
        translations, vectors, scales = (np.zeros((frames, count, 3), np.float32) for _ in range(3))
        quaternions = np.tile(np.array([0, 0, 0, 1], np.float32), (frames, count, 1))
        held = np.zeros((frames, count, 3), bool)
        if calc in tables:
            table = records["table"].decode(blob, data["poses"])
            slots = (table["translations"], table["rotations"], table["scales"])
            if table["floats"] or any(size not in (0, count) for size in slots) or table["stride"] < 3 * sum(slots):
                raise MotionFormatError(f"A pose table of {count} joints holds slots {table}")
            hashes = struct.unpack_from(f"<{count}I", blob, table["hashes"])
            flags = np.frombuffer(blob, np.uint8, count, table["flags"])
            block = np.frombuffer(blob, np.float32, frames * table["stride"], table["values"]).reshape(frames, -1)
            at = 0
            for component, (size, target) in enumerate(zip(slots, (translations, vectors, scales))):
                target[:, :size] = block[:, at:at + 3 * size].reshape(frames, size, 3)
                held[:, :size, component] = (flags[:size] & bits[component]) != 0
                at += 3 * size
            return cls(tuple(hashes), translations, quaternions, vectors, scales, held, calc)
        width, quaternion_at, vector_at = rotations[rotation]
        hashes, read = [], {}
        frame_record = records["frame"]
        for joint, pointer in enumerate(struct.unpack_from(f"<{count}Q", blob, data["poses"])):
            pose = records["pose"].decode(blob, pointer)
            hashes.append(pose["name_hash"])
            for frame, at in enumerate(struct.unpack_from(f"<{frames}Q", blob, pose["frames"])):
                if at not in read:
                    flags = frame_record.decode(blob, at)["flags"]
                    if flags & ~(bits[0] | bits[1] | bits[2]):
                        raise MotionFormatError(f"Pose frame flags 0x{flags:X} hold unknown bits")
                    at_value, values = at + frame_record.size, [None, None, None]
                    for component, size in enumerate((3, width, 3)):
                        if flags & bits[component]:
                            values[component] = struct.unpack_from(f"<{size}f", blob, at_value)
                            at_value += 4 * size
                    read[at] = values
                translation, turn, scale = read[at]
                if translation is not None:
                    translations[frame, joint], held[frame, joint, 0] = translation, True
                if turn is not None:
                    held[frame, joint, 1] = True
                    if quaternion_at is not None:
                        quaternions[frame, joint] = turn[quaternion_at:quaternion_at + 4]
                    if vector_at is not None:
                        vectors[frame, joint] = turn[vector_at:vector_at + 3]
                if scale is not None:
                    scales[frame, joint], held[frame, joint, 2] = np.float32(scale) - np.float32(1), True
        return cls(tuple(hashes), translations, quaternions, vectors if vector_at is not None else None, scales, held,
                   None if vector_at is None else rotation)
