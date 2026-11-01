"""See docs/internals/mot-format.md."""

from __future__ import annotations

from dataclasses import dataclass, field

lazy from file_handlers.clip.model import Timeline


@dataclass(eq=False, slots=True)
class Track:
    kind: str
    frames: list[int]
    encoding: int
    parameters: tuple[float, ...]
    data: bytes
    max_frame: float = 0.0
    shares: Track | None = None

    @property
    def key_count(self) -> int:
        return max(len(self.frames), 1)


@dataclass(eq=False, slots=True)
class Joint:
    name: str
    parent: Joint | None = None
    children: list[Joint] = field(default_factory=list)
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    joint_id: int = 0
    extra_type: int = 0


@dataclass(eq=False, slots=True)
class Skeleton:
    joints: list[Joint] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class JointChannels:
    joint_id: int
    name_hash: int
    weight: float = 1.0
    translation: Track | None = None
    rotation: Track | None = None
    scale: Track | None = None


@dataclass(eq=False, slots=True)
class PropertyTrack:
    name_hash: int
    track: Track


@dataclass(eq=False, slots=True)
class SequenceTrack:
    id: int = 0xFFFFFFFF
    filters: tuple[int, int, int] = (0, 0, 0)
    invalid: tuple[int, int, int] = (0, 0, 0)


@dataclass(eq=False, slots=True)
class Sequence:
    category: int
    clip: Timeline
    tracks: list[SequenceTrack] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class SyncPoints:
    block_count: int
    point_count: int
    start_point: int
    end_point: int
    frames: list[float]


@dataclass(eq=False, slots=True)
class AppendValue:
    name_hash: int
    type: int
    primitive: int
    value: object


@dataclass(eq=False, slots=True)
class AppendClass:
    name_hash: int
    id: int
    properties: list[AppendValue] = field(default_factory=list)
    arrays: list[AppendValue] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class AppendData:
    classes: list[AppendClass] = field(default_factory=list)
    remaps: list[tuple[int, int]] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class PoseFrame:
    translation: tuple[float, float, float] | None = None
    rotation: tuple[float, float, float, float] | None = None
    scale: tuple[float, float, float] | None = None


@dataclass(eq=False, slots=True)
class JointPose:
    name_hash: int
    frames: list[PoseFrame] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class GpuStream:
    format: int
    data: bytes
    ranges: tuple[float, ...] = ()


@dataclass(eq=False, slots=True)
class GpuMotion:
    joint_hashes: list[int]
    frame_count: int
    frame_rate: float
    translation: GpuStream
    rotation: GpuStream
    start_frame: float = 0.0
    end_frame: float = 0.0


@dataclass(eq=False, slots=True)
class Motion:
    version: int
    name: str
    end_frame: float = 0.0
    loop_frame: float = -1.0
    raw_start_frame: float = 0.0
    raw_end_frame: float = 0.0
    fps: int = 60
    attributes: int = 0
    error_flags: int = 0
    joint_count: int = 0
    skeleton: Skeleton | None = None
    channels: list[JointChannels] = field(default_factory=list)
    properties: list[PropertyTrack] = field(default_factory=list)
    sequences: list[Sequence] = field(default_factory=list)
    jmap_path: str | None = None
    joint_poses: list[JointPose] | None = None
    gpu: GpuMotion | None = None
    sync: list[SyncPoints] = field(default_factory=list)
    raw_sync: list[list[tuple[float, float]]] = field(default_factory=list)
    append: AppendData | None = None


@dataclass(eq=False, slots=True)
class TreeParam:
    type: int
    key: int
    value: bool | int | float | str | bytes
    name: str | None = None


@dataclass(eq=False, slots=True)
class TreeTag:
    name: str
    hash: int = 0


@dataclass(eq=False, slots=True)
class TreeNode:
    class_name: str
    name: str = ""
    name_hash: int = 0
    id: int = 0
    class_hashes: tuple[int, int] = (0, 0)
    node_type: int = 0
    flags: int = 0
    tags: list[TreeTag] | None = None
    params: list[TreeParam] | None = None


@dataclass(eq=False, slots=True)
class TreeLink:
    parent: TreeNode
    pin: int
    child: TreeNode
    child_pin: int
    type: int
    guid: bytes | None = None


@dataclass(eq=False, slots=True)
class TreeMotion:
    node: TreeNode
    choices: list[tuple[int, int]] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class MotionTree:
    version: int
    name: str
    nodes: list[TreeNode] = field(default_factory=list)
    links: list[TreeLink] = field(default_factory=list)
    root: TreeNode | None = None
    remaps: list[tuple[int, int]] = field(default_factory=list)
    motions: list[TreeMotion] = field(default_factory=list)
    unknown_50: int = 0


@dataclass(eq=False, slots=True)
class ReferencePath:
    path: str


@dataclass(eq=False, slots=True)
class MotionListEntry:
    motion_id: int
    item: Motion | MotionTree | ReferencePath | None
    type: int = 1
    flags: int = 0
    tag_hash: int = 0
    physics: int = 0
    joint_mask: int = 0
    overrides: list[Sequence] = field(default_factory=list)
    category_tags: tuple[int, ...] = (0,) * 10
    additional_sequences: int = 0
    source_bank: int = 0
    source_key: int = 0


@dataclass(eq=False, slots=True)
class MotionList:
    version: int
    name: str
    entries: list[MotionListEntry] = field(default_factory=list)
    base_list: str | None = None
    error_flags: int = 0
    joint_mask_mode: int = 0
    motion_packs: list[str] = field(default_factory=list)
    first_update: int = 0
