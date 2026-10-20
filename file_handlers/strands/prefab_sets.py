from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.mmtr.material_preview import MaterialPreviewError
lazy from file_handlers.rsz.plain_values import plain_fields

lazy from .chain_catalog import StrandCatalog


@dataclass(frozen=True, slots=True)
class PrefabSet:
    owner: object
    render: Mapping[str, object]
    dynamics: Mapping[str, object]


def prefab_sets(graph, catalog: StrandCatalog) -> list[PrefabSet]:
    kinds = (catalog.render.component, catalog.constants.component)
    out = []
    for document in graph.documents.values():
        for owner in document.objects.values():
            held = {kind: [document.components[key] for key in owner.components
                           if document.components[key].type_name == kind] for kind in kinds}
            if not any(held.values()):
                continue
            if any(len(found) != 1 for found in held.values()):
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The object {name} does not hold one strand render and one strand physics "
                                       "component."), {"name": owner.name})
            render, dynamics = (plain_fields(document, held[kind][0]) for kind in kinds)
            out.append(PrefabSet(owner, render, dynamics))
    return out


__all__ = ["PrefabSet", "prefab_sets"]
