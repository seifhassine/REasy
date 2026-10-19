"""See docs/internals/mesh-preview.md, Forward order catalog."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from file_handlers.build_catalog import catalog_file_name, catalog_reader

CATALOG_FORMAT = 2
CATALOG_DIRECTORY = "resources/data/draw_order"
SORTED_ORDERS = {"nearest_part": False, "farthest_object_depth": True}
ORDERS = ("objects", *SORTED_ORDERS)


class DrawOrderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DrawOrder:
    game: str
    resource_version: int
    unblended: str
    blended: str
    source: Mapping = field(default_factory=dict)


def catalog_from_dict(data: Mapping) -> DrawOrder:
    if data.get("format") != CATALOG_FORMAT:
        raise DrawOrderError(f"draw order catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    unblended, blended = str(data["unblended"]), str(data["blended"])
    for order in (unblended, blended):
        if order not in ORDERS:
            raise DrawOrderError(f"order {order!r} is none of {', '.join(ORDERS)}")
    return DrawOrder(str(data["game"]), int(data["resource_version"]), unblended, blended,
                     dict(data.get("source", {})))


draw_order_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, DrawOrderError, "draw order", (AttributeError,))


__all__ = ["CATALOG_DIRECTORY", "CATALOG_FORMAT", "DrawOrder", "DrawOrderError", "ORDERS", "SORTED_ORDERS",
           "catalog_file_name", "catalog_from_dict", "draw_order_for"]
