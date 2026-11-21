"""See docs/internals/maba-format.md."""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

import numpy as np

from utils.hash_util import murmur3_hash_utf16le

MAGIC = b"maba"
VERSION = 4
ORACLE = murmur3_hash_utf16le("Oracle")
_HEADER = struct.Struct("<I4sQQI4x")
_GROUP = struct.Struct("<IIQ")
_RECORD = struct.Struct("<QQIIQ")
_DATABASE = struct.Struct("<QQQQIII")
_KEY = struct.Struct("<QQIIIIIIQ")
_RANGE = struct.Struct("<QIIIIII")
_JOINT = re.compile(r"(.*?)(?:[<>@]-?\d+)?")


@dataclass(slots=True)
class OrclKey:
    name: str
    command: int
    size: int

    @property
    def joints(self) -> list[str]:
        return [_JOINT.fullmatch(token).group(1) for token in self.name.split(" ")[1:]]


@dataclass(slots=True, eq=False)
class MotionRange:
    bank_id: int
    motion_id: int
    motion_frames: int
    tags: list[int]
    frames: np.ndarray
    values: np.ndarray
    tags_shared_with: OrclDatabase | None = field(default=None, repr=False)


@dataclass(slots=True, eq=False)
class OrclDatabase:
    name: str
    keys: list[OrclKey] = field(default_factory=list)
    ranges: list[MotionRange] = field(default_factory=list)

    @property
    def frame_size(self) -> int:
        return sum(key.size for key in self.keys)


@dataclass(slots=True)
class MabaFile:
    databases: list[OrclDatabase] = field(default_factory=list)


def read_maba(data: bytes) -> MabaFile:
    version, magic, _, groups, group_count = _HEADER.unpack_from(data)
    if magic != MAGIC or version != VERSION:
        raise ValueError(f"not a maba motion analyzer bank of version {VERSION}")
    kind, count, records = _GROUP.unpack_from(data, groups)
    if group_count != 1 or kind != ORACLE:
        raise ValueError(f"the bank holds {group_count} database groups of type 0x{kind:08X}; REasy knows one group "
                         "of Oracle databases")
    databases, pools = [], {}
    for index in range(count):
        name_at, block, name_hash, _, _ = _RECORD.unpack_from(data, records + index * _RECORD.size)
        if name_hash != murmur3_hash_utf16le(name := _text(data, name_at)):
            raise ValueError(f"database {index} {name!r} has the hash 0x{name_hash:08X} of another name")
        database = OrclDatabase(name)
        pools[_read_database(data, block, database, pools)] = database
        databases.append(database)
    return MabaFile(databases)


def _read_database(data: bytes, block: int, database: OrclDatabase, pools: dict) -> int:
    keys_at, ranges_at, frames_at, values_at, key_count, range_count, _ = _DATABASE.unpack_from(data, block)
    for index in range(key_count):
        joints_at, name_at, name_hash, command, _, joint_count, size, offset, _ = _KEY.unpack_from(
            data, keys_at + index * _KEY.size)
        key = OrclKey(_text(data, name_at), command, size)
        if (name_hash, offset, list(struct.unpack_from(f"<{joint_count}I", data, joints_at))) != (
                murmur3_hash_utf16le(key.name), database.frame_size, [*map(murmur3_hash_utf16le, key.joints)]):
            raise ValueError(f"key {index} of {database.name!r} differs from its name {key.name!r} or column")
        database.keys.append(key)
    width, row = database.frame_size, 0
    for index in range(range_count):
        tags_at, start, end, bank_id, motion_id, motion_frames, tag_count = _RANGE.unpack_from(
            data, ranges_at + index * _RANGE.size)
        if start != row or end < start:
            raise ValueError(f"motion {index} of {database.name!r} holds rows {start}-{end} after row {row}")
        rows, row = end - start, end
        database.ranges.append(MotionRange(
            bank_id, motion_id, motion_frames, list(struct.unpack_from(f"<{tag_count}I", data, tags_at)),
            np.frombuffer(data, "<u4", rows, frames_at + start * 4).copy(),
            np.frombuffer(data, "<f4", rows * width, values_at + start * width * 4).reshape(rows, width).copy(),
            None if tag_count else pools.get(tags_at)))
    return ranges_at + range_count * _RANGE.size


def write_maba(maba: MabaFile) -> bytes:
    out = bytearray(_HEADER.size + _GROUP.size)
    _HEADER.pack_into(out, 0, VERSION, MAGIC, 0, _HEADER.size, 1)
    _GROUP.pack_into(out, _HEADER.size, ORACLE, len(maba.databases), len(out))
    records = len(out)
    out += bytes(_RECORD.size * len(maba.databases))
    names = []
    for database in maba.databases:
        names.append(len(out))
        out += database.name.encode("utf-16le") + b"\0\0"
    pools = {}
    for index, database in enumerate(maba.databases):
        _RECORD.pack_into(out, records + index * _RECORD.size, names[index], len(out),
                          murmur3_hash_utf16le(database.name), 0, 0)
        pools[id(database)] = _write_database(out, database, pools)
    return bytes(out)


def _write_database(out: bytearray, database: OrclDatabase, pools: dict) -> int:
    block = len(out)
    out += bytes(_DATABASE.size + -(block + _DATABASE.size) % 16)
    keys_at = len(out)
    out += bytes(_KEY.size * len(database.keys))
    joints = []
    for key in database.keys:
        joints.append(len(out))
        out += struct.pack(f"<{len(key.joints)}I", *map(murmur3_hash_utf16le, key.joints))
    column = 0
    for index, key in enumerate(database.keys):
        _KEY.pack_into(out, keys_at + index * _KEY.size, joints[index], len(out), murmur3_hash_utf16le(key.name),
                       key.command, murmur3_hash_utf16le(key.name.split(" ")[0]), len(key.joints), key.size, column, 0)
        out += key.name.encode("utf-16le") + b"\0\0"
        column += key.size
    out += bytes(-len(out) % 8)
    ranges_at, row = len(out), 0
    out += bytes(_RANGE.size * len(database.ranges))
    for index, motion in enumerate(database.ranges):
        if motion.values.shape != (rows := len(motion.frames), column):
            raise ValueError(f"motion {index} of {database.name!r} holds {motion.values.shape} values for {rows} "
                             f"rows of {column}")
        shared = None if motion.tags else pools.get(id(motion.tags_shared_with))
        _RANGE.pack_into(out, ranges_at + index * _RANGE.size, len(out) if shared is None else shared, row,
                         row + rows, motion.bank_id, motion.motion_id, motion.motion_frames, len(motion.tags))
        out += struct.pack(f"<{len(motion.tags)}I", *motion.tags)
        row += rows
    frames_at = len(out)
    out += b"".join(motion.frames.astype("<u4").tobytes() for motion in database.ranges)
    out += bytes(-len(out) % 16)
    values_at = len(out)
    out += b"".join(motion.values.astype("<f4").tobytes() for motion in database.ranges)
    _DATABASE.pack_into(out, block, keys_at, ranges_at, frames_at, values_at, len(database.keys),
                        len(database.ranges), row)
    return ranges_at + len(database.ranges) * _RANGE.size


def _text(data: bytes, at: int) -> str:
    end = at
    while data[end:end + 2] not in (b"\0\0", b""):
        end += 2
    return data[at:end].decode("utf-16le")
