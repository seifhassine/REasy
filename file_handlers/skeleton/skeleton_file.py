"""See docs/internals/skeleton-format.md."""
from __future__ import annotations

import functools
from dataclasses import dataclass, field

from utils.hash_util import murmur3_hash_utf16le

lazy from file_handlers.layout_codec import RecordCodec, load_table

MAGIC = b"skln"


class SkeletonFormatError(ValueError):
    pass


@functools.cache
def layouts() -> dict:
    return load_table("resources/data/skeletons", "layouts", SkeletonFormatError)


@functools.cache
def codec(name: str, kind: str = "") -> RecordCodec:
    spec = layouts()[name][kind] if kind else layouts()[name]
    return RecordCodec(kind or name, spec["size"], spec["fields"], SkeletonFormatError)


def joint_codec(version: int) -> RecordCodec:
    if (kind := layouts()["versions"].get(str(version))) is None:
        raise SkeletonFormatError(f"skeleton version {version} has no layout ({', '.join(layouts()['versions'])} do)")
    return codec("joints", kind)


@dataclass(slots=True, eq=False)
class Joint:
    name: str
    parent: Joint | None = None
    mirror: Joint | None = None
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    segment_scale: bool = False

    @property
    def name_hash(self) -> int:
        return murmur3_hash_utf16le(self.name)


@dataclass(slots=True)
class SkeletonFile:
    version: int
    joints: list[Joint] = field(default_factory=list)
    err_flags: int = 0
    master_size: int = 0

    @property
    def segment_scales(self) -> bool:
        return "segment_scale" in joint_codec(self.version).names

    def rows(self) -> dict[int, int]:
        return {id(joint): row for row, joint in enumerate(self.joints)} | {id(None): -1}


def _hash_table(joints: list[Joint]) -> list[dict]:
    return [{"hash": value, "index": row} for value, row in sorted(
        (joint.name_hash, row) for row, joint in enumerate(joints))]


def read_skeleton(data: bytes) -> SkeletonFile:
    header = codec("header").decode(data, 0)
    records = joint_codec(header["version"]).decode_table(data, header["joints"], header["count"])
    joints = []
    for row, record in enumerate(records):
        end = at = record["name"]
        while data[end:end + 2] not in (b"\0\0", b""):
            end += 2
        if not -1 <= record["parent"] < row or not -1 <= record["symmetry"] < len(records):
            raise SkeletonFormatError(f"joint {row} names joint {record['parent']} as its parent and "
                                      f"{record['symmetry']} as its mirror")
        joints.append(Joint(data[at:end].decode("utf-16-le"), None, None, record["position"], record["rotation"],
                            record["scale"], bool(record.get("segment_scale"))))
        if joints[-1].name_hash != record["name_hash"]:
            raise SkeletonFormatError(f"joint {row} ({joints[-1].name}) stores the name hash "
                                      f"0x{record['name_hash']:08X}, not its name's")
    for joint, record in zip(joints, records):
        joint.parent, joint.mirror = (None if row < 0 else joints[row] for row in (record["parent"],
                                                                                  record["symmetry"]))
    if codec("hash").decode_table(data, header["hashes"], len(joints)) != _hash_table(joints):
        raise SkeletonFormatError("the hash table is not the joints' name hashes in ascending order")
    return SkeletonFile(header["version"], joints, header["err_flags"], header["master_size"])


def write_skeleton(skeleton: SkeletonFile) -> bytes:
    joints, rows, records = skeleton.joints, skeleton.rows(), joint_codec(skeleton.version)
    header, align = codec("header"), layouts()["table_align"]
    start = -(-header.size // align) * align
    hashes_at = start + records.size * len(joints)
    name_at, names, table = hashes_at + codec("hash").size * len(joints), [], []
    for row, joint in enumerate(joints):
        if not rows[id(joint.parent)] < row:
            raise SkeletonFormatError(f"joint {row} ({joint.name}) comes before its parent")
        names.append(joint.name.encode("utf-16-le") + b"\0\0")
        table.append(records.encode({
            "name": name_at, "name_hash": joint.name_hash, "parent": rows[id(joint.parent)],
            "symmetry": rows[id(joint.mirror)], "position": joint.position, "rotation": joint.rotation,
            "scale": joint.scale, "segment_scale": joint.segment_scale}))
        name_at += len(names[-1])
    return b"".join((header.encode({
        "version": skeleton.version, "err_flags": skeleton.err_flags, "master_size": skeleton.master_size,
        "joints": start, "hashes": hashes_at, "count": len(joints)}), bytes(start - header.size), *table,
        *map(codec("hash").encode, _hash_table(joints)), *names))
