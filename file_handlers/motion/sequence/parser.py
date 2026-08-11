from __future__ import annotations

from ..binary import ReadContext
from ..errors import MotionParseError
from ..mot_clip.parser import CompactClipV27Parser
from ..profiles import MotionFormatProfile
from .model import SequenceCategory, SequenceData, SequenceTrack
from .validator import SequenceV65Validator


class SequenceV65Parser:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot=65, mot_clip=27)
        self.profile = profile
        self.clip_parser = CompactClipV27Parser(profile)
        self.validator = SequenceV65Validator(profile)

    def parse(
        self,
        context: ReadContext,
        sequence_offset: int,
        *,
        pointer_base: int,
        allowed_categories: frozenset[int] | None = None,
    ) -> SequenceData:
        c = context
        layout = self.profile.mot
        c.require(sequence_offset, layout.sequence_clip_offset, "SequenceData")
        if c.u64(sequence_offset, "SequenceData name pointer"):
            raise MotionParseError(f"{c.label}: v65 SequenceData names are unsupported")
        clip_offset = pointer_base + c.u64(sequence_offset + 8, "SequenceData clip pointer")
        tracks_offset = pointer_base + c.u64(sequence_offset + 0x10, "SequenceData tracks pointer")
        if c.u32(sequence_offset + 0x18, "SequenceData attributes"):
            raise MotionParseError(f"{c.label}: SequenceData attribute flags are unsupported")
        count = c.u32(sequence_offset + 0x1C, "SequenceData tracks count")
        if c.u32(sequence_offset + 0x20, "SequenceData use flags") != 1:
            raise MotionParseError(f"{c.label}: SequenceData useFlags must equal one")
        raw_category = c.u32(sequence_offset + 0x24, "SequenceData category")
        try:
            category = SequenceCategory(raw_category)
        except ValueError as exc:
            raise MotionParseError(
                f"{c.label}: unsupported SequenceData category {raw_category}"
            ) from exc
        if clip_offset != sequence_offset + layout.sequence_clip_offset:
            raise MotionParseError(f"{c.label}: compact MotClip must begin at SequenceData +0x40")
        c.require_zero(
            sequence_offset + layout.sequence_wrapper_size,
            clip_offset,
            "SequenceData-to-MotClip padding",
        )
        clip = self.clip_parser.parse(
            c,
            clip_offset,
            tracks_offset,
            pointer_base=pointer_base,
            following_data_name="TracksData",
        )
        c.require(tracks_offset, count * layout.tracks_data_size, "TracksData table")
        tracks = [
            SequenceTrack(
                c.u32(tracks_offset + index * 0x10, "TracksData authored ID"),
                c.u32(tracks_offset + index * 0x10 + 4, "TracksData filter page 0"),
                c.u32(tracks_offset + index * 0x10 + 8, "TracksData filter page 1"),
                c.u32(tracks_offset + index * 0x10 + 0xC, "TracksData filter page 2"),
            )
            for index in range(count)
        ]
        child_count = len(clip.root.children)
        if len(tracks) < child_count:
            raise MotionParseError(
                f"{c.label}: TracksData has fewer rows than compact MotClip root children"
            )
        for extra in tracks[child_count:]:
            if (
                extra.authored_id != 0xFFFFFFFF
                or extra.filter_page0
                or extra.filter_page1
                or extra.filter_page2
            ):
                raise MotionParseError(f"{c.label}: ownerless TracksData suffix row is not default")
        result = SequenceData(category, clip, tracks[:child_count])
        self.validator.validate(result, allowed_categories=allowed_categories)
        return result

    def physical_end(self, context: ReadContext, sequence_offset: int, *, pointer_base: int) -> int:
        tracks_offset = pointer_base + context.u64(sequence_offset + 0x10, "SequenceData tracks pointer")
        count = context.u32(sequence_offset + 0x1C, "SequenceData tracks count")
        return tracks_offset + count * self.profile.mot.tracks_data_size
