from __future__ import annotations

import struct

import numpy as np

from utils.hash_util import murmur3_hash

from ..binary import ReadContext, align_up
from ..errors import MotionParseError, MotionValidationError
from ..profiles import MotionFormatProfile
from ..sequence.parser import SequenceV65Parser
from .model import (
    AnimationNode,
    AppendArray,
    AppendClass,
    AppendProperty,
    AppendPropertyType,
    Joint,
    JointMapExtraType,
    KeyTrack,
    Motion,
    MotionAppend,
    PropertyHashRemap,
    PropertyTrack,
    Skeleton,
    SyncPointGrid,
    TrackFamily,
)
from .track_codec import (
    FAMILY_BY_SOLVER,
    bytes_per_key,
    decode_track_values,
    needs_parameters,
    parameter_count,
)
from .validator import MotV65Validator


class MotV65Parser:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot=65, mot_clip=27)
        self.profile = profile
        self.sequence_parser = SequenceV65Parser(profile)
        self.validator = MotV65Validator(profile)

    def parse(self, context: ReadContext, base: int, physical_end: int) -> Motion:
        c = context.subcontext(base, physical_end, label=f"MOT@0x{base:X}", object_base=base)
        c.require(base, self.profile.mot.header_size, "MOT header")
        if c.u32(base, "MOT version") != self.profile.mot.version or c.bytes(base + 4, 4) != b"mot ":
            raise MotionParseError(f"{c.label}: expected MOT v{self.profile.mot.version}")
        if c.u32(base + 8, "MOT error flags") or c.u32(base + 0xC, "MOT masterSize"):
            raise MotionParseError(f"{c.label}: unsupported error/master header state")
        stored_offsets = [c.u64(base + 0x10 + index * 8, "MOT pointer") for index in range(9)]
        (
            skeleton_stored,
            animation_stored,
            property_stored,
            custom_stored,
            sequence_stored,
            character_stored,
            sync_stored,
            append_stored,
            name_stored,
        ) = stored_offsets
        if custom_stored:
            raise MotionParseError(f"{c.label}: custom key data is unsupported")
        end_frame = c.f32(base + 0x58, "MOT end frame")
        loop_time = c.f32(base + 0x5C, "MOT loop time")
        if loop_time not in (-1.0, 0.0):
            raise MotionParseError(f"{c.label}: loop time must be -1 or 0")
        looping = not loop_time
        raw_start = c.f32(base + 0x60, "MOT raw start")
        raw_end = c.f32(base + 0x64, "MOT raw end")
        joint_count = c.u16(base + 0x68, "MOT joint count")
        animation_count = c.u16(base + 0x6A, "MOT animation count")
        sequence_count = c.u8(base + 0x6C, "MOT sequence count")
        sync_count = c.u8(base + 0x6D, "MOT sync count")
        if c.u16(base + 0x6E, "MOT fps") != 60:
            raise MotionParseError(f"{c.label}: MOT fps must equal 60")
        property_count = c.u16(base + 0x70, "MOT property count")
        if c.u16(base + 0x72, "MOT attribute flags"):
            raise MotionParseError(f"{c.label}: MOT attribute flags are unsupported")

        name_offset = base + name_stored
        if name_stored != self.profile.mot.header_size:
            raise MotionParseError(f"{c.label}: v65 MOT name must follow the header")
        name, name_end = c.utf16_z(name_offset, "MOT name")
        cursor = name_end

        def block(stored: int, present: bool, label: str) -> int | None:
            if bool(stored) != present:
                raise MotionParseError(f"{c.label}: {label} pointer/count presence mismatch")
            if not present:
                return None
            expected = align_up(cursor_state[0], 16)
            absolute = base + stored
            if absolute != expected:
                raise MotionParseError(f"{c.label}: {label} violates canonical top-level order")
            c.require_zero(cursor_state[0], expected, f"{label} alignment")
            return absolute

        cursor_state = [cursor]
        skeleton = None
        skeleton_joint_ids = []
        skeleton_offset = block(skeleton_stored, joint_count > 0, "skeleton")
        if skeleton_offset is not None:
            skeleton, skeleton_joint_ids, cursor_state[0] = self._parse_skeleton(
                c, base, skeleton_offset, joint_count
            )

        animation_nodes: list[AnimationNode] = []
        animation_offset = block(animation_stored, animation_count > 0, "animation")
        if animation_offset is not None:
            if skeleton is None:
                raise MotionParseError(f"{c.label}: animated MOT has no skeleton")
            animation_nodes, cursor_state[0] = self._parse_animation(
                c,
                base,
                animation_offset,
                animation_count,
                skeleton,
                skeleton_joint_ids,
            )

        character_path = None
        character_offset = block(character_stored, character_stored > 0, "character data")
        if character_offset is not None:
            character_path, cursor_state[0] = c.utf16_z(character_offset, "MOT character path")
            if not character_path.lower().endswith(".jmap"):
                raise MotionParseError(f"{c.label}: character data is not a .jmap path")

        sequences = []
        sequence_offset = block(sequence_stored, sequence_count > 0, "sequence table")
        if sequence_offset is not None:
            sequences, cursor_state[0] = self._parse_sequences(
                c,
                base,
                sequence_offset,
                sequence_count,
            )

        property_tracks = []
        property_offset = block(property_stored, property_count > 0, "property animation")
        if property_offset is not None:
            property_tracks, cursor_state[0] = self._parse_property_tracks(
                c, base, property_offset, property_count
            )

        append_data = None
        append_offset = block(append_stored, append_stored > 0, "MotionAppendData")
        if append_offset is not None:
            append_data, cursor_state[0] = self._parse_append(c, append_offset)

        sync_points = []
        sync_offset = block(sync_stored, sync_count > 0, "sync data")
        if sync_offset is not None:
            sync_points, cursor_state[0] = self._parse_sync(
                c, base, sync_offset, sync_count, end_frame, looping
            )

        expected_end = align_up(cursor_state[0], 16)
        if expected_end != physical_end:
            raise MotionParseError(f"{c.label}: MOT physical end does not equal Align16(logical end)")
        c.require_zero(cursor_state[0], expected_end, "MOT physical padding")
        result = Motion(
            name=name,
            end_frame=end_frame,
            looping=looping,
            raw_start_frame=raw_start,
            raw_end_frame=raw_end,
            skeleton=skeleton,
            animation_nodes=animation_nodes,
            property_tracks=property_tracks,
            sequences=sequences,
            character_path=character_path,
            sync_points=sync_points,
            append=append_data,
        )
        self.validator.validate(result)
        return result

    def _parse_skeleton(self, c, base, offset, count):
        table_stored = c.u64(offset, "Skeleton joint table")
        if base + table_stored != offset + 0x10 or c.u32(offset + 8) != count or c.u32(offset + 0xC):
            raise MotionParseError(f"{c.label}: invalid SkeletonData header")
        table = base + table_stored
        c.require(table, count * 0x50, "skeleton joints")
        joints: list[Joint] = []
        parent_offsets: list[int] = []
        child_offsets: list[int] = []
        sibling_offsets: list[int] = []
        name_pointers: list[int] = []
        physical_joint_ids: list[int] = []
        for index in range(count):
            record = table + index * 0x50
            name_ptr = c.u64(record, "joint name pointer")
            name, _ = c.utf16_z(base + name_ptr, "joint name")
            if c.u32(record + 0x2C, "joint translation pad") or c.u32(record + 0x4C):
                raise MotionParseError(f"{c.label}: invalid joint padding")
            if c.u32(record + 0x44) != murmur3_hash(name.encode("utf-16le")):
                raise MotionParseError(f"{c.label}: joint name hash mismatch")
            try:
                joint_map_extra_type = JointMapExtraType(c.u32(record + 0x48))
            except ValueError as exc:
                raise MotionParseError(
                    f"{c.label}: unsupported JointMapExtraType"
                ) from exc
            if joint_map_extra_type is JointMapExtraType.INCLUDE_EXTRA_VALUE:
                raise MotionParseError(
                    f"{c.label}: JointMapExtraType IncludeExtraValue is unsupported"
                )
            joints.append(
                Joint(
                    name=name,
                    translation=tuple(c.f32(record + 0x20 + part * 4) for part in range(3)),
                    rotation=tuple(c.f32(record + 0x30 + part * 4) for part in range(4)),
                    joint_map_extra_type=joint_map_extra_type,
                )
            )
            physical_joint_ids.append(c.u32(record + 0x40))
            name_pointers.append(name_ptr)
            parent_offsets.append(c.u64(record + 8))
            child_offsets.append(c.u64(record + 0x10))
            sibling_offsets.append(c.u64(record + 0x18))
        def index_from_pointer(pointer: int) -> int | None:
            if pointer == 0:
                return None
            if pointer < table_stored or (pointer - table_stored) % 0x50:
                raise MotionParseError(f"{c.label}: skeleton relation pointer is invalid")
            index = (pointer - table_stored) // 0x50
            if index >= count:
                raise MotionParseError(f"{c.label}: skeleton relation pointer is outside table")
            return index
        for index, parent in enumerate(parent_offsets):
            parent_index = index_from_pointer(parent)
            joints[index].parent = joints[parent_index] if parent_index is not None else None
            index_from_pointer(sibling_offsets[index])
        actual_topology_index = any(child_offsets) or any(sibling_offsets)
        try:
            expected_joint_ids = self.validator.expected_joint_ids(joints)
            expected_topology_index = self.validator.topology_index_present(joints)
        except MotionValidationError as exc:
            raise MotionParseError(f"{c.label}: {exc}") from exc
        if tuple(physical_joint_ids) != expected_joint_ids:
            raise MotionParseError(
                f"{c.label}: skeleton joint IDs violate the v65 auxiliary-joint form"
            )
        hash_bound_indices = {
            index
            for index, (joint, physical_id) in enumerate(
                zip(joints, physical_joint_ids)
            )
            if joint.joint_map_extra_type
            == JointMapExtraType.EXTRA_JOINT_INCLUDE_DEFORM
            and physical_id == 0
        }
        if any(
            child_offsets[index] or sibling_offsets[index]
            for index in hash_bound_indices
        ):
            raise MotionParseError(
                f"{c.label}: hash-bound auxiliary joints cannot own child/sibling links"
            )
        if actual_topology_index != expected_topology_index:
            raise MotionParseError(
                f"{c.label}: skeleton child/sibling index presence violates the v65 profile strategy"
            )
        if expected_topology_index:
            for parent_index, first_child in enumerate(child_offsets):
                expected_children = {
                    index
                    for index, joint in enumerate(joints)
                    if joint.parent is joints[parent_index]
                    and index not in hash_bound_indices
                }
                ordered_children = []
                child_index = index_from_pointer(first_child)
                while child_index is not None:
                    if child_index in ordered_children:
                        raise MotionParseError(f"{c.label}: skeleton sibling chain contains a cycle")
                    if child_index not in expected_children:
                        raise MotionParseError(f"{c.label}: skeleton child/sibling chain conflicts with parent pointer")
                    ordered_children.append(child_index)
                    child_index = index_from_pointer(sibling_offsets[child_index])
                if set(ordered_children) != expected_children:
                    raise MotionParseError(
                        f"{c.label}: skeleton child/sibling chain for joint {parent_index} "
                        f"is incomplete (chain={ordered_children}, parents={sorted(expected_children)})"
                    )
                joints[parent_index].children = [joints[index] for index in ordered_children]
        name_cursor = table + count * 0x50
        first_names: dict[str, int] = {}
        for index, joint in enumerate(joints):
            serialized_name, name_cursor = c.utf16_z(name_cursor, "serialized joint name")
            if serialized_name != joint.name:
                raise MotionParseError(f"{c.label}: serialized joint-name copy mismatch")
            first_names.setdefault(joint.name, name_cursor - len(serialized_name.encode("utf-16le")) - 2 - base)
            if name_pointers[index] != first_names[joint.name]:
                raise MotionParseError(f"{c.label}: joint name pointer does not target first equal copy")
        return Skeleton(joints), physical_joint_ids, name_cursor

    def _parse_animation(self, c, base, offset, count, skeleton, skeleton_joint_ids):
        c.require(offset, count * 0x18, "AnimationNode table")
        raw_nodes = []
        track_count = 0
        first_track = align_up(offset + count * 0x18, 16)
        c.require_zero(offset + count * 0x18, first_track, "AnimationNode alignment")
        for index in range(count):
            record = offset + index * 0x18
            joint_id = c.u16(record)
            transform = c.u8(record + 2)
            if not transform or transform & ~7 or c.u8(record + 3) or c.u32(record + 0xC):
                raise MotionParseError(f"{c.label}: invalid AnimationNode")
            first = c.u64(record + 0x10)
            expected = first_track - base + track_count * 0x28
            if first != expected:
                raise MotionParseError(f"{c.label}: AnimationNode track pointer violates global table")
            raw_nodes.append((joint_id, transform, c.u32(record + 4), c.f32(record + 8)))
            track_count += transform.bit_count()
        headers = first_track
        c.require(headers, track_count * 0x28, "animation TrackHeader table")
        payload_cursor = align_up(headers + track_count * 0x28, 16)
        c.require_zero(headers + track_count * 0x28, payload_cursor, "TrackHeader alignment")
        header_index = 0
        parsed_nodes = []
        for joint_id, transform, name_hash, weight in raw_nodes:
            tracks = {}
            for bit, family, name in (
                (1, TrackFamily.VECTOR3, "translation"),
                (2, TrackFamily.QUATERNION, "rotation"),
                (4, TrackFamily.VECTOR3, "scale"),
            ):
                if transform & bit:
                    expected_payload = align_up(payload_cursor, 16)
                    c.require_zero(payload_cursor, expected_payload, "animation track payload alignment")
                    track, payload_cursor = self._parse_track(
                        c,
                        base,
                        headers + header_index * 0x28,
                        family,
                        expected_payload,
                        property_owner=False,
                    )
                    tracks[name] = track
                    header_index += 1
            parsed_nodes.append((joint_id, name_hash, weight, tracks))
        bindings: dict[tuple[int, int], list[Joint]] = {}
        for joint, physical_id in zip(skeleton.joints, skeleton_joint_ids):
            key = (
                physical_id,
                murmur3_hash(joint.name.encode("utf-16le")),
            )
            bindings.setdefault(key, []).append(joint)
        result = []
        for joint_id, name_hash, weight, tracks in parsed_nodes:
            matches = bindings.get((joint_id, name_hash), ())
            if not matches:
                raise MotionParseError(f"{c.label}: AnimationNode does not bind a skeleton joint")
            if len(matches) > 1:
                raise MotionParseError(f"{c.label}: AnimationNode joint binding is ambiguous")
            joint = matches[0]
            result.append(AnimationNode(
                joint,
                weight,
                tracks.get("translation"),
                tracks.get("rotation"),
                tracks.get("scale"),
            ))
        return result, payload_cursor

    def _parse_track(self, c, base, header, expected_family, cursor, *, property_owner):
        key_type = c.u32(header, "Track key type")
        count = c.u32(header + 4, "Track key count")
        if not count or c.u32(header + 8, "Track frame rate") != 60 or key_type >> 24:
            raise MotionParseError(f"{c.label}: invalid v65 TrackHeader")
        solver = key_type & 0xFFF
        compression = (key_type >> 12) & 0xFF
        selector = (key_type >> 20) & 0xF
        family = FAMILY_BY_SOLVER.get(solver)
        if family != expected_family or selector not in (2, 4):
            raise MotionParseError(f"{c.label}: unsupported track solver/family/selector")
        width = bytes_per_key(family, compression)
        frame_width = 1 if selector == 2 else 2
        frame_offset = base + c.u64(header + 0x10, "track frames")
        value_offset = base + c.u64(header + 0x18, "track values")
        parameter_stored = c.u64(header + 0x20, "track parameters")
        if frame_offset != cursor:
            raise MotionParseError(f"{c.label}: track frame table violates writer cursor")
        c.require(frame_offset, count * frame_width, "track frames")
        frames = np.frombuffer(
            c.data,
            dtype="u1" if selector == 2 else "<u2",
            count=count,
            offset=frame_offset,
        ).tolist()
        logical = frame_offset + count * frame_width
        expected = align_up(logical, 16)
        c.require_zero(logical, expected, "track frame alignment")
        if value_offset != expected:
            raise MotionParseError(f"{c.label}: track value table violates writer cursor")
        logical = value_offset + count * width
        parameters = []
        if needs_parameters(family, compression):
            expected = align_up(logical, 16)
            c.require_zero(logical, expected, "track value alignment")
            if base + parameter_stored != expected:
                raise MotionParseError(f"{c.label}: track parameter table violates writer cursor")
            pcount = parameter_count(family, compression)
            parameters = [c.f32(expected + index * 4) for index in range(pcount)]
            logical = expected + pcount * 4
        else:
            expected_stored = header - base if property_owner else 0
            if parameter_stored != expected_stored:
                raise MotionParseError(f"{c.label}: unused track parameter pointer is invalid")
        values = decode_track_values(
            c,
            value_offset,
            count,
            family,
            compression,
            parameters,
        )
        return (
            KeyTrack(
                family,
                frames,
                values,
                c.f32(header + 0xC, "track max frame"),
            ),
            logical,
        )

    def _parse_sequences(self, c, base, offset, count):
        c.require(offset, count * 8, "sequence offset table")
        cursor = offset + count * 8
        result = []
        previous_category = -1
        for index in range(count):
            expected = align_up(cursor, 16)
            c.require_zero(cursor, expected, "sequence alignment")
            record = base + c.u64(offset + index * 8, "sequence pointer")
            if record != expected:
                raise MotionParseError(f"{c.label}: sequence record violates writer cursor")
            sequence = self.sequence_parser.parse(
                c,
                record,
                pointer_base=base,
            )
            if sequence.category <= previous_category:
                raise MotionParseError(f"{c.label}: MOT sequences are not sorted by category")
            result.append(sequence)
            previous_category = sequence.category
            cursor = self.sequence_parser.physical_end(c, record, pointer_base=base)
        return result, cursor

    def _parse_property_tracks(self, c, base, offset, count):
        c.require(offset, count * 0x10, "PropertyAnimationNode table")
        cursor = offset + count * 0x10
        result = []
        for index in range(count):
            record = offset + index * 0x10
            header = base + c.u64(record, "property TrackHeader")
            if header != cursor or c.u32(record + 0xC):
                raise MotionParseError(f"{c.label}: property TrackHeader violates writer cursor")
            track, cursor = self._parse_track(
                c, base, header, TrackFamily.FLOAT, align_up(header + 0x28, 16), property_owner=True
            )
            c.require_zero(header + 0x28, align_up(header + 0x28, 16), "property TrackHeader alignment")
            result.append(PropertyTrack(c.u32(record + 8), track))
        return result, cursor

    def _parse_append(self, c, base):
        class_stored = c.u64(base, "append class table")
        remap_stored = c.u64(base + 8, "append remap table")
        class_count = c.u32(base + 0x10)
        remap_count = c.u32(base + 0x14)
        c.require_zero(base + 0x18, base + 0x20, "append header alignment")
        if class_stored != 0x20 or bool(remap_stored) != bool(remap_count):
            raise MotionParseError(f"{c.label}: invalid MotionAppend header")
        class_table = base + class_stored
        c.require(class_table, class_count * 0x20, "append class table")
        metadata_cursor = class_table + class_count * 0x20
        classes = []
        raw_arrays = []
        raw_strings = []
        for index in range(class_count):
            record = class_table + index * 0x20
            prop_count = c.u32(record + 0x10)
            array_count = c.u32(record + 0x14)
            prop_table = base + c.u64(record)
            array_table = base + c.u64(record + 8)
            expected = align_up(metadata_cursor, 16)
            c.require_zero(metadata_cursor, expected, "append metadata alignment")
            if prop_table != expected or array_table != prop_table + prop_count * 0x10:
                raise MotionParseError(f"{c.label}: append metadata violates writer cursor")
            props = []
            for prop_index in range(prop_count):
                item = prop_table + prop_index * 0x10
                raw = c.u64(item)
                name_hash = c.u32(item + 8)
                ptype = c.u8(item + 0xC)
                if c.u8(item + 0xD) or c.u16(item + 0xE):
                    raise MotionParseError(f"{c.label}: invalid append scalar flags")
                if ptype == AppendPropertyType.INT32:
                    if raw >> 32:
                        raise MotionParseError(f"{c.label}: append Int32 upper dword is nonzero")
                    value = struct.unpack("<i", struct.pack("<I", raw))[0]
                elif ptype == AppendPropertyType.UINT32:
                    if raw >> 32:
                        raise MotionParseError(f"{c.label}: append UInt32 upper dword is nonzero")
                    value = raw
                elif ptype == AppendPropertyType.STRING:
                    value = ""
                    raw_strings.append((len(classes), len(props), base + raw))
                else:
                    raise MotionParseError(f"{c.label}: unsupported append scalar type {ptype}")
                props.append(AppendProperty(name_hash, AppendPropertyType(ptype), value))
            arrays = []
            for array_index in range(array_count):
                item = array_table + array_index * 0x18
                value_stored = c.u64(item)
                name_hash = c.u32(item + 8)
                value_count = c.u32(item + 0xC)
                if c.u32(item + 0x10) or c.u8(item + 0x15) or c.u16(item + 0x16):
                    raise MotionParseError(f"{c.label}: invalid append array flags")
                ptype = c.u8(item + 0x14)
                if ptype not in (AppendPropertyType.INT32, AppendPropertyType.UINT32, AppendPropertyType.UINT64):
                    raise MotionParseError(f"{c.label}: unsupported append array type {ptype}")
                arrays.append(AppendArray(name_hash, AppendPropertyType(ptype), []))
                raw_arrays.append((len(classes), len(arrays) - 1, base + value_stored, value_count, ptype))
            classes.append(AppendClass(c.u32(record + 0x18), c.u32(record + 0x1C), props, arrays))
            metadata_cursor = array_table + array_count * 0x18
        payload_cursor = align_up(metadata_cursor, 16)
        c.require_zero(metadata_cursor, payload_cursor, "append payload alignment")
        for class_index, array_index, values, count, ptype in raw_arrays:
            expected = align_up(payload_cursor, 16)
            c.require_zero(payload_cursor, expected, "append array alignment")
            if values != expected:
                raise MotionParseError(f"{c.label}: append array payload violates writer cursor")
            width = 8 if ptype == AppendPropertyType.UINT64 else 4
            c.require(values, count * width, "append array payload")
            parsed = np.frombuffer(
                c.data,
                dtype={
                    AppendPropertyType.INT32: "<i4",
                    AppendPropertyType.UINT32: "<u4",
                    AppendPropertyType.UINT64: "<u8",
                }[ptype],
                count=count,
                offset=values,
            ).tolist()
            classes[class_index].arrays[array_index].values = parsed
            payload_cursor = values + count * width
        for class_index, prop_index, string_offset in raw_strings:
            if string_offset != payload_cursor:
                raise MotionParseError(f"{c.label}: append string violates writer cursor")
            value, payload_cursor = c.utf16_z(string_offset, "append string")
            classes[class_index].properties[prop_index].value = value
        remaps = []
        if remap_count:
            expected = align_up(payload_cursor, 16)
            c.require_zero(payload_cursor, expected, "append remap alignment")
            if base + remap_stored != expected:
                raise MotionParseError(f"{c.label}: append remap table violates writer cursor")
            for index in range(remap_count):
                remaps.append(PropertyHashRemap(c.u32(expected + index * 8), c.u32(expected + index * 8 + 4)))
            payload_cursor = expected + remap_count * 8
        return MotionAppend(classes, remaps), payload_cursor

    def _parse_sync(self, c, base, offset, count, end_frame, looping):
        c.require(offset, count * 8, "sync offset table")
        cursor = offset + count * 8
        result = []
        for index in range(count):
            expected = align_up(cursor, 16)
            c.require_zero(cursor, expected, "sync record alignment")
            record = base + c.u64(offset + index * 8, "sync record")
            if record != expected:
                raise MotionParseError(f"{c.label}: sync record violates writer cursor")
            frames = base + c.u64(record, "sync frames")
            block_count = c.u8(record + 8)
            point_count = c.u8(record + 9)
            start = c.u8(record + 0xA)
            end = c.u8(record + 0xB)
            frame_count = block_count * point_count + 1
            expected_frames = align_up(record + 0xC, 16)
            c.require_zero(record + 0xC, expected_frames, "sync frame alignment")
            if not block_count or not point_count or frames != expected_frames:
                raise MotionParseError(f"{c.label}: invalid sync record")
            values = [c.f32(frames + item * 4) for item in range(frame_count)]
            fake_motion = Motion("", end_frame=end_frame, looping=looping)
            expected_phase = self.validator.sync_phase(
                SyncPointGrid(block_count, point_count, values),
                fake_motion,
            )
            if (start, end) != expected_phase:
                raise MotionParseError(f"{c.label}: sync phase offsets violate producer formula")
            result.append(SyncPointGrid(block_count, point_count, values))
            cursor = frames + frame_count * 4
        return result, cursor
