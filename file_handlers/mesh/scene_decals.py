"""See docs/internals/volume-decals.md."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from functools import partial

lazy import numpy as np
from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

lazy from file_handlers.mmtr.engine_buffers import (
    CHECKER_BOARD_BUFFER, camera_key, checker_board_values, laid_out, with_words,
)
lazy from file_handlers.mmtr.light_passes import LightPasses, PassRead, Source
lazy from file_handlers.mmtr.material_preview import (
    RANGE_CBV, RANGE_SAMPLER, RANGE_SRV, SHADER_VISIBILITY, VIEW_DIMENSIONS, MaterialPreviewError)
lazy from file_handlers.mmtr.mesh_draw import input_elements, program_states
lazy from file_handlers.mmtr.pretransform import SystemProgram
lazy from file_handlers.mmtr.pretransform_passes import SystemPrograms
lazy from file_handlers.mmtr.resident import RENDER_TARGET
lazy from file_handlers.mmtr.user_render_targets import (
    is_render_texture, read_render_texture, user_render_target_catalog_for)
lazy from file_handlers.mmtr.volume_decals import (
    VolumeDecalCatalog, decal_bounds, decal_box, decal_order, decal_words, map_set, program_name, valid_flags,
    volume_decal_catalog_for)
lazy from file_handlers.tex.dxgi import DXGI_FORMAT_R16_UINT
lazy from file_handlers.tex.qt_image_utils import parse_tex_bytes
lazy from utils.resource_file_utils import normalize_resource_path, resource_path_with_version


@dataclass(frozen=True, slots=True)
class SceneDecalSource:
    """``world`` is 4 x 4 of row vectors."""
    key: str
    world: np.ndarray
    fields: Mapping[str, object]
    visible: bool
    resolve: Callable[[str], tuple[str, bytes] | None]


@dataclass(frozen=True, slots=True)
class SceneLitDecal:
    key: str
    world: np.ndarray
    program: str
    words: Mapping[str, tuple[int, ...]]
    textures: Mapping[str, str]
    box: tuple[np.ndarray, np.ndarray]
    pyramid: bool
    render_textures: frozenset[str] = frozenset()
    fields: Mapping[str, object] = frozendict()
    alpha_format: int | None = None
    timed: bool = False


translate = QCoreApplication.translate


def gather_decals(sources: Sequence[SceneDecalSource], profile, files: dict[str, bytes],
                  resolve: Callable[[str], tuple[str, bytes] | None],
                  timed: Collection[str] = ()) -> tuple[tuple[SceneLitDecal, ...], str]:
    catalog = volume_decal_catalog_for(profile)
    if catalog is None or not sources:
        return (), ""
    for path in catalog.files():
        found = resolve(resource_path_with_version(path, "sdf", catalog.resource_version)
                        if path.endswith(".sdf") else path)
        if found is None:
            return (), translate("SceneLitPreview", "The game's file {path} was not found.").format(path=path)
        files[path] = found[1]
    refused: Counter[str] = Counter()
    drawn: list[tuple[int, SceneLitDecal]] = []
    for made, source in enumerate(sources):
        if not source.visible:
            continue
        fields = source.fields
        reason = _refusal(catalog, fields)
        if reason is None:
            textures, alpha_format = {}, None
            for name, field_name in catalog.bindings.items():
                path = str(fields.get(field_name) or "").strip()
                if not path:
                    textures[name] = catalog.fallback
                    continue
                key = normalize_resource_path(path)
                if key not in files:
                    found = source.resolve(path)
                    if found is None:
                        reason = translate("SceneLitPreview", "a map's texture was not found")
                        break
                    files[key] = found[1]
                textures[name] = key
                if field_name == catalog.second_alpha_map and is_render_texture(key):
                    alpha_format = _render_texture_format(files[key], profile)
                elif field_name == catalog.second_alpha_map:
                    tex = parse_tex_bytes(files[key])
                    alpha_format = None if tex is None else int(tex.header.format)
        if reason is not None:
            refused[reason] += 1
            continue
        flags = valid_flags(catalog, fields, alpha_format)
        # The renderer draws a decal of no maps' bits no more.
        if not flags & ((1 << catalog.blend_shift) - 1):
            continue
        world = np.asarray(source.world, np.float32).reshape(4, 4)
        drawn.append((decal_order(catalog, fields, made), SceneLitDecal(
            source.key, world, program_name(catalog, fields, flags), decal_words(catalog, fields, world, alpha_format),
            textures, decal_box(catalog, fields), int(catalog.field(fields, "shape") or 0) in catalog.pyramid_shapes,
            frozenset(path for path in textures.values() if is_render_texture(path)), fields, alpha_format,
            source.key in timed)))
    drawn.sort(key=lambda item: item[0])
    return tuple(decal for _order, decal in drawn), "; ".join(
        translate("SceneLitPreview", "{count} with {reason}").format(count=count, reason=reason)
        for reason, count in refused.items())


def _render_texture_format(data: bytes, profile) -> int | None:
    catalog = user_render_target_catalog_for(profile)
    try:
        return None if catalog is None else read_render_texture(data, catalog).form
    except MaterialPreviewError:
        return None


def _refusal(catalog: VolumeDecalCatalog, fields: Mapping[str, object]) -> str | None:
    shape = int(catalog.field(fields, "shape") or 0)
    if shape not in catalog.drawn_shapes:
        return translate("SceneLitPreview", "a projection shape the game does not define ({shape})").format(shape=shape)
    if map_set(fields, catalog.fields["custom"]):
        return translate("SceneLitPreview", "a custom material")
    if map_set(fields, catalog.fields["occlusion"]):
        return translate("SceneLitPreview", "a baked occlusion map")
    return None


class VolumeDecalDraws:
    def __init__(self, lit: LightPasses, decals: Sequence[SceneLitDecal], programs: SystemPrograms, values):
        catalog = volume_decal_catalog_for(programs.profile)
        self.lit, self.decals, self.programs, self.catalog = lit, tuple(decals), programs, catalog
        self.values = values
        self._boxes = tuple(np.array([decal.box[side] for decal in decals], np.float32).reshape(-1, 3)
                            for side in (0, 1))
        self._worlds = np.array([decal.world for decal in decals], np.float32).reshape(-1, 4, 4)
        number = lit.purpose_phase(catalog.phase, QT_TRANSLATE_NOOP(
            "MaterialPreview", "The light pass catalog has no {phase} phase to draw the volume decals after."))
        self.number, self.at = number, (lit.catalog.phases[number].after, number)
        self._steps: list[int] | None = None
        self._draws: list[int] = []
        self._camera = self._size = self._copied = None
        lit.add(self.at, self._frame)

    def _frame(self) -> list[int]:
        if self._size != tuple(self.lit.scene.size):
            if self._copied is not None:
                self.lit.device.release([self._copied])
            self._size, self._draws, self._camera = tuple(self.lit.scene.size), [], None
            self._steps = self._made()
        camera = self.lit.scene.camera
        key = camera_key(camera)
        if key != self._camera:
            self._camera = key
            low, high = decal_bounds(self.catalog, self._boxes, self._worlds, camera.view_matrix().T,
                                     camera.projection(), camera.near)
            for step, least, most in zip(self._draws, low.tolist(), high.tolist(), strict=True):
                self.lit.device.scene.bound(step, least, most)
        return self._steps

    def _made(self) -> list[int]:
        lit, catalog, device = self.lit, self.catalog, self.lit.device
        targets = lit.phase_targets(self.number)
        form, normals = targets[catalog.normals_target]
        copied = self._copied = device.texture(form, lit.scene.size, RENDER_TARGET)
        steps = [device.copy(copied, normals)]
        box = device.buffer(np.asarray(catalog.box_vertices, np.float32).tobytes(), 1)
        pyramid = (device.buffer(np.asarray(catalog.pyramid_vertices, np.float32).tobytes(), 1)
                   if any(decal.pyramid for decal in self.decals) else None)
        indices = device.buffer(np.asarray(catalog.box_indices, np.uint16).tobytes(), 1)
        width, height = lit.scene.size
        for decal in self.decals:
            program, contracts = self.programs.graphics(catalog.target, SystemProgram(catalog.file, decal.program))
            before: list[int] = []
            tables = self._tables(decal, contracts, (form, copied), before)
            job = {"vertex_shader": bytes(program.shader("vertex").payload),
                   "pixel_shader": bytes(program.shader("pixel").payload),
                   "vertex_buffers": [(pyramid if decal.pyramid else box, 12)], "elements": input_elements(program),
                   "indices": indices, "index_format": DXGI_FORMAT_R16_UINT,
                   "draw": (len(catalog.box_indices), 1, 0, 0, 0),
                   "size": (width, height), "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0),
                   "scissor": (0, 0, width, height), "targets": targets,
                   "depth": (lit.scene.depth_format, lit.scene.depth), "tables": tables, "heap": [],
                   **program_states(program, len(targets), 0, catalog.bounds.unbounded)}
            try:
                draw = device.scene.draw(job)
            except RuntimeError as exc:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "Direct3D could not run the volume decal program {name}: {reason}"),
                    {"name": decal.program, "reason": str(exc)}) from exc
            self._draws.append(draw)
            steps += [*before, draw]
        return steps

    def _tables(self, decal: SceneLitDecal, contracts: Mapping, normals: tuple[int, int], before: list[int]) -> list:
        lit, catalog, device = self.lit, self.catalog, self.lit.device
        tables = []
        for stage, contract in contracts.items():
            visibility = SHADER_VISIBILITY[stage]
            for resource in contract.resources:
                kind, place = resource.register_class, (resource.bind_point, resource.register_space,
                                                        resource.bind_count)
                if kind == "s":
                    state = lit.samplers.states[resource.name].descriptor()
                    tables.append((RANGE_SAMPLER, *place, 0, visibility,
                                   tuple((slot, *state) for slot in range(max(1, resource.bind_count)))))
                elif kind == "b":
                    layout = contract.buffer(resource.name)
                    if resource.name == CHECKER_BOARD_BUFFER:
                        held = device.buffer(bytes(laid_out(layout, checker_board_values(), True)), 1)
                    elif lit.gives(resource.name):
                        held = device.following((id(self), resource.name, layout.byte_size, layout.variables),
                                                lambda name=resource.name, layout=layout: lit.engine_block(name,
                                                                                                           layout))
                    else:
                        if any(variable.name not in decal.words and variable.name not in catalog.unwritten
                               for variable in layout.variables):
                            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                                "MaterialPreview", "The volume decal program {name} reads a buffer {buffer} REasy "
                                                   "does not fill."), {"name": decal.program, "buffer": resource.name})
                        held = device.following((id(self), decal.key, resource.name),
                                                partial(self._words, decal, layout), 1) if decal.timed \
                            else device.buffer(with_words(bytearray(layout.byte_size), layout, decal.words), 1)
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, held),)))
                elif kind == "t":
                    if resource.name == catalog.normals_read:
                        entry = (0, *normals)
                    elif resource.name == catalog.depth_read:
                        entry = lit.entry(PassRead(stage, resource.name, Source("depth"), 0), resource, contract,
                                          self.at, lit.scene.depth, before)
                    elif resource.name in decal.textures:
                        entry = lit.entry(PassRead(stage, resource.name, Source("file", decal.textures[resource.name]),
                                                   0), resource, contract, self.at, lit.scene.depth, before)
                    else:
                        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                            "MaterialPreview", "The volume decal program {name} reads a texture {texture} REasy does "
                                               "not give."), {"name": decal.program, "texture": resource.name})
                    tables.append((RANGE_SRV, *place, VIEW_DIMENSIONS[resource.dimension], visibility, (entry,)))
                else:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The volume decal program {name} writes {resource}."),
                        {"name": decal.program, "resource": resource.name})
        return tables

    def _words(self, decal: SceneLitDecal, layout) -> bytes:
        now = self.values.of(decal.key)
        words = decal_words(self.catalog, {**decal.fields, **now}, decal.world, decal.alpha_format) if now \
            else decal.words
        return bytes(with_words(bytearray(layout.byte_size), layout, words))


__all__ = ["SceneDecalSource", "SceneLitDecal", "VolumeDecalDraws", "gather_decals"]
