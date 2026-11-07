"""See docs/internals/user-render-targets.md."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

lazy from file_handlers.mmtr.material_preview import MaterialPreviewError
lazy from file_handlers.mmtr.user_render_targets import (
    RenderTexture, UserRenderTarget, UserRenderTargetCatalog, read_render_texture, user_render_target_catalog_for,
)

lazy from .material_resolver import MdfSurfaceProfile, read_mdf, surface_profile
lazy from .material_shader_preview import preview_material

translate = QCoreApplication.translate


@dataclass(frozen=True, slots=True)
class SceneRenderTargetSource:
    key: str
    fields: Mapping[str, object]
    visible: bool
    resolve: Callable[[str], tuple[str, bytes] | None]


def drawn_surface(source: SceneRenderTargetSource, catalog: UserRenderTargetCatalog
                  ) -> tuple[str, RenderTexture, MdfSurfaceProfile] | None:
    names, fields = catalog.component, source.fields
    if fields.get(names.parameters):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "No capture shows a render target modifier's own material parameters set."))
    target, material = (str(fields.get(name) or "").strip() for name in (names.target, names.material))
    if not target or not material:
        return None
    found = source.resolve(target)
    if found is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The render texture file {path} was not found."), {"path": target})
    texture = read_render_texture(found[1], catalog)
    mdf = read_mdf(source.resolve, material)
    if len(mdf.materials) <= names.drawn_material:
        return None
    return target, texture, surface_profile(mdf.materials[names.drawn_material], mdf.file_version)


def gather_render_targets(sources: Sequence[SceneRenderTargetSource], profile,
                          gather_surface: Callable[[MdfSurfaceProfile, Callable], str]
                          ) -> tuple[tuple[UserRenderTarget, ...], str]:
    catalog = user_render_target_catalog_for(profile) if sources else None
    if catalog is None:
        return (), ""
    found, refused = [], []
    for source in sources:
        if not source.visible:
            continue
        try:
            drawn = drawn_surface(source, catalog)
        except MaterialPreviewError as exc:
            refused.append(str(exc))
            continue
        if drawn is None:
            continue
        path, texture, surface = drawn
        if surface.instance_context is None:
            refused.append(translate("SceneLitPreview", "No MDF material."))
            continue
        reason = gather_surface(surface, source.resolve)
        if reason:
            refused.append(reason)
            continue
        found.append(UserRenderTarget(source.key, path, texture, surface.mmtr_path, surface.instance_context,
                                      preview_material(surface)))
    return tuple(found), "; ".join(dict.fromkeys(refused))


__all__ = ["SceneRenderTargetSource", "drawn_surface", "gather_render_targets"]
