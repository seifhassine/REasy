"""See docs/internals/speedtree.md, Catalog."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from file_handlers.build_catalog import catalog_reader
from file_handlers.stmesh.stmesh_file import COMPONENT, FOLIAGE, PLACEMENTS

CATALOG_FORMAT = 3
CATALOG_DIRECTORY = "resources/data/speedtree"
# ``CBBillboardNormal`` entries.
BILLBOARD_VIEWS = 70


class SpeedTreeCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SpeedTreePrograms:
    # Batch type name -> (marker, kind).
    shader_types: Mapping[str, tuple[str, str]]
    # (marker, MDF flag mask adding it), in writing order.
    material_markers: tuple[tuple[str, int], ...]
    kinds_taking_material_markers: frozenset[str]


@dataclass(frozen=True, slots=True)
class SpeedTreeBuffers:
    wind_state: str
    wind_states: int
    lod_transition: str
    lod_transition_shown: bytes
    density_cull: str | None
    density_cull_shown: bytes | None


@dataclass(frozen=True, slots=True)
class FoliageBillboards:
    mesh_constant: str
    mesh_constant_size: int
    cross_fading_flag: int
    normals_buffer: str
    # Threshold type -> side view count -> cosine.
    cross_fade: Mapping[str, Mapping[int, float]]
    normals: tuple[tuple[float, float, float, float], ...]
    wind_noise_scale: float


@dataclass(frozen=True, slots=True)
class FoliageBuffers:
    buffers: SpeedTreeBuffers
    billboards: FoliageBillboards | None


@dataclass(frozen=True, slots=True)
class SpeedTreeConstant:
    name: str
    fields: Mapping[str, tuple[float, ...]]
    world: str | None = None
    previous_world: str | None = None


@dataclass(frozen=True, slots=True)
class MeshletPlacement:
    raster: str
    shade: str
    transform: str
    billboard: str
    # Buffer name -> an instance record's bytes.
    instance: Mapping[str, bytes]
    extended: bytes


@dataclass(frozen=True, slots=True)
class SpeedTreePlacement:
    name: str
    # Program name stem with ``{marker}``, ``{kind}`` and ``{input}`` fields.
    stem: str | None
    # Grass-marked wind config -> input type ("grass" or "tree").
    input_types: Mapping[str, str]
    billboard_stem: str | None
    foliage: FoliageBuffers | None = None
    constants: tuple[SpeedTreeConstant, ...] = ()
    meshlets: MeshletPlacement | None = None
    # (buffer name, wind state count).
    wind_state: tuple[str, int] | None = None
    # Per-instance vertex inputs: semantic with index (``GENERIC4``) -> one instance's bytes.
    instance_inputs: Mapping[str, bytes] = field(default_factory=dict)
    pivots: str | None = None


@dataclass(frozen=True, slots=True)
class SpeedTreeCatalog:
    game: str
    resource_version: int
    programs: SpeedTreePrograms
    placements: Mapping[str, SpeedTreePlacement]
    source: Mapping = field(default_factory=dict)

    @property
    def viewed(self) -> SpeedTreePlacement:
        return next(iter(self.placements.values()))


def _programs(data: Mapping) -> SpeedTreePrograms:
    markers = tuple((str(marker), sum(1 << int(bit) for bit in bits)) for marker, bits in data["material_markers"])
    return SpeedTreePrograms({str(batch): (str(marker), str(kind)) for batch, (marker, kind) in
                              data["shader_types"].items()}, markers,
                             frozenset(map(str, data["kinds_taking_material_markers"])))


_BILLBOARD_BUFFERS = ("mesh_constant", "billboard_normals")
_BILLBOARD_VALUES = ("billboard_cross_fade", "billboard_normals", "billboard_wind_noise_scale")


def _buffers(data: Mapping) -> SpeedTreeBuffers:
    wind, lod, density = data["wind_state"], data["lod_transition"], data.get("density_cull")
    return SpeedTreeBuffers(str(wind["name"]), int(wind["states"]), str(lod["name"]), bytes.fromhex(lod["shown"]),
                            None if density is None else str(density["name"]),
                            None if density is None else bytes.fromhex(density["shown"]))


def _billboards(data: Mapping) -> FoliageBillboards:
    normals = tuple(tuple(float(value) for value in row) for row in data["billboard_normals"])
    if len(normals) != BILLBOARD_VIEWS or any(len(row) != 4 for row in normals):
        raise SpeedTreeCatalogError(f"a SpeedTree catalog holds {BILLBOARD_VIEWS} billboard normals of four numbers")
    fades = {str(kind): {int(views): float(value) for views, value in by_views.items()}
             for kind, by_views in data["billboard_cross_fade"].items()}
    constant = data["buffers"]["mesh_constant"]
    return FoliageBillboards(str(constant["name"]), int(constant["size"]), int(constant["cross_fading_flag"]),
                             str(data["buffers"]["billboard_normals"]["name"]), fades, normals,
                             float(data["billboard_wind_noise_scale"]))


def _foliage(data: Mapping) -> FoliageBuffers:
    drawn = data["billboard_stem"] is not None
    if not drawn and (any(key in data["buffers"] for key in _BILLBOARD_BUFFERS)
                      or any(key in data for key in _BILLBOARD_VALUES)):
        raise SpeedTreeCatalogError("a SpeedTree foliage placement drawing no billboards holds no billboard constants")
    return FoliageBuffers(_buffers(data["buffers"]), _billboards(data) if drawn else None)


def _constant(data: Mapping) -> SpeedTreeConstant:
    fields = {str(name): tuple(float(value) for value in values) for name, values in data["fields"].items()}
    world, previous = data.get("world"), data.get("previous_world")
    if world in fields or previous in fields:
        raise SpeedTreeCatalogError("a SpeedTree constant's transform field also holds fixed values")
    return SpeedTreeConstant(str(data["name"]), fields, None if world is None else str(world),
                             None if previous is None else str(previous))


def _meshlets(data: Mapping) -> MeshletPlacement:
    names = tuple(str(data[key]) for key in ("raster", "shade", "transform"))
    if not all("{billboard}" in name for name in names) or "{marker}" not in names[0]:
        raise SpeedTreeCatalogError("a SpeedTree meshlet placement's program names take the billboard word, and the "
                                    "raster's the marker")
    instance = {str(name): bytes.fromhex(value) for name, value in data["instance"].items()}
    return MeshletPlacement(*names, str(data["billboard"]), instance, bytes.fromhex(data["extended"]))


def _placement(name: str, data: Mapping) -> SpeedTreePlacement:
    if name not in PLACEMENTS:
        raise SpeedTreeCatalogError(f"SpeedTree placement {name!r} is none of {', '.join(PLACEMENTS)}")
    if "meshlets" in data:
        if set(data) != {"meshlets"}:
            raise SpeedTreeCatalogError("a SpeedTree placement of meshlet meshes holds only their programs and data")
        return SpeedTreePlacement(name, None, {}, None, meshlets=_meshlets(data["meshlets"]))
    billboards = data["billboard_stem"]
    constants = tuple(_constant(item) for item in data.get("constants", ()))
    if (name == FOLIAGE) == bool(constants):
        raise SpeedTreeCatalogError("only a component placement lists the constants the engine fills per component")
    foliage = _foliage(data) if name == FOLIAGE else None
    wind, inputs = data.get("wind_state"), data.get("instance_inputs", {})
    if name == FOLIAGE and (wind is not None or inputs):
        raise SpeedTreeCatalogError("a SpeedTree foliage placement's wind state is among its buffers, and its "
                                    "instances' data in the engine's buffers")
    return SpeedTreePlacement(name, str(data["stem"]), {str(key): str(value) for key, value in
                                                        data["input_types"].items()},
                              None if billboards is None else str(billboards), foliage, constants,
                              wind_state=None if wind is None else (str(wind["name"]), int(wind["states"])),
                              instance_inputs={str(key): bytes.fromhex(value) for key, value in inputs.items()},
                              pivots=None if data.get("pivots") is None else str(data["pivots"]))


def catalog_from_dict(data: Mapping) -> SpeedTreeCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise SpeedTreeCatalogError(f"SpeedTree catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    placements = {str(name): _placement(str(name), item) for name, item in data["placements"].items()}
    if not placements:
        raise SpeedTreeCatalogError("a SpeedTree catalog describes at least one placement")
    return SpeedTreeCatalog(str(data["game"]), int(data["resource_version"]), _programs(data["programs"]),
                            placements, dict(data.get("source", {})))


speedtree_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, SpeedTreeCatalogError, "SpeedTree")


__all__ = ["BILLBOARD_VIEWS", "CATALOG_DIRECTORY", "CATALOG_FORMAT", "COMPONENT", "FOLIAGE", "FoliageBillboards",
           "FoliageBuffers", "MeshletPlacement",
           "PLACEMENTS", "SpeedTreeBuffers", "SpeedTreeCatalog", "SpeedTreeCatalogError", "SpeedTreeConstant",
           "SpeedTreePlacement", "SpeedTreePrograms", "catalog_from_dict", "speedtree_catalog_for"]
