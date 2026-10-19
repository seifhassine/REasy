"""See docs/internals/strands-rendering.md."""
from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

from file_handlers.tex.dxgi import DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_R32_FLOAT, DXGI_FORMAT_R32_UINT

lazy from .chain_passes import program_variant
from .render_passes import Fill, RenderLayout, Step, Write, _over

# Pixels per tile side.
TILE = 16
# ``MultipleScattering`` value -> the hair shading's name; only these run dual scattering.
SCATTERING = {1: "Dual", 2: "DualIBL", 3: "DualIBLOpt"}
# Arguments written before a set's coarse raster: the fine raster's dispatch and the fill's draw.
_ARGUMENTS = struct.pack("<8I", 0, 1, 1, 1, 6, 0, 0, 0)
# Mips after the first, each made from the one before.
_VOLUME_MIPS = 6
# ``IBL_OPTIMIZED`` is ``DualWithIBLOptimized``, which samples the IBL itself; ``_IBL_COUNTS`` maps ``IBLDominantNum``
# to direction counts.
IBL_OPTIMIZED = 3
_IBL_COUNTS = {3: 2, 5: 4, 7: 8}
_IBL_VARIANTS = {2: ("two", "two"), 6: ("cube", "")}
_IBL_FACES = {6: 6}
# The composite's direction offset and id tables per count, eight words each.
_IBL_ORDER = tuple(range(8))
_IBL_TABLES = {2: ((0, 4, 0, 0, 0, 0, 0, 0), _IBL_ORDER), 4: ((0, 2, 4, 6, 0, 0, 0, 0), (0, 4, 1, 5, 2, 6, 3, 7)),
               6: (_IBL_ORDER, _IBL_ORDER), 8: (_IBL_ORDER, _IBL_ORDER)}
_IBL_DEFAULT_TABLE = ((0,) * 8, _IBL_ORDER)


def ibl_count(dominant: int) -> int:
    return _IBL_COUNTS.get(dominant, dominant)


def ibl_tables(count: int) -> dict[str, bytes]:
    offsets, ids = _IBL_TABLES.get(count, _IBL_DEFAULT_TABLE)
    return {"ibl.comp_offset": struct.pack("<8I", *offsets), "ibl.comp_id": struct.pack("<8I", *ids)}


def work_sizes(segments: int, vertices: int) -> dict[str, int]:
    return {"work.solid_hair": segments * 4, "work.queue": segments * 256, "work.shade": vertices * 4,
            "work.visibility": ((vertices + 31) * 4 >> 5) & ~3, "work.counter": 16}


LAYOUT = RenderLayout(
    tile_bytes=256, queue_bytes=4,
    view_textures={"view.strand_depth": (DXGI_FORMAT_R32_FLOAT, None, 1)},
    set_textures={"ds.volume": (DXGI_FORMAT_R32_UINT, (128, 128, 128), _VOLUME_MIPS),
                  "ds.lut": (DXGI_FORMAT_R16G16B16A16_FLOAT, (64, 64, 16), 1)},
    set_buffers={"ds.bounds": 48, "ds.info": 84, "ds.arguments": 112,
                 **{"ibl.dir" + end: 32 for end in ("@now", "@before")},
                 **{"ibl.color" + end: 64 for end in ("@now", "@before")},
                 **dict.fromkeys(("ibl.face_dir", "ibl.face_color", "ibl.face_color2", "ibl.face_color_max"), 96)},
    arguments={"view.arguments": 64},
    work_sizes=work_sizes)


@dataclass(frozen=True, slots=True)
class DirectState:
    drawn: int
    injected: int
    pretransformed: bool
    mix: bool
    countup: bool
    bits32: bool
    blend: bool
    scattering: str | None
    interpolated: bool
    lut: bool
    shrink_shadow: bool
    projection_lights: bool
    material_shading: bool
    ibl: int | None = None


def direct_steps(state: DirectState, tiles: tuple[int, int],
                 threads: Callable[[str, str], tuple[int, int, int]]) -> list:
    pretransformed, drawn = ("pretransformed", state.pretransformed), state.drawn

    def over(kind: str, variant: str, count: int) -> tuple[int, int, int]:
        return _over(count, threads(kind, variant)[0]), 1, 1

    def tiled(kind: str, variant: str) -> tuple[int, int, int]:
        x, y, _ = threads(kind, variant)
        return _over(tiles[0], x), _over(tiles[1], y), 1

    coarse = program_variant(pretransformed, ("mix", state.mix), ("countup", state.countup))
    steps = []
    if state.ibl is not None:
        dominant, composite = _IBL_VARIANTS.get(state.ibl, ("", ""))
        steps += [Step("ibl_dominant", dominant, (1, _IBL_FACES.get(state.ibl, 8), 1)),
                  Step("ibl_composite", composite, (1, 1, 1)) if composite else
                  Step("ibl_composite", composite, (state.ibl, 1, 1), constant=state.ibl)]
    steps += [Fill("view.accumulate", 0 if state.countup else 256), Fill("view.offset", 0), Fill("work.counter", 1),
             Write("view.arguments", _ARGUMENTS), Step("coarse_z", "", (*tiles, 1)),
             Step("coarse", coarse, over("coarse", coarse, drawn), constant=drawn)]
    if state.countup:
        # The engine counts the second coarse raster's groups by the allocation program's group width.
        alloc = program_variant(pretransformed, ("mix", state.mix))
        steps += [Step("allocation", "", tiled("allocation", "")),
                  Step("coarse_alloc", alloc, over("allocation", "", drawn), constant=drawn)]
    if state.scattering is not None:
        bounds = program_variant(pretransformed)
        raster = program_variant(pretransformed, ("interpolated", state.interpolated))
        steps += [Fill("ds.bounds", 0), Step("ds_arguments", "", (1, 1, 1)),
                  Step("ds_bounds", bounds, over("ds_bounds", bounds, drawn), constant=drawn),
                  Step("ds_info", "", (1, 1, 1)), Step("ds_clear", "", indirect=("ds.arguments", 0x60)),
                  Step("ds_raster", raster, over("ds_raster", raster, state.injected), constant=state.injected),
                  Step("ds_raster_depth", "", indirect=("ds.arguments", 0)),
                  *(Step("ds_mip", "", indirect=("ds.arguments", 16 * level), constant=level, level=level)
                    for level in range(1, _VOLUME_MIPS))]
        if state.lut:
            size = LAYOUT.set_textures["ds.lut"][1]
            steps.append(Step("ds_lut", "", tuple(map(_over, size, threads("ds_lut", "")))))
    shading = "material_shading" if state.material_shading else "shading"
    shaded = program_variant(pretransformed, ("shrink_shadow", state.shrink_shadow),
                      ("projection_lights", state.projection_lights),
                      *(((name, True),) if (name := (state.scattering or "").lower()) else ()))
    steps += [Step(shading, shaded, over(shading, shaded, drawn), constant=drawn),
              Step("distributor", "countup" if state.countup else "",
                   tiled("distributor", "countup" if state.countup else ""))]
    if state.mix:
        steps.append(Step("fill", program_variant(pretransformed, ("blend", state.blend)),
                          indirect=("view.arguments", 0x10)))
    steps += [Step("fine", program_variant(pretransformed, ("32bit", state.bits32), ("countup", state.countup)),
                   indirect=("view.arguments", 0)),
              Step("depth_write", "")]
    return steps


__all__ = ["DirectState", "IBL_OPTIMIZED", "LAYOUT", "SCATTERING", "TILE", "direct_steps", "ibl_count", "ibl_tables",
           "work_sizes"]
