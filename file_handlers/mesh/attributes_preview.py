from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.mmtr.material_preview import MaterialPreviewError

from .textures_preview import (
    TexturedContent, TexturedPart, TexturedScene, TexturedShape, TexturedView, TexturesPreview,
)

_UV_SETS = {"uv0": "uvs", "uv1": "uv1", "uv2": "uv2"}


@dataclass(frozen=True, slots=True)
class AttributeView(TexturedView):
    attribute: str = ""


def attribute_shape(draw) -> TexturedShape:
    """The draw's arrays change in place, so they are copied."""
    positions = np.array(draw.vertices, np.float32).reshape(-1, 3)
    normals = (np.zeros_like(positions) if draw.normals is None
               else np.array(draw.normals, np.float32).reshape(-1, 3))
    return TexturedShape(positions, normals, np.array(draw.colors, np.float32).reshape(-1, 4))


class AttributesPreview(TexturesPreview):
    view_type = AttributeView

    def __init__(self, handler, session, inspection, shown: Callable[[], object],
                 attribute: Callable[[], str | None], parent=None):
        super().__init__(handler, session, inspection, shown, parent)
        self._attribute = attribute
        self._source: tuple[object, str | None] = (None, None)

    def geometry_changed(self, preview=None) -> None:
        if not self._active:
            return
        if preview is None or preview is not self._source[0]:
            self.scene_changed()
        else:
            self.colours_changed()

    def colours_changed(self) -> None:
        preview = self._source[0]
        if self._active and self._scene is not None and preview is not None:
            self._scene = replace(self._scene, shape=attribute_shape(preview.scene[0]))
            self._ask()

    def _drawable(self) -> bool:
        return super()._drawable() and self._view.attribute == self._source[1]

    def _gather(self) -> TexturedScene:
        inspection, preview, name = self._inspection(), self._shown(), self._attribute()
        self._source = preview, name
        if (inspection is None or preview is None or name is None
                or (preview.layer, preview.lod_index) != inspection.geometry):
            raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The mesh has no attribute shown."))
        draw = preview.scene[0]
        uvs = None
        if name in _UV_SETS:
            uvs = getattr(draw, _UV_SETS[name])
            if uvs is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The mesh has no {name}."),
                                           {"name": name.upper()})
            uvs = np.array(uvs, np.float32).reshape(-1, 2)
        white = (1.0, 1.0, 1.0, 1.0)
        parts = tuple(TexturedPart(index, "", np.array(batch.indices, np.uint32), "", white, False, False, False, "",
                                   None, None) for index, batch in enumerate(draw.batches))
        return TexturedScene(TexturedContent("", inspection.geometry, parts, uvs, {}, None, 1, False, {}, False),
                             attribute_shape(draw))


__all__ = ["AttributeView", "AttributesPreview", "attribute_shape"]
