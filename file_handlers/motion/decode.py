"""See docs/internals/mot-format.md."""

from __future__ import annotations

import struct

lazy from file_handlers.clip.decode import read_motclip

lazy from .tree import read_tree

from .errors import MotionFormatError
from .model import (
    AppendClass,
    AppendData,
    AppendValue,
    GpuMotion,
    GpuStream,
    Joint,
    JointChannels,
    JointPose,
    Motion,
    MotionList,
    MotionListEntry,
    MotionTree,
    PoseFrame,
    PropertyTrack,
    ReferencePath,
    Sequence,
    SequenceTrack,
    Skeleton,
    SyncPoints,
    Track,
)
from .rules import (
    MotProfile,
    append_primitives,
    append_types,
    display_names,
    encoding,
    frame_types,
    gpu_format,
    gpu_range_floats,
    list_profile,
    mot_profile,
    pose_flags,
    solver_kinds,
)

_U32 = struct.Struct("<I")
_FRAME_FORMATS = {"u8": "B", "u16": "H", "u32": "I"}
TRANSFORMS = ("translation", "rotation", "scale")


def read_mot(data: bytes, base: int = 0, *, standalone: bool = True) -> Motion:
    if len(data) < base + 8 or data[base + 4:base + 8] != b"mot ":
        raise MotionFormatError("Not a MOT file")
    return _MotReader(data, base, mot_profile(_U32.unpack_from(data, base)[0]), standalone).read()


def read_motlist(data: bytes) -> MotionList:
    if len(data) < 8 or data[4:8] != b"mlst":
        raise MotionFormatError("Not a MOTLIST file")
    return _ListReader(data).read()


def read_file(data: bytes) -> Motion | MotionList | MotionTree:
    if data[4:8] == b"mlst":
        return read_motlist(data)
    if data[4:8] == b"mtre":
        return read_tree(data)
    return read_mot(data)


def utf16z(data: bytes, offset: int) -> str:
    end = offset
    while True:
        end = data.find(b"\0\0", end)
        if end < 0:
            raise MotionFormatError(f"UTF-16 string at 0x{offset:X} has no terminator")
        if (end - offset) % 2 == 0:
            break
        end += 1
    try:
        return data[offset:end].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise MotionFormatError(f"UTF-16 string at 0x{offset:X} is not valid text: {exc}") from None


class _Reader:
    def __init__(self, data: bytes, base: int, records: dict):
        self.data, self.base, self.records = data, base, records

    def _record(self, name: str, offset: int) -> dict:
        return self.records[name].decode(self.data, self.base + offset)

    def _table(self, name: str, offset: int, count: int) -> list[dict]:
        return self.records[name].decode_table(self.data, self.base + offset, count)

    def _string(self, offset: int) -> str:
        return utf16z(self.data, self.base + offset)

    def _unpack(self, fmt: str, offset: int) -> tuple:
        try:
            return struct.unpack_from("<" + fmt, self.data, self.base + offset)
        except struct.error:
            raise MotionFormatError(f"{fmt} values at 0x{offset:X} leave the data") from None

    def _pointers(self, offset: int, count: int) -> tuple[int, ...]:
        return self._unpack(f"{count}Q", offset) if count else ()

    def _sequence(self, offset: int) -> Sequence:
        record = self._record("sequence", offset)
        rows = self._table("sequence_track", record["tracks"], record["track_count"])
        clip, end = read_motclip(self.data, self.base + record["clip"], self.base)
        if end != self.base + record["tracks"]:
            raise MotionFormatError("A sequence's track table does not follow its clip")
        return Sequence(record["category"], clip,
                        [SequenceTrack(row["id"], (row["filter0"], row["filter1"], row["filter2"]),
                                       (row.get("invalid0", 0), row.get("invalid1", 0), row.get("invalid2", 0)))
                         for row in rows])


class _MotReader(_Reader):
    def __init__(self, data: bytes, base: int, profile: MotProfile, standalone: bool):
        super().__init__(data, base, profile.rules.records)
        self.profile, self.rules, self.standalone = profile, profile.rules, standalone
        self.header = self.rules.header.decode(data, base)
        self.shared: dict[int, list[Track]] = {}

    def read(self) -> Motion:
        h = self.header
        motion = Motion(self.profile.version, self._string(h["name"]), h["end_frame"], h["loop_frame"],
                        h["raw_start_frame"], h["raw_end_frame"], h["fps"], h["attributes"], h["error_flags"],
                        h["joint_count"])
        if h["skeleton"] and (self.standalone or not h["master_size"]):
            motion.skeleton = self._skeleton(h["skeleton"], h["joint_count"])
        if h["animation_count"]:
            motion.channels = self._channels(h["animation"], h["animation_count"])
            self._link_shared()
        if h["property_count"]:
            motion.properties = [PropertyTrack(item["name_hash"], self._track(item["track"], True))
                                 for item in self._table("property", h["properties"], h["property_count"])]
        if h["sequence_count"]:
            motion.sequences = [self._sequence(offset) for offset in self._pointers(h["sequences"], h["sequence_count"])]
        if h["character"]:
            if h["attributes"] & _flag("AttrFlags", "GpuMot"):
                motion.gpu = self._gpu(h["character"])
            elif h["attributes"] & _flag("AttrFlags", "JointPose"):
                motion.joint_poses = self._joint_poses(h["character"])
            else:
                motion.jmap_path = self._string(h["character"])
        if h["sync_count"]:
            motion.sync = [self._sync(offset) for offset in self._pointers(h["sync"], h["sync_count"])]
        if h.get("raw_sync_count"):
            motion.raw_sync = [self._raw_sync(offset) for offset in self._pointers(h["raw_sync"], h["raw_sync_count"])]
        if h["append"]:
            motion.append = self._append(h["append"])
        return motion

    def _skeleton(self, offset: int, count: int) -> Skeleton:
        record = self._record("skeleton", offset)
        if record["joint_count"] != count:
            raise MotionFormatError("The skeleton and the header disagree on the joint count")
        rows = self._table("joint", record["joints"], count)
        size = self.rules.records["joint"].size
        joints = [Joint(self._string(row["name"]), None, [], tuple(row["translation"]), tuple(row["rotation"]),
                        row["joint_id"], row["extra_type"]) for row in rows]

        def joint_at(pointer: int, what: str) -> Joint:
            index, rest = divmod(pointer - record["joints"], size)
            if rest or not 0 <= index < count:
                raise MotionFormatError(f"A joint's {what} is outside the joint table")
            return joints[index]

        for joint, row in zip(joints, rows):
            if row["parent"]:
                joint.parent = joint_at(row["parent"], "parent")
        for joint, row in zip(joints, rows):
            pointer, seen = row["child"], set()
            while pointer:
                child = joint_at(pointer, "child")
                if child.parent is not joint or id(child) in seen:
                    raise MotionFormatError(f"The child chain of joint {joint.name!r} is broken")
                seen.add(id(child))
                joint.children.append(child)
                pointer = rows[joints.index(child)]["sibling"]
        return Skeleton(joints)

    def _channels(self, offset: int, count: int) -> list[JointChannels]:
        channels = []
        for row in self._table("channel", offset, count):
            channel = JointChannels(row["joint_id"], row["name_hash"],
                                    row["weight"] if self.rules.tracks == "legacy" else row["weight"] / 255.0)
            at = row["tracks"]
            size = self.rules.records["track"].size
            for bit, name in enumerate(TRANSFORMS):
                if row["transforms"] >> bit & 1:
                    setattr(channel, name, self._track(at, False))
                    at += size
            if row["transforms"] & ~7:
                raise MotionFormatError(f"Channel of joint {row['joint_id']} has unknown transform bits")
            channels.append(channel)
        return channels

    def _track(self, offset: int, owned_by_property: bool) -> Track:
        record = self._record("track", offset)
        kind = solver_kinds().get(record["solver"])
        group = self.shared.setdefault(record["values"], []) if not owned_by_property and not record["frames"] else None
        for track in group or ():
            if track.kind == kind and track.encoding == record["encoding"]:
                return track
        if kind is None:
            raise MotionFormatError(f"Track solver 0x{record['solver']:X} is not known")
        enc = encoding(self.profile.keys, kind, record["encoding"])
        count = record["key_count"]
        frame_format = _FRAME_FORMATS[frame_types()[record["frame_type"]]] if record["frame_type"] in frame_types() \
            else None
        if frame_format is None:
            raise MotionFormatError(f"Track frame type {record['frame_type']} is not known")
        frames = list(self._unpack(f"{count}{frame_format}", record["frames"])) if record["frames"] else [0] * count
        if count == 0 or (not record["frames"] and count != 1):
            raise MotionFormatError("A track without frame indices must hold one key")
        start = self.base + record["values"]
        data = bytes(self.data[start:start + count * enc.size])
        if len(data) != count * enc.size:
            raise MotionFormatError("Track keys leave the data")
        parameters = self._unpack(f"{enc.params}f", record["parameters"]) if enc.params else ()
        track = Track(kind, frames, enc.code, parameters, data, record.get("max_frame", 0.0))
        if group is not None:
            group.append(track)
        return track

    def _link_shared(self) -> None:
        for group in self.shared.values():
            owner = max(reversed(group), key=lambda track: len(track.data))
            for track in group:
                if track is not owner:
                    if owner.data[:len(track.data)] != track.data:
                        raise MotionFormatError("Tracks sharing key data disagree on it")
                    track.shares = owner

    def _sync(self, offset: int) -> SyncPoints:
        record = self._record("sync", offset)
        count = record["block_count"] * record["point_count"] + 1
        at = record["frames"] if "frames" in record else offset + self.rules.records["sync"].size
        return SyncPoints(record["block_count"], record["point_count"], record["start_point"], record["end_point"],
                          list(self._unpack(f"{count}f", at)))

    def _raw_sync(self, offset: int) -> list[tuple[float, float]]:
        count = self._record("raw_sync", offset)["count"]
        values = self._unpack(f"{count * 2}f", offset + self.rules.records["raw_sync"].size)
        return list(zip(values[0::2], values[1::2]))

    def _gpu(self, offset: int) -> GpuMotion:
        record = self._record("gpu", offset)
        count = record["joint_count"]
        frames = record["frame_count"]
        if "end_frame" in record:
            frames = int(record["end_frame"] - record["start_frame"]) + 1

        def stream(name: str) -> GpuStream:
            fmt = record[f"{name}_format"]
            size = gpu_format(name, fmt)["size"] * count * frames
            start = self.base + record[name]
            data = bytes(self.data[start:start + size])
            if len(data) != size:
                raise MotionFormatError(f"The GPU {name} stream leaves the data")
            ranges = record[f"{name}_ranges"]
            return GpuStream(fmt, data, self._unpack(f"{gpu_range_floats() * count}f", ranges) if ranges else ())

        return GpuMotion(list(self._unpack(f"{count}I", record["joints"])), record["frame_count"],
                         record["frame_rate"], stream("translation"), stream("rotation"),
                         record.get("start_frame", 0.0), record.get("end_frame", 0.0))

    def _joint_poses(self, offset: int) -> list[JointPose]:
        payload = self._record("pose_payload", offset)
        poses = []
        for pose_offset in self._pointers(payload["poses"], payload["pose_count"]):
            pose = self._record("pose", pose_offset)
            count = pose.get("frame_count", payload.get("frame_count", 0))
            poses.append(JointPose(pose["name_hash"], [self._pose_frame(at)
                                                       for at in self._pointers(pose["frames"], count)]))
        return poses

    def _pose_frame(self, offset: int) -> PoseFrame:
        record = self._record("pose_frame", offset)
        at = offset + self.rules.records["pose_frame"].size
        if "values" in record and record["values"] != at:
            raise MotionFormatError("A pose frame's values do not follow its record")
        frame = PoseFrame()
        known = 0
        for name, fmt in pose_flags().items():
            bit = _flag("JointPoseFlags", name)
            known |= bit
            if record["flags"] & bit:
                setattr(frame, _POSE_FIELDS[name], self._unpack(fmt, at))
                at += struct.calcsize(fmt)
        if record["flags"] & ~known:
            raise MotionFormatError(f"Pose frame flags 0x{record['flags']:X} hold unknown bits")
        return frame

    def _append(self, offset: int) -> AppendData:
        header = self._record("append", offset)
        result = AppendData()
        for row in self._table("append_class", offset + header["classes"], header["class_count"]):
            item = AppendClass(row["name_hash"], row["id"])
            item.properties = [self._append_value(offset, value, False) for value in
                               self._table("append_property", offset + row["properties"], row["property_count"])]
            item.arrays = [self._append_value(offset, value, True) for value in
                           self._table("append_array", offset + row["arrays"], row["array_count"])]
            result.classes.append(item)
        result.remaps = [(row["requested"], row["stored"])
                         for row in self._table("append_remap", offset + header["remaps"], header["remap_count"])]
        return result

    def _append_value(self, base: int, row: dict, array: bool) -> AppendValue:
        kind = _kind(row["type"])
        value = AppendValue(row["name_hash"], row["type"], row["primitive"], None)
        if kind == "string":
            value.value = self._string(base + row["value"]) if row["value"] else None
        elif kind == "primitive":
            fmt = _primitive(row["primitive"])
            if array:
                value.value = [self._unpack(fmt, base + row["values"] + index * struct.calcsize(fmt))
                               for index in range(row["count"])]
            else:
                value.value = self._unpack(fmt, base + row["value"]) if row["value"] else None
        elif array:
            value.value = list(self._unpack(f"{row['count']}{kind}", base + row["values"])) if row["count"] else []
        else:
            value.value = row["value"]
        return value


class _ListReader(_Reader):
    def __init__(self, data: bytes):
        self.profile = list_profile(_U32.unpack_from(data, 0)[0])
        super().__init__(data, 0, mot_profile(self.profile.mot).rules.records)
        self.header = self.profile.header.decode(data, 0)

    def read(self) -> MotionList:
        h = self.header
        result = MotionList(self.profile.version, self._string(h["name"]),
                            base_list=self._string(h["base_list"]) if h["base_list"] else None,
                            error_flags=h["error_flags"], joint_mask_mode=h.get("joint_mask_mode", 0),
                            motion_packs=[self._string(at) for at in
                                          self._pointers(h.get("motion_packs", 0), h.get("motion_pack_count", 0))],
                            first_update=h.get("first_update", 0))
        pointers = self._pointers(h["motions"], h["count"])
        rows = self.profile.records["row"].decode_table(self.data, h["rows"], h["count"])
        items: dict[int, object] = {}
        for pointer, row in zip(pointers, rows):
            item = None
            if pointer:
                item = items.get(pointer)
                if item is None:
                    item = items[pointer] = self._item(pointer, row)
            entry = MotionListEntry(row["motion_id"], item, row["type"], row["flags"], row["tag_hash"], row["physics"],
                                    row["joint_mask"],
                                    [self._override(at) for at in self._pointers(row["overrides"], row["override_count"])])
            if "tag0" in row:
                entry.category_tags = tuple(row[f"tag{index}"] for index in range(10))
                entry.additional_sequences = row["additional_sequences"]
                entry.source_bank, entry.source_key = row["source_bank"], row["source_key"]
            result.entries.append(entry)
        return result

    def _override(self, offset: int) -> Sequence:
        if self.profile.override_base == "sequence":
            return _Reader(self.data, offset, self.records)._sequence(0)
        return self._sequence(offset)

    def _item(self, offset: int, row: dict) -> object:
        magic = self.data[offset + 4:offset + 8]
        if row["flags"] & _flag("ElementFlags", "Flags_RefPath"):
            self.profile.records["reference"].decode(self.data, offset)
            return ReferencePath(utf16z(self.data, offset + self.profile.records["reference"].size))
        if magic == b"mot ":
            return read_mot(self.data, offset, standalone=False)
        if magic == b"mtre":
            return read_tree(self.data, offset)
        raise MotionFormatError(f"MOTLIST entry {row['motion_id']} holds an unknown object at 0x{offset:X}")


_POSE_FIELDS = {"Trans": "translation", "Rotation": "rotation", "Scale": "scale"}


def _flag(enum: str, name: str) -> int:
    return next(value for value, text in display_names(enum).items() if text == name)


def _kind(code: int) -> str:
    kind = append_types().get(display_names("AppendPropertyType").get(code, ""))
    if kind is None:
        raise MotionFormatError(f"Append value type {code} is not known")
    return kind


def _primitive(code: int) -> str:
    fmt = append_primitives().get(display_names("AppendPrimitiveType").get(code, ""))
    if fmt is None:
        raise MotionFormatError(f"Append primitive type {code} is not known")
    return fmt
