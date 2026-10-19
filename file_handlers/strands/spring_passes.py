"""See docs/internals/strands-chain.md, RE4's spring system."""
from __future__ import annotations

import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.mmtr.material_preview import MaterialPreviewError
from file_handlers.tex.dxgi import DXGI_FORMAT_R32_UINT

from .chain_catalog import BEFORE, NOW
from .chain_passes import Grid, Step, dispatch_groups, program_variant
from .strands_file import StrandsFile

# Byte sizes; the bounds hold four slots of six words.
_NODE, _VELOCITY, _PROPERTIES, _BOUNDS_BYTES = 28, 24, 32, 96
_CELL_WORDS = 6
GRIDS = ("grid.velocity" + NOW, "grid.velocity" + BEFORE)


@dataclass(frozen=True, slots=True)
class SpringShape:
    guides: int
    strand_size: int
    roots: int
    vertices: int
    grid: tuple[int, int, int]

    @classmethod
    def of(cls, constant: Callable[[str], Sequence[int]]) -> SpringShape:
        (guides,), (size,), (roots,), (vertices,) = map(constant, ("strandCount", "strandSize", "renderStrandCount",
                                                                    "renderStrandVertexCount"))
        return cls(guides, size, roots, vertices, tuple(constant("gridDimension")))

    @property
    def nodes(self) -> int:
        return self.guides * self.strand_size


@dataclass(frozen=True, slots=True)
class SpringState:
    start: bool
    lod_changed: bool
    simulated: bool
    bound: bool
    world: bool
    guided: bool
    guide_only: bool


def spring_steps(shape: SpringShape, state: SpringState) -> list[Step]:
    nodes, vertices, bound = dispatch_groups(shape.nodes), dispatch_groups(shape.vertices), ("bound", state.bound)
    world = ("world", state.bound and state.world)
    steps = [Step("init", "", nodes)] if state.start else []
    if state.lod_changed:
        steps.append(Step("init_lod", "", nodes))
    if not state.simulated:
        return steps
    steps += [Step("rest_root", program_variant(bound, world), dispatch_groups(shape.roots)),
              Step("rest_data", program_variant(bound, ("start", state.start)), nodes)]
    if state.guided:
        steps.append(Step("rest_position", program_variant(bound, world), vertices))
    steps += [Step("update", program_variant(("world", state.world)), nodes),
              Step("interp", program_variant(("world", state.world)), nodes)]
    if state.guided:
        steps.append(Step("guide", program_variant(("guide_only", state.guide_only)), vertices))
    return steps


def spring_kept(shape: SpringShape, first_vertices: int) -> dict[str, bytes | Grid]:
    nodes, roots = shape.nodes, shape.roots
    kept = {"rest": bytes(_NODE * nodes), "node_properties": bytes(_PROPERTIES * nodes),
            "rest_roots": bytes(_NODE * roots), "deformed_roots": bytes(_NODE * roots), "validation": bytes(4 * roots),
            "bounds": bytes(_BOUNDS_BYTES), "guided_rest": bytes(16 * first_vertices)}
    for end in (NOW, BEFORE):
        kept |= {"rest_data" + end: bytes(_NODE * nodes), "nodes" + end: bytes(_NODE * nodes),
                 "velocities" + end: bytes(_VELOCITY * nodes)}
    x, y, z = shape.grid
    for slot in GRIDS:
        kept[slot] = Grid(DXGI_FORMAT_R32_UINT, ((x + 1) * _CELL_WORDS, y + 1, z + 1))
    return kept


def spring_buffers(strands: StrandsFile, lod: int) -> dict[str, bytes]:
    if strands.secondary_guide_count > strands.guide_count:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The strands have secondary guides, which the spring system has no programs for."))
    posed = strands.lods[lod]
    return {"strand.root_uvs": b"".join(struct.pack("<2f", *uv) for uv in strands.root_uvs),
            "strand.vertices": posed.vertex_records, "strand.vertex_strands": posed.vertex_strands,
            "strand.offsets": struct.pack(f"<{len(posed.strand_offsets)}I", *posed.strand_offsets),
            "strand.closest_guides": posed.closest_guide_data, "strand.segments": posed.segment_records}


__all__ = ["GRIDS", "SpringShape", "SpringState", "spring_buffers", "spring_kept", "spring_steps"]
