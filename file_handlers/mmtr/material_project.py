from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import json
import math
import re
from typing import Any, Callable
from uuid import uuid4

lazy from file_handlers.material_names import is_private_material_parameter
lazy from file_handlers.mdf.material_layout import MaterialDescriptorLayout
from file_handlers.sdf.contracts import (
    SdfContractError,
    material_buffer_contract,
    matches_outer_resource,
)
lazy from file_handlers.sdf.hlsl import HlslCompileError
lazy from file_handlers.sdf.models import STAGE_KEYS, SdfConstantBuffer, SdfConstantVariable
from file_handlers.sdf.shader_graph import (
    MAX_GRAPH_LINKS,
    MAX_GRAPH_NODES,
    MAX_PROJECT_LENGTH,
    ShaderContract,
    ShaderContractBuffer,
    ShaderContractResource,
    ShaderContractVariable,
    ShaderGraph,
    ShaderGraphDiagnostic,
    SHADER_GRAPH_STAGES,
    ShaderGraphError,
    connection_compatible,
    node_pins,
    node_property_diagnostics,
    node_title,
    validate_shader_contract,
)

lazy from .pass_purpose import pass_purpose, purpose_title
lazy from .pipeline_settings import PipelineSettingError, apply_pipeline_states, validate_pipeline_states
lazy from .material_function import (
    MaterialFunctionError,
    _expand_material_functions_for_compilation,
    material_function_from_node,
    validate_material_function_nodes,
)
from .material_implementation import (
    MaterialProgramImplementation, implementation_routes, compile_implementation, implementation_execution, implementation_contract,
    _cached_contract_fingerprint,
)
lazy from .surface_meaning import shader_bytes
lazy from .engine_inputs import RESOURCE_TABLES
lazy from .models import BINDLESS_PARAMETER_STORAGES
from .profiles.base import MmtrGameProfile, same_mmtr_profile
lazy from .material_family import family_member_project, build_material_family
lazy from .profiles.re_engine_dxil_recovery import compact_recovered_dxil_storage
lazy from .profiles.re_engine_shader_graph import RE_ENGINE_RECOVERED_MASTER_KIND
lazy from .profiles.renderer_inputs import (
    RendererResourceUnavailable, RendererValueUnavailable, engine_buffer_layout, engine_resource_layout, renderer_only,
    free_register, engine_input_plan, graph_renderer_buffers, graph_renderer_resources,
)
lazy from file_handlers.sdf.shader_graph_math import value_width
lazy from utils.hex_util import align


MATERIAL_SCHEMA = "reasy.material"
MATERIAL_SCHEMA_VERSION = 2
MAX_MATERIAL_PROJECT_BYTES = MAX_PROJECT_LENGTH
MATERIAL_VALUE_TYPES = ("float", "float2", "float3", "float4")
_TEXTURE_CONTRACT_DIMENSIONS = {
    "texture1d": "texture1d",
    "texture1darray": "texture1d-array",
    "texture2d": "texture2d",
    "texture2darray": "texture2d-array",
    "texturecube": "texture-cube",
    "texture3d": "texture3d",
}
MATERIAL_TEXTURE_DIMENSIONS = frozenset(_TEXTURE_CONTRACT_DIMENSIONS)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MATERIAL_PARAMETER_NAME = MmtrGameProfile.material_parameter_name


class MaterialProjectError(ValueError):
    pass


class MaterialBuildError(MaterialProjectError):
    def __init__(self, message: str, *, technical_details: str = "") -> None:
        super().__init__(message)
        self.technical_details = str(technical_details)


def _engine_input_unavailable(exc):
    while exc is not None:
        if isinstance(exc, (RendererValueUnavailable, RendererResourceUnavailable)):
            return exc
        exc = exc.__cause__
    return None


def material_program_routes(document, project=None) -> tuple[tuple[int, int, str], ...]:
    if project is not None and project.domain == "implementation":
        if not project.renderer_template_routes:
            raise ShaderGraphError("A graph-owned material needs an explicit renderer template route table.")
        result = []
        for target, index, role, name in project.renderer_template_routes:
            if target >= len(document.sdf.targets) or index >= len(document.sdf.targets[target].programs):
                raise ShaderGraphError("The renderer template program table changed.")
            program = document.sdf.targets[target].programs[index]
            shader = program.shader(role)
            if program.name != name or shader is None or not shader.present:
                raise ShaderGraphError("The renderer template route changed its native meaning or stage.")
            result.append((target, index, role))
        return tuple(result)
    profile = document.profile.shader_graph_profile
    routes = (profile.material_project_program_routes(project, document) if project is not None
              else profile.material_program_routes(document))
    return tuple((int(target), int(program), str(role)) for target, program, role in routes)


def pass_description(document, target_index: int, program_index: int, role: str) -> str:
    program = document.sdf.targets[int(target_index)].programs[int(program_index)]
    shader = program.shader(role)
    api = ("Vulkan" if shader is not None and shader.binary_format == "spirv-module" else
           "DirectX 12" if shader is not None and shader.reflection is not None and shader.reflection.model_major >= 6
           else "DirectX 11")
    purpose = purpose_title(pass_purpose(document, document.profile.shader_graph_profile, target_index, program_index))
    return f"{purpose} · {program.name} ({api}, {role}) [{target_index}:{program_index}]"


def material_program_contract(
    document,
    target_index: int,
    program_index: int,
    role: str = "pixel",
    *,
    allow_unreflected_material_fields: bool = False,
) -> ShaderContract:
    resolved_target = int(target_index)
    resolved_program = int(program_index)
    resolved_role = str(role).strip().casefold()
    cache_key = (
        resolved_target,
        resolved_program,
        resolved_role,
        bool(allow_unreflected_material_fields),
    )
    cached = document._material_contract_cache.get(cache_key)
    if cached is not None:
        return cached
    program = document.sdf.targets[resolved_target].programs[resolved_program]
    contract = document.profile.shader_graph_profile.shader_contract(document, program, resolved_role)
    logical_name = document.profile.material_parameter_name
    standard_names = (
        type(document.profile) is MmtrGameProfile
        and getattr(logical_name, "__func__", None) is _MATERIAL_PARAMETER_NAME
    )
    buffer_names = tuple(document.profile.user_material_buffer_names)
    prefixes = tuple(getattr(document.profile, "reflected_material_variable_prefixes", ()))
    shared = getattr(document, "_material_buffer_contract_cache", None) if (
        standard_names and all(type(name) is str for name in (*buffer_names, *prefixes))
        and type(contract) is ShaderContract and type(program.constant_buffers) is tuple
    ) else None
    shared_key = (id(contract), id(program.constant_buffers), bool(allow_unreflected_material_fields),
                  buffer_names, prefixes)
    prior = shared.get(shared_key) if shared is not None else None
    if prior is None:
        reconciled = material_buffer_contract(
            contract,
            program,
            buffer_names=buffer_names,
            logical_name=logical_name,
            allow_unreflected_fields=allow_unreflected_material_fields,
            _indexed_names=standard_names,
        )
        if shared is not None and _immutable_material_contract_tables(contract, program.constant_buffers):
            shared[shared_key] = contract, program.constant_buffers, reconciled
        contract = reconciled
    else:
        contract = prior[2]
    document._material_contract_cache[cache_key] = contract
    return contract


def _immutable_material_contract_tables(contract, outer_buffers) -> bool:
    if type(contract.constant_buffers) is not tuple:
        return False
    for buffers, buffer_type, variable_type in (
        (contract.constant_buffers, ShaderContractBuffer, ShaderContractVariable),
        (outer_buffers, SdfConstantBuffer, SdfConstantVariable),
    ):
        for buffer in buffers:
            if (type(buffer) is not buffer_type or type(buffer.name) is not str
                    or type(buffer.byte_size) is not int or type(buffer.variables) is not tuple):
                return False
            if any(type(variable) is not variable_type or type(variable.name) is not str
                   or type(variable.byte_offset) is not int or type(variable.byte_size) is not int
                   for variable in buffer.variables):
                return False
    return True


def _finite_values(values: Any, width: int) -> tuple[float, ...]:
    if not isinstance(values, (list, tuple)) or len(values) != width:
        raise MaterialProjectError(f"default value needs exactly {width} component(s)")
    result = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in result):
        raise MaterialProjectError("material defaults must be finite")
    return result


@dataclass(frozen=True, slots=True)
class MaterialParameterDefinition:
    parameter_id: str
    name: str
    value_type: str
    default: tuple[float, ...]
    role: str = "custom"
    group: str = "Material"
    minimum: float | None = None
    maximum: float | None = None
    source_name: str = ""
    source_byte_offset: int | None = None

    def __post_init__(self) -> None:
        if not self.parameter_id or len(self.parameter_id) > 128:
            raise MaterialProjectError("a material parameter needs a stable ID")
        if not _NAME.fullmatch(self.name):
            raise MaterialProjectError(
                f"parameter {self.name!r} must start with a letter or underscore and contain only letters, numbers, and underscores"
            )
        if self.value_type not in MATERIAL_VALUE_TYPES:
            raise MaterialProjectError(f"unsupported material parameter type {self.value_type!r}")
        if not self.role or len(self.role) > 64:
            raise MaterialProjectError("material parameter role is invalid")
        object.__setattr__(self, "default", _finite_values(self.default, value_width(self.value_type)))
        if self.minimum is not None and not math.isfinite(float(self.minimum)):
            raise MaterialProjectError("parameter minimum must be finite")
        if self.maximum is not None and not math.isfinite(float(self.maximum)):
            raise MaterialProjectError("parameter maximum must be finite")
        if (
            self.minimum is not None
            and self.maximum is not None
            and float(self.minimum) > float(self.maximum)
        ):
            raise MaterialProjectError("parameter minimum cannot exceed its maximum")
        if self.source_byte_offset is not None and int(self.source_byte_offset) < 0:
            raise MaterialProjectError("source parameter offsets cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.parameter_id,
            "name": self.name,
            "type": self.value_type,
            "default": list(self.default),
            "role": self.role,
            "group": self.group,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "source_name": self.source_name,
            "source_byte_offset": self.source_byte_offset,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "MaterialParameterDefinition":
        if not isinstance(value, dict):
            raise MaterialProjectError("material parameter definitions must be objects")
        value_type = str(value.get("type", "float"))
        return cls(
            parameter_id=str(value.get("id", "")),
            name=str(value.get("name", "")),
            value_type=value_type,
            default=value.get("default", []),
            role=str(value.get("role", "custom"))[:64],
            group=str(value.get("group", "Material"))[:128],
            minimum=(None if value.get("minimum") is None else float(value["minimum"])),
            maximum=(None if value.get("maximum") is None else float(value["maximum"])),
            source_name=str(value.get("source_name", ""))[:128],
            source_byte_offset=(
                None
                if value.get("source_byte_offset") is None
                else int(value["source_byte_offset"])
            ),
        )


def valid_default_path(path: str) -> bool:
    return len(path) <= 2048 and "\x00" not in path


@dataclass(frozen=True, slots=True)
class MaterialTextureDefinition:
    texture_id: str
    name: str
    role: str = "color"
    dimension: str | None = "texture2d"  # None: descriptor with no image-type constraint.
    source_name: str = ""
    default_path: str = ""

    def __post_init__(self) -> None:
        if not self.texture_id or len(self.texture_id) > 128:
            raise MaterialProjectError("a material texture needs a stable ID")
        if not _NAME.fullmatch(self.name):
            raise MaterialProjectError(f"invalid material texture name {self.name!r}")
        if self.dimension is not None and (
            not isinstance(self.dimension, str) or self.dimension not in _TEXTURE_CONTRACT_DIMENSIONS
        ):
            raise MaterialProjectError(f"unsupported material texture dimension {self.dimension!r}")
        if len(self.role) > 64 or len(self.source_name) > 128:
            raise MaterialProjectError("material texture metadata is too long")
        if not valid_default_path(self.default_path):
            raise MaterialProjectError("material texture default path is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.texture_id,
            "name": self.name,
            "role": self.role,
            "dimension": self.dimension,
            "source_name": self.source_name,
            "default_path": self.default_path,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "MaterialTextureDefinition":
        if not isinstance(value, dict):
            raise MaterialProjectError("material texture definitions must be objects")
        return cls(
            texture_id=str(value.get("id", "")),
            name=str(value.get("name", "")),
            role=str(value.get("role", "color")),
            dimension=value.get("dimension", "texture2d"),
            source_name=str(value.get("source_name", "")),
            default_path=str(value.get("default_path", "")),
        )


def _compact_project_dxil_recipes(payload: str) -> str:
    document = json.loads(payload)
    compacted = {}
    for node in document["graph"]["nodes"]:
        if node["kind"] != RE_ENGINE_RECOVERED_MASTER_KIND:
            continue
        properties = node["properties"]
        if not isinstance(properties, dict):
            continue
        recipes = [properties]
        family = properties.get("family_recipes", ())
        if isinstance(family, list):
            recipes.extend(recipe for recipe in family if isinstance(recipe, dict))
        for recipe in recipes:
            programs = recipe.get("semantic_programs", ())
            if not isinstance(programs, list):
                continue
            for program in programs:
                if not isinstance(program, dict) or program.get("ir_kind") != "dxil":
                    continue
                stored = program["semantic_ir"]
                if stored not in compacted:
                    compacted[stored] = compact_recovered_dxil_storage(stored)
                program["semantic_ir"] = compacted[stored]
    return json.dumps(document, separators=(',', ':'), sort_keys=True)


@dataclass(slots=True)
class MmtrMaterialProject:
    name: str
    source_profile: str
    graph: ShaderGraph
    parameters: list[MaterialParameterDefinition]
    textures: list[MaterialTextureDefinition]
    domain: str = "surface"
    source_material_model: str = ""
    source_texture_names: list[str] = field(default_factory=list)
    implementations: list[MaterialProgramImplementation] = field(default_factory=list)
    renderer_template_routes: list[tuple[int, int, str, str]] = field(default_factory=list)
    pipeline_states: dict[str, dict] = field(default_factory=dict)
    _prepare_semantics: Callable[[MmtrMaterialProject], None] | None = field(
        default=None, repr=False, compare=False,
    )

    def prepare_semantics(self) -> None:
        if self.domain == "implementation":
            self._prepare_semantics = None
        if self._prepare_semantics is not None:
            try:
                self._prepare_semantics(self)
            except ShaderGraphError as exc:
                raise MaterialProjectError(str(exc)) from exc
            self._prepare_semantics = None

    def parameter(self, parameter_id: str) -> MaterialParameterDefinition | None:
        return next((item for item in self.parameters if item.parameter_id == parameter_id), None)

    def texture(self, texture_id: str) -> MaterialTextureDefinition | None:
        return next((item for item in self.textures if item.texture_id == texture_id), None)

    def to_dict(self) -> dict[str, Any]:
        self.prepare_semantics()
        return {
            "schema": MATERIAL_SCHEMA,
            "version": MATERIAL_SCHEMA_VERSION,
            "name": self.name,
            "source_profile": self.source_profile,
            "domain": self.domain,
            "source_material_model": self.source_material_model,
            "source_texture_names": list(self.source_texture_names),
            "parameters": [item.to_dict() for item in self.parameters],
            "textures": [item.to_dict() for item in self.textures],
            "graph": self.graph.to_dict(),
            "implementations": [item.to_dict() for item in self.implementations],
            "renderer_template_routes": [list(row) for row in self.renderer_template_routes],
            "pipeline_states": self.pipeline_states,
        }

    def to_json(self) -> str:
        payload = json.dumps(self.to_dict(), separators=(',', ':'), sort_keys=True)
        if len(payload.encode("utf-8")) > MAX_MATERIAL_PROJECT_BYTES:
            try:
                payload = _compact_project_dxil_recipes(payload)
            except ShaderGraphError as exc:
                raise MaterialProjectError(
                    f"material project is too large, and its recovered shader data cannot be compacted: {exc}"
                ) from exc
            if len(payload.encode("utf-8")) > MAX_MATERIAL_PROJECT_BYTES:
                raise MaterialProjectError("material project is too large")
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "MmtrMaterialProject":
        if not isinstance(value, dict) or value.get("schema") != MATERIAL_SCHEMA:
            raise MaterialProjectError("file is not a REasy material")
        try:
            if int(value.get("version", -1)) not in {1, MATERIAL_SCHEMA_VERSION}:
                raise MaterialProjectError(
                    f"unsupported material project version {value.get('version')!r}"
                )
            parameters_value = value.get("parameters", [])
            textures_value = value.get("textures", [])
            source_texture_names = value.get("source_texture_names")
            if not isinstance(parameters_value, list) or len(parameters_value) > 1024:
                raise MaterialProjectError("material parameter table is invalid or too large")
            if not isinstance(textures_value, list) or len(textures_value) > 256:
                raise MaterialProjectError("material texture table is invalid or too large")
            if source_texture_names is not None and (
                not isinstance(source_texture_names, list)
                or len(source_texture_names) > 256
                or any(not isinstance(item, str) for item in source_texture_names)
            ):
                raise MaterialProjectError("source material texture table is invalid or too large")
            textures = [MaterialTextureDefinition.from_dict(item) for item in textures_value]
            implementations = value.get("implementations", [])
            if not isinstance(implementations, list) or len(implementations) > 65536:
                raise MaterialProjectError("Material implementation table is invalid or too large.")
            implementations = [MaterialProgramImplementation.from_dict(item) for item in implementations]
            template_routes = value.get("renderer_template_routes", [])
            if (not isinstance(template_routes, list) or len(template_routes) > 65536
                    or any(not isinstance(row, list) or len(row) != 4 for row in template_routes)):
                raise MaterialProjectError("The renderer template route table is invalid.")
            pipeline_states = value.get("pipeline_states", {})
            if (not isinstance(pipeline_states, dict) or len(pipeline_states) > 65536
                    or any(not isinstance(item, dict) for item in pipeline_states.values())):
                raise MaterialProjectError("The render settings table is invalid.")
            project = cls(
                name=str(value.get("name", "Material"))[:256],
                source_profile=str(value.get("source_profile", ""))[:64],
                domain=str(value.get("domain", "surface"))[:32],
                source_material_model=str(
                    value.get("source_material_model", "")
                )[:128],
                parameters=[MaterialParameterDefinition.from_dict(item) for item in parameters_value],
                textures=textures,
                source_texture_names=(
                    [str(item) for item in source_texture_names]
                    if source_texture_names is not None
                    else [str(item.source_name or item.name) for item in textures]
                ),
                graph=ShaderGraph.from_dict(value.get("graph")),
                implementations=implementations,
                renderer_template_routes=[tuple(row) for row in template_routes],
                pipeline_states={str(key): item for key, item in pipeline_states.items()},
            )
            errors = [item.message for item in validate_material_project(project) if item.severity == "error"]
            if errors:
                raise MaterialProjectError("\n".join(errors))
            return project
        except MaterialProjectError:
            raise
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise MaterialProjectError(f"invalid material project data: {exc}") from exc

    @classmethod
    def from_json(cls, text: str) -> "MmtrMaterialProject":
        if not isinstance(text, str):
            raise MaterialProjectError("material project is invalid or too large")
        try:
            byte_size = len(text.encode("utf-8"))
        except UnicodeError as exc:
            raise MaterialProjectError(f"invalid material JSON: {exc}") from exc
        if byte_size > MAX_MATERIAL_PROJECT_BYTES:
            raise MaterialProjectError("material project is invalid or too large")
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise MaterialProjectError(f"invalid material JSON: {exc}") from exc
        return cls.from_dict(payload)


def _parameter_type(parameter) -> str:
    values = {
        str(name).casefold()
        for name in getattr(parameter, "type_names", ())
        if str(name).casefold() in MATERIAL_VALUE_TYPES
    }
    if len(values) != 1:
        raise MaterialProjectError(
            f"Material parameter {getattr(parameter, 'name', '')!r} does not "
            "have one exact reflected shader-language type. REasy will not "
            "infer it from the field's byte width."
        )
    return next(iter(values))


def _default_parameter_value(name: str, value_type: str) -> tuple[float, ...]:
    width = value_width(value_type)
    lowered = str(name).casefold()
    values = [0.0] * width
    if any(token in lowered for token in ("color", "tint")):
        values[:] = [1.0] * width
    elif any(token in lowered for token in ("roughness", "alpha", "opacity", "occlu", "ao")):
        values[0] = 1.0 if "roughness" not in lowered else 0.5
    return tuple(values)


def _parameter_role(name: str) -> str:
    lowered = re.sub(r"[^a-z0-9]", "", str(name).casefold())
    if lowered in {"basecolor", "albedo", "diffusecolor"}:
        return "base_color"
    if "roughness" in lowered:
        return "roughness"
    if lowered in {"metallic", "metalness"}:
        return "metallic"
    if lowered in {"specular", "reflectance", "specularlevel"}:
        return "specular"
    if "transluc" in lowered:
        return "translucency"
    if lowered in {"occlucion", "occlusion", "ambientocclusion", "ao"}:
        return "ambient_occlusion"
    if lowered in {"alpha", "opacity", "transparent", "transparency"}:
        return "opacity"
    if lowered in {"alphavalue", "opacitymask", "alphamask", "cutout"}:
        return "opacity_mask"
    if lowered in {"glow", "glowintensity", "bloom", "bloomintensity"}:
        return "glow"
    if lowered in {"emissiveintensity", "emissionintensity"}:
        return "emission_intensity"
    if "uv" in lowered and any(token in lowered for token in ("tile", "tili", "scale")):
        return "uv_scale"
    return "custom"


def _texture_role(name: str) -> str:
    lowered = str(name).casefold()
    if "normal" in lowered:
        return "normal"
    if "rough" in lowered:
        return "roughness"
    if "metal" in lowered:
        return "metallic"
    if any(token in lowered for token in ("emiss", "glow")):
        return "emission"
    if any(token in lowered for token in ("mask", "alpha", "opacity")):
        return "mask"
    return "color"


def _parameter_limits(role: str, value_type: str) -> tuple[float | None, float | None]:
    if value_type == "float" and role in {
        "roughness",
        "metallic",
        "specular",
        "translucency",
        "ambient_occlusion",
        "opacity",
        "opacity_mask",
    }:
        return 0.0, 1.0
    return None, None


def _input_ids(nodes) -> tuple[set[str], set[str]]:
    parameters, textures = set(), set()
    for node in nodes:
        if node.kind == "material_parameter":
            parameters.add(str(node.properties.get("parameter_id", "")))
        elif node.kind == "material_texture":
            textures.add(str(node.properties.get("texture_id", "")))
    return parameters, textures


def ensure_material_interface_nodes(project: MmtrMaterialProject) -> bool:
    if project.domain == "implementation":
        return False
    graph = project.graph
    represented_parameters, represented_textures = _input_ids(graph.nodes)
    missing_parameters = [
        item
        for item in project.parameters
        if item.parameter_id not in represented_parameters
    ]
    missing_textures = [
        item
        for item in project.textures
        if item.texture_id not in represented_textures
    ]
    if not missing_parameters and not missing_textures:
        return False

    minimum_x = min((float(node.x) for node in graph.nodes), default=850.0)
    rows = 10
    horizontal_step = 310.0
    vertical_step = 125.0
    parameter_anchor = minimum_x - 390.0
    for index, definition in enumerate(missing_parameters):
        graph.add_node(
            "material_parameter",
            x=parameter_anchor - (index // rows) * horizontal_step,
            y=70.0 + (index % rows) * vertical_step,
            properties={
                "parameter_id": definition.parameter_id,
                "name": definition.name,
                "display_name": definition.name.replace("_", " "),
                "value_type": definition.value_type,
                "interface_only": bool(definition.source_name),
            },
        )

    parameter_columns = max(1, (len(missing_parameters) + rows - 1) // rows)
    texture_anchor = parameter_anchor - parameter_columns * horizontal_step - 80.0
    for index, definition in enumerate(missing_textures):
        graph.add_node(
            "material_texture",
            x=texture_anchor - (index // rows) * horizontal_step,
            y=70.0 + (index % rows) * vertical_step,
            properties={
                "texture_id": definition.texture_id,
                "name": definition.name,
                "display_name": definition.name.replace("_", " "),
                "dimension": definition.dimension,
                "interface_only": bool(definition.source_name),
            },
        )
    return True


def prune_orphaned_material_inputs(project: MmtrMaterialProject) -> bool:
    represented_parameters, represented_textures = _input_ids(project.graph.nodes)
    parameter_count = len(project.parameters)
    texture_count = len(project.textures)
    project.parameters[:] = [
        definition
        for definition in project.parameters
        if definition.parameter_id in represented_parameters
    ]
    project.textures[:] = [
        definition
        for definition in project.textures
        if definition.texture_id in represented_textures
    ]
    return (
        len(project.parameters) != parameter_count
        or len(project.textures) != texture_count
    )


def material_semantics_complete(project: MmtrMaterialProject) -> bool:
    if project.domain == "implementation":
        return bool(project.implementations) and not any(
            issue.severity == "error" for issue in validate_material_project(project)
        )
    if not str(project.source_material_model).strip():
        return False

    surfaces = [
        node for node in project.graph.nodes if node.kind == "material_surface"
    ]
    if len(surfaces) != 1:
        return False
    reverse: dict[str, set[str]] = {
        node.node_id: set() for node in project.graph.nodes
    }
    for link in project.graph.links:
        if link.target_node in reverse and link.source_node in reverse:
            reverse[link.target_node].add(link.source_node)
    reachable = {surfaces[0].node_id}
    pending = list(reachable)
    while pending:
        target = pending.pop()
        for source in reverse.get(target, ()):
            if source not in reachable:
                reachable.add(source)
                pending.append(source)

    reachable_parameters, reachable_textures = _input_ids(
        node for node in project.graph.nodes if node.node_id in reachable
    )
    parameter_ids = {definition.parameter_id for definition in project.parameters}
    texture_ids = {definition.texture_id for definition in project.textures}
    reconstruction_boundaries = [
        node
        for node in project.graph.nodes
        if bool(
            node.properties.get(
                "semantic_reconstruction_boundary",
                False,
            )
        )
    ]
    if reconstruction_boundaries:
        if len(reconstruction_boundaries) != 1:
            return False
        boundary = reconstruction_boundaries[0]
        incoming = {
            (link.target_node, link.target_pin)
            for link in project.graph.links
        }
        for pin in boundary.properties.get("input_pins", []):
            source_id = str(pin.get("source_id", ""))
            if pin.get("input_kind") == "parameter" and source_id in parameter_ids:
                reachable_parameters.add(source_id)
            elif pin.get("input_kind") == "texture" and source_id in texture_ids:
                reachable_textures.add(source_id)
        return (
            boundary.node_id in reachable
            and reachable_parameters == parameter_ids
            and reachable_textures == texture_ids
            and all(
                not pin.required or (boundary.node_id, pin.name) in incoming
                for pin in node_pins(boundary, None)
                if pin.direction == "input"
            )
        )
    return reachable_parameters == parameter_ids and reachable_textures == texture_ids


def new_material_project(document, *, defer_shader_recovery: bool = False) -> MmtrMaterialProject:
    parameters = []
    for item in document.material_interface.parameters:
        if is_private_material_parameter(item.name):
            continue
        value_type = _parameter_type(item)
        role = _parameter_role(item.name)
        minimum, maximum = _parameter_limits(role, value_type)
        parameters.append(
            MaterialParameterDefinition(
                parameter_id=uuid4().hex,
                name=str(item.name),
                value_type=value_type,
                default=_default_parameter_value(item.name, value_type),
                role=role,
                group="Material",
                minimum=minimum,
                maximum=maximum,
                source_name=str(item.name),
                source_byte_offset=int(item.byte_offset),
            )
        )

    profile = document.profile.shader_graph_profile
    textures = []
    for item in profile.editable_textures(document):
        dimension = material_resource_dimension(item) or None
        textures.append(
            MaterialTextureDefinition(
                texture_id=uuid4().hex,
                name=str(item.name),
                role=_texture_role(item.name) if dimension else "custom",
                dimension=dimension,
                source_name=str(item.name),
            )
        )

    graph = ShaderGraph("material", "")
    surface = graph.add_node("material_surface", x=850.0, y=110.0)
    by_name: dict[str, list[MaterialParameterDefinition]] = {}
    for item in parameters:
        by_name.setdefault(item.name.casefold(), []).append(item)
    y = 70.0
    aliases = (
        (("basecolor", "base_color"), "base_color", "rgb"),
        (("roughness",), "roughness", ""),
        (("metallic", "metalness"), "metalness", ""),
        (("translucency",), "translucency", ""),
        (("occlucion", "occlusion", "ambientocclusion"), "ambient_occlusion", ""),
        (("alpha", "opacity", "transparent", "transparency", "alphavalue"), "opacity", ""),
        (("alphavalue", "opacitymask"), "opacity_mask", ""),
        (("glow", "glowintensity", "bloom", "bloomintensity"), "glow", ""),
    )
    for names, surface_pin, swizzle in aliases:
        definition = next(
            (
                matches[0]
                for name in names
                for matches in (by_name.get(name, ()),)
                if len(matches) == 1
            ),
            None,
        )
        if definition is None:
            continue
        parameter_node = graph.add_node(
            "material_parameter",
            x=80.0,
            y=y,
            properties={
                "parameter_id": definition.parameter_id,
                "name": definition.name,
                "display_name": definition.name,
                "value_type": definition.value_type,
            },
        )
        source_node, source_pin = parameter_node, "value"
        resolved_swizzle = swizzle
        if (
            not resolved_swizzle
            and surface_pin != "base_color"
            and definition.value_type != "float"
        ):
            # DMC5 sometimes stores a scalar material control in a float4 field.
            resolved_swizzle = "x"
        if resolved_swizzle:
            mask = graph.add_node(
                "swizzle",
                x=355.0,
                y=y,
                properties={
                    "source_type": definition.value_type,
                    "mask": resolved_swizzle,
                },
            )
            graph.connect(parameter_node.node_id, "value", mask.node_id, "value")
            source_node, source_pin = mask, "result"
        graph.connect(source_node.node_id, source_pin, surface.node_id, surface_pin)
        y += 125.0

    project = MmtrMaterialProject(
        name=document.family.replace("-", " ").title() or "Material",
        source_profile=document.profile.game,
        graph=graph,
        parameters=parameters,
        textures=textures,
        source_texture_names=[str(item.name) for item in textures],
    )
    if defer_shader_recovery:
        profile.seed_material_project_interface(document, project)
    else:
        profile.seed_material_project(document, project)
    ensure_material_interface_nodes(project)
    return project


def validate_material_project(
    project: MmtrMaterialProject,
    contract: ShaderContract | None = None,
) -> tuple[ShaderGraphDiagnostic, ...]:
    graph = project.graph
    issues: list[ShaderGraphDiagnostic] = []
    resolved_pins = {}

    def pins(node):
        identity = id(node)
        if identity not in resolved_pins:
            resolved_pins[identity] = node_pins(node, contract)
        return resolved_pins[identity]

    template_keys = []
    for row in project.renderer_template_routes:
        if (not isinstance(row, (list, tuple)) or len(row) != 4
                or any(type(value) is not int or value < 0 for value in row[:2])
                or row[2] not in STAGE_KEYS or not isinstance(row[3], str) or not row[3]):
            issues.append(ShaderGraphDiagnostic("error", "renderer-template", "A renderer template route is invalid."))
            break
        template_keys.append(tuple(row[:3]))
    if len(template_keys) != len(set(template_keys)):
        issues.append(ShaderGraphDiagnostic("error", "renderer-template", "Renderer template routes must be unique."))
    try:
        implementation_routes(project)
        for implementation in project.implementations:
            issues.extend(implementation.diagnostics(source_profile=project.source_profile))
    except ShaderGraphError as exc:
        issues.append(ShaderGraphDiagnostic("error", "implementation", str(exc)))
    if project.domain == "implementation":
        if not project.implementations or not template_keys or graph.nodes or graph.links:
            issues.append(ShaderGraphDiagnostic("error", "implementation", "A graph-owned material needs implementations and no imported master graph."))
    issues.extend(validate_material_function_nodes(graph))
    if graph.stage != "material":
        issues.append(ShaderGraphDiagnostic("error", "domain", "This graph is not a surface material."))
    if len(graph.nodes) > MAX_GRAPH_NODES or len(graph.links) > MAX_GRAPH_LINKS:
        issues.append(ShaderGraphDiagnostic("error", "size", "The material graph is too large."))
    surfaces = [item for item in graph.nodes if item.kind == "material_surface"]
    if len(surfaces) != 1 and project.domain != "implementation":
        issues.append(ShaderGraphDiagnostic("error", "surface", "A material needs exactly one Material Output node."))
    parameter_ids = [item.parameter_id for item in project.parameters]
    texture_ids = [item.texture_id for item in project.textures]
    if len(parameter_ids) != len(set(parameter_ids)) or len(
        {item.name for item in project.parameters}
    ) != len(project.parameters):
        issues.append(ShaderGraphDiagnostic("error", "parameters", "Material parameter names and IDs must be unique."))
    if len(texture_ids) != len(set(texture_ids)) or len(
        {item.name for item in project.textures}
    ) != len(project.textures):
        issues.append(ShaderGraphDiagnostic("error", "textures", "Material texture names and IDs must be unique."))
    source_texture_names = [
        str(item) for item in project.source_texture_names if str(item)
    ]
    if len(set(source_texture_names)) != len(
        source_texture_names
    ) or any(not _NAME.fullmatch(item) for item in source_texture_names):
        issues.append(
            ShaderGraphDiagnostic(
                "error",
                "textures",
                "The source MMTR texture interface contains invalid or duplicate names.",
            )
        )

    if project.domain == "implementation":
        return tuple(issues)
    node_map = {item.node_id: item for item in graph.nodes}
    if len(node_map) != len(graph.nodes):
        issues.append(ShaderGraphDiagnostic("error", "nodes", "Material node IDs must be unique."))
    occupied: set[tuple[str, str]] = set()
    adjacency = {item.node_id: set() for item in graph.nodes}
    reverse = {item.node_id: set() for item in graph.nodes}
    for node in graph.nodes:
        issues.extend(node_property_diagnostics(node))
        if node.kind == "material_parameter" and str(node.properties.get("parameter_id", "")) not in parameter_ids:
            issues.append(ShaderGraphDiagnostic("error", "parameter", f"{node_title(node)} refers to a deleted parameter.", node.node_id))
        if node.kind == "material_texture" and str(node.properties.get("texture_id", "")) not in texture_ids:
            issues.append(ShaderGraphDiagnostic("error", "texture", f"{node_title(node)} refers to a deleted texture.", node.node_id))
        if node.kind == "material_function":
            try:
                asset = material_function_from_node(node)
            except MaterialFunctionError:
                continue
            if asset.source_profile and not same_mmtr_profile(asset.source_profile, project.source_profile):
                issues.append(
                    ShaderGraphDiagnostic(
                        "error",
                        "function-profile",
                        f"{asset.name} targets {asset.source_profile}, not {project.source_profile}.",
                        node.node_id,
                    )
                )
    for link in graph.links:
        source = node_map.get(link.source_node)
        target = node_map.get(link.target_node)
        if source is None or target is None:
            issues.append(ShaderGraphDiagnostic("error", "link", "A material connection points to a deleted node."))
            continue
        source_pin = next((item for item in pins(source) if item.direction == "output" and item.name == link.source_pin), None)
        target_pin = next((item for item in pins(target) if item.direction == "input" and item.name == link.target_pin), None)
        if source_pin is None or target_pin is None:
            issues.append(ShaderGraphDiagnostic("error", "pin", "A material connection points to an unavailable socket.", target.node_id))
            continue
        key = (target.node_id, target_pin.name)
        if key in occupied:
            issues.append(ShaderGraphDiagnostic("error", "connection", f"{target_pin.label} has more than one connection.", target.node_id))
        occupied.add(key)
        if not connection_compatible(source_pin.value_type, target_pin.value_type):
            issues.append(ShaderGraphDiagnostic("error", "type", f"{source_pin.value_type} cannot connect to {target_pin.label} ({target_pin.value_type}).", target.node_id))
        adjacency[source.node_id].add(target.node_id)
        reverse[target.node_id].add(source.node_id)

    indegree = dict.fromkeys(adjacency, 0)
    for targets in adjacency.values():
        for identity in targets:
            indegree[identity] += 1
    pending = [identity for identity, count in indegree.items() if count == 0]
    visited = 0
    while pending:
        identity = pending.pop()
        visited += 1
        for target in adjacency[identity]:
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    if visited != len(adjacency):
        issues.append(ShaderGraphDiagnostic("error", "cycle", "Material graphs cannot contain loops."))

    reachable = {item.node_id for item in surfaces}
    pending = list(reachable)
    while pending:
        target = pending.pop()
        for source in reverse.get(target, ()):
            if source not in reachable:
                reachable.add(source)
                pending.append(source)
    for node in graph.nodes:
        if node.node_id not in reachable:
            continue
        for pin in pins(node):
            if pin.direction == "input" and pin.required and (node.node_id, pin.name) not in occupied:
                issues.append(ShaderGraphDiagnostic("error", "input", f"{node_title(node)} needs {pin.label}.", node.node_id))
    return tuple(issues)


@dataclass(frozen=True, slots=True)
class MaterialBufferVariable:
    name: str
    byte_offset: int
    byte_size: int
    value_type: str = "float"
    default: tuple[float, ...] = ()
    source_name: str = ""


@dataclass(frozen=True, slots=True)
class MaterialBufferLayout:
    name: str
    byte_size: int
    variables: tuple[MaterialBufferVariable, ...]


@dataclass(frozen=True, slots=True)
class MaterialTextureSlot:
    texture_id: str
    name: str
    dimension: str | None  # None: MDF descriptor with no shader sampling constraint.
    default_path: str = ""
    source_name: str = ""


@dataclass(frozen=True, slots=True)
class MaterialTextureBinding:
    texture_id: str
    name: str
    bind_point: int
    type_code: int


@dataclass(frozen=True, slots=True)
class MaterialSystemBufferVariable:
    name: str
    byte_offset: int
    byte_size: int


@dataclass(frozen=True, slots=True)
class MaterialSystemBufferBinding:
    name: str
    bind_point: int
    byte_size: int
    variables: tuple[MaterialSystemBufferVariable, ...] = ()
    type_code: int = 0xFF


@dataclass(frozen=True, slots=True)
class MaterialEngineResourceBinding:
    """``table`` is ``shader_resource`` or ``sampler``."""
    table: str
    name: str
    bind_point: int


@dataclass(frozen=True, slots=True)
class MaterialShaderReplacement:
    target_index: int
    program_index: int
    stage: str
    bytecode: bytes
    texture_bindings: tuple[MaterialTextureBinding, ...] = ()
    constant_buffer_bindings: tuple[MaterialSystemBufferBinding, ...] = ()
    binary_format: str = "dxbc-container"
    entry_point: str = "main"
    diagnostics: str = ''
    resource_bindings: tuple[ShaderContractResource, ...] | None = None
    engine_resource_bindings: tuple[MaterialEngineResourceBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class MaterialShaderCompileRequest:
    generated: object
    adjusted_contract: ShaderContract
    semantics: object
    shader: object
    target_index: int


@dataclass(frozen=True, slots=True)
class _PreparedMaterialShader:
    request: MaterialShaderCompileRequest
    layout: MaterialBufferLayout
    authored: MaterialProgramImplementation | None
    execution: object


@dataclass(frozen=True, slots=True)
class _PreparedMaterialRoute:
    target_index: int
    program_index: int
    shader_role: str
    cache_key: str
    shader: _PreparedMaterialShader


@dataclass(frozen=True, slots=True)
class _CompiledMaterialShader:
    bytecode: bytes
    binary_format: str
    entry_point: str
    layout: MaterialBufferLayout
    texture_interface: tuple[MaterialTextureBinding, ...]
    texture_bindings: tuple[MaterialTextureBinding, ...]
    constant_buffer_bindings: tuple[MaterialSystemBufferBinding, ...]
    diagnostics: str
    resource_bindings: tuple[ShaderContractResource, ...]
    engine_resource_bindings: tuple[MaterialEngineResourceBinding, ...]


@dataclass(frozen=True, slots=True)
class MmtrMaterialBuild:
    profile: str
    replacements: tuple[MaterialShaderReplacement, ...]
    buffer_layout: MaterialBufferLayout
    texture_layout: tuple[MaterialTextureSlot, ...]
    source_texture_names: tuple[str, ...]
    compiled_contracts: int
    source_descriptor_layout: MaterialDescriptorLayout | None = None
    descriptor_layout: MaterialDescriptorLayout | None = None
    family_builds: tuple["MmtrMaterialBuild", ...] = ()
    pipeline_states: Mapping[str, Mapping] = field(default_factory=dict)


def material_parameter_abi(interface, layout: MaterialBufferLayout):
    current = {(str(item.name), int(item.byte_offset), int(item.byte_size))
               for item in interface.parameters if not is_private_material_parameter(item.name)}
    built = {(str(item.name), int(item.byte_offset), int(item.byte_size)) for item in layout.variables}
    return current, built, {int(value) for value in interface.user_buffer_sizes}


def material_interface_matches_layout(document, layout: MaterialBufferLayout) -> bool:
    current, built, sizes = material_parameter_abi(document.material_interface, layout)
    return current == built and sizes == {int(layout.byte_size)}


def material_resource_dimension(resource) -> str:
    names = " ".join(str(item).casefold() for item in resource.type_names)
    if "texture1d-array" in names:
        return "texture1darray"
    if "texture2d-array" in names:
        return "texture2darray"
    if "cube" in names:
        return "texturecube"
    if "texture3d" in names:
        return "texture3d"
    if "texture1d" in names:
        return "texture1d"
    if "texture2d" in names:
        return "texture2d"
    return ""


def material_interface_matches_build(document, build: MmtrMaterialBuild) -> bool:
    if not material_interface_matches_layout(document, build.buffer_layout):
        return False
    if build.source_descriptor_layout != build.descriptor_layout:
        return False
    layouts = document.material_interface.descriptor_layouts
    source_layout = build.source_descriptor_layout
    if source_layout is not None and len(layouts) == 1 and source_layout != layouts[0]:
        return False
    desired = {
        str(item.name): str(item.dimension) if item.dimension is not None else ""
        for item in build.texture_layout
    }
    relevant = desired.keys() | {
        str(name) for name in build.source_texture_names
    }
    resources = document.profile.shader_graph_profile.editable_textures(document)
    current = {
        str(item.name): material_resource_dimension(item)
        for item in resources
        if item.resource_kind == "shader_resource"
        and str(item.name) in relevant
    }
    if source_layout is not None and tuple(layouts) == (source_layout,):
        for name in source_layout.textures:
            current.setdefault(str(name), "")
    return current == desired


def _material_buffer_metadata_variables(
    buffer,
    layout: MaterialBufferLayout,
) -> tuple[dict[str, object], ...]:
    rows = [
        {
            "name": variable.name,
            "byte_offset": variable.byte_offset,
            "byte_size": variable.byte_size,
        }
        for variable in layout.variables
    ]
    occupied = [
        (int(variable.byte_offset), int(variable.byte_offset) + int(variable.byte_size))
        for variable in layout.variables
    ]
    for variable in buffer.variables:
        if not is_private_material_parameter(variable.name):
            continue
        start = int(variable.byte_offset)
        stop = start + int(variable.byte_size)
        if any(start < other_stop and stop > other_start for other_start, other_stop in occupied):
            continue
        rows.append(
            {
                "name": variable.name,
                "byte_offset": start,
                "byte_size": int(variable.byte_size),
            }
        )
        occupied.append((start, stop))
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                int(item["byte_offset"]),
                str(item["name"]).casefold(),
            ),
        )
    )


def apply_material_build(editor, document, build: MmtrMaterialBuild) -> None:
    texture_slots = {item.texture_id: item for item in build.texture_layout}
    material_names = set(build.source_texture_names)
    material_names.update(
        name for item in build.texture_layout for name in (item.name, item.source_name) if name
    )
    implicit = document.profile.sdf_profile.implicit_resource_bindings
    grouped: dict[tuple[int, int], list[MaterialShaderReplacement]] = {}
    seen = set()
    for replacement in build.replacements:
        route = (replacement.target_index, replacement.program_index, replacement.stage)
        if replacement.stage not in STAGE_KEYS or route in seen:
            raise MaterialBuildError(f"Invalid or duplicate material shader route {route!r}.")
        seen.add(route)
        grouped.setdefault(route[:2], []).append(replacement)

    def native_rows(metadata, bindings, *, buffers=False):
        if len(metadata) != len(bindings):
            raise MaterialBuildError("Material metadata and binding counts disagree.")
        rows = {}
        for index, (item, binding) in enumerate(zip(metadata, bindings)):
            row = dict(name=item.name, bind_slots=list(binding.bind_slots),
                       stage_mask=int(binding.stage_mask), type_code=int(binding.type_code),
                       reserved=int(item.reserved))
            if buffers:
                row.update(byte_size=int(item.byte_size), variables=tuple(
                    dict(name=v.name, byte_offset=v.byte_offset, byte_size=v.byte_size) for v in item.variables
                ))
            key = item.name if item.name else ("anonymous", index)
            if key in rows:
                raise MaterialBuildError(f"Material metadata has duplicate resource {item.name!r}.")
            rows[key] = row
        return rows

    for (target_index, program_index), replacements in grouped.items():
        program = document.sdf.targets[target_index].programs[program_index]
        resources = native_rows(program.shader_resources, program.bindings[2])
        buffers = native_rows(program.constant_buffers, program.bindings[0], buffers=True)
        other_resources = {
            'sampler': native_rows(program.samplers,program.bindings[1]),
            'unordered_access': native_rows(program.unordered_access_resources,program.bindings[3]),
            'optional_resource': native_rows(program.optional_resources,program.bindings[4]),
        }
        changed_tables = set()
        for replacement in replacements:
            if replacement.resource_bindings is None:
                continue
            stage = STAGE_KEYS.index(replacement.stage)
            mask = 1 << stage
            for kind,rows in {'shader_resource':resources,**other_resources}.items():
                for row in rows.values():
                    if row['stage_mask'] & mask and not any(matches_outer_resource(
                        binding,kind,row['name'],row['bind_slots'][stage]) for binding in replacement.resource_bindings):
                        row['stage_mask'] &= ~mask
                        changed_tables.add(kind)
        stage_mask = sum(1 << STAGE_KEYS.index(item.stage) for item in replacements)
        removed = set()
        for key, row in resources.items():
            if key in material_names:
                row["stage_mask"] &= ~stage_mask
                removed.add(key)
        for row in buffers.values():
            if row["name"]:
                row["stage_mask"] &= ~stage_mask

        def bind(rows, name, role, bind_point, type_code, *, register_class):
            if (role, name, register_class, bind_point, 0) in implicit and name not in rows:
                return None
            row = rows.setdefault(name, dict(
                name=name, bind_slots=[0] * len(STAGE_KEYS), stage_mask=0,
                type_code=int(type_code), reserved=0,
            ))
            if row["stage_mask"] and row["type_code"] != int(type_code):
                raise MaterialBuildError(f"Resource {name!r} has incompatible types across shader stages.")
            row["name"], row["type_code"] = name, int(type_code)
            index = STAGE_KEYS.index(role)
            row["bind_slots"][index] = int(bind_point)
            row["stage_mask"] |= 1 << index
            return row

        for replacement in replacements:
            editor.replace_shader_payload(
                target_index, program_index, replacement.stage, replacement.bytecode,
                binary_format=replacement.binary_format, entry_point=replacement.entry_point,
            )
            for binding in replacement.texture_bindings:
                slot = texture_slots.get(binding.texture_id)
                if slot is None:
                    raise MaterialBuildError(f"Unknown material texture ID {binding.texture_id!r}.")
                key, source_key = binding.name, slot.source_name
                if key in resources and key not in material_names:
                    raise MaterialBuildError(f"Material texture {binding.name!r} collides with an engine resource.")
                if source_key and source_key != key and source_key in resources:
                    source = resources.pop(source_key)
                    if key in resources:
                        raise MaterialBuildError(f"Material texture rename collides with {binding.name!r}.")
                    resources[key] = source
                    removed.discard(source_key)
                    removed.add(key)
                bind(resources, binding.name, replacement.stage, binding.bind_point, binding.type_code, register_class="t")
            for binding in replacement.constant_buffer_bindings:
                row = bind(buffers, binding.name, replacement.stage, binding.bind_point, binding.type_code, register_class="b")
                if row is None:
                    continue
                variables = tuple(dict(name=v.name, byte_offset=v.byte_offset, byte_size=v.byte_size) for v in binding.variables)
                engine = engine_buffer_layout(document, program, replacement.stage, binding.name)[0] if "byte_size" not in row else None
                if engine is not None:
                    if engine.type_code != int(binding.type_code) or int(binding.byte_size) > engine.table_byte_size:
                        raise MaterialBuildError(f"Engine buffer {binding.name!r} does not match the game's declaration.")
                    row.update(byte_size=engine.table_byte_size, reserved=engine.reserved, variables=tuple(
                        dict(name=name, byte_offset=offset, byte_size=size) for name, offset, size in engine.table_variables))
                elif "byte_size" not in row:
                    row.update(byte_size=int(binding.byte_size), variables=variables)
                elif int(binding.byte_size) > row["byte_size"] and binding.name.casefold() != build.buffer_layout.name.casefold():
                    shader = program.shader(replacement.stage) if hasattr(program, "shader") else None
                    reflected = getattr(getattr(shader, "reflection", None), "reflection", None)
                    native_size = max((int(item.byte_size) for item in getattr(reflected, "constant_buffers", ())
                                       if item.name == binding.name), default=int(row["byte_size"]))
                    if int(binding.byte_size) > native_size:
                        raise MaterialBuildError(f"Buffer {binding.name!r} exceeds its native SDF allocation/compiled schema.")
                # e.g. RE8 Vulkan declares RootConstant.constant32Bits as 4 bytes inside a 16-byte SDF allocation.
            for binding in replacement.engine_resource_bindings:
                rows = resources if binding.table == "shader_resource" else other_resources[binding.table]
                register_class = RESOURCE_TABLES[binding.table]
                if binding.name not in rows and (replacement.stage, binding.name, register_class, binding.bind_point, 0) in implicit:
                    continue
                engine = None
                if binding.name not in rows:
                    engine = engine_resource_layout(document, program, replacement.stage, binding.name)[0]
                    if engine is None or engine.table != binding.table:
                        raise MaterialBuildError(f"The game does not bind engine resource {binding.name!r} in "
                                                 f"{program.name} {replacement.stage}.")
                row = bind(rows, binding.name, replacement.stage, binding.bind_point,
                           rows[binding.name]["type_code"] if engine is None else engine.type_code,
                           register_class=register_class)
                if engine is not None and row is not None:
                    row["reserved"] = engine.reserved
                if binding.table != "shader_resource":
                    changed_tables.add(binding.table)
        for key, row in buffers.items():
            if key == build.buffer_layout.name:
                original_buffer = next((b for b in program.constant_buffers if b.name == key), None)
                row.update(
                    byte_size=int(build.buffer_layout.byte_size),
                    variables=(_material_buffer_metadata_variables(original_buffer, build.buffer_layout)
                               if original_buffer is not None else tuple(
                                   dict(name=v.name, byte_offset=v.byte_offset, byte_size=v.byte_size)
                                   for v in build.buffer_layout.variables
                               )),
                )
        for row in (*resources.values(), *buffers.values(),
                    *(row for rows in other_resources.values() for row in rows.values())):
            row["bind_slots"] = [value if row["stage_mask"] & (1 << index) else 0
                                 for index, value in enumerate(row["bind_slots"])]
        editor.set_named_resource_table(target_index, program_index, "shader_resource", [
            row for key, row in resources.items() if key not in removed or row["stage_mask"]
        ])
        editor.set_constant_buffer_table(target_index, program_index, [
            row for row in buffers.values() if row["stage_mask"]
        ])
        for kind,rows in other_resources.items():
            if kind in changed_tables:
                editor.set_named_resource_table(target_index,program_index,kind,list(rows.values()))

    apply_pipeline_states(editor, build.pipeline_states)
    layout = build.buffer_layout
    if not layout.name:
        return
    for target in document.sdf.targets:
        for program in target.programs:
            if (target.index, program.program_index) in grouped:
                continue
            for index, buffer in enumerate(program.constant_buffers):
                if buffer.name.casefold() == layout.name.casefold():
                    editor.set_resource_metadata(
                        target.index, program.program_index, "constant_buffer", index,
                        name=layout.name, byte_size=layout.byte_size,
                        variables=_material_buffer_metadata_variables(buffer, layout),
                    )


@dataclass(frozen=True, slots=True)
class MaterialAuthoringCoverage:
    total_program_records: int
    material_consumer_keys: tuple[tuple[int, int, str], ...]
    rebuildable_keys: tuple[tuple[int, int, str], ...]
    family_rows: tuple[tuple[str, int, int, int], ...]
    member_coverages: tuple["MaterialAuthoringCoverage", ...] = ()

    @property
    def material_consumer_records(self) -> int:
        return len(self.material_consumer_keys)

    @property
    def rebuildable_records(self) -> int:
        consumers = set(self.material_consumer_keys)
        return sum(key in consumers for key in self.rebuildable_keys)

    @property
    def uncovered_keys(self) -> tuple[tuple[int, int, str], ...]:
        rebuilt = set(self.rebuildable_keys)
        return tuple(key for key in self.material_consumer_keys if key not in rebuilt)

    @property
    def complete(self) -> bool:
        return bool(
            self.material_consumer_keys
            and not self.uncovered_keys
            and all(member.complete for member in self.member_coverages)
        )


def material_authoring_coverage(document, project=None) -> MaterialAuthoringCoverage:
    if project is not None and project.domain == "implementation":
        required = material_program_routes(document, project)
        authored = implementation_routes(project)
        covered = tuple(route for route in required if route in authored and authored[route].contract.stage in SHADER_GRAPH_STAGES
            and document.sdf.targets[route[0]].programs[route[1]].shader(route[2]).binary_format in {"dxbc-container", "spirv-module"})
        return MaterialAuthoringCoverage(sum(len(target.programs) for target in document.sdf.targets), required,
                                         covered, ())
    family = getattr(document, "material_family", None)
    if family is not None:
        ready = project is not None and project._prepare_semantics is None
        rows = tuple(material_authoring_coverage(member, family_member_project(project, lod) if ready else None)
                     for lod, member in enumerate(family.members))
        return replace(rows[0], member_coverages=rows[1:])
    profile = document.profile.shader_graph_profile
    renderer = renderer_only(document, profile, project)
    rebuildable = set(material_program_routes(document, project))
    user_buffers = {
        str(name).casefold()
        for name in document.profile.user_material_buffer_names
    }
    material_instance_resources = {*profile.material_instance_resource_names, profile.instanced_material_buffer} - {""}
    if not document.material_interface.parameters and not document.material_interface.descriptor_layouts and not (project and project.parameters):
        material_instance_resources.clear()
    material_textures = {str(item.name).casefold() for item in profile.editable_textures(document)}
    if project is not None:
        material_textures.update(str(name).casefold() for name in project.source_texture_names)
        material_textures.update(t.name.casefold() for t in project.textures)
    semantics_by_key = {(item.target_index, item.program_index): item for item in document.programs}
    consumers: set[tuple[int, int, str]] = set()
    family_totals: dict[str, int] = {}
    family_consumers: dict[str, int] = {}
    family_rebuildable: dict[str, int] = {}
    for target in document.sdf.targets:
        for program in target.programs:
            key = (int(target.index), int(program.program_index))
            family = str(semantics_by_key[key].descriptor.family)
            family_totals[family] = family_totals.get(family, 0) + 1
            for shader in program.shaders:
                if not shader.present or shader.role not in profile.authoring_roles:
                    continue
                stage_bit = 1 << STAGE_KEYS.index(shader.role)
                route = (*key, shader.role)
                outer_consumes_material = renderer or any(
                    item.name.casefold() in user_buffers and binding.stage_mask & stage_bit
                    for item, binding in zip(program.constant_buffers, program.bindings[0])
                ) or any(
                    (item.name.casefold() in material_textures or item.name in material_instance_resources)
                    and binding.stage_mask & stage_bit
                    for item, binding in zip(program.shader_resources, program.bindings[2])
                )
                consumes_material = outer_consumes_material
                if not consumes_material:
                    try:
                        contract = material_program_contract(document, *route)
                    except (SdfContractError, ShaderGraphError):
                        pass
                    else:
                        consumes_material = any(
                            buffer.name.casefold() in user_buffers for buffer in contract.constant_buffers
                        ) or any(
                            resource.name.casefold() in material_textures or resource.name in material_instance_resources
                            for resource in contract.resources
                        )
                if consumes_material:
                    consumers.add(route)
                    family_consumers[family] = family_consumers.get(family, 0) + 1
                    if route in rebuildable:
                        family_rebuildable[family] = family_rebuildable.get(family, 0) + 1
    families = sorted(family_totals, key=lambda value: (value.replace("-", " ").casefold(), value))
    return MaterialAuthoringCoverage(
        total_program_records=len(document.programs),
        material_consumer_keys=tuple(sorted(consumers)),
        rebuildable_keys=tuple(sorted(rebuildable)),
        family_rows=tuple(
            (family, family_totals[family], family_consumers.get(family, 0), family_rebuildable.get(family, 0))
            for family in families
        ),
    )


def _material_texture_name(definition) -> str:
    # The shader declares a texture by its definition's name; the source name only matches it to the recovered one.
    name = str(definition.name)
    if not _NAME.fullmatch(name):
        raise MaterialBuildError(
            f"{definition.name} cannot be represented by this game's texture naming rules."
        )
    return name


def _material_texture_type_code(profile, dimension: str) -> int:
    try:
        value = int(profile.material_texture_type_code(str(dimension)))
    except (TypeError, ValueError) as exc:
        raise MaterialBuildError(
            f"This game profile cannot encode a {dimension} material texture."
        ) from exc
    if not 0 <= value <= 0xFF:
        raise MaterialBuildError("The game profile returned an invalid texture type code.")
    return value


def _augment_material_textures(
    project: MmtrMaterialProject,
    contract: ShaderContract,
    profile,
    document,
    stage: str,
) -> ShaderContract:
    resources = list(contract.resources)
    source_names = {
        str(name)
        for name in project.source_texture_names
        if str(name)
    }
    target_names: dict[str, str] = {}
    same_profile = same_mmtr_profile(project.source_profile, profile.profile_id)
    for definition in project.textures:
        if definition.dimension is None:
            continue
        target_name = _material_texture_name(definition)
        previous = target_names.get(target_name)
        if previous is not None:
            raise MaterialBuildError(
                f"{definition.name} and {previous} map to the same target-game texture slot."
            )
        target_names[target_name] = definition.name
        expected_dimension = _TEXTURE_CONTRACT_DIMENSIONS[definition.dimension]
        resource = next((item for item in resources if item.name == target_name), None)
        if resource is None and (
            not same_profile
            or (
                bool(definition.source_name)
                and definition.source_name not in source_names
            )
        ):
            resource = next((item for item in contract.resources
                             if item.name == definition.source_name and item.sampleable), None)
        if resource is not None:
            if int(resource.register_space) != 0:
                raise MaterialBuildError(
                    f"{definition.name} resolves to renderer-owned register "
                    f"space {resource.register_space}, not an MMTR material slot."
                )
            if (
                not definition.source_name
                and target_name not in source_names
                and same_profile
            ):
                raise MaterialBuildError(
                    f"{definition.name} collides with an engine-owned shader resource."
                )
            if not resource.sampleable:
                raise MaterialBuildError(
                    f"{definition.name} maps to a non-texture shader resource."
                )
            if resource.dimension != expected_dimension:
                raise MaterialBuildError(
                    f"{definition.name} is {definition.dimension}, but the target slot is {resource.dimension}."
                )
            continue

        bind_point = free_register(document, replace(contract, resources=tuple(resources)), stage, "t")
        if bind_point is None:
            raise MaterialBuildError(
                "This material pass has no free D3D11 texture register."
            )
        resources.append(
            ShaderContractResource(
                name=target_name,
                input_type="texture",
                return_type="float",
                dimension=expected_dimension,
                sample_count=0xFFFFFFFF,
                bind_point=bind_point,
                bind_count=1,
                flags=(4 - 1) << 2,  # Public material textures are float4, not scalar float.
                register_space=0,
            )
        )
    return replace(contract, resources=tuple(
        resource for resource in resources
        if resource.register_class != "t" or resource.name not in source_names
        or resource.name in target_names
    ))


def _material_texture_bindings(
    project: MmtrMaterialProject,
    contract: ShaderContract,
    profile,
    document,
    semantics,
    *,
    compiled_contract,
    required: bool = True,
) -> tuple[MaterialTextureBinding, ...]:
    bindings = []
    for definition in project.textures:
        if definition.dimension is None:
            continue
        name = _material_texture_name(definition)
        resource = next((item for item in contract.resources if item.name == name), None)
        if resource is None or not resource.sampleable:
            if (not required or document.material_interface.parameter_storage in BINDLESS_PARAMETER_STORAGES
                    or any(n.properties.get("parameter_storage") in BINDLESS_PARAMETER_STORAGES
                           for n in project.graph.nodes)):
                continue
            raise MaterialBuildError(
                f"{definition.name} is unavailable in one required material pass."
            )
        if int(resource.register_space) != 0:
            raise MaterialBuildError(
                f"{definition.name} resolves to a renderer-owned register space."
            )
        expected_dimension = _TEXTURE_CONTRACT_DIMENSIONS[definition.dimension]
        if resource.dimension != expected_dimension:
            raise MaterialBuildError(
                f"{definition.name} has an inconsistent texture dimension."
            )
        consumed = compiled_contract.resource(resource.name)
        bind_point = (
            int(profile.material_output_binding_point(document, semantics, compiled_contract, consumed))
            if consumed is not None else int(resource.bind_point)
        )
        bindings.append(
            MaterialTextureBinding(
                texture_id=definition.texture_id,
                name=resource.name,
                bind_point=bind_point,
                type_code=_material_texture_type_code(profile, definition.dimension),
            )
        )
    return tuple(bindings)


def _material_constant_buffer_bindings(
    contract: ShaderContract,
    profile,
    document,
    semantics,
) -> tuple[MaterialSystemBufferBinding, ...]:
    bindings: list[MaterialSystemBufferBinding] = []
    for buffer in contract.constant_buffers:
        if int(buffer.register_space) != 0:
            continue
        resource = next(
            (
                item
                for item in contract.resources
                if item.input_type == "constant-buffer"
                and item.name.casefold() == buffer.name.casefold()
            ),
            None,
        )
        if resource is None:
            raise MaterialBuildError(
                f"Constant buffer {buffer.name} has no shader resource contract."
            )
        bind_point = int(profile.material_output_binding_point(document, semantics, contract, resource))
        bindings.append(
            MaterialSystemBufferBinding(
                name=buffer.name,
                bind_point=bind_point,
                byte_size=int(buffer.byte_size),
                variables=tuple(
                    MaterialSystemBufferVariable(
                        name=variable.name,
                        byte_offset=int(variable.byte_offset),
                        byte_size=int(variable.byte_size),
                    )
                    for variable in buffer.variables
                ),
            )
        )
    return tuple(bindings)


def _engine_resource_bindings(
    contract: ShaderContract,
    names,
    profile,
    document,
    semantics,
) -> tuple[MaterialEngineResourceBinding, ...]:
    tables = {register_class: table for table, register_class in RESOURCE_TABLES.items()}
    bindings = []
    for resource in contract.resources:
        if resource.name not in names or int(resource.register_space) != 0 or resource.register_class not in tables:
            continue
        bind_point = int(profile.material_output_binding_point(document, semantics, contract, resource))
        bindings.append(MaterialEngineResourceBinding(tables[resource.register_class], resource.name, bind_point))
    return tuple(bindings)


def _augmented_contract(
    project: MmtrMaterialProject,
    contract: ShaderContract,
    profile,
    document,
    stage: str,
    *,
    material_buffer: ShaderContractBuffer | None = None,
    augment_textures: bool = True,
) -> tuple[ShaderContract, MaterialBufferLayout]:
    if augment_textures:
        contract = _augment_material_textures(project, contract, profile, document, stage)
    buffer_name = profile.user_buffer_name
    declared_buffer = contract.buffer(buffer_name)
    original = material_buffer or declared_buffer
    instances = None
    if original is None:
        instances = contract.resource(profile.instanced_material_buffer) if profile.instanced_material_buffer else None
        if instances is None or instances.input_type != "structured-buffer" or instances.sample_count <= 0:
            return contract, MaterialBufferLayout("", 0, ())
        original = contract.structured_buffer(instances.name) or ShaderContractBuffer(
            instances.name, instances.sample_count, instances.bind_point, instances.bind_count, (),
        )
    logical_name, shader_name = profile.logical_parameter_name, profile.shader_parameter_name
    target_name_resolver = profile.material_parameter_name
    existing = {
        logical_name(variable.name): variable
        for variable in original.variables
    }
    folded_existing: dict[str, list[ShaderContractVariable]] = {}
    for logical, variable in existing.items():
        folded_existing.setdefault(logical.casefold(), []).append(variable)
    variables: list[ShaderContractVariable] = []
    layout_variables: list[MaterialBufferVariable] = []
    occupied: list[tuple[int, int]] = []
    occupied_words: set[int] = set()
    pending: list[tuple[MaterialParameterDefinition, str]] = []
    target_names: dict[str, str] = {}

    def place(definition, target_name, offset, size):
        variables.append(ShaderContractVariable(
            shader_name(target_name), offset, size, definition.value_type, definition.value_type))
        layout_variables.append(MaterialBufferVariable(
            target_name, offset, size, definition.value_type, tuple(float(value) for value in definition.default),
            str(definition.source_name or "")))

    for definition in project.parameters:
        target_name = str(
            target_name_resolver(
                definition,
                contract,
                project.source_profile,
            )
        )
        if not _NAME.fullmatch(target_name):
            raise MaterialBuildError(
                f"{definition.name} cannot be represented by this game's material naming rules."
            )
        if target_name in target_names:
            raise MaterialBuildError(
                f"{definition.name} and {target_names[target_name]} map to the same target-game parameter."
            )
        target_names[target_name] = definition.name
        candidates = (
            target_name,
            str(definition.source_name),
            definition.name,
        )
        source = next(
            (existing[key] for key in candidates if key and key in existing),
            None,
        )
        if source is None:
            source = next(
                (
                    matches[0]
                    for key in candidates
                    if key
                    for matches in (folded_existing.get(key.casefold(), ()),)
                    if len(matches) == 1
                ),
                None,
            )
        size = value_width(definition.value_type) * 4
        offset = definition.source_byte_offset
        if source is not None and offset is None:
            offset = source.byte_offset
        valid_source = (
            offset is not None
            and int(offset) >= 0
            and int(offset) % 4 == 0
            and int(offset) + size <= original.byte_size
            and occupied_words.isdisjoint(range(int(offset), int(offset) + size, 4))
            and (declared_buffer is None or int(offset) % 16 + size <= 16)
        )
        if not valid_source:
            pending.append((definition, target_name))
            continue
        resolved_offset = int(offset)
        occupied.append((resolved_offset, resolved_offset + size))
        occupied_words.update(range(resolved_offset, resolved_offset + size, 4))
        place(definition, target_name, resolved_offset, size)

    parameter_names = {name.casefold() for name in target_names}
    for variable in original.variables:
        if logical_name(variable.name).casefold() in parameter_names:
            continue
        if any(variable.byte_offset < stop and variable.byte_offset + variable.byte_size > start for start, stop in occupied):
            continue
        variables.append(variable)
        occupied.append((variable.byte_offset, variable.byte_offset + variable.byte_size))

    cursor = align(original.byte_size, 16)
    for definition, target_name in pending:
        size = value_width(definition.value_type) * 4
        cursor = align(cursor, 4)
        if cursor % 16 + size > 16:
            cursor = align(cursor, 16)
        place(definition, target_name, cursor, size)
        cursor += size
    byte_size = max(original.byte_size, align(cursor, 16))
    replacement_buffer = replace(
        original,
        byte_size=byte_size,
        variables=tuple(sorted(variables, key=lambda item: (item.byte_offset, item.name.casefold()))),
    )
    if instances is not None:
        adjusted = replace(
            contract,
            resources=tuple(
                replace(item, sample_count=byte_size) if item.name == instances.name else item
                for item in contract.resources
            ),
            structured_buffers=tuple(
                item for item in contract.structured_buffers if item.name != instances.name
            ) + (replacement_buffer,),
        )
    else:
        adjusted = replace(
            contract,
            constant_buffers=tuple(
                replacement_buffer
                if item.name.casefold() == buffer_name.casefold()
                else item
                for item in contract.constant_buffers
            ),
        )
    return adjusted, MaterialBufferLayout(
        buffer_name,
        byte_size,
        tuple(sorted(layout_variables, key=lambda item: (item.byte_offset, item.name.casefold()))),
    )


def build_material_project(project: MmtrMaterialProject, document) -> MmtrMaterialBuild:
    try:
        validate_pipeline_states(document, project.pipeline_states)
    except PipelineSettingError as exc:
        raise MaterialBuildError(str(exc)) from exc
    if getattr(document, "material_family", None) is not None:
        if project.implementations:
            raise MaterialBuildError("Graph-owned bodies in shader-LOD families need explicit per-member renderer templates.")
        return build_material_family(project, document)
    return _build_material_project(project, document)


def _build_material_project(project: MmtrMaterialProject, document) -> MmtrMaterialBuild:
    try:
        project.prepare_semantics()
    except MaterialProjectError as exc:
        raise MaterialBuildError(
            "The material's shader recipes could not be reconstructed.",
            technical_details=str(exc),
        ) from exc
    from .material_region_read import region_read_session
    with region_read_session():
        return _build_prepared_material_project(project, document)


def _build_prepared_material_project(project: MmtrMaterialProject, document) -> MmtrMaterialBuild:
    try:
        project = replace(
            project,
            graph=_expand_material_functions_for_compilation(project.graph, source_profile=project.source_profile, live_only=True),
        )
    except MaterialFunctionError as exc:
        raise MaterialBuildError(
            "A reusable material function could not be expanded.",
            technical_details=str(exc),
        ) from exc

    profile = document.profile.shader_graph_profile
    if project.domain != "implementation":
        try:
            profile.validate_material_project_edit(project, document)
        except ShaderGraphError as exc:
            raise MaterialBuildError(str(exc)) from exc
    try:
        targets = material_program_routes(document, project)
    except ShaderGraphError as exc:
        raise MaterialBuildError(
            "This material project does not match the selected master material.",
            technical_details=str(exc),
        ) from exc
    if not targets:
        raise MaterialBuildError("This master material has no supported surface passes.")
    try:
        authored_routes = implementation_routes(project)
        if not set(authored_routes).issubset(targets):
            raise ShaderGraphError("An editable implementation names a route outside this renderer template.")
        if project.domain == "implementation" and set(authored_routes) != set(targets):
            raise ShaderGraphError("A graph-owned material must implement every required renderer route.")
    except ShaderGraphError as exc:
        raise MaterialBuildError(str(exc)) from exc

    cache: dict[str, _CompiledMaterialShader] = {}
    authored_keys = {id(item): item.content_hash for item in project.implementations}
    authored_native = {}
    contract_fingerprints = {}
    replacements: list[MaterialShaderReplacement] = []
    layout: MaterialBufferLayout | None = None
    texture_layout: tuple[MaterialTextureSlot, ...] | None = None
    project_owns_contract = profile.material_project_owns_contract(project)
    prepared: dict[str, _PreparedMaterialShader] = {}
    compiled_results: dict[str, object] = {}

    engine_buffers, engine_resources = graph_renderer_buffers(project.graph), graph_renderer_resources(project.graph)

    def prepare_route(target_index, program_index, shader_role, *, first):
        authored = authored_routes.get((target_index, program_index, shader_role))
        program = document.sdf.targets[int(target_index)].programs[int(program_index)]
        shader = program.shader(shader_role)
        if shader is None or not shader.present:
            raise MaterialBuildError(f"A required material pass is missing its {shader_role} stage.")
        # Equal shaders of different passes share a compilation only when the game binds the same engine inputs.
        plan = engine_input_plan(document, program, shader_role, engine_buffers, engine_resources)
        try:
            base_contract = material_program_contract(
                document,
                int(target_index),
                int(program_index),
                shader_role,
                allow_unreflected_material_fields=project_owns_contract or authored is not None,
            )
        except (SdfContractError, ShaderGraphError) as exc:
            raise MaterialBuildError(
                "A required surface pass does not have a complete native shader contract.",
                technical_details=f"{pass_description(document, target_index, program_index, shader_role)}: {exc}",
            ) from exc
        semantics = replace(document.program(int(target_index), int(program_index)), shader_role=shader_role)
        if authored is not None:
            try:
                base_contract = authored.renderer_contract(implementation_contract(base_contract, shader_bytes(shader)))
            except ShaderGraphError as exc:
                raise MaterialBuildError(str(exc)) from exc
        if first:
            errors = [item.message for item in validate_material_project(project, base_contract)
                      if item.severity == "error"]
            if errors:
                raise MaterialBuildError(
                    "The material graph needs attention before it can be applied.",
                    technical_details="\n".join(errors),
                )
        recovered_product = None
        try:
            if authored is not None:
                if shader.binary_format not in {"dxbc-container", "spirv-module"}:
                    raise ShaderGraphError("This shader backend needs a native binding adapter for graph-owned bodies.")
                native_key = (target_index, shader.offset, shader.size)
                if native_key not in authored_native:
                    authored_native[native_key] = implementation_execution(shader_bytes(shader))
                execution = authored_native[native_key]
                semantic_key = f"{authored_keys[id(authored)]}:execution={execution!r}"
            else:
                semantic_key = profile.material_project_shader_key(project, base_contract, semantics, document)
            fingerprint = _cached_contract_fingerprint(base_contract, contract_fingerprints)
            cache_key = f"{fingerprint}:{semantic_key or ''}:{plan!r}"
            cached = prepared.get(cache_key)
            if cached is None:
                if authored is not None:
                    # A body reads only its template's resources; added material textures would become new bindings.
                    adjusted, candidate_layout = _augmented_contract(project, base_contract, profile, document,
                                                                     shader_role, augment_textures=False)
                    rebound = authored._bind_material_interface_for_compilation(base_contract, adjusted, project, profile)
                    adjusted = rebound.contract
                    generated = rebound.generate(adjusted, early_depth_stencil=execution.early_depth_stencil, source_profile=project.source_profile)
                    recovered_product = (generated, adjusted, candidate_layout, semantic_key)
                else:
                    recovered_product = profile.generate_recovered_material_shader_source(
                        project, document, base_contract, semantics)
                recovered_semantic_key = (
                    str(recovered_product[3])
                    if recovered_product is not None and len(recovered_product) > 3
                    else base_contract.source_sha256 if recovered_product is not None else ""
                )
                if semantic_key != recovered_semantic_key:
                    raise ShaderGraphError("The material's compilation key disagrees with its semantic program.")
        except (ShaderGraphError, HlslCompileError) as exc:
            unavailable = _engine_input_unavailable(exc)
            if unavailable is not None:
                from .profiles.renderer_inputs import (
                    RendererValueUnavailable, engine_buffer_unavailable_reason, engine_resource_unavailable_reason,
                )
                reason = (engine_buffer_unavailable_reason(document, program, shader_role, base_contract, unavailable.buffer)
                          if isinstance(unavailable, RendererValueUnavailable) else
                          engine_resource_unavailable_reason(document, program, shader_role, base_contract,
                                                             unavailable.properties))
                raise MaterialBuildError(
                    f"{unavailable.label} is not available in every rendering pass this graph drives.",
                    technical_details=f"{pass_description(document, target_index, program_index, shader_role)}: {reason}",
                ) from exc
            raise MaterialBuildError(
                "REasy could not recover this material for its {purpose} passes.".format(
                    purpose=purpose_title(pass_purpose(document, profile, target_index, program_index))),
                technical_details=f"{pass_description(document, target_index, program_index, shader_role)}: {exc}",
            ) from exc
        if cached is None:
            if recovered_product is None:
                raise MaterialBuildError("Material graph compilation is not implemented for this game yet.")
            generated, adjusted, candidate_layout = recovered_product[:3]
            cached = _PreparedMaterialShader(
                MaterialShaderCompileRequest(generated, adjusted, semantics, shader, int(target_index)),
                candidate_layout,
                authored,
                execution if authored is not None else None,
            )
            prepared[cache_key] = cached
        return _PreparedMaterialRoute(
            int(target_index), int(program_index), shader_role, cache_key, cached,
        )

    def finish_route(route):
        nonlocal layout, texture_layout
        target_index, program_index, shader_role = route.target_index, route.program_index, route.shader_role
        cache_key = route.cache_key
        request = route.shader.request
        generated, adjusted = request.generated, request.adjusted_contract
        semantics, shader = request.semantics, request.shader
        candidate_layout = route.shader.layout
        authored, execution = route.shader.authored, route.shader.execution
        cached = cache.get(cache_key)
        if cached is None:
            try:
                compiled = (compile_implementation(generated, adjusted, shader.binary_format) if authored is not None
                            else compiled_results[cache_key])
                if isinstance(compiled, Exception):
                    if isinstance(compiled, (ShaderGraphError, HlslCompileError, SdfContractError)):
                        raise compiled
                    raise ShaderGraphError(f"{type(compiled).__name__}: {compiled}") from compiled
                binary_format = str(
                    getattr(compiled, "binary_format", "dxbc-container")
                )
                if authored is not None:
                    if execution != implementation_execution(compiled.bytecode):
                        raise ShaderGraphError("The replacement changed early depth/stencil execution.")
                profile.validate_material_shader_bytecode(compiled.bytecode)
                candidate_contract = getattr(compiled, "semantic_contract", None)
                if candidate_contract is None:
                    candidate_contract = profile.compiled_material_shader_contract(compiled.bytecode, binary_format)
                report = validate_shader_contract(adjusted, candidate_contract)
            except (ShaderGraphError, HlslCompileError, SdfContractError) as exc:
                raise MaterialBuildError(
                    "REasy could not build this material for its {purpose} passes.".format(
                        purpose=purpose_title(pass_purpose(document, profile, target_index, program_index))),
                    technical_details=f"{pass_description(document, target_index, program_index, shader_role)}: {exc}",
                ) from exc
            if not report.compatible:
                raise MaterialBuildError(
                    "REasy could not safely map this material to every required rendering path.",
                    technical_details=f"{pass_description(document, target_index, program_index, shader_role)}:\n"
                                      + "\n".join(item.message for item in report.errors),
                )
            candidate_texture_interface = _material_texture_bindings(
                project,
                adjusted,
                profile,
                document,
                semantics,
                compiled_contract=candidate_contract,
                required=authored is None,
            )
            used_texture_names = {
                resource.name
                for resource in candidate_contract.resources
                if resource.sampleable
            }
            candidate_texture_bindings = tuple(
                item
                for item in candidate_texture_interface
                if item.name in used_texture_names
            )
            candidate_engine_resource_bindings = _engine_resource_bindings(
                candidate_contract, engine_resources, profile, document, semantics,
            )
            used_constant_buffers = {
                item.name.casefold()
                for item in candidate_contract.constant_buffers
                if int(item.register_space) == 0
            }
            candidate_constant_buffer_bindings = tuple(
                item
                for item in _material_constant_buffer_bindings(
                    candidate_contract,
                    profile,
                    document,
                    semantics,
                )
                if item.name.casefold() in used_constant_buffers
            )
            cached = _CompiledMaterialShader(
                bytecode=compiled.bytecode,
                binary_format=binary_format,
                entry_point=generated.entry_point,
                layout=candidate_layout,
                texture_interface=candidate_texture_interface,
                texture_bindings=candidate_texture_bindings,
                constant_buffer_bindings=candidate_constant_buffer_bindings,
                diagnostics=getattr(compiled, 'diagnostics', ''),
                resource_bindings=candidate_contract.resources,
                engine_resource_bindings=candidate_engine_resource_bindings,
            )
            cache[cache_key] = cached
        candidate_layout = cached.layout
        if layout is None or (
            layout.byte_size == 0 and candidate_layout.byte_size > 0
        ):
            layout = candidate_layout
        elif candidate_layout.byte_size > 0 and layout != candidate_layout:
            raise MaterialBuildError(
                "The master material uses inconsistent parameter layouts across required passes."
            )
        binding_by_id = {item.texture_id: item for item in cached.texture_interface}
        candidate_texture_layout = tuple(
            MaterialTextureSlot(
                texture_id=definition.texture_id,
                name=(
                    binding_by_id[definition.texture_id].name
                    if definition.texture_id in binding_by_id
                    else _material_texture_name(definition)
                ),
                dimension=definition.dimension,
                default_path=definition.default_path,
                source_name=definition.source_name,
            )
            for definition in project.textures
        )
        if texture_layout is None:
            texture_layout = candidate_texture_layout
        elif texture_layout != candidate_texture_layout:
            raise MaterialBuildError(
                "The master material maps texture slots inconsistently across required passes."
            )
        replacements.append(
            MaterialShaderReplacement(
                int(target_index),
                int(program_index),
                shader_role,
                cached.bytecode,
                cached.texture_bindings,
                cached.constant_buffer_bindings,
                cached.binary_format,
                cached.entry_point,
                cached.diagnostics,
                cached.resource_bindings,
                cached.engine_resource_bindings,
            )
        )

    staged_routes = []
    preparation_error = None
    for index, (target_index, program_index, shader_role) in enumerate(targets):
        try:
            staged_routes.append(prepare_route(target_index, program_index, shader_role, first=index == 0))
        except Exception as exc:
            preparation_error = exc
            break
    pending = [(key, product.request) for key, product in prepared.items() if product.authored is None]
    if pending:
        try:
            results = tuple(profile.compile_material_shaders(project, tuple(request for _key, request in pending)))
            if len(results) != len(pending):
                raise ShaderGraphError("The material compiler returned an incomplete shader batch.")
        except Exception as exc:
            results = (exc,) * len(pending)
        compiled_results.update((key, result) for (key, _request), result in zip(pending, results))
    for route in staged_routes:
        finish_route(route)
    if preparation_error is not None:
        raise preparation_error
    if layout is None:
        raise MaterialBuildError("No material build products were generated.")
    if not layout.variables:
        shipped = {str(item.name) for item in document.material_interface.parameters}
        stranded = [item.name for item in project.parameters
                    if not is_private_material_parameter(item.name)
                    and item.name not in shipped and item.source_name not in shipped]
        if stranded:
            raise MaterialBuildError(
                "This material has no parameter buffer, so new parameters cannot be saved: "
                + ", ".join(stranded[:8]) + ("…" if len(stranded) > 8 else "") + ".",
                technical_details="No material pass binds the material constant buffer or instance records.",
            )
    descriptor_layouts = profile.material_descriptor_layout(project, document, layout)
    if descriptor_layouts[1] is not None:
        slots = {slot.name: slot for slot in texture_layout or ()}
        texture_layout = tuple(
            slots.get(name) or MaterialTextureSlot(f"descriptor-slot:{name}", name, None, source_name=name)
            for name in descriptor_layouts[1].textures
        )
    return MmtrMaterialBuild(
        profile=profile.profile_id,
        replacements=tuple(replacements),
        buffer_layout=layout,
        texture_layout=texture_layout or (),
        source_texture_names=tuple(
            dict.fromkeys(
                str(name)
                for name in project.source_texture_names
                if str(name)
            )
        ),
        compiled_contracts=len(cache),
        source_descriptor_layout=descriptor_layouts[0],
        descriptor_layout=descriptor_layouts[1],
        pipeline_states=project.pipeline_states,
    )


__all__ = [
    "MATERIAL_SCHEMA",
    "MATERIAL_SCHEMA_VERSION",
    "MATERIAL_TEXTURE_DIMENSIONS",
    "MATERIAL_VALUE_TYPES",
    "MaterialAuthoringCoverage",
    "MaterialBufferLayout",
    "MaterialBufferVariable",
    "MaterialBuildError",
    "MaterialParameterDefinition",
    "MaterialProjectError",
    "MaterialShaderReplacement",
    "MaterialShaderCompileRequest",
    "MaterialEngineResourceBinding",
    "MaterialSystemBufferBinding",
    "MaterialSystemBufferVariable",
    "MaterialTextureBinding",
    "MaterialTextureDefinition",
    "MaterialTextureSlot",
    "MmtrMaterialBuild",
    "MmtrMaterialProject",
    "apply_material_build",
    "build_material_project",
    "ensure_material_interface_nodes",
    "material_semantics_complete",
    "material_interface_matches_build",
    "material_interface_matches_layout",
    "material_parameter_abi",
    "material_program_contract",
    "material_resource_dimension",
    "material_authoring_coverage",
    "new_material_project",
    "prune_orphaned_material_inputs",
    "valid_default_path",
    "validate_material_project",
]
