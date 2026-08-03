from __future__ import annotations

from ..errors import MotionValidationError
from ..mot_clip.validator import CompactMotClipV27Validator
from ..profiles import MotionFormatProfile
from .model import SequenceData


class SequenceV65Validator:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot=65, mot_clip=27)
        self.profile = profile
        self.clip_validator = CompactMotClipV27Validator(profile)

    def validate(self, sequence: SequenceData, *, allowed_categories: frozenset[int] | None = None) -> None:
        categories = self.profile.mot.sequence_categories if allowed_categories is None else allowed_categories
        if sequence.category not in categories:
            raise MotionValidationError(f"sequence category {sequence.category} is unsupported")
        self.clip_validator.validate(sequence.clip)
        child_count = len(sequence.clip.root.children)
        if len(sequence.tracks) != child_count:
            raise MotionValidationError("TracksData must correspond to MotClip root children")
        for track in sequence.tracks:
            for value in (
                track.authored_id,
                track.filter_page0,
                track.filter_page1,
                track.filter_page2,
            ):
                if not 0 <= value <= 0xFFFFFFFF:
                    raise MotionValidationError("TracksData value exceeds u32")
