"""See docs/internals/gui-rendering.md, Device."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import numpy as np

from file_handlers.native_math import f32, fdiv, fmul, fsub
lazy from file_handlers.engine_math import crt
lazy from file_handlers.mmtr.engine_buffers import (
    FRAME_BUFFERS, Camera, frame_values, laid_out, scene_values, with_words,
)
lazy from file_handlers.mmtr.frame_clock import FrameClock, clock_words, frame_clock_catalog_for
lazy from file_handlers.mmtr.light_passes import light_pass_catalog_for, parsed_pass_file
lazy from file_handlers.mmtr.material_context import MaterialInstanceContext
lazy from file_handlers.mmtr.material_family import bind_drawn_master
lazy from file_handlers.mmtr.material_preview import (
    RANGE_CBV, RANGE_SAMPLER, RANGE_SRV, SHADER_VISIBILITY, VIEW_DIMENSIONS, Following, _engine_heap_fields,
    _texture_texels,
    shader_bindings,
)
lazy from file_handlers.mmtr.mesh_draw import input_elements, program_states
lazy from file_handlers.mmtr.mmtr_file import parse_drawn_master
lazy from file_handlers.mmtr.resident import RENDER_TARGET, TEXTURE_2D
lazy from file_handlers.mmtr.sampler_states import sampler_catalog_for
lazy from file_handlers.sdf.contracts import program_contracts
lazy from file_handlers.sdf.dxbc import PRIMITIVE_TOPOLOGY_NAMES
lazy from file_handlers.sdf.models import program_names
lazy from file_handlers.tex.dxgi import DXGI_FORMAT_R16_UINT, DXGI_FORMAT_R32_UINT
lazy from utils.resource_file_utils import resource_version_from_path

lazy from ..errors import GuiAssetError
lazy from ..model import ZERO_GUID
lazy from ..passes import gui_pass_catalog_for
lazy from .blur_chain import chain_values, level_sizes
lazy from .glyph_draws import GlyphAtlas, GlyphBlurs, effect_draws, filter_constants, text_draws
lazy from .icon_tags import resolve_dmc5_icon_tag
lazy from .masks import mask_passes, mask_producers
lazy from .materials import MaterialGap, slotted, unread
lazy from .text import dmc5_text_rules
lazy from .text_layout import LayoutFonts, TextContext, TextLayoutGap, layout_dmc5_text
lazy from .text_tags import resolve_dmc5_text_tag
lazy from .vertices import (
    CUT, MATERIAL_VERTEX, RULES, blur_quads, direct_texture_gap, element_vertices, gui_half, material_quads,
    pattern_source, strip_vertices, texture_source, world_matrix,
)


def _triangles(indices: np.ndarray) -> bytes:
    triangles, strip = [], []
    for index in (*indices.tolist(), CUT):
        if index != CUT:
            strip.append(index)
            continue
        triangles += [corner for at in range(len(strip) - 2)
                      for corner in ((strip[at], strip[at + 1], strip[at + 2]) if at % 2 == 0
                                     else (strip[at + 1], strip[at], strip[at + 2]))]
        strip = []
    return np.array(triangles, dtype=np.uint16).tobytes()


def _frozen(value):
    if isinstance(value, dict):
        return tuple((key, _frozen(item)) for key, item in value.items())
    return tuple(map(_frozen, value)) if isinstance(value, (list, tuple)) else value


class GuiDevice:

    def __init__(self, resident, profile, runtime, load_file: Callable[[str], bytes | None], dependencies,
                 icon_context):
        self.device, self.runtime, self.load_file = resident, runtime, load_file
        self.dependencies, self.assets, self.icon_context = dependencies, dependencies.assets, icon_context
        self.catalog = gui_pass_catalog_for(profile)
        light = light_pass_catalog_for(profile)
        if self.catalog is None or light is None:
            raise ValueError(f"REasy has no GUI pass or light pass catalog for {profile.game}")
        self.display = next(item for item in light.passes if item.role == "display")
        self.master_target, clock = light.master_target, frame_clock_catalog_for(profile)
        self.camera, self.clock = self.catalog.camera, None if clock is None else FrameClock(clock)
        self.samplers, self.sdf_profile, self.game = sampler_catalog_for(profile), profile.sdf_profile, profile.game
        self._documents, self._programs, self._targets, self._masters, self.engine = {}, {}, {}, {}, {}
        self._textures_read, self._cameras, self._steps, self._built, self._latest, self._slot = (
            {}, {}, {}, {}, {}, None)
        self._list = next(number for number, name in PRIMITIVE_TOPOLOGY_NAMES.items() if name == "triangle-list")
        atlas, targets = self.catalog.targets["glyph_atlas"], self.catalog.targets
        self.atlas = GlyphAtlas(tuple(atlas["size"]), tuple(atlas["largest"]), atlas["padding"])
        self._pages, self._text, self.language = [], None, runtime.language(runtime.preview_language)
        self._sizes, self._frame_size = {}, None
        self.blurs = GlyphBlurs(tuple(targets["glyph_blur"]["size"]), tuple(targets["glyph_scratch"]["size"]),
                                atlas["padding"])
        self._blur_pages, self._scratch_pages = [], []

    def program(self, path: str, target: int, name: str):
        if path not in self._documents:
            data = self.load_file(path)
            if data is None:
                raise FileNotFoundError(f"the game's shader file {path} was not found")
            self._documents[path] = parsed_pass_file(data, self.game, path).document
        key = (path, target, name)
        if key not in self._programs:
            found = [at for at, item in enumerate(program_names(self._documents[path].targets[target])) if item == name]
            program = self._documents[path].targets[target].programs[found[0]] if len(found) == 1 else None
            self._programs[key] = None if program is None else (program, program_contracts(self.sdf_profile, program))
        return self._programs[key]

    def _texture(self, node) -> str | None:
        kind = node.object.type_name
        properties = node.render_properties
        if kind == "via.gui.Texture":
            return texture_source(node, self.assets)[0]
        if kind == "via.gui.Circle" and str(properties["UVSequence"]):
            return pattern_source(self.assets, str(properties["UVSequence"]), int(properties["UVSequenceNo"]),
                                  int(properties["UVPatternNo"]))[0]
        if kind in ("via.gui.Scale9Grid", "via.gui.TextureSet"):
            cells = node.object.special_data.cells[:9] if kind == "via.gui.Scale9Grid" else (
                node.object.special_data.entries)
            names = {pattern_source(self.assets, str(properties["UVSequence"]), cell.sequence, cell.pattern)[0]
                     for cell in cells}
            if len(names) > 1:
                raise ValueError(f"{node.path}'s cells sample {len(names)} textures; the game draws them with one")
            return next(iter(names), None)
        return None

    def scene_camera(self, pixels: tuple[int, int]) -> Camera:
        if pixels not in self._cameras:
            lens = self.camera
            scale = fdiv(1.0, crt("tanf")(fmul(fmul(fmul(lens["fov"], lens["degree"]), lens["pi"]), lens["half"])))
            ratio = fdiv(lens["far"], fsub(lens["far"], lens["near"]))
            depth, far = fsub(ratio, 1.0), fmul(ratio, lens["near"]) / fsub(ratio, 1.0)
            self._cameras[pixels] = Camera(np.eye(3, 4), (scale, fmul(scale, fdiv(*map(float, pixels)))),
                                           depth * far / (1.0 + depth), far, pixels)
        return self._cameras[pixels]

    def _values(self, node, facts, pixels: tuple[int, int], size: tuple[float, float], fields: dict) -> dict:
        width, height = size
        system, world = self.catalog.system, world_matrix(node, facts).ravel()
        values = {**({} if self.camera is None else scene_values(self.scene_camera(pixels))),
                  "guiViewMatrix": np.eye(4).ravel(),
                  "guiProjMatrix": [fdiv(2.0, width), 0, 0, 0, 0, fdiv(-2.0, height), 0, 0, 0, 0, 0, 0, -1, 1, 0, 1],
                  "guiWorldMat": world, "worldMat": world, "guiIntensity": [1.0],
                  "guiSaturation": [fsub(1.0, node.saturation)], "guiStencilScale": [1.0],
                  "guiAlphaBlendRate": [system["LinearAlphaBlendRate"] if node.render_properties.get("LinearAlphaBlend")
                                        else 0.0], "screenSize": pixels,
                  "screenInverseSize": [fdiv(1.0, float(side)) for side in pixels], **self.engine, **fields}
        return {name: np.asarray(value, np.float64) for name, value in values.items()}

    def _read(self, path: str, dimension: str, smallest: int | None = None) -> tuple | None:
        key = (path, dimension, smallest)
        if key not in self._textures_read:
            data = self.load_file(path)
            self._textures_read[key] = _texture_texels(data, dimension, None, smallest) if data else None
        return self._textures_read[key]

    def _sampler(self, name: str, node) -> tuple:
        if name == self.catalog.element_sampler:
            name = (self.catalog.kind_samplers[node.object.type_name] if "SamplerType" not in node.render_properties
                    else self.runtime.enum_label(node.render_properties, "SamplerType"))
        return self.catalog.samplers[name].descriptor()

    def _tables(self, contracts, node, facts, pixels, size, textures: dict, fields: dict) -> list:
        tables, values = [], self._values(node, facts, pixels, size, fields)
        for stage, contract in contracts.items():
            visibility = SHADER_VISIBILITY[stage]
            for resource in contract.resources:
                place = (resource.bind_point, resource.register_space, resource.bind_count)
                if resource.register_class == "s":
                    state = self._sampler(resource.name, node)
                    tables.append((RANGE_SAMPLER, *place, 0, visibility,
                                   tuple((slot, *state) for slot in range(max(1, resource.bind_count)))))
                elif resource.register_class == "b":
                    data = bytes(laid_out(contract.buffer(resource.name), values, True))
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, data),)))
                elif resource.register_class == "t" and resource.name in textures:
                    texture = textures[resource.name]
                    entry = texture if isinstance(texture, tuple) else None if (
                        found := self._read(texture, resource.dimension)) is None else (0, *found)
                    if entry is None:
                        raise FileNotFoundError(f"{node.path} samples {texture}, which was not found")
                    tables.append((RANGE_SRV, *place, VIEW_DIMENSIONS[resource.dimension], visibility, (entry,)))
                else:
                    raise ValueError(f"the GUI program reads {resource.name}, which the device doesn't bind")
        return tables

    def _element_name(self, node, facts, family: str, role: str, masks) -> str:
        blend = self.runtime.enum_label(node.render_properties, "BlendType")
        return self.catalog.program_name(facts["program_names"], family=family, space="screen", blend=blend, mask=role,
                                         nest=str(len(masks) if role == "write" else 0))

    def _textures(self, masks, texture) -> dict:
        return {**{name: (0, *surface) for name, surface in zip(self.catalog.mask_textures, masks)},
                self.catalog.element_texture: texture}

    def _job(self, node, facts, name: str, target, pixels, size, textures: dict, vertices, indices,
             fields: dict | None = None) -> dict:
        found = self.program(*self.catalog.programs, name)
        if found is None:
            raise ValueError(f"{self.catalog.programs[0]} has no program {name}")
        program, contracts = found
        return self._draw(program, target, pixels, vertices, indices,
                          self._tables(contracts, node, facts, pixels, size, textures, fields or {}), [])

    def _kept(self, key: tuple, data: bytes) -> Following:
        key, latest = (*key, len(data)), self._latest
        latest[key] = data
        return Following(key, lambda: latest[key])

    def _draw(self, program, target, pixels, vertices, indices, tables: list, heap) -> dict:
        listed, data = _triangles(indices), vertices.tobytes()
        width, height = pixels
        if self._slot is not None:
            self._slot[-1] += 1
            tables = [(*table[:-1], tuple((index, self._kept((*self._slot, at, index), held) if isinstance(
                held, bytes) else held) for index, held in table[-1])) if table[0] == RANGE_CBV else table
                for at, table in enumerate(tables)]
            data = self.device.following(*self._kept((*self._slot, "vertices"), data), 1)
        return self.device.held({
            "vertex_shader": bytes(program.shader("vertex").payload),
            "pixel_shader": bytes(program.shader("pixel").payload),
            "vertex_buffers": [(data, vertices.dtype.itemsize)], "elements": input_elements(program),
            "indices": listed, "index_format": DXGI_FORMAT_R16_UINT, "draw": (len(listed) // 2, 1, 0, 0, 0),
            "size": (width, height), "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0),
            "scissor": (0, 0, width, height), "targets": [target], "depth": None, "tables": tables, "heap": heap,
            **program_states(program, 1, 0), "topology": self._list})

    def element_jobs(self, node, role: str, target: tuple[int, int], pixels: tuple[int, int],
                     size: tuple[float, float], masks: tuple = ()) -> list[dict]:
        facts = self.runtime.runtime_type(node.object.type_name).facts
        vertices, indices = element_vertices(node, facts, self.assets)
        return [self._job(node, facts, self._element_name(node, facts, facts["program_family"], role, masks), target,
                          pixels, size, self._textures(masks, self._texture(node) or self.catalog.null_texture),
                          vertices, indices)] if len(vertices) else []

    def material(self, node, facts):
        reference, rule = str(node.render_properties[facts["material_property"]]), self.catalog.material
        if not reference:
            return None
        source, mdf = self.assets.load_mdf(reference)
        if not mdf.materials or int(mdf.materials[0].header.shader_type) != rule["shader_type"]:
            return None
        first = mdf.materials[0]
        key = (str(first.header.mmtr_path), MaterialInstanceContext.from_material(first))
        if key not in self._masters:
            document = bind_drawn_master(key[1], key[0], self.dependencies.resource_data_loader, lambda found, data: (
                self._documents.get(found)
                or self._documents.setdefault(found, parse_drawn_master(data, self.game, found))))
            names = document.sdf.targets[self.master_target].program_indices
            self._masters[key] = document if all(name in names for name in rule["techniques"]) else None
        return None if self._masters[key] is None else (self._masters[key], slotted(
            node, facts["material_variables"], first, resource_version_from_path(source, "mdf2")))

    def _material_job(self, node, facts, bound, name: str, target, pixels, size, textures: dict, vertices, indices,
                      fields: dict | None = None) -> dict:
        document, material = bound
        programs = document.sdf.targets[self.master_target]
        if name not in programs.program_indices:
            raise MaterialGap(f"its master has no program {name}, and what the game draws instead is unread")
        program = programs.programs[programs.program_indices[name][0]]
        values, graph = self._values(node, facts, pixels, size, fields or {}), document.profile.shader_graph_profile
        shaders = {shader.role: (graph.shader_contract(document, program, shader.role), bytes(shader.payload))
                   for shader in program.shaders if shader.present}
        reason = unread(program, shaders, document.profile.user_material_buffer_names, material, values.keys() | set(
            () if self.clock is None else self.clock.catalog.fields), textures)
        if reason:
            raise MaterialGap(reason)
        tables, heap, missing = shader_bindings(
            document, shaders, material, self.load_file,
            lambda contract, resource: bytes(laid_out(contract.buffer(resource.name), values, True))
            if resource.register_class == "b" else None,
            lambda name: self._sampler(self.catalog.element_sampler, node) if name == self.catalog.material[
                "glyph_sampler"] else self.samplers.states[name].descriptor(),
            engine_texture=lambda _contract, resource: textures[resource.name] if isinstance(
                textures[resource.name], tuple) else None if (found := self._read(
                    textures[resource.name], resource.dimension)) is None else (0, *found),
            made_texture=lambda path: None if (found := self._read(
                path, "texture2d", self.catalog.material["streamed_smallest"])) is None else ("texture2d", *found))
        if missing:
            raise FileNotFoundError(f"{node.path}'s material samples {', '.join(missing)}, which was not found")
        clocked = {} if self.clock is None else {
            (SHADER_VISIBILITY[stage], resource.bind_point, resource.register_space): contract.buffer(resource.name)
            for stage, (contract, _payload) in shaders.items() for resource in contract.resources
            if resource.register_class == "b" and resource.name == self.clock.catalog.buffer}
        tables = [(*table[:-1], ((0, Following(
            (self.clock.catalog.buffer, layout.byte_size,
             *((item.name, item.byte_offset) for item in layout.variables)),
            lambda layout=layout, clock=self.clock: _engine_heap_fields(layout, bytes(with_words(laid_out(
                layout, values, True), layout, clock_words(clock, clock.catalog.buffer, layout)))))),))
            if table[0] == RANGE_CBV and (layout := clocked.get((table[5], table[1], table[2]))) is not None else table
            for table in tables]
        return self._draw(program, target, pixels, vertices, indices, tables, heap)

    def material_jobs(self, node, role: str, target: tuple[int, int], pixels: tuple[int, int],
                      size: tuple[float, float], masks: tuple = ()) -> list[dict]:
        facts = self.runtime.runtime_type(node.object.type_name).facts
        bound, quads = self.material(node, facts), material_quads(node, facts)
        if bound is None or not quads:
            return []
        return [self._material_job(node, facts, bound, self._element_name(node, facts, facts["program_family"], role,
                                                                          masks), target, pixels, size,
                                   self._textures(masks, self.catalog.null_texture),
                                   *strip_vertices(quads, MATERIAL_VERTEX))]

    def material_text_jobs(self, node, role: str, target: tuple[int, int], pixels: tuple[int, int],
                           size: tuple[float, float], masks: tuple = ()) -> list[dict]:
        facts = self.runtime.runtime_type(node.object.type_name).facts
        bound = self.material(node, facts)
        if bound is None:
            return []
        span = [fmul(gui_half(fmul(f32(side), 0.5)), facts["material_uv_span"])
                for side in node.render_properties[facts["size"]]]
        jobs = []
        for family, texture, vertices, indices, intensity in self.text_layers(node, pixels, size):
            name, fields = self._element_name(node, facts, family, role, masks), {"guiIntensity": [intensity]}
            if family == facts["icon_family"]:
                jobs.append(self._job(node, facts, name, target, pixels, size, self._textures(masks, texture), vertices,
                                      indices, fields))
                continue
            made = np.zeros(len(vertices), MATERIAL_VERTEX)
            made["position"], made["color"], made["uv1"] = vertices["position"], vertices["color"], vertices["uv"]
            made["uv"] = [[gui_half(fdiv(float(position[axis]), span[axis])) for axis in range(2)]
                          for position in vertices["position"]]
            jobs.append(self._material_job(node, facts, bound, name, target, pixels, size, {
                **self._textures(masks, self.catalog.null_texture), self.catalog.material["glyph_texture"]: texture},
                made, indices, fields))
        return jobs

    def blur_type(self, node) -> str:
        return self.runtime.enum_label(node.render_properties, "BlurType", "BlurFilter.BlurType")

    def blur_jobs(self, node, target: tuple[int, int], pixels: tuple[int, int], size: tuple[float, float],
                  sources: dict) -> list[dict]:
        facts, kind, system = (self.runtime.runtime_type(node.object.type_name).facts, self.blur_type(node),
                               self.catalog.system)
        name = self.catalog.program_name(facts["program_names"], family=facts["program_family"], space="screen",
                                         type=kind, test="off")
        scene = self.catalog.blur_chain["scene"]
        fields = {"guiFilterParam": [node.render_properties["BlurScale"] if kind == "Instant"
                                     else fdiv(system["BlurFilterScale"], float(pixels[1]))],
                  "guiFilterMipLevel": [system["BlurFilterMipLevel"]], "guiSaturation": [0.0]}
        return [self._job(node, facts, name, target, pixels, size, {
            self.catalog.element_texture: texture or self.catalog.null_texture, scene["texture"]: sources[scene[kind]]},
            *strip_vertices(quads), fields)
            for texture, quads in blur_quads(node, facts, self.assets, self.runtime.uv_orientations)]

    def blur_sources(self, colour: tuple[int, int], pixels: tuple[int, int], chain: bool) -> tuple[list[int], dict]:
        copy = self.target("scene_copy", pixels)
        steps, sources = [self.device.copy(copy[1], colour[1])], {"copy": (0, *copy)}
        if not chain:
            return steps, sources
        system, data = self.catalog.system, self.catalog.blur_chain
        sizes = level_sizes(pixels, system["BlurFilterReductionRate"], int(system["BlurFilterMipmapCount"]))
        form, key = self.catalog.targets["blur"]["format"], ("blur", sizes[0], "levels")
        if key not in self._targets:
            self._targets[key] = (form, self.device.scene.texture(form, *sizes[0], 1, len(sizes), TEXTURE_2D, 0, None))
        values, source = None, sources["copy"]
        for level, size in enumerate(sizes):
            horizontal, vertical = (self.target("blur", size, held) for held in (0, 1))
            for name, onto in zip(data["programs"], (horizontal, vertical)):
                if values is None:
                    layout = self.program(*self.catalog.filters, name)[1]["pixel"].buffer(data["constants"])
                    values = chain_values(pixels, system, data, next(
                        item.byte_size for item in layout.variables if item.name == "guiFilterGaussianKernelTbl") // 4)
                steps.append(self._step(self._filter_job(
                    name, (0, *onto), size, (0, 0, *size), values, {data["source"]: source}, self.catalog.samplers)))
                source = (0, *onto)
            steps.append(self.device.copy(self._targets[key][1], vertical[1], level, 0))
        return steps, {**sources, "chain": (0, *self._targets[key])}

    def _text_context(self, pixels: tuple[int, int], size: tuple[float, float]) -> TextContext:
        if self._text is None:
            icons, language = self.dependencies.icon_catalog, self.language
            icons.load()
            model = icons.ift.require_model()
            self._text = TextContext(
                LayoutFonts(self.dependencies.font_catalog, self.runtime.text["stem_darkening"]),
                dmc5_text_rules(self.game), (model.font_size, model.descent),
                lambda name: icons.resolve(resolve_dmc5_icon_tag(name, self.runtime.icons, self.icon_context)),
                lambda name, parameter: resolve_dmc5_text_tag(
                    name, parameter, language, self._named_message, self.runtime.message_tags,
                    self.runtime.runtime_target),
                language, 1.0, self.runtime.text["ruby_size_ratio"])
        return replace(self._text, raster_scale=np.float32(pixels[1]) / np.float32(size[1]))

    def _named_message(self, name: str, language: int) -> str:
        self.dependencies.load_messages()
        return self.dependencies.cached_named_message(name, language)

    def _layout(self, node, pixels: tuple[int, int], size: tuple[float, float]):
        properties = node.render_properties
        message = str(properties["Message"] or "")
        if not message and str(properties["MessageId"]) != str(ZERO_GUID):
            self.dependencies.load_messages()
            message = self.dependencies.cached_message(str(properties["MessageId"]),
                                                       self.language)
        values = {**properties, **{name: self.runtime.enum_value(properties, name, enum) for name, enum in (
            ("FontSlot", "FontSlot"), ("AutoRegionFit", "RegionFitType"), ("PageAlignment", "PageAlignment"),
            ("LetterAlignment", "LetterAlignment"))}, "Language": self.runtime.language(properties["Language"])}
        layout = layout_dmc5_text(message, values, self._text_context(pixels, size))
        self._sizes[node.path] = (properties, (pixels, size)), tuple(gui_half(side) for side in layout.size)
        return layout

    def display_size(self, node) -> tuple[float, float]:
        state, held = (node.render_properties, self._frame_size), self._sizes.get(node.path)
        if held is None or held[0] != state:
            try:
                reason = "no frame drawn" if self._frame_size is None else self.layout_gap(node)
                if reason:
                    raise TextLayoutGap(reason)
                self._layout(node, *self._frame_size)
                held = self._sizes[node.path]
            except (TextLayoutGap, FileNotFoundError, GuiAssetError) as gap:
                held = self._sizes[node.path] = state, str(gap)
        if isinstance(held[1], str):
            raise TextLayoutGap(held[1])
        return held[1]

    def text_layers(self, node, pixels: tuple[int, int], size: tuple[float, float]) -> list[tuple]:
        facts, properties = self.runtime.runtime_type(node.object.type_name).facts, node.render_properties
        layout, raster_scale = self._layout(node, pixels, size), self._text_context(pixels, size).raster_scale
        glow, inner, layers = bool(properties["GlowEnable"]), bool(properties["GlowInner"]), []
        for kind, drawn in (("Shadow", properties["ShadowEnable"]), ("Glow", glow and not inner), (None, True),
                            ("Glow", glow and inner)):
            if drawn and kind is None:
                layers += [(facts["icon_family" if icon else "program_family"],
                            texture if icon else self._page(texture), vertices, indices, 1.0)
                           for texture, icon, vertices, indices in text_draws(node, facts, layout, self.atlas)]
            elif drawn:
                layers += [(facts.get("glow_family", facts["program_family"]),
                            self._target_page("glyph_blur", self._blur_pages, page), vertices, indices,
                            float(properties[f"{kind}Strength"]))
                           for page, _icon, vertices, indices in effect_draws(
                               node, facts, layout, self.blurs, kind, raster_scale, self.runtime.degree_factors)]
        return layers

    def text_jobs(self, node, role: str, target: tuple[int, int], pixels: tuple[int, int], size: tuple[float, float],
                  masks: tuple = ()) -> list[dict]:
        facts = self.runtime.runtime_type(node.object.type_name).facts
        return [self._job(node, facts, self._element_name(node, facts, family, role, masks), target, pixels, size,
                          self._textures(masks, texture), vertices, indices, {"guiIntensity": [intensity]})
                for family, texture, vertices, indices, intensity in self.text_layers(node, pixels, size)]

    def _page(self, page: int) -> tuple[int, int, int]:
        form = self.catalog.targets["glyph_atlas"]["format"]
        for at in range(len(self._pages), page + 1):
            self._pages.append(self.device.texture(form, self.atlas.pages.shapes[at], 0,
                                                   self.atlas.images[at].tobytes()))
            self.atlas.changed.discard(at)
        if page in self.atlas.changed:
            self.device.scene.write(self._pages[page], self.atlas.images[page].tobytes())
            self.atlas.changed.discard(page)
        return 0, form, self._pages[page]

    def _target_page(self, kind: str, held: list, page: int) -> tuple[int, int, int]:
        form, size = self.catalog.targets[kind]["format"], tuple(self.catalog.targets[kind]["size"])
        while len(held) <= page:
            held.append(self.device.texture(form, size, RENDER_TARGET))
        return 0, form, held[page]

    def _filter_job(self, name: str, target: tuple[int, int, int], size: tuple[int, int], cell, values: dict,
                    textures: dict, samplers) -> dict:
        program, contracts = self.program(*self.catalog.filters, name)
        tables = []
        for stage, contract in contracts.items():
            visibility = SHADER_VISIBILITY[stage]
            for resource in contract.resources:
                place = (resource.bind_point, resource.register_space, resource.bind_count)
                if resource.register_class == "s":
                    state = samplers[resource.name].descriptor()
                    tables.append((RANGE_SAMPLER, *place, 0, visibility, ((0, *state),)))
                elif resource.register_class == "b":
                    data = bytes(laid_out(contract.buffer(resource.name), {key: np.asarray(value, np.float64)
                                                                           for key, value in values.items()}, True))
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, data),)))
                else:
                    tables.append((RANGE_SRV, *place, VIEW_DIMENSIONS[resource.dimension], visibility,
                                   (textures[resource.name],)))
        (x, y, width, height), (columns, rows) = cell, size
        return self.device.held({
            "vertex_shader": bytes(program.shader("vertex").payload),
            "pixel_shader": bytes(program.shader("pixel").payload),
            "vertex_buffers": [], "elements": [], "indices": np.arange(3, dtype=np.uint32).tobytes(),
            "index_format": DXGI_FORMAT_R32_UINT, "draw": (3, 1, 0, 0, 0), "size": (columns, rows),
            "viewport": (float(x), float(y), float(width), float(height), 0.0, 1.0), "scissor": (0, 0, columns, rows),
            "targets": [target[1:]], "depth": None, "tables": tables, "heap": [], **program_states(program, 1, 0),
            "topology": self._list})

    def blur_steps(self) -> list[int]:
        steps, filters, pad = [], self.catalog.glyph_filters, self.catalog.targets["glyph_atlas"]["padding"]
        for glyph, blur, inner, page, cell, scratch_page, scratch_cell in self.blurs.pending:
            sharp_page, sharp_box = self.atlas.place(glyph)
            sharp, scratch = (self._page(sharp_page),
                              self._target_page("glyph_scratch", self._scratch_pages, scratch_page))
            blurred = self._target_page("glyph_blur", self._blur_pages, page)
            horizontal, vertical = filter_constants(
                sharp_box, self.atlas.pages.shapes[sharp_page], scratch_cell, self.blurs.scratch.shapes[scratch_page],
                blur, pad, self.catalog.system["GlowShadowTextureMarginPixelValue"])
            programs = filters["programs"]["inner" if inner else "outer"]
            steps.append(self.device.scene.draw(self._filter_job(
                programs[0], scratch, self.blurs.scratch.shapes[scratch_page], scratch_cell, horizontal,
                {filters["source"]: sharp}, self.samplers.states)))
            steps.append(self.device.scene.draw(self._filter_job(
                programs[1], blurred, self.blurs.pages.shapes[page], cell, vertical,
                {filters["source"]: scratch, filters["glyph"]: sharp}, self.samplers.states)))
        self.blurs.pending.clear()
        return steps

    def layout_gap(self, node) -> str | None:
        properties, root = node.render_properties, node
        while root.parent is not None:
            root = root.parent
        if self.runtime.enum_label(root.render_properties, "ViewType") != "Screen":
            return "Text in a world view (its raster scale is unread)"
        return ("typing" if properties["UseTyping"] else "text animation" if properties["AnimationEnable"]
                and properties.get("TextAnimation") else None)

    def gap(self, node) -> str | None:
        facts, properties = self.runtime.runtime_type(node.object.type_name).facts, node.render_properties
        if "glyph_colors" in facts:
            limit = self.catalog.glyph_filters["single_pass_limit"]
            wide = [kind for kind in ("Glow", "Shadow")
                    if properties[f"{kind}Enable"] and max(properties[f"{kind}BlurSize"]) > limit]
            return self.layout_gap(node) or (f"{wide[0]} blur over {limit:g} texels" if wide else None)
        if facts.get("program_names") == "blur" or "material_property" in facts:
            return None
        return node.object.type_name if node.object.type_name not in RULES else direct_texture_gap(node)

    def display_job(self, image: int, output: tuple[int, int], pixels: tuple[int, int]) -> dict:
        item = self.display
        program, contracts = self.program(item.file, item.target, item.name)
        width, height = pixels
        tables = []
        for stage, contract in contracts.items():
            visibility = SHADER_VISIBILITY[stage]
            for resource in contract.resources:
                place = (resource.bind_point, resource.register_space, resource.bind_count)
                if resource.register_class == "s":
                    state = self.samplers.states[resource.name].descriptor()
                    tables.append((RANGE_SAMPLER, *place, 0, visibility, ((0, *state),)))
                elif resource.register_class == "t":
                    tables.append((RANGE_SRV, *place, VIEW_DIMENSIONS[resource.dimension], visibility,
                                   ((0, self.catalog.targets["colour"]["format"], image),)))
                elif resource.register_class == "b" and resource.name in FRAME_BUFFERS:
                    data = bytes(laid_out(contract.buffer(resource.name), frame_values(resource.name), True))
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, data),)))
                else:
                    raise ValueError(f"the display pass reads {resource.name}, which the GUI preview doesn't make")
        vertices, instances = item.count
        return self.device.held({
            "vertex_shader": bytes(program.shader("vertex").payload),
            "pixel_shader": bytes(program.shader("pixel").payload),
            "vertex_buffers": [], "elements": [], "indices": np.arange(vertices, dtype=np.uint32).tobytes(),
            "index_format": DXGI_FORMAT_R32_UINT, "draw": (vertices, instances, 0, 0, 0), "size": (width, height),
            "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0), "scissor": (0, 0, width, height),
            "targets": [output], "depth": None, "tables": tables, "heap": [], **program_states(program, 1, 0)})

    def target(self, kind: str, size: tuple[int, int], slot: int = 0) -> tuple[int, int]:
        key = (kind, size, slot)
        if key not in self._targets:
            form = self.display.outputs[0].format if kind == "display" else self.catalog.targets[kind]["format"]
            self._targets[key] = (form, self.device.texture(form, size, RENDER_TARGET))
        return self._targets[key]

    def frame(self, scene, size: tuple[int, int],
              visible: Callable = lambda node: node.effective_visible) -> tuple[int, list]:
        colour, shown = self.target("colour", size), self.target("display", size)
        root, left = scene.root, []
        screen = tuple(float(side) for side in root.render_properties["ScreenSize"])
        self._frame_size = size, screen
        masks = mask_producers(scene.draw_nodes, lambda node: node is not root and visible(node))
        clearing, steps = [self._step(target) for target in (colour, *(
            self.target("mask", size, level)
            for level in sorted({level for scope in masks.values() for level in scope})))], []
        drawn = [node for node in scene.draw_nodes if node is not root and visible(node)
                 and self.runtime.runtime_type(node.object.type_name).facts.get("drawable")]
        blurs = [self.runtime.runtime_type(node.object.type_name).facts.get("program_names") == "blur"
                 for node in drawn]
        if any(blurs):
            filters = [node for node, blur in zip(drawn, blurs) if blur]
            steps, sources = self.blur_sources(colour, size, "chain" in {
                self.catalog.blur_chain["scene"][self.blur_type(node)] for node in filters})
            for node in filters:
                steps += self._drawn(node, (size, screen, sources), lambda node=node: self.blur_jobs(
                    node, colour, size, screen, sources))[0]
        for node, role, writes, reads, clears in mask_passes([node for node, blur in zip(drawn, blurs) if not blur],
                                                             masks):
            target = colour if writes is None else self.target("mask", size, writes)
            if clears:
                steps.append(self._step(target))
            reason, surfaces = self.gap(node), tuple(self.target("mask", size, level) for level in reads)
            if reason:
                left.append((node.path, reason))
                continue
            facts = self.runtime.runtime_type(node.object.type_name).facts
            text, material = "glyph_colors" in facts, "material_property" in facts
            made, reason = self._drawn(node, (size, screen, role, target, surfaces), lambda node=node: (
                self.material_text_jobs if text and material else self.text_jobs if text else self.material_jobs
                if material else self.element_jobs)(node, role, target, size, screen, surfaces))
            if reason:
                left.append((node.path, reason))
            steps += made
        steps.append(self._step(self.display_job(colour[1], shown, size)))
        self.device.pending.extend(clearing + self.blur_steps() + steps)
        self.device.refresh()
        return shown[1], left

    def _step(self, job) -> int:
        key = _frozen(job)
        if key not in self._steps:
            self._steps[key] = self.device.scene.clear(*job) if isinstance(job, tuple) else self.device.scene.draw(job)
        return self._steps[key]

    def _drawn(self, node, state: tuple, jobs: Callable[[], list]) -> tuple[list[int], str | None]:
        state = (node.render_properties, node.world_matrix, node.color_scale, node.color_offset, node.saturation,
                 *state)
        held = self._built.get(node.path)
        if held is None or held[0] != state:
            self._slot = [node.path, 0]
            try:
                held = (state, [self._step(job) for job in jobs()], None)
            except (TextLayoutGap, MaterialGap, FileNotFoundError, GuiAssetError) as gap:
                held = (state, [], str(gap))
            finally:
                self._slot = None
            self._built[node.path] = held
        return held[1], held[2]

    def forget(self) -> None:
        self._built.clear()
        self._sizes.clear()
        self._text = None
