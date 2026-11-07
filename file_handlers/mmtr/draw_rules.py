"""See docs/internals/mesh-preview.md, Draw rules catalog."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.build_catalog import catalog_file_name, catalog_reader
lazy from file_handlers.mdf.mdf_file import BASE_ALPHA_TEST_ENABLE, BASE_TWO_SIDE_ENABLE, material_flag_layout
lazy from file_handlers.mmtr.material_preview import MaterialPreviewError
lazy from file_handlers.mesh.lod_settings import lod_settings_file
lazy from utils.hash_util import murmur3_hash_utf16le

CATALOG_FORMAT = 3
CATALOG_DIRECTORY = "resources/data/draw_rules"
DECAL_RULES = ("always", "never", "unskinned")
MESH_COMPONENT = "via.render.Mesh"
MESH_DECAL_RECEIVE = (MESH_COMPONENT, None)
MARKERS = ("A", "TS")


class DrawRulesError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StencilTerm:
    component: tuple[str, ...] = ()
    material: str | None = None
    value: str | None = None
    shift: int = 0
    bits: int = 0
    setting: str | None = None


@dataclass(frozen=True, slots=True)
class DecalRule:
    field: str | None
    enum: str | None
    members: Mapping[str, str]
    otherwise: str


@dataclass(frozen=True, slots=True)
class MaterialParams:
    list: str
    material: str | None
    variable: str
    scalar: str
    vector: str
    texture: str | None
    buffer: str | None
    scalar_components: int
    vector_components: int
    locked_kept: bool
    own: bool = True


_PARAM_FIELDS = ("list", "variable", "scalar", "vector")
LOD_MODES = ("automatic", "manual", "follow_parent")


@dataclass(frozen=True, slots=True)
class MeshLodRule:
    mode: str
    modes: Mapping[str, int]
    level: str
    range_enabled: str
    range: str
    lod_settings: str
    lod_settings_version: int
    rate: float
    rate2: float
    size: float
    exponent: float
    offset: int
    depth_over_p00: bool

    def file(self) -> str:
        return lod_settings_file(self.lod_settings, self.lod_settings_version)


@dataclass(frozen=True, slots=True)
class SmallObjectCulling:
    ratio: float
    cpu: Mapping[str, str]
    gpu: frozenset[str]
    gpu_bounds: float
    boxes: Mapping[str, str]


BOX_METHODS = frozenset({"corners", "oriented_box"})


@dataclass(frozen=True, slots=True)
class DrawRules:
    game: str
    resource_version: int
    masked: Mapping[str, str]
    derived: Mapping[str, tuple[str, ...]]
    markers: tuple[tuple[str, str], ...]
    decal_receive: Mapping[str, DecalRule]
    stencil: Mapping[str, tuple[StencilTerm, ...]]
    component: Mapping[str, Mapping[str, object]]
    settings: Mapping[str, object] = field(default_factory=dict)
    material_params: Mapping[str, MaterialParams] = field(default_factory=dict)
    lods: Mapping[str, MeshLodRule] = field(default_factory=dict)
    small_objects: SmallObjectCulling | None = None

    def runtime_flags(self, flags: int, version: int) -> int:
        fields = {item.name: item for item in material_flag_layout(version)}
        flags = int(flags)
        for name, mask in self.masked.items():
            if name in fields:
                kept = fields[mask].get(flags) if mask in fields else 0
                flags = fields[name].set(flags, fields[name].get(flags) & kept)
        for name, sources in self.derived.items():
            if name in fields:
                flags = fields[name].set(flags, any(fields[item].get(flags) for item in sources if item in fields))
        return flags

    def flags_by_name(self, flags: int, version: int) -> dict[str, int]:
        runtime = self.runtime_flags(flags, version)
        return {item.name: item.get(runtime) for item in material_flag_layout(version)}

    def program_flags(self, flags: int, version: int) -> int:
        named = self.flags_by_name(flags, version)
        bits = {"A": BASE_ALPHA_TEST_ENABLE, "TS": BASE_TWO_SIDE_ENABLE}
        return sum(bits[marker] for marker, name in self.markers if named.get(name))

    def receives_decals(self, mode: tuple[str, str | None], skinned: bool) -> bool:
        component, member = mode
        rule = self.decal_receive.get(component)
        if rule is None:
            raise DrawRulesError(f"the draw rules of {self.game} have no decal rule for {component}")
        found = rule.members.get(member, rule.otherwise)
        return found == "always" or found == "unskinned" and not skinned

    def value_of(self, fields: Mapping[str, object] | None, name: str, component: str = MESH_COMPONENT):
        return self.component[component][name] if fields is None or name not in fields else fields[name]

    def overridden(self, surface, component: str, fields: Mapping[str, object] | None):
        params = self.material_params.get(component)
        entries = () if params is None or fields is None else fields.get(params.list, ())
        material = (surface.material_name, murmur3_hash_utf16le(surface.material_name)) if entries else ()
        entries = [entry for entry in entries if params.material not in entry or entry[params.material] in material]
        if not entries:
            return surface
        held = {item.name: item for item in surface.parameters}
        slots = {item.texture_type: item for item in surface.textures}
        held_buffers = {} if params.buffer is None else {item.name: item for item in surface.gpu_buffers}
        values = {name: item.values for name, item in held.items()}
        textures = {name: item.texture_path for name, item in slots.items()}
        buffers = {name: item.path for name, item in held_buffers.items()}
        hashed = {} if all(isinstance(entry.get(params.variable), str) for entry in entries) else {
            murmur3_hash_utf16le(name): name for name in (*held, *slots, *held_buffers)}
        for entry in entries:
            name = hashed.get(entry.get(params.variable), entry.get(params.variable))
            found = held.get(name) or slots.get(name) or held_buffers.get(name)
            if found is None or params.locked_kept and found.locked:
                continue
            if name in held:
                if len(values[name]) == params.scalar_components and params.scalar in entry:
                    values[name] = (float(entry[params.scalar]),)
                elif len(values[name]) == params.vector_components and params.vector in entry:
                    values[name] = tuple(old if new is None else float(new)
                                         for old, new in zip(values[name], entry[params.vector]))
            elif name in slots:
                if params.texture is not None:
                    textures[name] = entry.get(params.texture) or slots[name].texture_path
            else:
                buffers[name] = entry.get(params.buffer) or held_buffers[name].path
        parameters = tuple(item if values[item.name] is item.values else replace(item, values=values[item.name])
                           for item in surface.parameters)
        return replace(surface, parameters=parameters,
                       textures=tuple(item if textures[item.texture_type] is item.texture_path
                                      else replace(item, texture_path=textures[item.texture_type])
                                      for item in surface.textures),
                       gpu_buffers=tuple(item if buffers.get(item.name, item.path) is item.path
                                         else replace(item, path=buffers[item.name]) for item in surface.gpu_buffers),
                       texture_path=textures.get(surface.texture_type, surface.texture_path))

    def stencil_reference(self, component: str, fields: Mapping[str, object] | None,
                          materials: Iterable[tuple[int, int]]) -> int:
        if component not in self.stencil:
            component, fields = MESH_COMPONENT, None
        named = [self.flags_by_name(flags, version) for flags, version in materials]
        reference = 0
        for term in self.stencil[component]:
            if not all(self.value_of(fields, name, component) for name in term.component):
                continue
            if term.setting is not None and not self.settings[term.setting]:
                continue
            if term.material is not None and not any(item.get(term.material) for item in named):
                continue
            value = 0 if term.value is None else int(self.value_of(fields, term.value, component)) & 0xFF
            reference |= term.bits | value << term.shift
        return reference & 0xFF


def catalog_from_dict(data: Mapping) -> DrawRules:
    if data.get("format") != CATALOG_FORMAT:
        raise DrawRulesError(f"draw rules catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    flags = data["material_flags"]
    markers = tuple((str(marker), str(name)) for marker, name in data["program_markers"])
    if not {marker for marker, _name in markers} <= set(MARKERS):
        raise DrawRulesError(f"program markers are not of {', '.join(MARKERS)}")
    decals = {str(component): _decal_rule(rule) for component, rule in data["decal_receive"].items()}
    if MESH_COMPONENT not in decals:
        raise DrawRulesError(f"decal receive rules have none for {MESH_COMPONENT}")
    stencil = {str(kind): tuple(StencilTerm(tuple(str(name) for name in term.get("component", ())),
                                            term.get("material"), term.get("value"), int(term.get("shift", 0)),
                                            int(term.get("bits", 0)), term.get("setting"))
                                for term in terms)
               for kind, terms in data["stencil"].items()}
    component = {str(kind): dict(defaults) for kind, defaults in data["component"].items()}
    settings = dict(data.get("settings", {}))
    if MESH_COMPONENT not in stencil or MESH_COMPONENT not in component:
        raise DrawRulesError(f"the stencil terms or component defaults have none for {MESH_COMPONENT}")
    for kind, terms in stencil.items():
        read = {name for term in terms for name in (*term.component, *(() if term.value is None else (term.value,)))}
        missing = read - set(component.get(kind, ()))
        if missing:
            raise DrawRulesError(f"{kind}'s stencil terms read fields without a default: {', '.join(sorted(missing))}")
        if not {term.setting for term in terms if term.setting is not None} <= set(settings):
            raise DrawRulesError(f"{kind}'s stencil terms read a setting the catalog does not give")
    params = {str(kind): _material_params(value) for kind, value in (data.get("material_params") or {}).items()}
    lods = {str(kind): _lod_rule(value) for kind, value in (data.get("lods") or {}).items()}
    for kind, rule in lods.items():
        missing = {rule.mode, rule.level, rule.range_enabled} - {""} - set(component.get(kind, ()))
        if missing:
            raise DrawRulesError(f"{kind}'s LOD rule reads fields without a default: {', '.join(sorted(missing))}")
    small = data.get("small_object_culling")
    if small is not None:
        small = SmallObjectCulling(float(small["ratio"]), {str(kind): str(name) for kind, name in small["cpu"].items()},
                                   frozenset(str(kind) for kind in small["gpu"]), float(small["gpu_bounds"]),
                                   {str(kind): str(method) for kind, method in small["boxes"].items()})
        missing = {kind for kind, name in small.cpu.items() if name not in component.get(kind, ())}
        if missing or small.cpu.keys() & small.gpu:
            raise DrawRulesError("the small object culling reads factor fields without a default or tests a type twice")
        if small.boxes.keys() != small.cpu.keys() | small.gpu or not set(small.boxes.values()) <= BOX_METHODS:
            raise DrawRulesError("the small object culling's boxes name a type it does not test, miss one, or name "
                                 "an unknown method")
    return DrawRules(str(data["game"]), int(data["resource_version"]),
                     {str(name): str(mask) for name, mask in flags["masked"].items()},
                     {str(name): tuple(str(item) for item in sources) for name, sources in flags["derived"].items()},
                     markers, decals, stencil, component, settings, params, lods, small)


def _lod_rule(data: Mapping) -> MeshLodRule:
    # A type without a mode field picks by the view alone.
    fields = [str(data.get(name) or "") for name in ("mode", "level", "range_enabled", "range")]
    modes = {str(name): int(value) for name, value in (data.get("modes") or {}).items()}
    if fields[0] and ("automatic" not in modes or not set(modes) <= set(LOD_MODES)):
        raise DrawRulesError(f"a LOD rule's modes are not of {', '.join(LOD_MODES)} with automatic")
    if not fields[0] and (modes or any(fields)):
        raise DrawRulesError("a LOD rule without a mode field names modes or other fields")
    return MeshLodRule(fields[0], modes, *fields[1:], str(data["lod_settings"]["path"]),
                       int(data["lod_settings"]["version"]), float(data["rate"]), float(data["rate2"]),
                       float(data["size"]), float(data["exponent"]), int(data["offset"]), bool(data["depth_over_p00"]))


def _material_params(data: Mapping) -> MaterialParams:
    named, optional = {name: str(data[name]) for name in _PARAM_FIELDS}, {
        name: None if data.get(name) is None else str(data[name]) for name in ("material", "texture", "buffer")}
    return MaterialParams(**named, **optional, scalar_components=int(data["scalar_components"]),
                          vector_components=int(data["vector_components"]), locked_kept=bool(data["locked_kept"]),
                          own=bool(data.get("own", True)))


def _decal_rule(data: Mapping) -> DecalRule:
    members = {str(name): str(rule) for name, rule in data.items() if name not in ("field", "enum", "otherwise")}
    rule = DecalRule(data.get("field"), data.get("enum"), members, str(data.get("otherwise")))
    if (rule.field is None) != (rule.enum is None) or not {*members.values(), rule.otherwise} <= set(DECAL_RULES):
        raise DrawRulesError(f"decal receive rules are not of {', '.join(DECAL_RULES)} with one for otherwise, and "
                             f"a field with its enum")
    return rule


draw_rules_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, DrawRulesError, "draw rules", (AttributeError,))


def drawing_rules(profile) -> DrawRules:
    rules = draw_rules_for(profile)
    if rules is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "REasy has no catalog of the draw rules of this game build."))
    return rules


def program_flags(profile, material) -> int:
    return drawing_rules(profile).program_flags(material.material_flags, material.flag_version)


def with_textures(surface, textures: Mapping[str, str]):
    if not textures.keys() & {item.texture_type for item in surface.textures}:
        return surface
    return replace(surface, textures=tuple(replace(item, texture_path=textures[item.texture_type])
                                           if item.texture_type in textures else item for item in surface.textures),
                   texture_path=textures.get(surface.texture_type, surface.texture_path))


__all__ = ["CATALOG_DIRECTORY", "CATALOG_FORMAT", "DECAL_RULES", "DecalRule", "DrawRules", "DrawRulesError",
           "LOD_MODES", "MARKERS", "MESH_COMPONENT", "MESH_DECAL_RECEIVE", "MaterialParams", "MeshLodRule",
           "SmallObjectCulling", "StencilTerm",
           "catalog_file_name", "catalog_from_dict", "draw_rules_for", "drawing_rules", "program_flags",
           "with_textures"]
