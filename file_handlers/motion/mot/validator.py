from __future__ import annotations

import math
from collections import Counter

import numpy as np

from utils.hash_util import murmur3_hash

from ..errors import MotionValidationError
from ..profiles import MotionFormatProfile
from ..sequence.validator import SequenceV65Validator
from .model import (
    AppendPropertyType,
    JointMapExtraType,
    KeyTrack,
    Motion,
    TrackFamily,
)


class MotV65Validator:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot=65, mot_clip=27)
        self.profile = profile
        self.sequence_validator = SequenceV65Validator(profile)

    def validate(self, motion: Motion) -> None:
        self._string(motion.name, "MOT name")
        self._count(len(motion.animation_nodes), 0xFFFF, "animation node", "u16")
        self._count(len(motion.property_tracks), 0xFFFF, "property track", "u16")
        self._count(len(motion.sequences), 0xFF, "sequence", "u8")
        self._count(len(motion.sync_points), 0xFF, "sync grid", "u8")
        for value, label in (
            (motion.end_frame, "end_frame"),
            (motion.raw_start_frame, "raw_start_frame"),
            (motion.raw_end_frame, "raw_end_frame"),
        ):
            if not math.isfinite(value) or value < 0 or value != math.floor(value):
                self._fail(f"{label} must be a nonnegative whole frame")
        joints = motion.skeleton.joints if motion.skeleton else []
        self._count(len(joints), 0xFFFF, "joint", "u16")
        if len({id(joint) for joint in joints}) != len(joints):
            self._fail("skeleton repeats a Joint object")
        joint_ids = {id(joint) for joint in joints}
        physical_joint_ids = self.expected_joint_ids(joints)
        hash_only_joints = {
            id(joint)
            for joint, physical_id in zip(joints, physical_joint_ids)
            if joint.joint_map_extra_type
            == JointMapExtraType.EXTRA_JOINT_INCLUDE_DEFORM
            and physical_id == 0
        }
        if any(
            joint.parent is not None
            and id(joint) not in hash_only_joints
            and id(joint.parent) in hash_only_joints
            for joint in joints
        ):
            self._fail(
                "non-hash-bound joints cannot parent to a hash-bound auxiliary joint"
            )
        has_explicit_child_order = any(joint.children for joint in joints)
        topology_index_present = self.topology_index_present(joints)
        if not topology_index_present and has_explicit_child_order:
            self._fail("skeleton without child/sibling links cannot define explicit child order")
        for index, joint in enumerate(joints):
            self._string(joint.name, "joint name")
            if joint.parent is not None and id(joint.parent) not in joint_ids:
                self._fail("joint parent is outside its skeleton")
            if len({id(child) for child in joint.children}) != len(joint.children):
                self._fail("joint child order repeats a Joint object")
            if any(id(child) not in joint_ids for child in joint.children):
                self._fail("joint child is outside its skeleton")
            if any(child.parent is not joint for child in joint.children):
                self._fail("joint child order conflicts with parent relationships")
            if joint.joint_map_extra_type not in (
                JointMapExtraType.DEFAULT,
                JointMapExtraType.EXTRA_JOINT_INCLUDE_DEFORM,
            ):
                self._fail("JointMapExtraType is unsupported by v65")
            if len(joint.translation) != 3 or len(joint.rotation) != 4:
                self._fail("joint transform component count is invalid")
            translation_is_unset = all(math.isnan(value) for value in joint.translation)
            if not (translation_is_unset or all(math.isfinite(value) for value in joint.translation)):
                self._fail(
                    f"DMC5 joint {joint.name!r} has a partially nonfinite translation {joint.translation!r}"
                )
            if not all(math.isfinite(value) for value in joint.rotation):
                self._fail(f"DMC5 joint {joint.name!r} has a nonfinite rotation {joint.rotation!r}")
            norm = sum(value * value for value in joint.rotation)
            if not 0.9999 <= norm <= 1.0001:
                self._fail("joint rotation must be a unit quaternion")
        if has_explicit_child_order:
            ordered_children = [id(child) for joint in joints for child in joint.children]
            if any(child in hash_only_joints for child in ordered_children):
                self._fail("hash-bound auxiliary joints cannot appear in child/sibling chains")
            parented = [
                id(joint)
                for joint in joints
                if joint.parent is not None and id(joint) not in hash_only_joints
            ]
            if sorted(ordered_children) != sorted(parented):
                self._fail("explicit joint child order must include every parented joint exactly once")
        if motion.animation_nodes and not motion.skeleton:
            self._fail("animated DMC5 MOT requires its own skeleton")
        binding_keys = [
            (physical_id, murmur3_hash(joint.name.encode("utf-16le")))
            for joint, physical_id in zip(joints, physical_joint_ids)
        ]
        ambiguous_binding_keys = {
            key for key, count in Counter(binding_keys).items() if count > 1
        }
        binding_key_by_joint = {
            id(joint): key for joint, key in zip(joints, binding_keys)
        }
        for node in motion.animation_nodes:
            if id(node.joint) not in joint_ids:
                self._fail("AnimationNode joint is outside the MOT skeleton")
            if binding_key_by_joint[id(node.joint)] in ambiguous_binding_keys:
                self._fail("AnimationNode target has an ambiguous joint ID/name hash")
            if not 0.0 <= node.weight <= 1.0:
                self._fail("AnimationNode weight must be normalized")
            if not any((node.translation, node.rotation, node.scale)):
                self._fail("AnimationNode must own at least one transform track")
            for track, family, channel in (
                (node.translation, TrackFamily.VECTOR3, "translation"),
                (node.rotation, TrackFamily.QUATERNION, "rotation"),
                (node.scale, TrackFamily.VECTOR3, "scale"),
            ):
                if track is not None:
                    if track.family != family:
                        self._fail(f"{channel} track has the wrong family")
                    self._validate_track(track)
        for prop in motion.property_tracks:
            if not 0 <= prop.target_name_hash <= 0xFFFFFFFF:
                self._fail("property target hash exceeds u32")
            if prop.track.family != TrackFamily.FLOAT:
                self._fail("property animation requires a Float track")
            self._validate_track(prop.track)
        previous_category = -1
        for sequence in motion.sequences:
            self.sequence_validator.validate(sequence)
            if sequence.category <= previous_category:
                self._fail("MOT sequences must be sorted by increasing category")
            previous_category = sequence.category
        if motion.character_path is not None:
            self._string(motion.character_path, "character path")
            if not motion.character_path.lower().endswith(".jmap"):
                self._fail("DMC5 character path must end in .jmap")
        for grid in motion.sync_points:
            if not 1 <= grid.block_count <= 0xFF or not 1 <= grid.point_count <= 0xFF:
                self._fail("sync block/point counts must be nonzero u8 values")
            if len(grid.frames) != grid.block_count * grid.point_count + 1:
                self._fail("sync frame count must equal blockCount*pointCount+1")
            if any(not math.isfinite(frame) or frame < 0 or frame != math.floor(frame) for frame in grid.frames):
                self._fail("sync frames must be nonnegative whole frames")
            start, end = self.sync_phase(grid, motion)
            if start > 0xFF or end > 0xFF:
                self._fail("derived sync phase exceeds u8")
        if motion.append:
            ids = [item.authored_id for item in motion.append.classes]
            if len(ids) != len(set(ids)):
                self._fail("MotionAppend class IDs must be unique")
            for item in motion.append.classes:
                hashes = [prop.name_hash for prop in item.properties] + [array.name_hash for array in item.arrays]
                if len(hashes) != len(set(hashes)):
                    self._fail("MotionAppend property hashes must be unique within a class")
                for prop in item.properties:
                    if prop.property_type not in (
                        AppendPropertyType.INT32,
                        AppendPropertyType.UINT32,
                        AppendPropertyType.STRING,
                    ):
                        self._fail("unsupported v65 MotionAppend scalar type")
                    if prop.property_type == AppendPropertyType.STRING:
                        if not isinstance(prop.value, str):
                            self._fail("MotionAppend string value must be str")
                        self._string(prop.value, "MotionAppend string")
                    elif not isinstance(prop.value, int):
                        self._fail("MotionAppend integer value must be int")
                for array in item.arrays:
                    if array.property_type not in (
                        AppendPropertyType.INT32,
                        AppendPropertyType.UINT32,
                        AppendPropertyType.UINT64,
                    ):
                        self._fail("unsupported v65 MotionAppend array type")

    @classmethod
    def expected_joint_ids(cls, joints) -> tuple[int, ...]:
        """Derive the two DMC5 v65 auxiliary-joint producer forms."""
        extra = [
            joint.joint_map_extra_type
            == JointMapExtraType.EXTRA_JOINT_INCLUDE_DEFORM
            for joint in joints
        ]
        if not any(extra):
            return tuple(range(len(joints)))

        if extra[0]:
            prefix_count = next(
                (index for index, is_extra in enumerate(extra) if not is_extra),
                len(joints),
            )
            if any(extra[prefix_count:]):
                cls._fail(
                    "hash-bound ExtraJointIncludeDeform joints must form a table prefix"
                )
            for index in range(prefix_count):
                expected_parent = None if index == 0 else joints[0]
                if joints[index].parent is not expected_parent:
                    cls._fail(
                        "hash-bound auxiliary joints must parent to record zero"
                    )
            return tuple(
                0 if index < prefix_count else index
                for index in range(len(joints))
            )

        suffix_start = next(index for index, is_extra in enumerate(extra) if is_extra)
        if not all(extra[suffix_start:]):
            cls._fail(
                "indexed ExtraJointIncludeDeform joints must form a table suffix"
            )
        return tuple(range(len(joints)))

    def topology_index_present(self, joints) -> bool:
        physical_ids = self.expected_joint_ids(joints)
        indexed_auxiliary_parent = any(
            joint.parent is not None
            and joint.joint_map_extra_type
            == JointMapExtraType.EXTRA_JOINT_INCLUDE_DEFORM
            and physical_id != 0
            for joint, physical_id in zip(joints, physical_ids)
        )
        return (
            self.profile.mot.skeleton_topology_index.is_present(joints)
            or indexed_auxiliary_parent
        )

    @staticmethod
    def sync_phase(grid, motion: Motion) -> tuple[int, int]:
        frames = grid.frames
        if not motion.looping:
            leading = 0
            while leading < len(frames) and not frames[leading]:
                leading += 1
            start = leading - 1 if grid.block_count > 1 and leading > 0 else 0
            trailing = 0
            while (
                trailing < len(frames)
                and math.isclose(
                    frames[len(frames) - trailing - 1],
                    motion.end_frame,
                )
            ):
                trailing += 1
            end = (
                trailing - 1
                if grid.block_count > 1
                and grid.point_count > 1
                and trailing > 0
                else 0
            )
            return start, end
        if len(frames) <= 1:
            return 0, 0
        phase = min(
            range(len(frames) - 1),
            key=lambda index: (frames[index], -index),
        )
        return phase, 0

    @classmethod
    def expected_selector(cls, motion: Motion) -> int:
        frame_extent = motion.end_frame
        for node in motion.animation_nodes:
            for track in (node.translation, node.rotation, node.scale):
                if track and track.frames:
                    frame_extent = max(frame_extent, track.frames[-1])
        for prop in motion.property_tracks:
            if prop.track.frames:
                frame_extent = max(frame_extent, prop.track.frames[-1])
        if frame_extent < 255:
            return 2
        if frame_extent < 65535:
            return 4
        cls._fail("v65 corpus does not establish u32 frame selectors")
        return 0

    @classmethod
    def _validate_track(cls, track: KeyTrack) -> None:
        cls._count(len(track.frames), 0xFFFFFFFF, "track key", "u32")
        frames = np.asarray(track.frames)
        if (
            frames.shape != (len(track.frames),)
            or not np.issubdtype(frames.dtype, np.integer)
            or np.issubdtype(frames.dtype, np.bool_)
            or not len(frames)
            or frames[0] != 0
        ):
            cls._fail("track frames must be nonempty and begin at zero")
        if bool(np.any(frames[1:] < frames[:-1])):
            cls._fail("track frames must be nondecreasing")
        if len(track.values) != len(track.frames):
            cls._fail("track values and frames must have the same count")
        if track.max_frame is not None and (
            not math.isfinite(track.max_frame) or track.max_frame < track.frames[-1]
        ):
            cls._fail("track max_frame must be finite and not precede its final key")
        try:
            values = np.asarray(track.values, dtype=np.float64)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MotionValidationError(
                f"{track.family.name} track contains nonnumeric values"
            ) from exc
        if track.family == TrackFamily.FLOAT:
            if values.shape != (len(track.values),) or not bool(np.all(np.isfinite(values))):
                cls._fail("Float values must be finite numbers")
            return
        width = 3 if track.family == TrackFamily.VECTOR3 else 4
        if values.shape != (len(track.values), width):
            cls._fail(f"{track.family.name} values must be {width}-float tuples")
        if not bool(np.all(np.isfinite(values))):
            cls._fail(f"{track.family.name} values must contain only finite numbers")
        if track.family == TrackFamily.QUATERNION:
            xyz = values[:, :3].astype(np.float32)
            remaining = np.subtract(
                np.float32(1.0), np.multiply(xyz[:, 0], xyz[:, 0])
            )
            remaining = np.subtract(
                remaining, np.multiply(xyz[:, 1], xyz[:, 1])
            )
            remaining = np.subtract(
                remaining, np.multiply(xyz[:, 2], xyz[:, 2])
            )
            expected_w = np.sqrt(np.maximum(np.float32(0.0), remaining))
            if bool(np.any(values[:, 3] < 0)) or not bool(
                np.all(np.isclose(values[:, 3], expected_w, rtol=0.0, atol=1e-5))
            ):
                cls._fail(
                    "Quaternion keys must use the canonical nonnegative W derived from XYZ"
                )

    @staticmethod
    def _string(value: str, what: str) -> None:
        if "\0" in value:
            raise MotionValidationError(f"{what} contains NUL")
        value.encode("utf-16le")

    @classmethod
    def _count(cls, value: int, maximum: int, what: str, storage: str) -> None:
        if value > maximum:
            cls._fail(f"{what} count exceeds {storage}")

    @staticmethod
    def _fail(message: str) -> None:
        raise MotionValidationError(message)
