from __future__ import annotations

from ..dmc5_codec import DMC5_MOTION_FORMAT_CODEC
from ..mhr_codec import MHR_MOTION_FORMAT_CODEC
from ..wilds_codec import WILDS_MOTION_FORMAT_CODEC
from dataclasses import replace
from ..evaluation.mhr import MHR_EVALUATION_PROFILE
from ..evaluation import DMC5_EVALUATION_PROFILE
from ..format_codec import MotionFormatCodec
from ..runtime.dmc5 import DMC5_ENTITY_MOTION_BACKEND
from .catalog_reader import Dmc5MotionListCatalogReader
from .resolution import DMC5_TREE_MOTION_REFERENCES
from .support import EntityMotionSupport, MotionPreviewSupport


DMC5_ENTITY_MOTION_SUPPORT = EntityMotionSupport(
    format_codec=DMC5_MOTION_FORMAT_CODEC,
    evaluation=DMC5_EVALUATION_PROFILE,
    tree_references=DMC5_TREE_MOTION_REFERENCES,
    backend=DMC5_ENTITY_MOTION_BACKEND,
    catalog_reader=Dmc5MotionListCatalogReader(DMC5_MOTION_FORMAT_CODEC.profile),
)

ENTITY_MOTION_SUPPORTS = (DMC5_ENTITY_MOTION_SUPPORT,)


class MhrTreeMotionReferences:
    def extract(self, tree):
        raise ValueError('MHR MotTree payloads are not supported by the MOT 495 codec')


MHR_MOTION_PREVIEW_SUPPORT = MotionPreviewSupport(
    MHR_MOTION_FORMAT_CODEC, MHR_EVALUATION_PROFILE, MhrTreeMotionReferences(),
)
WILDS_EVALUATION_PROFILE = replace(MHR_EVALUATION_PROFILE, name='Monster Hunter Wilds')
WILDS_MOTION_PREVIEW_SUPPORT = MotionPreviewSupport(
    WILDS_MOTION_FORMAT_CODEC, WILDS_EVALUATION_PROFILE, MhrTreeMotionReferences(),
)
MOTION_PREVIEW_SUPPORTS = (*ENTITY_MOTION_SUPPORTS, MHR_MOTION_PREVIEW_SUPPORT, WILDS_MOTION_PREVIEW_SUPPORT)


def entity_motion_support_for_game(
    game_version: str,
) -> EntityMotionSupport | None:
    normalized = str(game_version).strip().upper()
    return next(
        (
            support
            for support in ENTITY_MOTION_SUPPORTS
            if support.backend.game_version.upper() == normalized
        ),
        None,
    )


def entity_motion_support_for_format(
    codec: MotionFormatCodec,
) -> MotionPreviewSupport | None:
    matches = tuple(
        support
        for support in MOTION_PREVIEW_SUPPORTS
        if support.format_codec.profile == codec.profile
    )
    return matches[0] if len(matches) == 1 else None
