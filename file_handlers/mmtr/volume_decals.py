"""See docs/internals/volume-decals.md."""
from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass

lazy import numpy as np

from file_handlers.build_catalog import NamedFields, catalog_reader
lazy from file_handlers.engine_math import (
    BOX_CORNERS, affine_inverse, crt, half_bits, normalized, transform_product,
)

CATALOG_FORMAT = 2
CATALOG_DIRECTORY = "resources/data/volume_decals"


class VolumeDecalCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DecalBounds:
    box: tuple[float, float]
    smallest: float
    floor: float
    ceiling: float
    unbounded: tuple[float, float]
    # A pyramid whose shape has this bit opens along -Y.
    pyramid_y_bit: int


@dataclass(frozen=True, slots=True)
class VolumeDecalCatalog(NamedFields):
    game: str
    resource_version: int
    component: str
    file: str
    target: int
    fields: Mapping[str, str]
    programs: tuple[tuple[str, ...], ...]
    shape_rows: Mapping[int, int]
    drawn_shapes: frozenset[int]
    named: Mapping[str, object]
    variant_blend: int
    variant_opacity: int
    variants: Mapping[int, int]
    blend_variants: Mapping[int, int]
    map_bits: Mapping[str, int]
    second_alpha_bit: int
    second_alpha_map: str
    blend_shift: int
    blend_bits: int
    two_side_bit: int
    second_alpha_formats: frozenset[int]
    bindings: Mapping[str, str]
    fallback: str
    phase: str
    segment: int
    normals_target: int
    normals_read: str
    depth_read: str
    box_vertices: tuple[tuple[float, float, float], ...]
    box_indices: tuple[int, ...]
    # Drawn with the box's indices: x, y and the share from the near clip to the far one.
    pyramid_vertices: tuple[tuple[float, float, float], ...]
    pyramid_shapes: frozenset[int]
    constants: Mapping[str, str]
    unwritten: frozenset[str]
    roughness_mod: float
    clamps: Mapping[str, tuple[float, float]]
    paste_axes: Mapping[int, int]
    paste_axis: int
    tangent_axis: int
    degrees: tuple[float, float, float]
    uv_epsilon: float
    bounds: DecalBounds
    order_shift: int
    order_highest: int

    def files(self) -> tuple[str, ...]:
        return self.file, self.fallback


def catalog_from_dict(data: Mapping) -> VolumeDecalCatalog:
    if int(data["format"]) != CATALOG_FORMAT:
        raise VolumeDecalCatalogError(f"format {data['format']} is not {CATALOG_FORMAT}")
    programs = tuple(tuple(str(name) for name in row) for row in data["programs"])
    if len({len(row) for row in programs}) != 1:
        raise VolumeDecalCatalogError("program rows differ in length")
    variants, flags, bounds = data["variants"], data["flags"], data["bounds"]
    width = len(programs[0])
    chosen = [*map(int, variants["valid"].values()), *map(int, variants["blends"].values())]
    if any(not 0 < number < width for number in chosen):
        raise VolumeDecalCatalogError("a variant falls outside the program rows")
    if any(not 0 <= int(row) < len(programs) for row in data["shape_rows"].values()):
        raise VolumeDecalCatalogError("a shape row falls outside the programs")
    fields = {str(key): str(value) for key, value in data["fields"].items()}
    maps = {str(name): int(bit) for name, bit in flags["maps"].items()}
    if flags["second_alpha_map"] not in maps:
        raise VolumeDecalCatalogError(f"the second alpha's map {flags['second_alpha_map']} has no valid bit")
    if len(data["pyramid"]["vertices"]) != len(data["box"]["vertices"]):
        raise VolumeDecalCatalogError("the pyramid's vertices do not match the box's indices")
    return VolumeDecalCatalog(
        str(data["game"]), int(data["resource_version"]), str(data["component"]), str(data["file"]),
        int(data["target"]), fields,
        programs, {int(shape): int(row) for shape, row in data["shape_rows"].items()},
        frozenset(map(int, data["drawn_shapes"])), dict(data["named"]), int(variants["blend"]),
        int(variants["opacity"]), {int(valid): int(number) for valid, number in variants["valid"].items()},
        {int(blend): int(number) for blend, number in variants["blends"].items()}, maps, int(flags["second_alpha"]),
        str(flags["second_alpha_map"]), int(flags["blend_shift"]), int(flags["blend_bits"]), int(flags["two_side"]),
        frozenset(map(int, data["second_alpha_formats"])),
        {str(name): str(field) for name, field in data["bindings"].items()}, str(data["fallback"]),
        str(data["phase"]), int(data["segment"]), int(data["normals"]["target"]), str(data["normals"]["read"]),
        str(data["depth_read"]),
        tuple(tuple(float(value) for value in vertex) for vertex in data["box"]["vertices"]),
        tuple(int(index) for index in data["box"]["indices"]),
        tuple(tuple(float(value) for value in vertex) for vertex in data["pyramid"]["vertices"]),
        frozenset(map(int, data["pyramid_shapes"])),
        {str(key): str(value) for key, value in data["constants"].items()}, frozenset(data.get("unwritten", ())),
        float(data["roughness_mod"]),
        {str(key): (float(low), float(high)) for key, (low, high) in data["clamps"].items()},
        {int(shape): int(axis) for shape, axis in data["paste_axis"].items() if shape != "default"},
        int(data["paste_axis"]["default"]), int(data["tangent_axis"]),
        tuple(float(value) for value in data["degrees"]), float(data["uv_epsilon"]),
        DecalBounds(tuple(map(float, bounds["box"])), float(bounds["smallest"]), float(bounds["floor"]),
                    float(bounds["ceiling"]), tuple(map(float, bounds["unbounded"])), int(bounds["pyramid_y_bit"])),
        int(data["order"]["shift"]), int(data["order"]["highest"]))


volume_decal_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, VolumeDecalCatalogError,
                                          "volume decal", (AttributeError, IndexError))


def _halves(*values: float) -> tuple[int, ...]:
    packed = [half_bits(value) for value in values]
    return tuple(packed[index] | packed[index + 1] << 16 for index in range(0, len(packed), 2))


def _word(value) -> int:
    return struct.unpack("<I", struct.pack("<f", float(value)))[0]


def _pair(value) -> tuple[float, float]:
    return (0.0, 0.0) if value is None else (float(value[0]), float(value[1]))


def _colour(value) -> int:
    return 0 if value is None else sum(int(channel) << 8 * place for place, channel in enumerate(value[:4]))


def map_set(fields: Mapping[str, object], name: str) -> bool:
    return bool(str(fields.get(name) or "").strip())


def valid_flags(catalog: VolumeDecalCatalog, fields: Mapping[str, object], alpha_format: int | None) -> int:
    flags = sum(bit for name, bit in catalog.map_bits.items() if map_set(fields, name))
    if catalog.field(fields, "second_alpha") and alpha_format is not None \
            and alpha_format in catalog.second_alpha_formats:
        flags |= catalog.second_alpha_bit
    blend = int(catalog.field(fields, "blend") or 0) & ((1 << catalog.blend_bits) - 1)
    two_side = bool(catalog.field(fields, "two_side"))
    return flags | blend << catalog.blend_shift | int(two_side) << catalog.two_side_bit


def program_name(catalog: VolumeDecalCatalog, fields: Mapping[str, object], flags: int) -> str:
    shape = int(catalog.field(fields, "shape") or 0)
    height, occlusion = (map_set(fields, catalog.fields[meaning]) for meaning in ("height", "occlusion"))
    if height or occlusion:
        named = catalog.named
        return (str(named["base"]) + str(named["shapes"].get(str(shape), "")) + (str(named["height"]) if height else "")
                + (str(named["occlusion"]) if occlusion else ""))
    variant, blend = 0, flags >> catalog.blend_shift & ((1 << catalog.blend_bits) - 1)
    if blend == catalog.variant_blend:
        if int(catalog.field(fields, "opacity_flag") or 0) == catalog.variant_opacity:
            variant = catalog.variants.get(flags & ((1 << catalog.blend_shift) - 1), 0)
    else:
        variant = catalog.blend_variants.get(blend, 0)
    return catalog.programs[catalog.shape_rows.get(shape, 0)][variant]


def _clamped(catalog: VolumeDecalCatalog, fields: Mapping[str, object], meaning: str, default: float) -> np.float32:
    value = catalog.field(fields, meaning)
    value = np.float32(default if value is None else value)
    low, high = catalog.clamps.get(meaning, (None, None))
    if low is not None and not np.float32(low) <= value:
        return np.float32(low)
    if high is not None and not value <= np.float32(high):
        return np.float32(high)
    return value


def _pyramid_tan(catalog: VolumeDecalCatalog, fields: Mapping[str, object]) -> np.float32:
    f = np.float32
    half_angle, per_degree, pi = (f(value) for value in catalog.degrees)
    return f(crt("tanf")(float(f(catalog.field(fields, "pyramid_angle") or 0) * half_angle * per_degree * pi)))


def decal_words(catalog: VolumeDecalCatalog, fields: Mapping[str, object], world: np.ndarray,
                alpha_format: int | None) -> dict[str, tuple[int, ...]]:
    """``world``: 4 x 4, row vectors (the axes, then the position)."""
    f, names = np.float32, catalog.constants
    world = np.asarray(world, f).reshape(4, 4)
    flags = valid_flags(catalog, fields, alpha_format)
    one = f(1)
    # The inverse's columns are the rows a world position takes to the decal's space.
    inverse = affine_inverse(world)
    shape = int(catalog.field(fields, "shape") or 0)
    paste = normalized(world[catalog.paste_axes.get(shape, catalog.paste_axis)])
    tangent = normalized(world[catalog.tangent_axis])
    binormal = (tangent[1] * paste[2] - tangent[2] * paste[1], tangent[2] * paste[0] - tangent[0] * paste[2],
                tangent[0] * paste[1] - tangent[1] * paste[0])
    per_degree, pi = (f(value) for value in catalog.degrees[1:])
    near, far = (f(catalog.field(fields, meaning) or 0) for meaning in ("near", "far"))
    inverse_tan = one / _pyramid_tan(catalog, fields)
    ratio = near / far
    epsilon = f(catalog.uv_epsilon)

    def reciprocal(value: float) -> np.float32:
        value = f(value)
        return one / value if abs(value) >= epsilon else f(0)

    uv_offset, uv_scale = (_pair(catalog.field(fields, meaning)) for meaning in ("uv_offset", "uv_scale"))
    if map_set(fields, catalog.fields["height"]):
        # Setting UV offset or scale copies it onto the normal UV of a decal with a height map.
        normal_offset, normal_scale = uv_offset, uv_scale
    else:
        normal_offset, normal_scale = (_pair(catalog.field(fields, meaning))
                                       for meaning in ("normal_uv_offset", "normal_uv_scale"))
    normal_reciprocal = tuple(reciprocal(value) for value in normal_scale)

    def ranged(meaning: str) -> tuple[int, ...]:
        low, high = _pair(catalog.field(fields, meaning))
        return _halves(min(low, high), high)
    angle = _clamped(catalog, fields, "angle", 0.0)
    words = {
        names["world"]: tuple(_word(value) for value in world.reshape(-1)),
        names["near"]: (_word(near),), names["far"]: (_word(far),), names["tan"]: (_word(inverse_tan),),
        names["inverse_world"]: tuple(_word(value) for value in inverse[:, :3].T.reshape(-1)),
        names["valid"]: (flags,), names["opacity_flag"]: (int(catalog.field(fields, "opacity_flag") or 0),),
        names["paste"]: _halves(*paste, 1.0), names["tangent"]: _halves(*tangent, 1.0),
        names["binormal"]: _halves(*binormal, 1.0),
        names["opacity"]: (_word(f(catalog.field(fields, "opacity") or 0)),),
        names["angle"]: (_word(crt("cosf")(float(angle * per_degree * pi))),),
        names["falloff"]: (_word(_clamped(catalog, fields, "falloff", 0.0)),),
        names["parallax"]: (_word(_clamped(catalog, fields, "parallax", 0.0)),),
        names["uv"]: _halves(*uv_offset, *(reciprocal(value) for value in uv_scale)),
        names["normal_uv"]: _halves(*normal_offset, *normal_reciprocal),
        names["base_color"]: (_colour(catalog.field(fields, "base_color")),),
        names["roughness"]: (_word(catalog.roughness_mod),),
        names["emissive"]: (_colour(catalog.field(fields, "emissive")),),
        names["emissive_intensity"]: (_word(f(catalog.field(fields, "emissive_intensity") or 0)),),
        names["normal"]: (_word(f(catalog.field(fields, "normal") or 0)),),
        names["alpha_range"]: ranged("alpha_range"), names["alpha_range_g"]: ranged("alpha_range_g"),
        names["near_plane"]: (_word(near),), names["far_plane"]: (_word(far),),
        names["inverse_tan"]: (_word(inverse_tan),), names["z_to_linear"]: (_word(ratio), _word(ratio - one)),
        names["signs"]: _halves(*(-1.0 if value < 0 else 1.0 for value in normal_reciprocal))}
    return words


def decal_order(catalog: VolumeDecalCatalog, fields: Mapping[str, object], made: int) -> int:
    priority = int(catalog.field(fields, "priority") or 0)
    return (catalog.order_highest - priority) << catalog.order_shift | made & ((1 << catalog.order_shift) - 1)


def _view_box(low, high, matrix) -> tuple[np.ndarray, np.ndarray]:
    """Boxes (``low``, ``high``: 3 or n x 3) through ``matrix`` (row vectors, 4 x 4 or n x 4 x 4) as the bounds test
    takes them: each corner through the rows without the translation, (y r1 + x r0) + z r2, their bounds, then the
    translation."""
    rows = np.asarray(matrix, np.float32).reshape(-1, 4, 4)
    low, high = np.asarray(low), np.asarray(high)
    corners = np.where(BOX_CORNERS, high.reshape(-1, 1, 3), low.reshape(-1, 1, 3)).astype(np.float32)
    turned = (corners[..., 1:2] * rows[:, None, 1] + corners[..., 0:1] * rows[:, None, 0]) \
        + corners[..., 2:3] * rows[:, None, 2]
    least, most = (turned.min(axis=1) + rows[:, 3])[:, :3], (turned.max(axis=1) + rows[:, 3])[:, :3]
    return (least[0], most[0]) if low.ndim == 1 and rows.shape[0] == 1 else (least, most)


def decal_box(catalog: VolumeDecalCatalog, fields: Mapping[str, object]) -> tuple[np.ndarray, np.ndarray]:
    f = np.float32
    shape = int(catalog.field(fields, "shape") or 0)
    if shape not in catalog.pyramid_shapes:
        return np.full(3, catalog.bounds.box[0], f), np.full(3, catalog.bounds.box[1], f)
    near, far = (f(catalog.field(fields, meaning) or 0) for meaning in ("near", "far"))
    tan, one = _pyramid_tan(catalog, fields), f(1)
    low, high = np.full(3, np.finfo(f).max, f), np.full(3, -np.finfo(f).max, f)
    for x, y, share in np.asarray(catalog.pyramid_vertices, f):
        spread = (one - share) * near + share * far
        depth = (far - near) * share + near
        point = np.array((x * tan * spread, -depth, y * tan * spread) if shape & catalog.bounds.pyramid_y_bit else
                         (x * tan * spread, y * tan * spread, depth), f)
        # minps and maxps.
        low, high = np.where(low < point, low, point), np.where(high > point, high, point)
    return low, high


def decal_bounds(catalog: VolumeDecalCatalog, box: tuple[np.ndarray, np.ndarray], world: np.ndarray, view: np.ndarray,
                 projection: np.ndarray, near: float) -> tuple:
    """The depth bounds test's range for a decal whose PreferObbFlag is clear: ``box`` its own (``decal_box``),
    matrices of row vectors, ``projection`` of reversed depth; of decals stacked (``box`` n x 3 each, ``world``
    n x 4 x 4), the arrays of their ranges."""
    return _projected_bounds(catalog, *_view_box(*box, transform_product(world, view)), projection, near)


def _projected_bounds(catalog: VolumeDecalCatalog, least: np.ndarray, most: np.ndarray, projection: np.ndarray,
                      near: float) -> tuple[float, float]:
    """The range for boxes in view space (``least``, ``most``: 3 or n x 3)."""
    f, rule = np.float32, catalog.bounds
    single = np.ndim(least) == 1
    least, most = np.array(least, f).reshape(-1, 3), np.array(most, f).reshape(-1, 3)
    clipped = most[:, 2] > 0
    # Held to the near plane as min(z, -near) and min(z, -near - floor) take it.
    for box, plane in ((least, f(-near)), (most, f(-near) - f(rule.floor))):
        box[clipped] = np.stack([np.zeros(len(box), f), np.zeros(len(box), f),
                                 np.where(plane < box[:, 2], plane, box[:, 2])], axis=1)[clipped]
    rows = np.asarray(projection, f).reshape(4, 4)
    corners = np.where(BOX_CORNERS, most[:, None], least[:, None]).astype(f)
    points = (corners[..., 1:2] * rows[1] + corners[..., 0:1] * rows[0]) + (corners[..., 2:3] * rows[2] + rows[3])
    projected = points[..., :3] / points[..., 3:]
    low_ndc, high_ndc = projected.min(axis=1), projected.max(axis=1)
    unbounded = ~clipped & ~(f(rule.smallest) * f(rule.smallest)
                             < (high_ndc[:, 1] - low_ndc[:, 1]) * (high_ndc[:, 0] - low_ndc[:, 0]))
    # As max and min of Python floats take them.
    low, high = low_ndc[:, 2].astype(np.float64), high_ndc[:, 2].astype(np.float64)
    lowest = np.where(rule.floor > low, rule.floor, low)
    top = np.where(rule.ceiling < high, rule.ceiling, high)
    found = (np.where(unbounded, rule.unbounded[0], lowest),
             np.where(unbounded, rule.unbounded[1], np.where(top > lowest, top, lowest)))
    return (float(found[0][0]), float(found[1][0])) if single else found


__all__ = ["DecalBounds", "VolumeDecalCatalog", "VolumeDecalCatalogError", "catalog_from_dict", "decal_bounds",
           "decal_box", "decal_order", "decal_words", "map_set", "program_name", "valid_flags",
           "volume_decal_catalog_for"]
