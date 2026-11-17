"""See docs/internals/gui-rendering.md, Device."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from math import inf

import numpy as np

from file_handlers.native_math import f32, fadd, fdiv, fmul, fsub
lazy from file_handlers.engine_math import transform_product
lazy from file_handlers.ift.model import IconGlyph
lazy from file_handlers.mmtr.engine_buffers import (
    FRAME_BUFFERS, Camera, frame_values, laid_out, scene_values, with_words,
)
lazy from file_handlers.mmtr.draw_rules import program_flags
lazy from file_handlers.mmtr.frame_clock import FrameClock, clock_words, frame_clock_catalog_for
lazy from file_handlers.mmtr.light_passes import light_pass_catalog_for, parsed_pass_file
lazy from file_handlers.mmtr.material_context import MaterialInstanceContext
lazy from file_handlers.mmtr.material_family import bind_drawn_master
lazy from file_handlers.mmtr.material_preview import (
    RANGE_CBV, RANGE_SAMPLER, RANGE_SRV, SHADER_VISIBILITY, VIEW_DIMENSIONS, Following, TranslatedError,
    _engine_heap_fields, _texture_texels, shader_bindings,
)
lazy from file_handlers.mmtr.mesh_draw import MeshBuffer, SubmeshRange, input_elements, program_states, submesh_geometry
lazy from file_handlers.mmtr.mmtr_file import parse_drawn_master
lazy from file_handlers.mmtr.pass_choice import GUI_MESH, own_program_name
lazy from file_handlers.mmtr.resident import DEPTH_STENCIL, RENDER_TARGET, TEXTURE_2D
lazy from file_handlers.mmtr.sampler_states import sampler_catalog_for
lazy from file_handlers.sdf.contracts import program_contracts
lazy from file_handlers.sdf.dxbc import PRIMITIVE_TOPOLOGY_NAMES, strict_dxbc
lazy from file_handlers.sdf.models import program_names
lazy from file_handlers.tex.dxgi import DXGI_FORMAT_R16_UINT, DXGI_FORMAT_R32_UINT
lazy from utils.enum_manager import game_enum_definitions
lazy from utils.hash_util import murmur3_hash_utf16le
lazy from utils.resource_file_utils import resource_version_from_path

lazy from ..errors import GuiAssetError
lazy from ..model import ZERO_GUID
lazy from ..passes import gui_pass_catalog_for
lazy from ..sf6.input_icons import key_name
lazy from ..sf6.message_tags import Sf6TagState, resolve_sf6_text_tag
lazy from .blur_chain import chain_programs, chain_values, level_sizes
lazy from .cameras import gui_camera, gui_view, lens_camera
lazy from .glyph_draws import (GlyphAtlas, GlyphBlurs, GlyphDistances, distance_constants, distance_draws, effect_draws,
                               filter_constants, glyph_cells, sprite_corners, text_draws, wide_constants)
lazy from .icon_tags import resolve_dmc5_icon_tag
lazy from .masks import mask_passes
lazy from .materials import MaterialGap, slotted, unread
lazy from .meshes import entity_world, listed_lights, mesh_parts, mesh_world
lazy from .text import dmc5_text_rules
lazy from .text_layout import LayoutFonts, TextContext, TextLayoutGap, layout_dmc5_text
lazy from .text_tags import resolve_dmc5_text_tag
lazy from .vertices import (
    CUT, MATERIAL_VERTEX, PASSES, RULES, add_color, blur_quads, direct_texture_gap, element_vertices, gui_half,
    material_quads, pattern_source, stored_half, strip_vertices, texture_source, world_matrix,
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
        self.atlas = self.blurs = self.distances = None
        self._pages, self._distance_pages, self._text = [], [], None
        self.language = runtime.language(runtime.preview_language)
        self._sizes, self._frame_size, self._world, self._view_key, self._view = {}, None, None, None, None
        self._blur_pages, self._scratch_pages, self._quarter_pages = [], [], []
        self.typed, self._drawing, self.animations = {}, (0, False, 0), None
        self._lit, self._lights, self._buffers, self._refreshed = {}, None, {}, None

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
        return None

    def scene_camera(self, pixels: tuple[int, int], lens=None) -> Camera:
        if (pixels, lens) not in self._cameras:
            self._cameras[pixels, lens] = lens_camera(self.camera, np.eye(3, 4), pixels) if lens is None else (
                gui_camera(self.catalog, lens, pixels))
        return self._cameras[pixels, lens]

    def _values(self, node, facts, pixels: tuple[int, int], size: tuple[float, float], fields: dict) -> dict:
        width, height = size
        system, world, placed = self.catalog.system, world_matrix(node, facts), self._world
        camera = self.scene_camera(pixels, placed[1]) if placed else None if self.camera is None else (
            self.scene_camera(pixels))
        values = {**({} if camera is None else scene_values(camera)),
                  "guiViewMatrix": (np.eye(4) if placed is None else placed[0]).ravel(),
                  "guiProjMatrix": [fdiv(2.0, width), 0, 0, 0, 0, fdiv(-2.0, height), 0, 0, 0, 0, 0, 0, -1, 1, 0, 1],
                  "guiWorldMat": world.ravel(), "guiIntensity": [1.0],
                  "worldMat": (world if placed is None else transform_product(world, placed[0])).ravel(),
                  "guiSaturation": [fsub(1.0, node.saturation)],
                  "guiStencilScale": [0.0 if placed and placed[2]["StencilZeroFill"] else 1.0],
                  "guiAlphaBlendRate": [system["LinearAlphaBlendRate"] if node.render_properties.get("LinearAlphaBlend")
                                        else 0.0], "screenSize": pixels,
                  "screenInverseSize": [fdiv(1.0, float(side)) for side in pixels], **self.catalog.constants,
                  "guiShaderCommonFlag": [self._drawing[2]], **({} if self.catalog.add_color is None else {
                      "guiAdjustAddColor": add_color(node, self.catalog.add_color, self.runtime.srgb_to_linear)}),
                  **self.engine, **fields}
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
                    state = (self.samplers.states[resource.name].descriptor() if resource.register_space ==
                             self.catalog.static_space else self._sampler(resource.name, node))
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
        blend, placed, (channel, noalpha, _flag) = (self.runtime.enum_label(node.render_properties, "BlendType"),
                                                    self._world, self._drawing)
        return self.catalog.program_name(
            facts["program_names"], family=family, space="camera" if placed else "screen", blend=blend, mask=role,
            test="on" if placed and placed[2]["DepthTest"] else "off", nest=str(len(masks) if role == "write" else 0),
            channel="on" if channel else "off", noalpha="on" if noalpha else "off",
            material="on" if "material_property" in facts else "off",
            **{key: "on" if all(self._view[name] for name in names) else "off"
               for key, names in self.catalog.view_choices.items()})

    def draw_context(self, node, flag: int) -> tuple[int, bool, int]:
        rule, bits, properties = self.catalog.draw_context.get(node.object.type_name), self.catalog.common_flag, (
            node.render_properties)
        if bits is None:
            return 0, False, flag
        channel = 0 if rule is None or "channel" not in rule else self.runtime.enum_value(
            properties, rule["channel"], "AlphaChannelType")
        flag &= ~bits["channel"]
        legacy = bits["legacy_linear_alpha_blend"] if self.catalog.system["UseLegacyLinearAlphaBlend"] else 0
        flag = flag | bits["linear_alpha_blend"] | legacy if properties["LinearAlphaBlend"] else (
            flag & ~bits["linear_alpha_blend"])
        return channel, rule is not None and bool(properties[rule["noalpha"]]), flag | channel

    def _textures(self, masks, texture) -> dict:
        return {**{name: (0, *surface) for name, surface in zip(self.catalog.mask_textures, masks)},
                self.catalog.element_texture: texture, **({} if self._world is None else {
                    self.catalog.depth_texture: (0, *self._world[3])})}

    def _job(self, node, facts, name: str, target, pixels, size, textures: dict, vertices, indices,
             fields: dict | None = None) -> dict:
        found = self.program(*self.catalog.programs, name)
        if found is None:
            raise GuiAssetError(f"{self.catalog.programs[0]} has no program {name}")
        program, contracts = found
        return self._draw(program, target, pixels, vertices, indices,
                          self._tables(contracts, node, facts, pixels, size, textures, fields or {}), [])

    def _kept(self, key: tuple, data: bytes) -> Following:
        key, latest = (*key, len(data)), self._latest
        latest[key] = data
        return Following(key, lambda: latest[key])

    def _draw(self, program, target, pixels, vertices, indices, tables: list, heap) -> dict:
        listed, data, states = _triangles(indices), vertices.tobytes(), program_states(program, 1, 0)
        width, height = pixels
        if self._slot is not None:
            self._slot[-1] += 1
            tables = [(*table[:-1], tuple((index, self._kept((*self._slot, at, index), held) if isinstance(
                held, bytes) else held) for index, held in table[-1])) if table[0] == RANGE_CBV else table
                for at, table in enumerate(tables)]
            data = self.device.following(*self._kept((*self._slot, "vertices"), data), 1)
        return self.device.held({
            "vertex_shader": strict_dxbc(bytes(program.shader("vertex").payload)),
            "pixel_shader": strict_dxbc(bytes(program.shader("pixel").payload)),
            "vertex_buffers": [(data, vertices.dtype.itemsize)], "elements": input_elements(program),
            "indices": listed, "index_format": DXGI_FORMAT_R16_UINT, "draw": (len(listed) // 2, 1, 0, 0, 0),
            "size": (width, height), "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0),
            "scissor": (0, 0, width, height), "targets": [target],
            "depth": self._depth(pixels) if states["depth_stencil"][0] or states["depth_stencil"][3] else None,
            "tables": tables, "heap": heap,
            **states, "topology": self._list})

    def _depth(self, size: tuple[int, int]) -> tuple[int, int]:
        key = ("gui_depth", size, 0)
        if key not in self._targets:
            form = self.catalog.targets["gui_depth"]["format"]
            self._targets[key] = (form, self.device.texture(form, size, DEPTH_STENCIL))
        return self._targets[key]

    def element_jobs(self, node, role: str, target: tuple[int, int], pixels: tuple[int, int],
                     size: tuple[float, float], masks: tuple = ()) -> list[dict]:
        facts = self.runtime.runtime_type(node.object.type_name).facts
        name = self._element_name(node, facts, facts["program_family"], role, masks)
        if node.object.type_name in PASSES:
            return [self._job(node, facts, name, target, pixels, size, self._textures(masks, path),
                              *strip_vertices(quads)) for path, quads in PASSES[node.object.type_name](
                node, facts, self.assets)]
        vertices, indices = element_vertices(node, facts, self.assets)
        return [self._job(node, facts, name, target, pixels, size, self._textures(
            masks, self._texture(node) or self.catalog.null_texture), vertices, indices)] if len(vertices) else []

    def material(self, node, facts):
        reference, rule = str(node.render_properties[facts["material_property"]]), self.catalog.material
        if not reference:
            return None
        source, mdf = self.assets.load_mdf(reference)
        if not mdf.materials or int(mdf.materials[0].header.shader_type) != rule["shader_type"]:
            return None
        document = self._master(mdf.materials[0])
        names = document.sdf.targets[self.master_target].program_indices
        return None if not all(name in names for name in self.catalog.tables["techniques"]) else (document, slotted(
            node, facts["material_variables"], mdf.materials[0], resource_version_from_path(source, "mdf2"),
            self.assets))

    def _master(self, material):
        key = (str(material.header.mmtr_path), MaterialInstanceContext.from_material(material))
        if key not in self._masters:
            self._masters[key] = bind_drawn_master(
                key[1], key[0], self.dependencies.resource_data_loader, lambda found, data: (
                    self._documents.get(found)
                    or self._documents.setdefault(found, parse_drawn_master(data, self.game, found))))
        return self._masters[key]

    def _material_job(self, node, facts, bound, name: str, target, pixels, size, textures: dict, vertices, indices,
                      fields: dict | None = None) -> dict:
        programs = bound[0].sdf.targets[self.master_target]
        if name not in programs.program_indices:
            raise MaterialGap(f"its master has no program {name}, and what the game draws instead is unread")
        program = programs.programs[programs.program_indices[name][0]]
        return self._draw(program, target, pixels, vertices, indices, *self._material_tables(
            node, bound, program, self._values(node, facts, pixels, size, fields or {}), textures))

    def _material_tables(self, node, bound, program, values: dict, textures: dict, rows=None) -> tuple[list, tuple]:
        document, material = bound
        graph = document.profile.shader_graph_profile
        shaders = {shader.role: (graph.shader_contract(document, program, shader.role), bytes(shader.payload))
                   for shader in program.shaders if shader.present}
        reason = unread(program, shaders, (*document.profile.user_material_buffer_names, *(
            () if rows is None else (self.catalog.mesh["light_buffer"],))), material, values.keys() | set(
            () if self.clock is None else self.clock.catalog.fields), textures, self.catalog.material["frame_textures"])
        if reason:
            raise MaterialGap(reason)

        def engine_buffer(contract, resource):
            if resource.register_class == "b":
                return bytes(laid_out(contract.buffer(resource.name), values, True))
            if rows is None or resource.name != self.catalog.mesh["light_buffer"]:
                return None
            layout = contract.structured_buffer(resource.name)
            return b"".join(bytes(laid_out(layout, row, False)) for row in rows).ljust(
                layout.byte_size * self.catalog.mesh["lights"]["rows"], b"\0")
        tables, heap, missing = shader_bindings(
            document, shaders, material, self.load_file, engine_buffer,
            lambda name: self._sampler(self.catalog.element_sampler, node) if name == self.catalog.material[
                "glyph_sampler"] else self.samplers.states[name].descriptor(),
            engine_texture=lambda _contract, resource: textures[resource.name] if isinstance(
                textures[resource.name], tuple) else None if (found := self._read(
                    textures[resource.name], resource.dimension)) is None else (0, *found),
            made_texture=lambda path: None if (found := next(filter(None, (self._read(
                at, "texture2d", self.catalog.material["streamed_smallest"]) for at in (f"streaming/{path}", path))),
                None)) is None else ("texture2d", *found))
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
        return tables, heap

    def mesh_jobs(self, node, target: tuple[int, int], pixels: tuple[int, int]) -> list[dict]:
        facts, rule = self.runtime.runtime_type(node.object.type_name).facts, self.catalog.mesh
        names, held, named = facts["mesh"], node.render_properties, {}
        if not held[names["mesh"]] or not held[names["material"]]:
            return []
        reference = str(held[names["mesh"]])
        mesh, (source, mdf) = self.assets.load_mesh(reference)[1], self.assets.load_mdf(str(held[names["material"]]))
        for material in mdf.materials:
            named.setdefault(str(material.header.mat_name), material)
        parts = [(named[name], submesh) for name, submesh in mesh_parts(mesh)
                 if name in named and int(named[name].header.shader_type) in names["shading"]]
        if parts and (not self._world or rule is None):
            raise MaterialGap("a Mesh outside a placed GUI-camera view is drawn through a camera the preview doesn't "
                              "have" if not self._world else "a Mesh in a placed GUI-camera view is not drawn: the "
                              "game's files place none, so its pass is unread")
        if not parts or node.color_scale[3] < rule["least_alpha"]:
            return []
        (rows, masks), world = self._lights, mesh_world(node, names, rule, self.runtime.degree_factors, self._world[0],
                                                        entity_world(self._world[4]["world"],
                                                                     self._world[4]["rotation"])).ravel()
        colour, scene = self.catalog.targets["colour"]["format"], self.scene_camera(pixels, self._world[1])
        values = {name: np.asarray(value, np.float64) for name, value in {
            **scene_values(scene), "worldMat": world, "prevWorldMat": world, "jointOffset": [rule["joint_offset"]],
            "prevJoinOffset": [rule["joint_offset"]], "colorTransform": node.color_scale,
            "stencilScale": [0.0 if self._world[2]["StencilZeroFill"] else 1.0], "GUIPunctualLightCount": [len(rows)],
            "LightMask": [sum(mask for group, mask in masks.items() if held[names["light_groups"]] >> group & 1)],
            **rule["frame"], **self.engine}.items()}
        textures = {rule["scene_texture"]: (0, colour, self.device.zeros(colour, pixels, 0, 4 * pixels[0] * pixels[1])),
                    rule["ies_texture"]: (0, rule["ies"]["format"], *rule["ies"]["shape"], bytes(
                        2 * rule["ies"]["shape"][0] * rule["ies"]["shape"][2]))}
        jobs, width, height = [], *pixels
        for material, submesh in parts:
            document = self._master(material)
            made = slotted(node, facts["material_variables"], material, resource_version_from_path(source, "mdf2"),
                           self.assets)
            programs = document.sdf.targets[self.master_target]
            program = own_program_name(program_flags(document.profile, made), GUI_MESH, rule["route"])
            if program not in programs.program_indices:
                raise MaterialGap(f"its master has no program {program}, and what the game draws instead is unread")
            program = programs.programs[programs.program_indices[program][0]]
            tables, heap = self._material_tables(node, (document, made), program, values, textures, rows)
            key = reference, submesh.buffer_index
            if key not in self._buffers:
                self._buffers[key] = MeshBuffer.from_payload(mesh.mesh_buffer.buffer_payloads[key[1]])
            vertex = document.profile.shader_graph_profile.shader_contract(document, program, "vertex")
            jobs.append(self.device.held({
                "vertex_shader": strict_dxbc(bytes(program.shader("vertex").payload)),
                "pixel_shader": strict_dxbc(bytes(program.shader("pixel").payload)),
                **submesh_geometry(program, vertex, self._buffers[key], SubmeshRange.from_submesh(submesh)),
                "size": (width, height), "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0),
                "scissor": (0, 0, width, height), "targets": [target], "depth": self._depth(pixels),
                "tables": tables, "heap": heap, **program_states(program, 1, 0)}))
        return jobs

    def material_jobs(self, node, role: str, target: tuple[int, int], pixels: tuple[int, int],
                      size: tuple[float, float], masks: tuple = ()) -> list[dict]:
        facts = self.runtime.runtime_type(node.object.type_name).facts
        bound, quads = self.material(node, facts), material_quads(node, facts, self.assets)
        if bound is None or not quads:
            return []
        return [self._material_job(node, facts, bound, self._element_name(node, facts, facts["program_family"], role,
                                                                          masks), target, pixels, size,
                                   self._textures(masks, self.catalog.null_texture),
                                   *strip_vertices(quads, MATERIAL_VERTEX))]

    def material_text_jobs(self, node, role: str, target: tuple[int, int], pixels: tuple[int, int],
                           size: tuple[float, float], masks: tuple = ()) -> list[dict]:
        facts, properties = self.runtime.runtime_type(node.object.type_name).facts, node.render_properties
        bound = self.material(node, facts)
        if bound is None:
            return []
        layers, jobs = self.text_layers(node, pixels, size, False), []
        if "material_uv_type" in facts and not self.catalog.system["UseLegacyMaterialTextUV"]:
            halves, control = [stored_half(node, side) for side in properties[facts["size"]]], (
                self.runtime.enum_value(properties, "ControlPoint"))
            corner, kind = [fmul(at, half) for at, half in zip((control & 3, control >> 2), halves)], (
                facts["material_uv_type"])
            if self.runtime.enum_value(properties, kind["property"], kind["enum"]):
                align, display = self.runtime.enum_value(properties, "PageAlignment"), self.display_size(node)
                shift = [fmul(fsub(half, fmul(side, 0.5)), at) for half, side, at in zip(halves, display, (
                    align & 3, align >> 2))]
                material_uv = lambda position: [gui_half(fdiv(fsub(fadd(corner[axis], position[axis]), shift[axis]),
                                                              display[axis])) for axis in range(2)]
            else:
                material_uv = lambda position: [gui_half(fdiv(fadd(corner[axis], position[axis]),
                                                              fmul(halves[axis], 2.0))) for axis in range(2)]
        else:
            span = [fmul(gui_half(fmul(f32(side), 0.5)), facts["material_uv_span"])
                    for side in properties[facts["size"]]]
            material_uv = lambda position: [gui_half(fdiv(float(position[axis]), span[axis])) for axis in range(2)]
        for family, texture, vertices, indices, fields in layers:
            name = self._element_name(node, facts, family, role, masks)
            if family == facts["icon_family"]:
                jobs.append(self._job(node, facts, name, target, pixels, size, self._textures(masks, texture), vertices,
                                      indices, fields))
                continue
            made = np.zeros(len(vertices), MATERIAL_VERTEX)
            made["position"], made["color"], made["uv1"] = vertices["position"], vertices["color"], vertices["uv"]
            made["uv"] = [material_uv(position) for position in vertices["position"]]
            jobs.append(self._material_job(node, facts, bound, name, target, pixels, size, {
                **self._textures(masks, self.catalog.null_texture), self.catalog.material["glyph_texture"]: texture},
                sprite_corners(made) if self._animated(node) else made, indices, fields))
        return jobs

    def blur_type(self, node) -> str:
        return self.runtime.enum_label(node.render_properties, "BlurType",
                                       self.runtime.runtime_type(node.object.type_name).facts["type_enum"])

    def blur_jobs(self, node, target: tuple[int, int], pixels: tuple[int, int], size: tuple[float, float],
                  sources: dict) -> list[dict]:
        facts, kind, system, placed = (self.runtime.runtime_type(node.object.type_name).facts, self.blur_type(node),
                                       self.catalog.system, self._world)
        name = self.catalog.program_name(
            facts["program_names"], family=facts["program_family"], space="camera" if placed else "screen",
            type=kind, test="on" if placed and placed[2]["DepthTest"] else "off")
        scene = self.catalog.blur_chain["scene"]
        fields = {"guiFilterParam": [node.render_properties["BlurScale"] if kind == "Instant"
                                     else fdiv(system["BlurFilterScale"], float(pixels[1]))],
                  "guiFilterMipLevel": [system["BlurFilterMipLevel"]], "guiSaturation": [0.0]}
        return [self._job(node, facts, name, target, pixels, size, {
            **self._textures((), texture or self.catalog.null_texture), scene["texture"]: sources[scene[kind]]},
            *strip_vertices(quads), fields)
            for texture, quads in blur_quads(node, facts, self.assets)]

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
            for name, onto in zip(chain_programs(data, system), (horizontal, vertical)):
                if values is None:
                    layout = self.program(*self.catalog.filters, name)[1]["pixel"].buffer(data["constants"])
                    values = chain_values(pixels, system, data, next(
                        item.byte_size for item in layout.variables if item.name == data["fields"]["kernel"]) // 4)
                steps.append(self._step(self._filter_job(
                    name, (0, *onto), size, (0, 0, *size), values, {data["source"]: source}, self.catalog.samplers)))
                source = (0, *onto)
            steps.append(self.device.copy(self._targets[key][1], vertical[1], level, 0))
        return steps, {**sources, "chain": (0, *self._targets[key])}

    def _text_context(self, pixels: tuple[int, int], size: tuple[float, float]) -> TextContext:
        rules = dmc5_text_rules(self.game)
        if self.atlas is None:
            atlas, targets = self.catalog.targets["glyph_atlas"], self.catalog.targets
            self.atlas = GlyphAtlas(tuple(atlas["size"]), tuple(atlas["largest"]), atlas["padding"], rules.quad_margin,
                                    atlas["unused_frames"])
            filters = self.catalog.glyph_filters
            self.blurs = None if filters is None else GlyphBlurs(
                tuple(targets["glyph_blur"]["size"]), tuple(targets["glyph_scratch"]["size"]),
                None if filters["wide_programs"] is None else tuple(targets["glyph_quarter"]["size"]),
                atlas["padding"], targets["glyph_blur"]["unused_frames"])
            distance = self.catalog.distance_field
            self.distances = None if distance is None else GlyphDistances(
                tuple(targets["glyph_distance"]["size"]), distance["spread"],
                targets["glyph_distance"]["unused_frames"])
        if self._text is None:
            if self.dependencies.font_catalog is None:
                raise TextLayoutGap("the game's font config was not found")
            fonts = LayoutFonts(self.dependencies.font_catalog, self.runtime.text["stem_darkening"])
            presets = {preset.name_hash: preset.colors[0] for preset in self.assets.color_presets.values()}
            self._text = TextContext(fonts, rules, None, None, None, self.language, 1.0,
                                     self.runtime.text["ruby_size_ratio"], self._referenced_message,
                                     lambda name: (rgb := presets.get(murmur3_hash_utf16le(name))) and (
                                         rgb[0] | rgb[1] << 8 | rgb[2] << 16))
            if (icons := self.dependencies.icon_catalog) is not None:
                icons.load()
                model, table = icons.ift.require_model(), self.runtime.icons
                self._text = replace(self._text, icon_font=(model.font_size, model.descent, *model.extra_floats),
                                     icon_resolver=lambda name: icons.resolve(name if table is None else (
                                         resolve_dmc5_icon_tag(name, table, self.icon_context))))
            if (tags := self.runtime.message_tags) is not None:
                language, dependencies = self.language, self.dependencies
                state = tags["kind"] == "sf6" and Sf6TagState(
                    tags, language, self.runtime.language, lambda guid: self._message(guid, language),
                    self._referenced_message, lambda: dependencies.world.tables if dependencies.in_game else None,
                    lambda enum: {item["name"]: item["value"] for item in game_enum_definitions(
                        self.game, namespace=enum)[enum]}, self.icon_context.keyboard_mode,
                    lambda: dependencies.world.input if dependencies.in_game else None, key_name)
                self._text = replace(self._text, tag_resolver=(lambda name, parameter: resolve_sf6_text_tag(
                    name, parameter, state)) if state else (lambda name, parameter: resolve_dmc5_text_tag(
                        name, parameter, language, self._named_message, tags, self.runtime.runtime_target)))
        return replace(self._text, raster_scale=np.float32(pixels[1]) / np.float32(size[1]))

    def _message(self, message_id: str, language: int) -> str | None:
        self.dependencies.load_messages()
        return (found := self.dependencies.message(message_id)) and found.text(language)

    def _named_message(self, name: str, language: int) -> str:
        self.dependencies.load_messages()
        return self.dependencies.cached_named_message(name, language)

    def _referenced_message(self, name: str) -> str | None:
        self.dependencies.load_messages()
        message = self.dependencies.named_message(name)
        return message.text(self.language) if message is not None and message.name == name else None

    def _message_text(self, node) -> str:
        properties = node.render_properties
        message = str(properties["Message"] or "")
        if not message and str(properties["MessageId"]) != str(ZERO_GUID):
            self.dependencies.load_messages()
            message = self.dependencies.cached_message(str(properties["MessageId"]), self.language)
        return message

    def _animated(self, node) -> bool:
        return self.animations is not None and self.animations.active(node)

    def _layout(self, node, pixels: tuple[int, int], size: tuple[float, float]):
        properties, message = node.render_properties, self._message_text(node)
        values = {**properties, **{name: self.runtime.enum_value(properties, name, enum) for name, enum in (
            ("FontSlot", "FontSlot"), ("AutoRegionFit", "RegionFitType"), ("PageAlignment", "PageAlignment"),
            ("LetterAlignment", "LetterAlignment"))}, "Language": self.runtime.language(properties["Language"])}
        context = self._text_context(pixels, size)
        layout = layout_dmc5_text(message, values, context)
        self._sizes[node.path] = (properties, (pixels, size)), tuple(
            gui_half(side) if context.rules.region_halves else float(side) for side in layout.size)
        return layout

    def display_size(self, node) -> tuple[float, float]:
        state, held = (node.render_properties, self._frame_size), self._sizes.get(node.path)
        if held is None or held[0] != state:
            try:
                reason = "no frame is drawn" if self._frame_size is None else self.layout_gap(node)
                if reason:
                    raise TextLayoutGap(reason)
                self._layout(node, *self._frame_size)
                held = self._sizes[node.path]
            except (TextLayoutGap, FileNotFoundError, GuiAssetError) as gap:
                held = self._sizes[node.path] = state, str(gap)
        if isinstance(held[1], str):
            raise TextLayoutGap(held[1])
        return held[1]

    def _typed(self, node) -> float:
        return self.typed.get(node.path, 0) if node.render_properties.get("UseTyping") else inf

    def text_layers(self, node, pixels: tuple[int, int], size: tuple[float, float], clip: bool = True) -> list[tuple]:
        facts, properties = self.runtime.runtime_type(node.object.type_name).facts, node.render_properties
        layout, context = self._layout(node, pixels, size), self._text_context(pixels, size)
        typed = self._typed(node)
        glow, inner, layers = bool(properties["GlowEnable"]), bool(properties["GlowInner"]), []
        distance = "distance_field" in facts and all(properties[name] for name in facts["distance_field"]["enable"])
        if distance and any(isinstance(quad.glyph, IconGlyph) for quad in layout.quads):
            raise TextLayoutGap("an icon's distance field is not drawn")
        alphas = self.animations.alphas(node, len(layout.quads), len(self._message_text(node))) if self._animated(
            node) else ()
        limit = self.catalog.glyph_filters and self.catalog.glyph_filters["single_pass_limit"]
        if "icon_effects" in facts and properties[facts["icon_effects"]] and any(
                properties[f"{kind}Enable"] and max(properties[f"{kind}BlurSize"]) * context.raster_scale > limit
                for kind in ("Glow", "Shadow")) and any(isinstance(quad.glyph, IconGlyph) for quad in layout.quads):
            raise TextLayoutGap(f"its icons' glow or shadow wider than {limit:g} texels is not drawn")
        for cache, held in zip((self.atlas, self.blurs, self.distances), glyph_cells(
                node, facts, layout, self.atlas, self.blurs, self.distances if distance else None, context.raster_scale,
                context.rules)):
            if cache is not None:
                cache.holders[node.path] = held
        for kind, drawn in (("Shadow", properties["ShadowEnable"]), ("Glow", glow and not inner),
                            ("DistanceField", distance), (None, True), ("Glow", glow and inner)):
            if drawn and kind is None:
                kept = [at for at, quad in enumerate(layout.quads) if not distance or properties[
                    "DistanceFieldDrawGlyph"] or self.distances.place(quad.glyph) is None]
                layers += [(facts["icon_family" if icon else "program_family"],
                            texture if icon else self._page(texture), vertices, indices, {"guiIntensity": [1.0]})
                           for texture, icon, vertices, indices in text_draws(
                               node, facts, replace(layout, quads=tuple(layout.quads[at] for at in kept)), self.atlas,
                               self.assets, typed, clip, alphas and tuple(alphas[at] for at in kept))]
            elif drawn and kind == "DistanceField":
                fields = {"guiIntensity": [1.0], **distance_constants(node, self.catalog.distance_field)}
                layers += [(facts["distance_field"]["family"], self._page(page, "glyph_distance"), vertices, indices,
                            fields) for page, _icon, vertices, indices in distance_draws(
                                node, facts, layout, self.distances, context.raster_scale, typed)]
            elif drawn:
                layers += [(facts.get("glow_family", facts["program_family"]),
                            self._target_page("glyph_blur", self._blur_pages, page), vertices, indices,
                            {"guiIntensity": [float(properties[f"{kind}Strength"])]})
                           for page, _icon, vertices, indices in effect_draws(
                               node, facts, layout, self.blurs, kind, context.raster_scale, context.rules,
                               self.runtime.degree_factors, self.assets, typed, clip, alphas)]
        return layers

    def text_jobs(self, node, role: str, target: tuple[int, int], pixels: tuple[int, int], size: tuple[float, float],
                  masks: tuple = ()) -> list[dict]:
        facts, animated = self.runtime.runtime_type(node.object.type_name).facts, self._animated(node)
        return [self._job(node, facts, self._element_name(node, facts, family, role, masks), target, pixels, size,
                          self._textures(masks, texture), sprite_corners(vertices) if animated else vertices, indices,
                          fields)
                for family, texture, vertices, indices, fields in self.text_layers(node, pixels, size)]

    def _page(self, page: int, kind: str = "glyph_atlas") -> tuple[int, int, int]:
        cache, held = (self.atlas, self._pages) if kind == "glyph_atlas" else (self.distances, self._distance_pages)
        form = self.catalog.targets[kind]["format"]
        for at in range(len(held), page + 1):
            held.append(self.device.texture(form, cache.pages.shapes[at], 0, cache.images[at].tobytes()))
            cache.changed.discard(at)
        if page in cache.changed:
            self.device.scene.write(held[page], cache.images[page].tobytes())
            cache.changed.discard(page)
        return 0, form, held[page]

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
                    state = (self.samplers.states if resource.register_space == self.catalog.static_space
                             else samplers)[resource.name].descriptor()
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
            "vertex_shader": strict_dxbc(bytes(program.shader("vertex").payload)),
            "pixel_shader": strict_dxbc(bytes(program.shader("pixel").payload)),
            "vertex_buffers": [], "elements": [], "indices": np.arange(3, dtype=np.uint32).tobytes(),
            "index_format": DXGI_FORMAT_R32_UINT, "draw": (3, 1, 0, 0, 0), "size": (columns, rows),
            "viewport": (float(x), float(y), float(width), float(height), 0.0, 1.0), "scissor": (0, 0, columns, rows),
            "targets": [target[1:]], "depth": None, "tables": tables, "heap": [], **program_states(program, 1, 0),
            "topology": self._list})

    def flush_order(self) -> list:
        order, textures, made = self.catalog.glyph_filters["flush_sort"], {}, self.blurs.pending[::-1]
        if order is None:
            return made

        def key(entry) -> tuple:
            glyph, icon = entry[0][0], isinstance(entry[0][0], IconGlyph)
            sharp = textures.setdefault(glyph.texture_path, len(textures)) if icon else -self.atlas.place(glyph)[0]
            return tuple({"icon": icon, "sharp_page": sharp, "page": -entry[1], "blur": entry[0][1]}[name]
                         for name in order)
        return sorted(made, key=key)

    def blur_steps(self) -> list[int]:
        if self.blurs is None or not self.blurs.pending:
            return []
        filters, pad = self.catalog.glyph_filters, self.catalog.targets["glyph_atlas"]["padding"]
        margin = self.catalog.system["GlowShadowTextureMarginPixelValue"]
        steps = [self._step(self._target_page(kind, held, page)[1:]) for kind, held, pages in (
            ("glyph_scratch", self._scratch_pages, self.blurs.scratch),
            ("glyph_quarter", self._quarter_pages, self.blurs.quarter)) if pages for page in range(len(pages.shapes))]
        for (glyph, blur, inner, knockout), page, cell, (scratch_page, *scratch_cell), quarter in self.flush_order():
            icon = isinstance(glyph, IconGlyph)
            if icon:
                read = self._read(glyph.texture_path, "texture2d")
                sharp, sharp_box, sharp_size, table = (0, *read), glyph.uv_rect, read[1:3], filters["icons"]
            else:
                sharp_page, sharp_box = self.atlas.place(glyph)
                sharp, sharp_size, table = self._page(sharp_page), self.atlas.pages.shapes[sharp_page], filters
            scratch = self._target_page("glyph_scratch", self._scratch_pages, scratch_page)
            blurred, shape = (self._target_page("glyph_blur", self._blur_pages, page),
                              self.blurs.scratch.shapes[scratch_page])
            horizontal, vertical = filter_constants(
                sharp_box, sharp_size, scratch_cell, shape, blur, pad, margin)
            kind = "inner" if inner else "knockout" if knockout else "outer"
            programs, sources = table["programs"][kind], {filters["source"]: sharp}
            first = programs[0]
            if max(blur) > filters["single_pass_limit"]:
                quarter_page, *quarter_cell = quarter
                sources[filters["quarter"]] = self._target_page("glyph_quarter", self._quarter_pages, quarter_page)
                down, horizontal = wide_constants(horizontal, quarter_cell, self.blurs.quarter.shapes[quarter_page],
                                                  shape, blur, margin)
                steps.append(self.device.scene.draw(self._filter_job(
                    first, sources[filters["quarter"]], self.blurs.quarter.shapes[quarter_page], quarter_cell, down,
                    {filters["source"]: sharp}, self.samplers.states)))
                first = table["wide_programs"][kind]
            steps.append(self.device.scene.draw(self._filter_job(
                first, scratch, shape, scratch_cell, horizontal, sources, self.samplers.states)))
            steps.append(self.device.scene.draw(self._filter_job(
                programs[1], blurred, self.blurs.pages.shapes[page], cell, vertical,
                {filters["source"]: scratch, filters["glyph"]: sharp}, self.samplers.states)))
        self.blurs.pending.clear()
        self.blurs.restart()
        return steps

    def layout_gap(self, node) -> str | None:
        properties = node.render_properties
        unported = next((reason for name, reason in self.runtime.text["unported"].items() if properties[name]), None)
        if unported or not (properties["AnimationEnable"] and properties.get("TextAnimation")):
            return unported
        return "text with a text animation is not drawn" if self.animations is None else self.animations.gap(node)

    def gap(self, node) -> str | None:
        facts, properties = self.runtime.runtime_type(node.object.type_name).facts, node.render_properties
        if "draw_gap" in facts:
            return facts["draw_gap"]
        if "unported_properties" in facts and any(properties[name] for name in facts["unported_properties"]):
            return next(reason for name, reason in facts["unported_properties"].items() if properties[name])
        if "glyph_colors" in facts and self.catalog.glyph_filters is None:
            effects = [kind.lower() for kind in ("Glow", "Shadow") if properties[f"{kind}Enable"]]
            return self.layout_gap(node) or (f"its {' and '.join(effects)} is not drawn" if effects else None)
        if "glyph_colors" in facts:
            limit = self.catalog.glyph_filters["single_pass_limit"]
            wide = [kind for kind in ("Glow", "Shadow") if self.catalog.glyph_filters["wide_programs"] is None
                    and properties[f"{kind}Enable"] and max(properties[f"{kind}BlurSize"]) > limit]
            return self.layout_gap(node) or (
                f"a {wide[0].lower()} blur wider than {limit:g} texels is not drawn" if wide else None)
        if facts.get("program_names") == "blur" or "material_property" in facts or "mesh" in facts:
            return None
        kind = node.object.type_name
        if "uv_types" in facts and any(properties["TilingSize"]) and (min(properties["TilingSize"]) < 0.0 or (
                self.runtime.enum_label(properties, "ColorType") == "EachVertex")):
            return "a texture tiled mirrored or with a colour per corner is not drawn"
        return f"{kind} is not drawn" if kind not in RULES else direct_texture_gap(node)

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
            "vertex_shader": strict_dxbc(bytes(program.shader("vertex").payload)),
            "pixel_shader": strict_dxbc(bytes(program.shader("pixel").payload)),
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

    def place(self, view, placed, size: tuple[int, int]) -> None:
        self._world, self._view_key, self._lights, self._view = None, None, None, view
        self._frame_size = size, tuple(float(side) for side in (
            view["ScreenSize"] if placed is None else self.runtime.world_view_output))
        if placed is not None:
            form = self.catalog.targets["scene_depth"]["format"]
            sides = tuple(max(1.0, side) for side in view["ScreenSize"])
            self._world = (gui_view(self.catalog, sides, placed["world"]), placed["lens"], view,
                           (form, self.device.zeros(form, size, 0, 4 * size[0] * size[1])), placed)
            self._view_key = (placed["lens"], tuple(placed["world"]), sides, view["DepthTest"],
                              view["StencilZeroFill"], placed["lights"], placed["rotation"])
            if placed["lights"] not in self._lit:
                self._lit[placed["lights"]] = listed_lights(placed["lit"], self.catalog.mesh["lights"])
            self._lights = self._lit[placed["lights"]]

    def frame(self, scene, size: tuple[int, int], visible: Callable = lambda node: node.effective_visible,
              placed=None) -> tuple[int, list]:
        colour, shown = self.target("colour", size), self.target("display", size)
        root, left = scene.root, []
        self.place(root.render_properties, placed, size)
        screen, steps, written = self._frame_size[1], [], set()
        drawn = [node for node in scene.draw_nodes if node is not root and visible(node)
                 and self.runtime.runtime_type(node.object.type_name).facts.get("drawable")]
        blurs = [self.runtime.runtime_type(node.object.type_name).facts.get("program_names") == "blur"
                 for node in drawn]
        elements = [node for node, blur in zip(drawn, blurs) if not blur]
        drawing = {id(node) for node in elements}
        passes = mask_passes(scene.tree_nodes, lambda node: node is root or visible(node),
                             lambda node: id(node) in drawing)
        clearing = [self._step(target) for target in (colour, self._depth(size), *(
            self.target("mask", size, level) for level in sorted({
            level for _role, writes, reads, _clears in passes.values()
            for level in (*reads, *(() if writes is None else (writes,)))})))]
        if any(blurs):
            filters = [node for node, blur in zip(drawn, blurs) if blur]
            steps, sources = self.blur_sources(colour, size, "chain" in {
                self.catalog.blur_chain["scene"][self.blur_type(node)] for node in filters})
            for node in filters:
                self._drawing = self.draw_context(node, 0)
                steps += self._drawn(node, (size, screen, sources), lambda node=node: self.blur_jobs(
                    node, colour, size, screen, sources))[0]
        flag = 0
        for node in elements:
            role, writes, reads, clears = passes[node.path]
            target = colour if writes is None else self.target("mask", size, writes)
            if clears:
                steps.append(self._step(target))
            reason, surfaces = self.gap(node), tuple(self.target("mask", size, level) for level in reads)
            if written.issuperset(reads):
                written.update(() if writes is None else (writes,))
            else:
                reason = "the mask level it reads is not drawn before it: the game shows what that level held"
            if reason:
                left.append((node.path, reason))
                continue
            facts = self.runtime.runtime_type(node.object.type_name).facts
            text, material = "glyph_colors" in facts, "material_property" in facts
            self._drawing = (0, False, flag) if "mesh" in facts else self.draw_context(node, flag)
            state = (size, screen, role, target, surfaces, self._typed(node), self._drawing,
                     self.animations.serial if self._animated(node) else None)
            made, reason = self._drawn(node, state, lambda node=node: self.mesh_jobs(
                node, target, size) if "mesh" in facts else (
                self.material_text_jobs if text and material else self.text_jobs if text else self.material_jobs
                if material else self.element_jobs)(node, role, target, size, screen, surfaces))
            if reason:
                left.append((node.path, reason))
            steps += made
            flag = self._drawing[2] if made else flag
        steps.append(self._step(self.display_job(colour[1], shown, size)))
        self.device.pending.extend(clearing + self.blur_steps() + steps)
        alive, shown_with = {node.path for node in scene.tree_nodes}, {
            "language": self.language, "input": self.icon_context.keyboard_mode,
            "raster_scale": np.float32(size[1]) / np.float32(screen[1])}
        refreshed = tuple(shown_with[name] for name in self.catalog.targets["glyph_atlas"]["refreshed_by"])
        refresh, self._refreshed = refreshed != self._refreshed, refreshed
        for cache, held in ((self.atlas, self._pages), (self.blurs, self._blur_pages),
                            (self.distances, self._distance_pages)):
            if cache is not None:
                dropped = cache.collect(alive, refresh)
                held[:] = [handle for page, handle in enumerate(held) if page not in dropped]
        for path in self._built.keys() - alive:
            del self._built[path]
        self.device.refresh()
        return shown[1], left

    def _step(self, job) -> int:
        key = _frozen(job)
        if key not in self._steps:
            self._steps[key] = self.device.scene.clear(*job) if isinstance(job, tuple) else self.device.scene.draw(job)
        return self._steps[key]

    def _drawn(self, node, state: tuple, jobs: Callable[[], list]) -> tuple[list[int], str | None]:
        state = (node.render_properties, node.world_matrix, node.color_scale, node.color_offset, node.saturation,
                 self._view_key, *state)
        held = self._built.get(node.path)
        if held is None or held[0] != state:
            self._slot = [node.path, 0]
            try:
                held = (state, [self._step(job) for job in jobs()], None)
            except (TextLayoutGap, MaterialGap, FileNotFoundError, GuiAssetError, TranslatedError) as gap:
                held = (state, [], str(gap))
            finally:
                self._slot = None
            self._built[node.path] = held
        return held[1], held[2]

    def forget(self) -> None:
        self._built.clear()
        self._sizes.clear()
        self._text = None
