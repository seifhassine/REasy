"""See docs/internals/shell-fur.md."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import MISSING, dataclass, fields, replace
from functools import partial

lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.build_catalog import catalog_reader

lazy from file_handlers.mdf.mdf_file import BASE_ALPHA_TEST_ENABLE, BASE_TWO_SIDE_ENABLE
lazy from file_handlers.mmtr.material_preview import (
    VIEW_DIMENSIONS, MaterialPreviewError, _texture_entry, missing_texture,
)
lazy from file_handlers.mmtr.mesh_draw import SubmeshRange
lazy from file_handlers.mmtr.mesh_render import MeshObject, MeshPart, PartError, _Draw, _drawing, _part_geometry
lazy from file_handlers.mmtr.pass_choice import depth_prepass_program, gbuffer_program
lazy from file_handlers.mmtr.pretransform import (
    ChainLevel, ChainStep, Destination, SystemProgram, constant_buffer as chain_constants, span_constants,
)
lazy from file_handlers.mmtr.resident import UNORDERED_ACCESS
lazy from file_handlers.mmtr.surface_meaning import GBUFFER_PURPOSE
lazy from file_handlers.sdf.models import program_names

from .sfur_file import PARAMETER_FIELDS, PARAMETER_KINDS, FurParameter

CATALOG_FORMAT = 1
CATALOG_DIRECTORY = "resources/data/shell_fur"
SHELLS, SIMULATION = "shells", "simulation"
BUFFERS = ("source", "destination", SHELLS, SIMULATION)
_GEOMETRY_RULES = ("shells", "simulation", "count", "zero")
_PARAMETER_RULES = ("copy", "unit_line", "reciprocal_rest", "power")


def _parity_split(shells: int, bottom: bool) -> list[int]:
    kept = shells - bottom
    return [shells - 1 - 2 * place + (0 if 2 * place < kept else kept + (kept & 1) - 1)
            for place in range(kept)] + [0] * bottom


def _bottom_first(shells: int, bottom: bool) -> list[int]:
    return [0, *range(shells - 1, 0, -1)] if bottom and shells else list(range(shells - 1, -1, -1))


_ORDERS = {"outermost_first": (lambda shells, _bottom: list(range(shells - 1, -1, -1)), True),
           "bottom_first": (_bottom_first, False), "parity_split": (_parity_split, True)}
_INSTANCE_ORDERS = {"innermost_first": (lambda shells, _bottom: list(range(shells)), True),
                    "bottom_first": (_bottom_first, False),
                    "parity_split_reversed": (lambda shells, bottom: _parity_split(shells, bottom)[::-1], True)}
# A shell info word: 0, the shell, 0xffffffff, or one the renderer leaves as it was.
_INFO_WORDS = {"zero": 0, "shell": None, "none": 0xffffffff, "unwritten": 0}
_PREPASSES = ("every", "alpha_tested", "alpha_tested_until_opaque")
_CONVERT = {"str": lambda value: str(value or ""), "int": lambda value: int(value or 0),
            "float": lambda value: float(value or 0.0), "bool": bool}


class ShellFurCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParameterRule:
    field: str
    rule: str = "copy"
    start: float = 0.0
    scale: float = 1.0
    less: str | None = None
    exponent: int = 1


@dataclass(frozen=True, slots=True)
class ShellFurComponent:
    """Each field holds the component's serialized field name."""
    type: str
    resource: str
    parameters: str
    shown: tuple[str, ...]
    shell_type: str
    simulation: str
    lod_settings: str
    shell_count: str = ""
    count_scale: str = ""
    lod_limit: str = ""


@dataclass(frozen=True, slots=True)
class ShellSetup:
    program: SystemProgram
    texture: str
    buffers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ShellInstancing:
    programs: Mapping[str, str]
    shell_info: str
    info: tuple[str, ...]
    orders: Mapping[int, str]
    texture: str


@dataclass(frozen=True, slots=True)
class ShellFurCatalog:
    game: str
    resource_version: int
    component: ShellFurComponent
    parameter_fields: Mapping[str, str]
    shading_type: int
    shell_types: frozenset[int]
    simulations: frozenset[int]
    orders: Mapping[int, str]
    programs: Mapping[str, str]
    threads: int
    buffers: Mapping[str, str]
    skinning_buffer: str
    geometry_buffer: str
    parameter_buffer: str
    # The shells' draw programs are the master's of this prefix and the base draw's name.
    draw_prefix: str
    shells: tuple[tuple[str, int], ...]
    simulation: tuple[tuple[str, int], ...]
    views: Mapping[str, str]
    geometry: Mapping[str, tuple[str, str | None]]
    parameters: Mapping[str, ParameterRule]
    unread: frozenset[str]
    setup: ShellSetup | None
    instancing: ShellInstancing
    default_texture: str
    lod_count: int
    prepass: str

    def files(self) -> tuple[str, ...]:
        return () if self.setup is None else (self.setup.program.file,)

    def offsets(self, blocks: Sequence[tuple[str, int]], total: int) -> dict[str, tuple[int, int]]:
        found, place = {}, 0
        for name, size in blocks:
            found[name] = place, size
            place += size * total
        return found


def _blocks(value) -> tuple[tuple[str, int], ...]:
    blocks = tuple((str(name), int(size)) for name, size in value)
    if len({name for name, _size in blocks}) != len(blocks) or any(size <= 0 for _name, size in blocks):
        raise ShellFurCatalogError("a buffer's blocks repeat a name or hold no bytes")
    return blocks


def catalog_from_dict(data: Mapping) -> ShellFurCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise ShellFurCatalogError(f"shell fur catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    component = data["component"]
    names = {str(key): str(value) for key, value in data["parameter_fields"].items()}
    if not {item.name for item in fields(FurParameter) if item.default is MISSING} <= set(names) <= set(
            PARAMETER_FIELDS):
        raise ShellFurCatalogError("the parameter fields are not named for every shell setting")
    compute, setup = data["compute"], data["setup"]
    buffers = {str(name): str(kind) for name, kind in compute["buffers"].items()}
    setup_buffers = {} if setup is None else {str(name): str(kind) for name, kind in setup["buffers"].items()}
    if not {*buffers.values(), *setup_buffers.values()} <= set(BUFFERS):
        raise ShellFurCatalogError(f"a compute buffer is none of {', '.join(BUFFERS)}")
    orders = {int(kind): str(order) for kind, order in compute["orders"].items()}
    if not set(orders.values()) <= set(_ORDERS):
        raise ShellFurCatalogError(f"a shell order is none of {', '.join(_ORDERS)}")
    shells, simulation = _blocks(data["shells"]), _blocks(data["simulation"])
    held = {name for name, _size in shells}
    views = {str(name): str(block) for name, block in data["views"].items()}
    if not set(views.values()) <= held:
        raise ShellFurCatalogError("a vertex input reads a block the shell buffer does not hold")
    geometry = {str(name): (str(rule[0]), None if len(rule) < 2 else str(rule[1]))
                for name, rule in data["geometry"].items()}
    for rule, block in geometry.values():
        if rule not in _GEOMETRY_RULES or (rule == "shells" and block not in held) or (
                rule == "simulation" and block not in {name for name, _size in simulation}):
            raise ShellFurCatalogError(f"a geometry field's rule {rule} {block} is not one REasy makes")
    parameters = {str(name): ParameterRule(str(rule["field"]), str(rule.get("rule", "copy")),
                                           float(rule.get("start", 0.0)), float(rule.get("scale", 1.0)),
                                           rule.get("less"), int(rule.get("exponent", 1)))
                  for name, rule in data["parameters"].items()}
    if any(rule.rule not in _PARAMETER_RULES or rule.field not in PARAMETER_FIELDS or rule.exponent < 1
           or rule.exponent & (rule.exponent - 1) or (rule.less is not None and rule.less not in parameters)
           for rule in parameters.values()):
        raise ShellFurCatalogError("a parameter field's rule is not one REasy makes")
    instancing = data["instancing"]
    instanced_orders = {int(kind): str(order) for kind, order in instancing["orders"].items()}
    if not set(instanced_orders.values()) <= set(_INSTANCE_ORDERS):
        raise ShellFurCatalogError(f"an instanced shell order is none of {', '.join(_INSTANCE_ORDERS)}")
    if not set(instancing["info"]) <= set(_INFO_WORDS) or list(instancing["info"]).count("shell") != 1:
        raise ShellFurCatalogError(f"a shell info word is none of {', '.join(_INFO_WORDS)}, or not one is the shell")
    if int(data["lod_count"]) < 2:
        raise ShellFurCatalogError("the shell table has fewer than two LODs")
    if data["prepass"] not in _PREPASSES:
        raise ShellFurCatalogError(f"the shells' depth pre-pass rule is none of {', '.join(_PREPASSES)}")
    return ShellFurCatalog(
        str(data["game"]), int(data["resource_version"]),
        ShellFurComponent(str(component["type"]), str(component["resource"]), str(component["parameters"]),
                          tuple(str(name) for name in component["shown"]), str(component["shell_type"]),
                          str(component["simulation"]), str(component["lod_settings"]),
                          str(component.get("shell_count", "")), str(component.get("count_scale", "")),
                          str(component.get("lod_limit", ""))),
        names, int(data["shading_type"]), frozenset(int(item) for item in compute["shell_types"]),
        frozenset(int(item) for item in compute["simulations"]), orders,
        {str(kind): str(name) for kind, name in compute["programs"].items()}, int(compute["threads"]), buffers,
        str(compute["skinning_buffer"]), str(compute["geometry_buffer"]), str(compute["parameter_buffer"]),
        str(data["draw_prefix"]), shells, simulation, views, geometry, parameters,
        frozenset(str(name) for name in data["unread"]),
        None if setup is None else ShellSetup(SystemProgram(str(setup["file"]), str(setup["program"])),
                                              str(setup["texture"]), setup_buffers),
        ShellInstancing({str(kind): str(suffix) for kind, suffix in instancing["programs"].items()},
                        str(instancing["shell_info"]), tuple(str(word) for word in instancing["info"]),
                        instanced_orders, str(instancing["texture"])),
        str(data["default_texture"]), int(data["lod_count"]), str(data["prepass"]))


shell_fur_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, ShellFurCatalogError, "shell fur",
                                       (AttributeError,))


def shell_fur_catalog(profile) -> ShellFurCatalog:
    if (catalog := shell_fur_catalog_for(profile)) is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "REasy has no catalog of the shell fur passes of this game build."))
    return catalog


def component_parameters(entries: Iterable[Mapping], catalog: ShellFurCatalog) -> tuple[FurParameter, ...]:
    return tuple(FurParameter(**{name: _CONVERT[PARAMETER_KINDS[name]](entry.get(serialized))
                                 for name, serialized in catalog.parameter_fields.items()}) for entry in entries)


def named_parameters(resource: Sequence[FurParameter], component: Sequence[FurParameter]) -> dict[str, FurParameter]:
    found: dict[str, FurParameter] = {}
    for item in (*resource, *component):
        found[item.material] = item
    return found


@dataclass(frozen=True, slots=True)
class ShellRecord:
    submesh: SubmeshRange
    count: int
    start: int
    shells: int
    parameter: FurParameter
    material: int = -1

    @property
    def drawn(self) -> bool:
        return self.shells > 0 and not self.parameter.disable

    def order(self, orders: Mapping[int, str], rules: Mapping) -> tuple[list[int], bool]:
        rule = orders.get(self.parameter.thin_type)
        if rule is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "No capture shows the order the renderer draws shells of thin type {kind} in."),
                {"kind": self.parameter.thin_type})
        made, last = rules[rule]
        return made(self.shells, self.parameter.bottom_save), last

    def bases(self, catalog: ShellFurCatalog) -> list[int]:
        return [self.start + shell * self.count for shell in self.order(catalog.orders, _ORDERS)[0]]

    def infos(self, catalog: ShellFurCatalog) -> bytes:
        shells = self.order(catalog.instancing.orders, _INSTANCE_ORDERS)[0]
        words = np.array([_INFO_WORDS[word] or 0 for word in catalog.instancing.info] * self.shells, "<u4").reshape(
            self.shells, -1)
        words[:, catalog.instancing.info.index("shell")] = shells
        return words.tobytes()


def thinned(shells: int, lod, count: int, bottom_save: bool, limit: int | None = None) -> int:
    f = np.float32
    lod = f(lod)
    steps = count - 2 if limit is None else min(count - 2, limit)
    start = f(1) if limit is None else f(steps > 0)
    if start < lod:
        share = f(lod - start) / f(max(steps, 1))
        share = share if f(0) <= share else f(0)
        share = f(1) if f(1) <= share else share
        shells = int(np.ceil(f(f(1) - share) * f(shells)))
    return 1 if bottom_save and not shells else shells


@dataclass(frozen=True, slots=True)
class ShellLods:
    """Each frame's shell LOD inputs: ``scale`` -1 over the mesh box's radius, thresholds from LOD 1, ``level`` -1
    for the view's."""
    centre: tuple[float, float, float]
    scale: float
    mesh: tuple[float, ...]
    shells: tuple[float, ...]
    level: int = -1
    over_p00: bool = True
    lod_limit: int | None = None


@dataclass(frozen=True, slots=True)
class ShellFurObject:
    records: tuple[ShellRecord, ...]
    program: str | None
    total: int
    lods: tuple[tuple[ShellRecord, ...], ...] = ()
    levels: ShellLods | None = None

    def record(self, submesh: SubmeshRange) -> ShellRecord | None:
        return next((item for item in self.records if item.submesh == submesh), None)

    def at(self, lod: int) -> ShellFurObject | None:
        records = self.records if not lod else self.lods[lod - 1] if lod <= len(self.lods) else ()
        return replace(self, records=records, lods=()) if records else None


def shell_records(spans: Iterable[tuple[SubmeshRange, int, str, int, int]], parameters: Mapping[str, FurParameter],
                  catalog: ShellFurCatalog, lod: int = 0) -> tuple[ShellRecord, ...]:
    records, start = [], 0
    for submesh, count, name, shading, material in spans:
        parameter = parameters.get(name, parameters.get(""))
        if shading != catalog.shading_type or parameter is None or not parameter.shell_count:
            continue
        shells = thinned(parameter.shell_count, lod, catalog.lod_count, parameter.bottom_save)
        if not shells:
            continue
        records.append(ShellRecord(submesh, count, start, shells, parameter, material))
        start += shells * count
    return tuple(records)


def _float(value) -> np.float32:
    return np.float32(value)


def parameter_constants(parameter: FurParameter, catalog: ShellFurCatalog) -> dict[str, np.ndarray]:
    made: dict[str, np.generic] = {}

    def value(name: str) -> np.generic:
        if name in made:
            return made[name]
        rule = catalog.parameters[name]
        raw = getattr(parameter, rule.field)
        if rule.rule == "copy":
            found = np.uint32(raw) if isinstance(raw, (bool, int)) else _float(raw)
        elif rule.rule == "unit_line":
            line = _float(1.0) - (_float(raw) - _float(rule.start)) * _float(rule.scale)
            found = np.clip(line, _float(0.0), _float(1.0))
            if rule.less is not None:
                found = found - value(rule.less)
        elif rule.rule == "reciprocal_rest":
            found = _float(0.0) if _float(raw) >= _float(1.0) else _float(1.0) / (_float(1.0) - _float(raw))
        else:
            found = _float(raw)
            for _turn in range(rule.exponent.bit_length() - 1):
                found = found * found
        made[name] = found
        return found
    return {name: np.array([value(name)]) for name in catalog.parameters}


def instanced_constants(parameter: FurParameter, catalog: ShellFurCatalog) -> dict[str, np.ndarray]:
    return {**{name: np.zeros(1, "<u4") for name in catalog.unread}, **parameter_constants(parameter, catalog)}


def geometry_constants(record: ShellRecord, total: int, catalog: ShellFurCatalog) -> dict[str, np.ndarray]:
    places = {"shells": catalog.offsets(catalog.shells, total),
              "simulation": catalog.offsets(catalog.simulation, total)}
    found = {}
    for name, (rule, block) in catalog.geometry.items():
        if rule in places:
            offset, size = places[rule][block]
            found[name] = offset + record.start * size
        else:
            found[name] = record.shells if rule == "count" else 0
    return {name: np.array([value], "<u4") for name, value in found.items()}


def shell_steps(fur: ShellFurObject, level: ChainLevel, destination: Destination,
                catalog: ShellFurCatalog) -> list[tuple[ShellRecord, ChainStep, ChainStep]]:
    if not level.coloured:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "No capture shows the colours the shell setup reads for a mesh without vertex colours."))
    unread = {name: np.zeros(1, "<u4") for name in catalog.unread}
    read = frozenset({catalog.skinning_buffer, catalog.geometry_buffer, catalog.parameter_buffer})
    steps = []
    for record in fur.records:
        if not record.drawn:
            continue
        constants = {**unread, **span_constants(level, destination, record.submesh.base_vertex, record.count),
                     **geometry_constants(record, fur.total, catalog), **parameter_constants(record.parameter, catalog)}
        groups = -(-record.count // catalog.threads)
        steps.append((record, ChainStep(catalog.setup.program, None, constants, dict(catalog.setup.buffers), groups,
                                        constant_buffers=read),
                      ChainStep(fur.program, None, constants, dict(catalog.buffers), groups, constant_buffers=read)))
    return steps


def shell_views(catalog: ShellFurCatalog) -> dict[tuple[str, int], tuple[int, int]]:
    places = catalog.offsets(catalog.shells, 1)
    found = {}
    for name, block in catalog.views.items():
        semantic = name.rstrip("0123456789").upper()
        index = int(name[len(semantic):] or 0)
        before, size = places[catalog.views["POSITION0"] if (semantic, index) == ("POSITION", 1) else block]
        found[semantic, index] = size, before
    return found


def shell_buffer_sizes(catalog: ShellFurCatalog, total: int) -> tuple[int, int]:
    return (sum(size for _name, size in catalog.shells) * total,
            sum(size for _name, size in catalog.simulation) * total)


def shell_arguments(fur: ShellFurObject, drawn: Sequence[int], catalog: ShellFurCatalog) -> tuple[list[int], bytes]:
    rows, offsets = [], []
    instanced = fur.program is None
    orders, rules = (catalog.instancing.orders, _INSTANCE_ORDERS) if instanced else (catalog.orders, _ORDERS)
    for record, kept in zip(fur.records, drawn):
        offsets.append(20 * len(rows))
        count = record.submesh.count
        if not record.drawn or record.parameter.thin_type not in orders:
            rows += [(count, 0, 0, record.submesh.base_vertex, 0)] * (1 if instanced else record.shells)
        elif instanced:
            thins = record.order(orders, rules)[1]
            rows.append((count, kept if thins else record.shells, 0, record.submesh.base_vertex, 0))
        else:
            shells, last = record.order(orders, rules)
            rows += [(count, int(place >= record.shells - kept if last else place < kept), 0,
                      record.start + shell * record.count, 0) for place, shell in enumerate(shells)]
    return offsets, np.array(rows, "<i4").reshape(-1, 5).tobytes()


class MeshSceneShells:
    def _shell_draws(self, owner: int, item: MeshObject, part: MeshPart, purpose: str, program, prepass,
                     geometry: dict) -> list[_Draw]:
        fur = item.shell_fur
        record = fur.record(part.submesh)
        if record is None or not record.drawn:
            return []
        catalog = shell_fur_catalog(self.profile)
        if purpose != GBUFFER_PURPOSE:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "No capture shows the renderer drawing shells of a part it does not draw in the "
                                   "G-buffer."))
        if record.parameter.force_two_side and not self._program_flags(part.material) & BASE_TWO_SIDE_ENABLE:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "No capture shows the renderer drawing shells whose entry forces two-sided drawing "
                                   "on a material without it."))
        materials = {other.submesh: other.material for other in (*item.parts, *item.skins)}

        def flags(other: ShellRecord) -> int:
            if other.submesh not in materials:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "No capture shows the renderer drawing shells without the part of an earlier "
                                       "shell material, which gates their depth pre-pass."))
            forced = BASE_ALPHA_TEST_ENABLE * other.parameter.force_alpha_test
            return self._program_flags(materials[other.submesh]) | forced
        if flags(record) != self._program_flags(part.material):
            route, target = part.route or item.route, self.shader_target
            program = part.document.sdf.targets[target].programs[
                gbuffer_program(part.document, target, flags(record), route)]
            prepass = depth_prepass_program(part.document, target, flags(record), route)
            prepass = None if prepass is None else part.document.sdf.targets[target].programs[prepass]
        if catalog.prepass != "every":
            if not all(flags(other) & BASE_ALPHA_TEST_ENABLE
                       for other in fur.records if other.drawn and (other is record or (
                           catalog.prepass == "alpha_tested_until_opaque" and other.material < record.material))):
                prepass = None
        instanced, suffix = fur.program is None, ""
        if instanced:
            suffix = catalog.instancing.programs.get("skinned" if item.skinned else "static")
            if suffix is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "No capture shows the programs the renderer draws this mesh's shells with."))
            record.infos(catalog)
        target = part.document.sdf.targets[self.shader_target]
        names = program_names(target)

        def shell(base):
            name = catalog.draw_prefix + base.name + suffix
            if name not in names:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The master has no program {name}, which the game draws the part's shells "
                                       "with."), {"name": name})
            return target.programs[names.index(name)]
        drawn = shell(program)
        placing = None
        if self._order is not None:
            # The part's pre-pass program is its master's ``ZPrePass`` one (``_placing``).
            placing = None if self._order.shell_fur is None else self._order.shell_fur.placing(part.material)
            if placing is None or prepass is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "REasy's G-buffer order catalog of this game build places no shells of the "
                                       "material, or its master has no depth pre-pass program."))
        prepass = None if prepass is None else shell(prepass)
        views = None if instanced else shell_views(catalog)
        for made in (drawn, prepass):
            if made is not None:
                key = id(part), id(made), id(None) if instanced else SHELLS, id(item.engine)
                if key not in self._built:
                    self._built[key] = _part_geometry(part, made, None, item.engine, views)
                geometry[id(part), id(made)] = self._built[key]
        start = shell_arguments(fur, [0] * len(fur.records), catalog)[0][fur.records.index(record)]
        if instanced:
            return [_Draw(owner, part, purpose, drawn, prepass, placing, None, start, record)]
        return [_Draw(owner, part, purpose, drawn, prepass, placing, None, start + 20 * number)
                for number, _base in enumerate(record.bases(catalog))]

    def thin(self, owner: int, drawn: Sequence[int]) -> None:
        self._shells_drawn[owner] = tuple(drawn)

    def _shell_bytes(self, owner: int) -> bytes:
        fur = self.objects[owner].shell_fur
        return shell_arguments(fur, self._shells_drawn.get(owner) or [record.shells for record in fur.records],
                               shell_fur_catalog(self.profile))[1]

    def _shell(self, owner: int, item: MeshObject) -> None:
        parts = {part.submesh: part for part in (*item.parts, *item.skins)}
        fur, deformation = item.shell_fur, item.deformation
        try:
            catalog = shell_fur_catalog(self.profile)
            steps = shell_steps(fur, deformation.level, deformation.destination, catalog)
        except MaterialPreviewError as exc:
            self._refuse(PartError([(part, exc) for submesh, part in parts.items() if fur.record(submesh)]))
            return
        sizes, scene = shell_buffer_sizes(catalog, fur.total), self.device.scene
        held = {**self._chains.held(owner), **{role: scene.buffer(bytes(size), UNORDERED_ACCESS)
                                               for role, size in zip((SHELLS, SIMULATION), sizes) if size}}
        made = []
        for record, setup, step in steps:
            part = parts.get(record.submesh)
            if part is None:
                continue
            with self._refusing(), _drawing(part):
                made += [self._chains.system(owner, setup, held, partial(self._grooming, catalog, record),
                                             self._sampler),
                         self._skin(owner, part, step, held)]
        self._shells[owner] = held[SHELLS], made
        self._stale.add(owner)

    def _grooming(self, catalog, record, resource) -> tuple | None:
        if resource.name != catalog.setup.texture:
            return None
        return self.device.read(VIEW_DIMENSIONS[resource.dimension], self._grooming_entry(catalog, record, resource))

    def _fur_input(self, record: ShellRecord, contract, resource) -> bytes | None:
        catalog = shell_fur_catalog_for(self.profile)
        if resource.register_class == "b" and resource.name == catalog.parameter_buffer:
            return chain_constants(contract.buffer(resource.name), instanced_constants(record.parameter, catalog))
        if resource.register_class == "t" and resource.name == catalog.instancing.shell_info:
            return record.infos(catalog)
        return None

    def _fur_texture(self, record: ShellRecord, texture: Callable | None, contract, resource) -> tuple | None:
        catalog = shell_fur_catalog_for(self.profile)
        if resource.name == catalog.instancing.texture:
            return self._grooming_entry(catalog, record, resource)
        return None if texture is None else texture(contract, resource)

    def _grooming_entry(self, catalog, record: ShellRecord, resource) -> tuple:
        """Whole: the programs read its first mip."""
        path = record.parameter.grooming_texture or catalog.default_texture
        entry = _texture_entry(0, path, self.load_file, resource.dimension, self.read_textures, whole=True)
        if entry is None:
            raise missing_texture(path)
        return entry


__all__ = ["BUFFERS", "CATALOG_DIRECTORY", "CATALOG_FORMAT", "MeshSceneShells", "ParameterRule", "SHELLS", "SIMULATION",
           "ShellFurCatalog", "ShellFurCatalogError", "ShellFurComponent", "ShellFurObject", "ShellInstancing",
           "ShellLods", "ShellRecord", "ShellSetup", "catalog_from_dict", "component_parameters", "geometry_constants",
           "instanced_constants", "named_parameters", "parameter_constants", "shell_arguments", "shell_buffer_sizes",
           "shell_fur_catalog", "shell_fur_catalog_for", "shell_records", "shell_steps", "shell_views", "thinned"]
