from __future__ import annotations

from dataclasses import dataclass

from ..evaluation import MotionEvaluationProfile
from ..format_codec import MotionFormatCodec
from ..runtime.backend import EntityMotionBackend
from .catalog_reader import MotionListCatalogReader
from .resolution import TreeMotionReferenceStrategy


@dataclass(frozen=True, slots=True)
class EntityMotionSupport:
    """Composition metadata for one game's entity-motion preview."""

    format_codec: MotionFormatCodec
    evaluation: MotionEvaluationProfile
    tree_references: TreeMotionReferenceStrategy
    backend: EntityMotionBackend
    catalog_reader: MotionListCatalogReader | None = None
