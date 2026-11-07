"""See docs/internals/speedtree.md, Catalog."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from file_handlers.build_catalog import catalog_reader
from file_handlers.stmesh.stmesh_file import COMPONENT, FOLIAGE, PLACEMENTS, LodProfile

CATALOG_FORMAT = 4
CATALOG_DIRECTORY = "resources/data/speedtree"
# ``CBBillboardNormal`` entries.
BILLBOARD_VIEWS = 70
_PROFILE_VALUES = frozenset(name for name, item in LodProfile.__dataclass_fields__.items() if item.type == "float")


class SpeedTreeCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SpeedTreePrograms:
    # Batch type name -> (marker, kind).
    shader_types: Mapping[str, tuple[str, str]]
    material_markers: tuple[tuple[str, int], ...]
    kinds_taking_material_markers: frozenset[str]
    kinds_ignoring_material_markers: frozenset[str]


@dataclass(frozen=True, slots=True)
class SpeedTreeBuffers:
    wind_state: str
    wind_states: int
    lod_transition: str
    lod_transition_shown: bytes
    density_cull: str | None
    density_cull_shown: bytes | None
    # The engine writes each instance's LOD transition there as a float.
    lod_transition_float: bool = False


@dataclass(frozen=True, slots=True)
class FoliageBillboards:
    mesh_constant: str
    mesh_constant_size: int
    cross_fading_flag: int
    normals_buffer: str
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
    billboard_cosine: str | None = None
    profile: Mapping[str, str] = field(default_factory=dict)


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
class SpeedTreeLod:
    bias: int
    manual: tuple[str, int, str] | None = None
    narrow: tuple[float, int] | None = None
    nudge: float = field(kw_only=True)


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
    lod_transition_input: str | None = None
    pivots: str | None = None
    billboard_cross_fade: Mapping[str, Mapping[int, float]] = field(default_factory=dict)
    lod: SpeedTreeLod | None = None


@dataclass(frozen=True, slots=True)
class SpeedTreeCatalog:
    game: str
    resource_version: int
    programs: SpeedTreePrograms
    placements: Mapping[str, SpeedTreePlacement]

    @property
    def viewed(self) -> SpeedTreePlacement:
        return next(iter(self.placements.values()))


def _programs(data: Mapping) -> SpeedTreePrograms:
    markers = tuple((str(marker), sum(1 << int(bit) for bit in bits)) for marker, bits in data["material_markers"])
    taking, ignoring = (frozenset(map(str, data[key]))
                        for key in ("kinds_taking_material_markers", "kinds_ignoring_material_markers"))
    if taking & ignoring:
        raise SpeedTreeCatalogError("a SpeedTree kind both takes and ignores its material's markers")
    return SpeedTreePrograms({str(batch): (str(marker), str(kind)) for batch, (marker, kind) in
                              data["shader_types"].items()}, markers, taking, ignoring)


_BILLBOARD_BUFFERS = ("mesh_constant", "billboard_normals")
_BILLBOARD_VALUES = ("billboard_normals", "billboard_wind_noise_scale")


def _buffers(data: Mapping) -> SpeedTreeBuffers:
    wind, lod, density = data["wind_state"], data["lod_transition"], data.get("density_cull")
    if lod.get("value", "float") != "float":
        raise SpeedTreeCatalogError(f"a SpeedTree LOD transition is written as a float or not at all, not "
                                    f"{lod['value']!r}")
    return SpeedTreeBuffers(str(wind["name"]), int(wind["states"]), str(lod["name"]), bytes.fromhex(lod["shown"]),
                            None if density is None else str(density["name"]),
                            None if density is None else bytes.fromhex(density["shown"]), "value" in lod)


def _billboards(data: Mapping) -> FoliageBillboards:
    normals = tuple(tuple(float(value) for value in row) for row in data["billboard_normals"])
    if len(normals) != BILLBOARD_VIEWS or any(len(row) != 4 for row in normals):
        raise SpeedTreeCatalogError(f"a SpeedTree catalog holds {BILLBOARD_VIEWS} billboard normals of four numbers")
    constant = data["buffers"]["mesh_constant"]
    return FoliageBillboards(str(constant["name"]), int(constant["size"]), int(constant["cross_fading_flag"]),
                             str(data["buffers"]["billboard_normals"]["name"]), normals,
                             float(data["billboard_wind_noise_scale"]))


def _cross_fade(data: Mapping) -> dict[str, dict[int, float]]:
    drawn = data["billboard_stem"] is not None
    if drawn != ("billboard_cross_fade" in data):
        raise SpeedTreeCatalogError("a SpeedTree placement lists billboard cross-fade cosines exactly when it draws "
                                    "billboards")
    return {str(kind): {int(views): float(value) for views, value in by_views.items()}
            for kind, by_views in data.get("billboard_cross_fade", {}).items()}


def _foliage(data: Mapping) -> FoliageBuffers:
    drawn = data["billboard_stem"] is not None
    if not drawn and (any(key in data["buffers"] for key in _BILLBOARD_BUFFERS)
                      or any(key in data for key in _BILLBOARD_VALUES)):
        raise SpeedTreeCatalogError("a SpeedTree foliage placement drawing no billboards holds no billboard constants")
    return FoliageBuffers(_buffers(data["buffers"]), _billboards(data) if drawn else None)


def _constant(data: Mapping) -> SpeedTreeConstant:
    fields = {str(name): tuple(float(value) for value in values) for name, values in data["fields"].items()}
    world, previous, cosine = data.get("world"), data.get("previous_world"), data.get("billboard_cosine")
    profile = {str(name): str(value) for name, value in data.get("profile", {}).items()}
    if world in fields or previous in fields or cosine in fields:
        raise SpeedTreeCatalogError("a SpeedTree constant's transform or billboard field also holds fixed values")
    if set(profile) & {*fields, world, previous, cosine} or not set(profile.values()) <= _PROFILE_VALUES:
        raise SpeedTreeCatalogError(f"a SpeedTree constant's fields taken from the mesh's LOD profile hold no other "
                                    f"value and name its distances ({', '.join(sorted(_PROFILE_VALUES))})")
    return SpeedTreeConstant(str(data["name"]), fields, None if world is None else str(world),
                             None if previous is None else str(previous), None if cosine is None else str(cosine),
                             profile)


def _meshlets(data: Mapping) -> MeshletPlacement:
    names = tuple(str(data[key]) for key in ("raster", "shade", "transform"))
    if not all("{billboard}" in name for name in names) or "{marker}" not in names[0]:
        raise SpeedTreeCatalogError("a SpeedTree meshlet placement's program names take the billboard word, and the "
                                    "raster's the marker")
    instance = {str(name): bytes.fromhex(value) for name, value in data["instance"].items()}
    return MeshletPlacement(*names, str(data["billboard"]), instance, bytes.fromhex(data["extended"]))


def _lod(name: str, data: Mapping) -> SpeedTreeLod:
    manual = data.get("manual")
    if manual is not None and name != COMPONENT:
        raise SpeedTreeCatalogError("only a component placement sets a LOD by its own fields")
    narrow = data.get("narrow")
    return SpeedTreeLod(int(data["bias"]), None if manual is None else (
        str(manual["field"]), int(manual["member"]), str(manual["share"])),
        None if narrow is None else (float(narrow["below"]), int(narrow["bias"])), nudge=float(data["nudge"]))


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
    transition = data.get("lod_transition_input")
    if transition is not None and transition not in inputs:
        raise SpeedTreeCatalogError(f"a SpeedTree placement's LOD transition input {transition!r} is none of its "
                                    f"per-instance inputs")
    return SpeedTreePlacement(name, str(data["stem"]), {str(key): str(value) for key, value in
                                                        data["input_types"].items()},
                              None if billboards is None else str(billboards), foliage, constants,
                              wind_state=None if wind is None else (str(wind["name"]), int(wind["states"])),
                              instance_inputs={str(key): bytes.fromhex(value) for key, value in inputs.items()},
                              lod_transition_input=None if transition is None else str(transition),
                              pivots=None if data.get("pivots") is None else str(data["pivots"]),
                              billboard_cross_fade=_cross_fade(data),
                              lod=_lod(name, data["lod"]) if "lod" in data else None)


def catalog_from_dict(data: Mapping) -> SpeedTreeCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise SpeedTreeCatalogError(f"SpeedTree catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    placements = {str(name): _placement(str(name), item) for name, item in data["placements"].items()}
    if not placements:
        raise SpeedTreeCatalogError("a SpeedTree catalog describes at least one placement")
    return SpeedTreeCatalog(str(data["game"]), int(data["resource_version"]), _programs(data["programs"]),
                            placements)


speedtree_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, SpeedTreeCatalogError, "SpeedTree")


__all__ = ["BILLBOARD_VIEWS", "CATALOG_DIRECTORY", "CATALOG_FORMAT", "COMPONENT", "FOLIAGE", "FoliageBillboards",
           "FoliageBuffers", "MeshletPlacement",
           "PLACEMENTS", "SpeedTreeBuffers", "SpeedTreeCatalog", "SpeedTreeCatalogError", "SpeedTreeConstant",
           "SpeedTreeLod", "SpeedTreePlacement", "SpeedTreePrograms", "catalog_from_dict", "speedtree_catalog_for"]
