from __future__ import annotations

import struct

from ..binary import pad_to_alignment
from ..mot_clip.writer import CompactMotClipV27Writer
from ..profiles import MotionFormatProfile
from .model import SequenceData
from .validator import SequenceV65Validator


class SequenceV65Writer:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot=65, mot_clip=27)
        self.profile = profile
        self.clip_writer = CompactMotClipV27Writer(profile)
        self.validator = SequenceV65Validator(profile)

    def build(
        self,
        sequence: SequenceData,
        *,
        sequence_offset: int,
        pointer_base: int,
        allowed_categories: frozenset[int] | None = None,
    ) -> bytes:
        self.validator.validate(sequence, allowed_categories=allowed_categories)
        layout = self.profile.mot
        out = bytearray(layout.sequence_clip_offset)
        clip_absolute = sequence_offset + layout.sequence_clip_offset
        clip_blob = self.clip_writer.build(
            sequence.clip,
            origin_offset=clip_absolute - pointer_base,
        )
        out.extend(clip_blob)
        pad_to_alignment(out, 16)
        tracks_absolute = sequence_offset + len(out)
        for track in sequence.tracks:
            out.extend(
                struct.pack(
                    "<IIII",
                    track.authored_id,
                    track.filter_page0,
                    track.filter_page1,
                    track.filter_page2,
                )
            )
        struct.pack_into(
            "<QQQIIII",
            out,
            0,
            0,
            clip_absolute - pointer_base,
            tracks_absolute - pointer_base,
            0,
            len(sequence.tracks),
            1,
            sequence.category,
        )
        return bytes(out)
