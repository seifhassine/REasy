from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from ..sequence.model import SequenceData


class TrackFamily(IntEnum):
    FLOAT = 0
    VECTOR3 = 1
    QUATERNION = 2


class JointMapExtraType(IntEnum):
    DEFAULT = 0
    EXTRA_JOINT_INCLUDE_DEFORM = 1
    INCLUDE_EXTRA_VALUE = 2


Vector3Value = tuple[float, float, float]
QuaternionValue = tuple[float, float, float, float]
TrackValue = float | Vector3Value | QuaternionValue


@dataclass(slots=True)
class KeyTrack:
    """Semantic key values independent of v65 storage and compression.

    Quaternion values contain canonical nonnegative W even though v65 stores
    only XYZ for every encoding observed in DMC5. Compression identifiers,
    quantized codewords, denormalization parameters, offsets, and counts are
    writer-derived and intentionally absent from this model.
    """

    family: TrackFamily
    frames: list[int]
    values: list[TrackValue]
    max_frame: float | None = None


@dataclass(slots=True, eq=False)
class Joint:
    name: str
    parent: "Joint | None" = None
    children: list["Joint"] = field(default_factory=list)
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    joint_map_extra_type: JointMapExtraType = JointMapExtraType.DEFAULT


@dataclass(slots=True, eq=False)
class Skeleton:
    joints: list[Joint] = field(default_factory=list)


@dataclass(slots=True)
class AnimationNode:
    """Authored joint channels and their shared blend weight."""

    joint: Joint
    weight: float = 1.0
    translation: KeyTrack | None = None
    rotation: KeyTrack | None = None
    scale: KeyTrack | None = None


@dataclass(slots=True)
class PropertyTrack:
    target_name_hash: int
    track: KeyTrack


@dataclass(slots=True)
class SyncPointGrid:
    block_count: int
    point_count: int
    frames: list[float]


class AppendPropertyType(IntEnum):
    INT32 = 7
    UINT32 = 8
    UINT64 = 10
    STRING = 15


@dataclass(slots=True)
class AppendProperty:
    name_hash: int
    property_type: AppendPropertyType
    value: int | str


@dataclass(slots=True)
class AppendArray:
    name_hash: int
    property_type: AppendPropertyType
    values: list[int] = field(default_factory=list)


@dataclass(slots=True)
class AppendClass:
    name_hash: int
    authored_id: int
    properties: list[AppendProperty] = field(default_factory=list)
    arrays: list[AppendArray] = field(default_factory=list)


@dataclass(slots=True)
class PropertyHashRemap:
    requested_hash: int
    stored_hash: int


@dataclass(slots=True)
class MotionAppend:
    classes: list[AppendClass] = field(default_factory=list)
    remaps: list[PropertyHashRemap] = field(default_factory=list)


@dataclass(slots=True)
class Motion:
    name: str
    end_frame: float = 0.0
    looping: bool = False
    raw_start_frame: float = 0.0
    raw_end_frame: float = 0.0
    skeleton: Skeleton | None = None
    animation_nodes: list[AnimationNode] = field(default_factory=list)
    property_tracks: list[PropertyTrack] = field(default_factory=list)
    sequences: list[SequenceData] = field(default_factory=list)
    character_path: str | None = None
    sync_points: list[SyncPointGrid] = field(default_factory=list)
    append: MotionAppend | None = None
