from __future__ import annotations

from dataclasses import dataclass

lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.mmtr.material_preview import MaterialPreviewError
lazy from file_handlers.mmtr.wrinkle import WrinkleCatalog, component_settings
lazy from file_handlers.rsz.plain_values import plain_fields


@dataclass(frozen=True, slots=True)
class SceneWrinkle:
    key: str
    render_target: str = ""
    spread: bool = False
    proxy: str = ""
    refused: str = ""


def scene_wrinkles(graph, catalog: WrinkleCatalog) -> list[SceneWrinkle]:
    names = catalog.component
    owners: dict[object, SceneWrinkle] = {}
    for document in graph.documents.values():
        for owner in document.objects.values():
            held = {kind: [document.components[key] for key in owner.components
                           if document.components[key].type_name == kind] for kind in (names.type, names.proxy)}
            if not held[names.type]:
                continue
            if len(held[names.type]) != 1 or len(held[names.proxy]) != 1:
                owners[owner.id] = SceneWrinkle("", refused=str(MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The object {name} does not hold one wrinkle and one skinning component."),
                    {"name": owner.name})))
                continue
            proxy = str(plain_fields(document, held[names.proxy][0]).get(names.mesh) or "")
            try:
                target, spread = component_settings(plain_fields(document, held[names.type][0]), catalog)
            except MaterialPreviewError as exc:
                owners[owner.id] = SceneWrinkle("", refused=str(exc))
                continue
            owners[owner.id] = SceneWrinkle("", target, spread, proxy, "" if proxy else str(MaterialPreviewError(
                QT_TRANSLATE_NOOP("MaterialPreview", "The object {name}'s skinning component names no proxy mesh."),
                {"name": owner.name})))
    return [SceneWrinkle(renderable.key, *(getattr(found, name) for name in ("render_target", "spread", "proxy",
                                                                            "refused")))
            for renderable in graph.renderables if (found := owners.get(renderable.source_object_id)) is not None]


__all__ = ["SceneWrinkle", "scene_wrinkles"]
