"""MOTLIST 992 / MOT 932 skeletal preview; CLIP 85 remains opaque.

The original bytes and the locations of deferred sections remain in the model.
Writing is deliberately unavailable until those sections have relocation support.
"""
from dataclasses import dataclass, field, replace
import struct

from .mhr_codec import MhrParser, MHR_PROFILE
from .mhr_storage import MhrMotList
from .wilds_tracks import decode_track
from .errors import MotionWriteError


WILDS_PROFILE = replace(MHR_PROFILE, name='Monster Hunter Wilds',
                       motlist=replace(MHR_PROFILE.motlist, version=992),
                       mot=replace(MHR_PROFILE.mot, version=932),
                       mot_clip=replace(MHR_PROFILE.mot_clip, version=85))


@dataclass(frozen=True)
class DeferredSection:
    kind: str
    offset: int
    count: int


@dataclass(slots=True)
class WildsMotList(MhrMotList):
    deferred_sections: list[DeferredSection] = field(default_factory=list)


class WildsParser(MhrParser):
    container_version = 992
    motion_version = 932
    decode_track = staticmethod(decode_track)

    def __init__(self, data, label):
        super().__init__(data, label)
        self.model = WildsMotList('', source=bytes(data))

    def check_auxiliary(self, c, base, pointers):
        for i in (2, 3, 5, 6, 7):
            if pointers[i]:
                c.require(base+pointers[i], 0, 'MOT 932 auxiliary section')
                self.model.deferred_sections.append(DeferredSection(f'Auxiliary {i}', base+pointers[i], 1))

    def read_sequences(self, c, base, table, count, motion, group):
        if count:
            c.require(base+table, count*8, 'MOT 932 sequence pointers')
            self.model.deferred_sections.append(DeferredSection('CLIP 85 sequences', base+table, count))

    def read_overrides(self, c, table, count, slot, group):
        if count:
            self.model.deferred_sections.append(DeferredSection('CLIP 85 overrides', table, count))


class WildsMotionFormatCodec:
    profile = WILDS_PROFILE

    def matches(self, data):
        return len(data) >= 52 and struct.unpack_from('<I', data)[0] == 992 and bytes(data[4:8]) == b'mlst'

    def parse(self, data, *, label='MOTLIST'):
        return WildsParser(data, label).parse()

    def write(self, model):
        raise MotionWriteError('MOTLIST 992 is preview-only: CLIP 85 and auxiliary relocation are not implemented')


WILDS_MOTION_FORMAT_CODEC = WildsMotionFormatCodec()
