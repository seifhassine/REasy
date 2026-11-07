"""See docs/internals/user-render-targets.md."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.build_catalog import catalog_file_name, catalog_reader
lazy from file_handlers.rtex import RtexFormatError, read_rtex
lazy from file_handlers.sdf.models import program_names
lazy from file_handlers.tex.dxgi import DXGI_FORMAT_R32_UINT
lazy from utils.resource_file_utils import normalize_resource_path

lazy from .engine_buffers import CAMERA_BUFFERS, PLACEMENT_BUFFERS, Placement, engine_buffer
lazy from .frame_clock import follows_clock
lazy from .material_preview import MaterialPreviewError, _heap_fields_kept, shader_bindings
lazy from .mesh_draw import program_states
lazy from .mesh_render import _not_drawn
lazy from .pretransform import constant_buffer as chain_constants
lazy from .resident import RENDER_TARGET, UNORDERED_ACCESS

CATALOG_FORMAT = 2
CATALOG_DIRECTORY = "resources/data/user_render_targets"
_EXTENSION = ".rtex"


class UserRenderTargetCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RenderTargetComponent:
    """Each field but ``drawn_material`` (the number of the MDF file's material drawn) holds the component's serialized
    field name."""
    type: str
    target: str
    material: str
    drawn_material: int
    parameters: str


@dataclass(frozen=True, slots=True)
class UserRenderTargetCatalog:
    game: str
    resource_version: int
    component: RenderTargetComponent
    dimensions: frozenset[int]
    refused: tuple[str, ...]
    compute: str
    asynchronous: bool
    threads: tuple[int, int]
    info: str
    info_fields: Mapping[str, str]
    result: str
    pixel: str
    vertices: int


def catalog_from_dict(data: Mapping) -> UserRenderTargetCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise UserRenderTargetCatalogError(f"user render target catalog format {data.get('format')!r} is not "
                                           f"{CATALOG_FORMAT}")
    component, texture, compute, pixel = data["component"], data["texture"], data["compute"], data["pixel"]
    fields = {str(name): str(variable) for name, variable in compute["info_fields"].items()}
    if set(fields) != {"size", "inverse_size"}:
        raise UserRenderTargetCatalogError("the size buffer's fields are not the size and its inverse")
    threads = tuple(int(item) for item in compute["threads"])
    if len(threads) != 2 or min(threads) < 1:
        raise UserRenderTargetCatalogError("a group's threads are not two counts")
    return UserRenderTargetCatalog(
        str(data["game"]), int(data["resource_version"]),
        RenderTargetComponent(str(component["type"]), str(component["target"]), str(component["material"]),
                              int(component["drawn_material"]), str(component["parameters"])),
        frozenset(int(item) for item in texture["dimensions"]), tuple(str(name) for name in texture["refused"]),
        str(compute["program"]), bool(compute["asynchronous"]),
        threads, str(compute["info"]), fields, str(compute["result"]), str(pixel["program"]), int(pixel["vertices"]))


user_render_target_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, UserRenderTargetCatalogError,
                                                "user render target", (AttributeError,))


@dataclass(frozen=True, slots=True)
class RenderTexture:
    form: int
    size: tuple[int, int]
    target: bool
    unordered: bool


def read_render_texture(data: bytes, catalog: UserRenderTargetCatalog) -> RenderTexture:
    try:
        texture = read_rtex(data)
    except RtexFormatError as exc:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The render texture file is not one REasy reads.")) from exc
    if texture.fields["Dimension"] not in catalog.dimensions:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The render texture is of a texture type REasy does not draw into."))
    if (name := next(filter(texture.set, catalog.refused), None)) is not None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The render texture sets {field}, which REasy does not draw into."), {"field": name})
    return RenderTexture(texture.form, texture.size, texture.set("RenderTargetView"),
                         texture.set("UnorderedAccessView"))


def is_render_texture(path: str) -> bool:
    name = normalize_resource_path(path).rsplit("/", 1)[-1].casefold()
    return name.endswith(_EXTENSION) or f"{_EXTENSION}." in name


def info_constants(texture: RenderTexture, catalog: UserRenderTargetCatalog) -> dict[str, np.ndarray]:
    size = np.asarray(texture.size, np.float32)
    return {catalog.info_fields["size"]: size, catalog.info_fields["inverse_size"]: np.float32(1) / size}


def groups(texture: RenderTexture, catalog: UserRenderTargetCatalog) -> tuple[int, int, int]:
    return (-(-texture.size[0] // catalog.threads[0]), -(-texture.size[1] // catalog.threads[1]), 1)


@dataclass(frozen=True, slots=True)
class UserRenderTarget:
    key: str
    path: str
    texture: RenderTexture
    mmtr_path: str
    context: object
    material: object


class MeshSceneRenderTargets:
    def user_render_target(self, document, target: UserRenderTarget) -> None:
        """``document`` is the material's master."""
        catalog = user_render_target_catalog_for(self.profile)
        if catalog is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "REasy has no catalog of the render target modifiers of this game build."))
        texture = target.texture
        dispatched = catalog.asynchronous and texture.unordered
        if not dispatched and not texture.target:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The render texture has no render target view for the renderer's pixel program to "
                                   "draw into."))
        name, roles = (catalog.compute, ("compute",)) if dispatched else (catalog.pixel, ("vertex", "pixel"))
        sdf = document.sdf.targets[self.shader_target]
        names = program_names(sdf)
        program = sdf.programs[names.index(name)] if name in names else None
        if program is None or tuple(shader.role for shader in program.shaders if shader.present) != roles \
                or any(program.shader(role).spirv is not None for role in roles):
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The master has no Direct3D program {name}, which the renderer draws a render "
                                   "target modifier with."), {"name": name})
        contracts = {role: document.profile.shader_graph_profile.shader_contract(document, program, role)
                     for role in roles}
        if dispatched and tuple(contracts["compute"].thread_group_size[:2]) != catalog.threads:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The program {name} runs other groups than the renderer dispatches."),
                {"name": name})
        held = self.render_texture(target.path, texture.form, texture.size,
                                   RENDER_TARGET | UNORDERED_ACCESS if dispatched else RENDER_TARGET)
        info, placement = info_constants(texture, catalog), Placement(np.eye(4)[:3])

        def buffer(contract, resource) -> bytes | None:
            if resource.register_class == "b" and resource.name == catalog.info:
                return chain_constants(contract.buffer(resource.name), info)
            if resource.name in CAMERA_BUFFERS or resource.name in PLACEMENT_BUFFERS:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The program {name} reads {buffer}, which no capture shows the renderer giving "
                                       "a render target modifier."), {"name": name, "buffer": resource.name})
            return engine_buffer(contract, resource, self.camera, placement, self.frame, self.captured,
                                 clock=self.clock)
        shaders = {role: (contracts[role], bytes(program.shader(role).payload)) for role in roles}
        tables, heap, missing = shader_bindings(
            document, shaders, target.material, self.load_file, buffer, self._sampler,
            read_textures=self.read_textures, read_buffers=self.read_buffers, heap_start=self._heap_start,
            made_texture=self.made_texture)
        self._follow_clock(tables, [(contracts[role], resource) for role in roles
                                    for resource in contracts[role].resources], buffer)
        self._read_clock(program)
        resources = [resource for role in roles for resource in contracts[role].resources]
        for index, resource in enumerate(resources):
            if resource.register_class == "u":
                if not dispatched or resource.name != catalog.result:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The program {name} writes {resource}, which the renderer does not bind."),
                        {"name": name, "resource": resource.name})
                tables[index] = (*tables[index][:-1], ((0, texture.form, held),))
        self._took(heap, missing)
        width, height = texture.size
        try:
            if dispatched:
                job = self.device.bound({"compute_shader": shaders["compute"][1], "tables": tables, "heap": heap})
                self._user_steps.append(self.device.scene.dispatch(job | {"groups": groups(texture, catalog)}))
                return
            job = self.device.bound({
                "vertex_shader": shaders["vertex"][1], "pixel_shader": shaders["pixel"][1], "vertex_buffers": [],
                "elements": [], "indices": self.device.buffer(np.arange(catalog.vertices, dtype="<u4").tobytes()),
                "index_format": DXGI_FORMAT_R32_UINT, "draw": (catalog.vertices, 1, 0, 0, 0), "tables": tables,
                "heap": heap, **program_states(program, 1, 0)})
            self._user_steps.append(self.device.scene.draw(job | {
                "size": (width, height), "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0),
                "scissor": (0, 0, width, height), "targets": [(texture.form, held)], "depth": None}))
        except RuntimeError as exc:
            raise _not_drawn(exc) from exc

    def _follow_clock(self, tables: list, declared: Sequence[tuple], make: Callable) -> None:
        for index, (contract, resource) in enumerate(declared):
            if resource.register_class != "b" or not follows_clock(self.clock, resource.name) \
                    or not tables[index][-1]:
                continue
            layout = contract.buffer(resource.name)
            *entry, bound = tables[index][-1][0]
            kind = ("clock", resource.name, layout.byte_size, layout.variables,
                    _heap_fields_kept(layout, bytes(len(bound)), bound))
            found = self.device.following(kind, lambda contract=contract, resource=resource, layout=layout,
                                          bound=bound: _heap_fields_kept(layout, make(contract, resource), bound))
            tables[index] = (*tables[index][:-1], ((*entry, found),))


__all__ = ["CATALOG_DIRECTORY", "CATALOG_FORMAT", "MeshSceneRenderTargets", "RenderTargetComponent", "RenderTexture",
           "UserRenderTarget", "UserRenderTargetCatalog", "UserRenderTargetCatalogError", "catalog_file_name",
           "catalog_from_dict", "groups", "info_constants", "is_render_texture", "read_render_texture",
           "user_render_target_catalog_for"]
