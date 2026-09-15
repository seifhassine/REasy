from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from ..mot_clip.model import CompactMotClip


class SequenceCategory(IntEnum):
    GAME = 0
    SOUND = 1
    VFX = 2
    MOTION_SYNC = 3
    PHYSICS = 4
    EXTRA_0 = 5
    EXTRA_1 = 6
    EXTRA_2 = 7
    EXTRA_3 = 8
    EXTRA_4 = 9


@dataclass(slots=True)
class SequenceTrack:
    authored_id: int = 0xFFFFFFFF
    filter_page0: int = 0
    filter_page1: int = 0
    filter_page2: int = 0


@dataclass(slots=True)
class SequenceData:
    category: SequenceCategory
    clip: CompactMotClip
    # One semantic metadata row per compact MotClip root child, in order.
    tracks: list[SequenceTrack] = field(default_factory=list)
