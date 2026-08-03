from __future__ import annotations

import struct

from utils.hash_util import murmur3_hash

from ..binary import pad_to_alignment
from ..errors import MotionWriteError
from ..profiles import MotionFormatProfile
from ..sequence.writer import SequenceV65Writer
from .model import AppendPropertyType, JointMapExtraType, KeyTrack, Motion
from .track_codec import SOLVER_BY_FAMILY, encode_track, needs_parameters
from .validator import MotV65Validator


class MotV65Writer:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot=65, mot_clip=27)
        self.profile = profile
        self.validator = MotV65Validator(profile)
        self.sequence_writer = SequenceV65Writer(profile)

    def build(self, motion: Motion) -> bytes:
        self.validator.validate(motion)
        selector = self.validator.expected_selector(motion)
        out = bytearray(self.profile.mot.header_size)
        offsets = {
            "skeleton": 0,
            "animation": 0,
            "property": 0,
            "sequence": 0,
            "character": 0,
            "sync": 0,
            "append": 0,
        }

        # Legacy v65 names immediately follow the packed 0x74 header.
        name_offset = len(out)
        out.extend(motion.name.encode("utf-16le") + b"\0\0")

        if motion.skeleton and motion.skeleton.joints:
            pad_to_alignment(out, 16)
            offsets["skeleton"] = len(out)
            self._write_skeleton(out, motion)

        if motion.animation_nodes:
            pad_to_alignment(out, 16)
            offsets["animation"] = len(out)
            self._write_animation(out, motion, selector)

        if motion.character_path is not None:
            pad_to_alignment(out, 16)
            offsets["character"] = len(out)
            out.extend(motion.character_path.encode("utf-16le") + b"\0\0")

        if motion.sequences:
            pad_to_alignment(out, 16)
            offsets["sequence"] = len(out)
            self._write_sequences(out, motion)

        if motion.property_tracks:
            pad_to_alignment(out, 16)
            offsets["property"] = len(out)
            self._write_property_tracks(out, motion, selector)

        if motion.append is not None:
            pad_to_alignment(out, 16)
            offsets["append"] = len(out)
            self._write_append(out, motion)

        if motion.sync_points:
            pad_to_alignment(out, 16)
            offsets["sync"] = len(out)
            self._write_sync(out, motion)

        logical_end = len(out)
        pad_to_alignment(out, 16)
        joint_count = len(motion.skeleton.joints) if motion.skeleton else 0
        struct.pack_into("<I4sII", out, 0, self.profile.mot.version, b"mot ", 0, 0)
        struct.pack_into(
            "<QQQQQQQQQ",
            out,
            0x10,
            offsets["skeleton"],
            offsets["animation"],
            offsets["property"],
            0,
            offsets["sequence"],
            offsets["character"],
            offsets["sync"],
            offsets["append"],
            name_offset,
        )
        struct.pack_into(
            "<ffffHHBBHH",
            out,
            0x58,
            motion.end_frame,
            0.0 if motion.looping else -1.0,
            motion.raw_start_frame,
            motion.raw_end_frame,
            joint_count,
            len(motion.animation_nodes),
            len(motion.sequences),
            len(motion.sync_points),
            60,
            len(motion.property_tracks),
        )
        struct.pack_into("<H", out, 0x72, 0)
        if logical_end == 0:
            raise MotionWriteError("internal MOT layout failure")
        return bytes(out)

    def _write_skeleton(self, out: bytearray, motion: Motion) -> None:
        skeleton = motion.skeleton
        assert skeleton is not None
        base = len(out)
        joints = skeleton.joints
        physical_joint_ids = self.validator.expected_joint_ids(joints)
        table = base + 0x10
        out.extend(bytes(0x10 + len(joints) * 0x50))
        struct.pack_into("<QII", out, base, table, len(joints), 0)
        index_by_id = {id(joint): index for index, joint in enumerate(joints)}
        topology_index_present = self.validator.topology_index_present(joints)
        if not topology_index_present:
            children = {index: [] for index in range(len(joints))}
        elif any(joint.children for joint in joints):
            children = {
                index: [index_by_id[id(child)] for child in joint.children]
                for index, joint in enumerate(joints)
            }
        else:
            children = {index: [] for index in range(len(joints))}
            for index, joint in enumerate(joints):
                hash_bound = (
                    joint.joint_map_extra_type
                    == JointMapExtraType.EXTRA_JOINT_INCLUDE_DEFORM
                    and physical_joint_ids[index] == 0
                )
                if joint.parent is not None and not hash_bound:
                    children[index_by_id[id(joint.parent)]].append(index)
        name_offsets: dict[str, int] = {}
        cursor = base + 0x10 + len(joints) * 0x50
        for joint in joints:
            name_offsets.setdefault(joint.name, cursor)
            encoded = joint.name.encode("utf-16le") + b"\0\0"
            cursor += len(encoded)
        for index, joint in enumerate(joints):
            record = table + index * 0x50
            parent_offset = table + index_by_id[id(joint.parent)] * 0x50 if joint.parent else 0
            hash_bound = (
                joint.joint_map_extra_type
                == JointMapExtraType.EXTRA_JOINT_INCLUDE_DEFORM
                and physical_joint_ids[index] == 0
            )
            child_indices = [] if hash_bound else children[index]
            child_offset = table + child_indices[0] * 0x50 if child_indices else 0
            sibling_offset = 0
            if topology_index_present and joint.parent is not None and not hash_bound:
                siblings = children[index_by_id[id(joint.parent)]]
                position = siblings.index(index)
                if position + 1 < len(siblings):
                    sibling_offset = table + siblings[position + 1] * 0x50
            struct.pack_into("<QQQQ", out, record, name_offsets[joint.name], parent_offset, child_offset, sibling_offset)
            struct.pack_into("<4f", out, record + 0x20, *joint.translation, 0.0)
            struct.pack_into("<4f", out, record + 0x30, *joint.rotation)
            struct.pack_into(
                "<IIII",
                out,
                record + 0x40,
                physical_joint_ids[index],
                murmur3_hash(joint.name.encode("utf-16le")),
                joint.joint_map_extra_type,
                0,
            )
        for joint in joints:
            out.extend(joint.name.encode("utf-16le") + b"\0\0")

    def _write_animation(self, out: bytearray, motion: Motion, selector: int) -> None:
        table = len(out)
        node_count = len(motion.animation_nodes)
        out.extend(bytes(node_count * 0x18))
        tracks: list[KeyTrack] = []
        node_first_track: list[int] = []
        joint_index = {id(joint): index for index, joint in enumerate(motion.skeleton.joints)}
        physical_joint_ids = self.validator.expected_joint_ids(motion.skeleton.joints)
        for node in motion.animation_nodes:
            node_first_track.append(len(tracks))
            tracks.extend(track for track in (node.translation, node.rotation, node.scale) if track is not None)
        pad_to_alignment(out, 16)
        headers = len(out)
        out.extend(bytes(len(tracks) * 0x28))
        for index, node in enumerate(motion.animation_nodes):
            transform = int(node.translation is not None) | (int(node.rotation is not None) << 1) | (int(node.scale is not None) << 2)
            struct.pack_into(
                "<HBBIfIQ",
                out,
                table + index * 0x18,
                physical_joint_ids[joint_index[id(node.joint)]],
                transform,
                0,
                murmur3_hash(node.joint.name.encode("utf-16le")),
                node.weight,
                0,
                headers + node_first_track[index] * 0x28,
            )
        pad_to_alignment(out, 16)
        for index, track in enumerate(tracks):
            self._write_track_payload(out, headers + index * 0x28, track, selector, property_owner=False)

    def _write_property_tracks(self, out: bytearray, motion: Motion, selector: int) -> None:
        table = len(out)
        out.extend(bytes(len(motion.property_tracks) * 0x10))
        for index, item in enumerate(motion.property_tracks):
            track_header = len(out)
            out.extend(bytes(0x28))
            struct.pack_into("<QII", out, table + index * 0x10, track_header, item.target_name_hash, 0)
            self._write_track_payload(out, track_header, item.track, selector, property_owner=True)

    def _write_track_payload(
        self,
        out: bytearray,
        header: int,
        track: KeyTrack,
        selector: int,
        *,
        property_owner: bool,
    ) -> None:
        encoding = encode_track(track)
        pad_to_alignment(out, 16)
        frame_offset = len(out)
        if selector == 2:
            out.extend(bytes(track.frames))
        else:
            out.extend(struct.pack(f"<{len(track.frames)}H", *track.frames))
        pad_to_alignment(out, 16)
        value_offset = len(out)
        out.extend(encoding.value_bytes)
        parameter_offset = 0
        if needs_parameters(track.family, encoding.compression):
            pad_to_alignment(out, 16)
            parameter_offset = len(out)
            out.extend(
                struct.pack(
                    f"<{len(encoding.parameters)}f",
                    *encoding.parameters,
                )
            )
        elif property_owner:
            parameter_offset = header
        key_type = (
            SOLVER_BY_FAMILY[track.family]
            | (encoding.compression << 12)
            | (selector << 20)
        )
        struct.pack_into(
            "<IIIfQQQ",
            out,
            header,
            key_type,
            len(track.frames),
            60,
            track.frames[-1] if track.max_frame is None else track.max_frame,
            frame_offset,
            value_offset,
            parameter_offset,
        )

    def _write_sequences(
        self,
        out: bytearray,
        motion: Motion,
    ) -> None:
        table = len(out)
        out.extend(bytes(len(motion.sequences) * 8))
        for index, sequence in enumerate(motion.sequences):
            pad_to_alignment(out, 16)
            offset = len(out)
            struct.pack_into("<Q", out, table + index * 8, offset)
            out.extend(
                self.sequence_writer.build(
                    sequence,
                    sequence_offset=offset,
                    pointer_base=0,
                )
            )

    def _write_append(self, out: bytearray, motion: Motion) -> None:
        append = motion.append
        assert append is not None
        base = len(out)
        out.extend(bytes(0x20))
        class_table = len(out)
        out.extend(bytes(len(append.classes) * 0x20))
        scalar_string_patches: list[tuple[int, str]] = []
        array_records: list[tuple[int, object]] = []
        for index, item in enumerate(append.classes):
            pad_to_alignment(out, 16)
            prop_table = len(out)
            for prop in item.properties:
                record = len(out)
                out.extend(bytes(0x10))
                if prop.property_type == AppendPropertyType.STRING:
                    scalar_string_patches.append((record, prop.value))
                elif prop.property_type == AppendPropertyType.INT32:
                    struct.pack_into("<I", out, record, prop.value & 0xFFFFFFFF)
                else:
                    struct.pack_into("<I", out, record, prop.value)
                struct.pack_into("<IBBBB", out, record + 8, prop.name_hash, int(prop.property_type), 0, 0, 0)
            array_table = len(out)
            for array in item.arrays:
                record = len(out)
                out.extend(bytes(0x18))
                struct.pack_into(
                    "<QIIIBBBB",
                    out,
                    record,
                    0,
                    array.name_hash,
                    len(array.values),
                    0,
                    int(array.property_type),
                    0,
                    0,
                    0,
                )
                array_records.append((record, array))
            struct.pack_into(
                "<QQIIII",
                out,
                class_table + index * 0x20,
                prop_table - base,
                array_table - base,
                len(item.properties),
                len(item.arrays),
                item.name_hash,
                item.authored_id,
            )
        pad_to_alignment(out, 16)
        for record, array in array_records:
            pad_to_alignment(out, 16)
            struct.pack_into("<Q", out, record, len(out) - base)
            if array.property_type == AppendPropertyType.INT32:
                out.extend(struct.pack(f"<{len(array.values)}i", *array.values))
            elif array.property_type == AppendPropertyType.UINT32:
                out.extend(struct.pack(f"<{len(array.values)}I", *array.values))
            else:
                out.extend(struct.pack(f"<{len(array.values)}Q", *array.values))
        for record, value in scalar_string_patches:
            struct.pack_into("<Q", out, record, len(out) - base)
            out.extend(value.encode("utf-16le") + b"\0\0")
        remap_offset = 0
        if append.remaps:
            pad_to_alignment(out, 16)
            remap_offset = len(out) - base
            for remap in append.remaps:
                out.extend(struct.pack("<II", remap.requested_hash, remap.stored_hash))
        struct.pack_into(
            "<QQII",
            out,
            base,
            class_table - base,
            remap_offset,
            len(append.classes),
            len(append.remaps),
        )

    def _write_sync(self, out: bytearray, motion: Motion) -> None:
        table = len(out)
        out.extend(bytes(len(motion.sync_points) * 8))
        for index, grid in enumerate(motion.sync_points):
            pad_to_alignment(out, 16)
            record = len(out)
            struct.pack_into("<Q", out, table + index * 8, record)
            out.extend(bytes(0x0C))
            pad_to_alignment(out, 16)
            frames = len(out)
            start, end = self.validator.sync_phase(grid, motion)
            struct.pack_into("<QBBBB", out, record, frames, grid.block_count, grid.point_count, start, end)
            out.extend(struct.pack(f"<{len(grid.frames)}f", *grid.frames))

