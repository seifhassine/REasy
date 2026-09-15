from __future__ import annotations

from dataclasses import dataclass, field
import struct
from typing import List

_HEADER_STRUCT = struct.Struct('<256H')
_EVENT_STRUCT = struct.Struct('<IIIIBBhhhhhBBBIhBBBBBIIIq')


@dataclass
class WELPrioritySerialized:
    mId1: int = 0
    mId2: int = 0
    mId3: int = 0
    mBookingTimer: int = 0
    mFlangingTimer: int = 0
    mGlobalId: int = 0
    mLimit: int = 0
    mPriority: int = 0
    mMode: int = 0
    mReleaseTime: int = 0


@dataclass
class WELFreeArea:
    mFreeArea0to7: int = 0
    mFreeArea8to11: int = 0
    mFreeArea12to15: int = 0

    @property
    def slots(self) -> tuple[int, ...]:
        """ Capcom's 16 logical slots ."""

        low = tuple((self.mFreeArea0to7 >> (index * 4)) & 0xF for index in range(8))
        middle = tuple((self.mFreeArea8to11 >> (index * 8)) & 0xFF for index in range(4))
        raw = self.mFreeArea12to15 & 0xFFFFFFFFFFFFFFFF
        high = tuple(
            value - 0x10000 if value & 0x8000 else value
            for value in ((raw >> (index * 16)) & 0xFFFF for index in range(4))
        )
        return low + middle + high

    @classmethod
    def from_slots(cls, values) -> "WELFreeArea":
        values = tuple(map(int, values))
        if len(values) != 16:
            raise ValueError("WEL free area requires exactly 16 slots")
        if any(not 0 <= value <= 0xF for value in values[:8]):
            raise ValueError("WEL free slots 0..7 must be between 0 and 15")
        if any(not 0 <= value <= 0xFF for value in values[8:12]):
            raise ValueError("WEL free slots 8..11 must be between 0 and 255")
        if any(not -0x8000 <= value <= 0x7FFF for value in values[12:]):
            raise ValueError("WEL free slots 12..15 must be signed 16-bit values")
        packed_low = sum(value << (index * 4) for index, value in enumerate(values[:8]))
        packed_middle = sum(value << (index * 8) for index, value in enumerate(values[8:12]))
        packed_high = sum((value & 0xFFFF) << (index * 16) for index, value in enumerate(values[12:]))
        if packed_high >= 1 << 63:
            packed_high -= 1 << 64
        return cls(packed_low, packed_middle, packed_high)


@dataclass
class WELEventEntry:
    mTriggerId: int = 0
    mEventId: int = 0
    mJointHash: int = 0
    mGameObjectHash: int = 0
    mTracking: int = 0
    mRotation: int = 0
    mPriority: WELPrioritySerialized = field(default_factory=WELPrioritySerialized)
    mDisableObsOcl: int = 0
    mUpdateObsOcl: int = 0
    mDisableMaxObsOclDistance: int = 0
    mEnableSpaceFeature: int = 0
    mWaitUntilFinished: int = 0
    mListenerMask: int = 0
    mFreeArea: WELFreeArea = field(default_factory=WELFreeArea)


@dataclass
class WELFile:
    bank_path_raw: List[int] = field(default_factory=lambda: [0] * 256)
    event_count: int = 0
    events: List[WELEventEntry] = field(default_factory=list)
    trailing_data: bytes = b''

    @property
    def bank_path(self) -> str:
        return ''.join(chr(ch) for ch in self.bank_path_raw if ch != 0)

    def read(self, data: bytes) -> bool:
        if len(data) < _HEADER_STRUCT.size + 4:
            return False

        off = 0
        self.bank_path_raw = list(_HEADER_STRUCT.unpack_from(data, off))
        off += _HEADER_STRUCT.size

        self.event_count = struct.unpack_from('<i', data, off)[0]
        off += 4

        if self.event_count < 0:
            raise ValueError('Negative event count encountered in WEL data')

        self.events = []
        for _ in range(self.event_count):
            if off + _EVENT_STRUCT.size > len(data):
                raise ValueError('Unexpected end of file while parsing WEL events')

            unpacked = _EVENT_STRUCT.unpack_from(data, off)
            off += _EVENT_STRUCT.size

            priority = WELPrioritySerialized(
                mId1=unpacked[6],
                mId2=unpacked[7],
                mId3=unpacked[8],
                mBookingTimer=unpacked[9],
                mFlangingTimer=unpacked[10],
                mGlobalId=unpacked[11],
                mLimit=unpacked[12],
                mPriority=unpacked[13],
                mMode=unpacked[14],
                mReleaseTime=unpacked[15],
            )

            free_area = WELFreeArea(
                mFreeArea0to7=unpacked[22],
                mFreeArea8to11=unpacked[23],
                mFreeArea12to15=unpacked[24],
            )

            event = WELEventEntry(
                mTriggerId=unpacked[0],
                mEventId=unpacked[1],
                mJointHash=unpacked[2],
                mGameObjectHash=unpacked[3],
                mTracking=unpacked[4],
                mRotation=unpacked[5],
                mPriority=priority,
                mDisableObsOcl=unpacked[16],
                mUpdateObsOcl=unpacked[17],
                mDisableMaxObsOclDistance=unpacked[18],
                mEnableSpaceFeature=unpacked[19],
                mWaitUntilFinished=unpacked[20],
                mListenerMask=unpacked[21],
                mFreeArea=free_area,
            )
            self.events.append(event)

        self.trailing_data = data[off:]
        return True

    def write(self) -> bytes:
        out = bytearray()
        out += _HEADER_STRUCT.pack(*self.bank_path_raw)
        out += struct.pack('<i', len(self.events))

        for event in self.events:
            out += _EVENT_STRUCT.pack(
                event.mTriggerId,
                event.mEventId,
                event.mJointHash,
                event.mGameObjectHash,
                event.mTracking,
                event.mRotation,
                event.mPriority.mId1,
                event.mPriority.mId2,
                event.mPriority.mId3,
                event.mPriority.mBookingTimer,
                event.mPriority.mFlangingTimer,
                event.mPriority.mGlobalId,
                event.mPriority.mLimit,
                event.mPriority.mPriority,
                event.mPriority.mMode,
                event.mPriority.mReleaseTime,
                event.mDisableObsOcl,
                event.mUpdateObsOcl,
                event.mDisableMaxObsOclDistance,
                event.mEnableSpaceFeature,
                event.mWaitUntilFinished,
                event.mListenerMask,
                event.mFreeArea.mFreeArea0to7,
                event.mFreeArea.mFreeArea8to11,
                event.mFreeArea.mFreeArea12to15,
            )

        out += self.trailing_data
        return bytes(out)
