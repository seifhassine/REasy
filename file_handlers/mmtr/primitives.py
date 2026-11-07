"""See docs/internals/mesh-preview.md, Primitive shapes."""
from __future__ import annotations

import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache

lazy import numpy as np

from file_handlers.build_catalog import catalog_file_name, catalog_reader
lazy from file_handlers.engine_math import crt

lazy from .engine_buffers import instance_values, laid_out, with_words

CATALOG_FORMAT = 1
CATALOG_DIRECTORY = "resources/data/primitives"
_f = np.float32
# A vertex: position, then the normal as signed bytes.
VERTEX_STRIDE = 16


class PrimitiveCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PrimitiveCatalog:
    game: str
    resource_version: int
    component: str
    fields: Mapping[str, str]
    values: tuple[str, ...]
    file: str
    target: int
    program: str
    shapes: Mapping[int, str]
    generators: Mapping[str, Mapping[str, float]]
    normal_scale: float
    opaque_alpha: float
    segment: int
    stencil_reference: int
    transparent_program: str
    transparent_segment: int
    key_scale: float
    buffer: str
    constants: Mapping[str, str]
    world_variable: str
    world_words: Mapping[int, str]

    @property
    def read(self) -> frozenset[str]:
        return frozenset((*self.fields.values(), *self.values, *self.world_words.values()))

    def geometry(self, name: str) -> tuple[bytes, bytes]:
        return shape_geometry(name, tuple(sorted(self.generators[name].items())), self.normal_scale)


def catalog_from_dict(data: Mapping) -> PrimitiveCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise PrimitiveCatalogError(f"primitive catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    component, constants = data["component"], data["constants"]
    fields = {str(name): str(value) for name, value in component.items() if name not in ("type", "values")}
    if set(fields) != {"shape", "color", "interleave"}:
        raise PrimitiveCatalogError("the component's fields are not named for every meaning REasy reads")
    shapes = {int(number): str(name) for number, name in data["shapes"].items()}
    if not set(shapes.values()) <= _GENERATORS.keys():
        raise PrimitiveCatalogError(f"a shape is none of {', '.join(sorted(_GENERATORS))}")
    generators = {str(name): {str(key): value for key, value in held.items()}
                  for name, held in data["generators"].items()}
    if any(set(generators.get(name, ())) != _GENERATORS[name][1] for name in shapes.values()):
        raise PrimitiveCatalogError("a shape's generator values are missing or not the ones it reads")
    if set(constants) != {"buffer", "color", "values"}:
        raise PrimitiveCatalogError("the constants are not named for the colour and the values")
    world, transparent = data["world"], data["transparent"]
    return PrimitiveCatalog(
        str(data["game"]), int(data["resource_version"]), str(component["type"]), fields,
        tuple(str(name) for name in component["values"]), str(data["file"]), int(data["target"]),
        str(data["program"]), shapes, generators, float(data["normal_scale"]), float(data["opaque_alpha"]),
        int(data["segment"]),
        int(data["stencil_reference"]), str(transparent["program"]), int(transparent["segment"]),
        float(transparent["key_scale"]), str(constants["buffer"]),
        {str(constants["color"]): "color", str(constants["values"]): "values"}, str(world["variable"]),
        {word: str(name) for word, name in enumerate(world["words"]) if name is not None})


primitive_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, PrimitiveCatalogError, "primitive",
                                       (AttributeError,))


def colour(value) -> tuple[float, float, float, float]:
    return tuple(float(_f(_f(int(channel)) / _f(255.0))) for channel in value)


def material_constants(layout, catalog: PrimitiveCatalog, fields: Mapping[str, object]) -> bytes:
    held = {"color": np.array(colour(fields[catalog.fields["color"]]), np.float64),
            "values": np.array([float(fields[name]) for name in catalog.values], np.float64)}
    return bytes(laid_out(layout, {name: held[meaning] for name, meaning in catalog.constants.items()}, True))


def world_constants(layout, catalog: PrimitiveCatalog, fields: Mapping[str, object], placement) -> bytes:
    count = next(variable for variable in layout.variables if variable.name == catalog.world_variable).byte_size // 4
    words = [int(np.array(float(fields[catalog.world_words[word]]), "<f4").view("<u4"))
             if word in catalog.world_words else 0 for word in range(count)]
    return bytes(with_words(laid_out(layout, instance_values(placement), True), layout,
                            {catalog.world_variable: words}))


def transparent_key(position, camera, scale: float) -> int:
    x, y, z = (_f(_f(eye) - _f(at)) for eye, at in zip(camera, position))
    distance = np.sqrt(_f(_f(_f(x * x) + _f(y * y)) + _f(z * z)))
    return (-1 - int(_f(distance * _f(scale)))) & 0xFFFFFFFF


def _byte(value, scale: float) -> int:
    return int(crt("floorf")(_f(_f(value) * _f(scale)))) & 0xFF


def _sphere(held: Mapping[str, float], scale: float) -> tuple[list, list[int]]:
    rings, steps = int(held["rings"]), int(held["steps"])
    vertices, indices = [], []
    for ring in range(rings):
        azimuth = _f(_f(_f(ring) / _f(held["ring_divisor"])) * _f(held["turn"]))
        sine, cosine = _f(crt("sinf")(azimuth)), _f(crt("cosf")(azimuth))
        for step in range(steps):
            polar = _f(_f(_f(step) / _f(held["step_divisor"])) * _f(held["half_turn"]))
            ring_sine, ring_cosine = _f(crt("sinf")(polar)), _f(crt("cosf")(polar))
            position = (_f(ring_sine * sine), ring_cosine, _f(ring_sine * cosine))
            vertices.append((position, tuple(_byte(value, scale) for value in position)))
    last = steps - 1
    for start in range(0, (rings - 1) * steps, steps):
        top = start + steps
        for _step in range(last):
            other = ((top - last) & last) + start
            indices += [top, top - steps, other, other + steps, top, other]
            top += 1
    return vertices, indices


def _cube_face(face: int, held: Mapping[str, float]) -> tuple[tuple, tuple, tuple]:
    zero, one = _f(0.0), _f(1.0)
    if face == 0:
        return (one, zero, zero), (zero, one, zero), (zero, zero, one)
    if face == 1:
        half = _f(held["half_turn"])
        sine, cosine = _f(crt("sinf")(half)), _f(crt("cosf")(half))
        return (cosine, sine, zero), (_f(-sine), cosine, zero), (zero, zero, one)
    quarter = _f(held["quarter_turn"])
    angle = _f(_f(face - 2) * quarter)
    sine, cosine = _f(crt("sinf")(angle)), _f(crt("cosf")(angle))
    quarter_sine, quarter_cosine = _f(crt("sinf")(quarter)), _f(crt("cosf")(quarter))
    minus = _f(-sine)

    def summed(*terms):
        total = terms[0]
        for term in terms[1:]:
            total = _f(total + term)
        return total
    return ((summed(_f(sine * zero), _f(zero * zero), _f(quarter_cosine * cosine), _f(quarter_sine * zero)),
             summed(_f(zero * zero), _f(zero * zero), _f(quarter_cosine * zero), _f(quarter_sine * one)),
             summed(_f(cosine * zero), _f(zero * zero), _f(quarter_cosine * minus), _f(quarter_sine * zero))),
            (summed(_f(sine * zero), _f(zero * zero), _f(_f(-quarter_sine) * cosine), _f(quarter_cosine * zero)),
             summed(_f(zero * zero), _f(zero * zero), _f(_f(-quarter_sine) * zero), _f(quarter_cosine * one)),
             summed(_f(cosine * zero), _f(zero * zero), _f(_f(-quarter_sine) * minus), _f(quarter_cosine * zero))),
            (summed(_f(one * sine), _f(zero * zero), _f(zero * cosine), _f(zero * zero)),
             summed(_f(one * zero), _f(zero * zero), _f(zero * zero), _f(zero * one)),
             summed(_f(one * cosine), _f(zero * zero), _f(zero * minus), _f(zero * zero))))


def _cube(held: Mapping[str, float], scale: float) -> tuple[list, list[int]]:
    quad = [(-1, 1, -1), (-1, 1, 1), (1, 1, 1), (1, 1, -1)]
    vertices, indices = [], []
    for face in range(6):
        rows = _cube_face(face, held)
        (m00, m01, m02), (m10, m11, m12), (m20, m21, m22) = rows
        normal = (_f(_f(_f(1.0) * m10) + _f(_f(0.0) * m00)) + _f(_f(0.0) * m20)),
        normal += (_f(_f(_f(1.0) * m11) + _f(_f(0.0) * m01)) + _f(_f(0.0) * m21)),
        normal += (_f(_f(_f(1.0) * m12) + _f(_f(0.0) * m02)) + _f(_f(0.0) * m22)),
        packed = tuple(_byte(value, scale) for value in normal)
        for x, y, z in quad:
            x, y, z = _f(x), _f(y), _f(z)
            vertices.append(((_f(_f(_f(x * m00) + _f(y * m10)) + _f(z * m20)),
                              _f(_f(_f(x * m01) + _f(y * m11)) + _f(z * m21)),
                              _f(_f(_f(x * m02) + _f(y * m12)) + _f(z * m22))), packed))
        indices += [4 * face, 4 * face + 1, 4 * face + 2, 4 * face, 4 * face + 2, 4 * face + 3]
    return vertices, indices


def _cylinder(held: Mapping[str, float], scale: float) -> tuple[list, list[int]]:
    steps = int(held["steps"])
    angles = [_f(_f(_f(step) * _f(held["step"])) * _f(held["turn"])) for step in range(steps)]
    vertices, indices = [], []
    for height, top in ((_f(1.0), True), (_f(-1.0), False)):
        start, packed = len(vertices), (_byte(0.0, scale), _byte(height, scale), _byte(0.0, scale))
        vertices.append(((_f(0.0), height, _f(0.0)), packed))
        for step, angle in enumerate(angles):
            vertices.append(((_f(crt("sinf")(angle)), height, _f(crt("cosf")(angle))), packed))
            rim = ((step + 1) & (steps - 1)) + 1
            indices += [start, start + step + 1, start + rim] if top else [start, start + rim, start + step + 1]
    start, side = len(vertices), 2 * steps - 1
    for step, angle in enumerate(angles):
        sine, cosine = _f(crt("sinf")(angle)), _f(crt("cosf")(angle))
        packed = (_byte(sine, scale), _byte(0.0, scale), _byte(cosine, scale))
        vertices += [((sine, _f(1.0), cosine), packed), ((sine, _f(-1.0), cosine), packed)]
        indices += [start + 2 * step, start + 2 * step + 1, start + ((step + 1) * 2 & side),
                    start + ((step + 1) * 2 & side), start + 2 * step + 1, start + ((2 * step + 3) & side)]
    return vertices, indices


_GENERATORS: Mapping[str, tuple[Callable[[Mapping[str, float], float], tuple[list, list[int]]], frozenset[str]]] = {
    "sphere": (_sphere, frozenset({"rings", "ring_divisor", "turn", "steps", "step_divisor", "half_turn"})),
    "cube": (_cube, frozenset({"half_turn", "quarter_turn"})),
    "cylinder": (_cylinder, frozenset({"steps", "step", "turn"}))}


@cache
def shape_geometry(name: str, held: tuple[tuple[str, float], ...], scale: float) -> tuple[bytes, bytes]:
    vertices, indices = _GENERATORS[name][0](dict(held), scale)
    return (b"".join(struct.pack("<3f4B", *position, *normal, 0) for position, normal in vertices),
            struct.pack(f"<{len(indices)}H", *indices))


__all__ = ["CATALOG_DIRECTORY", "CATALOG_FORMAT", "PrimitiveCatalog", "PrimitiveCatalogError", "VERTEX_STRIDE",
           "catalog_file_name", "catalog_from_dict", "colour", "material_constants", "primitive_catalog_for",
           "shape_geometry", "transparent_key", "world_constants"]
