"""See docs/internals/pretransform.md."""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.build_catalog import catalog_file_name, catalog_reader

from file_handlers.sdf.models import program_names

from .material_preview import MaterialPreviewError, gbuffer_route

CATALOG_FORMAT = 2
CATALOG_DIRECTORY = "resources/data/pretransform"
# Part of the game's G-buffer program names for this route (``pass_choice``).
ROUTE = "DeferredPreTransform"
SOURCE, DESTINATION, ACCUMULATOR, WEIGHTS, VERTEX_WORDS, INDEX_WORDS = (
    "source", "destination", "accumulator", "weights", "vertex-words", "index-words")
CHAIN_BUFFERS = ("cbCSSkinning", "cbCSBlendShape", "cbCSBlendShapeNone", "cbCSCalculateNormal", "cbCSWriteNormal")
# No captured program loads these fields.
_UNREAD = frozenset({
    "reserved", "cbCSSkinningReserved", "cbCSSkinning_reserve0", "cbCSSkinning_reserve1", "cbCSSkinningPadding0",
    "cbCSSkinningPadding1", "cbCSSkinningPadding2", "blendshape_reserved0", "blendshape_reserved1",
    "blendshape_reserved2", "cbCSBlendShapeReserved0", "cbCSBlendShapeReserved1", "cbCSBlendShapeReserved2",
    "cCSCalculateNormal_Reserved", "cTargetUV", "cMaxChannelCount", "cBlendShapeFlags", "cChannelIndex"})
# Threads per group: one vertex each, or two triangles when summing face normals.
THREADS = 256
_TRIANGLE_INDICES = 6
# In the engine's ``VertexBufferType`` order.
_POSITION, _NORMAL, _UV0, _UV1, _WEIGHTS, _COLOUR, _UV2, _MORE_WEIGHTS = range(8)
# Bytes; ``_NORMAL_SIZE`` holds a normal and its tangent.
_POSITION_SIZE, _NORMAL_SIZE, _WHITE_SIZE = 12, 8, 4
# Where the oldest builds read a target's deltas when the file records no place; see docs/internals/pretransform.md.
VERTEX_SUM = "vertex_sum"
BLEND_PLACES = (VERTEX_SUM,)
# By compression type: delta bytes per vertex with normal-recalculation data (four more without), and the lanes' steps.
_BLEND_FORMS = {0: (4, (2047, 1023, 2047)), 1: (8, (2047, 1023, 2047)), 3: (3, (255, 255, 255))}


class PreTransformCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SystemProgram:
    file: str
    name: str


@dataclass(frozen=True, slots=True)
class PreTransformCatalog:
    game: str
    resource_version: int
    target: int
    source_holds_indices: bool
    # Whether the destination holds the whole buffer or the first LOD; None when no capture tells.
    destination_holds_buffer: bool | None
    blend: Mapping[int, SystemProgram]
    blend_still: SystemProgram | None
    calculate: SystemProgram | None
    write: SystemProgram | None
    write_excluding_edge: SystemProgram | None
    write_by_submesh: bool
    # Names of master programs captures show filling the destination.
    skinning: frozenset[str]
    # Whether those run per blend-shape range or per submesh; None when no capture tells.
    skin_by_range: bool | None
    system_skins_blend: bool | None
    system_skinning: Mapping[str, SystemProgram]
    system_blend: Mapping[str, SystemProgram]
    source: Mapping = field(default_factory=dict)
    # None refuses a target the file gives no place.
    blend_place: str | None = None
    # Whether a boneless mesh runs the master's ``Static`` program matching the ``Skinning`` one.
    static_like_skinning: bool = False
    # Whether a blend-shape-less LOD of a blend-shaped mesh is copied whole and skinned from the copy; None when no
    # capture tells.
    still_without_blend: bool | None = None
    # How masters skin a copied LOD: one dispatch (True) or one per submesh (False); None when no capture tells.
    copied_skin_whole: bool | None = None

    def files(self) -> tuple[str, ...]:
        programs = (*self.blend.values(), self.blend_still, self.calculate, self.write, self.write_excluding_edge,
                    *self.system_skinning.values(), *self.system_blend.values())
        return tuple(dict.fromkeys(program.file for program in programs if program is not None))


def _program_to_dict(program: SystemProgram | None) -> dict | None:
    return None if program is None else {"file": program.file, "program": program.name}


def _program_from_dict(data: Mapping | None) -> SystemProgram | None:
    return None if data is None else SystemProgram(str(data["file"]), str(data["program"]))


def catalog_to_dict(catalog: PreTransformCatalog) -> dict:
    return {"format": CATALOG_FORMAT, "game": catalog.game, "resource_version": catalog.resource_version,
            "target": catalog.target, "source_holds_indices": catalog.source_holds_indices,
            "destination_holds_buffer": catalog.destination_holds_buffer,
            "blend": {str(kind): _program_to_dict(program) for kind, program in sorted(catalog.blend.items())},
            "blend_still": _program_to_dict(catalog.blend_still), "calculate": _program_to_dict(catalog.calculate),
            "write": _program_to_dict(catalog.write),
            "write_excluding_edge": _program_to_dict(catalog.write_excluding_edge),
            "write_by_submesh": catalog.write_by_submesh, "skinning": sorted(catalog.skinning),
            "skin_by_range": catalog.skin_by_range, "system_skins_blend": catalog.system_skins_blend,
            "system_skinning": {key: _program_to_dict(program)
                                for key, program in sorted(catalog.system_skinning.items())},
            "system_blend": {key: _program_to_dict(program) for key, program in sorted(catalog.system_blend.items())},
            "blend_place": catalog.blend_place, "static_like_skinning": catalog.static_like_skinning,
            "still_without_blend": catalog.still_without_blend, "copied_skin_whole": catalog.copied_skin_whole,
            "source": dict(catalog.source)}


def _flag(value) -> bool | None:
    return None if value is None else bool(value)


def catalog_from_dict(data: Mapping) -> PreTransformCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise PreTransformCatalogError(f"pre-transform catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    blend = {int(kind): _program_from_dict(program) for kind, program in data["blend"].items()}
    system_blend = {str(key): _program_from_dict(program) for key, program in data["system_blend"].items()}
    kinds = {*blend, *(int(key.split(" ", 1)[0]) for key in system_blend)}
    if not kinds <= _BLEND_FORMS.keys():
        raise PreTransformCatalogError(f"no capture shows the blend-shape compression types {sorted(kinds)}")
    if data["blend_place"] not in (None, *BLEND_PLACES):
        raise PreTransformCatalogError(f"unknown blend-shape delta place {data['blend_place']!r}")
    return PreTransformCatalog(
        str(data["game"]), int(data["resource_version"]), int(data["target"]), bool(data["source_holds_indices"]),
        _flag(data["destination_holds_buffer"]), blend, _program_from_dict(data["blend_still"]),
        _program_from_dict(data["calculate"]), _program_from_dict(data["write"]),
        _program_from_dict(data["write_excluding_edge"]), bool(data["write_by_submesh"]),
        frozenset(map(str, data["skinning"])), _flag(data["skin_by_range"]), _flag(data["system_skins_blend"]),
        {str(key): _program_from_dict(program) for key, program in data["system_skinning"].items()}, system_blend,
        dict(data.get("source", {})), data["blend_place"], bool(data["static_like_skinning"]),
        _flag(data["still_without_blend"]), _flag(data["copied_skin_whole"]))


pretransform_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, PreTransformCatalogError,
                                          "pre-transform", (AttributeError,))


@dataclass(frozen=True, slots=True)
class ChainSubmesh:
    first: int
    count: int
    first_index: int
    index_count: int
    material: int
    group: int


@dataclass(frozen=True, slots=True)
class BlendRange:
    first: int
    count: int
    location: int
    group: int
    material: int


@dataclass(frozen=True, slots=True)
class BlendTarget:
    channel_location: int
    channel_count: int
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]
    ranges: tuple[BlendRange, ...]
    placed: bool = True


@dataclass(frozen=True, slots=True)
class BlendShapes:
    compression: int
    stride: int
    offset: int
    targets: tuple[BlendTarget, ...]
    still: tuple[BlendRange, ...]
    ranged: bool

    @property
    def channel_count(self) -> int:
        return max((target.channel_location + target.channel_count for target in self.targets), default=0)


@dataclass(frozen=True, slots=True)
class NormalRecalculation:
    vertex_words: bytes
    index_words: bytes
    index_offset: int
    vertex_offset: int
    of_level: bool


@dataclass(frozen=True, slots=True)
class ChainLevel:
    source: bytes
    streams: Mapping[int, tuple[int, int]]
    vertex_count: int
    base: int
    base_index: int
    submeshes: tuple[ChainSubmesh, ...]
    blend: BlendShapes | None
    normals: NormalRecalculation | None
    mesh_recalculates: bool | None = None
    copied: bool = False

    @property
    def recalculates(self) -> bool:
        return self.normals is not None if self.mesh_recalculates is None else self.mesh_recalculates

    @property
    def end(self) -> int:
        return self.submeshes[-1].first + self.submeshes[-1].count

    @property
    def skinned(self) -> bool:
        return _WEIGHTS in self.streams

    @property
    def position_offset(self) -> int:
        return self.streams[_POSITION][0]

    @property
    def uv_offset(self) -> int:
        return self.streams[_UV0][0]

    @property
    def weights_offset(self) -> int:
        return self.streams[_WEIGHTS][0]


@dataclass(frozen=True, slots=True)
class Destination:
    """The two position blocks swap each frame."""
    vertex_count: int
    swapped: bool = False

    @property
    def size(self) -> int:
        return self.vertex_count * (2 * _POSITION_SIZE + _NORMAL_SIZE)

    @property
    def positions(self) -> int:
        return self.vertex_count * _POSITION_SIZE if self.swapped else 0

    @property
    def previous(self) -> int:
        return 0 if self.swapped else self.vertex_count * _POSITION_SIZE

    @property
    def normals(self) -> int:
        return self.vertex_count * 2 * _POSITION_SIZE

    @property
    def accumulator_size(self) -> int:
        return self.vertex_count * 12


@dataclass(frozen=True, slots=True, eq=False)
class Deformation:
    level: ChainLevel
    catalog: PreTransformCatalog
    enabled: frozenset[int]
    edge_excluded: bool = False
    weights: np.ndarray | None = None
    unskinned: frozenset[int] = frozenset()

    @property
    def destination(self) -> Destination:
        return Destination(self.level.vertex_count)

    @property
    def program(self) -> str:
        return masters_skinning_program(self.level, self.edge_excluded)

    def steps(self) -> list[ChainStep]:
        return chain_steps(self.level, self.catalog, self.destination, self.enabled, self.edge_excluded,
                           self.unskinned)


def destination_view(destination: Destination, semantic: str, index: int) -> tuple[int, int] | None:
    if semantic == "POSITION" and index in (0, 1):
        # A still object's previous position is its position.
        return _POSITION_SIZE, destination.positions
    if semantic in ("NORMAL", "TANGENT") and index == 0:
        return _NORMAL_SIZE, destination.normals
    return None


@dataclass(frozen=True, slots=True)
class ChainStep:
    program: SystemProgram | str
    material: int | None
    constants: Mapping[str, np.ndarray]
    buffers: Mapping[str, str]
    groups: int
    target: BlendTarget | None = None


def refusal(text: str, **values) -> MaterialPreviewError:
    return MaterialPreviewError(text, values)


def chain_level(mesh, level: int, catalog: PreTransformCatalog, deformed_only: bool = True) -> ChainLevel | None:
    from file_handlers.mesh.normal_recalc import legacy_normal_recalc_words

    lods = mesh.meshes[0].lods
    submeshes = sorted((item for group in lods[level].mesh_groups for item in group.submeshes),
                       key=lambda item: item.verts_index_offset)
    if not submeshes:
        return None
    groups = {id(item): int(group.group_id) for group in lods[level].mesh_groups for item in group.submeshes}
    body = None if mesh.blend_shape_data is None else next(
        (item for item in mesh.blend_shape_data.bodies if item.lod_index == level), None)
    if body is not None and level and catalog.system_skins_blend:
        # The oldest builds take the first LOD's groups and blend entries whatever LOD is drawn.
        raise refusal(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The renderer of this game build applies blend shapes to the first LOD alone."))
    data = mesh.normal_recalc_data
    own = None if data is None else next((item for item in data.lods if item.lod_index == level), None)
    # The oldest files hold one block of words for the whole mesh, used for the first LOD.
    legacy = data is not None and not data.lods and bool(mesh.header.normal_recalc_offset) and level == 0
    if body is None and own is None and not legacy and deformed_only:
        return None
    copied = False
    if body is None and mesh.blend_shape_data is not None and mesh.blend_shape_data.bodies \
            and catalog.system_skins_blend is False:
        if catalog.still_without_blend is None:
            raise refusal(QT_TRANSLATE_NOOP(
                "MaterialPreview", "No capture shows how the renderer deforms a LOD without blend shapes when "
                                   "another LOD of the mesh has them."))
        copied = catalog.still_without_blend
    buffers = {item.buffer_index for item in submeshes}
    if len(buffers) != 1:
        raise refusal(QT_TRANSLATE_NOOP("MaterialPreview", "The LOD's submeshes lie in several mesh buffers."))
    buffer, = buffers
    payload = mesh.mesh_buffer.buffer_payloads[buffer]
    header = mesh.mesh_buffer.streaming_buffer_headers[buffer] if buffer else mesh.mesh_buffer
    starts = sorted({item.verts_index_offset for lod in lods for group in lod.mesh_groups for item in group.submeshes
                     if item.buffer_index == buffer} | {int(payload.vertex_count)})
    counts = dict(zip(starts, np.diff(starts).tolist()))
    parts = tuple(ChainSubmesh(int(item.verts_index_offset), counts[item.verts_index_offset],
                               int(item.faces_index_offset), int(item.indices_count), int(item.material_index),
                               groups[id(item)]) for item in submeshes)
    end = parts[-1].first + parts[-1].count
    # A destination of the first LOD fits no other LOD; others use the whole buffer.
    first_alone = catalog.destination_holds_buffer is False and not level
    blend = None
    if body is not None:
        unseen = refusal(QT_TRANSLATE_NOOP(
            "MaterialPreview", "No capture shows the renderer applying blend shapes stored as this mesh's are."))
        if body.alias_of_lod is not None or body.compression_type not in _BLEND_FORMS:
            raise unseen

        def ranges(target) -> tuple[BlendRange, ...]:
            return tuple(BlendRange(int(item.base_vertex_location), int(item.vertex_count),
                                    int(item.blend_shape_location), int(item.part_id), int(item.surface_range_id))
                         for item in target.ranges)
        ranged = not any(item.derived_from_target for target in body.targets for item in target.ranges)
        # A target's channels may be split over records sharing ``same_target_id``, deltas following in order.
        targets, opened, vertices = [], None, 0
        for index, record in enumerate(body.targets):
            held = BlendTarget(int(record.channel_location), int(record.channel_count),
                               tuple(map(float, record.aabb.minimum)), tuple(map(float, record.aabb.maximum)),
                               ranges(record), all(not item.derived_from_target or not item.blend_shape_location
                                                   for item in record.ranges))
            if catalog.blend_place == VERTEX_SUM and all(item.derived_from_target for item in record.ranges):
                # The game's place: the sum of earlier targets' vertex counts.
                held = replace(held, ranges=tuple(replace(item, location=vertices) for item in held.ranges),
                               placed=True)
            vertices += sum(int(item.vertex_count) for item in record.ranges)
            if record.same_target_id == index:
                targets.append(held)
                opened = index
                continue
            if record.same_target_id != opened:
                raise unseen
            first = targets[-1]
            moved = sum(item.count for item in first.ranges) * first.channel_count
            if (held.channel_location, held.minimum, held.maximum, held.ranges) != (
                    first.channel_location + first.channel_count, first.minimum, first.maximum,
                    tuple(replace(item, location=item.location + moved) for item in first.ranges)):
                raise unseen
            targets[-1] = replace(first, channel_count=first.channel_count + held.channel_count)
        blend = BlendShapes(
            int(body.compression_type),
            _BLEND_FORMS[body.compression_type][0] + (0 if mesh.header.normal_recalc_offset else 4),
            int(header.blend_shape_offset), tuple(targets),
            tuple(item for target in body.none_targets for item in ranges(target)), ranged)
    normals = None
    if own is not None:
        normals = NormalRecalculation(
            np.asarray(own.vertex_entries, "<u4").tobytes(), np.asarray(own.face_entries, "<u4").tobytes(),
            int(header.normal_recalc_local_index_buffer_offset), int(header.normal_recalc_redirected_list_offset),
            True)
    elif legacy:
        vertex_words, index_words = legacy_normal_recalc_words(mesh)
        indices = max(item.first_index + item.index_count for item in parts)
        if len(vertex_words) != (end if first_alone else payload.vertex_count) or len(index_words) < indices:
            raise refusal(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The mesh's normal-recalculation data does not cover its first LOD."))
        normals = NormalRecalculation(np.asarray(vertex_words, "<u4").tobytes(),
                                      np.asarray(index_words[:indices], "<u4").tobytes(), 0, 0, False)
    source = bytes(payload.vertex_bytes) + (bytes(payload.face_bytes) if catalog.source_holds_indices else b"")
    streams = {int(item.type): (int(item.offset), int(item.size)) for item in payload.buffer_headers}
    return ChainLevel(source, streams, end if first_alone else int(payload.vertex_count),
                      parts[0].first, min(item.first_index for item in parts), parts, blend, normals,
                      normals is not None or bool(mesh.header.normal_recalc_offset), copied)


def _stream(level: ChainLevel, kind: int, first: int, missing: int | None = None) -> int | None:
    if kind in level.streams:
        offset, stride = level.streams[kind]
        return offset + first * stride
    return None if missing is None else _stream(level, missing, first)


def _skinning(level: ChainLevel, destination: Destination, first: int, count: int, optional: bool) -> dict:
    position, normal = destination.positions + first * _POSITION_SIZE, destination.normals + first * _NORMAL_SIZE
    weights, more_weights = (_stream(level, _WEIGHTS, first), _stream(level, _MORE_WEIGHTS, first, _WEIGHTS)) \
        if level.skinned else (0, 0)
    # Without a colour stream the program reads a default white buffer.
    colour = _stream(level, _COLOUR, first) if _COLOUR in level.streams else first * _WHITE_SIZE
    return {"cVertexCount": count, "cUVCount": sum(kind in level.streams for kind in (_UV0, _UV1, _UV2)),
            "cVertexPositionByteOffset": position if optional else _stream(level, _POSITION, first),
            "cVertexNormalByteOffset": normal if optional else _stream(level, _NORMAL, first),
            "cVertexTexcoordByteOffset": _stream(level, _UV0, first),
            "cVertexTexcoord2ByteOffset": _stream(level, _UV1, first, _UV0),
            "cVertexTexcoord3ByteOffset": _stream(level, _UV2, first, _UV0),
            "cVertexSkinWeightByteOffset": weights, "cVertexSkinWeight2ByteOffset": more_weights,
            "cVertexColorByteOffset": colour,
            "cDstVertexPositionByteOffset": position,
            "cDstVertexPrevPositionByteOffset": destination.previous + first * _POSITION_SIZE,
            "cDstVertexNormalByteOffset": normal, "cDstVertexColorByteOffset": colour,
            "cDstVertexTexcoordByteOffset": _stream(level, _UV0, first),
            "cDstVertexTexcoord2ByteOffset": _stream(level, _UV1, first, _UV0),
            "cDstVertexTexcoord3ByteOffset": _stream(level, _UV2, first, _UV0),
            "cBuildPrev": 0}


def _still(level: ChainLevel, destination: Destination, first: int, count: int) -> dict:
    return {**_skinning(level, destination, first, count, False),
            "cBlendShapePositionByteOffset": _stream(level, _POSITION, first),
            "cBlendShapeNormalByteOffset": _stream(level, _NORMAL, first),
            "cDstBlendShapePositionByteOffset": destination.positions + first * _POSITION_SIZE,
            "cDstBlendShapeNormalByteOffset": destination.normals + first * _NORMAL_SIZE, "cRangeCount": count}


def _blend(level: ChainLevel, destination: Destination, target: BlendTarget, item: BlendRange) -> dict:
    blend = level.blend
    low, high = np.asarray(target.minimum, np.float32), np.asarray(target.maximum, np.float32)
    # Each factor is rounded to float32 before the product, as the game rounds them.
    scale = (high - low) * (1 / np.asarray(_BLEND_FORMS[blend.compression][1])).astype(np.float32)
    return {**_still(level, destination, item.first, item.count),
            "cChannelVertexCount": sum(other.count for other in target.ranges),
            "cUsingChannelOffset": target.channel_location,
            "cBlendShapeOffset": blend.offset + item.location * blend.stride, "cBlendShapeStride": blend.stride,
            "cBlendShapeSubtractOffset": low, "cBlendShapeSubtractRange": scale}


def _calculate(level: ChainLevel, destination: Destination, submesh: ChainSubmesh) -> dict:
    normals, first = level.normals, submesh.first
    if not normals.of_level:
        return {"cTotalVertexCount": destination.vertex_count, "cIndexCount": submesh.index_count,
                "cBaseVertexLocaioin": first, "cVertexCurrentPositionByteOffset": destination.positions,
                "cSrcIBBaseAddr": submesh.first_index * 4, "cDstIBBaseAddr": submesh.first_index * 2,
                "cDstVertexNormalByteOffsetCluster0": destination.normals}
    place = submesh.first_index - level.base_index
    return {"cTotalVertexCount": len(normals.vertex_words) // 4, "cTotalIndexCount": len(normals.index_words) // 4,
            "cIndexCount": submesh.index_count, "cBaseVertexLocaioin": first,
            "cVertexCurrentPositionByteOffset": destination.positions + level.base * _POSITION_SIZE,
            "cSrcIBBaseAddr": place * 4, "cDstIBBaseAddr": place * 2,
            "cDstVertexNormalByteOffsetCluster0": destination.normals + first * _NORMAL_SIZE,
            "cNormalLodOffset": level.base * 12, "cLocalIndexBufferOffset": normals.index_offset + level.base_index * 4,
            "cRedirectedListOffset": normals.vertex_offset + level.base * 4}


def _write(level: ChainLevel, destination: Destination, submesh: ChainSubmesh | None) -> tuple[dict, int]:
    normals = level.normals
    # Normal writing shares the summing programs' buffer and loads none of its other fields.
    unread = {"cIndexCount": 0, "cBaseVertexLocaioin": 0, "cSrcIBBaseAddr": 0, "cDstIBBaseAddr": 0}
    if submesh is not None:
        return {"cWriteNormalTotalVertexCount": submesh.count,
                "cWriteNormalDstVertexNormalByteOffset": destination.normals + submesh.first * _NORMAL_SIZE,
                "cWriteNormalLodOffset": level.base * 12,
                "cWriteNormalRedirectedListOffset": normals.vertex_offset + submesh.first * 4}, submesh.count
    if not normals.of_level:
        return {**unread, "cTotalVertexCount": destination.vertex_count,
                "cVertexCurrentPositionByteOffset": destination.positions,
                "cDstVertexNormalByteOffsetCluster0": destination.normals}, destination.vertex_count
    count = len(normals.vertex_words) // 4
    return {**unread, "cTotalVertexCount": count,
            "cVertexCurrentPositionByteOffset": destination.positions + level.base * _POSITION_SIZE,
            "cDstVertexNormalByteOffsetCluster0": destination.normals + level.base * _NORMAL_SIZE,
            "cNormalLodOffset": level.base * 12}, count


def _words(values: Mapping[str, object]) -> dict[str, np.ndarray]:
    return {name: value if isinstance(value, np.ndarray) else np.array([value], "<u4")
            for name, value in values.items() if value is not None}


def system_variant(streams: Mapping[int, tuple[int, int]], with_normals: bool) -> str:
    held = sum(streams[kind][1] for kind in (_WEIGHTS, _MORE_WEIGHTS) if kind in streams)
    return f"{'WithNorm ' if with_normals else ''}{f'{held // 2}w' if held else 'none'}"


def system_blend_normals(recalculates: bool, edge_excluded: bool) -> str:
    return "" if recalculates and not edge_excluded else "WithNormW" if recalculates else "WithNormR"


def system_blend_key(compression: int, weights: str, normals: str) -> str:
    return f"{compression} {normals + ' ' if normals else ''}{weights}"


def skinning_program_name(skinned: bool, blended: bool, with_normals: bool) -> str:
    return (f"PreTransform{'Skinning' if skinned else 'Static'}{'OptionalBuffer' if blended else ''}"
            f"{'WithNorm' if with_normals else ''}CS")


def masters_skinning_program(level: ChainLevel, edge_excluded: bool) -> str:
    return skinning_program_name(level.skinned, level.blend is not None or level.copied,
                                 level.normals is None or edge_excluded)


def deformed_route(document, shader_target: int, route: str, program: str | None, skinned: bool) -> str:
    """A master without ``program`` skins none of its spans."""
    if route != ROUTE or program is None or program in program_names(document.sdf.targets[shader_target]):
        return route
    return gbuffer_route(skinned)


def _group_runs(submeshes: Sequence[ChainSubmesh], first: int, end: int) -> list[tuple[int, int]]:
    starts = [first]
    group = None
    for item in submeshes:
        if item.first + item.count <= first or item.first >= end:
            continue
        if group is not None and item.group != group:
            starts.append(item.first)
        group = item.group
    return [(start, stop) for start, stop in zip(starts, [*starts[1:], end]) if stop > start]


def _groups(level: ChainLevel) -> list[tuple[int, int, int]]:
    starts = []
    for item in level.submeshes:
        if not starts or item.group != starts[-1][0]:
            starts.append((item.group, item.first))
    starts[0] = (starts[0][0], level.base)
    return [(group, first, end) for (group, first), end in zip(starts, [*(item[1] for item in starts[1:]), level.end])]


def chain_steps(level: ChainLevel, catalog: PreTransformCatalog, destination: Destination, enabled: Collection[int],
                edge_excluded: bool, unskinned: Collection[int] = ()) -> list[ChainStep]:
    blend, normals = level.blend, level.normals
    drawn = [item for item in level.submeshes if item.group in enabled]
    steps = []

    def groups(count: int, each: int = 1) -> int:
        return -(-count // (THREADS * each))

    def unseen(what: str) -> MaterialPreviewError:
        return refusal(QT_TRANSLATE_NOOP(
            "MaterialPreview", "No capture of this game build shows the program the renderer runs here ({what})."),
            what=what)
    spans = [(item.first, item.count, item.material) for item in drawn]
    with_normals = normals is None or edge_excluded
    if blend is not None and catalog.system_skins_blend is None:
        raise unseen("blend shapes")
    if blend is not None and catalog.system_skins_blend:
        # Oldest builds: every vertex in turn, group by group; a disabled group stops later groups' ranges being
        # reached, so those are skinned whole.
        variant = system_variant(level.streams, with_normals)
        blend_key = system_blend_key(blend.compression, system_variant(level.streams, False),
                                     system_blend_normals(level.recalculates, edge_excluded))

        def system(programs: Mapping[str, SystemProgram], key: str) -> SystemProgram:
            if key not in programs:
                raise unseen(f"blend shapes, the system skinning {variant}")
            return programs[key]

        def stretch(first: int, end: int) -> None:
            for start, stop in _group_runs(level.submeshes, first, end):
                steps.append(ChainStep(system(catalog.system_skinning, variant), None,
                                       _words(_skinning(level, destination, start, stop - start, False)),
                                       {"InputByteBuffer": SOURCE, "bDestPosVB": DESTINATION}, groups(stop - start)))
        ranges = sorted(((target, item) for target in blend.targets for item in target.ranges),
                        key=lambda pair: pair[1].first)
        place, held = level.base, False
        for group, first, end in _groups(level):
            inside = [(target, item) for target, item in ranges if first <= item.first < end]
            if group not in enabled:
                held = held or bool(inside)
                place = max(place, end)
                continue
            for target, item in [] if held else inside:
                if item.first < place:
                    raise unseen("overlapping blend-shape ranges")
                stretch(place, item.first)
                steps.append(ChainStep(system(catalog.system_blend, blend_key), None,
                                       _words(_blend(level, destination, target, item)),
                                       {"InputByteBuffer": SOURCE, "bWeightList": WEIGHTS,
                                        "bDestPosVB": DESTINATION}, groups(item.count), target))
                place = item.first + item.count
            if place > level.end:
                raise unseen("blend-shape ranges past the LOD")
            stretch(place, end)
            place = max(place, end)
        spans = []
    elif blend is not None:
        program = catalog.blend.get(blend.compression)
        if program is None or catalog.blend_still is None:
            raise unseen("blend shapes")
        still = [(item.first, item.count) for item in blend.still]
        if not blend.ranged:
            if len(blend.targets) != 1:
                raise unseen("several blend-shape targets")
            moved = blend.targets[0].ranges[0]
            edges = (level.base, moved.first, moved.first + moved.count, level.end)
            still = [(first, last - first) for first, last in (edges[:2], edges[2:]) if last > first]
        elif catalog.skin_by_range is not False:
            ranges = [item for target in blend.targets for item in target.ranges] + list(blend.still)
            by_range = [(item.first, item.count, item.material) for item in ranges if item.group in enabled]
            if catalog.skin_by_range is None and sorted(by_range) != sorted(spans):
                raise unseen("blend-shape ranges that are not the submeshes")
            spans = by_range
            covered = np.zeros(level.end, bool)
            for first, count, _material in spans:
                covered[first:first + count] = True
            if not all(covered[item.first:item.first + item.count].all() for item in drawn):
                raise unseen("blend-shape ranges leaving vertices of a drawn submesh out")
        for target in blend.targets:
            for item in target.ranges:
                steps.append(ChainStep(program, None, _words(_blend(level, destination, target, item)),
                                       {"InputByteBuffer": SOURCE, "bWeightList": WEIGHTS,
                                        "OutputByteBuffer": DESTINATION, "bDestPosVB": DESTINATION},
                                       groups(item.count), target))
        for first, count in still:
            steps.append(ChainStep(catalog.blend_still, None, _words(_still(level, destination, first, count)),
                                   {"InputByteBuffer": SOURCE, "OutputByteBuffer": DESTINATION,
                                    "bDestPosVB": DESTINATION}, groups(count)))
    elif level.copied:
        if catalog.blend_still is None:
            raise unseen("blend shapes")
        count = level.end - level.base
        steps.append(ChainStep(catalog.blend_still, None, _words(_still(level, destination, level.base, count)),
                               {"InputByteBuffer": SOURCE, "OutputByteBuffer": DESTINATION,
                                "bDestPosVB": DESTINATION}, groups(count)))
        if catalog.copied_skin_whole is None:
            raise unseen("the skinning of a LOD without blend shapes of a mesh holding them")
        if catalog.copied_skin_whole and drawn:
            spans = [(level.base, count, min(item.material for item in drawn))]
    blended = blend is not None or level.copied
    name = masters_skinning_program(level, edge_excluded)
    spans = [span for span in spans if span[2] not in unskinned]
    shown = name in catalog.skinning or (catalog.static_like_skinning and not level.skinned and skinning_program_name(
        True, blended, with_normals) in catalog.skinning)
    if spans and not shown:
        raise unseen(name)
    for first, count, material in spans:
        steps.append(ChainStep(name, material, _words(_skinning(level, destination, first, count, blended)),
                               {"InputByteBuffer": SOURCE, "bDestPosVB": DESTINATION}, groups(count)))
    if normals is None:
        return steps
    write = catalog.write_excluding_edge if edge_excluded else catalog.write
    if catalog.calculate is None or write is None:
        raise unseen("normal recalculation")
    for item in drawn:
        steps.append(ChainStep(catalog.calculate, None, _words(_calculate(level, destination, item)),
                               {"InputByteBuffer": DESTINATION, "bVertexBuffer": SOURCE, "bRedirectedIB": INDEX_WORDS,
                                "bDestNormal": ACCUMULATOR}, groups(item.index_count, _TRIANGLE_INDICES)))
    for item in drawn if catalog.write_by_submesh else (None,):
        constants, count = _write(level, destination, item)
        steps.append(ChainStep(write, None, _words(constants),
                               {"bSrcNormal": ACCUMULATOR, "bVertexBuffer": SOURCE, "bRedirectIndexList": VERTEX_WORDS,
                                "bDestNormal": DESTINATION}, groups(count)))
    return steps


def constant_buffer(layout, constants: Mapping[str, np.ndarray]) -> bytes:
    data = bytearray(layout.byte_size)
    for variable in layout.variables:
        words = constants.get(variable.name)
        if words is None and variable.name in _UNREAD:
            continue
        if words is None or words.nbytes > variable.byte_size:
            raise refusal(QT_TRANSLATE_NOOP(
                "MaterialPreview", "No capture shows the renderer setting the compute program's constant {name}."),
                name=variable.name)
        data[variable.byte_offset:variable.byte_offset + words.nbytes] = words.tobytes()
    return bytes(data)


def used_channels(target: BlendTarget, weights: Sequence[float]) -> np.ndarray:
    channels = np.arange(target.channel_location, target.channel_location + target.channel_count)
    held = np.asarray(weights, np.float32)[channels]
    return channels[held > 0][np.argsort(-held[held > 0], kind="stable")]


def blend_weights(blend: BlendShapes, weights: Sequence[float]) -> np.ndarray:
    held = np.asarray(weights, np.float32)
    if held.shape != (blend.channel_count,):
        raise ValueError(f"{held.shape} weights for {blend.channel_count} blend-shape channels")
    if any(len(used_channels(target, held)) for target in blend.targets if not target.placed):
        raise refusal(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The mesh file does not record where the deltas of a blend shape in use lie."))
    return held


def weight_list(blend: BlendShapes, weights: Sequence[float]) -> bytes:
    held = blend_weights(blend, weights)
    pairs = np.zeros(max(blend.channel_count, 1), np.dtype([("weight", "<f4"), ("channel", "<u4")]))
    pairs["channel"][:blend.channel_count] = np.arange(blend.channel_count)
    for target in blend.targets:
        used = used_channels(target, held)
        rest = np.setdiff1d(np.arange(target.channel_location, target.channel_location + target.channel_count), used)
        place = slice(target.channel_location, target.channel_location + target.channel_count)
        pairs["channel"][place] = np.concatenate([used, rest])
        pairs["weight"][place] = np.concatenate([held[used], np.zeros(len(rest), np.float32)])
    return pairs.tobytes()


__all__ = [
    "ACCUMULATOR", "BlendRange", "BlendShapes", "BlendTarget", "CATALOG_DIRECTORY", "CATALOG_FORMAT", "CHAIN_BUFFERS",
    "BLEND_PLACES", "ChainLevel", "ChainStep", "ChainSubmesh", "DESTINATION", "Deformation", "Destination", "INDEX_WORDS",
    "NormalRecalculation", "PreTransformCatalog", "PreTransformCatalogError", "ROUTE", "SOURCE", "SystemProgram",
    "THREADS", "VERTEX_WORDS", "WEIGHTS", "blend_weights", "catalog_file_name", "catalog_from_dict", "catalog_to_dict",
    "chain_level", "chain_steps", "constant_buffer", "deformed_route", "destination_view", "masters_skinning_program",
    "pretransform_catalog_for",
    "skinning_program_name", "system_blend_key", "system_blend_normals", "system_variant", "used_channels",
    "VERTEX_SUM", "weight_list",
]
