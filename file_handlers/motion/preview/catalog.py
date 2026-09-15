from __future__ import annotations

from utils.resource_file_utils import ResourceResolutionContext

from ..format_codec import MotionFormatCodec
from .resolution import (
    MotionListDocument,
    MotionPreviewResolution,
    MotionPreviewResolver,
    TreeMotionReferenceStrategy,
)
from .resources import MotionListResourceStore


class MotionPreviewCatalog:
    """Resolve all playable motions and external list dependencies for a preview."""

    def __init__(
        self,
        root: MotionListDocument,
        tree_references: TreeMotionReferenceStrategy,
        format_codec: MotionFormatCodec,
        *,
        app=None,
        selection_parent=None,
        resource_context: ResourceResolutionContext | None = None,
    ):
        self.root = root
        self.resources = MotionListResourceStore(
            format_codec,
            app=app,
            anchor_path=root.path,
            selection_parent=selection_parent,
            resource_context=resource_context,
        )
        self._resolver = MotionPreviewResolver(
            self.resources.load,
            tree_references,
        )
        self.resolution = MotionPreviewResolution((), (), (), ())
        self.messages: tuple[str, ...] = ()

    def refresh(self) -> MotionPreviewResolution:
        resolution = self._resolver.resolve(self.root)
        self.resolution = resolution
        self.messages = tuple(
            dict.fromkeys(
                [item.message for item in resolution.diagnostics]
                + self.resources.errors
            )
        )
        return resolution
