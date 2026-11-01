"""See docs/internals/mot-format.md."""

from __future__ import annotations

import struct

from utils.hash_util import murmur3_hash_utf16le
from utils.hex_util import align as _align

lazy from file_handlers.clip.encode import write_motclip

lazy from .tree import write_tree

from .decode import TRANSFORMS, _FRAME_FORMATS, _POSE_FIELDS, _flag, _kind, _primitive
from .errors import MotionFormatError
from .model import AppendData, Motion, MotionList, MotionTree, ReferencePath, Sequence, Skeleton, Track
from .rules import (
    MotProfile,
    encoding,
    frame_types,
    list_profile,
    mot_profile,
    pose_flags,
    solver_codes,
)

def write_mot(motion: Motion, *, embedded: bool = False, list_owner: bool = False) -> bytes:
    return _MotWriter(motion, mot_profile(motion.version), embedded, list_owner).write()


def write_motlist(motion_list: MotionList) -> bytes:
    return _ListWriter(motion_list).write()


def write_file(root: Motion | MotionList | MotionTree) -> bytes:
    if isinstance(root, MotionList):
        return write_motlist(root)
    if isinstance(root, MotionTree):
        return write_tree(root)
    return write_mot(root)


def _utf16z(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\0\0"


class _Writer:
    def __init__(self, records: dict, size: int = 0):
        self.records = records
        self.out = bytearray(size)

    def _pad(self, alignment: int) -> int:
        self.out.extend(bytes(_align(len(self.out), alignment) - len(self.out)))
        return len(self.out)

    def _put(self, data: bytes, align: int = 1) -> int:
        at = self._pad(align)
        self.out.extend(data)
        return at

    def _set(self, at: int, data: bytes) -> None:
        self.out[at:at + len(data)] = data

    def _record(self, name: str, record: dict) -> bytes:
        return self.records[name].encode(record)

    def _pointer_table(self, count: int) -> int:
        return self._put(bytes(8 * count))

    def _fill_pointers(self, table: int, offsets: list[int]) -> None:
        self._set(table, struct.pack(f"<{len(offsets)}Q", *offsets))

    def _sequence(self, sequence: Sequence, clip_offset: int, relative: bool = False) -> int:
        at = self._pad(16)
        base = at if relative else 0
        self.out.extend(bytes(clip_offset))
        clip = self._put(write_motclip(sequence.clip, len(self.out) - base))
        tracks = self._pad(16)
        for row in sequence.tracks:
            self._put(self._record("sequence_track", {
                "id": row.id, "filter0": row.filters[0], "filter1": row.filters[1], "filter2": row.filters[2],
                "invalid0": row.invalid[0], "invalid1": row.invalid[1], "invalid2": row.invalid[2]}))
        self._set(at, self._record("sequence", {
            "clip": clip - base, "tracks": tracks - base, "track_count": len(sequence.tracks),
            "category": sequence.category}))
        return at


class _MotWriter(_Writer):
    def __init__(self, motion: Motion, profile: MotProfile, embedded: bool, list_owner: bool):
        super().__init__(profile.rules.records, profile.rules.header.size)
        self.motion, self.profile, self.rules, self.embedded = motion, profile, profile.rules, embedded
        self.list_owner = list_owner
        self.pointers: dict[str, int] = {}
        self.frame_type, self.frame_format = self._frame_type()

    def _frame_type(self) -> tuple[int, str]:
        extent = max([self.motion.end_frame, *(frame for track in self._tracks() for frame in track.frames)])
        name = "u8" if extent < 255 else "u16" if extent < 65535 else "u32"
        return next(code for code, kind in frame_types().items() if kind == name), _FRAME_FORMATS[name]

    def _tracks(self):
        for channel in self.motion.channels:
            for name in TRANSFORMS:
                if (track := getattr(channel, name)) is not None:
                    yield track
        for prop in self.motion.properties:
            yield prop.track

    def write(self) -> bytes:
        motion = self.motion
        dependent = motion.skeleton is None and motion.joint_count > 0
        if dependent and not self.embedded:
            raise MotionFormatError("A standalone MOT must own its skeleton")
        payload = motion.joint_poses is not None or motion.gpu is not None
        order = self.rules.payload_order if payload else self.rules.order
        align = self.rules.payload_align if payload else self.rules.align
        logical_end = len(self.out)
        for block in order:
            if block == "skeleton" and dependent:
                self.pointers["skeleton"] = _align(len(self.out), 16)
                continue
            writer = getattr(self, f"_write_{block}")
            if not writer(True):
                continue
            self._pad(align.get(block, 16))
            writer(False)
            if block != "skeleton":
                logical_end = len(self.out)
        if self.embedded:
            self._pad(16)
            owner = motion.skeleton is not None or self.list_owner
        else:
            owner = motion.gpu is not None
        self._header(logical_end if self.rules.master_size and not owner else 0)
        return bytes(self.out)

    def _header(self, master_size: int) -> None:
        motion, p = self.motion, self.pointers
        self._set(0, self.rules.header.encode({
            "version": motion.version, "error_flags": motion.error_flags, "master_size": master_size,
            "skeleton": p.get("skeleton", 0), "animation": p.get("animation", 0), "properties": p.get("properties", 0),
            "sequences": p.get("sequences", 0), "character": p.get("character", 0), "sync": p.get("sync", 0),
            "raw_sync": p.get("raw_sync", 0), "append": p.get("append", 0), "name": p["name"],
            "end_frame": motion.end_frame, "loop_frame": motion.loop_frame, "raw_start_frame": motion.raw_start_frame,
            "raw_end_frame": motion.raw_end_frame,
            "joint_count": len(motion.skeleton.joints) if motion.skeleton else motion.joint_count,
            "animation_count": len(motion.channels), "sequence_count": len(motion.sequences),
            "sync_count": len(motion.sync), "raw_sync_count": len(motion.raw_sync), "fps": motion.fps,
            "property_count": len(motion.properties), "attributes": motion.attributes}))

    # Blocks: probe=True asks whether the block is written.

    def _write_name(self, probe: bool) -> bool:
        if not probe:
            self.pointers["name"] = self._put(_utf16z(self.motion.name))
        return True

    def _write_skeleton(self, probe: bool) -> bool:
        skeleton = self.motion.skeleton
        if probe:
            return skeleton is not None and bool(skeleton.joints)
        self.pointers["skeleton"] = self._skeleton(skeleton)
        return True

    def _skeleton(self, skeleton: Skeleton) -> int:
        joints = skeleton.joints
        base = len(self.out)
        table = base + self.records["skeleton"].size
        size = self.records["joint"].size
        index = {id(joint): position for position, joint in enumerate(joints)}

        def at(joint) -> int:
            return table + size * index[id(joint)] if joint is not None else 0

        sibling = {id(left): right for joint in joints for left, right in zip(joint.children, joint.children[1:])}
        names_at = table + size * len(joints)
        name_offsets: dict[str, int] = {}
        names = bytearray()
        for joint in joints:
            name_offsets.setdefault(joint.name, names_at + len(names))
            names.extend(_utf16z(joint.name))
        self._put(self._record("skeleton", {"joints": table, "joint_count": len(joints)}))
        for joint in joints:
            self._put(self._record("joint", {
                "name": name_offsets[joint.name], "parent": at(joint.parent),
                "child": at(joint.children[0] if joint.children else None), "sibling": at(sibling.get(id(joint))),
                "translation": joint.translation, "rotation": joint.rotation, "joint_id": joint.joint_id,
                "name_hash": murmur3_hash_utf16le(joint.name), "extra_type": joint.extra_type}))
        self._put(bytes(names))
        return base

    def _write_animation(self, probe: bool) -> bool:
        channels = self.motion.channels
        if probe:
            return bool(channels)
        base = self.pointers["animation"] = len(self.out)
        legacy = self.rules.tracks == "legacy"
        track_size = self.records["track"].size
        tracks_at = base + self.records["channel"].size * len(channels)
        if legacy:
            tracks_at = _align(tracks_at, 16)
        ordered, at = [], tracks_at
        for channel in channels:
            present = [getattr(channel, name) for name in TRANSFORMS]
            self._put(self._record("channel", {
                "joint_id": channel.joint_id, "name_hash": channel.name_hash, "tracks": at,
                "transforms": sum(1 << bit for bit, track in enumerate(present) if track is not None),
                "weight": channel.weight if legacy else round(channel.weight * 255)}))
            for track in present:
                if track is not None:
                    ordered.append(track)
                    at += track_size
        self._pad(16 if legacy else 1)
        self.out.extend(bytes(track_size * len(ordered)))
        places = self._legacy_payloads(ordered) if legacy else self._modern_payloads(ordered)
        for position, (track, place) in enumerate(zip(ordered, places)):
            self._set(tracks_at + position * track_size, self._track_record(track, place))
        return True

    def _legacy_payloads(self, tracks: list[Track]) -> list[tuple[int, int, int]]:
        places = []
        for track in tracks:
            frames = self._pad(16)
            self._put(self._frames(track))
            values = self._pad(16)
            self._put(track.data)
            params = 0
            if track.parameters:
                params = self._pad(16)
                self._put(self._parameters(track))
            places.append((frames, values, params))
        return places

    def _modern_payloads(self, tracks: list[Track]) -> list[tuple[int, int, int]]:
        last = {id(track.shares or track): position for position, track in enumerate(tracks)}
        places, written = [], {}
        for position, track in enumerate(tracks):
            holder = track.shares or track
            frames = params = 0
            if track.key_count > 1:
                frames = self._pad(4)
                self._put(self._frames(track))
            if last[id(holder)] == position:
                written[id(holder)] = self._pad(4)
                self._put(holder.data)
            if track.parameters:
                params = self._pad(4)
                self._put(self._parameters(track))
            places.append((frames, holder, params))
        return [(frames, written[id(holder)], params) for frames, holder, params in places]

    def _frames(self, track: Track) -> bytes:
        return struct.pack(f"<{len(track.frames)}{self.frame_format}", *track.frames)

    @staticmethod
    def _parameters(track: Track) -> bytes:
        return struct.pack(f"<{len(track.parameters)}f", *track.parameters)

    def _track_record(self, track: Track, place: tuple[int, int, int]) -> bytes:
        enc = encoding(self.profile.keys, track.kind, track.encoding)
        stored = (track.shares or track).data
        if len(stored) < enc.size * track.key_count or len(track.parameters) != enc.params:
            raise MotionFormatError(f"A {track.kind} track's keys do not fit encoding 0x{track.encoding:02X}")
        frames, values, params = place
        return self._record("track", {
            "solver": solver_codes()[track.kind], "encoding": track.encoding, "frame_type": self.frame_type,
            "key_count": track.key_count, "frame_rate": self.motion.fps, "max_frame": track.max_frame,
            "frames": frames, "values": values, "parameters": params})

    def _write_properties(self, probe: bool) -> bool:
        props = self.motion.properties
        if probe:
            return bool(props)
        base = self.pointers["properties"] = len(self.out)
        size = self.records["property"].size
        legacy = self.rules.tracks == "legacy"
        align = 16 if legacy else 4
        self.out.extend(bytes(size * len(props)))
        for position, prop in enumerate(props):
            track = prop.track
            header = self._put(bytes(self.records["track"].size))
            frames = self._pad(align if legacy else 1)
            self._put(self._frames(track))
            values = self._pad(align)
            self._put(track.data)
            params = header if legacy else 0
            if track.parameters:
                params = self._pad(align)
                self._put(self._parameters(track))
            self._set(header, self._track_record(track, (frames, values, params)))
            self._set(base + position * size, self._record("property", {"track": header, "name_hash": prop.name_hash}))
        return True

    def _write_character(self, probe: bool) -> bool:
        motion = self.motion
        if probe:
            return motion.jmap_path is not None or motion.joint_poses is not None or motion.gpu is not None
        self.pointers["character"] = len(self.out)
        if motion.gpu is not None:
            self._gpu()
        elif motion.joint_poses is not None:
            self._joint_poses()
        else:
            self._put(_utf16z(motion.jmap_path))
        return True

    def _gpu(self) -> None:
        gpu = self.motion.gpu
        base = self._put(bytes(self.records["gpu"].size))
        record = {"joints": self._put(struct.pack(f"<{len(gpu.joint_hashes)}I", *gpu.joint_hashes)),
                  "joint_count": len(gpu.joint_hashes), "frame_count": gpu.frame_count,
                  "frame_rate": gpu.frame_rate, "start_frame": gpu.start_frame, "end_frame": gpu.end_frame}
        for name in ("translation", "rotation"):
            stream = getattr(gpu, name)
            record[name] = self._pad(16)
            self._put(stream.data)
            record[f"{name}_ranges"] = 0
            if stream.ranges:
                record[f"{name}_ranges"] = self._pad(16)
                self._put(struct.pack(f"<{len(stream.ranges)}f", *stream.ranges))
            record[f"{name}_format"] = stream.format
        self._set(base, self._record("gpu", record))

    def _joint_poses(self) -> None:
        poses = self.motion.joint_poses
        base = self._put(bytes(self.records["pose_payload"].size))
        table = self._pointer_table(len(poses))
        pose_at, frame_at = [], {}
        for pose in poses:
            at = self._pad(16)
            pose_at.append(at)
            self.out.extend(bytes(self.records["pose"].size))
            frames_table = self._pointer_table(len(pose.frames))
            offsets = []
            for frame in pose.frames:
                key = (self._pose_flags(frame), self._pose_values(frame))
                if key not in frame_at:
                    start = frame_at[key] = self._pad(16)
                    record = {"flags": key[0], "values": start + self.records["pose_frame"].size}
                    self._put(self._record("pose_frame", record) + key[1])
                offsets.append(frame_at[key])
            self._fill_pointers(frames_table, offsets)
            self._set(at, self._record("pose", {"name_hash": pose.name_hash, "frame_count": len(pose.frames),
                                                "frames": frames_table}))
        self._fill_pointers(table, pose_at)
        self._set(base, self._record("pose_payload", {"poses": table, "pose_count": len(poses),
                                                      "frame_count": len(poses[0].frames) if poses else 0}))

    @staticmethod
    def _pose_flags(frame) -> int:
        return sum(_flag("JointPoseFlags", name) for name, field in _POSE_FIELDS.items()
                   if getattr(frame, field) is not None)

    @staticmethod
    def _pose_values(frame) -> bytes:
        return b"".join(struct.pack("<" + fmt, *getattr(frame, _POSE_FIELDS[name]))
                        for name, fmt in pose_flags().items() if getattr(frame, _POSE_FIELDS[name]) is not None)

    def _write_sequences(self, probe: bool) -> bool:
        sequences = self.motion.sequences
        if probe:
            return bool(sequences)
        table = self.pointers["sequences"] = self._pointer_table(len(sequences))
        self._fill_pointers(table, [self._sequence(sequence, self.rules.sequence_clip) for sequence in sequences])
        return True

    def _write_sync(self, probe: bool) -> bool:
        sync, raw = self.motion.sync, self.motion.raw_sync
        if probe:
            return bool(sync or raw)
        if sync:
            table = self.pointers["sync"] = self._pointer_table(len(sync))
            offsets = []
            for points in sync:
                at = self._pad(self.rules.align["sync"])
                record = {"block_count": points.block_count, "point_count": points.point_count,
                          "start_point": points.start_point, "end_point": points.end_point,
                          "frames": at + self.records["sync"].size}
                self._put(self._record("sync", record) + struct.pack(f"<{len(points.frames)}f", *points.frames))
                offsets.append(at)
            self._fill_pointers(table, offsets)
        if raw:
            self._pad(8)
            table = self.pointers["raw_sync"] = self._pointer_table(len(raw))
            offsets = []
            for entries in raw:
                offsets.append(self._pad(8))
                self._put(self._record("raw_sync", {"count": len(entries)}) +
                          b"".join(struct.pack("<2f", *entry) for entry in entries))
            self._fill_pointers(table, offsets)
        return True

    def _write_append(self, probe: bool) -> bool:
        append = self.motion.append
        if probe:
            return append is not None
        self.pointers["append"] = self._put(_AppendWriter(self.records, append).write())
        return True


class _AppendWriter(_Writer):
    def __init__(self, records: dict, append: AppendData):
        super().__init__(records)
        self.append = append

    def write(self) -> bytes:
        records = self.records
        classes = self.append.classes
        self.out.extend(bytes(records["append"].size))
        class_table = self._pad(16)
        self.out.extend(bytes(records["append_class"].size * len(classes)))
        tables = []
        for item in classes:
            props = self._pad(16)
            self.out.extend(bytes(records["append_property"].size * len(item.properties)))
            tables.append((props, self._put(bytes(records["append_array"].size * len(item.arrays)))))
        self._pad(16)
        array_values = []
        for item in classes:
            for value in item.arrays:
                array_values.append(self._pad(16))
                self.out.extend(self._array_bytes(value))
        scalar_values = []
        for item in classes:
            for value in item.properties:
                kind = _kind(value.type)
                if kind == "string" and value.value is not None:
                    scalar_values.append(self._put(_utf16z(value.value)))
                elif kind == "primitive" and value.value is not None:
                    scalar_values.append(self._pad(16))
                    self.out.extend(struct.pack("<" + _primitive(value.primitive), *value.value))
                elif kind in ("string", "primitive"):
                    scalar_values.append(0)
                else:
                    scalar_values.append(value.value)
        remaps = 0
        if self.append.remaps:
            remaps = self._pad(16)
            for requested, stored in self.append.remaps:
                self._put(records["append_remap"].encode({"requested": requested, "stored": stored}))
        arrays_iter, scalars_iter = iter(array_values), iter(scalar_values)
        for position, (item, (props, arrays)) in enumerate(zip(classes, tables)):
            self._set(class_table + position * records["append_class"].size, records["append_class"].encode({
                "properties": props, "arrays": arrays, "property_count": len(item.properties),
                "array_count": len(item.arrays), "name_hash": item.name_hash, "id": item.id}))
            for index, value in enumerate(item.properties):
                self._set(props + index * records["append_property"].size, records["append_property"].encode({
                    "value": next(scalars_iter), "name_hash": value.name_hash, "type": value.type,
                    "primitive": value.primitive}))
            for index, value in enumerate(item.arrays):
                self._set(arrays + index * records["append_array"].size, records["append_array"].encode({
                    "values": next(arrays_iter), "name_hash": value.name_hash, "count": len(value.value),
                    "type": value.type, "primitive": value.primitive}))
        self._set(0, records["append"].encode({"classes": class_table, "remaps": remaps, "class_count": len(classes),
                                               "remap_count": len(self.append.remaps)}))
        return bytes(self.out)

    def _array_bytes(self, value) -> bytes:
        kind = _kind(value.type)
        if kind == "primitive":
            fmt = _primitive(value.primitive)
            return b"".join(struct.pack("<" + fmt, *item) for item in value.value)
        if kind == "string":
            raise MotionFormatError("Append data cannot hold string arrays")
        return struct.pack(f"<{len(value.value)}{kind}", *value.value)


class _ListWriter(_Writer):
    def __init__(self, motion_list: MotionList):
        self.profile = list_profile(motion_list.version)
        self.mot_rules = mot_profile(self.profile.mot).rules
        super().__init__(self.mot_rules.records, self.profile.header.size)
        self.list = motion_list
        self.first_motion = True

    def write(self) -> bytes:
        lst, profile = self.list, self.profile
        header = {"version": lst.version, "error_flags": lst.error_flags, "count": len(lst.entries),
                  "joint_mask_mode": lst.joint_mask_mode, "first_update": lst.first_update,
                  "motion_pack_count": len(lst.motion_packs), "motion_packs": 0}
        header["name"] = self._put(_utf16z(lst.name))
        header["base_list"] = self._put(_utf16z(lst.base_list)) if lst.base_list is not None else 0
        if lst.motion_packs:
            header["motion_packs"] = self._pad(8)
            table = self._pointer_table(len(lst.motion_packs))
            self._fill_pointers(table, [self._put(_utf16z(path)) for path in lst.motion_packs])
        self._pad(16)
        header["motions"] = self._pointer_table(len(lst.entries))
        self._pad(16)
        placed: dict[int, int] = {}
        for entry in lst.entries:
            if entry.item is not None and id(entry.item) not in placed:
                placed[id(entry.item)] = self._item(entry)
        self._fill_pointers(header["motions"], [placed.get(id(entry.item), 0) if entry.item is not None else 0
                                                for entry in lst.entries])
        header["rows"] = self._pad(16)
        row_size = profile.records["row"].size
        self.out.extend(bytes(row_size * len(lst.entries)))
        for index, entry in enumerate(lst.entries):
            overrides = 0
            if entry.overrides:
                overrides = self._pad(8)
                table = self._pointer_table(len(entry.overrides))
                self._fill_pointers(table, [self._sequence(sequence, self.mot_rules.sequence_clip,
                                                           profile.override_base == "sequence")
                                            for sequence in entry.overrides])
            row = {"overrides": overrides, "motion_id": entry.motion_id, "type": entry.type, "flags": entry.flags,
                   "tag_hash": entry.tag_hash, "physics": entry.physics, "override_count": len(entry.overrides),
                   "joint_mask": entry.joint_mask, "additional_sequences": entry.additional_sequences,
                   "source_bank": entry.source_bank, "source_key": entry.source_key}
            row.update((f"tag{position}", tag) for position, tag in enumerate(entry.category_tags))
            self._set(header["rows"] + index * row_size, profile.records["row"].encode(row))
        self._set(0, profile.header.encode(header))
        return bytes(self.out)

    def _item(self, entry) -> int:
        item = entry.item
        at = self._pad(16)
        if isinstance(item, Motion):
            self._put(write_mot(item, embedded=True, list_owner=self.first_motion))
            self.first_motion = False
        elif isinstance(item, MotionTree):
            self._put(write_tree(item))
        elif isinstance(item, ReferencePath):
            self._put(self.profile.records["reference"].encode({}) + _utf16z(item.path))
        else:
            raise MotionFormatError(f"MOTLIST entry {entry.motion_id} holds an unknown object")
        self._pad(16)
        return at
