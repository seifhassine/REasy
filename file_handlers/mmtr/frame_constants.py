"""See docs/internals/engine-buffers.md."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from file_handlers.build_catalog import catalog_file_name, catalog_reader

CATALOG_FORMAT = 1
CATALOG_DIRECTORY = "resources/data/frame_constants"


class FrameConstantError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FrameConstants:
    game: str
    resource_version: int
    # One value per 32-bit word, typed as the field is declared.
    scene: Mapping[str, tuple[float | int, ...]]
    source: Mapping = field(default_factory=dict)


def catalog_to_dict(catalog: FrameConstants) -> dict:
    return {"format": CATALOG_FORMAT, "game": catalog.game, "resource_version": catalog.resource_version,
            "scene": {name: list(values) for name, values in catalog.scene.items()}, "source": dict(catalog.source)}


def catalog_from_dict(data: Mapping) -> FrameConstants:
    if data.get("format") != CATALOG_FORMAT:
        raise FrameConstantError(f"frame constant catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    scene = {str(name): tuple(values) for name, values in data["scene"].items()}
    if any(not values or not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values)
           for values in scene.values()):
        raise FrameConstantError("every field of a frame constant catalog holds one number or more")
    return FrameConstants(str(data["game"]), int(data["resource_version"]), scene, dict(data.get("source", {})))


frame_constants_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, FrameConstantError,
                                     "frame constant", (AttributeError,))


__all__ = ["CATALOG_DIRECTORY", "CATALOG_FORMAT", "FrameConstantError", "FrameConstants", "catalog_file_name",
           "catalog_from_dict", "catalog_to_dict", "frame_constants_for"]
