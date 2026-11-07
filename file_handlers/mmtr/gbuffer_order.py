"""See docs/internals/mesh-preview.md, G-buffer order catalog."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial

lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.build_catalog import catalog_file_name, catalog_reader
lazy from file_handlers.mdf.mdf_file import material_flag
lazy from file_handlers.stmesh.stmesh_file import BatchType

lazy from .draw_rules import MESH_COMPONENT
lazy from .material_preview import MaterialPreviewError, PreviewMaterial
lazy from .mesh_render import MeshObject, MeshPart, PartError, _bound_targets
lazy from .meshlets import MeshletBuffer
lazy from .pass_choice import depth_only_program, zprepass_program
lazy from .surface_meaning import GBUFFER_PURPOSE, PREPASS_PURPOSE

CATALOG_FORMAT = 8
CATALOG_DIRECTORY = "resources/data/gbuffer_order"
DRAW_TYPES = ("solid", "alpha_test", "decal", "forward", "transparent")
MATERIAL_ROLES = ("alpha_test", "two_side", "emissive", "rough_transparent", "stencil_priority", "pixel_depth_offset")
_SEGMENTED = {"solid": "solid_emissive", "alpha_test": "alpha_test_emissive", "decal": "decal"}
_RENDERER = {"ForceZPrepass": bool, "PartialZPrepass": bool, "PartialZPrepassRange": float,
             "AlphaTestZPrepassFirst": bool, "ZPrepassDisable": bool, "BoundingLengthScale": float,
             "UseGpuOcclusionCulling": bool, "UseInstancingDraw": bool, "UseInstancingCount": int,
             "ShaderLodGlobalValue": int}


class GBufferOrderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class KeyField:
    name: str
    when: str | None = None
    unset: int | None = None
    renderer: str | None = None
    mask: int | None = None

    def value(self, rules, fields: Mapping[str, object] | None, renderer: Mapping[str, object]):
        if self.when is not None and not rules.value_of(fields, self.when):
            return 0
        value = rules.value_of(fields, self.name)
        if self.unset is not None and value == self.unset:
            value = renderer[self.renderer]
        return value if self.mask is None else int(value) & self.mask


@dataclass(frozen=True, slots=True)
class MaterialDraw:
    shading_type: int
    alpha_test: bool
    two_side: bool
    emissive: bool
    rough_transparent: bool
    stencil_priority: bool
    pixel_depth_offset: bool


@dataclass(frozen=True, slots=True)
class Placing:
    segment: int | None
    first: int | None
    other: int | None


@dataclass(frozen=True, slots=True)
class InstancerOrder:
    components: frozenset[str]
    draw_types: Mapping[str, frozenset[int]]
    two_side: tuple[str, ...]
    emissive: str
    alpha_test: str
    segments: Mapping[str, tuple[int, int]]
    prepass: Mapping[str, tuple[int, int]]

    def placing(self, rules, material) -> Placing | None:
        kind = next((name for name, types in self.draw_types.items() if material.shading_type in types), None)
        if kind is None:
            return None
        if kind == "solid" and rules.flags_by_name(material.material_flags, material.flag_version).get(
                self.alpha_test):
            kind = "alpha_test"
        flags, version = material.material_flags, material.flag_version
        two_side = any(material_flag(flags, version, name) for name in self.two_side)
        emissive = kind in _SEGMENTED and _SEGMENTED[kind] != kind and material_flag(flags, version, self.emissive)
        first = self.prepass[kind][two_side] if kind in self.prepass else None
        return Placing(self.segments[_SEGMENTED[kind] if emissive else kind][two_side], first, first)


@dataclass(frozen=True, slots=True)
class BatchOrder:
    components: frozenset[str]
    shading_types: frozenset[int]
    segments: Mapping[str, int]
    prepass: Mapping[str, int]
    prepass_when: Mapping[str, str]

    def placing(self, rules, material, batch: int) -> Placing | None:
        if material.shading_type not in self.shading_types:
            return None
        name = BatchType(batch).name
        flag = self.prepass_when.get(name)
        first = self.prepass.get(name)
        if flag is not None and not rules.flags_by_name(material.material_flags, material.flag_version).get(flag):
            first = None
        return Placing(self.segments[name], first, first)


@dataclass(frozen=True, slots=True)
class ShellOrder:
    components: frozenset[str]
    shading_types: frozenset[int]
    two_side: tuple[str, ...]
    alpha_test: tuple[str, ...]
    emissive: str
    segments: Mapping[str, tuple[int, int]]
    prepass: Mapping[str, tuple[int, int]]

    def placing(self, material) -> Placing | None:
        if material.shading_type not in self.shading_types:
            return None
        flags, version = material.material_flags, material.flag_version
        kind = "alpha_test" if any(material_flag(flags, version, name) for name in self.alpha_test) else "solid"
        two_side = any(material_flag(flags, version, name) for name in self.two_side)
        emissive = material_flag(flags, version, self.emissive)
        first = self.prepass[kind][two_side]
        return Placing(self.segments[_SEGMENTED[kind] if emissive else kind][two_side], first, first)


@dataclass(frozen=True, slots=True)
class GBufferOrder:
    game: str
    resource_version: int
    renderer: Mapping[str, object]
    distance_scale: float
    draw_types: Mapping[str, frozenset[int]]
    gbuffer_skipped: frozenset[int]
    prepass_skipped: frozenset[int]
    segments: Mapping[str, tuple[int, int]]
    stencil_priority: int
    prepass_first: Mapping[str, tuple[int, int]]
    prepass_other: int
    prepass_other_types: frozenset[str]
    material_flags: Mapping[str, str]
    apart: tuple[str, ...]
    key: tuple[KeyField, ...]
    instancer: InstancerOrder
    batches: BatchOrder
    ignore_depth: str
    ignore_depth_segments: tuple[int, ...]
    ignore_depth_stamped: frozenset[int]
    ignore_depth_program: str
    transparent_layer: frozenset[str]
    # The G-buffer targets a frame clears before its draws, with their clear colours; the others keep the last frame's
    # values where none writes.
    cleared: Mapping[int, tuple[float, ...]]
    shell_fur: ShellOrder | None = None

    @property
    def drawers(self) -> frozenset[str]:
        return self.instancer.components | self.batches.components

    @property
    def fields(self) -> frozenset[str]:
        return frozenset((*self.apart, self.ignore_depth,
                          *(name for item in self.key for name in (item.name, item.when) if name)))

    def material_draw(self, rules, material) -> MaterialDraw:
        named = rules.flags_by_name(material.material_flags, material.flag_version)
        return MaterialDraw(material.shading_type,
                            *(bool(named.get(self.material_flags[role])) for role in MATERIAL_ROLES))

    def draw_type(self, material: MaterialDraw) -> str:
        for name in ("forward", "transparent", "decal"):
            if material.shading_type in self.draw_types[name]:
                return name
        return "alpha_test" if material.alpha_test else "solid"

    def transparent(self, material: MaterialDraw) -> bool:
        return self.draw_type(material) in self.transparent_layer

    def placing(self, material: MaterialDraw, stencil_priority: bool) -> Placing:
        kind = self.draw_type(material)
        priority = stencil_priority and material.stencil_priority
        segment = None
        if kind in _SEGMENTED and material.shading_type not in self.gbuffer_skipped and not material.rough_transparent:
            segment = self.stencil_priority if priority else self.segments[
                _SEGMENTED[kind] if material.emissive else kind][material.two_side]
        renderer = self.renderer
        if (renderer["ZPrepassDisable"] or material.shading_type in self.prepass_skipped or material.rough_transparent
                or material.pixel_depth_offset and not material.alpha_test or kind not in self.prepass_first):
            return Placing(segment, None, None)
        first = self.stencil_priority if priority else self.prepass_first[kind][material.two_side]
        other = None
        if kind in self.prepass_other_types:
            other = self.stencil_priority if priority else self.prepass_other
        if renderer["AlphaTestZPrepassFirst"]:
            other = first
        return Placing(segment, first, other)

    def batch_state(self, rules, fields: Mapping[str, object], decals: bool) -> tuple | None:
        if any(rules.value_of(fields, name) for name in self.apart):
            return None
        return decals, *(item.value(rules, fields, self.renderer) for item in self.key)

    def drawn_first(self, instanced: bool) -> bool | None:
        renderer = self.renderer
        if renderer["ForceZPrepass"]:
            return True
        if instanced or not (renderer["PartialZPrepass"] and renderer["UseGpuOcclusionCulling"]):
            return False
        return None

    def near(self, boxes: np.ndarray, eye: np.ndarray) -> np.ndarray:
        boxes = np.asarray(boxes, np.float64)
        centre = (boxes[:, 0] + boxes[:, 1]) * 0.5
        side = (boxes[:, 1] - boxes[:, 0]).max(axis=1)
        distance = np.linalg.norm(centre - np.asarray(eye, np.float64), axis=1)
        scaled = np.maximum(distance - side * self.renderer["BoundingLengthScale"], 0.0) * self.distance_scale
        inside = ((boxes[:, 0] <= eye) & (eye <= boxes[:, 1])).all(axis=1)
        return (scaled <= self.renderer["PartialZPrepassRange"]) | inside


def _pair(value, name: str) -> tuple[int, int]:
    pair = tuple(int(item) for item in value)
    if len(pair) != 2:
        raise GBufferOrderError(f"{name} gives {len(pair)} segments, not one-sided and two-sided")
    return pair


def _segments(data: Mapping, kinds) -> dict[str, tuple[int, int]]:
    return {name: _pair(data[name], name) for name in (*kinds, *(_SEGMENTED[kind] for kind in kinds))}


def catalog_from_dict(data: Mapping) -> GBufferOrder:
    if data.get("format") != CATALOG_FORMAT:
        raise GBufferOrderError(f"G-buffer order catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    renderer = {name: kind(data["renderer"][name]) for name, kind in _RENDERER.items()}
    draw_types = {name: frozenset(int(item) for item in data["draw_types"][name])
                  for name in ("forward", "transparent", "decal")}
    segments = _segments(data["segments"], _SEGMENTED)
    prepass = data["prepass"]
    first = {name: _pair(value, name) for name, value in prepass["first"].items()}
    other_types = frozenset(str(item) for item in prepass["other_types"])
    if not set(first) | other_types <= set(DRAW_TYPES) or not other_types <= set(first):
        raise GBufferOrderError("the pre-pass draw types are not draw types the first segments name")
    flags = {str(role): str(name) for role, name in data["material_flags"].items()}
    if set(flags) != set(MATERIAL_ROLES):
        raise GBufferOrderError(f"material flags are not named for {', '.join(MATERIAL_ROLES)}")
    instance = data["instance_key"]
    key = tuple(KeyField(str(item["field"]), item.get("when"), item.get("unset"), item.get("renderer"),
                         item.get("mask")) for item in instance["fields"])
    if any((item.unset is None) != (item.renderer is None) or item.renderer not in (None, *_RENDERER)
           for item in key):
        raise GBufferOrderError("an instance key field's unset value names no renderer setting")
    instancer, batches = _instancer(data["instancer"]), _batches(data["batches"])
    if instancer.components & batches.components:
        raise GBufferOrderError("a component type is both drawn by instancing and placed by batch type")
    layer = frozenset(str(name) for name in data["transparent_layer"])
    if not layer <= set(DRAW_TYPES):
        raise GBufferOrderError("the transparent layer names a draw type the order does not have")
    ignore = data["ignore_depth"]
    shells = None if data["shell_fur"] is None else _shells(data["shell_fur"])
    return GBufferOrder(str(data["game"]), int(data["resource_version"]), renderer, float(data["distance_scale"]),
                        draw_types, frozenset(int(item) for item in data["skipped_shading_types"]["gbuffer"]),
                        frozenset(int(item) for item in data["skipped_shading_types"]["prepass"]), segments,
                        int(data["segments"]["stencil_priority"]), first, int(prepass["other"]), other_types,
                        flags, tuple(str(name) for name in instance["apart"]), key, instancer, batches,
                        str(ignore["field"]), tuple(int(segment) for segment in ignore["segments"]),
                        frozenset(int(item) for item in ignore["stamped"]), str(ignore["program"]), layer,
                        {int(target): tuple(float(value) for value in colour)
                         for target, colour in dict(data["cleared"]).items()}, shells)


def _shells(data: Mapping) -> ShellOrder:
    kinds = ("solid", "alpha_test")
    segments = _segments(data["segments"], kinds)
    prepass = {name: _pair(data["prepass"][name], name) for name in kinds}
    return ShellOrder(frozenset(str(item) for item in data["components"]),
                      frozenset(int(item) for item in data["shading_types"]),
                      tuple(str(name) for name in data["two_side"]), tuple(str(name) for name in data["alpha_test"]),
                      str(data["emissive"]), segments, prepass)


def _instancer(data: Mapping) -> InstancerOrder:
    draw_types = {str(name): frozenset(int(item) for item in types) for name, types in data["draw_types"].items()}
    segments = _segments(data["segments"], _SEGMENTED)
    prepass = {str(name): _pair(value, name) for name, value in data["prepass"].items()}
    if not set(draw_types) <= {"solid", "decal"} or not set(prepass) <= set(_SEGMENTED):
        raise GBufferOrderError("the instancer's draw types are not of solid and decal, or its pre-pass types not "
                                "of those it segments")
    return InstancerOrder(frozenset(str(item) for item in data["components"]), draw_types,
                          tuple(str(name) for name in data["two_side"]), str(data["emissive"]),
                          str(data["alpha_test"]), segments, prepass)


def _batches(data: Mapping) -> BatchOrder:
    names = {item.name for item in BatchType}
    segments = {str(name): int(value) for name, value in data["segments"].items()}
    prepass = {str(name): int(value) for name, value in data["prepass"].items()}
    when = {str(name): str(flag) for name, flag in data["prepass_when"].items()}
    if set(segments) != names or not set(prepass) <= names or not set(when) <= set(prepass):
        raise GBufferOrderError("the batch types' segments are not SpeedTree's batch types, or a pre-pass flag is of "
                                "a type without a pre-pass")
    return BatchOrder(frozenset(str(item) for item in data["components"]),
                      frozenset(int(item) for item in data["shading_types"]), segments, prepass, when)


gbuffer_order_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, GBufferOrderError, "G-buffer order",
                                   (AttributeError,))


@dataclass(slots=True)
class _Ordered:
    clears: list[int]
    meshlets: list[int]
    segments: dict[int, list[int]]
    prepasses: list[tuple[int, list[int], int | None, int | None]]
    listed: int = 0
    placed: tuple[bytes, list[int]] | None = None
    _table: tuple | None = None

    def frame(self, first: np.ndarray) -> list[int]:
        key = first.tobytes()
        if self.placed is not None and self.placed[0] == key:
            return self.placed[1]
        count, prepassed, *columns = self._table or (0, [], *np.zeros((3, 0), np.int64))
        if count != len(self.prepasses):
            added = [(step, owner, -1 if segment is None else segment, -1 if other is None else other)
                     for owner, steps, segment, other in self.prepasses[count:] for step in steps]
            prepassed = prepassed + [row[0] for row in added]
            columns = [np.concatenate([held, np.array([row[column] for row in added], np.int64)])
                       for column, held in enumerate(columns, 1)]
            self._table = len(self.prepasses), prepassed, *columns
        owners, firsts, others = columns
        chosen = np.where(first[owners], firsts, others)
        kept = np.flatnonzero(chosen >= 0)
        order = kept[np.argsort(chosen[kept], kind="stable")]
        placed, at = list(map(prepassed.__getitem__, order.tolist())), chosen[order]
        drawn = min(self.segments, default=None)
        steps = list(self.clears)
        for segment in sorted({*self.segments, *np.unique(at).tolist()}):
            # Meshlets draw over the pre-pass depth, before the first segment of draws.
            if segment == drawn:
                steps += self.meshlets
            # A segment holding both (stencil priority) draws its pre-passes first.
            steps += placed[np.searchsorted(at, segment):np.searchsorted(at, segment, "right")]
            steps += self.segments.get(segment, ())
        if drawn is None:
            steps += self.meshlets
        self.placed = key, steps
        return steps


class MeshSceneGBufferOrder:
    def _order_scene(self) -> None:
        self._order = gbuffer_order_for(self.profile)
        self._segment_steps: dict[int, list[Callable[[], list[int]]]] = defaultdict(list)
        self._transparent_steps: list[tuple[Callable[[list], list[int]], Callable[[], tuple[list, list]]]] = []
        meshes = set(self._rules.component[MESH_COMPONENT])
        if self._order is not None and not self._order.fields <= meshes:
            raise GBufferOrderError(f"the draw rules give no defaults of "
                                    f"{', '.join(sorted(self._order.fields - meshes))}")
        self._material_draws: dict[int, object] = {}
        self._keys: dict[tuple, int] = {}
        self._key_rows = np.zeros(0, np.int64)
        self._meshes = np.zeros(0, bool)
        self._instanced = np.zeros(0, bool)

    def _ordering_of(self, item: MeshObject) -> tuple[bool, bool]:
        if self._order is None:
            return False, False
        # The renderer's stencil-priority traits are its mesh's when a material of it sets the flag.
        return self._ignores_depth(item), any(self._material_draw(part.material).stencil_priority
                                              for part in item.parts)

    def _check_ignored(self, part: MeshPart) -> None:
        if isinstance(part.buffer, MeshletBuffer) or self._material_draw(
                part.material).shading_type not in self._order.ignore_depth_stamped:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The game draws a part of this shading type of a mesh ignoring depth "
                                   "in G-buffer segments {segments}, which REasy does not build."),
                {"segments": ", ".join(map(str, self._order.ignore_depth_segments))})

    def _order_placing(self, item: MeshObject, part: MeshPart, route: str, purpose: str, prepass,
                priority: bool) -> tuple[Placing | None, object]:
        if self._order is None:
            return None, prepass
        component = item.decal_receive[0]
        if purpose != "forward" or component in self._order.drawers:
            return self._placing(part, route, prepass, priority, component)
        if component == MESH_COMPONENT:
            return self._forward_placing(part, route, prepass, priority)
        return None, prepass

    def _instance_keys(self, objects: Sequence[MeshObject]) -> None:
        order = self._order
        if order is None:
            return
        rows = []
        for owner, item in enumerate(objects, len(self.objects) - len(objects)):
            key = None
            if (item.decal_receive[0] == MESH_COMPONENT and item.draw_state is not None and item.deformation is None
                    and not any(isinstance(part.buffer, MeshletBuffer) or self._material_draw(part.material)
                                .shading_type in order.draw_types["forward"] for part in item.parts)):
                state = order.batch_state(self._rules, item.draw_state, self._decals[owner])
                key = None if state is None else (tuple(id(part) for part in item.parts), item.skinned, state)
            rows.append(-1 if key is None else self._keys.setdefault(key, len(self._keys)))
        self._key_rows = np.concatenate([self._key_rows, np.array(rows, np.int64)])
        self._meshes = np.concatenate([self._meshes, np.array([item.decal_receive[0] == MESH_COMPONENT
                                                               for item in objects], bool)])
        if not order.renderer["UseInstancingDraw"]:
            self._instanced = np.zeros(len(self._key_rows), bool)
            return
        counts = np.append(np.bincount(self._key_rows[self._key_rows >= 0], minlength=len(self._keys)), 0)
        self._instanced = (self._key_rows >= 0) & (counts[self._key_rows] >= order.renderer["UseInstancingCount"])

    def _ignores_depth(self, item: MeshObject) -> bool:
        return (self._order is not None and item.decal_receive[0] == MESH_COMPONENT and item.draw_state is not None
                and bool(self._rules.value_of(item.draw_state, self._order.ignore_depth)))

    def _stamp(self, part: MeshPart, route: str, main) -> object:
        name = self._order.ignore_depth_program
        found = None if main is None or main[0] != "forward" else depth_only_program(
            part.document, self.shader_target, self._program_flags(part.material), name, route)
        if found is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The game writes the depth of a mesh drawn ignoring depth with its master's {name} "
                                   "program, then draws it forward: the part has no such program or forward draw."),
                {"name": name})
        return part.document.sdf.targets[self.shader_target].programs[found]

    def _transparent(self, part: MeshPart) -> bool:
        return self._order.transparent(self._material_draw(part.material))

    def _material_draw(self, material: PreviewMaterial):
        found = self._material_draws.get(id(material))
        if found is None or found[0] is not material:
            found = self._material_draws[id(material)] = material, self._order.material_draw(self._rules, material)
        return found[1]

    def _placing(self, part: MeshPart, route: str, prepass, priority: bool, component: str) -> tuple[Placing, object]:
        order = self._order
        if component in order.instancer.components:
            placing = order.instancer.placing(self._rules, part.material)
        elif component in order.batches.components:
            if part.batch is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The renderer of the component draws the clusters of SpeedTree meshes only."))
            placing = order.batches.placing(self._rules, part.material, part.batch)
        else:
            placing = order.placing(self._material_draw(part.material), priority)
        if placing is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The renderer of the component draws no part of the material's shading type."))
        if component in order.drawers:
            prepass = None
        if (component == MESH_COMPONENT or placing.first is not None and component in order.drawers) \
                and prepass is None:
            found = zprepass_program(part.document, self.shader_target, self._program_flags(part.material), route)
            prepass = None if found is None else part.document.sdf.targets[self.shader_target].programs[found]
        if prepass is None:
            return replace(placing, first=None, other=None), None
        return (placing if component == MESH_COMPONENT else replace(placing, other=placing.first)), prepass

    def _forward_placing(self, part: MeshPart, route: str, prepass, priority: bool) -> tuple[Placing | None, object]:
        material = self._material_draw(part.material)
        placing = self._order.placing(material, priority)
        found = None
        if self._order.draw_type(material) == "forward" and (placing.first is not None or placing.other is not None):
            found = zprepass_program(part.document, self.shader_target, self._program_flags(part.material), route)
        if found is None:
            return None, prepass
        return placing, part.document.sdf.targets[self.shader_target].programs[found]

    def _prepass_first(self) -> np.ndarray:
        order = self._order
        self._box_moved()
        first = np.ones(len(self.objects) + 1, bool)
        for instanced in (False, True):
            owners = np.flatnonzero(self._meshes & (self._instanced == instanced))
            chosen = order.drawn_first(instanced)
            if chosen is not None:
                first[owners] = chosen
                continue
            view = np.asarray(self.camera.view, np.float64).reshape(3, 4)
            near = order.near(self._boxes[owners], -view[:, :3].T @ view[:, 3])
            # The renderer's meshes all have a box; one REasy cannot bound is taken as far.
            first[owners] = near & self._bounded[owners]
        return first

    def _listed_ordered(self, apart: bool) -> _Ordered:
        listing = self._ordered.get(apart)
        unblended = _bound_targets(self.catalog, False)
        if listing is None:
            scene = self.device.scene
            listing = _Ordered([scene.clear(row.format, self.targets[row.target]) for row in unblended
                                if row.target in self._order.cleared]
                               + [scene.clear(self.depth_format, self.depth)], self._meshlet_steps(),
                               {segment: [step for make in made for step in make()]
                                for segment, made in self._segment_steps.items()}, [])
        added = self._draws[listing.listed:]
        for item in added:
            placing = item.placing
            if placing is not None and item.prepass is not None and self._drawable((item.part,)):
                with self._refusing():
                    # The pre-pass only writes depth; the G-buffer pass draws its targets again.
                    listing.prepasses.append((item.owner, self._steps(item, item.prepass, partial(
                        self._job, item, item.prepass, len(unblended), self._engine_texture(PREPASS_PURPOSE)),
                        self.scratch(row.format for row in unblended)), placing.first, placing.other))
        onto = {}
        for item in added:
            if not (item.purpose == GBUFFER_PURPOSE or item.purpose == "blending"
                    and ("blending", ()) not in self.phases) or not self._drawable((item.part,)):
                continue
            if item.placing.segment is None:
                self._refuse(PartError([(item.part, MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The renderer draws the material outside the G-buffer (its shading type or "
                                       "flags send it to another layer), which Game lighting does not draw with its "
                                       "G-buffer program.")))]))
                continue
            blending = item.purpose == "blending"
            if blending not in onto:
                onto[blending] = self._gbuffer_onto(blending, apart)
            with self._refusing():
                listing.segments.setdefault(item.placing.segment, []).extend(self._steps(
                    item, item.program, partial(self._job, item, item.program, len(onto[blending]),
                                                self._engine_texture(GBUFFER_PURPOSE)), onto[blending]))
        listing.listed, listing.placed = len(self._draws), None
        self._ordered[apart] = listing
        return listing

    def _ordering(self) -> None:
        if self._order is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "REasy has no G-buffer order catalog of this game build to place the draw by."))

    def add_gbuffer(self, segment: int, steps: Callable[[], list[int]]) -> None:
        self._ordering()
        self._segment_steps[segment].append(steps)
        self._ordered = {}

    def add_transparent(self, steps: Callable[[list], list[int]], ordered: Callable[[], tuple[list, list]]) -> None:
        self._ordering()
        self._transparent_steps.append((steps, ordered))
        self._phases = {}

    def _transparent_ordered(self, steps: list[int]) -> list[int]:
        for _steps, ordered in self._transparent_steps:
            listed, now = ordered()
            if listed == now:
                continue
            try:
                start = steps.index(listed[0])
            except ValueError:
                continue
            if steps[start:start + len(listed)] == listed:
                steps[start:start + len(listed)] = now
        return steps


__all__ = ["BatchOrder", "CATALOG_DIRECTORY", "CATALOG_FORMAT", "DRAW_TYPES", "GBufferOrder", "GBufferOrderError",
           "InstancerOrder", "KeyField", "MATERIAL_ROLES", "MaterialDraw", "Placing", "ShellOrder",
           "catalog_file_name", "catalog_from_dict", "gbuffer_order_for"]
